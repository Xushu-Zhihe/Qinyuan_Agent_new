"""Dedicated Windows/Wine profile.

Wine needs a writable prefix of a few hundred MiB and more memory than the
default 512 MiB Linux sandbox. This profile is opt-in (QINYUAN_SANDBOX_PROFILE
=windows); the default TaskRun profile and its boundaries are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.settings import SandboxSettings

WINDOWS_MEMORY = "4g"
WINDOWS_CPU = 2_000_000_000
WINDOWS_PROCESS_LIMIT = 512
WINDOWS_WORKSPACE = "4g"
WINDOWS_TEMPORARY = "512m"


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
                "WINEPREFIX": "/workspace/work/.wine64",
                "WINEPREFIX32": "/workspace/work/.wine32",
                "WINEARCH": "win64",
                "WINEDEBUG": "-all",
                # /tmp is noexec; wineserver32 needs a writable/exec temp dir.
                "TMPDIR": "/workspace/work/tmp",
            }
        )
        spec["environment"] = environment
        return spec
