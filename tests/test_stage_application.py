"""U3 real controller/SQLite/evidence/MCP executor, controlled model and tool IO."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.stage_application import StageApplication
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_core.contracts import TaskRequest, ModelBackend
from agent_core.domain import IntentStatus
from agent_core.control_store import TargetNetworkBinding
from agent_core.handoff_contracts import D8HandoffConfig, D8CapacityPolicy
from agent_core.dual_llm_contracts import D3RunConfig, D3Authorization
from agent_core.operation_accounting import D8HandoffBudgetPolicy, D4RecoveryRequired
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


def test_road_success_does_not_imply_stage_success(case):
    case.models.inner.complete = False
    result = asyncio.run(case.app.run(case.token))
    assert result.reason == "stage.exhausted"
    assert result.turn.achievement is None
    assert result.turn.roads[0].status is IntentStatus.SUCCEEDED


def test_outer_rejection_does_not_register_and_feedback_stays_in_root_session(case):
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
    assert len(case.app.store.list_stage_turns(case.app.task_id)) == 1
    assert len(case.tools.calls) == 1


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
    with pytest.raises(D4RecoveryRequired):
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
        assert len(actions) == 2 and actions[1].correction_of_model_request_id == actions[0].model_request_id
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
    assert len(intents) == 2 and intents[1].correction_of_model_request_id == intents[0].model_request_id
    invalid = [entry for event, entry in seen if event == "stage.output_rejected"]
    assert len(invalid) == (2 if repeat else 1)
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
    case.models.outer.native = native
    def transform(request, raw):
        if request.stage == "stage_review" and isinstance(request.proposal, StageFactProposal) and not request.correction_of_model_request_id:
            return {**raw, "reason": "a" * 806}
        return raw
    case.models.outer.transform = transform
    assert asyncio.run(case.app.run(case.token)).turn.achievement is not None
    corrected = [e for e in case.models.outer.envelopes if e.correction]
    assert corrected
    for envelope in corrected:
        feedback = envelope.correction
        assert feedback["correction_of_model_request_id"] == envelope.request.correction_of_model_request_id
        issue = feedback["issues"][0]
        assert issue["code"] == "string_too_long" and issue["path"] == ("$/result/reason" if native else "$/reason")
        assert issue["expected"] == "unicode_codepoints_le_512" and issue["actual"] == "unicode_codepoints_806"
        frozen = case.app.evidence.read("contract", envelope.request.model_request_id)
        assert frozen["correction"] == feedback
        assert "a" * 806 not in json.dumps(feedback)
        from agent_adapters.deepseek_roles import build_payload, role_config
        role = role_config("outer_planning", version=17)
        outgoing = replace(envelope, request=replace(envelope.request, role_config_ref=role.role_config_ref))
        payload = build_payload(outgoing, role)
        assert '"actual": "unicode_codepoints_806"' in payload["messages"][0]["content"]


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


@pytest.mark.parametrize("first", ["format", "business"])
def test_completion_business_and_format_errors_share_one_correction(case, first):
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
    assert error.value.category == "input" and len(attempted) == 2 and len(case.tools.calls) == 1
    assert attempted[1].correction_of_model_request_id == attempted[0].model_request_id
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


def test_ordinary_stage_reason_is_not_truncated_or_given_a_field_limit(case):
    long_reason = "r" * 2000
    def transform(request, raw):
        if "reason" in raw and raw.get("kind") != "fact_candidate" and not isinstance(request.proposal, StageFactProposal):
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
    assert len(case.models.inner.calls) == 2
    assert not case.models.outer.calls and not case.tools.calls
    assert not case.app.store.list_stage_turns(case.app.task_id)


def test_supervision_keeps_original_four_decision_cadence_without_extra_planning(case):
    counter = []
    def transform(request, raw):
        if request.stage == "stage_action" and len(counter) < 5:
            counter.append(True)
            return {"kind": "mcp_call", "tool_name": "shell.run_bash", "arguments": {"script": "printf observed"}}
        return raw
    case.models.inner.transform = transform
    result = asyncio.run(case.app.run(case.token))
    assert result.turn.achievement is not None and len(case.tools.calls) == 5
    reviews = [r for r in case.models.outer.calls if r.stage == "stage_supervision"]
    assert len(reviews) == 1
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
    with pytest.raises(D4RecoveryRequired, match="dispatch_stopped") as error:
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
    assert "Do not copy the contract envelope" in payload["messages"][0]["content"]
    assert payload["tool_choice"] == "auto" and payload["tools"][0]["function"]["name"] == OUTPUT_TOOL
    assert "response_format" not in payload
    history = replace(request, messages=({"role": "assistant", "content": '{"kind":"roads"}'},))
    projected = build_payload(replace(envelope, request=history), inner)
    assert projected["messages"][1]["role"] == "user"
    assert json.loads(projected["messages"][1]["content"])["historical_model_result"] == '{"kind":"roads"}'
    assert payload["thinking"] == {"type": "enabled"} and payload["reasoning_effort"] == "high"
    assert "max_tokens" not in payload and payload["stream"] is True
    with pytest.raises(ValueError, match="model_role_binding_changed"):
        build_payload(envelope, outer)
    request = replace(request, stage="stage_summary", role_config_ref=outer.role_config_ref)
    envelope = replace(envelope, request=request, output_contract={"version": 1, "stage": request.stage, "schema": schema(request)})
    assert build_payload(envelope, outer)["reasoning_effort"] == "high"


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
    with pytest.raises(D4RecoveryRequired, match="model_outcome_unknown") as error:
        asyncio.run(case.app.run(case.token))
    from qinyuan_core import Category, classify
    assert classify(error.value) == (Category.UNKNOWN, "stage.model_outcome_unknown", False)
    with pytest.raises(D4RecoveryRequired, match="operation_outcome_unknown"):
        asyncio.run(case.app.run(case.token))
    assert len(case.models.inner.calls) == 1 and not case.tools.calls


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
