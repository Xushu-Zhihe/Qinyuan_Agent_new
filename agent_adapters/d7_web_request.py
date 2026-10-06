"""Parser for the explicitly authorized protocol-v3 Web profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent_core.contracts import TaskRequest
from agent_core.d7_web_contracts import D7WebAuthorization, D7WebRunConfig, canonical_web_origin
from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.turn_contracts import D1RoleConfig


def _uuid4(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class D7WebRequest:
    request_id: str
    task_request: TaskRequest
    authorization: D7WebAuthorization
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    d4_budget: D4BudgetPolicy
    source_root: str

    @classmethod
    def parse(cls, value: object) -> D7WebRequest:
        if not isinstance(value, dict) or value.get("protocol_version") != 3:
            raise ValueError("unsupported_protocol_version")
        if set(value) != {
            "protocol_version",
            "request_id",
            "task_request",
            "authorization",
            "dual_llm_config",
        }:
            raise ValueError("invalid_v3_web_request_fields")
        task = value["task_request"]
        if not isinstance(task, dict) or set(task) - {
            "goal",
            "target",
            "deadline_seconds",
            "model_backend",
        }:
            raise ValueError("invalid_v3_web_task_request")
        target = task.get("target")
        if not isinstance(target, dict) or set(target) - {
            "attachments",
            "endpoints",
        }:
            raise ValueError("invalid_v3_web_target")
        endpoints = target.get("endpoints")
        attachments = target.get("attachments", [])
        if (
            not isinstance(endpoints, list)
            or len(endpoints) != 1
            or not isinstance(attachments, list)
            or attachments
        ):
            raise ValueError("d7_web_requires_one_endpoint_and_no_attachments")
        endpoint = endpoints[0]
        if not isinstance(endpoint, dict) or set(endpoint) != {"url"}:
            raise ValueError("invalid_v3_web_endpoint")
        authorized_origin = canonical_web_origin(endpoint["url"])
        authorization = D7WebAuthorization.from_dict(value["authorization"])
        if authorization.allowed_origin != authorized_origin:
            raise ValueError("d7_web_endpoint_authorization_mismatch")
        dual = value["dual_llm_config"]
        if not isinstance(dual, dict) or set(dual) != {
            "outer_role",
            "inner_role",
            "d4_budget",
        }:
            raise ValueError("invalid_v3_web_dual_llm_config")
        outer = D1RoleConfig.from_dict(dual["outer_role"])
        inner = D1RoleConfig.from_dict(dual["inner_role"])
        budget = dual["d4_budget"]
        if not isinstance(budget, dict):
            raise ValueError("d4_budget_required")
        policy = D4BudgetPolicy.from_dict(budget)
        normalized_task = dict(task)
        normalized_target = dict(target)
        normalized_target["endpoints"] = [{"url": authorized_origin}]
        normalized_target["attachments"] = []
        normalized_task["target"] = normalized_target
        request = TaskRequest.from_dict(normalized_task)
        source_root = str(Path.cwd().resolve())
        return cls(
            _uuid4(value["request_id"], "request_id"),
            request,
            authorization,
            outer,
            inner,
            policy,
            source_root,
        )

    def run_config(
        self, task_run_id: str, model_transport_config_ref: str
    ) -> D7WebRunConfig:
        return D7WebRunConfig(
            task_run_id=task_run_id,
            request_id=self.request_id,
            endpoint=self.authorization.allowed_origin,
            authorization=self.authorization,
            outer_role=self.outer_role,
            inner_role=self.inner_role,
            d4_budget=self.d4_budget,
            model_transport_config_ref=model_transport_config_ref,
        )


__all__ = ["D7WebRequest"]
