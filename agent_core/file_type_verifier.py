"""Deterministic verification of one file-type candidate and its proof."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from agent_core.attachment_import import (
    AttachmentImportClaim,
    AttachmentImportReceipt,
)
from agent_core.control_store import ControlStore
from agent_core.domain import (
    ExecutionEvidence,
    ExecutionStatus,
    IntentStatus,
    PlanStatus,
)
from agent_core.file_inspection import FILE_TOOL
from agent_core.file_type_fact import (
    FileTypeFactCandidate,
    FileTypeOutputRejected,
    observed_file_type,
)
from agent_core.file_type_proof import FileTypeVerificationProof
from agent_core.runtime import TaskRunStatus
from agent_core.tool_contracts import (
    MCPCommand,
    StoredToolRequest,
    ToolResult,
)
from agent_core.tool_policy import FileToolPolicy


class FileTypeVerificationError(RuntimeError):
    def __init__(
        self,
        code: Literal["candidate_rejected", "evidence_unavailable", "state_conflict"],
    ) -> None:
        self.code = code
        super().__init__(code)


class FileTypeToolEvidencePort(Protocol):
    def read_request_receipt(self, request_id: str) -> StoredToolRequest | None: ...

    def read_completed(self, request_id: str) -> ToolResult | None: ...

    def load_command(self, command_ref: str) -> MCPCommand: ...


class FileTypeImportEvidencePort(Protocol):
    def read_claim(self, import_id: str) -> AttachmentImportClaim | None: ...

    def read_receipt(self, import_id: str) -> AttachmentImportReceipt | None: ...


class FileTypeProofPort(Protocol):
    def read(self, proof_id: str) -> FileTypeVerificationProof | None: ...

    def save(self, proof: FileTypeVerificationProof) -> None: ...


@dataclass(frozen=True, slots=True)
class FileTypeProofEvidence:
    proof: FileTypeVerificationProof
    execution: ExecutionEvidence


class FileTypeProofEvidencePort(Protocol):
    def lookup_file_type_proof(
        self, execution_id: str, proof_id: str
    ) -> FileTypeProofEvidence: ...


class FileTypeVerifier:
    def __init__(
        self,
        *,
        store: ControlStore,
        tools: FileTypeToolEvidencePort,
        imports: FileTypeImportEvidencePort,
        proofs: FileTypeProofPort,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._tools = tools
        self._imports = imports
        self._proofs = proofs
        self._clock = clock
        self._policy = FileToolPolicy()

    def verify_candidate(
        self, *, intent_id: str, candidate: FileTypeFactCandidate, proof_id: str
    ) -> FileTypeVerificationProof:
        if not isinstance(candidate, FileTypeFactCandidate):
            raise TypeError("candidate must be a FileTypeFactCandidate")
        try:
            previous = self._proofs.read(proof_id)
        except Exception as exc:
            raise FileTypeVerificationError("evidence_unavailable") from exc
        if previous is not None:
            if (
                previous.intent_id != intent_id
                or previous.execution_id != candidate.execution_id
                or previous.path != candidate.path
                or previous.observed_type != candidate.observed_type
            ):
                raise FileTypeVerificationError("state_conflict")
            self._check(previous, candidate=True)
            return previous
        binding = self._store.load_intent_goal(intent_id)
        if binding is None:
            raise FileTypeVerificationError("state_conflict")
        import_id = binding.first_commit.event.details.get("import_id")
        if not isinstance(import_id, str):
            raise FileTypeVerificationError("evidence_unavailable")
        now = self._clock()
        proof = FileTypeVerificationProof(
            proof_id=proof_id,
            task_run_id=binding.goal.task_run_id,
            intent_id=intent_id,
            execution_id=candidate.execution_id,
            import_id=import_id,
            path=candidate.path,
            local_path=self._local_path(import_id),
            observed_type=candidate.observed_type,
            verified_at=now,
        )
        self._check(proof, candidate=True)
        self._proofs.save(proof)
        return proof

    def lookup_file_type_proof(
        self, execution_id: str, proof_id: str
    ) -> FileTypeProofEvidence:
        try:
            proof = self._proofs.read(proof_id)
        except Exception as exc:
            raise FileTypeVerificationError("evidence_unavailable") from exc
        if proof is None:
            raise FileTypeVerificationError("evidence_unavailable")
        if proof.execution_id != execution_id:
            raise FileTypeVerificationError("state_conflict")
        return self._check(proof, candidate=False)

    def _local_path(self, import_id: str) -> str:
        try:
            claim = self._imports.read_claim(import_id)
            receipt = self._imports.read_receipt(import_id)
        except Exception as exc:
            raise FileTypeVerificationError("evidence_unavailable") from exc
        if (
            claim is None
            or receipt is None
            or claim.identity() != receipt.identity()
            or claim.claimed_at != receipt.claimed_at
        ):
            raise FileTypeVerificationError("evidence_unavailable")
        return receipt.local_path

    def _check(
        self, proof: FileTypeVerificationProof, *, candidate: bool
    ) -> FileTypeProofEvidence:
        binding = self._store.load_intent_goal(proof.intent_id)
        state = self._store.load_control_state_rows(proof.task_run_id)
        if (
            binding is None
            or state is None
            or binding.goal.task_run_id != proof.task_run_id
        ):
            raise FileTypeVerificationError("state_conflict")
        task = state.task_run
        intent = next(
            (x for x in state.intents if x.intent_id == proof.intent_id), None
        )
        execution = next(
            (x for x in state.executions if x.execution_id == proof.execution_id), None
        )
        plan = next(
            (
                x
                for x in state.plans
                if execution is not None and x.plan_id == execution.plan_id
            ),
            None,
        )
        if (
            intent is None
            or execution is None
            or plan is None
            or plan.intent_id != proof.intent_id
            or len(plan.steps) != 1
            or execution.step_index != 0
            or execution.status is not ExecutionStatus.SUCCEEDED
            or execution.output_ref is None
            or (
                candidate
                and (
                    task.status is not TaskRunStatus.RUNNING
                    or intent.status is not IntentStatus.RUNNING
                    or plan.status is not PlanStatus.COMPLETED
                )
            )
        ):
            raise FileTypeVerificationError("state_conflict")
        if proof.path != binding.goal.path:
            raise FileTypeVerificationError(
                "candidate_rejected" if candidate else "state_conflict"
            )
        import_id = binding.first_commit.event.details.get("import_id")
        if import_id != proof.import_id:
            raise FileTypeVerificationError("state_conflict")

        try:
            claim = self._imports.read_claim(proof.import_id)
            imported = self._imports.read_receipt(proof.import_id)
            request_receipt = self._tools.read_request_receipt(proof.execution_id)
            finished = self._tools.read_completed(proof.execution_id)
            command = self._tools.load_command(execution.command_ref)
        except Exception as exc:
            raise FileTypeVerificationError("evidence_unavailable") from exc
        if (
            claim is None
            or imported is None
            or request_receipt is None
            or finished is None
            or claim.identity() != imported.identity()
            or claim.claimed_at != imported.claimed_at
        ):
            raise FileTypeVerificationError("evidence_unavailable")

        attachments = task.request.target.attachments
        request = request_receipt.request
        authorization = request_receipt.authorization
        record = finished.record
        if (
            imported.task_run_id != task.task_run_id
            or imported.sandbox_id != task.sandbox_id
            or imported.source_root != task.source_root
            or imported.sandbox_path != proof.path
            or imported.local_path != proof.local_path
            or imported.attachment_index >= len(attachments)
            or attachments[imported.attachment_index].kind != "local_path"
            or attachments[imported.attachment_index].value != proof.local_path
            or request.task_run_id != task.task_run_id
            or request.plan_id != plan.plan_id
            or request.step_index != execution.step_index
            or request.tool_name != FILE_TOOL
            or request.path != proof.path
            or request_receipt.sandbox_id != task.sandbox_id
            or authorization.task_run_id != task.task_run_id
            or not self._policy.authorize(request, authorization).allowed
            or not self._policy.validate_command(request, command).allowed
            or record.request_id != request.request_id
            or record.execution_id != execution.execution_id
            or record.task_run_id != task.task_run_id
            or record.sandbox_id != task.sandbox_id
            or record.plan_id != plan.plan_id
            or record.step_index != execution.step_index
            or record.authorization_id != authorization.authorization_id
            or record.command_ref != execution.command_ref
            or record.output_ref != execution.output_ref
            or finished.action is not None
        ):
            raise FileTypeVerificationError("evidence_unavailable")

        now = self._clock()
        if not (
            task.started_at
            <= claim.claimed_at
            <= imported.completed_at
            <= record.started_at
            <= record.finished_at
            <= proof.verified_at
            <= min(task.deadline_at, now)
        ):
            raise FileTypeVerificationError("evidence_unavailable")
        try:
            observed = observed_file_type(record)
        except FileTypeOutputRejected as exc:
            raise FileTypeVerificationError(
                "candidate_rejected" if candidate else "evidence_unavailable"
            ) from exc
        if observed != proof.observed_type:
            raise FileTypeVerificationError(
                "candidate_rejected" if candidate else "evidence_unavailable"
            )
        return FileTypeProofEvidence(
            proof,
            ExecutionEvidence(task.task_run_id, proof.local_path, record.finished_at),
        )
