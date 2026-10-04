"""Durable D4 start accounting and its compatibility with D3 records."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_d4_chain import make_d4_scenario, reopen_scenario

from agent_core.control_store import StorageError
from agent_core.d4_contracts import (
    D4AuditEvent,
    D4BudgetExhausted,
    D4OperationCompletion,
    D4OperationConflict,
    D4OperationStart,
    D4RecoveryRequired,
)
from agent_core.dual_llm_contracts import (
    D3CommitMetadata,
    D3OperationCompletion,
    D3OperationStart,
    d3_payload,
)
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_modeling import digest, stable_id

D4_BUDGET_MATRIX = {
    "planning": 4,
    "action": 16,
    "review": 8,
    "refinement": 4,
    "tool_dispatch": 6,
    "sha_observation": 4,
    "artifact_share": 4,
    "attachment_import": 2,
}


def _start(case, category, index, *, operation_id=None):
    app = case[0]
    task_run_id = app.state.task_run.task_run_id
    model_role = (
        app.config.outer_role
        if category in {"planning", "review", "refinement"}
        else app.config.inner_role
        if category == "action"
        else None
    )
    return D4OperationStart(
        operation_id or stable_id(task_run_id, f"d4-budget/{category}/{index}"),
        task_run_id,
        category,
        case[5].now,
        app.state.latest_checkpoint.sequence,
        digest({"category": category, "index": index}),
        f"d4-resource/{category}/{index}",
        app.config.d4_budget.budget_policy_ref,
        model_role.role if model_role else None,
        model_role.role_config_ref if model_role else None,
    )


def _completion(start, *, result="completed"):
    return D4OperationCompletion(
        start.operation_id,
        start.started_at,
        result,
        f"evidence/{start.operation_id}",
        digest({"operation_id": start.operation_id, "result": result}),
    )


def test_all_eight_limits_sum_to_48_and_failed_starts_remain_charged(tmp_path):
    case = make_d4_scenario(tmp_path)
    app = case[0]
    task_run_id = app.state.task_run.task_run_id
    try:
        assert sum(D4_BUDGET_MATRIX.values()) == 48
        for category, limit in D4_BUDGET_MATRIX.items():
            for index in range(limit):
                start = _start(case, category, index)
                assert app.store.begin_d4_operation(start) is None
                app.store.complete_d4_operation(
                    _completion(start, result="failed" if index == 0 else "completed")
                )

        usage = app.store.d4_budget_usage(task_run_id)
        assert usage.total_used == 48
        assert dict(usage.category_counts) == D4_BUDGET_MATRIX
        assert any(
            record.completion.result_kind == "failed"
            for record in app.store.list_d4_operations(task_run_id)
        )
        for category, index in (("copy_preparation", 0), ("intent_stop", 0)):
            audit = _start(case, category, index)
            assert app.store.begin_d4_operation(audit) is None
            app.store.complete_d4_operation(_completion(audit))
        usage = app.store.d4_budget_usage(task_run_id)
        assert usage.total_used == 48 and usage.audit_only_used == 2
        with pytest.raises(D4BudgetExhausted):
            app.store.begin_d4_operation(_start(case, "action", 16))

        d3_operation_id = stable_id(task_run_id, "d4-total-limit-d3-admission")
        d3_start = D3OperationStart(
            d3_operation_id,
            task_run_id,
            "tool_dispatch",
            case[5].now,
            app.state.latest_checkpoint.sequence,
            digest("D3 admission rejected by D4 total budget"),
            "d4-total-limit-resource",
        )
        with pytest.raises(D4BudgetExhausted):
            app.store.begin_d3_operation(d3_start)
        assert app.store.load_d3_operation(d3_operation_id) is None
        assert app.store.load_d4_operation(d3_operation_id) is None

        store_path = app.directory / "control.sqlite"
        with SqliteControlStore.open(store_path, clock=case[5]) as reopened:
            assert reopened.d4_budget_usage(task_run_id) == usage
            with pytest.raises(D4BudgetExhausted):
                reopened.begin_d4_operation(_start(case, "planning", 4))
    finally:
        app.controller.cancel(str(uuid4()))
        app.controller.cleanup_sandbox()
        app.close()
        case[1].destroyed = True


def test_known_d4_start_is_idempotent_but_changed_request_conflicts(
    d4_scenario_fixture,
):
    case = d4_scenario_fixture
    app = case[0]
    start = _start(case, "tool_dispatch", 0)
    assert app.store.begin_d4_operation(start) is None
    completion = _completion(start, result="failed")
    app.store.complete_d4_operation(completion)
    assert app.store.begin_d4_operation(start).completion == completion
    changed = replace(start, resource_ref="different-resource")
    with pytest.raises(D4OperationConflict):
        app.store.begin_d4_operation(changed)
    assert app.store.d4_budget_usage(start.task_run_id).total_used == 1


def test_planning_budget_exhaustion_stops_before_model_dispatch(d4_scenario_fixture):
    case = d4_scenario_fixture
    app, sandbox, outer = case[0], case[1], case[2]
    task_run_id = app.state.task_run.task_run_id
    for index in range(D4_BUDGET_MATRIX["planning"]):
        start = _start(case, "planning", index)
        app.store.begin_d4_operation(start)
        app.store.complete_d4_operation(_completion(start))

    result = asyncio.run(app.run(case[6]))

    assert result.stop_reason.startswith("budget_exhausted:")
    assert not result.recovery_required
    assert not outer.calls
    assert not sandbox.command_calls
    outer_request_id = app._id("outer/turn/1")
    assert app.store.load_d3_operation(outer_request_id) is None
    assert app.store.load_d4_operation(outer_request_id) is None
    assert dict(app.store.d4_budget_usage(task_run_id).category_counts)["planning"] == 4


def test_cancel_audit_permanently_blocks_new_ids_but_allows_known_replay(
    d4_scenario_fixture,
):
    case = d4_scenario_fixture
    app = case[0]
    task_run_id = app.state.task_run.task_run_id
    completed_start = _start(case, "tool_dispatch", 0)
    completion = _completion(completed_start)
    app.store.begin_d4_operation(completed_start)
    app.store.complete_d4_operation(completion)

    app._persist_cancel_intent()
    app._persist_cancel_intent()
    cancellation_events = [
        event
        for event in app.store.list_d4_audit_events(task_run_id)
        if event.category == "cancellation_requested"
    ]
    assert len(cancellation_events) == 1
    assert cancellation_events[0].outcome == "known"

    app = reopen_scenario(case)
    replay = app.store.begin_d4_operation(completed_start)
    assert replay is not None and replay.completion == completion
    rejected_start = _start(case, "tool_dispatch", 1)
    with pytest.raises(D4RecoveryRequired, match="d4_cancellation_requested"):
        app.store.begin_d4_operation(rejected_start)
    assert app.store.load_d4_operation(rejected_start.operation_id) is None
    assert (
        len(
            [
                event
                for event in app.store.list_d4_audit_events(task_run_id)
                if event.category == "cancellation_requested"
            ]
        )
        == 1
    )


@pytest.fixture
def d4_scenario_fixture(tmp_path):
    from test_d4_chain import close_scenario

    case = make_d4_scenario(tmp_path)
    try:
        yield case
    finally:
        close_scenario(case)


def test_d3_admission_and_completion_mirror_atomically_to_d4(
    d4_scenario_fixture, monkeypatch
):
    case = d4_scenario_fixture
    app = case[0]
    task_run_id = app.state.task_run.task_run_id
    operation_id = stable_id(task_run_id, "d4-atomic-d3-operation")
    start = D3OperationStart(
        operation_id,
        task_run_id,
        "inner_model",
        case[5].now,
        app.state.latest_checkpoint.sequence,
        digest({"request": "fixed-tool-dispatch"}),
        "fixed-tool-resource",
        app.config.inner_role.role,
        app.config.inner_role.role_config_ref,
    )
    assert app.store.begin_d3_operation(start) is None
    d3_record = app.store.load_d3_operation(operation_id)
    d4_record = app.store.load_d4_operation(operation_id)
    assert d4_record is not None
    assert d4_record.start.category == "action"
    assert d4_record.start.operation_id == d3_record.start.operation_id

    completion = D3OperationCompletion(
        operation_id,
        case[5].now,
        "completed",
        f"evidence/{operation_id}",
        digest({"operation_id": operation_id}),
    )
    with monkeypatch.context() as patch:

        def fail_companion(_completion):
            raise RuntimeError("injected D4 companion write failure")

        patch.setattr(app.store, "_complete_d4_operation", fail_companion)
        with pytest.raises(RuntimeError, match="companion write failure"):
            app.store.complete_d3_operation(completion)
    assert app.store.load_d3_operation(operation_id).completion is None
    assert app.store.load_d4_operation(operation_id).completion is None
    assert not any(
        event.related_ref == operation_id
        and event.category == "proposal_evidence_saved"
        for event in app.store.list_d4_audit_events(task_run_id)
    )

    app.store.complete_d3_operation(completion)
    assert app.store.load_d4_operation(
        operation_id
    ).completion == D4OperationCompletion(**asdict(completion))
    app.store.mark_d3_operation_applied(operation_id, "control-commit/1")
    assert (
        app.store.load_d4_operation(operation_id).application_ref == "control-commit/1"
    )
    proposal_events = [
        event
        for event in app.store.list_d4_audit_events(task_run_id)
        if event.related_ref == operation_id
        and event.category == "proposal_evidence_saved"
    ]
    assert len(proposal_events) == 1
    assert proposal_events[0].outcome == "known"
    assert proposal_events[0].evidence_ref == completion.evidence_ref
    assert proposal_events[0].evidence_sha256 == completion.evidence_sha256


@pytest.mark.parametrize("category", ["planning", "action", "review", "refinement"])
def test_proposal_completion_audit_is_deterministic_and_idempotent(
    d4_scenario_fixture, category
):
    app = d4_scenario_fixture[0]
    task_run_id = app.state.task_run.task_run_id
    start = _start(d4_scenario_fixture, category, 0)
    completion = _completion(start)

    assert app.store.begin_d4_operation(start) is None
    app.store.complete_d4_operation(completion)
    app.store.complete_d4_operation(completion)

    expected_id = app.store._d4_audit_event_id(
        task_run_id, start.operation_id, "proposal_evidence_saved", "known"
    )
    event = app.store.load_d4_audit_event(expected_id)
    assert event == D4AuditEvent(
        expected_id,
        task_run_id,
        "proposal_evidence_saved",
        "known",
        completion.finished_at,
        completion.evidence_ref,
        completion.evidence_sha256,
        start.operation_id,
    )
    assert (
        sum(
            item.category == "proposal_evidence_saved"
            and item.related_ref == start.operation_id
            for item in app.store.list_d4_audit_events(task_run_id)
        )
        == 1
    )


def test_d3_admission_rejected_by_category_limit_leaves_no_partial_rows(
    d4_scenario_fixture,
):
    case = d4_scenario_fixture
    app = case[0]
    task_run_id = app.state.task_run.task_run_id
    for index in range(D4_BUDGET_MATRIX["planning"]):
        operation_id = stable_id(task_run_id, f"d4-unknown-review-priority/{index}")
        start = D3OperationStart(
            operation_id,
            task_run_id,
            "outer_model",
            case[5].now,
            app.state.latest_checkpoint.sequence,
            digest({"planning_attempt": index}),
            f"planning/unknown-review-priority/{index}",
            app.config.outer_role.role,
            app.config.outer_role.role_config_ref,
        )
        assert app.store.begin_d3_operation(start) is None
        app.store.complete_d3_operation(
            D3OperationCompletion(
                operation_id,
                case[5].now,
                "completed",
                f"evidence/{operation_id}",
                digest({"operation_id": operation_id}),
            )
        )

    operation_id = stable_id(task_run_id, "d4-planning-cap-d3-attempt")
    d3_start = D3OperationStart(
        operation_id,
        task_run_id,
        "outer_model",
        case[5].now,
        app.state.latest_checkpoint.sequence,
        digest("planning category cap"),
        "planning/d3-attempt",
        app.config.outer_role.role,
        app.config.outer_role.role_config_ref,
    )
    with pytest.raises(D4BudgetExhausted):
        app.store.begin_d3_operation(d3_start)
    assert app.store.load_d3_operation(operation_id) is None
    assert app.store.load_d4_operation(operation_id) is None


def test_unknown_review_blocks_d3_category_exhaustion_without_partial_admission(
    d4_scenario_fixture,
):
    case = d4_scenario_fixture
    app = case[0]
    task_run_id = app.state.task_run.task_run_id
    for index in range(D4_BUDGET_MATRIX["planning"]):
        start = _start(case, "planning", index)
        app.store.begin_d4_operation(start)
        app.store.complete_d4_operation(_completion(start))

    unknown_review = _start(case, "review", 0)
    assert app.store.begin_d4_operation(unknown_review) is None
    operation_id = stable_id(task_run_id, "d4-unknown-review-before-d3-limit")
    d3_start = D3OperationStart(
        operation_id,
        task_run_id,
        "outer_model",
        case[5].now,
        app.state.latest_checkpoint.sequence,
        digest("unknown review precedes exhausted planning category"),
        "planning/unknown-review-priority",
        app.config.outer_role.role,
        app.config.outer_role.role_config_ref,
    )

    with pytest.raises(D4RecoveryRequired):
        app.store.begin_d3_operation(d3_start)

    assert app.store.load_d3_operation(operation_id) is None
    assert app.store.load_d4_operation(operation_id) is None
    assert app.store.load_d4_operation(unknown_review.operation_id).completion is None


def test_unknown_refinement_seal_preserves_unknown_and_budget_and_forbids_replay(
    d4_scenario_fixture,
):
    case = d4_scenario_fixture
    app = case[0]
    start = _start(case, "refinement", 0)
    assert app.store.begin_d4_operation(start) is None
    seal = {
        "decided_at": start.started_at,
        "evidence_ref": f"d5/refinement/{start.operation_id}/unknown-seal",
        "evidence_sha256": digest({"operation_id": start.operation_id, "sealed": True}),
    }

    app.store.seal_unknown_refinement(start.operation_id, **seal)
    app.store.seal_unknown_refinement(start.operation_id, **seal)

    record = app.store.load_d4_operation(start.operation_id)
    assert record is not None and record.completion is None
    assert app.store.is_unknown_refinement_sealed(start.operation_id)
    assert app.store.list_d4_unresolved_operations(start.task_run_id) == ()
    usage = app.store.d4_budget_usage(start.task_run_id)
    assert usage.total_used == 1
    assert dict(usage.category_counts)["refinement"] == 1

    with pytest.raises(D4RecoveryRequired, match="do not dispatch again"):
        app.store.begin_d4_operation(start)
    with pytest.raises(D4RecoveryRequired, match="sealed refinement"):
        app.store.complete_d4_operation(_completion(start))
    with pytest.raises(D4OperationConflict, match="immutable"):
        app.store.seal_unknown_refinement(
            start.operation_id,
            decided_at=seal["decided_at"],
            evidence_ref=f"{seal['evidence_ref']}/changed",
            evidence_sha256=seal["evidence_sha256"],
        )
    assert app.store.load_d4_operation(start.operation_id).completion is None


@pytest.mark.parametrize(
    "category",
    [
        "planning",
        "action",
        "review",
        "tool_dispatch",
        "sha_observation",
        "artifact_share",
        "attachment_import",
    ],
)
def test_non_refinement_unknown_cannot_be_sealed(d4_scenario_fixture, category):
    case = d4_scenario_fixture
    app = case[0]
    start = _start(case, category, 0)
    assert app.store.begin_d4_operation(start) is None

    with pytest.raises(D4OperationConflict, match="only refinement"):
        app.store.seal_unknown_refinement(
            start.operation_id,
            decided_at=start.started_at,
            evidence_ref=f"d5/refinement/{start.operation_id}/unknown-seal",
            evidence_sha256=digest(
                {"operation_id": start.operation_id, "sealed": True}
            ),
        )

    assert not app.store.is_unknown_refinement_sealed(start.operation_id)
    assert tuple(
        item.start.operation_id
        for item in app.store.list_d4_unresolved_operations(start.task_run_id)
    ) == (start.operation_id,)


def test_sealed_refinement_is_exempt_from_lifecycle_gate_but_tool_unknown_is_not(
    d4_scenario_fixture,
):
    case = d4_scenario_fixture
    app = case[0]
    refinement = _start(case, "refinement", 0)
    assert app.store.begin_d4_operation(refinement) is None
    app.store.seal_unknown_refinement(
        refinement.operation_id,
        decided_at=refinement.started_at,
        evidence_ref=f"d5/refinement/{refinement.operation_id}/unknown-seal",
        evidence_sha256=digest(
            {"operation_id": refinement.operation_id, "sealed": True}
        ),
    )
    assert app._d4_unknown_operations() == ()

    tool = _start(case, "tool_dispatch", 0)
    assert app.store.begin_d4_operation(tool) is None
    assert app._d4_unknown_operations() == (tool.operation_id,)
    with pytest.raises(D4RecoveryRequired, match="outcome_unknown"):
        app.controller.cancel(str(uuid4()))
    assert app.state.task_run.status.value == "running"

    app.store.complete_d4_operation(_completion(tool))
    app.controller.cancel(str(uuid4()))
    assert app.state.task_run.status.value == "cancelled"
    assert app.store.load_d4_operation(refinement.operation_id).completion is None


def test_schema14_readonly_keeps_unknown_refinement_unresolved(tmp_path):
    case = make_d4_scenario(tmp_path)
    app = case[0]
    start = _start(case, "refinement", 0)
    assert app.store.begin_d4_operation(start) is None
    path = app.directory / "control.sqlite"
    app.store.close()

    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE d5_attempts")
    connection.execute("DROP TABLE process_facts")
    connection.execute("ALTER TABLE d4_operations DROP COLUMN unknown_disposition_json")
    connection.execute("DELETE FROM schema_versions WHERE version = 15")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with SqliteControlStore.open_readonly(path) as readonly:
        assert readonly._schema_version == 14
        assert not readonly.is_unknown_refinement_sealed(start.operation_id)
        assert tuple(
            item.start.operation_id
            for item in readonly.list_d4_unresolved_operations(start.task_run_id)
        ) == (start.operation_id,)
    assert path.read_bytes() == before

    with SqliteControlStore.open(path, clock=case[5]) as upgraded:
        version = upgraded._connection.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0]
        assert version == 15
        assert tuple(
            item.start.operation_id
            for item in upgraded.list_d4_unresolved_operations(start.task_run_id)
        ) == (start.operation_id,)
    case[1].destroyed = True


def test_corrupt_refinement_disposition_fails_closed(d4_scenario_fixture):
    case = d4_scenario_fixture
    app = case[0]
    start = _start(case, "refinement", 0)
    assert app.store.begin_d4_operation(start) is None
    app.store.seal_unknown_refinement(
        start.operation_id,
        decided_at=start.started_at,
        evidence_ref=f"d5/refinement/{start.operation_id}/unknown-seal",
        evidence_sha256=digest({"operation_id": start.operation_id, "sealed": True}),
    )
    original = app.store._connection.execute(
        "SELECT unknown_disposition_json FROM d4_operations WHERE operation_id = ?",
        (start.operation_id,),
    ).fetchone()[0]
    app.store._connection.execute(
        "UPDATE d4_operations SET unknown_disposition_json = ? WHERE operation_id = ?",
        ("{invalid", start.operation_id),
    )

    try:
        with pytest.raises(StorageError, match="unknown disposition"):
            app.store.list_d4_unresolved_operations(start.task_run_id)
    finally:
        app.store._connection.execute(
            "UPDATE d4_operations SET unknown_disposition_json = ? WHERE operation_id = ?",
            (original, start.operation_id),
        )


def test_legacy_d3_payload_and_metadata_do_not_gain_d4_budget_field(
    d4_scenario_fixture,
):
    config = replace(d4_scenario_fixture[0].config, d4_budget=None)
    payload = d3_payload(config)
    assert "d4_budget" not in payload
    metadata = D3CommitMetadata(config=config)
    assert "d4_budget" not in d3_payload(metadata)["config"]


def test_schema13_readonly_is_unchanged_and_upgrade_does_not_backfill_d4(
    tmp_path,
):
    case = make_d4_scenario(tmp_path)
    app = case[0]
    source = _start(case, "tool_dispatch", 0)
    app.store.begin_d4_operation(source)
    app.store.complete_d4_operation(_completion(source))
    d3_start = D3OperationStart(
        stable_id(source.task_run_id, "legacy-d3-history"),
        source.task_run_id,
        "tool_dispatch",
        source.started_at,
        source.base_checkpoint_sequence,
        digest("legacy-history"),
        "legacy-d3-resource",
    )
    app.store.begin_d3_operation(d3_start)
    app.store.complete_d3_operation(
        D3OperationCompletion(
            d3_start.operation_id,
            source.started_at,
            "completed",
            "legacy/d3/evidence",
            digest("legacy-d3"),
        )
    )
    app.store.close()

    path = app.directory / "control.sqlite"
    connection = sqlite3.connect(path)
    config_row = connection.execute(
        "SELECT config_json FROM d3_runs WHERE task_run_id = ?",
        (source.task_run_id,),
    ).fetchone()
    legacy_config = json.loads(config_row[0])
    legacy_config.pop("d4_budget", None)
    connection.execute(
        "UPDATE d3_runs SET config_json = ? WHERE task_run_id = ?",
        (
            json.dumps(legacy_config, sort_keys=True, separators=(",", ":")),
            source.task_run_id,
        ),
    )
    connection.execute("DROP TABLE d5_attempts")
    connection.execute("DROP TABLE process_facts")
    connection.execute("DROP TABLE d4_audit_events")
    connection.execute("DROP TABLE d4_operations")
    connection.execute("DELETE FROM schema_versions WHERE version >= 14")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with SqliteControlStore.open_readonly(path) as readonly:
        assert readonly.load_d3_operation(d3_start.operation_id) is not None
        assert readonly.load_d4_operation(source.operation_id) is None
        assert readonly.list_d4_operations(source.task_run_id) == ()
    assert path.read_bytes() == before

    with SqliteControlStore.open(path, clock=case[5]) as upgraded:
        version = upgraded._connection.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0]
        assert version == 15
        assert upgraded.list_d4_operations(source.task_run_id) == ()
        assert upgraded.load_d3_operation(d3_start.operation_id) is not None
    case[1].destroyed = True
