from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from agent_core.action_audit import (
    ActionAuditConflict,
    ActionInvocationCompletion,
    ActionInvocationRecord,
    ActionInvocationStart,
    ActionRecoveryRequired,
    ActionResourceIds,
)
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_candidate import (
    project_candidate,
    validate_control_ownership,
    validate_running_transition,
)
from agent_core.control_store import (
    AppendD2ReviewApplication,
    AppendFailureSummary,
    AppendPlanValidationRecord,
    Checkpoint,
    ControlCommitResult,
    ControlMutation,
    ControlStateRows,
    D2RunConfig,
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
    TargetNetworkBinding,
    TaskRunSnapshot,
    TurnSnapshot,
    UpsertExecution,
    UpsertFact,
    UpsertIntent,
    UpsertIntentGoal,
    UpsertPlan,
    UpsertTaskRun,
    UpsertTurn,
    active_d2_pause_gates,
    canonical_json,
    control_mutation_fingerprint,
    task_request_payload,
    validate_control_event,
)
from agent_core.v7.web_contracts import (
    D7WebExecutionRecord,
    D7WebPlanRecord,
    D7WebRunConfig,
    canonical_web_origin,
)
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
from agent_core.dual_llm_contracts import (
    D3_CATEGORY_LIMITS,
    D3_SHA_STEP,
    D3_SIZE_STEP,
    D3BudgetExhausted,
    D3CommitMetadata,
    D3IntentBinding,
    D3OperationCompletion,
    D3OperationConflict,
    D3OperationRecord,
    D3OperationStart,
    D3Outcome,
    D3PlanBinding,
    D3PlanResult,
    D3RecoveryRequired,
    D3ReviewApplication,
    D3RunConfig,
    D3TurnBinding,
    d3_payload,
)
from agent_core.goal_candidates import D7AnswerCandidate, D7WebExecutionBinding
from agent_core.goal_feedback import (
    ExternalValidationCheckRecord,
    ExternalValidationCheckRequest,
    ExternalValidationCheckResult,
    ExternalValidationFeedbackRecord,
    external_validation_feedback_from_dict,
    project_candidate_feedback,
    validate_external_check_binding,
)
from agent_core.intent_action_application import (
    IntentActionApplication,
    IntentActionApplicationConflict,
    IntentPlanApplication,
)
from agent_core.intent_goals import FileTypeIntentGoal
from agent_core.model_invocation_audit import (
    ModelInvocationBudgetExhausted,
    ModelInvocationCompletion,
    ModelInvocationConflict,
    ModelInvocationRecord,
    ModelInvocationRecoveryRequired,
    ModelInvocationStart,
    result_from_payload,
    result_payload,
)
from agent_core.modeling import (
    IntentActionResponse,
    ModelRequestType,
    PlanGenerationResponse,
    parse_intent_action,
)
from agent_core.operation_accounting import (
    D4AuditCategory,
    D4AuditEvent,
    D4AuditOutcome,
    D4BudgetExhausted,
    D4BudgetPolicy,
    D4BudgetUsage,
    D4OperationCategory,
    D4OperationCompletion,
    D4OperationConflict,
    D4OperationRecord,
    D4OperationStart,
    D4RecoveryRequired,
)
from agent_core.process_facts import D5Attempt, D5ProcessFact
from agent_core.conclusion_contracts import D8Conclusion
from agent_core.handoff_storage import (
    D8_HANDOFF_SCHEMA, D8_REVISION_SCHEMA, D8_SESSION_SCHEMA, D8_CHANGES, D8_OPERATIONS,
    load_records, validate_handoff_mutation, save_handoff_mutation,
)
from agent_core.review_contracts import (
    D2AllowedActionKinds,
    D2Constraint,
    D2MaxActionTimeout,
    d2_payload,
)
from agent_core.runtime import TERMINAL_STATUSES, RuntimeEvent, TaskRunStatus
from agent_core.turn_contracts import (
    D1_CATEGORY_LIMITS,
    D1BudgetExhausted,
    D1OperationCompletion,
    D1OperationConflict,
    D1OperationRecord,
    D1OperationStart,
    D1RecoveryRequired,
    D1RunConfig,
    D1RoleConfig,
    D1TurnBinding,
    D1TurnProof,
    d1_payload,
)

CURRENT_SCHEMA_VERSION = 27

D8_OUTPUT_SCHEMA = """
CREATE TABLE d8_refinement_corrections (
    model_request_id TEXT PRIMARY KEY REFERENCES d4_operations(operation_id),
    root_model_request_id TEXT NOT NULL UNIQUE REFERENCES d5_attempts(model_request_id),
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    attempt_json TEXT NOT NULL
);
CREATE TABLE d8_refinement_process_facts (
    process_fact_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    version INTEGER NOT NULL CHECK (version > 0),
    model_request_id TEXT NOT NULL UNIQUE REFERENCES d8_refinement_corrections(model_request_id),
    metadata_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL,
    UNIQUE(task_run_id, version)
);
"""

MIGRATION_23 = """
CREATE TABLE d8_conclusions (
    record_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    plan_id TEXT NOT NULL REFERENCES d3_plan_bindings(plan_id),
    proposal_request_id TEXT NOT NULL UNIQUE REFERENCES d4_operations(operation_id),
    review_request_id TEXT NOT NULL UNIQUE REFERENCES d4_operations(operation_id),
    fact_id TEXT UNIQUE REFERENCES facts(fact_id),
    metadata_json TEXT NOT NULL,
    event_sequence INTEGER NOT NULL
);
CREATE INDEX idx_d8_conclusions_run ON d8_conclusions(task_run_id, event_sequence);
"""

MIGRATION_14 = """
CREATE TABLE d4_operations (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    category TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    charged INTEGER NOT NULL CHECK (charged IN (0, 1)),
    start_json TEXT NOT NULL,
    completion_json TEXT,
    application_ref TEXT
);
CREATE INDEX idx_d4_operations_task_category
ON d4_operations(task_run_id, category);
CREATE TABLE d4_audit_events (
    event_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    category TEXT NOT NULL,
    outcome TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    related_ref TEXT,
    event_json TEXT NOT NULL
);
CREATE INDEX idx_d4_audit_events_task_category
ON d4_audit_events(task_run_id, category);
CREATE INDEX idx_d4_audit_events_related
ON d4_audit_events(task_run_id, related_ref);
"""

MIGRATION_15 = """
ALTER TABLE d4_operations
ADD COLUMN unknown_disposition_json TEXT;
CREATE TABLE d5_attempts (
    model_request_id TEXT PRIMARY KEY REFERENCES d4_operations(operation_id),
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    review_id TEXT NOT NULL REFERENCES d3_review_applications(review_id),
    attempt_json TEXT NOT NULL,
    UNIQUE(task_run_id, review_id)
);
CREATE TABLE process_facts (
    process_fact_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    version INTEGER NOT NULL CHECK (version > 0),
    model_request_id TEXT NOT NULL UNIQUE REFERENCES d5_attempts(model_request_id),
    metadata_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL,
    UNIQUE(task_run_id, version)
);
"""

MIGRATION_16 = """
CREATE TABLE d7_answer_candidates (
    candidate_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    plan_id TEXT NOT NULL REFERENCES d3_plan_bindings(plan_id),
    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
    evidence_version TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL,
    UNIQUE(task_run_id, candidate_id)
);
CREATE INDEX idx_d7_candidates_task_turn
ON d7_answer_candidates(task_run_id, turn_id);
CREATE TABLE d7_validation_feedback (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    candidate_id TEXT NOT NULL REFERENCES d7_answer_candidates(candidate_id),
    evidence_version TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted', 'rejected', 'unknown')),
    feedback_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE INDEX idx_d7_feedback_candidate_version
ON d7_validation_feedback(task_run_id, candidate_id, evidence_version);
"""

MIGRATION_11 = """
CREATE TABLE d1_runs (
    task_run_id TEXT PRIMARY KEY REFERENCES task_runs(task_run_id),
    config_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d1_turn_bindings (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    task_run_id TEXT NOT NULL REFERENCES d1_runs(task_run_id),
    intent_id TEXT NOT NULL UNIQUE REFERENCES intents(intent_id),
    binding_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d1_turn_proofs (
    turn_id TEXT PRIMARY KEY REFERENCES d1_turn_bindings(turn_id),
    proof_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d1_operations (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d1_runs(task_run_id),
    category TEXT NOT NULL,
    resource_ref TEXT,
    start_json TEXT NOT NULL,
    completion_json TEXT,
    application_ref TEXT
);
"""

MIGRATION_17 = """
CREATE TABLE d7_external_validation_checks (
    query_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    candidate_id TEXT NOT NULL REFERENCES d7_answer_candidates(candidate_id),
    submission_operation_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence > 0),
    request_json TEXT NOT NULL,
    started_event_sequence INTEGER NOT NULL,
    result_json TEXT,
    result_event_sequence INTEGER,
    UNIQUE(task_run_id, query_id),
    CHECK ((result_json IS NULL) = (result_event_sequence IS NULL))
);
CREATE INDEX idx_d7_external_checks_candidate
ON d7_external_validation_checks(task_run_id, candidate_id, evidence_version);
"""

MIGRATION_18 = """
CREATE TABLE d7_web_run_configs (
    task_run_id TEXT PRIMARY KEY REFERENCES task_runs(task_run_id),
    request_id TEXT NOT NULL UNIQUE,
    config_ref TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
"""

MIGRATION_19 = """
CREATE TABLE d7_candidate_registry (
    candidate_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('d3', 'web')),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    plan_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL,
    UNIQUE(task_run_id, candidate_id)
);
CREATE INDEX idx_d7_candidate_registry_task_turn
ON d7_candidate_registry(task_run_id, turn_id);
INSERT INTO d7_candidate_registry(
    candidate_id, task_run_id, source_kind, turn_id, intent_id, plan_id,
    execution_id, evidence_version, source_ref, candidate_json,
    created_event_sequence
)
SELECT candidate_id, task_run_id, 'd3', turn_id, intent_id, plan_id,
       execution_id, evidence_version, source_ref, candidate_json,
       created_event_sequence
FROM d7_answer_candidates;

ALTER TABLE d7_validation_feedback RENAME TO d7_validation_feedback_d3_fk;
CREATE TABLE d7_validation_feedback (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    candidate_id TEXT NOT NULL REFERENCES d7_candidate_registry(candidate_id),
    evidence_version TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted', 'rejected', 'unknown')),
    feedback_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
INSERT INTO d7_validation_feedback
SELECT operation_id, task_run_id, candidate_id, evidence_version, verdict,
       feedback_json, created_event_sequence
FROM d7_validation_feedback_d3_fk;
DROP TABLE d7_validation_feedback_d3_fk;
CREATE INDEX idx_d7_feedback_candidate_version
ON d7_validation_feedback(task_run_id, candidate_id, evidence_version);

ALTER TABLE d7_external_validation_checks RENAME TO d7_external_validation_checks_d3_fk;
CREATE TABLE d7_external_validation_checks (
    query_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    candidate_id TEXT NOT NULL REFERENCES d7_candidate_registry(candidate_id),
    submission_operation_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence > 0),
    request_json TEXT NOT NULL,
    started_event_sequence INTEGER NOT NULL,
    result_json TEXT,
    result_event_sequence INTEGER,
    UNIQUE(task_run_id, query_id),
    CHECK ((result_json IS NULL) = (result_event_sequence IS NULL))
);
INSERT INTO d7_external_validation_checks
SELECT query_id, task_run_id, candidate_id, submission_operation_id,
       evidence_version, checkpoint_sequence, request_json,
       started_event_sequence, result_json, result_event_sequence
FROM d7_external_validation_checks_d3_fk;
DROP TABLE d7_external_validation_checks_d3_fk;
CREATE INDEX idx_d7_external_checks_candidate
ON d7_external_validation_checks(task_run_id, candidate_id, evidence_version);
"""

MIGRATION_20 = """
CREATE TABLE d7_web_plans (
    plan_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d7_web_run_configs(task_run_id),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    method TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    request_bytes INTEGER NOT NULL CHECK (request_bytes BETWEEN 0 AND 65536),
    created_at REAL NOT NULL,
    record_json TEXT NOT NULL,
    UNIQUE(task_run_id, plan_id)
);
CREATE INDEX idx_d7_web_plans_task_turn
ON d7_web_plans(task_run_id, turn_id);
CREATE TABLE d7_web_executions (
    execution_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d7_web_run_configs(task_run_id),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    plan_id TEXT NOT NULL,
    http_operation_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    request_bytes INTEGER NOT NULL CHECK (request_bytes BETWEEN 0 AND 65536),
    started_at REAL NOT NULL,
    start_json TEXT NOT NULL,
    UNIQUE(task_run_id, execution_id),
    FOREIGN KEY(task_run_id, plan_id)
        REFERENCES d7_web_plans(task_run_id, plan_id)
);
CREATE INDEX idx_d7_web_executions_task_turn
ON d7_web_executions(task_run_id, turn_id);
CREATE TABLE d7_web_execution_results (
    execution_id TEXT PRIMARY KEY REFERENCES d7_web_executions(execution_id),
    task_run_id TEXT NOT NULL REFERENCES d7_web_run_configs(task_run_id),
    state TEXT NOT NULL CHECK (state IN ('completed', 'rejected', 'unknown')),
    http_status INTEGER,
    response_evidence_id TEXT,
    response_sha256 TEXT,
    response_bytes INTEGER,
    finished_at REAL NOT NULL,
    result_json TEXT NOT NULL,
    CHECK (
        (state = 'completed'
            AND http_status BETWEEN 100 AND 599
            AND response_evidence_id IS NOT NULL
            AND response_sha256 IS NOT NULL
            AND response_bytes BETWEEN 0 AND 2097152)
        OR
        (state IN ('rejected', 'unknown')
            AND http_status IS NULL
            AND response_evidence_id IS NULL
            AND response_sha256 IS NULL
            AND response_bytes IS NULL)
    )
);
CREATE UNIQUE INDEX idx_d7_web_response_evidence
ON d7_web_execution_results(task_run_id, response_evidence_id)
WHERE response_evidence_id IS NOT NULL;
"""

MIGRATION_21 = """
CREATE TABLE d4_operations_new (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    category TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    charged INTEGER NOT NULL CHECK (charged IN (0, 1)),
    start_json TEXT NOT NULL,
    completion_json TEXT,
    application_ref TEXT,
    unknown_disposition_json TEXT
);
INSERT INTO d4_operations_new(
    operation_id, task_run_id, category, resource_ref, charged, start_json,
    completion_json, application_ref, unknown_disposition_json
)
SELECT operation_id, task_run_id, category, resource_ref, charged, start_json,
       completion_json, application_ref, unknown_disposition_json
FROM d4_operations;
DROP TABLE d4_operations;
ALTER TABLE d4_operations_new RENAME TO d4_operations;
CREATE INDEX idx_d4_operations_task_category
ON d4_operations(task_run_id, category);

CREATE TABLE d4_audit_events_new (
    event_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
    category TEXT NOT NULL,
    outcome TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    related_ref TEXT,
    event_json TEXT NOT NULL
);
INSERT INTO d4_audit_events_new(
    event_id, task_run_id, category, outcome, occurred_at, evidence_ref,
    evidence_sha256, related_ref, event_json
)
SELECT event_id, task_run_id, category, outcome, occurred_at, evidence_ref,
       evidence_sha256, related_ref, event_json
FROM d4_audit_events;
DROP TABLE d4_audit_events;
ALTER TABLE d4_audit_events_new RENAME TO d4_audit_events;
CREATE INDEX idx_d4_audit_events_task_category
ON d4_audit_events(task_run_id, category);
CREATE INDEX idx_d4_audit_events_related
ON d4_audit_events(task_run_id, related_ref);
"""

MIGRATION_22 = """
ALTER TABLE turns ADD COLUMN parent_turn_id TEXT REFERENCES turns(turn_id);
ALTER TABLE turns ADD COLUMN triggering_intent_id TEXT REFERENCES intents(intent_id);
CREATE INDEX idx_turns_parent ON turns(parent_turn_id) WHERE parent_turn_id IS NOT NULL;
CREATE UNIQUE INDEX idx_turns_triggering_intent
ON turns(triggering_intent_id) WHERE triggering_intent_id IS NOT NULL;
CREATE TRIGGER turns_parent_metadata_pair_insert
BEFORE INSERT ON turns
WHEN (NEW.parent_turn_id IS NULL) != (NEW.triggering_intent_id IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'Turn parent metadata must be set together');
END;
CREATE TRIGGER turns_parent_metadata_pair_update
BEFORE UPDATE OF parent_turn_id, triggering_intent_id ON turns
WHEN (NEW.parent_turn_id IS NULL) != (NEW.triggering_intent_id IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'Turn parent metadata must be set together');
END;
CREATE TRIGGER turns_parent_reference_insert
BEFORE INSERT ON turns
WHEN NEW.parent_turn_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM turns AS current_turn WHERE current_turn.turn_id = NEW.turn_id
) AND NOT EXISTS (
    SELECT 1
    FROM turns AS parent
    JOIN intents AS trigger_intent
      ON trigger_intent.intent_id = NEW.triggering_intent_id
    WHERE parent.turn_id = NEW.parent_turn_id
      AND parent.task_run_id = NEW.task_run_id
      AND parent.status = 'running'
      AND trigger_intent.turn_id = parent.turn_id
      AND trigger_intent.status = 'succeeded'
)
BEGIN
    SELECT RAISE(ABORT, 'Turn parent reference is invalid');
END;
CREATE TRIGGER turns_parent_metadata_immutable
BEFORE UPDATE OF parent_turn_id, triggering_intent_id ON turns
WHEN NEW.parent_turn_id IS NOT OLD.parent_turn_id
  OR NEW.triggering_intent_id IS NOT OLD.triggering_intent_id
BEGIN
    SELECT RAISE(ABORT, 'Turn parent metadata is immutable');
END;
"""

MIGRATION_12 = """
CREATE TABLE d2_runs (
    task_run_id TEXT PRIMARY KEY REFERENCES d1_runs(task_run_id),
    config_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d2_review_applications (
    task_run_id TEXT NOT NULL REFERENCES d2_runs(task_run_id),
    review_root_id TEXT NOT NULL,
    review_generation INTEGER NOT NULL,
    review_id TEXT NOT NULL,
    application_ref TEXT NOT NULL,
    context_version TEXT NOT NULL,
    constraint_version INTEGER NOT NULL,
    output_constraints_json TEXT NOT NULL,
    paused_gate INTEGER NOT NULL CHECK (paused_gate IN (0, 1)),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    review_scope TEXT NOT NULL,
    action_candidate_id TEXT,
    pause_reason TEXT,
    created_event_sequence INTEGER NOT NULL,
    PRIMARY KEY (task_run_id, review_id),
    UNIQUE (task_run_id, application_ref)
);
CREATE INDEX idx_d2_review_applications_root
ON d2_review_applications(task_run_id, review_root_id, review_generation);
CREATE UNIQUE INDEX idx_d2_review_accepted_generation
ON d2_review_applications(task_run_id, review_root_id, review_generation)
WHERE paused_gate = 0;
"""

MIGRATION_13 = """
CREATE TABLE d3_runs (
    task_run_id TEXT PRIMARY KEY REFERENCES task_runs(task_run_id),
    config_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d3_turn_bindings (
    turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    binding_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d3_intent_bindings (
    intent_id TEXT PRIMARY KEY REFERENCES intents(intent_id),
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    turn_id TEXT NOT NULL REFERENCES d3_turn_bindings(turn_id),
    binding_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d3_plan_bindings (
    plan_id TEXT PRIMARY KEY REFERENCES plans(plan_id),
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    intent_id TEXT NOT NULL REFERENCES d3_intent_bindings(intent_id),
    binding_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d3_plan_results (
    plan_id TEXT PRIMARY KEY REFERENCES d3_plan_bindings(plan_id),
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    result_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d3_outcomes (
    entity_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    entity_kind TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE TABLE d3_operations (
    operation_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    category TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    start_json TEXT NOT NULL,
    completion_json TEXT,
    application_ref TEXT
);
CREATE INDEX idx_d3_operations_task_category ON d3_operations(task_run_id, category);
CREATE TABLE d3_review_applications (
    review_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES d3_runs(task_run_id),
    review_root_id TEXT NOT NULL,
    review_generation INTEGER NOT NULL,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    decision TEXT NOT NULL,
    paused_gate INTEGER NOT NULL CHECK (paused_gate IN (0, 1)),
    constraint_version INTEGER NOT NULL,
    application_json TEXT NOT NULL,
    created_event_sequence INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_d3_review_accepted_generation
ON d3_review_applications(task_run_id, review_root_id, review_generation)
WHERE paused_gate = 0;
"""


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
        self._schema_version = 0

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
            store._schema_version = CURRENT_SCHEMA_VERSION
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
        """Read supported history without migrations or journal changes."""
        database_path = Path(path).resolve(strict=True)
        connection = sqlite3.connect(
            database_path.as_uri() + "?mode=ro", uri=True, timeout=0.1,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
            if not isinstance(version, int) or not 10 <= version <= CURRENT_SCHEMA_VERSION:
                raise StorageError(
                    f"read-only history requires supported schema 10 through {CURRENT_SCHEMA_VERSION}"
                )
            connection.execute("BEGIN")
            store = cls(connection, database_path, clock=time.time)
            store._schema_version = int(version)
            return store
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
            if version < 11:
                for statement in _sql_statements(MIGRATION_11):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (11, self._now()),
                )
            if version < 12:
                for statement in _sql_statements(MIGRATION_12):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (12, self._now()),
                )
            if version < 13:
                for statement in _sql_statements(MIGRATION_13):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (13, self._now()),
                )
            if version < 14:
                for statement in _sql_statements(MIGRATION_14):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (14, self._now()),
                )
            if version < 15:
                for statement in _sql_statements(MIGRATION_15):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (15, self._now()),
                )
            if version < 16:
                for statement in _sql_statements(MIGRATION_16):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (16, self._now()),
                )
            if version < 17:
                for statement in _sql_statements(MIGRATION_17):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (17, self._now()),
                )
            if version < 18:
                for statement in _sql_statements(MIGRATION_18):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (18, self._now()),
                )
            if version < 19:
                for statement in _sql_statements(MIGRATION_19):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (19, self._now()),
                )
            if version < 20:
                for statement in _sql_statements(MIGRATION_20):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (20, self._now()),
                )
            if version < 21:
                for statement in _sql_statements(MIGRATION_21):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (21, self._now()),
                )
            if version < 22:
                for statement in _sql_statements(MIGRATION_22):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (22, self._now()),
                )
            if version < 23:
                for statement in _sql_statements(MIGRATION_23):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                    (23, self._now()),
                )
            if version < 24:
                for statement in _sql_statements(D8_HANDOFF_SCHEMA):
                    self._connection.execute(statement)
                self._connection.execute("INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                                         (24, self._now()))
            if version < 25:
                for statement in _sql_statements(D8_REVISION_SCHEMA):
                    self._connection.execute(statement)
                self._connection.execute("INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                                         (25, self._now()))
            if version < 26:
                for statement in _sql_statements(D8_SESSION_SCHEMA):
                    self._connection.execute(statement)
                self._connection.execute("INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                                         (26, self._now()))
            if version < 27:
                for statement in _sql_statements(D8_OUTPUT_SCHEMA):
                    self._connection.execute(statement)
                self._connection.execute("INSERT INTO schema_versions(version, applied_at) VALUES (?, ?)",
                                         (27, self._now()))
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

    def load_d1_run_config(self, task_run_id: str) -> D1RunConfig | None:
        if self._schema_version < 11:
            return None
        row = self._connection.execute("SELECT config_json FROM d1_runs WHERE task_run_id = ?", (task_run_id,)).fetchone()
        return None if row is None else D1RunConfig.from_dict(json.loads(row[0]))

    def load_d1_turn_binding(self, turn_id: str) -> D1TurnBinding | None:
        if self._schema_version < 11:
            return None
        row = self._connection.execute("SELECT binding_json FROM d1_turn_bindings WHERE turn_id = ?", (turn_id,)).fetchone()
        return None if row is None else D1TurnBinding.from_dict(json.loads(row[0]))

    def load_d1_turn_proof(self, turn_id: str) -> D1TurnProof | None:
        if self._schema_version < 11:
            return None
        row = self._connection.execute("SELECT proof_json FROM d1_turn_proofs WHERE turn_id = ?", (turn_id,)).fetchone()
        return None if row is None else D1TurnProof.from_dict(json.loads(row[0]))

    @staticmethod
    def _decode_d1_operation(row: sqlite3.Row) -> D1OperationRecord:
        return D1OperationRecord(
            D1OperationStart(**json.loads(row["start_json"])),
            D1OperationCompletion(**json.loads(row["completion_json"])) if row["completion_json"] is not None else None,
            row["application_ref"],
        )

    def load_d1_operation(self, operation_id: str) -> D1OperationRecord | None:
        if self._schema_version < 11:
            return None
        row = self._connection.execute("SELECT * FROM d1_operations WHERE operation_id = ?", (operation_id,)).fetchone()
        return None if row is None else self._decode_d1_operation(row)

    def list_d1_operations(self, task_run_id: str) -> tuple[D1OperationRecord, ...]:
        if self._schema_version < 11:
            return ()
        rows = self._connection.execute("SELECT * FROM d1_operations WHERE task_run_id = ? ORDER BY rowid", (task_run_id,)).fetchall()
        return tuple(self._decode_d1_operation(row) for row in rows)

    @staticmethod
    def _decode_d2_constraints(value: object) -> tuple[D2Constraint, ...]:
        if not isinstance(value, list):
            raise StorageError("D2 review constraints are not a JSON array")
        constraints: list[D2Constraint] = []
        for item in value:
            if not isinstance(item, dict):
                raise StorageError("D2 review constraint is not a JSON object")
            kind = item.get("kind")
            if kind == "allowed_action_kinds":
                if set(item) != {"kind", "scope_kind", "scope_id", "allowed_action_kinds"}:
                    raise StorageError("D2 stored action constraint has unexpected fields")
                if not isinstance(item["allowed_action_kinds"], list):
                    raise StorageError("D2 stored action kinds are not an array")
                constraints.append(
                    D2AllowedActionKinds(
                        item["scope_kind"], item["scope_id"],
                        tuple(item["allowed_action_kinds"]),
                    )
                )
            elif kind == "max_action_timeout_seconds":
                if set(item) != {"kind", "scope_kind", "scope_id", "max_action_timeout_seconds"}:
                    raise StorageError("D2 stored timeout constraint has unexpected fields")
                constraints.append(
                    D2MaxActionTimeout(
                        item["scope_kind"], item["scope_id"],
                        item["max_action_timeout_seconds"],
                    )
                )
            else:
                raise StorageError("unsupported D2 review constraint kind")
        return tuple(constraints)

    @classmethod
    def _decode_d2_review_application(cls, row: sqlite3.Row) -> AppendD2ReviewApplication:
        if row["paused_gate"] not in {0, 1}:
            raise StorageError("D2 paused gate is not a stored boolean")
        return AppendD2ReviewApplication(
            row["review_root_id"], row["review_generation"], row["review_id"],
            row["application_ref"], row["context_version"], row["constraint_version"],
            cls._decode_d2_constraints(json.loads(row["output_constraints_json"])),
            bool(row["paused_gate"]),
            row["turn_id"], row["intent_id"], row["review_scope"],
            row["action_candidate_id"], row["pause_reason"],
        )

    def load_d2_run_config(self, task_run_id: str) -> D2RunConfig | None:
        if self._schema_version < 12:
            return None
        row = self._connection.execute(
            "SELECT config_json FROM d2_runs WHERE task_run_id = ?", (task_run_id,)
        ).fetchone()
        return None if row is None else D2RunConfig(**json.loads(row["config_json"]))

    def load_d2_review_application(
        self, task_run_id: str, review_id: str
    ) -> AppendD2ReviewApplication | None:
        if self._schema_version < 12:
            return None
        row = self._connection.execute(
            "SELECT * FROM d2_review_applications WHERE task_run_id = ? AND review_id = ?",
            (task_run_id, review_id),
        ).fetchone()
        return None if row is None else self._decode_d2_review_application(row)

    def list_d2_review_applications(
        self, task_run_id: str
    ) -> tuple[AppendD2ReviewApplication, ...]:
        if self._schema_version < 12:
            return ()
        rows = self._connection.execute(
            "SELECT * FROM d2_review_applications WHERE task_run_id = ? ORDER BY created_event_sequence",
            (task_run_id,),
        ).fetchall()
        return tuple(self._decode_d2_review_application(row) for row in rows)

    def load_d3_run_config(self, task_run_id: str) -> D3RunConfig | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT config_json FROM d3_runs WHERE task_run_id = ?", (task_run_id,)
        ).fetchone()
        return None if row is None else D3RunConfig.from_dict(json.loads(row["config_json"]))

    def load_d3_turn_binding(self, turn_id: str) -> D3TurnBinding | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT binding_json FROM d3_turn_bindings WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return None if row is None else D3TurnBinding(**json.loads(row["binding_json"]))

    def load_d3_intent_binding(self, intent_id: str) -> D3IntentBinding | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT binding_json FROM d3_intent_bindings WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return None if row is None else D3IntentBinding(**json.loads(row["binding_json"]))

    def load_d3_plan_binding(self, plan_id: str) -> D3PlanBinding | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT binding_json FROM d3_plan_bindings WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return None if row is None else D3PlanBinding(**json.loads(row["binding_json"]))

    def load_d3_plan_result(self, plan_id: str) -> D3PlanResult | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT result_json FROM d3_plan_results WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return None if row is None else D3PlanResult(**json.loads(row["result_json"]))

    def load_d3_outcome(self, entity_id: str) -> D3Outcome | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT outcome_json FROM d3_outcomes WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["outcome_json"])
        value["evidence_refs"] = tuple(value["evidence_refs"])
        return D3Outcome(**value)

    def load_d3_operation(self, operation_id: str) -> D3OperationRecord | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT * FROM d3_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        start = D3OperationStart(**json.loads(row["start_json"]))
        completion = (
            D3OperationCompletion(**json.loads(row["completion_json"]))
            if row["completion_json"] is not None else None
        )
        if (
            start.task_run_id != row["task_run_id"]
            or start.category != row["category"]
            or start.resource_ref != row["resource_ref"]
            or completion is not None and completion.operation_id != start.operation_id
        ):
            raise StorageError("D3 operation row differs from its typed record")
        return D3OperationRecord(start, completion, row["application_ref"])

    def list_d3_operations(self, task_run_id: str) -> tuple[D3OperationRecord, ...]:
        if self._schema_version < 13:
            return ()
        rows = self._connection.execute(
            "SELECT operation_id FROM d3_operations WHERE task_run_id = ? ORDER BY rowid",
            (task_run_id,),
        ).fetchall()
        records = []
        for row in rows:
            record = self.load_d3_operation(row["operation_id"])
            if record is None:
                raise StorageError("D3 operation disappeared while listing")
            records.append(record)
        return tuple(records)

    def load_d7_answer_candidate(
        self, candidate_id: str
    ) -> D7AnswerCandidate | None:
        if self._schema_version < 16:
            return None
        if self._schema_version >= 19:
            row = self._connection.execute(
                "SELECT * FROM d7_candidate_registry WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT * FROM d7_answer_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            candidate = D7AnswerCandidate.from_dict(
                json.loads(row["candidate_json"])
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("stored D7 candidate is invalid") from exc
        if (
            candidate.candidate_id != row["candidate_id"]
            or candidate.task_run_id != row["task_run_id"]
            or candidate.turn_id != row["turn_id"]
            or candidate.intent_id != row["intent_id"]
            or candidate.plan_id != row["plan_id"]
            or candidate.execution_id != row["execution_id"]
            or candidate.evidence_version != row["evidence_version"]
            or candidate.source_ref != row["source_ref"]
            or self._schema_version >= 19
            and candidate.source_kind != row["source_kind"]
        ):
            raise StorageError("D7 candidate row differs from its typed record")
        return candidate

    def list_d7_answer_candidates(
        self, task_run_id: str
    ) -> tuple[D7AnswerCandidate, ...]:
        if self._schema_version < 16:
            return ()
        table = (
            "d7_candidate_registry"
            if self._schema_version >= 19
            else "d7_answer_candidates"
        )
        rows = self._connection.execute(
            f"SELECT candidate_id FROM {table} "
            "WHERE task_run_id = ? ORDER BY created_event_sequence, rowid",
            (task_run_id,),
        ).fetchall()
        candidates = tuple(
            self.load_d7_answer_candidate(row["candidate_id"]) for row in rows
        )
        if any(item is None for item in candidates):
            raise StorageError("D7 candidate disappeared while listing")
        return tuple(item for item in candidates if item is not None)

    def load_d7_web_run_config(self, task_run_id: str) -> D7WebRunConfig | None:
        if self._schema_version < 18:
            return None
        row = self._connection.execute(
            "SELECT request_id, config_ref, config_json FROM d7_web_run_configs "
            "WHERE task_run_id = ?",
            (task_run_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            config = D7WebRunConfig.from_dict(json.loads(row["config_json"]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("stored D7 Web run config is invalid") from exc
        if (
            config.task_run_id != task_run_id
            or config.request_id != row["request_id"]
            or config.config_ref != row["config_ref"]
        ):
            raise StorageError("D7 Web config row differs from its typed record")
        return config

    def load_d7_web_plan(self, plan_id: str) -> D7WebPlanRecord | None:
        if self._schema_version < 20:
            return None
        row = self._connection.execute(
            "SELECT * FROM d7_web_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            record = D7WebPlanRecord.from_dict(json.loads(row["record_json"]))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("stored D7 Web Plan is invalid") from exc
        if (
            record.plan_id != row["plan_id"]
            or record.task_run_id != row["task_run_id"]
            or record.turn_id != row["turn_id"]
            or record.intent_id != row["intent_id"]
            or record.method != row["method"]
            or record.request_sha256 != row["request_sha256"]
            or record.request_bytes != row["request_bytes"]
            or record.created_at != row["created_at"]
        ):
            raise StorageError("D7 Web Plan row differs from its typed record")
        return record

    def list_d7_web_plans(self, task_run_id: str) -> tuple[D7WebPlanRecord, ...]:
        if self._schema_version < 20:
            return ()
        rows = self._connection.execute(
            "SELECT plan_id FROM d7_web_plans WHERE task_run_id = ? "
            "ORDER BY created_at, rowid",
            (task_run_id,),
        ).fetchall()
        records = tuple(self.load_d7_web_plan(row["plan_id"]) for row in rows)
        if any(record is None for record in records):
            raise StorageError("D7 Web Plan disappeared while listing")
        return tuple(record for record in records if record is not None)

    def load_d7_web_execution(
        self, execution_id: str
    ) -> D7WebExecutionRecord | None:
        if self._schema_version < 20:
            return None
        row = self._connection.execute(
            """SELECT e.*, r.state AS result_state, r.http_status,
                      r.response_evidence_id, r.response_sha256,
                      r.response_bytes, r.finished_at, r.result_json
               FROM d7_web_executions AS e
               LEFT JOIN d7_web_execution_results AS r
                 ON r.execution_id = e.execution_id
               WHERE e.execution_id = ?""",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            started = D7WebExecutionRecord.from_dict(json.loads(row["start_json"]))
            record = (
                started
                if row["result_json"] is None
                else D7WebExecutionRecord.from_dict(json.loads(row["result_json"]))
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("stored D7 Web Execution is invalid") from exc
        immutable_fields = (
            "task_run_id", "turn_id", "intent_id", "plan_id", "execution_id",
            "http_operation_id", "request_sha256", "request_bytes", "started_at",
        )
        if (
            started.state != "started"
            or any(getattr(started, name) != getattr(record, name) for name in immutable_fields)
            or record.execution_id != row["execution_id"]
            or record.task_run_id != row["task_run_id"]
            or record.turn_id != row["turn_id"]
            or record.intent_id != row["intent_id"]
            or record.plan_id != row["plan_id"]
            or record.http_operation_id != row["http_operation_id"]
            or record.request_sha256 != row["request_sha256"]
            or record.request_bytes != row["request_bytes"]
            or record.started_at != row["started_at"]
        ):
            raise StorageError("D7 Web Execution row differs from its typed record")
        if row["result_json"] is not None and (
            record.state != row["result_state"]
            or record.http_status != row["http_status"]
            or record.response_evidence_id != row["response_evidence_id"]
            or record.response_sha256 != row["response_sha256"]
            or record.response_bytes != row["response_bytes"]
            or record.finished_at != row["finished_at"]
        ):
            raise StorageError("D7 Web Execution result row differs from its record")
        return record

    def list_d7_web_executions(
        self, task_run_id: str
    ) -> tuple[D7WebExecutionRecord, ...]:
        if self._schema_version < 20:
            return ()
        rows = self._connection.execute(
            "SELECT execution_id FROM d7_web_executions WHERE task_run_id = ? "
            "ORDER BY started_at, rowid",
            (task_run_id,),
        ).fetchall()
        records = tuple(
            self.load_d7_web_execution(row["execution_id"]) for row in rows
        )
        if any(record is None for record in records):
            raise StorageError("D7 Web Execution disappeared while listing")
        return tuple(record for record in records if record is not None)

    def load_d7_web_execution_binding(
        self, execution_id: str
    ) -> D7WebExecutionBinding | None:
        record = self.load_d7_web_execution(execution_id)
        if record is None or record.state != "completed":
            return None
        assert record.response_evidence_id is not None
        assert record.response_sha256 is not None
        assert record.response_bytes is not None
        return D7WebExecutionBinding(
            task_run_id=record.task_run_id,
            turn_id=record.turn_id,
            intent_id=record.intent_id,
            plan_id=record.plan_id,
            execution_id=record.execution_id,
            http_operation_id=record.http_operation_id,
            response_evidence_id=record.response_evidence_id,
            response_sha256=record.response_sha256,
            response_bytes=record.response_bytes,
        )

    def _validate_d7_web_scope(
        self, task_run_id: str, turn_id: str, intent_id: str
    ) -> D7WebRunConfig:
        config = self.load_d7_web_run_config(task_run_id)
        state = self.load_control_state_rows(task_run_id)
        if (
            config is None
            or self.load_d3_run_config(task_run_id) is not None
            or state is None
            or state.task_run.status.value != "running"
        ):
            raise StorageError("D7 Web operation requires a running Web TaskRun")
        turns = {item.turn_id: item for item in state.turns}
        intents = {item.intent_id: item for item in state.intents}
        turn = turns.get(turn_id)
        intent = intents.get(intent_id)
        if (
            turn is None
            or turn.task_run_id != task_run_id
            or turn.status is not TurnStatus.RUNNING
            or intent is None
            or intent.turn_id != turn_id
            or intent.status is not IntentStatus.RUNNING
        ):
            raise StorageError("D7 Web operation is outside its active Turn and Intent")
        return config

    @staticmethod
    def _same_d7_web_execution_start(
        left: D7WebExecutionRecord, right: D7WebExecutionRecord
    ) -> bool:
        return all(
            getattr(left, name) == getattr(right, name)
            for name in (
                "task_run_id", "turn_id", "intent_id", "plan_id", "execution_id",
                "http_operation_id", "request_sha256", "request_bytes", "started_at",
            )
        )

    def register_d7_web_plan(
        self, record: D7WebPlanRecord
    ) -> D7WebPlanRecord:
        if not isinstance(record, D7WebPlanRecord):
            raise TypeError("record must be D7WebPlanRecord")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.load_d7_web_plan(record.plan_id)
            if existing is not None:
                if existing != record:
                    raise IdempotencyConflictError("D7 Web Plan ID has different content")
                self._connection.commit()
                return existing
            config = self._validate_d7_web_scope(
                record.task_run_id, record.turn_id, record.intent_id
            )
            if record.method not in config.authorization.allowed_methods:
                raise StorageError("D7 Web Plan method is not authorized")
            self._connection.execute(
                """INSERT INTO d7_web_plans(
                       plan_id, task_run_id, turn_id, intent_id, method,
                       request_sha256, request_bytes, created_at, record_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.plan_id,
                    record.task_run_id,
                    record.turn_id,
                    record.intent_id,
                    record.method,
                    record.request_sha256,
                    record.request_bytes,
                    record.created_at,
                    canonical_json(record.to_dict()),
                ),
            )
            self._connection.commit()
            return record
        except BaseException:
            self._rollback_quietly()
            raise

    def start_d7_web_execution(
        self, record: D7WebExecutionRecord
    ) -> tuple[D7WebExecutionRecord, bool]:
        if not isinstance(record, D7WebExecutionRecord) or record.state != "started":
            raise TypeError("record must be a started D7WebExecutionRecord")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.load_d7_web_execution(record.execution_id)
            if existing is not None:
                if not self._same_d7_web_execution_start(existing, record):
                    raise IdempotencyConflictError(
                        "D7 Web Execution ID has different start content"
                    )
                self._connection.commit()
                return existing, False
            operation_row = self._connection.execute(
                "SELECT execution_id FROM d7_web_executions WHERE http_operation_id = ?",
                (record.http_operation_id,),
            ).fetchone()
            if operation_row is not None:
                raise IdempotencyConflictError(
                    "D7 HTTP operation ID is already bound to another Execution"
                )
            config = self._validate_d7_web_scope(
                record.task_run_id, record.turn_id, record.intent_id
            )
            plan = self.load_d7_web_plan(record.plan_id)
            if (
                plan is None
                or plan.task_run_id != record.task_run_id
                or plan.turn_id != record.turn_id
                or plan.intent_id != record.intent_id
                or plan.request_sha256 != record.request_sha256
                or plan.request_bytes != record.request_bytes
                or plan.method not in config.authorization.allowed_methods
            ):
                raise StorageError("D7 Web Execution does not match its saved Plan")
            self._connection.execute(
                """INSERT INTO d7_web_executions(
                       execution_id, task_run_id, turn_id, intent_id, plan_id,
                       http_operation_id, request_sha256, request_bytes, started_at,
                       start_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.execution_id,
                    record.task_run_id,
                    record.turn_id,
                    record.intent_id,
                    record.plan_id,
                    record.http_operation_id,
                    record.request_sha256,
                    record.request_bytes,
                    record.started_at,
                    canonical_json(record.to_dict()),
                ),
            )
            self._connection.commit()
            return record, True
        except BaseException:
            self._rollback_quietly()
            raise

    def complete_d7_web_execution(
        self, record: D7WebExecutionRecord
    ) -> D7WebExecutionRecord:
        if (
            not isinstance(record, D7WebExecutionRecord)
            or record.state not in {"completed", "rejected", "unknown"}
        ):
            raise TypeError("record must be a terminal D7WebExecutionRecord")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            started = self.load_d7_web_execution(record.execution_id)
            if started is None or not self._same_d7_web_execution_start(started, record):
                raise StorageError("D7 Web Execution completion has no matching start")
            row = self._connection.execute(
                "SELECT result_json FROM d7_web_execution_results WHERE execution_id = ?",
                (record.execution_id,),
            ).fetchone()
            if row is not None:
                existing = D7WebExecutionRecord.from_dict(json.loads(row["result_json"]))
                if existing != record:
                    raise IdempotencyConflictError(
                        "D7 Web Execution terminal result is immutable"
                    )
                self._connection.commit()
                return existing
            self._validate_d7_web_scope(
                record.task_run_id, record.turn_id, record.intent_id
            )
            self._connection.execute(
                """INSERT INTO d7_web_execution_results(
                       execution_id, task_run_id, state, http_status,
                       response_evidence_id, response_sha256, response_bytes,
                       finished_at, result_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.execution_id,
                    record.task_run_id,
                    record.state,
                    record.http_status,
                    record.response_evidence_id,
                    record.response_sha256,
                    record.response_bytes,
                    record.finished_at,
                    canonical_json(record.to_dict()),
                ),
            )
            self._connection.commit()
            return record
        except BaseException:
            self._rollback_quietly()
            raise

    def list_d7_validation_feedback(
        self, task_run_id: str, candidate_id: str, evidence_version: str,
        *, through_event_sequence: int | None = None,
    ) -> tuple[ExternalValidationFeedbackRecord, ...]:
        if through_event_sequence is not None and (
            type(through_event_sequence) is not int or through_event_sequence < 1
        ):
            raise ValueError("feedback event sequence must be a positive integer")
        if self._schema_version < 16:
            return ()
        boundary = (
            "" if through_event_sequence is None
            else "AND created_event_sequence <= ? "
        )
        parameters = (task_run_id, candidate_id, evidence_version)
        if through_event_sequence is not None:
            parameters += (through_event_sequence,)
        rows = self._connection.execute(
            "SELECT operation_id, feedback_json FROM d7_validation_feedback "
            "WHERE task_run_id = ? AND candidate_id = ? AND evidence_version = ? "
            + boundary + "ORDER BY created_event_sequence, rowid",
            parameters,
        ).fetchall()
        records = []
        for row in rows:
            try:
                feedback = external_validation_feedback_from_dict(
                    json.loads(row["feedback_json"])
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise StorageError("stored D7 feedback is invalid") from exc
            if (
                feedback.operation_id != row["operation_id"]
                or feedback.task_run_id != task_run_id
                or feedback.candidate_id != candidate_id
                or feedback.evidence_version != evidence_version
            ):
                raise StorageError("D7 feedback row differs from its typed record")
            records.append(feedback)
        return tuple(records)

    def load_d7_external_validation_check(
        self, query_id: str
    ) -> ExternalValidationCheckRecord | None:
        if self._schema_version < 17:
            return None
        row = self._connection.execute(
            "SELECT * FROM d7_external_validation_checks WHERE query_id = ?",
            (query_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            request = ExternalValidationCheckRequest.from_dict(
                json.loads(row["request_json"])
            )
            result = (
                None
                if row["result_json"] is None
                else ExternalValidationCheckResult.from_dict(
                    json.loads(row["result_json"])
                )
            )
            record = ExternalValidationCheckRecord(
                request=request,
                result=result,
                started_event_sequence=row["started_event_sequence"],
                result_event_sequence=row["result_event_sequence"],
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StorageError("stored D7 external validation check is invalid") from exc
        if (
            request.query_id != row["query_id"]
            or request.task_run_id != row["task_run_id"]
            or request.candidate_id != row["candidate_id"]
            or request.submission_operation_id != row["submission_operation_id"]
            or request.evidence_version != row["evidence_version"]
            or request.checkpoint_sequence != row["checkpoint_sequence"]
            or record.started_event_sequence <= 0
        ):
            raise StorageError("D7 external validation row differs from its request")
        return record

    def list_d7_external_validation_checks(
        self, task_run_id: str
    ) -> tuple[ExternalValidationCheckRecord, ...]:
        if self._schema_version < 17:
            return ()
        rows = self._connection.execute(
            "SELECT query_id FROM d7_external_validation_checks "
            "WHERE task_run_id = ? ORDER BY started_event_sequence, rowid",
            (task_run_id,),
        ).fetchall()
        records = tuple(
            self.load_d7_external_validation_check(row["query_id"]) for row in rows
        )
        if any(record is None for record in records):
            raise StorageError("D7 external validation check disappeared while listing")
        return tuple(record for record in records if record is not None)

    def load_d4_operation(self, operation_id: str) -> D4OperationRecord | None:
        if self._schema_version < 14:
            return None
        row = self._connection.execute(
            "SELECT * FROM d4_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        start = D4OperationStart(**json.loads(row["start_json"]))
        completion = (
            D4OperationCompletion(**json.loads(row["completion_json"]))
            if row["completion_json"] is not None else None
        )
        if (
            start.operation_id != row["operation_id"]
            or start.task_run_id != row["task_run_id"]
            or start.category != row["category"]
            or start.resource_ref != row["resource_ref"]
            or int(start.charged) != row["charged"]
            or completion is not None and completion.operation_id != start.operation_id
        ):
            raise StorageError("D4 operation row differs from its typed record")
        self._decode_d4_unknown_disposition(row, start, completion)
        return D4OperationRecord(start, completion, row["application_ref"])

    @staticmethod
    def _d4_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        )

    def _decode_d4_unknown_disposition(
        self,
        row: sqlite3.Row,
        start: D4OperationStart,
        completion: D4OperationCompletion | None,
    ) -> dict[str, object] | None:
        if self._schema_version < 15:
            return None
        raw = row["unknown_disposition_json"]
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError("D4 unknown disposition is invalid JSON") from exc
        expected = {
            "disposition",
            "operation_id",
            "task_run_id",
            "decided_at",
            "evidence_ref",
            "evidence_sha256",
            "payload_sha256",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise StorageError("D4 unknown disposition has invalid fields")
        decided_at = value["decided_at"]
        evidence_ref = value["evidence_ref"]
        if (
            value["disposition"] != "sealed_not_applied"
            or value["operation_id"] != start.operation_id
            or value["task_run_id"] != start.task_run_id
            or start.category != "refinement"
            or completion is not None
            or isinstance(decided_at, bool)
            or not isinstance(decided_at, (int, float))
            or not math.isfinite(decided_at)
            or decided_at < start.started_at
            or not isinstance(evidence_ref, str)
            or not evidence_ref.strip()
            or not self._d4_sha256(value["evidence_sha256"])
            or not self._d4_sha256(value["payload_sha256"])
        ):
            raise StorageError("D4 unknown disposition differs from its operation")
        payload = {key: value[key] for key in expected - {"payload_sha256"}}
        payload_sha256 = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        if value["payload_sha256"] != payload_sha256:
            raise StorageError("D4 unknown disposition checksum differs")
        return value

    def is_unknown_refinement_sealed(self, operation_id: str) -> bool:
        if self._schema_version < 14:
            return False
        record = self.load_d4_operation(operation_id)
        if record is None or self._schema_version < 15:
            return False
        row = self._connection.execute(
            "SELECT unknown_disposition_json FROM d4_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise StorageError("D4 operation disappeared while checking disposition")
        return row["unknown_disposition_json"] is not None

    def list_d4_unresolved_operations(
        self, task_run_id: str
    ) -> tuple[D4OperationRecord, ...]:
        unresolved = []
        for record in self.list_d4_operations(task_run_id):
            if record.start.task_run_id != task_run_id:
                raise StorageError("D4 operation belongs to another TaskRun")
            if record.completion is None and not self.is_unknown_refinement_sealed(
                record.start.operation_id
            ):
                unresolved.append(record)
        return tuple(unresolved)

    def seal_unknown_refinement(
        self,
        operation_id: str,
        *,
        decided_at: float,
        evidence_ref: str,
        evidence_sha256: str,
    ) -> None:
        if self._schema_version < 15:
            raise D4RecoveryRequired("refinement unknown sealing requires schema 15")
        if (
            isinstance(decided_at, bool)
            or not isinstance(decided_at, (int, float))
            or not math.isfinite(decided_at)
            or not isinstance(evidence_ref, str)
            or not evidence_ref.strip()
            or not self._d4_sha256(evidence_sha256)
        ):
            raise ValueError("invalid D4 refinement unknown disposition")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.load_d4_operation(operation_id)
            if record is None:
                raise D4OperationConflict("refinement disposition has no operation")
            if record.start.category != "refinement":
                raise D4OperationConflict("only refinement unknown may be sealed")
            if record.completion is not None:
                raise D4RecoveryRequired("known refinement result cannot be sealed")
            if decided_at < record.start.started_at:
                raise D4OperationConflict("refinement disposition precedes operation")
            payload: dict[str, object] = {
                "disposition": "sealed_not_applied",
                "operation_id": record.start.operation_id,
                "task_run_id": record.start.task_run_id,
                "decided_at": float(decided_at),
                "evidence_ref": evidence_ref,
                "evidence_sha256": evidence_sha256,
            }
            payload["payload_sha256"] = hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest()
            row = self._connection.execute(
                "SELECT unknown_disposition_json FROM d4_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise StorageError("D4 operation disappeared while sealing unknown")
            existing_raw = row["unknown_disposition_json"]
            if existing_raw is not None:
                if json.loads(existing_raw) != payload:
                    raise D4OperationConflict(
                        "refinement unknown disposition is immutable"
                    )
            else:
                self._connection.execute(
                    "UPDATE d4_operations SET unknown_disposition_json = ? "
                    "WHERE operation_id = ?",
                    (canonical_json(payload), operation_id),
                )
            self._connection.commit()
        except BaseException:
            self._rollback_quietly()
            raise
        if not self.is_unknown_refinement_sealed(operation_id):
            raise D4RecoveryRequired("refinement unknown disposition readback failed")

    def list_d4_operations(self, task_run_id: str) -> tuple[D4OperationRecord, ...]:
        if self._schema_version < 14:
            return ()
        rows = self._connection.execute(
            "SELECT operation_id FROM d4_operations WHERE task_run_id = ? ORDER BY rowid",
            (task_run_id,),
        ).fetchall()
        records = []
        for row in rows:
            record = self.load_d4_operation(row["operation_id"])
            if record is None:
                raise StorageError("D4 operation disappeared while listing")
            records.append(record)
        return tuple(records)

    def load_d4_audit_event(self, event_id: str) -> D4AuditEvent | None:
        if self._schema_version < 14:
            return None
        row = self._connection.execute(
            "SELECT * FROM d4_audit_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        event = D4AuditEvent(**json.loads(row["event_json"]))
        if (
            event.event_id != row["event_id"]
            or event.task_run_id != row["task_run_id"]
            or event.category != row["category"]
            or event.outcome != row["outcome"]
            or event.occurred_at != row["occurred_at"]
            or event.evidence_ref != row["evidence_ref"]
            or event.evidence_sha256 != row["evidence_sha256"]
            or event.related_ref != row["related_ref"]
        ):
            raise StorageError("D4 audit row differs from its typed event")
        return event

    def list_d4_audit_events(self, task_run_id: str) -> tuple[D4AuditEvent, ...]:
        if self._schema_version < 14:
            return ()
        rows = self._connection.execute(
            "SELECT event_id FROM d4_audit_events WHERE task_run_id = ? ORDER BY rowid",
            (task_run_id,),
        ).fetchall()
        events = []
        for row in rows:
            event = self.load_d4_audit_event(row["event_id"])
            if event is None:
                raise StorageError("D4 audit event disappeared while listing")
            events.append(event)
        return tuple(events)

    @staticmethod
    def _d4_audit_event_id(
        task_run_id: str,
        related_ref: str,
        category: str,
        outcome: D4AuditOutcome | None = None,
    ) -> str:
        identity = f":{outcome}" if outcome is not None else ""
        return str(
            uuid5(
                NAMESPACE_URL,
                f"qinyuan-agent:d4:{task_run_id}:{related_ref}:{category}{identity}",
            )
        )

    def _insert_d4_audit_event(self, event: D4AuditEvent) -> D4AuditEvent:
        existing = self.load_d4_audit_event(event.event_id)
        if existing is not None:
            if existing != event:
                raise D4OperationConflict("audit event ID belongs to another payload")
            return existing
        self._connection.execute(
            """
            INSERT INTO d4_audit_events(
                event_id, task_run_id, category, outcome, occurred_at,
                evidence_ref, evidence_sha256, related_ref, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.task_run_id,
                event.category,
                event.outcome,
                event.occurred_at,
                event.evidence_ref,
                event.evidence_sha256,
                event.related_ref,
                canonical_json(asdict(event)),
            ),
        )
        return event

    def _control_commit_audit_event(
        self,
        mutation: ControlMutation,
        category: D4AuditCategory,
        outcome: D4AuditOutcome,
        occurred_at: float,
    ) -> D4AuditEvent:
        return D4AuditEvent(
            self._d4_audit_event_id(
                mutation.task_run_id, mutation.mutation_id, category, outcome
            ),
            mutation.task_run_id,
            category,
            outcome,
            occurred_at,
            mutation.mutation_id,
            mutation.request_fingerprint,
            mutation.mutation_id,
        )

    def _prepare_control_commit_audit(
        self, mutation: ControlMutation
    ) -> D4AuditEvent | None:
        configuration = self._d4_policy_and_roles(mutation.task_run_id)
        d4_enabled = configuration is not None
        if not d4_enabled and mutation.d3 is not None:
            d4_enabled = (
                mutation.d3.config is not None
                and mutation.d3.config.d4_budget is not None
            )
        if not d4_enabled and mutation.d7 is not None:
            d4_enabled = (
                mutation.d7.run_config is not None
                and mutation.d7.run_config.d4_budget is not None
            )
        if not d4_enabled:
            return None
        event_id = self._d4_audit_event_id(
            mutation.task_run_id,
            mutation.mutation_id,
            "control_commit_started",
            "known",
        )
        existing = self.load_d4_audit_event(event_id)
        if existing is not None:
            if (
                existing.task_run_id != mutation.task_run_id
                or existing.category != "control_commit_started"
                or existing.outcome != "known"
                or existing.evidence_ref != mutation.mutation_id
                or existing.evidence_sha256 != mutation.request_fingerprint
                or existing.related_ref != mutation.mutation_id
            ):
                raise D4OperationConflict("control commit start differs from its D4 audit")
            return existing
        event = self._control_commit_audit_event(
            mutation, "control_commit_started", "known", self._now()
        )
        if configuration is not None:
            self.record_d4_audit_event(event)
        return event

    def _record_control_commit_failure(self, mutation: ControlMutation) -> None:
        configuration = self._d4_policy_and_roles(mutation.task_run_id)
        if configuration is None:
            return
        try:
            row = self._connection.execute(
                """
                SELECT request_fingerprint FROM control_mutations
                WHERE task_run_id = ? AND mutation_id = ?
                """,
                (mutation.task_run_id, mutation.mutation_id),
            ).fetchone()
            if row is not None and row["request_fingerprint"] == mutation.request_fingerprint:
                category: D4AuditCategory = "control_commit_committed"
                outcome: D4AuditOutcome = "known"
            else:
                category = "control_commit_failed"
                outcome = "failed"
        except sqlite3.Error:
            category = "control_commit_failed"
            outcome = "unknown"
        event_id = self._d4_audit_event_id(
            mutation.task_run_id, mutation.mutation_id, category, outcome
        )
        if self.load_d4_audit_event(event_id) is not None:
            return
        self.record_d4_audit_event(
            self._control_commit_audit_event(
                mutation, category, outcome, self._now()
            )
        )

    def _abort_control_commit(self, mutation: ControlMutation, error: Exception) -> None:
        self._rollback_quietly()
        try:
            self._record_control_commit_failure(mutation)
        except Exception as audit_error:
            error.add_note(f"D4 control commit audit could not be saved: {audit_error}")

    def reconcile_d4_control_commit_audit(self, task_run_id: str) -> None:
        """Close started commit audits from the SQLite transaction's durable outcome."""
        events = self.list_d4_audit_events(task_run_id)
        for started in events:
            if (
                started.category != "control_commit_started"
                or started.related_ref is None
            ):
                continue
            committed_id = self._d4_audit_event_id(
                task_run_id,
                started.related_ref,
                "control_commit_committed",
                "known",
            )
            failed_id = self._d4_audit_event_id(
                task_run_id,
                started.related_ref,
                "control_commit_failed",
                "failed",
            )
            if (
                self.load_d4_audit_event(committed_id) is not None
                or self.load_d4_audit_event(failed_id) is not None
            ):
                continue
            row = self._connection.execute(
                """
                SELECT request_fingerprint FROM control_mutations
                WHERE task_run_id = ? AND mutation_id = ?
                """,
                (task_run_id, started.related_ref),
            ).fetchone()
            if row is not None:
                if row["request_fingerprint"] != started.evidence_sha256:
                    raise D4OperationConflict("control mutation differs from its D4 audit")
                resolved = D4AuditEvent(
                    committed_id,
                    task_run_id,
                    "control_commit_committed",
                    "known",
                    self._now(),
                    started.related_ref,
                    started.evidence_sha256,
                    started.related_ref,
                )
            else:
                resolved = D4AuditEvent(
                    failed_id,
                    task_run_id,
                    "control_commit_failed",
                    "failed",
                    self._now(),
                    started.related_ref,
                    started.evidence_sha256,
                    started.related_ref,
                )
            self.record_d4_audit_event(resolved)

    def record_d4_audit_event(self, event: D4AuditEvent) -> D4AuditEvent:
        if not isinstance(event, D4AuditEvent):
            raise TypeError("event must be D4AuditEvent")
        if self._d4_policy_and_roles(event.task_run_id) is None:
            raise D4OperationConflict("audit event requires a registered D4 TaskRun")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.load_d4_audit_event(event.event_id)
            if existing is not None:
                if existing != event:
                    raise D4OperationConflict("audit event ID belongs to another payload")
                self._connection.commit()
                return existing
            self._insert_d4_audit_event(event)
            self._connection.commit()
            return event
        except BaseException:
            self._rollback_quietly()
            raise

    def _d4_policy_and_roles(
        self, task_run_id: str
    ) -> tuple[D4BudgetPolicy, D1RoleConfig, D1RoleConfig] | None:
        d3_config = self.load_d3_run_config(task_run_id)
        web_config = self.load_d7_web_run_config(task_run_id)
        if d3_config is not None and web_config is not None:
            raise D4OperationConflict("TaskRun has conflicting D4 configuration owners")
        if d3_config is not None:
            if d3_config.d4_budget is None:
                return None
            return d3_config.d4_budget, d3_config.outer_role, d3_config.inner_role
        if web_config is not None:
            return web_config.d4_budget, web_config.outer_role, web_config.inner_role
        return None

    def d4_budget_usage(self, task_run_id: str) -> D4BudgetUsage:
        configuration = self._d4_policy_and_roles(task_run_id)
        if configuration is None:
            raise D4OperationConflict("budget usage requires an explicitly registered D4 TaskRun")
        budget, _outer_role, _inner_role = configuration
        records = self.list_d4_operations(task_run_id)
        if any(record.start.budget_policy_ref != budget.budget_policy_ref for record in records):
            raise D4OperationConflict("operation budget differs from frozen configuration")
        counts = tuple(
            (category, sum(record.start.category == category for record in records))
            for category in budget.category_limits
        )
        return D4BudgetUsage(
            task_run_id,
            budget.budget_policy_ref,
            sum(record.start.charged for record in records),
            counts,
            sum(not record.start.charged for record in records),
        )

    @staticmethod
    def _d4_start_from_d3(start: D3OperationStart, config: D3RunConfig) -> D4OperationStart:
        if config.d4_budget is None:
            raise D4OperationConflict("D3 operation has no frozen D4 budget")
        category = {"outer_model": "planning", "inner_model": "action"}.get(start.category, start.category)
        return D4OperationStart(
            start.operation_id, start.task_run_id, cast(D4OperationCategory, category),
            start.started_at, start.base_checkpoint_sequence, start.context_sha256,
            start.resource_ref, config.d4_budget.budget_policy_ref,
            start.role, start.role_config_ref,
        )

    def _begin_d4_operation(self, start: D4OperationStart) -> D4OperationRecord | None:
        """Admit a start inside the caller's transaction before any dispatch."""
        configuration = self._d4_policy_and_roles(start.task_run_id)
        if configuration is None:
            raise D4OperationConflict("operation requires an explicitly registered D4 TaskRun")
        budget, outer_role, inner_role = configuration
        if start.charged and start.category not in budget.category_limits:
            raise D4OperationConflict("operation category is not enabled in frozen budget")
        if start.budget_policy_ref != budget.budget_policy_ref:
            raise D4OperationConflict("operation budget differs from frozen configuration")
        if start.role is not None:
            role = outer_role if start.role == "outer_planning" else inner_role
            if start.role_config_ref != role.role_config_ref:
                raise D4OperationConflict("model role differs from frozen configuration")
        existing = self.load_d4_operation(start.operation_id)
        if existing is not None:
            if existing.start != start:
                raise D4OperationConflict("operation ID belongs to another fixed request")
            if existing.completion is None:
                raise D4RecoveryRequired("operation result is unknown; do not dispatch again")
            return existing
        cancellation = self._connection.execute(
            "SELECT 1 FROM d4_audit_events "
            "WHERE task_run_id = ? AND category = 'cancellation_requested' LIMIT 1",
            (start.task_run_id,),
        ).fetchone()
        if cancellation is not None:
            raise D4RecoveryRequired("d4_cancellation_requested")
        task = self.load_task_run(start.task_run_id)
        if task is None:
            raise D4OperationConflict("operation requires an explicitly registered D4 TaskRun")
        checkpoint = self._connection.execute(
            "SELECT MAX(sequence) FROM checkpoints WHERE task_run_id = ?",
            (start.task_run_id,),
        ).fetchone()[0]
        if checkpoint != start.base_checkpoint_sequence:
            raise StaleControlStateError("D4 operation was prepared from a stale checkpoint")
        if (
            task.status is not TaskRunStatus.RUNNING
            or not task.started_at <= start.started_at < task.deadline_at
            or self._now() >= task.deadline_at
        ):
            raise D4OperationConflict("operation requires a running TaskRun before Deadline")
        if self.list_d4_unresolved_operations(start.task_run_id):
            raise D4RecoveryRequired("an earlier operation result remains unknown")
        records = self.list_d4_operations(start.task_run_id)
        if start.charged:
            if sum(record.start.charged for record in records) >= budget.total:
                raise D4BudgetExhausted("D4 total operation-start budget exhausted")
            if sum(record.start.category == start.category for record in records) >= getattr(budget, start.category):
                raise D4BudgetExhausted(f"D4 {start.category} budget exhausted")
        self._connection.execute(
            "INSERT INTO d4_operations(operation_id, task_run_id, category, resource_ref, charged, start_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (start.operation_id, start.task_run_id, start.category, start.resource_ref,
             int(start.charged), canonical_json(asdict(start))),
        )
        return None

    def _d4_companion_for_d3(self, record: D3OperationRecord) -> D4OperationRecord | None:
        config = self.load_d3_run_config(record.start.task_run_id)
        if config is None:
            raise D3OperationConflict("operation requires frozen D3 configuration")
        if config.d4_budget is None:
            return None
        companion = self.load_d4_operation(record.start.operation_id)
        if companion is None or companion.start != self._d4_start_from_d3(record.start, config):
            raise D4OperationConflict("D3 operation differs from its D4 admission")
        if (
            companion.completion != (
                D4OperationCompletion(**asdict(record.completion))
                if record.completion is not None else None
            )
            or companion.application_ref != record.application_ref
        ):
            raise D4OperationConflict("D3 operation differs from its D4 completion or application")
        return companion

    def begin_d4_operation(self, start: D4OperationStart) -> D4OperationRecord | None:
        if not isinstance(start, D4OperationStart):
            raise TypeError("start must be D4OperationStart")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self._begin_d4_operation(start)
            self._connection.commit()
            return record
        except BaseException:
            self._rollback_quietly()
            raise

    def _complete_d4_operation(self, completion: D4OperationCompletion) -> D4OperationRecord:
        record = self.load_d4_operation(completion.operation_id)
        if record is None:
            raise D4OperationConflict("completion has no admitted operation")
        if self.is_unknown_refinement_sealed(completion.operation_id):
            raise D4RecoveryRequired("sealed refinement response cannot be completed")
        if completion.finished_at < record.start.started_at:
            raise D4OperationConflict("completion precedes operation start")
        if record.completion is not None and record.completion != completion:
            raise D4OperationConflict("known operation completion is immutable")
        if record.completion is None:
            self._connection.execute(
                "UPDATE d4_operations SET completion_json = ? WHERE operation_id = ?",
                (canonical_json(asdict(completion)), completion.operation_id),
            )
        if record.start.category in {"planning", "action", "review", "refinement", "fact_review", "handoff_summary"}:
            event = D4AuditEvent(
                self._d4_audit_event_id(
                    record.start.task_run_id,
                    completion.operation_id,
                    "proposal_evidence_saved",
                    "known",
                ),
                record.start.task_run_id,
                "proposal_evidence_saved",
                "known",
                completion.finished_at,
                completion.evidence_ref,
                completion.evidence_sha256,
                completion.operation_id,
            )
            self._insert_d4_audit_event(event)
        return D4OperationRecord(record.start, completion, record.application_ref)

    def complete_d4_operation(self, completion: D4OperationCompletion) -> D4OperationRecord:
        if not isinstance(completion, D4OperationCompletion):
            raise TypeError("completion must be D4OperationCompletion")
        companion = self.load_d3_operation(completion.operation_id)
        if companion is not None:
            self.complete_d3_operation(D3OperationCompletion(**asdict(completion)))
            record = self.load_d4_operation(completion.operation_id)
            if record is None:
                raise D4OperationConflict("D3 operation has no D4 admission")
            return record
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self._complete_d4_operation(completion)
            self._connection.commit()
            return record
        except BaseException:
            self._rollback_quietly()
            raise

    def _mark_d4_operation_applied(self, operation_id: str, application_ref: str) -> D4OperationRecord:
        record = self.load_d4_operation(operation_id)
        if record is None or record.completion is None:
            raise D4RecoveryRequired("unknown operation cannot be applied")
        if record.completion.result_kind != "completed":
            raise D4OperationConflict("unsuccessful operation cannot be applied")
        if record.application_ref is not None and record.application_ref != application_ref:
            raise D4OperationConflict("operation application is immutable")
        self._connection.execute(
            "UPDATE d4_operations SET application_ref = ? WHERE operation_id = ?",
            (application_ref, operation_id),
        )
        return D4OperationRecord(record.start, record.completion, application_ref)

    def mark_d4_operation_applied(self, operation_id: str, application_ref: str) -> D4OperationRecord:
        if not isinstance(application_ref, str) or not application_ref.strip():
            raise ValueError("application_ref must be non-empty")
        if self.load_d3_operation(operation_id) is not None:
            self.mark_d3_operation_applied(operation_id, application_ref)
            record = self.load_d4_operation(operation_id)
            if record is None:
                raise D4OperationConflict("D3 operation has no D4 admission")
            return record
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self._mark_d4_operation_applied(operation_id, application_ref)
            self._connection.commit()
            return record
        except BaseException:
            self._rollback_quietly()
            raise

    def begin_d3_operation(self, start: D3OperationStart) -> D3OperationRecord | None:
        if not isinstance(start, D3OperationStart):
            raise TypeError("start must be D3OperationStart")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.load_d3_operation(start.operation_id)
            if existing is not None:
                if existing.start != start:
                    raise D3OperationConflict("operation ID belongs to another fixed request")
                if existing.completion is None:
                    raise D3RecoveryRequired("operation result is unknown; do not dispatch again")
                self._d4_companion_for_d3(existing)
                self._connection.commit()
                return existing
            task = self.load_task_run(start.task_run_id)
            config = self.load_d3_run_config(start.task_run_id)
            if task is None or config is None:
                raise D3OperationConflict("operation requires an explicitly registered D3 TaskRun")
            checkpoint = self._connection.execute(
                "SELECT MAX(sequence) FROM checkpoints WHERE task_run_id = ?",
                (start.task_run_id,),
            ).fetchone()[0]
            if checkpoint != start.base_checkpoint_sequence:
                raise StaleControlStateError("D3 operation was prepared from a stale checkpoint")
            if (
                task.status is not TaskRunStatus.RUNNING
                or not task.started_at <= start.started_at < task.deadline_at
                or self._now() >= task.deadline_at
            ):
                raise D3OperationConflict("operation requires a running TaskRun before Deadline")
            if start.role is not None:
                role = config.outer_role if start.role == "outer_planning" else config.inner_role
                if start.role_config_ref != role.role_config_ref:
                    raise D3OperationConflict("model role differs from frozen configuration")
            records = self.list_d3_operations(start.task_run_id)
            if any(record.completion is None for record in records):
                raise D3RecoveryRequired("an earlier operation result remains unknown")
            if any(
                record.start.category == start.category
                and record.start.resource_ref == start.resource_ref
                and start.category not in {"outer_model", "inner_model"}
                for record in records
            ):
                raise D3OperationConflict("resource already has a fixed operation ID")
            if config.d4_budget is not None:
                companion = self._begin_d4_operation(self._d4_start_from_d3(start, config))
                if companion is not None:
                    raise D4OperationConflict("D4 admission has no matching D3 operation")
            limit = D3_CATEGORY_LIMITS[start.category]
            if limit is not None and sum(
                record.start.category == start.category for record in records
            ) >= limit:
                raise D3BudgetExhausted(f"D3 {start.category} budget exhausted")
            self._connection.execute(
                "INSERT INTO d3_operations(operation_id, task_run_id, category, resource_ref, start_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (start.operation_id, start.task_run_id, start.category,
                 start.resource_ref, canonical_json(d3_payload(start))),
            )
            self._connection.commit()
            return None
        except BaseException:
            self._rollback_quietly()
            raise

    def complete_d3_operation(self, completion: D3OperationCompletion) -> D3OperationRecord:
        if not isinstance(completion, D3OperationCompletion):
            raise TypeError("completion must be D3OperationCompletion")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.load_d3_operation(completion.operation_id)
            if record is None:
                raise D3OperationConflict("completion has no admitted operation")
            companion = self._d4_companion_for_d3(record)
            if completion.finished_at < record.start.started_at:
                raise D3OperationConflict("completion precedes operation start")
            if record.completion is not None and record.completion != completion:
                raise D3OperationConflict("known operation completion is immutable")
            if record.completion is None:
                self._connection.execute(
                    "UPDATE d3_operations SET completion_json = ? WHERE operation_id = ?",
                    (canonical_json(d3_payload(completion)), completion.operation_id),
                )
            if companion is not None:
                self._complete_d4_operation(D4OperationCompletion(**asdict(completion)))
            self._connection.commit()
            return D3OperationRecord(record.start, completion, record.application_ref)
        except BaseException:
            self._rollback_quietly()
            raise

    def mark_d3_operation_applied(self, operation_id: str, application_ref: str) -> D3OperationRecord:
        if not isinstance(application_ref, str) or not application_ref.strip():
            raise ValueError("application_ref must be non-empty")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.load_d3_operation(operation_id)
            if record is None or record.completion is None:
                raise D3RecoveryRequired("unknown operation cannot be applied")
            companion = self._d4_companion_for_d3(record)
            if record.completion.result_kind != "completed":
                raise D3OperationConflict("unsuccessful operation cannot be applied")
            if record.application_ref is not None and record.application_ref != application_ref:
                raise D3OperationConflict("operation application is immutable")
            self._connection.execute(
                "UPDATE d3_operations SET application_ref = ? WHERE operation_id = ?",
                (application_ref, operation_id),
            )
            if companion is not None:
                self._mark_d4_operation_applied(operation_id, application_ref)
            self._connection.commit()
            return D3OperationRecord(record.start, record.completion, application_ref)
        except BaseException:
            self._rollback_quietly()
            raise

    @classmethod
    def _decode_d3_review_application(cls, row: sqlite3.Row) -> D3ReviewApplication:
        value = json.loads(row["application_json"])
        value["output_constraints"] = cls._decode_d2_constraints(value["output_constraints"])
        value["source_ids"] = tuple(value["source_ids"])
        application = D3ReviewApplication(**value)
        if (
            application.task_run_id != row["task_run_id"]
            or application.review_id != row["review_id"]
            or application.review_root_id != row["review_root_id"]
            or application.review_generation != row["review_generation"]
            or application.turn_id != row["turn_id"]
            or application.intent_id != row["intent_id"]
            or application.decision != row["decision"]
            or int(application.paused_gate) != row["paused_gate"]
            or application.constraint_version != row["constraint_version"]
        ):
            raise StorageError("D3 review row differs from its typed application")
        return application

    def load_d3_review_application(self, task_run_id: str, review_id: str) -> D3ReviewApplication | None:
        if self._schema_version < 13:
            return None
        row = self._connection.execute(
            "SELECT * FROM d3_review_applications WHERE task_run_id = ? AND review_id = ?",
            (task_run_id, review_id),
        ).fetchone()
        return None if row is None else self._decode_d3_review_application(row)

    def list_d3_review_applications(self, task_run_id: str) -> tuple[D3ReviewApplication, ...]:
        if self._schema_version < 13:
            return ()
        rows = self._connection.execute(
            "SELECT * FROM d3_review_applications WHERE task_run_id = ? ORDER BY created_event_sequence",
            (task_run_id,),
        ).fetchall()
        return tuple(self._decode_d3_review_application(row) for row in rows)

    def begin_d1_operation(self, start: D1OperationStart) -> D1OperationRecord | None:
        if not isinstance(start, D1OperationStart):
            raise TypeError("start must be D1OperationStart")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.load_d1_operation(start.operation_id)
            if existing is not None:
                current_payload = d1_payload(start)
                existing_payload = d1_payload(existing.start)
                current_payload.pop("started_at")
                existing_payload.pop("started_at")
                if current_payload != existing_payload:
                    raise D1OperationConflict("operation ID belongs to another fixed request")
                if existing.completion is None:
                    raise D1RecoveryRequired("operation result is unknown; do not dispatch again")
                self._connection.commit()
                return existing
            task = self.load_task_run(start.task_run_id)
            config = self.load_d1_run_config(start.task_run_id)
            if task is None or config is None:
                raise D1OperationConflict("operation requires an explicitly registered D1 TaskRun")
            checkpoint = self._connection.execute("SELECT MAX(sequence) FROM checkpoints WHERE task_run_id = ?", (start.task_run_id,)).fetchone()[0]
            if checkpoint != start.base_checkpoint_sequence:
                raise StaleControlStateError("D1 operation was prepared from a stale checkpoint")
            if task.status is not TaskRunStatus.RUNNING or not task.started_at <= start.started_at < task.deadline_at or self._now() >= task.deadline_at:
                raise D1OperationConflict("operation requires a running TaskRun before Deadline")
            if self._connection.execute("SELECT 1 FROM turns WHERE task_run_id = ? AND status = ?", (start.task_run_id, TurnStatus.COMPLETED.value)).fetchone() is not None:
                raise D1OperationConflict("D1 Turn has completed; dispatch is stopped")
            if start.role is not None:
                role_config = config.outer_role if start.role == "outer_planning" else config.inner_role
                if start.role_config_ref != role_config.role_config_ref:
                    raise D1OperationConflict("model role configuration differs from frozen TaskRun content")
            records = self.list_d1_operations(start.task_run_id)
            if self.load_d2_run_config(start.task_run_id) is not None:
                review_apps = self.list_d2_review_applications(start.task_run_id)
                if active_d2_pause_gates(review_apps):
                    raise D1RecoveryRequired("d2_review_pause_gate")
                if start.category in {
                    "inner_probe_model", "inner_complete_model", "inner_tool",
                    "first_digest", "copy_preparation",
                } and not any(
                    item.review_scope == "turn_start" and not item.paused_gate
                    for item in review_apps
                ):
                    raise D1RecoveryRequired("d2_turn_start_review_required")
            for record in records:
                same_resource = record.start.category == start.category and record.start.resource_ref == start.resource_ref
                if record.completion is None:
                    raise D1RecoveryRequired("an earlier operation result remains unknown")
                if same_resource and start.resource_ref is not None and start.role is None:
                    raise D1OperationConflict("resource already has a fixed operation ID")
            category_count = sum(record.start.category == start.category for record in records)
            limit = D1_CATEGORY_LIMITS[start.category]
            if limit is not None and category_count >= limit:
                raise D1BudgetExhausted(f"D1 {start.category} budget exhausted")
            if start.category in {"inner_probe_model", "inner_complete_model"} and sum(record.start.category in {"inner_probe_model", "inner_complete_model"} for record in records) >= 4:
                raise D1BudgetExhausted("D1 inner model total budget exhausted")
            self._connection.execute(
                "INSERT INTO d1_operations(operation_id,task_run_id,category,resource_ref,start_json) VALUES (?,?,?,?,?)",
                (start.operation_id, start.task_run_id, start.category, start.resource_ref, canonical_json(d1_payload(start))),
            )
            self._connection.commit()
            return None
        except BaseException:
            self._rollback_quietly()
            raise

    def complete_d1_operation(self, completion: D1OperationCompletion) -> D1OperationRecord:
        if not isinstance(completion, D1OperationCompletion):
            raise TypeError("completion must be D1OperationCompletion")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.load_d1_operation(completion.operation_id)
            if record is None:
                raise D1OperationConflict("completion has no admitted operation")
            if completion.finished_at < record.start.started_at:
                raise D1OperationConflict("completion precedes operation start")
            if record.completion is not None and record.completion != completion:
                raise D1OperationConflict("known operation completion is immutable")
            if record.completion is None:
                self._connection.execute("UPDATE d1_operations SET completion_json = ? WHERE operation_id = ?", (canonical_json(d1_payload(completion)), completion.operation_id))
            self._connection.commit()
            return D1OperationRecord(record.start, completion, record.application_ref)
        except BaseException:
            self._rollback_quietly()
            raise

    def mark_d1_operation_applied(self, operation_id: str, application_ref: str) -> D1OperationRecord:
        if not isinstance(application_ref, str) or not application_ref.strip():
            raise ValueError("application_ref must be a non-empty committed reference")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            record = self.load_d1_operation(operation_id)
            if record is None or record.completion is None:
                raise D1RecoveryRequired("unknown operation cannot be applied")
            if record.completion.result_kind != "completed":
                raise D1OperationConflict("rejected, stale or unsuccessful result cannot be applied")
            if record.application_ref is not None and record.application_ref != application_ref:
                raise D1OperationConflict("operation application is immutable")
            self._connection.execute("UPDATE d1_operations SET application_ref = ? WHERE operation_id = ?", (application_ref, operation_id))
            self._connection.commit()
            return D1OperationRecord(record.start, record.completion, application_ref)
        except BaseException:
            self._rollback_quietly()
            raise

    def _validate_d2_mutation(self, mutation: ControlMutation) -> None:
        if mutation.d2_config is not None:
            if self.load_d2_run_config(mutation.task_run_id) is not None:
                raise StorageError("D2 run configuration is immutable")
        applications = [
            change for change in mutation.state_changes
            if isinstance(change, AppendD2ReviewApplication)
        ]
        if mutation.operation == "apply_d2_review":
            if self.load_d2_run_config(mutation.task_run_id) is None:
                raise StorageError("D2 review application requires an explicit D2 run")
            if len(applications) != 1 or len(mutation.state_changes) != 1:
                raise StorageError("D2 review application requires one typed change")
            application = applications[0]
            if (
                self._task_run_for_turn(application.turn_id) != mutation.task_run_id
                or self._task_run_for_intent(application.intent_id) != mutation.task_run_id
                or self._connection.execute(
                    "SELECT turn_id FROM intents WHERE intent_id = ?", (application.intent_id,)
                ).fetchone()["turn_id"] != application.turn_id
            ):
                raise StorageError("D2 review application scope is outside its TaskRun")
            if not application.paused_gate and any(
                not (
                    item.pause_reason == "review_application_uncommitted"
                    and item.review_root_id == application.review_root_id
                    and application.review_generation >= item.review_generation
                )
                for item in active_d2_pause_gates(self.list_d2_review_applications(mutation.task_run_id))
            ):
                raise StorageError("an earlier D2 review pause still closes dispatch")
            existing = self.load_d2_review_application(
                mutation.task_run_id, application.review_id
            )
            if existing is not None:
                raise StorageError("D2 review application is append-only; replay its mutation")
            if not application.paused_gate and self._connection.execute(
                "SELECT 1 FROM d2_review_applications WHERE task_run_id = ? "
                "AND review_root_id = ? AND review_generation = ? AND paused_gate = 0",
                (mutation.task_run_id, application.review_root_id, application.review_generation),
            ).fetchone() is not None:
                raise StorageError("D2 review generation already has an immutable application")
            prior_applications = self.list_d2_review_applications(mutation.task_run_id)
            expected_version = max(
                (item.constraint_version for item in prior_applications), default=0
            ) + bool(application.output_constraints)
            if application.constraint_version != expected_version:
                raise StaleControlStateError("D2 constraint version is not current")
            for constraint in application.output_constraints:
                owner = (
                    self._task_run_for_intent(constraint.scope_id)
                    if constraint.scope_kind == "intent"
                    else self._task_run_for_turn(constraint.scope_id)
                )
                if owner != mutation.task_run_id:
                    raise StorageError("D2 constraint scope is outside its TaskRun")
                expected_scope_id = (
                    application.intent_id if constraint.scope_kind == "intent"
                    else application.turn_id
                )
                if constraint.scope_id != expected_scope_id:
                    raise StorageError("D2 constraint scope differs from its review")
            conflict = self._connection.execute(
                "SELECT review_id FROM d2_review_applications "
                "WHERE task_run_id = ? AND application_ref = ?",
                (mutation.task_run_id, application.application_ref),
            ).fetchone()
            if conflict is not None and conflict["review_id"] != application.review_id:
                raise StorageError("D2 application_ref already belongs to another review")
        elif applications:
            raise StorageError("D2 review application requires its dedicated operation")

    def _save_d2_mutation(self, mutation: ControlMutation, event_sequence: int) -> None:
        if mutation.d2_config is not None:
            self._connection.execute(
                "INSERT INTO d2_runs VALUES (?, ?, ?)",
                (mutation.task_run_id, canonical_json(d2_payload(mutation.d2_config)), event_sequence),
            )
        for application in mutation.state_changes:
            if not isinstance(application, AppendD2ReviewApplication):
                continue
            self._connection.execute(
                """
                INSERT INTO d2_review_applications(
                    task_run_id, review_root_id, review_generation, review_id,
                    application_ref, context_version, constraint_version,
                    output_constraints_json, paused_gate, created_event_sequence
                    , turn_id, intent_id, review_scope, action_candidate_id, pause_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.task_run_id,
                    application.review_root_id,
                    application.review_generation,
                    application.review_id,
                    application.application_ref,
                    application.context_version,
                    application.constraint_version,
                    canonical_json([d2_payload(item) for item in application.output_constraints]),
                    int(application.paused_gate),
                    event_sequence,
                    application.turn_id,
                    application.intent_id,
                    application.review_scope,
                    application.action_candidate_id,
                    application.pause_reason,
                ),
            )

    def _validate_d1_mutation(self, mutation: ControlMutation) -> None:
        config = self.load_d1_run_config(mutation.task_run_id)
        if mutation.d1_config is not None:
            if config is not None:
                raise StorageError("D1 TaskRun configuration is immutable")
            attachments = mutation.request.target.attachments
            if mutation.request.target.endpoints or len(attachments) != 1 or attachments[0].kind != "local_path":
                raise StorageError("D1 requires the sole authorized local attachment without network")
            if len(mutation.state_changes) != 1 or not isinstance(mutation.state_changes[0], UpsertTaskRun):
                raise StorageError("D1 start saves only its TaskRun and frozen chain configuration")
        if config is None:
            if mutation.d1_binding is not None or mutation.d1_turn_proof is not None:
                raise StorageError("D1 binding/proof requires explicit D1 TaskRun identity")
            return
        new_turns = [change for change in mutation.state_changes if isinstance(change, UpsertTurn) and self._task_run_for_turn(change.turn_id) is None]
        if new_turns:
            if mutation.operation != "register_turn" or mutation.d1_binding is None or len(new_turns) != 1 or self._connection.execute("SELECT 1 FROM turns WHERE task_run_id = ?", (mutation.task_run_id,)).fetchone() is not None:
                raise StorageError("D1 supports exactly one explicitly bound Turn")
            binding = mutation.d1_binding
            turn = new_turns[0]
            if binding.turn_id != turn.turn_id or turn.intent_ids != (binding.intent_id,) or binding.attachment_index != config.authorization.attachment_index:
                raise StorageError("D1 binding differs from fixed Turn/Intent registration")
            proposal = self.load_d1_operation(binding.proposal_request_id)
            if proposal is None or proposal.start.task_run_id != mutation.task_run_id or proposal.start.category != "outer_model" or proposal.completion is None or proposal.completion.result_kind != "completed":
                raise StorageError("D1 binding requires known accepted outer proposal evidence")
        for change in mutation.state_changes:
            if isinstance(change, UpsertTaskRun) and change.status in {TaskRunStatus.ACHIEVED, TaskRunStatus.ASSUMED_ACHIEVED}:
                raise StorageError("D1 has no registered whole-task success rule")
            if isinstance(change, UpsertTurn):
                row = self._connection.execute("SELECT sequence, turn_target, intent_ids_json FROM turns WHERE turn_id = ?", (change.turn_id,)).fetchone()
                if row is not None and (row["sequence"], row["turn_target"], tuple(json.loads(row["intent_ids_json"]))) != (change.sequence, change.turn_target, change.intent_ids):
                    raise StorageError("registered D1 Turn declaration is immutable")
            if isinstance(change, UpsertIntent):
                row = self._connection.execute("SELECT description, priority, intent_type FROM intents WHERE intent_id = ?", (change.intent_id,)).fetchone()
                if row is not None and tuple(row) != (change.description, change.priority, change.intent_type):
                    raise StorageError("registered D1 Intent declaration is immutable")
            if isinstance(change, UpsertIntent) and change.status is IntentStatus.SUCCEEDED and mutation.operation != "complete_verified_t6_intent":
                raise StorageError("D1 Intent success requires the separate fixed-rule completion gate")
            if isinstance(change, UpsertTurn) and change.status is TurnStatus.COMPLETED:
                if mutation.operation != "complete_verified_d1_turn" or mutation.d1_turn_proof is None:
                    raise StorageError("D1 Turn success requires the independent completion gate")
                binding = self.load_d1_turn_binding(change.turn_id)
                proof = mutation.d1_turn_proof
                if binding is None or proof.turn_id != change.turn_id or proof.binding_id != binding.binding_id or mutation.event.details.get("verified_fact_ids") != list(proof.supporting_fact_ids) or mutation.event.details.get("proof_ref") != proof.proof_ref:
                    raise StorageError("D1 Turn proof does not match its immutable binding")

    def _save_d1_mutation(self, mutation: ControlMutation, event_sequence: int) -> None:
        if mutation.d1_config is not None:
            self._connection.execute("INSERT INTO d1_runs VALUES (?,?,?)", (mutation.task_run_id, canonical_json(d1_payload(mutation.d1_config)), event_sequence))
        if mutation.d1_binding is not None:
            binding = mutation.d1_binding
            self._connection.execute("INSERT INTO d1_turn_bindings VALUES (?,?,?,?,?)", (binding.turn_id, binding.task_run_id, binding.intent_id, canonical_json(d1_payload(binding)), event_sequence))
        if mutation.d1_turn_proof is not None:
            proof = mutation.d1_turn_proof
            self._connection.execute("INSERT INTO d1_turn_proofs VALUES (?,?,?)", (proof.turn_id, canonical_json(d1_payload(proof)), event_sequence))

    @staticmethod
    def _validate_d5_attempt_start(attempt: D5Attempt, start: D4OperationStart) -> None:
        if (
            start.operation_id != attempt.model_request_id
            or start.task_run_id != attempt.task_run_id
            or start.category != "refinement"
            or start.context_sha256 != attempt.context_sha256
            or start.base_checkpoint_sequence != attempt.base_checkpoint_sequence
            or start.role != "outer_planning"
            or start.role_config_ref != attempt.role_config_ref
            or start.resource_ref != attempt.review_id
            or start.started_at != attempt.started_at
        ):
            raise StorageError("D5 admission binding changed")

    def load_d5_attempt(self, model_request_id: str) -> D5Attempt | None:
        if self._schema_version < 15:
            return None
        row = self._connection.execute(
            "SELECT * FROM d5_attempts WHERE model_request_id = ?", (model_request_id,)
        ).fetchone()
        correction = None
        if row is None and self._schema_version >= 27:
            row = self._connection.execute(
                "SELECT * FROM d8_refinement_corrections WHERE model_request_id = ?", (model_request_id,)
            ).fetchone()
            if row is not None:
                correction = row["root_model_request_id"]
        if row is None:
            return None
        attempt = D5Attempt.from_dict(json.loads(row["attempt_json"]))
        if (attempt.model_request_id, attempt.task_run_id) != (row["model_request_id"], row["task_run_id"]):
            raise StorageError("D5 attempt ownership changed")
        if correction is None:
            if attempt.review_id != row["review_id"]:
                raise StorageError("D5 attempt ownership changed")
        else:
            if self._connection.execute(
                "SELECT 1 FROM d5_attempts WHERE model_request_id = ?", (correction,)
            ).fetchone() is None:
                raise StorageError("D8 refinement correction root unavailable")
            root = self.load_d5_attempt(correction)
            config = self.load_d3_run_config(attempt.task_run_id)
            if (root is None or config is None or config.contract_version not in {6, 7}
                    or (root.task_run_id, root.review_id, root.sources, root.context_sha256,
                        root.base_checkpoint_sequence, root.role_config_ref)
                    != (attempt.task_run_id, attempt.review_id, attempt.sources, attempt.context_sha256,
                        attempt.base_checkpoint_sequence, attempt.role_config_ref)):
                raise StorageError("D8 refinement correction binding changed")
        operation = self.load_d4_operation(model_request_id)
        if operation is None:
            raise StorageError("D5 attempt has no D4 admission")
        self._validate_d5_attempt_start(attempt, operation.start)
        return attempt

    def list_d5_attempt_refs(self, task_run_id: str) -> tuple[str, ...]:
        """List owned keys without parsing each row, for isolated evidence reads."""
        if self._schema_version < 15:
            return ()
        rows = self._connection.execute(
            "SELECT model_request_id FROM d5_attempts WHERE task_run_id = ? ORDER BY rowid",
            (task_run_id,),
        ).fetchall()
        if self._schema_version >= 27:
            rows += self._connection.execute(
                "SELECT model_request_id FROM d8_refinement_corrections WHERE task_run_id = ? ORDER BY rowid",
                (task_run_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def list_d5_attempts(self, task_run_id: str) -> tuple[D5Attempt, ...]:
        if self._schema_version < 15:
            return ()
        attempts = tuple(self.load_d5_attempt(ref) for ref in self.list_d5_attempt_refs(task_run_id))
        if any(item is None or item.task_run_id != task_run_id for item in attempts):
            raise StorageError("D5 attempt ownership changed")
        return cast(tuple[D5Attempt, ...], attempts)

    def begin_d5_attempt(self, attempt: D5Attempt, start: D4OperationStart) -> D5Attempt | None:
        """Charge admission and source-version dedup in the same transaction."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._validate_d5_attempt_start(attempt, start)
            existing = self.load_d5_attempt(attempt.model_request_id)
            if existing is not None:
                if existing != attempt:
                    raise IdempotencyConflictError("D5 attempt content changed")
                operation = self.load_d4_operation(attempt.model_request_id)
                if operation is None or operation.start != start:
                    raise IdempotencyConflictError("D5 attempt admission changed")
                self._connection.commit()
                return existing
            config = self.load_d3_run_config(attempt.task_run_id)
            review = self.load_d3_review_application(attempt.task_run_id, attempt.review_id)
            if config is None or not config.d5_enabled or config.d4_budget is None:
                raise StorageError("D5 requires explicit new-run enablement")
            if review is None or review.paused_gate or not set(
                source.object_id for source in attempt.sources
            ).issubset(review.source_ids):
                raise StorageError("D5 requires committed accepted review sources")
            previous = self.list_d5_attempts(attempt.task_run_id)
            tried = {source.version_key for item in previous for source in item.sources}
            if any(item.review_id == attempt.review_id for item in previous) or not any(
                source.version_key not in tried for source in attempt.sources
            ):
                raise StorageError("D5 source versions were already attempted")
            if self._begin_d4_operation(start) is not None:
                raise StorageError("D5 attempt cannot adopt a prior D4 admission")
            self._connection.execute(
                "INSERT INTO d5_attempts VALUES (?, ?, ?, ?)",
                (attempt.model_request_id, attempt.task_run_id, attempt.review_id,
                 canonical_json(asdict(attempt))),
            )
            self._connection.commit()
            return None
        except BaseException:
            self._connection.rollback()
            raise

    def d8_refinement_root(self, model_request_id: str) -> str | None:
        if self._schema_version < 27:
            return None
        row = self._connection.execute(
            "SELECT root_model_request_id FROM d8_refinement_corrections WHERE model_request_id = ?",
            (model_request_id,),
        ).fetchone()
        return row[0] if row is not None else None

    def begin_d8_refinement_correction(self, root_id: str, attempt: D5Attempt,
                                     start: D4OperationStart) -> D5Attempt | None:
        """Append one separately charged correction; retain the root's source dedup."""
        from agent_core.turn_modeling import stable_id
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._validate_d5_attempt_start(attempt, start)
            config = self.load_d3_run_config(attempt.task_run_id)
            root = self.load_d5_attempt(root_id)
            operation = self.load_d4_operation(root_id)
            if (config is None or config.contract_version not in {6, 7} or root is None
                    or self.d8_refinement_root(root_id) is not None
                    or operation is None or operation.completion is None
                    or operation.completion.result_kind != "failed"
                    or operation.application_ref is not None
                    or attempt.model_request_id != stable_id(attempt.task_run_id, "d8/refinement/correction/" + root_id)
                    or (root.task_run_id, root.review_id, root.sources, root.context_sha256,
                        root.base_checkpoint_sequence, root.role_config_ref)
                    != (attempt.task_run_id, attempt.review_id, attempt.sources, attempt.context_sha256,
                        attempt.base_checkpoint_sequence, attempt.role_config_ref)):
                raise StorageError("D8 correction requires a bound completed failed root")
            existing = self.load_d5_attempt(attempt.model_request_id)
            if existing is not None:
                if (existing != attempt or self.d8_refinement_root(attempt.model_request_id) != root_id
                        or self.load_d4_operation(attempt.model_request_id).start != start):
                    raise IdempotencyConflictError("D8 refinement correction changed")
                self._connection.commit()
                return existing
            if self._begin_d4_operation(start) is not None:
                raise StorageError("D8 correction cannot adopt a prior admission")
            self._connection.execute("INSERT INTO d8_refinement_corrections VALUES (?, ?, ?, ?)",
                                     (attempt.model_request_id, root_id, attempt.task_run_id,
                                      canonical_json(asdict(attempt))))
            self._connection.commit()
            return None
        except BaseException:
            self._connection.rollback()
            raise

    def list_process_facts(self, task_run_id: str) -> tuple[D5ProcessFact, ...]:
        if self._schema_version < 15:
            return ()
        rows = self._connection.execute(
            "SELECT * FROM process_facts WHERE task_run_id = ? ORDER BY version",
            (task_run_id,),
        ).fetchall()
        if self._schema_version >= 27:
            rows += self._connection.execute(
                "SELECT * FROM d8_refinement_process_facts WHERE task_run_id = ? ORDER BY version",
                (task_run_id,),
            ).fetchall()
            rows.sort(key=lambda row: row["version"])
        facts = []
        for row in rows:
            fact = D5ProcessFact.from_dict(json.loads(row["metadata_json"]))
            if (fact.process_fact_id, fact.task_run_id, fact.version, fact.model_request_id) != (
                row["process_fact_id"], task_run_id, row["version"], row["model_request_id"]
            ):
                raise StorageError("Process_Fact ownership changed")
            facts.append(fact)
        if tuple(item.version for item in facts) != tuple(range(1, len(facts) + 1)):
            raise StorageError("Process_Fact versions must be contiguous")
        return tuple(facts)

    def list_process_fact_refs(self, task_run_id: str) -> tuple[str, ...]:
        """List owned summary keys without parsing row metadata."""
        if self._schema_version < 15:
            return ()
        query = "SELECT process_fact_id, version FROM process_facts WHERE task_run_id = ?"
        parameters = (task_run_id,)
        if self._schema_version >= 27:
            query += " UNION ALL SELECT process_fact_id, version FROM d8_refinement_process_facts WHERE task_run_id = ?"
            parameters += (task_run_id,)
        return tuple(row[0] for row in self._connection.execute(query + " ORDER BY version", parameters))

    def load_process_fact(self, process_fact_id: str) -> D5ProcessFact | None:
        """Read and verify one immutable metadata row independently."""
        if self._schema_version < 15:
            return None
        row = self._connection.execute(
            "SELECT * FROM process_facts WHERE process_fact_id = ?", (process_fact_id,),
        ).fetchone()
        if row is None and self._schema_version >= 27:
            row = self._connection.execute(
                "SELECT * FROM d8_refinement_process_facts WHERE process_fact_id = ?", (process_fact_id,),
            ).fetchone()
        if row is None:
            return None
        fact = D5ProcessFact.from_dict(json.loads(row["metadata_json"]))
        if (
            fact.process_fact_id, fact.task_run_id, fact.version, fact.model_request_id
        ) != (
            row["process_fact_id"], row["task_run_id"], row["version"], row["model_request_id"]
        ):
            raise StorageError("Process_Fact ownership changed")
        return fact

    def _validate_d5_mutation(self, mutation: ControlMutation) -> None:
        if mutation.operation != "append_process_fact":
            return
        fact = mutation.state_changes[0]
        assert isinstance(fact, D5ProcessFact)
        config = self.load_d3_run_config(mutation.task_run_id)
        attempt = self.load_d5_attempt(fact.model_request_id)
        operation = self.load_d4_operation(fact.model_request_id)
        review = self.load_d3_review_application(mutation.task_run_id, fact.review_id)
        previous = self.list_process_facts(mutation.task_run_id)
        if (
            config is None or not config.d5_enabled or config.d4_budget is None
            or fact.task_run_id != mutation.task_run_id
            or attempt is None or attempt.task_run_id != mutation.task_run_id
            or attempt.review_id != fact.review_id or attempt.sources != fact.sources
            or operation is None or operation.start.category != "refinement"
            or operation.start.task_run_id != mutation.task_run_id
            or operation.start.resource_ref != attempt.review_id
            or operation.start.base_checkpoint_sequence != attempt.base_checkpoint_sequence
            or operation.start.context_sha256 != attempt.context_sha256
            or operation.start.started_at != attempt.started_at
            or operation.start.role != "outer_planning"
            or operation.start.role_config_ref != attempt.role_config_ref
            or operation.completion is None or operation.completion.result_kind != "completed"
            or operation.completion.evidence_ref != fact.model_request_id
            or operation.completion.evidence_sha256 != fact.evidence_sha256
            or operation.application_ref is not None
            or review is None or review.paused_gate or fact.version != len(previous) + 1
        ):
            raise StorageError("Process_Fact requires accepted, unapplied, bound D5 evidence")
        if self.list_d4_unresolved_operations(mutation.task_run_id):
            raise D4RecoveryRequired("D5 cannot apply while other outcomes are unknown")

    def _save_d5_mutation(self, mutation: ControlMutation, event_sequence: int) -> None:
        if mutation.operation != "append_process_fact":
            return
        fact = mutation.state_changes[0]
        assert isinstance(fact, D5ProcessFact)
        corrected = self.d8_refinement_root(fact.model_request_id) is not None
        self._connection.execute(
            "INSERT INTO d8_refinement_process_facts VALUES (?, ?, ?, ?, ?, ?)" if corrected
            else "INSERT INTO process_facts VALUES (?, ?, ?, ?, ?, ?)",
            (fact.process_fact_id, fact.task_run_id, fact.version, fact.model_request_id,
             canonical_json(asdict(fact)), event_sequence),
        )
        self._mark_d4_operation_applied(fact.model_request_id, fact.process_fact_id)

    def _validate_d7_mutation(self, mutation: ControlMutation) -> None:
        metadata = mutation.d7
        if metadata is not None and metadata.run_config is not None:
            config = metadata.run_config
            task_change = next(
                (item for item in mutation.state_changes if isinstance(item, UpsertTaskRun)),
                None,
            )
            endpoints = mutation.request.target.endpoints
            if (
                mutation.operation != "start_task_run"
                or mutation.d3 is not None
                or mutation.d1_config is not None
                or mutation.d2_config is not None
                or self.load_d7_web_run_config(mutation.task_run_id) is not None
                or self.load_d3_run_config(mutation.task_run_id) is not None
                or self.load_d1_run_config(mutation.task_run_id) is not None
                or self.load_d2_run_config(mutation.task_run_id) is not None
                or config.task_run_id != mutation.task_run_id
                or task_change is None
                or len(mutation.state_changes) != 1
                or task_change.task_run_id != mutation.task_run_id
                or task_change.request != mutation.request
                or task_change.status is not TaskRunStatus.RUNNING
                or len(endpoints) != 1
                or mutation.request.target.attachments
                or canonical_web_origin(endpoints[0].url) != config.endpoint
                or config.authorization.allowed_origin != config.endpoint
            ):
                raise StorageError("D7 Web config must bind its explicit Web TaskRun start")
            return
        if mutation.operation not in {
            "register_d7_candidate", "apply_external_validation_feedback",
            "start_external_validation_check", "complete_external_validation_check",
        }:
            if mutation.d7 is not None:
                raise StorageError("D7 metadata is not allowed for this operation")
            return
        if metadata is None:
            raise StorageError("D7 lifecycle operation requires typed metadata")
        d3_config = self.load_d3_run_config(mutation.task_run_id)
        web_config = self.load_d7_web_run_config(mutation.task_run_id)
        if (d3_config is None) == (web_config is None):
            raise StorageError("D7 lifecycle requires exactly one D3 or Web TaskRun config")
        state = self.load_control_state_rows(mutation.task_run_id)
        task_change = next(
            (item for item in mutation.state_changes if isinstance(item, UpsertTaskRun)),
            None,
        )
        if (
            state is None
            or task_change is None
            or len(mutation.state_changes) != 1
            or task_change.task_run_id != mutation.task_run_id
            or task_change.request != state.task_run.request
            or task_change.effective_model_backend != state.task_run.effective_model_backend
            or task_change.started_at != state.task_run.started_at
            or task_change.deadline_at != state.task_run.deadline_at
            or task_change.sandbox_id != state.task_run.sandbox_id
        ):
            raise StorageError("D7 lifecycle requires one immutable TaskRun update")
        if self.list_d4_unresolved_operations(mutation.task_run_id):
            raise D4RecoveryRequired("D7 feedback cannot apply with unresolved operations")

        if mutation.operation == "register_d7_candidate":
            candidate = metadata.candidate
            if (
                candidate is None
                or metadata.feedback is not None
                or state.task_run.status is not TaskRunStatus.RUNNING
                or task_change.status is not TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
                or candidate.task_run_id != mutation.task_run_id
                or self.load_d7_answer_candidate(candidate.candidate_id) is not None
            ):
                raise StorageError("D7 candidate registration is not admitted")
            turns = {item.turn_id: item for item in state.turns}
            intents = {item.intent_id: item for item in state.intents}
            turn = turns.get(candidate.turn_id)
            intent = intents.get(candidate.intent_id)
            if (
                turn is None
                or turn.task_run_id != mutation.task_run_id
                or turn.status is not TurnStatus.COMPLETED
                or intent is None
                or intent.turn_id != candidate.turn_id
                or intent.status is not IntentStatus.SUCCEEDED
            ):
                raise StorageError("D7 candidate Turn or Intent is not complete and owned")
            if candidate.source_kind == "d3":
                plans = {item.plan_id: item for item in state.plans}
                executions = {item.execution_id: item for item in state.executions}
                plan = plans.get(candidate.plan_id)
                execution = executions.get(candidate.execution_id)
                binding = self.load_d3_plan_binding(candidate.plan_id)
                result = self.load_d3_plan_result(candidate.plan_id)
                if (
                    d3_config is None
                    or web_config is not None
                    or plan is None
                    or plan.intent_id != candidate.intent_id
                    or plan.status is not PlanStatus.COMPLETED
                    or execution is None
                    or execution.plan_id != candidate.plan_id
                    or execution.status is not ExecutionStatus.SUCCEEDED
                    or execution.output_ref is None
                    or binding is None
                    or binding.task_run_id != mutation.task_run_id
                    or binding.intent_id != candidate.intent_id
                    or binding.artifact_version_id != candidate.source_ref
                    or result is None
                    or result.task_run_id != mutation.task_run_id
                    or result.evidence_sha256 != candidate.evidence_version
                ):
                    raise StorageError("D7 candidate source is not a completed owned D3 Execution")
                if any(item.status is ExecutionStatus.RUNNING for item in state.executions):
                    raise StorageError("D7 candidate cannot be registered with a running Execution")
            elif candidate.source_kind == "web":
                persisted_binding = self.load_d7_web_execution_binding(
                    candidate.execution_id
                )
                plan = self.load_d7_web_plan(candidate.plan_id)
                execution = self.load_d7_web_execution(candidate.execution_id)
                if (
                    web_config is None
                    or d3_config is not None
                    or plan is None
                    or plan.task_run_id != mutation.task_run_id
                    or plan.turn_id != candidate.turn_id
                    or plan.intent_id != candidate.intent_id
                    or execution is None
                    or execution.state != "completed"
                    or execution.task_run_id != mutation.task_run_id
                    or execution.turn_id != candidate.turn_id
                    or execution.intent_id != candidate.intent_id
                    or execution.plan_id != candidate.plan_id
                    or persisted_binding != candidate.web_execution_binding
                    or persisted_binding is None
                    or persisted_binding.response_evidence_id != candidate.source_ref
                    or persisted_binding.response_sha256 != candidate.evidence_version
                ):
                    raise StorageError("D7 candidate source is not a completed owned Web Execution")
            else:
                raise StorageError("D7 candidate source kind is invalid")
            if self._d7_has_feedback_conflict(mutation.task_run_id):
                raise StorageError("D7 feedback conflict blocks further candidate registration")
            return

        if mutation.operation == "start_external_validation_check":
            request = metadata.check_request
            candidate = (
                None
                if request is None
                else self.load_d7_answer_candidate(request.candidate_id)
            )
            prior = (
                ()
                if request is None
                else self.list_d7_validation_feedback(
                    request.task_run_id,
                    request.candidate_id,
                    request.evidence_version,
                )
            )
            if request is not None:
                validate_external_check_binding(request, prior)
            if (
                request is None
                or metadata.feedback is not None
                or metadata.check_completion is not None
                or state.task_run.status is not TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK and not request.conflict_sources
                or task_change.status is not state.task_run.status
                or mutation.event.timestamp >= state.task_run.deadline_at
                or request.task_run_id != mutation.task_run_id
                or request.checkpoint_sequence != mutation.checkpoint.sequence
                or candidate is None
                or candidate.task_run_id != mutation.task_run_id
                or candidate.evidence_version != request.evidence_version
                or self.load_d7_external_validation_check(request.query_id) is not None
            ):
                raise StorageError("D7 external check start is not bound to unresolved feedback")
            return

        if mutation.operation == "complete_external_validation_check":
            completion = metadata.check_completion
            if completion is None or metadata.check_request is not None:
                raise StorageError("D7 external check completion metadata is invalid")
            request = completion.result.request
            record = self.load_d7_external_validation_check(request.query_id)
            if (
                record is None
                or record.request != request
                or record.result is not None
                or request.task_run_id != mutation.task_run_id
                or completion.result.verdict == "unknown"
                and completion.reconciliation is not None
                or completion.result.verdict != "unknown"
                and completion.reconciliation is None
            ):
                raise StorageError("D7 external check result has no matching incomplete start")
            candidate = self.load_d7_answer_candidate(request.candidate_id)
            prior = self.list_d7_validation_feedback(
                request.task_run_id,
                request.candidate_id,
                request.evidence_version,
            )
            if (
                candidate is None
                or candidate.task_run_id != mutation.task_run_id
                or candidate.evidence_version != request.evidence_version
            ):
                raise StorageError("D7 external check result lost its unknown submission")
            validate_external_check_binding(request, prior)
            proposed = (
                prior
                if completion.reconciliation is None
                else (*prior, completion.reconciliation)
            )
            projection = project_candidate_feedback(
                request.task_run_id,
                request.candidate_id,
                request.evidence_version,
                proposed,
            )
            previous_task = self.load_task_run(mutation.task_run_id)
            if previous_task is None:
                raise StorageError("D7 external check TaskRun disappeared")
            if previous_task.status in TERMINAL_STATUSES:
                expected_status = previous_task.status
            elif mutation.event.timestamp >= previous_task.deadline_at:
                expected_status = TaskRunStatus.DEADLINE_EXCEEDED
            else:
                expected_status = {
                    "accepted": TaskRunStatus.ACHIEVED,
                    "rejected": TaskRunStatus.RUNNING,
                    "unknown": TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
                    "conflict": previous_task.status,
                    "pending": previous_task.status,
                }[projection.state]
            if task_change.status is not expected_status:
                raise StorageError("D7 external check TaskRun transition is not admitted")
            return

        feedback = metadata.feedback
        if feedback is None or metadata.candidate is not None:
            raise StorageError("D7 feedback metadata is invalid")
        candidate = self.load_d7_answer_candidate(feedback.candidate_id)
        prior = self.list_d7_validation_feedback(
            mutation.task_run_id,
            feedback.candidate_id,
            feedback.evidence_version,
        )
        prior_determinate = any(
            item.verdict in {"accepted", "rejected"} for item in prior
        )
        if prior_determinate:
            expected_status = state.task_run.status
        elif state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:
            expected_status = (
                TaskRunStatus.DEADLINE_EXCEEDED
                if mutation.event.timestamp >= state.task_run.deadline_at
                else {
                    "accepted": TaskRunStatus.ACHIEVED,
                    "rejected": TaskRunStatus.RUNNING,
                    "unknown": TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
                }[feedback.verdict]
            )
        elif state.task_run.status in TERMINAL_STATUSES:
            expected_status = state.task_run.status
        else:
            expected_status = None
        if (
            feedback.task_run_id != mutation.task_run_id
            or candidate is None
            or candidate.task_run_id != mutation.task_run_id
            or candidate.evidence_version != feedback.evidence_version
            or expected_status is None
            or task_change.status is not expected_status
        ):
            raise StorageError("D7 feedback is not bound to the awaited candidate version")

    def _d7_has_feedback_conflict(self, task_run_id: str) -> bool:
        for candidate in self.list_d7_answer_candidates(task_run_id):
            records = self.list_d7_validation_feedback(
                task_run_id,
                candidate.candidate_id,
                candidate.evidence_version,
            )
            if project_candidate_feedback(task_run_id, candidate.candidate_id, candidate.evidence_version, records).state == "conflict":
                return True
        return False

    def _save_d7_mutation(self, mutation: ControlMutation, event_sequence: int) -> None:
        metadata = mutation.d7
        if metadata is None:
            return
        if metadata.run_config is not None:
            config = metadata.run_config
            self._connection.execute(
                "INSERT INTO d7_web_run_configs VALUES (?, ?, ?, ?, ?)",
                (
                    config.task_run_id,
                    config.request_id,
                    config.config_ref,
                    canonical_json(config.to_dict()),
                    event_sequence,
                ),
            )
        elif metadata.candidate is not None:
            candidate = metadata.candidate
            self._connection.execute(
                """INSERT INTO d7_candidate_registry(
                       candidate_id, task_run_id, source_kind, turn_id, intent_id,
                       plan_id, execution_id, evidence_version, source_ref,
                       candidate_json, created_event_sequence
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.candidate_id,
                    candidate.task_run_id,
                    candidate.source_kind,
                    candidate.turn_id,
                    candidate.intent_id,
                    candidate.plan_id,
                    candidate.execution_id,
                    candidate.evidence_version,
                    candidate.source_ref,
                    canonical_json(candidate.to_dict()),
                    event_sequence,
                ),
            )
            if candidate.source_kind == "d3":
                self._connection.execute(
                    """INSERT INTO d7_answer_candidates(
                           candidate_id, task_run_id, turn_id, intent_id, plan_id,
                           execution_id, evidence_version, source_ref, candidate_json,
                           created_event_sequence
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.candidate_id,
                        candidate.task_run_id,
                        candidate.turn_id,
                        candidate.intent_id,
                        candidate.plan_id,
                        candidate.execution_id,
                        candidate.evidence_version,
                        candidate.source_ref,
                        canonical_json(candidate.to_dict()),
                        event_sequence,
                    ),
                )
        elif metadata.feedback is not None:
            feedback = metadata.feedback
            self._connection.execute(
                "INSERT INTO d7_validation_feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback.operation_id,
                    feedback.task_run_id,
                    feedback.candidate_id,
                    feedback.evidence_version,
                    feedback.verdict,
                    canonical_json(feedback.to_dict()),
                    event_sequence,
                ),
            )
        elif metadata.check_request is not None:
            request = metadata.check_request
            self._connection.execute(
                """INSERT INTO d7_external_validation_checks(
                       query_id, task_run_id, candidate_id, submission_operation_id,
                       evidence_version, checkpoint_sequence, request_json,
                       started_event_sequence, result_json, result_event_sequence
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (
                    request.query_id,
                    request.task_run_id,
                    request.candidate_id,
                    request.submission_operation_id,
                    request.evidence_version,
                    request.checkpoint_sequence,
                    canonical_json(request.to_dict()),
                    event_sequence,
                ),
            )
        else:
            assert metadata.check_completion is not None
            completion = metadata.check_completion
            request = completion.result.request
            cursor = self._connection.execute(
                """UPDATE d7_external_validation_checks
                   SET result_json = ?, result_event_sequence = ?
                   WHERE query_id = ? AND task_run_id = ? AND result_json IS NULL""",
                (
                    canonical_json(completion.result.to_dict()),
                    event_sequence,
                    request.query_id,
                    request.task_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StorageError("D7 external check result was already completed")
            if completion.reconciliation is not None:
                feedback = completion.reconciliation
                self._connection.execute(
                    "INSERT INTO d7_validation_feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        feedback.operation_id,
                        feedback.task_run_id,
                        feedback.candidate_id,
                        feedback.evidence_version,
                        feedback.verdict,
                        canonical_json(feedback.to_dict()),
                        event_sequence,
                    ),
                )

    def list_d8_conclusions(self, task_run_id: str) -> tuple[D8Conclusion, ...]:
        if self._schema_version < 23:
            return ()
        rows = self._connection.execute(
            "SELECT * FROM d8_conclusions WHERE task_run_id = ? ORDER BY event_sequence",
            (task_run_id,),
        ).fetchall()
        records = []
        for row in rows:
            record = D8Conclusion(**json.loads(row["metadata_json"]))
            if (record.record_id, record.task_run_id, record.plan_id,
                record.proposal_request_id, record.review_request_id, record.fact_id) != (
                    row["record_id"], task_run_id, row["plan_id"], row["proposal_request_id"],
                    row["review_request_id"], row["fact_id"]):
                raise StorageError("D8 conclusion metadata does not match its owner")
            records.append(record)
        return tuple(records)

    def list_d8_handoffs(self, task_run_id):
        return load_records(self, "d8_handoff_versions", task_run_id)

    def list_d8_revisions(self, task_run_id):
        return load_records(self, "d8_conclusion_revisions", task_run_id)

    def list_d8_decisions(self, task_run_id):
        return load_records(self, "d8_decisions", task_run_id)

    def list_d8_capacities(self, task_run_id):
        return load_records(self, "d8_capacity_versions", task_run_id)

    def list_d8_tree_records(self, task_run_id):
        return load_records(self, "d8_tree_records", task_run_id)

    def d8_history_sequences(self, task_run_id):
        result = {}
        for row in self._connection.execute("SELECT sequence, details_json FROM runtime_events WHERE task_run_id=? ORDER BY sequence", (task_run_id,)):
            for identity in json.loads(row["details_json"]).get("entity_ids", []):
                result[identity] = row["sequence"]
        if self._schema_version >= 24:
            for row in self._connection.execute("SELECT event_sequence, metadata_json FROM d8_tree_records WHERE task_run_id=? ORDER BY event_sequence", (task_run_id,)):
                data = json.loads(row["metadata_json"])
                result["tree/" + data["record_id"]] = row["event_sequence"]
                for identity in (data["turn_id"], data["intent_id"]):
                    if identity:
                        result[identity] = row["event_sequence"]
            for row in self._connection.execute("SELECT record_id, event_sequence FROM d8_conclusions WHERE task_run_id=?", (task_run_id,)):
                result["review/" + row["record_id"]] = row["event_sequence"]
        return result

    def list_d8_reads(self, task_run_id):
        if self._schema_version < 24:
            return ()
        return tuple((row["request_id"], json.loads(row["start_json"]),
                      json.loads(row["completion_json"]) if row["completion_json"] else None)
                     for row in self._connection.execute("SELECT * FROM d8_history_reads WHERE task_run_id=? ORDER BY rowid", (task_run_id,)))

    def load_d8_read(self, request_id):
        if self._schema_version < 24:
            return None
        row = self._connection.execute("SELECT * FROM d8_history_reads WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["start_json"]), json.loads(row["completion_json"]) if row["completion_json"] else None

    def begin_d8_read(self, start):
        from agent_core.handoff_contracts import D8ReadStart
        if not isinstance(start, D8ReadStart):
            raise TypeError("D8 read start must be typed")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.load_d8_read(start.request_id)
            if existing is not None:
                if existing[0] != asdict(start):
                    raise StorageError("D8 read identity differs from its frozen snapshot")
                self._connection.commit()
                return existing
            config = self.load_d3_run_config(start.task_run_id)
            task = self.load_task_run(start.task_run_id)
            latest = self._connection.execute("SELECT MAX(sequence) FROM checkpoints WHERE task_run_id=?", (start.task_run_id,)).fetchone()[0]
            if (config is None or config.contract_version not in {3, 4, 5, 6, 7} or task.status is not TaskRunStatus.RUNNING
                    or latest != start.checkpoint_sequence or not task.started_at <= start.started_at < task.deadline_at
                    or self._now() >= task.deadline_at or self.list_d4_unresolved_operations(start.task_run_id)
                    or self._connection.execute("SELECT 1 FROM d4_audit_events WHERE task_run_id=? AND category='cancellation_requested'", (start.task_run_id,)).fetchone()):
                raise StorageError("D8 read admission is stopped or stale")
            count = self._connection.execute("SELECT COUNT(*) FROM d8_history_reads WHERE task_run_id=?", (start.task_run_id,)).fetchone()[0]
            if count >= 8:
                raise StorageError("d8_read_budget_exhausted")
            if start.capacity_version != len(self.list_d8_capacities(start.task_run_id)):
                raise StorageError("D8 read capacity is stale")
            self._connection.execute("INSERT INTO d8_history_reads VALUES (?,?,?,NULL)",
                                     (start.request_id, start.task_run_id, canonical_json(asdict(start))))
            self._connection.commit()
            return None
        except BaseException:
            self._rollback_quietly()
            raise

    def complete_d8_read(self, request_id, completion):
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            known = self.load_d8_read(request_id)
            if known is None or (known[1] is not None and known[1] != completion):
                raise StorageError("D8 read completion is missing or changed")
            self._connection.execute("UPDATE d8_history_reads SET completion_json=? WHERE request_id=?",
                                     (canonical_json(completion), request_id))
            self._connection.commit()
        except BaseException:
            self._rollback_quietly()
            raise

    def _validate_d8_mutation(self, mutation: ControlMutation) -> None:
        config = self.load_d3_run_config(mutation.task_run_id)
        records = [c for c in mutation.state_changes if isinstance(c, D8Conclusion)]
        facts = [c for c in mutation.state_changes if isinstance(c, UpsertFact)]
        if config is None or not config.d8_enabled:
            if records or mutation.operation == "append_d8_conclusion":
                raise StorageError("D8 conclusions require explicit D8 enablement")
            return
        if facts and (len(records) != 1 or records[0].outcome != "verified_fact"):
            raise StorageError("D8 Fact requires its atomic approval record")
        for record in records:
            binding = self.load_d3_plan_binding(record.plan_id)
            if (record.task_run_id != mutation.task_run_id or binding is None
                    or (binding.task_run_id, binding.turn_id, binding.intent_id)
                    != (record.task_run_id, record.turn_id, record.intent_id)):
                raise StorageError("D8 conclusion scope differs from its Plan")
            for request_id, category in ((record.proposal_request_id, "action"),
                                         (record.review_request_id, "fact_review")):
                call = self.load_d4_operation(request_id)
                if (call is None or call.start.task_run_id != record.task_run_id
                        or call.start.resource_ref != record.plan_id or call.start.category != category
                        or call.completion is None or call.completion.result_kind != "completed"
                        or call.application_ref is not None):
                    raise StorageError("D8 conclusion requires unapplied known model results")
            if record.outcome == "verified_fact":
                if len(facts) != 1 or facts[0].fact_id != record.fact_id:
                    raise StorageError("D8 verified conclusion requires its matching Fact")
            elif facts:
                raise StorageError("D8 inference/rejection cannot submit a Fact")

    def _save_d8_mutation(self, mutation: ControlMutation, event_sequence: int) -> None:
        for record in mutation.state_changes:
            if not isinstance(record, D8Conclusion):
                continue
            self._connection.execute(
                "INSERT INTO d8_conclusions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record.record_id, record.task_run_id, record.plan_id,
                 record.proposal_request_id, record.review_request_id, record.fact_id,
                 canonical_json(d3_payload(record)), event_sequence),
            )
            for request_id in (record.proposal_request_id, record.review_request_id):
                self._mark_d4_operation_applied(request_id, record.record_id)

    def _validate_d3_mutation(self, mutation: ControlMutation) -> None:
        if validate_handoff_mutation(self, mutation):
            return
        metadata = mutation.d3
        config = self.load_d3_run_config(mutation.task_run_id)
        if metadata is not None and metadata.config is not None:
            if config is not None or mutation.operation != "start_task_run":
                raise StorageError("D3 TaskRun configuration is immutable")
            if metadata != D3CommitMetadata(config=metadata.config):
                raise StorageError("D3 start may only save its frozen configuration")
            attachments = mutation.request.target.attachments
            if (
                mutation.request.target.endpoints
                or len(attachments) != len(metadata.config.authorization.attachment_indices)
                or any(item.kind != "local_path" for item in attachments)
                or len(mutation.state_changes) != 1
                or not isinstance(mutation.state_changes[0], UpsertTaskRun)
            ):
                raise StorageError("D3 requires frozen local attachments without network")
            return
        if config is None:
            if metadata is not None or any(
                isinstance(change, D3ReviewApplication) for change in mutation.state_changes
            ):
                raise StorageError("D3 metadata requires explicit D3 TaskRun identity")
            return
        if mutation.d1_config is not None or mutation.d2_config is not None:
            raise StorageError("D3 cannot be combined with a D1 or D2 run")
        allowed = {
            "append_process_fact",
            "append_d8_conclusion",
            "finish_task_run", "register_d3_turn", "select_d3_intent",
            "apply_d3_review", "register_d3_plan", "replan_d3_plan",
            "complete_d3_plan", "complete_d3_intent", "switch_d3_intent", "abandon_d8_intent",
            "complete_d3_turn", "advance_d3_turn", "exhaust_d3_turn",
            "append_plan_validation_record", "create_execution", "start_execution",
            "complete_execution", "finish_d3_execution",
            "register_d7_candidate", "apply_external_validation_feedback",
            "start_external_validation_check", "complete_external_validation_check",
        }
        if mutation.operation not in allowed:
            raise StorageError("legacy control operation is not admitted for D3")
        success_change = any(
            isinstance(change, UpsertTaskRun)
            and change.status in {TaskRunStatus.ACHIEVED, TaskRunStatus.ASSUMED_ACHIEVED}
            for change in mutation.state_changes
        )
        feedback = None if mutation.d7 is None else mutation.d7.feedback
        check_completion = (
            None if mutation.d7 is None else mutation.d7.check_completion
        )
        previous_task = self.load_task_run(mutation.task_run_id)
        accepted_feedback = (
            mutation.operation == "apply_external_validation_feedback"
            and feedback is not None
            and feedback.verdict == "accepted"
        )
        accepted_check = (
            mutation.operation == "complete_external_validation_check"
            and check_completion is not None
            and check_completion.result.verdict == "accepted"
            and mutation.event.status is TaskRunStatus.ACHIEVED
        )
        preserved_achieved_feedback = (
            mutation.operation in {"apply_external_validation_feedback", "start_external_validation_check", "complete_external_validation_check"}
            and previous_task is not None
            and previous_task.status is TaskRunStatus.ACHIEVED
            and any(
                isinstance(change, UpsertTaskRun)
                and change.status is TaskRunStatus.ACHIEVED
                for change in mutation.state_changes
            )
        )
        if success_change and not (
            accepted_feedback or accepted_check or preserved_achieved_feedback
        ):
            raise StorageError("D3 has no whole-task success evaluator")
        turn_changes = [change for change in mutation.state_changes if isinstance(change, UpsertTurn)]
        intent_changes = [change for change in mutation.state_changes if isinstance(change, UpsertIntent)]
        plan_changes = [change for change in mutation.state_changes if isinstance(change, UpsertPlan)]
        new_turns = [change for change in turn_changes if self._task_run_for_turn(change.turn_id) is None]
        new_plans = [change for change in plan_changes if self._task_run_for_plan(change.plan_id) is None]
        if new_turns:
            if (
                mutation.operation not in {"register_d3_turn", "advance_d3_turn"}
                or metadata is None or metadata.turn_binding is None
                or len(new_turns) != 1
                or config.max_turns is not None and self._connection.execute(
                    "SELECT COUNT(*) FROM turns WHERE task_run_id = ?",
                    (mutation.task_run_id,),
                ).fetchone()[0] >= config.max_turns
            ):
                raise StorageError("D3 Turn requires one bound registration within its versioned limit")
            turn = new_turns[0]
            binding = metadata.turn_binding
            children = sorted(
                (item for item in intent_changes if item.turn_id == turn.turn_id),
                key=lambda item: item.priority,
            )
            if (
                binding.task_run_id != mutation.task_run_id
                or binding.turn_id != turn.turn_id
                or binding.attachment_index not in config.authorization.attachment_indices
                or not 1 <= len(children) <= 2
                or tuple(item.intent_id for item in children) != turn.intent_ids
                or tuple(item.priority for item in children) != tuple(range(1, len(children) + 1))
                or turn.status is not TurnStatus.PENDING
                or any(item.status is not IntentStatus.PENDING for item in children)
                or {item.intent_id for item in metadata.intent_bindings}
                != set(turn.intent_ids)
                or any(
                    item.task_run_id != mutation.task_run_id
                    or item.turn_id != turn.turn_id
                    for item in metadata.intent_bindings
                )
            ):
                raise StorageError("D3 Turn, Intent and rule bindings differ")
            proposal = self.load_d3_operation(binding.proposal_request_id)
            if (
                proposal is None or proposal.start.task_run_id != mutation.task_run_id
                or proposal.start.category != "outer_model"
                or proposal.completion is None
                or proposal.completion.result_kind != "completed"
            ):
                raise StorageError("D3 Turn requires a known outer proposal")
        elif metadata is not None and (metadata.turn_binding is not None or metadata.intent_bindings):
            raise StorageError("D3 Turn binding requires a new Turn")
        if new_plans:
            if (
                mutation.operation not in {"register_d3_plan", "replan_d3_plan"}
                or metadata is None or metadata.plan_binding is None
                or len(new_plans) != 1
            ):
                raise StorageError("D3 Plan requires one bound registration")
            plan = new_plans[0]
            binding = metadata.plan_binding
            expected_steps = (
                (D3_SIZE_STEP, D3_SHA_STEP)
                if binding.template == "size_then_sha256" else (D3_SHA_STEP,)
            )
            count = self._connection.execute(
                "SELECT COUNT(*) FROM plans WHERE intent_id = ?", (plan.intent_id,)
            ).fetchone()[0]
            if (
                binding.task_run_id != mutation.task_run_id
                or binding.plan_id != plan.plan_id
                or binding.intent_id != plan.intent_id
                or self._task_run_for_intent(plan.intent_id) != mutation.task_run_id
                or self.load_d3_intent_binding(plan.intent_id) is None
                or plan.status is not PlanStatus.RUNNING
                or tuple(step.description for step in plan.steps) != expected_steps
                or count >= 2
            ):
                raise StorageError("D3 Plan declaration or per-Intent limit differs")
            proposal = self.load_d3_operation(binding.proposal_request_id)
            if (
                proposal is None or proposal.start.category != "inner_model"
                or proposal.start.task_run_id != mutation.task_run_id
                or proposal.completion is None
                or proposal.completion.result_kind != "completed"
            ):
                raise StorageError("D3 Plan requires a known inner proposal")
        elif metadata is not None and metadata.plan_binding is not None:
            raise StorageError("D3 Plan binding requires a new Plan")
        if mutation.operation == "complete_d3_plan" and (
            metadata is None or metadata.plan_result is None
        ):
            raise StorageError("D3 Plan completion requires its saved result")
        if metadata is not None and metadata.plan_result is not None:
            result = metadata.plan_result
            binding = self.load_d3_plan_binding(result.plan_id)
            facts = [item for item in mutation.state_changes if isinstance(item, UpsertFact)]
            if (
                binding is None or result.task_run_id != mutation.task_run_id
                or result.artifact_version_id != binding.artifact_version_id
                or self.load_d3_plan_result(result.plan_id) is not None
                or not any(
                    item.plan_id == result.plan_id and item.status is PlanStatus.COMPLETED
                    for item in plan_changes
                )
                or binding.observation_mode == "reuse_saved_proof" and (
                    result.proof_id != binding.source_proof_id
                    or result.supporting_fact_id != binding.source_fact_id
                )
                or result.outcome == "satisfied" and binding.observation_mode == "new_observation" and (
                    len(facts) != 1
                    or facts[0].fact_id != result.supporting_fact_id
                    or facts[0].verification_ref != f"d3:{result.proof_id}"
                )
                or binding.observation_mode == "reuse_saved_proof" and facts
                or result.outcome == "not_satisfied" and facts
            ):
                raise StorageError("D3 Plan result is not bound to its completed Plan")
        elif any(isinstance(item, UpsertFact) for item in mutation.state_changes):
            raise StorageError("D3 Fact requires the independent Plan result transaction")
        if mutation.operation == "apply_d3_review":
            self._validate_d3_review(mutation)
        elif any(isinstance(change, D3ReviewApplication) for change in mutation.state_changes):
            raise StorageError("D3 review requires its dedicated operation")
        if metadata is not None:
            for outcome in metadata.outcomes:
                change = next((
                    item for item in mutation.state_changes
                    if outcome.entity_kind == "turn" and isinstance(item, UpsertTurn) and item.turn_id == outcome.entity_id
                    or outcome.entity_kind == "intent" and isinstance(item, UpsertIntent) and item.intent_id == outcome.entity_id
                    or outcome.entity_kind == "plan" and isinstance(item, UpsertPlan) and item.plan_id == outcome.entity_id
                ), None)
                if (
                    change is None or change.status.value != outcome.status
                    or self.load_d3_outcome(outcome.entity_id) is not None
                ):
                    raise StorageError("D3 outcome requires its own terminal state change")
                if outcome.reason == "target_mismatch" or outcome.reason == "all_candidates_target_mismatch":
                    raise StorageError("fixed v1 rule cannot negate an entire Intent")
        required_outcomes = set()
        for change in mutation.state_changes:
            if isinstance(change, UpsertTurn) and change.status in {
                TurnStatus.COMPLETED, TurnStatus.FAILED,
                TurnStatus.INCONCLUSIVE, TurnStatus.SUPERSEDED,
            }:
                kind, table, key, entity_id = "turn", "turns", "turn_id", change.turn_id
            elif isinstance(change, UpsertIntent) and change.status in {
                IntentStatus.SUCCEEDED, IntentStatus.FAILED,
                IntentStatus.INCONCLUSIVE, IntentStatus.ABANDONED,
                IntentStatus.SKIPPED,
            }:
                kind, table, key, entity_id = "intent", "intents", "intent_id", change.intent_id
            elif isinstance(change, UpsertPlan) and change.status is PlanStatus.SUPERSEDED:
                kind, table, key, entity_id = "plan", "plans", "plan_id", change.plan_id
            else:
                continue
            row = self._connection.execute(
                f"SELECT status FROM {table} WHERE {key} = ?", (entity_id,)
            ).fetchone()
            if row is not None and row["status"] != change.status.value:
                required_outcomes.add((kind, entity_id))
        supplied_outcomes = {
            (item.entity_kind, item.entity_id): item
            for item in metadata.outcomes
        } if metadata is not None else {}
        if not required_outcomes.issubset(supplied_outcomes):
            raise StorageError("D3 terminal transition requires an independent outcome")
        expected_reasons = {
            ("replan_d3_plan", "plan", "superseded"): "replanned",
            ("complete_d3_intent", "intent", "succeeded"): "rule_satisfied",
            ("complete_d3_turn", "turn", "completed"): "turn_goal_satisfied",
            ("complete_d3_turn", "intent", "skipped"): "turn_goal_satisfied",
            ("switch_d3_intent", "intent", "abandoned"): "strategy_abandoned",
            ("switch_d3_intent", "plan", "superseded"): "sibling_switch",
            ("abandon_d8_intent", "intent", "abandoned"): "strategy_abandoned",
            ("abandon_d8_intent", "plan", "superseded"): "sibling_switch",
            ("advance_d3_turn", "turn", "superseded"): "turn_target_adjusted",
            ("advance_d3_turn", "intent", "abandoned"): "turn_target_adjusted",
            ("advance_d3_turn", "intent", "skipped"): "turn_target_adjusted",
            ("advance_d3_turn", "plan", "superseded"): "turn_target_adjusted",
        }
        for kind, entity_id in required_outcomes:
            outcome = supplied_outcomes[(kind, entity_id)]
            expected_reason = expected_reasons.get(
                (mutation.operation, kind, outcome.status)
            )
            if expected_reason is not None and outcome.reason != expected_reason:
                raise StorageError("D3 terminal outcome reason differs from its transition")
        for change in turn_changes:
            row = self._connection.execute(
                "SELECT sequence, turn_target, intent_ids_json FROM turns WHERE turn_id = ?",
                (change.turn_id,),
            ).fetchone()
            if row is not None and (
                row["sequence"], row["turn_target"], tuple(json.loads(row["intent_ids_json"]))
            ) != (change.sequence, change.turn_target, change.intent_ids):
                raise StorageError("registered D3 Turn declaration is immutable")
            if change.status is TurnStatus.COMPLETED and mutation.operation != "complete_d3_turn":
                raise StorageError("D3 Turn completion requires its independent gate")
        for change in intent_changes:
            row = self._connection.execute(
                "SELECT description, priority, intent_type FROM intents WHERE intent_id = ?",
                (change.intent_id,),
            ).fetchone()
            if row is not None and tuple(row) != (
                change.description, change.priority, change.intent_type,
            ):
                raise StorageError("registered D3 Intent declaration is immutable")
            if change.status is IntentStatus.SUCCEEDED and mutation.operation != "complete_d3_intent":
                raise StorageError("D3 Intent success requires its independent gate")
        self._validate_d3_transitions(mutation)

    def _validate_d3_transitions(self, mutation: ControlMutation) -> None:
        before = self.load_control_state_rows(mutation.task_run_id)
        if before is None:
            raise StorageError("D3 transition requires an existing TaskRun")
        after = project_candidate(before, mutation)
        old_turns = {item.turn_id: item for item in before.turns}
        old_intents = {item.intent_id: item for item in before.intents}
        old_plans = {item.plan_id: item for item in before.plans}
        new_turns = {item.turn_id: item for item in after.turns}
        new_intents = {item.intent_id: item for item in after.intents}
        new_plans = {item.plan_id: item for item in after.plans}
        status_changes = tuple(
            ("turn", change.turn_id, change.status) if isinstance(change, UpsertTurn)
            else ("intent", change.intent_id, change.status) if isinstance(change, UpsertIntent)
            else ("plan", change.plan_id, change.status) if isinstance(change, UpsertPlan)
            else None
            for change in mutation.state_changes
        )
        transitions = {
            "turn": {
                (TurnStatus.PENDING, TurnStatus.RUNNING): {"select_d3_intent"},
                (TurnStatus.RUNNING, TurnStatus.COMPLETED): {"complete_d3_turn"},
                (TurnStatus.RUNNING, TurnStatus.SUPERSEDED): {"advance_d3_turn"},
                (TurnStatus.RUNNING, TurnStatus.INCONCLUSIVE): {"exhaust_d3_turn"},
            },
            "intent": {
                (IntentStatus.PENDING, IntentStatus.RUNNING): {"select_d3_intent", "switch_d3_intent"},
                (IntentStatus.PENDING, IntentStatus.SKIPPED): {"complete_d3_turn", "advance_d3_turn"},
                (IntentStatus.RUNNING, IntentStatus.SUCCEEDED): {"complete_d3_intent"},
                (IntentStatus.RUNNING, IntentStatus.ABANDONED): {"switch_d3_intent", "advance_d3_turn", "abandon_d8_intent"},
            },
            "plan": {
                (PlanStatus.RUNNING, PlanStatus.COMPLETED): {"complete_d3_plan"},
                (PlanStatus.RUNNING, PlanStatus.SUPERSEDED): {
                    "replan_d3_plan", "switch_d3_intent", "advance_d3_turn", "abandon_d8_intent",
                },
            },
        }
        for kind, old_items, new_items in (
            ("turn", old_turns, new_turns),
            ("intent", old_intents, new_intents),
            ("plan", old_plans, new_plans),
        ):
            for entity_id, previous in old_items.items():
                current = new_items[entity_id]
                if previous.status == current.status:
                    continue
                if mutation.operation not in transitions[kind].get(
                    (previous.status, current.status), set()
                ):
                    raise StorageError(f"D3 {kind} state transition is not admitted")
        config = self.load_d3_run_config(mutation.task_run_id)
        if sum(item.status is TurnStatus.RUNNING for item in after.turns) > 1 and config.contract_version not in {3, 4, 5, 6, 7}:
            raise StorageError("D3 may run only one Turn")
        if sum(item.status is IntentStatus.RUNNING for item in after.intents) > 1:
            raise StorageError("D3 may run only one Intent")
        if mutation.operation == "register_d3_turn":
            created = [item for item in after.turns if item.turn_id not in old_turns]
            if len(created) != 1 or any(
                item.status not in {
                    TurnStatus.COMPLETED, TurnStatus.FAILED,
                    TurnStatus.INCONCLUSIVE, TurnStatus.SUPERSEDED,
                } for item in before.turns
            ):
                raise StorageError("D3 next Turn requires the previous Turn to be terminal")
            if before.turns:
                old_turn = before.turns[-1]
                outcome = self.load_d3_outcome(old_turn.turn_id)
                allowed_reasons = {
                    "turn_goal_satisfied",
                    "candidates_exhausted_after_strategy_abandonment",
                }
                if (
                    outcome is None
                    or outcome.reason not in allowed_reasons
                    or not any(
                        item.turn_id == old_turn.turn_id
                        and item.review_scope == "turn_end"
                        and item.decision == "acknowledge_turn_end"
                        and not item.paused_gate
                        for item in self.list_d3_review_applications(mutation.task_run_id)
                    )
                ):
                    raise StorageError("D3 exhausted Turn needs its accepted tail review")
        if mutation.operation == "select_d3_intent":
            activated = [
                item for item in after.intents
                if item.intent_id in old_intents
                and old_intents[item.intent_id].status is IntentStatus.PENDING
                and item.status is IntentStatus.RUNNING
            ]
            if (
                len(activated) != 1 or activated[0].priority != 1
                or old_turns[activated[0].turn_id].status is not TurnStatus.PENDING
                or new_turns[activated[0].turn_id].status is not TurnStatus.RUNNING
            ):
                raise StorageError("D3 first selection must use the highest-priority candidate")
            expected = {
                ("turn", activated[0].turn_id, TurnStatus.RUNNING),
                ("intent", activated[0].intent_id, IntentStatus.RUNNING),
            }
            if set(status_changes) != expected or len(status_changes) != len(expected):
                raise StorageError("D3 first selection must change only its Turn and Intent")
        if mutation.operation in {"switch_d3_intent", "abandon_d8_intent"}:
            old_active = [
                item for item in before.intents if item.status is IntentStatus.RUNNING
            ]
            new_active = [
                item for item in after.intents if item.status is IntentStatus.RUNNING
            ]
            pending = [
                item for item in before.intents
                if old_active and item.turn_id == old_active[0].turn_id
                and item.status is IntentStatus.PENDING
            ]
            if (
                len(old_active) != 1
                or mutation.operation == "abandon_d8_intent" and (config.contract_version not in {5, 6, 7} or pending or new_active)
                or mutation.operation == "switch_d3_intent" and (not pending or len(new_active) != 1)
                or pending and new_active[0].turn_id != old_active[0].turn_id
                or new_intents[old_active[0].intent_id].status is not IntentStatus.ABANDONED
                or pending and new_active[0].priority != min(item.priority for item in pending)
            ):
                raise StorageError("D3 sibling switch must choose the highest-priority untried Intent")
            expected = {
                ("intent", old_active[0].intent_id, IntentStatus.ABANDONED),
                *(
                    ("plan", item.plan_id, PlanStatus.SUPERSEDED)
                    for item in before.plans
                    if item.intent_id == old_active[0].intent_id
                    and item.status is PlanStatus.RUNNING
                ),
            }
            if new_active:
                expected.add(("intent", new_active[0].intent_id, IntentStatus.RUNNING))
            if set(status_changes) != expected or len(status_changes) != len(expected):
                raise StorageError("D3 sibling switch must close old running Plans")
        if mutation.operation == "advance_d3_turn":
            created = [item for item in after.turns if item.turn_id not in old_turns]
            old_turn = before.turns[-1] if before.turns else None
            active = [item for item in before.intents if old_turn is not None
                      and item.turn_id == old_turn.turn_id
                      and item.status is IntentStatus.RUNNING]
            if (
                old_turn is None or old_turn.status is not TurnStatus.RUNNING
                or len(created) != 1 or len(active) != 1
            ):
                raise StorageError("D3 adjusted Turn requires one active predecessor")
            expected = {
                ("turn", old_turn.turn_id, TurnStatus.SUPERSEDED),
                ("turn", created[0].turn_id, TurnStatus.PENDING),
                ("intent", active[0].intent_id, IntentStatus.ABANDONED),
                *(
                    ("intent", item.intent_id, IntentStatus.SKIPPED)
                    for item in before.intents
                    if item.turn_id == old_turn.turn_id
                    and item.status is IntentStatus.PENDING
                ),
                *(
                    ("intent", item.intent_id, IntentStatus.PENDING)
                    for item in after.intents
                    if item.turn_id == created[0].turn_id
                ),
                *(
                    ("plan", item.plan_id, PlanStatus.SUPERSEDED)
                    for item in before.plans
                    if item.intent_id == active[0].intent_id
                    and item.status is PlanStatus.RUNNING
                ),
            }
            if set(status_changes) != expected or len(status_changes) != len(expected):
                raise StorageError("D3 adjusted Turn must close remaining candidates")
        if mutation.operation == "complete_d3_intent":
            completed = [
                item for item in after.intents
                if item.intent_id in old_intents
                and old_intents[item.intent_id].status is IntentStatus.RUNNING
                and item.status is IntentStatus.SUCCEEDED
            ]
            if len(completed) != 1:
                raise StorageError("D3 Intent completion needs one active Intent")
            plans = [item for item in after.plans if item.intent_id == completed[0].intent_id]
            if not plans or plans[-1].status is not PlanStatus.COMPLETED:
                raise StorageError("D3 Intent completion needs its latest completed Plan")
            result = self.load_d3_plan_result(plans[-1].plan_id)
            if result is None or result.outcome != "satisfied":
                raise StorageError("D3 latest Plan has no trusted satisfied support")
        if mutation.operation == "complete_d3_turn":
            completed = [
                item for item in after.turns
                if item.turn_id in old_turns
                and old_turns[item.turn_id].status is TurnStatus.RUNNING
                and item.status is TurnStatus.COMPLETED
            ]
            if len(completed) != 1 or sum(
                new_intents[intent_id].status is IntentStatus.SUCCEEDED
                for intent_id in completed[0].intent_ids
            ) != 1 or any(
                new_intents[intent_id].status not in {
                    IntentStatus.SUCCEEDED, IntentStatus.ABANDONED,
                    IntentStatus.FAILED, IntentStatus.SKIPPED,
                } for intent_id in completed[0].intent_ids
            ):
                raise StorageError("D3 completed Turn must close all sibling candidates")
            expected = {
                ("turn", completed[0].turn_id, TurnStatus.COMPLETED),
                *(
                    ("intent", item.intent_id, IntentStatus.SKIPPED)
                    for item in before.intents
                    if item.turn_id == completed[0].turn_id
                    and item.status is IntentStatus.PENDING
                ),
            }
            if set(status_changes) != expected or len(status_changes) != len(expected):
                raise StorageError("D3 Turn completion changed an unrelated candidate")
        plan_binding = (
            self.load_d3_plan_binding(mutation.d3.plan_result.plan_id)
            if mutation.d3 is not None and mutation.d3.plan_result is not None
            else None
        )
        if mutation.operation == "complete_d3_plan" and mutation.d3 is not None and (
            mutation.d3.plan_binding is None and mutation.d3.plan_result is not None
        ) and plan_binding is not None and plan_binding.observation_mode == "new_observation":
            changed = [
                item for item in after.plans
                if item.plan_id in old_plans
                and old_plans[item.plan_id].status is PlanStatus.RUNNING
                and item.status is PlanStatus.COMPLETED
            ]
            if len(changed) != 1 or any(
                sum(
                    execution.plan_id == changed[0].plan_id
                    and execution.step_index == step_index
                    and execution.status is ExecutionStatus.SUCCEEDED
                    for execution in before.executions
                ) != 1 for step_index in range(len(changed[0].steps))
            ):
                raise StorageError("D3 observed Plan cannot finish with an unexecuted fixed step")

    def _validate_d3_review(self, mutation: ControlMutation) -> None:
        if len(mutation.state_changes) != 1 or not isinstance(mutation.state_changes[0], D3ReviewApplication):
            raise StorageError("D3 review requires one typed application")
        application = mutation.state_changes[0]
        if (
            application.task_run_id != mutation.task_run_id
            or self._task_run_for_turn(application.turn_id) != mutation.task_run_id
            or self._task_run_for_intent(application.intent_id) != mutation.task_run_id
            or self._connection.execute(
                "SELECT turn_id FROM intents WHERE intent_id = ?", (application.intent_id,)
            ).fetchone()["turn_id"] != application.turn_id
            or self.load_d3_review_application(mutation.task_run_id, application.review_id) is not None
        ):
            raise StorageError("D3 review scope or immutable identity differs")
        prior = self.list_d3_review_applications(mutation.task_run_id)
        expected_version = max((item.constraint_version for item in prior), default=0) + bool(application.output_constraints)
        if application.constraint_version != expected_version:
            raise StaleControlStateError("D3 constraint version is not current")
        if not application.paused_gate and any(
            item.review_root_id == application.review_root_id
            and item.review_generation == application.review_generation
            and not item.paused_gate for item in prior
        ):
            raise StorageError("D3 review generation already has an accepted application")
        for constraint in application.output_constraints:
            expected_scope = application.intent_id if constraint.scope_kind == "intent" else application.turn_id
            if constraint.scope_id != expected_scope:
                raise StorageError("D3 constraint scope differs from its review")
            previous = [
                item for item in prior for item in item.output_constraints
                if item.scope_kind == constraint.scope_kind and item.scope_id == constraint.scope_id
                and type(item) is type(constraint)
            ]
            if isinstance(constraint, D2AllowedActionKinds):
                admitted = {"probe", "complete_intent"}
                for item in previous:
                    admitted &= set(item.allowed_action_kinds)
                if not set(constraint.allowed_action_kinds).issubset(admitted):
                    raise StorageError("D3 action kinds can only tighten")
            else:
                limit = min((item.max_action_timeout_seconds for item in previous), default=300)
                if constraint.max_action_timeout_seconds > limit:
                    raise StorageError("D3 action timeout can only tighten")

    def _save_d3_mutation(self, mutation: ControlMutation, event_sequence: int) -> None:
        metadata = mutation.d3
        if metadata is not None:
            if metadata.config is not None:
                self._connection.execute(
                    "INSERT INTO d3_runs VALUES (?, ?, ?)",
                    (mutation.task_run_id, canonical_json(d3_payload(metadata.config)), event_sequence),
                )
            if metadata.turn_binding is not None:
                binding = metadata.turn_binding
                self._connection.execute(
                    "INSERT INTO d3_turn_bindings VALUES (?, ?, ?, ?)",
                    (binding.turn_id, binding.task_run_id, canonical_json(d3_payload(binding)), event_sequence),
                )
            for binding in metadata.intent_bindings:
                self._connection.execute(
                    "INSERT INTO d3_intent_bindings VALUES (?, ?, ?, ?, ?)",
                    (binding.intent_id, binding.task_run_id, binding.turn_id,
                     canonical_json(d3_payload(binding)), event_sequence),
                )
            if metadata.plan_binding is not None:
                binding = metadata.plan_binding
                self._connection.execute(
                    "INSERT INTO d3_plan_bindings VALUES (?, ?, ?, ?, ?)",
                    (binding.plan_id, binding.task_run_id, binding.intent_id,
                     canonical_json(d3_payload(binding)), event_sequence),
                )
            if metadata.plan_result is not None:
                result = metadata.plan_result
                self._connection.execute(
                    "INSERT INTO d3_plan_results VALUES (?, ?, ?, ?)",
                    (result.plan_id, result.task_run_id, canonical_json(d3_payload(result)), event_sequence),
                )
            for outcome in metadata.outcomes:
                self._connection.execute(
                    "INSERT INTO d3_outcomes VALUES (?, ?, ?, ?, ?)",
                    (outcome.entity_id, mutation.task_run_id, outcome.entity_kind,
                     canonical_json(d3_payload(outcome)), event_sequence),
                )
        for change in mutation.state_changes:
            if not isinstance(change, D3ReviewApplication):
                continue
            self._connection.execute(
                "INSERT INTO d3_review_applications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (change.review_id, change.task_run_id, change.review_root_id,
                 change.review_generation, change.turn_id, change.intent_id,
                 change.decision, int(change.paused_gate), change.constraint_version,
                 canonical_json(d3_payload(change)), event_sequence),
            )

    def commit(self, mutation: ControlMutation) -> ControlCommitResult:
        expected_fingerprint = control_mutation_fingerprint(mutation)
        if mutation.request_fingerprint != expected_fingerprint:
            raise IdempotencyConflictError(
                "ControlMutation request_fingerprint does not match its content"
            )
        commit_started_audit = self._prepare_control_commit_audit(mutation)

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
                if commit_started_audit is not None:
                    self._insert_d4_audit_event(commit_started_audit)
                    committed_id = self._d4_audit_event_id(
                        mutation.task_run_id,
                        mutation.mutation_id,
                        "control_commit_committed",
                        "known",
                    )
                    if self.load_d4_audit_event(committed_id) is None:
                        self._insert_d4_audit_event(
                            self._control_commit_audit_event(
                                mutation,
                                "control_commit_committed",
                                "known",
                                result.checkpoint.created_at,
                            )
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
            self._validate_d1_mutation(mutation)
            self._validate_d2_mutation(mutation)
            self._validate_d7_mutation(mutation)
            self._validate_d3_mutation(mutation)
            self._validate_d5_mutation(mutation)
            self._validate_d8_mutation(mutation)
            self._validate_existing_owners(mutation)
            self._apply_state_changes(
                mutation,
                event_sequence=event_sequence,
                committed_at=committed_at,
            )
            self._save_d1_mutation(mutation, event_sequence)
            self._save_d2_mutation(mutation, event_sequence)
            self._save_d3_mutation(mutation, event_sequence)
            self._save_d5_mutation(mutation, event_sequence)
            self._save_d8_mutation(mutation, event_sequence)
            save_handoff_mutation(self, mutation, event_sequence)
            self._save_d7_mutation(mutation, event_sequence)
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
            if commit_started_audit is not None:
                self._insert_d4_audit_event(commit_started_audit)
                self._insert_d4_audit_event(
                    self._control_commit_audit_event(
                        mutation,
                        "control_commit_committed",
                        "known",
                        committed_at,
                    )
                )
            self._connection.commit()
            return ControlCommitResult(
                event=_numbered_event(mutation.event, event_sequence),
                checkpoint=checkpoint,
                replayed=False,
            )
        except (IdempotencyConflictError, StaleControlStateError) as exc:
            self._abort_control_commit(mutation, exc)
            raise
        except StorageError as exc:
            self._abort_control_commit(mutation, exc)
            raise
        except sqlite3.Error as exc:
            self._abort_control_commit(mutation, exc)
            raise StorageError(f"SQLite control transaction failed: {exc}") from exc
        except Exception as exc:
            self._abort_control_commit(mutation, exc)
            raise StorageError(f"control transaction failed: {exc}") from exc

    def list_task_run_refs(self) -> tuple[str, ...]:
        """Read the database ownership index, including from a read-only store."""
        try:
            rows = self._connection.execute(
                "SELECT task_run_id FROM task_runs ORDER BY task_run_id"
            ).fetchall()
            return tuple(row["task_run_id"] for row in rows)
        except sqlite3.Error as exc:
            raise StorageError("could not read TaskRun ownership index") from exc

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
                WHERE m.task_run_id = ? AND e.status NOT IN (
                    'running', 'awaiting_confirmation', 'awaiting_external_feedback'
                )
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
                    parent_turn_id=(
                        row["parent_turn_id"] if self._schema_version >= 22 else None
                    ),
                    triggering_intent_id=(
                        row["triggering_intent_id"]
                        if self._schema_version >= 22
                        else None
                    ),
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
        d3_config = self.load_d3_run_config(row["task_run_id"])
        allow_empty_target = (
            d3_config is not None
            and d3_config.authorization.attachment_indices == ()
        )
        request = TaskRequest.from_dict(
            request_value, allow_empty_target=allow_empty_target
        )
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
            AppendD2ReviewApplication, D3ReviewApplication, D5ProcessFact, D8Conclusion, *D8_CHANGES,
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
                if mutation.operation in {"commit_verified_file_type_fact", "commit_verified_t6_observation", "commit_verified_t6_fact", "complete_d3_plan"} and self._connection.execute(
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
            elif isinstance(change, (AppendD2ReviewApplication, D3ReviewApplication, D5ProcessFact, D8Conclusion, *D8_CHANGES)):
                # Append-only review rows are saved with the same event and checkpoint.
                pass
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
        elif isinstance(change, AppendD2ReviewApplication):
            if self._connection.execute(
                "SELECT 1 FROM task_runs WHERE task_run_id = ?", (task_run_id,)
            ).fetchone() is None:
                raise StorageError("D2 review application TaskRun does not exist")
            return
        elif isinstance(change, (D3ReviewApplication, D5ProcessFact, D8Conclusion, *D8_CHANGES)):
            if change.task_run_id != task_run_id or self._connection.execute(
                "SELECT 1 FROM task_runs WHERE task_run_id = ?", (task_run_id,)
            ).fetchone() is None:
                raise StorageError("D3 review application TaskRun does not exist")
            return
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
                turn_id, task_run_id, sequence, turn_target, intent_ids_json, status,
                parent_turn_id, triggering_intent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                sequence = excluded.sequence,
                turn_target = excluded.turn_target,
                intent_ids_json = excluded.intent_ids_json,
                status = excluded.status,
                parent_turn_id = COALESCE(excluded.parent_turn_id, turns.parent_turn_id),
                triggering_intent_id = COALESCE(
                    excluded.triggering_intent_id, turns.triggering_intent_id
                )
            WHERE turns.task_run_id = excluded.task_run_id
            """,
            (
                change.turn_id,
                change.task_run_id,
                change.sequence,
                change.turn_target,
                canonical_json(list(change.intent_ids)),
                change.status.value,
                change.parent_turn_id,
                change.triggering_intent_id,
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
