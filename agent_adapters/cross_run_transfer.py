"""Read-only source receipt validation and explicit non-sensitive capture."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from agent_adapters.cross_run_artifacts import (
    CrossTaskRunArtifactStore,
    D6ArtifactImportError,
    D6ArtifactImportManifest,
    D6ArtifactImportRecoveryRequired,
    D6ArtifactSnapshot,
    _check_path,
)
from agent_adapters.run_files import immutable_read, immutable_write, run_lock
from agent_adapters.workspace_evidence import FileD3WorkspaceEvidence
from agent_core.control_candidate import validate_control_ownership
from agent_core.d6_contracts import (
    D6_MAX_IMPORT_BYTES,
    D6ArtifactImportRequest,
    D6ArtifactReference,
)
from agent_core.modeling import CancellationToken
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_modeling import digest


class D6ArtifactReader(Protocol):
    """Trusted adapter reads only the committed path, bounded before returning."""

    async def read_artifact_bytes(
        self, sandbox_id: str, path: str, max_bytes: int,
        timeout_seconds: float, token: CancellationToken,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class D6SnapshotDeclaration:
    """Source references are provenance only, never imported conclusions."""

    source: D6ArtifactReference
    classification: str
    source_receipt_sha256: str
    source_path: str
    source_sandbox_id: str
    source_fact_refs: tuple[str, ...]
    source_proof_refs: tuple[str, ...]
    source_process_fact_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, D6ArtifactReference) or self.classification != "non_sensitive":
            raise ValueError("d6_non_sensitive_declaration_required")
        if (
            not isinstance(self.source_receipt_sha256, str)
            or len(self.source_receipt_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.source_receipt_sha256)
        ):
            raise ValueError("d6_source_receipt_digest_invalid")
        if not isinstance(self.source_path, str) or not self.source_path.startswith("/workspace/work/intents/"):
            raise ValueError("d6_source_path_invalid")
        if not isinstance(self.source_sandbox_id, str) or not self.source_sandbox_id:
            raise ValueError("d6_source_sandbox_invalid")
        for references in (self.source_fact_refs, self.source_proof_refs, self.source_process_fact_refs):
            if not isinstance(references, tuple) or any(not isinstance(item, str) for item in references):
                raise ValueError("d6_source_provenance_invalid")


class D6ArtifactTransfer:
    """Owns classification/capture records outside the source run directory."""

    def __init__(self, snapshots: CrossTaskRunArtifactStore, clock: Callable[[], float]) -> None:
        self.snapshots = snapshots
        self.clock = clock

    def _declaration_path(self, reference: D6ArtifactReference) -> Path:
        directory = self.snapshots.root / "classifications"
        _check_path(directory)
        return directory / (
            f"{reference.source_task_run_id}-{reference.object_version_id}-{reference.sha256}.json"
        )

    @staticmethod
    def _read_declaration(path: Path) -> D6SnapshotDeclaration:
        try:
            _check_path(path)
            value = immutable_read(path)
            return D6SnapshotDeclaration(
                D6ArtifactReference(**value["source"]), value["classification"],
                value["source_receipt_sha256"], value["source_path"], value["source_sandbox_id"],
                tuple(value["source_fact_refs"]), tuple(value["source_proof_refs"]),
                tuple(value["source_process_fact_refs"]),
            )
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise D6ArtifactImportError("d6_source_classification_unavailable") from exc

    def verify_declared_snapshot(self, snapshot: D6ArtifactSnapshot) -> D6SnapshotDeclaration:
        """Require the trusted declaration and completed capture before importing."""
        path = self._declaration_path(snapshot.reference)
        declaration = self._read_declaration(path)
        if declaration.source != snapshot.reference:
            raise D6ArtifactImportError("d6_source_classification_changed")
        completed_path = path.with_suffix(".snapshot.json")
        try:
            _check_path(completed_path)
            value = immutable_read(completed_path)
            expected = {
                "source_path": snapshot.source_path, "byte_size": snapshot.byte_size,
                "created_at": snapshot.created_at,
                "declaration_sha256": digest(asdict(declaration)),
            }
            if value != expected:
                raise ValueError("d6_capture_binding_mismatch")
        except FileNotFoundError as exc:
            raise D6ArtifactImportRecoveryRequired("d6_source_read_outcome_unknown") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise D6ArtifactImportError("d6_source_capture_unavailable") from exc
        self.snapshots.verify_snapshot(snapshot)
        return declaration

    def verify_confirmed_import(
        self, request: D6ArtifactImportRequest,
    ) -> tuple[D6ArtifactImportManifest, D6SnapshotDeclaration]:
        """Verify the persisted user classification, source snapshot and target copy."""
        inspection = self.snapshots.inspect_import(request)
        manifest = inspection.manifest
        if manifest.request != request or manifest.status != "confirmed" or inspection.target_status != "matching":
            raise D6ArtifactImportError("d6_confirmed_target_copy_required")
        declaration_path = self._declaration_path(request.source)
        completed_path = declaration_path.with_suffix(".snapshot.json")
        try:
            _check_path(completed_path)
            completed = immutable_read(completed_path)
            snapshot = D6ArtifactSnapshot(
                request.source, completed["source_path"], completed["byte_size"],
                completed["created_at"],
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise D6ArtifactImportError("d6_source_capture_unavailable") from exc
        declaration = self.verify_declared_snapshot(snapshot)
        if (
            manifest.source_path != snapshot.source_path
            or manifest.byte_size != snapshot.byte_size
            or manifest.sha256 != request.source.sha256
            or declaration.source != request.source
            or declaration.classification != "non_sensitive"
        ):
            raise D6ArtifactImportError("d6_import_source_binding_changed")
        return manifest, declaration

    @staticmethod
    def _source_declaration(
        source_directory: Path, reference: D6ArtifactReference,
    ) -> tuple[D6SnapshotDeclaration, int]:
        source_directory = _check_path(source_directory)
        _check_path(source_directory / "control.sqlite")
        _check_path(source_directory / "d3-workspace")
        # mode=ro: no migrations, ownership updates or source control mutations.
        with SqliteControlStore.open_readonly(source_directory / "control.sqlite") as store:
            if store.list_task_run_refs() != (reference.source_task_run_id,):
                raise D6ArtifactImportError("d6_source_run_owner_mismatch")
            state = store.load_control_state_rows(reference.source_task_run_id)
            if state is None:
                raise D6ArtifactImportError("d6_source_run_unavailable")
            validate_control_ownership(state)
            receipt = FileD3WorkspaceEvidence(
                source_directory / "d3-workspace", create=False,
            ).read_artifact(reference.object_version_id)
            operation = store.load_d3_operation(reference.object_version_id)
            if (
                receipt is None or receipt.status != "confirmed"
                or receipt.claim.task_run_id != reference.source_task_run_id
                or receipt.claim.expected_sha256 != reference.sha256
                or receipt.claim.sandbox_id != state.task_run.sandbox_id
                or receipt.readback.byte_size > D6_MAX_IMPORT_BYTES
                or operation is None or operation.start.category != "copy_preparation"
                or operation.start.operation_id != reference.object_version_id
                or operation.start.task_run_id != reference.source_task_run_id
                or operation.start.resource_ref != reference.object_version_id
                or operation.start.context_sha256 != digest(asdict(receipt.claim))
                or operation.completion is None
                or operation.completion.result_kind != "completed"
                or operation.completion.evidence_ref != reference.object_version_id
                or operation.completion.evidence_sha256 != digest(asdict(receipt))
                or operation.application_ref != reference.object_version_id
            ):
                raise D6ArtifactImportError("d6_source_success_receipt_required")
            binding = store.load_d3_plan_binding(receipt.claim.plan_id)
            if (
                binding is None or binding.artifact_version_id != reference.object_version_id
                or binding.task_run_id != reference.source_task_run_id
                or binding.turn_id != receipt.claim.turn_id
                or binding.intent_id != receipt.claim.intent_id
                or not any(plan.plan_id == binding.plan_id and plan.intent_id == binding.intent_id for plan in state.plans)
            ):
                raise D6ArtifactImportError("d6_source_version_binding_mismatch")
            result = store.load_d3_plan_result(binding.plan_id)
            proof_refs = (result.proof_id,) if result is not None and result.proof_id else ()
            fact_refs = (result.supporting_fact_id,) if result is not None and result.supporting_fact_id else ()
            process_refs = store.list_process_fact_refs(reference.source_task_run_id)
            return D6SnapshotDeclaration(
                reference, "non_sensitive", digest(asdict(receipt)),
                receipt.claim.path, receipt.claim.sandbox_id, fact_refs, proof_refs, process_refs,
            ), receipt.readback.byte_size

    async def snapshot_from_run(
        self, source_directory: Path, reference: D6ArtifactReference, *,
        classification: str, reader: D6ArtifactReader, token: CancellationToken,
    ) -> D6ArtifactSnapshot:
        if classification != "non_sensitive":
            raise D6ArtifactImportError("d6_non_sensitive_declaration_required")
        if token.is_cancelled:
            raise D6ArtifactImportError("d6_snapshot_cancelled")
        declaration_path = self._declaration_path(reference)
        # Capture metadata must be outside source SQLite/evidence namespace.
        source_root = _check_path(source_directory)
        snapshot_directory = _check_path(
            self.snapshots._snapshot_path(reference).parent
        )
        if (
            snapshot_directory == source_root
            or snapshot_directory.is_relative_to(source_root)
            or source_root.is_relative_to(snapshot_directory)
        ):
            raise D6ArtifactImportError("d6_snapshot_store_must_be_external")
        declaration, expected_size = self._source_declaration(source_directory, reference)
        completed_path = declaration_path.with_suffix(".snapshot.json")
        declaration_path.parent.mkdir(parents=True, exist_ok=True)
        with run_lock(declaration_path.parent):
            if declaration_path.exists():
                saved_declaration = self._read_declaration(declaration_path)
                # Source summary references may grow after capture. The artifact
                # identity, successful receipt, fixed path and classification cannot.
                if (
                    saved_declaration.source != declaration.source
                    or saved_declaration.source_receipt_sha256 != declaration.source_receipt_sha256
                    or saved_declaration.source_path != declaration.source_path
                    or saved_declaration.source_sandbox_id != declaration.source_sandbox_id
                ):
                    raise D6ArtifactImportError("d6_source_classification_changed")
                if not completed_path.exists():
                    raise D6ArtifactImportRecoveryRequired("d6_source_read_outcome_unknown")
                saved = immutable_read(completed_path)
                snapshot = D6ArtifactSnapshot(
                    reference, saved["source_path"], saved["byte_size"], saved["created_at"],
                )
                self.verify_declared_snapshot(snapshot)
                return snapshot
            immutable_write(declaration_path, asdict(declaration))
        # The immutable declaration admits exactly one source body read. Timeout,
        # cancellation or process loss leaves it unresolved, never eligible to reread.
        try:
            content = await asyncio.wait_for(reader.read_artifact_bytes(
                declaration.source_sandbox_id, declaration.source_path, D6_MAX_IMPORT_BYTES,
                60.0, token,
            ), timeout=60.0)
        except (Exception, asyncio.CancelledError) as exc:
            raise D6ArtifactImportRecoveryRequired("d6_source_read_outcome_unknown") from exc
        if not isinstance(content, bytes) or len(content) != expected_size:
            raise D6ArtifactImportError("d6_source_readback_size_mismatch")
        if token.is_cancelled:
            raise D6ArtifactImportRecoveryRequired("d6_source_read_outcome_unknown")
        try:
            snapshot = self.snapshots.save_snapshot(reference=reference, content=content, created_at=self.clock())
            immutable_write(completed_path, {
                "source_path": snapshot.source_path, "byte_size": snapshot.byte_size,
                "created_at": snapshot.created_at,
                "declaration_sha256": digest(asdict(declaration)),
            })
        except (OSError, ValueError, D6ArtifactImportError) as exc:
            raise D6ArtifactImportRecoveryRequired("d6_source_read_outcome_unknown") from exc
        return snapshot
