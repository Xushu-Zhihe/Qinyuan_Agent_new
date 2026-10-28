"""MCP Tool Executor for D8 Plan continuous tool calling.

Replaces "one HTTP request per Plan" with rolling MCP tool selection,
maintaining budget, step counters, and Deadline constraints.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.operation_accounting import (
    OperationBudgetExhausted,
    D4OperationStart,
    D4OperationCompletion,
)

if TYPE_CHECKING:
    from MCPs.Mcp_client.client import MCPManager, AgentMCPClient
    from Sandbox.sandbox_adapter import D8SandboxAdapter
    from agent_core.control_store import D4ControlStore


@dataclass(frozen=True, slots=True)
class MCPExecutionBudget:
    """
    Execution budget for MCP tool calls within one Intent.

    Lenient toward LLM:
    - No hard limit on number of Plans per Intent (removed "max 2")
    - Allows rolling tool selection based on current evidence

    Strict about execution:
    - Total step count across all Plans tracked
    - Cumulative time against Deadline enforced
    - Budget exhaustion triggers review, not automatic failure
    """
    max_steps_per_intent: int
    max_tool_calls_per_step: int
    max_cumulative_seconds: float
    deadline_at: float

    def __post_init__(self) -> None:
        if self.max_steps_per_intent < 1:
            raise ValueError("mcp_budget_steps_too_low")
        if self.max_tool_calls_per_step < 1:
            raise ValueError("mcp_budget_tools_per_step_too_low")
        if self.max_cumulative_seconds <= 0:
            raise ValueError("mcp_budget_timeout_invalid")


@dataclass(frozen=True, slots=True)
class MCPExecutionState:
    """
    Current execution state for MCP tool call tracking.

    Tracks:
    - steps_consumed: Total Plan executions for this Intent
    - tools_consumed: Total MCP tool calls across all Plans
    - seconds_consumed: Cumulative execution time
    - last_tool_results: Evidence from most recent tool calls
    """
    intent_id: str
    steps_consumed: int
    tools_consumed: int
    seconds_consumed: float
    last_tool_results: tuple[str, ...]  # tool output summaries

    def exceeds_budget(self, budget: MCPExecutionBudget, current_time: float) -> tuple[bool, str]:
        """
        Check if current state exceeds budget.

        Returns:
            (exceeded, reason) tuple
        """
        if self.steps_consumed >= budget.max_steps_per_intent:
            return True, f"step_limit_reached_{self.steps_consumed}"
        if current_time >= budget.deadline_at:
            return True, f"deadline_exceeded_{current_time:.1f}>{budget.deadline_at:.1f}"
        if self.seconds_consumed >= budget.max_cumulative_seconds:
            return True, f"cumulative_timeout_{self.seconds_consumed:.1f}s"
        return False, ""


class MCPPlanExecutor:
    """
    Executor for continuous MCP tool calling within D8 Plans.

    Responsibilities:
    - Bind MCP agent_id to TaskRun sandbox
    - Execute rolling tool selections from inner layer
    - Track budget and step consumption
    - Generate Evidence for each tool call
    - Trigger review when budget exhausted or tools blocked

    Does NOT:
    - Make tool selection decisions (that's inner layer's job)
    - Automatically create Facts (outer layer decides via review)
    - Retry failed tools (inner layer can propose retry in next Plan)
    """

    def __init__(
        self,
        mcp_manager: MCPManager,
        sandbox_adapter: D8SandboxAdapter,
        control_store: D4ControlStore,
        *,
        default_budget: MCPExecutionBudget | None = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.sandbox_adapter = sandbox_adapter
        self.control_store = control_store
        self.default_budget = default_budget or MCPExecutionBudget(
            max_steps_per_intent=20,
            max_tool_calls_per_step=5,
            max_cumulative_seconds=300.0,
            deadline_at=float('inf'),
        )

        # Track execution state per Intent
        self._intent_states: dict[str, MCPExecutionState] = {}

        # Track bound agent clients per agent_id
        self._bound_agents: dict[str, AgentMCPClient] = {}

    async def bind_intent(
        self,
        intent_id: str,
        task_run_id: str,
        agent_id: str,
        *,
        budget: MCPExecutionBudget | None = None,
    ) -> AgentMCPClient:
        """
        Bind an Intent to MCP execution context.

        Args:
            intent_id: D8 Intent UUID
            task_run_id: D8 TaskRun UUID
            agent_id: MCP agent identifier (e.g., "inner_v17_{intent_id}")
            budget: Optional override for default budget

        Returns:
            AgentMCPClient bound to this Intent's sandbox

        Raises:
            SandboxError: If TaskRun sandbox doesn't exist
        """
        # Bind sandbox adapter to TaskRun
        self.sandbox_adapter.bind_agent(agent_id, task_run_id)

        # Initialize execution state
        self._intent_states[intent_id] = MCPExecutionState(
            intent_id=intent_id,
            steps_consumed=0,
            tools_consumed=0,
            seconds_consumed=0.0,
            last_tool_results=(),
        )

        # Bind MCP agent to sandbox
        agent_client = await self.mcp_manager.bind_agent(
            agent_id,
            allowed_tools=None,  # inherit from registry
        )

        # Store bound agent for tool call routing
        self._bound_agents[agent_id] = agent_client

        return agent_client

    async def execute_plan_step(
        self,
        intent_id: str,
        agent_id: str,
        plan_id: str,
        task_run_id: str,
        tool_selections: list[tuple[str, dict]],  # [(tool_name, params), ...]
        current_time: float,
    ) -> tuple[bool, list[dict]]:
        """
        Execute one Plan step with MCP tool calls.

        Lenient toward input:
        - Accepts 0-N tool selections (inner layer decides count)
        - Allows partial success (some tools fail, others succeed)

        Strict about execution:
        - Enforces budget before executing tools
        - Records all tool results as Evidence
        - Returns budget status for inner layer to decide next action

        Args:
            intent_id: Current Intent UUID
            agent_id: MCP agent identifier
            plan_id: Current Plan UUID
            task_run_id: D8 TaskRun UUID (for operation accounting)
            tool_selections: List of (tool_name, parameters) from inner layer
            current_time: Current timestamp for deadline check

        Returns:
            (budget_available, tool_results) tuple
            - budget_available: False if budget exhausted
            - tool_results: List of {tool, params, content, is_error, stderr} dicts

        Raises:
            ExecutionRecoveryRequired: If Intent not bound or sandbox unavailable
            OperationBudgetExhausted: If D4/D8 operation budget exhausted
        """
        state = self._intent_states.get(intent_id)
        if state is None:
            raise ExecutionRecoveryRequired(f"mcp_intent_not_bound_{intent_id}")

        # Check MCP execution budget BEFORE execution
        budget = self.default_budget  # TODO: per-Intent budget override
        exceeded, reason = state.exceeds_budget(budget, current_time)
        if exceeded:
            return False, []

        # Enforce per-step tool limit
        if len(tool_selections) > budget.max_tool_calls_per_step:
            tool_selections = tool_selections[:budget.max_tool_calls_per_step]

        # Start D4 operation accounting for this step
        operation_id = str(uuid4())
        operation_start = D4OperationStart(
            operation_id=operation_id,
            task_run_id=task_run_id,
            category="tool_dispatch",  # MCP tool calls count as tool_dispatch
            turn_id=plan_id,  # Use plan_id as turn_id for this operation
            requested_by="mcp_executor",
            scope=f"intent_{intent_id}_step_{state.steps_consumed}",
        )

        try:
            # Record operation start (may raise OperationBudgetExhausted)
            self.control_store.record_operation_start(operation_start)
        except OperationBudgetExhausted:
            # D4/D8 budget exhausted - return budget unavailable
            return False, []

        # Execute tools
        results: list[dict] = []
        step_start = time.time()
        operation_result = "completed"

        try:
            for tool_name, params in tool_selections:
                try:
                    output = await self._call_mcp_tool(intent_id, agent_id, tool_name, params)
                    results.append({
                        "tool": tool_name,
                        "params": params,
                        **output,  # content, is_error, stderr
                    })
                except Exception as exc:
                    results.append({
                        "tool": tool_name,
                        "params": params,
                        "content": [],
                        "is_error": True,
                        "stderr": str(exc),
                    })
                    if "not_satisfied" in str(exc).lower():
                        operation_result = "not_satisfied"
                    elif operation_result == "completed":
                        operation_result = "failed"

            step_duration = time.time() - step_start

            # Update MCP execution state
            new_state = MCPExecutionState(
                intent_id=intent_id,
                steps_consumed=state.steps_consumed + 1,
                tools_consumed=state.tools_consumed + len(results),
                seconds_consumed=state.seconds_consumed + step_duration,
                last_tool_results=tuple(
                    str(r["content"]) for r in results if not r["is_error"]
                ),
            )
            self._intent_states[intent_id] = new_state

            # Record D4 operation completion
            operation_completion = D4OperationCompletion(
                operation_id=operation_id,
                task_run_id=task_run_id,
                category="tool_dispatch",
                result=operation_result,  # type: ignore[arg-type]
                summary=f"mcp_tools_{len(results)}_calls_{len([r for r in results if not r['is_error']])}_success",
            )
            self.control_store.record_operation_completion(operation_completion)

            # Check MCP execution budget AFTER execution
            exceeded, reason = new_state.exceeds_budget(budget, current_time)
            return not exceeded, results

        except Exception:
            # Record failed operation
            operation_completion = D4OperationCompletion(
                operation_id=operation_id,
                task_run_id=task_run_id,
                category="tool_dispatch",
                result="failed",
                summary=f"mcp_step_failed_{len(results)}_partial_results",
            )
            self.control_store.record_operation_completion(operation_completion)
            raise

    async def _call_mcp_tool(
        self,
        intent_id: str,
        agent_id: str,
        tool_name: str,
        params: dict,
    ) -> dict:
        """
        Call MCP tool through bound agent client.

        Routes through MCPManager → D8SandboxAdapter → D8DockerWorkspace.

        Args:
            intent_id: Current Intent UUID
            agent_id: MCP agent identifier (e.g., "inner_v17_{intent_id}")
            tool_name: Qualified MCP tool name (e.g., "filesystem__read_file")
            params: Tool-specific parameters dict

        Returns:
            {
                "content": list[dict],  # Tool output blocks
                "is_error": bool,
                "stderr": str | None,   # Stderr from tool execution
            }

        Raises:
            MCPClientError: If tool not allowed for agent
            SandboxError: If sandbox execution fails
        """
        from MCPs.Mcp_client.client import AgentMCPClient, MCPExecutionResult

        # Retrieve bound agent client
        # NOTE: AgentMCPClient must be stored during bind_intent()
        agent_client = self._bound_agents.get(agent_id)
        if agent_client is None:
            raise ExecutionRecoveryRequired(f"mcp_agent_not_bound_{agent_id}")

        # Execute tool through MCP layer
        result: MCPExecutionResult = await agent_client.call_tool(tool_name, params)

        return {
            "content": result.content,
            "is_error": result.is_error,
            "stderr": result.stderr,
        }

    def get_intent_state(self, intent_id: str) -> MCPExecutionState | None:
        """Retrieve current execution state for Intent."""
        return self._intent_states.get(intent_id)

    def unbind_intent(self, intent_id: str, agent_id: str) -> None:
        """
        Unbind Intent from MCP execution (Intent completed or failed).

        Clears execution state and bound agent client,
        but preserves Evidence in storage.
        """
        self._intent_states.pop(intent_id, None)
        self._bound_agents.pop(agent_id, None)
        self.sandbox_adapter.unbind_agent(agent_id)


# Integration point: Replace existing Plan execution in multi_turn_application.py
#
# OLD (D8 current):
#   if plan_binding.observation_mode == "new_observation":
#       proof = self.proof_store.read(...)
#       return await self._review_d8_observation(...)
#
# NEW (D22):
#   if plan_binding.template == "mcp_tool_call":
#       budget_ok, results = await self.mcp_executor.execute_plan_step(
#           plan_binding.intent_id, plan_binding.plan_id, tool_selections, time.time()
#       )
#       # Store results as Evidence
#       # If budget exhausted, trigger review
#       # If tools successful, inner layer decides next action
