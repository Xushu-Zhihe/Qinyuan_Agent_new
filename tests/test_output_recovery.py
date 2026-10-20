"""Known ended response recovery stays inside one correction and existing budgets."""
import asyncio
import json
import pytest
from test_http import http_case
from test_intent_execution import install
from agent_core.output_contracts import D8ModelResponse

@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("stage", ["tree_planning","inner_plan","fact_review","intent_evaluation"])
def test_known_truncation_regenerates_once_without_repeating_execution(http_case,stage):
    c=http_case
    install(c,steps=1)
    model=c.models[1] if stage=="inner_plan" else c.models[0]
    original=model.invoke
    calls=[]
    def invoke(envelope,*args):
        if envelope.output_contract["stage"]==stage:
            calls.append(envelope)
            if len(calls)==1:return D8ModelResponse('{"incomplete":',"length")
        return original(envelope,*args)
    model.invoke=invoke
    result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,result.stop_reason
    assert len(c.sends)==1 and len(c.app.state.facts)==1
    assert calls[1].request.correction_of_model_request_id==calls[0].request.model_request_id
    assert calls[1].correction["issues"][0]["code"]=="output_truncated"
    assert calls[0].output_contract["input_projection"] == "shared_refs_v1"
    assert calls[1].output_contract["input_projection"] == "shared_refs_v1"
    diagnostic=c.app.output_boundary.diagnostic(calls[0].request.model_request_id)
    assert diagnostic["terminal"] is None and diagnostic["output_measurement"]["utf8_bytes"]>0

@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("exhausted", [False,True])
def test_repeated_or_budget_exhausted_truncation_preserves_previous_fact(http_case,exhausted):
    c=http_case
    install(c,steps=2)
    original=c.models[0].invoke
    calls=[]
    def invoke(envelope,*args):
        if envelope.output_contract["stage"]=="fact_review" and len(c.sends)==2:
            calls.append(envelope)
            if exhausted:
                identity=envelope.request.model_request_id
                c.app.output_boundary.evidence.save("provider-request",identity,{"parameters":{"max_tokens":32768}})
                c.app.output_boundary.evidence.save("provider-result",identity,{"transport":{"transport_end":"response_received","usage":{"completion_tokens":32768}}})
            return D8ModelResponse('{"incomplete":',"length")
        return original(envelope,*args)
    c.models[0].invoke=invoke
    result=asyncio.run(c.app.run(c.token))
    assert result.recovery_required
    assert result.stop_reason==("d8_model_output_budget_exhausted" if exhausted else "d8_model_output_truncated")
    assert len(calls)==(1 if exhausted else 2)
    assert len(c.app.state.facts)==1 and len(c.sends)==2
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot = snapshot_v3_task_result(c.directory, c.declaration)
    assert snapshot["errors"] == [], snapshot
    assert snapshot["recovery"]["reason"] == result.stop_reason
    assert len(snapshot["verified_facts"]) == 1
    asyncio.run(c.app.resume(c.task,c.token))
    assert len(calls)==(1 if exhausted else 2) and len(c.sends)==2


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_new_shared_payload_and_legacy_frozen_request_remain_separate(http_case, monkeypatch):
    from dataclasses import replace
    from agent_adapters.deepseek_roles import build_payload, payload_bytes
    c = http_case
    install(c, steps=1)
    captured = []
    invoke = c.models[1].invoke
    def capture(envelope, *args):
        captured.append(envelope)
        return invoke(envelope, *args)
    c.models[1].invoke = capture
    result = asyncio.run(c.app.run(c.token))
    assert not result.recovery_required, result
    envelope = next(e for e in captured if e.output_contract["stage"] == "inner_plan")
    boundary = c.app.output_boundary
    role = c.app.config.inner_role
    loader = c.app.web.model_context
    before = json.dumps(envelope.output_contract)
    projected = build_payload(envelope, role, context_loader=loader)
    assert isinstance(json.loads(projected["messages"][1]["content"])["history_context"], dict)
    legacy_shape = {k: v for k, v in envelope.output_contract.items() if k != "input_projection"}
    legacy = replace(envelope, output_contract=legacy_shape)
    original = build_payload(legacy, role, context_loader=loader)
    assert isinstance(json.loads(original["messages"][1]["content"])["history_context"], str)
    assert len(payload_bytes(projected)) < len(payload_bytes(original))
    assert projected["max_tokens"] == original["max_tokens"] == 32768
    assert projected["thinking"] == original["thinking"]
    assert json.dumps(envelope.output_contract) == before
    capacity = c.app.handoff.capacity_port
    assert capacity.fits(envelope, c.app.config.handoff_config.inner_capacity)
    assert capacity.last_measurement["payload_utf8_bytes"] == len(payload_bytes(projected))
    # A formerly duplicated packet can fit only after sharing. Admission must
    # not reject it with the unprojected business-request estimate first.
    from uuid import uuid4
    from agent_adapters.deepseek_roles import CAPACITY_POLICY
    history = json.loads(envelope.request.history_context)
    history["shared_test_detail"] = "x" * 140000
    session = json.loads(envelope.request.session_context)
    session["current_history"] = history
    larger = replace(envelope.request, model_request_id=str(uuid4()),
                     history_context=json.dumps(history), session_context=json.dumps(session))
    large_envelope = boundary._contract(larger)[0]
    assert capacity.fits(large_envelope, CAPACITY_POLICY)
    assert not capacity.fits(replace(large_envelope, output_contract={k: v for k, v in large_envelope.output_contract.items()
                               if k != "input_projection"}), CAPACITY_POLICY)
    assert c.app.handoff._fits_complete_request(larger, CAPACITY_POLICY)
    identity = envelope.request.model_request_id
    read = boundary.evidence.read
    saved = read("contract", identity)
    old = {**saved, "output_contract": legacy_shape}
    monkeypatch.setattr(boundary.evidence, "read", lambda kind, key: old if kind == "contract" and key == identity else read(kind, key))
    assert "input_projection" not in boundary._contract(envelope.request)[0].output_contract
    assert boundary.envelope(envelope.request).output_contract == legacy_shape

    correction = replace(envelope.request, model_request_id=str(uuid4()),
                         correction_of_model_request_id=identity)
    assert "input_projection" not in boundary._contract(correction)[0].output_contract
