"""Frozen operation admission, budget, and audit contracts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
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
    "handoff_summary",
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
    "handoff_summary",
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
    "planning": 24,
    "action": 48,
    "review": 32,
    "refinement": 16,
    "tool_dispatch": 24,
    "sha_observation": 8,
    "artifact_share": 8,
    "attachment_import": 4,
}
D4_AUDIT_ONLY_CATEGORIES = frozenset({"copy_preparation", "intent_stop"})
# Only thinking/observation categories are budgeted; `total` is always derived so
# adjusting a category limit can never desynchronise the matrix.
D8_CATEGORY_LIMITS: dict[str, int] = {**D4_CATEGORY_LIMITS, "fact_review": 32}
D8_HANDOFF_CATEGORY_LIMITS: dict[str, int] = {**D8_CATEGORY_LIMITS, "handoff_summary": 16}
D4_TOTAL: int = sum(D4_CATEGORY_LIMITS.values())
D8_TOTAL: int = sum(D8_CATEGORY_LIMITS.values())
D8_HANDOFF_TOTAL: int = sum(D8_HANDOFF_CATEGORY_LIMITS.values())


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
    total: int = field(default=D4_TOTAL)
    planning: int = field(default=D4_CATEGORY_LIMITS["planning"])
    action: int = field(default=D4_CATEGORY_LIMITS["action"])
    review: int = field(default=D4_CATEGORY_LIMITS["review"])
    refinement: int = field(default=D4_CATEGORY_LIMITS["refinement"])
    tool_dispatch: int = field(default=D4_CATEGORY_LIMITS["tool_dispatch"])
    sha_observation: int = field(default=D4_CATEGORY_LIMITS["sha_observation"])
    artifact_share: int = field(default=D4_CATEGORY_LIMITS["artifact_share"])
    attachment_import: int = field(default=D4_CATEGORY_LIMITS["attachment_import"])

    def __post_init__(self) -> None:
        expected = {"total": D4_TOTAL, **D4_CATEGORY_LIMITS}
        if any(
            type(getattr(self, name)) is not int or getattr(self, name) != limit
            for name, limit in expected.items()
        ):
            raise ValueError("D4 supports only the confirmed budget matrix")

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

    total: int = field(default=D8_TOTAL)
    fact_review: int = field(default=D8_CATEGORY_LIMITS["fact_review"])

    def __post_init__(self) -> None:
        expected = {"total": D8_TOTAL, **D8_CATEGORY_LIMITS}
        if any(type(getattr(self, key)) is not int or getattr(self, key) != limit
               for key, limit in expected.items()):
            raise ValueError("D8 supports only the confirmed budget matrix")

    @property
    def category_limits(self) -> dict[str, int]:
        return dict(D8_CATEGORY_LIMITS)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D8BudgetPolicy:
        if set(value) != {"total", *D4_CATEGORY_LIMITS, "fact_review"}:
            raise ValueError("invalid D8BudgetPolicy fields")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D8HandoffBudgetPolicy(D8BudgetPolicy):
    """Explicit D8-2 matrix; capacity changes never upgrade an older budget."""

    total: int = field(default=D8_HANDOFF_TOTAL)
    handoff_summary: int = field(default=D8_HANDOFF_CATEGORY_LIMITS["handoff_summary"])

    def __post_init__(self) -> None:
        expected = {"total": D8_HANDOFF_TOTAL, **D8_HANDOFF_CATEGORY_LIMITS}
        if any(type(getattr(self, key)) is not int or getattr(self, key) != limit
               for key, limit in expected.items()):
            raise ValueError("D8-2 supports only the confirmed budget matrix")

    @property
    def category_limits(self) -> dict[str, int]:
        return dict(D8_HANDOFF_CATEGORY_LIMITS)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D8HandoffBudgetPolicy:
        if set(value) != {"total", *D4_CATEGORY_LIMITS, "fact_review", "handoff_summary"}:
            raise ValueError("invalid D8HandoffBudgetPolicy fields")
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
            and self.category not in {"fact_review", "handoff_summary"}
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
            if self.category in {"planning", "review", "refinement", "fact_review", "handoff_summary"}
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
        return self.category in D4_CATEGORY_LIMITS or self.category in {"fact_review", "handoff_summary"}


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
