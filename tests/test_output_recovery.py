"""Known ended response recovery stays inside one correction and existing budgets."""
import asyncio
import json
import pytest
from test_http import http_case
from test_output import output_case
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
    assert "max_tokens" not in projected and "max_tokens" not in original
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


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("stage", ["fact_proposal", "fact_review"])
@pytest.mark.parametrize("bad", ["length", "missing"])
def test_optional_fact_format_exhaustion_keeps_observation_and_continues(http_case, stage, bad):
    from agent_adapters.deepseek_roles import build_payload
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c = http_case
    install(c, steps=2, stop="inconclusive")
    model = c.models[1] if stage == "fact_proposal" else c.models[0]
    original = model.invoke
    attempts = []
    def invoke(envelope, *args):
        raw = original(envelope, *args)
        if envelope.output_contract["stage"] == stage and len(c.sends) == 1:
            attempts.append(envelope)
            if bad == "missing":
                return {k: v for k, v in raw.items() if k != "reason"}
            if stage == "fact_proposal" and len(attempts) == 1:
                return {**raw, "content": "PRIVATE_REJECTED_TEXT" * 20, "reason": "R" * 513}
            return {**raw, "reason": "R" * 513}
        return raw
    model.invoke = invoke
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert len(attempts) == 2 and len(c.sends) == 2 and len(c.app.state.facts) == 1
    assert c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id).outcome == "observed"
    assert c.app.state.intents[0].status.value == "inconclusive"
    if bad == "length":
        issues = attempts[1].correction["issues"]
        assert any(i["path"] == "$/reason" and i["expected"] == "unicode_codepoints_le_512"
                   and i["actual"] == "unicode_codepoints_513" for i in issues)
        if stage == "fact_proposal":assert any(i["path"] == "$/content" for i in issues)
    role = c.app.config.inner_role if stage == "fact_proposal" else c.app.config.outer_role
    system = build_payload(attempts[0], role, context_loader=c.app.web.model_context)["messages"][0]["content"]
    if stage == "fact_proposal":assert "it has no per-field character limit" not in system
    assert "maximum_unicode_codepoints" in system and "not UTF-8 bytes" in system
    notices = [c.app.sessions.payload(r)["observation"].get("optional_conclusion_failure") for r in c.app.sessions.records()]
    assert any(n and n["identity"] == "unreviewed_optional_format_failure" for n in notices)
    snapshot = snapshot_v3_task_result(c.directory, c.declaration)
    assert not snapshot["recovery"]["required"] and not snapshot["errors"], snapshot
    assert "PRIVATE_REJECTED_TEXT" not in "".join(p.read_text(encoding="utf-8") for p in (c.directory/"d8-output").glob("*.json"))
    before = len(c.app.store.list_d4_operations(c.task)), len(c.sends)
    c.app.store.close(); c.app = c.create()
    assert asyncio.run(c.app.resume(c.task,c.token)).stop_reason == "search_exhausted"
    assert (len(c.app.store.list_d4_operations(c.task)),len(c.sends)) == before


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("after_commit", [False, True])
def test_optional_failure_recovers_observation_commit_without_reasking(http_case, monkeypatch, after_commit):
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    install(c, steps=2, stop="inconclusive")
    invoke = c.models[1].invoke
    attempts = []
    def malformed(envelope,*args):
        raw=invoke(envelope,*args)
        if envelope.output_contract["stage"]=="fact_proposal" and len(c.sends)==1:
            attempts.append(envelope.request.model_request_id)
            return {**raw,"content":"x"*257}
        return raw
    c.models[1].invoke=malformed
    complete=c.app.control.complete_plan
    def lost(*args,**kwargs):
        if args[1].outcome=="observed":
            if after_commit:complete(*args,**kwargs)
            raise ProcessLoss("optional format receipt saved; observation boundary")
        return complete(*args,**kwargs)
    monkeypatch.setattr(c.app.control,"complete_plan",lost)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    assert len(attempts)==2 and len(c.sends)==1
    c.app.store.close();c.app=c.create()
    result=asyncio.run(c.app.resume(c.task,c.token))
    assert result.stop_reason=="search_exhausted",result
    assert len(attempts)==2 and len(c.sends)==2 and len(c.app.state.facts)==1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("malformed_proposal", [False, True])
def test_generic_fact_format_failure_during_closure_does_not_loop_or_forge_verdict(http_case, monkeypatch, malformed_proposal):
    import agent_core.intent_execution as execution
    c=http_case
    install(c,steps=100,stop="inconclusive")
    inner,outer=c.models[1].invoke,c.models[0].invoke
    candidates=[];reviews=[]
    def choose(envelope,*args):
        stage=envelope.output_contract["stage"]
        if stage=="fact_proposal":
            return dict(kind="working_record",content="Unreviewed intermediate observation.",evidence_refs=[envelope.request.observation.proof_id])
        if stage=="inner_plan" and c.sends:
            context=json.loads(envelope.request.history_context)
            stop=context["intent_execution"]["stop_requested"]
            if stop["fact_candidate_allowed"]:
                candidates.append(envelope)
                return dict(kind="fact_candidate",content="x"*257 if malformed_proposal else "A scoped response was observed.",reason="Original saved evidence only.",evidence_refs=[context["evidence_sources"][0]["ref"]],inferred=False)
            return dict(kind="intent_resolution",outcome="inconclusive",reason="Legitimate bounded closure with unresolved formal support.",evidence_refs=[i["ref"] for i in context["items"]],fact_ids=[],next_intent_candidates=[])
        return inner(envelope,*args)
    def review(envelope,*args):
        if envelope.output_contract["stage"]=="fact_review":
            reviews.append(envelope)
            return dict(decision="accept",reason="R"*513)
        return outer(envelope,*args)
    c.models[1].invoke,c.models[0].invoke=choose,review
    prepare=execution.prepare_closure
    def boundary(app,intent_id):
        if c.sends and execution.closure_marker(app,intent_id) is None:
            app.handoff.evidence.save("pause",app._id("budget-closure/"+intent_id),{"intent_id":intent_id,"checkpoint_sequence":app.state.latest_checkpoint.sequence})
        return prepare(app,intent_id)
    monkeypatch.setattr(execution,"prepare_closure",boundary)
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="d22_closure_completed_budget_stop",result
    assert len(c.sends)==1 and not c.app.state.facts
    assert len(candidates)==(2 if malformed_proposal else 1) and len(reviews)==(0 if malformed_proposal else 2)
    assert not c.app.store.list_d8_conclusions(c.task)
    before=len(c.app.store.list_d4_operations(c.task))
    c.app.store.close();c.app=c.create()
    assert asyncio.run(c.app.resume(c.task,c.token)).stop_reason==result.stop_reason
    assert len(c.app.store.list_d4_operations(c.task))==before and len(c.sends)==1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_malformed_execution_proposal_still_stops_without_dispatch(http_case):
    c=http_case
    install(c,steps=1)
    calls=[]
    def malformed(envelope,*args):
        calls.append(envelope)
        return dict(kind="http_request",method="GET",path_query=123,headers={},body="")
    c.models[1].invoke=malformed
    result=asyncio.run(c.app.run(c.token))
    assert result.recovery_required and len(calls)==2 and not c.sends
    assert not any(c.app.conclusions.optional_failure(e.request.model_request_id) for e in calls)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("mode", ["once", "twice", "unknown"])
def test_http_predispatch_constraint_feedback_uses_only_the_original_correction(http_case, monkeypatch, mode):
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    from agent_core.execution_feasibility import KEY
    c=http_case
    install(c,steps=1)
    original=c.app.validator._allowed
    checks=[];proposals=[]
    inner=c.models[1].invoke
    def capture(envelope,*args):
        if envelope.output_contract["stage"]=="inner_plan":proposals.append(envelope)
        return inner(envelope,*args)
    c.models[1].invoke=capture
    def guard(*args):
        checks.append(args)
        if mode=="unknown":raise D3RecoveryRequired("d4_operation_outcome_unknown")
        if len(checks)==1 or mode=="twice":raise D3RecoveryRequired("d3_action_rejected_by_review_constraint")
        return original(*args)
    monkeypatch.setattr(c.app.validator,"_allowed",guard)
    result=asyncio.run(c.app.run(c.token))
    if mode=="unknown":
        assert result.recovery_required and result.stop_reason=="d4_operation_outcome_unknown"
        assert len(proposals)==1 and not c.sends
    else:
        corrected=next(e for e in proposals if e.request.correction_of_model_request_id)
        root=corrected.request.correction_of_model_request_id
        assert corrected.correction["reason"]=="d3_action_rejected_by_review_constraint"
        assert corrected.correction[KEY]==json.loads(corrected.request.history_context)[KEY]
        assert sum(e.request.model_request_id==root or e.request.correction_of_model_request_id==root for e in proposals)==2
        if mode=="once":
            assert not result.recovery_required and len(c.sends)==1,result
        else:
            assert result.recovery_required and not c.sends
    before=len(c.app.store.list_d4_operations(c.task)),len(c.sends)
    c.app.store.close();c.app=c.create()
    # Keep the injected unresolved guard active after reopen; a synthetic exception alone is not durable state.
    if mode=="unknown":monkeypatch.setattr(c.app.validator,"_allowed",guard)
    asyncio.run(c.app.resume(c.task,c.token))
    assert (len(c.app.store.list_d4_operations(c.task)),len(c.sends))==before


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_handled_optional_failure_is_not_reported_as_unresolved_format_error(http_case, monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c=http_case
    install(c,steps=2,stop="inconclusive")
    inner=c.models[1].invoke
    def bad(envelope,*args):
        raw=inner(envelope,*args)
        if envelope.output_contract["stage"]=="fact_proposal" and len(c.sends)==1:
            return {**raw,"content":"x"*257}
        return raw
    c.models[1].invoke=bad
    reconcile=c.app.sessions.reconcile
    def lost(*args,**kwargs):
        result=reconcile(*args,**kwargs)
        if any(r.disposition=="technical_failure" for r in c.app.sessions.records()):
            raise ProcessLoss("handled failure recorded before the next model operation")
        return result
    monkeypatch.setattr(c.app.sessions,"reconcile",lost)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    snapshot=snapshot_v3_task_result(c.directory,c.declaration)
    assert not snapshot["errors"] and not snapshot["recovery"]["required"],snapshot
    assert len(snapshot["model_format_diagnostics"])==2
    before=len(c.sends)
    c.app.store.close();c.app=c.create()
    assert asyncio.run(c.app.resume(c.task,c.token)).stop_reason=="search_exhausted"
    assert len(c.sends)==before+1


def test_fact_length_contract_counts_unicode_and_rejects_without_truncation():
    from uuid import uuid4
    from agent_core.domain import MAX_FACT_CONTENT_LENGTH
    from agent_core.conclusion_contracts import D8Candidate,MAX_CONCLUSION_REASON_LENGTH
    from agent_core.evidence_conclusions import EvidenceCandidate
    from agent_core.output_contracts import obj,text,shape_issues
    ref=str(uuid4())
    content="汉"*MAX_FACT_CONTENT_LENGTH
    reason="理"*MAX_CONCLUSION_REASON_LENGTH
    assert len(content.encode("utf-8"))==768
    assert D8Candidate(content,reason,(ref,),None).content==content
    assert EvidenceCandidate(content,reason,(ref,),False).content==content
    schema=obj({"content":text(MAX_FACT_CONTENT_LENGTH),"reason":text(MAX_CONCLUSION_REASON_LENGTH)})
    assert not shape_issues(dict(content=content,reason=reason),schema)
    assert {i.path for i in shape_issues(dict(content=content+"汉",reason=reason+"理"),schema)}=={"$/content","$/reason"}
    with pytest.raises(ValueError):D8Candidate(content+"汉",reason,(ref,),None)
    with pytest.raises(ValueError):EvidenceCandidate(content+"汉",reason,(ref,),False)


@pytest.mark.parametrize("output_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("stage", ["fact_proposal", "fact_review"])
def test_attachment_optional_format_failure_preserves_fixed_observation(output_case, stage):
    c=output_case
    inner=c.models["inner"].invoke
    attempts=[]
    def choose(envelope,*args):
        current=envelope.output_contract["stage"]
        if current=="inner_plan" and c.app.state.plans and c.app.state.plans[0].status.value=="completed":
            c.models["inner"].calls.append(envelope)
            context=json.loads(envelope.request.history_context)
            return dict(kind="intent_resolution",outcome="inconclusive",reason="Legitimate bounded closure after the saved file observation.",evidence_refs=[i["ref"] for i in context["items"]],fact_ids=[],next_intent_candidates=[])
        raw=inner(envelope,*args)
        if current=="fact_proposal" and stage=="fact_proposal":
            attempts.append(envelope)
            return {**raw,"content":"x"*257}
        return raw
    c.models["inner"].invoke=choose
    review=c.models["fact"].invoke
    def malformed_review(envelope,*args):
        raw=review(envelope,*args)
        if stage=="fact_review":
            attempts.append(envelope)
            return {**raw,"reason":"x"*513}
        return raw
    c.models["fact"].invoke=malformed_review
    handoff=c.models["handoff"].invoke
    def end(envelope,*args):
        if envelope.output_contract["stage"]=="intent_evaluation":
            c.models["handoff"].calls.append(envelope)
            return dict(decision="inconclusive",reason="A legitimate scoped closure, not a technical fallback.",evidence_refs=[i.ref for i in envelope.request.history])
        return handoff(envelope,*args)
    c.models["handoff"].invoke=end
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="search_exhausted",result
    assert len(attempts)==2 and len(c.app.state.executions)==2 and not c.app.state.facts
    assert c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id).outcome=="observed"
    assert not c.app.store.list_d8_conclusions(c.task)
    before=len(c.app.store.list_d4_operations(c.task))
    c.app.store.close();c.app=c.create()
    assert asyncio.run(c.app.resume(c.task,c.token)).stop_reason==result.stop_reason
    assert len(c.app.store.list_d4_operations(c.task))==before


def test_nonrolling_plan_rejection_does_not_require_optional_conclusion_services():
    from types import SimpleNamespace
    from agent_adapters.multi_turn_application import D3Application
    from agent_core.dual_llm_modeling import D3ProposalRejected
    error=D3ProposalRejected("known_pre_d8_rejection",model_request_id="known")
    async def rejected(*args,**kwargs):raise error
    app=SimpleNamespace(config=SimpleNamespace(intent_execution_policy=None),
        conclusions=None,inner=SimpleNamespace(propose=rejected))
    with pytest.raises(D3ProposalRejected) as caught:
        asyncio.run(D3Application._propose_inner_plan(app,None,None,refresh=None,validate=None))
    assert caught.value is error


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("stage", ["inner_plan", "fact_proposal"])
@pytest.mark.parametrize("mode", ["success", "usage_missing", "repeated", "invalid_after", "invalid_before", "unknown"])
def test_inner_length_limit_shares_one_correction_and_preserves_progress(http_case, stage, mode):
    c = http_case
    install(c, steps=2)
    original = c.models[1].invoke
    calls = []
    def invoke(envelope, *args):
        target = len(c.sends) == (1 if stage == "inner_plan" else 2)
        if envelope.output_contract["stage"] == stage and target:
            calls.append(envelope)
            attempt = len(calls)
            if attempt == 2 and mode in {"success", "usage_missing"}:return original(envelope, *args)
            if mode == "invalid_before" and attempt == 1 or mode == "invalid_after" and attempt == 2:
                return D8ModelResponse("{}", "stop")
            identity = envelope.request.model_request_id
            c.app.output_boundary.evidence.save("provider-request", identity, {"parameters": {}})
            c.app.output_boundary.evidence.save("provider-result", identity, {"transport": {
                "transport_end": "worker_lost" if mode == "unknown" else "response_received",
                "usage": None if mode == "usage_missing" else {"completion_tokens": 65536, "completion_tokens_details": {"reasoning_tokens": 65536}}}})
            return D8ModelResponse("", "length")
        return original(envelope, *args)
    c.models[1].invoke = invoke
    result = asyncio.run(c.app.run(c.token))
    assert len(calls) == (1 if mode == "unknown" else 2)
    assert len(c.sends) == (2 if stage == "fact_proposal" or mode in {"success", "usage_missing"} else 1)
    assert len(c.app.state.facts) >= 1
    if mode in {"success", "usage_missing"}:
        assert not result.recovery_required, result
        assert calls[1].correction["reason"] == "output_length_limit"
        assert "only regeneration opportunity" in calls[1].instruction
        assert calls[1].request.correction_of_model_request_id == calls[0].request.model_request_id
    else:
        assert result.recovery_required
        if mode in {"repeated", "invalid_before", "unknown"}:assert result.stop_reason == "d8_model_output_truncated"
        assert c.app.state.task_run.status.value == "running"
        assert any(i.status.value == "running" for i in c.app.state.intents)
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot = snapshot_v3_task_result(c.directory, c.declaration)
    assert not snapshot["errors"], snapshot
    if mode not in {"success", "usage_missing"}:assert snapshot["recovery"]["required"]
    before = len(c.app.store.list_d4_operations(c.task)), len(c.sends), len(calls)
    c.app.store.close(); c.app = c.create()
    asyncio.run(c.app.resume(c.task, c.token))
    assert (len(c.app.store.list_d4_operations(c.task)), len(c.sends), len(calls)) == before


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_known_inner_explicit_output_exhaustion_can_use_the_same_correction(http_case):
    c = http_case
    install(c, steps=1)
    invoke = c.models[1].invoke
    calls = []
    def once(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and not c.sends:
            calls.append(envelope)
            if len(calls) == 1:
                identity = envelope.request.model_request_id
                c.app.output_boundary.evidence.save("provider-request", identity, {"parameters": {"max_tokens": 32768}})
                c.app.output_boundary.evidence.save("provider-result", identity, {"transport": {
                    "transport_end": "response_received", "usage": {"completion_tokens": 32768}}})
                return D8ModelResponse("", "length")
        return invoke(envelope, *args)
    c.models[1].invoke = once
    result = asyncio.run(c.app.run(c.token))
    assert not result.recovery_required, result
    assert len(calls) == 2 and len(c.sends) == 1 and len(c.app.state.facts) == 1
    assert calls[1].correction["reason"] == "output_length_limit"


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_inner_length_regeneration_still_obeys_deadline_and_cancel(http_case, stop):
    c = http_case
    install(c, steps=2)
    invoke = c.models[1].invoke
    calls = []
    def ended(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) == 1:
            calls.append(envelope)
            identity = envelope.request.model_request_id
            c.app.output_boundary.evidence.save("provider-request", identity, {"parameters": {}})
            c.app.output_boundary.evidence.save("provider-result", identity, {"transport": {"transport_end": "response_received"}})
            if stop == "deadline":c.clock.now = c.app.state.task_run.deadline_at
            else:c.token.is_cancelled = True
            return D8ModelResponse("", "length")
        return invoke(envelope, *args)
    c.models[1].invoke = ended
    asyncio.run(c.app.run(c.token))
    assert len(calls) == 1 and len(c.sends) == 1 and len(c.app.state.facts) == 1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_cache_order_keeps_policy_priority_evidence_and_correction(http_case):
    from collections import Counter
    from dataclasses import replace
    from agent_adapters.deepseek_roles import build_payload, _shared_schema

    c = http_case
    install(c, steps=2)
    captured = []
    for model in c.models:
        invoke = model.invoke
        def capture(envelope, *args, invoke=invoke):
            captured.append(envelope)
            return invoke(envelope, *args)
        model.invoke = capture
    result = asyncio.run(c.app.run(c.token))
    assert not result.recovery_required, result

    def sections(text):
        values = {}
        for name in ("plan_policy", "review_policy", "fixed_probe_policy", "planning_policy",
                     "progress_policy", "Current branch limits", "output_contract", "correction"):
            marker = " " + name + "="
            if marker in text:
                before, after = text.split(marker, 1)
                value, end = json.JSONDecoder().raw_decode(after)
                values[name] = value
                text = before + after[end:]
        return values, text

    seen = set()
    fact_prefixes = [[], []]
    for envelope in captured:
        stage = envelope.output_contract["stage"]
        role = c.app.config.inner_role if stage in {"inner_plan", "fact_proposal", "inner_candidate"} else c.app.config.outer_role
        original_contract = json.dumps(envelope.output_contract)
        original_request = repr(envelope.request)
        payload = build_payload(envelope, role, context_loader=c.app.web.model_context)
        legacy = build_payload(replace(envelope, output_contract={k: v for k, v in envelope.output_contract.items()
            if k != "input_projection"}), role, context_loader=c.app.web.model_context)
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert {k: v for k, v in payload.items() if k != "messages"} == {k: v for k, v in legacy.items() if k != "messages"}
        values, guidance = sections(payload["messages"][0]["content"])
        old_values, old_guidance = sections(legacy["messages"][0]["content"])
        assert values["output_contract"] == {**envelope.output_contract, "schema": _shared_schema(envelope.output_contract["schema"])}
        assert {k: v for k, v in values.items() if k != "output_contract"} == {k: v for k, v in old_values.items() if k != "output_contract"}
        shared_start = guidance.index(" Shared input references use JSON Pointer:")
        shared_end = guidance.index("never substitute a $ref object.", shared_start) + len("never substitute a $ref object.")
        assert Counter((guidance[:shared_start] + guidance[shared_end:]).split()) == Counter(old_guidance.split())
        assert payload["messages"][0]["content"].index("Shared input references") < payload["messages"][0]["content"].index(" output_contract=")
        assert json.dumps(envelope.output_contract) == original_contract and repr(envelope.request) == original_request

        correction = {"issues": [{"code": "output_truncated"}]}
        corrected = build_payload(replace(envelope, correction=correction), role, context_loader=c.app.web.model_context)
        assert corrected["messages"][1] == payload["messages"][1]
        assert corrected["messages"][0]["content"].split(" correction=", 1)[0] == payload["messages"][0]["content"].split(" correction=", 1)[0]
        assert sections(corrected["messages"][0]["content"])[0]["correction"] == correction
        if stage == "fact_proposal":
            fact_prefixes[0].append(legacy["messages"][0]["content"])
            fact_prefixes[1].append(payload["messages"][0]["content"])
        seen.add(stage)
    assert {"inner_plan", "fact_proposal", "fact_review", "intent_evaluation", "handoff_summary"} <= seen
    from os.path import commonprefix
    assert len(fact_prefixes[0]) >= 2
    assert len(commonprefix(fact_prefixes[1])) > len(commonprefix(fact_prefixes[0]))
