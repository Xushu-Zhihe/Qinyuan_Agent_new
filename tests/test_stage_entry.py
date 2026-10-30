"""U6 full CLI worker + separate human command, local persistence and fixed IO only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from test_public_simulation import request
from agent_adapters.simulation import simulation_config
from agent_adapters.run_files import immutable_read
from agent_adapters.web.task_result import snapshot_v3_task_result


@pytest.mark.parametrize("decision,expected", [("accept", "achieved"), ("continue", "inconclusive"), ("end", "inconclusive")])
def test_public_cli_human_command_and_original_waiting_worker(tmp_path, decision, expected):
    raw = request(tmp_path)
    raw.update(execution_profile="d8_v22", d8_config=simulation_config(rolling=True))
    raw["task_request"]["deadline_seconds"] = 60
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(raw), encoding="utf-8")
    state = tmp_path / "state"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    root = str(Path(__file__).resolve().parents[1])
    command = [sys.executable, "-B", "-m", "agent"]
    process = subprocess.Popen(command + ["run-task", "--request", str(request_file), "--mode", "simulated", "--state-dir", str(state)],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        limit = time.monotonic() + 25
        view = None
        while time.monotonic() < limit and process.poll() is None:
            declarations = list(state.glob("*/run.json"))
            if declarations:
                declaration_file, = declarations
                declaration = immutable_read(declaration_file)
                view = snapshot_v3_task_result(declaration_file.parent, declaration)
                if (view.get("task_run") or {}).get("status") == "awaiting_confirmation":
                    break
            time.sleep(0.05)
        else:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(repr((stdout, stderr)))
            pytest.fail(repr(view))
        assert view["goal_validation"]["actions"] == ["accept", "continue", "end"]
        candidate, = view["answer_candidates"]
        control = subprocess.run(command + ["human-decision", "--state-dir", str(state), "--task-run-id", view["task_run_id"],
            "--candidate-id", candidate["candidate_id"], "--operation-id", str(uuid4()), "--decision", decision],
            cwd=root, env=env, capture_output=True, timeout=15)
        assert control.returncode == 0, (control.stdout, control.stderr)
        stdout, stderr = process.communicate(timeout=20)
        result = json.loads(stdout)
        assert not stderr, stderr
        assert result["errors"] == [], result
        assert result["task_run"]["status"] == expected, result
        assert result["cleanup_status"] == "confirmed", result
        assert result["goal_evaluation"] == ("accepted" if decision == "accept" else "rejected" if decision == "continue" else "not_evaluated")
        assert process.returncode == (0 if decision == "accept" else 4), result
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_workbench_waiting_worker_accepts_short_human_control(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_workbench_v3 import _challenge
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile="d8_v22", d7_mode="simulated")
    try:
        challenge = workbench.create(_challenge())
        workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"controlled U6"), len(b"controlled U6"))
        workbench.runner.start(challenge["id"])
        limit = time.monotonic() + 25
        while time.monotonic() < limit:
            view = workbench.runner.progress(challenge["id"])
            progress = view.get("progress") or {}
            if (progress.get("task_run") or {}).get("status") == "awaiting_confirmation":
                break
            time.sleep(0.05)
        else:
            pytest.fail(repr(view))
        candidate, = progress["answer_candidates"]
        workbench.runner.human_decision(challenge["id"], dict(candidate_id=candidate["candidate_id"],
            action="end", operation_id=str(uuid4())))
        while time.monotonic() < limit:
            view = workbench.runner.progress(challenge["id"])
            if view["run"]["status"] in {"finished", "cleanup_confirmed"}:
                break
            time.sleep(0.05)
        assert view["run"]["result"]["task_run"]["status"] == "inconclusive", view
        assert view["run"]["result"]["cleanup_status"] == "confirmed", view
    finally:
        workbench.runner.shutdown()
