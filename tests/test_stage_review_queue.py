"""Durable review queue with real local evidence and controlled model/tool IO."""

import asyncio
import sqlite3
import threading
import time
import json
from dataclasses import asdict
from dataclasses import replace
from uuid import uuid4

import pytest

from test_stage_application import case, overlap_case, save_provider_stop
from agent_adapters.stage_application import StageApplication
from agent_adapters.stage_evidence import StageModelTransportFailure
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.control_store import StorageError
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.stage_contracts import (
    PlanningSession, StageFactProposal, StageReviewBinding, StageStateChange,
    StageTurnSnapshot, TurnPlanningState, StageFactBatch, fact_review_parts,
    StageBranchFork, StagePlanningDecision,
)
from agent_core.stage_planning import stage_runtime
from agent_core.turn_modeling import digest
from agent_core.stage_storage import StageRecoveryRequired, StageStateError, review_groups


def save_confirmed_cancel(case, envelope, *, confirmed=True):
    request = envelope.request
    sent = dict(model_request_id=request.model_request_id, task_run_id=request.task_run_id,
        stage=request.stage, role_config_ref=request.role_config_ref, envelope_sha256=digest(asdict(envelope)))
    case.app.evidence.save("provider-request", request.model_request_id, sent)
    case.app.evidence.save("provider-result", request.model_request_id, dict(model_request_id=request.model_request_id,
        request_sha256=digest(sent), error={"kind": "cancelled", "code": "model_cancelled"},
        transport={"worker_exit_confirmed": confirmed, "transport_end": "cancelled"}))
    return ModelError(ModelErrorKind.CANCELLED, "model_cancelled")


def test_road_inherits_frozen_planning_prefix_with_own_current_execution_context(case):
    result = asyncio.run(case.app.run(case.token))
    planning = next(request for request in case.models.inner.calls if request.stage == "stage_roads")
    actions = [request for request in case.models.inner.calls if request.stage == "stage_action"]
    from agent_core.stage_planning import conversation_operation_ids
    assert actions and all(planning.model_request_id in conversation_operation_ids(request) for request in actions)
    assert all(request.context.session == PlanningSession("road_execution", result.turn.roads[0].road_id)
        and request.tools for request in actions)
    assert all(request.execution_context["stage_runtime"]["branch_id"] for request in actions)
    assert len(case.tools.calls) == 1
    assert len([request for request in case.models.inner.calls if request.stage == "stage_roads"]) == 1


def test_branch_inheritance_cannot_seed_a_different_or_existing_road(case):
    result = asyncio.run(case.app.run(case.token))
    planning = next(request for request in case.models.inner.calls if request.stage == "stage_roads")
    for owner in (str(uuid4()), result.turn.roads[0].road_id):
        with pytest.raises(StageStateError, match="branch_(inheritance_unbound|already_registered)"):
            case.app._commit(StageStateChange(branches=(StageBranchFork(planning.model_request_id,
                target_session=PlanningSession("road_execution", owner)),)))


@pytest.mark.parametrize("which", ["stage_intent", "stage_roads"])
def test_authorized_tool_does_not_wait_for_planning_review(case, which):
    explored = threading.Event()
    invoke = case.tools.call_tool
    checked = []
    async def tool(name, arguments):
        from agent_core.stage_plans import status
        turn = next(turn for turn in case.app.store.list_stage_turns(case.app.task_id)
            if any(road.status.value == "running" for road in turn.roads))
        session = PlanningSession("turn_planning", turn.turn_id) if which == "stage_intent" else PlanningSession(
            "road_execution", turn.roads[0].road_id)
        checked.append(status(case.app.store, case.app.task_id, session))
        result = await invoke(name, arguments)
        explored.set()
        return result
    case.tools.call_tool = tool
    def outer(request, raw):
        if request.stage == "stage_review" and case.app.evidence.request(request.proposal_operation_id).stage == which:
            assert explored.wait(8), "planning review blocked authorized exploration"
        return raw
    case.models.outer.transform = outer
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and checked == ["pending"]
    assert len(case.tools.calls) == 1


def install_successor(case, *, proactive=False, reject_goal=False):
    from agent_core.stage_contracts import TurnCompletion, TurnIntent
    state = dict(started=threading.Event(), release=threading.Event(), child_ready=threading.Event(),
        rejected=False, checks=[], proposals=[], road_plans=[])
    def inner(request, raw):
        turn = request.context.turn
        if proactive and request.stage == "stage_action" and turn.parent_turn_id is None and turn.achievement is None:
            facts = [item.ref for item in request.context.history if item.record_type == "verified_fact"]
            if facts:
                return {"kind": "turn_completion", "reason": "the original criterion has formal support", "support_refs": facts}
        if request.stage == "stage_continuation" and turn.parent_turn_id is None:
            assert turn.achievement is not None and not any(road.status.value == "running" for road in turn.roads)
            state["proposals"].append(request)
            state["child_ready"] = threading.Event()
            return {"kind": "turn_intent", "description": "verify the direct successor", "completion_criterion": "another supported observation",
                "support_refs": list(turn.fact_refs)}
        if request.stage == "stage_roads" and turn.parent_turn_id is not None:
            state["road_plans"].append(request)
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, TurnCompletion) and request.context.turn.parent_turn_id is None:
            state["started"].set()
            assert state["release"].wait(8)
        if isinstance(request.proposal, TurnIntent) and case.app.evidence.request(request.proposal_operation_id).stage == "stage_continuation":
            assert state["child_ready"].wait(8), "child execution waited for its own Intent review"
            if reject_goal and not state["rejected"]:
                state["rejected"] = True
                return {"decision": "revise", "reason": "narrow the successor deliverable before formal acceptance"}
        return raw
    invoke = case.tools.call_tool
    async def tool(name, arguments):
        from agent_core.stage_plans import status
        from agent_core.stage_storage import active_turn
        turn = active_turn(case.app.store.list_stage_turns(case.app.task_id))
        if turn.parent_turn_id is not None:
            parent = case.app.store.load_turn(case.app.task_id, turn.parent_turn_id)
            state["checks"].append((parent.achievement is not None,
                not any(road.status.value == "running" for road in parent.roads),
                status(case.app.store, case.app.task_id, PlanningSession("turn_planning", turn.turn_id)),
                status(case.app.store, case.app.task_id, PlanningSession("road_execution", turn.roads[0].road_id))))
            state["child_ready"].set()
        return await invoke(name, arguments)
    case.models.inner.transform, case.models.outer.transform, case.tools.call_tool = inner, outer, tool
    return state


async def finish_parent_before_successor(case, state):
    task = asyncio.create_task(case.app.run(case.token))
    until = time.monotonic() + 8
    while not state["started"].is_set() and not task.done() and time.monotonic() < until:
        await asyncio.sleep(.01)
    assert state["started"].is_set()
    assert not [request for request in case.models.inner.calls if request.stage == "stage_continuation"]
    root, = case.app.store.list_stage_turns(case.app.task_id)
    assert root.achievement is None and not state["proposals"]
    state["release"].set()
    return await task


@pytest.mark.parametrize("proactive", [False, True])
def test_successor_waits_for_parent_closure_then_plans_and_executes_without_own_reviews(case, proactive):
    from agent_core.stage_planning import conversation_operation_ids
    state = install_successor(case, proactive=proactive)
    result = asyncio.run(finish_parent_before_successor(case, state))
    root, child = case.app.store.list_stage_turns(case.app.task_id)
    assert result.turn.turn_id == child.turn_id and child.parent_turn_id == root.turn_id
    assert state["checks"] == [(True, True, "pending", "pending")]
    assert len(case.tools.calls) == 2 and len(state["proposals"]) == len(state["road_plans"]) == 1
    proposal, roads = state["proposals"][0], state["road_plans"][0]
    assert proposal.context.session == PlanningSession("turn_planning", root.turn_id)
    assert roads.context.session == PlanningSession("turn_planning", child.turn_id)
    assert proposal.model_request_id in conversation_operation_ids(roads)
    snapshot = project(case)
    item = next(item for item in snapshot["review_queue"] if item["proposal_operation_id"] == proposal.model_request_id)
    assert item["author"] == dict(scope="turn_planning", owner_id=root.turn_id)
    assert item["targets"] == [dict(turn_id=child.turn_id, road_id=None)]
    inherited = [branch for branch in snapshot["branches"] if branch["kind"] == "inheritance"]
    assert any(branch["session"]["owner_id"] == child.turn_id and branch["author"]["owner_id"] == root.turn_id for branch in inherited)
    assert any(branch["road_id"] in {road.road_id for road in child.roads} for branch in inherited)


def test_rejected_successor_intent_returns_to_parent_planner_and_preserves_achievement(case):
    state = install_successor(case, reject_goal=True)
    result = asyncio.run(finish_parent_before_successor(case, state))
    root, retired, current = case.app.store.list_stage_turns(case.app.task_id)
    assert root.achievement is not None and retired.planning_state is TurnPlanningState.SUPERSEDED
    assert current.turn_id == result.turn.turn_id and current.parent_turn_id == retired.parent_turn_id == root.turn_id
    assert len(state["proposals"]) == 2 and all(request.context.session.owner_id == root.turn_id for request in state["proposals"])
    assert "narrow the successor deliverable" in json.dumps(state["proposals"][1].messages)
    assert len(case.tools.calls) == 3 and state["checks"] == [(True, True, "pending", "pending")] * 2


def test_rejected_provisional_root_is_corrected_with_new_identity_and_original_observations(case):
    from agent_core.stage_contracts import TurnIntent
    explored = threading.Event()
    state = dict(rejected=False)
    invoke = case.tools.call_tool
    async def tool(name, arguments):
        result = await invoke(name, arguments)
        explored.set()
        return result
    case.tools.call_tool = tool
    def outer(request, raw):
        if isinstance(request.proposal, TurnIntent) and not state["rejected"]:
            assert explored.wait(8)
            state["rejected"] = True
            return {"decision": "revise", "reason": "scope this deliverable more clearly"}
        return raw
    case.models.outer.transform = outer
    result = asyncio.run(case.app.run(case.token))
    turns = case.app.store.list_stage_turns(case.app.task_id)
    assert len(turns) == 2 and turns[0].planning_state is TurnPlanningState.SUPERSEDED
    assert turns[1].turn_id == result.turn.turn_id != turns[0].turn_id
    assert turns[0].intent == turns[1].intent
    assert all(request.context.turn is None for request in case.models.outer.calls
        if isinstance(request.proposal, TurnIntent) and case.app.evidence.request(request.proposal_operation_id).stage == "stage_intent")
    assert len(case.tools.calls) == 2
    observations = [item for item in case.app.evidence.history(case.app.task_id) if item.identity == "tool_observation_not_fact"]
    assert {item.turn_id for item in observations} == {turn.turn_id for turn in turns}


def test_provisional_registration_rolls_back_with_queue_and_branch(case, monkeypatch):
    from agent_core import stage_plans
    session = PlanningSession("turn_planning", case.app._id("stage/root"))
    request = case.app._request("stage_intent", session)
    identity, proposal = asyncio.run(case.app.models.call(request, case.token))
    save = stage_plans.save
    def fail(*args):
        raise RuntimeError("controlled registration failure")
    monkeypatch.setattr(stage_plans, "save", fail)
    with pytest.raises(StorageError, match="controlled registration failure"):
        case.app._submit_planning(request, identity, proposal)
    assert not case.app.store.list_stage_turns(case.app.task_id)
    assert not case.app.store.list_stage_reviews(case.app.task_id)
    assert not stage_plans.bindings(case.app.store, case.app.task_id)
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM stage_branches").fetchone()[0] == 0
    monkeypatch.setattr(stage_plans, "save", save)
    case.app._submit_planning(request, identity, proposal)
    assert stage_plans.status(case.app.store, case.app.task_id, session) == "pending"
    assert len(case.models.inner.calls) == 1


def test_committed_provisional_plan_restarts_without_regenerating_its_proposal(case, monkeypatch):
    commit = case.app._commit
    def stopped(change):
        result = commit(change)
        if change.planning_submissions:
            raise RuntimeError("controlled after provisional commit")
        return result
    monkeypatch.setattr(case.app, "_commit", stopped)
    with pytest.raises(RuntimeError, match="controlled after provisional commit"):
        asyncio.run(case.app.run(case.token))
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    assert len([request for request in case.models.inner.calls if request.stage == "stage_intent"]) == 1
    assert len(case.tools.calls) == 1


@pytest.mark.parametrize("confirmed", [True, False])
def test_planning_generation_correction_requires_confirmed_exit_and_never_replays_unknown(case, confirmed):
    from agent_core.stage_contracts import TurnIntent
    started = threading.Event()
    state = dict(rejected=False, stopped=False)
    invoke = case.models.inner.invoke
    def inner(envelope, timeout, token):
        response = invoke(envelope, timeout, token)
        if envelope.request.stage == "stage_roads" and not state["stopped"]:
            state["stopped"] = True
            started.set()
            until = time.monotonic() + 8
            while not token.is_cancelled and time.monotonic() < until:
                time.sleep(.005)
            assert token.is_cancelled
            return save_confirmed_cancel(case, envelope, confirmed=confirmed)
        return response
    def outer(request, raw):
        if isinstance(request.proposal, TurnIntent) and not state["rejected"]:
            assert started.wait(8)
            state["rejected"] = True
            return {"decision": "revise", "reason": "narrow the provisional criterion"}
        return raw
    case.models.inner.invoke, case.models.outer.transform = inner, outer
    if confirmed:
        result = asyncio.run(case.app.run(case.token))
        assert result.turn.achievement is not None and len(case.tools.calls) == 1
        assert len(case.app.store.list_stage_turns(case.app.task_id)) == 2
    else:
        with pytest.raises(StageRecoveryRequired, match="model_outcome_unknown"):
            asyncio.run(case.app.run(case.token))
        calls = len(case.models.inner.calls), len(case.models.outer.calls)
        with pytest.raises(StageRecoveryRequired, match="operation_outcome_unknown"):
            asyncio.run(case.app.run(case.token))
        assert calls == (len(case.models.inner.calls), len(case.models.outer.calls))
        assert not case.tools.calls


def test_blocked_intent_review_keeps_exploration_but_never_formalizes_the_goal(case):
    from agent_core.stage_contracts import TurnIntent
    from agent_core.stage_plans import status
    invoke = case.models.outer.invoke
    def outer(envelope, timeout, token):
        response = invoke(envelope, timeout, token)
        if isinstance(envelope.request.proposal, TurnIntent):
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return response
    def inner(request, raw):
        if request.stage == "stage_action" and case.tools.calls:
            return {"kind": "road_resolution", "outcome": "inconclusive", "reason": "preserve authorized observation", "support_refs": []}
        return raw
    case.models.outer.invoke, case.models.inner.transform = outer, inner
    case.models.inner.complete = False
    result = asyncio.run(case.app.run(case.token))
    assert len(case.tools.calls) == 1 and result.turn.achievement is None
    assert status(case.app.store, case.app.task_id, PlanningSession("turn_planning", result.turn.turn_id)) == "blocked"
    assert not [item for item in case.app.evidence.history(case.app.task_id) if item.record_type == "verified_fact"]
    from agent_adapters.web.task_result import _stage_task_result
    declaration = dict(task_run_id=case.app.task_id, mode="simulated", protocol_version=3)
    with SqliteControlStore.open_readonly(case.path / "control.sqlite") as store:
        view = _stage_task_result(case.path, declaration, store, case.args["config"])
        assert store._connection.total_changes == 0
    assert view["turns"][0]["review_state"] == "blocked"
    assert not view["errors"] and any(failure["code"] == "stage.review_unavailable" for failure in view["model_failures"])


def test_achieved_parent_can_explore_its_new_road_before_that_road_review(case):
    from test_stage_chain import install_chain
    install_chain(case, expand=True)
    explored = threading.Event()
    review = case.models.outer.transform
    def outer(request, raw):
        if isinstance(request.proposal, tuple) and any(candidate.description == "new direction after child feedback"
                for candidate in request.proposal):
            assert explored.wait(8), "new parent Road waited for review despite Inner's explicit plan"
        return review(request, raw)
    invoke = case.tools.call_tool
    async def tool(name, arguments):
        result = await invoke(name, arguments)
        if sum(call[1]["script"] == "printf root" for call in case.tools.calls) == 2:
            explored.set()
        return result
    case.models.outer.transform, case.tools.call_tool = outer, tool
    result = asyncio.run(case.app.run(case.token))
    assert explored.is_set() and result.turn.achievement is not None
    assert len(case.tools.calls) == 4


@pytest.mark.parametrize("switch,reorder,late", [(False, False, False), (True, False, False),
    (True, True, False), (True, False, True)])
def test_outer_priority_returns_to_road_planner_and_inner_decides(case, switch, reorder, late):
    started = threading.Event()
    state = dict(cancelled=[], choices=[], resumed=[], paused=False)
    def inner(request, raw):
        if stage_runtime(request).get("route_advice"):
            state["choices"].append(request)
            if reorder:
                return {"kind": "roads", "roads": [asdict(road.candidate) for road in reversed(request.context.turn.roads)]}
            return {"kind": "select_road", "road_id": request.context.turn.roads[int(switch)].road_id,
                "reason": "choose using the actual first observation", "support_refs": []}
        if request.stage == "stage_roads" and not request.context.turn.roads:
            return {"kind": "roads", "roads": [{"description": "approach A", "support_refs": []},
                {"description": "approach B", "support_refs": []}]}
        if request.stage == "stage_action":
            own = [item.ref for item in request.context.history if item.record_type == "progress"
                and item.intent_id == request.context.session.owner_id]
            facts = [item.ref for item in request.context.history if item.record_type == "verified_fact"]
            if state["choices"]:
                state["resumed"].append((request.context.session.owner_id, bool(own)))
            if not own:
                return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf scoped"}}
            if late and not state["choices"]:
                return {"kind": "road_resolution", "outcome": "inconclusive", "reason": "a ready but unused decision", "support_refs": []}
            if not facts:
                return {"kind": "fact_candidate", "content": "scoped tool observation", "reason": "directly observed",
                    "support_refs": own, "inferred": False}
            return {"kind": "road_resolution", "outcome": "succeeded", "reason": "scoped support", "support_refs": facts}
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, tuple):
            assert started.wait(8)
            return {"decision": "accept", "reason": "both admissible, B seems more promising", "accepted_road_indices": [1, 0]}
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    if late:
        queued = case.app._queued_review
        async def submit(session, identity, proposal, token):
            from agent_core.stage_contracts import RoadResolution
            if isinstance(proposal, RoadResolution) and not state["choices"]:
                started.set()
            return await queued(session, identity, proposal, token)
        case.app._queued_review = submit
    invoke = case.models.inner.invoke
    def cancellable(envelope, timeout, token):
        response = invoke(envelope, timeout, token)
        request = envelope.request
        own = [item for item in request.context.history if item.record_type == "progress"
            and item.intent_id == request.context.session.owner_id]
        if request.stage == "stage_action" and own and not state["paused"] and not late:
            state["paused"] = True
            started.set()
            until = time.monotonic() + 8
            while not token.is_cancelled and time.monotonic() < until:
                time.sleep(.005)
            assert token.is_cancelled
            state["cancelled"].append(request.model_request_id)
            return save_confirmed_cancel(case, envelope)
        return response
    case.models.inner.invoke = cancellable
    result = asyncio.run(case.app.run(case.token))
    assert len(state["cancelled"]) == int(not late) and len(state["choices"]) == 1
    choice = state["choices"][0]
    assert choice.context.session == PlanningSession("turn_planning", result.turn.turn_id)
    assert state["resumed"][0] == (choice.context.turn.roads[int(switch)].road_id, not switch)
    assert len(case.tools.calls) == 1 + int(switch)
    if late:
        assert any(entry.state == "superseded" and case.app.store.load_d4_operation(entry.proposal_operation_id).application_ref.startswith("stage-discarded/")
            for entry in case.app.store.list_stage_reviews(case.app.task_id))
    assert not any(isinstance(request.proposal, StagePlanningDecision)
        and request.proposal.kind == "select_road" for request in case.models.outer.calls)
    if reorder:
        from agent_core.stage_planning import parse, stage_output_issues
        from agent_core.output_contracts import D8ModelResponse
        invalid = D8ModelResponse(json.dumps({"kind": "roads", "roads": [asdict(choice.context.turn.roads[0].candidate)]}))
        with pytest.raises(ValueError, match="route_order_changed_candidates"):
            parse(choice, invalid)
        assert stage_output_issues(choice, invalid)[0].code == "stage.route_order_changed_candidates"


def pending_root(case):
    session = PlanningSession("turn_planning", case.app._id("stage/root"))
    request = case.app._request("stage_intent", session)
    identity, value = asyncio.run(case.app.models.call(request, case.token))
    case.app._commit(StageStateChange(review_submissions=(identity,)))
    return session, identity, value


def review(case, session, identity, value):
    review_id, result = asyncio.run(case.app._queued_review(session, identity, value, case.token))
    return StageReviewBinding(identity, review_id), result


def test_enqueue_is_not_acceptance_and_reopens_without_new_model_call(case):
    session, identity, value = pending_root(case)
    entries = case.app.store.list_stage_reviews(case.app.task_id)
    assert [(entry.proposal_operation_id, entry.state, entry.review_operation_id) for entry in entries] == [
        (identity, "pending", None)]
    assert not case.app.store.list_stage_turns(case.app.task_id)
    assert not case.app.evidence.applied_reviews(case.app.task_id)
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    # The queue checkpoint must not cause a duplicate Inner proposal on restart.
    request = case.app._request("stage_intent", session)
    assert request.model_request_id == identity
    assert asyncio.run(case.app.models.call(request, case.token)) == (identity, value)
    assert len(case.models.inner.calls) == 1


def test_repeated_submission_keeps_fifo_and_does_not_reset_state(case):
    _, identity, _ = pending_root(case)
    case.app._commit(StageStateChange(review_submissions=(identity,)))
    assert [entry.proposal_operation_id for entry in case.app.store.list_stage_reviews(case.app.task_id)] == [identity]
    with pytest.raises(StageStateError, match="review_submission_duplicate"):
        case.app._commit(StageStateChange(review_submissions=(identity, identity)))


def test_queued_review_can_cross_checkpoints_but_does_not_apply_itself(case):
    session, identity, value = pending_root(case)
    case.app._commit(StageStateChange())
    binding, _ = review(case, session, identity, value)
    # Complete and authenticated late review does not obstruct unrelated metadata.
    case.app._commit(StageStateChange())
    assert case.app.store.load_d4_operation(binding.review_operation_id).application_ref is None
    assert not case.app.store.list_stage_turns(case.app.task_id)
    turn = StageTurnSnapshot(session.owner_id, value, TurnPlanningState.ACTIVE)
    case.app._commit(StageStateChange(turns=(turn,), reviews=(binding,)))
    entry, = case.app.store.list_stage_reviews(case.app.task_id)
    assert entry.state == "settled" and entry.review_operation_id == binding.review_operation_id
    assert case.app.store.load_turn(case.app.task_id, turn.turn_id) == turn
    with pytest.raises(StageStateError, match="review_already_closed"):
        case.app._commit(StageStateChange(review_submissions=(identity,)))


def test_queue_and_domain_change_rollback_together(case):
    session = PlanningSession("turn_planning", case.app._id("stage/root"))
    identity, value = asyncio.run(case.app.models.call(case.app._request("stage_intent", session), case.token))
    turn = StageTurnSnapshot(session.owner_id, value, TurnPlanningState.ACTIVE)
    sequence = case.app.state.latest_checkpoint.sequence
    with pytest.raises(StageStateError, match="registration_not_reviewed"):
        case.app._commit(StageStateChange(turns=(turn,), review_submissions=(identity,)))
    assert not case.app.store.list_stage_reviews(case.app.task_id)
    assert not case.app.store.list_stage_turns(case.app.task_id)
    assert case.app.state.latest_checkpoint.sequence == sequence


def test_queue_never_exempts_an_unknown_outer_operation(case):
    session, identity, value = pending_root(case)
    def lose_response(*args):
        raise RuntimeError("controlled lost response")
    case.models.outer.invoke = lose_response
    with pytest.raises(StageRecoveryRequired, match="model_outcome_unknown"):
        review(case, session, identity, value)
    unresolved = case.app.store.list_d4_unresolved_operations(case.app.task_id)
    assert len(unresolved) == 1 and unresolved[0].start.role == "outer_planning"
    with pytest.raises(StageRecoveryRequired, match="operation_outcome_unknown"):
        case.app._commit(StageStateChange())
    entry, = case.app.store.list_stage_reviews(case.app.task_id)
    assert entry.state == "pending" and not case.tools.calls
    snapshot = project(case)
    item, = snapshot["review_queue"]
    assert item["state"] == "pending" and item["decision"] is None and item["attempt_ids"]
    assert any(call["operation_id"] in item["attempt_ids"] and call["status"] == "unsettled" for call in snapshot["retrospective"])
    assert snapshot["recovery"]["required"] and not case.tools.calls


def failed_review(case, session, identity, value, *, attempts=3):
    invoke = case.models.outer.invoke
    def fail(envelope, timeout, token):
        invoke(envelope, timeout, token)
        save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
        return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
    case.models.outer.invoke = fail
    with pytest.raises(StageModelTransportFailure):
        review(case, session, identity, value)
    assert len(case.models.outer.calls) == attempts
    return StageReviewBinding(identity, case.models.outer.calls[-1].model_request_id)


def test_only_exhausted_confirmed_transport_failure_can_be_marked_blocked(case):
    session, identity, value = pending_root(case)
    binding = failed_review(case, session, identity, value)
    case.app._commit(StageStateChange(blocked_reviews=(binding,)))
    entry, = case.app.store.list_stage_reviews(case.app.task_id)
    assert entry.state == "blocked" and entry.review_operation_id == binding.review_operation_id
    # No fake verdict, acceptance, or extra retry after recording failure.
    assert not case.app.store.list_stage_turns(case.app.task_id)
    assert not case.app.evidence.applied_reviews(case.app.task_id)
    case.app._commit(StageStateChange())
    with pytest.raises(StageStateError, match="review_already_closed"):
        asyncio.run(case.app._queued_review(session, identity, value, case.token))
    assert len(case.models.outer.calls) == 3


@pytest.mark.parametrize("which", ["not_exhausted", "wrong_proposal", "completed_result"])
def test_blocked_status_does_not_replace_failure_proof(case, which):
    session, identity, value = pending_root(case)
    if which == "completed_result":
        binding, _ = review(case, session, identity, value)
    else:
        binding = failed_review(case, session, identity, value)
        if which == "not_exhausted":
            binding = replace(binding, review_operation_id=case.models.outer.calls[0].model_request_id)
        else:
            binding = replace(binding, proposal_operation_id=str(uuid4()))
    with pytest.raises(StageStateError, match="review_failure_(unconfirmed|not_pending)"):
        case.app._commit(StageStateChange(blocked_reviews=(binding,)))
    assert case.app.store.list_stage_reviews(case.app.task_id)[0].state == "pending"


def pending_fact(case, monkeypatch):
    captured = []
    original = case.app._knowledge_action
    async def stop(request, identity, value, token):
        if isinstance(value, StageFactProposal):
            captured.append((request, identity, value))
            raise RuntimeError("controlled before Fact review")
        return await original(request, identity, value, token)
    monkeypatch.setattr(case.app, "_knowledge_action", stop)
    with pytest.raises(RuntimeError, match="controlled before Fact review"):
        asyncio.run(case.app.run(case.token))
    monkeypatch.setattr(case.app, "_knowledge_action", original)
    return captured[0]


def next_proposal(case, source_request, *, transform=None):
    request = replace(source_request, model_request_id=str(uuid4()), frozen_contract_ref=None,
        base_checkpoint_sequence=case.app.state.latest_checkpoint.sequence)
    if transform:
        case.models.inner.transform = transform
    identity, value = asyncio.run(case.app.models.call(request, case.token))
    case.app._commit(StageStateChange(review_submissions=(identity,)))
    return identity, value


def test_same_road_fact_grouping_preserves_each_proposal_and_ordered_barriers(case, monkeypatch):
    request, first, _ = pending_fact(case, monkeypatch)
    case.app._commit(StageStateChange(review_submissions=(first,)))
    second, _ = next_proposal(case, request)
    barrier, _ = next_proposal(case, request, transform=lambda req, raw: {
        "kind": "road_resolution", "outcome": "inconclusive", "reason": "no decisive observation", "support_refs": []})
    third, _ = next_proposal(case, request, transform=lambda req, raw: raw)
    pending_groups = [group for group in review_groups(case.app.store, case.app.task_id)
                      if first in group or second in group or barrier in group or third in group]
    assert pending_groups == [(first, second), (barrier,), (third,)]
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert not any(entry.state == "settled" for entry in case.app.store.list_stage_reviews(case.app.task_id)
                   if entry.proposal_operation_id in {first, second, third, barrier})


def test_target_version_is_checked_again_when_a_late_review_is_applied(case, monkeypatch):
    request, identity, value = pending_fact(case, monkeypatch)
    case.app._commit(StageStateChange(review_submissions=(identity,)))
    binding, _ = review(case, request.context.session, identity, value)
    # Simulate inconsistent target storage; a completed review cannot authorize it.
    case.app.store._connection.execute("UPDATE stage_roads SET candidate_json=? WHERE road_id=?",
        ('{"description":"different target version","support_refs":[]}', request.context.session.owner_id))
    with pytest.raises(StageStateError, match="review_target_changed"):
        case.app._commit(StageStateChange(reviews=(binding,)))
    assert case.app.store.list_stage_reviews(case.app.task_id)[-1].state == "pending"


def test_tool_decision_is_not_a_review_proposal(case, monkeypatch):
    request, _, _ = pending_fact(case, monkeypatch)
    # The already pending Fact is the only new item allowed in this transaction.
    identity = case.models.inner.calls[-1].model_request_id
    case.app._commit(StageStateChange(review_submissions=(identity,)))
    case.models.inner.transform = lambda req, raw: {
        "kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf controlled"}}
    request = replace(request, model_request_id=str(uuid4()), frozen_contract_ref=None,
        base_checkpoint_sequence=case.app.state.latest_checkpoint.sequence)
    identity, _ = asyncio.run(case.app.models.call(request, case.token))
    with pytest.raises(StageStateError, match="review_proposal_invalid"):
        case.app._commit(StageStateChange(review_submissions=(identity,)))
    assert len(case.tools.calls) == 1


def test_existing_runtime_uses_one_queue_without_extra_model_generations(case):
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    entries = case.app.store.list_stage_reviews(case.app.task_id)
    assert entries and all(entry.state == "settled" for entry in entries)
    actual = [r for r in case.models.outer.calls if r.stage == "stage_review"]
    assert len(entries) == len(actual)
    assert len({entry.proposal_operation_id for entry in entries}) == len(entries)
    assert len(case.tools.calls) == 1


def test_review_queue_migration_is_atomic_and_does_not_adopt_existing_operations(case, monkeypatch):
    from agent_core import stage_storage
    task_id = case.app.task_id
    database = case.app.store.path
    asyncio.run(case.app.close())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE stage_plan_bindings")
        connection.execute("DROP TABLE stage_branches")
        connection.execute("DROP TABLE stage_review_queue")
        connection.execute("DELETE FROM schema_versions WHERE version>=36")
    schema = stage_storage.REVIEW_QUEUE_SCHEMA
    monkeypatch.setattr(stage_storage, "REVIEW_QUEUE_SCHEMA", schema.replace("CREATE TABLE", "CREATE TABL"))
    with pytest.raises(StorageError, match="migration failed"):
        SqliteControlStore.open(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 35
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='stage_review_queue'").fetchone() is None
    monkeypatch.setattr(stage_storage, "REVIEW_QUEUE_SCHEMA", schema)
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    assert not case.app.store.list_stage_reviews(task_id)
    assert case.app.state.task_run.task_run_id == task_id


def test_other_task_proposal_cannot_enter_this_queue(case):
    _, identity, _ = pending_root(case)
    other = StageApplication(case.path / "other-task", **case.args)
    try:
        other.start(case.app.state.task_run.request)
        with pytest.raises(StageStateError, match="review_proposal_not_ready"):
            other._commit(StageStateChange(review_submissions=(identity,)))
        assert not other.store.list_stage_reviews(other.task_id)
    finally:
        asyncio.run(other.close())


def test_inner_executes_an_authorized_tool_while_fact_review_is_pending(case):
    explored = threading.Event()
    order, requests = [], []
    invoke_tool = case.tools.call_tool
    async def tool(name, arguments):
        result = await invoke_tool(name, arguments)
        if len(case.tools.calls) == 2:
            order.append("independent_tool_completed")
            explored.set()
        return result
    case.tools.call_tool = tool
    def inner(request, raw):
        if request.stage == "stage_action" and stage_runtime(request).get("pending_reviews"):
            requests.append(request)
            if len(requests) == 1:
                return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf independent"}}
            return {"kind": "wait_for_review", "reason": "next useful step requires this claim"}
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, StageFactProposal):
            order.append("fact_review_started")
            assert explored.wait(8), "Inner waited for Outer instead of exploring"
            order.append("fact_review_finished")
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    assert order.index("fact_review_started") < order.index("independent_tool_completed") < order.index("fact_review_finished")
    assert requests and not any(i.record_type == "verified_fact" for i in requests[0].context.history)
    assert len(case.tools.calls) == 2
    assert all(entry.state == "settled" for entry in case.app.store.list_stage_reviews(case.app.task_id))


def install_queued_claims(case, *, partial=False, cancel_exit=True, cancel_end="cancelled"):
    release_first, long_started = threading.Event(), threading.Event()
    state = dict(submitted=0, corrected=[], cancelled=[], outer_active=0, outer_max=0)
    def inner(request, raw):
        if request.stage != "stage_action":
            return raw
        pending = stage_runtime(request).get("pending_reviews", ())
        if raw.get("kind") == "fact_candidate" and not pending:
            state["submitted"] += 1
            return {**raw, "content": "anchor observation"}
        if pending and state["submitted"] == 1:
            state["submitted"] += 1
            refs = [i.ref for i in request.context.history if i.record_type == "progress"]
            return {"kind": "fact_candidates", "candidates": [
                {"content": "second accepted claim", "reason": "observed", "support_refs": refs, "inferred": False},
                {"content": "second unsupported claim", "reason": "observed", "support_refs": refs, "inferred": False}]}
        if pending and state["submitted"] == 2:
            state["submitted"] += 1
            release_first.set()
            refs = [i.ref for i in request.context.history if i.record_type == "progress"]
            return {"kind": "fact_candidate", "content": "third independent accepted claim",
                "reason": "observed", "support_refs": refs, "inferred": False}
        if pending:
            return {"kind": "wait_for_review", "reason": "need the queued review"}
        if partial and state["cancelled"]:
            state["corrected"].append(request)
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, StageFactProposal):
            assert release_first.wait(8)
        if isinstance(request.proposal, StageFactBatch):
            assert len(request.proposal.candidates) == 3
            if partial:
                assert long_started.wait(8)
            return {"decisions": [{"decision": "reject" if partial and index == 1 else "accept",
                "reason": "unsupported second claim" if index == 1 and partial else "independently supported"}
                for index in range(3)]}
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    invoke_outer = case.models.outer.invoke
    def serial_outer(*args):
        state["outer_active"] += 1
        state["outer_max"] = max(state["outer_max"], state["outer_active"])
        try:
            return invoke_outer(*args)
        finally:
            state["outer_active"] -= 1
    case.models.outer.invoke = serial_outer
    if partial:
        invoke_inner = case.models.inner.invoke
        def cancellable(envelope, timeout, token):
            suspend = state["submitted"] == 3
            response = invoke_inner(envelope, timeout, token)
            request = envelope.request
            if request.stage == "stage_action" and suspend and stage_runtime(request).get("pending_reviews"):
                long_started.set()
                deadline = time.monotonic() + 8
                while not token.is_cancelled and time.monotonic() < deadline:
                    time.sleep(.005)
                assert token.is_cancelled
                sent = dict(model_request_id=request.model_request_id, task_run_id=request.task_run_id,
                    stage=request.stage, role_config_ref=request.role_config_ref, envelope_sha256=digest(asdict(envelope)))
                case.app.evidence.save("provider-request", request.model_request_id, sent)
                timeout_stop = cancel_end == "timeout"
                case.app.evidence.save("provider-result", request.model_request_id, dict(
                    model_request_id=request.model_request_id, request_sha256=digest(sent),
                    error={"kind": "timed_out" if timeout_stop else "cancelled", "code": "model_idle_timeout" if timeout_stop else "model_cancelled"},
                    transport={"worker_exit_confirmed": cancel_exit, "transport_end": cancel_end}))
                state["cancelled"].append(request.model_request_id)
                return ModelError(ModelErrorKind.TIMED_OUT if timeout_stop else ModelErrorKind.CANCELLED,
                    "model_idle_timeout" if timeout_stop else "model_cancelled")
            return response
        case.models.inner.invoke = cancellable
    return state


def test_separate_same_road_submissions_share_one_review_with_global_claim_mapping(case):
    state = install_queued_claims(case)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    batches = [request for request in case.models.outer.calls if isinstance(request.proposal, StageFactBatch)]
    assert len(batches) == 1 and len(stage_runtime(batches[0])["review_members"]) == 2
    rows = case.app.store._connection.execute("SELECT content,verification_ref FROM facts").fetchall()
    assert len(rows) == 4
    refs = [fact_review_parts(row[1]) for row in rows if row[0] != "anchor observation"]
    assert {value[0] for value in refs} == {batches[0].model_request_id}
    assert {value[1] for value in refs} == {0, 1, 2}
    entries = case.app.store.list_stage_reviews(case.app.task_id)
    assert sum(entry.review_operation_id == batches[0].model_request_id for entry in entries) == 2
    assert state["outer_max"] == 1
    assert len([i for i in case.app.evidence.history(case.app.task_id) if i.record_type == "verified_fact"]) == 4


@pytest.mark.parametrize("exit_confirmed", [True, False])
def test_partial_batch_correction_cancels_generation_and_restores_author_prefix(case, exit_confirmed):
    state = install_queued_claims(case, partial=True, cancel_exit=exit_confirmed)
    if not exit_confirmed:
        with pytest.raises(StageRecoveryRequired, match="model_outcome_unknown"):
            asyncio.run(case.app.run(case.token))
        assert not state["corrected"]
        assert len(state["cancelled"]) == 1
        assert case.app.store.list_d4_unresolved_operations(case.app.task_id)
        return
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(state["cancelled"]) == 1
    assert state["corrected"] and state["outer_max"] == 1
    corrected = state["corrected"][0]
    assert "unsupported second claim" in json.dumps(corrected.messages)
    subject = next(json.loads(message["content"])["reviewed_candidates"] for message in corrected.messages
        if message["role"] == "user" and "reviewed_candidates" in json.loads(message["content"]))
    assert [item["candidate"]["content"] for item in subject] == [
        "second accepted claim", "second unsupported claim", "third independent accepted claim"]
    assert "third independent accepted claim" not in " ".join(message["content"] for message in corrected.messages if message["role"] == "assistant")
    assert any(i.content == "third independent accepted claim" and i.record_type == "verified_fact" for i in corrected.context.history)
    assert any(i.record_type == "progress" for i in corrected.context.history)
    rows = case.app.store._connection.execute("SELECT content FROM facts").fetchall()
    assert {row[0] for row in rows} == {"anchor observation", "second accepted claim", "third independent accepted claim"}
    cancelled = state["cancelled"][0]
    assert case.app.store.load_d4_operation(cancelled).completion.result_kind == "failed"
    assert case.app.evidence.read("result", cancelled)["outcome"] == "superseded"
    assert not case.app.store.list_d4_unresolved_operations(case.app.task_id)
    from agent_adapters.web.task_result import _stage_task_result
    from scripts import qymonitor
    before = {path.relative_to(case.path): path.stat().st_size for path in case.path.rglob("*") if path.is_file()}
    with SqliteControlStore.open_readonly(case.path / "control.sqlite") as store:
        snapshot = _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="controlled", goal_validation=None), store, case.app.config)
        monitored = qymonitor.planning_report(case.path, store._connection)["reviews"]
        assert store._connection.total_changes == 0
    assert before == {path.relative_to(case.path): path.stat().st_size for path in case.path.rglob("*") if path.is_file()}
    claims = {candidate["content"]: (item, candidate) for item in snapshot["review_queue"] for candidate in item["candidates"]}
    for content in ("second accepted claim", "third independent accepted claim"):
        item, candidate = claims[content]
        assert candidate["decision"] == "accept" and candidate["fact_id"]
        assert item["review_operation_id"] in item["attempt_ids"]
        assert any(fact["fact_id"] == candidate["fact_id"] and fact["content"] == content for fact in snapshot["verified_facts"])
    assert claims["second unsupported claim"][1]["decision"] == "reject"
    assert claims["second unsupported claim"][1]["fact_id"] is None
    assert any(branch["kind"] == "correction" and branch["review_operation_id"] for branch in snapshot["branches"])
    ids = {branch["branch_id"] for branch in snapshot["branches"]}
    assert all(branch["parent_branch_id"] is None or branch["parent_branch_id"] in ids for branch in snapshot["branches"])
    assert any(item["operation_id"] == cancelled for item in snapshot["inner_activity"])
    assert monitored["branch_count"] == len(snapshot["branches"])
    assert monitored["exploration"]["fact_ids_referenced_by_achievements"]
    assert monitored["exploration"]["unresolved_fact_source_records"] == 0


def project(case):
    from agent_adapters.web.task_result import _stage_task_result
    return _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="controlled", goal_validation=None),
        case.app.store, case.app.config)


def test_known_review_exhaustion_only_blocks_the_claim_and_duplicate_does_not_retry(case, caplog):
    explored = threading.Event()
    state = dict(first=None, after_block=False, duplicated=False, final_tool=False, failures=0)
    case.models.inner.complete = False
    invoke_tool = case.tools.call_tool
    async def tool(name, arguments):
        result = await invoke_tool(name, arguments)
        if len(case.tools.calls) >= 2:
            explored.set()
        return result
    case.tools.call_tool = tool
    def inner(request, raw):
        if request.stage != "stage_action":
            return raw
        pending = stage_runtime(request).get("pending_reviews", ())
        if not pending and raw.get("kind") == "fact_candidate":
            state["first"] = raw
        if any(entry["state"] == "blocked" for entry in pending):
            state["after_block"] = True
            if not state["duplicated"]:
                state["duplicated"] = True
                return state["first"]
            if not state["final_tool"]:
                state["final_tool"] = True
                return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf after_block"}}
            return {"kind": "road_resolution", "outcome": "inconclusive", "reason": "no formal support for this claim", "support_refs": []}
        if pending:
            if not explored.is_set():
                return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf independent"}}
            return {"kind": "wait_for_review", "reason": "waiting for this premise"}
        return raw
    case.models.inner.transform = inner
    invoke_outer = case.models.outer.invoke
    def failed(envelope, timeout, token):
        result = invoke_outer(envelope, timeout, token)
        if isinstance(envelope.request.proposal, StageFactProposal):
            state["failures"] += 1
            if state["failures"] == 3:
                assert explored.wait(8)
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return result
    case.models.outer.invoke = failed
    result = asyncio.run(case.app.run(case.token))
    assert result.reason == "stage.exhausted" and result.turn.achievement is None
    assert state["after_block"] and state["failures"] == 3 and len(case.tools.calls) == 3
    notice = next(record for record in caplog.records if record.msg == "stage.review_unavailable")
    detail = notice.qy.get("model_call")
    assert detail and detail["model_error_code"] == "model_idle_timeout" and detail["correction_count"] == 2
    assert "PRIVATE_REASONING" not in json.dumps(notice.qy)
    assert sum(entry.state == "blocked" for entry in case.app.store.list_stage_reviews(case.app.task_id)) == 1
    snapshot = project(case)
    assert not snapshot["verified_facts"] and not snapshot["errors"] and not snapshot["recovery"]["required"]
    blocked = [item for item in snapshot["review_queue"] if item["state"] == "blocked"]
    assert len(blocked) == 1 and len(blocked[0]["attempt_ids"]) == 3
    assert blocked[0]["decision"] is None and all(item["fact_id"] is None for item in blocked[0]["candidates"])
    assert any(error["code"] == "stage.review_unavailable" for error in snapshot["model_failures"])
    calls = len(case.models.outer.calls)
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    assert asyncio.run(case.app.run(case.token)).reason == "stage.exhausted"
    assert len(case.models.outer.calls) == calls and len(case.tools.calls) == 3


def test_completed_aggregate_review_reopens_without_model_or_tool_replay(case, monkeypatch):
    install_queued_claims(case)
    commit = case.app._commit
    def interrupt(change):
        if len(change.facts) == 3:
            raise RuntimeError("controlled before aggregate publication")
        return commit(change)
    monkeypatch.setattr(case.app, "_commit", interrupt)
    with pytest.raises(RuntimeError, match="before aggregate publication"):
        asyncio.run(case.app.run(case.token))
    before = len([request for request in case.models.outer.calls if isinstance(request.proposal, (StageFactProposal, StageFactBatch))])
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len([request for request in case.models.outer.calls if isinstance(request.proposal, (StageFactProposal, StageFactBatch))]) == before == 2
    assert len(case.tools.calls) == 1
    assert len(project(case)["verified_facts"]) == 4


def test_program_branch_lineage_stays_out_of_provider_text_and_pending_is_in_current_tail(case):
    from agent_adapters.deepseek_roles import build_payload, _stage_input, role_config
    from agent_adapters.stage_evidence import _envelope
    install_queued_claims(case, partial=True)
    asyncio.run(case.app.run(case.token))
    request = next(request for request in case.models.inner.calls
        if request.stage == "stage_action" and stage_runtime(request).get("pending_reviews"))
    contract = case.app.evidence.read("contract", request.model_request_id)
    role = role_config("inner_action", version=17)
    payload = build_payload(_envelope(replace(request, role_config_ref=role.role_config_ref), contract), role)
    wire = json.dumps(payload)
    assert stage_runtime(request)["branch_id"] not in wire
    assert '"sources"' not in payload["messages"][0]["content"]
    assert "pending_reviews" not in payload["messages"][0]["content"]
    assert _stage_input(request)["current"]["review_status"]["pending_reviews"]
    snapshot = project(case)
    assert not snapshot["errors"] and not snapshot["recovery"]["required"]
    assert any(item["code"] == "stage.generation_superseded" for item in snapshot["model_failures"])


def test_rejected_prefix_supersedes_unreviewed_descendant_candidates(case):
    state = install_queued_claims(case)
    inner = case.models.inner.transform
    outer = case.models.outer.transform
    def corrected(request, raw):
        value = inner(request, raw)
        if request.stage == "stage_action" and state["submitted"] > 1 and not stage_runtime(request).get("pending_reviews") and value.get("kind") == "fact_candidate":
            return {**value, "content": "corrected direct observation"}
        return value
    count = []
    def reject_first(request, raw):
        if isinstance(request.proposal, StageFactProposal):
            count.append(request)
            if len(count) == 1:
                outer(request, raw)  # Release only after descendants were submitted.
                return {"decision": "reject", "reason": "the original assertion is unsupported"}
            return raw
        assert not isinstance(request.proposal, StageFactBatch), "obsolete descendants must not get another review"
        return raw
    case.models.inner.transform, case.models.outer.transform = corrected, reject_first
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    entries = case.app.store.list_stage_reviews(case.app.task_id)
    superseded = [entry for entry in entries if entry.state == "superseded"]
    assert superseded and len(count) == 2
    assert all(case.app.store.load_d4_operation(entry.proposal_operation_id).application_ref.startswith("stage-discarded/") for entry in superseded)
    snapshot = project(case)
    assert {item["content"] for item in snapshot["verified_facts"]} == {"corrected direct observation"}
    obsolete = [item for item in snapshot["review_queue"] if item["state"] == "superseded"]
    assert obsolete and all(item["decision"] is None and item["review_operation_id"] for item in obsolete)
    assert all(candidate["decision"] is None and candidate["fact_id"] is None for item in obsolete for candidate in item["candidates"])
    assert len(case.tools.calls) == 1


def test_already_returned_but_unused_tool_decision_is_discarded_before_dispatch(case):
    release, rejected = threading.Event(), threading.Event()
    unused = []
    def inner(request, raw):
        if request.stage == "stage_action" and stage_runtime(request).get("pending_reviews"):
            return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "MUST_NOT_RUN"}}
        if request.stage == "stage_action" and rejected.is_set() and raw.get("kind") == "fact_candidate":
            return {**raw, "content": "corrected observation"}
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, StageFactProposal) and not rejected.is_set():
            assert release.wait(8)
            rejected.set()
            return {"decision": "reject", "reason": "unsupported premise"}
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    call = case.app.models.call
    async def completed_before_feedback(request, token):
        result = await call(request, token)
        if request.stage == "stage_action" and stage_runtime(request).get("pending_reviews"):
            unused.append(result[0])
            release.set()
            await asyncio.wait({case.app.reviews.job.task})
        return result
    case.app.models.call = completed_before_feedback
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len(case.tools.calls) == 1 and unused
    record = case.app.store.load_d4_operation(unused[0])
    assert record.application_ref.startswith("stage-discarded/")
    assert case.app.evidence.read("result", unused[0])["outcome"] == "valid"
    assert not any(arguments["script"] == "MUST_NOT_RUN" for _, arguments in case.tools.calls)


def test_capacity_summary_waits_for_outer_and_survives_the_next_submission_fork(case):
    release = threading.Event()
    state = dict(submitted=0, compacted=False, active=0, maximum=0)
    def inner(request, raw):
        if request.stage != "stage_action":
            return raw
        pending = stage_runtime(request).get("pending_reviews", ())
        if raw.get("kind") == "fact_candidate" and not pending:
            state["submitted"] = 1
            return {**raw, "content": "claim 1"}
        if pending and (state["submitted"] < 4 or request.capacity_recovery_of is not None):
            state["submitted"] += 1
            refs = [i.ref for i in request.context.history if i.record_type == "progress"]
            return {"kind": "fact_candidate", "content": "claim " + str(state["submitted"]),
                "reason": "direct", "support_refs": refs, "inferred": False}
        if pending:
            return {"kind": "wait_for_review", "reason": "all claims already submitted"}
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, StageFactProposal) and not state["compacted"]:
            assert release.wait(8)
        if isinstance(request.proposal, StageFactBatch):
            return {"decisions": [{"decision": "accept", "reason": "supported"} for _ in request.proposal.candidates]}
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    invoke = case.models.outer.invoke
    def serial(*args):
        state["active"] += 1
        state["maximum"] = max(state["active"], state["maximum"])
        try:
            return invoke(*args)
        finally:
            state["active"] -= 1
    case.models.outer.invoke = serial
    fits = case.app.models.capacity_port.fits
    def capacity(envelope, policy):
        request = envelope.request
        if request.stage == "stage_action" and state["submitted"] == 4 and not state["compacted"]:
            state["compacted"] = True
            release.set()
            return False
        return fits(envelope, policy)
    case.app.models.capacity_port.fits = capacity
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    summaries = [request for request in case.models.outer.calls if request.stage == "stage_summary"]
    assert len(summaries) == 1 and state["maximum"] == 1
    assert state["submitted"] == 5 and len(project(case)["verified_facts"]) == 5
    fifth = next(request for request in case.models.inner.calls if request.capacity_recovery_of is not None)
    following = next(request for request in case.models.inner.calls
        if request.stage == "stage_action" and request.base_checkpoint_sequence > fifth.base_checkpoint_sequence
        and stage_runtime(request).get("pending_reviews"))
    assert summaries[0].model_request_id in json.dumps(following.messages)


def test_fact_review_and_publication_do_not_wait_for_an_unrelated_inflight_tool(overlap_case, monkeypatch):
    case, control = overlap_case, overlap_case.overlap
    monkeypatch.setattr("agent_adapters.stage_application.TOOL_BATCH_WAIT_SECONDS", 0.05)
    control.release["A"].set()
    actions = []
    def decision(request):
        actions.append(request)
        if len(actions) == 1:
            return dict(kind="mcp_calls", calls=[dict(tool_name="shell.run_bash", arguments={"script": "A"}),
                dict(tool_name="web.search", arguments={"query": "B"})])
        facts = [item.ref for item in request.context.history if item.record_type == "verified_fact"]
        if facts:
            return dict(kind="road_resolution", outcome="succeeded", reason="local result supported", support_refs=facts)
        if stage_runtime(request).get("pending_reviews"):
            return dict(kind="wait_for_review", reason="needs the submitted result")
        return dict(kind="fact_candidate", content="The local tool returned A.", reason="own completed observation",
            support_refs=[item.ref for item in request.context.history if item.identity == "tool_observation_not_fact"], inferred=False)
    control.decide = decision
    async def exercise():
        running = asyncio.create_task(case.app.run(case.token))
        await control.started["B"].wait()
        async def published():
            while case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0:
                if running.done():
                    running.result()
                await asyncio.sleep(.01)
            assert "B" in control.active and not control.release["B"].is_set()
        try:
            await asyncio.wait_for(published(), timeout=8)
        finally:
            control.release["B"].set()
            await running
    asyncio.run(exercise())
    assert len(control.calls) == 2


def test_prepared_group_survives_crash_before_any_review_operation_starts(case, monkeypatch):
    write = case.app.evidence.save
    def stop_after_manifest(kind, identity, value):
        write(kind, identity, value)
        if kind == "review-group":
            raise RuntimeError("controlled after manifest before dispatch")
    monkeypatch.setattr(case.app.evidence, "save", stop_after_manifest)
    with pytest.raises(RuntimeError, match="before dispatch"):
        asyncio.run(case.app.run(case.token))
    entry = next(entry for entry in case.app.store.list_stage_reviews(case.app.task_id) if entry.state == "pending")
    group = case.app.evidence.read("review-group", entry.proposal_operation_id)
    assert case.app.evidence.request(group["model_request_id"])
    assert case.app.store.load_d4_operation(group["model_request_id"]) is None
    assert not [request for request in case.models.outer.calls if isinstance(request.proposal, StageFactProposal)]
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len([request for request in case.models.outer.calls if isinstance(request.proposal, StageFactProposal)]) == 1
    assert len(case.tools.calls) == 1


def test_changed_aggregate_members_stop_before_outer_dispatch(case, monkeypatch):
    install_queued_claims(case)
    make = case.app.reviews._make_request
    def wrong(members):
        request = make(members)
        if len(members) > 1:
            runtime = {**stage_runtime(request), "review_members": list(reversed(members))}
            request = replace(request, execution_context={**request.execution_context, "stage_runtime": runtime})
        return request
    monkeypatch.setattr(case.app.reviews, "_make_request", wrong)
    with pytest.raises(StageStateError, match="review_members_changed"):
        asyncio.run(case.app.run(case.token))
    assert not [request for request in case.models.outer.calls if isinstance(request.proposal, StageFactBatch)]
    assert len(case.tools.calls) == 1


@pytest.mark.parametrize("dispatch", [None, "synchronous_v1"])
def test_old_frozen_execution_policy_stays_readable_but_is_not_resumed(case, monkeypatch, dispatch):
    import agent_adapters.stage_application as application
    from agent_adapters.web.task_result import _stage_task_result
    policy = application.execution_policy
    def historical(config):
        value = policy(config)
        if dispatch is None:
            value.pop("review_queue")
        else:
            value["review_queue"]["dispatch"] = dispatch
        return value
    monkeypatch.setattr(application, "execution_policy", historical)
    other = StageApplication(case.path / "historical", **case.args)
    try:
        other.start(case.app.state.task_run.request)
        result = _stage_task_result(other.directory, dict(task_run_id=other.task_id, mode="controlled", goal_validation=None),
            other.store, other.config)
        assert not result["errors"]
        monkeypatch.setattr(application, "execution_policy", policy)
        with pytest.raises(StageStateError, match="execution_configuration_changed"):
            other.bind(other.task_id)
    finally:
        asyncio.run(other.close())


def test_async_queue_schema_migration_rolls_back_preserving_pending_proposals(case, monkeypatch):
    from agent_core import stage_storage
    _, identity, _ = pending_root(case)
    database = case.path / "schema36.sqlite"
    with sqlite3.connect(database) as copy:
        case.app.store._connection.backup(copy)
        copy.execute("DROP TABLE stage_plan_bindings")
        copy.execute("DROP TABLE stage_branches")
        rows = copy.execute("SELECT * FROM stage_review_queue ORDER BY rowid").fetchall()
        copy.execute("DROP TABLE stage_review_queue")
        copy.executescript(stage_storage.REVIEW_QUEUE_SCHEMA)
        copy.executemany("INSERT INTO stage_review_queue VALUES (?,?,?)", rows)
        copy.execute("DELETE FROM schema_versions WHERE version>=37")
    schema = stage_storage.ASYNC_REVIEW_SCHEMA
    monkeypatch.setattr(stage_storage, "ASYNC_REVIEW_SCHEMA", schema.replace("CREATE INDEX", "CREATE INDX"))
    with pytest.raises(StorageError, match="migration failed"):
        SqliteControlStore.open(database)
    with sqlite3.connect(database) as copy:
        assert copy.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 36
        assert copy.execute("SELECT * FROM stage_review_queue").fetchall() == rows
        assert not copy.execute("SELECT 1 FROM sqlite_master WHERE name='stage_branches'").fetchone()
    monkeypatch.setattr(stage_storage, "ASYNC_REVIEW_SCHEMA", schema)
    with SqliteControlStore.open(database) as store:
        assert store._schema_version == 38
        assert [entry.proposal_operation_id for entry in store.list_stage_reviews(case.app.task_id)] == [identity]
        assert not store._connection.execute("PRAGMA foreign_key_check").fetchall()


def test_aggregation_shrinks_to_existing_capacity_without_spending_a_correction(case):
    install_queued_claims(case)
    outer = case.models.outer.transform
    def sized(request, raw):
        if isinstance(request.proposal, StageFactBatch):
            return {"decisions": [{"decision": "accept", "reason": "supported"} for _ in request.proposal.candidates]}
        return outer(request, raw)
    case.models.outer.transform = sized
    fits = case.app.models.capacity_port.fits
    def bounded(envelope, policy):
        return (not isinstance(envelope.request.proposal, StageFactBatch)
            or len(envelope.request.proposal.candidates) <= 2) and fits(envelope, policy)
    case.app.models.capacity_port.fits = bounded
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    calls = [request for request in case.models.outer.calls if isinstance(request.proposal, (StageFactProposal, StageFactBatch))]
    assert len(calls) == 3 and all(request.correction_of_model_request_id is None for request in calls)
    assert len(project(case)["verified_facts"]) == 4


@pytest.mark.parametrize("native", [False, True])
def test_aggregated_inference_remains_separate_from_formal_fact(case, native):
    install_queued_claims(case)
    case.models.inner.native = case.models.outer.native = native
    inner = case.models.inner.transform
    def inferred(request, raw):
        value = inner(request, raw)
        if value.get("kind") == "fact_candidates":
            value["candidates"][1].update(content="a tentative explanation", inferred=True)
        return value
    case.models.inner.transform = inferred
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    history = case.app.evidence.history(case.app.task_id)
    assert len([item for item in history if item.record_type == "verified_fact"]) == 3
    assert [(item.content, item.identity) for item in history if item.record_type == "reviewed_inference"] == [
        ("a tentative explanation", "reviewed_inference_not_verified")]
    assert not any(item["content"] == "a tentative explanation" for item in project(case)["verified_facts"])


@pytest.mark.parametrize("transport_end", ["not_dispatched", "timeout"])
def test_branch_cancellation_accepts_only_confirmed_early_stop_or_timeout_race(case, transport_end):
    state = install_queued_claims(case, partial=True, cancel_end=transport_end)
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len(state["cancelled"]) == 1 and state["corrected"]
    stop = case.app.evidence.read("branch-stop", state["cancelled"][0])
    assert stop["returned_kind"] == ("transport_failure" if transport_end == "timeout" else "cancelled")
    assert not project(case)["errors"]


def test_blocked_claim_does_not_prevent_completion_from_other_formal_evidence(case):
    state = dict(attempts=0, explored=False)
    def inner(request, raw):
        if request.stage != "stage_action":
            return raw
        facts = [item.ref for item in request.context.history if item.record_type == "verified_fact"]
        if facts:
            return dict(kind="road_resolution", outcome="succeeded", reason="independent result supported", support_refs=facts)
        pending = stage_runtime(request).get("pending_reviews", ())
        if any(entry["state"] == "blocked" for entry in pending):
            if not state["explored"]:
                state["explored"] = True
                return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "printf independent_B"})
            refs = [item.ref for item in request.context.history if item.record_type == "progress"]
            return dict(kind="fact_candidate", content="independent claim B", reason="a different original observation",
                support_refs=refs[-1:], inferred=False)
        if pending:
            return dict(kind="wait_for_review", reason="no independent action chosen yet")
        if raw.get("kind") == "fact_candidate":
            return {**raw, "content": "claim A"}
        return raw
    case.models.inner.transform = inner
    invoke = case.models.outer.invoke
    def outer(envelope, timeout, token):
        result = invoke(envelope, timeout, token)
        if isinstance(envelope.request.proposal, StageFactProposal) and envelope.request.proposal.content == "claim A":
            state["attempts"] += 1
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return result
    case.models.outer.invoke = outer
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and state["attempts"] == 3
    snapshot = project(case)
    assert {item["content"] for item in snapshot["verified_facts"]} == {"independent claim B"}
    assert not snapshot["errors"] and len(case.tools.calls) == 2


def test_aggregate_transport_failure_blocks_all_members_without_partial_acceptance(case):
    install_queued_claims(case)
    inner = case.models.inner.transform
    def finish_with_anchor(request, raw):
        pending = stage_runtime(request).get("pending_reviews", ())
        if request.stage == "stage_action" and pending and all(entry["state"] == "blocked" for entry in pending):
            return dict(kind="road_resolution", outcome="succeeded", reason="only the anchor is formal",
                support_refs=[item.ref for item in request.context.history if item.record_type == "verified_fact"])
        return inner(request, raw)
    case.models.inner.transform = finish_with_anchor
    invoke = case.models.outer.invoke
    def fail(envelope, timeout, token):
        result = invoke(envelope, timeout, token)
        if isinstance(envelope.request.proposal, StageFactBatch):
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return result
    case.models.outer.invoke = fail
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    calls = [request for request in case.models.outer.calls if isinstance(request.proposal, StageFactBatch)]
    assert len(calls) == 3
    blocked = [entry for entry in case.app.store.list_stage_reviews(case.app.task_id) if entry.state == "blocked"]
    assert len(blocked) == 2 and {entry.review_operation_id for entry in blocked} == {calls[-1].model_request_id}
    snapshot = project(case)
    assert {item["content"] for item in snapshot["verified_facts"]} == {"anchor observation"}
    assert not snapshot["errors"] and not snapshot["recovery"]["required"]


@pytest.mark.parametrize("boundary", ["after_decision", "scheduled_dispatch"])
def test_review_arriving_between_decision_and_tool_admission_stops_dispatch(case, monkeypatch, boundary):
    release, rejected = threading.Event(), threading.Event()
    def inner(request, raw):
        if request.stage == "stage_action" and stage_runtime(request).get("pending_reviews"):
            return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "MUST_NOT_RUN"})
        if request.stage == "stage_action" and rejected.is_set() and raw.get("kind") == "fact_candidate":
            return {**raw, "content": "corrected post-fence assertion"}
        return raw
    def outer(request, raw):
        if isinstance(request.proposal, StageFactProposal) and not rejected.is_set():
            assert release.wait(8)
            rejected.set()
            return {"decision": "reject", "reason": "unsupported premise"}
        return raw
    case.models.inner.transform, case.models.outer.transform = inner, outer
    async def hold():
        job = case.app.reviews.job
        release.set()
        await asyncio.wait({job.task})
    if boundary == "after_decision":
        start = case.app._start_tools
        async def delayed(*args, **kwargs):
            if case.app.reviews.job is not None:
                await hold()
            return await start(*args, **kwargs)
        monkeypatch.setattr(case.app, "_start_tools", delayed)
    else:
        create = asyncio.create_task
        def delayed(coroutine, *args, **kwargs):
            if "_start_tools.<locals>.dispatch" in coroutine.__qualname__ and case.app.reviews.job is not None:
                async def wait_then_dispatch():
                    await hold()
                    return await coroutine
                return create(wait_then_dispatch(), *args, **kwargs)
            return create(coroutine, *args, **kwargs)
        monkeypatch.setattr(asyncio, "create_task", delayed)
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len(case.tools.calls) == 1
    request = next(request for request in case.models.inner.calls
        if request.stage == "stage_action" and stage_runtime(request).get("pending_reviews"))
    assert case.app.store.load_d4_operation(request.model_request_id).application_ref.startswith("stage-discarded/")
    stop = case.app.evidence.read("tool-dispatch-stop", case.app._id("mcp/" + request.model_request_id))
    assert stop["outcome"] == "not_dispatched"


def test_partial_tool_batch_retains_admitted_result_and_fences_the_unadmitted_member(overlap_case, monkeypatch):
    case, control = overlap_case, overlap_case.overlap
    for label in "AB":
        control.release[label].set()
    release, rejected = threading.Event(), threading.Event()
    proposals = []
    def decision(request):
        progress = [item.ref for item in request.context.history if item.identity == "tool_observation_not_fact"]
        facts = [item.ref for item in request.context.history if item.record_type == "verified_fact"]
        if facts:
            return dict(kind="road_resolution", outcome="succeeded", reason="corrected result", support_refs=facts)
        if stage_runtime(request).get("pending_reviews"):
            proposals.append(request.model_request_id)
            return dict(kind="mcp_calls", calls=[dict(tool_name="shell.run_bash", arguments={"script": "A"}),
                dict(tool_name="web.search", arguments={"query": "B"})])
        if not progress:
            return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "A"})
        return dict(kind="fact_candidate", content="corrected observation" if rejected.is_set() else "initial assertion",
            reason="own observations", support_refs=progress, inferred=False)
    control.decide = decision
    def outer(request, raw):
        if isinstance(request.proposal, StageFactProposal) and not rejected.is_set():
            assert release.wait(8)
            rejected.set()
            return dict(decision="reject", reason="unsupported premise")
        return raw
    case.models.outer.transform = outer
    create = asyncio.create_task
    def delayed(coroutine, *args, **kwargs):
        if "_start_tools.<locals>.dispatch" in coroutine.__qualname__:
            entry = coroutine.cr_frame.f_locals["entry"]
            if entry.call.tool_name == "web.search":
                async def wait_then_dispatch():
                    job = case.app.reviews.job
                    release.set()
                    await asyncio.wait({job.task})
                    return await coroutine
                return create(wait_then_dispatch(), *args, **kwargs)
        return create(coroutine, *args, **kwargs)
    monkeypatch.setattr(asyncio, "create_task", delayed)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is None  # This overlap fixture explicitly returns incomplete at Turn completion.
    assert control.calls == [("A", "shell.run_bash"), ("A", "shell.run_bash")]
    parent = proposals[0]
    assert case.app.store.load_d4_operation(parent).application_ref == parent
    assert len([item for item in case.app.evidence.history(case.app.task_id) if item.identity == "tool_observation_not_fact"]) == 2
    assert any(item.content == "corrected observation" for item in case.app.evidence.history(case.app.task_id) if item.record_type == "verified_fact")
