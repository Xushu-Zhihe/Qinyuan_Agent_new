"""Durable, provider-neutral records for T4 model calls."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol
from uuid import UUID

from agent_core.domain import PlanStep
from agent_core.modeling import (
    FileTypeCandidateAction,
    InspectFileAction,
    IntentActionResponse,
    ModelError,
    ModelErrorKind,
    ModelRequestType,
    PlanCandidate,
    PlanGenerationResponse,
    StopIntentAction,
    parse_intent_action,
)


AuditedResult = PlanGenerationResponse | IntentActionResponse | ModelError
AuditedResultKind = Literal["plan", "intent_action", "invalid_structure", "model_error"]


def _uuid4(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID4")


@dataclass(frozen=True, slots=True)
class ModelInvocationStart:
    model_request_id: str
    task_run_id: str
    intent_id: str
    request_type: ModelRequestType
    plan_id: str | None
    correction_of_model_request_id: str | None
    started_at: float

    def __post_init__(self) -> None:
        _uuid4(self.model_request_id, "model_request_id")
        _uuid4(self.task_run_id, "task_run_id")
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("intent_id is required")
        if self.request_type not in {
            ModelRequestType.PLAN_GENERATION, ModelRequestType.INTENT_ACTION
        }:
            raise ValueError("T4 audit only records Plan and Intent actions")
        if (self.plan_id is None) != (
            self.request_type is ModelRequestType.PLAN_GENERATION
        ):
            raise ValueError("Plan ID is required only for intent_action")
        if self.correction_of_model_request_id is not None:
            _uuid4(self.correction_of_model_request_id, "correction_of_model_request_id")
        if not isinstance(self.started_at, (int, float)) or not math.isfinite(
            self.started_at
        ):
            raise ValueError("started_at must be finite")


@dataclass(frozen=True, slots=True)
class ModelInvocationCompletion:
    model_request_id: str
    finished_at: float
    result_kind: AuditedResultKind
    result: AuditedResult

    def __post_init__(self) -> None:
        _uuid4(self.model_request_id, "model_request_id")
        if not isinstance(self.finished_at, (int, float)) or not math.isfinite(
            self.finished_at
        ):
            raise ValueError("finished_at must be finite")
        if self.result_kind == "plan" and isinstance(
            self.result, PlanGenerationResponse
        ):
            return
        if self.result_kind == "intent_action" and isinstance(
            self.result, IntentActionResponse
        ):
            return
        if self.result_kind in {"invalid_structure", "model_error"} and isinstance(
            self.result, ModelError
        ):
            return
        raise ValueError("model invocation result does not match its kind")


@dataclass(frozen=True, slots=True)
class ModelInvocationRecord:
    start: ModelInvocationStart
    completion: ModelInvocationCompletion | None


class ModelInvocationConflict(RuntimeError):
    pass


class ModelInvocationRecoveryRequired(RuntimeError):
    pass


class ModelInvocationBudgetExhausted(RuntimeError):
    pass


class ModelInvocationAuditPort(Protocol):
    def begin_model_invocation(
        self, start: ModelInvocationStart
    ) -> ModelInvocationRecord | None: ...

    def complete_model_invocation(
        self, completion: ModelInvocationCompletion
    ) -> ModelInvocationRecord: ...

    def list_model_invocations(
        self, task_run_id: str, intent_id: str
    ) -> tuple[ModelInvocationRecord, ...]: ...


def result_payload(completion: ModelInvocationCompletion) -> dict[str, object]:
    result = completion.result
    if isinstance(result, PlanGenerationResponse):
        return {"steps": [{"description": step.description} for step in result.candidate.steps]}
    if isinstance(result, IntentActionResponse):
        action = result.action
        if isinstance(action, InspectFileAction):
            return {"action": {"kind": "inspect_file", "path": action.path}}
        if isinstance(action, FileTypeCandidateAction):
            return {
                "action": {
                    "kind": "file_type_candidate",
                    "path": action.path,
                    "observed_type": action.observed_type,
                    "execution_id": action.execution_id,
                }
            }
        if isinstance(action, StopIntentAction):
            return {"action": {"kind": "stop", "short_reason": action.short_reason}}
        raise ValueError("unsupported Intent action")
    assert isinstance(result, ModelError)
    return {"kind": result.kind.value, "short_reason": result.short_reason,
            "evidence_ref": result.evidence_ref}


def result_from_payload(kind: AuditedResultKind, payload: object) -> AuditedResult:
    if not isinstance(payload, Mapping):
        raise ValueError("model invocation result must be an object")
    if kind == "plan":
        if set(payload) != {"steps"} or not isinstance(payload["steps"], list):
            raise ValueError("invalid persisted Plan summary")
        return PlanGenerationResponse(
            PlanCandidate(tuple(PlanStep.from_dict(step) for step in payload["steps"]))
        )
    if kind == "intent_action":
        if set(payload) != {"action"}:
            raise ValueError("invalid persisted Intent action")
        return IntentActionResponse(parse_intent_action(payload["action"]))
    if kind in {"invalid_structure", "model_error"}:
        if set(payload) != {"kind", "short_reason", "evidence_ref"}:
            raise ValueError("invalid persisted ModelError")
        error_kind = ModelErrorKind(payload["kind"])
        return ModelError(error_kind, payload["short_reason"], payload["evidence_ref"])
    raise ValueError("unsupported persisted model result kind")
