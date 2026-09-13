from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_core import (
    AppendFailureSummary,
    AppendPlanValidationRecord,
    CommittedPlanValidation,
    ControlCommitResult,
    ControlMutation,
    ControlStateRepository,
    ExecutionEvidence,
    ExecutionStatus,
    FactStatus,
    FailureType,
    IntentStatus,
    ModelBackend,
    PlanStatus,
    PlanStep,
    PlanValidationRecordDraft,
    PlanValidationStatus,
    RuntimeEvent,
    SqliteControlStore,
    StorageError,
    TaskRequest,
    TaskRunNotFoundError,
    TaskRunStatus,
    TurnStatus,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
)


@dataclass
class FakeStorageClock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeExecutionEvidence:
    command_refs: set[str] = field(default_factory=set)
    output_refs: set[str] = field(default_factory=set)
    records: dict[str, ExecutionEvidence] = field(default_factory=dict)
    failure: Exception | None = None

    def command_exists(self, command_ref: str) -> bool:
        if self.failure is not None:
            raise self.failure
        return command_ref in self.command_refs

    def output_exists(self, output_ref: str) -> bool:
        if self.failure is not None:
            raise self.failure
        return output_ref in self.output_refs

    def lookup(self, execution_id: str) -> ExecutionEvidence | None:
        if self.failure is not None:
            raise self.failure
        return self.records.get(execution_id)


@pytest.fixture
def database_path() -> Path:
    runtime_directory = Path(__file__).resolve().parents[1] / ".test" / "runtime"
    runtime_directory.mkdir(parents=True, exist_ok=True)
    path = runtime_directory / f"state-{uuid.uuid4()}.sqlite3"
    yield path
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def request() -> TaskRequest:
    return TaskRequest.from_dict(
        {
            "goal": "找到 flag",
            "target": {"endpoints": [{"url": "http://example.test:8080"}]},
            "deadline_seconds": 3600,
            "model_backend": "codex_cli",
        }
    )


def commit_changes(
    store: SqliteControlStore,
    *,
    request_value: TaskRequest,
    task_run_id: str,
    base_checkpoint_sequence: int,
    operation: str,
    changes: tuple[object, ...],
) -> ControlCommitResult:
    entity_ids = []
    for change in changes:
        if isinstance(change, UpsertTaskRun):
            value = change.task_run_id
        elif isinstance(change, UpsertTurn):
            value = change.turn_id
        elif isinstance(change, UpsertIntent):
            value = change.intent_id
        elif isinstance(change, UpsertPlan):
            value = change.plan_id
        elif isinstance(change, UpsertExecution):
            value = change.execution_id
        elif isinstance(change, UpsertFact):
            value = change.fact_id
        elif isinstance(change, AppendPlanValidationRecord):
            value = change.record.plan_validation_id
        else:
            continue
        if value not in entity_ids:
            entity_ids.append(value)
    dynamic_result_fields = {
        "start_task_run": "task_run_id",
        "register_turn": "turn_id",
        "register_plan": "plan_id",
        "create_execution": "execution_id",
        "create_fact": "fact_id",
    }
    if operation == "append_plan_validation_record":
        result = next(
            change.record.plan_validation_id
            for change in changes
            if isinstance(change, AppendPlanValidationRecord)
        )
    elif operation in dynamic_result_fields:
        field_name = dynamic_result_fields[operation]
        result = next(
            value
            for change in changes
            if isinstance((value := getattr(change, field_name, None)), str)
        )
    elif operation == "record_target_mismatch":
        result = "try_next_intent"
    elif operation == "verify_fact":
        result = "verified"
    else:
        raise AssertionError(f"test fixture has no result for operation {operation}")
    mutation = ControlMutation.create(
        task_run_id=task_run_id,
        mutation_id=str(uuid.uuid4()),
        operation=operation,
        request=request_value,
        effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=base_checkpoint_sequence,
        state_changes=changes,
        event=RuntimeEvent(
            type=(
                "task_run_started"
                if operation == "start_task_run"
                else "control_state_committed"
            ),
            task_run_id=task_run_id,
            timestamp=1_700_000_000.0 + base_checkpoint_sequence,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": operation,
                "entity_ids": entity_ids,
                "result": result,
            },
        ),
    )
    return store.commit(mutation)


def persist_backtracking_state(
    store: SqliteControlStore,
) -> tuple[str, FakeExecutionEvidence]:
    task_run_id = "task-run-state"
    request_value = request()
    started_at = 1_700_000_000.0
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=0,
        operation="start_task_run",
        changes=(
            UpsertTaskRun(
                task_run_id=task_run_id,
                request=request_value,
                effective_model_backend=ModelBackend.CODEX_CLI,
                started_at=started_at,
                deadline_at=started_at + request_value.deadline_seconds,
                status=TaskRunStatus.RUNNING,
                sandbox_id="sandbox-state",
            ),
        ),
    )
    steps = (PlanStep("使用已授权工具确认入口"),)
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=1,
        operation="register_turn",
        changes=(
            UpsertTurn(
                turn_id="turn-1",
                task_run_id=task_run_id,
                sequence=1,
                turn_target="确认入口",
                intent_ids=("intent-a", "intent-b"),
                status=TurnStatus.RUNNING,
            ),
            UpsertIntent(
                "intent-a",
                "turn-1",
                "尝试路径 A",
                1,
                None,
                IntentStatus.RUNNING,
            ),
            UpsertIntent(
                "intent-b",
                "turn-1",
                "尝试路径 B",
                2,
                None,
                IntentStatus.PENDING,
            ),
            UpsertPlan("plan-a", "intent-a", steps, PlanStatus.RUNNING),
            UpsertPlan("plan-b", "intent-b", steps, PlanStatus.PENDING),
        ),
    )
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=2,
        operation="append_plan_validation_record",
        changes=(
            AppendPlanValidationRecord(
                PlanValidationRecordDraft(
                    plan_validation_id="validation-a",
                    plan_id="plan-a",
                    task_run_id=task_run_id,
                    step_index=0,
                    attempt=1,
                    status=PlanValidationStatus.SUCCEEDED,
                    failure_type=None,
                    short_reason="command generated",
                    command_ref="command-a",
                )
            ),
        ),
    )
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=3,
        operation="create_execution",
        changes=(
            UpsertExecution(
                "execution-a",
                "plan-a",
                0,
                "command-a",
                ExecutionStatus.RUNNING,
                None,
            ),
        ),
    )
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=4,
        operation="record_target_mismatch",
        changes=(
            UpsertExecution(
                "execution-a",
                "plan-a",
                0,
                "command-a",
                ExecutionStatus.SUCCEEDED,
                "output-a",
            ),
            UpsertPlan("plan-a", "intent-a", steps, PlanStatus.COMPLETED),
            UpsertIntent(
                "intent-a",
                "turn-1",
                "尝试路径 A",
                1,
                None,
                IntentStatus.FAILED,
            ),
            UpsertIntent(
                "intent-b",
                "turn-1",
                "尝试路径 B",
                2,
                None,
                IntentStatus.RUNNING,
            ),
            UpsertPlan("plan-b", "intent-b", steps, PlanStatus.RUNNING),
            AppendFailureSummary(
                FailureType.TARGET_MISMATCH,
                "命令成功但未满足当前轮目标",
                "execution-a",
            ),
        ),
    )
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=5,
        operation="append_plan_validation_record",
        changes=(
            AppendPlanValidationRecord(
                PlanValidationRecordDraft(
                    plan_validation_id="validation-b",
                    plan_id="plan-b",
                    task_run_id=task_run_id,
                    step_index=0,
                    attempt=1,
                    status=PlanValidationStatus.SUCCEEDED,
                    failure_type=None,
                    short_reason="command generated",
                    command_ref="command-b",
                )
            ),
        ),
    )
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=6,
        operation="create_execution",
        changes=(
            UpsertExecution(
                "execution-b",
                "plan-b",
                0,
                "command-b",
                ExecutionStatus.RUNNING,
                None,
            ),
        ),
    )
    commit_changes(
        store,
        request_value=request_value,
        task_run_id=task_run_id,
        base_checkpoint_sequence=7,
        operation="verify_fact",
        changes=(
            UpsertExecution(
                "execution-b",
                "plan-b",
                0,
                "command-b",
                ExecutionStatus.SUCCEEDED,
                "output-b",
            ),
            UpsertPlan("plan-b", "intent-b", steps, PlanStatus.COMPLETED),
            UpsertIntent(
                "intent-b",
                "turn-1",
                "尝试路径 B",
                2,
                None,
                IntentStatus.SUCCEEDED,
            ),
            UpsertTurn(
                "turn-1",
                task_run_id,
                1,
                "确认入口",
                ("intent-a", "intent-b"),
                TurnStatus.COMPLETED,
            ),
            UpsertFact(
                "fact-1",
                "turn-1",
                "发现可访问的入口",
                "execution-b",
                FactStatus.VERIFIED,
            ),
        ),
    )
    evidence = FakeExecutionEvidence(
        command_refs={"command-a", "command-b"},
        output_refs={"output-a", "output-b"},
    )
    return task_run_id, evidence


def test_loads_immutable_ordered_state_and_model_context(
    database_path: Path,
) -> None:
    with SqliteControlStore.open(database_path, clock=FakeStorageClock()) as store:
        task_run_id, evidence = persist_backtracking_state(store)
        repository = ControlStateRepository(store, evidence)

        task_run = repository.load_task_run(task_run_id)
        state = repository.load_control_state(task_run_id)
        context = repository.query_model_context(task_run_id)

        assert task_run is not None
        assert task_run.request.model_backend is None
        assert task_run.effective_model_backend is ModelBackend.CODEX_CLI
        assert state is not None
        assert state.latest_checkpoint.sequence == 8
        assert [item.intent_id for item in state.intents] == ["intent-a", "intent-b"]
        assert [item.plan_id for item in state.plans] == ["plan-a", "plan-b"]
        assert [item.execution_id for item in state.executions] == [
            "execution-a",
            "execution-b",
        ]
        assert state.failure_summaries[0].created_event_sequence == 5
        assert state.failure_summaries[0].created_event_ordinal == 5
        assert state.facts[0].created_event_sequence == 8
        assert state.facts[0].created_event_ordinal == 4
        assert [item.plan_validation_id for item in state.plan_validation_records] == [
            "validation-a",
            "validation-b",
        ]
        assert [item.content for item in context.facts] == ["发现可访问的入口"]
        assert [item.failure_type for item in context.failure_summaries] == [
            FailureType.TARGET_MISMATCH
        ]


def test_load_rejects_invalid_reference_without_misreporting_port_failure(
    database_path: Path,
) -> None:
    with SqliteControlStore.open(database_path, clock=FakeStorageClock()) as store:
        task_run_id, evidence = persist_backtracking_state(store)
        evidence.command_refs.remove("command-b")

        with pytest.raises(StorageError, match="invalid command_ref"):
            ControlStateRepository(store, evidence).load_control_state(task_run_id)

        evidence.failure = RuntimeError("artifact service offline")
        with pytest.raises(StorageError, match="validation is unavailable"):
            ControlStateRepository(store, evidence).load_control_state(task_run_id)


def test_load_plan_validation_record_and_build_committed_result(
    database_path: Path,
) -> None:
    clock = FakeStorageClock()
    with SqliteControlStore.open(database_path, clock=clock) as store:
        task_run_id, _ = persist_backtracking_state(store)
        draft = PlanValidationRecordDraft(
            plan_validation_id="validation-plan-level",
            plan_id="plan-b",
            task_run_id=task_run_id,
            step_index=None,
            attempt=1,
            status=PlanValidationStatus.SUCCEEDED,
            failure_type=None,
            short_reason="plan validated",
            command_ref=None,
        )
        commit = commit_changes(
            store,
            request_value=request(),
            task_run_id=task_run_id,
            base_checkpoint_sequence=8,
            operation="append_plan_validation_record",
            changes=(AppendPlanValidationRecord(draft),),
        )

        committed = CommittedPlanValidation.from_store(
            store,
            task_run_id=task_run_id,
            plan_validation_id=draft.plan_validation_id,
            commit=commit,
        )

        assert committed.record.plan_validation_id == draft.plan_validation_id
        assert committed.record.observed_at == clock.now
        assert committed.commit == commit
        with pytest.raises(StorageError, match="does not exist"):
            store.load_plan_validation_record(task_run_id, "missing")
        with pytest.raises(StorageError, match="does not exist"):
            store.load_plan_validation_record("another-task", draft.plan_validation_id)


def test_missing_task_run_is_none_for_loads_but_an_error_for_context(
    database_path: Path,
) -> None:
    with SqliteControlStore.open(database_path, clock=FakeStorageClock()) as store:
        repository = ControlStateRepository(store, FakeExecutionEvidence())

        assert repository.load_task_run("missing") is None
        assert repository.load_control_state("missing") is None
        with pytest.raises(TaskRunNotFoundError):
            repository.query_model_context("missing")
