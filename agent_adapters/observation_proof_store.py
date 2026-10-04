"""Immutable local storage for independently verified observation proofs."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.artifact_proofs import D3ObservationProof
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.observation_proof import T6ObservationProof


class T6ObservationProofStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self._records = T6CommandRecorder(directory, create=create)

    def save(self, proof: T6ObservationProof) -> None:
        value = asdict(proof)
        value["execution_ids"] = list(proof.execution_ids)
        self._records._write("observation-proof", proof.proof_id, value)

    def read(self, proof_id: str) -> T6ObservationProof | None:
        value = self._records._read("observation-proof", proof_id)
        if value is None:
            return None
        try:
            value["execution_ids"] = tuple(value["execution_ids"])
            return T6ObservationProof(**value)
        except (KeyError, TypeError, ValueError) as exc:
            raise T6EvidenceError("invalid_observation_proof") from exc


class FileD3ProofStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self.records = T6CommandRecorder(directory, create=create)

    def save(self, proof: D3ObservationProof) -> None:
        self.records._write("d3-proof", proof.proof_id, asdict(proof))

    def read(self, proof_id: str) -> D3ObservationProof | None:
        try:
            payload = self.records._read("d3-proof", proof_id)
            if payload is None:
                return None
            proof = D3ObservationProof(**payload)
            if proof.proof_id != proof_id:
                raise ValueError("D3 proof owner mismatch")
            return proof
        except (T6EvidenceError, TypeError, ValueError, KeyError) as exc:
            raise D3RecoveryRequired("d3_proof_evidence_unavailable") from exc
