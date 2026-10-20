"""Frozen internal contracts for the D2 review slice."""

from __future__ import annotations

from agent_core.review_policy import ACTION_KINDS, MAX_ACTION_TIMEOUT

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast
from uuid import UUID

MAX_REVIEW_ATTEMPTS = 2
LEGACY_REVIEW_CALL_LIMIT = 8

D2ReviewScope = Literal["turn_start", "action", "turn_end"]
D2ReviewDecision = Literal["continue_current_intent", "acknowledge_turn_end"]
D2ReviewOutcome = Literal[
    "accepted",
    "rejected",
    "empty_response",
    "invalid_response",
    "authentication_failed",
    "refused",
    "provider_failure",
    "unknown",
    "not_called",
]
D2ConstraintKind = Literal["allowed_action_kinds", "max_action_timeout_seconds"]


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string")


def _trusted_id(value: object, name: str) -> None:
    _text(value, name)
    parsed = UUID(value)  # type: ignore[arg-type]
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical trusted UUID")


def _finite_int(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")


def d2_payload(value: object) -> dict[str, object]:
    return asdict(value) if hasattr(value, "__dataclass_fields__") else {}  # type: ignore[call-overload]


def d2_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class D2AllowedActionKinds:
    scope_kind: Literal["intent", "turn"]
    scope_id: str
    allowed_action_kinds: tuple[Literal["probe", "complete_intent"], ...]
    kind: Literal["allowed_action_kinds"] = "allowed_action_kinds"

    def __post_init__(self) -> None:
        _text(self.scope_id, "scope_id")
        if self.scope_kind not in {"intent", "turn"}:
            raise ValueError("invalid constraint scope")
        if not isinstance(self.allowed_action_kinds, tuple) or any(
            not isinstance(item, str) for item in self.allowed_action_kinds
        ):
            raise ValueError("allowed_action_kinds must be immutable text values")
        if not self.allowed_action_kinds or len(set(self.allowed_action_kinds)) != len(
            self.allowed_action_kinds
        ):
            raise ValueError("allowed_action_kinds must be a non-empty set")
        if any(
            item not in set(ACTION_KINDS)
            for item in self.allowed_action_kinds
        ):
            raise ValueError("unsupported allowed action kind")


@dataclass(frozen=True, slots=True)
class D2MaxActionTimeout:
    scope_kind: Literal["intent", "turn"]
    scope_id: str
    max_action_timeout_seconds: int
    kind: Literal["max_action_timeout_seconds"] = "max_action_timeout_seconds"

    def __post_init__(self) -> None:
        _text(self.scope_id, "scope_id")
        if self.scope_kind not in {"intent", "turn"}:
            raise ValueError("invalid constraint scope")
        _finite_int(
            self.max_action_timeout_seconds, "max_action_timeout_seconds", 1, MAX_ACTION_TIMEOUT
        )


D2Constraint = D2AllowedActionKinds | D2MaxActionTimeout


@dataclass(frozen=True, slots=True)
class D2ReviewRequest:
    review_id: str
    review_root_id: str
    review_generation: int
    review_attempt: int
    review_scope: D2ReviewScope
    task_run_id: str
    turn_id: str
    intent_id: str
    action_candidate_id: str | None
    checkpoint_id: int
    context_version: str
    source_versions: tuple[str, ...]
    constraint_version: int
    context_summary: str

    def __post_init__(self) -> None:
        for name in (
            "review_id",
            "review_root_id",
            "task_run_id",
            "turn_id",
            "intent_id",
        ):
            _trusted_id(getattr(self, name), name)
        if self.action_candidate_id is not None:
            _trusted_id(self.action_candidate_id, "action_candidate_id")
        if self.review_scope not in {"turn_start", "action", "turn_end"}:
            raise ValueError("unsupported review scope")
        if self.review_scope == "action" and self.action_candidate_id is None:
            raise ValueError("action review requires action_candidate_id")
        if self.review_scope != "action" and self.action_candidate_id is not None:
            raise ValueError("turn review must not have action_candidate_id")
        _finite_int(self.review_generation, "review_generation", 1, 2**31 - 1)
        _finite_int(self.review_attempt, "review_attempt", 1, MAX_REVIEW_ATTEMPTS)
        _finite_int(self.checkpoint_id, "checkpoint_id", 1, 2**63 - 1)
        _finite_int(self.constraint_version, "constraint_version", 0, 2**31 - 1)
        _text(self.context_version, "context_version")
        _text(self.context_summary, "context_summary")
        if not isinstance(self.source_versions, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.source_versions
        ):
            raise ValueError("source_versions must be immutable text references")


@dataclass(frozen=True, slots=True)
class D2ReviewOutput:
    decision: D2ReviewDecision
    rationale: str
    source_ids: tuple[str, ...]
    constraints: tuple[D2Constraint, ...]

    def __post_init__(self) -> None:
        if self.decision not in {"continue_current_intent", "acknowledge_turn_end"}:
            raise ValueError("unsupported review decision")
        _text(self.rationale, "rationale")
        if not isinstance(self.source_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.source_ids
        ):
            raise ValueError("source_ids must be immutable text references")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source_ids must be unique")
        if not isinstance(self.constraints, tuple) or any(
            not isinstance(item, (D2AllowedActionKinds, D2MaxActionTimeout))
            for item in self.constraints
        ):
            raise ValueError("constraints must be immutable typed restrictions")
        if self.decision == "acknowledge_turn_end" and self.constraints:
            raise ValueError("turn_end review cannot add constraints")


@dataclass(frozen=True, slots=True)
class D2ModelFailure:
    kind: Literal[
        "empty_response",
        "invalid_response",
        "authentication_failed",
        "refused",
        "provider_failure",
        "unknown",
    ]
    reason: str
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "empty_response",
            "invalid_response",
            "authentication_failed",
            "refused",
            "provider_failure",
            "unknown",
        }:
            raise ValueError("unsupported model failure")
        _text(self.reason, "reason")
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "provider_request_id")


@dataclass(frozen=True, slots=True)
class D2ModelResponse:
    """Transport metadata around an untrusted successful provider payload."""

    response: object
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_request_id is not None:
            _text(self.provider_request_id, "provider_request_id")


class D2ReviewModel(Protocol):
    def invoke(self, request: D2ReviewRequest) -> object: ...


class D2ReviewLedger(Protocol):
    def read_root(self, review_root_id: str) -> dict[str, object] | None: ...

    def review_call_count(self, task_run_id: str | None = None) -> int: ...

    def list_roots(self, task_run_id: str) -> tuple[dict[str, object], ...]: ...

    def begin_attempt(self, request: D2ReviewRequest) -> None: ...

    def finish_attempt(
        self,
        request: D2ReviewRequest,
        *,
        outcome: D2ReviewOutcome,
        response: object,
        provider_request_id: str | None,
        reason: str | None = None,
    ) -> None: ...

    def record_trigger(
        self, request: D2ReviewRequest, reasons: tuple[str, ...]
    ) -> None: ...

    def record_pause(
        self, request: D2ReviewRequest, reason: str, outcome: str
    ) -> None: ...

    def record_attempt(
        self,
        request: D2ReviewRequest,
        *,
        outcome: D2ReviewOutcome,
        response: object,
        provider_request_id: str | None,
    ) -> None: ...

    def record_application(
        self, request: D2ReviewRequest, output: D2ReviewOutput, application_ref: str
    ) -> None: ...

    def record_not_called(self, request: D2ReviewRequest, reason: str) -> None: ...


def parse_d2_output(raw: object, request: D2ReviewRequest) -> D2ReviewOutput:
    if isinstance(raw, D2ReviewOutput):
        raw = json.loads(json.dumps(d2_payload(raw), allow_nan=False))
    if not isinstance(raw, dict) or set(raw) != {
        "decision",
        "rationale",
        "source_ids",
        "constraints",
    }:
        raise ValueError("review_output_fields_invalid")
    decision = raw["decision"]
    if not isinstance(decision, str) or decision not in {
        "continue_current_intent",
        "acknowledge_turn_end",
    }:
        raise ValueError("review_decision_invalid")
    rationale = raw["rationale"]
    source_ids = raw["source_ids"]
    constraints = raw["constraints"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("review_rationale_invalid")
    if not isinstance(source_ids, list) or any(
        not isinstance(item, str) for item in source_ids
    ):
        raise ValueError("review_sources_invalid")
    if not isinstance(constraints, list):
        raise TypeError("review_constraints_invalid")
    parsed: list[D2Constraint] = []
    for item in constraints:
        if not isinstance(item, dict):
            raise TypeError("review_constraint_invalid")
        kind = item.get("kind")
        if kind == "allowed_action_kinds":
            if set(item) != {"kind", "scope_kind", "scope_id", "allowed_action_kinds"}:
                raise ValueError("review_constraint_fields_invalid")
            values = item["allowed_action_kinds"]
            if not isinstance(values, list) or any(
                value not in set(ACTION_KINDS) for value in values
            ):
                raise ValueError("review_allowed_actions_invalid")
            parsed.append(
                D2AllowedActionKinds(
                    item["scope_kind"], item["scope_id"], tuple(values)
                )
            )  # type: ignore[arg-type]
        elif kind == "max_action_timeout_seconds":
            if set(item) != {
                "kind",
                "scope_kind",
                "scope_id",
                "max_action_timeout_seconds",
            }:
                raise ValueError("review_constraint_fields_invalid")
            parsed.append(
                D2MaxActionTimeout(
                    item["scope_kind"],
                    item["scope_id"],
                    item["max_action_timeout_seconds"],
                )
            )  # type: ignore[arg-type]
        else:
            raise ValueError("review_constraint_kind_invalid")
    output = D2ReviewOutput(
        cast(D2ReviewDecision, decision), rationale, tuple(source_ids), tuple(parsed)
    )
    if request.review_scope == "turn_end" and output.decision != "acknowledge_turn_end":
        raise ValueError("turn_end_decision_invalid")
    if (
        request.review_scope != "turn_end"
        and output.decision != "continue_current_intent"
    ):
        raise ValueError("continue_decision_invalid")
    return output
