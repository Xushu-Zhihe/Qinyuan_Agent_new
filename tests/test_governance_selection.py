"""S5 observable choices, frozen sources and original-ID recovery."""
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters import task_cli
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.governance_policy import context, materialize
from agent_core.output_contracts import decode_response
from test_public_simulation import request
from test_output import output_case, run as run_output, reopen


def test_public_s5_summary_and_s4_inheritance(tmp_path, monkeypatch):
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v15'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    result = task_cli.dispatch_run_task(SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path/'state', events=None))
    assert result['errors'] == [] and not result['recovery']['required'], result
    assert result['contract_version'] == 15 and result['goal_evaluation'] == 'not_evaluated'
    assert len(result['verified_facts']) == 1 and result['answer_candidates'] == []
    directory = tmp_path/'state'/result['task_run_id']
    output = FileD8OutputEvidence(directory/'d8-output', create=False)
    records = [output.read('selection-binding', path.stem[-36:]) for path in (directory/'d8-output').glob('*selection-binding-*.json')]
    assert records and all(record['stage'] == 'handoff_summary' for record in records)
    assert all(output.read('selection-request', record['model_request_id']) is not None for record in records)


def modern(e, raw):
    if e.output_contract['stage'] == 'refinement' and isinstance(raw, dict):
        return {**raw, 'claims': [{key: value for key, value in claim.items()
            if key not in {'subject_ref', 'observation_scope'}} for claim in raw['claims']]}
    if e.output_contract['stage'] == 'change_review' and isinstance(raw, dict):
        ctx = context(asdict(e.request), 'change_review')
        return {key: value for key, value in raw.items() if key not in {'goal_supported', 'scope_explanation'} or ctx[key]}
    return raw


@pytest.fixture
def s5_case(output_case):
    assert output_case.app.config.contract_version == 15
    for model in output_case.models.values():
        model.transform = modern
    return output_case


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_s5_fake_native_chain(s5_case):
    result = run_output(s5_case)
    assert result.stop_reason == 'search_exhausted'
    assert len(s5_case.app.state.facts) == 1
    assert not s5_case.app.selection_bindings.check_task(s5_case.task)
    assert not s5_case.app.store.list_d4_unresolved_operations(s5_case.task)


def hold(case, *, two=False):
    from agent_core.modeling import ModelError, ModelErrorKind
    if two:
        case.models['handoff'].adapter.modify = lambda req, raw: {**raw, 'intents': raw['intents'][:1]} if req.stage == 'tree_planning' and raw['kind'] == 'candidates' else raw
    case.models['inner'].transform = lambda e, raw: ModelError(ModelErrorKind.INVALID_RESPONSE, 'known_test_stop') if (
        e.output_contract['stage'] == 'inner_complete' and len(case.app.state.facts) >= (2 if two else 1)) else modern(e, raw)
    run_output(case)
    actor = next(item.intent_id for item in case.app.state.intents if item.status.value == 'running')
    return actor


def change_wire(e, operation, *, targets=None, replacement_ref=None, resolution=None, relation_ref=None):
    ctx = context(asdict(e.request), e.output_contract['stage'])
    return {'kind': 'conclusion_change', 'operation': operation,
            'targets': targets or [next(iter(ctx['targets']))], 'replacement_ref': replacement_ref,
            'content': None, 'evidence_refs': [], 'reason': 'Review the exact selected statement version.',
            'resolution': resolution, 'relation_ref': relation_ref}


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_governance_withdraw_restore_uses_new_head_not_unchanged_content_ref(s5_case):
    c = s5_case
    actor = hold(c)
    c.models['fact'].transform = lambda e, raw: {'decision': 'accept', 'reason': 'Accept the scoped version change.'} if e.output_contract['stage'] == 'change_review' else modern(e, raw)
    selected = []
    def withdraw(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        wire = change_wire(e, 'withdraw')
        selected.append(wire['targets'][0])
        return wire
    c.models['inner'].transform = withdraw
    first = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert first.decision == 'accept'
    attempts = []
    def restore(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        ctx = context(asdict(e.request), 'conclusion_repair')
        token = next(iter(ctx['targets']))
        attempts.append(token)
        assert token != selected[0]
        return change_wire(e, 'restore', targets=[selected[0] if e.correction is None else token], replacement_ref=ctx['targets'][token]['ref'])
    c.models['inner'].transform = restore
    second = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert second.decision == 'accept' and len(attempts) == 2
    assert any(e.correction for e in c.models['inner'].calls if e.output_contract['stage'] == 'conclusion_repair')
    assert any(item.current_status == 'effective' for item in c.app.revisions._history() if item.record_type == 'verified_fact')


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_relation_selects_parent_owner_and_requires_goal_support(s5_case):
    c = s5_case
    actor = hold(c, two=True)
    assert len(c.app.state.facts) == 2
    chosen = []
    def propose(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        ctx = context(asdict(e.request), 'conclusion_repair')
        token, relation = next((token, relation) for token, relation in ctx['relations'].items() if relation['relation_kind'] == 'parent_support')
        chosen.append(relation)
        assert relation['relation_owner_id'] != actor
        replacement = next(ref for ref in ctx['replacement_refs'] if ref != relation['old_ref'])
        return change_wire(e, 'replace_support', targets=[relation['target']], relation_ref=token, replacement_ref=replacement)
    c.models['inner'].transform = propose
    c.models['fact'].transform = lambda e, raw: {'decision': 'accept', 'reason': 'The replacement supports the same original local goal.', 'goal_supported': True} if e.output_contract['stage'] == 'change_review' else modern(e, raw)
    result = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert result.decision == 'accept'
    saved = c.app.revisions._payload(result)[1]
    assert saved.relation_owner_id == chosen[0]['relation_owner_id'] and saved.relation_owner_id != actor
    assert c.app.revisions._payload(result)[0]['relation_old_ref'] == chosen[0]['old_ref']


@pytest.mark.parametrize('output_case', [15], indirect=True)
@pytest.mark.parametrize('stage', ['conclusion_repair', 'change_review', 'handoff_summary', 'refinement'])
def test_known_materialization_survives_loss_before_native_result(s5_case, monkeypatch, stage):
    from test_chain_multi_turn import ProcessLoss
    c = s5_case
    if stage in {'conclusion_repair', 'change_review'}:
        actor = hold(c)
        c.models['inner'].transform = lambda e, raw: change_wire(e, 'withdraw') if e.output_contract['stage'] == 'conclusion_repair' else modern(e, raw)
        c.models['fact'].transform = lambda e, raw: {'decision': 'reject', 'reason': 'Keep the saved statement.'} if e.output_contract['stage'] == 'change_review' else modern(e, raw)
    elif stage == 'refinement':
        c.models['inner'].transform = lambda e, raw: {**raw, 'review_requested': True} if e.output_contract['stage'] == 'inner_size' else modern(e, raw)
    original = c.app.output_boundary.evidence.save
    def fail_after(kind, identity, value):
        result = original(kind, identity, value)
        if kind == 'selection-binding' and value['stage'] == stage:
            raise ProcessLoss()
        return result
    monkeypatch.setattr(c.app.output_boundary.evidence, 'save', fail_after)
    identity = str(uuid4())
    if stage in {'conclusion_repair', 'change_review'}:
        with pytest.raises(ProcessLoss):
            asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
    elif stage == 'refinement':
        with pytest.raises(ProcessLoss):
            run_output(c)
    else:
        run_output(c)
    records = [p for p in (c.directory/'d8-output').glob('*selection-binding-*.json')
               if c.app.output_boundary.evidence.read('selection-binding', p.stem[-36:])['stage'] == stage]
    assert records, stage
    before_files = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in c.directory.rglob('*.json')}
    assert c.app.selection_bindings.check_task(c.task)
    assert before_files == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in c.directory.rglob('*.json')}
    counts = {name: len(model.calls) for name, model in c.models.items()}
    reopen(c)
    if stage in {'conclusion_repair', 'change_review'}:
        result = asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
        assert result.decision == 'reject'
    else:
        run_output(c)
    for name, model in c.models.items():
        prior_ids = [e.request.model_request_id if hasattr(e.request, 'model_request_id') else e.request.attempt.model_request_id
                     for e in model.calls[:counts[name]] if not hasattr(e.request, 'review_id')]
        new_ids = [e.request.model_request_id if hasattr(e.request, 'model_request_id') else e.request.attempt.model_request_id
                   for e in model.calls[counts[name]:] if not hasattr(e.request, 'review_id')]
        assert not set(prior_ids) & set(new_ids)


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_history_null_query_and_known_original_snapshot(s5_case):
    c = s5_case
    actor = hold(c)
    c.models['inner'].transform = lambda e, raw: {'kind': 'history_query', 'query': {'intent_id': None, 'record_type': None}} if e.output_contract['stage'] == 'conclusion_repair' else modern(e, raw)
    identity = str(uuid4())
    result = asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
    assert result['historical_snapshot'] and result['items']
    binding = c.app.selection_bindings.read(identity)
    assert binding['wire_response']['query'] == {'intent_id': None, 'record_type': None}
    assert binding['canonical_response']['query'] == {}
    before = len(c.models['inner'].calls)
    reopened = c.app.handoff.evidence.read('read-result', result['request_id'])
    assert reopened is not None
    reopen(c)
    assert asyncio.run(c.app.repair_conclusions(actor, identity, c.token)) == result
    assert len(c.models['inner'].calls) == before


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_conflict_target_order_is_preserved_through_review_and_adjudication(s5_case):
    c = s5_case
    actor = hold(c, two=True)
    roots = []
    def propose(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        ctx = context(asdict(e.request), 'conclusion_repair')
        tokens = sorted(ctx['targets'], reverse=True)
        roots[:] = [ctx['targets'][token]['root_id'] for token in tokens]
        return change_wire(e, 'report_conflict', targets=tokens)
    c.models['inner'].transform = propose
    c.models['fact'].transform = lambda e, raw: {'decision': 'accept', 'reason': 'Accept the explicit ordered conflict report.'} if e.output_contract['stage'] == 'change_review' else modern(e, raw)
    record = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert [target.root_id for target in c.app.revisions._payload(record)[1].targets] == roots
    def resolve(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        ctx = context(asdict(e.request), 'conclusion_repair')
        tokens = [next(token for token, target in ctx['targets'].items() if target['root_id'] == root) for root in roots]
        return change_wire(e, 'resolve_conflict', targets=tokens, resolution='keep_first')
    c.models['inner'].transform = resolve
    result = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert result.decision == 'accept'
    _, heads, conflicts, _ = c.app.revisions._projection(c.app.revisions._base())
    assert heads[roots[0]]['ref'] is not None and heads[roots[1]]['ref'] is None and not conflicts


@pytest.mark.parametrize('output_case', [15], indirect=True)
@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'request_missing'])
def test_known_binding_damage_pauses_without_another_model_call(s5_case, damage):
    c = s5_case
    run_output(c)
    paths = list((c.directory/'d8-output').glob('*selection-binding-*.json'))
    assert paths
    path = paths[0]
    identity = path.stem[-36:]
    before = {name: len(model.calls) for name, model in c.models.items()}
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_text('{}', encoding='utf-8')
    else:
        next((c.directory/'d8-output').glob('*selection-request-'+identity+'.json')).unlink()
    with pytest.raises(D3RecoveryRequired):
        c.app.selection_bindings.check_task(c.task)
    assert before == {name: len(model.calls) for name, model in c.models.items()}


def refinement_request(*, mixed=False):
    from agent_core.process_facts import D5Source, D5Attempt, D5RefinementRequest
    artifact, other = str(uuid4()), str(uuid4())
    sources = (D5Source('execution', str(uuid4()), '0'*64, artifact, 'saved_copy', artifact),
               D5Source('observation_proof', str(uuid4()), '1'*64, other if mixed else artifact,
                        'another_scope' if mixed else 'saved_copy', other if mixed else artifact))
    attempt = D5Attempt(str(uuid4()), str(uuid4()), str(uuid4()), 1, 1., 'sha256:'+'2'*64, sources, '3'*64)
    return D5RefinementRequest(attempt, tuple({'source': asdict(source), 'content': {}} for source in sources))


@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('claims_kind', ['one', 'none', 'duplicate', 'foreign', 'generated_identity'])
def test_refinement_uses_explicit_homogeneous_sources_without_identity_generation(mixed, claims_kind):
    from agent_core.output_contracts import output_schema
    request = refinement_request(mixed=mixed)
    ids = [source.object_id for source in request.attempt.sources]
    claim = {'claim_key': 'observation', 'claim_value': 'Evidence supports this scoped observation only.', 'source_refs': ids}
    if claims_kind == 'duplicate': claim['source_refs'] = [ids[0], ids[0]]
    if claims_kind == 'foreign': claim['source_refs'] = [str(uuid4())]
    if claims_kind == 'generated_identity': claim['subject_ref'] = request.attempt.sources[0].subject_ref
    wire = {'summary_text': 'A concise process summary, not a verified Fact.', 'claims': [] if claims_kind == 'none' else [claim]}
    _, issues, _, _ = decode_response(wire, output_schema(request, contract_version=15)[1])
    valid_shape = claims_kind in {'none', 'one'}
    assert bool(issues) != valid_shape
    if valid_shape and claims_kind == 'one' and mixed:
        from agent_core.governance_policy import MixedClaimSources
        with pytest.raises(MixedClaimSources):
            materialize(context(asdict(request), 'refinement'), 'refinement', wire)
    elif valid_shape:
        canonical = materialize(context(asdict(request), 'refinement'), 'refinement', wire)
        if canonical['claims']:
            assert canonical['claims'][0]['subject_ref'] == request.attempt.sources[0].subject_ref
            assert canonical['claims'][0]['observation_scope'] == 'saved_copy'
            assert canonical['claims'][0]['source_refs'] == ids
            assert canonical['claims'][0]['claim_value'] == claim['claim_value']


@pytest.mark.parametrize('goal,scope', [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize('decision', ['accept', 'reject', 'insufficient'])
def test_change_review_only_outputs_applicable_assessments(goal, scope, decision):
    from agent_core.governance_policy import review_schema
    ctx = {'goal_supported': goal, 'scope_explanation': scope}
    wire = {'decision': decision, 'reason': 'A bound semantic assessment.'}
    if goal: wire['goal_supported'] = True if decision == 'accept' else None
    if scope: wire['scope_explanation'] = 'Distinct observation scopes.' if decision == 'accept' else None
    assert not decode_response(wire, review_schema(ctx))[1]
    canonical = materialize(ctx, 'change_review', wire)
    assert canonical['goal_supported'] == wire.get('goal_supported')
    assert canonical['scope_explanation'] == wire.get('scope_explanation')
    if not goal:
        assert decode_response({**wire, 'goal_supported': None}, review_schema(ctx))[1]
    if not scope:
        assert decode_response({**wire, 'scope_explanation': None}, review_schema(ctx))[1]


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_summary_keeps_partial_order_and_empty_question_references(s5_case):
    c = s5_case
    expected = []
    def modify(e, raw):
        if e.output_contract['stage'] != 'handoff_summary': return modern(e, raw)
        refs = [item.ref for item in reversed(e.request.history)][:2]
        expected.append(refs)
        return {**raw, 'turn_body': 'Different concise wording is valid.', 'ordered_refs': refs,
                'open_questions': [{'text': 'Is another independent direction justified?', 'refs': []}]}
    c.models['handoff'].transform = modify
    assert run_output(c).stop_reason == 'search_exhausted'
    for record, refs in zip(c.app.store.list_d8_handoffs(c.task), expected, strict=True):
        saved = c.app.handoff.evidence.read('handoff', record.evidence_ref)
        assert saved['ordered_refs'] == refs
        assert saved['open_questions'][0]['refs'] == []


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_governance_unknown_does_not_redispatch_on_reopen(s5_case):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_core.operation_accounting import D4RecoveryRequired
    c = s5_case
    actor = hold(c)
    c.models['inner'].transform = lambda e, raw: ModelError(ModelErrorKind.TIMED_OUT, 'known_test_timeout') if e.output_contract['stage'] == 'conclusion_repair' else modern(e, raw)
    identity = str(uuid4())
    with pytest.raises((D3RecoveryRequired, D4RecoveryRequired), match='outcome_unknown'):
        asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
    assert c.app.revisions.evidence.read('result', identity) is None
    before = len(c.models['inner'].calls)
    reopen(c)
    with pytest.raises((D3RecoveryRequired, D4RecoveryRequired), match='outcome_unknown'):
        asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
    assert len(c.models['inner'].calls) == before


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_read_after_durable_start_recovers_original_page_without_model(s5_case, monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    c = s5_case
    actor = hold(c)
    c.models['inner'].transform = lambda e, raw: {'kind': 'history_query', 'query': {'turn_id': None}} if e.output_contract['stage'] == 'conclusion_repair' else modern(e, raw)
    monkeypatch.setattr(c.app.handoff, '_page', lambda *args: (_ for _ in ()).throw(ProcessLoss()))
    identity = str(uuid4())
    with pytest.raises(ProcessLoss):
        asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
    pending = [(start, completion) for _, start, completion in c.app.store.list_d8_reads(c.task) if completion is None]
    assert len(pending) == 1
    before = len(c.models['inner'].calls)
    snapshot = c.app.handoff.evidence.read('read-snapshot', pending[0][0]['snapshot_id'])
    reopen(c)
    c.app.handoff.resume_read(pending[0][0]['request_id'])
    result = asyncio.run(c.app.repair_conclusions(actor, identity, c.token))
    assert result['snapshot_id'] == snapshot['snapshot_id']
    assert result['checkpoint_sequence'] == snapshot['checkpoint_sequence']
    assert len(c.models['inner'].calls) == before


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_visible_cursor_is_explicit_and_foreign_cursor_gets_only_one_correction(s5_case):
    c = s5_case
    actor = hold(c, two=True)
    c.models['inner'].transform = lambda e, raw: {'kind': 'history_query', 'query': {}} if e.output_contract['stage'] == 'conclusion_repair' else modern(e, raw)
    first = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert first['has_more'] and first['next_cursor'] is not None
    attempts = []
    def next_page(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        ctx = context(asdict(e.request), 'conclusion_repair')
        assert first['next_cursor'] in ctx['cursors']
        attempts.append(e.request.model_request_id)
        return {'kind': 'history_next_page', 'cursor': str(uuid4()) if e.correction is None else first['next_cursor']}
    c.models['inner'].transform = next_page
    second = asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert len(attempts) == 2 and first['snapshot_id'] == second['snapshot_id']
    assert not {item['ref'] for item in first['items']} & {item['ref'] for item in second['items']}


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_model_workers_do_not_query_sqlite_for_selection_materialization(s5_case, monkeypatch):
    import threading
    c = s5_case
    main = threading.get_ident()
    for name in ('load_d4_operation', 'load_d3_operation', 'list_d5_attempts'):
        original = getattr(c.app.store, name)
        def guard(*args, _original=original, **kwargs):
            assert threading.get_ident() == main, 'model worker must use frozen files only'
            return _original(*args, **kwargs)
        monkeypatch.setattr(c.app.store, name, guard)
    assert run_output(c).stop_reason == 'search_exhausted'


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_real_v10_payload_uses_reduced_refinement_and_current_schema_offline(s5_case):
    from agent_adapters.deepseek_roles import build_payload, role_config, role_contract_version
    from agent_core.output_contracts import D8OutputRequest, output_schema
    request = refinement_request()
    role = role_config('outer_planning', version=10)
    request = replace(request, attempt=replace(request.attempt, role_config_ref=role.role_config_ref))
    stage, schema = output_schema(request, contract_version=15)
    payload = build_payload(D8OutputRequest(request, {'version': 1, 'stage': stage, 'schema': schema}, 'Follow frozen schema.', None), role)
    body = json.loads(payload['messages'][1]['content'])
    assert role_contract_version(role) == 15 and 's5_selection_context' in body
    claim_properties = schema['properties']['claims']['items']['properties']
    assert 'subject_ref' not in claim_properties and 'observation_scope' not in claim_properties
    assert payload['max_tokens'] == 32768 and payload['reasoning_effort'] == 'max'


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_sensitive_governance_is_not_bound_or_sent_for_review(s5_case):
    c = s5_case
    actor = hold(c)
    marker = 'password=PRIVATE_S5_SECRET'
    def propose(e, raw):
        if e.output_contract['stage'] != 'conclusion_repair': return modern(e, raw)
        return {**change_wire(e, 'withdraw'), 'reason': marker}
    c.models['inner'].transform = propose
    before = len(c.models['fact'].calls)
    with pytest.raises(D3RecoveryRequired):
        asyncio.run(c.app.repair_conclusions(actor, str(uuid4()), c.token))
    assert len(c.models['fact'].calls) == before
    assert all(marker not in path.read_text(encoding='utf-8') for path in c.directory.rglob('*.json'))


@pytest.mark.parametrize('mixed', [False, True])
def test_source_coherence_failure_is_business_rejection_without_format_diagnostic(mixed):
    from agent_core.structured_output import StructuredOutput
    from agent_core.output_contracts import output_schema
    request = refinement_request(mixed=mixed)
    wire = {'summary_text': 'Scoped progress.', 'claims': [{'claim_key': 'x', 'claim_value': 'y',
        'source_refs': [s.object_id for s in request.attempt.sources]}]}
    evidence = SimpleNamespace(save=lambda *args: pytest.fail('business rejection must not create format diagnostics'))
    def normalize(req, value, clock):
        return materialize(context(asdict(req), 'refinement'), 'refinement', value)
    app = SimpleNamespace(config=SimpleNamespace(contract_version=15),
        conclusions=SimpleNamespace(_sensitive=lambda value: False),
        selection_bindings=SimpleNamespace(normalize=normalize), clock=lambda: 1)
    boundary = StructuredOutput(app, evidence)
    boundary.envelope = lambda req: SimpleNamespace(output_contract={'stage': 'refinement', 'schema': output_schema(req, contract_version=15)[1]})
    result = boundary.normalize(request, wire)
    assert (result is None) == mixed


@pytest.mark.parametrize('output_case', [15], indirect=True)
def test_d5_unknown_sealing_keeps_no_retry_and_no_verified_identity(s5_case):
    from agent_core.modeling import ModelError, ModelErrorKind
    c = s5_case
    c.models['inner'].transform = lambda e, raw: {**raw, 'review_requested': True} if e.output_contract['stage'] == 'inner_size' else modern(e, raw)
    c.models['refinement'].transform = lambda e, raw: ModelError(ModelErrorKind.TIMED_OUT, 'known_test_unknown')
    assert run_output(c).stop_reason == 'search_exhausted'
    calls = list(c.models['refinement'].calls)
    assert calls and all(e.correction is None for e in calls)
    for e in calls:
        identity = e.request.attempt.model_request_id
        assert c.app.store.is_unknown_refinement_sealed(identity)
        assert c.app.refinement.evidence.read_result(identity)['outcome'] == 'response_unknown'
        assert c.app.refinement.evidence.read_candidate(identity) is None
    assert not c.app.selection_bindings.check_task(c.task)
    assert not c.app.store.list_process_facts(c.task)
    count = len(c.models['refinement'].calls)
    reopen(c)
    assert run_output(c).stop_reason == 'search_exhausted'
    assert len(c.models['refinement'].calls) == count


@pytest.mark.parametrize('bad', ['summary_empty', 'claim_key_empty', 'claim_value_empty', 'claim_value_nul'])
def test_d5_native_text_business_rejection_never_becomes_format_retry(bad):
    from agent_core.structured_output import StructuredOutput
    from agent_core.output_contracts import output_schema
    from agent_core.selection_bindings import validate_value
    request = refinement_request()
    wire = {'summary_text': 'Scoped progress.', 'claims': [{'claim_key': 'x', 'claim_value': 'y',
        'source_refs': [request.attempt.sources[0].object_id]}]}
    if bad == 'summary_empty': wire['summary_text'] = ''
    if bad == 'claim_key_empty': wire['claims'][0]['claim_key'] = ''
    if bad == 'claim_value_empty': wire['claims'][0]['claim_value'] = ''
    if bad == 'claim_value_nul': wire['claims'][0]['claim_value'] = 'x\x00y'
    def normalize(req, raw, clock):
        canonical = materialize(context(asdict(req), 'refinement'), 'refinement', raw)
        validate_value(asdict(req), 'refinement', canonical)
        return canonical
    app = SimpleNamespace(config=SimpleNamespace(contract_version=15),
        conclusions=SimpleNamespace(_sensitive=lambda value: False),
        selection_bindings=SimpleNamespace(normalize=normalize), clock=lambda: 1)
    boundary = StructuredOutput(app, SimpleNamespace(save=lambda *args: pytest.fail('business failure cannot gain format correction')))
    boundary.envelope = lambda req: SimpleNamespace(output_contract={'stage': 'refinement', 'schema': output_schema(req, contract_version=15)[1]})
    assert boundary.normalize(request, wire) is None
