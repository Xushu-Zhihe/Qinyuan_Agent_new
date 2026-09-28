"""Read-only proof for an observation, separate from file-type proofs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from agent_core.domain import ExecutionEvidence, IntentStatus, PlanStatus
from agent_core.runtime import TaskRunStatus
from agent_core.intent_postconditions import (
    CheckStatus,
    IntentPostconditions,
    OutputContains,
    PostconditionStorePort,
    check_postconditions,
)
from agent_core.actions import ProbeAction

if TYPE_CHECKING:
    from agent_core.control_store import ControlStore
    from agent_core.command_contracts import T6CommandRecorderPort


def binding_digest(binding: IntentPostconditions) -> str:
    payload = {
        "task_run_id": binding.task_run_id,
        "intent_id": binding.intent_id,
        "plan_id": binding.plan_id,
        "conditions": [
            {
                "kind": "output_contains"
                if isinstance(c, OutputContains)
                else "file_sha256",
                **asdict(c),
            }
            for c in binding.conditions
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def observation_fact_content(proof_id: str, condition_count: int) -> str:
    return (
        f"Verified execution observation: {condition_count} declared conditions satisfied; "
        f"proof {proof_id}. This does not verify a candidate answer."
    )


@dataclass(frozen=True, slots=True)
class T6ObservationProof:
    proof_id: str
    task_run_id: str
    intent_id: str
    plan_id: str
    source_execution_id: str
    execution_ids: tuple[str, ...]
    binding_sha256: str
    verified_at: float

    def __post_init__(self) -> None:
        for value in (self.proof_id, self.task_run_id, self.source_execution_id):
            if str(UUID(value)) != value:
                raise ValueError("invalid_observation_proof_id")
        if (
            not isinstance(self.execution_ids, tuple)
            or not self.execution_ids
            or self.source_execution_id != self.execution_ids[0]
            or len(self.binding_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.binding_sha256)
            or not math.isfinite(self.verified_at)
        ):
            raise ValueError("invalid_observation_proof")


class ObservationProofStorePort(Protocol):
    def read(self, proof_id: str) -> T6ObservationProof | None: ...
    def save(self, proof: T6ObservationProof) -> None: ...


class ObservationProofError(RuntimeError):
    pass


class T6ObservationProofEvidencePort(Protocol):
    def read_proof(self, proof_id: str) -> T6ObservationProof | None: ...
    def lookup(self, execution_id: str, proof_id: str) -> ExecutionEvidence: ...
    def latest_verifiable_plan(self, state, intent_id: str) -> str: ...
    def read_verified(
        self, execution_id: str, proof_id: str
    ) -> tuple[T6ObservationProof, ExecutionEvidence]: ...


class T6ObservationProofVerifier:
    def __init__(
        self,
        control_store: ControlStore,
        postconditions: PostconditionStorePort,
        commands: T6CommandRecorderPort,
        proofs: ObservationProofStorePort,
        clock: Callable[[], float],
        action_journal=None,
    ) -> None:
        self.control_store = control_store
        self.postconditions = postconditions
        self.commands = commands
        self.proofs = proofs
        self.clock = clock
        self.action_journal = action_journal

    def latest_verifiable_plan(self, state, intent_id: str) -> str:
        plans = [item for item in state.plans if item.intent_id == intent_id]
        for plan in reversed(plans):
            binding = self.postconditions.read(plan.plan_id, intent_id)
            if binding is not None:
                if binding.task_run_id != state.task_run.task_run_id:
                    raise ObservationProofError("postcondition_scope_mismatch")
                return plan.plan_id
            # A missing binding may only be skipped when the saved model action
            # explicitly declared an exploratory probe for this exact Plan.
            if self.action_journal is None:
                raise ObservationProofError("plan_declaration_unavailable")
            records = self.action_journal.list_for_intent(
                state.task_run.task_run_id, intent_id,
            )
            owner = next((item for item in records
                          if item.start.resource_ids.plan_id == plan.plan_id), None)
            if owner is None or owner.applied_at is None:
                raise ObservationProofError("plan_declaration_unavailable")
            action = self.action_journal.read_result(owner)
            if not isinstance(action, ProbeAction) or action.conditions:
                raise ObservationProofError("plan_postconditions_missing")
        raise ObservationProofError("no_verifiable_plan")

    def read_proof(self, proof_id: str) -> T6ObservationProof | None:
        return self.proofs.read(proof_id)

    def issue(
        self, intent_id: str, proof_id: str, plan_id: str | None = None
    ) -> T6ObservationProof:
        existing = self.proofs.read(proof_id)
        if existing is not None:
            if existing.intent_id != intent_id:
                raise ObservationProofError("proof_id_conflict")
            self.lookup(existing.source_execution_id, proof_id)
            return existing
        binding = (
            self.postconditions.read(intent_id)
            if plan_id is None
            else self.postconditions.read(plan_id, intent_id)
        )
        if binding is None:
            raise ObservationProofError("postconditions_missing")
        state = self.control_store.load_control_state_rows(binding.task_run_id)
        if state is None:
            raise ObservationProofError("task_run_missing")
        intent = next((i for i in state.intents if i.intent_id == intent_id), None)
        if (
            state.task_run.status is not TaskRunStatus.RUNNING
            or intent is None
            or intent.status is not IntentStatus.RUNNING
        ):
            raise ObservationProofError("intent_not_running")
        status, sources = check_postconditions(
            state,
            binding,
            self.commands,
            lambda index, path: self.postconditions.read_file(binding, index, path),
        )
        if status is not CheckStatus.SATISFIED:
            raise ObservationProofError(f"postconditions_{status.value}")
        now = self.clock()
        if not state.task_run.started_at <= now < state.task_run.deadline_at:
            raise ObservationProofError("proof_outside_task_time")
        proof = T6ObservationProof(
            proof_id,
            binding.task_run_id,
            intent_id,
            binding.plan_id,
            sources[0],
            sources,
            binding_digest(binding),
            now,
        )
        self.proofs.save(proof)
        return proof

    def lookup(self, execution_id: str, proof_id: str) -> ExecutionEvidence:
        return self.read_verified(execution_id, proof_id)[1]

    def read_verified(
        self, execution_id: str, proof_id: str
    ) -> tuple[T6ObservationProof, ExecutionEvidence]:
        try:
            proof = self.proofs.read(proof_id)
            if proof is None or proof.source_execution_id != execution_id:
                raise ObservationProofError("proof_missing_or_mismatched")
            binding = self.postconditions.read(proof.plan_id, proof.intent_id)
            state = self.control_store.load_control_state_rows(proof.task_run_id)
            if binding is None or state is None:
                raise ObservationProofError("proof_dependencies_missing")
            if (
                binding.plan_id != proof.plan_id
                or binding.task_run_id != proof.task_run_id
                or binding_digest(binding) != proof.binding_sha256
                or not state.task_run.started_at
                <= proof.verified_at
                <= state.task_run.deadline_at
            ):
                raise ObservationProofError("proof_binding_mismatch")
            plan = next((p for p in state.plans if p.plan_id == proof.plan_id), None)
            intent = next(
                (i for i in state.intents if i.intent_id == proof.intent_id), None
            )
            if (
                plan is None
                or plan.status is not PlanStatus.COMPLETED
                or plan.intent_id != proof.intent_id
                or intent is None
                or intent.status is IntentStatus.PENDING
            ):
                raise ObservationProofError("proof_state_mismatch")
            status, sources = check_postconditions(
                state,
                binding,
                self.commands,
                lambda index, path: self.postconditions.read_file(binding, index, path),
            )
            if status is not CheckStatus.SATISFIED or sources != proof.execution_ids:
                raise ObservationProofError("proof_evidence_mismatch")
            for source in set(sources):
                record = self.commands.read_record(source)
                if record is None or record.finished_at > proof.verified_at:
                    raise ObservationProofError("proof_precedes_observation")
            return proof, ExecutionEvidence(proof.task_run_id, None, proof.verified_at)
        except ObservationProofError:
            raise
        except Exception as exc:
            raise ObservationProofError("proof_unavailable") from exc
