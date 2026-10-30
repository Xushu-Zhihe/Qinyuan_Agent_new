"""U6 trusted candidate and human/external feedback; controlled model/tool IO."""

import asyncio
from uuid import uuid4

import pytest

from test_stage_application import case
from agent_core.goal_feedback import ExternalValidationFeedback
from agent_core.runtime import TaskRunStatus


def prepare(case, mode="human"):
    from agent_adapters.stage_application import StageApplication
    from agent_adapters.stage_goal import StageGoal
    # The ordinary fixture starts its task before this test; build a separate explicitly configured task.
    task = case.app.state.task_run.request
    app = StageApplication(case.path / "goal-task", **case.args, goal_validation=mode)
    app.start(task)
    asyncio.run(case.app.close())
    case.app = app
    case.tools.task_id = app.task_id
    assert isinstance(app.goal, StageGoal)
    previous = case.models.inner.transform
    def candidate(request, raw):
        if request.stage == "stage_continuation":
            sources = [i.ref for i in request.context.history if i.record_type == "progress" and i.identity == "tool_observation_not_fact"]
            return {**raw, "candidate": "observed", "source_ref": sources[-1]}
        return previous(request, raw)
    case.models.inner.transform = candidate
    return asyncio.run(app.run(case.token))


@pytest.mark.parametrize("native", [False, True])
def test_human_candidate_is_protected_and_manual_success_is_trusted_feedback(case, native):
    case.models.inner.native = case.models.outer.native = native
    prepare(case)
    candidate, = case.app.store.list_d7_answer_candidates(case.app.task_id)
    task = case.app.state.task_run
    assert task.status is TaskRunStatus.AWAITING_CONFIRMATION
    assert task.confirmation_deadline == case.now.value + 300
    assert not case.app.store.list_d7_validation_feedback(case.app.task_id, candidate.candidate_id, candidate.evidence_version)
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
    protected = D7CandidateEvidenceService(case.app.directory, case.app.task_id, trusted_adapter_ids=("human",), create=False)
    assert protected.read_port("human").read(candidate.candidate_id) == "observed"
    operation = str(uuid4())
    case.app.goal.decide(candidate.candidate_id, "accept", operation)
    case.app.goal.decide(candidate.candidate_id, "accept", operation)
    assert case.app.state.task_run.status is TaskRunStatus.ACHIEVED
    records = case.app.store.list_d7_validation_feedback(case.app.task_id, candidate.candidate_id, candidate.evidence_version)
    assert len(records) == 1 and records[0].source_ref.startswith("human:")


def test_rejected_candidate_returns_to_original_turn_planning_and_late_decision_is_closed(case):
    prepare(case)
    candidate, = case.app.store.list_d7_answer_candidates(case.app.task_id)
    original = case.app.store.list_stage_turns(case.app.task_id)[0]
    case.app.goal.decide(candidate.candidate_id, "continue", str(uuid4()))
    before = len(case.models.inner.calls)
    previous = case.models.inner.transform
    def exhausted(request, raw):
        if request.stage == "stage_continuation":
            assert request.context.session.owner_id == original.turn_id
            assert request.context.feedback_refs and request.context.turn.achievement == original.achievement
            return dict(kind="exhausted", reason="no further option", support_refs=list(request.context.feedback_refs))
        return previous(request, raw)
    case.models.inner.transform = exhausted
    report = asyncio.run(case.app.run(case.token))
    assert report.reason == "stage.exhausted"
    assert len(case.models.inner.calls) == before + 1
    assert len(case.tools.calls) == 1
    with pytest.raises(Exception, match="human_confirmation_closed"):
        case.app.goal.decide(candidate.candidate_id, "accept", str(uuid4()))


def test_wait_restart_preserves_original_deadline_and_timeout_operation_is_idempotent(case):
    prepare(case)
    from agent_adapters.stage_application import StageApplication
    task_id = case.app.task_id
    directory = case.app.directory
    deadline = case.app.state.task_run.confirmation_deadline
    asyncio.run(case.app.close())
    case.now.value += 300
    case.app = StageApplication(directory, **case.args, goal_validation="human")
    case.app.bind(task_id)
    assert case.app.state.task_run.confirmation_deadline == deadline
    operation = str(uuid4())
    case.app.controller.advance_time(operation)
    case.app.controller.advance_time(operation)
    assert case.app.state.task_run.status is TaskRunStatus.INCONCLUSIVE


def test_five_minute_timeout_is_inconclusive_without_resetting_wait(case):
    prepare(case)
    deadline = case.app.state.task_run.confirmation_deadline
    case.now.value += 299
    asyncio.run(case.app.run(case.token))
    assert case.app.state.task_run.confirmation_deadline == deadline
    assert case.app.state.task_run.status is TaskRunStatus.AWAITING_CONFIRMATION
    case.now.value += 1
    asyncio.run(case.app.run(case.token))
    assert case.app.state.task_run.status is TaskRunStatus.INCONCLUSIVE
    assert case.app.state.task_run.failure_reason == "human_confirmation_timeout"


@pytest.mark.parametrize("verdict", ["rejected", "partial", "unknown"])
def test_external_feedback_retains_historical_stage_result(case, verdict):
    prepare(case, "external")
    candidate, = case.app.store.list_d7_answer_candidates(case.app.task_id)
    turn, = case.app.store.list_stage_turns(case.app.task_id)
    case.app.controller.apply_external_validation_feedback(ExternalValidationFeedback(case.app.task_id, candidate.candidate_id,
        candidate.evidence_version, verdict, "controlled-verifier", str(uuid4())))
    expected = TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK if verdict == "unknown" else TaskRunStatus.RUNNING
    assert case.app.state.task_run.status is expected
    assert case.app.store.load_turn(case.app.task_id, turn.turn_id).achievement == turn.achievement


def test_human_can_end_without_claiming_success(case):
    prepare(case)
    candidate, = case.app.store.list_d7_answer_candidates(case.app.task_id)
    operation = str(uuid4())
    case.app.goal.decide(candidate.candidate_id, "end", operation)
    case.app.goal.decide(candidate.candidate_id, "end", operation)
    assert case.app.state.task_run.status is TaskRunStatus.INCONCLUSIVE
    assert case.app.state.task_run.failure_reason == "human_unverified_end"
    from agent_core.control_store import IdempotencyConflictError
    with pytest.raises(IdempotencyConflictError):
        case.app.controller.advance_time(operation)


@pytest.mark.parametrize("verdict", ["accepted", "rejected", "partial", "unknown"])
def test_original_competition_submit_uses_stage_protected_candidate_once(case, verdict):
    prepare(case, "external")
    from agent_adapters.competition_submission import submit
    frozen = dict(request_id=str(uuid4()), profile=dict(id=str(uuid4()), adapter="benchmark", base_url="http://example.test", timeout_seconds=2),
        question=dict(remote_ref="controlled-question"), platform_deadline_at=None)
    sent = []
    def send(profile, credential, question, value):
        sent.append(value)
        return dict(verdict=verdict, correct_flag_count=1, total_flag_count=1 if verdict == "accepted" else 2)
    operation = str(uuid4())
    first = submit(case.app, frozen, "controlled-not-a-credential", operation, send=send)
    assert first["state"] == verdict
    assert submit(case.app, frozen, "controlled-not-a-credential", operation, send=send) == first
    assert sent == ["observed"]
    if verdict == "unknown":
        submit(case.app, frozen, "controlled-not-a-credential", str(uuid4()), send=send)
        assert sent == ["observed"]


def test_unknown_validation_only_checks_original_submission_and_never_dispatches_models(case):
    prepare(case, "external")
    from agent_core.answer_candidates import check_request
    from agent_core.goal_feedback import ExternalValidationCheckResult
    from types import SimpleNamespace
    candidate, = case.app.store.list_d7_answer_candidates(case.app.task_id)
    submission = str(uuid4())
    case.app.controller.apply_external_validation_feedback(ExternalValidationFeedback(case.app.task_id, candidate.candidate_id,
        candidate.evidence_version, "unknown", "controlled-verifier", submission))
    before = len(case.models.inner.calls), len(case.tools.calls)
    asyncio.run(case.app.run(case.token))
    assert (len(case.models.inner.calls), len(case.tools.calls)) == before
    selection = dict(query_id=str(uuid4()), candidate_id=candidate.candidate_id,
        evidence_version=candidate.evidence_version, submission_operation_id=submission)
    request = check_request(case.app.store, case.app.task_id, selection, case.app.state.latest_checkpoint.sequence + 1)
    calls = []
    def check(request):
        calls.append(request)
        return ExternalValidationCheckResult(request, "accepted", "controlled-read-only-query")
    checker = SimpleNamespace(check=check)
    case.app.controller.run_external_validation_check(request, checker)
    case.app.controller.run_external_validation_check(request, checker)
    assert calls == [request]
    assert case.app.state.task_run.status is TaskRunStatus.ACHIEVED
    assert (len(case.models.inner.calls), len(case.tools.calls)) == before


def test_candidate_schema_migration_failure_preserves_registry_and_feedback(case, monkeypatch):
    prepare(case)
    import sqlite3
    from agent_core import stage_storage
    from agent_core.control_store import StorageError
    from agent_core.sqlite_control_store import SqliteControlStore
    from agent_adapters.stage_application import StageApplication
    directory, task_id = case.app.directory, case.app.task_id
    asyncio.run(case.app.close())
    path = directory / "control.sqlite"
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT * FROM d7_candidate_registry").fetchall()
        db.execute("DROP TABLE stage_plan_bindings")
        db.execute("DROP TABLE stage_branches")
        db.execute("DROP TABLE stage_review_queue")
        db.execute("DELETE FROM schema_versions WHERE version>=34")
    schema = stage_storage.CANDIDATE_SCHEMA
    monkeypatch.setattr(stage_storage, "CANDIDATE_SCHEMA", schema + "\nBROKEN MIGRATION;")
    with pytest.raises(StorageError, match="schema migration failed"):
        SqliteControlStore.open(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM d7_candidate_registry").fetchall() == before
        assert db.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 33
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    monkeypatch.setattr(stage_storage, "CANDIDATE_SCHEMA", schema)
    case.app = StageApplication(directory, **case.args, goal_validation="human")
    case.app.bind(task_id)
    assert case.app.store.list_d7_answer_candidates(task_id)[0].candidate_id == before[0][0]
