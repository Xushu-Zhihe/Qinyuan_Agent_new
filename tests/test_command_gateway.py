"""Direct commands through real Controller/SQLite, with simulated execution."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_adapters.command_recorder import T6CommandRecorder
from agent_adapters.observation_proof_store import T6ObservationProofStore
from agent_adapters.postcondition_store import T6PostconditionStore
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.domain import (
    ExecutionStatus,
    FailureAction,
    FailureType,
    IntentStatus,
    Plan,
    PlanStatus,
    PlanStep,
    TurnStatus,
)
from agent_core.modeling import IntentCandidate
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.command_contracts import (
    SandboxCommandAuthorization,
    SandboxCommandPolicy,
    SandboxCommandRequest,
    T6CommandExecutionError,
    T6CommandRecoveryRequired,
    T6CommandResult,
    T6EvidenceError,
)
from agent_core.command_gateway import T6CommandGateway
from agent_core.observation_proof import T6ObservationProofVerifier
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy
from Sandbox.command_sandbox import T6TaskRunSandbox
from test_task_run_sandbox import FakeRuntime
from test_tool_gateway import Clock, Token


class Commands:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.calls = []
        self.quiesced = []
        self.code = 0
        self.stdout = b"observed\x00bytes"
        self.stdout_truncated = False
        self.timeout = False
        self.confirmed = True
        self.stream_complete = True
        self.error = None
        self.on_execute = lambda: None

    async def execute(self, approved, token):
        self.calls.append(approved)
        self.on_execute()
        if self.error:
            raise self.error
        return T6CommandResult(
            approved.sandbox_id,
            None if self.timeout else self.code,
            self.stdout,
            b"stderr\xff",
            self.stdout_truncated,
            False,
            self.timeout,
            self.confirmed,
            self.stream_complete,
            True,
        )

    def quiesce(self, sandbox_id):
        self.quiesced.append(sandbox_id)
        self.sandbox.quiesce(sandbox_id)


class Scenario:
    def __init__(self, root):
        self.clock = Clock()
        self.monotonic = Clock(now=100.0)
        self.token = Token()
        self.sandbox = T6TaskRunSandbox(FakeRuntime())
        self.recorder = T6CommandRecorder(root / "evidence")
        self.store = SqliteControlStore.open(root / "control.sqlite", clock=self.clock)
        self.postconditions = T6PostconditionStore(root / "conditions")
        self.proof_store = T6ObservationProofStore(root / "proofs")
        self.proof_verifier = T6ObservationProofVerifier(
            self.store, self.postconditions, self.recorder, self.proof_store, self.clock
        )
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=ControlStateRepository(
                self.store, self.recorder,
                t6_observation_proofs=self.proof_verifier,
            ),
            registry=TaskRunControllerRegistry(),
            t6_observation_proofs=self.proof_verifier,
        )
        self.controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "test command",
                    "target": {
                        "attachments": [
                            {"kind": "local_path", "path": "sample.txt"},
                        ]
                    },
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
        )
        registration = TurnManager.prepare_turn_registration(
            state=self.state,
            mutation_id=str(uuid4()),
            turn_target="test command",
            candidates=(
                IntentCandidate("inspect output"),
                IntentCandidate("alternative"),
            ),
        )
        self.controller.commit_turn_registration(registration, str(uuid4()))
        intent = self.commit(self.work.select_next_intent(registration.turn_id)).value
        self.commit(
            self.work.register_plan(Plan("plan", intent, (PlanStep("run script"),)))
        )
        self.commit(self.work.start_plan("plan"))
        self.commands = Commands(self.sandbox)
        self.authorization = SandboxCommandAuthorization(
            str(uuid4()),
            self.state.task_run.task_run_id,
            ("sandbox.command",),
        )
        self.gateway = self.new_gateway()

    @property
    def state(self):
        return self.controller.control_state

    @property
    def work(self):
        return TurnWorkingCopy(self.state, self.recorder)

    def commit(self, operation):
        return self.controller.commit_turn_operation(operation, str(uuid4()))

    def new_gateway(self):
        return T6CommandGateway(
            controller=self.controller,
            policy=SandboxCommandPolicy(),
            sandbox=self.commands,
            recorder=self.recorder,
            clock=self.clock,
            step_limit_seconds=60,
            monotonic=self.monotonic,
        )

    def request(self, **changes):
        return replace(
            SandboxCommandRequest(
                str(uuid4()),
                self.state.task_run.task_run_id,
                "plan",
                0,
                "printf hello",
                b"",
                "/workspace/work",
                120,
            ),
            **changes,
        )

    def call(self, request=None, authorization=None):
        return asyncio.run(
            self.gateway.call(
                request or self.request(),
                authorization or self.authorization,
                self.token,
            )
        )

    def close(self):
        if self.state.task_run.status not in TERMINAL_STATUSES:
            self.controller.cancel(str(uuid4()))
        self.controller.close()
        self.sandbox.close()
        self.store.close()


@pytest.fixture
def scenario(tmp_path):
    result = Scenario(tmp_path)
    try:
        yield result
    finally:
        result.close()


@pytest.mark.parametrize("code", [0, 1, 124, 137])
def test_normal_exit_commits_execution_without_fact_or_intent_success(scenario, code):
    s = scenario
    s.commands.code = code
    request = s.request()
    result = s.call(request)
    assert result.record.status == "completed"
    assert result.record.exit_code == code
    assert result.record.stdout == b"observed\x00bytes"
    assert result.record.effective_timeout_seconds == 60
    assert s.state.executions[0].status is ExecutionStatus.SUCCEEDED
    assert s.state.intents[0].status is IntentStatus.RUNNING
    assert not s.state.facts and not s.state.failure_summaries
    s.controller.cancel(str(uuid4()))
    state = s.state
    s.gateway = s.new_gateway()
    assert s.call(request) == result
    assert len(s.commands.calls) == 1
    assert s.state == state


def test_denied_permission_never_dispatches(scenario):
    s = scenario
    result = s.call(authorization=replace(s.authorization, allowed_tools=()))
    assert result.record.status == "policy_rejected"
    assert not result.record.dispatched and not s.state.executions
    assert not s.commands.calls


def test_timeout_allows_one_changed_command_then_fails_intent(scenario):
    s = scenario
    s.commands.timeout = True
    first = s.call()
    assert first.action is FailureAction.RETRY_STEP_COMMAND
    assert s.state.executions[0].status is ExecutionStatus.TIMED_OUT
    s.gateway = s.new_gateway()
    second = s.call(s.request(script="new command"))
    assert second.action is FailureAction.TRY_NEXT_INTENT
    assert s.state.intents[0].status is IntentStatus.FAILED
    assert len(s.state.plan_validation_records) == 2
    assert len(s.commands.calls) == 2


def test_unconfirmed_timeout_blocks_new_id_and_replay_is_read_only(scenario):
    s = scenario
    s.commands.timeout = True
    s.commands.confirmed = False
    request = s.request()
    first = s.call(request)
    assert first.record.status == "outcome_unknown"
    assert s.commands.quiesced
    s.gateway = s.new_gateway()
    assert s.call(request) == first
    with pytest.raises(T6CommandRecoveryRequired):
        s.call()
    assert len(s.commands.calls) == 1


def test_incomplete_stream_after_zero_exit_requires_recovery(scenario):
    s = scenario
    s.commands.stream_complete = False
    request = s.request()
    first = s.call(request)

    assert first.record.status == "outcome_unknown"
    assert first.record.dispatched
    assert first.record.exit_code == 0
    assert first.record.termination_confirmed
    assert not first.record.stream_complete
    assert first.action is FailureAction.RECOVERY_REQUIRED
    assert s.state.executions[0].status is ExecutionStatus.FAILED
    assert s.state.plans[0].status is PlanStatus.RUNNING
    assert s.state.intents[0].status is IntentStatus.RUNNING
    assert s.state.turns[0].status is TurnStatus.RUNNING
    assert [
        (failure.failure_type, failure.short_reason, failure.evidence_ref)
        for failure in s.state.failure_summaries
    ] == [(FailureType.OUTCOME_UNKNOWN, "outcome_unknown", request.request_id)]

    state = s.state
    s.gateway = s.new_gateway()
    assert s.call(request) == first
    assert s.state == state
    with pytest.raises(T6CommandRecoveryRequired):
        s.call()
    assert len(s.commands.calls) == 1


def test_receipt_crash_blocks_new_request_even_without_execution(scenario):
    s = scenario
    s.recorder.begin(s.request(), s.authorization, s.state.task_run.sandbox_id)
    with pytest.raises(T6CommandRecoveryRequired):
        s.call()
    assert not s.commands.calls


def test_evidence_commit_failure_never_redispatches(scenario, monkeypatch):
    s = scenario

    def fail(_result):
        raise T6EvidenceError("finished_write_failed")

    monkeypatch.setattr(s.recorder, "finish", fail)
    request = s.request()
    with pytest.raises(T6EvidenceError):
        s.call(request)
    assert s.state.executions[0].status is ExecutionStatus.SUCCEEDED
    with pytest.raises(T6CommandRecoveryRequired):
        s.call(request)
    with pytest.raises(T6CommandRecoveryRequired):
        s.call()
    assert len(s.commands.calls) == 1


def test_missing_command_evidence_prevents_completed_replay(scenario):
    s = scenario
    request = s.request()
    result = s.call(request)
    path = s.recorder.directory / f"command-{result.record.command_ref}.json"
    content = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(T6EvidenceError, match="finished_command_mismatch"):
            s.call(request)
    finally:
        path.write_bytes(content)
    assert len(s.commands.calls) == 1


def test_pre_dispatch_budget_exhaustion_consumes_timeout_retry(scenario, monkeypatch):
    s = scenario
    original = s.recorder.begin

    def slow_begin(*args):
        result = original(*args)
        s.monotonic.now += 61
        return result

    monkeypatch.setattr(s.recorder, "begin", slow_begin)
    first = s.call()
    assert first.action is FailureAction.RETRY_STEP_COMMAND
    assert not first.record.dispatched
    assert first.record.effective_timeout_seconds is None
    assert not s.state.executions and not s.commands.calls
    monkeypatch.setattr(s.recorder, "begin", original)
    s.commands.timeout = True
    second = s.call(s.request(script="modified"))
    assert second.action is FailureAction.TRY_NEXT_INTENT


def test_environment_fault_requires_same_command_for_explicit_retry(scenario):
    s = scenario
    s.commands.error = T6CommandExecutionError(
        dispatched=False, reason="environment_fault"
    )
    first = s.call()
    assert first.action is FailureAction.RETRY_EXECUTION
    assert not first.record.dispatched
    with pytest.raises(T6CommandRecoveryRequired, match="command_changed"):
        s.call(s.request(script="changed"))
    s.commands.error = None
    second = s.call()
    assert second.record.status == "completed"
    assert first.record.command_ref == second.record.command_ref
    assert len(s.state.plan_validation_records) == 1


def test_environment_retry_budget_exhaustion_uses_existing_validation(scenario):
    s = scenario
    s.commands.error = T6CommandExecutionError(
        dispatched=False, reason="environment_fault"
    )
    first = s.call()
    assert first.action is FailureAction.RETRY_EXECUTION
    s.commands.error = None
    original = s.recorder.begin

    def slow_begin(*args):
        result = original(*args)
        s.monotonic.now += 61
        return result

    s.recorder.begin = slow_begin
    second = s.call()
    assert second.record.status == "step_timeout"
    assert second.action is FailureAction.RETRY_STEP_COMMAND
    assert len(s.state.plan_validation_records) == 1
    assert len(s.commands.calls) == 1


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_and_deadline_commit_terminal_before_discarding_late_result(
    scenario, cancel
):
    s = scenario

    def interrupt():
        if cancel:
            s.token.is_cancelled = True
        else:
            s.clock.now = s.state.task_run.deadline_at

    s.commands.on_execute = interrupt
    result = s.call()
    expected = TaskRunStatus.CANCELLED if cancel else TaskRunStatus.DEADLINE_EXCEEDED
    assert s.state.task_run.status is expected
    assert result.record.status == "outcome_unknown"
    assert s.state.executions[0].output_ref is None
    assert not s.state.facts
