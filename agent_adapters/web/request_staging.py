"""Atomic, bounded v3 request attachment staging and safe request receipts."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from agent_adapters.run_files import encode, immutable_read, immutable_write, run_lock
from agent_adapters.windows_evidence_protection import (
    WindowsEvidenceProtection,
    WindowsEvidenceProtectionError,
)

MAX_V3_REQUEST_ATTACHMENTS = 16
MAX_V3_REQUEST_BYTES = 64 * 1024 * 1024
MAX_D3_ATTACHMENT_BYTES = 16 * 1024 * 1024


def _uuid4(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{name}_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name}_invalid")
    return value


def _safe_name(name: str) -> str:
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or name.rstrip(" .").casefold() in reserved
        or any(
            char in name for char in ("/", "\\", "\x00", ":", "<", ">", "|", "?", "*")
        )
        or name.endswith((".", " "))
        or not name.isprintable()
    ):
        raise ValueError("attachment_name_invalid")
    return name


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    index: int
    name: str
    byte_size: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.index) is not int
            or not 0 <= self.index < MAX_V3_REQUEST_ATTACHMENTS
        ):
            raise ValueError("attachment_index_invalid")
        _safe_name(self.name)
        if (
            type(self.byte_size) is not int
            or not 0 <= self.byte_size <= MAX_V3_REQUEST_BYTES
        ):
            raise ValueError("attachment_size_invalid")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.sha256)
        ):
            raise ValueError("attachment_digest_invalid")


@dataclass(frozen=True, slots=True)
class RequestReceipt:
    request_id: str
    request_sha256: str
    request_identity_sha256: str
    state: str
    attachments: tuple[StagedAttachment, ...]
    created_at: float
    task_run_id: str | None = None
    reason: str | None = None
    staging_status: str = "complete"
    conflict_count: int = 0
    request_exit_code: int | None = None

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "request_id")
        if (
            not isinstance(self.request_sha256, str)
            or len(self.request_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.request_sha256)
        ):
            raise ValueError("request_digest_invalid")
        if (
            not isinstance(self.request_identity_sha256, str)
            or len(self.request_identity_sha256) != 64
            or any(
                char not in "0123456789abcdef" for char in self.request_identity_sha256
            )
        ):
            raise ValueError("request_identity_digest_invalid")
        if self.state not in {
            "staged",
            "task_run_created",
            "capability_not_supported",
            "input_staging_error",
        }:
            raise ValueError("request_receipt_state_invalid")
        if (
            not isinstance(self.attachments, tuple)
            or len(self.attachments) > MAX_V3_REQUEST_ATTACHMENTS
        ):
            raise ValueError("request_receipt_attachments_invalid")
        if any(not isinstance(item, StagedAttachment) for item in self.attachments):
            raise ValueError("request_receipt_attachments_invalid")
        if tuple(item.index for item in self.attachments) != tuple(
            range(len(self.attachments))
        ):
            raise ValueError("request_receipt_attachment_order_invalid")
        if sum(item.byte_size for item in self.attachments) > MAX_V3_REQUEST_BYTES:
            raise ValueError("request_receipt_size_limit_exceeded")
        if self.task_run_id is not None:
            _uuid4(self.task_run_id, "task_run_id")
        if self.staging_status not in {
            "complete",
            "removed",
            "cleanup_pending",
            "failed",
        }:
            raise ValueError("request_staging_status_invalid")
        if type(self.conflict_count) is not int or self.conflict_count < 0:
            raise ValueError("request_conflict_count_invalid")
        if self.request_exit_code is not None and (
            type(self.request_exit_code) is not int or self.request_exit_code != 2
        ):
            raise ValueError("request_exit_code_invalid")
        if self.state in {"capability_not_supported", "input_staging_error"} and (
            self.request_exit_code != 2
        ):
            raise ValueError("request_exit_code_missing")
        if type(self.created_at) not in {int, float} or not math.isfinite(
            self.created_at
        ):
            raise ValueError("request_receipt_time_invalid")
        if self.reason is not None and (
            not isinstance(self.reason, str)
            or not self.reason.isascii()
            or "\x00" in self.reason
        ):
            raise ValueError("request_receipt_reason_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "request_identity_sha256": self.request_identity_sha256,
            "state": self.state,
            "attachments": [asdict(item) for item in self.attachments],
            "created_at": self.created_at,
            "task_run_id": self.task_run_id,
            "reason": self.reason,
            "staging_status": self.staging_status,
            "conflict_count": self.conflict_count,
            "request_exit_code": self.request_exit_code,
        }

    @classmethod
    def from_dict(cls, value: object) -> RequestReceipt:
        expected = {
            "schema_version",
            "request_id",
            "request_sha256",
            "request_identity_sha256",
            "state",
            "attachments",
            "created_at",
            "task_run_id",
            "reason",
            "staging_status",
            "conflict_count",
            "request_exit_code",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("request_receipt_invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("request_receipt_version_unsupported")
        attachments = value["attachments"]
        if not isinstance(attachments, list):
            raise TypeError("request_receipt_attachments_invalid")
        return cls(
            value["request_id"],
            value["request_sha256"],
            value["request_identity_sha256"],
            value["state"],
            tuple(StagedAttachment(**item) for item in attachments),
            value["created_at"],
            value["task_run_id"],
            value["reason"],
            value["staging_status"],
            value["conflict_count"],
            value["request_exit_code"],
        )


@dataclass(frozen=True, slots=True)
class RequestStageResult:
    receipt: RequestReceipt
    staged_directory: Path | None
    replay: bool
    conflict: bool = False


class RequestStagingError(RuntimeError):
    def __init__(self, code: str, receipt: RequestReceipt | None = None) -> None:
        self.code = code
        self.receipt = receipt
        super().__init__(code)


class D7RequestStager:
    """Own request-scoped payload bytes separately from D3 Sandbox imports."""

    def __init__(self, state_dir: Path, *, clock, protection=None) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.root = self.state_dir / "requests"
        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = RLock()
        self.protection = protection or WindowsEvidenceProtection()
        self._restrict_or_verify(self.root, created=created)

    @classmethod
    def open_existing(
        cls, state_dir: Path, *, clock, protection=None
    ) -> D7RequestStager:
        """Open the request ledger without creating any directory or file."""
        stager = cls.__new__(cls)
        stager.state_dir = Path(state_dir).resolve()
        stager.root = stager.state_dir / "requests"
        stager.clock = clock
        stager._lock = RLock()
        stager.protection = protection or WindowsEvidenceProtection()
        return stager

    def _restrict_or_verify(self, directory: Path, *, created: bool) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise RequestStagingError("request_directory_invalid")
        try:
            if created:
                self.protection.restrict_directory(directory)
            else:
                self.protection.verify_directory(directory)
        except WindowsEvidenceProtectionError as exc:
            raise RequestStagingError("request_directory_protection_failed") from exc

    def _verify_root(self) -> bool:
        if not self.root.exists():
            return False
        self._restrict_or_verify(self.root, created=False)
        return True

    def _request_dir(self, request_id: str) -> Path:
        _uuid4(request_id, "request_id")
        directory = self.root / request_id
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise RequestStagingError("request_directory_invalid")
        created = not directory.exists()
        self._verify_root()
        directory.mkdir(exist_ok=True)
        self._restrict_or_verify(directory, created=created)
        return directory

    def _existing_request_dir(self, request_id: str) -> Path | None:
        _uuid4(request_id, "request_id")
        if not self._verify_root():
            return None
        directory = self.root / request_id
        if not directory.exists():
            return None
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise RequestStagingError("request_directory_invalid")
        self._restrict_or_verify(directory, created=False)
        return directory

    @staticmethod
    def _source(root_text: str, relative_text: str) -> tuple[Path, os.stat_result]:
        root = Path(root_text)
        try:
            if (
                not root.is_absolute()
                or str(root.resolve(strict=True)) != root_text
                or not root.is_dir()
                or root.is_symlink()
            ):
                raise RequestStagingError("attachment_source_root_invalid")
            relative = Path(relative_text)
            if (
                not isinstance(relative_text, str)
                or not relative_text
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                raise RequestStagingError("attachment_source_rejected")
            target = root
            for index, component in enumerate(relative.parts):
                target = target / component
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise RequestStagingError("attachment_source_rejected")
                if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
                    raise RequestStagingError("attachment_source_rejected")
            if not stat.S_ISREG(info.st_mode) or not target.resolve(
                strict=True
            ).is_relative_to(root):
                raise RequestStagingError("attachment_source_rejected")
            return target, info
        except RequestStagingError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise RequestStagingError("attachment_source_unavailable") from exc

    def _read_copy(
        self,
        root_text: str,
        relative: str,
        destination: Path,
        remaining: int,
        index: int,
    ) -> StagedAttachment:
        source, before = self._source(root_text, relative)
        if before.st_size > remaining:
            raise RequestStagingError("attachment_run_capacity_exceeded")
        name = _safe_name(source.name)
        digest = hashlib.sha256()
        length = 0
        try:
            with (
                source.open("rb") as input_stream,
                destination.open("xb") as output_stream,
            ):
                opened = os.fstat(input_stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise RequestStagingError("attachment_source_changed")
                while block := input_stream.read(64 * 1024):
                    length += len(block)
                    if length > remaining or length > MAX_V3_REQUEST_BYTES:
                        raise RequestStagingError("attachment_run_capacity_exceeded")
                    output_stream.write(block)
                    digest.update(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
                finished = os.fstat(input_stream.fileno())
            if length != before.st_size or (
                opened.st_mtime_ns,
                opened.st_size,
                opened.st_ino,
            ) != (finished.st_mtime_ns, finished.st_size, finished.st_ino):
                raise RequestStagingError("attachment_source_changed")
            return StagedAttachment(index, name, length, digest.hexdigest())
        except RequestStagingError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise RequestStagingError("attachment_staging_failed") from exc

    def stage(
        self,
        *,
        request_id: str,
        source_root: str,
        attachment_paths: tuple[str, ...],
        request_identity: object,
    ) -> RequestStageResult:
        _uuid4(request_id, "request_id")
        if (
            not isinstance(attachment_paths, tuple)
            or len(attachment_paths) > MAX_V3_REQUEST_ATTACHMENTS
        ):
            raise RequestStagingError("attachment_count_invalid")
        directory = self._request_dir(request_id)
        identity_sha = hashlib.sha256(encode(request_identity)).hexdigest()
        with self._lock, run_lock(directory):
            temp = directory / (".stage-" + str(uuid4()))
            payload = temp / "attachments"
            try:
                payload.mkdir(parents=True)
            except OSError:
                temp_removed = self._remove_tree(temp)
                return self._record_staging_error(
                    directory,
                    request_id,
                    identity_sha,
                    "attachment_staging_failed",
                    cleanup_pending=not temp_removed,
                )
            records: list[StagedAttachment] = []
            total = 0
            payload_committed = False
            try:
                for index, relative in enumerate(attachment_paths):
                    provisional_name = f"{index:02d}.input"
                    record = self._read_copy(
                        source_root,
                        relative,
                        payload / provisional_name,
                        MAX_V3_REQUEST_BYTES - total,
                        index,
                    )
                    total += record.byte_size
                    records.append(
                        StagedAttachment(
                            index, record.name, record.byte_size, record.sha256
                        )
                    )
                if len({item.name.casefold() for item in records}) != len(records):
                    raise RequestStagingError("duplicate_attachment_names")
                request_sha = hashlib.sha256(
                    encode(
                        {
                            "request": request_identity,
                            "attachments": [asdict(item) for item in records],
                        }
                    )
                ).hexdigest()
                receipt_path = directory / "receipt.json"
                if receipt_path.exists():
                    existing = self.read_receipt(request_id)
                    assert existing is not None
                    if existing.request_sha256 == request_sha:
                        shutil.rmtree(temp)
                        existing_payload = directory / "attachments"
                        return RequestStageResult(
                            existing,
                            existing_payload if existing_payload.exists() else None,
                            True,
                        )
                    self._record_conflict(
                        directory, existing.request_sha256, request_sha
                    )
                    shutil.rmtree(temp)
                    return RequestStageResult(
                        self.read_receipt(request_id) or existing,
                        None,
                        True,
                        conflict=True,
                    )
                staged_payload = directory / "attachments"
                if staged_payload.exists():
                    raise RequestStagingError("staged_payload_without_receipt")
                os.replace(payload, staged_payload)
                payload_committed = True
                staged_records = tuple(records)
                receipt = RequestReceipt(
                    request_id,
                    request_sha,
                    identity_sha,
                    "staged",
                    staged_records,
                    float(self.clock()),
                )
                immutable_write(receipt_path, receipt.to_dict())
                if not self._remove_tree(temp):
                    receipt = self.record_outcome(
                        request_id,
                        state="staged",
                        staging_status="cleanup_pending",
                    )
                return RequestStageResult(receipt, staged_payload, False)
            except RequestStagingError as exc:
                stage_removed = self._remove_tree(temp)
                if payload_committed and not (directory / "receipt.json").exists():
                    payload_removed = self._remove_tree(directory / "attachments")
                else:
                    payload_removed = True
                result = self._record_staging_error(
                    directory,
                    request_id,
                    identity_sha,
                    exc.code,
                    cleanup_pending=not (stage_removed and payload_removed),
                )
                return result
            except (OSError, ValueError, TypeError):
                stage_removed = self._remove_tree(temp)
                if payload_committed and not (directory / "receipt.json").exists():
                    payload_removed = self._remove_tree(directory / "attachments")
                else:
                    payload_removed = True
                result = self._record_staging_error(
                    directory,
                    request_id,
                    identity_sha,
                    "attachment_staging_failed",
                    cleanup_pending=not (stage_removed and payload_removed),
                )
                return result

    def _record_conflict(
        self, directory: Path, previous_sha: str, incoming_sha: str
    ) -> None:
        conflicts = directory / "conflicts"
        conflicts.mkdir(exist_ok=True)
        immutable_write(
            conflicts / (str(uuid4()) + ".json"),
            {
                "request_id": directory.name,
                "state": "idempotency_conflict",
                "original_request_sha256": previous_sha,
                "incoming_request_sha256": incoming_sha,
                "recorded_at": float(self.clock()),
            },
        )

    def _record_staging_error(
        self,
        directory: Path,
        request_id: str,
        identity_sha: str,
        reason: str,
        *,
        cleanup_pending: bool = False,
    ) -> RequestStageResult:
        receipt_path = directory / "receipt.json"
        if receipt_path.exists():
            existing = self.read_receipt(request_id)
            assert existing is not None
            if existing.request_identity_sha256 != identity_sha:
                self._record_conflict(directory, existing.request_sha256, identity_sha)
                return RequestStageResult(existing, None, True, conflict=True)
            return RequestStageResult(existing, None, True)
        receipt = RequestReceipt(
            request_id,
            identity_sha,
            identity_sha,
            "input_staging_error",
            (),
            float(self.clock()),
            reason=reason,
            staging_status="cleanup_pending" if cleanup_pending else "failed",
            request_exit_code=2,
        )
        immutable_write(receipt_path, receipt.to_dict())
        return RequestStageResult(receipt, None, False)

    def record_outcome(
        self,
        request_id: str,
        *,
        state: str,
        reason: str | None = None,
        task_run_id: str | None = None,
        staging_status: str | None = None,
    ) -> RequestReceipt:
        directory = self._existing_request_dir(request_id)
        if directory is None:
            raise RequestStagingError("request_receipt_missing")
        with self._lock, run_lock(directory):
            path = directory / "receipt.json"
            if not path.exists():
                raise RequestStagingError("request_receipt_missing")
            previous = self.read_receipt(request_id)
            assert previous is not None
            if state not in {
                previous.state,
                "task_run_created",
                "capability_not_supported",
            }:
                raise RequestStagingError("request_receipt_transition_invalid")
            if previous.state == "capability_not_supported" and state != previous.state:
                raise RequestStagingError("request_receipt_transition_invalid")
            if previous.state == "task_run_created" and state != previous.state:
                raise RequestStagingError("request_receipt_transition_invalid")
            if (
                task_run_id is not None
                and previous.task_run_id is not None
                and task_run_id != previous.task_run_id
            ):
                raise RequestStagingError("request_task_run_conflict")
            updated = RequestReceipt(
                previous.request_id,
                previous.request_sha256,
                previous.request_identity_sha256,
                state,
                previous.attachments,
                previous.created_at,
                task_run_id if task_run_id is not None else previous.task_run_id,
                previous.reason if reason is None else reason,
                staging_status or previous.staging_status,
                previous.conflict_count,
                2
                if state in {"capability_not_supported", "input_staging_error"}
                else previous.request_exit_code,
            )
            if updated == previous:
                return previous
            # Receipt history is append-only; the current projection is the last
            # committed immutable version and carries the prior digest.
            versions = directory / "receipt-versions"
            versions.mkdir(exist_ok=True)
            existing_versions = [
                int(item.stem)
                for item in versions.glob("*.json")
                if item.stem.isdecimal()
            ]
            next_version = max(existing_versions, default=0) + 1
            immutable_write(
                versions / f"{next_version:06d}.json",
                {
                    "previous_version_sha256": hashlib.sha256(
                        encode(previous.to_dict())
                    ).hexdigest(),
                    "receipt": updated.to_dict(),
                },
            )
            return updated

    def remove_rejected_payload(self, request_id: str) -> str:
        directory = self._existing_request_dir(request_id)
        if directory is None:
            raise RequestStagingError("request_receipt_missing")
        with self._lock, run_lock(directory):
            path = directory / "attachments"
            if not path.exists() and not path.is_symlink():
                return "removed"
            return "removed" if self._remove_tree(path) else "cleanup_pending"

    def remove_task_run_payload(
        self, request_id: str, task_run_id: str, run_dir: Path
    ) -> str:
        """Remove request bytes only after the owning Sandbox cleanup is confirmed."""
        _uuid4(task_run_id, "task_run_id")
        receipt = self.read_receipt(request_id)
        if receipt is None or receipt.task_run_id != task_run_id:
            raise RequestStagingError("request_task_run_mismatch")
        run_path = Path(run_dir)
        if run_path.is_symlink() or not run_path.is_dir():
            raise RequestStagingError("task_run_directory_invalid")
        evidence = run_path / "request-evidence"
        if evidence.is_symlink():
            raise RequestStagingError("task_run_directory_invalid")
        payload = evidence / "attachments"
        if not payload.exists() and not payload.is_symlink():
            return "removed"
        return "removed" if self._remove_tree(payload) else "cleanup_pending"

    def _remove_tree(self, path: Path) -> bool:
        if not path.exists() and not path.is_symlink():
            return True
        try:
            if path.is_symlink() or path.resolve().parent != path.parent.resolve():
                return False
            shutil.rmtree(path)
        except OSError:
            return False
        return not path.exists() and not path.is_symlink()

    def move_to_task_run(self, request_id: str, run_dir: Path) -> Path | None:
        """Move a complete request stage into its owning TaskRun without recopying."""
        directory = self._request_dir(request_id)
        with self._lock, run_lock(directory):
            receipt = self.read_receipt(request_id)
            if receipt is None or receipt.state != "staged":
                raise RequestStagingError("request_not_staged")
            source = directory / "attachments"
            if not source.is_dir() or source.is_symlink():
                if receipt.attachments:
                    raise RequestStagingError("staged_payload_unavailable")
                return None
            target_dir = Path(run_dir).resolve() / "request-evidence"
            if Path(run_dir).is_symlink() or not Path(run_dir).is_dir():
                raise RequestStagingError("task_run_directory_invalid")
            created = not target_dir.exists()
            target_dir.mkdir(exist_ok=True)
            self._restrict_or_verify(target_dir, created=created)
            target = target_dir / "attachments"
            if target.exists() or target.is_symlink():
                raise RequestStagingError("task_run_staged_payload_conflict")
            try:
                os.replace(source, target)
            except OSError as exc:
                raise RequestStagingError(
                    "task_run_staged_payload_move_failed"
                ) from exc
            return target

    def read_receipt(self, request_id: str) -> RequestReceipt | None:
        directory = self._existing_request_dir(request_id)
        if directory is None:
            return None
        with self._lock:
            path = directory / "receipt.json"
            if not path.exists():
                return None
            receipt = RequestReceipt.from_dict(immutable_read(path))
            versions = directory / "receipt-versions"
            for version_path in (
                sorted(versions.glob("*.json")) if versions.exists() else ()
            ):
                record = immutable_read(version_path)
                prior = record.get("previous_version_sha256")
                updated = RequestReceipt.from_dict(record.get("receipt"))
                expected_prior = hashlib.sha256(encode(receipt.to_dict())).hexdigest()
                if prior != expected_prior or updated.request_id != request_id:
                    raise RequestStagingError("request_receipt_history_invalid")
                receipt = updated
            conflict_dir = directory / "conflicts"
            if conflict_dir.exists():
                receipt = RequestReceipt(
                    receipt.request_id,
                    receipt.request_sha256,
                    receipt.request_identity_sha256,
                    receipt.state,
                    receipt.attachments,
                    receipt.created_at,
                    receipt.task_run_id,
                    receipt.reason,
                    receipt.staging_status,
                    len(tuple(conflict_dir.glob("*.json"))),
                    receipt.request_exit_code,
                )
            return receipt
