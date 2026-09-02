"""Synthetic provider fixtures for the model-contract test slice only."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Mapping

from agent_core.modeling import (
    CancellationToken,
    ModelAdapter,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ModelRequestType,
    NormalizedIntentResponse,
    NormalizedModelResponse,
    NormalizedPlanResponse,
)


RawFixture = object | Callable[[ModelRequest, float, CancellationToken], object]


@dataclass(frozen=True, slots=True)
class FakeInvocation:
    request: ModelRequest
    timeout_seconds: float


@dataclass
class _FakeAdapterBase(ModelAdapter):
    """Queue synthetic raw fixtures and expose normalized calls for assertions."""

    raw_responses: Deque[RawFixture] = field(default_factory=deque)
    invocations: list[FakeInvocation] = field(default_factory=list)

    def __init__(self, raw_responses: list[RawFixture] | None = None) -> None:
        self.raw_responses = deque(raw_responses or [])
        self.invocations = []

    def invoke(
        self,
        request: ModelRequest,
        timeout_seconds: float,
        cancellation_token: CancellationToken,
    ) -> NormalizedModelResponse | ModelError:
        if cancellation_token.is_cancelled:
            return ModelError(
                kind=ModelErrorKind.CANCELLED,
                short_reason="synthetic provider saw cancellation before invocation",
            )
        self.invocations.append(
            FakeInvocation(request=request, timeout_seconds=timeout_seconds)
        )
        if not self.raw_responses:
            return ModelError(
                kind=ModelErrorKind.PROVIDER_FAILURE,
                short_reason="synthetic provider has no queued raw response",
            )

        raw = self.raw_responses.popleft()
        if callable(raw):
            raw = raw(request, timeout_seconds, cancellation_token)
        if cancellation_token.is_cancelled:
            return ModelError(
                kind=ModelErrorKind.CANCELLED,
                short_reason="synthetic provider saw cancellation during invocation",
            )
        if raw is None or raw == "":
            return ModelError(
                kind=ModelErrorKind.EMPTY_RESPONSE,
                short_reason="synthetic provider returned no content",
            )
        return self._normalize(request, raw)

    def _normalize(
        self,
        request: ModelRequest,
        raw: object,
    ) -> NormalizedModelResponse | ModelError:
        raise NotImplementedError


def _empty_or_error(
    raw: object,
    *,
    code: object,
    message: object,
) -> ModelError | None:
    if raw is None or raw == "":
        return ModelError(
            kind=ModelErrorKind.EMPTY_RESPONSE,
            short_reason="synthetic provider returned no content",
        )
    if code is None:
        return None
    error_kinds = {
        "authentication_failed": ModelErrorKind.AUTHENTICATION_FAILED,
        "timed_out": ModelErrorKind.TIMED_OUT,
        "refused": ModelErrorKind.REFUSED,
        "provider_failure": ModelErrorKind.PROVIDER_FAILURE,
    }
    kind = error_kinds.get(code, ModelErrorKind.PROVIDER_FAILURE)
    return ModelError(
        kind=kind,
        short_reason=(message if isinstance(message, str) and message.strip() else str(code)),
    )


class FakeCodexAdapter(_FakeAdapterBase):
    """Synthetic format: {event, payload} with payload.{intents|steps}."""

    def _normalize(
        self,
        request: ModelRequest,
        raw: object,
    ) -> NormalizedModelResponse | ModelError:
        if not isinstance(raw, Mapping):
            return _normalized_for_request(request, None)
        error = _empty_or_error(
            raw,
            code=raw.get("code") if raw.get("event") == "error" else None,
            message=raw.get("message"),
        )
        if error is not None:
            return error
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            return _normalized_for_request(request, None)
        return _normalized_for_request(
            request,
            payload.get("intents")
            if request.request_type is ModelRequestType.INTENT_GENERATION
            else payload.get("steps"),
        )


class FakeClaudeCodeAdapter(_FakeAdapterBase):
    """Synthetic format: {type, content:[{type:'json', data:{...}}]}."""

    def _normalize(
        self,
        request: ModelRequest,
        raw: object,
    ) -> NormalizedModelResponse | ModelError:
        if not isinstance(raw, Mapping):
            return _normalized_for_request(request, None)
        error = _empty_or_error(
            raw,
            code=raw.get("error_type") if raw.get("type") == "error" else None,
            message=raw.get("message"),
        )
        if error is not None:
            return error
        blocks = raw.get("content")
        if not isinstance(blocks, list) or not blocks:
            return _normalized_for_request(request, None)
        first = blocks[0]
        if not isinstance(first, Mapping) or first.get("type") != "json":
            return _normalized_for_request(request, None)
        data = first.get("data")
        if not isinstance(data, Mapping):
            return _normalized_for_request(request, None)
        return _normalized_for_request(
            request,
            data.get("intents")
            if request.request_type is ModelRequestType.INTENT_GENERATION
            else data.get("steps"),
        )


class FakeDeepSeekAdapter(_FakeAdapterBase):
    """Synthetic format: {choices:[{message:{content:'JSON object'}}]}."""

    def _normalize(
        self,
        request: ModelRequest,
        raw: object,
    ) -> NormalizedModelResponse | ModelError:
        if not isinstance(raw, Mapping):
            return _normalized_for_request(request, None)
        error_value = raw.get("error")
        if isinstance(error_value, Mapping):
            error = _empty_or_error(
                raw,
                code=error_value.get("code"),
                message=error_value.get("message"),
            )
            if error is not None:
                return error
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            return _normalized_for_request(request, None)
        first = choices[0]
        if not isinstance(first, Mapping):
            return _normalized_for_request(request, None)
        message = first.get("message")
        if not isinstance(message, Mapping):
            return _normalized_for_request(request, None)
        content = message.get("content")
        if content is None or content == "":
            return ModelError(
                kind=ModelErrorKind.EMPTY_RESPONSE,
                short_reason="synthetic provider returned no content",
            )
        if not isinstance(content, str):
            return _normalized_for_request(request, None)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return _normalized_for_request(request, None)
        if not isinstance(data, Mapping):
            return _normalized_for_request(request, None)
        return _normalized_for_request(
            request,
            data.get("intents")
            if request.request_type is ModelRequestType.INTENT_GENERATION
            else data.get("steps"),
        )


def _normalized_for_request(
    request: ModelRequest,
    value: object,
) -> NormalizedModelResponse:
    if request.request_type is ModelRequestType.INTENT_GENERATION:
        return NormalizedIntentResponse(candidates=value)
    return NormalizedPlanResponse(steps=value)
