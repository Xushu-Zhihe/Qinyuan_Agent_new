"""U5 real control/evidence/dispatch paths with scoped, controlled model IO."""

import asyncio
from dataclasses import replace

import pytest

from test_stage_application import case
from agent_adapters.stage_application import StageApplication
from agent_core.domain import IntentStatus
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.operation_accounting import OperationRecoveryRequired
from agent_core.stage_contracts import PlanningSession, StagePlanningDecision, StageStateChange, TurnIntent, TurnPlanningState
from agent_core.stage_storage import StageStateError, active_turn


def install_chain(case, *, spare=False, combined=False, blocked=False, expand=False):
    def inner(request, raw):
        turn = request.context.turn
        name = turn.intent.description if turn else "root"
        history = request.context.history
        facts = [i.ref for i in history if i.record_type == "verified_fact"]
        local = [i.ref for i in history if i.record_type == "verified_fact" and i.turn_id == turn.turn_id] if turn else []
        own = request.context.session.owner_id
        feedback = request.context.feedback_refs
        if request.stage == "stage_intent":
            return {"kind": "turn_intent", "description": "root", "completion_criterion": "two observations" if combined else "one observation", "support_refs": []}
        if request.stage == "stage_roads":
            if not turn.roads:
                count = 2 if name == "root" and (spare or combined) else 1
                return {"kind": "roads", "roads": [{"description": name + "/" + str(n), "support_refs": []} for n in range(count)]}
            return {"kind": "exhausted", "reason": "no further usable route", "support_refs": [i.ref for i in history if i.turn_id == turn.turn_id]}
        if request.stage == "stage_action":
            observations = [i.ref for i in history if i.record_type == "progress" and i.intent_id == own
                and not i.record_id.startswith(("revision/", "feedback/"))]
            observed_facts = [i.ref for i in history if i.record_type == "verified_fact" and i.intent_id == own]
            if not observations:
                return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf " + name}}
            if blocked and name == "child1":
                return {"kind": "return_to_parent", "reason": "the settled observation identifies a blocking dependency", "support_refs": observations}
            if name == "child2":
                return {"kind": "road_resolution", "outcome": "failed", "reason": "this scoped attempt did not satisfy the criterion", "support_refs": observations}
            if not observed_facts:
                return {"kind": "fact_candidate", "content": name + " observed " + own, "reason": "own original observation", "support_refs": observations, "inferred": False}
            return {"kind": "road_resolution", "outcome": "succeeded", "reason": "own result obtained", "support_refs": observed_facts}
        if request.stage == "stage_completion":
            complete = name != "child2" and (not combined or name != "root" or len(local) >= 2)
            return {"kind": "turn_completion" if complete else "incomplete", "reason": "check only the fixed criterion", "support_refs": local}
        if request.stage == "stage_continuation":
            if feedback:
                if name == "child1":
                    return {"kind": "exhausted", "reason": "no continuation after the scoped child feedback", "support_refs": list(feedback)}
                pending = next((r for r in turn.roads if r.status is IntentStatus.PENDING), None)
                if spare and pending:
                    return {"kind": "select_road", "road_id": pending.road_id, "reason": "reassess the retained alternative", "support_refs": list(feedback)}
                if expand and len(turn.roads) == 1:
                    return {"kind": "roads", "roads": [{"description": "new direction after child feedback", "support_refs": list(feedback)}]}
                return {"kind": "request_final_validation", "reason": "hand off without declaring TaskRun success", "support_refs": local}
            return {"kind": "turn_intent", "description": "child1" if name == "root" else "child2",
                "completion_criterion": "a useful scoped result", "support_refs": facts}
        return raw
    case.models.inner.transform = inner
    case.models.outer.transform = lambda request, raw: ({**raw, "accepted_road_indices": list(range(len(request.proposal)))}
        if isinstance(request.proposal, tuple) else raw)


def reopen(case):
    identity = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(identity)


@pytest.mark.parametrize("native", [False, True])
def test_three_stages_return_only_to_direct_parent_and_keep_original_achievement(case, native):
    case.models.inner.native = case.models.outer.native = native
    install_chain(case, combined=True)
    result = asyncio.run(case.app.run(case.token))
    turns = case.app.store.list_stage_turns(case.app.task_id)
    root, first, second = turns
    assert result.reason == "stage.final_validation_requested" and result.turn == root
    assert first.parent_turn_id == root.turn_id and second.parent_turn_id == first.turn_id
    assert len(first.intent.support_refs) == 2
    assert set(first.intent.support_refs) <= set(second.intent.support_refs)
    assert root.achievement and first.achievement and not second.achievement
    assert first.planning_state is second.planning_state is TurnPlanningState.EXHAUSTED
    resumed = [r for r in case.models.inner.calls if r.stage == "stage_continuation" and r.context.feedback_refs]
    assert [r.context.turn.turn_id for r in resumed] == [first.turn_id, root.turn_id]
    for request in resumed:
        original = case.app.store.load_turn(case.app.task_id, request.context.turn.turn_id)
        assert request.context.turn.achievement == original.achievement
        assert request.messages  # Retained parent planning history, not a fresh substitute session.
        assert all(i.identity == "reviewed_feedback_not_fact" for i in request.context.history if i.ref in request.context.feedback_refs)
    child_roads = [r for r in case.models.inner.calls if r.stage == "stage_roads" and r.context.turn.turn_id == first.turn_id]
    assert child_roads[0].context.session == PlanningSession("turn_planning", first.turn_id)
    assert case.app.state.task_run.status.value == "running"
    count = len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)
    reopen(case)
    assert asyncio.run(case.app.run(case.token)).reason == result.reason
    assert (len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)) == count


@pytest.mark.parametrize("mode", ["retained", "new"])
def test_resumed_achieved_parent_can_execute_selected_or_new_road(case, mode):
    install_chain(case, spare=mode == "retained", expand=mode == "new")
    result = asyncio.run(case.app.run(case.token))
    root = result.turn
    assert len(root.roads) == 2 and all(r.status is IntentStatus.SUCCEEDED for r in root.roads)
    assert root.achievement and len(case.tools.calls) == 4
    assert len({r.road_id for t in case.app.store.list_stage_turns(case.app.task_id) for r in t.roads}) == 4
    assert len([i for i in case.app.evidence.history(case.app.task_id) if i.record_type == "verified_fact"]) == 3


def test_known_settled_blockage_returns_with_suspended_road_and_no_fake_failure(case):
    install_chain(case, blocked=True)
    result = asyncio.run(case.app.run(case.token))
    root, child = case.app.store.list_stage_turns(case.app.task_id)
    assert result.turn == root and child.roads[0].status is IntentStatus.SUSPENDED
    assert child.planning_state is TurnPlanningState.ACTIVE and child.achievement is None
    assert len(case.tools.calls) == 2 and root.achievement
    request = next(r for r in case.models.inner.calls if r.stage == "stage_continuation" and r.context.feedback_refs)
    assert request.context.turn.turn_id == root.turn_id
    assert any('"kind": "return_to_parent"' in i.content for i in request.context.history if i.ref in request.context.feedback_refs)


@pytest.mark.parametrize("boundary", ["register", "feedback", "new_road_start", "after_commit"])
def test_chain_restart_resumes_committed_scope_without_replaying_execution(case, monkeypatch, boundary):
    install_chain(case, expand=boundary == "new_road_start")
    original = case.app._commit
    fired = []
    def interrupt(change):
        match = (boundary in {"register", "after_commit"} and any(t.parent_turn_id for t in change.turns)
            or boundary == "feedback" and any(t.planning_state is TurnPlanningState.EXHAUSTED for t in change.turns)
            or boundary == "new_road_start" and any(t.achievement and len(t.roads) == 2
                and any(r.status is IntentStatus.RUNNING for r in t.roads) for t in change.turns))
        if match and not fired:
            fired.append(True)
            if boundary == "after_commit":
                original(change)
            raise RuntimeError("controlled chain loss")
        return original(change)
    monkeypatch.setattr(case.app, "_commit", interrupt)
    with pytest.raises(RuntimeError, match="controlled chain loss"):
        asyncio.run(case.app.run(case.token))
    calls = len(case.tools.calls)
    assert fired
    reopen(case)
    result = asyncio.run(case.app.run(case.token))
    assert result.reason == "stage.final_validation_requested"
    assert len(case.tools.calls) == (4 if boundary == "new_road_start" else 3)
    assert len(case.tools.calls) >= calls
    roots = [t for t in case.app.store.list_stage_turns(case.app.task_id) if t.parent_turn_id is None]
    assert len(roots) == 1 and active_turn(case.app.store.list_stage_turns(case.app.task_id)) == roots[0]
    proposals = [r.model_request_id for r in case.models.inner.calls]
    assert len(proposals) == len(set(proposals))
    reviews = [r.model_request_id for r in case.models.outer.calls]
    assert len(reviews) == len(set(reviews))


def test_unknown_child_tool_never_returns_to_parent_or_replays_on_restart(case):
    install_chain(case)
    inner = case.models.inner.transform
    def lose(request, raw):
        value = inner(request, raw)
        if request.context.turn and request.context.turn.intent.description == "child1" and value.get("kind") == "mcp_call":
            case.tools.error = RuntimeError("controlled unknown child execution")
        return value
    case.models.inner.transform = lose
    with pytest.raises(ExecutionRecoveryRequired, match="mcp.outcome_unknown"):
        asyncio.run(case.app.run(case.token))
    root, child = case.app.store.list_stage_turns(case.app.task_id)
    assert root.planning_state is TurnPlanningState.WAITING_CHILD and child.roads[0].status is IntentStatus.RUNNING
    calls = len(case.tools.calls), len(case.models.inner.calls), len(case.models.outer.calls)
    reopen(case)
    case.tools.error = None
    with pytest.raises(OperationRecoveryRequired, match="operation_outcome_unknown"):
        asyncio.run(case.app.run(case.token))
    assert (len(case.tools.calls), len(case.models.inner.calls), len(case.models.outer.calls)) == calls
    assert not any(r.context.feedback_refs for r in case.models.inner.calls)


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_child_approval_after_stop_never_becomes_formal_or_dispatches_more_tools(case, monkeypatch, stop):
    install_chain(case)
    call = case.app.models.call
    deadline = case.app.state.task_run.deadline_at
    stopped_calls = []
    async def stopped(request, token):
        result = await call(request, token)
        if isinstance(request.proposal, TurnIntent) and request.proposal.description == "child1":
            stopped_calls.append(len(case.tools.calls))
            if stop == "cancel":
                case.token.is_cancelled = True
            else:
                case.now.value = deadline
        return result
    monkeypatch.setattr(case.app.models, "call", stopped)
    with pytest.raises(OperationRecoveryRequired, match="dispatch_stopped"):
        asyncio.run(case.app.run(case.token))
    root, child = case.app.store.list_stage_turns(case.app.task_id)
    from agent_core.stage_plans import status
    assert root.achievement and child.achievement is None
    assert status(case.app.store, case.app.task_id, PlanningSession("turn_planning", child.turn_id)) == "pending"
    assert stopped_calls == [len(case.tools.calls)]


def test_continuation_exhaustion_keeps_pending_candidates_and_historical_achievement(case):
    install_chain(case, spare=True)
    inner = case.models.inner.transform
    def exhaust(request, raw):
        if request.stage == "stage_continuation" and request.context.turn.parent_turn_id is None and request.context.feedback_refs:
            return {"kind": "exhausted", "reason": "the retained method cannot solve the follow-up problem", "support_refs": list(request.context.feedback_refs)}
        return inner(request, raw)
    case.models.inner.transform = exhaust
    result = asyncio.run(case.app.run(case.token))
    assert result.reason == "stage.exhausted" and result.turn.achievement
    assert [r.status for r in result.turn.roads] == [IntentStatus.SUCCEEDED, IntentStatus.PENDING]
    assert len(case.tools.calls) == 3
    count = len(case.models.inner.calls), len(case.models.outer.calls)
    assert asyncio.run(case.app.run(case.token)).reason == result.reason
    assert (len(case.models.inner.calls), len(case.models.outer.calls)) == count


def test_rejected_final_handoff_returns_feedback_to_original_planning_session(case):
    rejected = []
    def review(request, raw):
        if isinstance(request.proposal, StagePlanningDecision) and request.proposal.kind == "request_final_validation" and not rejected:
            rejected.append(request)
            return {"decision": "revise", "reason": "make the handoff scope explicit"}
        return raw
    case.models.outer.transform = review
    result = asyncio.run(case.app.run(case.token))
    requests = [r for r in case.models.inner.calls if r.stage == "stage_continuation"]
    assert len(requests) == 2 and requests[0].context.session == requests[1].context.session
    assert "make the handoff scope explicit" in str(requests[1].messages)
    assert result.reason == "stage.final_validation_requested" and case.app.state.task_run.status.value == "running"


def test_waiting_parent_cannot_resume_without_reviewed_direct_child_feedback(case, monkeypatch):
    install_chain(case)
    request = case.app._request
    def interrupt(stage, session, **kwargs):
        turn = case.app.store.load_turn(case.app.task_id, session.owner_id)
        if stage == "stage_roads" and turn and turn.parent_turn_id:
            raise RuntimeError("controlled pause at child entry")
        return request(stage, session, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(case.app, "_request", interrupt)
        with pytest.raises(RuntimeError, match="controlled pause"):
            asyncio.run(case.app.run(case.token))
    root, child = case.app.store.list_stage_turns(case.app.task_id)
    with pytest.raises(StageStateError, match="child_feedback_not_ready"):
        case.app._commit(StageStateChange(turns=(replace(root, planning_state=TurnPlanningState.ACTIVE),)))
    assert active_turn(case.app.store.list_stage_turns(case.app.task_id)) == child
    refs = list(root.fact_refs)
    case.models.inner.transform = lambda request, raw: {"kind": "exhausted", "reason": "review this scoped exhaustion", "support_refs": refs}
    async def incomplete_return():
        _, _, binding = await case.app._review(case.app._request("stage_roads", PlanningSession("turn_planning", child.turn_id)), case.token)
        case.app._commit(StageStateChange(turns=(replace(child, planning_state=TurnPlanningState.EXHAUSTED),), reviews=(binding,)))
    with pytest.raises(StageStateError, match="child_feedback_not_atomic"):
        asyncio.run(incomplete_return())
    assert active_turn(case.app.store.list_stage_turns(case.app.task_id)) == child


def test_known_final_handoff_review_is_reused_after_process_loss(case, monkeypatch):
    commit = case.app._commit
    def interrupt(change):
        if any(isinstance(case.app.evidence.request(b.review_operation_id).proposal, StagePlanningDecision)
                and case.app.evidence.request(b.review_operation_id).proposal.kind == "request_final_validation" for b in change.reviews):
            raise RuntimeError("controlled loss after handoff approval")
        return commit(change)
    monkeypatch.setattr(case.app, "_commit", interrupt)
    with pytest.raises(RuntimeError, match="controlled loss"):
        asyncio.run(case.app.run(case.token))
    calls = len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)
    reopen(case)
    assert asyncio.run(case.app.run(case.token)).reason == "stage.final_validation_requested"
    assert (len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)) == calls


def test_continuation_provider_payload_and_capacity_use_inner_role(case):
    from agent_adapters.deepseek_roles import build_payload, EstimatedCapacity, role_config, model_capacity_policy, TRANSPORT
    from agent_core.output_contracts import D8OutputRequest
    from agent_core.stage_planning import schema
    result = asyncio.run(case.app.run(case.token))
    inner, outer = role_config("inner_action", version=17), role_config("outer_planning", version=17)
    request = replace(case.app._request("stage_continuation", PlanningSession("turn_planning", result.turn.turn_id)),
        role_config_ref=inner.role_config_ref)
    envelope = D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": schema(request)}, "JSON only", None)
    payload = build_payload(envelope, inner)
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "max"
    assert "max_tokens" not in payload and payload["stream"] is True
    with pytest.raises(ValueError, match="model_role_binding_changed"):
        build_payload(envelope, outer)
    capacity = EstimatedCapacity(outer, inner)
    assert capacity.fits(envelope, model_capacity_policy(inner.model, TRANSPORT["base_url"]))
    assert capacity.last_measurement["stage"] == "stage_continuation"


@pytest.mark.parametrize("native", [False, True])
def test_proactive_stage_completion_closes_road_before_child_planning(case, native):
    from agent_core.stage_planning import model_contract
    case.models.inner.native = case.models.outer.native = native
    install_chain(case)
    original = case.models.inner.transform
    def boundary(request, raw):
        value = original(request, raw)
        turn = request.context.turn
        if request.stage == "stage_action" and value["kind"] == "road_resolution" and value["outcome"] == "succeeded":
            rules = model_contract(request)["rules"]
            if turn.achievement is None:
                assert "compare current formal Facts" in rules
                return dict(kind="turn_completion", reason="current criterion already has formal support", support_refs=value["support_refs"])
            assert "truthful closure" in rules and "resumed parent" in rules
        return value
    case.models.inner.transform = boundary
    result = asyncio.run(case.app.run(case.token))
    turns = case.app.store.list_stage_turns(case.app.task_id)
    assert len(turns) == 3 and result.reason == "stage.final_validation_requested"
    assert len(case.tools.calls) == 3
    for turn in turns[:2]:
        assert turn.achievement and all(r.status is IntentStatus.SUCCEEDED for r in turn.roads)
        calls = [q for q in case.models.inner.calls if q.context.session == PlanningSession("road_execution", turn.roads[0].road_id)]
        values = [case.app.evidence.read_model_result(case.app.store.load_d4_operation(q.model_request_id)).result for q in calls]
        from agent_core.stage_contracts import TurnCompletion, RoadResolution
        completed = next(i for i,v in enumerate(values) if isinstance(v,TurnCompletion))
        assert isinstance(values[completed+1],RoadResolution)
        child = next(t for t in turns if t.parent_turn_id == turn.turn_id)
        child_plans = [q for q in case.models.inner.calls if q.stage == "stage_roads" and q.context.turn.turn_id == child.turn_id]
        assert child_plans[0].context.session == PlanningSession("turn_planning", child.turn_id)
    assert case.app.state.task_run.status.value == "running"
