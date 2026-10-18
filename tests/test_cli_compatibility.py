from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import cli, task_cli, task_result
from agent_adapters.run_files import immutable_write, run_path
from agent_core.run_outcome import (
    D7OperationResult,
    D7RunOutcomeState,
    map_operation_exit,
    map_run_outcome,
)


@pytest.mark.parametrize("version", [True, "2", 4, 0, None])
def test_persisted_unsupported_protocol_is_rejected_at_all_cli_boundaries(
    tmp_path, version
):
    declaration = {"protocol_version": version}

    with pytest.raises(ValueError, match="unsupported_persisted_protocol_version"):
        cli._snapshot(tmp_path, declaration)
    with pytest.raises(ValueError, match="unsupported_persisted_protocol_version"):
        cli._worker(None, None, "", declaration, "resume", None, None, "out.json")


@pytest.mark.parametrize("version", [1, 2])
def test_snapshot_keeps_legacy_protocol_dispatch(version, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "snapshot_result", lambda *_: {"reader": "v1"})
    monkeypatch.setattr(
        task_result, "snapshot_task_result", lambda *_: {"reader": "v2"}
    )

    result = cli._snapshot(
        tmp_path, {"protocol_version": version, "task_run_id": str(uuid4())}
    )

    assert result == {"reader": "v1" if version == 1 else "v2"}


@pytest.mark.parametrize("version", [True, "2", 4, 0, None])
def test_existing_run_dispatch_rejects_unsupported_persisted_protocol(
    tmp_path, version
):
    task_run_id = str(uuid4())
    state_dir = tmp_path / "state"
    directory = run_path(state_dir, task_run_id)
    directory.mkdir(parents=True)
    immutable_write(
        directory / "run.json",
        {"protocol_version": version, "task_run_id": task_run_id},
    )
    args = SimpleNamespace(
        command="status", task_run_id=task_run_id, state_dir=str(state_dir)
    )

    with pytest.raises(ValueError, match="unsupported_persisted_protocol_version"):
        cli.dispatch(args)


def test_missing_persisted_protocol_is_rejected(tmp_path):
    task_run_id = str(uuid4())
    state_dir = tmp_path / "state"
    directory = run_path(state_dir, task_run_id)
    directory.mkdir(parents=True)
    immutable_write(directory / "run.json", {"task_run_id": task_run_id})
    args = SimpleNamespace(
        command="status", task_run_id=task_run_id, state_dir=str(state_dir)
    )

    with pytest.raises(ValueError, match="unsupported_persisted_protocol_version"):
        cli.dispatch(args)


def test_existing_run_dispatch_preserves_v1_and_v2_routes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "_snapshot",
        lambda _directory, declaration: {
            "route": "v1", "task_run_id": declaration["task_run_id"]
        },
    )
    monkeypatch.setattr(
        task_cli,
        "dispatch_existing",
        lambda _args, _directory, declaration: {
            "route": "v2", "task_run_id": declaration["task_run_id"]
        },
    )

    for version in (1, 2):
        task_run_id = str(uuid4())
        state_dir = tmp_path / f"state-{version}"
        directory = run_path(state_dir, task_run_id)
        directory.mkdir(parents=True)
        immutable_write(
            directory / "run.json",
            {"protocol_version": version, "task_run_id": task_run_id},
        )
        args = SimpleNamespace(
            command="status", task_run_id=task_run_id, state_dir=str(state_dir)
        )

        assert cli.dispatch(args) == {
            "route": f"v{version}", "task_run_id": task_run_id
        }


@pytest.mark.parametrize(
    ("version", "error"),
    [(3, "protocol_v3_not_supported"), (1, "unsupported_protocol_version"),
     (4, "unsupported_protocol_version"), (True, "unsupported_protocol_version")],
)
def test_run_task_rejects_unassembled_protocol_without_legacy_fallback(
    version, error, monkeypatch
):
    monkeypatch.setattr(task_cli, "_input", lambda _path: {"protocol_version": version})

    with pytest.raises(ValueError, match=error):
        task_cli.dispatch_run_task(
            SimpleNamespace(request="request.json", mode="simulated")
        )


def test_v3_resume_routes_known_d3_operation_recovery_to_d3_reconciliation(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    directory = tmp_path / task_run_id
    directory.mkdir()
    declaration = {"protocol_version": 3, "task_run_id": task_run_id}
    projection = {
        "task_run_id": task_run_id,
        "task_run": {"status": "running"},
        "recovery": {"required": True, "reason": "operation_or_evidence_unknown"},
        "errors": [],
    }
    monkeypatch.setattr(
        "agent_adapters.web.task_result.snapshot_v3_task_result",
        lambda *_: projection,
    )
    calls = []

    def resume(_directory, _declaration, *, operation, events):
        calls.append((operation, events))
        return projection | {
            "recovery": {"required": True, "reason": "operation_or_evidence_unknown"}
        }

    monkeypatch.setattr(task_cli, "_run_v3_simulated_operation", resume)
    result = task_cli.dispatch_v3_existing(
        SimpleNamespace(
            command="resume", task_run_id=task_run_id,
            cleanup_only=False, events=None,
        ),
        directory,
        declaration,
    )

    assert calls == [("resume", None)]
    assert result["recovery"]["required"] is True
    assert result["exit_code"] == 5


@pytest.mark.parametrize(
    "reason",
    [
        "external_validation_check_outcome_unknown",
        "external_feedback_reconciliation_required",
        "unclassified_recovery_reason",
    ],
)
def test_v3_resume_does_not_dispatch_external_or_unclassified_recovery(
    reason, tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    directory = tmp_path / task_run_id
    directory.mkdir()
    declaration = {"protocol_version": 3, "task_run_id": task_run_id}
    projection = {
        "task_run_id": task_run_id,
        "task_run": {"status": "running"},
        "recovery": {"required": True, "reason": reason},
        "errors": [],
    }
    monkeypatch.setattr(
        "agent_adapters.web.task_result.snapshot_v3_task_result",
        lambda *_: projection,
    )
    monkeypatch.setattr(
        task_cli,
        "_run_v3_simulated_operation",
        lambda *_args, **_kwargs: pytest.fail("unsafe resume dispatch"),
    )

    result = task_cli.dispatch_v3_existing(
        SimpleNamespace(
            command="resume", task_run_id=task_run_id,
            cleanup_only=False, events=None,
        ),
        directory,
        declaration,
    )

    assert result["errors"] == [{"code": "recovery_required"}]
    assert result["recovery"]["reason"] == reason
    assert result["exit_code"] == 5


@pytest.mark.parametrize(
    "external_state",
    [
        {
            "answer_candidates": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "validation_state": "unknown", "feedback": [],
            }],
        },
        {
            "answer_candidates": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "validation_state": "conflict", "feedback": [],
            }],
        },
        {
            "answer_candidates": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "validation_state": "pending", "feedback": [{
                    "operation_id": "operation", "verdict": "unknown",
                    "feedback_kind": "submission", "resolves_operation_id": None,
                }],
            }],
        },
        {
            "answer_candidates": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "validation_state": "pending", "feedback": [],
            }],
            "external_validation_feedback": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "operation_id": "operation", "verdict": "unknown",
            }],
        },
        {
            "task_run": {"status": "awaiting_external_feedback"},
            "answer_candidates": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "validation_state": "pending", "feedback": [],
            }],
        },
        {
            "external_validation_checks": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "submission_operation_id": "operation", "status": "started",
                "verdict": None,
            }],
        },
        {
            "answer_candidates": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "validation_state": "pending", "feedback": [],
            }],
            "external_validation_checks": [{
                "candidate_id": "candidate", "evidence_version": "v1",
                "submission_operation_id": "operation", "status": "completed",
                "verdict": "unknown",
            }],
        },
    ],
)
def test_v3_resume_rechecks_external_ledgers_before_d3_reconciliation(
    external_state, tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    directory = tmp_path / task_run_id
    directory.mkdir()
    declaration = {"protocol_version": 3, "task_run_id": task_run_id}
    projection = {
        "task_run_id": task_run_id,
        "task_run": {"status": "running"},
        "recovery": {"required": True, "reason": "operation_or_evidence_unknown"},
        "errors": [],
        **external_state,
    }
    monkeypatch.setattr(
        "agent_adapters.web.task_result.snapshot_v3_task_result",
        lambda *_: projection,
    )
    monkeypatch.setattr(
        task_cli,
        "_run_v3_simulated_operation",
        lambda *_args, **_kwargs: pytest.fail("external feedback must stay read-only"),
    )

    result = task_cli.dispatch_v3_existing(
        SimpleNamespace(
            command="resume", task_run_id=task_run_id,
            cleanup_only=False, events=None,
        ),
        directory,
        declaration,
    )

    assert result["errors"] == [{"code": "recovery_required"}]
    assert result["exit_code"] == 5


def test_v3_resume_allows_d3_reconciliation_after_unknown_feedback_is_resolved(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    directory = tmp_path / task_run_id
    directory.mkdir()
    declaration = {"protocol_version": 3, "task_run_id": task_run_id}
    projection = {
        "task_run_id": task_run_id,
        "task_run": {"status": "running"},
        "recovery": {"required": True, "reason": "operation_or_evidence_unknown"},
        "answer_candidates": [{
            "candidate_id": "candidate", "evidence_version": "v1",
            "validation_state": "accepted", "feedback": [
                {
                    "operation_id": "submission", "verdict": "unknown",
                    "feedback_kind": "submission", "resolves_operation_id": None,
                },
                {
                    "operation_id": "query", "verdict": "accepted",
                    "feedback_kind": "reconciliation", "resolves_operation_id": "submission",
                },
            ],
        }],
        "external_validation_checks": [{
            "candidate_id": "candidate", "evidence_version": "v1",
            "submission_operation_id": "submission", "status": "completed",
            "verdict": "unknown",
        }],
    }
    monkeypatch.setattr(
        "agent_adapters.web.task_result.snapshot_v3_task_result",
        lambda *_: projection,
    )
    calls = []

    def resume(_directory, _declaration, *, operation, events):
        calls.append((operation, events))
        return projection

    monkeypatch.setattr(task_cli, "_run_v3_simulated_operation", resume)
    task_cli.dispatch_v3_existing(
        SimpleNamespace(
            command="resume", task_run_id=task_run_id,
            cleanup_only=False, events=None,
        ),
        directory,
        declaration,
    )

    assert calls == [("resume", None)]


def test_v3_status_reports_successful_query_for_unresolved_recovery(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    directory = tmp_path / task_run_id
    directory.mkdir()
    declaration = {"protocol_version": 3, "task_run_id": task_run_id}
    projection = {
        "task_run_id": task_run_id,
        "task_run": {"status": "running"},
        "recovery": {"required": True, "reason": "external_validation_check_outcome_unknown"},
        "errors": [],
    }
    monkeypatch.setattr(
        "agent_adapters.web.task_result.snapshot_v3_task_result",
        lambda *_: projection,
    )

    result = task_cli.dispatch_v3_existing(
        SimpleNamespace(command="status", task_run_id=task_run_id),
        directory,
        declaration,
    )

    assert result["recovery"]["required"] is True
    assert result["exit_code"] == 0


def test_v3_request_has_stable_input_rejection_exit_code(
    monkeypatch, capsys
):
    monkeypatch.setattr(task_cli, "_input", lambda _path: {"protocol_version": 3})

    exit_code = cli.main(
        [
            "run-task", "--request", "request.json", "--mode", "simulated",
            "--state-dir", "state",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert result["exit_code"] == 2
    assert result["errors"] == [{"code": "protocol_v3_not_supported"}]
    assert result["task_run_id"] is None


@pytest.mark.parametrize("operation", ["status", "replay", "cancel", "cleanup"])
def test_successful_read_or_lifecycle_operation_preserves_run_exit_code(operation):
    result = D7OperationResult(operation, "completed", run_exit_code=5)

    assert map_operation_exit(result).to_dict() == {
        "exit_code": 0,
        "run_exit_code": 5,
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("unknown", {"exit_code": 5, "run_exit_code": 5}),
        ("refused", {"exit_code": 2, "run_exit_code": 4}),
        ("export_failed", {"exit_code": 3, "run_exit_code": 4}),
    ],
)
def test_failed_operation_does_not_replace_persisted_run_outcome(state, expected):
    result = D7OperationResult("status" if state != "export_failed" else "export", state,
                               run_exit_code=expected["run_exit_code"])

    assert map_operation_exit(result).to_dict() == expected


@pytest.mark.parametrize(
    ("goal_evaluation", "cleanup_state", "unknown", "expected"),
    [
        ("accepted", "confirmed", False, 0),
        ("accepted", "unconfirmed", False, 5),
        ("rejected", "confirmed", False, 5),
        ("accepted", "confirmed", True, 5),
    ],
)
def test_goal_success_requires_accepted_evidence_and_confirmed_cleanup(
    goal_evaluation, cleanup_state, unknown, expected
):
    outcome = map_run_outcome(
        D7RunOutcomeState(
            task_run_status="achieved",
            goal_evaluation=goal_evaluation,
            cleanup_state=cleanup_state,
            unresolved_operation_unknown=unknown,
        )
    )

    assert outcome.run_exit_code == expected
