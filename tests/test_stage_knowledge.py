"""U4 scoped views and revisions: production storage/IO, controlled model/tool transport."""

import asyncio
from dataclasses import replace

import pytest

from test_stage_application import case
from agent_adapters.stage_application import StageApplication
from agent_core.conclusion_revision_contracts import D8ChangeDecision
from agent_core.domain import IntentStatus
from agent_core.stage_contracts import (
    PlanningSession, RoadSnapshot, StageConclusionChange, StageFactProposal,
    StageStateChange, StageTurnSnapshot, TurnAchievement,
    TurnPlanningState,
)
from agent_core.stage_storage import StageStateError
from agent_core.turn_modeling import stable_id


def formal(case):
    return tuple(i for i in case.app.evidence.history(case.app.task_id) if i.record_type == "verified_fact")


def two_roads(case):
    def inner(request, raw):
        history = request.context.history
        facts = [i for i in history if i.record_type == "verified_fact"]
        if request.stage == "stage_intent":
            return {"kind": "turn_intent", "description": "compare two independently scoped observations",
                "completion_criterion": "obtain a formal observation from each of two Roads", "support_refs": []}
        if request.stage == "stage_roads" and not request.context.turn.roads:
            return {"kind": "roads", "roads": [{"description": name, "support_refs": []} for name in ("first route", "second route")]}
        if request.stage == "stage_completion":
            return {"kind": "turn_completion" if len(facts) == 2 else "incomplete",
                "reason": "check both independent Road results", "support_refs": [i.ref for i in facts]}
        if request.stage != "stage_action":
            return raw
        own = request.context.session.owner_id
        observations = [i.ref for i in history if i.record_type == "progress" and i.intent_id == own]
        own_facts = [i.ref for i in facts if i.intent_id == own]
        if not observations:
            return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf observed"}}
        if not own_facts:
            road = next(r for r in request.context.turn.roads if r.road_id == own)
            return {"kind": "fact_candidate", "content": road.candidate.description + " returned observed.",
                "reason": "original result of this Road", "support_refs": observations, "inferred": False}
        selected = tuple(i.ref for i in facts)
        if request.context.turn.fact_refs != selected:
            return {"kind": "select_facts", "support_refs": list(selected)}
        return {"kind": "road_resolution", "outcome": "succeeded", "reason": "own observation complete", "support_refs": own_facts}
    case.models.inner.transform = inner
    case.models.outer.transform = lambda request, raw: ({**raw, "accepted_road_indices": list(range(len(request.proposal)))}
        if isinstance(request.proposal, tuple) else raw)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    return result.turn, formal(case)


def revision_reviewer(case, *, supported=True, decision="accept"):
    def outer(request, raw):
        if not isinstance(request.proposal, StageConclusionChange):
            return raw
        result = {"decision": decision, "reason": "review the exact references within original scope"}
        if request.proposal.operation == "replace_support":
            result.update(goal_supported=supported, scope_explanation="the replacement supports the unchanged stage criterion")
        if request.proposal.operation == "resolve_conflict" and request.proposal.resolution == "different_scope":
            result["scope_explanation"] = "the recorded scopes differ"
        return result
    case.models.outer.transform = outer


def apply_knowledge(case, session, wire):
    previous = case.models.inner.transform
    case.models.inner.transform = lambda request, raw: wire
    async def perform():
        request = case.app._request("stage_roads" if session.scope == "turn_planning" else "stage_action", session)
        identity, value = await case.app.models.call(request, case.token)
        assert await case.app._knowledge_action(request, identity, value, case.token)
        return identity
    try:
        return asyncio.run(perform())
    finally:
        case.models.inner.transform = previous


def child(case, parent, premise):
    previous = case.models.inner.transform
    case.models.inner.transform = lambda request, raw: {"kind": "turn_intent", "description": "use the existing observations",
        "completion_criterion": "the original observations support this follow-up", "support_refs": [premise]}
    async def register():
        proposal, review, binding = await case.app._review(case.app._request("stage_intent",
            PlanningSession("turn_planning", parent.turn_id)), case.token)
        assert review.decision == "accept"
        turn = StageTurnSnapshot(stable_id(case.app.task_id, "test/child"), proposal, TurnPlanningState.ACTIVE, parent.turn_id)
        case.app._commit(StageStateChange(turns=(replace(parent, planning_state=TurnPlanningState.WAITING_CHILD), turn), reviews=(binding,)))
        return turn
    try:
        return asyncio.run(register())
    finally:
        case.models.inner.transform = previous


def pending_road(case, turn):
    previous = case.models.inner.transform
    case.models.inner.transform = lambda request, raw: {"kind": "roads", "roads": [{"description": "follow the fixed premise", "support_refs": []}]}
    async def register():
        proposal, _, binding = await case.app._review(case.app._request("stage_roads",
            PlanningSession("turn_planning", turn.turn_id)), case.token)
        updated = replace(turn, roads=(RoadSnapshot(stable_id(case.app.task_id, "test/child-road"), proposal[0], IntentStatus.PENDING),))
        case.app._commit(StageStateChange(turns=(updated,), reviews=(binding,)))
        return updated
    try:
        return asyncio.run(register())
    finally:
        case.models.inner.transform = previous


def start_road(case, turn):
    updated = replace(turn, roads=(replace(turn.roads[0], status=IntentStatus.RUNNING),))
    case.app._commit(StageStateChange(turns=(updated,)))
    return updated


def test_two_roads_share_formal_references_without_reauditing_selection(case):
    turn, facts = two_roads(case)
    assert len(facts) == 2 and len(case.tools.calls) == 2
    assert len([r for r in case.models.outer.calls if isinstance(r.proposal, StageFactProposal)]) == 2
    assert set(turn.achievement.completion.support_refs) == {i.ref for i in facts}
    view = case.app.evidence.knowledge(case.app.task_id).turn_facts(turn.turn_id)
    assert {i.record_id for i in view} == {i.record_id for i in facts}
    assert {i.source_version for i in view} == {i.source_version for i in facts}
    assert all(not isinstance(r.proposal, tuple) or r.proposal for r in case.models.outer.calls)
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_ancestor_view_is_optional_and_selection_does_not_copy_fact(case):
    parent, facts = two_roads(case)
    descendant = child(case, parent, facts[0].ref)
    knowledge = case.app.evidence.knowledge(case.app.task_id)
    assert not knowledge.turn_facts(descendant.turn_id)
    assert {i.ref for i in knowledge.turn_facts(descendant.turn_id, include_ancestors=True)} == {i.ref for i in facts}
    count = len(case.models.outer.calls)
    apply_knowledge(case, PlanningSession("turn_planning", descendant.turn_id), {"kind": "select_facts", "support_refs": [facts[0].ref]})
    assert len(case.models.outer.calls) == count
    selected = case.app.evidence.knowledge(case.app.task_id).turn_facts(descendant.turn_id)
    assert len(selected) == 1 and selected[0].record_id == facts[0].record_id and selected[0].turn_id == parent.turn_id
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2


def test_correction_requires_explicit_reviewed_premise_replacement(case):
    parent, facts = two_roads(case)
    descendant = child(case, parent, facts[0].ref)
    revision_reviewer(case)
    session = PlanningSession("turn_planning", descendant.turn_id)
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "correct",
        "target_refs": [facts[0].ref], "replacement_ref": facts[1].ref, "reason": "adopt the independent replacement"})
    changed = case.app.store.read_history(case.app.task_id, (facts[0].ref,))[0]
    assert changed.current_status == "superseded" and changed.effective_ref == facts[1].ref
    descendant = pending_road(case, descendant)
    with pytest.raises(StageStateError, match="source_not_effective"):
        start_road(case, descendant)
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "replace_support",
        "target_refs": [facts[0].ref], "replacement_ref": facts[1].ref, "relation_kind": "turn_premise",
        "reason": "the reviewed alternative supports the same fixed criterion"})
    original = case.app.store.load_turn(case.app.task_id, descendant.turn_id)
    assert original.intent.support_refs == (facts[0].ref,)
    assert case.app.evidence.knowledge(case.app.task_id).effective_premises(original) == (facts[1].ref,)
    assert start_road(case, original).roads[0].status is IntentStatus.RUNNING
    assert case.app.store.load_turn(case.app.task_id, parent.turn_id).achievement == parent.achievement
    assert len(formal(case)) == 2


def test_withdrawal_is_visible_even_after_view_deselection(case):
    parent, facts = two_roads(case)
    descendant = child(case, parent, facts[0].ref)
    revision_reviewer(case)
    session = PlanningSession("turn_planning", descendant.turn_id)
    apply_knowledge(case, session, {"kind": "select_facts", "support_refs": [facts[0].ref]})
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "withdraw", "target_refs": [facts[0].ref], "reason": "withdraw this claim"})
    apply_knowledge(case, session, {"kind": "select_facts", "support_refs": []})
    request = case.app._request("stage_roads", session)
    item = next(i for i in request.context.history if i.ref == facts[0].ref)
    assert item.current_status == "withdrawn" and item.source_status == "available"
    assert case.app.store.load_turn(case.app.task_id, parent.turn_id).achievement == parent.achievement


def test_missing_original_observation_marks_views_unavailable_without_replay(case):
    parent, facts = two_roads(case)
    descendant = child(case, parent, facts[0].ref)
    source = case.app.store._connection.execute("SELECT source_ref FROM facts WHERE fact_id=?", (facts[0].record_id,)).fetchone()[0]
    case.app.evidence.records._path("stage-mcp-result", source).unlink()
    items = case.app.store.read_history(case.app.task_id, (facts[0].ref, facts[1].ref))
    assert items[0].source_status == "unavailable" and items[0].current_status == "unavailable"
    assert items[1].current_status == "effective"
    descendant = pending_road(case, descendant)
    with pytest.raises(StageStateError, match="source_not_effective"):
        start_road(case, descendant)
    assert len(case.tools.calls) == 2


def test_synthesis_is_reviewed_inference_and_never_formal_success_support(case):
    parent, facts = two_roads(case)
    session = PlanningSession("turn_planning", parent.turn_id)
    identity = apply_knowledge(case, session, {"kind": "fact_candidate", "content": "Both observations suggest a follow-up hypothesis.",
        "reason": "a new combination, not another observed result", "support_refs": [i.ref for i in facts], "inferred": True})
    inference = next(i for i in case.app.evidence.history(case.app.task_id) if i.record_id == identity)
    assert inference.record_type == "reviewed_inference" and len(formal(case)) == 2
    descendant = child(case, parent, facts[0].ref)
    session = PlanningSession("turn_planning", descendant.turn_id)
    apply_knowledge(case, session, {"kind": "history_details", "refs": [inference.ref]})
    previous = case.models.inner.transform
    case.models.inner.transform = lambda request, raw: {"kind": "turn_completion", "reason": "try the inference", "support_refs": [inference.ref]}
    async def complete():
        request = case.app._request("stage_completion", session)
        proposal, _, binding = await case.app._review(request, case.token)
        updated = replace(descendant, achievement=TurnAchievement(proposal, binding.review_operation_id), fact_refs=(inference.ref,))
        case.app._commit(StageStateChange(turns=(updated,), reviews=(binding,)))
    try:
        with pytest.raises(StageStateError, match="formal_fact_required"):
            asyncio.run(complete())
    finally:
        case.models.inner.transform = previous
    assert case.app.store.load_turn(case.app.task_id, descendant.turn_id).achievement is None


def test_explicit_source_withdrawal_invalidates_dependent_conclusion(case):
    parent, facts = two_roads(case)
    first = facts[0]
    session = PlanningSession("road_execution", first.intent_id)
    observation = next(i for i in case.app.evidence.history(case.app.task_id)
        if i.record_type == "progress" and i.intent_id == first.intent_id and not i.record_id.startswith("revision/"))
    apply_knowledge(case, session, {"kind": "fact_candidate", "content": "A dependent observed conclusion.",
        "reason": "both explicit supports are required", "support_refs": [first.ref, observation.ref], "inferred": False})
    dependent = next(i for i in formal(case) if i.record_id not in {f.record_id for f in facts})
    revision_reviewer(case)
    apply_knowledge(case, PlanningSession("turn_planning", parent.turn_id), {"kind": "conclusion_change", "operation": "withdraw",
        "target_refs": [first.ref], "reason": "the declared support is withdrawn"})
    after = case.app.store.read_history(case.app.task_id, (dependent.ref,))[0]
    assert after.current_status == "unavailable" and after.source_status == "available"
    assert case.app.store._connection.execute("SELECT status FROM facts WHERE fact_id=?", (dependent.record_id,)).fetchone()[0] == "verified"
    assert case.app.store.load_turn(case.app.task_id, parent.turn_id).achievement == parent.achievement


def test_conflict_and_explicit_resolution_preserve_original_fact_identity(case):
    parent, facts = two_roads(case)
    revision_reviewer(case)
    session = PlanningSession("turn_planning", parent.turn_id)
    targets = [i.ref for i in facts]
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "report_conflict", "target_refs": targets,
        "reason": "review this explicit pair"})
    assert all(i.conflict and i.current_status == "conflicted" for i in case.app.store.read_history(case.app.task_id, tuple(targets)))
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "resolve_conflict", "target_refs": targets,
        "resolution": "different_scope", "reason": "the review distinguishes the recorded scopes"})
    after = case.app.store.read_history(case.app.task_id, tuple(targets))
    assert all(not i.conflict and i.current_status == "effective" for i in after)
    assert {i.record_id for i in after} == {i.record_id for i in facts}
    assert {i.source_version for i in after} == {i.source_version for i in facts}


def test_replacement_must_support_unchanged_goal(case):
    parent, facts = two_roads(case)
    descendant = child(case, parent, facts[0].ref)
    revision_reviewer(case, supported=False)
    with pytest.raises(StageStateError, match="replacement_goal_not_supported"):
        apply_knowledge(case, PlanningSession("turn_planning", descendant.turn_id), {"kind": "conclusion_change", "operation": "replace_support",
            "target_refs": [facts[0].ref], "replacement_ref": facts[1].ref, "relation_kind": "turn_premise", "reason": "proposed substitution"})
    assert not case.app.store.list_d8_revisions(case.app.task_id)
    current = case.app.store.load_turn(case.app.task_id, descendant.turn_id)
    assert current.intent == descendant.intent
    assert case.app.evidence.knowledge(case.app.task_id).effective_premises(current) == (facts[0].ref,)


def test_verified_link_does_not_promote_inference_and_corrected_inference_is_readable(case):
    parent, facts = two_roads(case)
    session = PlanningSession("turn_planning", parent.turn_id)
    identity = apply_knowledge(case, session, {"kind": "fact_candidate", "content": "A combined hypothesis.", "reason": "derive a hypothesis",
        "support_refs": [i.ref for i in facts], "inferred": True})
    inference = next(i for i in case.app.evidence.history(case.app.task_id) if i.record_id == identity)
    revision_reviewer(case)
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "link_verified", "target_refs": [inference.ref],
        "replacement_ref": facts[0].ref, "reason": "attach an explicit formal validation link"})
    linked = case.app.store.read_history(case.app.task_id, (inference.ref,))[0]
    assert linked.record_type == "reviewed_inference" and linked.validated_by_refs == (facts[0].ref,)
    apply_knowledge(case, session, {"kind": "conclusion_change", "operation": "correct", "target_refs": [inference.ref],
        "content": "A corrected combined hypothesis.", "evidence_refs": [facts[1].ref], "reason": "revise only the inference wording"})
    old = case.app.store.read_history(case.app.task_id, (inference.ref,))[0]
    corrected = case.app.store.read_history(case.app.task_id, (old.effective_ref,))[0]
    assert corrected.record_type == "reviewed_inference" and old.current_status == "superseded"
    second = apply_knowledge(case, session, {"kind": "fact_candidate", "content": "A subsequent inference with an exact revision source.",
        "reason": "use the corrected source", "support_refs": [corrected.ref], "inferred": True})
    assert next(i for i in case.app.evidence.history(case.app.task_id) if i.record_id == second).source_refs == (corrected.ref,)
    assert len(formal(case)) == 2


def test_live_ancestor_view_does_not_rewrite_child_selection_or_premise(case):
    parent, facts = two_roads(case)
    descendant = child(case, parent, facts[0].ref)
    before = case.app.evidence.knowledge(case.app.task_id).turn_facts(descendant.turn_id, include_ancestors=True)
    assert len(before) == 2
    count = len(case.models.outer.calls)
    apply_knowledge(case, PlanningSession("turn_planning", parent.turn_id), {"kind": "select_facts", "support_refs": []})
    assert len(case.models.outer.calls) == count
    assert not case.app.evidence.knowledge(case.app.task_id).turn_facts(descendant.turn_id, include_ancestors=True)
    current = case.app.store.load_turn(case.app.task_id, descendant.turn_id)
    assert current.fact_refs == () and current.intent == descendant.intent


def test_running_road_only_gets_tools_after_explicit_premise_repair(case):
    from agent_core.stage_planning import schema
    parent, facts = two_roads(case)
    descendant = start_road(case, pending_road(case, child(case, parent, facts[0].ref)))
    revision_reviewer(case)
    planning = PlanningSession("turn_planning", descendant.turn_id)
    execution = PlanningSession("road_execution", descendant.roads[0].road_id)
    apply_knowledge(case, planning, {"kind": "conclusion_change", "operation": "withdraw", "target_refs": [facts[0].ref], "reason": "withdraw premise"})
    def tools_visible():
        request = case.app._request("stage_action", execution)
        shape = schema(request)
        return any(branch.get("properties", {}).get("kind", {}).get("const") == "mcp_call" for branch in shape.get("anyOf", [shape]))
    assert not tools_visible()
    apply_knowledge(case, planning, {"kind": "conclusion_change", "operation": "replace_support", "target_refs": [facts[0].ref],
        "replacement_ref": facts[1].ref, "relation_kind": "turn_premise", "reason": "approved independent premise"})
    assert tools_visible()
    assert case.app.store.load_turn(case.app.task_id, descendant.turn_id).intent == descendant.intent


def test_formal_fact_text_cannot_be_rewritten_as_a_reviewed_inference(case):
    parent, facts = two_roads(case)
    revision_reviewer(case)
    count = len(case.models.outer.calls)
    with pytest.raises(StageStateError, match="model_output_unusable"):
        apply_knowledge(case, PlanningSession("turn_planning", parent.turn_id), {"kind": "conclusion_change", "operation": "correct",
            "target_refs": [facts[0].ref], "content": "a rewritten assertion", "evidence_refs": [facts[1].ref], "reason": "try rewriting"})
    assert len(case.models.outer.calls) == count
    assert not case.app.store.list_d8_revisions(case.app.task_id)
    assert {i.ref for i in formal(case)} == {i.ref for i in facts}


@pytest.mark.parametrize("boundary", ["read_commit", "revision_commit", "revision_transaction"])
def test_known_knowledge_result_resumes_without_model_or_tool_replay(case, monkeypatch, boundary):
    import agent_core.handoff_storage as storage
    parent, facts = two_roads(case)
    revision_reviewer(case)
    session = PlanningSession("turn_planning", parent.turn_id)
    wire = ({"kind": "history_details", "refs": [facts[0].ref]} if boundary == "read_commit" else
        {"kind": "conclusion_change", "operation": "withdraw", "target_refs": [facts[0].ref], "reason": "reviewed withdrawal"})
    checkpoint = case.app.state.latest_checkpoint.sequence
    with monkeypatch.context() as patch:
        if boundary == "revision_transaction":
            original = storage.save_handoff_mutation
            def interrupt(store, mutation, sequence):
                original(store, mutation, sequence)
                raise RuntimeError("controlled loss after revision insertion")
            patch.setattr(storage, "save_handoff_mutation", interrupt)
        else:
            patch.setattr(case.app, "_commit", lambda change: (_ for _ in ()).throw(RuntimeError("controlled loss before commit")))
        with pytest.raises(RuntimeError, match="controlled loss"):
            apply_knowledge(case, session, wire)
    assert case.app.state.latest_checkpoint.sequence == checkpoint
    assert not case.app.store.list_d8_revisions(case.app.task_id)
    assert case.app.store.read_history(case.app.task_id, (facts[0].ref,))[0].current_status == "effective"
    calls = len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    identity = apply_knowledge(case, session, wire)
    assert (len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)) == calls
    assert case.app.store.load_d4_operation(identity).application_ref is not None
    if boundary == "read_commit":
        assert case.app.evidence.read_history_result(case.app.store.load_d4_operation(identity))[0].ref == facts[0].ref
    else:
        assert len(case.app.store.list_d8_revisions(task_id)) == 1
        assert case.app.store.read_history(task_id, (facts[0].ref,))[0].current_status == "withdrawn"


def test_stale_revision_target_cannot_reuse_frozen_head(case):
    from agent_core.stage_planning import parse, frozen_history
    parent, facts = two_roads(case)
    session = PlanningSession("turn_planning", parent.turn_id)
    request = case.app._request("stage_roads", session)
    wire = {"kind": "conclusion_change", "operation": "withdraw", "target_refs": [facts[0].ref], "reason": "original head"}
    stale = parse(request, wire)
    revision_reviewer(case)
    apply_knowledge(case, session, wire)
    with pytest.raises(StageStateError, match="revision_head_changed"):
        case.app.evidence.knowledge(case.app.task_id).validate_change(stale, D8ChangeDecision("accept", "old approval", None, None),
            session, frozen_history(request))
    assert len(case.app.store.list_d8_revisions(case.app.task_id)) == 1


def test_rejected_change_is_traceable_and_does_not_change_current_fact(case):
    parent, facts = two_roads(case)
    revision_reviewer(case, decision="reject")
    apply_knowledge(case, PlanningSession("turn_planning", parent.turn_id), {"kind": "conclusion_change", "operation": "withdraw",
        "target_refs": [facts[0].ref], "reason": "a rejected proposal"})
    records = case.app.store.list_d8_revisions(case.app.task_id)
    assert len(records) == 1 and records[0].decision == "reject"
    assert case.app.store.read_history(case.app.task_id, (facts[0].ref,))[0].current_status == "effective"
    assert case.app.store.load_turn(case.app.task_id, parent.turn_id).achievement == parent.achievement
