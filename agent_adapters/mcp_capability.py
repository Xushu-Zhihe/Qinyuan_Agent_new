"""
MCP Tool Execution Capability for D8 Plans.

Handles Plans with kind='mcp_action', executing MCP tool calls
through MCPManager with D8SandboxAdapter.

Design:
- Lenient toward LLM: accepts MCPAction with multiple tool calls
- Strict about execution: validates tool_name, enforces timeout, tracks evidence
- Strict about boundaries: all execution happens in isolated sandbox
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_core.actions import MCPAction, MCPToolCall
from agent_core.dual_llm_contracts import D3PlanResult, D3RecoveryRequired

if TYPE_CHECKING:
    from agent_adapters.sandbox_adapter import D8SandboxAdapter
    from MCPs.Mcp_client.client import MCPManager
    from agent_core.state import D8TaskRunState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPExecutionResult:
    """Result from executing one MCPToolCall."""

    tool_name: str
    success: bool
    output: str  # JSON-serialized tool output or error message
    execution_time_ms: int


@dataclass(frozen=True, slots=True)
class MCPPlanBinding:
    """Extended D3PlanBinding with MCP-specific fields."""

    # Base D3PlanBinding fields
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    observation_mode: str
    artifact_version_id: str
    proposal_request_id: str
    rule_binding_sha256: str
    execution_id: str

    # MCP-specific fields
    kind: str  # Must be 'mcp_action'
    action: MCPAction  # MCPAction with tool calls
    agent_id: str  # For D8SandboxAdapter lookup


class MCPCapability:
    """
    Executes MCP tool calls within D8 Plans.

    Integrates MCPManager with D8SandboxAdapter for safe tool execution.
    """

    def __init__(
        self,
        mcp_manager: MCPManager,
        sandbox_adapter: D8SandboxAdapter,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.sandbox_adapter = sandbox_adapter

    async def run_plan(
        self,
        binding: MCPPlanBinding,
        token,  # CancellationToken
    ) -> bool:
        """
        Execute MCP tool calls for a Plan.

        Args:
            binding: MCPPlanBinding with action + agent_id
            token: CancellationToken for interruption

        Returns:
            True if Plan should retry (currently always False)

        Raises:
            D3RecoveryRequired: If execution cannot proceed
        """
        if binding.kind != "mcp_action":
            raise D3RecoveryRequired(f"mcp_capability_wrong_kind:{binding.kind}")

        action = binding.action
        if not isinstance(action, MCPAction):
            raise D3RecoveryRequired("mcp_capability_action_not_mcp")

        # Verify sandbox is bound
        session = self.sandbox_adapter.get_session(binding.agent_id)
        if session is None:
            raise D3RecoveryRequired(f"mcp_capability_no_sandbox:{binding.agent_id}")

        # Execute tool calls sequentially
        results: list[MCPExecutionResult] = []
        for call in action.calls:
            if token.is_cancelled:
                raise D3RecoveryRequired("mcp_capability_cancelled")

            result = await self._execute_tool_call(binding.agent_id, call)
            results.append(result)

            # Stop on first failure (strict about execution)
            if not result.success:
                break

        # Store execution results as evidence
        # TODO: Integrate with FileD8Evidence / artifact_proofs.py
        proof_id = self._store_evidence(binding, results)

        # Complete Plan with results
        # TODO: Convert to D3PlanResult and call control.complete_plan()
        logger.info(
            "MCP Plan %s completed: %d calls, proof=%s",
            binding.plan_id,
            len(results),
            proof_id,
        )

        return False  # Plan complete, no retry needed

    async def _execute_tool_call(
        self,
        agent_id: str,
        call: MCPToolCall,
    ) -> MCPExecutionResult:
        """
        Execute single MCP tool call.

        Lenient toward input: accepts any tool_name string
        Strict about execution: validates against registry, enforces timeout
        """
        import time

        start_time_ms = int(time.time() * 1000)

        try:
            # Validate tool exists in registry
            available_tools = self.mcp_manager.list_registered_tools()
            if call.tool_name not in available_tools:
                return MCPExecutionResult(
                    tool_name=call.tool_name,
                    success=False,
                    output=json.dumps({
                        "error": "tool_not_found",
                        "available": list(available_tools.keys()),
                    }),
                    execution_time_ms=int(time.time() * 1000) - start_time_ms,
                )

            # Execute tool through MCPManager
            # MCPManager._execute_bound_tool(agent_id, tool_name, parameters, timeout)
            result = await self.mcp_manager._execute_bound_tool(
                agent_id,
                call.tool_name,
                call.parameters,
                timeout=call.timeout_seconds,
            )

            execution_time_ms = int(time.time() * 1000) - start_time_ms

            # Parse result
            if result.get("success", False):
                return MCPExecutionResult(
                    tool_name=call.tool_name,
                    success=True,
                    output=json.dumps(result.get("output", {})),
                    execution_time_ms=execution_time_ms,
                )
            else:
                return MCPExecutionResult(
                    tool_name=call.tool_name,
                    success=False,
                    output=json.dumps({
                        "error": result.get("error", "unknown_error"),
                        "details": result.get("details"),
                    }),
                    execution_time_ms=execution_time_ms,
                )

        except Exception as error:
            execution_time_ms = int(time.time() * 1000) - start_time_ms
            logger.exception("MCP tool call failed: %s", call.tool_name)
            return MCPExecutionResult(
                tool_name=call.tool_name,
                success=False,
                output=json.dumps({
                    "error": "execution_exception",
                    "message": str(error),
                }),
                execution_time_ms=execution_time_ms,
            )

    def _store_evidence(
        self,
        binding: MCPPlanBinding,
        results: list[MCPExecutionResult],
    ) -> str:
        """
        Store MCP execution results as evidence.

        Returns proof_id for Plan completion.

        TODO: Integrate with:
        - agent_adapters.conclusion_evidence.FileD8Evidence
        - agent_core.artifact_proofs (register_proof)
        """
        # Placeholder: serialize results as JSON
        evidence_json = json.dumps(
            {
                "plan_id": binding.plan_id,
                "execution_id": binding.execution_id,
                "tool_calls": len(results),
                "results": [
                    {
                        "tool_name": r.tool_name,
                        "success": r.success,
                        "output": r.output,
                        "execution_time_ms": r.execution_time_ms,
                    }
                    for r in results
                ],
            },
            indent=2,
        )

        # TODO: Write to FileD8Evidence store
        # For now, return placeholder proof_id
        proof_id = f"mcp:{binding.plan_id}:{binding.execution_id[:8]}"

        logger.debug("Stored MCP evidence: proof_id=%s, size=%d bytes", proof_id, len(evidence_json))

        return proof_id
