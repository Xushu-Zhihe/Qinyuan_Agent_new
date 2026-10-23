"""C01-2 local platform/real ledger checks; no live models, Docker or competition."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from test_http import http_case
from test_exploration_loop import install
from test_mcp_link import Tools
from test_competition import platform,benchmark,probe
from test_workbench import call
from agent_adapters.competition import submit_candidate
from agent_adapters.competition_submission import submit,SubmissionEvidence,feedback_progress
from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_adapters.workbench_contracts import WorkbenchError
from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_core.goal_feedback import ExternalValidationFeedback,project_candidate_feedback
from agent_core.runtime import TaskRunStatus
from agent_core.competition_candidates import validate_mcp_source
from agent_core.turn_modeling import digest


def binding(case):
    return {"request_id":str(uuid4()),"profile":{"id":str(uuid4()),"adapter":"benchmark","base_url":"http://example.test","timeout_seconds":2},
            "question":{"remote_ref":"controlled-question"},"platform_deadline_at":None}


def pending(case,*,mcp=False):
    install(case,steps=0 if mcp else 1)
    if mcp:
        tools=Tools(case.task,case.app.state.task_run.sandbox_id)
        case.app.mcp=MCPPlanExecutor(case.app,tools);case.app.mcp.authorize(case.task)
        previous=case.models[1].invoke
        def choose(envelope,*args):
            stage=envelope.output_contract["stage"]
            if stage=="inner_plan" and not tools.calls:
                return {"kind":"mcp_call","tool_name":"shell.run_bash","arguments":{"script":"fixed test"}}
            if stage=="inner_candidate":
                return {"action":{"kind":"candidate_flag","value":"test tool output","source_ref":envelope.request.context.source_ref}}
            return previous(envelope,*args)
        case.models[1].invoke=choose
    report=asyncio.run(case.app._run_once(case.token))
    assert report.stop_reason=="d9_awaiting_external_feedback",report
    candidate=case.app.store.list_d7_answer_candidates(case.task)[-1]
    return candidate


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_mcp_only_protected_candidate_has_no_fake_plan_or_execution(http_case):
    c=http_case;candidate=pending(c,mcp=True)
    assert candidate.source_kind=="mcp" and candidate.plan_id is None and candidate.execution_id is None
    assert not c.app.state.plans and not c.app.state.executions
    service=D7CandidateEvidenceService(c.directory,c.task,trusted_adapter_ids=("competition",),create=False)
    assert service.read_port("competition").read(candidate.candidate_id)=="test tool output"
    validate_mcp_source(c.app.store,candidate,c.app.output_boundary.evidence)
    with pytest.raises(ValueError):validate_mcp_source(c.app.store,replace(candidate,evidence_version="0"*64),c.app.output_boundary.evidence)
    from agent_core.completion_evaluation import check_candidate_task
    assert not check_candidate_task(c.app.config,c.app.output_boundary.evidence,c.app.sessions.evidence,c.app.model_evidence,c.app.store,c.task)


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
@pytest.mark.parametrize("verdict",["rejected","partial","accepted"])
def test_receipt_applies_to_original_task_and_replay_does_not_post(http_case,verdict):
    c=http_case;candidate=pending(c);frozen=binding(c);operation=str(uuid4());calls=[]
    def send(*args):
        calls.append(args[-1]);return {"verdict":verdict,"correct_flag_count":3 if verdict=="accepted" else 1,"total_flag_count":3}
    result=submit(c.app,frozen,"fake-token",operation,send=send)
    assert result["state"]==verdict and calls==["sample-answer"]
    assert c.app.state.task_run.task_run_id==c.task
    assert c.app.state.task_run.status==(TaskRunStatus.ACHIEVED if verdict=="accepted" else TaskRunStatus.RUNNING)
    assert submit(c.app,frozen,"fake-token",operation,send=send)==result and len(calls)==1
    records=c.app.store.list_d7_validation_feedback(c.task,candidate.candidate_id,candidate.evidence_version)
    assert records[-1].operation_id==operation
    assert feedback_progress(c.directory,records[-1])["correct_flag_count"]==result["correct_flag_count"]
    assert project_candidate_feedback(c.task,candidate.candidate_id,candidate.evidence_version,records).state==verdict
    assert "fake-token" not in b"".join(p.read_bytes() for p in (c.directory/"competition-submissions").glob("*.json")).decode()


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
@pytest.mark.parametrize("failure",["lost_receipt","unknown_response","before_apply","after_apply"])
def test_submission_interruptions_never_repeat_post(http_case,monkeypatch,failure):
    c=http_case;candidate=pending(c);frozen=binding(c);operation=str(uuid4());calls=[]
    def send(*args):
        calls.append(1)
        if failure=="unknown_response":raise WorkbenchError("competition_transport_failed",502)
        return {"verdict":"partial","correct_flag_count":1,"total_flag_count":3}
    save=SubmissionEvidence.save;apply=c.app.controller.apply_external_validation_feedback
    if failure=="lost_receipt":
        def broken(self,kind,*args):
            if kind=="receipt":raise RuntimeError("controlled receipt interruption")
            return save(self,kind,*args)
        monkeypatch.setattr(SubmissionEvidence,"save",broken)
    elif failure in {"before_apply","after_apply"}:
        def broken(feedback):
            if failure=="after_apply":apply(feedback)
            raise RuntimeError("controlled apply interruption")
        monkeypatch.setattr(c.app.controller,"apply_external_validation_feedback",broken)
    if failure=="unknown_response":assert submit(c.app,frozen,"fake",operation,send=send)["state"]=="unknown"
    else:
        with pytest.raises(RuntimeError):submit(c.app,frozen,"fake",operation,send=send)
    monkeypatch.setattr(SubmissionEvidence,"save",save)
    monkeypatch.setattr(c.app.controller,"apply_external_validation_feedback",apply)
    again=submit(c.app,frozen,"fake",operation,send=send)
    assert len(calls)==1
    assert again["state"]==("unknown" if failure in {"lost_receipt","unknown_response"} else "partial")
    if again["state"]=="unknown":
        assert submit(c.app,frozen,"fake",str(uuid4()),send=send)["state"]=="unknown" and len(calls)==1


def test_import_requires_explicit_protocol_and_reuses_question(platform):
    p=platform;p.workbench.execution_profile="d8_v22";p.workbench.protocol_version=3
    profile,_=p.profile();p.payload=[benchmark()]
    path=f"/api/competition/profiles/{profile['id']}/import"
    code,result=call(p.port,"POST",path,{"remote_ref":"test-code"})
    assert code==400 and result["error"]=="competition_target_selection_required"
    body={"remote_ref":"test-code","endpoint":"http://10.0.1.5:8080"}
    code,first=call(p.port,"POST",path,body)
    assert code==201,first
    code,second=call(p.port,"POST",path,body)
    assert code==201 and second["id"]==first["id"] and len(p.workbench.list())==1
    assert all(r[0]=="GET" for r in p.requests)
    body["endpoint"]="http://another.test:8080"
    assert call(p.port,"POST",path,body)[1]["error"]=="competition_target_not_in_question"


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
@pytest.mark.parametrize("correct,completed",[(False,False),(True,False),(True,True)])
def test_real_loopback_post_and_trusted_feedback_roundtrip(http_case,platform,correct,completed):
    c=http_case;p=platform;pending(c)
    profile,token=p.profile();p.bodies=[]
    p.post_payload={"correct":correct,"correct_flag_count":3 if completed else 1,"total_flag_count":3}
    frozen=binding(c);frozen["profile"]={k:profile[k] for k in ("id","adapter","base_url","timeout_seconds")}
    result=submit(c.app,frozen,token,str(uuid4()))
    assert result["state"]==("accepted" if completed else "partial" if correct else "rejected")
    assert p.requests[0][:2]==("POST","/openapi/v1/challenges/submit")
    assert p.requests[0][2]["BENCHMARK_TOKEN"]==token
    assert p.bodies==[{"unique_code":"controlled-question","flag":"sample-answer"}]
    assert token not in json.dumps(result) and "sample-answer" not in json.dumps(result)


@pytest.mark.parametrize("correct,state",[(True,"solved"),(False,"active"),(False,"failed")])
def test_arena_submit_exact_attempt_and_known_platform_state(platform,correct,state):
    p=platform;profile,token=p.profile("arena");p.bodies=[]
    p.post_payload={"code":200,"data":{"correct":correct,"attempt":{"id":123,"state_label":state}}}
    result=submit_candidate(profile,token,123,"controlled-flag")
    assert result["verdict"]==("accepted" if correct else "rejected" if state=="active" else "unknown")
    assert p.requests[0][:2]==("POST","/api/skill/agent/arena/attempt/123/submit/")
    assert p.requests[0][2]["Authorization"]=="Bearer "+token and p.bodies==[{"flag":"controlled-flag"}]


def test_duplicate_receipt_queries_progress_without_counting_again(platform):
    p=platform;profile,token=p.profile();p.bodies=[]
    p.post_status=409;p.post_payload={"code":"duplicate"};p.payload=[benchmark()]
    result=submit_candidate(profile,token,"test-code","controlled-flag")
    assert result["duplicate"] and result["verdict"]=="partial" and result["correct_flag_count"]==1
    assert [r[0] for r in p.requests]==["POST","GET"]


def test_explicit_https_default_port_and_path_survive_import(platform):
    p=platform;p.workbench.execution_profile="d8_v22";p.workbench.protocol_version=3
    profile,_=p.profile();p.payload=[benchmark()];p.payload[0]["container_addr"]=["https://example.test/challenge"]
    code,task=call(p.port,"POST",f"/api/competition/profiles/{profile['id']}/import",
                   {"remote_ref":"test-code","endpoint":"https://example.test/challenge"})
    assert code==201,task
    assert task["endpoint"]=="https://example.test:443" and "/challenge" in task["description"]


def test_schema29_migration_preserves_existing_candidate_feedback_and_foreign_keys(tmp_path,monkeypatch):
    from agent_core import sqlite_control_store as sqlite
    from test_candidate_stage_recovery import _make_run
    original=sqlite.MIGRATION_30
    # Execute the genuine pre30 migrations, excluding only the new rebuild.
    monkeypatch.setattr(sqlite,"MIGRATION_30","")
    app,token,service,model=_make_run(tmp_path,monkeypatch,"controlled-legacy-answer")
    candidate=asyncio.run(app.propose_d7_candidate(token,service))
    feedback=ExternalValidationFeedback(candidate.task_run_id,candidate.candidate_id,candidate.evidence_version,
        "rejected","controlled-old-judge",str(uuid4()))
    app.controller.apply_external_validation_feedback(feedback);app.cancel()
    app.store._connection.execute("DROP TABLE task_operator_controls")
    app.store._connection.execute("DELETE FROM schema_versions WHERE version>=30")
    database=app.store.path;app.close()
    with sqlite.SqliteControlStore.open_readonly(database) as store:
        assert store._schema_version==29 and store.load_d7_answer_candidate(candidate.candidate_id)==candidate
    monkeypatch.setattr(sqlite,"MIGRATION_30",original)
    with sqlite.SqliteControlStore.open(database) as store:
        assert store._schema_version==31 and store.load_d7_answer_candidate(candidate.candidate_id)==candidate
        assert store.list_d7_validation_feedback(candidate.task_run_id,candidate.candidate_id,candidate.evidence_version)==(feedback,)
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall()==[]


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_cli_submission_assembly_uses_host_stdin_and_no_model_dispatch(http_case,platform,monkeypatch,capsys,tmp_path):
    import io
    from agent_adapters import cli,simulation,docker_profile
    from agent_adapters.run_files import immutable_write
    c=http_case;p=platform;pending(c)
    profile,token=p.profile();p.bodies=[];p.post_payload={"correct":True,"correct_flag_count":1,"total_flag_count":3}
    frozen=binding(c);frozen["request_id"]=c.declaration["request_id"]
    frozen["profile"]={k:profile[k] for k in ("id","adapter","base_url","timeout_seconds")}
    path=tmp_path/"frozen-platform.json";immutable_write(path,frozen)
    c.app.store.close()
    restored=c.create();c.app=restored
    closed=[]
    resources=SimpleNamespace(_runtime=SimpleNamespace(client=None),close=lambda:closed.append(True))
    monkeypatch.setattr(docker_profile,"connect",lambda *a,**k:(resources,c.sandbox))
    def assemble(*args,**kwargs):
        assert kwargs["target_network_adapter"].network.allow_private_ipv4 is True
        return restored
    monkeypatch.setattr(simulation,"D3Application",assemble)
    before=[len(m.calls) for m in c.models]
    monkeypatch.setattr(cli.sys,"stdin",SimpleNamespace(buffer=io.BytesIO(json.dumps({"credential":token}).encode())))
    result_code=cli.main(["competition-submit","--task-run-id",c.task,"--run-dir",str(c.directory),
        "--binding-file",str(path),"--operation-id",str(uuid4())])
    output=capsys.readouterr();result=json.loads(output.out)
    assert result_code==0,(result.get("errors"),result.get("recovery"),output.err[-4000:])
    assert result["task_run_id"]==c.task and result["task_run"]["status"]=="running"
    assert result["competition_submission"]["state"]=="partial"
    assert result["answer_candidates"][0]["feedback"][0]["correct_flag_count"]==1
    assert [len(m.calls) for m in c.models]==before and closed==[True]
    assert token not in output.out+output.err and "sample-answer" not in output.out+output.err
    assert len(p.bodies)==1


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_workbench_submits_using_stdin_without_credentials_in_args_or_environment(http_case,monkeypatch,tmp_path):
    from agent_adapters.workbench import Workbench
    import agent_adapters.workbench_runner as module
    from agent_adapters.run_files import immutable_write
    from agent_adapters.web.task_result import snapshot_v3_task_result
    from test_workbench_v3 import _NoThread,_challenge
    c=http_case;pending(c)
    hub=Workbench(tmp_path/"hub",protocol_version=3,execution_profile="d8_v22")
    challenge=hub.create(_challenge());root=tmp_path/"launcher";root.mkdir()
    profile=hub.competitions.save({"name":"Controlled","adapter":"benchmark","base_url":"https://example.test","timeout_seconds":2,"credential":str(uuid4())})
    frozen=binding(c);frozen["request_id"]=c.declaration["request_id"]
    frozen["profile"]={k:profile[k] for k in ("id","adapter","base_url","timeout_seconds")}
    immutable_write(root/"competition-binding.json",frozen)
    import shutil
    c.app.store.close()
    agent_dir=root/"agent-state"/c.task
    shutil.copytree(c.directory,agent_dir)
    run=str(uuid4());hub._insert_run(run,challenge["id"],root)
    hub._finish_run(run,status="paused",exit_code=4,result=snapshot_v3_task_result(agent_dir,c.declaration),agent_task_run_id=c.task)
    class Pipe:
        def __init__(self):self.data=b""
        def write(self,value):self.data+=value
        def close(self):pass
    class Process:
        def __init__(self):self.stdin=self.pipe=Pipe()
    calls=[];process=Process()
    monkeypatch.setattr(module.subprocess,"Popen",lambda command,**kwargs:calls.append((command,kwargs)) or process)
    monkeypatch.setattr(module.threading,"Thread",_NoThread)
    hub.runner.competition_submit(challenge["id"])
    command,options=calls[0]
    secret=hub.competitions.credentials[profile["id"]]
    assert command[1:4]==["-m","agent","competition-submit"]
    assert options["stdin"]==module.subprocess.PIPE and json.loads(process.pipe.data)=={"credential":secret}
    assert secret not in json.dumps(command) and secret not in json.dumps(options["env"])
    assert process.stdin is None


@pytest.mark.parametrize("state,operation,action",[("awaiting_external_feedback","run","submit"),("running","competition-submit","resume"),("awaiting_external_feedback","competition-submit",None)])
@pytest.mark.parametrize("scheduled",[False,True])
def test_supervisor_only_continues_bound_known_question(tmp_path,monkeypatch,state,operation,action,scheduled):
    from agent_adapters.workbench import Workbench
    from agent_adapters.workbench_runner import ActiveRun
    from test_workbench_v3 import _FinishedProcess,_challenge
    hub=Workbench(tmp_path/"hub",protocol_version=3,execution_profile="d8_v22")
    challenge=hub.create(_challenge());root=tmp_path/"launcher";root.mkdir()
    from agent_adapters.run_files import immutable_write
    immutable_write(root/"competition-binding.json",dict(request_id=str(uuid4()),**({"schedule":{"session_id":str(uuid4())}} if scheduled else {})))
    run,task=str(uuid4()),str(uuid4());hub._insert_run(run,challenge["id"],root)
    result={"protocol_version":3,"contract_version":22,"task_run_id":task,"task_run":{"status":state},"exit_code":0,"run_exit_code":4,
            "competition_submission":{"state":"partial" if state=="running" else "unknown"}}
    process=_FinishedProcess(result)
    hub.runner._active[run]=ActiveRun(run,challenge["id"],root,process,operation=operation)
    calls=[]
    monkeypatch.setattr(hub.runner,"competition_submit",lambda identity:calls.append(("submit",identity)))
    monkeypatch.setattr(hub.runner,"resume",lambda identity:calls.append(("resume",identity)))
    hub.runner._watch(run)
    assert calls==([(action,challenge["id"])] if action and not scheduled else [])


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_repeated_body_reuses_known_receipt_across_candidate_ids(http_case):
    from agent_core.goal_candidates import D7AnswerCandidate
    c=http_case;first=pending(c);frozen=binding(c);calls=[]
    def send(*args):calls.append(1);return {"verdict":"partial","correct_flag_count":1,"total_flag_count":3}
    submit(c.app,frozen,"fake",str(uuid4()),send=send)
    second=replace(first,candidate_id=str(uuid4()))
    c.app.controller.register_answer_candidate(second,str(uuid4()))
    reader=SimpleNamespace(read=lambda candidate_id:"sample-answer")
    result=submit(c.app,frozen,"fake",str(uuid4()),reader=reader,send=send)
    assert result["state"]=="partial" and result["correct_flag_count"]==1 and len(calls)==1
    assert len(c.app.store.list_d7_answer_candidates(c.task))==2


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_known_auth_rejection_can_be_explicitly_retried_without_false_flag_verdict(http_case):
    c=http_case;candidate=pending(c);frozen=binding(c)
    def denied(*args):raise WorkbenchError("competition_auth_failed",502)
    result=submit(c.app,frozen,"fake",str(uuid4()),send=denied)
    assert result["state"]=="blocked" and c.app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    assert not c.app.store.list_d7_validation_feedback(c.task,candidate.candidate_id,candidate.evidence_version)
    result=submit(c.app,frozen,"fixed-fake",str(uuid4()),send=lambda *args:{"verdict":"partial"})
    assert result["state"]=="partial" and c.app.state.task_run.status is TaskRunStatus.RUNNING


def test_partial_reconciliation_and_conflict_remain_distinct_from_overall_success():
    from agent_core.goal_feedback import ExternalValidationReconciliation,ExternalValidationConflictSource
    task,candidate=str(uuid4()),str(uuid4());version="a"*64
    partial=ExternalValidationFeedback(task,candidate,version,"partial","test",str(uuid4()))
    rejected=replace(partial,verdict="rejected",operation_id=str(uuid4()))
    conflict=project_candidate_feedback(task,candidate,version,[partial,rejected])
    assert conflict.state=="conflict"
    sources=tuple(ExternalValidationConflictSource.from_record(r) for r in sorted([partial,rejected],key=lambda r:r.operation_id))
    resolved=ExternalValidationReconciliation(task,candidate,version,"partial","test-query",str(uuid4()),partial.operation_id,sources)
    assert project_candidate_feedback(task,candidate,version,[partial,rejected,resolved]).state=="partial"
    unknown=replace(partial,verdict="unknown",operation_id=str(uuid4()))
    resolved=ExternalValidationReconciliation(task,candidate,version,"partial","test-query",str(uuid4()),unknown.operation_id)
    assert project_candidate_feedback(task,candidate,version,[unknown,resolved]).state=="partial"


def test_freeze_rechecks_edited_target_and_expired_platform_binding(platform,tmp_path):
    import time
    p=platform;p.workbench.execution_profile="d8_v22";p.workbench.protocol_version=3
    profile,_=p.profile();p.payload=[benchmark()];p.payload[0]["container_addr"]=["93.184.216.34:8080"]
    code,task=call(p.port,"POST",f"/api/competition/profiles/{profile['id']}/import",
                   {"remote_ref":"test-code","endpoint":"http://93.184.216.34:8080"})
    assert code==201
    service=p.workbench.competitions
    request={"request_id":str(uuid4()),"execution_profile":"d8_v22"}
    with pytest.raises(WorkbenchError,match="competition_target_not_in_question"):
        service.freeze({**task,"endpoint":"http://93.184.216.35:8080"},tmp_path,request)
    service.freeze(task,tmp_path,request)
    from agent_adapters.run_files import immutable_read
    assert immutable_read(tmp_path/"competition-binding.json")["request_id"]==request["request_id"]
    with p.workbench._db() as db:
        record=db.execute("SELECT snapshot_json FROM competition_questions WHERE challenge_id=?",(task["id"],)).fetchone()
        snapshot=json.loads(record["snapshot_json"]);snapshot["platform_deadline_at"]=time.time()-1
        db.execute("UPDATE competition_questions SET snapshot_json=? WHERE challenge_id=?",(json.dumps(snapshot),task["id"]))
    with pytest.raises(WorkbenchError,match="competition_question_not_active"):
        service.freeze(task,tmp_path,request)


@pytest.mark.parametrize("address",["127.0.0.1","169.254.169.254","100.64.1.2"])
def test_import_rejects_special_addresses_even_when_platform_lists_them(platform,address):
    p=platform;p.workbench.execution_profile="d8_v22";p.workbench.protocol_version=3
    profile,_=p.profile();p.payload=[benchmark()];p.payload[0]["container_addr"]=[address+":8080"]
    code,result=call(p.port,"POST",f"/api/competition/profiles/{profile['id']}/import",
                     {"remote_ref":"test-code","endpoint":"http://"+address+":8080"})
    assert code==409 and result["error"]=="competition_target_address_not_supported"
    assert not p.workbench.list()


@pytest.mark.parametrize("version",[21,22])
def test_manual_workbench_private_target_is_scoped_to_current_contract(platform,version):
    p=platform;p.workbench.protocol_version=3;p.workbench.execution_profile=f"d8_v{version}"
    body=dict(title="Controlled private target",category="web",description="Explicit test scope",endpoint="http://10.0.1.5:8080",deadline_minutes=10,step_timeout_seconds=30)
    code,task=call(p.port,"POST","/api/challenges",body)
    if version==21:
        assert code==400 and task["error"]=="invalid_endpoint"
    else:
        assert code==201 and task["endpoint"]==body["endpoint"]
        body["endpoint"]="http://192.168.1.5:8081"
        assert call(p.port,"PUT",f"/api/challenges/{task['id']}",body)[0]==200


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_current_inner_mcp_prompt_explains_isolated_target_transport(http_case):
    from agent_adapters.deepseek_roles import build_payload
    c=http_case;pending(c,mcp=True)
    model=c.models[1]
    envelope=next(e for e in c.exploration_envelopes if e.output_contract["stage"]=="inner_plan")
    system="\n".join(m["content"] for m in build_payload(envelope,model.role_config)["messages"] if m["role"]=="system")
    assert "target.qinyuan" in system and "curl --connect-to" in system
    assert "certificate verification" in system and "Do not replace the authorized endpoint" in system
