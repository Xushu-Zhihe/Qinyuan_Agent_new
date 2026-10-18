"""T6 model actions preserve evidence and identities across a process crash."""

import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_adapters.action_evidence_store import T6ActionEvidenceStore
from agent_core.command_contracts import T6EvidenceError
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.sqlite_control_store import CURRENT_SCHEMA_VERSION, SqliteControlStore
from agent_core.action_audit import (
    ActionAuditConflict,
    ActionInvocationStart,
    ActionRecoveryRequired,
    ActionResourceIds,
    T6ActionJournal,
)
from agent_core.actions import CandidateFlagAction, ProbeAction
from test_control_store import make_start_mutation
from test_command_gateway import Scenario


@pytest.fixture
def scenario(tmp_path):
    current = Scenario(tmp_path)
    try:
        yield current
    finally:
        current.close()


def start_for(s, **changes):
    return replace(
        ActionInvocationStart(
            str(uuid4()), s.state.task_run.task_run_id,
            s.state.intents[0].intent_id, s.state.latest_checkpoint.sequence,
            "a" * 64, ActionResourceIds.create(), s.clock.now,
        ),
        **changes,
    )


def journal_for(s, root):
    return T6ActionJournal(s.store, T6ActionEvidenceStore(root / "actions"))


def test_saved_result_replays_without_script_or_candidate_in_database(scenario, tmp_path):
    s = scenario
    journal = journal_for(s, tmp_path)
    start = start_for(s)
    action = ProbeAction("printf private-command", b"private-input", "/workspace/work", 30, ())
    assert journal.begin(start) is None
    recorded = journal.save_result(start, s.clock.now + 1, "action", action)
    assert recorded.completion is not None
    assert journal.begin(start) == recorded

    restarted = journal_for(s, tmp_path)
    assert restarted.resume_unapplied(start.task_run_id, start.intent_id) == (recorded, action)
    with sqlite3.connect(s.store.path) as connection:
        row = connection.execute(
            "SELECT resource_ids_json, result_sha256 FROM t6_action_invocations"
        ).fetchone()
        assert row is not None
        assert json.loads(row[0])["plan_id"] == start.resource_ids.plan_id
        assert row[1] == recorded.completion.result_sha256
        assert "private-command" not in " ".join(connection.iterdump())
        assert "private-input" not in " ".join(connection.iterdump())
    applied = restarted.mark_applied(start.model_request_id, s.clock.now + 2)
    assert restarted.resume_unapplied(start.task_run_id, start.intent_id) is None
    assert restarted.mark_applied(start.model_request_id, s.clock.now + 2) == applied


def test_started_call_without_result_requires_explicit_recovery(scenario, tmp_path):
    s = scenario
    journal = journal_for(s, tmp_path)
    start = start_for(s)
    journal.begin(start)
    with pytest.raises(ActionRecoveryRequired, match="no complete evidence"):
        journal_for(s, tmp_path).resume_unapplied(start.task_run_id, start.intent_id)
    with pytest.raises(ActionRecoveryRequired, match="unapplied"):
        journal.begin(start_for(s))


def test_evidence_written_before_sqlite_completion_is_recovered_once(scenario, tmp_path):
    s = scenario
    evidence = T6ActionEvidenceStore(tmp_path / "actions")
    journal = T6ActionJournal(s.store, evidence)
    start = start_for(s)
    action = CandidateFlagAction("flag{pending}", "execution-1")
    journal.begin(start)
    digest = evidence.save(start.model_request_id, s.clock.now + 1, "action", action)
    record, replay = journal_for(s, tmp_path).resume_unapplied(
        start.task_run_id, start.intent_id
    )
    assert replay == action
    assert record.completion is not None
    assert record.completion.result_sha256 == digest
    assert journal_for(s, tmp_path).resume_unapplied(
        start.task_run_id, start.intent_id
    ) == (record, action)
    with sqlite3.connect(s.store.path) as connection:
        assert "flag{pending}" not in " ".join(connection.iterdump())


def test_changed_identity_or_evidence_cannot_be_replayed(scenario, tmp_path):
    s = scenario
    journal = journal_for(s, tmp_path)
    start = start_for(s)
    action = ProbeAction("pwd", b"", "/workspace/work", 30, ())
    journal.begin(start)
    with pytest.raises(ActionAuditConflict, match="different action fields"):
        journal.begin(replace(start, resource_ids=ActionResourceIds.create()))
    journal.save_result(start, s.clock.now + 1, "action", action)
    path = tmp_path / "actions" / f"model-action-{start.model_request_id}.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(T6EvidenceError, match="evidence_read_failed"):
        journal.resume_unapplied(start.task_run_id, start.intent_id)


def test_one_structural_correction_and_model_error_stop(scenario, tmp_path):
    s = scenario
    journal = journal_for(s, tmp_path)
    first = start_for(s)
    journal.begin(first)
    journal.save_result(
        first, s.clock.now + 1, "invalid_structure",
        ModelError(ModelErrorKind.INVALID_RESPONSE, "bad fields"),
    )
    journal.mark_applied(first.model_request_id, s.clock.now + 2)
    with pytest.raises(ActionAuditConflict, match="only one matching"):
        journal.begin(start_for(s))
    correction = start_for(s, correction_of_model_request_id=first.model_request_id)
    journal.begin(correction)
    journal.save_result(
        correction, s.clock.now + 3, "model_error",
        ModelError(ModelErrorKind.PROVIDER_FAILURE, "provider stopped"),
    )
    journal.mark_applied(correction.model_request_id, s.clock.now + 4)
    with pytest.raises(ActionAuditConflict, match="prior model error"):
        journal.begin(start_for(s))


def test_resource_id_cannot_be_reused_across_model_actions(scenario, tmp_path):
    s = scenario
    journal = journal_for(s, tmp_path)
    first = start_for(s)
    journal.begin(first)
    journal.save_result(first, s.clock.now + 1, "action", ProbeAction(
        "pwd", b"", "/workspace/work", 30, ()
    ))
    journal.mark_applied(first.model_request_id, s.clock.now + 2)
    second = start_for(s)
    reused = replace(
        second.resource_ids, candidate_id=first.resource_ids.command_request_id
    )
    with pytest.raises(ActionAuditConflict, match="already claimed"):
        journal.begin(replace(second, resource_ids=reused))
    assert len(s.store.list_t6_actions(first.task_run_id, first.intent_id)) == 1


def test_schema_9_upgrades_without_changing_existing_task(tmp_path):
    path = tmp_path / "control.sqlite"
    with SqliteControlStore.open(path) as store:
        store.commit(make_start_mutation())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE t6_action_resource_ids")
        connection.execute("DROP TABLE t6_action_invocations")
        connection.execute("DELETE FROM schema_versions WHERE version = 10")
    with SqliteControlStore.open(path) as upgraded:
        assert upgraded.load_task_run("task-run-1") is not None
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT MAX(version) FROM schema_versions").fetchone() == (CURRENT_SCHEMA_VERSION,)
