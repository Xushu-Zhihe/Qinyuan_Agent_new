"""Test assembly proves trusted rejection advances the same D3 TaskRun."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from d7_fake_judge_fixture import FakeJudge
from test_d7_candidate_evidence import _TestOnlyProtection
from test_d3_chain import Model, ReviewModel
from test_d5_chain import RefinementModel
from test_d7_turn_budget import make_case

from agent_adapters import d7_candidate_evidence
from agent_adapters.d7_candidate_evidence import D7CandidateEvidenceService
from agent_adapters.multi_turn_application import D3Application
from agent_core.goal_feedback import ExternalValidationFeedback
from agent_core.runtime import TaskRunStatus


def _reopen_app(app, *, inner_model=None):
    return D3Application(
        app.directory,
        config=app.config,
        sandbox=app.sandbox,
        import_sandbox=app.sandbox,
        workspace_sandbox=app.sandbox,
        command_sandbox=app.sandbox,
        outer_model=Model(app.config.outer_role),
        inner_model=inner_model or Model(app.config.inner_role),
        review_model=ReviewModel(app.config.outer_role),
        refinement_model=RefinementModel(app.config.outer_role),
        sensitive_config={},
        clock=app.clock,
    )


def test_fake_judge_rejection_automatically_executes_second_turn_and_accepts(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection,
    )
    app, token = make_case(tmp_path, 2)
    candidates = []
    try:
        task_run_id = app.state.task_run.task_run_id
        service = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage", "fake-judge-test-assembly"),
        )
        app.inner.adapter.adapter.candidate_values = ["fake", "correct"]
        reader = service.read_port("fake-judge-test-assembly")
        first_run = asyncio.run(app.run(token))
        assert first_run.stop_reason == "turn_completed_sandbox_retained"
        first = asyncio.run(app.propose_d7_candidate(token, service))
        candidates.append(first)
        judge = FakeJudge()

        rejected = judge.evaluate(first, lambda item: reader.read(item.candidate_id))
        # Simulate a crash after the durable Controller feedback commit but
        # before the application has registered the continuation Turn.
        app.controller.apply_external_validation_feedback(rejected)
        continued = asyncio.run(
            app.apply_trusted_d7_feedback(rejected, token, service)
        )
        second = continued.continuation_candidate
        assert second is not None
        candidates.append(second)
        assert continued.feedback_state == "rejected"
        assert second.task_run_id == task_run_id
        assert len(app.state.turns) == 2
        assert first.turn_id != second.turn_id
        assert first.execution_id != second.execution_id
        assert second.turn_id == app.state.turns[-1].turn_id

        calls_before_replay = len(app.inner.adapter.adapter.calls)
        replay = asyncio.run(app.apply_trusted_d7_feedback(rejected, token, service))
        assert replay.continuation_candidate == second
        assert len(app.state.turns) == 2
        assert len(app.inner.adapter.adapter.calls) == calls_before_replay

        accepted = judge.evaluate(second, lambda item: reader.read(item.candidate_id))
        completed = asyncio.run(
            app.apply_trusted_d7_feedback(accepted, token, service)
        )
        assert completed.feedback_state == "accepted"
        assert completed.continuation_candidate is None
        assert len(app.state.turns) == 2
        assert app.state.task_run.status is TaskRunStatus.ACHIEVED
        assert app.store.list_d7_answer_candidates(task_run_id) == tuple(candidates)
        assert [
            app.store.list_d7_validation_feedback(
                task_run_id, candidate.candidate_id, candidate.evidence_version,
            )[0].verdict
            for candidate in candidates
        ] == ["rejected", "accepted"]
        app.controller.cleanup_sandbox()
        assert app.sandbox.destroyed
    finally:
        if app.state.task_run.status is TaskRunStatus.RUNNING:
            app.cancel()
        elif app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:
            app.controller.close()
        else:
            app.controller.cleanup_sandbox()
        app.close()


def test_reopened_application_replays_rejected_feedback_and_continues(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection,
    )
    app, token = make_case(tmp_path, 2)
    reopened = None
    old_store_closed = False
    try:
        task_run_id = app.state.task_run.task_run_id
        first_service = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage", "fake-judge-test-assembly"),
        )
        asyncio.run(app.run(token))
        app.inner.adapter.adapter.candidate_values = ["fake"]
        first = asyncio.run(app.propose_d7_candidate(token, first_service))
        reader = first_service.read_port("fake-judge-test-assembly")
        rejected = FakeJudge().evaluate(
            first, lambda item: reader.read(item.candidate_id)
        )

        # A process loss after the feedback commit leaves the TaskRun running;
        # close the SQLite handle as a crash would, without a terminal commit.
        app.controller.apply_external_validation_feedback(rejected)
        app.store.close()
        old_store_closed = True

        inner_model = Model(app.config.inner_role)
        inner_model.candidate_values = ["correct"]
        reopened = _reopen_app(app, inner_model=inner_model)
        service = D7CandidateEvidenceService(
            reopened.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage", "fake-judge-test-assembly"),
        )
        assert reopened.store.load_d3_run_config(task_run_id) == app.config

        continued = asyncio.run(
            reopened.apply_trusted_d7_feedback(rejected, token, service)
        )
        second = continued.continuation_candidate
        assert continued.feedback_state == "rejected"
        assert second is not None
        assert second.turn_id != first.turn_id
        assert len(reopened.state.turns) == 2

        calls_before_replay = len(inner_model.calls)
        replay = asyncio.run(
            reopened.apply_trusted_d7_feedback(rejected, token, service)
        )
        assert replay.continuation_candidate == second
        assert len(reopened.state.turns) == 2
        assert len(inner_model.calls) == calls_before_replay

        accepted = FakeJudge().evaluate(
            second,
            lambda item: service.read_port("fake-judge-test-assembly").read(
                item.candidate_id
            ),
        )
        completed = asyncio.run(
            reopened.apply_trusted_d7_feedback(accepted, token, service)
        )
        assert completed.feedback_state == "accepted"
        assert reopened.state.task_run.status is TaskRunStatus.ACHIEVED
        assert app.config == reopened.store.load_d3_run_config(task_run_id)
    finally:
        target = reopened or (app if not old_store_closed else None)
        if target is not None:
            if target.state.task_run.status in {
                TaskRunStatus.RUNNING,
                TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
            }:
                target.cancel()
            elif not target.sandbox.destroyed:
                target.controller.cleanup_sandbox()
            if reopened is not None:
                reopened.close()
            else:
                app.close()


def test_unknown_feedback_does_not_register_or_run_a_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection,
    )
    app, token = make_case(tmp_path, 2)
    try:
        task_run_id = app.state.task_run.task_run_id
        service = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage",),
        )
        asyncio.run(app.run(token))
        candidate = asyncio.run(app.propose_d7_candidate(token, service))
        feedback = ExternalValidationFeedback(
            task_run_id=task_run_id,
            candidate_id=candidate.candidate_id,
            evidence_version=candidate.evidence_version,
            verdict="unknown",
            source_ref="fake-judge-test-assembly",
            operation_id=str(uuid4()),
        )

        applied = asyncio.run(app.apply_trusted_d7_feedback(feedback, token, service))

        assert applied.feedback_state == "unknown"
        assert applied.continuation_candidate is None
        assert len(app.state.turns) == 1
        assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
    finally:
        if app.state.task_run.status in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
        }:
            app.cancel()
        else:
            app.controller.cleanup_sandbox()
        app.close()


def test_opposite_feedback_conflict_does_not_reopen_or_continue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection,
    )
    app, token = make_case(tmp_path, 2)
    try:
        task_run_id = app.state.task_run.task_run_id
        service = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage",),
        )
        asyncio.run(app.run(token))
        candidate = asyncio.run(app.propose_d7_candidate(token, service))
        accepted = ExternalValidationFeedback(
            task_run_id,
            candidate.candidate_id,
            candidate.evidence_version,
            "accepted",
            "fake-judge-test-assembly",
            str(uuid4()),
        )
        first = asyncio.run(app.apply_trusted_d7_feedback(accepted, token, service))
        assert first.feedback_state == "accepted"
        assert app.state.task_run.status is TaskRunStatus.ACHIEVED

        rejected = ExternalValidationFeedback(
            task_run_id,
            candidate.candidate_id,
            candidate.evidence_version,
            "rejected",
            "conflicting-test-assembly",
            str(uuid4()),
        )
        conflict = asyncio.run(
            app.apply_trusted_d7_feedback(rejected, token, service)
        )

        assert conflict.feedback_state == "conflict"
        assert conflict.continuation_candidate is None
        assert len(app.state.turns) == 1
        assert app.state.task_run.status is TaskRunStatus.ACHIEVED
    finally:
        if app.state.task_run.status in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
        }:
            app.cancel()
        else:
            app.controller.cleanup_sandbox()
        app.close()


@pytest.mark.parametrize("injection_point", ["continuation_entry", "outer_proposal"])
def test_conflict_after_rejection_blocks_continuation_dispatch(
    tmp_path, monkeypatch, injection_point,
):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection,
    )
    app, token = make_case(tmp_path, 2)
    try:
        task_run_id = app.state.task_run.task_run_id
        service = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage", "fake-judge-test-assembly"),
        )
        asyncio.run(app.run(token))
        candidate = asyncio.run(app.propose_d7_candidate(token, service))
        rejected = ExternalValidationFeedback(
            task_run_id,
            candidate.candidate_id,
            candidate.evidence_version,
            "rejected",
            "fake-judge-test-assembly",
            str(uuid4()),
        )
        opposite = replace(
            rejected,
            verdict="accepted",
            source_ref="conflicting-test-assembly",
            operation_id=str(uuid4()),
        )
        outer_calls_before = len(app.outer.adapter.adapter.calls)
        executions_before = tuple(app.state.executions)

        if injection_point == "continuation_entry":
            register_next = app.register_next_d7_turn

            async def inject_before_continuation(token):
                app.controller.apply_external_validation_feedback(opposite)
                return await register_next(token)

            monkeypatch.setattr(app, "register_next_d7_turn", inject_before_continuation)
        else:
            outer_proposal = app._outer_proposal

            async def inject_before_proposal(*args, **kwargs):
                app.controller.apply_external_validation_feedback(opposite)
                return await outer_proposal(*args, **kwargs)

            monkeypatch.setattr(app, "_outer_proposal", inject_before_proposal)

        result = asyncio.run(
            app.apply_trusted_d7_feedback(rejected, token, service)
        )

        assert result.feedback_state == "conflict"
        assert result.continuation_candidate is None
        assert len(app.state.turns) == 1
        assert tuple(app.state.executions) == executions_before
        assert len(app.outer.adapter.adapter.calls) == outer_calls_before
        feedback = app.store.list_d7_validation_feedback(
            task_run_id, candidate.candidate_id, candidate.evidence_version,
        )
        assert tuple(item.verdict for item in feedback) == ("rejected", "accepted")
    finally:
        if app.state.task_run.status in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
        }:
            app.cancel()
        else:
            app.controller.cleanup_sandbox()
        app.close()


def test_cancel_while_waiting_for_external_feedback_cleans_sandbox(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        d7_candidate_evidence, "WindowsEvidenceProtection", _TestOnlyProtection,
    )
    app, token = make_case(tmp_path, 2)
    try:
        task_run_id = app.state.task_run.task_run_id
        service = D7CandidateEvidenceService(
            app.directory,
            task_run_id,
            trusted_adapter_ids=("d7_candidate_stage",),
        )
        asyncio.run(app.run(token))
        asyncio.run(app.propose_d7_candidate(token, service))
        assert app.state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK

        cancelled = app.cancel(task_run_id)

        assert app.state.task_run.status is TaskRunStatus.CANCELLED
        assert cancelled.task_run_status == TaskRunStatus.CANCELLED.value
        assert cancelled.sandbox_status == "cleaned"
        assert app.sandbox.destroyed
    finally:
        if app.state.task_run.status in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
        }:
            app.cancel()
        elif not app.sandbox.destroyed:
            app.controller.cleanup_sandbox()
        app.close()
