from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

import pytest

from agent_adapters.deepseek_model import (
    DeepSeekModelAdapter,
    DeepSeekSettings,
    build_payload,
)
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import StorageError
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.fake_model_adapters import FakeCodexAdapter
from agent_core.intent_generation import AuditedTurnGenerator, OrderedIntentGenerator
from agent_core.model_invocation_audit import ModelInvocationRecoveryRequired
from agent_core.modeling import (
    IntentGenerationResponse,
    ModelInvocationCoordinator,
    ModelRequest,
    ModelRequestType,
)
from agent_core.sqlite_control_store import SqliteControlStore

NOW = 1_800_000_000.0


class Sandbox:
    def create(self, task_run_id):
        return "sandbox-" + task_run_id

    def interrupt(self, sandbox_id):
        pass

    def destroy(self, sandbox_id):
        pass


class Evidence:
    def command_exists(self, command_ref):
        return False

    def output_exists(self, output_ref):
        return False

    def lookup(self, execution_id):
        return None

    def exists(self, task_run_id, evidence_ref):
        return False


@dataclass
class Token:
    cancelled: bool = False

    @property
    def is_cancelled(self):
        return self.cancelled


class Fixture:
    def __init__(self, root, responses):
        self.store = SqliteControlStore.open(root / "control.sqlite", clock=lambda: NOW)
        self.repository = ControlStateRepository(self.store, Evidence())
        self.controller = TaskRunController(
            Sandbox(), clock=lambda: NOW, store=self.store,
            repository=self.repository, registry=TaskRunControllerRegistry(),
        )
        self.task_id = str(uuid4())
        self.controller.start(
            TaskRequest.from_dict({
                "goal": "find flag",
                "target": {"attachments": [{"kind": "local_path", "path": "sample"}]},
            }),
            ModelBackend.CODEX_CLI, str(uuid4()), task_run_id=self.task_id,
        )
        self.adapter = FakeCodexAdapter(responses)
        self.model = ModelInvocationCoordinator(
            self.adapter, self, Evidence(), audit=self.store, clock=lambda: NOW,
        )
        self.generator = OrderedIntentGenerator(
            self.controller, self.repository, self.store, self.model, clock=lambda: NOW,
        )
        self.token = Token()

    def remaining_seconds(self):
        return self.controller.control_state.task_run.deadline_at - NOW

    def generate(self, mutation_id):
        return asyncio.run(self.generator.generate(
            self.task_id, "find an exploitation path", mutation_id, self.token,
        ))


def response(count=6):
    return {"event": "result", "payload": {"intents": [
        {"description": f"approach {index}", "intent_type": None}
        for index in range(count)
    ]}}


def intent_worker(pipe, settings, payload, timeout):
    pipe.send({"payload": {"candidates": [
        {"description": "inspect the binary", "intent_type": "analysis"},
        {"description": "test the target", "intent_type": None},
    ]}})


def test_deepseek_adapter_normalizes_ordered_intents_without_tools(tmp_path):
    fixture = Fixture(tmp_path, [])
    request = ModelRequest(
        str(uuid4()), fixture.task_id, str(uuid4()),
        ModelRequestType.INTENT_GENERATION,
        fixture.controller.control_state.task_run.request.goal,
        fixture.controller.control_state.task_run.request.target,
        "find an exploitation path", (), (),
    )
    settings = DeepSeekSettings("deepseek-test", "test-key")
    payload = build_payload(request, settings)
    assert payload["response_format"] == {"type": "json_object"}
    assert "tools" not in payload
    adapter = DeepSeekModelAdapter(settings, worker=intent_worker)
    result = ModelInvocationCoordinator(adapter, fixture, Evidence()).invoke(
        request, fixture.token
    )
    assert isinstance(result, IntentGenerationResponse)
    assert [(item.description, item.intent_type) for item in result.candidates] == [
        ("inspect the binary", "analysis"), ("test the target", None)
    ]


def test_ordered_intents_are_durable_and_exact_replay_has_no_model_call(tmp_path):
    fixture = Fixture(tmp_path, [response()])
    mutation = str(uuid4())
    assert asyncio.run(fixture.generator.resume_unapplied(
        fixture.task_id, fixture.token
    )) is None
    result = fixture.generate(mutation)
    assert result.turn_id is not None
    assert result.model_error is None
    assert len(fixture.adapter.invocations) == 1
    state = fixture.controller.control_state
    assert [(item.description, item.priority) for item in state.intents] == [
        (f"approach {index}", index + 1) for index in range(6)
    ]
    assert len(fixture.store.list_model_invocations(fixture.task_id, result.turn_id)) == 1
    assert fixture.generate(mutation).replayed
    assert len(fixture.adapter.invocations) == 1
    assert len(fixture.controller.control_state.turns) == 1
    fixture.store.close()
    with SqliteControlStore.open(tmp_path / "control.sqlite", clock=lambda: NOW) as store:
        assert len(store.list_model_invocations(fixture.task_id, result.turn_id)) == 1
        assert store.load_control_state_rows(fixture.task_id).turns[0].turn_id == result.turn_id


def test_audited_supervisor_adapter_registers_first_turn(tmp_path):
    fixture = Fixture(tmp_path, [response(2)])
    adapter = AuditedTurnGenerator(fixture.generator)

    result = asyncio.run(
        adapter.generate_next(fixture.task_id, "find flag", fixture.token)
    )

    assert result.turn_id == fixture.controller.control_state.turns[0].turn_id
    assert result.model_error is None
    assert fixture.controller.control_state.turns[0].turn_target == "find flag"
    assert len(fixture.adapter.invocations) == 1


def test_audited_supervisor_adapter_replays_model_error_without_recall(tmp_path):
    fixture = Fixture(tmp_path, [
        {"event": "error", "code": "provider_failure", "message": "provider_down"},
        response(),
    ])
    adapter = AuditedTurnGenerator(fixture.generator)

    first = asyncio.run(adapter.generate_next(fixture.task_id, "find flag", fixture.token))
    second = asyncio.run(adapter.generate_next(fixture.task_id, "find flag", fixture.token))

    assert first.model_error is not None
    assert first.model_error.short_reason == "provider_down"
    assert second.model_error == first.model_error
    assert second.replayed
    assert not fixture.controller.control_state.turns
    assert len(fixture.adapter.invocations) == 1


def test_audited_supervisor_adapter_reuses_saved_result(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, [response(2)])
    adapter = AuditedTurnGenerator(fixture.generator)
    original = fixture.controller.commit_turn_registration

    def interrupted(*args):
        raise StorageError("simulated precommit interruption")

    monkeypatch.setattr(fixture.controller, "commit_turn_registration", interrupted)
    with pytest.raises(StorageError):
        asyncio.run(adapter.generate_next(fixture.task_id, "find flag", fixture.token))
    assert len(fixture.adapter.invocations) == 1
    assert len(fixture.store.list_unapplied_turn_generations(fixture.task_id)) == 1

    monkeypatch.setattr(fixture.controller, "commit_turn_registration", original)
    result = asyncio.run(adapter.generate_next(fixture.task_id, "find flag", fixture.token))
    assert result.replayed
    assert result.turn_id == fixture.controller.control_state.turns[0].turn_id
    assert len(fixture.adapter.invocations) == 1


def test_completed_generation_resumes_registration_without_model_recall(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, [response(2)])
    mutation = str(uuid4())
    original = fixture.controller.commit_turn_registration

    def interrupted(*args):
        raise StorageError("simulated precommit interruption")

    monkeypatch.setattr(fixture.controller, "commit_turn_registration", interrupted)
    with pytest.raises(StorageError):
        fixture.generate(mutation)
    records = fixture.store.list_unapplied_turn_generations(fixture.task_id)
    assert len(records) == 1 and records[0].completion is not None
    with pytest.raises(ModelInvocationRecoveryRequired):
        fixture.generate(str(uuid4()))
    monkeypatch.setattr(fixture.controller, "commit_turn_registration", original)
    fixture.store.close()
    with SqliteControlStore.open(tmp_path / "control.sqlite", clock=lambda: NOW) as store:
        repository = ControlStateRepository(store, Evidence())
        controller = TaskRunController(
            Sandbox(), clock=lambda: NOW, store=store,
            repository=repository, registry=TaskRunControllerRegistry(),
        )
        controller.attach(fixture.task_id)
        adapter = FakeCodexAdapter([])
        model = ModelInvocationCoordinator(
            adapter, fixture, Evidence(), audit=store, clock=lambda: NOW,
        )
        resumed = OrderedIntentGenerator(
            controller, repository, store, model, clock=lambda: NOW,
        )
        result = asyncio.run(resumed.resume_unapplied(fixture.task_id, Token()))
        assert result.replayed and result.turn_id is not None
        assert not adapter.invocations
        assert len(controller.control_state.turns) == 1
    assert len(fixture.adapter.invocations) == 1


def test_started_model_call_without_result_is_never_redispatched(tmp_path):
    fixture = Fixture(tmp_path, [response()])
    mutation = str(uuid4())

    def lost_result(request, timeout, cancellation):
        raise RuntimeError("simulated provider process loss")

    fixture.adapter.invoke = lost_result
    with pytest.raises(RuntimeError):
        fixture.generate(mutation)
    records = fixture.store.list_unapplied_turn_generations(fixture.task_id)
    assert len(records) == 1 and records[0].completion is None
    with pytest.raises(ModelInvocationRecoveryRequired):
        fixture.generate(mutation)
    with pytest.raises(ModelInvocationRecoveryRequired):
        fixture.generate(str(uuid4()))


def test_structural_correction_is_separately_audited(tmp_path):
    fixture = Fixture(tmp_path, [
        {"event": "result", "payload": {"intents": [4]}},
        response(2),
    ])
    result = fixture.generate(str(uuid4()))
    calls = fixture.store.list_model_invocations(fixture.task_id, result.turn_id)
    assert [item.completion.result_kind for item in calls] == [
        "invalid_structure", "intent_generation"
    ]
    assert calls[1].start.correction_of_model_request_id == calls[0].start.model_request_id


def test_cancelled_task_cannot_apply_completed_generation(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, [response()])
    mutation = str(uuid4())
    original = fixture.controller.commit_turn_registration
    monkeypatch.setattr(fixture.controller, "commit_turn_registration",
                        lambda *args: (_ for _ in ()).throw(StorageError("interrupted")))
    with pytest.raises(StorageError):
        fixture.generate(mutation)
    monkeypatch.setattr(fixture.controller, "commit_turn_registration", original)
    fixture.token.cancelled = True
    with pytest.raises(ModelInvocationRecoveryRequired):
        fixture.generate(mutation)
    assert not fixture.controller.control_state.turns


def test_deadline_prevents_stored_generation_from_registering(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, [response()])
    mutation = str(uuid4())
    original = fixture.controller.commit_turn_registration
    monkeypatch.setattr(fixture.controller, "commit_turn_registration",
                        lambda *args: (_ for _ in ()).throw(StorageError("interrupted")))
    with pytest.raises(StorageError):
        fixture.generate(mutation)
    monkeypatch.setattr(fixture.controller, "commit_turn_registration", original)
    fixture.generator._clock = lambda: NOW + 3600
    with pytest.raises(ModelInvocationRecoveryRequired):
        fixture.generate(mutation)
    assert not fixture.controller.control_state.turns


def test_changed_audited_context_digest_does_not_register(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, [response()])
    mutation = str(uuid4())
    original = fixture.controller.commit_turn_registration
    monkeypatch.setattr(fixture.controller, "commit_turn_registration",
                        lambda *args: (_ for _ in ()).throw(StorageError("interrupted")))
    with pytest.raises(StorageError):
        fixture.generate(mutation)
    monkeypatch.setattr(fixture.controller, "commit_turn_registration", original)
    fixture.store._connection.execute(
        "UPDATE turn_model_invocations SET context_sha256 = ?",
        ("0" * 64,),
    )
    with pytest.raises(ModelInvocationRecoveryRequired):
        fixture.generate(mutation)
    assert not fixture.controller.control_state.turns


def test_schema_7_upgrade_keeps_registered_turn_and_adds_generation_audit(tmp_path):
    fixture = Fixture(tmp_path, [response(2)])
    result = fixture.generate(str(uuid4()))
    task_id = fixture.task_id
    fixture.store.close()
    path = tmp_path / "control.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE t6_action_resource_ids")
        connection.execute("DROP TABLE t6_action_invocations")
        connection.execute("DROP TABLE turn_model_invocations")
        connection.execute("DELETE FROM schema_versions WHERE version >= 8")
    with SqliteControlStore.open(path, clock=lambda: NOW) as upgraded:
        state = upgraded.load_control_state_rows(task_id)
        assert state is not None
        assert state.turns[0].turn_id == result.turn_id
        assert [item.description for item in state.intents] == [
            "approach 0", "approach 1"
        ]
        assert upgraded.list_unapplied_turn_generations(task_id) == ()
        assert upgraded._connection.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0] == 10
