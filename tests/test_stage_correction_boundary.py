"""Correction feedback must name the boundary the retry has to satisfy."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_core.handoff_contracts import D8HistoryItem
from agent_core.output_contracts import D8FormatIssue, D8ModelResponse
from agent_core.stage_contracts import (
    PlanningSession, StageFactBatch, StageTurnSnapshot, TurnIntent, TurnPlanningContext, TurnPlanningState,
)
from agent_core.stage_planning import (
    StageModelRequest, correction_hint, decode_stage_response, offered_kinds, parse,
    stage_output_issues,
)

TOOL = {"qualified_name": "shell.run_bash",
        "input_schema": {"type": "object", "properties": {"script": {"type": "string"}},
                         "required": ["script"], "additionalProperties": False}}
PUBLIC = {"qualified_name": "web.search",
          "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"], "additionalProperties": False}}


def action_request(*, tools=(TOOL,), pending=(), facts=0, achievement=None):
    task, turn_id, road_id = str(uuid4()), str(uuid4()), str(uuid4())
    observation = D8HistoryItem(str(uuid4()), task, "progress", turn_id, road_id, None,
        "observed bytes", (), "a" * 64, 1, "tool_observation_not_fact", "current", "available")
    history = [observation]
    for index in range(facts):
        history.append(D8HistoryItem(str(uuid4()), task, "verified_fact", turn_id, road_id, None,
            "reviewed conclusion %d" % index, (observation.ref,), "b" * 64, index + 2,
            "reviewed_formal_fact", "current", "available"))
    turn = StageTurnSnapshot(turn_id, TurnIntent("inspect the artifact", "recorded observation"),
        TurnPlanningState.ACTIVE, achievement=achievement)
    context = TurnPlanningContext(task, "identify the artifact",
        PlanningSession("road_execution", road_id), turn, tuple(history),
        pending_operation_ids=tuple(pending))
    return StageModelRequest(str(uuid4()), task, "stage_action", "inner-role", 1, context, tools=tuple(tools))


def claim(request, *, kind="fact_candidate", support=None, content="observed bytes"):
    observation = next(item for item in request.context.history if item.record_type == "progress")
    return {"kind": kind, "content": content, "reason": "recorded from the original tool output",
            "support_refs": list(support if support is not None else [observation.ref]), "inferred": False}


def test_batch_items_may_repeat_the_sibling_tag():
    request = action_request(tools=(TOOL, PUBLIC))
    item = claim(request)
    item.pop("kind")
    response = D8ModelResponse(json.dumps({"kind": "fact_candidates", "candidates": [
        {"kind": "fact_candidate", **item}]}))
    parsed = parse(request, response)
    assert isinstance(parsed, StageFactBatch) and len(parsed.candidates) == 1
    # The tag is still constrained, so a wrong value stays a format error.
    wrong = D8ModelResponse(json.dumps({"kind": "fact_candidates", "candidates": [
        {"kind": "something_else", **item}]}))
    with pytest.raises(ValueError, match="stage.output_invalid"):
        parse(request, wrong)


def test_batch_items_stay_closed_without_the_tag():
    request = action_request(tools=(TOOL, PUBLIC))
    item = claim(request)
    item.pop("kind")
    response = D8ModelResponse(json.dumps({"kind": "fact_candidates", "candidates": [
        {**item, "extra": "not declared"}]}))
    with pytest.raises(ValueError, match="stage.output_invalid"):
        parse(request, response)


def test_hint_names_the_kinds_and_why_one_is_absent():
    request = action_request(tools=(TOOL, PUBLIC), pending=("operation-1",))
    kinds = offered_kinds(request)
    assert "mcp_calls" not in kinds and "wait_for_tools" in kinds
    hint = correction_hint(request, [D8FormatIssue("constant_mismatch", "$/result/kind", "fixed_value", "string")])
    assert ", ".join(kinds) in hint
    assert "mcp_calls is unavailable while work is pending" in hint
    assert "turn_completion is unavailable" in hint


def test_hint_lists_the_citable_fact_sources():
    request = action_request(tools=(TOOL, PUBLIC))
    observation = next(item for item in request.context.history if item.record_type == "progress")
    hint = correction_hint(request, [D8FormatIssue("stage.fact_source_ineligible",
        "$/result/support_refs/0", "current_formal_fact_or_own_road_tool_observation", "not_in_frozen_history")])
    assert observation.ref in hint


def test_hint_explains_a_batch_conflict():
    request = action_request(tools=(TOOL, PUBLIC))
    hint = correction_hint(request, [D8FormatIssue("stage.tool_batch_resource_conflict",
        "$/result/calls", "at_most_one_sandbox_or_target_action", "two_private_actions")])
    assert "at most one sandbox or target action" in hint


def test_hint_explains_a_textual_tool_invocation():
    request = action_request(tools=(TOOL, PUBLIC))
    hint = correction_hint(request, [D8FormatIssue("output_trailing_markup", "$", "single_complete_json", "invalid")])
    assert "submit_stage_result exactly once" in hint


def test_hint_bounds_a_truncated_generation():
    request = action_request(tools=(TOOL, PUBLIC))
    hint = correction_hint(request, [D8FormatIssue("output_truncated", "$", "complete_response", "incomplete")])
    assert "ended at the output limit" in hint


def test_hint_repeats_the_length_budget():
    request = action_request(tools=(TOOL, PUBLIC))
    hint = correction_hint(request, [D8FormatIssue("string_too_long", "$/result/content",
        "unicode_codepoints_le_256", "unicode_codepoints_342")])
    assert "256 code points" in hint and "reason" in hint


def test_hint_is_absent_for_unrelated_issues():
    request = action_request(tools=(TOOL, PUBLIC))
    assert correction_hint(request, []) == ""
    assert correction_hint(request, [D8FormatIssue("field_missing", "$/result/reason", "string", "missing")]) == ""


def test_wrapped_text_failure_reports_the_inner_path():
    request = action_request(tools=(TOOL, PUBLIC))
    value = claim(request, support=["verified_fact:absent:" + "0" * 64])
    response = D8ModelResponse(json.dumps({"result": value}))
    _, issues, _, _ = decode_stage_response(request, response)
    assert issues and all(issue.path.startswith("$/result") for issue in issues)
    business = stage_output_issues(request, response)
    assert [issue.code for issue in business] == ["stage.fact_source_ineligible"]
    assert business[0].path == "$/support_refs/0"


def test_unwrapped_text_failure_is_unchanged():
    request = action_request(tools=(TOOL, PUBLIC))
    response = D8ModelResponse(json.dumps(claim(request)))
    assert parse(request, response).content == "observed bytes"
