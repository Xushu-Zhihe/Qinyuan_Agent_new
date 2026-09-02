from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-burp-command-planner",
    instructions=(
        "Validate an authorized HTTP request and return a curl command plan "
        "for execution through Burp Proxy in the Agent sandbox."
    ),
    json_response=True,
)

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower().rstrip("/") for item in os.getenv(name, default).split(",") if item.strip())


def _host_allowed(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return any(
        normalized == rule.rstrip(".")
        or (rule.startswith("*.") and normalized.endswith(rule[1:]))
        for rule in _csv_env("MCP_BURP_ALLOWED_HOSTS", "127.0.0.1,localhost,target-web")
    )


def _validate_http_url(value: str, field_name: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 2048 or any(c in candidate for c in "\r\n\t"):
        raise ValueError(f"{field_name} is invalid")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an http or https URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not contain credentials")
    return candidate


def _validate_target_url(value: str) -> str:
    candidate = _validate_http_url(value, "target_url")
    host = urlparse(candidate).hostname or ""
    if not _host_allowed(host):
        raise PermissionError(f"target host {host!r} is not authorized")
    return candidate


def _validate_proxy_url(value: str) -> str:
    candidate = _validate_http_url(value, "proxy_url").rstrip("/")
    allowed = _csv_env("MCP_BURP_ALLOWED_PROXIES", "http://burp:8080")
    if candidate.lower() not in allowed:
        raise PermissionError(f"proxy URL {candidate!r} is not authorized")
    return candidate


def _validate_headers(headers: dict[str, str] | None) -> dict[str, str]:
    result = dict(headers or {})
    if len(result) > 30:
        raise ValueError("headers cannot contain more than 30 entries")
    for name, value in result.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("header names and values must be strings")
        if not name.strip() or ":" in name or any(c in name + value for c in "\r\n\x00"):
            raise ValueError(f"invalid HTTP header: {name!r}")
        if len(name) > 128 or len(value) > 4096:
            raise ValueError("HTTP header is too long")
    result.setdefault("User-Agent", "Qinyuan-Agent-Burp-MCP/0.2")
    return result


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 295:
        raise ValueError("timeout_seconds must be between 5 and 295")
    return value


@mcp.tool(title="Send Authorized HTTP Request Through Burp Proxy")
def send_http_request_through_burp(
    target_url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    proxy_url: str = "http://burp:8080",
    timeout_seconds: int = 30,
    verify_tls: bool = False,
    max_response_bytes: int = 200_000,
) -> dict[str, Any]:
    target = _validate_target_url(target_url)
    proxy = _validate_proxy_url(proxy_url)
    normalized_method = method.strip().upper()
    if normalized_method not in _METHODS:
        raise ValueError(f"method must be one of {sorted(_METHODS)}")
    if body is not None and len(body.encode("utf-8")) > 256_000:
        raise ValueError("body cannot exceed 256000 UTF-8 bytes")
    if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 1024 <= max_response_bytes <= 2_000_000:
        raise ValueError("max_response_bytes must be between 1024 and 2000000")
    timeout = _validate_timeout(timeout_seconds)

    arguments = [
        "--silent", "--show-error", "--include", "--fail-with-body",
        "--max-time", str(timeout),
        "--max-filesize", str(max_response_bytes),
        "--proxy", proxy,
        "--request", normalized_method,
    ]
    if not verify_tls:
        arguments.append("--insecure")
    for name, value in _validate_headers(headers).items():
        arguments.extend(["--header", f"{name}: {value}"])
    if body is not None:
        arguments.extend(["--data-binary", body])
    arguments.append(target)

    return {
        "sandbox_command": {
            "tool": "curl",
            "arguments": arguments,
            "timeout_seconds": timeout + 5,
        },
        "request": {
            "operation": "burp-proxy-request",
            "target_url": target,
            "method": normalized_method,
            "proxy_url": proxy,
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
