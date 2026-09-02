from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlparse

from nuclei_mcp.config import ALLOWED_SEVERITIES, MAX_TIMEOUT_SECONDS


_HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_TEMPLATE_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,240}$")
_TEMPLATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_TAG_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")


def _allowed_target_rules() -> tuple[str, ...]:
    raw = os.getenv(
        "MCP_NUCLEI_ALLOWED_TARGETS",
        "127.0.0.1,localhost,target-web",
    )
    rules = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
    if not rules:
        raise RuntimeError("MCP_NUCLEI_ALLOWED_TARGETS cannot be empty")
    return rules


def _host_allowed(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return any(
            normalized == rule.rstrip(".")
            or (rule.startswith("*.") and normalized.endswith(rule[1:]))
            for rule in _allowed_target_rules()
            if "/" not in rule
        )

    for rule in _allowed_target_rules():
        try:
            if address in ipaddress.ip_network(rule, strict=False):
                return True
        except ValueError:
            if normalized == rule:
                return True
    return False


def validate_target(target: str) -> str:
    if not isinstance(target, str):
        raise ValueError("target must be a string")
    candidate = target.strip()
    if not candidate or len(candidate) > 2048 or any(c in candidate for c in "\r\n\t"):
        raise ValueError("target is invalid")

    parsed = urlparse(candidate)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("target URL must not contain credentials")
        host = parsed.hostname
    else:
        host = candidate
        if not _HOST_PATTERN.fullmatch(host):
            try:
                ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError("target must be one URL, hostname or IP") from exc

    if not _host_allowed(host):
        raise PermissionError(f"target host {host!r} is not authorized")
    return candidate


def validate_template(template: str) -> str:
    candidate = template.strip().replace("\\", "/")
    if not candidate:
        return ""
    if candidate.startswith("/") or ".." in candidate.split("/"):
        raise ValueError("template must be a relative official template path")
    if not _TEMPLATE_PATTERN.fullmatch(candidate):
        raise ValueError("template contains unsupported characters")
    return candidate


def validate_template_id(template_id: str) -> str:
    candidate = template_id.strip()
    if not _TEMPLATE_ID_PATTERN.fullmatch(candidate):
        raise ValueError("template_id is invalid")
    return candidate


def validate_timeout(timeout_seconds: int) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 5 <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(f"timeout_seconds must be between 5 and {MAX_TIMEOUT_SECONDS}")
    return timeout_seconds


def validate_severity(severity: str) -> str:
    candidate = severity.strip().lower()
    if candidate and candidate not in ALLOWED_SEVERITIES:
        raise ValueError(f"invalid severity: {candidate}")
    return candidate


def validate_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags, list) or not 1 <= len(tags) <= 20:
        raise ValueError("tags must contain between 1 and 20 items")
    cleaned = []
    for tag in tags:
        candidate = tag.strip().lower()
        if not _TAG_PATTERN.fullmatch(candidate):
            raise ValueError(f"invalid tag: {tag!r}")
        cleaned.append(candidate)
    return list(dict.fromkeys(cleaned))


def validate_int_range(value: int, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value
