from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import asdict
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
    PersistedIntentGoal,
    PlanSnapshot,
    StaleControlStateError,
    StateChange,
    StorageError,
    TaskRunSnapshot,
    TargetNetworkBinding,
    TurnSnapshot,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertIntentGoal,
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
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.modeling import (
    IntentActionResponse, ModelRequestType, PlanGenerationResponse, parse_intent_action,
)
from agent_core.intent_action_application import (
    IntentActionApplication,
    IntentActionApplicationConflict,
    IntentPlanApplication,
)
from agent_core.model_invocation_audit import (
    ModelInvocationCompletion,
    ModelInvocationBudgetExhausted,
    ModelInvocationConflict,
    ModelInvocationRecord,
    ModelInvocationRecoveryRequired,
    ModelInvocationStart,
    result_from_payload,
    result_payload,
)
from agent_core.action_audit import (
    ActionAuditConflict,
    ActionInvocationCompletion,
    ActionInvocationRecord,
    ActionInvocationStart,
    ActionRecoveryRequired,
    ActionResourceIds,
)


CURRENT_SCHEMA_VERSION = 10


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


MIGRATION_2 = """
CREATE TABLE intent_goals (
    intent_id TEXT PRIMARY KEY REFERENCES intents(intent_id),
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    goal_type TEXT NOT NULL CHECK (goal_type = 'file_type'),
    path TEXT NOT NULL CHECK (length(path) > 0),
    created_event_sequence INTEGER NOT NULL,
    FOREIGN KEY (task_run_id, created_event_sequence)
        REFERENCES runtime_events(task_run_id, sequence)
        DEFERRABLE INITIALLY DEFERRED
);
"""


MIGRATION_3 = """
ALTER TABLE task_runs ADD COLUMN source_root TEXT;
"""


MIGRATION_4 = """
ALTER TABLE facts ADD COLUMN verification_ref TEXT;
"""


MIGRATION_5 = """
CREATE TABLE model_invocations (
    model_request_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    request_type TEXT NOT NULL CHECK (request_type IN ('plan_generation', 'intent_action')),
    plan_id TEXT REFERENCES plans(plan_id),
    correction_of_model_request_id TEXT REFERENCES model_invocations(model_request_id),
    started_at REAL NOT NULL,
    finished_at REAL,
    result_kind TEXT CHECK (result_kind IN ('plan', 'intent_action', 'invalid_structure', 'model_error')),
    result_json TEXT,
    CHECK ((finished_at IS NULL AND result_kind IS NULL AND result_json IS NULL)
        OR (finished_at IS NOT NULL AND result_kind IS NOT NULL AND result_json IS NOT NULL)),
    CHECK ((request_type = 'plan_generation' AND plan_id IS NULL)
        OR (request_type = 'intent_action' AND plan_id IS NOT NULL))
);
CREATE INDEX idx_model_invocations_scope
ON model_invocations(task_run_id, intent_id, request_type, started_at);
"""


MIGRATION_6 = """
CREATE TABLE intent_plan_applications (
    model_request_id TEXT PRIMARY KEY REFERENCES model_invocations(model_request_id),
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
    plan_id TEXT NOT NULL UNIQUE,
    application_json TEXT NOT NULL
);
CREATE TABLE intent_action_applications (
    model_request_id TEXT PRIMARY KEY REFERENCES model_invocations(model_request_id),
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    application_json TEXT NOT NULL
);
"""

MIGRATION_7 = """
ALTER TABLE task_runs ADD COLUMN target_network_json TEXT;
"""

MIGRATION_8 = """
CREATE TABLE turn_model_invocations (
    model_request_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    turn_id TEXT NOT NULL,
    registration_mutation_id TEXT NOT NULL,
    base_checkpoint_sequence INTEGER NOT NULL,
    context_sha256 TEXT NOT NULL,
    turn_target TEXT NOT NULL,
    correction_of_model_request_id TEXT UNIQUE REFERENCES turn_model_invocations(model_request_id),
    started_at REAL NOT NULL,
    finished_at REAL,
    result_kind TEXT CHECK (result_kind IN ('intent_generation', 'invalid_structure', 'model_error')),
    result_json TEXT,
    CHECK ((finished_at IS NULL AND result_kind IS NULL AND result_json IS NULL)
        OR (finished_at IS NOT NULL AND result_kind IS NOT NULL AND result_json IS NOT NULL))
);
CREATE INDEX idx_turn_model_invocations_scope
ON turn_model_invocations(task_run_id, turn_id, started_at);
"""

MIGRATION_9 = """
CREATE TABLE plans_v9 (
    plan_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL,
    UNIQUE(intent_id, sequence)
);
INSERT INTO plans_v9(plan_id, intent_id, sequence, status)
SELECT plan_id, intent_id, 1, status FROM plans;
DROP TABLE plans;
ALTER TABLE plans_v9 RENAME TO plans;
CREATE TABLE intent_plan_applications_v9 (
    model_request_id TEXT PRIMARY KEY REFERENCES model_invocations(model_request_id),
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    plan_id TEXT NOT NULL UNIQUE,
    application_json TEXT NOT NULL
);
INSERT INTO intent_plan_applications_v9
SELECT * FROM intent_plan_applications;
DROP TABLE intent_plan_applications;
ALTER TABLE intent_plan_applications_v9 RENAME TO intent_plan_applications;
CREATE INDEX idx_intent_plan_applications_scope
ON intent_plan_applications(task_run_id, intent_id);
"""

MIGRATION_10 = """
CREATE TABLE t6_action_invocations (
    model_request_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    base_checkpoint_sequence INTEGER NOT NULL CHECK (base_checkpoint_sequence > 0),
    context_sha256 TEXT NOT NULL,
    resource_ids_json TEXT NOT NULL,
    correction_of_model_request_id TEXT UNIQUE REFERENCES t6_action_invocations(model_request_id),
    started_at REAL NOT NULL,
    finished_at REAL,
    result_kind TEXT CHECK (result_kind IN ('action', 'invalid_structure', 'model_error')),
    result_sha256 TEXT,
    applied_at REAL,
    CHECK ((finished_at IS NULL AND result_kind IS NULL AND result_sha256 IS NULL)
        OR (finished_at IS NOT NULL AND result_kind IS NOT NULL AND result_sha256 IS NOT NULL)),
    CHECK (applied_at IS NULL OR finished_at IS NOT NULL)
);
CREATE INDEX idx_t6_action_invocations_scope
ON t6_action_invocations(task_run_id, intent_id, started_at);
CREATE TABLE t6_action_resource_ids (
    resource_id TEXT PRIMARY KEY,
    model_request_id TEXT NOT NULL REFERENCES t6_action_invocations(model_request_id),
    resource_kind TEXT NOT NULL,
    UNIQUE(model_request_id, resource_kind)
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
        connection: sqlite3.Connection | None = None
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
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise StorageError(f"could not open SQLite control store: {exc}") from exc

    @classmethod
    def open_readonly(cls, path: str | os.PathLike[str]) -> SqliteControlStore:
        """Read current-schema history without migrations or journal changes."""
        database_path = Path(path).resolve(strict=True)
        connection = sqlite3.connect(
            database_path.as_uri() + "?mode=ro", uri=True, timeout=0.1,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
            if version != CURRENT_SCHEMA_VERSION:
                raise StorageError("read-only history requires the current schema")
            connection.execute("BEGIN")
            return cls(connection, database_path, clock=time.time)
        except BaseException:
            connection.close()
            raise

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
            # SQLite cannot remove the old UNIQUE(intent_id) with ALTER TABLE.
            # Rebuild in one transaction and check every child reference before commit.
            self._connection.execute("PRAGMA foreign_keys = OFF")
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
            if version < 2:
                for statement in _sql_statements(MIGRATION_2):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (2, self._now()),
                )
            if version < 3:
                for statement in _sql_statements(MIGRATION_3):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (3, self._now()),
                )
            if version < 4:
                for statement in _sql_statements(MIGRATION_4):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (4, self._now()),
                )
            if version < 5:
                for statement in _sql_statements(MIGRATION_5):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (5, self._now()),
                )
            if version < 6:
                for statement in _sql_statements(MIGRATION_6):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (6, self._now()),
                )
            if version < 7:
                for statement in _sql_statements(MIGRATION_7):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (7, self._now()),
                )
            if version < 8:
                for statement in _sql_statements(MIGRATION_8):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (8, self._now()),
                )
            if version < 9:
                for statement in _sql_statements(MIGRATION_9):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (9, self._now()),
                )
            if version < 10:
                for statement in _sql_statements(MIGRATION_10):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (10, self._now()),
                )
            if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StorageError("SQLite migration left invalid foreign keys")
            self._connection.commit()
        except StorageError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise StorageError(f"SQLite schema migration failed: {exc}") from exc
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _t6_action_from_row(row: sqlite3.Row) -> ActionInvocationRecord:
        start = ActionInvocationStart(
            row["model_request_id"], row["task_run_id"], row["intent_id"],
            row["base_checkpoint_sequence"], row["context_sha256"],
            ActionResourceIds(**json.loads(row["resource_ids_json"])),
            row["started_at"], row["correction_of_model_request_id"],
        )
        completion = None
        if row["finished_at"] is not None:
            completion = ActionInvocationCompletion(
                start.model_request_id, row["finished_at"], row["result_kind"],
                row["result_sha256"],
            )
        return ActionInvocationRecord(start, completion, row["applied_at"])

    def begin_t6_action(
        self, start: ActionInvocationStart,
    ) -> ActionInvocationRecord | None:
        if not isinstance(start, ActionInvocationStart):
            raise TypeError("start must be an ActionInvocationStart")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT * FROM t6_action_invocations WHERE model_request_id = ?",
                (start.model_request_id,),
            ).fetchone()
            if existing is not None:
                record = self._t6_action_from_row(existing)
                if record.start != start:
                    raise ActionAuditConflict("model request ID has different action fields")
                self._connection.commit()
                return record
            owner = self._connection.execute(
                """SELECT task_runs.status AS task_status, task_runs.started_at,
                          task_runs.deadline_at, intents.status AS intent_status
                   FROM intents JOIN turns ON turns.turn_id = intents.turn_id
                   JOIN task_runs ON task_runs.task_run_id = turns.task_run_id
                   WHERE intents.intent_id = ? AND task_runs.task_run_id = ?""",
                (start.intent_id, start.task_run_id),
            ).fetchone()
            checkpoint = self._connection.execute(
                "SELECT MAX(sequence) FROM checkpoints WHERE task_run_id = ?",
                (start.task_run_id,),
            ).fetchone()[0]
            if (owner is None or owner["task_status"] != TaskRunStatus.RUNNING.value
                or owner["intent_status"] != IntentStatus.RUNNING.value
                or not owner["started_at"] <= start.started_at < owner["deadline_at"]
                or checkpoint != start.base_checkpoint_sequence):
                raise ActionAuditConflict("action is outside its running checkpoint")
            prior_rows = self._connection.execute(
                """SELECT * FROM t6_action_invocations
                   WHERE task_run_id = ? AND intent_id = ? ORDER BY rowid""",
                (start.task_run_id, start.intent_id),
            ).fetchall()
            if prior_rows:
                prior = self._t6_action_from_row(prior_rows[-1])
                if prior.completion is None or prior.applied_at is None:
                    raise ActionRecoveryRequired("prior model action remains unapplied")
                if prior.completion.result_kind == "model_error":
                    raise ActionAuditConflict("prior model error stops this Intent")
                if prior.completion.result_kind == "invalid_structure":
                    if (start.correction_of_model_request_id != prior.start.model_request_id
                        or prior.start.correction_of_model_request_id is not None
                        or start.base_checkpoint_sequence != prior.start.base_checkpoint_sequence
                        or start.context_sha256 != prior.start.context_sha256):
                        raise ActionAuditConflict("only one matching structure correction is allowed")
                elif start.correction_of_model_request_id is not None:
                    raise ActionAuditConflict("correction requires an invalid structure")
            elif start.correction_of_model_request_id is not None:
                raise ActionAuditConflict("correction has no original action")
            self._connection.execute(
                """INSERT INTO t6_action_invocations(
                    model_request_id, task_run_id, intent_id, base_checkpoint_sequence,
                    context_sha256, resource_ids_json, correction_of_model_request_id,
                    started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (start.model_request_id, start.task_run_id, start.intent_id,
                 start.base_checkpoint_sequence, start.context_sha256,
                 canonical_json(asdict(start.resource_ids)),
                 start.correction_of_model_request_id, start.started_at),
            )
            for resource_kind, resource_id in asdict(start.resource_ids).items():
                self._connection.execute(
                    """INSERT INTO t6_action_resource_ids(
                        resource_id, model_request_id, resource_kind
                    ) VALUES (?, ?, ?)""",
                    (resource_id, start.model_request_id, resource_kind),
                )
            self._connection.commit()
            return None
        except (ActionAuditConflict, ActionRecoveryRequired, StorageError):
            self._rollback_quietly()
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback_quietly()
            raise ActionAuditConflict("T6 action resource ID is already claimed") from exc
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not begin T6 model action") from exc

    def complete_t6_action(
        self, completion: ActionInvocationCompletion,
    ) -> ActionInvocationRecord:
        if not isinstance(completion, ActionInvocationCompletion):
            raise TypeError("completion must be an ActionInvocationCompletion")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM t6_action_invocations WHERE model_request_id = ?",
                (completion.model_request_id,),
            ).fetchone()
            if row is None:
                raise ActionAuditConflict("model action was not started")
            previous = self._t6_action_from_row(row)
            if previous.completion is not None:
                if previous.completion != completion:
                    raise ActionAuditConflict("model action has another result")
                self._connection.commit()
                return previous
            if completion.finished_at < previous.start.started_at:
                raise ActionAuditConflict("action result precedes its invocation")
            self._connection.execute(
                """UPDATE t6_action_invocations
                   SET finished_at = ?, result_kind = ?, result_sha256 = ?
                   WHERE model_request_id = ? AND finished_at IS NULL""",
                (completion.finished_at, completion.result_kind,
                 completion.result_sha256, completion.model_request_id),
            )
            self._connection.commit()
            return ActionInvocationRecord(previous.start, completion, None)
        except (ActionAuditConflict, StorageError):
            self._rollback_quietly()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not complete T6 model action") from exc

    def mark_t6_action_applied(
        self, model_request_id: str, applied_at: float,
    ) -> ActionInvocationRecord:
        if not isinstance(applied_at, (int, float)) or not math.isfinite(applied_at):
            raise ValueError("invalid_action_applied_time")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM t6_action_invocations WHERE model_request_id = ?",
                (model_request_id,),
            ).fetchone()
            if row is None:
                raise ActionAuditConflict("model action was not started")
            previous = self._t6_action_from_row(row)
            if previous.completion is None or applied_at < previous.completion.finished_at:
                raise ActionAuditConflict("model action has no applicable result")
            if previous.applied_at is not None:
                if previous.applied_at != applied_at:
                    raise ActionAuditConflict("model action already has another application time")
                self._connection.commit()
                return previous
            self._connection.execute(
                "UPDATE t6_action_invocations SET applied_at = ? WHERE model_request_id = ?",
                (applied_at, model_request_id),
            )
            self._connection.commit()
            return ActionInvocationRecord(previous.start, previous.completion, applied_at)
        except (ActionAuditConflict, StorageError):
            self._rollback_quietly()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not mark T6 model action applied") from exc

    def list_t6_actions(
        self, task_run_id: str, intent_id: str,
    ) -> tuple[ActionInvocationRecord, ...]:
        try:
            rows = self._connection.execute(
                """SELECT * FROM t6_action_invocations
                   WHERE task_run_id = ? AND intent_id = ? ORDER BY rowid""",
                (task_run_id, intent_id),
            ).fetchall()
            return tuple(self._t6_action_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise StorageError("could not read T6 model actions") from exc

    def begin_model_invocation(
        self, start: ModelInvocationStart
    ) -> ModelInvocationRecord | None:
        if not isinstance(start, ModelInvocationStart):
            raise TypeError("start must be a ModelInvocationStart")
        if start.request_type is ModelRequestType.INTENT_GENERATION:
            return self._begin_turn_model_invocation(start)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            previous = self._connection.execute(
                "SELECT * FROM model_invocations WHERE model_request_id = ?",
                (start.model_request_id,),
            ).fetchone()
            if previous is not None:
                record = self._model_invocation_from_row(previous)
                if record.start != start:
                    raise ModelInvocationConflict("model_request_id has different start fields")
                self._connection.commit()
                return record
            if self._connection.execute(
                """
                SELECT 1 FROM model_invocations
                WHERE task_run_id = ? AND intent_id = ? AND finished_at IS NULL
                LIMIT 1
                """,
                (start.task_run_id, start.intent_id),
            ).fetchone():
                raise ModelInvocationRecoveryRequired(
                    "an earlier model call has no committed outcome"
                )
            limit = 2 if start.request_type is ModelRequestType.PLAN_GENERATION else 8
            count = self._connection.execute(
                """
                SELECT COUNT(*) FROM model_invocations
                WHERE task_run_id = ? AND intent_id = ? AND request_type = ?
                """,
                (start.task_run_id, start.intent_id, start.request_type.value),
            ).fetchone()[0]
            if count >= limit:
                raise ModelInvocationBudgetExhausted(
                    f"{start.request_type.value} call budget is exhausted"
                )
            owner = self._connection.execute(
                """
                SELECT task_runs.task_run_id, task_runs.status AS task_status,
                       task_runs.started_at AS task_started_at,
                       task_runs.deadline_at, intents.status AS intent_status,
                       intent_goals.intent_id AS goal_intent_id
                FROM intents
                JOIN turns ON turns.turn_id = intents.turn_id
                JOIN task_runs ON task_runs.task_run_id = turns.task_run_id
                LEFT JOIN intent_goals ON intent_goals.intent_id = intents.intent_id
                WHERE intents.intent_id = ?
                """,
                (start.intent_id,),
            ).fetchone()
            if (
                owner is None
                or owner["task_run_id"] != start.task_run_id
                or owner["task_status"] != TaskRunStatus.RUNNING.value
                or owner["intent_status"] != IntentStatus.RUNNING.value
                or owner["goal_intent_id"] is None
                or not owner["task_started_at"] <= start.started_at < owner["deadline_at"]
            ):
                raise ModelInvocationConflict("model invocation is outside a running bound Intent")
            if start.plan_id is not None:
                plan = self._connection.execute(
                    "SELECT intent_id, status FROM plans WHERE plan_id = ?",
                    (start.plan_id,),
                ).fetchone()
                if (
                    plan is None
                    or plan["intent_id"] != start.intent_id
                    or plan["status"] not in {
                        PlanStatus.RUNNING.value,
                        PlanStatus.COMPLETED.value,
                    }
                ):
                    raise ModelInvocationConflict(
                        "intent_action requires its running or completed Plan"
                    )
            if start.correction_of_model_request_id is not None:
                original = self._connection.execute(
                    "SELECT intent_id, request_type, result_kind FROM model_invocations WHERE model_request_id = ?",
                    (start.correction_of_model_request_id,),
                ).fetchone()
                if (
                    original is None
                    or original["intent_id"] != start.intent_id
                    or original["request_type"] != start.request_type.value
                    or original["result_kind"] != "invalid_structure"
                ):
                    raise ModelInvocationConflict("correction does not reference a structural error")
            self._connection.execute(
                """
                INSERT INTO model_invocations(
                    model_request_id, task_run_id, intent_id, request_type,
                    plan_id, correction_of_model_request_id, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    start.model_request_id,
                    start.task_run_id,
                    start.intent_id,
                    start.request_type.value,
                    start.plan_id,
                    start.correction_of_model_request_id,
                    start.started_at,
                ),
            )
            self._connection.commit()
            return None
        except (
            ModelInvocationBudgetExhausted,
            ModelInvocationConflict,
            ModelInvocationRecoveryRequired,
            StorageError,
        ):
            self._rollback_quietly()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not begin model invocation") from exc

    def _begin_turn_model_invocation(
        self, start: ModelInvocationStart
    ) -> ModelInvocationRecord | None:
        from agent_core.turn_manager import TurnManager

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM turn_model_invocations WHERE model_request_id = ?",
                (start.model_request_id,),
            ).fetchone()
            if row is not None:
                previous = self._turn_invocation_from_row(row)
                if previous.start != start:
                    raise ModelInvocationConflict("model_request_id has different Turn generation fields")
                self._connection.commit()
                return previous
            if start.intent_id != TurnManager.registration_turn_id(
                start.task_run_id, start.registration_mutation_id
            ):
                raise ModelInvocationConflict("Turn ID does not match registration mutation")
            owner = self._connection.execute(
                "SELECT status, started_at, deadline_at FROM task_runs WHERE task_run_id = ?",
                (start.task_run_id,),
            ).fetchone()
            checkpoint = self._connection.execute(
                "SELECT MAX(sequence) FROM checkpoints WHERE task_run_id = ?",
                (start.task_run_id,),
            ).fetchone()[0]
            if (owner is None or owner["status"] != TaskRunStatus.RUNNING.value
                or not owner["started_at"] <= start.started_at < owner["deadline_at"]
                or checkpoint != start.base_checkpoint_sequence):
                raise ModelInvocationConflict("Turn generation is outside its running checkpoint")
            if self._connection.execute(
                "SELECT 1 FROM turns WHERE task_run_id = ? AND status NOT IN (?, ?, ?) LIMIT 1",
                (start.task_run_id, TurnStatus.COMPLETED.value,
                 TurnStatus.INCONCLUSIVE.value, TurnStatus.FAILED.value),
            ).fetchone():
                raise ModelInvocationConflict("a prior Turn is still active")
            if self._connection.execute(
                """SELECT 1 FROM turn_model_invocations AS invocation
                   WHERE invocation.task_run_id = ? AND invocation.registration_mutation_id != ?
                   AND NOT EXISTS (SELECT 1 FROM turns WHERE turn_id = invocation.turn_id)
                   LIMIT 1""",
                (start.task_run_id, start.registration_mutation_id),
            ).fetchone():
                raise ModelInvocationRecoveryRequired("an earlier Turn generation remains unapplied")
            if self._connection.execute(
                """SELECT 1 FROM turn_model_invocations WHERE task_run_id = ?
                   AND turn_id = ? AND finished_at IS NULL LIMIT 1""",
                (start.task_run_id, start.intent_id),
            ).fetchone():
                raise ModelInvocationRecoveryRequired("an earlier Turn model call has no committed result")
            if start.correction_of_model_request_id is not None:
                original = self._connection.execute(
                    "SELECT * FROM turn_model_invocations WHERE model_request_id = ?",
                    (start.correction_of_model_request_id,),
                ).fetchone()
                if (original is None or original["turn_id"] != start.intent_id
                    or original["result_kind"] != "invalid_structure"
                    or any(original[field] != getattr(start, field) for field in (
                        "registration_mutation_id", "base_checkpoint_sequence",
                        "context_sha256", "turn_target"))):
                    raise ModelInvocationConflict("Turn correction does not match an invalid result")
            elif self._connection.execute(
                "SELECT 1 FROM turn_model_invocations WHERE task_run_id = ? AND turn_id = ? LIMIT 1",
                (start.task_run_id, start.intent_id),
            ).fetchone():
                raise ModelInvocationConflict("Turn generation was already started")
            self._connection.execute(
                """INSERT INTO turn_model_invocations(
                    model_request_id, task_run_id, turn_id, registration_mutation_id,
                    base_checkpoint_sequence, context_sha256, turn_target,
                    correction_of_model_request_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (start.model_request_id, start.task_run_id, start.intent_id,
                 start.registration_mutation_id, start.base_checkpoint_sequence,
                 start.context_sha256, start.turn_target,
                 start.correction_of_model_request_id, start.started_at),
            )
            self._connection.commit()
            return None
        except (ModelInvocationConflict, ModelInvocationRecoveryRequired, StorageError):
            self._rollback_quietly()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not begin Turn model invocation") from exc

    def _complete_turn_model_invocation(
        self, completion: ModelInvocationCompletion
    ) -> ModelInvocationRecord:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM turn_model_invocations WHERE model_request_id = ?",
                (completion.model_request_id,),
            ).fetchone()
            if row is None:
                raise ModelInvocationConflict("Turn model invocation was not started")
            previous = self._turn_invocation_from_row(row)
            if previous.completion is not None:
                if previous.completion != completion:
                    raise ModelInvocationConflict("Turn model invocation has another result")
                self._connection.commit()
                return previous
            if (completion.finished_at < previous.start.started_at
                or completion.result_kind not in {
                    "intent_generation", "invalid_structure", "model_error"
                }):
                raise ModelInvocationConflict("model result does not match Turn generation")
            self._connection.execute(
                """UPDATE turn_model_invocations
                   SET finished_at = ?, result_kind = ?, result_json = ?
                   WHERE model_request_id = ? AND finished_at IS NULL""",
                (completion.finished_at, completion.result_kind,
                 canonical_json(result_payload(completion)), completion.model_request_id),
            )
            self._connection.commit()
            return ModelInvocationRecord(previous.start, completion)
        except (ModelInvocationConflict, StorageError):
            self._rollback_quietly()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not complete Turn model invocation") from exc

    @staticmethod
    def _turn_invocation_from_row(row: sqlite3.Row) -> ModelInvocationRecord:
        start = ModelInvocationStart(
            row["model_request_id"], row["task_run_id"], row["turn_id"],
            ModelRequestType.INTENT_GENERATION, None,
            row["correction_of_model_request_id"], row["started_at"],
            row["registration_mutation_id"], row["base_checkpoint_sequence"],
            row["context_sha256"], row["turn_target"],
        )
        if row["finished_at"] is None:
            return ModelInvocationRecord(start, None)
        completion = ModelInvocationCompletion(
            start.model_request_id, row["finished_at"], row["result_kind"],
            result_from_payload(row["result_kind"], json.loads(row["result_json"])),
        )
        return ModelInvocationRecord(start, completion)

    def list_unapplied_turn_generations(
        self, task_run_id: str
    ) -> tuple[ModelInvocationRecord, ...]:
        try:
            rows = self._connection.execute(
                """SELECT * FROM turn_model_invocations AS invocation
                   WHERE invocation.task_run_id = ?
                   AND NOT EXISTS (SELECT 1 FROM turns WHERE turn_id = invocation.turn_id)
                   ORDER BY started_at, rowid""",
                (task_run_id,),
            ).fetchall()
            return tuple(self._turn_invocation_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise StorageError("could not read unapplied Turn model calls") from exc

    def complete_model_invocation(
        self, completion: ModelInvocationCompletion
    ) -> ModelInvocationRecord:
        if not isinstance(completion, ModelInvocationCompletion):
            raise TypeError("completion must be a ModelInvocationCompletion")
        if self._connection.execute(
            "SELECT 1 FROM turn_model_invocations WHERE model_request_id = ?",
            (completion.model_request_id,),
        ).fetchone():
            return self._complete_turn_model_invocation(completion)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM model_invocations WHERE model_request_id = ?",
                (completion.model_request_id,),
            ).fetchone()
            if row is None:
                raise ModelInvocationConflict("model invocation was not started")
            previous = self._model_invocation_from_row(row)
            if previous.completion is not None:
                if previous.completion != completion:
                    raise ModelInvocationConflict("model invocation has another result")
                self._connection.commit()
                return previous
            if (
                completion.finished_at < previous.start.started_at
                or completion.result_kind == "plan"
                and previous.start.request_type is not ModelRequestType.PLAN_GENERATION
                or completion.result_kind == "intent_action"
                and previous.start.request_type is not ModelRequestType.INTENT_ACTION
            ):
                raise ModelInvocationConflict("model result does not match its request")
            encoded = canonical_json(result_payload(completion))
            self._connection.execute(
                """
                UPDATE model_invocations
                SET finished_at = ?, result_kind = ?, result_json = ?
                WHERE model_request_id = ? AND finished_at IS NULL
                """,
                (
                    completion.finished_at,
                    completion.result_kind,
                    encoded,
                    completion.model_request_id,
                ),
            )
            self._connection.commit()
            return ModelInvocationRecord(previous.start, completion)
        except (ModelInvocationConflict, StorageError):
            self._rollback_quietly()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            self._rollback_quietly()
            raise StorageError("could not complete model invocation") from exc

    def list_model_invocations(
        self, task_run_id: str, intent_id: str
    ) -> tuple[ModelInvocationRecord, ...]:
        try:
            turn_rows = self._connection.execute(
                "SELECT * FROM turn_model_invocations WHERE task_run_id = ? AND turn_id = ? ORDER BY started_at, rowid",
                (task_run_id, intent_id),
            ).fetchall()
            if turn_rows:
                return tuple(self._turn_invocation_from_row(row) for row in turn_rows)
            rows = self._connection.execute(
                """
                SELECT * FROM model_invocations
                WHERE task_run_id = ? AND intent_id = ?
                ORDER BY started_at, rowid
                """,
                (task_run_id, intent_id),
            ).fetchall()
            return tuple(self._model_invocation_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise StorageError("could not read model invocation audit") from exc

    @staticmethod
    def _model_invocation_from_row(row: sqlite3.Row) -> ModelInvocationRecord:
        start = ModelInvocationStart(
            row["model_request_id"],
            row["task_run_id"],
            row["intent_id"],
            ModelRequestType(row["request_type"]),
            row["plan_id"],
            row["correction_of_model_request_id"],
            row["started_at"],
        )
        if row["finished_at"] is None:
            return ModelInvocationRecord(start, None)
        kind = row["result_kind"]
        completion = ModelInvocationCompletion(
            start.model_request_id,
            row["finished_at"],
            kind,
            result_from_payload(kind, json.loads(row["result_json"])),
        )
        return ModelInvocationRecord(start, completion)

    def save_plan_application(self, claim: IntentPlanApplication) -> None:
        if not isinstance(claim, IntentPlanApplication):
            raise TypeError("claim must be an IntentPlanApplication")
        self._save_intent_application(claim)

    def save_action_application(self, claim: IntentActionApplication) -> None:
        if not isinstance(claim, IntentActionApplication):
            raise TypeError("claim must be an IntentActionApplication")
        self._save_intent_application(claim)

    def _save_intent_application(
        self, claim: IntentPlanApplication | IntentActionApplication
    ) -> None:
        is_plan = isinstance(claim, IntentPlanApplication)
        table = "intent_plan_applications" if is_plan else "intent_action_applications"
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM model_invocations WHERE model_request_id = ?",
                (claim.model_request_id,),
            ).fetchone()
            if row is None:
                raise IntentActionApplicationConflict("model invocation is missing")
            record = self._model_invocation_from_row(row)
            completion = record.completion
            if (
                record.start.task_run_id != claim.task_run_id
                or record.start.intent_id != claim.intent_id
                or completion is None
            ):
                raise IntentActionApplicationConflict("application requires its completed model invocation")
            if isinstance(claim, IntentPlanApplication):
                if (
                    not isinstance(completion.result, PlanGenerationResponse)
                    or completion.result.candidate.steps != claim.steps
                ):
                    raise IntentActionApplicationConflict("Plan differs from audited result")
            elif (
                not isinstance(completion.result, IntentActionResponse)
                or completion.result.action != claim.action
                or record.start.plan_id != claim.plan_id
            ):
                raise IntentActionApplicationConflict("action differs from audited result")
            payload = asdict(claim)
            if not is_plan:
                payload["action"] = result_payload(completion)["action"]
            encoded = canonical_json(payload)
            previous = self._connection.execute(
                f"SELECT application_json FROM {table} WHERE model_request_id = ?",
                (claim.model_request_id,),
            ).fetchone()
            if previous is not None:
                if previous["application_json"] != encoded:
                    raise IntentActionApplicationConflict("application identity is immutable")
                self._connection.commit()
                return
            owner = self._connection.execute(
                """SELECT task_runs.status AS task_status, intents.status AS intent_status
                   FROM intents JOIN turns ON turns.turn_id = intents.turn_id
                   JOIN task_runs ON task_runs.task_run_id = turns.task_run_id
                   WHERE intents.intent_id = ? AND task_runs.task_run_id = ?""",
                (claim.intent_id, claim.task_run_id),
            ).fetchone()
            if (
                owner is None
                or owner["task_status"] != TaskRunStatus.RUNNING.value
                or owner["intent_status"] != IntentStatus.RUNNING.value
            ):
                raise IntentActionApplicationConflict("application requires a running Intent")
            self._connection.execute(
                f"""INSERT INTO {table}
                    (model_request_id, task_run_id, intent_id, plan_id, application_json)
                    VALUES (?, ?, ?, ?, ?)""",
                (claim.model_request_id, claim.task_run_id, claim.intent_id, claim.plan_id, encoded),
            )
            self._connection.commit()
        except IntentActionApplicationConflict:
            self._rollback_quietly()
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback_quietly()
            raise IntentActionApplicationConflict("application ownership conflicts") from exc
        except (sqlite3.Error, ValueError, TypeError, StorageError) as exc:
            self._rollback_quietly()
            raise StorageError("could not save Intent application") from exc

    def load_plan_application(self, model_request_id: str) -> IntentPlanApplication | None:
        payload = self._load_intent_application("intent_plan_applications", model_request_id)
        if payload is None:
            return None
        try:
            payload["steps"] = tuple(PlanStep.from_dict(step) for step in payload["steps"])
            return IntentPlanApplication(**payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageError("invalid Plan application") from exc

    def load_action_application(self, model_request_id: str) -> IntentActionApplication | None:
        payload = self._load_intent_application("intent_action_applications", model_request_id)
        if payload is None:
            return None
        try:
            payload["action"] = parse_intent_action(payload["action"])
            return IntentActionApplication(**payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise StorageError("invalid action application") from exc

    def _load_intent_application(self, table: str, model_request_id: str) -> dict | None:
        try:
            row = self._connection.execute(
                f"SELECT * FROM {table} WHERE model_request_id = ?", (model_request_id,)
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["application_json"])
            if any(payload[field] != row[field] for field in (
                "model_request_id", "task_run_id", "intent_id", "plan_id"
            )):
                raise ValueError("application ownership changed")
            return payload
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise StorageError("could not read Intent application") from exc

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

    def committed_mutations_after(
        self, task_run_id: str, checkpoint_sequence: int,
    ) -> tuple[tuple[str, ControlCommitResult], ...]:
        if type(checkpoint_sequence) is not int or checkpoint_sequence < 1:
            raise ValueError("invalid_action_checkpoint")
        try:
            rows = self._connection.execute(
                "SELECT mutation_id, event_sequence, checkpoint_sequence "
                "FROM control_mutations WHERE task_run_id = ? "
                "AND checkpoint_sequence > ? ORDER BY checkpoint_sequence",
                (task_run_id, checkpoint_sequence),
            ).fetchall()
            return tuple(
                (row["mutation_id"], self._load_commit_result(
                    task_run_id, row["event_sequence"], row["checkpoint_sequence"],
                    replayed=True,
                )) for row in rows
            )
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("could not reconcile action commits") from exc

    def load_intent_goal(self, intent_id: str) -> PersistedIntentGoal | None:
        try:
            row = self._connection.execute(
                """
                SELECT g.task_run_id, g.goal_type, g.path, g.created_event_sequence,
                       m.checkpoint_sequence
                FROM intent_goals g
                JOIN control_mutations m
                  ON m.task_run_id = g.task_run_id
                 AND m.event_sequence = g.created_event_sequence
                WHERE g.intent_id = ?
                """,
                (intent_id,),
            ).fetchone()
            if row is None:
                exists = self._connection.execute(
                    "SELECT 1 FROM intent_goals WHERE intent_id = ?", (intent_id,)
                ).fetchone()
                if exists is not None:
                    raise StorageError("Intent goal is missing its first commit")
                return None
            if row["goal_type"] != "file_type":
                raise StorageError("unsupported persisted Intent goal type")
            goal = FileTypeIntentGoal(row["task_run_id"], intent_id, row["path"])
            commit = self._load_commit_result(
                goal.task_run_id,
                int(row["created_event_sequence"]),
                int(row["checkpoint_sequence"]),
                replayed=True,
            )
            if (
                commit.event.details.get("operation") != "bind_intent_goal"
                or commit.event.details.get("result") != intent_id
                or not isinstance(commit.event.details.get("import_id"), str)
            ):
                raise StorageError("Intent goal first commit is invalid")
            return PersistedIntentGoal(goal, commit)
        except StorageError:
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageError("could not load Intent goal") from exc

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
                ORDER BY turns.sequence, intents.priority, plans.sequence
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
                    ORDER BY turns.sequence, intents.priority, plans.sequence,
                             executions.rowid
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
                    verification_ref=row["verification_ref"],
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
            source_root=row["source_root"],
            target_network=(
                TargetNetworkBinding.from_dict(json.loads(row["target_network_json"]))
                if row["target_network_json"] is not None
                else None
            ),
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
            UpsertTaskRun, UpsertTurn, UpsertIntent, UpsertIntentGoal, UpsertPlan, UpsertExecution,
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
            elif isinstance(change, UpsertIntentGoal):
                self._insert_intent_goal(change, event_sequence)
            elif isinstance(change, UpsertPlan):
                self._upsert_plan(change)
            elif isinstance(change, UpsertExecution):
                self._upsert_execution(change)
            elif isinstance(change, UpsertFact):
                if mutation.operation in {"commit_verified_file_type_fact", "commit_verified_t6_observation", "commit_verified_t6_fact"} and self._connection.execute(
                    "SELECT 1 FROM facts WHERE fact_id = ?", (change.fact_id,)
                ).fetchone() is not None:
                    raise StorageError("file-type Fact commit must insert a new Fact")
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
        elif isinstance(change, UpsertIntentGoal):
            owner = self._task_run_for_intent(change.goal.intent_id)
            if change.goal.task_run_id != task_run_id:
                raise StorageError("Intent goal belongs to another TaskRun")
        elif isinstance(change, UpsertPlan):
            owner = self._task_run_for_intent(change.intent_id)
        elif isinstance(change, UpsertExecution):
            owner = self._task_run_for_plan(change.plan_id)
        elif isinstance(change, UpsertFact):
            owner = self._task_run_for_turn(change.turn_id)
            if self._task_run_for_execution(change.source_ref) != task_run_id:
                raise StorageError("Fact source Execution belongs to another TaskRun")
        elif isinstance(change, AppendFailureSummary):
            model_error_for_intent = (
                change.failure_type.value == "model_error"
                and self._task_run_for_intent(change.evidence_ref) == task_run_id
            )
            if not model_error_for_intent and not self._failure_evidence_exists(
                task_run_id, change.evidence_ref
            ):
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
                   sandbox_id, source_root, target_network_json
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
            change.source_root,
            (
                canonical_json(change.target_network.to_dict())
                if change.target_network is not None
                else None
            ),
        )
        if previous is not None and tuple(previous) != immutable_values:
            raise StorageError("immutable TaskRun fields cannot be modified")
        self._connection.execute(
            """
            INSERT INTO task_runs(
                task_run_id, request_json, goal, target_json, deadline_seconds,
                effective_model_backend, started_at, deadline_at, status, sandbox_id,
                confirmation_deadline, finished_at, failure_reason, source_root,
                target_network_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                change.source_root,
                (
                    canonical_json(change.target_network.to_dict())
                    if change.target_network is not None
                    else None
                ),
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

    def _insert_intent_goal(
        self, change: UpsertIntentGoal, event_sequence: int
    ) -> None:
        goal = change.goal
        row = self._connection.execute(
            """
            SELECT intents.status, turns.task_run_id,
                   EXISTS(SELECT 1 FROM plans WHERE plans.intent_id = intents.intent_id)
                   AS has_plan
            FROM intents JOIN turns ON turns.turn_id = intents.turn_id
            WHERE intents.intent_id = ?
            """,
            (goal.intent_id,),
        ).fetchone()
        if (
            row is None
            or row["task_run_id"] != goal.task_run_id
            or row["status"] != IntentStatus.RUNNING.value
            or row["has_plan"]
        ):
            raise StorageError("Intent goal requires a running Intent before Plan")
        if self._connection.execute(
            "SELECT 1 FROM intent_goals WHERE intent_id = ?", (goal.intent_id,)
        ).fetchone() is not None:
            raise StorageError("Intent goal is immutable after first binding")
        self._connection.execute(
            """
            INSERT INTO intent_goals(
                intent_id, task_run_id, goal_type, path, created_event_sequence
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                goal.intent_id,
                goal.task_run_id,
                goal.goal_type,
                goal.path,
                event_sequence,
            ),
        )

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
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO plans(plan_id, intent_id, sequence, status)
                SELECT ?, ?, COALESCE(MAX(sequence), 0) + 1, ?
                FROM plans WHERE intent_id = ?
                """,
                (change.plan_id, change.intent_id, change.status.value, change.intent_id),
            )
        else:
            self._connection.execute(
                "UPDATE plans SET status = ? WHERE plan_id = ?",
                (change.status.value, change.plan_id),
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
            "SELECT turn_id, source_ref, verification_ref FROM facts WHERE fact_id = ?",
            (change.fact_id,),
        ).fetchone()
        if existing is not None and tuple(existing) != (
            change.turn_id,
            change.source_ref,
            change.verification_ref,
        ):
            raise StorageError("Fact owner, source_ref and verification_ref cannot be modified")
        self._connection.execute(
            """
            INSERT INTO facts(
                fact_id, turn_id, content, source_ref, status,
                created_event_sequence, created_event_ordinal, verification_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                change.verification_ref,
            ),
        )

    def _append_failure(
        self,
        task_run_id: str,
        change: AppendFailureSummary,
        event_sequence: int,
        ordinal: int,
    ) -> None:
        model_error_for_intent = (
            change.failure_type.value == "model_error"
            and self._task_run_for_intent(change.evidence_ref) == task_run_id
        )
        if not model_error_for_intent and not self._failure_evidence_exists(
            task_run_id, change.evidence_ref
        ):
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
