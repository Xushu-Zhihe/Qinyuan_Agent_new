from __future__ import annotations

from agent_core.file_inspection import (
    FILE_TOOL,
    inspection_arguments,
    validate_inspection_path,
)
from agent_core.tool_contracts import (
    MCPCommand,
    PolicyDecision,
    ToolAuthorization,
    ToolRequest,
)


class FileToolPolicy:
    def authorize_intent_path(self, *, goal_path: str, requested_path: str) -> PolicyDecision:
        """T4's extra scope check; gateway authorization is still mandatory."""
        return PolicyDecision(
            requested_path == goal_path,
            "authorized" if requested_path == goal_path else "path_out_of_scope",
        )

    def authorize(
        self, request: ToolRequest, authorization: ToolAuthorization
    ) -> PolicyDecision:
        if request.task_run_id != authorization.task_run_id:
            return PolicyDecision(False, "authorization_task_mismatch")
        if (
            request.tool_name != FILE_TOOL
            or request.tool_name not in authorization.allowed_tools
        ):
            return PolicyDecision(False, "tool_not_authorized")
        try:
            validate_inspection_path(request.path)
        except ValueError:
            return PolicyDecision(False, "file_path_rejected")
        if request.path not in authorization.allowed_paths:
            return PolicyDecision(False, "file_not_authorized")
        return PolicyDecision(True, "authorized")

    def validate_command(
        self, request: ToolRequest, command: MCPCommand
    ) -> PolicyDecision:
        expected = MCPCommand(
            "python3", inspection_arguments(request.path), request.timeout_seconds
        )
        if command != expected:
            return PolicyDecision(False, "mcp_command_rejected")
        return PolicyDecision(True, "command_authorized")
