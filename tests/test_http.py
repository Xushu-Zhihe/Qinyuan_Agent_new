"""Contract20 HTTP capability over the D8 scheduler; no external network."""
import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_adapters.multi_turn_application import D3Application
from agent_adapters.web import http_adapter,candidate_evidence
from agent_adapters.web.http_adapter import D7HttpWireResponse
from agent_core.contracts import TaskRequest
from agent_core.control_store import TargetNetworkBinding
from agent_core.dual_llm_contracts import D3Authorization,D3RunConfig
from agent_core.handoff_contracts import D8CapacityPolicy,D8HandoffConfig,D8Capacity
from agent_core.operation_accounting import D8HandoffBudgetPolicy
from agent_core.turn_contracts import D1RoleConfig
from agent_core.web_contracts import D7WebAuthorization
from test_chain_multi_turn import Clock,Token,Sandbox
from test_candidate_evidence import _TestOnlyProtection

ORIGIN='http://example.test:18080'


class Network:
    def create(self,task,url):
        return TargetNetworkBinding('1'*64,'2'*64,'3'*64,'a'*64,url)
    def bind(self,*args,**kwargs):pass
    def destroy(self,*args):pass


class WebSandbox(Sandbox):
    def bind(self,*args,**kwargs):pass
    def close(self):pass
    def configure_target_network(self,*args):pass
    def quiesce(self,*args):pass
    def interrupt(self,*args):pass


class Model:
    def __init__(self,role):
        self.role_config=role
        self.calls=[]
    def invoke(self,envelope,*args):
        if hasattr(self,'context_loader'):
            from agent_adapters.deepseek_roles import build_payload
            build_payload(envelope,self.role_config,context_loader=self.context_loader)
        self.calls.append(envelope)
        request=envelope.request
        stage=envelope.output_contract['stage']
        if stage=='tree_planning':
            if request.turn_id is not None:
                return dict(kind='exhausted',reason='No more directions.',evidence_refs=[])
            return dict(kind='candidates',intents=[dict(description='Observe the home page response.',intent_type=None,dependency_refs=[])])
        if stage in {'review','progress_review'}:
            result=dict(decision='continue_current_intent',rationale='Current authorized observation is useful.',
                        source_ids=list(request.source_versions),constraints=[])
            context=json.loads(request.context_summary)
            if request.review_scope=='turn_end':result['decision']='acknowledge_turn_end'
            if stage=='progress_review':
                result['progress']=dict(classification='meaningful',reason='New saved evidence.',
                    evidence_refs=[context['d8_progress']['recent'][-1]['ref']])
            return result
        if stage == 'strategy_supervision':
            return dict(decision='continue_current_intent', reason='The bounded experiment remains useful.',
                        evidence_refs=[request.history[-1].ref], suggestion=None)
        if stage=='inner_plan':
            return dict(kind='http_request',method='GET',path_query='/',headers={},body='')
        if stage=='fact_proposal':
            return dict(kind='candidate',content='The authorized HTTP response was observed.',reason='Saved response evidence.',
                evidence_refs=[request.observation.proof_id],assertion=dict(rule_id='http_response_observed'))
        if stage=='fact_review':return dict(decision='accept',reason='The exact recorded observation supports this scope.')
        if stage=='inner_complete':
            return dict(action=dict(kind='complete_intent',fact_ids=list(request.context.supporting_fact_ids)),
                        review_requested=False,next_intent_candidates=[])
        if stage=='intent_evaluation':
            return dict(decision='succeeded',reason='The response observation fulfils the unchanged local goal.',
                        evidence_refs=[i.ref for i in request.history])
        if stage=='inner_candidate':
            return dict(action=dict(kind='candidate_flag',value='sample-answer',execution_id=request.context.execution_id))
        if stage=='handoff_summary':
            return dict(turn_body='Observed HTTP response; final task not evaluated.',global_body='HTTP observation only.',
                        ordered_refs=[i.ref for i in request.history],open_questions=[])
        if stage=='refinement':return dict(summary_text='HTTP progress only.',claims=[])
        raise AssertionError(stage)


@pytest.fixture
def http_case(tmp_path,monkeypatch,request):
    monkeypatch.setattr(http_adapter,'WindowsEvidenceProtection',_TestOnlyProtection)
    monkeypatch.setattr(candidate_evidence,'WindowsEvidenceProtection',_TestOnlyProtection)
    from agent_adapters.deepseek_roles import role_config,EstimatedCapacity
    from agent_adapters.simulation import real_config
    requested=getattr(request,'param',20)
    rolling=requested in {'rolling', 'continuous'}
    version=22 if rolling else requested
    outer=role_config('outer_planning',version=version-5)
    inner=role_config('inner_action',version=version-5)
    public_config=real_config(contract_version=version,http=True,rolling=rolling)
    if rolling:public_config['intent_execution_policy']='continuous_v1' if requested=='continuous' else 'rolling_v1'
    authorization_id=str(uuid4())
    config=D3RunConfig(outer,inner,D3Authorization(authorization_id,()),outer.role_config_ref,
        contract_version=version,d4_budget=D8HandoffBudgetPolicy(),d5_enabled=True,d5_sensitive_requirements=(),
        d8_enabled=True,handoff_config=D8HandoffConfig.from_dict(public_config['handoff_config']),
        web_authorization=D7WebAuthorization(authorization_id,ORIGIN,('GET','HEAD','OPTIONS')),http_timeout_seconds=60,
        intent_execution_policy=public_config.get("intent_execution_policy"))
    models=[Model(outer),Model(inner)]
    clock,token,sandbox=Clock(),Token(),WebSandbox()
    directory=tmp_path/'run'
    directory.mkdir()
    sends=[]
    response_headers=[('Content-Type','text/html')]
    class Transport:
        def send(self,**kwargs):
            sends.append(kwargs)
            return D7HttpWireResponse(200,tuple(response_headers),b'<p>ordinary response</p>')
    def create():
        app=D3Application(directory,config=config,sandbox=sandbox,import_sandbox=sandbox,
            workspace_sandbox=sandbox,command_sandbox=sandbox,outer_model=models[0],inner_model=models[1],
            review_model=models[0],fact_review_model=models[0],refinement_model=models[0],handoff_model=models[0],
            capacity_port=EstimatedCapacity(outer,inner),clock=clock,target_network_adapter=Network())
        app.web.transport_factory=lambda _:Transport()
        return app
    app=create()
    task=app.start(TaskRequest.from_dict(dict(goal='Observe home page and propose a candidate.',deadline_seconds=600,
        target=dict(attachments=[],endpoints=[dict(url=ORIGIN)]))),tmp_path)
    case=SimpleNamespace(app=app,task=task,token=token,clock=clock,sends=sends,models=models,
        create=create,directory=directory,response_headers=response_headers,sandbox=sandbox)
    from agent_adapters.run_files import immutable_write
    from agent_adapters.simulation import real_declaration_binding
    raw=dict(protocol_version=3,request_id=str(uuid4()),execution_profile=f'd8_v{version}',mode='real',
        d8_config=public_config,task_request=dict(goal='Observe home page and propose a candidate.',deadline_seconds=600,
            target=dict(source_root=str(tmp_path),attachments=[],endpoints=[dict(url=ORIGIN)])),
        authorization=config.web_authorization.to_dict(),dual_llm_config=dict(outer_role=asdict(outer),
            inner_role=asdict(inner),d4_budget=asdict(config.d4_budget)))
    declaration=dict(protocol_version=3,task_run_id=task,request_id=raw['request_id'],request=raw,mode='real',
        contract_version=version,execution_profile=f'd8_v{version}',sandbox_backend='docker',env_file=str(tmp_path/'unused.env'),
        d4_budget_policy=asdict(config.d4_budget),source_root=str(directory),attachments=[],staged_attachment_paths=[],
        role_config_refs=dict(outer=outer.role_config_ref,inner=inner.role_config_ref),ids=dict(cancel=str(uuid4()),start=str(uuid4())))
    declaration=json.loads(json.dumps(declaration))
    raw=declaration['request']
    immutable_write(directory/'run.json',declaration)
    immutable_write(directory/'d8-runtime.json',real_declaration_binding(declaration))
    case.declaration,case.raw=declaration,raw
    yield case
    case.app.store.close()


@pytest.mark.parametrize("known", [True, False])
def test_partial_start_failure_only_compensates_confirmed_creation(http_case, known):
    from Sandbox.target_network import T6TargetNetworkError
    c = http_case
    app = c.create()
    cleaned = []
    class FailedNetwork(Network):
        def create(self, task, url):
            raise T6TargetNetworkError("create", "creation_failed" if known else "outcome_unknown", ("1" * 64,))
        def cleanup_uncommitted(self, task):cleaned.append(task)
    from agent_adapters.target_network_cleanup import RecordedTargetNetwork
    app.controller._target_network = RecordedTargetNetwork(FailedNetwork(), c.directory)
    task = str(uuid4())
    try:
        with pytest.raises(T6TargetNetworkError):app.start(c.app.state.task_run.request, c.directory, task_run_id=task)
        assert cleaned == ([task] if known else [])
        assert app.store.load_task_run(task) is None
    finally:
        app.store.close()


@pytest.mark.parametrize("fault", [None, "network", "sandbox"])
def test_terminal_network_cleanup_follows_sandbox_and_retains_unknown(http_case, monkeypatch, fault):
    from Sandbox.target_network import T6TargetNetworkError
    from agent_adapters.target_network_cleanup import RecordedTargetNetwork
    c = http_case
    order = []
    destroy = c.sandbox.destroy
    def sandbox_destroy(identity):
        assert c.app.store.load_task_run(c.task).status.value == "cancelled"
        order.append("sandbox")
        if fault == "sandbox":raise RuntimeError("controlled cleanup failure")
        destroy(identity)
    class CleanupNetwork(Network):
        def destroy(self, task, binding):
            assert task == c.task and c.sandbox.destroyed
            order.append("network")
            if fault == "network":raise T6TargetNetworkError("destroy", "outcome_unknown", (binding.forwarder_id,))
    monkeypatch.setattr(c.sandbox, "destroy", sandbox_destroy)
    c.app.controller._target_network = RecordedTargetNetwork(CleanupNetwork(), c.directory)
    result = c.app.cancel(c.task)
    assert order == (["sandbox"] if fault == "sandbox" else ["sandbox", "network"])
    assert (c.directory / "target-network-cleanup.json").exists() == (fault is None)
    assert bool(result.recovery_required) == (fault is not None)


def test_http_d8_tree_fact_and_candidate_wait(http_case):
    case=http_case
    result=asyncio.run(case.app.run(case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.sends)==1
    assert len(case.app.state.facts)==1
    assert case.app.state.intents[0].status.value=='succeeded'
    candidate,=case.app.store.list_d7_answer_candidates(case.task)
    assert candidate.execution_id==case.app.state.executions[0].execution_id
    assert len(case.app.state.turns)==1
    for directory in ('d8-output','d8-sessions','d3-models'):
        for path in (case.directory/directory).rglob('*.json'):
            text=path.read_text(encoding='utf-8')
            assert 'sample-answer' not in text and '<p>ordinary response</p>' not in text


def test_http_public_snapshot_is_readonly_and_model_context_is_bound(http_case):
    case=http_case
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    from agent_adapters.web.task_result import snapshot_v3_task_result
    result=snapshot_v3_task_result(case.directory,case.declaration)
    assert result['errors']==[],result
    assert result['contract_version']==20 and not result['recovery']['required'],result
    assert len(result['answer_candidates'])==1
    from agent_adapters.deepseek_roles import build_payload
    envelope=next(e for e in case.models[0].calls if e.output_contract['stage']=='fact_review')
    payload=build_payload(envelope,case.app.config.outer_role,context_loader=case.app.web.model_context)
    assert '<p>ordinary response</p>' in payload['messages'][-1]['content']
    assert len(case.sends)==1


def reopen(case):
    case.app.store.close()
    case.app=case.create()


@pytest.mark.parametrize('change',[
    dict(method='POST'),dict(path_query='http://other.test:18080/'),dict(path_query='//other.test/'),
    dict(headers={'Host':'other.test'}),dict(headers={'Authorization':'test-only-placeholder'})])
def test_http_permission_and_origin_rejected_before_send(http_case,change):
    case=http_case
    original=case.models[1].invoke
    def invoke(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan':
            return dict(kind='http_request',method='GET',path_query='/',headers={},body='')|change
        return original(envelope,*args)
    case.models[1].invoke=invoke
    result=asyncio.run(case.app.run(case.token))
    assert result.recovery_required
    assert case.sends==[]
    from agent_core.structured_output import StructuredOutput
    assert any(case.app.output_boundary.diagnostic(o.start.operation_id) is not None
               for o in case.app.store.list_d3_operations(case.task))


def child_once(case):
    original=case.models[0].invoke
    def invoke(envelope,*args):
        if envelope.output_contract['stage']=='tree_planning' and envelope.request.intent_id:
            return dict(kind='candidates',intents=[dict(description='Observe the saved session response.',
                intent_type=None,dependency_refs=[])])
        return original(envelope,*args)
    case.models[0].invoke=invoke


def reject_candidate(case):
    from agent_core.goal_feedback import ExternalValidationFeedback
    candidate=case.app.store.list_d7_answer_candidates(case.task)[-1]
    return asyncio.run(case.app.answers.apply_feedback(ExternalValidationFeedback(case.task,
        candidate.candidate_id,candidate.evidence_version,'rejected','test-only-trusted-adapter',str(uuid4())),case.token))


def test_http_cookie_restores_after_reopen_without_plaintext(http_case):
    case=http_case
    case.response_headers.append(('Set-Cookie','session=TEST_PRIVATE_COOKIE; Path=/; HttpOnly'))
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    assert not any(h[0].lower()=='cookie' for h in case.sends[0]['headers'])
    reopen(case)
    asyncio.run(case.app.resume(case.task,case.token))
    child_once(case)
    result=reject_candidate(case)
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.sends)==2
    assert ('Cookie','session=TEST_PRIVATE_COOKIE') in case.sends[1]['headers']
    for directory in ('d8-output','d8-sessions','d3-models'):
        assert all('TEST_PRIVATE_COOKIE' not in p.read_text('utf-8') for p in (case.directory/directory).rglob('*.json'))
    assert b'TEST_PRIVATE_COOKIE' not in (case.directory/'control.sqlite').read_bytes()


@pytest.mark.parametrize('damage',['missing','conflict'])
def test_http_cookie_missing_or_conflict_pauses_without_new_send(http_case,damage):
    case=http_case
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    execution=case.app.state.executions[0]
    value=case.app.web.result(execution.execution_id)
    if damage=='missing':
        (case.directory/'d7-http-evidence'/(value['cookie_evidence_id']+'.cipher')).unlink()
    else:
        path=case.directory/'d8-output'/('d8-handoff-http-result-'+execution.execution_id+'.json')
        raw=json.loads(path.read_text('utf-8'))
        raw['payload']['cookie_previous_execution_id']=str(uuid4())
        from agent_core.turn_modeling import digest
        raw['sha256']=digest(raw['payload'])
        path.write_text(json.dumps(raw),encoding='utf-8')
    reopen(case)
    from agent_core.control_store import StorageError
    with pytest.raises(StorageError):asyncio.run(case.app.resume(case.task,case.token))
    from agent_adapters.web.task_result import snapshot_v3_task_result
    result=snapshot_v3_task_result(case.directory,case.declaration)
    assert result['recovery']['required'] and len(case.sends)==1


@pytest.mark.parametrize('stop',['cancel','deadline'])
def test_http_inflight_stop_does_not_apply_late_result(http_case,stop):
    import threading
    case=http_case
    release=threading.Event()
    ended=threading.Event()
    class Blocking:
        def send(self,**kwargs):
            case.sends.append(kwargs)
            if stop=='cancel':case.token.is_cancelled=True
            else:case.clock.now+=601
            release.wait(3)
            ended.set()
            return D7HttpWireResponse(200,(),b'late response')
    case.app.web.transport_factory=lambda _:Blocking()
    try:
        result=asyncio.run(case.app.run(case.token))
        # An admitted unknown HTTP request keeps the existing D4 recovery gate;
        # cancellation/deadline cannot fabricate a known terminal tool outcome.
        assert result.recovery_required and result.stop_reason=='d4_operation_outcome_unknown',result
        assert not case.sandbox.destroyed
        assert not case.app.state.facts and len(case.sends)==1
        assert case.app.web.result(case.app.state.executions[0].execution_id) is None
    finally:
        release.set()
        ended.wait(3)


def test_http_unknown_never_resends_after_resume(http_case):
    case=http_case
    class Broken:
        def send(self,**kwargs):
            case.sends.append(kwargs)
            raise RuntimeError('TEST_ONLY_ERROR_NEVER_PUBLIC')
    case.app.web.transport_factory=lambda _:Broken()
    result=asyncio.run(case.app.run(case.token))
    assert result.recovery_required and len(case.sends)==1
    reopen(case)
    result=asyncio.run(case.app.resume(case.task,case.token))
    assert result.recovery_required and len(case.sends)==1
    assert not case.app.state.facts


def test_http_saved_result_recovers_without_resend(http_case,monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    case=http_case
    complete=case.app.store.complete_d4_operation
    def interrupt(value):
        operation=case.app.store.load_d4_operation(value.operation_id)
        if operation.start.category=='tool_dispatch':raise ProcessLoss()
        return complete(value)
    monkeypatch.setattr(case.app.store,'complete_d4_operation',interrupt)
    with pytest.raises(ProcessLoss):asyncio.run(case.app.run(case.token))
    assert len(case.sends)==1
    reopen(case)
    result=asyncio.run(case.app.resume(case.task,case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.sends)==1


def test_http_waiting_replay_keeps_model_and_request_counts(http_case):
    case=http_case
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    count=sum(len(m.calls) for m in case.models)
    reopen(case)
    result=asyncio.run(case.app.resume(case.task,case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert sum(len(m.calls) for m in case.models)==count and len(case.sends)==1


@pytest.mark.parametrize('verdict',['accepted','rejected','unknown'])
def test_http_candidate_feedback_uses_d8_child_boundary(http_case,verdict):
    from agent_core.goal_feedback import ExternalValidationFeedback
    case=http_case
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    candidate,=case.app.store.list_d7_answer_candidates(case.task)
    record=ExternalValidationFeedback(case.task,candidate.candidate_id,candidate.evidence_version,
                                      verdict,'test-only-trusted-adapter',str(uuid4()))
    result=asyncio.run(case.app.answers.apply_feedback(record,case.token))
    if verdict=='accepted':assert case.app.state.task_run.status.value=='achieved'
    elif verdict=='unknown':assert case.app.state.task_run.status.value=='awaiting_external_feedback'
    else:
        assert result.stop_reason=='search_exhausted',result
        assert case.app.state.intents[0].status.value=='succeeded'
        assert len(case.app.state.facts)==1
        calls=[c for m in case.models for c in m.calls if c.output_contract['stage']=='tree_planning' and c.request.intent_id]
        assert any('answer_feedback' in h.content and 'rejected' in h.content for h in calls[-1].request.history)
    assert len(case.sends)==1


def test_http_corrupt_source_blocks_reliable_projection(http_case):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    case=http_case
    assert asyncio.run(case.app.run(case.token)).stop_reason=='d9_awaiting_external_feedback'
    execution=case.app.state.executions[0]
    result=case.app.web.result(execution.execution_id)
    cipher=case.directory/'d7-http-evidence'/(result['body_evidence_id']+'.cipher')
    data=cipher.read_bytes()
    cipher.write_bytes(bytes([data[0]^1])+data[1:])
    projected=snapshot_v3_task_result(case.directory,case.declaration)
    assert projected['recovery']['required'] and projected['verified_facts']==[]
    assert len(case.sends)==1


@pytest.mark.parametrize('first',['inference','rejected','review_replan'])
def test_http_conclusion_review_keeps_existing_d8_recovery_loop(http_case,first):
    case=http_case
    inner_original=case.models[1].invoke
    outer_original=case.models[0].invoke
    count={'proposal':0,'review':0}
    def inner(envelope,*args):
        if envelope.output_contract['stage']=='fact_proposal':
            count['proposal']+=1
            if count['proposal']==1:
                if first=='review_replan':return dict(kind='request_review',reason='Need a new authorized observation.')
                value=inner_original(envelope,*args)
                if first=='inference':value['assertion']=None
                return value
        return inner_original(envelope,*args)
    def outer(envelope,*args):
        if envelope.output_contract['stage']=='fact_review':
            count['review']+=1
            if first=='rejected' and count['review']==1:return dict(decision='reject',reason='Observation scope is not clear enough.')
        value=outer_original(envelope,*args)
        if first=='review_replan' and envelope.output_contract['stage']=='review' and envelope.request.review_scope=='action':
            value['decision']='replan_current_intent'
        return value
    case.models[1].invoke=inner
    case.models[0].invoke=outer
    result=asyncio.run(case.app.run(case.token))
    assert result.stop_reason=='d9_awaiting_external_feedback',result
    assert len(case.app.state.facts)==1
    outcomes=[r.outcome for r in case.app.store.list_d8_conclusions(case.task)]
    assert outcomes==(['reviewed_inference','verified_fact'] if first=='inference' else
        ['rejected','verified_fact'] if first=='rejected' else ['verified_fact'])
    assert len(case.sends)==(2 if first=='review_replan' else 1)
