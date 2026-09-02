from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from MCPs.fastmcp_compat import FastMCP


DEFAULT_ZAP_API_BASE = "http://zap:8080"

mcp = FastMCP(
    "qinyuan-zap-command-planner",
    instructions=(
        "Return one authorized curl command per ZAP API operation. "
        "The Agent orchestrates start, status polling and alert retrieval."
    ),
    json_response=True,
)


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower().rstrip("/") for item in os.getenv(name, default).split(",") if item.strip())


def _validate_url(value: str, field_name: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 2048 or any(c in candidate for c in "\r\n\t"):
        raise ValueError(f"{field_name} is invalid")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an http or https URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not contain credentials")
    return candidate


def _validate_api_base(value: str) -> str:
    candidate = _validate_url(value, "api_base").rstrip("/")
    if candidate.lower() not in _csv_env("MCP_ZAP_ALLOWED_API_BASES", DEFAULT_ZAP_API_BASE):
        raise PermissionError(f"ZAP API base {candidate!r} is not authorized")
    return candidate


def _validate_target(value: str) -> str:
    candidate = _validate_url(value, "target_url")
    host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    allowed = _csv_env("MCP_ZAP_ALLOWED_TARGETS", "127.0.0.1,localhost,target-web")
    if not any(
        host == rule.rstrip(".")
        or (rule.startswith("*.") and host.endswith(rule[1:]))
        for rule in allowed
    ):
        raise PermissionError(f"target host {host!r} is not authorized")
    return candidate


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 295:
        raise ValueError("timeout_seconds must be between 5 and 295")
    return value


def _validate_scan_id(value: str) -> str:
    candidate = value.strip()
    if not candidate.isdigit() or len(candidate) > 20:
        raise ValueError("scan_id must contain only decimal digits")
    return candidate


def _curl_plan(
    api_base: str,
    path: str,
    query: list[tuple[str, str]],
    timeout_seconds: int,
    request: dict[str, Any],
) -> dict[str, Any]:
    timeout = _validate_timeout(timeout_seconds)
    arguments = [
        "--silent", "--show-error", "--fail-with-body",
        "--max-time", str(timeout),
        "--header", "Accept: application/json",
        "--get", f"{_validate_api_base(api_base)}{path}",
    ]
    for name, value in query:
        arguments.extend(["--data-urlencode", f"{name}={value}"])
    return {
        "sandbox_command": {
            "tool": "curl",
            "arguments": arguments,
            "timeout_seconds": timeout + 5,
        },
        "request": request,
    }


@mcp.tool(title="OWASP ZAP API Status")
def get_zap_api_status(
    api_base: str = DEFAULT_ZAP_API_BASE,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    return _curl_plan(
        api_base,
        "/JSON/core/view/version/",
        [],
        timeout_seconds,
        {"operation": "zap-version"},
    )


@mcp.tool(title="Start OWASP ZAP Spider Scan")
def start_zap_spider_scan(
    target_url: str,
    api_base: str = DEFAULT_ZAP_API_BASE,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    target = _validate_target(target_url)
    return _curl_plan(
        api_base,
        "/JSON/spider/action/scan/",
        [("url", target), ("recurse", "true"), ("subtreeOnly", "true")],
        timeout_seconds,
        {"operation": "start-spider", "target_url": target},
    )


@mcp.tool(title="Get OWASP ZAP Spider Status")
def get_zap_spider_status(
    scan_id: str,
    api_base: str = DEFAULT_ZAP_API_BASE,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    normalized_id = _validate_scan_id(scan_id)
    return _curl_plan(
        api_base,
        "/JSON/spider/view/status/",
        [("scanId", normalized_id)],
        timeout_seconds,
        {"operation": "spider-status", "scan_id": normalized_id},
    )


@mcp.tool(title="Start OWASP ZAP Active Scan")
def start_zap_active_scan(
    target_url: str,
    api_base: str = DEFAULT_ZAP_API_BASE,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    target = _validate_target(target_url)
    return _curl_plan(
        api_base,
        "/JSON/ascan/action/scan/",
        [("url", target), ("recurse", "true"), ("inScopeOnly", "false")],
        timeout_seconds,
        {"operation": "start-active-scan", "target_url": target},
    )


@mcp.tool(title="Get OWASP ZAP Active Scan Status")
def get_zap_active_scan_status(
    scan_id: str,
    api_base: str = DEFAULT_ZAP_API_BASE,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    normalized_id = _validate_scan_id(scan_id)
    return _curl_plan(
        api_base,
        "/JSON/ascan/view/status/",
        [("scanId", normalized_id)],
        timeout_seconds,
        {"operation": "active-scan-status", "scan_id": normalized_id},
    )


@mcp.tool(title="Get OWASP ZAP Alerts")
def get_zap_alerts(
    target_url: str,
    max_alerts: int = 100,
    api_base: str = DEFAULT_ZAP_API_BASE,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    target = _validate_target(target_url)
    if isinstance(max_alerts, bool) or not isinstance(max_alerts, int) or not 1 <= max_alerts <= 500:
        raise ValueError("max_alerts must be between 1 and 500")
    return _curl_plan(
        api_base,
        "/JSON/core/view/alerts/",
        [("baseurl", target), ("start", "0"), ("count", str(max_alerts))],
        timeout_seconds,
        {"operation": "get-alerts", "target_url": target, "max_alerts": max_alerts},
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
