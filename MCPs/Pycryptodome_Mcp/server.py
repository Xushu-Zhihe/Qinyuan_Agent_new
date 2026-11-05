from __future__ import annotations

from typing import Any

from MCPs.analysis_common import python_command, script_source, validate_timeout
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-pycryptodome-command-planner",
    instructions=(
        "Prepare a PyCryptodome Python script for execution inside the current "
        "Agent sandbox. This server never executes Python itself."
    ),
    json_response=True,
)


@mcp.tool(title="Run PyCryptodome Script")
def run_crypto(script: str | None = None, timeout_seconds: int = 120, script_path: str | None = None) -> dict[str, Any]:
    """Run inline Python or reuse script_path in this sandbox; provide exactly one."""
    source, metadata = script_source(script, script_path)
    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(source),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pycryptodome_python", **metadata},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
