from __future__ import annotations

from typing import Any

from nuclei_mcp.config import DEFAULT_TEMPLATES_DIR, DEFAULT_TIMEOUT_SECONDS
from nuclei_mcp.runner import base_scan_arguments, command_plan
from nuclei_mcp.security import (
    validate_int_range,
    validate_severity,
    validate_tags,
    validate_target,
    validate_template,
    validate_template_id,
    validate_timeout,
)


def register_tools(mcp: Any) -> None:
    @mcp.tool(title="Nuclei Version")
    def check_nuclei_installation(timeout_seconds: int = 15) -> dict[str, Any]:
        return command_plan(
            ["-version"],
            validate_timeout(timeout_seconds),
            {"operation": "version"},
        )

    @mcp.tool(title="Nuclei Authorized Scan")
    def run_scan(
        target: str,
        template: str = "",
        severity: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        normalized_target = validate_target(target)
        normalized_template = validate_template(template)
        normalized_severity = validate_severity(severity)
        arguments = base_scan_arguments(normalized_target)
        if normalized_template:
            arguments[3] = f"{DEFAULT_TEMPLATES_DIR}/{normalized_template}"
        if normalized_severity:
            arguments.extend(["-s", normalized_severity])
        return command_plan(
            arguments,
            validate_timeout(timeout_seconds),
            {"operation": "scan", "target": normalized_target},
        )

    @mcp.tool(title="Nuclei Validate Official Template")
    def validate_template(
        template: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        normalized = globals()["validate_template"](template)
        if not normalized:
            raise ValueError("template cannot be empty")
        path = f"{DEFAULT_TEMPLATES_DIR}/{normalized}"
        return command_plan(
            ["-validate", "-t", path, "-duc"],
            validate_timeout(timeout_seconds),
            {"operation": "validate-template", "template": normalized},
        )

    @mcp.tool(title="Nuclei List Templates")
    def list_templates(
        tags: list[str] | None = None,
        severity: str = "",
        limit: int = 50,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        arguments = ["-tl", "-t", DEFAULT_TEMPLATES_DIR, "-duc"]
        if tags:
            arguments.extend(["-tags", ",".join(validate_tags(tags))])
        normalized_severity = validate_severity(severity)
        if normalized_severity:
            arguments.extend(["-s", normalized_severity])
        return command_plan(
            arguments,
            validate_timeout(timeout_seconds),
            {"operation": "list-templates", "result_limit_hint": validate_int_range(limit, "limit", 1, 200)},
        )

    @mcp.tool(title="Nuclei Display Template")
    def read_template_info(
        template: str,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        normalized = globals()["validate_template"](template)
        if not normalized:
            raise ValueError("template cannot be empty")
        return command_plan(
            ["-td", "-t", f"{DEFAULT_TEMPLATES_DIR}/{normalized}", "-duc"],
            validate_timeout(timeout_seconds),
            {"operation": "display-template", "template": normalized},
        )

    @mcp.tool(title="Nuclei Scan By Tags")
    def run_scan_by_tags(
        target: str,
        tags: list[str],
        severity: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        normalized_target = validate_target(target)
        normalized_tags = validate_tags(tags)
        arguments = base_scan_arguments(normalized_target)
        arguments.extend(["-tags", ",".join(normalized_tags)])
        normalized_severity = validate_severity(severity)
        if normalized_severity:
            arguments.extend(["-s", normalized_severity])
        return command_plan(
            arguments,
            validate_timeout(timeout_seconds),
            {"operation": "scan-by-tags", "target": normalized_target, "tags": normalized_tags},
        )

    @mcp.tool(title="Nuclei Find Template By ID")
    def find_template_by_id(
        template_id: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        normalized = validate_template_id(template_id)
        return command_plan(
            ["-tl", "-t", DEFAULT_TEMPLATES_DIR, "-id", normalized, "-duc"],
            validate_timeout(timeout_seconds),
            {"operation": "find-template", "template_id": normalized},
        )

    @mcp.tool(title="Nuclei Scan By Template ID")
    def run_scan_by_template_id(
        target: str,
        template_id: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        normalized_target = validate_target(target)
        normalized_id = validate_template_id(template_id)
        arguments = base_scan_arguments(normalized_target)
        arguments.extend(["-id", normalized_id])
        return command_plan(
            arguments,
            validate_timeout(timeout_seconds),
            {"operation": "scan-by-template-id", "target": normalized_target, "template_id": normalized_id},
        )
