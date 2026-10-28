from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable


MAX_SCRIPT_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 16 * 1024
SANDBOX_INPUT_ROOT = "/workspace/input"
SANDBOX_WORK_ROOT = "/workspace/work"
SANDBOX_TEMP_ROOT = "/tmp"
SANDBOX_PATH_HELP = "Paths are inside this TaskRun sandbox: /workspace/input is read-only; /workspace/work and /tmp are writable. Relative analysis-tool path arguments resolve below /workspace/work."


def validate_sandbox_path(value: str, *, label: str = "path", writable: bool = False) -> str:
    """Allow only paths visible inside the Agent sandbox."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError(f"{label} must be a non-empty path")
    if "\x00" in candidate or "\\" in candidate:
        raise ValueError(f"{label} contains an invalid character")
    if candidate.startswith("-"):
        raise ValueError(f"{label} must not start with '-'")

    path = PurePosixPath(candidate)
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain traversal")
    if not path.is_absolute():
        path = PurePosixPath(SANDBOX_WORK_ROOT) / path
    roots = (SANDBOX_WORK_ROOT, SANDBOX_TEMP_ROOT) if writable else (
        SANDBOX_INPUT_ROOT, SANDBOX_WORK_ROOT, SANDBOX_TEMP_ROOT)
    if not any(path == PurePosixPath(root) or PurePosixPath(root) in path.parents for root in roots):
        raise ValueError(f"{label} is outside the TaskRun sandbox paths")
    return str(path)


def validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ValueError("timeout_seconds must be an integer between 5 and 300")
    return value


def validate_script(value: str, *, label: str = "script") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain null bytes")
    if len(value.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ValueError(f"{label} must not exceed 64 KiB")
    return value


def validate_commands(values: Iterable[str]) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("commands must contain between 1 and 32 strings")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("commands must contain non-empty strings")
        if "\x00" in value or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("gdb command is invalid or too long")
        normalized.append(value.strip())
    return normalized


def python_command(script: str) -> list[str]:
    return ["-c", validate_script(script)]
