"""Contract14 explicit support, trustworthy observations and protected provenance."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import task_cli
from agent_adapters.simulation import SimulationModel
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.completion_evaluation import evaluation_policy
from agent_core.dual_llm_modeling import D3CandidateContext, D3ProposalRejected
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.sqlite_control_store import SqliteControlStore
from test_public_simulation import request
from test_output import output_case, run as run_output


def run_s4(tmp_path, monkeypatch, transform=None):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v14'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    original = SimulationModel.invoke
    seen = []
    def invoke(model, envelope, *args):
        result = original(model, envelope, *args)
        seen.append(envelope)
        return transform(envelope, result) if transform else result
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    from agent_adapters.multi_turn_application import D3Application
    original_result = D3Application.result
    stops = []
    def capture(app, reason, **kwargs):
        stops.append(reason)
        return original_result(app, reason, **kwargs)
    monkeypatch.setattr(D3Application, 'result', capture)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path/'state', events=None)
    result = task_cli.dispatch_run_task(args)
    result['s4_test_stop_reasons'] = stops
    return result, args, seen, args.state_dir/result['task_run_id']


def test_public_completion_and_explicit_local_observations(tmp_path, monkeypatch):
    result, _, seen, directory = run_s4(tmp_path, monkeypatch)
    assert result['errors'] == [] and not result['recovery']['required'], json.dumps(result)
    assert result['contract_version'] == 14 and result['goal_evaluation'] == 'not_evaluated'
    assert len(result['verified_facts']) == 1
    complete = next(e for e in seen if e.output_contract['stage'] == 'inner_complete')
    refs = complete.output_contract['schema']['anyOf'][0]['properties']['action']['properties']['fact_ids']
    assert refs['items']['enum'] == list(complete.request.context.supporting_fact_ids)
    evaluation = next(e for e in seen if e.output_contract['stage'] == 'intent_evaluation')
    policy = evaluation_policy(evaluation.request)
    local = next(i for i in evaluation.request.history if i.ref == policy['goal_ref'])
    assert json.loads(local.content)['goal'] == policy['local_goal']
    size = [json.loads(i.content)['observation'] for i in evaluation.request.history
            if i.record_type == 'progress' and 'observation' in json.loads(i.content)]
    assert len(size) == 1 and size[0]['byte_size'] == len(b'D9 local observation')
    assert size[0]['identity'] == 'saved_execution_observation_not_verified_fact'
    assert policy['sha256_observation']['scope'] == 'first_saved_observation'
    output = FileD8OutputEvidence(directory/'d8-output', create=False)
    binding = output.read('evaluation-binding', evaluation.request.model_request_id)
    assert binding['result']['raw']['decision'] == 'succeeded'
    assert not list((directory/'d7-candidates').glob('*.cipher'))


@pytest.mark.parametrize('stage,change', [('inner_complete','bad_fact'),('intent_evaluation','bad_ref'),
    ('intent_evaluation','missing_support'),('intent_evaluation','bad_decision')])
def test_targeted_schema_correction_keeps_existing_one_limit(tmp_path, monkeypatch, stage, change):
    def modify(e, raw):
        if e.output_contract['stage'] != stage or e.correction is not None:
            return raw
        if change == 'bad_fact': raw['action']['fact_ids'] = ['PRIVATE_INVALID_REFERENCE']
        elif change == 'bad_ref': raw['evidence_refs'] = ['PRIVATE_INVALID_REFERENCE']
        elif change == 'bad_decision': raw['decision'] = 'PRIVATE_INVALID_REFERENCE'
        else:
            p = evaluation_policy(e.request)
            raw['evidence_refs'] = [p['goal_ref']]
        return raw
    result, _, seen, directory = run_s4(tmp_path, monkeypatch, modify)
    assert result['errors'] == [] and not result['recovery']['required'], result
    calls = [e for e in seen if e.output_contract['stage'] == stage]
    assert len(calls) == 2 and calls[1].correction['issues']
    for path in (directory/'d8-output').glob('*.json'):
        assert 'PRIVATE_INVALID_REFERENCE' not in path.read_text(encoding='utf-8')


def test_inconclusive_preserves_original_goal_and_stops_without_retry(tmp_path, monkeypatch):
    def modify(e, raw):
        if e.output_contract['stage'] == 'intent_evaluation':
            raw['decision'] = 'inconclusive'
            raw['reason'] = 'The stated local objective needs evidence beyond these saved observations.'
        return raw
    result, _, seen, directory = run_s4(tmp_path, monkeypatch, modify)
    assert result['goal_evaluation'] == 'not_evaluated'
    assert len([e for e in seen if e.output_contract['stage'] == 'intent_evaluation']) == 1
    with SqliteControlStore.open_readonly(directory/'control.sqlite') as store:
        assert not store.list_d8_handoffs(result['task_run_id'])
        assert any(i.status.value == 'inconclusive' for i in store.load_control_state_rows(result['task_run_id']).intents)


@pytest.mark.parametrize('output_case', [14], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'changed', 'task', 'artifact'])
def test_missing_or_changed_size_stops_before_local_evaluation(output_case, monkeypatch, damage):
    case = output_case
    original = case.app.workspace_evidence.read_size
    def read(identity):
        observed = original(identity)
        if observed is None or damage == 'missing': return None
        if damage == 'task': return replace(observed, task_run_id=str(uuid4()))
        if damage == 'artifact': return replace(observed, artifact_version_id=str(uuid4()))
        return replace(observed, output_sha256='0'*64)
    monkeypatch.setattr(case.app.workspace_evidence, 'read_size', read)
    result = run_output(case)
    assert result.recovery_required
    assert not any(e.output_contract['stage'] == 'intent_evaluation' for e in case.models['handoff'].calls)
    monkeypatch.setattr(case.app.workspace_evidence, 'read_size', original)


@pytest.mark.parametrize('output_case', [14], indirect=True)
def test_known_local_result_recovers_original_id_without_call(output_case, monkeypatch):
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    request = next(e.request for e in case.models['handoff'].calls if e.output_contract['stage'] == 'intent_evaluation')
    expected = case.app.handoff.evidence.read('result', request.model_request_id)
    original = case.app.handoff.evidence.read
    def read(kind, identity):
        if kind == 'result' and identity == request.model_request_id:
            return None
        return original(kind, identity)
    monkeypatch.setattr(case.app.handoff.evidence, 'read', read)
    monkeypatch.setattr(case.app.handoff.evidence, 'save', lambda *a: None)
    before = len(case.models['handoff'].calls)
    assert case.app.evaluation_bindings.check_task(case.task)
    assert case.app.handoff._known_call(request) == expected
    assert len(case.models['handoff'].calls) == before


@pytest.mark.parametrize('output_case', [14], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'changed', 'shape'])
def test_bad_local_binding_never_recovers_or_calls_model(output_case, monkeypatch, damage):
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    req = next(e.request for e in case.models['handoff'].calls if e.output_contract['stage'] == 'intent_evaluation')
    output = case.app.output_boundary.evidence
    original = output.read
    def read(kind, identity):
        record = original(kind, identity)
        if kind == 'evaluation-binding' and identity == req.model_request_id:
            if damage == 'shape': return []
            return None if damage == 'missing' else {**record, 'policy_sha256': '0'*64}
        return record
    monkeypatch.setattr(output, 'read', read)
    before = len(case.models['handoff'].calls)
    with pytest.raises(D3RecoveryRequired):
        case.app.handoff._known_call(req)
    assert len(case.models['handoff'].calls) == before


def test_semantic_business_rejection_has_safe_diagnostic_and_one_correction(tmp_path, monkeypatch):
    def modify(e, raw):
        if e.output_contract['stage'] == 'intent_evaluation' and e.correction is None:
            raw['reason'] = '   '
        return raw
    result, _, seen, directory = run_s4(tmp_path, monkeypatch, modify)
    assert result['errors'] == [] and not result['recovery']['required'], result
    calls = [e for e in seen if e.output_contract['stage'] == 'intent_evaluation']
    assert len(calls) == 2
    assert calls[1].correction['issues'][0]['code'] == 'evaluation_business_rejected'
    diagnostic = FileD8OutputEvidence(directory/'d8-output', create=False).read(
        'evaluation-diagnostic', calls[0].request.model_request_id)
    assert diagnostic['result']['raw'] is None
    assert any(r['stage'] == 'intent_evaluation' for r in result['business_diagnostics'])


def test_failed_semantic_goal_is_not_overridden_by_existing_fact(tmp_path, monkeypatch):
    def modify(e, raw):
        if e.output_contract['stage'] == 'intent_evaluation':
            raw.update(decision='failed', reason='The evidence does not establish the stated local objective.',
                       evidence_refs=[evaluation_policy(e.request)['goal_ref']])
        return raw
    result, _, _, directory = run_s4(tmp_path, monkeypatch, modify)
    assert result['goal_evaluation'] == 'not_evaluated' and len(result['verified_facts']) == 1
    with SqliteControlStore.open_readonly(directory/'control.sqlite') as store:
        assert any(r.kind == 'failed' for r in store.list_d8_tree_records(result['task_run_id']))


@pytest.mark.parametrize('output_case', [14], indirect=True)
def test_complete_evaluation_binding_recovers_after_process_loss(output_case, monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    case = output_case
    original = case.app.handoff.evidence.save
    lost = []
    def save(kind, identity, value):
        if kind == 'result' and case.app.output_boundary.evidence.read('evaluation-binding', identity) is not None:
            lost.append(identity)
            raise ProcessLoss()
        return original(kind, identity, value)
    monkeypatch.setattr(case.app.handoff.evidence, 'save', save)
    with pytest.raises(ProcessLoss):
        run_output(case)
    assert len(lost) == 1
    request = next(e.request for e in case.models['handoff'].calls if e.request.model_request_id == lost[0])
    assert case.app.store.load_d4_operation(lost[0]).completion is None
    before = len(case.models['handoff'].calls)
    monkeypatch.setattr(case.app.handoff.evidence, 'save', original)
    case.app.handoff.reconcile()
    assert case.app.handoff._known_call(request)['outcome'] == 'valid'
    assert case.app.store.load_d4_operation(lost[0]).completion is not None
    assert len(case.models['handoff'].calls) == before


@pytest.mark.parametrize('output_case', [14], indirect=True)
def test_real_v9_payloads_use_frozen_s4_fields_without_api(output_case):
    from agent_adapters.deepseek_roles import role_config, build_payload
    from agent_core.output_contracts import D8OutputRequest
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    for group, stage in [('inner','inner_complete'),('handoff','intent_evaluation')]:
        envelope = next(e for e in case.models[group].calls if e.output_contract['stage'] == stage)
        role = role_config('inner_action' if group == 'inner' else 'outer_planning', version=9)
        request = replace(envelope.request, role_config_ref=role.role_config_ref)
        payload = build_payload(D8OutputRequest(request, envelope.output_contract,
                                                envelope.instruction, envelope.correction), role)
        assert payload['response_format'] == {'type':'json_object'}
        if stage == 'intent_evaluation':
            context = json.loads(payload['messages'][1]['content'])
            assert context['s4_evaluation_policy']['local_goal'] == evaluation_policy(request)['local_goal']
            assert 'not a verified Fact' in payload['messages'][0]['content']


@pytest.mark.parametrize('output_case', [14], indirect=True)
def test_protected_candidate_keeps_explicit_source_without_plaintext_binding(output_case, monkeypatch):
    from agent_adapters.web import candidate_evidence
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
    from test_candidate_evidence import _TestOnlyProtection
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    plan = next(p for p in case.app.state.plans if p.status.value == 'completed')
    binding = case.app.store.load_d3_plan_binding(plan.plan_id)
    result = case.app.store.load_d3_plan_result(plan.plan_id)
    fact = next(f for f in case.app.state.facts if f.fact_id == result.supporting_fact_id)
    context = D3CandidateContext(case.app.store.load_d3_turn_binding(binding.turn_id), binding, fact.source_ref, result)
    req = case.app._model_request('inner_candidate', f'{fact.source_ref}:inner_candidate', context,
                                  f'candidate/{fact.source_ref}')
    monkeypatch.setattr(candidate_evidence, 'WindowsEvidenceProtection', _TestOnlyProtection)
    protected = D7CandidateEvidenceService(case.directory, case.task, trusted_adapter_ids=('d7_candidate_stage',))
    text = '  S4_PRIVATE_CANDIDATE_EXACT_TEXT  '
    monkeypatch.setattr(case.models['inner'], 'invoke', lambda e, *a: {
        'action': {'kind':'candidate_flag','value': text,'execution_id': context.execution_id}})
    _, candidate = asyncio.run(case.app.inner.propose_candidate(req, case.token, protected))
    pending, metadata = protected.load_pending_for_request(req.model_request_id)
    assert candidate == pending and metadata['byte_size'] == len(text.encode())
    out = case.app.output_boundary.evidence.read('candidate-binding', req.model_request_id)
    assert out['choice'] == {'execution_id': context.execution_id}
    assert out['canonical_provenance']['evidence_version'] == context.plan_result.evidence_sha256
    assert text not in json.dumps(out)
    monkeypatch.setattr(case.models['inner'], 'invoke', lambda *a: pytest.fail('known candidate called model'))
    assert asyncio.run(case.app.inner.propose_candidate(req, case.token, protected))[1] == candidate
    from agent_core.completion_evaluation import check_candidate_task
    assert not check_candidate_task(case.app.config, case.app.output_boundary.evidence,
        case.app.sessions.evidence, case.app.model_evidence, case.app.store, case.task)
    original = case.app.output_boundary.evidence.read
    monkeypatch.setattr(case.app.output_boundary.evidence, 'read', lambda kind, identity:
        None if kind == 'candidate-binding' and identity == req.model_request_id else original(kind, identity))
    with pytest.raises(D3RecoveryRequired, match='s4_candidate_binding'):
        asyncio.run(case.app.inner.propose_candidate(req, case.token, protected))


@pytest.mark.parametrize('output_case', [14], indirect=True)
@pytest.mark.parametrize('bad', ['source', 'extra_binding'])
def test_candidate_bad_choice_never_creates_protected_body(output_case, monkeypatch, bad):
    from agent_adapters.web import candidate_evidence
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
    from test_candidate_evidence import _TestOnlyProtection
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    plan = next(p for p in case.app.state.plans if p.status.value == 'completed')
    binding = case.app.store.load_d3_plan_binding(plan.plan_id)
    result = case.app.store.load_d3_plan_result(plan.plan_id)
    fact = next(f for f in case.app.state.facts if f.fact_id == result.supporting_fact_id)
    context = D3CandidateContext(case.app.store.load_d3_turn_binding(binding.turn_id), binding, fact.source_ref, result)
    request = case.app._model_request('inner_candidate', f'{fact.source_ref}:inner_candidate', context,
                                     f'candidate/{fact.source_ref}')
    monkeypatch.setattr(candidate_evidence, 'WindowsEvidenceProtection', _TestOnlyProtection)
    protected = D7CandidateEvidenceService(case.directory, case.task, trusted_adapter_ids=('d7_candidate_stage',))
    def propose(e, *args):
        action = {'kind':'candidate_flag','value':'PRIVATE_BAD_CANDIDATE','execution_id': context.execution_id}
        if bad == 'source': action['execution_id'] = str(uuid4())
        else: action['evidence_version'] = '0'*64
        return {'action':action}
    monkeypatch.setattr(case.models['inner'], 'invoke', propose)
    with pytest.raises(D3ProposalRejected):
        asyncio.run(case.app.inner.propose_candidate(request, case.token, protected))
    assert protected.load_pending_for_request(request.model_request_id) is None
    assert case.app.output_boundary.evidence.read('candidate-binding', request.model_request_id) is None
    monkeypatch.setattr(case.models['inner'], 'invoke', lambda *a: pytest.fail('rejected candidate was retried'))
    with pytest.raises(D3ProposalRejected):
        asyncio.run(case.app.inner.propose_candidate(request, case.token, protected))


@pytest.mark.parametrize('output_case', [14], indirect=True)
@pytest.mark.parametrize('protected_saved', [False, True])
def test_candidate_process_loss_never_reasks_unknown_and_recovers_known(output_case, monkeypatch, protected_saved):
    from agent_adapters.web import candidate_evidence
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
    from test_candidate_evidence import _TestOnlyProtection
    from test_chain_multi_turn import ProcessLoss
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    plan = next(p for p in case.app.state.plans if p.status.value == 'completed')
    binding = case.app.store.load_d3_plan_binding(plan.plan_id)
    result = case.app.store.load_d3_plan_result(plan.plan_id)
    fact = next(f for f in case.app.state.facts if f.fact_id == result.supporting_fact_id)
    context = D3CandidateContext(case.app.store.load_d3_turn_binding(binding.turn_id), binding, fact.source_ref, result)
    request = case.app._model_request('inner_candidate', f'{fact.source_ref}:inner_candidate', context,
                                     f'candidate/{fact.source_ref}')
    monkeypatch.setattr(candidate_evidence, 'WindowsEvidenceProtection', _TestOnlyProtection)
    protected = D7CandidateEvidenceService(case.directory, case.task, trusted_adapter_ids=('d7_candidate_stage',))
    monkeypatch.setattr(case.models['inner'], 'invoke', lambda e, *args: {'action': {
        'kind':'candidate_flag','value':'PRIVATE_RECOVERY_CANDIDATE','execution_id': context.execution_id}})
    original = protected.save_pending
    def save(*args):
        if protected_saved: original(*args)
        raise ProcessLoss()
    monkeypatch.setattr(protected, 'save_pending', save)
    with pytest.raises(ProcessLoss):
        asyncio.run(case.app.inner.propose_candidate(request, case.token, protected))
    assert case.app.store.load_d3_operation(request.model_request_id).completion is None
    monkeypatch.setattr(protected, 'save_pending', original)
    monkeypatch.setattr(case.models['inner'], 'invoke', lambda *a: pytest.fail('recovery called model'))
    if protected_saved:
        identity, candidate = asyncio.run(case.app.inner.propose_candidate(request, case.token, protected))
        assert identity == request.model_request_id
        assert candidate.execution_id == context.execution_id
        assert case.app.store.load_d3_operation(identity).completion is not None
    else:
        with pytest.raises(D3RecoveryRequired, match='outcome_unknown'):
            asyncio.run(case.app.inner.propose_candidate(request, case.token, protected))
