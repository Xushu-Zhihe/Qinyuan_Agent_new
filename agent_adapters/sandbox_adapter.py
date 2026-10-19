"""Bind MCP's Agent interface to an already-owned TaskRun container."""

from dataclasses import replace
from Sandbox.models import SandboxSession, SandboxStatus
from Sandbox.service import SandboxNotFoundError, SandboxPolicyError


class D8SandboxAdapter:
    def __init__(self, sandbox, task_run_id, sandbox_id):
        self.sandbox = sandbox
        self.task_run_id = task_run_id
        self.sandbox_id = sandbox_id
        self.maximum_timeout_seconds = sandbox._settings.maximum_timeout_seconds

    def get_session(self, agent_id):
        if agent_id != self.task_run_id:
            return None
        return SandboxSession(agent_id, self.sandbox_id, SandboxStatus.RUNNING)

    def execute(self, agent_id, request):
        if self.get_session(agent_id) is None:
            raise SandboxNotFoundError("mcp.task_run_not_bound")
        request = replace(request, timeout_seconds=min(
            request.timeout_seconds, self.maximum_timeout_seconds))
        # TaskRunSandbox keeps ownership, admission locks, timeout and cancellation.
        try:
            return self.sandbox.execute_mcp(self.sandbox_id, request)
        except ValueError as exc:
            # TaskRun wraps errors after actual dispatch as outcome_unknown.
            raise SandboxPolicyError("mcp.command_rejected") from exc

    def interrupt_for_agent(self, agent_id):
        if self.get_session(agent_id) is None:
            raise SandboxNotFoundError("mcp.task_run_not_bound")
        self.sandbox.interrupt(self.sandbox_id)
        return self.sandbox_id
