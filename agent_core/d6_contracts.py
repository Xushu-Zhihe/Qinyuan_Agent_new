"""Small, immutable contracts for the D6 protected-evidence slice.

The contracts intentionally describe only the first D6 vertical slice: protected
sensitive evidence and explicit, non-sensitive local-artifact import.  They do
not grant network access or authentication capability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

D6_MAX_IMPORT_BYTES = 8 * 1024 * 1024
D6_MAX_IMPORTS_PER_RUN = 2
D6Impact = Literal["low_impact", "state_change", "destructive"]
D6_SENSITIVE_KINDS = frozenset({
    "credential", "password", "token", "api_key", "private_key", "auth_header",
})


def _uuid(value: object, field: str, *, versions: tuple[int, ...] = (4,)) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.version not in versions or str(parsed) != value:
        raise ValueError(f"{field}_invalid")


def _sha(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field}_invalid")


def _time(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field}_invalid")


@dataclass(frozen=True, slots=True)
class D6ActionDeclaration:
    """Trusted adapter declaration used by the D6 policy boundary."""

    action_id: str
    impact: D6Impact
    uses_credentials: bool
    authentication: bool

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id_invalid")
        if self.impact not in {"low_impact", "state_change", "destructive"}:
            raise ValueError("impact_invalid")
        if type(self.uses_credentials) is not bool or type(self.authentication) is not bool:
            raise ValueError("action_classification_invalid")


@dataclass(frozen=True, slots=True)
class D6ArtifactReference:
    """An exact, non-sensitive local artifact identity."""

    source_task_run_id: str
    object_version_id: str
    sha256: str

    def __post_init__(self) -> None:
        _uuid(self.source_task_run_id, "source_task_run_id")
        _uuid(self.object_version_id, "object_version_id", versions=(4, 5))
        _sha(self.sha256, "sha256")


@dataclass(frozen=True, slots=True)
class D6ArtifactImportRequest:
    """Explicit source/target identity for one cross-TaskRun import."""

    import_id: str
    source: D6ArtifactReference
    target_task_run_id: str
    non_sensitive: bool

    def __post_init__(self) -> None:
        _uuid(self.import_id, "import_id")
        if not isinstance(self.source, D6ArtifactReference):
            raise TypeError("source_invalid")
        _uuid(self.target_task_run_id, "target_task_run_id")
        if self.source.source_task_run_id == self.target_task_run_id:
            raise ValueError("cross_task_run_required")
        if type(self.non_sensitive) is not bool or not self.non_sensitive:
            raise ValueError("d6_sensitive_import_forbidden")


@dataclass(frozen=True, slots=True)
class D6SensitiveEvidenceMetadata:
    """Metadata safe for ordinary queries; the body is never represented here."""

    evidence_id: str
    task_run_id: str
    kind: str
    cipher_sha256: str
    byte_size: int
    created_at: float
    status: Literal["available", "deleted"]
    deleted_at: float | None = None

    def __post_init__(self) -> None:
        _uuid(self.evidence_id, "evidence_id")
        _uuid(self.task_run_id, "task_run_id")
        if self.kind not in D6_SENSITIVE_KINDS:
            raise ValueError("kind_invalid")
        _sha(self.cipher_sha256, "cipher_sha256")
        if type(self.byte_size) is not int or self.byte_size < 0:
            raise ValueError("byte_size_invalid")
        _time(self.created_at, "created_at")
        if self.status not in {"available", "deleted"}:
            raise ValueError("status_invalid")
        if self.deleted_at is not None:
            _time(self.deleted_at, "deleted_at")
        if self.status == "available" and self.deleted_at is not None:
            raise ValueError("available_evidence_deleted_at")
        if self.status == "deleted" and self.deleted_at is None:
            raise ValueError("deleted_evidence_missing_time")

