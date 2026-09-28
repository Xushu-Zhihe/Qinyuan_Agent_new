"""A proved T6 observation can finish an Intent without solving the TaskRun."""

import hashlib
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from test_t6_command_gateway import Scenario

from agent_core.control_store import IdempotencyConflictError, StorageError
from agent_core.domain import DomainError, FactStatus, IntentStatus, Plan, PlanStep
from agent_core.runtime import TaskRunStatus
from agent_core.observation_proof import ObservationProofError
from agent_core.intent_postconditions import (
    CheckStatus,
    FileDigestObservation,
    FileSha256,
    IntentPostconditions,
    OutputContains,
    T6PostconditionVerifier,
)


class Files:
    def __init__(self):
        self.calls = 0

    def hash_file(self, task_run_id, sandbox_id, path):
        self.calls += 1
        return FileDigestObservation("present", hashlib.sha256(b"result").hexdigest())


@pytest.fixture
def s(tmp_path):
    scenario = Scenario(tmp_path)
    try:
        yield scenario
    finally:
        scenario.close()


def bind(s, files, *conditions):
    verifier = T6PostconditionVerifier(
        s.controller, s.postconditions, s.recorder, files
    )
    binding = IntentPostconditions(
        s.state.task_run.task_run_id,
        s.state.intents[0].intent_id,
        "plan",
        conditions,
    )
    verifier.bind(binding)
    return verifier, binding


def finish(s):
    s.call()
    s.commit(s.work.complete_plan("plan"))


def test_verified_observations_commit_fact_and_intent_atomically(s, monkeypatch):
    files = Files()
    verifier, binding = bind(
        s,
        files,
        OutputContains(0, "stdout", "observed"),
        FileSha256(0, "/workspace/work/result", hashlib.sha256(b"result").hexdigest()),
    )
    finish(s)
    assert verifier.verify(binding.intent_id) is CheckStatus.SATISFIED
    proof_id, fact_id, mutation_id = (str(uuid4()) for _ in range(3))
    proof = s.proof_verifier.issue(binding.intent_id, proof_id)
    assert proof.execution_ids == (s.state.executions[0].execution_id,) * 2
    # Saving the same proof twice must be an immutable idempotent write.
    s.proof_store.save(proof)
    first = s.controller.commit_verified_t6_observation(
        binding.intent_id, proof_id, fact_id, mutation_id
    )
    with monkeypatch.context() as patch:

        def unavailable(_):
            raise AssertionError("exact replay must not read proof evidence")

        patch.setattr(s.proof_verifier, "read_proof", unavailable)
        replay = s.controller.commit_verified_t6_observation(
            binding.intent_id, proof_id, fact_id, mutation_id
        )
    assert first.commit.checkpoint == replay.commit.checkpoint
    assert s.state.facts[0].status is FactStatus.VERIFIED
    assert s.state.facts[0].verification_ref == f"t6:{proof_id}"
    assert s.state.intents[0].status is IntentStatus.SUCCEEDED
    assert s.state.task_run.status is TaskRunStatus.RUNNING
    assert files.calls == 1
    with pytest.raises(IdempotencyConflictError):
        s.controller.commit_verified_t6_observation(
            binding.intent_id, str(uuid4()), fact_id, mutation_id
        )


def test_short_plan_facts_stay_verified_while_intent_continues(s):
    files = Files()
    verifier, first = bind(
        s, files, FileSha256(0, "/workspace/work/result", hashlib.sha256(b"result").hexdigest())
    )
    finish(s)
    assert verifier.verify(first.intent_id) is CheckStatus.SATISFIED
    first_proof = s.proof_verifier.issue(first.intent_id, str(uuid4()), first.plan_id)
    first_fact_id, first_mutation_id = str(uuid4()), str(uuid4())
    s.controller.commit_verified_t6_observation(
        first.intent_id, first_proof.proof_id, first_fact_id, first_mutation_id,
        complete_intent=False,
    )
    assert s.state.intents[0].status is IntentStatus.RUNNING
    assert s.state.facts[0].status is FactStatus.VERIFIED
    replay = s.controller.commit_verified_t6_observation(
        first.intent_id, first_proof.proof_id, first_fact_id, first_mutation_id,
        complete_intent=False,
    )
    assert replay.commit.replayed
    with pytest.raises(IdempotencyConflictError):
        s.controller.commit_verified_t6_observation(
            first.intent_id, first_proof.proof_id, first_fact_id, first_mutation_id,
            complete_intent=True,
        )

    s.commit(s.work.register_plan(Plan("plan-2", first.intent_id, (PlanStep("probe again"),))))
    s.commit(s.work.start_plan("plan-2"))
    second = IntentPostconditions(
        first.task_run_id, first.intent_id, "plan-2",
        (FileSha256(0, "/workspace/work/result", hashlib.sha256(b"result").hexdigest()),),
    )
    verifier.bind(second)
    s.call(s.request(plan_id="plan-2"))
    s.commit(s.work.complete_plan("plan-2"))
    assert verifier.verify(first.intent_id) is CheckStatus.SATISFIED
    assert files.calls == 2
    second_proof = s.proof_verifier.issue(first.intent_id, str(uuid4()), second.plan_id)
    second_fact_id = str(uuid4())
    s.controller.commit_verified_t6_observation(
        first.intent_id, second_proof.proof_id, second_fact_id, str(uuid4()),
        complete_intent=False,
    )
    assert len(s.state.facts) == 2
    assert s.proof_verifier.read_verified(
        first_proof.source_execution_id, first_proof.proof_id
    )[0] == first_proof
    completion_id = str(uuid4())
    completion = s.controller.complete_verified_t6_intent(
        first.intent_id, (first_fact_id, second_fact_id), completion_id
    )
    assert completion.value == "succeeded"
    replay = s.controller.complete_verified_t6_intent(
        first.intent_id, (first_fact_id, second_fact_id), completion_id
    )
    assert replay.commit.replayed
    with pytest.raises(IdempotencyConflictError):
        s.controller.complete_verified_t6_intent(
            first.intent_id, (second_fact_id, first_fact_id), completion_id
        )
    assert s.state.intents[0].status is IntentStatus.SUCCEEDED


def test_t6_completion_requires_fact_from_latest_short_plan(s):
    verifier, first = bind(s, Files(), OutputContains(0, "stdout", "observed"))
    finish(s)
    assert verifier.verify(first.intent_id) is CheckStatus.SATISFIED
    proof = s.proof_verifier.issue(first.intent_id, str(uuid4()), first.plan_id)
    fact_id = str(uuid4())
    s.controller.commit_verified_t6_observation(
        first.intent_id, proof.proof_id, fact_id, str(uuid4()), complete_intent=False,
    )
    s.commit(s.work.register_plan(Plan("exploration", first.intent_id, (PlanStep("look"),))))
    s.commit(s.work.start_plan("exploration"))
    s.call(s.request(plan_id="exploration"))
    s.commit(s.work.complete_plan("exploration"))
    with pytest.raises(DomainError, match="trusted latest-Plan"):
        s.work.succeed_intent_with_fact(intent_id=first.intent_id, fact_id=fact_id)
    with pytest.raises(ObservationProofError, match="plan_declaration_unavailable"):
        s.controller.complete_verified_t6_intent(first.intent_id, (fact_id,), str(uuid4()))
    assert s.state.intents[0].status is IntentStatus.RUNNING


def test_timeout_retry_can_prove_the_successful_output(s):
    verifier, binding = bind(s, Files(), OutputContains(0, "stdout", "observed"))
    s.commands.timeout = True
    s.call()
    s.commands.timeout = False
    second = s.call(s.request(script="changed after confirmed timeout"))
    s.commit(s.work.complete_plan("plan"))
    assert verifier.verify(binding.intent_id) is CheckStatus.SATISFIED
    proof = s.proof_verifier.issue(binding.intent_id, str(uuid4()))
    assert proof.source_execution_id == second.record.request_id


def test_sqlite_failure_rolls_back_both_fact_and_intent(s):
    verifier, binding = bind(s, Files(), OutputContains(0, "stdout", "observed"))
    finish(s)
    assert verifier.verify(binding.intent_id) is CheckStatus.SATISFIED
    proof = s.proof_verifier.issue(binding.intent_id, str(uuid4()))
    before = s.state
    with closing(sqlite3.connect(s.store.path, isolation_level=None)) as connection:
        connection.execute(
            "CREATE TRIGGER reject_t6_fact BEFORE INSERT ON facts "
            "BEGIN SELECT RAISE(ABORT, 'injected_fact_failure'); END"
        )
        try:
            with pytest.raises(StorageError, match="injected_fact_failure"):
                s.controller.commit_verified_t6_observation(
                    binding.intent_id, proof.proof_id, str(uuid4()), str(uuid4())
                )
        finally:
            connection.execute("DROP TRIGGER reject_t6_fact")
    assert s.state == before
    rows = s.store.load_control_state_rows(s.state.task_run.task_run_id)
    assert rows is not None and not rows.facts
    assert rows.intents[0].status is IntentStatus.RUNNING
    assert rows.latest_checkpoint == before.latest_checkpoint


def test_mismatch_cannot_issue_proof_or_commit_fact(s):
    files = Files()
    verifier, binding = bind(s, files, OutputContains(0, "stdout", "missing"))
    finish(s)
    assert verifier.verify(binding.intent_id) is CheckStatus.NOT_SATISFIED
    with pytest.raises(ObservationProofError, match="not_satisfied"):
        s.proof_verifier.issue(binding.intent_id, str(uuid4()))
    assert not s.state.facts
    assert s.state.intents[0].status is IntentStatus.RUNNING


@pytest.mark.parametrize(
    "status", ["truncated", "missing_file_observation", "corrupt_output"]
)
def test_incomplete_evidence_never_issues_proof(s, status):
    condition = (
        FileSha256(0, "/workspace/work/result", hashlib.sha256(b"result").hexdigest())
        if status == "missing_file_observation"
        else OutputContains(
            0, "stdout", "absent" if status == "truncated" else "observed"
        )
    )
    _, binding = bind(s, Files(), condition)
    s.commands.stdout_truncated = status == "truncated"
    finish(s)
    if status == "corrupt_output":
        path = s.recorder._path("output", s.state.executions[0].execution_id)
        original = path.read_bytes()
        path.write_bytes(b"broken")
    try:
        with pytest.raises(ObservationProofError, match="inconclusive"):
            s.proof_verifier.issue(binding.intent_id, str(uuid4()))
        assert not s.state.facts
    finally:
        if status == "corrupt_output":
            path.write_bytes(original)


@pytest.mark.parametrize("end", ["cancel", "deadline"])
def test_late_proof_cannot_commit_after_task_stops(s, end):
    verifier, binding = bind(s, Files(), OutputContains(0, "stdout", "observed"))
    finish(s)
    assert verifier.verify(binding.intent_id) is CheckStatus.SATISFIED
    proof = s.proof_verifier.issue(binding.intent_id, str(uuid4()))
    if end == "cancel":
        s.controller.cancel(str(uuid4()))
    else:
        s.clock.now = s.state.task_run.deadline_at
    with pytest.raises(ObservationProofError, match="state_conflict"):
        s.controller.commit_verified_t6_observation(
            binding.intent_id, proof.proof_id, str(uuid4()), str(uuid4())
        )
    assert not s.state.facts


def test_missing_or_corrupt_proof_blocks_commit_and_reload(s):
    files = Files()
    verifier, binding = bind(s, files, OutputContains(0, "stdout", "observed"))
    finish(s)
    assert verifier.verify(binding.intent_id) is CheckStatus.SATISFIED
    with pytest.raises(ObservationProofError, match="proof_missing"):
        s.controller.commit_verified_t6_observation(
            binding.intent_id, str(uuid4()), str(uuid4()), str(uuid4())
        )
    proof_id = str(uuid4())
    s.proof_verifier.issue(binding.intent_id, proof_id)
    s.controller.commit_verified_t6_observation(
        binding.intent_id, proof_id, str(uuid4()), str(uuid4())
    )
    proof_path = s.proof_store._records._path("observation-proof", proof_id)
    original = proof_path.read_bytes()
    try:
        proof_path.write_bytes(b"corrupt")
        with pytest.raises(StorageError):
            s.controller.reload_state()
    finally:
        proof_path.write_bytes(original)
        s.controller.reload_state()
