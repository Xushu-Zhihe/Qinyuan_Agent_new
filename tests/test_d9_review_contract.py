"""Ordinary review9 wire choices, trusted scope binding and durable recovery."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_adapters import task_cli, cli
from agent_adapters.d8_simulation import SimulationModel
from agent_adapters.d8_model import build_payload, role_config
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_core.review_policy import policy_for
from test_d9_public import request


def run_case(tmp_path, monkeypatch, failure=None, *, twice=False, scopes=(), version=9, phase='turn_start', action_review=False):
    raw = request(tmp_path)
    raw['execution_profile'] = f'd8_v{version}'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    original = SimulationModel.invoke
    seen = []
    def invoke(model, envelope, *args):
        value = original(model, envelope, *args)
        if action_review and envelope.output_contract['stage'] == 'inner_size':
            value['review_requested'] = True
        if envelope.output_contract['stage'] != 'review': return value
        seen.append(envelope)
        if not isinstance(value, dict): return value
        if envelope.request.review_scope == phase:
            if scopes:
                value['constraints'] = [{'kind': 'allowed_action_kinds', 'scope_kind': scope,
                                        'allowed_action_kinds': ['probe', 'complete_intent']} for scope in scopes]
            if failure and (envelope.correction is None or twice):
                if failure == 'end': value['decision'] = 'acknowledge_turn_end'
                elif failure == 'continue': value['decision'] = 'continue_current_intent'
                elif failure == 'actions': value['constraints'] = [{'kind': 'allowed_action_kinds', 'scope_kind': 'intent', 'allowed_action_kinds': ['PRIVATE_REJECTED_VALUE']}]
                elif failure == 'scope_id': value['constraints'] = [{'kind': 'allowed_action_kinds', 'scope_kind': 'intent', 'scope_id': 'PRIVATE_REJECTED_VALUE', 'allowed_action_kinds': ['probe']}]
                elif failure == 'rationale': value['rationale'] = 'PRIVATE_REJECTED_VALUE\x00'
                elif failure == 'empty': value['rationale'] = ' '
                elif failure == 'sources': value['source_ids'] = ['PRIVATE_REJECTED_VALUE']
                elif failure == 'timeout': value['constraints'] = [{'kind': 'max_action_timeout_seconds', 'scope_kind': 'turn', 'max_action_timeout_seconds': 0}]
                elif failure == 'duplicates': value['constraints'] = [{'kind': 'allowed_action_kinds', 'scope_kind': 'intent', 'allowed_action_kinds': ['probe', 'probe']}]
        return value
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    result = task_cli.dispatch_run_task(args)
    return result, args, seen


def test_ordinary_review_reaches_fact_and_preserves_model_scope_choice(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch, scopes=('intent', 'turn'))
    assert result['contract_version'] == 9
    assert len(result['verified_facts']) == 1, result
    assert result['goal_evaluation'] == 'not_evaluated'
    evidence = FileD8OutputEvidence(args.state_dir / result['task_run_id'] / 'd8-output', create=False)
    start = next(e for e in seen if e.request.review_scope == 'turn_start')
    schema = start.output_contract['schema']
    assert 'acknowledge_turn_end' not in schema['properties']['decision']['enum']
    assert 'scope_id' not in json.dumps(schema)
    saved = evidence.read('review-binding', start.request.review_id)
    assert [c['scope_kind'] for c in saved['wire_response']['constraints']] == ['intent', 'turn']
    assert all('scope_id' not in c for c in saved['wire_response']['constraints'])
    assert [c['scope_id'] for c in saved['canonical_response']['constraints']] == [start.request.intent_id, start.request.turn_id]
    end = next(e for e in seen if e.request.review_scope == 'turn_end')
    assert end.output_contract['schema']['properties']['decision']['enum'] == ['acknowledge_turn_end']
    assert end.output_contract['schema']['properties']['constraints']['maxItems'] == 0
    payload = build_payload(start, role_config('outer_planning', version=4))
    assert 'scope_id' in payload['messages'][0]['content']
    assert payload['response_format'] == {'type': 'json_object'}
    assert 'acknowledge_turn_end' not in payload['messages'][0]['content']


@pytest.mark.parametrize('failure', ['end', 'actions', 'scope_id', 'rationale', 'empty', 'sources', 'timeout', 'duplicates'])
def test_targeted_single_correction_and_no_invalid_body(tmp_path, monkeypatch, failure):
    result, args, seen = run_case(tmp_path, monkeypatch, failure)
    assert len(result['verified_facts']) == 1, result
    starts = [e for e in seen if e.request.review_scope == 'turn_start']
    assert len(starts) == 2
    assert starts[1].correction['issues']
    if failure in {'rationale', 'empty'}:
        assert starts[1].correction['reason'] == 'output_business_invalid'
        assert result['business_diagnostics'][0]['issues'][0]['code'] == 'review_rationale_invalid'
    else:
        assert starts[1].correction['reason'] == 'output_format_invalid'
    for path in (args.state_dir / result['task_run_id']).rglob('*.json'):
        assert 'PRIVATE_REJECTED_VALUE' not in path.read_text(encoding='utf-8')


def test_two_rejections_never_get_third_call(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch, 'rationale', twice=True)
    assert result['verified_facts'] == []
    assert len(seen) == 2
    assert len(result['business_diagnostics']) == 2
    args.command, args.task_run_id = 'resume', result['task_run_id']
    cli.dispatch(args)
    assert len(seen) == 2


def test_legacy8_schema_and_correction_stay_unchanged(tmp_path, monkeypatch):
    result, _, seen = run_case(tmp_path, monkeypatch, 'end', twice=True, version=8)
    assert result['contract_version'] == 8 and len(seen) == 2
    assert seen[1].correction['reason'] == 'proposal_rejected'
    assert seen[0].output_contract['schema']['properties']['decision'] == {'type': 'string'}


def test_public_readonly_projection_detects_corrupt_binding(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch)
    directory = args.state_dir / result['task_run_id']
    identity = seen[0].request.review_id
    path = next((directory / 'd8-output').glob(f'*review-binding*{identity}*.json'))
    path.write_text('{}', encoding='utf-8')
    declaration = json.loads((directory / 'run.json').read_text(encoding='utf-8')) if (directory / 'run.json').exists() else None
    if declaration is None:
        from agent_adapters.run_files import immutable_read
        declaration = immutable_read(directory / 'declaration.json')
    snapshot = snapshot_v3_task_result(directory, declaration)
    assert snapshot['recovery']['required']


def test_real_v4_transport_offline_has_current_schema_and_actual_capacity(tmp_path, monkeypatch):
    from agent_adapters.d8_simulation import real_config
    from test_d9_models import install_controlled_transport, private_fixture
    raw = request(tmp_path)
    raw['execution_profile'], raw['mode'], raw['d8_config'] = 'd8_v9', 'real', real_config()
    for key, role in [('outer_role', 'outer_planning'), ('inner_role', 'inner_action')]:
        raw['dual_llm_config'][key] = asdict(role_config(role, version=4))
    raw = json.loads(json.dumps(raw))
    captured = install_controlled_transport(monkeypatch)
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    result = task_cli.dispatch_run_task(SimpleNamespace(request='unused', mode='real',
        state_dir=tmp_path / 'state', events=None, env_file=private_fixture(tmp_path), fixture=None))
    assert result['contract_version'] == 9 and len(result['verified_facts']) == 1, result
    reviews = [p for _, p in captured if 'review_policy=' in p['messages'][0]['content']]
    assert reviews
    assert all(p['response_format'] == {'type': 'json_object'} for p in reviews)


@pytest.mark.parametrize('stage', ['diagnostic', 'binding', 'result', 'ledger', 'completion'])
def test_known_evidence_interruption_repairs_without_model_on_cancel(tmp_path, monkeypatch, stage):
    from agent_adapters.budgeted_review_ledger import D4ReviewLedger
    from agent_core.sqlite_control_store import SqliteControlStore
    lost = []
    original_save = FileD8OutputEvidence.save
    original_finish = D4ReviewLedger.finish_attempt
    original_complete = SqliteControlStore.complete_d4_operation
    def save(evidence, kind, identity, value):
        original_save(evidence, kind, identity, value)
        trigger = {'diagnostic': 'business-diagnostic', 'binding': 'review-binding', 'result': 'review-result'}.get(stage)
        if trigger == kind and value.get('stage', 'review') == 'review' and not lost:
            lost.append(identity)
            raise SystemExit('offline interruption')
    def finish(ledger, req, **kwargs):
        if stage == 'ledger' and req.review_scope == 'turn_start' and not lost:
            lost.append(req.review_id)
            raise SystemExit('offline interruption')
        return original_finish(ledger, req, **kwargs)
    def complete(store, value):
        op = store.load_d4_operation(value.operation_id)
        if stage == 'completion' and op.start.category == 'review' and not lost:
            lost.append(value.operation_id)
            raise SystemExit('offline interruption')
        return original_complete(store, value)
    monkeypatch.setattr(FileD8OutputEvidence, 'save', save)
    monkeypatch.setattr(D4ReviewLedger, 'finish_attempt', finish)
    monkeypatch.setattr(SqliteControlStore, 'complete_d4_operation', complete)
    if stage in {'ledger', 'completion'}:
        with pytest.raises(SystemExit): run_case(tmp_path, monkeypatch, 'rationale')
    else:
        run_case(tmp_path, monkeypatch, None if stage == 'binding' else 'rationale')
    assert len(lost) == 1
    monkeypatch.setattr(FileD8OutputEvidence, 'save', original_save)
    monkeypatch.setattr(D4ReviewLedger, 'finish_attempt', original_finish)
    monkeypatch.setattr(SqliteControlStore, 'complete_d4_operation', original_complete)
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('cancel dispatched model'))
    from agent_adapters.run_files import immutable_read
    directory = next((tmp_path / 'state').glob('*/run.json')).parent
    declaration = immutable_read(directory / 'run.json')
    result = cli.dispatch(SimpleNamespace(command='cancel', state_dir=directory.parent,
        task_run_id=declaration['task_run_id'], events=None))
    assert result['task_run']['status'] == 'cancelled' and result['cleanup_status'] == 'confirmed', result


@pytest.mark.parametrize('damage', ['request', 'request_and_contract', 'diagnostic', 'code', 'path', 'root', 'checkpoint', 'result'])
def test_diagnostic_damage_stops_projection_and_cancel(tmp_path, monkeypatch, damage):
    result, args, seen = run_case(tmp_path, monkeypatch, 'rationale', twice=True)
    directory = args.state_dir / result['task_run_id']
    target = seen[0].request.review_id
    original = FileD8OutputEvidence.read
    def corrupt(evidence, kind, identity):
        value = original(evidence, kind, identity)
        if identity != target: return value
        if damage == 'request' and kind == 'review-request': return None
        if damage == 'request_and_contract' and kind in {'review-request', 'contract'}: return None
        if damage == 'diagnostic' and kind == 'business-diagnostic': return None
        if damage == 'result' and kind == 'review-result': return {**value, 'business_diagnostic_sha256': '0' * 64}
        if kind == 'business-diagnostic':
            value = json.loads(json.dumps(value))
            if damage == 'code': value['issues'][0]['code'] = 'PRIVATE_UNKNOWN_CODE'
            if damage == 'path': value['issues'][0]['path'] = '$/PRIVATE_PATH'
            if damage == 'root': value['root_request_id'] = seen[-1].request.review_id
            if damage == 'checkpoint': value['base_checkpoint_sequence'] += 1
        return value
    monkeypatch.setattr(FileD8OutputEvidence, 'read', corrupt)
    from agent_adapters.run_files import immutable_read
    snapshot = snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))
    assert snapshot['recovery']['required'] and snapshot['verified_facts'] == []
    cancelled = cli.dispatch(SimpleNamespace(command='cancel', state_dir=args.state_dir,
        task_run_id=result['task_run_id'], events=None))
    assert cancelled['recovery']['required'] and cancelled['cleanup_status'] != 'confirmed'


@pytest.mark.parametrize('phase,failure', [('action', 'end'), ('turn_end', 'continue')])
def test_other_review_phases_have_correct_choices_and_correction(tmp_path, monkeypatch, phase, failure):
    result, _, seen = run_case(tmp_path, monkeypatch, failure, phase=phase, action_review=phase == 'action')
    assert len(result['verified_facts']) == 1, result
    target = [e for e in seen if e.request.review_scope == phase]
    assert len(target) >= 2
    assert target[1].correction['issues'][0]['code'] == 'enum_mismatch'


def test_unknown_review_never_replays(tmp_path, monkeypatch):
    from agent_core.review_contracts import D2ModelFailure
    original = SimulationModel.invoke
    count = []
    def invoke(model, env, *args):
        if env.output_contract['stage'] == 'review':
            count.append(env)
            return D2ModelFailure('unknown', 'fixed_unknown')
        return original(model, env, *args)
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    result, args, _ = run_case(tmp_path, monkeypatch)
    assert len(count) == 1 and result['verified_facts'] == []
    from agent_adapters.review_ledger import FileD2ReviewLedger
    root = FileD2ReviewLedger(args.state_dir / result['task_run_id'] / 'd3-reviews').read_root(count[0].request.review_root_id)
    assert root['last_outcome'] == 'unknown'
    args.command, args.task_run_id = 'resume', result['task_run_id']
    cli.dispatch(args)
    assert len(count) == 1


@pytest.mark.parametrize('damage', ['binding', 'result', 'both', 'request_and_contract'])
def test_missing_accepted_evidence_pauses_readonly_projection(tmp_path, monkeypatch, damage):
    result, args, seen = run_case(tmp_path, monkeypatch)
    target, directory = seen[0].request.review_id, args.state_dir / result['task_run_id']
    original = FileD8OutputEvidence.read
    kinds = {'binding': {'review-binding'}, 'result': {'review-result'}, 'both': {'review-binding', 'review-result'},
             'request_and_contract': {'review-request', 'contract'}}[damage]
    monkeypatch.setattr(FileD8OutputEvidence, 'read', lambda e,k,i: None if i == target and k in kinds else original(e,k,i))
    from agent_adapters.run_files import immutable_read
    snapshot = snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))
    assert snapshot['recovery']['required'] and snapshot['verified_facts'] == []


def test_worker_correction_uses_frozen_files_without_sqlite(tmp_path, monkeypatch):
    from agent_adapters.d8_simulation import parse_request
    from agent_adapters.review_ledger import FileD2ReviewLedger
    from agent_adapters.run_files import immutable_read
    from agent_core.review_diagnostics import ReviewDiagnostics
    from agent_core.structured_output import StructuredOutput
    result, args, seen = run_case(tmp_path, monkeypatch, 'rationale')
    directory = args.state_dir / result['task_run_id']
    _, config = parse_request(immutable_read(directory / 'run.json')['request'])
    class NoStore:
        def __getattr__(self, name): pytest.fail('worker queried SQLite')
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    manager = ReviewDiagnostics(config, output, FileD2ReviewLedger(directory / 'd3-reviews'), NoStore(), lambda: 0)
    boundary = StructuredOutput(SimpleNamespace(config=config, review_diagnostics=manager, business_diagnostics=None), output)
    correction = seen[1].correction
    assert boundary._error(correction['correction_of_model_request_id']) == correction


def test_complete_correction_capacity_failure_prevents_second_dispatch(tmp_path, monkeypatch):
    from agent_adapters.handoff_evidence import FakeUnicodeCapacity
    from agent_core.output_contracts import D8OutputRequest
    original = FakeUnicodeCapacity.fits
    def fits(port, value, policy):
        if isinstance(value, D8OutputRequest) and value.output_contract['stage'] == 'review' and value.correction:
            return False
        return original(port, value, policy)
    monkeypatch.setattr(FakeUnicodeCapacity, 'fits', fits)
    result, _, seen = run_case(tmp_path, monkeypatch, 'rationale')
    assert len(seen) == 1 and result['recovery']['required']
    assert result['budget']['category_counts']['review'] == 1


def test_sequential_constraint_widening_is_diagnosed(tmp_path, monkeypatch):
    original = SimulationModel.invoke
    def invoke(model, env, *args):
        value = original(model, env, *args)
        if env.output_contract['stage'] == 'review' and env.request.review_scope == 'turn_start' and env.correction is None:
            value['constraints'] = [
                {'kind': 'allowed_action_kinds', 'scope_kind': 'intent', 'allowed_action_kinds': ['probe']},
                {'kind': 'allowed_action_kinds', 'scope_kind': 'turn', 'allowed_action_kinds': ['complete_intent']}]
        return value
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    result, _, seen = run_case(tmp_path, monkeypatch)
    assert len(result['verified_facts']) == 1, result
    assert result['business_diagnostics'][0]['issues'][0]['code'] == 'review_actions_widened'


def test_new_workbench_actual_child_uses9_and_cancels(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_d7_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile='d8_v9')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'plain'), 5)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['result']['contract_version'] == 9 and run['result']['verified_facts']
    assert run['result']['event_export']['error'] is None
    workbench.runner.cancel(challenge['id'])
    cancelled = _wait_for_cli(workbench, challenge['id'])
    assert cancelled['result']['cleanup_status'] == 'confirmed'
