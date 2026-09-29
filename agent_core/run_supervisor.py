"""T6-4 depth-first control-plane scheduler.

This module deliberately does not execute Bash, MCP calls, or model requests.
Those responsibilities are supplied through the two narrow ports in
``supervisor_contracts``. Every state change is prepared from the latest
committed snapshot and submitted through ``TaskRunController``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from agent_core.controller import TaskRunController
from agent_core.domain import (
    ExecutionStatus, FailureType, IntentStatus, PlanStatus,
    PlanValidationStatus, TurnStatus,
)
from agent_core.intent_generation import IntentGenerationResult
from agent_core.modeling import CancellationToken
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.supervisor_contracts import (
    IntentRunnerPort,
    IntentRunResult,
    TurnGeneratorPort,
)
from agent_core.turn_working_copy import TurnWorkingCopy

SupervisorStop = Literal[
    "turn_completed", "next_intent", "next_turn", "paused_inconclusive",
    "model_error", "deadline_exceeded", "cancelled", "no_work", "recovery_required",
    "action_rejected",
]


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    task_run_id: str
    stop: SupervisorStop
    turn_id: str | None = None
    intent_id: str | None = None
    generated_next_turn: bool = False
    reason: str = ""


class SupervisorRecoveryRequired(RuntimeError):
    """The runner or control state did not provide a safe classification."""


class _NeverCancelled:
    @property
    def is_cancelled(self) -> bool:
        return False


class RunSupervisor:
    """Run ordered Intents depth-first until a safe stop boundary is reached."""

    def __init__(
        self,
        controller: TaskRunController,
        evidence,
        runner: IntentRunnerPort,
        *,
        generator: TurnGeneratorPort | None = None,
    ) -> None:
        self._controller = controller
        self._evidence = evidence
        self._runner = runner
        self._generator = generator

    @property
    def _state(self):
        return self._controller.control_state

    @property
    def _work(self) -> TurnWorkingCopy:
        return TurnWorkingCopy(self._state, self._evidence)

    def _task_boundary(self, token: CancellationToken | None) -> SupervisorResult | None:
        state = self._state
        task = state.task_run
        if task.status in TERMINAL_STATUSES:
            stop: SupervisorStop = (
                "deadline_exceeded" if task.status is TaskRunStatus.DEADLINE_EXCEEDED
                else "cancelled" if task.status is TaskRunStatus.CANCELLED
                else "no_work"
            )
            return SupervisorResult(task.task_run_id, stop, reason=task.status.value)
        if token is not None and token.is_cancelled:
            self._controller.cancel(str(uuid4()))
            return SupervisorResult(task.task_run_id, "cancelled")
        if self._controller._now_utc() >= task.deadline_at:  # trusted Controller clock
            self._controller.advance_time(str(uuid4()))
            return SupervisorResult(task.task_run_id, "deadline_exceeded")
        return None

    def _current_turn(self):
        state = self._state
        running = [item for item in state.turns if item.status is TurnStatus.RUNNING]
        if len(running) > 1:
            raise SupervisorRecoveryRequired("multiple_running_turns")
        if running:
            return running[0]
        pending = [item for item in state.turns if item.status is TurnStatus.PENDING]
        return min(pending, key=lambda item: item.sequence) if pending else None

    def _current_intent(self, turn):
        state = self._state
        intents = [item for item in state.intents if item.turn_id == turn.turn_id]
        running = [item for item in intents if item.status is IntentStatus.RUNNING]
        if len(running) > 1:
            raise SupervisorRecoveryRequired("multiple_running_intents")
        if running:
            return running[0], False
        pending = [item for item in intents if item.status is IntentStatus.PENDING]
        if not pending:
            return None, False
        return min(pending, key=lambda item: item.priority), True

    async def run_once(self, token: CancellationToken | None = None) -> SupervisorResult:
        """Advance exactly one Intent or one terminal/new-Turn boundary."""

        reconcile = getattr(self._runner, "reconcile_terminal_actions", None)
        if reconcile is not None:
            reconcile()
        boundary = self._task_boundary(token)
        if boundary is not None:
            return boundary
        task_id = self._state.task_run.task_run_id
        turn = self._current_turn()
        if turn is None:
            prior = max(self._state.turns, key=lambda item: item.sequence, default=None)
            if prior is not None and prior.status is TurnStatus.INCONCLUSIVE:
                return SupervisorResult(
                    task_id, "paused_inconclusive", turn_id=prior.turn_id,
                    reason="turn_already_inconclusive",
                )
            if self._generator is not None:
                target = prior.turn_target if prior is not None else self._state.task_run.request.goal
                return await self._generate_next(target, token)
            return SupervisorResult(task_id, "no_work", reason="no_pending_turn")
        completed_intent = next(
            (
                item for item in self._state.intents
                if item.turn_id == turn.turn_id and item.status is IntentStatus.SUCCEEDED
            ), None,
        )
        if completed_intent is not None:
            if turn.status is not TurnStatus.RUNNING:
                raise SupervisorRecoveryRequired("succeeded_intent_has_no_running_turn")
            self._controller.commit_turn_operation(
                self._work.complete_turn(turn.turn_id), str(uuid4())
            )
            return SupervisorResult(
                task_id, "turn_completed", turn.turn_id, completed_intent.intent_id,
                reason="committed_intent_reconciled",
            )
        intent, needs_selection = self._current_intent(turn)
        if intent is None:
            raise SupervisorRecoveryRequired("turn_has_no_dispatchable_intent")
        if needs_selection:
            self._controller.commit_turn_operation(
                self._work.select_next_intent(turn.turn_id), str(uuid4())
            )
            intent = next(item for item in self._state.intents if item.intent_id == intent.intent_id)
        previous_model_error = next(
            (
                item
                for item in self._state.failure_summaries
                if item.failure_type is FailureType.MODEL_ERROR
                and item.evidence_ref == intent.intent_id
            ),
            None,
        )
        if previous_model_error is not None:
            return SupervisorResult(
                task_id,
                "model_error",
                turn.turn_id,
                intent.intent_id,
                reason=previous_model_error.short_reason,
            )
        if needs_selection:
            result = await self._runner.run(intent.intent_id)
        else:
            resume = getattr(self._runner, "resume", None)
            if resume is None:
                return SupervisorResult(
                    task_id, "recovery_required", turn.turn_id, intent.intent_id,
                    reason="intent_already_running",
                )
            result = await resume(intent.intent_id)
        if not isinstance(result, IntentRunResult):
            raise SupervisorRecoveryRequired("runner_returned_invalid_result")
        if result.task_run_id != task_id or result.intent_id != intent.intent_id:
            raise SupervisorRecoveryRequired("runner_result_scope_mismatch")
        return await self._apply_result(turn.turn_id, intent.intent_id, result, token)

    async def _apply_result(
        self, turn_id: str, intent_id: str, result: IntentRunResult,
        token: CancellationToken | None,
    ) -> SupervisorResult:
        task_id = self._state.task_run.task_run_id
        if result.outcome == "succeeded":
            intent = next(item for item in self._state.intents if item.intent_id == intent_id)
            if intent.status is not IntentStatus.SUCCEEDED:
                raise SupervisorRecoveryRequired("runner_success_not_committed")
            turn = next(item for item in self._state.turns if item.turn_id == turn_id)
            if turn.status is TurnStatus.RUNNING:
                self._controller.commit_turn_operation(
                    self._work.complete_turn(turn_id), str(uuid4())
                )
            return SupervisorResult(task_id, "turn_completed", turn_id, intent_id)

        if result.outcome == "target_mismatch":
            assert result.plan_id is not None and result.evidence_ref is not None
            operation = self._work.record_target_mismatch(
                plan_id=result.plan_id,
                evidence_ref=result.evidence_ref,
                short_reason=result.reason or "target_mismatch",
            )
            committed = self._controller.commit_turn_operation(operation, str(uuid4()))
            if committed.value == "try_next_intent":
                return SupervisorResult(task_id, "next_intent", turn_id, intent_id)
            if committed.value == "turn_failed":
                if self._generator is None:
                    return SupervisorResult(task_id, "next_turn", turn_id, intent_id,
                                            reason="all_candidates_failed")
                turn = next(item for item in self._state.turns if item.turn_id == turn_id)
                return await self._generate_next(turn.turn_target, token)
            raise SupervisorRecoveryRequired("target_mismatch_left_turn_unclassified")

        if result.outcome == "target_mismatch_committed":
            intent = next(item for item in self._state.intents if item.intent_id == intent_id)
            turn = next(item for item in self._state.turns if item.turn_id == turn_id)
            plan = next((item for item in self._state.plans if item.plan_id == result.plan_id), None)
            execution = next(
                (item for item in self._state.executions if item.execution_id == result.evidence_ref),
                None,
            )
            if (
                intent.status is not IntentStatus.FAILED
                or plan is None or plan.intent_id != intent_id
                or plan.status is not PlanStatus.COMPLETED
                or execution is None or execution.plan_id != plan.plan_id
                or execution.status is not ExecutionStatus.SUCCEEDED
                or not any(
                    item.failure_type is FailureType.TARGET_MISMATCH
                    and item.evidence_ref == result.evidence_ref
                    for item in self._state.failure_summaries
                )
            ):
                raise SupervisorRecoveryRequired("target_mismatch_not_committed")
            if turn.status is TurnStatus.RUNNING:
                return SupervisorResult(task_id, "next_intent", turn_id, intent_id)
            if turn.status is TurnStatus.FAILED:
                if self._generator is None:
                    return SupervisorResult(task_id, "next_turn", turn_id, intent_id,
                                            reason="all_candidates_failed")
                return await self._generate_next(turn.turn_target, token)
            raise SupervisorRecoveryRequired("target_mismatch_turn_status_mismatch")

        if result.outcome == "step_timeout":
            intent = next(item for item in self._state.intents if item.intent_id == intent_id)
            turn = next(item for item in self._state.turns if item.turn_id == turn_id)
            plan = next((item for item in self._state.plans if item.plan_id == result.plan_id), None)
            execution = next(
                (item for item in self._state.executions if item.execution_id == result.evidence_ref),
                None,
            )
            validation = next(
                (
                    item for item in self._state.plan_validation_records
                    if item.plan_validation_id == result.evidence_ref
                ), None,
            )
            def belongs_to_step(reference: str) -> bool:
                return any(
                    item.execution_id == reference and item.plan_id == result.plan_id
                    and item.step_index == 0 and item.status is ExecutionStatus.TIMED_OUT
                    for item in self._state.executions
                ) or any(
                    item.plan_validation_id == reference and item.plan_id == result.plan_id
                    and item.step_index == 0 and item.status is PlanValidationStatus.FAILED
                    and item.failure_type is FailureType.STEP_TIMEOUT
                    for item in self._state.plan_validation_records
                )
            timeouts = [
                item for item in self._state.failure_summaries
                if item.failure_type is FailureType.STEP_TIMEOUT
                and belongs_to_step(item.evidence_ref)
            ]
            if (
                intent.status is not IntentStatus.FAILED
                or plan is None or plan.intent_id != intent_id or plan.status is not PlanStatus.FAILED
                or not (
                    execution is not None and execution.plan_id == plan.plan_id
                    and execution.status is ExecutionStatus.TIMED_OUT
                    and execution.output_ref == execution.execution_id
                    or validation is not None and validation.plan_id == plan.plan_id
                    and validation.status is PlanValidationStatus.FAILED
                    and validation.failure_type is FailureType.STEP_TIMEOUT
                    and validation.command_ref is None
                )
                or len(timeouts) != 2 or timeouts[-1].evidence_ref != result.evidence_ref
            ):
                raise SupervisorRecoveryRequired("step_timeout_not_committed")
            if turn.status is TurnStatus.RUNNING:
                return SupervisorResult(task_id, "next_intent", turn_id, intent_id,
                                        reason="step_timeout_retry_exhausted")
            if turn.status is TurnStatus.INCONCLUSIVE:
                return SupervisorResult(task_id, "paused_inconclusive", turn_id, intent_id,
                                        reason="step_timeout_retry_exhausted")
            raise SupervisorRecoveryRequired("step_timeout_turn_status_mismatch")

        if result.outcome == "inconclusive":
            if result.reason == "fixture_exhausted":
                return SupervisorResult(task_id, "paused_inconclusive", turn_id, intent_id,
                                        reason="fixture_exhausted")
            intent = next(item for item in self._state.intents if item.intent_id == intent_id)
            if intent.status is IntentStatus.RUNNING:
                self._controller.commit_turn_operation(
                    self._work.mark_intent_inconclusive(intent_id=intent_id), str(uuid4())
                )
            return SupervisorResult(task_id, "paused_inconclusive", turn_id, intent_id,
                                    reason=result.reason or "outcome_unknown")

        if result.outcome == "recovery_required":
            # A pending receipt or running Execution is deliberately left
            # untouched.  The existing running Intent/Turn combination is
            # the durable recovery anchor; dispatching another candidate
            # could duplicate an unknown side effect.
            intent = next(item for item in self._state.intents if item.intent_id == intent_id)
            turn = next(item for item in self._state.turns if item.turn_id == turn_id)
            if intent.status is not IntentStatus.RUNNING or turn.status is not TurnStatus.RUNNING:
                raise SupervisorRecoveryRequired("recovery_required_without_running_parents")
            return SupervisorResult(task_id, "recovery_required", turn_id, intent_id,
                                    reason=result.reason or "unfinished_execution")

        if result.outcome == "model_error":
            self._controller.commit_turn_operation(
                self._work.record_model_error(
                    intent_id=intent_id, short_reason=result.reason or "model_error"
                ), str(uuid4()),
            )
            return SupervisorResult(task_id, "model_error", turn_id, intent_id,
                                    reason=result.reason or "model_error")
        if result.outcome == "action_rejected":
            intent = next(item for item in self._state.intents if item.intent_id == intent_id)
            turn = next(item for item in self._state.turns if item.turn_id == turn_id)
            if intent.status is not IntentStatus.RUNNING or turn.status is not TurnStatus.RUNNING:
                raise SupervisorRecoveryRequired("action_rejected_without_running_parents")
            return SupervisorResult(task_id, "action_rejected", turn_id, intent_id,
                                    reason=result.reason or "invalid_t6_completion_action")
        raise SupervisorRecoveryRequired("unsupported_runner_outcome")

    async def _generate_next(
        self, turn_target: str, token: CancellationToken | None
    ) -> SupervisorResult:
        if self._generator is None:
            raise SupervisorRecoveryRequired("turn_generator_not_configured")
        boundary = self._task_boundary(token)
        if boundary is not None:
            return boundary
        task_id = self._state.task_run.task_run_id
        value = await self._generator.generate_next(
            task_id, turn_target, token if token is not None else _NeverCancelled()
        )
        boundary = self._task_boundary(token)
        if boundary is not None:
            return boundary
        if not isinstance(value, IntentGenerationResult):
            raise SupervisorRecoveryRequired("generator_returned_invalid_result")
        if value.model_error is not None:
            if value.model_error.short_reason == "fixture_exhausted":
                return SupervisorResult(task_id, "paused_inconclusive",
                                        reason="fixture_exhausted")
            return SupervisorResult(
                task_id, "model_error", reason=value.model_error.short_reason[:256]
            )
        turn = next(
            (item for item in self._state.turns if item.turn_id == value.turn_id), None
        )
        if turn is None or turn.status is not TurnStatus.PENDING or turn.turn_target != turn_target:
            raise SupervisorRecoveryRequired("generator_did_not_register_expected_turn")
        return SupervisorResult(
            task_id, "next_turn", turn_id=turn.turn_id, generated_next_turn=True
        )

    async def run(self, token: CancellationToken | None = None) -> SupervisorResult:
        """Continue across sibling candidates and generated Turns."""

        generated = False
        while True:
            result = await self.run_once(token)
            generated = generated or result.generated_next_turn
            if result.stop == "next_intent":
                continue
            if result.stop == "turn_completed" and self._generator is not None:
                turn = next(item for item in self._state.turns if item.turn_id == result.turn_id)
                next_turn = await self._generate_next(turn.turn_target, token)
                if next_turn.stop != "next_turn":
                    return next_turn
                generated = True
                continue
            if result.stop == "next_turn" and self._generator is not None:
                if result.generated_next_turn:
                    continue
                turn = next(item for item in self._state.turns if item.turn_id == result.turn_id)
                next_turn = await self._generate_next(turn.turn_target, token)
                if next_turn.stop != "next_turn":
                    return next_turn
                generated = True
                continue
            return SupervisorResult(result.task_run_id, result.stop, result.turn_id,
                                    result.intent_id, generated, result.reason)


__all__ = ["RunSupervisor", "SupervisorRecoveryRequired", "SupervisorResult", "SupervisorStop"]
