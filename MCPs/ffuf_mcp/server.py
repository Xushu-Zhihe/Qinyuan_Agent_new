from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-ffuf-command-planner",
    instructions=(
        "Validate authorized web-fuzzing requests and return sandbox_command "
        "objects. This server never executes ffuf."
    ),
    json_response=True,
)

_WORDLISTS = {
    "common": "/opt/qinyuan/wordlists/common.txt",
    "directories": "/opt/qinyuan/wordlists/directories.txt",
    "api": "/opt/qinyuan/wordlists/api.txt",
}
_EXTENSION_PATTERN = re.compile(r"^[A-Za-z0-9]{1,12}$")


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.getenv("MCP_FFUF_ALLOWED_HOSTS", "127.0.0.1,localhost,target-web")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _validate_target(target: str) -> str:
    candidate = target.strip()
    if "FUZZ" not in candidate:
        raise ValueError("target must contain the FUZZ keyword")
    if len(candidate) > 2048 or any(c in candidate for c in "\r\n\t"):
        raise ValueError("target is invalid")
    parsed = urlparse(candidate.replace("FUZZ", "qinyuan-placeholder"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target must be an http or https URL")
    host = parsed.hostname.lower().rstrip(".")
    allowed = any(
        host == rule.rstrip(".")
        or (rule.startswith("*.") and host.endswith(rule[1:]))
        for rule in _allowed_hosts()
    )
    if not allowed:
        raise PermissionError(f"target host {host!r} is not authorized")
    return candidate


def _validate_wordlist(name: str) -> str:
    try:
        return _WORDLISTS[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"wordlist must be one of {sorted(_WORDLISTS)}") from exc


def _validate_extensions(values: list[str] | None) -> list[str]:
    extensions = values or []
    if len(extensions) > 20:
        raise ValueError("extensions cannot contain more than 20 values")
    normalized = []
    for value in extensions:
        item = value.strip().lstrip(".").lower()
        if not _EXTENSION_PATTERN.fullmatch(item):
            raise ValueError(f"invalid extension: {value!r}")
        normalized.append(item)
    return list(dict.fromkeys(normalized))


def _validate_rate(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ValueError("rate must be an integer between 1 and 500")
    return value


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ValueError("timeout_seconds must be an integer between 5 and 300")
    return value


def _plan(
    target: str,
    wordlist: str,
    *,
    method: str,
    extensions: list[str],
    rate: int,
    timeout_seconds: int,
    match_codes: str,
) -> dict[str, Any]:
    arguments = [
        "-u", target,
        "-w", wordlist,
        "-X", method,
        "-rate", str(_validate_rate(rate)),
        "-mc", match_codes,
        "-s",
        "-json",
    ]
    if extensions:
        arguments.extend(["-e", ",".join(f".{item}" for item in extensions)])
    return {
        "sandbox_command": {
            "tool": "ffuf",
            "arguments": arguments,
            "timeout_seconds": _validate_timeout(timeout_seconds),
        },
        "request": {
            "operation": "fuzz",
            "target": target,
            "method": method,
            "wordlist": wordlist,
        },
    }


@mcp.tool(title="FFUF Directory Discovery")
def discover_directories(
    target: str,
    wordlist: str = "directories",
    extensions: list[str] | None = None,
    rate: int = 100,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    return _plan(
        _validate_target(target),
        _validate_wordlist(wordlist),
        method="GET",
        extensions=_validate_extensions(extensions),
        rate=rate,
        timeout_seconds=timeout_seconds,
        match_codes="200,204,301,302,307,308,401,403,500",
    )


@mcp.tool(title="FFUF API Endpoint Discovery")
def discover_api_endpoints(
    target: str,
    method: str = "GET",
    wordlist: str = "api",
    rate: int = 100,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    normalized_method = method.strip().upper()
    if normalized_method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
        raise ValueError("method is not allowed")
    return _plan(
        _validate_target(target),
        _validate_wordlist(wordlist),
        method=normalized_method,
        extensions=[],
        rate=rate,
        timeout_seconds=timeout_seconds,
        match_codes="200,201,204,301,302,307,308,401,403,405,500",
    )


@mcp.tool(title="FFUF Hidden Path Discovery")
def discover_hidden_paths(
    target: str,
    wordlist: str = "common",
    extensions: list[str] | None = None,
    rate: int = 75,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    default_extensions = ["bak", "old", "backup", "swp", "tmp", "log", "conf"]
    return _plan(
        _validate_target(target),
        _validate_wordlist(wordlist),
        method="GET",
        extensions=_validate_extensions(extensions or default_extensions),
        rate=rate,
        timeout_seconds=timeout_seconds,
        match_codes="200,204,301,302,307,308,401,403,500",
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
