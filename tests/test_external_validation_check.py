from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_controller_lifecycle import _completed_candidate
from test_turn_budget import make_case


@pytest.fixture
def run_dir():
    path = Path(".test") / f"d7-q20-{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)

from agent_core.goal_feedback import (
    ExternalValidationCheckRequest,
    ExternalValidationCheckResult,
    ExternalValidationFeedback,
    ExternalValidationFeedbackConflict,
    ExternalValidationReconciliation,
)
from agent_core.runtime import TaskRunStatus


class Checker:
    def __init__(self, result_factory):
        self.result_factory = result_factory
        self.calls = 0

    def check(self, request):
        self.calls += 1
        return self.result_factory(request)


def _await_unknown_candidate(app, token):
    assert asyncio.run(app.run(token)).stop_reason == "turn_completed_sandbox_retained"
    candidate = _completed_candidate(app)
    app.controller.register_answer_candidate(candidate, str(uuid4()))
    submission = ExternalValidationFeedback(
        task_run_id=candidate.task_run_id,
        candidate_id=candidate.candidate_id,
        evidence_version=candidate.evidence_version,
        verdict="unknown",
        source_ref="judge:submission-unknown",
        operation_id=str(uuid4()),
    )
    app.controller.apply_external_validation_feedback(submission)
    checkpoint_sequence = app.state.latest_checkpoint.sequence + 1
    request = ExternalValidationCheckRequest(
        query_id=str(uuid4()),
        task_run_id=candidate.task_run_id,
        submission_operation_id=submission.operation_id,
        candidate_id=candidate.candidate_id,
        evidence_version=candidate.evidence_version,
        checkpoint_sequence=checkpoint_sequence,
    )
    return candidate, submission, request


def _result(request, verdict):
    return ExternalValidationCheckResult(
        request=request,
        verdict=verdict,
        source_ref=f"judge:check:{verdict}",
    )


def _close(app):
    if app.state.task_run.status is TaskRunStatus.RUNNING:
        app.cancel()
    elif app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:
        app.controller.close()
    else:
        app.controller.cleanup_sandbox()
    app.close()


def test_check_start_is_durable_before_checker_and_unknown_stays_awaiting(run_dir):
    app, token = make_case(run_dir, 2)
    try:
        _candidate, submission, request = _await_unknown_candidate(app, token)

        def inspect_then_unknown(current):
            saved = app.store.load_d7_external_validation_check(current.query_id)
            assert saved is not None
            assert saved.request == current
            assert saved.result is None
            assert saved.started_event_sequence > 0
            assert app.state.latest_checkpoint.sequence == current.checkpoint_sequence
            return _result(current, "unknown")

        checker = Checker(inspect_then_unknown)
        record = app.controller.run_external_validation_check(request, checker)

        assert checker.calls == 1
        assert record.result == _result(request, "unknown")
        assert record.result_event_sequence is not None
        assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
        assert app.store.list_d7_validation_feedback(
            submission.task_run_id, submission.candidate_id, submission.evidence_version
        ) == (submission,)
    finally:
        _close(app)


def test_accepted_check_atomically_appends_reconciliation_and_replays_without_query(
    run_dir,
):
    app, token = make_case(run_dir, 2)
    try:
        _candidate, submission, request = _await_unknown_candidate(app, token)
        checker = Checker(lambda current: _result(current, "accepted"))

        record = app.controller.run_external_validation_check(request, checker)
        replay = app.controller.run_external_validation_check(request, checker)
        feedback = app.store.list_d7_validation_feedback(
            submission.task_run_id, submission.candidate_id, submission.evidence_version
        )

        assert checker.calls == 1
        assert replay == record
        assert isinstance(feedback[-1], ExternalValidationReconciliation)
        assert feedback[-1].operation_id == request.query_id
        assert feedback[-1].resolves_operation_id == submission.operation_id
        assert app.state.task_run.status is TaskRunStatus.ACHIEVED
    finally:
        _close(app)


def test_reused_query_id_with_changed_request_is_rejected_before_checker(run_dir):
    app, token = make_case(run_dir, 2)
    try:
        _candidate, _submission, request = _await_unknown_candidate(app, token)
        checker = Checker(lambda current: _result(current, "unknown"))
        app.controller.run_external_validation_check(request, checker)
        changed = replace(request, evidence_version="b" * 64)

        with pytest.raises(ExternalValidationFeedbackConflict, match="different check"):
            app.controller.run_external_validation_check(changed, checker)

        assert checker.calls == 1
    finally:
        _close(app)


def test_failed_result_commit_keeps_start_and_retries_same_query_id(run_dir):
    app, token = make_case(run_dir, 2)
    try:
        _candidate, submission, request = _await_unknown_candidate(app, token)
        checker = Checker(lambda current: _result(current, "rejected"))
        original_save = app.store._save_d7_mutation
        fail_once = True

        def fail_after_writes(mutation, event_sequence):
            nonlocal fail_once
            original_save(mutation, event_sequence)
            if mutation.operation == "complete_external_validation_check" and fail_once:
                fail_once = False
                raise RuntimeError("simulated interruption before transaction commit")

        app.store._save_d7_mutation = fail_after_writes
        with pytest.raises(RuntimeError, match="simulated interruption"):
            app.controller.run_external_validation_check(request, checker)

        interrupted = app.store.load_d7_external_validation_check(request.query_id)
        assert interrupted is not None and interrupted.result is None
        assert app.store.list_d7_validation_feedback(
            submission.task_run_id, submission.candidate_id, submission.evidence_version
        ) == (submission,)
        assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK

        app.store._save_d7_mutation = original_save
        saved = app.controller.run_external_validation_check(request, checker)

        assert checker.calls == 2
        assert saved.result == _result(request, "rejected")
        assert app.state.task_run.status is TaskRunStatus.RUNNING
    finally:
        _close(app)
