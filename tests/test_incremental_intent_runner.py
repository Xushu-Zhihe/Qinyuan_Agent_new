"""T6 incremental Intent decisions through the real control/evidence boundary."""

from __future__ import annotations

import asyncio
import pytest

from agent_adapters.action_evidence_store import T6ActionEvidenceStore
from agent_adapters.pending_candidate_store import PendingCandidateStore
from agent_core.action_invocation import AuditedT6ActionModel
from agent_core.incremental_intent_runner import IncrementalIntentRunner
from agent_core.intent_postconditions import T6PostconditionVerifier
from agent_core.pending_candidates import PendingCandidateRecorder
from agent_core.action_audit import T6ActionJournal
from agent_core.domain import FactStatus, IntentStatus
from test_command_gateway import Scenario


class Decisions:
    def __init__(self, scenario, *responses):
        self.scenario = scenario
        self.responses = list(responses)
        self.calls = 0

    def invoke(self, request, timeout_seconds, cancellation_token):
        self.calls += 1
        response = self.responses.pop(0)
        if response == "complete":
            return {"kind": "complete_intent", "fact_ids": [
                item.fact_id for item in self.scenario.state.facts
                if item.status is FactStatus.VERIFIED
            ]}
        if response == "candidate":
            return {"kind": "candidate_flag", "value": "flag{pending}",
                    "execution_id": self.scenario.state.executions[-1].execution_id}
        if response == "abandon":
            return {"kind": "abandon_intent", "short_reason": "target_mismatch",
                    "execution_id": self.scenario.state.executions[-1].execution_id}
        return response


def probe(*, conditions=None, script="printf hello"):
    return {
        "kind": "probe", "script": script, "working_directory": "/workspace/work",
        "timeout_seconds": 30, "conditions": conditions or [],
    }


def setup_runner(s, root, decisions):
    journal = T6ActionJournal(s.store, T6ActionEvidenceStore(root / "actions"))
    s.proof_verifier.action_journal = journal
    candidate_store = PendingCandidateStore(root / "candidates")
    postconditions = T6PostconditionVerifier(
        s.controller, s.postconditions, s.recorder, files=None,
    )
    runner = IncrementalIntentRunner(
        controller=s.controller, store=s.store,
        repository=s.controller._persistent.repository,
        journal=journal, model=AuditedT6ActionModel(journal, decisions, clock=s.clock),
        gateway=s.gateway, postconditions=postconditions,
        proofs=s.proof_verifier,
        candidates=PendingCandidateRecorder(candidate_store, s.recorder),
        candidate_store=candidate_store, authorization=s.authorization,
        cancellation_token=s.token, clock=s.clock,
    )
    return runner, journal, candidate_store


@pytest.fixture
def s(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.commit(scenario.work.complete_plan("plan"))
    try:
        yield scenario
    finally:
        scenario.close()


def test_probe_fact_candidate_and_completion(s, tmp_path):
    responses = Decisions(s,
        probe(),
        "candidate",
        probe(conditions=[{"kind": "output_contains", "step_index": 0,
                           "stream": "stdout", "literal": "observed"}]),
        "complete",
    )
    runner, journal, candidate_store = setup_runner(s, tmp_path, responses)
    intent_id = s.state.intents[0].intent_id
    result = asyncio.run(runner.run(intent_id))
    assert result.outcome == "succeeded"
    assert responses.calls == 4
    assert len(s.commands.calls) == 2
    assert len(candidate_store.list_for_task(s.state.task_run.task_run_id)) == 1
    assert s.state.intents[0].status is IntentStatus.SUCCEEDED
    assert len(s.state.facts) == 1
    assert s.state.facts[0].status is FactStatus.VERIFIED
    assert all(item.applied_at is not None for item in journal.list_for_intent(
        s.state.task_run.task_run_id, intent_id,
    ))


def test_completed_probe_replays_after_mark_applied_interruption(s, tmp_path, monkeypatch):
    responses = Decisions(s,
        probe(conditions=[{"kind": "output_contains", "step_index": 0,
                           "stream": "stdout", "literal": "observed"}]),
        "complete",
    )
    runner, journal, _ = setup_runner(s, tmp_path, responses)
    intent_id = s.state.intents[0].intent_id
    original = journal.mark_applied
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after Fact commit")
        return original(*args)

    monkeypatch.setattr(journal, "mark_applied", interrupted)
    with pytest.raises(RuntimeError, match="crash after Fact"):
        asyncio.run(runner.run(intent_id))
    assert len(s.state.facts) == 1
    assert len(s.commands.calls) == 1
    result = asyncio.run(runner.resume(intent_id))
    assert result.outcome == "succeeded"
    assert len(s.state.facts) == 1
    assert len(s.commands.calls) == 1
    assert responses.calls == 2


def test_environment_fault_stops_without_redispatch(s, tmp_path):
    from agent_core.command_contracts import T6CommandExecutionError

    responses = Decisions(s, probe())
    runner, _, _ = setup_runner(s, tmp_path, responses)
    s.commands.error = T6CommandExecutionError(dispatched=False, reason="docker unavailable")
    result = asyncio.run(runner.run(s.state.intents[0].intent_id))
    assert result.outcome == "inconclusive"
    assert result.reason == "environment_fault"
    assert len(s.commands.calls) == 1
    assert responses.calls == 1


def test_confirmed_timeout_reuses_plan_and_allows_one_new_command(s, tmp_path):
    conditions = [{"kind": "output_contains", "step_index": 0,
                   "stream": "stdout", "literal": "observed"}]
    responses = Decisions(s, probe(conditions=conditions),
                          probe(conditions=conditions, script="printf retry"), "complete")
    runner, journal, _ = setup_runner(s, tmp_path, responses)
    s.commands.on_execute = lambda: setattr(s.commands, "timeout", len(s.commands.calls) == 1)
    intent_id = s.state.intents[0].intent_id
    result = asyncio.run(runner.run(intent_id))
    assert result.outcome == "succeeded"
    assert len(s.commands.calls) == 2
    records = journal.list_for_intent(s.state.task_run.task_run_id, intent_id)
    assert len(records) == 3
    assert len([item for item in s.state.plans if item.intent_id == intent_id]) == 2
    assert len(s.state.facts) == 1


def test_latest_exploration_does_not_erase_prior_verified_plan(s, tmp_path):
    responses = Decisions(s,
        probe(conditions=[{"kind": "output_contains", "step_index": 0,
                           "stream": "stdout", "literal": "observed"}]),
        probe(script="printf explore"), "complete",
    )
    runner, _, _ = setup_runner(s, tmp_path, responses)
    result = asyncio.run(runner.run(s.state.intents[0].intent_id))
    assert result.outcome == "succeeded"
    assert len(s.state.facts) == 1
    assert len(s.commands.calls) == 2


def test_abandon_commit_is_reconciled_before_sibling_dispatch(s, tmp_path, monkeypatch):
    responses = Decisions(s, probe(), "abandon")
    runner, journal, _ = setup_runner(s, tmp_path, responses)
    intent_id = s.state.intents[0].intent_id
    original = journal.mark_applied
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash after target mismatch")
        return original(*args)

    monkeypatch.setattr(journal, "mark_applied", interrupted)
    with pytest.raises(RuntimeError, match="crash after target mismatch"):
        asyncio.run(runner.run(intent_id))
    assert s.state.intents[0].status is IntentStatus.FAILED
    assert journal.list_for_intent(s.state.task_run.task_run_id, intent_id)[-1].applied_at is None
    runner.reconcile_terminal_actions()
    assert journal.list_for_intent(s.state.task_run.task_run_id, intent_id)[-1].applied_at
    assert len(s.commands.calls) == 1
