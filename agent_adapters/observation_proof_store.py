"""Immutable local receipt for verified observation proofs."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
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
