from __future__ import annotations

import hashlib
import shlex
from pathlib import PurePosixPath
from typing import Iterable


MAX_SCRIPT_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 16 * 1024
SANDBOX_INPUT_ROOT = "/workspace/input"
SANDBOX_WORK_ROOT = "/workspace/work"
SANDBOX_TEMP_ROOT = "/tmp"
SANDBOX_STAGE_ROOT = "/workspace/work/.stage"
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


def script_source(script: str | None, script_path: str | None) -> tuple[str, dict]:
    """Select inline code or a sandbox file; planners never read host files."""
    if (script is None) == (script_path is None):
        raise ValueError("tool_input_invalid: provide exactly one of script or script_path")
    if script_path is None:
        source = validate_script(script)
        return source, {"script_bytes": len(source.encode("utf-8"))}
    path = validate_sandbox_path(script_path, label="script_path")
    source = (
        "from pathlib import Path\nimport sys\n"
        f"_qy_file = {path!r}\n"
        "_qy_source = Path(_qy_file).read_bytes()\n"
        "if not _qy_source.strip() or len(_qy_source) > 65536 or b'\\x00' in _qy_source:\n"
        "    raise ValueError('tool_input_invalid: script file must be nonempty, NUL-free and at most 64 KiB')\n"
        "__file__ = _qy_file\nsys.argv = [_qy_file]\nsys.path.insert(0, str(Path(_qy_file).parent))\n"
        "exec(compile(_qy_source, _qy_file, 'exec'), globals())\n"
    )
    return source, {"script_path": path}


def json_input_source(path: str, variable: str) -> str:
    """Load JSON data during sandbox execution, never on the MCP host."""
    normalized = validate_sandbox_path(path, label="data_path")
    return (
        "import json\n"
        f"with open({normalized!r}, encoding='utf-8') as _qy_input:\n"
        f"    {variable} = json.load(_qy_input)\n"
    )


def needs_stage(path: str) -> bool:
    """True when an artifact lives on a noexec mount or lacks the exec bit.

    Imported attachments are written as ``0444`` below ``/workspace/input``,
    which is a ``noexec`` tmpfs. ``/tmp`` is ``noexec`` as well; only
    ``/workspace/work`` can execute. Anything outside the work tree is copied
    to a writable, executable staging path first.
    """

    normalized = PurePosixPath(path)
    work = PurePosixPath(SANDBOX_WORK_ROOT)
    return normalized != work and work not in normalized.parents


def staged_path(path: str) -> str:
    """Return a stable, executable path below ``/workspace/work/.stage``."""

    normalized = validate_sandbox_path(path)
    name = PurePosixPath(normalized).name
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError("path must name a file")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{SANDBOX_STAGE_ROOT}/{digest}-{name}"


def stage_shell_prefix(source: str, target: str) -> str:
    """Bash prefix that copies an artifact to an executable staging path."""

    return (
        "set -eu; "
        f"mkdir -p {shlex.quote(SANDBOX_STAGE_ROOT)}; "
        f"cp -f {shlex.quote(source)} {shlex.quote(target)}; "
        f"chmod 0755 {shlex.quote(target)}; "
    )


def stage_python_prefix(source: str, target: str) -> str:
    """Python prefix that copies an artifact and exports ``QINYUAN_BIN``."""

    return (
        "import os, shutil\n"
        f"_qy_src = {source!r}\n"
        f"_qy_dst = {target!r}\n"
        "os.makedirs(os.path.dirname(_qy_dst), exist_ok=True)\n"
        "shutil.copyfile(_qy_src, _qy_dst)\n"
        "os.chmod(_qy_dst, 0o755)\n"
        "os.environ['QINYUAN_BIN'] = _qy_dst\n"
    )
