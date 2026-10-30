"""A bounded command timeout is a known failure, not an unknown side effect."""
import asyncio
import json
from types import SimpleNamespace

from MCPs.Mcp_client.client import MCPManager
from Sandbox.models import CommandResult


class _Session:
    async def call_tool(self, tool_name, arguments=None):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps({
                "sandbox_command": {
                    "tool": "bash",
                    "arguments": ["-lc", "true"],
                    "timeout_seconds": 5,
                }
            }))],
            isError=False,
            structuredContent=None,
        )


class _Service:
    def __init__(self):
        self.calls = []

    def execute(self, agent_id, request):
        self.calls.append((agent_id, request))
        return CommandResult(
            sandbox_id="sandbox",
            exit_code=124,
            stdout="",
            stderr="",
            timed_out=True,
            termination_confirmed=True,
        )


def test_mcp_timeout_is_failed_not_outcome_unknown():
    manager = object.__new__(MCPManager)
    manager.tool_index = {
        "shell.run_bash": SimpleNamespace(server_name="shell", tool_name="run_bash"),
    }
    manager.runtimes = {
        "shell": SimpleNamespace(
            session=_Session(),
            config=SimpleNamespace(sandbox_tools=frozenset({"bash"})),
        ),
    }
    manager.sandbox_service = _Service()
    manager.mcp_call_timeout_seconds = 10
    manager.sandbox_execution_grace_seconds = 10
    manager.sandbox_interrupt_timeout_seconds = 10
    manager.output_character_limit = 20_000
    manager.audit_path = None

    result = asyncio.run(manager._execute_bound_tool(
        agent_id="task",
        qualified_name="shell.run_bash",
        arguments={},
    ))

    assert result.status == "failed"
    assert result.timed_out is True
    payload = result.to_tool_payload()
    assert payload["status"] == "failed"
    assert payload["timed_out"] is True
    assert payload["exit_code"] == 124
    assert manager.sandbox_service.calls
