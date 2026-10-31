"""Frozen authorization and configuration for one D7 Web TaskRun."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from uuid import UUID

from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.turn_contracts import D1RoleConfig

D7_WEB_ALLOWED_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)
D7_WEB_MAX_REQUEST_BODY_BYTES = 64 * 1024
D7_WEB_MAX_RESPONSE_BODY_BYTES = 2 * 1024 * 1024
D7_WEB_MAX_MODEL_TEXT_BYTES = 64 * 1024
D7_WEB_MAX_HEADER_BYTES = 64 * 1024
D7_WEB_MAX_HEADER_COUNT = 100
D7_WEB_MAX_HEADER_VALUE_BYTES = 8 * 1024
D7_WEB_RESPONSE_HEADERS = frozenset({"content-type", "location", "set-cookie"})
D7_WEB_SANDBOX_IMAGE = "qinyuan/pentest-sandbox:0.3.5"
D7_WEB_FORWARDER_PROFILE = "qinyuan.d7.taskrun-forwarder.v1"


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


def _uuid4_or5(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{field}_invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field}_invalid")
    return value


def _timestamp(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field}_invalid")
    return float(value)


def _sha256_ref(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ValueError(f"{field}_invalid")
    return value


def canonical_web_origin(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ValueError("d7_web_origin_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("d7_web_origin_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise ValueError("d7_web_origin_invalid")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"


@dataclass(frozen=True, slots=True)
class D7WebAuthorization:
    authorization_id: str
    allowed_origin: str
    allowed_methods: tuple[str, ...]
    allowed_tool: str = "d7_http_request"
    network_allowed: bool = True
    attachment_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _uuid4(self.authorization_id, "authorization_id")
        if self.allowed_origin != canonical_web_origin(self.allowed_origin):
            raise ValueError("d7_web_origin_must_be_canonical")
        if (
            not isinstance(self.allowed_methods, tuple)
            or not self.allowed_methods
            or any(
                not isinstance(method, str) or method not in D7_WEB_ALLOWED_METHODS
                for method in self.allowed_methods
            )
            or len(self.allowed_methods) != len(set(self.allowed_methods))
        ):
            raise ValueError("d7_web_allowed_methods_invalid")
        if (
            self.allowed_tool != "d7_http_request"
            or self.network_allowed is not True
            or self.attachment_indices != ()
        ):
            raise ValueError("d7_web_authorization_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "allowed_origin": self.allowed_origin,
            "allowed_methods": list(self.allowed_methods),
            "allowed_tool": self.allowed_tool,
            "network_allowed": self.network_allowed,
            "attachment_indices": [],
        }

    @classmethod
    def from_dict(cls, value: object) -> D7WebAuthorization:
        if not isinstance(value, dict) or set(value) != {
            "authorization_id",
            "allowed_origin",
            "allowed_methods",
            "allowed_tool",
            "network_allowed",
            "attachment_indices",
        }:
            raise ValueError("d7_web_authorization_fields_invalid")
        methods = value["allowed_methods"]
        indices = value["attachment_indices"]
        if (
            not isinstance(methods, list)
            or any(not isinstance(item, str) for item in methods)
            or indices != []
        ):
            raise ValueError("d7_web_authorization_fields_invalid")
        return cls(
            value["authorization_id"],  # type: ignore[arg-type]
            value["allowed_origin"],  # type: ignore[arg-type]
            tuple(methods),
            value["allowed_tool"],  # type: ignore[arg-type]
            value["network_allowed"],  # type: ignore[arg-type]
            (),
        )


@dataclass(frozen=True, slots=True)
class D7WebRunConfig:
    task_run_id: str
    request_id: str
    endpoint: str
    authorization: D7WebAuthorization
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    d4_budget: D4BudgetPolicy
    model_transport_config_ref: str
    sandbox_image: str = D7_WEB_SANDBOX_IMAGE
    forwarder_profile: str = D7_WEB_FORWARDER_PROFILE
    schema_version: int = 1

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id, "task_run_id")
        _uuid4(self.request_id, "request_id")
        if (
            not isinstance(self.endpoint, str)
            or self.endpoint != canonical_web_origin(self.endpoint)
            or self.endpoint != self.authorization.allowed_origin
        ):
            raise ValueError("d7_web_endpoint_authorization_mismatch")
        if (
            not isinstance(self.outer_role, D1RoleConfig)
            or self.outer_role.role != "outer_planning"
            or not isinstance(self.inner_role, D1RoleConfig)
            or self.inner_role.role != "inner_action"
            or self.outer_role.provider != "deepseek"
            or self.inner_role.provider != "deepseek"
            or self.outer_role.prompt_template != "qinyuan.d7.outer.v1"
            or self.inner_role.prompt_template != "qinyuan.d7.inner.v1"
        ):
            raise ValueError("d7_web_role_configuration_invalid")
        if not isinstance(self.d4_budget, D4BudgetPolicy):
            raise ValueError("d7_web_d4_budget_invalid")
        _sha256_ref(self.model_transport_config_ref, "model_transport_config_ref")
        if (
            self.sandbox_image != D7_WEB_SANDBOX_IMAGE
            or self.forwarder_profile != D7_WEB_FORWARDER_PROFILE
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("d7_web_runtime_profile_invalid")

    @property
    def role_config_refs(self) -> tuple[str, str]:
        return (self.outer_role.role_config_ref, self.inner_role.role_config_ref)

    @property
    def config_ref(self) -> str:
        payload = self.to_dict()
        payload.pop("task_run_id")
        payload.pop("request_id")
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_run_id": self.task_run_id,
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "authorization": self.authorization.to_dict(),
            "outer_role": {
                "role": self.outer_role.role,
                "provider": self.outer_role.provider,
                "model": self.outer_role.model,
                "prompt_template": self.outer_role.prompt_template,
                "parameters": [list(item) for item in self.outer_role.parameters],
            },
            "inner_role": {
                "role": self.inner_role.role,
                "provider": self.inner_role.provider,
                "model": self.inner_role.model,
                "prompt_template": self.inner_role.prompt_template,
                "parameters": [list(item) for item in self.inner_role.parameters],
            },
            "d4_budget": asdict(self.d4_budget),
            "model_transport_config_ref": self.model_transport_config_ref,
            "sandbox_image": self.sandbox_image,
            "forwarder_profile": self.forwarder_profile,
        }

    @classmethod
    def from_dict(cls, value: object) -> D7WebRunConfig:
        fields = {
            "schema_version",
            "task_run_id",
            "request_id",
            "endpoint",
            "authorization",
            "outer_role",
            "inner_role",
            "d4_budget",
            "model_transport_config_ref",
            "sandbox_image",
            "forwarder_profile",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("d7_web_run_config_fields_invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("d7_web_run_config_version_unsupported")
        outer = D1RoleConfig.from_dict(value["outer_role"])
        inner = D1RoleConfig.from_dict(value["inner_role"])
        budget = value["d4_budget"]
        if not isinstance(budget, dict):
            raise ValueError("d7_web_d4_budget_invalid")
        return cls(
            value["task_run_id"],  # type: ignore[arg-type]
            value["request_id"],  # type: ignore[arg-type]
            value["endpoint"],  # type: ignore[arg-type]
            D7WebAuthorization.from_dict(value["authorization"]),
            outer,
            inner,
            D4BudgetPolicy.from_dict(budget),
            value["model_transport_config_ref"],  # type: ignore[arg-type]
            value["sandbox_image"],  # type: ignore[arg-type]
            value["forwarder_profile"],  # type: ignore[arg-type]
            value["schema_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class D7WebPlanRecord:
    """Body-free immutable record of one proposed HTTP action."""

    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    method: str
    request_sha256: str
    request_bytes: int
    created_at: float

    def __post_init__(self) -> None:
        _uuid4(self.task_run_id, "task_run_id")
        _uuid4_or5(self.turn_id, "turn_id")
        _uuid4_or5(self.intent_id, "intent_id")
        _uuid4(self.plan_id, "plan_id")
        if self.method not in D7_WEB_ALLOWED_METHODS:
            raise ValueError("d7_web_plan_method_invalid")
        _sha256(self.request_sha256, "request_sha256")
        if (
            type(self.request_bytes) is not int
            or not 0 <= self.request_bytes <= 64 * 1024
        ):
            raise ValueError("d7_web_plan_request_bytes_invalid")
        _timestamp(self.created_at, "created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_run_id": self.task_run_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "method": self.method,
            "request_sha256": self.request_sha256,
            "request_bytes": self.request_bytes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> D7WebPlanRecord:
        fields = {
            "schema_version",
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "method",
            "request_sha256",
            "request_bytes",
            "created_at",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("d7_web_plan_record_fields_invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("d7_web_plan_record_version_unsupported")
        return cls(
            value["task_run_id"],  # type: ignore[arg-type]
            value["turn_id"],  # type: ignore[arg-type]
            value["intent_id"],  # type: ignore[arg-type]
            value["plan_id"],  # type: ignore[arg-type]
            value["method"],  # type: ignore[arg-type]
            value["request_sha256"],  # type: ignore[arg-type]
            value["request_bytes"],  # type: ignore[arg-type]
            value["created_at"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class D7WebExecutionRecord:
    """Body-free durable state for one admitted HTTP dispatch."""

    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    execution_id: str
    http_operation_id: str
    state: str
    request_sha256: str
    request_bytes: int
    started_at: float
    http_status: int | None = None
    response_evidence_id: str | None = None
    response_sha256: str | None = None
    response_bytes: int | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "task_run_id",
            "plan_id",
            "execution_id",
            "http_operation_id",
        ):
            _uuid4(getattr(self, name), name)
        for name in ("turn_id", "intent_id"):
            _uuid4_or5(getattr(self, name), name)
        if self.state not in {"started", "completed", "rejected", "unknown"}:
            raise ValueError("d7_web_execution_state_invalid")
        _sha256(self.request_sha256, "request_sha256")
        if (
            type(self.request_bytes) is not int
            or not 0 <= self.request_bytes <= 64 * 1024
        ):
            raise ValueError("d7_web_execution_request_bytes_invalid")
        _timestamp(self.started_at, "started_at")
        response_values = (
            self.http_status,
            self.response_evidence_id,
            self.response_sha256,
            self.response_bytes,
        )
        if self.state == "completed":
            if (
                type(self.http_status) is not int
                or not 100 <= self.http_status <= 599
                or not isinstance(self.response_evidence_id, str)
                or not isinstance(self.response_sha256, str)
                or type(self.response_bytes) is not int
                or not 0 <= self.response_bytes <= 2 * 1024 * 1024
                or self.finished_at is None
            ):
                raise ValueError("d7_web_execution_completion_invalid")
            _uuid4(self.response_evidence_id, "response_evidence_id")
            _sha256(self.response_sha256, "response_sha256")
        elif (
            any(value is not None for value in response_values)
            or self.finished_at is None
            and self.state != "started"
        ):
            raise ValueError("d7_web_execution_terminal_fields_invalid")
        elif self.state == "started" and self.finished_at is not None:
            raise ValueError("d7_web_execution_start_fields_invalid")
        if self.finished_at is not None:
            _timestamp(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("d7_web_execution_finished_before_start")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_run_id": self.task_run_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "execution_id": self.execution_id,
            "http_operation_id": self.http_operation_id,
            "state": self.state,
            "request_sha256": self.request_sha256,
            "request_bytes": self.request_bytes,
            "started_at": self.started_at,
            "http_status": self.http_status,
            "response_evidence_id": self.response_evidence_id,
            "response_sha256": self.response_sha256,
            "response_bytes": self.response_bytes,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> D7WebExecutionRecord:
        fields = {
            "schema_version",
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "execution_id",
            "http_operation_id",
            "state",
            "request_sha256",
            "request_bytes",
            "started_at",
            "http_status",
            "response_evidence_id",
            "response_sha256",
            "response_bytes",
            "finished_at",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("d7_web_execution_record_fields_invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("d7_web_execution_record_version_unsupported")
        return cls(
            value["task_run_id"],  # type: ignore[arg-type]
            value["turn_id"],  # type: ignore[arg-type]
            value["intent_id"],  # type: ignore[arg-type]
            value["plan_id"],  # type: ignore[arg-type]
            value["execution_id"],  # type: ignore[arg-type]
            value["http_operation_id"],  # type: ignore[arg-type]
            value["state"],  # type: ignore[arg-type]
            value["request_sha256"],  # type: ignore[arg-type]
            value["request_bytes"],  # type: ignore[arg-type]
            value["started_at"],  # type: ignore[arg-type]
            value["http_status"],  # type: ignore[arg-type]
            value["response_evidence_id"],  # type: ignore[arg-type]
            value["response_sha256"],  # type: ignore[arg-type]
            value["response_bytes"],  # type: ignore[arg-type]
            value["finished_at"],  # type: ignore[arg-type]
        )


__all__ = [
    "D7_WEB_ALLOWED_METHODS",
    "D7_WEB_FORWARDER_PROFILE",
    "D7_WEB_MAX_HEADER_BYTES",
    "D7_WEB_MAX_HEADER_COUNT",
    "D7_WEB_MAX_HEADER_VALUE_BYTES",
    "D7_WEB_MAX_MODEL_TEXT_BYTES",
    "D7_WEB_MAX_REQUEST_BODY_BYTES",
    "D7_WEB_MAX_RESPONSE_BODY_BYTES",
    "D7_WEB_RESPONSE_HEADERS",
    "D7_WEB_SANDBOX_IMAGE",
    "D7WebAuthorization",
    "D7WebExecutionRecord",
    "D7WebPlanRecord",
    "D7WebRunConfig",
    "canonical_web_origin",
]
