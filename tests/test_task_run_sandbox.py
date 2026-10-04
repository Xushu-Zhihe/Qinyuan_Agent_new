from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from docker.errors import NotFound

from Sandbox.models import CommandRequest, CommandResult
from Sandbox.settings import SandboxSettings
from Sandbox.task_run_sandbox import TaskRunSandbox, TaskRunSandboxError
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.controller import (
    TaskRunController,
    TaskRunControllerRegistry,
    CommittedStateReloadError,
    SandboxCleanupError,
    ControllerAlreadyClaimedError,
    ControllerNotClaimedError,
)
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import StorageError
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.runtime import InvalidTransition, TaskRunStatus
from agent_core.modeling import IntentCandidate
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy
from agent_core.domain import Plan, PlanStep, FailureType, PlanValidationStatus
from agent_core.control_store import PlanValidationRecordDraft


# These are the only three script families used by T2. FakeRuntime recognizes
# their exact contents and simulates state; it never evals code on the host.
WRITE = "from pathlib import Path; import sys; Path('/workspace/t2-marker').write_text(sys.argv[1])"
READ = "from pathlib import Path; import sys; p=Path('/workspace/t2-marker'); value=p.read_text() if p.exists() else ''; assert value == sys.argv[1]; print(value)"
WAIT = "import time; time.sleep(60)"


def command(script, value=None):
    return CommandRequest(
        "python3", ("-c", script) + (() if value is None else (value,)), 90
    )


@dataclass
class FakeImage:
    id: str = "sha256:image"
    attrs: dict = None

    def __post_init__(self):
        if self.attrs is None:
            self.attrs = {"Config": {"Volumes": None}}


class FakeContainer:
    def __init__(
        self, client, container_id: str, labels: dict[str, str], spec: dict[str, object]
    ):
        self.client = client
        self.id = container_id
        self.status = "created"
        self._labels = labels
        self.spec = spec
        self.attrs: dict[str, object] = {}
        self.marker = ""
        self.start_error = None
        self.stop_error = None
        self.remove_error = None
        self.start_running = True
        self.stop_completes = True
        self.remove_completes = True
        self.inspect_error = None
        self.overrides = lambda attrs: None
        self._refresh()

    def _refresh(self) -> None:
        spec = self.spec
        self.attrs = {
            "Id": self.id,
            "Image": self.client.image.id,
            "Config": {
                "Labels": self._labels,
                "User": spec["user"],
                "WorkingDir": spec["working_dir"],
                "Cmd": spec["command"],
                "Volumes": None,
            },
            "HostConfig": {
                "NetworkMode": spec["network_mode"],
                "ReadonlyRootfs": spec["read_only"],
                "Privileged": False,
                "AutoRemove": spec["auto_remove"],
                "CapDrop": spec["cap_drop"],
                "SecurityOpt": spec["security_opt"],
                "Tmpfs": spec["tmpfs"],
                "Memory": 536870912,
                "MemorySwap": 536870912,
                "NanoCpus": spec["nano_cpus"],
                "PidsLimit": spec["pids_limit"],
                "Binds": None,
                "Mounts": [],
                "PortBindings": None,
                "PublishAllPorts": False,
                "CapAdd": None,
                "Devices": None,
                "DeviceRequests": None,
                "VolumesFrom": None,
                "Links": None,
            },
            "NetworkSettings": {"Networks": {"none": {}}, "Ports": {}},
            "Mounts": [
                {"Type": "tmpfs", "Destination": "/workspace"},
                {"Type": "tmpfs", "Destination": "/tmp"},
            ],
        }
        self.overrides(self.attrs)

    def reload(self) -> None:
        self.client.events.append(("inspect", self.id))
        if self.inspect_error:
            raise self.inspect_error
        self._refresh()

    def start(self) -> None:
        self.client.events.append(("start", self.id))
        if self.start_running:
            self.status = "running"
        if self.start_error:
            raise self.start_error

    def stop(self, timeout: int) -> None:
        self.client.events.append(("stop", self.id, timeout))
        if self.stop_completes:
            self.status = "exited"
            self.marker = ""
        if self.stop_error:
            raise self.stop_error

    def remove(self, force: bool = False) -> None:
        self.client.events.append(("remove", self.id))
        if self.remove_error:
            raise self.remove_error
        if self.remove_completes:
            self.client.containers.containers_by_id.pop(self.id, None)


class FakeContainers:
    def __init__(self, client):
        self.client = client
        self.containers_by_id: dict[str, FakeContainer] = {}
        self.before_create_error = None
        self.after_create_error = None
        self.query_error = None
        self.on_create = lambda item: None

    def create(self, **kwargs):
        self.client.events.append(("create", kwargs["name"]))
        if self.before_create_error:
            raise self.before_create_error
        item = FakeContainer(self.client, uuid4().hex * 2, kwargs["labels"], kwargs)
        self.containers_by_id[item.id] = item
        self.on_create(item)
        if self.after_create_error:
            raise self.after_create_error
        return item

    def get(self, container_id: str):
        self.client.events.append(("get", container_id))
        if self.query_error:
            raise self.query_error
        if container_id not in self.containers_by_id:
            raise NotFound("missing")
        return self.containers_by_id[container_id]

    def list(self, all: bool = False, filters=None):
        self.client.events.append(("list",))
        if self.query_error:
            raise self.query_error
        labels = [x.split("=", 1) for x in filters["label"]]
        return [c for c in self.containers_by_id.values() if all_labels(c, labels)]


def all_labels(container, labels):
    return all(container._labels.get(key) == value for key, value in labels)


class FakeClient:
    def __init__(self, settings: SandboxSettings):
        self.settings = settings
        self.image = FakeImage()
        self.events: list[tuple] = []
        self.containers = FakeContainers(self)
        self.images = SimpleNamespace(get=lambda _name: self.image)

    def close(self):
        self.events.append(("close",))


class FakeRuntime:
    def __init__(self):
        self.settings = SandboxSettings()
        self.client = FakeClient(self.settings)
        self.execute_calls: list[tuple[str, tuple[str, ...]]] = []
        self.waiting = Event()
        self.release_wait = Event()
        self.before_admission = lambda: None
        self.stream_error = None

    def execute(
        self, sandbox_id: str, request: CommandRequest, *, admission=None
    ) -> CommandResult:
        if admission is not None:
            self.before_admission()
            with admission():
                self.execute_calls.append((sandbox_id, request.arguments))
                self.client.events.append(("execute", sandbox_id))
        c = self.client.containers.get(sandbox_id)
        script = request.arguments[1]
        output = ""
        if script == WRITE:
            c.marker = request.arguments[2]
        elif script == READ:
            assert c.marker == request.arguments[2]
            output = c.marker
        elif script == WAIT:
            self.waiting.set()
            assert self.release_wait.wait(5), (
                "test did not trigger explicit cancellation"
            )
        else:
            raise AssertionError("unapproved T2 test script")
        return CommandResult(sandbox_id, 0, output, "")

    def _close_output_stream(self, _sandbox_id: str) -> None:
        self.client.events.append(("interrupt", _sandbox_id))
        self.release_wait.set()
        if self.stream_error:
            raise self.stream_error

    def close(self) -> None:
        self.client.close()


def test_task_run_sandbox_creates_once_and_reuses_state_for_fixed_scripts():
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)
    task_run_id = str(uuid4())
    sandbox_id = adapter.create(task_run_id)

    first = adapter.execute(sandbox_id, command(WRITE, task_run_id))
    second = adapter.execute(sandbox_id, command(READ, task_run_id))

    assert first.sandbox_id == second.sandbox_id == sandbox_id
    assert second.stdout == task_run_id
    assert effects(runtime) == ["create", "start", "execute", "execute"]
    assert len(runtime.execute_calls) == 2
    with pytest.raises(TaskRunSandboxError, match="already_attempted"):
        adapter.create(task_run_id)


def test_task_run_sandbox_bind_checks_exact_owner_and_configuration():
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)
    task_run_id = str(uuid4())
    sandbox_id = adapter.create(task_run_id)

    adapter.bind(task_run_id, sandbox_id, terminal=False)
    with pytest.raises(TaskRunSandboxError, match="ownership_mismatch"):
        adapter.bind(str(uuid4()), sandbox_id, terminal=False)


def test_task_run_sandbox_inspect_presence_reports_owned_resource_without_cleanup():
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)
    task_run_id = str(uuid4())
    sandbox_id = adapter.create(task_run_id)
    before = effects(runtime)

    assert adapter.inspect_presence(task_run_id, sandbox_id) == "present"
    assert effects(runtime) == before == ["create", "start"]


def test_task_run_sandbox_inspect_presence_reports_not_found_without_create_or_cleanup():
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)

    assert adapter.inspect_presence(str(uuid4()), "missing-container") == "absent"
    assert effects(runtime) == []


@pytest.mark.parametrize("failure", ["ownership", "inspect"])
def test_task_run_sandbox_inspect_presence_reports_unknown_without_create_or_cleanup(failure):
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)
    task_run_id = str(uuid4())
    sandbox_id = adapter.create(task_run_id)
    if failure == "ownership":
        inspected_task_run_id = str(uuid4())
    else:
        inspected_task_run_id = task_run_id
        runtime.client.containers.containers_by_id[sandbox_id].inspect_error = TimeoutError(
            "inspect unavailable"
        )
    before = effects(runtime)

    assert adapter.inspect_presence(inspected_task_run_id, sandbox_id) == "unknown"
    assert effects(runtime) == before == ["create", "start"]


def test_task_run_sandbox_cancel_order_is_quiesce_stop_remove_and_is_idempotent():
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)
    task_run_id = str(uuid4())
    sandbox_id = adapter.create(task_run_id)

    adapter.destroy(sandbox_id)
    adapter.destroy(sandbox_id)

    assert effects(runtime) == ["create", "start", "stop", "remove"]


def test_task_run_sandbox_rejects_non_python_t2_commands_and_terminal_execution():
    runtime = FakeRuntime()
    adapter = TaskRunSandbox(runtime)
    task_run_id = str(uuid4())
    sandbox_id = adapter.create(task_run_id)

    with pytest.raises(ValueError, match="python3"):
        adapter.execute(sandbox_id, CommandRequest("sh", ("-c", "echo x")))
    adapter.bind(task_run_id, sandbox_id, terminal=True)
    with pytest.raises(TaskRunSandboxError, match="execution_rejected"):
        adapter.execute(sandbox_id, command(READ, ""))


def effects(runtime):
    return [
        e[0]
        for e in runtime.client.events
        if e[0] in {"create", "start", "stop", "remove", "execute"}
    ]


class Clock:
    now = 1800000000.0

    def now_utc(self):
        return self.now

    def __call__(self):
        return self.now


class Evidence:
    def command_exists(self, ref):
        return False

    def output_exists(self, ref):
        return False

    def lookup(self, ref):
        return None


@pytest.fixture
def app(request):
    runtime = FakeRuntime()
    clock = Clock()
    evidence = Evidence()
    path = (
        ":memory:"
        if getattr(request, "param", "file") == "memory"
        else Path(__file__).resolve().parents[1]
        / ".test"
        / "runtime-work"
        / f"t2-{uuid4()}.sqlite3"
    )
    store = SqliteControlStore.open(path, clock=clock)
    repository = ControlStateRepository(store, evidence)
    registry = TaskRunControllerRegistry()
    sandbox = TaskRunSandbox(runtime)
    controller = TaskRunController(
        sandbox, clock=clock, store=store, repository=repository, registry=registry
    )
    state = SimpleNamespace(
        runtime=runtime,
        clock=clock,
        evidence=evidence,
        store=store,
        repository=repository,
        registry=registry,
        sandbox=sandbox,
        controller=controller,
    )
    yield state
    store.close()


def start(app):
    app.controller.start(
        TaskRequest.from_dict(
            {
                "goal": "T2 fixed simulated lifecycle scripts",
                "target": {
                    "attachments": [{"kind": "local_path", "path": "t2-fixture"}]
                },
                "deadline_seconds": 120,
            }
        ),
        ModelBackend.CODEX_CLI,
        str(uuid4()),
    )
    return app.controller.task_run


def new_controller(app, sandbox=None):
    return TaskRunController(
        sandbox or TaskRunSandbox(app.runtime),
        clock=app.clock,
        store=app.store,
        repository=app.repository,
        registry=TaskRunControllerRegistry(),
    )


def test_persisted_two_intents_share_marker_but_another_task_cannot_see_it(app):
    task = start(app)
    registration = TurnManager.prepare_turn_registration(
        state=app.controller.control_state,
        mutation_id=str(uuid4()),
        turn_target="fixed script calls",
        candidates=(IntentCandidate("first"), IntentCandidate("second")),
    )
    app.controller.commit_turn_registration(registration, str(uuid4()))

    def work():
        return TurnWorkingCopy(app.controller.control_state, app.evidence)

    def commit(operation):
        return app.controller.commit_turn_operation(operation, str(uuid4()))

    first = commit(work().select_next_intent(registration.turn_id)).value
    written = app.sandbox.execute(task.sandbox_id, command(WRITE, task.task_run_id))
    # Simulated policy refusal advances the domain to the next Intent without
    # inventing Fact/Execution evidence for these lifecycle-only script probes.
    commit(work().register_plan(Plan("p", first, (PlanStep("probe"),))))
    commit(work().start_plan("p"))
    draft = PlanValidationRecordDraft(
        "v",
        "p",
        task.task_run_id,
        0,
        1,
        PlanValidationStatus.FAILED,
        FailureType.POLICY_REJECTED,
        "simulated refusal",
        None,
    )
    commit(work().append_plan_validation_record(draft))
    commit(work().record_command_generation_failure(plan_validation_id="v"))
    second = commit(work().select_next_intent(registration.turn_id)).value
    read = app.sandbox.execute(task.sandbox_id, command(READ, task.task_run_id))
    assert first != second
    assert written.sandbox_id == read.sandbox_id == app.controller.task_run.sandbox_id
    assert read.stdout == task.task_run_id
    assert effects(app.runtime).count("create") == 1
    other = new_controller(app, app.sandbox)
    other.start(task.request, ModelBackend.CODEX_CLI, str(uuid4()))
    assert other.task_run.sandbox_id != task.sandbox_id
    assert (
        app.sandbox.execute(other.task_run.sandbox_id, command(READ, "")).stdout == ""
    )


@pytest.mark.parametrize("finish", ["cancel", "deadline", "complete"])
def test_controller_commits_before_quiesce_and_cleans_once(app, finish, monkeypatch):
    task = start(app)
    if finish == "complete":
        app.controller.mark_candidate_found(str(uuid4()))
    if finish == "deadline":
        app.clock.now = task.deadline_at
    original = app.sandbox.quiesce

    def observe(sandbox_id):
        assert app.store.load_task_run(task.task_run_id).status not in {
            TaskRunStatus.RUNNING,
            TaskRunStatus.AWAITING_CONFIRMATION,
        }
        assert (
            app.controller.task_run.status
            == app.store.load_task_run(task.task_run_id).status
        )
        original(sandbox_id)
        with pytest.raises(TaskRunSandboxError, match="execution_rejected"):
            app.sandbox.execute(sandbox_id, command(READ, ""))

    monkeypatch.setattr(app.sandbox, "quiesce", observe)
    mutation = str(uuid4())
    action = {
        "cancel": lambda: app.controller.cancel(mutation),
        "deadline": lambda: app.controller.advance_time(mutation),
        "complete": lambda: app.controller.handle_control_message(
            {
                "type": "completion",
                "task_run_id": task.task_run_id,
                "status": "achieved",
            },
            mutation,
        ),
    }[finish]
    result = action()
    assert effects(app.runtime) == ["create", "start", "stop", "remove"]
    assert [e[2] for e in app.runtime.client.events if e[0] == "stop"] == [2]
    calls = list(app.runtime.client.events)
    app.repository.load_control_state = lambda _: (_ for _ in ()).throw(
        AssertionError("replay queried evidence")
    )
    assert action().commit.replayed
    assert app.controller.cancel(str(uuid4())).value == result.value
    assert app.runtime.client.events == calls


@pytest.mark.parametrize("finish", ["cancel", "deadline"])
@pytest.mark.parametrize("failure", ["validation", "transaction", "reload"])
def test_failed_terminal_commit_or_reload_does_not_touch_sandbox(
    app, monkeypatch, finish, failure
):
    task = start(app)
    before = app.controller.control_state
    mutation = str(uuid4())
    if finish == "deadline":
        app.clock.now = task.deadline_at
    action = (
        app.controller.cancel if finish == "cancel" else app.controller.advance_time
    )
    method = {
        "validation": (app.repository, "validate_candidate"),
        "transaction": (app.store, "commit"),
        "reload": (app.repository, "load_control_state"),
    }[failure]
    original = getattr(*method)

    def fail(*args):
        raise StorageError("injected failure")

    monkeypatch.setattr(*method, fail)
    calls = list(app.runtime.client.events)
    with pytest.raises(StorageError) as raised:
        action(mutation)
    assert app.runtime.client.events == calls
    if failure == "reload":
        assert isinstance(raised.value, CommittedStateReloadError)
        assert raised.value.commit is not None
        assert action(mutation).commit.replayed
        assert app.controller.cancel(str(uuid4())).commit.replayed
        assert app.runtime.client.events == calls
    else:
        assert app.controller.control_state == before
        assert app.store.load_task_run(task.task_run_id) == before.task_run
        assert app.store.load_committed_mutation(task.task_run_id, mutation) is None
    monkeypatch.setattr(*method, original)
    if failure != "reload":
        action(mutation)
        assert effects(app.runtime)[-2:] == ["stop", "remove"]


@pytest.mark.parametrize("finish", ["cancel", "deadline"])
def test_explicit_cancel_or_fake_clock_interrupts_simulated_wait_and_blocks_queued_execution(
    app, finish
):
    task = start(app)
    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(app.sandbox.execute, task.sandbox_id, command(WAIT))
        assert app.runtime.waiting.wait(3)
        queued = workers.submit(
            app.sandbox.execute, task.sandbox_id, command(WRITE, task.task_run_id)
        )
        if finish == "cancel":
            app.controller.cancel(str(uuid4()))
        else:
            app.clock.now = task.deadline_at
            app.controller.advance_time(str(uuid4()))
        running.result(timeout=3)
        with pytest.raises(TaskRunSandboxError, match="execution_rejected"):
            queued.result(timeout=3)
    assert len(app.runtime.execute_calls) == 1


def test_admission_is_rechecked_after_inspection(app):
    task = start(app)
    app.runtime.before_admission = lambda: app.controller.cancel(str(uuid4()))
    with pytest.raises(TaskRunSandboxError, match="execution_rejected"):
        app.sandbox.execute(task.sandbox_id, command(WRITE, task.task_run_id))
    assert app.runtime.execute_calls == []


@pytest.mark.parametrize(
    "case", ["missing", "stopped", "owner", "query", "config", "wrong_id"]
)
def test_active_recovery_rejects_untrusted_resources_without_create_start_or_delete(
    app, case, monkeypatch
):
    task = start(app)
    c = app.runtime.client.containers.get(task.sandbox_id)
    if case == "missing":
        app.runtime.client.containers.containers_by_id.pop(task.sandbox_id)
    elif case == "stopped":
        c.status = "exited"
    elif case == "owner":
        c._labels["qinyuan.task_run_id"] = str(uuid4())
    elif case == "query":
        app.runtime.client.containers.query_error = TimeoutError("inspect timed out")
    elif case == "config":
        c.spec["network_mode"] = "bridge"
    elif case == "wrong_id":
        c.id = "another-exact-id"
    controller = new_controller(app)
    before = effects(app.runtime)
    with pytest.raises(TaskRunSandboxError):
        controller.attach(task.task_run_id)
    assert effects(app.runtime) == before
    assert controller._persistent.registry.owner(task.task_run_id) is None
    assert app.store.load_task_run(task.task_run_id) == task


@pytest.mark.parametrize(
    "case",
    [
        "network",
        "extra_network",
        "mount",
        "socket",
        "port",
        "privileged",
        "image",
        "fingerprint",
        "user",
    ],
)
def test_recovery_verifies_actual_configuration_not_only_label_hash(app, case):
    task = start(app)
    c = app.runtime.client.containers.get(task.sandbox_id)

    def tamper(attrs):
        host = attrs["HostConfig"]
        if case == "network":
            host["NetworkMode"] = "bridge"
        elif case == "extra_network":
            attrs["NetworkSettings"]["Networks"]["lab"] = {}
        elif case == "mount":
            attrs["Mounts"].append({"Type": "bind", "Destination": "/host"})
        elif case == "socket":
            host["Binds"] = ["/var/run/docker.sock:/var/run/docker.sock"]
        elif case == "port":
            host["PortBindings"] = {"80/tcp": [{"HostPort": "8080"}]}
        elif case == "privileged":
            host["Privileged"] = True
        elif case == "image":
            attrs["Image"] = "sha256:other-image"
        elif case == "fingerprint":
            attrs["Config"]["Labels"]["qinyuan.config_sha256"] = "bad"
        else:
            attrs["Config"]["User"] = "0:0"

    c.overrides = tamper
    with pytest.raises(TaskRunSandboxError, match="configuration_mismatch"):
        new_controller(app).attach(task.task_run_id)
    assert effects(app.runtime) == ["create", "start"]


def test_valid_active_recovery_binds_exact_id_without_creation(app):
    task = start(app)
    controller = new_controller(app)
    state = controller.attach(task.task_run_id)
    assert state.task_run.sandbox_id == task.sandbox_id
    assert effects(app.runtime) == ["create", "start"]


def test_terminal_recovery_reads_result_with_missing_container_without_docker(app):
    task = start(app)
    first = app.controller.cancel(str(uuid4()))
    calls = list(app.runtime.client.events)
    adapter = TaskRunSandbox(app.runtime)
    controller = new_controller(app, adapter)
    controller.attach(task.task_run_id)
    assert controller.cancel(str(uuid4())).value == first.value
    assert app.runtime.client.events == calls
    with pytest.raises(TaskRunSandboxError, match="execution_rejected"):
        adapter.execute(task.sandbox_id, command(READ, ""))
    controller.cleanup_sandbox()  # precise missing ID is already clean
    assert effects(app.runtime) == ["create", "start", "stop", "remove"]


def test_stop_timeout_never_repeats_stop_and_explicit_cleanup_waits_for_stopped(app):
    task = start(app)
    c = app.runtime.client.containers.get(task.sandbox_id)
    c.stop_completes = False
    c.stop_error = TimeoutError("stop may still be pending")
    mutation = str(uuid4())
    with pytest.raises(SandboxCleanupError) as raised:
        app.controller.cancel(mutation)
    assert app.controller.task_run.status is TaskRunStatus.CANCELLED
    assert raised.value.original_error.outcome == "outcome_unknown"
    calls = list(app.runtime.client.events)
    assert app.controller.cancel(mutation).commit.replayed
    assert app.controller.cancel(str(uuid4())).value == "cancelled"
    assert app.runtime.client.events == calls
    with pytest.raises(SandboxCleanupError):
        app.controller.cleanup_sandbox()
    assert effects(app.runtime).count("stop") == 1
    assert "remove" not in effects(app.runtime)
    # Simulate Docker eventually finishing the *original* request.
    c.status = "exited"
    app.controller.cleanup_sandbox()
    assert effects(app.runtime) == ["create", "start", "stop", "remove"]


def test_terminal_recovery_does_not_issue_another_stop_to_running_container(app):
    task = start(app)
    c = app.runtime.client.containers.get(task.sandbox_id)
    c.stop_completes = False
    c.stop_error = TimeoutError("pending")
    with pytest.raises(SandboxCleanupError):
        app.controller.cancel(str(uuid4()))
    controller = new_controller(app)
    controller.attach(task.task_run_id)
    with pytest.raises(SandboxCleanupError):
        controller.cleanup_sandbox()
    assert effects(app.runtime).count("stop") == 1
    c.status = "exited"
    controller.cleanup_sandbox()
    assert effects(app.runtime)[-1] == "remove"


@pytest.mark.parametrize("error", ["timeout", "still_present", "ownership", "query"])
def test_cleanup_failures_do_not_erase_terminal_state_or_repeat_mutation(app, error):
    task = start(app)
    c = app.runtime.client.containers.get(task.sandbox_id)
    if error == "timeout":
        c.remove_error = TimeoutError("remove")
    elif error == "still_present":
        c.remove_completes = False
    elif error == "ownership":
        c._labels["qinyuan.task_run_id"] = str(uuid4())
    else:
        app.runtime.client.containers.query_error = TimeoutError("query")
    mutation = str(uuid4())
    with pytest.raises(SandboxCleanupError):
        app.controller.cancel(mutation)
    assert app.store.load_task_run(task.task_run_id).status is TaskRunStatus.CANCELLED
    calls = list(app.runtime.client.events)
    assert app.controller.cancel(mutation).commit.replayed
    assert app.runtime.client.events == calls
    if error in {"ownership", "query"}:
        assert effects(app.runtime) == ["create", "start"]


@pytest.mark.parametrize(
    "case",
    [
        "image_missing",
        "before_create",
        "after_create",
        "start_failed",
        "start_unknown",
        "query_failed",
        "cleanup_failed",
    ],
)
def test_creation_error_preserves_identity_and_never_reissues_create(app, case):
    containers = app.runtime.client.containers
    original = TimeoutError("original create/start failure")
    cleanup = TimeoutError("cleanup failure")
    if case == "image_missing":
        app.runtime.client.images.get = lambda _: (_ for _ in ()).throw(
            NotFound("image")
        )
    if case in {"before_create", "query_failed"}:
        containers.before_create_error = original
    if case == "query_failed":
        containers.query_error = TimeoutError("lookup")
    if case == "after_create":
        containers.after_create_error = original

    def configure(c):
        if case in {"start_failed", "cleanup_failed"}:
            c.start_running = False
            c.start_error = original
        if case == "cleanup_failed":
            c.remove_error = cleanup
        if case == "start_unknown":
            c.start_error = original  # running with lost acknowledgement

    containers.on_create = configure
    if case == "start_unknown":
        task = start(app)
        assert app.store.load_task_run(task.task_run_id).sandbox_id == task.sandbox_id
        assert effects(app.runtime).count("create") == 1
        return
    with pytest.raises(TaskRunSandboxError) as raised:
        start(app)
    error = raised.value
    assert app.store.load_task_run(error.task_run_id) is None
    assert app.registry.owner(error.task_run_id) is None
    assert error.outcome == (
        "creation_failed"
        if case in {"image_missing", "after_create", "start_failed"}
        else "outcome_unknown"
    )
    if case == "cleanup_failed":
        assert error.original_error is original
        assert error.cleanup_error is cleanup
        assert len(error.sandbox_ids) == 1
    if case != "image_missing":
        assert effects(app.runtime).count("create") == 1
    with pytest.raises(TaskRunSandboxError, match="already_attempted"):
        app.sandbox.create(error.task_run_id)


def test_create_timeout_can_only_adopt_one_matching_running_container(app):
    containers = app.runtime.client.containers
    containers.after_create_error = TimeoutError("lost create response")
    containers.on_create = lambda c: setattr(c, "status", "running")
    task = start(app)
    assert effects(app.runtime) == ["create"]
    assert app.store.load_task_run(task.task_run_id).sandbox_id == task.sandbox_id


def test_uncommitted_cleanup_uses_exact_identity_and_never_stops_unknown_running_resource(
    app,
):
    containers = app.runtime.client.containers
    containers.before_create_error = TimeoutError("lost create")
    with pytest.raises(TaskRunSandboxError) as raised:
        start(app)
    error = raised.value
    with pytest.raises(TaskRunSandboxError, match="outcome_unknown"):
        app.sandbox.cleanup_uncommitted(error.task_run_id)
    # The original request later materializes; manually model daemon state.
    containers.before_create_error = None
    labels = {
        **app.sandbox._labels(error.task_run_id),
        "qinyuan.config_sha256": app.sandbox._fingerprint(),
    }
    c = containers.create(**app.sandbox._spec(), name="late", labels=labels)
    c.status = "running"
    with pytest.raises(TaskRunSandboxError, match="outcome_unknown"):
        app.sandbox.cleanup_uncommitted(error.task_run_id)
    assert "stop" not in effects(app.runtime)
    c.status = "exited"
    app.sandbox.cleanup_uncommitted(error.task_run_id, c.id)
    assert c.id not in containers.containers_by_id


def test_start_transaction_failure_reports_original_and_compensating_cleanup_errors(
    app, monkeypatch
):
    error = StorageError("start write")
    monkeypatch.setattr(app.store, "commit", lambda _: (_ for _ in ()).throw(error))
    app.runtime.client.containers.on_create = lambda c: setattr(
        c, "remove_error", TimeoutError("remove")
    )
    with pytest.raises(SandboxCleanupError) as raised:
        start(app)
    assert raised.value.original_error is error
    assert raised.value.cleanup_error.outcome == "outcome_unknown"
    c = app.runtime.client.containers.get(raised.value.sandbox_id)
    assert app.store.load_task_run(c._labels["qinyuan.task_run_id"]) is None
    assert app.registry.owner(c._labels["qinyuan.task_run_id"]) is None


@pytest.mark.parametrize("quiesce_during_handshake", [False, True])
def test_real_sdk_adapter_path_with_fake_docker_never_prepares_network_and_closes_late_stream(
    monkeypatch, quiesce_during_handshake
):
    from Sandbox import docker_runtime

    client = FakeClient(SandboxSettings())
    api_calls = []

    class Stream:
        closed = False

        def __iter__(self):
            yield (b"simulated output", None)

        def close(self):
            self.closed = True

    stream = Stream()

    def exec_create(**kwargs):
        api_calls.append(kwargs)
        return {"Id": "exec-id"}

    client.api = SimpleNamespace(
        api_version="1.45",
        trust_env=True,
        exec_create=exec_create,
        exec_inspect=lambda _: {"ExitCode": 0},
    )
    client.version = lambda: {"ApiVersion": "1.45"}
    # No `networks` attribute: any attempt to create/inspect an internal network
    # through the previous constructor path fails this test.
    monkeypatch.setattr(docker_runtime.docker, "DockerClient", lambda **kwargs: client)
    sandbox = TaskRunSandbox.connect()
    assert client.api.trust_env is False
    assert client.api.timeout == 5
    sandbox_id = sandbox.create(str(uuid4()))

    def exec_start(**kwargs):
        if quiesce_during_handshake:
            sandbox.quiesce(sandbox_id)
        return stream

    monkeypatch.setattr(
        sandbox._runtime, "_stream_api", lambda: SimpleNamespace(exec_start=exec_start)
    )
    if quiesce_during_handshake:
        with pytest.raises(TaskRunSandboxError, match="outcome_unknown"):
            sandbox.execute(sandbox_id, command(READ, ""))
    else:
        result = sandbox.execute(sandbox_id, command(READ, ""))
        assert result.stdout == "simulated output"
    assert stream.closed
    assert sandbox._runtime._active_output_streams == {}
    assert api_calls[0]["container"] == sandbox_id
    assert api_calls[0]["workdir"] == "/workspace"
    assert api_calls[0]["user"] == "10001:10001"
    assert api_calls[0]["cmd"] == [
        "timeout",
        "--signal=TERM",
        "--kill-after=5s",
        "90s",
        "python3",
        "-c",
        READ,
        "",
    ]


def test_close_prevents_new_create_or_bind_and_does_not_destroy(app):
    task = start(app)
    app.sandbox.close()
    with pytest.raises(TaskRunSandboxError, match="closed"):
        app.sandbox.create(str(uuid4()))
    with pytest.raises(TaskRunSandboxError, match="closed"):
        app.sandbox.bind(task.task_run_id, task.sandbox_id, terminal=False)
    assert effects(app.runtime) == ["create", "start"]


def fail_cancel_reload(app, monkeypatch, finish="cancel"):
    task = start(app)
    controller = app.controller
    before = controller.control_state
    read = app.repository.load_control_state
    mutation_id = str(uuid4())
    if finish == "deadline":
        app.clock.now = task.deadline_at

    def unavailable(_task_run_id):
        raise StorageError("temporary state read failure")

    monkeypatch.setattr(app.repository, "load_control_state", unavailable)
    action = controller.cancel if finish == "cancel" else controller.advance_time
    with pytest.raises(CommittedStateReloadError) as raised:
        action(mutation_id)
    assert raised.value.commit is not None
    assert app.store.load_task_run(task.task_run_id).status in {
        TaskRunStatus.CANCELLED,
        TaskRunStatus.DEADLINE_EXCEEDED,
    }
    assert controller._persistent_state == before
    monkeypatch.setattr(app.repository, "load_control_state", read)
    return task, mutation_id, action


@pytest.mark.parametrize("app", ["memory", "file"], indirect=True)
@pytest.mark.parametrize("finish", ["cancel", "deadline"])
def test_reload_state_unblocks_explicit_cleanup_close_and_same_registry_attach(
    app, monkeypatch, finish
):
    task, mutation_id, replay = fail_cancel_reload(app, monkeypatch, finish)
    controller = app.controller
    container = app.runtime.client.containers.get(task.sandbox_id)
    assert container.status == "running"
    with pytest.raises(StorageError, match="cache is invalid"):
        controller.cleanup_sandbox()
    with pytest.raises(StorageError, match="cache is invalid"):
        controller.close()
    next_controller = TaskRunController(
        TaskRunSandbox(app.runtime),
        clock=app.clock,
        store=app.store,
        repository=app.repository,
        registry=app.registry,
    )
    with pytest.raises(ControllerAlreadyClaimedError):
        next_controller.attach(task.task_run_id)

    calls = list(app.runtime.client.events)
    database = tuple(app.store._connection.iterdump())
    assert replay(mutation_id).commit.replayed
    with pytest.raises(StorageError, match="cache is invalid"):
        controller.status()
    state = controller.reload_state()
    assert state.task_run == app.store.load_task_run(task.task_run_id)
    assert controller.control_state == state
    assert controller.status() == state.task_run.status
    assert app.registry.owner(task.task_run_id) is controller
    assert app.runtime.client.events == calls
    assert tuple(app.store._connection.iterdump()) == database
    assert replay(mutation_id).commit.replayed
    assert app.runtime.client.events == calls

    controller.cleanup_sandbox()
    assert task.sandbox_id not in app.runtime.client.containers.containers_by_id
    assert effects(app.runtime) == ["create", "start", "stop", "remove"]
    assert tuple(app.store._connection.iterdump()) == database
    controller.close()
    assert app.registry.owner(task.task_run_id) is None
    next_controller.attach(task.task_run_id)
    assert next_controller.status() == state.task_run.status
    assert app.registry.owner(task.task_run_id) is next_controller
    next_controller.close()


@pytest.mark.parametrize("app", ["memory"], indirect=True)
@pytest.mark.parametrize(
    "failure",
    ["query", "missing", "foreign_task", "foreign_checkpoint", "sandbox", "stale"],
)
def test_reload_state_failure_keeps_old_cache_invalid_without_resource_or_storage_effects(
    app, monkeypatch, failure
):
    task, mutation_id, replay = fail_cancel_reload(app, monkeypatch)
    controller = app.controller
    old_state = controller._persistent_state
    old_snapshot = controller._persistent_snapshot
    read = app.repository.load_control_state
    valid = read(task.task_run_id)
    if failure == "foreign_task":
        invalid = replace(
            valid, task_run=replace(valid.task_run, task_run_id=str(uuid4()))
        )
    elif failure == "foreign_checkpoint":
        invalid = replace(
            valid,
            latest_checkpoint=replace(
                valid.latest_checkpoint, task_run_id=str(uuid4())
            ),
        )
    elif failure == "sandbox":
        invalid = replace(
            valid, task_run=replace(valid.task_run, sandbox_id="other-sandbox")
        )
    elif failure == "stale":
        # old_state is checkpoint 1; move it to 2 so checkpoint 1 is stale.
        controller._persistent_state = valid
        old_state = valid
        invalid = replace(
            valid, latest_checkpoint=replace(valid.latest_checkpoint, sequence=1)
        )
    else:
        invalid = None

    def bad_read(_task_run_id):
        if failure == "query":
            raise OSError("temporarily unavailable")
        return invalid

    monkeypatch.setattr(app.repository, "load_control_state", bad_read)
    calls = list(app.runtime.client.events)
    database = tuple(app.store._connection.iterdump())
    with pytest.raises(StorageError) as raised:
        controller.reload_state()
    assert not isinstance(raised.value, CommittedStateReloadError)
    assert controller._persistent_state == old_state
    assert controller._persistent_snapshot == old_snapshot
    assert app.registry.owner(task.task_run_id) is controller
    with pytest.raises(StorageError, match="cache is invalid"):
        controller.cleanup_sandbox()
    assert replay(mutation_id).commit.replayed
    assert app.runtime.client.events == calls
    assert tuple(app.store._connection.iterdump()) == database
    monkeypatch.setattr(app.repository, "load_control_state", read)
    controller.reload_state()
    controller.cleanup_sandbox()
    controller.close()


@pytest.mark.parametrize("app", ["memory"], indirect=True)
def test_reload_state_revalidates_external_evidence_before_restoring_cache(
    app, monkeypatch
):
    task = start(app)
    registration = TurnManager.prepare_turn_registration(
        state=app.controller.control_state,
        mutation_id=str(uuid4()),
        turn_target="reload evidence",
        candidates=(IntentCandidate("first"),),
    )
    app.controller.commit_turn_registration(registration, str(uuid4()))

    def work():
        return TurnWorkingCopy(app.controller.control_state, app.evidence)

    def commit(operation):
        return app.controller.commit_turn_operation(operation, str(uuid4()))

    intent = commit(work().select_next_intent(registration.turn_id)).value
    commit(work().register_plan(Plan("reload-plan", intent, (PlanStep("probe"),))))
    commit(work().start_plan("reload-plan"))
    commands = {"reload-command"}
    monkeypatch.setattr(app.evidence, "command_exists", lambda ref: ref in commands)
    commit(
        work().append_plan_validation_record(
            PlanValidationRecordDraft(
                "reload-validation",
                "reload-plan",
                task.task_run_id,
                0,
                1,
                PlanValidationStatus.SUCCEEDED,
                None,
                "verified command ref",
                "reload-command",
            )
        )
    )
    # Lose evidence after terminal commit, before the automatic repository load.
    read = app.repository.load_control_state

    def lose_evidence(task_run_id):
        commands.clear()
        return read(task_run_id)

    monkeypatch.setattr(app.repository, "load_control_state", lose_evidence)
    mutation = str(uuid4())
    with pytest.raises(CommittedStateReloadError):
        app.controller.cancel(mutation)
    monkeypatch.setattr(app.repository, "load_control_state", read)
    calls = list(app.runtime.client.events)
    database = tuple(app.store._connection.iterdump())
    with pytest.raises(StorageError, match="invalid command_ref"):
        app.controller.reload_state()
    with pytest.raises(StorageError, match="cache is invalid"):
        app.controller.status()
    assert app.controller.cancel(mutation).commit.replayed
    commands.add("reload-command")
    assert app.controller.reload_state().task_run.status is TaskRunStatus.CANCELLED
    assert tuple(app.store._connection.iterdump()) == database
    assert app.runtime.client.events == calls
    app.controller.cleanup_sandbox()
    app.controller.close()


@pytest.mark.parametrize("app", ["memory"], indirect=True)
@pytest.mark.parametrize("case", ["unbound", "closed", "not_owner", "legacy"])
def test_reload_state_requires_current_open_persistent_owner(app, case):
    if case == "legacy":
        controller = TaskRunController(app.sandbox)
    else:
        controller = app.controller
        if case != "unbound":
            task = start(app)
            if case == "closed":
                controller.cancel(str(uuid4()))
                controller.close()
            else:
                app.registry.release(task.task_run_id, controller)
                app.registry.claim(task.task_run_id, object())
    calls = list(app.runtime.client.events)
    with pytest.raises((InvalidTransition, ControllerNotClaimedError)):
        controller.reload_state()
    assert app.runtime.client.events == calls
