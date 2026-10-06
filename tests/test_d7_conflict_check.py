"""Authoritative conflict queries preserve history and never reopen cleaned runs."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_d7_controller_lifecycle import _completed_candidate
from test_d7_turn_budget import make_case

from agent_core.goal_feedback import (
    ExternalValidationCheckRequest,
    ExternalValidationCheckResult,
    ExternalValidationConflictSource,
    ExternalValidationFeedback,
    ExternalValidationFeedbackConflict,
    external_validation_feedback_from_json,
    project_candidate_feedback,
)
from agent_core.operation_accounting import D4RecoveryRequired
from agent_core.runtime import TaskRunStatus


def setup_conflict(app, token, *, first="accepted"):
    asyncio.run(app.run(token))
    candidate = _completed_candidate(app)
    app.controller.register_answer_candidate(candidate, str(uuid4()))
    records = []
    for verdict in (first, "rejected" if first == "accepted" else "accepted"):
        feedback = ExternalValidationFeedback(candidate.task_run_id, candidate.candidate_id, candidate.evidence_version, verdict, "test-trusted-judge", str(uuid4()))
        app.controller.apply_external_validation_feedback(feedback)
        records.append(feedback)
    request = ExternalValidationCheckRequest(
        str(uuid4()), candidate.task_run_id, records[0].operation_id,
        candidate.candidate_id, candidate.evidence_version,
        app.state.latest_checkpoint.sequence + 1,
        tuple(ExternalValidationConflictSource.from_record(item) for item in sorted(records, key=lambda item: item.operation_id)),
    )
    return candidate, records, request


class Checker:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def check(self, request):
        self.calls += 1
        return ExternalValidationCheckResult(request, self.verdict, "authoritative-test-query")


def close(app):
    if app.state.task_run.status in {TaskRunStatus.RUNNING, TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK}:
        app.cancel()
    else:
        app.controller.cleanup_sandbox()
    app.close()


@pytest.mark.parametrize("verdict", ["accepted", "rejected", "unknown"])
def test_terminal_conflict_query_preserves_history_and_cleanup(tmp_path, verdict):
    app, token = make_case(tmp_path, 2)
    try:
        candidate, originals, request = setup_conflict(app, token)
        app.controller.cleanup_sandbox()
        task_status = app.state.task_run.status
        finished_at = app.state.task_run.finished_at
        cleanup_events = tuple(
            event
            for event in app.store.list_d4_audit_events(candidate.task_run_id)
            if event.category == "cleanup_completed"
        )
        assert cleanup_events
        assert all(event.outcome == "known" for event in cleanup_events)
        usage = app.store.d4_budget_usage(candidate.task_run_id)
        checker = Checker(verdict)
        record = app.controller.run_external_validation_check(request, checker)
        assert app.controller.run_external_validation_check(request, checker) == record
        assert checker.calls == 1
        assert app.state.task_run.status is task_status is TaskRunStatus.ACHIEVED
        assert app.state.task_run.finished_at == finished_at
        assert tuple(
            event
            for event in app.store.list_d4_audit_events(candidate.task_run_id)
            if event.category == "cleanup_completed"
        ) == cleanup_events
        assert app.store.d4_budget_usage(candidate.task_run_id) == usage
        records = app.store.list_d7_validation_feedback(candidate.task_run_id, candidate.candidate_id, candidate.evidence_version)
        assert records[:2] == tuple(originals)
        projection = project_candidate_feedback(candidate.task_run_id, candidate.candidate_id, candidate.evidence_version, records)
        assert projection.state == ("conflict" if verdict == "unknown" else verdict)
        if verdict != "unknown":
            assert external_validation_feedback_from_json(records[-1].to_json()) == records[-1]
        assert len(app.store.list_d7_external_validation_checks(candidate.task_run_id)) == 1
    finally:
        close(app)


def test_conflict_source_body_cannot_be_changed_before_checker(tmp_path):
    app, token = make_case(tmp_path, 2)
    try:
        _, _, request = setup_conflict(app, token, first="rejected")
        sources = list(request.conflict_sources)
        sources[0] = replace(sources[0], body_sha256="b" * 64)
        changed = replace(request, conflict_sources=tuple(sources))
        checker = Checker("accepted")
        with pytest.raises(ExternalValidationFeedbackConflict, match="all current"):
            app.controller.run_external_validation_check(changed, checker)
        assert checker.calls == 0
        assert app.store.load_d7_external_validation_check(request.query_id) is None
    finally:
        close(app)


def test_deadline_does_not_start_new_conflict_query(tmp_path):
    app, token = make_case(tmp_path, 2)
    try:
        _, _, request = setup_conflict(app, token)
        app.controller._clock = lambda: app.state.task_run.deadline_at
        checker = Checker("accepted")
        with pytest.raises(D4RecoveryRequired, match="deadline_unknown_retained"):
            app.controller.run_external_validation_check(request, checker)
        assert checker.calls == 0
    finally:
        close(app)
