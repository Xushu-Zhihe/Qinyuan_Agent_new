"""Provisional planning identities over the original proposals and review queue."""

from dataclasses import replace

from agent_core.domain import IntentStatus
from agent_core.stage_contracts import PlanningSession, TurnIntent, TurnPlanningState
from agent_core.stage_planning import conversation_operation_ids, stage_runtime
from agent_core.stage_storage import _fail, list_reviews, list_turns, session_id, load_session, active_turn
from agent_core.turn_modeling import stable_id
from qinyuan_core import Category


def bindings(store, task_id):
    if store._schema_version < 38:
        return ()
    return tuple((PlanningSession(row[0], row[1]), row[2]) for row in store._connection.execute(
        "SELECT s.scope,s.owner_id,b.proposal_operation_id "
        "FROM stage_plan_bindings b JOIN stage_sessions s ON s.session_id=b.session_id WHERE s.task_run_id=?",
        (task_id,)))


def admitted(store, identity):
    return bool(store._connection.execute("SELECT 1 FROM stage_branches WHERE proposal_operation_id=? "
        "AND review_operation_id IS NULL", (identity,)).fetchone())


def status(store, task_id, session, *, evidence=None):
    binding = next((row for row in bindings(store, task_id) if row[0] == session), None)
    if binding is None:
        return "accepted"
    entry = next(entry for entry in list_reviews(store, task_id) if entry.proposal_operation_id == binding[1])
    if entry.state != "settled":
        return entry.state
    evidence = evidence if evidence is not None else store.stage_evidence
    reviewed = evidence.read_model_result(store.load_d4_operation(entry.review_operation_id)).result
    if reviewed.decision != "accept":
        return "rejected"
    if session.scope == "road_execution":
        source = evidence.read_model_result(store.load_d4_operation(binding[1])).result
        return "accepted" if any(stable_id(task_id, "stage/road/" + binding[1] + "/" + str(index)) == session.owner_id
            for index in reviewed.accepted_road_indices if index < len(source)) else "rejected"
    return "accepted"


def ready(store, task_id, turn, road=None):
    return (status(store, task_id, PlanningSession("turn_planning", turn.turn_id)) == "accepted"
        and (road is None or status(store, task_id, PlanningSession("road_execution", road.road_id)) == "accepted"))


def execution_allowed(store, task_id, turn, road):
    by_id = {t.turn_id: t for t in list_turns(store, task_id)}
    current = turn
    while current.parent_turn_id:
        current = by_id[current.parent_turn_id]
        if current.achievement is None or any(road.status is IntentStatus.RUNNING for road in current.roads):
            return False
    return (turn.planning_state is TurnPlanningState.ACTIVE
        and status(store, task_id, PlanningSession("turn_planning", turn.turn_id)) not in {"rejected", "superseded"}
        and status(store, task_id, PlanningSession("road_execution", road.road_id)) not in {"rejected", "superseded"})


def submissions(store, task_id, change):
    """Validate structural admission; no synthetic semantic acceptance is made."""
    result = []
    if len(set(change.planning_submissions)) != len(change.planning_submissions):
        _fail("stage.planning_submission_duplicate", Category.INPUT)
    old = {t.turn_id: t for t in list_turns(store, task_id)}
    current = active_turn(tuple(old.values()))
    for identity in change.planning_submissions:
        if identity not in change.review_submissions:
            _fail("stage.planning_submission_not_queued", Category.POLICY)
        request = store.stage_evidence.request(identity)
        value = store.stage_evidence.read_model_result(store.load_d4_operation(identity)).result
        if request.context.session.scope != "turn_planning":
            _fail("stage.planning_submission_scope", Category.POLICY)
        if isinstance(value, TurnIntent):
            new = [t for t in change.turns if t.turn_id not in old and t.intent == value]
            if len(new) != 1:
                _fail("stage.planning_target_unbound", Category.POLICY)
            turn = new[0]
            parent = request.context.turn
            if (parent is None and current is not None or parent is not None
                    and (current is None or parent.turn_id != current.turn_id)):
                _fail("stage.planning_frontier_changed", Category.POLICY)
            expected = (request.context.session.owner_id if parent is None and request.context.session.owner_id not in old
                else stable_id(task_id, "stage/turn/" + identity))
            if turn.turn_id != expected or turn.parent_turn_id != (parent.turn_id if parent else None):
                _fail("stage.planning_target_unbound", Category.POLICY)
            if parent is not None and (parent.achievement is None or any(
                    road.status is IntentStatus.RUNNING for road in parent.roads)):
                _fail("stage.parent_not_closed", Category.POLICY)
            result.append((PlanningSession("turn_planning", turn.turn_id), identity))
        elif isinstance(value, tuple):
            turn = next((t for t in change.turns if t.turn_id == request.context.session.owner_id), None)
            prior = old.get(request.context.session.owner_id)
            if turn is None or prior is None:
                _fail("stage.planning_target_unbound", Category.POLICY)
            if current is None or prior.turn_id != current.turn_id:
                _fail("stage.planning_frontier_changed", Category.POLICY)
            old_roads = {r.road_id for r in prior.roads}
            for index, candidate in enumerate(value):
                if any(r.candidate == candidate and r.status is IntentStatus.PENDING for r in prior.roads):
                    continue
                road_id = stable_id(task_id, "stage/road/" + identity + "/" + str(index))
                if not any(r.road_id == road_id and r.road_id not in old_roads and r.candidate == candidate
                        and r.status is IntentStatus.PENDING for r in turn.roads):
                    _fail("stage.planning_target_unbound", Category.POLICY)
                result.append((PlanningSession("road_execution", road_id), identity))
        else:
            _fail("stage.planning_submission_kind", Category.INPUT)
    if len({row[0] for row in result}) != len(result) or any(load_session(store, task_id, row[0])
            and any(old_row[0] == row[0] for old_row in bindings(store, task_id)) for row in result):
        _fail("stage.planning_target_already_bound", Category.CONFLICT)
    return tuple(result)


def feedback_turns(store, task_id, review_id):
    """Retire only plan identities affected by an authenticated substantive critique."""
    rows = bindings(store, task_id)
    if not rows:
        return ()
    evidence = store.stage_evidence
    request = evidence.request(review_id)
    reviewed = evidence.read_model_result(store.load_d4_operation(review_id)).result
    source = request.proposal_operation_id
    rejected = set(evidence.review_rejections(review_id))
    targets, roads = set(), set()
    for session, identity in rows:
        dependent = source in conversation_operation_ids(evidence.request(identity))
        own = identity in rejected
        if identity == source and isinstance(request.proposal, tuple) and reviewed.decision == "accept":
            own = session.scope == "road_execution" and session.owner_id not in {
                stable_id(task_id, "stage/road/" + source + "/" + str(index)) for index in reviewed.accepted_road_indices}
        if (own or dependent and rejected) and session.scope == "turn_planning":
            targets.add(session.owner_id)
        elif own or dependent and rejected:
            roads.add(session.owner_id)
    changed = []
    for turn in list_turns(store, task_id):
        invalid = turn.turn_id in targets
        updated = replace(turn, planning_state=TurnPlanningState.SUPERSEDED if invalid else turn.planning_state,
            roads=tuple(replace(road, status=IntentStatus.ABANDONED)
                if (invalid or road.road_id in roads) and road.status in {
                    IntentStatus.PENDING, IntentStatus.RUNNING, IntentStatus.SUSPENDED} else road for road in turn.roads))
        if turn.planning_state is TurnPlanningState.WAITING_CHILD and any(t.parent_turn_id == turn.turn_id
                and t.turn_id in targets for t in list_turns(store, task_id)):
            updated = replace(updated, planning_state=TurnPlanningState.ACTIVE)
        if updated != turn:
            changed.append(updated)
    return tuple(changed)


def route_advice(store, task_id):
    """A different Outer priority is answered by the original Road planner."""
    evidence = store.stage_evidence
    answered = {stage_runtime(evidence.request(op.start.operation_id)).get("route_advice")
        for op in store.list_d4_operations(task_id) if op.start.role == "inner_action" and op.application_ref is not None
        and not op.application_ref.startswith("stage-discarded/")}
    for entry in list_reviews(store, task_id):
        if entry.state != "settled" or entry.review_operation_id in answered:
            continue
        request = evidence.request(entry.review_operation_id)
        if not isinstance(request.proposal, tuple):
            continue
        review = evidence.read_model_result(store.load_d4_operation(entry.review_operation_id)).result
        if review.decision == "accept" and (review.accepted_road_indices != tuple(sorted(review.accepted_road_indices))
                or len(review.accepted_road_indices) != len(request.proposal)):
            turn = next(t for t in list_turns(store, task_id) if t.turn_id == request.context.session.owner_id)
            if turn.planning_state is not TurnPlanningState.SUPERSEDED:
                return entry.review_operation_id
    return None


def save(store, task_id, rows):
    for session, source in rows:
        store._connection.execute("INSERT INTO stage_plan_bindings VALUES (?,?)",
            (session_id(task_id, session), source))
