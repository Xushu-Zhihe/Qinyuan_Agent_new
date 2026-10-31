"""Workbench run state must not hide unresolved Agent work."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from agent_adapters.run_files import immutable_read, immutable_write
from agent_adapters.workbench import Workbench, WorkbenchError
from agent_adapters.workbench_runner import ActiveRun, stderr_reports_failure


class FakeProcess:
    def __init__(self, result=None):
        self.result = result
        self.returncode = None if result is None else result["exit_code"]

    def poll(self):
        return self.returncode

    def communicate(self):
        return (json.dumps(self.result) if self.result else ""), ""


class NoThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        pass


def prepared_workbench(root: Path):
    workbench = Workbench(root)
    workbench.configure_model({
        "provider": "deepseek",
        "model": "test-model",
        "base_url": "https://api.deepseek.com",
        "api_key": "test-only-key",
    })
    challenge = workbench.create({
        "title": "one",
        "category": "misc",
        "description": "test question",
        "endpoint": "",
        "deadline_minutes": 60,
        "step_timeout_seconds": 60,
    })
    workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"test"), 4)
    return workbench, challenge["id"]


def test_start_exposes_running_status_and_cancel_does_not_spawn_twice(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    processes = []

    def fake_popen(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    with patch("agent_adapters.workbench_runner.subprocess.Popen", side_effect=fake_popen), patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        run = workbench.runner.start(challenge_id)
        assert run["status"] == workbench.get(challenge_id)["status"] == "running"
        request = json.loads((Path(run["directory"]) / "request.json").read_text(encoding="utf-8"))
        assert "题型（任务提供）：misc" in request["task_request"]["goal"]
        assert workbench.runner.cancel(challenge_id)["status"] == "cancelling"
        with pytest.raises(WorkbenchError, match="challenge_already_running"):
            workbench.runner.start(challenge_id)
    assert len(processes) == 1


def test_running_cancel_writes_cli_readable_request(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()), patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        run = workbench.runner.start(challenge_id)
        task_id = str(uuid4())
        agent_directory = Path(run["directory"]) / "agent-state" / task_id
        agent_directory.mkdir()
        (agent_directory / "run.json").write_text("{}", encoding="utf-8")
        workbench.runner.cancel(challenge_id)
        workbench.runner.cancel(challenge_id)
    assert immutable_read(agent_directory / "cancel-request.json") == {"task_run_id": task_id}


def test_progress_during_cli_startup_does_not_require_recovery(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id, task_id = str(uuid4()), str(uuid4())
    parent = workbench.runner.root / run_id
    agent_directory = parent / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    (agent_directory / "run.json").write_text("{}", encoding="utf-8")
    workbench._insert_run(run_id, challenge_id, parent)
    workbench._started_run(run_id)
    assert workbench.runner.progress(challenge_id)["progress"] is None


def test_deadline_result_is_not_reported_as_generic_failure(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id = str(uuid4())
    directory = workbench.runner.root / run_id
    directory.mkdir()
    workbench._insert_run(run_id, challenge_id, directory)
    workbench._started_run(run_id)
    result = {"protocol_version": 2, "task_run_id": str(uuid4()),
              "task_run": {"status": "deadline_exceeded"},
              "cleanup": {"status": "succeeded"}, "recovery": {"required": False},
              "exit_code": 3}
    workbench.runner._active[run_id] = ActiveRun(
        run_id, challenge_id, directory, FakeProcess(result)
    )
    workbench.runner._watch(run_id)
    assert workbench.runner.status(challenge_id)["status"] == "deadline_exceeded"


@pytest.mark.parametrize("exit_code", [4, 5])
def test_unknown_or_cleanup_unconfirmed_cannot_be_archived(tmp_path, exit_code):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id = str(uuid4())
    run_directory = workbench.runner.root / run_id
    run_directory.mkdir()
    workbench._insert_run(run_id, challenge_id, run_directory)
    workbench._started_run(run_id)
    result = {
        "protocol_version": 1,
        "task_run_id": str(uuid4()),
        "task_run": {"status": "running" if exit_code == 4 else "cancelled"},
        "cleanup": {"status": "unknown"},
        "errors": [{"code": "recovery_required"}],
        "exit_code": exit_code,
    }
    workbench.runner._active[run_id] = ActiveRun(
        run_id, challenge_id, run_directory, FakeProcess(result)
    )
    workbench.runner._watch(run_id)
    assert workbench.runner.status(challenge_id)["status"] == "recovery_required"
    with pytest.raises(WorkbenchError, match="cancel_running_challenge_first"):
        workbench.archive(challenge_id)


def test_terminal_recovery_uses_explicit_cleanup_only_before_archive(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id, task_id = str(uuid4()), str(uuid4())
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(agent_directory / "run.json", {"protocol_version": 2, "task_run_id": task_id})
    workbench._insert_run(run_id, challenge_id, directory)
    result = {
        "protocol_version": 2, "task_run_id": task_id,
        "task_run": {"status": "cancelled"},
        "cleanup": {"status": "not_confirmed"},
        "recovery": {"required": True}, "exit_code": 5,
    }
    workbench._finish_run(
        run_id, status="recovery_required", exit_code=5,
        result=result, agent_task_run_id=task_id,
    )
    with pytest.raises(WorkbenchError, match="cancel_not_available"):
        workbench.runner.cancel(challenge_id)
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        assert workbench.runner.cleanup(challenge_id)["status"] == "cleaning"
        with pytest.raises(WorkbenchError, match="cleanup_not_available"):
            workbench.runner.cleanup(challenge_id)
    assert popen.call_args.args[0][3:] == [
        "resume", "--task-run-id", task_id, "--state-dir",
        str(agent_directory.parent), "--cleanup-only", "--events",
        str(directory / "events.jsonl"),
    ]
    with pytest.raises(WorkbenchError, match="cancel_running_challenge_first"):
        workbench.archive(challenge_id)
    cleaned = result | {"cleanup": {"status": "succeeded"}, "exit_code": 5}
    process = workbench.runner._active[run_id].process
    process.result = cleaned
    process.returncode = 5
    (directory / "cleanup.stdout.log").write_text(json.dumps(cleaned), encoding="utf-8")
    workbench.runner._watch(run_id)
    assert workbench.runner.status(challenge_id)["status"] == "cleanup_confirmed"
    assert workbench.archive(challenge_id)["archived"] is True


def test_restart_does_not_acknowledge_cancel_without_owned_process(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id = str(uuid4())
    run_directory = workbench.runner.root / run_id
    agent_directory = run_directory / "agent-state" / str(uuid4())
    agent_directory.mkdir(parents=True)
    (agent_directory / "run.json").write_text("{}", encoding="utf-8")
    workbench._insert_run(run_id, challenge_id, run_directory)
    workbench._started_run(run_id)

    restarted = Workbench(tmp_path)
    with pytest.raises(WorkbenchError, match="run_process_not_owned"):
        restarted.runner.cancel(challenge_id)
    assert restarted.runner.status(challenge_id)["status"] == "running"
    assert not (agent_directory / "cancel-request.json").exists()


def test_no_agent_result_requires_recovery(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id = str(uuid4())
    run_directory = workbench.runner.root / run_id
    run_directory.mkdir()
    workbench._insert_run(run_id, challenge_id, run_directory)
    workbench._started_run(run_id)
    workbench.runner._active[run_id] = ActiveRun(
        run_id, challenge_id, run_directory, FakeProcess({"exit_code": 4})
    )
    workbench.runner._watch(run_id)
    assert workbench.runner.status(challenge_id)["status"] == "recovery_required"
    with pytest.raises(WorkbenchError, match="cancel_not_available"):
        workbench.runner.cancel(challenge_id)


def test_start_uses_v2_real_cli_and_independent_bash_budget(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    workbench.add_attachment(challenge_id, "second.bin", io.BytesIO(b"two"), 3)
    challenge = workbench.get(challenge_id)
    workbench.update(challenge_id, {key: challenge[key] for key in (
        "title", "category", "description", "endpoint", "deadline_minutes"
    )} | {"step_timeout_seconds": 120})
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        run = workbench.runner.start(challenge_id)
    args, kwargs = popen.call_args
    assert args[0][3:6] == ["run-task", "--mode", "real"]
    assert kwargs["env"]["QINYUAN_T6_STEP_TIMEOUT_SECONDS"] == "120"
    assert kwargs["env"]["QINYUAN_RUNTIME_MODEL_TIMEOUT"] == "180"
    request = json.loads((Path(run["directory"]) / "request.json").read_text())
    assert request["protocol_version"] == 2
    assert len(request["task_request"]["target"]["attachments"]) == 2
    assert request["authorization"] == {"allowed_tools": ["sandbox.command"], "allowed_target_urls": []}
    assert "test-only-key" not in json.dumps(request)
    assert kwargs["stdout"].name.endswith("agent.stdout.log")


def test_start_failure_is_durable_and_preflight_accepts_remote(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    with patch("agent_adapters.workbench_runner.subprocess.Popen", side_effect=OSError):
        with pytest.raises(WorkbenchError, match="agent_process_start_failed"):
            workbench.runner.start(challenge_id)
    assert workbench.runner.status(challenge_id)["status"] == "failed"
    assert not workbench.runner._active
    data = workbench.get(challenge_id)
    data["status"] = "draft"
    data["endpoint"] = "tcp://example.org:1234"
    workbench.runner._preflight(data)
    data["endpoint"] = ""
    data["attachments"][0]["byte_size"] = 64 * 1024 * 1024 + 1
    with pytest.raises(WorkbenchError, match="attachment_run_capacity_exceeded"):
        workbench.runner._preflight(data)


def test_remote_challenge_without_attachments_passes_exact_target(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    challenge = workbench.get(challenge_id)
    workbench.remove_attachment(challenge_id, challenge["attachments"][0]["id"])
    workbench.update(challenge_id, {
        "title": challenge["title"], "category": challenge["category"],
        "description": challenge["description"], "endpoint": "tcp://example.org:24009",
        "deadline_minutes": challenge["deadline_minutes"],
        "step_timeout_seconds": challenge["step_timeout_seconds"],
    })
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()), patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        run = workbench.runner.start(challenge_id)
    request = json.loads((Path(run["directory"]) / "request.json").read_text())
    assert request["task_request"]["target"] == {
        "attachments": [], "endpoints": [{"url": "tcp://example.org:24009"}],
    }
    assert request["authorization"]["allowed_target_urls"] == ["tcp://example.org:24009"]


def test_paused_v2_can_cancel_through_cli_without_resuming_model(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id, task_id = str(uuid4()), str(uuid4())
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(agent_directory / "run.json", {"protocol_version": 2, "task_run_id": task_id})
    workbench._insert_run(run_id, challenge_id, directory)
    result = {"protocol_version": 2, "task_run_id": task_id,
              "task_run": {"status": "running"}, "recovery": {"required": False},
              "exit_code": 4}
    workbench.runner._active[run_id] = ActiveRun(run_id, challenge_id, directory, FakeProcess(result))
    workbench.runner._watch(run_id)
    assert workbench.runner.status(challenge_id)["status"] == "paused"
    with pytest.raises(WorkbenchError, match="cancel_running_challenge_first"):
        workbench.archive(challenge_id)
    # Paused CLI exited, so the explicit cancel owns a new lifecycle process.
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        assert workbench.runner.cancel(challenge_id)["status"] == "cancelling"
        workbench.runner.cancel(challenge_id)
    assert popen.call_count == 1
    assert popen.call_args.args[0][3:6] == ["cancel", "--task-run-id", task_id]
    assert "resume" not in popen.call_args.args[0]
    assert popen.call_args.args[0][-2:] == [
        "--events", str(directory / "events.jsonl")
    ]


@pytest.mark.parametrize("terminal_status", ["cancelled", "deadline_exceeded"])
def test_recovery_required_v2_can_explicitly_end_and_cleanup(tmp_path, terminal_status):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id, task_id = str(uuid4()), str(uuid4())
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(agent_directory / "run.json", {"protocol_version": 2, "task_run_id": task_id})
    workbench._insert_run(run_id, challenge_id, directory)
    result = {
        "protocol_version": 2, "task_run_id": task_id,
        "task_run": {"status": "running"},
        "cleanup": {"status": "not_confirmed"},
        "recovery": {"required": True, "reason": "execution_outcome_unknown"},
        "exit_code": 5,
    }
    workbench._finish_run(
        run_id, status="recovery_required", exit_code=5,
        result=result, agent_task_run_id=task_id,
    )
    with pytest.raises(WorkbenchError, match="cancel_running_challenge_first"):
        workbench.archive(challenge_id)
    with pytest.raises(WorkbenchError, match="cleanup_not_available"):
        workbench.runner.cleanup(challenge_id)
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        assert workbench.runner.cancel(challenge_id)["status"] == "cancelling"
    assert popen.call_count == 1
    assert popen.call_args.args[0][3:6] == ["cancel", "--task-run-id", task_id]
    assert "resume" not in popen.call_args.args[0]
    ended = result | {
        "task_run": {"status": terminal_status},
        "cleanup": {"status": "succeeded"},
    }
    process = workbench.runner._active[run_id].process
    process.result = ended
    process.returncode = 5
    (directory / "cancel.stdout.log").write_text(json.dumps(ended), encoding="utf-8")
    workbench.runner._watch(run_id)
    finished = workbench.runner.status(challenge_id)
    assert finished["status"] == "cleanup_confirmed"
    assert finished["result"]["recovery"] == result["recovery"]
    assert workbench.archive(challenge_id)["archived"] is True


def test_historical_terminal_cleanup_success_is_projected_without_rewriting_run(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id, task_id = str(uuid4()), str(uuid4())
    directory = workbench.runner.root / run_id
    directory.mkdir()
    workbench._insert_run(run_id, challenge_id, directory)
    result = {
        "protocol_version": 2, "task_run_id": task_id,
        "task_run": {"status": "cancelled"},
        "cleanup": {"status": "succeeded"},
        "recovery": {"required": True, "reason": "execution_outcome_unknown"},
        "exit_code": 5,
    }
    workbench._finish_run(
        run_id, status="recovery_required", exit_code=5,
        result=result, agent_task_run_id=task_id,
    )
    restarted = Workbench(tmp_path)
    assert restarted.get(challenge_id)["status"] == "cleanup_confirmed"
    assert restarted.runner.status(challenge_id)["status"] == "cleanup_confirmed"
    with restarted._db() as db:
        assert db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0] == "recovery_required"
    assert restarted.archive(challenge_id)["archived"] is True


def test_historical_remote_cleanup_requires_explicit_network_cleanup(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    challenge = workbench.get(challenge_id)
    fields = {key: challenge[key] for key in (
        "title", "category", "description", "deadline_minutes", "step_timeout_seconds"
    )}
    workbench.update(challenge_id, fields | {"endpoint": "tcp://example.org:24009"})
    run_id, task_id = str(uuid4()), str(uuid4())
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(agent_directory / "run.json", {"protocol_version": 2, "task_run_id": task_id})
    workbench._insert_run(run_id, challenge_id, directory)
    old_result = {
        "protocol_version": 2, "task_run_id": task_id,
        "task_run": {"status": "cancelled"},
        "cleanup": {"status": "succeeded"},
        "recovery": {"required": True, "reason": "execution_outcome_unknown"},
        "exit_code": 5,
    }
    workbench._finish_run(
        run_id, status="cleanup_confirmed", exit_code=5,
        result=old_result, agent_task_run_id=task_id,
    )

    restarted = Workbench(tmp_path)
    projected = restarted.get(challenge_id)
    assert projected["status"] == projected["run"]["status"] == "recovery_required"
    assert projected["run"]["result"]["cleanup"]["status"] == "not_confirmed"
    with restarted._db() as db:
        stored = db.execute("SELECT status,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert stored["status"] == "cleanup_confirmed"
        assert json.loads(stored["result_json"])["cleanup"]["status"] == "succeeded"
    with pytest.raises(WorkbenchError, match="cancel_running_challenge_first"):
        restarted.archive(challenge_id)
    with restarted._db() as db:
        db.execute("UPDATE challenges SET archived=1 WHERE id=?", (challenge_id,))
    with pytest.raises(WorkbenchError, match="archive_terminal_challenge_first"):
        restarted.purge(challenge_id, "one")
    restarted.restore(challenge_id)

    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        assert restarted.runner.cleanup(challenge_id)["status"] == "cleaning"
    assert popen.call_args.args[0][3:] == [
        "resume", "--task-run-id", task_id, "--state-dir",
        str(agent_directory.parent), "--cleanup-only", "--events",
        str(directory / "events.jsonl"),
    ]
    confirmed = old_result | {"cleanup": {
        "status": "succeeded", "target_network_confirmed": True,
    }}
    process = restarted.runner._active[run_id].process
    process.result = confirmed
    process.returncode = 5
    (directory / "cleanup.stdout.log").write_text(json.dumps(confirmed), encoding="utf-8")
    restarted.runner._watch(run_id)
    assert restarted.runner.status(challenge_id)["status"] == "cleanup_confirmed"
    assert restarted.archive(challenge_id)["archived"] is True


def test_newer_run_status_is_not_overwritten_by_old_result(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    first, second = str(uuid4()), str(uuid4())
    workbench._insert_run(first, challenge_id, tmp_path / first)
    workbench._finish_run(first, status="failed", exit_code=2, result={}, agent_task_run_id=None)
    workbench._insert_run(second, challenge_id, tmp_path / second)
    workbench._finish_run(first, status="cancelled", exit_code=130, result={}, agent_task_run_id=None)
    assert workbench.get(challenge_id)["status"] == "starting"


def test_agent_logs_go_to_the_run_directory(tmp_path, monkeypatch):
    workbench, _ = prepared_workbench(tmp_path)
    monkeypatch.setenv("QY_LOG_CONSOLE", "1")
    env = workbench.runner._subprocess_environment(3, tmp_path)
    # A visible console is the operator's shell choice; children must not turn
    # their own INFO chatter into stderr that _wait() can read as a failure.
    assert "QY_LOG_CONSOLE" not in env
    assert env["QY_LOG_JSONL"] == str(tmp_path / "agent.log.jsonl")


@pytest.mark.parametrize(
    "stderr,failure",
    [
        ("10:21:00.525 INFO  store phase.finished run=x\n", False),
        ("[10/31/26 10:21:28] INFO     Processing request of type server.py:733\n", False),
        ("10:23:09.668 WARNING ctrl stage.call_rejected code=stage.model_output_unusable\n", False),
        ("Traceback (most recent call last):\n  File \"x.py\", line 1\n", True),
        ("12:00:00.001 ERROR   ctrl boom\n", True),
    ],
)
def test_stderr_failure_signature(stderr, failure):
    assert stderr_reports_failure(stderr) is failure


def test_cleanup_operation_keeps_the_run_logs(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id, task_id = str(uuid4()), str(uuid4())
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(agent_directory / "run.json", {"protocol_version": 2, "task_run_id": task_id})
    workbench._insert_run(run_id, challenge_id, directory)
    result = {
        "protocol_version": 2, "task_run_id": task_id,
        "task_run": {"status": "cancelled"},
        "cleanup": {"status": "not_confirmed"},
        "recovery": {"required": True}, "exit_code": 5,
    }
    workbench._finish_run(
        run_id, status="recovery_required", exit_code=5,
        result=result, agent_task_run_id=task_id,
    )
    (directory / "agent.stdout.log").write_text('{"protocol_version": 2}\n', encoding="utf-8")
    (directory / "agent.stderr.log").write_text("10:14:04.001 INFO  ctrl run started\n", encoding="utf-8")
    with patch("agent_adapters.workbench_runner.subprocess.Popen", return_value=FakeProcess()) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", NoThread
    ):
        workbench.runner.cleanup(challenge_id)
    assert popen.call_args.kwargs["stdout"].name.endswith("cleanup.stdout.log")
    assert popen.call_args.kwargs["stderr"].name.endswith("cleanup.stderr.log")
    # The original run transcript must survive the follow-up operation.
    assert (directory / "agent.stderr.log").read_text(encoding="utf-8").startswith("10:14:04")
    assert (directory / "agent.stdout.log").read_text(encoding="utf-8").startswith('{"protocol_version"')


@pytest.mark.parametrize(
    "stderr,marked",
    [
        ("10:21:00.525 INFO  store phase.finished run=x\n", False),
        ("[10/31/26 10:21:28] INFO     Processing request of type server.py:733\n", False),
        ("Traceback (most recent call last):\n  File \"x.py\", line 1\n", True),
    ],
)
def test_stderr_only_marks_a_failure_when_it_carries_one(tmp_path, stderr, marked):
    workbench, challenge_id = prepared_workbench(tmp_path)
    run_id = str(uuid4())
    directory = workbench.runner.root / run_id
    directory.mkdir()
    workbench._insert_run(run_id, challenge_id, directory)
    workbench._started_run(run_id)
    result = {"protocol_version": 1, "task_run_id": None, "exit_code": 0, "errors": []}
    workbench.runner._active[run_id] = ActiveRun(
        run_id, challenge_id, directory, FakeProcess(result)
    )
    (directory / "agent.stdout.log").write_text(json.dumps(result), encoding="utf-8")
    (directory / "agent.stderr.log").write_text(stderr, encoding="utf-8")
    workbench.runner._watch(run_id)
    codes = [error.get("code") for error in (workbench.runner.status(challenge_id)["result"] or {}).get("errors") or []]
    assert ("agent_process_stderr" in codes) is marked
