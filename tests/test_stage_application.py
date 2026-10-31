"""U3 real controller/SQLite/evidence/MCP executor, controlled model and tool IO."""

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.stage_application import StageApplication
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_core.contracts import TaskRequest, ModelBackend
from agent_core.actions import MCPCall
from agent_core.domain import IntentStatus
from agent_core.control_store import TargetNetworkBinding
from agent_core.handoff_contracts import D8HandoffConfig, D8CapacityPolicy
from agent_core.dual_llm_contracts import D3RunConfig, D3Authorization, ExecutionRecoveryRequired
from agent_core.operation_accounting import D8HandoffBudgetPolicy, OperationRecoveryRequired
from agent_core.output_contracts import D8ModelResponse
from agent_core.stage_contracts import PlanningSession, StageStateChange
from agent_core.stage_contracts import TurnCompletion
from agent_core.stage_contracts import (TurnIntent, RoadCandidate, RoadResolution, PlanningExhausted,
    StageFactProposal, StagePlanningDecision, TurnPlanningContext)
from agent_core.stage_planning import StageModelRequest, schema, parse, OUTPUT_TOOL
from agent_core.stage_contracts import StageTurnSnapshot, StageReviewBinding, TurnPlanningState
from agent_core.stage_storage import StageStateError
from agent_core.turn_contracts import D1RoleConfig
from agent_core.web_contracts import D7WebAuthorization
from MCPs.Mcp_client.client import MCPExecutionResult


class Model:
    def __init__(self, role):
        self.role_config, self.calls = role, []
        self.transform = lambda request, raw: raw
        self.complete = True
        self.native = False
        self.envelopes = []

    def invoke(self, envelope, timeout, token):
        request = envelope.request
        self.envelopes.append(envelope)
        self.calls.append(request)
        stage = request.stage
        history = request.context.history
        facts = [i.ref for i in history if i.record_type == "verified_fact"]
        observations = [i.ref for i in history if i.record_type == "progress"]
        if stage == "stage_intent":
            raw = {"kind": "turn_intent", "description": "inspect the controlled result",
                "completion_criterion": "a supported result is available", "support_refs": []}
        elif stage == "stage_roads":
            if request.context.turn.roads:
                raw = {"kind": "exhausted", "reason": "no further route", "support_refs": facts or observations}
            else:
                raw = {"kind": "roads", "roads": [{"description": "inspect using the existing tool", "support_refs": []}]}
        elif stage == "stage_review":
            raw = {"decision": "accept", "reason": "supported within its declared criterion"}
            if isinstance(request.proposal, tuple):
                raw["accepted_road_indices"] = [0]
        elif stage == "stage_completion":
            raw = {"kind": "turn_completion" if self.complete else "incomplete", "reason": "criterion checked",
                "support_refs": facts}
        elif stage == "stage_continuation":
            raw = {"kind": "request_final_validation", "reason": "handoff to independent validation", "support_refs": facts}
        elif stage == "stage_supervision":
            raw = {"decision": "continue_current_intent", "reason": "check the remaining criterion", "support_refs": observations}
        elif stage == "stage_summary":
            raw = {"content": "Unverified session index: the controlled observations remain preserved; no result is TaskRun success."}
        elif not observations:
            raw = {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf observed"}}
        elif not facts:
            raw = {"kind": "fact_candidate", "content": "The controlled tool returned observed.",
                "reason": "direct observation", "support_refs": observations, "inferred": False}
        else:
            raw = {"kind": "road_resolution", "outcome": "succeeded", "reason": "local result obtained", "support_refs": facts}
        value = self.transform(request, raw)
        return native_response(value) if self.native else D8ModelResponse(json.dumps(value))


def native_response(value):
    return D8ModelResponse("", "tool_calls", ({"id": "call_" + uuid4().hex, "type": "function",
        "function": {"name": OUTPUT_TOOL, "arguments": json.dumps({"result": value})}},))


class Tools:
    def __init__(self):
        self.calls = []
        self.task_id = None
        self.error = None
        self.specs = [{"qualified_name": "shell.run_bash", "description": "controlled tool",
            "tags": ["shell"], "input_schema": {"type": "object", "properties": {"script": {"type": "string"}},
                "required": ["script"], "additionalProperties": False}}]

    async def start_all(self): pass
    async def stop_all(self): pass
    def list_registered_tools(self): return [SimpleNamespace(qualified_name="shell.run_bash")]
    def bind_agent(self, agent_id, *, allowed_tools):
        assert agent_id == self.task_id and allowed_tools == ["shell.run_bash"]
        return self
    def list_tool_specs(self): return self.specs
    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return MCPExecutionResult(self.task_id, "controlled-sandbox", name, "bash", ("-lc", arguments["script"]),
            "completed", 0, "observed", "", False)


class Sandbox:
    def create(self, task_id): return "controlled-sandbox"
    def destroy(self, sandbox_id): pass
    def configure_target_network(self, task_id, binding): pass


class Network:
    def create(self, task_id, url):
        return TargetNetworkBinding("a" * 64, "b" * 64, "c" * 64, "d" * 64, url)
    def bind(self, task_id, binding, *, terminal): pass
    def destroy(self, task_id, binding): pass


@pytest.fixture
def case(tmp_path):
    outer, inner = (D1RoleConfig(role, "fake", "test", "test") for role in ("outer_planning", "inner_action"))
    authorization_id = str(uuid4())
    config = D3RunConfig(outer, inner, D3Authorization(authorization_id, ()), outer.role_config_ref,
        contract_version=22, d4_budget=D8HandoffBudgetPolicy(),
        d5_enabled=True, d5_sensitive_requirements=(), d8_enabled=True,
        handoff_config=D8HandoffConfig(D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 1000000, 10000),
            D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 1000000, 10000)),
        web_authorization=D7WebAuthorization(authorization_id, "http://example.test:8080", ("GET",)),
        http_timeout_seconds=60, intent_execution_policy="continuous_v1")
    tools = Tools()
    models = SimpleNamespace(inner=Model(inner), outer=Model(outer))
    now = SimpleNamespace(value=1800000000.0)
    args = dict(config=config, sandbox=Sandbox(), workspace_sandbox=SimpleNamespace(),
        inner_model=models.inner, outer_model=models.outer, clock=lambda: now.value, mcp_manager=tools,
        capacity_port=FakeUnicodeCapacity(), target_network_adapter=Network())
    app = StageApplication(tmp_path, **args)
    request = TaskRequest.from_dict({"goal": "check the controlled result", "deadline_seconds": 3600,
        "target": {"endpoints": [{"url": "http://example.test:8080"}]}})
    app.start(request, effective_model_backend=ModelBackend.CODEX_CLI)
    tools.task_id = app.task_id
    case = SimpleNamespace(app=app, models=models, tools=tools, now=now, args=args, path=tmp_path,
        token=SimpleNamespace(is_cancelled=False))
    yield case
    asyncio.run(case.app.close())


def test_single_turn_actual_mcp_path_and_single_canonical_fact(case):
    result = asyncio.run(case.app.run(case.token))
    assert result.reason == "stage.final_validation_requested"
    assert result.turn.achievement is not None
    assert result.turn.roads[0].status is IntentStatus.SUCCEEDED
    assert len(case.tools.calls) == 1
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1
    assert case.app.state.task_run.status.value == "running"
    assert {r.stage for r in case.models.inner.calls} >= {"stage_intent", "stage_roads", "stage_action", "stage_completion"}
    assert all(r.stage == "stage_review" for r in case.models.outer.calls)
    turn_session = PlanningSession("turn_planning", result.turn.turn_id)
    road_session = PlanningSession("road_execution", result.turn.roads[0].road_id)
    assert case.app.store.load_stage_session(case.app.task_id, turn_session) != case.app.store.load_stage_session(case.app.task_id, road_session)
    for request in case.models.inner.calls:
        if request.stage != "stage_action":
            assert request.context.session == turn_session
        else:
            assert request.context.session == road_session
    assert any(request.messages for request in case.models.inner.calls if request.stage == "stage_action")


def test_challenge_category_reaches_inner_outer_and_actual_payload(case):
    from agent_adapters.workbench_runner import challenge_goal
    from agent_adapters.deepseek_roles import build_payload, role_config
    goal = challenge_goal({"category": "pwn", "description": "原始题目描述保持完整。"})
    request = replace(case.app.state.task_run.request, goal=goal)
    asyncio.run(case.app.close())
    case.path = case.path / "category"
    case.app = StageApplication(case.path, **case.args)
    case.app.start(request, effective_model_backend=ModelBackend.CODEX_CLI)
    case.tools.task_id = case.app.task_id
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert case.app.state.task_run.request.goal == goal
    for model in (case.models.inner, case.models.outer):
        assert model.envelopes and all(e.request.context.goal == goal for e in model.envelopes)
        envelope = model.envelopes[0]
        role = role_config(model.role_config.role, version=17)
        outgoing = replace(envelope, request=replace(envelope.request, role_config_ref=role.role_config_ref))
        payload = build_payload(outgoing, role)
        wire = json.dumps(payload, ensure_ascii=False)
        assert "题型（任务提供）：pwn" in wire and "原始题目描述保持完整。" in wire
        assert "可依据证据修正的分析背景" in wire
        assert payload["reasoning_effort"] == "high"


def test_road_success_does_not_imply_stage_success(case):
    case.models.inner.complete = False
    result = asyncio.run(case.app.run(case.token))
    assert result.reason == "stage.exhausted"
    assert result.turn.achievement is None
    assert result.turn.roads[0].status is IntentStatus.SUCCEEDED


def test_outer_rejection_retires_provisional_identity_and_feedback_stays_in_root_session(case):
    seen = []
    def review(request, raw):
        if not seen:
            seen.append(request)
            return {"decision": "revise", "reason": "state a clearer criterion"}
        return raw
    case.models.outer.transform = review
    result = asyncio.run(case.app.run(case.token))
    root_requests = [r for r in case.models.inner.calls if r.stage == "stage_intent"]
    assert len(root_requests) == 2
    assert root_requests[0].context.session == root_requests[1].context.session
    assert "state a clearer criterion" in json.dumps(root_requests[1].messages)
    previous, current = case.app.store.list_stage_turns(case.app.task_id)
    assert previous.planning_state is TurnPlanningState.SUPERSEDED
    assert current.turn_id == result.turn.turn_id != previous.turn_id
    assert 1 <= len(case.tools.calls) <= 2


def test_known_review_after_process_loss_is_applied_without_recalling_models(case, monkeypatch):
    commit = case.app._commit
    stopped = []
    def interrupted(change, **kwargs):
        if change.facts and not stopped:
            stopped.append(True)
            raise RuntimeError("controlled process loss before applying reviewed Fact")
        return commit(change, **kwargs)
    monkeypatch.setattr(case.app, "_commit", interrupted)
    with pytest.raises(RuntimeError, match="controlled process loss"):
        asyncio.run(case.app.run(case.token))
    calls = len(case.models.inner.calls), len(case.models.outer.calls)
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 1
    # Only Road end and stage completion remain; Fact proposal/review are restored.
    assert len(case.models.inner.calls) == calls[0] + 3
    assert len(case.models.outer.calls) == calls[1] + 3


def test_unknown_tool_is_not_replayed_after_restart(case):
    case.tools.error = RuntimeError("controlled lost response")
    with pytest.raises(Exception, match="mcp.outcome_unknown"):
        asyncio.run(case.app.run(case.token))
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    with pytest.raises(OperationRecoveryRequired):
        asyncio.run(case.app.run(case.token))
    assert len(case.tools.calls) == 1


def test_known_tool_result_before_checkpoint_is_not_dispatched_again(case, monkeypatch):
    commit = case.app._commit
    stopped = []
    def interrupted(change, **kwargs):
        if case.tools.calls and not stopped:
            stopped.append(True)
            raise RuntimeError("controlled loss after tool result")
        return commit(change, **kwargs)
    monkeypatch.setattr(case.app, "_commit", interrupted)
    with pytest.raises(RuntimeError, match="controlled loss"):
        asyncio.run(case.app.run(case.token))
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 1


@pytest.fixture
def review_request():
    context = TurnPlanningContext("task", "goal", PlanningSession("turn_planning", "turn"), None, ())
    return StageModelRequest("review", "task", "stage_review", "outer", 0, context)


@pytest.mark.parametrize("proposal", [
    TurnIntent("stage", "criterion"), TurnCompletion("supported", ()),
    PlanningExhausted("no route", ()), RoadResolution(IntentStatus.SUCCEEDED, "done", ()),
    StageFactProposal("observation", "supported", ()),
    StagePlanningDecision("return_to_parent", "blocked", ()),
    StagePlanningDecision("request_final_validation", "ready", ()),
])
def test_non_road_review_never_advertises_or_accepts_road_selection(review_request, proposal):
    request = replace(review_request, proposal=proposal)
    assert "accepted_road_indices" not in schema(request)["properties"]
    for decision in ("accept", "revise", "reject"):
        raw = {"decision": decision, "reason": "reviewed"}
        assert parse(request, D8ModelResponse(json.dumps(raw))).accepted_road_indices == ()
        with pytest.raises(ValueError, match="stage.output_invalid"):
            parse(request, D8ModelResponse(json.dumps({**raw, "accepted_road_indices": [0]})))


def test_fact_review_scope_and_revision_reason_limit_remain_distinct(review_request):
    from agent_core.conclusion_revision_contracts import D8RevisionTarget
    from agent_core.stage_contracts import StageConclusionChange
    from agent_core.stage_planning import model_contract
    request = replace(review_request, proposal=StageFactProposal("a supported partial observation", "direct source", ()))
    rules = model_contract(request)["rules"]
    assert "not the whole Turn completion criterion" in rules
    assert "Accept a supported partial observation" in rules
    assert "an accepted inference remains inference" in rules
    long_review = {"decision": "accept", "reason": "r" * 2000}
    assert parse(request, long_review).reason == long_review["reason"]
    revision = StageConclusionChange("withdraw", (D8RevisionTarget(str(uuid4()), 1, "existing-version"),),
        None, None, (), "source withdrawn", None, None, None)
    request = replace(request, proposal=revision)
    with pytest.raises(ValueError, match="stage.output_invalid"):
        parse(request, long_review)
    assert parse(request, {"decision": "accept", "reason": "checked the revision"}).decision == "accept"


@pytest.mark.parametrize("decision,selection,valid", [
    ("accept", [1, 0], True), ("accept", None, False), ("accept", [], False),
    ("accept", [2], False), ("accept", [0, 0], False), ("accept", [-1], False),
    ("revise", None, True), ("reject", [], True), ("reject", [0], False),
])
def test_road_review_schema_and_parser_agree_on_selection(review_request, decision, selection, valid):
    from agent_core.output_contracts import decode_response
    request = replace(review_request, proposal=(RoadCandidate("first"), RoadCandidate("second")))
    raw = {"decision": decision, "reason": "reviewed"}
    if selection is not None:
        raw["accepted_road_indices"] = selection
    response = D8ModelResponse(json.dumps(raw))
    _, issues, terminal, _ = decode_response(response, schema(request))
    assert (not issues and not terminal) == valid
    if valid:
        assert parse(request, response).accepted_road_indices == tuple(selection or ())
    else:
        with pytest.raises(ValueError, match="stage.output_invalid"):
            parse(request, response)


def test_stable_provider_indices_do_not_relax_local_selection_range(review_request):
    from agent_core.output_contracts import decode_response
    from agent_core.stage_planning import provider_schema
    request = replace(review_request, proposal=(RoadCandidate("first"), RoadCandidate("second")))
    value = {"decision": "accept", "reason": "select original candidates", "accepted_road_indices": [99]}
    assert not decode_response(value, provider_schema(request))[1]
    with pytest.raises(ValueError, match="stage.output_invalid"):
        parse(request, value)


def test_schema_guided_turn_review_needs_no_format_correction(case):
    def transform(request, raw):
        if request.stage == "stage_review" and isinstance(request.proposal, TurnIntent):
            raw.pop("accepted_road_indices", None)
            if "accepted_road_indices" in schema(request).get("properties", {}):
                raw["accepted_road_indices"] = [0]
        return raw
    case.models.outer.transform = transform
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    reviews = [r for r in case.models.outer.calls if isinstance(r.proposal, TurnIntent)]
    assert len(reviews) == 1 and reviews[0].correction_of_model_request_id is None


@pytest.mark.parametrize("arguments,valid", [
    ({}, True), ({"script": "observed", "symbols": {"entry": "value"}}, True),
    ({"script": 7}, False), ({"symbols": {"entry": 7}}, False),
    ({"extra": "not declared"}, False),
])
def test_stage_tool_schema_preserves_optional_fields_and_local_refs(review_request, arguments, valid):
    tool_schema = {"type": "object", "$defs": {"symbol": {"type": "string"}},
        "properties": {"script": {"type": "string"}, "symbols": {
            "type": "object", "additionalProperties": {"$ref": "#/$defs/symbol"}}},
        "additionalProperties": False}
    request = replace(review_request, stage="stage_action", tools=(
        {"qualified_name": "shell.run_bash", "input_schema": tool_schema},))
    response = D8ModelResponse(json.dumps({"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": arguments}))
    if valid:
        assert parse(request, response).arguments == arguments
    else:
        with pytest.raises(ValueError, match="stage.output_invalid"):
            parse(request, response)
    assert "$schema" not in tool_schema  # Advertised tool metadata stays unchanged.


@pytest.mark.parametrize("invalid", [False, True])
def test_optional_tool_parameters_reach_execution_and_stage_completion(case, invalid):
    case.tools.specs[0]["input_schema"].pop("required")
    if invalid:
        case.models.inner.transform = lambda request, raw: ({**raw, "arguments": {"script": 7}}
            if raw.get("kind") == "mcp_call" else raw)
        with pytest.raises(StageStateError, match="model_output_unusable") as error:
            asyncio.run(case.app.run(case.token))
        assert error.value.category == "input" and not case.tools.calls
        actions = [r for r in case.models.inner.calls if r.stage == "stage_action"]
        assert len(actions) == 3 and actions[1].correction_of_model_request_id == actions[0].model_request_id
        assert all(op.completion is not None for op in case.app.store.list_d4_operations(case.app.task_id))
        return
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 1
    assert not any(r.correction_of_model_request_id for r in case.models.inner.calls)


@pytest.mark.parametrize("repeat", [False, True])
def test_wrapped_stage_output_remains_invalid_and_correction_is_bounded(case, repeat, monkeypatch):
    from agent_adapters import stage_evidence
    seen = []
    logger = SimpleNamespace(info=lambda *a, **kw: None,
        warning=lambda event, **kw: seen.append((event, kw)))
    monkeypatch.setattr(stage_evidence, "get_logger", lambda _: logger)
    def transform(request, raw):
        if request.stage == "stage_intent" and (repeat or not request.correction_of_model_request_id):
            return {"version": 1, "stage": request.stage, "schema": raw}
        return raw
    case.models.inner.transform = transform
    if repeat:
        with pytest.raises(StageStateError, match="model_output_unusable"):
            asyncio.run(case.app.run(case.token))
        assert not case.tools.calls and not case.models.outer.calls
    else:
        assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    intents = [r for r in case.models.inner.calls if r.stage == "stage_intent"]
    assert len(intents) == (3 if repeat else 2) and intents[1].correction_of_model_request_id == intents[0].model_request_id
    invalid = [entry for event, entry in seen if event == "stage.output_rejected"]
    assert len(invalid) == (3 if repeat else 1)
    assert all(i["code"] == "stage.output_envelope_invalid" and i["category"] == "input"
        and i["phase"] == "stage_intent" for i in invalid)


def test_bounded_format_correction_preserves_scope_and_role(case):
    malformed = []
    def transform(request, raw):
        if request.stage == "stage_roads" and not malformed:
            malformed.append(request)
            return {"kind": "roads", "roads": "wrong type"}
        return raw
    case.models.inner.transform = transform
    result = asyncio.run(case.app.run(case.token))
    requests = [r for r in case.models.inner.calls if r.stage == "stage_roads"]
    assert len(requests) == 2 and requests[1].correction_of_model_request_id == requests[0].model_request_id
    assert requests[1].context == requests[0].context
    assert result.turn.achievement is not None


@pytest.mark.parametrize("native", [False, True])
def test_correction_receives_frozen_exact_length_feedback(case, native):
    case.models.inner.native = native
    def transform(request, raw):
        if raw.get("kind") == "fact_candidate" and not request.correction_of_model_request_id:
            return {**raw, "content": "a" * 806}
        return raw
    case.models.inner.transform = transform
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    corrected = [e for e in case.models.inner.envelopes if e.correction]
    assert corrected
    for envelope in corrected:
        feedback = envelope.correction
        assert feedback["correction_of_model_request_id"] == envelope.request.correction_of_model_request_id
        issue = feedback["issues"][0]
        assert issue["code"] == "string_too_long" and issue["path"] == ("$/result/content" if native else "$/content")
        assert issue["expected"] == "unicode_codepoints_le_256" and issue["actual"] == "unicode_codepoints_806"
        frozen = case.app.evidence.read("contract", envelope.request.model_request_id)
        assert frozen["correction"] == feedback
        assert "a" * 806 not in json.dumps(feedback)
        from agent_adapters.deepseek_roles import build_payload, role_config
        role = role_config("inner_action", version=17)
        outgoing = replace(envelope, request=replace(envelope.request, role_config_ref=role.role_config_ref))
        payload = build_payload(outgoing, role)
        assert '"actual": "unicode_codepoints_806"' in payload["messages"][-2]["content"]


def test_native_output_preserves_raw_identity_and_canonical_history(case):
    case.models.inner.native = case.models.outer.native = True
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len(case.tools.calls) == 1
    for model in (case.models.inner, case.models.outer):
        for request in model.calls:
            result = case.app.evidence.read("result", request.model_request_id)
            assert result["finish_reason"] == "tool_calls" and result["content"] == ""
            assert len(result["tool_calls"]) == 1
            assert result["tool_calls"][0]["function"]["name"] == OUTPUT_TOOL
            operation = case.app.store.load_d4_operation(request.model_request_id)
            assert case.app.evidence.read_model_result(operation).result is not None
    assert any('"kind": "mcp_call"' in m["content"] for r in case.models.inner.calls
        for m in r.messages if m["role"] == "assistant")


def test_correction_cannot_rebind_a_different_stage_scope(case):
    case.models.inner.transform = lambda request, raw: ({"bad": "shape"}
        if request.stage == "stage_intent" and not request.correction_of_model_request_id else raw)
    asyncio.run(case.app.run(case.token))
    request = next(e.request for e in case.models.inner.envelopes if e.correction)
    calls = len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls)
    changed = replace(request, context=replace(request.context, goal="different goal"))
    with pytest.raises(StageStateError, match="correction_binding_changed"):
        case.app.evidence.contract(changed)
    assert calls == (len(case.models.inner.calls), len(case.models.outer.calls), len(case.tools.calls))


@pytest.mark.parametrize("stage", ["stage_action", "stage_review"])
@pytest.mark.parametrize("repeat", [False, True])
def test_complete_multi_call_stream_shares_two_corrections_without_dispatch(case, monkeypatch, stage, repeat):
    import io
    from agent_adapters.model_stream import read_chat_stream
    from agent_adapters.web.task_result import _stage_task_result
    from test_model_stream import chunk
    model = case.models.inner if stage == "stage_action" else case.models.outer
    model.native = True
    original = model.invoke
    roots, reports = [], []
    def invoke(envelope, timeout, token):
        response = original(envelope, timeout, token)
        request = envelope.request
        if request.stage != stage:
            return response
        if not roots:
            roots.append(request.model_request_id)
        if request.model_request_id != roots[0] and request.correction_of_model_request_id is None:
            return response
        calls = list(response.tool_calls)
        if repeat or request.correction_of_model_request_id is None:
            calls.append({**calls[0], "id": "second_unapplied_call"})
        raw = b"".join(chunk({"tool_calls": [{"index": i, **call}]}) for i, call in enumerate(calls))
        raw += chunk({}, "tool_calls", usage={"prompt_tokens": 42, "completion_tokens": 12}) + b"data: [DONE]\n\n"
        data, _ = read_chat_stream(io.BytesIO(raw), 65536, lambda **v: reports.append(v), allow_tool_calls=True)
        message = data["choices"][0]["message"]
        return D8ModelResponse(message["content"], data["choices"][0]["finish_reason"], tuple(message["tool_calls"]))
    monkeypatch.setattr(model, "invoke", invoke)
    if repeat:
        with pytest.raises(StageStateError, match="model_output_unusable") as error:
            asyncio.run(case.app.run(case.token))
        assert error.value.category == "input" and not case.tools.calls
    else:
        assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
        assert len(case.tools.calls) == 1
    corrected = [e for e in model.envelopes if e.request.stage == stage and e.request.correction_of_model_request_id]
    assert len(corrected) == (2 if repeat else 1)
    assert corrected[0].correction["reason"] == "output_format_invalid"
    assert corrected[0].correction["issues"] == [{"code": "multiple_tool_calls", "path": "$/tool_calls",
        "expected": "exactly_one_output_tool", "actual": "multiple"}]
    first = case.app.evidence.read("result", roots[0])
    assert first["outcome"] == "invalid" and first["finish_reason"] == "tool_calls" and len(first["tool_calls"]) == 2
    assert reports[-1]["finish_seen"] and reports[-1]["done_seen"] and reports[-1]["usage"]["completion_tokens"] == 12
    assert case.app.store.load_d4_operation(roots[0]).completion.result_kind == "failed"
    view = _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="simulated"),
        case.app.store, case.args["config"])
    if repeat:
        assert view["model_failures"][0]["issue_codes"] == ["multiple_tool_calls"]
        assert view["model_failures"][0]["category"] == "input"
        count = len(model.calls)
        with pytest.raises(StageStateError, match="model_output_unusable"):
            asyncio.run(case.app.run(case.token))
        assert len(model.calls) == count and not case.tools.calls
    else:
        assert not view["model_failures"]


@pytest.mark.parametrize("change", ["wrong_name", "two_calls", "oversized_content", "duplicate_key", "stop", "length", "missing_result"])
def test_native_envelope_never_bypasses_local_validation(review_request, change):
    request = replace(review_request, stage="stage_intent")
    response = native_response({"kind": "turn_intent", "description": "stage", "completion_criterion": "criterion", "support_refs": []})
    calls = response.tool_calls
    if change == "wrong_name": calls[0]["function"]["name"] = "shell.run_bash"
    elif change == "two_calls": response = replace(response, tool_calls=calls * 2)
    elif change == "oversized_content": response = replace(response, content="x" * 65537)
    elif change == "duplicate_key": calls[0]["function"]["arguments"] = '{"result":{},"result":{}}'
    elif change in {"stop", "length"}: response = replace(response, finish_reason=change)
    else: calls[0]["function"]["arguments"] = '{"script":"untrusted"}'
    with pytest.raises(ValueError, match="stage.output_invalid"):
        parse(request, response)


@pytest.mark.parametrize("suffix", ["}", " \t}\r\n"])
def test_native_single_trailing_brace_preserves_values_and_requires_policy(review_request, suffix):
    from agent_core.output_contracts import NATIVE_TRAILING_BRACE_POLICY
    request = replace(review_request, stage="stage_intent")
    value = {"kind": "turn_intent", "description": '正文🙂 literal } and "quoted {"',
        "completion_criterion": "criterion", "support_refs": []}
    response = native_response(value)
    arguments = response.tool_calls[0]["function"]["arguments"] + suffix
    response.tool_calls[0]["function"]["arguments"] = arguments
    diagnostics = {}
    assert parse(request, response, diagnostics=diagnostics).description == value["description"]
    assert response.tool_calls[0]["function"]["arguments"] == arguments
    assert diagnostics["normalization"] == {"policy": NATIVE_TRAILING_BRACE_POLICY,
        "offset": arguments.rfind("}"),
        "removed_characters": 1}
    with pytest.raises(ValueError):
        parse(request, response, normalization_policy=None)
    with pytest.raises(ValueError):
        parse(request, D8ModelResponse(json.dumps(value) + suffix))


@pytest.mark.parametrize("change", ["two_braces", "second_json", "text", "markup", "unicode_whitespace", "missing_brace",
    "duplicate_key", "nonfinite", "overflow", "length", "stop", "oversized", "deep", "wrong_shape"])
def test_native_trailing_brace_never_repairs_other_invalid_outputs(review_request, change):
    request = replace(review_request, stage="stage_intent")
    value = {"kind": "turn_intent", "description": "stage", "completion_criterion": "criterion", "support_refs": []}
    response = native_response(value)
    arguments = response.tool_calls[0]["function"]["arguments"]
    if change in {"two_braces", "second_json", "text", "markup", "unicode_whitespace"}:
        arguments += {"two_braces": "}}", "second_json": "{}", "text": "} execute",
            "markup": "}</||DSML|| parameter>", "unicode_whitespace": "}\u00a0"}[change]
    elif change == "missing_brace": arguments = arguments[:-1]
    elif change == "duplicate_key": arguments = '{"result":' + json.dumps(value) + ',"result":{}}}'
    elif change in {"nonfinite", "overflow"}:
        arguments = arguments[:-1] + ',"extra":' + ("NaN" if change == "nonfinite" else "1e400") + '}}'
    elif change in {"length", "stop"}:
        response = replace(response, finish_reason=change)
        arguments += "}"
    elif change == "oversized": arguments = json.dumps({"result": {**value, "description": "x" * 65537}}) + "}"
    elif change == "deep": arguments = '{"result":' + '[' * 33 + '0' + ']' * 33 + '}}'
    else: arguments = '{"result":{"kind":"not_allowed"}}}'
    response.tool_calls[0]["function"]["arguments"] = arguments
    diagnostics = {}
    with pytest.raises(ValueError):
        parse(request, response, diagnostics=diagnostics)
    assert not diagnostics


@pytest.mark.parametrize("stage", ["stage_action", "stage_review"])
def test_native_trailing_brace_audited_without_correction_and_reloads(case, monkeypatch, stage):
    from agent_adapters.stage_evidence import StageEvidence
    from agent_adapters import stage_evidence
    model = case.models.inner if stage == "stage_action" else case.models.outer
    model.native = True
    original, events = model.invoke, []
    def invoke(envelope, timeout, token):
        response = original(envelope, timeout, token)
        if envelope.request.stage == stage:
            response.tool_calls[0]["function"]["arguments"] += "}"
        return response
    logger = SimpleNamespace(info=lambda event, **fields: events.append((event, fields)),
        warning=lambda *args, **fields: None)
    monkeypatch.setattr(model, "invoke", invoke)
    monkeypatch.setattr(stage_evidence, "get_logger", lambda _: logger)
    assert asyncio.run(case.app.run(case.token)).turn.achievement
    assert len(case.tools.calls) == 1
    assert not any(r.correction_of_model_request_id for r in case.models.inner.calls + case.models.outer.calls)
    reader = StageEvidence(case.path / "stage-evidence", case.app.store, create=False)
    for request in (r for r in model.calls if r.stage == stage):
        result = case.app.evidence.read("result", request.model_request_id)
        assert result["outcome"] == "valid" and result["tool_calls"][0]["function"]["arguments"].endswith("}}")
        assert result["normalization"]["removed_characters"] == 1
        assert reader.read_model_result(case.app.store.load_d4_operation(request.model_request_id)).result
        record = next(fields for event, fields in events
            if event == "stage.output_normalized" and fields["operation"] == request.model_request_id)
        assert record["offset"] == result["normalization"]["offset"]
        assert record["code"] == "json_trailing_data_trimmed"


def test_native_trailing_brace_correction_has_position_and_still_blocks_illegal_tool(case, monkeypatch):
    model = case.models.inner
    model.native = True
    original = model.invoke
    def invoke(envelope, timeout, token):
        response = original(envelope, timeout, token)
        if envelope.request.stage == "stage_action":
            if envelope.request.correction_of_model_request_id:
                value = {"kind": "mcp_call", "tool_name": "shell.not_registered", "arguments": {"script": "never run"}}
                response = native_response(value)
                response.tool_calls[0]["function"]["arguments"] += "}"
            else:
                response.tool_calls[0]["function"]["arguments"] += "}}"
        return response
    monkeypatch.setattr(model, "invoke", invoke)
    with pytest.raises(StageStateError, match="stage.model_output_unusable"):
        asyncio.run(case.app.run(case.token))
    requests = [r for r in model.calls if r.stage == "stage_action"]
    assert len(requests) == 3 and requests[1].correction_of_model_request_id == requests[0].model_request_id
    issue = next(e.correction["issues"][0] for e in model.envelopes if e.request == requests[1])
    assert issue["code"] == "json_trailing_data" and issue["path"] == "$/tool_calls/0/function/arguments"
    assert issue["actual"].startswith("trailing_data_at_char_")
    assert issue["actual"].endswith(";extra_right_braces_2")
    assert not case.tools.calls
    assert all(case.app.evidence.read("result", r.model_request_id)["outcome"] == "invalid" for r in requests)
    assert all("normalization" not in case.app.evidence.read("result", r.model_request_id) for r in requests)


def test_native_trailing_brace_absent_frozen_policy_keeps_legacy_strict(case, monkeypatch):
    from agent_adapters import stage_evidence
    from agent_adapters.stage_evidence import StageEvidence
    model, contract = case.models.inner, stage_evidence.model_contract
    model.native = True
    original, roots = model.invoke, []
    def legacy_contract(request):
        value = contract(request)
        value.pop("output_normalization")
        return value
    def invoke(envelope, timeout, token):
        response = original(envelope, timeout, token)
        if envelope.request.stage == "stage_action" and not roots:
            roots.append(envelope.request.model_request_id)
            response.tool_calls[0]["function"]["arguments"] += "}"
        return response
    with monkeypatch.context() as patch:
        patch.setattr(stage_evidence, "model_contract", legacy_contract)
        patch.setattr(model, "invoke", invoke)
        assert asyncio.run(case.app.run(case.token)).turn.achievement
    reader = StageEvidence(case.path / "stage-evidence", case.app.store, create=False)
    old = reader.read("result", roots[0])
    assert old["outcome"] == "invalid" and "normalization" not in old
    corrected = next(r for r in model.calls if r.correction_of_model_request_id == roots[0])
    assert "output_normalization" not in reader.read("contract", corrected.model_request_id)
    assert reader.read_model_result(case.app.store.load_d4_operation(corrected.model_request_id)).result
    with pytest.raises(StageStateError, match="stage.original_evidence_changed"):
        reader.read_model_result(case.app.store.load_d4_operation(roots[0]))
    assert reader.read("result", roots[0]) == old and len(case.tools.calls) == 1


def test_multi_call_diagnostic_does_not_hide_response_limit(review_request):
    from agent_core.stage_planning import decode_stage_response
    response = native_response({"decision": "accept", "reason": "controlled"})
    response = replace(response, content="x" * 65537, tool_calls=response.tool_calls * 2)
    _, issues, terminal, _ = decode_stage_response(review_request, response)
    assert terminal == "output_size_exceeded" and issues[0].code == "output_size_exceeded"


def test_premature_completion_is_corrected_before_outer_and_then_creates_fact(case):
    case.models.inner.native = case.models.outer.native = True
    rejected = []
    def transform(request, raw):
        observations = [i.ref for i in request.context.history if i.record_type == "progress"]
        if request.stage == "stage_action" and observations and not rejected:
            rejected.append(request.model_request_id)
            return {"kind": "turn_completion", "reason": "premature", "support_refs": observations[:1]}
        return raw
    case.models.inner.transform = transform
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 1
    corrected = next(e for e in case.models.inner.envelopes if e.request.correction_of_model_request_id == rejected[0])
    assert corrected.correction["reason"] == "output_business_invalid"
    assert corrected.correction["issues"][0]["code"] == "stage.formal_fact_required"
    assert not any(e.request.proposal_operation_id == rejected[0] for e in case.models.outer.envelopes)
    assert any(i.record_type == "verified_fact" for i in case.app.evidence.history(case.app.task_id))


@pytest.mark.parametrize("source_kind", ["import", "other_road", "feedback", "inference", "formal_only", "own_tool"])
def test_fact_sources_are_checked_before_review(source_kind):
    from agent_core.handoff_contracts import D8HistoryItem
    from agent_core.stage_planning import stage_output_issues
    task, turn_id, road_id = (str(uuid4()) for _ in range(3))
    own = D8HistoryItem(str(uuid4()), task, "progress", turn_id, road_id, None,
        "observed", (), "a" * 64, 1, "tool_observation_not_fact", "current", "available")
    extra = {
        "import": replace(own, record_id="import/" + str(uuid4()), turn_id=None, intent_id=None, identity="imported_attachment_not_fact"),
        "other_road": replace(own, record_id=str(uuid4()), intent_id=str(uuid4())),
        "feedback": replace(own, record_id="feedback/" + str(uuid4()), identity="reviewed_feedback_not_fact"),
        "inference": replace(own, record_type="reviewed_inference", identity="reviewed_inference_not_verified"),
        "formal_only": replace(own, record_type="verified_fact", identity="reviewed_formal_fact"),
        "own_tool": own,
    }[source_kind]
    turn = StageTurnSnapshot(turn_id, TurnIntent("inspect", "observed"), TurnPlanningState.ACTIVE)
    request = StageModelRequest(str(uuid4()), task, "stage_action", "test-role", 1,
        TurnPlanningContext(task, "inspect", PlanningSession("road_execution", road_id), turn, (own, extra)))
    refs = [extra.ref] if source_kind in {"formal_only", "own_tool"} else [own.ref, extra.ref]
    raw = native_response(dict(kind="fact_candidate", content="observed", reason="direct observation", support_refs=refs, inferred=False))
    if source_kind == "own_tool":
        assert isinstance(parse(request, raw), StageFactProposal)
        assert not stage_output_issues(request, raw)
    else:
        with pytest.raises(ValueError):
            parse(request, raw)
        issue = stage_output_issues(request, raw)[0]
        assert issue.code == ("stage.formal_fact_requires_own_observation" if source_kind == "formal_only" else "stage.fact_source_ineligible")


def test_fact_source_failure_preserves_safe_scope_diagnostics():
    from agent_core.handoff_contracts import D8HistoryItem
    from agent_core.stage_storage import fact_scope_failure
    from agent_adapters.output_evidence import safe_failure_details
    task, turn, road, proposal, review, fact = (str(uuid4()) for _ in range(6))
    source = D8HistoryItem("import/" + str(uuid4()), task, "progress", None, None, None,
        "private content must not appear in diagnostics", (), "a" * 64, 1,
        "imported_attachment_not_fact", "current", "available")
    with pytest.raises(StageStateError) as caught:
        fact_scope_failure(source, PlanningSession("road_execution", road), expected_turn_id=turn,
            fact_id=fact, proposal_id=proposal, review_id=review, reason="source_not_tool_dispatch")
    details = safe_failure_details(caught.value)["fact_source"]
    assert details["source_ref"] == source.ref and details["fact_id"] == fact
    assert details["expected_owner_id"] == road and details["expected_turn_id"] == turn
    assert details["actual_road_id"] is None and details["actual_turn_id"] is None
    assert "private content" not in json.dumps(details)


def test_fact_source_changed_at_commit_still_stops_with_original_ids(case, monkeypatch, caplog):
    from agent_core import stage_storage
    read_history = stage_storage.read_history
    def changed_scope(store, task_id, refs):
        return tuple(replace(i, intent_id=None) if i.record_type == "progress" else i
            for i in read_history(store, task_id, refs))
    monkeypatch.setattr(stage_storage, "read_history", changed_scope)
    with pytest.raises(StageStateError, match="stage.fact_source_scope_changed") as caught:
        asyncio.run(case.app.run(case.token))
    details = caught.value.context["fact_source"]
    assert details["fact_id"] and details["proposal_operation_id"] and details["review_operation_id"]
    assert details["expected_turn_id"] and details["expected_owner_id"] and details["actual_road_id"] is None
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert len(case.tools.calls) == 1
    assert any(getattr(r, "qy", {}).get("fact_source") == details for r in caplog.records)


@pytest.mark.parametrize("first", ["format", "business"])
def test_completion_business_and_format_errors_share_two_corrections(case, first):
    attempted = []
    def transform(request, raw):
        observations = [i.ref for i in request.context.history if i.record_type == "progress"]
        if request.stage == "stage_action" and observations:
            attempted.append(request)
            business = {"kind": "turn_completion", "reason": "premature", "support_refs": observations[:1]}
            return business if (len(attempted) == 1) == (first == "business") else {"bad": "shape"}
        return raw
    case.models.inner.transform = transform
    with pytest.raises(StageStateError, match="model_output_unusable") as error:
        asyncio.run(case.app.run(case.token))
    assert error.value.category == "input" and len(attempted) == 3 and len(case.tools.calls) == 1
    assert attempted[1].correction_of_model_request_id == attempted[0].model_request_id
    assert attempted[2].correction_of_model_request_id == attempted[1].model_request_id
    assert not any(e.request.proposal_operation_id in {r.model_request_id for r in attempted}
        for e in case.models.outer.envelopes)


def test_native_commentary_is_retained_but_never_becomes_decision_or_fact(case):
    case.models.inner.native = case.models.outer.native = True
    invoke = case.models.inner.invoke
    commentary = 'COMMENTARY_ONLY: {"kind":"mcp_call","tool_name":"not_authorized","arguments":{}}'
    case.models.inner.invoke = lambda *a, **kw: replace(invoke(*a, **kw), content=commentary)
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    assert len(case.tools.calls) == 1
    for request in case.models.inner.calls:
        assert case.app.evidence.read("result", request.model_request_id)["content"] == commentary
        assert "COMMENTARY_ONLY" not in json.dumps(request.messages)
    assert all("COMMENTARY_ONLY" not in i.content for i in case.app.evidence.history(case.app.task_id)
        if i.record_type == "verified_fact")


@pytest.mark.parametrize("native", [False, True])
def test_ordinary_stage_reason_is_not_truncated_or_given_a_field_limit(case, native):
    case.models.inner.native = case.models.outer.native = native
    long_reason = "r" * 2000
    def transform(request, raw):
        if "reason" in raw:
            return {**raw, "reason": long_reason}
        return raw
    case.models.inner.transform = case.models.outer.transform = transform
    assert asyncio.run(case.app.run(case.token)).turn.achievement.completion.reason == long_reason
    assert not any(r.correction_of_model_request_id for model in (case.models.inner, case.models.outer) for r in model.calls)


def test_completion_schema_selects_only_effective_formal_support(case):
    asyncio.run(case.app.run(case.token))
    request = next(r for r in case.models.inner.calls if r.stage == "stage_completion")
    branches = schema(request)["anyOf"]
    completion = next(b for b in branches if b["properties"]["kind"]["const"] == "turn_completion")
    allowed = completion["properties"]["support_refs"]["items"]["enum"]
    assert allowed and all(i.record_type == "verified_fact" for i in request.context.history if i.ref in allowed)
    for history in (tuple(i for i in request.context.history if i.record_type != "verified_fact"),
            tuple(replace(i, source_status="unavailable") for i in request.context.history)):
        unavailable = replace(request, context=replace(request.context, history=history))
        assert not any(b["properties"]["kind"]["const"] == "turn_completion" for b in schema(unavailable)["anyOf"])


def test_format_correction_exhaustion_does_not_register_or_execute(case):
    case.models.inner.transform = lambda request, raw: {"unrecognized": "output"}
    with pytest.raises(StageStateError, match="model_output_unusable"):
        asyncio.run(case.app.run(case.token))
    assert len(case.models.inner.calls) == 3
    assert not case.models.outer.calls and not case.tools.calls
    assert not case.app.store.list_stage_turns(case.app.task_id)


@pytest.mark.parametrize("correct_first_action", [False, True])
def test_supervision_keeps_original_four_decision_cadence_without_extra_planning(case, correct_first_action):
    counter, supervised_after = [], []
    invalid_sent = False
    invoke = case.models.outer.invoke
    def outer(envelope, *args):
        if envelope.request.stage == "stage_supervision":
            supervised_after.append(len(case.tools.calls))
        return invoke(envelope, *args)
    case.models.outer.invoke = outer
    def transform(request, raw):
        nonlocal invalid_sent
        if request.stage == "stage_action" and correct_first_action and not invalid_sent:
            invalid_sent = True
            return {"invalid": "controlled format failure"}
        if request.stage == "stage_action" and len(counter) < 5:
            counter.append(True)
            return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf observed"}}
        return raw
    case.models.inner.transform = transform
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 5
    reviews = [r for r in case.models.outer.calls if r.stage == "stage_supervision"]
    assert len(reviews) == 1
    assert supervised_after == [4]
    assert len([r for r in case.models.inner.calls if r.stage == "stage_intent"]) == 1
    assert len([r for r in case.models.inner.calls if r.stage == "stage_roads"]) == 1


def test_tampered_original_result_is_rejected_without_model_or_tool_replay(case):
    result = asyncio.run(case.app.run(case.token))
    fact = next(i for i in case.app.evidence.history(case.app.task_id) if i.record_type == "verified_fact")
    operation = next(op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == "tool_dispatch")
    path = case.app.evidence.records._path("stage-mcp-result", operation.start.operation_id)
    path.write_text('{"sha256":"changed","payload":{}}', encoding="utf-8")
    calls = len(case.models.inner.calls), len(case.tools.calls)
    with pytest.raises(StageStateError, match="evidence_corrupt"):
        case.app.store.read_history(case.app.task_id, (fact.ref,))
    assert calls == (len(case.models.inner.calls), len(case.tools.calls))


def test_saved_model_output_reconciles_interrupted_accounting_without_recall(case, monkeypatch):
    complete = case.app.store.complete_d4_operation
    stopped = []
    def interrupt(value):
        if not stopped:
            stopped.append(True)
            raise RuntimeError("controlled loss before model accounting")
        return complete(value)
    monkeypatch.setattr(case.app.store, "complete_d4_operation", interrupt)
    with pytest.raises(RuntimeError, match="controlled loss"):
        asyncio.run(case.app.run(case.token))
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None
    assert len([r for r in case.models.inner.calls if r.stage == "stage_intent"]) == 1


def test_rejected_stage_completion_keeps_formal_fact_and_road_success(case):
    def reject(request, raw):
        return {"decision": "reject", "reason": "criterion not supported"} if isinstance(request.proposal, TurnCompletion) else raw
    case.models.outer.transform = reject
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is None and result.turn.roads[0].status is IntentStatus.SUCCEEDED
    assert len([i for i in case.app.evidence.history(case.app.task_id) if i.record_type == "verified_fact"]) == 1


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stop_after_known_inner_result_prevents_next_review_and_registration(case, stop):
    def transform(request, raw):
        if stop == "cancel": case.token.is_cancelled = True
        else: case.now.value += 3601
        return raw
    case.models.inner.transform = transform
    with pytest.raises(OperationRecoveryRequired, match="dispatch_stopped") as error:
        asyncio.run(case.app.run(case.token))
    from qinyuan_core import Category, classify
    assert classify(error.value) == (Category.POLICY, "stage.dispatch_stopped", False)
    assert not case.models.outer.calls and not case.tools.calls
    assert not case.app.store.list_stage_turns(case.app.task_id)
    records = case.app.store.list_d4_operations(case.app.task_id)
    assert len(records) == 1 and records[0].completion is not None and records[0].application_ref is None


def test_capacity_admission_stops_before_model_dispatch(case):
    case.app.models.capacity_port = SimpleNamespace(fits=lambda envelope, policy: False)
    with pytest.raises(StageStateError, match="model_capacity_blocked"):
        asyncio.run(case.app.run(case.token))
    assert not case.models.inner.calls and not case.models.outer.calls
    assert not case.app.store.list_d4_operations(case.app.task_id)


def test_real_provider_payload_keeps_role_and_reasoning_parameters(case):
    from agent_adapters.deepseek_roles import build_payload, role_config
    from agent_core.stage_planning import schema, model_contract
    from agent_core.output_contracts import D8OutputRequest
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    inner, outer = role_config("inner_action", version=17), role_config("outer_planning", version=17)
    request = replace(request, role_config_ref=inner.role_config_ref)
    envelope = D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": schema(request)}, model_contract(request)["instruction"], None)
    payload = build_payload(envelope, inner)
    assert "Do not copy the contract envelope" in payload["messages"][-2]["content"]
    assert payload["tool_choice"] == "auto" and payload["tools"][0]["function"]["name"] == OUTPUT_TOOL
    assert "response_format" not in payload
    history = replace(request, messages=({"role": "assistant", "content": '{"kind":"roads"}'},))
    projected = build_payload(replace(envelope, request=history), inner)
    assert projected["messages"][1]["role"] == "user"
    assert json.loads(projected["messages"][1]["content"])["history"][0]["content"]["historical_model_result"] == {"kind": "roads"}
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "high"
    assert "max_tokens" not in payload and payload["stream"] is True
    with pytest.raises(ValueError, match="model_role_binding_changed"):
        build_payload(envelope, outer)
    request = replace(request, stage="stage_summary", role_config_ref=outer.role_config_ref)
    envelope = replace(envelope, request=request, output_contract={"version": 1, "stage": request.stage, "schema": schema(request)})
    assert build_payload(envelope, outer)["reasoning_effort"] == "high"


def test_execution_channels_are_frozen_for_inner_outer_and_catalog(case):
    from agent_adapters.deepseek_roles import build_payload, role_config
    from agent_core.output_contracts import D8OutputRequest
    from agent_core.stage_planning import model_contract
    asyncio.run(case.app.mcp.open())
    assert 'sandbox-only' in case.app.mcp.tools[0]['tags']
    for stage, name in [('stage_intent', 'inner_action'), ('stage_supervision', 'outer_planning')]:
        role = role_config(name, version=17)
        req = replace(case.app._request(stage, PlanningSession('turn_planning', case.app._id('stage/root'))),
            role_config_ref=role.role_config_ref)
        contract = model_contract(req)
        payload = build_payload(D8OutputRequest(req, {'version': 1, 'stage': stage, 'schema': contract['schema']},
            contract['instruction'], None), role)
        runtime = next(m['content'] for m in payload['messages'] if m['role'] == 'system' and 'Trusted execution' in m['content'])
        assert 'network_mode=none' in runtime and 'localhost' in runtime
        assert 'No http.run_experiment target channel' in runtime
        assert not req.tools and 'does not prove target behavior' in runtime


@pytest.mark.parametrize("stage", ["stage_roads", "stage_action", "stage_review",
    "stage_completion", "stage_continuation", "stage_supervision"])
def test_generic_exploration_guidance_reaches_provider_without_granting_tools(stage):
    from agent_adapters.deepseek_roles import build_payload, role_config
    from agent_adapters.public_web import SPECS
    from agent_core.actions import MCPCall
    from agent_core.output_contracts import D8OutputRequest
    from agent_core.stage_planning import INNER_STAGES, model_contract
    turn = StageTurnSnapshot(str(uuid4()), TurnIntent("compare candidate explanations", "a discriminating observation"),
        TurnPlanningState.ACTIVE)
    session = PlanningSession("road_execution" if stage in {"stage_action", "stage_supervision"} else "turn_planning",
        str(uuid4()) if stage in {"stage_action", "stage_supervision"} else turn.turn_id)
    context = TurnPlanningContext(str(uuid4()), "investigate the authorized question", session, turn, ())
    role = role_config("inner_action" if stage in INNER_STAGES else "outer_planning", version=17)
    local_tool = {"qualified_name": "shell.run_bash", "input_schema": {"type": "object",
        "properties": {"script": {"type": "string"}}, "required": ["script"], "additionalProperties": False}}
    request = StageModelRequest(str(uuid4()), context.task_run_id, stage, role.role_config_ref, 1, context,
        tools=(*SPECS, local_tool) if stage == "stage_action" else (),
        proposal=turn.intent if stage == "stage_review" else None)
    contract = model_contract(request)
    payload = build_payload(D8OutputRequest(request, {"version": 1, "stage": stage, "schema": contract["schema"]},
        contract["instruction"], None), role)
    system = payload["messages"][-2]["content"]
    assert "answer could change the next decision" in system
    assert "use adequate local sources directly and reuse existing retrievals" in system
    assert "Do not search merely because a version, error or attempt count exists" in system
    assert "cannot distinguish success from failure leaves that hypothesis unresolved" in system
    assert "Separate tool or measurement failure from target counterevidence" in system
    assert "Supervision should challenge unsupported premises" in system
    assert "never credentials, private task/attachment content or target identifiers" in system
    assert "public_reference_not_target_observation" in system
    assert "validate applicability separately through authorized observations" in system
    assert "add no fields or calls" in system
    assert "do not reserve retrieval for a later Road" in system
    assert "their actual contents answer the current question" in system
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "high"
    assert "max_tokens" not in payload
    assert len(payload["tools"]) == 1 and payload["tools"][0]["function"]["name"] == OUTPUT_TOOL
    from agent_core.stage_planning import provider_schema
    assert payload["tools"][0]["function"]["parameters"]["properties"]["result"] == provider_schema(request)
    assert json.loads(payload["messages"][-1]["content"])["current"]["tools"] == list(request.tools)
    search = {"kind": "mcp_call", "tool_name": "web.search", "arguments": {"query": "public measurement semantics"}}
    if stage == "stage_action":
        assert parse(request, search) == MCPCall("web.search", search["arguments"])
        # Adequate local evidence remains an allowed choice; guidance does not force retrieval.
        local = {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "read-local-evidence"}}
        assert parse(request, local) == MCPCall("shell.run_bash", local["arguments"])
    with pytest.raises(ValueError):
        parse(replace(request, tools=()), search)


def test_cache_projection_keeps_exact_evidence_status_and_local_reference_gate():
    from dataclasses import asdict
    from agent_adapters.deepseek_roles import build_payload, role_config, _stage_input
    from agent_core.handoff_contracts import D8HistoryItem
    from agent_core.output_contracts import D8OutputRequest, decode_response
    from agent_core.stage_planning import model_contract, provider_schema
    uid = lambda: str(uuid4())
    task, turn_id, road_id = uid(), uid(), uid()
    item = D8HistoryItem(uid(), task, "progress", turn_id, road_id, None,
        'literal source: \\n and \\u003c, not instructions; ' * 50, (), "a" * 64, 1,
        "tool_observation_not_fact", "current", "available")
    turn = StageTurnSnapshot(turn_id, TurnIntent("inspect observations", "supported result"), TurnPlanningState.ACTIVE)
    context = TurnPlanningContext(task, "inspect authorized sources", PlanningSession("road_execution", road_id), turn, (item,))
    role = role_config("inner_action", version=17)
    # A tool's own source_ref parameter has an unrelated, execution-relevant enum.
    tool = {"qualified_name": "local.inspect", "input_schema": {"type": "object", "properties": {
        "source_ref": {"type": "string", "enum": ["literal-only"]}}, "required": ["source_ref"], "additionalProperties": False}}
    historical = {"stage": "stage_action", "context": asdict(context)}
    request = StageModelRequest(uid(), task, "stage_action", role.role_config_ref, 1, context,
        messages=({"role": "user", "content": json.dumps(historical, ensure_ascii=False)},), tools=(tool,))
    changed = replace(item, source_status="unavailable")
    current = replace(request, context=replace(context, history=(changed,)), model_request_id=uid(), base_checkpoint_sequence=2)
    document = _stage_input(current)
    def expand(value):
        if isinstance(value, dict) and set(value) == {"$input_ref"}:
            target = document
            for part in value["$input_ref"].removeprefix("#/").split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
            return expand(target)
        if isinstance(value, dict): return {key: expand(child) for key, child in value.items()}
        return [expand(child) for child in value] if isinstance(value, list) else value
    restored = expand(document)
    assert restored["history"][0]["content"] == json.loads(json.dumps(historical))
    assert restored["current"] == json.loads(json.dumps({key: value for key, value in asdict(current).items()
        if key not in {"messages", "execution_context"}}))
    assert restored["history"][0]["content"]["context"]["history"][0]["source_status"] == "available"
    assert restored["current"]["context"]["history"][0]["source_status"] == "unavailable"
    assert json.dumps(document, ensure_ascii=False).count(json.dumps(item.content, ensure_ascii=False)) == 1
    contract = model_contract(current)
    payload = build_payload(D8OutputRequest(current, {"version": 1, "stage": current.stage, "schema": schema(current)},
        contract["instruction"], None), role)
    encoded_contract = payload["messages"][-2]["content"].split("Output contract=", 1)[1]
    runtime, _ = json.JSONDecoder().raw_decode(encoded_contract)
    assert runtime["schema"] == schema(current)  # Full current constraints stay system-priority.
    proposal = {"kind": "fact_candidate", "content": "source statement", "reason": "direct",
        "support_refs": ["invented-reference"], "inferred": False}
    assert not decode_response(proposal, provider_schema(current))[1]
    with pytest.raises(ValueError): parse(current, proposal)
    native_call = next(b for b in provider_schema(current)["anyOf"] if b["properties"]["kind"]["const"] == "mcp_call")
    assert native_call["properties"]["arguments"] == {"$schema": "https://json-schema.org/draft/2020-12/schema", **tool["input_schema"]}
    second = replace(item, record_id=uid(), content="a new observed result", source_version="b" * 64)
    following = replace(request, model_request_id=uid(), base_checkpoint_sequence=3,
        context=replace(context, history=(item, second)), messages=(*request.messages,
            {"role": "assistant", "content": '{"kind":"history_details","refs":[]}'}))
    following_contract = model_contract(following)
    next_payload = build_payload(D8OutputRequest(following, {"version": 1, "stage": following.stage, "schema": schema(following)},
        following_contract["instruction"], None), role)
    assert next_payload["tools"] == payload["tools"]
    assert next_payload["messages"][0] == payload["messages"][0]
    assert next_payload["messages"][-2] != payload["messages"][-2]
    assert next_payload["messages"][1] == payload["messages"][1]
    assert request.context.history == (item,) and item.content.startswith('literal source: \\n')


def test_review_proposal_stays_verbatim_when_history_already_holds_an_equal_array():
    """The reviewer must read the stored proposal, never a reference to an equal copy.

    Regression for a live revise loop: a re-submitted closure repeated the support
    array of an earlier select_facts result, so request assembly replaced
    proposal.support_refs with a pointer. The reviewer kept answering "write a
    literal array", which the Inner had already done, because the pointer made a
    declared array look like an object.
    """
    from agent_adapters.deepseek_roles import build_payload, role_config, _stage_input
    from agent_core.handoff_contracts import D8HistoryItem
    from agent_core.output_contracts import D8OutputRequest
    from agent_core.stage_planning import model_contract
    uid = lambda: str(uuid4())
    task, turn_id, road_id = uid(), uid(), uid()
    refs = ["verified_fact:" + uid() + ":" + "a" * 64 for _ in range(5)]
    item = D8HistoryItem(uid(), task, "progress", turn_id, road_id, None,
        "the binary was inspected and each observation was registered", (),
        "a" * 64, 1, "verified_fact", "current", "available")
    turn = StageTurnSnapshot(turn_id, TurnIntent("triage the attachment", "supported triage"), TurnPlanningState.ACTIVE)
    context = TurnPlanningContext(task, "triage the authorized attachment and record every supported observation " * 3,
        PlanningSession("road_execution", road_id), turn, (item,))
    role = role_config("outer_planning", version=17)
    proposal = RoadResolution(IntentStatus.SUCCEEDED, "the closure cites the five effective Facts", tuple(refs))
    request = StageModelRequest(uid(), task, "stage_review", role.role_config_ref, 1, context,
        messages=({"role": "user", "content": json.dumps({"stage": "stage_action", "context": asdict(context)})},
                  {"role": "assistant", "content": json.dumps({"kind": "select_facts", "support_refs": refs})}),
        proposal_operation_id=uid(), proposal=proposal)
    document = _stage_input(request)
    assert document["current"]["proposal"]["support_refs"] == refs
    # Unchanged sharing still applies outside the reviewed proposal.
    assert document["current"]["context"] == {"$input_ref": "#/history/0/content/context"}
    contract = model_contract(request)
    payload = build_payload(D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": schema(request)},
        contract["instruction"], None), role)
    current = json.loads(payload["messages"][-1]["content"])["current"]
    assert current["proposal"]["support_refs"] == refs
    assert proposal.support_refs == tuple(refs)


def test_provider_history_segments_stay_identical_while_live_snapshots_change(case):
    from agent_adapters.deepseek_roles import build_payload, role_config, _stage_input
    from agent_core.output_contracts import D8OutputRequest
    from agent_core.stage_planning import model_contract
    role = role_config('inner_action', version=17)
    req = replace(case.app._request('stage_intent', PlanningSession('turn_planning', case.app._id('stage/root'))),
        role_config_ref=role.role_config_ref)
    archive = ({'role': 'user', 'content': json.dumps({'source': 'literal \\n \\u003c ' * 30})},
        {'role': 'assistant', 'content': '{"kind":"unverified historical decision"}'})
    def payload(messages):
        current = replace(req, messages=messages)
        contract = model_contract(current)
        return build_payload(D8OutputRequest(current, {'version': 1, 'stage': current.stage, 'schema': contract['schema']},
            contract['instruction'], None), role)
    first = payload((*archive, {'role': 'user', 'content': '{"pending_tools":[{"operation_id":"A"}]}'}))
    second = payload((*archive, {'role': 'user', 'content': '{"review_feedback":"late advice"}'},
        {'role': 'user', 'content': '{"tool_feedback":[{"operation_id":"A","status":"failed"}]}'}))
    assert second['messages'][:3] == first['messages'][:3]
    assert [json.loads(m['content'])['history_start'] for m in second['messages'][1:-2]] == [0, 1, 2]
    assert 'pending_tools' not in json.dumps(first['messages'][1:-2])
    assert 'pending_tools' in json.loads(first['messages'][-1]['content'])['current']
    assert 'tool_feedback' in json.loads(second['messages'][-1]['content'])['current']
    doc = _stage_input(replace(req, messages=archive))
    assert doc['history'][0]['content']['source'] == json.loads(archive[0]['content'])['source']
    assert second['tools'] == first['tools'] and second['reasoning_effort'] == 'high'


def test_continuation_history_omits_only_exact_summary_covered_unneeded_raw():
    from agent_core.handoff_contracts import D8HistoryItem
    from agent_core.stage_contracts import StageSessionSummary, StageHistoryRead
    from agent_core.turn_modeling import digest
    task, owner = str(uuid4()), str(uuid4())
    def observation(text):
        return D8HistoryItem(str(uuid4()), task, "progress", owner, str(uuid4()), None,
            text, (), "a" * 64, 1, "tool_observation_not_fact", "current", "available")
    old, necessary, recent, retrieved, negative, support, exact_read = (
        observation(text) for text in ("covered old raw", "required premise", "recent complete decision",
            "explicitly retrieved", "uncovered failed probe", "formal Fact support", "history_details original"))
    fact = replace(observation("formal conclusion"), record_type="verified_fact", identity="verified_fact",
        source_refs=(support.ref,))
    revision = replace(observation("withdrawn premise notification"), record_id="revision/" + str(uuid4()),
        identity="revision_notification")
    history = (old, necessary, recent, retrieved, negative, support, exact_read, fact, revision)
    context = TurnPlanningContext(task, "useful next result", PlanningSession("turn_planning", owner), None, history)
    base = StageModelRequest(str(uuid4()), task, "stage_continuation", "frozen-role", 1, context)
    values = {}
    def source(items, value=None):
        request = replace(base, model_request_id=str(uuid4()), context=replace(context, history=items))
        operation = SimpleNamespace(start=SimpleNamespace(operation_id=request.model_request_id))
        values[request.model_request_id] = SimpleNamespace(result=value)
        return operation, request, {"outcome": "valid", "content": "frozen result"}
    covered = source((old, necessary, retrieved, support), StageHistoryRead((exact_read.ref,)))
    latest_four = [source((recent,)) for _ in range(4)]
    version = digest({"request": asdict(covered[1]), "result": covered[2]})
    summary = replace(base, model_request_id=str(uuid4()), stage="stage_summary",
        context=replace(context, history=()), summary_coverage=((covered[0].start.operation_id, version),))
    values[summary.model_request_id] = SimpleNamespace(result=StageSessionSummary("Unverified index; retrieve originals."))
    sources = [covered, *latest_four, (SimpleNamespace(start=SimpleNamespace(operation_id=summary.model_request_id)),
        summary, {"outcome": "valid", "content": "Unverified index; retrieve originals."})]
    app = object.__new__(StageApplication)
    app.evidence = SimpleNamespace(read_model_result=lambda operation: values[operation.start.operation_id],
        read_history_result=lambda operation: (exact_read,))
    before = asdict(base)
    projected = app._planning_history(history, sources, required=(necessary.ref,), retrieved=(retrieved.ref,))
    assert projected == tuple(i for i in history if i not in (old, exact_read))
    assert negative in projected and fact in projected and revision in projected and recent in projected
    assert app._planning_history(history, latest_four) == history  # No summary means no omission.
    assert app._planning_history(history, sources, retrieved=(old.ref, exact_read.ref),
        required=(necessary.ref, retrieved.ref)) == history
    for changed_old in (replace(old, source_status="unavailable"), replace(old, conflict=True),
                        replace(old, source_version="b" * 64)):
        assert changed_old in app._planning_history((changed_old, *history[1:]), sources)
    assert asdict(base) == before  # Originals, versions and latest decisions are unchanged.
    changed = [(*covered[:2], {**covered[2], "content": "changed covered result"}), *sources[1:]]
    with pytest.raises(StageStateError, match="summary_coverage_changed"):
        app._planning_history(history, changed)


@pytest.mark.parametrize("invalid_attempts", [0, 1, 2, 3])
def test_supervision_and_correction_finish_before_next_inner(case, invalid_attempts):
    events, actions, supervision = [], [], []
    invoke = case.models.outer.invoke
    def outer(envelope, *args):
        request = envelope.request
        if request.stage == "stage_supervision":
            events.append("supervision_start")
            assert len(case.app.models.live_operations) == 1
        result = invoke(envelope, *args)
        if request.stage == "stage_supervision": events.append("supervision_end")
        return result
    case.models.outer.invoke = outer
    def review(request, raw):
        if request.stage == "stage_supervision":
            supervision.append(request)
            if len(supervision) <= invalid_attempts:
                return {"invalid": "format"}
        return raw
    case.models.outer.transform = review
    def decision(request, raw):
        if request.stage == "stage_action":
            actions.append(request)
            events.append("inner")
            if len(actions) <= 5:
                return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "controlled read"})
        return raw
    case.models.inner.transform = decision
    if invalid_attempts == 3:
        with pytest.raises(StageStateError, match="stage.model_output_unusable"):
            asyncio.run(case.app.run(case.token))
        assert len(actions) == 4
    else:
        assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
        assert "review_feedback" in str(actions[4].messages)
        accepted_id = supervision[-1].model_request_id
        assert case.app.store.load_d4_operation(accepted_id).application_ref == "stage-advice/" + accepted_id
    assert len(supervision) == min(invalid_attempts + 1, 3)
    assert events[:4] == ["inner"] * 4
    assert events[4:6] == ["supervision_start", "supervision_end"]
    assert not case.app.models.live_operations
    assert not case.app.store.list_d4_unresolved_operations(case.app.task_id)


def test_single_review_cannot_create_duplicate_formal_facts(case, monkeypatch):
    commit = case.app._commit
    def duplicate(change, **kwargs):
        if change.facts:
            change = replace(change, facts=(*change.facts, replace(change.facts[0], fact_id=str(uuid4()))))
        return commit(change, **kwargs)
    monkeypatch.setattr(case.app, "_commit", duplicate)
    with pytest.raises(StageStateError, match="duplicate_fact_review"):
        asyncio.run(case.app.run(case.token))
    assert case.app.store._connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def prepare_capacity_episode(case, *, block_summary=False, block_rebuilt=False):
    counter = []
    def transform(request, raw):
        if request.stage == "stage_action" and len(counter) < 6:
            counter.append(True)
            return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf observed"}}
        return raw
    case.models.inner.transform = transform
    blocked = []
    def fits(envelope, policy):
        request = envelope.request
        if request.stage == "stage_summary":
            return not block_summary
        if request.capacity_recovery_of:
            return not block_rebuilt
        if request.stage == "stage_action" and len(request.messages) >= 12 and not blocked:
            blocked.append(request)
            return False
        return True
    case.app.models.capacity_port = SimpleNamespace(fits=fits)
    case.args["capacity_port"] = case.app.models.capacity_port
    return blocked


@pytest.mark.parametrize("native", [False, True])
def test_automatic_capacity_summary_keeps_recent_four_and_original_sources(case, native):
    case.models.inner.native = case.models.outer.native = native
    blocked = prepare_capacity_episode(case)
    result = asyncio.run(case.app.run(case.token))
    summaries = [r for r in case.models.outer.calls if r.stage == "stage_summary"]
    assert result.turn.achievement is not None and len(summaries) == 1
    summary = summaries[0]
    assert summary.summary_coverage and summary.context.session.scope == "road_execution"
    before = [r for r in case.models.inner.calls if r.stage == "stage_action"
        and r.context.session == summary.context.session and r.model_request_id not in {
            call.model_request_id for call in case.models.inner.calls if call.capacity_recovery_of}]
    covered = {identity for identity, _ in summary.summary_coverage}
    assert not covered.intersection(r.model_request_id for r in before[-4:])
    rebuilt = next(r for r in case.models.inner.calls if r.capacity_recovery_of == blocked[0].model_request_id)
    assert rebuilt.context == blocked[0].context
    assert "unverified_session_summary" in json.dumps(rebuilt.messages)
    for identity, _ in summary.summary_coverage:
        assert case.app.evidence.read("request", identity) and case.app.evidence.read("result", identity)
    assert all(i.record_type != "summary" for i in case.app.evidence.history(case.app.task_id))


def test_capacity_replacement_recovers_known_output_without_new_summary_or_tool(case, monkeypatch):
    prepare_capacity_episode(case)
    commit = case.app._commit
    stopped = []
    def interrupt(change, **kwargs):
        if change.facts and not stopped:
            stopped.append(True)
            raise RuntimeError("controlled loss after capacity replacement")
        return commit(change, **kwargs)
    monkeypatch.setattr(case.app, "_commit", interrupt)
    with pytest.raises(RuntimeError, match="controlled loss"):
        asyncio.run(case.app.run(case.token))
    count = len([r for r in case.models.outer.calls if r.stage == "stage_summary"])
    task_id = case.app.task_id
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 6
    assert len([r for r in case.models.outer.calls if r.stage == "stage_summary"]) == count


@pytest.mark.parametrize("mode", ["summary_cannot_fit", "replacement_cannot_fit"])
def test_capacity_recovery_is_bounded_and_cannot_fake_business_outcome(case, mode):
    prepare_capacity_episode(case, block_summary=mode == "summary_cannot_fit", block_rebuilt=mode == "replacement_cannot_fit")
    with pytest.raises(StageStateError, match="model_capacity_blocked"):
        asyncio.run(case.app.run(case.token))
    turn = case.app.store.list_stage_turns(case.app.task_id)[0]
    assert turn.achievement is None and turn.roads[0].status is IntentStatus.RUNNING
    assert len(case.tools.calls) == 6
    assert len([r for r in case.models.outer.calls if r.stage == "stage_summary"]) == (0 if mode == "summary_cannot_fit" else 1)


@pytest.mark.parametrize("corrected", [False, True])
def test_declared_planning_ports_use_audited_inner_and_outer(case, corrected):
    invalid = []
    def transform(request, raw):
        if corrected and not invalid:
            invalid.append(True)
            return {"invalid": "first output"}
        return raw
    case.models.inner.transform = transform
    async def exercise():
        session = PlanningSession("turn_planning", case.app._id("stage/root"))
        context = case.app._request("stage_intent", session).context
        proposal_id, review_id = str(uuid4()), str(uuid4())
        proposal = await case.app.models.propose_turn(context, proposal_id, case.token)
        review = await case.app.models.review(context, proposal, proposal_id, review_id, case.token)
        assert review.decision == "accept"
        turn = StageTurnSnapshot(session.owner_id, proposal, TurnPlanningState.ACTIVE)
        case.app._commit(StageStateChange(turns=(turn,), reviews=(StageReviewBinding(
            case.app.models.resolved_operation_id(proposal_id), case.app.models.resolved_operation_id(review_id)),)))
        context = case.app._request("stage_roads", session).context
        roads = await case.app.models.propose_roads(context, str(uuid4()), case.token)
        assert len(roads) == 1
    asyncio.run(exercise())
    assert [r.stage for r in case.models.inner.calls] == (["stage_intent"] if corrected else []) + ["stage_intent", "stage_roads"]
    assert [r.stage for r in case.models.outer.calls] == ["stage_review"]


def test_unknown_model_result_is_not_reissued(case):
    def lost(request, raw):
        raise RuntimeError("controlled provider result lost")
    case.models.inner.transform = lost
    with pytest.raises(OperationRecoveryRequired, match="model_outcome_unknown") as error:
        asyncio.run(case.app.run(case.token))
    from qinyuan_core import Category, classify
    assert classify(error.value) == (Category.UNKNOWN, "stage.model_outcome_unknown", False)
    with pytest.raises(OperationRecoveryRequired, match="operation_outcome_unknown"):
        asyncio.run(case.app.run(case.token))
    assert len(case.models.inner.calls) == 1 and not case.tools.calls


def save_provider_stop(case, envelope, code, *, exit_confirmed=True, changed_binding=False, transport_end=None):
    from agent_core.turn_modeling import digest
    request = envelope.request
    sent = dict(model_request_id=request.model_request_id, task_run_id=request.task_run_id,
        stage=request.stage, role_config_ref=request.role_config_ref, envelope_sha256=digest(asdict(envelope)))
    case.app.evidence.save("provider-request", request.model_request_id, sent)
    result = dict(model_request_id=request.model_request_id,
        request_sha256="a" * 64 if changed_binding else digest(sent),
        error={"kind": "timed_out", "code": code}, transport=dict(
            elapsed_seconds=.03, first_output_seconds=.001, last_output_seconds=.029,
            reasoning_chars=120, reasoning_utf8_bytes=120, content_chars=0,
            worker_exit_confirmed=exit_confirmed, finish_seen=False, done_seen=False,
            reasoning_content="PRIVATE_REASONING_MUST_NOT_LEAK"))
    if transport_end is not None:
        result["transport"]["transport_end"] = transport_end
    case.app.evidence.save("provider-result", request.model_request_id, result)
    return result


@pytest.mark.parametrize("failures", [("timeout", "timeout"), ("timeout", "format"), ("format", "timeout")])
def test_stage_two_shared_corrections_reuse_bound_chain_without_replay(case, failures, caplog):
    from agent_core.modeling import ModelError, ModelErrorKind
    caplog.set_level("INFO", logger="qinyuan.model")
    invoke = case.models.inner.invoke
    def controlled(envelope, timeout, token):
        valid = invoke(envelope, timeout, token)
        index = len(case.models.inner.calls) - 1
        if index < len(failures):
            if failures[index] == "format":
                return D8ModelResponse('{"bad":"shape"}')
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return valid
    case.models.inner.invoke = controlled
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    identity, value = asyncio.run(case.app.models.call(request, case.token))
    calls = case.models.inner.calls
    assert len(calls) == 3 and isinstance(value, TurnIntent)
    assert [case.app.evidence.correction_depth(r) for r in calls] == [0, 1, 2]
    assert calls[2].correction_of_model_request_id == calls[1].model_request_id
    assert case.app.models.resolved_operation_id(calls[0].model_request_id) == identity
    case.app.models.reconcile(case.app.task_id)
    assert asyncio.run(case.app.models.call(request, case.token))[0] == identity
    assert len(calls) == 3 and not case.tools.calls
    assert sum(record.msg == "stage.call_retried" for record in caplog.records) == 2
    assert not case.app.store.list_d4_unresolved_operations(case.app.task_id)
    for r in calls:
        contract = case.app.evidence.read("contract", r.model_request_id)
        assert contract["call_policy"]["max_corrections"] == 2
        if r.correction_of_model_request_id:
            reason = contract["correction"]["reason"]
            previous = case.app.evidence.read("result", r.correction_of_model_request_id)
            assert reason == ("output_format_invalid" if previous["outcome"] == "invalid"
                              else "previous_model_transport_failed_no_usable_decision")


@pytest.mark.parametrize("stage", ["stage_intent", "stage_review"])
def test_known_transport_exhaustion_is_failed_not_unknown_and_review_does_not_pass(case, stage):
    from agent_adapters.stage_evidence import StageModelTransportFailure
    from agent_core.modeling import ModelError, ModelErrorKind
    session = PlanningSession("turn_planning", case.app._id("stage/root"))
    request = case.app._request("stage_intent", session)
    model = case.models.inner
    if stage == "stage_review":
        identity, proposal = asyncio.run(case.app.models.call(request, case.token))
        request = case.app._request(stage, session, proposal_id=identity, proposal=proposal)
        model = case.models.outer
    invoke = model.invoke
    def failed(envelope, timeout, token):
        invoke(envelope, timeout, token)
        save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
        return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
    model.invoke = failed
    for _ in range(2):
        with pytest.raises(StageModelTransportFailure) as failure:
            asyncio.run(case.app.models.call(request, case.token))
        assert failure.value.category == "dependency" and failure.value.retryable is False
        assert len(model.calls) == 3
        case.app.models.reconcile(case.app.task_id)
    assert not case.app.store.list_d4_unresolved_operations(case.app.task_id)
    assert not case.app.store.list_stage_turns(case.app.task_id) and not case.tools.calls
    assert all(case.app.store.load_d4_operation(r.model_request_id).completion.result_kind == "failed" for r in model.calls)
    from agent_adapters.web.task_result import _stage_task_result
    view = _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="simulated"),
                             case.app.store, case.args["config"])
    assert view["recovery"] == {"required": True, "reason": "stage.model_transport_failed"}


@pytest.mark.parametrize("window", ["provider_saved", "result_saved"])
def test_transport_failure_reconciles_crash_before_settlement_without_recalling_original(case, monkeypatch, window):
    from agent_core.modeling import ModelError, ModelErrorKind
    invoke = case.models.inner.invoke
    def controlled(envelope, timeout, token):
        valid = invoke(envelope, timeout, token)
        if len(case.models.inner.calls) == 1:
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return valid
    case.models.inner.invoke = controlled
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    if window == "provider_saved":
        save = case.app.evidence.save
        def stop(kind, identity, value):
            if kind == "result": raise RuntimeError("controlled crash before stage result")
            return save(kind, identity, value)
        monkeypatch.setattr(case.app.evidence, "save", stop)
    else:
        monkeypatch.setattr(case.app.store, "complete_d4_operation", lambda _: (_ for _ in ()).throw(RuntimeError("controlled crash")))
    with pytest.raises(RuntimeError, match="controlled crash"):
        asyncio.run(case.app.models.call(request, case.token))
    monkeypatch.undo()
    case.app.models.reconcile(case.app.task_id)
    assert case.app.store.load_d4_operation(request.model_request_id).completion.result_kind == "failed"
    assert isinstance(asyncio.run(case.app.models.call(request, case.token))[1], TurnIntent)
    assert len(case.models.inner.calls) == 2 and not case.tools.calls


@pytest.mark.parametrize("exit_confirmed,changed_binding", [(True, False), (False, False), (True, True)])
def test_only_confirmed_supervision_transport_failure_degrades_after_two_corrections(case, exit_confirmed, changed_binding):
    from agent_core.modeling import ModelError, ModelErrorKind
    actions = []
    def action(request, raw):
        if request.stage == "stage_action":
            actions.append(request)
            if len(actions) <= 5:
                return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "controlled read"})
        return raw
    case.models.inner.transform = action
    invoke = case.models.outer.invoke
    def outer(envelope, timeout, token):
        valid = invoke(envelope, timeout, token)
        if envelope.request.stage == "stage_supervision":
            save_provider_stop(case, envelope, "model_idle_timeout", exit_confirmed=exit_confirmed,
                               changed_binding=changed_binding, transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return valid
    case.models.outer.invoke = outer
    if exit_confirmed and not changed_binding:
        assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
        assert len(case.tools.calls) == 5
        from agent_adapters.web.task_result import _stage_task_result
        from agent_core.sqlite_control_store import SqliteControlStore
        with SqliteControlStore.open_readonly(case.path / "control.sqlite") as store:
            view = _stage_task_result(case.path, dict(task_run_id=case.app.task_id, mode="simulated"), store, case.args["config"])
            assert store._connection.total_changes == 0
        assert not view["recovery"]["required"] and not view["errors"]
        assert [f["code"] for f in view["model_failures"]] == ["stage.supervision_unavailable"]
    else:
        with pytest.raises(OperationRecoveryRequired, match="model_outcome_unknown"):
            asyncio.run(case.app.run(case.token))
        assert len(actions) == 4
    calls = [r for r in case.models.outer.calls if r.stage == "stage_supervision"]
    assert len(calls) == (3 if exit_confirmed and not changed_binding else 1)
    assert all(case.app.store.load_d4_operation(r.model_request_id).application_ref is None for r in calls)


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_known_transport_settles_but_never_retries_after_task_stop(case, stop):
    from agent_core.modeling import ModelError, ModelErrorKind
    invoke = case.models.inner.invoke
    deadline = case.app.state.task_run.deadline_at
    def stopped(envelope, timeout, token):
        invoke(envelope, timeout, token)
        save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
        if stop == "cancel": case.token.is_cancelled = True
        else: case.now.value = deadline + 1
        return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
    case.models.inner.invoke = stopped
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    with pytest.raises(OperationRecoveryRequired, match="dispatch_stopped"):
        asyncio.run(case.app.models.call(request, case.token))
    assert len(case.models.inner.calls) == 1 and not case.tools.calls
    assert not case.app.store.list_d4_unresolved_operations(case.app.task_id)


def test_unknown_second_attempt_is_not_recalled_or_degraded(case):
    from agent_core.modeling import ModelError, ModelErrorKind
    invoke = case.models.inner.invoke
    def controlled(envelope, timeout, token):
        invoke(envelope, timeout, token)
        if len(case.models.inner.calls) == 1:
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        raise RuntimeError("controlled unknown attempt")
    case.models.inner.invoke = controlled
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    for _ in range(2):
        with pytest.raises(OperationRecoveryRequired, match="model_outcome_unknown"):
            asyncio.run(case.app.models.call(request, case.token))
        case.app.models.reconcile(case.app.task_id)
    assert len(case.models.inner.calls) == 2 and not case.tools.calls
    assert len(case.app.store.list_d4_unresolved_operations(case.app.task_id)) == 1


def test_capacity_recovery_does_not_reset_two_shared_corrections(case):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_core.turn_modeling import digest, stable_id
    invoke = case.models.inner.invoke
    def controlled(envelope, timeout, token):
        invoke(envelope, timeout, token)
        if len(case.models.inner.calls) <= 2:
            save_provider_stop(case, envelope, "model_idle_timeout", transport_end="timeout")
            return ModelError(ModelErrorKind.TIMED_OUT, "model_idle_timeout")
        return D8ModelResponse('{"bad":"shape"}')
    case.models.inner.invoke = controlled
    request = case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root")))
    first_correction = stable_id(case.app.task_id, "stage/correction/" + request.model_request_id)
    capacity = case.app.models.capacity_port
    fits = capacity.fits
    capacity.fits = lambda envelope, policy: envelope.request.model_request_id != first_correction and fits(envelope, policy)
    async def recover(source, token):
        summary_request = replace(source, model_request_id=str(uuid4()), stage="stage_summary",
            role_config_ref=case.models.outer.role_config.role_config_ref, proposal=None, proposal_operation_id=None,
            correction_of_model_request_id=None, frozen_contract_ref=None)
        summary_id, _ = await case.app.models.call(summary_request, token)
        case.app.store.mark_d4_operation_applied(summary_id, "controlled-summary/" + summary_id)
        case.app.evidence.save("capacity-recovery", source.model_request_id,
            {"source_request_sha256": digest(asdict(source)), "summary_operation_id": summary_id})
        return replace(source, model_request_id=stable_id(case.app.task_id,
            "stage/capacity/" + source.model_request_id + "/" + summary_id),
            capacity_recovery_of=source.model_request_id, frozen_contract_ref=None)
    case.app.models.recover_capacity = recover
    for _ in range(2):
        with pytest.raises(StageStateError, match="model_output_unusable"):
            asyncio.run(case.app.models.call(request, case.token))
    assert len(case.models.inner.calls) == 3 and len(case.models.outer.calls) == 1
    assert [case.app.evidence.correction_depth(r) for r in case.models.inner.calls] == [0, 1, 2]
    last = case.models.inner.calls[-1]
    with pytest.raises(StageStateError, match="correction_limit_exceeded"):
        case.app.evidence.contract(replace(last, model_request_id=str(uuid4()),
            correction_of_model_request_id=last.model_request_id, frozen_contract_ref=None))
    assert not case.tools.calls


@pytest.mark.parametrize("code", ["model_connect_timeout", "model_first_output_timeout", "model_idle_timeout",
    "model_total_timeout", "http_timeout", "model_cancelled"])
def test_stage_transport_stop_keeps_bound_diagnosis_and_never_retries(case, code):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_adapters.output_evidence import safe_failure_details
    original = case.models.inner.invoke
    def invoke(envelope, timeout, token):
        original(envelope, timeout, token)
        save_provider_stop(case, envelope, code)
        return ModelError(ModelErrorKind.CANCELLED if code == "model_cancelled" else ModelErrorKind.TIMED_OUT, code)
    case.models.inner.invoke = invoke
    with pytest.raises(OperationRecoveryRequired) as failure:
        asyncio.run(case.app.run(case.token))
    detail = safe_failure_details(failure.value, include_chain=True)["model_call"]
    identity = case.models.inner.calls[0].model_request_id
    assert detail["stage"] == "stage_intent" and detail["operation_id"] == identity
    assert detail["reason"] == "transport_interrupted" and detail["diagnostic_status"] == "bound"
    assert detail["model_error_code"] == code
    assert detail.get("timeout_kind") == (None if code == "model_cancelled" else code)
    assert detail["transport"]["worker_exit_confirmed"] is True
    assert detail["provider_result_ref"] == "stage-provider-result/" + identity
    assert "PRIVATE_REASONING" not in json.dumps(safe_failure_details(failure.value, include_chain=True))
    assert case.app.evidence.read("result", identity) is None
    assert case.app.store.load_d4_operation(identity).completion is None
    before = case.app.evidence.read("provider-result", identity)
    with pytest.raises(OperationRecoveryRequired, match="operation_outcome_unknown"):
        asyncio.run(case.app.run(case.token))
    assert case.app.evidence.read("provider-result", identity) == before
    assert len(case.models.inner.calls) == 1 and not case.tools.calls


@pytest.mark.parametrize("exit_confirmed,changed_binding", [(False, False), (True, True)])
def test_stage_stop_diagnostic_never_settles_or_trusts_other_identity(case, exit_confirmed, changed_binding):
    from agent_core.modeling import ModelError, ModelErrorKind
    def invoke(envelope, timeout, token):
        save_provider_stop(case, envelope, "model_total_timeout",
            exit_confirmed=exit_confirmed, changed_binding=changed_binding)
        return ModelError(ModelErrorKind.TIMED_OUT, "d8_transport_outcome_unknown")
    case.models.inner.invoke = invoke
    with pytest.raises(OperationRecoveryRequired) as failure:
        asyncio.run(case.app.run(case.token))
    detail = failure.value.context["model_call"]
    assert detail["diagnostic_status"] == ("binding_mismatch" if changed_binding else "bound")
    assert ("provider_result_ref" in detail) is not changed_binding
    if not changed_binding:
        assert detail["transport"]["worker_exit_confirmed"] is False
    assert case.app.store.list_d4_operations(case.app.task_id)[0].completion is None


def test_stage_transport_can_close_diagnostic_after_budget_without_new_generation(case, monkeypatch):
    import time
    from agent_adapters import stage_evidence
    from agent_core.modeling import ModelError, ModelErrorKind
    monkeypatch.setattr(stage_evidence, "TASK_DEADLINE_STAGES", frozenset())
    monkeypatch.setattr(stage_evidence, "model_call_timeout", lambda model, remaining: .03)
    monkeypatch.setattr(stage_evidence, "MODEL_DIAGNOSTIC_GRACE_SECONDS", .3)
    calls = []
    def invoke(envelope, timeout, token):
        calls.append(envelope.request.model_request_id)
        while not token.is_cancelled:
            time.sleep(.002)
        assert token.is_timed_out
        time.sleep(.025)  # Simulate worker cleanup / immutable diagnostic IO.
        save_provider_stop(case, envelope, "model_total_timeout")
        return ModelError(ModelErrorKind.TIMED_OUT, "model_total_timeout")
    case.models.inner.invoke = invoke
    with pytest.raises(OperationRecoveryRequired) as failure:
        asyncio.run(case.app.run(case.token))
    detail = failure.value.context["model_call"]
    assert detail["diagnostic_status"] == "bound" and detail["timeout_kind"] == "model_total_timeout"
    assert detail["limit_seconds"] == .03 and .03 <= detail["elapsed_seconds"] < .6
    assert len(calls) == 1 and case.app.evidence.read("result", calls[0]) is None


@pytest.mark.parametrize("stop", ["watchdog", "cancel", "deadline", "late_proposal"])
def test_stage_watchdog_grace_is_bounded_and_never_accepts_late_or_cancelled_proposal(case, monkeypatch, stop):
    import threading
    import time
    from agent_adapters import stage_evidence
    monkeypatch.setattr(stage_evidence, "TASK_DEADLINE_STAGES", frozenset())
    monkeypatch.setattr(stage_evidence, "model_call_timeout", lambda model, remaining: .025)
    monkeypatch.setattr(stage_evidence, "MODEL_DIAGNOSTIC_GRACE_SECONDS", .3 if stop == "late_proposal" else .03)
    release, ended = threading.Event(), threading.Event()
    def invoke(envelope, timeout, token):
        try:
            if stop == "cancel": case.token.is_cancelled = True
            if stop == "deadline": case.now.value += 4000
            if stop == "late_proposal": time.sleep(.04)
            else: release.wait(1)
            return D8ModelResponse(json.dumps({"kind": "turn_intent", "description": "late",
                "completion_criterion": "must not register", "support_refs": []}))
        finally:
            ended.set()
    case.models.inner.invoke = invoke
    try:
        with pytest.raises(OperationRecoveryRequired) as failure:
            asyncio.run(case.app.run(case.token))
        detail = failure.value.context["model_call"]
        assert detail["reason"] == {"watchdog": "watchdog_timeout", "cancel": "user_cancelled",
            "deadline": "task_deadline", "late_proposal": "generation_budget_expired"}[stop]
        operation = case.app.store.list_d4_operations(case.app.task_id)[0]
        assert operation.completion is None and case.app.evidence.read("result", operation.start.operation_id) is None
        assert not case.app.store.list_stage_turns(case.app.task_id)
    finally:
        release.set()
        assert ended.wait(1)


def test_stage_adapter_exception_reports_safe_cause(case):
    from agent_adapters.output_evidence import safe_failure_details
    def invoke(envelope, timeout, token):
        raise ConnectionError("PRIVATE_PROVIDER_ERROR")
    case.models.inner.invoke = invoke
    with pytest.raises(OperationRecoveryRequired) as failure:
        asyncio.run(case.app.run(case.token))
    details = safe_failure_details(failure.value, include_chain=True)
    assert details["model_call"]["reason"] == "adapter_exception"
    assert details["model_call"]["exception_type"] == "ConnectionError"
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(details)


def test_stage_stop_links_actual_provider_adapter_metadata_without_network(case):
    from agent_adapters.deepseek_roles import D8DeepSeekModel, role_config, TRANSPORT
    from agent_adapters.deepseek_model import DeepSeekSettings
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_core.output_contracts import D8OutputRequest
    from agent_core.stage_planning import model_contract
    role = role_config("inner_action", version=17)
    settings = DeepSeekSettings(role.model, "offline-stage-boundary-secret", base_url=TRANSPORT["base_url"],
        allow_custom_endpoint=TRANSPORT["base_url"] != "https://api.deepseek.com")
    calls = []
    class Transport:
        def _invoke_payload(self, payload, timeout, token, *, diagnostic):
            calls.append(payload)
            diagnostic.update(transport_end="timeout", elapsed_seconds=.1, reasoning_chars=12,
                content_chars=0, worker_exit_confirmed=True, first_output_seconds=.01, last_output_seconds=.09)
            return ModelError(ModelErrorKind.TIMED_OUT, "model_total_timeout")
    model = D8DeepSeekModel(role, settings, transport=Transport())
    model.bind_diagnostics(case.app.evidence)
    request = replace(case.app._request("stage_intent", PlanningSession("turn_planning", case.app._id("stage/root"))),
        role_config_ref=role.role_config_ref)
    contract = model_contract(request)
    envelope = D8OutputRequest(request, {"version": 1, "stage": request.stage, "schema": contract["schema"]},
        contract["instruction"], None)
    with pytest.raises(OperationRecoveryRequired) as failure:
        asyncio.run(case.app.models._invoke(model, envelope, 1, case.token, case.app.state.task_run.deadline_at))
    detail = failure.value.context["model_call"]
    assert len(calls) == 1 and calls[0]["reasoning_effort"] == "high" and "max_tokens" not in calls[0]
    assert detail["diagnostic_status"] == "bound" and detail["timeout_kind"] == "model_total_timeout"
    assert detail["transport"]["reasoning_chars"] == 12 and detail["transport"]["content_chars"] == 0
    assert detail["provider_result_ref"] == "stage-provider-result/" + request.model_request_id


def test_fact_owner_schema_update_rolls_back_without_losing_canonical_fact(case, monkeypatch):
    import sqlite3
    from agent_core import stage_storage
    from agent_core.control_store import StorageError
    from agent_core.sqlite_control_store import SqliteControlStore
    result = asyncio.run(case.app.run(case.token))
    task_id = case.app.task_id
    path = case.path / "control.sqlite"
    asyncio.run(case.app.close())
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT * FROM facts").fetchall()
        db.execute("DROP TABLE stage_plan_bindings")
        db.execute("DROP TABLE stage_branches")
        db.execute("DROP TABLE stage_review_queue")
        db.execute("DELETE FROM schema_versions WHERE version>=33")
    original = stage_storage.FACT_SCHEMA
    monkeypatch.setattr(stage_storage, "FACT_SCHEMA", original.replace("CREATE INDEX idx_facts_context", "CREATE INDX idx_facts_context"))
    with pytest.raises(StorageError, match="schema migration failed"):
        SqliteControlStore.open(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM facts").fetchall() == before
        assert db.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 32
    monkeypatch.setattr(stage_storage, "FACT_SCHEMA", original)
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    assert case.app.store.load_turn(task_id, result.turn.turn_id).achievement == result.turn.achievement
    assert len([i for i in case.app.evidence.history(task_id) if i.record_type == "verified_fact"]) == 1


def test_dispatch_cannot_replace_the_admitted_inner_tool_arguments(case, monkeypatch):
    from agent_core.actions import MCPCall
    execute = case.app.mcp.execute
    async def tampered(request_id, road_id, call, token):
        return await execute(request_id, road_id, MCPCall(call.tool_name, {"script": "a different action"}), token)
    monkeypatch.setattr(case.app.mcp, "execute", tampered)
    with pytest.raises(StageStateError, match="tool_proposal_changed"):
        asyncio.run(case.app.run(case.token))
    assert not case.tools.calls
    assert not [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == "tool_dispatch"]


def test_changed_prompt_contract_cannot_reinterpret_an_existing_result(case, monkeypatch):
    from agent_core.stage_planning import RULES
    asyncio.run(case.app.run(case.token))
    operation = next(op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == "planning")
    original = RULES["stage_intent"]
    monkeypatch.setitem(RULES, "stage_intent", original + " a different contract")
    with pytest.raises(StageStateError, match="model_contract_changed"):
        case.app.evidence.read_model_result(operation)


@pytest.fixture
def overlap_case(case):
    request = case.app.state.task_run.request
    asyncio.run(case.app.close())
    case.path = case.path / "overlap"
    case.app = StageApplication(case.path, **case.args)
    control = SimpleNamespace(started={key: asyncio.Event() for key in "ABC"},
        release={key: asyncio.Event() for key in "ABC"}, active=set(), maximum=0, calls=[], failure=None,
        known_failure=None, confirmed_stop=False)
    async def operation(name, arguments):
        label = (arguments.get("query") or arguments.get("url") or arguments["script"]).split()[-1].rsplit("/", 1)[-1]
        control.calls.append((label, name))
        control.active.add(label)
        control.maximum = max(control.maximum, len(control.active))
        control.started[label].set()
        try:
            await control.release[label].wait()
            if control.failure == label:
                raise RuntimeError("controlled unknown tool outcome")
            return label
        finally:
            control.active.remove(label)
    async def public(call, timeout, token, target):
        try:
            label = await operation(call.tool_name, call.arguments)
        except asyncio.CancelledError:
            if not control.confirmed_stop:
                raise
            return dict(status="failed", ok=False, identity="public_reference_not_target_observation", code="retrieval.cancelled")
        return dict(status="failed" if control.known_failure == label else "completed", ok=control.known_failure != label,
            identity="public_reference_not_target_observation", content=label)
    async def sandbox(name, arguments):
        label = await operation(name, arguments)
        case.tools.calls.append((name, arguments))
        return MCPExecutionResult(case.app.task_id, "controlled-sandbox", name, "bash", ("-lc", arguments["script"]),
            "completed", 0, label, "", False)
    case.tools.call_tool = sandbox
    case.app.mcp.public_web = SimpleNamespace(binding={"backend": "controlled"}, validate=lambda call: None, call=public)
    case.app.start(request, effective_model_backend=ModelBackend.CODEX_CLI)
    case.tools.task_id = case.app.task_id
    control.decide = lambda req: {"kind": "road_resolution", "outcome": "failed", "reason": "controlled exploration ended",
        "support_refs": [i.ref for i in req.context.history if i.record_type == "progress"]}
    def transform(req, raw):
        if req.stage == "stage_action":
            return control.decide(req)
        if req.stage == "stage_completion":
            return {"kind": "incomplete", "reason": "no formal result claimed", "support_refs": []}
        return raw
    case.models.inner.transform = transform
    case.overlap = control
    return case


@pytest.mark.parametrize("tool", ["web.search", "web.fetch", "shell.run_bash"])
def test_single_tool_waits_for_result_without_speculative_inner(overlap_case, tool):
    case, control = overlap_case, overlap_case.overlap
    actions = []
    def decision(req):
        actions.append(req)
        if len(actions) == 1:
            key = "query" if tool == "web.search" else "url" if tool == "web.fetch" else "script"
            return dict(kind="mcp_call", tool_name=tool,
                arguments={key: "https://docs.example/A" if tool == "web.fetch" else "A"})
        assert not req.context.pending_operation_ids
        assert len([i for i in req.context.history if i.record_type == "progress"]) == 1
        return dict(kind="road_resolution", outcome="failed", reason="controlled end", support_refs=[])
    control.decide = decision
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        try:
            await asyncio.wait_for(control.started["A"].wait(), 5)
            await asyncio.sleep(.08)
            assert len(actions) == 1 and control.active == {"A"}
            control.release["A"].set()
            await asyncio.wait_for(pending, 5)
        finally:
            control.release["A"].set()
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    assert len(actions) == 2 and len(control.calls) == 1


def test_results_arriving_during_generation_merge_without_interrupting_inner(overlap_case):
    import threading
    case, control = overlap_case, overlap_case.overlap
    case.models.inner.native = case.models.outer.native = True
    entered, release_model = threading.Event(), threading.Event()
    actions = []
    def decision(req):
        actions.append(req)
        if len(actions) == 1:
            return dict(kind="mcp_calls", calls=[dict(tool_name="web.search", arguments={"query": label}) for label in "AB"])
        if len(actions) == 2:
            entered.set()
            assert release_model.wait(5)
            return dict(kind="wait_for_tools", reason="await the remaining response")
        return dict(kind="road_resolution", outcome="failed", reason="controlled end", support_refs=[])
    control.decide = decision
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        try:
            await asyncio.wait_for(control.started["B"].wait(), 5)
            control.release["A"].set()
            while not entered.is_set(): await asyncio.sleep(0.01)
            assert len([i for i in actions[1].context.history if i.record_type == "progress"]) == 1
            control.release["B"].set()
            while control.active: await asyncio.sleep(0.01)
            await asyncio.sleep(0.04)
            assert len(actions) == 2 and not pending.done()
            release_model.set()
            await asyncio.wait_for(pending, 5)
        finally:
            release_model.set()
            for gate in control.release.values(): gate.set()
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    assert len(actions) == 3 and not actions[-1].context.pending_operation_ids
    assert len([i for i in actions[-1].context.history if i.record_type == "progress"]) == 2


def test_concurrent_unknown_keeps_other_known_result_and_blocks_restart(overlap_case):
    case, control = overlap_case, overlap_case.overlap
    actions = []
    def decision(req):
        actions.append(req)
        return dict(kind='mcp_calls', calls=[dict(tool_name='web.search', arguments={'query': label}) for label in 'AB'])
    control.decide, control.failure = decision, "A"
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        await asyncio.wait_for(control.started["B"].wait(), 5)
        control.release["A"].set()
        control.release["B"].set()
        with pytest.raises(Exception, match="retrieval.outcome_unknown"):
            await asyncio.wait_for(pending, 5)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    records = [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == "tool_dispatch"]
    assert len(records) == 2 and sum(op.completion is not None for op in records) == 1
    assert case.app.store.blocking_d4_operations(case.app.task_id) and not case.app._tools_in_flight
    task_id, count = case.app.task_id, len(case.models.inner.calls)
    asyncio.run(case.app.close())
    case.app = StageApplication(case.path, **case.args)
    case.app.bind(task_id)
    with pytest.raises(OperationRecoveryRequired): asyncio.run(case.app.run(case.token))
    assert len(case.models.inner.calls) == count and len(control.calls) == 2


@pytest.mark.parametrize("known_failure", [None, "B"])
def test_free_slot_is_reused_but_road_review_waits_for_all_tools(overlap_case, known_failure):
    case, control = overlap_case, overlap_case.overlap
    control.known_failure = known_failure
    actions = []
    def decision(req):
        actions.append(req)
        if len(actions) == 1:
            return dict(kind="mcp_calls", calls=[dict(tool_name="web.search", arguments={"query": label}) for label in "AB"])
        if len(actions) == 2:
            return dict(kind="mcp_call", tool_name="shell.run_bash", arguments={"script": "C"})
        return dict(kind="road_resolution", outcome="failed", reason="controlled end", support_refs=[])
    control.decide = decision
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        try:
            await asyncio.wait_for(control.started["B"].wait(), 5)
            control.release["B"].set()
            await asyncio.wait_for(control.started["C"].wait(), 5)
            assert control.active == {"A", "C"} and control.maximum == 2
            control.release["C"].set()
            while len(actions) < 3: await asyncio.sleep(0.01)
            await asyncio.sleep(0.04)
            assert not any(isinstance(req.proposal, RoadResolution) for req in case.models.outer.calls)
            assert case.app.store.list_stage_turns(case.app.task_id)[0].roads[0].status is IntentStatus.RUNNING
            control.release["A"].set()
            await asyncio.wait_for(pending, 5)
        finally:
            for gate in control.release.values(): gate.set()
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    assert len(control.calls) == 3 and control.maximum == 2
    assert not any(isinstance(req.proposal, MCPCall) for req in case.models.outer.calls)


def test_wait_for_tools_requires_pending_operation_and_has_no_tool_effect(review_request):
    request = replace(review_request, stage="stage_action", proposal=None)
    value = D8ModelResponse(json.dumps(dict(kind="wait_for_tools", reason="depends on pending observation")))
    with pytest.raises(ValueError, match="stage.output_invalid"):
        parse(request, value)
    request = replace(request, context=replace(request.context, pending_operation_ids=(str(uuid4()),)))
    assert parse(request, value) == StagePlanningDecision("wait_for_tools", "depends on pending observation", ())


def test_cancelled_concurrent_retrievals_keep_confirmed_stop_results(overlap_case):
    case, control = overlap_case, overlap_case.overlap
    control.confirmed_stop = True
    actions = []
    def decision(req):
        actions.append(req)
        return dict(kind='mcp_calls', calls=[dict(tool_name='web.search', arguments={'query': label}) for label in 'AB'])
    control.decide = decision
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        await asyncio.wait_for(control.started["B"].wait(), 5)
        case.token.is_cancelled = True
        with pytest.raises(Exception, match="dispatch_stopped"):
            await asyncio.wait_for(pending, 5)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    records = [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == "tool_dispatch"]
    assert len(records) == 2 and all(op.completion is not None and op.completion.result_kind == "failed" for op in records)
    assert not case.app._tools_in_flight and not case.app.store.blocking_d4_operations(case.app.task_id)
    assert not control.active and len(actions) == 1


@pytest.mark.parametrize('first_tool,second_tool,known_failure', [
    ('web.search', 'web.search', None), ('web.search', 'web.fetch', 'B'),
    ('shell.run_bash', 'web.search', None)])
def test_one_decision_starts_two_tools_and_first_result_resumes(overlap_case, first_tool, second_tool, known_failure):
    case, control = overlap_case, overlap_case.overlap
    control.known_failure = known_failure
    case.models.inner.native = True
    actions = []
    def decision(req):
        actions.append(req)
        if len(actions) == 1:
            return dict(kind='mcp_calls', calls=[dict(tool_name=tool, arguments={
                'query' if tool == 'web.search' else 'url' if tool == 'web.fetch' else 'script':
                'https://docs.example/B' if tool == 'web.fetch' else label})
                for tool, label in [(first_tool, 'A'), (second_tool, 'B')]])
        if len(actions) == 2:
            assert len(req.context.pending_operation_ids) == 1
            assert len([i for i in req.context.history if i.record_type == 'progress']) == 1
            return dict(kind='wait_for_tools', reason='the remaining result is needed')
        return dict(kind='road_resolution', outcome='failed', reason='controlled end', support_refs=[])
    control.decide = decision
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        try:
            await asyncio.wait_for(control.started['B'].wait(), 5)
            assert control.active == {'A', 'B'} and len(actions) == 1
            with pytest.raises(StageStateError, match='tools_still_running'):
                case.app._commit(StageStateChange(turns=case.app.store.list_stage_turns(case.app.task_id)))
            control.release['B'].set()
            while len(actions) < 2: await asyncio.sleep(.01)
            assert not control.release['A'].is_set()
            control.release['A'].set()
            await asyncio.wait_for(pending, 5)
        finally:
            for gate in control.release.values(): gate.set()
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    records = [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == 'tool_dispatch']
    assert len(records) == len({op.start.operation_id for op in records}) == 2
    parents = {case.app.evidence.read('mcp-call', op.start.operation_id)['model_request_id'] for op in records}
    assert parents == {actions[0].model_request_id}
    assert all(op.completion is not None for op in records) and len(actions) == 3
    assert not any(req.correction_of_model_request_id for req in actions)
    assert not any(type(req.proposal).__name__ == 'StageToolBatch' for req in case.models.outer.calls)
    before = len(control.calls)
    case.app.mcp.reconcile()
    assert len(control.calls) == before


@pytest.mark.parametrize('native', [False, True])
def test_batch_fact_uses_the_original_private_tool_receipt(overlap_case, native):
    case, control = overlap_case, overlap_case.overlap
    case.models.inner.native = native
    for label in 'AB': control.release[label].set()
    actions = []
    def decision(req):
        actions.append(req)
        if len(actions) == 1:
            return dict(kind='mcp_calls', calls=[dict(tool_name='shell.run_bash', arguments={'script': 'A'}),
                dict(tool_name='web.search', arguments={'query': 'B'})])
        observations = [i.ref for i in req.context.history if i.identity == 'tool_observation_not_fact']
        facts = [i.ref for i in req.context.history if i.record_type == 'verified_fact']
        if not facts:
            return dict(kind='fact_candidate', content='The local tool returned A.', reason='original private-tool observation',
                support_refs=observations, inferred=False)
        return dict(kind='road_resolution', outcome='succeeded', reason='controlled result obtained', support_refs=facts)
    control.decide = decision
    asyncio.run(case.app.run(case.token))
    source = case.app.store._connection.execute('SELECT source_ref FROM facts').fetchone()[0]
    claim = case.app.evidence.read('mcp-call', source)
    assert source != actions[0].model_request_id and claim['model_request_id'] == actions[0].model_request_id
    assert claim['call']['tool_name'] == 'shell.run_bash' and len(control.calls) == 2
    from scripts.qymonitor import tool_usage
    counts, _ = tool_usage(case.app.directory)
    assert dict(counts) == {'shell.run_bash': 1, 'web.search': 1}


@pytest.mark.parametrize('invalid', ['duplicate', 'two_private', 'three', 'one_pending', 'unknown_tool'])
def test_invalid_tool_batch_is_corrected_before_any_dispatch(overlap_case, invalid):
    case, control = overlap_case, overlap_case.overlap
    calls = [dict(tool_name='web.search', arguments={'query': label}) for label in 'AB']
    if invalid == 'duplicate': calls[1] = calls[0]
    if invalid == 'two_private': calls = [dict(tool_name='shell.run_bash', arguments={'script': label}) for label in 'AB']
    if invalid == 'three': calls.append(dict(tool_name='web.search', arguments={'query': 'C'}))
    if invalid == 'unknown_tool': calls[1]['tool_name'] = 'unknown.tool'
    actions = []
    def decision(req):
        actions.append(req)
        if invalid == 'one_pending' and len(actions) == 1:
            return dict(kind='mcp_calls', calls=[dict(tool_name='web.search', arguments={'query': label}) for label in 'AC'])
        if req.correction_of_model_request_id:
            return dict(kind='road_resolution', outcome='failed', reason='controlled correction', support_refs=[])
        return dict(kind='mcp_calls', calls=calls)
    control.decide = decision
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        try:
            if invalid == 'one_pending':
                await asyncio.wait_for(control.started['C'].wait(), 5)
                control.release['C'].set()
            while not any(req.correction_of_model_request_id for req in actions): await asyncio.sleep(.01)
            assert control.calls == ([('A', 'web.search'), ('C', 'web.search')] if invalid == 'one_pending' else [])
            if invalid == 'two_private':
                feedback = case.app.evidence.contract(actions[-1])["correction"]["issues"]
                assert feedback[0]["code"] == "stage.tool_batch_resource_conflict"
                assert feedback[0]["actual"] == "two_private_actions"
            control.release['A'].set()
            await asyncio.wait_for(pending, 5)
        finally:
            for gate in control.release.values(): gate.set()
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(asyncio.wait_for(exercise(), 10))
    assert len([req for req in actions if req.correction_of_model_request_id]) == 1
    assert len(control.calls) == (2 if invalid == 'one_pending' else 0)


@pytest.mark.parametrize("size", [1100000, 2200000])
def test_storage_capacity_stops_before_dispatch_with_safe_metadata(case, size):
    from agent_adapters.output_evidence import safe_failure_details
    from qinyuan_core import Category
    request = case.app._request("stage_intent", PlanningSession("turn_planning", str(uuid4())))
    request = replace(request, messages=({"role": "user", "content": "x" * size},))
    with pytest.raises(StageStateError, match="stage.evidence_capacity_exceeded") as caught:
        asyncio.run(case.app.models.call(request, case.token))
    assert caught.value.category is Category.CAPACITY
    detail = safe_failure_details(caught.value, include_chain=True)["evidence_capacity"]
    assert detail["record_kind"] == "stage-request" and detail["record_id"] == request.model_request_id
    assert detail["actual_bytes"] > detail["limit_bytes"] and detail["dispatched"] is False
    assert not case.models.inner.calls and not case.app.store.list_d4_operations(case.app.task_id)
    saved = case.app.evidence.read("request", request.model_request_id)
    assert bool(saved) == (size < 2 * 1024 * 1024)


def test_storage_capacity_triggers_bounded_summary_before_model_admission(case):
    prepare_capacity_episode(case)
    case.app.models.capacity_port = SimpleNamespace(fits=lambda envelope, policy: True)
    original = case.tools.call_tool
    async def large_observation(name, arguments):
        return replace(await original(name, arguments), stdout="observed " + "x" * 50000)
    case.tools.call_tool = large_observation
    result = asyncio.run(case.app.run(case.token))
    summaries = [r for r in case.models.outer.calls if r.stage == "stage_summary"]
    assert result.turn.achievement is not None and summaries
    for summary in summaries:
        source = case.app.evidence.request(summary.capacity_recovery_of)
        data = {"request": asdict(source), "proposal_kind": type(source.proposal).__name__}
        from agent_core.command_contracts import T6EvidenceCapacityError
        with pytest.raises(T6EvidenceCapacityError):
            case.app.evidence.records.encode("stage-request", source.model_request_id, data, limit=1024 * 1024)
        assert case.app.store.load_d4_operation(source.model_request_id) is None
        assert source.model_request_id not in {r.model_request_id for r in case.models.inner.calls}
    for summary in summaries:
        covered = {identity for identity, _ in summary.summary_coverage}
        source = case.app.evidence.request(summary.capacity_recovery_of)
        decisions = [r for r in case.models.inner.calls if r.context.session == source.context.session
            and r.base_checkpoint_sequence < source.base_checkpoint_sequence and r.stage == "stage_action"]
        assert not covered.intersection(r.model_request_id for r in decisions[-4:])


@pytest.mark.parametrize("mcp_failure", [False, True])
def test_failed_tools_cannot_skip_owned_mcp_and_store_close(mcp_failure):
    closed = []
    async def exercise():
        owner = asyncio.current_task()
        original = RuntimeError("controlled tool cleanup failure")
        async def stop():
            closed.append("tools")
            raise original
        async def stop_reviews():
            closed.append("reviews")
        async def close_mcp():
            assert asyncio.current_task() is owner
            closed.append("mcp")
            if mcp_failure: raise ValueError("secondary cleanup failure")
        app = SimpleNamespace(_stop_tools=stop, reviews=SimpleNamespace(stop=stop_reviews), mcp=SimpleNamespace(close=close_mcp),
            store=SimpleNamespace(close=lambda: closed.append("store")))
        with pytest.raises(RuntimeError) as caught:
            await StageApplication.close(app)
        assert caught.value is original
        if mcp_failure: assert "stage.cleanup_failed:mcp" in caught.value.__notes__
    asyncio.run(exercise())
    assert closed == ["reviews", "tools", "mcp", "store"]


@pytest.mark.parametrize("failure", ["corrupt", "io"])
def test_stage_evidence_distinguishes_corruption_from_io(case, monkeypatch, failure):
    from qinyuan_core import Category
    identity = str(uuid4())
    path = case.app.evidence.records._path("stage-result", identity)
    if failure == "corrupt":
        path.write_bytes(b"invalid json")
    else:
        from agent_core.command_contracts import T6EvidenceError
        def fail_read(*args):
            try: raise PermissionError("private path must not appear in diagnostics")
            except PermissionError as error: raise T6EvidenceError("evidence_read_failed") from error
        monkeypatch.setattr(case.app.evidence.records, "_read", fail_read)
    with pytest.raises(StageStateError) as caught:
        case.app.evidence.read("result", identity)
    assert caught.value.code == ("stage.evidence_corrupt" if failure == "corrupt" else "stage.evidence_io_failed")
    assert caught.value.category is (Category.UNKNOWN if failure == "corrupt" else Category.DEPENDENCY)


def test_result_storage_capacity_keeps_original_unsettled_identity(case):
    from agent_adapters.output_evidence import safe_failure_details
    case.models.inner.transform = lambda request, raw: {**raw, "description": "x" * 1100000}
    with pytest.raises(StageStateError, match="stage.evidence_capacity_exceeded") as caught:
        asyncio.run(case.app.run(case.token))
    detail = safe_failure_details(caught.value)["evidence_capacity"]
    assert detail["dispatched"] is True and detail["record_kind"] == "stage-result"
    records = case.app.store.blocking_d4_operations(case.app.task_id)
    assert len(records) == 1 and records[0].start.operation_id == detail["record_id"]
    assert records[0].completion is None and len(case.models.inner.calls) == 1
    with pytest.raises(OperationRecoveryRequired):
        asyncio.run(case.app.run(case.token))
    assert len(case.models.inner.calls) == 1


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_live_model_ownership_is_revoked_on_interruption_without_replay(case, stop):
    import threading
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow(request, raw):
        entered.set()
        try:
            assert release.wait(5)
            return raw
        finally:
            finished.set()
    case.models.inner.transform = slow
    async def exercise():
        pending = asyncio.create_task(case.app.run(case.token))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert len(case.app.models.live_operations) == 1
            if stop == "cancel": case.token.is_cancelled = True
            else: case.now.value = case.app.state.task_run.deadline_at
            with pytest.raises(OperationRecoveryRequired, match="stage.model_outcome_unknown"):
                await asyncio.wait_for(pending, 5)
            assert not case.app.models.live_operations
            assert len(case.app.store.blocking_d4_operations(case.app.task_id)) == 1
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(exercise())
    case.token.is_cancelled = False
    case.now.value = 1800000000.0
    with pytest.raises(OperationRecoveryRequired): asyncio.run(case.app.run(case.token))
    assert len(case.models.inner.calls) == 1 and not case.app.models.live_operations


@pytest.mark.parametrize("known_rejection", [True, False])
def test_planner_rejection_settles_but_generic_mcp_error_remains_unknown(case, known_rejection):
    from MCPs.Mcp_client.client import MCPCommandPlanError, MCPClientError
    from agent_adapters.output_evidence import safe_failure_details
    case.tools.error = MCPCommandPlanError("mcp.planner_rejected") if known_rejection else MCPClientError("PRIVATE transport failure")
    actions = []
    def decision(req, raw):
        if req.stage == "stage_action":
            actions.append(req)
            if len(actions) > 1:
                assert req.context.history[0].identity == "tool_observation_not_fact"
                result = json.loads(req.context.history[0].content)
                assert result["status"] == "rejected" and result["code"] == "mcp.planner_rejected"
                return dict(kind="road_resolution", outcome="failed", reason="known planner rejection", support_refs=[])
        if req.stage == "stage_completion": return dict(kind="incomplete", reason="no success", support_refs=[])
        return raw
    case.models.inner.transform = decision
    if known_rejection:
        asyncio.run(case.app.run(case.token))
        assert len(actions) == 2 and not case.app.store.blocking_d4_operations(case.app.task_id)
    else:
        with pytest.raises(ExecutionRecoveryRequired) as caught: asyncio.run(case.app.run(case.token))
        detail = safe_failure_details(caught.value, include_chain=True)
        assert "mcp.outcome_unknown" in detail["error_codes"] and "MCPClientError" in detail["exception_types"]
        context = detail["operation_context"]
        pending = case.app.store.blocking_d4_operations(case.app.task_id)
        assert len(pending) == 1 and pending[0].start.operation_id == context["operation_id"]
        saved = case.app.evidence.read("mcp-failure", context["operation_id"])
        assert saved["operation_context"] == context and "PRIVATE" not in json.dumps(saved)
        with pytest.raises(OperationRecoveryRequired): asyncio.run(case.app.run(case.token))
    assert len(case.tools.calls) == 1
    assert not any(i.record_type == "verified_fact" for i in case.app.evidence.history(case.app.task_id))
