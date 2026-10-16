"""D9-4 candidate checkpoints and trusted feedback over the real D8 core."""

import asyncio
import json
from dataclasses import asdict
from uuid import uuid4

import pytest

from agent_core.goal_feedback import ExternalValidationFeedback
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_adapters.v7 import candidate_evidence
from test_d7_candidate_evidence import _TestOnlyProtection
from test_d8_output import output_case, EnvelopeModel, run as run_output


@pytest.fixture
def candidate_model(monkeypatch):
    monkeypatch.setattr(candidate_evidence, 'WindowsEvidenceProtection', _TestOnlyProtection)
    original = EnvelopeModel.invoke
    state = {'calls': [], 'value': None}

    def invoke(model, envelope, *args):
        if envelope.output_contract['stage'] == 'inner_candidate':
            state['calls'].append(envelope)
            value = state['value']
            if callable(value):
                return value(envelope)
            return {'action': {'kind': 'no_candidate'}} if value is None else {
                'action': {'kind': 'candidate_flag', 'value': value,
                           'execution_id': envelope.request.context.execution_id}}
        return original(model, envelope, *args)

    monkeypatch.setattr(EnvelopeModel, 'invoke', invoke)
    return state


def feedback(case, candidate, verdict):
    return ExternalValidationFeedback(case.task, candidate.candidate_id,
        candidate.evidence_version, verdict, 'test-only-trusted-judge', str(uuid4()))


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_no_candidate_is_saved_without_body_or_repeat(output_case, candidate_model):
    case = output_case
    result = run_output(case)
    assert result.stop_reason == 'search_exhausted', result
    assert len(candidate_model['calls']) == 1
    assert case.app.store.list_d7_answer_candidates(case.task) == ()
    intent = next(i for i in case.app.state.intents if i.status.value == 'succeeded')
    assert not asyncio.run(case.app.answers.after_success(intent, case.token))
    assert len(candidate_model['calls']) == 1
    assert len(case.app.state.facts) == 1


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_candidate_waits_before_child_and_keeps_text_private(output_case, candidate_model):
    case = output_case
    candidate_model['value'] = '  PRIVATE_TEST_ANSWER\n'
    result = run_output(case)
    assert result.stop_reason == 'd9_awaiting_external_feedback', result
    assert case.app.state.task_run.status.value == 'awaiting_external_feedback'
    assert len(case.app.state.turns) == 1
    assert case.app.state.turns[0].status.value == 'running'
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    assert candidate.intent_id == next(i.intent_id for i in case.app.state.intents if i.status.value == 'succeeded')
    for folder in ('d8-output', 'd8-sessions', 'd3-models'):
        for p in (case.directory / folder).rglob('*.json'):
            assert 'PRIVATE_TEST_ANSWER' not in p.read_text(encoding='utf-8')
    assert asyncio.run(case.app.answers.after_success(case.app.state.intents[0], case.token))
    assert len(candidate_model['calls']) == 1
    service = candidate_evidence.D7CandidateEvidenceService(case.directory, case.task,
        trusted_adapter_ids=('test-only-byte-check',))
    assert service.read_port('test-only-byte-check').read(candidate.candidate_id) == '  PRIVATE_TEST_ANSWER\n'


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_rejection_keeps_local_success_and_enters_outer_child_planning(output_case, candidate_model):
    case = output_case
    candidate_model['value'] = 'PRIVATE_REJECTED_ANSWER'
    assert run_output(case).stop_reason == 'd9_awaiting_external_feedback'
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    record = feedback(case, candidate, 'rejected')
    result = asyncio.run(case.app.answers.apply_feedback(record, case.token))
    assert result.stop_reason == 'search_exhausted', result
    assert case.app.state.intents[0].status.value == 'succeeded'
    assert len(case.app.state.facts) == 1
    child = [e for e in case.models['handoff'].calls
             if e.output_contract['stage'] == 'tree_planning' and e.request.intent_id == candidate.intent_id]
    assert child
    assert any('answer_feedback' in i.content and 'rejected' in i.content for i in child[-1].request.history)
    assert 'PRIVATE_REJECTED_ANSWER' not in json.dumps(asdict(child[-1]), ensure_ascii=False)
    before = len(candidate_model['calls'])
    asyncio.run(case.app.answers.apply_feedback(record, case.token))
    assert len(candidate_model['calls']) == before


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_unknown_does_not_continue_and_accepted_is_independent(output_case, candidate_model):
    case = output_case
    candidate_model['value'] = 'PRIVATE_PENDING_ANSWER'
    assert run_output(case).stop_reason == 'd9_awaiting_external_feedback'
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    before = len(candidate_model['calls'])
    asyncio.run(case.app.answers.apply_feedback(feedback(case, candidate, 'unknown'), case.token))
    assert case.app.state.task_run.status.value == 'awaiting_external_feedback'
    assert len(candidate_model['calls']) == before
    # Reconciliation of an unknown submission is covered by the trusted check ledger.
    assert len(case.app.state.turns) == 1


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_trusted_acceptance_is_required_for_achieved(output_case, candidate_model):
    case = output_case
    candidate_model['value'] = 'PRIVATE_ACCEPTED_ANSWER'
    assert run_output(case).stop_reason == 'd9_awaiting_external_feedback'
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    asyncio.run(case.app.answers.apply_feedback(feedback(case, candidate, 'accepted'), case.token))
    assert case.app.state.task_run.status.value == 'achieved'
    assert len(candidate_model['calls']) == 1


def test_public_new19_no_candidate_has_readonly_safe_projection(tmp_path, monkeypatch):
    from test_d9_public import request, start
    from agent_adapters.run_files import immutable_read
    from agent_adapters.v7.task_result import snapshot_v3_task_result
    monkeypatch.setattr(candidate_evidence, 'WindowsEvidenceProtection', _TestOnlyProtection)
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v19'
    result, args = start(tmp_path, monkeypatch, raw)
    assert result['errors'] == [] and not result['recovery']['required'], result
    assert result['contract_version'] == 19 and result['goal_evaluation'] == 'not_evaluated'
    directory = args.state_dir / result['task_run_id']
    declaration = immutable_read(directory / 'run.json')
    assert snapshot_v3_task_result(directory, declaration)['answer_candidates'] == []
    from agent_core.sqlite_control_store import SqliteControlStore
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert len([o for o in store.list_d3_operations(result['task_run_id'])
                    if o.start.resource_ref.startswith('answer-candidate/')]) == 1


@pytest.mark.parametrize('output_case', [19], indirect=True)
@pytest.mark.parametrize('point', ['protected_result', 'registered', 'no_candidate'])
def test_known_candidate_interruption_recovers_without_model(output_case, candidate_model, monkeypatch, point):
    from test_d3_chain import ProcessLoss
    case = output_case
    candidate_model['value'] = None if point == 'no_candidate' else 'PRIVATE_RECOVERY_ANSWER'
    if point == 'protected_result':
        original = candidate_evidence.D7CandidateEvidenceService.save_pending
        def interrupt(protected, *args):
            original(protected, *args)
            raise ProcessLoss()
        monkeypatch.setattr(candidate_evidence.D7CandidateEvidenceService, 'save_pending', interrupt)
    elif point == 'registered':
        original = case.app.controller.register_answer_candidate
        def interrupt(*args):
            original(*args)
            raise ProcessLoss()
        monkeypatch.setattr(case.app.controller, 'register_answer_candidate', interrupt)
    else:
        original = case.app.model_evidence.save
        def interrupt(value):
            saved = original(value)
            if value.stage == 'inner_candidate':
                raise ProcessLoss()
            return saved
        monkeypatch.setattr(case.app.model_evidence, 'save', interrupt)
    with pytest.raises(ProcessLoss):
        run_output(case)
    if point == 'protected_result':
        monkeypatch.setattr(candidate_evidence.D7CandidateEvidenceService, 'save_pending', original)
    before = len(candidate_model['calls'])
    case.app.store.close()
    case.app = case.create()
    result = asyncio.run(case.app.resume(case.task, case.token))
    assert result.stop_reason == ('search_exhausted' if point == 'no_candidate' else 'd9_awaiting_external_feedback'), result
    assert len(candidate_model['calls']) == before
    assert all(o.application_ref is not None for o in case.app.store.list_d3_operations(case.task)
               if o.start.resource_ref.startswith('answer-candidate/'))


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_unknown_candidate_never_reasks(output_case, candidate_model):
    case = output_case
    def unavailable(envelope):
        raise RuntimeError('PRIVATE_MODEL_ERROR_DO_NOT_SAVE')
    candidate_model['value'] = unavailable
    assert run_output(case).recovery_required
    before = len(candidate_model['calls'])
    case.app.store.close()
    case.app = case.create()
    result = asyncio.run(case.app.resume(case.task, case.token))
    assert result.recovery_required
    assert len(candidate_model['calls']) == before


@pytest.fixture
def public_candidate(tmp_path, monkeypatch):
    import time
    from agent_adapters.d8_simulation import SimulationModel, parse_request
    from agent_adapters.multi_turn_application import D3Application
    from agent_adapters.handoff_evidence import FakeUnicodeCapacity
    from agent_adapters.v7.simulated_application import _LocalDigestSandbox, _Token
    from agent_adapters.run_files import immutable_read
    from test_d9_public import request
    from agent_adapters import task_cli
    from types import SimpleNamespace
    original = SimulationModel.invoke
    calls = []
    def invoke(model, envelope, *args):
        calls.append(envelope.output_contract['stage'])
        if envelope.output_contract['stage'] == 'inner_candidate':
            return {'action': {'kind': 'candidate_flag', 'value': 'PRIVATE_PUBLIC_CANDIDATE',
                               'execution_id': envelope.request.context.execution_id}}
        return original(model, envelope, *args)
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    monkeypatch.setattr(candidate_evidence, 'WindowsEvidenceProtection', _TestOnlyProtection)
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v19'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    args = SimpleNamespace(request='unused', mode='simulated',
        state_dir=tmp_path / 'workbench-run' / 'agent-state', events=None)
    result = task_cli.dispatch_run_task(args)
    assert result['task_run']['status'] == 'awaiting_external_feedback', result
    assert not result['recovery']['required'], result
    directory = args.state_dir / result['task_run_id']
    declaration = immutable_read(directory / 'run.json')
    _, config = parse_request(raw)
    sandbox = _LocalDigestSandbox(directory)
    outer, inner = SimulationModel(config.outer_role), SimulationModel(config.inner_role)
    app = D3Application(directory, config=config, sandbox=sandbox, import_sandbox=sandbox,
        workspace_sandbox=sandbox, command_sandbox=sandbox, outer_model=outer, inner_model=inner,
        review_model=outer, refinement_model=outer, fact_review_model=outer, handoff_model=outer,
        capacity_port=FakeUnicodeCapacity(), sensitive_config={}, clock=time.time)
    app.controller.attach(result['task_run_id'])
    app._bound_task_run_id = result['task_run_id']
    case = SimpleNamespace(app=app, task=result['task_run_id'], directory=directory,
        declaration=declaration, token=_Token(directory), calls=calls)
    yield case
    app.store.close()


def selection(case, candidate, submission):
    return dict(candidate_id=candidate.candidate_id, evidence_version=candidate.evidence_version,
                submission_operation_id=submission.operation_id, query_id=str(uuid4()))


def test_unconfigured_public_check_does_not_write_or_call_model(public_candidate):
    from agent_adapters.validation_check import dispatch_check
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    before = (case.app.state.latest_checkpoint.sequence, len(case.calls))
    picked = selection(case, candidate, submitted)
    result = dispatch_check(case.directory, case.declaration, picked)
    assert result['exit_code'] == 2
    assert result['errors'][-1]['code'] == 'd9_validation_adapter_unconfigured'
    assert case.app.store.load_d7_external_validation_check(picked['query_id']) is None
    assert (case.app.state.latest_checkpoint.sequence, len(case.calls)) == before
    assert 'PRIVATE_PUBLIC_CANDIDATE' not in json.dumps(result)
    assert result['answer_candidates'][0]['checkable_submission_ids'] == [submitted.operation_id]


@pytest.mark.parametrize('verdict', ['accepted', 'rejected', 'unknown'])
def test_public_trusted_check_is_durable_and_never_runs_model(public_candidate, monkeypatch, verdict):
    from agent_adapters import validation_check
    from agent_core.goal_feedback import ExternalValidationCheckResult
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    picked = selection(case, candidate, submitted)
    class Checker:
        calls = 0
        def check(self, request):
            self.calls += 1
            return ExternalValidationCheckResult(request, verdict, 'test-only-readonly-check')
    checker = Checker()
    monkeypatch.setattr(validation_check, 'configured_checker', lambda _: checker)
    before = len(case.calls)
    result = validation_check.dispatch_check(case.directory, case.declaration, picked)
    assert result['task_run']['status'] == {'accepted': 'achieved', 'rejected': 'running',
                                           'unknown': 'awaiting_external_feedback'}[verdict], result
    assert len(case.calls) == before and checker.calls == 1
    assert case.app.store.load_d7_external_validation_check(picked['query_id']).result is not None
    monkeypatch.setattr(validation_check, 'configured_checker', lambda _: pytest.fail('cached query loaded adapter'))
    replay = validation_check.dispatch_check(case.directory, case.declaration, picked)
    assert replay['validation_check']['state'] == verdict
    assert checker.calls == 1 and len(case.calls) == before
    assert 'PRIVATE_PUBLIC_CANDIDATE' not in json.dumps(replay)


@pytest.mark.parametrize('bad', ['verdict', 'endpoint', 'candidate', 'version', 'submission'])
def test_public_check_rejects_untrusted_fields_and_wrong_bindings(public_candidate, bad):
    from agent_adapters.validation_check import dispatch_check
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    picked = selection(case, candidate, submitted)
    if bad == 'verdict': picked['verdict'] = 'accepted'
    elif bad == 'endpoint': picked['endpoint'] = 'https://untrusted.invalid/'
    elif bad == 'candidate': picked['candidate_id'] = str(uuid4())
    elif bad == 'version': picked['evidence_version'] = '0' * 64
    else: picked['submission_operation_id'] = str(uuid4())
    with pytest.raises((ValueError, D3RecoveryRequired)):
        dispatch_check(case.directory, case.declaration, picked)
    assert case.app.store.list_d7_external_validation_checks(case.task) == ()


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_fake_judge_rejects_then_child_candidate_is_accepted(output_case, candidate_model):
    from d7_fake_judge_fixture import FakeJudge
    case = output_case
    candidate_model['value'] = lambda e: {'action': {'kind': 'candidate_flag',
        'value': 'correct' if len(candidate_model['calls']) > 1 else 'incorrect',
        'execution_id': e.request.context.execution_id}}
    def planning(request, raw):
        if request.stage == 'tree_planning' and raw['kind'] == 'candidates':
            return {**raw, 'intents': raw['intents'][:1]}
        return raw
    case.models['handoff'].adapter.modify = planning
    assert run_output(case).stop_reason == 'd9_awaiting_external_feedback'
    first, = case.app.store.list_d7_answer_candidates(case.task)
    service = candidate_evidence.D7CandidateEvidenceService(case.directory, case.task,
        trusted_adapter_ids=('test-only-fake-judge',))
    port = service.read_port('test-only-fake-judge')
    judge = FakeJudge()
    rejected = judge.evaluate(first, lambda c: port.read(c.candidate_id))
    assert rejected.verdict == 'rejected'
    resumed = asyncio.run(case.app.answers.apply_feedback(rejected, case.token))
    assert resumed.stop_reason == 'd9_awaiting_external_feedback', resumed
    candidates = case.app.store.list_d7_answer_candidates(case.task)
    assert len(candidates) == 2
    second = next(c for c in candidates if c.candidate_id != first.candidate_id)
    assert len(case.app.state.turns) == 2
    child = next(t for t in case.app.state.turns if t.turn_id == second.turn_id)
    assert child.parent_turn_id == first.turn_id and child.triggering_intent_id == first.intent_id
    accepted = judge.evaluate(second, lambda c: port.read(c.candidate_id))
    assert accepted.verdict == 'accepted'
    asyncio.run(case.app.answers.apply_feedback(accepted, case.token))
    assert case.app.state.task_run.status.value == 'achieved'
    assert len(candidate_model['calls']) == 2 and len(case.app.state.facts) == 2


def test_checked_rejection_requires_explicit_public_resume(public_candidate, monkeypatch):
    from agent_adapters import validation_check, task_cli
    from agent_core.goal_feedback import ExternalValidationCheckResult
    from types import SimpleNamespace
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    class Checker:
        def check(self, request):
            return ExternalValidationCheckResult(request, 'rejected', 'test-only-checked-rejection')
    monkeypatch.setattr(validation_check, 'configured_checker', lambda _: Checker())
    before = len(case.calls)
    checked = validation_check.dispatch_check(case.directory, case.declaration, selection(case, candidate, submitted))
    assert checked['task_run']['status'] == 'running' and len(case.calls) == before
    args = SimpleNamespace(command='resume', task_run_id=case.task, cleanup_only=False, events=None)
    resumed = task_cli.dispatch_v3_existing(args, case.directory, case.declaration)
    assert not resumed['recovery']['required'], resumed
    assert len(case.calls) > before
    assert resumed['answer_candidates'][0]['validation_state'] == 'rejected'


def test_conflicting_feedback_blocks_success_and_trusted_check_resolves(public_candidate, monkeypatch):
    from agent_adapters import validation_check
    from agent_core.goal_feedback import ExternalValidationCheckResult
    from agent_adapters.v7.task_result import snapshot_v3_task_result
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    accepted = feedback(case, candidate, 'accepted')
    rejected = feedback(case, candidate, 'rejected')
    case.app.controller.apply_external_validation_feedback(accepted)
    case.app.controller.apply_external_validation_feedback(rejected)
    before = len(case.calls)
    conflict = snapshot_v3_task_result(case.directory, case.declaration)
    assert conflict['goal_evaluation'] == 'unknown' and conflict['recovery']['required']
    assert set(conflict['answer_candidates'][0]['checkable_submission_ids']) == {accepted.operation_id, rejected.operation_id}
    class Checker:
        def check(self, request):
            assert len(request.conflict_sources) == 2
            return ExternalValidationCheckResult(request, 'accepted', 'test-only-conflict-check')
    monkeypatch.setattr(validation_check, 'configured_checker', lambda _: Checker())
    result = validation_check.dispatch_check(case.directory, case.declaration, selection(case, candidate, accepted))
    assert result['answer_candidates'][0]['validation_state'] == 'accepted', result
    assert result['goal_evaluation'] == 'accepted' and len(case.calls) == before


@pytest.mark.parametrize('output_case', [19], indirect=True)
@pytest.mark.parametrize('damage', ['execution', 'extra', 'empty', 'invalid_json', 'nul', 'utf8_limit'])
def test_candidate_boundary_rejection_is_single_and_body_free(output_case, candidate_model, damage):
    case = output_case
    def invalid(envelope):
        raw = {'action': {'kind': 'candidate_flag', 'value': 'PRIVATE_BAD_ANSWER',
                          'execution_id': envelope.request.context.execution_id}}
        if damage == 'execution': raw['action']['execution_id'] = str(uuid4())
        elif damage == 'extra': raw['action']['evidence_version'] = '0' * 64
        elif damage == 'empty': raw['action']['value'] = ''
        elif damage == 'nul': raw['action']['value'] = 'PRIVATE_BAD_ANSWER\x00'
        elif damage == 'utf8_limit': raw['action']['value'] = '\u4e2d' * 6000
        else: return 'not JSON PRIVATE_BAD_ANSWER'
        return raw
    candidate_model['value'] = invalid
    result = run_output(case)
    assert result.recovery_required
    assert len(candidate_model['calls']) == 1
    assert case.app.store.list_d7_answer_candidates(case.task) == ()
    assert not list((case.directory / 'd7-candidate-evidence').glob('*.cipher'))
    for folder in ('d8-output', 'd8-sessions', 'd3-models'):
        assert all('PRIVATE_BAD_ANSWER' not in p.read_text(encoding='utf-8')
                   for p in (case.directory / folder).rglob('*.json'))


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_no_candidate_corrupt_decision_blocks_new_dispatch(output_case, candidate_model, monkeypatch):
    case = output_case
    assert run_output(case).stop_reason == 'search_exhausted'
    original = case.app.output_boundary.evidence.read
    def damaged(kind, identity):
        value = original(kind, identity)
        return {**value, 'candidate': {'forged': True}} if kind == 'answer-decision' else value
    monkeypatch.setattr(case.app.output_boundary.evidence, 'read', damaged)
    before = len(candidate_model['calls'])
    result = run_output(case)
    assert result.recovery_required and len(candidate_model['calls']) == before


@pytest.mark.parametrize('output_case', [19], indirect=True)
@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_cancellation_and_deadline_do_not_achieve_goal(output_case, candidate_model, stop):
    case = output_case
    candidate_model['value'] = 'PRIVATE_STOPPED_ANSWER'
    assert run_output(case).stop_reason == 'd9_awaiting_external_feedback'
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    before = len(candidate_model['calls'])
    if stop == 'cancel':
        case.token.is_cancelled = True
        with pytest.raises(D3RecoveryRequired, match='cancelled_before_application'):
            asyncio.run(case.app.answers.apply_feedback(feedback(case, candidate, 'accepted'), case.token))
        case.app.cancel(case.task)
        assert case.app.state.task_run.status.value == 'cancelled'
    else:
        case.clock.now = case.app.state.task_run.deadline_at + 1
        asyncio.run(case.app.answers.apply_feedback(feedback(case, candidate, 'accepted'), case.token))
        assert case.app.state.task_run.status.value == 'deadline_exceeded'
    assert len(candidate_model['calls']) == before


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_committed_candidate_reload_failure_is_limited_and_resumable(output_case, candidate_model, monkeypatch):
    case = output_case
    candidate_model['value'] = 'PRIVATE_RELOAD_ANSWER'
    repository = case.app.controller._persistent.repository
    original = repository.load_control_state
    def reload_state(task):
        if case.app.store.list_d7_answer_candidates(task):
            raise RuntimeError('PRIVATE_RELOAD_FAILURE')
        return original(task)
    monkeypatch.setattr(repository, 'load_control_state', reload_state)
    result = run_output(case)
    assert result.recovery_required
    assert result.stop_reason.startswith('d8_controller_reload_required:')
    assert result.verified_fact_ids == () and not case.sandbox.destroyed
    assert len(case.app.store.list_d7_answer_candidates(case.task)) == 1
    before = len(candidate_model['calls'])
    case.app.store.close()
    case.app = case.create()
    restored = asyncio.run(case.app.resume(case.task, case.token))
    assert restored.stop_reason == 'd9_awaiting_external_feedback'
    assert len(candidate_model['calls']) == before


def test_check_failure_keeps_durable_start_and_reuses_readonly_query(public_candidate, monkeypatch):
    from agent_adapters import validation_check
    from agent_core.goal_feedback import ExternalValidationCheckResult
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    picked = selection(case, candidate, submitted)
    before = len(case.calls)
    class Checker:
        calls = []
        def check(self, request):
            persisted = case.app.store.load_d7_external_validation_check(request.query_id)
            assert persisted is not None and persisted.result is None
            self.calls.append(request)
            if len(self.calls) == 1:
                raise RuntimeError('PRIVATE_VERIFIER_ERROR')
            return ExternalValidationCheckResult(request, 'rejected', 'test-only-query-recovered')
    checker = Checker()
    monkeypatch.setattr(validation_check, 'configured_checker', lambda _: checker)
    failed = validation_check.dispatch_check(case.directory, case.declaration, picked)
    assert failed['exit_code'] == 5 and failed['recovery']['required']
    assert 'PRIVATE_VERIFIER_ERROR' not in json.dumps(failed)
    assert case.app.store.load_d7_external_validation_check(picked['query_id']).result is None
    recovered = validation_check.dispatch_check(case.directory, case.declaration, picked)
    assert recovered['validation_check']['state'] == 'rejected'
    assert checker.calls[0] == checker.calls[1]
    assert len(case.app.store.list_d7_external_validation_checks(case.task)) == 1
    assert len(case.calls) == before


@pytest.mark.parametrize('output_case', [19], indirect=True)
def test_candidate_action_is_charged_once_across_replay(output_case, candidate_model):
    case = output_case
    candidate_model['value'] = 'PRIVATE_CHARGED_ANSWER'
    assert run_output(case).stop_reason == 'd9_awaiting_external_feedback'
    request = candidate_model['calls'][0].request
    operation = case.app.store.load_d4_operation(request.model_request_id)
    assert operation.start.category == 'action' and operation.start.charged
    assert case.app.config.d4_budget.total == 60
    usage = case.app.store.d4_budget_usage(case.task)
    asyncio.run(case.app.answers.after_success(case.app.state.intents[0], case.token))
    assert case.app.store.d4_budget_usage(case.task) == usage


def test_public_binding_rejection_preserves_task_identity(public_candidate):
    from agent_adapters import task_cli
    from types import SimpleNamespace
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    picked = selection(case, candidate, submitted)
    picked['evidence_version'] = '0' * 64
    args = SimpleNamespace(command='validation-check', task_run_id=case.task, events=None, **picked)
    result = task_cli.dispatch_v3_existing(args, case.directory, case.declaration)
    assert result['exit_code'] == 2 and result['task_run_id'] == case.task
    assert result['contract_version'] == 19 and result['task_run']['status'] == 'awaiting_external_feedback'
    assert case.app.store.list_d7_external_validation_checks(case.task) == ()


def test_check_event_export_and_cached_replay_are_contiguous(public_candidate, monkeypatch):
    from agent_adapters import validation_check
    from agent_core.goal_feedback import ExternalValidationCheckResult
    case = public_candidate
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submitted = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submitted)
    picked = selection(case, candidate, submitted)
    class Checker:
        def check(self, request):
            return ExternalValidationCheckResult(request, 'rejected', 'test-only-export-check')
    monkeypatch.setattr(validation_check, 'configured_checker', lambda _: Checker())
    events = case.directory.parent.parent / 'check-events.jsonl'
    result = validation_check.dispatch_check(case.directory, case.declaration, picked, events=str(events))
    assert result['event_export']['error'] is None, result
    before = events.read_bytes()
    replay = validation_check.dispatch_check(case.directory, case.declaration, picked, events=str(events))
    assert replay['event_export']['error'] is None and events.read_bytes() == before
    rows = [json.loads(line) for line in before.decode('utf-8').splitlines()]
    assert [row['sequence'] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row['task_run_id'] == case.task for row in rows)
    assert b'PRIVATE_PUBLIC_CANDIDATE' not in before
