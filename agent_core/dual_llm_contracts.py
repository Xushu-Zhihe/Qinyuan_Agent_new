"""Frozen internal contracts for the dual-LLM multi-path slice."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, is_dataclass
from typing import Literal
from uuid import UUID

from agent_core.review_contracts import (
    D2AllowedActionKinds,
    D2Constraint,
    D2MaxActionTimeout,
    D2ReviewRequest,
    parse_d2_output,
)
from agent_core.turn_contracts import D1RoleConfig

D3PlanTemplate = Literal["size_then_sha256", "sha256_only"]
D3ObservationMode = Literal["new_observation", "reuse_saved_proof"]
D3OperationCategory = Literal[
    "outer_model",
    "inner_model",
    "tool_dispatch",
    "sha_observation",
    "attachment_import",
    "copy_preparation",
    "artifact_share",
    "intent_stop",
]
D3OperationResult = Literal["completed", "rejected", "stale", "not_satisfied", "failed"]
D3ReviewDecision = Literal[
    "continue_current_intent",
    "switch_sibling_intent",
    "replan_current_intent",
    "adjust_turn_target",
    "acknowledge_turn_end",
]
D3EntityKind = Literal["turn", "intent", "plan"]
D3_CATEGORY_LIMITS: dict[str, int | None] = {
    "outer_model": 4,
    "inner_model": 16,
    "tool_dispatch": 6,
    "sha_observation": 4,
    "attachment_import": 2,
    "copy_preparation": None,
    "artifact_share": 4,
    "intent_stop": None,
}
D3_SIZE_STEP = "fixed read-only attachment size check"
D3_SHA_STEP = "fixed read-only SHA-256 check"


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")


def _uuid(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a trusted UUID") from exc
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5")


def _sha(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be lowercase SHA-256")


def _time(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite timestamp")


def d3_payload(value: object) -> dict[str, object]:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else {}


@dataclass(frozen=True, slots=True)
class D3Authorization:
    authorization_id: str
    attachment_indices: tuple[int, ...]
    allowed_tool: str = "d3_fixed_readonly"
    network_allowed: bool = False

    def __post_init__(self) -> None:
        _uuid(self.authorization_id, "authorization_id")
        if self.attachment_indices not in {(0,), (0, 1)}:
            raise ValueError("D3 authorizes one or two ordered local attachments")
        if (
            self.allowed_tool != "d3_fixed_readonly"
            or self.network_allowed is not False
        ):
            raise ValueError("D3 permits only fixed read-only checks without network")


@dataclass(frozen=True, slots=True)
class D3RunConfig:
    outer_role: D1RoleConfig
    inner_role: D1RoleConfig
    authorization: D3Authorization
    review_role_ref: str
    chain_id: str = "dual_llm_d3"
    contract_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outer_role, D1RoleConfig)
            or self.outer_role.role != "outer_planning"
        ):
            raise ValueError("D3 requires a frozen outer planning role")
        if (
            not isinstance(self.inner_role, D1RoleConfig)
            or self.inner_role.role != "inner_action"
        ):
            raise ValueError("D3 requires a frozen inner action role")
        if not isinstance(self.authorization, D3Authorization):
            raise TypeError("D3 requires frozen authorization")
        if self.review_role_ref != self.outer_role.role_config_ref:
            raise ValueError("D3 review must use the frozen outer role")
        if (
            self.chain_id != "dual_llm_d3"
            or type(self.contract_version) is not int
            or self.contract_version != 1
        ):
            raise ValueError("unsupported D3 chain contract")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> D3RunConfig:
        if set(value) != {
            "outer_role",
            "inner_role",
            "authorization",
            "review_role_ref",
            "chain_id",
            "contract_version",
        }:
            raise ValueError("invalid D3RunConfig fields")
        authorization = value["authorization"]
        if not isinstance(authorization, dict):
            raise TypeError("invalid D3 authorization")
        indices = authorization.get("attachment_indices")
        if not isinstance(indices, list):
            raise TypeError("invalid D3 attachment indices")
        return cls(
            D1RoleConfig.from_dict(value["outer_role"]),  # type: ignore[arg-type]
            D1RoleConfig.from_dict(value["inner_role"]),  # type: ignore[arg-type]
            D3Authorization(
                authorization["authorization_id"],  # type: ignore[arg-type]
                tuple(indices),
                authorization["allowed_tool"],  # type: ignore[arg-type]
                authorization["network_allowed"],  # type: ignore[arg-type]
            ),
            value["review_role_ref"],  # type: ignore[arg-type]
            value["chain_id"],  # type: ignore[arg-type]
            value["contract_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class D3TurnBinding:
    binding_id: str
    task_run_id: str
    turn_id: str
    attachment_index: int
    import_id: str
    expected_byte_size: int
    expected_sha256: str
    proposal_request_id: str
    rule_id: str = "attachment_copy_sha256"
    rule_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "task_run_id",
            "turn_id",
            "import_id",
            "proposal_request_id",
        ):
            _uuid(getattr(self, name), name)
        if type(self.attachment_index) is not int or self.attachment_index not in {
            0,
            1,
        }:
            raise ValueError("D3 attachment index must be zero or one")
        if (
            type(self.expected_byte_size) is not int
            or not 0 <= self.expected_byte_size <= 16 * 1024 * 1024
        ):
            raise ValueError("D3 expected size is invalid")
        _sha(self.expected_sha256, "expected_sha256")
        if (
            self.rule_id != "attachment_copy_sha256"
            or type(self.rule_version) is not int
            or self.rule_version != 1
        ):
            raise ValueError("unsupported D3 completion rule")


@dataclass(frozen=True, slots=True)
class D3IntentBinding:
    task_run_id: str
    turn_id: str
    intent_id: str
    work_directory: str

    def __post_init__(self) -> None:
        for name in ("task_run_id", "turn_id", "intent_id"):
            _uuid(getattr(self, name), name)
        if self.work_directory != f"/workspace/work/intents/{self.intent_id}":
            raise ValueError("D3 Intent requires its isolated working directory")


@dataclass(frozen=True, slots=True)
class D3PlanBinding:
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    template: D3PlanTemplate
    observation_mode: D3ObservationMode
    artifact_version_id: str
    proposal_request_id: str
    rule_binding_sha256: str
    replan_review_id: str | None = None
    source_proof_id: str | None = None
    source_fact_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "artifact_version_id",
            "proposal_request_id",
        ):
            _uuid(getattr(self, name), name)
        _sha(self.rule_binding_sha256, "rule_binding_sha256")
        if self.replan_review_id is not None:
            _uuid(self.replan_review_id, "replan_review_id")
        if self.template not in {"size_then_sha256", "sha256_only"}:
            raise ValueError("unsupported D3 Plan template")
        if self.observation_mode not in {"new_observation", "reuse_saved_proof"}:
            raise ValueError("unsupported D3 observation mode")
        if (
            self.template == "size_then_sha256"
            and self.observation_mode != "new_observation"
        ):
            raise ValueError("initial two-step Plan requires a new observation")
        if (self.template == "size_then_sha256") == (self.replan_review_id is not None):
            raise ValueError("only a reviewed replacement may use the one-step Plan")
        if self.observation_mode == "reuse_saved_proof":
            _uuid(self.source_proof_id, "source_proof_id")
            _uuid(self.source_fact_id, "source_fact_id")
        elif self.source_proof_id is not None or self.source_fact_id is not None:
            raise ValueError("new observation cannot cite old support")


@dataclass(frozen=True, slots=True)
class D3PlanResult:
    task_run_id: str
    plan_id: str
    outcome: Literal["satisfied", "not_satisfied"]
    artifact_version_id: str
    observation_id: str
    proof_id: str | None
    evidence_sha256: str
    supporting_fact_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_run_id", "plan_id", "artifact_version_id", "observation_id"):
            _uuid(getattr(self, name), name)
        if self.outcome not in {"satisfied", "not_satisfied"}:
            raise ValueError("unsupported D3 Plan result")
        if self.outcome == "satisfied":
            _uuid(self.proof_id, "proof_id")
            _uuid(self.supporting_fact_id, "supporting_fact_id")
        elif self.proof_id is not None:
            raise ValueError("not_satisfied cannot claim a proof")
        elif self.supporting_fact_id is not None:
            raise ValueError("not_satisfied cannot claim a Fact")
        _sha(self.evidence_sha256, "evidence_sha256")


@dataclass(frozen=True, slots=True)
class D3Outcome:
    entity_kind: D3EntityKind
    entity_id: str
    status: str
    reason: str
    evidence_refs: tuple[str, ...]
    successor_turn_id: str | None = None

    def __post_init__(self) -> None:
        if self.entity_kind not in {"turn", "intent", "plan"}:
            raise ValueError("unsupported D3 outcome owner")
        _uuid(self.entity_id, "entity_id")
        _text(self.status, "status")
        _text(self.reason, "reason")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("D3 outcome requires immutable evidence references")
        for reference in self.evidence_refs:
            _text(reference, "evidence_ref")
        if self.successor_turn_id is not None:
            _uuid(self.successor_turn_id, "successor_turn_id")


@dataclass(frozen=True, slots=True)
class D3ReviewApplication:
    task_run_id: str
    review_root_id: str
    review_generation: int
    review_id: str
    context_version: str
    output_sha256: str
    constraint_version: int
    output_constraints: tuple[D2Constraint, ...]
    turn_id: str
    intent_id: str
    review_scope: Literal["turn_start", "action", "turn_end"]
    action_candidate_id: str | None
    decision: D3ReviewDecision
    source_ids: tuple[str, ...]
    paused_gate: bool = False
    pause_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "task_run_id",
            "review_root_id",
            "review_id",
            "turn_id",
            "intent_id",
        ):
            _uuid(getattr(self, name), name)
        if type(self.review_generation) is not int or self.review_generation < 1:
            raise ValueError("invalid D3 review generation")
        if type(self.constraint_version) is not int or self.constraint_version < 0:
            raise ValueError("invalid D3 constraint version")
        _text(self.context_version, "context_version")
        _sha(self.output_sha256, "output_sha256")
        if self.review_scope not in {"turn_start", "action", "turn_end"}:
            raise ValueError("invalid D3 review scope")
        if (self.review_scope == "action") != (self.action_candidate_id is not None):
            raise ValueError("D3 Action review requires one action candidate")
        if self.action_candidate_id is not None:
            _uuid(self.action_candidate_id, "action_candidate_id")
        if self.decision not in {
            "continue_current_intent",
            "switch_sibling_intent",
            "replan_current_intent",
            "adjust_turn_target",
            "acknowledge_turn_end",
        }:
            raise ValueError("invalid D3 review decision")
        if self.review_scope == "turn_end" and (
            self.decision != "acknowledge_turn_end" or self.output_constraints
        ):
            raise ValueError("D3 tail review is read-only")
        if self.review_scope != "turn_end" and self.decision == "acknowledge_turn_end":
            raise ValueError("D3 tail acknowledgement is terminal only")
        if not isinstance(self.output_constraints, tuple) or any(
            not isinstance(item, (D2AllowedActionKinds, D2MaxActionTimeout))
            for item in self.output_constraints
        ):
            raise ValueError("D3 constraints must be typed")
        if not isinstance(self.source_ids, tuple) or len(set(self.source_ids)) != len(
            self.source_ids
        ):
            raise ValueError("D3 review sources must be immutable and unique")
        for reference in self.source_ids:
            _text(reference, "source_id")
        if type(self.paused_gate) is not bool:
            raise ValueError("invalid D3 pause gate")
        if self.paused_gate:
            _text(self.pause_reason, "pause_reason")
            if self.output_constraints:
                raise ValueError("paused D3 review cannot add constraints")
        elif self.pause_reason is not None:
            raise ValueError("accepted D3 review cannot have a pause reason")


@dataclass(frozen=True, slots=True)
class D3ReviewOutput:
    decision: D3ReviewDecision
    rationale: str
    source_ids: tuple[str, ...]
    constraints: tuple[D2Constraint, ...]

    def __post_init__(self) -> None:
        if self.decision not in {
            "continue_current_intent",
            "switch_sibling_intent",
            "replan_current_intent",
            "adjust_turn_target",
            "acknowledge_turn_end",
        }:
            raise ValueError("invalid D3 review decision")
        _text(self.rationale, "rationale")
        if not isinstance(self.source_ids, tuple) or len(set(self.source_ids)) != len(
            self.source_ids
        ):
            raise ValueError("invalid D3 review sources")
        for source_id in self.source_ids:
            _text(source_id, "source_id")
        if not isinstance(self.constraints, tuple) or any(
            not isinstance(item, (D2AllowedActionKinds, D2MaxActionTimeout))
            for item in self.constraints
        ):
            raise ValueError("invalid D3 review constraints")
        if self.decision == "acknowledge_turn_end" and self.constraints:
            raise ValueError("D3 tail review is read-only")


def parse_d3_review_output(raw: object, request: D2ReviewRequest) -> D3ReviewOutput:
    if isinstance(raw, D3ReviewOutput):
        raw = asdict(raw)
    if not isinstance(raw, dict) or set(raw) != {
        "decision",
        "rationale",
        "source_ids",
        "constraints",
    }:
        raise ValueError("d3_review_output_fields_invalid")
    decision = raw["decision"]
    if decision not in {
        "continue_current_intent",
        "switch_sibling_intent",
        "replan_current_intent",
        "adjust_turn_target",
        "acknowledge_turn_end",
    }:
        raise ValueError("d3_review_decision_invalid")
    if request.review_scope == "turn_end":
        if decision != "acknowledge_turn_end":
            raise ValueError("d3_tail_decision_invalid")
        checked = parse_d2_output(raw, request)
    else:
        if decision == "acknowledge_turn_end":
            raise ValueError("d3_nonterminal_acknowledgement_invalid")
        checked = parse_d2_output(
            {**raw, "decision": "continue_current_intent"}, request
        )
        if not checked.source_ids:
            raise ValueError("d3_review_requires_sources")
    return D3ReviewOutput(
        decision, checked.rationale, checked.source_ids, checked.constraints
    )


@dataclass(frozen=True, slots=True)
class D3OperationStart:
    operation_id: str
    task_run_id: str
    category: D3OperationCategory
    started_at: float
    base_checkpoint_sequence: int
    context_sha256: str
    resource_ref: str
    role: Literal["outer_planning", "inner_action"] | None = None
    role_config_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.operation_id, "operation_id")
        _uuid(self.task_run_id, "task_run_id")
        if self.category not in D3_CATEGORY_LIMITS:
            raise ValueError("unsupported D3 operation category")
        _time(self.started_at, "started_at")
        if (
            type(self.base_checkpoint_sequence) is not int
            or self.base_checkpoint_sequence < 1
        ):
            raise ValueError("D3 operation requires a committed checkpoint")
        _sha(self.context_sha256, "context_sha256")
        _text(self.resource_ref, "resource_ref")
        expected_role = (
            "outer_planning"
            if self.category == "outer_model"
            else "inner_action"
            if self.category == "inner_model"
            else None
        )
        if self.role != expected_role:
            raise ValueError("D3 model role differs from operation category")
        if expected_role is not None:
            if not isinstance(
                self.role_config_ref, str
            ) or not self.role_config_ref.startswith("sha256:"):
                raise ValueError("D3 model call requires frozen role reference")
            _sha(self.role_config_ref[7:], "role_config_ref")
        elif self.role_config_ref is not None:
            raise ValueError("non-model D3 operation cannot use model role")


@dataclass(frozen=True, slots=True)
class D3OperationCompletion:
    operation_id: str
    finished_at: float
    result_kind: D3OperationResult
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
            raise ValueError("unsupported D3 operation result")
        _text(self.evidence_ref, "evidence_ref")
        _sha(self.evidence_sha256, "evidence_sha256")


@dataclass(frozen=True, slots=True)
class D3OperationRecord:
    start: D3OperationStart
    completion: D3OperationCompletion | None = None
    application_ref: str | None = None


@dataclass(frozen=True, slots=True)
class D3CommitMetadata:
    config: D3RunConfig | None = None
    turn_binding: D3TurnBinding | None = None
    intent_bindings: tuple[D3IntentBinding, ...] = ()
    plan_binding: D3PlanBinding | None = None
    plan_result: D3PlanResult | None = None
    outcomes: tuple[D3Outcome, ...] = ()

    def __post_init__(self) -> None:
        if not any(
            (
                self.config is not None,
                self.turn_binding is not None,
                self.intent_bindings,
                self.plan_binding is not None,
                self.plan_result is not None,
                self.outcomes,
            )
        ):
            raise ValueError("D3 metadata must contain a typed change")
        if self.config is not None and not isinstance(self.config, D3RunConfig):
            raise TypeError("invalid D3 run configuration")
        if self.turn_binding is not None and not isinstance(
            self.turn_binding, D3TurnBinding
        ):
            raise TypeError("invalid D3 Turn binding")
        if not isinstance(self.intent_bindings, tuple) or any(
            not isinstance(item, D3IntentBinding) for item in self.intent_bindings
        ):
            raise TypeError("invalid D3 Intent bindings")
        if len({item.intent_id for item in self.intent_bindings}) != len(
            self.intent_bindings
        ):
            raise ValueError("duplicate D3 Intent binding")
        if self.plan_binding is not None and not isinstance(
            self.plan_binding, D3PlanBinding
        ):
            raise TypeError("invalid D3 Plan binding")
        if self.plan_result is not None and not isinstance(
            self.plan_result, D3PlanResult
        ):
            raise TypeError("invalid D3 Plan result")
        if not isinstance(self.outcomes, tuple) or any(
            not isinstance(item, D3Outcome) for item in self.outcomes
        ):
            raise TypeError("invalid D3 outcomes")
        if len({(item.entity_kind, item.entity_id) for item in self.outcomes}) != len(
            self.outcomes
        ):
            raise ValueError("duplicate D3 outcome owner")


class D3RecoveryRequired(RuntimeError):
    pass


class D3BudgetExhausted(RuntimeError):
    pass


class D3OperationConflict(RuntimeError):
    pass
