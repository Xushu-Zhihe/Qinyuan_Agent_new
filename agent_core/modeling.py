"""Provider-neutral model contracts used before tool calling is introduced."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Protocol, TypeAlias

from agent_core.contracts import Target
from agent_core.domain import FailureSummary, FailureType, PlanStep


class ModelContractError(ValueError):
    """Raised when a confirmed model-boundary contract is violated."""


def _non_empty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelContractError(f"{field_name} must be a non-empty string")
    return value


class ModelRequestType(StrEnum):
    INTENT_GENERATION = "intent_generation"
    PLAN_GENERATION = "plan_generation"


class ModelErrorKind(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    EMPTY_RESPONSE = "empty_response"
    REFUSED = "refused"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_FAILURE = "provider_failure"


@dataclass(frozen=True, slots=True)
class FactSnapshot:
    fact_id: str
    content: str
    source_ref: str

    def __post_init__(self) -> None:
        _non_empty(self.fact_id, "fact_id")
        _non_empty(self.content, "content")
        if len(self.content) > 256:
            raise ModelContractError("Fact.content must be at most 256 Unicode code points")
        _non_empty(self.source_ref, "source_ref")


@dataclass(frozen=True, slots=True)
class IntentContext:
    intent_id: str
    description: str

    def __post_init__(self) -> None:
        _non_empty(self.intent_id, "intent_id")
        _non_empty(self.description, "description")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model_request_id: str
    task_run_id: str
    turn_id: str
    request_type: ModelRequestType
    goal: str
    target: Target
    turn_target: str
    verified_facts: tuple[FactSnapshot, ...]
    failure_summaries: tuple[FailureSummary, ...]
    intent: IntentContext | None = None
    correction_of_model_request_id: str | None = None
    short_reason: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.model_request_id, "model_request_id")
        _non_empty(self.task_run_id, "task_run_id")
        _non_empty(self.turn_id, "turn_id")
        if not isinstance(self.request_type, ModelRequestType):
            raise ModelContractError("request_type must be a ModelRequestType")
        _non_empty(self.goal, "goal")
        if not isinstance(self.target, Target):
            raise ModelContractError("target must be a Target")
        _non_empty(self.turn_target, "turn_target")
        if not isinstance(self.verified_facts, tuple):
            raise ModelContractError("verified_facts must be a tuple")
        if any(not isinstance(fact, FactSnapshot) for fact in self.verified_facts):
            raise ModelContractError("verified_facts must contain FactSnapshot values")
        if not isinstance(self.failure_summaries, tuple):
            raise ModelContractError("failure_summaries must be a tuple")
        if any(
            not isinstance(summary, FailureSummary)
            for summary in self.failure_summaries
        ):
            raise ModelContractError(
                "failure_summaries must contain FailureSummary values"
            )
        if any(
            summary.failure_type is not FailureType.TARGET_MISMATCH
            for summary in self.failure_summaries
        ):
            raise ModelContractError(
                "failure_summaries may only contain target_mismatch"
            )

        if self.request_type is ModelRequestType.PLAN_GENERATION:
            if not isinstance(self.intent, IntentContext):
                raise ModelContractError("plan_generation requires an IntentContext")
        elif self.intent is not None:
            raise ModelContractError("intent_generation must not include an IntentContext")

        correction_fields = (
            self.correction_of_model_request_id,
            self.short_reason,
        )
        if any(item is not None for item in correction_fields):
            if any(item is None for item in correction_fields):
                raise ModelContractError(
                    "correction request requires correction_of_model_request_id and short_reason"
                )
            _non_empty(
                self.correction_of_model_request_id,
                "correction_of_model_request_id",
            )
            _non_empty(self.short_reason, "short_reason")


@dataclass(frozen=True, slots=True)
class IntentCandidate:
    description: str
    intent_type: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.description, "description")
        if self.intent_type is not None:
            _non_empty(self.intent_type, "intent_type")


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    steps: tuple[PlanStep, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ModelContractError("steps must be a non-empty object array")
        if any(not isinstance(step, PlanStep) for step in self.steps):
            raise ModelContractError("steps must contain PlanStep values")


@dataclass(frozen=True, slots=True)
class NormalizedIntentResponse:
    """Adapter-normalized, but not yet structurally validated, Intent output."""

    candidates: object


@dataclass(frozen=True, slots=True)
class NormalizedPlanResponse:
    """Adapter-normalized, but not yet structurally validated, Plan output."""

    steps: object


NormalizedModelResponse: TypeAlias = (
    NormalizedIntentResponse | NormalizedPlanResponse
)


@dataclass(frozen=True, slots=True)
class IntentGenerationResponse:
    candidates: tuple[IntentCandidate, ...]


@dataclass(frozen=True, slots=True)
class PlanGenerationResponse:
    candidate: PlanCandidate


ModelResponse: TypeAlias = IntentGenerationResponse | PlanGenerationResponse


@dataclass(frozen=True, slots=True)
class ModelError:
    kind: ModelErrorKind
    short_reason: str
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ModelErrorKind):
            raise ModelContractError("kind must be a ModelErrorKind")
        _non_empty(self.short_reason, "short_reason")
        if self.evidence_ref is not None:
            _non_empty(self.evidence_ref, "evidence_ref")

    def to_dict(self) -> dict[str, str]:
        result = {"kind": self.kind.value, "short_reason": self.short_reason}
        if self.evidence_ref is not None:
            result["evidence_ref"] = self.evidence_ref
        return result


class CancellationToken(Protocol):
    @property
    def is_cancelled(self) -> bool:
        """Return whether further model work must stop."""


class DeadlinePort(Protocol):
    def remaining_seconds(self) -> float:
        """Return remaining TaskRun time without mutating TaskRun state."""


class ModelErrorEvidencePort(Protocol):
    def exists(self, task_run_id: str, evidence_ref: str) -> bool:
        """Return whether an error evidence reference belongs to a TaskRun."""


class ModelAdapter(Protocol):
    def invoke(
        self,
        request: ModelRequest,
        timeout_seconds: float,
        cancellation_token: CancellationToken,
    ) -> NormalizedModelResponse | ModelError:
        """Call one selected provider and normalize only its protocol."""


class StructuredResponseValidationError(ValueError):
    def __init__(self, kind: ModelErrorKind, short_reason: str) -> None:
        super().__init__(short_reason)
        self.kind = kind
        self.short_reason = short_reason


class StructuredResponseValidator:
    """Validate normalized candidates without knowing a provider protocol."""

    def validate(
        self,
        request: ModelRequest,
        response: NormalizedModelResponse,
    ) -> ModelResponse:
        if request.request_type is ModelRequestType.INTENT_GENERATION:
            return self._intent_response(response)
        return self._plan_response(response)

    def _intent_response(
        self,
        response: NormalizedModelResponse,
    ) -> IntentGenerationResponse:
        if not isinstance(response, NormalizedIntentResponse):
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE,
                "response does not match intent_generation",
            )
        if not isinstance(response.candidates, (list, tuple)):
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE,
                "intent candidates must be an array",
            )
        if not response.candidates:
            raise StructuredResponseValidationError(
                ModelErrorKind.EMPTY_RESPONSE,
                "intent candidates must not be empty",
            )
        try:
            candidates = tuple(
                self._intent_candidate(item) for item in response.candidates
            )
        except (ModelContractError, ValueError, TypeError) as exc:
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE,
                "intent candidate structure is invalid",
            ) from exc
        return IntentGenerationResponse(candidates=candidates)

    def _plan_response(
        self,
        response: NormalizedModelResponse,
    ) -> PlanGenerationResponse:
        if not isinstance(response, NormalizedPlanResponse):
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE,
                "response does not match plan_generation",
            )
        if not isinstance(response.steps, (list, tuple)):
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE,
                "plan steps must be an array",
            )
        if not response.steps:
            raise StructuredResponseValidationError(
                ModelErrorKind.EMPTY_RESPONSE,
                "plan steps must not be empty",
            )
        try:
            steps = tuple(self._plan_step(item) for item in response.steps)
        except (ModelContractError, ValueError, TypeError) as exc:
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE,
                "plan step structure is invalid",
            ) from exc
        return PlanGenerationResponse(candidate=PlanCandidate(steps=steps))

    @staticmethod
    def _intent_candidate(value: object) -> IntentCandidate:
        if isinstance(value, IntentCandidate):
            return value
        if not isinstance(value, Mapping):
            raise ModelContractError("intent candidate must be an object")
        intent_type = value.get("intent_type")
        if intent_type is not None and not isinstance(intent_type, str):
            raise ModelContractError("intent_type must be a string")
        return IntentCandidate(
            description=_non_empty(value.get("description"), "description"),
            intent_type=intent_type,
        )

    @staticmethod
    def _plan_step(value: object) -> PlanStep:
        if isinstance(value, PlanStep):
            return value
        if not isinstance(value, Mapping):
            raise ModelContractError("plan step must be an object")
        return PlanStep.from_dict(value)


class ModelInvocationCoordinator:
    """Own timeout, cancellation, validation, and one correction attempt."""

    def __init__(
        self,
        adapter: ModelAdapter,
        deadline: DeadlinePort,
        error_evidence: ModelErrorEvidencePort,
        *,
        validator: StructuredResponseValidator | None = None,
    ) -> None:
        self._adapter = adapter
        self._deadline = deadline
        self._error_evidence = error_evidence
        self._validator = validator or StructuredResponseValidator()

    def invoke(
        self,
        request: ModelRequest,
        cancellation_token: CancellationToken,
    ) -> ModelResponse | ModelError:
        first = self._invoke_adapter(request, cancellation_token)
        if isinstance(first, ModelError):
            return self._validate_model_error(request, first)

        try:
            return self._validator.validate(request, first)
        except StructuredResponseValidationError as issue:
            correction = self._correction_request(request, issue.short_reason)

        corrected = self._invoke_adapter(correction, cancellation_token)
        if isinstance(corrected, ModelError):
            return self._validate_model_error(correction, corrected)
        try:
            return self._validator.validate(correction, corrected)
        except StructuredResponseValidationError as issue:
            return ModelError(kind=issue.kind, short_reason=issue.short_reason)

    def _invoke_adapter(
        self,
        request: ModelRequest,
        cancellation_token: CancellationToken,
    ) -> NormalizedModelResponse | ModelError:
        if cancellation_token.is_cancelled:
            return ModelError(
                kind=ModelErrorKind.CANCELLED,
                short_reason="model invocation was cancelled before it started",
            )

        remaining = self._deadline.remaining_seconds()
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            raise ModelContractError("DeadlinePort.remaining_seconds must return a number")
        if remaining <= 0:
            return ModelError(
                kind=ModelErrorKind.TIMED_OUT,
                short_reason="TaskRun deadline has elapsed before model invocation",
            )
        return self._adapter.invoke(
            request,
            min(600, float(remaining)),
            cancellation_token,
        )

    def _validate_model_error(
        self,
        request: ModelRequest,
        error: ModelError,
    ) -> ModelError:
        if error.evidence_ref is None:
            return error
        if self._error_evidence.exists(request.task_run_id, error.evidence_ref):
            return error
        return ModelError(
            kind=ModelErrorKind.INVALID_RESPONSE,
            short_reason="model error evidence_ref is invalid for the current TaskRun",
        )

    @staticmethod
    def _correction_request(
        request: ModelRequest,
        short_reason: str,
    ) -> ModelRequest:
        return replace(
            request,
            model_request_id=str(uuid.uuid4()),
            correction_of_model_request_id=request.model_request_id,
            short_reason=short_reason,
        )
