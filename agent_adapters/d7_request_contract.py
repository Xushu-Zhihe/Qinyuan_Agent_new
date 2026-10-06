"""Validation for the temporary protocol v3 simulation envelope.

This module deliberately validates the public envelope without pretending to
freeze the eventual real-task wire protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.turn_contracts import D1RoleConfig
from agent_core.contracts import TaskRequest


def _uuid4(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{name}_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class V3SimulationRequest:
    request_id: str
    task_request: dict[str, object]
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    authorization: dict[str, object]
    attachments: tuple[str, ...]
    source_root: str
    d4_budget: D4BudgetPolicy

    @classmethod
    def parse(cls, value: object) -> "V3SimulationRequest":
        if not isinstance(value, dict) or value.get("protocol_version") != 3:
            raise ValueError("unsupported_protocol_version")
        required = {"protocol_version", "request_id", "task_request", "authorization", "dual_llm_config"}
        if set(value) != required:
            raise ValueError("invalid_v3_request_fields")
        if not isinstance(value.get("task_request"), dict):
            raise ValueError("invalid_v3_task_request")
        task = value["task_request"]
        if set(task) - {"goal", "target", "deadline_seconds", "model_backend"}:
            raise ValueError("invalid_v3_task_request_fields")
        if "goal" not in task or "target" not in task:
            raise ValueError("invalid_v3_task_request")
        target = task.get("target")
        if not isinstance(target, dict):
            raise ValueError("invalid_v3_target")
        if set(target) - {"source_root", "attachments", "endpoints"}:
            raise ValueError("invalid_v3_target_fields")
        attachments = target.get("attachments")
        endpoints = target.get("endpoints", [])
        if not isinstance(attachments, list) or not isinstance(endpoints, list):
            raise ValueError("invalid_v3_target")
        if endpoints:
            raise ValueError("v3_network_not_supported")
        if len(attachments) > 16:
            raise ValueError("attachment_count_invalid")
        paths: list[str] = []
        for item in attachments:
            if not isinstance(item, dict) or set(item) not in (
                {"kind", "path"}, {"kind", "local_path"}
            ):
                raise ValueError("invalid_v3_attachment")
            if item.get("kind") != "local_path":
                raise ValueError("v3_requires_local_attachments")
            path_keys = [key for key in ("path", "local_path") if key in item]
            if len(path_keys) != 1 or not isinstance(item[path_keys[0]], str):
                raise ValueError("invalid_v3_attachment")
            paths.append(item[path_keys[0]])
        auth = value.get("authorization")
        if not isinstance(auth, dict) or set(auth) != {
            "authorization_id", "attachment_indices", "allowed_tool", "network_allowed"
        }:
            raise ValueError("invalid_v3_authorization")
        indices = auth.get("attachment_indices")
        if (
            not isinstance(indices, list)
            or any(type(index) is not int for index in indices)
            or indices != list(range(len(paths)))
        ):
            raise ValueError("attachment_indices_must_cover_all")
        _uuid4(auth.get("authorization_id"), "authorization_id")
        if auth.get("allowed_tool") != "d3_fixed_readonly" or auth.get("network_allowed") is not False:
            raise ValueError("v3_authorization_not_supported")
        dual = value.get("dual_llm_config")
        if not isinstance(dual, dict) or set(dual) != {
            "outer_role", "inner_role", "d4_budget"
        }:
            raise ValueError("invalid_v3_dual_llm_config")
        outer = D1RoleConfig.from_dict(dual.get("outer_role"))
        inner = D1RoleConfig.from_dict(dual.get("inner_role"))
        budget = dual.get("d4_budget")
        if not isinstance(budget, dict):
            raise ValueError("d4_budget_required")
        policy = D4BudgetPolicy.from_dict(budget)
        # The temporary envelope follows the existing CLI convention where
        # relative attachment paths resolve beneath the process workspace.
        source_root = target.get("source_root", str(Path.cwd().resolve()))
        if not isinstance(source_root, str) or not Path(source_root).is_absolute():
            raise ValueError("source_root_must_be_absolute")
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal.strip() or "\x00" in goal:
            raise ValueError("invalid_v3_task_goal")
        deadline = task.get("deadline_seconds", 3600)
        if type(deadline) is not int or deadline <= 0:
            raise ValueError("invalid_v3_task_deadline")
        if paths:
            normalized_task = dict(task)
            normalized_target = dict(target)
            normalized_target["attachments"] = [
                {"kind": "local_path", "path": path} for path in paths
            ]
            normalized_task["target"] = normalized_target
            TaskRequest.from_dict(normalized_task)
        return cls(_uuid4(value.get("request_id"), "request_id"), task, outer, inner, auth,
                   tuple(paths), source_root, policy)
