"""D5 Fake acceptance over real control commits and immutable evidence."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from test_chain_multi_turn import Clock, Model, ReviewModel, Sandbox, Token
from test_chain_operation import ProcessLoss, close_scenario

from agent_adapters.multi_turn_application import D3Application
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.redaction import D5SensitiveValue
from agent_core.turn_contracts import D1RoleConfig


class RefinementModel:
    def __init__(self, role_config):
        self.role_config = role_config
        self.calls = []
        self.value = "observed"
        self.fail = False
        self.modify = lambda request, candidate: candidate

    def invoke(self, request, timeout_seconds, token):
        self.calls.append(request)
        if self.fail:
            return ModelError(ModelErrorKind.REFUSED, "not retained")
        source = request.attempt.sources[0]
        return self.modify(
            request,
            {
                "summary_text": "Saved execution observations.",
                "claims": [
                    {
                        "subject_ref": source.subject_ref,
                        "observation_scope": source.observation_scope,
                        "claim_key": "observation",
                        "claim_value": self.value,
                        "source_refs": [source.object_id],
                        "conflicts_with": [],
                    }
                ],
            },
        )


def make_d5_scenario(directory: Path, *, sensitive_values=(), timeout=180.0):
    source = directory / "source"
    source.mkdir(parents=True)
    (source / "first.txt").write_bytes(b"D5 source")
    outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1")
    config = D3RunConfig(
        outer_role,
        D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
        D3Authorization(str(uuid4()), (0,)),
        outer_role.role_config_ref,
        d4_budget=D4BudgetPolicy(),
        d5_enabled=True,
        d5_sensitive_requirements=tuple(
            (f"secret_{index}", "v1") for index, _ in enumerate(sensitive_values)
        ),
    )
    sandbox, clock, token = Sandbox(), Clock(), Token()
    outer, inner, review = (
        Model(config.outer_role),
        Model(config.inner_role),
        ReviewModel(config.outer_role),
    )
    refine = RefinementModel(config.outer_role)
    inner.review_when = lambda request: request.stage in {"inner_size", "inner_sha256"}
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
        refinement_model=refine,
        sensitive_config={
            f"secret_{index}": D5SensitiveValue("v1", value)
            for index, value in enumerate(sensitive_values)
        },
        refinement_timeout_seconds=timeout,
    )
    app.start(
        TaskRequest.from_dict(
            {
                "goal": "verify local copy",
                "target": {
                    "attachments": [{"kind": "local_path", "path": "first.txt"}]
                },
            }
        ),
        source,
    )
    return [
        app,
        sandbox,
        outer,
        inner,
        review,
        clock,
        token,
        refine,
        tuple(sensitive_values),
    ]


@pytest.fixture
def d5_scenario(tmp_path):
    case = make_d5_scenario(tmp_path)
    try:
        yield case
    finally:
        close_scenario(case)


def _run(case):
    return asyncio.run(case[0].run(case[6]))


def _reopen(case, *, sensitive_config=...):
    original = case[0]
    task_id = original.state.task_run.task_run_id
    original.store.close()
    if sensitive_config is ...:
        sensitive_config = {
            f"secret_{index}": D5SensitiveValue("v1", value)
            for index, value in enumerate(case[8])
        }
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
        refinement_model=case[7],
        sensitive_config=sensitive_config,
    )
    case[0] = app
    app.controller.attach(task_id)
    app._bound_task_run_id = task_id
    return app


def test_committed_reviews_append_summaries_and_bounded_context_without_verifying_them(
    d5_scenario,
):
    case = d5_scenario
    result = _run(case)
    assert not result.recovery_required, result
    app = case[0]
    facts = app.store.list_process_facts(result.task_run_id)
    assert len(facts) == len(case[7].calls) == 2
    assert [fact.version for fact in facts] == [1, 2]
    assert len(result.verified_fact_ids) == 1
    assert (
        result.overall_goal == "not_evaluated" and result.task_run_status == "running"
    )
    assert any(call.process_facts for call in case[3].calls)
    assert all(len(call.process_facts) <= 4 for call in case[3].calls)
    assert all(
        item["identity"] == "model_summary"
        for call in case[3].calls
        for item in call.process_facts
    )
    assert all(
        app.store.load_d4_operation(fact.model_request_id).application_ref
        == fact.process_fact_id
        for fact in facts
    )
    assert (
        dict(app.store.d4_budget_usage(result.task_run_id).category_counts)[
            "refinement"
        ]
        == 2
    )
    for fact in facts:
        app._after_review(fact.review_id)
    assert len(case[7].calls) == 2
    checkpoint = app.state.latest_checkpoint.sequence
    reopened = _reopen(case)
    reopened._reconcile_known_d4_operations()
    assert reopened.state.latest_checkpoint.sequence == checkpoint
    assert reopened.store.list_process_facts(result.task_run_id) == facts
    assert len(case[7].calls) == 2


def test_definite_failure_is_nonblocking_and_same_sources_are_not_retried(d5_scenario):
    case = d5_scenario
    case[7].fail = True
    result = _run(case)
    assert not result.recovery_required, result
    attempts = case[0].store.list_d5_attempts(result.task_run_id)
    assert len(attempts) == 2 and not case[0].store.list_process_facts(
        result.task_run_id
    )
    for attempt in attempts:
        case[0]._after_review(attempt.review_id)
        operation = case[0].store.load_d4_operation(attempt.model_request_id)
        assert (
            operation.completion.result_kind == "failed"
            and operation.application_ref is None
        )
    assert len(case[7].calls) == 2


def test_response_unknown_seals_without_false_failure_and_late_response_is_ignored(
    tmp_path,
):
    case = make_d5_scenario(tmp_path, timeout=0.01)
    released, entered = threading.Event(), threading.Event()
    original = case[7].invoke

    def late(request, timeout_seconds, token):
        entered.set()
        released.wait(5)
        return original(request, timeout_seconds, token)

    case[7].invoke = late
    try:
        result = _run(case)
        assert entered.is_set() and not result.recovery_required, result
        attempts = case[0].store.list_d5_attempts(result.task_run_id)
        assert len(attempts) == 2
        for attempt in attempts:
            operation = case[0].store.load_d4_operation(attempt.model_request_id)
            assert operation.completion is None and operation.application_ref is None
            assert case[0].store.is_unknown_refinement_sealed(attempt.model_request_id)
            assert (
                case[0].refinement.evidence.read_result(attempt.model_request_id)[
                    "outcome"
                ]
                == "response_unknown"
            )
            assert (
                case[0].refinement.evidence.read_candidate(attempt.model_request_id)
                is None
            )
        assert not case[0].store.list_d4_unresolved_operations(result.task_run_id)
        assert not case[0].store.list_process_facts(result.task_run_id)
        released.set()
        _reopen(case)._reconcile_known_d4_operations()
        assert not case[0].store.list_process_facts(result.task_run_id)
        assert (
            dict(case[0].store.d4_budget_usage(result.task_run_id).category_counts)[
                "refinement"
            ]
            == 2
        )
        case[0].cancel(result.task_run_id)
        assert case[0].state.task_run.status.value == "cancelled"
    finally:
        released.set()
        close_scenario(case)


def test_saved_accepted_candidate_recovers_without_another_model_call(d5_scenario):
    case = d5_scenario
    evidence = case[0].refinement.evidence
    save_result = evidence.save_result

    def lost(*args):
        if args[1] == "accepted":
            raise ProcessLoss()
        return save_result(*args)

    evidence.save_result = lost
    with pytest.raises(ProcessLoss):
        _run(case)
    assert len(case[7].calls) == 1
    app = _reopen(case)
    app._reconcile_known_d4_operations()
    task_id = app.state.task_run.task_run_id
    assert len(app.store.list_process_facts(task_id)) == 1
    assert len(case[7].calls) == 1
    assert not app.store.list_d4_unresolved_operations(task_id)


def test_controller_commit_failure_does_not_become_nonblocking_model_failure(
    d5_scenario,
):
    case = d5_scenario
    app = case[0]
    commit = app.store.commit

    def fail(mutation):
        if mutation.operation == "append_process_fact":
            raise OSError("database not committed")
        return commit(mutation)

    app.store.commit = fail
    result = _run(case)
    assert result.recovery_required
    task_id = app.state.task_run.task_run_id
    assert not app.store.list_process_facts(task_id)
    attempt = app.store.list_d5_attempts(task_id)[0]
    assert (
        app.refinement.evidence.read_result(attempt.model_request_id)["outcome"]
        == "accepted"
    )
    assert not app.store.is_unknown_refinement_sealed(attempt.model_request_id)
    app.store.commit = commit
    _reopen(case)._reconcile_known_d4_operations()
    assert (
        len(case[0].store.list_process_facts(task_id)) == 1 and len(case[7].calls) == 1
    )


def test_new_source_same_value_is_still_new_and_conflicts_do_not_overwrite(d5_scenario):
    case = d5_scenario
    case[7].modify = lambda request, candidate: {
        **candidate,
        "claims": [{**candidate["claims"][0], "claim_value": str(len(case[7].calls))}],
    }
    result = _run(case)
    assert not result.recovery_required, result
    facts = case[0].store.list_process_facts(result.task_run_id)
    assert len(facts) == 2 and facts[0].sources != facts[1].sources
    assert any(item.kind == "candidate_disagreement" for item in facts[1].conflicts)
    assert (
        case[0]
        .refinement.evidence.read_candidate(facts[0].model_request_id)
        .claims[0]
        .claim_value
        == "1"
    )


def test_terminal_or_cancelled_task_does_not_start_new_refinement(d5_scenario):
    case = d5_scenario
    result = _run(case)
    calls = len(case[7].calls)
    case[0].cancel(result.task_run_id)
    reviews = case[0].store.list_d3_review_applications(result.task_run_id)
    for review in reviews:
        case[0]._after_review(review.review_id)
    assert len(case[7].calls) == calls


def test_d5_enablement_requires_d4_budget():
    role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-v1")
    with pytest.raises(ValueError, match="D5"):
        D3RunConfig(
            role,
            D1RoleConfig("inner_action", "fake", "same-model", "inner-v1"),
            D3Authorization(str(uuid4()), (0,)),
            role.role_config_ref,
            d5_enabled=True,
        )


@pytest.mark.parametrize("serialized_state", ("missing", "null", "empty"))
def test_d5_sensitive_requirements_must_be_explicit(tmp_path, serialized_state):
    from agent_core.dual_llm_contracts import d3_payload

    case = make_d5_scenario(tmp_path)
    try:
        payload = json.loads(json.dumps(d3_payload(case[0].config)))
        if serialized_state == "missing":
            payload.pop("d5_sensitive_requirements")
        elif serialized_state == "null":
            payload["d5_sensitive_requirements"] = None
        else:
            payload["d5_sensitive_requirements"] = []
        if serialized_state == "empty":
            restored = D3RunConfig.from_dict(payload)
            assert restored.d5_sensitive_requirements == ()
            assert d3_payload(restored)["d5_sensitive_requirements"] == ()
        else:
            with pytest.raises(ValueError, match="explicitly declared"):
                D3RunConfig.from_dict(payload)
    finally:
        close_scenario(case)
