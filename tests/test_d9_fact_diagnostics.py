"""Contract8 proposal diagnosis, correction and durable recovery without API calls."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import cli, task_cli
from agent_adapters.conclusion_evidence import FileD8Evidence
from agent_adapters.d8_simulation import SimulationModel, parse_request, real_config
from agent_adapters.d8_model import role_config, build_payload
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.run_files import immutable_read
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_core.fact_diagnostics import proposal_issues
from agent_core.sqlite_control_store import SqliteControlStore
from test_d9_public import request


def raw8(tmp_path):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v8'
    return raw


def bad_proposal(value, failure):
    raw = json.loads(json.dumps(value))
    if failure == 'content': raw['content'] = ' '
    if failure == 'reason': raw['reason'] = 'bad\x00reason'
    if failure == 'uuid': raw['evidence_refs'] = ['PRIVATE_REJECTED_VALUE']
    if failure == 'duplicate': raw['evidence_refs'] *= 2
    if failure == 'rule': raw['assertion']['rule_id'] = 'PRIVATE_REJECTED_VALUE'
    if failure == 'version': raw['assertion']['rule_version'] = 2
    if failure == 'artifact': raw['assertion']['artifact_version_id'] = 'PRIVATE_REJECTED_VALUE'
    if failure == 'observation': raw['assertion']['observation_id'] = 'PRIVATE_REJECTED_VALUE'
    if failure == 'review': raw = {'kind': 'request_review', 'reason': ' '}
    return raw


def run_case(tmp_path, monkeypatch, failure='content', *, twice=False, version=8, order=None, events=None):
    raw = raw8(tmp_path)
    raw['execution_profile'] = f'd8_v{version}'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    original = SimulationModel.invoke
    seen = []
    def invoke(model, envelope, *args):
        if envelope.output_contract['stage'] == 'fact_proposal':
            seen.append(envelope)
            if order == 'unknown':
                from agent_core.modeling import ModelError, ModelErrorKind
                return ModelError(ModelErrorKind.TIMED_OUT, 'fixed_timeout')
            if order in {'format_first', 'business_first'}:
                is_format = (len(seen) == 1) == (order == 'format_first')
                return '{"kind":"candidate","kind":"candidate"}' if is_format else bad_proposal(original(model, envelope, *args), failure)
            if envelope.correction is None or twice:
                return bad_proposal(original(model, envelope, *args), failure)
        return original(model, envelope, *args)
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=events)
    result = task_cli.dispatch_run_task(args)
    return result, args, seen


@pytest.mark.parametrize('failure,code', [
    ('content', 'fact_content_invalid'), ('reason', 'fact_reason_invalid'),
    ('uuid', 'fact_evidence_uuid_invalid'), ('duplicate', 'fact_evidence_duplicate'),
    ('rule', 'fact_assertion_rule_unregistered'), ('version', 'fact_assertion_version_unsupported'),
    ('artifact', 'fact_assertion_artifact_uuid_invalid'), ('observation', 'fact_assertion_observation_uuid_invalid'),
    ('review', 'fact_reason_invalid'),
])
def test_diagnosis_corrects_existing_parser_rejection_and_reaches_fact_review(tmp_path, monkeypatch, failure, code):
    result, args, seen = run_case(tmp_path, monkeypatch, failure)
    assert len(seen) == 2
    assert result['contract_version'] == 8 and result['execution_profile'] == 'd8_v8'
    assert len(result['verified_facts']) == 1, result
    assert result['goal_evaluation'] == 'not_evaluated'
    diagnostic = result['business_diagnostics'][0]
    assert diagnostic['stage'] == 'fact_proposal'
    assert diagnostic['issues'][0]['code'] == code
    correction = seen[1].correction
    assert correction['reason'] == 'output_business_invalid'
    assert correction['issues'] == diagnostic['issues']
    assert correction['business_diagnostic_sha256'] == diagnostic['diagnostic_sha256']
    directory = args.state_dir / result['task_run_id']
    rejected = FileD8Evidence(directory / 'd8-conclusions', create=False).read_result(diagnostic['model_request_id'])
    assert rejected['raw'] is None and rejected['outcome'] == 'invalid'
    for path in directory.rglob('*.json'):
        assert 'PRIVATE_REJECTED_VALUE' not in path.read_text(encoding='utf-8')


def test_second_rejection_is_known_and_never_gets_third_proposal(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch, twice=True)
    assert len(seen) == 2 and len(result['business_diagnostics']) == 2
    assert result['verified_facts'] == []
    args.command, args.task_run_id = 'resume', result['task_run_id']
    cli.dispatch(args)
    assert len(seen) == 2


@pytest.mark.parametrize('version', [6, 7])
def test_old_contract_has_no_fact_business_diagnostic(tmp_path, monkeypatch, version):
    result, _, seen = run_case(tmp_path, monkeypatch, twice=True, version=version)
    assert len(seen) == 2 and seen[1].correction['reason'] == 'proposal_rejected'
    assert not result.get('business_diagnostics')


@pytest.mark.parametrize('order', ['format_first', 'business_first', 'unknown'])
def test_shared_correction_and_unknown_no_replay(tmp_path, monkeypatch, order):
    result, args, seen = run_case(tmp_path, monkeypatch, order=order)
    assert len(seen) == (1 if order == 'unknown' else 2)
    assert len(result['business_diagnostics']) == (0 if order == 'unknown' else 1)
    args.command, args.task_run_id = 'resume', result['task_run_id']
    cli.dispatch(args)
    assert len(seen) == (1 if order == 'unknown' else 2)


def test_interrupted_after_diagnostic_recovers_original_rejection_on_cancel(tmp_path, monkeypatch):
    save = FileD8Evidence.save_result
    lost = []
    def interrupt(evidence, identity, value):
        if value.get('business_diagnostic_sha256') and not lost:
            lost.append(identity)
            raise SystemExit('offline interruption')
        return save(evidence, identity, value)
    monkeypatch.setattr(FileD8Evidence, 'save_result', interrupt)
    with pytest.raises(SystemExit):
        run_case(tmp_path, monkeypatch)
    monkeypatch.setattr(FileD8Evidence, 'save_result', save)
    directory = next((tmp_path / 'state').glob('*/run.json')).parent
    declaration = immutable_read(directory / 'run.json')
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('cancel dispatched model'))
    args = SimpleNamespace(command='cancel', state_dir=directory.parent,
                           task_run_id=declaration['task_run_id'], events=None)
    result = cli.dispatch(args)
    assert result['task_run']['status'] == 'cancelled' and result['cleanup_status'] == 'confirmed', result
    saved = FileD8Evidence(directory / 'd8-conclusions', create=False).read_result(lost[0])
    assert saved['outcome'] == 'invalid' and saved['business_diagnostic_sha256']


@pytest.mark.parametrize('damage', ['missing', 'owner', 'root', 'role', 'checkpoint', 'observation', 'session', 'contract', 'code', 'path', 'too_many', 'result', 'request', 'request_and_contract', 'format_conflict'])
def test_missing_corrupt_or_unbound_diagnostic_stops_projection_and_cancel(tmp_path, monkeypatch, damage):
    result, args, seen = run_case(tmp_path, monkeypatch, twice=True)
    directory = args.state_dir / result['task_run_id']
    declaration = immutable_read(directory / 'run.json')
    target = result['business_diagnostics'][0]['model_request_id']
    read = FileD8OutputEvidence.read
    def corrupt(evidence, kind, identity):
        value = read(evidence, kind, identity)
        if identity == target and damage == 'request_and_contract' and kind == 'contract':
            return None
        if identity == target and damage == 'format_conflict' and kind == 'diagnostic':
            return {}
        if kind == 'business-diagnostic' and identity == target:
            if damage == 'missing': return None
            value = json.loads(json.dumps(value))
            key = {'owner': 'task_run_id', 'root': 'root_request_id', 'role': 'role_config_ref',
                'checkpoint': 'base_checkpoint_sequence', 'observation': 'observation_sha256',
                'session': 'session_sha256', 'contract': 'contract_sha256'}.get(damage)
            if key: value[key] = 999 if damage == 'checkpoint' else str(uuid4())
            if damage == 'code': value['issues'][0]['code'] = 'private_code'
            if damage == 'path': value['issues'][0]['path'] = '$/private_path'
            if damage == 'too_many': value['issues'] *= 9
        return value
    monkeypatch.setattr(FileD8OutputEvidence, 'read', corrupt)
    if damage == 'result':
        result_read = FileD8Evidence.read_result
        def changed(evidence, identity):
            value = result_read(evidence, identity)
            if identity == target:
                value = {**value, 'business_diagnostic_sha256': '0' * 64}
            return value
        monkeypatch.setattr(FileD8Evidence, 'read_result', changed)
    if damage in {'request', 'request_and_contract'}:
        request_read = FileD8Evidence.read_request
        monkeypatch.setattr(FileD8Evidence, 'read_request', lambda e, i: None if i == target else request_read(e, i))
    projection = snapshot_v3_task_result(directory, declaration)
    assert projection['recovery']['required'] and projection['verified_facts'] == []
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    cancelled = cli.dispatch(args)
    assert cancelled['recovery']['required'] and cancelled['cleanup_status'] != 'confirmed'
    assert len(seen) == 2


def test_complete_limits_and_safety_without_raw_values():
    raw = {'kind': 'candidate', 'content': 'x' * 257, 'reason': 'r' * 513,
           'evidence_refs': [], 'assertion': None}
    codes = {value['code'] for value in proposal_issues(raw)}
    assert {'fact_content_too_long', 'fact_reason_too_long', 'fact_evidence_count_invalid'} <= codes
    raw['PRIVATE_FIELD'] = 'PRIVATE_VALUE'
    raw['assertion'] = {'PRIVATE_FIELD': 'PRIVATE_VALUE'}
    issues = proposal_issues(raw)
    assert len(issues) <= 8 and 'PRIVATE' not in json.dumps(issues)


def test_correction_capacity_is_checked_before_second_admission(tmp_path, monkeypatch):
    from agent_adapters.handoff_evidence import FakeUnicodeCapacity
    from agent_core.output_contracts import D8OutputRequest
    fits = FakeUnicodeCapacity.fits
    def bounded(counter, envelope, policy):
        if isinstance(envelope, D8OutputRequest) and envelope.output_contract['stage'] == 'fact_proposal' and envelope.correction is not None:
            return False
        return fits(counter, envelope, policy)
    monkeypatch.setattr(FakeUnicodeCapacity, 'fits', bounded)
    result, _, seen = run_case(tmp_path, monkeypatch)
    assert len(seen) == 1 and result['verified_facts'] == []
    assert result['budget']['category_counts']['action'] == 4


def test_sensitive_proposal_has_no_business_diagnostic_or_correction(tmp_path, monkeypatch):
    from agent_core.conclusion_review import ConclusionReview
    monkeypatch.setattr(ConclusionReview, '_sensitive', lambda _, raw: isinstance(raw, dict) and raw.get('kind') == 'candidate')
    result, _, seen = run_case(tmp_path, monkeypatch)
    assert len(seen) == 1 and result['business_diagnostics'] == [] and result['verified_facts'] == []


def test_parse_valid_source_mismatch_does_not_add_new_diagnostic_admission(tmp_path, monkeypatch):
    raw = raw8(tmp_path)
    original = SimulationModel.invoke
    count = []
    def invoke(model, envelope, *args):
        value = original(model, envelope, *args)
        if envelope.output_contract['stage'] == 'fact_proposal':
            count.append(envelope)
            value['evidence_refs'] = [str(uuid4())]
        return value
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    result = task_cli.dispatch_run_task(SimpleNamespace(request='unused', mode='simulated',
        state_dir=tmp_path / 'state', events=None))
    assert len(count) == 1 and result['verified_facts'] == [] and result['business_diagnostics'] == []
    assert result['budget']['category_counts']['fact_review'] == 0


def test_contract8_retains_tree_diagnostics_and_actual_workbench_child(tmp_path, monkeypatch):
    import io
    from test_d9_business_diagnostics import run_case as planning_case
    from agent_adapters.workbench import Workbench
    from test_d7_workbench_v3 import _challenge, _wait_for_cli
    result, _, _ = planning_case(tmp_path, monkeypatch, raw=raw8(tmp_path))
    assert result['contract_version'] == 8 and result['business_diagnostics'][0]['stage'] == 'tree_planning'
    monkeypatch.undo()
    workbench = Workbench(tmp_path / 'workbench', protocol_version=3, execution_profile='d8_v8')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'plain'), 5)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['result']['contract_version'] == 8 and run['result']['verified_facts']
    assert run['result']['event_export']['error'] is None
    workbench.runner.cancel(challenge['id'])
    cancelled = _wait_for_cli(workbench, challenge['id'])
    assert cancelled['result']['cleanup_status'] == 'confirmed'


def test_real_v3_prompt_and_configuration_are_frozen(tmp_path):
    raw = raw8(tmp_path)
    raw['mode'], raw['d8_config'] = 'real', real_config()
    for key, role in [('outer_role', 'outer_planning'), ('inner_role', 'inner_action')]:
        raw['dual_llm_config'][key] = asdict(role_config(role, version=3))
    _, config = parse_request(json.loads(json.dumps(raw)))
    assert config.contract_version == 8
    raw['dual_llm_config']['inner_role'] = asdict(role_config('inner_action', version=2))
    with pytest.raises(ValueError, match='role_configuration'):
        parse_request(json.loads(json.dumps(raw)))


def test_worker_correction_validation_never_queries_sqlite(tmp_path, monkeypatch):
    result, args, seen = run_case(tmp_path, monkeypatch)
    directory = args.state_dir / result['task_run_id']
    from agent_core.fact_diagnostics import FactBusinessDiagnostics
    from agent_core.structured_output import StructuredOutput
    _, config = parse_request(immutable_read(directory / 'run.json')['request'])
    class NoStore:
        def __getattr__(self, name): pytest.fail('model worker queried SQLite')
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    diagnostics = FactBusinessDiagnostics(config, output, FileD8Evidence(directory / 'd8-conclusions', create=False), NoStore())
    boundary = StructuredOutput(SimpleNamespace(config=config, fact_business_diagnostics=diagnostics,
        business_diagnostics=None), output)
    assert boundary._error(seen[1].request.correction_of_model_request_id) == seen[1].correction


def test_real_v3_adapter_offline_drives_fact_diagnostic_and_review(tmp_path, monkeypatch):
    from test_d9_models import install_controlled_transport, private_fixture
    def alter(env, count):
        if env.output_contract['stage'] == 'fact_proposal' and env.correction is None:
            raw = SimulationModel(role_config('inner_action', version=3)).invoke(env, 180, None)
            return {'content': json.dumps(bad_proposal(raw, 'uuid')), 'finish_reason': 'stop'}
    captured = install_controlled_transport(monkeypatch, alter=alter)
    raw = raw8(tmp_path)
    raw['mode'], raw['d8_config'] = 'real', real_config()
    for key, role in [('outer_role', 'outer_planning'), ('inner_role', 'inner_action')]:
        raw['dual_llm_config'][key] = asdict(role_config(role, version=3))
    monkeypatch.setattr(task_cli, '_input', lambda _: json.loads(json.dumps(raw)))
    args = SimpleNamespace(request='unused', mode='real', state_dir=tmp_path / 'state', events=None,
                           env_file=private_fixture(tmp_path), fixture=None)
    result = task_cli.dispatch_run_task(args)
    assert len(result['verified_facts']) == 1 and result['business_diagnostics'][0]['stage'] == 'fact_proposal'
    payloads = [payload for env, payload in captured if env.output_contract['stage'] == 'fact_proposal']
    assert len(payloads) == 2 and '256 Unicode' in payloads[1]['messages'][0]['content']
    assert 'output_business_invalid' in payloads[1]['messages'][0]['content']
