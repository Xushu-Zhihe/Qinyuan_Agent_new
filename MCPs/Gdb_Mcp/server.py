from __future__ import annotations

import re
from typing import Any

from MCPs.analysis_common import (
    validate_commands,
    validate_sandbox_path,
    validate_timeout,
)
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-gdb-command-planner",
    instructions=(
        "Prepare non-interactive GDB commands for a binary in the current "
        "Agent sandbox. This server never starts GDB itself."
    ),
    json_response=True,
)


_UNSAFE_COMMAND = re.compile(
    r"^\s*(?:!|shell\b|source\b|python(?:-interactive)?\b|"
    r"define\b|document\b|set\s+exec-wrapper\b)",
    re.IGNORECASE,
)


def _validate_gdb_commands(commands: list[str]) -> list[str]:
    normalized = validate_commands(commands)
    for command in normalized:
        if _UNSAFE_COMMAND.search(command):
            raise ValueError("gdb command may not invoke a shell, source, or embedded code")
    return normalized


@mcp.tool(title="Debug Binary With GDB")
def debug_binary(
    path: str,
    commands: list[str] | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    normalized_path = validate_sandbox_path(path)
    gdb_commands = _validate_gdb_commands(commands or ["info files", "info registers"])
    timeout = validate_timeout(timeout_seconds)
    arguments = ["--batch", "--nx", "--quiet", normalized_path]
    for command in gdb_commands:
        arguments.extend(["-ex", command])
    arguments.extend(["-ex", "quit"])
    return {
        "sandbox_command": {
            "tool": "gdb",
            "arguments": arguments,
            "timeout_seconds": timeout,
        },
        "request": {"operation": "debug", "path": normalized_path, "commands": gdb_commands},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
