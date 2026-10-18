"""D4 terminal lifecycle and explicit cleanup recovery acceptance."""

from __future__ import annotations

import asyncio

import pytest
from test_chain_operation import close_scenario, make_d4_scenario, reopen_scenario

from agent_core.controller import CommittedStateReloadError
from agent_core.dual_llm_contracts import D3RecoveryRequired


@pytest.fixture
def d4_lifecycle_scenario(tmp_path):
    case = make_d4_scenario(tmp_path)
    try:
        yield case
    finally:
        close_scenario(case)


def _events(app, task_run_id):
    return app.store.list_d4_audit_events(task_run_id)


def test_d4_cancel_commits_and_reloads_before_destroy(d4_lifecycle_scenario):
    app, sandbox, *_ = d4_lifecycle_scenario
    task_run_id = app.state.task_run.task_run_id

    result = app.cancel(task_run_id)

    assert result.task_run_status == "cancelled"
    assert result.stop_reason == "task_run_cancelled"
    assert result.sandbox_status == "cleaned"
    assert not result.recovery_required
    assert sandbox.destroyed

    events = _events(app, task_run_id)
    cancellation_index = max(
        index
        for index, event in enumerate(events)
        if event.category == "cancellation_requested"
    )
    lifecycle = [
        event.category
        for event in events[cancellation_index + 1 :]
        if event.category
        in {
            "control_commit_started",
            "control_commit_committed",
            "control_reload_confirmed",
            "cleanup_started",
            "cleanup_completed",
        }
    ]
    assert lifecycle == [
        "control_commit_started",
        "control_commit_committed",
        "control_reload_confirmed",
        "cleanup_started",
        "cleanup_completed",
    ]


def test_d4_failed_terminal_reload_never_starts_cleanup(
    d4_lifecycle_scenario, monkeypatch
):
    app, sandbox, *_ = d4_lifecycle_scenario
    task_run_id = app.state.task_run.task_run_id
    repository = app.controller._persistent.repository
    load_control_state = repository.load_control_state

    def fail_reload(_task_run_id):
        if app.store.load_task_run(_task_run_id).status.value == "cancelled":
            raise RuntimeError("control snapshot is unavailable")
        return load_control_state(_task_run_id)

    monkeypatch.setattr(repository, "load_control_state", fail_reload)
    with pytest.raises(CommittedStateReloadError) as raised:
        app.cancel(task_run_id)
    assert raised.value.committed
    assert app.controller._cache_invalid
    assert app.store.load_task_run(task_run_id).status.value == "cancelled"
    assert not sandbox.destroyed
    assert not any(
        event.category == "cleanup_started" for event in _events(app, task_run_id)
    )
    with pytest.raises(D3RecoveryRequired, match="d4_cleanup_reload_failed"):
        app.resume_cleanup(task_run_id)
    assert not sandbox.destroyed
    assert not any(
        event.category == "cleanup_started" for event in _events(app, task_run_id)
    )

    monkeypatch.setattr(repository, "load_control_state", load_control_state)
    cleanup = app.resume_cleanup(task_run_id)
    assert cleanup.sandbox_status == "cleaned"
    assert not cleanup.recovery_required
    assert sandbox.destroyed


def test_d4_reconciled_control_commit_replays_original_mutation_without_audit_conflict(
    d4_lifecycle_scenario, monkeypatch
):
    app, sandbox, _, _, _, clock, _ = d4_lifecycle_scenario
    task_run_id = app.state.task_run.task_run_id
    commit = app.store.commit
    insert_audit = app.store._insert_d4_audit_event
    committed_mutations = []
    committed_results = []

    def capture_mutation(mutation):
        committed_mutations.append(mutation)
        result = commit(mutation)
        committed_results.append(result)
        return result

    def omit_first_commit_completion(event):
        if (
            committed_mutations
            and event.related_ref == committed_mutations[0].mutation_id
            and event.category == "control_commit_committed"
        ):
            return event
        return insert_audit(event)

    with monkeypatch.context() as patch:
        patch.setattr(app.store, "commit", capture_mutation)
        patch.setattr(app.store, "_insert_d4_audit_event", omit_first_commit_completion)
        cancelled = app.cancel(task_run_id)

    assert cancelled.task_run_status == "cancelled"
    assert sandbox.destroyed
    original_mutation = committed_mutations[0]
    original_result = committed_results[0]
    mutation_events = [
        event
        for event in _events(app, task_run_id)
        if event.related_ref == original_mutation.mutation_id
    ]
    assert any(event.category == "control_commit_started" for event in mutation_events)
    assert not any(
        event.category == "control_commit_committed" for event in mutation_events
    )

    clock.now += 1
    app = reopen_scenario(d4_lifecycle_scenario)
    app.store.reconcile_d4_control_commit_audit(task_run_id)
    reconciled_events = _events(app, task_run_id)
    reconciled = next(
        event
        for event in reconciled_events
        if event.related_ref == original_mutation.mutation_id
        and event.category == "control_commit_committed"
    )
    assert reconciled.outcome == "known"
    assert reconciled.event_id == app.store._d4_audit_event_id(
        task_run_id, original_mutation.mutation_id, "control_commit_committed", "known"
    )
    assert reconciled.occurred_at != original_result.checkpoint.created_at
    usage = app.store.d4_budget_usage(task_run_id)

    replay = app.store.commit(original_mutation)

    assert replay.replayed
    assert replay.event == original_result.event
    assert replay.checkpoint == original_result.checkpoint
    assert _events(app, task_run_id) == reconciled_events
    assert app.store.d4_budget_usage(task_run_id) == usage


@pytest.mark.parametrize(
    (
        "loss_boundary",
        "restart",
        "presence",
        "expected_status",
        "expected_recovery",
        "expected_destroy_count",
    ),
    [
        ("destroy", True, "absent", "cleaned", False, 1),
        ("destroy", True, "present", "cleaned", False, 2),
        ("destroy", True, "unknown", "unknown", True, 1),
        ("interrupt", False, "absent", "cleaned", False, 1),
        ("interrupt", False, "present", "cleaned", False, 2),
        ("interrupt", False, "unknown", "unknown", True, 1),
    ],
)
def test_d4_cleanup_unknown_requires_explicit_presence_reconciliation(
    d4_lifecycle_scenario,
    monkeypatch,
    loss_boundary,
    restart,
    presence,
    expected_status,
    expected_recovery,
    expected_destroy_count,
):
    app, sandbox, *_ = d4_lifecycle_scenario
    task_run_id = app.state.task_run.task_run_id
    destroy_attempts = []
    interrupt_attempts = []

    def destroy(sandbox_id):
        destroy_attempts.append(sandbox_id)
        if loss_boundary == "destroy" and len(destroy_attempts) == 1:
            raise TimeoutError("destroy outcome was lost")
        sandbox.destroyed = True

    def interrupt(sandbox_id):
        if loss_boundary == "interrupt":
            interrupt_attempts.append(sandbox_id)
        if loss_boundary == "interrupt" and len(interrupt_attempts) == 1:
            raise TimeoutError("interrupt result was lost")

    monkeypatch.setattr(sandbox, "destroy", destroy)
    monkeypatch.setattr(sandbox, "interrupt", interrupt)
    presence_calls = []

    def inspect_presence(inspected_task_run_id, sandbox_id):
        presence_calls.append((inspected_task_run_id, sandbox_id))
        if presence == "absent":
            sandbox.destroyed = True
        elif presence == "present":
            sandbox.destroyed = False
        return presence

    monkeypatch.setattr(sandbox, "inspect_presence", inspect_presence, raising=False)

    cancelled = app.cancel(task_run_id)

    assert cancelled.task_run_status == "cancelled"
    assert cancelled.sandbox_status == "unknown"
    assert cancelled.recovery_required
    assert len(destroy_attempts) == 1
    assert not presence_calls

    repeated_cancel = app.cancel(task_run_id)
    assert repeated_cancel.task_run_status == "cancelled"
    assert repeated_cancel.sandbox_status == "unknown"
    assert repeated_cancel.recovery_required
    assert len(destroy_attempts) == 1
    assert not presence_calls

    usage_before_resume = app.store.d4_budget_usage(task_run_id)
    initial_events = _events(app, task_run_id)
    initial_cleanup_start = next(
        event for event in initial_events if event.category == "cleanup_started"
    )
    initial_cleanup_failure = next(
        event for event in initial_events if event.category == "cleanup_failed"
    )
    assert initial_cleanup_failure.outcome == "unknown"
    assert initial_cleanup_failure.related_ref == initial_cleanup_start.related_ref

    if restart:
        app = reopen_scenario(d4_lifecycle_scenario)
    resumed = asyncio.run(app.resume(task_run_id, d4_lifecycle_scenario[6]))
    assert resumed.stop_reason == "task_run_already_terminal"
    assert resumed.task_run_status == "cancelled"
    assert resumed.sandbox_status == "unknown"
    assert resumed.recovery_required
    assert len(destroy_attempts) == 1
    assert not presence_calls

    recovered = app.resume_cleanup(task_run_id)

    assert recovered.task_run_status == "cancelled"
    assert recovered.sandbox_status == expected_status
    assert recovered.recovery_required is expected_recovery
    assert len(destroy_attempts) == expected_destroy_count
    assert len(presence_calls) == 1
    assert presence_calls[0] == (task_run_id, app.state.task_run.sandbox_id)
    assert app.store.d4_budget_usage(task_run_id) == usage_before_resume

    events = _events(app, task_run_id)
    presence_event = next(
        event for event in events if event.category == "resource_presence_verified"
    )
    assert presence_event.outcome == presence
    assert presence_event.related_ref == initial_cleanup_start.related_ref
    cleanup_completions = [
        event for event in events if event.category == "cleanup_completed"
    ]
    assert bool(cleanup_completions) is (presence != "unknown")
    if presence == "absent":
        assert cleanup_completions[0].related_ref == initial_cleanup_start.related_ref
    elif presence == "present":
        assert len(cleanup_completions) == 1
        assert cleanup_completions[0].related_ref != initial_cleanup_start.related_ref
