from __future__ import annotations

from typing import Any

from MCPs.analysis_common import python_command, validate_timeout
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-z3-command-planner",
    instructions=(
        "Prepare a Z3 Python constraint-solving script for the current Agent "
        "sandbox. This server never executes Python itself."
    ),
    json_response=True,
)


@mcp.tool(title="Solve Constraints With Z3")
def solve_constraints(script: str, timeout_seconds: int = 120) -> dict[str, Any]:
    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "z3_python", "script_bytes": len(script.encode("utf-8"))},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
