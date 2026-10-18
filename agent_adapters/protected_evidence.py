"""D6 protected evidence storage.

The store owns one TaskRun's sensitive evidence directory.  Plaintext is only
ever held in the caller's memory and in the encrypted DPAPI envelope; ordinary
metadata queries return a withheld body marker and a ciphertext digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import UUID

from agent_adapters.run_files import run_lock
from agent_adapters.windows_evidence_protection import (
    WindowsEvidenceProtection,
    WindowsEvidenceProtectionError,
)
from agent_core.sensitive_evidence_contracts import (
    D6_SENSITIVE_KINDS,
    D6SensitiveEvidenceMetadata,
)
from qinyuan_core import CodedError


class ProtectedEvidenceError(CodedError):
    """A fixed, non-sensitive protected-evidence failure."""


class ProtectedEvidenceNotFound(ProtectedEvidenceError):
    def __init__(self) -> None:
        super().__init__("d6_evidence_not_found")


class ProtectedEvidenceDeleted(ProtectedEvidenceError):
    def __init__(self) -> None:
        super().__init__("d6_evidence_deleted")


class ProtectedEvidenceConflict(ProtectedEvidenceError):
    def __init__(self) -> None:
        super().__init__("d6_evidence_conflict")


class ProtectedEvidenceAudit(Protocol):
    def record(
        self,
        *,
        identity: str,
        task_run_id: str,
        evidence_id: str,
        operation: str,
        result: str,
        occurred_at: float,
    ) -> None: ...


class FileProtectedEvidenceAudit:
    """Append-only no-body audit records for local acceptance tests."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        identity: str,
        task_run_id: str,
        evidence_id: str,
        operation: str,
        result: str,
        occurred_at: float,
    ) -> None:
        if not identity.startswith("S-1-"):
            raise ProtectedEvidenceError("d6_audit_identity_invalid")
        if operation not in {"view", "delete"} or result not in {"authorized", "success", "deleted", "failed"}:
            raise ProtectedEvidenceError("d6_audit_record_invalid")
        entry = {
            "identity": identity,
            "task_run_id": task_run_id,
            "evidence_id": evidence_id,
            "operation": operation,
            "result": result,
            "occurred_at": occurred_at,
        }
        payload = json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        try:
            with self.path.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise ProtectedEvidenceError("d6_audit_write_failed") from exc


def _uuid4(value: object, field: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProtectedEvidenceError(f"{field}_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ProtectedEvidenceError(f"{field}_invalid")


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@contextmanager
def _locked(lock: RLock):
    with lock:
        yield


class ProtectedEvidenceStore:
    """Store one TaskRun's sensitive evidence under current-user protection."""

    def __init__(
        self,
        run_dir: Path,
        task_run_id: str,
        protection: WindowsEvidenceProtection,
        clock,
        audit: ProtectedEvidenceAudit,
        *,
        create: bool = True,
    ) -> None:
        _uuid4(task_run_id, "task_run_id")
        if not isinstance(protection, WindowsEvidenceProtection):
            raise ProtectedEvidenceError("d6_protection_invalid")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise ProtectedEvidenceError("d6_audit_invalid")
        self.run_dir = Path(run_dir)
        self.task_run_id = task_run_id
        self.protection = protection
        self.clock = clock
        self.audit = audit
        self._lock = RLock()
        self.evidence_dir = self.run_dir / "sensitive-evidence"
        try:
            if create:
                self.run_dir.mkdir(parents=True, exist_ok=True)
            if self.run_dir.is_symlink() or not self.run_dir.is_dir():
                raise ProtectedEvidenceError("d6_run_directory_invalid")
            new_directory = not self.evidence_dir.exists()
            if new_directory and create:
                self.evidence_dir.mkdir()
            if self.evidence_dir.is_symlink() or not self.evidence_dir.is_dir():
                raise ProtectedEvidenceError("d6_evidence_directory_invalid")
            # The security adapter rejects inherited/broadened ACLs and reparse
            # points. Existing directories are verified before any file access.
            if new_directory and create:
                self.protection.restrict_directory(self.evidence_dir)
            self.protection.verify_directory(self.evidence_dir)
            owner_path = self.evidence_dir / "owner.json"
            expected_owner = {"task_run_id": task_run_id, "identity": protection.identity}
            if new_directory and create:
                self._write_exclusive(owner_path, _encode(expected_owner))
            if owner_path.is_symlink() or json.loads(owner_path.read_bytes()) != expected_owner:
                raise ProtectedEvidenceError("d6_evidence_ownership_invalid")
        except WindowsEvidenceProtectionError as exc:
            raise ProtectedEvidenceError("d6_protected_directory_failed") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise ProtectedEvidenceError("d6_protected_directory_failed") from exc

    def _verify(self) -> None:
        try:
            self.protection.verify_directory(self.evidence_dir)
        except WindowsEvidenceProtectionError as exc:
            raise ProtectedEvidenceError("d6_protected_directory_failed") from exc

    @contextmanager
    def _mutation_lock(self):
        # This lock is separate from the running Agent's operation lock: explicit
        # evidence deletion is allowed while a TaskRun is executing.
        with self._lock:
            self._verify()
            try:
                with run_lock(self.evidence_dir):
                    yield
            except ValueError as exc:
                raise ProtectedEvidenceError("d6_evidence_busy") from exc

    def _paths(self, evidence_id: str) -> tuple[Path, Path]:
        _uuid4(evidence_id, "evidence_id")
        return (
            self.evidence_dir / f"{evidence_id}.meta.json",
            self.evidence_dir / f"{evidence_id}.cipher",
        )

    def _entropy(self, evidence_id: str, kind: str) -> bytes:
        return hashlib.sha256(
            f"qinyuan-d6:{self.task_run_id}:{evidence_id}:{kind}".encode()
        ).digest()

    @staticmethod
    def _write_exclusive(path: Path, payload: bytes) -> None:
        if path.exists() or path.is_symlink():
            raise ProtectedEvidenceConflict()
        fd, temporary_name = tempfile.mkstemp(prefix=".d6-pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError as exc:
                raise ProtectedEvidenceConflict() from exc
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _read_metadata(path: Path) -> D6SensitiveEvidenceMetadata:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return D6SensitiveEvidenceMetadata(**value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ProtectedEvidenceError("d6_evidence_metadata_corrupt") from exc

    def _metadata(self, evidence_id: str) -> D6SensitiveEvidenceMetadata:
        meta_path, _ = self._paths(evidence_id)
        if not meta_path.exists() or meta_path.is_symlink() or not meta_path.is_file():
            raise ProtectedEvidenceNotFound()
        metadata = self._read_metadata(meta_path)
        if metadata.task_run_id != self.task_run_id or metadata.evidence_id != evidence_id:
            raise ProtectedEvidenceError("d6_evidence_ownership_invalid")
        return metadata

    def save(self, evidence_id: str, kind: str, body: bytes) -> D6SensitiveEvidenceMetadata:
        if kind not in D6_SENSITIVE_KINDS:
            raise ProtectedEvidenceError("d6_sensitive_kind_invalid")
        if not isinstance(body, bytes):
            raise ProtectedEvidenceError("d6_evidence_body_invalid")
        with self._mutation_lock():
            metadata_path, cipher_path = self._paths(evidence_id)
            created_at = float(self.clock())
            envelope = {"body_sha256": hashlib.sha256(body).hexdigest(), "body": body.hex()}
            try:
                ciphertext = self.protection.protect(_encode(envelope), self._entropy(evidence_id, kind))
            except WindowsEvidenceProtectionError as exc:
                raise ProtectedEvidenceError("d6_evidence_protect_failed") from exc
            cipher_sha = hashlib.sha256(ciphertext).hexdigest()
            metadata = D6SensitiveEvidenceMetadata(evidence_id, self.task_run_id, kind, cipher_sha, len(body), created_at, "available")
            self._write_exclusive(cipher_path, ciphertext)
            try:
                self._write_exclusive(metadata_path, _encode(asdict(metadata)))
            except Exception:
                cipher_path.unlink(missing_ok=True)
                raise
            return metadata

    def query(self, evidence_id: str) -> dict[str, object]:
        with _locked(self._lock):
            self._verify()
            metadata = self._metadata(evidence_id)
            return {
                **asdict(metadata),
                "body": None,
                "body_status": "withheld" if metadata.status == "available" else "deleted",
            }

    def list_metadata(self) -> tuple[dict[str, object], ...]:
        with _locked(self._lock):
            self._verify()
            entries = []
            for path in sorted(self.evidence_dir.glob("*.meta.json")):
                if path.is_symlink():
                    raise ProtectedEvidenceError("d6_evidence_metadata_invalid")
                metadata = self._read_metadata(path)
                if metadata.task_run_id != self.task_run_id or path.name != f"{metadata.evidence_id}.meta.json":
                    raise ProtectedEvidenceError("d6_evidence_ownership_invalid")
                entries.append({**asdict(metadata), "body": None, "body_status": "withheld" if metadata.status == "available" else "deleted"})
            return tuple(entries)

    def view(
        self,
        evidence_id: str,
        *,
        validate_body: Callable[[bytes], None] | None = None,
    ) -> bytes:
        with self._mutation_lock():
            metadata = self._metadata(evidence_id)
            if metadata.status == "deleted":
                self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="view", result="deleted", occurred_at=float(self.clock()))
                raise ProtectedEvidenceDeleted()
            # The audit is committed before the DPAPI call; a failed audit never
            # exposes plaintext to this process.
            self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="view", result="authorized", occurred_at=float(self.clock()))
            _, cipher_path = self._paths(evidence_id)
            if cipher_path.is_symlink() or not cipher_path.is_file():
                raise ProtectedEvidenceError("d6_evidence_cipher_missing")
            try:
                ciphertext = cipher_path.read_bytes()
                if hashlib.sha256(ciphertext).hexdigest() != metadata.cipher_sha256:
                    raise ProtectedEvidenceError("d6_evidence_cipher_integrity_failed")
                plaintext = self.protection.unprotect(ciphertext, self._entropy(evidence_id, metadata.kind))
                envelope = json.loads(plaintext.decode("utf-8"))
                body = bytes.fromhex(envelope["body"])
                if hashlib.sha256(body).hexdigest() != envelope["body_sha256"] or len(body) != metadata.byte_size:
                    raise ProtectedEvidenceError("d6_evidence_body_integrity_failed")
                if validate_body is not None:
                    validate_body(body)
                self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="view", result="success", occurred_at=float(self.clock()))
                return body
            except ProtectedEvidenceError:
                self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="view", result="failed", occurred_at=float(self.clock()))
                raise
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, WindowsEvidenceProtectionError) as exc:
                self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="view", result="failed", occurred_at=float(self.clock()))
                raise ProtectedEvidenceError("d6_evidence_decrypt_failed") from exc

    def delete(self, evidence_id: str) -> D6SensitiveEvidenceMetadata:
        with self._mutation_lock():
            metadata = self._metadata(evidence_id)
            self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="delete", result="authorized", occurred_at=float(self.clock()))
            metadata_path, cipher_path = self._paths(evidence_id)
            tombstone = metadata if metadata.status == "deleted" else D6SensitiveEvidenceMetadata(metadata.evidence_id, metadata.task_run_id, metadata.kind, metadata.cipher_sha256, metadata.byte_size, metadata.created_at, "deleted", float(self.clock()))
            temporary = metadata_path.with_suffix(".pending")
            try:
                if metadata.status != "deleted":
                    with temporary.open("xb") as stream:
                        stream.write(_encode(asdict(tombstone)))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, metadata_path)
                if cipher_path.is_symlink():
                    raise ProtectedEvidenceError("d6_evidence_cipher_invalid")
                cipher_path.unlink(missing_ok=True)
            except ProtectedEvidenceError:
                raise
            except OSError as exc:
                raise ProtectedEvidenceError("d6_evidence_delete_failed") from exc
            finally:
                temporary.unlink(missing_ok=True)
            self.audit.record(identity=self.protection.identity, task_run_id=self.task_run_id, evidence_id=evidence_id, operation="delete", result="success", occurred_at=float(self.clock()))
            return tombstone

