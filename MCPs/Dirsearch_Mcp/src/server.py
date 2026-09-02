from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-dirsearch-command-planner",
    instructions=(
        "Validate authorized content-discovery requests and return "
        "sandbox_command objects. This server never executes dirsearch."
    ),
    json_response=True,
)

_WORDLISTS = {
    "common": "/opt/qinyuan/wordlists/common.txt",
    "directories": "/opt/qinyuan/wordlists/directories.txt",
    "api": "/opt/qinyuan/wordlists/api.txt",
    "admin": "/opt/qinyuan/wordlists/admin.txt",
}
_EXTENSION_PATTERN = re.compile(r"^[A-Za-z0-9]{1,12}$")


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.getenv("MCP_DIRSEARCH_ALLOWED_HOSTS", "127.0.0.1,localhost,target-web")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _validate_target(target: str) -> str:
    candidate = target.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target must be one http or https URL")
    if parsed.username or parsed.password or any(c in candidate for c in "\r\n\t"):
        raise ValueError("target URL is not allowed")
    host = parsed.hostname.lower().rstrip(".")
    if not any(
        host == rule.rstrip(".")
        or (rule.startswith("*.") and host.endswith(rule[1:]))
        for rule in _allowed_hosts()
    ):
        raise PermissionError(f"target host {host!r} is not authorized")
    return candidate


def _validate_wordlist(name: str) -> str:
    try:
        return _WORDLISTS[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"wordlist must be one of {sorted(_WORDLISTS)}") from exc


def _validate_extensions(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        item = value.strip().lstrip(".").lower()
        if not _EXTENSION_PATTERN.fullmatch(item):
            raise ValueError(f"invalid extension: {value!r}")
        result.append(item)
    if len(result) > 20:
        raise ValueError("extensions cannot contain more than 20 values")
    return list(dict.fromkeys(result))


def _validate_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _plan(
    target: str,
    *,
    wordlist: str,
    extensions: list[str],
    threads: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    arguments = [
        "-u", target,
        "-w", wordlist,
        "--threads", str(_validate_int(threads, "threads", 1, 50)),
        "--quiet-mode",
        "--no-color",
    ]
    if extensions:
        arguments.extend(["-e", ",".join(extensions)])
    return {
        "sandbox_command": {
            "tool": "dirsearch",
            "arguments": arguments,
            "timeout_seconds": _validate_int(timeout_seconds, "timeout_seconds", 5, 300),
        },
        "request": {"operation": "content-discovery", "target": target, "wordlist": wordlist},
    }


@mcp.tool(title="Dirsearch Version")
def dirsearch_check(timeout_seconds: int = 15) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "dirsearch",
            "arguments": ["--version"],
            "timeout_seconds": _validate_int(timeout_seconds, "timeout_seconds", 5, 60),
        },
        "request": {"operation": "version"},
    }


@mcp.tool(title="Dirsearch Configurable Scan")
def dirsearch_scan(
    target: str,
    wordlist: str = "directories",
    extensions: list[str] | None = None,
    threads: int = 20,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    return _plan(
        _validate_target(target),
        wordlist=_validate_wordlist(wordlist),
        extensions=_validate_extensions(extensions),
        threads=threads,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(title="Dirsearch Quick Scan")
def dirsearch_quick_scan(target: str, timeout_seconds: int = 90) -> dict[str, Any]:
    return _plan(
        _validate_target(target),
        wordlist=_WORDLISTS["common"],
        extensions=[],
        threads=15,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(title="Dirsearch API Scan")
def dirsearch_api_scan(target: str, timeout_seconds: int = 150) -> dict[str, Any]:
    return _plan(
        _validate_target(target),
        wordlist=_WORDLISTS["api"],
        extensions=["json", "xml", "yaml", "yml"],
        threads=20,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool(title="Dirsearch Admin Path Scan")
def dirsearch_admin_scan(target: str, timeout_seconds: int = 150) -> dict[str, Any]:
    return _plan(
        _validate_target(target),
        wordlist=_WORDLISTS["admin"],
        extensions=["php", "asp", "aspx", "jsp", "html"],
        threads=20,
        timeout_seconds=timeout_seconds,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
