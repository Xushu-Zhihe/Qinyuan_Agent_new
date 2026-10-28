"""Stage state in the existing SQLite/control transaction, without a scheduler."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
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
    StageToolBatch, tool_calls, tool_call_key,
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
    def __init__(self, code, *, category=Category.UNKNOWN, layer=Layer.MODEL):
        self.category = category
        super().__init__(code, code=code, layer=layer, retryable=False)


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
        "tool_execution": {"max_in_flight": MAX_IN_FLIGHT_TOOLS, "sandbox_target_in_flight": 1,
                           "max_calls_per_decision": MAX_IN_FLIGHT_TOOLS,
                           "wake": "first_result_before_next_decision_merge_during_generation"}}))


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


def _reviewed(store, mutation, change):
    config = require_run(store, mutation.task_run_id)
    accepted = []
    used = set()
    for binding in change.reviews:
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
            if record.start.base_checkpoint_sequence != mutation.base_checkpoint_sequence:
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
        changing = isinstance(proposal.result, StageConclusionChange)
        category = "fact_review" if isinstance(proposal.result, (StageFactProposal, StageConclusionChange)) else "review"
        if (review_record.start.category != category or not isinstance(review.result, D8ChangeDecision if changing else TurnPlanningReview)
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
        expected_scope = "road_execution" if isinstance(value, (RoadResolution, StageFactProposal)) else "turn_planning"
        if isinstance(value, (TurnCompletion, StageConclusionChange)) or isinstance(value, StageFactProposal) and value.inferred:
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
        if isinstance(value, StageFactProposal):
            if not refs or len(value.content) > 256 or type(value.inferred) is not bool:
                _fail("stage.fact_proposal_invalid", Category.INPUT)
            _text(value.content, "fact_content")
            sources = read_history(store, mutation.task_run_id, refs)
            observations = [i for i in sources if i.record_type == "progress"]
            if not value.inferred and (not observations or any(i.record_type == "reviewed_inference" for i in sources)):
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
    pending = {r.start.operation_id for r in store.list_d4_operations(mutation.task_run_id)
               if (r.completion is None or r.completion.result_kind == "completed" and r.application_ref is None)
               and not (r.start.category == "tool_dispatch"
                        and getattr(store, "live_tool_operation", lambda identity: False)(r.start.operation_id))} - used - set(change.applications)
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
            if any(c.status in {TaskRunStatus.ACHIEVED, TaskRunStatus.ASSUMED_ACHIEVED}
                   for c in mutation.state_changes):
                _fail("stage.final_validation_not_connected", Category.POLICY)
        return None
    if len(changes) != 1:
        _fail("stage.change_invalid", Category.INPUT)
    change = changes[0]
    if not all(isinstance(value, tuple) for value in (change.turns, change.sessions, change.reviews, change.facts, change.applications, change.revisions)):
        _fail("stage.change_invalid", Category.INPUT)
    for session in change.sessions:
        if not isinstance(session, PlanningSession) or session.scope not in {"turn_planning", "road_execution"}:
            _fail("stage.session_scope_invalid", Category.INPUT)
        _uuid(session.owner_id, "session_owner_id")
    for binding in change.reviews:
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
                or change.turns or change.reviews or change.facts or change.applications or change.revisions or not isinstance(config, StageRunConfig)
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
    accepted = _reviewed(store, mutation, change)
    if (change.turns or change.reviews or change.facts or change.revisions) and live_tool_requests(store, mutation.task_run_id):
        _fail("stage.tools_still_running", Category.POLICY)
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
    if sum(t.turn_id not in old for t in change.turns) > 1:
        _fail("stage.multiple_successors", Category.POLICY)
    for binding, scope, value, _ in accepted:
        if isinstance(value, (TurnIntent, tuple, PlanningExhausted, StagePlanningDecision)) and current is not None:
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
    if len({a.review_operation_id for a in change.facts}) != len(change.facts):
        _fail("stage.duplicate_fact_review", Category.INPUT)
    consumed.update(b.review_operation_id for b, scope, value, _ in accepted
        if isinstance(value, PlanningExhausted) and scope.owner_id not in projected)
    selections = {}
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
        matches = [(b, scope, value) for b, scope, value, _ in accepted
            if b.review_operation_id == application.review_operation_id and isinstance(value, StageFactProposal)]
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
    consumed.update(b.review_operation_id for b, _, value, _ in accepted
                    if isinstance(value, StageFactProposal) and value.inferred)
    for turn in change.turns:
        consumed.update(_validate_turn(store, mutation.task_run_id, turn, old.get(turn.turn_id), projected, accepted, selections))
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


def _validate_turn(store, task_id, turn, prior, projected, accepted, selections):
    consumed = set()

    def match(kind, owner):
        return [(b, value, decision) for b, scope, value, decision in accepted
                if isinstance(value, kind) and (scope.owner_id == owner or kind is TurnCompletion
                    and scope.scope == "road_execution" and scope.owner_id in {r.road_id for r in turn.roads})]
    if prior is None:
        proposals = match(TurnIntent, turn.parent_turn_id or turn.turn_id)
        if (not any(value == turn.intent for _, value, _ in proposals) or turn.roads or turn.achievement
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
                  if tuple(value[i] for i in review.accepted_road_indices) == candidates]
    consumed.update(b.review_operation_id for b in selections)
    if (set(after) - set(before) or set(pending_before) == set(pending_after)
            and pending_before != pending_after) and not selections:
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
        if previous.status is not IntentStatus.PENDING and prior.roads.index(previous) != index:
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
                    or _just_registered_roads(store, task_id, turn.turn_id)):
                _fail("stage.achieved_road_requires_selection", Category.POLICY)
            from agent_core.stage_knowledge import StageKnowledge
            knowledge = StageKnowledge(store.stage_evidence, task_id, store=store)
            _sources(store, task_id, knowledge.effective_premises(turn, road))
    if turn.achievement is not None and (prior is None or prior.achievement is None):
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
    # A two-commit register/start boundary is recoverable from the original review.
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


def save(store, mutation, change):
    if change is None:
        return
    task_id = mutation.task_run_id
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
    for binding in change.reviews:
        if binding.review_operation_id in {r.review_request_id for r in change.revisions}:
            continue
        for op_id in (binding.proposal_operation_id, binding.review_operation_id):
            store._mark_d4_operation_applied(op_id, mutation.mutation_id)
    for ordinal, application in enumerate(change.facts):
        record = store.load_d4_operation(application.review_operation_id)
        review_request = store.stage_evidence.read_model_result(record)
        source = store.load_d4_operation(review_request.proposal_operation_id)
        proposed = store.stage_evidence.read_model_result(source)
        value = proposed.result
        item = next(i for i in read_history(store, task_id, value.support_refs) if i.record_type == "progress")
        store._connection.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?)", (
            application.fact_id, item.turn_id, value.content, item.record_id, FactStatus.VERIFIED.value,
            store._next_event_sequence(task_id), ordinal, "stage:" + application.review_operation_id))
    for identity in change.applications:
        store._mark_d4_operation_applied(identity, mutation.mutation_id)
    if change.revisions:
        from types import SimpleNamespace
        from agent_core.handoff_storage import save_handoff_mutation
        save_handoff_mutation(store, SimpleNamespace(state_changes=change.revisions), store._next_event_sequence(task_id))
