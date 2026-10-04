from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_d1_persistence import Imports, config, prepare_registration

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import (
    AppendD2ReviewApplication,
    D2RunConfig,
    IdempotencyConflictError,
    StorageError,
)
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.review_contracts import D2AllowedActionKinds
from agent_core.runtime import TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore


class _Sandbox:
    def create(self, task_run_id):
        return "sandbox-" + task_run_id

    def interrupt(self, sandbox_id):
        pass

    def destroy(self, sandbox_id):
        pass


class _Evidence:
    def command_exists(self, ref):
        return False

    def output_exists(self, ref):
        return False

    def lookup(self, ref):
        return None


def _start(path: Path):
    store = SqliteControlStore.open(path, clock=lambda: 1800000000.0)
    imports = Imports()
    selected = config()
    controller = TaskRunController(
        _Sandbox(),
        store=store,
        repository=ControlStateRepository(store, _Evidence()),
        registry=TaskRunControllerRegistry(),
        attachment_imports=imports,
        clock=lambda: 1800000000.0,
    )
    request = TaskRequest.from_dict(
        {
            "goal": "review",
            "target": {
                "endpoints": [],
                "attachments": [{"kind": "local_path", "path": "input.bin"}],
            },
            "deadline_seconds": 60,
        }
    )
    started = controller.start(
        request,
        ModelBackend.CODEX_CLI,
        str(uuid4()),
        source_root=str(path.parent.resolve()),
        d1_config=selected,
        d2_config=D2RunConfig(selected.outer_role.role_config_ref),
    )
    runtime = store, controller, _Sandbox(), imports, selected, started
    registration, binding, mutation_id = prepare_registration(runtime)
    controller.commit_turn_registration(registration, mutation_id, d1_binding=binding)
    return store, controller, started.value, binding


@pytest.fixture
def runtime_path():
    path = Path(".test/d2-control-store") / str(uuid4())
    path.mkdir(parents=True)
    return path.resolve()


def test_d2_application_is_atomic_replayed_and_readable(runtime_path):
    store, controller, task_run_id, binding = _start(runtime_path / "control.sqlite3")
    review_id = str(uuid4())
    application = AppendD2ReviewApplication(
        str(uuid4()),
        1,
        review_id,
        "d2-review:" + review_id,
        "v1",
        1,
        (D2AllowedActionKinds("turn", binding.turn_id, ("complete_intent",)),),
        False,
        binding.turn_id,
        binding.intent_id,
        "turn_start",
    )
    mutation_id = str(uuid4())
    first = controller.apply_d2_review(
        application, mutation_id, evidence_validator=lambda _: None
    )
    second = controller.apply_d2_review(application, mutation_id)
    assert first.commit.replayed is False
    assert second.commit.replayed is True
    assert store.load_d2_review_application(task_run_id, review_id) == application
    assert store.list_d2_review_applications(task_run_id) == (application,)
    assert controller.status() is TaskRunStatus.RUNNING
    controller.cancel(str(uuid4()))
    controller.cleanup_sandbox()
    controller.close()
    store.close()


def test_schema_11_readonly_is_unchanged_and_has_no_d2_table(runtime_path):
    path = runtime_path / "schema11.sqlite3"
    connection = sqlite3.connect(path)
    import agent_core.sqlite_control_store as module

    for version in range(1, 12):
        connection.executescript(getattr(module, f"MIGRATION_{version}"))
        connection.execute(
            "INSERT INTO schema_versions VALUES (?, ?)", (version, 1800000000.0)
        )
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with SqliteControlStore.open_readonly(path) as store:
        assert store.list_d2_review_applications(str(uuid4())) == ()
    assert path.read_bytes() == before
    with SqliteControlStore.open(path) as store:
        assert store.list_d2_review_applications(str(uuid4())) == ()
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT MAX(version) FROM schema_versions"
    ).fetchone() == (module.CURRENT_SCHEMA_VERSION,)
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'd2_review_applications'"
    ).fetchone() == (1,)
    connection.close()


def test_same_review_cannot_change_and_pause_stays_closed(runtime_path):
    store, controller, task_run_id, binding = _start(runtime_path / "closed.sqlite3")
    review_id = str(uuid4())
    application = AppendD2ReviewApplication(
        str(uuid4()),
        1,
        review_id,
        "d2-review:" + review_id,
        "v1",
        0,
        (),
        False,
        binding.turn_id,
        binding.intent_id,
        "turn_start",
    )
    mutation_id = str(uuid4())
    controller.apply_d2_review(
        application, mutation_id, evidence_validator=lambda _: None
    )
    with pytest.raises(IdempotencyConflictError):
        controller.apply_d2_review(
            replace(application, context_version="v2"), mutation_id
        )
    pause_id = str(uuid4())
    pause = replace(
        application,
        review_id=pause_id,
        application_ref="pause:" + pause_id,
        paused_gate=True,
        pause_reason="provider_failure",
    )
    controller.apply_d2_review(pause, str(uuid4()))
    assert (
        store.list_d2_review_applications(task_run_id)[-1].pause_reason
        == "provider_failure"
    )
    later_id = str(uuid4())
    later = replace(
        application,
        review_id=later_id,
        application_ref="later:" + later_id,
        review_generation=2,
    )
    with pytest.raises(StorageError, match="pause"):
        controller.apply_d2_review(
            later, str(uuid4()), evidence_validator=lambda _: None
        )
    controller.cancel(str(uuid4()))
    controller.cleanup_sandbox()
    controller.close()
    store.close()


def test_failed_schema_12_upgrade_rolls_back(runtime_path, monkeypatch):
    import agent_core.sqlite_control_store as module

    path = runtime_path / "failed12.sqlite3"
    connection = sqlite3.connect(path)
    for version in range(1, 12):
        connection.executescript(getattr(module, f"MIGRATION_{version}"))
        connection.execute(
            "INSERT INTO schema_versions VALUES (?, ?)", (version, 1800000000.0)
        )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        module,
        "MIGRATION_12",
        module.MIGRATION_12 + "CREATE TABLE invalid (bad SYNTAX,);\n",
    )
    with pytest.raises(StorageError, match="migration"):
        SqliteControlStore.open(path)
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT MAX(version) FROM schema_versions"
    ).fetchone() == (11,)
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'd2_runs'"
        ).fetchone()
        is None
    )
    connection.close()


def test_application_row_rolls_back_if_event_write_fails(runtime_path, monkeypatch):
    store, controller, task_run_id, binding = _start(runtime_path / "rollback.sqlite3")
    review_id = str(uuid4())
    application = AppendD2ReviewApplication(
        str(uuid4()),
        1,
        review_id,
        "d2-review:" + review_id,
        "v1",
        0,
        (),
        False,
        binding.turn_id,
        binding.intent_id,
        "turn_start",
    )
    checkpoint = controller.control_state.latest_checkpoint.sequence
    mutation_id = str(uuid4())
    original = store._insert_event

    def fail_event(event, sequence):
        if event.details.get("operation") == "apply_d2_review":
            raise StorageError("injected D2 event write failure")
        return original(event, sequence)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_insert_event", fail_event)
        with pytest.raises(StorageError, match="injected"):
            controller.apply_d2_review(
                application, mutation_id, evidence_validator=lambda _: None
            )
    assert store.load_d2_review_application(task_run_id, review_id) is None
    assert store.load_committed_mutation(task_run_id, mutation_id) is None
    assert controller.control_state.latest_checkpoint.sequence == checkpoint
    controller.apply_d2_review(
        application, mutation_id, evidence_validator=lambda _: None
    )
    assert store.load_d2_review_application(task_run_id, review_id) == application
    controller.cancel(str(uuid4()))
    controller.cleanup_sandbox()
    controller.close()
    store.close()
