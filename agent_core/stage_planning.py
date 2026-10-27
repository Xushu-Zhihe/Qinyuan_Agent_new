"""New-stage model requests and output boundaries; no provider or tool IO."""

from dataclasses import dataclass

from agent_core.actions import MCPCall
from agent_core.domain import IntentStatus
from agent_core.conclusion_revision_contracts import D8ChangeDecision, D8RevisionTarget
from agent_core.output_contracts import array, obj, tagged, text, decode_response
from agent_core.stage_contracts import (
    PlanningExhausted, PlanningSession, RoadCandidate, RoadResolution, RoadSnapshot,
    StageConclusionChange, StageFactSelection, StageHistoryRead, StageFactProposal, StageIncomplete, StageSessionSummary, StageStrategyAdvice, StageTurnSnapshot, TurnAchievement, TurnCompletion, TurnIntent,
    TurnPlanningContext, TurnPlanningProposal, TurnPlanningReview, TurnPlanningState, StagePlanningDecision,
)
from agent_core.handoff_contracts import D8HistoryItem


@dataclass(frozen=True, slots=True)
class StageModelRequest:
    model_request_id: str
    task_run_id: str
    stage: str
    role_config_ref: str
    base_checkpoint_sequence: int
    context: TurnPlanningContext
    messages: tuple[dict, ...] = ()
    tools: tuple[dict, ...] = ()
    proposal_operation_id: str | None = None
    proposal: TurnPlanningProposal | StageFactProposal | None = None
    correction_of_model_request_id: str | None = None
    summary_coverage: tuple[tuple[str, str], ...] = ()
    capacity_recovery_of: str | None = None
    frozen_contract_ref: str | None = None
    history_catalog: tuple[dict, ...] = ()


RULES = {
    "stage_continuation": "Plan from this achieved Turn in its retained Inner session. Propose only the next Turn's goal/criterion/premises; its own Inner will plan its Roads. Alternatively select an existing pending own Road, propose own Roads retaining all pending candidates, report known settled blockage, or request independent final validation. Exhaustion here concerns available continuation, not historical achievement or unexecuted Road failure. Child feedback is scoped evidence, not proof that the parent goal failed. Never replay closed Roads or unknown operations.",
    "stage_intent": "Propose the current stage deliverable and its completion criterion, within TaskRun goal and authority. Known premises are optional; never invent future evidence. This root proposal belongs to its preallocated planning session.",
    "stage_roads": "Propose this Turn's independent alternative solution chains, in execution priority order. Do not propose another stage's Roads. A different tool, URL or parameter alone is not a new chain. Retain all existing pending candidates when updating their order; closed Roads keep their identities and outcomes. Business re-exploration with new premises creates a new Road. No fixed candidate count. Known settled blockage can be reviewed with return_to_parent; technical waiting alone is not business exhaustion.",
    "stage_review": "Review the exact bound proposal: accept, revise or reject. Explain substantive revisions to its original Inner; do not silently replace it. For Roads select original indices in execution priority order. Stage completion must satisfy the immutable completion_criterion; Road success and TaskRun success are separate. A Fact candidate must follow from its cited original observations in its declared scope; inference is not verified Fact.",
    "stage_action": "Explore only this Road in its own continuous conversation. Tools return observations, never automatic Facts. Propose a concise formal conclusion only when useful, with original support refs; label inference. Road resolution concerns its own result. Stage completion requires the Turn criterion and current formal support. Use only declared tools and current authority. Tool errors are not target counterevidence; unknown operations are never replayed.",
    "stage_completion": "Check this Turn's original completion criterion after Road review. Propose completion only with actual current formal support, or explain why it remains incomplete. Do not add stronger criteria or treat a single Road success as automatic stage completion.",
    "stage_supervision": "Provide read-only strategy advice using current observations and alternatives. Return continue_current_intent or request_intent_resolution with reason and support_refs. Advice never changes a Road result, stage goal, authorization or TaskRun success.",
    "stage_summary": "Summarize only the supplied covered conversation prefix. Preserve the original goal and criterion, observations, hypotheses and uncertainty, negative findings, review feedback and open issues. Keep references and distinguish formal Fact from inference. This is an unverified conversation index, never new evidence, permission or success. Do not imply an omitted check never happened. Coverage is fixed by trusted metadata; do not invent it.",
}
KNOWLEDGE_GUIDANCE = (
    " Select existing formal Facts with select_facts when useful; selecting references creates no new claim or review. "
    "Turn.Fact is a curated reference view, not a copy of knowledge. Do not create another Fact for an unchanged claim. "
    "Use history_details for exact same-task catalog refs; file/tool authority is separate. Known unavailable, withdrawn, "
    "superseded or conflicted premises cannot support execution or completion. effective_support_refs records only "
    "explicit reviewed replacements; never silently substitute a new version or overwrite a fixed goal. "
    "A new combined assertion requires fact_candidate review. A formal Fact still requires own-Road original observation; "
    "reasoning from other Facts is reviewed inference, not verified Fact. Stage completion cites actual effective formal "
    "Facts and explains their combination against the original criterion. A completed/failed Road does not erase its valid Facts. "
    "Conclusion changes are append-only and reviewed. target_refs select the frozen logical revision heads shown in "
    "context/catalog; trusted code supplies their root/version IDs. Correcting text never creates a formal Fact. "
    "Archived messages and summaries describe their recorded time; current status and effective_support_refs govern now. "
)
INNER_STAGES = {"stage_intent", "stage_roads", "stage_action", "stage_completion", "stage_continuation"}


def model_contract(request):
    return {"schema": schema(request), "rules": RULES[request.stage] + (KNOWLEDGE_GUIDANCE if request.stage in {"stage_roads", "stage_action", "stage_review", "stage_completion", "stage_continuation"} else "")
            + (" Final validation requests must include an exact candidate found in the cited original tool observation. "
               "Outer approval is not independent validation. Rejected or partial validation feedback returns to this original planning session; "
               "preserve historical stage achievement and improve the proposal using actual evidence. Unknown submission is never resubmitted."
               if request.context.goal_validation is not None else ""),
        "instruction": "Return one complete JSON object matching the output contract. No Markdown or extra fields."
            + (" This is the only format correction; preserve the original decision scope."
               if request.correction_of_model_request_id else "")}


def schema(request):
    if request.stage == "stage_summary":
        return obj({"content": text()})
    ref = text()
    visible = [item.ref for item in request.context.history]
    refs = {**array({**ref, "enum": visible}, maximum=8), "uniqueItems": True}
    supported = {"reason": text(512), "support_refs": refs}
    exhausted = tagged("exhausted", supported)
    completion = tagged("turn_completion", supported)
    feedback = tagged("return_to_parent", supported)
    if request.stage == "stage_intent":
        return {"anyOf": [tagged("turn_intent", {"description": text(), "completion_criterion": text(),
            "support_refs": refs}), exhausted]}
    if request.stage in {"stage_roads", "stage_continuation"}:
        choices = [tagged("roads", {"roads": array(obj({"description": text(),
            "support_refs": refs}), minimum=1)}), exhausted, feedback, *knowledge_actions(request)]
        if request.stage == "stage_continuation":
            validation = supported
            if request.context.goal_validation is not None:
                validation = {**supported, "candidate": text(16384),
                    "source_ref": {"type": "string", "enum": [i.ref for i in request.context.history
                        if i.record_type == "progress" and i.identity == "tool_observation_not_fact" and i.source_status == "available"]}}
            choices += [tagged("turn_intent", {"description": text(), "completion_criterion": text(), "support_refs": refs}),
                tagged("request_final_validation", validation), tagged("select_road", {**supported,
                    "road_id": {"type": "string", "enum": [r.road_id for r in request.context.turn.roads if r.status is IntentStatus.PENDING]}})]
        return {"anyOf": choices}
    if request.stage == "stage_review":
        if isinstance(request.proposal, (StageFactSelection, StageHistoryRead)):
            raise ValueError("stage.pure_reference_review_forbidden")
        if isinstance(request.proposal, StageConclusionChange):
            properties = {"decision": {"type": "string", "enum": ["accept", "reject", "insufficient"]}, "reason": text(512)}
            if request.proposal.operation == "replace_support":
                properties.update(goal_supported={"type": "boolean"}, scope_explanation=text(512))
            elif request.proposal.operation == "resolve_conflict" and request.proposal.resolution == "different_scope":
                properties["scope_explanation"] = text(512)
            return obj(properties)
        return obj({"decision": {"type": "string", "enum": ["accept", "revise", "reject"]},
            **supported, "accepted_road_indices": {**array({"type": "integer", "minimum": 0}), "uniqueItems": True}},
            optional=("support_refs", "accepted_road_indices"))
    if request.stage == "stage_completion":
        return {"anyOf": [completion, tagged("incomplete", supported)]}
    if request.stage == "stage_supervision":
        return obj({"decision": {"type": "string", "enum": ["continue_current_intent", "request_intent_resolution"]}, **supported})
    if request.stage != "stage_action":
        raise ValueError("stage.model_stage_invalid")
    from agent_core.conclusion_revisions import ConclusionRevisions
    by_ref = {item.ref: item for item in request.context.history}
    ready = all(ConclusionRevisions.reliable(by_ref.get(ref)) for ref in request.context.effective_support_refs)
    calls = [tagged("mcp_call", {"tool_name": {"type": "string", "const": tool["qualified_name"]},
        "arguments": tool["input_schema"]}) for tool in request.tools] if ready else []
    resolution = tagged("road_resolution", {"outcome": {"type": "string", "enum": [
        "succeeded", "failed", "inconclusive", "abandoned", "skipped"]}, **supported})
    if request.context.turn is not None and request.context.turn.achievement is not None:
        return {"anyOf": [*calls, resolution, feedback, *knowledge_actions(request)]}
    return {"anyOf": [*calls, completion,
        resolution, feedback, *knowledge_actions(request)]}


def knowledge_actions(request):
    if request.context.turn is None:
        return []
    entries = {i["ref"]: i for i in request.history_catalog}
    entries.update({i.ref: i.visible(include_content=False) for i in request.context.history})
    reference = lambda refs: {"type": "string", "enum": list(refs)}
    formal = [ref for ref, i in entries.items() if i["record_type"] == "verified_fact"
        and i.get("current_status", "effective") == "effective" and not i["conflict"] and i["source_status"] == "available"]
    conclusions = [ref for ref, i in entries.items() if i["record_type"] in {"verified_fact", "reviewed_inference"}]
    refs = {**array(reference(i.ref for i in request.context.history), maximum=8), "uniqueItems": True}
    branches = [tagged("select_facts", {"support_refs": {**array(reference(formal)), "uniqueItems": True}}),
        tagged("history_details", {"refs": {**array(reference(entries), minimum=1, maximum=2), "uniqueItems": True}})]
    sources = [i.ref for i in request.context.history if i.record_type in {"verified_fact", "reviewed_inference"}
               or i.record_type == "progress" and not i.record_id.startswith("revision/")]
    inferred = {"type": "boolean"}
    if request.context.session.scope == "turn_planning":
        inferred["const"] = True
    branches.append(tagged("fact_candidate", {"content": text(256), "reason": text(512),
        "support_refs": {**array(reference(sources), minimum=1, maximum=8), "uniqueItems": True}, "inferred": inferred}))
    for operation in ("correct", "withdraw", "restore", "report_conflict", "resolve_conflict", "replace_support", "link_verified"):
        properties = {"operation": {"type": "string", "const": operation}, "target_refs": {**array(reference(conclusions),
            minimum=2 if operation in {"report_conflict", "resolve_conflict"} else 1,
            maximum=2 if operation in {"report_conflict", "resolve_conflict"} else 1), "uniqueItems": True}, "reason": text(512)}
        variants = [{}]
        if operation in {"correct", "restore"}:
            variants = [{"replacement_ref": reference(conclusions)}, {"content": text(256), "evidence_refs": refs}]
        elif operation == "link_verified":
            properties["replacement_ref"] = reference(formal)
        elif operation == "replace_support":
            properties["replacement_ref"] = reference(formal)
            road = {"relation_kind": {"type": "string", "const": "road_premise"}}
            if request.context.session.scope == "turn_planning":
                road["road_id"] = {"type": "string", "enum": [r.road_id for r in request.context.turn.roads]}
            variants = [{"relation_kind": {"type": "string", "const": "turn_premise"}}, road]
        elif operation == "resolve_conflict":
            properties["resolution"] = {"type": "string", "enum": ["keep_first", "keep_second", "withdraw_both", "unresolved", "different_scope"]}
        for extra in variants:
            branch = tagged("conclusion_change", {**properties, **extra})
            if "content" in extra:
                branch["properties"]["target_refs"] = array(reference(ref for ref, i in entries.items()
                    if i["record_type"] == "reviewed_inference"), minimum=1, maximum=1)
            branches.append(branch)
    return branches


def frozen_history(request):
    result = {i.ref: i for i in request.context.history}
    for entry in request.history_catalog:
        if entry["ref"] not in result:
            result[entry["ref"]] = D8HistoryItem.from_dict({**{k: v for k, v in entry.items() if k != "ref"}, "content": ""})
    return tuple(result.values())


def parse(request, raw):
    value, issues, terminal, _ = decode_response(raw, schema(request))
    if issues or terminal:
        raise ValueError("stage.output_invalid")
    def checked(item, key):
        result = item[key]
        if not result.strip() or "\x00" in result:
            raise ValueError("stage.output_text_invalid")
        return result
    refs = tuple(value.get("support_refs", ()))
    if request.stage == "stage_summary":
        return StageSessionSummary(checked(value, "content"))
    if request.stage == "stage_review":
        if isinstance(request.proposal, StageConclusionChange):
            return D8ChangeDecision(value["decision"], checked(value, "reason"), value.get("goal_supported"), value.get("scope_explanation"))
        indices = tuple(value.get("accepted_road_indices", ()))
        if isinstance(request.proposal, tuple) and value["decision"] == "accept":
            if not indices or any(i >= len(request.proposal) for i in indices):
                raise ValueError("stage.road_selection_invalid")
        elif indices:
            raise ValueError("stage.road_selection_invalid")
        return TurnPlanningReview(value["decision"], checked(value, "reason"), refs, indices)
    if request.stage == "stage_supervision":
        return StageStrategyAdvice(value["decision"], checked(value, "reason"), refs)
    kind = value["kind"]
    if kind in {"select_road", "return_to_parent", "request_final_validation"}:
        return StagePlanningDecision(kind, checked(value, "reason"), refs, value.get("road_id"), value.get("candidate"), value.get("source_ref"))
    if kind == "select_facts":
        return StageFactSelection(tuple(value["support_refs"]))
    if kind == "history_details":
        return StageHistoryRead(tuple(value["refs"]))
    if kind == "conclusion_change":
        by_ref = {i.ref: i for i in frozen_history(request)}
        targets = tuple(D8RevisionTarget(getattr(by_ref[ref], "logical_root_id", by_ref[ref].record_id),
            getattr(by_ref[ref], "revision", 1), getattr(by_ref[ref], "last_version_ref", None) or ref) for ref in value["target_refs"])
        relation = value.get("relation_kind")
        owner = None
        if relation == "turn_premise":
            owner = request.context.turn.turn_id
        elif relation == "road_premise":
            owner = value.get("road_id") or (request.context.session.owner_id if request.context.session.scope == "road_execution" else None)
        return StageConclusionChange(value["operation"], targets, value.get("replacement_ref"), value.get("content"),
            tuple(value.get("evidence_refs", ())), checked(value, "reason"), value.get("resolution"), relation, owner)
    if kind == "incomplete":
        return StageIncomplete(checked(value, "reason"), refs)
    if kind == "exhausted":
        return PlanningExhausted(checked(value, "reason"), refs)
    if kind == "turn_intent":
        return TurnIntent(checked(value, "description"), checked(value, "completion_criterion"), refs)
    if kind == "roads":
        return tuple(RoadCandidate(checked(item, "description"), tuple(item["support_refs"])) for item in value["roads"])
    if kind == "turn_completion":
        return TurnCompletion(checked(value, "reason"), refs)
    if kind == "road_resolution":
        return RoadResolution(IntentStatus(value["outcome"]), checked(value, "reason"), refs)
    if kind == "fact_candidate":
        return StageFactProposal(checked(value, "content"), checked(value, "reason"), refs, value["inferred"])
    return MCPCall(value["tool_name"], value["arguments"])


def request_from_dict(raw, proposal_kind):
    """Rebuild the exact frozen input, including its historical Turn projection."""
    data = dict(raw)
    context = dict(data["context"])
    context["session"] = PlanningSession(**context["session"])
    context["history"] = tuple(D8HistoryItem.from_dict(i) for i in context["history"])
    for field in ("feedback_refs", "pending_operation_ids", "effective_support_refs"):
        context[field] = tuple(context[field])
    turn = context["turn"]
    if turn is not None:
        turn = dict(turn)
        turn["intent"] = TurnIntent(**{**turn["intent"], "support_refs": tuple(turn["intent"]["support_refs"])})
        turn["planning_state"] = TurnPlanningState(turn["planning_state"])
        turn["roads"] = tuple(RoadSnapshot(r["road_id"], RoadCandidate(r["candidate"]["description"],
            tuple(r["candidate"]["support_refs"])), IntentStatus(r["status"])) for r in turn["roads"])
        turn["fact_refs"] = tuple(turn["fact_refs"])
        if turn["achievement"]:
            achievement = turn["achievement"]
            completion = achievement["completion"]
            turn["achievement"] = TurnAchievement(TurnCompletion(completion["reason"],
                tuple(completion["support_refs"])), achievement["review_operation_id"])
        context["turn"] = StageTurnSnapshot(**turn)
    data["context"] = TurnPlanningContext(**context)
    data["tools"] = tuple(data["tools"])
    data["messages"] = tuple(data["messages"])
    data["history_catalog"] = tuple(data["history_catalog"])
    data["summary_coverage"] = tuple(tuple(i) for i in data["summary_coverage"])
    proposal = data["proposal"]
    if proposal_kind == "tuple":
        data["proposal"] = tuple(RoadCandidate(p["description"], tuple(p["support_refs"])) for p in proposal)
    elif proposal is not None:
        if proposal_kind == "StageConclusionChange":
            data["proposal"] = StageConclusionChange.from_dict(proposal)
            return StageModelRequest(**data)
        kinds = {c.__name__: c for c in (TurnIntent, TurnCompletion, PlanningExhausted, RoadResolution, StageFactProposal, StagePlanningDecision)}
        proposal = {**proposal, "support_refs": tuple(proposal["support_refs"])}
        if proposal_kind == "RoadResolution":
            proposal["outcome"] = IntentStatus(proposal["outcome"])
        data["proposal"] = kinds[proposal_kind](**proposal)
    return StageModelRequest(**data)
