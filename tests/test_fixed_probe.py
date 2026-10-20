"""Contract11 model choices, trusted materialization and non-replayed recovery."""

import json
from types import SimpleNamespace

import pytest

from agent_adapters import cli, task_cli
from agent_adapters.simulation import SimulationModel
from agent_adapters.handoff_evidence import FileD8HandoffEvidence
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.run_files import immutable_read
from agent_adapters.web.task_result import snapshot_v3_task_result
from agent_core.sqlite_control_store import SqliteControlStore
from test_public_simulation import request


def run_fixed(tmp_path, monkeypatch, *, failure=None, twice=False, review=False):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v11'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    seen = []
    original = SimulationModel.invoke

    def invoke(model, envelope, *args):
        value = original(model, envelope, *args)
        if envelope.output_contract['stage'] not in {'inner_size', 'inner_sha256'}:
            return value
        seen.append(envelope)
        value['review_requested'] = review
        if failure and (twice or envelope.correction is None):
            if failure == 'stage': value['action']['operation'] = 'observe_sha256'
            elif failure == 'script': value['action']['script'] = 'PRIVATE_REJECTED_SCRIPT'
            elif failure == 'timeout': value['action']['timeout_seconds'] = 301
            elif failure == 'type': value['action']['timeout_seconds'] = '60'
            elif failure == 'kind': value['action']['kind'] = 'probe'
            elif failure == 'stdin': value['action']['stdin_base64'] = 'eA=='
            elif failure == 'directory': value['action']['working_directory'] = '/workspace/work'
            elif failure == 'conditions': value['action']['conditions'] = []
        return value

    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    result = task_cli.dispatch_run_task(args)
    directory = args.state_dir / result['task_run_id']
    return result, args, seen, directory


def test_public_choice_materializes_and_preserves_known_replay(tmp_path, monkeypatch):
    result, args, seen, directory = run_fixed(tmp_path, monkeypatch)
    assert result['errors'] == [], result
    assert result['contract_version'] == 11 and result['execution_profile'] == 'd8_v11'
    assert result['goal_evaluation'] == 'not_evaluated'
    assert len(result['verified_facts']) == 1 and len(seen) == 2
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    for envelope in seen:
        identity = envelope.request.model_request_id
        binding = output.read('fixed-probe-binding', identity)
        assert set(binding['wire_response']['action']) == {'kind', 'operation', 'timeout_seconds'}
        canonical = binding['canonical_response']['action']
        assert canonical['script'] == envelope.request.context.fixed_script
        assert canonical['working_directory'] == envelope.request.context.fixed_working_directory
        assert canonical['stdin_base64'] == ''
        assert len(canonical['conditions']) == (envelope.request.stage == 'inner_sha256')
    before = (directory / 'control.sqlite').read_bytes()
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('replay called model'))
    assert task_cli.dispatch_run_task(args)['task_run_id'] == result['task_run_id']
    assert (directory / 'control.sqlite').read_bytes() == before
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    assert cli.dispatch(args)['cleanup_status'] == 'confirmed'


@pytest.mark.parametrize('failure', ['stage', 'script', 'timeout', 'type', 'kind', 'stdin', 'directory', 'conditions'])
def test_bad_choice_is_safely_corrected_once(tmp_path, monkeypatch, failure):
    result, _, seen, directory = run_fixed(tmp_path, monkeypatch, failure=failure)
    assert result['errors'] == [], result
    assert len(result['verified_facts']) == 1
    size = [e for e in seen if e.request.stage == 'inner_size']
    assert len(size) == 2 and size[1].correction is not None
    assert size[1].correction['issues']
    assert size[1].correction['fixed_probe_policy']['max_timeout_seconds'] == size[0].request.context.max_timeout_seconds
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    assert output.read('fixed-probe-binding', size[0].request.model_request_id) is None
    assert output.read('fixed-probe-binding', size[1].request.model_request_id)
    assert 'PRIVATE_REJECTED_SCRIPT' not in ''.join(p.read_text(encoding='utf-8') for p in (directory / 'd8-output').glob('*.json'))


def test_two_wrong_choices_never_execute_or_gain_third_call(tmp_path, monkeypatch):
    result, _, seen, directory = run_fixed(tmp_path, monkeypatch, failure='script', twice=True)
    assert result['recovery']['required'] and len(seen) == 2
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert not store.load_control_state_rows(result['task_run_id']).executions


def test_action_review_still_uses_materialized_candidate(tmp_path, monkeypatch):
    result, _, seen, _ = run_fixed(tmp_path, monkeypatch, review=True)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    assert len(seen) == 2


@pytest.mark.parametrize('phase', ['before_binding', 'after_binding', 'native', 'completion'])
def test_interrupted_result_recovers_only_known_original_call(tmp_path, monkeypatch, phase):
    saved = FileD8OutputEvidence.save
    tripped = []

    def save(adapter, kind, identity, value):
        if kind == 'fixed-probe-binding' and not tripped:
            tripped.append(identity)
            if phase == 'before_binding': raise RuntimeError('simulated loss')
            checksum = saved(adapter, kind, identity, value)
            if phase == 'after_binding': raise RuntimeError('simulated loss')
            return checksum
        return saved(adapter, kind, identity, value)

    from agent_adapters.model_evidence import FileD3ModelEvidence
    native_save = FileD3ModelEvidence.save
    complete = SqliteControlStore.complete_d3_operation

    def save_native(adapter, evidence):
        if phase == 'native' and evidence.stage == 'inner_size':
            raise SystemExit('simulated native result loss')
        return native_save(adapter, evidence)

    def complete_call(store, completion):
        if phase == 'completion' and completion.operation_id in tripped:
            raise SystemExit('simulated completion loss')
        return complete(store, completion)

    monkeypatch.setattr(FileD8OutputEvidence, 'save', save)
    monkeypatch.setattr(FileD3ModelEvidence, 'save', save_native)
    monkeypatch.setattr(SqliteControlStore, 'complete_d3_operation', complete_call)
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v11'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    try:
        result = task_cli.dispatch_run_task(args)
        task_id = result['task_run_id']
        assert result['recovery']['required']
    except SystemExit:
        task_id = next(p.name for p in args.state_dir.iterdir() if (p / 'control.sqlite').exists())
    assert len(tripped) == 1
    monkeypatch.setattr(FileD8OutputEvidence, 'save', saved)
    monkeypatch.setattr(FileD3ModelEvidence, 'save', native_save)
    monkeypatch.setattr(SqliteControlStore, 'complete_d3_operation', complete)
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('recovery called model'))
    args.command, args.task_run_id = 'cancel', task_id
    cancelled = cli.dispatch(args)
    if phase == 'before_binding':
        assert cancelled['recovery']['required']
    else:
        assert cancelled['cleanup_status'] == 'confirmed', cancelled
        directory = args.state_dir / task_id
        native = FileD3ModelEvidence(directory / 'd3-models', create=False).read(tripped[0])
        assert native.outcome == 'accepted'
        with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
            assert store.load_d3_operation(tripped[0]).completion is not None
            assert not store.load_control_state_rows(task_id).executions


def test_materialization_worker_never_queries_sqlite(tmp_path, monkeypatch):
    from agent_core.fixed_probe import FixedProbeBindings
    normalize = FixedProbeBindings.normalize

    class NoSql:
        def __getattr__(self, name): pytest.fail('worker queried SQLite')

    def guarded(bindings, request, wire):
        app = bindings.app
        worker = FixedProbeBindings(SimpleNamespace(config=app.config, output_boundary=app.output_boundary,
            sessions=app.sessions, clock=app.clock, store=NoSql()))
        return normalize(worker, request, wire)

    monkeypatch.setattr(FixedProbeBindings, 'normalize', guarded)
    result, _, _, _ = run_fixed(tmp_path, monkeypatch)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result


def test_current_review_timeout_limit_is_frozen_in_schema(tmp_path, monkeypatch):
    original = SimulationModel.invoke

    def restrict(model, envelope, *args):
        value = original(model, envelope, *args)
        if envelope.output_contract['stage'] == 'review' and envelope.request.review_scope == 'turn_start':
            value['constraints'] = [{'kind': 'max_action_timeout_seconds', 'scope_kind': 'intent',
                                     'max_action_timeout_seconds': 5}]
        return value

    monkeypatch.setattr(SimulationModel, 'invoke', restrict)
    result, _, seen, directory = run_fixed(tmp_path, monkeypatch)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    for envelope in seen:
        assert envelope.request.context.max_timeout_seconds == 5
        schema = envelope.output_contract['schema']['anyOf'][0]['properties']['action']['properties']
        assert schema['timeout_seconds']['maximum'] == 5
        record = FileD8OutputEvidence(directory / 'd8-output', create=False).read('fixed-probe-binding', envelope.request.model_request_id)
        assert record['canonical_response']['action']['timeout_seconds'] == 5


def test_v6_actual_transport_uses_current_choice_schema_offline(tmp_path, monkeypatch):
    from dataclasses import replace
    from agent_adapters.deepseek_roles import build_payload, role_config, EstimatedCapacity, CAPACITY_POLICY
    result, _, seen, _ = run_fixed(tmp_path, monkeypatch)
    assert len(result['verified_facts']) == 1
    inner, outer = role_config('inner_action', version=6), role_config('outer_planning', version=6)
    for envelope in seen:
        request = replace(envelope.request, role_config_ref=inner.role_config_ref)
        envelope = replace(envelope, request=request)
        payload = build_payload(envelope, inner)
        assert payload['model'] == 'deepseek-flash'
        system = payload['messages'][0]['content']
        assert 'fixed_probe_policy' in system and 'Never generate script' in system
        schema = envelope.output_contract['schema']['anyOf'][0]['properties']['action']['properties']
        assert schema['operation']['enum'] == ['observe_size' if request.stage == 'inner_size' else 'observe_sha256']
        assert not {'script', 'working_directory', 'stdin_base64', 'conditions'} & set(schema)
        assert EstimatedCapacity(outer, inner).fits(envelope, CAPACITY_POLICY)
        with pytest.raises(ValueError, match='output_contract_mismatch'):
            build_payload(replace(envelope, request=replace(request,
                role_config_ref=role_config('inner_action', version=5).role_config_ref)),
                role_config('inner_action', version=5))


def test_new_workbench_actual_child_uses11_and_cancels(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile='d8_v11')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'plain'), 5)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['result']['contract_version'] == 11 and run['result']['verified_facts'], run
    assert run['result']['event_export']['error'] is None
    workbench.runner.cancel(challenge['id'])
    assert _wait_for_cli(workbench, challenge['id'])['result']['cleanup_status'] == 'confirmed'


def test_unknown_fixed_model_response_is_not_reasked(tmp_path, monkeypatch):
    from agent_core.modeling import ModelError, ModelErrorKind
    original = SimulationModel.invoke
    seen = []

    def unknown(model, envelope, *args):
        if envelope.output_contract['stage'] == 'inner_size':
            seen.append(envelope.request.model_request_id)
            return ModelError(ModelErrorKind.TIMED_OUT, 'fixed_unknown')
        return original(model, envelope, *args)

    monkeypatch.setattr(SimulationModel, 'invoke', unknown)
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v11'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    result = task_cli.dispatch_run_task(args)
    assert result['recovery']['required'] and len(seen) == 1
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('unknown result was reasked'))
    args.command, args.task_run_id = 'resume', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']
    with SqliteControlStore.open_readonly(args.state_dir / result['task_run_id'] / 'control.sqlite') as store:
        assert not store.load_control_state_rows(result['task_run_id']).executions


def test_unknown_execution_is_never_dispatched_again(tmp_path, monkeypatch):
    from dataclasses import replace
    from agent_adapters.web.simulated_application import _LocalDigestSandbox
    execute = _LocalDigestSandbox.execute
    dispatched = []

    async def incomplete(sandbox, approved, token):
        dispatched.append(approved.request.request_id)
        return replace(await execute(sandbox, approved, token), stream_complete=False)

    monkeypatch.setattr(_LocalDigestSandbox, 'execute', incomplete)
    result, args, _, directory = run_fixed(tmp_path, monkeypatch)
    assert result['recovery']['required'] and len(dispatched) == 1
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('unknown execution recalled model'))
    monkeypatch.setattr(_LocalDigestSandbox, 'execute', lambda *a: pytest.fail('unknown execution was redispatched'))
    args.command, args.task_run_id = 'resume', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']
    assert len(dispatched) == 1


def test_materialized_timeout_keeps_effective_step_budget(tmp_path, monkeypatch):
    from agent_adapters.command_recorder import T6CommandRecorder
    original = SimulationModel.invoke

    def longer(model, envelope, *args):
        value = original(model, envelope, *args)
        if envelope.output_contract['stage'] in {'inner_size', 'inner_sha256'}:
            value['action']['timeout_seconds'] = 300
        return value

    monkeypatch.setattr(SimulationModel, 'invoke', longer)
    result, _, _, directory = run_fixed(tmp_path, monkeypatch)
    assert len(result['verified_facts']) == 1, result
    recorder = T6CommandRecorder(directory / 'commands', create=False)
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        state = store.load_control_state_rows(result['task_run_id'])
        for execution in state.executions:
            saved = recorder.read_completed(execution.execution_id)
            assert saved.record.effective_timeout_seconds <= 60


def test_default_launcher_selects18_without_starting_runtime(monkeypatch, tmp_path):
    from agent_adapters import workbench
    captured = []

    def server_factory(*args, **kwargs):
        captured.append(args[1])
        raise KeyboardInterrupt

    monkeypatch.setattr(workbench, 'WorkbenchServer', server_factory)
    monkeypatch.delenv('QINYUAN_WORKBENCH_PROTOCOL_VERSION', raising=False)
    try:
        workbench.main(['--data-dir', str(tmp_path)])
    except KeyboardInterrupt:
        pass
    assert captured
    owner = captured[0]
    assert owner.execution_profile == 'd8_v18'


@pytest.mark.parametrize('damage', ['missing', 'action', 'wire', 'role', 'checkpoint', 'root', 'contract', 'snapshot', 'diagnostic', 'business'])
def test_corrupt_binding_blocks_readonly_and_cancel(tmp_path, monkeypatch, damage):
    result, args, seen, directory = run_fixed(tmp_path, monkeypatch)
    identity = seen[0].request.model_request_id
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    record = output.read('fixed-probe-binding', identity)
    original = FileD8OutputEvidence.read
    snapshots = FileD8HandoffEvidence.read

    def read(adapter, kind, key):
        if key == identity and kind == ('diagnostic' if damage == 'diagnostic' else 'business-diagnostic') and damage in {'diagnostic', 'business'}:
            return {}
        if key == identity and kind == 'fixed-probe-binding':
            if damage == 'missing': return None
            value = json.loads(json.dumps(record))
            if damage == 'action': value['canonical_response']['action']['script'] = 'echo changed'
            elif damage == 'wire': value['wire_response']['action']['timeout_seconds'] = 1
            elif damage == 'role': value['role_config_ref'] = 'changed'
            elif damage == 'checkpoint': value['base_checkpoint_sequence'] += 1
            elif damage == 'root': value['root_request_id'] = 'changed'
            return value
        if key == identity and kind == 'contract' and damage == 'contract': return None
        return original(adapter, kind, key)

    def read_snapshot(adapter, kind, key):
        if key == identity and kind == 'request' and damage == 'snapshot' and adapter.records.directory.name == 'd8-sessions':
            return None
        return snapshots(adapter, kind, key)

    monkeypatch.setattr(FileD8OutputEvidence, 'read', read)
    monkeypatch.setattr(FileD8HandoffEvidence, 'read', read_snapshot)
    declaration = immutable_read(directory / 'run.json')
    before = (directory / 'control.sqlite').read_bytes()
    assert snapshot_v3_task_result(directory, declaration)['recovery']['required']
    assert (directory / 'control.sqlite').read_bytes() == before
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('cancel called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']


@pytest.mark.parametrize('damage', ['contract', 'operation'])
def test_correction_root_is_independently_bound(tmp_path, monkeypatch, damage):
    result, args, seen, directory = run_fixed(tmp_path, monkeypatch, failure='stage')
    assert len(result['verified_facts']) == 1
    root = seen[0].request.model_request_id
    read = FileD8OutputEvidence.read
    load = SqliteControlStore.load_d3_operation

    def changed_contract(adapter, kind, key):
        if damage == 'contract' and key == root and kind == 'contract': return None
        return read(adapter, kind, key)

    def changed_start(store, key):
        from dataclasses import replace
        operation = load(store, key)
        if damage == 'operation' and key == root:
            return replace(operation, start=replace(operation.start, context_sha256='f' * 64))
        return operation

    monkeypatch.setattr(FileD8OutputEvidence, 'read', changed_contract)
    monkeypatch.setattr(SqliteControlStore, 'load_d3_operation', changed_start)
    assert snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))['recovery']['required']
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('corrupt root recalled model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']
