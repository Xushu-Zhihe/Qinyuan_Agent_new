"""Immutable workspace provenance, observations, scoped stops, and shares."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder
from agent_core.artifact_workspace import (
    D3ArtifactClaim,
    D3ArtifactReceipt,
    D3DigestClaim,
    D3DigestReceipt,
    D3ReadbackAudit,
    D3ShareClaim,
    D3ShareReceipt,
    D3SizeObservation,
    D3StopClaim,
    D3StopReadback,
    D3StopReceipt,
)
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.attachment_workspace import (
    D1CopyClaim,
    D1CopyReceipt,
    D1DigestClaim,
    D1DigestReceipt,
    D1ProofClaim,
)
from agent_core.command_contracts import T6EvidenceError
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.intent_postconditions import FileDigestObservation


class FileD1WorkspaceRecorder:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self._records = T6CommandRecorder(directory, create=create)

    @property
    def directory(self) -> Path:
        return self._records.directory

    def begin_proof(self, claim: D1ProofClaim) -> None:
        self._records._write("d1-proof-claim", claim.proof_id, asdict(claim))

    def read_proof_claim(self, proof_id: str) -> D1ProofClaim | None:
        value = self._records._read("d1-proof-claim", proof_id)
        if value is None:
            return None
        try:
            claim = D1ProofClaim(**value)
            if claim.proof_id != proof_id:
                raise ValueError("d1_proof_reference_mismatch")
            return claim
        except (TypeError, ValueError, AttributeError) as exc:
            raise T6EvidenceError("invalid_d1_proof_claim") from exc

    def begin_copy(self, claim: D1CopyClaim) -> None:
        self._records._write("d1-copy-claim", claim.preparation_ref, asdict(claim))

    def read_copy_claim(self, reference: str) -> D1CopyClaim | None:
        value = self._records._read("d1-copy-claim", reference)
        if value is None:
            return None
        try:
            claim = D1CopyClaim(**value)
            if claim.preparation_ref != reference:
                raise ValueError("d1_copy_reference_mismatch")
            return claim
        except (TypeError, ValueError, AttributeError) as exc:
            raise T6EvidenceError("invalid_d1_copy_claim") from exc

    def save_copy(self, receipt: D1CopyReceipt) -> None:
        if self.read_copy_claim(receipt.claim.preparation_ref) != receipt.claim:
            raise T6EvidenceError("d1_copy_receipt_without_claim")
        self._records._write("d1-copy", receipt.claim.preparation_ref, asdict(receipt))

    def read_copy(self, reference: str) -> D1CopyReceipt | None:
        value = self._records._read("d1-copy", reference)
        if value is None:
            return None
        try:
            receipt = D1CopyReceipt(
                D1CopyClaim(**value["claim"]), value["sha256"], value["completed_at"]
            )
            if self.read_copy_claim(reference) != receipt.claim:
                raise ValueError("d1_copy_receipt_without_claim")
            return receipt
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise T6EvidenceError("invalid_d1_copy_receipt") from exc

    def begin_digest(self, claim: D1DigestClaim) -> None:
        self._records._write("d1-digest-claim", claim.operation_id, asdict(claim))

    def read_digest_claim(self, reference: str) -> D1DigestClaim | None:
        value = self._records._read("d1-digest-claim", reference)
        if value is None:
            return None
        try:
            claim = D1DigestClaim(**value)
            if claim.operation_id != reference:
                raise ValueError("d1_digest_reference_mismatch")
            return claim
        except (TypeError, ValueError, AttributeError) as exc:
            raise T6EvidenceError("invalid_d1_digest_claim") from exc

    def save_digest(self, receipt: D1DigestReceipt) -> None:
        if self.read_digest_claim(receipt.claim.operation_id) != receipt.claim:
            raise T6EvidenceError("d1_digest_receipt_without_claim")
        self._records._write("d1-digest", receipt.claim.operation_id, asdict(receipt))

    def read_digest(self, reference: str) -> D1DigestReceipt | None:
        value = self._records._read("d1-digest", reference)
        if value is None:
            return None
        try:
            receipt = D1DigestReceipt(
                D1DigestClaim(**value["claim"]),
                FileDigestObservation(**value["observed"]),
                value["completed_at"],
            )
            if self.read_digest_claim(reference) != receipt.claim:
                raise ValueError("d1_digest_receipt_without_claim")
            return receipt
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise T6EvidenceError("invalid_d1_digest_receipt") from exc


class FileD3WorkspaceEvidence:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.records = T6CommandRecorder(directory, create=create)

    def begin_artifact(self, claim: D3ArtifactClaim) -> None:
        self.records._write(
            "d3-artifact-claim", claim.artifact_version_id, asdict(claim)
        )

    def read_artifact_claim(self, artifact_version_id: str) -> D3ArtifactClaim | None:
        try:
            payload = self.records._read("d3-artifact-claim", artifact_version_id)
            if payload is None:
                return None
            claim = D3ArtifactClaim(**payload)
            if claim.artifact_version_id != artifact_version_id:
                raise ValueError("D3 artifact claim owner mismatch")
            return claim
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_artifact_claim_unavailable") from exc

    def save_artifact(self, receipt: D3ArtifactReceipt) -> None:
        if self.read_artifact_claim(receipt.claim.artifact_version_id) != receipt.claim:
            raise ExecutionRecoveryRequired("d3_artifact_receipt_without_claim")
        self.records._write(
            "d3-artifact", receipt.claim.artifact_version_id, asdict(receipt)
        )

    def read_artifact(self, artifact_version_id: str) -> D3ArtifactReceipt | None:
        try:
            payload = self.records._read("d3-artifact", artifact_version_id)
            if payload is None:
                return None
            receipt = D3ArtifactReceipt(
                D3ArtifactClaim(**payload["claim"]),
                AttachmentImportReadback(**payload["readback"]),
                payload["status"],
                payload["completed_at"],
            )
            if (
                receipt.claim.artifact_version_id != artifact_version_id
                or self.read_artifact_claim(artifact_version_id) != receipt.claim
            ):
                raise ValueError("D3 artifact receipt owner mismatch")
            return receipt
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_artifact_receipt_unavailable") from exc

    def list_artifacts(self, task_run_id: str) -> tuple[D3ArtifactReceipt, ...]:
        receipts = []
        for path in self.records.directory.glob("d3-artifact-claim-*.json"):
            version_id = path.name.removeprefix("d3-artifact-claim-").removesuffix(
                ".json"
            )
            claim = self.read_artifact_claim(version_id)
            if claim is None:
                raise ExecutionRecoveryRequired("d3_artifact_claim_disappeared")
            if claim.task_run_id != task_run_id:
                continue
            receipt = self.read_artifact(version_id)
            if receipt is not None:
                receipts.append(receipt)
        return tuple(sorted(receipts, key=lambda item: item.claim.artifact_version_id))

    def save_size(self, observation: D3SizeObservation) -> None:
        self.records._write("d3-size", observation.execution_id, asdict(observation))

    def read_size(self, execution_id: str) -> D3SizeObservation | None:
        try:
            payload = self.records._read("d3-size", execution_id)
            if payload is None:
                return None
            observation = D3SizeObservation(**payload)
            if observation.execution_id != execution_id:
                raise ValueError("D3 size observation owner mismatch")
            return observation
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_size_observation_unavailable") from exc

    def begin_digest(self, claim: D3DigestClaim) -> None:
        self.records._write("d3-digest-claim", claim.observation_id, asdict(claim))

    def read_digest_claim(self, observation_id: str) -> D3DigestClaim | None:
        try:
            payload = self.records._read("d3-digest-claim", observation_id)
            if payload is None:
                return None
            claim = D3DigestClaim(**payload)
            if claim.observation_id != observation_id:
                raise ValueError("D3 digest claim owner mismatch")
            return claim
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_digest_claim_unavailable") from exc

    def save_digest(self, receipt: D3DigestReceipt) -> None:
        if self.read_digest_claim(receipt.claim.observation_id) != receipt.claim:
            raise ExecutionRecoveryRequired("d3_digest_receipt_without_claim")
        self.records._write("d3-digest", receipt.claim.observation_id, asdict(receipt))

    def read_digest(self, observation_id: str) -> D3DigestReceipt | None:
        try:
            payload = self.records._read("d3-digest", observation_id)
            if payload is None:
                return None
            receipt = D3DigestReceipt(
                D3DigestClaim(**payload["claim"]),
                FileDigestObservation(**payload["observed"]),
                payload["status"],
                payload["completed_at"],
            )
            if (
                receipt.claim.observation_id != observation_id
                or self.read_digest_claim(observation_id) != receipt.claim
            ):
                raise ValueError("D3 digest receipt owner mismatch")
            return receipt
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_digest_receipt_unavailable") from exc

    def begin_share(self, claim: D3ShareClaim) -> None:
        for path in self.records.directory.glob("d3-share-claim-*.json"):
            other_id = path.name.removeprefix("d3-share-claim-").removesuffix(".json")
            other = self.read_share_claim(other_id)
            if other is None:
                raise ExecutionRecoveryRequired("d3_share_claim_disappeared")
            if (
                other.task_run_id,
                other.artifact_version_id,
                other.target_intent_id,
            ) == (
                claim.task_run_id,
                claim.artifact_version_id,
                claim.target_intent_id,
            ) and other.share_id != claim.share_id:
                raise ExecutionRecoveryRequired("d3_share_pair_already_claimed")
        self.records._write("d3-share-claim", claim.share_id, asdict(claim))

    def read_share_claim(self, share_id: str) -> D3ShareClaim | None:
        try:
            payload = self.records._read("d3-share-claim", share_id)
            if payload is None:
                return None
            claim = D3ShareClaim(**payload)
            if claim.share_id != share_id:
                raise ValueError("D3 share claim owner mismatch")
            return claim
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_share_claim_unavailable") from exc

    def save_share(self, receipt: D3ShareReceipt) -> None:
        if self.read_share_claim(receipt.claim.share_id) != receipt.claim:
            raise ExecutionRecoveryRequired("d3_share_receipt_without_claim")
        self.records._write("d3-share", receipt.claim.share_id, asdict(receipt))

    def read_share(self, share_id: str) -> D3ShareReceipt | None:
        try:
            payload = self.records._read("d3-share", share_id)
            if payload is None:
                return None
            receipt = D3ShareReceipt(
                D3ShareClaim(**payload["claim"]),
                AttachmentImportReadback(**payload["source"]),
                AttachmentImportReadback(**payload["target"]),
                payload["status"],
                payload["completed_at"],
            )
            if (
                receipt.claim.share_id != share_id
                or self.read_share_claim(share_id) != receipt.claim
            ):
                raise ValueError("D3 share receipt owner mismatch")
            return receipt
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_share_receipt_unavailable") from exc

    def list_shares(self, task_run_id: str) -> tuple[D3ShareReceipt, ...]:
        receipts = []
        for path in self.records.directory.glob("d3-share-claim-*.json"):
            share_id = path.name.removeprefix("d3-share-claim-").removesuffix(".json")
            claim = self.read_share_claim(share_id)
            if claim is None:
                raise ExecutionRecoveryRequired("d3_share_claim_disappeared")
            if claim.task_run_id != task_run_id:
                continue
            receipt = self.read_share(share_id)
            if receipt is not None:
                receipts.append(receipt)
        return tuple(sorted(receipts, key=lambda item: item.claim.share_id))

    def begin_stop(self, claim: D3StopClaim) -> None:
        self.records._write("d3-stop-claim", claim.stop_id, asdict(claim))

    def read_stop_claim(self, stop_id: str) -> D3StopClaim | None:
        try:
            payload = self.records._read("d3-stop-claim", stop_id)
            if payload is None:
                return None
            payload["process_refs"] = tuple(payload["process_refs"])
            claim = D3StopClaim(**payload)
            if claim.stop_id != stop_id:
                raise ValueError("D3 stop claim owner mismatch")
            return claim
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_stop_claim_unavailable") from exc

    def save_stop(self, receipt: D3StopReceipt) -> None:
        if self.read_stop_claim(receipt.claim.stop_id) != receipt.claim:
            raise ExecutionRecoveryRequired("d3_stop_receipt_without_claim")
        self.records._write("d3-stop", receipt.claim.stop_id, asdict(receipt))

    def read_stop(self, stop_id: str) -> D3StopReceipt | None:
        try:
            payload = self.records._read("d3-stop", stop_id)
            if payload is None:
                return None
            claim = dict(payload["claim"])
            claim["process_refs"] = tuple(claim["process_refs"])
            readback = dict(payload["readback"])
            for name in ("stopped_refs", "absent_refs", "unknown_refs"):
                readback[name] = tuple(readback[name])
            receipt = D3StopReceipt(
                D3StopClaim(**claim),
                D3StopReadback(**readback),
                payload["status"],
                payload["completed_at"],
            )
            if (
                receipt.claim.stop_id != stop_id
                or self.read_stop_claim(stop_id) != receipt.claim
            ):
                raise ValueError("D3 stop receipt owner mismatch")
            return receipt
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_stop_receipt_unavailable") from exc

    def save_readback_audit(self, audit: D3ReadbackAudit) -> None:
        self.records._write("d3-readback-audit", audit.audit_id, asdict(audit))

    def read_readback_audit(self, audit_id: str) -> D3ReadbackAudit | None:
        try:
            payload = self.records._read("d3-readback-audit", audit_id)
            if payload is None:
                return None
            if payload["source"] is not None:
                payload["source"] = AttachmentImportReadback(**payload["source"])
            if payload["target"] is not None:
                payload["target"] = AttachmentImportReadback(**payload["target"])
            if payload["process_readback"] is not None:
                readback = payload["process_readback"]
                for name in ("stopped_refs", "absent_refs", "unknown_refs"):
                    readback[name] = tuple(readback[name])
                payload["process_readback"] = D3StopReadback(**readback)
            audit = D3ReadbackAudit(**payload)
            if audit.audit_id != audit_id:
                raise ValueError("D3 readback audit owner mismatch")
            return audit
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_readback_audit_unavailable") from exc
