from __future__ import annotations

import re
import shlex
from typing import Any

from MCPs.analysis_common import (
    needs_stage,
    stage_shell_prefix,
    staged_path,
    validate_commands,
    validate_sandbox_path,
    validate_timeout,
)
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-gdb-command-planner",
    instructions=(
        "Prepare non-interactive GDB commands for a binary in the current "
        "Agent sandbox. Artifacts outside /workspace/work are copied to an "
        "executable staging path before gdb starts. `run=True` starts the "
        "program under gdb and yields deterministic addresses (gdb disables "
        "ASLR). pwntools gdb.debug() launches through gdbserver, which keeps "
        "ASLR for the target; use process(aslr=False)+gdb.attach, or start the "
        "target with qinyuan-exec before attaching. This server never starts "
        "GDB itself."
    ),
    json_response=True,
)


# `shell`/`!` would bypass the argv-only sandbox contract. Other commands,
# including gdb's own Python, run under the same container identity as the
# bash/python3 tools, so they add no privilege.
_UNSAFE_COMMAND = re.compile(r"^\s*(?:!|shell\b)", re.IGNORECASE)

_INSPECT_COMMANDS = ("info files", "info registers")
_RUN_COMMANDS = ("starti", "info registers rip", "bt")


def _validate_gdb_commands(commands: list[str]) -> list[str]:
    normalized = validate_commands(commands)
    for command in normalized:
        if _UNSAFE_COMMAND.search(command):
            raise ValueError("gdb command may not invoke a shell")
    return normalized


@mcp.tool(title="Debug Binary With GDB")
def debug_binary(
    path: str,
    commands: list[str] | None = None,
    timeout_seconds: int = 120,
    run: bool = False,
) -> dict[str, Any]:
    """Prepare one GDB batch run.

    ``run=False`` only inspects the ELF. ``run=True`` stops at the entry point
    (``starti``) and reports registers/backtrace, which is the deterministic
    starting point for pwn and reverse-engineering work.
    """

    normalized_path = validate_sandbox_path(path)
    if commands is None:
        gdb_commands = list(_RUN_COMMANDS if run else _INSPECT_COMMANDS)
    else:
        gdb_commands = _validate_gdb_commands(commands)
    timeout = validate_timeout(timeout_seconds)

    staged = needs_stage(normalized_path)
    target = staged_path(normalized_path) if staged else normalized_path
    gdb_argv = ["gdb", "--batch", "--quiet", target]
    for command in ("set pagination off", "set confirm off", *gdb_commands):
        gdb_argv.extend(["-ex", command])
    gdb_argv.extend(["-ex", "quit"])

    request = {
        "operation": "debug",
        "path": normalized_path,
        "staged_path": target if staged else None,
        "run": run,
        "commands": gdb_commands,
    }
    if staged:
        script = stage_shell_prefix(normalized_path, target) + shlex.join(gdb_argv)
        return {
            "sandbox_command": {
                "tool": "bash",
                "arguments": ["--noprofile", "--norc", "-lc", script],
                "timeout_seconds": timeout,
            },
            "request": request,
        }
    return {
        "sandbox_command": {
            "tool": "gdb",
            "arguments": gdb_argv[1:],
            "timeout_seconds": timeout,
        },
        "request": request,
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
