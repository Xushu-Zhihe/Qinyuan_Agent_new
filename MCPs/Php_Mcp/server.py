from __future__ import annotations

from typing import Any

from MCPs.analysis_common import (
    MAX_ARGUMENT_BYTES,
    validate_sandbox_path,
    validate_script,
    validate_timeout,
)
from MCPs.fastmcp_compat import FastMCP


MAX_ARGUMENTS = 32


mcp = FastMCP(
    "qinyuan-php-command-planner",
    instructions=(
        "Prepare general PHP CLI commands for execution inside the current "
        "Agent sandbox. Use run_php for code without PHP opening or closing "
        "tags, and run_php_file for a PHP file under /workspace/input, /workspace/work or /tmp. "
        "This server never executes PHP itself."
    ),
    json_response=True,
)


def _validate_arguments(value: list[str]) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ARGUMENTS:
        raise ValueError(f"arguments must contain at most {MAX_ARGUMENTS} strings")
    normalized: list[str] = []
    for argument in value:
        if not isinstance(argument, str):
            raise ValueError("arguments must contain strings")
        if "\x00" in argument or len(argument.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("PHP argument is invalid or too long")
        normalized.append(argument)
    return normalized


@mcp.tool(title="Run PHP Code In Sandbox")
def run_php(script: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run PHP code without opening or closing tags in the Agent sandbox."""

    normalized_script = validate_script(script, label="PHP script")
    return {
        "sandbox_command": {
            "tool": "php",
            "arguments": [
                "-d",
                "display_errors=stderr",
                "-r",
                normalized_script,
            ],
            "timeout_seconds": validate_timeout(timeout_seconds),
        },
        "request": {
            "operation": "php_inline",
            "script_bytes": len(normalized_script.encode("utf-8")),
        },
    }


@mcp.tool(title="Run PHP File In Sandbox")
def run_php_file(
    path: str,
    arguments: list[str] | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Run a PHP file under /workspace/input, /workspace/work or /tmp with optional arguments."""

    normalized_path = validate_sandbox_path(path)
    normalized_arguments = _validate_arguments(
        [] if arguments is None else arguments
    )
    return {
        "sandbox_command": {
            "tool": "php",
            "arguments": [
                "-d",
                "display_errors=stderr",
                "-f",
                normalized_path,
                "--",
                *normalized_arguments,
            ],
            "timeout_seconds": validate_timeout(timeout_seconds),
        },
        "request": {
            "operation": "php_file",
            "path": normalized_path,
            "argument_count": len(normalized_arguments),
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
