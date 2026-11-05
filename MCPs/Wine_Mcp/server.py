from __future__ import annotations

from typing import Any

from MCPs.analysis_common import validate_sandbox_path, validate_timeout
from MCPs.fastmcp_compat import FastMCP
from MCPs.Session_Mcp.server import session_capture, session_send, session_stop, session_start


mcp = FastMCP(
    "qinyuan-wine-command-planner",
    instructions=(
        "Wine plans for the dedicated Windows profile (user-mode PE only; no "
        "kernel drivers, .NET Framework or anti-cheat). Workflow: "
        "1) wine_init before the first run; wait for completion and initialize only the needed "
        "architecture (both prefixes together take ~1.9GB, 1-2 minutes). "
        "2) wine_run for one-shot execution; arch='auto' reads the PE header. "
        "3) wine_debug for breakpoints: it starts `winedbg --gdb Z:\\...`, so "
        "the tmux pane is a GNU gdb prompt; arch must be '32' or '64' "
        "(default 64; check with file or Pe_Mcp.inspect_pe). "
        "4) Drive the session with wine_send/wine_capture; set breakpoints "
        "before 'c'; wine_stop when done. PEs may live in /workspace/input "
        "(read-only) or /workspace/work; Wine maps them through Z:, so no "
        "staging copy is needed. This server never executes Wine itself."
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
    """Call once per TaskRun before wine_run/wine_debug.

    Creates the 64-bit `.wine64` and 32-bit `.wine32` prefixes under
    /workspace/work (about 1.9GB, 1-2 minutes). `arch='both'` by default; use
    '32' or '64' only when you need a single prefix. Wait for completion before
    running. The wrappers prepare /workspace/work prefixes and TMPDIR; reuse
    successful initialization. A wineserver tmpdir error is a setup failure,
    not proof that a PE cannot execute.
    """

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
    """Run one PE and return its console output/exit code.

    `arch='auto'` picks the loader from the PE header; use '32'/'64' to force.
    Use this for non-interactive/crackme runs; for breakpoints or stepping use
    wine_debug instead.
    """

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


@mcp.tool(title="Debug PE With winedbg --gdb")
def wine_debug(
    executable: str,
    arguments: list[str] | None = None,
    arch: str = "64",
    name: str = "wine",
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Start an interactive GNU gdb session for a PE via `winedbg --gdb`.

    The tmux pane is a gdb prompt: send `b main` or `b *0x...`, press Enter,
    then `c`; read output with wine_capture. `arch` must be '32' or '64'
    (default 64; check with file or Pe_Mcp.inspect_pe). The session survives
    across tool calls; call wine_stop when done.
    """

    target = _target(executable)
    if arch not in {"32", "64"}:
        raise ValueError("arch must be 32 or 64 (check the PE with file/Pe_Mcp)")
    wrapper = _WRAPPERS[arch]
    values = _arguments(arguments)
    windows_target = "Z:" + target.replace("/", "\\")
    return session_start(
        name=name,
        program=wrapper,
        arguments=["winedbg", "--gdb", windows_target, *values],
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(title="Send To Wine Session")
def wine_send(
    text: str | None = None,
    key: str | None = None,
    name: str = "wine",
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Send input to the `wine` session: text or one named key.

    Use `text` for a gdb command and then `key='Enter'`; use `key` alone for
    Enter, C-c, Up/Down, etc. Provide exactly one of text or key.
    """

    return session_send(name=name, text=text, key=key, timeout_seconds=timeout_seconds)


@mcp.tool(title="Capture Wine Session")
def wine_capture(
    lines: int = 120,
    name: str = "wine",
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Read the last `lines` (10-400) of the `wine` session pane.

    Call after wine_send to see the gdb prompt/output or program messages.
    """

    return session_capture(name=name, lines=lines, timeout_seconds=timeout_seconds)


@mcp.tool(title="Stop Wine Session")
def wine_stop(name: str = "wine", timeout_seconds: int = 10) -> dict[str, Any]:
    """Stop the `wine` session; Wine prefixes remain for the next run."""

    return session_stop(name=name, timeout_seconds=timeout_seconds)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
