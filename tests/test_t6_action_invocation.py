"""Audited T6 action calls never repeat an unknown model invocation."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from uuid import uuid4

import pytest

from agent_adapters.action_evidence_store import T6ActionEvidenceStore
from agent_adapters.pending_candidate_store import PendingCandidateStore
from agent_core.action_model_request import RetryProbeContext, build_action_model_request
from agent_core.control_state_repository import ModelContextSnapshot
from agent_core.control_store import FactRecordSnapshot, canonical_json
from agent_core.domain import FactStatus
from agent_core.intent_postconditions import IntentPostconditions, OutputContains
from agent_core.modeling import FactSnapshot, ModelError, ModelErrorKind
from agent_core.pending_candidates import PendingCandidateContext
from agent_core.action_audit import (
    ActionInvocationStart, ActionRecoveryRequired, ActionResourceIds, T6ActionJournal,
)
from agent_core.action_invocation import AuditedT6ActionModel
from agent_core.actions import ProbeAction, T6ActionValidationCode
from agent_core.action_model_request import T6ActionModelRequest
from test_t6_command_gateway import Scenario


class Model:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def invoke(self, request, timeout_seconds, cancellation_token):
        self.calls.append(request)
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def probe():
    return {
        "kind": "probe", "script": "printf ok", "working_directory": "/workspace/work",
        "timeout_seconds": 30, "conditions": [],
    }


def make_request(s):
    state = s.state
    intent = state.intents[0]
    return T6ActionModelRequest(
        str(uuid4()), state.task_run.task_run_id, intent.turn_id, intent.intent_id,
        state.task_run.request.goal, state.task_run.request.target,
        intent.description, (), (), (), PendingCandidateContext((), False),
    )


def make_invoker(s, root, model):
    journal = T6ActionJournal(s.store, T6ActionEvidenceStore(root / "action-evidence"))
    return AuditedT6ActionModel(journal, model, clock=s.clock), journal


@pytest.fixture
def s(tmp_path):
    scenario = Scenario(tmp_path)
    try:
        yield scenario
    finally:
        scenario.close()


def test_action_is_saved_and_replayed_with_same_resource_ids(s, tmp_path):
    model = Model(probe())
    invoker, journal = make_invoker(s, tmp_path, model)
    request = make_request(s)
    sequence = s.state.latest_checkpoint.sequence

    first = invoker.propose(request, sequence, s.state.task_run.deadline_at, s.token)
    replay = invoker.propose(make_request(s), sequence, s.state.task_run.deadline_at, s.token)

    assert isinstance(first.result, ProbeAction)
    assert replay.replayed
    assert replay.record.start.resource_ids == first.record.start.resource_ids
    assert len(model.calls) == 1
    assert first.record.completion is not None
    assert journal.list_for_intent(request.task_run_id, request.intent_id)[0].applied_at is None


def test_invalid_structure_gets_one_separately_audited_correction(s, tmp_path):
    model = Model({"kind": "unknown"}, probe())
    invoker, journal = make_invoker(s, tmp_path, model)
    request = make_request(s)

    result = invoker.propose(
        request, s.state.latest_checkpoint.sequence, s.state.task_run.deadline_at, s.token,
    )

    records = journal.list_for_intent(request.task_run_id, request.intent_id)
    assert isinstance(result.result, ProbeAction)
    assert [item.completion.result_kind for item in records] == ["invalid_structure", "action"]
    assert records[0].applied_at is not None
    assert records[1].start.correction_of_model_request_id == records[0].start.model_request_id
    assert records[1].start.context_sha256 == records[0].start.context_sha256
    assert len(model.calls) == 2


def test_legacy_context_digest_allows_existing_structure_correction(s, tmp_path):
    model = Model(probe())
    invoker, journal = make_invoker(s, tmp_path, model)
    request = make_request(s)
    old_context = request.context_payload()
    for name in (
        "model_request_id", "correction_of_model_request_id", "short_reason",
        "intent_verified_fact_ids", "latest_verifiable_plan_fact_ids",
    ):
        old_context.pop(name)
    old_digest = hashlib.sha256(
        canonical_json(old_context).encode("utf-8")
    ).hexdigest()
    start = ActionInvocationStart(
        request.model_request_id, request.task_run_id, request.intent_id,
        s.state.latest_checkpoint.sequence, old_digest,
        ActionResourceIds.create(), s.clock(),
    )
    journal.begin(start)
    journal.save_result(
        start, s.clock(), "invalid_structure",
        ModelError(ModelErrorKind.INVALID_RESPONSE, "invalid_t6_action_structure"),
    )
    journal.mark_applied(start.model_request_id, s.clock())

    result = invoker.propose(
        make_request(s), s.state.latest_checkpoint.sequence,
        s.state.task_run.deadline_at, s.token,
    )

    assert isinstance(result.result, ProbeAction)
    assert len(model.calls) == 1
    assert result.record.start.correction_of_model_request_id == start.model_request_id


def test_second_invalid_structure_stops_as_model_error(s, tmp_path):
    model = Model({"kind": "unknown"}, {"kind": "still_unknown"}, probe())
    invoker, journal = make_invoker(s, tmp_path, model)
    request = make_request(s)
    first = invoker.propose(
        request, s.state.latest_checkpoint.sequence, s.state.task_run.deadline_at, s.token,
    )
    second = invoker.propose(
        make_request(s), s.state.latest_checkpoint.sequence,
        s.state.task_run.deadline_at, s.token,
    )
    assert isinstance(first.result, ModelError)
    assert first.result.kind is ModelErrorKind.INVALID_RESPONSE
    assert first.result.validation_code == T6ActionValidationCode.ACTION_KIND_INVALID.value
    assert second.replayed and second.result == first.result
    assert [item.completion.result_kind for item in journal.list_for_intent(
        request.task_run_id, request.intent_id,
    )] == ["invalid_structure", "model_error"]
    assert len(model.calls) == 2
    saved = journal.read_result(journal.list_for_intent(
        request.task_run_id, request.intent_id,
    )[-1])
    assert isinstance(saved, ModelError)
    assert saved.validation_code == T6ActionValidationCode.ACTION_KIND_INVALID.value


def test_legacy_action_error_evidence_without_validation_code_is_readable(tmp_path):
    store = T6ActionEvidenceStore(tmp_path / "action-evidence")
    model_request_id = str(uuid4())
    digest = store.save(
        model_request_id,
        1.0,
        "model_error",
        ModelError(ModelErrorKind.PROVIDER_FAILURE, "provider_failed"),
    )
    result = store.read(model_request_id)
    assert result is not None
    _, kind, error, read_digest = result
    assert kind == "model_error"
    assert isinstance(error, ModelError)
    assert error.validation_code is None
    assert read_digest == digest


def test_started_call_without_result_requires_recovery(s, tmp_path):
    model = Model(RuntimeError("provider process vanished"), probe())
    invoker, journal = make_invoker(s, tmp_path, model)
    request = make_request(s)
    with pytest.raises(RuntimeError, match="provider process vanished"):
        invoker.propose(
            request, s.state.latest_checkpoint.sequence,
            s.state.task_run.deadline_at, s.token,
        )
    with pytest.raises(ActionRecoveryRequired):
        invoker.propose(
            make_request(s), s.state.latest_checkpoint.sequence,
            s.state.task_run.deadline_at, s.token,
        )
    assert len(model.calls) == 1
    assert journal.list_for_intent(request.task_run_id, request.intent_id)[0].completion is None


def test_saved_invalid_structure_recovers_without_repeating_first_call(
    s, tmp_path, monkeypatch,
):
    model = Model({"kind": "unknown"}, probe())
    invoker, journal = make_invoker(s, tmp_path, model)
    request = make_request(s)
    original = s.store.complete_t6_action

    def interrupted(completion):
        raise RuntimeError("completion transaction interrupted")

    monkeypatch.setattr(s.store, "complete_t6_action", interrupted)
    with pytest.raises(RuntimeError, match="completion transaction interrupted"):
        invoker.propose(
            request, s.state.latest_checkpoint.sequence,
            s.state.task_run.deadline_at, s.token,
        )
    monkeypatch.setattr(s.store, "complete_t6_action", original)

    result = invoker.propose(
        make_request(s), s.state.latest_checkpoint.sequence,
        s.state.task_run.deadline_at, s.token,
    )
    assert isinstance(result.result, ProbeAction)
    assert len(model.calls) == 2
    records = journal.list_for_intent(request.task_run_id, request.intent_id)
    assert records[0].completion.result_kind == "invalid_structure"
    assert records[0].applied_at is not None
    assert records[1].start.correction_of_model_request_id == records[0].start.model_request_id


def test_context_builder_requires_completed_short_plan(s, tmp_path):
    state = s.state
    intent_id = state.intents[0].intent_id
    context = ModelContextSnapshot((), ())
    candidates = PendingCandidateStore(tmp_path / "candidates")
    with pytest.raises(ValueError, match="retry_action_evidence_required"):
        build_action_model_request(
            state, context, intent_id, str(uuid4()), s.recorder, candidates,
        )
    s.call()
    s.commit(s.work.complete_plan("plan"))
    request = build_action_model_request(
        s.state, context, intent_id, str(uuid4()), s.recorder, candidates,
    )
    assert request.intent_description == "inspect output"
    assert len(request.observations) == 1
    assert request.observations[0].execution_id == s.state.executions[0].execution_id
    assert request.pending_candidates.entries == ()
    assert request.intent_verified_fact_ids == ()
    assert request.latest_verifiable_plan_fact_ids == ()


def test_completion_fact_ids_are_scoped_to_intent_and_latest_proved_plan(s, tmp_path):
    initial = s.state
    intent = initial.intents[0]
    s.postconditions.bind(IntentPostconditions(
        initial.task_run.task_run_id, intent.intent_id, "plan",
        (OutputContains(0, "stdout", "observed"),),
    ))
    s.call()
    s.commit(s.work.complete_plan("plan"))
    state = s.state
    own_id, other_id = str(uuid4()), str(uuid4())
    other_plan = replace(
        state.plans[0], plan_id=str(uuid4()), intent_id=state.intents[1].intent_id,
    )
    other_execution = replace(
        state.executions[0], execution_id=str(uuid4()), plan_id=other_plan.plan_id,
    )
    own_fact = FactRecordSnapshot(
        own_id, intent.turn_id, "own observation", state.executions[0].execution_id,
        FactStatus.VERIFIED, 1, 0, f"t6:{uuid4()}",
    )
    other_fact = FactRecordSnapshot(
        other_id, intent.turn_id, "other Intent observation",
        other_execution.execution_id, FactStatus.VERIFIED, 2, 0,
        f"t6:{uuid4()}",
    )
    state = replace(
        state, plans=state.plans + (other_plan,),
        executions=state.executions + (other_execution,),
        facts=(own_fact, other_fact),
    )
    context = ModelContextSnapshot((
        FactSnapshot(own_id, own_fact.content, own_fact.source_ref),
        FactSnapshot(other_id, other_fact.content, other_fact.source_ref),
    ), ())
    candidates = PendingCandidateStore(tmp_path / "candidates")
    request = build_action_model_request(
        state, context, intent.intent_id, str(uuid4()), s.recorder, candidates,
        s.postconditions,
    )
    assert {fact.fact_id for fact in request.verified_facts} == {own_id, other_id}
    assert request.context_payload()["intent_verified_fact_ids"] == [own_id]
    assert request.context_payload()["latest_verifiable_plan_fact_ids"] == [own_id]


def test_confirmed_timeout_exposes_same_plan_retry_context(s, tmp_path):
    s.commands.timeout = True
    result = s.call()
    assert result.action.value == "retry_step_command"
    intent_id = s.state.intents[0].intent_id
    candidates = PendingCandidateStore(tmp_path / "candidates")
    first = ProbeAction("sleep 99", b"", "/workspace/work", 30, ())
    with pytest.raises(ValueError, match="retry_action_evidence_required"):
        build_action_model_request(
            s.state, ModelContextSnapshot((), ()), intent_id,
            str(uuid4()), s.recorder, candidates,
        )
    request = build_action_model_request(
        s.state, ModelContextSnapshot((), ()), intent_id,
        str(uuid4()), s.recorder, candidates, s.postconditions, first,
        result.record.request_id,
    )
    assert request.retry_probe.plan_id == "plan"
    assert request.retry_probe.timeout_evidence_ref == result.record.request_id
    assert request.retry_probe.dispatched
    assert request.retry_probe.conditions == ()
    assert request.context_payload()["retry_probe"]["plan_id"] == "plan"


def test_retry_model_cannot_change_fixed_conditions(s, tmp_path):
    request = replace(
        make_request(s), retry_probe=RetryProbeContext(
            "plan", str(uuid4()), True, (OutputContains(0, "stdout", "ok"),),
        ),
    )
    wrong = probe()
    correct = probe()
    correct["conditions"] = [{
        "kind": "output_contains", "step_index": 0, "stream": "stdout", "literal": "ok",
    }]
    model = Model(wrong, correct)
    invoker, journal = make_invoker(s, tmp_path, model)
    result = invoker.propose(
        request, s.state.latest_checkpoint.sequence, s.state.task_run.deadline_at, s.token,
    )
    assert isinstance(result.result, ProbeAction)
    assert result.result.conditions == request.retry_probe.conditions
    assert [item.completion.result_kind for item in journal.list_for_intent(
        request.task_run_id, request.intent_id,
    )] == ["invalid_structure", "action"]


def test_undispatched_budget_timeout_is_visible_as_retry(s, tmp_path, monkeypatch):
    original = s.recorder.begin

    def slow_begin(*args):
        result = original(*args)
        s.monotonic.now += 61
        return result

    monkeypatch.setattr(s.recorder, "begin", slow_begin)
    first = s.call()
    assert first.record.status == "step_timeout"
    assert not first.record.dispatched
    assert not s.state.executions
    request = build_action_model_request(
        s.state, ModelContextSnapshot((), ()), s.state.intents[0].intent_id,
        str(uuid4()), s.recorder, PendingCandidateStore(tmp_path / "candidates"),
        s.postconditions, ProbeAction("printf ok", b"", "/workspace/work", 30, ()),
        first.record.request_id,
    )
    assert request.retry_probe.timeout_evidence_ref == s.state.plan_validation_records[0].plan_validation_id
    assert not request.retry_probe.dispatched
