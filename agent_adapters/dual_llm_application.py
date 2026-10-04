"""Internal D1/D2 application assembly; public CLI/workbench remain unchanged."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable
from uuid import uuid4

from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.model_evidence import D1ModelEvidenceStore
from agent_adapters.observation_proof_store import T6ObservationProofStore
from agent_adapters.postcondition_store import T6PostconditionStore
from agent_adapters.review_ledger import FileD2ReviewLedger
from agent_adapters.run_files import run_lock
from agent_adapters.workspace_evidence import FileD1WorkspaceRecorder
from agent_core.attachment_import import AttachmentImportRequest
from agent_core.attachment_importer import AttachmentImporter
from agent_core.attachment_workspace import (
    D1CommandPolicy,
    D1CopyPreparer,
    D1DigestObserver,
)
from agent_core.command_gateway import T6CommandGateway
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import (
    AppendD2ReviewApplication,
    ControlStateSnapshot,
    D2RunConfig,
    active_d2_pause_gates,
    canonical_json,
)
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.domain import IntentStatus, TurnStatus
from agent_core.modeling import CancellationToken
from agent_core.observation_proof import T6ObservationProofVerifier
from agent_core.review import D2ReviewCoordinator, D2ReviewPaused, D2ReviewSession
from agent_core.review_contracts import (
    D2ModelFailure,
    D2ReviewRequest,
)
from agent_core.runtime import TaskRunStatus
from agent_core.single_turn_runner import D1SingleTurnRunner, D1TurnRuleEvaluator
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_contracts import (
    D1OperationCompletion,
    D1OperationStart,
    D1RecoveryRequired,
    D1RunConfig,
)
from agent_core.turn_modeling import (
    D1AuditedModel,
    D1ModelAdapterPort,
    digest,
    stable_id,
)


@dataclass(frozen=True, slots=True)
class _EvidenceState:
    control_state: ControlStateSnapshot


class _SavedPostconditions:
    def __init__(self, application) -> None:
        self.application = application

    def bind(self, binding):
        self.application.postconditions.bind(binding)

    def read(self, plan_id, intent_id=None):
        return self.application.postconditions.read(plan_id, intent_id)

    def _observer(self, binding):
        state = self.application.store.load_control_state_rows(binding.task_run_id)
        intent = (
            next(
                (item for item in state.intents if item.intent_id == binding.intent_id),
                None,
            )
            if state
            else None
        )
        turn_binding = (
            self.application.store.load_d1_turn_binding(intent.turn_id)
            if intent
            else None
        )
        if turn_binding is None:
            raise D1RecoveryRequired("postcondition_turn_binding_missing")
        # Repository attach validates evidence before Controller has a bound cache.
        return D1DigestObserver(
            _EvidenceState(state),
            self.application.store,
            self.application.workspace,
            self.application.digest_sandbox,
            self.application.postconditions,
            turn_binding,
            clock=self.application.clock,
        )

    def read_file(self, binding, index, path):
        return self._observer(binding).read_file(binding, index, path)

    def observe_file(self, binding, index, path, loader):
        return self.read_file(binding, index, path)


class D1Application:
    _d2_enabled = False

    def __init__(
        self,
        directory: Path,
        *,
        config: D1RunConfig,
        sandbox,
        import_sandbox,
        copy_sandbox,
        command_sandbox,
        digest_sandbox,
        outer_model: D1ModelAdapterPort,
        inner_model: D1ModelAdapterPort,
        clock: Callable[[], float],
        review_model=None,
        review_ledger=None,
    ) -> None:
        self.config = config
        self.clock = clock
        self.directory = directory
        self.store = SqliteControlStore.open(directory / "control.sqlite", clock=clock)
        self.commands = T6CommandRecorder(directory / "commands")
        self.postconditions = T6PostconditionStore(directory / "postconditions")
        self.workspace = FileD1WorkspaceRecorder(directory / "workspace-evidence")
        self.imports = FileAttachmentImportRecorder(directory / "imports")
        self.model_evidence = D1ModelEvidenceStore(directory / "models")
        self.review_model = review_model
        self.review_ledger = review_ledger
        self.d2_coordinator = None
        self.d2_session = None
        if review_model is not None:
            if review_ledger is None:
                review_ledger = FileD2ReviewLedger(directory / "d2-reviews")
            self.review_ledger = review_ledger
            self.d2_coordinator = D2ReviewCoordinator(review_ledger)
            self.d2_session = D2ReviewSession(self.d2_coordinator)
        self.digest_sandbox = digest_sandbox
        self.command_sandbox = command_sandbox
        self.evidence_postconditions = _SavedPostconditions(self)
        self.proofs = T6ObservationProofVerifier(
            self.store,
            self.evidence_postconditions,
            self.commands,
            T6ObservationProofStore(directory / "proofs"),
            clock,
        )
        self.evaluator = D1TurnRuleEvaluator(
            self.store,
            self.imports,
            self.evidence_postconditions,
            self.commands,
            self.proofs,
            self.workspace,
        )
        self.controller = TaskRunController(
            sandbox,
            clock=clock,
            store=self.store,
            repository=ControlStateRepository(
                self.store, self.commands, t6_observation_proofs=self.proofs
            ),
            registry=TaskRunControllerRegistry(),
            attachment_imports=self.imports,
            t6_observation_proofs=self.proofs,
            d1_turn_evaluator=self.evaluator,
        )
        self.importer = AttachmentImporter(
            controller=self.controller,
            recorder=self.imports,
            sandbox=import_sandbox,
            clock=clock,
        )
        self.copies = D1CopyPreparer(
            self.controller,
            self.store,
            self.workspace,
            copy_sandbox,
            self.imports,
            clock=clock,
        )
        self.outer = D1AuditedModel(
            self.controller,
            self.store,
            self.model_evidence,
            outer_model,
            config.outer_role,
            clock,
        )
        self.inner = D1AuditedModel(
            self.controller,
            self.store,
            self.model_evidence,
            inner_model,
            config.inner_role,
            clock,
            d2_action_envelope=review_model is not None,
        )

    def start(self, request: TaskRequest, source_root: Path) -> str:
        from uuid import uuid4

        result = self.controller.start(
            request,
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=str(source_root.resolve(strict=True)),
            d1_config=self.config,
        )
        return result.value

    def observer(self, binding):
        return D1DigestObserver(
            self.controller,
            self.store,
            self.workspace,
            self.digest_sandbox,
            self.postconditions,
            binding,
            clock=self.clock,
        )

    def gateway(self, binding):
        return T6CommandGateway(
            controller=self.controller,
            policy=D1CommandPolicy(
                binding, self.config, self.postconditions, self.store
            ),
            sandbox=self.command_sandbox,
            recorder=self.commands,
            clock=self.clock,
            step_limit_seconds=60,
        )

    def runner(self):
        task_id = self.controller.control_state.task_run.task_run_id
        return D1SingleTurnRunner(
            controller=self.controller,
            ledger=self.store,
            config=self.config,
            imports=self.imports,
            import_id=stable_id(task_id, "import"),
            outer=self.outer,
            inner=self.inner,
            copy_preparer=self.copies,
            observer_factory=self.observer,
            gateway_factory=self.gateway,
            postconditions=self.postconditions,
            commands=self.commands,
            proofs=self.proofs,
            evaluator=self.evaluator,
            proof_claims=self.workspace,
            clock=self.clock,
            d2_session=self.d2_session,
            d2_model=self.review_model,
            d2_coordinator=self.d2_coordinator,
        )

    async def run(self, token: CancellationToken):
        runner = self.runner()
        try:
            return await self._run(runner, token)
        except Exception as exc:
            return runner.result(str(exc), "inconclusive", True)

    async def _run(self, runner: D1SingleTurnRunner, token: CancellationToken):
        if runner._interrupt(token):
            return runner.result("cancelled_or_deadline")
        task = self.controller.control_state.task_run
        if (
            self.store.load_d2_run_config(task.task_run_id) is not None
        ) != self._d2_enabled:
            return runner.result("d2_chain_identity_required", "inconclusive", True)
        if self.store.load_d1_run_config(task.task_run_id) != self.config:
            return runner.result("run_configuration_changed", "inconclusive", True)
        if (
            self.outer.adapter.role_config != self.config.outer_role
            or self.inner.adapter.role_config != self.config.inner_role
        ):
            raise D1RecoveryRequired("adapter_role_configuration_changed")
        import_id = stable_id(task.task_run_id, "import")
        operation = self.store.load_d1_operation(import_id)
        receipt = self.imports.read_receipt(import_id)
        if operation is not None and receipt is None:
            return runner.result(
                "attachment_import_outcome_unknown", "inconclusive", True
            )
        if receipt is None:
            if task.source_root is None:
                raise D1RecoveryRequired("attachment_source_root_missing")
            request = AttachmentImportRequest(
                import_id,
                task.task_run_id,
                task.sandbox_id,
                0,
                task.source_root,
                "/workspace/input/d1-attachment",
            )
            self.store.begin_d1_operation(
                D1OperationStart(
                    import_id,
                    task.task_run_id,
                    "attachment_import",
                    self.clock(),
                    self.controller.control_state.latest_checkpoint.sequence,
                    digest(asdict(request)),
                    resource_ref=request.sandbox_path,
                )
            )
            try:
                receipt = await self.importer.import_attachment(request, token)
            except Exception as exc:
                runner._interrupt(token)
                return runner.result(type(exc).__name__, "inconclusive", True)
        self.store.complete_d1_operation(
            D1OperationCompletion(
                import_id,
                receipt.completed_at,
                "completed",
                import_id,
                digest(asdict(receipt)),
            )
        )
        self.store.mark_d1_operation_applied(import_id, import_id)
        return await runner.run(token)

    def close(self) -> None:
        self.controller.close()
        self.store.close()


def _review_source_version(value):
    """Hash a trusted evidence record without decoding or exposing binary fields."""

    def normalize(item):
        if isinstance(item, bytes):
            return {
                "byte_length": len(item),
                "sha256": hashlib.sha256(item).hexdigest(),
            }
        if isinstance(item, bytearray):
            return normalize(bytes(item))
        if isinstance(item, Enum):
            return normalize(item.value)
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, dict):
            return {str(key): normalize(part) for key, part in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(part) for part in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError(f"unsupported review source value: {type(item).__name__}")

    return digest(normalize(value))


def _review_source_summary(value):
    if "model_evidence" in value:
        evidence = value["model_evidence"]
        rejections = [
            value[key]["rejection_code"]
            for key in ("application_rejection", "dispatch_rejection")
            if value[key] is not None
        ]
        return {
            "kind": "action_candidate",
            "outcome": evidence["outcome"],
            "rejections": rejections,
        }
    if "observed" in value:
        observed = value["observed"]
        return {
            "kind": "digest_observation",
            "status": observed["status"],
            "sha256": observed.get("sha256"),
        }
    if "exit_code" in value:
        return {
            "kind": "execution",
            **{
                key: value[key]
                for key in (
                    "status",
                    "exit_code",
                    "dispatched",
                    "timed_out",
                    "termination_confirmed",
                    "stream_complete",
                )
            },
        }
    if "fact_id" in value:
        return {
            "kind": "fact",
            "status": value["status"],
            "source_ref": value["source_ref"],
            "verification_ref": value["verification_ref"],
        }
    return {"kind": "attachment_import", "sha256": value["sha256"]}


class _ReviewCall:
    def __init__(self, application) -> None:
        self.application = application

    def invoke(self, request):
        app = self.application
        app._check_role()
        app._check_dispatch(request.review_root_id)
        finished = threading.Event()
        values = []

        def call():
            try:
                values.append((True, app.review_model.invoke(request)))
            except BaseException as exc:
                values.append((False, exc))
            finally:
                finished.set()

        # A late provider response can only write to this local list.
        threading.Thread(target=call, daemon=True, name="d2-review-call").start()
        end = time.monotonic() + min(
            180, app.controller.control_state.task_run.deadline_at - app.clock()
        )
        while not finished.wait(0.01):
            if app._dispatch_stopped() or time.monotonic() >= end:
                return D2ModelFailure("unknown", "review_call_outcome_unknown")
        succeeded, raw = values[0]
        if not succeeded:
            if isinstance(raw, (TimeoutError, ConnectionError)):
                return D2ModelFailure("unknown", "review_call_outcome_unknown")
            return D2ModelFailure("provider_failure", "review_provider_failure")
        return raw


class D2Application(D1Application):
    """Explicit new D2 runs; public CLI and workbench remain unchanged."""

    _d2_enabled = True

    def __init__(self, directory: Path, *, review_model, **kwargs) -> None:
        super().__init__(directory, review_model=review_model, **kwargs)
        self.d2_config = D2RunConfig(self.config.outer_role.role_config_ref)
        self._token = None
        self._review_request = None
        self.review_ledger = FileD2ReviewLedger(directory / "d2-reviews")
        self.d2_coordinator = D2ReviewCoordinator(
            self.review_ledger,
            apply=self._apply_review,
            pause=self._pause_review,
            refresh=self._refresh_review,
            committed=self._review_committed,
        )
        self.d2_session = D2ReviewSession(self.d2_coordinator)
        self._review_call = _ReviewCall(self)
        self._check_role()

    def start(self, request, source_root):
        from agent_core.contracts import ModelBackend

        self._check_role()
        result = self.controller.start(
            request,
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            source_root=str(source_root.resolve(strict=True)),
            d1_config=self.config,
            d2_config=self.d2_config,
        )
        return result.value

    def runner(self):
        runner = super().runner()
        runner.d2_model = self._review_call
        runner.d2_request_builder = self._make_review
        runner.d2_gate = self._gate
        runner.d2_pause = self._runner_pause
        return runner

    def _check_role(self):
        if self.review_model.role_config != self.config.outer_role:
            raise D1RecoveryRequired("review_role_configuration_changed")

    def _dispatch_stopped(self):
        state = self.controller.control_state
        return (
            state.task_run.status is not TaskRunStatus.RUNNING
            or self.clock() >= state.task_run.deadline_at
            or self._token is not None
            and self._token.is_cancelled
        )

    def _check_dispatch(self, review_root_id=None):
        if self._dispatch_stopped():
            raise D2ReviewPaused("review_dispatch_stopped")
        task_id = self.controller.control_state.task_run.task_run_id
        if self.store.load_d2_run_config(task_id) != self.d2_config:
            raise D1RecoveryRequired("d2_run_configuration_changed")
        for item in active_d2_pause_gates(
            self.store.list_d2_review_applications(task_id)
        ):
            if (
                item.pause_reason == "review_application_uncommitted"
                and item.review_root_id == review_root_id
            ):
                continue
            raise D2ReviewPaused(item.pause_reason or "review_paused")

    def _constraint_version(self):
        task_id = self.controller.control_state.task_run.task_run_id
        return max(
            (
                item.constraint_version
                for item in self.store.list_d2_review_applications(task_id)
            ),
            default=0,
        )

    def _saved_source(self, reference):
        state = self.controller.control_state
        fact = next((item for item in state.facts if item.fact_id == reference), None)
        if fact is not None:
            return asdict(fact)
        if state.turns:
            binding = self.store.load_d1_turn_binding(state.turns[-1].turn_id)
            if binding is not None and reference == binding.import_id:
                receipt = self.imports.read_receipt(reference)
                if receipt is not None:
                    return asdict(receipt)
                raise D1RecoveryRequired("review_import_source_unavailable")
        model_evidence = self.model_evidence.read(reference)
        if model_evidence is not None:
            application_rejection = self.model_evidence.read_application_rejection(
                reference
            )
            dispatch_rejection = self.model_evidence.read_dispatch_rejection(reference)
            return {
                "model_evidence": asdict(model_evidence),
                "application_rejection": (
                    asdict(application_rejection)
                    if application_rejection is not None
                    else None
                ),
                "dispatch_rejection": (
                    asdict(dispatch_rejection)
                    if dispatch_rejection is not None
                    else None
                ),
            }
        for reader in (self.commands.read_completed, self.workspace.read_digest):
            value = reader(reference)
            if value is not None:
                if hasattr(value, "record"):
                    return asdict(value.record)
                return asdict(value)
        raise D1RecoveryRequired("review_source_unavailable")

    def _make_review(self, binding, scope, candidate_id, source_ids):
        state = self.controller.control_state
        suffix = candidate_id or "turn"
        root_id = stable_id(binding.task_run_id, f"d2/{scope}/root/{suffix}")
        sources = []
        versions = {}
        source_summaries = {}
        for reference in source_ids:
            if reference in {"fixed_probe_rejected", "completion_fact_rejected"}:
                continue
            source = self._saved_source(reference)
            versions[reference] = _review_source_version(source)
            source_summaries[reference] = _review_source_summary(source)
            sources.append(reference)
        if not sources:
            raise D1RecoveryRequired("review_sources_empty")
        version = self._constraint_version()
        context = {
            "turn_target": state.turns[0].turn_target,
            "binding": asdict(binding),
            "turn_status": state.turns[0].status.value,
            "intent_status": state.intents[0].status.value,
            "source_versions": versions,
            "source_summaries": source_summaries,
            "constraint_version": version,
        }
        summary = {
            **context,
            "checkpoint": state.latest_checkpoint.sequence,
        }
        value = D2ReviewRequest(
            stable_id(binding.task_run_id, f"d2/{scope}/request/{suffix}"),
            root_id,
            1,
            1,
            scope,
            binding.task_run_id,
            binding.turn_id,
            binding.intent_id,
            candidate_id,
            state.latest_checkpoint.sequence,
            digest(context),
            tuple(sources),
            version,
            canonical_json(summary),
        )
        root = self.review_ledger.read_root(root_id)
        if root is not None and root.get("applied"):
            saved_fields = root["last_request"]
            saved = D2ReviewRequest(
                **{
                    **saved_fields,
                    "source_versions": tuple(saved_fields["source_versions"]),
                }
            )
            application = self.store.load_d2_review_application(
                binding.task_run_id, saved.review_id
            )
            prior_summary = json.loads(saved.context_summary)
            semantic_summary = {
                key: part
                for key, part in summary.items()
                if key not in {"checkpoint", "constraint_version"}
            }
            prior_semantic_summary = {
                key: part
                for key, part in prior_summary.items()
                if key not in {"checkpoint", "constraint_version"}
            }
            if (
                application is not None
                and application.constraint_version == version
                and canonical_json(semantic_summary)
                == canonical_json(prior_semantic_summary)
            ):
                # Applying this response's own constraints is not a new review trigger.
                value = saved
        self._review_request = value
        return value

    def _refresh_review(self, request):
        self._check_dispatch(request.review_root_id)
        binding = self.store.load_d1_turn_binding(request.turn_id)
        if binding is None:
            raise D1RecoveryRequired("review_binding_unavailable")
        fresh = self._make_review(
            binding,
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

    def _review_committed(self, request):
        return (
            self.store.load_d2_review_application(
                request.task_run_id, request.review_id
            )
            is not None
        )

    def _apply_review(self, request, output):
        reference = f"d2-review:{request.review_id}"
        application = AppendD2ReviewApplication(
            request.review_root_id,
            request.review_generation,
            request.review_id,
            reference,
            request.context_version,
            request.constraint_version + bool(output.constraints),
            output.constraints,
            False,
            request.turn_id,
            request.intent_id,
            request.review_scope,
            request.action_candidate_id,
        )

        def validate(_application):
            self._check_dispatch(request.review_root_id)
            self._check_role()
            fresh = self._refresh_review(request)
            if (
                fresh.context_version != request.context_version
                or fresh.constraint_version != request.constraint_version
            ):
                raise D2ReviewPaused("review_context_stale")

        self.controller.apply_d2_review(
            application,
            stable_id(request.task_run_id, f"d2/apply/{request.review_id}"),
            evidence_validator=validate,
        )
        return reference

    def _pause_review(self, request, reason, outcome):
        state = self.controller.control_state
        if (
            state.task_run.status is not TaskRunStatus.RUNNING
            or self.clock() >= state.task_run.deadline_at
            or self._token is not None
            and self._token.is_cancelled
        ):
            return
        review_id = stable_id(
            request.task_run_id, f"d2/pause/{request.review_root_id}/{reason}"
        )
        existing = self.store.load_d2_review_application(request.task_run_id, review_id)
        if existing is not None:
            if (
                existing.pause_reason != reason
                or existing.review_root_id != request.review_root_id
            ):
                raise D1RecoveryRequired("review_pause_identity_changed")
            return
        application = AppendD2ReviewApplication(
            request.review_root_id,
            request.review_generation,
            review_id,
            f"d2-pause:{review_id}",
            request.context_version,
            self._constraint_version(),
            (),
            True,
            request.turn_id,
            request.intent_id,
            request.review_scope,
            request.action_candidate_id,
            reason,
        )
        self.controller.apply_d2_review(application, review_id)

    def _runner_pause(self, error):
        if self._review_request is not None:
            self._pause_review(self._review_request, error.reason, error.outcome)

    def _gate(self, kind, timeout):
        self._check_dispatch()
        state = self.controller.control_state
        allowed = {"probe", "complete_intent"}
        upper = 300
        from agent_core.review_contracts import D2AllowedActionKinds

        for application in self.store.list_d2_review_applications(
            state.task_run.task_run_id
        ):
            for constraint in application.output_constraints:
                live = (
                    constraint.scope_kind == "intent"
                    and any(
                        item.intent_id == constraint.scope_id
                        and item.status is IntentStatus.RUNNING
                        for item in state.intents
                    )
                    or constraint.scope_kind == "turn"
                    and any(
                        item.turn_id == constraint.scope_id
                        and item.status is TurnStatus.RUNNING
                        for item in state.turns
                    )
                )
                if not live:
                    continue
                if isinstance(constraint, D2AllowedActionKinds):
                    allowed &= set(constraint.allowed_action_kinds)
                else:
                    upper = min(upper, constraint.max_action_timeout_seconds)
        if kind not in allowed:
            raise D2ReviewPaused(
                "action_kind_rejected_by_review_constraint", outcome="rejected"
            )
        if kind == "probe" and timeout > upper:
            raise D2ReviewPaused(
                "action_timeout_rejected_by_review_constraint", outcome="rejected"
            )

    async def run(self, token):
        with run_lock(self.directory):
            return await self._run_locked(token)

    async def _run_locked(self, token):
        self._token = token
        try:
            self._check_role()
            state = self.controller.control_state
            if not self._dispatch_stopped():
                for application in active_d2_pause_gates(
                    self.store.list_d2_review_applications(state.task_run.task_run_id)
                ):
                    if application.paused_gate:
                        root = self.review_ledger.read_root(application.review_root_id)
                        if (
                            application.pause_reason == "review_application_uncommitted"
                            and root is not None
                            and root.get("last_outcome") == "accepted"
                        ):
                            continue
                        outcome = (
                            "satisfied"
                            if state.turns
                            and state.turns[0].status is TurnStatus.COMPLETED
                            else "not_satisfied"
                            if application.pause_reason == "no_safe_continuation_in_d2"
                            else "inconclusive"
                        )
                        return self.runner().result(
                            application.pause_reason,
                            outcome,
                            root is not None and root.get("last_outcome") == "unknown",
                        )
            return await super().run(token)
        finally:
            self._token = None
