from __future__ import annotations

from typing import Protocol

from Sandbox.models import CommandRequest, CommandResult, SandboxMounts


class SandboxRuntime(Protocol):
    def create(self, agent_id: str, mounts: SandboxMounts) -> str:
        """Create a sandbox and return its identifier."""
        ...

    def execute(
        self,
        sandbox_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        """Execute one command inside a sandbox."""
        ...

    def interrupt(self, sandbox_id: str) -> None:
        """Stop a sandbox without deleting it while output may still be active."""
        ...

    def destroy(self, sandbox_id: str) -> None:
        """Destroy one sandbox."""
        ...

    def close(self) -> None:
        """Release resources held by the runtime."""
        ...
