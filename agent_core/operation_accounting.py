"""Frozen operation admission, budget, and audit contracts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID

D4ChargedCategory = Literal[
    "planning",
    "action",
    "review",
    "refinement",
    "tool_dispatch",
    "sha_observation",
    "artifact_share",
    "attachment_import",
    "fact_review",
]
D4OperationCategory = Literal[
    "planning",
    "action",
    "review",
    "refinement",
    "tool_dispatch",
    "sha_observation",
    "artifact_share",
    "attachment_import",
    "copy_preparation",
    "intent_stop",
    "fact_review",
]
D4OperationResult = Literal["completed", "rejected", "stale", "not_satisfied", "failed"]
D4AuditCategory = Literal[
    "proposal_evidence_saved",
    "control_commit_started",
    "control_commit_committed",
    "control_commit_failed",
    "control_reload_confirmed",
    "cleanup_started",
    "cleanup_completed",
    "cleanup_failed",
    "resource_presence_verified",
    "cancellation_requested",
]
D4AuditOutcome = Literal["known", "unknown", "failed", "present", "absent"]
D4_CATEGORY_LIMITS: dict[str, int] = {
    "planning": 4,
    "action": 16,
    "review": 8,
    "refinement": 4,
    "tool_dispatch": 6,
    "sha_observation": 4,
    "artifact_share": 4,
    "attachment_import": 2,
}
D4_AUDIT_ONLY_CATEGORIES = frozenset({"copy_preparation", "intent_stop"})


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")


def _uuid(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a trusted UUID") from exc
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5")


def _sha(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _time(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite timestamp")


@dataclass(frozen=True, slots=True)
class D4BudgetPolicy:
    total: int = 48
    planning: int = 4
    action: int = 16
    review: int = 8
    refinement: int = 4
    tool_dispatch: int = 6
    sha_observation: int = 4
    artifact_share: int = 4
    attachment_import: int = 2

    def __post_init__(self) -> None:
        expected = {"total": 48, **D4_CATEGORY_LIMITS}
        if any(
            type(getattr(self, name)) is not int or getattr(self, name) != limit
            for name, limit in expected.items()
        ):
            raise ValueError("D4 supports only the confirmed 48-start budget matrix")

    @property
    def category_limits(self) -> dict[str, int]:
        return dict(D4_CATEGORY_LIMITS)

    @property
    def budget_policy_ref(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D4BudgetPolicy:
        if set(value) != {"total", *D4_CATEGORY_LIMITS}:
            raise ValueError("invalid D4BudgetPolicy fields")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D8BudgetPolicy(D4BudgetPolicy):
    """Explicit D8 policy; never inferred for an existing D4 run."""

    total: int = 56
    fact_review: int = 8

    def __post_init__(self) -> None:
        expected = {"total": 56, **D4_CATEGORY_LIMITS, "fact_review": 8}
        if any(type(getattr(self, key)) is not int or getattr(self, key) != limit
               for key, limit in expected.items()):
            raise ValueError("D8 supports only the confirmed 56-start budget matrix")

    @property
    def category_limits(self) -> dict[str, int]:
        return {**D4_CATEGORY_LIMITS, "fact_review": 8}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D8BudgetPolicy:
        if set(value) != {"total", *D4_CATEGORY_LIMITS, "fact_review"}:
            raise ValueError("invalid D8BudgetPolicy fields")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D4OperationStart:
    operation_id: str
    task_run_id: str
    category: D4OperationCategory
    started_at: float
    base_checkpoint_sequence: int
    context_sha256: str
    resource_ref: str
    budget_policy_ref: str
    role: Literal["outer_planning", "inner_action"] | None = None
    role_config_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation_id")
        _uuid(self.task_run_id, "task_run_id")
        if (
            self.category not in D4_CATEGORY_LIMITS
            and self.category not in D4_AUDIT_ONLY_CATEGORIES
            and self.category != "fact_review"
        ):
            raise ValueError("unsupported D4 operation category")
        _time(self.started_at, "started_at")
        if (
            type(self.base_checkpoint_sequence) is not int
            or self.base_checkpoint_sequence < 1
        ):
            raise ValueError("D4 operation requires a committed checkpoint")
        _sha(self.context_sha256, "context_sha256")
        _text(self.resource_ref, "resource_ref")
        if not isinstance(
            self.budget_policy_ref, str
        ) or not self.budget_policy_ref.startswith("sha256:"):
            raise ValueError("D4 operation requires a frozen budget reference")
        _sha(self.budget_policy_ref[7:], "budget_policy_ref")
        expected_role = (
            "outer_planning"
            if self.category in {"planning", "review", "refinement", "fact_review"}
            else "inner_action"
            if self.category == "action"
            else None
        )
        if self.role != expected_role:
            raise ValueError("D4 model role differs from operation category")
        if expected_role is not None:
            if not isinstance(
                self.role_config_ref, str
            ) or not self.role_config_ref.startswith("sha256:"):
                raise ValueError("D4 model call requires a frozen role reference")
            _sha(self.role_config_ref[7:], "role_config_ref")
        elif self.role_config_ref is not None:
            raise ValueError("non-model D4 operation cannot use model role")

    @property
    def charged(self) -> bool:
        return self.category in D4_CATEGORY_LIMITS or self.category == "fact_review"


@dataclass(frozen=True, slots=True)
class D4OperationCompletion:
    operation_id: str
    finished_at: float
    result_kind: D4OperationResult
    evidence_ref: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation_id")
        _time(self.finished_at, "finished_at")
        if self.result_kind not in {
            "completed",
            "rejected",
            "stale",
            "not_satisfied",
            "failed",
        }:
            raise ValueError("unsupported known D4 operation result")
        _text(self.evidence_ref, "evidence_ref")
        _sha(self.evidence_sha256, "evidence_sha256")


@dataclass(frozen=True, slots=True)
class D4OperationRecord:
    start: D4OperationStart
    completion: D4OperationCompletion | None = None
    application_ref: str | None = None


@dataclass(frozen=True, slots=True)
class D4BudgetUsage:
    task_run_id: str
    budget_policy_ref: str
    total_used: int
    category_counts: tuple[tuple[str, int], ...]
    audit_only_used: int


@dataclass(frozen=True, slots=True)
class D4AuditEvent:
    event_id: str
    task_run_id: str
    category: D4AuditCategory
    outcome: D4AuditOutcome
    occurred_at: float
    evidence_ref: str
    evidence_sha256: str
    related_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.event_id, "event_id")
        _uuid(self.task_run_id, "task_run_id")
        if self.category not in {
            "proposal_evidence_saved",
            "control_commit_started",
            "control_commit_committed",
            "control_commit_failed",
            "control_reload_confirmed",
            "cleanup_started",
            "cleanup_completed",
            "cleanup_failed",
            "resource_presence_verified",
            "cancellation_requested",
        }:
            raise ValueError("unsupported D4 audit category")
        if self.outcome not in {
            "known",
            "unknown",
            "failed",
            "present",
            "absent",
        }:
            raise ValueError("unsupported D4 audit outcome")
        if self.category == "cancellation_requested" and self.outcome != "known":
            raise ValueError("cancellation request audit must have a known outcome")
        _time(self.occurred_at, "occurred_at")
        _text(self.evidence_ref, "evidence_ref")
        _sha(self.evidence_sha256, "evidence_sha256")
        if self.related_ref is not None:
            _text(self.related_ref, "related_ref")


class D4RecoveryRequired(RuntimeError):
    """An admitted operation has no reliable completed result."""


class D4BudgetExhausted(RuntimeError):
    """The persistent TaskRun start budget admits no further operation."""


class D4OperationConflict(RuntimeError):
    """A fixed operation identity or frozen run contract does not match."""
