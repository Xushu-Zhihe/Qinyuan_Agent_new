"""D7 Controller replay and quiescent detach rules."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from test_d7_turn_budget import make_case

from agent_core.control_store import StorageError
from agent_core.controller import (
    TaskRunController,
    TaskRunControllerRegistry,
)
from agent_core.domain import ExecutionStatus
from agent_core.goal_candidates import D7AnswerCandidate
from agent_core.goal_feedback import (
    ExternalValidationFeedback,
    project_candidate_feedback,
)
from agent_core.operation_accounting import D4RecoveryRequired
from agent_core.run_outcome import D7RunOutcomeState, map_run_outcome
from agent_core.runtime import InvalidTransition, TaskRunStatus


class Sandbox:
    def __init__(self):
        self.destroy_calls = []

    def create(self, task_run_id):
        return f"sandbox-{task_run_id}"

    def interrupt(self, sandbox_id):
        pass

    def destroy(self, sandbox_id):
        self.destroy_calls.append(sandbox_id)


def _candidate(task_run_id: str) -> D7AnswerCandidate:
    return D7AnswerCandidate(
        task_run_id=task_run_id,
        candidate_id=str(uuid4()),
        turn_id=str(uuid4()),
        intent_id=str(uuid4()),
        plan_id=str(uuid4()),
        execution_id=str(uuid4()),
        evidence_version="a" * 64,
        source_ref="artifact:v1",
    )


def test_candidate_accepts_d3_uuid5_references_but_keeps_new_ids_uuid4():
    candidate = _candidate(str(uuid4()))
    turn_id = str(uuid5(NAMESPACE_URL, "persisted-d3-turn"))
    intent_id = str(uuid5(NAMESPACE_URL, "persisted-d3-intent"))

    bound = replace(candidate, turn_id=turn_id, intent_id=intent_id)

    assert bound.turn_id == turn_id
    assert bound.intent_id == intent_id
    with pytest.raises(ValueError, match="candidate_id must be a canonical UUID4"):
        replace(candidate, candidate_id=turn_id)


def _feedback(candidate: D7AnswerCandidate) -> ExternalValidationFeedback:
    return ExternalValidationFeedback(
        task_run_id=candidate.task_run_id,
        candidate_id=candidate.candidate_id,
        evidence_version=candidate.evidence_version,
        verdict="accepted",
        source_ref="test-judge",
        operation_id=str(uuid4()),
    )


def _controller_for_feedback(task_run, candidate, *, now):
    store = SimpleNamespace(
        load_d7_answer_candidate=lambda candidate_id: (
            candidate if candidate_id == candidate.candidate_id else None
        ),
        list_d7_validation_feedback=lambda *_args: (),
    )
    controller = TaskRunController(
        Sandbox(),
        clock=lambda: now,
        store=store,
        repository=SimpleNamespace(),
        registry=SimpleNamespace(),
    )
    state = SimpleNamespace(latest_checkpoint=SimpleNamespace(sequence=4))
    controller._require_bound_state = lambda: (task_run, state)
    controller._require_persistent = lambda: controller._persistent
    controller._load_existing_commit = lambda _mutation_id: None
    return controller


def test_deadline_feedback_is_persisted_without_claiming_success():
    task_run_id = str(uuid4())
    candidate = _candidate(task_run_id)
    feedback = _feedback(candidate)
    task_run = SimpleNamespace(
        task_run_id=task_run_id,
        status=TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
        deadline_at=10.0,
        failure_reason=None,
    )
    controller = _controller_for_feedback(task_run, candidate, now=10.0)
    captured = {}

    def commit_feedback(**kwargs):
        captured.update(kwargs)
        return "feedback-deadline-commit"

    controller._commit_d7_lifecycle = commit_feedback
    result = controller.apply_external_validation_feedback(feedback)

    assert result == "feedback-deadline-commit"
    assert captured["status"] is TaskRunStatus.DEADLINE_EXCEEDED
    assert captured["operation"] == "apply_external_validation_feedback"
    assert captured["mutation_id"] == feedback.operation_id


def test_late_feedback_is_persisted_while_deadline_status_is_preserved():
    task_run_id = str(uuid4())
    candidate = _candidate(task_run_id)
    feedback = _feedback(candidate)
    task_run = SimpleNamespace(
        task_run_id=task_run_id,
        status=TaskRunStatus.DEADLINE_EXCEEDED,
        deadline_at=10.0,
        failure_reason=None,
    )
    controller = _controller_for_feedback(task_run, candidate, now=20.0)
    captured = {}
    controller._commit_d7_lifecycle = lambda **kwargs: (
        captured.update(kwargs) or "saved"
    )

    result = controller.apply_external_validation_feedback(feedback)

    assert result == "saved"
    assert captured["status"] is TaskRunStatus.DEADLINE_EXCEEDED
    assert captured["metadata"].feedback == feedback


def _completed_candidate(app):
    state = app.state
    execution = next(
        item for item in state.executions if item.status is ExecutionStatus.SUCCEEDED
    )
    plan = next(item for item in state.plans if item.plan_id == execution.plan_id)
    intent = next(item for item in state.intents if item.intent_id == plan.intent_id)
    turn = next(item for item in state.turns if item.turn_id == intent.turn_id)
    binding = app.store.load_d3_plan_binding(plan.plan_id)
    result = app.store.load_d3_plan_result(plan.plan_id)
    assert binding is not None and result is not None
    return D7AnswerCandidate(
        task_run_id=state.task_run.task_run_id,
        candidate_id=str(uuid4()),
        turn_id=turn.turn_id,
        intent_id=intent.intent_id,
        plan_id=plan.plan_id,
        execution_id=execution.execution_id,
        evidence_version=result.evidence_sha256,
        source_ref=binding.artifact_version_id,
    )


@pytest.mark.parametrize(
    ("first_verdict", "second_verdict", "expected_status"),
    [
        ("rejected", "accepted", TaskRunStatus.RUNNING),
        ("accepted", "rejected", TaskRunStatus.ACHIEVED),
    ],
)
def test_opposite_determinate_feedback_is_appended_and_projects_conflict(
    first_verdict, second_verdict, expected_status
):
    with TemporaryDirectory(
        prefix="d7-conflict-", dir=Path("tests/.runtime-work")
    ) as runtime_dir:
        app, token = make_case(Path(runtime_dir), 2)
        try:
            result = asyncio.run(app.run(token))
            assert result.stop_reason == "turn_completed_sandbox_retained"
            candidate = _completed_candidate(app)
            app.controller.register_answer_candidate(candidate, str(uuid4()))
            first = ExternalValidationFeedback(
                task_run_id=candidate.task_run_id,
                candidate_id=candidate.candidate_id,
                evidence_version=candidate.evidence_version,
                verdict=first_verdict,
                source_ref="judge:first",
                operation_id=str(uuid4()),
            )
            second = ExternalValidationFeedback(
                task_run_id=candidate.task_run_id,
                candidate_id=candidate.candidate_id,
                evidence_version=candidate.evidence_version,
                verdict=second_verdict,
                source_ref="judge:second",
                operation_id=str(uuid4()),
            )

            app.controller.apply_external_validation_feedback(first)
            app.controller.apply_external_validation_feedback(second)

            records = app.store.list_d7_validation_feedback(
                candidate.task_run_id,
                candidate.candidate_id,
                candidate.evidence_version,
            )
            projection = project_candidate_feedback(
                candidate.task_run_id,
                candidate.candidate_id,
                candidate.evidence_version,
                records,
            )
            outcome = map_run_outcome(
                D7RunOutcomeState(
                    task_run_status=app.state.task_run.status.value,
                    goal_evaluation="accepted",
                    cleanup_state="confirmed",
                    recovery_required=projection.state == "conflict",
                )
            )
            assert records == (first, second)
            assert projection.state == "conflict"
            assert app.state.task_run.status is expected_status
            assert outcome.run_exit_code == 5

            if first_verdict == "rejected":
                next_candidate = replace(candidate, candidate_id=str(uuid4()))
                with pytest.raises(InvalidTransition, match="conflict blocks"):
                    app.controller.register_answer_candidate(
                        next_candidate, str(uuid4())
                    )

                app.controller._d7_has_feedback_conflict = lambda *_args: False
                with pytest.raises(StorageError, match="conflict blocks"):
                    app.controller.register_answer_candidate(
                        next_candidate, str(uuid4())
                    )
        finally:
            if app.state.task_run.status is TaskRunStatus.RUNNING:
                app.cancel()
            elif app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:
                app.controller.close()
            else:
                app.controller.cleanup_sandbox()
            app.close()


@pytest.mark.parametrize(
    ("unresolved", "running_execution", "error"),
    [
        (True, False, "has_unresolved_operation"),
        (False, True, "has_running_execution"),
    ],
)
def test_close_rejects_non_quiescent_external_feedback_wait(
    unresolved, running_execution, error
):
    task_run_id = str(uuid4())
    task_run = SimpleNamespace(
        task_run_id=task_run_id, status=TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    )
    execution = SimpleNamespace(
        status=(
            ExecutionStatus.RUNNING if running_execution else ExecutionStatus.SUCCEEDED
        )
    )
    state = SimpleNamespace(
        task_run=task_run,
        latest_checkpoint=SimpleNamespace(task_run_id=task_run_id),
        executions=(execution,),
    )
    store = SimpleNamespace(
        load_d3_run_config=lambda _task_run_id: SimpleNamespace(d4_budget=object()),
        list_d4_unresolved_operations=lambda _task_run_id: (
            (SimpleNamespace(start=SimpleNamespace(task_run_id=task_run_id)),)
            if unresolved
            else ()
        ),
    )
    registry = TaskRunControllerRegistry()
    controller = TaskRunController(
        Sandbox(),
        store=store,
        repository=SimpleNamespace(load_control_state=lambda _task_run_id: state),
        registry=registry,
    )
    controller._persistent_task_run_id = task_run_id
    controller._persistent_snapshot = task_run
    controller._persistent_state = state
    registry.claim(task_run_id, controller)

    with pytest.raises(D4RecoveryRequired, match=error):
        controller.close()

    assert registry.owner(task_run_id) is controller
    assert not controller._closed


def test_close_detaches_quiescent_feedback_wait_without_sandbox_cleanup():
    task_run_id = str(uuid4())
    sandbox = Sandbox()
    task_run = SimpleNamespace(
        task_run_id=task_run_id, status=TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    )
    state = SimpleNamespace(
        task_run=task_run,
        latest_checkpoint=SimpleNamespace(task_run_id=task_run_id),
        executions=(),
    )
    store = SimpleNamespace(
        load_d3_run_config=lambda _task_run_id: SimpleNamespace(d4_budget=object()),
        list_d4_unresolved_operations=lambda _task_run_id: (),
    )
    registry = TaskRunControllerRegistry()
    controller = TaskRunController(
        sandbox,
        store=store,
        repository=SimpleNamespace(load_control_state=lambda _task_run_id: state),
        registry=registry,
    )
    controller._persistent_task_run_id = task_run_id
    controller._persistent_snapshot = task_run
    controller._persistent_state = state
    registry.claim(task_run_id, controller)

    controller.close()

    assert registry.owner(task_run_id) is None
    assert controller._closed
    assert sandbox.destroy_calls == []


def test_close_still_rejects_an_active_task_run():
    task_run_id = str(uuid4())
    task_run = SimpleNamespace(task_run_id=task_run_id, status=TaskRunStatus.RUNNING)
    state = SimpleNamespace(
        task_run=task_run,
        latest_checkpoint=SimpleNamespace(task_run_id=task_run_id),
        executions=(),
    )
    registry = TaskRunControllerRegistry()
    controller = TaskRunController(
        Sandbox(),
        store=SimpleNamespace(),
        repository=SimpleNamespace(load_control_state=lambda _task_run_id: state),
        registry=registry,
    )
    controller._persistent_task_run_id = task_run_id
    controller._persistent_snapshot = task_run
    controller._persistent_state = state
    registry.claim(task_run_id, controller)

    with pytest.raises(InvalidTransition, match="quiescent external-feedback wait"):
        controller.close()
