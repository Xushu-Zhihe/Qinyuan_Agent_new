"""Contract12 field ownership and bound recovery through the actual public core."""

import json
from types import SimpleNamespace
from dataclasses import replace

import pytest

from agent_adapters import cli, task_cli
from agent_adapters.simulation import SimulationModel
from agent_adapters.deepseek_roles import role_config, build_payload
from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_adapters.handoff_evidence import FileD8HandoffEvidence
from agent_adapters.conclusion_evidence import FileD8Evidence
from agent_adapters.attachment_import_recorder import FileAttachmentImportRecorder
from agent_adapters.run_files import immutable_read
from agent_adapters.web.task_result import snapshot_v3_task_result
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.planning_assertion import PlanningAssertionBindings, planning_policy
from test_public_simulation import request


def run_s2(tmp_path, monkeypatch, *, count=1, modify=None):
    raw = request(tmp_path, count=count)
    raw['execution_profile'] = 'd8_v12'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    seen = []
    original = SimulationModel.invoke
    def invoke(model, envelope, *args):
        value = original(model, envelope, *args)
        seen.append(envelope)
        return modify(envelope, value) if modify else value
    monkeypatch.setattr(SimulationModel, 'invoke', invoke)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    result = task_cli.dispatch_run_task(args)
    directory = args.state_dir / result['task_run_id']
    return result, args, seen, directory


def bindings(directory, store):
    declaration = immutable_read(directory / 'run.json')
    config = store.load_d3_run_config(declaration['task_run_id'])
    return PlanningAssertionBindings(config, FileD8OutputEvidence(directory / 'd8-output', create=False),
        FileD8HandoffEvidence(directory / 'd8-handoff', create=False),
        FileD8Evidence(directory / 'd8-conclusions', create=False), store,
        FileAttachmentImportRecorder(directory / 'imports', read_only=True))


def test_public_s2_binds_choices_and_keeps_s1(tmp_path, monkeypatch):
    result, args, seen, directory = run_s2(tmp_path, monkeypatch)
    assert result['errors'] == [], result
    assert result['contract_version'] == 12 and len(result['verified_facts']) == 1, result
    assert not result['recovery']['required'], result
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    for e in seen:
        identity = e.request.model_request_id if hasattr(e.request, 'model_request_id') else None
        if e.output_contract['stage'] == 'tree_planning':
            record = output.read('planning-binding', identity)
            assert 'turn_target' not in record['wire_response']
            if record['wire_response']['kind'] == 'candidates':
                assert 'version' not in record['wire_response']['completion_rule']
                assert record['canonical_response']['turn_target'] == e.request.goal
                assert record['canonical_response']['completion_rule']['version'] == 1
        if e.output_contract['stage'] == 'fact_proposal':
            record = output.read('fact-assertion-binding', identity)
            assert record['wire_response']['assertion'] == {'rule_id': 'attachment_copy_sha256'}
            assert record['wire_response']['evidence_refs'] == [e.request.observation.proof_id]
            assert record['canonical_response']['assertion']['artifact_version_id'] == e.request.observation.artifact_version_id
        if e.output_contract['stage'] in {'inner_size', 'inner_sha256'}:
            assert output.read('fixed-probe-binding', identity)['contract_version'] == 12
    before = (directory / 'control.sqlite').read_bytes()
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('known run called model'))
    assert task_cli.dispatch_run_task(args)['task_run_id'] == result['task_run_id']
    assert (directory / 'control.sqlite').read_bytes() == before
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    assert cli.dispatch(args)['cleanup_status'] == 'confirmed'


def test_two_attachments_model_can_choose_one(tmp_path, monkeypatch):
    def choose(e, raw):
        if e.output_contract['stage'] == 'tree_planning' and raw['kind'] == 'candidates':
            assert planning_policy(e.request)['attachment_indices'] == [0, 1]
            raw['completion_rule']['parameters']['attachment_index'] = 1
        return raw
    result, _, _, directory = run_s2(tmp_path, monkeypatch, count=2, modify=choose)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        state = store.load_control_state_rows(result['task_run_id'])
        assert {store.load_d3_turn_binding(t.turn_id).attachment_index for t in state.turns} == {1}


@pytest.mark.parametrize('stage,field', [('tree_planning', 'turn_target'), ('tree_planning', 'version'),
    ('tree_planning', 'rule'), ('tree_planning', 'attachment'), ('tree_planning', 'attachment_type'),
    ('fact_proposal', 'rule_version'), ('fact_proposal', 'artifact_version_id'),
    ('fact_proposal', 'observation_id'), ('fact_proposal', 'rule'), ('fact_proposal', 'proof')])
def test_forbidden_generation_corrected_once_without_bad_binding(tmp_path, monkeypatch, stage, field):
    rejected = []
    def change(e, raw):
        if e.output_contract['stage'] == stage and e.correction is None and raw['kind'] in {'candidates', 'candidate'}:
            rejected.append(e.request.model_request_id)
            if stage == 'tree_planning':
                if field == 'turn_target': raw[field] = 'PRIVATE_REJECTED_TARGET'
                elif field == 'version': raw['completion_rule'][field] = 1
                elif field == 'rule': raw['completion_rule']['rule_id'] = 'PRIVATE_REJECTED_RULE'
                else: raw['completion_rule']['parameters']['attachment_index'] = '0' if field == 'attachment_type' else 1
            elif field == 'proof': raw['evidence_refs'] = [e.request.observation.artifact_version_id]
            elif field == 'rule': raw['assertion']['rule_id'] = 'PRIVATE_REJECTED_RULE'
            else: raw['assertion'][field] = 'PRIVATE_REJECTED_BINDING'
        return raw
    result, _, seen, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    assert len([e for e in seen if e.output_contract['stage'] == stage and e.correction is not None]) == 1
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    kind = 'planning-binding' if stage == 'tree_planning' else 'fact-assertion-binding'
    assert rejected and output.read(kind, rejected[0]) is None
    assert output.read('diagnostic', rejected[0]) is not None
    assert 'PRIVATE_REJECTED' not in ''.join(p.read_text(encoding='utf-8') for p in (directory / 'd8-output').rglob('*.json'))


@pytest.mark.parametrize('stage', ['tree_planning', 'fact_proposal'])
def test_business_invalid_keeps_safe_diagnostic_only(tmp_path, monkeypatch, stage):
    rejected = []
    def change(e, raw):
        if e.output_contract['stage'] == stage and e.correction is None and raw['kind'] in {'candidates', 'candidate'}:
            rejected.append(e.request.model_request_id)
            if stage == 'tree_planning': raw['intents'][0]['description'] = ' '
            else: raw['content'] = ' '
        return raw
    result, _, _, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    kind = 'planning-binding' if stage == 'tree_planning' else 'fact-assertion-binding'
    assert output.read(kind, rejected[0]) is None
    assert output.read('business-diagnostic', rejected[0]) is not None


def test_inference_and_outer_rejection_never_become_verified(tmp_path, monkeypatch):
    counts = {'proposal': 0}
    def change(e, raw):
        if e.output_contract['stage'] == 'fact_proposal':
            counts['proposal'] += 1
            if counts['proposal'] == 1: raw['assertion'] = None
        return raw
    result, _, _, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        rows = store.list_d8_conclusions(result['task_run_id'])
        assert [r.outcome for r in rows] == ['reviewed_inference', 'verified_fact']
        b = bindings(directory, store)
        req = b.conclusions.read_request(rows[0].proposal_request_id)
        assert b.read(req)['canonical_response']['assertion'] is None


@pytest.mark.parametrize('stage', ['tree_planning', 'fact_proposal'])
def test_known_materialization_repairs_original_result_without_call(tmp_path, monkeypatch, stage):
    result, args, seen, directory = run_s2(tmp_path, monkeypatch)
    e = next(e for e in seen if e.output_contract['stage'] == stage)
    declaration = immutable_read(directory / 'run.json')
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        b = bindings(directory, store)
        req = b._request(stage, e.request.model_request_id)
        record = b.read(req)
        expected = b.result(req, record)
    recorder = b.handoff if stage == 'tree_planning' else b.conclusions
    # Fault injection deletes only this test's known result, retaining its binding and completed ledger.
    result_kind = 'result' if stage == 'tree_planning' else 'd8-result'
    path = next(p for p in (directory / ('d8-handoff' if stage == 'tree_planning' else 'd8-conclusions')).rglob('*.json')
                if result_kind in p.name and e.request.model_request_id in p.name)
    path.unlink()
    before = (directory / 'control.sqlite').read_bytes()
    projection = snapshot_v3_task_result(directory, declaration)
    assert projection['task_run_id'] == result['task_run_id'] and projection['recovery']['required'], projection
    assert not path.exists() and (directory / 'control.sqlite').read_bytes() == before
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('recovery called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    cancelled = cli.dispatch(args)
    assert cancelled['cleanup_status'] == 'confirmed', cancelled
    assert (recorder.read('result', req.model_request_id) if stage == 'tree_planning'
            else recorder.read_result(req.model_request_id)) == expected


@pytest.mark.parametrize('stage', ['tree_planning', 'fact_proposal'])
@pytest.mark.parametrize('failure', ['missing', 'canonical', 'wire', 'role', 'checkpoint', 'policy', 'result', 'root'])
def test_binding_corruption_pauses_without_repair_or_dispatch(tmp_path, monkeypatch, stage, failure):
    from agent_core.turn_modeling import digest
    result, args, seen, directory = run_s2(tmp_path, monkeypatch)
    e = next(e for e in seen if e.output_contract['stage'] == stage)
    kind = 'planning-binding' if stage == 'tree_planning' else 'fact-assertion-binding'
    path = next(p for p in (directory / 'd8-output').rglob('*.json') if kind in p.name and e.request.model_request_id in p.name)
    if failure == 'missing':
        path.unlink()
    else:
        if failure == 'result':
            root = directory / ('d8-handoff' if stage == 'tree_planning' else 'd8-conclusions')
            prefix = 'result' if stage == 'tree_planning' else 'd8-result'
            path = next(p for p in root.rglob('*.json') if prefix in p.name and e.request.model_request_id in p.name)
        envelope = json.loads(path.read_text(encoding='utf-8'))
        payload = envelope['payload']
        if failure == 'canonical': payload['canonical_response']['reason' if stage == 'fact_proposal' else 'turn_target'] = 'Changed'
        elif failure == 'wire': payload['wire_response']['kind'] = 'exhausted' if stage == 'fact_proposal' else 'candidate'
        elif failure == 'role': payload['role_config_ref'] = 'changed-role'
        elif failure == 'checkpoint': payload['base_checkpoint_sequence'] += 1
        elif failure == 'policy': payload['policy']['rule_version'] = 2
        elif failure == 'root': payload['root_request_id'] = result['task_run_id']
        else: payload['raw']['reason' if stage == 'fact_proposal' else 'turn_target'] = 'Changed'
        # Re-sign only the test file envelope to prove semantic binding, not just file checksums.
        envelope['sha256'] = digest(payload)
        path.write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')
    before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()
              and not p.name.endswith(('-wal', '-shm'))}
    projection = snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))
    assert projection['recovery']['required'], projection
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('bad binding called model'))
    args.command, args.task_run_id = 'cancel', result['task_run_id']
    cancelled = cli.dispatch(args)
    assert cancelled['recovery']['required'] and cancelled['cleanup_status'] != 'confirmed', cancelled
    assert {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()
            and not p.name.endswith(('-wal', '-shm'))} == before


@pytest.mark.parametrize('stage', ['tree_planning', 'fact_proposal'])
def test_unknown_model_has_no_binding_and_no_automatic_retry(tmp_path, monkeypatch, stage):
    from agent_core.modeling import ModelError, ModelErrorKind
    calls = []
    def change(e, raw):
        if e.output_contract['stage'] == stage:
            calls.append(e.request.model_request_id)
            return ModelError(ModelErrorKind.TIMED_OUT, 'controlled_unknown')
        return raw
    result, args, _, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['recovery']['required'] and len(calls) == 1, result
    output = FileD8OutputEvidence(directory / 'd8-output', create=False)
    assert output.read('planning-binding' if stage == 'tree_planning' else 'fact-assertion-binding', calls[0]) is None
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('unknown called model'))
    args.command, args.task_run_id = 'resume', result['task_run_id']
    assert cli.dispatch(args)['recovery']['required']


def test_partial_dependency_rejection_preserves_valid_sibling(tmp_path, monkeypatch):
    def change(e, raw):
        if e.output_contract['stage'] == 'tree_planning' and raw['kind'] == 'candidates':
            raw['intents'].insert(0, {**raw['intents'][0], 'dependency_refs': ['unknown-history-reference']})
        return raw
    result, _, seen, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    e = next(e for e in seen if e.output_contract['stage'] == 'tree_planning')
    record = FileD8OutputEvidence(directory / 'd8-output', create=False).read('planning-binding', e.request.model_request_id)
    assert record['rejected_candidate_indices'] == [0] and len(record['wire_response']['intents']) == 2
    assert not any(e.correction for e in seen if e.output_contract['stage'] == 'tree_planning')


def test_outer_rejects_registered_assertion_before_later_accepted_proposal(tmp_path, monkeypatch):
    count = []
    def change(e, raw):
        if e.output_contract['stage'] == 'fact_review':
            count.append(1)
            if len(count) == 1: raw['decision'] = 'reject'
        return raw
    result, _, _, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        assert [r.outcome for r in store.list_d8_conclusions(result['task_run_id'])] == ['rejected', 'verified_fact']


def test_v7_http_uses_new_wire_and_inherits_fixed_and_review_offline(tmp_path, monkeypatch):
    from agent_adapters.deepseek_roles import EstimatedCapacity, CAPACITY_POLICY
    result, _, seen, _ = run_s2(tmp_path, monkeypatch)
    assert result['verified_facts']
    outer, inner = role_config('outer_planning', version=7), role_config('inner_action', version=7)
    for e in seen:
        stage = e.output_contract['stage']
        if stage not in {'tree_planning', 'fact_proposal', 'inner_size', 'inner_sha256'}: continue
        role = outer if stage == 'tree_planning' else inner
        current = replace(e.request, role_config_ref=role.role_config_ref)
        envelope = replace(e, request=current)
        payload = build_payload(envelope, role)
        system = payload['messages'][0]['content']
        assert payload['model'] == 'deepseek-flash' and payload['response_format'] == {'type': 'json_object'}
        assert EstimatedCapacity(outer, inner).fits(envelope, CAPACITY_POLICY)
        if stage == 'tree_planning':
            assert 'never generate turn_target or rule version' in system
            assert 'copy turn_target exactly' not in system
        if stage == 'fact_proposal':
            assert 'Never generate rule_version' in system and 'allowed_evidence_refs' in json.loads(payload['messages'][1]['content'])
        if stage in {'inner_size', 'inner_sha256'}: assert 'Never generate script' in system
        if stage in {'tree_planning', 'fact_proposal'}:
            with pytest.raises(ValueError, match='output_contract_mismatch'):
                build_payload(replace(envelope, request=replace(current,
                    role_config_ref=role_config(role.role, version=6).role_config_ref)), role_config(role.role, version=6))


def test_new12_workbench_child_and_explicit_cancel(tmp_path):
    import io
    from agent_adapters.workbench import Workbench
    from test_workbench_v3 import _challenge, _wait_for_cli
    workbench = Workbench(tmp_path, protocol_version=3, execution_profile='d8_v12')
    challenge = workbench.create(_challenge())
    workbench.add_attachment(challenge['id'], 'sample.txt', io.BytesIO(b'plain'), 5)
    workbench.runner.start(challenge['id'], mode='simulated')
    run = _wait_for_cli(workbench, challenge['id'])
    assert run['result']['contract_version'] == 12 and run['result']['verified_facts'], run
    assert run['result']['event_export']['error'] is None
    workbench.runner.cancel(challenge['id'])
    assert _wait_for_cli(workbench, challenge['id'])['result']['cleanup_status'] == 'confirmed'


@pytest.mark.parametrize('stage', ['tree_planning', 'fact_proposal'])
@pytest.mark.parametrize('phase', ['before_binding', 'after_binding', 'completion'])
def test_crash_sequence_recovers_only_complete_known_binding(tmp_path, monkeypatch, stage, phase):
    saved = FileD8OutputEvidence.save
    complete = SqliteControlStore.complete_d4_operation
    kind = 'planning-binding' if stage == 'tree_planning' else 'fact-assertion-binding'
    tripped = []
    def save(port, item_kind, identity, value):
        if item_kind == kind and not tripped:
            tripped.append(identity)
            if phase == 'before_binding': raise SystemExit('controlled loss')
            result = saved(port, item_kind, identity, value)
            if phase == 'after_binding': raise SystemExit('controlled loss')
            return result
        return saved(port, item_kind, identity, value)
    def finish(store, completion):
        if phase == 'completion' and completion.operation_id in tripped: raise SystemExit('controlled loss')
        return complete(store, completion)
    monkeypatch.setattr(FileD8OutputEvidence, 'save', save)
    monkeypatch.setattr(SqliteControlStore, 'complete_d4_operation', finish)
    raw = request(tmp_path)
    raw['execution_profile'] = 'd8_v12'
    monkeypatch.setattr(task_cli, '_input', lambda _: raw)
    args = SimpleNamespace(request='unused', mode='simulated', state_dir=tmp_path / 'state', events=None)
    with pytest.raises(SystemExit): task_cli.dispatch_run_task(args)
    directory = next(p for p in args.state_dir.iterdir() if (p / 'control.sqlite').exists())
    monkeypatch.setattr(FileD8OutputEvidence, 'save', saved)
    monkeypatch.setattr(SqliteControlStore, 'complete_d4_operation', complete)
    monkeypatch.setattr(SimulationModel, 'invoke', lambda *a: pytest.fail('crash recovery called model'))
    args.command, args.task_run_id = 'cancel', directory.name
    result = cli.dispatch(args)
    if phase == 'before_binding':
        assert result['recovery']['required'] and result['cleanup_status'] != 'confirmed', result
    else:
        assert result['cleanup_status'] == 'confirmed', result
        with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
            assert store.load_d4_operation(tripped[0]).completion is not None


def test_request_review_branch_preserves_choice_and_binding(tmp_path, monkeypatch):
    attempts = []
    def change(e, raw):
        if e.output_contract['stage'] == 'fact_proposal':
            attempts.append(e)
            if len(attempts) == 1: return {'kind': 'request_review', 'reason': 'Need strategic review first'}
        return raw
    result, _, _, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['errors'] == [] and len(result['verified_facts']) == 1, result
    record = FileD8OutputEvidence(directory / 'd8-output', create=False).read('fact-assertion-binding', attempts[0].request.model_request_id)
    assert record['wire_response'] == record['canonical_response'] == {'kind': 'request_review', 'reason': 'Need strategic review first'}


def test_s2_worker_reads_files_and_binding_save_is_dispatch_thread(tmp_path, monkeypatch):
    import threading
    from agent_core.structured_output import StructuredOutput
    from agent_core.business_diagnostics import BusinessDiagnostics
    from agent_core.fact_diagnostics import FactBusinessDiagnostics
    normalize = StructuredOutput.normalize
    save = PlanningAssertionBindings.save
    saved_stages = []
    class NoSql:
        def __getattr__(self, name): pytest.fail('S2 worker queried SQLite')
    def guarded(boundary, req, raw):
        if getattr(req, 'stage', None) not in {'tree_planning', 'fact_proposal'}:
            return normalize(boundary, req, raw)
        app = boundary.app
        current = SimpleNamespace(config=app.config, store=NoSql(), clock=app.clock,
            business_diagnostics=BusinessDiagnostics(app.config, boundary.evidence, app.handoff.evidence, NoSql()),
            fact_business_diagnostics=FactBusinessDiagnostics(app.config, boundary.evidence, app.conclusions.evidence, NoSql()))
        return normalize(StructuredOutput(current, boundary.evidence), req, raw)
    def save_on_dispatch(port, req, wire, result):
        assert threading.current_thread() is threading.main_thread()
        saved_stages.append(req.stage)
        return save(port, req, wire, result)
    monkeypatch.setattr(StructuredOutput, 'normalize', guarded)
    monkeypatch.setattr(PlanningAssertionBindings, 'save', save_on_dispatch)
    result, _, _, _ = run_s2(tmp_path, monkeypatch)
    assert result['errors'] == [] and result['verified_facts'], result
    assert {'tree_planning', 'fact_proposal'} <= set(saved_stages)


@pytest.mark.parametrize('stage', ['tree_planning', 'fact_proposal'])
def test_program_field_diagnostic_cannot_be_attributed_to_new_wire(tmp_path, monkeypatch, stage):
    from agent_core.business_diagnostics import issue as planning_issue
    from agent_core.fact_diagnostics import issue as fact_issue
    from agent_core.turn_modeling import digest
    def change(e, raw):
        if e.output_contract['stage'] == stage and e.correction is None and raw['kind'] in {'candidates', 'candidate'}:
            if stage == 'tree_planning': raw['intents'][0]['description'] = ' '
            else: raw['content'] = ' '
        return raw
    result, _, seen, directory = run_s2(tmp_path, monkeypatch, modify=change)
    assert result['verified_facts']
    e = next(e for e in seen if e.output_contract['stage'] == stage)
    with SqliteControlStore.open_readonly(directory / 'control.sqlite') as store:
        b = bindings(directory, store)
        record = b.output.read('business-diagnostic', e.request.model_request_id)
        record['issues'] = [planning_issue('planning_target_invalid') if stage == 'tree_planning'
                            else fact_issue('fact_assertion_version_unsupported')]
    path = next(p for p in (directory / 'd8-output').rglob('*.json') if 'business-diagnostic' in p.name and e.request.model_request_id in p.name)
    path.write_text(json.dumps({'payload': record, 'sha256': digest(record)}, ensure_ascii=False), encoding='utf-8')
    assert snapshot_v3_task_result(directory, immutable_read(directory / 'run.json'))['recovery']['required']

