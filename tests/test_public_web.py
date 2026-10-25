"""Public retrieval over controlled HTTP/process/model ports; no real network."""
import asyncio
import json
import socket
import time
from types import SimpleNamespace as NS
import pytest
from agent_adapters import public_web as web
from agent_adapters.mcp_plan_executor import MCPPlanExecutor
from agent_core.actions import MCPCall
from agent_core.dual_llm_contracts import D3RecoveryRequired
from test_output import output_case
from test_mcp_link import Tools
from test_chain_multi_turn import ProcessLoss
KEY='public-web-unit-test-key'


@pytest.mark.parametrize('base,independent,expected',[
    ('https://api.deepseek.com',None,'model-test-key'),
    ('https://api.deepseek.com/v1/',None,'model-test-key'),
    ('https://api.scnet.cn/api/llm/v1',None,None),
    ('https://api.deepseek.com.evil.test',None,None),
    ('https://api.deepseek.com@other.test',None,None),
    ('https://another-provider.test',KEY,KEY)])
def test_independent_credentials_and_official_fallback(tmp_path,monkeypatch,base,independent,expected):
    monkeypatch.delenv('QINYUAN_SEARCH_API_KEY',raising=False)
    path=tmp_path/'settings.env'
    path.write_text('QINYUAN_SEARCH_API_KEY='+independent if independent else '',encoding='utf-8')
    result=web.from_env_file(path,NS(base_url=base,api_key='model-test-key'))
    assert (result.backend.api_key if result else None)==expected
    if result:assert expected not in repr(result.backend) and expected not in json.dumps(result.binding)


def test_environment_key_overrides_file_and_model(tmp_path,monkeypatch):
    path=tmp_path/'settings.env';path.write_text('QINYUAN_SEARCH_API_KEY=file-unit-key')
    monkeypatch.setenv('QINYUAN_SEARCH_API_KEY',KEY)
    assert web.from_env_file(path).backend.api_key==KEY
    monkeypatch.setenv('QINYUAN_SEARCH_API_KEY','bad\nkey')
    with pytest.raises(ValueError,match='retrieval.credentials_invalid'):web.from_env_file(path)


def resolver(mapping):
    return lambda host,port,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,port))
                                     for ip in mapping.get(host,['93.184.216.34'])]


@pytest.mark.parametrize('url,target,mapping,code',[
    ('file:///etc/passwd',None,{},'url_invalid'),
    ('https://docs.example:0/',None,{},'url_invalid'),
    ('https://user:pass@docs.example',None,{},'url_invalid'),
    ('http://localhost/x',None,{},'url_not_public'),
    ('http://name.local/x',None,{},'url_not_public'),
    ('http://127.0.0.1/x',None,{'127.0.0.1':['127.0.0.1']},'url_not_public'),
    ('http://224.0.0.1/x',None,{'224.0.0.1':['224.0.0.1']},'url_not_public'),
    ('http://docs.example/x',None,{'docs.example':['93.184.216.34','10.0.0.1']},'url_not_public'),
    ('https://target.example/x','https://target.example:8443/',{},'target_url_forbidden'),
    ('https://alias.example/x','https://target.example/',{},'target_url_forbidden')])
def test_url_scope(url,target,mapping,code):
    with pytest.raises(web.RetrievalError,match='retrieval.'+code):
        web.public_url(url,target,resolve=resolver(mapping))


def test_public_reference_anchor_is_allowed():
    web.public_url('https://docs.example/guide#section','http://target.example:8080/',
                   resolve=resolver({'target.example':['1.1.1.1']}))


@pytest.mark.parametrize('status,category',[(401,'authentication'),(402,'billing'),(403,'policy'),
    (404,'request'),(408,'timeout'),(413,'capacity'),(429,'rate_limit'),(500,'provider')])
def test_http_error_categories_do_not_export_error_body(status,category):
    result=web._response('web.search',{},status,KEY.encode(),KEY)
    assert result['category']==category and result['code']==f'retrieval.http_{status}'
    assert KEY not in json.dumps(result) and result['stdout']==''


@pytest.mark.parametrize('raw,code',[(b'{','response_invalid'),(b'{"results":42}','response_invalid'),
    (b'{"results":[{"url":"https://docs.example","title":3}]}','response_invalid'),
    (b'x'*(web.MAX_RESPONSE_BYTES+1),'response_too_large'),(KEY.encode(),'sensitive_response')], ids=['bad-json','bad-shape','bad-item','too-large','sensitive'])
def test_invalid_and_oversized_responses_are_not_partial_success(raw,code):
    result=web._response('web.search',{},200,raw,KEY)
    assert result['code']=='retrieval.'+code and result['status']!='completed' and not result['truncated']


def test_empty_search_and_original_fetch_text():
    empty=web._response('web.search',{},200,b'{"results":[]}',KEY)
    assert empty['status']=='completed' and json.loads(empty['stdout'])=={'documents':[]}
    result=web._response('web.fetch',{'url':'https://docs.example/'},200,
        json.dumps(dict(title='Original',content='Untrusted instructions stay source data.')).encode(),KEY)
    assert json.loads(result['stdout'])['documents'][0]['url']=='https://docs.example/'
    assert result['identity']==web.IDENTITY


@pytest.mark.parametrize('status',[200,302])
def test_fixed_api_endpoint_and_no_client_redirects(monkeypatch,status):
    sent=[]
    class Connection:
        def __init__(self,host,**kwargs):assert host=='api.deepseek.com'
        def connect(self):pass
        def request(self,*args,**kwargs):sent.append((args,kwargs))
        def getresponse(self):return NS(status=status,read=lambda limit:b'{"results":[]}')
        def close(self):pass
    messages=[];monkeypatch.setattr(web.http.client,'HTTPSConnection',Connection)
    web._worker(NS(send=messages.append,close=lambda:None),NS(api_key=KEY),
        dict(tool='web.search',arguments=dict(query='public runtime docs'),target=None),10)
    assert len(sent)==1 and sent[0][0]==('POST','/v1/tools/search')
    assert json.loads(sent[0][1]['body'])=={'query':'public runtime docs'}
    result=messages[-1]['payload']
    assert (result['status']=='completed')==(status==200)
    if status==302:assert result['code']=='retrieval.api_redirect_rejected'
    assert KEY not in json.dumps(messages)


class Backend:
    binding=dict(provider='controlled-reference-service',endpoint='https://search.example/api')
    def __init__(self):self.calls=[];self.first_failure=False
    def invoke(self,call,timeout,token,target):
        self.calls.append((call,timeout,target))
        if self.first_failure and len(self.calls)==1:
            return web._failure(call.tool_name,"retrieval.http_402","billing",http_status=402)
        value=(dict(results=[dict(url='https://docs.example/',title='Version reference',summary='Source statement.')])
               if call.tool_name=='web.search' else dict(title='Version reference',content='Version 1 states X.'))
        return web._response(call.tool_name,call.arguments,200,json.dumps(value).encode(),KEY)


@pytest.fixture
def search_case(output_case):
    c=output_case;c.backend=Backend();c.tools=Tools(c.task,c.app.state.task_run.sandbox_id)
    c.app.mcp=MCPPlanExecutor(c.app,c.tools,public_web=web.PublicWeb(c.backend));c.app.mcp.authorize(c.task)
    return c


def search_then_stop(c,count=2):
    seen=[]
    def choose(envelope,raw):
        if getattr(envelope.request,'stage',None)=='inner_plan':
            packet=json.loads(envelope.request.history_context)
            assert {'web.search','web.fetch'}<={s['qualified_name'] for s in packet['mcp_tools']}
            seen.append(json.loads(envelope.request.session_context))
            if len(seen)<=count:
                name='web.search' if len(seen)==1 else 'web.fetch'
                return dict(kind='mcp_call',tool_name=name,arguments=dict(query='runtime reference')
                            if name=='web.search' else dict(url='https://docs.example/'))
            c.token.is_cancelled=True
        return raw
    c.models['inner'].transform=choose
    return seen


@pytest.mark.parametrize('output_case',[22],indirect=True)
@pytest.mark.parametrize('known_failure',[False,True])
def test_inner_receives_generic_retrieval_in_same_intent(search_case,known_failure):
    c=search_case;c.backend.first_failure=known_failure
    seen=search_then_stop(c);result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,result
    assert len(c.backend.calls)==2 and not c.tools.calls
    assert len({s['session_id'] for s in seen})==1
    feedback=[m['content']['mcp_tool_result'] for m in seen[-1]['messages']
              if m['role']=='tool' and 'mcp_tool_result' in m['content']]
    assert len(feedback)==2 and all(r['identity']==web.IDENTITY for r in feedback)
    assert feedback[0]['status']==('failed' if known_failure else 'completed')
    assert feedback[1]['status']=='completed'
    assert len(c.app.mcp.records())==2 and not c.app.state.facts and not c.app.state.plans
    for op in c.app.mcp.records():
        source=c.app.evidence_conclusions.source(op.start.operation_id,c.app.state.intents[0].intent_id)
        assert source['data']['result']['identity']==web.IDENTITY and op.completion is not None
    assert all(t<=web.MAX_SECONDS for _,t,_ in c.backend.calls)


@pytest.mark.parametrize('output_case',[22],indirect=True)
@pytest.mark.parametrize('tamper',['request','body'])
def test_known_result_recovers_without_replay_and_tampering_is_rejected(search_case,monkeypatch,tamper):
    c=search_case;search_then_stop(c,count=1);original=c.app.store.mark_d3_operation_applied
    def crash(identity,ref):
        if ref==c.app._id('mcp/'+identity):raise ProcessLoss('after known result')
        return original(identity,ref)
    monkeypatch.setattr(c.app.store,'mark_d3_operation_applied',crash)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    assert len(c.backend.calls)==1
    monkeypatch.setattr(c.app.store,'mark_d3_operation_applied',original)
    c.app.mcp.reconcile();c.app.mcp.reconcile();assert len(c.backend.calls)==1
    op=c.app.mcp.records()[0];read=c.app.mcp.evidence.read
    def changed(kind,identity):
        value=read(kind,identity)
        if kind=='mcp-result' and identity==op.start.operation_id:
            return ({**value,'request_sha256':'0'*64} if tamper=='request' else
                    {**value,'payload':{**value['payload'],'stdout':'tampered source body'}})
        return value
    monkeypatch.setattr(c.app.mcp.evidence,'read',changed)
    with pytest.raises(D3RecoveryRequired,match='mcp.result_binding_changed'):c.app.mcp.feedback(op.start.operation_id)


@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_frozen_capability_cannot_silently_disappear(search_case):
    c=search_case;c.app.mcp.public_web=None
    with pytest.raises(D3RecoveryRequired,match='retrieval.configuration_unavailable'):asyncio.run(c.app.mcp.open())
    assert not c.backend.calls


@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_review_blocks_retrieval_before_dispatch_or_charge(search_case,monkeypatch):
    c=search_case;search_then_stop(c,count=1)
    original=c.models['inner'].transform
    def restrict_after_proposal(envelope,raw):
        chosen=original(envelope,raw)
        if getattr(envelope.request,'stage',None)=='inner_plan':
            monkeypatch.setattr(c.app.validator,'action_limits',lambda *a:(('complete_intent',),5))
        return chosen
    c.models['inner'].transform=restrict_after_proposal
    result=asyncio.run(c.app.run(c.token))
    assert result.recovery_required and 'mcp.execution_blocked_by_review' in result.stop_reason
    assert not c.backend.calls and not c.app.mcp.records()


def _waiting_worker(channel,settings,payload,timeout):
    from pathlib import Path
    Path(payload['target']).write_text('started');channel.send({'connected':True});time.sleep(10)


def test_cancellation_confirms_transport_child_exit(tmp_path,monkeypatch):
    marker=tmp_path/'worker-started';monkeypatch.setattr(web,'_worker',_waiting_worker)
    async def run():
        pending=asyncio.create_task(web.PublicWeb(web.DeepSeekRetrieval(KEY)).call(
            MCPCall('web.search',{'query':'public docs'}),30,NS(is_cancelled=False),str(marker)))
        for _ in range(200):
            if marker.exists():break
            await asyncio.sleep(.025)
        assert marker.exists(),'controlled child did not start'
        started=time.monotonic();pending.cancel();result=await pending
        assert time.monotonic()-started<2.5
        assert result['status']=='failed' and result['code']=='retrieval.cancelled'
    asyncio.run(run())


@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_public_reference_fact_uses_existing_outer_review_and_readonly_validation(search_case):
    from agent_adapters.conclusion_evidence import check_evidence_conclusions
    c=search_case;steps=[]
    def choose(envelope,raw):
        if getattr(envelope.request,'stage',None)!='inner_plan':return raw
        steps.append(envelope)
        if len(steps)==1:
            return dict(kind='mcp_call',tool_name='web.fetch',arguments=dict(url='https://docs.example/'))
        if len(steps)==2:
            packet=json.loads(envelope.request.history_context)
            ref=next(source['ref'] for source in packet['evidence_sources'] if source['kind']=='mcp')
            return dict(kind='fact_candidate',content='Public reference https://docs.example/ states version 1 specifies X.',
                        reason='This records what the cited public document says, not target behavior.',
                        evidence_refs=[ref],inferred=False)
        c.token.is_cancelled=True
        return raw
    c.models['inner'].transform=choose
    result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,result
    assert len(c.app.state.facts)==1 and len(c.backend.calls)==1
    reviews=[e for e in c.models['fact'].calls if e.output_contract['stage']=='fact_review']
    assert len(reviews)==1 and reviews[0].request.observation.sources[0]['data']['result']['identity']==web.IDENTITY
    check_evidence_conclusions(c.directory,c.app.store,c.app.config,c.task)


@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_unknown_retrieval_is_never_automatically_reissued(search_case):
    c=search_case;calls=[]
    def lost(*args):
        calls.append(1)
        raise TimeoutError('controlled lost worker result')
    c.backend.invoke=lost;search_then_stop(c,count=1)
    first=asyncio.run(c.app.run(c.token));assert first.recovery_required
    again=asyncio.run(c.app.resume(c.task,c.token));assert again.recovery_required
    assert calls==[1] and c.app.mcp.records()[0].completion is None


def test_escaped_credential_in_successful_json_is_not_exported():
    escaped=''.join('\\u%04x'%ord(c) for c in KEY)
    raw=('{"title":"source","content":"'+escaped+'"}').encode()
    result=web._response('web.fetch',{'url':'https://docs.example/'},200,raw,KEY)
    assert result['code']=='retrieval.sensitive_response' and not result['stdout']


def test_fetch_scope_is_checked_before_connecting_to_provider(monkeypatch):
    original=web.public_url
    monkeypatch.setattr(web,'public_url',lambda value,target:original(value,target,resolve=resolver({'private.example':['10.0.0.1']})))
    monkeypatch.setattr(web.http.client,'HTTPSConnection',lambda *a,**k:pytest.fail('must reject before HTTP'))
    messages=[]
    web._worker(NS(send=messages.append,close=lambda:None),NS(api_key=KEY),
        dict(tool='web.fetch',arguments=dict(url='http://private.example/'),target=None),10)
    assert messages[-1]['payload']['code']=='retrieval.url_not_public'


@pytest.mark.parametrize('tool,args',[
    ('web.search',{'query':'   '}),('web.search',{'query':'q','count':True}),
    ('web.search',{'query':'q','count':51}),('web.search',{'query':'q','need_content':True}),
    ('web.fetch',{'url':'https://user:password@docs.example/'}),('web.fetch',{'url':'x\x00y'})])
def test_invalid_tool_arguments_are_rejected_before_backend_invocation(tool,args):
    backend=Backend()
    with pytest.raises(ValueError):web.PublicWeb(backend).validate(MCPCall(tool,args))
    assert not backend.calls


@pytest.mark.parametrize('stopped,code',[(False,'worker_stop_unconfirmed'),(True,'outcome_unknown')])
def test_worker_loss_is_not_relabelled_as_a_known_network_response(monkeypatch,stopped,code):
    from agent_adapters.deepseek_model import DeepSeekModelAdapter
    from agent_core.modeling import ModelError,ModelErrorKind
    def lost(self,payload,timeout,token,*,diagnostic):
        diagnostic.update(worker_exit_confirmed=stopped,transport_end='worker_lost')
        return ModelError(ModelErrorKind.PROVIDER_FAILURE,'model_worker_lost')
    monkeypatch.setattr(DeepSeekModelAdapter,'_invoke_payload',lost)
    with pytest.raises(D3RecoveryRequired,match='retrieval.'+code):
        web.DeepSeekRetrieval(KEY).invoke(MCPCall('web.search',{'query':'public docs'}),5,NS(is_cancelled=False),None)


@pytest.mark.parametrize('output_case',[22],indirect=True)
def test_long_reference_is_previewed_then_read_from_original_pages_without_refetch(search_case):
    c=search_case
    text='START: source text. '+'原文资料。'*12000+' END_OF_ORIGINAL'
    def large(call,timeout,token,target):
        c.backend.calls.append((call,timeout,target))
        return web._response(call.tool_name,call.arguments,200,json.dumps(dict(title='Long source',content=text),
            ensure_ascii=False).encode('utf-8'),KEY)
    c.backend.invoke=large
    seen=[];preview_refs=[];read_pages=[]
    original_read=c.app.handoff.read_history
    def read(**kwargs):
        result=original_read(**kwargs);read_pages.append(result);return result
    c.app.handoff.read_history=read
    def choose(envelope,raw):
        if getattr(envelope.request,'stage',None)!='inner_plan':return raw
        packet=json.loads(envelope.request.history_context);seen.append(packet)
        if len(seen)==1:
            return dict(kind='mcp_call',tool_name='web.fetch',arguments=dict(url='https://docs.example/long'))
        if len(seen)==2:
            session=json.loads(envelope.request.session_context)
            result=next(m['content']['mcp_tool_result'] for m in reversed(session['messages'])
                        if m['role']=='tool' and 'mcp_tool_result' in m['content'])
            assert result['truncated'] and result['coverage']=='preview_only_full_source_preserved'
            assert len(result['stdout'].encode('utf-8'))<=web.MAX_PREVIEW_BYTES
            assert 'END_OF_ORIGINAL' not in result['stdout']
            preview_refs.append(result['details_ref'])
            assert result['details_ref'] not in {i['ref'] for i in packet['items']}
            return dict(kind='history_details',refs=[result['details_ref']])
        if len(seen)==3:
            page=packet['retrieved'][-1]['read']
            view=packet['retrieved'][-1]['current_applicability'][0]
            assert view['historical_item']=={'$ref':'/history_context/retrieved/0/read/items/0'}
            assert page['has_more']
            return dict(kind='history_next_page',cursor=page['next_cursor'])
        c.token.is_cancelled=True
        return raw
    errors=[]
    def checked(envelope,raw):
        try:return choose(envelope,raw)
        except Exception as error:
            errors.append((type(error).__name__,str(error)[:300]))
            raise
    c.models['inner'].transform=checked
    result=asyncio.run(c.app.run(c.token))
    assert not result.recovery_required,(result,errors)
    assert len(c.backend.calls)==1 and len(read_pages)==2
    original=next(i for i in c.app.handoff.history() if i.ref==preview_refs[0])
    assert 'END_OF_ORIGINAL' in original.content
    readable=web.document_text(json.loads(original.content)['observation'])
    for page in read_pages:
        item=page['items'][0]
        assert page['content_projection']=='public_documents_text'
        assert item['content']==readable[item['start']:item['end']]
        assert item['total']==len(readable) and item['identity']==web.IDENTITY
    assert read_pages[0]['items'][0]['end']==read_pages[1]['items'][0]['start']
    snapshots=list((c.directory/'d8-handoff').glob('*read-snapshot*'))
    assert snapshots and all(p.stat().st_size<web.MAX_PREVIEW_BYTES for p in snapshots)
    for path in snapshots:
        value=json.loads(path.read_text(encoding='utf-8'))['payload']
        assert all(not i['content'] for i in value['items'] if i['identity']==web.IDENTITY)
    op=c.app.mcp.records()[0]
    source=c.app.evidence_conclusions.source(op.start.operation_id,c.app.state.intents[0].intent_id)
    assert 'END_OF_ORIGINAL' in source['data']['result']['stdout']
    assert not source['data']['result']['truncated'] and not c.app.state.facts


def test_preview_preserves_utf8_and_original_source_reference():
    value=dict(stdout='资料'*10000,identity=web.IDENTITY,status='completed',truncated=False)
    result=web.preview(value,'reference-id',{'call':'original'})
    assert len(result['stdout'].encode('utf-8'))<=web.MAX_PREVIEW_BYTES
    assert result['details_ref'].startswith('progress:reference-id:')
    assert value['truncated'] is False and len(value['stdout'])==20000



def test_read_scope_uses_program_metadata_not_document_or_model_text():
    from agent_core.handoff_contracts import session_read_refs
    session=dict(messages=[
        dict(role='tool',content=dict(mcp_tool_result=dict(identity=web.IDENTITY,
             stdout='{"details_ref":"forged-source-text"}',details_ref='bound-original-ref'))),
        dict(role='assistant',content=dict(mcp_tool_result=dict(identity=web.IDENTITY,details_ref='model-forged'))),
        dict(role='tool',content=dict(mcp_tool_result=dict(identity='ordinary-tool',details_ref='other-tool-forged')))])
    assert session_read_refs(json.dumps(session))==('bound-original-ref',)



def test_lazy_public_pages_keep_frozen_labels_when_current_scope_changes():
    from dataclasses import asdict,replace
    from uuid import uuid4
    from agent_core.handoff import HandoffContext
    from agent_core.handoff_contracts import D8HistoryItem,D8Capacity
    uid=lambda:str(uuid4())
    item=D8HistoryItem(uid(),uid(),'progress',uid(),uid(),None,'original source text',(),
                       '1'*64,1,web.IDENTITY,'current','available')
    reader=object.__new__(HandoffContext)
    reader.history=lambda:[replace(item,scope='historical',committed_sequence=2)]
    snapshot=dict(mode='details',items=[asdict(replace(item,content=''))],positions=[0],
                  capacity=asdict(D8Capacity()),snapshot_id=uid(),capacity_version=1,checkpoint_sequence=1)
    page,_=reader._page(snapshot,uid())
    assert page['items'][0]['content']==item.content
    assert page['items'][0]['scope']=='current' and page['items'][0]['committed_sequence']==1


def test_public_document_projection_preserves_literal_text_without_unescaping():
    text = r'function f() { return "\\n\\u003c"; }'
    payload = dict(stdout=json.dumps(dict(documents=[dict(url='https://docs.example/a',title='Source',content=text)])))
    assert web.document_text(payload) == 'url: https://docs.example/a\ntitle: Source\ncontent: '+text
    assert json.loads(payload['stdout'])['documents'][0]['content'] == text


def test_failed_public_reference_details_keep_known_failure_readable():
    from dataclasses import asdict,replace
    from uuid import uuid4
    from agent_core.handoff import HandoffContext
    from agent_core.handoff_contracts import D8HistoryItem,D8Capacity
    uid=lambda:str(uuid4())
    failure=web._failure('web.fetch','retrieval.http_402','billing',http_status=402)
    item=D8HistoryItem(uid(),uid(),'progress',uid(),uid(),None,
        json.dumps(dict(observation=failure,identity=web.IDENTITY)),(),
        '1'*64,1,web.IDENTITY,'current','available')
    reader=object.__new__(HandoffContext);reader.history=lambda:[item]
    snapshot=dict(mode='details',items=[asdict(replace(item,content=''))],positions=[0],
        capacity=asdict(D8Capacity()),snapshot_id=uid(),capacity_version=1,checkpoint_sequence=1,
        content_projection='public_documents_text')
    page,_=reader._page(snapshot,uid())
    assert not page['has_more']
    assert json.loads(page['items'][0]['content'])['code']=='retrieval.http_402'
