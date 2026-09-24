from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from MCPs.file_tool_adapter import FileMCPPlanner
from Sandbox.models import CommandResult
from Sandbox.task_run_sandbox import TaskRunSandbox
from Sandbox.tool_runtime import TaskRunToolRuntime
from agent_adapters.file_tool_recorder import EvidenceStorageError, FileToolRecorder
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.domain import (
    ExecutionStatus,
    FailureAction,
    FailureType,
    Plan,
    PlanStep,
)
from agent_core.file_inspection import FILE_INSPECTION_SCRIPT, inspection_arguments
from agent_core.modeling import IntentCandidate
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.tool_contracts import (
    MCPCommand,
    ToolAuthorization,
    ToolCallError,
    ToolRecoveryRequired,
    ToolRequest,
)
from agent_core.tool_gateway import ToolGateway
from agent_core.tool_policy import FileToolPolicy
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy
from test_task_run_sandbox import FakeRuntime


@dataclass
class Clock:
    now: float = 1_800_000_000

    def __call__(self):
        return self.now


@dataclass
class Token:
    is_cancelled: bool = False


class Planner:
    def __init__(self):
        self.calls = []
        self.error = None
        self.tamper = False
        self.wait = False

    async def prepare(self, request, timeout_seconds):
        self.calls.append(request)
        if self.wait:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        args = (
            ("-c", "print('untrusted')")
            if self.tamper
            else inspection_arguments(request.path)
        )
        return MCPCommand("python3", args, request.timeout_seconds)


class FileRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.result_code = 0
        self.confirmed = True
        self.timed_out = False
        self.stdout = "ASCII text\n"
        self.wait = False
        self.raise_error = False
        self.on_execute = lambda: None
        self.files = {}
        self.dispatched_timeouts = []

    def execute(self, sandbox_id, request, *, admission=None):
        if admission is not None:
            with admission():
                self.execute_calls.append((sandbox_id, request.arguments))
                self.client.events.append(("execute", sandbox_id))
                self.dispatched_timeouts.append(request.timeout_seconds)
        assert request.arguments == inspection_arguments("/workspace/input/sample.txt")
        self.on_execute()
        if self.raise_error:
            raise OSError("private SDK diagnostic")
        if self.wait:
            self.waiting.set()
            assert self.release_wait.wait(5)
        exists = request.arguments[-1] in self.files.get(sandbox_id, {})
        return CommandResult(
            sandbox_id,
            self.result_code if exists else 40,
            self.stdout if exists else "",
            "",
            timed_out=self.timed_out,
            termination_confirmed=self.confirmed,
        )


class Scenario:
    def __init__(self, root: Path, planner=None):
        self.clock = Clock()
        self.token = Token()
        self.runtime = FileRuntime()
        self.sandbox = TaskRunSandbox(self.runtime)
        self.recorder = FileToolRecorder(
            root / "evidence", sensitive_values=("secret-value",)
        )
        self.store = SqliteControlStore.open(root / "control.sqlite", clock=self.clock)
        self.repository = ControlStateRepository(self.store, self.recorder)
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
        )
        self.controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "inspect local fixture",
                    "target": {
                        "attachments": [
                            {
                                "kind": "local_path",
                                "path": "sample.txt",
                            }
                        ]
                    },
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
        )
        self.runtime.files[self.state.task_run.sandbox_id] = {
            "/workspace/input/sample.txt": b"local sample text\n"
        }
        self.planner = planner or Planner()
        self.gateway = ToolGateway(
            controller=self.controller,
            policy=FileToolPolicy(),
            planner=self.planner,
            sandbox=TaskRunToolRuntime(self.sandbox),
            recorder=self.recorder,
            clock=self.clock,
        )
        registration = TurnManager.prepare_turn_registration(
            state=self.state,
            mutation_id=str(uuid4()),
            turn_target="inspect local file",
            candidates=(
                IntentCandidate("file type"),
                IntentCandidate("another file attempt"),
            ),
        )
        self.controller.commit_turn_registration(registration, str(uuid4()))
        intent = self.commit(self.work.select_next_intent(registration.turn_id)).value
        self.commit(
            self.work.register_plan(
                Plan(
                    "plan",
                    intent,
                    (PlanStep("identify file"), PlanStep("inspect again")),
                )
            )
        )
        self.commit(self.work.start_plan("plan"))

    @property
    def state(self):
        return self.controller.control_state

    @property
    def work(self):
        return TurnWorkingCopy(self.state, self.recorder)

    def commit(self, operation):
        return self.controller.commit_turn_operation(operation, str(uuid4()))

    def request(self, **changes):
        value = ToolRequest(
            str(uuid4()),
            self.state.task_run.task_run_id,
            "plan",
            0,
            "file.inspect_file",
            "/workspace/input/sample.txt",
        )
        return replace(value, **changes)

    @property
    def authorization(self):
        return ToolAuthorization(
            "test-authorization",
            self.state.task_run.task_run_id,
            ("/workspace/input/sample.txt",),
        )

    async def call(self, request=None, authorization=None):
        return await self.gateway.call(
            request or self.request(), authorization or self.authorization, self.token
        )

    def close(self):
        self.runtime.release_wait.set()
        if self.controller.status() not in TERMINAL_STATUSES:
            self.controller.cancel(str(uuid4()))
        self.controller.close()
        self.sandbox.close()
        self.store.close()


@pytest.fixture
def t3_root():
    root = (
        Path(__file__).resolve().parents[1] / ".test" / "runtime-work" / f"t3-{uuid4()}"
    )
    root.mkdir(parents=True)
    return root


@pytest.fixture
def scenario(t3_root):
    value = Scenario(t3_root)
    yield value
    value.close()


def test_real_mcp_to_fake_docker_and_durable_control(t3_root):
    async def run():
        s = Scenario(t3_root, FileMCPPlanner())
        try:
            request = s.request()
            result = await s.call(request)
            assert result.record.failure_type is None, result.record
            assert result.record.stdout == "ASCII text\n"
            assert result.record.failure_type is None
            assert s.state.executions[0].status is ExecutionStatus.SUCCEEDED
            assert s.state.facts == ()
            assert (
                s.recorder.lookup(request.request_id).task_run_id == request.task_run_id
            )
            assert s.repository.load_control_state(request.task_run_id) == s.state
            before = len(s.runtime.client.events)
            assert await s.call(request) == result
            assert len(s.runtime.client.events) == before
            assert len(s.runtime.execute_calls) == 1
            assert s.runtime.execute_calls[0][0] == s.state.task_run.sandbox_id
            stored = s.recorder.load_command(result.record.command_ref)
            assert stored.arguments == inspection_arguments(request.path)
        finally:
            s.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "path",
    [
        "sample.txt",
        "/workspace/input/../secret",
        "/workspace/input/./sample.txt",
        "/workspace/input//sample.txt",
        "/workspace/input2/sample.txt",
        "/etc/passwd",
        "/workspace/input/other.txt",
        "/workspace/input/sample.txt\x00",
    ],
)
def test_policy_rejects_before_mcp_and_docker(scenario, path):
    result = asyncio.run(scenario.call(scenario.request(path=path)))
    assert result.record.failure_type is FailureType.POLICY_REJECTED
    assert scenario.planner.calls == []
    assert scenario.runtime.execute_calls == []
    assert scenario.state.executions == ()
    assert len(scenario.state.plan_validation_records) == 1
    assert all(
        f.failure_type is not FailureType.TARGET_MISMATCH
        for f in scenario.state.failure_summaries
    )


def test_wrong_tool_and_authorization_are_rejected(scenario):
    result = asyncio.run(scenario.call(scenario.request(tool_name="shell.execute")))
    assert result.record.failure_type is FailureType.POLICY_REJECTED
    assert not scenario.planner.calls


def test_cross_task_scope_is_rejected(scenario):
    authorization = replace(scenario.authorization, task_run_id=str(uuid4()))
    result = asyncio.run(scenario.call(authorization=authorization))
    assert result.record.failure_type is FailureType.POLICY_REJECTED
    assert not scenario.planner.calls


def test_mcp_command_tampering_is_rejected(scenario):
    scenario.planner.tamper = True
    result = asyncio.run(scenario.call())
    assert result.record.short_reason == "mcp_command_rejected"
    assert not scenario.state.executions
    assert not scenario.runtime.execute_calls


@pytest.mark.parametrize(
    "code,category,action",
    [
        (40, FailureType.PARAMETER_ERROR, FailureAction.TRY_NEXT_INTENT),
        (41, FailureType.POLICY_REJECTED, FailureAction.TRY_NEXT_INTENT),
        (42, FailureType.ENVIRONMENT_FAULT, FailureAction.RETRY_EXECUTION),
        (43, FailureType.OUTCOME_UNKNOWN, FailureAction.TRY_NEXT_INTENT),
        (137, FailureType.OUTCOME_UNKNOWN, FailureAction.TRY_NEXT_INTENT),
    ],
)
def test_execution_error_classification(scenario, code, category, action):
    scenario.runtime.result_code = code
    result = asyncio.run(scenario.call())
    assert result.record.failure_type is category
    assert result.action is action
    assert len(scenario.state.executions) == 1
    assert all(
        f.failure_type is not FailureType.TARGET_MISMATCH
        for f in scenario.state.failure_summaries
    )


def test_explicit_execution_retry_reuses_command_and_is_bounded(scenario):
    async def run():
        scenario.runtime.result_code = 42
        first = await scenario.call()
        second = await scenario.call()
        assert first.action is FailureAction.RETRY_EXECUTION
        assert second.action is FailureAction.TRY_NEXT_INTENT
        assert first.record.command_ref == second.record.command_ref
        assert len(scenario.planner.calls) == 1
        assert len(scenario.state.plan_validation_records) == 1
        assert len(scenario.runtime.execute_calls) == 2
        with pytest.raises(ToolRecoveryRequired):
            await scenario.call()

    asyncio.run(run())


def test_explicit_mcp_retry_then_success(scenario):
    async def run():
        scenario.planner.error = ToolCallError(
            FailureType.ENVIRONMENT_FAULT, "mcp_unavailable"
        )
        first = await scenario.call()
        assert first.action is FailureAction.RETRY_COMMAND_GENERATION
        assert not scenario.state.executions
        scenario.planner.error = None
        second = await scenario.call()
        assert second.record.failure_type is None
        assert [v.attempt for v in scenario.state.plan_validation_records] == [1, 2]

    asyncio.run(run())


def test_request_id_conflict_and_incomplete_receipt(scenario):
    async def run():
        request = scenario.request()
        scenario.recorder.begin(
            request, scenario.authorization, scenario.state.task_run.sandbox_id
        )
        with pytest.raises(ToolRecoveryRequired):
            await scenario.call(request)
        with pytest.raises(EvidenceStorageError):
            await scenario.call(replace(request, timeout_seconds=20))
        assert not scenario.planner.calls
        assert not scenario.runtime.execute_calls

    asyncio.run(run())


@pytest.mark.parametrize("deadline", [False, True])
def test_cancel_or_deadline_during_execution_commits_before_cleanup(scenario, deadline):
    async def run():
        scenario.runtime.wait = True
        request = scenario.request()
        task = asyncio.create_task(scenario.call(request))
        while not scenario.runtime.waiting.is_set():
            await asyncio.sleep(0.01)
        container = scenario.runtime.client.containers.get(
            scenario.state.task_run.sandbox_id
        )
        original_stop = container.stop

        def stop(*args, **kwargs):
            assert scenario.store.load_task_run(request.task_run_id).status in {
                TaskRunStatus.CANCELLED,
                TaskRunStatus.DEADLINE_EXCEEDED,
            }
            return original_stop(*args, **kwargs)

        container.stop = stop
        if deadline:
            scenario.clock.now = scenario.state.task_run.deadline_at
        else:
            scenario.token.is_cancelled = True
        result = await task
        expected = (
            TaskRunStatus.DEADLINE_EXCEEDED if deadline else TaskRunStatus.CANCELLED
        )
        assert scenario.state.task_run.status is expected
        assert result.record.timed_out if deadline else result.record.cancelled
        assert scenario.state.executions[0].output_ref is None
        assert scenario.recorder.output_exists(request.request_id)
        count = len(scenario.runtime.client.events)
        assert await scenario.call(request) == result
        assert len(scenario.runtime.client.events) == count

    asyncio.run(run())


def test_cancel_while_mcp_generates_has_no_execution(scenario):
    async def run():
        scenario.planner.wait = True
        task = asyncio.create_task(scenario.call())
        while not scenario.planner.calls:
            await asyncio.sleep(0.01)
        scenario.token.is_cancelled = True
        result = await task
        assert result.record.cancelled
        assert not scenario.state.executions
        assert not scenario.runtime.execute_calls

    asyncio.run(run())


def test_unknown_termination_closes_admission(scenario):
    scenario.runtime.confirmed = False
    result = asyncio.run(scenario.call())
    assert result.record.failure_type is FailureType.OUTCOME_UNKNOWN
    assert not scenario.sandbox._binding(scenario.state.task_run.sandbox_id).accepting


def test_known_timeout_retains_admission_without_automatic_retry(scenario):
    scenario.runtime.result_code = 124
    scenario.runtime.timed_out = True
    result = asyncio.run(scenario.call())
    assert result.record.failure_type is FailureType.OUTCOME_UNKNOWN
    assert result.record.timed_out
    assert scenario.sandbox._binding(scenario.state.task_run.sandbox_id).accepting
    assert len(scenario.runtime.execute_calls) == 1


def test_output_bounding_redaction_and_integrity(scenario):
    scenario.runtime.stdout = "secret-value " + "a" * 70000
    result = asyncio.run(scenario.call())
    assert result.record.stdout_truncated
    assert len(result.record.stdout.encode()) == 65536
    assert result.record.stdout.startswith("[REDACTED]")
    for path in scenario.recorder.directory.glob("*.json"):
        assert "secret-value" not in path.read_text()
    path = scenario.recorder.directory / f"output-{result.record.output_ref}.json"
    original = path.read_bytes()
    payload = json.loads(path.read_text())
    payload["payload"]["stdout"] = "changed"
    path.write_text(json.dumps(payload))
    with pytest.raises(EvidenceStorageError):
        scenario.recorder.output_exists(result.record.output_ref)
    path.write_bytes(original)


def test_evidence_write_failure_prevents_dispatch(scenario, monkeypatch):
    def fail(*args):
        raise EvidenceStorageError("disk full")

    monkeypatch.setattr(scenario.recorder, "save_command", fail)
    with pytest.raises(EvidenceStorageError):
        asyncio.run(scenario.call())
    assert not scenario.runtime.execute_calls
    assert not scenario.state.executions


def test_running_state_is_committed_before_dispatch(scenario):
    def inspect():
        state = scenario.store.load_control_state_rows(
            scenario.state.task_run.task_run_id
        )
        assert state.executions[0].status is ExecutionStatus.RUNNING

    scenario.runtime.on_execute = inspect
    asyncio.run(scenario.call())


def test_dispatch_exception_has_unknown_evidence_and_no_hidden_retry(scenario):
    scenario.runtime.raise_error = True
    result = asyncio.run(scenario.call())
    assert result.record.failure_type is FailureType.OUTCOME_UNKNOWN
    assert result.record.short_reason == "execution_termination_unknown"
    assert len(scenario.runtime.execute_calls) == 1
    assert "private SDK diagnostic" not in str(result)


def test_output_storage_failure_leaves_running_execution_for_recovery(
    scenario, monkeypatch
):
    original = scenario.recorder.save_record

    def fail(record):
        raise EvidenceStorageError("output storage unavailable")

    monkeypatch.setattr(scenario.recorder, "save_record", fail)
    request = scenario.request()
    with pytest.raises(EvidenceStorageError):
        asyncio.run(scenario.call(request))
    assert scenario.state.executions[0].status is ExecutionStatus.RUNNING
    assert len(scenario.runtime.execute_calls) == 1
    with pytest.raises(ToolRecoveryRequired):
        asyncio.run(scenario.call(request))
    monkeypatch.setattr(scenario.recorder, "save_record", original)


def test_precommit_failure_does_not_dispatch(scenario, monkeypatch):
    from agent_core.control_store import StorageError

    original = scenario.repository.validate_candidate

    def fail(state, mutation):
        if mutation.operation == "start_execution":
            raise StorageError("validation unavailable")
        return original(state, mutation)

    monkeypatch.setattr(scenario.repository, "validate_candidate", fail)
    with pytest.raises(StorageError):
        asyncio.run(scenario.call())
    assert not scenario.runtime.execute_calls
    assert scenario.state.executions[0].status is ExecutionStatus.PENDING


def test_terminal_commit_failure_does_not_stop_sandbox(scenario, monkeypatch):
    from agent_core.control_store import StorageError

    original = scenario.repository.validate_candidate

    async def run():
        scenario.runtime.wait = True
        request = scenario.request()
        task = asyncio.create_task(scenario.call(request))
        while not scenario.runtime.waiting.is_set():
            await asyncio.sleep(0.01)

        def fail(state, mutation):
            raise StorageError("terminal validation unavailable")

        monkeypatch.setattr(scenario.repository, "validate_candidate", fail)
        scenario.token.is_cancelled = True
        with pytest.raises(StorageError):
            await task
        assert not any(
            e[0] in {"stop", "remove", "interrupt"}
            for e in scenario.runtime.client.events
        )
        assert scenario.state.task_run.status is TaskRunStatus.RUNNING
        audit = scenario.recorder.read_record(request.request_id)
        assert (
            audit.effective_timeout_seconds == scenario.runtime.dispatched_timeouts[0]
        )
        scenario.runtime.release_wait.set()
        monkeypatch.setattr(scenario.repository, "validate_candidate", original)

    asyncio.run(run())


def test_replay_survives_recorder_reopen(scenario):
    async def run():
        request = scenario.request()
        result = await scenario.call(request)
        reopened = FileToolRecorder(
            scenario.recorder.directory, sensitive_values=("secret-value",)
        )
        assert (
            reopened.begin(
                request, scenario.authorization, scenario.state.task_run.sandbox_id
            )
            == result
        )

    asyncio.run(run())


def test_unknown_execution_remains_blocked_with_fresh_gateway(scenario):
    async def run():
        scenario.runtime.confirmed = False
        await scenario.call()
        turn = scenario.state.turns[0]
        intent = scenario.commit(scenario.work.select_next_intent(turn.turn_id)).value
        scenario.commit(
            scenario.work.register_plan(Plan("next-plan", intent, (PlanStep("next"),)))
        )
        scenario.commit(scenario.work.start_plan("next-plan"))
        gateway = ToolGateway(
            controller=scenario.controller,
            policy=FileToolPolicy(),
            planner=Planner(),
            sandbox=TaskRunToolRuntime(scenario.sandbox),
            recorder=FileToolRecorder(scenario.recorder.directory),
            clock=scenario.clock,
        )
        with pytest.raises(ToolRecoveryRequired, match="termination"):
            await gateway.call(
                scenario.request(plan_id="next-plan"),
                scenario.authorization,
                scenario.token,
            )
        assert len(scenario.runtime.execute_calls) == 1

    asyncio.run(run())


def test_mcp_error_retry_exhaustion(scenario):
    async def run():
        scenario.planner.error = ToolCallError(
            FailureType.ENVIRONMENT_FAULT, "mcp_unavailable"
        )
        assert (await scenario.call()).action is FailureAction.RETRY_COMMAND_GENERATION
        assert (await scenario.call()).action is FailureAction.TRY_NEXT_INTENT
        with pytest.raises(ToolRecoveryRequired):
            await scenario.call()
        assert len(scenario.planner.calls) == 2
        assert not scenario.runtime.execute_calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value",
    [("tool", "bash"), ("arguments", ["-c", "id"]), ("timeout_seconds", 99)],
)
def test_real_mcp_parser_command_is_still_subject_to_policy(field, value):
    request = ToolRequest(
        str(uuid4()),
        str(uuid4()),
        "plan",
        0,
        "file.inspect_file",
        "/workspace/input/sample.txt",
    )
    command = {
        "tool": "python3",
        "arguments": list(inspection_arguments(request.path)),
        "timeout_seconds": 30,
    }
    command[field] = value
    if field == "timeout_seconds":
        with pytest.raises(ToolCallError):
            FileMCPPlanner._parse({"sandbox_command": command, "request": {}})
    else:
        parsed = FileMCPPlanner._parse({"sandbox_command": command, "request": {}})
        assert not FileToolPolicy().validate_command(request, parsed).allowed


@pytest.mark.parametrize(
    "component,error,regular,expected",
    [
        (None, None, True, 0),
        (1, "ELOOP", True, 41),
        (2, "ELOOP", True, 41),
        (3, "ELOOP", True, 41),
        (4, "ELOOP", True, 41),
        (4, "ENOENT", True, 40),
        (None, None, False, 41),
    ],
)
def test_controlled_program_opens_descriptors_without_following_links(
    component, error, regular, expected
):
    import builtins
    import errno
    import stat
    from types import SimpleNamespace

    opens, closed, commands = [], [], []

    def fake_open(path, flags, **kwargs):
        opens.append((path, flags, kwargs))
        if component == len(opens):
            raise OSError(getattr(errno, error), "test file error")
        return 10 + len(opens)

    nofollow, directory, nonblock = 0x100000, 0x200000, 0x400000
    fake_os = SimpleNamespace(
        open=fake_open,
        close=closed.append,
        O_RDONLY=0,
        O_DIRECTORY=directory,
        O_NOFOLLOW=nofollow,
        O_NONBLOCK=nonblock,
        fstat=lambda fd: SimpleNamespace(
            st_mode=stat.S_IFREG if regular else stat.S_IFIFO
        ),
    )

    def fake_run(argv, **kwargs):
        commands.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    fake_modules = {
        "os": fake_os,
        "sys": SimpleNamespace(
            argv=["script", "/workspace/input/sample.txt"],
            stderr=__import__("io").StringIO(),
        ),
        "subprocess": SimpleNamespace(run=fake_run),
    }
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        return (
            fake_modules[name]
            if name in fake_modules
            else real_import(name, *args, **kwargs)
        )

    namespace = {"__builtins__": dict(vars(builtins), __import__=fake_import)}
    if expected:
        with pytest.raises(SystemExit) as exc:
            exec(FILE_INSPECTION_SCRIPT, namespace)
        assert exc.value.code == expected
        assert not commands
    else:
        exec(FILE_INSPECTION_SCRIPT, namespace)
        assert commands[0][0][-1] == "/proc/self/fd/14"
        assert commands[0][1]["pass_fds"] == (14,)
        assert "/workspace/input/sample.txt" not in commands[0][0]
    assert all(flags & nofollow for _, flags, _ in opens)
    assert closed == list(
        reversed(range(11, 11 + len(opens) - (component is not None)))
    )


def test_control_database_contains_no_command_or_output(scenario):
    result = asyncio.run(scenario.call())
    import sqlite3

    with sqlite3.connect(
        scenario.recorder.directory.parent / "control.sqlite"
    ) as connection:
        dump = "\n".join(connection.iterdump())
    assert FILE_INSPECTION_SCRIPT not in dump
    assert result.record.stdout not in dump


def test_host_timeout_does_not_claim_worker_was_stopped(scenario):
    async def run():
        scenario.runtime.wait = True
        result = await scenario.call(scenario.request(timeout_seconds=1))
        scenario.runtime.release_wait.set()
        assert result.record.timed_out
        assert not result.record.termination_confirmed
        assert result.record.failure_type is FailureType.OUTCOME_UNKNOWN
        assert not scenario.sandbox._binding(
            scenario.state.task_run.sandbox_id
        ).accepting
        assert len(scenario.runtime.execute_calls) == 1

    asyncio.run(run())


def test_changed_retry_command_is_rejected_without_consuming_retry(scenario):
    async def run():
        scenario.runtime.result_code = 42
        first = await scenario.call()
        rejected = await scenario.call(scenario.request(timeout_seconds=20))
        assert rejected.record.failure_type is FailureType.POLICY_REJECTED
        assert len(scenario.runtime.execute_calls) == 1
        assert len(scenario.state.plan_validation_records) == 1
        scenario.runtime.result_code = 0
        final = await scenario.call()
        assert final.record.failure_type is None
        assert final.record.command_ref == first.record.command_ref

    asyncio.run(run())


def test_mcp_startup_obeys_total_budget(monkeypatch):
    from contextlib import asynccontextmanager
    import MCPs.file_tool_adapter as adapter

    closed = []

    @asynccontextmanager
    async def delayed(*args, **kwargs):
        try:
            await asyncio.Event().wait()
            yield None
        finally:
            closed.append(True)

    monkeypatch.setattr(adapter, "stdio_client", delayed)
    request = ToolRequest(
        str(uuid4()),
        str(uuid4()),
        "plan",
        0,
        "file.inspect_file",
        "/workspace/input/sample.txt",
    )
    with pytest.raises(ToolCallError) as exc:
        asyncio.run(FileMCPPlanner().prepare(request, 0.025))
    assert exc.value.failure_type is FailureType.ENVIRONMENT_FAULT
    assert closed == [True]


def test_two_steps_reuse_exact_sandbox_and_request_receipts(scenario):
    async def run():
        first = await scenario.call()
        second = await scenario.call(scenario.request(step_index=1))
        assert first.record.sandbox_id == second.record.sandbox_id
        assert len(scenario.runtime.execute_calls) == 2
        assert len({call[0] for call in scenario.runtime.execute_calls}) == 1
        receipt = json.loads(
            (
                scenario.recorder.directory / f"receipt-{first.record.request_id}.json"
            ).read_text()
        )["payload"]["snapshot"]
        assert receipt["authorization"]["allowed_paths"] == [
            "/workspace/input/sample.txt"
        ]
        assert receipt["request"]["path"] == "/workspace/input/sample.txt"

    asyncio.run(run())


def test_missing_fake_file_is_parameter_error(scenario):
    scenario.runtime.files[scenario.state.task_run.sandbox_id].clear()
    result = asyncio.run(scenario.call())
    assert result.record.failure_type is FailureType.PARAMETER_ERROR
    assert result.record.stdout == ""


@pytest.mark.parametrize("remaining,expected", [(90.0, 30), (12.9, 12), (1.0, 1)])
def test_effective_timeout_matches_dispatch_and_survives_replay(
    scenario, remaining, expected
):
    async def run():
        scenario.clock.now = scenario.state.task_run.deadline_at - remaining
        request = scenario.request()
        result = await scenario.call(request)
        assert scenario.runtime.dispatched_timeouts == [expected]
        assert result.record.effective_timeout_seconds == expected
        assert (
            scenario.recorder.load_command(result.record.command_ref).timeout_seconds
            == 30
        )
        reopened = FileToolRecorder(scenario.recorder.directory)
        assert (
            reopened.read_record(request.request_id).effective_timeout_seconds
            == expected
        )
        assert (
            reopened.begin(request, scenario.authorization, result.record.sandbox_id)
            == result
        )
        assert await scenario.call(request) == result
        assert scenario.runtime.dispatched_timeouts == [expected]

    asyncio.run(run())


@pytest.mark.parametrize("case", ["policy", "cancel", "deadline", "fractional_budget"])
def test_effective_timeout_is_empty_without_dispatch(scenario, case):
    request = scenario.request()
    if case == "policy":
        request = replace(request, path="/etc/passwd")
    elif case == "cancel":
        scenario.token.is_cancelled = True
    else:
        scenario.clock.now = scenario.state.task_run.deadline_at - (
            0.5 if case == "fractional_budget" else 0
        )
    result = asyncio.run(scenario.call(request))
    assert not scenario.runtime.dispatched_timeouts
    assert result.record.effective_timeout_seconds is None
    assert (
        scenario.recorder.read_record(request.request_id).effective_timeout_seconds
        is None
    )


def test_effective_timeout_retained_when_dispatch_outcome_unknown(scenario):
    scenario.clock.now = scenario.state.task_run.deadline_at - 12.9
    scenario.runtime.raise_error = True
    result = asyncio.run(scenario.call())
    assert result.record.failure_type is FailureType.OUTCOME_UNKNOWN
    assert result.record.effective_timeout_seconds == 12
    assert scenario.runtime.dispatched_timeouts == [12]
