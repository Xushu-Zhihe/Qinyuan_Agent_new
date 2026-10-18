"""Test-only feedback adapter resumes a v3 CLI-created Workbench run."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
from pathlib import Path

import pytest

from fake_judge_fixture import FakeJudge

from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_adapters.web.simulated_application import _Token, _make_application
from agent_adapters.run_files import immutable_read
from agent_adapters.workbench import Workbench
from agent_core.runtime import TaskRunStatus


def _challenge():
    return {
        "title": "simulated D7 feedback",
        "category": "misc",
        "description": "derive the answer from the attached input",
        "endpoint": "",
        "deadline_minutes": 5,
        "step_timeout_seconds": 30,
    }


def _wait_for_cli(workbench: Workbench, challenge_id: str):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = workbench.runner.status(challenge_id)
        if run and run["status"] not in {
            "starting", "running", "cancelling", "cleaning"
        }:
            return run
        time.sleep(0.05)
    raise AssertionError("v3 CLI subprocess did not finish")


@pytest.mark.skipif(os.name != "nt", reason="candidate storage requires Windows user DPAPI")
def test_fake_judge_reopens_workbench_cli_run_and_continues_rejected_candidate(
    tmp_path: Path,
):
    workbench = Workbench(tmp_path, protocol_version=3)
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge["id"], "input.txt", io.BytesIO(b"fixture"), 7)
    app = None
    try:
        workbench.runner.start(challenge["id"])
        pending = _wait_for_cli(workbench, challenge["id"])
        assert pending["status"] == "paused", pending["result"]
        agent_directory = workbench.runner._find_agent_directory(
            Path(pending["directory"])
        )
        assert agent_directory is not None
        declaration = immutable_read(agent_directory / "run.json")
        task_run_id = declaration["task_run_id"]

        # This new application instance represents a trusted adapter process
        # taking over after the CLI process returned awaiting_external_feedback.
        app, sandbox = _make_application(
            agent_directory,
            declaration,
            candidate_values=("correct",),
        )
        candidate_evidence = D7CandidateEvidenceService(
            agent_directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage", "fake-judge-workbench-test"),
        )
        reader = candidate_evidence.read_port("fake-judge-workbench-test")
        judge = FakeJudge()
        first = app.store.list_d7_answer_candidates(task_run_id)[0]

        rejected = judge.evaluate(first, lambda item: reader.read(item.candidate_id))
        rejection = asyncio.run(
            app.apply_trusted_d7_feedback(
                rejected, _Token(agent_directory), candidate_evidence
            )
        )
        second = rejection.continuation_candidate
        assert rejection.feedback_state == "rejected"
        assert second is not None
        assert second.task_run_id == task_run_id
        assert second.turn_id != first.turn_id
        assert len(app.state.turns) == 2

        replay = asyncio.run(
            app.apply_trusted_d7_feedback(
                rejected, _Token(agent_directory), candidate_evidence
            )
        )
        assert replay.continuation_candidate == second
        assert len(app.state.turns) == 2

        accepted = judge.evaluate(second, lambda item: reader.read(item.candidate_id))
        result = asyncio.run(
            app.apply_trusted_d7_feedback(
                accepted, _Token(agent_directory), candidate_evidence
            )
        )
        assert result.feedback_state == "accepted"
        assert app.state.task_run.status is TaskRunStatus.ACHIEVED
        app.controller.cleanup_sandbox()
        app.close()
        app = None

        projection = workbench.runner.progress(challenge["id"])["progress"]
        assert projection["task_run"]["status"] == "achieved"
        assert projection["goal_evaluation"] == "accepted"
        assert projection["cleanup_status"] == "confirmed"
        assert [item["validation_state"] for item in projection["answer_candidates"]] == [
            "rejected", "accepted"
        ]
        assert "simulation-only-pending-candidate" not in json.dumps(projection)
        assert '"correct"' not in json.dumps(projection)
        events = workbench.runner.events(challenge["id"])
        assert "simulation-only-pending-candidate" not in json.dumps(events)
        assert '"correct"' not in json.dumps(events)
        sandbox_data = agent_directory / "d7-simulated-sandbox"
        assert not (sandbox_data / "active.json").exists()
        assert not list(sandbox_data.glob("*.data"))
    finally:
        if app is not None:
            if app.state.task_run.status in {
                TaskRunStatus.RUNNING,
                TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
            }:
                app.cancel()
            else:
                app.controller.cleanup_sandbox()
            app.close()
        workbench.runner.shutdown()
