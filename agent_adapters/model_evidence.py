"""Immutable structured model proposals and trusted rejection evidence."""

from dataclasses import asdict
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder
from agent_core.command_contracts import T6EvidenceError
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.dual_llm_modeling import D3ModelEvidence, D3ModelRejection
from agent_core.turn_contracts import D1RecoveryRequired
from agent_core.turn_modeling import (
    D1ApplicationRejection,
    D1ModelEnvelope,
    D1ModelEvidence,
    digest,
)


class D1ModelEvidenceStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.records = T6CommandRecorder(directory, create=create)

    def save(self, evidence: D1ModelEvidence) -> str:
        payload = asdict(evidence)
        self.records._write("d1-model", evidence.model_request_id, payload)
        return digest(payload)

    def read(self, model_request_id: str) -> D1ModelEvidence | None:
        try:
            payload = self.records._read("d1-model", model_request_id)
            if payload is None:
                return None
            value = D1ModelEvidence(
                **{
                    **payload,
                    "envelope": D1ModelEnvelope(**payload["envelope"]),
                }
            )
            if value.model_request_id != model_request_id:
                raise ValueError("model_evidence_owner_mismatch")
            return value
        except (TypeError, ValueError, KeyError, OSError, T6EvidenceError) as exc:
            raise D1RecoveryRequired("model_evidence_unavailable") from exc

    def save_application_rejection(self, rejection: D1ApplicationRejection) -> None:
        self.records._write(
            "d1-application-rejection", rejection.model_request_id, asdict(rejection)
        )

    def read_application_rejection(
        self, model_request_id: str
    ) -> D1ApplicationRejection | None:
        try:
            payload = self.records._read("d1-application-rejection", model_request_id)
            if payload is None:
                return None
            rejection = D1ApplicationRejection(**payload)
            if rejection.model_request_id != model_request_id:
                raise ValueError("application_rejection_owner_mismatch")
            return rejection
        except (TypeError, ValueError, OSError, T6EvidenceError) as exc:
            raise D1RecoveryRequired("application_rejection_unavailable") from exc

    def save_dispatch_rejection(self, rejection: D1ApplicationRejection) -> None:
        self.records._write(
            "d1-dispatch-rejection", rejection.model_request_id, asdict(rejection)
        )

    def read_dispatch_rejection(
        self, model_request_id: str
    ) -> D1ApplicationRejection | None:
        try:
            payload = self.records._read("d1-dispatch-rejection", model_request_id)
            if payload is None:
                return None
            rejection = D1ApplicationRejection(**payload)
            if rejection.model_request_id != model_request_id:
                raise ValueError("dispatch_rejection_owner_mismatch")
            return rejection
        except (TypeError, ValueError, OSError, T6EvidenceError) as exc:
            raise D1RecoveryRequired("dispatch_rejection_unavailable") from exc


class FileD3ModelEvidence:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.records = T6CommandRecorder(directory, create=create)

    def save(self, evidence: D3ModelEvidence) -> str:
        payload = asdict(evidence)
        self.records._write("d3-model", evidence.model_request_id, payload)
        return digest(payload)

    def read(self, model_request_id: str) -> D3ModelEvidence | None:
        try:
            payload = self.records._read("d3-model", model_request_id)
            if payload is None:
                return None
            evidence = D3ModelEvidence(**payload)
            if evidence.model_request_id != model_request_id:
                raise ValueError("D3 model evidence owner mismatch")
            return evidence
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_model_evidence_unavailable") from exc

    def save_rejection(self, rejection: D3ModelRejection) -> None:
        evidence = self.read(rejection.model_request_id)
        if evidence is None or evidence.outcome != "accepted":
            raise ExecutionRecoveryRequired("d3_rejection_requires_saved_proposal")
        self.records._write(
            "d3-model-rejection", rejection.model_request_id, asdict(rejection)
        )

    def read_rejection(self, model_request_id: str) -> D3ModelRejection | None:
        try:
            payload = self.records._read("d3-model-rejection", model_request_id)
            if payload is None:
                return None
            rejection = D3ModelRejection(**payload)
            if rejection.model_request_id != model_request_id:
                raise ValueError("D3 model rejection owner mismatch")
            return rejection
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryRequired("d3_model_rejection_unavailable") from exc
