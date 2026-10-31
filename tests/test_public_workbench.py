"""Public D8 HTTP assembly with explicit Fake runtime boundaries; no external I/O."""
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_adapters import docker_profile,deepseek_roles,simulation,task_cli
from agent_adapters.workbench import Workbench,WorkbenchError
from agent_adapters.run_files import immutable_read
from agent_core.sqlite_control_store import SqliteControlStore
from test_http import http_case,Model,WebSandbox,Network,ORIGIN
from test_workbench_v3 import _challenge,_Process,_NoThread
from test_models import private_fixture


def test_public_real_http_assembly_and_keyless_cancel(http_case,tmp_path,monkeypatch):
    case=http_case
    seen=[]
    class RealBoundaryModel(Model):
        def __init__(self,role,settings):super().__init__(role)
        def bind_runtime(self,*args):pass
    sandbox=WebSandbox()
    sandbox._runtime=object()
    requests=[]
    def execute(sandbox_id,**kwargs):
        from agent_core.http_contracts import HttpRunConfig
        assert isinstance(kwargs['run_config'],HttpRunConfig)
        assert kwargs['authorization'].allowed_origin==ORIGIN
        requests.append(kwargs)
        return SimpleNamespace(status=200,headers=(),body=b'public ordinary response')
    sandbox.execute_d7_http=execute
    def connect(configuration,directory):
        seen.append(configuration)
        # Mirror connect(): resource owner supports HTTP; file workspace does not.
        return sandbox,SimpleNamespace(quiesce=lambda *a:None)
    monkeypatch.setattr(deepseek_roles,'D8DeepSeekModel',RealBoundaryModel)
    monkeypatch.setattr(docker_profile,'connect',connect)
    monkeypatch.setattr('Sandbox.target_network.T6TargetNetwork',lambda _:Network())
    env_file=private_fixture(tmp_path)
    monkeypatch.setattr(task_cli,'_input',lambda _:case.raw)
    args=SimpleNamespace(request='unused',mode='real',state_dir=tmp_path/'public-state',
        events=None,env_file=env_file,fixture=None)
    result=task_cli.dispatch_run_task(args)
    assert result['errors']==[] and not result['recovery']['required'],result
    assert result['contract_version']==20 and result['execution_profile']=='d8_v20'
    assert result['task_run']['status']=='awaiting_external_feedback'
    assert result['goal_evaluation']=='not_evaluated'
    assert len(result['verified_facts'])==1 and len(result['answer_candidates'])==1
    directory=args.state_dir/result['task_run_id']
    with SqliteControlStore.open_readonly(directory/'control.sqlite') as store:
        config=store.load_d3_run_config(result['task_run_id'])
        assert config.web_authorization.allowed_origin==ORIGIN
        assert config.web_authorization.allowed_methods==('GET','HEAD','OPTIONS')
        assert store.list_d8_tree_records(result['task_run_id'])
        assert store.list_d8_decisions(result['task_run_id'])
    declaration=immutable_read(directory/'run.json')
    env_file.unlink()
    monkeypatch.setattr(deepseek_roles,'D8DeepSeekModel',lambda *a:pytest.fail('cancel invoked model'))
    cancelled=simulation.run_simulated_task(directory,declaration,operation='cancel')
    assert cancelled['cleanup_status']=='confirmed',cancelled
    assert immutable_read(directory/'target-network-cleanup.json')['status']=='succeeded'
    monkeypatch.setattr(docker_profile,'connect',lambda *a:pytest.fail('query accessed Docker'))
    from agent_adapters.web.task_result import snapshot_v3_task_result
    assert snapshot_v3_task_result(directory,declaration)['cleanup_status']=='confirmed'
    assert len(seen)==2 and len(requests)==1


def test_workbench_additive_migration_preserves_rows_and_methods(tmp_path):
    root=tmp_path/'workbench'
    old=Workbench(root,protocol_version=3,execution_profile='d8_v19')
    challenge=old.create(_challenge(endpoint=ORIGIN))
    with sqlite3.connect(old.database) as db:
        db.execute('ALTER TABLE challenges DROP COLUMN http_methods_json')
    current=Workbench(root,protocol_version=3,execution_profile='d8_v20')
    migrated=current.get(challenge['id'])
    assert migrated['http_methods']==['GET','HEAD','OPTIONS','POST','PUT','PATCH']
    assert migrated['description']==challenge['description']
    updated=current.update(challenge['id'],_challenge(endpoint=ORIGIN)|{'http_methods':['GET','POST']})
    assert updated['http_methods']==['GET','POST']
    assert Workbench(root,protocol_version=3,execution_profile='d8_v20').get(challenge['id'])['http_methods']==['GET','POST']
    assert 'http_methods' not in old.get(challenge['id'])


@pytest.mark.parametrize('methods',[[],['TRACE'],['GET','GET'],'GET',[True]])
def test_workbench_rejects_invalid_methods_without_challenge_write(tmp_path,methods):
    current=Workbench(tmp_path,protocol_version=3,execution_profile='d8_v20')
    with pytest.raises(WorkbenchError,match='invalid_http_methods'):
        current.create(_challenge(endpoint=ORIGIN)|{'http_methods':methods})
    assert current.list()==[]


def test_workbench_freezes_authorized_methods_in_actual_cli_request(tmp_path,monkeypatch):
    workbench=Workbench(tmp_path,protocol_version=3,execution_profile='d8_v20')
    challenge=workbench.create(_challenge(endpoint=ORIGIN+'/')|{'http_methods':['GET','POST']})
    monkeypatch.setattr(deepseek_roles,'settings_from_file',lambda *a:None)
    monkeypatch.setattr('agent_adapters.workbench_runner.subprocess.Popen',lambda *a,**k:_Process())
    monkeypatch.setattr('agent_adapters.workbench_runner.threading.Thread',_NoThread)
    run=workbench.runner.start(challenge['id'],mode='real')
    raw=json.loads((Path(run['directory'])/'request.json').read_text('utf-8'))
    request,config=simulation.parse_request(raw)
    assert raw['execution_profile']=='d8_v20' and config.contract_version==20
    assert raw['authorization']['allowed_methods']==['GET','POST']
    assert request.authorization==raw['authorization']
    assert raw['task_request']['target']['endpoints']==[{'url':ORIGIN}]
    assert raw['d8_config']['http_timeout_seconds']==challenge['step_timeout_seconds']
    assert raw['dual_llm_config']['d4_budget']['total']==60
    assert 'api_key' not in json.dumps(raw)


@pytest.mark.parametrize('scenario',['simulated','mixed','old19'])
def test_workbench_rejects_unconfirmed_network_shapes(tmp_path,monkeypatch,scenario):
    profile='d8_v19' if scenario=='old19' else 'd8_v20'
    workbench=Workbench(tmp_path,protocol_version=3,execution_profile=profile)
    challenge=workbench.create(_challenge(endpoint=ORIGIN))
    if scenario=='mixed':workbench.add_attachment(challenge['id'],'input.txt',io.BytesIO(b'x'),1)
    monkeypatch.setattr(deepseek_roles,'settings_from_file',lambda *a:None)
    monkeypatch.setattr('agent_adapters.workbench_runner.subprocess.Popen',lambda *a,**k:pytest.fail('spawned'))
    with pytest.raises(WorkbenchError):
        workbench.runner.start(challenge['id'],mode='simulated' if scenario=='simulated' else 'real')


def test_new20_attachment_simulation_retains_prior_behavior(tmp_path,monkeypatch):
    from test_public_simulation import request,start
    raw=request(tmp_path)
    raw['execution_profile']='d8_v20'
    result,args=start(tmp_path,monkeypatch,raw)
    assert result['errors']==[] and not result['recovery']['required'],result
    assert result['contract_version']==20 and len(result['verified_facts'])==1
    assert result['goal_evaluation']=='not_evaluated'
    assert result['answer_candidates']==[]
    with SqliteControlStore.open_readonly(args.state_dir/result['task_run_id']/'control.sqlite') as store:
        assert store.load_d3_run_config(result['task_run_id']).web_authorization is None
