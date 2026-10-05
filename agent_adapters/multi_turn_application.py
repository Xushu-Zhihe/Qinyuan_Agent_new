"""Internal bounded D3 multi-path application; no public CLI integration."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast
from uuid import uuid4

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.cross_run_artifacts import (
    CrossTaskRunArtifactStore,
    D6ArtifactImportError,
    D6ArtifactImportRecoveryRequired,
)
from agent_adapters.cross_run_transfer import D6ArtifactTransfer
from agent_adapters.d4_review_ledger import D4ReviewLedger
from agent_adapters.model_evidence import FileD3ModelEvidence
from agent_adapters.observation_proof_store import FileD3ProofStore
from agent_adapters.process_fact_evidence import D5EvidenceError, FileD5Evidence
from agent_adapters.process_fact_model import D5RedactingModel
from agent_adapters.process_sources import D5SourceReader
from agent_adapters.review_ledger import FileD2ReviewLedger
from agent_adapters.run_files import run_lock
from agent_adapters.workspace_evidence import FileD3WorkspaceEvidence
from agent_core.actions import CompleteIntentAction, ProbeAction
from agent_core.artifact_operations import D3WorkspaceOperations, fixed_size_script
from agent_core.artifact_proofs import D3ObservationProofVerifier
from agent_core.attachment_import import (
    AttachmentImportConflict,
    AttachmentImportRecoveryRequired,
    AttachmentImportRejected,
    AttachmentImportRequest,
    AttachmentImportStorageError,
)
from agent_core.attachment_importer import AttachmentImporter
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.command_contracts import (
    SandboxCommandAuthorization,
    SandboxCommandRequest,
)
from agent_core.contracts import Attachment, ModelBackend, TaskRequest, TaskRequestError
from agent_core.control_evidence_validator import D3EvidenceValidator
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import StorageError
from agent_core.controller import (
    CommittedStateReloadError,
    SandboxCleanupError,
    TaskRunController,
    TaskRunControllerRegistry,
)
from agent_core.d4_contracts import (
    D4AuditEvent,
    D4BudgetExhausted,
    D4RecoveryRequired,
)
from agent_core.d6_contracts import D6ArtifactReference
from agent_core.domain import FactStatus, IntentStatus, PlanStatus, TurnStatus
from agent_core.dual_llm_contracts import (
    D3BudgetExhausted,
    D3OperationCompletion,
    D3OperationStart,
    D3PlanResult,
    D3RecoveryRequired,
    D3ReviewApplication,
    D3ReviewOutput,
    D3RunConfig,
    parse_d3_review_output,
)
from agent_core.dual_llm_modeling import (
    D3ActionContext,
    D3AuditedModel,
    D3ModelRequest,
    D3ModelStage,
    D3OuterContext,
    D3PlanContext,
    D3ProposalRejected,
    D3SharedArtifactContext,
)
from agent_core.fixed_command_gateway import D3FixedCommandGateway, D3FixedCommandPolicy
from agent_core.intent_postconditions import FileSha256
from agent_core.modeling import CancellationToken
from agent_core.multi_turn_control import D3Control
from agent_core.process_refinement import ProcessRefinement
from agent_core.redaction import (
    D5Redactor,
    D5SensitiveValue,
    d5_sensitive_configuration_matches,
)
from agent_core.review import D2ReviewCoordinator, D2ReviewPaused
from agent_core.review_contracts import (
    D2ModelFailure,
    D2ReviewOutput,
    D2ReviewRequest,
    D2ReviewScope,
)
from agent_core.runtime import TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_modeling import digest, stable_id


@dataclass(frozen=True, slots=True)
class D3Result:
    task_run_id: str
    turn_statuses: tuple[tuple[str, str], ...]
    intent_statuses: tuple[tuple[str, str], ...]
    plan_statuses: tuple[tuple[str, str], ...]
    verified_fact_ids: tuple[str, ...]
    task_run_status: str
    overall_goal: str
    sandbox_status: str
    dispatch_status: str
    stop_reason: str
    recovery_required: bool


class _D4TerminalStop(RuntimeError):
    def __init__(self, reason: str, *, cleanup_unknown: bool = False) -> None:
        self.reason = reason
        self.cleanup_unknown = cleanup_unknown
        super().__init__(reason)


class _D3ReviewCall:
    def __init__(self, application: D3Application) -> None:
        self.application = application

    def invoke(self, request: D2ReviewRequest):
        application = self.application
        application._check_role()
        if application.config.d4_budget is None:
            application._check_dispatch()
        else:
            state = application.state
            if (
                application._token is not None
                and application._token.is_cancelled
                or state.task_run.status is not TaskRunStatus.RUNNING
                or application.clock() >= state.task_run.deadline_at
            ):
                return D2ModelFailure("provider_failure", "d4_review_dispatch_stopped")
            application._check_dispatch(allow_admitted_operation_id=request.review_id)
        finished = threading.Event()
        values = []

        def invoke() -> None:
            try:
                values.append((True, application.review_model.invoke(request)))
            except BaseException as exc:
                values.append((False, exc))
            finally:
                finished.set()

        threading.Thread(target=invoke, daemon=True, name="d3-review-call").start()
        limit = time.monotonic() + min(
            180.0,
            application.controller.control_state.task_run.deadline_at
            - application.clock(),
        )
        while not finished.wait(0.01):
            if application._dispatch_stopped() or time.monotonic() >= limit:
                return D2ModelFailure("unknown", "d3_review_call_outcome_unknown")
        succeeded, value = values[0]
        if not succeeded:
            if isinstance(value, (TimeoutError, ConnectionError)):
                return D2ModelFailure("unknown", "d3_review_call_outcome_unknown")
            return D2ModelFailure("provider_failure", "d3_review_provider_failure")
        return value


class D3Application:
    def __init__(
        self,
        directory: Path,
        *,
        config: D3RunConfig,
        sandbox,
        import_sandbox,
        workspace_sandbox,
        command_sandbox,
        outer_model,
        inner_model,
        review_model,
        clock: Callable[[], float],
        refinement_model=None,
        sensitive_config: Mapping[str, D5SensitiveValue] | None = None,
        refinement_timeout_seconds: float = 180.0,
        d6_artifact_store: CrossTaskRunArtifactStore | None = None,
        d6_artifact_transfer: D6ArtifactTransfer | None = None,
    ) -> None:
        self.directory = directory
        self.config = config
        self.clock = clock
        self.sandbox = sandbox
        self.d6_artifact_store = d6_artifact_store
        self.d6_artifact_transfer = d6_artifact_transfer
        if (d6_artifact_store is None) != (d6_artifact_transfer is None):
            raise ValueError("d6_import_services_incomplete")
        if (
            d6_artifact_transfer is not None
            and d6_artifact_transfer.snapshots is not d6_artifact_store
        ):
            raise ValueError("d6_import_store_mismatch")
        self._redaction_configured = d5_sensitive_configuration_matches(
            config.d5_sensitive_requirements, sensitive_config
        )
        self.redactor = D5Redactor(
            tuple(entry.value for entry in (sensitive_config or {}).values())
            if self._redaction_configured else None
        )
        self.review_model = D5RedactingModel(
            review_model, self.redactor, enabled=config.d5_enabled,
            configured=self._redaction_configured,
        )
        self._token: CancellationToken | None = None
        self._bound_task_run_id: str | None = None
        self.store = SqliteControlStore.open(directory / "control.sqlite", clock=clock)
        self.imports = FileAttachmentImportRecorder(directory / "imports")
        self.commands = T6CommandRecorder(directory / "commands")
        self.workspace_evidence = FileD3WorkspaceEvidence(directory / "d3-workspace")
        self.model_evidence = FileD3ModelEvidence(directory / "d3-models")
        self.review_ledger = (
            D4ReviewLedger(
                directory / "d3-reviews",
                store=self.store,
                config=config,
                clock=clock,
            )
            if config.d4_budget is not None
            else FileD2ReviewLedger(directory / "d3-reviews")
        )
        self.proof_store = FileD3ProofStore(directory / "d3-proofs")
        self.proofs = D3ObservationProofVerifier(
            self.store,
            self.imports,
            self.workspace_evidence,
            self.commands,
            self.proof_store,
            clock,
        )
        self.validator = D3EvidenceValidator(
            self.store,
            self.imports,
            self.workspace_evidence,
            self.commands,
            self.model_evidence,
            self.review_ledger,
            self.proofs,
            self._source,
        )
        self.controller = TaskRunController(
            sandbox,
            clock=clock,
            store=self.store,
            repository=ControlStateRepository(
                self.store,
                self.commands,
                d3_observation_proofs=self.proofs,
            ),
            registry=TaskRunControllerRegistry(),
            attachment_imports=self.imports,
            d3_evidence_validator=self.validator,
            process_fact_validator=self._validate_process_fact,
        )
        self.refinement = (
            ProcessRefinement(
                self.controller, self.store, FileD5Evidence(directory / "d5-process"),
                D5SourceReader(directory, self.store), D5RedactingModel(
                    refinement_model or outer_model, self.redactor,
                    configured=self._redaction_configured, redact_response=False,
                ),
                config, clock, self._dispatch_stopped, self.redactor,
                timeout_seconds=refinement_timeout_seconds,
            ) if config.d5_enabled else None
        )
        self.importer = AttachmentImporter(
            controller=self.controller,
            recorder=self.imports,
            sandbox=import_sandbox,
            clock=clock,
        )
        self.workspace = D3WorkspaceOperations(
            self.controller,
            self.store,
            self.workspace_evidence,
            workspace_sandbox,
            self.imports,
            self.commands,
            self.proofs,
            clock,
        )
        self.outer = D3AuditedModel(
            self.controller,
            self.store,
            self.model_evidence,
            D5RedactingModel(
                outer_model, self.redactor, enabled=config.d5_enabled,
                configured=self._redaction_configured,
            ),
            config,
            clock,
        )
        self.inner = D3AuditedModel(
            self.controller,
            self.store,
            self.model_evidence,
            D5RedactingModel(
                inner_model, self.redactor, enabled=config.d5_enabled,
                configured=self._redaction_configured,
            ),
            config,
            clock,
        )
        self.control = D3Control(self.controller, self.store)
        self.command_sandbox = command_sandbox
        self.review_coordinator = D2ReviewCoordinator(
            self.review_ledger,
            apply=cast(
                Callable[[D2ReviewRequest, D2ReviewOutput], str], self._apply_review
            ),
            pause=self._pause_review,
            refresh=self._refresh_review,
            committed=self._review_committed,
            parse_output=cast(
                Callable[[object, D2ReviewRequest], D2ReviewOutput], self._parse_review
            ),
        )
        self.review_call = _D3ReviewCall(self)

    @property
    def state(self):
        return self.controller.control_state

    def _validate_process_fact(self, mutation, state) -> None:
        if self.refinement is None:
            raise D3RecoveryRequired("d5_not_enabled")
        self.refinement.validate(mutation, state)

    def _after_review(self, review_id: str) -> None:
        if self.refinement is not None:
            try:
                self.refinement.after_review(review_id)
            except (D5EvidenceError, OSError, StorageError, CommittedStateReloadError) as exc:
                raise D3RecoveryRequired("d5_refinement_persistence_recovery_required") from exc

    def _id(self, name: str) -> str:
        return stable_id(self.state.task_run.task_run_id, f"d3/{name}")

    def _check_role(self) -> None:
        if (
            self.outer.adapter.role_config != self.config.outer_role
            or self.inner.adapter.role_config != self.config.inner_role
            or self.review_model.role_config != self.config.outer_role
            or self.store.load_d3_run_config(self.state.task_run.task_run_id)
            != self.config
        ):
            raise D3RecoveryRequired("d3_frozen_role_configuration_changed")

    def _dispatch_stopped(self) -> bool:
        state = self.state
        return (
            state.task_run.status is not TaskRunStatus.RUNNING
            or self.clock() >= state.task_run.deadline_at
            or self._token is not None
            and self._token.is_cancelled
        )

    def _d4_unknown_operations(
        self, *, exclude_operation_id: str | None = None
    ) -> tuple[str, ...]:
        if self.config.d4_budget is None:
            return ()
        task_run_id = self.state.task_run.task_run_id
        list_unresolved = getattr(
            self.store, "list_d4_unresolved_operations", None
        )
        if not callable(list_unresolved):
            raise D4RecoveryRequired("d4_operation_ledger_unavailable")
        operations = list_unresolved(task_run_id)
        if any(item.start.task_run_id != task_run_id for item in operations):
            raise D4RecoveryRequired("d4_operation_owner_changed")
        return tuple(
            item.start.operation_id
            for item in operations
            if item.start.operation_id != exclude_operation_id
        )

    def _persist_cancel_intent(self) -> None:
        if self.config.d4_budget is None or self._bound_task_run_id is None:
            return
        task_run_id = self._bound_task_run_id
        related_ref = task_run_id
        event_id = stable_id(task_run_id, "d4/lifecycle/cancel")
        evidence_sha256 = digest({"task_run_id": task_run_id, "request": "cancel"})
        existing = self.store.load_d4_audit_event(event_id)
        event = D4AuditEvent(
            event_id,
            task_run_id,
            "cancellation_requested",
            "known",
            existing.occurred_at if existing is not None else self.clock(),
            task_run_id,
            evidence_sha256,
            related_ref,
        )
        if existing is not None and existing != event:
            raise D4RecoveryRequired("d4_cancellation_intent_binding_changed")
        self.store.record_d4_audit_event(event)

    def _has_cancel_intent(self, task_run_id: str) -> bool:
        expected_sha256 = digest({"task_run_id": task_run_id, "request": "cancel"})
        return any(
            event.category == "cancellation_requested"
            and event.outcome == "known"
            and event.evidence_ref == task_run_id
            and event.related_ref == task_run_id
            and event.evidence_sha256 == expected_sha256
            for event in self.store.list_d4_audit_events(task_run_id)
        )

    def _settle_stoppage(
        self, *, exclude_operation_id: str | None = None
    ) -> str | None:
        """Persist cancellation or Deadline only after all admitted results are known."""
        if self.config.d4_budget is None or self._bound_task_run_id is None:
            return None
        if self.state.task_run.status is not TaskRunStatus.RUNNING:
            return None
        if self._token is not None and self._token.is_cancelled:
            self._persist_cancel_intent()
        if self._d4_unknown_operations(exclude_operation_id=exclude_operation_id):
            return "d4_operation_outcome_unknown"
        if self.clock() >= self.state.task_run.deadline_at:
            try:
                self.controller.advance_time(self._id("lifecycle/deadline"))
            except SandboxCleanupError:
                return "task_run_deadline_cleanup_unknown"
            return "task_run_deadline_exceeded"
        if self._has_cancel_intent(self._bound_task_run_id):
            try:
                self.controller.cancel(self._id("lifecycle/cancel"))
            except SandboxCleanupError:
                return "task_run_cancel_cleanup_unknown"
            return "task_run_cancelled"
        return None

    def _reconcile_known_d4_operations(self) -> None:
        """Repair SQLite operation completions from already-persisted evidence only."""
        if self.config.d4_budget is None:
            return
        task_run_id = self.state.task_run.task_run_id
        self.store.reconcile_d4_control_commit_audit(task_run_id)
        if self.refinement is not None:
            try:
                self.refinement.reconcile()
            except (D5EvidenceError, OSError, StorageError, CommittedStateReloadError) as exc:
                raise D3RecoveryRequired("d5_refinement_persistence_recovery_required") from exc
        for d4_record in self.store.list_d4_operations(task_run_id):
            operation_id = d4_record.start.operation_id
            d3_record = self.store.load_d3_operation(operation_id)
            if d3_record is None:
                continue
            if d4_record.completion is not None:
                if (
                    d4_record.application_ref is None
                    and d4_record.completion.result_kind == "completed"
                ):
                    self._reconcile_committed_plan_application(
                        task_run_id, operation_id, d3_record
                    )
                continue
            category = d3_record.start.category
            if category in {"outer_model", "inner_model"}:
                evidence = self.model_evidence.read(operation_id)
                if evidence is None:
                    continue
                if (
                    evidence.task_run_id != task_run_id
                    or evidence.base_checkpoint_sequence
                    != d3_record.start.base_checkpoint_sequence
                    or evidence.context_sha256 != d3_record.start.context_sha256
                    or evidence.resource_ref != d3_record.start.resource_ref
                    or evidence.role_config_ref != d3_record.start.role_config_ref
                ):
                    raise D4RecoveryRequired("d4_model_evidence_binding_changed")
                self.store.complete_d3_operation(
                    D3OperationCompletion(
                        operation_id,
                        evidence.finished_at,
                        "completed" if evidence.outcome == "accepted" else "rejected",
                        operation_id,
                        digest(asdict(evidence)),
                    )
                )
                continue
            if category == "attachment_import":
                receipt = self.imports.read_receipt(operation_id)
                if receipt is None:
                    continue
                import_request = AttachmentImportRequest(
                    receipt.import_id,
                    receipt.task_run_id,
                    receipt.sandbox_id,
                    receipt.attachment_index,
                    receipt.source_root,
                    receipt.sandbox_path,
                )
                if (
                    d3_record.start.context_sha256 != digest(asdict(import_request))
                    or d3_record.start.resource_ref != import_request.sandbox_path
                    or receipt.task_run_id != task_run_id
                ):
                    raise D4RecoveryRequired("d4_import_evidence_binding_changed")
                self.store.complete_d3_operation(
                    D3OperationCompletion(
                        operation_id,
                        receipt.completed_at,
                        "completed",
                        operation_id,
                        digest(asdict(receipt)),
                    )
                )
                self.store.mark_d3_operation_applied(operation_id, operation_id)
                continue
            if category == "copy_preparation":
                artifact_receipt = self.workspace_evidence.read_artifact(operation_id)
                if artifact_receipt is not None:
                    self.workspace._repair_known(
                        operation_id, category, artifact_receipt
                    )
                continue
            if category == "sha_observation":
                digest_receipt = self.workspace_evidence.read_digest(operation_id)
                if digest_receipt is not None:
                    self.workspace._repair_known(operation_id, category, digest_receipt)
                continue
            if category == "artifact_share":
                share_receipt = self.workspace_evidence.read_share(operation_id)
                if share_receipt is not None:
                    self.workspace._repair_known(operation_id, category, share_receipt)
                continue
            if category == "intent_stop":
                stop_receipt = self.workspace_evidence.read_stop(operation_id)
                if stop_receipt is not None:
                    self.workspace._repair_known(operation_id, category, stop_receipt)
                continue
            if category == "tool_dispatch":
                completed = self.commands.read_completed(operation_id)
                if completed is None or completed.record.status == "outcome_unknown":
                    continue
                record = completed.record
                plan = next(
                    (
                        item
                        for item in self.state.plans
                        if item.plan_id == record.plan_id
                    ),
                    None,
                )
                binding = self.store.load_d3_plan_binding(record.plan_id)
                turn = (
                    next(
                        (
                            item
                            for item in self.state.turns
                            if item.turn_id == binding.turn_id
                        ),
                        None,
                    )
                    if binding is not None
                    else None
                )
                turn_binding = (
                    self.store.load_d3_turn_binding(binding.turn_id)
                    if binding is not None
                    else None
                )
                if (
                    plan is None
                    or binding is None
                    or turn is None
                    or turn_binding is None
                ):
                    raise D4RecoveryRequired("d4_tool_evidence_scope_missing")
                is_size = (
                    binding.template == "size_then_sha256" and record.step_index == 0
                )
                stage: D3ModelStage = "inner_size" if is_size else "inner_sha256"
                try:
                    _model_request_id, proposal = self.validator._known_action(
                        task_run_id, plan.plan_id, stage
                    )
                except D3RecoveryRequired as exc:
                    raise D4RecoveryRequired("d4_tool_action_proposal_missing") from exc
                if not isinstance(proposal.action, ProbeAction):
                    raise D4RecoveryRequired("d4_tool_action_proposal_invalid")
                artifact = self.workspace.read_artifact(binding.artifact_version_id)
                claim = artifact.claim
                expected_script = (
                    fixed_size_script(claim.path)
                    if is_size
                    else fixed_digest_script(claim.path)
                )
                expected_conditions = (
                    ()
                    if is_size
                    else (
                        FileSha256(
                            record.step_index, claim.path, turn_binding.expected_sha256
                        ),
                    )
                )
                action = proposal.action
                if (
                    action.script != expected_script
                    or action.stdin != b""
                    or action.working_directory != claim.path.rsplit("/", 1)[0]
                    or action.conditions != expected_conditions
                ):
                    raise D4RecoveryRequired("d4_tool_action_command_binding_changed")
                command_request = SandboxCommandRequest(
                    operation_id,
                    task_run_id,
                    plan.plan_id,
                    record.step_index,
                    action.script,
                    action.stdin,
                    action.working_directory,
                    action.timeout_seconds,
                )
                authorization = SandboxCommandAuthorization(
                    self.config.authorization.authorization_id,
                    task_run_id,
                    ("sandbox.command",),
                )
                if (
                    self.commands.replay(
                        command_request,
                        authorization,
                        self.state.task_run.sandbox_id,
                    )
                    is None
                ):
                    raise D4RecoveryRequired("d4_tool_evidence_not_replayable")
                self._gateway(turn_binding, binding)._complete_known(
                    command_request, completed
                )

    def _reconcile_committed_plan_application(
        self, task_run_id: str, operation_id: str, d3_record
    ) -> None:
        if d3_record.start.category != "inner_model":
            return
        evidence = self.model_evidence.read(operation_id)
        completion = d3_record.completion
        if evidence is None or completion is None:
            raise D4RecoveryRequired("d4_plan_application_evidence_missing")
        if (
            evidence.task_run_id != task_run_id
            or evidence.base_checkpoint_sequence
            != d3_record.start.base_checkpoint_sequence
            or evidence.context_sha256 != d3_record.start.context_sha256
            or evidence.resource_ref != d3_record.start.resource_ref
            or evidence.role_config_ref != d3_record.start.role_config_ref
            or completion.operation_id != operation_id
            or completion.evidence_ref != operation_id
            or completion.evidence_sha256 != digest(asdict(evidence))
            or completion.result_kind
            != ("completed" if evidence.outcome == "accepted" else "rejected")
        ):
            raise D4RecoveryRequired("d4_plan_application_evidence_binding_changed")

        matches = []
        for plan in self.state.plans:
            binding = self.store.load_d3_plan_binding(plan.plan_id)
            if binding is not None and binding.proposal_request_id == operation_id:
                matches.append((plan, binding))
        if not matches:
            return
        if len(matches) != 1:
            raise D4RecoveryRequired("d4_plan_application_binding_ambiguous")
        plan, binding = matches[0]
        plans_for_intent = [
            item for item in self.state.plans if item.intent_id == plan.intent_id
        ]
        ordinal = next(
            (
                index
                for index, item in enumerate(plans_for_intent, start=1)
                if item.plan_id == plan.plan_id
            ),
            None,
        )
        if (
            ordinal is None
            or binding.task_run_id != task_run_id
            or binding.intent_id != plan.intent_id
            or binding.plan_id != plan.plan_id
            or binding.proposal_request_id != operation_id
        ):
            raise D4RecoveryRequired("d4_plan_application_binding_changed")

        mutation_id = stable_id(
            task_run_id, f"d3/plan/{plan.intent_id}/{ordinal}/register"
        )
        committed = self.store.load_committed_mutation(task_run_id, mutation_id)
        if committed is None:
            raise D4RecoveryRequired("d4_plan_application_commit_missing")
        operation = committed.event.details.get("operation")
        entity_ids = committed.event.details.get("entity_ids")
        valid_register = operation == "register_d3_plan" and entity_ids == [
            plan.plan_id
        ]
        previous_plan_id = (
            plans_for_intent[ordinal - 2].plan_id if ordinal > 1 else None
        )
        valid_replan = (
            operation == "replan_d3_plan"
            and previous_plan_id is not None
            and plans_for_intent[ordinal - 2].status is PlanStatus.SUPERSEDED
            and entity_ids == [previous_plan_id, plan.plan_id]
        )
        if (
            not (valid_register or valid_replan)
            or committed.event.details.get("result") != plan.plan_id
        ):
            raise D4RecoveryRequired("d4_plan_application_commit_binding_changed")

        audits = self.store.list_d4_audit_events(task_run_id)
        commit_confirmed = any(
            event.category == "control_commit_committed"
            and event.outcome == "known"
            and event.related_ref == mutation_id
            for event in audits
        )
        checkpoint_ref = f"checkpoint:{committed.checkpoint.sequence}"
        reload_confirmed = any(
            event.category == "control_reload_confirmed"
            and event.outcome == "known"
            and event.evidence_ref == checkpoint_ref
            and event.related_ref == checkpoint_ref
            for event in audits
        )
        if not commit_confirmed:
            raise D4RecoveryRequired("d4_plan_application_commit_audit_missing")
        if not reload_confirmed:
            try:
                reloaded = self.controller.reload_state()
            except Exception as exc:
                raise D4RecoveryRequired(
                    "d4_plan_application_reload_unconfirmed"
                ) from exc
            if reloaded.latest_checkpoint.sequence < committed.checkpoint.sequence:
                raise D4RecoveryRequired("d4_plan_application_reload_stale")
            if not any(
                event.category == "control_reload_confirmed"
                and event.outcome == "known"
                and event.evidence_ref == checkpoint_ref
                and event.related_ref == checkpoint_ref
                for event in self.store.list_d4_audit_events(task_run_id)
            ):
                raise D4RecoveryRequired("d4_plan_application_reload_unconfirmed")
        self.store.mark_d3_operation_applied(operation_id, plan.plan_id)

    def _check_dispatch(
        self, *, allow_admitted_operation_id: str | None = None
    ) -> None:
        if self.config.d4_budget is not None:
            stopped = self._settle_stoppage(
                exclude_operation_id=allow_admitted_operation_id
            )
            if stopped is not None:
                if stopped == "d4_operation_outcome_unknown":
                    raise D4RecoveryRequired(stopped)
                if stopped.endswith("cleanup_unknown"):
                    raise _D4TerminalStop(stopped, cleanup_unknown=True)
                if self.state.task_run.status is not TaskRunStatus.RUNNING:
                    raise _D4TerminalStop(stopped)
                raise D3RecoveryRequired(stopped)
            if self._bound_task_run_id is not None and self._has_cancel_intent(
                self._bound_task_run_id
            ):
                raise D4RecoveryRequired("d4_cancellation_requested")
        if self._dispatch_stopped():
            raise D3RecoveryRequired("d3_dispatch_stopped")
        for application in self.store.list_d3_review_applications(
            self.state.task_run.task_run_id
        ):
            if application.paused_gate:
                raise D3RecoveryRequired(application.pause_reason or "d3_review_paused")

    def result(self, reason: str, *, recovery_required: bool = False) -> D3Result:
        state = self.state
        sandbox_status = "retained"
        if self.config.d4_budget is not None and state.task_run.sandbox_id is not None:
            for event in self.store.list_d4_audit_events(state.task_run.task_run_id):
                if event.evidence_ref != state.task_run.sandbox_id:
                    continue
                if event.category in {"cleanup_started", "cleanup_failed"}:
                    sandbox_status = "unknown"
                elif event.category == "cleanup_completed":
                    sandbox_status = "cleaned"
        return D3Result(
            state.task_run.task_run_id,
            tuple((item.turn_id, item.status.value) for item in state.turns),
            tuple((item.intent_id, item.status.value) for item in state.intents),
            tuple((item.plan_id, item.status.value) for item in state.plans),
            tuple(
                item.fact_id
                for item in state.facts
                if item.status is FactStatus.VERIFIED
            ),
            state.task_run.status.value,
            "not_evaluated",
            sandbox_status,
            "stopped",
            reason,
            recovery_required,
        )

    def start(
        self, request: TaskRequest, source_root: Path, *, task_run_id: str | None = None,
        d6_artifacts: tuple[D6ArtifactReference, ...] | None = None,
    ) -> str:
        committed_request = request
        committed_source_root: str | None = None
        if self.config.d6_authorization is None:
            if d6_artifacts is not None:
                raise D3RecoveryRequired("d6_import_not_authorized")
        else:
            if task_run_id is None or task_run_id != self.config.d6_authorization.task_run_id:
                raise D3RecoveryRequired("d6_task_run_id_required")
            if (
                not isinstance(d6_artifacts, tuple)
                or d6_artifacts != self.config.d6_authorization.local_artifacts
                or not d6_artifacts
                or len(d6_artifacts) > 2
            ):
                raise D3RecoveryRequired("d6_start_artifacts_mismatch")
            if self.d6_artifact_store is None or self.d6_artifact_transfer is None:
                raise D3RecoveryRequired("d6_import_services_unavailable")
            if (
                len(request.target.attachments) != len(d6_artifacts)
                or any(item.kind != "local_path" for item in request.target.attachments)
            ):
                raise TaskRequestError("D6 requires one local attachment slot per imported artifact")
            imports = []
            for reference in d6_artifacts:
                try:
                    manifest = self.d6_artifact_store.find_import(task_run_id, reference)
                    verified, _declaration = self.d6_artifact_transfer.verify_confirmed_import(
                        manifest.request
                    )
                except (D6ArtifactImportError, D6ArtifactImportRecoveryRequired) as exc:
                    raise D3RecoveryRequired(str(exc)) from exc
                if (
                    verified != manifest
                    or manifest.request.target_task_run_id != task_run_id
                    or manifest.request.source != reference
                    or not manifest.request.non_sensitive
                ):
                    raise D3RecoveryRequired("d6_import_manifest_binding_changed")
                imports.append(manifest)
            input_root = Path(imports[0].target_path).parent.resolve(strict=True)
            if any(Path(item.target_path).parent.resolve(strict=True) != input_root for item in imports):
                raise D3RecoveryRequired("d6_import_input_root_changed")
            request_payload = request.to_dict()
            request_payload["target"]["attachments"] = [
                Attachment("local_path", Path(item.target_path).name).to_dict()
                for item in imports
            ]
            committed_request = TaskRequest.from_dict(request_payload)
            committed_source_root = str(input_root)
        if self.config.d5_enabled and not self._redaction_configured:
            raise D3RecoveryRequired("d5_redaction_configuration_unavailable")
        if self.config.d5_enabled:
            if not isinstance(request, TaskRequest):
                raise TaskRequestError("request must be a TaskRequest")
            source_root_text = str(source_root)
            if self.redactor.contains_sensitive(source_root_text):
                raise TaskRequestError("d5_sensitive_task_request_rejected")

            def string_values(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, Mapping):
                    for key, item in value.items():
                        yield from string_values(key)
                        yield from string_values(item)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        yield from string_values(item)

            readable_text = tuple(string_values(committed_request.to_dict()))
            if any(self.redactor.contains_sensitive(value) for value in readable_text):
                raise TaskRequestError("d5_sensitive_task_request_rejected")
            if committed_source_root is None:
                committed_source_root = str(source_root.resolve(strict=True))
            if self.redactor.contains_sensitive(committed_source_root):
                raise TaskRequestError("d5_sensitive_task_request_rejected")
        if (
            self.outer.adapter.role_config != self.config.outer_role
            or self.inner.adapter.role_config != self.config.inner_role
            or self.review_model.role_config != self.config.outer_role
        ):
            raise D3RecoveryRequired("d3_adapter_role_configuration_changed")
        committed = self.controller.start(
            committed_request,
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=committed_source_root or str(source_root.resolve(strict=True)),
            task_run_id=task_run_id,
            d3_config=self.config,
        )
        self._bound_task_run_id = committed.value
        return committed.value

    async def resume(self, task_run_id: str, token: CancellationToken) -> D3Result:
        """Attach to a committed D4 run and resume only from durable evidence."""
        if self.config.d4_budget is None:
            raise D3RecoveryRequired("d4_resume_requires_explicit_budget")
        with run_lock(self.directory):
            if self._bound_task_run_id is None:
                self.controller.attach(task_run_id)
                self._bound_task_run_id = task_run_id
            elif self._bound_task_run_id != task_run_id:
                raise D3RecoveryRequired("d3_application_already_bound_to_another_task")
            else:
                self.controller.reload_state()
            frozen = self.store.load_d3_run_config(task_run_id)
            if frozen != self.config:
                raise D3RecoveryRequired("d3_frozen_run_configuration_changed")
            if self.config.d5_enabled and not self._redaction_configured:
                return self.result("d5_redaction_configuration_unavailable", recovery_required=True)
            self.store.reconcile_d4_control_commit_audit(task_run_id)
            if self.state.task_run.status is not TaskRunStatus.RUNNING:
                terminal_result = self.result("task_run_already_terminal")
                return replace(
                    terminal_result,
                    recovery_required=terminal_result.sandbox_status == "unknown",
                )
            if token.is_cancelled:
                try:
                    self._persist_cancel_intent()
                except D4RecoveryRequired as exc:
                    return self.result(str(exc), recovery_required=True)
            try:
                self._reconcile_known_d4_operations()
            except D4RecoveryRequired as exc:
                return self.result(str(exc), recovery_required=True)
            if isinstance(self.review_ledger, D4ReviewLedger):
                try:
                    self.review_ledger.reconcile_known_completions(task_run_id)
                except D4RecoveryRequired as exc:
                    return self.result(str(exc), recovery_required=True)
            try:
                stopped = self._settle_stoppage()
            except D4RecoveryRequired as exc:
                return self.result(str(exc), recovery_required=True)
            if stopped is not None:
                return self.result(
                    stopped,
                    recovery_required=(
                        stopped == "d4_operation_outcome_unknown"
                        or stopped.endswith("cleanup_unknown")
                    ),
                )
        return await self.run(token)

    def cancel(self, task_run_id: str | None = None) -> D3Result:
        """Cancel a D4 TaskRun unless an admitted external result is still unknown."""
        with run_lock(self.directory):
            if self.config.d4_budget is None:
                raise D3RecoveryRequired("d4_cancel_requires_explicit_budget")
            if self._bound_task_run_id is None:
                raise D3RecoveryRequired("d3_cancel_requires_started_or_resumed_task")
            if task_run_id is not None and task_run_id != self._bound_task_run_id:
                raise D3RecoveryRequired("d3_cancel_task_run_mismatch")
            if self.state.task_run.status is not TaskRunStatus.RUNNING:
                terminal_status = self.state.task_run.status
                reason = (
                    "task_run_cancelled"
                    if terminal_status is TaskRunStatus.CANCELLED
                    else "task_run_deadline_exceeded"
                    if terminal_status is TaskRunStatus.DEADLINE_EXCEEDED
                    else f"task_run_already_terminal:{terminal_status.value}"
                )
                terminal_result = self.result(reason)
                return replace(
                    terminal_result,
                    recovery_required=terminal_result.sandbox_status == "unknown",
                )
            try:
                self._persist_cancel_intent()
                self._reconcile_known_d4_operations()
                if isinstance(self.review_ledger, D4ReviewLedger):
                    self.review_ledger.reconcile_known_completions(
                        self._bound_task_run_id
                    )
                stopped = self._settle_stoppage()
                if stopped is not None:
                    return self.result(
                        stopped,
                        recovery_required=(
                            stopped == "d4_operation_outcome_unknown"
                            or stopped.endswith("cleanup_unknown")
                        ),
                    )
            except (D3RecoveryRequired, D4RecoveryRequired, SandboxCleanupError) as exc:
                return self.result(
                    f"cancel_recovery_required:{type(exc).__name__}",
                    recovery_required=True,
                )
            return self.result("task_run_cancelled", recovery_required=False)

    def resume_cleanup(self, task_run_id: str | None = None) -> D3Result:
        """Explicitly reconcile and retry cleanup for a terminal D4 run."""
        with run_lock(self.directory):
            if self.config.d4_budget is None:
                raise D3RecoveryRequired("d4_cleanup_requires_explicit_budget")
            target_id = task_run_id or self._bound_task_run_id
            if target_id is None:
                raise D3RecoveryRequired("d4_cleanup_requires_task_run_id")
            if self._bound_task_run_id is None:
                try:
                    self.controller.attach(target_id)
                except Exception as exc:
                    raise D3RecoveryRequired(
                        f"d4_cleanup_attach_failed:{type(exc).__name__}"
                    ) from exc
                self._bound_task_run_id = target_id
            elif self._bound_task_run_id != target_id:
                raise D3RecoveryRequired("d3_application_already_bound_to_another_task")
            else:
                try:
                    self.controller.reload_state()
                except Exception as exc:
                    raise D3RecoveryRequired(
                        f"d4_cleanup_reload_failed:{type(exc).__name__}"
                    ) from exc
            if self.store.load_d3_run_config(target_id) != self.config:
                raise D3RecoveryRequired("d3_frozen_run_configuration_changed")
            try:
                self.store.reconcile_d4_control_commit_audit(target_id)
            except Exception as exc:
                raise D3RecoveryRequired(
                    f"d4_cleanup_commit_audit_failed:{type(exc).__name__}"
                ) from exc
            if self.state.task_run.status is TaskRunStatus.RUNNING:
                return self.result(
                    "d4_cleanup_requires_terminal_task_run", recovery_required=True
                )
            try:
                self.controller.cleanup_sandbox()
            except (SandboxCleanupError, D4RecoveryRequired) as exc:
                return self.result(
                    f"d4_cleanup_unknown:{type(exc).__name__}",
                    recovery_required=True,
                )
            return self.result("d4_cleanup_confirmed", recovery_required=False)

    def _model_request(
        self, stage: D3ModelStage, resource_ref: str, context, name: str
    ) -> D3ModelRequest:
        task_id = self.state.task_run.task_run_id
        request_id = self._id(name)
        prior = self.store.load_d3_operation(request_id)
        role = (
            self.config.outer_role if stage == "outer_turn" else self.config.inner_role
        )
        return D3ModelRequest(
            request_id,
            task_id,
            prior.start.base_checkpoint_sequence
            if prior
            else self.state.latest_checkpoint.sequence,
            role.role,
            role.role_config_ref,
            resource_ref,
            stage,
            context,
            process_facts=self.refinement.context_for(resource_ref) if self.refinement is not None else (),
        )

    async def _import_all(self, token: CancellationToken):
        task = self.state.task_run
        receipts = []
        for index in self.config.authorization.attachment_indices:
            import_id = self._id(f"import/{index}")
            request = AttachmentImportRequest(
                import_id,
                task.task_run_id,
                task.sandbox_id,
                index,
                task.source_root,
                f"/workspace/input/d3-attachment-{index}",
            )
            operation = self.store.load_d3_operation(import_id)
            try:
                receipt = self.imports.read_receipt(import_id)
            except AttachmentImportStorageError as exc:
                raise D3RecoveryRequired("d3_import_evidence_unavailable") from exc
            if operation is None:
                if receipt is not None:
                    raise D3RecoveryRequired("d3_import_receipt_without_start")
                self._check_dispatch()
                self.store.begin_d3_operation(
                    D3OperationStart(
                        import_id,
                        task.task_run_id,
                        "attachment_import",
                        self.clock(),
                        self.state.latest_checkpoint.sequence,
                        digest(asdict(request)),
                        request.sandbox_path,
                    )
                )
                try:
                    receipt = await self.importer.import_attachment(request, token)
                except AttachmentImportRecoveryRequired as exc:
                    raise D3RecoveryRequired("d3_import_outcome_unknown") from exc
                except AttachmentImportRejected as exc:
                    raise D3RecoveryRequired("d3_import_rejected") from exc
                except AttachmentImportConflict as exc:
                    raise D3RecoveryRequired("d3_import_binding_changed") from exc
                except AttachmentImportStorageError as exc:
                    raise D3RecoveryRequired("d3_import_evidence_unavailable") from exc
            if receipt is None:
                raise D3RecoveryRequired("d3_import_outcome_unknown")
            if (
                receipt.task_run_id != task.task_run_id
                or receipt.sandbox_id != task.sandbox_id
                or receipt.attachment_index != index
                or receipt.sandbox_path != request.sandbox_path
                or operation is not None
                and (
                    operation.start.category != "attachment_import"
                    or operation.start.context_sha256 != digest(asdict(request))
                    or operation.start.resource_ref != request.sandbox_path
                )
            ):
                raise D3RecoveryRequired("d3_import_binding_changed")
            expected = D3OperationCompletion(
                import_id,
                receipt.completed_at,
                "completed",
                import_id,
                digest(asdict(receipt)),
            )
            self.store.complete_d3_operation(expected)
            self.store.mark_d3_operation_applied(import_id, import_id)
            if self.config.d6_authorization is not None:
                if self.d6_artifact_store is None or self.d6_artifact_transfer is None:
                    raise D3RecoveryRequired("d6_import_services_unavailable")
                references = self.config.d6_authorization.local_artifacts
                if index >= len(references):
                    raise D3RecoveryRequired("d6_import_attachment_index_unbound")
                try:
                    d6_manifest = self.d6_artifact_store.find_import(
                        task.task_run_id, references[index],
                    )
                    verified_manifest, _declaration = self.d6_artifact_transfer.verify_confirmed_import(
                        d6_manifest.request,
                    )
                except (D6ArtifactImportError, D6ArtifactImportRecoveryRequired) as exc:
                    raise D3RecoveryRequired(str(exc)) from exc
                target_path = Path(d6_manifest.target_path)
                if (
                    verified_manifest != d6_manifest
                    or d6_manifest.request.source != references[index]
                    or d6_manifest.request.target_task_run_id != task.task_run_id
                    or d6_manifest.status != "confirmed"
                    or not d6_manifest.request.non_sensitive
                    or task.source_root is None
                    or target_path.parent.resolve(strict=True) != Path(task.source_root).resolve(strict=True)
                    or task.request.target.attachments[index].value != target_path.name
                    or receipt.local_path != target_path.name
                    or receipt.source_root != str(target_path.parent.resolve(strict=True))
                    or receipt.byte_size != d6_manifest.byte_size
                    or receipt.sha256 != d6_manifest.sha256
                    or receipt.sandbox_path != request.sandbox_path
                    or receipt.attachment_index != index
                ):
                    raise D3RecoveryRequired("d6_attachment_receipt_source_binding_changed")
                receipt_sha256 = digest(asdict(receipt))
                try:
                    self.d6_artifact_store.bind_attachment_receipt(
                        request=d6_manifest.request,
                        attachment_index=index,
                        receipt_sha256=receipt_sha256,
                    )
                    self.d6_artifact_store.verify_attachment_receipt(
                        request=d6_manifest.request,
                        attachment_index=index,
                        receipt_sha256=receipt_sha256,
                    )
                except D6ArtifactImportError as exc:
                    raise D3RecoveryRequired(str(exc)) from exc
            receipts.append(receipt)
        return tuple(receipts)

    async def _outer_proposal(
        self,
        ordinal: int,
        receipts,
        token: CancellationToken,
        prior_turn_id: str | None = None,
        prior_reason: str | None = None,
    ):
        context = D3OuterContext(
            self.state.task_run.request.goal,
            receipts,
            prior_turn_id,
            prior_reason,
        )
        request = self._model_request(
            "outer_turn",
            f"turn:{ordinal}",
            context,
            f"outer/turn/{ordinal}",
        )

        def refresh(_current: D3ModelRequest) -> D3ModelRequest:
            if prior_turn_id is not None and not any(
                item.turn_id == prior_turn_id and item.status is TurnStatus.RUNNING
                for item in self.state.turns
            ):
                raise D3RecoveryRequired("d3_turn_proposal_scope_expired")
            fresh = D3OuterContext(
                self.state.task_run.request.goal,
                receipts,
                prior_turn_id,
                prior_reason,
            )
            return replace(
                request,
                base_checkpoint_sequence=self.state.latest_checkpoint.sequence,
                context=fresh,
            )

        def validate(_current: D3ModelRequest, proposal) -> None:
            if (
                proposal.attachment_index
                not in self.config.authorization.attachment_indices
            ):
                raise D3ProposalRejected("d3_attachment_not_authorized")

        if self.config.d4_budget is None:
            request_id, proposal = await self.outer.propose(request, token)
            if (
                proposal.attachment_index
                not in self.config.authorization.attachment_indices
            ):
                self.outer.reject(request_id, "d3_attachment_not_authorized")
                raise D3ProposalRejected("d3_attachment_not_authorized")
        else:
            request_id, proposal = await self.outer.propose(
                request, token, refresh=refresh, validate=validate
            )
        return request_id, proposal

    def _source(self, reference: str) -> object:
        outcome = self.store.load_d3_outcome(reference)
        if outcome is not None:
            return {"kind": "d3_outcome", **asdict(outcome)}
        operation = self.store.load_d3_operation(reference)
        if operation is not None and operation.start.category == "attachment_import":
            receipt = self.imports.read_receipt(reference)
            if receipt is None:
                raise D3RecoveryRequired("d3_review_import_source_missing")
            return {
                "kind": "attachment_import",
                "task_run_id": receipt.task_run_id,
                "attachment_index": receipt.attachment_index,
                "byte_size": receipt.byte_size,
                "sha256": receipt.sha256,
            }
        if operation is not None and operation.start.category in {
            "outer_model",
            "inner_model",
        }:
            model = self.model_evidence.read(reference)
            if model is None:
                raise D3RecoveryRequired("d3_review_model_source_missing")
            rejection = self.model_evidence.read_rejection(reference)
            return {
                "kind": "model_proposal",
                "task_run_id": model.task_run_id,
                "stage": model.stage,
                "outcome": model.outcome,
                "reason": model.reason,
                "rejection": rejection.code if rejection else None,
                "context_sha256": model.context_sha256,
            }
        if operation is not None and operation.start.category == "tool_dispatch":
            completed = self.commands.read_completed(reference)
            if completed is None:
                raise D3RecoveryRequired("d3_review_execution_source_missing")
            record = completed.record
            size = self.workspace_evidence.read_size(reference)
            return {
                "kind": "execution",
                "task_run_id": record.task_run_id,
                "plan_id": record.plan_id,
                "step_index": record.step_index,
                "status": record.status,
                "exit_code": record.exit_code,
                "stdout_sha256": hashlib.sha256(record.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(record.stderr).hexdigest(),
                "size_observation": asdict(size) if size else None,
            }
        if operation is not None and operation.start.category == "sha_observation":
            observed = self.workspace_evidence.read_digest(reference)
            if observed is None:
                raise D3RecoveryRequired("d3_review_digest_source_missing")
            return {
                "kind": "digest_observation",
                "claim": asdict(observed.claim),
                "status": observed.status,
                "observed": asdict(observed.observed),
            }
        fact = next(
            (item for item in self.state.facts if item.fact_id == reference), None
        )
        if fact is not None:
            return {
                "kind": "verified_fact",
                "fact_id": fact.fact_id,
                "turn_id": fact.turn_id,
                "status": fact.status.value,
                "source_ref": fact.source_ref,
                "verification_ref": fact.verification_ref,
            }
        if operation is not None and operation.start.category == "artifact_share":
            share = self.workspace_evidence.read_share(reference)
            if share is None:
                raise D3RecoveryRequired("d3_review_share_source_missing")
            return {
                "kind": "shared_artifact",
                "claim": asdict(share.claim),
                "status": share.status,
            }
        raise D3RecoveryRequired("d3_review_source_unavailable")

    def _review_request(
        self,
        turn_id: str,
        intent_id: str,
        scope: D2ReviewScope,
        action_candidate_id: str | None,
        source_ids: tuple[str, ...],
    ) -> D2ReviewRequest:
        state = self.state
        turn = next(item for item in state.turns if item.turn_id == turn_id)
        intent = next(item for item in state.intents if item.intent_id == intent_id)
        binding = self.store.load_d3_turn_binding(turn_id)
        if binding is None or not source_ids:
            raise D3RecoveryRequired("d3_review_binding_or_sources_missing")
        if scope == "turn_start":
            root_name = f"review/start/{turn_id}/{intent_id}"
        elif scope == "action":
            root_name = f"review/action/{action_candidate_id}"
        else:
            root_name = f"review/end/{turn_id}"
        root_id = self._id(root_name)
        source_versions = {
            reference: digest(self._source(reference)) for reference in source_ids
        }
        version = max(
            (
                item.constraint_version
                for item in self.store.list_d3_review_applications(
                    state.task_run.task_run_id
                )
            ),
            default=0,
        )
        context = {
            "turn_target": turn.turn_target,
            "turn_binding": asdict(binding),
            "turn_status": turn.status.value,
            "intent_status": intent.status.value,
            "source_versions": source_versions,
            "source_summaries": {
                reference: self._source(reference) for reference in source_ids
            },
            "constraint_version": version,
        }
        if self.config.d5_enabled:
            context["source_summaries"] = self.redactor.redact_value(context["source_summaries"])
            context["turn_target"] = self.redactor.redact(context["turn_target"])
        summary = {**context, "checkpoint": state.latest_checkpoint.sequence}
        from agent_core.control_store import canonical_json

        return D2ReviewRequest(
            self._id(f"review/request/{root_name}"),
            root_id,
            1,
            1,
            scope,
            state.task_run.task_run_id,
            turn_id,
            intent_id,
            action_candidate_id,
            state.latest_checkpoint.sequence,
            digest(context),
            source_ids,
            version,
            canonical_json(summary),
        )

    def _refresh_review(self, request: D2ReviewRequest) -> D2ReviewRequest:
        self._check_dispatch()
        fresh = self._review_request(
            request.turn_id,
            request.intent_id,
            request.review_scope,
            request.action_candidate_id,
            request.source_versions,
        )
        return replace(
            fresh,
            review_id=request.review_id,
            review_generation=request.review_generation,
            review_attempt=request.review_attempt,
        )

    def _parse_review(self, raw, request: D2ReviewRequest) -> D3ReviewOutput:
        output = parse_d3_review_output(raw, request)
        state = self.state
        if output.decision == "switch_sibling_intent" and not any(
            item.turn_id == request.turn_id and item.status is IntentStatus.PENDING
            for item in state.intents
        ):
            raise ValueError("d3_no_sibling_candidate")
        if (
            output.decision == "replan_current_intent"
            and not 1
            <= sum(item.intent_id == request.intent_id for item in state.plans)
            < 2
        ):
            raise ValueError("d3_replan_not_available")
        if output.decision == "adjust_turn_target" and len(state.turns) >= 2:
            raise ValueError("d3_turn_limit_reached")
        return output

    def _review_committed(self, request: D2ReviewRequest) -> bool:
        return (
            self.store.load_d3_review_application(
                request.task_run_id, request.review_id
            )
            is not None
        )

    def _apply_review(self, request: D2ReviewRequest, output: D3ReviewOutput) -> str:
        self._check_dispatch()
        application = D3ReviewApplication(
            request.task_run_id,
            request.review_root_id,
            request.review_generation,
            request.review_id,
            request.context_version,
            digest(asdict(output)),
            request.constraint_version + bool(output.constraints),
            output.constraints,
            request.turn_id,
            request.intent_id,
            request.review_scope,
            request.action_candidate_id,
            output.decision,
            output.source_ids,
        )
        self.controller.commit_d3_transition(
            "apply_d3_review",
            (application,),
            (request.review_id,),
            "applied",
            self._id(f"apply-review/{request.review_id}"),
        )
        return f"d3-review:{request.review_id}"

    def _pause_review(
        self, request: D2ReviewRequest, reason: str, outcome: str
    ) -> None:
        if self._dispatch_stopped() or reason == "review_application_uncommitted":
            return
        pause_id = self._id(f"review/pause/{request.review_root_id}/{reason}")
        if (
            self.store.load_d3_review_application(request.task_run_id, pause_id)
            is not None
        ):
            return
        application = D3ReviewApplication(
            request.task_run_id,
            request.review_root_id,
            request.review_generation,
            pause_id,
            request.context_version,
            digest({"paused": reason, "outcome": outcome}),
            max(
                (
                    item.constraint_version
                    for item in self.store.list_d3_review_applications(
                        request.task_run_id
                    )
                ),
                default=0,
            ),
            (),
            request.turn_id,
            request.intent_id,
            request.review_scope,
            request.action_candidate_id,
            "acknowledge_turn_end"
            if request.review_scope == "turn_end"
            else "continue_current_intent",
            (),
            True,
            reason,
        )
        self.controller.commit_d3_transition(
            "apply_d3_review",
            (application,),
            (pause_id,),
            "paused",
            pause_id,
        )

    def _review(
        self,
        turn_id: str,
        intent_id: str,
        scope: D2ReviewScope,
        candidate_id: str | None,
        source_ids: tuple[str, ...],
    ) -> tuple[str, D3ReviewOutput]:
        request = self._review_request(
            turn_id, intent_id, scope, candidate_id, source_ids
        )
        prior = [
            item
            for item in self.store.list_d3_review_applications(request.task_run_id)
            if item.review_root_id == request.review_root_id and not item.paused_gate
        ]
        if prior:
            root = self.review_ledger.read_root(request.review_root_id)
            applied = root.get("applied", ()) if isinstance(root, dict) else ()
            if not isinstance(applied, (list, tuple)):
                raise D3RecoveryRequired("d3_review_application_receipt_invalid")
            if root is None or not any(
                isinstance(item, dict) and item.get("review_id") == prior[-1].review_id
                for item in applied
            ):
                result = self.review_coordinator.review(
                    request,
                    self.review_call,
                    trigger_reasons=(scope,),
                )
                if not isinstance(result.output, D3ReviewOutput):
                    raise D3RecoveryRequired("d3_review_application_receipt_missing")
                self._after_review(result.review_id)
                return result.review_id, result.output
            application, _, output = self.validator._review_call(
                request.task_run_id, prior[-1].review_id
            )
            self._after_review(application.review_id)
            return application.review_id, output
        if self._dispatch_stopped():
            self.review_ledger.record_not_called(request, "d3_review_dispatch_stopped")
            raise D3RecoveryRequired("d3_review_not_called")
        result = self.review_coordinator.review(
            request,
            self.review_call,
            trigger_reasons=(scope,),
        )
        if not isinstance(result.output, D3ReviewOutput):
            raise D3RecoveryRequired("d3_review_output_missing")
        self._after_review(result.review_id)
        return result.review_id, result.output

    async def _shared_context(
        self,
        intent_id: str,
        token: CancellationToken,
    ) -> tuple[D3SharedArtifactContext, ...]:
        task_run_id = self.state.task_run.task_run_id
        for operation in self.store.list_d3_operations(task_run_id):
            if operation.start.category != "artifact_share":
                continue
            claim = self.workspace_evidence.read_share_claim(
                operation.start.operation_id
            )
            if (
                claim is None
                or claim.task_run_id != task_run_id
                or operation.start.context_sha256 != digest(asdict(claim))
                or operation.start.resource_ref != claim.share_id
            ):
                raise D3RecoveryRequired("d3_share_claim_changed")
            if (
                claim.target_intent_id == intent_id
                and self.workspace_evidence.read_share(claim.share_id) is None
            ):
                raise D3RecoveryRequired("d3_share_outcome_unknown")
        shared = []
        for receipt in self.workspace_evidence.list_shares(task_run_id):
            claim = receipt.claim
            if claim.target_intent_id != intent_id:
                continue
            await self.workspace.verify_share(claim.share_id, token)
            shared.append(
                D3SharedArtifactContext(
                    claim.share_id,
                    claim.artifact_version_id,
                    claim.source_turn_id,
                    claim.source_intent_id,
                    claim.target_path,
                    claim.source_sha256,
                    claim.original_verification_status,
                )
            )
        return tuple(sorted(shared, key=lambda item: item.share_id))

    async def _new_plan(
        self,
        intent_id: str,
        token: CancellationToken,
        *,
        review_id: str | None = None,
    ) -> None:
        intent = next(
            item for item in self.state.intents if item.intent_id == intent_id
        )
        turn = next(item for item in self.state.turns if item.turn_id == intent.turn_id)
        binding = self.store.load_d3_turn_binding(turn.turn_id)
        if binding is None:
            raise D3RecoveryRequired("d3_turn_binding_missing")
        previous = [item for item in self.state.plans if item.intent_id == intent_id]
        ordinal = len(previous) + 1
        reusable = []
        for plan in previous:
            result = self.store.load_d3_plan_result(plan.plan_id)
            if result is not None and result.outcome == "satisfied":
                if result.proof_id is None:
                    raise D3RecoveryRequired("d3_reusable_proof_missing")
                reusable.append(result.proof_id)
        context = D3PlanContext(
            turn.turn_target,
            binding,
            intent_id,
            tuple(item.plan_id for item in previous),
            tuple(reusable),
            await self._shared_context(intent_id, token),
        )
        request = self._model_request(
            "inner_plan",
            f"plan:{intent_id}:{ordinal}",
            context,
            f"inner/plan/{intent_id}/{ordinal}",
        )

        def refresh(_current: D3ModelRequest) -> D3ModelRequest:
            live_intent = next(
                (item for item in self.state.intents if item.intent_id == intent_id),
                None,
            )
            if live_intent is None or live_intent.status is not IntentStatus.RUNNING:
                raise D3RecoveryRequired("d3_plan_proposal_scope_expired")
            live_turn = next(
                item for item in self.state.turns if item.turn_id == live_intent.turn_id
            )
            live_binding = self.store.load_d3_turn_binding(live_turn.turn_id)
            if live_binding is None:
                raise D3RecoveryRequired("d3_turn_binding_missing")
            live_plans = [
                item for item in self.state.plans if item.intent_id == intent_id
            ]
            live_proofs = []
            for plan in live_plans:
                result = self.store.load_d3_plan_result(plan.plan_id)
                if result is not None and result.outcome == "satisfied":
                    if result.proof_id is None:
                        raise D3RecoveryRequired("d3_reusable_proof_missing")
                    live_proofs.append(result.proof_id)
            fresh = D3PlanContext(
                live_turn.turn_target,
                live_binding,
                intent_id,
                tuple(item.plan_id for item in live_plans),
                tuple(live_proofs),
                context.shared_artifacts,
            )
            return replace(
                request,
                base_checkpoint_sequence=self.state.latest_checkpoint.sequence,
                context=fresh,
            )

        def validate(_current: D3ModelRequest, proposal) -> None:
            live_context = _current.context
            if (
                ordinal == 1
                and proposal.template != "size_then_sha256"
                or ordinal == 2
                and proposal.template != "sha256_only"
                or ordinal not in {1, 2}
                or proposal.observation_mode == "reuse_saved_proof"
                and (
                    not isinstance(live_context, D3PlanContext)
                    or proposal.source_proof_id not in live_context.reusable_proof_ids
                )
            ):
                raise D3ProposalRejected("d3_plan_template_or_source_unavailable")

        if self.config.d4_budget is None:
            request_id, proposal = await self.inner.propose(request, token)
            if (
                ordinal == 1
                and proposal.template != "size_then_sha256"
                or ordinal == 2
                and proposal.template != "sha256_only"
                or ordinal not in {1, 2}
                or proposal.observation_mode == "reuse_saved_proof"
                and proposal.source_proof_id not in context.reusable_proof_ids
            ):
                self.inner.reject(request_id, "d3_plan_template_or_source_unavailable")
                raise D3ProposalRejected("d3_plan_template_or_source_unavailable")
        else:
            request_id, proposal = await self.inner.propose(
                request, token, refresh=refresh, validate=validate
            )
        plan_id = self._id(f"plan/{intent_id}/{ordinal}")
        source_fact_id = None
        if proposal.observation_mode == "reuse_saved_proof":
            proof = self.proof_store.read(proposal.source_proof_id)
            if proof is None:
                raise D3RecoveryRequired("d3_selected_proof_missing")
            original = self.store.load_d3_plan_result(proof.plan_id)
            if original is None or original.proof_id != proof.proof_id:
                raise D3RecoveryRequired("d3_selected_proof_result_missing")
            artifact_version_id = proof.artifact_version_id
            source_fact_id = original.supporting_fact_id
        else:
            artifact_version_id = self._id(f"artifact/{plan_id}")
        plan_binding = self.control.register_plan(
            intent_id,
            proposal,
            request_id,
            artifact_version_id,
            source_fact_id=source_fact_id,
            review_id=review_id,
        )
        self.store.mark_d3_operation_applied(request_id, plan_binding.plan_id)

    async def _route(
        self,
        review_id: str,
        output: D3ReviewOutput,
        intent_id: str,
        receipts,
        token: CancellationToken,
    ) -> bool:
        if output.decision == "continue_current_intent":
            return False
        intent = next(
            item for item in self.state.intents if item.intent_id == intent_id
        )
        turn = next(item for item in self.state.turns if item.turn_id == intent.turn_id)
        if output.decision == "replan_current_intent":
            await self._new_plan(intent_id, token, review_id=review_id)
            return True
        if output.decision == "switch_sibling_intent":
            siblings = [
                item
                for item in self.state.intents
                if item.turn_id == turn.turn_id and item.status is IntentStatus.PENDING
            ]
            if not siblings:
                raise D3RecoveryRequired("d3_no_sibling_candidate")
            next_intent = min(siblings, key=lambda item: item.priority)
            await self._shared_context(next_intent.intent_id, token)
            stop_id = self._id(f"stop/{review_id}")
            await self.workspace.stop_intent(
                turn.turn_id,
                intent_id,
                review_id,
                "sibling_switch",
                stop_id,
                token,
            )
            self.control.switch_sibling(intent_id, review_id, stop_id)
            return True
        if output.decision == "adjust_turn_target":
            ordinal = len(self.state.turns) + 1
            proposal_id, proposal = await self._outer_proposal(
                ordinal,
                receipts,
                token,
                turn.turn_id,
                "turn_target_adjusted",
            )
            receipt = receipts[proposal.attachment_index]
            registration, binding, _ = self.control.prepare_adjust_turn(
                review_id,
                proposal,
                proposal_id,
                receipt,
            )
            self.validator.validate_turn_proposal(
                self.state.task_run.task_run_id,
                "advance_d3_turn",
                self.state,
                binding,
                registration.state_changes,
            )
            stop_id = self._id(f"stop/{review_id}")
            await self.workspace.stop_intent(
                turn.turn_id,
                intent_id,
                review_id,
                "turn_target_adjusted",
                stop_id,
                token,
            )
            next_binding = self.control.adjust_turn(
                intent_id,
                review_id,
                stop_id,
                proposal,
                proposal_id,
                receipt,
            )
            self.store.mark_d3_operation_applied(proposal_id, next_binding.turn_id)
            return True
        raise D3RecoveryRequired("d3_nonterminal_review_decision_invalid")

    def _gateway(self, turn_binding, plan_binding):
        return D3FixedCommandGateway(
            self.controller,
            self.store,
            self.commands,
            self.command_sandbox,
            D3FixedCommandPolicy(
                self.config,
                turn_binding,
                plan_binding,
                self.store,
                self.workspace,
            ),
            self.clock,
        )

    def _reviewable_action_rejection(
        self, error: D3ProposalRejected, fallback_request_id: str
    ) -> str:
        request_id = error.model_request_id or fallback_request_id
        evidence = self.model_evidence.read(request_id)
        rejection = self.model_evidence.read_rejection(request_id)
        if evidence is None:
            raise D4RecoveryRequired(
                f"d4_action_proposal_evidence_missing:{error.code}"
            ) from error
        if evidence.outcome != "accepted" or rejection is None:
            reason = evidence.reason or evidence.outcome
            raise D4RecoveryRequired(
                f"d4_action_proposal_outcome_requires_recovery:{reason}"
            ) from error
        if evidence.correction_of_model_request_id is not None:
            raise D4RecoveryRequired(
                f"d4_action_refinement_rejected:{rejection.code}"
            ) from error
        return request_id

    async def _step(
        self,
        turn,
        plan_binding,
        step_index: int,
        receipts,
        token: CancellationToken,
    ) -> bool:
        artifact = self.workspace.read_artifact(plan_binding.artifact_version_id)
        path = artifact.claim.path
        is_size = plan_binding.template == "size_then_sha256" and step_index == 0
        stage: D3ModelStage = "inner_size" if is_size else "inner_sha256"
        script = fixed_size_script(path) if is_size else fixed_digest_script(path)
        conditions = (
            () if is_size else (FileSha256(step_index, path, turn.expected_sha256),)
        )
        shared_artifacts = await self._shared_context(plan_binding.intent_id, token)
        request = self._model_request(
            stage,
            f"{plan_binding.plan_id}:{stage}",
            D3ActionContext(
                next(
                    item.turn_target
                    for item in self.state.turns
                    if item.turn_id == turn.turn_id
                ),
                turn,
                plan_binding,
                step_index,
                script,
                path.rsplit("/", 1)[0],
                (),
                shared_artifacts,
            ),
            f"action/{plan_binding.plan_id}/{stage}",
        )

        def refresh(_current: D3ModelRequest) -> D3ModelRequest:
            live_turn = next(
                (item for item in self.state.turns if item.turn_id == turn.turn_id),
                None,
            )
            live_intent = next(
                (
                    item
                    for item in self.state.intents
                    if item.intent_id == plan_binding.intent_id
                ),
                None,
            )
            current_plan = next(
                (
                    item
                    for item in self.state.plans
                    if item.plan_id == plan_binding.plan_id
                ),
                None,
            )
            if (
                live_turn is None
                or live_intent is None
                or current_plan is None
                or live_turn.status is not TurnStatus.RUNNING
                or live_intent.status is not IntentStatus.RUNNING
                or current_plan.status is not PlanStatus.RUNNING
            ):
                raise D3RecoveryRequired("d3_action_proposal_scope_expired")
            turn_binding = self.store.load_d3_turn_binding(live_turn.turn_id)
            if turn_binding is None or turn_binding != turn:
                raise D3RecoveryRequired("d3_action_turn_binding_changed")
            fresh = D3ActionContext(
                live_turn.turn_target,
                turn_binding,
                plan_binding,
                step_index,
                script,
                path.rsplit("/", 1)[0],
                (),
                shared_artifacts,
            )
            return replace(
                request,
                base_checkpoint_sequence=self.state.latest_checkpoint.sequence,
                context=fresh,
            )

        def validate(_current: D3ModelRequest, candidate) -> None:
            action = candidate.action
            if (
                not isinstance(action, ProbeAction)
                or action.script != script
                or action.stdin != b""
                or action.working_directory != path.rsplit("/", 1)[0]
                or action.conditions != conditions
            ):
                raise D3ProposalRejected("d3_fixed_probe_rejected")
            try:
                self.validator._allowed(
                    plan_binding.task_run_id,
                    turn.turn_id,
                    plan_binding.intent_id,
                    "probe",
                    action.timeout_seconds,
                )
            except D3RecoveryRequired as exc:
                if "rejected_by_review_constraint" in str(exc):
                    raise D3ProposalRejected(str(exc)) from exc
                raise

        try:
            if self.config.d4_budget is None:
                request_id, proposal = await self.inner.propose(request, token)
            else:
                request_id, proposal = await self.inner.propose(
                    request, token, refresh=refresh, validate=validate
                )
        except D3ProposalRejected as exc:
            if self.config.d4_budget is None:
                raise
            request_id = self._reviewable_action_rejection(
                exc, request.model_request_id
            )
            review_id, output = self._review(
                turn.turn_id,
                plan_binding.intent_id,
                "action",
                request_id,
                (request_id,),
            )
            if await self._route(
                review_id, output, plan_binding.intent_id, receipts, token
            ):
                return True
            raise D3RecoveryRequired(str(exc)) from exc
        action = proposal.action
        if (
            not isinstance(action, ProbeAction)
            or action.script != script
            or action.stdin != b""
            or action.working_directory != path.rsplit("/", 1)[0]
            or action.conditions != conditions
        ):
            self.inner.reject(request_id, "d3_fixed_probe_rejected")
            review_id, output = self._review(
                turn.turn_id,
                plan_binding.intent_id,
                "action",
                request_id,
                (request_id,),
            )
            if await self._route(
                review_id, output, plan_binding.intent_id, receipts, token
            ):
                return True
            raise D3RecoveryRequired("d3_fixed_probe_rejected")
        try:
            self.validator._allowed(
                plan_binding.task_run_id,
                turn.turn_id,
                plan_binding.intent_id,
                "probe",
                action.timeout_seconds,
            )
        except D3RecoveryRequired as exc:
            self.inner.reject(request_id, str(exc))
            review_id, output = self._review(
                turn.turn_id,
                plan_binding.intent_id,
                "action",
                request_id,
                (request_id,),
            )
            if await self._route(
                review_id, output, plan_binding.intent_id, receipts, token
            ):
                return True
            raise
        tool_id = self._id(f"tool/{plan_binding.plan_id}/{step_index}")
        completed = await self._gateway(turn, plan_binding).call(
            SandboxCommandRequest(
                tool_id,
                plan_binding.task_run_id,
                plan_binding.plan_id,
                step_index,
                action.script,
                action.stdin,
                action.working_directory,
                action.timeout_seconds,
            ),
            SandboxCommandAuthorization(
                self.config.authorization.authorization_id,
                plan_binding.task_run_id,
                ("sandbox.command",),
            ),
            token,
        )
        if completed.record.status != "completed" or completed.record.exit_code != 0:
            raise D3RecoveryRequired("d3_fixed_tool_did_not_complete")
        self.store.mark_d3_operation_applied(request_id, tool_id)
        if is_size:
            self.workspace.record_size(plan_binding, tool_id, turn.expected_byte_size)
        else:
            observation_id = self._id(f"observation/{plan_binding.plan_id}")
            receipt = await self.workspace.observe_digest(
                turn,
                plan_binding,
                tool_id,
                observation_id,
                token,
            )
            proof_id = self._id(f"proof/{plan_binding.plan_id}")
            proof = self.proofs.issue(plan_binding, observation_id, proof_id)
            fact_id = self._id(f"fact/{plan_binding.plan_id}")
            result = D3PlanResult(
                plan_binding.task_run_id,
                plan_binding.plan_id,
                "satisfied",
                plan_binding.artifact_version_id,
                observation_id,
                proof_id,
                digest(asdict(receipt)),
                fact_id,
            )
            self.control.complete_plan(plan_binding, result, proof.source_execution_id)
        if proposal.review_requested:
            sources = (
                (request_id, tool_id)
                if is_size
                else (
                    request_id,
                    tool_id,
                    self._id(f"observation/{plan_binding.plan_id}"),
                    self._id(f"fact/{plan_binding.plan_id}"),
                )
            )
            review_id, output = self._review(
                turn.turn_id,
                plan_binding.intent_id,
                "action",
                request_id,
                sources,
            )
            return await self._route(
                review_id,
                output,
                plan_binding.intent_id,
                receipts,
                token,
            )
        return False

    async def _run_plan(self, plan_binding, receipts, token: CancellationToken) -> bool:
        turn = self.store.load_d3_turn_binding(plan_binding.turn_id)
        if turn is None:
            raise D3RecoveryRequired("d3_plan_turn_missing")
        if plan_binding.observation_mode == "reuse_saved_proof":
            proof = self.proof_store.read(plan_binding.source_proof_id)
            original = self.store.load_d3_plan_result(proof.plan_id) if proof else None
            if proof is None or original is None or original.outcome != "satisfied":
                raise D3RecoveryRequired("d3_reused_proof_unavailable")
            result = D3PlanResult(
                plan_binding.task_run_id,
                plan_binding.plan_id,
                "satisfied",
                plan_binding.artifact_version_id,
                proof.observation_id,
                proof.proof_id,
                original.evidence_sha256,
                plan_binding.source_fact_id,
            )
            self.control.complete_plan(plan_binding, result)
            return False
        await self.workspace.prepare_artifact(
            turn,
            plan_binding.intent_id,
            plan_binding.plan_id,
            plan_binding.artifact_version_id,
            token,
        )
        steps = 2 if plan_binding.template == "size_then_sha256" else 1
        for step_index in range(steps):
            if await self._step(turn, plan_binding, step_index, receipts, token):
                return True
        return False

    async def _complete_intent(
        self,
        intent_id: str,
        receipts,
        token: CancellationToken,
    ) -> bool:
        intent = next(
            item for item in self.state.intents if item.intent_id == intent_id
        )
        plan = [item for item in self.state.plans if item.intent_id == intent_id][-1]
        binding = self.store.load_d3_plan_binding(plan.plan_id)
        result = self.store.load_d3_plan_result(plan.plan_id)
        turn = self.store.load_d3_turn_binding(intent.turn_id)
        if (
            binding is None
            or result is None
            or turn is None
            or result.outcome != "satisfied"
            or result.supporting_fact_id is None
        ):
            raise D3RecoveryRequired("d3_completion_support_unavailable")
        fact_id = result.supporting_fact_id
        context = D3ActionContext(
            next(
                item.turn_target
                for item in self.state.turns
                if item.turn_id == intent.turn_id
            ),
            turn,
            binding,
            None,
            None,
            None,
            (fact_id,),
            await self._shared_context(intent_id, token),
        )
        request = self._model_request(
            "inner_complete",
            f"{plan.plan_id}:inner_complete",
            context,
            f"action/{plan.plan_id}/inner_complete",
        )

        def refresh(_current: D3ModelRequest) -> D3ModelRequest:
            live_intent = next(
                (item for item in self.state.intents if item.intent_id == intent_id),
                None,
            )
            live_plan = next(
                (item for item in self.state.plans if item.plan_id == plan.plan_id),
                None,
            )
            live_result = self.store.load_d3_plan_result(plan.plan_id)
            if (
                live_intent is None
                or live_intent.status is not IntentStatus.RUNNING
                or live_plan is None
                or live_plan.status is not PlanStatus.COMPLETED
                or live_result is None
                or live_result.outcome != "satisfied"
                or live_result.supporting_fact_id != fact_id
            ):
                raise D3RecoveryRequired("d3_completion_proposal_scope_expired")
            live_turn = next(
                (item for item in self.state.turns if item.turn_id == intent.turn_id),
                None,
            )
            if live_turn is None:
                raise D3RecoveryRequired("d3_completion_turn_missing")
            turn_binding = self.store.load_d3_turn_binding(live_turn.turn_id)
            if turn_binding is None or turn_binding != turn:
                raise D3RecoveryRequired("d3_completion_turn_binding_changed")
            fresh = D3ActionContext(
                live_turn.turn_target,
                turn_binding,
                binding,
                None,
                None,
                None,
                (fact_id,),
                context.shared_artifacts,
            )
            return replace(
                request,
                base_checkpoint_sequence=self.state.latest_checkpoint.sequence,
                context=fresh,
            )

        def validate(_current: D3ModelRequest, candidate) -> None:
            if not isinstance(
                candidate.action, CompleteIntentAction
            ) or candidate.action.fact_ids != (fact_id,):
                raise D3ProposalRejected("d3_completion_fact_rejected")
            try:
                self.validator._allowed(
                    self.state.task_run.task_run_id,
                    intent.turn_id,
                    intent_id,
                    "complete_intent",
                    None,
                )
            except D3RecoveryRequired as exc:
                if "rejected_by_review_constraint" in str(exc):
                    raise D3ProposalRejected(str(exc)) from exc
                raise

        try:
            if self.config.d4_budget is None:
                request_id, proposal = await self.inner.propose(request, token)
            else:
                request_id, proposal = await self.inner.propose(
                    request, token, refresh=refresh, validate=validate
                )
        except D3ProposalRejected as exc:
            if self.config.d4_budget is None:
                raise
            request_id = self._reviewable_action_rejection(
                exc, request.model_request_id
            )
            review_id, output = self._review(
                intent.turn_id,
                intent_id,
                "action",
                request_id,
                (request_id, fact_id),
            )
            if await self._route(review_id, output, intent_id, receipts, token):
                return True
            raise D3RecoveryRequired(str(exc)) from exc
        if not isinstance(
            proposal.action, CompleteIntentAction
        ) or proposal.action.fact_ids != (fact_id,):
            self.inner.reject(request_id, "d3_completion_fact_rejected")
            review_id, output = self._review(
                intent.turn_id,
                intent_id,
                "action",
                request_id,
                (request_id, fact_id),
            )
            if await self._route(review_id, output, intent_id, receipts, token):
                return True
            raise D3RecoveryRequired("d3_completion_fact_rejected")
        if proposal.review_requested:
            review_id, output = self._review(
                intent.turn_id,
                intent_id,
                "action",
                request_id,
                (request_id, fact_id),
            )
            if await self._route(review_id, output, intent_id, receipts, token):
                return True
        self.validator._allowed(
            self.state.task_run.task_run_id,
            intent.turn_id,
            intent_id,
            "complete_intent",
            None,
        )
        self.control.complete_intent(
            intent_id,
            plan.plan_id,
            fact_id,
            request_id,
        )
        self.store.mark_d3_operation_applied(request_id, intent_id)
        self.control.complete_turn(intent.turn_id, intent_id, fact_id)
        return False

    def _tail_intent(self, turn_id: str) -> str:
        intents = [item for item in self.state.intents if item.turn_id == turn_id]
        terminal = [
            item
            for item in intents
            if item.status
            in {
                IntentStatus.SUCCEEDED,
                IntentStatus.ABANDONED,
                IntentStatus.FAILED,
                IntentStatus.INCONCLUSIVE,
            }
        ]
        if not terminal:
            raise D3RecoveryRequired("d3_terminal_turn_has_no_intent")
        return terminal[-1].intent_id

    def _tail(self, turn_id: str) -> None:
        intent_id = self._tail_intent(turn_id)
        _, output = self._review(
            turn_id,
            intent_id,
            "turn_end",
            None,
            (turn_id,),
        )
        if output.decision != "acknowledge_turn_end":
            raise D3RecoveryRequired("d3_tail_review_not_read_only")

    async def _run(self, token: CancellationToken) -> D3Result:
        self._reconcile_known_d4_operations()
        if isinstance(self.review_ledger, D4ReviewLedger):
            self.review_ledger.reconcile_known_completions(
                self.state.task_run.task_run_id
            )
        self._check_role()
        self._check_dispatch()
        receipts = await self._import_all(token)
        if not self.state.turns:
            request_id, proposal = await self._outer_proposal(1, receipts, token)
            binding = self.control.register_turn(
                proposal,
                request_id,
                receipts[proposal.attachment_index],
            )
            self.store.mark_d3_operation_applied(request_id, binding.turn_id)
        for _ in range(32):
            self._check_dispatch()
            for turn in self.state.turns:
                if turn.status in {
                    TurnStatus.COMPLETED,
                    TurnStatus.FAILED,
                    TurnStatus.INCONCLUSIVE,
                    TurnStatus.SUPERSEDED,
                }:
                    self._tail(turn.turn_id)
            turn = self.state.turns[-1]
            if turn.status is TurnStatus.COMPLETED:
                return self.result("turn_completed_sandbox_retained")
            if turn.status in {TurnStatus.FAILED, TurnStatus.INCONCLUSIVE}:
                raise D3RecoveryRequired(
                    "d3_terminal_turn_requires_separate_next_proposal"
                )
            if turn.status is TurnStatus.PENDING:
                self.control.select_first(turn.turn_id)
                continue
            current = next(
                (
                    item
                    for item in self.state.intents
                    if item.turn_id == turn.turn_id
                    and item.status is IntentStatus.RUNNING
                ),
                None,
            )
            if current is None:
                winner = next(
                    (
                        item
                        for item in self.state.intents
                        if item.turn_id == turn.turn_id
                        and item.status is IntentStatus.SUCCEEDED
                    ),
                    None,
                )
                if winner is None:
                    raise D3RecoveryRequired("d3_running_turn_has_no_active_intent")
                plan = [
                    item
                    for item in self.state.plans
                    if item.intent_id == winner.intent_id
                ][-1]
                result = self.store.load_d3_plan_result(plan.plan_id)
                if result is None or result.supporting_fact_id is None:
                    raise D3RecoveryRequired("d3_winner_result_missing")
                self.control.complete_turn(
                    turn.turn_id, winner.intent_id, result.supporting_fact_id
                )
                continue
            plans = [
                item for item in self.state.plans if item.intent_id == current.intent_id
            ]
            if not plans:
                turn_binding = self.store.load_d3_turn_binding(turn.turn_id)
                if turn_binding is None:
                    raise D3RecoveryRequired("d3_start_turn_binding_missing")
                review_id, output = self._review(
                    turn.turn_id,
                    current.intent_id,
                    "turn_start",
                    None,
                    (turn_binding.import_id, turn_binding.proposal_request_id),
                )
                if await self._route(
                    review_id, output, current.intent_id, receipts, token
                ):
                    continue
                await self._new_plan(current.intent_id, token)
                continue
            plan = plans[-1]
            binding = self.store.load_d3_plan_binding(plan.plan_id)
            if binding is None:
                raise D3RecoveryRequired("d3_plan_binding_missing")
            if plan.status is PlanStatus.RUNNING:
                if await self._run_plan(binding, receipts, token):
                    continue
                continue
            if plan.status is not PlanStatus.COMPLETED:
                raise D3RecoveryRequired("d3_current_plan_not_usable")
            if await self._complete_intent(current.intent_id, receipts, token):
                continue
        raise D3RecoveryRequired("d3_internal_transition_limit")

    async def run(self, token: CancellationToken) -> D3Result:
        with run_lock(self.directory):
            self._token = token
            try:
                if self.config.d4_budget is not None:
                    attached_task_run_id = self.controller.task_run.task_run_id
                    if self._bound_task_run_id is None:
                        self._bound_task_run_id = attached_task_run_id
                    elif self._bound_task_run_id != attached_task_run_id:
                        raise D4RecoveryRequired(
                            "d4_controller_task_run_binding_changed"
                        )
                    if (
                        self.store.load_d3_run_config(attached_task_run_id)
                        != self.config
                    ):
                        raise D4RecoveryRequired("d3_frozen_run_configuration_changed")
                if self.config.d5_enabled and not self._redaction_configured:
                    return self.result("d5_redaction_configuration_unavailable", recovery_required=True)
                return await self._run(token)
            except (
                D3RecoveryRequired,
                D3BudgetExhausted,
                D4BudgetExhausted,
                D4RecoveryRequired,
                D3ProposalRejected,
                D2ReviewPaused,
            ) as exc:
                if self.config.d4_budget is not None and token.is_cancelled:
                    try:
                        task_run_id = self._bound_task_run_id
                        if task_run_id is None:
                            raise D4RecoveryRequired("d4_cancellation_task_run_unbound")
                        self._persist_cancel_intent()
                        self._reconcile_known_d4_operations()
                        if isinstance(self.review_ledger, D4ReviewLedger):
                            self.review_ledger.reconcile_known_completions(task_run_id)
                        stopped = self._settle_stoppage()
                    except (D3RecoveryRequired, D4RecoveryRequired) as recovery_exc:
                        return self.result(str(recovery_exc), recovery_required=True)
                    if stopped is not None:
                        return self.result(
                            stopped,
                            recovery_required=(
                                stopped == "d4_operation_outcome_unknown"
                                or stopped.endswith("cleanup_unknown")
                            ),
                        )
                if isinstance(exc, (D3BudgetExhausted, D4BudgetExhausted)):
                    return self.result(
                        f"budget_exhausted:{exc}", recovery_required=False
                    )
                return self.result(str(exc), recovery_required=True)
            except _D4TerminalStop as exc:
                return self.result(exc.reason, recovery_required=exc.cleanup_unknown)
            finally:
                self._token = None

    async def share_artifact(
        self,
        artifact_version_id: str,
        target_intent_id: str,
        token: CancellationToken,
    ):
        with run_lock(self.directory):
            self._check_role()
            self._check_dispatch()
            share_id = self._id(f"share/{artifact_version_id}/{target_intent_id}")
            receipt = await self.workspace.share(
                artifact_version_id,
                target_intent_id,
                share_id,
                token,
            )
            await self.workspace.verify_share(share_id, token)
            return receipt

    async def audit_unknown_operation(
        self,
        operation_id: str,
        token: CancellationToken,
    ):
        with run_lock(self.directory):
            return await self.workspace.audit_unknown(operation_id, token)

    def close(self) -> None:
        self.controller.close()
        self.store.close()
