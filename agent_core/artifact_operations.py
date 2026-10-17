"""Bounded, evidence-first workspace operations for the internal D3 chain."""

from __future__ import annotations

from agent_core.http_contracts import plan_turn_binding
import asyncio
import hashlib
import json
import shlex
from dataclasses import asdict
from typing import Callable, Literal
from uuid import uuid4

from agent_core.artifact_workspace import (
    D3ArtifactClaim,
    D3ArtifactReceipt,
    D3DigestClaim,
    D3DigestReceipt,
    D3ReadbackAudit,
    D3ShareClaim,
    D3ShareReadback,
    D3ShareReceipt,
    D3SizeObservation,
    D3StopClaim,
    D3StopReadback,
    D3StopReceipt,
    artifact_path,
    shared_path,
)
from agent_core.attachment_import import (
    AttachmentImportReadback,
    AttachmentImportReceipt,
)
from agent_core.command_contracts import T6ExecutionRecord, validate_working_directory
from agent_core.domain import FactStatus, IntentStatus, PlanStatus, TurnStatus
from agent_core.dual_llm_contracts import (
    D3OperationCategory,
    D3OperationCompletion,
    D3OperationStart,
    D3PlanBinding,
    D3RecoveryRequired,
    D3TurnBinding,
)
from agent_core.intent_postconditions import FileDigestObservation
from agent_core.modeling import CancellationToken
from agent_core.runtime import TaskRunStatus
from agent_core.turn_modeling import digest

D3_SIZE_SCRIPT = r"""import json,os,stat,sys
path=sys.argv[1];root='/workspace/work'
parts=path[len(root)+1:].split('/')
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    for part in parts[:-1]:
        nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        os.close(fd);fd=nextfd
    item=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        info=os.fstat(item)
        if not stat.S_ISREG(info.st_mode):
            print(json.dumps({'status':'unsafe'}));sys.exit(0)
        print(json.dumps({'status':'present','byte_size':info.st_size}))
    finally:os.close(item)
except FileNotFoundError:
    print(json.dumps({'status':'missing'}))
except OSError:
    print(json.dumps({'status':'unsafe'}))
finally:os.close(fd)
"""


def fixed_size_script(path: str) -> str:
    validate_working_directory(path)
    if path == "/workspace/work":
        raise ValueError("size_path_is_directory")
    return "exec python3 -I -c " + shlex.quote(D3_SIZE_SCRIPT) + " " + shlex.quote(path)


def parse_size_output(
    stdout: bytes,
) -> tuple[Literal["present", "missing", "unsafe", "invalid"], int | None]:
    try:
        if len(stdout) > 1024:
            raise ValueError("size output oversized")
        payload = json.loads(stdout.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("size output is not an object")
        if payload == {"status": "missing"}:
            return "missing", None
        if payload == {"status": "unsafe"}:
            return "unsafe", None
        if set(payload) == {"status", "byte_size"} and payload["status"] == "present":
            size = payload["byte_size"]
            if type(size) is int and 0 <= size <= 16 * 1024 * 1024:
                return "present", size
    except (UnicodeDecodeError, ValueError, TypeError):
        pass
    return "invalid", None


class D3WorkspaceOperations:
    def __init__(
        self,
        controller,
        ledger,
        evidence,
        sandbox,
        imports,
        commands,
        proofs,
        clock: Callable[[], float],
    ) -> None:
        self.controller = controller
        self.ledger = ledger
        self.evidence = evidence
        self.sandbox = sandbox
        self.imports = imports
        self.commands = commands
        self.proofs = proofs
        self.clock = clock

    @property
    def state(self):
        return self.controller.control_state

    def _budget(self, token: CancellationToken) -> float:
        state = self.state
        if token.is_cancelled or state.task_run.status is not TaskRunStatus.RUNNING:
            raise D3RecoveryRequired("d3_workspace_dispatch_stopped")
        remaining = min(60.0, state.task_run.deadline_at - self.clock())
        if remaining <= 0:
            raise D3RecoveryRequired("d3_workspace_deadline_elapsed")
        return remaining

    @staticmethod
    def _read_budget(token: CancellationToken) -> float:
        if token.is_cancelled:
            raise D3RecoveryRequired("d3_readonly_query_cancelled")
        return 60.0

    def _start(
        self,
        operation_id: str,
        category: D3OperationCategory,
        resource_ref: str,
        claim,
    ) -> None:
        self.ledger.begin_d3_operation(
            D3OperationStart(
                operation_id,
                claim.task_run_id,
                category,
                self.clock(),
                self.state.latest_checkpoint.sequence,
                digest(asdict(claim)),
                resource_ref,
            )
        )

    def _finish(self, operation_id: str, receipt, *, confirmed: bool) -> None:
        self.ledger.complete_d3_operation(
            D3OperationCompletion(
                operation_id,
                receipt.completed_at,
                "completed" if confirmed else "failed",
                operation_id,
                digest(asdict(receipt)),
            )
        )
        if confirmed:
            self.ledger.mark_d3_operation_applied(operation_id, operation_id)

    def _repair_known(self, operation_id: str, category: str, receipt) -> None:
        record = self.ledger.load_d3_operation(operation_id)
        expected = digest(asdict(receipt))
        if (
            record is None
            or record.start.category != category
            or record.start.task_run_id != receipt.claim.task_run_id
            or record.start.context_sha256 != digest(asdict(receipt.claim))
            or record.start.resource_ref
            != (receipt.claim.intent_id if category == "intent_stop" else operation_id)
        ):
            raise D3RecoveryRequired("d3_known_receipt_operation_mismatch")
        if record.completion is None:
            self.ledger.complete_d3_operation(
                D3OperationCompletion(
                    operation_id,
                    receipt.completed_at,
                    "completed" if receipt.status == "confirmed" else "failed",
                    operation_id,
                    expected,
                )
            )
        elif (
            record.completion.evidence_ref != operation_id
            or record.completion.evidence_sha256 != expected
            or record.completion.result_kind
            != ("completed" if receipt.status == "confirmed" else "failed")
        ):
            raise D3RecoveryRequired("d3_known_receipt_completion_mismatch")
        if receipt.status == "confirmed":
            self.ledger.mark_d3_operation_applied(operation_id, operation_id)

    def _import(self, binding: D3TurnBinding) -> AttachmentImportReceipt:
        state = self.state
        receipt = self.imports.read_receipt(binding.import_id)
        if (
            not isinstance(receipt, AttachmentImportReceipt)
            or receipt.task_run_id != binding.task_run_id
            or receipt.sandbox_id != state.task_run.sandbox_id
            or receipt.attachment_index != binding.attachment_index
            or receipt.byte_size != binding.expected_byte_size
            or receipt.sha256 != binding.expected_sha256
            or receipt.sandbox_path
            != f"/workspace/input/d3-attachment-{binding.attachment_index}"
        ):
            raise D3RecoveryRequired("d3_frozen_import_unavailable")
        return receipt

    def read_artifact(self, version_id: str) -> D3ArtifactReceipt:
        receipt = self.evidence.read_artifact(version_id)
        operation = self.ledger.load_d3_operation(version_id)
        if (
            receipt is None
            or receipt.status != "confirmed"
            or operation is None
            or operation.start.category != "copy_preparation"
            or operation.start.context_sha256 != digest(asdict(receipt.claim))
            or operation.completion is None
            or operation.completion.result_kind != "completed"
            or operation.completion.evidence_sha256 != digest(asdict(receipt))
            or operation.application_ref != version_id
        ):
            raise D3RecoveryRequired("d3_artifact_version_unconfirmed")
        return receipt

    async def prepare_artifact(
        self,
        binding: D3TurnBinding,
        intent_id: str,
        plan_id: str,
        artifact_version_id: str,
        token: CancellationToken,
    ) -> D3ArtifactReceipt:
        source = self._import(binding)
        state = self.state
        intent = next(
            (item for item in state.intents if item.intent_id == intent_id), None
        )
        plan = next((item for item in state.plans if item.plan_id == plan_id), None)
        plan_binding = self.ledger.load_d3_plan_binding(plan_id)
        turn = next(
            (item for item in state.turns if item.turn_id == binding.turn_id), None
        )
        if (
            plan_turn_binding(self.ledger,plan_binding) != binding
            or intent is None
            or intent.turn_id != binding.turn_id
            or intent.status is not IntentStatus.RUNNING
            or plan is None
            or plan.intent_id != intent_id
            or plan.status is not PlanStatus.RUNNING
            or plan_binding is None
            or plan_binding.artifact_version_id != artifact_version_id
            or plan_binding.turn_id != binding.turn_id
            or plan_binding.intent_id != intent_id
            or turn is None
            or turn.status is not TurnStatus.RUNNING
        ):
            raise D3RecoveryRequired("d3_artifact_owner_mismatch")
        existing = self.evidence.read_artifact(artifact_version_id)
        if existing is not None:
            self._repair_known(artifact_version_id, "copy_preparation", existing)
            receipt = self.read_artifact(artifact_version_id)
            if (
                receipt.claim.turn_id != binding.turn_id
                or receipt.claim.intent_id != intent_id
                or receipt.claim.plan_id != plan_id
                or receipt.claim.import_id != binding.import_id
            ):
                raise D3RecoveryRequired("d3_artifact_version_rebound")
            return receipt
        if (
            self.evidence.read_artifact_claim(artifact_version_id) is not None
            or self.ledger.load_d3_operation(artifact_version_id) is not None
        ):
            raise D3RecoveryRequired("d3_artifact_copy_outcome_unknown")
        self._budget(token)
        claim = D3ArtifactClaim(
            artifact_version_id,
            binding.task_run_id,
            binding.turn_id,
            intent_id,
            plan_id,
            binding.import_id,
            binding.attachment_index,
            state.task_run.sandbox_id,
            source.sandbox_path,
            artifact_path(intent_id, artifact_version_id),
            source.byte_size,
            source.sha256,
            self.clock(),
        )
        self._start(artifact_version_id, "copy_preparation", artifact_version_id, claim)
        self.evidence.begin_artifact(claim)
        try:
            readback = await asyncio.wait_for(
                self.sandbox.copy_exclusive_and_readback(
                    claim.sandbox_id,
                    claim.source_path,
                    claim.path,
                    self._budget(token),
                    token,
                ),
                timeout=self._budget(token),
            )
        except (Exception, asyncio.CancelledError) as exc:
            raise D3RecoveryRequired("d3_artifact_copy_outcome_unknown") from exc
        if not isinstance(readback, AttachmentImportReadback):
            raise D3RecoveryRequired("d3_artifact_copy_result_unknown")
        receipt = D3ArtifactReceipt(
            claim,
            readback,
            "confirmed"
            if readback == AttachmentImportReadback(source.byte_size, source.sha256)
            else "integrity_exception",
            max(claim.started_at, self.clock()),
        )
        self.evidence.save_artifact(receipt)
        self._finish(
            artifact_version_id, receipt, confirmed=receipt.status == "confirmed"
        )
        if receipt.status != "confirmed":
            raise D3RecoveryRequired("d3_artifact_integrity_exception")
        return receipt

    def record_size(
        self,
        plan: D3PlanBinding,
        execution_id: str,
        expected_byte_size: int,
    ) -> D3SizeObservation:
        artifact = self.read_artifact(plan.artifact_version_id)
        completed = self.commands.read_completed(execution_id)
        if (
            artifact.claim.plan_id != plan.plan_id
            or completed is None
            or completed.record.plan_id != plan.plan_id
            or completed.record.step_index != 0
            or completed.record.status != "completed"
            or completed.record.exit_code != 0
            or completed.record.stdout_truncated
        ):
            raise D3RecoveryRequired("d3_size_execution_unavailable")
        record: T6ExecutionRecord = completed.record
        observed_status, observed_size = parse_size_output(record.stdout)
        result = D3SizeObservation(
            plan.task_run_id,
            plan.plan_id,
            plan.artifact_version_id,
            execution_id,
            expected_byte_size,
            observed_status,
            observed_size,
            hashlib.sha256(record.stdout).hexdigest(),
            "confirmed"
            if observed_status == "present" and observed_size == expected_byte_size
            else "integrity_exception",
        )
        self.evidence.save_size(result)
        if result.status != "confirmed":
            raise D3RecoveryRequired("d3_size_integrity_exception")
        return result

    async def observe_digest(
        self,
        binding: D3TurnBinding,
        plan: D3PlanBinding,
        execution_id: str,
        observation_id: str,
        token: CancellationToken,
    ) -> D3DigestReceipt:
        artifact = self.read_artifact(plan.artifact_version_id)
        if (
            artifact.claim.plan_id != plan.plan_id
            or artifact.claim.turn_id != binding.turn_id
            or plan.observation_mode != "new_observation"
        ):
            raise D3RecoveryRequired("d3_digest_artifact_mismatch")
        existing = self.evidence.read_digest(observation_id)
        if existing is not None:
            self._repair_known(observation_id, "sha_observation", existing)
            record = self.ledger.load_d3_operation(observation_id)
            if (
                record is None
                or record.completion is None
                or record.completion.evidence_sha256 != digest(asdict(existing))
                or record.application_ref != observation_id
                or existing.claim.plan_id != plan.plan_id
                or existing.claim.execution_id != execution_id
                or existing.status != "confirmed"
            ):
                raise D3RecoveryRequired("d3_digest_evidence_mismatch")
            return existing
        if (
            self.evidence.read_digest_claim(observation_id) is not None
            or self.ledger.load_d3_operation(observation_id) is not None
        ):
            raise D3RecoveryRequired("d3_digest_outcome_unknown")
        completed = self.commands.read_completed(execution_id)
        if (
            completed is None
            or completed.record.status != "completed"
            or completed.record.exit_code != 0
            or completed.record.plan_id != plan.plan_id
        ):
            raise D3RecoveryRequired("d3_digest_execution_unavailable")
        self._budget(token)
        claim = D3DigestClaim(
            observation_id,
            plan.task_run_id,
            binding.turn_id,
            plan.intent_id,
            plan.plan_id,
            plan.artifact_version_id,
            execution_id,
            self.state.task_run.sandbox_id,
            artifact.claim.path,
            binding.expected_sha256,
            self.clock(),
        )
        self._start(observation_id, "sha_observation", observation_id, claim)
        self.evidence.begin_digest(claim)
        try:
            observed = await asyncio.wait_for(
                self.sandbox.hash_file(
                    plan.task_run_id,
                    claim.sandbox_id,
                    claim.path,
                    self._budget(token),
                    token,
                ),
                timeout=self._budget(token),
            )
        except (Exception, asyncio.CancelledError) as exc:
            raise D3RecoveryRequired("d3_digest_outcome_unknown") from exc
        if (
            not isinstance(observed, FileDigestObservation)
            or observed.status == "unknown"
        ):
            raise D3RecoveryRequired("d3_digest_outcome_unknown")
        receipt = D3DigestReceipt(
            claim,
            observed,
            "confirmed"
            if observed.status == "present" and observed.sha256 == claim.expected_sha256
            else "integrity_exception",
            max(claim.started_at, self.clock()),
        )
        self.evidence.save_digest(receipt)
        self._finish(observation_id, receipt, confirmed=receipt.status == "confirmed")
        if receipt.status != "confirmed":
            raise D3RecoveryRequired("d3_digest_integrity_exception")
        return receipt

    async def observe_mcp_result(
        self,
        binding: D3TurnBinding,
        plan: D3PlanBinding,
        execution_id: str,
        observation_id: str,
        mcp_result,
        token: CancellationToken,
    ) -> D3DigestReceipt:
        """
        D22: Record MCP tool execution result as observation.
        Uses same D3DigestReceipt structure but records MCP output instead of file hash.
        """
        artifact = self.read_artifact(plan.artifact_version_id)
        if (
            artifact.claim.plan_id != plan.plan_id
            or artifact.claim.turn_id != binding.turn_id
            or plan.observation_mode != "new_observation"
        ):
            raise D3RecoveryRequired("d22_mcp_artifact_mismatch")

        existing = self.evidence.read_digest(observation_id)
        if existing is not None:
            self._repair_known(observation_id, "mcp_observation", existing)
            return existing

        if (
            self.evidence.read_digest_claim(observation_id) is not None
            or self.ledger.load_d3_operation(observation_id) is not None
        ):
            raise D3RecoveryRequired("d22_mcp_outcome_unknown")

        self._budget(token)
        claim = D3DigestClaim(
            observation_id,
            plan.task_run_id,
            binding.turn_id,
            plan.intent_id,
            plan.plan_id,
            plan.artifact_version_id,
            execution_id,
            self.state.task_run.sandbox_id,
            mcp_result.tool_name,  # Store tool name in path field
            digest(mcp_result.output.encode() if isinstance(mcp_result.output, str) else mcp_result.output),
            self.clock(),
        )

        self._start(observation_id, "mcp_observation", observation_id, claim)
        self.evidence.begin_digest(claim)

        # MCP result is already synchronous, no async wait needed
        observed = FileDigestObservation(
            status="present",
            sha256=claim.expected_sha256,
            byte_size=len(mcp_result.output.encode() if isinstance(mcp_result.output, str) else mcp_result.output),
        )

        receipt = D3DigestReceipt(
            claim,
            observed,
            "confirmed",
            max(claim.started_at, self.clock()),
        )

        self.evidence.save_digest(receipt)
        self._finish(observation_id, receipt, confirmed=True)
        return receipt

    async def share(
        self,
        artifact_version_id: str,
        target_intent_id: str,
        share_id: str,
        token: CancellationToken,
    ) -> D3ShareReceipt:
        artifact = self.read_artifact(artifact_version_id)
        claim_source = artifact.claim
        state = self.state
        target = next(
            (item for item in state.intents if item.intent_id == target_intent_id), None
        )
        if (
            target is None
            or target.intent_id == claim_source.intent_id
            or target.turn_id not in {item.turn_id for item in state.turns}
            or claim_source.task_run_id != state.task_run.task_run_id
        ):
            raise D3RecoveryRequired("d3_share_target_mismatch")
        result = self.ledger.load_d3_plan_result(claim_source.plan_id)
        source_binding = self.ledger.load_d3_plan_binding(claim_source.plan_id)
        if (
            source_binding is None
            or source_binding.task_run_id != claim_source.task_run_id
            or source_binding.turn_id != claim_source.turn_id
            or source_binding.intent_id != claim_source.intent_id
            or source_binding.artifact_version_id != artifact_version_id
            or result is not None
            and result.artifact_version_id != artifact_version_id
        ):
            raise D3RecoveryRequired("d3_share_source_version_mismatch")
        original_verification_status: Literal["verified", "unverified"] = "unverified"
        source_proof_id = source_fact_id = None
        if result is not None and result.outcome == "satisfied":
            fact = next(
                (
                    item
                    for item in state.facts
                    if item.fact_id == result.supporting_fact_id
                ),
                None,
            )
            if (
                fact is None
                or fact.status is not FactStatus.VERIFIED
                or fact.verification_ref != f"d3:{result.proof_id}"
                or fact.source_ref is None
                or result.proof_id is None
                or result.supporting_fact_id is None
            ):
                raise D3RecoveryRequired("d3_share_source_fact_unavailable")
            proof, _ = self.proofs.read_verified(fact.source_ref, result.proof_id)
            if (
                proof.plan_id != claim_source.plan_id
                or proof.artifact_version_id != artifact_version_id
                or proof.intent_id != claim_source.intent_id
            ):
                raise D3RecoveryRequired("d3_share_source_proof_mismatch")
            original_verification_status = "verified"
            source_proof_id = result.proof_id
            source_fact_id = result.supporting_fact_id
        existing = self.evidence.read_share(share_id)
        if existing is not None:
            self._repair_known(share_id, "artifact_share", existing)
            if (
                existing.status != "confirmed"
                or existing.claim.artifact_version_id != artifact_version_id
                or existing.claim.target_intent_id != target_intent_id
            ):
                raise D3RecoveryRequired("d3_share_receipt_mismatch")
            return existing
        if (
            self.evidence.read_share_claim(share_id) is not None
            or self.ledger.load_d3_operation(share_id) is not None
        ):
            raise D3RecoveryRequired("d3_share_outcome_unknown")
        self._budget(token)
        claim = D3ShareClaim(
            share_id,
            claim_source.task_run_id,
            claim_source.turn_id,
            claim_source.intent_id,
            artifact_version_id,
            claim_source.path,
            claim_source.expected_byte_size,
            claim_source.expected_sha256,
            original_verification_status,
            target.turn_id,
            target_intent_id,
            shared_path(target_intent_id, share_id),
            self.clock(),
            source_proof_id,
            source_fact_id,
        )
        self._start(share_id, "artifact_share", share_id, claim)
        self.evidence.begin_share(claim)
        try:
            readback = await asyncio.wait_for(
                self.sandbox.share_exclusive_and_readback(
                    state.task_run.sandbox_id,
                    claim.source_path,
                    claim.target_path,
                    self._budget(token),
                    token,
                ),
                timeout=self._budget(token),
            )
        except (Exception, asyncio.CancelledError) as exc:
            raise D3RecoveryRequired("d3_share_outcome_unknown") from exc
        if not isinstance(readback, D3ShareReadback):
            raise D3RecoveryRequired("d3_share_outcome_unknown")
        expected = AttachmentImportReadback(claim.source_byte_size, claim.source_sha256)
        try:
            receipt = D3ShareReceipt(
                claim,
                readback.source,
                readback.target,
                "confirmed"
                if readback.source == readback.target == expected
                else "rejected",
                max(claim.started_at, self.clock()),
            )
        except (TypeError, ValueError) as exc:
            raise D3RecoveryRequired("d3_share_outcome_unknown") from exc
        self.evidence.save_share(receipt)
        self._finish(share_id, receipt, confirmed=receipt.status == "confirmed")
        if receipt.status != "confirmed":
            raise D3RecoveryRequired("d3_share_integrity_exception")
        return receipt

    async def verify_share(
        self,
        share_id: str,
        token: CancellationToken,
    ) -> D3ReadbackAudit:
        receipt = self.evidence.read_share(share_id)
        if receipt is None or receipt.status != "confirmed":
            raise D3RecoveryRequired("d3_share_receipt_unavailable")
        self._repair_known(share_id, "artifact_share", receipt)
        claim = receipt.claim
        artifact = self.read_artifact(claim.artifact_version_id)
        source = artifact.claim
        if (
            claim.task_run_id != self.state.task_run.task_run_id
            or claim.source_turn_id != source.turn_id
            or claim.source_intent_id != source.intent_id
            or claim.source_path != source.path
            or claim.source_byte_size != source.expected_byte_size
            or claim.source_sha256 != source.expected_sha256
            or not any(
                item.intent_id == claim.target_intent_id
                and item.turn_id == claim.target_turn_id
                for item in self.state.intents
            )
        ):
            raise D3RecoveryRequired("d3_share_manifest_owner_changed")
        if claim.original_verification_status == "verified":
            result = self.ledger.load_d3_plan_result(source.plan_id)
            fact = next(
                (
                    item
                    for item in self.state.facts
                    if item.fact_id == claim.source_fact_id
                ),
                None,
            )
            if (
                result is None
                or result.outcome != "satisfied"
                or result.artifact_version_id != claim.artifact_version_id
                or result.proof_id != claim.source_proof_id
                or result.supporting_fact_id != claim.source_fact_id
                or fact is None
                or fact.status is not FactStatus.VERIFIED
                or fact.verification_ref != f"d3:{claim.source_proof_id}"
            ):
                raise D3RecoveryRequired("d3_share_original_verification_changed")
            proof, _ = self.proofs.read_verified(fact.source_ref, claim.source_proof_id)
            if (
                proof.plan_id != source.plan_id
                or proof.intent_id != source.intent_id
                or proof.artifact_version_id != claim.artifact_version_id
            ):
                raise D3RecoveryRequired("d3_share_original_proof_changed")
        try:
            source_readback = await asyncio.wait_for(
                self.sandbox.readback(
                    self.state.task_run.sandbox_id,
                    claim.source_path,
                    self._read_budget(token),
                    token,
                ),
                timeout=self._read_budget(token),
            )
            target_readback = await asyncio.wait_for(
                self.sandbox.readback(
                    self.state.task_run.sandbox_id,
                    claim.target_path,
                    self._read_budget(token),
                    token,
                ),
                timeout=self._read_budget(token),
            )
        except (Exception, asyncio.CancelledError) as exc:
            raise D3RecoveryRequired("d3_share_readback_unavailable") from exc
        audit = D3ReadbackAudit(
            str(uuid4()),
            share_id,
            claim.task_run_id,
            "artifact_share",
            source_readback,
            target_readback,
            None,
            self.clock(),
        )
        self.evidence.save_readback_audit(audit)
        if source_readback != receipt.source or target_readback != receipt.target:
            raise D3RecoveryRequired("d3_share_content_changed")
        return audit

    async def audit_unknown(
        self,
        operation_id: str,
        token: CancellationToken,
    ) -> D3ReadbackAudit:
        operation = self.ledger.load_d3_operation(operation_id)
        if (
            operation is None
            or operation.completion is not None
            or operation.start.category
            not in {
                "copy_preparation",
                "artifact_share",
                "intent_stop",
            }
        ):
            raise D3RecoveryRequired("d3_unknown_operation_claim_unavailable")
        category = operation.start.category
        if operation.start.task_run_id != self.state.task_run.task_run_id:
            raise D3RecoveryRequired("d3_unknown_operation_owner_mismatch")
        source_readback = target_readback = None
        process_readback = None
        try:
            if category == "copy_preparation":
                claim = self.evidence.read_artifact_claim(operation_id)
                if (
                    claim is None
                    or self.evidence.read_artifact(operation_id) is not None
                ):
                    raise D3RecoveryRequired("d3_artifact_result_not_unknown")
                plan = self.ledger.load_d3_plan_binding(claim.plan_id)
                turn = plan_turn_binding(self.ledger,plan) if plan is not None else None
                intent = next(
                    (
                        item
                        for item in self.state.intents
                        if item.intent_id == claim.intent_id
                    ),
                    None,
                )
                if (
                    claim.task_run_id != operation.start.task_run_id
                    or operation.start.context_sha256 != digest(asdict(claim))
                    or operation.start.resource_ref != operation_id
                    or turn is None
                    or plan is None
                    or intent is None
                    or intent.turn_id != claim.turn_id
                    or plan.task_run_id != claim.task_run_id
                    or plan.turn_id != claim.turn_id
                    or plan.intent_id != claim.intent_id
                    or plan.artifact_version_id != operation_id
                    or claim.import_id != turn.import_id
                    or claim.attachment_index != turn.attachment_index
                    or claim.expected_byte_size != turn.expected_byte_size
                    or claim.expected_sha256 != turn.expected_sha256
                    or claim.sandbox_id != self.state.task_run.sandbox_id
                    or claim.source_path != self._import(turn).sandbox_path
                ):
                    raise D3RecoveryRequired("d3_unknown_artifact_claim_changed")
                source_path, target_path = claim.source_path, claim.path
            elif category == "artifact_share":
                claim = self.evidence.read_share_claim(operation_id)
                if claim is None or self.evidence.read_share(operation_id) is not None:
                    raise D3RecoveryRequired("d3_share_result_not_unknown")
                artifact = self.read_artifact(claim.artifact_version_id)
                source = artifact.claim
                target = next(
                    (
                        item
                        for item in self.state.intents
                        if item.intent_id == claim.target_intent_id
                    ),
                    None,
                )
                if (
                    claim.task_run_id != operation.start.task_run_id
                    or operation.start.context_sha256 != digest(asdict(claim))
                    or operation.start.resource_ref != operation_id
                    or claim.source_turn_id != source.turn_id
                    or claim.source_intent_id != source.intent_id
                    or claim.source_path != source.path
                    or claim.source_byte_size != source.expected_byte_size
                    or claim.source_sha256 != source.expected_sha256
                    or target is None
                    or target.turn_id != claim.target_turn_id
                ):
                    raise D3RecoveryRequired("d3_unknown_share_claim_changed")
                source_path, target_path = claim.source_path, claim.target_path
            else:
                claim = self.evidence.read_stop_claim(operation_id)
                saved_stop = self.evidence.read_stop(operation_id)
                if (
                    claim is None
                    or saved_stop is not None
                    and (saved_stop.claim != claim or saved_stop.status != "unknown")
                ):
                    raise D3RecoveryRequired("d3_stop_result_not_unknown")
                intent = next(
                    (
                        item
                        for item in self.state.intents
                        if item.intent_id == claim.intent_id
                    ),
                    None,
                )
                review = self.ledger.load_d3_review_application(
                    claim.task_run_id,
                    claim.review_id,
                )
                process_refs = tuple(
                    execution.execution_id
                    for execution in self.state.executions
                    if any(
                        plan.plan_id == execution.plan_id
                        and plan.intent_id == claim.intent_id
                        for plan in self.state.plans
                    )
                )
                if (
                    claim.task_run_id != operation.start.task_run_id
                    or operation.start.context_sha256 != digest(asdict(claim))
                    or operation.start.resource_ref != claim.intent_id
                    or intent is None
                    or intent.turn_id != claim.turn_id
                    or review is None
                    or review.paused_gate
                    or review.intent_id != claim.intent_id
                    or review.turn_id != claim.turn_id
                    or review.decision
                    != (
                        "switch_sibling_intent"
                        if claim.reason == "sibling_switch"
                        else "adjust_turn_target"
                    )
                    or claim.process_refs != process_refs
                ):
                    raise D3RecoveryRequired("d3_unknown_stop_claim_changed")
                process_readback = await asyncio.wait_for(
                    self.sandbox.query_intent_processes(
                        claim,
                        self.state.task_run.sandbox_id,
                        self._read_budget(token),
                        token,
                    ),
                    timeout=self._read_budget(token),
                )
            if category != "intent_stop":
                source_readback = await asyncio.wait_for(
                    self.sandbox.readback(
                        self.state.task_run.sandbox_id,
                        source_path,
                        self._read_budget(token),
                        token,
                    ),
                    timeout=self._read_budget(token),
                )
                target_readback = await asyncio.wait_for(
                    self.sandbox.readback(
                        self.state.task_run.sandbox_id,
                        target_path,
                        self._read_budget(token),
                        token,
                    ),
                    timeout=self._read_budget(token),
                )
        except D3RecoveryRequired:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            raise D3RecoveryRequired(
                "d3_unknown_operation_readback_unavailable"
            ) from exc
        audit = D3ReadbackAudit(
            str(uuid4()),
            operation_id,
            operation.start.task_run_id,
            category,
            source_readback,
            target_readback,
            process_readback,
            self.clock(),
        )
        self.evidence.save_readback_audit(audit)
        return audit

    async def stop_intent(
        self,
        turn_id: str,
        intent_id: str,
        review_id: str,
        reason: Literal["sibling_switch", "turn_target_adjusted"],
        stop_id: str,
        token: CancellationToken,
    ) -> D3StopReceipt:
        state = self.state
        current = next(
            (item for item in state.intents if item.intent_id == intent_id), None
        )
        review = self.ledger.load_d3_review_application(
            state.task_run.task_run_id, review_id
        )
        if (
            current is None
            or current.turn_id != turn_id
            or current.status is not IntentStatus.RUNNING
            or review is None
            or review.paused_gate
            or review.intent_id != intent_id
            or review.turn_id != turn_id
            or review.decision not in (
                ({"switch_sibling_intent", "abandon_current_intent"}
                 if self.ledger.load_d3_run_config(state.task_run.task_run_id).contract_version in {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}
                 else {"switch_sibling_intent"})
                if reason == "sibling_switch" else {"adjust_turn_target"}
            )
        ):
            raise D3RecoveryRequired("d3_stop_review_or_owner_mismatch")
        if any(
            execution.status.value in {"pending", "running"}
            and any(
                plan.plan_id == execution.plan_id and plan.intent_id == intent_id
                for plan in state.plans
            )
            for execution in state.executions
        ):
            raise D3RecoveryRequired("d3_intent_has_pending_execution")
        process_refs = tuple(
            execution.execution_id
            for execution in state.executions
            if any(
                plan.plan_id == execution.plan_id and plan.intent_id == intent_id
                for plan in state.plans
            )
        )
        existing = self.evidence.read_stop(stop_id)
        if existing is not None:
            if existing.status == "confirmed":
                self._repair_known(stop_id, "intent_stop", existing)
            if (
                existing.status != "confirmed"
                or existing.claim.intent_id != intent_id
                or existing.claim.review_id != review_id
                or existing.claim.process_refs != process_refs
            ):
                raise D3RecoveryRequired("d3_stop_receipt_mismatch")
            return existing
        if (
            self.evidence.read_stop_claim(stop_id) is not None
            or self.ledger.load_d3_operation(stop_id) is not None
        ):
            raise D3RecoveryRequired("d3_stop_outcome_unknown")
        self._budget(token)
        claim = D3StopClaim(
            stop_id,
            state.task_run.task_run_id,
            turn_id,
            intent_id,
            review_id,
            reason,
            process_refs,
            self.clock(),
        )
        self._start(stop_id, "intent_stop", intent_id, claim)
        self.evidence.begin_stop(claim)
        try:
            readback = await asyncio.wait_for(
                self.sandbox.stop_intent_processes(
                    claim,
                    state.task_run.sandbox_id,
                    self._budget(token),
                    token,
                ),
                timeout=self._budget(token),
            )
        except (Exception, asyncio.CancelledError) as exc:
            raise D3RecoveryRequired("d3_stop_outcome_unknown") from exc
        if not isinstance(readback, D3StopReadback):
            raise D3RecoveryRequired("d3_stop_outcome_unknown")
        try:
            receipt = D3StopReceipt(
                claim,
                readback,
                "confirmed" if not readback.unknown_refs else "unknown",
                max(claim.started_at, self.clock()),
            )
        except (TypeError, ValueError) as exc:
            raise D3RecoveryRequired("d3_stop_outcome_unknown") from exc
        self.evidence.save_stop(receipt)
        if receipt.status != "confirmed":
            raise D3RecoveryRequired("d3_stop_outcome_unknown")
        self._finish(stop_id, receipt, confirmed=True)
        return receipt
