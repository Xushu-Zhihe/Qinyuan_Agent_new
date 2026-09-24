"""Evidence contracts for one trusted local attachment import."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_core.contracts import Attachment
from agent_core.file_inspection import validate_inspection_path
from agent_core.modeling import CancellationToken


MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024


class AttachmentImportConflict(RuntimeError):
    """The import identity, attachment, or destination was already claimed."""


class AttachmentImportRecoveryRequired(RuntimeError):
    """An import request exists without a confirmed successful result."""


class AttachmentImportStorageError(RuntimeError):
    """Import evidence could not be written or verified."""


class AttachmentImportRejected(RuntimeError):
    """A pre-dispatch import check rejected the request."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _uuid4(value: str, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical UUID4")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID4")


@dataclass(frozen=True, slots=True)
class AttachmentImportRequest:
    import_id: str
    task_run_id: str
    sandbox_id: str
    attachment_index: int
    source_root: str
    sandbox_path: str

    def __post_init__(self) -> None:
        _uuid4(self.import_id, "import_id")
        _uuid4(self.task_run_id, "task_run_id")
        if not isinstance(self.sandbox_id, str) or not self.sandbox_id:
            raise ValueError("sandbox_id must be non-empty")
        if type(self.attachment_index) is not int or self.attachment_index < 0:
            raise ValueError("attachment_index must be non-negative")
        if not isinstance(self.source_root, str) or not self.source_root:
            raise ValueError("source_root must be non-empty")
        validate_inspection_path(self.sandbox_path)


@dataclass(frozen=True, slots=True)
class AttachmentImportReadback:
    byte_size: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.byte_size) is not int
            or not 0 <= self.byte_size <= MAX_ATTACHMENT_BYTES
        ):
            raise ValueError("invalid import readback size")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("invalid import readback SHA-256")


class AttachmentImportSandboxPort(Protocol):
    async def create_and_readback(
        self,
        sandbox_id: str,
        path: str,
        content: bytes,
        timeout_seconds: float,
        cancellation_token: CancellationToken,
    ) -> AttachmentImportReadback: ...

    async def readback(
        self,
        sandbox_id: str,
        path: str,
        timeout_seconds: float,
        cancellation_token: CancellationToken,
    ) -> AttachmentImportReadback: ...


@dataclass(frozen=True, slots=True)
class AttachmentImportClaim:
    import_id: str
    task_run_id: str
    sandbox_id: str
    attachment_index: int
    local_path: str
    source_root: str
    sandbox_path: str
    byte_size: int
    sha256: str
    claimed_at: float

    def __post_init__(self) -> None:
        _uuid4(self.import_id, "import_id")
        _uuid4(self.task_run_id, "task_run_id")
        if not isinstance(self.sandbox_id, str) or not self.sandbox_id:
            raise ValueError("sandbox_id must be non-empty")
        if type(self.attachment_index) is not int or self.attachment_index < 0:
            raise ValueError("attachment_index must be non-negative")
        Attachment.from_dict({"kind": "local_path", "path": self.local_path})
        if not isinstance(self.source_root, str) or not self.source_root:
            raise ValueError("source_root must be non-empty")
        validate_inspection_path(self.sandbox_path)
        if (
            type(self.byte_size) is not int
            or not 0 <= self.byte_size <= MAX_ATTACHMENT_BYTES
        ):
            raise ValueError("byte_size exceeds the 16 MiB import limit")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("sha256 must be lowercase SHA-256 hex")
        if not isinstance(self.claimed_at, (int, float)) or not math.isfinite(
            self.claimed_at
        ):
            raise ValueError("claimed_at must be a finite timestamp")

    def identity(self) -> tuple[object, ...]:
        return (
            self.import_id,
            self.task_run_id,
            self.sandbox_id,
            self.attachment_index,
            self.local_path,
            self.source_root,
            self.sandbox_path,
            self.byte_size,
            self.sha256,
        )


@dataclass(frozen=True, slots=True)
class AttachmentImportReceipt(AttachmentImportClaim):
    completed_at: float

    def __post_init__(self) -> None:
        super(AttachmentImportReceipt, self).__post_init__()
        if not isinstance(self.completed_at, (int, float)) or not math.isfinite(
            self.completed_at
        ):
            raise ValueError("completed_at must be a finite timestamp")
        if self.completed_at < self.claimed_at:
            raise ValueError("completed_at must not predate the import claim")


class AttachmentImportEvidencePort(Protocol):
    def read_claim(self, import_id: str) -> AttachmentImportClaim | None: ...

    def read_receipt(self, import_id: str) -> AttachmentImportReceipt | None: ...
