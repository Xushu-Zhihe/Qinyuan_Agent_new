from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-httpx-command-planner",
    instructions=(
        "Validate authorized HTTP probing requests and return sandbox_command "
        "objects. This server never executes httpx."
    ),
    json_response=True,
)


def _allowed_hosts() -> tuple[str, ...]:
    raw = os.getenv("MCP_HTTPX_ALLOWED_HOSTS", "127.0.0.1,localhost,target-web")
    rules = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    if not rules:
        raise RuntimeError("MCP_HTTPX_ALLOWED_HOSTS cannot be empty")
    return rules


def _host_allowed(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return any(
        normalized == rule.rstrip(".")
        or (rule.startswith("*.") and normalized.endswith(rule[1:]))
        for rule in _allowed_hosts()
    )


def _validate_target(target: str) -> str:
    candidate = target.strip()
    if not candidate or len(candidate) > 2048 or any(c in candidate for c in "\r\n\t"):
        raise ValueError("target must be a valid non-empty URL")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("target must be one http or https URL")
    if parsed.username or parsed.password:
        raise ValueError("target URL must not contain credentials")
    if not _host_allowed(parsed.hostname):
        raise PermissionError(f"target host {parsed.hostname!r} is not authorized")
    return candidate


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ValueError("timeout_seconds must be an integer between 5 and 300")
    return value


def _build_probe_arguments(
    targets: list[str],
    *,
    follow_redirects: bool,
    include_title: bool,
    include_technology: bool,
) -> list[str]:
    arguments = ["-silent", "-json", "-sc", "-cl"]
    if follow_redirects:
        arguments.append("-fr")
    if include_title:
        arguments.append("-title")
    if include_technology:
        arguments.append("-td")
    for target in targets:
        arguments.extend(["-u", target])
    return arguments


def _plan(targets: list[str], arguments: list[str], timeout_seconds: int) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "httpx",
            "arguments": arguments,
            "timeout_seconds": _validate_timeout(timeout_seconds),
        },
        "request": {"operation": "probe", "targets": targets, "format": "jsonl"},
    }


@mcp.tool(title="HTTPX Single Target Probe")
def run_httpx_probe(
    target: str,
    follow_redirects: bool = True,
    include_title: bool = True,
    include_technology: bool = True,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    normalized = _validate_target(target)
    arguments = _build_probe_arguments(
        [normalized],
        follow_redirects=follow_redirects,
        include_title=include_title,
        include_technology=include_technology,
    )
    return _plan([normalized], arguments, timeout_seconds)


@mcp.tool(title="HTTPX Batch Target Probe")
def run_httpx_batch(
    targets: list[str],
    follow_redirects: bool = True,
    include_title: bool = True,
    include_technology: bool = True,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
        raise ValueError("targets must contain between 1 and 50 URLs")
    normalized = list(dict.fromkeys(_validate_target(item) for item in targets))
    arguments = _build_probe_arguments(
        normalized,
        follow_redirects=follow_redirects,
        include_title=include_title,
        include_technology=include_technology,
    )
    return _plan(normalized, arguments, timeout_seconds)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
