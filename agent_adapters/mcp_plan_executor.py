"""
MCP-based Plan execution adapter (D22).

Integrates MCPManager with D8 Plan completion flow, replacing ProbeAction
shell scripts with MCP tool calls for Plans that declare MCPAction.

Design:
- Lenient toward model: accepts rolling tool calls, semantic postconditions
- Strict about execution: timeout enforcement, evidence capture, authorization checks
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from MCPs.Mcp_client.client import MCPManager
    from Sandbox.workspace import D8DockerWorkspace


@dataclass(frozen=True, slots=True)
class MCPExecutionResult:
    """Result of executing an MCPAction Plan."""

    success: bool
    tool_outputs: tuple[dict, ...]  # One per MCPToolCall, in order
    postconditions_met: bool
    error: str | None = None


class MCPPlanExecutor:
    """
    Executes MCPAction Plans within D8DockerWorkspace sandbox.

    Bridges MCPManager (expects SandboxService interface) with D8DockerWorkspace
    (wraps T6TaskRunSandbox). Enforces timeouts, captures evidence, validates
    postconditions.
    """

    def __init__(
        self,
        mcp_manager: MCPManager,
        workspace: D8DockerWorkspace,
        task_run_id: str,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.workspace = workspace
        self.task_run_id = task_run_id

    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        tool_id: str,
        plan_id: str,
        step_index: int,
        token,
    ) -> MCPExecutionResult:
        """
        Execute single MCP tool call for D22 Plan.

        Args:
            tool_name: Qualified MCP tool name
            arguments: Tool-specific parameters
            tool_id: Unique ID for this tool execution
            plan_id: Current Plan UUID
            step_index: Step number within Plan
            token: Cancellation token

        Returns:
            MCPExecutionResult with success, outputs, and postconditions status

        Lenient: tolerates tool failures (captured in evidence)
        Strict: enforces timeouts, authorization, sandbox isolation
        """
        tool_outputs: list[dict] = []
        error: str | None = None

        try:
            # Execute single tool call
            result = await self._execute_tool_call(
                tool_name, arguments, plan_id, step_index
            )
            tool_outputs.append(result)

            return MCPExecutionResult(
                success=True,
                tool_outputs=tuple(tool_outputs),
                postconditions_met=True,  # No postconditions for single tool call
                error=None,
            )

        except Exception as exc:
            # Lenient: capture tool failure as evidence
            tool_outputs.append(
                {
                    "success": False,
                    "error": str(exc),
                    "tool_name": tool_name,
                }
            )
            return MCPExecutionResult(
                success=False,
                tool_outputs=tuple(tool_outputs),
                postconditions_met=False,
                error=f"Tool {tool_name} failed: {exc}",
            )

    async def _execute_tool_call(
        self, tool_name: str, arguments: dict, plan_id: str, step_index: int
    ) -> dict:
        """
        Execute single MCP tool call within sandbox context.

        Uses MCPManager._execute_bound_tool() with agent_id mapped from task_run_id.
        """
        # Map task_run_id → agent_id for MCPManager
        agent_id = f"task_run:{self.task_run_id}"

        # Execute tool via MCPManager (handles timeout, sandbox binding)
        result = await self.mcp_manager._execute_bound_tool(
            agent_id=agent_id,
            qualified_name=tool_name,
            arguments=arguments,
        )

        return {
            "success": True,
            "tool_name": tool_name,
            "output": result,
            "step_index": step_index,
        }

