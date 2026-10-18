"""Contract10 current-proof selection and bounded source correction, no API calls."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import cli, task_cli
from agent_adapters.simulation import SimulationModel, parse_request, real_config
from agent_adapters.deepseek_roles import build_payload, role_config
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.conclusion_evidence import FileD8Evidence
from agent_adapters.run_files import immutable_read
from agent_core.conclusion_contracts import allowed_fact_evidence_refs
from test_public_simulation import request


def run_sources(tmp_path, monkeypatch, failure='observation', *, twice=False, version=10, order=None):
    raw = request(tmp_path)
    raw['execution_profile'] = f'd8_v{version}'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    original = SimulationModel.invoke
    seen, reviews = [], []
    def invoke(model, env, *args):
        if env.output_contract['stage'] == 'fact_review': reviews.append(env)
        value = original(model, env, *args)
        if env.output_contract['stage'] != 'fact_proposal': return value
        seen.append(env)
        if order == 'unknown':
            from agent_core.modeling import ModelError, ModelErrorKind
            return ModelError(ModelErrorKind.TIMED_OUT, 'fixed_unknown')
        if order in {'source_business', 'business_source'}:
            if (len(seen) == 1) == (order == 'business_source'):
                value['content'] = ' '
                return value
        elif env.correction is not None and not twice:
            return value
        obs = env.request.observation
        refs = {'observation': [obs.observation_id], 'artifact': [obs.artifact_version_id],
                'execution': [obs.execution_id], 'all_objects': [obs.observation_id, obs.artifact_version_id, obs.execution_id],
                'mixed': [obs.proof_id, obs.observation_id], 'duplicate': [obs.proof_id, obs.proof_id],
                'outside': [str(uuid4())], 'private': ['PRIVATE_REJECTED_REF'], 'empty': []}
        value['evidence_refs'] = refs[failure]
        return value
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    return task_cli.dispatch_run_task(args), args, seen, reviews


@pytest.mark.parametrize('failure', ['observation', 'artifact', 'execution', 'all_objects', 'mixed', 'duplicate', 'outside', 'private', 'empty'])
def test_wrong_source_is_rejected_then_one_correction_reaches_outer_review(tmp_path, monkeypatch, failure):
    result, args, seen, reviews = run_sources(tmp_path, monkeypatch, failure)
    assert result['contract_version'] == 10 and result['execution_profile'] == 'd8_v10'
    assert len(result['verified_facts']) == 1, result
    assert len(seen) == 2 and len(reviews) == 1
    assert seen[1].correction['reason'] == 'output_format_invalid'
    assert any(i['path'].startswith('$/evidence_refs') for i in seen[1].correction['issues'])
    allowed = allowed_fact_evidence_refs(seen[0].request.observation)
    schema = seen[0].output_contract['schema']['anyOf'][0]['properties']['evidence_refs']
    assert schema['items']['enum'] == list(allowed) and schema['minItems'] == schema['maxItems'] == 1
    directory = args.state_dir / result['task_run_id']
    first = FileD8Evidence(directory / 'd8-conclusions', create=False).read_result(seen[0].request.model_request_id)
    assert first['outcome'] == 'invalid' and first['raw'] is None
    for path in directory.rglob('*.json'):
        assert 'PRIVATE_REJECTED_REF' not in path.read_text(encoding='utf-8')


def test_twice_wrong_source_is_known_and_never_gets_third_call(tmp_path, monkeypatch):
    result, args, seen, reviews = run_sources(tmp_path, monkeypatch, twice=True)
    assert len(seen) == 2 and not reviews and not result['verified_facts']
    args.command, args.task_run_id = 'resume', result['task_run_id']
    cli.dispatch(args)
    assert len(seen) == 2


@pytest.mark.parametrize('version', [6, 7, 8, 9])
def test_old_source_gate_still_stops_without_new_schema_or_correction(tmp_path, monkeypatch, version):
    result, _, seen, reviews = run_sources(tmp_path, monkeypatch, 'observation', version=version)
    assert len(seen) == 1 and not reviews and not result['verified_facts']
    schema = seen[0].output_contract['schema']['anyOf'][0]['properties']['evidence_refs']
    assert schema == {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 8}


@pytest.mark.parametrize('order', ['source_business', 'business_source', 'unknown'])
def test_shared_correction_and_unknown_are_bounded(tmp_path, monkeypatch, order):
    result, args, seen, reviews = run_sources(tmp_path, monkeypatch, order=order)
    assert len(seen) == (1 if order == 'unknown' else 2)
    assert not reviews and not result['verified_facts']
    args.command, args.task_run_id = 'resume', result['task_run_id']
    cli.dispatch(args)
    assert len(seen) == (1 if order == 'unknown' else 2)


def test_real_v5_payload_has_explicit_trusted_range_and_full_version(tmp_path, monkeypatch):
    from test_models import install_controlled_transport, private_fixture
    raw = request(tmp_path)
    raw['execution_profile'], raw['mode'], raw['d8_config'] = 'd8_v10', 'real', real_config()
    for key, role in [('outer_role', 'outer_planning'), ('inner_role', 'inner_action')]:
        raw['dual_llm_config'][key] = asdict(role_config(role, version=5))
    raw = json.loads(json.dumps(raw))
    assert parse_request(raw)[1].contract_version == 10
    def alter(env, _):
        if env.output_contract['stage'] == 'fact_proposal' and env.correction is None:
            value = SimulationModel(role_config('inner_action', version=5)).invoke(env)
            value['evidence_refs'] = [env.request.observation.observation_id]
            return {'content': json.dumps(value), 'finish_reason': 'stop'}
    captured = install_controlled_transport(monkeypatch, alter=alter)
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    args = SimpleNamespace(request='unused', mode='real', state_dir=tmp_path / 'state', events=None,
                           env_file=private_fixture(tmp_path), fixture=None)
    result = task_cli.dispatch_run_task(args)
    assert len(result['verified_facts']) == 1 and result['contract_version'] == 10, result
    proposals = [(e,p) for e,p in captured if e.output_contract['stage'] == 'fact_proposal']
    assert len(proposals) == 2
    for env, payload in proposals:
        context = json.loads(payload['messages'][1]['content'])
        assert context['allowed_evidence_refs'] == list(allowed_fact_evidence_refs(env.request.observation))
        assert 'current proof_id only' in payload['messages'][0]['content']
        assert payload['response_format'] == {'type': 'json_object'}
    raw['dual_llm_config']['inner_role'] = asdict(role_config('inner_action', version=4))
    with pytest.raises(ValueError, match='role_configuration'):
        parse_request(json.loads(json.dumps(raw)))


def test_interrupted_rejection_is_repaired_by_original_id_on_cancel(tmp_path, monkeypatch):
    original = FileD8Evidence.save_result
    lost = []
    def save(evidence, identity, value):
        if value['outcome'] == 'invalid' and not lost:
            lost.append(identity)
            raise SystemExit('offline interruption')
        return original(evidence, identity, value)
    monkeypatch.setattr(FileD8Evidence, 'save_result', save)
    with pytest.raises(SystemExit): run_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(FileD8Evidence, 'save_result', original)
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('cancel called model'))
    directory = next((tmp_path / 'state').glob('*/run.json')).parent
    declaration = immutable_read(directory / 'run.json')
    result = cli.dispatch(SimpleNamespace(command='cancel', state_dir=directory.parent,
        task_run_id=declaration['task_run_id'], events=None))
    assert result['task_run']['status'] == 'cancelled' and result['cleanup_status'] == 'confirmed', result
    assert FileD8Evidence(directory / 'd8-conclusions', create=False).read_result(lost[0])['outcome'] == 'invalid'


def test_new_workbench_child_selects10_and_preserves_cancel(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile='d8_v10')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'plain'), 5)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['result']['contract_version'] == 10 and run['result']['verified_facts']
    workbench.runner.cancel(challenge['id'])
    assert _wait_for_cli(workbench, challenge['id'])['result']['cleanup_status'] == 'confirmed'


def test_source_correction_capacity_failure_prevents_second_call(tmp_path, monkeypatch):
    from agent_adapters.handoff_evidence import FakeUnicodeCapacity
    from agent_core.output_contracts import D8OutputRequest
    original = FakeUnicodeCapacity.fits
    def fits(port, value, policy):
        if isinstance(value, D8OutputRequest) and value.output_contract['stage'] == 'fact_proposal' and value.correction:
            return False
        return original(port, value, policy)
    monkeypatch.setattr(FakeUnicodeCapacity, 'fits', fits)
    result, _, seen, reviews = run_sources(tmp_path, monkeypatch)
    assert len(seen) == 1 and not reviews and not result['verified_facts']
    assert result['recovery']['required']


def test_frozen_schema_rejects_changed_proof_and_preserves_request_review(tmp_path, monkeypatch):
    from dataclasses import replace
    from agent_core.conclusion_contracts import parse_d8_proposal, D8ReviewRequestAction
    from agent_core.output_contracts import decode_response
    result, _, seen, _ = run_sources(tmp_path, monkeypatch)
    env = seen[0]
    raw, issues, terminal, _ = decode_response({'kind': 'request_review', 'reason': 'Need a supported conclusion'}, env.output_contract['schema'])
    assert not issues and terminal is None and isinstance(parse_d8_proposal(raw), D8ReviewRequestAction)
    observation = replace(env.request.observation, proof_id=str(uuid4()))
    changed = replace(env, request=replace(env.request, observation=observation))
    with pytest.raises(ValueError, match='output_contract_mismatch'):
        build_payload(changed, role_config('inner_action', version=5))
