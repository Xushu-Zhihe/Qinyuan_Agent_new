from __future__ import annotations

import os
import re
from typing import Any

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-subfinder-command-planner",
    instructions=(
        "Validate authorized subdomain-enumeration requests and return "
        "sandbox_command objects. This server never executes Subfinder."
    ),
    json_response=True,
)

_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def _allowed_domain_rules() -> tuple[str, ...]:
    raw = os.getenv(
        "MCP_SUBFINDER_ALLOWED_DOMAINS",
        "localhost,target-web",
    )
    rules = tuple(item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip())
    if not rules:
        raise RuntimeError("MCP_SUBFINDER_ALLOWED_DOMAINS cannot be empty")
    return rules


def _validate_domain(domain: str) -> str:
    candidate = domain.strip().lower().rstrip(".")
    if not candidate or candidate.startswith("-"):
        raise ValueError("domain must be a non-empty DNS name")

    # Docker Compose service names such as target-web have no dot.
    if candidate != "localhost" and not _DOMAIN_PATTERN.fullmatch(candidate):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", candidate):
            raise ValueError("domain must be one hostname or root domain")

    allowed = any(
        candidate == rule
        or (rule.startswith("*.") and candidate.endswith(rule[1:]))
        for rule in _allowed_domain_rules()
    )
    if not allowed:
        raise PermissionError(f"domain {candidate!r} is not authorized")
    return candidate


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ValueError("timeout_seconds must be an integer between 5 and 300")
    return value


def _plan(arguments: list[str], timeout_seconds: int, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "subfinder",
            "arguments": arguments,
            "timeout_seconds": _validate_timeout(timeout_seconds),
        },
        "request": request,
    }


@mcp.tool(title="Subfinder Version")
def check_subfinder(timeout_seconds: int = 15) -> dict[str, Any]:
    """Prepare a version check inside the current Agent sandbox."""
    return _plan(["-version"], timeout_seconds, {"operation": "version"})


@mcp.tool(title="Subfinder Domain Scan")
def run_subfinder_scan(
    domain: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    normalized = _validate_domain(domain)
    return _plan(
        ["-d", normalized, "-silent"],
        timeout_seconds,
        {"operation": "enumerate", "domains": [normalized], "format": "text"},
    )


@mcp.tool(title="Subfinder JSON Domain Scan")
def run_subfinder_json_scan(
    domain: str,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    normalized = _validate_domain(domain)
    return _plan(
        ["-d", normalized, "-silent", "-oJ"],
        timeout_seconds,
        {"operation": "enumerate", "domains": [normalized], "format": "jsonl"},
    )


@mcp.tool(title="Subfinder Batch Domain Scan")
def run_subfinder_batch_scan(
    domains: list[str],
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    if not isinstance(domains, list) or not 1 <= len(domains) <= 50:
        raise ValueError("domains must contain between 1 and 50 items")

    normalized = list(dict.fromkeys(_validate_domain(item) for item in domains))
    arguments: list[str] = ["-silent", "-oJ"]
    for domain in normalized:
        arguments.extend(["-d", domain])

    return _plan(
        arguments,
        timeout_seconds,
        {"operation": "enumerate", "domains": normalized, "format": "jsonl"},
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
