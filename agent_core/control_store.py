from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias, cast

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.conclusion_contracts import D8Conclusion
from agent_core.handoff_contracts import D8HandoffVersion, D8CapacityVersion, D8TreeRecord
from agent_core.conclusion_revision_contracts import D8RevisionRecord
from agent_core.session_contracts import D8DecisionRecord
from agent_core.handoff_storage import D8_CHANGES, D8_OPERATIONS
from agent_core.web_contracts import (
    D7WebExecutionRecord,
    D7WebPlanRecord,
    D7WebRunConfig,
    canonical_web_origin,
)
from agent_core.domain import (
    Execution,
    ExecutionStatus,
    Fact,
    FactStatus,
    FailureSummary,
    FailureType,
    Intent,
    IntentStatus,
    Plan,
    PlanStatus,
    PlanStep,
    PlanValidationRecord,
    PlanValidationStatus,
    Turn,
    TurnStatus,
)
from agent_core.dual_llm_contracts import (
    D3CommitMetadata,
    D3ReviewApplication,
    d3_payload,
)
from agent_core.file_type_fact import FACT_PREFIX
from agent_core.goal_candidates import D7CommitMetadata, D7WebExecutionBinding
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.process_facts import D5ProcessFact
from agent_core.review_contracts import (
    D2AllowedActionKinds,
    D2Constraint,
    D2MaxActionTimeout,
    d2_payload,
)
from agent_core.runtime import RuntimeEvent, TaskRunStatus
from agent_core.turn_contracts import (
    D1RunConfig,
    D1TurnBinding,
    D1TurnProof,
    d1_payload,
)


class StorageError(RuntimeError):
    """Base error exposed by a ControlStore implementation."""


class IdempotencyConflictError(StorageError):
    """The same mutation ID was reused for different request content."""


class StaleControlStateError(StorageError):
    """A new mutation was calculated from an obsolete checkpoint."""


class TaskRunNotFoundError(StorageError):
    """A required TaskRun does not exist."""


def _non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _finite_timestamp(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a numeric UTC Unix timestamp")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _uuid4(value: object, field_name: str) -> str:
    text = _non_empty(value, field_name)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID4 string") from exc
    if parsed.version != 4 or str(parsed) != text.lower():
        raise ValueError(f"{field_name} must be a canonical UUID4 string")
    return text


def canonical_json(value: object) -> str:
    """Encode confirmed control-plane data with one deterministic JSON form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def task_request_snapshot(
    request: TaskRequest,
    effective_model_backend: ModelBackend,
) -> dict[str, object]:
    return {
        "effective_model_backend": effective_model_backend.value,
        "request": task_request_payload(request),
    }


def task_request_payload(request: TaskRequest) -> dict[str, object]:
    return {
        "deadline_seconds": request.deadline_seconds,
        "goal": request.goal,
        "target": request.target.to_dict(),
    }


@dataclass(frozen=True, slots=True)
class TargetNetworkBinding:
    private_network_id: str
    egress_network_id: str
    forwarder_id: str
    configuration_sha256: str
    target_url: str

    def __post_init__(self) -> None:
        for name in (
            "private_network_id",
            "egress_network_id",
            "forwarder_id",
            "configuration_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"TargetNetworkBinding.{name} must be SHA-256 hex")
        _non_empty(self.target_url, "TargetNetworkBinding.target_url")

    def to_dict(self) -> dict[str, str]:
        return {
            "private_network_id": self.private_network_id,
            "egress_network_id": self.egress_network_id,
            "forwarder_id": self.forwarder_id,
            "configuration_sha256": self.configuration_sha256,
            "target_url": self.target_url,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TargetNetworkBinding:
        fields = {
            "private_network_id",
            "egress_network_id",
            "forwarder_id",
            "configuration_sha256",
            "target_url",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("invalid TargetNetworkBinding fields")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class UpsertTaskRun:
    task_run_id: str
    request: TaskRequest
    effective_model_backend: ModelBackend
    started_at: float
    deadline_at: float
    status: TaskRunStatus
    sandbox_id: str
    confirmation_deadline: float | None = None
    finished_at: float | None = None
    failure_reason: str | None = None
    source_root: str | None = None
    target_network: TargetNetworkBinding | None = None

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "UpsertTaskRun.task_run_id")
        if not isinstance(self.request, TaskRequest):
            raise ValueError("UpsertTaskRun.request must be a TaskRequest")
        if self.source_root is not None and (
            not isinstance(self.source_root, str)
            or not self.source_root
            or "\x00" in self.source_root
        ):
            raise ValueError("UpsertTaskRun.source_root must be a path or None")
        if self.target_network is not None and (
            not isinstance(self.target_network, TargetNetworkBinding)
            or tuple(item.url for item in self.request.target.endpoints)
            != (self.target_network.target_url,)
        ):
            raise ValueError("UpsertTaskRun target network must match its sole endpoint")
        if not isinstance(self.effective_model_backend, ModelBackend):
            raise ValueError(
                "UpsertTaskRun.effective_model_backend must be a ModelBackend"
            )
        if (
            self.request.model_backend is not None
            and self.request.model_backend is not self.effective_model_backend
        ):
            raise ValueError(
                "UpsertTaskRun effective_model_backend conflicts with TaskRequest"
            )
        _finite_timestamp(self.started_at, "UpsertTaskRun.started_at")
        _finite_timestamp(self.deadline_at, "UpsertTaskRun.deadline_at")
        if self.deadline_at != self.started_at + self.request.deadline_seconds:
            raise ValueError("UpsertTaskRun.deadline_at must match TaskRequest")
        if not isinstance(self.status, TaskRunStatus):
            raise ValueError("UpsertTaskRun.status must be a TaskRunStatus")
        _non_empty(self.sandbox_id, "UpsertTaskRun.sandbox_id")
        if self.confirmation_deadline is not None:
            _finite_timestamp(
                self.confirmation_deadline,
                "UpsertTaskRun.confirmation_deadline",
            )
        if self.finished_at is not None:
            _finite_timestamp(self.finished_at, "UpsertTaskRun.finished_at")
        if self.failure_reason is not None:
            _non_empty(self.failure_reason, "UpsertTaskRun.failure_reason")


@dataclass(frozen=True, slots=True)
class UpsertTurn:
    turn_id: str
    task_run_id: str
    sequence: int
    turn_target: str
    intent_ids: tuple[str, ...]
    status: TurnStatus
    parent_turn_id: str | None = None
    triggering_intent_id: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.turn_id, "UpsertTurn.turn_id")
        _non_empty(self.task_run_id, "UpsertTurn.task_run_id")
        _positive_integer(self.sequence, "UpsertTurn.sequence")
        _non_empty(self.turn_target, "UpsertTurn.turn_target")
        if (self.parent_turn_id is None) != (self.triggering_intent_id is None):
            raise ValueError(
                "UpsertTurn parent_turn_id and triggering_intent_id must be set together"
            )
        if self.parent_turn_id is not None:
            _non_empty(self.parent_turn_id, "UpsertTurn.parent_turn_id")
            _non_empty(self.triggering_intent_id, "UpsertTurn.triggering_intent_id")
        if not isinstance(self.intent_ids, tuple):
            raise ValueError("UpsertTurn.intent_ids must be a tuple")
        if any(not isinstance(item, str) or not item.strip() for item in self.intent_ids):
            raise ValueError("UpsertTurn.intent_ids must contain non-empty strings")
        if len(self.intent_ids) != len(set(self.intent_ids)):
            raise ValueError("UpsertTurn.intent_ids must be unique")
        if not isinstance(self.status, TurnStatus):
            raise ValueError("UpsertTurn.status must be a TurnStatus")


@dataclass(frozen=True, slots=True)
class UpsertIntent:
    intent_id: str
    turn_id: str
    description: str
    priority: int
    intent_type: str | None
    status: IntentStatus

    def __post_init__(self) -> None:
        _non_empty(self.intent_id, "UpsertIntent.intent_id")
        _non_empty(self.turn_id, "UpsertIntent.turn_id")
        _non_empty(self.description, "UpsertIntent.description")
        _positive_integer(self.priority, "UpsertIntent.priority")
        if self.intent_type is not None:
            _non_empty(self.intent_type, "UpsertIntent.intent_type")
        if not isinstance(self.status, IntentStatus):
            raise ValueError("UpsertIntent.status must be an IntentStatus")


@dataclass(frozen=True, slots=True)
class UpsertIntentGoal:
    goal: FileTypeIntentGoal

    def __post_init__(self) -> None:
        if not isinstance(self.goal, FileTypeIntentGoal):
            raise ValueError("UpsertIntentGoal.goal must be a FileTypeIntentGoal")


@dataclass(frozen=True, slots=True)
class UpsertPlan:
    plan_id: str
    intent_id: str
    steps: tuple[PlanStep, ...]
    status: PlanStatus

    def __post_init__(self) -> None:
        _non_empty(self.plan_id, "UpsertPlan.plan_id")
        _non_empty(self.intent_id, "UpsertPlan.intent_id")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ValueError("UpsertPlan.steps must be a non-empty tuple")
        if any(not isinstance(step, PlanStep) for step in self.steps):
            raise ValueError("UpsertPlan.steps must contain PlanStep values")
        if not isinstance(self.status, PlanStatus):
            raise ValueError("UpsertPlan.status must be a PlanStatus")


@dataclass(frozen=True, slots=True)
class UpsertExecution:
    execution_id: str
    plan_id: str
    step_index: int
    command_ref: str
    status: ExecutionStatus
    output_ref: str | None

    def __post_init__(self) -> None:
        _non_empty(self.execution_id, "UpsertExecution.execution_id")
        _non_empty(self.plan_id, "UpsertExecution.plan_id")
        _non_negative_integer(self.step_index, "UpsertExecution.step_index")
        _non_empty(self.command_ref, "UpsertExecution.command_ref")
        if not isinstance(self.status, ExecutionStatus):
            raise ValueError("UpsertExecution.status must be an ExecutionStatus")
        terminal = self.status in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.REJECTED,
        }
        if terminal and self.output_ref is None:
            raise ValueError("terminal UpsertExecution requires output_ref")
        if not terminal and self.output_ref is not None:
            raise ValueError("non-terminal UpsertExecution must not have output_ref")
        if self.output_ref is not None:
            _non_empty(self.output_ref, "UpsertExecution.output_ref")


@dataclass(frozen=True, slots=True)
class UpsertFact:
    fact_id: str
    turn_id: str
    content: str
    source_ref: str
    status: FactStatus
    verification_ref: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.fact_id, "UpsertFact.fact_id")
        _non_empty(self.turn_id, "UpsertFact.turn_id")
        _non_empty(self.content, "UpsertFact.content")
        if len(self.content) > 256:
            raise ValueError("UpsertFact.content must be at most 256 Unicode code points")
        _non_empty(self.source_ref, "UpsertFact.source_ref")
        if self.verification_ref is not None:
            _non_empty(self.verification_ref, "UpsertFact.verification_ref")
        if not isinstance(self.status, FactStatus):
            raise ValueError("UpsertFact.status must be a FactStatus")


@dataclass(frozen=True, slots=True)
class AppendFailureSummary:
    failure_type: FailureType
    short_reason: str
    evidence_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_type, FailureType):
            raise ValueError("AppendFailureSummary.failure_type must be a FailureType")
        _non_empty(self.short_reason, "AppendFailureSummary.short_reason")
        if len(self.short_reason) > 256:
            raise ValueError(
                "AppendFailureSummary.short_reason must be at most 256 Unicode code points"
            )
        _non_empty(self.evidence_ref, "AppendFailureSummary.evidence_ref")


@dataclass(frozen=True, slots=True)
class PlanValidationRecordDraft:
    plan_validation_id: str
    plan_id: str
    task_run_id: str
    step_index: int | None
    attempt: int
    status: PlanValidationStatus
    failure_type: FailureType | None
    short_reason: str
    command_ref: str | None

    def __post_init__(self) -> None:
        _non_empty(
            self.plan_validation_id,
            "PlanValidationRecordDraft.plan_validation_id",
        )
        _non_empty(self.plan_id, "PlanValidationRecordDraft.plan_id")
        _non_empty(self.task_run_id, "PlanValidationRecordDraft.task_run_id")
        if self.step_index is not None:
            _non_negative_integer(
                self.step_index,
                "PlanValidationRecordDraft.step_index",
            )
        _positive_integer(self.attempt, "PlanValidationRecordDraft.attempt")
        if not isinstance(self.status, PlanValidationStatus):
            raise ValueError(
                "PlanValidationRecordDraft.status must be a PlanValidationStatus"
            )
        if self.status is PlanValidationStatus.SUCCEEDED:
            if self.failure_type is not None:
                raise ValueError(
                    "successful PlanValidationRecordDraft must not have failure_type"
                )
            if self.step_index is None:
                if self.command_ref is not None:
                    raise ValueError(
                        "Plan-level PlanValidationRecordDraft must not have command_ref"
                    )
            else:
                _non_empty(
                    self.command_ref,
                    "successful step PlanValidationRecordDraft.command_ref",
                )
        elif not isinstance(self.failure_type, FailureType):
            raise ValueError(
                "failed PlanValidationRecordDraft requires a FailureType"
            )
        elif self.command_ref is not None:
            raise ValueError(
                "failed PlanValidationRecordDraft must not have command_ref"
            )
        _non_empty(self.short_reason, "PlanValidationRecordDraft.short_reason")


@dataclass(frozen=True, slots=True)
class AppendPlanValidationRecord:
    record: PlanValidationRecordDraft

    def __post_init__(self) -> None:
        if not isinstance(self.record, PlanValidationRecordDraft):
            raise ValueError(
                "AppendPlanValidationRecord.record must be a PlanValidationRecordDraft"
            )


@dataclass(frozen=True, slots=True)
class D2RunConfig:
    review_role_ref: str
    chain_id: str = "dual_llm_d2"
    contract_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.review_role_ref, str)
            or not self.review_role_ref.startswith("sha256:")
            or len(self.review_role_ref) != 71
            or any(char not in "0123456789abcdef" for char in self.review_role_ref[7:])
        ):
            raise ValueError("D2 review role must have an immutable SHA-256 reference")
        if self.chain_id != "dual_llm_d2" or self.contract_version != 1:
            raise ValueError("unsupported D2 chain configuration")


@dataclass(frozen=True, slots=True)
class AppendD2ReviewApplication:
    """Immutable control-plane application of one accepted D2 review."""

    review_root_id: str
    review_generation: int
    review_id: str
    application_ref: str
    context_version: str
    constraint_version: int
    output_constraints: tuple[D2Constraint, ...]
    paused_gate: bool
    turn_id: str
    intent_id: str
    review_scope: Literal["turn_start", "action", "turn_end"]
    action_candidate_id: str | None = None
    pause_reason: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.review_root_id, "AppendD2ReviewApplication.review_root_id")
        _positive_integer(self.review_generation, "AppendD2ReviewApplication.review_generation")
        _non_empty(self.review_id, "AppendD2ReviewApplication.review_id")
        _non_empty(self.application_ref, "AppendD2ReviewApplication.application_ref")
        _non_empty(self.context_version, "AppendD2ReviewApplication.context_version")
        _non_negative_integer(self.constraint_version, "AppendD2ReviewApplication.constraint_version")
        _non_empty(self.turn_id, "AppendD2ReviewApplication.turn_id")
        _non_empty(self.intent_id, "AppendD2ReviewApplication.intent_id")
        if self.review_scope not in {"turn_start", "action", "turn_end"}:
            raise ValueError("unsupported D2 review application scope")
        if (self.review_scope == "action") != (self.action_candidate_id is not None):
            raise ValueError("D2 action review must bind exactly one action candidate")
        if self.action_candidate_id is not None:
            _non_empty(self.action_candidate_id, "AppendD2ReviewApplication.action_candidate_id")
        if not isinstance(self.output_constraints, tuple) or any(
            not isinstance(item, (D2AllowedActionKinds, D2MaxActionTimeout))
            for item in self.output_constraints
        ):
            raise ValueError("AppendD2ReviewApplication.output_constraints must be D2 constraints")
        if type(self.paused_gate) is not bool:
            raise ValueError("AppendD2ReviewApplication.paused_gate must be bool")
        if self.paused_gate:
            _non_empty(self.pause_reason, "AppendD2ReviewApplication.pause_reason")
            if self.output_constraints:
                raise ValueError("a paused D2 gate cannot apply new constraints")
        elif self.pause_reason is not None:
            raise ValueError("a continuing D2 application has no pause reason")
        if self.review_scope == "turn_end" and self.output_constraints:
            raise ValueError("D2 turn_end application cannot add constraints")


def active_d2_pause_gates(
    applications: tuple[AppendD2ReviewApplication, ...],
) -> tuple[AppendD2ReviewApplication, ...]:
    """Known application failures resolve only through their own trusted application."""
    return tuple(
        item for item in applications
        if item.paused_gate and not (
            item.pause_reason == "review_application_uncommitted"
            and any(
                not applied.paused_gate
                and applied.review_root_id == item.review_root_id
                and applied.review_generation >= item.review_generation
                for applied in applications
            )
        )
    )


StateChange: TypeAlias = (
    UpsertTaskRun
    | UpsertTurn
    | UpsertIntent
    | UpsertIntentGoal
    | UpsertPlan
    | UpsertExecution
    | UpsertFact
    | AppendFailureSummary
    | AppendPlanValidationRecord
    | AppendD2ReviewApplication
    | D3ReviewApplication
    | D5ProcessFact
    | D8Conclusion
    | D8HandoffVersion
    | D8CapacityVersion
    | D8TreeRecord
    | D8RevisionRecord
    | D8DecisionRecord
)

STATE_CHANGE_TYPES = (
    UpsertTaskRun,
    UpsertTurn,
    UpsertIntent,
    UpsertIntentGoal,
    UpsertPlan,
    UpsertExecution,
    UpsertFact,
    AppendFailureSummary,
    AppendPlanValidationRecord,
    AppendD2ReviewApplication,
    D3ReviewApplication,
    D5ProcessFact,
    D8Conclusion,
    D8HandoffVersion,
    D8CapacityVersion,
    D8TreeRecord,
    D8RevisionRecord,
    D8DecisionRecord,
)


@dataclass(frozen=True, slots=True)
class TurnRegistration:
    """Prepared first scheduling mutation for one fixed Turn and Intent list."""

    task_run_id: str
    base_checkpoint_sequence: int
    turn_id: str
    state_changes: tuple[StateChange, ...]
    parent_turn_id: str | None = None
    triggering_intent_id: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "TurnRegistration.task_run_id")
        _non_negative_integer(
            self.base_checkpoint_sequence,
            "TurnRegistration.base_checkpoint_sequence",
        )
        _non_empty(self.turn_id, "TurnRegistration.turn_id")
        if not isinstance(self.state_changes, tuple) or len(self.state_changes) < 2:
            raise ValueError(
                "TurnRegistration must contain one Turn and a non-empty Intent list"
            )

        turn, *intents = self.state_changes
        if not isinstance(turn, UpsertTurn):
            raise ValueError("TurnRegistration must begin with UpsertTurn")
        if any(not isinstance(item, UpsertIntent) for item in intents):
            raise ValueError("TurnRegistration may only contain UpsertIntent after Turn")
        if turn.task_run_id != self.task_run_id or turn.turn_id != self.turn_id:
            raise ValueError("TurnRegistration Turn belongs to another TaskRun")
        if (self.parent_turn_id is None) != (self.triggering_intent_id is None):
            raise ValueError(
                "TurnRegistration parent_turn_id and triggering_intent_id must be set together"
            )
        if (turn.parent_turn_id, turn.triggering_intent_id) != (
            self.parent_turn_id,
            self.triggering_intent_id,
        ):
            raise ValueError("TurnRegistration parent metadata differs from its Turn")
        if turn.status is not TurnStatus.PENDING:
            raise ValueError("TurnRegistration Turn must begin pending")

        typed_intents = cast(tuple[UpsertIntent, ...], tuple(intents))
        intent_ids = tuple(item.intent_id for item in typed_intents)
        if turn.intent_ids != intent_ids:
            raise ValueError("TurnRegistration intent_ids must match its Intents")
        if any(item.turn_id != turn.turn_id for item in typed_intents):
            raise ValueError("TurnRegistration Intent belongs to another Turn")
        if any(item.status is not IntentStatus.PENDING for item in typed_intents):
            raise ValueError("TurnRegistration Intents must begin pending")
        if tuple(item.priority for item in typed_intents) != tuple(
            range(1, len(typed_intents) + 1)
        ):
            raise ValueError("TurnRegistration Intent priorities must be ordered from one")

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Event references, preserving the fixed priority order."""

        return (self.turn_id,) + tuple(
            item.intent_id
            for item in self.state_changes
            if isinstance(item, UpsertIntent)
        )


@dataclass(frozen=True, slots=True)
class OperationResultRule:
    event_type: str
    allowed_results: frozenset[str] = frozenset()
    dynamic_result_entity: str | None = None
    result_required: bool = True

    def __post_init__(self) -> None:
        _non_empty(self.event_type, "OperationResultRule.event_type")
        if self.allowed_results and self.dynamic_result_entity is not None:
            raise ValueError(
                "OperationResultRule cannot mix fixed and dynamic results"
            )
        if not self.allowed_results and self.dynamic_result_entity is None:
            raise ValueError("OperationResultRule must define a result contract")
        if any(not isinstance(item, str) or not item.strip() for item in self.allowed_results):
            raise ValueError(
                "OperationResultRule.allowed_results must contain non-empty strings"
            )
        if self.dynamic_result_entity is not None:
            _non_empty(
                self.dynamic_result_entity,
                "OperationResultRule.dynamic_result_entity",
            )


TURN_OPERATIONS = frozenset({
    "select_next_intent", "register_plan", "start_plan", "complete_plan",
    "append_plan_validation_record", "create_execution", "start_execution",
    "complete_execution", "create_fact", "verify_fact", "succeed_intent_with_fact",
    "complete_turn", "record_command_generation_failure", "record_execution_failure",
    "record_step_timeout", "record_target_mismatch", "mark_intent_inconclusive",
    "record_model_error", "finish_task_run",
})


@dataclass(frozen=True, slots=True)
class TurnOperation:
    """Immutable domain delta; the Controller supplies the event, clock and commit."""

    task_run_id: str
    base_checkpoint_sequence: int
    operation: str
    state_changes: tuple[StateChange, ...]
    entity_ids: tuple[str, ...]
    result: str | None

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "TurnOperation.task_run_id")
        _non_negative_integer(self.base_checkpoint_sequence, "TurnOperation.base_checkpoint_sequence")
        if self.operation not in TURN_OPERATIONS:
            raise ValueError("unsupported TurnOperation.operation")
        if not isinstance(self.state_changes, tuple) or not self.state_changes:
            raise ValueError("TurnOperation requires typed state changes")
        if any(not isinstance(item, STATE_CHANGE_TYPES) for item in self.state_changes):
            raise ValueError("TurnOperation contains an unsupported state change")
        if not isinstance(self.entity_ids, tuple):
            raise ValueError("TurnOperation.entity_ids must be a tuple")
        task_changes = [item for item in self.state_changes if isinstance(item, UpsertTaskRun)]
        if len(task_changes) > 1:
            raise ValueError("TurnOperation may change at most one TaskRun")
        if task_changes:
            task = task_changes[0]
            if task.task_run_id != self.task_run_id:
                raise ValueError("TurnOperation TaskRun belongs to another task")
            if self.operation == "finish_task_run":
                if task.status not in {TaskRunStatus.PARTIAL, TaskRunStatus.INCONCLUSIVE}:
                    raise ValueError("Turn conclusion must be partial or inconclusive")
            elif self.operation not in {
                "record_execution_failure",
                "record_command_generation_failure",
                "record_model_error",
            } or (
                task.status is not TaskRunStatus.FAILED or self.result != "task_failed"
            ):
                raise ValueError("only model failure may terminate a TaskRun in a Turn operation")
        elif self.operation == "finish_task_run" or self.result == "task_failed":
            raise ValueError("terminal TurnOperation requires its TaskRun state change")
        validate_control_event(self.operation, self.state_changes, self.event(timestamp=0.0))

    def event(self, *, timestamp: float) -> RuntimeEvent:
        details: dict[str, object] = {
            "operation": self.operation, "entity_ids": list(self.entity_ids),
        }
        if self.result is not None:
            details["result"] = self.result
        status = next(
            (item.status for item in self.state_changes if isinstance(item, UpsertTaskRun)),
            TaskRunStatus.RUNNING,
        )
        return RuntimeEvent(
            type=CONTROL_OPERATION_RULES[self.operation].event_type,
            task_run_id=self.task_run_id, timestamp=timestamp, status=status, details=details,
        )


_CONTROL_EVENT_TYPE = "control_state_committed"

CONTROL_OPERATION_RULES: Mapping[str, OperationResultRule] = MappingProxyType(
    {
        "start_task_run": OperationResultRule(
            event_type="task_run_started",
            dynamic_result_entity="task_run",
        ),
        "request_completion_confirmation": OperationResultRule(
            event_type="completion_confirmation_requested",
            allowed_results=frozenset({"awaiting_confirmation"}),
        ),
        "register_d7_candidate": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"awaiting_external_feedback"}),
        ),
        "apply_external_validation_feedback": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"accepted", "rejected", "unknown"}),
        ),
        "start_external_validation_check": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"check_started"}),
        ),
        "complete_external_validation_check": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"accepted", "rejected", "unknown"}),
        ),
        "finish_task_run": OperationResultRule(
            event_type="task_run_finished",
            allowed_results=frozenset(
                {
                    "achieved",
                    "assumed_achieved",
                    "partial",
                    "inconclusive",
                    "failed",
                    "cancelled",
                    "deadline_exceeded",
                }
            ),
        ),
        "select_next_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="running_intent",
            result_required=False,
        ),
        "register_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="turn",
        ),
        "register_plan": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="plan",
        ),
        "bind_intent_goal": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="intent_goal",
        ),
        "start_plan": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"running"}),
        ),
        "complete_plan": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"completed"}),
        ),
        "append_plan_validation_record": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="plan_validation",
        ),
        "create_execution": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="execution",
        ),
        "start_execution": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"running"}),
        ),
        "complete_execution": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"succeeded"}),
        ),
        "create_fact": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="fact",
        ),
        "commit_verified_file_type_fact": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="fact",
        ),
        "commit_verified_t6_observation": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="fact",
        ),
        "commit_verified_t6_fact": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="fact",
        ),
        "verify_fact": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"verified", "rejected"}),
        ),
        "succeed_intent_with_fact": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"succeeded"}),
        ),
        "complete_satisfied_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"succeeded"}),
        ),
        "complete_verified_t6_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"succeeded"}),
        ),
        "complete_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"completed"}),
        ),
        "complete_verified_d1_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"completed"}),
        ),
        "apply_d2_review": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"applied", "paused"}),
        ),
        "register_d3_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="turn",
        ),
        "select_d3_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="running_intent",
        ),
        "apply_d3_review": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"applied", "paused"}),
        ),
        "append_process_fact": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"applied"}),
        ),
        "append_d8_conclusion": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"reviewed_inference", "rejected", "insufficient"}),
        ),
        "register_d3_plan": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="plan",
        ),
        "replan_d3_plan": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="plan",
        ),
        "complete_d3_plan": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"satisfied", "not_satisfied"}),
        ),
        "finish_d3_execution": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="execution",
        ),
        "complete_d3_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"succeeded"}),
        ),
        "switch_d3_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="running_intent",
        ),
        "abandon_d8_intent": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"abandoned"}),
        ),
        "complete_d3_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"completed"}),
        ),
        "advance_d3_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            dynamic_result_entity="turn",
        ),
        "exhaust_d3_turn": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"failed", "inconclusive"}),
        ),
        "record_command_generation_failure": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset(
                {
                    "retry_command_generation",
                    "retry_step_command",
                    "try_next_intent",
                    "turn_failed",
                    "turn_inconclusive",
                    "model_error",
                    "task_failed",
                }
            ),
        ),
        "record_execution_failure": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset(
                {
                    "retry_execution",
                    "recovery_required",
                    "try_next_intent",
                    "turn_failed",
                    "turn_inconclusive",
                    "model_error",
                    "task_failed",
                }
            ),
        ),
        "record_step_timeout": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset(
                {
                    "retry_step_command",
                    "try_next_intent",
                    "turn_failed",
                    "turn_inconclusive",
                }
            ),
        ),
        "record_target_mismatch": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset(
                {"try_next_intent", "turn_failed", "turn_inconclusive"}
            ),
        ),
        "mark_intent_inconclusive": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"turn_inconclusive"}),
        ),
        "record_model_error": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"model_error"}),
        ),
        "recover_running_execution_as_inconclusive": OperationResultRule(
            event_type=_CONTROL_EVENT_TYPE,
            allowed_results=frozenset({"inconclusive"}),
        ),
    }
)


def _dynamic_result_ids(
    state_changes: tuple[StateChange, ...],
    entity: str,
) -> frozenset[str]:
    if entity == "task_run":
        return frozenset(
            change.task_run_id
            for change in state_changes
            if isinstance(change, UpsertTaskRun)
        )
    if entity == "turn":
        return frozenset(
            change.turn_id
            for change in state_changes
            if isinstance(change, UpsertTurn)
        )
    if entity in {"intent", "running_intent"}:
        return frozenset(
            change.intent_id
            for change in state_changes
            if isinstance(change, UpsertIntent)
            and (entity == "intent" or change.status is IntentStatus.RUNNING)
        )
    if entity == "plan":
        return frozenset(
            change.plan_id
            for change in state_changes
            if isinstance(change, UpsertPlan)
        )
    if entity == "intent_goal":
        return frozenset(
            change.goal.intent_id
            for change in state_changes
            if isinstance(change, UpsertIntentGoal)
        )
    if entity == "execution":
        return frozenset(
            change.execution_id
            for change in state_changes
            if isinstance(change, UpsertExecution)
        )
    if entity == "fact":
        return frozenset(
            change.fact_id
            for change in state_changes
            if isinstance(change, UpsertFact)
        )
    if entity == "plan_validation":
        return frozenset(
            change.record.plan_validation_id
            for change in state_changes
            if isinstance(change, AppendPlanValidationRecord)
        )
    raise ValueError(f"unsupported dynamic result entity: {entity}")


def validate_control_event(
    operation: str,
    state_changes: tuple[StateChange, ...],
    event: RuntimeEvent,
) -> None:
    if operation in D8_OPERATIONS:
        records = [c for c in state_changes if isinstance(c, D8_CHANGES)]
        if (len(records) != 1 or event.type != _CONTROL_EVENT_TYPE
                or event.status is not TaskRunStatus.RUNNING
                or set(event.details) != {"operation", "entity_ids", "result"}
                or event.details["operation"] != operation
                or event.details["entity_ids"] != [records[0].record_id]
                or event.details["result"] != records[0].record_id):
            raise ValueError("D8-2 event does not bind its exact metadata")
        return
    rule = CONTROL_OPERATION_RULES.get(operation)
    if rule is None:
        raise ValueError(f"unsupported ControlMutation.operation: {operation}")
    if event.type != rule.event_type:
        raise ValueError(
            f"operation {operation} requires RuntimeEvent.type={rule.event_type}"
        )
    if operation == "bind_intent_goal" and (
        len(state_changes) != 1
        or not isinstance(state_changes[0], UpsertIntentGoal)
    ):
        raise ValueError("bind_intent_goal requires one Intent goal change")
    if operation == "complete_verified_d1_turn" and (
        len(state_changes) != 1 or not isinstance(state_changes[0], UpsertTurn)
        or state_changes[0].status is not TurnStatus.COMPLETED
    ):
        raise ValueError("D1 Turn completion requires one completed Turn")
    if operation == "apply_d2_review" and (
        len(state_changes) != 1
        or not isinstance(state_changes[0], AppendD2ReviewApplication)
    ):
        raise ValueError("D2 review application requires one typed application change")
    if operation == "apply_d3_review" and (
        len(state_changes) != 1
        or not isinstance(state_changes[0], D3ReviewApplication)
    ):
        raise ValueError("D3 review application requires one typed application change")
    if operation == "append_process_fact" and (
        len(state_changes) != 1 or not isinstance(state_changes[0], D5ProcessFact)
    ):
        raise ValueError("Process_Fact requires one typed append change")
    if operation != "append_process_fact" and any(isinstance(change, D5ProcessFact) for change in state_changes):
        raise ValueError("Process_Fact may only be appended by its dedicated operation")
    conclusions = [c for c in state_changes if isinstance(c, D8Conclusion)]
    if operation == "append_d8_conclusion" and (
        len(state_changes) != 1 or len(conclusions) != 1
        or conclusions[0].outcome == "verified_fact"
    ):
        raise ValueError("D8 inference/rejection requires its dedicated typed append")
    if conclusions and operation not in {"append_d8_conclusion", "complete_d3_plan"}:
        raise ValueError("D8 conclusion requires its dedicated operation")
    if operation == "complete_d3_plan" and (
        len(state_changes) not in {1, 2, 3}
        or not isinstance(state_changes[0], UpsertPlan)
        or state_changes[0].status is not PlanStatus.COMPLETED
        or len(state_changes) >= 2 and (
            not isinstance(state_changes[1], UpsertFact)
            or state_changes[1].status is not FactStatus.VERIFIED
            or state_changes[1].verification_ref is None
            or not state_changes[1].verification_ref.startswith("d3:")
        )
        or len(state_changes) == 3 and (
            not isinstance(state_changes[2], D8Conclusion)
            or state_changes[2].outcome != "verified_fact"
            or state_changes[2].fact_id != state_changes[1].fact_id
        )
    ):
        raise ValueError("D3 Plan completion requires its checked Plan and optional verified Fact")
    if operation == "commit_verified_file_type_fact" and (
        len(state_changes) != 1
        or not isinstance(state_changes[0], UpsertFact)
        or state_changes[0].status is not FactStatus.VERIFIED
        or state_changes[0].verification_ref is None
    ):
        raise ValueError("file-type Fact commit requires one verified Fact with proof")
    if operation == "commit_verified_t6_observation" and (
        len(state_changes) != 2
        or not isinstance(state_changes[0], UpsertFact)
        or not isinstance(state_changes[1], UpsertIntent)
        or state_changes[0].status is not FactStatus.VERIFIED
        or state_changes[1].status is not IntentStatus.SUCCEEDED
        or state_changes[0].verification_ref is None
        or not state_changes[0].verification_ref.startswith("t6:")
    ):
        raise ValueError("T6 observation requires one verified Fact and succeeded Intent")
    if operation == "commit_verified_t6_fact" and (
        len(state_changes) != 1
        or not isinstance(state_changes[0], UpsertFact)
        or state_changes[0].status is not FactStatus.VERIFIED
        or state_changes[0].verification_ref is None
        or not state_changes[0].verification_ref.startswith("t6:")
    ):
        raise ValueError("T6 Fact requires one verified observation with proof")
    if operation in {"complete_satisfied_intent", "complete_verified_t6_intent"} and (
        len(state_changes) != 1
        or not isinstance(state_changes[0], UpsertIntent)
        or state_changes[0].status is not IntentStatus.SUCCEEDED
    ):
        raise ValueError("Intent completion requires one succeeded Intent change")
    if operation not in {"commit_verified_file_type_fact", "commit_verified_t6_observation", "commit_verified_t6_fact", "complete_d3_plan"} and any(
        isinstance(change, UpsertFact) and change.verification_ref is not None
        for change in state_changes
    ):
        raise ValueError("a Fact proof reference requires a dedicated verified operation")
    if not isinstance(event.details, Mapping):
        raise ValueError("RuntimeEvent.details must be a JSON object")
    details = dict(event.details)
    if any(not isinstance(key, str) for key in details):
        raise ValueError("RuntimeEvent.details keys must be strings")
    allowed_fields = {"operation", "entity_ids", "result"}
    if operation == "finish_task_run":
        allowed_fields.add("reason")
        if "reason" in details and (
            details["reason"] != "single_intent_slice_finished"
            or event.status is not TaskRunStatus.INCONCLUSIVE
            or len(state_changes) != 1
            or not isinstance(state_changes[0], UpsertTaskRun)
            or state_changes[0].status is not TaskRunStatus.INCONCLUSIVE
            or state_changes[0].failure_reason != details["reason"]
        ):
            raise ValueError("invalid single Intent slice conclusion event")
    if operation == "bind_intent_goal":
        allowed_fields.add("import_id")
    if operation == "commit_verified_file_type_fact":
        allowed_fields.update({"intent_id", "path", "observed_type"})
    if operation in {"commit_verified_t6_observation", "commit_verified_t6_fact"}:
        allowed_fields.add("intent_id")
    if operation in {"complete_satisfied_intent", "complete_verified_t6_intent"}:
        allowed_fields.add("verified_fact_ids")
    if operation == "complete_verified_d1_turn":
        allowed_fields.update({"verified_fact_ids", "proof_ref"})
    if operation == "register_d7_candidate":
        allowed_fields.update({"candidate_id", "evidence_version"})
    if operation == "apply_external_validation_feedback":
        allowed_fields.update({
            "candidate_id", "evidence_version", "feedback_operation_id", "verdict",
        })
    if operation in {
        "start_external_validation_check",
        "complete_external_validation_check",
    }:
        allowed_fields.update({
            "candidate_id", "checkpoint_sequence", "evidence_version",
            "feedback_operation_id", "query_id", "verdict",
        })
    if operation == "apply_d2_review":
        allowed_fields.update({
            "review_root_id", "review_generation", "review_id", "application_ref",
            "context_version", "constraint_version", "paused_gate",
            "turn_id", "intent_id", "review_scope", "action_candidate_id", "pause_reason",
        })
    unknown_fields = set(details) - allowed_fields
    if unknown_fields:
        raise ValueError(
            "RuntimeEvent.details contains unsupported fields: "
            + ", ".join(sorted(unknown_fields))
        )
    if operation == "apply_d2_review":
        application = state_changes[0]
        assert isinstance(application, AppendD2ReviewApplication)
        expected = {
            "review_root_id": application.review_root_id,
            "review_generation": application.review_generation,
            "review_id": application.review_id,
            "application_ref": application.application_ref,
            "context_version": application.context_version,
            "constraint_version": application.constraint_version,
            "paused_gate": application.paused_gate,
            "turn_id": application.turn_id,
            "intent_id": application.intent_id,
            "review_scope": application.review_scope,
            "action_candidate_id": application.action_candidate_id,
            "pause_reason": application.pause_reason,
        }
        if any(details.get(name) != value for name, value in expected.items()):
            raise ValueError("D2 review event does not match its typed application")
        if details.get("result") != ("paused" if application.paused_gate else "applied"):
            raise ValueError("D2 review result does not match its paused gate")
    if operation in {
        "register_d7_candidate",
        "apply_external_validation_feedback",
        "start_external_validation_check",
        "complete_external_validation_check",
    }:
        if len(state_changes) != 1 or not isinstance(state_changes[0], UpsertTaskRun):
            raise ValueError("D7 lifecycle commits require exactly one TaskRun change")
        task_change = state_changes[0]
        if operation == "register_d7_candidate":
            if (
                task_change.status is not TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
                or event.status is not TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
                or details.get("candidate_id") is None
                or details.get("evidence_version") is None
                or details.get("result") != "awaiting_external_feedback"
                or details.get("entity_ids") != [task_change.task_run_id, details.get("candidate_id")]
            ):
                raise ValueError("D7 candidate event does not match its waiting transition")
        elif operation == "apply_external_validation_feedback":
            verdict = details.get("verdict")
            expected_status = {
                "accepted": TaskRunStatus.ACHIEVED,
                "rejected": TaskRunStatus.RUNNING,
                "unknown": TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
            }.get(verdict)
            if (
                expected_status is None
                or task_change.status is not event.status
                or details.get("result") != verdict
                or details.get("entity_ids") != [task_change.task_run_id, details.get("candidate_id")]
                or not isinstance(details.get("feedback_operation_id"), str)
            ):
                raise ValueError("D7 feedback event does not match its verdict transition")
        elif operation == "start_external_validation_check":
            if (
                task_change.status is not event.status
                or details.get("result") != "check_started"
                or not isinstance(details.get("query_id"), str)
                or not isinstance(details.get("feedback_operation_id"), str)
                or not isinstance(details.get("checkpoint_sequence"), int)
                or details.get("entity_ids")
                != [task_change.task_run_id, details.get("candidate_id")]
            ):
                raise ValueError("D7 check-start event does not match its waiting state")
        else:
            verdict = details.get("verdict")
            expected_status = {
                "accepted": TaskRunStatus.ACHIEVED,
                "rejected": TaskRunStatus.RUNNING,
                "unknown": TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
            }.get(verdict)
            if (
                expected_status is None
                or task_change.status is not event.status
                or details.get("result") != verdict
                or not isinstance(details.get("query_id"), str)
                or not isinstance(details.get("feedback_operation_id"), str)
                or details.get("entity_ids")
                != [task_change.task_run_id, details.get("candidate_id")]
            ):
                raise ValueError("D7 check-result event does not match its verdict")
    if operation == "apply_d3_review":
        application = state_changes[0]
        assert isinstance(application, D3ReviewApplication)
        if details.get("entity_ids") != [application.review_id] or details.get("result") != (
            "paused" if application.paused_gate else "applied"
        ):
            raise ValueError("D3 review event does not match its typed application")
    if operation == "append_process_fact":
        fact = state_changes[0]
        assert isinstance(fact, D5ProcessFact)
        if details.get("entity_ids") != [fact.process_fact_id]:
            raise ValueError("Process_Fact event does not match its typed append")
    if operation == "append_d8_conclusion":
        record = state_changes[0]
        if details.get("entity_ids") != [record.record_id] or details.get("result") != record.outcome:
            raise ValueError("D8 event does not match its typed append")
    if operation == "bind_intent_goal":
        _uuid4(details.get("import_id"), "RuntimeEvent.details.import_id")
    if operation == "commit_verified_file_type_fact":
        change = state_changes[0]
        assert isinstance(change, UpsertFact)
        _uuid4(change.fact_id, "UpsertFact.fact_id")
        _uuid4(change.verification_ref, "UpsertFact.verification_ref")
        if (
            not isinstance(details.get("intent_id"), str)
            or not isinstance(details.get("path"), str)
            or not isinstance(details.get("observed_type"), str)
            or change.content != FACT_PREFIX + details["observed_type"]
        ):
            raise ValueError("file-type Fact event does not match its content")
    if operation in {"commit_verified_t6_observation", "commit_verified_t6_fact"}:
        fact = state_changes[0]
        assert isinstance(fact, UpsertFact)
        assert fact.verification_ref is not None
        _uuid4(fact.fact_id, "UpsertFact.fact_id")
        _uuid4(fact.verification_ref.removeprefix("t6:"), "UpsertFact.verification_ref")
        intent_id = (
            state_changes[1].intent_id
            if operation == "commit_verified_t6_observation"
            and isinstance(state_changes[1], UpsertIntent)
            else details.get("intent_id")
        )
        observation_entity_ids = [fact.fact_id, intent_id] if operation == "commit_verified_t6_observation" else [fact.fact_id]
        if (
            not isinstance(intent_id, str)
            or details.get("intent_id") != intent_id
            or details.get("entity_ids") != observation_entity_ids
            or details.get("result") != fact.fact_id
        ):
            raise ValueError("T6 observation event does not match its state changes")
    if operation in {"complete_satisfied_intent", "complete_verified_t6_intent"}:
        change = state_changes[0]
        assert isinstance(change, UpsertIntent)
        fact_ids = details.get("verified_fact_ids")
        if (
            details.get("entity_ids") != [change.intent_id]
            or not isinstance(fact_ids, list)
            or not fact_ids
            or any(not isinstance(item, str) or not item for item in fact_ids)
            or len(fact_ids) != len(set(fact_ids))
        ):
            raise ValueError("Intent completion requires supporting Fact IDs")
    if details.get("operation") != operation:
        raise ValueError(
            "ControlMutation.operation must match RuntimeEvent.details.operation"
        )
    entity_ids = details.get("entity_ids")
    if not isinstance(entity_ids, list) or not entity_ids:
        raise ValueError(
            "RuntimeEvent.details.entity_ids must be a non-empty array"
        )
    if any(not isinstance(item, str) or not item.strip() for item in entity_ids):
        raise ValueError(
            "RuntimeEvent.details.entity_ids must contain non-empty strings"
        )
    if len(entity_ids) != len(set(entity_ids)):
        raise ValueError("RuntimeEvent.details.entity_ids must be unique")

    result_present = "result" in details
    if not result_present:
        if rule.result_required:
            raise ValueError(f"operation {operation} requires RuntimeEvent.details.result")
        return

    result = details["result"]
    if not isinstance(result, str) or not result.strip():
        raise ValueError("RuntimeEvent.details.result must be a non-empty string")
    if rule.allowed_results:
        if result not in rule.allowed_results:
            raise ValueError(
                f"operation {operation} does not allow result={result}"
            )
        return

    assert rule.dynamic_result_entity is not None
    if result not in entity_ids:
        raise ValueError(
            "a dynamic RuntimeEvent.details.result must appear in entity_ids"
        )
    matching_ids = _dynamic_result_ids(
        state_changes,
        rule.dynamic_result_entity,
    )
    if result not in matching_ids:
        raise ValueError(
            f"operation {operation} result does not match its typed state_changes"
        )


@dataclass(frozen=True, slots=True)
class CheckpointDraft:
    task_run_id: str
    sequence: int

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "CheckpointDraft.task_run_id")
        _positive_integer(self.sequence, "CheckpointDraft.sequence")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    task_run_id: str
    sequence: int
    watermark_event_sequence: int
    created_at: float

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "Checkpoint.task_run_id")
        _positive_integer(self.sequence, "Checkpoint.sequence")
        _positive_integer(
            self.watermark_event_sequence,
            "Checkpoint.watermark_event_sequence",
        )
        _finite_timestamp(self.created_at, "Checkpoint.created_at")


@dataclass(frozen=True, slots=True)
class ControlMutation:
    task_run_id: str
    mutation_id: str
    operation: str
    request: TaskRequest
    effective_model_backend: ModelBackend
    base_checkpoint_sequence: int
    state_changes: tuple[StateChange, ...]
    event: RuntimeEvent
    checkpoint: CheckpointDraft
    request_fingerprint: str
    d1_config: D1RunConfig | None = None
    d1_binding: D1TurnBinding | None = None
    d1_turn_proof: D1TurnProof | None = None
    d2_config: D2RunConfig | None = None
    d3: D3CommitMetadata | None = None
    d7: D7CommitMetadata | None = None

    def __post_init__(self) -> None:
        _non_empty(self.task_run_id, "ControlMutation.task_run_id")
        _uuid4(self.mutation_id, "ControlMutation.mutation_id")
        _non_empty(self.operation, "ControlMutation.operation")
        if not isinstance(self.request, TaskRequest):
            raise ValueError("ControlMutation.request must be a TaskRequest")
        if not isinstance(self.effective_model_backend, ModelBackend):
            raise ValueError(
                "ControlMutation.effective_model_backend must be a ModelBackend"
            )
        _non_negative_integer(
            self.base_checkpoint_sequence,
            "ControlMutation.base_checkpoint_sequence",
        )
        if not isinstance(self.state_changes, tuple) or not self.state_changes:
            raise ValueError("ControlMutation.state_changes must be a non-empty tuple")
        if any(not isinstance(change, STATE_CHANGE_TYPES) for change in self.state_changes):
            raise ValueError("ControlMutation contains an unsupported state change")
        if self.event.task_run_id != self.task_run_id:
            raise ValueError("ControlMutation.event belongs to another TaskRun")
        validate_control_event(self.operation, self.state_changes, self.event)
        if self.d7 is not None:
            details = self.event.details
            if self.d7.candidate is not None and (
                details.get("candidate_id") != self.d7.candidate.candidate_id
                or details.get("evidence_version") != self.d7.candidate.evidence_version
            ):
                raise ValueError("D7 candidate event differs from its typed payload")
            if self.d7.feedback is not None and (
                details.get("candidate_id") != self.d7.feedback.candidate_id
                or details.get("evidence_version") != self.d7.feedback.evidence_version
                or details.get("feedback_operation_id") != self.d7.feedback.operation_id
                or details.get("verdict") != self.d7.feedback.verdict
            ):
                raise ValueError("D7 feedback event differs from its typed payload")
            if self.d7.check_request is not None and (
                self.operation != "start_external_validation_check"
                or details.get("query_id") != self.d7.check_request.query_id
                or details.get("candidate_id") != self.d7.check_request.candidate_id
                or details.get("evidence_version") != self.d7.check_request.evidence_version
                or details.get("feedback_operation_id")
                != self.d7.check_request.submission_operation_id
                or details.get("checkpoint_sequence")
                != self.d7.check_request.checkpoint_sequence
                or self.d7.check_request.task_run_id != self.task_run_id
                or self.d7.check_request.query_id != self.mutation_id
                or self.d7.check_request.checkpoint_sequence != self.checkpoint.sequence
            ):
                raise ValueError("D7 check-start event differs from its typed request")
            if self.d7.check_completion is not None:
                result = self.d7.check_completion.result
                request = result.request
                if (
                    self.operation != "complete_external_validation_check"
                    or details.get("query_id") != request.query_id
                    or details.get("candidate_id") != request.candidate_id
                    or details.get("evidence_version") != request.evidence_version
                    or details.get("feedback_operation_id")
                    != request.submission_operation_id
                    or details.get("verdict") != result.verdict
                    or request.task_run_id != self.task_run_id
                ):
                    raise ValueError("D7 check-result event differs from its typed result")
        if self.d1_config is not None and (
            not isinstance(self.d1_config, D1RunConfig) or self.operation != "start_task_run"
        ):
            raise ValueError("D1 run configuration may only be saved at explicit start")
        if self.d2_config is not None and (
            not isinstance(self.d2_config, D2RunConfig)
            or self.operation != "start_task_run" or self.d1_config is None
        ):
            raise ValueError("D2 configuration requires explicit start and fixed execution configuration")
        if self.d3 is not None:
            if not isinstance(self.d3, D3CommitMetadata) or self.d1_config is not None or self.d2_config is not None:
                raise ValueError("D3 metadata must be typed and separate from D1/D2")
            if self.d3.config is not None and self.operation != "start_task_run":
                raise ValueError("D3 run configuration requires explicit start")
            if self.d3.turn_binding is not None and self.operation not in {
                "register_d3_turn", "advance_d3_turn", "register_d8_tree_turn",
            }:
                raise ValueError("D3 Turn binding requires its registration transaction")
            if self.d3.plan_binding is not None and self.operation not in {
                "register_d3_plan", "replan_d3_plan",
            }:
                raise ValueError("D3 Plan binding requires its registration transaction")
            if self.d3.plan_result is not None and self.operation != "complete_d3_plan":
                raise ValueError("D3 Plan result requires its completion transaction")
        if self.d7 is not None:
            if not isinstance(self.d7, D7CommitMetadata):
                raise ValueError("D7 metadata must be typed")
            if self.d1_config is not None or self.d2_config is not None:
                raise ValueError("D7 metadata cannot be combined with a D1 or D2 run")
            if self.d7.run_config is not None:
                config = self.d7.run_config
                endpoints = self.request.target.endpoints
                if (
                    self.operation != "start_task_run"
                    or self.d7.candidate is not None
                    or self.d7.feedback is not None
                    or self.d7.check_request is not None
                    or self.d7.check_completion is not None
                    or self.d3 is not None
                    or config.task_run_id != self.task_run_id
                    or len(endpoints) != 1
                    or self.request.target.attachments
                    or canonical_web_origin(endpoints[0].url) != config.endpoint
                ):
                    raise ValueError("D7 Web config must bind its explicit Web TaskRun start")
            elif self.operation == "register_d7_candidate":
                if self.d7.candidate is None or self.d7.feedback is not None:
                    raise ValueError("D7 candidate registration requires a candidate")
                if self.d7.candidate.task_run_id != self.task_run_id:
                    raise ValueError("D7 candidate belongs to another TaskRun")
            elif self.operation == "apply_external_validation_feedback":
                if self.d7.feedback is None or self.d7.candidate is not None:
                    raise ValueError("D7 feedback commit requires feedback")
                if self.d7.feedback.task_run_id != self.task_run_id:
                    raise ValueError("D7 feedback belongs to another TaskRun")
                if self.d7.feedback.operation_id != self.mutation_id:
                    raise ValueError("D7 feedback operation_id must equal mutation_id")
            elif self.operation == "start_external_validation_check":
                if (
                    self.d7.check_request is None
                    or self.d7.check_completion is not None
                ):
                    raise ValueError("D7 check start requires a query request")
            elif self.operation == "complete_external_validation_check":
                if (
                    self.d7.check_completion is None
                    or self.d7.check_request is not None
                ):
                    raise ValueError("D7 check completion requires a query result")
                from agent_core.goal_feedback import (
                    external_validation_check_result_mutation_id,
                )

                if (
                    external_validation_check_result_mutation_id(
                        self.d7.check_completion.result.request
                    )
                    != self.mutation_id
                ):
                    raise ValueError("D7 check result mutation ID is not stable")
            else:
                raise ValueError("D7 metadata is only valid for D7 lifecycle operations")
        elif self.operation in {
            "register_d7_candidate", "apply_external_validation_feedback",
            "start_external_validation_check", "complete_external_validation_check",
        }:
            raise ValueError("D7 lifecycle operation requires typed D7 metadata")
        if self.d1_binding is not None and (
            not isinstance(self.d1_binding, D1TurnBinding)
            or self.operation != "register_turn" or self.d1_binding.task_run_id != self.task_run_id
        ):
            raise ValueError("D1 binding requires the same Turn registration transaction")
        if self.operation == "complete_verified_d1_turn" and self.d1_turn_proof is None:
            raise ValueError("D1 Turn completion requires its independent proof")
        if self.d1_turn_proof is not None and (
            not isinstance(self.d1_turn_proof, D1TurnProof)
            or self.operation != "complete_verified_d1_turn"
        ):
            raise ValueError("D1 Turn proof requires the independent completion operation")
        if self.checkpoint.task_run_id != self.task_run_id:
            raise ValueError("ControlMutation.checkpoint belongs to another TaskRun")
        if self.checkpoint.sequence != self.base_checkpoint_sequence + 1:
            raise ValueError(
                "ControlMutation checkpoint sequence must equal base sequence plus one"
            )
        _non_empty(self.request_fingerprint, "ControlMutation.request_fingerprint")
        if len(self.request_fingerprint) != 64:
            raise ValueError("ControlMutation.request_fingerprint must be SHA-256 hex")
        try:
            int(self.request_fingerprint, 16)
        except ValueError as exc:
            raise ValueError(
                "ControlMutation.request_fingerprint must be SHA-256 hex"
            ) from exc

    @classmethod
    def create(
        cls,
        *,
        task_run_id: str,
        mutation_id: str,
        operation: str,
        request: TaskRequest,
        effective_model_backend: ModelBackend,
        base_checkpoint_sequence: int,
        state_changes: tuple[StateChange, ...],
        event: RuntimeEvent,
        d1_config: D1RunConfig | None = None,
        d1_binding: D1TurnBinding | None = None,
        d1_turn_proof: D1TurnProof | None = None,
        d2_config: D2RunConfig | None = None,
        d3: D3CommitMetadata | None = None,
        d7: D7CommitMetadata | None = None,
    ) -> "ControlMutation":
        checkpoint = CheckpointDraft(
            task_run_id=task_run_id,
            sequence=base_checkpoint_sequence + 1,
        )
        provisional = cls(
            task_run_id=task_run_id,
            mutation_id=mutation_id,
            operation=operation,
            request=request,
            effective_model_backend=effective_model_backend,
            base_checkpoint_sequence=base_checkpoint_sequence,
            state_changes=state_changes,
            event=event,
            checkpoint=checkpoint,
            request_fingerprint="0" * 64,
            d1_config=d1_config,
            d1_binding=d1_binding,
            d1_turn_proof=d1_turn_proof,
            d2_config=d2_config,
            d3=d3,
            d7=d7,
        )
        return cls(
            task_run_id=provisional.task_run_id,
            mutation_id=provisional.mutation_id,
            operation=provisional.operation,
            request=provisional.request,
            effective_model_backend=provisional.effective_model_backend,
            base_checkpoint_sequence=provisional.base_checkpoint_sequence,
            state_changes=provisional.state_changes,
            event=provisional.event,
            checkpoint=provisional.checkpoint,
            request_fingerprint=control_mutation_fingerprint(provisional),
            d1_config=provisional.d1_config,
            d1_binding=provisional.d1_binding,
            d1_turn_proof=provisional.d1_turn_proof,
            d2_config=provisional.d2_config,
            d3=provisional.d3,
            d7=provisional.d7,
        )


@dataclass(frozen=True, slots=True)
class ControlCommitResult:
    event: RuntimeEvent
    checkpoint: Checkpoint
    replayed: bool


@dataclass(frozen=True, slots=True)
class CommittedOperation:
    value: str | None
    commit: ControlCommitResult

    @classmethod
    def from_commit(cls, commit: ControlCommitResult) -> "CommittedOperation":
        result = commit.event.details.get("result")
        if result is not None and not isinstance(result, str):
            raise ValueError("committed RuntimeEvent result must be a string")
        return cls(value=result, commit=commit)


@dataclass(frozen=True, slots=True)
class PersistedIntentGoal:
    goal: FileTypeIntentGoal
    first_commit: ControlCommitResult


@dataclass(frozen=True, slots=True)
class IntentGoalBindingResult:
    goal: FileTypeIntentGoal
    first_commit: ControlCommitResult
    disposition: Literal["created", "replayed", "existing"]


@dataclass(frozen=True, slots=True)
class CommittedPlanValidation:
    record: PlanValidationRecord
    commit: ControlCommitResult

    @classmethod
    def from_store(
        cls,
        store: "ControlStore",
        *,
        task_run_id: str,
        plan_validation_id: str,
        commit: ControlCommitResult,
    ) -> "CommittedPlanValidation":
        record = store.load_plan_validation_record(
            task_run_id,
            plan_validation_id,
        )
        return cls(record=record, commit=commit)


@dataclass(frozen=True, slots=True)
class TaskRunSnapshot:
    task_run_id: str
    request: TaskRequest
    effective_model_backend: ModelBackend
    started_at: float
    deadline_at: float
    status: TaskRunStatus
    sandbox_id: str
    confirmation_deadline: float | None
    finished_at: float | None
    failure_reason: str | None
    source_root: str | None = None
    target_network: TargetNetworkBinding | None = None


@dataclass(frozen=True, slots=True)
class TurnSnapshot(UpsertTurn):
    """Turn 的读投影。

    字段与校验完全继承自 UpsertTurn（写命令），这里只加「转回领域对象」的能力——
    原先两个类各自重复声明同一组 8 个字段。
    """

    def to_domain(self) -> Turn:
        return Turn(
            turn_id=self.turn_id,
            task_run_id=self.task_run_id,
            sequence=self.sequence,
            turn_target=self.turn_target,
            intent_ids=self.intent_ids,
            status=self.status,
            parent_turn_id=self.parent_turn_id,
            triggering_intent_id=self.triggering_intent_id,
        )


@dataclass(frozen=True, slots=True)
class IntentSnapshot:
    intent_id: str
    turn_id: str
    description: str
    priority: int
    intent_type: str | None
    status: IntentStatus

    def to_domain(self) -> Intent:
        return Intent(
            intent_id=self.intent_id,
            turn_id=self.turn_id,
            description=self.description,
            priority=self.priority,
            intent_type=self.intent_type,
            status=self.status,
        )


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    plan_id: str
    intent_id: str
    steps: tuple[PlanStep, ...]
    status: PlanStatus

    def to_domain(self) -> Plan:
        return Plan(
            plan_id=self.plan_id,
            intent_id=self.intent_id,
            steps=self.steps,
            status=self.status,
        )


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    execution_id: str
    plan_id: str
    step_index: int
    command_ref: str
    status: ExecutionStatus
    output_ref: str | None

    def to_domain(self) -> Execution:
        return Execution(
            execution_id=self.execution_id,
            plan_id=self.plan_id,
            step_index=self.step_index,
            command_ref=self.command_ref,
            status=self.status,
            output_ref=self.output_ref,
        )


@dataclass(frozen=True, slots=True)
class FactRecordSnapshot:
    fact_id: str
    turn_id: str
    content: str
    source_ref: str
    status: FactStatus
    created_event_sequence: int
    created_event_ordinal: int
    verification_ref: str | None = None

    def to_domain(self) -> Fact:
        return Fact(
            fact_id=self.fact_id,
            turn_id=self.turn_id,
            content=self.content,
            source_ref=self.source_ref,
            status=self.status,
            verification_ref=self.verification_ref,
        )


@dataclass(frozen=True, slots=True)
class FailureSummaryRecordSnapshot:
    failure_type: FailureType
    short_reason: str
    evidence_ref: str
    created_event_sequence: int
    created_event_ordinal: int

    def to_domain(self) -> FailureSummary:
        return FailureSummary(
            failure_type=self.failure_type,
            short_reason=self.short_reason,
            evidence_ref=self.evidence_ref,
        )


@dataclass(frozen=True, slots=True)
class ControlStateSnapshot:
    task_run: TaskRunSnapshot
    latest_checkpoint: Checkpoint
    turns: tuple[TurnSnapshot, ...]
    intents: tuple[IntentSnapshot, ...]
    plans: tuple[PlanSnapshot, ...]
    executions: tuple[ExecutionSnapshot, ...]
    facts: tuple[FactRecordSnapshot, ...]
    failure_summaries: tuple[FailureSummaryRecordSnapshot, ...]
    plan_validation_records: tuple[PlanValidationRecord, ...]


# 曾经与 ControlStateSnapshot 逐字段相同的第二个类，只保留一个名字。
# 别名保证既有 `ControlStateRows(...)` 调用与类型注解继续可用。
ControlStateRows = ControlStateSnapshot


class ControlStore(Protocol):
    def commit(self, mutation: ControlMutation) -> ControlCommitResult: ...

    def committed_mutations_after(
        self, task_run_id: str, checkpoint_sequence: int,
    ) -> tuple[tuple[str, ControlCommitResult], ...]:
        """Read the commits used to reconcile an interrupted action."""
        ...

    def load_committed_mutation(
        self,
        task_run_id: str,
        mutation_id: str,
    ) -> ControlCommitResult | None: ...

    def load_intent_goal(self, intent_id: str) -> PersistedIntentGoal | None: ...

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None: ...

    def list_task_run_refs(self) -> tuple[str, ...]:
        """Read TaskRun identities without exposing their request contents."""
        ...

    def load_d1_run_config(self, task_run_id: str) -> D1RunConfig | None: ...

    def load_d1_turn_binding(self, turn_id: str) -> D1TurnBinding | None: ...

    def load_d1_turn_proof(self, turn_id: str) -> D1TurnProof | None: ...

    def load_d2_review_application(
        self, task_run_id: str, review_id: str
    ) -> AppendD2ReviewApplication | None: ...

    def list_d2_review_applications(
        self, task_run_id: str
    ) -> tuple[AppendD2ReviewApplication, ...]: ...

    def load_d2_run_config(self, task_run_id: str) -> D2RunConfig | None: ...

    def load_d3_run_config(self, task_run_id: str): ...

    def load_d3_turn_binding(self, turn_id: str): ...

    def load_d3_plan_turn_binding(self, plan_id: str): ...

    def load_d3_intent_binding(self, intent_id: str): ...

    def load_d3_plan_binding(self, plan_id: str): ...

    def load_d3_plan_result(self, plan_id: str): ...

    def load_d3_outcome(self, entity_id: str): ...

    def load_d3_operation(self, operation_id: str): ...

    def list_d3_operations(self, task_run_id: str): ...

    def load_d3_review_application(self, task_run_id: str, review_id: str): ...

    def list_d3_review_applications(self, task_run_id: str): ...

    def load_d7_answer_candidate(self, candidate_id: str): ...

    def list_d7_answer_candidates(self, task_run_id: str): ...

    def load_d7_web_run_config(
        self, task_run_id: str
    ) -> D7WebRunConfig | None: ...

    def register_d7_web_plan(
        self, record: D7WebPlanRecord
    ) -> D7WebPlanRecord: ...

    def load_d7_web_plan(self, plan_id: str) -> D7WebPlanRecord | None: ...

    def list_d7_web_plans(
        self, task_run_id: str
    ) -> tuple[D7WebPlanRecord, ...]: ...

    def start_d7_web_execution(
        self, record: D7WebExecutionRecord
    ) -> tuple[D7WebExecutionRecord, bool]: ...

    def complete_d7_web_execution(
        self, record: D7WebExecutionRecord
    ) -> D7WebExecutionRecord: ...

    def load_d7_web_execution(
        self, execution_id: str
    ) -> D7WebExecutionRecord | None: ...

    def load_d7_web_execution_binding(
        self, execution_id: str
    ) -> D7WebExecutionBinding | None: ...

    def list_d7_web_executions(
        self, task_run_id: str
    ) -> tuple[D7WebExecutionRecord, ...]: ...

    def list_d7_validation_feedback(
        self, task_run_id: str, candidate_id: str, evidence_version: str,
        *, through_event_sequence: int | None = None,
    ): ...

    def list_process_facts(self, task_run_id: str) -> tuple[D5ProcessFact, ...]: ...
    def list_d8_conclusions(self, task_run_id: str) -> tuple[D8Conclusion, ...]: ...

    def load_terminal_commit(self, task_run_id: str) -> ControlCommitResult | None:
        """First persisted terminal result for repeated cancellation; no new mutation."""
        ...

    def load_control_state_rows(
        self,
        task_run_id: str,
    ) -> ControlStateRows | None: ...

    def load_plan_validation_record(
        self,
        task_run_id: str,
        plan_validation_id: str,
    ) -> PlanValidationRecord: ...


def _state_change_payload(change: StateChange) -> dict[str, object]:
    if isinstance(change, UpsertTaskRun):
        payload: dict[str, object] = {
            "type": "upsert_task_run",
            "task_run_id": change.task_run_id,
            "request": task_request_snapshot(
                change.request,
                change.effective_model_backend,
            ),
            "sandbox_id": change.sandbox_id,
            "status": change.status.value,
            "failure_reason": change.failure_reason,
        }
        if change.source_root is not None:
            payload["source_root"] = change.source_root
        if change.target_network is not None:
            payload["target_network"] = change.target_network.to_dict()
        return payload
    if isinstance(change, UpsertTurn):
        return {
            "type": "upsert_turn",
            "turn_id": change.turn_id,
            "task_run_id": change.task_run_id,
            "sequence": change.sequence,
            "turn_target": change.turn_target,
            "intent_ids": list(change.intent_ids),
            "status": change.status.value,
            "parent_turn_id": change.parent_turn_id,
            "triggering_intent_id": change.triggering_intent_id,
        }
    if isinstance(change, UpsertIntent):
        return {
            "type": "upsert_intent",
            "intent_id": change.intent_id,
            "turn_id": change.turn_id,
            "description": change.description,
            "priority": change.priority,
            "intent_type": change.intent_type,
            "status": change.status.value,
        }
    if isinstance(change, UpsertIntentGoal):
        return {
            "type": "upsert_intent_goal",
            "task_run_id": change.goal.task_run_id,
            "intent_id": change.goal.intent_id,
            "goal_type": change.goal.goal_type,
            "path": change.goal.path,
        }
    if isinstance(change, UpsertPlan):
        return {
            "type": "upsert_plan",
            "plan_id": change.plan_id,
            "intent_id": change.intent_id,
            "steps": [{"description": item.description} for item in change.steps],
            "status": change.status.value,
        }
    if isinstance(change, UpsertExecution):
        return {
            "type": "upsert_execution",
            "execution_id": change.execution_id,
            "plan_id": change.plan_id,
            "step_index": change.step_index,
            "command_ref": change.command_ref,
            "status": change.status.value,
            "output_ref": change.output_ref,
        }
    if isinstance(change, UpsertFact):
        data: dict[str, object] = {
            "type": "upsert_fact",
            "fact_id": change.fact_id,
            "turn_id": change.turn_id,
            "content": change.content,
            "source_ref": change.source_ref,
            "status": change.status.value,
        }
        if change.verification_ref is not None:
            data["verification_ref"] = change.verification_ref
        return data
    if isinstance(change, AppendFailureSummary):
        return {
            "type": "append_failure_summary",
            "failure_type": change.failure_type.value,
            "short_reason": change.short_reason,
            "evidence_ref": change.evidence_ref,
        }
    if isinstance(change, AppendPlanValidationRecord):
        record = change.record
        return {
            "type": "append_plan_validation_record",
            "plan_validation_id": record.plan_validation_id,
            "plan_id": record.plan_id,
            "task_run_id": record.task_run_id,
            "step_index": record.step_index,
            "attempt": record.attempt,
            "status": record.status.value,
            "failure_type": (
                record.failure_type.value if record.failure_type is not None else None
            ),
            "short_reason": record.short_reason,
            "command_ref": record.command_ref,
        }
    if isinstance(change, AppendD2ReviewApplication):
        return {
            "type": "append_d2_review_application",
            "review_root_id": change.review_root_id,
            "review_generation": change.review_generation,
            "review_id": change.review_id,
            "application_ref": change.application_ref,
            "context_version": change.context_version,
            "constraint_version": change.constraint_version,
            "output_constraints": [d2_payload(item) for item in change.output_constraints],
            "paused_gate": change.paused_gate,
            "turn_id": change.turn_id,
            "intent_id": change.intent_id,
            "review_scope": change.review_scope,
            "action_candidate_id": change.action_candidate_id,
            "pause_reason": change.pause_reason,
        }
    if isinstance(change, D3ReviewApplication):
        return {"type": "append_d3_review_application", **d3_payload(change)}
    if isinstance(change, D5ProcessFact):
        return {"type": "append_process_fact", **d3_payload(change)}
    if isinstance(change, D8Conclusion):
        return {"type": "append_d8_conclusion", **d3_payload(change)}
    if isinstance(change, D8_CHANGES):
        return {"type": type(change).__name__, **d3_payload(change)}
    raise TypeError("unsupported state change")


def control_mutation_fingerprint(mutation: ControlMutation) -> str:
    if mutation.operation in {"complete_satisfied_intent", "complete_verified_t6_intent"}:
        (change,) = mutation.state_changes
        assert isinstance(change, UpsertIntent)
        return hashlib.sha256(
            canonical_json(
                {
                    "operation": mutation.operation,
                    "task_run_id": mutation.task_run_id,
                    "intent_id": change.intent_id,
                    "verified_fact_ids": mutation.event.details["verified_fact_ids"],
                }
            ).encode("utf-8")
        ).hexdigest()
    if mutation.operation == "bind_intent_goal":
        (change,) = mutation.state_changes
        assert isinstance(change, UpsertIntentGoal)
        return hashlib.sha256(
            canonical_json(
                {
                    "operation": mutation.operation,
                    "task_run_id": change.goal.task_run_id,
                    "intent_id": change.goal.intent_id,
                    "goal_type": change.goal.goal_type,
                    "path": change.goal.path,
                }
            ).encode("utf-8")
        ).hexdigest()
    if mutation.operation == "commit_verified_file_type_fact":
        (change,) = mutation.state_changes
        assert isinstance(change, UpsertFact)
        return hashlib.sha256(
            canonical_json(
                {
                    "operation": mutation.operation,
                    "task_run_id": mutation.task_run_id,
                    "intent_id": mutation.event.details["intent_id"],
                    "path": mutation.event.details["path"],
                    "observed_type": mutation.event.details["observed_type"],
                    "execution_id": change.source_ref,
                    "proof_id": change.verification_ref,
                    "fact_id": change.fact_id,
                }
            ).encode("utf-8")
        ).hexdigest()
    if mutation.operation in {"commit_verified_t6_observation", "commit_verified_t6_fact"}:
        fact = mutation.state_changes[0]
        assert isinstance(fact, UpsertFact)
        return hashlib.sha256(
            canonical_json(
                {
                    "operation": mutation.operation,
                    "task_run_id": mutation.task_run_id,
                    "intent_id": mutation.event.details["intent_id"],
                    "execution_id": fact.source_ref,
                    "proof_id": fact.verification_ref,
                    "fact_id": fact.fact_id,
                }
            ).encode("utf-8")
        ).hexdigest()
    details = dict(mutation.event.details)
    payload = {
        "task_run_id": mutation.task_run_id,
        "task_request": task_request_snapshot(
            mutation.request,
            mutation.effective_model_backend,
        ),
        "operation": mutation.operation,
        "base_checkpoint_sequence": mutation.base_checkpoint_sequence,
        "state_changes": [
            _state_change_payload(change) for change in mutation.state_changes
        ],
        "event": {
            "type": mutation.event.type,
            "status": mutation.event.status.value,
            "details": details,
        },
    }
    for name in ("d1_config", "d1_binding", "d1_turn_proof", "d2_config"):
        value = getattr(mutation, name)
        if value is not None:
            payload[name] = d1_payload(value)
    if mutation.d3 is not None:
        payload["d3"] = d3_payload(mutation.d3)
    if mutation.d7 is not None:
        payload["d7"] = mutation.d7.to_dict()
    encoded = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
