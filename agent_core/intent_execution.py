"""Frozen rolling Intent decisions and evidence-bound end reviews (D22)."""
from dataclasses import asdict, dataclass
import json

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.handoff_contracts import checked_refs, handoff_json
from agent_core.turn_modeling import digest

POLICY = "rolling_v1"


def enabled(config):
    return config.intent_execution_policy in {POLICY, "continuous_v1"}


def continuous(config):
    return config.intent_execution_policy == "continuous_v1"


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
    stop = context["intent_execution"].get("stop_requested")
    if stop:
        outcomes = ["failed", "inconclusive", "review"] if stop.get("review_latest_evidence") else ["failed", "inconclusive"]
        choices = [(outcomes, 0)] + ([(["succeeded"], 1)] if closure_only(request) and facts and "complete_intent" in allowed else [])
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


def continuation_scope(app, intent_id):
    """Ignore bookkeeping from repeated proposals; retain actual material and authority."""
    history = app.handoff.history()
    material = [(i.ref, i.source_status, i.conflict) for i in history if not i.record_id.startswith("tree/")
                and (not i.record_id.startswith("decision/") or i.identity == "unreviewed_working_record")]
    constraints = max((r.constraint_version for r in app.store.list_d3_review_applications(app.state.task_run.task_run_id)), default=0)
    return digest(dict(material=sorted(material), epoch=app.supervision.epoch(intent_id), constraints=constraints,
                       reads=app.handoff.read_count(intent_id)))


def last_continuation(app, intent_id):
    return next((r for r in reversed(app.store.list_d8_tree_records(app.state.task_run.task_run_id))
                 if r.intent_id == intent_id and r.kind == "evaluation_continued"), None)


def validate_continuation_reuse(app, request_id):
    """A receipt reuses an old continue; it is never a new model verdict."""
    identity = app._id("continuation-reuse/" + request_id)
    receipt = app.handoff.evidence.read("decision", identity)
    if receipt is None:
        return None
    frozen = app.sessions.evidence.read("request", request_id)
    marker = packet(frozen["request"]).get("intent_execution", {}).get("last_review", {}) if frozen else {}
    record = next((r for r in app.store.list_d8_tree_records(app.state.task_run.task_run_id)
                   if r.record_id == marker.get("record_id") and r.kind == "evaluation_continued"), None)
    if record is None:
        raise D3RecoveryRequired("d22_continuation_reuse_changed")
    from agent_core.handoff_contracts import D8HandoffRequest
    saved = app.handoff.evidence.read("request", record.model_request_id)
    if saved is None:
        raise D3RecoveryRequired("d22_continuation_reuse_changed")
    original = D8HandoffRequest.from_dict(saved)
    policy = evaluation_policy(original, json.loads(original.prior_summary)["s4_evaluation_policy"])
    decision = app.handoff._known_call(original)
    proposal = app.validator._known_model(record.task_run_id, request_id, "inner_plan")
    expected = dict(kind="reused_intent_continue", task_run_id=record.task_run_id, intent_id=record.intent_id,
                    model_request_id=request_id, review_record_id=record.record_id,
                    scope_sha256=marker.get("scope_sha256"))
    if (receipt != expected or frozen["intent_id"] != record.intent_id or frozen["turn_id"] != record.turn_id
            or not isinstance(proposal, IntentResolution)
            or handoff_json(asdict(proposal)) != handoff_json(policy["proposal"])
            or not marker.get("scope_sha256") or marker["scope_sha256"] != policy.get("continuation_scope_sha256")
            or decision is None or decision["outcome"] != "valid" or decision["raw"]["decision"] != "continue"):
        raise D3RecoveryRequired("d22_continuation_reuse_changed")
    return dict(reused=True, review_record_id=record.record_id, review_request_id=record.model_request_id,
                reason=decision["raw"]["reason"])


def reuse_continuation(app, request_id, intent_id, proposal):
    if not continuous(app.config):
        return False
    identity = app._id("continuation-reuse/" + request_id)
    if app.handoff.evidence.read("decision", identity) is None:
        frozen = app.sessions.evidence.read("request", request_id)
        if frozen is None:
            raise D3RecoveryRequired("d22_resolution_request_missing")
        context = packet(frozen["request"])["intent_execution"]
        marker = context.get("last_review")
        if not marker or stop_request(app, intent_id) is not None:
            return False
        record = last_continuation(app, intent_id)
        if record is None or marker["record_id"] != record.record_id:
            return False
        original = app.handoff.evidence.read("request", record.model_request_id)
        policy = json.loads(original["prior_summary"])["s4_evaluation_policy"]
        if (handoff_json(asdict(proposal)) != handoff_json(policy["proposal"])
                or marker["scope_sha256"] != policy.get("continuation_scope_sha256")
                or marker["scope_sha256"] != continuation_scope(app, intent_id)):
            return False
        validate_resolution(app, frozen["request"], proposal)
        app.handoff.evidence.save("decision", identity, dict(kind="reused_intent_continue",
            task_run_id=record.task_run_id, intent_id=intent_id, model_request_id=request_id,
            review_record_id=record.record_id, scope_sha256=marker["scope_sha256"]))
    reviewed = validate_continuation_reuse(app, request_id)
    app._check_dispatch()
    app.store.mark_d3_operation_applied(request_id, identity)
    app.sessions.reconcile()
    from qinyuan_core import get_logger
    get_logger("ctrl").info("intent.resolution_reused", run=app.state.task_run.task_run_id,
        intent=intent_id, request=request_id, review=reviewed["review_request_id"],
        outcome="continue", category="review_reuse")
    return True


def validate_resolution(app, request, proposal):
    from agent_core.dual_llm_modeling import D3ProposalRejected
    stop = packet(request)["intent_execution"].get("stop_requested")
    if stop and not stop.get("review_latest_evidence") and not closure_only(request) and proposal.outcome not in {"failed", "inconclusive"}:
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
        if support.plan_id is None:
            app.evidence_conclusions.validate_fact(next(f for f in app.state.facts if f.fact_id==fact_id),app.state)
            continue
        binding = app.store.load_d3_plan_binding(support.plan_id)
        result = app.store.load_d3_plan_result(support.plan_id)
        if result is None or result.supporting_fact_id != fact_id:
            raise D3RecoveryRequired("d22_resolution_fact_result_changed")
        app.conclusions.check_support(app.state, binding, result.proof_id, fact_id)


def pending_stop(app, intent_id):
    supervisor = getattr(app, "supervision", None)
    stop = supervisor.stop(intent_id) if supervisor else None
    if stop is not None:
        return stop
    reviews = [r for r in app.store.list_d3_review_applications(app.state.task_run.task_run_id)
               if r.intent_id == intent_id and not r.paused_gate
               and r.decision in {"switch_sibling_intent", "abandon_current_intent", "request_intent_resolution"}]
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
    notes = []
    strategy = []
    if continuous(app.config):
        for row in app.store.list_d8_tree_records(app.state.task_run.task_run_id):
            if row.intent_id == intent_id and row.kind == "suspended":
                note = app.handoff.evidence.read("pause", row.record_id)
                if note is not None:notes.append(note)
            if row.intent_id == intent_id and row.kind == "strategy_yielded":
                strategy.append(app.handoff.evidence.read("strategy-yield", row.record_id))
    from agent_core.intent_planning import LOCAL_GOAL_GUIDANCE
    previous = last_continuation(app, intent_id) if continuous(app.config) else None
    return {**({"last_review": {"record_id": previous.record_id, "scope_sha256": continuation_scope(app, intent_id)}} if previous else {}),**({"technical_history": notes, "revisit_count": visits(app.store, app.state.task_run.task_run_id, intent_id)} if notes else {}), "policy": POLICY, "review_contract": "candidate_subset_v1", "facts": [{"fact_id": f["fact_id"], "ref": f["ref"]}
                                      for f in facts(app, intent_id)],
        "stop_requested": stop_request(app, intent_id),
        **({"strategy_history": strategy, "strategy_identity": "voluntary_yield_not_technical_retry_or_business_failure"} if strategy else {}),
        "goal_rule": LOCAL_GOAL_GUIDANCE,
        "next_decision": "Continue with a Plan/tool, request a review, or propose an evidence-bound Intent resolution.",
        "stop_rule": "No new Fact and repeated responses do not prove saturation. Negative observations may increase coverage; prerequisites may enable later progress.",
        "reason_rule": "For an end/review proposal summarize reason, tested scope, unresolved issues and optional conditions for revisiting; this is not a verified Fact.",
        "evidence_rule": "Cite exact visible refs; use history reads for missing details. Separate directly observed source text or measurements from causal/runtime hypotheses. An important narrow observation may be proposed with inferred=false and original evidence for normal outer review; derived or mixed unverified claims remain inferred=true. Never promote an accepted inference merely to obtain success support. An open candidate space is not disproved by a few misses.",
        "progress_rule": "Optionally save a concise working_record indexing important files/ranges already read, their original refs and reading limits. It is an unreviewed retrieval aid, never Fact evidence. Consult earlier read indexes and original results before re-reading. After continue address its concrete original-scope gap or explain a material deliverable change; repeating the identical request on unchanged material reuses that continue. Different wording alone is not new evidence; the supervisor assesses its value."}


def make_request(app, intent_id, inner_id, proposal):
    from agent_core.handoff_contracts import D8HandoffRequest
    frozen = app.sessions.evidence.read("request", inner_id)
    if frozen is None:
        raise D3RecoveryRequired("d22_resolution_request_missing")
    validate_resolution(app, frozen["request"], proposal)
    intent = next(i for i in app.state.intents if i.intent_id == intent_id)
    chosen, _, context = app.handoff.select("execution", intent.turn_id, intent_id)
    stop = packet(frozen["request"])["intent_execution"].get("stop_requested") or {}
    if not stop.get("review_latest_evidence"):
        # A completed end proposal may race with the background result; retain it
        # while still reviewing the outer reason and newest observations.
        stop = stop_request(app, intent_id) or stop
    review = stop if stop.get("review_latest_evidence") else None
    observations = list(dict.fromkeys((*proposal.evidence_refs, *(review["new_evidence_refs"] if review else ()))))
    by_ref = {i.ref: i for i in app.handoff.history()}
    required = set(context["required_refs"]) | set(observations)
    if review:
        required.update(review["evidence_refs"])  # Outer-only sibling comparison stays out of the inner view.
    all_items = {i.ref: i for i in chosen}
    for ref in required:
        if ref not in by_ref or by_ref[ref].source_status != "available" or by_ref[ref].conflict:
            raise D3RecoveryRequired("d22_resolution_source_changed")
        all_items[ref] = by_ref[ref]
    selected = tuple(all_items.values())
    fact_refs = [next(i.ref for i in selected if i.record_id == fid) for fid in proposal.fact_ids]
    policy = dict(version=3 if packet(frozen["request"])["intent_execution"].get("review_contract") == "candidate_subset_v1" else 2, policy=POLICY, intent_id=intent_id,
        local_goal=intent.description, inner_request_id=inner_id, proposal=asdict(proposal),
        goal_ref=next(i.ref for i in selected if i.record_id == intent_id),
        support_fact_ref=fact_refs[0] if fact_refs else None, support_fact_refs=fact_refs,
        required_observation_refs=observations,
        history_sha256=digest([asdict(i) for i in selected]),
        goal_interpretation=packet(frozen["request"])["intent_execution"].get("goal_rule", "") +
            " Judge evidence coverage of the local goal. Insufficient evidence normally means continue. Only a bounded complete test domain supports a universal negative. Strategy closure is not proof of impossibility.")
    if continuous(app.config):
        policy["continuation_scope_sha256"] = continuation_scope(app, intent_id)
    if review:
        policy["strategy_review"] = review
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


def observation_refs(request):
    def observed(item):
        if item.record_type == "verified_fact" or item.identity == "saved_execution_observation_not_verified_fact":
            return True
        if item.identity == "historical_decision_not_verified":
            return bool(json.loads(item.content).get("observation", {}).get("mcp_tool_result"))
        return False
    return [i.ref for i in request.history if i.source_status == "available" and not i.conflict and observed(i)]


def selected_fact_refs(policy, decision):
    if policy["version"] == 2:
        return policy["support_fact_refs"]
    return [r for r in policy["support_fact_refs"] if r in decision["evidence_refs"]]


def support_anchor(request, policy, decision):
    if policy["version"] == 2:
        return request.support_fact_id
    selected = selected_fact_refs(policy, decision)
    return next((i.record_id for r in selected for i in request.history if i.ref == r), None)


def evaluation_schema(request, policy):
    from agent_core.output_contracts import obj, array, text
    refs = [i.ref for i in request.history if i.source_status == "available" and not i.conflict]
    decisions = ["continue", "failed"]
    if policy["support_fact_refs"]:
        decisions.append("succeeded")
    if policy["proposal"]["outcome"] in {"inconclusive", "review"}:
        decisions.append("inconclusive")
    fields = {"reason": text(), "evidence_refs": {**array({"type": "string", "enum": refs}, 1), "uniqueItems": True}}
    if policy["version"] == 2:
        return obj({"decision": {"type": "string", "enum": decisions}, **fields})
    fields["reason"].update(minLength=1, pattern=r"^[^\x00]*[^\s\x00][^\x00]*$")
    branches = []
    for decision in decisions:
        required = policy["support_fact_refs"] if decision == "succeeded" else observation_refs(request) if decision == "failed" else None
        if required == []:
            continue
        evidence = dict(fields["evidence_refs"])
        if required is not None:
            evidence["contains"] = {"type": "string", "enum": required}
        branches.append(obj({"decision": {"type": "string", "const": decision}, **fields, "evidence_refs": evidence}))
    return {"anyOf": branches}


def parse_evaluation(request, raw, policy):
    from agent_core.output_contracts import decode_response
    schema = evaluation_schema(request, policy)
    if policy["version"] == 3:
        # Decode the common structure first so a direct parser rejection has the exact safe reason.
        from agent_core.output_contracts import obj, array, text
        refs = [i.ref for i in request.history if i.source_status == "available" and not i.conflict]
        schema = obj({"decision": {"type": "string", "enum": [b["properties"]["decision"]["const"] for b in schema["anyOf"]]},
            "reason": text(), "evidence_refs": {**array({"type": "string", "enum": refs}, 1), "uniqueItems": True}})
    parsed, issues, terminal, _ = decode_response(raw, schema)
    if issues or terminal or not parsed["reason"].strip() or "\x00" in parsed["reason"]:
        raise ValueError("d22_resolution_review_invalid")
    if parsed["decision"] == "succeeded" and (not selected_fact_refs(policy, parsed) if policy["version"] == 3 else not set(policy["support_fact_refs"]) <= set(parsed["evidence_refs"])):
        raise ValueError("d22_resolution_success_support_required")
    if parsed["decision"] == "failed" and not set(observation_refs(request)) & set(parsed["evidence_refs"]):
        raise ValueError("d22_resolution_negative_observation_required")
    return parsed


def close_stop_observation(app, intent_id):
    if stop_request(app, intent_id) is None:
        return
    running = [p for p in app.state.plans if p.intent_id == intent_id and p.status.value == "running"]
    if not running:
        return
    plan = running[-1]
    binding = app.store.load_d3_plan_binding(plan.plan_id)
    if getattr(binding, "kind", None) == "http_request":
        if (stop_request(app, intent_id)["decision"] == "request_intent_resolution"
                and app.store.load_d4_operation(binding.execution_id) is None):
            return  # Registered is not dispatched: preserve this Plan for a continued verdict.
        proof = app.web.observation(binding.execution_id)
    else:
        proof = app.proof_store.read(app._id("proof/" + plan.plan_id))
    if proof is None:
        if stop_request(app, intent_id)["decision"] == "request_intent_resolution":
            return  # Finish this already admitted bounded Plan; do not open another one.
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
    if reuse_continuation(app, request_id, intent_id, proposal):
        return
    request = make_request(app, intent_id, request_id, proposal)
    final, decision = await app.handoff.call(request, token)
    intent = next(i for i in app.state.intents if i.intent_id == intent_id)
    if decision is None:
        if json.loads(final.prior_summary)["s4_evaluation_policy"]["version"] == 3:
            from qinyuan_core import get_logger
            get_logger("ctrl").info("intent.resolution", run=request.task_run_id, intent=intent_id,
                request=final.model_request_id, outcome="awaiting_adjudication", category="technical_result")
            raise D3RecoveryRequired("d22_awaiting_adjudication")
        # The reviewer could not produce a readable verdict. The Intent is unjudged,
        # not the run state, so degrade locally and let the tree pick a sibling.
        if not app.handoff.note_evaluation_fallback("d22_resolution_review_invalid"):
            raise D3RecoveryRequired("d22_resolution_review_unavailable")
        record = D8TreeRecord(app._id("evaluation/" + final.model_request_id), request.task_run_id,
            intent.turn_id, intent_id, "inconclusive", final.model_request_id,
            request.support_fact_id, request_id)
        app.handoff._commit("evaluate_d8_tree_intent", record,
            (UpsertIntent(intent_id, intent.turn_id, intent.description, intent.priority,
                          intent.intent_type, IntentStatus.INCONCLUSIVE),))
        app.store.mark_d3_operation_applied(request_id, record.record_id)
        app.sessions.reconcile()
        from qinyuan_core import get_logger
        get_logger("ctrl").info("intent.resolution", run=request.task_run_id, intent=intent_id,
            request=final.model_request_id, outcome="inconclusive",
            category="business_result", reason="resolution_review_invalid")
        return
    app.handoff.note_evaluation_success()
    status = decision["decision"]
    record = D8TreeRecord(app._id("evaluation/" + final.model_request_id), request.task_run_id,
        intent.turn_id, intent_id, "evaluation_continued" if status == "continue" else status,
        final.model_request_id, support_anchor(final, json.loads(final.prior_summary)["s4_evaluation_policy"], decision), request_id)
    change = UpsertIntent(intent_id, intent.turn_id, intent.description, intent.priority,
        intent.intent_type, IntentStatus.RUNNING if status == "continue" else IntentStatus(status))
    app.handoff._commit("evaluate_d8_tree_intent", record, (change,))
    app.store.mark_d3_operation_applied(request_id, record.record_id)
    app.sessions.reconcile()
    from qinyuan_core import get_logger
    get_logger("ctrl").info("intent.resolution", run=request.task_run_id, intent=intent_id,
        request=final.model_request_id, outcome=status, category="business_progress" if status == "continue" else "business_result")
    await finish_budget_closure(app, token)


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
    if continuous(app.config):
        for operation in app.store.list_d3_operations(app.state.task_run.task_run_id):
            if operation.start.category != "inner_model" or operation.completion is None or operation.application_ref is not None:
                continue
            identity = app._id("continuation-reuse/" + operation.start.operation_id)
            if app.handoff.evidence.read("decision", identity) is not None:
                validate_continuation_reuse(app, operation.start.operation_id)
                app.store.mark_d3_operation_applied(operation.start.operation_id, identity)
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
        if json.loads(raw["prior_summary"]).get("s4_evaluation_policy", {}).get("version") not in {2, 3}:
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


EVALUATION_ISSUES = {
    "d22_resolution_success_support_required": ("$/evidence_refs", "at_least_one_candidate_fact"),
    "d22_resolution_negative_observation_required": ("$/evidence_refs", "observed_source_reference"),
    "d22_resolution_review_invalid": ("$/reason", "nonempty_valid_local_goal_decision"),
}


def pending_adjudication(store, evidence, output, task_id):
    """Derive a durable gate from completed review lineage; never dispatch or change state."""
    from agent_core.structured_output import format_stop_reason
    config = store.load_d3_run_config(task_id)
    handled = suspended_calls(store, task_id) if config is not None and continuous(config) else set()
    for op in store.list_d4_operations(task_id):
        if op.start.operation_id in handled:continue
        if op.completion is None or op.application_ref is not None or op.start.role != "outer_planning" or op.start.category != "review":
            continue
        request = evidence.read("request", op.start.operation_id)
        if request is None or request.get("stage") != "intent_evaluation":
            continue
        policy = json.loads(request["prior_summary"]).get("s4_evaluation_policy", {})
        if policy.get("version") != 3:
            continue
        inner = store.load_d3_operation(policy["inner_request_id"])
        if inner is None or inner.application_ref is not None:
            continue
        result = evidence.read("result", op.start.operation_id)
        if result is None or result["outcome"] == "valid":
            continue
        from agent_core.structured_output import transient_provider_result
        if (config is not None and continuous(config) and request.get("correction_of_model_request_id") is None
                and result["outcome"] == "model_error" and transient_provider_result(output, op.start.operation_id)):
            continue  # Its single known-ended correction must be admitted before parking.
        diagnostic = output.read("diagnostic", op.start.operation_id)
        if (request.get("correction_of_model_request_id") is not None or result["outcome"] != "invalid"
                or format_stop_reason(diagnostic) is not None):
            return {"reason": "d22_awaiting_adjudication", "intent_id": request["intent_id"],
                    "model_request_id": request["model_request_id"], "cause": result.get("reason") or result["outcome"]}
    return None


@dataclass(frozen=True, slots=True)
class WorkingRecord:
    content: str
    evidence_refs: tuple[str, ...]


def working_refs(request):
    """Availability is frozen in the request; original sources are checked again before application."""
    session = json.loads(getattr(request, "session_context", "{}") or "{}")
    if request.stage == "fact_proposal":
        return [request.observation.proof_id] if session.get("working_record_policy") == "unreviewed_v1" else None
    context = packet(request)
    if (context.get("intent_execution", {}).get("review_contract") != "candidate_subset_v1"
            or context["intent_execution"].get("stop_requested")):
        return None
    return [r for r, item in visible_items(request).items() if item["source_status"] == "available" and not item.get("conflict")]


def working_schema(request):
    from agent_core.output_contracts import tagged, text, array
    refs = working_refs(request)
    if not refs:
        return None
    return tagged("working_record", {"content": {**text(512), "minLength": 1, "pattern": r"^[^\x00]*[^\s\x00][^\x00]*$"},
        "evidence_refs": {**array({"type": "string", "enum": refs}, 1, 8), "uniqueItems": True}})


def parse_working(request, raw):
    if not isinstance(raw, dict) or raw.get("kind") != "working_record":
        return None
    from agent_core.output_contracts import decode_response
    schema = working_schema(request)
    if schema is None:
        raise ValueError("d22_working_record_unavailable")
    value, issues, terminal, _ = decode_response(raw, schema)
    if issues or terminal or not value["content"].strip() or "\x00" in value["content"]:
        raise ValueError("d22_working_record_invalid")
    return WorkingRecord(value["content"], tuple(value["evidence_refs"]))


def validate_working(app, request, note):
    if request.stage == "fact_proposal":
        app.conclusions._scope(request.observation)
        if list(note.evidence_refs) != working_refs(request):
            from agent_core.dual_llm_modeling import D3ProposalRejected
            raise D3ProposalRejected("d22_working_source_changed")
        return
    live = {i.ref: i for i in app.handoff.history()}
    frozen = visible_items(request)
    for ref in note.evidence_refs:
        item = live.get(ref)
        if (ref not in frozen or item is None or item.source_status != "available" or item.conflict
                or handoff_json(item.visible()) != handoff_json(frozen[ref])
                or app.revisions is not None and not app.revisions.reliable(item)):
            from agent_core.dual_llm_modeling import D3ProposalRejected
            raise D3ProposalRejected("d22_working_source_changed")


def apply_working(app, request_id):
    app.store.mark_d4_operation_applied(request_id, app._id("working/" + request_id))
    app.sessions.reconcile()


def reconcile_working(app):
    # Applied marker may be missing after an observed Plan commit; reuse only a known result.
    for op in app.store.list_d4_operations(app.state.task_run.task_run_id):
        if op.completion is None or op.application_ref is not None or op.start.role != "inner_action":
            continue
        saved = app.sessions.evidence.read("request", op.start.operation_id)
        if saved is None:
            continue
        raw = saved["request"]
        result = (app.conclusions.evidence.read_result(op.start.operation_id) if raw["stage"] == "fact_proposal"
                  else None)
        if result is None or result["outcome"] != "valid" or result["raw"].get("kind") != "working_record":
            continue
        plan = app.store.load_d3_plan_result(raw["observation"]["plan_id"])
        if plan is not None and plan.outcome == "observed" and plan.proof_id == raw["observation"]["proof_id"]:
            request = app.conclusions.evidence.read_request(op.start.operation_id)
            parse_working(request, result["raw"])
            app.store.mark_d4_operation_applied(op.start.operation_id, app._id("working/" + op.start.operation_id))


def closure_marker(app, intent_id):
    if continuous(app.config):return None
    return app.handoff.evidence.read("pause", app._id("budget-closure/" + intent_id))


def budget_stop_reason(store, evidence, task_id):
    """One small pause receipt per Intent; readonly callers never repair it."""
    from agent_core.turn_modeling import stable_id
    config = store.load_d3_run_config(task_id)
    if config is not None and continuous(config):return None
    intent_ids = {r.intent_id for r in store.list_d8_tree_records(task_id) if r.intent_id is not None}
    for intent_id in sorted(intent_ids):
        record = evidence.read("pause", stable_id(task_id, "d3/budget-stop/" + intent_id))
        if record is not None:
            if record != {"intent_id": intent_id, "reason": record.get("reason")} or record["reason"] not in {
                    "d22_closure_budget_unavailable", "d22_closure_requires_more_budget", "d22_closure_completed_budget_stop"}:
                raise D3RecoveryRequired("d22_budget_pause_changed")
            return record["reason"]
    return None


def stop_for_budget(app, intent_id, reason):
    app.handoff.evidence.save("pause", app._id("budget-stop/" + intent_id), {"intent_id": intent_id, "reason": reason})
    from qinyuan_core import get_logger
    get_logger("ctrl").info("budget.closure_stopped", intent=intent_id, reason=reason, category="resource_limit")
    raise D3RecoveryRequired(reason)


def closing_candidate_used(app, intent_id, marker):
    if marker is None:
        return False
    for row in app.sessions.records(intent_id):
        if app.conclusions.optional_failure(row.model_request_id) is not None:
            request = app.sessions.evidence.read("request", row.model_request_id)
            if request["request"]["stage"] == "inner_plan" and request["request"]["base_checkpoint_sequence"] >= marker["checkpoint_sequence"]:
                return True
    for row in app.store.list_d8_conclusions(app.state.task_run.task_run_id):
        if row.intent_id == intent_id and row.plan_id is None:
            request = app.sessions.evidence.read("request", row.proposal_request_id)
            if request is not None and request["request"]["base_checkpoint_sequence"] >= marker["checkpoint_sequence"]:
                return True
    return False


def closure_requirements(app, intent_id):
    required = {"action": 2, "review": 2, "handoff_summary": 2}
    marker = closure_marker(app, intent_id)
    stop = pending_stop(app, intent_id)
    if ((stop is None or stop["decision"] == "request_intent_resolution") and not facts(app, intent_id)
            and not closing_candidate_used(app, intent_id, marker) and app.evidence_conclusions.catalog(intent_id)):
        required["action"] += 2
        required["fact_review"] = 2
    return required


def remaining_budget(app):
    usage = app.store.d4_budget_usage(app.state.task_run.task_run_id)
    used = dict(usage.category_counts)
    return {k: limit - used.get(k, 0) for k, limit in app.config.d4_budget.category_limits.items()}


def exploration_cost(app, intent_id):
    # Bound one admitted capability path, including one possible periodic review/summary.
    # Corrections consume starts, but not extra logical decisions in the four-decision window.
    plans = [p for p in app.state.plans if p.intent_id == intent_id]
    file_path = app.web is None or bool(app.config.authorization.attachment_indices)
    steps = (2 if not plans else 1) if file_path else 0
    return {"action": 2 * (2 + steps), "tool_dispatch": max(1, steps),
            "fact_review": 2, "review": 2 * (steps + 2), "handoff_summary": 2,
            **({"sha_observation": 1} if file_path else {})}


def prepare_closure(app, intent_id):
    if not enabled(app.config) or continuous(app.config):
        return
    # Complete/apply an already known decision before considering another exploration.
    for call in app.store.list_d4_operations(app.state.task_run.task_run_id):
        if call.completion is None or call.application_ref is not None or call.start.role != "inner_action":
            continue
        saved = app.sessions.evidence.read("request", call.start.operation_id)
        result = app.model_evidence.read(call.start.operation_id)
        if (saved is not None and saved["intent_id"] == intent_id and saved["request"]["stage"] == "inner_plan"
                and call.start.base_checkpoint_sequence == app.state.latest_checkpoint.sequence
                and result is not None):
            return
    marker = closure_marker(app, intent_id)
    needed = closure_requirements(app, intent_id)
    left = remaining_budget(app)
    if any(left[k] < count for k, count in needed.items()):
        stop_for_budget(app, intent_id, "d22_closure_budget_unavailable")
    if marker is not None:
        return
    cost = exploration_cost(app, intent_id)
    next_reserve = dict(needed)
    if "fact_review" not in next_reserve and not facts(app, intent_id):
        # A new observation may still yield no approved Fact. Preserve the option to nominate its saved evidence.
        next_reserve.update(action=4, fact_review=2)
    if any(left[k] < next_reserve.get(k, 0) + cost.get(k, 0) for k in set(next_reserve) | set(cost)):
        app.handoff.evidence.save("pause", app._id("budget-closure/" + intent_id),
            {"intent_id": intent_id, "checkpoint_sequence": app.state.latest_checkpoint.sequence})
        from qinyuan_core import get_logger
        get_logger("ctrl").info("budget.closure_requested", intent=intent_id, required=next_reserve,
            remaining={k: left[k] for k in set(needed) | set(cost)}, category="resource_limit")


def stop_request(app, intent_id):
    stop = pending_stop(app, intent_id)
    marker = closure_marker(app, intent_id) if enabled(app.config) else None
    if marker is not None:
        if marker != {"intent_id": intent_id, "checkpoint_sequence": marker.get("checkpoint_sequence")} or type(marker["checkpoint_sequence"]) is not int:
            raise D3RecoveryRequired("d22_budget_closure_changed")
        return {"review_id": app._id("budget-closure/" + intent_id), "decision": "request_intent_resolution",
            "reason": "Remaining category budgets are reserved for a bounded end proposal, its review and necessary summary.",
            "fact_candidate_allowed": (stop is None or stop["decision"] == "request_intent_resolution") and not closing_candidate_used(app, intent_id, marker) and not facts(app, intent_id),
            "history_reads_allowed": remaining_budget(app)["action"] >= closure_requirements(app, intent_id)["action"] + 2,
            **(stop if stop is not None and stop["decision"] != "request_intent_resolution" else {})}
    if stop is not None and stop["decision"] == "request_intent_resolution":
        stop["fact_candidate_allowed"] = not facts(app, intent_id)
    return stop


async def finish_budget_closure(app, token):
    if not enabled(app.config) or continuous(app.config):
        return
    for row in app.store.list_d8_tree_records(app.state.task_run.task_run_id):
        if row.kind not in {"succeeded", "failed", "inconclusive", "evaluation_continued"} or row.intent_id is None:
            continue
        marker = closure_marker(app, row.intent_id)
        if marker is None or not row.inner_request_id:
            continue
        saved = app.sessions.evidence.read("request", row.inner_request_id)
        stop = packet(saved["request"]).get("intent_execution", {}).get("stop_requested") or {}
        if not stop or saved["request"]["base_checkpoint_sequence"] < marker["checkpoint_sequence"]:
            continue
        if row.kind == "evaluation_continued":
            stop_for_budget(app, row.intent_id, "d22_closure_requires_more_budget")
        await app.handoff.summarize(row.turn_id, token)
        stop_for_budget(app, row.intent_id, "d22_closure_completed_budget_stop")


def closure_only(request):
    return (packet(request).get("intent_execution", {}).get("stop_requested") or {}).get("decision") == "request_intent_resolution"


# Continuous scheduling keeps technical suspension separate from business verdicts.
def visits(store, task_id, intent_id):
    return sum(r.kind == "resumed" and r.intent_id == intent_id for r in store.list_d8_tree_records(task_id))


def suspended_calls(store, task_id):
    return {r.model_request_id for r in store.list_d8_tree_records(task_id)
            if r.kind == "suspended" and r.model_request_id is not None}


def retry_candidate(store, state, turn_id):
    from agent_core.domain import IntentStatus
    rows = store.list_d8_tree_records(state.task_run.task_run_id)
    siblings = sorted((i for i in state.intents if i.turn_id == turn_id), key=lambda i: i.priority)
    for index, item in enumerate(siblings[:-1]):
        latest = next((r for r in reversed(rows) if r.intent_id == item.intent_id and r.kind in {"suspended", "resumed", "strategy_yielded", "strategy_resumed"}), None)
        if latest is not None and latest.kind == "strategy_yielded":
            continue
        if item.status is not IntentStatus.SUSPENDED or visits(store, state.task_run.task_run_id, item.intent_id):
            continue
        next_item = siblings[index + 1]
        if next_item.status not in {IntentStatus.FAILED, IntentStatus.INCONCLUSIVE, IntentStatus.ABANDONED, IntentStatus.SUSPENDED}:
            continue
        parked = next((n for n, r in enumerate(rows) if r.kind == "suspended" and r.intent_id == item.intent_id), None)
        if parked is not None and any(r.intent_id == next_item.intent_id and r.kind in {
                "failed", "inconclusive", "suspended"} for r in rows[parked + 1:]):
            return item
    return None


def runnable_turns(store, state, supervision=None):
    from agent_core.domain import IntentStatus, TurnStatus
    rows = store.list_d8_tree_records(state.task_run.task_run_id)
    def work(turn):
        if supervision is not None and supervision.resume_candidate(turn.turn_id) is not None:
            return True
        if retry_candidate(store, state, turn.turn_id) is not None:return True
        for item in state.intents:
            if item.turn_id != turn.turn_id:continue
            if item.status in {IntentStatus.PENDING, IntentStatus.RUNNING}:return True
            if item.status is IntentStatus.SUCCEEDED and not any(t.triggering_intent_id == item.intent_id for t in state.turns) and not any(
                    r.kind == "expansion_exhausted" and r.intent_id == item.intent_id for r in rows):return True
        # Fully resolved leaves still need their ordinary exhaustion/summary commit.
        return not any(i.turn_id == turn.turn_id and i.status is IntentStatus.SUSPENDED for i in state.intents) and not any(
            t.parent_turn_id == turn.turn_id and t.status is not TurnStatus.COMPLETED for t in state.turns)
    return [t for t in state.turns if t.status in {TurnStatus.PENDING, TurnStatus.RUNNING} and work(t)]


def validate_local_transition(store, mutation, state):
    from agent_core.control_store import StorageError, UpsertIntent
    from agent_core.domain import IntentStatus, ExecutionStatus
    record = next(r for r in mutation.state_changes if hasattr(r, "kind"))
    changes = [c for c in mutation.state_changes if isinstance(c, UpsertIntent)]
    old = next((i for i in state.intents if i.intent_id == record.intent_id), None)
    if (mutation.operation != {"suspended": "suspend_d8_intent", "resumed": "resume_d8_intent"}.get(record.kind)
            or record.support_fact_id is not None or record.inner_request_id is not None
            or not continuous(store.load_d3_run_config(mutation.task_run_id)) or old is None
            or len(changes) != 1 or len(mutation.state_changes) != 2
            or changes[0].intent_id != old.intent_id or record.turn_id != old.turn_id
            or any(e.status is ExecutionStatus.RUNNING for e in state.executions)):
        raise StorageError("d22_local_transition_scope_invalid")
    if record.kind == "suspended":
        if old.status is not IntentStatus.RUNNING or changes[0].status is not IntentStatus.SUSPENDED:
            raise StorageError("d22_suspension_requires_running_intent")
        if record.model_request_id:
            op = store.load_d4_operation(record.model_request_id)
            if op is None or op.completion is None or op.application_ref is not None or op.start.task_run_id != mutation.task_run_id:
                raise StorageError("d22_suspension_result_unknown")
    elif record.kind == "resumed":
        expected = retry_candidate(store, state, old.turn_id)
        if (old.status is not IntentStatus.SUSPENDED or changes[0].status is not IntentStatus.RUNNING
                or expected is None or expected.intent_id != old.intent_id or record.model_request_id is not None):
            raise StorageError("d22_revisit_not_available")
    else:
        raise StorageError("d22_local_transition_kind_invalid")


def resume_local(app, intent):
    from agent_core.control_store import UpsertIntent
    from agent_core.domain import IntentStatus
    from agent_core.handoff_contracts import D8TreeRecord
    first = next(r for r in app.store.list_d8_tree_records(app.state.task_run.task_run_id)
                 if r.kind == "suspended" and r.intent_id == intent.intent_id)
    record = D8TreeRecord(app._id("revisit/" + first.record_id), app.state.task_run.task_run_id,
        intent.turn_id, intent.intent_id, "resumed", None)
    app.handoff._commit("resume_d8_intent", record, (UpsertIntent(intent.intent_id, intent.turn_id,
        intent.description, intent.priority, intent.intent_type, IntentStatus.RUNNING),))


def suspend_local(app, intent, error):
    from agent_core.domain import IntentStatus, ExecutionStatus
    from agent_core.dual_llm_modeling import D3ProposalRejected
    from agent_core.control_store import UpsertIntent
    from agent_core.handoff_contracts import D8TreeRecord
    from agent_core.review import D2ReviewPaused
    if not continuous(app.config) or intent.status is not IntentStatus.RUNNING:return False
    if app.store.blocking_d4_operations(app.state.task_run.task_run_id) or any(
            e.status is ExecutionStatus.RUNNING for e in app.state.executions):return False
    code = getattr(error, "code", None) or str(error)
    context_failure = code.startswith("d8_context_paused:")
    allowed = {"d22_awaiting_adjudication", "d8_progress_inconclusive", "d8_progress_repetitive",
        "d8_model_output_truncated", "d8_model_output_budget_exhausted", "d8_model_format_rejected",
        "d8_model_invalid", "d8_model_model_error", "d8_model_sensitive_rejected"}
    if not context_failure and not isinstance(error, (D3ProposalRejected, D2ReviewPaused)) and code not in allowed:return False
    identity = None
    for op in (() if context_failure else reversed(app.store.list_d4_operations(app.state.task_run.task_run_id))):
        if op.completion is None or op.application_ref is not None or op.start.role is None:continue
        key = op.start.operation_id
        saved = app.sessions.evidence.read("request", key) or app.handoff.evidence.read("request", key) or app.output_boundary.evidence.read("review-request", key)
        if saved is None:
            obj = app.conclusions.evidence.read_request(key)
            saved = asdict(obj) if obj is not None else None
        owner = (saved or {}).get("intent_id") or (saved or {}).get("observation", {}).get("intent_id")
        if owner == intent.intent_id:
            provider = app.output_boundary.evidence.read("provider-result", key)
            transport_end = (provider or {}).get("transport", {}).get("transport_end")
            if (provider or {}).get("transport", {}).get("http_status") in {401, 402, 403}:return False
            model = app.model_evidence.read(key)
            raw = model.raw if model is not None and isinstance(model.raw, dict) else {}
            from agent_core.structured_output import ended_transport_failure
            ended = ended_transport_failure((provider or {}).get("transport", {}))
            if ((not ended and (transport_end in {"timeout", "cancelled", "transport_error", "incomplete_response", "worker_lost", "unknown"}
                    or raw.get("kind") in {"timed_out", "cancelled"}))
                    or raw.get("kind") == "authentication_failed"
                    or str(raw.get("short_reason", "")).endswith(("http_401", "http_402", "http_403"))):
                return False  # Scope is not safe to advance while the call outcome is unknown.
            identity = key
            break
    if not context_failure and identity is None:return False
    # No technical event is promoted to a business failure or verified Fact.
    record_id = app._id(f"suspend/{intent.intent_id}/{visits(app.store, app.state.task_run.task_run_id, intent.intent_id)}")
    note = {"intent_id": intent.intent_id, "reason": code if context_failure or code in allowed else "d22_local_model_failure",
            "failed_request_id": identity, "identity": "technical_suspension_not_business_verdict"}
    note = app.handoff.evidence.read("pause", record_id) or {**note, "progress": app.sessions.handoff_progress(intent.intent_id)}
    app.sessions._safe(note)
    app.handoff.evidence.save("pause", record_id, note)
    record = D8TreeRecord(record_id, app.state.task_run.task_run_id, intent.turn_id, intent.intent_id, "suspended", identity)
    app.handoff._commit("suspend_d8_intent", record, (UpsertIntent(intent.intent_id, intent.turn_id,
        intent.description, intent.priority, intent.intent_type, IntentStatus.SUSPENDED),))
    from qinyuan_core import get_logger
    get_logger("ctrl").info("intent.suspended", run=record.task_run_id, intent=intent.intent_id,
        code=note["reason"], request=identity, category="technical_result")
    return True


def park_pending_adjudication(app):
    from agent_core.domain import IntentStatus
    if not continuous(app.config):return False
    pending = pending_adjudication(app.store, app.handoff.evidence, app.output_boundary.evidence,
                                   app.state.task_run.task_run_id)
    if pending is None:return False
    intent = next(i for i in app.state.intents if i.intent_id == pending["intent_id"])
    return intent.status is IntentStatus.RUNNING and suspend_local(app, intent, D3RecoveryRequired(pending["reason"]))


async def replan_root_once(app, receipts, token):
    from agent_core.handoff_contracts import D8HandoffRequest
    marker_id = app._id("continuous/root-replan")
    done_id = app._id("continuous/root-replan-done")
    if app.handoff.evidence.read("pause", done_id) is not None:return False
    marker = app.handoff.evidence.read("pause", marker_id)
    if marker is None:
        app._root_replanning = True
        try:request, _ = app.handoff._request("tree_planning", None)
        finally:app._root_replanning = False
        app.handoff.evidence.save("request", request.model_request_id, asdict(request))
        marker = {"model_request_id": request.model_request_id}
        app.handoff.evidence.save("pause", marker_id, marker)
    request = D8HandoffRequest.from_dict(app.handoff.evidence.read("request", marker["model_request_id"]))
    turn_id = await app.handoff.plan_expansion(None, None, receipts, token, frozen_request=request)
    app.handoff.evidence.save("pause", done_id, {"model_request_id": request.model_request_id, "turn_id": turn_id})
    return turn_id is not None
