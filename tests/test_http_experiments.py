"""Real scheduler/evidence with local script and transport substitutes; no Docker."""
import asyncio
import base64
import io
import json
import queue
import threading

import pytest
from test_http import http_case
from test_exploration_loop import install
from test_mcp_link import Tools
from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_adapters.http_experiments import TOOL
from agent_core.dual_llm_contracts import D3RecoveryRequired


class ScriptProcess:
    """Execute only fixed test programs, not model/user input, on the test host."""
    def __init__(self, sandbox, task, sandbox_id, script, timeout):
        self.events,self.replies=queue.Queue(),queue.Queue()
        self.closed=False
        self.exec_id='local-script-substitute'
        self.events.put(dict(kind='ready',pid=123))
        def request(method,path_query,headers=None,body=''):
            self.events.put(dict(kind='request',method=method,path_query=path_query,headers=headers or {},body=body))
            value=self.replies.get(timeout=15)
            if value is None:raise RuntimeError('closed')
            value={**value,'body':base64.b64decode(value['body_b64'])}
            value['text']=value['body'].decode()
            return value
        def run():
            output=io.StringIO()
            try:
                # A per-script print avoids process-global stdout redirection.
                exec(script,{'request':request,'print':lambda *args:print(*args,file=output)})
                self.events.put(dict(kind='finished',status='completed',output=output.getvalue()))
            except Exception:
                self.events.put(dict(kind='finished',status='failed',output=output.getvalue()))
        self.thread=threading.Thread(target=run,daemon=True)
        self.thread.start()
    def receive(self):return self.events.get(timeout=15)
    def send(self,value):self.replies.put(value)
    def close(self):
        self.closed=True
        self.replies.put(None)
        self.thread.join(2)
        assert not self.thread.is_alive()


def setup(case,script,*,fact=False):
    install(case,steps=0,stop=None if fact else 'inconclusive')
    case.tools=Tools(case.task,case.app.state.task_run.sandbox_id)
    case.app.mcp=MCPPlanExecutor(case.app,case.tools)
    case.app.mcp.authorize(case.task)
    case.app.mcp.experiments.runner_factory=ScriptProcess
    previous=case.models[1].invoke
    called=[]
    def choose(envelope,*args):
        if envelope.output_contract['stage']=='inner_candidate':
            return dict(action=dict(kind='no_candidate'))
        if envelope.output_contract['stage']=='inner_plan' and not called:
            called.append(envelope.request.model_request_id)
            return dict(kind='mcp_call',tool_name=TOOL,arguments=dict(script=script,timeout_seconds=60))
        return previous(envelope,*args)
    case.models[1].invoke=choose
    return called


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_one_model_selection_drives_conditional_requests_and_reviewed_fact(http_case):
    c=http_case
    c.response_headers.append(('Set-Cookie','session=local; Path=/'))
    calls=setup(c,"""first=request('GET','/baseline')
if first['status']==200:
    for i in range(3):
        response=request('GET','/variant?i='+str(i))
        assert response['body']==first['body']
print('Compared baseline and three variants.')
""",fact=True)
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert len(calls)==1 and len(c.sends)==4
    assert all(any(k.lower()=='cookie' for k,v in send['headers']) for send in c.sends[1:])
    report=c.app.mcp.experiments.result(calls[0])
    assert report['status']=='completed' and len(report['requests'])==4
    assert len(c.app.state.facts)==1
    for ref in report['requests']:
        source=c.app.evidence_conclusions.source(ref,c.app.state.intents[0].intent_id)
        assert source['data']['result']['http_response']['status']==200
        assert source['data']['call']['arguments']['path_query'].startswith(('/baseline','/variant'))
    assert len(c.app.sessions.execution_refs(c.app.state.intents[0].intent_id))==4
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot=snapshot_v3_task_result(c.directory,c.declaration)
    assert not snapshot['errors'] and len(snapshot['verified_facts'])==1,snapshot


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('tail',["request('POST','/forbidden')","request('GET','https://other.test/')","raise RuntimeError('local failure')"])
def test_failed_experiment_preserves_first_request_without_replay(http_case,tail):
    c=http_case
    calls=setup(c,"request('GET','/first')\n"+tail)
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    report=c.app.mcp.experiments.result(calls[0])
    assert report['status'] in {'failed','rejected'} and len(c.sends)==1 and len(report['requests'])==1
    c.app.mcp.reconcile()
    assert len(c.sends)==1 and not c.app.state.facts
    from agent_core.actions import MCPCall
    saved=c.app.mcp.experiments.evidence.read('experiment-session',calls[0])['call']
    asyncio.run(c.app.mcp.execute(calls[0],c.app.state.intents[0].intent_id,MCPCall(**saved),c.token))
    assert len(c.sends)==1


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_lost_but_confirmed_exited_script_can_return_partial_progress_without_replay(http_case,monkeypatch):
    from types import SimpleNamespace as NS
    c=http_case
    calls=setup(c,"request('GET','/once')")
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    read=c.app.output_boundary.evidence.read
    write=c.app.output_boundary.evidence.save
    recovered={}
    monkeypatch.setattr(c.app.output_boundary.evidence,'read',lambda kind,key:
        recovered.get(key) if kind=='experiment-result' else read(kind,key))
    def save(kind,key,value):
        if kind=='experiment-result':recovered[key]=value
        else:write(kind,key,value)
    monkeypatch.setattr(c.app.output_boundary.evidence,'save',save)
    c.app.http_sandbox._runtime=NS(client=NS(api=NS(exec_inspect=lambda key:
        dict(ContainerID=c.app.state.task_run.sandbox_id,Running=False))))
    c.app.mcp.reconcile()
    assert len(c.sends)==1 and recovered[calls[0]]['status']=='interrupted'
    assert len(recovered[calls[0]]['requests'])==1


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_strategy_stop_between_requests_preserves_four_observations(http_case):
    c=http_case
    calls=setup(c,"for i in range(6):\n    request('GET','/repeat')")
    outer=c.models[0].invoke
    start=c.app.supervision.start
    def review(envelope,*args):
        if envelope.output_contract['stage']=='strategy_supervision':
            return dict(decision='request_intent_resolution',reason='The recorded repeats no longer distinguish this hypothesis.',
                evidence_refs=[envelope.request.history[-1].ref],suggestion=None)
        return outer(envelope,*args)
    def wait_review(intent):
        start(intent)
        assert c.app.supervision.pending[1].wait(3)
        c.app.supervision.poll()
    c.models[0].invoke,c.app.supervision.start=review,wait_review
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    report=c.app.mcp.experiments.result(calls[0])
    assert report['status']=='stopped' and len(c.sends)==4
    assert len(c.app.sessions.execution_refs(c.app.state.intents[0].intent_id))==4


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_unknown_request_is_not_replayed_or_reported_as_local_script_failure(http_case):
    c=http_case
    calls=setup(c,"request('GET','/first')\nrequest('GET','/uncertain')")
    class Transport:
        def send(self,**kw):
            from agent_adapters.web.http_adapter import D7HttpWireResponse
            c.sends.append(kw)
            if len(c.sends)==2:raise ConnectionResetError('controlled transport interruption')
            return D7HttpWireResponse(200,(),b'first')
    c.app.web.transport_factory=lambda _:Transport()
    result=asyncio.run(c.app._run_once(c.token))
    assert result.recovery_required and len(c.sends)==2,result
    assert c.app.mcp.experiments.result(calls[0]) is None
    assert len([r for r in c.app.mcp.records() if r.completion is not None])==1
    with pytest.raises(D3RecoveryRequired):c.app.mcp.reconcile()
    assert len(c.sends)==2


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_background_stop_interrupts_local_compute_without_waiting_for_next_request(http_case):
    c=http_case
    calls=setup(c,"for i in range(4):\n    request('GET','/repeat')")
    computing=threading.Event()
    class Computing(ScriptProcess):
        def __init__(self,*args):
            self.stopped=threading.Event()
            super().__init__(*args)
        def receive(self):
            event=super().receive()
            if event['kind']=='finished':
                computing.set()
                assert self.stopped.wait(5)
            return event
        def close(self):
            self.stopped.set()
            super().close()
    c.app.mcp.experiments.runner_factory=Computing
    outer=c.models[0].invoke
    def review(envelope,*args):
        if envelope.output_contract['stage']=='strategy_supervision':
            assert computing.wait(3)
            return dict(decision='request_intent_resolution',reason='The four repetitions are sufficient to review this scope.',
                evidence_refs=[envelope.request.history[-1].ref],suggestion=None)
        return outer(envelope,*args)
    c.models[0].invoke=review
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert len(c.sends)==4 and c.app.mcp.experiments.result(calls[0])['code']=='d22_experiment_strategy_stop'


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_deadline_can_close_networkless_script_after_preserving_completed_request(http_case):
    c=http_case
    calls=setup(c,"request('GET','/first')\nrequest('GET','/not-admitted')")
    class Deadline(ScriptProcess):
        def send(self,value):
            super().send(value)
            c.clock.now=c.app.state.task_run.deadline_at
    c.app.mcp.experiments.runner_factory=Deadline
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert c.app.state.task_run.status.value=='deadline_exceeded' and len(c.sends)==1
    assert len(c.app.mcp.experiments.result(calls[0])['requests'])==1


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_unavailable_process_isolation_never_falls_back_to_shell_or_sends(http_case):
    c=http_case
    calls=setup(c,"request('GET','/should-not-send')")
    closed=[]
    class Unavailable:
        def __init__(self,*args):pass
        def receive(self):return dict(kind='unavailable',code='d22_experiment_isolation_unavailable')
        def close(self):closed.append(True)
    c.app.mcp.experiments.runner_factory=Unavailable
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert not c.sends and not c.tools.calls and closed
    assert c.app.mcp.experiments.result(calls[0])['code']=='d22_experiment_isolation_unavailable'


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_lost_session_completion_is_not_reexecuted_and_corrupt_response_is_rejected(http_case,monkeypatch):
    c=http_case
    calls=setup(c,"request('GET','/once')")
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    report=c.app.mcp.experiments.result(calls[0])
    ref=report['requests'][0]
    body_id=c.app.mcp.feedback(ref)['http_response']['body_evidence_id']
    read=c.app.web.protected.read
    with monkeypatch.context() as patch:
        patch.setattr(c.app.web.protected,'read',lambda key:b'corrupted' if key==body_id else read(key))
        with pytest.raises(D3RecoveryRequired,match='source_changed'):
            c.app.evidence_conclusions.source(ref,c.app.state.intents[0].intent_id)
    read=c.app.output_boundary.evidence.read
    monkeypatch.setattr(c.app.output_boundary.evidence,'read',lambda kind,key:None if kind=='experiment-result' else read(kind,key))
    with pytest.raises(D3RecoveryRequired,match='process_reconciliation_required'):
        c.app.mcp.reconcile()
    assert len(c.sends)==1


@pytest.mark.skipif(__import__('sys').platform!='linux',reason='Linux seccomp integration requires Linux; Docker acceptance remains external')
def test_actual_worker_cannot_open_socket_but_can_compute():
    import subprocess,sys
    from Sandbox.http_experiment import WORKER
    program="import socket\ntry:\n socket.socket()\nexcept PermissionError:\n print('network denied')\nelse:\n raise RuntimeError('network escaped')"
    result=subprocess.run([sys.executable,'-I','-c',WORKER],input=json.dumps({'script':program})+'\n',
        capture_output=True,text=True,timeout=5)
    values=[json.loads(line) for line in result.stdout.splitlines()]
    assert values[0]['kind']=='ready' and values[-1]['status']=='completed'
    assert 'network denied' in values[-1]['output']


@pytest.mark.skipif(__import__('sys').platform=='linux',reason='Non-Linux failure path only')
def test_actual_worker_does_not_execute_script_when_filter_is_unavailable():
    import subprocess,sys
    from Sandbox.http_experiment import WORKER
    result=subprocess.run([sys.executable,'-I','-c',WORKER],input=json.dumps({'script':"raise SystemExit('must not execute')"})+'\n',
        capture_output=True,text=True,timeout=5)
    assert result.returncode==2 and not result.stderr
    assert json.loads(result.stdout)==dict(kind='unavailable',code='d22_experiment_isolation_unavailable')


@pytest.mark.parametrize('malformed',[False,True])
def test_container_bridge_frames_use_existing_container_and_close_exact_process(monkeypatch,malformed):
    from contextlib import nullcontext
    from types import SimpleNamespace as NS
    from test_sandbox_http import _RawSocket,_frame
    from Sandbox.http_experiment import HttpExperimentProcess
    import docker.utils.socket
    monkeypatch.setattr(docker.utils.socket,'read',lambda sock,n=4096:sock.recv(n))
    frames=_frame(1,b'{"kind":"ready","pid":4321}\n')
    frames+=_frame(2,b'bad stderr') if malformed else _frame(1,b'{"kind":"request","method":"GET","path_query":"/","headers":{},"body":""}\n')
    raw=_RawSocket(frames)
    calls=[]
    class API:
        running=True
        def exec_create(self,**kwargs):
            calls.append(kwargs)
            return {'Id':'worker' if kwargs['cmd'][0]=='timeout' else 'kill'}
        def exec_start(self,identity,**kwargs):
            if identity=='kill':self.running=False;return b''
            return raw
        def exec_inspect(self,identity):return {'Running':False,'ExitCode':0} if identity=='kill' else {'Running':self.running,'ExitCode':137 if not self.running else None}
    api=API()
    sandbox=NS(_runtime=NS(client=NS(api=api)),_binding=lambda identity:object(),
        _verify_active=lambda *args:None,_admit=lambda binding:nullcontext())
    process=HttpExperimentProcess(sandbox,'task','same-container','print(1)',30)
    assert process.receive()==dict(kind='ready',pid=4321)
    if malformed:
        with pytest.raises(RuntimeError,match='bridge_invalid'):process.receive()
    else:
        assert process.receive()['kind']=='request'
        process.send(dict(status=200,body_b64='',evidence_ref='ref'))
    process.close()
    assert calls[0]['container']=='same-container' and calls[0]['user']=='10001:10001'
    assert calls[0]['environment']=={'PATH':'/usr/bin:/bin'} and calls[0]['privileged'] is False
    assert calls[-1]['cmd'][:3]==['python3','-I','-c'] and calls[-1]['cmd'][-1]=='4321'
    assert not api.running and process.stop_diagnostic['signal_exit_code']==0
    assert process.stop_diagnostic['process_stopped'] is True
    assert b'"script": "print(1)"' in raw.sent


@pytest.mark.parametrize('http_case', ['continuous'], indirect=True)
@pytest.mark.parametrize('tail', ["print('A finite comparison, not a verified conclusion.')", "raise RuntimeError('local measurement failed')"])
def test_supervision_counts_individual_batch_requests_and_keeps_script_outcome(http_case, tail, monkeypatch):
    c = http_case
    calls = setup(c, "for i in range(6):\n    request('GET', '/same' if i < 3 else '/other')\n" + tail)
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    intent = c.app.state.intents[0].intent_id
    progress = c.app.supervision.exploration_progress(intent)
    assert progress['http_request_count'] == progress['completed_tool_count'] == 6
    assert progress['experiment_session_count'] == 1
    assert sorted(len(g['operation_ids']) for g in progress['request_shape_groups']) == [3, 3]
    assert len(progress['response_groups']) == 1
    assert len(progress['response_groups'][0]['operation_ids']) == 6
    assert len(progress['recent_tools']) == 4
    assert all(t['result']['http_response'] for t in progress['recent_tools'])
    assert progress['experiment_results'][0]['status'] == ('failed' if tail.startswith('raise') else 'completed')
    assert not progress['known_failed_or_rejected']  # Script failure is not a failed HTTP observation.
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    assert len(c.sends) == 6 and len(calls) == 1
    read = c.app.mcp.evidence.read
    monkeypatch.setattr(c.app.mcp.evidence, 'read', lambda kind, identity: None if kind == 'mcp-result' else read(kind, identity))
    with pytest.raises(D3RecoveryRequired, match='mcp.result_binding_changed'):
        c.app.supervision.exploration_progress(intent)


@pytest.mark.parametrize('http_case', ['continuous'], indirect=True)
def test_current_review_drains_inflight_response_and_blocks_next_experiment_request(http_case):
    from agent_adapters.web.http_adapter import D7HttpWireResponse
    c = http_case
    calls = setup(c, "for i in range(6):\n    request('GET', '/variant?i='+str(i))")
    fifth_started, review_applied = threading.Event(), threading.Event()
    outer, apply = c.models[0].invoke, c.app.supervision.apply
    def applied(request, result):
        value = apply(request, result)
        if result['outcome'] == 'valid' and result['raw']['decision'] == 'request_intent_resolution':
            review_applied.set()
        return value
    class Transport:
        def send(self, **kwargs):
            c.sends.append(kwargs)
            if len(c.sends) == 5:
                fifth_started.set()
                assert review_applied.wait(5)
            return D7HttpWireResponse(200, (), b'new control result' if len(c.sends) == 5 else b'baseline')
    def review(envelope, *args):
        if envelope.output_contract['stage'] == 'strategy_supervision':
            assert fifth_started.wait(5)
            return dict(decision='request_intent_resolution', reason='Review the diminishing value against the pending control.',
                evidence_refs=[envelope.request.history[-1].ref], suggestion=None)
        return outer(envelope, *args)
    c.models[0].invoke, c.app.supervision.apply = review, applied
    c.app.web.transport_factory = lambda _: Transport()
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    report = c.app.mcp.experiments.result(calls[0])
    assert report['status'] == 'stopped'
    assert len(c.sends) == len(report['requests']) == 5
    assert all(c.app.store.load_d4_operation(ref).completion is not None for ref in report['requests'])
    last = c.app.mcp.feedback(report['requests'][-1])
    assert last['http_response']['body_bytes'] == len(b'new control result')
    reviews = list(c.app.supervision.calls())
    assert len(reviews) == 1
    assert not c.app.store.list_d4_unresolved_operations(c.task)


@pytest.mark.parametrize('scenario',[
    'stopped','already_exited','signal_failed','signal_failed_but_exited',
    'inspect_failed','signal_inspect_failed','missing_running','stream_failed',
])
def test_experiment_close_classifies_signal_and_final_process_state(monkeypatch,scenario):
    from contextlib import nullcontext
    from types import SimpleNamespace as NS
    from Sandbox.http_experiment import HttpExperimentProcess
    from Sandbox.task_run_sandbox import TaskRunSandboxError
    from agent_adapters.output_evidence import safe_failure_details
    import Sandbox.http_experiment as bridge
    ticks=iter(range(100))
    monkeypatch.setattr(bridge.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(bridge.time,'sleep',lambda _:None)
    calls=[]
    class API:
        running=scenario!='already_exited'
        def exec_inspect(self,identity):
            if scenario=='inspect_failed' or identity=='signal' and scenario=='signal_inspect_failed':
                raise ConnectionError('private Docker response must not be saved')
            if identity=='signal':
                return dict(Running=False,ExitCode=1 if scenario.startswith('signal_failed') else 0)
            return {} if scenario=='missing_running' else dict(Running=self.running,ExitCode=None if self.running else 137)
        def exec_create(self,**kwargs):calls.append(kwargs);return dict(Id='signal')
        def exec_start(self,identity):
            if scenario not in {'signal_failed','signal_inspect_failed'}:self.running=False
            return b''
    closed=[]
    def close_stream():
        closed.append(True)
        if scenario=='stream_failed':raise OSError('private stream error')
    process=HttpExperimentProcess.__new__(HttpExperimentProcess)
    process.api=API()
    process.exec_id='original-exec'
    process.pid=4321
    process.task_id='task'
    process.sandbox_id='sandbox'
    process.binding=object()
    process.socket=NS(close=close_stream)
    process.sandbox=NS(_verify_active=lambda *args:None,_admit=lambda _:nullcontext())
    unknown=scenario in {'signal_failed','inspect_failed','signal_inspect_failed','missing_running'}
    if unknown:
        with pytest.raises(TaskRunSandboxError) as caught:process.close()
        assert caught.value.phase=='experiment_stop' and caught.value.outcome=='outcome_unknown'
        diagnostic=safe_failure_details(caught.value,include_chain=True)
        assert 'sandbox_experiment_stop_unknown' in diagnostic['error_codes']
        assert 'private' not in json.dumps(diagnostic)
    else:process.close()
    assert process.stop_diagnostic['process_stopped'] is (not unknown)
    assert process.stop_diagnostic['stream_closed'] is (scenario!='stream_failed')
    assert closed==[True]
    if scenario=='already_exited':assert not calls
    if scenario.startswith('signal_failed'):assert process.stop_diagnostic['signal_exit_code']==1
    if calls:
        assert calls[0]['cmd'][:3]==['python3','-I','-c'] and calls[0]['cmd'][-1]=='4321'
        assert calls[0]['user']=='10001:10001' and calls[0]['privileged'] is False


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('closure',['completed','strategy_stop'])
def test_stop_failure_retains_progress_and_requires_exit_confirmation_without_replay(http_case,closure):
    from types import SimpleNamespace as NS
    from Sandbox.task_run_sandbox import TaskRunSandboxError
    c=http_case
    script="request('GET','/once')\nprint('Retained progress, not a verified Fact.')" if closure=='completed' else "for i in range(6):\n    request('GET','/repeat')"
    calls=setup(c,script)
    if closure=='strategy_stop':
        outer,start=c.models[0].invoke,c.app.supervision.start
        def review(envelope,*args):
            if envelope.output_contract['stage']=='strategy_supervision':
                return dict(decision='request_intent_resolution',reason='Review the recorded repetitions.',
                    evidence_refs=[envelope.request.history[-1].ref],suggestion=None)
            return outer(envelope,*args)
        def wait_review(intent):
            start(intent)
            assert c.app.supervision.pending[1].wait(3)
            c.app.supervision.poll()
        c.models[0].invoke,c.app.supervision.start=review,wait_review
    class StopFailed(ScriptProcess):
        def close(self):
            super().close()
            self.stop_diagnostic=dict(phase='wait_exit',process_stopped=False,signal_exit_code=1,stream_closed=True)
            raise TaskRunSandboxError('experiment_stop','outcome_unknown',c.task,(c.app.state.task_run.sandbox_id,))
    c.app.mcp.experiments.runner_factory=StopFailed
    result=asyncio.run(c.app._run_once(c.token))
    assert result.recovery_required,result
    parent=calls[0]
    experiments=c.app.mcp.experiments
    retained=experiments.evidence.read('experiment-stop',parent)
    expected=1 if closure=='completed' else 4
    assert len(retained['requests'])==expected and len(c.sends)==expected
    if closure=='completed':
        assert retained['status']=='completed' and 'Retained progress' in retained['output']
    else:
        assert retained['status']=='stopped' and retained['code']=='d22_experiment_strategy_stop'
    assert retained['stop_diagnostic']['signal_exit_code']==1
    assert retained['stop_diagnostic']['sandbox_phase']=='experiment_stop'
    assert retained['stop_diagnostic']['sandbox_outcome']=='outcome_unknown'
    assert 'sandbox_experiment_stop_unknown' in retained['stop_failure']['error_codes']
    assert experiments.result(parent) is None and experiments.live is None
    assert c.app.store.load_d3_operation(parent).application_ref is None
    def unavailable(_):raise ConnectionError('private Docker inspection failure')
    c.app.http_sandbox._runtime=NS(client=NS(api=NS(exec_inspect=unavailable)))
    with pytest.raises(D3RecoveryRequired,match='process_reconciliation_required'):c.app.mcp.reconcile()
    assert experiments.result(parent) is None
    status=dict(ContainerID=c.app.state.task_run.sandbox_id,Running=True)
    c.app.http_sandbox._runtime=NS(client=NS(api=NS(exec_inspect=lambda _:status)))
    with pytest.raises(D3RecoveryRequired,match='process_reconciliation_required'):c.app.mcp.reconcile()
    status['Running']=False
    c.app.mcp.reconcile()
    restored=experiments.result(parent)
    assert restored['requests']==retained['requests'] and restored['output']==retained['output']
    assert restored['stop_diagnostic']['phase']=='reconciled_exit'
    assert c.app.store.load_d3_operation(parent).application_ref==parent
    c.app.mcp.reconcile()
    assert len(c.sends)==expected and len(calls)==1


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
def test_request_unknown_is_not_masked_by_stop_failure_or_replayed(http_case):
    from types import SimpleNamespace as NS
    c=http_case
    calls=setup(c,"request('GET','/first')\nrequest('GET','/unknown')")
    class StopFailed(ScriptProcess):
        def close(self):
            super().close()
            raise ConnectionError('private stop failure')
    class Transport:
        def send(self,**kwargs):
            from agent_adapters.web.http_adapter import D7HttpWireResponse
            c.sends.append(kwargs)
            if len(c.sends)==2:raise ConnectionResetError('private request failure')
            return D7HttpWireResponse(200,(),b'first')
    c.app.web.transport_factory=lambda _:Transport()
    c.app.mcp.experiments.runner_factory=StopFailed
    result=asyncio.run(c.app._run_once(c.token))
    assert result.recovery_required,result
    parent=calls[0]
    experiments=c.app.mcp.experiments
    retained=experiments.evidence.read('experiment-stop',parent)
    assert len(retained['requests'])==2 and 'private' not in json.dumps(retained)
    assert experiments.result(parent) is None
    c.app.http_sandbox._runtime=NS(client=NS(api=NS(exec_inspect=lambda _:
        dict(ContainerID=c.app.state.task_run.sandbox_id,Running=False))))
    with pytest.raises(D3RecoveryRequired,match='request_outcome_unknown'):c.app.mcp.reconcile()
    assert len(c.sends)==2 and len(calls)==1


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('fallback',['none','http','mcp'])
def test_experiment_detail_does_not_impersonate_a_final_candidate_source(http_case,monkeypatch,fallback):
    c=http_case
    parents=setup(c,"request('GET','/child')",fact=True)
    choose=c.models[1].invoke
    seeded=[]
    candidates=[]
    def inner(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan' and fallback!='none' and not seeded:
            seeded.append(True)
            if fallback=='mcp':
                return dict(kind='mcp_call',tool_name='shell.run_bash',arguments=dict(script='fixed test'))
            return dict(kind='http_request',method='GET',path_query='/ordinary',headers={},body='')
        if envelope.output_contract['stage']=='inner_candidate':
            candidates.append(envelope.request.context)
            return dict(action=dict(kind='no_candidate'))
        return choose(envelope,*args)
    c.models[1].invoke=inner
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert len(parents)==1 and len(c.app.mcp.experiments.records(parents[0]))==1
    assert len(c.app.state.facts)==1 and c.app.state.intents[0].status.value=='succeeded'
    assert c.app.state.task_run.status.value=='running'  # No independent final validation.
    if fallback=='none':
        assert not candidates  # Unsupported source skips the checkpoint, not child planning.
        assert any(r.kind=='expansion_exhausted' and r.intent_id==c.app.state.intents[0].intent_id
                   for r in c.app.store.list_d8_tree_records(c.task))
    elif fallback=='http':
        assert candidates[0].execution_id==c.app.state.executions[0].execution_id
    else:
        assert '/experiment/' not in c.app.store.load_d4_operation(
            candidates[0].source_ref.removeprefix('mcp-result/')).start.resource_ref
    # Directly presenting a child as an ordinary MCP source must still be rejected.
    from types import SimpleNamespace
    from agent_core.competition_candidates import validate_mcp_source
    if fallback=='none':
        with monkeypatch.context() as absent:
            absent.setattr(c.app.store,'list_d4_operations',lambda task:())
            with pytest.raises(D3RecoveryRequired,match='d9_candidate_source_missing'):
                c.app.answers._request(c.app.state.intents[0])
    if fallback=='mcp':
        from dataclasses import replace
        from uuid import uuid4
        from agent_core.competition_candidates import provenance
        original=SimpleNamespace(**provenance(candidates[0]))
        validate_mcp_source(c.app.store,original,c.app.output_boundary.evidence)
        for change in ({'task_run_id':str(uuid4())},{'intent_id':str(uuid4())},{'evidence_version':'0'*64}):
            with pytest.raises(ValueError,match='mcp_candidate_source_invalid'):
                validate_mcp_source(c.app.store,SimpleNamespace(**(vars(original)|change)),c.app.output_boundary.evidence)
        operation=c.app.store.load_d4_operation(original.source_ref.removeprefix('mcp-result/'))
        model=c.app.store.load_d3_operation(operation.start.resource_ref.split('/')[-1])
        with monkeypatch.context() as broken:
            broken.setattr(c.app.store,'load_d3_operation',lambda key:replace(model,application_ref=parents[0]))
            with pytest.raises(ValueError,match='mcp_candidate_model_binding_invalid'):
                validate_mcp_source(c.app.store,original,c.app.output_boundary.evidence)
    child=c.app.mcp.experiments.records(parents[0])[0]
    candidate=SimpleNamespace(source_ref=child.completion.evidence_ref,plan_id=None,execution_id=None,
        task_run_id=c.task,intent_id=c.app.state.intents[0].intent_id,evidence_version=child.completion.evidence_sha256)
    with pytest.raises(ValueError,match='mcp_candidate_source_invalid'):
        validate_mcp_source(c.app.store,candidate,c.app.output_boundary.evidence)


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('group',[None,'same baseline hypothesis'])
def test_optional_exploration_group_preserves_multi_request_source_binding(http_case,monkeypatch,group):
    from dataclasses import asdict,replace
    from agent_core.turn_modeling import digest
    c=http_case
    parents=setup(c,"for i in range(9):\n    request('GET','/variant?i='+str(i))")
    inner=c.models[1].invoke
    def choose(envelope,*args):
        raw=inner(envelope,*args)
        return {**raw,'exploration_group':group} if group and raw.get('tool_name')==TOOL else raw
    c.models[1].invoke=choose
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    report=c.app.mcp.experiments.result(parents[0])
    assert report['status']=='completed' and len(report['requests'])==len(c.sends)==9
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    if group:
        notices=[g for _,request in c.app.supervision.calls() for g in json.loads(request.prior_summary)['exploration_progress']['repeated_observations']]
        assert any(g['threshold']==4 for g in notices)
        assert c.app.supervision.repeated_observations(c.app.state.intents[0].intent_id)[0]['count']==9
    child=c.app.store.load_d4_operation(report['requests'][0])
    claim=c.app.mcp.evidence.read('mcp-call',child.start.operation_id)
    saved=c.app.mcp.evidence.read('mcp-result',child.start.operation_id)
    original=c.app.model_evidence.read(parents[0]);parent=c.app.store.load_d3_operation(parents[0])
    for replacement in [dict(tool_name='shell.run_bash'),dict(arguments={'script':'changed','timeout_seconds':60}),dict(extra_authority=True)]:
        altered=replace(original,raw={**original.raw,**replacement})
        # Match the model's own checksum, so the execution-content comparison
        # itself must still reject a different tool/arguments/unknown field.
        altered_parent=replace(parent,completion=replace(parent.completion,evidence_sha256=digest(asdict(altered))))
        with monkeypatch.context() as patch:
            patch.setattr(c.app.model_evidence,'read',lambda key:altered)
            patch.setattr(c.app.store,'load_d3_operation',lambda key:altered_parent)
            with pytest.raises(D3RecoveryRequired,match='d22_experiment_source_changed'):
                c.app.mcp.experiments.validate_receipt(child,claim,saved)
    assert len(c.sends)==9


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('phase',['receipt_save','receipt_apply'])
def test_receipt_failure_is_durable_before_close_and_public_without_replay(http_case,monkeypatch,phase):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    from agent_adapters.http_experiments import failure_diagnostics
    c=http_case;parents=setup(c,"request('GET','/once')\nrequest('GET','/must-not-run')")
    original_save=c.app.mcp.evidence.save;original_apply=c.app.mcp._apply
    code='d22_experiment_source_changed' if phase=='receipt_apply' else 'd8_handoff_evidence_write_recovery_required'
    def save(kind,key,value):
        if phase=='receipt_save' and kind=='mcp-result':raise D3RecoveryRequired(code)
        return original_save(kind,key,value)
    def apply(operation,claim,result):
        if phase=='receipt_apply' and 'experiment_request' in claim:raise D3RecoveryRequired(code)
        return original_apply(operation,claim,result)
    monkeypatch.setattr(c.app.mcp.evidence,'save',save)
    monkeypatch.setattr(c.app.mcp,'_apply',apply)
    closed=[]
    class CheckedClose(ScriptProcess):
        def close(self):
            # close runs in a worker thread; inspect only files, never the main-thread SQLite connection.
            files=list((c.directory/'d8-output').glob('d8-handoff-experiment-failure-*.json'))
            assert len(files)==1
            assert json.loads(files[0].read_text('utf-8'))['payload']['phase']==phase
            closed.append(True);super().close()
    c.app.mcp.experiments.runner_factory=CheckedClose
    result=asyncio.run(c.app._run_once(c.token))
    assert result.recovery_required and result.stop_reason==code,result
    assert len(c.sends)==1 and closed==[True]
    operation=c.app.mcp.experiments.records(parents[0])[0]
    assert operation.completion is None
    failure=c.app.mcp.evidence.read('experiment-failure',operation.start.operation_id)
    assert failure['phase']==phase and failure['code']==code
    assert 'private' not in json.dumps(failure)
    snapshot=snapshot_v3_task_result(c.directory,c.declaration)
    assert not snapshot['errors'],snapshot
    row=next(item for item in snapshot['http_diagnostics'] if item.get('execution_id')==operation.start.operation_id)
    assert row['phase']==phase and row['active'] and row['result_saved']==(phase=='receipt_apply')
    assert snapshot['recovery']['reason']==code
    if phase=='receipt_apply':
        with pytest.raises(D3RecoveryRequired,match=code):c.app.mcp.reconcile()
        assert c.app.mcp.evidence.read('experiment-failure',operation.start.operation_id)==failure
    assert len(c.sends)==1
    read=c.app.mcp.evidence.read
    monkeypatch.setattr(c.app.mcp.evidence,'read',lambda kind,key:
        {**failure,'request_sha256':'0'*64} if kind=='experiment-failure' else read(kind,key))
    with pytest.raises(D3RecoveryRequired,match='d22_experiment_failure_diagnostic_invalid'):
        failure_diagnostics(c.app.mcp.evidence,c.app.store,c.task)
