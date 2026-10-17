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

from agent_core.actions import MCPAction, MCPToolCall
from agent_core.intent_postconditions import Postcondition

if TYPE_CHECKING:
    from MCPs.Mcp_client.client import MCPManager
    from Sandbox.d8_workspace import D8DockerWorkspace


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

    async def execute(self, action: MCPAction, plan_id: str) -> MCPExecutionResult:
        """
        Execute an MCPAction Plan: sequentially call each MCPToolCall,
        validate postconditions, return evidence.

        Lenient: tolerates tool failures (captured in evidence)
        Strict: enforces timeouts, authorization, postcondition validation
        """
        tool_outputs: list[dict] = []
        error: str | None = None

        try:
            # Execute each tool call in sequence
            for idx, call in enumerate(action.calls):
                try:
                    result = await self._execute_tool_call(
                        call, plan_id, step_index=idx
                    )
                    tool_outputs.append(result)
                except Exception as exc:
                    # Lenient: capture tool failure as evidence rather than aborting
                    tool_outputs.append(
                        {
                            "success": False,
                            "error": str(exc),
                            "tool_name": call.tool_name,
                        }
                    )
                    error = f"Tool {call.tool_name} failed: {exc}"
                    break  # Stop sequence on first failure

            # Validate postconditions
            postconditions_met = await self._check_postconditions(
                action.conditions, tool_outputs
            )

            return MCPExecutionResult(
                success=error is None and postconditions_met,
                tool_outputs=tuple(tool_outputs),
                postconditions_met=postconditions_met,
                error=error,
            )

        except Exception as exc:
            # Strict: surface system errors (sandbox failure, authorization)
            return MCPExecutionResult(
                success=False,
                tool_outputs=tuple(tool_outputs),
                postconditions_met=False,
                error=f"System error: {exc}",
            )

    async def _execute_tool_call(
        self, call: MCPToolCall, plan_id: str, step_index: int
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
            tool_name=call.tool_name,
            arguments=call.parameters,
            timeout=call.timeout_seconds,
        )

        return {
            "success": True,
            "tool_name": call.tool_name,
            "output": result,
            "step_index": step_index,
        }

    async def _check_postconditions(
        self, conditions: tuple[Postcondition, ...], tool_outputs: list[dict]
    ) -> bool:
        """
        Validate postconditions against tool execution results.

        Lenient: semantic checks (OutputContains allows fuzzy matching)
        Strict: FileSha256 requires exact match
        """
        if not conditions:
            return True  # No postconditions = trivially satisfied

        for condition in conditions:
            if not await self._check_single_postcondition(condition, tool_outputs):
                return False

        return True

    async def _check_single_postcondition(
        self, condition: Postcondition, tool_outputs: list[dict]
    ) -> bool:
        """Check one postcondition against captured evidence."""
        from agent_core.intent_postconditions import FileSha256, OutputContains

        if isinstance(condition, OutputContains):
            # Lenient: search for substring in any tool output
            search_text = condition.text.lower()
            for output_dict in tool_outputs:
                if output_dict.get("success"):
                    output_str = str(output_dict.get("output", "")).lower()
                    if search_text in output_str:
                        return True
            return False

        elif isinstance(condition, FileSha256):
            # Strict: verify exact file hash via workspace
            try:
                actual_hash = await self.workspace.hash_file(condition.path)
                return actual_hash == condition.expected_sha256
            except Exception:
                return False

        else:
            # Unknown postcondition type: fail-safe (strict)
            return False
