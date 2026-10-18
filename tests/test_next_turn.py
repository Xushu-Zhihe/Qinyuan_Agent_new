"""D7 rejection can durably register and execute a subsequent D3 Turn."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from agent_core.goal_feedback import ExternalValidationFeedback
from agent_core.runtime import TaskRunStatus

from test_controller_lifecycle import _completed_candidate
from test_turn_budget import make_case


def test_rejected_candidate_registers_next_turn_in_same_task_run(tmp_path: Path):
    app, token = make_case(tmp_path, 2)
    try:
        first = asyncio.run(app.run(token))
        assert first.stop_reason == "turn_completed_sandbox_retained"
        candidate = _completed_candidate(app)
        app.controller.register_answer_candidate(candidate, str(uuid4()))
        feedback = ExternalValidationFeedback(
            task_run_id=candidate.task_run_id,
            candidate_id=candidate.candidate_id,
            evidence_version=candidate.evidence_version,
            verdict="rejected",
            source_ref="fake-judge",
            operation_id=str(uuid4()),
        )
        app.controller.apply_external_validation_feedback(feedback)
        assert app.state.task_run.status is TaskRunStatus.RUNNING

        binding = asyncio.run(app.register_next_d7_turn(token))
        assert binding.task_run_id == candidate.task_run_id
        assert len(app.state.turns) == 2
        assert app.state.turns[0].status.value == "completed"
        assert app.state.turns[1].status.value == "pending"

        second = asyncio.run(app.run(token))
        assert second.stop_reason == "turn_completed_sandbox_retained"
        assert len(app.state.turns) == 2
        assert app.state.turns[-1].status.value == "completed"
    finally:
        if app.state.task_run.status is TaskRunStatus.RUNNING:
            app.cancel()
        elif app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:
            app.controller.close()
        else:
            app.controller.cleanup_sandbox()
        app.close()
