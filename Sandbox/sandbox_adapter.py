"""SandboxService adapter for D8 TaskRun integration with MCPManager.

This adapter bridges MCPManager's agent_id-based interface with D8's
(task_run_id, sandbox_id) pair pattern, enabling MCP tool execution
within the TaskRun's isolated Docker container.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.workspace import D8DockerWorkspace
from Sandbox.models import CommandRequest, CommandResult, SandboxSession
from Sandbox.service import SandboxError, SandboxNotFoundError


class D8SandboxRuntime(Protocol):
    """Protocol for D8's sandbox runtime operations."""

    def get_workspace(self, task_run_id: str) -> D8DockerWorkspace:
        """Retrieve workspace for a TaskRun."""
        ...


@dataclass(frozen=True, slots=True)
class D8SessionBinding:
    """Maps MCP agent_id to D8 TaskRun sandbox."""
    task_run_id: str
    sandbox_id: str
    workspace: D8DockerWorkspace


class D8SandboxAdapter:
    """
    Adapter: MCPManager SandboxService interface → D8 T6TaskRunSandbox.

    Lenient toward LLM:
    - Accepts agent_id in MCP tool calls, maps to TaskRun sandbox
    - Allows multiple MCP tools per TaskRun without per-call approval

    Strict about system:
    - Enforces container isolation (one agent_id → one TaskRun sandbox)
    - Validates all commands against TaskRun's allowed tools
    - Prevents cross-TaskRun contamination via agent_id namespace
    """

    def __init__(
        self,
        runtime: D8SandboxRuntime,
        *,
        registry_path: str | Path = ".test/d8_mcp_registry.json",
        allowed_tools: set[str] | None = None,
    ) -> None:
        """
        Args:
            runtime: D8 runtime providing workspace access
            registry_path: Path to container-compatible MCP registry (default: D8 container registry)
            allowed_tools: MCP-specific allowed tools (default: inherit from workspace)
        """
        self.runtime = runtime
        self.registry_path = Path(registry_path)
        self.allowed_tools = allowed_tools

        self._bindings: dict[str, D8SessionBinding] = {}
        self._lock = RLock()
        self._closed = False

    def bind_agent(
        self,
        agent_id: str,
        task_run_id: str,
    ) -> None:
        """
        Bind an MCP agent_id to a D8 TaskRun sandbox.

        This establishes the mapping MCPManager needs to route tool calls.
        Must be called before execute() for this agent_id.

        Args:
            agent_id: Unique identifier for MCP agent (e.g., "inner_layer_v17")
            task_run_id: D8 TaskRun UUID

        Raises:
            SandboxError: If agent_id already bound or service closed
            SandboxNotFoundError: If TaskRun workspace doesn't exist
        """
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise SandboxError("agent_id cannot be empty")

        with self._lock:
            if self._closed:
                raise SandboxError("D8SandboxAdapter is closed")

            if normalized_agent_id in self._bindings:
                existing = self._bindings[normalized_agent_id]
                if existing.task_run_id != task_run_id:
                    raise SandboxError(
                        f"agent_id {agent_id!r} already bound to different TaskRun"
                    )
                return  # idempotent re-bind to same TaskRun

            # Retrieve workspace from D8 runtime (may raise if TaskRun doesn't exist)
            workspace = self.runtime.get_workspace(task_run_id)

            # Extract sandbox_id from workspace's underlying T6TaskRunSandbox
            # (D8DockerWorkspace wraps T6CommandSandbox which wraps T6TaskRunSandbox)
            sandbox_id = workspace.sandbox.sandbox_id

            self._bindings[normalized_agent_id] = D8SessionBinding(
                task_run_id=task_run_id,
                sandbox_id=sandbox_id,
                workspace=workspace,
            )

    def get_session(self, agent_id: str) -> SandboxSession | None:
        """
        Retrieve sandbox session for MCP agent (SandboxService interface).

        Returns None if agent_id not bound. MCPManager uses this to check
        whether a session exists before attempting tool calls.
        """
        with self._lock:
            binding = self._bindings.get(agent_id)
            if binding is None:
                return None

            # Construct SandboxSession compatible with MCPManager expectations
            from Sandbox.models import SandboxStatus

            return SandboxSession(
                agent_id=agent_id,
                sandbox_id=binding.sandbox_id,
                status=SandboxStatus.RUNNING,
                mounts=None,  # D8 manages mounts internally
            )

    def get_registry_path(self) -> Path:
        """
        Return path to container-compatible MCP registry.

        Used by MCPManager to load tool configurations with container paths.
        """
        return self.registry_path

    def execute(
        self,
        agent_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        """
        Execute MCP tool command in TaskRun sandbox (SandboxService interface).

        Lenient toward input:
        - Accepts any tool listed in registry (filtered by allowed_tools)
        - Tolerates repeated calls from same agent_id

        Strict about execution:
        - Only executes in bound TaskRun's container
        - Validates tool against allowed list
        - Preserves D8's timeout and resource constraints
        """
        with self._lock:
            binding = self._bindings.get(agent_id)

        if binding is None:
            raise SandboxNotFoundError(
                f"Agent {agent_id!r} not bound to any TaskRun sandbox"
            )

        # Validate tool against allowed list (lenient: check at execution, not registration)
        if self.allowed_tools is not None and request.tool not in self.allowed_tools:
            raise SandboxError(
                f"Tool {request.tool!r} not allowed for MCP execution"
            )

        # Delegate to D8DockerWorkspace's execute method
        # (This routes through T6CommandSandbox → T6TaskRunSandbox → Docker)
        return binding.workspace.execute(request)

    def interrupt_for_agent(self, agent_id: str) -> str | None:
        """
        Interrupt running command for MCP agent (SandboxService interface).

        Returns sandbox_id if interrupted, None if no active session.
        """
        with self._lock:
            binding = self._bindings.get(agent_id)
            if binding is None:
                return None

            # D8DockerWorkspace doesn't expose interrupt directly;
            # route through its underlying T6CommandSandbox
            binding.workspace.sandbox.interrupt(binding.sandbox_id)
            return binding.sandbox_id

    def unbind_agent(self, agent_id: str) -> None:
        """
        Remove agent_id binding (does not destroy sandbox).

        Use when MCP session ends but TaskRun continues.
        """
        with self._lock:
            self._bindings.pop(agent_id, None)

    def close(self) -> None:
        """
        Close adapter (does not destroy D8 sandboxes).

        D8 manages TaskRun sandbox lifecycle independently.
        This only clears bindings and prevents new registrations.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._bindings.clear()
