from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.attachment_import import AttachmentImportReceipt
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import (
    ControlMutation,
    StorageError,
    control_mutation_fingerprint,
)
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.modeling import IntentCandidate
from agent_core.runtime import InvalidTransition
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_contracts import (
    D1Authorization,
    D1BudgetExhausted,
    D1OperationCompletion,
    D1OperationConflict,
    D1OperationStart,
    D1RecoveryRequired,
    D1RoleConfig,
    D1RunConfig,
    D1TurnBinding,
)
from agent_core.turn_manager import TurnManager


def uid() -> str:
    return str(uuid4())


class Sandbox:
    def __init__(self):
        self.destroyed = []

    def create(self, task_run_id):
        return "sandbox-" + task_run_id

    def interrupt(self, sandbox_id):
        pass

    def destroy(self, sandbox_id):
        self.destroyed.append(sandbox_id)


class NoEvidence:
    def command_exists(self, ref):
        return False

    def output_exists(self, ref):
        return False

    def lookup(self, ref):
        return None


class Imports:
    receipt = None

    def read_claim(self, import_id):
        return (
            self.receipt
            if self.receipt is not None and self.receipt.import_id == import_id
            else None
        )

    read_receipt = read_claim


def config():
    return D1RunConfig(
        D1RoleConfig("outer_planning", "fake", "fake-outer", "outer-v1"),
        D1RoleConfig("inner_action", "fake", "fake-inner", "inner-v1"),
        D1Authorization(uid()),
    )


@pytest.fixture
def runtime_path():
    path = Path(".test/d1-persistence") / uid()
    path.mkdir(parents=True)
    return path.resolve()


@pytest.fixture
def runtime(runtime_path):
    clock = lambda: 1800000000.0
    store = SqliteControlStore.open(runtime_path / "control.sqlite3", clock=clock)
    sandbox = Sandbox()
    imports = Imports()
    controller = TaskRunController(
        sandbox,
        store=store,
        repository=ControlStateRepository(store, NoEvidence()),
        registry=TaskRunControllerRegistry(),
        attachment_imports=imports,
        clock=clock,
    )
    request = TaskRequest.from_dict(
        {
            "goal": "check imported copy",
            "target": {
                "endpoints": [],
                "attachments": [{"kind": "local_path", "path": "input.bin"}],
            },
            "deadline_seconds": 60,
        }
    )
    selected = config()
    started = controller.start(
        request,
        ModelBackend.CODEX_CLI,
        uid(),
        source_root=str(runtime_path),
        d1_config=selected,
    )
    yield store, controller, sandbox, imports, selected, started
    if controller.status().value == "running":
        controller.cancel(uid())
    controller.cleanup_sandbox()
    assert sandbox.destroyed
    controller.close()
    store.close()


def start_for(controller, config, category="outer_model", **kwargs):
    role = (
        "outer_planning"
        if category == "outer_model"
        else "inner_action"
        if category.startswith("inner_") and category.endswith("model")
        else None
    )
    return D1OperationStart(
        uid(),
        controller.task_run.task_run_id,
        category,
        1800000000.0,
        controller.control_state.latest_checkpoint.sequence,
        "a" * 64,
        role,
        config.outer_role.role_config_ref
        if role == "outer_planning"
        else config.inner_role.role_config_ref
        if role
        else None,
        **kwargs,
    )


def completion(start, kind="completed"):
    return D1OperationCompletion(
        start.operation_id,
        start.started_at,
        kind,
        "evidence/" + start.operation_id,
        "b" * 64,
    )


def test_explicit_config_is_frozen_in_initial_transaction(runtime):
    store, controller, sandbox, _, selected, started = runtime
    assert store.load_d1_run_config(started.value) == selected
    assert store.load_control_state_rows(started.value).latest_checkpoint.sequence == 1
    altered = replace(
        selected, inner_role=replace(selected.inner_role, prompt_template="changed")
    )
    assert altered.inner_role.role_config_ref != selected.inner_role.role_config_ref
    with pytest.raises(D1OperationConflict, match="frozen"):
        store.begin_d1_operation(
            replace(
                start_for(controller, selected, "inner_probe_model"),
                role_config_ref=altered.inner_role.role_config_ref,
            )
        )
    assert store.list_d1_operations(started.value) == ()
    assert sandbox.destroyed == []


def test_unknown_operations_survive_reopen_without_reset_or_redispatch(runtime):
    store, controller, _, _, selected, _ = runtime
    start = start_for(controller, selected)
    assert store.begin_d1_operation(start) is None
    second = SqliteControlStore.open(store.path, clock=lambda: start.started_at)
    try:
        with pytest.raises(D1RecoveryRequired):
            second.begin_d1_operation(replace(start, started_at=start.started_at + 1))
        with pytest.raises(D1RecoveryRequired):
            second.begin_d1_operation(
                start_for(controller, selected, "inner_probe_model")
            )
        with pytest.raises(D1RecoveryRequired):
            second.begin_d1_operation(
                start_for(controller, selected, "copy_preparation", resource_ref=uid())
            )
        assert len(second.list_d1_operations(start.task_run_id)) == 1
        assert second.load_d1_operation(start.operation_id).completion is None
    finally:
        second.close()


def test_known_rejected_result_and_application_are_distinct(runtime):
    store, controller, _, _, selected, _ = runtime
    start = start_for(controller, selected, resource_ref="registration-1")
    store.begin_d1_operation(start)
    done = completion(start, "rejected")
    store.complete_d1_operation(done)
    record = store.begin_d1_operation(replace(start, started_at=start.started_at + 1))
    assert record.completion == done
    assert record.application_ref is None
    with pytest.raises(D1OperationConflict):
        store.mark_d1_operation_applied(start.operation_id, uid())
    correction = start_for(controller, selected, resource_ref="registration-1")
    assert store.begin_d1_operation(correction) is None
    store.complete_d1_operation(completion(correction))
    store.mark_d1_operation_applied(correction.operation_id, "turn-registration")
    with pytest.raises(D1BudgetExhausted):
        store.begin_d1_operation(
            start_for(controller, selected, resource_ref="registration-1")
        )


def test_independent_phase_and_tool_budgets_are_persistent(runtime):
    store, controller, _, _, selected, _ = runtime
    for category in ("inner_probe_model", "inner_complete_model"):
        for _ in range(2):
            start = start_for(controller, selected, category)
            store.begin_d1_operation(start)
            store.complete_d1_operation(completion(start, "rejected"))
        with pytest.raises(D1BudgetExhausted):
            store.begin_d1_operation(start_for(controller, selected, category))
    for category in ("inner_tool", "first_digest"):
        start = start_for(controller, selected, category, resource_ref=uid())
        store.begin_d1_operation(start)
        store.complete_d1_operation(completion(start))
        with pytest.raises(D1BudgetExhausted):
            store.begin_d1_operation(
                start_for(controller, selected, category, resource_ref=uid())
            )
    assert len(store.list_d1_operations(controller.task_run.task_run_id)) == 6


def prepare_registration(runtime):
    store, controller, _, imports, selected, _ = runtime
    proposal = start_for(controller, selected)
    store.begin_d1_operation(proposal)
    store.complete_d1_operation(completion(proposal))
    mutation_id = uid()
    registration = TurnManager.prepare_turn_registration(
        state=controller.control_state,
        mutation_id=mutation_id,
        turn_target="copy SHA matches original at first observation",
        candidates=(IntentCandidate("check fixed copy", "digest"),),
    )
    intent_id = registration.entity_ids[1]
    imports.receipt = AttachmentImportReceipt(
        uid(),
        controller.task_run.task_run_id,
        controller.task_run.sandbox_id,
        0,
        "input.bin",
        controller.task_run.source_root,
        "/workspace/input/input.bin",
        1,
        "c" * 64,
        1800000000.0,
        1800000000.0,
    )
    binding = D1TurnBinding(
        uid(),
        controller.task_run.task_run_id,
        registration.turn_id,
        intent_id,
        0,
        imports.receipt.import_id,
        imports.receipt.sha256,
        f"/workspace/work/intents/{intent_id}/input.bin",
        uid(),
        proposal.operation_id,
    )
    return registration, binding, mutation_id


def test_turn_binding_is_atomic_and_regular_registration_cannot_omit_it(runtime):
    store, controller, _, _, _, _ = runtime
    registration, binding, mutation_id = prepare_registration(runtime)
    with pytest.raises(InvalidTransition):
        controller.commit_turn_registration(registration, mutation_id)
    assert store.load_d1_turn_binding(registration.turn_id) is None
    assert controller.control_state.turns == ()
    committed = controller.commit_turn_registration(
        registration, mutation_id, d1_binding=binding
    )
    assert store.load_d1_turn_binding(registration.turn_id) == binding
    assert (
        store.load_control_state_rows(binding.task_run_id).latest_checkpoint.sequence
        == committed.commit.checkpoint.sequence
    )
    assert len(controller.control_state.intents) == 1
    replay = controller.commit_turn_registration(
        registration, mutation_id, d1_binding=binding
    )
    assert replay.value == registration.turn_id


def test_binding_receipt_mismatch_leaves_no_partial_turn(runtime):
    store, controller, _, _, _, _ = runtime
    registration, binding, mutation_id = prepare_registration(runtime)
    with pytest.raises(InvalidTransition, match="receipt"):
        controller.commit_turn_registration(
            registration,
            mutation_id,
            d1_binding=replace(binding, expected_sha256="d" * 64),
        )
    assert controller.control_state.turns == ()
    assert store.load_d1_turn_binding(binding.turn_id) is None
    assert store.load_committed_mutation(binding.task_run_id, mutation_id) is None


def test_turn_and_binding_rollback_together_after_binding_write(runtime, monkeypatch):
    store, controller, _, _, _, _ = runtime
    registration, binding, mutation_id = prepare_registration(runtime)
    save = store._save_d1_mutation

    def fail_after_binding(mutation, event_sequence):
        save(mutation, event_sequence)
        raise StorageError("injected failure after binding write")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_save_d1_mutation", fail_after_binding)
        with pytest.raises(StorageError, match="injected"):
            controller.commit_turn_registration(
                registration, mutation_id, d1_binding=binding
            )
    rows = store.load_control_state_rows(binding.task_run_id)
    assert rows.turns == rows.intents == ()
    assert rows.latest_checkpoint.sequence == 1
    assert store.load_d1_turn_binding(binding.turn_id) is None
    assert store.load_committed_mutation(binding.task_run_id, mutation_id) is None
    controller.commit_turn_registration(registration, mutation_id, d1_binding=binding)
    assert store.load_d1_turn_binding(binding.turn_id) == binding


def test_store_rejects_mutating_bound_turn_declaration(runtime):
    store, controller, _, _, _, _ = runtime
    registration, binding, mutation_id = prepare_registration(runtime)
    controller.commit_turn_registration(registration, mutation_id, d1_binding=binding)
    from agent_core.control_store import UpsertTurn
    from agent_core.runtime import RuntimeEvent

    turn = controller.control_state.turns[0]
    changed = UpsertTurn(
        turn.turn_id,
        turn.task_run_id,
        turn.sequence,
        "changed meaning",
        turn.intent_ids,
        turn.status,
    )
    mutation = ControlMutation.create(
        task_run_id=binding.task_run_id,
        mutation_id=uid(),
        operation="register_turn",
        request=controller.task_run.request,
        effective_model_backend=controller.task_run.effective_model_backend,
        base_checkpoint_sequence=controller.control_state.latest_checkpoint.sequence,
        state_changes=(changed,),
        event=RuntimeEvent(
            "control_state_committed",
            binding.task_run_id,
            1800000000.0,
            controller.task_run.status,
            {
                "operation": "register_turn",
                "entity_ids": [turn.turn_id],
                "result": turn.turn_id,
            },
        ),
    )
    with pytest.raises(StorageError, match="immutable"):
        store.commit(mutation)
    assert (
        store.load_control_state_rows(binding.task_run_id).turns[0].turn_target
        == turn.turn_target
    )


def test_readonly_schema_10_keeps_database_bytes_and_has_no_d1_identity(runtime_path):
    import agent_core.sqlite_control_store as module

    path = runtime_path / "schema10.sqlite3"
    connection = sqlite3.connect(path)
    for version in range(1, 11):
        connection.executescript(getattr(module, f"MIGRATION_{version}"))
        connection.execute(
            "INSERT INTO schema_versions VALUES (?,?)", (version, 1800000000.0)
        )
        connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with SqliteControlStore.open_readonly(path) as old:
        assert old.load_d1_run_config(uid()) is None
        assert old.list_d1_operations(uid()) == ()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT MAX(version) FROM schema_versions"
    ).fetchone() == (10,)
    connection.close()
    with SqliteControlStore.open(path):
        pass
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT MAX(version) FROM schema_versions"
    ).fetchone() == (module.CURRENT_SCHEMA_VERSION,)
    connection.close()


def test_failed_d1_schema_upgrade_preserves_schema_10(runtime_path, monkeypatch):
    import agent_core.sqlite_control_store as module

    path = runtime_path / "schema10-failed.sqlite3"
    connection = sqlite3.connect(path)
    for version in range(1, 11):
        connection.executescript(getattr(module, f"MIGRATION_{version}"))
        connection.execute(
            "INSERT INTO schema_versions VALUES (?,?)", (version, 1800000000.0)
        )
        connection.commit()
    connection.close()
    with monkeypatch.context() as patch:
        patch.setattr(
            module,
            "MIGRATION_11",
            module.MIGRATION_11 + "CREATE TABLE invalid_upgrade (value NOT_VALID,);\n",
        )
        with pytest.raises(StorageError, match="migration"):
            SqliteControlStore.open(path)
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT MAX(version) FROM schema_versions"
    ).fetchone() == (10,)
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'd1_runs'"
        ).fetchone()
        is None
    )
    connection.close()


def test_d1_fingerprint_covers_config_without_changing_legacy_payload(runtime):
    store, controller, _, _, selected, _ = runtime
    task = controller.task_run
    from agent_core.control_store import UpsertTaskRun
    from agent_core.runtime import RuntimeEvent

    change = UpsertTaskRun(
        task.task_run_id,
        task.request,
        task.effective_model_backend,
        task.started_at,
        task.deadline_at,
        task.status,
        task.sandbox_id,
        source_root=task.source_root,
    )
    mutation = ControlMutation.create(
        task_run_id=task.task_run_id,
        mutation_id=uid(),
        operation="start_task_run",
        request=task.request,
        effective_model_backend=task.effective_model_backend,
        base_checkpoint_sequence=0,
        state_changes=(change,),
        event=RuntimeEvent(
            "task_run_started",
            task.task_run_id,
            task.started_at,
            task.status,
            {
                "operation": "start_task_run",
                "entity_ids": [task.task_run_id],
                "result": task.task_run_id,
            },
        ),
    )
    assert (
        control_mutation_fingerprint(replace(mutation, d1_config=selected))
        != mutation.request_fingerprint
    )
    with pytest.raises(StorageError):
        store.commit(replace(mutation, d1_config=selected))
