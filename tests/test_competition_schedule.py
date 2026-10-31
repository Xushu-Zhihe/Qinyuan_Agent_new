"""C01-3 controlled operator/competition tests; no real Docker, model or platform."""
import asyncio
from uuid import uuid4
import pytest
from test_http import http_case
from agent_core import operator_controls as controls
from agent_core.controller import TaskRunController
from agent_core.operation_accounting import D4OperationStart
from agent_core.runtime import TaskRunStatus


def configure(case,overall=1800,platform=3600):
    state=TaskRunController.submit_operator_control(case.app.store,case.task,str(uuid4()),"configure",dict(
        question_seconds=600,overall_deadline_at=case.clock()+overall,platform_deadline_at=case.clock()+platform))
    controls.enable(case.app.controller)
    return state


def command(case,action,payload=None,identity=None):
    return TaskRunController.submit_operator_control(case.app.store,case.task,identity or str(uuid4()),action,payload or {})


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_deadline_extension_is_idempotent_preserves_request_and_checkpoint(http_case):
    c=http_case;original=c.app.state.task_run;checkpoint=c.app.state.latest_checkpoint
    configure(c)
    operation=str(uuid4());changed=command(c,"extend_question",{"seconds":120},operation)
    assert c.app.state.task_run.deadline_at==original.deadline_at+120
    assert command(c,"extend_question",{"seconds":120},operation)==changed
    assert c.app.state.task_run.request==original.request and c.app.state.latest_checkpoint==checkpoint
    with pytest.raises(ValueError,match="operator_operation_conflict"):command(c,"extend_question",{"seconds":121},operation)
    c.app.cancel()
    assert c.app.state.task_run.status is TaskRunStatus.CANCELLED


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_pause_blocks_new_admission_and_keeps_original_intent_and_sandbox(http_case):
    c=http_case;configure(c);before=c.app.state
    command(c,"pause")
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="operator_paused" and not result.recovery_required
    assert c.app.state==before and not any(model.calls for model in c.models)
    assert not c.sandbox.destroyed
    command(c,"resume")
    assert not controls.current(c.app.store,c.task)["paused"]


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_overall_extension_does_not_increase_question_budget_and_caps_single_extension(http_case):
    c=http_case;original=c.app.state.task_run.deadline_at
    configure(c,overall=900,platform=1000)
    global_state=command(c,"extend_overall",{"seconds":500})
    assert global_state["question_deadline_at"]==original and c.app.state.task_run.deadline_at==original
    with pytest.raises(ValueError,match="operator_extension_exceeds_ceiling"):command(c,"extend_question",{"seconds":401})
    command(c,"extend_question",{"seconds":400})
    assert c.app.state.task_run.deadline_at==original+400


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_unknown_operation_cannot_be_resumed_or_redispatched(http_case):
    c=http_case;configure(c)
    start=D4OperationStart(str(uuid4()),c.task,"tool_dispatch",c.clock(),c.app.state.latest_checkpoint.sequence,
        "a"*64,"controlled",c.app.config.d4_budget.budget_policy_ref)
    c.app.store.begin_d4_operation(start)
    command(c,"pause")
    with pytest.raises(ValueError,match="operator_unknown_operation"):command(c,"resume")
    with pytest.raises(controls.OperatorPaused):
        c.app.store.begin_d4_operation(D4OperationStart(str(uuid4()),c.task,"tool_dispatch",c.clock(),c.app.state.latest_checkpoint.sequence,
            "a"*64,"controlled-next",c.app.config.d4_budget.budget_policy_ref))
    assert len(c.app.store.list_d4_operations(c.task))==1


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_expired_deadline_cannot_be_extended_or_reactivated(http_case):
    c=http_case;configure(c)
    c.clock.now=c.app.state.task_run.deadline_at
    with pytest.raises(ValueError,match="operator_task_expired_or_terminal"):command(c,"extend_question",{"seconds":60})


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_control_state_survives_readonly_reopen_without_migration(http_case):
    from agent_core.sqlite_control_store import SqliteControlStore, CURRENT_SCHEMA_VERSION
    c=http_case;configure(c);command(c,"pause")
    with SqliteControlStore.open_readonly(c.app.store.path) as store:
        assert store._schema_version==CURRENT_SCHEMA_VERSION and controls.current(store,c.task)["paused"]
        assert store._connection.total_changes == 0
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall()==[]


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_inflight_model_result_drains_then_pause_blocks_following_call(http_case):
    from agent_core.sqlite_control_store import SqliteControlStore
    c=http_case;configure(c)
    original=c.models[0].invoke
    def response(*args):
        with SqliteControlStore.open(c.app.store.path,clock=c.clock) as other:
            controls.submit(other,c.task,str(uuid4()),"pause",{})
        return original(*args)
    c.models[0].invoke=response
    result=asyncio.run(asyncio.wait_for(c.app.run(c.token),5))
    assert result.stop_reason=="operator_paused" and not result.recovery_required
    assert c.app.state.task_run.status is TaskRunStatus.RUNNING
    assert sum(len(model.calls) for model in c.models)==1
    assert not c.app.store.blocking_d4_operations(c.task)


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_deadline_refresh_from_other_connection_does_not_invalidate_checkpoint(http_case):
    from agent_core.sqlite_control_store import SqliteControlStore
    c=http_case;configure(c);before=c.app.state.latest_checkpoint
    with SqliteControlStore.open(c.app.store.path,clock=c.clock) as other:
        controls.submit(other,c.task,str(uuid4()),"extend_question",{"seconds":60})
    assert c.app.state.task_run.deadline_at==c.app.state.task_run.started_at+660
    assert c.app.state.latest_checkpoint==before
    c.app.controller.advance_time(str(uuid4()))
    assert c.app.state.task_run.status is TaskRunStatus.RUNNING


from test_competition import platform,benchmark
from test_workbench import call
import json
from types import SimpleNamespace
from agent_adapters.workbench_contracts import WorkbenchError
from agent_adapters import competition_schedule as module


def schedule(platform,adapter="benchmark",**changes):
    p=platform;p.workbench.protocol_version=3;p.workbench.execution_profile="d8_v22";p.workbench.d7_mode=None
    p.workbench.schedules.automatic=False
    profile,token=p.profile(adapter)
    value=dict(profile_id=profile["id"],default_scheme="http",question_minutes=10,overall_minutes=30,
        queue=["test-code"] if adapter=="benchmark" else [],http_methods=["GET","HEAD","OPTIONS"],mode="real")
    value.update(changes)
    code,row=call(p.port,"POST","/api/competition/sessions",value)
    assert code==201,row
    return p.workbench.schedules,row,profile,token


def fake_run(p,row,status="running",**extra):
    hub=p.workbench;identity=row["state"]["challenge_id"]
    run=hub.runner.status(identity)
    if run is None:
        root=hub.root/"controlled-agent"/identity;root.mkdir(parents=True,exist_ok=True)
        from agent_adapters.run_files import immutable_write
        immutable_write(root/"competition-binding.json",dict(request_id=str(uuid4())))
        run_id=str(uuid4());hub._insert_run(run_id,identity,root)
    else:run_id=run["run_id"]
    hub._finish_run(run_id,status="paused",exit_code=4,result=dict(task_run=dict(status=status),**extra),agent_task_run_id=str(uuid4()))


def test_session_api_locks_origin_allows_same_origin_key_repair_and_clear_pauses(platform):
    p=platform;service,row,profile,token=schedule(p)
    body={k:v for k,v in profile.items() if k not in {"id","credential_configured"}}
    body.update(credential=str(uuid4()),name="Repaired")
    code,result=call(p.port,"PUT",f"/api/competition/profiles/{profile['id']}",body)
    assert code==200 and result["credential_configured"]
    body["base_url"]="https://different.example.test"
    code,result=call(p.port,"PUT",f"/api/competition/profiles/{profile['id']}",body)
    assert code==409 and result["error"]=="competition_profile_in_use"
    code,_=call(p.port,"DELETE",f"/api/competition/profiles/{profile['id']}/credential")
    assert code==200 and service.get(row["id"])["state"]["status"]=="pausing"
    service.tick()
    assert service.get(row["id"])["state"]["status"]=="paused" and not p.requests
    assert token not in p.workbench.database.read_bytes().decode(errors="ignore")


def test_serial_benchmark_preserves_same_task_after_partial_and_stops_after_success(platform,monkeypatch):
    p=platform;p.payload=[benchmark()]
    service,row,_,_=schedule(p)
    starts=[];submits=[];resumes=[]
    def start(challenge,**options):
        starts.append((challenge,options));fake_run(p,service.get(row["id"]))
    monkeypatch.setattr(p.workbench.runner,"start",start)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"competition_submit",lambda challenge:submits.append(challenge))
    monkeypatch.setattr(p.workbench.runner,"resume",lambda challenge:resumes.append(challenge))
    service.tick();current=service.get(row["id"]);challenge=current["state"]["challenge_id"]
    assert len(starts)==1 and p.workbench.get(challenge)["endpoint"]=="http://10.0.1.5:8080"
    assert starts[0][1]["competition_control"]["session_id"]==row["id"]
    fake_run(p,current,"awaiting_external_feedback")
    service.tick();assert submits==[challenge]
    fake_run(p,current,"running",competition_submission={"state":"partial"})
    service.tick();assert resumes==[challenge] and len(starts)==1
    service.command(row["id"],dict(action="stop_after_current",enabled=True))
    p.payload[0].update(is_completed=True,correct_flag_count=3)
    fake_run(p,current,"achieved")
    service.tick();assert service.get(row["id"])["state"]["status"]=="finished"
    assert all(method=="GET" for method,_,_ in p.requests) # reused instance never auto-closed


def test_owned_benchmark_instance_is_closed_once_only_after_whole_success(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];p.payload[0].update(container_status="stopped",container_addr=[])
    service,row,_,_=schedule(p);actions=[]
    def action(profile,key,action,remote):
        actions.append(action)
        if action=="start":
            p.payload[0].update(container_status="available",container_addr=["10.0.1.5:8080"])
            return dict(owned=True,addresses=p.payload[0]["container_addr"])
        return dict(closed=True)
    monkeypatch.setattr(module,"platform_action",action)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();current=service.get(row["id"])
    assert actions==["start"]
    p.payload[0].update(is_completed=True,correct_flag_count=3)
    fake_run(p,current,"achieved");service.tick();service.tick()
    assert actions==["start","close"] and service.get(row["id"])["state"]["status"]=="finished"


def test_unknown_start_only_reads_and_does_not_claim_ownership(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];service,row,_,_=schedule(p);calls=[]
    def unknown(*args):calls.append(1);raise WorkbenchError("competition_timeout",502)
    monkeypatch.setattr(module,"platform_action",unknown)
    with pytest.raises(WorkbenchError,match="competition_platform_operation_unknown"):service.perform(row,"start","test-code")
    receipt=service.perform(row,"start","test-code")
    assert receipt["owned"] is False and len(calls)==1 and len(p.requests)==1


def test_unknown_operation_without_observable_result_is_never_replayed(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];p.payload[0].update(container_status="pending",container_addr=[])
    service,row,_,_=schedule(p);calls=[]
    def unknown(*args):calls.append(1);raise WorkbenchError("competition_timeout",502)
    monkeypatch.setattr(module,"platform_action",unknown)
    for _ in range(2):
        with pytest.raises(WorkbenchError,match="competition_platform_operation_unknown"):service.perform(row,"start","test-code")
    assert len(calls)==1


def test_overall_extension_is_idempotent_at_session_scope_and_checks_platform_cap(platform):
    p=platform;import time
    service,row,_,_=schedule(p,platform_deadline_at=time.time()+1900)
    command=dict(action="extend_overall",seconds=60,operation_id=str(uuid4()))
    one=service.command(row["id"],command)
    two=service.command(row["id"],command)
    assert one["state"]["deadline_at"]==row["state"]["deadline_at"]+60==two["state"]["deadline_at"]
    with pytest.raises(WorkbenchError,match="operator_operation_conflict"):service.command(row["id"],{**command,"seconds":61})
    with pytest.raises(WorkbenchError,match="competition_extension_exceeds_platform"):
        service.command(row["id"],{**command,"operation_id":str(uuid4()),"seconds":100})


def test_restart_keeps_session_paused_and_requires_credentials(platform):
    from agent_adapters.workbench import Workbench
    p=platform;service,row,_,_=schedule(p)
    restarted=Workbench(p.workbench.root,protocol_version=3,execution_profile="d8_v22")
    restored=restarted.schedules.get(row["id"])
    assert restored["state"]["status"]=="paused" and restarted.schedules.thread is None
    with pytest.raises(WorkbenchError,match="competition_credential_missing"):
        restarted.schedules.command(row["id"],dict(action="resume"))
    assert not p.requests


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_platform_end_is_trusted_failed_not_flag_rejected_and_unknown_blocks(http_case):
    from agent_adapters.competition_submission import end_unavailable
    from test_competition_bridge import binding
    c=http_case;configure(c)
    ended=lambda *args:dict(state="expired")
    result=end_unavailable(c.app,binding(c),"unused",str(uuid4()),read=ended)
    assert result["state"]=="expired" and c.app.state.task_run.status is TaskRunStatus.FAILED
    assert c.app.state.task_run.failure_reason=="competition_platform_expired"
    assert not c.app.store.list_d7_answer_candidates(c.task)


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_operator_cli_does_not_connect_docker_or_invoke_model(http_case,monkeypatch,capsys):
    from agent_adapters import cli,docker_profile
    c=http_case;configure(c)
    def forbidden(*a,**kw):raise AssertionError("operator command must not create runtime")
    monkeypatch.setattr(docker_profile,"connect",forbidden)
    operation=str(uuid4())
    code=cli.main(["operator-control","--run-dir",str(c.directory),"--task-run-id",c.task,
        "--operation-id",operation,"--action","pause"])
    output=json.loads(capsys.readouterr().out)
    assert code==0 and output["operator_control"]["paused"]
    assert not any(model.calls for model in c.models)


def test_startup_control_queue_is_durable_idempotent_and_pause_gate_precedes_start_lock(platform):
    from agent_adapters.run_files import immutable_write,immutable_read,run_lock
    from test_workbench_v3 import _challenge
    p=platform;hub=p.workbench
    challenge=hub.create(_challenge());root=hub.root/"initializing";root.mkdir()
    request=str(uuid4());immutable_write(root/"competition-binding.json",dict(request_id=request))
    hub._insert_run(str(uuid4()),challenge["id"],root)
    gate=root/"operator-start-controls";gate.mkdir()
    with run_lock(gate):
        result=hub.runner.operator_control(challenge["id"],"pause")
    assert result["pending"] and immutable_read(root/"operator-pause-request.json")["request_id"]==request
    identity=str(uuid4())
    one=hub.runner.operator_control(challenge["id"],"extend_question",seconds=60,operation_id=identity)
    two=hub.runner.operator_control(challenge["id"],"extend_question",seconds=60,operation_id=identity)
    assert one==two and len(list(gate.glob("*.json")))==1
    with pytest.raises(WorkbenchError,match="operator_operation_conflict"):
        hub.runner.operator_control(challenge["id"],"extend_question",seconds=61,operation_id=identity)


def test_arena_local_timeout_pauses_without_next_or_abandon(platform,monkeypatch):
    p=platform
    p.payload={"code":200,"data":{"agent":{},"attempt":{"id":123,"state_label":"active","remaining_seconds":3000,
        "problem":{"content":"Controlled target","container":{"url":["http://10.0.1.5:8080"]}}}}}
    service,row,_,_=schedule(p,"arena")
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();fake_run(p,service.get(row["id"]),"deadline_exceeded")
    service.tick();state=service.get(row["id"])["state"]
    assert state["status"]=="needs_attention" and state["reason"]=="competition_arena_local_deadline_active"
    assert all(method=="GET" for method,_,_ in p.requests)


def test_benchmark_queue_keeps_order_and_never_starts_two_questions(platform,monkeypatch):
    p=platform;p.payload=[benchmark(),{**benchmark(),"unique_code":"second"}]
    service,row,_,_=schedule(p,queue=["second","test-code"]);starts=[]
    def start(challenge,**kw):starts.append(challenge);fake_run(p,service.get(row["id"]))
    monkeypatch.setattr(p.workbench.runner,"start",start)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"resume",lambda _:None)
    service.tick();first=service.get(row["id"])
    assert first["state"]["current_ref"]=="second" and len(starts)==1
    service.tick();assert len(starts)==1
    p.payload[1].update(is_completed=True,correct_flag_count=3)
    fake_run(p,first,"achieved");service.tick();service.tick()
    assert service.get(row["id"])["state"]["current_ref"]=="test-code" and len(starts)==2


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_platform_expiry_checks_unknown_before_terminal_and_local_deadline_needs_no_api(http_case):
    from agent_adapters.competition_submission import end_unavailable
    from test_competition_bridge import binding
    c=http_case;configure(c)
    operation=D4OperationStart(str(uuid4()),c.task,"tool_dispatch",c.clock(),c.app.state.latest_checkpoint.sequence,
        "a"*64,"controlled",c.app.config.d4_budget.budget_policy_ref)
    c.app.store.begin_d4_operation(operation)
    with pytest.raises(ValueError,match="operator_unknown_operation"):
        end_unavailable(c.app,binding(c),"",str(uuid4()),read=lambda *a:dict(state="expired"))
    assert c.app.state.task_run.status is TaskRunStatus.RUNNING


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_operator_configured_overall_platform_cap_cannot_be_extended(http_case):
    c=http_case
    controls.submit(c.app.store,c.task,str(uuid4()),"configure",dict(question_seconds=600,
        overall_deadline_at=c.clock()+900,platform_deadline_at=None,overall_ceiling_at=c.clock()+1000))
    with pytest.raises(ValueError,match="operator_extension_exceeds_ceiling"):
        command(c,"extend_overall",dict(seconds=101))


@pytest.mark.parametrize("http_case",["continuous"],indirect=True)
def test_stale_expiry_cannot_finish_a_concurrently_extended_run(http_case):
    from agent_core.control_store import UpsertTaskRun,StaleControlStateError
    c=http_case;configure(c);original=c.app.state.task_run
    command(c,"extend_question",dict(seconds=60))
    change=UpsertTaskRun(original.task_run_id,original.request,original.effective_model_backend,
        original.started_at,original.deadline_at,TaskRunStatus.DEADLINE_EXCEEDED,sandbox_id=original.sandbox_id,
        finished_at=original.deadline_at,source_root=original.source_root,target_network=original.target_network)
    with pytest.raises(StaleControlStateError,match="deadline changed"):
        c.app.store._upsert_task_run(change)
    assert c.app.store.load_task_run(c.task).status is TaskRunStatus.RUNNING


def test_fixed_platform_operations_use_only_expected_routes_and_safe_fields(monkeypatch):
    from agent_adapters import competition
    profile=dict(adapter="benchmark",base_url="https://example.test",timeout_seconds=2)
    calls=[]
    def exchange(profile,key,method,path,payload=None):
        calls.append((method,path,payload))
        return dict(unique_code="test / code",container_addr=["10.0.1.5:8080"],closed=True,ignored="not forwarded")
    monkeypatch.setattr(competition,"_exchange",exchange)
    assert competition.platform_action(profile,"unused","start","test / code")==dict(addresses=["10.0.1.5:8080"],owned=True)
    assert competition.platform_action(profile,"unused","close","test / code")==dict(closed=True)
    assert calls==[("POST","/openapi/v1/challenges/start?unique_code=test+%2F+code",None),("POST","/openapi/v1/challenges/close?unique_code=test+%2F+code",None)]
    with pytest.raises(WorkbenchError,match="competition_platform_action_unsupported"):
        competition.platform_action(profile,"unused","hint","test / code")


def test_known_rejected_start_requires_explicit_resume_before_retry(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];p.payload[0].update(container_status="stopped",container_addr=[])
    service,row,_,_=schedule(p);calls=[]
    def denied(*args):calls.append(1);raise WorkbenchError("competition_auth_failed",502)
    monkeypatch.setattr(module,"platform_action",denied)
    for _ in range(2):
        with pytest.raises(WorkbenchError,match="competition_auth_failed"):service.perform(row,"start","test-code")
    assert len(calls)==1
    service.update(row["id"],current_ref="test-code",status="paused")
    service.command(row["id"],dict(action="resume"))
    with pytest.raises(WorkbenchError,match="competition_auth_failed"):service.perform(service.get(row["id"]),"start","test-code")
    assert len(calls)==2


def test_platform_success_during_inflight_submission_waits_for_its_receipt(platform,monkeypatch):
    from agent_adapters.workbench_runner import ActiveRun
    from test_workbench_v3 import _Process
    p=platform;p.payload=[benchmark()];service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();current=service.get(row["id"]);challenge=current["state"]["challenge_id"]
    run=p.workbench.runner.status(challenge)
    p.workbench._started_run(run["run_id"])
    p.workbench.runner._active[run["run_id"]]=ActiveRun(run["run_id"],challenge,__import__("pathlib").Path(run["directory"]),_Process(),operation="competition-submit")
    p.payload[0].update(is_completed=True,correct_flag_count=3)
    service.update(row["id"],last_platform_check=0)
    service.tick()
    assert service.get(row["id"])["state"]["status"]=="running"
    p.workbench.runner._active.clear()
    fake_run(p,current,"achieved");service.tick()
    assert service.get(row["id"])["state"]["current_ref"] is None


def test_overall_can_extend_after_current_terminal_without_reopening_it(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();current=service.get(row["id"]);commands=[]
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:dict(status="achieved",deadline_at=service.clock()+10,unknown=False,control=None))
    monkeypatch.setattr(p.workbench.runner,"operator_control",lambda *a,**kw:commands.append(a))
    result=service.command(row["id"],dict(action="extend_overall",seconds=60,operation_id=str(uuid4())))
    assert result["state"]["deadline_at"]==current["state"]["deadline_at"]+60 and not commands


def test_workbench_shutdown_pauses_scheduled_child_without_cancel_or_kill(platform):
    from test_workbench_v3 import _challenge,_Process
    from agent_adapters.workbench_runner import ActiveRun
    from agent_adapters.run_files import immutable_write
    from pathlib import Path
    p=platform;service,row,_,_=schedule(p);hub=p.workbench
    challenge=hub.create(_challenge());root=hub.root/"scheduled-child";root.mkdir()
    immutable_write(root/"competition-binding.json",dict(request_id=str(uuid4()),schedule={"session_id":row["id"]}))
    run=str(uuid4());hub._insert_run(run,challenge["id"],root)
    child=ActiveRun(run,challenge["id"],root,_Process());hub.runner._active[run]=child
    service.update(row["id"],challenge_id=challenge["id"])
    service.shutdown();hub.runner.shutdown()
    assert not child.cancel_requested and (root/"operator-pause-request.json").is_file()
    assert hub.runner._active[run] is child
    hub.runner._active.clear()


def test_waiting_for_attachment_can_extend_budgets_without_inventing_a_taskrun(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];p.payload[0]["container_addr"]=[]
    service,row,_,_=schedule(p)
    # This case covers the no-TaskRun window, not the address poll itself.
    monkeypatch.setattr(module,"ADDRESS_WAIT_SECONDS",0)
    def start_result(*args):return dict(owned=True,addresses=[])
    monkeypatch.setattr(module,"platform_action",start_result)
    service.tick();service.update(row["id"],last_platform_check=0);service.tick();current=service.get(row["id"])
    challenge=current["state"]["challenge_id"]
    assert challenge is not None and p.workbench.runner.status(challenge) is None
    global_command=dict(action="extend_overall",seconds=60,operation_id=str(uuid4()))
    after=service.command(row["id"],global_command)
    assert after["state"]["deadline_at"]==current["state"]["deadline_at"]+60
    question=dict(action="extend_question",seconds=60,operation_id=str(uuid4()))
    after=service.command(row["id"],question)
    assert after["state"]["question_seconds"]==660 and service.command(row["id"],question)["state"]["question_seconds"]==660
    assert p.workbench.runner.status(challenge) is None


@pytest.mark.parametrize("retry",[False,True])
def test_arena_attachment_is_downloaded_before_scheduled_start_and_failure_can_resume(platform,monkeypatch,retry):
    from test_competition import arena_attachment
    p=platform;body=arena_attachment(p)
    service,row,_,_=schedule(p,"arena")
    starts=[]
    def start(identity,**options):
        task=p.workbench.get(identity);file,=task["attachments"]
        assert (p.workbench.uploads/identity/file["id"]).read_bytes()==body
        assert options["competition_control"]["session_id"]==row["id"]
        starts.append(identity)
        fake_run(p,service.get(row["id"]))
    monkeypatch.setattr(p.workbench.runner,"start",start)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    if retry:
        p.routes["/archive"]=(403,b"controlled download refusal",{})
        service.tick();service.tick();state=service.get(row["id"])["state"]
        assert state["status"]=="paused" and state["reason"]=="competition_attachment_http_403" and not starts
        task,=p.workbench.list();assert not task["attachments"]
        p.routes["/archive"]=(200,body,{})
        service.command(row["id"],{"action":"resume"})
    service.tick()
    state=service.get(row["id"])["state"]
    assert len(starts)==1 and state["challenge_id"]==starts[0] and state["status"]=="running"
    assert len(p.workbench.list())==1
    assert all(method=="GET" for method,_,_ in p.requests)


def test_pause_after_attachment_download_prevents_registration_and_agent_start(platform,monkeypatch):
    from contextlib import contextmanager
    from test_competition import arena_attachment
    from agent_adapters import competition
    p=platform;arena_attachment(p)
    service,row,_,_=schedule(p,"arena")
    original=competition._download_attachment
    @contextmanager
    def paused(*args,**kwargs):
        with original(*args,**kwargs) as result:
            service.pause(row["id"])
            yield result
    monkeypatch.setattr(competition,"_download_attachment",paused)
    starts=[]
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:starts.append(a))
    service.tick();service.tick()
    assert not starts and service.get(row["id"])["state"]["status"]=="paused"
    task,=p.workbench.list();assert not task["attachments"]


def test_paused_session_finishes_at_overall_deadline_once_current_task_is_terminal(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();service.update(row["id"],status="paused")
    service.clock=lambda:row["state"]["deadline_at"]
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:dict(status="deadline_exceeded",unknown=False))
    service.tick()
    assert service.get(row["id"])["state"]["status"]=="finished" and not service.has_active()


def test_failed_terminal_reconciliation_does_not_spawn_again_without_explicit_resume(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();current=service.get(row["id"])
    fake_run(p,current,"running",recovery=dict(required=True,reason="competition_end_recovery_required"))
    calls=[];monkeypatch.setattr(p.workbench.runner,"competition_end",lambda _:calls.append(1))
    service.tick();service.tick()
    assert service.get(row["id"])["state"]["status"]=="needs_attention" and not calls


def test_worker_lifetime_lock_covers_idle_dispatch_and_stdout_flush(tmp_path,monkeypatch):
    import io
    from agent_adapters import cli
    from agent_adapters.run_files import immutable_write,run_lock
    root=tmp_path/"launcher";root.mkdir();state=root/"agent-state";state.mkdir()
    immutable_write(root/"competition-binding.json",dict(schedule={"session_id":str(uuid4())}))
    checks=[]
    def verify_lock():
        with pytest.raises(ValueError,match="task_run_busy"):
            with run_lock(root,lock_name="worker.lock"):pass
        checks.append(True)
    def execute(args):verify_lock();return dict(exit_code=0)
    class Output(io.StringIO):
        def flush(self):verify_lock();super().flush()
    output=Output()
    monkeypatch.setattr(cli,"dispatch",execute);monkeypatch.setattr(cli.sys,"stdout",output)
    assert cli.main(["run-task","--request",str(root/"request.json"),"--state-dir",str(state),"--mode","simulated"])==0
    assert len(checks)==2 and json.loads(output.getvalue())["exit_code"]==0
    with run_lock(root,lock_name="worker.lock"):pass


def test_restart_cannot_resume_idle_worker_just_because_dispatch_lock_is_free(platform):
    from agent_adapters.run_files import immutable_write,run_lock
    from test_workbench_v3 import _challenge
    p=platform;hub=p.workbench;challenge=hub.create(_challenge())
    root=hub.root/"idle-worker";root.mkdir();task=str(uuid4());agent=root/"agent-state"/task;agent.mkdir(parents=True)
    immutable_write(agent/"run.json",dict(task_run_id=task,protocol_version=3))
    hub._insert_run(str(uuid4()),challenge["id"],root)
    with run_lock(root,lock_name="worker.lock"):
        with pytest.raises(WorkbenchError,match="competition_still_pausing"):
            hub.runner.reconcile_stopped_process(challenge["id"])
    assert hub.runner.status(challenge["id"])["status"]=="starting"


def test_restart_requests_pause_before_any_explicit_continuation(platform,monkeypatch):
    from agent_adapters.workbench import Workbench
    from pathlib import Path
    p=platform;p.payload=[benchmark()];service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"start",lambda *a,**kw:fake_run(p,service.get(row["id"])))
    service.tick();current=service.get(row["id"]);run=p.workbench.runner.status(current["state"]["challenge_id"])
    before=len(p.requests)
    restarted=Workbench(p.workbench.root,protocol_version=3,execution_profile="d8_v22")
    assert (Path(run["directory"])/"operator-pause-request.json").is_file()
    assert restarted.schedules.get(row["id"])["state"]["status"]=="paused"
    assert restarted.schedules.thread is None and len(p.requests)==before


def test_address_wait_only_covers_live_web_or_pwn_questions_without_addresses():
    web=dict(state="active",addresses=[],remaining_seconds=600,category="Web")
    assert module._should_wait_address(web,"arena") and module._should_wait_address({**web,"category":"Pwn"},"arena")
    assert not module._should_wait_address({**web,"category":"Reverse"},"arena")
    assert not module._should_wait_address({**web,"addresses":["10.0.1.5:8080"]},"arena")
    assert not module._should_wait_address({**web,"remaining_seconds":0},"arena")
    assert not module._should_wait_address({**web,"state":"solved"},"arena")
    assert module._should_wait_address(dict(state="available",addresses=[],remaining_seconds=None),"benchmark")
    assert not module._should_wait_address(dict(state="pending",addresses=[],remaining_seconds=None),"benchmark")


def test_arena_pwn_question_waits_for_the_late_address_instead_of_starting_on_empty_target(platform,monkeypatch):
    p=platform;now=[1000.0];starts=[]
    p.payload={"code":200,"data":{"agent":{},"attempt":{"id":11939,"state_label":"active","wrong_count":0,
        "max_wrong_count":20,"remaining_seconds":600,
        "problem":{"type_label":"Pwn","content":"controlled","container":None}}}}
    service,row,_,_=schedule(p,"arena")
    service.clock=lambda:now[0]
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:starts.append(challenge))
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    service.tick();state=service.get(row["id"])["state"]
    assert state["address_wait_started"]==1000.0 and state["challenge_id"] is None and not starts and not p.workbench.list()
    now[0]+=10;service.tick()
    assert service.get(row["id"])["state"]["address_wait_started"]==1000.0 and not starts
    p.payload["data"]["attempt"]["problem"]["container"]={"url":["10.0.1.5:8080"]}
    now[0]+=5;service.tick()
    current=service.get(row["id"])["state"];challenge=current["challenge_id"]
    assert challenge is not None and current.get("address_wait_started") is None
    assert p.workbench.get(challenge)["endpoint"]=="tcp://10.0.1.5:8080"
    assert starts==[challenge] and current["status"]=="running"


def test_address_wait_times_out_on_the_existing_no_target_path(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];p.payload[0]["container_addr"]=[]
    service,row,_,_=schedule(p);now=[1000.0];service.clock=lambda:now[0]
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    service.tick();state=service.get(row["id"])["state"]
    assert state["address_wait_started"]==1000.0 and state["challenge_id"] is None
    now[0]+=31;service.tick();service.tick()
    state=service.get(row["id"])["state"]
    assert state["address_wait_timeout_at"]==now[0] and state["status"]=="paused"
    assert state["reason"]=="competition_question_input_required"


def reopen(platform,row):
    value=dict(profile_id=row["config"]["profile"]["id"],default_scheme="http",question_minutes=10,
        overall_minutes=30,queue=["test-code"],http_methods=["GET","HEAD","OPTIONS"],mode="real")
    code,second=call(platform.port,"POST","/api/competition/sessions",value)
    assert code==201,second
    return platform.workbench.schedules,second


def test_finish_releases_the_paused_question_so_the_same_platform_question_can_run_again(platform,monkeypatch):
    p=platform;p.payload=[benchmark()]
    service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick();challenge=service.get(row["id"])["state"]["challenge_id"]
    assert challenge is not None and p.workbench.get(challenge)["status"]=="paused"
    service.command(row["id"],dict(action="finish"))
    assert service.get(row["id"])["state"]["status"]=="finished"
    released=p.workbench.get(challenge)
    assert released["status"]=="draft" and released["run"]["status"]=="cancelled" and released["run"]["started_at"]
    again,second=reopen(p,row);starts=[]
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:starts.append(challenge))
    again.tick()
    state=again.get(second["id"])["state"]
    assert starts==[challenge] and state["status"]=="running" and state["challenge_id"]==challenge


def test_deleted_local_question_does_not_pin_the_platform_question(platform,monkeypatch):
    import sqlite3
    p=platform;p.payload=[benchmark()]
    service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick();challenge=service.get(row["id"])["state"]["challenge_id"]
    service.command(row["id"],dict(action="finish"))
    # Deleting the question without a cascade leaves the platform binding behind.
    con=sqlite3.connect(p.workbench.database)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DELETE FROM runs WHERE challenge_id=?",(challenge,))
        con.execute("DELETE FROM challenges WHERE id=?",(challenge,))
        con.commit()
    finally:con.close()
    again,second=reopen(p,row);starts=[]
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:starts.append(challenge))
    again.tick()
    state=again.get(second["id"])["state"]
    assert state["status"]=="running" and state["challenge_id"] not in (None,challenge)
    assert starts==[state["challenge_id"]] and p.workbench.get(state["challenge_id"])["endpoint"]=="http://10.0.1.5:8080"


def test_restart_releases_the_question_left_paused_by_an_already_finished_session(platform,monkeypatch):
    import sqlite3
    from agent_adapters.workbench import Workbench
    p=platform;p.payload=[benchmark()]
    service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick();challenge=service.get(row["id"])["state"]["challenge_id"]
    # Older builds only flipped the session to finished and left the question paused.
    service.update(row["id"],status="finished",reason="operator_finished")
    assert p.workbench.get(challenge)["status"]=="paused"
    restarted=Workbench(p.workbench.root,protocol_version=3,execution_profile="d8_v22")
    released=restarted.get(challenge)
    assert released["status"]=="draft" and released["run"]["status"]=="cancelled"


def test_purging_the_question_detaches_it_from_every_session(platform,monkeypatch):
    p=platform;p.payload=[benchmark()];p.payload[0]["container_addr"]=[]
    service,row,_,_=schedule(p)
    monkeypatch.setattr(module,"ADDRESS_WAIT_SECONDS",0)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    service.tick();service.update(row["id"],last_platform_check=0);service.tick()
    challenge=service.get(row["id"])["state"]["challenge_id"]
    assert challenge is not None and service.get(row["id"])["state"]["status"]=="pausing"
    p.workbench.archive(challenge)
    p.workbench.purge(challenge,"test-code")
    state=service.get(row["id"])["state"]
    assert state["challenge_id"] is None and state["status"]=="paused"
    assert state["reason"]=="competition_question_deleted" and all(t["id"]!=challenge for t in p.workbench.list())


def test_restart_drops_a_session_reference_to_a_deleted_question(platform,monkeypatch):
    from agent_adapters.workbench import Workbench
    p=platform;p.payload=[benchmark()]
    service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick();challenge=service.get(row["id"])["state"]["challenge_id"]
    service.update(row["id"],status="finished",reason="operator_finished")
    with p.workbench._db() as db:
        db.execute("DELETE FROM runs WHERE challenge_id=?",(challenge,))
        db.execute("DELETE FROM challenges WHERE id=?",(challenge,))
    assert service.get(row["id"])["state"]["challenge_id"]==challenge
    restarted=Workbench(p.workbench.root,protocol_version=3,execution_profile="d8_v22")
    assert restarted.schedules.get(row["id"])["state"]["challenge_id"] is None


def test_arena_business_refusal_is_blocked_retryable_and_keeps_the_platform_code(platform,monkeypatch):
    p=platform;p.bodies=[]
    p.payload={"code":200,"data":{"agent":{},"attempt":None}}
    p.post_payload={"code":400,"message":"controlled refusal"}
    service,row,_,_=schedule(p,"arena")
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    service.tick();service.tick()
    state=service.get(row["id"])["state"]
    assert state["status"]=="paused" and state["reason"]=="competition_business_error"
    with p.workbench._db() as db:
        receipt=json.loads(db.execute("SELECT receipt_json FROM competition_platform_operations "
            "WHERE action='next' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert receipt=={"state":"blocked","code":"competition_business_error","platform_code":400}
    before=[r for r in p.requests if r[1]=="/api/skill/agent/arena/next/"]
    service.command(row["id"],dict(action="resume"));service.tick();service.tick()
    after=[r for r in p.requests if r[1]=="/api/skill/agent/arena/next/"]
    assert len(after)==len(before)+1
    assert service.get(row["id"])["state"]["reason"]=="competition_business_error"


def test_arena_next_refusal_falls_back_to_the_attempt_current_reports(platform,monkeypatch):
    p=platform;p.bodies=[]
    attempt={"id":12099,"state_label":"active","wrong_count":0,"max_wrong_count":20,"remaining_seconds":600,
        "problem":{"type_label":"Web","content":"controlled","container":{"url":["10.0.1.5:8080"]}}}
    p.payload={"code":200,"data":{"agent":{},"attempt":attempt}}
    p.post_payload={"code":400,"message":"controlled refusal"}
    service,row,_,_=schedule(p,"arena")
    real_read=module.read_question;calls=[]
    def first_empty(profile,credential,remote=None):
        calls.append(remote)
        return None if len(calls)==1 else real_read(profile,credential,remote)
    monkeypatch.setattr(module,"read_question",first_empty)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    starts=[]
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:starts.append(challenge))
    service.tick()
    state=service.get(row["id"])["state"]
    assert state["status"]=="running" and starts==[state["challenge_id"]]
    assert p.workbench.get(state["challenge_id"])["endpoint"]=="http://10.0.1.5:8080"


def test_finish_abandons_the_open_arena_attempt_before_ending(platform,monkeypatch):
    p=platform;p.bodies=[]
    p.payload={"code":200,"data":{"agent":{},"attempt":{"id":12041,"state_label":"active","wrong_count":0,
        "max_wrong_count":20,"remaining_seconds":600,
        "problem":{"type_label":"Pwn","content":"controlled","container":{"url":["10.0.1.5:8080"]}}}}}
    p.post_payload={"code":200,"data":{"attempt":{"id":12041,"state_label":"abandoned"}}}
    service,row,_,_=schedule(p,"arena")
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick();challenge=service.get(row["id"])["state"]["challenge_id"]
    assert challenge is not None and p.workbench.get(challenge)["status"]=="paused"
    service.command(row["id"],dict(action="finish"))
    assert ("POST","/api/skill/agent/arena/attempt/12041/abandon/") in [(m,path) for m,path,_ in p.requests]
    state=service.get(row["id"])["state"]
    assert state["status"]=="finished" and state["question_abandoned"]=="abandoned"
    assert p.workbench.get(challenge)["run"]["status"]=="cancelled"


def test_finish_still_ends_the_session_when_the_platform_refuses_to_abandon(platform,monkeypatch):
    p=platform;p.bodies=[]
    p.payload={"code":200,"data":{"agent":{},"attempt":{"id":12041,"state_label":"active","wrong_count":0,
        "max_wrong_count":20,"remaining_seconds":600,
        "problem":{"type_label":"Pwn","content":"controlled","container":{"url":["10.0.1.5:8080"]}}}}}
    p.post_payload={"code":400,"message":"controlled refusal"}
    service,row,_,_=schedule(p,"arena")
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick()
    service.command(row["id"],dict(action="finish"))
    state=service.get(row["id"])["state"]
    assert state["status"]=="finished" and state["question_abandoned"]=="competition_business_error"


def test_benchmark_finish_never_calls_the_arena_abandon_endpoint(platform,monkeypatch):
    p=platform;p.payload=[benchmark()]
    service,row,_,_=schedule(p)
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick();service.command(row["id"],dict(action="finish"))
    state=service.get(row["id"])["state"]
    assert state["status"]=="finished" and state["question_abandoned"] is None
    assert all("abandon" not in path for _,path,_ in p.requests)

def test_finish_treats_a_missing_abandon_endpoint_as_unsupported(platform,monkeypatch):
    p=platform;p.bodies=[]
    p.payload={"code":200,"data":{"agent":{},"attempt":{"id":12041,"state_label":"active","wrong_count":0,
        "max_wrong_count":20,"remaining_seconds":600,
        "problem":{"type_label":"Pwn","content":"controlled","container":{"url":["10.0.1.5:8080"]}}}}}
    p.post_payload={"error":"not found"};p.post_status=404
    service,row,_,_=schedule(p,"arena")
    monkeypatch.setattr(p.workbench.runner,"runtime_control",lambda _:None)
    monkeypatch.setattr(p.workbench.runner,"start",lambda challenge,**options:fake_run(p,service.get(row["id"])))
    service.tick()
    service.command(row["id"],dict(action="finish"))
    state=service.get(row["id"])["state"]
    assert state["status"]=="finished" and state["question_abandoned"]=="unsupported"



def test_arena_pwn_prefers_tcp_while_other_categories_keep_the_session_scheme():
    assert module._prefers_tcp({"category":"Pwn"},"arena")
    assert module._prefers_tcp({"category":"pwn"},"arena")
    assert not module._prefers_tcp({"category":"Web"},"arena")
    assert not module._prefers_tcp({"category":"Pwn"},"benchmark")
    assert not module._prefers_tcp({},"arena")


def test_tcp_origin_is_canonical_and_requires_a_port():
    from agent_adapters.competition import _target_origin
    assert _target_origin("tcp://10.0.1.5:8080")=="tcp://10.0.1.5:8080"
    with pytest.raises(ValueError):
        _target_origin("tcp://10.0.1.5")
    with pytest.raises(ValueError):
        _target_origin("tcp://10.0.1.5:8080/path")
