"""Public command and loopback workbench contracts for D9-4."""

import threading
import json
from uuid import uuid4

import pytest

from agent_adapters import cli
from agent_adapters.workbench import Workbench, WorkbenchError, WorkbenchServer
from agent_adapters.v7.task_result import snapshot_v3_task_result
from agent_adapters.validation_check import validate_selection
from test_d9_feedback import public_candidate, feedback, selection
from test_d7_workbench_v3 import _challenge, _NoThread, _Process, _FinishedProcess


def test_check_cli_has_only_bound_selection_and_no_verdict():
    values = {k: str(uuid4()) for k in ('candidate_id', 'submission_operation_id', 'query_id')}
    values['evidence_version'] = 'a' * 64
    task = str(uuid4())
    command = ['validation-check', '--task-run-id', task, '--state-dir', '.test/unused']
    for key, value in values.items():
        command.extend(['--' + key.replace('_', '-'), value])
    args = cli.parser().parse_args(command)
    assert validate_selection(task, {k: getattr(args, k) for k in values}) == values
    with pytest.raises(ValueError, match='invalid_cli_arguments'):
        cli.parser().parse_args(command + ['--verdict', 'accepted'])


@pytest.fixture
def bench(public_candidate, tmp_path):
    case = public_candidate
    workbench = Workbench(tmp_path / 'hub', protocol_version=3, execution_profile='d8_v19')
    challenge = workbench.create(_challenge())
    run_id = str(uuid4())
    workbench._insert_run(run_id, challenge['id'], case.directory.parent.parent)
    workbench._finish_run(run_id, status='paused', exit_code=4,
        result=snapshot_v3_task_result(case.directory, case.declaration), agent_task_run_id=case.task)
    return workbench, challenge['id'], case


def test_workbench_check_only_launches_bound_cli(bench, monkeypatch):
    import agent_adapters.workbench_runner as runner
    workbench, challenge, case = bench
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    submission = feedback(case, candidate, 'unknown')
    case.app.controller.apply_external_validation_feedback(submission)
    picked = selection(case, candidate, submission)
    calls = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda command, **kwargs: calls.append((command, kwargs)) or _Process())
    monkeypatch.setattr(runner.threading, 'Thread', _NoThread)
    workbench.runner.validation_check(challenge, picked)
    command = calls[0][0]
    assert command[1:4] == ['-m', 'agent', 'validation-check']
    assert '--env-file' not in command and '--verdict' not in command
    assert command[command.index('--task-run-id') + 1] == case.task
    assert command[command.index('--query-id') + 1] == picked['query_id']
    assert len(case.app.state.turns) == 1


def test_workbench_resume_rejects_wait_then_uses_persisted_run(bench, monkeypatch):
    import agent_adapters.workbench_runner as runner
    workbench, challenge, case = bench
    calls = []
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda command, **kwargs: calls.append(command) or _Process())
    monkeypatch.setattr(runner.threading, 'Thread', _NoThread)
    with pytest.raises(WorkbenchError, match='requires_running'):
        workbench.runner.resume(challenge)
    assert calls == []
    candidate, = case.app.store.list_d7_answer_candidates(case.task)
    case.app.controller.apply_external_validation_feedback(feedback(case, candidate, 'rejected'))
    workbench.runner.resume(challenge)
    assert calls[0][1:4] == ['-m', 'agent', 'resume']
    assert '--env-file' not in calls[0]
    assert calls[0][calls[0].index('--task-run-id') + 1] == case.task


def test_workbench_check_rejects_arbitrary_verdict_before_subprocess(bench, monkeypatch):
    import agent_adapters.workbench_runner as runner
    workbench, challenge, case = bench
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **kw: pytest.fail('invalid public body launched CLI'))
    with pytest.raises(ValueError, match='fields_invalid'):
        workbench.runner.validation_check(challenge, {'verdict': 'accepted'})


def test_loopback_routes_expose_operations_without_verdict_authority(tmp_path, monkeypatch):
    from test_workbench import call
    workbench = Workbench(tmp_path / 'hub', protocol_version=3, execution_profile='d8_v19')
    challenge = workbench.create(_challenge())['id']
    calls = []
    monkeypatch.setattr(workbench.runner, 'resume', lambda cid: calls.append(('resume', cid)) or {'status': 'running'})
    def check(cid, body):
        validate_selection(str(uuid4()), body)
        calls.append(('check', cid, body))
        return {'status': 'running'}
    monkeypatch.setattr(workbench.runner, 'validation_check', check)
    server = WorkbenchServer(('127.0.0.1', 0), workbench)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = call(server.server_port, 'POST', f'/api/challenges/{challenge}/resume', {})
        assert status == 202
        picked = dict(candidate_id=str(uuid4()), evidence_version='a' * 64,
                      submission_operation_id=str(uuid4()), query_id=str(uuid4()))
        status, _ = call(server.server_port, 'POST', f'/api/challenges/{challenge}/validation-check', picked)
        assert status == 202 and calls[-1] == ('check', challenge, picked)
        status, _ = call(server.server_port, 'POST', f'/api/challenges/{challenge}/validation-check', {'verdict': 'accepted'})
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('reported_task', [None, 'foreign'])
def test_workbench_unknown_cli_keeps_owned_task_link(bench, reported_task):
    from agent_adapters.workbench_runner import ActiveRun
    workbench, challenge, case = bench
    run = workbench.runner.status(challenge)
    process = _FinishedProcess({'protocol_version': 1, 'task_run_id': str(uuid4()) if reported_task else None,
        'exit_code': 5, 'errors': [{'code': 'operation_no_result'}]})
    workbench.runner._active[run['run_id']] = ActiveRun(run['run_id'], challenge,
        case.directory.parent.parent, process)
    workbench.runner._watch(run['run_id'])
    result = workbench.runner.status(challenge)
    assert result['agent_task_run_id'] == case.task
    assert result['status'] == 'recovery_required'
    assert result['result']['task_run_id'] == case.task
