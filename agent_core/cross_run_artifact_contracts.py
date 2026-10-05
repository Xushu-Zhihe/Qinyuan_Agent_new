"""Contracts for exact cross-TaskRun artifact references and imports."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

D6_MAX_IMPORT_BYTES = 8 * 1024 * 1024
D6_MAX_IMPORTS_PER_RUN = 2


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
