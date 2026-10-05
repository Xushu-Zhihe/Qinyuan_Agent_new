"""Trusted D6 sensitive-output boundary and no-body proof projections.

No model action exposes this service. Registered adapters must split declared
sensitive output before constructing any ordinary execution receipt or context.
The service stores only encrypted body plus safe, explicitly owned references.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_adapters.observation_proof_store import FileD3ProofStore
from agent_adapters.protected_evidence import (
    FileProtectedEvidenceAudit,
    ProtectedEvidenceError,
    ProtectedEvidenceStore,
    _uuid4,
)
from agent_adapters.run_files import immutable_read, immutable_write
from agent_adapters.windows_evidence_protection import WindowsEvidenceProtection
from agent_core.control_candidate import validate_control_ownership
from agent_core.sensitive_evidence_contracts import D6_SENSITIVE_KINDS
from agent_core.sqlite_control_store import SqliteControlStore


@dataclass(frozen=True, slots=True)
class D6SensitiveOutputContract:
    """A trusted adapter's declared sensitive output type, frozen at assembly."""

    adapter_id: str
    kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_id, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.adapter_id):
            raise ValueError("d6_sensitive_adapter_id_invalid")
        if self.kind not in D6_SENSITIVE_KINDS:
            raise ValueError("d6_sensitive_kind_invalid")


def _owned_run(run_dir: Path, task_run_id: str) -> None:
    _uuid4(task_run_id, "task_run_id")
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ProtectedEvidenceError("d6_run_directory_invalid")
    database = run_dir / "control.sqlite"
    if database.is_symlink():
        raise ProtectedEvidenceError("d6_evidence_ownership_invalid")
    try:
        with SqliteControlStore.open_readonly(database) as ledger:
            if ledger.list_task_run_refs() != (task_run_id,) or ledger.load_task_run(task_run_id) is None:
                raise ProtectedEvidenceError("d6_evidence_ownership_invalid")
    except ProtectedEvidenceError:
        raise
    except Exception as exc:
        raise ProtectedEvidenceError("d6_evidence_ownership_invalid") from exc


class TrustedSensitiveEvidenceService:
    """Separate sensitive bytes from ordinary state using frozen adapter contracts."""

    def __init__(
        self,
        run_dir: Path,
        task_run_id: str,
        contracts: tuple[D6SensitiveOutputContract, ...] = (),
        *,
        create: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.task_run_id = task_run_id
        _owned_run(self.run_dir, task_run_id)
        if not isinstance(contracts, tuple) or any(not isinstance(item, D6SensitiveOutputContract) for item in contracts):
            raise ProtectedEvidenceError("d6_sensitive_contract_invalid")
        self._contracts = {item.adapter_id: item for item in contracts}
        if len(self._contracts) != len(contracts):
            raise ProtectedEvidenceError("d6_sensitive_contract_invalid")
        evidence_dir = self.run_dir / "sensitive-evidence"
        self.store = ProtectedEvidenceStore(
            self.run_dir, task_run_id, WindowsEvidenceProtection(), clock,
            FileProtectedEvidenceAudit(evidence_dir / "audit.jsonl"), create=create,
        )

    def _owned_references(self, execution_id: str, proof_id: str) -> None:
        _uuid4(execution_id, "execution_id")
        _uuid4(proof_id, "proof_id")
        try:
            with SqliteControlStore.open_readonly(self.run_dir / "control.sqlite") as ledger:
                control = ledger.load_control_state_rows(self.task_run_id)
                if control is None:
                    raise ValueError("missing_run")
                validate_control_ownership(control)
                execution = next((item for item in control.executions if item.execution_id == execution_id), None)
                proof = FileD3ProofStore(self.run_dir / "d3-proofs", create=False).read(proof_id)
                if proof is None or proof.task_run_id != self.task_run_id:
                    raise ValueError("missing_proof")
                if execution is None or execution.plan_id != proof.plan_id or execution_id not in {proof.source_execution_id, proof.size_execution_id}:
                    raise ValueError("execution_proof_link_mismatch")
                if not any(item.plan_id == proof.plan_id and item.intent_id == proof.intent_id for item in control.plans) or not any(item.intent_id == proof.intent_id and item.turn_id == proof.turn_id for item in control.intents):
                    raise ValueError("missing_proof_owner")
        except Exception as exc:
            raise ProtectedEvidenceError("d6_sensitive_reference_ownership_invalid") from exc

    def save_sensitive(
        self,
        *,
        contract: D6SensitiveOutputContract,
        evidence_id: str,
        body: bytes,
        execution_id: str,
        proof_id: str,
    ) -> dict[str, object]:
        registered = self._contracts.get(contract.adapter_id) if isinstance(contract, D6SensitiveOutputContract) else None
        if registered is not contract:
            raise ProtectedEvidenceError("d6_sensitive_adapter_unregistered")
        self._owned_references(execution_id, proof_id)
        metadata = self.store.save(evidence_id, contract.kind, body)
        # Body is already protected before a reference becomes observable.
        reference = {
            "task_run_id": self.task_run_id,
            "evidence_id": evidence_id,
            "execution_id": execution_id,
            "proof_id": proof_id,
        }
        try:
            immutable_write(self.store.evidence_dir / f"{evidence_id}.refs.json", reference)
        except Exception as exc:
            raise ProtectedEvidenceError("d6_sensitive_reference_write_failed") from exc
        return {**reference, "kind": metadata.kind, "body_status": "withheld"}

    def query(self, evidence_id: str) -> dict[str, object]:
        return self.store.query(evidence_id)

    def view(
        self,
        evidence_id: str,
        *,
        validate_body: Callable[[bytes], None] | None = None,
    ) -> bytes:
        return self.store.view(evidence_id, validate_body=validate_body)

    def delete(self, evidence_id: str) -> dict[str, object]:
        self.store.delete(evidence_id)
        return self.store.query(evidence_id)

    def projection(self) -> dict[str, object]:
        metadata = self.store.list_metadata()
        proofs: dict[str, dict[str, Any]] = {}
        for evidence in metadata:
            evidence_id = str(evidence["evidence_id"])
            path = self.store.evidence_dir / f"{evidence_id}.refs.json"
            if not path.exists():
                continue  # interrupted write left an encrypted, unlinked body
            try:
                if path.is_symlink():
                    raise ValueError
                reference = immutable_read(path)
                if not isinstance(reference, dict) or set(reference) != {"task_run_id", "evidence_id", "execution_id", "proof_id"}:
                    raise ValueError
                if reference["task_run_id"] != self.task_run_id or reference["evidence_id"] != evidence_id:
                    raise ValueError
                self._owned_references(reference["execution_id"], reference["proof_id"])
            except Exception as exc:
                raise ProtectedEvidenceError("d6_sensitive_reference_corrupt") from exc
            proof_id = reference["proof_id"]
            row = proofs.setdefault(proof_id, {"proof_id": proof_id, "evidence_ids": [], "recheck_status": "available"})
            row["evidence_ids"].append(evidence_id)
            if evidence["status"] == "deleted":
                row["recheck_status"] = "evidence_deleted_unverifiable"
        return {"sensitive_evidence": list(metadata), "proof_recheck": list(proofs.values())}


def query_sensitive_evidence(run_dir: Path, task_run_id: str) -> dict[str, object]:
    """Read safe metadata without creating directories, audits or locks."""
    if not (Path(run_dir) / "sensitive-evidence").exists():
        return {}
    return TrustedSensitiveEvidenceService(run_dir, task_run_id, create=False).projection()
