"""
D8SandboxAdapter: Bridge between MCPManager and D8DockerWorkspace.

MCPManager expects SandboxService interface with:
  - get_session(agent_id) -> SandboxSession | None
  - execute(request: CommandRequest) -> CommandResult
  - interrupt_for_agent(agent_id) -> str | None

D8 uses (task_run_id, sandbox_id) pair with D8DockerWorkspace.

This adapter maps:
  agent_id (from MCPManager) → task_run_id (D8 TaskRun identifier)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from Sandbox.models import CommandRequest, CommandResult, SandboxSession, SandboxStatus
from Sandbox.workspace import D8DockerWorkspace

if TYPE_CHECKING:
    from agent_core.state import D8TaskRunState


@dataclass(frozen=True, slots=True)
class D8SandboxBinding:
    """Binding between agent_id and D8 TaskRun sandbox."""

    task_run_id: str
    sandbox_id: str
    workspace: D8DockerWorkspace


class D8SandboxAdapter:
    """
    Adapter implementing SandboxService interface for MCPManager.

    Maps agent_id → D8 TaskRun sandbox binding.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, D8SandboxBinding] = {}

    def bind_task_run(
        self,
        agent_id: str,
        task_run_id: str,
        sandbox_id: str,
        workspace: D8DockerWorkspace,
    ) -> None:
        """
        Register a D8 TaskRun's sandbox for MCP tool execution.

        Args:
            agent_id: Identifier for MCPManager (typically task_run_id)
            task_run_id: D8 TaskRun identifier
            sandbox_id: Docker container ID
            workspace: D8DockerWorkspace instance
        """
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise ValueError("agent_id cannot be empty")

        self._bindings[normalized_agent_id] = D8SandboxBinding(
            task_run_id=task_run_id,
            sandbox_id=sandbox_id,
            workspace=workspace,
        )

    def unbind_agent(self, agent_id: str) -> None:
        """Remove binding when TaskRun completes."""
        self._bindings.pop(agent_id, None)

    def get_session(self, agent_id: str) -> SandboxSession | None:
        """
        SandboxService interface: retrieve session by agent_id.

        Returns minimal SandboxSession for MCPManager validation.
        """
        binding = self._bindings.get(agent_id)
        if binding is None:
            return None

        return SandboxSession(
            agent_id=agent_id,
            sandbox_id=binding.sandbox_id,
            status=SandboxStatus.RUNNING,
            mounts=None,  # D8DockerWorkspace manages mounts internally
        )

    def execute(self, request: CommandRequest) -> CommandResult:
        """
        SandboxService interface: execute command in agent's sandbox.

        Routes to D8DockerWorkspace.execute() with proper error handling.
        """
        binding = self._bindings.get(request.agent_id)
        if binding is None:
            return CommandResult(
                agent_id=request.agent_id,
                sandbox_id="unknown",
                executable=request.executable,
                arguments=request.arguments,
                status="error",
                exit_code=-1,
                stdout="",
                stderr=f"No sandbox bound for agent_id={request.agent_id!r}",
                timed_out=False,
            )

        try:
            # D8DockerWorkspace.execute() returns CommandResult
            return binding.workspace.execute(
                executable=request.executable,
                arguments=request.arguments,
                timeout=request.timeout,
                env=request.env,
            )
        except Exception as error:
            return CommandResult(
                agent_id=request.agent_id,
                sandbox_id=binding.sandbox_id,
                executable=request.executable,
                arguments=request.arguments,
                status="error",
                exit_code=-1,
                stdout="",
                stderr=f"Execution failed: {error}",
                timed_out=False,
            )

    def interrupt_for_agent(self, agent_id: str) -> str | None:
        """
        SandboxService interface: interrupt running command.

        D8DockerWorkspace does not currently support interruption.
        Returns None (no-op).
        """
        binding = self._bindings.get(agent_id)
        if binding is None:
            return None

        # TODO: Implement interruption if D8DockerWorkspace supports it
        return None
