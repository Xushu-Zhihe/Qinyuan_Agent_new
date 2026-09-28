"""Audited model proposals; no command or control-plane side effects."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from agent_core.control_store import canonical_json
from agent_core.modeling import CancellationToken, ModelError, ModelErrorKind
from agent_core.action_audit import (
    ActionInvocationRecord,
    ActionInvocationStart,
    ActionRecoveryRequired,
    ActionResultKind,
    ActionResourceIds,
    T6ActionJournal,
)
from agent_core.actions import ProbeAction, T6Action, T6ActionError, parse_t6_action
from agent_core.action_model_request import T6ActionModelRequest


class T6ActionAdapterPort(Protocol):
    def invoke(
        self, request: T6ActionModelRequest, timeout_seconds: float,
        cancellation_token: CancellationToken,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class AuditedAction:
    record: ActionInvocationRecord
    result: T6Action | ModelError
    replayed: bool


def action_context_sha256(request: T6ActionModelRequest) -> str:
    payload = request.context_payload()
    for name in (
        "model_request_id", "correction_of_model_request_id", "short_reason",
    ):
        payload.pop(name)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _correction_id(original_id: str) -> str:
    raw = bytearray(hashlib.sha256(
        f"qinyuan/t6-action-correction/v1/{original_id}".encode()
    ).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


class AuditedT6ActionModel:
    def __init__(
        self, journal: T6ActionJournal, adapter: T6ActionAdapterPort,
        *, clock: Callable[[], float] = time.time,
    ) -> None:
        self._journal = journal
        self._adapter = adapter
        self._clock = clock

    def propose(
        self, request: T6ActionModelRequest, checkpoint_sequence: int,
        deadline_at: float, cancellation_token: CancellationToken,
    ) -> AuditedAction:
        if not isinstance(request, T6ActionModelRequest):
            raise TypeError("t6_action_request_required")
        if type(checkpoint_sequence) is not int or checkpoint_sequence < 1:
            raise ValueError("invalid_action_checkpoint")
        if not isinstance(deadline_at, (int, float)) or not math.isfinite(deadline_at):
            raise ValueError("invalid_action_deadline")
        saved = self._journal.resume_unapplied(request.task_run_id, request.intent_id)
        if saved is not None:
            record, saved_result = saved
            if record.completion is not None and record.completion.result_kind == "invalid_structure":
                self._journal.mark_applied(record.start.model_request_id, self._clock())
                return self.propose(
                    request, checkpoint_sequence, deadline_at, cancellation_token,
                )
            return AuditedAction(record, saved_result, True)
        if cancellation_token.is_cancelled or self._clock() >= deadline_at:
            raise ActionRecoveryRequired("model action no longer has a live budget")

        history = self._journal.list_for_intent(request.task_run_id, request.intent_id)
        if history and history[-1].completion is not None:
            if history[-1].completion.result_kind == "model_error":
                raise ActionRecoveryRequired("prior model error stops this Intent")
            if history[-1].completion.result_kind == "invalid_structure":
                prior = history[-1]
                if prior.start.correction_of_model_request_id is not None:
                    raise ActionRecoveryRequired("structure correction is exhausted")
                request = replace(
                    request,
                    model_request_id=_correction_id(prior.start.model_request_id),
                    correction_of_model_request_id=prior.start.model_request_id,
                    short_reason="invalid_t6_action_structure",
                )
                if (
                    checkpoint_sequence != prior.start.base_checkpoint_sequence
                    or action_context_sha256(request) != prior.start.context_sha256
                ):
                    raise ActionRecoveryRequired("structure correction context changed")

        start = ActionInvocationStart(
            request.model_request_id, request.task_run_id, request.intent_id,
            checkpoint_sequence, action_context_sha256(request),
            ActionResourceIds.create(), self._clock(),
            request.correction_of_model_request_id,
        )
        previous = self._journal.begin(start)
        if previous is not None:
            raise ActionRecoveryRequired("model action call already started")
        timeout = min(180.0, float(deadline_at - self._clock()))
        if timeout <= 0 or cancellation_token.is_cancelled:
            result: T6Action | ModelError = ModelError(
                ModelErrorKind.CANCELLED if cancellation_token.is_cancelled else ModelErrorKind.TIMED_OUT,
                "model_cancelled" if cancellation_token.is_cancelled else "model_budget_exhausted",
            )
            kind: ActionResultKind = "model_error"
        else:
            raw = self._adapter.invoke(request, timeout, cancellation_token)
            if isinstance(raw, ModelError):
                result = raw
                kind = "model_error"
            else:
                try:
                    result = parse_t6_action(raw)
                    if request.retry_probe is not None and (
                        not isinstance(result, ProbeAction)
                        or result.conditions != request.retry_probe.conditions
                    ):
                        raise T6ActionError("retry_probe_conditions_changed")
                    kind = "action"
                except (T6ActionError, TypeError, ValueError):
                    result = ModelError(ModelErrorKind.INVALID_RESPONSE, "invalid_t6_action_structure")
                    kind = (
                        "model_error" if request.correction_of_model_request_id is not None
                        else "invalid_structure"
                    )
        record = self._journal.save_result(start, self._clock(), kind, result)
        if kind == "invalid_structure":
            self._journal.mark_applied(start.model_request_id, self._clock())
            return self.propose(request, checkpoint_sequence, deadline_at, cancellation_token)
        return AuditedAction(record, result, False)
