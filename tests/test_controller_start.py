from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import StorageError, TargetNetworkBinding
from agent_core.controller import (
    TaskRunController,
    TaskRunControllerRegistry,
)
from agent_core.web_contracts import D7WebAuthorization, D7WebRunConfig
from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.runtime import TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_contracts import D1RoleConfig


ORIGIN = "http://node4.anna.nssctf.cn:23522"


class _Evidence:
    def lookup(self, _execution_id):
        return None


class _Sandbox:
    def __init__(self, *, fail_create: bool = False):
        self.fail_create = fail_create
        self.configured = []
        self.created = []
        self.destroyed = []

    def configure_target_network(self, task_run_id, binding):
        self.configured.append((task_run_id, binding))

    def create(self, task_run_id):
        self.created.append(task_run_id)
        if self.fail_create:
            raise RuntimeError("sandbox create failed")
        return f"sandbox-{task_run_id}"

    def destroy(self, sandbox_id):
        self.destroyed.append(sandbox_id)

    def interrupt(self, _sandbox_id):
        pass


class _Network:
    def __init__(self):
        self.created = []
        self.bound = []
        self.destroyed = []

    def create(self, task_run_id, target_url):
        self.created.append((task_run_id, target_url))
        digest = lambda name: hashlib.sha256(f"{task_run_id}/{name}".encode()).hexdigest()
        return TargetNetworkBinding(
            digest("private"),
            digest("egress"),
            digest("forwarder"),
            hashlib.sha256(target_url.encode()).hexdigest(),
            target_url,
        )

    def bind(self, task_run_id, binding, *, terminal):
        self.bound.append((task_run_id, binding, terminal))

    def destroy(self, task_run_id, binding):
        self.destroyed.append((task_run_id, binding))


def _config(task_run_id: str, origin: str = ORIGIN) -> D7WebRunConfig:
    return D7WebRunConfig(
        task_run_id=task_run_id,
        request_id=str(uuid4()),
        endpoint=origin,
        authorization=D7WebAuthorization(
            authorization_id=str(uuid4()),
            allowed_origin=origin,
            allowed_methods=("GET", "POST"),
        ),
        outer_role=D1RoleConfig(
            "outer_planning", "deepseek", "deepseek-chat", "qinyuan.d7.outer.v1"
        ),
        inner_role=D1RoleConfig(
            "inner_action", "deepseek", "deepseek-chat", "qinyuan.d7.inner.v1"
        ),
        d4_budget=D4BudgetPolicy(),
        model_transport_config_ref="sha256:" + "a" * 64,
    )


def _request(
    endpoint: str = ORIGIN,
    *,
    attachments: list[dict[str, str]] | None = None,
    endpoints: list[dict[str, str]] | None = None,
) -> TaskRequest:
    return TaskRequest.from_dict(
        {
            "goal": "Find the flag on the authorized Web origin",
            "target": {
                "endpoints": endpoints if endpoints is not None else [{"url": endpoint}],
                "attachments": attachments or [],
            },
            "deadline_seconds": 120,
        }
    )


def _open_controller(tmp_path: Path, sandbox, network):
    store = SqliteControlStore.open(tmp_path / "control.sqlite3", clock=lambda: 1_800_000_000.0)
    registry = TaskRunControllerRegistry()
    controller = TaskRunController(
        sandbox,
        clock=lambda: 1_800_000_000.0,
        store=store,
        repository=ControlStateRepository(store, _Evidence()),
        registry=registry,
        target_network_adapter=network,
    )
    return store, registry, controller


def test_web_start_atomically_persists_config_and_exact_network_binding(tmp_path):
    task_run_id = str(uuid4())
    selected = _config(task_run_id)
    sandbox = _Sandbox()
    network = _Network()
    store, registry, controller = _open_controller(tmp_path, sandbox, network)
    try:
        started = controller.start(
            _request(),
            ModelBackend.DEEPSEEK_OPENAI,
            str(uuid4()),
            d7_web_config=selected,
        )

        state = controller.control_state
        assert started.value == task_run_id
        assert store.load_d7_web_run_config(task_run_id) == selected
        assert state.latest_checkpoint.sequence == 1
        assert state.task_run.status is TaskRunStatus.RUNNING
        assert state.task_run.target_network.target_url == ORIGIN
        assert network.created == [(task_run_id, ORIGIN)]
        assert sandbox.configured == [(task_run_id, state.task_run.target_network)]
        assert registry.owner(task_run_id) is controller
    finally:
        if controller.status() is TaskRunStatus.RUNNING:
            controller.cancel(str(uuid4()))
        controller.cleanup_sandbox()
        controller.close()
        store.close()


@pytest.mark.parametrize(
    ("request_kwargs", "config_origin"),
    [
        (
            {
                "endpoints": [],
                "attachments": [{"kind": "local_path", "path": "input.txt"}],
            },
            ORIGIN,
        ),
        ({"endpoints": [{"url": ORIGIN}, {"url": "http://example.test:80"}]}, ORIGIN),
        ({"endpoint": "http://node4.anna.nssctf.cn:23523"}, ORIGIN),
        ({"attachments": [{"kind": "local_path", "path": "input.txt"}]}, ORIGIN),
    ],
)
def test_web_start_rejects_scope_mismatch_before_creating_resources(
    tmp_path, request_kwargs, config_origin
):
    task_run_id = str(uuid4())
    sandbox = _Sandbox()
    network = _Network()
    store, registry, controller = _open_controller(tmp_path, sandbox, network)
    try:
        with pytest.raises(ValueError, match="D7 Web requires one exact authorized origin"):
            controller.start(
                _request(**request_kwargs),
                ModelBackend.DEEPSEEK_OPENAI,
                str(uuid4()),
                task_run_id=task_run_id,
                d7_web_config=_config(task_run_id, config_origin),
            )
        assert sandbox.created == []
        assert network.created == []
        assert registry.owner(task_run_id) is None
    finally:
        store.close()


def test_web_start_commit_failure_compensates_sandbox_and_target_network(
    tmp_path, monkeypatch
):
    task_run_id = str(uuid4())
    sandbox = _Sandbox()
    network = _Network()
    store, registry, controller = _open_controller(tmp_path, sandbox, network)
    monkeypatch.setattr(
        store,
        "commit",
        lambda _mutation: (_ for _ in ()).throw(StorageError("commit failed")),
    )
    try:
        with pytest.raises(StorageError, match="commit failed"):
            controller.start(
                _request(),
                ModelBackend.DEEPSEEK_OPENAI,
                str(uuid4()),
                d7_web_config=_config(task_run_id),
            )

        assert sandbox.destroyed == [f"sandbox-{task_run_id}"]
        assert len(network.destroyed) == 1
        assert network.destroyed[0][0] == task_run_id
        assert registry.owner(task_run_id) is None
        assert store.load_task_run(task_run_id) is None
        assert store.load_d7_web_run_config(task_run_id) is None
    finally:
        # Controller has no bound TaskRun after compensation failure
        if controller._persistent_task_run_id is not None:
            controller.close()
        store.close()


def test_web_start_sandbox_create_failure_compensates_network(tmp_path):
    task_run_id = str(uuid4())
    sandbox = _Sandbox(fail_create=True)
    network = _Network()
    store, registry, controller = _open_controller(tmp_path, sandbox, network)
    try:
        with pytest.raises(RuntimeError, match="sandbox create failed"):
            controller.start(
                _request(),
                ModelBackend.DEEPSEEK_OPENAI,
                str(uuid4()),
                d7_web_config=_config(task_run_id),
            )
        assert len(network.destroyed) == 1
        assert registry.owner(task_run_id) is None
        assert store.load_task_run(task_run_id) is None
    finally:
        # Controller has no bound TaskRun after compensation failure
        if controller._persistent_task_run_id is not None:
            controller.close()
        store.close()

