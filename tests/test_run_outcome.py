from dataclasses import FrozenInstanceError

import pytest

from agent_core.run_outcome import (
    D7OperationExit,
    D7OperationResult,
    D7RunOutcome,
    D7RunOutcomeState,
    map_operation_exit,
    map_run_outcome,
)


def _run_state(
    *,
    task_run_status="running",
    goal_evaluation="not_evaluated",
    cleanup_state="not_required",
    unresolved_operation_unknown=False,
    recovery_required=False,
):
    return D7RunOutcomeState(
        task_run_status=task_run_status,
        goal_evaluation=goal_evaluation,
        cleanup_state=cleanup_state,
        unresolved_operation_unknown=unresolved_operation_unknown,
        recovery_required=recovery_required,
    )


@pytest.mark.parametrize(
    ("state", "expected_code", "expected_reason"),
    [
        (
            _run_state(
                task_run_status="achieved",
                goal_evaluation="accepted",
                cleanup_state="confirmed",
            ),
            0,
            "accepted_goal_achieved",
        ),
        (_run_state(), 4, "unfinished"),
        (_run_state(task_run_status="awaiting_confirmation"), 4, "unfinished"),
        (_run_state(task_run_status="assumed_achieved", cleanup_state="confirmed"), 0, "assumed_goal_ended"),
        (
            _run_state(task_run_status="awaiting_external_feedback"),
            4,
            "unfinished",
        ),
        (
            _run_state(
                task_run_status="achieved",
                goal_evaluation="rejected",
                cleanup_state="confirmed",
            ),
            5,
            "goal_taskrun_mismatch",
        ),
        (
            _run_state(
                task_run_status="achieved",
                goal_evaluation="unknown",
                cleanup_state="confirmed",
            ),
            5,
            "goal_taskrun_mismatch",
        ),
        (
            _run_state(
                task_run_status="achieved",
                goal_evaluation="not_evaluated",
                cleanup_state="confirmed",
            ),
            5,
            "goal_taskrun_mismatch",
        ),
        (
            _run_state(
                task_run_status="cancelled",
                cleanup_state="confirmed",
            ),
            130,
            "cancelled",
        ),
        (
            _run_state(
                task_run_status="deadline_exceeded",
                cleanup_state="confirmed",
            ),
            3,
            "deadline_exceeded",
        ),
        (
            _run_state(task_run_status="failed", cleanup_state="confirmed"),
            3,
            "terminal_error",
        ),
        (
            _run_state(task_run_status="partial", cleanup_state="confirmed"),
            4,
            "unfinished",
        ),
    ],
)
def test_run_exit_code_requires_independent_goal_and_confirmed_cleanup(
    state, expected_code, expected_reason
):
    result = map_run_outcome(state)

    assert result.run_exit_code == expected_code
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (
            _run_state(
                task_run_status="cancelled",
                goal_evaluation="accepted",
                cleanup_state="confirmed",
                unresolved_operation_unknown=True,
            ),
            "unknown_operation",
        ),
        (
            _run_state(
                task_run_status="deadline_exceeded",
                cleanup_state="confirmed",
                recovery_required=True,
            ),
            "recovery_required",
        ),
        (
            _run_state(
                task_run_status="running",
                cleanup_state="unconfirmed",
            ),
            "cleanup_unconfirmed",
        ),
        (
            _run_state(
                task_run_status="achieved",
                goal_evaluation="accepted",
                cleanup_state="not_required",
            ),
            "terminal_cleanup_not_required",
        ),
        (
            _run_state(
                task_run_status="cancelled",
                cleanup_state="not_required",
            ),
            "terminal_cleanup_not_required",
        ),
    ],
)
def test_unknown_recovery_and_terminal_cleanup_gaps_take_precedence(
    state, expected_reason
):
    result = map_run_outcome(state)

    assert result.run_exit_code == 5
    assert result.reason == expected_reason


def test_unknown_goal_evaluation_alone_does_not_override_an_unfinished_run():
    result = map_run_outcome(_run_state(goal_evaluation="unknown"))

    assert result.run_exit_code == 4
    assert result.reason == "unfinished"


@pytest.mark.parametrize(
    ("operation", "state", "expected_code"),
    [
        ("status", "completed", 0),
        ("replay", "completed", 0),
        ("evidence", "completed", 0),
        ("cancel", "completed", 0),
        ("cleanup", "completed", 0),
        ("cancel", "refused", 2),
        ("cleanup", "refused", 2),
        ("cancel", "failed", 3),
        ("cleanup", "failed", 3),
        ("cancel", "paused", 4),
        ("cleanup", "paused", 4),
        ("cancel", "unknown", 5),
        ("cleanup", "unknown", 5),
        ("status", "failed", 3),
        ("evidence", "unknown", 5),
        ("run", "refused", 2),
        ("resume", "refused", 2),
        ("run", "failed", 3),
        ("resume", "paused", 4),
        ("resume", "unknown", 5),
    ],
)
def test_operation_exit_code_describes_this_operation(operation, state, expected_code):
    result = map_operation_exit(D7OperationResult(operation, state))

    assert result.exit_code == expected_code
    assert result.run_exit_code is None


@pytest.mark.parametrize("run_exit_code", [0, 3, 4, 5, 130])
def test_run_and_resume_return_run_result_as_operation_code(run_exit_code):
    for operation in ("run", "resume"):
        result = map_operation_exit(
            D7OperationResult(operation, "completed", run_exit_code)
        )

        assert result.to_dict() == {
            "exit_code": run_exit_code,
            "run_exit_code": run_exit_code,
        }


def test_query_failure_does_not_rewrite_persisted_run_result():
    result = map_operation_exit(
        D7OperationResult("status", "failed", run_exit_code=130)
    )

    assert result.to_dict() == {"exit_code": 3, "run_exit_code": 130}


def test_export_failure_changes_operation_code_but_preserves_run_result():
    result = map_operation_exit(
        D7OperationResult("run", "export_failed", run_exit_code=0)
    )

    assert result.to_dict() == {"exit_code": 3, "run_exit_code": 0}


@pytest.mark.parametrize(
    ("operation", "state", "run_exit_code"),
    [
        ("run", "completed", None),
        ("status", "completed", 2),
        ("cancel", "export_failed", 0),
        ("export", "export_failed", None),
    ],
)
def test_operation_dto_rejects_invalid_code_combinations(
    operation, state, run_exit_code
):
    with pytest.raises(ValueError):
        D7OperationResult(operation, state, run_exit_code)


def test_run_outcome_state_is_immutable():
    state = _run_state()

    with pytest.raises(FrozenInstanceError):
        state.cleanup_state = "confirmed"


@pytest.mark.parametrize(
    "construct",
    [
        lambda: D7RunOutcome(2, "accepted_goal_achieved"),
        lambda: D7RunOutcome(0, "unrecognized"),
        lambda: D7OperationExit(2, 2),
        lambda: D7OperationExit(True, None),
    ],
)
def test_public_exit_dtos_reject_non_contract_values(construct):
    with pytest.raises(ValueError):
        construct()
