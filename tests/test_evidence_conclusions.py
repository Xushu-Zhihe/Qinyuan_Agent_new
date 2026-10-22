"""Local MCP evidence -> outer review -> durable conclusion, without synthetic Plans."""
import asyncio
import json
import pytest
from test_mcp_link import linked
from test_output import output_case

@pytest.mark.parametrize("output_case", [22], indirect=True)
@pytest.mark.parametrize("decision,inferred,expected", [("accept",False,"verified_fact"),("accept",True,"reviewed_inference"),("reject",False,"rejected"),("insufficient",False,"insufficient"),("no_new_information",False,"no_new_information")])
def test_reviewed_sources_and_continue(linked, decision, inferred, expected):
    case=linked
    seen=[]
    def choose(envelope,raw):
        request=envelope.request
        if request.stage=="inner_plan":
            sources=json.loads(request.history_context).get("evidence_sources",[])
            seen.append(sources)
            if len(seen)<=2:
                return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
            if len(seen)==3:
                assert len(sources)==2
                return dict(kind="fact_candidate",content="The two local commands returned test tool output.",reason="Read both saved command results.",evidence_refs=[s["ref"] for s in sources],inferred=inferred)
            case.token.is_cancelled=True
        return raw
    case.models["inner"].transform=choose
    reviewed=[]
    def review(envelope,raw):
        reviewed.append(envelope.request)
        return dict(decision=decision,reason="Controlled evidence review.",**(dict(refined_content="Both saved local commands printed test tool output.") if decision=="accept" else {}))
    case.models["fact"].transform=review
    result=asyncio.run(case.app.run(case.token))
    records=case.app.store.list_d8_conclusions(case.task)
    assert len(records)==1, (result.stop_reason,[(s.start.category,s.completion) for s in case.app.mcp.records()])
    record=records[0]
    assert record.outcome==expected and record.plan_id is None
    assert len(reviewed)==1 and len(reviewed[0].observation.sources)==2
    assert all(s["data"]["result"]["stdout"]=="test tool output" for s in reviewed[0].observation.sources)
    assert len(seen)==4 and len(case.tools.calls)==2, result.stop_reason
    assert case.app.state.plans==() and case.app.state.executions==()
    assert len(case.app.state.facts)==int(expected=="verified_fact")
    if case.app.state.facts:
        fact=case.app.state.facts[0]
        assert fact.content=="Both saved local commands printed test tool output."
        case.app.evidence_conclusions.validate_fact(fact,case.app.state)
    history=case.app.handoff.history()
    assert len([i for i in history if i.record_type=="reviewed_inference"])==int(expected=="reviewed_inference")

from test_http import http_case
from test_mcp_link import Tools
from agent_adapters.mcp_plan_executor import MCPPlanExecutor

@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_public_http_assembly_accepts_mcp_fact_and_completes_intent(http_case):
    c=http_case
    tools=Tools(c.task,c.app.state.task_run.sandbox_id)
    c.app.mcp=MCPPlanExecutor(c.app,tools)
    c.app.mcp.authorize(c.task)
    original_inner=c.models[1].invoke
    def inner(envelope,*args):
        if envelope.output_contract["stage"]=="inner_plan":
            context=json.loads(envelope.request.history_context)
            sources=context["evidence_sources"]
            if not sources:
                return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
            facts=context["intent_execution"]["facts"]
            if not facts:
                return dict(kind="fact_candidate",content="The local command printed test tool output.",reason="Saved command output.",evidence_refs=[sources[0]["ref"]],inferred=False)
            return dict(kind="intent_resolution",outcome="succeeded",reason="The local command observation is complete.",evidence_refs=[f["ref"] for f in facts],fact_ids=[f["fact_id"] for f in facts],next_intent_candidates=[])
        if envelope.output_contract["stage"]=="inner_candidate":return dict(action=dict(kind="no_candidate"))
        return original_inner(envelope,*args)
    c.models[1].invoke=inner
    result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,result.stop_reason
    assert c.app.state.intents[0].status.value=="succeeded"
    assert len(tools.calls)==1 and not c.sends
    assert len(c.app.state.facts)==1 and not c.app.state.plans
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot=snapshot_v3_task_result(c.directory,c.declaration)
    assert not snapshot["errors"],snapshot
    assert len(snapshot["verified_facts"])==1
    assert snapshot["verified_facts"][0]["source_execution_id"] is None
    assert snapshot["verified_facts"][0]["source_operation_id"]==c.app.state.facts[0].source_ref
    # Runtime reload validates evidence without calling providers or tools.
    c.app.store.close()
    c.app=c.create()
    c.app.controller.attach(c.task)
    assert len(c.app.state.facts)==1

@pytest.mark.parametrize("output_case", [22], indirect=True)
@pytest.mark.parametrize("fresh_observation",[False,True])
def test_explicit_reuse_and_source_loss_do_not_add_fact(linked,fresh_observation):
    c=linked
    decisions=[]
    def inner(envelope,raw):
        if envelope.request.stage=="inner_plan":
            sources=json.loads(envelope.request.history_context)["evidence_sources"]
            if not sources:return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
            if fresh_observation and decisions and len(c.tools.calls)<2:
                return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf second"))
            if len(decisions)>=3:
                c.token.is_cancelled=True
                return raw
            return dict(kind="fact_candidate",content="One local command printed test tool output.",reason="Same saved observation.",evidence_refs=[sources[-1]["ref"]],inferred=False)
        return raw
    def outer(envelope,raw):
        request=envelope.request
        decisions.append(request)
        if request.existing_facts:
            if request.correction_of_model_request_id is None:
                return dict(decision="accept",reason="Ambiguous duplicate must be corrected.")
            return dict(decision="reuse",reason="The same effective conclusion.",reuse_fact_id=request.existing_facts[0]["fact_id"])
        return dict(decision="accept",reason="The exact command observation supports this.")
    c.models["inner"].transform=inner
    c.models["fact"].transform=outer
    result=asyncio.run(c.app.run(c.token))
    records=c.app.store.list_d8_conclusions(c.task)
    assert len(records)==2, result.stop_reason
    assert len(records)==2 and len(c.app.state.facts)==1
    assert records[0].fact_id==records[1].fact_id and len(decisions)==3
    assert len(c.tools.calls)==(2 if fresh_observation else 1)
    # Corrupting/removing the original result cannot be covered by the approval.
    source=decisions[0].observation.sources[0]["ref"]
    path=next(p for p in (c.directory/"d8-output").rglob("*"+source+"*.json") if "mcp-result" in p.name)
    path.unlink()
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    with pytest.raises(D3RecoveryRequired):c.app.evidence_conclusions.validate_fact(c.app.state.facts[0],c.app.state)

@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_mixed_http_and_mcp_sources_share_one_review(http_case):
    c=http_case
    tools=Tools(c.task,c.app.state.task_run.sandbox_id)
    c.app.mcp=MCPPlanExecutor(c.app,tools)
    c.app.mcp.authorize(c.task)
    previous=c.models[1].invoke
    count=[]
    def inner(envelope,*args):
        if envelope.output_contract["stage"]=="inner_candidate":return dict(action=dict(kind="no_candidate"))
        if envelope.output_contract["stage"]!="inner_plan":return previous(envelope,*args)
        count.append(True)
        packet=json.loads(envelope.request.history_context)
        if len(count)==1:return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
        if len(count)==2:return dict(kind="http_request",method="GET",path_query="/",headers={},body="")
        if len(count)==3:
            assert {s["kind"] for s in packet["evidence_sources"]}=={"mcp","http"}
            from dataclasses import replace
            from agent_core.output_contracts import output_schema,shape_issues
            from agent_core.evidence_conclusions import EvidenceCandidate
            from agent_core.dual_llm_modeling import D3ProposalRejected
            stopped=json.loads(envelope.request.history_context)
            stopped["intent_execution"]["stop_requested"]={"review_id":"controlled-stop"}
            stop_request=replace(envelope.request,history_context=json.dumps(stopped))
            candidate=dict(kind="fact_candidate",content="Claim",reason="Reason",evidence_refs=[packet["evidence_sources"][0]["ref"]],inferred=False)
            assert shape_issues(candidate,output_schema(stop_request,contract_version=22)[1])
            with pytest.raises(D3ProposalRejected,match="stop_summary_required"):
                c.app.evidence_conclusions.validate_candidate(stop_request,EvidenceCandidate.parse(candidate))
            return dict(kind="fact_candidate",content="A local command printed test tool output and the scoped HTTP GET returned 200.",reason="Both independent channel records are visible.",evidence_refs=[s["ref"] for s in packet["evidence_sources"]],inferred=False)
        available=packet["intent_execution"]["facts"]
        return dict(kind="intent_resolution",outcome="succeeded",reason="The bounded observations are complete.",evidence_refs=[f["ref"] for f in available],fact_ids=[f["fact_id"] for f in available],next_intent_candidates=[])
    c.models[1].invoke=inner
    result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,result.stop_reason
    assert len(c.sends)==1 and len(tools.calls)==1
    generic=[r for r in c.app.store.list_d8_conclusions(c.task) if r.plan_id is None]
    assert len(generic)==1 and len(c.app.state.facts)==2
    request=c.app.conclusions.evidence.read_request(generic[0].review_request_id)
    assert len(request.observation.sources)==2
    from types import SimpleNamespace
    fact_ref=next(i.ref for i in c.app.handoff.history() if i.record_id==generic[0].fact_id)
    action=SimpleNamespace(evidence_refs=[fact_ref],content="A scoped reviewed correction.")
    snapshots=c.app.revisions._sources(action,c.app.handoff.history())
    restored=json.loads(json.dumps(snapshots))
    assert c.app.revisions._source_snapshot(restored[0])==restored[0]
    assert c.app.revisions._sources(action,c.app.handoff.history())==restored
    from agent_adapters.deepseek_roles import build_payload
    envelope=c.app.output_boundary.envelope(request)
    payload=build_payload(envelope,c.app.config.outer_role,context_loader=c.app.web.model_context)
    assert "Contract17 supports only registered fixed" not in payload["messages"][0]["content"]
    assert "test tool output" in payload["messages"][1]["content"]
    assert "ordinary response" in payload["messages"][1]["content"]
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot=snapshot_v3_task_result(c.directory,c.declaration)
    assert not snapshot["errors"],snapshot
    source=next(s["ref"] for s in request.observation.sources if s["kind"]=="mcp")
    next(p for p in (c.directory/"d8-output").rglob("*"+source+"*.json") if "mcp-result" in p.name).unlink()
    failed=snapshot_v3_task_result(c.directory,c.declaration)
    assert failed["verified_facts"]==[]
    assert failed["errors"][0]["code"]=="state_read_failed" and failed["errors"][0]["read_stage"]=="conclusion_evidence"
    assert "d22_evidence_source_unavailable" in failed["errors"][0]["error_codes"]
    assert len(c.sends)==1 and len(tools.calls)==1

@pytest.mark.parametrize("output_case",[22],indirect=True)
def test_existing_attachment_proof_is_an_eligible_evidence_source(output_case):
    c=output_case
    asyncio.run(c.app.run(c.token))
    result=c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id)
    source=c.app.evidence_conclusions.source(result.proof_id,c.app.state.intents[0].intent_id)
    assert source["kind"]=="attachment" and source["ref"]==result.proof_id
    assert source["source_ref"] in {e.execution_id for e in c.app.state.executions}

@pytest.mark.parametrize("http_case",["rolling"],indirect=True)
def test_result_projection_uses_same_snapshot_during_concurrent_commit(http_case,monkeypatch):
    from agent_adapters.web import task_result as projection
    from test_intent_execution import install
    c=http_case
    install(c,steps=1)
    asyncio.run(c.app.run(c.token))
    before=c.app.state.latest_checkpoint.sequence
    before_status=c.app.state.task_run.status.value
    original=projection._snapshot_v3_task_result
    committed=[]
    def project(directory,declaration,**kwargs):
        assert kwargs.get("read_store") is not None
        if not committed:
            from uuid import uuid4
            c.app.controller.cancel(str(uuid4()))
            committed.append(c.app.state.latest_checkpoint.sequence)
        return original(directory,declaration,**kwargs)
    monkeypatch.setattr(projection,"_snapshot_v3_task_result",project)
    first=projection.snapshot_v3_task_result(c.directory,c.declaration)
    assert not first["errors"] and first["checkpoint"]["sequence"]==before,first
    assert first["task_run"]["status"]==before_status and committed[0]>before
    second=projection.snapshot_v3_task_result(c.directory,c.declaration)
    assert not second["errors"] and second["checkpoint"]["sequence"]==committed[0]
    assert second["task_run"]["status"]=="cancelled"

@pytest.mark.parametrize("output_case",[22],indirect=True)
def test_review_result_saved_before_commit_is_reused_after_process_loss(linked,monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    c=linked
    seen=[]
    def inner(envelope,raw):
        if envelope.request.stage!="inner_plan":return raw
        sources=json.loads(envelope.request.history_context)["evidence_sources"]
        seen.append(True)
        if not sources:return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
        if len(seen)>2:
            c.token.is_cancelled=True
            return raw
        return dict(kind="fact_candidate",content="The saved command printed test tool output.",reason="One completed local call.",evidence_refs=[sources[0]["ref"]],inferred=False)
    c.models["inner"].transform=inner
    original=c.app.controller.commit_d3_transition
    crashes=[True]
    def commit(operation,*args,**kwargs):
        if operation=="commit_evidence_conclusion" and crashes:
            crashes.pop()
            raise ProcessLoss("after known outer result, before atomic commit")
        return original(operation,*args,**kwargs)
    monkeypatch.setattr(c.app.controller,"commit_d3_transition",commit)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    assert not c.app.state.facts and len(c.models["fact"].calls)==1
    asyncio.run(c.app.resume(c.task,c.token))
    assert len(c.app.state.facts)==1 and len(c.tools.calls)==1
    assert len(c.models["fact"].calls)==1

@pytest.mark.parametrize("output_case",[22],indirect=True)
def test_sensitive_fact_proposal_is_not_persisted_or_reviewed(linked):
    c=linked
    c.app.inner.change_response_sensitive=lambda raw:"EXAMPLE_PRIVATE" in json.dumps(raw)
    def inner(envelope,raw):
        if envelope.request.stage!="inner_plan":return raw
        sources=json.loads(envelope.request.history_context)["evidence_sources"]
        if not sources:return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
        return dict(kind="fact_candidate",content="EXAMPLE_PRIVATE",reason="Must be rejected before storage.",evidence_refs=[sources[0]["ref"]],inferred=False)
    c.models["inner"].transform=inner
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="d8_change_sensitive_rejected",result
    assert not c.app.state.facts and not c.models["fact"].calls
    assert all(b"EXAMPLE_PRIVATE" not in p.read_bytes() for p in c.directory.rglob("*.json"))

def test_schema29_failure_rolls_back_and_retry_migrates_once(tmp_path,monkeypatch):
    import sqlite3
    import agent_core.sqlite_control_store as module
    from agent_core.control_store import StorageError
    path=tmp_path/"schema28.sqlite"
    original=module._sql_statements
    # Build schema28 tables: skip all later DDL as well as their fixture markers.
    monkeypatch.setattr(module,"_sql_statements",lambda script:[] if script in (module.MIGRATION_29,module.MIGRATION_30) else original(script))
    with module.SqliteControlStore.open(path):pass
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM schema_versions WHERE version>=29")
        assert next(r for r in db.execute("PRAGMA table_info(d8_conclusions)") if r[1]=="plan_id")[3]==1
        assert any(r[3]=="source_ref" and r[2]=="executions" for r in db.execute("PRAGMA foreign_key_list(facts)"))
    monkeypatch.setattr(module,"_sql_statements",lambda script:[original(script)[0],"INVALID SQL"] if script==module.MIGRATION_29 else original(script))
    with pytest.raises(StorageError):module.SqliteControlStore.open(path)
    with module.SqliteControlStore.open_readonly(path) as store:
        assert store._schema_version==28
        assert not store._connection.execute("SELECT name FROM sqlite_master WHERE name IN ('facts_new','d8_conclusions_new')").fetchall()
    monkeypatch.setattr(module,"_sql_statements",original)
    with module.SqliteControlStore.open(path) as store:
        assert store._schema_version==module.CURRENT_SCHEMA_VERSION and store._connection.execute("PRAGMA foreign_key_check").fetchone() is None

@pytest.mark.parametrize("output_case",[14],indirect=True)
def test_old_contract_cannot_gain_fact_candidate_action(output_case):
    c=output_case
    c.models["inner"].transform=lambda envelope,raw:dict(kind="fact_candidate",content="Claim",reason="Unavailable in this contract.",evidence_refs=[],inferred=False) if envelope.request.stage=="inner_plan" else raw
    result=asyncio.run(c.app.run(c.token))
    assert result.recovery_required and not c.app.state.facts and not c.models["fact"].calls

@pytest.mark.parametrize("output_case",[22],indirect=True)
def test_reuse_rechecks_effective_fact_after_known_approval(linked,monkeypatch):
    c=linked
    decisions=[]
    def inner(envelope,raw):
        if envelope.request.stage!="inner_plan":return raw
        sources=json.loads(envelope.request.history_context)["evidence_sources"]
        if not sources:return dict(kind="mcp_call",tool_name="shell.run_bash",arguments=dict(script="printf test"))
        return dict(kind="fact_candidate",content="The saved local command printed test tool output.",reason="The same scoped observation.",evidence_refs=[sources[0]["ref"]],inferred=False)
    def outer(envelope,raw):
        request=envelope.request
        decisions.append(request)
        if request.existing_facts:return dict(decision="reuse",reason="The frozen conclusion was visible.",reuse_fact_id=request.existing_facts[0]["fact_id"])
        return dict(decision="accept",reason="The initial scope is supported.")
    c.models["inner"].transform=inner
    c.models["fact"].transform=outer
    original_commit=c.app.controller.commit_d3_transition
    original_reliable=c.app.revisions.reliable
    def commit(operation,*args,**kwargs):
        if operation=="commit_evidence_conclusion" and c.app.state.facts:
            identity=c.app.state.facts[0].fact_id
            monkeypatch.setattr(c.app.revisions,"reliable",lambda item:False if item is not None and item.record_id==identity else original_reliable(item))
        return original_commit(operation,*args,**kwargs)
    monkeypatch.setattr(c.app.controller,"commit_d3_transition",commit)
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="d22_reused_fact_not_effective",result
    assert len(c.app.state.facts)==1 and len(c.app.store.list_d8_conclusions(c.task))==1
    assert len(c.tools.calls)==1 and len(decisions)==2
