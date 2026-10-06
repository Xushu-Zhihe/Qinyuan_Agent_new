"""Protected storage for exact D7 answer-candidate text."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import UUID

from agent_adapters.run_files import parse_json, run_lock
from agent_adapters.windows_evidence_protection import (
    WindowsEvidenceProtection,
    WindowsEvidenceProtectionError,
)
from agent_core.control_candidate import validate_control_ownership
from agent_core.domain import ExecutionStatus, IntentStatus, PlanStatus, TurnStatus
from agent_core.goal_candidates import D7AnswerCandidate
from agent_core.sqlite_control_store import SqliteControlStore

MAX_D7_CANDIDATE_BYTES = 16 * 1024


class D7CandidateEvidenceError(RuntimeError):
    """A fixed, body-free error code for D7 protected candidate evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class D7CandidateEvidenceNotFound(D7CandidateEvidenceError):
    def __init__(self) -> None:
        super().__init__("d7_candidate_evidence_not_found")


class D7CandidateEvidenceConflict(D7CandidateEvidenceError):
    def __init__(self) -> None:
        super().__init__("d7_candidate_evidence_conflict")


class _EvidenceProtection(Protocol):
    identity: str

    def protect(self, value: bytes, entropy: bytes) -> bytes: ...

    def unprotect(self, value: bytes, entropy: bytes) -> bytes: ...

    def restrict_directory(self, directory: Path) -> None: ...

    def verify_directory(self, directory: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class D7CandidateEvidenceMetadata:
    schema_version: int
    task_run_id: str
    candidate_id: str
    evidence_version: str
    execution_id: str
    candidate_binding: dict[str, object]
    body_sha256: str
    cipher_sha256: str
    byte_size: int
    created_at: float
    model_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class D7CandidateEvidenceReadPort:
    """A candidate-body reader bound to one trusted adapter at assembly time."""

    _service: D7CandidateEvidenceService
    _adapter_id: str

    def read(self, candidate_id: str) -> str:
        return self._service._read_for_port(candidate_id, self)


class FileD7CandidateEvidenceAudit:
    """Durable append-only audit with no candidate body or source value."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        identity: str,
        adapter_id: str,
        candidate: D7AnswerCandidate,
        operation: str,
        result: str,
        occurred_at: float,
    ) -> None:
        if not identity.startswith("S-1-"):
            raise D7CandidateEvidenceError("d7_candidate_audit_identity_invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", adapter_id):
            raise D7CandidateEvidenceError("d7_candidate_audit_adapter_invalid")
        if operation != "read" or result not in {"authorized", "success", "failed"}:
            raise D7CandidateEvidenceError("d7_candidate_audit_record_invalid")
        entry = {
            "schema_version": 1,
            "identity": identity,
            "adapter_id": adapter_id,
            "task_run_id": candidate.task_run_id,
            "candidate_id": candidate.candidate_id,
            "evidence_version": candidate.evidence_version,
            "operation": operation,
            "result": result,
            "occurred_at": occurred_at,
        }
        payload = (
            json.dumps(
                entry, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with self.path.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise D7CandidateEvidenceError("d7_candidate_audit_write_failed") from exc


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_candidate_id(value: object) -> str:
    if not isinstance(value, str):
        raise D7CandidateEvidenceError("d7_candidate_id_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise D7CandidateEvidenceError("d7_candidate_id_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise D7CandidateEvidenceError("d7_candidate_id_invalid")
    return value


def _is_uuid4(value: object) -> bool:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _is_uuid4_or5(value: object) -> bool:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError):
        return False
    return parsed.version in {4, 5} and str(parsed) == value


def _encode_candidate_text(value: object) -> bytes:
    if not isinstance(value, str):
        raise D7CandidateEvidenceError("d7_candidate_body_not_text")
    try:
        body = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise D7CandidateEvidenceError("d7_candidate_body_not_utf8") from exc
    if not body or b"\x00" in body:
        raise D7CandidateEvidenceError("d7_candidate_body_invalid")
    if len(body) > MAX_D7_CANDIDATE_BYTES:
        raise D7CandidateEvidenceError("d7_candidate_body_too_large")
    return body


def _validate_decoded_candidate_body(body: bytes) -> str:
    if not body or b"\x00" in body or len(body) > MAX_D7_CANDIDATE_BYTES:
        raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed")
    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed") from exc


def _candidate_source_projection(candidate: D7AnswerCandidate) -> dict[str, object]:
    if candidate.source_kind == "d3":
        return {}
    binding = candidate.web_execution_binding
    if binding is None:
        raise D7CandidateEvidenceError("d7_candidate_binding_mismatch")
    return {
        "source_kind": "web",
        "http_operation_id": binding.http_operation_id,
        "response_evidence_id": binding.response_evidence_id,
        "response_sha256": binding.response_sha256,
        "response_bytes": binding.response_bytes,
    }


def _validate_owned_run(run_dir: Path, task_run_id: str) -> None:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise D7CandidateEvidenceError("d7_candidate_run_directory_invalid")
    database = run_dir / "control.sqlite"
    if database.is_symlink() or not database.is_file():
        raise D7CandidateEvidenceError("d7_candidate_ownership_invalid")
    try:
        with SqliteControlStore.open_readonly(database) as ledger:
            if (
                ledger.list_task_run_refs() != (task_run_id,)
                or ledger.load_task_run(task_run_id) is None
            ):
                raise D7CandidateEvidenceError("d7_candidate_ownership_invalid")
    except D7CandidateEvidenceError:
        raise
    except Exception as exc:
        raise D7CandidateEvidenceError("d7_candidate_ownership_invalid") from exc


def _persisted_candidate(
    run_dir: Path,
    task_run_id: str,
    candidate_id: str,
    *,
    candidate_hint: D7AnswerCandidate | None = None,
    require_registered: bool = True,
) -> D7AnswerCandidate:
    _validate_owned_run(run_dir, task_run_id)
    try:
        with SqliteControlStore.open_readonly(run_dir / "control.sqlite") as ledger:
            registered = ledger.load_d7_answer_candidate(candidate_id)
            candidate = registered or candidate_hint
            state = ledger.load_control_state_rows(task_run_id)
            if (
                candidate is None
                or candidate.task_run_id != task_run_id
                or state is None
            ):
                raise ValueError("candidate_missing")
            if require_registered and registered is None:
                raise ValueError("candidate_missing")
            if candidate.candidate_id != candidate_id:
                raise ValueError("candidate_identity_mismatch")
            validate_control_ownership(state)
            if registered is not None and not any(
                item.candidate_id == candidate_id
                for item in ledger.list_d7_answer_candidates(task_run_id)
            ):
                raise ValueError("candidate_not_owned")
            turns = {item.turn_id: item for item in state.turns}
            intents = {item.intent_id: item for item in state.intents}
            turn = turns.get(candidate.turn_id)
            intent = intents.get(candidate.intent_id)
            if (
                turn is None
                or turn.task_run_id != task_run_id
                or turn.status is not TurnStatus.COMPLETED
                or intent is None
                or intent.turn_id != candidate.turn_id
                or intent.status is not IntentStatus.SUCCEEDED
            ):
                raise ValueError("candidate_source_invalid")
            if candidate.source_kind == "d3":
                plans = {item.plan_id: item for item in state.plans}
                executions = {item.execution_id: item for item in state.executions}
                plan = plans.get(candidate.plan_id)
                execution = executions.get(candidate.execution_id)
                binding = ledger.load_d3_plan_binding(candidate.plan_id)
                result = ledger.load_d3_plan_result(candidate.plan_id)
                if (
                    plan is None
                    or plan.intent_id != candidate.intent_id
                    or plan.status is not PlanStatus.COMPLETED
                    or execution is None
                    or execution.plan_id != candidate.plan_id
                    or execution.status is not ExecutionStatus.SUCCEEDED
                    or execution.output_ref is None
                    or binding is None
                    or binding.task_run_id != task_run_id
                    or binding.intent_id != candidate.intent_id
                    or binding.artifact_version_id != candidate.source_ref
                    or result is None
                    or result.task_run_id != task_run_id
                    or result.outcome != "satisfied"
                    or result.evidence_sha256 != candidate.evidence_version
                ):
                    raise ValueError("candidate_source_invalid")
            elif candidate.source_kind == "web":
                web_binding = candidate.web_execution_binding
                persisted_binding = ledger.load_d7_web_execution_binding(
                    candidate.execution_id
                )
                if (
                    web_binding is None
                    or persisted_binding is None
                    or persisted_binding != web_binding
                    or persisted_binding.task_run_id != task_run_id
                    or persisted_binding.turn_id != candidate.turn_id
                    or persisted_binding.intent_id != candidate.intent_id
                    or persisted_binding.plan_id != candidate.plan_id
                    or persisted_binding.execution_id != candidate.execution_id
                    or persisted_binding.response_evidence_id != candidate.source_ref
                    or persisted_binding.response_sha256 != candidate.evidence_version
                ):
                    raise ValueError("candidate_source_invalid")
            else:
                raise ValueError("candidate_source_invalid")
            if registered is not None and registered != candidate:
                raise ValueError("candidate_row_changed")
            return candidate
    except D7CandidateEvidenceError:
        raise
    except Exception as exc:
        raise D7CandidateEvidenceError("d7_candidate_ownership_invalid") from exc


class D7CandidateEvidenceStore:
    """One TaskRun's protected candidate body records."""

    def __init__(
        self,
        run_dir: Path,
        task_run_id: str,
        protection: _EvidenceProtection,
        clock: Callable[[], float],
        audit: FileD7CandidateEvidenceAudit,
        *,
        create: bool = True,
    ) -> None:
        try:
            parsed = UUID(task_run_id)
        except (TypeError, ValueError) as exc:
            raise D7CandidateEvidenceError("d7_task_run_id_invalid") from exc
        if parsed.version != 4 or str(parsed) != task_run_id:
            raise D7CandidateEvidenceError("d7_task_run_id_invalid")
        if not all(
            callable(getattr(protection, name, None))
            for name in (
                "protect",
                "unprotect",
                "restrict_directory",
                "verify_directory",
            )
        ) or not isinstance(getattr(protection, "identity", None), str):
            raise D7CandidateEvidenceError("d7_candidate_protection_invalid")
        if not callable(clock) or audit is None:
            raise D7CandidateEvidenceError("d7_candidate_store_config_invalid")
        self.run_dir = Path(run_dir)
        self.task_run_id = task_run_id
        self.protection = protection
        self.clock = clock
        self.audit = audit
        self.directory = self.run_dir / "d7-candidate-evidence"
        self._lock = RLock()
        try:
            if create:
                self.run_dir.mkdir(parents=True, exist_ok=True)
            if self.run_dir.is_symlink() or not self.run_dir.is_dir():
                raise D7CandidateEvidenceError("d7_candidate_run_directory_invalid")
            new_directory = not self.directory.exists()
            if new_directory and create:
                self.directory.mkdir()
                self.protection.restrict_directory(self.directory)
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise D7CandidateEvidenceError("d7_candidate_directory_invalid")
            self.protection.verify_directory(self.directory)
            owner_path = self.directory / "owner.json"
            expected_owner = {
                "task_run_id": task_run_id,
                "identity": self.protection.identity,
            }
            if new_directory and create:
                self._write_exclusive(owner_path, _canonical_json(expected_owner))
            if (
                owner_path.is_symlink()
                or json.loads(owner_path.read_bytes()) != expected_owner
            ):
                raise D7CandidateEvidenceError("d7_candidate_ownership_invalid")
        except WindowsEvidenceProtectionError as exc:
            raise D7CandidateEvidenceError("d7_candidate_protection_failed") from exc
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, D7CandidateEvidenceError):
                raise
            raise D7CandidateEvidenceError("d7_candidate_directory_failed") from exc

    def _verify(self) -> None:
        try:
            self.protection.verify_directory(self.directory)
        except WindowsEvidenceProtectionError as exc:
            raise D7CandidateEvidenceError("d7_candidate_protection_failed") from exc

    @contextmanager
    def _mutation_lock(self):
        with self._lock:
            self._verify()
            try:
                with run_lock(self.directory):
                    yield
            except ValueError as exc:
                raise D7CandidateEvidenceError("d7_candidate_evidence_busy") from exc

    @staticmethod
    def _write_exclusive(path: Path, payload: bytes) -> None:
        if path.exists() or path.is_symlink():
            raise D7CandidateEvidenceConflict()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".d7-pending-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError as exc:
                raise D7CandidateEvidenceConflict() from exc
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def _paths(self, candidate_id: str) -> tuple[Path, Path]:
        return (
            self.directory / f"{candidate_id}.meta.json",
            self.directory / f"{candidate_id}.cipher",
        )

    def _entropy(self, candidate: D7AnswerCandidate) -> bytes:
        return hashlib.sha256(
            f"qinyuan-d7-candidate:{candidate.task_run_id}:"
            f"{candidate.candidate_id}:{candidate.evidence_version}".encode("ascii")
        ).digest()

    def _read_metadata(self, path: Path) -> D7CandidateEvidenceMetadata:
        try:
            data = path.read_bytes()
            value = parse_json(data, 64 * 1024)
            required = {
                "schema_version",
                "task_run_id",
                "candidate_id",
                "evidence_version",
                "execution_id",
                "candidate_binding",
                "body_sha256",
                "cipher_sha256",
                "byte_size",
                "created_at",
            }
            if not isinstance(value, dict) or frozenset(value) not in {
                frozenset(required),
                frozenset(required | {"model_request_id"}),
            }:
                raise ValueError("fields")
            if "model_request_id" not in value:
                value = {**value, "model_request_id": None}
            metadata = D7CandidateEvidenceMetadata(**value)
            if type(metadata.schema_version) is not int or metadata.schema_version != 1:
                raise ValueError("schema")
            candidate = D7AnswerCandidate.from_dict(metadata.candidate_binding)
            if (
                candidate.task_run_id != metadata.task_run_id
                or candidate.candidate_id != metadata.candidate_id
                or candidate.evidence_version != metadata.evidence_version
                or candidate.execution_id != metadata.execution_id
                or type(metadata.byte_size) is not int
                or not 1 <= metadata.byte_size <= MAX_D7_CANDIDATE_BYTES
                or not _is_sha256(metadata.body_sha256)
                or not _is_sha256(metadata.cipher_sha256)
                or isinstance(metadata.created_at, bool)
                or not isinstance(metadata.created_at, (int, float))
                or not math.isfinite(metadata.created_at)
                or metadata.model_request_id is not None
                and not _is_uuid4(metadata.model_request_id)
            ):
                raise ValueError("binding")
            return metadata
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise D7CandidateEvidenceError("d7_candidate_metadata_corrupt") from exc

    def _metadata(self, candidate: D7AnswerCandidate) -> D7CandidateEvidenceMetadata:
        metadata_path, _ = self._paths(candidate.candidate_id)
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise D7CandidateEvidenceNotFound()
        metadata = self._read_metadata(metadata_path)
        if (
            metadata.task_run_id != candidate.task_run_id
            or metadata.candidate_id != candidate.candidate_id
            or metadata.evidence_version != candidate.evidence_version
            or metadata.execution_id != candidate.execution_id
            or metadata.candidate_binding != candidate.to_dict()
        ):
            raise D7CandidateEvidenceError("d7_candidate_binding_mismatch")
        return metadata

    def _unseal(
        self, candidate: D7AnswerCandidate, ciphertext: bytes
    ) -> tuple[bytes, float, str | None]:
        try:
            envelope = parse_json(
                self.protection.unprotect(ciphertext, self._entropy(candidate)),
                64 * 1024,
            )
        except Exception as exc:
            raise D7CandidateEvidenceError("d7_candidate_decrypt_failed") from exc
        required_envelope = {
            "schema_version",
            "candidate_binding",
            "body_sha256",
            "body_hex",
            "created_at",
        }
        if (
            not isinstance(envelope, dict)
            or frozenset(envelope) not in {
                frozenset(required_envelope),
                frozenset(required_envelope | {"model_request_id"}),
            }
            or type(envelope.get("schema_version")) is not int
            or envelope["schema_version"] != 1
            or envelope["candidate_binding"] != candidate.to_dict()
            or not _is_sha256(envelope["body_sha256"])
            or not isinstance(envelope["body_hex"], str)
            or isinstance(envelope["created_at"], bool)
            or not isinstance(envelope["created_at"], (int, float))
            or not math.isfinite(envelope["created_at"])
            or envelope.get("model_request_id") is not None
            and not _is_uuid4_or5(envelope["model_request_id"])
        ):
            raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed")
        try:
            body = bytes.fromhex(envelope["body_hex"])
        except ValueError as exc:
            raise D7CandidateEvidenceError(
                "d7_candidate_body_integrity_failed"
            ) from exc
        if not 1 <= len(body) <= MAX_D7_CANDIDATE_BYTES:
            raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed")
        _validate_decoded_candidate_body(body)
        if hashlib.sha256(body).hexdigest() != envelope["body_sha256"]:
            raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed")
        return body, float(envelope["created_at"]), envelope.get("model_request_id")

    def _decrypt(
        self, candidate: D7AnswerCandidate, metadata: D7CandidateEvidenceMetadata
    ) -> bytes:
        _, cipher_path = self._paths(candidate.candidate_id)
        if cipher_path.is_symlink() or not cipher_path.is_file():
            raise D7CandidateEvidenceError("d7_candidate_cipher_missing")
        ciphertext = cipher_path.read_bytes()
        if hashlib.sha256(ciphertext).hexdigest() != metadata.cipher_sha256:
            raise D7CandidateEvidenceError("d7_candidate_cipher_integrity_failed")
        body, _, model_request_id = self._unseal(candidate, ciphertext)
        if (
            len(body) != metadata.byte_size
            or hashlib.sha256(body).hexdigest() != metadata.body_sha256
            or model_request_id != metadata.model_request_id
        ):
            raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed")
        return body

    def save(
        self,
        candidate: D7AnswerCandidate,
        value: str,
        *,
        model_request_id: str | None = None,
    ) -> D7CandidateEvidenceMetadata:
        if model_request_id is not None and not _is_uuid4_or5(model_request_id):
            raise D7CandidateEvidenceError("d7_candidate_model_request_id_invalid")
        body = _encode_candidate_text(value)
        with self._mutation_lock():
            metadata_path, cipher_path = self._paths(candidate.candidate_id)
            expected_hash = hashlib.sha256(body).hexdigest()
            if metadata_path.exists() or cipher_path.exists():
                if metadata_path.is_symlink() or cipher_path.is_symlink():
                    raise D7CandidateEvidenceError("d7_candidate_file_invalid")
                if metadata_path.exists() and not cipher_path.exists():
                    raise D7CandidateEvidenceError("d7_candidate_record_incomplete")
                if not metadata_path.exists() and cipher_path.exists():
                    ciphertext = cipher_path.read_bytes()
                    recovered_body, created_at, recovered_request_id = self._unseal(
                        candidate, ciphertext
                    )
                    if (
                        recovered_body != body
                        or recovered_request_id != model_request_id
                    ):
                        raise D7CandidateEvidenceConflict()
                    recovered = D7CandidateEvidenceMetadata(
                        1,
                        candidate.task_run_id,
                        candidate.candidate_id,
                        candidate.evidence_version,
                        candidate.execution_id,
                        candidate.to_dict(),
                        expected_hash,
                        hashlib.sha256(ciphertext).hexdigest(),
                        len(body),
                        created_at,
                        model_request_id,
                    )
                    self._write_exclusive(
                        metadata_path, _canonical_json(asdict(recovered))
                    )
                    return recovered
                existing = self._metadata(candidate)
                if (
                    existing.body_sha256 != expected_hash
                    or existing.model_request_id != model_request_id
                    or self._decrypt(candidate, existing) != body
                ):
                    raise D7CandidateEvidenceConflict()
                return existing

            created_at = float(self.clock())
            if not math.isfinite(created_at):
                raise D7CandidateEvidenceError("d7_candidate_clock_invalid")
            envelope = {
                "schema_version": 1,
                "candidate_binding": candidate.to_dict(),
                "body_sha256": expected_hash,
                "body_hex": body.hex(),
                "created_at": created_at,
            }
            if model_request_id is not None:
                envelope["model_request_id"] = model_request_id
            try:
                ciphertext = self.protection.protect(
                    _canonical_json(envelope), self._entropy(candidate)
                )
            except WindowsEvidenceProtectionError as exc:
                raise D7CandidateEvidenceError("d7_candidate_protect_failed") from exc
            metadata = D7CandidateEvidenceMetadata(
                1,
                candidate.task_run_id,
                candidate.candidate_id,
                candidate.evidence_version,
                candidate.execution_id,
                candidate.to_dict(),
                expected_hash,
                hashlib.sha256(ciphertext).hexdigest(),
                len(body),
                envelope["created_at"],
                model_request_id,
            )
            self._write_exclusive(cipher_path, ciphertext)
            try:
                self._write_exclusive(metadata_path, _canonical_json(asdict(metadata)))
            except Exception:
                cipher_path.unlink(missing_ok=True)
                raise
            return metadata

    def query(self, candidate: D7AnswerCandidate) -> dict[str, object]:
        with self._lock:
            self._verify()
            metadata = self._metadata(candidate)
            return {
                **asdict(metadata),
                "body": None,
                "body_status": "withheld",
            }

    def read(
        self,
        candidate: D7AnswerCandidate,
        *,
        adapter_id: str,
    ) -> str:
        with self._mutation_lock():
            metadata = self._metadata(candidate)
            self.audit.record(
                identity=self.protection.identity,
                adapter_id=adapter_id,
                candidate=candidate,
                operation="read",
                result="authorized",
                occurred_at=float(self.clock()),
            )
            try:
                body = self._decrypt(candidate, metadata)
                self.audit.record(
                    identity=self.protection.identity,
                    adapter_id=adapter_id,
                    candidate=candidate,
                    operation="read",
                    result="success",
                    occurred_at=float(self.clock()),
                )
                return body.decode("utf-8", errors="strict")
            except D7CandidateEvidenceError:
                self.audit.record(
                    identity=self.protection.identity,
                    adapter_id=adapter_id,
                    candidate=candidate,
                    operation="read",
                    result="failed",
                    occurred_at=float(self.clock()),
                )
                raise
            except UnicodeDecodeError as exc:
                self.audit.record(
                    identity=self.protection.identity,
                    adapter_id=adapter_id,
                    candidate=candidate,
                    operation="read",
                    result="failed",
                    occurred_at=float(self.clock()),
                )
                raise D7CandidateEvidenceError(
                    "d7_candidate_body_integrity_failed"
                ) from exc
            except Exception as exc:
                self.audit.record(
                    identity=self.protection.identity,
                    adapter_id=adapter_id,
                    candidate=candidate,
                    operation="read",
                    result="failed",
                    occurred_at=float(self.clock()),
                )
                raise D7CandidateEvidenceError("d7_candidate_read_failed") from exc


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class D7CandidateEvidenceService:
    """Trusted assembly facade; production always constructs current-user DPAPI."""

    def __init__(
        self,
        run_dir: Path,
        task_run_id: str,
        *,
        trusted_adapter_ids: tuple[str, ...],
        create: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _validate_owned_run(Path(run_dir), task_run_id)
        if (
            not isinstance(trusted_adapter_ids, tuple)
            or any(
                not isinstance(item, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", item)
                for item in trusted_adapter_ids
            )
            or len(set(trusted_adapter_ids)) != len(trusted_adapter_ids)
        ):
            raise D7CandidateEvidenceError("d7_candidate_trusted_adapters_invalid")
        self.run_dir = Path(run_dir)
        self.task_run_id = task_run_id
        evidence_dir = self.run_dir / "d7-candidate-evidence"
        self.store = D7CandidateEvidenceStore(
            self.run_dir,
            task_run_id,
            WindowsEvidenceProtection(),
            clock,
            FileD7CandidateEvidenceAudit(evidence_dir / "audit.jsonl"),
            create=create,
        )
        self._read_ports = {
            adapter_id: D7CandidateEvidenceReadPort(self, adapter_id)
            for adapter_id in trusted_adapter_ids
        }

    def save_pending(
        self,
        candidate: D7AnswerCandidate,
        value: str,
        model_request_id: str,
    ) -> dict[str, object]:
        """Protect a model candidate before its Controller commit exists."""
        if not isinstance(candidate, D7AnswerCandidate):
            raise D7CandidateEvidenceError("d7_candidate_binding_invalid")
        if not _is_uuid4_or5(model_request_id):
            raise D7CandidateEvidenceError("d7_candidate_model_request_id_invalid")
        verified = _persisted_candidate(
            self.run_dir,
            self.task_run_id,
            candidate.candidate_id,
            candidate_hint=candidate,
            require_registered=False,
        )
        if verified != candidate:
            raise D7CandidateEvidenceError("d7_candidate_binding_mismatch")
        metadata = self.store.save(
            candidate, value, model_request_id=model_request_id
        )
        return {
            "task_run_id": metadata.task_run_id,
            "candidate_id": metadata.candidate_id,
            "execution_id": metadata.execution_id,
            "evidence_version": metadata.evidence_version,
            "body_sha256": metadata.body_sha256,
            "byte_size": metadata.byte_size,
            "model_request_id": metadata.model_request_id,
            **_candidate_source_projection(candidate),
        }

    def load_pending_for_request(
        self, model_request_id: str
    ) -> tuple[D7AnswerCandidate, dict[str, object]] | None:
        if not _is_uuid4_or5(model_request_id):
            raise D7CandidateEvidenceError("d7_candidate_model_request_id_invalid")
        matches: list[tuple[D7AnswerCandidate, D7CandidateEvidenceMetadata]] = []
        for path in self.store.directory.glob("*.meta.json"):
            if path.is_symlink() or not path.is_file():
                raise D7CandidateEvidenceError("d7_candidate_metadata_invalid")
            metadata = self.store._read_metadata(path)
            if metadata.model_request_id != model_request_id:
                continue
            candidate = D7AnswerCandidate.from_dict(metadata.candidate_binding)
            if candidate.candidate_id != path.name.removesuffix(".meta.json"):
                raise D7CandidateEvidenceError("d7_candidate_binding_mismatch")
            verified = _persisted_candidate(
                self.run_dir,
                self.task_run_id,
                candidate.candidate_id,
                candidate_hint=candidate,
                require_registered=False,
            )
            if verified != candidate:
                raise D7CandidateEvidenceError("d7_candidate_binding_mismatch")
            if metadata.task_run_id != self.task_run_id:
                raise D7CandidateEvidenceError("d7_candidate_ownership_invalid")
            matches.append((candidate, metadata))
        if len(matches) > 1:
            raise D7CandidateEvidenceError("d7_candidate_model_request_conflict")
        if not matches:
            return None
        candidate, metadata = matches[0]
        self.store._decrypt(candidate, metadata)
        return candidate, {
            "task_run_id": metadata.task_run_id,
            "candidate_id": metadata.candidate_id,
            "execution_id": metadata.execution_id,
            "evidence_version": metadata.evidence_version,
            "body_sha256": metadata.body_sha256,
            "byte_size": metadata.byte_size,
            "model_request_id": metadata.model_request_id,
            **_candidate_source_projection(candidate),
        }

    def read_port(self, adapter_id: str) -> D7CandidateEvidenceReadPort:
        port = self._read_ports.get(adapter_id)
        if port is None:
            raise D7CandidateEvidenceError("d7_candidate_reader_untrusted")
        return port

    def save(
        self,
        candidate: D7AnswerCandidate,
        value: str,
    ) -> dict[str, object]:
        if not isinstance(candidate, D7AnswerCandidate):
            raise D7CandidateEvidenceError("d7_candidate_binding_invalid")
        persisted = _persisted_candidate(
            self.run_dir, self.task_run_id, candidate.candidate_id
        )
        if persisted != candidate:
            raise D7CandidateEvidenceError("d7_candidate_binding_mismatch")
        metadata = self.store.save(candidate, value)
        return {
            "task_run_id": metadata.task_run_id,
            "candidate_id": metadata.candidate_id,
            "execution_id": metadata.execution_id,
            "evidence_version": metadata.evidence_version,
            "body_sha256": metadata.body_sha256,
            "byte_size": metadata.byte_size,
            "body_ref": f"d7-candidate:{metadata.candidate_id}:{metadata.evidence_version}",
            "body_status": "withheld",
            **_candidate_source_projection(candidate),
        }

    def query(self, candidate_id: str) -> dict[str, object]:
        candidate_id = _validate_candidate_id(candidate_id)
        candidate = _persisted_candidate(self.run_dir, self.task_run_id, candidate_id)
        result = self.store.query(candidate)
        result = {
            key: result[key]
            for key in (
                "task_run_id",
                "candidate_id",
                "execution_id",
                "evidence_version",
                "body_sha256",
                "byte_size",
                "created_at",
                "body_status",
            )
        } | {
            "body_ref": f"d7-candidate:{candidate_id}:{candidate.evidence_version}",
            "body": None,
        }
        return result | _candidate_source_projection(candidate)

    def _read_for_port(
        self, candidate_id: str, port: D7CandidateEvidenceReadPort
    ) -> str:
        candidate_id = _validate_candidate_id(candidate_id)
        if (
            not isinstance(port, D7CandidateEvidenceReadPort)
            or self._read_ports.get(port._adapter_id) is not port
        ):
            raise D7CandidateEvidenceError("d7_candidate_reader_untrusted")
        candidate = _persisted_candidate(self.run_dir, self.task_run_id, candidate_id)
        return self.store.read(candidate, adapter_id=port._adapter_id)


__all__ = [
    "MAX_D7_CANDIDATE_BYTES",
    "D7CandidateEvidenceConflict",
    "D7CandidateEvidenceError",
    "D7CandidateEvidenceMetadata",
    "D7CandidateEvidenceNotFound",
    "D7CandidateEvidenceReadPort",
    "D7CandidateEvidenceService",
    "D7CandidateEvidenceStore",
    "FileD7CandidateEvidenceAudit",
]
