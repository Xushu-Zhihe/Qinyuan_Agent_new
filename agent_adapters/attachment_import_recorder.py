"""Immutable, checksummed local evidence for trusted attachment imports."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Iterator
from uuid import UUID, uuid4

from agent_core.attachment_import import (
    AttachmentImportClaim,
    AttachmentImportConflict,
    AttachmentImportRecoveryRequired,
    AttachmentImportReceipt,
    AttachmentImportStorageError,
)


def _encode(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


class FileAttachmentImportRecorder:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread_lock = RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # The OS file lock serializes reservations across recorder instances.
        with self._thread_lock:
            try:
                with (self.directory / ".claims.lock").open("a+b") as stream:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        fcntl = importlib.import_module("fcntl")
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        stream.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                raise AttachmentImportStorageError(
                    "import reservation lock failed"
                ) from None

    def _path(self, kind: str, import_id: str) -> Path:
        try:
            parsed = UUID(import_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise AttachmentImportStorageError("invalid import evidence ID") from exc
        if parsed.version != 4 or str(parsed) != import_id:
            raise AttachmentImportStorageError("invalid import evidence ID")
        return self.directory / f"{kind}-{import_id}.json"

    def _read(self, kind: str, import_id: str) -> dict[str, object] | None:
        path = self._path(kind, import_id)
        try:
            with path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("oversized import evidence")
            envelope = json.loads(raw)
            value = envelope["payload"]
            if (
                not isinstance(value, dict)
                or envelope["sha256"] != hashlib.sha256(_encode(value)).hexdigest()
            ):
                raise ValueError("invalid import evidence checksum")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            raise AttachmentImportStorageError(
                "import evidence read or integrity check failed"
            ) from None

    def _write(self, kind: str, import_id: str, value: object) -> None:
        path = self._path(kind, import_id)
        payload = _encode(value)
        envelope = _encode(
            {"sha256": hashlib.sha256(payload).hexdigest(), "payload": value}
        )
        temporary = self.directory / f".pending-{uuid4()}"
        try:
            with temporary.open("xb") as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if self._read(kind, import_id) != value:
                raise AttachmentImportConflict(
                    "immutable import evidence conflict"
                ) from None
        except OSError:
            raise AttachmentImportStorageError("import evidence write failed") from None
        finally:
            temporary.unlink(missing_ok=True)

    def read_claim(self, import_id: str) -> AttachmentImportClaim | None:
        value = self._read("claim", import_id)
        if value is None:
            return None
        try:
            return AttachmentImportClaim(**value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise AttachmentImportStorageError("invalid import claim") from None

    def read_receipt(self, import_id: str) -> AttachmentImportReceipt | None:
        value = self._read("success", import_id)
        if value is None:
            return None
        try:
            receipt = AttachmentImportReceipt(**value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise AttachmentImportStorageError("invalid import receipt") from None
        claim = self.read_claim(import_id)
        if (
            claim is None
            or claim.identity() != receipt.identity()
            or claim.claimed_at != receipt.claimed_at
        ):
            raise AttachmentImportStorageError("import receipt has no matching claim")
        return receipt

    def begin(self, claim: AttachmentImportClaim) -> AttachmentImportReceipt | None:
        if not isinstance(claim, AttachmentImportClaim):
            raise TypeError("claim must be an AttachmentImportClaim")
        with self._locked():
            previous = self.read_claim(claim.import_id)
            if previous is not None:
                if previous.identity() != claim.identity():
                    raise AttachmentImportConflict(
                        "import ID has different request content"
                    )
                completed = self.read_receipt(claim.import_id)
                if completed is None:
                    raise AttachmentImportRecoveryRequired(
                        "import claim exists without a confirmed result"
                    )
                return completed

            for path in self.directory.glob("claim-*.json"):
                other_id = path.name[len("claim-") : -len(".json")]
                other = self.read_claim(other_id)
                if other is None:
                    raise AttachmentImportStorageError("import claim disappeared")
                if (
                    other.task_run_id == claim.task_run_id
                    and other.attachment_index == claim.attachment_index
                ) or (
                    other.sandbox_id == claim.sandbox_id
                    and other.sandbox_path == claim.sandbox_path
                ):
                    raise AttachmentImportConflict(
                        "attachment or Sandbox path already claimed"
                    )

            self._write("claim", claim.import_id, asdict(claim))
            return None

    def save_receipt(self, receipt: AttachmentImportReceipt) -> None:
        if not isinstance(receipt, AttachmentImportReceipt):
            raise TypeError("receipt must be an AttachmentImportReceipt")
        claim = self.read_claim(receipt.import_id)
        if (
            claim is None
            or claim.identity() != receipt.identity()
            or claim.claimed_at != receipt.claimed_at
        ):
            raise AttachmentImportStorageError("import receipt has no matching claim")
        self._write("success", receipt.import_id, asdict(receipt))
