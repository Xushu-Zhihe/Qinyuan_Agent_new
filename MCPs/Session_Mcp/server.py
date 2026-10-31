from __future__ import annotations

import re
from typing import Any

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-interactive-session-planner",
    instructions=(
        "Prepare tmux-backed interactive sessions inside the current Agent "
        "sandbox. Start a program once, then send keystrokes and capture the "
        "pane across later tool calls. Use short session names (a-z0-9_-). "
        "Programs are restricted to debugging and analysis tools; this server "
        "never executes commands itself."
    ),
    json_response=True,
)

SESSION_PREFIX = "qy-"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CWD_ROOT = "/workspace/work"

# The model may pass a short alias; plans always use the absolute path.
_PROGRAMS = {
    "gdb": "/usr/bin/gdb",
    "gdb-multiarch": "/usr/bin/gdb-multiarch",
    "qemu-system-x86_64": "/usr/bin/qemu-system-x86_64",
    "python3": "/usr/bin/python3",
    "bash": "/bin/bash",
    "socat": "/usr/bin/socat",
    "nc": "/usr/bin/nc",
    "wine": "/usr/bin/wine",
    "winedbg": "/usr/bin/winedbg",
}

_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "Space",
        "BSpace",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "C-c",
        "C-d",
        "C-z",
        "C-l",
        "C-a",
    }
)


def _session_name(value: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError("name must match [a-z][a-z0-9_-]{0,31}")
    return SESSION_PREFIX + value


def _target(name: str) -> str:
    return _session_name(name)


def _program(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("program must be a string")
    candidate = _PROGRAMS.get(value.strip(), value.strip())
    if candidate not in _PROGRAMS.values():
        raise ValueError(f"program must be one of {sorted(_PROGRAMS)}")
    return candidate


def _arguments(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("arguments must be a list of at most 32 strings")
    for item in values:
        if not isinstance(item, str) or "\x00" in item or len(item.encode("utf-8")) > 2048:
            raise ValueError("argument is invalid or too long")
    return values


def _cwd(value: str) -> str:
    if not isinstance(value, str) or ".." in value or "\x00" in value:
        raise ValueError("cwd must stay below /workspace/work")
    if value == _CWD_ROOT or value.startswith(_CWD_ROOT + "/"):
        return value
    raise ValueError("cwd must stay below /workspace/work")


@mcp.tool(title="Start Interactive Session")
def session_start(
    name: str,
    program: str,
    arguments: list[str] | None = None,
    cwd: str = _CWD_ROOT,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Start a detached tmux session running an allowed program."""

    target = _target(name)
    executable = _program(program)
    argv = _arguments(arguments)
    workdir = _cwd(cwd)
    if not 5 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be between 5 and 60")
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": [
                "new-session",
                "-d",
                "-s",
                target,
                "-c",
                workdir,
                "--",
                executable,
                *argv,
            ],
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "session_start", "session": target, "program": executable},
    }


@mcp.tool(title="Send Text Or Key To Session")
def session_send(
    name: str,
    text: str | None = None,
    key: str | None = None,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Send literal text (no newline) or one named key to a session."""

    target = _target(name)
    if (text is None) == (key is None):
        raise ValueError("provide exactly one of text or key")
    if not 5 <= timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be between 5 and 30")
    if text is not None:
        if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > 4096:
            raise ValueError("text is invalid or longer than 4096 bytes")
        arguments = ["send-keys", "-t", target, "-l", "--", text]
        request = {"operation": "session_send", "session": target, "kind": "text"}
    else:
        if key not in _KEYS:
            raise ValueError(f"key must be one of {sorted(_KEYS)}")
        arguments = ["send-keys", "-t", target, key]
        request = {"operation": "session_send", "session": target, "kind": "key", "key": key}
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": arguments,
            "timeout_seconds": timeout_seconds,
        },
        "request": request,
    }


@mcp.tool(title="Capture Session Pane")
def session_capture(
    name: str,
    lines: int = 80,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Capture the last N lines of a session pane (10..400)."""

    target = _target(name)
    if isinstance(lines, bool) or not isinstance(lines, int) or not 10 <= lines <= 400:
        raise ValueError("lines must be an integer between 10 and 400")
    if not 5 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be between 5 and 60")
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": ["capture-pane", "-p", "-t", target, "-S", f"-{lines}"],
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "session_capture", "session": target, "lines": lines},
    }


@mcp.tool(title="List Interactive Sessions")
def session_list(timeout_seconds: int = 10) -> dict[str, Any]:
    """List session names and creation times inside this TaskRun."""

    if not 5 <= timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be between 5 and 30")
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": [
                "list-sessions",
                "-F",
                "#{session_name} #{session_created} #{session_windows}",
            ],
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "session_list"},
    }


@mcp.tool(title="Stop Interactive Session")
def session_stop(name: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Kill one session; other sessions and the container are unaffected."""

    target = _target(name)
    if not 5 <= timeout_seconds <= 30:
        raise ValueError("timeout_seconds must be between 5 and 30")
    return {
        "sandbox_command": {
            "tool": "tmux",
            "arguments": ["kill-session", "-t", target],
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "session_stop", "session": target},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

