"""One bounded File MCP stdio session. This adapter never executes commands."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_core.domain import FailureType
from agent_core.tool_contracts import MCPCommand, ToolCallError, ToolRequest


class FileMCPPlanner:
    async def prepare(self, request: ToolRequest, timeout_seconds: float) -> MCPCommand:
        if timeout_seconds <= 0:
            raise ToolCallError(FailureType.ENVIRONMENT_FAULT, "mcp_budget_exhausted")
        root = Path(__file__).resolve().parents[1]
        # Do not inherit provider credentials into the command-planning process.
        names = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PYTHONPYCACHEPREFIX")
        environment = {name: os.environ[name] for name in names if name in os.environ}
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "MCPs.File_Mcp.server"],
            cwd=str(root),
            env=environment,
        )
        started = asyncio.get_running_loop().time()
        try:
            # No stderr file or raw SDK message is exposed to evidence/logs.
            with open(os.devnull, "w") as errors:
                async with asyncio.timeout(timeout_seconds):
                    async with AsyncExitStack() as stack:
                        async with asyncio.timeout(min(10.0, timeout_seconds)):
                            reader, writer = await stack.enter_async_context(
                                stdio_client(parameters, errlog=errors)
                            )
                            session = await stack.enter_async_context(
                                ClientSession(reader, writer)
                            )
                            await session.initialize()
                        remaining = timeout_seconds - (
                            asyncio.get_running_loop().time() - started
                        )
                        async with asyncio.timeout(min(10.0, remaining)):
                            response = await session.call_tool(
                                "inspect_file",
                                {
                                    "path": request.path,
                                    "timeout_seconds": request.timeout_seconds,
                                },
                            )
                        if response.isError:
                            raise ToolCallError(
                                FailureType.PARAMETER_ERROR, "mcp_parameter_error"
                            )
                        payload = response.structuredContent
                        if (
                            payload is None
                            and len(response.content) == 1
                            and response.content[0].type == "text"
                        ):
                            text = response.content[0].text
                            if len(text.encode("utf-8")) > 65536:
                                raise ValueError("oversized MCP response")
                            payload = json.loads(text)
                        return self._parse(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # AnyIO task groups can wrap protocol errors in exception groups.
            known = self._known_error(exc)
            if known is not None:
                raise known from None
            raise ToolCallError(
                FailureType.ENVIRONMENT_FAULT, "mcp_unavailable_or_timed_out"
            ) from None

    @staticmethod
    def _known_error(exc: BaseException) -> ToolCallError | None:
        if isinstance(exc, ToolCallError):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                found = FileMCPPlanner._known_error(child)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _parse(payload: object) -> MCPCommand:
        try:
            if not isinstance(payload, dict) or set(payload) != {
                "sandbox_command",
                "request",
            }:
                raise ValueError("invalid command envelope")
            command = payload["sandbox_command"]
            if not isinstance(command, dict) or set(command) != {
                "tool",
                "arguments",
                "timeout_seconds",
            }:
                raise ValueError("invalid command fields")
            args = command["arguments"]
            if (
                not isinstance(command["tool"], str)
                or not isinstance(args, list)
                or len(args) > 8
                or any(not isinstance(arg, str) or "\x00" in arg for arg in args)
                or sum(len(arg.encode("utf-8")) for arg in args) > 16384
                or type(command["timeout_seconds"]) is not int
                or not 1 <= command["timeout_seconds"] <= 60
            ):
                raise ValueError("invalid command values")
            return MCPCommand(command["tool"], tuple(args), command["timeout_seconds"])
        except (ValueError, KeyError, TypeError):
            raise ToolCallError(
                FailureType.POLICY_REJECTED, "mcp_command_shape_rejected"
            ) from None
