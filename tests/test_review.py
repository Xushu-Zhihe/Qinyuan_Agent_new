"""D2 review contracts, durable attempts, and trusted action gating."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_adapters.review_ledger import FileD2ReviewLedger
from agent_core.review import (
    D2NotCalledResult,
    D2ReviewCoordinator,
    D2ReviewPaused,
    D2ReviewSession,
)
from agent_core.review_contracts import (
    D2ModelFailure,
    D2ModelResponse,
    D2ReviewOutput,
    D2ReviewRequest,
)


def _request(tmp: Path, *, scope="action", candidate=True, context="v1"):
    return D2ReviewRequest(
        str(uuid4()),
        str(uuid4()),
        1,
        1,
        scope,
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()) if candidate else None,
        1,
        context,
        ("execution:1", "fact:1"),
        0,
        "bounded context",
    )


class Model:
    def __init__(self, values):
        self.values = list(values)
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _accepted(*, decision="continue_current_intent", constraints=None):
    return {
        "decision": decision,
        "rationale": "saved evidence supports the next trusted step",
        "source_ids": ["execution:1", "fact:1"],
        "constraints": constraints or [],
    }


def test_turn_start_and_end_require_null_candidate():
    request = _request(Path("."), scope="turn_start", candidate=False)
    assert request.action_candidate_id is None
    with pytest.raises(ValueError, match="turn review"):
        _request(Path("."), scope="turn_end", candidate=True)


def test_review_is_durable_and_same_context_is_reused(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path)
    model = Model([_accepted()])
    first = coordinator.review(request, model)
    second = coordinator.review(request, model)
    assert first.outcome == "accepted"
    assert second.outcome == "reused"
    assert len(model.requests) == 1
    assert ledger.review_call_count() == 1


def test_action_constraints_intersect_and_reject_future_dispatch(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path)
    intent_id = request.intent_id
    output = _accepted(
        constraints=[
            {
                "kind": "allowed_action_kinds",
                "scope_kind": "intent",
                "scope_id": intent_id,
                "allowed_action_kinds": ["complete_intent"],
            },
            {
                "kind": "max_action_timeout_seconds",
                "scope_kind": "intent",
                "scope_id": intent_id,
                "max_action_timeout_seconds": 30,
            },
        ]
    )
    coordinator.review(request, Model([output]))
    coordinator.validate_future_action(
        (request.review_root_id,), action_kind="complete_intent", timeout_seconds=30
    )
    with pytest.raises(D2ReviewPaused, match="action_kind"):
        coordinator.validate_future_action(
            (request.review_root_id,), action_kind="probe", timeout_seconds=1
        )
    coordinator.validate_future_action(
        (request.review_root_id,), action_kind="complete_intent", timeout_seconds=31
    )


def test_invalid_response_gets_one_retry_but_unknown_does_not(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path)
    model = Model([{"bad": True}, _accepted()])
    result = coordinator.review(request, model)
    assert result.outcome == "accepted"
    assert len(model.requests) == 2
    assert ledger.review_call_count() == 2

    unknown_request = _request(tmp_path)
    unknown = Model([D2ModelFailure("provider_failure", "upstream unavailable")])
    with pytest.raises(D2ReviewPaused, match="provider_failure"):
        coordinator.review(unknown_request, unknown)
    with pytest.raises(D2ReviewPaused, match="provider_failure"):
        coordinator.review(unknown_request, Model([_accepted()]))
    assert len(unknown.requests) == 1


def test_turn_end_rejects_constraints_and_unknown_call_is_not_retried(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path, scope="turn_end", candidate=False)
    model = Model(
        [
            {
                "decision": "acknowledge_turn_end",
                "rationale": "turn was committed",
                "source_ids": ["execution:1"],
                "constraints": [],
            }
        ]
    )
    assert coordinator.review(request, model).outcome == "accepted"
    assert model.requests[0].action_candidate_id is None

    unknown_request = _request(tmp_path, scope="turn_start", candidate=False)
    unknown = Model([TimeoutError("no reliable completion")])
    with pytest.raises(D2ReviewPaused, match="unknown"):
        coordinator.review(unknown_request, unknown)
    with pytest.raises(D2ReviewPaused, match="unknown"):
        coordinator.review(unknown_request, Model([_accepted()]))


def test_context_change_uses_new_generation_but_shared_attempt_budget(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path, context="v1")
    assert coordinator.review(request, Model([_accepted()])).review_generation == 1
    changed = replace(request, context_version="v2")
    model = Model([_accepted()])
    result = coordinator.review(changed, model)
    assert result.review_generation == 2
    assert result.attempts_used == 2
    assert len(model.requests) == 1
    with pytest.raises(D2ReviewPaused, match="retry_exhausted"):
        coordinator.review(replace(changed, context_version="v3"), Model([_accepted()]))


def test_saved_unapplied_stale_response_refreshes_with_shared_budget(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    saved = _request(tmp_path)
    ledger.begin_attempt(saved)
    ledger.finish_attempt(
        saved, outcome="accepted", response=_accepted(), provider_request_id=None
    )
    fresh = replace(saved, context_version="v2", constraint_version=1)
    applied = []
    model = Model([_accepted()])
    coordinator = D2ReviewCoordinator(
        ledger,
        refresh=lambda request: replace(
            fresh, review_generation=request.review_generation
        ),
        apply=lambda request, output: applied.append(request) or "applied:v2",
    )

    result = coordinator.review(fresh, model)

    assert result.review_generation == 2 and result.attempts_used == 2
    assert len(model.requests) == len(applied) == 1
    assert applied[0].context_version == "v2"
    root = ledger.read_root(saved.review_root_id)
    assert [item["request"]["context_version"] for item in root["calls"]] == [
        "v1",
        "v2",
    ]
    assert root["applied"][0]["review_id"] != saved.review_id


def test_context_change_during_call_regenerates_and_exhaustion_never_applies_stale(
    tmp_path,
):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    request = _request(tmp_path)
    current = [request]
    applied = []

    class ChangingModel(Model):
        def invoke(self, invocation):
            value = super().invoke(invocation)
            current[0] = replace(
                current[0], context_version=f"v{len(self.requests) + 1}"
            )
            return value

    model = ChangingModel([_accepted(), _accepted()])
    coordinator = D2ReviewCoordinator(
        ledger,
        refresh=lambda invocation: replace(
            current[0], review_generation=invocation.review_generation
        ),
        apply=lambda invocation, output: applied.append(invocation) or "never",
    )
    with pytest.raises(D2ReviewPaused, match="retry_exhausted"):
        coordinator.review(request, model)
    assert [item.review_generation for item in model.requests] == [1, 2]
    assert not applied and ledger.review_call_count(request.task_run_id) == 2
    with pytest.raises(D2ReviewPaused, match="retry_exhausted"):
        coordinator.review(current[0], model)
    assert len(model.requests) == 2 and not applied


def test_context_change_after_first_response_applies_only_refreshed_generation(
    tmp_path,
):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    request = _request(tmp_path)
    current = [request]
    applied = []

    class ChangingModel(Model):
        def invoke(self, invocation):
            response = super().invoke(invocation)
            current[0] = replace(request, context_version="v2")
            return response

    model = ChangingModel([_accepted(), _accepted()])
    result = D2ReviewCoordinator(
        ledger,
        refresh=lambda invocation: replace(
            current[0], review_generation=invocation.review_generation
        ),
        apply=lambda invocation, output: applied.append(invocation) or "applied:v2",
    ).review(request, model)
    assert result.review_generation == 2 and result.attempts_used == 2
    assert len(applied) == 1 and applied[0].context_version == "v2"


def test_session_defers_action_review_until_evidence_and_records_not_called(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    session = D2ReviewSession(D2ReviewCoordinator(ledger))
    request = _request(tmp_path)
    result = session.action(
        request,
        Model([_accepted()]),
        action_finished=True,
        evidence_saved=True,
        triggered=False,
    )
    assert isinstance(result, D2NotCalledResult)
    with pytest.raises(D2ReviewPaused, match="not_saved"):
        session.action(
            request,
            Model([_accepted()]),
            action_finished=False,
            evidence_saved=False,
            triggered=True,
        )


def test_started_call_survives_crash_and_is_never_resent(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    request = _request(tmp_path)
    ledger.begin_attempt(request)
    resumed = FileD2ReviewLedger(tmp_path / "reviews")
    model = Model([_accepted()])
    with pytest.raises(D2ReviewPaused, match="unknown"):
        D2ReviewCoordinator(resumed).review(request, model)
    assert model.requests == []
    assert resumed.review_call_count(request.task_run_id) == 1


def test_accepted_result_is_reused_after_application_crash(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    request = _request(tmp_path)
    attempted = []

    def crash_apply(current, output):
        attempted.append(current.review_id)
        raise RuntimeError("application did not commit")

    model = Model([_accepted()])
    with pytest.raises(D2ReviewPaused, match="application_uncommitted"):
        D2ReviewCoordinator(ledger, apply=crash_apply).review(request, model)
    root = ledger.read_root(request.review_root_id)
    assert root["last_outcome"] == "accepted"
    assert root["last_response"] == _accepted()
    successful = []

    def apply_saved(current, output):
        successful.append(current.review_id)
        return "controller:applied"

    result = D2ReviewCoordinator(ledger, apply=apply_saved).review(request, model)
    assert result.outcome == "reused"
    assert successful == attempted
    assert len(model.requests) == 1


def test_typed_review_output_cannot_bypass_scope_decision(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path, scope="turn_end", candidate=False)
    model = Model(
        [
            D2ReviewOutput("continue_current_intent", "invalid tail decision", (), ()),
            _accepted(decision="acknowledge_turn_end"),
        ]
    )
    assert coordinator.review(request, model).outcome == "accepted"
    root = ledger.read_root(request.review_root_id)
    assert root["calls"][0]["reason"] == "turn_end_decision_invalid"
    assert root["calls"][0]["response"]["decision"] == "continue_current_intent"


def test_retry_refreshes_trusted_context_before_next_call(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    request = _request(tmp_path)
    model = Model([{"invalid": True}, _accepted()])

    def refresh(current):
        if model.requests:
            return replace(
                current, context_version="fresh-source-version", checkpoint_id=2
            )
        return current

    result = D2ReviewCoordinator(ledger, refresh=refresh).review(request, model)
    assert result.review_generation == 2
    assert model.requests[1].context_version == "fresh-source-version"
    assert model.requests[1].checkpoint_id == 2


def test_constraints_cross_roots_and_expire_with_scope(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path)
    constraint = {
        "kind": "allowed_action_kinds",
        "scope_kind": "intent",
        "scope_id": request.intent_id,
        "allowed_action_kinds": ["complete_intent"],
    }
    coordinator.review(request, Model([_accepted(constraints=[constraint])]))
    next_root = replace(
        _request(tmp_path),
        task_run_id=request.task_run_id,
        turn_id=request.turn_id,
        intent_id=request.intent_id,
    )
    wider = {**constraint, "allowed_action_kinds": ["probe", "complete_intent"]}
    with pytest.raises(D2ReviewPaused, match="retry_exhausted"):
        coordinator.review(next_root, Model([_accepted(constraints=[wider])] * 2))
    with pytest.raises(D2ReviewPaused, match="action_kind"):
        coordinator.validate_future_action(
            task_run_id=request.task_run_id,
            turn_id=request.turn_id,
            intent_id=request.intent_id,
            action_kind="probe",
            timeout_seconds=30,
        )
    coordinator.validate_future_action(
        task_run_id=request.task_run_id,
        turn_id=request.turn_id,
        intent_id=request.intent_id,
        action_kind="probe",
        timeout_seconds=30,
        live_intent_ids=(),
        live_turn_ids=(request.turn_id,),
    )
    assert ledger.read_root(request.review_root_id)["active_constraints"] == [
        constraint
    ]


def test_late_same_snapshot_trigger_reasons_merge_without_recall(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path)
    model = Model([_accepted()])
    coordinator.review(request, model, trigger_reasons=("request_review",))
    coordinator.review(
        request, model, trigger_reasons=("request_review", "trusted_not_satisfied")
    )
    assert len(model.requests) == 1
    assert ledger.read_root(request.review_root_id)["trigger_reasons"]["v1"] == [
        "request_review",
        "trusted_not_satisfied",
    ]


def test_successful_provider_id_is_only_audit_metadata(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    request = _request(tmp_path)
    model = Model([D2ModelResponse(_accepted(), "provider-id-1")])
    assert D2ReviewCoordinator(ledger).review(request, model).outcome == "accepted"
    call = ledger.read_root(request.review_root_id)["calls"][0]
    assert call["request"]["review_id"] == model.requests[0].review_id
    assert call["provider_request_id"] == "provider-id-1"
    assert call["response"]["decision"] == "continue_current_intent"


def test_untriggered_action_records_audit_without_review_call(tmp_path):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    session = D2ReviewSession(D2ReviewCoordinator(ledger))
    request = _request(tmp_path)
    model = Model([])
    result = session.action(
        request, model, action_finished=True, evidence_saved=True, triggered=False
    )
    assert isinstance(result, D2NotCalledResult)
    assert model.requests == []
    root = ledger.read_root(request.review_root_id)
    assert root["trigger_reasons"]["v1"] == ["no_review_trigger"]
    assert ledger.review_call_count(request.task_run_id) == 0


@pytest.mark.parametrize(
    "failure", [RuntimeError("transport failed"), OSError("disconnected")]
)
def test_arbitrary_model_exception_is_unknown_and_never_resent(tmp_path, failure):
    ledger = FileD2ReviewLedger(tmp_path / "reviews")
    coordinator = D2ReviewCoordinator(ledger)
    request = _request(tmp_path)
    model = Model([failure, _accepted()])
    with pytest.raises(D2ReviewPaused, match="unknown"):
        coordinator.review(request, model)
    with pytest.raises(D2ReviewPaused, match="unknown"):
        coordinator.review(replace(request, context_version="v2"), model)
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "failure_kind", ["authentication_failed", "refused", "provider_failure"]
)
def test_terminal_provider_failures_consume_one_attempt_and_never_resume_by_retry(
    tmp_path, failure_kind
):
    ledger = FileD2ReviewLedger(tmp_path / failure_kind)
    request = _request(tmp_path)
    model = Model([D2ModelFailure(failure_kind, "provider outcome is final")])
    coordinator = D2ReviewCoordinator(ledger)

    with pytest.raises(D2ReviewPaused, match=failure_kind):
        coordinator.review(request, model)
    with pytest.raises(D2ReviewPaused, match=failure_kind):
        coordinator.review(request, Model([_accepted()]))

    root = ledger.read_root(request.review_root_id)
    assert root["attempts_used"] == 1
    assert root["last_outcome"] == failure_kind
    assert len(model.requests) == 1
    assert ledger.review_call_count(request.task_run_id) == 1


@pytest.mark.parametrize(
    "reason", ["cancelled_before_tail_review", "deadline_before_tail_review"]
)
def test_lifecycle_stopped_tail_review_records_not_called_without_model(
    tmp_path, reason
):
    ledger = FileD2ReviewLedger(tmp_path / reason)
    session = D2ReviewSession(D2ReviewCoordinator(ledger))
    request = _request(tmp_path, scope="turn_end", candidate=False)
    model = Model([])

    result = session.turn_end(
        request,
        model,
        turn_completed=True,
        cancelled=reason == "cancelled_before_tail_review",
        deadline_exceeded=reason == "deadline_before_tail_review",
    )

    assert isinstance(result, D2NotCalledResult)
    assert model.requests == []
    assert ledger.review_call_count(request.task_run_id) == 0
    assert ledger._read()["not_called"] == [
        {
            "review_id": request.review_id,
            "review_root_id": request.review_root_id,
            "review_generation": request.review_generation,
            "review_scope": "turn_end",
            "reason": reason,
        }
    ]
