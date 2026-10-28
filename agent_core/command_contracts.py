"""Bash request and policy, independent of the frozen file-tool types."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_core.domain import ExecutionEvidencePort, FailureAction, PlanStatus
from agent_core.modeling import CancellationToken
from agent_core.runtime import TaskRunStatus

MAX_SCRIPT_BYTES = 64 * 1024
MAX_STDIN_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_COMMAND_SECONDS = 300
WORK_ROOT = "/workspace/work"


class T6EvidenceError(RuntimeError):
    pass


class T6EvidenceCapacityError(T6EvidenceError):
    def __init__(self, kind, reference, actual_bytes, limit_bytes):
        self.record_kind, self.record_id = kind, reference
        self.actual_bytes, self.limit_bytes = actual_bytes, limit_bytes
        super().__init__("evidence_capacity_exceeded")


class T6CommandRecoveryRequired(RuntimeError):
    pass


class T6CommandExecutionError(RuntimeError):
    def __init__(self, *, dispatched: bool, reason: str) -> None:
        self.dispatched = dispatched
        self.reason = reason
        super().__init__(reason)


def _uuid4(value: str, name: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{name}") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"invalid_{name}")


def validate_working_directory(path: str) -> None:
    if not isinstance(path, str) or len(path.encode("utf-8")) > 4096:
        raise ValueError("invalid_working_directory")
    if path != WORK_ROOT:
        if not path.startswith(WORK_ROOT + "/"):
            raise ValueError("working_directory_out_of_scope")
        parts = path[len(WORK_ROOT) + 1 :].split("/")
        if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
            raise ValueError("invalid_working_directory")


@dataclass(frozen=True, slots=True)
class SandboxCommandRequest:
    request_id: str
    task_run_id: str
    plan_id: str
    step_index: int
    script: str
    stdin: bytes
    working_directory: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "request_id")
        _uuid4(self.task_run_id, "task_run_id")
        if (
            not isinstance(self.plan_id, str)
            or not self.plan_id
            or len(self.plan_id) > 128
        ):
            raise ValueError("invalid_plan_id")
        if type(self.step_index) is not int or self.step_index < 0:
            raise ValueError("invalid_step_index")
        if (
            not isinstance(self.script, str)
            or not self.script
            or "\x00" in self.script
            or len(self.script.encode("utf-8")) > MAX_SCRIPT_BYTES
        ):
            raise ValueError("invalid_script")
        if not isinstance(self.stdin, bytes) or len(self.stdin) > MAX_STDIN_BYTES:
            raise ValueError("invalid_stdin")
        validate_working_directory(self.working_directory)
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= MAX_COMMAND_SECONDS
        ):
            raise ValueError("invalid_timeout")

    @property
    def stdin_sha256(self) -> str:
        return hashlib.sha256(self.stdin).hexdigest()


@dataclass(frozen=True, slots=True)
class SandboxCommandAuthorization:
    authorization_id: str
    task_run_id: str
    allowed_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid4(self.authorization_id, "authorization_id")
        _uuid4(self.task_run_id, "task_run_id")
        if (
            not isinstance(self.allowed_tools, tuple)
            or len(self.allowed_tools) > 32
            or any(not isinstance(value, str) for value in self.allowed_tools)
        ):
            raise ValueError("invalid_allowed_tools")


@dataclass(frozen=True, slots=True)
class AuthorizedCommand:
    request: SandboxCommandRequest
    authorization_id: str
    sandbox_id: str
    effective_timeout_seconds: int
    deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class T6CommandResult:
    sandbox_id: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    termination_confirmed: bool
    stream_complete: bool
    dispatched: bool


@dataclass(frozen=True, slots=True)
class T6CommandEvidence:
    request_id: str
    task_run_id: str
    authorization_id: str
    sandbox_id: str
    argv: tuple[str, ...]
    stdin_sha256: str
    stdin_size: int
    working_directory: str
    effective_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class T6ExecutionRecord:
    request_id: str
    task_run_id: str
    sandbox_id: str
    plan_id: str
    step_index: int
    authorization_id: str
    command_ref: str | None
    started_at: float
    finished_at: float
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    termination_confirmed: bool
    stream_complete: bool
    effective_timeout_seconds: int | None
    status: str
    dispatched: bool = False

    def __post_init__(self) -> None:
        _uuid4(self.request_id, "request_id")
        _uuid4(self.task_run_id, "task_run_id")
        if not math.isfinite(self.started_at) or not math.isfinite(self.finished_at):
            raise ValueError("invalid_execution_time")
        if self.finished_at < self.started_at:
            raise ValueError("invalid_execution_time")
        if not isinstance(self.stdout, bytes) or not isinstance(self.stderr, bytes):
            raise ValueError("invalid_execution_output")
        if len(self.stdout) > MAX_OUTPUT_BYTES or len(self.stderr) > MAX_OUTPUT_BYTES:
            raise ValueError("execution_output_oversized")
        if self.status not in {
            "completed",
            "step_timeout",
            "outcome_unknown",
            "environment_fault",
            "policy_rejected",
        }:
            raise ValueError("invalid_execution_status")
        if self.effective_timeout_seconds is not None and (
            type(self.effective_timeout_seconds) is not int
            or not 1 <= self.effective_timeout_seconds <= 300
        ):
            raise ValueError("invalid_effective_timeout")
        if any(
            type(value) is not bool
            for value in (
                self.dispatched,
                self.stdout_truncated,
                self.stderr_truncated,
                self.timed_out,
                self.termination_confirmed,
                self.stream_complete,
            )
        ):
            raise ValueError("invalid_execution_flag")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("invalid_exit_code")
        if self.dispatched and self.command_ref is None:
            raise ValueError("dispatched_command_missing")
        if self.status == "completed" and (
            not self.dispatched
            or not self.termination_confirmed
            or self.exit_code is None
            or self.timed_out
            or not self.stream_complete
        ):
            raise ValueError("completed_execution_unconfirmed")
        if (
            self.status == "step_timeout"
            and self.exit_code is not None
            and not self.dispatched
        ):
            raise ValueError("undispatched_timeout_has_exit_code")
        if self.status == "step_timeout" and not self.termination_confirmed:
            raise ValueError("timeout_termination_unconfirmed")
        if (self.status == "step_timeout") != self.timed_out:
            raise ValueError("timeout_status_mismatch")
        if self.status in {"environment_fault", "policy_rejected"} and self.dispatched:
            raise ValueError("pre_dispatch_failure_dispatched")


@dataclass(frozen=True, slots=True)
class SandboxCommandResult:
    record: T6ExecutionRecord
    action: FailureAction | None = None


class T6CommandSandboxPort(Protocol):
    async def execute(
        self,
        approved: AuthorizedCommand,
        token: CancellationToken,
    ) -> T6CommandResult: ...

    def quiesce(self, sandbox_id: str) -> None: ...


class T6CommandRecorderPort(ExecutionEvidencePort, Protocol):
    def read_completed(self, request_id: str) -> SandboxCommandResult | None: ...

    def replay(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        sandbox_id: str,
    ) -> SandboxCommandResult | None: ...

    def begin(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        sandbox_id: str,
    ) -> SandboxCommandResult | None: ...

    def save_command(
        self, reference: str, approved: AuthorizedCommand, argv: tuple[str, ...]
    ) -> None: ...

    def read_command(self, reference: str) -> T6CommandEvidence | None: ...

    def save_record(self, record: T6ExecutionRecord) -> None: ...

    def read_record(self, reference: str) -> T6ExecutionRecord | None: ...

    def finish(self, result: SandboxCommandResult) -> None: ...


class SandboxCommandPolicy:
    def authorize(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        state,
        *,
        now: float,
        step_limit_seconds: int,
    ) -> AuthorizedCommand:
        if request.task_run_id != authorization.task_run_id:
            raise ValueError("authorization_task_mismatch")
        if "sandbox.command" not in authorization.allowed_tools:
            raise ValueError("bash_not_authorized")
        task = state.task_run
        if (
            task.task_run_id != request.task_run_id
            or task.status is not TaskRunStatus.RUNNING
        ):
            raise ValueError("task_not_running")
        plan = next(
            (item for item in state.plans if item.plan_id == request.plan_id), None
        )
        if plan is None or not 0 <= request.step_index < len(plan.steps):
            raise ValueError("plan_step_not_found")
        if plan.status is not PlanStatus.RUNNING:
            raise ValueError("plan_not_running")
        if (
            type(step_limit_seconds) is not int
            or not 1 <= step_limit_seconds <= MAX_COMMAND_SECONDS
            or not math.isfinite(now)
        ):
            raise ValueError("invalid_command_budget")
        remaining = math.floor(task.deadline_at - now)
        effective = min(request.timeout_seconds, step_limit_seconds, remaining)
        if effective < 1:
            raise ValueError("task_deadline_exceeded")
        if not isinstance(task.sandbox_id, str) or not task.sandbox_id:
            raise ValueError("sandbox_not_bound")
        return AuthorizedCommand(
            request, authorization.authorization_id, task.sandbox_id, effective
        )
