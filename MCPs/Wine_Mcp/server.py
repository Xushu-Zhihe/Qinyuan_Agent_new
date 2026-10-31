from __future__ import annotations

from typing import Any

from MCPs.analysis_common import validate_sandbox_path, validate_timeout
from MCPs.fastmcp_compat import FastMCP
from MCPs.Session_Mcp.server import session_capture, session_send, session_stop, session_start


mcp = FastMCP(
    "qinyuan-wine-command-planner",
    instructions=(
        "Prepare Wine64 plans for the dedicated Windows profile. `wine_init` "
        "creates the prefix once; `wine_run` executes a PE in one shot; "
        "`wine_debug` starts winedbg in a tmux session so breakpoints survive "
        "across tool calls. This server never executes Wine itself."
    ),
    json_response=True,
)

WINE = "/usr/bin/wine"


def _target(path: str) -> str:
    # Wine maps the PE itself; the 0444/noexec input mount is readable, so no
    # executable staging copy is required here.
    return validate_sandbox_path(path)


@mcp.tool(title="Initialize Wine Prefix")
def wine_init(timeout_seconds: int = 180) -> dict[str, Any]:
    """Create/update WINEPREFIX once (writes only below /workspace/work)."""

    timeout = validate_timeout(timeout_seconds)
    return {
        "sandbox_command": {
            "tool": "wine",
            "arguments": ["wineboot", "-u"],
            "timeout_seconds": timeout,
        },
        "request": {"operation": "wine_init"},
    }


@mcp.tool(title="Run PE With Wine")
def wine_run(
    executable: str,
    arguments: list[str] | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run one PE and capture its console output."""

    target = _target(executable)
    timeout = validate_timeout(timeout_seconds)
    values = arguments or []
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("arguments must be a list of at most 32 strings")
    for item in values:
        if not isinstance(item, str) or "\x00" in item or len(item.encode("utf-8")) > 2048:
            raise ValueError("argument is invalid or too long")
    return {
        "sandbox_command": {
            "tool": "wine",
            "arguments": [target, *values],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "wine_run",
            "executable": target,
            "staged": target != executable,
        },
    }


@mcp.tool(title="Debug PE With winedbg")
def wine_debug(
    executable: str,
    arguments: list[str] | None = None,
    name: str = "wine",
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Start winedbg in an interactive tmux session."""

    target = _target(executable)
    values = arguments or []
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("arguments must be a list of at most 32 strings")
    return session_start(
        name=name,
        program="wine",
        arguments=["winedbg", target, *values],
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(title="Send To Wine Session")
def wine_send(
    text: str | None = None,
    key: str | None = None,
    name: str = "wine",
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    return session_send(name=name, text=text, key=key, timeout_seconds=timeout_seconds)


@mcp.tool(title="Capture Wine Session")
def wine_capture(
    lines: int = 120,
    name: str = "wine",
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    return session_capture(name=name, lines=lines, timeout_seconds=timeout_seconds)


@mcp.tool(title="Stop Wine Session")
def wine_stop(name: str = "wine", timeout_seconds: int = 10) -> dict[str, Any]:
    return session_stop(name=name, timeout_seconds=timeout_seconds)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

