"""Dedicated Windows/Wine profile.

Wine needs a writable prefix of a few hundred MiB and more memory than the
default 512 MiB Linux sandbox. This profile is opt-in (QINYUAN_SANDBOX_PROFILE
=windows); the default TaskRun profile and its boundaries are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.settings import SandboxSettings

WINDOWS_MEMORY = "2g"
WINDOWS_CPU = 1_500_000_000
WINDOWS_PROCESS_LIMIT = 256
WINDOWS_WORKSPACE = "1g"
WINDOWS_TEMPORARY = "256m"


@dataclass(frozen=True)
class WindowsSandboxSettings(SandboxSettings):
    memory_limit: str = WINDOWS_MEMORY
    memory_swap_limit: str = WINDOWS_MEMORY
    cpu_limit: int = WINDOWS_CPU
    process_limit: int = WINDOWS_PROCESS_LIMIT
    workspace_size: str = WINDOWS_WORKSPACE
    temporary_size: str = WINDOWS_TEMPORARY


class WindowsTaskRunSandbox(T6TaskRunSandbox):
    """T6 container with a larger writable work tree and Wine environment."""

    def _spec(self) -> dict[str, object]:
        spec = super()._spec()
        environment = dict(spec["environment"])
        environment.update(
            {
                "WINEPREFIX": "/workspace/work/.wine",
                "WINEARCH": "win64",
                "WINEDEBUG": "-all",
            }
        )
        spec["environment"] = environment
        return spec
