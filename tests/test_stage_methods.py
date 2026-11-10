"""Reviewed methods keep Fact truth separate from executable reuse."""
import asyncio
from dataclasses import replace
import pytest

from test_stage_application import case
from agent_core.stage_methods import StageMethods, resolve_method_call
from agent_core.stage_planning import stage_runtime, schema
from agent_core.stage_contracts import PlanningSession
from agent_core.stage_storage import StageStateError


@pytest.mark.parametrize('verdict', ['accept', 'unavailable', None])
def test_method_fact_and_reuse_are_independently_reviewed(case, verdict):
    inner = case.models.inner.transform
    def declare(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [],
                             'conditions': 'The same controlled input and runtime.'}
        return inner(request, raw)
    def assess(request, raw):
        if getattr(request.proposal, 'method', None) and verdict:
            raw['method_decision'] = verdict
        return raw
    case.models.inner.transform = declare
    case.models.outer.transform = assess
    asyncio.run(case.app.run(case.token))
    facts = [i for i in case.app.evidence.history(case.app.task_id) if i.record_type == 'verified_fact']
    assert facts
    methods = StageMethods(case.app.evidence, case.app.task_id).catalog()
    assert bool(methods) == (verdict == 'accept')
    assert len(case.tools.calls) == 1  # Registration never reruns the tool.
    if methods:
        method = methods[0]
        turn = case.app.store.list_stage_turns(case.app.task_id)[0]
        request = case.app._request('stage_action', PlanningSession('road_execution', turn.roads[0].road_id))
        from agent_adapters.deepseek_roles import _stage_input
        view = _stage_input(request)['current']['methods'][0]
        assert view['ref'] == method['ref'] and 'call' not in view and 'task_run_id' not in view
        assert method['fact_ref'] == facts[0].ref
        assert method['call']['arguments']['script'] == 'printf observed'
        assert resolve_method_call(request, method['ref']).arguments == {'script': 'printf observed'}
        details = case.app.evidence.history_tool_requests((facts[0],))
        assert details[0]['method']['call'] == method['call']
        assert any(c['properties']['kind']['const'] == 'reuse_method' for c in schema(request)['anyOf'])
        wrong = replace(request, task_run_id='another-task')
        with pytest.raises(StageStateError, match='stage.method_unavailable'):
            resolve_method_call(wrong, method['ref'])
        missing = replace(request, tools=())
        with pytest.raises(StageStateError, match='stage.method_tool_unavailable'):
            resolve_method_call(missing, method['ref'])

def test_pinned_script_keeps_executed_bytes_and_checks_input_versions():
    import base64
    import hashlib
    from types import SimpleNamespace
    from agent_adapters.method_inputs import capture_inputs
    from agent_adapters.mcp_plan_executor import ToolArgumentError
    from agent_core.stage_contracts import StagePinnedCall
    class Evidence:
        def __init__(self): self.values = {}
        def read(self, kind, identity): return self.values.get((kind, identity))
        def save(self, kind, identity, value): self.values[kind, identity] = value
    files = {'/workspace/work/main.py': b"result = 7\n", '/workspace/work/input.json': b'{"x": 1}'}
    service = SimpleNamespace(read_image=lambda task, path: files[path])
    evidence = Evidence()
    call = StagePinnedCall('pwntools.run_pwntools', {'script_path': '/workspace/work/main.py'}, tuple(files))
    claim = {'task_run_id': 'task', 'call': {'tool_name': call.tool_name, 'arguments': call.arguments}}
    args = asyncio.run(capture_inputs(service, evidence, 'execution', claim, call))
    files['/workspace/work/main.py'] = b'result = 99\n'
    scope = {}
    exec(args['script'], scope)
    assert scope['result'] == 7 and scope['__file__'] == '/workspace/work/main.py'
    record = evidence.read('mcp-method-inputs', 'execution')
    assert base64.b64decode(record['files'][0]['base64']) == b'result = 7\n'
    expected = hashlib.sha256(files['/workspace/work/input.json']).hexdigest()
    assert record['resolved_call']['expected_files'] == [['/workspace/work/input.json', expected]]
    files['/workspace/work/input.json'] = b'{"x": 2}'
    repeated = StagePinnedCall(call.tool_name, args, ('/workspace/work/input.json',),
                               (('/workspace/work/input.json', expected),))
    with pytest.raises(ToolArgumentError, match='method.input_version_changed'):
        asyncio.run(capture_inputs(service, evidence, 'new-execution', claim, repeated))
    assert evidence.read('mcp-method-inputs', 'new-execution') is None


def test_method_dependencies_are_explicit_and_do_not_reexecute(case):
    def declare(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [], 'conditions': 'controlled input'}
        if raw.get('kind') == 'road_resolution':
            methods = stage_runtime(request).get('methods', ())
            if len(methods) == 1:
                source = methods[0]['source_ref']
                return {'kind': 'fact_candidate', 'content': 'A second supported property.', 'reason': 'same controlled observation',
                        'support_refs': [source], 'inferred': False,
                        'method': {'source_ref': source, 'dependencies': [methods[0]['ref']], 'conditions': 'requires first method output'}}
        return raw
    def assess(request, raw):
        if getattr(request.proposal, 'method', None): raw['method_decision'] = 'accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = declare, assess
    asyncio.run(case.app.run(case.token))
    methods = StageMethods(case.app.evidence, case.app.task_id).catalog()
    assert len(methods) == 2
    assert methods[1]['dependencies'] == [methods[0]['ref']]
    assert len(case.tools.calls) == 1

def test_reuse_method_dispatches_a_new_bound_call(case):
    repeated = []
    def act(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [], 'conditions': 'controlled input'}
        if raw.get('kind') == 'road_resolution' and not repeated:
            methods = stage_runtime(request).get('methods', ())
            if methods:
                repeated.append(methods[0]['ref'])
                return {'kind': 'reuse_method', 'method_ref': methods[0]['ref']}
        return raw
    def review(request, raw):
        if getattr(request.proposal, 'method', None): raw['method_decision'] = 'accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = act, review
    asyncio.run(case.app.run(case.token))
    assert len(repeated) == 1
    assert case.tools.calls == [('shell.run_bash', {'script': 'printf observed'})] * 2
    tools = [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == 'tool_dispatch']
    assert len(tools) == 2 and tools[0].start.operation_id != tools[1].start.operation_id

def test_register_method_after_fact_does_not_create_another_fact(case):
    registrations = []
    def act(request, raw):
        if raw.get('kind') == 'road_resolution' and not registrations:
            fact = next(i for i in request.context.history if i.record_type == 'verified_fact')
            source = next(i.ref for i in request.context.history if i.identity == 'tool_observation_not_fact')
            registrations.append(fact.ref)
            return {'kind': 'register_method', 'fact_ref': fact.ref, 'reason': 'preserve the working method',
                    'method': {'source_ref': source, 'dependencies': [], 'conditions': 'same controlled input'}}
        return raw
    case.models.inner.transform = act
    asyncio.run(case.app.run(case.token))
    facts = [i for i in case.app.evidence.history(case.app.task_id) if i.record_type == 'verified_fact']
    assert len(facts) == 1
    methods = StageMethods(case.app.evidence, case.app.task_id).catalog()
    assert len(methods) == 1 and methods[0]['fact_ref'] == registrations[0]
    assert len(case.tools.calls) == 1

def test_registered_method_survives_restart(case):
    from agent_adapters.stage_application import StageApplication
    requests = []
    def act(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [], 'conditions': 'controlled input'}
        return raw
    def assess(request, raw):
        if getattr(request.proposal, 'method', None): raw['method_decision'] = 'accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    task_id, directory = case.app.task_id, case.app.directory
    before = StageMethods(case.app.evidence, task_id).catalog()
    asyncio.run(case.app.close())
    case.app = StageApplication(directory, **case.args)
    case.app.bind(task_id)
    after = StageMethods(case.app.evidence, task_id).catalog()
    assert after == before and len(after) == 1
    assert len(case.tools.calls) == 1


def test_batch_accepts_facts_but_only_one_method(case):
    from agent_core.stage_contracts import StageFactBatch
    def act(request, raw):
        if raw.get('kind') == 'fact_candidate':
            item = {k: v for k, v in raw.items() if k != 'kind'}
            item['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [], 'conditions': 'controlled input'}
            return {'kind': 'fact_candidates', 'candidates': [item, {**item, 'content': 'A second observation property.'}]}
        return raw
    def assess(request, raw):
        if isinstance(request.proposal, StageFactBatch):
            return {'decisions': [{'decision': 'accept', 'reason': 'supported', 'method_decision': verdict}
                                  for verdict in ['accept', 'unavailable']]}
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    assert len([i for i in case.app.evidence.history(case.app.task_id) if i.record_type == 'verified_fact']) == 2
    assert len(StageMethods(case.app.evidence, case.app.task_id).catalog()) == 1
    assert len(case.tools.calls) == 1


def test_materialization_restores_dependency_files_without_running_methods():
    import base64
    import hashlib
    from agent_core.stage_methods import method_artifacts, resolve_materialization
    from types import SimpleNamespace
    data = b'old input'
    artifact = {'path': '/workspace/work/decoded.bin', 'sha256': hashlib.sha256(data).hexdigest(),
                'base64': base64.b64encode(data).decode()}
    base = dict(task_run_id='task', dependencies=[], call={'tool_name': 'shell.run_bash', 'arguments': {'script': 'echo never-run'}}, files=[artifact])
    methods = [{**base, 'ref': 'one'}, {**base, 'ref': 'two', 'dependencies': ['one'], 'files': []}]
    result = method_artifacts(methods, 'two')
    assert next(f for f in result['files'] if f['path'] == artifact['path'])['base64'] == artifact['base64']
    assert all(f['materialized_path'].startswith(result['root'] + '/') for f in result['files'])
    request = SimpleNamespace(task_run_id='task', tools=[{'qualified_name': 'shell.run_bash'}],
                              execution_context={'stage_runtime': {'methods': methods}})
    call = resolve_materialization(request, 'two')
    assert call.materialize_ref == 'two' and call.tool_name == 'shell.run_bash'
    methods[0]['dependencies'] = ['two']
    with pytest.raises(StageStateError, match='stage.method_dependency_cycle'):
        method_artifacts(methods, 'two')

@pytest.mark.parametrize('writable', [False, True])
def test_materialization_worker_preserves_saved_files_and_work_copies(tmp_path, capsys, writable):
    import base64, hashlib, json, os, stat, zlib
    from types import SimpleNamespace
    from agent_core.stage_methods import MATERIALIZE_WORKER
    # Exercise descriptor-relative traversal on a real temporary filesystem.
    # Windows lacks dir_fd; this adapter translates only that OS interface.
    directories = {}
    class OS:
        O_RDONLY, O_WRONLY, O_CREAT, O_EXCL, O_NONBLOCK = os.O_RDONLY, os.O_WRONLY, os.O_CREAT, os.O_EXCL, getattr(os, "O_NONBLOCK", 0)
        O_DIRECTORY, O_NOFOLLOW = 1 << 28, 1 << 29
        fdopen, fstat, fsync = staticmethod(os.fdopen), staticmethod(os.fstat), staticmethod(os.fsync)
        def location(self, name, parent):
            return tmp_path if name == '/' else directories[parent] / name
        def open(self, name, flags, mode=0o777, *, dir_fd=None):
            path = self.location(name, dir_fd)
            if path.is_symlink(): raise ValueError('symlink')
            if flags & self.O_DIRECTORY:
                if not path.is_dir(): raise NotADirectoryError()
                fd = 100000 + len(directories)
                directories[fd] = path
                return fd
            return os.open(path, flags & ~(self.O_DIRECTORY | self.O_NOFOLLOW), mode)
        def mkdir(self, name, mode=0o777, *, dir_fd):
            self.location(name, dir_fd).mkdir(mode=mode)
        def close(self, fd):
            if fd in directories: directories.pop(fd)
            else: os.close(fd)
    # Use monotonic pseudo descriptors even after close, matching real fd ownership.
    proxy = OS()
    original_open = proxy.open
    next_fd = [100000]
    def open_fd(name, flags, mode=0o777, *, dir_fd=None):
        if flags & proxy.O_DIRECTORY:
            path = proxy.location(name, dir_fd)
            if path.is_symlink() or not path.is_dir(): raise ValueError('bad directory')
            next_fd[0] += 1; directories[next_fd[0]] = path
            return next_fd[0]
        return original_open(name, flags, mode, dir_fd=dir_fd)
    proxy.open = open_fd
    data = b'unchanged cached output'
    item = {'materialized_path': '/workspace/work/.methods/test/workspace/work/data.bin',
            'base64': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}
    manifest = {'root': '/workspace/work/.methods/test', 'files': [item], 'writable': writable}
    def run():
        payload = base64.b64encode(zlib.compress(json.dumps(manifest).encode())).decode()
        scope = dict(base64=base64, hashlib=hashlib, json=json, os=proxy, stat=stat, zlib=zlib,
                     sys=SimpleNamespace(argv=['worker', payload]))
        exec(MATERIALIZE_WORKER.split('\n', 1)[1], scope)
    run(); run()
    path = tmp_path / item['materialized_path'].lstrip('/')
    assert path.read_bytes() == data and not directories
    os.chmod(path, 0o600); path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='method.materialization_conflict'): run()
    assert path.read_bytes() == b'changed' and not directories
    capsys.readouterr()

def test_method_reuse_across_successor_turn_keeps_original_fact_owner(case):
    first_turn, successor, reused = [], [], []
    def act(request, raw):
        if request.stage == 'stage_action' and not first_turn:
            first_turn.append(request.context.turn.turn_id)
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [], 'conditions': 'unchanged controlled input'}
        if request.stage == 'stage_continuation' and not successor:
            successor.append(True)
            return {'kind': 'turn_intent', 'description': 'recheck the saved method in a successor scope',
                    'completion_criterion': 'a new controlled execution returns the same result', 'support_refs': raw['support_refs']}
        if request.stage == 'stage_action' and request.context.turn.turn_id != first_turn[0] and not reused:
            method = stage_runtime(request)['methods'][0]
            reused.append(method['ref'])
            return {'kind': 'reuse_method', 'method_ref': method['ref']}
        return raw
    def assess(request, raw):
        if getattr(request.proposal, 'method', None): raw['method_decision'] = 'accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    assert len(case.tools.calls) == 2 and len(reused) == 1
    records = [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == 'tool_dispatch']
    claims = [case.app.evidence.read('mcp-call', r.start.operation_id) for r in records]
    assert claims[0]['intent_id'] != claims[1]['intent_id']
    fact = next(i for i in case.app.evidence.history(case.app.task_id) if i.record_type == 'verified_fact')
    assert fact.turn_id == first_turn[0] and fact.intent_id == claims[0]['intent_id']


def test_materialize_method_is_a_fresh_tool_call_not_upstream_replay(case):
    materialized = []
    def act(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref': raw['support_refs'][0], 'dependencies': [], 'conditions': 'controlled input'}
        if raw.get('kind') == 'road_resolution' and not materialized:
            materialized.append(True)
            return {'kind': 'materialize_method', 'method_ref': stage_runtime(request)['methods'][0]['ref']}
        return raw
    def assess(request, raw):
        if getattr(request.proposal, 'method', None): raw['method_decision'] = 'accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    assert len(case.tools.calls) == 2
    assert case.tools.calls[0][1]['script'] == 'printf observed'
    assert case.tools.calls[1][1]['script'].startswith('python3 -I -c ')

def test_pinned_file_execution_is_bound_to_receipt_and_reusable(case, monkeypatch):
    import base64
    from copy import deepcopy
    from types import SimpleNamespace
    spec = case.tools.specs[0]['input_schema']
    spec['properties']['script_path'] = {'type': 'string'}
    spec.pop('required')
    case.tools.specs[0]['qualified_name'] = 'pwntools.run_pwntools'
    # The controlled client advertises its one tool using the chosen name.
    case.tools.list_registered_tools = lambda: [SimpleNamespace(qualified_name='pwntools.run_pwntools')]
    case.tools.bind_agent = lambda *args, **kwargs: case.tools
    data = b"print('observed')\n"
    case.app.mcp.service = SimpleNamespace(read_image=lambda task, path: data)
    def act(request, raw):
        if raw.get('kind') == 'mcp_call':
            return {'kind':'mcp_call','tool_name':'pwntools.run_pwntools',
                    'arguments':{'script_path':'/workspace/work/main.py'}, 'pin_files':['/workspace/work/main.py']}
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref':raw['support_refs'][0], 'dependencies':[], 'conditions':'controlled input'}
        return raw
    def assess(request, raw):
        if getattr(request.proposal, 'method', None):
            import json
            body = next(json.loads(m['content']) for m in request.messages if '"method_tool_requests"' in m['content'])
            file, = body['method_tool_requests'][0]['captured_inputs']['files']
            assert file['content'] == "print('observed')\n" and 'base64' not in file
            raw['method_decision'] = 'accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    method = StageMethods(case.app.evidence, case.app.task_id).catalog()[0]
    assert 'script_path' not in method['call']['arguments']
    assert base64.b64decode(method['files'][0]['base64']) == data
    read = case.app.evidence.read
    def tampered(kind, identity):
        value = read(kind, identity)
        if kind == 'mcp-method-inputs' and value:
            value = deepcopy(value); value['files'][0]['base64'] = base64.b64encode(b'changed').decode()
        return value
    monkeypatch.setattr(case.app.evidence, 'read', tampered)
    with pytest.raises(StageStateError, match='stage.method_input_binding_changed'):
        StageMethods(case.app.evidence, case.app.task_id).catalog()

def test_dependency_materialization_keeps_two_versions_of_same_path_separate():
    import base64, hashlib
    from agent_core.stage_methods import method_artifacts
    def node(ref, content, deps):
        return {'ref':ref, 'dependencies':deps, 'call':{'tool_name':'shell.run_bash','arguments':{'script':'true'}},
                'files':[{'path':'/workspace/work/data.bin','base64':base64.b64encode(content).decode(),
                          'sha256':hashlib.sha256(content).hexdigest()}]}
    manifest = method_artifacts([node('one', b'old', []), node('two', b'new', ['one'])], 'two')
    versions = [f for f in manifest['files'] if f['path']=='/workspace/work/data.bin']
    assert {base64.b64decode(f['base64']) for f in versions} == {b'old', b'new'}
    assert len({f['materialized_path'] for f in versions}) == 2
    assert len({f['ref'] for f in versions}) == 2

def test_materialization_exports_parameter_files_and_correct_script_suffix():
    import base64, json
    from agent_core.stage_methods import method_artifacts
    call = {'tool_name':'crypto.lll', 'arguments':{'matrix':[[1, 2], [3, 4]]}}
    methods = [{'ref':'parameters', 'dependencies':[], 'files':[], 'call':call}]
    item = method_artifacts(methods, 'parameters')['files'][0]
    assert item['path'].endswith('.json') and json.loads(base64.b64decode(item['base64'])) == call
    methods[0]['call'] = {'tool_name':'shell.run_powershell', 'arguments':{'script':'Write-Output observed'}}
    item = method_artifacts(methods, 'parameters')['files'][0]
    assert item['path'].endswith('.ps1')

@pytest.mark.parametrize('source', [b'\xef\xbb\xbfresult = 7\n', b"# coding: latin-1\nresult = '\xe9'\n"])
def test_pinned_python_preserves_source_encoding(source):
    from types import SimpleNamespace
    from agent_core.stage_contracts import StagePinnedCall
    from agent_adapters.method_inputs import capture_inputs
    class Evidence:
        def read(self, kind, identity): return None
        def save(self, *args): pass
    path = '/workspace/work/source.py'
    call = StagePinnedCall('z3.solve_constraints', {'script_path':path}, (path,))
    args = asyncio.run(capture_inputs(SimpleNamespace(read_image=lambda *args: source), Evidence(), 'id',
                                      {'task_run_id':'task'}, call))
    scope = {}; exec(args['script'], scope)
    assert scope['result'] == (7 if source.startswith(b'\xef') else '\xe9')
    assert scope['_qy_source'] == source

def test_materialization_failure_keeps_classified_receipt(case):
    from dataclasses import replace
    sent = []
    original = case.tools.call_tool
    async def fail_materialization(name, arguments):
        response = await original(name, arguments)
        if arguments.get('script', '').startswith('python3 -I -c '):
            return replace(response, status='failed', exit_code=1, stderr='ValueError: method.materialization_conflict')
        return response
    case.tools.call_tool = fail_materialization
    def act(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['method'] = {'source_ref':raw['support_refs'][0], 'dependencies':[], 'conditions':'same input'}
        if raw.get('kind') == 'road_resolution' and not sent:
            sent.append(True)
            return {'kind':'materialize_method', 'method_ref':stage_runtime(request)['methods'][0]['ref']}
        return raw
    def assess(request, raw):
        if getattr(request.proposal, 'method', None):raw['method_decision']='accept'
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    operations = [o for o in case.app.store.list_d4_operations(case.app.task_id) if o.start.category=='tool_dispatch']
    result = case.app.evidence.read('mcp-result', operations[-1].start.operation_id)['payload']
    assert result['status']=='failed' and result['code']=='method.materialization_conflict'
    assert result['category']=='dependency'
    assert len(case.tools.calls)==2


def test_empty_catalog_collects_partial_capability_in_existing_fact_review(case):
    from agent_core.stage_contracts import StageFactProposal
    reviewed = []
    assert not StageMethods(case.app.evidence, case.app.task_id).catalog()
    def act(request, raw):
        if raw.get('kind') == 'fact_candidate':
            raw['content'] = 'The tool returned observed and exited successfully.'
        return raw
    def assess(request, raw):
        if type(request.proposal) is StageFactProposal:
            refs = stage_runtime(request)['method_candidates']['None']
            assert refs == list(request.proposal.support_refs)
            assert 'printf observed' in str(request.messages)
            raw['method'] = {'source_ref': refs[0], 'capability': 'Read the controlled stdout only.'}
            reviewed.append(request.model_request_id)
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    methods = StageMethods(case.app.evidence, case.app.task_id).catalog()
    assert len(reviewed) == len(methods) == 1
    assert methods[0]['review_operation_id'] == reviewed[0]
    assert methods[0]['capability'] == 'Read the controlled stdout only.'
    assert methods[0]['dependencies'] == [] and methods[0]['conditions'] == ''
    assert len(case.tools.calls) == 1
    assert not any(type(r.proposal).__name__ == 'StageMethodRegistration' for r in case.models.outer.calls)


def test_working_script_available_before_any_fact_without_certifying_it(case):
    from agent_core.stage_methods import resolve_working_script
    prepared = []
    def act(request, raw):
        scripts = stage_runtime(request).get('working_scripts', ())
        if raw.get('kind') == 'fact_candidate' and scripts and not prepared:
            assert not stage_runtime(request)['methods']
            from agent_adapters.deepseek_roles import _stage_input
            assert 'script' not in _stage_input(request)['current']['working_scripts'][0]
            prepared.append(scripts[0]['source_ref'])
            wrong = replace(request, task_run_id='another-task')
            with pytest.raises(StageStateError, match='stage.working_script_unavailable'):
                resolve_working_script(wrong, scripts[0]['source_ref'])
            return {'kind': 'prepare_working_script', 'source_ref': scripts[0]['source_ref']}
        return raw
    case.models.inner.transform = act
    asyncio.run(case.app.run(case.token))
    assert len(prepared) == 1 and len(case.tools.calls) == 2
    assert case.tools.calls[0][1]['script'] == 'printf observed'
    assert case.tools.calls[1][1]['script'].startswith('python3 -I -c ')
    assert not StageMethods(case.app.evidence, case.app.task_id).catalog()
    entries = StageMethods(case.app.evidence, case.app.task_id).working_scripts(case.app.evidence.history(case.app.task_id))
    assert len(entries) == 1 and entries[0]['script'] == 'printf observed'


def test_method_review_batch_drops_nonexistent_verdict_and_rejects_cross_candidate_source():
    from types import SimpleNamespace
    from agent_core.stage_contracts import StageFactProposal, StageMethodFact, StageSelectedMethodReview
    from agent_core.stage_methods import parse_method_review, method_review_counts
    from agent_core.stage_contracts import StageFactBatch, StageFactBatchReview
    first = StageMethodFact('First', 'observed', ('a',), method={'source_ref': 'a'})
    second = StageFactProposal('Second', 'observed', ('b',))
    request = SimpleNamespace(proposal=StageFactBatch((first, second)), execution_context={'stage_runtime': {
        'method_reuse': 'reviewed_same_task_v3', 'method_candidates': {'1': ['b']}}})
    raw = {'decision': 'accept', 'reason': 'supported', 'method_decision': 'accept'}
    results = StageFactBatchReview(tuple(parse_method_review(request, raw, claim, index)
        for index, claim in enumerate((first, second))))
    assert not hasattr(results.decisions[1], 'method_decision')
    assert method_review_counts(request, results, 1) == {'offered': 2, 'selected': 1, 'collected': 1}
    method = {'source_ref': 'a', 'capability': 'partial', 'conditions': 'same', 'dependencies': []}
    with pytest.raises(ValueError, match='stage.method_selection_invalid'):
        parse_method_review(request, {**raw, 'method': method}, second, 1)
    selected = parse_method_review(request, {'decision': 'accept', 'reason': 'supported',
        'method': {**method, 'source_ref': 'b'}}, second, 1)
    assert isinstance(selected, StageSelectedMethodReview)
    # Frozen earlier requests retain their original parsed fields and hashes.
    request.execution_context['stage_runtime']['method_reuse'] = 'reviewed_same_task_v2'
    assert parse_method_review(request, raw, second, 1).method_decision == 'accept'


def test_declared_data_input_uses_fresh_capture_and_keeps_code_and_original_fact(case):
    from types import SimpleNamespace
    from MCPs.Mcp_client.client import MCPExecutionResult
    from agent_core.stage_contracts import StageFactProposal
    tool = 'crypto.sympy'
    case.tools.specs = [{'qualified_name': tool, 'description': 'controlled expression', 'tags': ['crypto'],
        'input_schema': {'type': 'object', 'properties': {'expression': {'type': 'string'},
            'data_path': {'type': 'string'}}, 'required': ['expression', 'data_path'], 'additionalProperties': False}}]
    case.tools.list_registered_tools = lambda: [SimpleNamespace(qualified_name=tool)]
    case.tools.bind_agent = lambda *args, **kwargs: case.tools
    old, new = '/workspace/work/old.json', '/workspace/work/new.json'
    files = {old: b'{"n":7}', new: b'{"n":11}'}
    case.app.mcp.service = SimpleNamespace(read_image=lambda task, path: files[path])
    async def call_tool(name, arguments):
        case.tools.calls.append((name, arguments))
        return MCPExecutionResult(case.app.task_id, 'controlled-sandbox', name, 'python', (),
                                  'completed', 0, 'observed', '', False)
    case.tools.call_tool = call_tool
    reused = []
    def act(request, raw):
        if raw.get('kind') == 'mcp_call':
            return {'kind': 'mcp_call', 'tool_name': tool, 'arguments': {'expression': 'factorint(n)', 'data_path': old},
                    'pin_files': [old]}
        if raw.get('kind') == 'road_resolution' and not reused:
            method = stage_runtime(request)['methods'][0]
            with pytest.raises(StageStateError, match='stage.method_input_undeclared'):
                resolve_method_call(request, method['ref'], {'expression': '0'})
            with pytest.raises(StageStateError, match='stage.method_input_path_invalid'):
                resolve_method_call(request, method['ref'], {'data_path': '/etc/passwd'})
            reused.append(method['ref'])
            return {'kind': 'reuse_method', 'method_ref': method['ref'], 'inputs': {'data_path': new}}
        return raw
    def assess(request, raw):
        if type(request.proposal) is StageFactProposal:
            raw['method'] = {'source_ref': request.proposal.support_refs[0], 'capability': 'Factor supplied n.',
                'conditions': 'JSON integer n.', 'dependencies': [], 'parameters': ['data_path']}
        return raw
    case.models.inner.transform, case.models.outer.transform = act, assess
    asyncio.run(case.app.run(case.token))
    assert case.tools.calls == [(tool, {'expression': 'factorint(n)', 'data_path': path}) for path in (old, new)]
    method, = StageMethods(case.app.evidence, case.app.task_id).catalog()
    assert method['call']['arguments']['data_path'] == old
    operations = [op for op in case.app.store.list_d4_operations(case.app.task_id) if op.start.category == 'tool_dispatch']
    captures = [case.app.evidence.read('mcp-method-inputs', op.start.operation_id) for op in operations]
    assert [c['files'][0]['path'] for c in captures] == [old, new]
    assert captures[0]['files'][0]['sha256'] != captures[1]['files'][0]['sha256']
    assert len([i for i in case.app.evidence.history(case.app.task_id) if i.record_type == 'verified_fact']) == 1


def test_history_read_prioritizes_older_working_code_without_expanding_default_index():
    from types import SimpleNamespace
    history = [SimpleNamespace(ref=str(n), task_run_id='task', identity='tool_observation_not_fact',
                               turn_id='turn' + str(n), intent_id='road' + str(n)) for n in range(5)]
    methods = StageMethods(None, 'task')
    methods.source = lambda item, **kwargs: ({'call': {'tool_name': 'shell.run_bash',
                                                    'arguments': {'script': 'echo ' + item.ref}}}, None)
    assert [s['source_ref'] for s in methods.working_scripts(history)] == ['4', '3', '2']
    selected = methods.working_scripts(history, preferred=('0',))
    assert [s['source_ref'] for s in selected] == ['0', '4', '3']
