"""D4 Fake acceptance over the existing D3 application and persistence boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_d3_chain import Clock, Model, ReviewModel, Sandbox, Token

from agent_adapters.multi_turn_application import D3Application
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import (
    D3Authorization,
    D3RecoveryRequired,
    D3RunConfig,
    D3TurnBinding,
)
from agent_core.dual_llm_modeling import (
    D3ActionContext,
    D3OuterContext,
    D3ProposalRejected,
)
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4BudgetPolicy, D4RecoveryRequired
from agent_core.review_contracts import D2ReviewRequest
from agent_core.turn_contracts import D1RoleConfig


class ProcessLoss(BaseException):
    pass


class StaleOnceToken:
    def __init__(self):
        self.cancelled = False
        self.stale_next = False

    @property
    def is_cancelled(self):
        if self.stale_next:
            self.stale_next = False
            return True
        return self.cancelled


def make_d4_scenario(directory: Path, *, attachment_count: int = 2):
    source = directory / "source"
    source.mkdir(parents=True)
    attachments = []
    for index in range(attachment_count):
        name = f"attachment-{index}.txt"
        (source / name).write_bytes(f"D4 attachment {index}".encode())
        attachments.append({"kind": "local_path", "path": name})

    outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1")
    config = D3RunConfig(
        outer_role,
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D3Authorization(str(uuid4()), tuple(range(attachment_count))),
        outer_role.role_config_ref,
    )
    config = replace(config, d4_budget=D4BudgetPolicy())
    sandbox, clock, token = Sandbox(), Clock(), Token()
    outer, inner = Model(config.outer_role), Model(config.inner_role)
    review = ReviewModel(config.outer_role)
    app = D3Application(
        directory / "run",
        config=config,
        sandbox=sandbox,
        import_sandbox=sandbox,
        workspace_sandbox=sandbox,
        command_sandbox=sandbox,
        outer_model=outer,
        inner_model=inner,
        review_model=review,
        clock=clock,
    )
    app.start(
        TaskRequest.from_dict(
            {
                "goal": "verify authorized local copies",
                "target": {"attachments": attachments},
            }
        ),
        source,
    )
    return [app, sandbox, outer, inner, review, clock, token]


@pytest.fixture
def d4_scenario(tmp_path):
    case = make_d4_scenario(tmp_path)
    try:
        yield case
    finally:
        close_scenario(case)


@pytest.fixture
def d4_single_scenario(tmp_path):
    case = make_d4_scenario(tmp_path, attachment_count=1)
    try:
        yield case
    finally:
        close_scenario(case)


def close_scenario(case):
    app, sandbox = case[0], case[1]
    task_run_id = app._bound_task_run_id
    assert task_run_id is not None
    task_run = app.store.load_task_run(task_run_id)
    assert task_run is not None
    has_unknown_operation = any(
        record.completion is None
        for record in app.store.list_d4_operations(task_run_id)
    )
    if has_unknown_operation:
        sandbox.destroy(task_run.sandbox_id)
        app.store.close()
        return
    if task_run.status.value == "running":
        app.cancel(task_run_id)
    task_run = app.store.load_task_run(task_run_id)
    assert task_run is not None
    if not sandbox.destroyed:
        sandbox.destroy(task_run.sandbox_id)
    if task_run.status.value == "running":
        app.store.close()
        return
    if app.controller._cache_invalid:
        app.controller.reload_state()
    app.close()


def reopen_scenario(case):
    original = case[0]
    task_run_id = original.state.task_run.task_run_id
    original.store.close()
    app = D3Application(
        original.directory,
        config=original.config,
        sandbox=case[1],
        import_sandbox=case[1],
        workspace_sandbox=case[1],
        command_sandbox=case[1],
        outer_model=case[2],
        inner_model=case[3],
        review_model=case[4],
        clock=case[5],
    )
    case[0] = app
    app.controller.attach(task_run_id)
    app._bound_task_run_id = task_run_id
    return app


def run(case):
    return asyncio.run(case[0].run(case[6]))


def _invocation_ids(model):
    return tuple(
        getattr(request, "model_request_id", getattr(request, "review_id", None))
        for request in model.calls
    )


def _assert_prior_invocations_were_not_repeated(model, prior_ids):
    invocation_ids = _invocation_ids(model)
    assert all(request_id is not None for request_id in invocation_ids)
    assert len(invocation_ids) == len(set(invocation_ids))
    assert all(invocation_ids.count(request_id) == 1 for request_id in prior_ids)


def _d4_operation_ids(app, task_run_id, category):
    return tuple(
        record.start.operation_id
        for record in app.store.list_d4_operations(task_run_id)
        if record.start.category == category
    )


def outer_proposal(attachment_index):
    return {
        "turn_target": f"verify attachment {attachment_index}",
        "completion_rule": {
            "rule_id": "attachment_copy_sha256",
            "version": 1,
            "parameters": {"attachment_index": attachment_index},
        },
        "intents": [{"description": "first strategy", "intent_type": "file_sha256"}],
    }


def outer_proposal_call(case, monkeypatch, responses):
    app, _, model, _, _, _, token = case
    receipts = asyncio.run(app._import_all(token))
    context = D3OuterContext(app.state.task_run.request.goal, receipts, None, None)
    request = app._model_request("outer_turn", "turn:1", context, "outer/turn/1")
    calls = []

    def invoke(model_request, timeout_seconds, invocation_token):
        calls.append(model_request)
        response = responses[len(calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(model, "invoke", invoke)

    def refresh(current):
        return replace(
            request,
            base_checkpoint_sequence=app.state.latest_checkpoint.sequence,
            context=replace(
                current.context, prior_turn_reason="refreshed after rejection"
            ),
        )

    def validate(_current, proposal):
        if proposal.attachment_index not in app.config.authorization.attachment_indices:
            raise D3ProposalRejected("d3_attachment_not_authorized")

    return request, calls, token, refresh, validate


def test_d4_normal_chain_replay_reuses_persisted_results(d4_scenario):
    result = run(d4_scenario)
    app = d4_scenario[0]
    task_run_id = result.task_run_id
    records = app.store.list_d4_operations(task_run_id)
    d3_records = app.store.list_d3_operations(task_run_id)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert records
    assert app.store.load_d3_run_config(task_run_id) == app.config
    assert len(records) >= len(d3_records)
    assert all(record.completion is not None for record in records)
    usage = app.store.d4_budget_usage(task_run_id)
    assert usage.total_used == sum(record.start.charged for record in records)

    calls = (
        len(d4_scenario[2].calls),
        len(d4_scenario[3].calls),
        len(d4_scenario[4].calls),
    )
    reopen_scenario(d4_scenario)
    replay = run(d4_scenario)
    assert replay == result
    assert (
        len(d4_scenario[2].calls),
        len(d4_scenario[3].calls),
        len(d4_scenario[4].calls),
    ) == calls
    assert d4_scenario[0].store.d4_budget_usage(task_run_id) == usage


def test_d4_turn_adjustment_replay_keeps_task_run_budget_cumulative(d4_scenario):
    app, _, outer, _, review, _, _ = d4_scenario

    def decide(request):
        if request.review_scope == "turn_start" and outer.turn_index == 0:
            outer.turn_index = 1
            return "adjust_turn_target"
        return "continue_current_intent"

    review.decision_for = decide
    result = run(d4_scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert [status for _, status in result.turn_statuses] == ["superseded", "completed"]
    before = app.store.d4_budget_usage(result.task_run_id)
    assert before.total_used == sum(
        record.start.charged
        for record in app.store.list_d4_operations(result.task_run_id)
    )
    call_counts = (len(outer.calls), len(d4_scenario[3].calls), len(review.calls))

    reopen_scenario(d4_scenario)
    replay = run(d4_scenario)
    assert replay == result
    assert d4_scenario[0].store.d4_budget_usage(result.task_run_id) == before
    assert (
        len(outer.calls),
        len(d4_scenario[3].calls),
        len(review.calls),
    ) == call_counts


def test_d4_semantic_rejection_uses_one_refreshed_same_scope_correction(
    d4_single_scenario, monkeypatch
):
    app = d4_single_scenario[0]
    request, calls, token, refresh, validate = outer_proposal_call(
        d4_single_scenario,
        monkeypatch,
        [outer_proposal(1), outer_proposal(0)],
    )
    request_id, proposal = asyncio.run(
        app.outer.propose(request, token, refresh=refresh, validate=validate)
    )
    assert proposal.attachment_index == 0
    assert len(calls) == 2
    first, correction = calls
    assert first.model_request_id == request.model_request_id
    assert correction.correction_of_model_request_id == first.model_request_id
    assert correction.rejection_code == "d3_attachment_not_authorized"
    assert (
        correction.task_run_id,
        correction.stage,
        correction.role,
        correction.resource_ref,
    ) == (
        first.task_run_id,
        first.stage,
        first.role,
        first.resource_ref,
    )
    assert request_id == correction.model_request_id


def test_d4_structural_then_semantic_rejection_shares_one_correction_and_reports_its_id(
    d4_single_scenario, monkeypatch
):
    app = d4_single_scenario[0]
    request, calls, token, refresh, validate = outer_proposal_call(
        d4_single_scenario,
        monkeypatch,
        [{"unexpected": "shape"}, outer_proposal(1)],
    )
    with pytest.raises(D3ProposalRejected) as raised:
        asyncio.run(
            app.outer.propose(request, token, refresh=refresh, validate=validate)
        )
    assert len(calls) == 2
    assert calls[1].correction_of_model_request_id == calls[0].model_request_id
    assert raised.value.code == "d3_attachment_not_authorized"
    assert raised.value.model_request_id == calls[1].model_request_id


def test_d4_action_semantic_rejection_uses_one_correction_then_executes_tool(
    d4_scenario, monkeypatch
):
    app, sandbox, _, inner, review, _, _ = d4_scenario
    correction_dispatch_sequences = {}
    invoke = inner.invoke

    def observe_correction_dispatch(request, timeout_seconds, token):
        if request.correction_of_model_request_id is not None:
            correction_dispatch_sequences[request.model_request_id] = (
                app.state.latest_checkpoint.sequence
            )
        return invoke(request, timeout_seconds, token)

    monkeypatch.setattr(inner, "invoke", observe_correction_dispatch)

    def modify(request, value):
        if (
            request.stage == "inner_size"
            and request.correction_of_model_request_id is None
        ):
            value["action"]["script"] = "echo untrusted"
        return value

    inner.modify_action = modify
    result = run(d4_scenario)

    assert result.stop_reason == "turn_completed_sandbox_retained", result
    size_calls = [request for request in inner.calls if request.stage == "inner_size"]
    assert len(size_calls) == 2
    original, correction = size_calls
    assert correction.correction_of_model_request_id == original.model_request_id
    assert correction.rejection_code == "d3_fixed_probe_rejected"
    assert (
        correction.base_checkpoint_sequence
        == correction_dispatch_sequences[correction.model_request_id]
    )
    assert isinstance(correction.context, D3ActionContext)
    assert isinstance(correction.context.turn_binding, D3TurnBinding)
    assert correction.context.turn_binding.turn_id == app.state.turns[0].turn_id
    assert app.model_evidence.read_rejection(original.model_request_id).code == (
        "d3_fixed_probe_rejected"
    )
    _assert_prior_invocations_were_not_repeated(inner, _invocation_ids(inner))
    assert len(sandbox.command_calls) == 2
    assert len({request.request_id for request in sandbox.command_calls}) == len(
        sandbox.command_calls
    )
    assert not any(request.review_scope == "action" for request in review.calls)
    usage = app.store.d4_budget_usage(result.task_run_id)
    assert dict(usage.category_counts)["action"] == sum(
        record.start.category == "inner_model"
        for record in app.store.list_d3_operations(result.task_run_id)
    )
    assert dict(usage.category_counts)["refinement"] == 0


def test_d4_second_action_semantic_rejection_pauses_without_review_or_tool(
    d4_scenario,
):
    _, sandbox, _, inner, review, _, _ = d4_scenario

    def always_modify(request, value):
        if request.stage == "inner_size":
            value["action"]["script"] = "echo untrusted"
        return value

    inner.modify_action = always_modify
    result = run(d4_scenario)

    size_calls = [request for request in inner.calls if request.stage == "inner_size"]
    assert result.recovery_required
    assert len(size_calls) == 2
    assert (
        size_calls[1].correction_of_model_request_id == size_calls[0].model_request_id
    )
    assert not any(request.review_scope == "action" for request in review.calls)
    assert sandbox.command_calls == []


def test_d4_recovery_reuses_corrected_action_for_saved_tool_completion(
    d4_scenario, monkeypatch
):
    app, sandbox, _, inner, _, _, _ = d4_scenario

    def modify(request, value):
        if (
            request.stage == "inner_size"
            and request.correction_of_model_request_id is None
        ):
            value["action"]["script"] = "echo untrusted"
        return value

    inner.modify_action = modify
    complete = app.store.complete_d3_operation

    def interrupt_tool_completion(completion):
        operation = app.store.load_d3_operation(completion.operation_id)
        if operation is not None and operation.start.category == "tool_dispatch":
            raise ProcessLoss()
        return complete(completion)

    monkeypatch.setattr(app.store, "complete_d3_operation", interrupt_tool_completion)
    with pytest.raises(ProcessLoss):
        run(d4_scenario)

    task_run_id = app.state.task_run.task_run_id
    size_request_ids = tuple(
        request.model_request_id
        for request in inner.calls
        if request.stage == "inner_size"
    )
    assert len(size_request_ids) == 2
    tool_operation_ids = _d4_operation_ids(app, task_run_id, "tool_dispatch")
    assert len(tool_operation_ids) == 1
    tool_id = tool_operation_ids[0]
    assert app.store.load_d4_operation(tool_id).completion is None
    assert app.commands.read_completed(tool_id) is not None
    command_ids = tuple(request.request_id for request in sandbox.command_calls)
    assert command_ids == (tool_id,)

    app = reopen_scenario(d4_scenario)
    task_run_id = d4_scenario[0].state.task_run.task_run_id
    result = asyncio.run(app.resume(task_run_id, d4_scenario[6]))

    assert result.stop_reason == "turn_completed_sandbox_retained", result
    _assert_prior_invocations_were_not_repeated(inner, size_request_ids)
    assert (
        tuple(
            request.model_request_id
            for request in inner.calls
            if request.stage == "inner_size"
        )
        == size_request_ids
    )
    completed_tool = app.store.load_d4_operation(tool_id)
    assert completed_tool is not None and completed_tool.completion is not None
    assert completed_tool.application_ref == tool_id
    replayed_command_ids = tuple(
        request.request_id for request in sandbox.command_calls
    )
    assert replayed_command_ids[0] == tool_id
    assert replayed_command_ids.count(tool_id) == 1
    assert len(replayed_command_ids) == len(set(replayed_command_ids)) == 2


@pytest.mark.parametrize("stage", ["inner_size", "inner_complete"])
@pytest.mark.parametrize("outcome", ["model_error", "stale"])
def test_d4_known_model_error_or_stale_replays_without_review_or_new_calls(
    d4_scenario, monkeypatch, stage, outcome
):
    app, sandbox, outer, inner, review, _, _ = d4_scenario
    if outcome == "model_error":
        invoke = inner.invoke

        def return_model_error(request, timeout_seconds, token):
            if request.stage == stage:
                inner.calls.append(request)
                return ModelError(
                    ModelErrorKind.PROVIDER_FAILURE, "fake_provider_error"
                )
            return invoke(request, timeout_seconds, token)

        monkeypatch.setattr(inner, "invoke", return_model_error)
    else:
        d4_scenario[6] = StaleOnceToken()
        invoke = app.inner._invoke

        async def return_stale(request, timeout_seconds, token):
            result = await invoke(request, timeout_seconds, token)
            if request.stage == stage:
                token.stale_next = True
            return result

        monkeypatch.setattr(app.inner, "_invoke", return_stale)

    first = run(d4_scenario)
    assert first.recovery_required
    assert not any(request.review_scope == "action" for request in review.calls)
    outer_ids = _invocation_ids(outer)
    inner_ids = _invocation_ids(inner)
    review_ids = _invocation_ids(review)
    command_ids = tuple(request.request_id for request in sandbox.command_calls)
    first_reason = first.stop_reason

    replay = run(d4_scenario)

    assert replay.recovery_required and replay.stop_reason == first_reason
    _assert_prior_invocations_were_not_repeated(outer, outer_ids)
    _assert_prior_invocations_were_not_repeated(inner, inner_ids)
    _assert_prior_invocations_were_not_repeated(review, review_ids)
    assert tuple(request.request_id for request in sandbox.command_calls) == command_ids
    assert not any(request.review_scope == "action" for request in review.calls)


def test_d4_unknown_correction_is_not_redispatched_after_recovery(
    d4_single_scenario, monkeypatch
):
    app = d4_single_scenario[0]
    request, calls, token, refresh, validate = outer_proposal_call(
        d4_single_scenario,
        monkeypatch,
        [outer_proposal(1), TimeoutError("correction result lost")],
    )
    with pytest.raises(D3RecoveryRequired, match="d3_model_call_outcome_unknown"):
        asyncio.run(
            app.outer.propose(request, token, refresh=refresh, validate=validate)
        )
    assert len(calls) == 2
    correction_id = calls[1].model_request_id
    correction_record = app.store.load_d3_operation(correction_id)
    assert correction_record is not None and correction_record.completion is None

    with pytest.raises(D3RecoveryRequired):
        asyncio.run(
            app.outer.propose(request, token, refresh=refresh, validate=validate)
        )
    assert len(calls) == 2
    assert app.store.load_d3_operation(correction_id).completion is None


def test_d4_saved_model_evidence_repairs_completion_without_second_call(
    d4_scenario, monkeypatch
):
    app, _, outer, _, _, _, _ = d4_scenario
    operation_id = app._id("outer/turn/1")
    complete = app.store.complete_d3_operation

    def interrupted(completion):
        if completion.operation_id == operation_id:
            raise ProcessLoss()
        return complete(completion)

    monkeypatch.setattr(app.store, "complete_d3_operation", interrupted)
    with pytest.raises(ProcessLoss):
        run(d4_scenario)
    calls = len(outer.calls)
    evidence = app.model_evidence.read(operation_id)
    assert evidence is not None and evidence.outcome == "accepted"
    assert app.store.load_d4_operation(operation_id).completion is None

    reopen_scenario(d4_scenario)
    result = run(d4_scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert len(outer.calls) == calls
    record = d4_scenario[0].store.load_d4_operation(operation_id)
    assert record.completion is not None
    assert record.application_ref is not None


def test_d4_known_proposal_is_applied_after_interrupted_control_submit(
    d4_scenario, monkeypatch
):
    app, _, outer, inner, _, _, _ = d4_scenario
    register_plan = app.control.register_plan
    interrupted_once = False

    def interrupted(*args, **kwargs):
        nonlocal interrupted_once
        if not interrupted_once:
            interrupted_once = True
            raise ProcessLoss()
        return register_plan(*args, **kwargs)

    monkeypatch.setattr(app.control, "register_plan", interrupted)
    with pytest.raises(ProcessLoss):
        run(d4_scenario)
    outer_request_ids = _invocation_ids(outer)
    plan_request_ids = _invocation_ids(inner)
    task_run_id = app.state.task_run.task_run_id
    proposal_operation = next(
        record
        for record in app.store.list_d4_operations(task_run_id)
        if record.start.category == "action"
    )
    assert proposal_operation.completion is not None
    assert proposal_operation.application_ref is None

    reopen_scenario(d4_scenario)
    result = run(d4_scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    _assert_prior_invocations_were_not_repeated(outer, outer_request_ids)
    _assert_prior_invocations_were_not_repeated(inner, plan_request_ids)
    assert [request.stage for request in inner.calls] == [
        "inner_plan",
        "inner_size",
        "inner_sha256",
        "inner_complete",
    ]
    applied = d4_scenario[0].store.load_d4_operation(
        proposal_operation.start.operation_id
    )
    assert applied is not None and applied.application_ref is not None


def test_d4_control_commit_reload_repairs_model_application_without_reinvoke(
    d4_scenario, monkeypatch
):
    app, _, outer, inner, _, _, _ = d4_scenario
    commit = app.controller.commit_d3_transition

    def commit_then_interrupt(operation, *args, **kwargs):
        result = commit(operation, *args, **kwargs)
        if operation == "register_d3_plan":
            raise ProcessLoss()
        return result

    monkeypatch.setattr(app.controller, "commit_d3_transition", commit_then_interrupt)
    with pytest.raises(ProcessLoss):
        run(d4_scenario)
    proposal_operation = next(
        record
        for record in app.store.list_d4_operations(app.state.task_run.task_run_id)
        if record.start.category == "action"
    )
    assert proposal_operation.completion is not None
    assert proposal_operation.application_ref is None
    outer_request_ids = _invocation_ids(outer)
    inner_request_ids = _invocation_ids(inner)

    reopen_scenario(d4_scenario)
    result = run(d4_scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    _assert_prior_invocations_were_not_repeated(outer, outer_request_ids)
    _assert_prior_invocations_were_not_repeated(inner, inner_request_ids)
    assert [request.stage for request in inner.calls] == [
        "inner_plan",
        "inner_size",
        "inner_sha256",
        "inner_complete",
    ]
    applied = d4_scenario[0].store.load_d4_operation(
        proposal_operation.start.operation_id
    )
    assert applied is not None and applied.application_ref is not None


def test_d4_artifact_copy_receipt_replays_without_second_copy(d4_scenario, monkeypatch):
    app, sandbox, outer, inner, _, _, _ = d4_scenario
    prepare = app.workspace.prepare_artifact

    async def interrupted(*args):
        await prepare(*args)
        raise ProcessLoss()

    monkeypatch.setattr(app.workspace, "prepare_artifact", interrupted)
    with pytest.raises(ProcessLoss):
        run(d4_scenario)
    assert sandbox.copy_calls == 1
    task_run_id = app.state.task_run.task_run_id
    outer_request_ids = _invocation_ids(outer)
    inner_request_ids = _invocation_ids(inner)
    copy_operation_ids = _d4_operation_ids(app, task_run_id, "copy_preparation")
    command_ids = tuple(request.request_id for request in sandbox.command_calls)

    reopen_scenario(d4_scenario)
    result = run(d4_scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert sandbox.copy_calls == 1
    _assert_prior_invocations_were_not_repeated(outer, outer_request_ids)
    _assert_prior_invocations_were_not_repeated(inner, inner_request_ids)
    assert [request.stage for request in inner.calls] == [
        "inner_plan",
        "inner_size",
        "inner_sha256",
        "inner_complete",
    ]
    assert (
        _d4_operation_ids(d4_scenario[0], task_run_id, "copy_preparation")
        == copy_operation_ids
    )
    replayed_command_ids = tuple(
        request.request_id for request in sandbox.command_calls
    )
    assert replayed_command_ids[: len(command_ids)] == command_ids
    assert len(replayed_command_ids) == len(set(replayed_command_ids))
    assert len(replayed_command_ids) == 2


def test_d4_saved_proof_replays_without_second_tool_or_observation(
    d4_scenario, monkeypatch
):
    app, sandbox, outer, inner, _, _, _ = d4_scenario
    issue = app.proofs.issue

    def interrupted(*args):
        issue(*args)
        raise ProcessLoss()

    monkeypatch.setattr(app.proofs, "issue", interrupted)
    with pytest.raises(ProcessLoss):
        run(d4_scenario)
    task_run_id = app.state.task_run.task_run_id
    outer_request_ids = _invocation_ids(outer)
    inner_request_ids = _invocation_ids(inner)
    inner_stages = tuple(request.stage for request in inner.calls)
    command_ids = tuple(request.request_id for request in sandbox.command_calls)
    tool_operation_ids = _d4_operation_ids(app, task_run_id, "tool_dispatch")
    observation_operation_ids = _d4_operation_ids(app, task_run_id, "sha_observation")
    digest_calls = sandbox.digest_calls
    reopen_scenario(d4_scenario)
    result = run(d4_scenario)
    assert result.stop_reason == "turn_completed_sandbox_retained", result
    _assert_prior_invocations_were_not_repeated(outer, outer_request_ids)
    _assert_prior_invocations_were_not_repeated(inner, inner_request_ids)
    assert tuple(request.stage for request in inner.calls) == (
        *inner_stages,
        "inner_complete",
    )
    replayed_command_ids = tuple(
        request.request_id for request in sandbox.command_calls
    )
    assert replayed_command_ids == command_ids
    assert len(replayed_command_ids) == len(set(replayed_command_ids))
    assert sandbox.digest_calls == digest_calls
    assert (
        _d4_operation_ids(d4_scenario[0], task_run_id, "tool_dispatch")
        == tool_operation_ids
    )
    assert (
        _d4_operation_ids(d4_scenario[0], task_run_id, "sha_observation")
        == observation_operation_ids
    )


def test_d4_review_file_completion_repairs_sqlite_without_second_provider_call(
    d4_scenario, monkeypatch
):
    app, _, _, _, review, _, _ = d4_scenario
    complete = app.store.complete_d4_operation

    def interrupt_after_turn_end_evidence(completion):
        operation = app.store.load_d4_operation(completion.operation_id)
        if operation is not None and operation.start.category == "review":
            call = next(
                item
                for root in app.review_ledger.list_roots(operation.start.task_run_id)
                for item in root["calls"]
                if item["review_id"] == completion.operation_id
            )
            if call["request"]["review_scope"] == "turn_end":
                raise ProcessLoss()
        return complete(completion)

    with monkeypatch.context() as patch:
        patch.setattr(
            app.store, "complete_d4_operation", interrupt_after_turn_end_evidence
        )
        with pytest.raises(ProcessLoss):
            run(d4_scenario)

    task_run_id = app.state.task_run.task_run_id
    end_request = next(
        request for request in review.calls if request.review_scope == "turn_end"
    )
    pending = app.store.load_d4_operation(end_request.review_id)
    assert pending is not None and pending.completion is None
    assert app.review_ledger.review_call_count(task_run_id) == 2
    assert dict(app.store.d4_budget_usage(task_run_id).category_counts)["review"] == 2
    prior_review_ids = tuple(request.review_id for request in review.calls)

    app = reopen_scenario(d4_scenario)
    result = run(d4_scenario)

    assert result.stop_reason == "turn_completed_sandbox_retained", result
    assert tuple(request.review_id for request in review.calls) == prior_review_ids
    assert app.review_ledger.review_call_count(task_run_id) == 2
    usage = app.store.d4_budget_usage(task_run_id)
    assert dict(usage.category_counts)["review"] == 2
    repaired = app.store.load_d4_operation(end_request.review_id)
    assert repaired is not None and repaired.completion is not None
    assert repaired.application_ref is not None


def test_d4_pending_review_reopens_before_admission_with_json_source_versions(
    d4_scenario, monkeypatch
):
    app, _, _, _, review, _, _ = d4_scenario
    task_run_id = app.state.task_run.task_run_id
    begin = app.store.begin_d4_operation

    def interrupt_before_review_admission(start):
        if start.category == "review":
            raise ProcessLoss()
        return begin(start)

    with monkeypatch.context() as patch:
        patch.setattr(
            app.store, "begin_d4_operation", interrupt_before_review_admission
        )
        with pytest.raises(ProcessLoss):
            run(d4_scenario)

    assert review.calls == []
    assert _d4_operation_ids(app, task_run_id, "review") == ()
    pending_requests = [
        request
        for root in app.review_ledger.list_roots(task_run_id)
        for request in root["d4_pending_requests"].values()
    ]
    assert len(pending_requests) == 1
    pending = pending_requests[0]
    assert isinstance(pending["source_versions"], list)
    assert pending["source_versions"]
    assert app.review_ledger.review_call_count(task_run_id) == 0

    app = reopen_scenario(d4_scenario)
    request = D2ReviewRequest(
        **{**pending, "source_versions": tuple(pending["source_versions"])}
    )
    app.review_ledger.begin_attempt(request)
    assert app.review_ledger.review_call_count(task_run_id) == 1
    assert dict(app.store.d4_budget_usage(task_run_id).category_counts)["review"] == 1

    response = review.invoke(request)
    app.review_ledger.finish_attempt(
        request,
        outcome="accepted",
        response=response,
        provider_request_id=None,
    )
    usage = app.store.d4_budget_usage(task_run_id)
    app.review_ledger.reconcile_known_completions(task_run_id)

    assert tuple(request.review_id for request in review.calls) == (
        pending["review_id"],
    )
    assert app.review_ledger.review_call_count(task_run_id) == 1
    assert app.store.d4_budget_usage(task_run_id) == usage
    record = app.store.load_d4_operation(pending["review_id"])
    assert record is not None and record.completion is not None
    assert record.completion.result_kind == "completed"
    assert all(
        not root["d4_pending_requests"]
        for root in app.review_ledger.list_roots(task_run_id)
    )


@pytest.mark.parametrize("interruption", ["before_file_call", "unknown_dispatch"])
def test_d4_review_admission_recovery_and_unknown_dispatch_stay_charged(
    d4_scenario, monkeypatch, interruption
):
    app, _, _, _, review, _, _ = d4_scenario
    task_run_id = app.state.task_run.task_run_id

    if interruption == "before_file_call":
        write = app.review_ledger._write
        writes = 0

        def interrupt_before_file_call(data):
            nonlocal writes
            writes += 1
            if writes == 3:
                raise ProcessLoss()
            write(data)

        with monkeypatch.context() as patch:
            patch.setattr(app.review_ledger, "_write", interrupt_before_file_call)
            with pytest.raises(ProcessLoss):
                run(d4_scenario)

        assert review.calls == []
        admitted = [
            record
            for record in app.store.list_d4_operations(task_run_id)
            if record.start.category == "review"
        ]
        assert len(admitted) == 1 and admitted[0].completion is None

        app = reopen_scenario(d4_scenario)
        result = asyncio.run(app.resume(task_run_id, d4_scenario[6]))

        assert result.stop_reason == "turn_completed_sandbox_retained", result
        repaired = app.store.load_d4_operation(admitted[0].start.operation_id)
        assert repaired is not None and repaired.completion is not None
        assert repaired.completion.result_kind == "rejected"
        assert len(review.calls) == 2
        assert app.review_ledger.review_call_count(task_run_id) == 3
        assert (
            dict(app.store.d4_budget_usage(task_run_id).category_counts)["review"] == 3
        )
        return

    def lose_review_result(request):
        review.calls.append(request)
        raise TimeoutError("fake review result lost")

    with monkeypatch.context() as patch:
        patch.setattr(review, "invoke", lose_review_result)
        first = run(d4_scenario)

    assert first.recovery_required
    unknown = [
        record
        for record in app.store.list_d4_operations(task_run_id)
        if record.start.category == "review"
    ]
    assert len(unknown) == 1 and unknown[0].completion is None
    assert app.review_ledger.review_call_count(task_run_id) == 1
    assert dict(app.store.d4_budget_usage(task_run_id).category_counts)["review"] == 1

    app = reopen_scenario(d4_scenario)
    replay = asyncio.run(app.resume(task_run_id, d4_scenario[6]))

    assert replay.recovery_required
    assert len(review.calls) == 1
    assert app.review_ledger.review_call_count(task_run_id) == 1
    assert dict(app.store.d4_budget_usage(task_run_id).category_counts)["review"] == 1
    assert app.store.load_d4_operation(unknown[0].start.operation_id).completion is None


def test_d4_cancel_with_unknown_operation_keeps_run_open_and_skips_cleanup(
    d4_scenario,
):
    app, sandbox, outer, _, _, _, _token = d4_scenario
    outer.fail_stage = "outer_turn"
    first = run(d4_scenario)
    assert first.recovery_required
    assert app.state.task_run.status.value == "running"
    assert not sandbox.destroyed

    cancelled = app.cancel(first.task_run_id)
    assert cancelled.recovery_required
    assert app.state.task_run.status.value == "running"
    assert not sandbox.destroyed
    assert len(outer.calls) == 1
    assert app.store.d4_budget_usage(first.task_run_id).total_used >= 1


def test_d4_cancel_intent_survives_restart_while_unknown_operation_stays_unreplayed(
    d4_scenario,
):
    app, sandbox, outer, _, _, _, _ = d4_scenario
    outer.fail_stage = "outer_turn"
    first = run(d4_scenario)
    assert first.recovery_required and first.task_run_status == "running"
    prior_outer_ids = _invocation_ids(outer)
    prior_operation_ids = tuple(
        record.start.operation_id
        for record in app.store.list_d4_operations(first.task_run_id)
    )

    cancelled = app.cancel(first.task_run_id)
    assert cancelled.recovery_required
    assert cancelled.task_run_status == "running"
    assert cancelled.stop_reason == "d4_operation_outcome_unknown"
    cancellation_events = [
        event
        for event in app.store.list_d4_audit_events(first.task_run_id)
        if event.category == "cancellation_requested"
    ]
    assert len(cancellation_events) == 1
    assert cancellation_events[0].outcome == "known"

    outer.fail_stage = None
    app = reopen_scenario(d4_scenario)
    replay = run(d4_scenario)
    assert replay.recovery_required and replay.task_run_status == "running"
    assert replay.stop_reason == "d4_operation_outcome_unknown"
    _assert_prior_invocations_were_not_repeated(outer, prior_outer_ids)
    assert (
        tuple(
            record.start.operation_id
            for record in app.store.list_d4_operations(first.task_run_id)
        )
        == prior_operation_ids
    )
    assert (
        len(
            [
                event
                for event in app.store.list_d4_audit_events(first.task_run_id)
                if event.category == "cancellation_requested"
            ]
        )
        == 1
    )
    assert not sandbox.destroyed


def test_d4_deadline_with_unknown_operation_keeps_run_open_and_skips_cleanup(
    d4_scenario,
):
    app, sandbox, outer, _, _, clock, _token = d4_scenario
    outer.fail_stage = "outer_turn"
    first = run(d4_scenario)
    assert first.recovery_required
    deadline = app.state.task_run.deadline_at
    clock.now = deadline

    stopped = run(d4_scenario)
    assert stopped.recovery_required
    assert app.state.task_run.status.value == "running"
    assert not sandbox.destroyed
    assert len(outer.calls) == 1


@pytest.mark.parametrize("stage", ["outer_turn", "inner_plan"])
def test_unknown_model_operation_stays_charged_and_is_never_reinvoked(
    d4_scenario, stage
):
    _, _, outer, inner, _, _, _ = d4_scenario
    model = outer if stage == "outer_turn" else inner
    model.fail_stage = stage
    first = run(d4_scenario)
    assert first.recovery_required and first.task_run_status == "running"
    calls = len(model.calls)
    app = d4_scenario[0]
    unknown = [
        record
        for record in app.store.list_d4_operations(first.task_run_id)
        if record.completion is None
    ]
    assert len(unknown) == 1
    operation_count = len(app.store.list_d4_operations(first.task_run_id))
    assert app.store.d4_budget_usage(first.task_run_id).total_used >= 1

    model.fail_stage = None
    reopen_scenario(d4_scenario)
    replay = run(d4_scenario)
    assert replay.recovery_required and replay.task_run_status == "running"
    assert len(model.calls) == calls
    assert (
        len(d4_scenario[0].store.list_d4_operations(first.task_run_id))
        == operation_count
    )
    with pytest.raises(D4RecoveryRequired):
        d4_scenario[0].store.begin_d4_operation(unknown[0].start)
