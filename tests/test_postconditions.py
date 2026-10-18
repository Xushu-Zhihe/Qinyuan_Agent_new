"""Predeclared Intent conditions cannot turn weak or missing evidence into Fact."""

import hashlib
from dataclasses import replace

import pytest

from agent_adapters.postcondition_store import T6PostconditionStore
from agent_adapters.command_recorder import T6EvidenceError
from agent_core.intent_postconditions import (
    CheckStatus,
    FileDigestObservation,
    FileSha256,
    IntentPostconditions,
    OutputContains,
    T6PostconditionVerifier,
)
from test_command_gateway import Scenario


class FakeFiles:
    def __init__(self):
        self.result = FileDigestObservation(
            "present", hashlib.sha256(b"artifact").hexdigest()
        )
        self.calls = 0

    def hash_file(self, task_run_id, sandbox_id, path):
        self.calls += 1
        return self.result


@pytest.fixture
def scenario(tmp_path):
    s = Scenario(tmp_path)
    files = FakeFiles()
    store = T6PostconditionStore(tmp_path / "conditions")
    verifier = T6PostconditionVerifier(s.controller, store, s.recorder, files)
    try:
        yield s, files, store, verifier
    finally:
        s.close()


def binding(s, *conditions):
    return IntentPostconditions(
        s.state.task_run.task_run_id,
        s.state.intents[0].intent_id,
        "plan",
        conditions,
    )


def finish_plan(s):
    s.commit(s.work.complete_plan("plan"))


def test_all_conditions_verified_without_auto_fact_or_intent_commit(scenario):
    s, files, store, verifier = scenario
    original = binding(
        s,
        OutputContains(0, "stdout", "observed"),
        FileSha256(0, "/workspace/work/result", files.result.sha256),
    )
    verifier.bind(original)
    assert store.read(original.intent_id) == original
    s.call()
    finish_plan(s)
    assert verifier.verify(original.intent_id) is CheckStatus.SATISFIED
    assert verifier.verify(original.intent_id) is CheckStatus.SATISFIED
    assert files.calls == 1
    assert not s.state.facts


def test_known_mismatch_and_truncated_unknown_are_separate(scenario):
    s, files, store, verifier = scenario
    original = binding(s, OutputContains(0, "stdout", "absent"))
    verifier.bind(original)
    s.call()
    finish_plan(s)
    assert verifier.verify(original.intent_id) is CheckStatus.NOT_SATISFIED


def test_truncated_output_without_literal_is_inconclusive(scenario):
    s, _, _, verifier = scenario
    original = binding(s, OutputContains(0, "stdout", "absent"))
    verifier.bind(original)
    s.commands.stdout_truncated = True
    s.call()
    finish_plan(s)
    assert verifier.verify(original.intent_id) is CheckStatus.INCONCLUSIVE


def test_binding_must_precede_execution_and_cannot_change(scenario):
    s, _, store, verifier = scenario
    original = binding(s, OutputContains(0, "stdout", "observed"))
    verifier.bind(original)
    with pytest.raises(T6EvidenceError, match="immutable_evidence_conflict"):
        verifier.bind(
            replace(original, conditions=(OutputContains(0, "stdout", "altered"),))
        )
    s.call()
    with pytest.raises(ValueError, match="precede_execution"):
        verifier.bind(original)


def test_missing_execution_is_inconclusive_not_failure(scenario):
    s, _, _, verifier = scenario
    original = binding(s, OutputContains(0, "stdout", "observed"))
    verifier.bind(original)
    assert verifier.verify(original.intent_id) is CheckStatus.INCONCLUSIVE


def test_file_digest_missing_is_definite_but_unknown_stays_inconclusive(scenario):
    s, files, _, verifier = scenario
    original = binding(s, FileSha256(0, "/workspace/work/result", files.result.sha256))
    verifier.bind(original)
    s.call()
    finish_plan(s)
    files.result = FileDigestObservation("missing")
    assert verifier.verify(original.intent_id) is CheckStatus.NOT_SATISFIED
    assert files.calls == 1


def test_file_digest_claim_without_result_never_rehashes(scenario):
    s, files, store, verifier = scenario
    original = binding(s, FileSha256(0, "/workspace/work/result", files.result.sha256))
    verifier.bind(original)
    s.call()
    finish_plan(s)

    def lost_result():
        files.calls += 1
        raise TimeoutError("docker result lost")

    with pytest.raises(TimeoutError):
        store.observe_file(original, 0, "/workspace/work/result", lost_result)
    assert verifier.verify(original.intent_id) is CheckStatus.INCONCLUSIVE
    assert files.calls == 1
