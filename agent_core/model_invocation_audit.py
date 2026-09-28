"""Durable, provider-neutral records for model calls."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from agent_core.domain import PlanStep
from agent_core.modeling import (
    FileTypeCandidateAction,
    InspectFileAction,
    IntentActionResponse,
    IntentCandidate,
    IntentGenerationResponse,
    ModelError,
    ModelErrorKind,
    ModelRequestType,
    PlanCandidate,
    PlanGenerationResponse,
    StopIntentAction,
    parse_intent_action,
)

AuditedResult = IntentGenerationResponse | PlanGenerationResponse | IntentActionResponse | ModelError
AuditedResultKind = Literal["intent_generation", "plan", "intent_action", "invalid_structure", "model_error"]


def _uuid4(value: str | None, field: str) -> None:
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
    registration_mutation_id: str | None = None
    base_checkpoint_sequence: int | None = None
    context_sha256: str | None = None
    turn_target: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.model_request_id, "model_request_id")
        _uuid4(self.task_run_id, "task_run_id")
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("intent_id is required")
        if not isinstance(self.request_type, ModelRequestType):
            raise TypeError("unsupported model request type")
        if (self.plan_id is None) != (
            self.request_type is not ModelRequestType.INTENT_ACTION
        ):
            raise ValueError("Plan ID is required only for intent_action")
        generation_fields = (
            self.registration_mutation_id,
            self.base_checkpoint_sequence,
            self.context_sha256,
            self.turn_target,
        )
        if self.request_type is ModelRequestType.INTENT_GENERATION:
            try:
                turn_id = UUID(self.intent_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("turn_id must be a canonical UUID5") from exc
            if turn_id.version != 5 or str(turn_id) != self.intent_id:
                raise ValueError("turn_id must be a canonical UUID5")
            _uuid4(self.registration_mutation_id, "registration_mutation_id")
            if type(self.base_checkpoint_sequence) is not int or self.base_checkpoint_sequence < 1:
                raise ValueError("base_checkpoint_sequence must be positive")
            if not isinstance(self.context_sha256, str) or len(self.context_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.context_sha256):
                raise ValueError("context_sha256 must be lowercase SHA-256")
            if not isinstance(self.turn_target, str) or not self.turn_target.strip():
                raise ValueError("turn_target is required")
        elif any(value is not None for value in generation_fields):
            raise ValueError("turn generation fields require intent_generation")
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
        if self.result_kind == "intent_generation" and isinstance(
            self.result, IntentGenerationResponse
        ) and self.result.candidates:
            return
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
    if isinstance(result, IntentGenerationResponse):
        return {"candidates": [{"description": candidate.description,
                                "intent_type": candidate.intent_type}
                               for candidate in result.candidates]}
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
        raise TypeError("model invocation result must be an object")
    if kind == "intent_generation":
        if set(payload) != {"candidates"} or not isinstance(payload["candidates"], list) or not payload["candidates"]:
            raise ValueError("invalid persisted Intent candidates")
        candidates = []
        for item in payload["candidates"]:
            if not isinstance(item, Mapping) or set(item) != {"description", "intent_type"}:
                raise ValueError("invalid persisted Intent candidate")
            candidates.append(IntentCandidate(item["description"], item["intent_type"]))
        return IntentGenerationResponse(tuple(candidates))
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
