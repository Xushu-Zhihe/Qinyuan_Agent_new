"""Fixed D8-5 output shapes. This is not a user-extensible Schema engine."""

from dataclasses import dataclass, is_dataclass, asdict
from collections.abc import Mapping
import json
import math


@dataclass(frozen=True)
class D8OutputRequest:
    request: object
    output_contract: dict
    instruction: str
    correction: dict | None


@dataclass(frozen=True)
class D8ModelResponse:
    content: object
    finish_reason: str = "stop"


@dataclass(frozen=True)
class D8FormatIssue:
    code: str
    path: str
    expected: str
    actual: str


def obj(properties, optional=()):
    return {"type": "object", "properties": properties,
            "required": [k for k in properties if k not in optional],
            "additionalProperties": False}


def array(item, minimum=0, maximum=None):
    result = {"type": "array", "items": item, "minItems": minimum}
    if maximum is not None:
        result["maxItems"] = maximum
    return result


def text(maximum=None):
    result = {"type": "string"}
    if maximum is not None:
        result["maxLength"] = maximum
    return result


S, I, B, N = text(), {"type": "integer"}, {"type": "boolean"}, {"type": "null"}
NS = {"type": ["string", "null"]}
NB = {"type": ["boolean", "null"]}


def tagged(kind, fields):
    return obj({"kind": {"type": "string", "const": kind}, **fields})


REFS = array(S)
RULE = obj({"rule_id": S, "version": I, "parameters": obj({"attachment_index": I})})
INTENT = obj({"description": S, "intent_type": NS, "dependency_refs": REFS})
CHANGE = tagged("conclusion_change", {
    "operation": S, "targets": array(obj({"root_id": S, "version": I, "ref": S}), 1, 2),
    "replacement_ref": NS, "content": {"type": ["string", "null"]},
    "evidence_refs": array(S, 0, 8), "reason": text(512), "resolution": NS,
    "relation_kind": NS, "relation_owner_id": NS})
READS = [tagged("history_query", {"query": obj({k: NS for k in (
    "turn_id", "intent_id", "artifact_version_id", "record_type")}, optional=(
    "turn_id", "intent_id", "artifact_version_id", "record_type"))}),
    tagged("history_details", {"refs": array(S, 1, 2)}),
    tagged("history_next_page", {"cursor": S})]
CONDITION = {"anyOf": [tagged("output_contains", {
    "step_index": I, "stream": S, "literal": S}), tagged("file_sha256", {
    "step_index": I, "path": S, "expected_sha256": S})]}
PROBE = obj({"kind": {"type": "string", "const": "probe"}, "script": S,
    "working_directory": S, "timeout_seconds": I, "conditions": array(CONDITION, 0, 32),
    "stdin_base64": S}, optional=("stdin_base64",))
SUGGESTION = obj({"description": S, "intent_type": NS, "dependency_refs": REFS,
                  "reason": text(512), "evidence_refs": array(S, 1, 8)})
CONSTRAINT = {"anyOf": [tagged("allowed_action_kinds", {
    "scope_kind": S, "scope_id": S, "allowed_action_kinds": array(S)}),
    tagged("max_action_timeout_seconds", {
    "scope_kind": S, "scope_id": S, "max_action_timeout_seconds": I})]}
CLAIM = obj({"subject_ref": S, "observation_scope": S, "claim_key": text(128),
    "claim_value": text(512), "source_refs": array(S, 1, 32),
    "conflicts_with": array(obj({"process_fact_id": S, "claim_index": I}))},
    optional=("conflicts_with",))


def output_schema(request, *, contract_version=None):
    stage, schema = _base_output_schema(request, contract_version=contract_version)
    if contract_version in {15, 16, 17, 18, 19, 20, 21, 22}:
        from agent_core.governance_policy import patch_schema
        schema = patch_schema(request, stage, schema)
    if stage == "inner_plan" and contract_version == 22:
        tools = json.loads(getattr(request, "history_context", "{}") or "{}").get("mcp_tools", [])
        choices = json.loads(getattr(request, "history_context", "{}") or "{}").get("completion_facts", [])
        if choices and "intent_execution" not in json.loads(request.history_context):
            completion = obj({"kind": {"type": "string", "const": "request_completion"},
                "fact_id": {"type": "string", "enum": [i["fact_id"] for i in choices]}})
            schema = {"anyOf": [*schema.get("anyOf", [schema]), completion]}
        if tools:
            calls = [obj({"kind": {"const": "mcp_call", "type": "string"},
                          "tool_name": {"const": t["qualified_name"], "type": "string"},
                          "arguments": {"$schema": "https://json-schema.org/draft/2020-12/schema",
                                        **t["input_schema"]}}) for t in tools]
            schema = {"anyOf": [*schema.get("anyOf", [schema]), *calls]}
    if contract_version == 22 and stage == "inner_plan" and "intent_execution" in json.loads(getattr(request, "history_context", "{}") or "{}"):
        from agent_core.intent_execution import resolution_schema
        context = json.loads(request.history_context)
        branches = schema.get("anyOf", [schema])
        if context["intent_execution"].get("stop_requested"):
            branches = [b for b in branches if b.get("properties", {}).get("kind", {}).get("const") in {"history_query", "history_details", "history_next_page"}]
        elif "probe" not in context["execution_feasibility"]["allowed_action_kinds"]:
            branches = [b for b in branches if b.get("properties", {}).get("kind", {}).get("const") in {"history_query", "history_details", "history_next_page", "conclusion_change"}]
        schema = {"anyOf": [*branches, *resolution_schema(request)["anyOf"]]}
    return stage, schema


def _base_output_schema(request, *, contract_version=None):
    """Enumerate the existing internal stages; unknown stages never fall back."""
    from agent_core.conclusion_revision_contracts import D8ChangeRequest
    from agent_core.process_facts import D5RefinementRequest
    from agent_core.review_contracts import D2ReviewRequest
    if isinstance(request, D5RefinementRequest):
        return "refinement", obj({"summary_text": text(2048), "claims": array(CLAIM, 0, 8)})
    if isinstance(request, D2ReviewRequest):
        if (contract_version in {17, 18, 19, 20, 21, 22} and request.review_scope != 'turn_end'
                and json.loads(request.context_summary).get('d8_progress', {}).get('mode') != 'restricted_repair'):
            from agent_core.execution_feasibility import policy
            policy(request, required=True)
        fields = {"decision": S, "rationale": S, "source_ids": REFS,
                  "constraints": array(CONSTRAINT)}
        context = json.loads(request.context_summary)
        progress = isinstance(context, dict) and "d8_progress" in context
        if contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and progress:
            from agent_core.progress_policy import progress_schema, policy_for
            if policy_for(request)['version'] != (2 if contract_version in {16, 17, 18, 19, 20, 21, 22} else 1):
                from agent_core.dual_llm_contracts import D3RecoveryRequired
                raise D3RecoveryRequired('s6_review_text_policy_version_changed')
            return "progress_review", progress_schema(request)
        if contract_version in {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22} and not progress:
            from agent_core.review_policy import review_schema
            return "review", review_schema(request)
        if progress:
            fields["progress"] = obj({"classification": S, "reason": text(512),
                                      "evidence_refs": array(S, 1, 8)})
        return "progress_review" if progress else "review", obj(fields)
    stage = request.stage
    if isinstance(request, D8ChangeRequest):
        if stage == "fact_proposal":
            return "conclusion_repair", {"anyOf": [CHANGE, *READS]}
        return "change_review", obj({"decision": S, "reason": text(512),
                                       "goal_supported": NB, "scope_explanation": NS})
    if stage == "tree_planning":
        if 'd20_http_policy' in json.loads(getattr(request, 'prior_summary', '{}')):
            from agent_core.http_policy import schema
            return stage, schema(request)
        if contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.planning_assertion import planning_schema
            return stage, planning_schema(request)
        return stage, {"anyOf": [tagged("candidates", {
            "turn_target": S, "completion_rule": RULE, "intents": array(INTENT, 1, 2)}),
            tagged("exhausted", {"reason": text(512), "evidence_refs": REFS})]}
    if stage == "outer_turn":
        old_intent = obj({"description": S, "intent_type": NS})
        return stage, obj({"turn_target": S, "completion_rule": RULE,
                           "intents": array(old_intent, 1, None if contract_version == 22 else 2)})
    if stage == "inner_plan":
        if getattr(getattr(getattr(request, 'context', None), 'turn_binding', None), 'kind', None) in {'http_origin','hybrid_origin'}:
            from agent_core.http_policy import schema
            return stage, {'anyOf': [schema(request), CHANGE, *READS]}
        if contract_version in {17, 18, 19, 20, 21, 22}:
            from agent_core.execution_feasibility import KEY
            if KEY not in json.loads(request.history_context):
                from agent_core.dual_llm_contracts import D3RecoveryRequired
                raise D3RecoveryRequired('d17_plan_execution_context_missing')
        if contract_version in {16, 17, 18, 19, 20, 21, 22}:
            from agent_core.plan_policy import plan_schema
            return stage, {"anyOf": [plan_schema(request), CHANGE, *READS]}
        return stage, {"anyOf": [obj({"template": S, "observation_mode": S,
                                      "source_proof_id": NS}), CHANGE, *READS]}
    if stage in {"inner_size", "inner_sha256"}:
        if contract_version in {11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.fixed_probe import fixed_probe_schema
            return stage, {"anyOf": [fixed_probe_schema(request), CHANGE]}
        return stage, {"anyOf": [obj({"action": PROBE, "review_requested": B}), CHANGE]}
    if stage == "inner_complete":
        if contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.completion_evaluation import completion_schema
            return stage, completion_schema(request, maximum=None if contract_version == 22 else 2)
        return stage, {"anyOf": [obj({"action": tagged("complete_intent", {
            "fact_ids": array(S, 1)}), "review_requested": B,
            "next_intent_candidates": array(SUGGESTION, 0, 2)}), CHANGE]}
    if stage == "inner_candidate":
        if contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.completion_evaluation import candidate_schema
            return stage, candidate_schema(request, allow_none=contract_version in {19, 20, 21, 22})
        return stage, obj({"action": tagged("candidate_flag", {
            "value": S, "execution_id": S, "evidence_version": S, "source_ref": S}),
            "review_requested": B})
    if stage == "fact_proposal":
        assertion = obj({"rule_id": S, "rule_version": I,
                         "artifact_version_id": S, "observation_id": S})
        if contract_version in {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.planning_assertion import assertion_schema
            assertion = assertion_schema()
        if getattr(getattr(request, 'observation', None), 'rule_id', None) == 'http_response_observed':
            assertion = obj({'rule_id': {'type':'string','const':'http_response_observed'}})
        evidence_refs = array(S, 1, 8)
        if contract_version in {10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.conclusion_contracts import allowed_fact_evidence_refs
            allowed = allowed_fact_evidence_refs(request.observation)
            evidence_refs = {**array({"type": "string", "enum": list(allowed)}, len(allowed), len(allowed)),
                             "uniqueItems": True}
        return stage, {"anyOf": [tagged("candidate", {
            "content": text(256), "reason": text(512), "evidence_refs": evidence_refs,
            "assertion": {"anyOf": [N, assertion]}}),
            tagged("request_review", {"reason": text(512)})]}
    if stage == "fact_review":
        fields = {"decision": S, "reason": text(512)}
        if contract_version == 22:
            # Version 22: outer layer can output refined content and merged sources.
            # refined_content becomes the committed Fact content, so it must obey
            # the same 256 code-point ceiling as Fact.content; advertising 2048
            # let the model produce content the control store then refused.
            fields["refined_content"] = {"type": ["string", "null"], "maxLength": 256}
            fields["merged_sources"] = {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 8,
            }
        schema = obj(fields, optional={"refined_content", "merged_sources"} if contract_version == 22 else ())
        from agent_core.conclusion_contracts import D22FactReviewRequest
        if isinstance(request, D22FactReviewRequest):
            fields["decision"] = {"type": "string", "enum": ["accept", "reject", "insufficient"]}
            options = [schema, obj({"decision": {"type": "string", "const": "no_new_information"}, "reason": text(512)})]
            if request.existing_facts:
                options.append(obj({"decision": {"type": "string", "const": "reuse"}, "reason": text(512),
                    "reuse_fact_id": {"type": "string", "enum": [i["fact_id"] for i in request.existing_facts]}}))
            schema = {"anyOf": options}
        return stage, schema
    if stage == "intent_evaluation":
        if contract_version in {14, 15, 16, 17, 18, 19, 20, 21, 22}:
            from agent_core.completion_evaluation import evaluation_schema
            return stage, evaluation_schema(request)
        return stage, obj({"decision": S, "reason": text(), "evidence_refs": REFS})
    if stage == "handoff_summary":
        return stage, obj({"turn_body": text(request.capacity.turn_body),
            "global_body": text(request.capacity.global_body), "ordered_refs": REFS,
            "open_questions": array(obj({"text": text(256), "refs": REFS}), 0, 8)})
    raise ValueError("d8_output_stage_unsupported")


def json_type(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "unsupported"


def shape_issues(value, schema, path="$"):
    if "$schema" in schema:
        # MCP advertises standard JSON Schema, including optional fields and refs.
        from itertools import islice
        from jsonschema.validators import validator_for
        return [D8FormatIssue("tool_argument_" + str(error.validator),
                    path + "".join("/" + str(part) for part in error.absolute_path),
                    "registered_tool_schema", json_type(error.instance))
                for error in islice(validator_for(schema)(schema).iter_errors(value), 8)]
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if isinstance(value, dict) and isinstance(value.get("progress"), dict):
            classification = value["progress"].get("classification")
            matched = [s for s in branches if s.get("properties", {}).get("progress", {}).get(
                "properties", {}).get("classification", {}).get("const") == classification
                and "const" in s.get("properties", {}).get("progress", {}).get("properties", {}).get("classification", {})]
            if matched:
                branches = matched
        if isinstance(value, dict) and isinstance(value.get("kind"), str):
            matched = [s for s in branches if s.get("properties", {}).get("kind", {}).get("const") == value["kind"]]
            if matched:
                branches = matched
        options = [shape_issues(value, s, path) for s in branches]
        return min(options, key=len)
    actual = json_type(value)
    expected = schema["type"]
    allowed = expected if isinstance(expected, list) else [expected]
    if actual not in allowed:
        return [D8FormatIssue("type_mismatch", path, "|".join(allowed), actual)]
    if "const" in schema and value != schema["const"]:
        return [D8FormatIssue("constant_mismatch", path, "fixed_value", actual)]
    if "enum" in schema and value not in schema["enum"]:
        return [D8FormatIssue("enum_mismatch", path, "allowed_choice", actual)]
    result = []
    if actual == "object":
        fields = schema["properties"]
        for key in schema["required"]:
            if key not in value:
                expected_type = fields[key].get("type", "allowed_shape")
                expected_type = "|".join(expected_type) if isinstance(expected_type, list) else expected_type
                result.append(D8FormatIssue("field_missing", path + "/" + key, expected_type, "missing"))
        extra = [key for key in value if key not in fields]
        additional = schema.get("additionalProperties", False)
        if extra and additional is False:
            result.append(D8FormatIssue("field_unknown", path + "/<unknown>", "declared_field", "unknown"))
        elif extra and isinstance(additional, Mapping):
            # Open maps such as HTTP headers declare a value schema instead of a
            # closed field list, so their keys must not be reported as unknown.
            for key in extra:
                result += shape_issues(value[key], additional, path + "/" + key)
        for key in fields:
            if len(result) >= 8:
                break
            if key in value:
                result += shape_issues(value[key], fields[key], path + "/" + key)
    elif actual == "array":
        if "contains" in schema and not any(not shape_issues(v, schema["contains"], path) for v in value):
            result.append(D8FormatIssue("array_required_item_missing", path, "required_current_reference", "missing"))
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            result.append(D8FormatIssue("array_duplicate", path, "unique_fields", "duplicate"))
        if len(value) < schema.get("minItems", 0):
            result.append(D8FormatIssue("array_too_short", path, "allowed_count", "array"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            result.append(D8FormatIssue("array_too_long", path, "allowed_count", "array"))
        for index, item in enumerate(value):
            if len(result) >= 8:
                break
            result += shape_issues(item, schema["items"], path + "/" + str(index))
    elif actual == "string" and len(value) > schema.get("maxLength", len(value)):
        result.append(D8FormatIssue("string_too_long", path, "allowed_length", "string"))
    elif actual in {"integer", "number"} and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        result.append(D8FormatIssue("number_out_of_range", path, "allowed_range", actual))
    return result[:8]


class _DuplicateKey(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("nonfinite")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite")
    return number


def _object_too_deep(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list, tuple)):
            depth += 1
            if depth > 32:
                return True
            values = item.values() if isinstance(item, dict) else item
            pending.extend((v, depth) for v in values if isinstance(v, (dict, list, tuple)))
    return False


def _depth(text):
    depth = maximum = 0
    quoted = escaped = False
    for c in text:
        if quoted:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c in "[{":
            depth += 1
            maximum = max(maximum, depth)
        elif c in "]}":
            depth -= 1
    return maximum


def decode_response(response, schema):
    """Return safe issues and explicit terminal status without retaining text."""
    finish = "stop"
    if isinstance(response, D8ModelResponse):
        finish, response = response.finish_reason, response.content
    if finish != "stop":
        code = "output_truncated" if finish == "length" else "completion_invalid"
        return None, [D8FormatIssue(code, "$", "complete_response", "incomplete")], code, None
    if is_dataclass(response):
        try:
            response = asdict(response)
        except (TypeError, ValueError, RecursionError):
            return None, [D8FormatIssue("json_value_invalid", "$", "finite_json", "unsupported")], None, None
    if not isinstance(response, str) and _object_too_deep(response):
        return None, [D8FormatIssue("output_depth_exceeded", "$", "bounded_response", "too_deep")], "output_depth_exceeded", None
    try:
        encoded = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False, allow_nan=False)
        units = {"utf8_bytes": len(encoded.encode("utf-8")), "unicode_characters": len(encoded), "tokens": None}
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None, [D8FormatIssue("json_value_invalid", "$", "finite_json", "unsupported")], None, None
    if units["utf8_bytes"] > 65536:
        return None, [D8FormatIssue("output_size_exceeded", "$", "bounded_response", "oversized")], "output_size_exceeded", units
    if _depth(encoded) > 32:
        return None, [D8FormatIssue("output_depth_exceeded", "$", "bounded_response", "too_deep")], "output_depth_exceeded", units
    if isinstance(response, str):
        try:
            response = json.loads(encoded, object_pairs_hook=_pairs, parse_constant=_nonfinite, parse_float=_float)
        except _DuplicateKey:
            return None, [D8FormatIssue("json_duplicate_key", "$", "unique_fields", "duplicate")], None, units
        except (ValueError, RecursionError):
            return None, [D8FormatIssue("json_parse_failed", "$", "valid_json", "invalid")], None, units
    else:
        # Normalize only JSON representation (e.g. immutable arrays), not wrong shapes.
        response = json.loads(encoded)
    try:
        json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError):
        return None, [D8FormatIssue("json_value_invalid", "$", "finite_json", "unsupported")], None, units
    issues = [D8FormatIssue(i.code, i.path if len(i.path) <= 128 else "$", i.expected, i.actual)
              for i in shape_issues(response, schema)]
    return response if not issues else None, issues, None, units
