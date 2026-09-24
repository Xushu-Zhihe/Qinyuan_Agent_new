"""Confirmed T3 types and ports. No SDK or persistence implementation."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from uuid import UUID

from agent_core.domain import ExecutionEvidencePort, FailureAction, FailureType
from agent_core.modeling import CancellationToken


def require_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("identifier must contain 1 to 128 characters")


@dataclass(frozen=True, slots=True)
class ToolRequest:
    request_id: str
    task_run_id: str
    plan_id: str
    step_index: int
    tool_name: str
    path: str
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        for value in (self.request_id, self.task_run_id, self.plan_id, self.tool_name):
            require_id(value)
        if (
            UUID(self.request_id).version != 4
            or str(UUID(self.request_id)) != self.request_id
        ):
            raise ValueError("request_id must be a canonical UUID4")
        if type(self.step_index) is not int or self.step_index < 0:
            raise ValueError("step_index must be non-negative")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        if not isinstance(self.path, str) or len(self.path.encode("utf-8")) > 4096:
            raise ValueError("path must be a bounded string")


@dataclass(frozen=True, slots=True)
class ToolAuthorization:
    authorization_id: str
    task_run_id: str
    allowed_paths: tuple[str, ...]
    allowed_tools: tuple[str, ...] = ("file.inspect_file",)

    def __post_init__(self) -> None:
        require_id(self.authorization_id)
        require_id(self.task_run_id)
        for values in (self.allowed_paths, self.allowed_tools):
            if not isinstance(values, tuple) or not 1 <= len(values) <= 64:
                raise ValueError("authorization requires bounded immutable allowlists")
            if any(not isinstance(v, str) or not v or len(v) > 4096 for v in values):
                raise ValueError("authorization entries must be bounded strings")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    short_reason: str

    def __post_init__(self) -> None:
        if (
            type(self.allowed) is not bool
            or not isinstance(self.short_reason, str)
            or not 1 <= len(self.short_reason) <= 256
        ):
            raise ValueError("invalid PolicyDecision")


@dataclass(frozen=True, slots=True)
class MCPCommand:
    tool: str
    arguments: tuple[str, ...]
    timeout_seconds: int

    def __post_init__(self) -> None:
        require_id(self.tool)
        if (
            not isinstance(self.arguments, tuple)
            or len(self.arguments) > 8
            or any(not isinstance(arg, str) or "\x00" in arg for arg in self.arguments)
        ):
            raise ValueError("command arguments must be a bounded immutable argv")
        if sum(len(arg.encode("utf-8")) for arg in self.arguments) > 16384:
            raise ValueError("command arguments exceed 16 KiB")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ValueError("invalid command timeout")


@dataclass(frozen=True, slots=True)
class SandboxExecutionRequest:
    sandbox_id: str
    command: MCPCommand


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    sandbox_id: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    termination_confirmed: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        require_id(self.sandbox_id)
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("invalid execution exit code")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ValueError("execution output must be text")
        if any(
            type(value) is not bool
            for value in (
                self.timed_out,
                self.termination_confirmed,
                self.stdout_truncated,
                self.stderr_truncated,
            )
        ):
            raise ValueError("invalid execution flags")


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    request_id: str
    task_run_id: str
    sandbox_id: str
    plan_id: str
    step_index: int
    authorization_id: str
    execution_id: str | None
    command_ref: str | None
    output_ref: str
    started_at: float
    finished_at: float
    exit_code: int | None
    failure_type: FailureType | None
    short_reason: str
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    termination_confirmed: bool = False
    effective_timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if not all(isfinite(t) for t in (self.started_at, self.finished_at)):
            raise ValueError("record timestamps must be finite")
        if self.finished_at < self.started_at or len(self.short_reason) > 256:
            raise ValueError("invalid record time or reason")
        if self.effective_timeout_seconds is not None and (
            type(self.effective_timeout_seconds) is not int
            or not 1 <= self.effective_timeout_seconds <= 60
            or self.execution_id is None
            or self.command_ref is None
        ):
            raise ValueError(
                "effective timeout requires an execution and 1 to 60 seconds"
            )


@dataclass(frozen=True, slots=True)
class ToolResult:
    record: ExecutionRecord
    action: FailureAction | None = None


@dataclass(frozen=True, slots=True)
class StoredToolRequest:
    request: ToolRequest
    authorization: ToolAuthorization
    sandbox_id: str


class ToolCallError(RuntimeError):
    def __init__(self, failure_type: FailureType, short_reason: str) -> None:
        self.failure_type = failure_type
        self.short_reason = short_reason[:256]
        super().__init__(self.short_reason)


class ToolRecoveryRequired(RuntimeError):
    """A receipt exists; dispatch must not be repeated to guess its outcome."""


class ToolPolicyPort(Protocol):
    def authorize(
        self, request: ToolRequest, authorization: ToolAuthorization
    ) -> PolicyDecision: ...

    def validate_command(
        self, request: ToolRequest, command: MCPCommand
    ) -> PolicyDecision: ...


class ToolCommandPlannerPort(Protocol):
    async def prepare(
        self, request: ToolRequest, timeout_seconds: float
    ) -> MCPCommand: ...


class ToolSandboxPort(Protocol):
    async def execute(
        self, request: SandboxExecutionRequest
    ) -> SandboxExecutionResult: ...

    def quiesce(self, sandbox_id: str) -> None: ...


class ToolRecorderPort(ExecutionEvidencePort, Protocol):
    def begin(
        self, request: ToolRequest, authorization: ToolAuthorization, sandbox_id: str
    ) -> ToolResult | None: ...

    def save_command(self, command_ref: str, command: MCPCommand) -> None: ...

    def load_command(self, command_ref: str) -> MCPCommand: ...

    def save_record(self, record: ExecutionRecord) -> ExecutionRecord: ...

    def read_record(self, output_ref: str) -> ExecutionRecord | None: ...

    def finish(self, result: ToolResult) -> None: ...


class ScopedToolGatewayPort(Protocol):
    async def call(
        self,
        request: ToolRequest,
        authorization: ToolAuthorization,
        cancellation_token: CancellationToken,
    ) -> ToolResult: ...
