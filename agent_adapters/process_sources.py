"""Read D5 source receipts without creating or changing run evidence."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.model_evidence import FileD3ModelEvidence
from agent_adapters.observation_proof_store import FileD3ProofStore
from agent_adapters.workspace_evidence import FileD3WorkspaceEvidence
from agent_core.command_contracts import T6EvidenceError, T6ExecutionRecord
from agent_core.control_candidate import validate_control_ownership
from agent_core.control_store import ControlStateRows, StorageError
from agent_core.dual_llm_contracts import D3PlanBinding, D3RecoveryRequired
from agent_core.process_facts import D5Source, D5SourceKind, d5_digest


class D5SourceCorrupt(ValueError):
    """A referenced source exists but fails its typed or immutable bindings."""


_SOURCE_KINDS = (
    "execution",
    "digest_observation",
    "observation_proof",
    "execution_rejection",
)
_ACTION_STAGES = {"inner_size", "inner_sha256", "inner_complete"}


def _typed_digest(value: object) -> str:
    return d5_digest(asdict(cast(Any, value)) if is_dataclass(value) else value)


def _execution_digest(record: T6ExecutionRecord) -> str:
    """Hash a full command receipt using the same encoding as its D3 ledger."""
    payload = asdict(record)
    payload["stdout"] = base64.b64encode(record.stdout).decode("ascii")
    payload["stderr"] = base64.b64encode(record.stderr).decode("ascii")
    return d5_digest(payload)


def _source_content_digest(value: Any) -> str:
    """Bind binary output by digest and byte count, without retaining its text."""
    payload = asdict(value)
    for field in ("stdout", "stderr"):
        if field not in payload:
            continue
        raw = payload.pop(field)
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
        payload[field + "_sha256"] = hashlib.sha256(data).hexdigest()
        payload[field + "_bytes"] = len(data)
    return d5_digest(payload)


class D5SourceReader:
    """Resolve source IDs against owned control rows and immutable receipts."""

    def __init__(self, directory: Path, store) -> None:
        self.directory = Path(directory)
        self.store = store
        self.commands = T6CommandRecorder(self.directory / "commands", create=False)
        self.workspace = FileD3WorkspaceEvidence(
            self.directory / "d3-workspace", create=False
        )
        self.proofs = FileD3ProofStore(self.directory / "d3-proofs", create=False)
        self.models = FileD3ModelEvidence(self.directory / "d3-models", create=False)

    def _rows_for(self, task_run_id: str) -> ControlStateRows | None:
        rows = self.store.load_control_state_rows(task_run_id)
        if rows is not None:
            if rows.task_run.task_run_id != task_run_id:
                raise D5SourceCorrupt("source_task_run_mismatch")
            validate_control_ownership(rows)
        return rows

    def _binding(self, task_run_id: str, plan_id: str):
        rows = self._rows_for(task_run_id)
        if rows is None or not any(item.plan_id == plan_id for item in rows.plans):
            raise D5SourceCorrupt("source_plan_owner_mismatch")
        binding = self.store.load_d3_plan_binding(plan_id)
        if binding is None or binding.task_run_id != task_run_id:
            raise D5SourceCorrupt("source_plan_binding_unavailable")
        return binding

    def _d3_operation(self, task_run_id: str, operation_id: str, category: str):
        operation = self.store.load_d3_operation(operation_id)
        if operation is None:
            return None
        if (
            operation.start.task_run_id != task_run_id
            or operation.start.operation_id != operation_id
            or operation.start.category != category
        ):
            raise D5SourceCorrupt("source_operation_owner_or_kind_mismatch")
        return operation

    def _d4_operation(self, task_run_id: str, operation_id: str, d3_operation):
        loader = getattr(self.store, "load_d4_operation", None)
        if loader is None:
            return None
        operation = loader(operation_id)
        if operation is None:
            return None
        expected_category = {"outer_model": "planning", "inner_model": "action"}.get(
            d3_operation.start.category, d3_operation.start.category
        )
        if (
            operation.start.task_run_id != task_run_id
            or operation.start.operation_id != operation_id
            or operation.start.category != expected_category
            or operation.start.resource_ref != d3_operation.start.resource_ref
        ):
            raise D5SourceCorrupt("source_d4_operation_mismatch")
        if operation.completion is None:
            return None
        if d3_operation.completion is None or (
            asdict(operation.completion) != asdict(d3_operation.completion)
        ):
            raise D5SourceCorrupt("source_d4_completion_mismatch")
        return operation

    @staticmethod
    def _check_completion(operation, *, result_kinds: set[str], ref: str, sha: str):
        completion = operation.completion
        if completion is None:
            return False
        if (
            completion.operation_id != operation.start.operation_id
            or completion.result_kind not in result_kinds
            or completion.evidence_ref != ref
            or completion.evidence_sha256 != sha
        ):
            raise D5SourceCorrupt("source_completion_digest_mismatch")
        return True

    def _execution_target(self, task_run_id: str, object_id: str):
        rows = self._rows_for(task_run_id)
        if rows is None:
            return None
        matches = [
            item
            for item in rows.executions
            if object_id in {item.execution_id, item.output_ref, item.command_ref}
        ]
        if not matches:
            command = self.commands.read_command(object_id)
            request_id = command.request_id if command is not None else object_id
            matches = [
                item for item in rows.executions if item.execution_id == request_id
            ]
        if not matches:
            operations = self.store.list_d3_operations(task_run_id)
            operation_ids = {
                item.start.operation_id
                for item in operations
                if item.start.category == "tool_dispatch"
                and object_id in {item.start.operation_id, item.start.resource_ref}
            }
            matches = [
                item for item in rows.executions if item.execution_id in operation_ids
            ]
        if len(matches) > 1:
            raise D5SourceCorrupt("source_execution_id_ambiguous")
        return None if not matches else (rows, matches[0])

    def _read_execution(self, task_run_id: str, object_id: str):
        target = self._execution_target(task_run_id, object_id)
        if target is None:
            return None
        rows, execution = target
        completed = self.commands.read_completed(execution.execution_id)
        if completed is None:
            return None
        record = completed.record
        if record.status == "outcome_unknown":
            return None
        if (
            record.request_id != execution.execution_id
            or record.task_run_id != task_run_id
            or record.plan_id != execution.plan_id
            or record.step_index != execution.step_index
            or record.command_ref != execution.command_ref
            or execution.output_ref != execution.execution_id
            or execution.status.value in {"pending", "running"}
        ):
            raise D5SourceCorrupt("source_execution_receipt_mismatch")
        operation = self._d3_operation(
            task_run_id, execution.execution_id, "tool_dispatch"
        )
        if operation is None:
            return None
        d3_digest = _execution_digest(record)
        successful = record.status == "completed" and record.exit_code == 0
        if not self._check_completion(
            operation,
            result_kinds={"completed" if successful else "failed"},
            ref=execution.execution_id,
            sha=d3_digest,
        ):
            return None
        d4_operation = self._d4_operation(
            task_run_id, execution.execution_id, operation
        )
        if (
            self.store.load_d4_operation(execution.execution_id) is not None
            and d4_operation is None
        ):
            return None
        binding = self._binding(task_run_id, execution.plan_id)
        receipt_hash = _source_content_digest(record)
        source = self._make_source("execution", object_id, receipt_hash, binding)
        record_status = record.status
        if record_status not in {
            "completed",
            "step_timeout",
            "environment_fault",
            "policy_rejected",
        }:
            return None
        content = {
            "operation_type": "tool_dispatch",
            "execution_id": execution.execution_id,
            "plan_id": execution.plan_id,
            "step_index": execution.step_index,
            "status": record_status,
            "exit_code": record.exit_code,
            "stdout_bytes": len(record.stdout),
            "stdout_sha256": hashlib.sha256(record.stdout).hexdigest(),
            "stderr_bytes": len(record.stderr),
            "stderr_sha256": hashlib.sha256(record.stderr).hexdigest(),
            "timed_out": record.timed_out,
            "finished_at": record.finished_at,
            "summary": (
                "The execution completed with exit code zero."
                if successful
                else "The execution produced a recorded non-success result."
            ),
        }
        origin = self._origin(
            execution=execution,
            binding=binding,
            rows=rows,
        )
        origin["fact_links"] = self._fact_links(rows, {execution.execution_id}, set())
        return source, content, origin

    def _read_digest(self, task_run_id: str, object_id: str):
        receipt = self.workspace.read_digest(object_id)
        if receipt is None:
            return None
        claim = receipt.claim
        if claim.task_run_id != task_run_id:
            raise D5SourceCorrupt("source_digest_owner_mismatch")
        operation = self._d3_operation(task_run_id, object_id, "sha_observation")
        if operation is None:
            return None
        completion_digest = _typed_digest(receipt)
        if not self._check_completion(
            operation,
            result_kinds={"completed" if receipt.status == "confirmed" else "failed"},
            ref=object_id,
            sha=completion_digest,
        ):
            return None
        d4_operation = self._d4_operation(task_run_id, object_id, operation)
        if self.store.load_d4_operation(object_id) is not None and d4_operation is None:
            return None
        binding = self._binding(task_run_id, claim.plan_id)
        if (
            binding.intent_id != claim.intent_id
            or binding.turn_id != claim.turn_id
            or binding.artifact_version_id != claim.artifact_version_id
            or operation.start.resource_ref != object_id
        ):
            raise D5SourceCorrupt("source_digest_binding_mismatch")
        execution = self._read_execution(task_run_id, claim.execution_id)
        if execution is None:
            return None
        _execution_source, _execution_content, _execution_origin = execution
        if (
            _execution_origin["plan_id"] != claim.plan_id
            or _execution_origin["artifact_version_id"] != claim.artifact_version_id
        ):
            raise D5SourceCorrupt("source_digest_execution_mismatch")
        source = self._make_source(
            "digest_observation", object_id, completion_digest, binding
        )
        observed = receipt.observed
        content = {
            "operation_type": "sha256_observation",
            "observation_id": object_id,
            "plan_id": claim.plan_id,
            "artifact_version_id": claim.artifact_version_id,
            "status": receipt.status,
            "observed_status": observed.status,
            "byte_size": None,
            "sha256": observed.sha256,
            "rule_version": source.rule_version,
            "summary": (
                "The artifact SHA-256 matched the bound expectation."
                if receipt.status == "confirmed"
                else "The artifact SHA-256 observation recorded an integrity exception."
            ),
        }
        rows = self._rows_for(task_run_id)
        origin = self._origin(binding=binding, rows=rows)
        origin["fact_links"] = self._fact_links(rows, {claim.execution_id}, set())
        return source, content, origin

    def _read_proof(self, task_run_id: str, object_id: str):
        proof = self.proofs.read(object_id)
        if proof is None:
            return None
        if proof.task_run_id != task_run_id:
            raise D5SourceCorrupt("source_proof_owner_mismatch")
        binding = self._binding(task_run_id, proof.plan_id)
        turn = self.store.load_d3_turn_binding(binding.turn_id)
        if (
            turn is None
            or turn.task_run_id != task_run_id
            or turn.turn_id != binding.turn_id
            or _typed_digest(turn) != binding.rule_binding_sha256
        ):
            raise D5SourceCorrupt("source_proof_turn_binding_mismatch")
        receipt = self.workspace.read_digest(proof.observation_id)
        result = self.store.load_d3_plan_result(proof.plan_id)
        if receipt is None or result is None:
            return None
        if (
            proof.turn_id != binding.turn_id
            or proof.intent_id != binding.intent_id
            or proof.artifact_version_id != binding.artifact_version_id
            or proof.rule_binding_sha256 != binding.rule_binding_sha256
            or proof.source_execution_id != receipt.claim.execution_id
            or proof.observed_sha256 != receipt.observed.sha256
            or result.task_run_id != task_run_id
            or result.plan_id != proof.plan_id
            or result.proof_id != proof.proof_id
            or result.observation_id != proof.observation_id
            or result.artifact_version_id != proof.artifact_version_id
            or result.outcome != "satisfied"
            or result.evidence_sha256 != _typed_digest(receipt)
        ):
            raise D5SourceCorrupt("source_proof_binding_mismatch")
        rows = self._rows_for(task_run_id)
        if rows is None:
            raise D5SourceCorrupt("source_proof_task_run_missing")
        fact = next(
            (item for item in rows.facts if item.fact_id == result.supporting_fact_id),
            None,
        )
        if (
            fact is None
            or fact.status.value != "verified"
            or fact.turn_id != proof.turn_id
            or fact.source_ref != proof.source_execution_id
            or fact.verification_ref != f"d3:{proof.proof_id}"
        ):
            raise D5SourceCorrupt("source_proof_fact_link_mismatch")
        source = self._make_source(
            "observation_proof", proof.proof_id, _typed_digest(proof), binding
        )
        content = {
            "operation_type": "verified_sha256_proof",
            "proof_id": proof.proof_id,
            "plan_id": proof.plan_id,
            "artifact_version_id": proof.artifact_version_id,
            "status": "verified",
            "byte_size": turn.expected_byte_size,
            "sha256": proof.observed_sha256,
            "rule_version": source.rule_version,
            "summary": "The typed proof binds the SHA-256 result to this artifact version.",
        }
        origin = self._origin(binding=binding, rows=rows)
        origin["fact_links"] = self._fact_links(
            rows, {proof.source_execution_id}, {f"d3:{proof.proof_id}"}
        )
        return source, content, origin

    def _read_rejection(self, task_run_id: str, object_id: str):
        evidence = self.models.read(object_id)
        rejection = self.models.read_rejection(object_id)
        if evidence is None or rejection is None:
            return None
        if (
            evidence.task_run_id != task_run_id
            or evidence.model_request_id != object_id
            or evidence.outcome != "accepted"
            or evidence.stage not in _ACTION_STAGES
            or evidence.correction_of_model_request_id is not None
            or rejection.model_request_id != object_id
        ):
            return None
        operation = self._d3_operation(task_run_id, object_id, "inner_model")
        if operation is None:
            return None
        evidence_digest = _typed_digest(evidence)
        if not self._check_completion(
            operation,
            result_kinds={"completed"},
            ref=object_id,
            sha=evidence_digest,
        ):
            return None
        d4_operation = self._d4_operation(task_run_id, object_id, operation)
        if self.store.load_d4_operation(object_id) is not None and d4_operation is None:
            return None
        receipt = {
            "model_evidence": asdict(evidence),
            "model_rejection": asdict(rejection),
            "d3_operation": asdict(operation),
            "d4_operation": asdict(d4_operation) if d4_operation is not None else None,
        }
        source = self._make_source(
            "execution_rejection", object_id, d5_digest(receipt), None
        )
        rows = self._rows_for(task_run_id)
        if rows is None:
            raise D5SourceCorrupt("source_rejection_task_run_missing")
        resource_ref = evidence.resource_ref
        plan_id = resource_ref.split(":", 1)[0]
        binding = None
        if any(item.plan_id == plan_id for item in rows.plans):
            binding = self._binding(task_run_id, plan_id)
            if binding.intent_id not in {
                item.intent_id
                for item in rows.intents
                if item.turn_id == binding.turn_id
            }:
                raise D5SourceCorrupt("source_rejection_intent_mismatch")
        intent_id = (
            binding.intent_id
            if binding is not None
            else self._intent_from_model_context(rows, evidence)
        )
        turn_id = (
            binding.turn_id
            if binding is not None
            else self._turn_for_intent(rows, intent_id)
        )
        content = {
            "operation_type": "accepted_action_rejected_by_policy",
            "model_request_id": object_id,
            "stage": evidence.stage,
            "status": "rejected",
            "summary": "A persisted accepted action proposal was explicitly rejected before dispatch.",
        }
        origin = {
            "turn_id": turn_id,
            "intent_id": intent_id,
            "plan_id": binding.plan_id if binding is not None else None,
            "artifact_version_id": binding.artifact_version_id
            if binding is not None
            else None,
            "rule_binding_sha256": binding.rule_binding_sha256
            if binding is not None
            else None,
            "fact_links": self._fact_links(rows, set(), set()),
        }
        return source, content, origin

    def _intent_from_model_context(self, rows, evidence) -> str | None:
        plan_id = evidence.resource_ref.split(":", 1)[0]
        binding = self.store.load_d3_plan_binding(plan_id)
        return binding.intent_id if binding is not None else None

    @staticmethod
    def _turn_for_intent(rows, intent_id: str | None) -> str | None:
        if intent_id is None:
            return None
        intent = next(
            (item for item in rows.intents if item.intent_id == intent_id), None
        )
        if intent is None:
            raise D5SourceCorrupt("source_rejection_intent_owner_mismatch")
        return intent.turn_id

    @staticmethod
    def _make_source(
        kind: D5SourceKind, object_id: str, content_sha256: str, binding
    ) -> D5Source:
        artifact_id = binding.artifact_version_id if binding is not None else None
        return D5Source(
            kind=kind,
            object_id=object_id,
            content_sha256=content_sha256,
            subject_ref=artifact_id or object_id,
            observation_scope="artifact_file" if artifact_id else "intent_execution",
            artifact_version_id=artifact_id,
            rule_version=1 if artifact_id else None,
        )

    @staticmethod
    def _origin(*, rows, binding=None, execution=None):
        return {
            "turn_id": binding.turn_id if binding else None,
            "intent_id": binding.intent_id if binding else None,
            "plan_id": binding.plan_id if binding else None,
            "artifact_version_id": binding.artifact_version_id if binding else None,
            "rule_binding_sha256": binding.rule_binding_sha256 if binding else None,
            "execution_id": execution.execution_id if execution else None,
            "fact_links": [],
        }

    @staticmethod
    def _fact_links(rows, execution_ids: set[str], verification_refs: set[str]):
        if rows is None:
            return []
        return [
            {
                "fact_id": item.fact_id,
                "turn_id": item.turn_id,
                "source_ref": item.source_ref,
                "verification_ref": item.verification_ref,
                "status": item.status.value,
            }
            for item in rows.facts
            if item.source_ref in execution_ids
            or item.verification_ref in verification_refs
        ]

    def _resolve(self, task_run_id: str, object_id: str, kind: str):
        if kind == "execution":
            return self._read_execution(task_run_id, object_id)
        if kind == "digest_observation":
            return self._read_digest(task_run_id, object_id)
        if kind == "observation_proof":
            return self._read_proof(task_run_id, object_id)
        if kind == "execution_rejection":
            return self._read_rejection(task_run_id, object_id)
        raise ValueError("unsupported_d5_source_kind")

    def read(
        self, task_run_id: str, object_id: str, kind: str | None = None
    ) -> D5Source | None:
        if kind is not None:
            resolved = self._resolve(task_run_id, object_id, kind)
            return None if resolved is None else resolved[0]
        matches = []
        corrupt: D5SourceCorrupt | None = None
        for candidate_kind in _SOURCE_KINDS:
            try:
                resolved = self._resolve(task_run_id, object_id, candidate_kind)
            except D5SourceCorrupt as exc:
                corrupt = corrupt or exc
                continue
            if resolved is not None:
                matches.append(resolved[0])
        if len(matches) > 1:
            raise D5SourceCorrupt("source_reference_ambiguous")
        if matches:
            return matches[0]
        if corrupt is not None:
            raise corrupt
        return None

    def project(self, task_run_id: str, source: D5Source) -> dict[str, Any]:
        base = {
            "source": asdict(source),
            "version_key": source.version_key,
            "status": "missing",
            "applicability": None,
            "content": None,
            "origin": None,
        }
        try:
            resolved = self._resolve(task_run_id, source.object_id, source.kind)
        except D5SourceCorrupt:
            return {**base, "status": "corrupt"}
        except (
            D3RecoveryRequired,
            T6EvidenceError,
            StorageError,
            sqlite3.Error,
            OSError,
            TypeError,
            ValueError,
        ):
            return {**base, "status": "corrupt"}
        if resolved is None:
            return base
        actual_source, content, origin = resolved
        if asdict(actual_source) != asdict(source):
            return {**base, "status": "corrupt", "origin": origin}
        try:
            applicability = self._applicability(task_run_id, source, origin)
        except (D5SourceCorrupt, TypeError, ValueError):
            return {**base, "status": "corrupt", "origin": origin}
        if content is None:
            return {
                **base,
                "status": "withheld",
                "applicability": applicability,
                "origin": origin,
            }
        return {
            **base,
            "status": "available",
            "applicability": applicability,
            "content": content,
            "origin": origin,
        }

    def _applicability(self, task_run_id: str, source: D5Source, origin: dict):
        rows = self._rows_for(task_run_id)
        if rows is None:
            return "historical"
        plan_by_id = {item.plan_id: item for item in rows.plans}
        origin_plan_id = origin.get("plan_id")
        origin_binding = (
            self.store.load_d3_plan_binding(origin_plan_id)
            if isinstance(origin_plan_id, str)
            else None
        )
        if origin_binding is not None and (
            origin_binding.task_run_id != task_run_id
            or origin_binding.plan_id not in plan_by_id
        ):
            raise D5SourceCorrupt("source_plan_binding_owner_mismatch")
        intent_id = (
            origin_binding.intent_id
            if origin_binding is not None
            else origin.get("intent_id")
        )
        if not isinstance(intent_id, str):
            return "historical"

        # A completed Intent remains current until a later Plan for that same
        # Intent binds a different artifact version or rule set. Reusing a saved
        # proof keeps the original object and rules current across Plan IDs.
        related_bindings: list[D3PlanBinding] = []
        for plan in rows.plans:
            if plan.intent_id != intent_id:
                continue
            binding = self.store.load_d3_plan_binding(plan.plan_id)
            if binding is None:
                continue
            if (
                binding.task_run_id != task_run_id
                or binding.intent_id != intent_id
                or binding.plan_id != plan.plan_id
            ):
                raise D5SourceCorrupt("source_related_plan_binding_mismatch")
            related_bindings.append(binding)
        if not related_bindings:
            return "historical"

        if origin_binding is None:
            origin_binding = next(
                (
                    binding
                    for binding in related_bindings
                    if binding.plan_id == origin_plan_id
                    or source.artifact_version_id is not None
                    and binding.artifact_version_id == source.artifact_version_id
                ),
                None,
            )
        if origin_binding is None:
            return "historical"
        latest_binding = related_bindings[-1]
        if source.artifact_version_id is not None and (
            origin_binding.artifact_version_id != source.artifact_version_id
        ):
            raise D5SourceCorrupt("source_artifact_binding_mismatch")
        return (
            "current"
            if (
                origin_binding.artifact_version_id == latest_binding.artifact_version_id
                and origin_binding.rule_binding_sha256
                == latest_binding.rule_binding_sha256
            )
            else "historical"
        )
