from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol


class DomainError(ValueError):
    """Raised when a confirmed core-domain contract is violated."""


class DomainTransitionError(RuntimeError):
    """Raised when a domain operation is invalid in the current state."""


class TurnStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class IntentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class PlanStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class FactStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    REJECTED = "rejected"


MAX_FACT_CONTENT_LENGTH = 256


class PlanValidationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FailureType(StrEnum):
    TARGET_MISMATCH = "target_mismatch"
    ENVIRONMENT_FAULT = "environment_fault"
    POLICY_REJECTED = "policy_rejected"
    PARAMETER_ERROR = "parameter_error"
    OUTCOME_UNKNOWN = "outcome_unknown"
    MODEL_ERROR = "model_error"


class FailureAction(StrEnum):
    RETRY_COMMAND_GENERATION = "retry_command_generation"
    RETRY_EXECUTION = "retry_execution"
    TRY_NEXT_INTENT = "try_next_intent"
    TURN_INCONCLUSIVE = "turn_inconclusive"
    TASK_FAILED = "task_failed"


EXECUTION_TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMED_OUT,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.REJECTED,
    }
)


def _non_empty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field_name} must be a non-empty string")
    return value


def _positive_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class PlanStep:
    description: str

    def __post_init__(self) -> None:
        _non_empty(self.description, "steps[].description")

    @classmethod
    def from_dict(cls, value: Any) -> "PlanStep":
        if not isinstance(value, Mapping):
            raise DomainError("steps must contain objects")
        return cls(description=_non_empty(value.get("description"), "steps[].description"))


@dataclass(slots=True)
class Turn:
    turn_id: str
    task_run_id: str
    sequence: int
    turn_target: str
    intent_ids: tuple[str, ...]
    status: TurnStatus = TurnStatus.PENDING

    def __post_init__(self) -> None:
        _non_empty(self.turn_id, "turn_id")
        _non_empty(self.task_run_id, "task_run_id")
        _positive_integer(self.sequence, "sequence")
        _non_empty(self.turn_target, "turn_target")
        if not isinstance(self.status, TurnStatus):
            raise DomainError("Turn status must be a TurnStatus")
        if not isinstance(self.intent_ids, tuple):
            raise DomainError("intent_ids must be a tuple")
        if any(not isinstance(item, str) or not item.strip() for item in self.intent_ids):
            raise DomainError("intent_ids must contain non-empty strings")
        if len(set(self.intent_ids)) != len(self.intent_ids):
            raise DomainError("intent_ids must be unique within a Turn")


@dataclass(slots=True)
class Intent:
    intent_id: str
    turn_id: str
    description: str
    priority: int
    intent_type: str | None = None
    status: IntentStatus = IntentStatus.PENDING

    def __post_init__(self) -> None:
        _non_empty(self.intent_id, "intent_id")
        _non_empty(self.turn_id, "turn_id")
        _non_empty(self.description, "description")
        _positive_integer(self.priority, "priority")
        if not isinstance(self.status, IntentStatus):
            raise DomainError("Intent status must be an IntentStatus")
        if self.intent_type is not None:
            _non_empty(self.intent_type, "intent_type")


@dataclass(slots=True)
class Plan:
    plan_id: str
    intent_id: str
    steps: tuple[PlanStep, ...]
    status: PlanStatus = PlanStatus.PENDING

    def __post_init__(self) -> None:
        _non_empty(self.plan_id, "plan_id")
        _non_empty(self.intent_id, "intent_id")
        if not isinstance(self.status, PlanStatus):
            raise DomainError("Plan status must be a PlanStatus")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise DomainError("steps must be a non-empty object array")
        if any(not isinstance(step, PlanStep) for step in self.steps):
            raise DomainError("steps must contain PlanStep objects")

    @classmethod
    def from_step_dicts(
        cls,
        *,
        plan_id: str,
        intent_id: str,
        steps: Iterable[Mapping[str, Any]],
    ) -> "Plan":
        return cls(
            plan_id=plan_id,
            intent_id=intent_id,
            steps=tuple(PlanStep.from_dict(item) for item in steps),
        )


@dataclass(slots=True)
class Execution:
    execution_id: str
    plan_id: str
    step_index: int
    command_ref: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    output_ref: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.execution_id, "execution_id")
        _non_empty(self.plan_id, "plan_id")
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int):
            raise DomainError("step_index must be an integer")
        if self.step_index < 0:
            raise DomainError("step_index must be non-negative")
        _non_empty(self.command_ref, "command_ref")
        if not isinstance(self.status, ExecutionStatus):
            raise DomainError("Execution status must be an ExecutionStatus")
        if self.status in EXECUTION_TERMINAL_STATUSES and self.output_ref is None:
            raise DomainError("terminal Execution requires output_ref")
        if self.status not in EXECUTION_TERMINAL_STATUSES and self.output_ref is not None:
            raise DomainError("non-terminal Execution must not have output_ref")
        if self.output_ref is not None:
            _non_empty(self.output_ref, "output_ref")


@dataclass(slots=True)
class Fact:
    fact_id: str
    turn_id: str
    content: str
    source_ref: str
    status: FactStatus = FactStatus.CANDIDATE
    verification_ref: str | None = None

    def __setattr__(self, name: str, value: object) -> None:
        if name == "content":
            _non_empty(value, "content")
            if len(value) > MAX_FACT_CONTENT_LENGTH:
                raise DomainError(
                    "Fact.content must be at most 256 Unicode code points"
                )
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        _non_empty(self.fact_id, "fact_id")
        _non_empty(self.turn_id, "turn_id")
        _non_empty(self.content, "content")
        _non_empty(self.source_ref, "source_ref")
        if self.verification_ref is not None:
            _non_empty(self.verification_ref, "verification_ref")
        if not isinstance(self.status, FactStatus):
            raise DomainError("Fact status must be a FactStatus")


@dataclass(frozen=True, slots=True)
class FailureSummary:
    failure_type: FailureType
    short_reason: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _non_empty(self.short_reason, "short_reason")
        _non_empty(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """Outer-layer facts used by the core's deterministic failure classifier."""

    infrastructure_diagnostic: bool = False
    policy_rejected: bool = False
    parameter_error: bool = False
    model_error: bool = False

    def __post_init__(self) -> None:
        causes = (
            self.infrastructure_diagnostic,
            self.policy_rejected,
            self.parameter_error,
            self.model_error,
        )
        if sum(causes) > 1:
            raise DomainError("FailureObservation must contain at most one cause")


def classify_failure(observation: FailureObservation) -> FailureType:
    if observation.model_error:
        return FailureType.MODEL_ERROR
    if observation.policy_rejected:
        return FailureType.POLICY_REJECTED
    if observation.parameter_error:
        return FailureType.PARAMETER_ERROR
    if observation.infrastructure_diagnostic:
        return FailureType.ENVIRONMENT_FAULT
    return FailureType.OUTCOME_UNKNOWN


@dataclass(frozen=True, slots=True)
class PlanValidationRecord:
    """Immutable evidence from Plan validation or command generation."""

    plan_validation_id: str
    plan_id: str
    task_run_id: str
    step_index: int | None
    attempt: int
    status: PlanValidationStatus
    failure_type: FailureType | None
    short_reason: str
    command_ref: str | None
    observed_at: float

    def __post_init__(self) -> None:
        _non_empty(self.plan_validation_id, "plan_validation_id")
        _non_empty(self.plan_id, "PlanValidationRecord.plan_id")
        _non_empty(self.task_run_id, "PlanValidationRecord.task_run_id")
        if self.step_index is not None:
            if isinstance(self.step_index, bool) or not isinstance(self.step_index, int):
                raise DomainError("PlanValidationRecord.step_index must be an integer or null")
            if self.step_index < 0:
                raise DomainError("PlanValidationRecord.step_index must be non-negative")
        _positive_integer(self.attempt, "PlanValidationRecord.attempt")
        if not isinstance(self.status, PlanValidationStatus):
            raise DomainError("PlanValidationRecord status must be a PlanValidationStatus")
        if self.status is PlanValidationStatus.SUCCEEDED:
            if self.failure_type is not None:
                raise DomainError("successful PlanValidationRecord must not have failure_type")
            if self.step_index is None:
                if self.command_ref is not None:
                    raise DomainError(
                        "Plan-level PlanValidationRecord must not have command_ref"
                    )
            else:
                _non_empty(
                    self.command_ref,
                    "successful step PlanValidationRecord.command_ref",
                )
        elif not isinstance(self.failure_type, FailureType):
            raise DomainError("failed PlanValidationRecord requires a FailureType")
        elif self.command_ref is not None:
            raise DomainError("failed PlanValidationRecord must not have command_ref")
        _non_empty(self.short_reason, "PlanValidationRecord.short_reason")
        if not isinstance(self.observed_at, (int, float)):
            raise DomainError("PlanValidationRecord.observed_at must be numeric")


# The read-only evidence has the same immutable shape as the external record.
PlanValidationEvidence = PlanValidationRecord


class PlanValidationEvidencePort(Protocol):
    """Read-only lookup boundary for Plan validation evidence."""

    def lookup(self, plan_validation_id: str) -> PlanValidationEvidence | None: ...


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    task_run_id: str
    target_ref: str | None
    observed_at: float

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "ExecutionEvidence.task_run_id")
        if self.target_ref is not None:
            _non_empty(self.target_ref, "ExecutionEvidence.target_ref")
        if not isinstance(self.observed_at, (int, float)):
            raise DomainError("ExecutionEvidence.observed_at must be numeric")


class ExecutionEvidencePort(Protocol):
    """Read-only evidence lookup boundary supplied by the execution layer."""

    def command_exists(self, command_ref: str) -> bool: ...

    def output_exists(self, output_ref: str) -> bool: ...

    def lookup(self, execution_id: str) -> ExecutionEvidence | None: ...
