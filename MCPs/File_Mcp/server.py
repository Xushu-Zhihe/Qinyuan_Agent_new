from __future__ import annotations

from typing import Any

from MCPs.analysis_common import validate_sandbox_path, validate_timeout
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
    normalized_path = validate_sandbox_path(path)
    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "file",
            "arguments": ["--brief", "--dereference", "--", normalized_path],
            "timeout_seconds": timeout,
        },
        "request": {"operation": "file_type", "path": normalized_path},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
