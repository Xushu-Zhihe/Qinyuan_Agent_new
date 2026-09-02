from __future__ import annotations

import os
import re
from typing import Any

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-amass-command-planner",
    instructions=(
        "Validate authorized Amass requests and return sandbox_command "
        "objects. This server never executes Amass."
    ),
    json_response=True,
)

_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def _allowed_domain_rules() -> tuple[str, ...]:
    raw = os.getenv("MCP_AMASS_ALLOWED_DOMAINS", "localhost,target-web")
    rules = tuple(item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip())
    if not rules:
        raise RuntimeError("MCP_AMASS_ALLOWED_DOMAINS cannot be empty")
    return rules


def _validate_domain(domain: str) -> str:
    candidate = domain.strip().lower().rstrip(".")
    if not candidate or candidate.startswith("-"):
        raise ValueError("domain must be a non-empty hostname")
    if candidate != "localhost" and not _DOMAIN_PATTERN.fullmatch(candidate):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", candidate):
            raise ValueError("domain must be one hostname or root domain")
    if not any(
        candidate == rule
        or (rule.startswith("*.") and candidate.endswith(rule[1:]))
        for rule in _allowed_domain_rules()
    ):
        raise PermissionError(f"domain {candidate!r} is not authorized")
    return candidate


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 300:
        raise ValueError("timeout_seconds must be an integer between 5 and 300")
    return value


def _plan(arguments: list[str], timeout_seconds: int, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "amass",
            "arguments": arguments,
            "timeout_seconds": _validate_timeout(timeout_seconds),
        },
        "request": request,
    }


@mcp.tool(title="Amass Version")
def check_amass(timeout_seconds: int = 15) -> dict[str, Any]:
    return _plan(["-version"], timeout_seconds, {"operation": "version"})


@mcp.tool(title="Amass Passive Domain Scan")
def run_amass_passive_scan(
    domain: str,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    normalized = _validate_domain(domain)
    return _plan(
        ["enum", "-passive", "-d", normalized],
        timeout_seconds,
        {"operation": "enum", "mode": "passive", "domains": [normalized]},
    )


@mcp.tool(title="Amass Active Domain Scan")
def run_amass_scan(
    domain: str,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    normalized = _validate_domain(domain)
    return _plan(
        ["enum", "-active", "-d", normalized],
        timeout_seconds,
        {"operation": "enum", "mode": "active", "domains": [normalized]},
    )


@mcp.tool(title="Amass Batch Passive Scan")
def run_amass_batch_scan(
    domains: list[str],
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    if not isinstance(domains, list) or not 1 <= len(domains) <= 25:
        raise ValueError("domains must contain between 1 and 25 items")
    normalized = list(dict.fromkeys(_validate_domain(item) for item in domains))
    arguments = ["enum", "-passive"]
    for domain in normalized:
        arguments.extend(["-d", domain])
    return _plan(
        arguments,
        timeout_seconds,
        {"operation": "enum", "mode": "passive", "domains": normalized},
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
