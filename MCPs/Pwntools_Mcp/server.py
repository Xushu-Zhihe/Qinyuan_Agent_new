from __future__ import annotations

from typing import Any

from MCPs.analysis_common import python_command, validate_timeout
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-pwntools-command-planner",
    instructions=(
        "Prepare a pwntools Python script for execution inside the current "
        "Agent sandbox. The MCP server never executes Python itself."
    ),
    json_response=True,
)


@mcp.tool(title="Run Pwntools Script")
def run_pwntools(script: str, timeout_seconds: int = 120) -> dict[str, Any]:
    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pwntools_python", "script_bytes": len(script.encode("utf-8"))},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
