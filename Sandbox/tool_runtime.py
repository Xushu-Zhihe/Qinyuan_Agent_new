"""Async execution adapter for the existing TaskRun-owned sandbox."""

from __future__ import annotations

import asyncio

from Sandbox.models import CommandRequest
from Sandbox.task_run_sandbox import TaskRunSandbox, TaskRunSandboxError
from agent_core.tool_contracts import SandboxExecutionRequest, SandboxExecutionResult


class TaskRunToolRuntime:
    def __init__(self, sandbox: TaskRunSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        command = request.command
        try:
            result = await asyncio.to_thread(
                self._sandbox.execute,
                request.sandbox_id,
                CommandRequest(
                    command.tool, command.arguments, command.timeout_seconds
                ),
            )
        except TaskRunSandboxError:
            return SandboxExecutionResult(request.sandbox_id, None)
        return SandboxExecutionResult(
            result.sandbox_id,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.timed_out,
            result.termination_confirmed,
            result.stdout_truncated,
            result.stderr_truncated,
        )

    def quiesce(self, sandbox_id: str) -> None:
        self._sandbox.quiesce(sandbox_id)
