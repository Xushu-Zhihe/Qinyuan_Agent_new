"""Immutable evidence identity for a verified file-type observation."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from uuid import UUID

from agent_core.file_type_fact import FileTypeFactCandidate


def _uuid4(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID4")


@dataclass(frozen=True, slots=True)
class FileTypeVerificationProof:
    proof_id: str
    task_run_id: str
    intent_id: str
    execution_id: str
    import_id: str
    path: str
    local_path: str
    observed_type: str
    verified_at: float

    def __post_init__(self) -> None:
        _uuid4(self.proof_id, "proof_id")
        _uuid4(self.import_id, "import_id")
        if not isinstance(self.local_path, str) or not self.local_path.strip():
            raise ValueError("local_path must be non-empty")
        if not isinstance(self.verified_at, (int, float)) or not isfinite(
            self.verified_at
        ):
            raise ValueError("verified_at must be finite")
        FileTypeFactCandidate(self.path, self.observed_type, self.execution_id)
