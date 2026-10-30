"""Contract9 ordinary review choices and trusted scope binding; no I/O."""

import json

ACTION_KINDS = ("probe", "complete_intent")
MAX_ACTION_TIMEOUT = 300
REVIEW_DECISIONS = ("continue_current_intent", "switch_sibling_intent",
                    "replan_current_intent", "adjust_turn_target", "acknowledge_turn_end")


def stage_decisions(scope):
    return REVIEW_DECISIONS[-1:] if scope == "turn_end" else REVIEW_DECISIONS[:-1]


def unavailable_decisions(state, turn_id, intent_id, max_turns, *, rolling=False):
    from agent_core.domain import IntentStatus
    result = {}
    if not any(i.turn_id == turn_id and i.status is IntentStatus.PENDING for i in state.intents):
        result["switch_sibling_intent"] = "d3_no_sibling_candidate"
    count = sum(p.intent_id == intent_id for p in state.plans)
    if not (count >= 1 if rolling else 1 <= count < 2):
        result["replan_current_intent"] = "d3_replan_not_available"
    if max_turns is not None and len(state.turns) >= max_turns:
        result["adjust_turn_target"] = "d3_turn_limit_reached"
    return result


def policy_for(request):
    context = json.loads(request.context_summary)
    if "d8_progress" in context:
        return None
    policy = context.get("review_policy")
    if (not isinstance(policy, dict) or set(policy) != {
            "version", "review_scope", "scope_bindings", "allowed_decisions",
            "allowed_action_kinds", "max_action_timeout_seconds"}
            or type(policy["version"]) is not int or policy["version"] != 1
            or policy["review_scope"] != request.review_scope
            or policy["scope_bindings"] != {"intent": request.intent_id, "turn": request.turn_id}
            or not isinstance(policy["allowed_decisions"], list)
            or not policy["allowed_decisions"]
            or policy["allowed_decisions"] != [d for d in stage_decisions(request.review_scope)
                                              if d in policy["allowed_decisions"]]
            or "continue_current_intent" not in policy["allowed_decisions"] and request.review_scope != "turn_end"
            or not isinstance(policy["allowed_action_kinds"], list)
            or not policy["allowed_action_kinds"]
            or policy["allowed_action_kinds"] != [a for a in ACTION_KINDS if a in policy["allowed_action_kinds"]]
            or type(policy["max_action_timeout_seconds"]) is not int
            or not 1 <= policy["max_action_timeout_seconds"] <= MAX_ACTION_TIMEOUT):
        from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
        raise ExecutionRecoveryRequired("d9_review_policy_invalid")
    return policy


def review_schema(request):
    from agent_core.output_contracts import obj, array, text, tagged
    policy = policy_for(request)
    choice = lambda values: {"type": "string", "enum": list(values)}
    constraint = {"anyOf": [tagged("allowed_action_kinds", {
        "scope_kind": choice(("intent", "turn")),
        "allowed_action_kinds": {**array(choice(policy["allowed_action_kinds"]), 1), "uniqueItems": True}}),
        tagged("max_action_timeout_seconds", {"scope_kind": choice(("intent", "turn")),
        "max_action_timeout_seconds": {"type": "integer", "minimum": 1,
                                       "maximum": policy["max_action_timeout_seconds"]}})]}
    schema = obj({"decision": choice(policy["allowed_decisions"]), "rationale": text(),
                "source_ids": {**array(choice(request.source_versions),
                                        0 if request.review_scope == "turn_end" else 1), "uniqueItems": True},
                "constraints": array(constraint, maximum=0 if request.review_scope == "turn_end" else None)})
    from agent_core.execution_feasibility import constrain_schema
    return constrain_schema(schema, request)


def validate_and_bind(raw, request):
    """Input already passed wire Schema. Fill only scope_id, then check business rules."""
    from agent_core.dual_llm_contracts import parse_d3_review_output
    from agent_core.review_contracts import D2AllowedActionKinds
    policy = policy_for(request)
    canonical = {**raw, "constraints": [
        {**item, "scope_id": policy["scope_bindings"][item["scope_kind"]]}
        for item in raw["constraints"]]}
    output = parse_d3_review_output(canonical, request)
    actions = set(policy["allowed_action_kinds"])
    timeout = policy["max_action_timeout_seconds"]
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


ISSUES = {
    "review_rationale_invalid": ("$/rationale", "nonempty_text_without_nul", "invalid"),
    "review_sources_invalid": ("$/source_ids", "unique_current_sources", "invalid"),
    "review_constraints_invalid": ("$/constraints", "registered_scoped_constraints", "invalid"),
    "review_actions_widened": ("$/constraints", "current_action_subset", "widened"),
    "review_timeout_widened": ("$/constraints", "current_timeout_limit", "widened"),
    "review_execution_path_unavailable": ("$/constraints", "preserve_current_execution_path", "unavailable"),
    "review_business_rejected": ("$", "valid_ordinary_review", "rejected"),
}


def issue(code):
    path, expected, actual = ISSUES[code]
    return {"code": code, "path": path, "expected": expected, "actual": actual}


def business_issues(raw, request):
    # Never store or return arbitrary exception text or model values.
    result = []
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or "\x00" in rationale:
        result.append(issue("review_rationale_invalid"))
    refs = raw.get("source_ids")
    if (not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() or "\x00" in r for r in refs)
            or len(set(refs)) != len(refs) or not set(refs).issubset(request.source_versions)):
        result.append(issue("review_sources_invalid"))
    try:
        validate_and_bind(raw, request)
    except (TypeError, ValueError) as exc:
        if exc.args == ("constraint_widens_allowed_actions",):
            result.append(issue("review_actions_widened"))
        elif exc.args == ("constraint_widens_timeout",):
            result.append(issue("review_timeout_widened"))
        elif exc.args == ("execution_path_unavailable",):
            result.append(issue("review_execution_path_unavailable"))
        elif not result:
            result.append(issue("review_constraints_invalid"))
    return result[:8] or [issue("review_business_rejected")]
