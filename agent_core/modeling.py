"""Provider-neutral model contracts used before tool calling is introduced."""

from __future__ import annotations

import uuid
import time
import asyncio
import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Protocol, TypeAlias

if TYPE_CHECKING:
    from agent_core.model_invocation_audit import ModelInvocationAuditPort

from agent_core.contracts import Target
from agent_core.domain import ExecutionStatus, FailureSummary, FailureType, PlanStep
from agent_core.file_inspection import validate_inspection_path


class ModelContractError(ValueError):
    """Raised when a confirmed model-boundary contract is violated."""


def _non_empty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelContractError(f"{field_name} must be a non-empty string")
    return value


class ModelRequestType(StrEnum):
    INTENT_GENERATION = "intent_generation"
    PLAN_GENERATION = "plan_generation"
    INTENT_ACTION = "intent_action"


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
class ToolObservation:
    execution_id: str
    status: ExecutionStatus
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    context_clipped: bool

    def __post_init__(self) -> None:
        _non_empty(self.execution_id, "execution_id")
        if not isinstance(self.status, ExecutionStatus):
            raise ModelContractError("observation status must be an ExecutionStatus")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ModelContractError("observation streams must be text")
        if len(self.stdout) + len(self.stderr) > 4096:
            raise ModelContractError("observation streams exceed 4096 characters")
        if any(
            type(value) is not bool
            for value in (
                self.stdout_truncated, self.stderr_truncated, self.context_clipped
            )
        ):
            raise ModelContractError("observation truncation flags must be boolean")


@dataclass(frozen=True, slots=True)
class IntentActionContext:
    intent_id: str
    plan_id: str
    step_index: int
    goal_path: str
    latest_tool_observation: ToolObservation | None = None

    def __post_init__(self) -> None:
        _non_empty(self.intent_id, "intent_id")
        _non_empty(self.plan_id, "plan_id")
        if type(self.step_index) is not int or self.step_index != 0:
            raise ModelContractError("first T4 Intent action requires step_index=0")
        validate_inspection_path(self.goal_path)
        if self.latest_tool_observation is not None and not isinstance(
            self.latest_tool_observation, ToolObservation
        ):
            raise ModelContractError("latest_tool_observation must be a ToolObservation")


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
    action_context: IntentActionContext | None = None
    registration_mutation_id: str | None = None
    base_checkpoint_sequence: int | None = None
    context_sha256: str | None = None

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
            if self.action_context is not None:
                raise ModelContractError("plan_generation must not include action context")
        elif self.request_type is ModelRequestType.INTENT_ACTION:
            if not isinstance(self.intent, IntentContext) or not isinstance(
                self.action_context, IntentActionContext
            ):
                raise ModelContractError("intent_action requires Intent and action context")
            if self.action_context.intent_id != self.intent.intent_id:
                raise ModelContractError("Intent action context belongs to another Intent")
        elif self.intent is not None or self.action_context is not None:
            raise ModelContractError("intent_generation must not include Intent context")
        if self.request_type is not ModelRequestType.INTENT_GENERATION and any(
            value is not None for value in (
                self.registration_mutation_id,
                self.base_checkpoint_sequence,
                self.context_sha256,
            )
        ):
            raise ModelContractError("registration fields require intent_generation")

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


@dataclass(frozen=True, slots=True)
class NormalizedIntentActionResponse:
    action: object


@dataclass(frozen=True, slots=True)
class InspectFileAction:
    path: str


@dataclass(frozen=True, slots=True)
class FileTypeCandidateAction:
    path: str
    observed_type: str
    execution_id: str


@dataclass(frozen=True, slots=True)
class StopIntentAction:
    short_reason: str


IntentAction: TypeAlias = (
    InspectFileAction | FileTypeCandidateAction | StopIntentAction
)


def parse_intent_action(raw: object) -> IntentAction:
    if not isinstance(raw, Mapping):
        raise ModelContractError("intent_action requires one action object")
    fields = {
        "inspect_file": {"kind", "path"},
        "file_type_candidate": {"kind", "path", "observed_type", "execution_id"},
        "stop": {"kind", "short_reason"},
    }
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in fields or set(raw) != fields[kind]:
        raise ModelContractError("intent_action fields are invalid")
    if kind == "inspect_file":
        return InspectFileAction(validate_inspection_path(raw["path"]))
    if kind == "file_type_candidate":
        return FileTypeCandidateAction(
            validate_inspection_path(raw["path"]),
            _non_empty(raw["observed_type"], "observed_type"),
            _non_empty(raw["execution_id"], "execution_id"),
        )
    return StopIntentAction(_non_empty(raw["short_reason"], "short_reason"))


NormalizedModelResponse: TypeAlias = (
    NormalizedIntentResponse | NormalizedPlanResponse | NormalizedIntentActionResponse
)


@dataclass(frozen=True, slots=True)
class IntentGenerationResponse:
    candidates: tuple[IntentCandidate, ...]


@dataclass(frozen=True, slots=True)
class PlanGenerationResponse:
    candidate: PlanCandidate


@dataclass(frozen=True, slots=True)
class IntentActionResponse:
    action: IntentAction


ModelResponse: TypeAlias = (
    IntentGenerationResponse | PlanGenerationResponse | IntentActionResponse
)


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
        if request.request_type is ModelRequestType.PLAN_GENERATION:
            return self._plan_response(response)
        return self._intent_action_response(response)

    def _intent_action_response(
        self, response: NormalizedModelResponse
    ) -> IntentActionResponse:
        if not isinstance(response, NormalizedIntentActionResponse):
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE, "intent_action requires one action object"
            )
        try:
            action = parse_intent_action(response.action)
        except (ModelContractError, TypeError, ValueError) as exc:
            raise StructuredResponseValidationError(
                ModelErrorKind.INVALID_RESPONSE, "intent_action values are invalid"
            ) from exc
        return IntentActionResponse(action)

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
        audit: ModelInvocationAuditPort | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._adapter = adapter
        self._deadline = deadline
        self._error_evidence = error_evidence
        self._validator = validator or StructuredResponseValidator()
        self._audit = audit
        self._clock = clock

    @property
    def invocation_audit(self) -> ModelInvocationAuditPort | None:
        return self._audit

    def invoke(
        self,
        request: ModelRequest,
        cancellation_token: CancellationToken,
    ) -> ModelResponse | ModelError:
        first, correction_allowed = self._invoke_once(request, cancellation_token)
        if not correction_allowed or request.correction_of_model_request_id is not None:
            return first
        assert isinstance(first, ModelError)
        correction = self._correction_request(request, first.short_reason)
        corrected, _ = self._invoke_once(correction, cancellation_token)
        return corrected

    def _invoke_once(
        self, request: ModelRequest, cancellation_token: CancellationToken
    ) -> tuple[ModelResponse | ModelError, bool]:
        replay = self._begin_audit(request)
        if replay is not None:
            return replay
        raw = self._invoke_adapter(request, cancellation_token)
        return self._complete_invocation(request, raw)

    async def invoke_async(
        self, request: ModelRequest, cancellation_token: CancellationToken
    ) -> ModelResponse | ModelError:
        """Keep audit writes local; isolate an adapter's late result on interruption."""
        first, correction_allowed = await self._invoke_once_async(request, cancellation_token)
        if not correction_allowed or request.correction_of_model_request_id is not None:
            return first
        assert isinstance(first, ModelError)
        correction = self._correction_request(request, first.short_reason)
        corrected, _ = await self._invoke_once_async(correction, cancellation_token)
        return corrected

    async def _invoke_once_async(
        self, request: ModelRequest, cancellation_token: CancellationToken
    ) -> tuple[ModelResponse | ModelError, bool]:
        if cancellation_token.is_cancelled:
            return ModelError(ModelErrorKind.CANCELLED, "model call was cancelled"), False
        remaining = self._deadline.remaining_seconds()
        if remaining <= 0:
            return ModelError(ModelErrorKind.TIMED_OUT, "TaskRun deadline elapsed"), False
        replay = self._begin_audit(request)
        if replay is not None:
            return replay
        stopped = threading.Event()

        class InvocationToken:
            @property
            def is_cancelled(self) -> bool:
                return stopped.is_set() or cancellation_token.is_cancelled

        timeout = min(600.0, float(remaining))
        task = asyncio.create_task(asyncio.to_thread(
            self._adapter.invoke, request, timeout, InvocationToken()
        ))
        end = asyncio.get_running_loop().time() + timeout
        try:
            while True:
                cancelled = cancellation_token.is_cancelled
                expired = (
                    self._deadline.remaining_seconds() <= 0
                    or asyncio.get_running_loop().time() >= end
                )
                if cancelled or expired:
                    stopped.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return ModelError(
                        ModelErrorKind.CANCELLED if cancelled else ModelErrorKind.TIMED_OUT,
                        "model call interrupted; persisted outcome remains unknown",
                    ), False
                if task.done():
                    return self._complete_invocation(request, task.result())
                await asyncio.wait({task}, timeout=0.025)
        except BaseException:
            stopped.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _begin_audit(
        self, request: ModelRequest
    ) -> tuple[ModelResponse | ModelError, bool] | None:
        audit = self._audit
        if audit is not None:
            from agent_core.model_invocation_audit import (
                ModelInvocationRecoveryRequired,
                ModelInvocationStart,
            )

            scope_id = (request.intent.intent_id if request.intent is not None
                        else request.turn_id)
            existing = next(
                (
                    item
                    for item in audit.list_model_invocations(
                        request.task_run_id, scope_id
                    )
                    if item.start.model_request_id == request.model_request_id
                ),
                None,
            )
            start = ModelInvocationStart(
                request.model_request_id,
                request.task_run_id,
                scope_id,
                request.request_type,
                request.action_context.plan_id if request.action_context else None,
                request.correction_of_model_request_id,
                existing.start.started_at if existing is not None else self._clock(),
                request.registration_mutation_id,
                request.base_checkpoint_sequence,
                request.context_sha256,
                request.turn_target if request.request_type is ModelRequestType.INTENT_GENERATION else None,
            )
            previous = audit.begin_model_invocation(start)
            if previous is not None:
                if previous.completion is None:
                    raise ModelInvocationRecoveryRequired(
                        "model invocation result was not committed"
                    )
                return (
                    previous.completion.result,
                    previous.completion.result_kind == "invalid_structure",
                )
        return None

    def _complete_invocation(
        self, request: ModelRequest, raw: NormalizedModelResponse | ModelError
    ) -> tuple[ModelResponse | ModelError, bool]:
        audit = self._audit
        kind: Literal["plan", "intent_action", "invalid_structure", "model_error", "intent_generation"]
        if isinstance(raw, ModelError):
            result: ModelResponse | ModelError = self._validate_model_error(request, raw)
            kind = "model_error"
        else:
            try:
                result = self._validator.validate(request, raw)
                kind = (
                    "plan"
                    if isinstance(result, PlanGenerationResponse)
                    else "intent_action"
                    if isinstance(result, IntentActionResponse)
                    else "intent_generation"
                )
            except StructuredResponseValidationError as issue:
                result = ModelError(kind=issue.kind, short_reason=issue.short_reason)
                kind = "invalid_structure"
        if audit is not None:
            from agent_core.model_invocation_audit import ModelInvocationCompletion

            audit.complete_model_invocation(
                ModelInvocationCompletion(request.model_request_id, self._clock(), kind, result)
            )
        return result, kind == "invalid_structure"

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

    def _correction_request(
        self,
        request: ModelRequest,
        short_reason: str,
    ) -> ModelRequest:
        correction_id = str(uuid.uuid4())
        if self._audit is not None:
            scope_id = request.intent.intent_id if request.intent is not None else request.turn_id
            previous = [
                item.start.model_request_id
                for item in self._audit.list_model_invocations(
                    request.task_run_id, scope_id
                )
                if item.start.correction_of_model_request_id == request.model_request_id
            ]
            if len(previous) > 1:
                raise ModelContractError("model audit contains duplicate correction calls")
            if previous:
                correction_id = previous[0]
        return replace(
            request,
            model_request_id=correction_id,
            correction_of_model_request_id=request.model_request_id,
            short_reason=short_reason,
        )
