"""Immutable local records for unverified candidate answers."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.pending_candidates import PendingCandidate


class PendingCandidateStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self._records = T6CommandRecorder(directory, create=create)

    def save(self, candidate: PendingCandidate) -> None:
        self._records._write(
            "pending-candidate", candidate.candidate_id, asdict(candidate)
        )

    def read(self, candidate_id: str) -> PendingCandidate | None:
        value = self._records._read("pending-candidate", candidate_id)
        if value is None:
            return None
        try:
            return PendingCandidate(**value)
        except (TypeError, ValueError) as exc:
            raise T6EvidenceError("invalid_pending_candidate") from exc

    def list_for_task(self, task_run_id: str) -> tuple[PendingCandidate, ...]:
        candidates = []
        for path in self._records.directory.glob("pending-candidate-*.json"):
            reference = path.name.removeprefix("pending-candidate-").removesuffix(
                ".json"
            )
            candidate = self.read(reference)
            if candidate is not None and candidate.task_run_id == task_run_id:
                candidates.append(candidate)
        return tuple(
            sorted(candidates, key=lambda item: (item.created_at, item.candidate_id))
        )
