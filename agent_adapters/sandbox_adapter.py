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
        self.prepared_download = None

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

    def prepared_tool_command(self, qualified_name, arguments, request):
        import json
        from agent_core.turn_modeling import digest
        prepared = self.prepared_download
        if (qualified_name != "download.install_tool" or prepared is None
                or prepared[0] != digest(arguments) or request.tool != "python3"
                or len(request.arguments) != 2 or request.arguments[0] != "-c"):
            raise SandboxPolicyError("download.preparation_binding_missing")
        context = prepared[1]
        prefix = ("import os\nos.environ['QINYUAN_OFFLINE_TOOL_INSTALL']='1'\n"
                  "os.environ['QINYUAN_TOOL_WHEELHOUSE']=" + repr(context["wheelhouse"]) + "\n"
                  "os.environ['QINYUAN_TOOL_WHEEL_HASHES']=" + repr(json.dumps(context["wheel_hashes"])) + "\n")
        return replace(request, arguments=("-c", prefix + request.arguments[1]))

    def interrupt_for_agent(self, agent_id):
        if self.get_session(agent_id) is None:
            raise SandboxNotFoundError("mcp.task_run_not_bound")
        self.sandbox.interrupt(self.sandbox_id)
        return self.sandbox_id
