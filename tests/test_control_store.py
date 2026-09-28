from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
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
    TargetNetworkBinding,
    IntentStatus,
    UpsertIntent,
    UpsertTaskRun,
)
from agent_core.control_store import canonical_json, task_request_payload


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
    source_root: str | None = None,
    target_network: TargetNetworkBinding | None = None,
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
        source_root=source_root,
        target_network=target_network,
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


def seed_legacy_start(connection: sqlite3.Connection, mutation: ControlMutation) -> None:
    change = mutation.state_changes[0]
    assert isinstance(change, UpsertTaskRun)
    connection.execute(
        """
        INSERT INTO task_runs(
            task_run_id, request_json, goal, target_json, deadline_seconds,
            effective_model_backend, started_at, deadline_at, status, sandbox_id,
            confirmation_deadline, finished_at, failure_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            change.task_run_id,
            canonical_json(task_request_payload(change.request)),
            change.request.goal,
            canonical_json(change.request.target.to_dict()),
            change.request.deadline_seconds,
            change.effective_model_backend.value,
            change.started_at,
            change.deadline_at,
            change.status.value,
            change.sandbox_id,
            None,
            None,
            None,
        ),
    )
    connection.execute(
        """
        INSERT INTO runtime_events(
            task_run_id, sequence, type, timestamp, status, details_json
        ) VALUES (?, 1, ?, ?, ?, ?)
        """,
        (
            mutation.task_run_id,
            mutation.event.type,
            mutation.event.timestamp,
            mutation.event.status.value,
            canonical_json(dict(mutation.event.details)),
        ),
    )
    connection.execute(
        """
        INSERT INTO checkpoints(
            task_run_id, sequence, watermark_event_sequence, created_at
        ) VALUES (?, 1, 1, ?)
        """,
        (mutation.task_run_id, mutation.event.timestamp),
    )
    connection.execute(
        """
        INSERT INTO control_mutations(
            task_run_id, mutation_id, request_fingerprint,
            event_sequence, checkpoint_sequence, committed_at
        ) VALUES (?, ?, ?, 1, 1, ?)
        """,
        (
            mutation.task_run_id,
            mutation.mutation_id,
            mutation.request_fingerprint,
            mutation.event.timestamp,
        ),
    )


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
    with open_store(database_path, FakeUtcClock()):
        assert database_path.is_file()
        connection = sqlite3.connect(database_path)
        try:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute(
                "SELECT MAX(version) FROM schema_versions"
            ).fetchone()[0] == 10
            assert connection.execute(
                "SELECT COUNT(*) FROM intent_goals"
            ).fetchone()[0] == 0
        finally:
            connection.close()


def test_schema_1_upgrade_preserves_committed_control_state(
    database_path: Path,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(sqlite_store_module.MIGRATION_1)
    connection.execute(
        "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (1, clock.now),
    )
    mutation = make_start_mutation()
    seed_legacy_start(connection, mutation)
    old_task_row = tuple(connection.execute("SELECT * FROM task_runs").fetchone())
    old_event_row = tuple(connection.execute("SELECT * FROM runtime_events").fetchone())
    old_checkpoint_row = tuple(connection.execute("SELECT * FROM checkpoints").fetchone())
    connection.close()

    with open_store(database_path, clock) as upgraded:
        state = upgraded.load_control_state_rows("task-run-1")
        assert state is not None
        assert state.task_run.source_root is None
        assert state.task_run.target_network is None
        assert state.task_run.request.goal == mutation.request.goal
        assert upgraded.commit(mutation).replayed is True
        verification = sqlite3.connect(database_path)
        try:
            assert tuple(verification.execute("SELECT * FROM task_runs").fetchone())[
                : len(old_task_row)
            ] == old_task_row
            assert tuple(
                verification.execute("SELECT * FROM runtime_events").fetchone()
            ) == old_event_row
            assert tuple(
                verification.execute("SELECT * FROM checkpoints").fetchone()
            ) == old_checkpoint_row
            assert verification.execute(
                "SELECT version FROM schema_versions ORDER BY version"
            ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]
            assert verification.execute(
                "SELECT COUNT(*) FROM intent_goals"
            ).fetchone()[0] == 0
            assert verification.execute(
                "SELECT source_root FROM task_runs WHERE task_run_id = ?",
                ("task-run-1",),
            ).fetchone() == (None,)
        finally:
            verification.close()


def test_schema_2_upgrade_adds_nullable_source_root_without_losing_state(
    database_path: Path,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(sqlite_store_module.MIGRATION_1)
    connection.execute(
        "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (1, clock.now),
    )
    connection.executescript(sqlite_store_module.MIGRATION_2)
    connection.execute(
        "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (2, clock.now),
    )
    mutation = make_start_mutation()
    seed_legacy_start(connection, mutation)
    old_task_row = tuple(connection.execute("SELECT * FROM task_runs").fetchone())
    connection.close()

    with open_store(database_path, clock) as upgraded:
        state = upgraded.load_control_state_rows("task-run-1")
        assert state is not None
        assert state.task_run.source_root is None
        verification = sqlite3.connect(database_path)
        try:
            assert tuple(verification.execute("SELECT * FROM task_runs").fetchone())[
                : len(old_task_row)
            ] == old_task_row
            assert verification.execute(
                "SELECT version FROM schema_versions ORDER BY version"
            ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]
            assert verification.execute(
                "SELECT source_root FROM task_runs WHERE task_run_id = ?",
                ("task-run-1",),
            ).fetchone() == (None,)
        finally:
            verification.close()


def test_schema_2_upgrade_failure_rolls_back_without_changing_schema_1(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(sqlite_store_module.MIGRATION_1)
    connection.execute(
        "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (1, clock.now),
    )
    mutation = make_start_mutation()
    seed_legacy_start(connection, mutation)
    connection.close()

    monkeypatch.setattr(
        sqlite_store_module,
        "MIGRATION_2",
        "CREATE TABLE intent_goals (intent_id TEXT PRIMARY KEY);\n"
        "CREATE TABLE broken_upgrade (value NOT_A_VALID_DEFINITION,);",
    )
    with pytest.raises(StorageError, match="SQLite schema migration failed"), open_store(
        database_path, clock
    ):
        pass

    verification = sqlite3.connect(database_path)
    try:
        assert verification.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall() == [(1,)]
        assert verification.execute(
            "SELECT name FROM sqlite_master WHERE name = 'intent_goals'"
        ).fetchone() is None
    finally:
        verification.close()

    monkeypatch.undo()
    with open_store(database_path, clock) as upgraded:
        state = upgraded.load_control_state_rows("task-run-1")
        assert state is not None
        assert state.task_run.source_root is None


def test_schema_3_upgrade_failure_rolls_back_without_changing_schema_2(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(sqlite_store_module.MIGRATION_1)
    connection.execute(
        "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (1, clock.now),
    )
    connection.executescript(sqlite_store_module.MIGRATION_2)
    connection.execute(
        "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
        (2, clock.now),
    )
    mutation = make_start_mutation()
    seed_legacy_start(connection, mutation)
    connection.close()

    monkeypatch.setattr(
        sqlite_store_module,
        "MIGRATION_3",
        "ALTER TABLE task_runs ADD COLUMN source_root TEXT;\n"
        "CREATE TABLE broken_upgrade (value NOT_A_VALID_DEFINITION,);",
    )
    with pytest.raises(StorageError, match="SQLite schema migration failed"), open_store(
        database_path, clock
    ):
        pass

    verification = sqlite3.connect(database_path)
    try:
        assert verification.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall() == [(1,), (2,)]
        assert "source_root" not in {
            row[1] for row in verification.execute("PRAGMA table_info(task_runs)")
        }
        assert verification.execute("SELECT COUNT(*) FROM task_runs").fetchone() == (1,)
    finally:
        verification.close()

    monkeypatch.undo()
    with open_store(database_path, clock) as upgraded:
        assert upgraded.commit(mutation).replayed is True
        assert upgraded.load_task_run("task-run-1").source_root is None


def test_schema_3_upgrade_preserves_legacy_fact_without_verification_ref(
    database_path: Path,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    for version, migration in enumerate(
        (
            sqlite_store_module.MIGRATION_1,
            sqlite_store_module.MIGRATION_2,
            sqlite_store_module.MIGRATION_3,
        ),
        start=1,
    ):
        connection.executescript(migration)
        connection.execute(
            "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
            (version, clock.now),
        )
    mutation = make_start_mutation()
    seed_legacy_start(connection, mutation)
    connection.execute(
        "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?)",
        ("turn-1", "task-run-1", 1, "inspect", '["intent-1"]', "running"),
    )
    connection.execute(
        "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?)",
        ("intent-1", "turn-1", "inspect", 1, "file_type", "running"),
    )
    connection.execute(
        "INSERT INTO plans VALUES (?, ?, ?)", ("plan-1", "intent-1", "completed")
    )
    connection.execute(
        "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?)",
        ("execution-1", "plan-1", 0, "command-1", "succeeded", "output-1"),
    )
    connection.execute(
        "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("fact-1", "turn-1", "legacy fact", "execution-1", "verified", 1, 0),
    )
    connection.close()

    with open_store(database_path, clock) as upgraded:
        state = upgraded.load_control_state_rows("task-run-1")
        assert state is not None
        assert state.facts[0].verification_ref is None
        assert state.facts[0].to_domain().verification_ref is None
        assert upgraded.commit(mutation).replayed is True

    verification = sqlite3.connect(database_path)
    try:
        assert verification.execute(
            "SELECT content, status, verification_ref FROM facts WHERE fact_id = ?",
            ("fact-1",),
        ).fetchone() == ("legacy fact", "verified", None)
        assert verification.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,), (10,)]
    finally:
        verification.close()


def test_schema_8_upgrade_preserves_plan_children_and_allows_next_sequence(
    database_path: Path,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    with closing(sqlite3.connect(database_path, isolation_level=None)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version in range(1, 9):
            connection.executescript(getattr(sqlite_store_module, f"MIGRATION_{version}"))
            connection.execute(
                "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                (version, clock.now),
            )
        seed_legacy_start(connection, make_start_mutation())
        connection.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?)",
            ("turn-1", "task-run-1", 1, "inspect", '["intent-1"]', "running"),
        )
        connection.execute(
            "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?)",
            ("intent-1", "turn-1", "inspect", 1, None, "running"),
        )
        connection.execute(
            "INSERT INTO plans VALUES (?, ?, ?)",
            ("plan-1", "intent-1", "completed"),
        )
        connection.execute(
            "INSERT INTO plan_steps VALUES (?, ?, ?)",
            ("plan-1", 0, "probe"),
        )
        connection.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?)",
            ("execution-1", "plan-1", 0, "command-1", "succeeded", "output-1"),
        )
        connection.execute(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("fact-1", "turn-1", "observation", "execution-1", "verified", 1, 0, None),
        )

    with open_store(database_path, clock) as upgraded:
        state = upgraded.load_control_state_rows("task-run-1")
        assert state is not None
        assert state.plans[0].plan_id == "plan-1"
        assert state.executions[0].plan_id == "plan-1"
        assert state.facts[0].source_ref == "execution-1"
        with closing(sqlite3.connect(database_path, isolation_level=None)) as verification:
            verification.execute("PRAGMA foreign_keys = ON")
            assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
            assert verification.execute(
                "SELECT sequence FROM plans WHERE plan_id = 'plan-1'"
            ).fetchone() == (1,)
            verification.execute(
                "INSERT INTO plans(plan_id, intent_id, sequence, status) VALUES (?, ?, ?, ?)",
                ("plan-2", "intent-1", 2, "pending"),
            )


def test_schema_4_upgrade_failure_preserves_schema_3(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.sqlite_control_store as sqlite_store_module

    clock = FakeUtcClock()
    connection = sqlite3.connect(database_path, isolation_level=None)
    for version, migration in enumerate(
        (
            sqlite_store_module.MIGRATION_1,
            sqlite_store_module.MIGRATION_2,
            sqlite_store_module.MIGRATION_3,
        ),
        start=1,
    ):
        connection.executescript(migration)
        connection.execute(
            "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
            (version, clock.now),
        )
    connection.close()

    monkeypatch.setattr(
        sqlite_store_module,
        "MIGRATION_4",
        "ALTER TABLE facts ADD COLUMN verification_ref TEXT;\n"
        "CREATE TABLE invalid_upgrade (value NOT_A_VALID_DEFINITION,);",
    )
    with pytest.raises(StorageError, match="SQLite schema migration failed"):
        open_store(database_path, clock)

    verification = sqlite3.connect(database_path)
    try:
        assert verification.execute(
            "SELECT version FROM schema_versions ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
        assert "verification_ref" not in {
            row[1] for row in verification.execute("PRAGMA table_info(facts)")
        }
    finally:
        verification.close()


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


def test_committed_source_root_cannot_be_replaced(
    database_path: Path,
) -> None:
    with open_store(database_path, FakeUtcClock()) as store:
        store.commit(make_start_mutation(source_root="C:\\source"))
        invalid = make_start_mutation(
            mutation_id=str(uuid.uuid4()),
            base_checkpoint_sequence=1,
            source_root="C:\\other",
        )
        with pytest.raises(StorageError, match="immutable TaskRun"):
            store.commit(invalid)
        assert store.load_task_run("task-run-1").source_root == "C:\\source"


def test_target_network_binding_commits_with_task_and_is_immutable(
    database_path: Path,
) -> None:
    binding = TargetNetworkBinding(
        private_network_id="a" * 64,
        egress_network_id="b" * 64,
        forwarder_id="c" * 64,
        configuration_sha256="d" * 64,
        target_url="http://example.test:8080",
    )
    with open_store(database_path, FakeUtcClock()) as store:
        store.commit(make_start_mutation(target_network=binding))
        assert store.load_task_run("task-run-1").target_network == binding
        invalid = make_start_mutation(
            mutation_id=str(uuid.uuid4()),
            base_checkpoint_sequence=1,
            target_network=None,
        )
        with pytest.raises(StorageError, match="immutable TaskRun"):
            store.commit(invalid)
        assert store.load_task_run("task-run-1").target_network == binding

    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT target_network_json FROM task_runs WHERE task_run_id=?",
            ("task-run-1",),
        ).fetchone()
        assert row is not None and row[0] == canonical_json(binding.to_dict())
    finally:
        connection.close()
