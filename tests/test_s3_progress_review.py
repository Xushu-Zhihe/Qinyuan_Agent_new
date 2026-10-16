"""Contract13 progress evidence, frozen choices and bound recovery through the core."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_adapters import cli, task_cli
from agent_adapters.d8_simulation import SimulationModel
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.run_files import immutable_read
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_core.progress_policy import policy_for
from agent_core.sqlite_control_store import SqliteControlStore
from test_d9_public import request
from test_d8_output import output_case, run as run_output, reopen as reopen_output


def run_progress(tmp_path, monkeypatch, *, change=None):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v13'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    seen = []
    original = SimulationModel.invoke
    def invoke(model, envelope, *args):
        value = original(model, envelope, *args)
        if envelope.output_contract['stage'] == 'progress_review':
            seen.append(envelope)
            if change: value = change(envelope, value)
        return value
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    result = task_cli.dispatch_run_task(args)
    return result, args, seen, args.state_dir / result['task_run_id']


def test_public13_progress_reaches_completion_and_handoff(tmp_path, monkeypatch):
    result, args, seen, directory = run_progress(tmp_path, monkeypatch)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    assert result['contract_version'] == 13 and not result['recovery']['required'], result
    assert len(seen) == 1
    p = policy_for(seen[0].request)
    assert p['decision_watermark'] == 4 and p['mode'] == 'ordinary'
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    binding = output.read('review-binding', seen[0].request.review_id)
    assert binding['stage'] == 'progress_review' and binding['progress_watermark'] == 4
    assert binding['progress_window_sha256'] == p['window_sha256']
    assert p['required_latest_decision_ref'] in binding['wire_response']['progress']['evidence_refs']
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert store.list_d8_handoffs(result['task_run_id'])
        assert store.list_d4_unresolved_operations(result['task_run_id']) == ()
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('cancel called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    assert cli.dispatch(args)['cleanup_status'] == 'confirmed'


def test_scope_choice_is_preserved_and_ids_are_program_bound(tmp_path, monkeypatch):
    def change(e, raw):
        raw['constraints'] = [{'kind': 'allowed_action_kinds', 'scope_kind': s,
                              'allowed_action_kinds': ['probe','complete_intent']} for s in ('turn','intent')]
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['errors'] == [] and not result['recovery']['required'], result
    record = FileD8OutputEvidence(directory / 'd8-output', create=False).read('review-binding', seen[0].request.review_id)
    assert all('scope_id' not in c for c in record['wire_response']['constraints'])
    assert [c['scope_id'] for c in record['canonical_response']['constraints']] == [seen[0].request.turn_id, seen[0].request.intent_id]


def test_program_watermark_does_not_depend_on_source_ids_selection(tmp_path, monkeypatch):
    def change(e, raw):
        raw['source_ids'] = [next(ref for ref in e.request.source_versions if not ref.startswith('d8-decision:'))]
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['errors'] == [] and not result['recovery']['required'], result
    assert len(seen) == 1
    record = FileD8OutputEvidence(directory / 'd8-output', create=False).read('review-binding', seen[0].request.review_id)
    assert not any(ref.startswith('d8-decision:') for ref in record['wire_response']['source_ids'])
    assert policy_for(seen[0].request)['required_latest_decision_ref'] in record['wire_response']['progress']['evidence_refs']


@pytest.mark.parametrize('failure', ['fixed_probe','scope_id','latest','decision','classification','source','duplicate','timeout','pause_constraints'])
def test_format_rejection_has_one_targeted_correction_and_no_bad_body(tmp_path, monkeypatch, failure):
    def change(e, raw):
        if e.correction is not None: return raw
        if failure == 'fixed_probe': raw['constraints'] = [{'kind':'allowed_action_kinds','scope_kind':'intent','allowed_action_kinds':['fixed_probe']}]
        elif failure == 'scope_id': raw['constraints'] = [{'kind':'max_action_timeout_seconds','scope_kind':'turn','scope_id':'PRIVATE_REJECTED_VALUE','max_action_timeout_seconds':5}]
        elif failure == 'latest': raw['progress']['evidence_refs'] = [next(r for r in e.request.source_versions if r != policy_for(e.request)['required_latest_decision_ref'])]
        elif failure == 'decision': raw['decision'] = 'switch_sibling_intent'
        elif failure == 'classification': raw['progress']['classification'] = 'PRIVATE_REJECTED_VALUE'
        elif failure == 'source': raw['source_ids'] = ['PRIVATE_REJECTED_VALUE']
        elif failure == 'duplicate': raw['progress']['evidence_refs'] *= 2
        elif failure == 'timeout': raw['constraints'] = [{'kind':'max_action_timeout_seconds','scope_kind':'intent','max_action_timeout_seconds':301}]
        else:
            raw['progress']['classification'], raw['decision'] = 'inconclusive', 'pause_current_intent'
            raw['constraints'] = [{'kind':'max_action_timeout_seconds','scope_kind':'intent','max_action_timeout_seconds':5}]
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['errors'] == [] and not result['recovery']['required'], result
    assert len(seen) == 2 and seen[1].correction['reason'] == 'output_format_invalid'
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    assert output.read('review-binding', seen[0].request.review_id) is None
    issues = output.read('diagnostic', seen[0].request.review_id)['issues']
    if failure == 'latest': assert any(i['code'] == 'array_required_item_missing' and i['path'] == '$/progress/evidence_refs' for i in issues)
    if failure == 'decision': assert any(i['path'] == '$/decision' for i in issues)
    assert 'PRIVATE_REJECTED_VALUE' not in ''.join(p.read_text(encoding='utf-8') for p in (directory / 'd8-output').rglob('*.json'))


@pytest.mark.parametrize('field', ['rationale','reason'])
def test_business_diagnostic_retains_only_fixed_safe_issues(tmp_path, monkeypatch, field):
    def change(e, raw):
        if e.correction is None:
            if field == 'rationale': raw[field] = 'PRIVATE_REJECTED_VALUE\x00'
            else: raw['progress']['reason'] = ' '
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['errors'] == [] and not result['recovery']['required'], result
    assert len(seen) == 2 and seen[1].correction['reason'] == 'output_business_invalid'
    diag = FileD8OutputEvidence(directory / 'd8-output', create=False).read('business-diagnostic', seen[0].request.review_id)
    assert diag['stage'] == 'progress_review' and diag['progress_watermark'] == 4
    assert diag['issues'][0]['code'] == ('progress_rationale_invalid' if field == 'rationale' else 'progress_reason_invalid')
    assert 'PRIVATE_REJECTED_VALUE' not in json.dumps(diag)


def test_inconclusive_progress_pauses_and_replay_never_gets_next_decision(tmp_path, monkeypatch):
    def change(e, raw):
        raw['progress']['classification'], raw['decision'] = 'inconclusive', 'pause_current_intent'
        return raw
    result, args, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['recovery']['required'] and len(seen) == 1, result
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert len(store.list_d8_decisions(result['task_run_id'])) == 4
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('pause replay called model'))
    args.command, args.task_run_id = 'resume', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']


@pytest.mark.parametrize('stage', ['binding','business_diagnostic','format_diagnostic','completion'])
def test_interrupted_progress_recovers_only_complete_known_evidence(tmp_path, monkeypatch, stage):
    save = FileD8OutputEvidence.save
    complete = SqliteControlStore.complete_d4_operation
    tripped = []
    def saved(port, kind, identity, record):
        target = {'binding':'review-binding','business_diagnostic':'business-diagnostic','format_diagnostic':'diagnostic'}.get(stage)
        if kind == target and record.get('stage') == 'progress_review' and not tripped:
            tripped.append(identity)
            save(port, kind, identity, record)
            raise RuntimeError('controlled saved evidence loss')
        return save(port, kind, identity, record)
    def completed(store, completion):
        record = FileD8OutputEvidence(store.path.parent / 'd8-output', create=False).read('contract', completion.operation_id)
        if stage == 'completion' and record is not None and record['stage'] == 'progress_review' and not tripped:
            tripped.append(completion.operation_id)
            raise SystemExit('controlled completion loss')
        return complete(store, completion)
    monkeypatch.setattr(FileD8OutputEvidence, 'save', saved)
    if stage == 'completion': monkeypatch.setattr(SqliteControlStore, 'complete_d4_operation', completed)
    def change(e, raw):
        if stage == 'business_diagnostic': raw['progress']['reason'] = ' '
        if stage == 'format_diagnostic': raw['progress']['evidence_refs'] = []
        return raw
    try:
        result, args, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    except SystemExit:
        directory = next(p for p in (tmp_path / 'state').iterdir() if (p / 'control.sqlite').exists())
        args = SimpleNamespace(state_dir=tmp_path / 'state')
        result = {'task_run_id': directory.name}
    assert len(tripped) == 1
    monkeypatch.setattr(FileD8OutputEvidence, 'save', save)
    monkeypatch.setattr(SqliteControlStore, 'complete_d4_operation', complete)
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('known progress recovery called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    cancelled = cli.dispatch(args)
    assert cancelled['cleanup_status'] == 'confirmed', cancelled
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert store.load_d4_operation(tripped[0]).completion is not None


@pytest.mark.parametrize('damage', ['binding','mode','watermark','latest','scope','window','contract','request','result'])
def test_corrupt_bound_progress_pauses_and_preserves_evidence(tmp_path, monkeypatch, damage):
    from agent_core.turn_modeling import digest
    result, args, seen, directory = run_progress(tmp_path, monkeypatch)
    identity = seen[0].request.review_id
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    kind = {'contract':'contract','request':'review-request','result':'review-result'}.get(damage, 'review-binding')
    path = next(p for p in (directory / 'd8-output').rglob('*.json') if f'-{kind}-{identity}' in p.name)
    if damage == 'binding': path.unlink()
    else:
        envelope = json.loads(path.read_text(encoding='utf-8'))
        value = envelope['payload']
        if damage == 'watermark': value['progress_watermark'] += 4
        elif damage == 'window': value['progress_window_sha256'] = 'f'*64
        elif damage == 'latest': value['wire_response']['progress']['evidence_refs'] = [result['task_run_id']]
        elif damage == 'scope': value['canonical_response']['constraints'] = [{'scope_kind':'intent','scope_id':result['task_run_id']}]
        elif damage == 'mode': value['stage'] = 'review'
        elif damage == 'contract': value['output_contract']['stage'] = 'review'
        elif damage == 'request': value['checkpoint_id'] += 1
        else: value['response']['decision'] = 'pause_current_intent'
        envelope['sha256'] = digest(value)
        path.write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')
    before = (directory / 'control.sqlite').read_bytes()
    assert snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))['recovery']['required']
    assert (directory / 'control.sqlite').read_bytes() == before
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('corrupt progress called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    cancelled = cli.dispatch(args)
    assert cancelled['recovery']['required'] and cancelled['cleanup_status'] != 'confirmed', cancelled


@pytest.mark.parametrize('output_case', [13], indirect=True)
@pytest.mark.parametrize('classification', ['meaningful','repetitive','inconclusive'])
def test_restricted_progress_uses_joint_choices_and_never_opens_tools(output_case, monkeypatch, classification):
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    case = output_case
    records = []
    def query(e, raw):
        if e.output_contract['stage'] == 'inner_plan': return {'kind':'history_query','query':{}}
        return raw
    case.models['inner'].transform = query
    original = case.app.sessions.before_decision
    async def restrict(intent_id, receipts, token, **kwargs):
        return await original(intent_id, receipts, token, restricted=True)
    monkeypatch.setattr(case.app.sessions, 'before_decision', restrict)
    def review(e, raw):
        if e.output_contract['stage'] != 'progress_review': return raw
        records.append(e)
        assert policy_for(e.request)['mode'] == 'restricted_repair'
        raw['progress']['classification'] = classification
        raw['decision'] = 'continue_current_intent' if classification == 'meaningful' else 'pause_current_intent'
        raw['constraints'] = []
        return raw
    case.models['review'].transform = review
    # Four queries reach the checkpoint. A known pause then prevents further work.
    if classification == 'meaningful':
        def stop_after_first(e, raw):
            value = review(e, raw)
            if e.output_contract['stage'] == 'progress_review' and len(records) > 1:
                value['progress']['classification'], value['decision'] = 'inconclusive', 'pause_current_intent'
            return value
        case.models['review'].transform = stop_after_first
    result = run_output(case)
    assert result.recovery_required and not case.sandbox.command_calls, result
    assert records and all(policy_for(e.request)['mode'] == 'restricted_repair' for e in records)


def test_repetitive_without_sibling_is_explicit_abandon(tmp_path, monkeypatch):
    def change(e, raw):
        p = policy_for(e.request)
        assert 'switch_sibling_intent' not in p['allowed_decisions']['repetitive']
        raw['progress']['classification'], raw['decision'] = 'repetitive', 'abandon_current_intent'
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['errors'] == [] and not result['recovery']['required'], result
    assert len(seen) == 1 and result['goal_evaluation'] == 'not_evaluated'
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert store.load_control_state_rows(result['task_run_id']).intents[0].status.value == 'abandoned'


@pytest.mark.parametrize('output_case', [13], indirect=True)
def test_replan_preserves_session_count_and_two_plan_limit(output_case):
    case = output_case
    policies, sessions = [], []
    def inner(e, raw):
        if e.output_contract['stage'] == 'inner_plan':
            sessions.append(e.request.session_id)
        return raw
    case.models['inner'].transform = inner
    def review(e, raw):
        if e.output_contract['stage'] == 'progress_review':
            p = policy_for(e.request)
            policies.append(p)
            if len(policies) == 1: raw['decision'] = 'replan_current_intent'
        return raw
    case.models['review'].transform = review
    result = run_output(case)
    assert result.stop_reason == 'search_exhausted', result
    assert len(sessions) == 2 and sessions[0] == sessions[1]
    ordinals = [r.ordinal for r in case.app.sessions.records()]
    assert ordinals == list(range(1, len(ordinals)+1)) and len(ordinals) >= 8
    assert 'replan_current_intent' in policies[0]['allowed_decisions']['meaningful']
    assert [p['decision_watermark'] for p in policies] == [4]
    from agent_core.review_policy import unavailable_decisions
    assert unavailable_decisions(case.app.state, case.app.state.turns[0].turn_id,
        case.app.state.intents[0].intent_id, case.app.config.max_turns)['replan_current_intent'] == 'd3_replan_not_available'


@pytest.mark.parametrize('output_case', [13], indirect=True)
def test_four_eight_twelve_windows_and_replan_unavailable_without_plan(output_case):
    case = output_case
    checks = []
    def query(e, raw):
        if e.output_contract['stage'] == 'inner_plan' and len([c for c in case.models['inner'].calls
                if c.output_contract['stage'] == 'inner_plan']) <= 8:
            return {'kind':'history_query','query':{}}
        return raw
    def review(e, raw):
        if e.output_contract['stage'] == 'progress_review':
            p = policy_for(e.request)
            checks.append(p)
            if len(checks) <= 2: assert 'replan_current_intent' not in p['allowed_decisions']['meaningful']
        return raw
    case.models['inner'].transform, case.models['review'].transform = query, review
    result = run_output(case)
    assert result.stop_reason == 'search_exhausted', result
    assert [p['decision_watermark'] for p in checks] == [4, 8, 12]
    assert len(case.app.store.list_d8_reads(case.task)) == 8


@pytest.mark.parametrize('output_case', [13], indirect=True)
def test_progress_latest_reference_must_be_available_before_dispatch(output_case, monkeypatch):
    case = output_case
    from agent_core.sessions import IntentSessions
    review_context = IntentSessions.review_context
    def unavailable(manager, *args, **kwargs):
        value = review_context(manager, *args, **kwargs)
        value['recent'][-1]['ref'] = 'unsupported-latest-ref'
        return value
    monkeypatch.setattr(IntentSessions, 'review_context', unavailable)
    result = run_output(case)
    assert result.recovery_required
    assert not any(e.output_contract['stage'] == 'progress_review' for e in case.models['review'].calls)


@pytest.mark.parametrize('twice', [False, True])
def test_missing_latest_reference_rejection_never_grants_unreviewed_continue(tmp_path, monkeypatch, twice):
    def change(e, raw):
        if twice or e.correction is None:
            raw['progress']['evidence_refs'] = [next(r for r in e.request.source_versions
                if r != policy_for(e.request)['required_latest_decision_ref'])]
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert len(seen) == 2
    if twice:
        assert result['recovery']['required'], result
        with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
            assert len(store.list_d8_decisions(result['task_run_id'])) == 4
            assert not store.list_d8_handoffs(result['task_run_id'])
    else: assert not result['recovery']['required'], result


def test_unknown_progress_call_is_not_reissued(tmp_path, monkeypatch):
    from agent_core.review_contracts import D2ModelFailure
    def change(e, raw): return D2ModelFailure('unknown', 'controlled_unknown')
    result, args, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['recovery']['required'] and len(seen) == 1, result
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    assert output.read('review-binding', seen[0].request.review_id) is None
    assert output.read('business-diagnostic', seen[0].request.review_id) is None
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('unknown progress called model'))
    args.command, args.task_run_id = 'resume', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']


def test_business_then_format_rejection_shares_one_correction(tmp_path, monkeypatch):
    def change(e, raw):
        if e.correction is None: raw['progress']['reason'] = ' '
        else: raw['constraints'] = [{'kind':'allowed_action_kinds','scope_kind':'intent','allowed_action_kinds':['fixed_probe']}]
        return raw
    result, _, seen, directory = run_progress(tmp_path, monkeypatch, change=change)
    assert result['recovery']['required'] and len(seen) == 2, result
    assert seen[1].correction['reason'] == 'output_business_invalid'
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert len(store.list_d8_decisions(result['task_run_id'])) == 4


def test_correction_capacity_gate_prevents_second_model_call(tmp_path, monkeypatch):
    from agent_adapters.handoff_evidence import FakeUnicodeCapacity
    from agent_core.output_contracts import D8OutputRequest
    fits = FakeUnicodeCapacity.fits
    def limited(port, req, policy):
        if isinstance(req, D8OutputRequest) and req.output_contract['stage'] == 'progress_review' and req.correction: return False
        return fits(port, req, policy)
    monkeypatch.setattr(FakeUnicodeCapacity, 'fits', limited)
    def change(e, raw): raw['progress']['reason'] = ' '; return raw
    result, _, seen, _ = run_progress(tmp_path, monkeypatch, change=change)
    assert result['recovery']['required'] and len(seen) == 1, result


def test_v8_actual_payload_is_stage_specific_and_capacity_checked_offline(tmp_path, monkeypatch):
    from agent_adapters.d8_model import build_payload, role_config, EstimatedCapacity, CAPACITY_POLICY
    result, _, seen, _ = run_progress(tmp_path, monkeypatch)
    assert not result['recovery']['required'], result
    outer, inner = role_config('outer_planning', version=8), role_config('inner_action', version=8)
    req = replace(seen[0].request)
    envelope = replace(seen[0], request=req)
    payload = build_payload(envelope, outer)
    assert 'progress_policy=' in payload['messages'][0]['content']
    assert 'never scope_id' in payload['messages'][0]['content']
    assert 'not an allowed execution category or alias' in payload['messages'][0]['content']
    assert payload['response_format'] == {'type':'json_object'}
    assert EstimatedCapacity(outer, inner).fits(envelope, CAPACITY_POLICY)
    with pytest.raises(ValueError, match='output_contract_mismatch'):
        build_payload(envelope, role_config('outer_planning', version=7))


def test_progress_worker_does_not_query_sqlite(tmp_path, monkeypatch):
    from agent_core.review_diagnostics import ReviewDiagnostics
    normalize = ReviewDiagnostics.normalize
    class NoSql:
        def __getattr__(self, name): pytest.fail('progress worker accessed SQLite')
    def guarded(manager, req, raw):
        if manager.progress(req):
            manager = ReviewDiagnostics(manager.config, manager.output, manager.ledger, NoSql(), manager.clock)
        return normalize(manager, req, raw)
    monkeypatch.setattr(ReviewDiagnostics, 'normalize', guarded)
    result, _, _, _ = run_progress(tmp_path, monkeypatch)
    assert result['errors'] == [] and not result['recovery']['required'], result


def test_completed_progress_result_loss_is_readonly_then_original_id_repair(tmp_path, monkeypatch):
    result, args, seen, directory = run_progress(tmp_path, monkeypatch)
    identity = seen[0].request.review_id
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    expected = output.read('review-result', identity)
    path = next(p for p in (directory / 'd8-output').rglob('*.json') if f'-review-result-{identity}' in p.name)
    path.unlink()
    before = (directory / 'control.sqlite').read_bytes()
    projection = snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))
    assert projection['errors'] == [] and projection['task_run_id'] == result['task_run_id'], projection
    assert projection['recovery']['required'] and not path.exists(), projection
    assert (directory / 'control.sqlite').read_bytes() == before
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('known result repair called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    assert cli.dispatch(args)['cleanup_status'] == 'confirmed'
    assert output.read('review-result', identity) == expected
