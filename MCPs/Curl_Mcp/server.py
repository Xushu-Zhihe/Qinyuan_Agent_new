from __future__ import annotations

from typing import Any

from MCPs.analysis_common import MAX_ARGUMENT_BYTES, validate_timeout
from MCPs.fastmcp_compat import FastMCP


MAX_ARGUMENTS = 64
MAX_ARGUMENT_TOTAL_BYTES = 128 * 1024


mcp = FastMCP(
    "qinyuan-curl-command-planner",
    instructions=(
        "Prepare general curl HTTP requests for execution inside the current "
        "Agent sandbox. Accept curl arguments as an argv list, including URLs, "
        "headers, request bodies, and output options. This server never executes "
        "curl itself. Network access is controlled by the Agent sandbox."
    ),
    json_response=True,
)


def _validate_arguments(value: list[str]) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ARGUMENTS:
        raise ValueError(
            f"arguments must contain between 1 and {MAX_ARGUMENTS} strings"
        )

    normalized: list[str] = []
    total_bytes = 0
    for argument in value:
        if not isinstance(argument, str) or not argument:
            raise ValueError("arguments must contain non-empty strings")
        if "\x00" in argument:
            raise ValueError("arguments must not contain null bytes")
        argument_bytes = len(argument.encode("utf-8"))
        if argument_bytes > MAX_ARGUMENT_BYTES:
            raise ValueError("each curl argument must not exceed 16 KiB")
        total_bytes += argument_bytes
        if total_bytes > MAX_ARGUMENT_TOTAL_BYTES:
            raise ValueError("curl arguments must not exceed 128 KiB")
        normalized.append(argument)
    return normalized


@mcp.tool(title="Run Curl In Sandbox")
def run_curl(arguments: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
    """Run a curl request with argv-style arguments in the Agent sandbox."""

    normalized_arguments = _validate_arguments(arguments)
    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "curl",
            "arguments": normalized_arguments,
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "curl",
            "argument_count": len(normalized_arguments),
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
