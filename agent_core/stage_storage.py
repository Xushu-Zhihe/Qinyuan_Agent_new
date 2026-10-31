"""Stage state in the existing SQLite/control transaction, without a scheduler."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass, replace
from uuid import NAMESPACE_URL, uuid5

from qinyuan_core import Category, Layer, QinyuanError
from agent_core.control_store import StorageError, UpsertTaskRun, canonical_json
from agent_core.domain import FactStatus, IntentStatus
from agent_core.actions import PUBLIC_REFERENCE_TOOLS
from agent_core.conclusion_revision_contracts import D8ChangeDecision, D8RevisionRecord
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.operation_accounting import OperationIdentityConflict, OperationRecoveryRequired, D8HandoffBudgetPolicy, _text, _uuid, _sha
from agent_core.runtime import TaskRunStatus
from agent_core.stage_contracts import (
    PlanningExhausted, PlanningSession, RoadCandidate, RoadResolution, RoadSnapshot,
    StageConclusionChange, StageFactApplication, StageFactProposal, StageFactSelection, StageHistoryRead,
    StageModelEvidence, StageReviewBinding, StageRunConfig, StageStateChange, StageTurnSnapshot,
    TurnAchievement, TurnCompletion, TurnIntent, TurnPlanningReview, TurnPlanningState, StagePlanningDecision, MAX_IN_FLIGHT_TOOLS,
    StageToolBatch, tool_calls, tool_call_key, StageFactBatch, StageFactBatchReview,
    fact_candidates, reviewed_fact, fact_verification_ref, StageQueuedReview, StageBranchFork, StageSessionBranch,
)
from agent_core.turn_contracts import D1RoleConfig


SCHEMA = """
CREATE TABLE stage_runs (
 task_run_id TEXT PRIMARY KEY REFERENCES task_runs(task_run_id), config_json TEXT NOT NULL);
CREATE TABLE stage_turns (
 turn_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES stage_runs(task_run_id),
 parent_turn_id TEXT REFERENCES stage_turns(turn_id), intent_json TEXT NOT NULL,
 planning_state TEXT NOT NULL, achievement_json TEXT, fact_refs_json TEXT NOT NULL);
CREATE INDEX stage_turns_run ON stage_turns(task_run_id);
CREATE TABLE stage_roads (
 road_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES stage_turns(turn_id),
 ordinal INTEGER NOT NULL, candidate_json TEXT NOT NULL, status TEXT NOT NULL,
 UNIQUE(turn_id,ordinal));
CREATE TABLE stage_sessions (
 task_run_id TEXT NOT NULL REFERENCES stage_runs(task_run_id), scope TEXT NOT NULL,
 owner_id TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE,
 PRIMARY KEY(task_run_id,scope,owner_id));
"""

REVIEW_QUEUE_SCHEMA = """
CREATE TABLE stage_review_queue (
 proposal_operation_id TEXT PRIMARY KEY REFERENCES d4_operations(operation_id),
 state TEXT NOT NULL CHECK(state IN ('pending','blocked','settled')),
 review_operation_id TEXT UNIQUE REFERENCES d4_operations(operation_id),
 CHECK((state='pending') = (review_operation_id IS NULL)));
"""

ASYNC_REVIEW_SCHEMA = """
CREATE TABLE stage_review_queue_async (
 proposal_operation_id TEXT PRIMARY KEY REFERENCES d4_operations(operation_id),
 state TEXT NOT NULL CHECK(state IN ('pending','blocked','settled','superseded')),
 review_operation_id TEXT REFERENCES d4_operations(operation_id),
 CHECK((state='pending') = (review_operation_id IS NULL)));
INSERT INTO stage_review_queue_async SELECT * FROM stage_review_queue ORDER BY rowid;
DROP TABLE stage_review_queue;
ALTER TABLE stage_review_queue_async RENAME TO stage_review_queue;
CREATE TABLE stage_branches (
 branch_id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES stage_sessions(session_id),
 proposal_operation_id TEXT NOT NULL REFERENCES d4_operations(operation_id),
 review_operation_id TEXT REFERENCES d4_operations(operation_id));
CREATE INDEX stage_branches_session ON stage_branches(session_id);
"""

PLAN_BINDING_SCHEMA = """
CREATE TABLE stage_plan_bindings (
 session_id TEXT PRIMARY KEY REFERENCES stage_sessions(session_id),
 proposal_operation_id TEXT NOT NULL REFERENCES d4_operations(operation_id));
"""

# Keep one canonical Fact table. Ownership accepts a stage Turn directly;
# a reviewed tool source still belongs to that exact TaskRun.
FACT_SCHEMA = """
CREATE TABLE facts_stage (
 fact_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, content TEXT NOT NULL,
 source_ref TEXT NOT NULL, status TEXT NOT NULL,
 created_event_sequence INTEGER NOT NULL, created_event_ordinal INTEGER NOT NULL,
 verification_ref TEXT);
INSERT INTO facts_stage SELECT * FROM facts;
DROP TABLE facts;
ALTER TABLE facts_stage RENAME TO facts;
CREATE INDEX idx_facts_context ON facts(turn_id,status,created_event_sequence DESC,created_event_ordinal DESC);
CREATE UNIQUE INDEX stage_fact_review ON facts(verification_ref) WHERE verification_ref LIKE 'stage:%';
CREATE TRIGGER facts_turn_insert BEFORE INSERT ON facts
WHEN NOT EXISTS(SELECT 1 FROM turns WHERE turn_id=NEW.turn_id)
 AND NOT EXISTS(SELECT 1 FROM stage_turns WHERE turn_id=NEW.turn_id)
BEGIN SELECT RAISE(ABORT,'Fact Turn unavailable'); END;
CREATE TRIGGER facts_source_insert BEFORE INSERT ON facts
WHEN NOT EXISTS(SELECT 1 FROM executions WHERE execution_id=NEW.source_ref)
 AND NOT EXISTS(
 SELECT 1 FROM d4_operations s JOIN d4_operations r
 ON r.operation_id=substr(NEW.verification_ref,CASE WHEN NEW.verification_ref LIKE 'stage:%' THEN 7 ELSE 5 END)
 WHERE s.operation_id=NEW.source_ref AND s.task_run_id=r.task_run_id
 AND s.category='tool_dispatch' AND r.category='fact_review'
 AND s.completion_json IS NOT NULL AND r.completion_json IS NOT NULL
 AND ((NEW.verification_ref LIKE 'stage:%' AND EXISTS(
   SELECT 1 FROM stage_turns t WHERE t.turn_id=NEW.turn_id AND t.task_run_id=s.task_run_id))
 OR (NEW.verification_ref LIKE 'd8e:%' AND EXISTS(
   SELECT 1 FROM turns t WHERE t.turn_id=NEW.turn_id AND t.task_run_id=s.task_run_id))))
BEGIN SELECT RAISE(ABORT,'Fact source unavailable'); END;
"""

# Existing Fact rows and hashes stay unchanged. A batch claim adds only its
# ordinal to the original review reference; the FK trigger resolves that review.
FACT_BATCH_SCHEMA = "DROP TRIGGER facts_source_insert;\n" + FACT_SCHEMA[
    FACT_SCHEMA.index("CREATE TRIGGER facts_source_insert"):].replace(
        "substr(NEW.verification_ref,CASE WHEN NEW.verification_ref LIKE 'stage:%' THEN 7 ELSE 5 END)",
        "CASE WHEN NEW.verification_ref LIKE 'stage:%' THEN substr(NEW.verification_ref,7,36) ELSE substr(NEW.verification_ref,5) END")

CANDIDATE_SCHEMA = """
CREATE TABLE d7_candidate_registry_stage (
 candidate_id TEXT PRIMARY KEY, task_run_id TEXT NOT NULL REFERENCES task_runs(task_run_id),
 source_kind TEXT NOT NULL CHECK(source_kind IN ('d3','web','mcp')),
 turn_id TEXT NOT NULL, intent_id TEXT NOT NULL, plan_id TEXT, execution_id TEXT,
 evidence_version TEXT NOT NULL, source_ref TEXT NOT NULL, candidate_json TEXT NOT NULL,
 created_event_sequence INTEGER NOT NULL,
 CHECK((source_kind='mcp' AND plan_id IS NULL AND execution_id IS NULL) OR
       (source_kind!='mcp' AND plan_id IS NOT NULL AND execution_id IS NOT NULL)),
 UNIQUE(task_run_id,candidate_id));
INSERT INTO d7_candidate_registry_stage SELECT * FROM d7_candidate_registry;
DROP TABLE d7_candidate_registry;
ALTER TABLE d7_candidate_registry_stage RENAME TO d7_candidate_registry;
CREATE INDEX idx_d7_candidate_registry_task_turn ON d7_candidate_registry(task_run_id,turn_id);
CREATE TRIGGER candidate_owner_insert BEFORE INSERT ON d7_candidate_registry
WHEN NOT EXISTS(SELECT 1 FROM turns t JOIN intents i ON i.turn_id=t.turn_id
 WHERE t.task_run_id=NEW.task_run_id AND t.turn_id=NEW.turn_id AND i.intent_id=NEW.intent_id)
 AND NOT EXISTS(SELECT 1 FROM stage_turns t JOIN stage_roads r ON r.turn_id=t.turn_id
 WHERE t.task_run_id=NEW.task_run_id AND t.turn_id=NEW.turn_id AND r.road_id=NEW.intent_id AND NEW.source_kind='mcp')
BEGIN SELECT RAISE(ABORT,'Candidate owner unavailable'); END;
"""


class StageStateError(QinyuanError, StorageError):
    category = Category.CONFLICT
    layer = Layer.STORE
    code = "stage.state_conflict"


class StageAdmissionError(StageStateError, OperationIdentityConflict):
    category = Category.POLICY


class StageRecoveryRequired(StageStateError, OperationRecoveryRequired):
    def __init__(self, code, *, category=Category.UNKNOWN, layer=Layer.MODEL, **context):
        self.category = category
        super().__init__(code, code=code, layer=layer, retryable=False, **context)


def _fail(code, category=Category.CONFLICT, **context):
    error = StageStateError(code, code=code, retryable=False, **context)
    error.category = category
    raise error


def fact_scope_failure(item, scope, *, expected_turn_id=None, fact_id=None,
                       proposal_id=None, review_id=None, reason="observation_owner_mismatch"):
    _fail("stage.fact_source_scope_changed", Category.POLICY, fact_source={
        "reason": reason, "fact_id": fact_id, "proposal_operation_id": proposal_id,
        "review_operation_id": review_id, "source_ref": item.ref if item else None,
        "source_identity": item.identity if item else None,
        "expected_scope": scope.scope, "expected_owner_id": scope.owner_id,
        "expected_turn_id": expected_turn_id, "actual_turn_id": item.turn_id if item else None,
        "actual_road_id": item.intent_id if item else None})


def execution_policy(config):
    """One projection shared by runtime binding and read-only public checks."""
    return json.loads(json.dumps({"authorization": asdict(config.authorization),
        "web_authorization": asdict(config.web_authorization) if config.web_authorization else None,
        "tcp_authorization": asdict(config.tcp_authorization) if config.tcp_authorization else None,
        "capacity": asdict(config.handoff_config),
        "human_confirmation": "assume_after_300s_v1",
        "review_queue": {"binding": "immutable_proposal_v1", "dispatch": "async_planning_v1"},
        "tool_execution": {"max_in_flight": MAX_IN_FLIGHT_TOOLS, "sandbox_target_in_flight": 1,
                           "max_calls_per_decision": MAX_IN_FLIGHT_TOOLS,
                           "wake": "first_result_before_next_decision_merge_during_generation"}}))


def human_confirmation_policy(store, task_id):
    """Read the frozen timeout rule; historical runs retain their original behavior."""
    config = require_run(store, task_id)
    evidence = store.stage_evidence
    policy = evidence.read("policy", task_id) if evidence is not None else None
    if not isinstance(policy, dict) or config.execution_policy_ref != "sha256:" + hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest():
        _fail("stage.execution_configuration_changed", Category.UNKNOWN)
    value = policy.get("human_confirmation")
    if value not in {None, "assume_after_300s_v1"}:
        _fail("stage.execution_configuration_changed", Category.POLICY)
    return value


def proposal_digest(value):
    payload = asdict(value) if is_dataclass(value) else [asdict(i) for i in value]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def load_config(store, task_id):
    if store._schema_version < 32:
        return None
    row = store._connection.execute(
        "SELECT config_json FROM stage_runs WHERE task_run_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        roles = []
        for name in ("outer_role", "inner_role"):
            role = data[name]
            role["parameters"] = tuple(tuple(pair) for pair in role["parameters"])
            roles.append(D1RoleConfig(**role))
        return StageRunConfig(*roles, D8HandoffBudgetPolicy.from_dict(data["budget"]),
            data.get("execution_policy_ref"), data.get("goal_validation"))
    except (ValueError, TypeError, KeyError):
        _fail("stage.config_corrupt", Category.UNKNOWN)


def require_run(store, task_id):
    config = load_config(store, task_id)
    if config is None:
        _fail("stage.run_unsupported", Category.POLICY)
    return config


def live_tool_requests(store, task_id):
    live = getattr(store, "live_tool_request", lambda identity: False)
    return tuple(record for record in store.list_d4_operations(task_id)
                 if record.start.category == "action" and live(record.start.operation_id))


def validate_tool_start(store, start):
    """Bind a new dispatch to the exact admitted Inner decision and live Road."""
    config = require_run(store, start.task_run_id)
    parts = start.resource_ref.split("/")
    experiment = len(parts) == 5 and parts[0] == "mcp" and parts[2] == "experiment"
    if config.execution_policy_ref is None or not (len(parts) == 3 and parts[0] == "mcp" or experiment):
        _fail("stage.tool_scope_invalid", Category.POLICY)
    road_id, proposal_id = parts[1], parts[3] if experiment else parts[2]
    row = store._connection.execute("SELECT r.status,t.turn_id,t.planning_state,t.achievement_json "
        "FROM stage_roads r JOIN stage_turns t ON t.turn_id=r.turn_id WHERE r.road_id=? AND t.task_run_id=?",
        (road_id, start.task_run_id)).fetchone()
    if (row is None or row["status"] != IntentStatus.RUNNING.value
            or row["planning_state"] != TurnPlanningState.ACTIVE.value):
        _fail("stage.tool_road_not_running", Category.POLICY)
    operation = store.load_d4_operation(proposal_id)
    if store.stage_evidence is None:
        _fail("stage.evidence_unconfigured", Category.DEPENDENCY)
    live_experiment = experiment and getattr(store, "live_tool_request", lambda identity: False)(proposal_id)
    applied_batch = (operation is not None and operation.application_ref == proposal_id
        and getattr(store, "live_tool_request", lambda identity: False)(proposal_id)
        and isinstance(store.stage_evidence.read_model_result(operation).result, StageToolBatch))
    if (operation is None or operation.start.task_run_id != start.task_run_id or operation.start.category != "action"
            or operation.start.role != "inner_action" or operation.start.role_config_ref != config.inner_role.role_config_ref
            or (operation.start.base_checkpoint_sequence != start.base_checkpoint_sequence and not live_experiment)
            or operation.completion is None or operation.completion.result_kind != "completed"
            or (operation.application_ref is not None and not applied_batch
                and not (live_experiment and operation.application_ref == proposal_id))):
        _fail("stage.tool_proposal_not_ready", Category.POLICY)
    evidence = store.stage_evidence.read_model_result(operation)
    saved = store.stage_evidence.read("mcp-call", start.operation_id)
    proposed = next((call for call in tool_calls(evidence.result) if saved and asdict(call) == saved.get("call")), None)
    if evidence.session != PlanningSession("road_execution", road_id) or not tool_calls(evidence.result):
        _fail("stage.tool_proposal_scope_changed", Category.POLICY)
    if proposed is None:
        _fail("stage.tool_proposal_changed", Category.UNKNOWN)
    others = [call for parent, call in getattr(store, "live_tool_calls", lambda: ())()
              if parent != proposal_id or call != proposed]
    if (len(others) >= MAX_IN_FLIGHT_TOOLS or proposed.tool_name not in PUBLIC_REFERENCE_TOOLS
            and any(call.tool_name not in PUBLIC_REFERENCE_TOOLS for call in others)):
        _fail("stage.tool_slot_unavailable", Category.POLICY)
    from agent_core.stage_knowledge import StageKnowledge
    knowledge = StageKnowledge(store.stage_evidence, start.task_run_id, store=store)
    turn = knowledge.turns[row["turn_id"]]
    if active_turn(tuple(knowledge.turns.values())).turn_id != turn.turn_id:
        _fail("stage.execution_frontier_changed", Category.POLICY)
    road = next(r for r in turn.roads if r.road_id == road_id)
    from agent_core.stage_plans import execution_allowed
    if not execution_allowed(store, start.task_run_id, turn, road):
        _fail("stage.parent_completion_not_accepted", Category.POLICY)
    _sources(store, start.task_run_id, knowledge.effective_premises(turn, road))
    claim = {"task_run_id": start.task_run_id, "intent_id": road_id,
             "model_request_id": proposal_id, "call": asdict(proposed)}
    if experiment:
        if (saved is None or proposed.tool_name != "http.run_experiment"
                or not parts[4].isdigit() or int(parts[4]) < 1
                or any(saved.get(key) != value for key, value in claim.items())
                or saved["experiment_request"]["ordinal"] != int(parts[4])):
            _fail("stage.experiment_binding_changed", Category.POLICY)
        claim = saved
    else:
        from agent_core.turn_modeling import stable_id
        if start.operation_id != stable_id(start.task_run_id, "mcp/" + tool_call_key(proposal_id, evidence.result, proposed)):
            _fail("stage.tool_proposal_changed", Category.UNKNOWN)
    if start.context_sha256 != hashlib.sha256(canonical_json(claim).encode("utf-8")).hexdigest():
        _fail("stage.tool_proposal_changed", Category.UNKNOWN)


def read_history(store, task_id, refs):
    require_run(store, task_id)
    _refs(refs)
    if not refs:
        return ()
    if store.stage_evidence is None:
        _fail("stage.evidence_unconfigured", Category.DEPENDENCY)
    items = store.stage_evidence.read_history(task_id, refs)
    if (not isinstance(items, tuple) or len(items) != len(refs)
            or any(not isinstance(i, D8HistoryItem) or i.task_run_id != task_id for i in items)
            or {i.ref for i in items} != set(refs)):
        _fail("stage.history_binding_changed", Category.UNKNOWN)
    by_ref = {item.ref: item for item in items}
    return tuple(by_ref[ref] for ref in refs)


def _sources(store, task_id, refs, *, formal=False):
    from agent_core.conclusion_revisions import ConclusionRevisions
    items = read_history(store, task_id, refs)
    if any(not ConclusionRevisions.reliable(i) for i in items):
        _fail("stage.source_not_effective", Category.POLICY)
    if formal and any(i.record_type != "verified_fact" for i in items):
        _fail("stage.formal_fact_required", Category.POLICY)


def _refs(refs):
    if (not isinstance(refs, tuple) or any(not isinstance(r, str) or not r for r in refs)
            or len(set(refs)) != len(refs)):
        _fail("stage.refs_invalid", Category.INPUT)


def load_turn(store, task_id, turn_id):
    require_run(store, task_id)
    row = store._connection.execute(
        "SELECT * FROM stage_turns WHERE turn_id=? AND task_run_id=?", (turn_id, task_id)).fetchone()
    if row is None:
        return None
    try:
        intent = json.loads(row["intent_json"])
        intent["support_refs"] = tuple(intent["support_refs"])
        achievement = json.loads(row["achievement_json"]) if row["achievement_json"] else None
        if achievement:
            completion = achievement["completion"]
            completion["support_refs"] = tuple(completion["support_refs"])
            achievement = TurnAchievement(TurnCompletion(**completion), achievement["review_operation_id"])
        roads = []
        for road in store._connection.execute(
                "SELECT * FROM stage_roads WHERE turn_id=? ORDER BY ordinal", (turn_id,)):
            candidate = json.loads(road["candidate_json"])
            candidate["support_refs"] = tuple(candidate["support_refs"])
            roads.append(RoadSnapshot(road["road_id"], RoadCandidate(**candidate), IntentStatus(road["status"])))
        result = StageTurnSnapshot(turn_id, TurnIntent(**intent), TurnPlanningState(row["planning_state"]),
            row["parent_turn_id"], tuple(roads), tuple(json.loads(row["fact_refs_json"])), achievement)
        _shape(result)
        return result
    except (ValueError, TypeError, KeyError):
        _fail("stage.state_corrupt", Category.UNKNOWN)


def list_turns(store, task_id):
    require_run(store, task_id)
    return tuple(load_turn(store, task_id, row[0]) for row in store._connection.execute(
        "SELECT turn_id FROM stage_turns WHERE task_run_id=? ORDER BY rowid", (task_id,)))


def active_turn(turns):
    """Follow the persisted waiting chain; historical branches retain their state."""
    turns = tuple(t for t in turns if t.planning_state is not TurnPlanningState.SUPERSEDED)
    roots = [t for t in turns if t.parent_turn_id is None]
    if not roots:
        return None
    if len(roots) != 1:
        _fail("stage.root_scope_ambiguous", Category.UNKNOWN)
    current, seen = roots[0], set()
    while current.planning_state is TurnPlanningState.WAITING_CHILD:
        if current.turn_id in seen:
            _fail("stage.parent_cycle", Category.UNKNOWN)
        seen.add(current.turn_id)
        children = [t for t in turns if t.parent_turn_id == current.turn_id]
        if not children:
            _fail("stage.waiting_without_child", Category.UNKNOWN)
        current = children[-1]
    return current


def session_id(task_id, session):
    return str(uuid5(NAMESPACE_URL, f"qinyuan/session/{task_id}/{session.scope}/{session.owner_id}"))


def load_session(store, task_id, session):
    require_run(store, task_id)
    row = store._connection.execute(
        "SELECT session_id FROM stage_sessions WHERE task_run_id=? AND scope=? AND owner_id=?",
        (task_id, session.scope, session.owner_id)).fetchone()
    if row and row[0] != session_id(task_id, session):
        _fail("stage.session_identity_changed", Category.UNKNOWN)
    return row[0] if row else None


def list_reviews(store, task_id):
    """FIFO metadata only; proposal/result bytes stay in the existing ledger."""
    require_run(store, task_id)
    if store._schema_version < 36:
        return ()
    return tuple(StageQueuedReview(*row) for row in store._connection.execute(
        "SELECT q.proposal_operation_id,q.state,q.review_operation_id "
        "FROM stage_review_queue q JOIN d4_operations p ON p.operation_id=q.proposal_operation_id "
        "WHERE p.task_run_id=? ORDER BY q.rowid", (task_id,)))


def _queue_source(store, task_id, identity, *, allow_applied=False):
    config = require_run(store, task_id)
    operation = store.load_d4_operation(identity)
    if (operation is None or operation.start.task_run_id != task_id
            or operation.start.role != "inner_action" or operation.start.category not in {"planning", "action"}
            or operation.start.role_config_ref != config.inner_role.role_config_ref
            or operation.start.budget_policy_ref != config.budget.budget_policy_ref
            or operation.completion is None or operation.completion.result_kind != "completed"
            or operation.application_ref is not None and not allow_applied):
        _fail("stage.review_proposal_not_ready", Category.POLICY)
    evidence = store.stage_evidence.read_model_result(operation)
    if load_session(store, task_id, evidence.session) is None:
        _fail("stage.review_session_unavailable", Category.POLICY)
    value = evidence.result
    if not isinstance(value, (TurnIntent, tuple, PlanningExhausted, StagePlanningDecision,
            StageFactProposal, StageFactBatch, StageConclusionChange, TurnCompletion, RoadResolution)):
        _fail("stage.review_proposal_invalid", Category.INPUT)
    if isinstance(value, StagePlanningDecision) and value.kind in {"wait_for_tools", "wait_for_review"}:
        _fail("stage.review_proposal_invalid", Category.INPUT)
    return operation, evidence


def _review_target_current(store, task_id, identity):
    """Checkpoint movement is allowed; changing the actual target is not."""
    request = store.stage_evidence.request(identity)
    original = request.context.turn
    if original is None:
        return
    current = load_turn(store, task_id, original.turn_id)
    if current is None or current.intent != original.intent or current.parent_turn_id != original.parent_turn_id:
        _fail("stage.review_target_changed", Category.CONFLICT)
    if request.context.session.scope == "road_execution":
        road_id = request.context.session.owner_id
        before = next((r for r in original.roads if r.road_id == road_id), None)
        after = next((r for r in current.roads if r.road_id == road_id), None)
        if before is None or after is None or before.candidate != after.candidate:
            _fail("stage.review_target_changed", Category.CONFLICT)


def _queue_review_matches(store, task_id, source, review, proposal_id=None):
    config = require_run(store, task_id)
    request = store.stage_evidence.request(review.start.operation_id)
    category = "fact_review" if isinstance(source.result,
        (StageFactProposal, StageFactBatch, StageConclusionChange)) else "review"
    if (review.start.task_run_id != task_id or review.start.role != "outer_planning"
            or review.start.role_config_ref != config.outer_role.role_config_ref
            or review.start.budget_policy_ref != config.budget.budget_policy_ref
            or review.start.category != category or request.stage != "stage_review"
            or request.context.session != source.session
            or request.proposal_operation_id is None
            or request.proposal is None
            ):
        _fail("stage.review_binding_changed", Category.UNKNOWN)
    sources = dict(store.stage_evidence.review_sources(request))
    original = sources.get(proposal_id or request.proposal_operation_id)
    if original is None or original != source:
        _fail("stage.review_binding_changed", Category.UNKNOWN)
    return request


def _queued_known_operations(store, task_id, entries):
    """Only complete authenticated results qualify, never an in-flight review."""
    pending = {entry.proposal_operation_id for entry in entries if entry.state in {"pending", "blocked"}}
    if not pending:
        return set()
    sources = {identity: _queue_source(store, task_id, identity)[1] for identity in pending}
    known = set(pending)
    for operation in store.list_d4_operations(task_id):
        if (operation.application_ref is not None or operation.completion is None
                or operation.completion.result_kind != "completed" or operation.start.role != "outer_planning"):
            continue
        saved = store.stage_evidence.read("request", operation.start.operation_id)
        if saved is None or saved["request"].get("proposal_operation_id") not in pending:
            continue
        identity = saved["request"]["proposal_operation_id"]
        _queue_review_matches(store, task_id, sources[identity], operation, identity)
        store.stage_evidence.read_model_result(operation)
        known.add(operation.start.operation_id)
    return known


def _validate_queue(store, mutation, change):
    from agent_core.stage_planning import MAX_STAGE_CORRECTIONS
    entries = {entry.proposal_operation_id: entry for entry in list_reviews(store, mutation.task_run_id)}
    if any(not isinstance(binding, StageReviewBinding) for binding in change.blocked_reviews):
        _fail("stage.review_binding_invalid", Category.INPUT)
    if len(set(change.review_submissions)) != len(change.review_submissions):
        _fail("stage.review_submission_duplicate", Category.INPUT)
    if len({b.proposal_operation_id for b in change.blocked_reviews}) != len(change.blocked_reviews):
        _fail("stage.review_failure_duplicate", Category.INPUT)
    for identity in change.review_submissions:
        _uuid(identity, "proposal_operation_id")
        if identity in entries and entries[identity].state != "pending":
            _fail("stage.review_already_closed", Category.CONFLICT)
        operation, _ = _queue_source(store, mutation.task_run_id, identity)
        if identity not in entries and operation.start.base_checkpoint_sequence != mutation.base_checkpoint_sequence:
            _fail("stage.model_context_stale", Category.CONFLICT)
        _review_target_current(store, mutation.task_run_id, identity)
    for binding in change.blocked_reviews:
        entry = entries.get(binding.proposal_operation_id)
        if (entry is None or entry.state != "pending"
                or binding.proposal_operation_id in change.review_submissions
                or any(other.proposal_operation_id == binding.proposal_operation_id for other in change.reviews)):
            _fail("stage.review_failure_not_pending", Category.CONFLICT)
        _, source = _queue_source(store, mutation.task_run_id, binding.proposal_operation_id)
        review = store.load_d4_operation(binding.review_operation_id)
        if review is None or review.completion is None or review.completion.result_kind != "failed" or review.application_ref is not None:
            _fail("stage.review_failure_unconfirmed", Category.UNKNOWN)
        request = _queue_review_matches(store, mutation.task_run_id, source, review, binding.proposal_operation_id)
        from agent_core.stage_planning import review_members
        if (not set(review_members(request)).issubset({b.proposal_operation_id for b in change.blocked_reviews})
                or store.stage_evidence.correction_depth(request) != MAX_STAGE_CORRECTIONS
                or store.stage_evidence.transport_failure(request,
                    store.stage_evidence.read("result", binding.review_operation_id)) is None):
            _fail("stage.review_failure_unconfirmed", Category.UNKNOWN)


def review_groups(store, task_id):
    """Suggest contiguous Fact batches, without merging identities or verdicts."""
    groups, last_key = [], None
    for entry in list_reviews(store, task_id):
        if entry.state != "pending":
            continue
        _, source = _queue_source(store, task_id, entry.proposal_operation_id)
        _review_target_current(store, task_id, entry.proposal_operation_id)
        request = store.stage_evidence.request(entry.proposal_operation_id)
        turn = request.context.turn
        road = next((r for r in turn.roads if r.road_id == source.session.owner_id), None) if turn else None
        key = ((source.session, turn.turn_id, proposal_digest(turn.intent), proposal_digest(road.candidate))
               if fact_candidates(source.result) and turn and road else None)
        if key is not None and key == last_key:
            groups[-1].append(entry.proposal_operation_id)
        else:
            groups.append([entry.proposal_operation_id])
        last_key = key
    return tuple(tuple(group) for group in groups)


def duplicate_fact(store, task_id, session, value):
    for entry in list_reviews(store, task_id):
        if entry.state not in {"pending", "blocked"}:
            continue
        original = store.stage_evidence.read_model_result(store.load_d4_operation(entry.proposal_operation_id))
        if original.session == session and proposal_digest(original.result) == proposal_digest(value):
            return entry
    return None


def branch_id(task_id, fork, target_session=None):
    from agent_core.turn_modeling import stable_id
    target = target_session or getattr(fork, "target_session", None)
    suffix = "/" + target.scope + "/" + target.owner_id if target else ""
    return stable_id(task_id, "stage/branch/" + fork.proposal_operation_id + "/" + (fork.review_operation_id or "continue") + suffix)


def load_branch(store, task_id, session):
    if store._schema_version < 37:
        return None
    identity = load_session(store, task_id, session)
    row = store._connection.execute("SELECT branch_id,proposal_operation_id,review_operation_id "
        "FROM stage_branches WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (identity,)).fetchone()
    if row is None:
        return None
    value = StageSessionBranch(*row)
    source = store.stage_evidence.request(value.proposal_operation_id)
    target = session if session != source.context.session else None
    if value.branch_id != branch_id(task_id, value, target):
        _fail("stage.branch_binding_changed", Category.UNKNOWN)
    return value


def branch_prefix(store, branch):
    from agent_core.stage_planning import conversation_operation_ids
    request = store.stage_evidence.request(branch.proposal_operation_id)
    return (*conversation_operation_ids(request), branch.proposal_operation_id,
            *((branch.review_operation_id,) if branch.review_operation_id else ()))


def review_rejections(store, review_id):
    return store.stage_evidence.review_rejections(review_id)


def _validate_branches(store, mutation, change):
    from agent_core.stage_planning import stage_runtime, review_members, conversation_operation_ids
    if not change.branches:
        return {}
    discarded = {}
    heads = set()
    reviewed = {identity for binding in change.reviews
        for identity in review_members(store.stage_evidence.request(binding.review_operation_id))}
    for fork in change.branches:
        if not isinstance(fork, StageBranchFork):
            _fail("stage.branch_invalid", Category.INPUT)
        operation, source = _queue_source(store, mutation.task_run_id, fork.proposal_operation_id, allow_applied=True)
        if fork.target_session is not None:
            if not isinstance(fork.target_session, PlanningSession) or fork.target_session.scope not in {"turn_planning", "road_execution"}:
                _fail("stage.branch_target_invalid", Category.INPUT)
            _uuid(fork.target_session.owner_id, "session_owner_id")
        session = fork.target_session or source.session
        if session in heads:
            _fail("stage.multiple_branch_heads", Category.INPUT)
        heads.add(session)
        request = store.stage_evidence.request(fork.proposal_operation_id)
        current = load_branch(store, mutation.task_run_id, session)
        identity = branch_id(mutation.task_run_id, fork)
        if store._connection.execute("SELECT 1 FROM stage_branches WHERE branch_id=?", (identity,)).fetchone():
            _fail("stage.branch_already_registered", Category.CONFLICT)
        queued = next((entry for entry in list_reviews(store, mutation.task_run_id)
            if entry.proposal_operation_id == fork.proposal_operation_id), None)
        if fork.target_session is not None:
            # Seed one Road with its author's immutable planning prefix. The
            # destination has its own future transcript and execution context.
            road = next((road for turn in change.turns for road in turn.roads
                if road.road_id == session.owner_id), None)
            accepted_source = next((binding for binding in change.reviews
                if binding.proposal_operation_id == fork.proposal_operation_id), None)
            review = (store.stage_evidence.read_model_result(store.load_d4_operation(accepted_source.review_operation_id)).result
                if accepted_source else None)
            pending = fork.proposal_operation_id in change.planning_submissions
            road_seed = (session.scope == "road_execution" and road is not None
                and isinstance(source.result, tuple) and road.candidate in source.result
                and (pending or isinstance(review, TurnPlanningReview) and review.decision == "accept"
                    and source.result.index(road.candidate) in review.accepted_road_indices)
                and not store._connection.execute("SELECT 1 FROM stage_roads WHERE road_id=?", (road.road_id,)).fetchone()
                and any(turn.turn_id == source.session.owner_id and road in turn.roads for turn in change.turns))
            turn_seed = (session.scope == "turn_planning" and isinstance(source.result, TurnIntent) and pending
                and any(turn.turn_id == session.owner_id and turn.intent == source.result for turn in change.turns)
                and load_turn(store, mutation.task_run_id, session.owner_id) is None)
            if (source.session.scope != "turn_planning" or fork.review_operation_id is not None
                    or current is not None or not (road_seed or turn_seed)):
                _fail("stage.branch_inheritance_unbound", Category.POLICY)
        elif fork.review_operation_id is None:
            if (operation.application_ref is not None or fork.proposal_operation_id not in change.review_submissions
                    or stage_runtime(request).get("branch_id") != (current.branch_id if current else None)):
                _fail("stage.branch_author_changed", Category.CONFLICT)
        else:
            if (queued is None or fork.proposal_operation_id not in review_rejections(store, fork.review_operation_id)
                    or not (queued.review_operation_id == fork.review_operation_id and queued.state == "settled"
                        or any(binding.review_operation_id == fork.review_operation_id for binding in change.reviews))
                    or current is None or fork.proposal_operation_id not in branch_prefix(store, current)):
                _fail("stage.branch_correction_unbound", Category.UNKNOWN)
            for candidate in store.list_d4_operations(mutation.task_run_id):
                if (candidate.start.role != "inner_action" or candidate.application_ref is not None
                        or candidate.completion is None or candidate.completion.result_kind != "completed"
                        or candidate.start.operation_id in reviewed):
                    continue
                original = store.stage_evidence.request(candidate.start.operation_id)
                if fork.proposal_operation_id not in conversation_operation_ids(original):
                    continue
                # Already executed actions must retain their actual outcomes;
                # only a never-dispatched decision can be discarded here.
                if any(child.start.category == "tool_dispatch" and candidate.start.operation_id in child.start.resource_ref.split("/")
                        for child in store.list_d4_operations(mutation.task_run_id)):
                    continue
                store.stage_evidence.read_model_result(candidate)
                discarded[candidate.start.operation_id] = fork.review_operation_id
        author_branch = load_branch(store, mutation.task_run_id, source.session) if fork.target_session else current
        inherited = set(branch_prefix(store, author_branch)) if author_branch else set()
        for prefix_id in branch_prefix(store, fork):
            prefix = store.load_d4_operation(prefix_id)
            if prefix is None or prefix.start.task_run_id != mutation.task_run_id:
                _fail("stage.branch_prefix_changed", Category.UNKNOWN)
            if (store.stage_evidence.read_model_result(prefix).session != source.session
                    and prefix_id not in inherited):
                _fail("stage.branch_prefix_changed", Category.UNKNOWN)
    return discarded


def _shape(turn):
    if not isinstance(turn, StageTurnSnapshot) or not isinstance(turn.intent, TurnIntent):
        _fail("stage.turn_invalid", Category.INPUT)
    _uuid(turn.turn_id, "turn_id")
    if turn.parent_turn_id is not None:
        _uuid(turn.parent_turn_id, "parent_turn_id")
    _text(turn.intent.description, "description")
    _text(turn.intent.completion_criterion, "completion_criterion")
    _refs(turn.intent.support_refs)
    _refs(turn.fact_refs)
    if not isinstance(turn.planning_state, TurnPlanningState) or not isinstance(turn.roads, tuple):
        _fail("stage.turn_invalid", Category.INPUT)
    for road in turn.roads:
        if (not isinstance(road, RoadSnapshot) or not isinstance(road.candidate, RoadCandidate)
                or not isinstance(road.status, IntentStatus)):
            _fail("stage.road_invalid", Category.INPUT)
        _uuid(road.road_id, "road_id")
        _text(road.candidate.description, "road_description")
        _refs(road.candidate.support_refs)
    if len({r.road_id for r in turn.roads}) != len(turn.roads):
        _fail("stage.duplicate_road", Category.INPUT)
    if turn.achievement is not None:
        if (not isinstance(turn.achievement, TurnAchievement)
                or not isinstance(turn.achievement.completion, TurnCompletion)):
            _fail("stage.achievement_invalid", Category.INPUT)
        _text(turn.achievement.completion.reason, "achievement_reason")
        _uuid(turn.achievement.review_operation_id, "review_operation_id")
        _refs(turn.achievement.completion.support_refs)
        if not turn.achievement.completion.support_refs:
            _fail("stage.achievement_support_required", Category.INPUT)


def _reviewed(store, mutation, change, discarded=()):
    config = require_run(store, mutation.task_run_id)
    queue = {entry.proposal_operation_id: entry for entry in list_reviews(store, mutation.task_run_id)}
    accepted = []
    used = set()
    for binding in change.reviews:
        queued = queue.get(binding.proposal_operation_id)
        if queued is not None and queued.state != "pending":
            _fail("stage.review_already_closed", Category.CONFLICT)
        if binding.proposal_operation_id == binding.review_operation_id:
            _fail("stage.review_binding_invalid", Category.INPUT)
        pair = []
        for op_id, role in ((binding.proposal_operation_id, "inner_action"),
                            (binding.review_operation_id, "outer_planning")):
            if op_id in used:
                _fail("stage.operation_reused")
            used.add(op_id)
            record = store.load_d4_operation(op_id)
            if (record is None or record.start.task_run_id != mutation.task_run_id
                    or record.start.role != role or record.completion is None
                    or record.completion.result_kind != "completed" or record.application_ref is not None):
                _fail("stage.model_operation_not_ready")
            if (record.start.base_checkpoint_sequence > mutation.base_checkpoint_sequence
                    or queued is None and record.start.base_checkpoint_sequence != mutation.base_checkpoint_sequence):
                _fail("stage.model_context_stale")
            expected = config.inner_role if role == "inner_action" else config.outer_role
            if (record.start.role_config_ref != expected.role_config_ref
                    or record.start.budget_policy_ref != config.budget.budget_policy_ref):
                _fail("stage.model_configuration_changed", Category.POLICY)
            if store.stage_evidence is None:
                _fail("stage.evidence_unconfigured", Category.DEPENDENCY)
            evidence = store.stage_evidence.read_model_result(record)
            if (not isinstance(evidence, StageModelEvidence)
                    or evidence.context_sha256 != record.start.context_sha256
                    or evidence.evidence_sha256 != record.completion.evidence_sha256
                    or load_session(store, mutation.task_run_id, evidence.session) is None):
                _fail("stage.model_evidence_changed", Category.UNKNOWN)
            pair.append((record, evidence))
        (proposal_record, proposal), (review_record, review) = pair
        if queued is not None:
            _review_target_current(store, mutation.task_run_id, binding.proposal_operation_id)
            request = _queue_review_matches(store, mutation.task_run_id, proposal, review_record, binding.proposal_operation_id)
            members = store.stage_evidence.review_sources(request)
            for identity, _ in members[1:]:
                if identity in used or identity not in queue or queue[identity].state != "pending":
                    _fail("stage.review_members_changed", Category.UNKNOWN)
                member, _ = _queue_source(store, mutation.task_run_id, identity)
                if member.start.base_checkpoint_sequence > mutation.base_checkpoint_sequence:
                    _fail("stage.model_context_stale")
                _review_target_current(store, mutation.task_run_id, identity)
                used.add(identity)
            proposal = replace(proposal, result=request.proposal)
            if review_record.start.base_checkpoint_sequence < proposal_record.start.base_checkpoint_sequence:
                _fail("stage.model_context_stale")
        changing = isinstance(proposal.result, StageConclusionChange)
        batching = isinstance(proposal.result, StageFactBatch)
        category = "fact_review" if isinstance(proposal.result, (StageFactProposal, StageFactBatch, StageConclusionChange)) else "review"
        if (review_record.start.category != category or not isinstance(review.result,
                D8ChangeDecision if changing else StageFactBatchReview if batching else TurnPlanningReview)
                or proposal.session != review.session
                or review.proposal_operation_id != proposal_record.start.operation_id
                or review.proposal_sha256 != proposal_digest(proposal.result)):
            _fail("stage.review_binding_changed", Category.UNKNOWN)
        decision = review.result
        if decision.decision not in ({"accept", "reject", "insufficient"} if changing else {"accept", "revise", "reject"}):
            _fail("stage.review_invalid", Category.INPUT)
        _text(decision.reason, "review_reason")
        _sources(store, mutation.task_run_id, getattr(decision, "support_refs", ()))
        if isinstance(proposal.result, tuple):
            if not proposal.result or any(not isinstance(r, RoadCandidate) for r in proposal.result):
                _fail("stage.roads_proposal_invalid", Category.INPUT)
            indices = decision.accepted_road_indices
            if decision.decision == "accept" and (not indices or len(set(indices)) != len(indices)
                    or any(type(i) is not int or not 0 <= i < len(proposal.result) for i in indices)):
                _fail("stage.road_selection_invalid", Category.INPUT)
        elif getattr(decision, "accepted_road_indices", ()):
            _fail("stage.road_selection_invalid", Category.INPUT)
        if decision.decision != "accept" and getattr(decision, "accepted_road_indices", ()):
            _fail("stage.road_selection_invalid", Category.INPUT)
        value = proposal.result
        if isinstance(value, (StageFactSelection, StageHistoryRead)):
            _fail("stage.pure_reference_review_forbidden", Category.POLICY)
        claims = fact_candidates(value)
        expected_scope = "road_execution" if isinstance(value, RoadResolution) or claims else "turn_planning"
        if isinstance(value, (TurnCompletion, StageConclusionChange)) or claims and all(c.inferred for _, c in claims):
            expected_scope = proposal.session.scope
        if isinstance(value, StagePlanningDecision) and value.kind == "return_to_parent":
            expected_scope = proposal.session.scope
        if proposal.session.scope != expected_scope:
            _fail("stage.planning_scope_changed", Category.POLICY)
        if isinstance(value, (TurnIntent, PlanningExhausted, tuple)) and proposal_record.start.category != "planning":
            _fail("stage.planning_category_invalid", Category.POLICY)
        if isinstance(value, tuple):
            refs = tuple(dict.fromkeys(ref for candidate in value for ref in candidate.support_refs))
        else:
            refs = value.evidence_refs if changing else value.support_refs
            _text(value.description if isinstance(value, TurnIntent) else value.reason, "proposal_text")
        if batching and (not claims or len(decision.decisions) != len(claims)):
            _fail("stage.fact_batch_binding_invalid", Category.INPUT)
        for index, claim in claims:
            _, claim_review = reviewed_fact(value, decision, index)
            if claim_review.decision not in {"accept", "revise", "reject"}:
                _fail("stage.fact_batch_binding_invalid", Category.INPUT)
            _text(claim_review.reason, "review_reason")
            if not claim.support_refs or len(claim.content) > 256 or type(claim.inferred) is not bool:
                _fail("stage.fact_proposal_invalid", Category.INPUT)
            _text(claim.content, "fact_content")
            sources = read_history(store, mutation.task_run_id, claim.support_refs)
            observations = [i for i in sources if i.record_type == "progress"]
            if not claim.inferred and (not observations or any(i.record_type == "reviewed_inference" for i in sources)):
                _fail("stage.formal_fact_requires_own_observation", Category.POLICY)
            owner_turn_id = next((t.turn_id for t in list_turns(store, mutation.task_run_id)
                if any(r.road_id == proposal.session.owner_id for r in t.roads)), None)
            for item in observations:
                operation = (store.load_d4_operation(item.record_id)
                             if not item.record_id.startswith("revision/") else None)
                if (operation is None or operation.start.category != "tool_dispatch" or item.intent_id != proposal.session.owner_id):
                    fact_scope_failure(item, proposal.session,
                        expected_turn_id=owner_turn_id,
                        fact_id=next((a.fact_id for a in change.facts if a.review_operation_id == binding.review_operation_id), None),
                        proposal_id=binding.proposal_operation_id, review_id=binding.review_operation_id,
                        reason="source_not_tool_dispatch" if operation is None or operation.start.category != "tool_dispatch"
                            else "observation_owner_mismatch")
        feedback = isinstance(value, PlanningExhausted) or isinstance(value, StagePlanningDecision) and value.kind == "return_to_parent"
        if feedback:
            read_history(store, mutation.task_run_id, refs)
        elif not changing or decision.decision == "accept":
            _sources(store, mutation.task_run_id, refs)
        if decision.decision == "accept":
            accepted.append((binding, proposal.session, value, decision))
    known_queue = _queued_known_operations(store, mutation.task_run_id, tuple(queue.values()))
    pending = {r.start.operation_id for r in store.list_d4_operations(mutation.task_run_id)
               if (r.completion is None or r.completion.result_kind == "completed" and r.application_ref is None)
               and not (r.start.category == "tool_dispatch"
                        and getattr(store, "live_tool_operation", lambda identity: False)(r.start.operation_id))
               and not getattr(store, "live_stage_review_operation", lambda identity: False)(r.start.operation_id)
               } - used - set(change.applications) - known_queue - set(change.review_submissions) - set(discarded)
    if pending:
        _fail("stage.pending_operations", Category.UNKNOWN)
    return accepted


def validate(store, mutation):
    try:
        return _validate(store, mutation)
    except (ValueError, TypeError):
        _fail("stage.input_invalid", Category.INPUT)


def _validate(store, mutation):
    changes = [c for c in mutation.state_changes if isinstance(c, StageStateChange)]
    if not changes:
        if mutation.operation == "apply_stage_state":
            _fail("stage.change_required", Category.INPUT)
        if load_config(store, mutation.task_run_id) is not None:
            if mutation.d1_config or mutation.d2_config or mutation.d3:
                _fail("stage.legacy_metadata_rejected", Category.POLICY)
            if mutation.d7 is not None:
                return None  # The original D7 validator delegates to validate_goal_mutation below.
            if any(not isinstance(c, UpsertTaskRun) for c in mutation.state_changes):
                _fail("stage.legacy_domain_write_rejected", Category.POLICY)
            for after in mutation.state_changes:
                if after.status is TaskRunStatus.ACHIEVED:
                    _fail("stage.final_validation_not_connected", Category.POLICY)
                if after.status is TaskRunStatus.ASSUMED_ACHIEVED:
                    task = store.load_task_run(mutation.task_run_id)
                    config = require_run(store, mutation.task_run_id)
                    now = mutation.event.timestamp
                    if (mutation.operation != "finish_task_run" or config.goal_validation != "human"
                            or human_confirmation_policy(store, mutation.task_run_id) != "assume_after_300s_v1"
                            or task.status is not TaskRunStatus.AWAITING_CONFIRMATION
                            or task.confirmation_deadline is None or not task.confirmation_deadline <= now < task.deadline_at
                            or after.failure_reason != "human_confirmation_timeout_assumed"
                            or after != replace(after, request=task.request, effective_model_backend=task.effective_model_backend,
                                started_at=task.started_at, deadline_at=task.deadline_at, sandbox_id=task.sandbox_id,
                                confirmation_deadline=task.confirmation_deadline, finished_at=now,
                                source_root=task.source_root, target_network=task.target_network)):
                        _fail("stage.human_timeout_not_ready", Category.POLICY)
                    candidates = store.list_d7_answer_candidates(mutation.task_run_id)
                    if not candidates or live_tool_requests(store, mutation.task_run_id) or store.blocking_d4_operations(mutation.task_run_id):
                        _fail("stage.human_timeout_not_ready", Category.POLICY)
                    goal_candidate_proof(store, candidates[-1])
        return None
    if len(changes) != 1:
        _fail("stage.change_invalid", Category.INPUT)
    change = changes[0]
    if not all(isinstance(value, tuple) for value in (change.turns, change.sessions, change.reviews,
            change.facts, change.applications, change.revisions, change.review_submissions, change.blocked_reviews, change.branches,
            change.planning_submissions, change.discarded_decisions)):
        _fail("stage.change_invalid", Category.INPUT)
    for session in change.sessions:
        if not isinstance(session, PlanningSession) or session.scope not in {"turn_planning", "road_execution"}:
            _fail("stage.session_scope_invalid", Category.INPUT)
        _uuid(session.owner_id, "session_owner_id")
    for binding in (*change.reviews, *change.discarded_decisions):
        if not isinstance(binding, StageReviewBinding):
            _fail("stage.review_binding_invalid", Category.INPUT)
        _uuid(binding.proposal_operation_id, "proposal_operation_id")
        _uuid(binding.review_operation_id, "review_operation_id")
    for turn in change.turns:
        _shape(turn)
    _refs(change.applications)
    for identity in change.applications:
        _uuid(identity, "application_operation_id")
    if change.config is not None:
        config = change.config
        if (mutation.operation != "start_task_run" or store.load_task_run(mutation.task_run_id) is not None
                or len(mutation.state_changes) != 2 or not any(isinstance(c, UpsertTaskRun) for c in mutation.state_changes)
                or mutation.d1_config or mutation.d2_config or mutation.d3 or mutation.d7
                or change.turns or change.reviews or change.facts or change.applications or change.revisions
                or change.review_submissions or change.blocked_reviews or change.branches or change.planning_submissions
                or change.discarded_decisions or not isinstance(config, StageRunConfig)
                or any(c.status is not TaskRunStatus.RUNNING for c in mutation.state_changes if isinstance(c, UpsertTaskRun))
                or config.outer_role.role != "outer_planning" or config.inner_role.role != "inner_action"
                or not isinstance(config.budget, D8HandoffBudgetPolicy)):
            _fail("stage.start_invalid", Category.INPUT)
        if config.execution_policy_ref is not None:
            if not config.execution_policy_ref.startswith("sha256:"):
                _fail("stage.execution_policy_invalid", Category.INPUT)
            _sha(config.execution_policy_ref[7:], "execution_policy_ref")
        if config.goal_validation not in {None, "human", "external"}:
            _fail("stage.goal_validation_invalid", Category.INPUT)
        return change
    if mutation.d1_config or mutation.d2_config or mutation.d3 or mutation.d7:
        _fail("stage.legacy_metadata_rejected", Category.POLICY)
    if mutation.operation != "apply_stage_state" or len(mutation.state_changes) != 1:
        _fail("stage.dedicated_operation_required", Category.INPUT)
    config = require_run(store, mutation.task_run_id)
    task = store.load_task_run(mutation.task_run_id)
    if (task.status is not TaskRunStatus.RUNNING or store._now() >= task.deadline_at
            or task.request != mutation.request or task.effective_model_backend != mutation.effective_model_backend):
        _fail("stage.task_not_active", Category.POLICY)
    _validate_queue(store, mutation, change)
    from agent_core import stage_plans
    stage_plans.submissions(store, mutation.task_run_id, change)
    discarded = _validate_branches(store, mutation, change)
    from agent_core.stage_planning import conversation_operation_ids
    for binding in change.discarded_decisions:
        operation = store.load_d4_operation(binding.proposal_operation_id)
        if (operation is None or operation.start.task_run_id != mutation.task_run_id or operation.start.role != "inner_action"
                or operation.application_ref is not None or operation.completion is None
                or operation.completion.result_kind != "completed"
                or not set(store.stage_evidence.review_interruptions(binding.review_operation_id)).intersection(
                    conversation_operation_ids(store.stage_evidence.request(binding.proposal_operation_id)))
                or any(child.start.category == "tool_dispatch" and binding.proposal_operation_id in child.start.resource_ref.split("/")
                    for child in store.list_d4_operations(mutation.task_run_id))):
            _fail("stage.discarded_decision_unbound", Category.POLICY)
        store.stage_evidence.read_model_result(operation)
        discarded[binding.proposal_operation_id] = binding.review_operation_id
    accepted = _reviewed(store, mutation, change, discarded)
    if (change.turns or change.reviews or change.facts or change.revisions) and live_tool_requests(store, mutation.task_run_id):
        if (change.turns or change.revisions or not change.reviews
                or any(not (fact_candidates(store.stage_evidence.request(binding.review_operation_id).proposal)
                        or isinstance(store.stage_evidence.request(binding.review_operation_id).proposal, (TurnIntent, tuple)))
                    or not store._connection.execute("SELECT 1 FROM stage_review_queue WHERE proposal_operation_id=?",
                        (binding.proposal_operation_id,)).fetchone() for binding in change.reviews)):
            _fail("stage.tools_still_running", Category.POLICY)
    if any(fork.review_operation_id for fork in change.branches) and live_tool_requests(store, mutation.task_run_id):
        _fail("stage.branch_tools_unsettled", Category.POLICY)
    old = {t.turn_id: t for t in list_turns(store, mutation.task_run_id)}
    current = active_turn(tuple(old.values()))
    projected = dict(old)
    if len({t.turn_id for t in change.turns}) != len(change.turns):
        _fail("stage.duplicate_turn", Category.INPUT)
    for turn in change.turns:
        owner = store._connection.execute("SELECT task_run_id FROM stage_turns WHERE turn_id=?", (turn.turn_id,)).fetchone()
        if owner and owner[0] != mutation.task_run_id:
            _fail("stage.cross_task_turn", Category.POLICY)
        prior = old.get(turn.turn_id)
        if prior and (turn.intent != prior.intent or turn.parent_turn_id != prior.parent_turn_id
                      or prior.achievement is not None and turn.achievement != prior.achievement):
            _fail("stage.immutable_turn_changed")
        projected[turn.turn_id] = turn
    consumed = set()
    plan_members = {row[1] for row in stage_plans.bindings(store, mutation.task_run_id)}
    plan_members.update(binding.proposal_operation_id for binding, _, value, _ in accepted
        if isinstance(value, (TurnIntent, tuple)) and stage_plans.admitted(store, binding.proposal_operation_id))
    consumed.update(binding.review_operation_id for binding, _, value, _ in accepted
        if binding.proposal_operation_id in plan_members and isinstance(value, (TurnIntent, tuple)))
    if sum(t.turn_id not in old for t in change.turns) > 1:
        _fail("stage.multiple_successors", Category.POLICY)
    for binding, scope, value, _ in accepted:
        if isinstance(value, (TurnIntent, tuple, PlanningExhausted, StagePlanningDecision)) and current is not None and binding.proposal_operation_id not in plan_members:
            if scope.owner_id != current.turn_id and not (isinstance(value, StagePlanningDecision)
                    and value.kind == "return_to_parent" and any(r.road_id == scope.owner_id for r in current.roads)):
                _fail("stage.planning_frontier_changed", Category.POLICY)
        if not isinstance(value, StagePlanningDecision):
            continue
        if value.kind not in {"select_road", "return_to_parent", "request_final_validation"} or current is None:
            _fail("stage.planning_decision_invalid", Category.INPUT)
        if value.kind != "select_road" and value.road_id is not None:
            _fail("stage.unexpected_selected_road", Category.INPUT)
        updated = projected[current.turn_id]
        if value.kind == "select_road":
            if (current.achievement is None or not any(r.road_id == value.road_id and r.status is IntentStatus.PENDING for r in current.roads)
                    or not any(r.road_id == value.road_id and r.status is IntentStatus.RUNNING for r in updated.roads)):
                _fail("stage.road_selection_not_applied")
        elif value.kind == "request_final_validation":
            if current.achievement is None or not value.support_refs or any(r.status is IntentStatus.RUNNING for r in current.roads):
                _fail("stage.final_handoff_not_ready", Category.POLICY)
            _sources(store, mutation.task_run_id, value.support_refs, formal=True)
            if config.goal_validation is not None:
                if not value.candidate or not value.source_ref:
                    _fail("stage.candidate_required", Category.INPUT)
                sources = read_history(store, mutation.task_run_id, (value.source_ref,))
                if sources[0].identity != "tool_observation_not_fact":
                    _fail("stage.candidate_requires_observation", Category.POLICY)
                _sources(store, mutation.task_run_id, (value.source_ref,))
        else:
            if not value.support_refs or any(r.status is IntentStatus.RUNNING for r in updated.roads):
                _fail("stage.feedback_not_settled", Category.POLICY)
            parent = projected.get(current.parent_turn_id)
            if parent is not None and parent.planning_state is not TurnPlanningState.ACTIVE:
                _fail("stage.feedback_parent_not_resumed")
        consumed.add(binding.review_operation_id)
    if any(not isinstance(a, StageFactApplication) for a in change.facts):
        _fail("stage.fact_application_invalid", Category.INPUT)
    if len({(a.review_operation_id, a.candidate_index) for a in change.facts}) != len(change.facts):
        _fail("stage.duplicate_fact_review", Category.INPUT)
    accepted_claims = [(b, scope, claim, index) for b, scope, value, decision in accepted
        for index, claim in fact_candidates(value) if reviewed_fact(value, decision, index)[1].decision == "accept"]
    if not {(b.review_operation_id, index) for b, _, claim, index in accepted_claims if not claim.inferred}.issubset({
            (a.review_operation_id, a.candidate_index) for a in change.facts}):
        _fail("stage.accepted_fact_not_applied", Category.INPUT)
    consumed.update(b.review_operation_id for b, scope, value, _ in accepted
        if isinstance(value, PlanningExhausted) and scope.owner_id not in projected)
    selections = {}
    route_changes = set()
    if change.applications or change.revisions:
        from agent_core.stage_knowledge import StageKnowledge
        knowledge = StageKnowledge(store.stage_evidence, mutation.task_run_id, store=store)
        for identity in change.applications:
            operation = store.load_d4_operation(identity)
            if (operation is None or operation.start.task_run_id != mutation.task_run_id
                    or operation.start.role != "inner_action" or operation.start.base_checkpoint_sequence != mutation.base_checkpoint_sequence
                    or operation.start.role_config_ref != config.inner_role.role_config_ref
                    or operation.start.budget_policy_ref != config.budget.budget_policy_ref
                    or operation.completion is None or operation.completion.result_kind != "completed" or operation.application_ref is not None):
                _fail("stage.reference_operation_not_ready", Category.POLICY)
            source = store.stage_evidence.read_model_result(operation)
            owner = knowledge.turn_for(source.session)
            if isinstance(source.result, StageFactSelection):
                _sources(store, mutation.task_run_id, source.result.support_refs, formal=True)
                selections[owner.turn_id] = source.result.support_refs
                if projected[owner.turn_id].fact_refs != source.result.support_refs:
                    _fail("stage.fact_selection_not_applied")
            elif isinstance(source.result, StageHistoryRead):
                store.stage_evidence.read_history_result(operation)
            elif isinstance(source.result, StagePlanningDecision) and source.result.kind == "wait_for_tools":
                request = store.stage_evidence.request(identity)
                if source.session.scope != "road_execution" or not request.context.pending_operation_ids:
                    _fail("stage.wait_without_tools", Category.POLICY)
            elif isinstance(source.result, StagePlanningDecision) and source.result.kind == "wait_for_review":
                from agent_core.stage_planning import stage_runtime
                request = store.stage_evidence.request(identity)
                pending = [entry["proposal_operation_id"] for entry in stage_runtime(request).get("pending_reviews", ())
                    if entry["state"] == "pending"]
                owned = {entry.proposal_operation_id for entry in list_reviews(store, mutation.task_run_id)}
                if not pending or not set(pending).issubset(owned):
                    _fail("stage.wait_without_review", Category.POLICY)
            elif isinstance(source.result, tuple) or isinstance(source.result, StagePlanningDecision) and source.result.kind == "select_road":
                from agent_core.stage_planning import stage_runtime
                request = store.stage_evidence.request(identity)
                advice = stage_runtime(request).get("route_advice")
                if source.session.scope != "turn_planning" or not advice or advice != stage_plans.route_advice(store, mutation.task_run_id):
                    _fail("stage.route_decision_unbound", Category.POLICY)
                _sources(store, mutation.task_run_id, tuple(ref for candidate in source.result for ref in candidate.support_refs)
                    if isinstance(source.result, tuple) else source.result.support_refs)
                before = [road for road in owner.roads if road.status in {IntentStatus.PENDING, IntentStatus.RUNNING, IntentStatus.SUSPENDED}]
                after = [road for road in projected[owner.turn_id].roads if road.road_id in {road.road_id for road in before}]
                if isinstance(source.result, tuple) and (len(source.result) != len(before)
                        or set(source.result) != {road.candidate for road in before}
                        or source.result != tuple(road.candidate for road in after)):
                    _fail("stage.route_order_changed_candidates", Category.POLICY)
                chosen = after[0] if isinstance(source.result, tuple) else next((road for road in projected[owner.turn_id].roads
                    if road.road_id == source.result.road_id and road.road_id in {road.road_id for road in before}), None)
                if chosen is None or chosen.status is not IntentStatus.RUNNING:
                    _fail("stage.route_decision_not_applied", Category.POLICY)
                route_changes.add(owner.turn_id)
            elif fact_candidates(source.result):
                duplicate = duplicate_fact(store, mutation.task_run_id, source.session, source.result)
                if duplicate is None or duplicate.proposal_operation_id == identity:
                    _fail("stage.reference_operation_invalid", Category.INPUT)
            else:
                _fail("stage.reference_operation_invalid", Category.INPUT)
        if len(change.revisions) > 1:
            _fail("stage.multiple_revision_decisions", Category.INPUT)
        for revision in change.revisions:
            if (not isinstance(revision, D8RevisionRecord) or revision.task_run_id != mutation.task_run_id
                    or revision.sequence != len(knowledge.records()) + 1
                    or StageReviewBinding(revision.proposal_request_id, revision.review_request_id) not in change.reviews):
                _fail("stage.revision_application_invalid", Category.INPUT)
            data, action, decision = knowledge._payload(revision)
            from agent_core.stage_planning import frozen_history
            request = store.stage_evidence.request(revision.proposal_request_id)
            origin = (knowledge.validate_change(action, decision, request.context.session, frozen_history(request))
                      if decision.decision == "accept" else None)
            if data["commit_sequence"] != mutation.checkpoint.sequence or data["relation_old_ref"] != origin:
                _fail("stage.revision_application_changed", Category.UNKNOWN)
            if decision.decision == "accept":
                consumed.add(revision.review_request_id)
    for application in change.facts:
        _uuid(application.fact_id, "fact_id")
        if application.candidate_index is not None and type(application.candidate_index) is not int:
            _fail("stage.fact_batch_binding_invalid", Category.INPUT)
        matches = [(b, scope, value) for b, scope, value, index in accepted_claims
            if b.review_operation_id == application.review_operation_id and index == application.candidate_index]
        if len(matches) != 1 or matches[0][2].inferred:
            _fail("stage.fact_not_reviewed")
        _, scope, value = matches[0]
        turn = next((t for t in projected.values() if any(r.road_id == scope.owner_id for r in t.roads)), None)
        sources = read_history(store, mutation.task_run_id, value.support_refs)
        observations = [i for i in sources if i.record_type == "progress"]
        if (turn is None or not observations or any(i.intent_id != scope.owner_id or i.turn_id != turn.turn_id for i in observations)
                or any(i.record_type not in {"progress", "verified_fact"} for i in sources)
                or store._connection.execute("SELECT 1 FROM facts WHERE fact_id=?", (application.fact_id,)).fetchone()):
            invalid = next((i for i in sources if i.record_type not in {"progress", "verified_fact"}
                or i.record_type == "progress" and (i.intent_id != scope.owner_id or turn is None or i.turn_id != turn.turn_id)), None)
            fact_scope_failure(invalid, scope, expected_turn_id=turn.turn_id if turn else None,
                fact_id=application.fact_id, review_id=application.review_operation_id,
                reason="formal_fact_application_invalid")
        consumed.add(application.review_operation_id)
    consumed.update(b.review_operation_id for b, _, value, _ in accepted_claims if value.inferred)
    feedback = {turn.turn_id: turn for binding in change.reviews
        for turn in stage_plans.feedback_turns(store, mutation.task_run_id, binding.review_operation_id)}
    for turn in change.turns:
        if turn == feedback.get(turn.turn_id):
            continue
        consumed.update(_validate_turn(store, mutation.task_run_id, turn, old.get(turn.turn_id), projected,
            accepted, selections, change.planning_submissions, turn.turn_id in route_changes))
    if consumed != {b.review_operation_id for b, _, _, _ in accepted}:
        _fail("stage.accepted_review_not_applied")
    if sum(r.status is IntentStatus.RUNNING for t in projected.values() for r in t.roads) > 1:
        _fail("stage.multiple_running_roads")
    return change


def goal_candidate_proof(store, candidate):
    """Bind the existing protected candidate DTO to an applied stage handoff."""
    evidence = store.stage_evidence
    if evidence is None:
        _fail("stage.evidence_unconfigured", Category.DEPENDENCY)
    data = evidence.read("goal-candidate", candidate.candidate_id)
    if data is None or data["candidate"] != candidate.to_dict():
        _fail("stage.candidate_binding_changed", Category.UNKNOWN)
    proposal_op = store.load_d4_operation(data["proposal_operation_id"])
    review_op = store.load_d4_operation(data["review_operation_id"])
    if (proposal_op is None or review_op is None or proposal_op.application_ref is None
            or proposal_op.application_ref != review_op.application_ref):
        _fail("stage.candidate_not_reviewed", Category.POLICY)
    proposal, review = evidence.read_model_result(proposal_op), evidence.read_model_result(review_op)
    value = proposal.result
    if (not isinstance(value, StagePlanningDecision) or value.kind != "request_final_validation"
            or review.result.decision != "accept" or review.proposal_operation_id != proposal_op.start.operation_id
            or review.proposal_sha256 != proposal_digest(value)):
        _fail("stage.candidate_not_reviewed", Category.POLICY)
    _sources(store, candidate.task_run_id, value.support_refs, formal=True)
    source = evidence.read_history(candidate.task_run_id, (value.source_ref,))[0]
    if candidate.source_ref != "mcp-result/" + source.record_id or source.intent_id != candidate.intent_id or source.turn_id != candidate.turn_id:
        _fail("stage.candidate_source_changed", Category.POLICY)
    _sources(store, candidate.task_run_id, (value.source_ref,))
    from agent_core.competition_candidates import validate_mcp_source
    validate_mcp_source(store, candidate, evidence)
    return proposal, review, value


def validate_goal_mutation(store, mutation):
    """Stage ownership for the original D7 candidate/feedback/check transaction."""
    from agent_core.goal_feedback import project_candidate_feedback, validate_external_check_binding
    from agent_core.runtime import TERMINAL_STATUSES
    metadata, task = mutation.d7, store.load_task_run(mutation.task_run_id)
    config = require_run(store, mutation.task_run_id)
    if metadata is None or task is None or config.goal_validation is None or len(mutation.state_changes) != 1:
        _fail("stage.goal_mutation_invalid", Category.POLICY)
    after = mutation.state_changes[0]
    if not isinstance(after, UpsertTaskRun) or (after.request, after.effective_model_backend, after.started_at,
            after.deadline_at, after.sandbox_id, after.source_root, after.target_network) != (
            task.request, task.effective_model_backend, task.started_at, task.deadline_at, task.sandbox_id, task.source_root, task.target_network):
        _fail("stage.goal_task_binding_changed", Category.POLICY)
    if live_tool_requests(store, mutation.task_run_id):
        _fail("stage.tools_still_running", Category.POLICY)
    if store.blocking_d4_operations(mutation.task_run_id):
        _fail("stage.operation_outcome_unknown", Category.UNKNOWN)
    operation = mutation.operation
    if operation == "register_d7_candidate":
        candidate = metadata.candidate
        if candidate is None or candidate.task_run_id != mutation.task_run_id or task.status is not TaskRunStatus.RUNNING:
            _fail("stage.candidate_not_ready", Category.POLICY)
        if store.load_d7_answer_candidate(candidate.candidate_id) is not None:
            _fail("stage.candidate_already_registered")
        goal_candidate_proof(store, candidate)
        expected = TaskRunStatus.AWAITING_CONFIRMATION if config.goal_validation == "human" else TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
        deadline = min(mutation.event.timestamp + 300, task.deadline_at) if config.goal_validation == "human" else None
        if after.status is not expected or after.confirmation_deadline != deadline:
            _fail("stage.candidate_wait_changed", Category.POLICY)
        return
    if operation == "apply_external_validation_feedback":
        feedback = metadata.feedback
        if feedback is None:
            _fail("stage.feedback_missing", Category.INPUT)
        candidate = store.load_d7_answer_candidate(feedback.candidate_id)
        if candidate is None or candidate.task_run_id != mutation.task_run_id or candidate.evidence_version != feedback.evidence_version:
            _fail("stage.feedback_candidate_changed", Category.POLICY)
        prior = store.list_d7_validation_feedback(mutation.task_run_id, candidate.candidate_id, candidate.evidence_version)
        proposed = (*prior, feedback)
    elif operation == "start_external_validation_check":
        request = metadata.check_request
        if request is None or request.task_run_id != mutation.task_run_id or store.load_d7_external_validation_check(request.query_id) is not None:
            _fail("stage.validation_check_invalid", Category.POLICY)
        validate_external_check_binding(request, store.list_d7_validation_feedback(
            request.task_run_id, request.candidate_id, request.evidence_version))
        if after.status is not task.status:
            _fail("stage.validation_check_changed_status", Category.POLICY)
        return
    elif operation == "complete_external_validation_check":
        completion = metadata.check_completion
        if completion is None:
            _fail("stage.validation_check_invalid", Category.INPUT)
        request = completion.result.request
        saved = store.load_d7_external_validation_check(request.query_id)
        if saved is None or saved.request != request or saved.result is not None or request.task_run_id != mutation.task_run_id:
            _fail("stage.validation_check_binding_changed", Category.POLICY)
        candidate = store.load_d7_answer_candidate(request.candidate_id)
        if candidate is None or candidate.evidence_version != request.evidence_version:
            _fail("stage.feedback_candidate_changed", Category.POLICY)
        prior = store.list_d7_validation_feedback(request.task_run_id, request.candidate_id, request.evidence_version)
        validate_external_check_binding(request, prior)
        if (completion.result.verdict == "unknown") != (completion.reconciliation is None):
            _fail("stage.validation_check_result_changed", Category.POLICY)
        proposed = prior if completion.reconciliation is None else (*prior, completion.reconciliation)
    else:
        _fail("stage.goal_operation_invalid", Category.POLICY)
    projection = project_candidate_feedback(mutation.task_run_id, candidate.candidate_id, candidate.evidence_version, proposed)
    previous = project_candidate_feedback(mutation.task_run_id, candidate.candidate_id, candidate.evidence_version, prior)
    if task.status in TERMINAL_STATUSES or previous.state in {"accepted", "partial", "rejected"}:
        expected = task.status
    elif mutation.event.timestamp >= task.deadline_at:
        expected = TaskRunStatus.DEADLINE_EXCEEDED
    else:
        expected = {"accepted": TaskRunStatus.ACHIEVED, "partial": TaskRunStatus.RUNNING,
            "rejected": TaskRunStatus.RUNNING, "unknown": TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
            "conflict": task.status, "pending": task.status}[projection.state]
    if after.status is not expected:
        _fail("stage.feedback_status_changed", Category.POLICY)
    if expected is TaskRunStatus.ACHIEVED and task.status is not TaskRunStatus.ACHIEVED:
        goal_candidate_proof(store, candidate)


def _validate_turn(store, task_id, turn, prior, projected, accepted, selections, planning_submissions=(), route_change=False):
    consumed = set()
    from agent_core import stage_plans
    provisional = [(identity, store.stage_evidence.read_model_result(store.load_d4_operation(identity)))
        for identity in planning_submissions]

    def match(kind, owner):
        return [(b, value, decision) for b, scope, value, decision in accepted
                if isinstance(value, kind) and (scope.owner_id == owner or kind is TurnCompletion
                    and scope.scope == "road_execution" and scope.owner_id in {r.road_id for r in turn.roads})]
    if prior is None:
        proposals = match(TurnIntent, turn.parent_turn_id or turn.turn_id)
        admitted = any(isinstance(source.result, TurnIntent) and source.result == turn.intent for _, source in provisional)
        if (not (admitted or any(value == turn.intent for _, value, _ in proposals)) or turn.roads or turn.achievement
                or turn.planning_state is not TurnPlanningState.ACTIVE):
            _fail("stage.registration_not_reviewed")
        consumed.update(b.review_operation_id for b, value, _ in proposals if value == turn.intent)
        if turn.parent_turn_id is not None:
            parent = projected.get(turn.parent_turn_id)
            if parent is None or parent.achievement is None or parent.planning_state is not TurnPlanningState.WAITING_CHILD:
                _fail("stage.parent_not_waiting")
            saved_parent = load_turn(store, task_id, turn.parent_turn_id)
            if saved_parent is None or saved_parent.planning_state is not TurnPlanningState.ACTIVE:
                _fail("stage.parent_not_active")
            if turn.parent_turn_id == turn.turn_id:
                _fail("stage.parent_cycle")
    if prior is None:
        _sources(store, task_id, turn.intent.support_refs)
    _sources(store, task_id, tuple(ref for ref in turn.fact_refs if prior is None or ref not in prior.fact_refs), formal=True)
    if (prior is not None and turn.fact_refs != prior.fact_refs and selections.get(turn.turn_id) != turn.fact_refs
            and not any(tuple(dict.fromkeys((*prior.fact_refs, *value.support_refs))) == turn.fact_refs
                        for _, value, _ in match(TurnCompletion, turn.turn_id))):
        _fail("stage.fact_selection_not_bound")
    before = {r.road_id: r for r in prior.roads} if prior else {}
    after = {r.road_id: r for r in turn.roads}
    if not set(before) <= set(after):
        _fail("stage.road_removed")
    pending_before = tuple(r.road_id for r in prior.roads if r.status is IntentStatus.PENDING) if prior else ()
    pending_after = tuple(r.road_id for r in turn.roads if r.status is IntentStatus.PENDING)
    candidates = tuple(r.candidate for r in turn.roads if r.status is IntentStatus.PENDING)
    selections = [b for b, value, review in match(tuple, turn.turn_id)
                  if tuple(value[i] for i in (sorted(review.accepted_road_indices)
                      if stage_plans.admitted(store, b.proposal_operation_id) else review.accepted_road_indices)) == candidates]
    admitted_roads = any(source.session.owner_id == turn.turn_id and isinstance(source.result, tuple)
        and source.result == candidates for _, source in provisional)
    consumed.update(b.review_operation_id for b in selections)
    if (set(after) - set(before) or set(pending_before) == set(pending_after)
            and pending_before != pending_after) and not selections and not admitted_roads and not route_change:
        _fail("stage.roads_not_reviewed")
    closed = {IntentStatus.SUCCEEDED, IntentStatus.FAILED, IntentStatus.INCONCLUSIVE,
              IntentStatus.ABANDONED, IntentStatus.SKIPPED}
    for index, road in enumerate(turn.roads):
        owner = store._connection.execute(
            "SELECT t.task_run_id,r.turn_id FROM stage_roads r JOIN stage_turns t ON t.turn_id=r.turn_id WHERE road_id=?",
            (road.road_id,)).fetchone()
        if owner and tuple(owner) != (task_id, turn.turn_id):
            _fail("stage.cross_scope_road", Category.POLICY)
        previous = before.get(road.road_id)
        if previous is None:
            _sources(store, task_id, road.candidate.support_refs)
            if road.status is not IntentStatus.PENDING:
                _fail("stage.new_road_not_pending")
            continue
        if road.candidate != previous.candidate or previous.status in closed and road.status != previous.status:
            _fail("stage.closed_or_changed_road")
        if previous.status is not IntentStatus.PENDING and prior.roads.index(previous) != index and not (route_change and previous.status not in closed):
            _fail("stage.running_road_reordered")
        if road.status == previous.status:
            continue
        allowed = {IntentStatus.PENDING: {IntentStatus.RUNNING},
                   IntentStatus.RUNNING: {IntentStatus.SUSPENDED, *closed},
                   IntentStatus.SUSPENDED: {IntentStatus.RUNNING}}
        if road.status not in allowed.get(previous.status, set()):
            _fail("stage.road_transition_invalid")
        if road.status in closed:
            verdicts = match(RoadResolution, road.road_id)
            if not any(value.outcome is road.status for _, value, _ in verdicts):
                _fail("stage.road_end_not_reviewed")
            consumed.update(b.review_operation_id for b, value, _ in verdicts if value.outcome is road.status)
            for _, value, _ in verdicts:
                _sources(store, task_id, value.support_refs, formal=road.status is IntentStatus.SUCCEEDED)
        if road.status is IntentStatus.RUNNING and turn.planning_state is not TurnPlanningState.ACTIVE:
            _fail("stage.road_requires_active_turn")
        if road.status is IntentStatus.RUNNING:
            if active_turn(tuple(projected.values())).turn_id != turn.turn_id:
                _fail("stage.execution_frontier_changed", Category.POLICY)
            if turn.achievement is not None and previous.status is IntentStatus.PENDING and not (
                    any(value.kind == "select_road" and value.road_id == road.road_id
                        for _, value, _ in match(StagePlanningDecision, turn.turn_id))
                    or route_change
                    or _just_registered_roads(store, task_id, turn.turn_id)):
                _fail("stage.achieved_road_requires_selection", Category.POLICY)
            from agent_core.stage_knowledge import StageKnowledge
            knowledge = StageKnowledge(store.stage_evidence, task_id, store=store)
            _sources(store, task_id, knowledge.effective_premises(turn, road))
    if turn.achievement is not None and (prior is None or prior.achievement is None):
        if not stage_plans.ready(store, task_id, turn) or any(not stage_plans.ready(store, task_id, turn, road)
                for road in turn.roads if road.status is IntentStatus.RUNNING):
            _fail("stage.completion_planning_not_accepted", Category.POLICY)
        if not any(b.review_operation_id == turn.achievement.review_operation_id
                   and value == turn.achievement.completion for b, value, _ in match(TurnCompletion, turn.turn_id)):
            _fail("stage.achievement_not_reviewed")
        consumed.add(turn.achievement.review_operation_id)
        _sources(store, task_id, turn.achievement.completion.support_refs, formal=True)
    if turn.planning_state is TurnPlanningState.WAITING_CHILD:
        if (turn.achievement is None or any(r.status is IntentStatus.RUNNING for r in turn.roads)
                or not any(t.parent_turn_id == turn.turn_id for t in projected.values())):
            _fail("stage.waiting_without_child")
    if turn.planning_state is TurnPlanningState.EXHAUSTED:
        if any(r.status in {IntentStatus.RUNNING, IntentStatus.SUSPENDED} or
                r.status is IntentStatus.PENDING and turn.achievement is None for r in turn.roads):
            _fail("stage.exhaustion_has_open_roads")
        if prior is None or prior.planning_state is not TurnPlanningState.EXHAUSTED:
            if not match(PlanningExhausted, turn.turn_id):
                _fail("stage.exhaustion_not_reviewed")
            if turn.parent_turn_id and projected[turn.parent_turn_id].planning_state is not TurnPlanningState.ACTIVE:
                _fail("stage.child_feedback_not_atomic")
        consumed.update(b.review_operation_id for b, _, _ in match(PlanningExhausted, turn.turn_id))
    if prior and prior.planning_state is TurnPlanningState.EXHAUSTED and turn.planning_state is TurnPlanningState.ACTIVE:
        known = set(prior.intent.support_refs) | set(prior.fact_refs)
        known.update(ref for r in prior.roads for ref in r.candidate.support_refs)
        if not any(set(value.support_refs if not isinstance(value, tuple) else
                           [ref for r in value for ref in r.support_refs]) - known
                   for _, scope, value, _ in accepted if scope.owner_id == turn.turn_id):
            _fail("stage.new_information_required")
    if prior and prior.planning_state is TurnPlanningState.WAITING_CHILD and turn.planning_state is TurnPlanningState.ACTIVE:
        children = [t for t in projected.values() if t.parent_turn_id == turn.turn_id]
        child = children[-1]
        if not any((scope.owner_id == child.turn_id or scope.owner_id in {r.road_id for r in child.roads})
                and (isinstance(value, PlanningExhausted) and child.planning_state is TurnPlanningState.EXHAUSTED
                     or isinstance(value, StagePlanningDecision) and value.kind == "return_to_parent")
                for _, scope, value, _ in accepted):
            _fail("stage.child_feedback_not_ready")
    return consumed


def _just_registered_roads(store, task_id, turn_id):
    # The latest admitted planning proposal authorizes its first selection;
    # finishing that selection returns an achieved Turn to its planner.
    from agent_core.stage_plans import admitted
    for operation in reversed(store.list_d4_operations(task_id)):
        if (operation.start.role != "inner_action" or operation.start.category != "planning"
                or operation.completion is None or operation.completion.result_kind != "completed"
                or operation.application_ref and operation.application_ref.startswith("stage-discarded/")):
            continue
        source = store.stage_evidence.read_model_result(operation)
        if source.session != PlanningSession("turn_planning", turn_id):
            continue
        if isinstance(source.result, tuple) and admitted(store, operation.start.operation_id):
            queued = next(entry for entry in list_reviews(store, task_id)
                if entry.proposal_operation_id == operation.start.operation_id)
            if (queued.state == "superseded" or queued.state == "settled" and store.stage_evidence.read_model_result(
                    store.load_d4_operation(queued.review_operation_id)).result.decision != "accept"):
                return False
            turn = load_turn(store, task_id, turn_id)
            return all(any(road.candidate == candidate and road.status is IntentStatus.PENDING for road in turn.roads)
                for candidate in source.result)
        break
    # Direct synchronous registration retains its exact commit binding.
    checkpoint = store.load_control_state_rows(task_id).latest_checkpoint.sequence
    rows = store._connection.execute("SELECT r.operation_id FROM d4_operations r JOIN control_mutations c "
        "ON c.task_run_id=r.task_run_id AND c.mutation_id=r.application_ref "
        "WHERE r.task_run_id=? AND r.category='review' AND c.checkpoint_sequence=?", (task_id, checkpoint))
    for row in rows:
        review = store.stage_evidence.read_model_result(store.load_d4_operation(row[0]))
        if review.session == PlanningSession("turn_planning", turn_id) and review.proposal_operation_id:
            proposal = store.stage_evidence.read_model_result(store.load_d4_operation(review.proposal_operation_id))
            if isinstance(proposal.result, tuple) and review.result.decision == "accept":
                return True
    return False


def _review_value(store, review_id, reviewed):
    if store._connection.execute("SELECT 1 FROM stage_review_queue WHERE proposal_operation_id=?",
            (reviewed.proposal_operation_id,)).fetchone():
        return store.stage_evidence.request(review_id).proposal
    return store.stage_evidence.read_model_result(store.load_d4_operation(reviewed.proposal_operation_id)).result


def save(store, mutation, change):
    if change is None:
        return
    task_id = mutation.task_run_id
    from agent_core import stage_plans
    # Bound once at function scope: the settlement loop below may run with an
    # empty change.reviews, so an import inside that loop left it unbound.
    from agent_core.stage_planning import review_members
    plan_rows = stage_plans.submissions(store, task_id, change) if change.config is None else ()
    discarded = _validate_branches(store, mutation, change)
    discarded.update((binding.proposal_operation_id, binding.review_operation_id) for binding in change.discarded_decisions)
    if change.config is not None:
        store._connection.execute("INSERT INTO stage_runs VALUES (?,?)",
                                  (task_id, canonical_json(asdict(change.config))))
    sessions = list(change.sessions)
    for turn in change.turns:
        store._connection.execute(
            "INSERT INTO stage_turns VALUES (?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET "
            "planning_state=excluded.planning_state,achievement_json=excluded.achievement_json,fact_refs_json=excluded.fact_refs_json",
            (turn.turn_id, task_id, turn.parent_turn_id, canonical_json(asdict(turn.intent)), turn.planning_state.value,
             canonical_json(asdict(turn.achievement)) if turn.achievement else None, canonical_json(turn.fact_refs)))
        store._connection.execute("UPDATE stage_roads SET ordinal=-ordinal-1 WHERE turn_id=?", (turn.turn_id,))
        for ordinal, road in enumerate(turn.roads):
            store._connection.execute(
                "INSERT INTO stage_roads VALUES (?,?,?,?,?) ON CONFLICT(road_id) DO UPDATE SET "
                "ordinal=excluded.ordinal,status=excluded.status",
                (road.road_id, turn.turn_id, ordinal, canonical_json(asdict(road.candidate)), road.status.value))
            sessions.append(PlanningSession("road_execution", road.road_id))
        sessions.append(PlanningSession("turn_planning", turn.turn_id))
    for session in sessions:
        if session.scope == "turn_planning":
            owner = store._connection.execute("SELECT task_run_id FROM stage_turns WHERE turn_id=?", (session.owner_id,)).fetchone()
        else:
            owner = store._connection.execute(
                "SELECT t.task_run_id FROM stage_roads r JOIN stage_turns t ON t.turn_id=r.turn_id WHERE road_id=?", (session.owner_id,)).fetchone()
        if owner and owner[0] != task_id or owner is None and session.scope == "road_execution":
            _fail("stage.session_owner_invalid", Category.POLICY)
        store._connection.execute("INSERT OR IGNORE INTO stage_sessions VALUES (?,?,?,?)",
            (task_id, session.scope, session.owner_id, session_id(task_id, session)))
    # Resolve old source evidence before publishing this review's application;
    # mixed batches must not expose an inference before its mutation row exists.
    fact_sources = {}
    for application in change.facts:
        reviewed = store.stage_evidence.read_model_result(store.load_d4_operation(application.review_operation_id))
        value = _review_value(store, application.review_operation_id, reviewed)
        value, _ = reviewed_fact(value, reviewed.result, application.candidate_index)
        fact_sources[application.fact_id] = next(i for i in read_history(store, task_id, value.support_refs)
            if i.record_type == "progress")
    for binding in change.reviews:
        if binding.review_operation_id in {r.review_request_id for r in change.revisions}:
            continue
        request = store.stage_evidence.request(binding.review_operation_id) if store._connection.execute(
            "SELECT 1 FROM stage_review_queue WHERE proposal_operation_id=?", (binding.proposal_operation_id,)).fetchone() else None
        members = review_members(request) if request is not None else (binding.proposal_operation_id,)
        for op_id in (*members, binding.review_operation_id):
            store._mark_d4_operation_applied(op_id, mutation.mutation_id)
    for identity in change.review_submissions:
        store._connection.execute("INSERT OR IGNORE INTO stage_review_queue VALUES (?,'pending',NULL)", (identity,))
    stage_plans.save(store, task_id, plan_rows)
    for binding in change.blocked_reviews:
        store._connection.execute("UPDATE stage_review_queue SET state='blocked',review_operation_id=? "
            "WHERE proposal_operation_id=?", (binding.review_operation_id, binding.proposal_operation_id))
    for binding in change.reviews:
        if not store._connection.execute("SELECT 1 FROM stage_review_queue WHERE proposal_operation_id=?",
                (binding.proposal_operation_id,)).fetchone():
            continue
        for identity in review_members(store.stage_evidence.request(binding.review_operation_id)):
            store._connection.execute("UPDATE stage_review_queue SET state='settled',review_operation_id=? "
                "WHERE proposal_operation_id=?", (binding.review_operation_id, identity))
    for identity, review_id in discarded.items():
        store._mark_d4_operation_applied(identity, "stage-discarded/" + review_id)
        store._connection.execute("UPDATE stage_review_queue SET state='superseded',review_operation_id=? "
            "WHERE proposal_operation_id=?", (review_id, identity))
    for fork in change.branches:
        source = store.stage_evidence.request(fork.proposal_operation_id)
        store._connection.execute("INSERT INTO stage_branches VALUES (?,?,?,?)", (
            branch_id(task_id, fork), session_id(task_id, fork.target_session or source.context.session),
            fork.proposal_operation_id, fork.review_operation_id))
    for ordinal, application in enumerate(change.facts):
        record = store.load_d4_operation(application.review_operation_id)
        review_request = store.stage_evidence.read_model_result(record)
        proposed = _review_value(store, application.review_operation_id, review_request)
        value, _ = reviewed_fact(proposed, review_request.result, application.candidate_index)
        item = fact_sources[application.fact_id]
        store._connection.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?)", (
            application.fact_id, item.turn_id, value.content, item.record_id, FactStatus.VERIFIED.value,
            store._next_event_sequence(task_id), ordinal, fact_verification_ref(application.review_operation_id, application.candidate_index)))
    for identity in change.applications:
        source = store.stage_evidence.read_model_result(store.load_d4_operation(identity))
        duplicate = duplicate_fact(store, task_id, source.session, source.result) if fact_candidates(source.result) else None
        store._mark_d4_operation_applied(identity, "stage-reused/" + duplicate.proposal_operation_id if duplicate else mutation.mutation_id)
    if change.revisions:
        from types import SimpleNamespace
        from agent_core.handoff_storage import save_handoff_mutation
        save_handoff_mutation(store, SimpleNamespace(state_changes=change.revisions), store._next_event_sequence(task_id))
