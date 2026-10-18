from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_store import (
    ControlMutation,
    IdempotencyConflictError,
    StorageError,
    UpsertIntent,
    UpsertTaskRun,
    UpsertTurn,
)
from agent_core.web_contracts import (
    D7WebAuthorization,
    D7WebExecutionRecord,
    D7WebPlanRecord,
    D7WebRunConfig,
)
from agent_core.domain import IntentStatus, TurnStatus
from agent_core.goal_candidates import D7CommitMetadata, D7WebExecutionBinding
from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.runtime import RuntimeEvent, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_contracts import D1RoleConfig

NOW = 1_800_000_000.0
ORIGIN = "http://web.test:28989"


def _uid() -> str:
    return str(uuid4())


@pytest.fixture
def tmp_path() -> Path:
    base = Path(
        os.environ.get("QINYUAN_D7_WEB_TEST_BASETEMP", ".test/d7-web-store-tests")
    )
    path = base / _uid()
    path.mkdir(parents=True)
    return path.resolve()


def _request() -> TaskRequest:
    return TaskRequest.from_dict(
        {
            "goal": "inspect the authorized Web target",
            "target": {"endpoints": [{"url": ORIGIN}], "attachments": []},
            "deadline_seconds": 300,
        }
    )


def _config(task_run_id: str) -> D7WebRunConfig:
    return D7WebRunConfig(
        task_run_id=task_run_id,
        request_id=_uid(),
        endpoint=ORIGIN,
        authorization=D7WebAuthorization(
            authorization_id=_uid(),
            allowed_origin=ORIGIN,
            allowed_methods=("GET", "POST"),
        ),
        outer_role=D1RoleConfig(
            "outer_planning", "deepseek", "outer-model", "qinyuan.d7.outer.v1"
        ),
        inner_role=D1RoleConfig(
            "inner_action", "deepseek", "inner-model", "qinyuan.d7.inner.v1"
        ),
        d4_budget=D4BudgetPolicy(),
        model_transport_config_ref="sha256:" + "c" * 64,
    )


def _seed_web_store(store: SqliteControlStore, tmp_path: Path):
    task_run_id = _uid()
    config = _config(task_run_id)
    request = _request()
    task = UpsertTaskRun(
        task_run_id=task_run_id,
        request=request,
        effective_model_backend=ModelBackend.DEEPSEEK_OPENAI,
        started_at=NOW,
        deadline_at=NOW + request.deadline_seconds,
        status=TaskRunStatus.RUNNING,
        sandbox_id="sandbox-" + task_run_id,
        source_root=str(tmp_path),
    )
    start_event = RuntimeEvent(
        type="task_run_started",
        task_run_id=task_run_id,
        timestamp=NOW,
        status=TaskRunStatus.RUNNING,
        details={
            "operation": "start_task_run",
            "entity_ids": [task_run_id],
            "result": task_run_id,
        },
    )
    store.commit(
        ControlMutation.create(
            task_run_id=task_run_id,
            mutation_id=_uid(),
            operation="start_task_run",
            request=request,
            effective_model_backend=ModelBackend.DEEPSEEK_OPENAI,
            base_checkpoint_sequence=0,
            state_changes=(task,),
            event=start_event,
            d7=D7CommitMetadata(run_config=config),
        )
    )

    turn_id = _uid()
    intent_id = _uid()
    turn = UpsertTurn(
        turn_id=turn_id,
        task_run_id=task_run_id,
        sequence=1,
        turn_target="inspect one authorized page",
        intent_ids=(intent_id,),
        status=TurnStatus.RUNNING,
    )
    intent = UpsertIntent(
        intent_id=intent_id,
        turn_id=turn_id,
        description="request the authorized page",
        priority=1,
        intent_type="web_request",
        status=IntentStatus.RUNNING,
    )
    turn_event = RuntimeEvent(
        type="control_state_committed",
        task_run_id=task_run_id,
        timestamp=NOW + 1,
        status=TaskRunStatus.RUNNING,
        details={
            "operation": "register_turn",
            "entity_ids": [turn_id, intent_id],
            "result": turn_id,
        },
    )
    store.commit(
        ControlMutation.create(
            task_run_id=task_run_id,
            mutation_id=_uid(),
            operation="register_turn",
            request=request,
            effective_model_backend=ModelBackend.DEEPSEEK_OPENAI,
            base_checkpoint_sequence=1,
            state_changes=(turn, intent),
            event=turn_event,
        )
    )
    return store, task_run_id, config, turn_id, intent_id


@pytest.fixture
def web_store(tmp_path: Path):
    store = SqliteControlStore.open(tmp_path / "control.sqlite3", clock=lambda: NOW)
    try:
        yield _seed_web_store(store, tmp_path)
    finally:
        store.close()


def _plan(task_run_id: str, turn_id: str, intent_id: str) -> D7WebPlanRecord:
    return D7WebPlanRecord(
        task_run_id=task_run_id,
        turn_id=turn_id,
        intent_id=intent_id,
        plan_id=_uid(),
        method="GET",
        request_sha256=hashlib.sha256(b"GET /").hexdigest(),
        request_bytes=0,
        created_at=NOW + 2,
    )


def test_web_config_is_saved_atomically_with_task_run_start_and_reopened(web_store):
    store, task_run_id, config, _turn_id, _intent_id = web_store

    assert store.load_d7_web_run_config(task_run_id) == config
    assert store.load_task_run(task_run_id).request.target.endpoints[0].url == ORIGIN
    assert store.load_control_state_rows(task_run_id).latest_checkpoint.sequence == 2

    with SqliteControlStore.open_readonly(store.path) as reopened:
        assert reopened.load_d7_web_run_config(task_run_id) == config
        assert reopened.load_task_run(task_run_id).task_run_id == task_run_id


def test_web_plan_registration_is_idempotent_and_rejects_changed_content(web_store):
    store, task_run_id, _config, turn_id, intent_id = web_store
    record = _plan(task_run_id, turn_id, intent_id)

    assert store.register_d7_web_plan(record) == record
    assert store.register_d7_web_plan(record) == record
    assert store.load_d7_web_plan(record.plan_id) == record
    assert store.list_d7_web_plans(task_run_id) == (record,)

    with pytest.raises(IdempotencyConflictError):
        store.register_d7_web_plan(replace(record, method="POST"))


def test_web_execution_start_result_and_completed_binding_are_persistent(web_store):
    store, task_run_id, _config, turn_id, intent_id = web_store
    plan = _plan(task_run_id, turn_id, intent_id)
    store.register_d7_web_plan(plan)
    started = D7WebExecutionRecord(
        task_run_id=task_run_id,
        turn_id=turn_id,
        intent_id=intent_id,
        plan_id=plan.plan_id,
        execution_id=_uid(),
        http_operation_id=_uid(),
        state="started",
        request_sha256=plan.request_sha256,
        request_bytes=plan.request_bytes,
        started_at=NOW + 3,
    )

    first, admitted = store.start_d7_web_execution(started)
    replay, replay_admitted = store.start_d7_web_execution(started)
    assert first == started
    assert admitted is True
    assert replay == started
    assert replay_admitted is False
    assert store.load_d7_web_execution(started.execution_id) == started
    assert store.load_d7_web_execution_binding(started.execution_id) is None

    completed = replace(
        started,
        state="completed",
        http_status=200,
        response_evidence_id=_uid(),
        response_sha256=hashlib.sha256(b"response").hexdigest(),
        response_bytes=len(b"response"),
        finished_at=NOW + 4,
    )
    assert store.complete_d7_web_execution(completed) == completed
    assert store.complete_d7_web_execution(completed) == completed
    assert store.load_d7_web_execution(started.execution_id) == completed
    assert store.list_d7_web_executions(task_run_id) == (completed,)
    assert store.load_d7_web_execution_binding(started.execution_id) == D7WebExecutionBinding(
        task_run_id=task_run_id,
        turn_id=turn_id,
        intent_id=intent_id,
        plan_id=plan.plan_id,
        execution_id=started.execution_id,
        http_operation_id=started.http_operation_id,
        response_evidence_id=completed.response_evidence_id,
        response_sha256=completed.response_sha256,
        response_bytes=completed.response_bytes,
    )

    changed_terminal = replace(completed, http_status=201)
    with pytest.raises(IdempotencyConflictError):
        store.complete_d7_web_execution(changed_terminal)


def test_web_execution_rejects_plan_with_changed_request_binding(web_store):
    store, task_run_id, _config, turn_id, intent_id = web_store
    plan = _plan(task_run_id, turn_id, intent_id)
    store.register_d7_web_plan(plan)
    mismatched = D7WebExecutionRecord(
        task_run_id=task_run_id,
        turn_id=turn_id,
        intent_id=intent_id,
        plan_id=plan.plan_id,
        execution_id=_uid(),
        http_operation_id=_uid(),
        state="started",
        request_sha256="d" * 64,
        request_bytes=0,
        started_at=NOW + 3,
    )

    with pytest.raises(StorageError, match="saved Plan"):
        store.start_d7_web_execution(mismatched)

