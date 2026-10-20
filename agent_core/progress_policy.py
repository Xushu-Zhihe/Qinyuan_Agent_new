"""Contract13 progress choices, explicit latest evidence and deterministic scope."""

from dataclasses import asdict, is_dataclass
import json

from agent_core.review_policy import ACTION_KINDS, MAX_ACTION_TIMEOUT, unavailable_decisions
from agent_core.turn_modeling import digest
from agent_core.dual_llm_contracts import D3RecoveryRequired


CLASSIFICATIONS = ("meaningful", "repetitive", "inconclusive")
PAUSE = "pause_current_intent"


def decisions(mode, sibling, replan, *, rolling=False):
    if mode == "restricted_repair":
        return {"meaningful": ["continue_current_intent"], "repetitive": [PAUSE], "inconclusive": [PAUSE]}
    result = {"meaningful": ["continue_current_intent"] + (["replan_current_intent"] if replan else []),
            "repetitive": (["replan_current_intent"] if replan else [])
                + (["switch_sibling_intent"] if sibling else []) + ["abandon_current_intent"],
            "inconclusive": ["continue_current_intent", PAUSE] if rolling else [PAUSE]}
    if rolling:
        for values in result.values():
            values.append("request_intent_resolution")
    return result


def freeze_policy(app, context, turn_id, intent_id, scope, actions, timeout):
    progress = context["d8_progress"]
    recent = progress["recent"]
    latest = recent[-1]["ref"] if recent else None
    if (not recent or recent[-1]["ordinal"] != progress["decision_count"]
            or latest not in context["source_versions"]):
        raise D3RecoveryRequired("s3_latest_decision_unavailable")
    unavailable = unavailable_decisions(app.state, turn_id, intent_id, app.config.max_turns, rolling=bool(app.config.intent_execution_policy))
    sibling, replan = "switch_sibling_intent" not in unavailable, "replan_current_intent" not in unavailable
    mode = progress["mode"]
    return {"version": 2 if app.config.contract_version in {16, 17, 18, 19, 20, 21, 22} else 1,
            "review_scope": scope, "scope_bindings": {"intent": intent_id, "turn": turn_id},
            "mode": mode, "decision_watermark": progress["decision_count"],
            "window_sha256": digest(progress), "required_latest_decision_ref": latest,
            "sibling_available": sibling, "replan_available": replan,
            "allowed_decisions": decisions(mode, sibling, replan, rolling=progress.get("intent_execution_policy") == "rolling_v1"),
            "allowed_action_kinds": [a for a in ACTION_KINDS if a in actions],
            "max_action_timeout_seconds": timeout}


def policy_for(request):
    context = json.loads(request.context_summary)
    p, progress = context.get("progress_policy"), context.get("d8_progress")
    try:
        if (not isinstance(p, dict) or set(p) != {"version", "review_scope", "scope_bindings", "mode",
                "decision_watermark", "window_sha256", "required_latest_decision_ref", "sibling_available",
                "replan_available", "allowed_decisions", "allowed_action_kinds", "max_action_timeout_seconds"}
                or type(p["version"]) is not int or p["version"] not in {1, 2} or request.review_scope != "action"
                or p["review_scope"] != request.review_scope
                or p["scope_bindings"] != {"intent": request.intent_id, "turn": request.turn_id}
                or not isinstance(progress, dict) or p["mode"] not in {"ordinary", "restricted_repair"}
                or p["mode"] != progress["mode"]
                or type(p["decision_watermark"]) is not int or p["decision_watermark"] < 4
                or p["decision_watermark"] != progress["decision_count"]
                or progress["window"] != 4 or progress["continue_decisions"] != 4
                or not isinstance(progress["recent"], list) or len(progress["recent"]) != 4
                or [r["ordinal"] for r in progress["recent"]] != list(range(p["decision_watermark"]-3, p["decision_watermark"]+1))
                or p["required_latest_decision_ref"] != progress["recent"][-1]["ref"]
                or p["required_latest_decision_ref"] not in request.source_versions
                or p["window_sha256"] != digest(progress)
                or type(p["sibling_available"]) is not bool or type(p["replan_available"]) is not bool
                or p["allowed_decisions"] != decisions(p["mode"], p["sibling_available"], p["replan_available"], rolling=progress.get("intent_execution_policy") == "rolling_v1")
                or not isinstance(p["allowed_action_kinds"], list)
                or p["allowed_action_kinds"] != [a for a in ACTION_KINDS if a in p["allowed_action_kinds"]]
                or p["mode"] == "ordinary" and not p["allowed_action_kinds"]
                or type(p["max_action_timeout_seconds"]) is not int
                or not 1 <= p["max_action_timeout_seconds"] <= MAX_ACTION_TIMEOUT):
            raise ValueError("policy")
    except (KeyError, TypeError, ValueError) as exc:
        raise D3RecoveryRequired("s3_progress_policy_invalid") from exc
    return p


def progress_schema(request):
    from agent_core.output_contracts import obj, array, text, tagged
    p = policy_for(request)
    choice = lambda values: {"type": "string", "enum": list(values)}
    constraint = {"anyOf": [tagged("allowed_action_kinds", {
        "scope_kind": choice(("intent", "turn")),
        "allowed_action_kinds": {**array(choice(p["allowed_action_kinds"]), 1), "uniqueItems": True}}),
        tagged("max_action_timeout_seconds", {"scope_kind": choice(("intent", "turn")),
        "max_action_timeout_seconds": {"type": "integer", "minimum": 1,
                                       "maximum": p["max_action_timeout_seconds"]}})]}
    branches = []
    for classification in CLASSIFICATIONS:
        refs = {**array(choice(request.source_versions), 1, 8), "uniqueItems": True,
                "contains": {"type": "string", "const": p["required_latest_decision_ref"]}, "minContains": 1}
        branch = obj({"decision": choice(p["allowed_decisions"][classification]), "rationale": text(),
            "source_ids": {**array(choice(request.source_versions), 1), "uniqueItems": True},
            "constraints": array(constraint, maximum=0 if p["mode"] == "restricted_repair" or classification == "inconclusive" else None),
            "progress": obj({"classification": {"type": "string", "const": classification},
                             "reason": text(None if p['version'] == 2 else 512), "evidence_refs": refs})})
        from agent_core.execution_feasibility import constrain_schema
        constrained = constrain_schema(branch, request)
        branches.extend(constrained['anyOf'] if 'anyOf' in constrained else [constrained])
    if p["mode"] == "ordinary" and json.loads(request.context_summary)["d8_progress"].get("intent_execution_policy"):
        for branch in branches:
            if branch["properties"]["decision"].get("enum") == ["request_intent_resolution"]:
                branch["properties"]["constraints"]["maxItems"] = 0
    return {"anyOf": branches}


def validate_and_bind(wire, request):
    from agent_core.output_contracts import decode_response
    from agent_core.session_contracts import _parse_progress_review_legacy
    from agent_core.review_contracts import D2AllowedActionKinds
    p = policy_for(request)
    decoded, issues, terminal, _ = decode_response(wire, progress_schema(request))
    if issues or terminal:
        raise ValueError("s3_progress_wire_invalid")
    canonical = {**decoded, "constraints": [{**item, "scope_id": p["scope_bindings"][item["scope_kind"]]}
                                           for item in decoded["constraints"]]}
    output = _parse_progress_review_legacy(canonical, request)
    actions, timeout = set(p["allowed_action_kinds"]), p["max_action_timeout_seconds"]
    for item in output.constraints:
        if isinstance(item, D2AllowedActionKinds):
            if not set(item.allowed_action_kinds).issubset(actions):
                raise ValueError("constraint_widens_allowed_actions")
            actions &= set(item.allowed_action_kinds)
        else:
            if item.max_action_timeout_seconds > timeout:
                raise ValueError("constraint_widens_timeout")
            timeout = min(timeout, item.max_action_timeout_seconds)
    from agent_core.execution_feasibility import validate
    validate(canonical, request)
    return canonical


def parse_bound(raw, request):
    from agent_core.session_contracts import _parse_progress_review_legacy
    if is_dataclass(raw): raw = asdict(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("constraints"), (list, tuple)):
        raise ValueError("s3_progress_bound_shape_invalid")
    p = policy_for(request)
    wire = {**raw, "constraints": []}
    for item in raw["constraints"]:
        if (not isinstance(item, dict) or item.get("scope_kind") not in p["scope_bindings"]
                or item.get("scope_id") != p["scope_bindings"][item["scope_kind"]]):
            raise ValueError("s3_progress_scope_binding_invalid")
        wire["constraints"].append({k: v for k, v in item.items() if k != "scope_id"})
    expected = validate_and_bind(wire, request)
    if json.loads(json.dumps(raw)) != expected:
        raise ValueError("s3_progress_bound_output_changed")
    return _parse_progress_review_legacy(expected, request)


ISSUES = {
    "progress_rationale_invalid": ("$/rationale", "nonempty_text_without_nul", "invalid"),
    "progress_reason_invalid": ("$/progress/reason", "nonempty_text_at_most_512", "invalid"),
    "progress_latest_reference_required": ("$/progress/evidence_refs", "explicit_latest_frozen_decision", "missing"),
    "progress_sources_invalid": ("$/source_ids", "explicit_current_sources", "invalid"),
    "progress_actions_widened": ("$/constraints", "current_action_subset", "widened"),
    "progress_timeout_widened": ("$/constraints", "current_timeout_limit", "widened"),
    "progress_execution_path_unavailable": ("$/constraints", "preserve_current_execution_path", "unavailable"),
    "progress_business_rejected": ("$", "valid_bound_progress_review", "rejected"),
}


def issue(code, *, unbounded=False):
    path, expected, actual = ISSUES[code]
    if code == 'progress_reason_invalid' and unbounded:
        expected = 'nonempty_text_without_nul'
    return {"code": code, "path": path, "expected": expected, "actual": actual}


def business_issues(raw, request):
    result = []
    if not isinstance(raw, dict): return [issue("progress_business_rejected")]
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or "\x00" in rationale:
        result.append(issue("progress_rationale_invalid"))
    progress = raw.get("progress", {})
    reason = progress.get("reason") if isinstance(progress, dict) else None
    unbounded = policy_for(request)['version'] == 2
    if (not isinstance(reason, str) or not reason.strip()
            or ("\x00" in reason if unbounded else len(reason) > 512)):
        result.append(issue("progress_reason_invalid", unbounded=unbounded))
    try:
        validate_and_bind(raw, request)
    except (TypeError, ValueError) as exc:
        if exc.args == ("constraint_widens_allowed_actions",): result.append(issue("progress_actions_widened"))
        elif exc.args == ("constraint_widens_timeout",): result.append(issue("progress_timeout_widened"))
        elif exc.args == ("execution_path_unavailable",): result.append(issue("progress_execution_path_unavailable"))
    return result[:8] or [issue("progress_business_rejected")]
