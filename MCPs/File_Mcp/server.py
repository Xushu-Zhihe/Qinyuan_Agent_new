from __future__ import annotations

from typing import Any

from agent_core.file_inspection import inspection_arguments
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-file-command-planner",
    instructions=(
        "Prepare file-type inspection commands for the current Agent sandbox. "
        "This server never reads files or executes commands itself."
    ),
    json_response=True,
)


@mcp.tool(title="Identify Sandbox File")
def inspect_file(path: str, timeout_seconds: int = 30) -> dict[str, Any]:
    arguments = inspection_arguments(path)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be between 1 and 60")
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": list(arguments),
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "file_type", "path": path},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
