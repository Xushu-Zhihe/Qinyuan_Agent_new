from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from agent_core.control_store import (
    AppendFailureSummary,
    AppendPlanValidationRecord,
    Checkpoint,
    ControlCommitResult,
    ControlMutation,
    ControlStateRows,
    ExecutionSnapshot,
    FactRecordSnapshot,
    FailureSummaryRecordSnapshot,
    IdempotencyConflictError,
    IntentSnapshot,
    PlanSnapshot,
    StaleControlStateError,
    StateChange,
    StorageError,
    TaskRunSnapshot,
    TurnSnapshot,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
    canonical_json,
    control_mutation_fingerprint,
    task_request_payload,
    validate_control_event,
)
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_candidate import validate_control_ownership, validate_running_transition
from agent_core.domain import (
    ExecutionStatus,
    FactStatus,
    FailureType,
    IntentStatus,
    PlanStatus,
    PlanStep,
    PlanValidationRecord,
    PlanValidationStatus,
    TurnStatus,
)
from agent_core.runtime import RuntimeEvent, TaskRunStatus


CURRENT_SCHEMA_VERSION = 1


MIGRATION_1 = """
CREATE TABLE schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE task_runs (
    task_run_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    goal TEXT NOT NULL,
    target_json TEXT NOT NULL,
    deadline_seconds INTEGER NOT NULL,
    effective_model_backend TEXT NOT NULL,
    started_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    status TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    confirmation_deadline REAL,
    finished_at REAL,
    failure_reason TEXT
);

CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    sequence INTEGER NOT NULL,
    turn_target TEXT NOT NULL,
    intent_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(task_run_id, sequence)
);

CREATE TABLE intents (
    intent_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    description TEXT NOT NULL,
    priority INTEGER NOT NULL,
    intent_type TEXT,
    status TEXT NOT NULL,
    UNIQUE(turn_id, priority)
);

CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
    status TEXT NOT NULL
);

CREATE TABLE plan_steps (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    step_index INTEGER NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY(plan_id, step_index)
);

CREATE TABLE executions (
    execution_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    step_index INTEGER NOT NULL,
    command_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    output_ref TEXT
);

CREATE TABLE facts (
    fact_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    content TEXT NOT NULL,
    source_ref TEXT NOT NULL REFERENCES executions(execution_id),
    status TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL,
    created_event_ordinal INTEGER NOT NULL
);

CREATE TABLE plan_validation_records (
    plan_validation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    step_index INTEGER,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    failure_type TEXT,
    short_reason TEXT NOT NULL,
    command_ref TEXT,
    observed_at REAL NOT NULL
);

CREATE UNIQUE INDEX uq_plan_validation_scope_attempt
ON plan_validation_records(plan_id, COALESCE(step_index, -1), attempt);

CREATE TABLE failure_summaries (
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    failure_type TEXT NOT NULL,
    short_reason TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL,
    created_event_ordinal INTEGER NOT NULL,
    PRIMARY KEY(task_run_id, created_event_sequence, created_event_ordinal)
);

CREATE TABLE runtime_events (
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY(task_run_id, sequence)
);

CREATE TABLE checkpoints (
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    sequence INTEGER NOT NULL,
    watermark_event_sequence INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(task_run_id, sequence)
);

CREATE TABLE control_mutations (
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    mutation_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    checkpoint_sequence INTEGER NOT NULL,
    committed_at REAL NOT NULL,
    PRIMARY KEY(task_run_id, mutation_id)
);

CREATE INDEX idx_facts_context
ON facts(turn_id, status, created_event_sequence DESC, created_event_ordinal DESC);

CREATE INDEX idx_failure_summaries_context
ON failure_summaries(
    task_run_id,
    failure_type,
    created_event_sequence DESC,
    created_event_ordinal DESC
);
"""


class SqliteControlStore:
    """SQLite implementation of the control-plane transaction boundary."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        path: Path,
        *,
        clock: Callable[[], float],
    ) -> None:
        self._connection = connection
        self.path = path
        self._clock = clock

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
    ) -> "SqliteControlStore":
        database_path = Path(path)
        try:
            if database_path.exists() and not database_path.is_file():
                raise StorageError("SQLite database path must be a file")
            database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                database_path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            store = cls(connection, database_path, clock=clock)
            store._migrate()
            return store
        except StorageError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise StorageError(f"could not open SQLite control store: {exc}") from exc

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as exc:
            raise StorageError(f"could not close SQLite control store: {exc}") from exc

    def __enter__(self) -> "SqliteControlStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            table_exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_versions'"
            ).fetchone()
            version = 0
            if table_exists:
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_versions"
                ).fetchone()
                assert row is not None
                version = int(row["version"])
            if version > CURRENT_SCHEMA_VERSION:
                raise StorageError(
                    f"database schema version {version} is newer than supported version "
                    f"{CURRENT_SCHEMA_VERSION}"
                )
            if version < 1:
                for statement in _sql_statements(MIGRATION_1):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (1, self._now()),
                )
            self._connection.commit()
        except StorageError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise StorageError(f"SQLite schema migration failed: {exc}") from exc

    def commit(self, mutation: ControlMutation) -> ControlCommitResult:
        expected_fingerprint = control_mutation_fingerprint(mutation)
        if mutation.request_fingerprint != expected_fingerprint:
            raise IdempotencyConflictError(
                "ControlMutation request_fingerprint does not match its content"
            )

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            previous = self._connection.execute(
                """
                SELECT request_fingerprint, event_sequence, checkpoint_sequence
                FROM control_mutations
                WHERE task_run_id = ? AND mutation_id = ?
                """,
                (mutation.task_run_id, mutation.mutation_id),
            ).fetchone()
            if previous is not None:
                if previous["request_fingerprint"] != mutation.request_fingerprint:
                    raise IdempotencyConflictError(
                        "mutation_id was already committed with different content"
                    )
                result = self._load_commit_result(
                    mutation.task_run_id,
                    int(previous["event_sequence"]),
                    int(previous["checkpoint_sequence"]),
                    replayed=True,
                )
                validate_control_event(
                    mutation.operation,
                    mutation.state_changes,
                    result.event,
                )
                self._connection.commit()
                return result

            current_checkpoint = self._connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) AS sequence
                FROM checkpoints
                WHERE task_run_id = ?
                """,
                (mutation.task_run_id,),
            ).fetchone()
            assert current_checkpoint is not None
            current_sequence = int(current_checkpoint["sequence"])
            if current_sequence != mutation.base_checkpoint_sequence:
                raise StaleControlStateError(
                    "base_checkpoint_sequence does not match current committed state"
                )

            event_sequence = self._next_event_sequence(mutation.task_run_id)
            committed_at = self._now()
            before = self.load_control_state_rows(mutation.task_run_id)
            self._validate_existing_owners(mutation)
            self._apply_state_changes(
                mutation,
                event_sequence=event_sequence,
                committed_at=committed_at,
            )
            self._insert_event(mutation.event, event_sequence)
            checkpoint = Checkpoint(
                task_run_id=mutation.task_run_id,
                sequence=mutation.checkpoint.sequence,
                watermark_event_sequence=event_sequence,
                created_at=committed_at,
            )
            self._connection.execute(
                """
                INSERT INTO checkpoints(
                    task_run_id, sequence, watermark_event_sequence, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    checkpoint.task_run_id,
                    checkpoint.sequence,
                    checkpoint.watermark_event_sequence,
                    checkpoint.created_at,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO control_mutations(
                    task_run_id, mutation_id, request_fingerprint,
                    event_sequence, checkpoint_sequence, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.task_run_id,
                    mutation.mutation_id,
                    mutation.request_fingerprint,
                    event_sequence,
                    checkpoint.sequence,
                    committed_at,
                ),
            )
            # Pure structural checks while every row is still uncommitted;
            # also cover an initial mutation that includes child objects.
            after = self.load_control_state_rows(mutation.task_run_id)
            assert after is not None
            validate_control_ownership(after)
            validate_running_transition(before, after, mutation)
            self._connection.commit()
            return ControlCommitResult(
                event=_numbered_event(mutation.event, event_sequence),
                checkpoint=checkpoint,
                replayed=False,
            )
        except (IdempotencyConflictError, StaleControlStateError):
            self._rollback_quietly()
            raise
        except StorageError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise StorageError(f"SQLite control transaction failed: {exc}") from exc
        except Exception as exc:
            self._rollback_quietly()
            raise StorageError(f"control transaction failed: {exc}") from exc

    def load_task_run(self, task_run_id: str) -> TaskRunSnapshot | None:
        try:
            row = self._connection.execute(
                "SELECT * FROM task_runs WHERE task_run_id = ?",
                (task_run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._decode_task_run(row)
        except StorageError:
            raise
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError(f"could not load TaskRun state: {exc}") from exc

    def load_committed_mutation(
        self,
        task_run_id: str,
        mutation_id: str,
    ) -> ControlCommitResult | None:
        """Load the first commit result for one mutation without replaying it."""

        try:
            row = self._connection.execute(
                """
                SELECT event_sequence, checkpoint_sequence
                FROM control_mutations
                WHERE task_run_id = ? AND mutation_id = ?
                """,
                (task_run_id, mutation_id),
            ).fetchone()
            if row is None:
                return None
            return self._load_commit_result(
                task_run_id,
                int(row["event_sequence"]),
                int(row["checkpoint_sequence"]),
                replayed=True,
            )
        except StorageError:
            raise
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError(
                f"could not load committed mutation: {exc}"
            ) from exc

    def load_terminal_commit(self, task_run_id: str) -> ControlCommitResult | None:
        try:
            row = self._connection.execute(
                """
                SELECT m.event_sequence, m.checkpoint_sequence
                FROM control_mutations m
                JOIN runtime_events e
                  ON e.task_run_id = m.task_run_id AND e.sequence = m.event_sequence
                WHERE m.task_run_id = ? AND e.status NOT IN ('running', 'awaiting_confirmation')
                ORDER BY m.event_sequence LIMIT 1
                """,
                (task_run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._load_commit_result(
                task_run_id, int(row["event_sequence"]), int(row["checkpoint_sequence"]),
                replayed=True,
            )
        except StorageError:
            raise
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("could not load terminal commit") from exc

    def load_plan_validation_record(
        self,
        task_run_id: str,
        plan_validation_id: str,
    ) -> PlanValidationRecord:
        try:
            row = self._connection.execute(
                """
                SELECT * FROM plan_validation_records
                WHERE task_run_id = ? AND plan_validation_id = ?
                """,
                (task_run_id, plan_validation_id),
            ).fetchone()
            if row is None:
                raise StorageError(
                    "PlanValidationRecord does not exist in the specified TaskRun"
                )
            return self._decode_plan_validation_record(row)
        except StorageError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError(
                f"could not load PlanValidationRecord: {exc}"
            ) from exc

    def load_control_state_rows(
        self,
        task_run_id: str,
    ) -> ControlStateRows | None:
        task_run = self.load_task_run(task_run_id)
        if task_run is None:
            return None
        try:
            checkpoint_row = self._connection.execute(
                """
                SELECT sequence, watermark_event_sequence, created_at
                FROM checkpoints
                WHERE task_run_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (task_run_id,),
            ).fetchone()
            if checkpoint_row is None:
                raise StorageError("TaskRun is missing its required Checkpoint")
            checkpoint = Checkpoint(
                task_run_id=task_run_id,
                sequence=int(checkpoint_row["sequence"]),
                watermark_event_sequence=int(
                    checkpoint_row["watermark_event_sequence"]
                ),
                created_at=float(checkpoint_row["created_at"]),
            )
            turns = tuple(
                TurnSnapshot(
                    turn_id=row["turn_id"],
                    task_run_id=row["task_run_id"],
                    sequence=int(row["sequence"]),
                    turn_target=row["turn_target"],
                    intent_ids=tuple(json.loads(row["intent_ids_json"])),
                    status=TurnStatus(row["status"]),
                )
                for row in self._connection.execute(
                    """
                    SELECT * FROM turns
                    WHERE task_run_id = ?
                    ORDER BY sequence
                    """,
                    (task_run_id,),
                )
            )
            intents = tuple(
                IntentSnapshot(
                    intent_id=row["intent_id"],
                    turn_id=row["turn_id"],
                    description=row["description"],
                    priority=int(row["priority"]),
                    intent_type=row["intent_type"],
                    status=IntentStatus(row["status"]),
                )
                for row in self._connection.execute(
                    """
                    SELECT intents.*
                    FROM intents
                    JOIN turns ON turns.turn_id = intents.turn_id
                    WHERE turns.task_run_id = ?
                    ORDER BY turns.sequence, intents.priority
                    """,
                    (task_run_id,),
                )
            )
            plan_rows = self._connection.execute(
                """
                SELECT plans.*
                FROM plans
                JOIN intents ON intents.intent_id = plans.intent_id
                JOIN turns ON turns.turn_id = intents.turn_id
                WHERE turns.task_run_id = ?
                ORDER BY turns.sequence, intents.priority, plans.plan_id
                """,
                (task_run_id,),
            ).fetchall()
            plans = tuple(
                PlanSnapshot(
                    plan_id=row["plan_id"],
                    intent_id=row["intent_id"],
                    steps=self._load_plan_steps(row["plan_id"]),
                    status=PlanStatus(row["status"]),
                )
                for row in plan_rows
            )
            executions = tuple(
                ExecutionSnapshot(
                    execution_id=row["execution_id"],
                    plan_id=row["plan_id"],
                    step_index=int(row["step_index"]),
                    command_ref=row["command_ref"],
                    status=ExecutionStatus(row["status"]),
                    output_ref=row["output_ref"],
                )
                for row in self._connection.execute(
                    """
                    SELECT executions.*
                    FROM executions
                    JOIN plans ON plans.plan_id = executions.plan_id
                    JOIN intents ON intents.intent_id = plans.intent_id
                    JOIN turns ON turns.turn_id = intents.turn_id
                    WHERE turns.task_run_id = ?
                    ORDER BY turns.sequence, intents.priority, plans.plan_id,
                             executions.step_index, executions.execution_id
                    """,
                    (task_run_id,),
                )
            )
            facts = tuple(
                FactRecordSnapshot(
                    fact_id=row["fact_id"],
                    turn_id=row["turn_id"],
                    content=row["content"],
                    source_ref=row["source_ref"],
                    status=FactStatus(row["status"]),
                    created_event_sequence=int(row["created_event_sequence"]),
                    created_event_ordinal=int(row["created_event_ordinal"]),
                )
                for row in self._connection.execute(
                    """
                    SELECT facts.*
                    FROM facts
                    JOIN turns ON turns.turn_id = facts.turn_id
                    WHERE turns.task_run_id = ?
                    ORDER BY facts.created_event_sequence,
                             facts.created_event_ordinal
                    """,
                    (task_run_id,),
                )
            )
            failure_summaries = tuple(
                FailureSummaryRecordSnapshot(
                    failure_type=FailureType(row["failure_type"]),
                    short_reason=row["short_reason"],
                    evidence_ref=row["evidence_ref"],
                    created_event_sequence=int(row["created_event_sequence"]),
                    created_event_ordinal=int(row["created_event_ordinal"]),
                )
                for row in self._connection.execute(
                    """
                    SELECT * FROM failure_summaries
                    WHERE task_run_id = ?
                    ORDER BY created_event_sequence, created_event_ordinal
                    """,
                    (task_run_id,),
                )
            )
            plan_validation_records = tuple(
                self._decode_plan_validation_record(row)
                for row in self._connection.execute(
                    """
                    SELECT plan_validation_records.*
                    FROM plan_validation_records
                    JOIN plans ON plans.plan_id = plan_validation_records.plan_id
                    JOIN intents ON intents.intent_id = plans.intent_id
                    JOIN turns ON turns.turn_id = intents.turn_id
                    WHERE plan_validation_records.task_run_id = ?
                    ORDER BY turns.sequence, intents.priority, plans.plan_id,
                             COALESCE(plan_validation_records.step_index, -1),
                             plan_validation_records.attempt
                    """,
                    (task_run_id,),
                )
            )
            return ControlStateRows(
                task_run=task_run,
                latest_checkpoint=checkpoint,
                turns=turns,
                intents=intents,
                plans=plans,
                executions=executions,
                facts=facts,
                failure_summaries=failure_summaries,
                plan_validation_records=plan_validation_records,
            )
        except StorageError:
            raise
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError(f"could not load control state: {exc}") from exc

    @staticmethod
    def _decode_plan_validation_record(row: sqlite3.Row) -> PlanValidationRecord:
        return PlanValidationRecord(
            plan_validation_id=row["plan_validation_id"],
            plan_id=row["plan_id"],
            task_run_id=row["task_run_id"],
            step_index=(
                int(row["step_index"])
                if row["step_index"] is not None
                else None
            ),
            attempt=int(row["attempt"]),
            status=PlanValidationStatus(row["status"]),
            failure_type=(
                FailureType(row["failure_type"])
                if row["failure_type"] is not None
                else None
            ),
            short_reason=row["short_reason"],
            command_ref=row["command_ref"],
            observed_at=float(row["observed_at"]),
        )

    def _decode_task_run(self, row: sqlite3.Row) -> TaskRunSnapshot:
        request_value = json.loads(row["request_json"])
        request = TaskRequest.from_dict(request_value)
        if request.model_backend is not None:
            raise StorageError("persisted TaskRequest must not contain model_backend")
        return TaskRunSnapshot(
            task_run_id=row["task_run_id"],
            request=request,
            effective_model_backend=ModelBackend(row["effective_model_backend"]),
            started_at=float(row["started_at"]),
            deadline_at=float(row["deadline_at"]),
            status=TaskRunStatus(row["status"]),
            sandbox_id=row["sandbox_id"],
            confirmation_deadline=(
                float(row["confirmation_deadline"])
                if row["confirmation_deadline"] is not None
                else None
            ),
            finished_at=(
                float(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
            failure_reason=row["failure_reason"],
        )

    def _load_plan_steps(self, plan_id: str) -> tuple[PlanStep, ...]:
        rows = self._connection.execute(
            """
            SELECT step_index, description FROM plan_steps
            WHERE plan_id = ? ORDER BY step_index
            """,
            (plan_id,),
        ).fetchall()
        indexes = tuple(int(row["step_index"]) for row in rows)
        if indexes != tuple(range(len(rows))):
            raise StorageError("Plan step indexes must be contiguous from zero")
        return tuple(PlanStep(description=row["description"]) for row in rows)
    def _apply_state_changes(
        self,
        mutation: ControlMutation,
        *,
        event_sequence: int,
        committed_at: float,
    ) -> None:
        order = {kind: index for index, kind in enumerate((
            UpsertTaskRun, UpsertTurn, UpsertIntent, UpsertPlan, UpsertExecution,
            UpsertFact, AppendPlanValidationRecord, AppendFailureSummary,
        ))}
        for ordinal, change in sorted(enumerate(mutation.state_changes), key=lambda entry: order[type(entry[1])]):
            self._validate_change_owner(change, mutation.task_run_id)
            if isinstance(change, UpsertTaskRun):
                self._upsert_task_run(change)
            elif isinstance(change, UpsertTurn):
                self._upsert_turn(change)
            elif isinstance(change, UpsertIntent):
                self._upsert_intent(change)
            elif isinstance(change, UpsertPlan):
                self._upsert_plan(change)
            elif isinstance(change, UpsertExecution):
                self._upsert_execution(change)
            elif isinstance(change, UpsertFact):
                self._upsert_fact(change, event_sequence, ordinal)
            elif isinstance(change, AppendFailureSummary):
                self._append_failure(
                    mutation.task_run_id,
                    change,
                    event_sequence,
                    ordinal,
                )
            elif isinstance(change, AppendPlanValidationRecord):
                self._append_plan_validation(change, committed_at)
            else:
                raise StorageError("unsupported state change")

    def _validate_existing_owners(self, mutation: ControlMutation) -> None:
        """Check the original rows before any parent can be changed or inserted."""
        specs = {
            UpsertTurn: ("turns", "turn_id", ("task_run_id",)),
            UpsertIntent: ("intents", "intent_id", ("turn_id",)),
            UpsertPlan: ("plans", "plan_id", ("intent_id",)),
            UpsertExecution: ("executions", "execution_id", ("plan_id", "step_index", "command_ref")),
            UpsertFact: ("facts", "fact_id", ("turn_id", "source_ref")),
        }
        owner_lookups = {
            UpsertTurn: self._task_run_for_turn,
            UpsertIntent: self._task_run_for_intent,
            UpsertPlan: self._task_run_for_plan,
            UpsertExecution: self._task_run_for_execution,
        }
        seen: set[tuple[type, str]] = set()
        for change in mutation.state_changes:
            spec = specs.get(type(change))
            if spec is None:
                continue
            table, id_column, immutable = spec
            entity_id = getattr(change, id_column)
            key = (type(change), entity_id)
            if key in seen:
                raise StorageError("duplicate writes to one control object")
            seen.add(key)
            row = self._connection.execute(
                f"SELECT {', '.join(immutable)} FROM {table} WHERE {id_column} = ?", (entity_id,),
            ).fetchone()
            if row is None:
                continue
            owner = (
                self._task_run_for_turn(row["turn_id"])
                if isinstance(change, UpsertFact) else owner_lookups[type(change)](entity_id)
            )
            if owner != mutation.task_run_id:
                raise StorageError("existing object belongs to another TaskRun")
            if tuple(row) != tuple(getattr(change, field) for field in immutable):
                raise StorageError("existing object owner and immutable fields cannot be modified")

    def _validate_change_owner(
        self,
        change: StateChange,
        task_run_id: str,
    ) -> None:
        if isinstance(change, UpsertTaskRun):
            if change.task_run_id != task_run_id:
                raise StorageError("UpsertTaskRun belongs to another TaskRun")
            return
        if isinstance(change, UpsertTurn):
            if change.task_run_id != task_run_id:
                raise StorageError("UpsertTurn belongs to another TaskRun")
            return
        if isinstance(change, UpsertIntent):
            owner = self._task_run_for_turn(change.turn_id)
        elif isinstance(change, UpsertPlan):
            owner = self._task_run_for_intent(change.intent_id)
        elif isinstance(change, UpsertExecution):
            owner = self._task_run_for_plan(change.plan_id)
        elif isinstance(change, UpsertFact):
            owner = self._task_run_for_turn(change.turn_id)
            if self._task_run_for_execution(change.source_ref) != task_run_id:
                raise StorageError("Fact source Execution belongs to another TaskRun")
        elif isinstance(change, AppendFailureSummary):
            if not self._failure_evidence_exists(task_run_id, change.evidence_ref):
                raise StorageError("FailureSummary evidence must belong to its TaskRun")
            return
        elif isinstance(change, AppendPlanValidationRecord):
            owner = self._task_run_for_plan(change.record.plan_id)
        else:
            raise StorageError("unsupported control object")
        if owner != task_run_id:
            raise StorageError("control object parent belongs to another TaskRun or does not exist")
        if isinstance(change, AppendPlanValidationRecord):
            if change.record.task_run_id != task_run_id:
                raise StorageError(
                    "AppendPlanValidationRecord belongs to another TaskRun"
                )

    def _upsert_task_run(self, change: UpsertTaskRun) -> None:
        request_json = canonical_json(task_request_payload(change.request))
        target_json = canonical_json(change.request.target.to_dict())
        previous = self._connection.execute(
            """
            SELECT request_json, effective_model_backend, started_at, deadline_at,
                   sandbox_id
            FROM task_runs WHERE task_run_id = ?
            """,
            (change.task_run_id,),
        ).fetchone()
        immutable_values = (
            request_json,
            change.effective_model_backend.value,
            change.started_at,
            change.deadline_at,
            change.sandbox_id,
        )
        if previous is not None and tuple(previous) != immutable_values:
            raise StorageError("immutable TaskRun fields cannot be modified")
        self._connection.execute(
            """
            INSERT INTO task_runs(
                task_run_id, request_json, goal, target_json, deadline_seconds,
                effective_model_backend, started_at, deadline_at, status, sandbox_id,
                confirmation_deadline, finished_at, failure_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_run_id) DO UPDATE SET
                status = excluded.status,
                confirmation_deadline = excluded.confirmation_deadline,
                finished_at = excluded.finished_at,
                failure_reason = excluded.failure_reason
            """,
            (
                change.task_run_id,
                request_json,
                change.request.goal,
                target_json,
                change.request.deadline_seconds,
                change.effective_model_backend.value,
                change.started_at,
                change.deadline_at,
                change.status.value,
                change.sandbox_id,
                change.confirmation_deadline,
                change.finished_at,
                change.failure_reason,
            ),
        )

    def _upsert_turn(self, change: UpsertTurn) -> None:
        self._connection.execute(
            """
            INSERT INTO turns(
                turn_id, task_run_id, sequence, turn_target, intent_ids_json, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                sequence = excluded.sequence,
                turn_target = excluded.turn_target,
                intent_ids_json = excluded.intent_ids_json,
                status = excluded.status
            WHERE turns.task_run_id = excluded.task_run_id
            """,
            (
                change.turn_id,
                change.task_run_id,
                change.sequence,
                change.turn_target,
                canonical_json(list(change.intent_ids)),
                change.status.value,
            ),
        )
        self._require_owner("turns", "turn_id", change.turn_id, change.task_run_id)

    def _upsert_intent(self, change: UpsertIntent) -> None:
        task_run_id = self._task_run_for_turn(change.turn_id)
        self._connection.execute(
            """
            INSERT INTO intents(
                intent_id, turn_id, description, priority, intent_type, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(intent_id) DO UPDATE SET
                description = excluded.description,
                priority = excluded.priority,
                intent_type = excluded.intent_type,
                status = excluded.status
            WHERE intents.turn_id = excluded.turn_id
            """,
            (
                change.intent_id,
                change.turn_id,
                change.description,
                change.priority,
                change.intent_type,
                change.status.value,
            ),
        )
        self._require_indirect_owner(
            "intents",
            "intent_id",
            change.intent_id,
            "turn_id",
            change.turn_id,
        )
        if task_run_id is None:
            raise StorageError("Intent Turn does not exist")

    def _upsert_plan(self, change: UpsertPlan) -> None:
        if self._task_run_for_intent(change.intent_id) is None:
            raise StorageError("Plan Intent does not exist")
        existing = self._connection.execute(
            "SELECT intent_id FROM plans WHERE plan_id = ?",
            (change.plan_id,),
        ).fetchone()
        if existing is not None and existing["intent_id"] != change.intent_id:
            raise StorageError("Plan owner cannot be modified")
        stored_steps = self._connection.execute(
            """
            SELECT step_index, description FROM plan_steps
            WHERE plan_id = ? ORDER BY step_index
            """,
            (change.plan_id,),
        ).fetchall()
        expected_steps = tuple(
            (index, item.description) for index, item in enumerate(change.steps)
        )
        if stored_steps and tuple(tuple(row) for row in stored_steps) != expected_steps:
            raise StorageError("Plan steps are immutable after first save")
        self._connection.execute(
            """
            INSERT INTO plans(plan_id, intent_id, status) VALUES (?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET status = excluded.status
            """,
            (change.plan_id, change.intent_id, change.status.value),
        )
        if not stored_steps:
            self._connection.executemany(
                """
                INSERT INTO plan_steps(plan_id, step_index, description)
                VALUES (?, ?, ?)
                """,
                (
                    (change.plan_id, index, item.description)
                    for index, item in enumerate(change.steps)
                ),
            )

    def _upsert_execution(self, change: UpsertExecution) -> None:
        if self._task_run_for_plan(change.plan_id) is None:
            raise StorageError("Execution Plan does not exist")
        existing = self._connection.execute(
            """
            SELECT plan_id, step_index, command_ref FROM executions
            WHERE execution_id = ?
            """,
            (change.execution_id,),
        ).fetchone()
        immutable = (change.plan_id, change.step_index, change.command_ref)
        if existing is not None and tuple(existing) != immutable:
            raise StorageError("immutable Execution fields cannot be modified")
        self._connection.execute(
            """
            INSERT INTO executions(
                execution_id, plan_id, step_index, command_ref, status, output_ref
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id) DO UPDATE SET
                status = excluded.status,
                output_ref = excluded.output_ref
            """,
            (
                change.execution_id,
                change.plan_id,
                change.step_index,
                change.command_ref,
                change.status.value,
                change.output_ref,
            ),
        )

    def _upsert_fact(
        self,
        change: UpsertFact,
        event_sequence: int,
        ordinal: int,
    ) -> None:
        task_run_id = self._task_run_for_turn(change.turn_id)
        if task_run_id is None:
            raise StorageError("Fact Turn does not exist")
        source_task_run_id = self._task_run_for_execution(change.source_ref)
        if source_task_run_id is None or source_task_run_id != task_run_id:
            raise StorageError("Fact source_ref must be an Execution in the same TaskRun")
        existing = self._connection.execute(
            "SELECT turn_id, source_ref FROM facts WHERE fact_id = ?",
            (change.fact_id,),
        ).fetchone()
        if existing is not None and tuple(existing) != (change.turn_id, change.source_ref):
            raise StorageError("Fact owner and source_ref cannot be modified")
        self._connection.execute(
            """
            INSERT INTO facts(
                fact_id, turn_id, content, source_ref, status,
                created_event_sequence, created_event_ordinal
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fact_id) DO UPDATE SET
                content = excluded.content,
                status = excluded.status
            """,
            (
                change.fact_id,
                change.turn_id,
                change.content,
                change.source_ref,
                change.status.value,
                event_sequence,
                ordinal,
            ),
        )

    def _append_failure(
        self,
        task_run_id: str,
        change: AppendFailureSummary,
        event_sequence: int,
        ordinal: int,
    ) -> None:
        if not self._failure_evidence_exists(task_run_id, change.evidence_ref):
            raise StorageError(
                "FailureSummary evidence_ref must reference committed evidence"
            )
        self._connection.execute(
            """
            INSERT INTO failure_summaries(
                task_run_id, failure_type, short_reason, evidence_ref,
                created_event_sequence, created_event_ordinal
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_run_id,
                change.failure_type.value,
                change.short_reason,
                change.evidence_ref,
                event_sequence,
                ordinal,
            ),
        )

    def _append_plan_validation(
        self,
        change: AppendPlanValidationRecord,
        committed_at: float,
    ) -> None:
        record = change.record
        if self._task_run_for_plan(record.plan_id) != record.task_run_id:
            raise StorageError(
                "PlanValidationRecord Plan must belong to its TaskRun"
            )
        self._connection.execute(
            """
            INSERT INTO plan_validation_records(
                plan_validation_id, plan_id, task_run_id, step_index, attempt,
                status, failure_type, short_reason, command_ref, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.plan_validation_id,
                record.plan_id,
                record.task_run_id,
                record.step_index,
                record.attempt,
                record.status.value,
                record.failure_type.value if record.failure_type is not None else None,
                record.short_reason,
                record.command_ref,
                committed_at,
            ),
        )

    def _insert_event(self, event: RuntimeEvent, sequence: int) -> None:
        self._connection.execute(
            """
            INSERT INTO runtime_events(
                task_run_id, sequence, type, timestamp, status, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.task_run_id,
                sequence,
                event.type,
                event.timestamp,
                event.status.value,
                canonical_json(dict(event.details)),
            ),
        )

    def _load_commit_result(
        self,
        task_run_id: str,
        event_sequence: int,
        checkpoint_sequence: int,
        *,
        replayed: bool,
    ) -> ControlCommitResult:
        event_row = self._connection.execute(
            """
            SELECT type, timestamp, status, details_json
            FROM runtime_events WHERE task_run_id = ? AND sequence = ?
            """,
            (task_run_id, event_sequence),
        ).fetchone()
        checkpoint_row = self._connection.execute(
            """
            SELECT watermark_event_sequence, created_at
            FROM checkpoints WHERE task_run_id = ? AND sequence = ?
            """,
            (task_run_id, checkpoint_sequence),
        ).fetchone()
        if event_row is None or checkpoint_row is None:
            raise StorageError("idempotency metadata references missing commit records")
        return ControlCommitResult(
            event=RuntimeEvent(
                type=event_row["type"],
                task_run_id=task_run_id,
                timestamp=float(event_row["timestamp"]),
                status=TaskRunStatus(event_row["status"]),
                details=json.loads(event_row["details_json"]),
                sequence=event_sequence,
            ),
            checkpoint=Checkpoint(
                task_run_id=task_run_id,
                sequence=checkpoint_sequence,
                watermark_event_sequence=int(
                    checkpoint_row["watermark_event_sequence"]
                ),
                created_at=float(checkpoint_row["created_at"]),
            ),
            replayed=replayed,
        )

    def _next_event_sequence(self, task_run_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence
            FROM runtime_events WHERE task_run_id = ?
            """,
            (task_run_id,),
        ).fetchone()
        assert row is not None
        return int(row["sequence"])

    def _task_run_for_turn(self, turn_id: str) -> str | None:
        return self._scalar(
            "SELECT task_run_id FROM turns WHERE turn_id = ?",
            (turn_id,),
        )

    def _task_run_for_intent(self, intent_id: str) -> str | None:
        return self._scalar(
            """
            SELECT turns.task_run_id
            FROM intents JOIN turns ON turns.turn_id = intents.turn_id
            WHERE intents.intent_id = ?
            """,
            (intent_id,),
        )

    def _task_run_for_plan(self, plan_id: str) -> str | None:
        return self._scalar(
            """
            SELECT turns.task_run_id
            FROM plans
            JOIN intents ON intents.intent_id = plans.intent_id
            JOIN turns ON turns.turn_id = intents.turn_id
            WHERE plans.plan_id = ?
            """,
            (plan_id,),
        )

    def _task_run_for_execution(self, execution_id: str) -> str | None:
        return self._scalar(
            """
            SELECT turns.task_run_id
            FROM executions
            JOIN plans ON plans.plan_id = executions.plan_id
            JOIN intents ON intents.intent_id = plans.intent_id
            JOIN turns ON turns.turn_id = intents.turn_id
            WHERE executions.execution_id = ?
            """,
            (execution_id,),
        )

    def _failure_evidence_exists(self, task_run_id: str, evidence_ref: str) -> bool:
        execution_owner = self._task_run_for_execution(evidence_ref)
        if execution_owner == task_run_id:
            return True
        validation_owner = self._scalar(
            """
            SELECT task_run_id FROM plan_validation_records
            WHERE plan_validation_id = ?
            """,
            (evidence_ref,),
        )
        return validation_owner == task_run_id

    def _scalar(self, sql: str, parameters: tuple[object, ...]) -> str | None:
        row = self._connection.execute(sql, parameters).fetchone()
        return None if row is None else str(row[0])

    def _require_owner(
        self,
        table: str,
        id_column: str,
        entity_id: str,
        task_run_id: str,
    ) -> None:
        row = self._connection.execute(
            f"SELECT task_run_id FROM {table} WHERE {id_column} = ?",
            (entity_id,),
        ).fetchone()
        if row is None or row["task_run_id"] != task_run_id:
            raise StorageError(f"{table} owner cannot be modified")

    def _require_indirect_owner(
        self,
        table: str,
        id_column: str,
        entity_id: str,
        owner_column: str,
        owner_id: str,
    ) -> None:
        row = self._connection.execute(
            f"SELECT {owner_column} FROM {table} WHERE {id_column} = ?",
            (entity_id,),
        ).fetchone()
        if row is None or row[owner_column] != owner_id:
            raise StorageError(f"{table} owner cannot be modified")

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise StorageError("storage clock returned a non-finite timestamp")
        return value

    def _rollback_quietly(self) -> None:
        try:
            self._connection.rollback()
        except sqlite3.Error:
            pass


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise StorageError("incomplete SQLite migration statement")
    return statements


def _numbered_event(event: RuntimeEvent, sequence: int) -> RuntimeEvent:
    return RuntimeEvent(
        type=event.type,
        task_run_id=event.task_run_id,
        timestamp=event.timestamp,
        status=event.status,
        details=dict(event.details),
        sequence=sequence,
    )
