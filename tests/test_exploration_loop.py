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
    assert manager(refs=("one","two"), kinds=["fact_candidate"]*10).due("intent")
    assert manager(refs=("a","b","c","d"), kinds=["http_request"]*4).due("intent")
    assert not manager(refs=("a","b","c","d","e"), previous=("a","b","c","d")).due("intent")
    assert manager(kinds=["working_record","history_query","history_details","working_record"]).due("intent")
    assert not manager(kinds=["working_record"]*3+["http_request","working_record"]).due("intent")
    assert not manager(kinds=["working_record"]*3+["http_request_saved","working_record"]).due("intent")


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


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_phase_reminder_is_optional_once_per_tool_window_and_frozen(http_case, monkeypatch):
    c = http_case
    events, _ = install(c, steps=9)
    choose = c.models[1].invoke
    def bounded_candidate(envelope, *args):
        raw = choose(envelope, *args)
        if isinstance(raw, dict) and raw.get("kind") == "fact_candidate":
            raw["evidence_refs"] = raw["evidence_refs"][-8:]
        return raw
    c.models[1].invoke = bounded_candidate
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    decisions = [e for e in c.exploration_envelopes if e.output_contract["stage"] == "inner_plan"]
    reminders = [e for e in decisions if "knowledge_checkpoint" in json.loads(e.request.history_context)]
    assert [len(json.loads(e.request.history_context)["knowledge_checkpoint"]["execution_refs"]) for e in reminders] == [4, 8]
    # Both reminders were answered with another tool, without any forced review or note.
    assert len(c.sends) == 9 and events.count("fact_review") == 1
    assert events.count("inner_plan") == 11
    saved = reminders[0].request
    monkeypatch.setattr(c.app.sessions, "consolidation_context", lambda _: pytest.fail("frozen request must not be recomputed"))
    from dataclasses import fields
    from agent_core.dual_llm_modeling import D3ModelRequest
    base = D3ModelRequest(**{f.name: getattr(saved, f.name) for f in fields(D3ModelRequest)})
    assert c.app.handoff.wrap_execution_request(base).history_context == saved.history_context


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_two_phase_facts_keep_same_intent_until_explicit_reviewed_end(http_case):
    c = http_case
    install(c, steps=4)
    inner, outer = c.models[1].invoke, c.models[0].invoke
    phases, end_rules = [], []
    def choose(envelope, *args):
        if envelope.output_contract["stage"] != "inner_plan":return inner(envelope, *args)
        ctx = json.loads(envelope.request.history_context)
        if len(c.sends) in {2, 4} and len(c.app.state.facts) < len(c.sends) // 2:
            assert c.app.state.intents[0].status.value == "running"
            phases.append(len(c.sends))
            return dict(kind="fact_candidate", content="Scoped phase " + str(len(c.sends)) + " is observed.",
                        reason="Distinct saved comparison for this phase.",
                        evidence_refs=[s["ref"] for s in ctx["evidence_sources"]][-2:], inferred=False)
        if len(c.sends) < 4:
            return dict(kind="http_request", method="GET", path_query="/?phase="+str(len(c.sends)), headers={}, body="")
        return dict(kind="intent_resolution", outcome="succeeded", reason="The declared local deliverable is supported.",
                    evidence_refs=[i["ref"] for i in ctx["items"]], fact_ids=[f["fact_id"] for f in ctx["intent_execution"]["facts"]], next_intent_candidates=[])
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":
            from agent_adapters.deepseek_roles import build_payload
            policy = json.loads(envelope.request.prior_summary)["s4_evaluation_policy"]
            end_rules.append(policy["goal_interpretation"])
            payload = build_payload(envelope, c.app.config.outer_role, context_loader=c.app.web.model_context)
            assert "do not add a requirement to execute that later request" in payload["messages"][-2]["content"]
        return outer(envelope, *args)
    c.models[1].invoke, c.models[0].invoke = choose, review
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert phases == [2, 4] and len(c.app.state.facts) == 2 and len(c.sends) == 4
    assert len(c.app.state.turns) == 1 and end_rules
    # The claim/evidence and independent task-verification gates are unchanged.
    assert c.app.state.task_run.status.value == "awaiting_external_feedback"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_known_technical_handoff_keeps_sources_visible_without_promoting_facts(http_case, monkeypatch):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    c = http_case
    install(c, steps=1, stop="inconclusive")
    inner, outer = c.models[1].invoke, c.models[0].invoke
    seen = []
    def plan(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning":
            return (dict(kind="exhausted", reason="No new direction.", evidence_refs=[]) if c.app.state.turns else
                    dict(kind="candidates", intents=[dict(description=name, intent_type=None, dependency_refs=[])
                         for name in ("Source route", "Sibling route")]))
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] != "inner_plan":return inner(envelope, *args)
        actor = next(i for i in c.app.state.intents if i.intent_id == envelope.request.context.intent_id)
        if actor.description == "Source route":
            if not c.sends:
                return dict(kind="http_request", method="GET", path_query="/?known=1", headers={}, body="")
            c.app.output_boundary.evidence.save("provider-result", envelope.request.model_request_id,
                {"transport": {"transport_end": "timeout", "worker_exit_confirmed": True}})
            return ModelError(ModelErrorKind.TIMED_OUT, "model_total_timeout")
        ctx = json.loads(envelope.request.history_context)
        notes = [i for i in ctx["items"] if i["identity"] == "unreviewed_intent_handoff_not_fact"]
        assert notes and notes[0]["ref"] in ctx["required_refs"]
        note = json.loads(notes[0]["content"])
        assert note["progress"]["decision_refs"] and note["progress"]["evidence_sources"]
        assert not ctx["intent_execution"]["facts"]
        assert not any(i["record_type"] == "intent" and i["record_id"] != actor.intent_id for i in ctx["items"])
        seen.append(note)
        return inner(envelope, *args)
    c.models[1].invoke, c.models[0].invoke = choose, plan
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert seen and len(c.sends) == 1 and not c.app.state.facts
    progress = seen[0]["progress"]
    owner = c.app.state.intents[0].intent_id
    c.app.sessions.validate_handoff_progress(owner, progress)
    source = c.app.evidence_conclusions.source
    monkeypatch.setattr(c.app.evidence_conclusions, "source", lambda *args: {**source(*args), "version": "0" * 64})
    with pytest.raises(D3RecoveryRequired, match="d22_handoff_progress_source_changed"):
        c.app.sessions.validate_handoff_progress(owner, progress)


def test_handoff_recent_sources_follow_tool_order_across_mcp_and_http():
    m = manager(refs=("old-http", "mcp1", "mcp2", "mcp3", "mcp4"))
    m.records = lambda _: ()
    # The catalogue groups MCP before HTTP; its tail is not chronological.
    catalogue = [dict(ref=ref, kind="mcp", version="v") for ref in ("mcp1", "mcp2", "mcp3", "mcp4")]
    catalogue.append(dict(ref="old-proof", kind="http", version="v"))
    m.app.evidence_conclusions = NS(catalog=lambda _: catalogue,
        source=lambda ref, _: {"source_ref": "old-http" if ref == "old-proof" else ref})
    progress = m.handoff_progress("intent")
    assert [s["ref"] for s in progress["evidence_sources"]] == ["mcp1", "mcp2", "mcp3", "mcp4"]


@pytest.mark.parametrize('kind',['intent_resolution','fact_candidate','strategy_yield','history_next_page'])
def test_all_internal_decisions_count_toward_the_existing_supervision_window(kind):
    from agent_core.sessions import IntentSessions
    rows=[NS(ordinal=n,record_id=str(n)) for n in range(4)]
    sessions=object.__new__(IntentSessions)
    sessions.app=NS(config=NS(intent_execution_policy='continuous_v1'),supervision=NS(watermark=lambda _:[]))
    sessions.reviews=lambda _:[]
    sessions.records=lambda _:rows
    sessions.execution_refs=lambda _:[]
    sessions.payload=lambda _:dict(request=dict(stage='inner_plan'),response=dict(kind=kind))
    assert sessions.due('actor')
    rows.pop()
    assert not sessions.due('actor')


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_early_reading_index_and_continue_feedback_survive_recent_window(http_case):
    c=http_case
    install(c,steps=1,stop='inconclusive')
    inner,outer=c.models[1].invoke,c.models[0].invoke
    state={'notes':0,'reads':0,'resolutions':0,'review':0}
    seen=[]
    def choose(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan' and c.sends:
            h=json.loads(envelope.request.history_context)
            ref=next(i['ref'] for i in h['items'] if i['record_id']==c.app.state.executions[0].execution_id)
            if state['notes']==0:
                state['notes']+=1
                return dict(kind='working_record',content='Read source/view lines 1-3; complete excerpt, runtime effect remains unverified.',evidence_refs=[ref])
            if state['reads']<6:
                state['reads']+=1
                return dict(kind='history_query',query={})
            state['resolutions']+=1
            if state['resolutions']==1:
                return dict(kind='intent_resolution',outcome='review',reason='Review the bounded local deliverable.',
                    evidence_refs=[ref],fact_ids=[],next_intent_candidates=[])
            seen.append(h)
        return inner(envelope,*args)
    def review(envelope,*args):
        if envelope.output_contract['stage'] in {'strategy_supervision','intent_evaluation'}:
            req=envelope.request
            if envelope.output_contract['stage']=='intent_evaluation':
                state['review']+=1
                if state['review']==1:
                    return dict(decision='continue',reason='Check the remaining local assumption against the original source.',evidence_refs=[i.ref for i in req.history])
            if state['reads']>=6:
                assert any(i.identity=='unreviewed_working_record' and 'Read source/view' in i.content for i in req.history)
        return outer(envelope,*args)
    c.models[1].invoke,c.models[0].invoke=choose,review
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert seen and any('reviewed_summary' in i['content'] for i in seen[-1]['items'])
    assert not c.app.state.facts
    assert c.app.state.intents[0].status.value=='inconclusive'



@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_observed_fact_and_conditional_inference_keep_separate_review_identity(http_case):
    c=http_case
    install(c,steps=1,stop='inconclusive')
    inner=c.models[1].invoke
    candidates=[]
    def choose(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan' and c.sends and len(candidates)<2:
            h=json.loads(envelope.request.history_context)
            assert 'inferred=false' in h['intent_execution']['evidence_rule']
            inferred=not candidates
            candidates.append(inferred)
            return dict(kind='fact_candidate',inferred=inferred,evidence_refs=[h['evidence_sources'][0]['ref']],
                content='A later variant may change this response; that causal hypothesis is untested.' if inferred else
                        'The original scoped request returned status 200 and the recorded body.',
                reason='Keep the causal hypothesis separate from the directly recorded observation.')
        return inner(envelope,*args)
    outer=c.models[0].invoke
    def review(envelope,*args):
        if envelope.output_contract['stage']=='fact_review':
            from agent_adapters.deepseek_roles import build_payload
            payload=build_payload(envelope,c.app.config.outer_role,context_loader=c.app.web.model_context)
            guidance=' '.join(m['content'] for m in payload['messages'])
            expected='Acceptance records a reviewed inference, not a Fact.' if envelope.request.candidate.inferred else 'Acceptance may create a formal Fact only for this narrow directly supported observation'
            assert expected in guidance
            assert 'Accept creates a distinct Fact.' not in guidance
        return outer(envelope,*args)
    c.models[1].invoke,c.models[0].invoke=choose,review
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    conclusions=c.app.store.list_d8_conclusions(c.task)
    assert [r.outcome for r in conclusions]==['reviewed_inference','verified_fact']
    assert conclusions[0].fact_id is None and len(c.app.state.facts)==1
    assert c.app.state.intents[0].status.value=='inconclusive'
    assert len(c.sends)==1


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_old_continuation_dependency_carries_later_completion_without_reopening(http_case, monkeypatch):
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    from agent_core.handoff_contracts import D8Capacity
    c = http_case
    install(c, steps=1)
    inner, outer = c.models[1].invoke, c.models[0].invoke
    reviews, seen = [], {}
    class ReachedNextDirection(BaseException):
        pass
    async def no_final_candidate(*args):
        return False
    monkeypatch.setattr(c.app.answers, "after_success", no_final_candidate)
    def review(envelope, *args):
        stage, req = envelope.output_contract["stage"], envelope.request
        if stage == "intent_evaluation":
            reviews.append(req)
            if len(reviews) == 1:
                return dict(decision="continue", reason="Check the remaining local condition before closing this phase.",
                            evidence_refs=[i.ref for i in req.history])
            earlier = next(i for i in req.history if i.record_id.startswith("tree/")
                           and json.loads(i.content).get("kind") == "evaluation_continued")
            seen["before"] = earlier
        if stage == "tree_planning" and c.app.state.turns:
            packet = json.loads(req.prior_summary)
            earlier = seen["before"]
            later = packet["later_intent_results"][earlier.ref]
            assert {earlier.ref, later} <= {i.ref for i in req.history}
            seen["later"] = later
            return dict(kind="candidates", reason="Test a distinct runtime prerequisite, not an old reviewer obligation.",
                intents=[dict(description="Test the next runtime prerequisite using preserved source results.",
                              intent_type=None, dependency_refs=[earlier.ref])])
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.app.state.intents) > 1:
            packet = json.loads(envelope.request.history_context)
            old = seen["before"]
            assert packet["later_intent_results"][old.ref] == seen["later"]
            items = {i["ref"]: i for i in packet["items"]}
            assert old.ref in packet["required_refs"] and seen["later"] in packet["required_refs"]
            assert items[old.ref]["content"] == old.content  # No historical opinion rewritten.
            assert any(i["record_type"] == "intent" and i["record_id"] == old.intent_id
                       and json.loads(i["content"])["status"] == "succeeded" for i in items.values())
            from agent_adapters.deepseek_roles import build_payload
            wire = build_payload(envelope, c.app.config.inner_role, context_loader=c.app.web.model_context)
            assert "An earlier request for more evidence is not automatically an outstanding obligation" in wire["messages"][-2]["content"]
            assert "not a prohibition" in wire["messages"][-2]["content"]
            seen["envelope"] = envelope
            seen["new_intent"] = envelope.request.context.intent_id
            raise ReachedNextDirection()
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    # Model workers reconcile interruptions as unknown rather than replaying them.
    assert "new_intent" in seen and result.recovery_required, result
    assert len(reviews) == 2 and len(c.sends) == 1 and len(c.app.state.facts) == 1
    from agent_adapters.deepseek_roles import build_payload
    envelope = seen["envelope"]
    packet = json.loads(envelope.request.history_context)
    old_packet = {key: value for key, value in packet.items() if key != "later_intent_results"}
    old_request = replace(envelope.request, history_context=json.dumps(old_packet))
    old_wire = build_payload(replace(envelope, request=old_request), c.app.config.inner_role,
                             context_loader=c.app.web.model_context)
    assert "later_intent_results links an earlier" not in old_wire["messages"][-2]["content"]
    assert c.app.state.intents[0].status.value == "succeeded"
    turn = next(i.turn_id for i in c.app.state.intents if i.intent_id == seen["new_intent"])
    original_history = c.app.handoff.history
    monkeypatch.setattr(c.app.handoff, "history", lambda **kwargs: tuple(
        replace(i, source_status="unavailable") if i.ref == seen["later"] else i for i in original_history(**kwargs)))
    with pytest.raises(D3RecoveryRequired, match="d8_required_source_unavailable_or_changed"):
        c.app.handoff.select("execution", turn, seen["new_intent"])
    monkeypatch.setattr(c.app.handoff, "history", original_history)
    monkeypatch.setattr(c.app.handoff, "capacity", lambda: NS(capacity=D8Capacity(initial_history=8192, visible_history=16384)))
    with pytest.raises(D3RecoveryRequired, match="d8_context_paused"):
        c.app.handoff.select("execution", turn, seen["new_intent"])


@pytest.mark.parametrize("status,later_owner,sequence,kind,expected", [
    ("succeeded", "one", 3, "succeeded", True),
    ("failed", "one", 3, "failed", True),
    ("inconclusive", "one", 3, "inconclusive", True),
    ("running", "one", 3, "succeeded", False),
    ("succeeded", "two", 3, "succeeded", False),
    ("succeeded", "one", 1, "succeeded", False),
    ("succeeded", "one", 3, "suspended", False),
])
def test_review_chronology_requires_same_intent_and_committed_terminal(status, later_owner, sequence, kind, expected):
    from agent_core.handoff import HandoffContext
    rows = [NS(record_id="old", intent_id="one", kind="evaluation_continued"),
            NS(record_id="later", intent_id=later_owner, kind=kind)]
    fake = NS(state=NS(task_run=NS(task_run_id="task"), intents=[NS(intent_id="one", status=NS(value=status))]),
              store=NS(list_d8_tree_records=lambda _: rows))
    items = [NS(record_id="tree/old", ref="old-ref", committed_sequence=2),
             NS(record_id="tree/later", ref="later-ref", committed_sequence=sequence)]
    assert HandoffContext._later_intent_results(fake, items) == ({"old-ref": "later-ref"} if expected else {})


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("source_failure", [False, True])
def test_evidence_fact_history_index_and_details_continue_same_intent(http_case, monkeypatch, caplog, source_failure):
    c = http_case
    install(c, steps=1)
    inner = c.models[1].invoke
    reads = []
    validate = c.app.handoff._validate_frozen_read_sources
    failures = []
    def transient_source_failure(items):
        if source_failure and not failures:
            from agent_core.dual_llm_contracts import D3RecoveryRequired
            failures.append("d8_read_source_version_changed")
            raise D3RecoveryRequired(failures[-1])
        return validate(items)
    monkeypatch.setattr(c.app.handoff, "_validate_frozen_read_sources", transient_source_failure)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and c.app.state.facts:
            packet = json.loads(envelope.request.history_context)
            if not reads:
                reads.append("index")
                return dict(kind="history_query", query={"intent_id": c.app.state.intents[0].intent_id})
            if len(reads) == 1:
                reads.append("details")
                ref = next(i["ref"] for i in packet["items"] if i["record_type"] == "verified_fact")
                return dict(kind="history_details", refs=[ref])
        return inner(envelope, *args)
    c.models[1].invoke = choose
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert reads == ["index", "details"] and len(c.sends) == 1
    fact, = c.app.state.facts
    assert fact.verification_ref.startswith("d8e:") and len(c.app.state.intents) == 1
    saved = c.app.store.list_d8_reads(c.task)
    assert len(saved) == 2 and all(completion is not None for _, _, completion in saved)
    responses = [c.app.handoff.evidence.read("read-result", identity) for identity, _, _ in saved]
    snapshot = c.app.handoff.evidence.read("read-snapshot", saved[0][1]["snapshot_id"])
    assert any(i["record_type"] == "progress" and i["record_id"].startswith("review/") for i in snapshot["items"])
    if source_failure:
        assert responses[0]["error"] == "source_unavailable" and responses[0]["items"] == []
        assert responses[0]["code"] == "d8_read_source_version_changed"
        assert "history.read_failed" in caplog.text
    else:
        assert all("error" not in response for response in responses)
    assert any(i.get("content") == fact.content for response in responses for i in response["items"])
    assert all(c.app.handoff.resume_read(identity) == response for (identity, _, _), response in zip(saved, responses))
