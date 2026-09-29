from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4
import sqlite3

import pytest

from agent_core import (
    AppendPlanValidationRecord,
    Checkpoint,
    ControlMutation,
    ControlStateSnapshot,
    ControllerAlreadyClaimedError,
    ControlStateRepository,
    DomainError,
    DomainTransitionError,
    IntentCandidate,
    ModelBackend,
    SandboxCleanupError,
    StorageError,
    TaskRequest,
    TaskRunController,
    TaskRunControllerRegistry,
    TaskRunNotFoundError,
    TaskRunSnapshot,
    TaskRunStatus,
    TargetNetworkBinding,
    SqliteControlStore,
    ExecutionStatus,
    IntentStatus,
    PlanStatus,
    PlanStep,
    PlanValidationRecordDraft,
    PlanValidationStatus,
    RuntimeEvent,
    TurnStatus,
    TurnManager,
    UpsertExecution,
    UpsertIntent,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
)


@dataclass
class FakeClock:
    now: float = 1_800_000_000.0

    def now_utc(self) -> float:
        return self.now

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeSandbox:
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_destroy: bool = False
    fail_create: bool = False
    create_requests: list[str] = field(default_factory=list)

    def create(self, task_run_id: str) -> str:
        self.create_requests.append(task_run_id)
        if self.fail_create:
            raise RuntimeError("create unavailable")
        sandbox_id = f"sandbox-{task_run_id}"
        self.calls.append(("create", sandbox_id))
        return sandbox_id

    def interrupt(self, sandbox_id: str) -> None:
        self.calls.append(("interrupt", sandbox_id))

    def destroy(self, sandbox_id: str) -> None:
        self.calls.append(("destroy", sandbox_id))
        if self.fail_destroy:
            raise RuntimeError("destroy unavailable")


class EmptyEvidence:
    def command_exists(self, command_ref: str) -> bool:
        return False

    def output_exists(self, output_ref: str) -> bool:
        return False

    def lookup(self, execution_id: str) -> None:
        return None


@dataclass
class RunningEvidence:
    command_refs: set[str] = field(default_factory=lambda: {"command-running"})

    def command_exists(self, command_ref: str) -> bool:
        return command_ref in self.command_refs

    def output_exists(self, output_ref: str) -> bool:
        return False

    def lookup(self, execution_id: str) -> None:
        return None


@dataclass
class FailingStore:
    error: Exception = field(default_factory=lambda: StorageError("write failed"))

    def commit(self, mutation: object) -> object:
        raise self.error


@dataclass
class StubRepository:
    task_run: TaskRunSnapshot | None = None
    state: ControlStateSnapshot | None = None

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None:
        if self.task_run is None or self.task_run.task_run_id != task_run_id:
            return None
        return self.task_run

    def load_control_state(self, task_run_id: str) -> ControlStateSnapshot | None:
        if self.state is None or self.state.task_run.task_run_id != task_run_id:
            return None
        return self.state


@dataclass
class ReloadingRepository:
    base: ControlStateRepository
    status: TaskRunStatus

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None:
        task_run = self.base.load_task_run(task_run_id)
        return (
            replace(task_run, status=self.status)
            if task_run is not None
            else None
        )

    def load_control_state(self, task_run_id: str) -> ControlStateSnapshot | None:
        state = self.base.load_control_state(task_run_id)
        return (
            replace(state, task_run=replace(state.task_run, status=self.status))
            if state is not None
            else None
        )


class UnavailableRepository:
    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None:
        raise StorageError("read unavailable")

    def load_control_state(self, task_run_id: str) -> ControlStateSnapshot | None:
        raise StorageError("read unavailable")


def request(**overrides: object) -> TaskRequest:
    payload: dict[str, object] = {
        "goal": "找到flag",
        "target": {"endpoints": [{"url": "http://example.test:8080"}]},
    }
    payload.update(overrides)
    return TaskRequest.from_dict(payload)


def persistent_controller(
    sandbox: FakeSandbox,
    *,
    clock: FakeClock | None = None,
    store: Any | None = None,
    repository: Any | None = None,
    registry: TaskRunControllerRegistry | None = None,
) -> TaskRunController:
    clock = clock or FakeClock()
    registry = registry or TaskRunControllerRegistry()
    if store is None:
        store = SqliteControlStore.open(
            controller_database_path(),
            clock=clock,
        )
    if repository is None:
        repository = ControlStateRepository(store, EmptyEvidence())
    return TaskRunController(
        sandbox,
        clock=clock,
        store=store,
        repository=repository,
        registry=registry,
    )


def controller_database_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / ".test"
        / "runtime-work"
        / f"controller-{uuid4()}.sqlite3"
    )


def test_t6_target_resources_bind_atomically_and_cleanup_after_sandbox() -> None:
    calls: list[str] = []

    class NetworkSandbox(FakeSandbox):
        def configure_target_network(self, task_run_id, binding):
            calls.append("sandbox_configure")
            assert binding.target_url == "http://example.test:8080"

        def create(self, task_run_id):
            calls.append("sandbox_create")
            return super().create(task_run_id)

        def destroy(self, sandbox_id):
            calls.append("sandbox_destroy")
            return super().destroy(sandbox_id)

        def bind(self, task_run_id, sandbox_id, *, terminal):
            calls.append("sandbox_bind")

        def quiesce(self, sandbox_id):
            calls.append("sandbox_quiesce")

    class Network:
        def create(self, task_run_id, target_url):
            calls.append("network_create")
            return TargetNetworkBinding(
                "a" * 64, "b" * 64, "c" * 64, "d" * 64, target_url
            )

        def bind(self, task_run_id, binding, *, terminal):
            calls.append("network_bind")

        def destroy(self, task_run_id, binding):
            calls.append("network_destroy")

    clock = FakeClock()
    sandbox = NetworkSandbox()
    network = Network()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        repository = ControlStateRepository(store, EmptyEvidence())
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=repository,
            registry=TaskRunControllerRegistry(),
            target_network_adapter=network,
        )
        task_id = str(uuid4())
        controller.start(request(), ModelBackend.CODEX_CLI, str(uuid4()), task_run_id=task_id)
        assert calls[:3] == ["network_create", "sandbox_configure", "sandbox_create"]
        assert store.load_task_run(task_id).target_network.forwarder_id == "c" * 64

        resumed = TaskRunController(
            NetworkSandbox(),
            clock=clock,
            store=store,
            repository=repository,
            registry=TaskRunControllerRegistry(),
            target_network_adapter=network,
        )
        resumed.attach(task_id)
        assert calls.index("network_bind") < calls.index("sandbox_bind")
        controller.conclude(TaskRunStatus.INCONCLUSIVE, str(uuid4()))
        assert calls[-3:] == ["sandbox_quiesce", "sandbox_destroy", "network_destroy"]
        controller.close()


def test_explicit_cancel_attaches_without_reopening_execution_when_target_is_unavailable() -> None:
    calls: list[str] = []

    class NetworkSandbox(FakeSandbox):
        def configure_target_network(self, _task_run_id, _binding):
            pass

        def bind(self, _task_run_id, _sandbox_id, *, terminal):
            calls.append(f"sandbox_bind:{terminal}")

        def quiesce(self, _sandbox_id):
            calls.append("sandbox_quiesce")

        def destroy(self, sandbox_id):
            calls.append("sandbox_destroy")
            super().destroy(sandbox_id)

    class Network:
        fail_destroy = True

        def create(self, _task_run_id, target_url):
            return TargetNetworkBinding(
                "a" * 64, "b" * 64, "c" * 64, "d" * 64, target_url
            )

        def bind(self, _task_run_id, _binding, *, terminal):
            calls.append(f"network_bind:{terminal}")
            if not terminal:
                raise RuntimeError("active forwarder unavailable")

        def destroy(self, _task_run_id, binding):
            assert binding.forwarder_id == "c" * 64
            calls.append("network_destroy")
            if self.fail_destroy:
                raise RuntimeError("network cleanup unconfirmed")

    clock = FakeClock()
    network = Network()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        repository = ControlStateRepository(store, EmptyEvidence())

        def owner():
            return TaskRunController(
                NetworkSandbox(), clock=clock, store=store,
                repository=repository, registry=TaskRunControllerRegistry(),
                target_network_adapter=network,
            )

        task_id = str(uuid4())
        started = owner()
        started.start(request(), ModelBackend.CODEX_CLI, str(uuid4()), task_run_id=task_id)

        # Fresh registries model separate CLI processes after the paused run.
        strict = owner()
        with pytest.raises(RuntimeError, match="active forwarder unavailable"):
            strict.attach(task_id)
        assert store.load_task_run(task_id).status is TaskRunStatus.RUNNING

        cancelling = owner()
        cancelling.attach_for_cancellation(task_id)
        assert "network_bind:True" in calls
        assert "sandbox_bind:True" in calls
        assert "sandbox_bind:False" not in calls
        with pytest.raises(SandboxCleanupError):
            cancelling.cancel(str(uuid4()))
        assert store.load_task_run(task_id).status is TaskRunStatus.CANCELLED
        assert calls.index("sandbox_destroy") < calls.index("network_destroy")

        network.fail_destroy = False
        cancelling.cleanup_sandbox()
        assert calls.count("sandbox_destroy") == 1
        assert calls.count("network_destroy") == 2
        cancelling.close()


@pytest.fixture
def source_root() -> Path:
    path = controller_database_path().parent / f"source-{uuid4()}"
    path.mkdir(parents=True)
    return path


def _commit_control_mutation(
    store: SqliteControlStore,
    *,
    task_run_id: str,
    request_value: TaskRequest,
    base_checkpoint_sequence: int,
    mutation_id: str,
    operation: str,
    changes: tuple[object, ...],
    entity_ids: list[str],
    result: str,
    status: TaskRunStatus,
) -> object:
    event = RuntimeEvent(
        type="task_run_started" if operation == "start_task_run" else "control_state_committed",
        task_run_id=task_run_id,
        timestamp=100.0 + base_checkpoint_sequence,
        status=status,
        details={
            "operation": operation,
            "entity_ids": entity_ids,
            "result": result,
        },
    )
    mutation = ControlMutation.create(
        task_run_id=task_run_id,
        mutation_id=mutation_id,
        operation=operation,
        request=request_value,
        effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=base_checkpoint_sequence,
        state_changes=changes,
        event=event,
    )
    return store.commit(mutation)


def _persist_running_execution(store: SqliteControlStore) -> str:
    task_run_id = "task-recovery"
    task_request = request()
    _commit_control_mutation(
        store,
        task_run_id=task_run_id,
        request_value=task_request,
        base_checkpoint_sequence=0,
        mutation_id=str(uuid4()),
        operation="start_task_run",
        changes=(
            UpsertTaskRun(
                task_run_id=task_run_id,
                request=task_request,
                effective_model_backend=ModelBackend.CODEX_CLI,
                started_at=100.0,
                deadline_at=3_700.0,
                status=TaskRunStatus.RUNNING,
                sandbox_id="sandbox-recovery",
            ),
        ),
        entity_ids=[task_run_id],
        result=task_run_id,
        status=TaskRunStatus.RUNNING,
    )
    plan_steps = (PlanStep("执行恢复测试命令"),)
    _commit_control_mutation(
        store,
        task_run_id=task_run_id,
        request_value=task_request,
        base_checkpoint_sequence=1,
        mutation_id=str(uuid4()),
        operation="register_turn",
        changes=(
            UpsertTurn(
                turn_id="turn-recovery",
                task_run_id=task_run_id,
                sequence=1,
                turn_target="恢复测试",
                intent_ids=("intent-recovery",),
                status=TurnStatus.RUNNING,
            ),
            UpsertIntent(
                intent_id="intent-recovery",
                turn_id="turn-recovery",
                description="恢复运行中的执行",
                priority=1,
                intent_type=None,
                status=IntentStatus.RUNNING,
            ),
            UpsertPlan(
                plan_id="plan-recovery",
                intent_id="intent-recovery",
                steps=plan_steps,
                status=PlanStatus.RUNNING,
            ),
        ),
        entity_ids=["turn-recovery", "intent-recovery", "plan-recovery"],
        result="turn-recovery",
        status=TaskRunStatus.RUNNING,
    )
    _commit_control_mutation(
        store,
        task_run_id=task_run_id,
        request_value=task_request,
        base_checkpoint_sequence=2,
        mutation_id=str(uuid4()),
        operation="append_plan_validation_record",
        changes=(
            AppendPlanValidationRecord(
                PlanValidationRecordDraft(
                    plan_validation_id="validation-recovery",
                    plan_id="plan-recovery",
                    task_run_id=task_run_id,
                    step_index=0,
                    attempt=1,
                    status=PlanValidationStatus.SUCCEEDED,
                    failure_type=None,
                    short_reason="command generated",
                    command_ref="command-running",
                )
            ),
        ),
        entity_ids=["validation-recovery"],
        result="validation-recovery",
        status=TaskRunStatus.RUNNING,
    )
    _commit_control_mutation(
        store,
        task_run_id=task_run_id,
        request_value=task_request,
        base_checkpoint_sequence=3,
        mutation_id=str(uuid4()),
        operation="create_execution",
        changes=(
            UpsertExecution(
                execution_id="execution-recovery",
                plan_id="plan-recovery",
                step_index=0,
                command_ref="command-running",
                status=ExecutionStatus.RUNNING,
                output_ref=None,
            ),
        ),
        entity_ids=["execution-recovery"],
        result="execution-recovery",
        status=TaskRunStatus.RUNNING,
    )
    return task_run_id


def test_persistent_start_commits_then_reloads_and_claims_registry() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    database = controller_database_path()
    with SqliteControlStore.open(database, clock=clock) as store:
        registry = TaskRunControllerRegistry()
        repository = ControlStateRepository(store, EmptyEvidence())
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=repository,
            registry=registry,
        )

        committed = controller.start(
            request(),
            effective_model_backend="codex_cli",
            mutation_id=str(uuid4()),
        )

        assert committed.value == controller.control_state.task_run.task_run_id
        assert committed.commit.replayed is False
        assert controller.status() is TaskRunStatus.RUNNING
        assert sandbox.calls == [("create", controller.control_state.task_run.sandbox_id)]
        assert registry.owner(committed.value) is controller


def test_persistent_start_saves_source_root_and_keeps_it_after_finish(
    source_root: Path,
) -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    canonical_root = str(source_root.resolve(strict=True))
    database = source_root / "control.sqlite3"
    with SqliteControlStore.open(database, clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        started = controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
            source_root=canonical_root,
        )
        assert controller.control_state.task_run.source_root == canonical_root
        assert store.load_task_run(started.value).source_root == canonical_root

        controller.cancel(str(uuid4()))
        assert store.load_task_run(started.value).source_root == canonical_root

    with SqliteControlStore.open(database, clock=clock) as reopened:
        assert reopened.load_task_run(started.value).source_root == canonical_root


@pytest.mark.parametrize("invalid_root", ["relative", "missing", "noncanonical", "file"])
def test_persistent_start_rejects_invalid_source_root_before_sandbox_create(
    source_root: Path, invalid_root: str
) -> None:
    source_file = source_root / "sample.txt"
    source_file.write_text("sample", encoding="utf-8")
    values = {
        "relative": "attachments",
        "missing": str(source_root / "missing"),
        "noncanonical": f"{source_root}{os.sep}.",
        "file": str(source_file),
    }
    sandbox = FakeSandbox()
    with SqliteControlStore.open(source_root / "control.sqlite3", clock=FakeClock()) as store:
        controller = TaskRunController(
            sandbox,
            clock=FakeClock(),
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        with pytest.raises(ValueError, match="source_root"):
            controller.start(
                request(),
                effective_model_backend=ModelBackend.CODEX_CLI,
                mutation_id=str(uuid4()),
                source_root=values[invalid_root],
            )
        assert sandbox.create_requests == []


def test_register_turn_persists_fixed_pending_intents_and_replays() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        mutation_id = str(uuid4())
        initial_state = controller.control_state
        registration = TurnManager.prepare_turn_registration(
            state=initial_state,
            mutation_id=mutation_id,
            turn_target="确认开放服务",
            candidates=(
                IntentCandidate("检查 HTTP 服务", "service_discovery"),
                IntentCandidate("检查附件内容"),
            ),
        )
        duplicate_preparation = TurnManager.prepare_turn_registration(
            state=initial_state,
            mutation_id=mutation_id,
            turn_target="确认开放服务",
            candidates=(
                IntentCandidate("检查 HTTP 服务", "service_discovery"),
                IntentCandidate("检查附件内容"),
            ),
        )

        assert duplicate_preparation == registration
        assert controller.control_state.latest_checkpoint.sequence == 1

        committed = controller.commit_turn_registration(registration, mutation_id)

        assert committed.value == registration.turn_id
        assert committed.commit.replayed is False
        assert committed.commit.event.details == {
            "operation": "register_turn",
            "entity_ids": list(registration.entity_ids),
            "result": registration.turn_id,
        }
        state = controller.control_state
        assert state.latest_checkpoint.sequence == 2
        assert len(state.turns) == 1
        turn = state.turns[0]
        assert turn.turn_id == registration.turn_id
        assert turn.sequence == 1
        assert turn.status is TurnStatus.PENDING
        assert turn.intent_ids == registration.entity_ids[1:]
        assert [(item.priority, item.status) for item in state.intents] == [
            (1, IntentStatus.PENDING),
            (2, IntentStatus.PENDING),
        ]

        replayed = controller.commit_turn_registration(registration, mutation_id)

        assert replayed.value == registration.turn_id
        assert replayed.commit.replayed is True
        assert controller.control_state.latest_checkpoint.sequence == 2
        assert len(controller.control_state.turns) == 1


def test_turn_registration_rejects_empty_candidates_and_nonterminal_previous_turn() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        with pytest.raises(DomainError, match="non-empty"):
            TurnManager.prepare_turn_registration(
                state=controller.control_state,
                mutation_id=str(uuid4()),
                turn_target="确认开放服务",
                candidates=(),
            )
        assert controller.control_state.latest_checkpoint.sequence == 1
        assert controller.control_state.turns == ()

        mutation_id = str(uuid4())
        registration = TurnManager.prepare_turn_registration(
            state=controller.control_state,
            mutation_id=mutation_id,
            turn_target="确认开放服务",
            candidates=(IntentCandidate("检查 HTTP 服务"),),
        )
        controller.commit_turn_registration(registration, mutation_id)

        with pytest.raises(DomainTransitionError, match="previous Turn"):
            TurnManager.prepare_turn_registration(
                state=controller.control_state,
                mutation_id=str(uuid4()),
                turn_target="寻找可利用入口",
                candidates=(IntentCandidate("检查上传接口"),),
            )
        assert controller.control_state.latest_checkpoint.sequence == 2


def test_turn_registration_requires_running_task_run_and_deadline_preempts() -> None:
    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.start(
            request(deadline_seconds=60),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        mutation_id = str(uuid4())
        registration = TurnManager.prepare_turn_registration(
            state=controller.control_state,
            mutation_id=mutation_id,
            turn_target="确认开放服务",
            candidates=(IntentCandidate("检查 HTTP 服务"),),
        )
        clock.now = 160.0

        preempted = controller.commit_turn_registration(registration, mutation_id)

        assert preempted.value == "deadline_exceeded"
        assert controller.status() is TaskRunStatus.DEADLINE_EXCEEDED
        assert controller.control_state.turns == ()

    clock = FakeClock()
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        controller.mark_candidate_found(str(uuid4()))

        with pytest.raises(DomainTransitionError, match="running TaskRun"):
            TurnManager.prepare_turn_registration(
                state=controller.control_state,
                mutation_id=str(uuid4()),
                turn_target="确认开放服务",
                candidates=(IntentCandidate("检查 HTTP 服务"),),
            )


def test_start_rejects_backend_and_mutation_before_sandbox_creation() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    database = controller_database_path()
    with SqliteControlStore.open(database, clock=clock) as store:
        repository = ControlStateRepository(store, EmptyEvidence())
        for backend, mutation in [
            (None, str(uuid4())),
            ("not-a-backend", str(uuid4())),
            ("codex_cli", "not-a-uuid4"),
        ]:
            controller = TaskRunController(
                sandbox,
                clock=clock,
                store=store,
                repository=repository,
                registry=TaskRunControllerRegistry(),
            )
            with pytest.raises(ValueError):
                controller.start(request(), effective_model_backend=backend, mutation_id=mutation)
        assert sandbox.calls == []


def test_start_conflicting_request_backend_is_rejected_before_sandbox() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    database = controller_database_path()
    with SqliteControlStore.open(database, clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        with pytest.raises(ValueError, match="conflicts"):
            controller.start(
                request(model_backend="claude_code_cli"),
                effective_model_backend="codex_cli",
                mutation_id=str(uuid4()),
            )
    assert sandbox.calls == []


def test_registry_rejects_second_controller_and_wrong_release() -> None:
    registry = TaskRunControllerRegistry()
    first = object()
    second = object()
    registry.claim("task-1", first)
    registry.claim("task-1", first)
    with pytest.raises(ControllerAlreadyClaimedError):
        registry.claim("task-1", second)
    with pytest.raises(RuntimeError):
        registry.release("task-1", second)
    registry.release("task-1", first)
    assert registry.owner("task-1") is None


def test_start_uses_reloaded_state_not_uncommitted_working_copy() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        repository = ReloadingRepository(
            ControlStateRepository(store, EmptyEvidence()),
            TaskRunStatus.INCONCLUSIVE,
        )
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=repository,
            registry=TaskRunControllerRegistry(),
        )

        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )

        assert controller.status() is TaskRunStatus.INCONCLUSIVE
        assert controller.control_state.task_run.status is TaskRunStatus.INCONCLUSIVE


def test_start_storage_failure_releases_registry_and_compensates_destroy() -> None:
    sandbox = FakeSandbox()
    registry = TaskRunControllerRegistry()
    controller = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=StubRepository(),
        registry=registry,
    )
    with pytest.raises(StorageError, match="write failed"):
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
    assert sandbox.calls and sandbox.calls[0][0] == "create"
    assert sandbox.calls[1][0] == "destroy"
    created_id = sandbox.calls[0][1]
    assert registry.owner(created_id.removeprefix("sandbox-")) is None


def test_start_compensation_failure_exposes_sandbox_and_original_errors() -> None:
    sandbox = FakeSandbox(fail_destroy=True)
    controller = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=StubRepository(),
    )
    with pytest.raises(SandboxCleanupError) as raised:
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
    assert raised.value.sandbox_id.startswith("sandbox-")
    assert isinstance(raised.value.original_error, StorageError)
    assert isinstance(raised.value.cleanup_error, RuntimeError)


def test_start_sandbox_create_failure_releases_registry() -> None:
    sandbox = FakeSandbox(fail_create=True)
    registry = TaskRunControllerRegistry()
    controller = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=StubRepository(),
        registry=registry,
    )

    with pytest.raises(RuntimeError, match="create unavailable"):
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )

    assert len(sandbox.create_requests) == 1
    assert registry.owner(sandbox.create_requests[0]) is None
    assert sandbox.calls == []


def test_start_reload_failure_keeps_registry_and_rejects_future_operations() -> None:
    sandbox = FakeSandbox()
    registry = TaskRunControllerRegistry()
    with SqliteControlStore.open(
        controller_database_path(),
        clock=FakeClock(),
    ) as store:
        controller = TaskRunController(
            sandbox,
            clock=FakeClock(),
            store=store,
            repository=UnavailableRepository(),
            registry=registry,
        )

        with pytest.raises(StorageError, match="read unavailable"):
            controller.start(
                request(),
                effective_model_backend=ModelBackend.CODEX_CLI,
                mutation_id=str(uuid4()),
            )

        task_run_id = sandbox.create_requests[0]
        assert registry.owner(task_run_id) is controller
        assert sandbox.calls == [("create", f"sandbox-{task_run_id}")]
        with pytest.raises(StorageError, match="cache is invalid"):
            controller.start(
                request(),
                effective_model_backend=ModelBackend.CODEX_CLI,
                mutation_id=str(uuid4()),
            )
        with pytest.raises(StorageError, match="cache is invalid"):
            controller.attach(task_run_id)
        with pytest.raises(StorageError, match="cache is invalid"):
            controller.control_state


def _stub_state(task_run_id: str, status: TaskRunStatus) -> tuple[TaskRunSnapshot, ControlStateSnapshot]:
    task = TaskRunSnapshot(
        task_run_id=task_run_id,
        request=request(),
        effective_model_backend=ModelBackend.CODEX_CLI,
        started_at=100.0,
        deadline_at=3_700.0,
        status=status,
        sandbox_id="sandbox-existing",
        confirmation_deadline=None,
        finished_at=200.0 if status in {TaskRunStatus.ACHIEVED, TaskRunStatus.FAILED} else None,
        failure_reason=None,
    )
    state = ControlStateSnapshot(
        task_run=task,
        latest_checkpoint=Checkpoint(task_run_id, 1, 1, 100.0),
        turns=(),
        intents=(),
        plans=(),
        executions=(),
        facts=(),
        failure_summaries=(),
        plan_validation_records=(),
    )
    return task, state


def test_attach_binds_existing_sandbox_without_create() -> None:
    task, state = _stub_state("task-existing", TaskRunStatus.RUNNING)
    sandbox = FakeSandbox()
    registry = TaskRunControllerRegistry()
    repository = StubRepository(task, state)
    controller = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=repository,
        registry=registry,
    )

    attached = controller.attach(task.task_run_id)

    assert attached == state
    assert sandbox.calls == []
    assert registry.owner(task.task_run_id) is controller
    assert controller.status() is TaskRunStatus.RUNNING


def test_attach_missing_task_releases_registry() -> None:
    sandbox = FakeSandbox()
    registry = TaskRunControllerRegistry()
    controller = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=StubRepository(),
        registry=registry,
    )
    with pytest.raises(TaskRunNotFoundError):
        controller.attach("missing-task")
    assert registry.owner("missing-task") is None


def test_attach_load_failure_releases_registry_and_allows_retry() -> None:
    task, state = _stub_state("task-retry", TaskRunStatus.RUNNING)
    sandbox = FakeSandbox()
    registry = TaskRunControllerRegistry()
    failed = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=UnavailableRepository(),
        registry=registry,
    )

    with pytest.raises(StorageError, match="read unavailable"):
        failed.attach(task.task_run_id)
    assert registry.owner(task.task_run_id) is None

    retry = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=StubRepository(task, state),
        registry=registry,
    )
    assert retry.attach(task.task_run_id) == state
    assert registry.owner(task.task_run_id) is retry


def test_close_requires_terminal_and_only_releases_without_destroy() -> None:
    task, state = _stub_state("task-terminal", TaskRunStatus.ACHIEVED)
    sandbox = FakeSandbox()
    registry = TaskRunControllerRegistry()
    controller = persistent_controller(
        sandbox,
        store=FailingStore(),
        repository=StubRepository(task, state),
        registry=registry,
    )
    controller.attach(task.task_run_id)

    controller.close()

    assert sandbox.calls == []
    assert registry.owner(task.task_run_id) is None


def test_close_rejects_running_task() -> None:
    task, state = _stub_state("task-running", TaskRunStatus.RUNNING)
    registry = TaskRunControllerRegistry()
    controller = persistent_controller(
        FakeSandbox(),
        store=FailingStore(),
        repository=StubRepository(task, state),
        registry=registry,
    )
    controller.attach(task.task_run_id)
    with pytest.raises(Exception, match="terminal"):
        controller.close()
    assert registry.owner(task.task_run_id) is controller


def test_recover_running_execution_commits_inconclusive_without_touching_sandbox() -> None:
    clock = FakeClock(now=500.0)
    sandbox = FakeSandbox()
    database = controller_database_path()
    with SqliteControlStore.open(database, clock=clock) as store:
        task_run_id = _persist_running_execution(store)
        repository = ControlStateRepository(store, RunningEvidence())
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=repository,
            registry=TaskRunControllerRegistry(),
        )

        attached = controller.attach(task_run_id)
        assert attached.task_run.status is TaskRunStatus.RUNNING

        recovered = controller.recover_task_run(str(uuid4()))

        assert recovered.commit is not None
        assert recovered.commit.replayed is False
        assert recovered.commit.event.details == {
            "operation": "recover_running_execution_as_inconclusive",
            "entity_ids": [
                task_run_id,
                "execution-recovery",
                "plan-recovery",
                "intent-recovery",
                "turn-recovery",
            ],
            "result": "inconclusive",
        }
        assert recovered.state.task_run.status is TaskRunStatus.INCONCLUSIVE
        assert (
            recovered.state.task_run.failure_reason
            == "unfinished_execution_detected_during_recovery"
        )
        assert recovered.state.task_run.finished_at == clock.now
        assert recovered.state.executions[0].status is ExecutionStatus.RUNNING
        assert recovered.state.plans[0].status is PlanStatus.INCONCLUSIVE
        assert recovered.state.intents[0].status is IntentStatus.INCONCLUSIVE
        assert recovered.state.turns[0].status is TurnStatus.INCONCLUSIVE
        assert sandbox.calls == []


def test_recover_terminal_task_only_loads_state_without_event() -> None:
    task, state = _stub_state("task-terminal-recovery", TaskRunStatus.INCONCLUSIVE)
    controller = persistent_controller(
        FakeSandbox(),
        store=FailingStore(),
        repository=StubRepository(task, state),
    )
    controller.attach(task.task_run_id)

    recovered = controller.recover_task_run(str(uuid4()))

    assert recovered.state == state
    assert recovered.commit is None


def test_recover_without_running_execution_only_loads_state() -> None:
    task, state = _stub_state("task-no-running-recovery", TaskRunStatus.RUNNING)
    controller = persistent_controller(
        FakeSandbox(),
        store=FailingStore(),
        repository=StubRepository(task, state),
    )
    controller.attach(task.task_run_id)

    recovered = controller.recover_task_run(str(uuid4()))

    assert recovered.state == state
    assert recovered.commit is None


def test_recover_rejects_running_execution_with_terminal_parent() -> None:
    clock = FakeClock(now=500.0)
    database = controller_database_path()
    with SqliteControlStore.open(database, clock=clock) as store:
        task_run_id = _persist_running_execution(store)
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE plans SET status = 'completed' WHERE plan_id = 'plan-recovery'"
            )
            connection.commit()
        finally:
            connection.close()
        repository = ControlStateRepository(store, RunningEvidence())
        controller = TaskRunController(
            FakeSandbox(),
            clock=clock,
            store=store,
            repository=repository,
            registry=TaskRunControllerRegistry(),
        )
        controller.attach(task_run_id)

        with pytest.raises(StorageError, match="terminal Plan"):
            controller.recover_task_run(str(uuid4()))


def test_persistent_confirmation_uses_existing_message_and_replays_commit() -> None:
    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        started = controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        candidate_mutation_id = str(uuid4())

        candidate = controller.mark_candidate_found(candidate_mutation_id)

        assert candidate.value == "awaiting_confirmation"
        assert candidate.commit.event.type == "completion_confirmation_requested"
        assert controller.status() is TaskRunStatus.AWAITING_CONFIRMATION
        stored_candidate = store.load_committed_mutation(
            started.value,
            candidate_mutation_id,
        )
        assert stored_candidate is not None
        assert stored_candidate.replayed is True
        assert stored_candidate.event == candidate.commit.event
        replayed_candidate = controller.mark_candidate_found(candidate_mutation_id)
        assert replayed_candidate.value == "awaiting_confirmation"
        assert replayed_candidate.commit.replayed is True

        completion_mutation_id = str(uuid4())
        completed = controller.handle_control_message(
            {
                "type": "completion",
                "task_run_id": started.value,
                "status": "achieved",
            },
            completion_mutation_id,
        )

        assert completed.value == "achieved"
        assert controller.status() is TaskRunStatus.ACHIEVED
        assert sandbox.calls == [
            ("create", f"sandbox-{started.value}"),
            ("destroy", f"sandbox-{started.value}"),
        ]
        replayed_completion = controller.handle_control_message(
            {
                "type": "completion",
                "task_run_id": started.value,
                "status": "achieved",
            },
            completion_mutation_id,
        )
        assert replayed_completion.value == "achieved"
        assert replayed_completion.commit.replayed is True
        assert sandbox.calls.count(("destroy", f"sandbox-{started.value}")) == 1


def test_persistent_advance_time_does_not_consume_mutation_without_change() -> None:
    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        mutation_id = str(uuid4())

        assert controller.advance_time(mutation_id) is None
        assert store.load_committed_mutation(
            controller.control_state.task_run.task_run_id,
            mutation_id,
        ) is None

        candidate = controller.mark_candidate_found(mutation_id)

        assert candidate.value == "awaiting_confirmation"
        assert candidate.commit.replayed is False


def test_persistent_confirmation_window_expires_as_assumed_achieved() -> None:
    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.start(
            request(deadline_seconds=1_000),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        controller.mark_candidate_found(str(uuid4()))
        clock.now = 280.0

        finished = controller.advance_time(str(uuid4()))

        assert finished is not None
        assert finished.value == "assumed_achieved"
        assert controller.status() is TaskRunStatus.ASSUMED_ACHIEVED
        assert sandbox.calls[-1][0] == "destroy"


@pytest.mark.parametrize(
    ("name", "finish"),
    [
        ("cancel", lambda controller, mutation_id: controller.cancel(mutation_id)),
        (
            "model_failed",
            lambda controller, mutation_id: controller.model_failed("provider failure", mutation_id),
        ),
        (
            "conclude",
            lambda controller, mutation_id: controller.conclude(
                TaskRunStatus.INCONCLUSIVE,
                mutation_id,
            ),
        ),
    ],
)
def test_persistent_deadline_preempts_other_terminal_requests(
    name: str,
    finish: object,
) -> None:
    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        started = controller.start(
            request(deadline_seconds=60),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        clock.now = 160.0

        result = finish(controller, str(uuid4()))  # type: ignore[operator]

        assert result.value == "deadline_exceeded", name
        assert result.commit.event.status is TaskRunStatus.DEADLINE_EXCEEDED, name
        assert controller.status() is TaskRunStatus.DEADLINE_EXCEEDED, name
        assert sandbox.calls[-2:] == [
            ("interrupt", f"sandbox-{started.value}"),
            ("destroy", f"sandbox-{started.value}"),
        ]


def test_persistent_deadline_preempts_candidate_and_external_completion() -> None:
    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        started = controller.start(
            request(deadline_seconds=60),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        clock.now = 160.0

        deadline_result = controller.mark_candidate_found(str(uuid4()))

        assert deadline_result.value == "deadline_exceeded"
        assert controller.status() is TaskRunStatus.DEADLINE_EXCEEDED

    clock = FakeClock(now=100.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        started = controller.start(
            request(deadline_seconds=60),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        controller.mark_candidate_found(str(uuid4()))
        clock.now = 160.0

        deadline_result = controller.handle_control_message(
            {
                "type": "completion",
                "task_run_id": started.value,
                "status": "achieved",
            },
            str(uuid4()),
        )

        assert deadline_result.value == "deadline_exceeded"
        assert controller.status() is TaskRunStatus.DEADLINE_EXCEEDED


def test_persistent_terminal_cleanup_retains_running_execution() -> None:
    clock = FakeClock(now=500.0)
    sandbox = FakeSandbox()
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        task_run_id = _persist_running_execution(store)
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, RunningEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        controller.attach(task_run_id)

        cancelled = controller.cancel(str(uuid4()))

        assert cancelled.value == "cancelled"
        assert controller.status() is TaskRunStatus.CANCELLED
        assert controller.control_state.executions[0].status is ExecutionStatus.RUNNING
        assert sandbox.calls == [
            ("interrupt", "sandbox-recovery"),
            ("destroy", "sandbox-recovery"),
        ]


def test_persistent_terminal_replay_does_not_retry_sandbox_destroy() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox(fail_destroy=True)
    with SqliteControlStore.open(controller_database_path(), clock=clock) as store:
        controller = TaskRunController(
            sandbox,
            clock=clock,
            store=store,
            repository=ControlStateRepository(store, EmptyEvidence()),
            registry=TaskRunControllerRegistry(),
        )
        started = controller.start(
            request(),
            effective_model_backend=ModelBackend.CODEX_CLI,
            mutation_id=str(uuid4()),
        )
        mutation_id = str(uuid4())

        with pytest.raises(SandboxCleanupError, match="destroy failed"):
            controller.model_failed("provider failure", mutation_id)

        assert controller.status() is TaskRunStatus.FAILED
        assert store.load_committed_mutation(started.value, mutation_id) is not None
        sandbox.fail_destroy = False

        replayed = controller.model_failed("provider failure", mutation_id)

        assert replayed.value == "failed"
        assert replayed.commit.replayed is True
        assert sandbox.calls.count(("destroy", f"sandbox-{started.value}")) == 1
        controller.cleanup_sandbox()
        assert sandbox.calls.count(("destroy", f"sandbox-{started.value}")) == 2
