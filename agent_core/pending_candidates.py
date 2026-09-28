"""Pending answer candidates backed by completed command evidence."""

from __future__ import annotations

import math
import hashlib
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from agent_core.command_contracts import T6CommandRecorderPort
from agent_core.control_store import ControlStateSnapshot, canonical_json
from agent_core.domain import ExecutionStatus, IntentStatus
from agent_core.actions import CandidateFlagAction


class CandidateEvidenceError(RuntimeError):
    pass


def _uuid4(value: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_candidate_id") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid_candidate_id")


@dataclass(frozen=True, slots=True)
class PendingCandidate:
    candidate_id: str
    task_run_id: str
    intent_id: str
    execution_id: str
    value: str
    created_at: float

    def __post_init__(self) -> None:
        _uuid4(self.candidate_id)
        _uuid4(self.task_run_id)
        _uuid4(self.execution_id)
        if not isinstance(self.intent_id, str) or not self.intent_id:
            raise ValueError("invalid_candidate_intent")
        if (
            not isinstance(self.value, str)
            or not self.value.strip()
            or len(self.value.encode("utf-8")) > 4096
        ):
            raise ValueError("invalid_candidate_value")
        if not isinstance(self.created_at, (int, float)) or not math.isfinite(
            self.created_at
        ):
            raise ValueError("invalid_candidate_time")


class PendingCandidateStorePort(Protocol):
    def save(self, candidate: PendingCandidate) -> None: ...
    def read(self, candidate_id: str) -> PendingCandidate | None: ...
    def list_for_task(self, task_run_id: str) -> tuple[PendingCandidate, ...]: ...


MAX_CANDIDATE_CONTEXT_BYTES = 16 * 1024
MAX_RECENT_CANDIDATES = 8


@dataclass(frozen=True, slots=True)
class PendingCandidateContextEntry:
    candidate_id: str
    execution_id: str
    value: str | None
    value_sha256: str
    verification_status: str = "pending"


@dataclass(frozen=True, slots=True)
class PendingCandidateContext:
    entries: tuple[PendingCandidateContextEntry, ...]
    history_clipped: bool


def _context_size(context: PendingCandidateContext) -> int:
    return len(canonical_json(asdict(context)).encode("utf-8"))


def recent_pending_candidate_context(
    state: ControlStateSnapshot, intent_id: str,
    store: PendingCandidateStorePort, commands: T6CommandRecorderPort,
) -> PendingCandidateContext:
    """Show bounded pending values only when their original source still agrees."""
    intent = next((item for item in state.intents if item.intent_id == intent_id), None)
    if intent is None:
        raise ValueError("intent_not_found")
    candidates = tuple(
        item for item in store.list_for_task(state.task_run.task_run_id)
        if item.task_run_id == state.task_run.task_run_id and item.intent_id == intent_id
    )
    selected = sorted(candidates, key=lambda item: (item.created_at, item.candidate_id))[-MAX_RECENT_CANDIDATES:]
    entries: list[PendingCandidateContextEntry] = []
    for candidate in reversed(selected):
        execution = next(
            (item for item in state.executions if item.execution_id == candidate.execution_id),
            None,
        )
        plan = next(
            (item for item in state.plans if execution is not None and item.plan_id == execution.plan_id),
            None,
        )
        if (
            execution is None or execution.status is not ExecutionStatus.SUCCEEDED
            or execution.output_ref != candidate.execution_id
            or plan is None or plan.intent_id != intent_id
            or not state.task_run.started_at <= candidate.created_at < state.task_run.deadline_at
        ):
            raise CandidateEvidenceError("candidate_execution_scope_mismatch")
        try:
            finished = commands.read_completed(candidate.execution_id)
        except Exception as exc:
            raise CandidateEvidenceError("candidate_execution_unverified") from exc
        if (
            finished is None or finished.action is not None
            or finished.record.request_id != candidate.execution_id
            or finished.record.task_run_id != state.task_run.task_run_id
            or finished.record.plan_id != plan.plan_id
            or finished.record.step_index != execution.step_index
            or finished.record.command_ref != execution.command_ref
            or finished.record.sandbox_id != state.task_run.sandbox_id
            or finished.record.status != "completed"
            or not finished.record.termination_confirmed
        ):
            raise CandidateEvidenceError("candidate_execution_unverified")
        digest = hashlib.sha256(candidate.value.encode("utf-8")).hexdigest()
        full = PendingCandidateContextEntry(
            candidate.candidate_id, candidate.execution_id, candidate.value, digest,
        )
        summary = PendingCandidateContext(tuple(entries + [full]), True)
        if _context_size(summary) <= MAX_CANDIDATE_CONTEXT_BYTES:
            entries.append(full)
            continue
        compact = PendingCandidateContextEntry(
            candidate.candidate_id, candidate.execution_id, None, digest,
        )
        summary = PendingCandidateContext(tuple(entries + [compact]), True)
        if _context_size(summary) > MAX_CANDIDATE_CONTEXT_BYTES:
            break
        entries.append(compact)
    clipped = len(entries) < len(candidates) or any(item.value is None for item in entries)
    return PendingCandidateContext(tuple(reversed(entries)), clipped)


class PendingCandidateRecorder:
    def __init__(
        self, store: PendingCandidateStorePort, commands: T6CommandRecorderPort
    ) -> None:
        self._store = store
        self._commands = commands

    def record(
        self,
        state: ControlStateSnapshot,
        intent_id: str,
        action: CandidateFlagAction,
        candidate_id: str,
        created_at: float,
    ) -> PendingCandidate:
        candidate = PendingCandidate(
            candidate_id,
            state.task_run.task_run_id,
            intent_id,
            action.execution_id,
            action.value,
            created_at,
        )
        existing = self._store.read(candidate_id)
        if existing is not None:
            if existing != candidate:
                raise CandidateEvidenceError("candidate_id_conflict")
            return existing
        intent = next(
            (item for item in state.intents if item.intent_id == intent_id), None
        )
        execution = next(
            (
                item
                for item in state.executions
                if item.execution_id == action.execution_id
            ),
            None,
        )
        plan = next(
            (
                item
                for item in state.plans
                if execution is not None and item.plan_id == execution.plan_id
            ),
            None,
        )
        if (
            intent is None
            or intent.status is not IntentStatus.RUNNING
            or execution is None
            or execution.status is not ExecutionStatus.SUCCEEDED
            or execution.output_ref != action.execution_id
            or plan is None
            or plan.intent_id != intent_id
            or not state.task_run.started_at <= created_at < state.task_run.deadline_at
        ):
            raise CandidateEvidenceError("candidate_execution_scope_mismatch")
        try:
            finished = self._commands.read_completed(action.execution_id)
        except Exception as exc:
            raise CandidateEvidenceError("candidate_execution_unverified") from exc
        if (
            finished is None
            or finished.action is not None
            or finished.record.request_id != action.execution_id
            or finished.record.task_run_id != state.task_run.task_run_id
            or finished.record.plan_id != plan.plan_id
            or finished.record.step_index != execution.step_index
            or finished.record.command_ref != execution.command_ref
            or finished.record.sandbox_id != state.task_run.sandbox_id
            or finished.record.status != "completed"
            or not finished.record.termination_confirmed
        ):
            raise CandidateEvidenceError("candidate_execution_unverified")
        self._store.save(candidate)
        return candidate


def grouped_pending_candidates(
    candidates: tuple[PendingCandidate, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    sources: dict[str, list[str]] = {}
    for candidate in sorted(
        candidates, key=lambda item: (item.created_at, item.candidate_id)
    ):
        owned = sources.setdefault(candidate.value, [])
        if candidate.execution_id not in owned:
            owned.append(candidate.execution_id)
    return tuple((value, tuple(ids)) for value, ids in sources.items())
