"""Pure v3 mapping from persisted run state and CLI operation result to exit codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TaskRunOutcomeStatus = Literal[
    "running",
    "awaiting_confirmation",
    "awaiting_external_feedback",
    "achieved",
    "assumed_achieved",
    "partial",
    "inconclusive",
    "failed",
    "cancelled",
    "deadline_exceeded",
]
GoalEvaluation = Literal["accepted", "rejected", "unknown", "not_evaluated"]
CleanupState = Literal["not_required", "confirmed", "unconfirmed"]
RunExitCode = Literal[0, 3, 4, 5, 130]
OperationKind = Literal[
    "run", "resume", "status", "replay", "evidence", "cancel", "cleanup", "export"
]
OperationState = Literal[
    "completed", "refused", "failed", "paused", "unknown", "export_failed"
]
OperationExitCode = Literal[0, 2, 3, 4, 5, 130]
RunOutcomeReason = Literal[
    "unknown_operation",
    "recovery_required",
    "cleanup_unconfirmed",
    "terminal_cleanup_not_required",
    "goal_taskrun_mismatch",
    "accepted_goal_achieved",
    "assumed_goal_ended",
    "cancelled",
    "deadline_exceeded",
    "terminal_error",
    "unfinished",
]

_TASK_RUN_STATUSES = frozenset(
    {
        "running",
        "awaiting_confirmation",
        "awaiting_external_feedback",
        "achieved",
        "assumed_achieved",
        "partial",
        "inconclusive",
        "failed",
        "cancelled",
        "deadline_exceeded",
    }
)
_GOAL_EVALUATIONS = frozenset({"accepted", "rejected", "unknown", "not_evaluated"})
_CLEANUP_STATES = frozenset({"not_required", "confirmed", "unconfirmed"})
_RUN_EXIT_CODES = frozenset({0, 3, 4, 5, 130})
_OPERATION_KINDS = frozenset(
    {"run", "resume", "status", "replay", "evidence", "cancel", "cleanup", "export"}
)
_OPERATION_STATES = frozenset(
    {"completed", "refused", "failed", "paused", "unknown", "export_failed"}
)
_OPERATION_EXIT_CODES = frozenset({0, 2, 3, 4, 5, 130})
_RUN_OUTCOME_REASONS = frozenset(
    {
        "unknown_operation",
        "recovery_required",
        "cleanup_unconfirmed",
        "terminal_cleanup_not_required",
        "goal_taskrun_mismatch",
        "accepted_goal_achieved",
        "assumed_goal_ended",
        "cancelled",
        "deadline_exceeded",
        "terminal_error",
        "unfinished",
    }
)
_TERMINAL_TASK_RUN_STATUSES = frozenset(
    {
        "achieved",
        "assumed_achieved",
        "partial",
        "inconclusive",
        "failed",
        "cancelled",
        "deadline_exceeded",
    }
)


@dataclass(frozen=True, slots=True)
class D7RunOutcomeState:
    """Only evaluator, TaskRun, operation-recovery, and cleanup state affect this mapping."""

    task_run_status: TaskRunOutcomeStatus
    goal_evaluation: GoalEvaluation
    cleanup_state: CleanupState
    unresolved_operation_unknown: bool = False
    recovery_required: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.task_run_status, str)
            or self.task_run_status not in _TASK_RUN_STATUSES
        ):
            raise ValueError("unsupported TaskRun outcome status")
        if (
            not isinstance(self.goal_evaluation, str)
            or self.goal_evaluation not in _GOAL_EVALUATIONS
        ):
            raise ValueError("unsupported independent goal evaluation")
        if (
            not isinstance(self.cleanup_state, str)
            or self.cleanup_state not in _CLEANUP_STATES
        ):
            raise ValueError("unsupported cleanup state")
        if type(self.unresolved_operation_unknown) is not bool:
            raise ValueError("unresolved_operation_unknown must be boolean")
        if type(self.recovery_required) is not bool:
            raise ValueError("recovery_required must be boolean")


@dataclass(frozen=True, slots=True)
class D7RunOutcome:
    run_exit_code: RunExitCode
    reason: RunOutcomeReason

    def __post_init__(self) -> None:
        if (
            type(self.run_exit_code) is not int
            or self.run_exit_code not in _RUN_EXIT_CODES
        ):
            raise ValueError("run_exit_code is not a persistent run outcome code")
        if not isinstance(self.reason, str) or self.reason not in _RUN_OUTCOME_REASONS:
            raise ValueError("unsupported run outcome reason")

    def to_dict(self) -> dict[str, object]:
        return {"run_exit_code": self.run_exit_code, "reason": self.reason}


def map_run_outcome(state: D7RunOutcomeState) -> D7RunOutcome:
    """Compute the persistent run result; candidate/Turn state is not an input."""
    if state.unresolved_operation_unknown:
        return D7RunOutcome(5, "unknown_operation")
    if state.recovery_required:
        return D7RunOutcome(5, "recovery_required")
    if state.cleanup_state == "unconfirmed":
        return D7RunOutcome(5, "cleanup_unconfirmed")
    if (
        state.task_run_status in _TERMINAL_TASK_RUN_STATUSES
        and state.cleanup_state == "not_required"
    ):
        return D7RunOutcome(5, "terminal_cleanup_not_required")
    if state.task_run_status == "achieved":
        if state.goal_evaluation != "accepted":
            return D7RunOutcome(5, "goal_taskrun_mismatch")
        return D7RunOutcome(0, "accepted_goal_achieved")
    if state.task_run_status == "assumed_achieved":
        return D7RunOutcome(0, "assumed_goal_ended")
    if state.task_run_status == "cancelled":
        return D7RunOutcome(130, "cancelled")
    if state.task_run_status == "deadline_exceeded":
        return D7RunOutcome(3, "deadline_exceeded")
    if state.task_run_status == "failed":
        return D7RunOutcome(3, "terminal_error")
    return D7RunOutcome(4, "unfinished")


@dataclass(frozen=True, slots=True)
class D7OperationResult:
    """One CLI operation result plus the separate persisted run result, if available."""

    operation: OperationKind
    state: OperationState
    run_exit_code: RunExitCode | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation, str)
            or self.operation not in _OPERATION_KINDS
        ):
            raise ValueError("unsupported CLI operation")
        if not isinstance(self.state, str) or self.state not in _OPERATION_STATES:
            raise ValueError("unsupported CLI operation result")
        if self.run_exit_code is not None and (
            type(self.run_exit_code) is not int
            or self.run_exit_code not in _RUN_EXIT_CODES
        ):
            raise ValueError("run_exit_code is not a persistent run outcome code")
        if (
            self.operation in {"run", "resume"}
            and self.state == "completed"
            and self.run_exit_code is None
        ):
            raise ValueError("completed run/resume requires run_exit_code")
        if self.state == "export_failed":
            if self.operation not in {"run", "resume", "export"}:
                raise ValueError("export failure is not valid for this operation")
            if self.run_exit_code is None:
                raise ValueError("export failure must preserve run_exit_code")


@dataclass(frozen=True, slots=True)
class D7OperationExit:
    exit_code: OperationExitCode
    run_exit_code: RunExitCode | None

    def __post_init__(self) -> None:
        if (
            type(self.exit_code) is not int
            or self.exit_code not in _OPERATION_EXIT_CODES
        ):
            raise ValueError("unsupported CLI operation exit code")
        if self.run_exit_code is not None and (
            type(self.run_exit_code) is not int
            or self.run_exit_code not in _RUN_EXIT_CODES
        ):
            raise ValueError("run_exit_code is not a persistent run outcome code")

    def to_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "run_exit_code": self.run_exit_code,
        }


def map_operation_exit(result: D7OperationResult) -> D7OperationExit:
    """Map this operation without changing the separately supplied persisted result."""
    if result.state == "completed":
        if result.operation in {"run", "resume"}:
            code = result.run_exit_code
            if code is None:
                raise ValueError("completed run/resume requires run_exit_code")
            return D7OperationExit(code, code)
        return D7OperationExit(0, result.run_exit_code)
    if result.state == "refused":
        return D7OperationExit(2, result.run_exit_code)
    if result.state in {"failed", "export_failed"}:
        return D7OperationExit(3, result.run_exit_code)
    if result.state == "paused":
        return D7OperationExit(4, result.run_exit_code)
    return D7OperationExit(5, result.run_exit_code)


__all__ = [
    "D7OperationExit",
    "D7OperationResult",
    "D7RunOutcome",
    "D7RunOutcomeState",
    "map_operation_exit",
    "map_run_outcome",
]
