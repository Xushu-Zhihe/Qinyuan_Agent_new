"""Internal D2 review timing, trusted continuation, and restart safety."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from test_chain_single import Clock, Model, Sandbox, Token

from agent_adapters.dual_llm_application import D2Application
from agent_core.contracts import TaskRequest
from agent_core.review_contracts import D2ModelFailure
from agent_core.turn_contracts import D1Authorization, D1RoleConfig, D1RunConfig
from agent_core.turn_modeling import stable_id


class ProcessLoss(BaseException):
    """Interrupt at a durable boundary without persisting a synthetic failure."""


class InnerModel(Model):
    def __init__(self, role_config):
        super().__init__(role_config)
        self.request_review = set()

    def invoke(self, request, timeout_seconds, token):
        value = super().invoke(request, timeout_seconds, token)
        if isinstance(value, dict) and value.get("kind") in {
            "probe",
            "complete_intent",
        }:
            return {
                "action": value,
                "review_requested": request.stage in self.request_review,
            }
        return value


class ReviewModel:
    def __init__(self, role_config):
        self.role_config = role_config
        self.calls = []
        self.failures = {}
        self.constraints = {}
        self.on_call = lambda request: None

    def invoke(self, request):
        self.calls.append(request)
        self.on_call(request)
        failure = self.failures.get(request.review_scope)
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            return failure
        constraints = self.constraints.get(request.review_scope)
        return {
            "decision": (
                "acknowledge_turn_end"
                if request.review_scope == "turn_end"
                else "continue_current_intent"
            ),
            "rationale": "continue according to the saved trusted evidence",
            "source_ids": list(request.source_versions),
            "constraints": constraints(request) if constraints else [],
        }


@pytest.fixture
def scenario(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.txt").write_bytes(b"D2 local attachment")
    clock, token, sandbox = Clock(), Token(), Sandbox()
    config = D1RunConfig(
        D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1"),
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D1Authorization(str(uuid4())),
    )
    outer = Model(config.outer_role)
    inner = InnerModel(config.inner_role)
    review = ReviewModel(config.outer_role)
    case = [None, sandbox, outer, inner, token, clock, review]
    case[0] = _application(tmp_path / "run", config, case)
    case[0].start(
        TaskRequest.from_dict(
            {
                "goal": "verify the local attachment copy",
                "target": {
                    "attachments": [{"kind": "local_path", "path": "sample.txt"}]
                },
            }
        ),
        source,
    )
    try:
        yield case
    finally:
        app = case[0]
        if app.controller.control_state.task_run.status.value == "running":
            app.controller.cancel(str(uuid4()))
        app.controller.cleanup_sandbox()
        assert sandbox.destroyed, "cleanup is verified separately from review success"
        app.close()


def _application(directory, config, case):
    _, sandbox, outer, inner, _, clock, review = case
    return D2Application(
        directory,
        config=config,
        sandbox=sandbox,
        import_sandbox=sandbox,
        copy_sandbox=sandbox,
        command_sandbox=sandbox,
        digest_sandbox=sandbox,
        outer_model=outer,
        inner_model=inner,
        review_model=review,
        clock=clock,
    )


def run(case):
    return asyncio.run(case[0].run(case[4]))


def reopen(case):
    original = case[0]
    task_id = original.controller.control_state.task_run.task_run_id
    original.store.close()
    case[0] = _application(original.directory, original.config, case)
    case[0].controller.attach(task_id)
    return case[0]


def counts(case):
    _, sandbox, outer, inner, _, _, review = case
    return (
        sandbox.import_calls,
        sandbox.copy_calls,
        len(sandbox.calls),
        sandbox.digest_calls,
        len(outer.calls),
        len(inner.calls),
        len(review.calls),
    )


def test_review_start_action_end_surround_trusted_commits(scenario):
    app, sandbox, _, inner, _, _, review = scenario
    inner.request_review.add("probe")
    observed = []

    def observe(request):
        state = app.controller.control_state
        intent = next(
            item for item in state.intents if item.intent_id == request.intent_id
        )
        turn = next(item for item in state.turns if item.turn_id == request.turn_id)
        if request.review_scope == "turn_start":
            assert intent.status.value == "running" and turn.status.value == "running"
            assert not inner.calls and not sandbox.calls
        elif request.review_scope == "action":
            assert intent.status.value == "running"
            assert len(sandbox.calls) == sandbox.digest_calls == 1
            assert state.facts and all(
                item.status.value == "verified" for item in state.facts
            )
            assert request.action_candidate_id is not None
        else:
            assert intent.status.value == "succeeded"
            assert turn.status.value == "completed"
            assert request.action_candidate_id is None
        observed.append(request.review_scope)

    review.on_call = observe
    result = run(scenario)
    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert observed == ["turn_start", "action", "turn_end"]
    assert result.intent_status == "succeeded" and result.turn_status == "completed"
    assert (
        result.task_run_status == "running" and result.overall_goal == "not_evaluated"
    )
    assert result.sandbox_status == "retained" and len(result.verified_fact_ids) == 1
    first_counts = counts(scenario)
    review.on_call = lambda request: pytest.fail("completed reviews must be reused")
    reopen(scenario)
    replay = run(scenario)
    assert replay.rule_outcome == "satisfied", replay
    assert counts(scenario) == first_counts


def test_successful_action_without_request_only_reviews_turn_boundaries(scenario):
    result = run(scenario)
    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert [request.review_scope for request in scenario[6].calls] == [
        "turn_start",
        "turn_end",
    ]
    assert scenario[1].digest_calls == len(scenario[1].calls) == 1


@pytest.mark.parametrize("scope", ["turn_start", "action", "turn_end"])
@pytest.mark.parametrize("failure", ["provider_failure", "unknown"])
def test_review_failure_is_not_redispatched_after_reopen(scenario, scope, failure):
    _, sandbox, _, inner, _, _, review = scenario
    inner.request_review.add("probe")
    review.failures[scope] = (
        D2ModelFailure("provider_failure", "provider unavailable", "provider-id")
        if failure == "provider_failure"
        else TimeoutError("no reliable provider completion")
    )
    first = run(scenario)
    assert first.task_run_status == "running" and first.sandbox_status == "retained"
    assert (
        len([request for request in review.calls if request.review_scope == scope]) == 1
    ), first
    assert first.turn_status == ("completed" if scope == "turn_end" else "running")
    if scope == "turn_start":
        assert not sandbox.calls and not inner.calls
    else:
        assert len(sandbox.calls) == sandbox.digest_calls == 1
    first_counts = counts(scenario)
    review.failures.clear()
    reopen(scenario)
    replay = run(scenario)
    assert counts(scenario) == first_counts
    assert replay.turn_status == first.turn_status
    assert replay.stop_reason == first.stop_reason


def test_complete_candidate_rechecks_constraint_added_by_its_review(scenario):
    _, sandbox, _, inner, _, _, review = scenario
    inner.request_review.add("complete")
    review.constraints["action"] = lambda request: [
        {
            "kind": "allowed_action_kinds",
            "scope_kind": "intent",
            "scope_id": request.intent_id,
            "allowed_action_kinds": ["probe"],
        }
    ]
    first = run(scenario)
    assert first.intent_status == "running" and first.turn_status == "running"
    assert first.verified_fact_ids and len(sandbox.calls) == sandbox.digest_calls == 1
    assert "constraint" in first.stop_reason
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "action",
        "action",
    ]
    first_counts = counts(scenario)
    reopen(scenario)
    replay = run(scenario)
    assert replay.intent_status == "running" and replay.turn_status == "running"
    assert counts(scenario) == first_counts


@pytest.mark.parametrize("status", ["missing", "unsafe"])
def test_not_satisfied_triggers_review_without_repeating_tool(scenario, status):
    _, sandbox, _, _, _, _, review = scenario
    sandbox.observe_status = status
    first = run(scenario)
    assert first.rule_outcome == "not_satisfied" and not first.recovery_required, (
        first.stop_reason,
        first,
    )
    assert first.intent_status == "running" and first.turn_status == "running"
    assert not first.verified_fact_ids
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "action",
    ]
    assert sandbox.digest_calls == len(sandbox.calls) == 1
    context = json.loads(review.calls[1].context_summary)
    observations = [
        item
        for item in context["source_summaries"].values()
        if item["kind"] == "digest_observation"
    ]
    assert observations[0]["status"] == status
    first_counts = counts(scenario)
    reopen(scenario)
    replay = run(scenario)
    assert replay.rule_outcome == "not_satisfied", replay
    assert counts(scenario) == first_counts


def test_structure_correction_keeps_candidate_review_root(scenario):
    app, _, _, inner, _, _, review = scenario
    inner.structural.add("probe")
    inner.request_review.add("probe")
    result = run(scenario)
    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    probe_calls = [request for request in inner.calls if request.stage == "probe"]
    assert len(probe_calls) == 2
    assert (
        probe_calls[1].correction_of_model_request_id == probe_calls[0].model_request_id
    )
    action_reviews = [
        request for request in review.calls if request.review_scope == "action"
    ]
    assert len(action_reviews) == 1
    assert action_reviews[0].action_candidate_id == probe_calls[0].model_request_id
    assert action_reviews[0].action_candidate_id != probe_calls[1].model_request_id
    assert action_reviews[0].action_candidate_id is not None
    records = app.store.list_d1_operations(result.task_run_id)
    assert (
        len(
            [
                record
                for record in records
                if record.start.category == "inner_probe_model"
            ]
        )
        == 2
    )


def test_turn_start_timeout_constraint_rejects_unmodified_probe(scenario):
    _, sandbox, _, _, _, _, review = scenario
    review.constraints["turn_start"] = lambda request: [
        {
            "kind": "max_action_timeout_seconds",
            "scope_kind": "turn",
            "scope_id": request.turn_id,
            "max_action_timeout_seconds": 30,
        }
    ]
    result = run(scenario)
    assert result.intent_status == "running" and result.turn_status == "running"
    assert not sandbox.calls and sandbox.digest_calls == 0
    assert "timeout" in result.stop_reason


def test_deterministic_action_rejection_is_reviewed_without_tool_dispatch(scenario):
    _, sandbox, _, inner, _, _, review = scenario
    review.constraints["turn_start"] = lambda request: [
        {
            "kind": "allowed_action_kinds",
            "scope_kind": "turn",
            "scope_id": request.turn_id,
            "allowed_action_kinds": ["complete_intent"],
        }
    ]

    result = run(scenario)

    assert result.stop_reason == "action_kind_rejected_by_review_constraint"
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "action",
    ]
    assert not sandbox.calls and sandbox.digest_calls == 0
    probe_request = inner.calls[0]
    rejection = scenario[0].model_evidence.read_application_rejection(
        probe_request.model_request_id
    )
    dispatch_rejection = scenario[0].model_evidence.read_dispatch_rejection(
        probe_request.model_request_id
    )
    assert rejection is None and dispatch_rejection is not None
    action_review = review.calls[1]
    assert action_review.action_candidate_id == probe_request.model_request_id
    root = scenario[0].review_ledger.read_root(action_review.review_root_id)
    assert root["trigger_reasons"][action_review.context_version] == [
        "trusted_action_rejection"
    ]


def test_saved_review_response_before_application_resumes_without_model(
    scenario, monkeypatch
):
    app, sandbox, _, _, _, _, review = scenario
    apply_review = app.controller.apply_d2_review

    def lose_process(application, *args, **kwargs):
        if application.review_scope == "turn_start" and not application.paused_gate:
            raise ProcessLoss("review response saved before trusted application")
        return apply_review(application, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(app.controller, "apply_d2_review", lose_process)
        with pytest.raises(ProcessLoss):
            run(scenario)
    assert [request.review_scope for request in review.calls] == ["turn_start"]
    assert not sandbox.calls
    saved_request = review.calls[0]
    root = app.review_ledger.read_root(saved_request.review_root_id)
    assert root["last_outcome"] == "accepted" and not root["applied"]
    reopen(scenario)
    result = run(scenario)
    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "turn_end",
    ]
    restored = scenario[0].review_ledger.read_root(saved_request.review_root_id)
    assert restored["attempts_used"] == 1
    assert restored["applied"][0]["review_id"] == saved_request.review_id
    assert len(sandbox.calls) == sandbox.digest_calls == 1


def test_saved_fact_before_action_review_resumes_original_request(
    scenario, monkeypatch
):
    app, sandbox, outer, inner, _, _, review = scenario
    inner.request_review.add("probe")
    runner_factory = app.runner

    def interrupted_runner():
        runner = runner_factory()
        runner._d2_action = lambda *args, **kwargs: (_ for _ in ()).throw(
            ProcessLoss("Fact committed before requested action review")
        )
        return runner

    with monkeypatch.context() as patch:
        patch.setattr(app, "runner", interrupted_runner)
        with pytest.raises(ProcessLoss):
            run(scenario)
    assert app.controller.control_state.facts
    assert len(sandbox.calls) == sandbox.digest_calls == 1
    assert len(outer.calls) == len(inner.calls) == 1
    assert [request.review_scope for request in review.calls] == ["turn_start"]
    candidate_id = inner.calls[0].model_request_id
    reopen(scenario)
    result = run(scenario)
    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert len(sandbox.calls) == sandbox.digest_calls == 1
    assert len(outer.calls) == 1 and len(inner.calls) == 2
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "action",
        "turn_end",
    ]
    assert review.calls[1].action_candidate_id == candidate_id


def test_completed_turn_before_tail_review_resumes_only_tail(scenario, monkeypatch):
    app, sandbox, outer, inner, _, _, review = scenario
    runner_factory = app.runner

    def interrupted_runner():
        runner = runner_factory()
        runner._d2_end = lambda *args, **kwargs: (_ for _ in ()).throw(
            ProcessLoss("Turn completed before read-only tail review")
        )
        return runner

    with monkeypatch.context() as patch:
        patch.setattr(app, "runner", interrupted_runner)
        with pytest.raises(ProcessLoss):
            run(scenario)
    assert app.controller.control_state.turns[0].status.value == "completed"
    assert app.controller.control_state.intents[0].status.value == "succeeded"
    assert [request.review_scope for request in review.calls] == ["turn_start"]
    before_counts = counts(scenario)
    reopen(scenario)
    result = run(scenario)
    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert counts(scenario)[:-1] == before_counts[:-1]
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "turn_end",
    ]
    assert len(outer.calls) == 1 and len(inner.calls) == 2
    assert len(sandbox.calls) == sandbox.digest_calls == 1


def test_controller_application_commit_before_review_ledger_recovers_idempotently(
    scenario, monkeypatch
):
    app, sandbox, _, _, _, _, review = scenario
    ledger = app.review_ledger
    record_application = ledger.record_application
    lost = False

    def lose_after_controller_commit(request, output, application_ref):
        nonlocal lost
        if request.review_scope == "turn_start" and not lost:
            lost = True
            raise ProcessLoss(
                "SQLite application committed before evidence-ledger append"
            )
        record_application(request, output, application_ref)

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "record_application", lose_after_controller_commit)
        with pytest.raises(ProcessLoss):
            run(scenario)
    assert app.store.list_d2_review_applications(
        app.controller.control_state.task_run.task_run_id
    )
    assert not sandbox.calls
    assert len(review.calls) == 1

    reopen(scenario)
    result = run(scenario)

    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert len(review.calls) == 2
    assert [request.review_scope for request in review.calls] == [
        "turn_start",
        "turn_end",
    ]
    assert len(sandbox.calls) == sandbox.digest_calls == 1


@pytest.mark.parametrize("scope", ["turn_start", "action", "turn_end"])
@pytest.mark.parametrize(
    "boundary", ["before_controller", "before_application_receipt"]
)
def test_known_review_application_exception_resumes_original_response_and_mutation(
    scenario, monkeypatch, scope, boundary
):
    app, sandbox, _, inner, _, _, review = scenario
    inner.request_review.add("probe")
    failed_requests = []
    failed_mutations = []

    if boundary == "before_controller":
        original = app.controller.apply_d2_review

        def fail(application, mutation_id, *args, **kwargs):
            if application.review_scope == scope and not application.paused_gate:
                failed_mutations.append(mutation_id)
                raise RuntimeError("known failure before Controller application")
            return original(application, mutation_id, *args, **kwargs)

        owner, method = app.controller, "apply_d2_review"
    else:
        original = app.review_ledger.record_application

        def fail(request, output, application_ref):
            if request.review_scope == scope:
                failed_requests.append(request)
                raise RuntimeError("Controller committed before application receipt")
            return original(request, output, application_ref)

        owner, method = app.review_ledger, "record_application"

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, fail)
        stopped = run(scenario)

    assert stopped.stop_reason == "review_application_uncommitted", stopped
    saved_request = next(item for item in review.calls if item.review_scope == scope)
    task_id = saved_request.task_run_id
    root = app.review_ledger.read_root(saved_request.review_root_id)
    assert root["last_outcome"] == "accepted" and not root["applied"]
    response = root["last_response"]
    mutation_id = stable_id(task_id, f"d2/apply/{saved_request.review_id}")
    first_commit = app.store.load_committed_mutation(task_id, mutation_id)
    if boundary == "before_controller":
        assert failed_mutations == [mutation_id] and first_commit is None
    else:
        assert failed_requests == [saved_request] and first_commit is not None
    before_counts = counts(scenario)
    assert (
        len(sandbox.calls)
        == sandbox.digest_calls
        == (0 if scope == "turn_start" else 1)
    )

    reopened = reopen(scenario)
    result = run(scenario)

    assert result.rule_outcome == "satisfied", (result.stop_reason, result)
    assert result.intent_status == "succeeded" and result.turn_status == "completed"
    assert len([item for item in review.calls if item.review_scope == scope]) == 1
    assert len(sandbox.calls) == sandbox.digest_calls == 1
    if scope != "turn_start":
        assert counts(scenario)[:4] == before_counts[:4]
    restored = reopened.review_ledger.read_root(saved_request.review_root_id)
    assert restored["attempts_used"] == 1 and restored["last_response"] == response
    assert restored["applied"][0]["review_id"] == saved_request.review_id
    final_commit = reopened.store.load_committed_mutation(task_id, mutation_id)
    assert final_commit is not None
    if first_commit is not None:
        assert final_commit == first_commit
    applications = reopened.store.list_d2_review_applications(task_id)
    assert (
        len(
            [item for item in applications if item.review_id == saved_request.review_id]
        )
        == 1
    )


@pytest.mark.parametrize(
    "reason", ["cancelled_before_tail_review", "deadline_before_tail_review"]
)
def test_completed_turn_lifecycle_stop_skips_tail_without_consuming_budget(
    scenario, monkeypatch, reason
):
    app, sandbox, _, _, token, clock, review = scenario
    complete_turn = app.controller.complete_verified_d1_turn

    def stop_after_turn_commit(*args, **kwargs):
        result = complete_turn(*args, **kwargs)
        if reason == "cancelled_before_tail_review":
            token.is_cancelled = True
        else:
            clock.now = app.controller.control_state.task_run.deadline_at
        return result

    with monkeypatch.context() as patch:
        patch.setattr(
            app.controller, "complete_verified_d1_turn", stop_after_turn_commit
        )
        result = run(scenario)

    assert result.turn_status == "completed" and result.intent_status == "succeeded"
    assert app.controller.control_state.turns[0].status.value == "completed"
    assert [item.review_scope for item in review.calls] == ["turn_start"]
    assert app.review_ledger.review_call_count(result.task_run_id) == 1
    saved = json.loads(app.review_ledger.path.read_text(encoding="utf-8"))
    tail = [item for item in saved["not_called"] if item["review_scope"] == "turn_end"]
    assert len(tail) == 1 and tail[0]["reason"] == reason
    assert len(sandbox.calls) == sandbox.digest_calls == 1


def test_same_run_cannot_dispatch_while_another_owner_holds_run_lock(scenario):
    from agent_adapters.run_files import run_lock

    with run_lock(scenario[0].directory):
        with pytest.raises(ValueError, match="task_run_busy"):
            run(scenario)
    assert counts(scenario) == (0, 0, 0, 0, 0, 0, 0)


def test_start_review_own_constraint_does_not_recall_on_resume(scenario, monkeypatch):
    app, _, _, _, _, _, review = scenario
    review.constraints["turn_start"] = lambda request: [
        {
            "kind": "max_action_timeout_seconds",
            "scope_kind": "turn",
            "scope_id": request.turn_id,
            "max_action_timeout_seconds": 300,
        }
    ]
    runner_factory = app.runner

    class InterruptedCopy:
        async def prepare(self, *args):
            raise ProcessLoss("review applied before copy")

    def interrupted_runner():
        runner = runner_factory()
        runner.copy_preparer = InterruptedCopy()
        return runner

    with monkeypatch.context() as patch:
        patch.setattr(app, "runner", interrupted_runner)
        with pytest.raises(ProcessLoss):
            run(scenario)
    original = review.calls[0]
    assert app.review_ledger.read_root(original.review_root_id)["applied"]
    assert app._constraint_version() == 1
    reopen(scenario)
    result = run(scenario)
    assert result.rule_outcome == "satisfied", result
    assert (
        len([call for call in review.calls if call.review_scope == "turn_start"]) == 1
    )
    root = scenario[0].review_ledger.read_root(original.review_root_id)
    assert root["attempts_used"] == root["current_generation"] == 1
