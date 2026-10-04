"""Internal bounded D3 multi-path application; no public CLI integration."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.model_evidence import FileD3ModelEvidence
from agent_adapters.observation_proof_store import FileD3ProofStore
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
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_evidence_validator import D3EvidenceValidator
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
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


class _D3ReviewCall:
    def __init__(self, application: D3Application) -> None:
        self.application = application

    def invoke(self, request: D2ReviewRequest):
        application = self.application
        application._check_role()
        application._check_dispatch()
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
    ) -> None:
        self.directory = directory
        self.config = config
        self.clock = clock
        self.sandbox = sandbox
        self.review_model = review_model
        self._token: CancellationToken | None = None
        self.store = SqliteControlStore.open(directory / "control.sqlite", clock=clock)
        self.imports = FileAttachmentImportRecorder(directory / "imports")
        self.commands = T6CommandRecorder(directory / "commands")
        self.workspace_evidence = FileD3WorkspaceEvidence(directory / "d3-workspace")
        self.model_evidence = FileD3ModelEvidence(directory / "d3-models")
        self.review_ledger = FileD2ReviewLedger(directory / "d3-reviews")
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
            outer_model,
            config,
            clock,
        )
        self.inner = D3AuditedModel(
            self.controller,
            self.store,
            self.model_evidence,
            inner_model,
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

    def _check_dispatch(self) -> None:
        if self._dispatch_stopped():
            raise D3RecoveryRequired("d3_dispatch_stopped")
        for application in self.store.list_d3_review_applications(
            self.state.task_run.task_run_id
        ):
            if application.paused_gate:
                raise D3RecoveryRequired(application.pause_reason or "d3_review_paused")

    def result(self, reason: str, *, recovery_required: bool = False) -> D3Result:
        state = self.state
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
            "retained",
            "stopped",
            reason,
            recovery_required,
        )

    def start(self, request: TaskRequest, source_root: Path) -> str:
        if (
            self.outer.adapter.role_config != self.config.outer_role
            or self.inner.adapter.role_config != self.config.inner_role
            or self.review_model.role_config != self.config.outer_role
        ):
            raise D3RecoveryRequired("d3_adapter_role_configuration_changed")
        committed = self.controller.start(
            request,
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=str(source_root.resolve(strict=True)),
            d3_config=self.config,
        )
        return committed.value

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
        request_id, proposal = await self.outer.propose(request, token)
        if (
            proposal.attachment_index
            not in self.config.authorization.attachment_indices
        ):
            self.outer.reject(request_id, "d3_attachment_not_authorized")
            raise D3ProposalRejected("d3_attachment_not_authorized")
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
                return result.review_id, result.output
            application, _, output = self.validator._review_call(
                request.task_run_id, prior[-1].review_id
            )
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
        action_context = D3ActionContext(
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
            await self._shared_context(plan_binding.intent_id, token),
        )
        request = self._model_request(
            stage,
            f"{plan_binding.plan_id}:{stage}",
            action_context,
            f"action/{plan_binding.plan_id}/{stage}",
        )
        request_id, proposal = await self.inner.propose(request, token)
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
        request_id, proposal = await self.inner.propose(request, token)
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
                return await self._run(token)
            except (
                D3RecoveryRequired,
                D3BudgetExhausted,
                D3ProposalRejected,
                D2ReviewPaused,
            ) as exc:
                return self.result(str(exc), recovery_required=True)
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
