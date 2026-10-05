"""Bounded local snapshots and independent cross-TaskRun input copies."""

from __future__ import annotations

import hashlib
import math
import os
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from agent_adapters.run_files import (
    encode,
    immutable_read,
    immutable_write,
    read_json,
    run_lock,
    run_path,
)
from agent_core.d6_contracts import (
    D6_MAX_IMPORT_BYTES,
    D6_MAX_IMPORTS_PER_RUN,
    D6ArtifactImportRequest,
    D6ArtifactReference,
)


class D6ArtifactImportError(RuntimeError):
    """The source, manifest, or destination failed a trusted check."""


class D6ArtifactImportRecoveryRequired(D6ArtifactImportError):
    """A started import has no confirmed result; inspect without redispatch."""


def _is_link(path: Path) -> bool:
    """Include Windows junctions/reparse points, even for dangling links."""
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _check_path(path: Path) -> Path:
    absolute = path.absolute()
    for item in (absolute, *absolute.parents):
        if _is_link(item):
            raise D6ArtifactImportError("d6_artifact_link_forbidden")
    return absolute.resolve()


def _read_bytes(path: Path, expected_size: int, expected_sha: str) -> bytes:
    try:
        _check_path(path)
        if not path.is_file():
            raise D6ArtifactImportError("d6_artifact_unavailable")
        with path.open("rb") as stream:
            content = stream.read(D6_MAX_IMPORT_BYTES + 1)
    except OSError as exc:
        raise D6ArtifactImportError("d6_artifact_unavailable") from exc
    if len(content) > D6_MAX_IMPORT_BYTES or len(content) != expected_size:
        raise D6ArtifactImportError("d6_artifact_size_mismatch")
    if hashlib.sha256(content).hexdigest() != expected_sha:
        raise D6ArtifactImportError("d6_artifact_sha_mismatch")
    return content


@dataclass(frozen=True, slots=True)
class D6ArtifactSnapshot:
    reference: D6ArtifactReference
    snapshot_path: str
    byte_size: int
    created_at: float
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.reference, D6ArtifactReference):
            raise TypeError("reference_invalid")
        source = Path(self.snapshot_path)
        if not source.is_absolute() or _is_link(source) or not source.is_file():
            raise ValueError("source_artifact_invalid")
        if type(self.byte_size) is not int or not 0 <= self.byte_size <= D6_MAX_IMPORT_BYTES:
            raise ValueError("source_artifact_size_invalid")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(self.created_at)
        ):
            raise ValueError("snapshot_time_invalid")
        if type(self.sensitive) is not bool or self.sensitive:
            raise ValueError("d6_sensitive_artifact_import_forbidden")

    @property
    def source_path(self) -> str:
        return self.snapshot_path


@dataclass(frozen=True, slots=True)
class D6ArtifactImportManifest:
    request: D6ArtifactImportRequest
    target_path: str
    source_path: str
    byte_size: int
    sha256: str
    status: Literal["prepared", "confirmed", "unknown"]
    readback_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, D6ArtifactImportRequest):
            raise TypeError("request_invalid")
        if not Path(self.target_path).is_absolute() or not Path(self.source_path).is_absolute():
            raise ValueError("artifact_path_invalid")
        if type(self.byte_size) is not int or not 0 <= self.byte_size <= D6_MAX_IMPORT_BYTES:
            raise ValueError("artifact_size_invalid")
        if self.sha256 != self.request.source.sha256:
            raise ValueError("artifact_sha256_invalid")
        if self.status not in {"prepared", "confirmed", "unknown"}:
            raise ValueError("manifest_status_invalid")
        if self.status == "confirmed" and self.readback_sha256 != self.sha256:
            raise ValueError("artifact_readback_mismatch")
        if self.status != "confirmed" and self.readback_sha256 is not None:
            raise ValueError("unconfirmed_manifest_has_readback")


@dataclass(frozen=True, slots=True)
class D6ArtifactImportInspection:
    """Read-only observation; a match does not change the persisted outcome."""

    manifest: D6ArtifactImportManifest
    target_status: Literal["matching", "missing", "mismatch", "unavailable"]


@dataclass(frozen=True, slots=True)
class D6AttachmentReceiptBinding:
    request: D6ArtifactImportRequest
    attachment_index: int
    receipt_sha256: str
    target_path: str
    artifact_sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.request, D6ArtifactImportRequest):
            raise TypeError("request_invalid")
        if type(self.attachment_index) is not int or self.attachment_index not in {0, 1}:
            raise ValueError("attachment_index_invalid")
        for value in (self.receipt_sha256, self.artifact_sha256):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("attachment_binding_sha256_invalid")
        if not Path(self.target_path).is_absolute():
            raise ValueError("target_path_invalid")
        if type(self.byte_size) is not int or not 0 <= self.byte_size <= D6_MAX_IMPORT_BYTES:
            raise ValueError("attachment_binding_size_invalid")
        if self.artifact_sha256 != self.request.source.sha256:
            raise ValueError("attachment_binding_source_mismatch")


class CrossTaskRunArtifactStore:
    """Owns snapshots/target copies; source receipt validation belongs to transfer."""

    def __init__(self, root: Path) -> None:
        self.root = _check_path(Path(root))
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise D6ArtifactImportError("d6_artifact_root_invalid")

    def _run_path(self, task_run_id: str, *, create: bool) -> Path:
        path = run_path(self.root, task_run_id)
        _check_path(path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot_path(self, reference: D6ArtifactReference, *, create: bool = False) -> Path:
        directory = self._run_path(reference.source_task_run_id, create=create) / "d6-snapshots"
        _check_path(directory)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{reference.object_version_id}-{reference.sha256}.bin"
        _check_path(path)
        return path

    def _manifest_path(self, target_task_run_id: str, import_id: str) -> Path:
        directory = self._run_path(target_task_run_id, create=False) / "d6-imports"
        _check_path(directory)
        return directory / f"{import_id}.json"

    def _attachment_binding_path(self, target_task_run_id: str, import_id: str) -> Path:
        directory = self._run_path(target_task_run_id, create=False) / "d6-imports"
        _check_path(directory)
        return directory / f"{import_id}.attachment.json"

    def _target_path(self, request: D6ArtifactImportRequest) -> Path:
        directory = self._run_path(request.target_task_run_id, create=False) / "d6-inputs"
        _check_path(directory)
        path = directory / f"{request.import_id}.bin"
        _check_path(path)
        return path

    def _validate_manifest_paths(self, manifest: D6ArtifactImportManifest) -> None:
        if (
            _check_path(Path(manifest.source_path)) != self._snapshot_path(manifest.request.source)
            or _check_path(Path(manifest.target_path)) != self._target_path(manifest.request)
        ):
            raise D6ArtifactImportError("d6_import_path_invalid")

    def save_snapshot(
        self, *, reference: D6ArtifactReference, content: bytes, created_at: float,
    ) -> D6ArtifactSnapshot:
        if not isinstance(content, bytes) or len(content) > D6_MAX_IMPORT_BYTES:
            raise D6ArtifactImportError("d6_source_artifact_too_large")
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise D6ArtifactImportError("d6_source_artifact_sha_mismatch")
        path = self._snapshot_path(reference, create=True)
        # Check the clock before writing; a failed declaration must not create a snapshot.
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)) or not math.isfinite(created_at):
            raise D6ArtifactImportError("d6_snapshot_time_invalid")
        temporary = path.parent / f".d6-snapshot-{uuid4()}"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if _read_bytes(path, len(content), reference.sha256) != content:
                    raise D6ArtifactImportError("d6_snapshot_conflict") from None
        except OSError as exc:
            raise D6ArtifactImportError("d6_snapshot_unavailable") from exc
        finally:
            temporary.unlink(missing_ok=True)
        snapshot = D6ArtifactSnapshot(reference, str(path), len(content), created_at)
        return snapshot

    def verify_snapshot(self, snapshot: D6ArtifactSnapshot) -> None:
        if not isinstance(snapshot, D6ArtifactSnapshot):
            raise D6ArtifactImportError("d6_snapshot_invalid")
        if _check_path(Path(snapshot.snapshot_path)) != self._snapshot_path(snapshot.reference):
            raise D6ArtifactImportError("d6_snapshot_path_invalid")
        _read_bytes(Path(snapshot.snapshot_path), snapshot.byte_size, snapshot.reference.sha256)

    def export_snapshot(
        self, *, source_task_run_id: str, object_version_id: str, source_path: Path,
        expected_sha256: str, created_at: float,
    ) -> D6ArtifactSnapshot:
        """Low-level local helper; application import requires a transfer declaration."""
        source_path = _check_path(Path(source_path))
        try:
            size = source_path.stat().st_size
        except OSError as exc:
            raise D6ArtifactImportError("d6_source_artifact_unavailable") from exc
        content = _read_bytes(source_path, size, expected_sha256)
        reference = D6ArtifactReference(source_task_run_id, object_version_id, expected_sha256)
        return self.save_snapshot(reference=reference, content=content, created_at=created_at)

    def _read_manifest(self, path: Path) -> D6ArtifactImportManifest | None:
        _check_path(path)
        if not path.exists():
            return None
        try:
            value = read_json(path)
            manifest = D6ArtifactImportManifest(
                D6ArtifactImportRequest(
                    value["request"]["import_id"],
                    D6ArtifactReference(**value["request"]["source"]),
                    value["request"]["target_task_run_id"],
                    value["request"]["non_sensitive"],
                ),
                value["target_path"], value["source_path"], value["byte_size"],
                value["sha256"], value["status"], value.get("readback_sha256"),
            )
            self._validate_manifest_paths(manifest)
            if self._manifest_path(manifest.request.target_task_run_id, manifest.request.import_id) != path:
                raise ValueError("manifest_owner_mismatch")
            return manifest
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise D6ArtifactImportError("d6_import_manifest_corrupt") from exc

    def prepare_import(
        self, *, request: D6ArtifactImportRequest, snapshot: D6ArtifactSnapshot,
        target_path: Path | None = None, existing_import_count: int,
    ) -> D6ArtifactImportManifest:
        if snapshot.reference != request.source or snapshot.sensitive:
            raise D6ArtifactImportError("d6_source_binding_mismatch")
        if type(existing_import_count) is not int or existing_import_count < 0:
            raise D6ArtifactImportError("d6_import_count_invalid")
        target = self._target_path(request)
        if target_path is not None and _check_path(Path(target_path)) != target:
            raise D6ArtifactImportError("d6_target_path_invalid")
        directory = self._run_path(request.target_task_run_id, create=True) / "d6-imports"
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = self._manifest_path(request.target_task_run_id, request.import_id)
        with run_lock(directory):
            previous = self._read_manifest(manifest_path)
            if previous is not None:
                if previous.request != request or previous.byte_size != snapshot.byte_size:
                    raise D6ArtifactImportError("d6_import_conflict")
                if previous.status != "confirmed":
                    raise D6ArtifactImportRecoveryRequired("d6_import_outcome_unknown")
                _read_bytes(target, previous.byte_size, previous.sha256)
                return previous
            manifest_count = sum(
                not item.name.endswith(".attachment.json")
                for item in directory.glob("*.json")
            )
            if max(existing_import_count, manifest_count) >= D6_MAX_IMPORTS_PER_RUN:
                raise D6ArtifactImportError("d6_import_limit_exceeded")
            self.verify_snapshot(snapshot)
            if target.exists() or _is_link(target):
                raise D6ArtifactImportError("d6_target_artifact_exists")
            manifest = D6ArtifactImportManifest(
                request, str(target), snapshot.source_path, snapshot.byte_size,
                request.source.sha256, "prepared",
            )
            self._write_manifest(manifest_path, manifest)
            return manifest

    def copy_and_confirm(self, manifest: D6ArtifactImportManifest) -> D6ArtifactImportManifest:
        self._validate_manifest_paths(manifest)
        path = self._manifest_path(manifest.request.target_task_run_id, manifest.request.import_id)
        with run_lock(path.parent):
            current = self._read_manifest(path)
            if current is None or current.request != manifest.request or current.byte_size != manifest.byte_size:
                raise D6ArtifactImportRecoveryRequired("d6_import_manifest_changed")
            if current.status == "confirmed":
                _read_bytes(Path(current.target_path), current.byte_size, current.sha256)
                return current
            if manifest.status != "prepared" or current != manifest:
                raise D6ArtifactImportRecoveryRequired("d6_import_outcome_unknown")
            # Persist dispatch admission before reading/copying. A crash from this
            # point onwards cannot authorize a second copy, even with the old object.
            self._write_manifest(path, replace(current, status="unknown"))
        target = Path(manifest.target_path)
        temporary = target.parent / f".d6-import-{uuid4()}"
        try:
            content = _read_bytes(Path(manifest.source_path), manifest.byte_size, manifest.sha256)
            target.parent.mkdir(parents=True, exist_ok=True)
            _check_path(target.parent)
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
            _read_bytes(target, manifest.byte_size, manifest.sha256)
            confirmed = replace(manifest, status="confirmed", readback_sha256=manifest.sha256)
            self._write_manifest(path, confirmed)
            return confirmed
        except (OSError, TimeoutError, D6ArtifactImportError) as exc:
            raise D6ArtifactImportRecoveryRequired("d6_import_outcome_unknown") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def inspect_import(self, request: D6ArtifactImportRequest) -> D6ArtifactImportInspection:
        """Observe the saved target only; never copy or change ledger state."""
        manifest = self._read_manifest(self._manifest_path(request.target_task_run_id, request.import_id))
        if manifest is None or manifest.request != request:
            raise D6ArtifactImportError("d6_import_manifest_unavailable")
        target = Path(manifest.target_path)
        status: Literal["matching", "missing", "mismatch", "unavailable"]
        if not target.exists():
            status = "missing"
        else:
            try:
                _read_bytes(target, manifest.byte_size, manifest.sha256)
                status = "matching"
            except D6ArtifactImportError as exc:
                status = "mismatch" if str(exc) in {"d6_artifact_size_mismatch", "d6_artifact_sha_mismatch"} else "unavailable"
        return D6ArtifactImportInspection(manifest, status)

    def find_import(
        self, target_task_run_id: str, source: D6ArtifactReference,
    ) -> D6ArtifactImportManifest:
        """Find exactly one import request for this frozen target/source pair."""
        directory = self._run_path(target_task_run_id, create=False) / "d6-imports"
        _check_path(directory)
        if not directory.is_dir():
            raise D6ArtifactImportError("d6_import_manifest_unavailable")
        matches = []
        try:
            for path in sorted(directory.glob("*.json")):
                if path.name.endswith(".attachment.json"):
                    continue
                manifest = self._read_manifest(path)
                if manifest is not None and manifest.request.source == source:
                    matches.append(manifest)
        except OSError as exc:
            raise D6ArtifactImportError("d6_import_manifest_unavailable") from exc
        if len(matches) != 1:
            code = "d6_import_manifest_unavailable" if not matches else "d6_import_manifest_ambiguous"
            raise D6ArtifactImportError(code)
        manifest = matches[0]
        if manifest.request.target_task_run_id != target_task_run_id:
            raise D6ArtifactImportError("d6_import_manifest_owner_mismatch")
        return manifest

    def bind_attachment_receipt(
        self, *, request: D6ArtifactImportRequest, attachment_index: int,
        receipt_sha256: str,
    ) -> D6AttachmentReceiptBinding:
        """Persist an immutable link from D3's actual import receipt to D6 source."""
        inspection = self.inspect_import(request)
        manifest = inspection.manifest
        if manifest.status != "confirmed" or inspection.target_status != "matching":
            raise D6ArtifactImportError("d6_confirmed_target_copy_required")
        binding = D6AttachmentReceiptBinding(
            request, attachment_index, receipt_sha256, manifest.target_path,
            manifest.sha256, manifest.byte_size,
        )
        path = self._attachment_binding_path(
            request.target_task_run_id, request.import_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with run_lock(path.parent):
                immutable_write(path, asdict(binding))
        except (OSError, ValueError) as exc:
            raise D6ArtifactImportError("d6_attachment_receipt_binding_unavailable") from exc
        return binding

    def verify_attachment_receipt(
        self, *, request: D6ArtifactImportRequest, attachment_index: int,
        receipt_sha256: str,
    ) -> D6AttachmentReceiptBinding:
        inspection = self.inspect_import(request)
        manifest = inspection.manifest
        if manifest.status != "confirmed" or inspection.target_status != "matching":
            raise D6ArtifactImportError("d6_confirmed_target_copy_required")
        path = self._attachment_binding_path(
            request.target_task_run_id, request.import_id,
        )
        try:
            value = immutable_read(path)
            binding = D6AttachmentReceiptBinding(
                D6ArtifactImportRequest(
                    value["request"]["import_id"],
                    D6ArtifactReference(**value["request"]["source"]),
                    value["request"]["target_task_run_id"],
                    value["request"]["non_sensitive"],
                ),
                value["attachment_index"], value["receipt_sha256"],
                value["target_path"], value["artifact_sha256"], value["byte_size"],
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise D6ArtifactImportError("d6_attachment_receipt_binding_unavailable") from exc
        expected = D6AttachmentReceiptBinding(
            request, attachment_index, receipt_sha256, manifest.target_path,
            manifest.sha256, manifest.byte_size,
        )
        if binding != expected:
            raise D6ArtifactImportError("d6_attachment_receipt_binding_changed")
        return binding

    @staticmethod
    def _write_manifest(path: Path, manifest: D6ArtifactImportManifest) -> None:
        payload = encode(asdict(manifest))
        temporary = path.parent / f".pending-{uuid4()}"
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
