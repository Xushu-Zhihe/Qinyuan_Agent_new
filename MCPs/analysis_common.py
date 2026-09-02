from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable


MAX_SCRIPT_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 16 * 1024


def validate_sandbox_path(value: str, *, label: str = "path") -> str:
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
    if path.is_absolute() and not (
        candidate == "/challenge"
        or candidate.startswith("/challenge/")
        or candidate == "/work"
        or candidate.startswith("/work/")
    ):
        raise ValueError(f"{label} must be under /challenge or /work")
    return candidate


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
