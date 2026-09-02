from __future__ import annotations

from typing import Any

from nuclei_mcp.config import DEFAULT_TEMPLATES_DIR


def command_plan(
    arguments: list[str],
    timeout_seconds: int,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Return a Nuclei command plan without touching host processes or files."""
    return {
        "sandbox_command": {
            "tool": "nuclei",
            "arguments": arguments,
            "timeout_seconds": timeout_seconds,
        },
        "request": request,
    }


def base_scan_arguments(target: str) -> list[str]:
    return [
        "-u", target,
        "-t", DEFAULT_TEMPLATES_DIR,
        "-silent",
        "-jsonl",
        "-duc",
        "-no-interactsh",
        "-rl", "50",
    ]
