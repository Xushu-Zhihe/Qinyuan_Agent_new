"""D9-2 close: reachable decisions, strict evidence and old-run isolation."""

from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_adapters.deepseek_roles import build_payload, role_config
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.execution_feasibility import KEY, freeze
from agent_core.output_contracts import decode_response, output_schema
from agent_core.plan_policy import policy_for
from agent_core.turn_modeling import digest
from test_output import output_case, run as run_output, reopen
from test_governance_selection import modern
from test_rule_evidence import calls


@pytest.fixture
def case17(output_case):
    assert output_case.app.config.contract_version == 17
    for model in output_case.models.values():
        model.transform = modern
    return output_case


def complete_only(raw):
    return {**raw, 'constraints': [{'kind': 'allowed_action_kinds', 'scope_kind': 'intent',
                                    'allowed_action_kinds': ['complete_intent']}]}


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_current_chain_and_restart_accept_ready_complete_only(case17):
    c = case17
    c.models['review'].transform = lambda e, raw: complete_only(raw) if e.output_contract['stage'] == 'progress_review' else modern(e, raw)
    result = run_output(c)
    assert result.stop_reason == 'search_exhausted', result
    assert len(c.app.state.executions) == 2 and len(c.app.state.facts) == 1
    progress = calls(c, 'progress_review')
    assert len(progress) == 1
    p = json.loads(progress[0].request.context_summary)[KEY]
    assert p['continue_paths'] == [['complete_intent']]
    count = sum(len(m.calls) for m in c.models.values())
    reopen(c)
    assert run_output(c).stop_reason == 'search_exhausted'
    assert count == sum(len(m.calls) for m in c.models.values())


@pytest.mark.parametrize('output_case', [17], indirect=True)
@pytest.mark.parametrize('always', [False, True])
def test_start_conflict_corrects_once_before_any_plan_or_tool(case17, always):
    c = case17
    def change(e, raw):
        if e.request.review_scope == 'turn_start' and (always or e.correction is None):
            return complete_only(raw)
        return modern(e, raw)
    c.models['review'].transform = change
    result = run_output(c)
    start = [e for e in calls(c, 'review') if e.request.review_scope == 'turn_start']
    assert len(start) == 2
    assert start[1].correction[KEY]['continue_paths'] == [['probe', 'complete_intent']]
    assert start[1].request.review_attempt == 2
    assert c.app.output_boundary.evidence.read('review-binding', start[0].request.review_id) is None
    if always:
        assert result.recovery_required
        assert not c.app.state.plans and not c.app.state.executions and not c.app.state.facts
        count = len(start)
        reopen(c)
        assert run_output(c).recovery_required
        assert count == len([e for e in calls(c, 'review') if e.request.review_scope == 'turn_start'])
    else:
        assert result.stop_reason == 'search_exhausted', result
        assert len(c.app.state.executions) == 2


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_plan_guard_rejects_changed_limits_even_with_valid_proposal(case17, monkeypatch):
    c = case17
    register = c.app.control.register_plan
    def changed(*a, **kw):
        monkeypatch.setattr(c.app.validator, 'action_limits', lambda *a: ({'complete_intent'}, 300))
        return register(*a, **kw)
    monkeypatch.setattr(c.app.control, 'register_plan', changed)
    result = run_output(c)
    assert result.recovery_required
    assert len(calls(c, 'inner_plan')) == 1
    assert not c.app.state.plans and not c.app.state.executions


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_review_snapshot_tamper_does_not_change_recovered_authority(case17):
    c = case17
    assert run_output(c).stop_reason == 'search_exhausted'
    e = calls(c, 'progress_review')[0]
    output = FileD8OutputEvidence(c.directory/'d8-output', create=False)
    snapshot = output.read('review-request', e.request.review_id)
    summary = json.loads(snapshot['context_summary'])
    summary[KEY]['continue_paths'] = [['probe', 'complete_intent']]
    snapshot['context_summary'] = json.dumps(summary)
    path = next((c.directory/'d8-output').glob('*review-request-' + e.request.review_id + '.json'))
    path.write_text(json.dumps({'payload': snapshot, 'sha256': digest(snapshot)}), encoding='utf-8')
    with pytest.raises(ExecutionRecoveryRequired):
        c.app.review_diagnostics.known(e.request)
    assert len(calls(c, 'progress_review')) == 1


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_real_payload_v12_has_same_frozen_path_policy_without_api(case17):
    c = case17
    assert run_output(c).stop_reason == 'search_exhausted'
    for stage, role in [('inner_plan', 'inner_action'), ('review', 'outer_planning'), ('progress_review', 'outer_planning')]:
        e = calls(c, stage)[0]
        role = role_config(role, version=12)
        request = replace(e.request, role_config_ref=role.role_config_ref) if stage == 'inner_plan' else e.request
        payload = build_payload(replace(e, request=request), role)
        assert 'Contract17' in payload['messages'][0]['content']
        user = json.loads(payload['messages'][1]['content'])
        packet = json.loads(user['history_context'] if stage == 'inner_plan' else user['context_summary'])
        assert packet[KEY]['version'] == 1


@pytest.mark.parametrize('output_case', [16], indirect=True)
def test_old16_start_still_parses_original_complete_only_rule(output_case):
    c = output_case
    for model in c.models.values(): model.transform = modern
    assert run_output(c).stop_reason == 'search_exhausted'
    request = [e.request for e in calls(c, 'review') if e.request.review_scope == 'turn_start'][0]
    assert KEY not in json.loads(request.context_summary)
    from agent_core.review_policy import validate_and_bind
    raw = {'decision': 'continue_current_intent', 'rationale': 'Preserve old semantics.',
           'source_ids': list(request.source_versions), 'constraints': []}
    assert validate_and_bind(complete_only(raw), request)['constraints'][0]['allowed_action_kinds'] == ['complete_intent']


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_unknown_start_review_is_not_reasked(case17):
    c = case17
    def unknown(e, raw):
        raise RuntimeError('controlled transport loss')
    c.models['review'].transform = unknown
    assert run_output(c).recovery_required
    assert len(calls(c, 'review')) == 1 and not c.app.state.plans
    reopen(c)
    assert run_output(c).recovery_required
    assert len(calls(c, 'review')) == 1


@pytest.mark.parametrize('output_case', [17], indirect=True)
@pytest.mark.parametrize('damage', [False, True])
def test_known_start_diagnostic_recovery_never_reasks_root(case17, monkeypatch, damage):
    from test_chain_multi_turn import ProcessLoss
    c = case17
    c.models['review'].transform = lambda e, raw: complete_only(raw) if (
        e.request.review_scope == 'turn_start' and e.correction is None) else modern(e, raw)
    finish, crashed = c.app.review_ledger.finish_attempt, []
    def crash(request, **data):
        if data['outcome'] == 'invalid_response' and not crashed:
            crashed.append(request.review_id)
            raise ProcessLoss()
        return finish(request, **data)
    monkeypatch.setattr(c.app.review_ledger, 'finish_attempt', crash)
    with pytest.raises(ProcessLoss):
        run_output(c)
    assert len(crashed) == 1
    monkeypatch.setattr(c.app.review_ledger, 'finish_attempt', finish)
    if damage:
        next((c.directory/'d8-output').glob('*diagnostic-' + crashed[0] + '.json')).write_text('{}', encoding='utf-8')
    reopen(c)
    if damage:
        with pytest.raises(ExecutionRecoveryRequired):
            asyncio.run(c.app.resume(c.task, c.token))
        assert not c.app.state.plans
        assert sum(e.request.review_id == crashed[0] for e in calls(c, 'review')) == 1
        return
    result = asyncio.run(c.app.resume(c.task, c.token))
    assert sum(e.request.review_id == crashed[0] for e in calls(c, 'review')) == 1
    assert result.stop_reason == 'search_exhausted'
    assert len(c.app.state.executions) == 2


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_replan_conflict_corrects_without_widening_committed_limits(case17):
    c = case17
    c.models['fact'].transform = lambda e, raw: {**raw, 'decision': 'reject', 'reason': 'Revise scope.'} if len(calls(c, 'fact_review')) == 1 else raw
    def change(e, raw):
        if e.output_contract['stage'] == 'progress_review' and len(calls(c, 'progress_review')) <= 2:
            raw = {**raw, 'decision': 'replan_current_intent', 'constraints': []}
            return complete_only(raw) if e.correction is None else raw
        return modern(e, raw)
    c.models['review'].transform = change
    c.models['inner'].transform = lambda e, raw: {**raw, 'template': 'sha256_only'} if (
        e.output_contract['stage'] == 'inner_plan' and e.request.context.prior_plan_ids) else modern(e, raw)
    result = run_output(c)
    assert result.stop_reason == 'search_exhausted', result
    progress = calls(c, 'progress_review')
    assert len(progress) >= 2
    assert progress[1].correction[KEY]['replan_paths'] == [['probe', 'complete_intent']]
    assert len(c.app.state.plans) == 2 and len(c.app.state.facts) == 1


def test_public17_simulation_and_frozen_profile(tmp_path, monkeypatch):
    from agent_adapters import task_cli
    from test_public_simulation import request
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v17'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    result = task_cli.dispatch_run_task(SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path/'state', events=None))
    assert result['contract_version'] == 17 and not result['recovery']['required'], result
    assert result['goal_evaluation'] == 'not_evaluated' and len(result['verified_facts']) == 1


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_child_parent_fact_does_not_make_new_intent_ready(case17):
    c = case17
    def planning(request, raw):
        if request.stage == 'tree_planning' and raw['kind'] == 'candidates':
            raw = {**raw, 'intents': raw['intents'][:1]}
            if request.turn_id is not None:
                parent = next(i.ref for i in request.history if i.record_type == 'verified_fact')
                raw['intents'][0].update(description='Review the existing parent observation without a new probe.',
                                         intent_type='review', dependency_refs=[parent])
        return raw
    c.models['handoff'].adapter.modify = planning
    def review(e, raw):
        if e.request.review_scope == 'turn_start':
            context = json.loads(e.request.context_summary)
            if any(i['record_type'] == 'verified_fact' for i in context['strategic_history']['items']):
                assert context[KEY]['continue_paths'] == [['probe', 'complete_intent']]
                return complete_only(raw)
        return modern(e, raw)
    c.models['review'].transform = review
    result = run_output(c)
    assert result.recovery_required
    assert len(c.app.state.turns) == 2 and len(c.app.state.plans) == 1
    assert len(c.app.state.executions) == 2 and len(c.app.state.facts) == 1
    child = [e for e in calls(c, 'review') if e.request.review_scope == 'turn_start'
             and e.request.turn_id != c.app.state.turns[0].turn_id]
    assert len(child) == 2
    assert not c.app.store.list_d4_unresolved_operations(c.task)


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_saved_observation_can_continue_fact_review_without_reprobe(case17):
    c = case17
    c.models['fact'].transform = lambda e, raw: {**raw, 'decision': 'reject', 'reason': 'Revise claim.'} if len(calls(c, 'fact_review')) == 1 else raw
    c.models['review'].transform = lambda e, raw: complete_only(raw) if e.request.review_scope == 'action' else modern(e, raw)
    result = run_output(c)
    assert result.stop_reason == 'search_exhausted', result
    assert len(calls(c, 'fact_proposal')) == 2
    assert len(c.app.state.executions) == 2 and len(c.app.state.facts) == 1


@pytest.mark.parametrize('output_case', [17], indirect=True)
def test_only_explicit_same_intent_reuse_survives_probe_prohibition(case17, monkeypatch):
    c = case17
    assert run_output(c).stop_reason == 'search_exhausted'
    req = calls(c, 'inner_plan')[0].request
    result = c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id)
    monkeypatch.setattr(c.app.validator, 'action_limits', lambda *a: ({'complete_intent'}, 300))
    p = freeze(c.app, req.context.turn_binding.turn_id, req.context.intent_id)
    packet = json.loads(req.history_context)
    packet[KEY] = p
    req = replace(req, context=replace(req.context, prior_plan_ids=(result.plan_id,),
                  reusable_proof_ids=(result.proof_id,)), history_context=json.dumps(packet))
    choices = policy_for(req)['allowed_choices']
    assert choices == [{'template': 'sha256_only', 'observation_mode': 'reuse_saved_proof', 'source_proof_id': result.proof_id}]
    assert not decode_response(choices[0], output_schema(req, contract_version=17)[1])[1]
    assert decode_response({**choices[0], 'source_proof_id': None}, output_schema(req, contract_version=17)[1])[1]


@pytest.mark.parametrize('output_case', [17], indirect=True)
@pytest.mark.parametrize('classification', ['meaningful', 'repetitive', 'inconclusive'])
def test_restricted_repair_keeps_no_tool_boundary(case17, monkeypatch, classification):
    from test_progress_review import test_restricted_progress_uses_joint_choices_and_never_opens_tools
    test_restricted_progress_uses_joint_choices_and_never_opens_tools(case17, monkeypatch, classification)


def test_workbench17_child_pause_and_cancel(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile='d8_v17')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'fixture'), 7)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['status'] == 'paused' and run['result']['contract_version'] == 17, run
    assert run['result']['goal_evaluation'] == 'not_evaluated' and len(run['result']['verified_facts']) == 1
    workbench.runner.cancel(challenge['id'])
    cancelled = _wait_for_cli(workbench, challenge['id'])
    assert cancelled['status'] == 'cancelled' and cancelled['result']['cleanup_status'] == 'confirmed'
