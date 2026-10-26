"""Explicit hypothesis groups; controlled state only, no network/model/Docker."""
import asyncio
import json
from types import SimpleNamespace as NS
import pytest
from agent_core.strategy_supervision import StrategySupervision
from agent_core.sessions import IntentSessions
from agent_core.dual_llm_modeling import parse_d3_plan_proposal
from test_http import http_case
from test_exploration_loop import install


def grouped_case():
    claims, results, native, refs = {}, {}, {}, []
    app=NS(state=NS(executions=()),web=None,
        sessions=NS(execution_refs=lambda intent:list(refs)),
        model_evidence=NS(read=lambda request:native.get(request)),
        mcp=NS(feedback=lambda identity:results.get(identity),evidence=NS(read=lambda kind,identity:claims.get(identity))))
    supervision=StrategySupervision(app)
    calls=[]
    supervision.calls=lambda intent:iter(calls)
    def add(group,*,output="same",tool="shell",truncated=False,status="completed"):
        identity=str(len(refs));refs.append(identity)
        claims[identity]={"model_request_id":identity,"call":{"tool_name":tool,"arguments":{"variant":identity}}}
        native[identity]=NS(outcome="accepted",raw={"exploration_group":group})
        results[identity]={"status":status,"exit_code":0,"stdout":output,"stderr":"", "stdout_truncated":truncated,
                           "command":[tool,identity],"request_id":identity,"finished_at":len(refs)}
        return identity
    return NS(app=app,supervision=supervision,add=add,refs=refs,native=native,results=results,calls=calls)


def test_interleaved_groups_and_result_buckets_do_not_share_counts():
    c=grouped_case()
    for _ in range(4):
        c.add("A");c.add("B",output="different");c.add(None);c.add({"bad":"label"})
    groups=c.supervision.repeated_observations("intent")
    assert [(g["group"],g["count"],g["threshold"]) for g in groups]==[("A",4,4),("B",4,4)]
    for _ in range(3):c.add("A",output="new result")
    assert c.supervision.repeated_observations("intent")==groups
    for _ in range(4):c.add("A",tool="other-tool")
    assert c.supervision.repeated_observations("intent")[0]["count"]==8


def test_doubling_watermark_survives_reconstruction_and_delayed_review():
    c=grouped_case()
    for _ in range(4):c.add("A")
    first=c.supervision.repetition_notices("intent")
    c.calls.append((None,NS(prior_summary=json.dumps({"exploration_progress":{"repeated_observations":first}}))))
    for _ in range(3):c.add("A")
    assert c.supervision.repetition_notices("intent")==[]
    for _ in range(4):c.add("A")
    notice=c.supervision.repetition_notices("intent")
    assert notice[0]["count"]==11 and notice[0]["threshold"]==8
    c.calls.append((None,NS(prior_summary=json.dumps({"exploration_progress":{"repeated_observations":notice}}))))
    for _ in range(5):c.add("A")
    resumed=StrategySupervision(c.app);resumed.calls=c.supervision.calls
    assert resumed.repetition_notices("intent")[0]["threshold"]==16


def test_known_errors_count_but_truncated_or_unaccepted_results_do_not():
    c=grouped_case()
    for _ in range(4):c.add("error",output="missing module",status="failed")
    for _ in range(8):c.add("partial",truncated=True)
    for _ in range(4):
        identity=c.add("unaccepted");c.native[identity].outcome="rejected"
    assert [(g["group"],g["count"]) for g in c.supervision.repeated_observations("intent")]==[("error",4)]


def test_http_cookie_changes_are_distinct_observations():
    c=grouped_case()
    for _ in range(8):c.add("login")
    c.app.state.executions=[NS(execution_id=i,plan_id=i) for i in c.refs]
    c.app.store=NS(load_d3_plan_binding=lambda i:NS(proposal_request_id=i))
    c.app.web=NS(result=lambda i:{"status":200,"body_sha256":"same","body_bytes":2,"cookie":int(i)//4},
                 cookie_snapshot=lambda result:{"session":{"cookie":result["cookie"]},"response":{}})
    groups=c.supervision.repeated_observations("intent")
    assert len(groups)==2 and [g["count"] for g in groups]==[4,4]


def test_metadata_never_changes_mcp_arguments_or_http_binding():
    call={"kind":"mcp_call","tool_name":"shell.run","arguments":{"script":"echo hi"}}
    assert parse_d3_plan_proposal({**call,"exploration_group":"A"})==parse_d3_plan_proposal(call)
    assert parse_d3_plan_proposal({**call,"exploration_group":[]})==parse_d3_plan_proposal(call)


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_http_group_reaches_outer_and_inner_without_stopping_exploration(http_case,monkeypatch):
    c=http_case;install(c,steps=9,stop="inconclusive")
    outer,inner=c.models[0].invoke,c.models[1].invoke
    notices=[];reviews=[]
    def choose(envelope,*args):
        raw=inner(envelope,*args)
        if envelope.output_contract["stage"]=="inner_plan":
            packet=json.loads(envelope.request.history_context)
            notices.extend(packet.get("repetition_advice",[]))
            assert packet["exploration_groups"] is True
            from dataclasses import replace
            from agent_core.output_contracts import output_schema,decode_response
            old=dict(packet)
            old.pop("exploration_groups")
            old_schema=output_schema(replace(envelope.request,history_context=json.dumps(old)),contract_version=22)[1]
            assert all("exploration_group" not in b.get("properties",{}) for b in old_schema["anyOf"])
            if raw.get("kind")=="http_request":
                ignored=decode_response({**raw,"exploration_group":{}},envelope.output_contract["schema"])
                assert not ignored[1] and "exploration_group" not in ignored[0]
                assert decode_response({**raw,"headers":3},envelope.output_contract["schema"])[1]
            if raw.get("kind")=="http_request":
                raw={**raw,"path_query":"/?variant="+str(len(c.sends)),"exploration_group":"test parameter effect"}
        return raw
    def review(envelope,*args):
        if envelope.output_contract["stage"]=="strategy_supervision":
            reviews.extend(json.loads(envelope.request.prior_summary)["exploration_progress"]["repeated_observations"])
        return outer(envelope,*args)
    c.models[1].invoke,c.models[0].invoke=choose,review
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert len(c.sends)==9
    assert [n["threshold"] for n in notices]==[4,8]
    assert any(g["threshold"]==4 for g in reviews)
    assert all("exploration_group" not in request for request in c.sends)
    assert c.app.supervision.repeated_observations(c.app.state.intents[0].intent_id)[0]["count"]==9
    # A known saved HTTP normalization can restore its optional label without replay.
    binding=c.app.store.load_d3_plan_binding(c.app.state.plans[0].plan_id)
    identity=binding.proposal_request_id
    read=c.app.model_evidence.read;expected=read(identity);hidden=[True]
    def interrupted_read(key):
        if key==identity and hidden[0]:hidden[0]=False;return None
        return read(key)
    monkeypatch.setattr(c.app.model_evidence,"read",interrupted_read)
    c.app.web.reconcile()
    assert read(identity)==expected and len(c.sends)==9


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_mcp_labels_stay_outside_tool_arguments_and_reach_inner_at_doubling_points(http_case):
    from agent_adapters.mcp_plan_executor import MCPPlanExecutor
    from test_mcp_link import Tools
    c=http_case;tools=Tools(c.task,c.app.state.task_run.sandbox_id)
    c.app.mcp=MCPPlanExecutor(c.app,tools);c.app.mcp.authorize(c.task)
    inner=c.models[1].invoke;notices=[]
    def choose(envelope,*args):
        if envelope.output_contract["stage"]=="inner_plan":
            packet=json.loads(envelope.request.history_context)
            notices.extend(packet.get("repetition_advice",[]))
            if len(tools.calls)>=9:c.token.is_cancelled=True
            return dict(kind="mcp_call",tool_name="shell.run_bash",
                        arguments={"script":"printf variant-"+str(len(tools.calls))},exploration_group="same hypothesis")
        return inner(envelope,*args)
    c.models[1].invoke=choose
    result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,result
    assert len(tools.calls)==9 and [n["threshold"] for n in notices]==[4,8]
    assert all(set(arguments)=={"script"} for _,arguments in tools.calls)


def test_repetition_checkpoint_can_trigger_between_regular_windows():
    c=grouped_case()
    for _ in range(8):c.add("A")
    reported={**c.supervision.repeated_observations("intent")[0],"threshold":4,"count":4}
    c.calls.append((None,NS(prior_summary=json.dumps({"d8_progress":{"decision_count":0,"execution_refs":c.refs[:-1]},
        "exploration_progress":{"repeated_observations":[reported]}}))))
    sessions=IntentSessions.__new__(IntentSessions)
    sessions.app=NS(config=NS(intent_execution_policy="continuous_v1"),supervision=c.supervision)
    sessions.records=lambda intent:[];sessions.reviews=lambda intent:[];sessions.execution_refs=lambda intent:c.refs
    assert sessions.due("intent")
    c.calls[-1]=(None,NS(prior_summary=json.dumps({"d8_progress":{"decision_count":0,"execution_refs":c.refs},
        "exploration_progress":{"repeated_observations":c.supervision.repeated_observations("intent")}})))
    assert not sessions.due("intent")
