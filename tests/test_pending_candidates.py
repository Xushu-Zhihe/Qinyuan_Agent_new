"""Pending candidates never imply validated answers or TaskRun success."""

from dataclasses import asdict
from uuid import uuid4

import pytest

from agent_adapters.pending_candidate_store import PendingCandidateStore
from agent_core.pending_candidates import (
    CandidateEvidenceError,
    MAX_CANDIDATE_CONTEXT_BYTES,
    PendingCandidateRecorder,
    grouped_pending_candidates,
    recent_pending_candidate_context,
)
from agent_core.control_store import canonical_json
from agent_core.actions import CandidateFlagAction
from agent_core.runtime import TaskRunStatus
from test_command_gateway import Scenario


@pytest.fixture
def s(tmp_path):
    scenario = Scenario(tmp_path)
    try:
        yield scenario
    finally:
        scenario.close()


def test_pending_candidate_is_immutable_and_display_groups_exact_value(s, tmp_path):
    result = s.call()
    store = PendingCandidateStore(tmp_path / "pending")
    recorder = PendingCandidateRecorder(store, s.recorder)
    action = CandidateFlagAction("flag{candidate}", result.record.request_id)
    now = s.clock.now
    first = recorder.record(
        s.state, s.state.intents[0].intent_id, action, str(uuid4()), now
    )
    assert (
        recorder.record(s.state, first.intent_id, action, first.candidate_id, now)
        == first
    )
    second = recorder.record(s.state, first.intent_id, action, str(uuid4()), now + 0.01)
    assert len(store.list_for_task(first.task_run_id)) == 2
    assert grouped_pending_candidates((first, second)) == (
        ("flag{candidate}", (result.record.request_id,)),
    )
    assert s.state.task_run.status is TaskRunStatus.RUNNING
    assert not s.state.facts
    context = recent_pending_candidate_context(
        s.state, first.intent_id, store, s.recorder
    )
    assert [entry.value for entry in context.entries] == [
        "flag{candidate}", "flag{candidate}"
    ]
    assert all(entry.verification_status == "pending" for entry in context.entries)
    assert context.history_clipped is False
    with pytest.raises(CandidateEvidenceError, match="candidate_id_conflict"):
        recorder.record(
            s.state,
            first.intent_id,
            CandidateFlagAction("flag{other}", result.record.request_id),
            first.candidate_id,
            now,
        )


def test_candidate_requires_completed_execution_and_intact_evidence(s, tmp_path):
    store = PendingCandidateStore(tmp_path / "pending")
    recorder = PendingCandidateRecorder(store, s.recorder)
    intent_id = s.state.intents[0].intent_id
    with pytest.raises(CandidateEvidenceError, match="scope_mismatch"):
        recorder.record(
            s.state,
            intent_id,
            CandidateFlagAction("x", str(uuid4())),
            str(uuid4()),
            s.clock.now,
        )
    result = s.call()
    path = s.recorder._path("finished", result.record.request_id)
    raw = path.read_bytes()
    try:
        path.write_bytes(b"corrupt")
        with pytest.raises(CandidateEvidenceError, match="unverified"):
            recorder.record(
                s.state,
                intent_id,
                CandidateFlagAction("x", result.record.request_id),
                str(uuid4()),
                s.clock.now,
            )
        assert not store.list_for_task(s.state.task_run.task_run_id)
    finally:
        path.write_bytes(raw)


def test_recent_candidates_use_digest_when_full_values_exceed_context_budget(s, tmp_path):
    result = s.call()
    store = PendingCandidateStore(tmp_path / "pending")
    recorder = PendingCandidateRecorder(store, s.recorder)
    intent_id = s.state.intents[0].intent_id
    for index in range(9):
        recorder.record(
            s.state, intent_id,
            CandidateFlagAction(f"{index}-" + "x" * 3900, result.record.request_id),
            str(uuid4()), s.clock.now + index,
        )
    context = recent_pending_candidate_context(s.state, intent_id, store, s.recorder)
    assert len(context.entries) == 8
    assert context.history_clipped is True
    assert context.entries[-1].value is not None
    assert any(item.value is None for item in context.entries)
    assert all(len(item.value_sha256) == 64 for item in context.entries)
    assert len(canonical_json(asdict(context)).encode("utf-8")) <= (
        MAX_CANDIDATE_CONTEXT_BYTES
    )
    assert len(store.list_for_task(s.state.task_run.task_run_id)) == 9


def test_candidate_context_requires_intact_source_receipt(s, tmp_path):
    result = s.call()
    store = PendingCandidateStore(tmp_path / "pending")
    recorder = PendingCandidateRecorder(store, s.recorder)
    intent_id = s.state.intents[0].intent_id
    recorder.record(
        s.state, intent_id,
        CandidateFlagAction("flag{pending}", result.record.request_id),
        str(uuid4()), s.clock.now,
    )
    path = s.recorder._path("finished", result.record.request_id)
    original = path.read_bytes()
    try:
        path.write_bytes(b"invalid")
        with pytest.raises(CandidateEvidenceError, match="unverified"):
            recent_pending_candidate_context(s.state, intent_id, store, s.recorder)
    finally:
        path.write_bytes(original)
