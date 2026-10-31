from __future__ import annotations

from typing import Any

from MCPs.analysis_common import validate_sandbox_path, validate_timeout
from MCPs.fastmcp_compat import FastMCP
from MCPs.Session_Mcp.server import session_capture, session_send, session_stop, session_start


mcp = FastMCP(
    "qinyuan-wine-command-planner",
    instructions=(
        "Prepare Wine plans for the dedicated Windows profile. `wine_init` "
        "creates both the 64-bit (.wine64) and 32-bit (.wine32) prefixes; "
        "`wine_run` auto-selects the loader from the PE header unless `arch` is "
        "32/64; `wine_debug` starts winedbg in a tmux session so breakpoints "
        "survive across tool calls. This server never executes Wine itself."
    ),
    json_response=True,
)

# Debian's /usr/bin/wine always prefers wine32 when it exists, so plans use the
# deterministic wrappers installed by the Windows image.
_WRAPPERS = {"auto": "qy-wine-auto", "32": "qy-wine32", "64": "qy-wine64"}


def _target(path: str) -> str:
    # Wine maps the PE itself; the 0444/noexec input mount is readable, so no
    # executable staging copy is required here.
    return validate_sandbox_path(path)


def _arch(value: str) -> str:
    if value not in _WRAPPERS:
        raise ValueError("arch must be one of auto, 32, 64")
    return value


def _arguments(values: list[str] | None) -> list[str]:
    result = values or []
    if not isinstance(result, list) or len(result) > 32:
        raise ValueError("arguments must be a list of at most 32 strings")
    for item in result:
        if not isinstance(item, str) or "\x00" in item or len(item.encode("utf-8")) > 2048:
            raise ValueError("argument is invalid or too long")
    return result


@mcp.tool(title="Initialize Wine Prefixes")
def wine_init(timeout_seconds: int = 300, arch: str = "both") -> dict[str, Any]:
    """Create/update the 64-bit and 32-bit prefixes below /workspace/work."""

    timeout = validate_timeout(timeout_seconds)
    if arch not in {"both", "32", "64"}:
        raise ValueError("arch must be both, 32 or 64")
    if arch == "both":
        script = "set -eu; qy-wine64 wineboot -u; qy-wine32 wineboot -u"
        return {
            "sandbox_command": {
                "tool": "bash",
                "arguments": ["--noprofile", "--norc", "-lc", script],
                "timeout_seconds": timeout,
            },
            "request": {"operation": "wine_init", "arch": "both"},
        }
    wrapper = _WRAPPERS[arch]
    return {
        "sandbox_command": {
            "tool": wrapper,
            "arguments": ["wineboot", "-u"],
            "timeout_seconds": timeout,
        },
        "request": {"operation": "wine_init", "arch": arch},
    }


@mcp.tool(title="Run PE With Wine")
def wine_run(
    executable: str,
    arguments: list[str] | None = None,
    arch: str = "auto",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run one PE and capture its console output."""

    target = _target(executable)
    wrapper = _WRAPPERS[_arch(arch)]
    timeout = validate_timeout(timeout_seconds)
    values = _arguments(arguments)
    return {
        "sandbox_command": {
            "tool": wrapper,
            "arguments": [target, *values],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "wine_run",
            "executable": target,
            "arch": arch,
        },
    }


@mcp.tool(title="Debug PE With winedbg")
def wine_debug(
    executable: str,
    arguments: list[str] | None = None,
    arch: str = "auto",
    name: str = "wine",
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Start winedbg in an interactive tmux session."""

    target = _target(executable)
    wrapper = _WRAPPERS[_arch(arch)]
    values = _arguments(arguments)
    return session_start(
        name=name,
        program=wrapper,
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
