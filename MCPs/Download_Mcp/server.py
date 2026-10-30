from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from MCPs.analysis_common import MAX_ARGUMENT_BYTES, validate_timeout
from MCPs.fastmcp_compat import FastMCP


_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,63}")
_MODULE_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_ARCHIVE_FORMATS = frozenset({"raw", "zip", "tar", "tar.gz", "tar.xz"})
_RESERVED_NAMES = frozenset({
    "sh", "bash", "dash", "python", "python3", "pip", "pip3", "timeout",
    "sleep", "curl", "wget", "nmap", "gdb", "file", "readelf", "objdump",
    "nm", "strings", "php", "pwsh", "powershell", "tar", "unzip", "zip",
    "subfinder", "amass", "httpx", "ffuf", "dirsearch", "nuclei", "jadx",
})
_DEFAULT_BOOTSTRAP_TIMEOUT = 300


mcp = FastMCP(
    "qinyuan-download-command-planner",
    instructions=(
        "Prepare bounded tool installation commands for the current Agent "
        "sandbox. The current TaskRun prepares verified artifacts through its controlled fetcher, "
        "then installs locally under /tools without direct Internet access; "
        "when native installation fails, a pinned Python package fallback may "
        "be attempted. This server never downloads to or executes on the host. "
        "Only use sources authorized by the challenge and provide a SHA256 for "
        "native artifacts."
    ),
    json_response=True,
)


def _configured_timeout() -> int:
    raw = os.environ.get(
        "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS",
        str(_DEFAULT_BOOTSTRAP_TIMEOUT),
    )
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if not 5 <= value <= 300:
        raise ValueError(
            "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS must be between 5 and 300"
        )
    return value


def _effective_timeout(value: int | None) -> int:
    configured = _configured_timeout()
    if value is None:
        return configured
    requested = validate_timeout(value)
    return min(requested, configured)


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or _NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe non-empty name")
    if value.lower() in _RESERVED_NAMES:
        raise ValueError(f"{label} must not shadow a platform executable")
    return value


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("url must be a non-empty URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("url must use HTTPS")
    return value


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return value.lower()


def _validate_archive_format(value: str) -> str:
    if value not in _ARCHIVE_FORMATS:
        raise ValueError(f"archive_format must be one of {sorted(_ARCHIVE_FORMATS)}")
    return value


def _validate_args(value: list[str], label: str = "self_test_args") -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError(f"{label} must contain at most 8 strings")
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError(f"{label} must contain non-empty strings")
        if len(item.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError(f"{label} contains an argument that is too long")
    return value


def _validate_optional_package(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _PACKAGE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe package name")
    return value


def _validate_optional_version(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("python_version must be a pinned package version")
    return value


def _validate_module(value: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if _MODULE_PATTERN.fullmatch(value) is None:
        raise ValueError("python_module must be a dotted Python module name")
    return value


def _script(**values: object) -> str:
    from MCPs.Download_Mcp.install_script import render
    return render(values)


def _bootstrap_plan(script: str, timeout_seconds: int) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": ["-c", script],
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "tool_bootstrap"},
    }


@mcp.tool(title="Install Native Tool With Python Fallback")
def install_tool(
    name: str,
    url: str | None = None,
    sha256: str | None = None,
    archive_format: str = "raw",
    executable_path: str = "",
    self_test_args: list[str] | None = None,
    python_package: str | None = None,
    python_version: str | None = None,
    python_module: str = "",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Install a verified native artifact, then a pinned Python fallback.

    Supply either url+sha256 (native) or python_package+python_version
    (fallback); python_package alone is rejected. archive_format accepts raw,
    zip, tar, tar.gz or tar.xz.
    """

    normalized_name = _validate_name(name, "name")
    normalized_url = _validate_url(url) if url is not None else None
    normalized_sha = _validate_sha256(sha256) if sha256 is not None else None
    if normalized_url is not None and normalized_sha is None:
        raise ValueError("sha256 is required when url is provided")
    if normalized_url is None and normalized_sha is not None:
        raise ValueError("url is required when sha256 is provided")
    normalized_format = _validate_archive_format(archive_format)
    if normalized_format != "raw" and not executable_path:
        raise ValueError("executable_path is required for archives")
    if "\x00" in executable_path or len(executable_path.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("executable_path is invalid or too long")
    normalized_python_package = _validate_optional_package(
        python_package, "python_package"
    )
    normalized_python_version = _validate_optional_version(python_version)
    normalized_python_module = _validate_module(python_module)
    if (normalized_python_package is None) != (normalized_python_version is None):
        raise ValueError("python_package and python_version must be provided together")
    if not normalized_url and not normalized_python_package:
        raise ValueError("native URL or Python fallback must be provided")
    normalized_args = _validate_args([] if self_test_args is None else self_test_args)
    timeout = _effective_timeout(timeout_seconds)
    return _bootstrap_plan(
        _script(
            name=normalized_name,
            url=normalized_url or "",
            sha256=normalized_sha or "",
            archive_format=normalized_format,
            executable_path=executable_path,
            self_test_args=normalized_args,
            python_package=normalized_python_package or "",
            python_version=normalized_python_version or "",
            python_module=normalized_python_module,
            timeout_seconds=timeout,
        ),
        timeout,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
