"""Stage state in the existing SQLite/control transaction, without a scheduler."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from uuid import NAMESPACE_URL, uuid5

from qinyuan_core import Category, Layer, QinyuanError
from agent_core.control_store import StorageError, UpsertTaskRun, canonical_json
from agent_core.domain import IntentStatus
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.operation_accounting import D4OperationConflict, D8HandoffBudgetPolicy, _text, _uuid
from agent_core.runtime import TaskRunStatus
from agent_core.stage_contracts import (
    PlanningExhausted, PlanningSession, RoadCandidate, RoadResolution, RoadSnapshot,
    StageModelEvidence, StageReviewBinding, StageRunConfig, StageStateChange, StageTurnSnapshot,
    TurnAchievement, TurnCompletion, TurnIntent, TurnPlanningReview, TurnPlanningState,
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


class StageStateError(QinyuanError, StorageError):
    category = Category.CONFLICT
    layer = Layer.STORE
    code = "stage.state_conflict"


class StageAdmissionError(StageStateError, D4OperationConflict):
    category = Category.POLICY


def _fail(code, category=Category.CONFLICT):
    error = StageStateError(code, code=code, retryable=False)
    error.category = category
    raise error


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
        return StageRunConfig(*roles, D8HandoffBudgetPolicy.from_dict(data["budget"]))
    except (ValueError, TypeError, KeyError):
        _fail("stage.config_corrupt", Category.UNKNOWN)


def require_run(store, task_id):
    config = load_config(store, task_id)
    if config is None:
        _fail("stage.run_unsupported", Category.POLICY)
    return config


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
    items = read_history(store, task_id, refs)
    if any(i.source_status != "available" or i.conflict for i in items):
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
        if (review_record.start.category != "review" or not isinstance(review.result, TurnPlanningReview)
                or proposal.session != review.session
                or review.proposal_operation_id != proposal_record.start.operation_id
                or review.proposal_sha256 != proposal_digest(proposal.result)):
            _fail("stage.review_binding_changed", Category.UNKNOWN)
        decision = review.result
        if decision.decision not in {"accept", "revise", "reject"}:
            _fail("stage.review_invalid", Category.INPUT)
        _text(decision.reason, "review_reason")
        _sources(store, mutation.task_run_id, decision.support_refs)
        if isinstance(proposal.result, tuple):
            if not proposal.result or any(not isinstance(r, RoadCandidate) for r in proposal.result):
                _fail("stage.roads_proposal_invalid", Category.INPUT)
            indices = decision.accepted_road_indices
            if decision.decision == "accept" and (not indices or len(set(indices)) != len(indices)
                    or any(type(i) is not int or not 0 <= i < len(proposal.result) for i in indices)):
                _fail("stage.road_selection_invalid", Category.INPUT)
        elif decision.accepted_road_indices:
            _fail("stage.road_selection_invalid", Category.INPUT)
        if decision.decision != "accept" and decision.accepted_road_indices:
            _fail("stage.road_selection_invalid", Category.INPUT)
        value = proposal.result
        expected_scope = "road_execution" if isinstance(value, RoadResolution) else "turn_planning"
        if isinstance(value, TurnCompletion):
            expected_scope = proposal.session.scope
        if proposal.session.scope != expected_scope:
            _fail("stage.planning_scope_changed", Category.POLICY)
        if isinstance(value, (TurnIntent, PlanningExhausted, tuple)) and proposal_record.start.category != "planning":
            _fail("stage.planning_category_invalid", Category.POLICY)
        if isinstance(value, tuple):
            refs = tuple(dict.fromkeys(ref for candidate in value for ref in candidate.support_refs))
        else:
            refs = value.support_refs
            _text(value.description if isinstance(value, TurnIntent) else value.reason, "proposal_text")
        _sources(store, mutation.task_run_id, refs)
        if decision.decision == "accept":
            accepted.append((binding, proposal.session, value, decision))
    pending = {r.start.operation_id for r in store.list_d4_operations(mutation.task_run_id)
               if r.completion is None or r.application_ref is None} - used
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
            if mutation.d1_config or mutation.d2_config or mutation.d3 or mutation.d7:
                _fail("stage.legacy_metadata_rejected", Category.POLICY)
            if any(not isinstance(c, UpsertTaskRun) for c in mutation.state_changes):
                _fail("stage.legacy_domain_write_rejected", Category.POLICY)
            if any(c.status in {TaskRunStatus.ACHIEVED, TaskRunStatus.ASSUMED_ACHIEVED}
                   for c in mutation.state_changes):
                _fail("stage.final_validation_not_connected", Category.POLICY)
        return None
    if len(changes) != 1:
        _fail("stage.change_invalid", Category.INPUT)
    change = changes[0]
    if not all(isinstance(value, tuple) for value in (change.turns, change.sessions, change.reviews)):
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
    if change.config is not None:
        config = change.config
        if (mutation.operation != "start_task_run" or store.load_task_run(mutation.task_run_id) is not None
                or len(mutation.state_changes) != 2 or not any(isinstance(c, UpsertTaskRun) for c in mutation.state_changes)
                or mutation.d1_config or mutation.d2_config or mutation.d3 or mutation.d7
                or change.turns or change.reviews or not isinstance(config, StageRunConfig)
                or any(c.status is not TaskRunStatus.RUNNING for c in mutation.state_changes if isinstance(c, UpsertTaskRun))
                or config.outer_role.role != "outer_planning" or config.inner_role.role != "inner_action"
                or not isinstance(config.budget, D8HandoffBudgetPolicy)):
            _fail("stage.start_invalid", Category.INPUT)
        return change
    if mutation.d1_config or mutation.d2_config or mutation.d3 or mutation.d7:
        _fail("stage.legacy_metadata_rejected", Category.POLICY)
    if mutation.operation != "apply_stage_state" or len(mutation.state_changes) != 1:
        _fail("stage.dedicated_operation_required", Category.INPUT)
    require_run(store, mutation.task_run_id)
    task = store.load_task_run(mutation.task_run_id)
    if (task.status is not TaskRunStatus.RUNNING or store._now() >= task.deadline_at
            or task.request != mutation.request or task.effective_model_backend != mutation.effective_model_backend):
        _fail("stage.task_not_active", Category.POLICY)
    accepted = _reviewed(store, mutation, change)
    old = {t.turn_id: t for t in list_turns(store, mutation.task_run_id)}
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
    for turn in change.turns:
        consumed.update(_validate_turn(store, mutation.task_run_id, turn, old.get(turn.turn_id), projected, accepted))
    if consumed != {b.review_operation_id for b, _, _, _ in accepted}:
        _fail("stage.accepted_review_not_applied")
    if sum(r.status is IntentStatus.RUNNING for t in projected.values() for r in t.roads) > 1:
        _fail("stage.multiple_running_roads")
    return change


def _validate_turn(store, task_id, turn, prior, projected, accepted):
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
            if turn.parent_turn_id == turn.turn_id:
                _fail("stage.parent_cycle")
    if prior is None:
        _sources(store, task_id, turn.intent.support_refs)
    _sources(store, task_id, tuple(ref for ref in turn.fact_refs if prior is None or ref not in prior.fact_refs), formal=True)
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
            if not any(value.outcome is road.status and value.support_refs for _, value, _ in verdicts):
                _fail("stage.road_end_not_reviewed")
            consumed.update(b.review_operation_id for b, value, _ in verdicts if value.outcome is road.status)
            for _, value, _ in verdicts:
                _sources(store, task_id, value.support_refs, formal=road.status is IntentStatus.SUCCEEDED)
        if road.status is IntentStatus.RUNNING and turn.planning_state is not TurnPlanningState.ACTIVE:
            _fail("stage.road_requires_active_turn")
        if road.status is IntentStatus.RUNNING:
            _sources(store, task_id, tuple(dict.fromkeys((*turn.intent.support_refs, *road.candidate.support_refs))))
    if turn.achievement is not None and (prior is None or prior.achievement is None):
        if not any(b.review_operation_id == turn.achievement.review_operation_id
                   and value == turn.achievement.completion for b, value, _ in match(TurnCompletion, turn.turn_id)):
            _fail("stage.achievement_not_reviewed")
        consumed.add(turn.achievement.review_operation_id)
        _sources(store, task_id, turn.achievement.completion.support_refs, formal=True)
    if turn.planning_state is TurnPlanningState.WAITING_CHILD:
        if turn.achievement is None or not any(t.parent_turn_id == turn.turn_id for t in projected.values()):
            _fail("stage.waiting_without_child")
    if turn.planning_state is TurnPlanningState.EXHAUSTED:
        if any(r.status in {IntentStatus.PENDING, IntentStatus.RUNNING, IntentStatus.SUSPENDED} for r in turn.roads):
            _fail("stage.exhaustion_has_open_roads")
        if prior is None or prior.planning_state is not TurnPlanningState.EXHAUSTED:
            if not match(PlanningExhausted, turn.turn_id):
                _fail("stage.exhaustion_not_reviewed")
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
        if not any(t.achievement or t.planning_state is TurnPlanningState.EXHAUSTED or t.roads
                   and all(r.status not in {IntentStatus.PENDING, IntentStatus.RUNNING} for r in t.roads)
                   for t in children):
            _fail("stage.child_feedback_not_ready")
    return consumed


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
        for op_id in (binding.proposal_operation_id, binding.review_operation_id):
            store._mark_d4_operation_applied(op_id, mutation.mutation_id)
