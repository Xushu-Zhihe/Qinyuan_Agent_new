"""Progress is a read-only view of the same v2 simulation, not a new runner."""

import asyncio
import hashlib
from uuid import uuid4

import pytest
from test_task_application import declaration
from test_tool_gateway import Token
from test_workbench_runner import prepared_workbench

from agent_adapters.run_files import immutable_write
from agent_adapters.task_application import TaskApplication
from agent_adapters.task_result import snapshot_task_result
from agent_adapters.workbench import WorkbenchError


def completed_run(tmp_path):
    workbench, challenge_id = prepared_workbench(tmp_path / "workbench")
    data = declaration(tmp_path)
    run_id = str(uuid4())
    parent = workbench.runner.root / run_id
    directory = parent / "agent-state" / data["task_run_id"]
    directory.mkdir(parents=True)
    immutable_write(directory / "run.json", data)
    workbench._insert_run(run_id, challenge_id, parent)
    app = TaskApplication(directory, data, Token())
    try:
        asyncio.run(app.execute(start=True))
    finally:
        app.close()
    result = snapshot_task_result(directory, data)
    workbench._finish_run(run_id, status="paused", result=result, exit_code=4,
                          agent_task_run_id=data["task_run_id"])
    return workbench, challenge_id, directory, result


def hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def test_progress_and_command_details_preserve_state_and_cli_contract(tmp_path):
    workbench, challenge_id, directory, result = completed_run(tmp_path)
    before = hashes(directory)
    progress = workbench.runner.progress(challenge_id)
    view = progress["progress"]
    assert progress["run"]["status"] == "paused"
    assert len(view["plans"]) == 1
    assert len(view["executions"]) == 1
    assert [item["kind"] for item in view["actions"]] == ["probe", "complete_intent"]
    assert view["checkpoint"] > 0
    assert view["overall_goal"]["outcome"] == "not_evaluated"
    assert "plans" not in result and "executions" not in result
    detail = workbench.runner.execution(challenge_id, view["executions"][0]["execution_id"])
    assert detail["script"] == "printf observed"
    assert detail["output"]["stdout"] == "observed\n"
    assert detail["output"]["exit_code"] == 0
    assert detail["output"]["termination_confirmed"] is True
    assert hashes(directory) == before
    with pytest.raises(WorkbenchError, match="execution_evidence_unavailable"):
        workbench.runner.execution(challenge_id, str(uuid4()))


def test_progress_reports_corrupt_evidence_without_partial_truth(tmp_path):
    workbench, challenge_id, directory, _ = completed_run(tmp_path)
    path = next((directory / "commands").glob("output-*.json"))
    path.write_bytes(b"damaged")
    before = hashes(directory)
    value = workbench.runner.progress(challenge_id)["progress"]
    assert value["recovery"]["reason"] == "state_or_evidence_unreadable"
    assert value["verified_facts"] == []
    assert hashes(directory) == before
