"""Frozen contracts for the confirmed D1 single-Turn slice."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import UUID

if TYPE_CHECKING:
    from agent_core.control_store import ControlStateSnapshot

D1Role = Literal["outer_planning", "inner_action"]
D1Category = Literal[
    "outer_model",
    "inner_probe_model",
    "inner_complete_model",
    "inner_tool",
    "first_digest",
    "attachment_import",
    "copy_preparation",
]
D1ResultKind = Literal["completed", "rejected", "stale", "not_satisfied", "failed"]
D1_CATEGORY_LIMITS: dict[str, int | None] = {
    "outer_model": 2,
    "inner_probe_model": 2,
    "inner_complete_model": 2,
    "inner_tool": 1,
    "first_digest": 1,
    "attachment_import": None,
    "copy_preparation": None,
}


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string")


def _uuid(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5") from exc
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5")


def _sha(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _time(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite timestamp")


def d1_payload(value: object) -> dict[str, object]:
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("D1 payload requires a dataclass instance")
    return asdict(value)


def d1_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class D1RoleConfig:
    role: D1Role
    provider: str
    model: str
    prompt_template: str
    parameters: tuple[tuple[str, str | int | float | bool], ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"outer_planning", "inner_action"}:
            raise ValueError("unsupported D1 role")
        for name in ("provider", "model", "prompt_template"):
            _text(getattr(self, name), name)
        if not isinstance(self.parameters, tuple):
            raise TypeError("parameters must be immutable")
        names = []
        for pair in self.parameters:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("invalid model parameter")
            name, value = pair
            _text(name, "parameter name")
            if name.lower() in {
                "api_key",
                "apikey",
                "authorization",
                "password",
                "secret",
                "token",
                "access_token",
            }:
                raise ValueError("credentials must not enter role configuration")
            if (
                type(value) not in {str, int, float, bool}
                or isinstance(value, float)
                and not math.isfinite(value)
            ):
                raise ValueError("model parameter must be a finite scalar")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("model parameter names must be unique")

    @property
    def role_config_ref(self) -> str:
        return "sha256:" + d1_sha256(d1_payload(self))

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D1RoleConfig:
        if set(value) != {"role", "provider", "model", "prompt_template", "parameters"}:
            raise ValueError("invalid D1RoleConfig fields")
        parameters = value["parameters"]
        if not isinstance(parameters, list):
            raise TypeError("persisted model parameters must be a list")
        return cls(
            cast(D1Role, value["role"]),
            cast(str, value["provider"]),
            cast(str, value["model"]),
            cast(str, value["prompt_template"]),
            tuple(tuple(pair) for pair in parameters),
        )


@dataclass(frozen=True, slots=True)
class D1Authorization:
    authorization_id: str
    attachment_index: int = 0
    allowed_tool: str = "d1_fixed_sha256"
    network_allowed: bool = False

    def __post_init__(self) -> None:
        _uuid(self.authorization_id, "authorization_id")
        if type(self.attachment_index) is not int or self.attachment_index != 0:
            raise ValueError("D1 authorizes the sole attachment index zero")
        if self.allowed_tool != "d1_fixed_sha256" or self.network_allowed is not False:
            raise ValueError("D1 requires fixed digest authorization without network")


@dataclass(frozen=True, slots=True)
class D1RunConfig:
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    authorization: D1Authorization
    chain_id: str = "dual_llm_d1"
    contract_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outer_role, D1RoleConfig)
            or self.outer_role.role != "outer_planning"
        ):
            raise ValueError("outer role configuration is required")
        if (
            not isinstance(self.inner_role, D1RoleConfig)
            or self.inner_role.role != "inner_action"
        ):
            raise ValueError("inner role configuration is required")
        if not isinstance(self.authorization, D1Authorization):
            raise TypeError("D1 authorization is required")
        if (
            self.chain_id != "dual_llm_d1"
            or type(self.contract_version) is not int
            or self.contract_version != 1
        ):
            raise ValueError("unsupported D1 chain contract")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D1RunConfig:
        if set(value) != {
            "outer_role",
            "inner_role",
            "authorization",
            "chain_id",
            "contract_version",
        }:
            raise ValueError("invalid D1RunConfig fields")
        return cls(
            D1RoleConfig.from_dict(cast(dict[str, object], value["outer_role"])),
            D1RoleConfig.from_dict(cast(dict[str, object], value["inner_role"])),
            D1Authorization(**cast(dict, value["authorization"])),
            cast(str, value["chain_id"]),
            cast(int, value["contract_version"]),
        )


@dataclass(frozen=True, slots=True)
class D1TurnBinding:
    binding_id: str
    task_run_id: str
    turn_id: str
    intent_id: str
    attachment_index: int
    import_id: str
    expected_sha256: str
    work_path: str
    preparation_ref: str
    proposal_request_id: str
    rule_id: str = "attachment_copy_sha256"
    rule_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "task_run_id",
            "turn_id",
            "intent_id",
            "import_id",
            "preparation_ref",
            "proposal_request_id",
        ):
            _uuid(getattr(self, name), name)
        if type(self.attachment_index) is not int or self.attachment_index != 0:
            raise ValueError("D1 binds the sole attachment index zero")
        _sha(self.expected_sha256, "expected_sha256")
        prefix = f"/workspace/work/intents/{self.intent_id}/"
        if (
            not isinstance(self.work_path, str)
            or not self.work_path.startswith(prefix)
            or self.work_path == prefix
            or "\\" in self.work_path
            or posixpath.normpath(self.work_path) != self.work_path
        ):
            raise ValueError("work_path must belong to the bound Intent directory")
        if (
            self.rule_id != "attachment_copy_sha256"
            or type(self.rule_version) is not int
            or self.rule_version != 1
        ):
            raise ValueError("unsupported D1 completion rule")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D1TurnBinding:
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class D1TurnProof:
    turn_id: str
    binding_id: str
    proof_ref: str
    supporting_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid(self.turn_id, "turn_id")
        _uuid(self.binding_id, "binding_id")
        _text(self.proof_ref, "proof_ref")
        if (
            not isinstance(self.supporting_fact_ids, tuple)
            or not self.supporting_fact_ids
            or len(set(self.supporting_fact_ids)) != len(self.supporting_fact_ids)
        ):
            raise ValueError("Turn proof requires unique supporting Fact IDs")
        for value in self.supporting_fact_ids:
            _uuid(value, "supporting_fact_id")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D1TurnProof:
        return cls(
            cast(str, value["turn_id"]),
            cast(str, value["binding_id"]),
            cast(str, value["proof_ref"]),
            tuple(cast(list[str], value["supporting_fact_ids"])),
        )


@dataclass(frozen=True, slots=True)
class D1TurnEvaluation:
    satisfied: bool
    proof_ref: str | None
    supporting_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.satisfied) is not bool or not isinstance(
            self.supporting_fact_ids, tuple
        ):
            raise ValueError("invalid Turn evaluation")
        if self.satisfied:
            _text(self.proof_ref, "proof_ref")
            if not self.supporting_fact_ids:
                raise ValueError("satisfied Turn requires supporting Facts")
        elif self.proof_ref is not None:
            raise ValueError("unsatisfied Turn must not claim a success proof")


class D1TurnEvaluatorPort(Protocol):
    def evaluate(
        self,
        binding: D1TurnBinding,
        state: ControlStateSnapshot,
        fact_ids: tuple[str, ...],
    ) -> D1TurnEvaluation: ...


@dataclass(frozen=True, slots=True)
class D1OperationStart:
    operation_id: str
    task_run_id: str
    category: D1Category
    started_at: float
    base_checkpoint_sequence: int
    context_sha256: str
    role: D1Role | None = None
    role_config_ref: str | None = None
    resource_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation_id")
        _uuid(self.task_run_id, "task_run_id")
        if self.category not in D1_CATEGORY_LIMITS:
            raise ValueError("unsupported D1 operation category")
        _time(self.started_at, "started_at")
        if (
            type(self.base_checkpoint_sequence) is not int
            or self.base_checkpoint_sequence < 1
        ):
            raise ValueError("operation requires a committed checkpoint")
        _sha(self.context_sha256, "context_sha256")
        expected_role = (
            "outer_planning"
            if self.category == "outer_model"
            else "inner_action"
            if self.category in {"inner_probe_model", "inner_complete_model"}
            else None
        )
        if self.role != expected_role:
            raise ValueError("operation role must match its model category")
        if expected_role is not None:
            if not isinstance(
                self.role_config_ref, str
            ) or not self.role_config_ref.startswith("sha256:"):
                raise ValueError(
                    "model operation requires immutable role configuration"
                )
            _sha(self.role_config_ref[7:], "role_config_ref")
        elif self.role_config_ref is not None:
            raise ValueError("non-model operations do not use model role configuration")
        if self.resource_ref is not None:
            _text(self.resource_ref, "resource_ref")


@dataclass(frozen=True, slots=True)
class D1OperationCompletion:
    operation_id: str
    finished_at: float
    result_kind: D1ResultKind
    evidence_ref: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation_id")
        _time(self.finished_at, "finished_at")
        if self.result_kind not in {
            "completed",
            "rejected",
            "stale",
            "not_satisfied",
            "failed",
        }:
            raise ValueError("unsupported known D1 operation result")
        _text(self.evidence_ref, "evidence_ref")
        _sha(self.evidence_sha256, "evidence_sha256")


@dataclass(frozen=True, slots=True)
class D1OperationRecord:
    start: D1OperationStart
    completion: D1OperationCompletion | None = None
    application_ref: str | None = None


class D1RecoveryRequired(RuntimeError):
    pass


class D1BudgetExhausted(RuntimeError):
    pass


class D1OperationConflict(RuntimeError):
    pass


class D1OperationAuditPort(Protocol):
    def begin_d1_operation(
        self, start: D1OperationStart
    ) -> D1OperationRecord | None: ...
    def complete_d1_operation(
        self, completion: D1OperationCompletion
    ) -> D1OperationRecord: ...
    def mark_d1_operation_applied(
        self, operation_id: str, application_ref: str
    ) -> D1OperationRecord: ...
    def load_d1_operation(self, operation_id: str) -> D1OperationRecord | None: ...
    def list_d1_operations(self, task_run_id: str) -> tuple[D1OperationRecord, ...]: ...
