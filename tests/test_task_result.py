"""Current external verdicts do not rewrite the first terminal run outcome."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_controller_lifecycle import _completed_candidate
from test_turn_budget import make_case

from agent_adapters.web.task_result import snapshot_v3_task_result
from agent_adapters.run_files import immutable_write
from agent_core.goal_feedback import (
    ExternalValidationCheckRequest,
    ExternalValidationCheckResult,
    ExternalValidationConflictSource,
    ExternalValidationFeedback,
)
from agent_core.runtime import TaskRunStatus


@pytest.mark.parametrize("authoritative_verdict", ["accepted", "rejected"])
def test_late_conflict_and_resolution_preserve_the_confirmed_terminal_result(
    tmp_path, authoritative_verdict,
):
    app, token = make_case(tmp_path, 2)
    try:
        assert asyncio.run(app.run(token)).stop_reason == "turn_completed_sandbox_retained"
        candidate = _completed_candidate(app)
        app.controller.register_answer_candidate(candidate, str(uuid4()))
        accepted = ExternalValidationFeedback(
            task_run_id=candidate.task_run_id,
            candidate_id=candidate.candidate_id,
            evidence_version=candidate.evidence_version,
            verdict="accepted",
            source_ref="trusted-adapter:first-result",
            operation_id=str(uuid4()),
        )
        app.controller.apply_external_validation_feedback(accepted)
        app.controller.cleanup_sandbox()
        immutable_write(
            app.directory / "cleanup.json",
            {"status": "succeeded", "sandbox_id": app.state.task_run.sandbox_id},
        )
        declaration = {
            "protocol_version": 3,
            "task_run_id": candidate.task_run_id,
            "mode": "simulated",
        }
        first = snapshot_v3_task_result(app.directory, declaration)
        cleanup_bytes = (app.directory / "cleanup.json").read_bytes()
        terminal = app.store.load_terminal_commit(candidate.task_run_id)
        assert terminal is not None
        assert first["run_exit_code"] == 0
        assert first["goal_evaluation"] == "accepted"

        rejected = replace(
            accepted, verdict="rejected", operation_id=str(uuid4()),
            source_ref="trusted-adapter:later-result",
        )
        app.controller.apply_external_validation_feedback(rejected)
        conflict = snapshot_v3_task_result(app.directory, declaration)
        assert conflict["task_run"]["status"] == "achieved"
        assert conflict["goal_evaluation"] == "unknown"
        assert conflict["recovery_status"] == "required"
        assert conflict["recovery"]["reason"] == "external_feedback_reconciliation_required"
        assert conflict["run_exit_code"] == 0
        assert conflict["run_outcome"] == first["run_outcome"]
        assert conflict["cleanup_status"] == "confirmed"

        records = app.store.list_d7_validation_feedback(
            candidate.task_run_id, candidate.candidate_id, candidate.evidence_version,
        )
        sources = tuple(
            ExternalValidationConflictSource.from_record(record)
            for record in sorted(records, key=lambda item: item.operation_id)
        )
        request = ExternalValidationCheckRequest(
            query_id=str(uuid4()),
            task_run_id=candidate.task_run_id,
            submission_operation_id=accepted.operation_id,
            candidate_id=candidate.candidate_id,
            evidence_version=candidate.evidence_version,
            checkpoint_sequence=app.state.latest_checkpoint.sequence + 1,
            conflict_sources=sources,
        )
        budget_before_checks = app.store.d4_budget_usage(candidate.task_run_id)

        class LostQueryReply:
            def check(self, bound_request):
                assert app.store.load_d7_external_validation_check(
                    bound_request.query_id,
                ) is not None
                raise TimeoutError("synthetic query reply was lost")

        with pytest.raises(TimeoutError, match="synthetic query reply"):
            app.controller.run_external_validation_check(request, LostQueryReply())
        pending = snapshot_v3_task_result(app.directory, declaration)
        assert pending["validation_accounting"] == {
            "status": "available", "logical_check_count": 1,
            "completed_count": 0, "pending_count": 1, "unknown_result_count": 0,
        }
        assert pending["external_validation_checks"][0]["status"] == "started"
        assert pending["external_validation_checks"][0]["verdict"] is None
        assert pending["run_outcome"] == first["run_outcome"]

        class UnknownChecker:
            def check(self, bound_request):
                return ExternalValidationCheckResult(
                    bound_request, "unknown", "trusted-adapter:still-pending",
                )

        unknown_check = app.controller.run_external_validation_check(request, UnknownChecker())
        unknown = snapshot_v3_task_result(app.directory, declaration)
        assert unknown["validation_accounting"] == {
            "status": "available", "logical_check_count": 1,
            "completed_count": 1, "pending_count": 0, "unknown_result_count": 1,
        }
        assert unknown["external_validation_checks"] == [{
            "query_id": request.query_id,
            "submission_operation_id": accepted.operation_id,
            "candidate_id": candidate.candidate_id,
            "evidence_version": candidate.evidence_version,
            "checkpoint_sequence": request.checkpoint_sequence,
            "status": "completed", "verdict": "unknown",
            "started_event_sequence": unknown_check.started_event_sequence,
            "result_event_sequence": unknown_check.result_event_sequence,
        }]
        assert unknown["recovery"]["reason"] == "external_feedback_reconciliation_required"

        class UnexpectedRetry:
            def check(self, _request):
                raise AssertionError("completed logical query must read back its result")

        assert app.controller.run_external_validation_check(request, UnexpectedRetry()) == unknown_check
        request = replace(
            request, query_id=str(uuid4()),
            checkpoint_sequence=app.state.latest_checkpoint.sequence + 1,
        )

        class TrustedChecker:
            def check(self, bound_request):
                return ExternalValidationCheckResult(
                    bound_request, authoritative_verdict, "trusted-adapter:current-result",
                )

        app.controller.run_external_validation_check(request, TrustedChecker())
        resolved = snapshot_v3_task_result(app.directory, declaration)
        assert app.state.task_run.status is TaskRunStatus.ACHIEVED
        assert resolved["goal_evaluation"] == authoritative_verdict
        assert resolved["recovery_status"] == "not_required"
        assert resolved["run_exit_code"] == 0
        assert resolved["run_outcome"] == first["run_outcome"]
        assert resolved["validation_accounting"] == {
            "status": "available", "logical_check_count": 2,
            "completed_count": 2, "pending_count": 0, "unknown_result_count": 1,
        }
        assert app.store.d4_budget_usage(candidate.task_run_id) == budget_before_checks
        assert (app.directory / "cleanup.json").read_bytes() == cleanup_bytes
        assert app.store.list_d7_validation_feedback(
            candidate.task_run_id, candidate.candidate_id, candidate.evidence_version,
            through_event_sequence=terminal.checkpoint.watermark_event_sequence,
        ) == (accepted,)
    finally:
        app.close()


def test_unresolved_unknown_feedback_has_its_own_recovery_reason(tmp_path):
    app, token = make_case(tmp_path, 2)
    try:
        assert asyncio.run(app.run(token)).stop_reason == "turn_completed_sandbox_retained"
        candidate = _completed_candidate(app)
        app.controller.register_answer_candidate(candidate, str(uuid4()))
        app.controller.apply_external_validation_feedback(
            ExternalValidationFeedback(
                task_run_id=candidate.task_run_id,
                candidate_id=candidate.candidate_id,
                evidence_version=candidate.evidence_version,
                verdict="unknown",
                source_ref="trusted-adapter:unknown",
                operation_id=str(uuid4()),
            )
        )

        result = snapshot_v3_task_result(
            app.directory,
            {"protocol_version": 3, "task_run_id": candidate.task_run_id, "mode": "simulated"},
        )

        assert result["answer_candidates"][0]["validation_state"] == "unknown"
        assert result["answer_candidates"][0]["feedback"][0]["feedback_kind"] == "submission"
        assert result["recovery"]["reason"] == "external_validation_feedback_unknown"
    finally:
        app.cancel()
        app.close()


@pytest.mark.parametrize("sequence", [0, -1, True, "1"])
def test_feedback_history_boundary_rejects_invalid_event_sequence(tmp_path, sequence):
    app, _ = make_case(tmp_path, 2)
    try:
        with pytest.raises(ValueError, match="positive integer"):
            app.store.list_d7_validation_feedback(
                app.state.task_run.task_run_id, str(uuid4()), "a" * 64,
                through_event_sequence=sequence,
            )
    finally:
        app.cancel()
        app.close()



def test_real_sqlite_exclusive_lock_is_busy_and_has_numeric_exception_chain(tmp_path, monkeypatch):
    import sqlite3
    from agent_adapters.web import task_result as projection
    path = tmp_path / "control.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE schema_versions (version INTEGER)")
        writer.execute("INSERT INTO schema_versions VALUES (28)")
    writer = sqlite3.connect(path)
    pauses = []
    monkeypatch.setattr(projection.time, "sleep", lambda seconds: pauses.append(seconds))
    try:
        writer.execute("BEGIN EXCLUSIVE")
        result = projection.snapshot_v3_task_result(tmp_path, dict(protocol_version=3, task_run_id=str(uuid4()), mode="simulated"))
        error = result["errors"][0]
        assert error["code"] == "state_read_busy" and error["read_stage"] == "result_projection"
        assert len(pauses) == 2
        node = next(n for n in error["exception_chain"] if n["exception_type"] == "OperationalError")
        assert node["sqlite_errorcode"] & 255 == sqlite3.SQLITE_BUSY
        assert node["source_location"]["module"] == "agent_core.sqlite_control_store"
        assert str(tmp_path) not in str(error)
    finally:
        writer.rollback()
        writer.close()


def test_state_read_exception_chain_keeps_codes_without_message_or_debug_leak(tmp_path, monkeypatch, capsys):
    from agent_adapters.web import task_result as projection
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    def read(*args):
        try:
            raise PermissionError(13, "PRIVATE_PATH_AND_KEY")
        except PermissionError as exc:
            raise D3RecoveryRequired("d8_handoff_evidence_unavailable") from exc
    monkeypatch.setenv("QINYUAN_DEBUG_ERRORS", "1")
    monkeypatch.setattr(projection, "_snapshot_v3_task_result", read)
    monkeypatch.setattr(projection.time, "sleep", lambda _: pytest.fail("permanent errors must not be retried"))
    result = projection.snapshot_v3_task_result(tmp_path, dict(protocol_version=3, task_run_id=str(uuid4()), mode="simulated"))
    error = result["errors"][0]
    assert error["exception_types"] == ["D3RecoveryRequired", "PermissionError"]
    assert error["exception_chain"][1]["errno"] == 13
    assert error["error_codes"] == ["d8_handoff_evidence_unavailable"]
    assert "PRIVATE" not in str(result) + capsys.readouterr().err
