"""Independent, read-only proof for one frozen D3 artifact observation."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from agent_core.artifact_operations import fixed_size_script, parse_size_output
from agent_core.artifact_workspace import artifact_path
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.domain import ExecutionEvidence, ExecutionStatus, PlanStatus
from agent_core.dual_llm_contracts import (
    D3PlanBinding,
    D3RecoveryRequired,
    _sha,
    _time,
    _uuid,
)
from agent_core.turn_modeling import digest


def d3_fact_content(proof_id: str) -> str:
    return f"Verified attachment_copy_sha256 v1 observation {proof_id}; whole task not evaluated."


@dataclass(frozen=True, slots=True)
class D3ObservationProof:
    proof_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    artifact_version_id: str
    observation_id: str
    source_execution_id: str
    size_execution_id: str | None
    rule_binding_sha256: str
    observed_sha256: str
    verified_at: float

    def __post_init__(self) -> None:
        for name in (
            "proof_id",
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "artifact_version_id",
            "observation_id",
            "source_execution_id",
        ):
            _uuid(getattr(self, name), name)
        if self.size_execution_id is not None:
            _uuid(self.size_execution_id, "size_execution_id")
        _sha(self.rule_binding_sha256, "rule_binding_sha256")
        _sha(self.observed_sha256, "observed_sha256")
        _time(self.verified_at, "verified_at")


class D3ProofStorePort(Protocol):
    def read(self, proof_id: str) -> D3ObservationProof | None: ...
    def save(self, proof: D3ObservationProof) -> None: ...


class D3ObservationProofVerifier:
    def __init__(
        self,
        ledger,
        imports,
        workspace,
        commands,
        proofs: D3ProofStorePort,
        clock: Callable[[], float],
    ) -> None:
        self.ledger = ledger
        self.imports = imports
        self.workspace = workspace
        self.commands = commands
        self.proofs = proofs
        self.clock = clock

    @staticmethod
    def _record_digest(record) -> str:
        value = asdict(record)
        value["stdout"] = base64.b64encode(record.stdout).decode("ascii")
        value["stderr"] = base64.b64encode(record.stderr).decode("ascii")
        return digest(value)

    def _execution(self, state, binding: D3PlanBinding, step_index: int, script: str):
        matches = [
            item
            for item in state.executions
            if item.plan_id == binding.plan_id and item.step_index == step_index
        ]
        if len(matches) != 1 or matches[0].status is not ExecutionStatus.SUCCEEDED:
            raise D3RecoveryRequired("d3_proof_execution_not_confirmed")
        execution = matches[0]
        completed = self.commands.read_completed(execution.execution_id)
        if completed is None or completed.action is not None:
            raise D3RecoveryRequired("d3_proof_command_receipt_missing")
        record = completed.record
        command = self.commands.read_command(execution.command_ref)
        operation = self.ledger.load_d3_operation(execution.execution_id)
        if (
            command is None
            or command.argv != ("bash", "-c", script)
            or record.request_id != execution.execution_id
            or record.task_run_id != binding.task_run_id
            or record.sandbox_id != state.task_run.sandbox_id
            or record.plan_id != binding.plan_id
            or record.step_index != step_index
            or record.command_ref != execution.command_ref
            or execution.output_ref != execution.execution_id
            or record.status != "completed"
            or record.exit_code != 0
            or record.stdout_truncated
            or record.stderr_truncated
            or record.stderr
            or not record.dispatched
            or not record.termination_confirmed
            or not record.stream_complete
            or not state.task_run.started_at
            <= record.started_at
            <= record.finished_at
            <= state.task_run.deadline_at
            or operation is None
            or operation.start.category != "tool_dispatch"
            or operation.start.task_run_id != binding.task_run_id
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_sha256 != self._record_digest(record)
            or operation.application_ref != execution.execution_id
        ):
            raise D3RecoveryRequired("d3_proof_command_evidence_changed")
        return execution, record

    def _observation(self, binding: D3PlanBinding, observation_id: str):
        state = self.ledger.load_control_state_rows(binding.task_run_id)
        turn = self.ledger.load_d3_turn_binding(binding.turn_id)
        plan = (
            next(
                (item for item in state.plans if item.plan_id == binding.plan_id), None
            )
            if state is not None
            else None
        )
        intent = (
            next(
                (item for item in state.intents if item.intent_id == binding.intent_id),
                None,
            )
            if state is not None
            else None
        )
        if (
            state is None
            or turn is None
            or plan is None
            or intent is None
            or self.ledger.load_d3_plan_binding(binding.plan_id) != binding
            or binding.observation_mode != "new_observation"
            or plan.intent_id != binding.intent_id
            or plan.status not in {PlanStatus.RUNNING, PlanStatus.COMPLETED}
            or intent.turn_id != binding.turn_id
            or binding.rule_binding_sha256 != digest(asdict(turn))
            or turn.task_run_id != binding.task_run_id
        ):
            raise D3RecoveryRequired("d3_proof_plan_binding_changed")
        artifact = self.workspace.read_artifact(binding.artifact_version_id)
        artifact_operation = self.ledger.load_d3_operation(binding.artifact_version_id)
        source = self.imports.read_receipt(turn.import_id)
        if (
            artifact is None
            or artifact.status != "confirmed"
            or source is None
            or source.task_run_id != binding.task_run_id
            or source.sandbox_id != state.task_run.sandbox_id
            or source.attachment_index != turn.attachment_index
            or source.byte_size != turn.expected_byte_size
            or source.sha256 != turn.expected_sha256
            or artifact.claim.task_run_id != binding.task_run_id
            or artifact.claim.turn_id != binding.turn_id
            or artifact.claim.intent_id != binding.intent_id
            or artifact.claim.plan_id != binding.plan_id
            or artifact.claim.import_id != turn.import_id
            or artifact.claim.attachment_index != turn.attachment_index
            or artifact.claim.sandbox_id != state.task_run.sandbox_id
            or artifact.claim.source_path != source.sandbox_path
            or artifact.claim.path
            != artifact_path(binding.intent_id, binding.artifact_version_id)
            or artifact.claim.expected_byte_size != turn.expected_byte_size
            or artifact.claim.expected_sha256 != turn.expected_sha256
            or artifact.readback.byte_size != turn.expected_byte_size
            or artifact.readback.sha256 != turn.expected_sha256
            or artifact_operation is None
            or artifact_operation.start.category != "copy_preparation"
            or artifact_operation.start.context_sha256 != digest(asdict(artifact.claim))
            or artifact_operation.completion is None
            or artifact_operation.completion.result_kind != "completed"
            or artifact_operation.completion.evidence_sha256 != digest(asdict(artifact))
            or artifact_operation.application_ref != binding.artifact_version_id
        ):
            raise D3RecoveryRequired("d3_proof_artifact_evidence_changed")
        size_execution_id = None
        sha_step = 0
        if binding.template == "size_then_sha256":
            size_execution, size_record = self._execution(
                state, binding, 0, fixed_size_script(artifact.claim.path)
            )
            size = self.workspace.read_size(size_execution.execution_id)
            observed_status, observed_size = parse_size_output(size_record.stdout)
            if (
                size is None
                or size.task_run_id != binding.task_run_id
                or size.plan_id != binding.plan_id
                or size.artifact_version_id != binding.artifact_version_id
                or size.execution_id != size_execution.execution_id
                or size.expected_byte_size != turn.expected_byte_size
                or size.observed_status != observed_status
                or size.observed_byte_size != observed_size
                or size.output_sha256 != hashlib.sha256(size_record.stdout).hexdigest()
                or size.status != "confirmed"
            ):
                raise D3RecoveryRequired("d3_proof_size_evidence_changed")
            size_execution_id = size_execution.execution_id
            sha_step = 1
        sha_execution, sha_record = self._execution(
            state, binding, sha_step, fixed_digest_script(artifact.claim.path)
        )
        try:
            output = json.loads(sha_record.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise D3RecoveryRequired("d3_sha_command_output_invalid") from exc
        if output != {"status": "present", "sha256": turn.expected_sha256}:
            raise D3RecoveryRequired("d3_sha_command_integrity_exception")
        receipt = self.workspace.read_digest(observation_id)
        operation = self.ledger.load_d3_operation(observation_id)
        if (
            receipt is None
            or receipt.status != "confirmed"
            or receipt.claim.task_run_id != binding.task_run_id
            or receipt.claim.turn_id != binding.turn_id
            or receipt.claim.intent_id != binding.intent_id
            or receipt.claim.plan_id != binding.plan_id
            or receipt.claim.artifact_version_id != binding.artifact_version_id
            or receipt.claim.execution_id != sha_execution.execution_id
            or receipt.claim.sandbox_id != state.task_run.sandbox_id
            or receipt.claim.path != artifact.claim.path
            or receipt.claim.expected_sha256 != turn.expected_sha256
            or receipt.observed.status != "present"
            or receipt.observed.sha256 != turn.expected_sha256
            or receipt.completed_at < sha_record.finished_at
            or operation is None
            or operation.start.category != "sha_observation"
            or operation.start.context_sha256 != digest(asdict(receipt.claim))
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_sha256 != digest(asdict(receipt))
            or operation.application_ref != observation_id
        ):
            raise D3RecoveryRequired("d3_proof_digest_evidence_changed")
        return state, turn, sha_execution, size_execution_id, receipt

    def issue(
        self, binding: D3PlanBinding, observation_id: str, proof_id: str
    ) -> D3ObservationProof:
        existing = self.proofs.read(proof_id)
        if existing is not None:
            if (
                existing.task_run_id != binding.task_run_id
                or existing.turn_id != binding.turn_id
                or existing.intent_id != binding.intent_id
                or existing.plan_id != binding.plan_id
                or existing.artifact_version_id != binding.artifact_version_id
                or existing.observation_id != observation_id
            ):
                raise D3RecoveryRequired("d3_proof_id_conflict")
            self.read_verified(existing.source_execution_id, proof_id)
            return existing
        state, turn, execution, size_id, receipt = self._observation(
            binding, observation_id
        )
        plan = next(item for item in state.plans if item.plan_id == binding.plan_id)
        if plan.status is not PlanStatus.RUNNING:
            raise D3RecoveryRequired("d3_proof_issue_requires_running_plan")
        now = self.clock()
        if not receipt.completed_at <= now < state.task_run.deadline_at:
            raise D3RecoveryRequired("d3_proof_issue_outside_task_time")
        proof = D3ObservationProof(
            proof_id,
            binding.task_run_id,
            binding.turn_id,
            binding.intent_id,
            binding.plan_id,
            binding.artifact_version_id,
            observation_id,
            execution.execution_id,
            size_id,
            digest(asdict(turn)),
            receipt.observed.sha256,
            now,
        )
        self.proofs.save(proof)
        return proof

    def read_verified(self, execution_id: str, proof_id: str):
        proof = self.proofs.read(proof_id)
        if proof is None or proof.source_execution_id != execution_id:
            raise D3RecoveryRequired("d3_proof_missing_or_mismatched")
        binding = self.ledger.load_d3_plan_binding(proof.plan_id)
        if binding is None:
            raise D3RecoveryRequired("d3_proof_plan_missing")
        state, turn, execution, size_id, receipt = self._observation(
            binding, proof.observation_id
        )
        if (
            (
                proof.task_run_id,
                proof.turn_id,
                proof.intent_id,
                proof.plan_id,
                proof.artifact_version_id,
                proof.source_execution_id,
                proof.size_execution_id,
                proof.rule_binding_sha256,
                proof.observed_sha256,
            )
            != (
                binding.task_run_id,
                binding.turn_id,
                binding.intent_id,
                binding.plan_id,
                binding.artifact_version_id,
                execution.execution_id,
                size_id,
                digest(asdict(turn)),
                receipt.observed.sha256,
            )
            or not receipt.completed_at
            <= proof.verified_at
            <= state.task_run.deadline_at
        ):
            raise D3RecoveryRequired("d3_proof_binding_changed")
        return proof, ExecutionEvidence(proof.task_run_id, None, receipt.completed_at)
