"""Public run-task v2 commands stay separate from run-intent v1."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from test_task_application import fixture, request

from agent_adapters import task_cli
from agent_adapters.run_files import immutable_write
from agent_adapters.task_result import snapshot_task_result

ENTRY = Path(__file__).resolve().parents[1] / "agent.py"


def _call(cwd: Path, *args: str):
    completed = subprocess.run(
        [sys.executable, str(ENTRY), *args], cwd=cwd,
        capture_output=True, text=True, timeout=35, check=False,
    )
    return completed.returncode, json.loads(completed.stdout)


def test_run_task_status_replay_and_cancel(tmp_path):
    (tmp_path / "sample.bin").write_bytes(b"binary sample")
    (tmp_path / "request.json").write_text(json.dumps(request()), encoding="utf-8")
    (tmp_path / "fixture.json").write_text(json.dumps(fixture()), encoding="utf-8")
    state = tmp_path / "state"
    status, created = _call(
        tmp_path, "run-task", "--request", "request.json", "--mode", "simulated",
        "--fixture", "fixture.json", "--state-dir", str(state),
    )
    assert status == 4
    assert created["protocol_version"] == 2
    assert created["stop"]["reason"] == "fixture_exhausted"
    assert len(created["verified_facts"]) == 1
    task_id = created["task_run_id"]
    status, saved = _call(tmp_path, "status", "--task-run-id", task_id,
                          "--state-dir", str(state))
    assert status == 4
    assert saved["task_run"]["status"] == "running"
    status, replay = _call(tmp_path, "replay", "--task-run-id", task_id,
                           "--state-dir", str(state))
    assert status == 0 and replay["events"]
    status, cancelled = _call(tmp_path, "cancel", "--task-run-id", task_id,
                              "--state-dir", str(state))
    assert status == 130
    assert cancelled["cleanup"]["status"] == "succeeded"


def test_simulated_fixture_can_append_a_turn_on_explicit_resume(tmp_path):
    (tmp_path / "sample.bin").write_bytes(b"binary sample")
    (tmp_path / "request.json").write_text(json.dumps(request()), encoding="utf-8")
    first = fixture()
    (tmp_path / "fixture.json").write_text(json.dumps(first), encoding="utf-8")
    state = tmp_path / "state"
    status, created = _call(
        tmp_path, "run-task", "--request", "request.json", "--mode", "simulated",
        "--fixture", "fixture.json", "--state-dir", str(state),
    )
    assert status == 4
    extended = fixture()
    extended["turns"].append({"candidates": [{"description": "inspect once more"}]})
    extended["actions"].extend(first["actions"])
    extended["commands"].extend(first["commands"])
    (tmp_path / "extended.json").write_text(json.dumps(extended), encoding="utf-8")
    (tmp_path / "authorization.json").write_text(
        json.dumps(request()["authorization"]), encoding="utf-8",
    )
    status, resumed = _call(
        tmp_path, "resume", "--task-run-id", created["task_run_id"],
        "--state-dir", str(state), "--authorization", "authorization.json",
        "--fixture", "extended.json",
    )
    assert status == 4
    assert len(resumed["turns"]) == 2
    assert len(resumed["verified_facts"]) == 2
    assert resumed["stop"]["reason"] == "fixture_exhausted"


def test_run_task_rejects_extra_authorization_before_creating_a_run(tmp_path):
    (tmp_path / "sample.bin").write_bytes(b"binary sample")
    raw = request()
    raw["authorization"]["allowed_target_urls"] = ["https://not-authorized.example"]
    (tmp_path / "request.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "fixture.json").write_text(json.dumps(fixture()), encoding="utf-8")
    state = tmp_path / "state"
    status, result = _call(
        tmp_path, "run-task", "--request", "request.json", "--mode", "simulated",
        "--fixture", "fixture.json", "--state-dir", str(state),
    )
    assert status != 0
    assert result["errors"]
    assert not state.exists() or not list(state.iterdir())


def test_simulated_remote_target_runs_without_attachment(tmp_path):
    raw = request()
    raw["task_request"]["target"] = {
        "attachments": [], "endpoints": [{"url": "tcp://example.org:31337"}],
    }
    raw["authorization"]["allowed_target_urls"] = ["tcp://example.org:31337"]
    (tmp_path / "request.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "fixture.json").write_text(json.dumps(fixture()), encoding="utf-8")
    status, result = _call(
        tmp_path, "run-task", "--request", "request.json", "--mode", "simulated",
        "--fixture", "fixture.json", "--state-dir", str(tmp_path / "state"),
    )
    assert status == 4
    assert result["task_run"]["status"] == "running"
    assert result["overall_goal"]["outcome"] == "not_evaluated"


def test_unknown_target_network_creation_does_not_claim_cleanup(tmp_path):
    task_id = "3f80f2be-0ac7-4d21-9efe-bf56fe735852"
    immutable_write(tmp_path / "start-failure.json", {
        "code": "target_network_error", "phase": "create", "outcome": "outcome_unknown",
    })
    result = snapshot_task_result(tmp_path, {"task_run_id": task_id, "mode": "real"})
    assert result["exit_code"] == 5
    assert result["recovery"] == {"required": True, "reason": "target_network_outcome_unknown"}
    assert result["cleanup"]["status"] == "not_confirmed"


def test_cancel_waits_for_running_worker_cleanup(tmp_path, monkeypatch):
    snapshots = iter((
        {"task_run": {"status": "running"}, "cleanup": {"status": "not_confirmed"},
         "exit_code": 4},
        {"task_run": {"status": "cancelled"}, "cleanup": {"status": "succeeded"},
         "exit_code": 130},
    ))

    @contextmanager
    def busy(_directory):
        raise ValueError("task_run_busy")
        yield

    monkeypatch.setattr(task_cli, "snapshot_task_result", lambda *_: next(snapshots))
    monkeypatch.setattr(task_cli, "run_lock", busy)
    declaration = {"task_run_id": "example"}
    args = SimpleNamespace(command="cancel")
    result = task_cli.dispatch_existing(args, tmp_path, declaration)
    assert result["exit_code"] == 130
    assert (tmp_path / "cancel-request.json").exists()
