from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_core import (
    CONTROL_OPERATION_RULES,
    CommittedOperation,
    ControlMutation,
    IdempotencyConflictError,
    ModelBackend,
    RuntimeEvent,
    SqliteControlStore,
    StaleControlStateError,
    StorageError,
    TaskRequest,
    TaskRunStatus,
    IntentStatus,
    UpsertIntent,
    UpsertTaskRun,
)


@dataclass
class FakeUtcClock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def task_request() -> TaskRequest:
    return TaskRequest.from_dict(
        {
            "goal": "找到 flag",
            "target": {
                "endpoints": [{"url": "http://example.test:8080"}],
                "attachments": [
                    {"kind": "local_path", "path": "attachments/a.bin"}
                ],
            },
            "deadline_seconds": 3600,
            "model_backend": "codex_cli",
        }
    )


def make_start_mutation(
    *,
    task_run_id: str = "task-run-1",
    mutation_id: str | None = None,
    base_checkpoint_sequence: int = 0,
    sandbox_id: str = "sandbox-1",
    started_at: float = 1_700_000_000.0,
    event_timestamp: float = 1_700_000_000.0,
    operation: str = "start_task_run",
    event_type: str = "task_run_started",
    event_details: dict[str, object] | None = None,
) -> ControlMutation:
    request = task_request()
    task_run = UpsertTaskRun(
        task_run_id=task_run_id,
        request=request,
        effective_model_backend=ModelBackend.CODEX_CLI,
        started_at=started_at,
        deadline_at=started_at + request.deadline_seconds,
        status=TaskRunStatus.RUNNING,
        sandbox_id=sandbox_id,
    )
    return ControlMutation.create(
        task_run_id=task_run_id,
        mutation_id=mutation_id or str(uuid.uuid4()),
        operation=operation,
        request=request,
        effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=base_checkpoint_sequence,
        state_changes=(task_run,),
        event=RuntimeEvent(
            type=event_type,
            task_run_id=task_run_id,
            timestamp=event_timestamp,
            status=TaskRunStatus.RUNNING,
            details=(
                event_details
                if event_details is not None
                else {
                    "operation": operation,
                    "entity_ids": [task_run_id],
                    "result": task_run_id,
                }
            ),
        ),
    )


def open_store(path: Path, clock: FakeUtcClock) -> SqliteControlStore:
    return SqliteControlStore.open(path, clock=clock)


@pytest.fixture
def database_path() -> Path:
    runtime_directory = Path(__file__).resolve().parents[1] / ".test" / "runtime"
    runtime_directory.mkdir(parents=True, exist_ok=True)
    path = runtime_directory / f"control-{uuid.uuid4()}.sqlite3"
    yield path
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def test_open_creates_parent_and_applies_confirmed_pragmas(
    database_path: Path,
) -> None:
    with open_store(database_path, FakeUtcClock()) as store:
        assert database_path.is_file()
        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute(
                "SELECT MAX(version) FROM schema_versions"
            ).fetchone()[0] == 1
        finally:
            connection.close()


def test_initial_commit_is_atomic_and_returns_database_sequences(
    database_path: Path,
) -> None:
    clock = FakeUtcClock()
    with open_store(database_path, clock) as store:
        mutation = make_start_mutation()

        result = store.commit(mutation)

        assert result.replayed is False
        assert result.event.sequence == 1
        assert result.event.timestamp == mutation.event.timestamp
        assert result.checkpoint.sequence == 1
        assert result.checkpoint.watermark_event_sequence == 1
        assert result.checkpoint.created_at == clock.now
        assert CommittedOperation.from_commit(result).value == "task-run-1"


def test_replay_ignores_generated_times_and_returns_first_commit(
    database_path: Path,
) -> None:
    clock = FakeUtcClock()
    mutation_id = str(uuid.uuid4())
    with open_store(database_path, clock) as store:
        original = make_start_mutation(mutation_id=mutation_id)
        first = store.commit(original)
        clock.now += 100
        replay = make_start_mutation(
            mutation_id=mutation_id,
            started_at=1_700_001_000.0,
            event_timestamp=1_700_001_000.0,
        )

        second = store.commit(replay)

        assert replay.request_fingerprint == original.request_fingerprint
        assert second.replayed is True
        assert second.event == first.event
        assert second.checkpoint == first.checkpoint
        assert CommittedOperation.from_commit(second).value == "task-run-1"


def test_operation_registry_contains_only_confirmed_complete_execution_result() -> None:
    assert CONTROL_OPERATION_RULES["complete_execution"].allowed_results == {
        "succeeded"
    }
    assert "try_next_intent" in CONTROL_OPERATION_RULES[
        "record_execution_failure"
    ].allowed_results


@pytest.mark.parametrize(
    ("operation", "event_type", "details", "message"),
    [
        (
            "unknown_operation",
            "control_state_committed",
            {
                "operation": "unknown_operation",
                "entity_ids": ["task-run-1"],
                "result": "task-run-1",
            },
            "unsupported ControlMutation.operation",
        ),
        (
            "start_task_run",
            "control_state_committed",
            {
                "operation": "start_task_run",
                "entity_ids": ["task-run-1"],
                "result": "task-run-1",
            },
            "requires RuntimeEvent.type=task_run_started",
        ),
        (
            "start_task_run",
            "task_run_started",
            {
                "operation": "register_turn",
                "entity_ids": ["task-run-1"],
                "result": "task-run-1",
            },
            "must match RuntimeEvent.details.operation",
        ),
        (
            "start_task_run",
            "task_run_started",
            {
                "operation": "start_task_run",
                "entity_ids": ["task-run-1", "task-run-1"],
                "result": "task-run-1",
            },
            "entity_ids must be unique",
        ),
        (
            "start_task_run",
            "task_run_started",
            {
                "operation": "start_task_run",
                "entity_ids": ["task-run-1"],
                "result": "task-run-1",
                "provider": "codex_cli",
            },
            "unsupported fields",
        ),
        (
            "start_task_run",
            "task_run_started",
            {
                "operation": "start_task_run",
                "entity_ids": ["task-run-1"],
            },
            "requires RuntimeEvent.details.result",
        ),
        (
            "start_task_run",
            "task_run_started",
            {
                "operation": "start_task_run",
                "entity_ids": ["task-run-1", "task-run-other"],
                "result": "task-run-other",
            },
            "does not match its typed state_changes",
        ),
        (
            "complete_execution",
            "control_state_committed",
            {
                "operation": "complete_execution",
                "entity_ids": ["task-run-1"],
                "result": "failed",
            },
            "does not allow result=failed",
        ),
    ],
)
def test_control_event_contract_rejects_invalid_combinations(
    operation: str,
    event_type: str,
    details: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        make_start_mutation(
            operation=operation,
            event_type=event_type,
            event_details=details,
        )


def test_select_next_intent_accepts_running_intent_or_omitted_result() -> None:
    request = task_request()
    running_intent = UpsertIntent(
        intent_id="intent-1",
        turn_id="turn-1",
        description="try path",
        priority=1,
        intent_type=None,
        status=IntentStatus.RUNNING,
    )

    selected = ControlMutation.create(
        task_run_id="task-run-1",
        mutation_id=str(uuid.uuid4()),
        operation="select_next_intent",
        request=request,
        effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=0,
        state_changes=(running_intent,),
        event=RuntimeEvent(
            type="control_state_committed",
            task_run_id="task-run-1",
            timestamp=1_700_000_000.0,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "select_next_intent",
                "entity_ids": ["intent-1"],
                "result": "intent-1",
            },
        ),
    )
    omitted = ControlMutation.create(
        task_run_id="task-run-1",
        mutation_id=str(uuid.uuid4()),
        operation="select_next_intent",
        request=request,
        effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=0,
        state_changes=(running_intent,),
        event=RuntimeEvent(
            type="control_state_committed",
            task_run_id="task-run-1",
            timestamp=1_700_000_000.0,
            status=TaskRunStatus.RUNNING,
            details={
                "operation": "select_next_intent",
                "entity_ids": ["intent-1"],
            },
        ),
    )

    assert selected.event.details["result"] == "intent-1"
    assert "result" not in omitted.event.details


def test_select_next_intent_rejects_non_running_result() -> None:
    request = task_request()
    pending_intent = UpsertIntent(
        intent_id="intent-1",
        turn_id="turn-1",
        description="try path",
        priority=1,
        intent_type=None,
        status=IntentStatus.PENDING,
    )

    with pytest.raises(ValueError, match="does not match its typed state_changes"):
        ControlMutation.create(
            task_run_id="task-run-1",
            mutation_id=str(uuid.uuid4()),
            operation="select_next_intent",
            request=request,
            effective_model_backend=ModelBackend.CODEX_CLI,
            base_checkpoint_sequence=0,
            state_changes=(pending_intent,),
            event=RuntimeEvent(
                type="control_state_committed",
                task_run_id="task-run-1",
                timestamp=1_700_000_000.0,
                status=TaskRunStatus.RUNNING,
                details={
                    "operation": "select_next_intent",
                    "entity_ids": ["intent-1"],
                    "result": "intent-1",
                },
            ),
        )


def test_replay_rejects_a_persisted_event_that_violates_the_registry(
    database_path: Path,
) -> None:
    mutation_id = str(uuid.uuid4())
    original = make_start_mutation(mutation_id=mutation_id)
    with open_store(database_path, FakeUtcClock()) as store:
        store.commit(original)

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            UPDATE runtime_events
            SET details_json = ?
            WHERE task_run_id = ? AND sequence = 1
            """,
            (
                '{"entity_ids":["task-run-1","task-run-other"],'
                '"operation":"start_task_run",'
                '"result":"task-run-other"}',
                "task-run-1",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with open_store(database_path, FakeUtcClock()) as store:
        with pytest.raises(StorageError, match="typed state_changes"):
            store.commit(make_start_mutation(mutation_id=mutation_id))


@pytest.mark.parametrize(
    "change",
    ["base_checkpoint_sequence", "sandbox_id"],
)
def test_same_mutation_id_with_changed_business_content_conflicts(
    database_path: Path,
    change: str,
) -> None:
    mutation_id = str(uuid.uuid4())
    with open_store(database_path, FakeUtcClock()) as store:
        store.commit(make_start_mutation(mutation_id=mutation_id))
        kwargs: dict[str, object] = {"mutation_id": mutation_id}
        if change == "base_checkpoint_sequence":
            kwargs[change] = 1
        else:
            kwargs[change] = "sandbox-other"

        with pytest.raises(IdempotencyConflictError):
            store.commit(make_start_mutation(**kwargs))


def test_new_mutation_from_stale_checkpoint_writes_nothing(
    database_path: Path,
) -> None:
    with open_store(database_path, FakeUtcClock()) as store:
        store.commit(make_start_mutation())

        with pytest.raises(StaleControlStateError):
            store.commit(make_start_mutation(mutation_id=str(uuid.uuid4())))

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM control_mutations").fetchone()[0] == 1
    finally:
        connection.close()


def test_failed_state_change_rolls_back_event_checkpoint_and_mutation(
    database_path: Path,
) -> None:
    with open_store(database_path, FakeUtcClock()) as store:
        store.commit(make_start_mutation())
        invalid = make_start_mutation(
            mutation_id=str(uuid.uuid4()),
            base_checkpoint_sequence=1,
            sandbox_id="sandbox-other",
        )

        with pytest.raises(StorageError, match="immutable TaskRun"):
            store.commit(invalid)

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM control_mutations").fetchone()[0] == 1
    finally:
        connection.close()
