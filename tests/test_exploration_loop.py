"""Current continuous execution with real local ledgers and fake models/transports."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS
from uuid import uuid4
import pytest
from test_http import http_case


def install(case, *, steps=5, decision="accept", stop=None):
    inner, outer = case.models[1].invoke, case.models[0].invoke
    events, candidates = [], []
    case.exploration_envelopes = []
    def choose(envelope, *args):
        stage = envelope.output_contract["stage"]
        events.append(stage)
        case.exploration_envelopes.append(envelope)
        if stage == "fact_proposal":
            pytest.fail("ordinary observations must not force a Fact proposal")
        if stage != "inner_plan":
            return inner(envelope, *args)
        context = json.loads(envelope.request.history_context)
        if len(case.sends) < steps:
            return dict(kind="http_request", method="GET", path_query="/?check="+str(len(case.sends)), headers={}, body="")
        if not candidates and stop is None:
            refs = [s["ref"] for s in context["evidence_sources"]]
            candidates.append(refs)
            return dict(kind="fact_candidate", content="The scoped multi-step observation is supported.",
                        reason="Compare the original requests and saved results.", evidence_refs=refs, inferred=False)
        facts = context["intent_execution"]["facts"]
        return dict(kind="intent_resolution", outcome=stop or ("succeeded" if facts else "inconclusive"),
                    reason="The tested scope is resolved; uncertainty outside it remains.",
                    evidence_refs=[i["ref"] for i in context["items"]], fact_ids=[f["fact_id"] for f in facts], next_intent_candidates=[])
    def review(envelope, *args):
        stage = envelope.output_contract["stage"]
        events.append(stage)
        case.exploration_envelopes.append(envelope)
        if stage == "fact_review":
            return dict(decision=decision, reason="Review the original multi-source evidence within its scope.")
        if stage == "intent_evaluation":
            verdict = stop or ("succeeded" if decision == "accept" else "inconclusive")
            return dict(decision=verdict, reason="Scoped judgment, not global task acceptance.",
                        evidence_refs=[i.ref for i in envelope.request.history])
        if stage == "tree_planning" and case.app.state.turns:
            return dict(kind="exhausted", reason="No further direction in this controlled case.", evidence_refs=[])
        return outer(envelope, *args)
    case.models[1].invoke, case.models[0].invoke = choose, review
    return events, candidates


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("reason", [None, "First test the ready prerequisite; later branches cost more and leave the same uncertainty."])
def test_outer_candidate_order_and_reason_are_traceable_without_extra_call(http_case, reason):
    c = http_case
    install(c, steps=1)
    outer = c.models[0].invoke
    planning = []
    def propose(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and not c.app.state.turns:
            planning.append(envelope.request)
            raw = dict(kind="candidates", intents=[dict(description=name, intent_type=None, dependency_refs=[])
                       for name in ("Check the ready prerequisite", "Explore an alternative", "Test a costlier alternative")])
            if reason is not None:raw["reason"] = reason
            return raw
        return outer(envelope, *args)
    c.models[0].invoke = propose
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(planning) == 1 and [i.priority for i in c.app.state.intents] == [1, 2, 3]
    assert c.app.state.intents[0].status.value == "succeeded"
    selection = next(r for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "selection")
    note = c.app.handoff.evidence.read("decision", selection.record_id)
    assert note["priority"] == 1 and note["model_request_id"] == planning[0].model_request_id
    assert note["reason_status"] == ("provided" if reason else "not_provided")
    assert c.app.handoff.evidence.read("result", note["model_request_id"])["raw"].get("reason") == reason
    assert "alternative" not in json.dumps(note)  # No sibling descriptions injected into the inner scope.


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("reason", ["", "   ", None, 7])
def test_order_reason_schema_and_parser_reject_invalid_values(http_case, reason):
    from agent_core.output_contracts import output_schema, decode_response
    c = http_case
    request, _ = c.app.handoff._request("tree_planning", None)
    raw = dict(kind="candidates", intents=[dict(description="Observe the declared scope", intent_type=None, dependency_refs=[])], reason=reason)
    assert decode_response(raw, output_schema(request, contract_version=22)[1])[1]
    with pytest.raises(ValueError):c.app.handoff._parse(request, raw)


@pytest.mark.parametrize("ordered", [False, True])
def test_attachment_order_reason_preserves_frozen_old_shape(ordered):
    from agent_core.planning_assertion import planning_schema, materialize
    from agent_core.output_contracts import decode_response
    from types import SimpleNamespace
    packet = {"s2_planning_policy": {"rule_id": "attachment_copy_sha256", "rule_version": 1,
        "attachment_indices": [0], "authorization_sha256": "a" * 64, "imports": [{"attachment_index": 0}]},
        "intent_planning": {"version": 1}}
    if ordered:packet["intent_planning"]["order"] = "execution_priority"
    request = SimpleNamespace(stage="tree_planning", prior_summary=json.dumps(packet), goal="Observe the attachment")
    raw = dict(kind="candidates", completion_rule={"rule_id": "attachment_copy_sha256", "parameters": {"attachment_index": 0}},
        intents=[dict(description="Observe declared attachment scope", intent_type=None, dependency_refs=[])], reason="Resolve this prerequisite first.")
    issues = decode_response(raw, planning_schema(request))[1]
    assert bool(issues) == (not ordered)
    if ordered:
        canonical = materialize(request, raw)
        assert canonical["reason"] == raw["reason"] and canonical["turn_target"] == request.goal


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_tools_return_directly_then_one_multi_source_fact_and_original_candidate_gate(http_case):
    c = http_case
    events, candidates = install(c)
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 5 and len(candidates) == 1 and len(candidates[0]) == 5
    assert len(c.app.state.facts) == 1
    assert {c.app.store.load_d3_plan_result(p.plan_id).outcome for p in c.app.state.plans} == {"observed"}
    assert events.count("strategy_supervision") == 1
    assert "fact_proposal" not in events and "handoff_summary" not in events
    assert events.index("inner_candidate") > events.index("intent_evaluation")
    assert len(c.app.state.turns) == 1 and c.app.state.task_run.status.value == "awaiting_external_feedback"
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    review = next(r for r in c.models[0].calls if r.output_contract["stage"] == "strategy_supervision")
    marker = json.loads(review.request.prior_summary)["d8_progress"]
    assert len(marker["execution_refs"]) == 4 and marker["decision_count"] == 4
    assert set(marker["execution_refs"]) <= {i.record_id for i in review.request.history}
    from agent_adapters.deepseek_roles import build_payload
    projection = json.loads(build_payload(review,c.app.config.outer_role,context_loader=c.app.web.model_context)["messages"][-1]["content"])["protected_source_projection"]
    assert len(projection["http_observations"]) == 4
    assert all("request" in o and "body_text" in o for o in projection["http_observations"])
    # Later tool/Facts cannot change the already-applied frozen progress window.
    assert c.app.supervision.watermark(c.app.state.intents[0].intent_id)[0]["decision_count"] == 4
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot = snapshot_v3_task_result(c.directory, c.declaration)
    assert not snapshot["errors"] and len(snapshot["verified_facts"]) == 1, snapshot


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("decision", ["reject", "insufficient"])
def test_rejected_explicit_fact_keeps_observed_progress_and_allows_legitimate_closure(http_case, decision):
    c = http_case
    events, _ = install(c, steps=2, decision=decision)
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert not c.app.state.facts and len(c.sends) == 2
    assert c.app.state.intents[0].status.value == "inconclusive"
    assert events.count("fact_review") == 1 and "progress_review" not in events
    assert all(c.app.store.load_d3_plan_result(p.plan_id).outcome == "observed" for p in c.app.state.plans)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("after_commit", [False, True])
def test_observation_commit_recovers_without_http_or_model_replay(http_case, monkeypatch, after_commit):
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    events, _ = install(c, steps=2)
    complete = c.app.control.complete_plan
    def lost(*args, **kwargs):
        if after_commit:
            complete(*args, **kwargs)
        raise ProcessLoss("known observation at commit boundary")
    monkeypatch.setattr(c.app.control, "complete_plan", lost)
    with pytest.raises(ProcessLoss):
        asyncio.run(c.app._run_once(c.token))
    assert len(c.sends) == 1
    c.app.store.close()
    c.app = c.create()
    async def finish(token): c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish
    asyncio.run(c.app.resume(c.task, c.token))
    assert len(c.sends) == 2 and events.count("fact_proposal") == 0
    assert len(c.app.state.facts) == 1


def manager(*, refs=(), previous=(), kinds=()):
    from agent_core.sessions import IntentSessions
    rows = [NS(ordinal=i+1, data={"request":{"stage":"inner_plan"},"response":{"kind":k}}) for i,k in enumerate(kinds)]
    m = IntentSessions(NS(config=NS(intent_execution_policy="continuous_v1"), store=NS()), NS())
    m.app.supervision.watermark = lambda intent: []
    m.records = lambda intent: rows
    m.execution_refs = lambda intent: list(refs)
    m.payload = lambda row: row.data
    marker = {"execution_refs":list(previous)}
    m.reviews = lambda intent: [(0,None,{"request":{"context_summary":json.dumps({"d8_progress":marker})}})] if previous else []
    return m


def test_periodic_counts_actual_executions_and_includes_repeated_or_failed_tools():
    assert not manager(refs=("one","two"), kinds=["fact_candidate"]*10).due("intent")
    assert manager(refs=("a","b","c","d"), kinds=["http_request"]*4).due("intent")
    assert not manager(refs=("a","b","c","d","e"), previous=("a","b","c","d")).due("intent")
    assert manager(kinds=["working_record","history_query","history_details","working_record"]).due("intent")
    assert not manager(kinds=["working_record"]*3+["http_request","working_record"]).due("intent")


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_new_input_is_stable_and_old_frozen_projection_is_preserved(http_case, monkeypatch):
    from agent_adapters.deepseek_roles import build_payload
    c = http_case
    events, _ = install(c, steps=2)
    assert asyncio.run(c.app._run_once(c.token)).stop_reason == "d9_awaiting_external_feedback"
    env = next(e for e in c.exploration_envelopes if e.output_contract["stage"] == "inner_plan")
    request = env.request
    env = c.app.output_boundary.envelope(request)
    assert env.output_contract["input_projection"] == "append_segments"
    payload = build_payload(env,c.app.config.inner_role,context_loader=c.app.web.model_context)
    assert "messages" in json.loads(payload["messages"][1]["content"])
    assert "falsifiable hypothesis" in payload["messages"][2]["content"]
    assert "without a value or execution_id" not in payload["messages"][0]["content"]

    second = [e for e in c.exploration_envelopes if e.output_contract["stage"] == "inner_plan"][1]
    other = build_payload(second,c.app.config.inner_role,context_loader=c.app.web.model_context)
    from os.path import commonprefix
    prefix = commonprefix([payload["messages"][0]["content"], other["messages"][0]["content"]])
    assert payload["messages"][0] == other["messages"][0]
    assert "Applicable stage rules" in prefix
    legacy = {**env.output_contract, "input_projection":"shared_refs_v1"}
    old = replace(env, output_contract=legacy)
    previous = build_payload(old,c.app.config.inner_role,context_loader=c.app.web.model_context)
    assert next(iter(json.loads(previous["messages"][1]["content"]))) != "session_context"
    boundary = c.app.output_boundary
    read = boundary.evidence.read
    record = read("contract",request.model_request_id)
    monkeypatch.setattr(boundary.evidence,"read",lambda kind,key:
        {**record,"output_contract":legacy} if kind=="contract" and key==request.model_request_id else read(kind,key))
    assert boundary._contract(request)[0].output_contract == legacy
    correction = replace(request,model_request_id=str(uuid4()),correction_of_model_request_id=request.model_request_id)
    assert boundary._contract(correction)[0].output_contract["input_projection"] == "shared_refs_v1"


def test_execution_window_counts_known_tools_across_http_attachment_and_mcp_once():
    from agent_core.sessions import IntentSessions
    def op(identity, resource, completion=True, category="tool_dispatch"):
        return NS(start=NS(operation_id=identity,resource_ref=resource,category=category),completion=NS() if completion else None)
    operations = [op("http","http-plan"),op("file","file-plan"),op("mcp","mcp/i/request"),
                  op("unknown","mcp/i/unknown",False),op("other","mcp/other/request"),op("model","i",category="action")]
    state=NS(task_run=NS(task_run_id="task"),plans=[NS(plan_id="p",intent_id="i")],
             executions=[NS(execution_id="http",plan_id="p",status=NS(value="succeeded")),
                         NS(execution_id="file",plan_id="p",status=NS(value="failed"))])
    app=NS(state=state,store=NS(list_d4_operations=lambda task:operations))
    assert IntentSessions(app,NS()).execution_refs("i") == ["http","file","mcp"]


def test_phase_timing_preserves_error_and_never_logs_exception_text(caplog):
    from qinyuan_core.logging import timed_phase
    with caplog.at_level("INFO",logger="qinyuan.store"):
        with pytest.raises(ValueError,match="private detail"):
            with timed_phase("store","state_read",run="r",request="q"):
                raise ValueError("private detail")
    record=next(r.qy for r in caplog.records if getattr(r,"qy",{}).get("event")=="phase.finished")
    assert record["outcome"]=="failed" and record["error_type"]=="ValueError"
    assert record["seconds"]>=0 and record["request"]=="q"
    assert "private detail" not in json.dumps(record)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_four_intermediate_records_trigger_review_without_automatic_fact_or_summary(http_case):
    c=http_case
    events,_=install(c,steps=1)
    original=c.models[1].invoke
    notes=[]
    def choose(envelope,*args):
        if envelope.output_contract["stage"]=="inner_plan" and c.sends and len(notes)<4:
            context=json.loads(envelope.request.history_context)
            notes.append(envelope.request.model_request_id)
            return dict(kind="working_record",content="Hypothesis needs one discriminating check; not verified.",
                        evidence_refs=[next(i["ref"] for i in context["items"] if i["record_id"]==c.app.state.executions[0].execution_id)])
        return original(envelope,*args)
    c.models[1].invoke=choose
    result=asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason=="d9_awaiting_external_feedback",result
    assert len(notes)==4 and len(c.sends)==1 and len(c.app.state.facts)==1
    assert events.count("strategy_supervision")==1 and "handoff_summary" not in events
    intent=c.app.state.intents[0].intent_id
    marker=c.app.supervision.watermark(intent)[0]
    assert len(marker["execution_refs"])==1


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("last", ["http", "mcp"])
def test_mixed_mcp_and_http_results_share_periodic_review_and_one_formal_fact(http_case, last):
    from test_mcp_link import Tools
    from agent_adapters.mcp_plan_executor import MCPPlanExecutor
    c=http_case
    tools=Tools(c.task,c.app.state.task_run.sandbox_id)
    c.app.mcp=MCPPlanExecutor(c.app,tools)
    c.app.mcp.authorize(c.task)
    events,candidates=install(c,steps=2)
    original=c.models[1].invoke
    def choose(envelope,*args):
        if (envelope.output_contract["stage"]=="inner_plan" and len(tools.calls)<2
                and (last=="http" or len(c.sends)==2)):
            return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
        if envelope.output_contract["stage"]=="inner_candidate" and last=="mcp":
            return dict(action=dict(kind="candidate_flag",value="sample-answer",source_ref=envelope.request.context.source_ref))
        return original(envelope,*args)
    c.models[1].invoke=choose
    result=asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason=="d9_awaiting_external_feedback",result
    candidate,=c.app.store.list_d7_answer_candidates(c.task)
    assert candidate.source_kind == ("mcp" if last=="mcp" else "d3")
    assert len(tools.calls)==2 and len(c.sends)==2
    assert len(candidates[0])==4 and len(c.app.state.facts)==1
    assert events.count("strategy_supervision")==1 and "fact_proposal" not in events
    review=next(e for e in c.exploration_envelopes if e.output_contract["stage"]=="strategy_supervision")
    # The observations remain exact; only their duplicated packet section was removed.
    observations=[r["data"]["observation"] for r in json.loads(review.request.prior_summary)["d8_progress"]["recent"]]
    assert sum("mcp_tool_result" in item for item in observations)==2


from test_output import output_case

@pytest.mark.parametrize("output_case", ["continuous"], indirect=True)
def test_attachment_tools_return_observed_and_review_counts_executions_not_model_decisions(output_case):
    c=output_case
    original=c.models["inner"].invoke
    def choose(envelope,*args):
        stage=envelope.output_contract["stage"]
        assert stage!="fact_proposal"
        if stage=="inner_plan":
            context=json.loads(envelope.request.history_context)
            if len(c.app.state.plans)<3:
                from agent_core.plan_policy import policy_for
                return policy_for(envelope.request)["allowed_choices"][0]
            return dict(kind="intent_resolution",outcome="inconclusive",
                reason="Close this finite exploratory scope; final task remains unresolved.",
                evidence_refs=[i["ref"] for i in context["items"]],fact_ids=[],next_intent_candidates=[])
        return original(envelope,*args)
    c.models["inner"].invoke=choose
    def organize(envelope,raw):
        if envelope.output_contract["stage"]=="intent_evaluation":
            return dict(decision="inconclusive",reason="Legitimate scoped strategy closure.",
                        evidence_refs=[i.ref for i in envelope.request.history])
        if envelope.output_contract["stage"]=="tree_planning" and c.app.state.turns:
            return dict(kind="exhausted",reason="No additional controlled direction.",evidence_refs=[])
        return raw
    handoff=c.models["handoff"].invoke
    def review(envelope,*args):
        if envelope.output_contract["stage"]=="intent_evaluation":
            return organize(envelope,{})
        return organize(envelope,handoff(envelope,*args))
    c.models["handoff"].invoke=review
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert len(c.app.state.executions)==4 and not c.app.state.facts
    assert all(c.app.store.load_d3_plan_result(p.plan_id).outcome=="observed" for p in c.app.state.plans)
    checks=c.app.supervision.watermark(c.app.state.intents[0].intent_id)
    assert len(checks)==1
    marker=checks[0]
    assert len(marker["execution_refs"])==4 and marker["decision_count"]==7
