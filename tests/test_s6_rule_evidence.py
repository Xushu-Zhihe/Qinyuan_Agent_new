"""Contract16 observable choices, trusted comparison and long review recovery."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import task_cli
from agent_adapters.d8_model import build_payload, role_config
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.conclusion_contracts import D16Observation, D8ModelRequest
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.output_contracts import decode_response, output_schema, D8ModelResponse
from agent_core.plan_policy import policy_for, plan_schema, validate
from agent_core.turn_modeling import digest
from test_d9_public import request
from test_d8_output import output_case, run as run_output, reopen
from test_s5_governance_selection import modern
from test_d3_chain import ProcessLoss


@pytest.fixture
def case16(output_case):
    assert output_case.app.config.contract_version == 16
    for model in output_case.models.values():
        model.transform = modern
    return output_case


def calls(case, stage):
    return [e for model in case.models.values() for e in model.calls if e.output_contract['stage'] == stage]


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_chain_and_known_restart_preserve_comparison(case16):
    c = case16
    assert run_output(c).stop_reason == 'search_exhausted'
    assert len(c.app.state.facts) == 1
    proposal, review = calls(c, 'fact_proposal')[0].request, calls(c, 'fact_review')[0].request
    assert isinstance(proposal.observation, D16Observation)
    assert proposal.observation == review.observation
    source = review.observation
    binding = c.app.store.load_d3_turn_binding(source.turn_id)
    assert source.expected_sha256 == source.observed_sha256 == binding.expected_sha256
    assert source.digest_matches_expected is True
    assert source.rule_binding_sha256 == digest(asdict(binding))
    assert source.source_version_kind == 'proof_receipt_turn_binding_sha256'
    assert source.source_version_sha256 != source.observed_sha256
    assert D8ModelRequest.from_dict(json.loads(json.dumps(asdict(proposal)))) == proposal
    before = {name: len(m.calls) for name, m in c.models.items()}
    reopen(c)
    assert run_output(c).stop_reason == 'search_exhausted'
    assert before == {name: len(m.calls) for name, m in c.models.items()}


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_provider_payload_explains_choices_and_comparison_without_api(case16):
    c = case16
    assert run_output(c).stop_reason == 'search_exhausted'
    for stage, role in [('inner_plan', 'inner_action'), ('fact_proposal', 'inner_action'),
                        ('fact_review', 'outer_planning'), ('progress_review', 'outer_planning')]:
        e = calls(c, stage)[0]
        actual_role = role_config(role, version=11)
        req = replace(e.request, role_config_ref=actual_role.role_config_ref) if stage != 'progress_review' else e.request
        payload = build_payload(replace(e, request=req), actual_role)
        system = payload['messages'][0]['content']
        user = json.loads(payload['messages'][1]['content'])
        if stage == 'inner_plan':
            assert json.dumps(policy_for(req), ensure_ascii=False) in system
        if stage.startswith('fact_'):
            assert user['observation']['expected_sha256'] == user['observation']['observed_sha256']
            assert 'not a file' in system
        if stage == 'progress_review':
            assert 'no per-field character ceiling' in system


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_reuse_requires_explicit_member_of_frozen_proof_choices(case16):
    c = case16
    assert run_output(c).stop_reason == 'search_exhausted'
    req = calls(c, 'inner_plan')[0].request
    proof = calls(c, 'fact_review')[0].request.observation.proof_id
    req = replace(req, context=replace(req.context,
        prior_plan_ids=(c.app.state.plans[0].plan_id,), reusable_proof_ids=(proof,)))
    choice = {'template': 'sha256_only', 'observation_mode': 'reuse_saved_proof', 'source_proof_id': proof}
    assert not decode_response(choice, plan_schema(req))[1]
    from agent_core.dual_llm_modeling import parse_d3_plan_proposal, D3ProposalRejected
    validate(req, parse_d3_plan_proposal(choice))
    foreign = {**choice, 'source_proof_id': str(uuid4())}
    assert decode_response(foreign, plan_schema(req))[1]
    with pytest.raises(D3ProposalRejected): validate(req, parse_d3_plan_proposal(foreign))


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_wrong_initial_template_has_targeted_single_correction(case16):
    c = case16
    def change(e, raw):
        if e.output_contract['stage'] == 'inner_plan' and e.correction is None:
            return {**raw, 'template': 'sha256_only'}
        return modern(e, raw)
    c.models['inner'].transform = change
    assert run_output(c).stop_reason == 'search_exhausted'
    plans = calls(c, 'inner_plan')
    assert len(plans) == 2
    assert any(i['path'] == '$/template' for i in plans[1].correction['issues'])
    assert plans[1].correction['allowed_plan_choices'] == policy_for(plans[0].request)['allowed_choices']
    assert plans[1].request.correction_of_model_request_id == plans[0].request.model_request_id
    assert len(c.app.state.executions) == 2
    reopen(c)
    assert run_output(c).stop_reason == 'search_exhausted'
    assert len(calls(c, 'inner_plan')) == 2


@pytest.mark.parametrize('output_case', [16], indirect=True)
@pytest.mark.parametrize('damage', [False, True])
def test_plan_saved_diagnostic_recovery_never_reasks_root(case16, monkeypatch, damage):
    c = case16
    c.models['inner'].transform = lambda e, raw: {**raw, 'template': 'sha256_only'} if (
        e.output_contract['stage'] == 'inner_plan' and e.correction is None) else modern(e, raw)
    save = FileD8OutputEvidence.save
    tripped = []
    def crash(port, kind, identity, record):
        save(port, kind, identity, record)
        if kind == 'diagnostic' and record['stage'] == 'inner_plan' and not tripped:
            tripped.append(identity)
            raise ProcessLoss()
    monkeypatch.setattr(FileD8OutputEvidence, 'save', crash)
    # The worker exception is intentionally surfaced as a restricted unknown stop.
    assert run_output(c).recovery_required
    assert len(tripped) == 1
    monkeypatch.setattr(FileD8OutputEvidence, 'save', save)
    if damage:
        path = next((c.directory/'d8-output').glob('*diagnostic-' + tripped[0] + '.json'))
        path.write_text('{}', encoding='utf-8')
    reopen(c)
    result = run_output(c)
    assert sum(e.request.model_request_id == tripped[0] for e in calls(c, 'inner_plan')) == 1
    if damage:
        assert result.recovery_required and len(calls(c, 'inner_plan')) == 1
    else:
        assert result.stop_reason == 'search_exhausted'
        assert len(calls(c, 'inner_plan')) == 2
        assert len(c.app.state.executions) == 2


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_unknown_plan_does_not_get_correction_or_replay(case16):
    from agent_core.modeling import ModelError, ModelErrorKind
    c = case16
    c.models['inner'].transform = lambda e, raw: ModelError(ModelErrorKind.TIMED_OUT, 'controlled_unknown') if (
        e.output_contract['stage'] == 'inner_plan') else raw
    assert run_output(c).recovery_required
    reopen(c)
    assert run_output(c).recovery_required
    assert len(calls(c, 'inner_plan')) == 1 and not c.app.state.executions


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_fact_rejected_then_replan_uses_only_second_template(case16):
    c = case16
    def fact(e, raw):
        return {**raw, 'decision': 'reject', 'reason': 'Candidate scope needs revision.'} if len(calls(c, 'fact_review')) == 1 else raw
    def progress(e, raw):
        return {**raw, 'decision': 'replan_current_intent'} if e.output_contract['stage'] == 'progress_review' and len(calls(c, 'progress_review')) == 1 else raw
    def wrong_second(e, raw):
        if (e.output_contract['stage'] == 'inner_plan' and e.request.context.prior_plan_ids
                and e.correction is None):
            return {**raw, 'template': 'size_then_sha256'}
        return modern(e, raw)
    c.models['fact'].transform = fact
    c.models['review'].transform = progress
    c.models['inner'].transform = wrong_second
    result = run_output(c)
    assert result.stop_reason == 'search_exhausted', result
    plans = calls(c, 'inner_plan')
    assert len(plans) == 3
    assert policy_for(plans[1].request)['allowed_choices'] == [
        {'template': 'sha256_only', 'observation_mode': 'new_observation', 'source_proof_id': None}]
    assert plans[2].correction['allowed_plan_choices'] == policy_for(plans[1].request)['allowed_choices']
    assert len(c.app.state.facts) == 1 and len(c.app.state.plans) == 2
    assert not c.app.store.list_d4_unresolved_operations(c.task)


@pytest.mark.parametrize('output_case', [16], indirect=True)
@pytest.mark.parametrize('length', [513, 2048, 8000])
def test_long_review_reasons_applied_measured_and_replayed(case16, length):
    c = case16
    def long(e, raw):
        raw = {**raw, 'rationale': '理' * length}
        if e.output_contract['stage'] == 'progress_review':
            raw['progress'] = {**raw['progress'], 'reason': '据' * length}
        return raw
    c.models['review'].transform = long
    assert run_output(c).stop_reason == 'search_exhausted'
    reviews = calls(c, 'review') + calls(c, 'progress_review')
    assert reviews and all(e.correction is None for e in reviews)
    for e in reviews:
        record = c.app.output_boundary.evidence.read('review-binding', e.request.review_id)
        assert record['reason_lengths']['rationale'] == length
        if e.output_contract['stage'] == 'progress_review':
            assert record['reason_lengths']['progress.reason'] == length
    before = len(reviews)
    reopen(c)
    assert run_output(c).stop_reason == 'search_exhausted'
    assert len(calls(c, 'review')) + len(calls(c, 'progress_review')) == before


@pytest.mark.parametrize('output_case', [16], indirect=True)
@pytest.mark.parametrize('bad', ['', ' ', '\x00'])
def test_invalid_progress_reason_still_rejected_and_corrected(case16, bad):
    c = case16
    def change(e, raw):
        if e.output_contract['stage'] == 'progress_review' and e.correction is None:
            return {**raw, 'progress': {**raw['progress'], 'reason': bad}}
        return raw
    c.models['review'].transform = change
    assert run_output(c).stop_reason == 'search_exhausted'
    assert len(calls(c, 'progress_review')) == 2


@pytest.mark.parametrize('output_case', [16], indirect=True)
@pytest.mark.parametrize('change', ['missing', 'foreign_expected', 'foreign_binding'])
def test_fact_projection_cannot_be_forged_or_downgraded(case16, change):
    c = case16
    assert run_output(c).stop_reason == 'search_exhausted'
    req = calls(c, 'fact_review')[0].request
    if change == 'missing':
        from agent_core.conclusion_contracts import D8Observation
        fields = {k: v for k, v in asdict(req.observation).items() if k in D8Observation.__dataclass_fields__}
        forged = replace(req, observation=D8Observation(**fields))
    elif change == 'foreign_expected':
        with pytest.raises(ValueError, match='s6_observation_comparison_invalid'):
            replace(req.observation, expected_sha256='0' * 64)
        return
    else:
        forged = replace(req, observation=replace(req.observation, rule_binding_sha256='0' * 64))
    before = len(calls(c, 'fact_review'))
    with pytest.raises(D3RecoveryRequired, match='s6_fact_source_projection_changed'):
        c.app.conclusions._known(forged, repair=False)
    assert len(calls(c, 'fact_review')) == before


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_reason_measurement_tamper_pauses(case16):
    c = case16
    assert run_output(c).stop_reason == 'search_exhausted'
    request = calls(c, 'progress_review')[0].request
    evidence = c.app.output_boundary.evidence
    # Corrupt the checksummed payload rather than bypassing the immutable writer.
    file = next((c.directory/'d8-output').glob('*review-binding-' + request.review_id + '.json'))
    envelope = json.loads(file.read_text(encoding='utf-8'))
    envelope['payload']['reason_lengths']['rationale'] += 1
    envelope['sha256'] = digest(envelope['payload'])
    file.write_text(json.dumps(envelope), encoding='utf-8')
    with pytest.raises(D3RecoveryRequired, match='d9_review_binding_changed'):
        c.app.review_diagnostics.known(request)


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_whole_output_limit_remains_terminal(case16):
    c = case16
    def huge(e, raw):
        if e.output_contract['stage'] == 'progress_review':
            return {**raw, 'rationale': 'x' * (65536 + 1)}
        return raw
    c.models['review'].transform = huge
    result = run_output(c)
    assert result.recovery_required
    assert len(calls(c, 'progress_review')) == 1
    assert not calls(c, 'handoff_summary')


def test_public16_routes_and_explicit_mode(tmp_path, monkeypatch):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v16'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    result = task_cli.dispatch_run_task(SimpleNamespace(request='unused', mode='simulated',
        state_dir=tmp_path/'state', events=None))
    assert result['contract_version'] == 16 and not result['recovery']['required'], result
    assert len(result['verified_facts']) == 1


def test_workbench16_child_and_terminal_cancel(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_d7_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile='d8_v16')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'fixture'), 7)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['status'] == 'paused' and run['result']['contract_version'] == 16, run
    assert len(run['result']['verified_facts']) == 1
    assert run['result']['goal_evaluation'] == 'not_evaluated'
    workbench.runner.cancel(challenge['id'])
    cancelled = _wait_for_cli(workbench, challenge['id'])
    assert cancelled['status'] == 'cancelled' and cancelled['result']['cleanup_status'] == 'confirmed'


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_old15_progress_512_ceiling_preserved(output_case):
    c = output_case
    for model in c.models.values(): model.transform = modern
    def change(e, raw):
        if e.output_contract['stage'] == 'progress_review' and e.correction is None:
            return {**raw, 'progress': {**raw['progress'], 'reason': 'x' * 513}}
        return raw
    c.models['review'].transform = change
    assert run_output(c).stop_reason == 'search_exhausted'
    assert len(calls(c, 'progress_review')) == 2
    assert not isinstance(calls(c, 'fact_review')[0].request.observation, D16Observation)
