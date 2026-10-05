"""Safe metadata contracts for protected sensitive evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

D6_SENSITIVE_KINDS = frozenset({
    "credential", "password", "token", "api_key", "private_key", "auth_header",
})


def _uuid(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
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
