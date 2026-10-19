"""Frozen rolling Intent decisions and evidence-bound end reviews (D22)."""
from dataclasses import asdict, dataclass
import json

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.handoff_contracts import checked_refs, handoff_json
from agent_core.turn_modeling import digest

POLICY = "rolling_v1"


def enabled(config):
    return config.intent_execution_policy == POLICY


def packet(request):
    return json.loads(request.get("history_context", "{}") if isinstance(request, dict)
                      else getattr(request, "history_context", "{}") or "{}")


@dataclass(frozen=True, slots=True)
class IntentResolution:
    outcome: str
    reason: str
    evidence_refs: tuple[str, ...]
    fact_ids: tuple[str, ...]
    next_intent_candidates: tuple


def visible_items(request):
    context = packet(request)
    items = {i["ref"]: i for i in context.get("items", [])}
    for read in context.get("retrieved", []):
        for item in read["read"]["items"]:
            if "content" in item:
                items[item["ref"]] = item
    return items


def parse_resolution(raw, request):
    from agent_core.session_contracts import parse_next_intents
    if not isinstance(raw, dict) or set(raw) != {
            "kind", "outcome", "reason", "evidence_refs", "fact_ids", "next_intent_candidates"}:
        raise ValueError("d22_resolution_fields_invalid")
    if (raw["kind"] != "intent_resolution" or raw["outcome"] not in {
            "succeeded", "failed", "inconclusive", "review"}
            or not isinstance(raw["reason"], str) or not raw["reason"].strip()
            or "\x00" in raw["reason"]):
        raise ValueError("d22_resolution_value_invalid")
    refs = checked_refs(raw["evidence_refs"], set(raw["evidence_refs"]) if isinstance(raw["evidence_refs"], list) else set())
    ids = raw["fact_ids"]
    if (not refs or not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids) or raw["outcome"] == "succeeded" and not ids):
        raise ValueError("d22_resolution_support_required")
    suggestions = parse_next_intents(raw["next_intent_candidates"], set(visible_items(request)), maximum=None)
    return IntentResolution(raw["outcome"], raw["reason"], refs, tuple(ids), suggestions)


def resolution_schema(request):
    from agent_core.output_contracts import obj, array, text, S, NS
    context = packet(request)
    refs = [r for r, i in visible_items(request).items()
            if i["source_status"] == "available" and not i.get("conflict")]
    source = {"type": "string", "enum": refs}
    facts = [i["fact_id"] for i in context["intent_execution"]["facts"]]
    suggestion = obj({"description": S, "intent_type": NS,
        "dependency_refs": {**array(source), "uniqueItems": True}, "reason": text(512),
        "evidence_refs": {**array(source, 1, 8), "uniqueItems": True}})
    choices = [(["failed", "inconclusive", "review"], 0)]
    allowed = context["execution_feasibility"]["allowed_action_kinds"]
    if facts and "complete_intent" in allowed:
        choices.insert(0, (["succeeded"], 1))
    if context["intent_execution"].get("stop_requested"):
        choices = [(["failed", "inconclusive"], 0)]
    return {"anyOf": [obj({"kind": {"type": "string", "const": "intent_resolution"},
        "outcome": {"type": "string", "enum": outcomes}, "reason": text(),
        "evidence_refs": {**array(source, 1), "uniqueItems": True},
        "fact_ids": {**array({"type": "string", "enum": facts}, minimum), "uniqueItems": True},
        "next_intent_candidates": array(suggestion)}) for outcomes, minimum in choices]}


def parse_saved_resolution(app, request_id, raw):
    saved = app.sessions.evidence.read("request", request_id)
    if saved is None:
        raise D3RecoveryRequired("d22_resolution_request_missing")
    return parse_resolution(raw, saved["request"])


def facts(app, intent_id):
    return [dict(fact_id=i.record_id, content=i.content, ref=i.ref)
            for i in app.handoff.history() if i.intent_id == intent_id and i.record_type == "verified_fact"
            and i.source_status == "available" and not i.conflict
            and (app.revisions is None or app.revisions.reliable(i))]


def validate_resolution(app, request, proposal):
    from agent_core.dual_llm_modeling import D3ProposalRejected
    if packet(request)["intent_execution"].get("stop_requested") and proposal.outcome not in {"failed", "inconclusive"}:
        raise D3ProposalRejected("d22_strategy_stop_summary_required")
    available = visible_items(request)
    selected = checked_refs(list(proposal.evidence_refs), set(available))
    if any(available[r]["source_status"] != "available" or available[r].get("conflict") for r in selected):
        raise D3ProposalRejected("d22_resolution_source_unavailable")
    candidate_refs = {r for c in proposal.next_intent_candidates
                      for r in (*c.evidence_refs, *c.dependency_refs)}
    live = {i.ref: i for i in app.handoff.history()}
    for ref in candidate_refs:
        item = available.get(ref)
        current_item = live.get(ref)
        if (item is None or item["source_status"] != "available" or item.get("conflict")
                or current_item is None or current_item.source_status != "available" or current_item.conflict
                or app.revisions is not None and not app.revisions.reliable(current_item)):
            raise D3ProposalRejected("d22_resolution_source_unavailable")
    frozen = {i["fact_id"]: i["ref"] for i in packet(request)["intent_execution"]["facts"]}
    intent_id = request["context"]["intent_id"] if isinstance(request, dict) else request.context.intent_id
    current = {i["fact_id"]: i["ref"] for i in facts(app, intent_id)}
    if any(i not in frozen or frozen[i] != current.get(i) or frozen[i] not in selected for i in proposal.fact_ids):
        raise D3ProposalRejected("d22_resolution_fact_not_effective")
    if proposal.outcome == "succeeded":
        intent = next(i for i in app.state.intents if i.intent_id == intent_id)
        app.validator._allowed(app.state.task_run.task_run_id, intent.turn_id, intent_id, "complete_intent", None)
    for fact_id in proposal.fact_ids:
        supports = [r for r in app.store.list_d8_conclusions(app.state.task_run.task_run_id) if r.fact_id == fact_id]
        if not supports:
            raise D3RecoveryRequired("d22_resolution_fact_source_missing")
        support = supports[-1]
        binding = app.store.load_d3_plan_binding(support.plan_id)
        result = app.store.load_d3_plan_result(support.plan_id)
        if result is None or result.supporting_fact_id != fact_id:
            raise D3RecoveryRequired("d22_resolution_fact_result_changed")
        app.conclusions.check_support(app.state, binding, result.proof_id, fact_id)


def pending_stop(app, intent_id):
    reviews = [r for r in app.store.list_d3_review_applications(app.state.task_run.task_run_id)
               if r.intent_id == intent_id and not r.paused_gate
               and r.decision in {"switch_sibling_intent", "abandon_current_intent"}]
    if not reviews:
        return None
    review = reviews[-1]
    for record in app.store.list_d8_tree_records(app.state.task_run.task_run_id):
        if record.intent_id != intent_id or not record.inner_request_id:
            continue
        saved = app.sessions.evidence.read("request", record.inner_request_id)
        context = packet(saved["request"]).get("intent_execution", {})
        if (context.get("stop_requested") or {}).get("review_id") == review.review_id:
            return None
    _, _, reviewed = app.validator._review_call(app.state.task_run.task_run_id, review.review_id)
    return {"review_id": review.review_id, "decision": review.decision, "reason": reviewed.rationale}


def execution_context(app, intent_id):
    return {"policy": POLICY, "facts": [{"fact_id": f["fact_id"], "ref": f["ref"]}
                                      for f in facts(app, intent_id)],
        "stop_requested": pending_stop(app, intent_id),
        "goal_rule": "A local goal with observable completion criteria and explicit scope, not a predetermined action list.",
        "next_decision": "Continue with a Plan/tool, request a review, or propose an evidence-bound Intent resolution.",
        "stop_rule": "No new Fact and repeated responses do not prove saturation. Negative observations may increase coverage; prerequisites may enable later progress.",
        "reason_rule": "For an end/review proposal summarize reason, tested scope, unresolved issues and optional conditions for revisiting; this is not a verified Fact.",
        "evidence_rule": "Cite exact visible refs; use history reads for missing details. An open candidate space is not disproved by a few misses."}


def make_request(app, intent_id, inner_id, proposal):
    from agent_core.handoff_contracts import D8HandoffRequest
    frozen = app.sessions.evidence.read("request", inner_id)
    if frozen is None:
        raise D3RecoveryRequired("d22_resolution_request_missing")
    validate_resolution(app, frozen["request"], proposal)
    intent = next(i for i in app.state.intents if i.intent_id == intent_id)
    chosen, _, context = app.handoff.select("execution", intent.turn_id, intent_id)
    by_ref = {i.ref: i for i in app.handoff.history()}
    required = set(context["required_refs"]) | set(proposal.evidence_refs)
    all_items = {i.ref: i for i in chosen}
    for ref in required:
        if ref not in by_ref or by_ref[ref].source_status != "available" or by_ref[ref].conflict:
            raise D3RecoveryRequired("d22_resolution_source_changed")
        all_items[ref] = by_ref[ref]
    selected = tuple(all_items.values())
    fact_refs = [next(i.ref for i in selected if i.record_id == fid) for fid in proposal.fact_ids]
    policy = dict(version=2, policy=POLICY, intent_id=intent_id,
        local_goal=intent.description, inner_request_id=inner_id, proposal=asdict(proposal),
        goal_ref=next(i.ref for i in selected if i.record_id == intent_id),
        support_fact_ref=fact_refs[0] if fact_refs else None, support_fact_refs=fact_refs,
        required_observation_refs=list(proposal.evidence_refs),
        history_sha256=digest([asdict(i) for i in selected]),
        goal_interpretation="Judge evidence coverage of the local goal. Insufficient evidence normally means continue. Only a bounded complete test domain supports a universal negative. Strategy closure is not proof of impossibility.")
    context.update(items=[i.visible() for i in selected], required_refs=sorted(required), s4_evaluation_policy=policy)
    version = app.handoff.capacity()
    identity = app._id("resolution/" + inner_id)
    saved = app.handoff.evidence.read("request", identity)
    if saved is not None:
        return D8HandoffRequest.from_dict(saved)
    return D8HandoffRequest(identity, app.state.task_run.task_run_id, intent.turn_id, intent_id,
        "intent_evaluation", app.config.outer_role.role_config_ref, app.state.latest_checkpoint.sequence,
        version.version, version.capacity, app.redactor.redact(app.state.task_run.request.goal),
        selected, handoff_json(context), proposal.fact_ids[0] if proposal.fact_ids else None)


def evaluation_policy(request, policy):
    by_ref = {i.ref: i for i in request.history}
    required = [policy["goal_ref"], *policy["support_fact_refs"], *policy["required_observation_refs"]]
    if (policy["policy"] != POLICY or policy["intent_id"] != request.intent_id
            or policy["history_sha256"] != digest([asdict(i) for i in request.history])
            or any(r not in by_ref or by_ref[r].source_status != "available" or by_ref[r].conflict for r in required)
            or by_ref[policy["goal_ref"]].record_id != request.intent_id
            or json.loads(by_ref[policy["goal_ref"]].content)["goal"] != policy["local_goal"]
            or [by_ref[r].record_id for r in policy["support_fact_refs"]] != policy["proposal"]["fact_ids"]):
        raise ValueError("d22_resolution_policy_changed")
    return policy


def evaluation_schema(request, policy):
    from agent_core.output_contracts import obj, array, text
    refs = [i.ref for i in request.history if i.source_status == "available" and not i.conflict]
    decisions = ["continue", "failed"]
    if policy["support_fact_refs"]:
        decisions.append("succeeded")
    if policy["proposal"]["outcome"] in {"inconclusive", "review"}:
        decisions.append("inconclusive")
    return obj({"decision": {"type": "string", "enum": decisions}, "reason": text(),
        "evidence_refs": {**array({"type": "string", "enum": refs}, 1), "uniqueItems": True}})


def parse_evaluation(request, raw, policy):
    from agent_core.output_contracts import decode_response
    parsed, issues, terminal, _ = decode_response(raw, evaluation_schema(request, policy))
    if issues or terminal or not parsed["reason"].strip() or "\x00" in parsed["reason"]:
        raise ValueError("d22_resolution_review_invalid")
    if parsed["decision"] == "succeeded" and not set(policy["support_fact_refs"]) <= set(parsed["evidence_refs"]):
        raise ValueError("d22_resolution_success_support_required")
    def observed(item):
        if item.record_type == "verified_fact" or item.identity == "saved_execution_observation_not_verified_fact":
            return True
        if item.identity == "historical_decision_not_verified":
            return bool(json.loads(item.content).get("observation", {}).get("mcp_tool_result"))
        return False
    if parsed["decision"] == "failed" and not any(
            i.ref in parsed["evidence_refs"] and observed(i) for i in request.history):
        raise ValueError("d22_resolution_negative_observation_required")
    return parsed


def close_stop_observation(app, intent_id):
    if pending_stop(app, intent_id) is None:
        return
    running = [p for p in app.state.plans if p.intent_id == intent_id and p.status.value == "running"]
    if not running:
        return
    plan = running[-1]
    binding = app.store.load_d3_plan_binding(plan.plan_id)
    if getattr(binding, "kind", None) == "http_request":
        proof = app.web.observation(binding.execution_id)
    else:
        proof = app.proof_store.read(app._id("proof/" + plan.plan_id))
    if proof is None:
        raise D3RecoveryRequired("d22_stop_observation_unavailable")
    complete_observation(app, binding, proof)


def complete_observation(app, binding, proof, conclusion=None):
    from agent_core.dual_llm_contracts import D3PlanResult
    source = app.conclusions.observation(proof.proof_id)
    evidence = (source.source_version_sha256 if getattr(binding, "kind", None) == "http_request"
                else digest(asdict(app.workspace_evidence.read_digest(source.observation_id))))
    result = D3PlanResult(binding.task_run_id, binding.plan_id, "observed", binding.artifact_version_id,
                         source.observation_id, source.proof_id, evidence)
    app.control.complete_plan(binding, result, conclusion=conclusion)


async def resolve(app, request_id, intent_id, proposal, token):
    from agent_core.control_store import UpsertIntent
    from agent_core.domain import IntentStatus
    from agent_core.handoff_contracts import D8TreeRecord
    request = make_request(app, intent_id, request_id, proposal)
    final, decision = await app.handoff.call(request, token)
    if decision is None:
        raise D3RecoveryRequired("d22_resolution_review_unavailable")
    intent = next(i for i in app.state.intents if i.intent_id == intent_id)
    status = decision["decision"]
    record = D8TreeRecord(app._id("evaluation/" + final.model_request_id), request.task_run_id,
        intent.turn_id, intent_id, "evaluation_continued" if status == "continue" else status,
        final.model_request_id, request.support_fact_id, request_id)
    change = UpsertIntent(intent_id, intent.turn_id, intent.description, intent.priority,
        intent.intent_type, IntentStatus.RUNNING if status == "continue" else IntentStatus(status))
    app.handoff._commit("evaluate_d8_tree_intent", record, (change,))
    app.store.mark_d3_operation_applied(request_id, record.record_id)
    app.sessions.reconcile()
    from qinyuan_core import get_logger
    get_logger("ctrl").info("intent.resolution", run=request.task_run_id, intent=intent_id,
        request=final.model_request_id, outcome=status, category="business_progress" if status == "continue" else "business_result")


def validate_record(app, request, record):
    policy = evaluation_policy(request, json.loads(request.prior_summary)["s4_evaluation_policy"])
    inner = app.validator._known_model(record.task_run_id, record.inner_request_id, "inner_plan")
    if (not enabled(app.config) or not isinstance(inner, IntentResolution)
            or record.inner_request_id != policy["inner_request_id"]
            or handoff_json(asdict(inner)) != handoff_json(policy["proposal"])):
        raise D3RecoveryRequired("d22_resolution_proposal_changed")
    frozen = app.sessions.evidence.read("request", record.inner_request_id)
    validate_resolution(app, frozen["request"], inner)


def reconcile(app):
    """Finish an inner application marker after a committed end review; never redispatch."""
    for record in app.store.list_d8_tree_records(app.state.task_run.task_run_id):
        if not record.inner_request_id:
            continue
        operation = app.store.load_d3_operation(record.inner_request_id)
        if operation is None:
            raise D3RecoveryRequired("d22_resolution_operation_missing")
        if operation.application_ref is not None:
            if operation.application_ref != record.record_id:
                raise D3RecoveryRequired("d22_resolution_application_changed")
            continue
        raw = app.handoff.evidence.read("request", record.model_request_id)
        if json.loads(raw["prior_summary"]).get("s4_evaluation_policy", {}).get("version") != 2:
            continue
        from agent_core.handoff_contracts import D8HandoffRequest
        validate_record(app, D8HandoffRequest.from_dict(raw), record)
        app.store.mark_d3_operation_applied(record.inner_request_id, record.record_id)


def result_summary(app, record):
    request = app.handoff.evidence.read("request", record.model_request_id)
    if request is None:
        raise D3RecoveryRequired("d22_resolution_request_missing")
    policy = json.loads(request["prior_summary"]).get("s4_evaluation_policy", {})
    if policy.get("policy") != POLICY:
        return None
    from agent_core.handoff_contracts import D8HandoffRequest
    result = app.handoff._known_call(D8HandoffRequest.from_dict(request))
    if result is None or result["outcome"] != "valid":
        raise D3RecoveryRequired("d22_resolution_result_missing")
    return dict(kind=record.kind, inner_summary=policy["proposal"]["reason"],
        reviewed_summary=result["raw"]["reason"], evidence_refs=result["raw"]["evidence_refs"],
        identity="reviewed_intent_result_not_verified_fact", scope="conditions_and_tested_domain_at_decision_time")
