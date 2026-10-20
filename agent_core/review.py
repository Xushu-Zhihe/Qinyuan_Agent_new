"""Trusted D2 review coordination and constraint application."""

from __future__ import annotations

from agent_core.review_policy import ACTION_KINDS, MAX_ACTION_TIMEOUT

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, NoReturn, cast
from uuid import uuid4

from agent_core.review_contracts import (
    MAX_REVIEW_ATTEMPTS, LEGACY_REVIEW_CALL_LIMIT,
    D2AllowedActionKinds,
    D2Constraint,
    D2MaxActionTimeout,
    D2ModelFailure,
    D2ModelResponse,
    D2ReviewLedger,
    D2ReviewModel,
    D2ReviewOutput,
    D2ReviewRequest,
    d2_payload,
    parse_d2_output,
)


class D2ReviewPaused(RuntimeError):
    def __init__(self, reason: str, *, outcome: str = "paused") -> None:
        self.reason = reason
        self.outcome = outcome
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class D2ReviewResult:
    review_root_id: str
    review_id: str
    review_generation: int
    attempts_used: int
    outcome: Literal["accepted", "reused", "paused", "rejected"]
    output: D2ReviewOutput | None
    reason: str | None = None


def _decode_constraint(raw: object) -> D2Constraint:
    if not isinstance(raw, dict):
        raise D2ReviewPaused("persisted_constraint_invalid")
    if raw.get("kind") == "allowed_action_kinds":
        return D2AllowedActionKinds(
            raw["scope_kind"],
            raw["scope_id"],
            tuple(raw["allowed_action_kinds"]),  # type: ignore[arg-type]
        )
    if raw.get("kind") == "max_action_timeout_seconds":
        return D2MaxActionTimeout(
            raw["scope_kind"],
            raw["scope_id"],
            raw["max_action_timeout_seconds"],  # type: ignore[arg-type]
        )
    raise D2ReviewPaused("persisted_constraint_kind_invalid")


def _saved_request(raw: object) -> D2ReviewRequest:
    if not isinstance(raw, dict):
        raise D2ReviewPaused("persisted_review_request_invalid")
    fields = dict(raw)
    fields["source_versions"] = tuple(fields["source_versions"])
    try:
        return D2ReviewRequest(**fields)
    except (TypeError, ValueError) as exc:
        raise D2ReviewPaused("persisted_review_request_invalid") from exc


class D2ReviewCoordinator:
    """Begin before I/O; replay known results; apply through a trusted owner."""

    MAX_ATTEMPTS_PER_ROOT = MAX_REVIEW_ATTEMPTS
    MAX_REVIEW_CALLS_PER_TASK_RUN = LEGACY_REVIEW_CALL_LIMIT

    def __init__(
        self,
        ledger: D2ReviewLedger,
        *,
        apply: Callable[[D2ReviewRequest, D2ReviewOutput], str] | None = None,
        pause: Callable[[D2ReviewRequest, str, str], None] | None = None,
        refresh: Callable[[D2ReviewRequest], D2ReviewRequest] | None = None,
        committed: Callable[[D2ReviewRequest], bool] | None = None,
        parse_output: Callable[[object, D2ReviewRequest], D2ReviewOutput] = parse_d2_output,
    ) -> None:
        self.ledger = ledger
        self._apply_callback = apply
        self._pause_callback = pause
        self._refresh_callback = refresh
        self._committed_callback = committed
        self._parse_output = parse_output

    def _pause(
        self, request: D2ReviewRequest, reason: str, outcome: str = "paused"
    ) -> NoReturn:
        self.ledger.record_pause(request, reason, outcome)
        if self._pause_callback is not None:
            self._pause_callback(request, reason, outcome)
        raise D2ReviewPaused(reason, outcome=outcome)

    @staticmethod
    def _validate_root(request: D2ReviewRequest, root: dict[str, object]) -> None:
        if any(
            root.get(name) != getattr(request, name)
            for name in (
                "task_run_id",
                "turn_id",
                "intent_id",
                "review_scope",
                "action_candidate_id",
            )
        ):
            raise D2ReviewPaused("review_root_identity_mismatch")

    @staticmethod
    def _same_snapshot(first: D2ReviewRequest, second: D2ReviewRequest) -> bool:
        return (
            first.context_version,
            first.source_versions,
            first.constraint_version,
        ) == (
            second.context_version,
            second.source_versions,
            second.constraint_version,
        )

    def _refresh(self, request: D2ReviewRequest) -> D2ReviewRequest:
        try:
            refreshed = (
                self._refresh_callback(request) if self._refresh_callback else request
            )
        except D2ReviewPaused as exc:
            self._pause(request, exc.reason, exc.outcome)
        if not isinstance(refreshed, D2ReviewRequest) or any(
            getattr(refreshed, name) != getattr(request, name)
            for name in (
                "review_root_id",
                "task_run_id",
                "turn_id",
                "intent_id",
                "review_scope",
                "action_candidate_id",
            )
        ):
            self._pause(request, "review_refresh_identity_mismatch")
        return refreshed

    def _constraints(
        self,
        task_run_id: str,
        turn_id: str,
        intent_id: str,
        *,
        live_turn_ids: tuple[str, ...] | None = None,
        live_intent_ids: tuple[str, ...] | None = None,
    ) -> tuple[D2Constraint, ...]:
        constraints = []
        for root in self.ledger.list_roots(task_run_id):
            raw = root.get("active_constraints", [])
            if not isinstance(raw, list):
                raise D2ReviewPaused("active_constraints_invalid")
            for item in raw:
                constraint = _decode_constraint(item)
                if constraint.scope_kind == "turn":
                    active = constraint.scope_id == turn_id and (
                        live_turn_ids is None or constraint.scope_id in live_turn_ids
                    )
                else:
                    active = constraint.scope_id == intent_id and (
                        live_intent_ids is None
                        or constraint.scope_id in live_intent_ids
                    )
                if active:
                    constraints.append(constraint)
        return tuple(constraints)

    def _validate_constraints(
        self, request: D2ReviewRequest, output: D2ReviewOutput
    ) -> None:
        if not set(output.source_ids).issubset(request.source_versions):
            raise ValueError("review_source_not_in_current_snapshot")
        actions = set(ACTION_KINDS)
        timeout = MAX_ACTION_TIMEOUT
        for item in self._constraints(
            request.task_run_id, request.turn_id, request.intent_id
        ):
            if isinstance(item, D2AllowedActionKinds):
                actions &= set(item.allowed_action_kinds)
            else:
                timeout = min(timeout, item.max_action_timeout_seconds)
        if not actions:
            raise D2ReviewPaused("constraint_action_intersection_empty")
        for item in output.constraints:
            expected_id = (
                request.intent_id if item.scope_kind == "intent" else request.turn_id
            )
            if item.scope_id != expected_id:
                raise ValueError("constraint_scope_mismatch")
            if isinstance(item, D2AllowedActionKinds):
                values = set(item.allowed_action_kinds)
                if not values.issubset(actions):
                    raise ValueError("constraint_widens_allowed_actions")
                actions &= values
            else:
                if item.max_action_timeout_seconds > timeout:
                    raise ValueError("constraint_widens_timeout")
                timeout = min(timeout, item.max_action_timeout_seconds)

    def _apply(
        self,
        request: D2ReviewRequest,
        output: D2ReviewOutput,
        attempts: int,
        *,
        reused: bool = False,
    ) -> D2ReviewResult:
        try:
            application_ref = (
                self._apply_callback(request, output)
                if self._apply_callback
                else (f"d2-review:{request.review_id}")
            )
            self.ledger.record_application(request, output, application_ref)
        except Exception as exc:  # The Controller application outcome may be unknown.
            self._pause(request, "review_application_uncommitted", "accepted")
            raise AssertionError("unreachable") from exc
        return D2ReviewResult(
            request.review_root_id,
            request.review_id,
            request.review_generation,
            attempts,
            "reused" if reused else "accepted",
            output,
        )

    def review(
        self,
        request: D2ReviewRequest,
        model: D2ReviewModel,
        *,
        trigger_reasons: tuple[str, ...] = (),
    ) -> D2ReviewResult:
        self.ledger.record_trigger(request, trigger_reasons)
        while True:
            root = self.ledger.read_root(request.review_root_id)
            attempts = 0
            if root is not None:
                self._validate_root(request, root)
                attempts = cast(int, root.get("attempts_used", 0))
                outcome = root.get("last_outcome")
                if outcome in {
                    "started",
                    "unknown",
                    "provider_failure",
                    "authentication_failed",
                    "refused",
                }:
                    failure = "unknown" if outcome == "started" else str(outcome)
                    boundary = getattr(self.ledger, "output_boundary", None)
                    saved = _saved_request(root.get("last_request"))
                    if not (outcome == "provider_failure" and boundary is not None
                            and boundary.retryable_technical_failure(saved)):
                        self._pause(request, failure, failure)
                applied = cast(list[dict], root.get("applied", []))
                if applied:
                    last = applied[-1]
                    saved = _saved_request(root.get("last_request"))
                    if (
                        self._same_snapshot(saved, request)
                        and last["review_id"] == saved.review_id
                    ):
                        return D2ReviewResult(
                            request.review_root_id,
                            last["review_id"],
                            last["review_generation"],
                            attempts,
                            "reused",
                            self._parse_output(last["output"], saved),
                        )
                if outcome == "accepted":
                    saved = _saved_request(root.get("last_request"))
                    applied_ids = {item.get("review_id") for item in applied}
                    if saved.review_id not in applied_ids:
                        output = self._parse_output(root.get("last_response"), saved)
                        if not set(output.source_ids).issubset(saved.source_versions):
                            self._pause(
                                saved, "saved_review_sources_invalid", "rejected"
                            )
                        # A committed mutation is immutable; only repair its evidence receipt.
                        if self._committed_callback and self._committed_callback(saved):
                            return self._apply(saved, output, attempts, reused=True)
                        refreshed = self._refresh(request)
                        if self._same_snapshot(saved, refreshed):
                            self._validate_constraints(refreshed, output)
                            return self._apply(saved, output, attempts, reused=True)
                        request = refreshed
            request = self._refresh(request)
            if attempts >= self.MAX_ATTEMPTS_PER_ROOT:
                self._pause(request, "review_retry_exhausted")
            call_limit = getattr(self.ledger, "review_call_limit", self.MAX_REVIEW_CALLS_PER_TASK_RUN)
            if call_limit is not None and self.ledger.review_call_count(request.task_run_id) >= call_limit:
                self._pause(request, "review_budget_exhausted")
            generation = request.review_generation
            if root is not None and root.get("last_request") is not None:
                saved = _saved_request(root["last_request"])
                generation = saved.review_generation + (
                    not self._same_snapshot(saved, request)
                )
            current = replace(
                request,
                review_id=str(uuid4()),
                review_attempt=attempts + 1,
                review_generation=generation,
            )
            self.ledger.record_trigger(current, trigger_reasons)
            self.ledger.begin_attempt(current)
            try:
                raw = model.invoke(current)
            except Exception as exc:  # noqa: BLE001 -- no reliable model completion exists
                self.ledger.finish_attempt(
                    current,
                    outcome="unknown",
                    response=None,
                    provider_request_id=None,
                    reason=f"model_exception:{type(exc).__name__}",
                )
                self._pause(current, "review_call_unknown", "unknown")
            provider_request_id = None
            if isinstance(raw, D2ModelResponse):
                provider_request_id = raw.provider_request_id
                raw = raw.response
            response = (
                d2_payload(raw)
                if isinstance(raw, (D2ReviewOutput, D2ModelFailure))
                else raw
            )
            if isinstance(raw, D2ModelFailure) or raw is None:
                outcome = (
                    raw.kind if isinstance(raw, D2ModelFailure) else "empty_response"
                )
                self.ledger.finish_attempt(
                    current,
                    outcome=outcome,
                    response=response,
                    provider_request_id=(
                        raw.provider_request_id if raw else provider_request_id
                    ),
                    reason=raw.reason if raw else "empty_response",
                )
                if outcome not in {"empty_response", "invalid_response"}:
                    boundary = getattr(self.ledger, "output_boundary", None)
                    if not (outcome == "provider_failure" and boundary is not None
                            and boundary.retryable_technical_failure(current)):
                        self._pause(current, outcome, outcome)
                if current.review_attempt >= self.MAX_ATTEMPTS_PER_ROOT:
                    self._pause(current, "review_retry_exhausted", outcome)
                request = current
                continue
            try:
                output = self._parse_output(raw, current)
                self._validate_constraints(current, output)
            except (TypeError, ValueError) as exc:
                self.ledger.finish_attempt(
                    current,
                    outcome="rejected",
                    response=response,
                    provider_request_id=provider_request_id,
                    reason=str(exc),
                )
                if current.review_attempt >= self.MAX_ATTEMPTS_PER_ROOT:
                    self._pause(current, "review_retry_exhausted", "rejected")
                request = current
                continue
            self.ledger.finish_attempt(
                current,
                outcome="accepted",
                response=response,
                provider_request_id=provider_request_id,
            )
            refreshed = self._refresh(current)
            if not self._same_snapshot(current, refreshed):
                request = refreshed
                continue
            return self._apply(current, output, current.review_attempt)

    def validate_future_action(
        self,
        review_root_ids: tuple[str, ...] = (),
        *,
        action_kind: Literal["probe", "complete_intent"],
        timeout_seconds: int | None,
        task_run_id: str | None = None,
        turn_id: str | None = None,
        intent_id: str | None = None,
        live_turn_ids: tuple[str, ...] | None = None,
        live_intent_ids: tuple[str, ...] | None = None,
    ) -> None:
        """Check all run restrictions bound to the still-live target scope."""
        if action_kind not in set(ACTION_KINDS):
            raise D2ReviewPaused("action_kind_not_authorized")
        if action_kind == "probe" and (
            type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300
        ):
            raise D2ReviewPaused("action_timeout_invalid")
        if task_run_id is None:
            if not review_root_ids:
                return
            root = self.ledger.read_root(review_root_ids[0])
            if root is None:
                raise D2ReviewPaused("review_root_missing")
            task_run_id, turn_id, intent_id = (
                str(root[name]) for name in ("task_run_id", "turn_id", "intent_id")
            )
        if turn_id is None or intent_id is None:
            raise ValueError("current_review_scope_required")
        allowed = set(ACTION_KINDS)
        upper = 300
        for item in self._constraints(
            task_run_id,
            turn_id,
            intent_id,
            live_turn_ids=live_turn_ids,
            live_intent_ids=live_intent_ids,
        ):
            if isinstance(item, D2AllowedActionKinds):
                allowed &= set(item.allowed_action_kinds)
            else:
                upper = min(upper, item.max_action_timeout_seconds)
        if not allowed:
            raise D2ReviewPaused("constraint_action_intersection_empty")
        if action_kind not in allowed:
            raise D2ReviewPaused(
                "action_kind_rejected_by_review_constraint", outcome="rejected"
            )
        if (
            action_kind == "probe"
            and timeout_seconds is not None
            and timeout_seconds > upper
        ):
            raise D2ReviewPaused(
                "action_timeout_rejected_by_review_constraint", outcome="rejected"
            )


@dataclass(frozen=True, slots=True)
class D2NotCalledResult:
    review_root_id: str
    review_id: str
    review_generation: int
    outcome: Literal["not_called"] = "not_called"


class D2ReviewSession:
    """Gate review timing while existing owners retain state and tool side effects."""

    def __init__(self, coordinator: D2ReviewCoordinator) -> None:
        self.coordinator = coordinator

    def turn_start(
        self, request: D2ReviewRequest, model: D2ReviewModel
    ) -> D2ReviewResult:
        if (
            request.review_scope != "turn_start"
            or request.action_candidate_id is not None
        ):
            raise ValueError("turn_start_review_request_invalid")
        return self.coordinator.review(request, model, trigger_reasons=("turn_start",))

    def action(
        self,
        request: D2ReviewRequest,
        model: D2ReviewModel,
        *,
        action_finished: bool,
        evidence_saved: bool,
        triggered: bool,
        trigger_reasons: tuple[str, ...] = (),
        deterministic_rejection: bool = False,
    ) -> D2ReviewResult | D2NotCalledResult:
        if request.review_scope != "action" or request.action_candidate_id is None:
            raise ValueError("action_review_request_invalid")
        if not triggered:
            self.coordinator.ledger.record_trigger(request, ("no_review_trigger",))
            self.coordinator.ledger.record_not_called(request, "no_review_trigger")
            return D2NotCalledResult(
                request.review_root_id, request.review_id, request.review_generation
            )
        self.coordinator.ledger.record_trigger(request, trigger_reasons)
        if not evidence_saved or (not action_finished and not deterministic_rejection):
            raise D2ReviewPaused("action_result_or_evidence_not_saved")
        return self.coordinator.review(request, model, trigger_reasons=trigger_reasons)

    def turn_end(
        self,
        request: D2ReviewRequest,
        model: D2ReviewModel,
        *,
        turn_completed: bool,
        cancelled: bool = False,
        deadline_exceeded: bool = False,
    ) -> D2ReviewResult | D2NotCalledResult:
        if (
            request.review_scope != "turn_end"
            or request.action_candidate_id is not None
        ):
            raise ValueError("turn_end_review_request_invalid")
        if not turn_completed:
            raise D2ReviewPaused("turn_not_completed")
        if cancelled or deadline_exceeded:
            reason = (
                "cancelled_before_tail_review"
                if cancelled
                else "deadline_before_tail_review"
            )
            self.coordinator.ledger.record_not_called(request, reason)
            return D2NotCalledResult(
                request.review_root_id, request.review_id, request.review_generation
            )
        try:
            return self.coordinator.review(
                request, model, trigger_reasons=("turn_end",)
            )
        except D2ReviewPaused as exc:
            if exc.reason == "review_budget_exhausted":
                self.coordinator.ledger.record_not_called(request, exc.reason)
            raise
