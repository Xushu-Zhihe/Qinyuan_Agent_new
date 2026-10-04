from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any

from agent_core.attachment_import import AttachmentImportEvidencePort
from agent_core.contracts import (
    ModelBackend,
    TaskRequest,
    TaskRequestError,
    resolve_model_backend,
)
from agent_core.control_state_repository import ControlStateRepository, RecoveryResult
from agent_core.control_store import (
    AppendD2ReviewApplication,
    CommittedOperation,
    ControlCommitResult,
    ControlMutation,
    ControlStateSnapshot,
    ControlStore,
    D2RunConfig,
    FactRecordSnapshot,
    IdempotencyConflictError,
    IntentGoalBindingResult,
    StaleControlStateError,
    StorageError,
    StateChange,
    TargetNetworkBinding,
    TaskRunNotFoundError,
    TaskRunSnapshot,
    TurnOperation,
    TurnRegistration,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertIntentGoal,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
)
from agent_core.dual_llm_contracts import D3CommitMetadata, D3RunConfig
from agent_core.turn_contracts import (
    D1RunConfig,
    D1TurnBinding,
    D1TurnEvaluatorPort,
    D1TurnProof,
)
from agent_core.domain import (
    ExecutionStatus,
    FactStatus,
    IntentStatus,
    PlanStatus,
    TurnStatus,
)
from agent_core.file_inspection import FILE_TOOL
from agent_core.file_type_fact import FileTypeFactCandidate, fact_content
from agent_core.file_type_verifier import (
    FileTypeProofEvidencePort,
    FileTypeVerificationError,
)
from agent_core.intent_goal_evaluator import (
    IntentGoalEvaluation,
    IntentGoalEvaluationError,
    IntentGoalEvaluatorPort,
)
from agent_core.intent_goals import FileTypeIntentGoal, IntentGoalBindingError
from agent_core.observation_proof import (
    ObservationProofError,
    T6ObservationProofEvidencePort,
    observation_fact_content,
)
from agent_core.runtime import (
    TERMINAL_STATUSES,
    InvalidTransition,
    RuntimeEvent,
    TaskRunSandboxPort,
    TaskRunStatus,
)
from agent_core.runtime import (
    TaskRunController as LegacyTaskRunController,
)
from agent_core.tool_contracts import ToolAuthorization


class ControllerRegistryError(RuntimeError):
    """Base error for in-process TaskRun controller ownership."""


class ControllerAlreadyClaimedError(ControllerRegistryError):
    """A TaskRun is already bound to another writable controller."""


class ControllerNotClaimedError(ControllerRegistryError):
    """A controller attempted to release a binding it does not own."""


class TaskRunControllerRegistry:
    """Process-local exclusive ownership for persistent TaskRun controllers."""

    def __init__(self) -> None:
        self._bindings: dict[str, object] = {}
        self._lock = RLock()

    def claim(self, task_run_id: str, controller: object) -> None:
        if not isinstance(task_run_id, str) or not task_run_id.strip():
            raise ValueError("task_run_id must be a non-empty string")
        if controller is None:
            raise ValueError("controller must not be None")
        with self._lock:
            existing = self._bindings.get(task_run_id)
            if existing is not None and existing is not controller:
                raise ControllerAlreadyClaimedError(
                    f"TaskRun is already claimed: {task_run_id}"
                )
            self._bindings[task_run_id] = controller

    def release(self, task_run_id: str, controller: object) -> None:
        with self._lock:
            existing = self._bindings.get(task_run_id)
            if existing is None or existing is not controller:
                raise ControllerNotClaimedError(
                    f"controller does not own TaskRun binding: {task_run_id}"
                )
            del self._bindings[task_run_id]

    def owner(self, task_run_id: str) -> object | None:
        with self._lock:
            return self._bindings.get(task_run_id)

    def is_claimed(self, task_run_id: str) -> bool:
        with self._lock:
            return task_run_id in self._bindings


class SandboxCleanupError(RuntimeError):
    """Sandbox compensation or terminal cleanup failed."""

    def __init__(
        self,
        sandbox_id: str,
        reason: str,
        *,
        original_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        super().__init__(f"Sandbox cleanup failed for {sandbox_id}: {reason}")


class CommittedStateReloadError(StorageError):
    """The mutation committed, but the post-commit control snapshot is unavailable."""

    committed = True

    def __init__(self, message: str, commit: ControlCommitResult | None = None) -> None:
        self.commit = commit
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _PersistentDependencies:
    store: ControlStore
    repository: ControlStateRepository
    registry: TaskRunControllerRegistry


def _clock_now(clock: Any) -> float:
    value = clock.now_utc() if hasattr(clock, "now_utc") else clock()
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Clock must return a numeric UTC Unix timestamp") from exc
    if not math.isfinite(result):
        raise ValueError("Clock must return a finite UTC Unix timestamp")
    return result


def _validate_mutation_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("mutation_id must be a UUID4 string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("mutation_id must be a UUID4 string") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ValueError("mutation_id must be a canonical UUID4 string")
    return value


def _validate_source_root(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise TaskRequestError("source_root must be a canonical absolute directory")
    root = Path(value)
    if not root.is_absolute():
        raise TaskRequestError("source_root must be a canonical absolute directory")
    try:
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TaskRequestError("source_root must be an existing directory") from exc
    if not canonical.is_dir() or str(canonical) != value:
        raise TaskRequestError("source_root must be a canonical absolute directory")
    return value


class TaskRunController(LegacyTaskRunController):
    """Own exactly one TaskRun, with an optional persistent control-plane mode."""

    def __init__(
        self,
        sandbox: Any,
        *,
        clock: Any = __import__("time").monotonic,
        store: ControlStore | None = None,
        repository: ControlStateRepository | None = None,
        registry: TaskRunControllerRegistry | None = None,
        attachment_imports: AttachmentImportEvidencePort | None = None,
        file_type_proofs: FileTypeProofEvidencePort | None = None,
        t6_observation_proofs: T6ObservationProofEvidencePort | None = None,
        intent_goal_evaluator: IntentGoalEvaluatorPort | None = None,
        target_network_adapter: Any | None = None,
        d1_turn_evaluator: D1TurnEvaluatorPort | None = None,
        d3_evidence_validator: Callable[[ControlMutation, ControlStateSnapshot], None] | None = None,
    ) -> None:
        super().__init__(sandbox, clock=clock if callable(clock) else _clock_now_wrapper(clock))
        supplied = (store, repository, registry)
        if any(item is not None for item in supplied) and not all(
            item is not None for item in supplied
        ):
            raise ValueError("persistent mode requires store, repository, and registry")
        self._persistent = (
            _PersistentDependencies(store, repository, registry)
            if store is not None and repository is not None and registry is not None
            else None
        )
        self._persistent_snapshot: TaskRunSnapshot | None = None
        self._persistent_state: ControlStateSnapshot | None = None
        self._persistent_task_run_id: str | None = None
        self._registry_released = False
        self._cache_invalid = False
        self._closed = False
        self._attachment_imports = attachment_imports
        self._file_type_proofs = file_type_proofs
        self._t6_observation_proofs = t6_observation_proofs
        self._intent_goal_evaluator = intent_goal_evaluator
        self._target_network = target_network_adapter
        self._target_network_destroyed = False
        self._d1_turn_evaluator = d1_turn_evaluator
        self._d3_evidence_validator = d3_evidence_validator

    def _now_utc(self) -> float:
        return _clock_now(self._clock)

    def _require_persistent(self) -> _PersistentDependencies:
        if self._persistent is None:
            raise InvalidTransition("persistent ControlStore dependencies are not configured")
        if self._cache_invalid:
            raise StorageError("controller cache is invalid; reload the TaskRun state")
        return self._persistent

    @property
    def task_run(self) -> Any:
        """Return the committed TaskRun view in persistent mode.

        Legacy callers still receive the mutable in-memory TaskRun.  Persistent
        callers must never observe the inherited, uncommitted legacy field.
        """

        if self._persistent is None:
            return super().task_run
        self._require_persistent()
        if self._persistent_snapshot is None:
            raise InvalidTransition("TaskRun has not been started or attached")
        return self._persistent_snapshot

    def poll(self) -> TaskRunStatus:
        """Keep the legacy polling API out of the persistent mutation path."""

        if self._persistent is None:
            return super().poll()
        raise InvalidTransition(
            "persistent TaskRun time changes require advance_time(mutation_id)"
        )

    def start(
        self,
        request: TaskRequest,
        effective_model_backend: ModelBackend | str | None = None,
        mutation_id: str | None = None,
        *,
        source_root: str | None = None,
        task_run_id: str | None = None,
        d1_config: D1RunConfig | None = None,
        d2_config: D2RunConfig | None = None,
        d3_config: D3RunConfig | None = None,
    ) -> Any:
        if self._persistent is None:
            if (
                effective_model_backend is not None
                or mutation_id is not None
                or source_root is not None
                or task_run_id is not None
                or d1_config is not None
                or d2_config is not None
                or d3_config is not None
            ):
                raise InvalidTransition("persistent dependencies are required for this start signature")
            return super().start(request)
        return self._start_persistent(
            request, effective_model_backend, mutation_id, source_root, task_run_id, d1_config, d2_config, d3_config,
        )

    def _start_persistent(
        self,
        request: TaskRequest,
        effective_model_backend: ModelBackend | str | None,
        mutation_id: str | None,
        source_root: str | None,
        task_run_id: str | None = None,
        d1_config: D1RunConfig | None = None,
        d2_config: D2RunConfig | None = None,
        d3_config: D3RunConfig | None = None,
    ) -> CommittedOperation:
        deps = self._require_persistent()
        if self._persistent_task_run_id is not None or self._closed:
            raise InvalidTransition("TaskRunController can only start or attach once")
        if not isinstance(request, TaskRequest):
            raise TaskRequestError("request must be a TaskRequest")
        if effective_model_backend is None:
            raise TaskRequestError("effective_model_backend must be specified")
        try:
            backend = resolve_model_backend(effective_model_backend, None)
        except TaskRequestError:
            raise
        if backend is None:
            raise TaskRequestError("effective_model_backend must be specified")
        _validate_mutation_id(mutation_id)
        assert mutation_id is not None
        if request.model_backend is not None and request.model_backend is not backend:
            raise TaskRequestError("TaskRequest model_backend conflicts with effective_model_backend")
        source_root = _validate_source_root(source_root)
        if d2_config is not None and (
            not isinstance(d2_config, D2RunConfig) or d1_config is None
        ):
            raise TaskRequestError("D2 requires its frozen configuration and fixed execution configuration")
        if d1_config is not None:
            if not isinstance(d1_config, D1RunConfig):
                raise TaskRequestError("d1_config must be a frozen D1RunConfig")
            attachments = request.target.attachments
            if request.target.endpoints or len(attachments) != 1 or attachments[0].kind != "local_path" or self._target_network is not None:
                raise TaskRequestError("D1 requires exactly one local attachment without network")
        if d3_config is not None:
            attachments = request.target.attachments
            if (
                not isinstance(d3_config, D3RunConfig)
                or d1_config is not None or d2_config is not None
                or request.target.endpoints or self._target_network is not None
                or len(attachments) != len(d3_config.authorization.attachment_indices)
                or any(item.kind != "local_path" for item in attachments)
            ):
                raise TaskRequestError("D3 requires one or two frozen local attachments without network")

        supplied_task_id = task_run_id is not None
        task_run_id = str(uuid.uuid4()) if task_run_id is None else _validate_mutation_id(task_run_id)
        if supplied_task_id and deps.store.load_task_run(task_run_id) is not None:
            raise InvalidTransition("TaskRun ID already exists; use attach")
        deps.registry.claim(task_run_id, self)
        self._registry_released = False
        sandbox_id: str | None = None
        target_network: TargetNetworkBinding | None = None
        committed = False
        try:
            started_at = self._now_utc()
            deadline_at = started_at + request.deadline_seconds
            if self._target_network is not None:
                endpoints = request.target.endpoints
                if len(endpoints) != 1:
                    raise TaskRequestError("T6 target network requires exactly one endpoint")
                target_network = self._target_network.create(task_run_id, endpoints[0].url)
                configure = getattr(self._sandbox, "configure_target_network", None)
                if not callable(configure):
                    raise InvalidTransition("Sandbox cannot bind a T6 target network")
                configure(task_run_id, target_network)
            sandbox_id = deps_sandbox_create(self._sandbox, task_run_id)
            change = UpsertTaskRun(
                task_run_id=task_run_id,
                request=request,
                effective_model_backend=backend,
                started_at=started_at,
                deadline_at=deadline_at,
                status=TaskRunStatus.RUNNING,
                sandbox_id=sandbox_id,
                source_root=source_root,
                target_network=target_network,
            )
            event = RuntimeEvent(
                type="task_run_started",
                task_run_id=task_run_id,
                timestamp=started_at,
                status=TaskRunStatus.RUNNING,
                details={
                    "operation": "start_task_run",
                    "entity_ids": [task_run_id],
                    "result": task_run_id,
                },
            )
            mutation = ControlMutation.create(
                task_run_id=task_run_id,
                mutation_id=mutation_id,
                operation="start_task_run",
                request=request,
                effective_model_backend=backend,
                base_checkpoint_sequence=0,
                state_changes=(change,),
                event=event,
                d1_config=d1_config,
                d2_config=d2_config,
                d3=D3CommitMetadata(config=d3_config) if d3_config is not None else None,
            )
            commit = deps.store.commit(mutation)
            committed = True
            # Preserve the committed identity if post-commit reload fails.
            self._persistent_task_run_id = task_run_id
            task_snapshot = deps.repository.load_task_run(task_run_id)
            state = deps.repository.load_control_state(task_run_id)
            if task_snapshot is None or state is None:
                raise StorageError("committed TaskRun could not be reloaded")
            self._persistent_task_run_id = task_run_id
            self._persistent_snapshot = task_snapshot
            self._persistent_state = state
            self._sandbox_destroyed = False
            self._target_network_destroyed = False
            return CommittedOperation(value=task_run_id, commit=commit)
        except Exception as exc:
            if committed:
                self._cache_invalid = True
                raise
            try:
                deps.registry.release(task_run_id, self)
                self._registry_released = True
            except Exception:
                pass
            if sandbox_id is not None:
                try:
                    self._sandbox.destroy(sandbox_id)
                except Exception as cleanup_exc:
                    raise SandboxCleanupError(
                        sandbox_id,
                        "compensating destroy failed after start failure",
                        original_error=exc,
                        cleanup_error=cleanup_exc,
                    ) from cleanup_exc
            if target_network is not None:
                try:
                    network = self._target_network
                    if network is None:
                        raise InvalidTransition("target network is not configured")
                    network.destroy(task_run_id, target_network)
                except Exception as cleanup_exc:
                    raise SandboxCleanupError(
                        sandbox_id or target_network.forwarder_id,
                        "compensating target network destroy failed after start failure",
                        original_error=exc,
                        cleanup_error=cleanup_exc,
                    ) from cleanup_exc
            raise

    def attach(self, task_run_id: str) -> ControlStateSnapshot:
        return self._attach(task_run_id, cancellation_only=False)

    def attach_for_cancellation(self, task_run_id: str) -> ControlStateSnapshot:
        """Bind persisted resources without admitting work before explicit cancel.

        A broken active resource must not prevent the TaskRun from reaching its
        terminal state. Resource ownership is still checked during cleanup.
        """

        return self._attach(task_run_id, cancellation_only=True)

    def _attach(
        self, task_run_id: str, *, cancellation_only: bool
    ) -> ControlStateSnapshot:
        deps = self._require_persistent()
        if self._persistent_task_run_id is not None or self._closed:
            raise InvalidTransition("TaskRunController can only start or attach once")
        if not isinstance(task_run_id, str) or not task_run_id.strip():
            raise ValueError("task_run_id must be a non-empty string")
        deps.registry.claim(task_run_id, self)
        self._registry_released = False
        try:
            snapshot = deps.repository.load_task_run(task_run_id)
            state = deps.repository.load_control_state(task_run_id)
            if snapshot is None or state is None:
                raise TaskRunNotFoundError(f"TaskRun does not exist: {task_run_id}")
            if isinstance(self._sandbox, TaskRunSandboxPort):
                non_executing = (
                    cancellation_only or snapshot.status in TERMINAL_STATUSES
                )
                if snapshot.target_network is not None:
                    if self._target_network is None:
                        raise InvalidTransition("T6 target network adapter is required")
                    self._target_network.bind(
                        task_run_id,
                        snapshot.target_network,
                        terminal=non_executing,
                    )
                    configure = getattr(self._sandbox, "configure_target_network", None)
                    if not callable(configure):
                        raise InvalidTransition("Sandbox cannot bind a T6 target network")
                    configure(task_run_id, snapshot.target_network)
                self._sandbox.bind(
                    task_run_id, snapshot.sandbox_id,
                    terminal=non_executing,
                )
            self._persistent_task_run_id = task_run_id
            self._persistent_snapshot = snapshot
            self._persistent_state = state
            self._target_network_destroyed = False
            self._sandbox_destroyed = False
            return state
        except Exception:
            try:
                deps.registry.release(task_run_id, self)
            finally:
                self._registry_released = True
            raise

    def status(self) -> TaskRunStatus:
        if self._persistent is None:
            return self.task_run.status
        self._require_persistent()
        if self._persistent_snapshot is None:
            raise InvalidTransition("TaskRun has not been started or attached")
        return self._persistent_snapshot.status

    def commit_turn_registration(
        self,
        registration: TurnRegistration,
        mutation_id: str,
        *,
        d1_binding: D1TurnBinding | None = None,
    ) -> CommittedOperation:
        """Atomically persist a prepared pending Turn and its fixed Intent list."""

        if self._persistent is None:
            raise InvalidTransition("persistent ControlStore dependencies are not configured")
        if not isinstance(registration, TurnRegistration):
            raise ValueError("registration must be a TurnRegistration")
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, _state = self._require_bound_state()
        if registration.task_run_id != task_run.task_run_id:
            raise InvalidTransition("TurnRegistration belongs to another TaskRun")
        deps = self._require_persistent()
        config = deps.store.load_d1_run_config(task_run.task_run_id)
        if (config is None) != (d1_binding is None):
            raise InvalidTransition("D1 registration requires the explicit immutable binding")
        if d1_binding is not None and (
            d1_binding.task_run_id != task_run.task_run_id
            or d1_binding.turn_id != registration.turn_id
            or registration.entity_ids != (d1_binding.turn_id, d1_binding.intent_id)
        ):
            raise InvalidTransition("D1 binding differs from its Turn/Intent registration")

        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            operation = replay.event.details.get("operation")
            if operation == "register_turn":
                if d1_binding is not None and deps.store.load_d1_turn_binding(registration.turn_id) != d1_binding:
                    raise IdempotencyConflictError("D1 binding differs from committed registration")
                return CommittedOperation.from_commit(replay)
            self._assert_deadline_preempted_replay(replay)
            return CommittedOperation.from_commit(replay)

        deps = self._require_persistent()
        if task_run.status is not TaskRunStatus.RUNNING:
            raise InvalidTransition("Turn registration requires a running TaskRun")
        now = self._now_utc()
        if now >= task_run.deadline_at:
            return self._finish_persistent(
                TaskRunStatus.DEADLINE_EXCEEDED,
                mutation_id=mutation_id,
                now=now,
                failure_reason=task_run.failure_reason,
            )
        if d1_binding is not None:
            imports = self._attachment_imports
            claim = None if imports is None else imports.read_claim(d1_binding.import_id)
            receipt = None if imports is None else imports.read_receipt(d1_binding.import_id)
            if (
                claim is None or receipt is None or claim.identity() != receipt.identity()
                or claim.claimed_at != receipt.claimed_at
                or receipt.task_run_id != task_run.task_run_id
                or receipt.sandbox_id != task_run.sandbox_id
                or receipt.source_root != task_run.source_root
                or receipt.attachment_index != d1_binding.attachment_index
                or receipt.local_path != task_run.request.target.attachments[d1_binding.attachment_index].value
                or receipt.sha256 != d1_binding.expected_sha256
                or not task_run.started_at <= receipt.claimed_at <= receipt.completed_at <= now
            ):
                raise InvalidTransition("D1 binding requires the matching successful trusted import receipt")

        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "register_turn",
                "entity_ids": list(registration.entity_ids),
                "result": registration.turn_id,
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="register_turn",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=registration.base_checkpoint_sequence,
            state_changes=registration.state_changes,
            event=event,
            d1_binding=d1_binding,
        )
        commit = deps.store.commit(mutation)
        self._reload_after_commit()
        return CommittedOperation.from_commit(commit)

    def commit_turn_operation(
        self,
        operation: TurnOperation,
        mutation_id: str,
    ) -> CommittedOperation:
        """Commit one Turn working-copy delta and reload the committed snapshot."""

        if self._persistent is None:
            raise InvalidTransition("persistent ControlStore dependencies are not configured")
        deps = self._persistent
        if not isinstance(operation, TurnOperation):
            raise ValueError("operation must be a TurnOperation")
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, _state = self._require_bound_state()
        if operation.task_run_id != task_run.task_run_id:
            raise InvalidTransition("TurnOperation belongs to another TaskRun")
        if self._closed or self._registry_released:
            raise InvalidTransition("TurnOperation requires an owned, open Controller")
        if deps.store.load_d1_run_config(task_run.task_run_id) is not None and any(
            isinstance(change, UpsertTurn) and change.status is TurnStatus.COMPLETED
            or isinstance(change, UpsertIntent) and change.status is IntentStatus.SUCCEEDED
            for change in operation.state_changes
        ):
            raise InvalidTransition("D1 success requires its dedicated Fact/Intent/Turn evidence gates")
        if deps.store.load_d3_run_config(task_run.task_run_id) is not None and operation.operation not in {
            "append_plan_validation_record", "create_execution", "start_execution",
            "complete_execution",
        }:
            raise InvalidTransition("D3 domain changes require their dedicated Controller gate")
        replay = self._load_existing_commit(mutation_id)
        now = replay.event.timestamp if replay is not None else self._now_utc()
        if replay is None:
            self._require_persistent()
            if task_run.status is not TaskRunStatus.RUNNING:
                raise InvalidTransition("TurnOperation requires a running TaskRun")
            if operation.base_checkpoint_sequence != self.control_state.latest_checkpoint.sequence:
                raise StaleControlStateError("TurnOperation was prepared from a stale checkpoint")
            if now >= task_run.deadline_at:
                raise InvalidTransition(
                    "TaskRun deadline elapsed; call advance_time(mutation_id) before further work"
                )
        changes = tuple(
            replace(change, finished_at=now)
            if isinstance(change, UpsertTaskRun) else change
            for change in operation.state_changes
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation=operation.operation,
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=operation.base_checkpoint_sequence,
            state_changes=changes,
            event=operation.event(timestamp=now),
        )
        if replay is not None:
            # The Store checks the original fingerprint. No evidence lookup,
            # snapshot reload, clock-driven transition or Sandbox call on replay.
            return CommittedOperation.from_commit(deps.store.commit(mutation))
        if deps.store.load_d3_run_config(task_run.task_run_id) is not None:
            if self._d3_evidence_validator is None:
                raise InvalidTransition("D3 evidence validator is not configured")
            self._d3_evidence_validator(mutation, self.control_state)
        deps.repository.validate_candidate(self.control_state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        if self.status() in TERMINAL_STATUSES:
            self._cleanup_after_terminal()
        return CommittedOperation.from_commit(commit)

    def commit_d3_transition(
        self,
        operation: str,
        state_changes: tuple[StateChange, ...],
        entity_ids: tuple[str, ...],
        result: str,
        mutation_id: str,
        *,
        metadata: D3CommitMetadata | None = None,
    ) -> CommittedOperation:
        """Commit one D3-only transition after independent evidence validation."""
        mutation_id = _validate_mutation_id(mutation_id)
        task, state = self._require_bound_state()
        deps = self._require_persistent()
        if deps.store.load_d3_run_config(task.task_run_id) is None:
            raise InvalidTransition("D3 transition requires an explicit D3 TaskRun")
        if self._d3_evidence_validator is None:
            raise InvalidTransition("D3 evidence validator is not configured")
        replay = self._load_existing_commit(mutation_id)
        now = replay.event.timestamp if replay is not None else self._now_utc()
        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": operation,
                "entity_ids": list(entity_ids),
                "result": result,
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task.task_run_id,
            mutation_id=mutation_id,
            operation=operation,
            request=task.request,
            effective_model_backend=task.effective_model_backend,
            base_checkpoint_sequence=(
                replay.checkpoint.sequence - 1
                if replay is not None else state.latest_checkpoint.sequence
            ),
            state_changes=state_changes,
            event=event,
            d3=metadata,
        )
        if replay is not None:
            return CommittedOperation.from_commit(deps.store.commit(mutation))
        if (
            self._closed or self._registry_released
            or task.status is not TaskRunStatus.RUNNING
            or now >= task.deadline_at
        ):
            raise InvalidTransition("D3 transition requires a running TaskRun before Deadline")
        self._d3_evidence_validator(mutation, state)
        if state.latest_checkpoint.sequence != self.control_state.latest_checkpoint.sequence:
            raise StaleControlStateError("D3 state changed during evidence validation")
        if self._now_utc() >= task.deadline_at:
            raise InvalidTransition("D3 Deadline elapsed during evidence validation")
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def bind_intent_goal(
        self,
        goal: FileTypeIntentGoal,
        import_id: str,
        mutation_id: str,
        authorization: ToolAuthorization,
    ) -> IntentGoalBindingResult:
        """Bind one file-type goal after Intent selection and before its Plan."""

        if not isinstance(goal, FileTypeIntentGoal):
            raise TypeError("goal must be a FileTypeIntentGoal")
        if not isinstance(authorization, ToolAuthorization):
            raise TypeError("authorization must be a ToolAuthorization")
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, state = self._require_bound_state()
        if goal.task_run_id != task_run.task_run_id:
            raise IntentGoalBindingError("task_run_mismatch")
        deps = self._require_persistent()

        replay = deps.store.load_committed_mutation(task_run.task_run_id, mutation_id)
        if replay is not None:
            existing = deps.store.load_intent_goal(goal.intent_id)
            if (
                replay.event.details.get("operation") != "bind_intent_goal"
                or existing is None
                or existing.goal != goal
                or existing.first_commit.event.sequence != replay.event.sequence
            ):
                raise IntentGoalBindingError("mutation_conflict")
            return IntentGoalBindingResult(
                existing.goal, existing.first_commit, "replayed"
            )

        now = self._now_utc()
        intent = next((item for item in state.intents if item.intent_id == goal.intent_id), None)
        if (
            self._closed
            or self._registry_released
            or task_run.status is not TaskRunStatus.RUNNING
            or now >= task_run.deadline_at
            or intent is None
            or intent.status is not IntentStatus.RUNNING
        ):
            raise IntentGoalBindingError("intent_not_running")
        imports = self._attachment_imports
        if imports is None:
            raise InvalidTransition("attachment import evidence is not configured")
        existing = deps.store.load_intent_goal(goal.intent_id)
        if existing is not None and existing.goal != goal:
            raise IntentGoalBindingError("goal_conflict")
        if existing is None and any(
            plan.intent_id == goal.intent_id for plan in state.plans
        ):
            raise IntentGoalBindingError("intent_not_running")

        def verify_import_scope() -> None:
            checked_at = self._now_utc()
            if checked_at >= task_run.deadline_at:
                raise IntentGoalBindingError("intent_not_running")
            claim = imports.read_claim(import_id)
            receipt = imports.read_receipt(import_id)
            attachments = task_run.request.target.attachments
            if (
                claim is None
                or receipt is None
                or claim.identity() != receipt.identity()
                or claim.claimed_at != receipt.claimed_at
                or receipt.task_run_id != task_run.task_run_id
                or receipt.sandbox_id != task_run.sandbox_id
                or receipt.source_root != task_run.source_root
                or receipt.sandbox_path != goal.path
                or receipt.attachment_index >= len(attachments)
                or attachments[receipt.attachment_index].kind != "local_path"
                or receipt.local_path != attachments[receipt.attachment_index].value
                or not task_run.started_at <= receipt.claimed_at
                or receipt.completed_at > checked_at
            ):
                raise IntentGoalBindingError("goal_path_not_in_task_scope")

        verify_import_scope()
        if (
            authorization.task_run_id != task_run.task_run_id
            or FILE_TOOL not in authorization.allowed_tools
            or goal.path not in authorization.allowed_paths
        ):
            raise IntentGoalBindingError("goal_path_not_authorized")

        if existing is not None:
            return IntentGoalBindingResult(
                existing.goal, existing.first_commit, "existing"
            )

        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "bind_intent_goal",
                "entity_ids": [goal.intent_id],
                "result": goal.intent_id,
                "import_id": import_id,
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="bind_intent_goal",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(UpsertIntentGoal(goal),),
            event=event,
        )
        deps.repository.validate_candidate(state, mutation)
        verify_import_scope()
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return IntentGoalBindingResult(goal, commit, "created")

    def commit_verified_file_type_fact(
        self,
        intent_id: str,
        candidate: FileTypeFactCandidate,
        proof_id: str,
        fact_id: str,
        mutation_id: str,
    ) -> CommittedOperation:
        """Commit one independently proved file-type Fact without a candidate row."""

        if not isinstance(candidate, FileTypeFactCandidate):
            raise TypeError("candidate must be a FileTypeFactCandidate")
        _validate_mutation_id(proof_id)
        _validate_mutation_id(fact_id)
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, state = self._require_bound_state()
        if self._persistent is None:
            raise InvalidTransition("persistent ControlStore dependencies are not configured")
        execution = next(
            (item for item in state.executions if item.execution_id == candidate.execution_id),
            None,
        )
        plan = next(
            (item for item in state.plans if execution is not None and item.plan_id == execution.plan_id),
            None,
        )
        intent = next((item for item in state.intents if item.intent_id == intent_id), None)
        turn = next(
            (item for item in state.turns if intent is not None and item.turn_id == intent.turn_id),
            None,
        )
        if turn is None:
            raise FileTypeVerificationError("state_conflict")
        now = self._now_utc()
        change = UpsertFact(
            fact_id=fact_id,
            turn_id=turn.turn_id,
            content=fact_content(candidate.observed_type),
            source_ref=candidate.execution_id,
            status=FactStatus.VERIFIED,
            verification_ref=proof_id,
        )
        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "commit_verified_file_type_fact",
                "entity_ids": [fact_id],
                "result": fact_id,
                "intent_id": intent_id,
                "path": candidate.path,
                "observed_type": candidate.observed_type,
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="commit_verified_file_type_fact",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(change,),
            event=event,
        )
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            return CommittedOperation.from_commit(self._persistent.store.commit(mutation))

        deps = self._require_persistent()
        if (
            self._closed
            or self._registry_released
            or task_run.status is not TaskRunStatus.RUNNING
            or now >= task_run.deadline_at
            or intent is None
            or intent.status is not IntentStatus.RUNNING
            or turn is None
            or turn.status is not TurnStatus.RUNNING
            or plan is None
            or plan.intent_id != intent_id
            or plan.status is not PlanStatus.COMPLETED
            or len(plan.steps) != 1
            or execution is None
            or execution.step_index != 0
            or execution.status is not ExecutionStatus.SUCCEEDED
            or any(item.fact_id == fact_id for item in state.facts)
        ):
            raise FileTypeVerificationError("state_conflict")
        binding = deps.store.load_intent_goal(intent_id)
        if binding is None or binding.goal.task_run_id != task_run.task_run_id:
            raise FileTypeVerificationError("state_conflict")
        if candidate.path != binding.goal.path:
            raise FileTypeVerificationError("candidate_rejected")
        if self._file_type_proofs is None:
            raise InvalidTransition("file-type proof evidence is not configured")
        verified = self._file_type_proofs.lookup_file_type_proof(
            candidate.execution_id, proof_id
        )
        if (
            verified.proof.task_run_id != task_run.task_run_id
            or verified.proof.intent_id != intent_id
            or verified.proof.path != candidate.path
            or verified.proof.observed_type != candidate.observed_type
            or verified.execution.target_ref not in {
                item.value for item in task_run.request.target.attachments
            }
        ):
            raise FileTypeVerificationError("evidence_unavailable")
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def commit_verified_t6_observation(
        self,
        intent_id: str,
        proof_id: str,
        fact_id: str,
        mutation_id: str,
        *,
        complete_intent: bool = True,
    ) -> CommittedOperation:
        """Record a checked observation, optionally finishing only its Intent."""
        _validate_mutation_id(proof_id)
        _validate_mutation_id(fact_id)
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, state = self._require_bound_state()
        deps = self._require_persistent()
        operation = (
            "commit_verified_t6_observation" if complete_intent
            else "commit_verified_t6_fact"
        )
        if complete_intent and deps.store.load_d1_run_config(task_run.task_run_id) is not None:
            raise InvalidTransition("D1 commits Fact and Intent completion separately")
        entity_ids = [fact_id, intent_id] if complete_intent else [fact_id]
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            fact = next((f for f in state.facts if f.fact_id == fact_id), None)
            if (
                replay.event.details.get("operation") != operation
                or replay.event.details.get("entity_ids") != entity_ids
                or fact is None or fact.verification_ref != f"t6:{proof_id}"
            ):
                raise IdempotencyConflictError("mutation_id belongs to another T6 observation")
            return CommittedOperation.from_commit(replay)
        if self._t6_observation_proofs is None:
            raise InvalidTransition("T6 observation proof verifier is not configured")
        proof = self._t6_observation_proofs.read_proof(proof_id)
        if proof is None:
            raise ObservationProofError("proof_missing")
        if proof.intent_id != intent_id or proof.task_run_id != task_run.task_run_id:
            raise ObservationProofError("proof_scope_mismatch")
        intent = next((i for i in state.intents if i.intent_id == intent_id), None)
        turn = next((t for t in state.turns if intent is not None and t.turn_id == intent.turn_id), None)
        if intent is None or turn is None:
            raise ObservationProofError("state_conflict")
        now = self._now_utc()
        fact_change = UpsertFact(
            fact_id, intent.turn_id,
            observation_fact_content(proof_id, len(proof.execution_ids)),
            proof.source_execution_id, FactStatus.VERIFIED, f"t6:{proof_id}",
        )
        changes = (fact_change,) + ((
            UpsertIntent(
                intent_id, intent.turn_id,
                intent.description,
                intent.priority,
                intent.intent_type,
                IntentStatus.SUCCEEDED,
            ),
        ) if complete_intent else ())
        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": operation,
                "entity_ids": entity_ids,
                "result": fact_id,
                "intent_id": intent_id,
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation=operation,
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=changes,
            event=event,
        )
        if (
            self._closed or self._registry_released
            or task_run.status is not TaskRunStatus.RUNNING
            or now >= task_run.deadline_at
            or intent.status is not IntentStatus.RUNNING
            or turn.status is not TurnStatus.RUNNING
            or any(f.fact_id == fact_id for f in state.facts)
        ):
            raise ObservationProofError("state_conflict")
        verified = self._t6_observation_proofs.lookup(proof.source_execution_id, proof_id)
        if verified.task_run_id != task_run.task_run_id:
            raise ObservationProofError("proof_task_mismatch")
        binding = deps.store.load_d1_turn_binding(intent.turn_id)
        if binding is not None:
            candidate_fact = FactRecordSnapshot(
                fact_change.fact_id, fact_change.turn_id, fact_change.content,
                fact_change.source_ref, fact_change.status,
                state.latest_checkpoint.sequence + 1, 0, fact_change.verification_ref,
            )
            self._evaluate_d1_evidence(binding, replace(state, facts=state.facts + (candidate_fact,)), (fact_id,))
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def complete_satisfied_intent(
        self,
        intent_id: str,
        evaluation: IntentGoalEvaluation,
        mutation_id: str,
    ) -> CommittedOperation:
        """Commit only the Intent transition after a fresh read-only evaluation."""

        if not isinstance(evaluation, IntentGoalEvaluation):
            raise TypeError("evaluation must be an IntentGoalEvaluation")
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, state = self._require_bound_state()
        if (
            evaluation.task_run_id != task_run.task_run_id
            or evaluation.intent_id != intent_id
            or evaluation.outcome != "satisfied"
            or not evaluation.verified_fact_ids
        ):
            raise IntentGoalEvaluationError("state_conflict")
        deps = self._require_persistent()
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            details = replay.event.details
            if (
                details.get("operation") != "complete_satisfied_intent"
                or details.get("entity_ids") != [intent_id]
                or details.get("verified_fact_ids") != list(evaluation.verified_fact_ids)
            ):
                raise IdempotencyConflictError("mutation_id was already committed for a different Intent completion")
            return CommittedOperation.from_commit(replay)

        now = self._now_utc()
        intent = next((item for item in state.intents if item.intent_id == intent_id), None)
        turn = next(
            (item for item in state.turns if intent is not None and item.turn_id == intent.turn_id),
            None,
        )
        if (
            self._closed
            or self._registry_released
            or task_run.status is not TaskRunStatus.RUNNING
            or now >= task_run.deadline_at
            or intent is None
            or intent.status is not IntentStatus.RUNNING
            or turn is None
            or turn.status is not TurnStatus.RUNNING
            or self._intent_goal_evaluator is None
        ):
            raise IntentGoalEvaluationError("state_conflict")
        fresh = self._intent_goal_evaluator.evaluate(task_run.task_run_id, intent_id)
        if fresh != evaluation:
            raise IntentGoalEvaluationError("state_conflict")

        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "complete_satisfied_intent",
                "entity_ids": [intent_id],
                "result": "succeeded",
                "verified_fact_ids": list(evaluation.verified_fact_ids),
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="complete_satisfied_intent",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(
                UpsertIntent(
                    intent.intent_id,
                    intent.turn_id,
                    intent.description,
                    intent.priority,
                    intent.intent_type,
                    IntentStatus.SUCCEEDED,
                ),
            ),
            event=event,
        )
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def complete_verified_t6_intent(
        self,
        intent_id: str,
        fact_ids: tuple[str, ...],
        mutation_id: str,
    ) -> CommittedOperation:
        """Complete an Intent from its last proved short Plan, not from a claim."""

        mutation_id = _validate_mutation_id(mutation_id)
        if (
            not isinstance(fact_ids, tuple) or not fact_ids
            or any(not isinstance(value, str) or not value for value in fact_ids)
            or len(set(fact_ids)) != len(fact_ids)
        ):
            raise ObservationProofError("invalid_supporting_facts")
        task_run, state = self._require_bound_state()
        deps = self._require_persistent()
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            if (
                replay.event.details.get("operation") != "complete_verified_t6_intent"
                or replay.event.details.get("entity_ids") != [intent_id]
                or replay.event.details.get("verified_fact_ids") != list(fact_ids)
            ):
                raise IdempotencyConflictError("mutation_id belongs to another Intent completion")
            return CommittedOperation.from_commit(replay)

        now = self._now_utc()
        intent = next((item for item in state.intents if item.intent_id == intent_id), None)
        turn = next((item for item in state.turns if intent is not None and item.turn_id == intent.turn_id), None)
        plans = [item for item in state.plans if item.intent_id == intent_id]
        if (
            self._closed or self._registry_released
            or task_run.status is not TaskRunStatus.RUNNING
            or now >= task_run.deadline_at
            or intent is None or intent.status is not IntentStatus.RUNNING
            or turn is None or turn.status is not TurnStatus.RUNNING
            or not plans or plans[-1].status is not PlanStatus.COMPLETED
            or self._t6_observation_proofs is None
        ):
            raise ObservationProofError("state_conflict")

        latest_verifiable_id = self._t6_observation_proofs.latest_verifiable_plan(
            state, intent_id,
        )
        latest_supported = False
        for fact_id in fact_ids:
            fact = next((item for item in state.facts if item.fact_id == fact_id), None)
            if (
                fact is None or fact.status is not FactStatus.VERIFIED
                or fact.turn_id != intent.turn_id
                or fact.verification_ref is None
                or not fact.verification_ref.startswith("t6:")
            ):
                raise ObservationProofError("supporting_fact_unverified")
            proof_id = fact.verification_ref.removeprefix("t6:")
            proof = self._t6_observation_proofs.read_proof(proof_id)
            if (
                proof is None or proof.task_run_id != task_run.task_run_id
                or proof.intent_id != intent_id
                or proof.source_execution_id != fact.source_ref
            ):
                raise ObservationProofError("supporting_fact_scope_mismatch")
            self._t6_observation_proofs.lookup(fact.source_ref, proof_id)
            latest_supported |= proof.plan_id == latest_verifiable_id
        if not latest_supported:
            raise ObservationProofError("latest_plan_has_no_verified_fact")
        binding = deps.store.load_d1_turn_binding(intent.turn_id)
        if binding is not None:
            self._evaluate_d1_evidence(binding, state, fact_ids)

        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "complete_verified_t6_intent",
                "entity_ids": [intent_id],
                "result": "succeeded",
                "verified_fact_ids": list(fact_ids),
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="complete_verified_t6_intent",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(UpsertIntent(
                intent.intent_id, intent.turn_id, intent.description,
                intent.priority, intent.intent_type, IntentStatus.SUCCEEDED,
            ),),
            event=event,
        )
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def _evaluate_d1_evidence(
        self, binding: D1TurnBinding, state: ControlStateSnapshot,
        fact_ids: tuple[str, ...],
    ):
        if binding.task_run_id != state.task_run.task_run_id or self._t6_observation_proofs is None:
            raise ObservationProofError("d1_evidence_scope_mismatch")
        for fact_id in fact_ids:
            fact = next((item for item in state.facts if item.fact_id == fact_id), None)
            execution = next((item for item in state.executions if fact is not None and item.execution_id == fact.source_ref), None)
            plan = next((item for item in state.plans if execution is not None and item.plan_id == execution.plan_id), None)
            if fact is None or fact.status is not FactStatus.VERIFIED or fact.turn_id != binding.turn_id or fact.verification_ref is None or not fact.verification_ref.startswith("t6:") or plan is None or plan.intent_id != binding.intent_id:
                raise ObservationProofError("d1_supporting_fact_scope_mismatch")
            self._t6_observation_proofs.lookup(fact.source_ref, fact.verification_ref.removeprefix("t6:"))
        evaluator = self._d1_turn_evaluator
        if evaluator is None:
            raise ObservationProofError("d1_evaluator_not_configured")
        evaluation = evaluator.evaluate(binding, state, fact_ids)
        if not evaluation.satisfied or evaluation.supporting_fact_ids != fact_ids or evaluation.proof_ref is None:
            raise ObservationProofError("d1_fixed_rule_not_satisfied")
        if self.control_state.latest_checkpoint.sequence != state.latest_checkpoint.sequence or self.status() is not TaskRunStatus.RUNNING or self._now_utc() >= state.task_run.deadline_at:
            raise ObservationProofError("state_conflict")
        return evaluation

    def complete_verified_d1_turn(
        self, turn_id: str, fact_ids: tuple[str, ...], mutation_id: str,
    ) -> CommittedOperation:
        """Independently recheck saved D1 evidence and atomically save Turn proof."""
        mutation_id = _validate_mutation_id(mutation_id)
        if not isinstance(fact_ids, tuple) or not fact_ids or len(set(fact_ids)) != len(fact_ids):
            raise ObservationProofError("invalid_supporting_facts")
        task, state = self._require_bound_state()
        deps = self._require_persistent()
        binding = deps.store.load_d1_turn_binding(turn_id)
        if binding is None or binding.task_run_id != task.task_run_id:
            raise ObservationProofError("d1_binding_missing_or_mismatched")
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            proof = deps.store.load_d1_turn_proof(turn_id)
            if replay.event.details.get("operation") != "complete_verified_d1_turn" or proof is None or proof.supporting_fact_ids != fact_ids or replay.event.details.get("entity_ids") != [turn_id]:
                raise IdempotencyConflictError("mutation_id belongs to another D1 Turn completion")
            return CommittedOperation.from_commit(replay)
        turn = next((item for item in state.turns if item.turn_id == turn_id), None)
        intent = next((item for item in state.intents if item.intent_id == binding.intent_id), None)
        now = self._now_utc()
        if self._closed or self._registry_released or task.status is not TaskRunStatus.RUNNING or now >= task.deadline_at or turn is None or turn.status is not TurnStatus.RUNNING or intent is None or intent.status is not IntentStatus.SUCCEEDED or turn.intent_ids != (intent.intent_id,):
            raise ObservationProofError("state_conflict")
        evaluation = self._evaluate_d1_evidence(binding, state, fact_ids)
        now = self._now_utc()
        if now >= task.deadline_at:
            raise ObservationProofError("state_conflict")
        proof = D1TurnProof(turn_id, binding.binding_id, evaluation.proof_ref, fact_ids)
        event = RuntimeEvent(
            type="control_state_committed", task_run_id=task.task_run_id,
            timestamp=now, status=TaskRunStatus.RUNNING,
            details={"operation": "complete_verified_d1_turn", "entity_ids": [turn_id],
                     "result": "completed", "verified_fact_ids": list(fact_ids), "proof_ref": proof.proof_ref},
        )
        mutation = ControlMutation.create(
            task_run_id=task.task_run_id, mutation_id=mutation_id,
            operation="complete_verified_d1_turn", request=task.request,
            effective_model_backend=task.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(UpsertTurn(turn.turn_id, turn.task_run_id, turn.sequence, turn.turn_target, turn.intent_ids, TurnStatus.COMPLETED),),
            event=event, d1_turn_proof=proof,
        )
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def apply_d2_review(
        self,
        application: AppendD2ReviewApplication,
        mutation_id: str,
        *,
        evidence_validator: Callable[[AppendD2ReviewApplication], None] | None = None,
    ) -> CommittedOperation:
        """Atomically apply one trusted, already-validated D2 review result."""
        if not isinstance(application, AppendD2ReviewApplication):
            raise TypeError("application must be AppendD2ReviewApplication")
        mutation_id = _validate_mutation_id(mutation_id)
        task, state = self._require_bound_state()
        deps = self._require_persistent()
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            details = replay.event.details
            if (
                details.get("operation") != "apply_d2_review"
                or details.get("entity_ids") != [application.review_id]
                or any(
                    details.get(name) != value
                    for name, value in {
                        "review_root_id": application.review_root_id,
                        "review_generation": application.review_generation,
                        "review_id": application.review_id,
                        "application_ref": application.application_ref,
                        "context_version": application.context_version,
                        "constraint_version": application.constraint_version,
                        "paused_gate": application.paused_gate,
                        "turn_id": application.turn_id,
                        "intent_id": application.intent_id,
                        "review_scope": application.review_scope,
                        "action_candidate_id": application.action_candidate_id,
                        "pause_reason": application.pause_reason,
                    }.items()
                )
            ):
                raise IdempotencyConflictError("mutation_id belongs to another D2 review")
            if deps.store.load_d2_review_application(task.task_run_id, application.review_id) != application:
                raise IdempotencyConflictError("D2 replay application content differs")
            return CommittedOperation.from_commit(replay)
        now = self._now_utc()
        if (
            self._closed or self._registry_released
            or task.status is not TaskRunStatus.RUNNING
            or now >= task.deadline_at
        ):
            raise InvalidTransition("D2 review application requires a running TaskRun")
        if deps.store.load_d2_run_config(task.task_run_id) is None:
            raise InvalidTransition("review application requires an explicit D2 run")
        turn = next((item for item in state.turns if item.turn_id == application.turn_id), None)
        intent = next((item for item in state.intents if item.intent_id == application.intent_id), None)
        if turn is None or intent is None or intent.turn_id != turn.turn_id:
            raise InvalidTransition("D2 review scope differs from registered objects")
        if not application.paused_gate:
            if evidence_validator is None:
                raise InvalidTransition("D2 review requires a saved known-evidence validator")
            evidence_validator(application)
        if state.latest_checkpoint.sequence != self.control_state.latest_checkpoint.sequence:
            raise StaleControlStateError("D2 review state changed during evidence validation")
        if self._now_utc() >= task.deadline_at:
            raise InvalidTransition("D2 review Deadline elapsed during evidence validation")
        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task.task_run_id,
            timestamp=now,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "apply_d2_review",
                "entity_ids": [application.review_id],
                "result": "paused" if application.paused_gate else "applied",
                "review_root_id": application.review_root_id,
                "review_generation": application.review_generation,
                "review_id": application.review_id,
                "application_ref": application.application_ref,
                "context_version": application.context_version,
                "constraint_version": application.constraint_version,
                "paused_gate": application.paused_gate,
                "turn_id": application.turn_id,
                "intent_id": application.intent_id,
                "review_scope": application.review_scope,
                "action_candidate_id": application.action_candidate_id,
                "pause_reason": application.pause_reason,
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task.task_run_id,
            mutation_id=mutation_id,
            operation="apply_d2_review",
            request=task.request,
            effective_model_backend=task.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(application,),
            event=event,
        )
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        return CommittedOperation.from_commit(commit)

    def mark_candidate_found(
        self,
        mutation_id: str | None = None,
    ) -> Any:
        """Move a persistent TaskRun into its external confirmation period."""

        if self._persistent is None:
            if mutation_id is not None:
                raise InvalidTransition(
                    "mutation_id is only supported with persistent dependencies"
                )
            return super().mark_candidate_found()

        mutation_id = _validate_mutation_id(mutation_id)
        task_run, state = self._require_bound_state()
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            if replay.event.details.get("operation") == "request_completion_confirmation":
                return CommittedOperation.from_commit(replay)
            self._assert_deadline_preempted_replay(replay)
            return CommittedOperation.from_commit(replay)
        deps = self._require_persistent()
        if task_run.status is not TaskRunStatus.RUNNING:
            raise InvalidTransition(
                "a completion candidate can only be reported while running"
            )
        now = self._now_utc()
        if now >= task_run.deadline_at:
            # A caller may arrive exactly at the deadline without having first
            # advanced time.  The hard deadline still wins for this mutation.
            return self._finish_persistent(
                TaskRunStatus.DEADLINE_EXCEEDED,
                mutation_id=mutation_id,
                now=now,
                failure_reason=task_run.failure_reason,
            )
        confirmation_deadline = now + 180
        change = UpsertTaskRun(
            task_run_id=task_run.task_run_id,
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            started_at=task_run.started_at,
            deadline_at=task_run.deadline_at,
            status=TaskRunStatus.AWAITING_CONFIRMATION,
            sandbox_id=task_run.sandbox_id,
            confirmation_deadline=confirmation_deadline,
            finished_at=None,
            failure_reason=task_run.failure_reason,
            source_root=task_run.source_root,
            target_network=task_run.target_network,
        )
        event = RuntimeEvent(
            type="completion_confirmation_requested",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.AWAITING_CONFIRMATION,
            details={
                "operation": "request_completion_confirmation",
                "entity_ids": [task_run.task_run_id],
                "result": "awaiting_confirmation",
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="request_completion_confirmation",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(change,),
            event=event,
        )
        commit = deps.store.commit(mutation)
        self._reload_after_commit()
        return CommittedOperation.from_commit(commit)

    def advance_time(self, mutation_id: str) -> CommittedOperation | None:
        """Persist deadline/confirmation transitions; return None if unchanged."""

        if self._persistent is None:
            # Legacy controllers retain their historical poll behavior.
            _validate_mutation_id(mutation_id)
            self.poll()
            return None

        mutation_id = _validate_mutation_id(mutation_id)
        task_run, state = self._require_bound_state()
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            self._assert_finish_replay(
                replay,
                {TaskRunStatus.DEADLINE_EXCEEDED.value, TaskRunStatus.ASSUMED_ACHIEVED.value},
            )
            return CommittedOperation.from_commit(replay)
        self._require_persistent()
        if task_run.status in TERMINAL_STATUSES:
            return None

        now = self._now_utc()
        # Deadline is the hard boundary and always wins over confirmation grace.
        if now >= task_run.deadline_at:
            return self._finish_persistent(
                TaskRunStatus.DEADLINE_EXCEEDED,
                mutation_id=mutation_id,
                now=now,
                failure_reason=task_run.failure_reason,
            )
        if (
            task_run.status is TaskRunStatus.AWAITING_CONFIRMATION
            and task_run.confirmation_deadline is not None
            and now >= task_run.confirmation_deadline
        ):
            return self._finish_persistent(
                TaskRunStatus.ASSUMED_ACHIEVED,
                mutation_id=mutation_id,
                now=now,
                failure_reason=task_run.failure_reason,
            )
        return None

    def handle_control_message(
        self,
        message: Any,
        mutation_id: str | None = None,
    ) -> Any:
        """Handle the existing external completion confirmation message."""

        if self._persistent is None:
            if mutation_id is not None:
                raise InvalidTransition(
                    "mutation_id is only supported with persistent dependencies"
                )
            return super().handle_control_message(message)

        mutation_id = _validate_mutation_id(mutation_id)
        if not isinstance(message, dict) and not hasattr(message, "get"):
            raise InvalidTransition("control message must be an object")
        if message.get("type") != "completion":
            raise InvalidTransition("unsupported control message type")
        task_run, _state = self._require_bound_state()
        if message.get("task_run_id") != task_run.task_run_id:
            raise InvalidTransition("control message task_run_id does not match")
        if message.get("status") != "achieved":
            raise InvalidTransition("completion message status must be achieved")
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            self._assert_finish_replay(
                replay,
                {TaskRunStatus.ACHIEVED.value, TaskRunStatus.DEADLINE_EXCEEDED.value},
            )
            return CommittedOperation.from_commit(replay)
        self._require_persistent()
        if task_run.status is not TaskRunStatus.AWAITING_CONFIRMATION:
            raise InvalidTransition(
                "completion can only be confirmed during the confirmation period"
            )

        # Re-use the supplied mutation ID for a due-time transition.  This
        # ensures a confirmation arriving at the boundary observes Deadline
        # precedence and remains idempotent.
        due = self.advance_time(mutation_id)
        if due is not None:
            return due
        return self._finish_persistent(
            TaskRunStatus.ACHIEVED,
            mutation_id=mutation_id,
            now=self._now_utc(),
            failure_reason=task_run.failure_reason,
        )

    def model_failed(
        self,
        reason: str | None = None,
        mutation_id: str | None = None,
    ) -> Any:
        if self._persistent is None:
            if mutation_id is not None:
                raise InvalidTransition(
                    "mutation_id is only supported with persistent dependencies"
                )
            return super().model_failed(reason)
        return self._finish_public(
            TaskRunStatus.FAILED,
            mutation_id=mutation_id,
            failure_reason=reason,
        )

    def cancel(self, mutation_id: str | None = None) -> Any:
        if self._persistent is None:
            if mutation_id is not None:
                raise InvalidTransition(
                    "mutation_id is only supported with persistent dependencies"
                )
            return super().cancel()
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, _state = self._require_bound_state()
        # A caller may send another cancel with a fresh mutation ID. It reads
        # the first terminal result without registering that ID or touching Docker.
        existing = self._load_existing_commit(mutation_id)
        if existing is None and (task_run.status in TERMINAL_STATUSES or self._cache_invalid):
            commit = self._persistent.store.load_terminal_commit(task_run.task_run_id)
            if commit is not None:
                return CommittedOperation.from_commit(commit)
            if task_run.status in TERMINAL_STATUSES:
                raise StorageError("terminal TaskRun has no committed result")
        return self._finish_public(
            TaskRunStatus.CANCELLED,
            mutation_id=mutation_id,
            failure_reason=None,
        )

    def conclude(
        self,
        status: TaskRunStatus,
        mutation_id: str | None = None,
        *,
        reason: str | None = None,
    ) -> Any:
        if self._persistent is None:
            if reason is not None:
                raise InvalidTransition("slice conclusion requires persistence")
            if mutation_id is not None:
                raise InvalidTransition(
                    "mutation_id is only supported with persistent dependencies"
                )
            return super().conclude(status)
        if status not in {TaskRunStatus.PARTIAL, TaskRunStatus.INCONCLUSIVE}:
            raise InvalidTransition("conclude only accepts partial or inconclusive")
        if reason is not None:
            if reason != "single_intent_slice_finished" or status is not TaskRunStatus.INCONCLUSIVE:
                raise InvalidTransition("invalid slice conclusion")
            if any(e.status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}
                   for e in self.control_state.executions):
                raise InvalidTransition("slice has unresolved executions")
        return self._finish_public(
            status,
            mutation_id=mutation_id,
            failure_reason=reason,
        )

    def _require_bound_state(self) -> tuple[Any, ControlStateSnapshot]:
        if (
            self._persistent_task_run_id is None
            or self._persistent_snapshot is None
            or self._persistent_state is None
        ):
            raise InvalidTransition("TaskRunController has no attached TaskRun")
        return self._persistent_snapshot, self._persistent_state

    def _load_existing_commit(self, mutation_id: str) -> Any:
        assert self._persistent is not None
        loader = getattr(self._persistent.store, "load_committed_mutation", None)
        if loader is None:
            return None
        return loader(self._persistent_task_run_id, mutation_id)

    @staticmethod
    def _assert_replay_operation(commit: Any, operation: str) -> None:
        if commit.event.details.get("operation") != operation:
            raise IdempotencyConflictError(
                "mutation_id was already committed for another operation"
            )

    @classmethod
    def _assert_finish_replay(cls, commit: Any, allowed_results: set[str]) -> None:
        cls._assert_replay_operation(commit, "finish_task_run")
        result = commit.event.details.get("result")
        if result not in allowed_results:
            raise IdempotencyConflictError(
                "mutation_id was already committed for a different terminal result"
            )

    @classmethod
    def _assert_deadline_preempted_replay(cls, commit: Any) -> None:
        cls._assert_finish_replay(commit, {TaskRunStatus.DEADLINE_EXCEEDED.value})

    def _finish_public(
        self,
        status: TaskRunStatus,
        *,
        mutation_id: str | None,
        failure_reason: str | None,
    ) -> CommittedOperation:
        mutation_id = _validate_mutation_id(mutation_id)
        task_run, _state = self._require_bound_state()
        replay = self._load_existing_commit(mutation_id)
        if replay is not None:
            self._assert_finish_replay(
                replay, {status.value, TaskRunStatus.DEADLINE_EXCEEDED.value}
            )
            if (replay.event.status is not TaskRunStatus.DEADLINE_EXCEEDED
                    and (failure_reason == "single_intent_slice_finished"
                         or replay.event.details.get("reason") == "single_intent_slice_finished")
                    and replay.event.details.get("reason") != failure_reason):
                raise IdempotencyConflictError("terminal reason differs from original commit")
            return CommittedOperation.from_commit(replay)
        self._require_persistent()
        if task_run.status in TERMINAL_STATUSES:
            raise InvalidTransition("cannot change a terminal TaskRun")
        now = self._now_utc()
        if now >= task_run.deadline_at:
            return self._finish_persistent(
                TaskRunStatus.DEADLINE_EXCEEDED,
                mutation_id=mutation_id,
                now=now,
                failure_reason=task_run.failure_reason,
            )
        return self._finish_persistent(
            status,
            mutation_id=mutation_id,
            now=now,
            failure_reason=failure_reason,
        )

    def _finish_persistent(
        self,
        status: TaskRunStatus,
        *,
        mutation_id: str,
        now: float,
        failure_reason: str | None,
    ) -> CommittedOperation:
        deps = self._require_persistent()
        task_run, state = self._require_bound_state()
        if task_run.status in TERMINAL_STATUSES:
            replay = self._load_existing_commit(mutation_id)
            if replay is not None:
                self._assert_replay_operation(replay, "finish_task_run")
                return CommittedOperation.from_commit(replay)
            raise InvalidTransition("cannot change a terminal TaskRun")
        change = UpsertTaskRun(
            task_run_id=task_run.task_run_id,
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            started_at=task_run.started_at,
            deadline_at=task_run.deadline_at,
            status=status,
            sandbox_id=task_run.sandbox_id,
            confirmation_deadline=task_run.confirmation_deadline,
            finished_at=now,
            failure_reason=failure_reason,
            source_root=task_run.source_root,
            target_network=task_run.target_network,
        )
        event = RuntimeEvent(
            type="task_run_finished",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=status,
            details={
                "operation": "finish_task_run",
                "entity_ids": [task_run.task_run_id],
                "result": status.value,
                **({"reason": failure_reason} if failure_reason == "single_intent_slice_finished" else {}),
            },
        )
        mutation = ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="finish_task_run",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
            state_changes=(change,),
            event=event,
        )
        deps.repository.validate_candidate(state, mutation)
        commit = deps.store.commit(mutation)
        self._reload_after_commit(commit)
        self._cleanup_after_terminal()
        return CommittedOperation(value=status.value, commit=commit)

    def reload_state(self) -> ControlStateSnapshot:
        """Explicitly revalidate committed state and restore this owner's cache.

        This is a read/revalidation path, not a mutation or Sandbox recovery:
        it never creates, binds, interrupts, destroys, or replays a resource.
        Keep the old cache inaccessible until the entire snapshot is verified.
        """

        # Deliberately do not call _require_persistent: recovering an invalid
        # cache is the purpose of this entry point.
        deps = self._persistent
        if deps is None:
            raise InvalidTransition("persistent ControlStore dependencies are not configured")
        task_run_id = self._persistent_task_run_id
        if task_run_id is None or self._closed or self._registry_released:
            raise InvalidTransition("reload_state requires an owned, open Controller")
        if deps.registry.owner(task_run_id) is not self:
            raise ControllerNotClaimedError("controller does not own TaskRun binding")
        try:
            # The repository validates the full snapshot and external evidence.
            # Its embedded TaskRun avoids combining two separate snapshot reads.
            state = deps.repository.load_control_state(task_run_id)
            if state is None:
                raise TaskRunNotFoundError(f"TaskRun does not exist: {task_run_id}")
            if (
                state.task_run.task_run_id != task_run_id
                or state.latest_checkpoint.task_run_id != task_run_id
            ):
                raise StorageError("reloaded control state belongs to another TaskRun")
            if self._persistent_snapshot is not None and (
                state.task_run.sandbox_id != self._persistent_snapshot.sandbox_id
            ):
                raise StorageError("reloaded control state changed the bound Sandbox ID")
            if self._persistent_snapshot is not None and (
                state.task_run.target_network != self._persistent_snapshot.target_network
            ):
                raise StorageError("reloaded control state changed the target network")
            if self._persistent_state is not None and (
                state.latest_checkpoint.sequence
                < self._persistent_state.latest_checkpoint.sequence
            ):
                raise StorageError("reloaded control state has a stale checkpoint")
        except Exception as exc:
            self._cache_invalid = True
            if isinstance(exc, StorageError):
                raise
            raise StorageError("control state revalidation failed during reload_state") from exc
        self._persistent_snapshot = state.task_run
        self._persistent_state = state
        self._cache_invalid = False
        return state

    def cleanup_sandbox(self) -> None:
        """Explicitly retry cleanup of a durably terminal TaskRun; never commit."""

        self._require_persistent()
        task_run, _state = self._require_bound_state()
        if task_run.status not in TERMINAL_STATUSES:
            raise InvalidTransition("cleanup requires a terminal TaskRun")
        self._cleanup_after_terminal()

    def _cleanup_after_terminal(self) -> None:
        """Clean a persisted terminal TaskRun after its commit is durable."""

        task_run, state = self._require_bound_state()
        if task_run.sandbox_id is not None and not self._sandbox_destroyed:
            if isinstance(self._sandbox, TaskRunSandboxPort):
                self._sandbox.quiesce(task_run.sandbox_id)
            active_execution = any(
                item.status is ExecutionStatus.RUNNING for item in state.executions
            )
            should_interrupt = (
                task_run.status
                in {TaskRunStatus.DEADLINE_EXCEEDED, TaskRunStatus.CANCELLED}
                or active_execution
            )
            interrupt_error: BaseException | None = None
            destroy_error: BaseException | None = None
            if should_interrupt:
                try:
                    self._sandbox.interrupt(task_run.sandbox_id)
                except BaseException as exc:  # cleanup must still attempt destroy
                    interrupt_error = exc
            try:
                self._sandbox.destroy(task_run.sandbox_id)
                self._sandbox_destroyed = True
            except BaseException as exc:
                destroy_error = exc
            if interrupt_error is not None or destroy_error is not None:
                reason = (
                    "sandbox interrupt failed"
                    if interrupt_error is not None and destroy_error is None
                    else "sandbox destroy failed"
                )
                raise SandboxCleanupError(
                    task_run.sandbox_id,
                    reason,
                    original_error=interrupt_error,
                    cleanup_error=destroy_error,
                ) from (destroy_error or interrupt_error)
        if task_run.target_network is not None and not self._target_network_destroyed:
            if self._target_network is None:
                raise SandboxCleanupError(
                    task_run.sandbox_id,
                    "target network adapter is missing",
                )
            try:
                self._target_network.destroy(task_run.task_run_id, task_run.target_network)
                self._target_network_destroyed = True
            except BaseException as exc:
                raise SandboxCleanupError(
                    task_run.sandbox_id,
                    "target network destroy failed",
                    cleanup_error=exc,
                ) from exc

    def recover_task_run(self, mutation_id: str) -> RecoveryResult:
        """Persist the confirmed crash-recovery transition for one attached task."""

        deps = self._require_persistent()
        _validate_mutation_id(mutation_id)
        if (
            self._persistent_task_run_id is None
            or self._persistent_snapshot is None
            or self._persistent_state is None
        ):
            raise InvalidTransition("recover_task_run requires an attached TaskRun")

        state = self._persistent_state
        existing = self._load_existing_commit(mutation_id)
        if existing is not None:
            operation = existing.event.details.get("operation")
            if operation != "recover_running_execution_as_inconclusive":
                raise IdempotencyConflictError(
                    "mutation_id was already committed for another operation"
                )
            refreshed = self._reload_after_commit()
            return RecoveryResult(state=refreshed, commit=existing)
        if state.task_run.status in TERMINAL_STATUSES:
            return RecoveryResult(state=state, commit=None)

        running_executions = tuple(
            item
            for item in state.executions
            if item.status is ExecutionStatus.RUNNING
        )
        if not running_executions:
            return RecoveryResult(state=state, commit=None)
        if len(running_executions) != 1:
            raise StorageError("control state contains multiple running Executions")

        mutation = self._build_recovery_mutation(
            state,
            mutation_id=mutation_id,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
        )
        commit = deps.store.commit(mutation)
        refreshed = self._reload_after_commit()
        if isinstance(self._sandbox, TaskRunSandboxPort):
            self._sandbox.quiesce(refreshed.task_run.sandbox_id)
        return RecoveryResult(state=refreshed, commit=commit)

    def _build_recovery_mutation(
        self,
        state: ControlStateSnapshot,
        *,
        mutation_id: str,
        base_checkpoint_sequence: int,
    ) -> ControlMutation:
        running_executions = tuple(
            item
            for item in state.executions
            if item.status is ExecutionStatus.RUNNING
        )
        if len(running_executions) != 1:
            raise StorageError(
                "crash recovery requires exactly one running Execution"
            )
        execution = running_executions[0]
        plans = {item.plan_id: item for item in state.plans}
        intents = {item.intent_id: item for item in state.intents}
        turns = {item.turn_id: item for item in state.turns}
        plan = plans.get(execution.plan_id)
        if plan is None:
            raise StorageError("running Execution references an unknown Plan")
        intent = intents.get(plan.intent_id)
        if intent is None:
            raise StorageError("running Execution Plan references an unknown Intent")
        turn = turns.get(intent.turn_id)
        if turn is None:
            raise StorageError("running Execution Intent references an unknown Turn")
        if plan.status in {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.INCONCLUSIVE,
        }:
            raise StorageError("running Execution is attached to a terminal Plan")
        if intent.status in {
            IntentStatus.SUCCEEDED,
            IntentStatus.FAILED,
            IntentStatus.INCONCLUSIVE,
        }:
            raise StorageError("running Execution is attached to a terminal Intent")
        if turn.status in {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.INCONCLUSIVE,
        }:
            raise StorageError("running Execution is attached to a terminal Turn")

        task_run = state.task_run
        now = self._now_utc()
        task_change = UpsertTaskRun(
            task_run_id=task_run.task_run_id,
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            started_at=task_run.started_at,
            deadline_at=task_run.deadline_at,
            status=TaskRunStatus.INCONCLUSIVE,
            sandbox_id=task_run.sandbox_id,
            confirmation_deadline=task_run.confirmation_deadline,
            finished_at=now,
            failure_reason="unfinished_execution_detected_during_recovery",
            source_root=task_run.source_root,
            target_network=task_run.target_network,
        )
        plan_change = UpsertPlan(
            plan_id=plan.plan_id,
            intent_id=plan.intent_id,
            steps=plan.steps,
            status=PlanStatus.INCONCLUSIVE,
        )
        intent_change = UpsertIntent(
            intent_id=intent.intent_id,
            turn_id=intent.turn_id,
            description=intent.description,
            priority=intent.priority,
            intent_type=intent.intent_type,
            status=IntentStatus.INCONCLUSIVE,
        )
        execution_change = UpsertExecution(
            execution_id=execution.execution_id,
            plan_id=execution.plan_id,
            step_index=execution.step_index,
            command_ref=execution.command_ref,
            status=ExecutionStatus.RUNNING,
            output_ref=None,
        )
        turn_change = UpsertTurn(
            turn_id=turn.turn_id,
            task_run_id=turn.task_run_id,
            sequence=turn.sequence,
            turn_target=turn.turn_target,
            intent_ids=turn.intent_ids,
            status=TurnStatus.INCONCLUSIVE,
        )
        event = RuntimeEvent(
            type="control_state_committed",
            task_run_id=task_run.task_run_id,
            timestamp=now,
            status=TaskRunStatus.INCONCLUSIVE,
            details={
                "operation": "recover_running_execution_as_inconclusive",
                "entity_ids": [
                    task_run.task_run_id,
                    execution.execution_id,
                    plan.plan_id,
                    intent.intent_id,
                    turn.turn_id,
                ],
                "result": "inconclusive",
            },
        )
        return ControlMutation.create(
            task_run_id=task_run.task_run_id,
            mutation_id=mutation_id,
            operation="recover_running_execution_as_inconclusive",
            request=task_run.request,
            effective_model_backend=task_run.effective_model_backend,
            base_checkpoint_sequence=base_checkpoint_sequence,
            state_changes=(
                task_change,
                execution_change,
                plan_change,
                intent_change,
                turn_change,
            ),
            event=event,
        )

    def _reload_after_commit(self, commit: ControlCommitResult | None = None) -> ControlStateSnapshot:
        assert self._persistent is not None
        assert self._persistent_task_run_id is not None
        try:
            snapshot = self._persistent.repository.load_task_run(
                self._persistent_task_run_id
            )
            state = self._persistent.repository.load_control_state(
                self._persistent_task_run_id
            )
            if snapshot is None or state is None:
                raise StorageError("committed TaskRun could not be reloaded")
        except Exception as exc:
            self._cache_invalid = True
            if isinstance(exc, StorageError):
                raise CommittedStateReloadError(
                    f"mutation committed; control state reload unavailable: {exc}", commit
                ) from exc
            raise CommittedStateReloadError(
                f"mutation committed; control state reload failed: {exc}", commit
            ) from exc
        self._persistent_snapshot = snapshot
        self._persistent_state = state
        return state

    def close(self) -> None:
        deps = self._require_persistent()
        if self._persistent_task_run_id is None or self._persistent_snapshot is None:
            raise InvalidTransition("TaskRunController has no bound TaskRun")
        if self._persistent_snapshot.status not in TERMINAL_STATUSES:
            raise InvalidTransition("close is only allowed after TaskRun reaches a terminal state")
        if not self._registry_released:
            deps.registry.release(self._persistent_task_run_id, self)
            self._registry_released = True
        self._closed = True

    @property
    def control_state(self) -> ControlStateSnapshot:
        self._require_persistent()
        if self._persistent_state is None:
            raise InvalidTransition("persistent TaskRun has not been started or attached")
        return self._persistent_state


def _clock_now_wrapper(clock: Any):
    def wrapped() -> float:
        return _clock_now(clock)

    return wrapped


def deps_sandbox_create(sandbox: Any, task_run_id: str) -> str:
    sandbox_id = sandbox.create(task_run_id)
    if not isinstance(sandbox_id, str) or not sandbox_id.strip():
        raise StorageError("Sandbox.create must return a non-empty sandbox_id")
    return sandbox_id
