"""Focused workbench checks for the explicitly selected D7 simulation."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from agent_adapters.run_files import immutable_write
from agent_adapters.workbench import Workbench, WorkbenchError
from agent_adapters.workbench_runner import ActiveRun


class _Process:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class _NoThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        pass


class _FinishedProcess(_Process):
    def __init__(self, result):
        super().__init__()
        self.returncode = 0
        self.result = result

    def communicate(self):
        return json.dumps(self.result), ""


def _challenge(*, endpoint: str = ""):
    return {
        "title": "simulated d7",
        "category": "misc",
        "description": "derive the answer from the attached input",
        "endpoint": endpoint,
        "deadline_minutes": 5,
        "step_timeout_seconds": 30,
    }


def test_v3_start_is_explicit_simulation_without_model_key_or_env_file(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"fixture"), 7)

    with patch.dict(
        "os.environ",
        {
            "QINYUAN_RUNTIME_MODEL": "inherited-model",
            "QINYUAN_RUNTIME_API_KEY": "inherited-secret",
            "QINYUAN_RUNTIME_BASE_URL": "https://model.example",
            "QINYUAN_RUNTIME_PROVIDER": "inherited-provider",
        },
    ), patch(
        "agent_adapters.workbench_runner.subprocess.Popen", return_value=_Process()
    ) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", _NoThread
    ):
        run = workbench.runner.start(challenge["id"])

    command = popen.call_args.args[0]
    assert command[0:5] == [command[0], "-m", "agent", "run-task", "--mode"]
    assert command[5] == "simulated"
    assert "--env-file" not in command
    request = json.loads((Path(run["directory"]) / "request.json").read_text())
    assert request["protocol_version"] == 3
    assert request["request_id"]
    assert request["task_request"]["target"]["endpoints"] == []
    assert request["authorization"]["allowed_tool"] == "d3_fixed_readonly"
    assert request["authorization"]["network_allowed"] is False
    assert request["authorization"]["attachment_indices"] == [0]
    assert request["dual_llm_config"]["d4_budget"]["total"] == 48
    # The model key is not a v3 simulation prerequisite and never enters the request.
    assert "api_key" not in json.dumps(request)
    child_env = popen.call_args.kwargs["env"]
    assert all(
        name not in child_env
        for name in (
            "QINYUAN_RUNTIME_MODEL",
            "QINYUAN_RUNTIME_API_KEY",
            "QINYUAN_RUNTIME_BASE_URL",
            "QINYUAN_RUNTIME_PROVIDER",
        )
    )


def test_v3_preflight_rejects_network_target_before_spawning(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    challenge = workbench.create(_challenge(endpoint="tcp://example.test:24009"))

    with patch("agent_adapters.workbench_runner.subprocess.Popen") as popen, pytest.raises(
        WorkbenchError, match="v3_network_not_supported"
    ):
        workbench.runner.start(challenge["id"])

    popen.assert_not_called()
    assert workbench.get(challenge["id"])["run"] is None


def test_v3_health_and_projection_advertise_simulation_capability(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    assert workbench.model_status()["protocol_version"] == 3
    # The safe projection is intentionally independent of candidate plaintext.
    result = {
        "protocol_version": 3,
        "task_run_id": "task",
        "task_run": {"status": "awaiting_external_feedback"},
        "goal_evaluation": "unknown",
        "answer_candidates": [
            {"candidate_id": "candidate", "evidence_version": "v1", "validation_state": "unknown"}
        ],
        "cleanup": {"status": "not_confirmed"},
        "recovery": {"required": True, "reason": "external_feedback_unknown"},
    }
    safe = workbench._run_dict(
        {
            "run_id": "run",
            "challenge_id": "challenge",
            "directory": str(tmp_path),
            "agent_task_run_id": "task",
            "status": "running",
            "started_at": "now",
            "finished_at": None,
            "exit_code": None,
            "cancel_requested": 0,
            "result_json": json.dumps(result),
            "updated_at": "now",
        }
    )
    assert safe["result"]["goal_evaluation"] == "unknown"
    assert "value" not in json.dumps(safe)


def test_invalid_protocol_environment_is_not_silently_migrated(tmp_path: Path):
    with patch.dict("os.environ", {"QINYUAN_WORKBENCH_PROTOCOL_VERSION": "invalid"}), pytest.raises(
        ValueError, match="unsupported_workbench_protocol_version"
    ):
        Workbench(tmp_path)


def _saved_run(workbench, result):
    challenge = workbench.create(_challenge())
    run_id = str(uuid4())
    task_id = result["task_run_id"]
    directory = workbench.runner.root / run_id
    agent_directory = directory / "agent-state" / task_id
    agent_directory.mkdir(parents=True)
    immutable_write(agent_directory / "run.json", {"protocol_version": 3, "task_run_id": task_id})
    workbench._insert_run(run_id, challenge["id"], directory)
    workbench._finish_run(run_id, status="recovery_required", exit_code=5,
                          result=result, agent_task_run_id=task_id)
    return challenge["id"], task_id


def test_v3_waiting_for_feedback_can_cancel_through_the_same_cli(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    task_id = str(uuid4())
    challenge_id, _ = _saved_run(workbench, {
        "protocol_version": 3, "task_run_id": task_id,
        "task_run": {"status": "awaiting_external_feedback"},
        "recovery": {"required": True}, "exit_code": 5,
    })
    with patch.dict("os.environ", {"QINYUAN_RUNTIME_API_KEY": "inherited-secret"}), patch(
        "agent_adapters.workbench_runner.subprocess.Popen", return_value=_Process()
    ) as popen, patch(
        "agent_adapters.workbench_runner.threading.Thread", _NoThread
    ):
        assert workbench.runner.cancel(challenge_id)["status"] == "cancelling"
    command = popen.call_args.args[0]
    assert command[3:6] == ["cancel", "--task-run-id", task_id]
    assert "resume" not in command
    assert "QINYUAN_RUNTIME_API_KEY" not in popen.call_args.kwargs["env"]


def test_v3_execution_endpoint_uses_only_the_safe_projection(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    challenge_id, task_id = _saved_run(workbench, {
        "protocol_version": 3, "task_run_id": str(uuid4()),
        "task_run": {"status": "awaiting_external_feedback"},
        "exit_code": 5,
    })
    execution_id = str(uuid4())
    safe_execution = {"execution_id": execution_id, "plan_id": str(uuid4()),
                      "status": "succeeded", "step_index": 0}
    with patch("agent_adapters.web.task_result.snapshot_v3_task_result",
               return_value={"executions": [safe_execution]}) as snapshot, patch(
        "agent_adapters.task_progress.execution_detail"
    ) as legacy:
        result = workbench.runner.execution(challenge_id, execution_id)
        with pytest.raises(WorkbenchError, match="execution_evidence_unavailable"):
            workbench.runner.execution(challenge_id, str(uuid4()))
    assert result == {"protocol_version": 3, "task_run_id": task_id,
                      "execution": safe_execution, "body_withheld": True}
    assert snapshot.call_count == 2
    legacy.assert_not_called()


def test_v3_cancel_operation_success_keeps_the_cancelled_run_outcome(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    result = {
        "protocol_version": 3, "task_run_id": str(uuid4()),
        "task_run": {"status": "cancelled"}, "cleanup": {"status": "succeeded"},
        "recovery": {"required": False}, "exit_code": 0, "run_exit_code": 130,
    }
    challenge_id, _ = _saved_run(workbench, result)
    run = workbench.runner.status(challenge_id)
    workbench.runner._active[run["run_id"]] = ActiveRun(
        run["run_id"], challenge_id, Path(run["directory"]), _FinishedProcess(result)
    )
    workbench.runner._watch(run["run_id"])
    finished = workbench.runner.status(challenge_id)
    assert finished["status"] == "cancelled"
    assert finished["exit_code"] == 0
    assert finished["result"]["run_exit_code"] == 130


def _wait_for_cli(workbench, challenge_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = workbench.runner.status(challenge_id)
        if run["status"] not in {"starting", "running", "cancelling", "cleaning"}:
            return run
        time.sleep(0.05)
    raise AssertionError("workbench CLI subprocess did not finish")


@pytest.mark.skipif(os.name != "nt", reason="v3 candidate storage requires Windows user DPAPI")
def test_v3_local_cli_subprocess_reaches_pending_candidate_and_explicit_cancel(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"fixture"), 7)
    try:
        workbench.runner.start(challenge["id"])
        pending = _wait_for_cli(workbench, challenge["id"])
        assert pending["status"] == "paused", pending["result"]
        result = pending["result"]
        assert result["task_run"]["status"] == "awaiting_external_feedback"
        assert result["goal_evaluation"] == "not_evaluated"
        assert len(result["answer_candidates"]) == 1
        assert result["answer_candidates"][0]["validation_state"] == "pending"
        assert result["executions"]
        assert "simulation-only-pending-candidate" not in json.dumps(result)
        assert "run_exit_code" not in result
        directory = workbench.runner._find_agent_directory(Path(pending["directory"]))
        assert (directory / "control.sqlite").is_file()
        event_path = Path(pending["directory"]) / "events.jsonl"
        before_cancel = [
            json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        workbench.runner.cancel(challenge["id"])
        cancelled = _wait_for_cli(workbench, challenge["id"])
        assert cancelled["status"] == "cancelled", cancelled["result"]
        assert cancelled["exit_code"] == 0
        assert cancelled["result"]["run_exit_code"] == 130
        assert cancelled["result"]["cleanup"]["status"] == "succeeded"
        exported = [
            json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(exported) > len(before_cancel)
        assert [item["sequence"] for item in exported] == list(
            range(1, len(exported) + 1)
        )
        assert any(
            item["type"] == "task_run_finished" and item["status"] == "cancelled"
            for item in exported[len(before_cancel):]
        )
    finally:
        workbench.runner.shutdown()


def test_v3_zero_attachment_cli_subprocess_is_inconclusive_and_cleaned(tmp_path: Path):
    workbench = Workbench(tmp_path, protocol_version=3)
    challenge = workbench.create(_challenge())
    try:
        workbench.runner.start(challenge["id"])
        finished = _wait_for_cli(workbench, challenge["id"])
        # Debug: print directory contents
        task_id = challenge["id"]
        run_dir = tmp_path / task_id
        print(f"\n[DEBUG] Run directory: {run_dir}")
        if run_dir.exists():
            print(f"[DEBUG] Files in run directory:")
            for f in run_dir.iterdir():
                print(f"  - {f.name}")
        assert finished["status"] == "cleanup_confirmed", finished["result"]
        result = finished["result"]
        assert result["task_run"]["status"] == "inconclusive"
        assert result["goal_evaluation"] == "not_evaluated"
        assert result["run_exit_code"] == 4
        assert result["cleanup"]["status"] == "succeeded"
        assert result["executions"] == []
        assert result["answer_candidates"] == []
    finally:
        workbench.runner.shutdown()

