"""Versioned, audited model proposals for the internal D3 chain."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, Protocol
from uuid import UUID

from agent_core.actions import (
    CompleteIntentAction,
    ProbeAction,
    parse_d2_action_envelope,
)
from agent_core.attachment_import import AttachmentImportReceipt
from agent_core.dual_llm_contracts import (
    D3OperationCompletion,
    D3OperationStart,
    D3PlanBinding,
    D3RecoveryRequired,
    D3RunConfig,
    D3TurnBinding,
)
from agent_core.modeling import (
    CancellationToken,
    IntentCandidate,
    ModelError,
    ModelTransport,
)
from agent_core.turn_contracts import D1RoleConfig
from agent_core.turn_modeling import digest, stable_id

D3ModelStage = Literal[
    "outer_turn",
    "inner_plan",
    "inner_size",
    "inner_sha256",
    "inner_complete",
]


class D3ProposalRejected(ValueError):
    def __init__(self, code: str, *, structural: bool = False) -> None:
        self.code = code
        self.structural = structural
        super().__init__(code)


def _fields(raw: object, required: set[str]) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != required:
        raise D3ProposalRejected("d3_proposal_fields_invalid", structural=True)
    return raw


@dataclass(frozen=True, slots=True)
class D3TurnProposal:
    turn_target: str
    attachment_index: int
    intents: tuple[IntentCandidate, ...]


def parse_d3_turn_proposal(raw: object) -> D3TurnProposal:
    value = _fields(raw, {"turn_target", "completion_rule", "intents"})
    target = value["turn_target"]
    if not isinstance(target, str) or not target.strip() or len(target.encode()) > 4096:
        raise D3ProposalRejected("d3_turn_target_invalid", structural=True)
    rule = _fields(value["completion_rule"], {"rule_id", "version", "parameters"})
    if (
        rule["rule_id"] != "attachment_copy_sha256"
        or type(rule["version"]) is not int
        or rule["version"] != 1
    ):
        raise D3ProposalRejected("d3_completion_rule_unregistered")
    parameters = _fields(rule["parameters"], {"attachment_index"})
    index = parameters["attachment_index"]
    if type(index) is not int or index not in {0, 1}:
        raise D3ProposalRejected("d3_attachment_index_invalid")
    candidates = value["intents"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 2:
        raise D3ProposalRejected("d3_intent_count_invalid")
    parsed = []
    for candidate in candidates:
        item = _fields(candidate, {"description", "intent_type"})
        if (
            not isinstance(item["description"], str)
            or item["intent_type"] is not None
            and not isinstance(item["intent_type"], str)
        ):
            raise D3ProposalRejected("d3_intent_structure_invalid", structural=True)
        try:
            parsed.append(IntentCandidate(item["description"], item["intent_type"]))
        except (TypeError, ValueError) as exc:
            raise D3ProposalRejected(
                "d3_intent_structure_invalid", structural=True
            ) from exc
    return D3TurnProposal(target, index, tuple(parsed))


@dataclass(frozen=True, slots=True)
class D3PlanProposal:
    template: Literal["size_then_sha256", "sha256_only"]
    observation_mode: Literal["new_observation", "reuse_saved_proof"]
    source_proof_id: str | None


def parse_d3_plan_proposal(raw: object) -> D3PlanProposal:
    value = _fields(raw, {"template", "observation_mode", "source_proof_id"})
    template, mode, proof_id = (
        value["template"],
        value["observation_mode"],
        value["source_proof_id"],
    )
    if template not in {"size_then_sha256", "sha256_only"} or mode not in {
        "new_observation",
        "reuse_saved_proof",
    }:
        raise D3ProposalRejected("d3_plan_template_invalid")
    if template == "size_then_sha256" and mode != "new_observation":
        raise D3ProposalRejected("d3_initial_plan_requires_observation")
    if mode == "reuse_saved_proof":
        if not isinstance(proof_id, str):
            raise D3ProposalRejected("d3_reused_proof_id_invalid")
        try:
            parsed = UUID(proof_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise D3ProposalRejected("d3_reused_proof_id_invalid") from exc
        if parsed.version not in {4, 5} or str(parsed) != proof_id:
            raise D3ProposalRejected("d3_reused_proof_id_invalid")
    elif proof_id is not None:
        raise D3ProposalRejected("d3_new_observation_cannot_select_proof")
    return D3PlanProposal(template, mode, proof_id)


@dataclass(frozen=True, slots=True)
class D3ActionProposal:
    action: ProbeAction | CompleteIntentAction
    review_requested: bool


def parse_d3_action(raw: object, stage: D3ModelStage) -> D3ActionProposal:
    try:
        candidate = raw
        step_indices: tuple[int, ...] = ()
        if (
            stage in {"inner_size", "inner_sha256"}
            and isinstance(raw, Mapping)
            and isinstance(raw.get("action"), Mapping)
            and raw["action"].get("kind") == "probe"
        ):
            action_payload = raw["action"]
            conditions = action_payload.get("conditions")
            if isinstance(conditions, list):
                indices = []
                for condition in conditions:
                    if not isinstance(condition, Mapping):
                        raise D3ProposalRejected(
                            "d3_condition_step_invalid", structural=True
                        )
                    index = condition.get("step_index")
                    if (
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or index not in {0, 1}
                    ):
                        raise D3ProposalRejected(
                            "d3_condition_step_invalid", structural=True
                        )
                    indices.append(index)
                step_indices = tuple(indices)
                candidate = {
                    **raw,
                    "action": {
                        **action_payload,
                        "conditions": [
                            {**condition, "step_index": 0} for condition in conditions
                        ],
                    },
                }
        envelope = parse_d2_action_envelope(candidate)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, D3ProposalRejected):
            raise
        raise D3ProposalRejected(
            "d3_action_structure_invalid", structural=True
        ) from exc
    action = envelope.action
    if step_indices and isinstance(action, ProbeAction):
        action = replace(
            action,
            conditions=tuple(
                replace(condition, step_index=index)
                for condition, index in zip(
                    action.conditions, step_indices, strict=True
                )
            ),
        )
    if stage in {"inner_size", "inner_sha256"} and not isinstance(action, ProbeAction):
        raise D3ProposalRejected("d3_probe_action_required")
    if stage == "inner_complete" and not isinstance(action, CompleteIntentAction):
        raise D3ProposalRejected("d3_complete_action_required")
    return D3ActionProposal(action, envelope.review_requested)


@dataclass(frozen=True, slots=True)
class D3OuterContext:
    goal: str
    attachments: tuple[AttachmentImportReceipt, ...]
    prior_turn_id: str | None
    prior_turn_reason: str | None


@dataclass(frozen=True, slots=True)
class D3PlanContext:
    turn_target: str
    turn_binding: D3TurnBinding
    intent_id: str
    prior_plan_ids: tuple[str, ...]
    reusable_proof_ids: tuple[str, ...]
    shared_artifacts: tuple[D3SharedArtifactContext, ...] = ()


@dataclass(frozen=True, slots=True)
class D3SharedArtifactContext:
    share_id: str
    artifact_version_id: str
    source_turn_id: str
    source_intent_id: str
    target_path: str
    sha256: str
    original_verification_status: Literal["verified", "unverified"]


@dataclass(frozen=True, slots=True)
class D3ActionContext:
    turn_target: str
    turn_binding: D3TurnBinding
    plan_binding: D3PlanBinding
    step_index: int | None
    fixed_script: str | None
    fixed_working_directory: str | None
    supporting_fact_ids: tuple[str, ...]
    shared_artifacts: tuple[D3SharedArtifactContext, ...] = ()


@dataclass(frozen=True, slots=True)
class D3ModelRequest:
    model_request_id: str
    task_run_id: str
    base_checkpoint_sequence: int
    role: Literal["outer_planning", "inner_action"]
    role_config_ref: str
    resource_ref: str
    stage: D3ModelStage
    context: D3OuterContext | D3PlanContext | D3ActionContext
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None

    @property
    def context_sha256(self) -> str:
        return digest(asdict(self.context))


@dataclass(frozen=True, slots=True)
class D3ModelEvidence:
    model_request_id: str
    task_run_id: str
    stage: D3ModelStage
    role_config_ref: str
    resource_ref: str
    base_checkpoint_sequence: int
    context_sha256: str
    correction_of_model_request_id: str | None
    outcome: Literal[
        "accepted", "invalid_structure", "rejected", "model_error", "stale"
    ]
    raw: object
    reason: str | None
    finished_at: float


@dataclass(frozen=True, slots=True)
class D3ModelRejection:
    model_request_id: str
    code: str
    recorded_at: float


class D3ModelEvidencePort(Protocol):
    def save(self, evidence: D3ModelEvidence) -> str: ...
    def read(self, model_request_id: str) -> D3ModelEvidence | None: ...
    def save_rejection(self, rejection: D3ModelRejection) -> None: ...
    def read_rejection(self, model_request_id: str) -> D3ModelRejection | None: ...


class D3ModelAdapterPort(ModelTransport[D3ModelRequest, object], Protocol):
    @property
    def role_config(self) -> D1RoleConfig: ...


class D3AuditedModel:
    def __init__(
        self,
        controller,
        ledger,
        evidence: D3ModelEvidencePort,
        adapter: D3ModelAdapterPort,
        config: D3RunConfig,
        clock,
    ) -> None:
        self.controller = controller
        self.ledger = ledger
        self.evidence = evidence
        self.adapter = adapter
        self.config = config
        self.clock = clock

    def _parse(self, request: D3ModelRequest, raw: object):
        if request.stage == "outer_turn":
            return parse_d3_turn_proposal(raw)
        if request.stage == "inner_plan":
            return parse_d3_plan_proposal(raw)
        return parse_d3_action(raw, request.stage)

    def _check(self, request: D3ModelRequest) -> None:
        expected = (
            self.config.outer_role
            if request.stage == "outer_turn"
            else self.config.inner_role
        )
        context_type = (
            D3OuterContext
            if request.stage == "outer_turn"
            else D3PlanContext
            if request.stage == "inner_plan"
            else D3ActionContext
        )
        if (
            self.ledger.load_d3_run_config(request.task_run_id) != self.config
            or self.adapter.role_config != expected
            or request.role != expected.role
            or request.role_config_ref != expected.role_config_ref
            or not isinstance(request.context, context_type)
            or request.task_run_id != self.controller.control_state.task_run.task_run_id
        ):
            raise D3RecoveryRequired("d3_model_configuration_or_context_changed")

    def _saved(
        self, request: D3ModelRequest
    ) -> tuple[D3ModelEvidence | None, object | None]:
        record = self.ledger.load_d3_operation(request.model_request_id)
        evidence = self.evidence.read(request.model_request_id)
        if record is None:
            if evidence is not None:
                raise D3RecoveryRequired("d3_model_evidence_without_call")
            return None, None
        start = record.start
        if (
            start.task_run_id != request.task_run_id
            or start.category
            != ("outer_model" if request.stage == "outer_turn" else "inner_model")
            or start.base_checkpoint_sequence != request.base_checkpoint_sequence
            or start.context_sha256 != request.context_sha256
            or start.resource_ref != request.resource_ref
            or start.role != request.role
            or start.role_config_ref != request.role_config_ref
        ):
            raise D3RecoveryRequired("d3_model_call_identity_changed")
        if evidence is None:
            raise D3RecoveryRequired("d3_model_call_outcome_unknown")
        if (
            evidence.model_request_id != request.model_request_id
            or evidence.task_run_id != request.task_run_id
            or evidence.stage != request.stage
            or evidence.role_config_ref != request.role_config_ref
            or evidence.resource_ref != request.resource_ref
            or evidence.base_checkpoint_sequence != request.base_checkpoint_sequence
            or evidence.context_sha256 != request.context_sha256
            or evidence.correction_of_model_request_id
            != request.correction_of_model_request_id
        ):
            raise D3RecoveryRequired("d3_model_evidence_identity_changed")
        if record.completion is None:
            self.ledger.complete_d3_operation(
                D3OperationCompletion(
                    request.model_request_id,
                    evidence.finished_at,
                    "completed" if evidence.outcome == "accepted" else "rejected",
                    request.model_request_id,
                    digest(asdict(evidence)),
                )
            )
        elif (
            record.completion.evidence_ref != request.model_request_id
            or record.completion.evidence_sha256 != digest(asdict(evidence))
            or (record.completion.result_kind == "completed")
            != (evidence.outcome == "accepted")
        ):
            raise D3RecoveryRequired("d3_model_completion_evidence_changed")
        return evidence, record

    async def _invoke(
        self, request: D3ModelRequest, timeout: float, token: CancellationToken
    ):
        finished = threading.Event()
        stopped = threading.Event()
        values = []

        class InvocationToken:
            @property
            def is_cancelled(self) -> bool:
                return stopped.is_set() or token.is_cancelled

        def invoke() -> None:
            try:
                values.append(
                    (True, self.adapter.invoke(request, timeout, InvocationToken()))
                )
            except BaseException as exc:
                values.append((False, exc))
            finally:
                finished.set()

        threading.Thread(target=invoke, daemon=True, name="d3-model-call").start()
        expires = asyncio.get_running_loop().time() + timeout
        try:
            while not finished.is_set():
                state = self.controller.control_state
                if (
                    token.is_cancelled
                    or state.task_run.status.value != "running"
                    or self.clock() >= state.task_run.deadline_at
                    or asyncio.get_running_loop().time() >= expires
                ):
                    raise D3RecoveryRequired("d3_model_call_outcome_unknown")
                await asyncio.sleep(0.025)
            succeeded, raw = values[0]
            if not succeeded:
                if isinstance(raw, BaseException):
                    raise D3RecoveryRequired("d3_model_call_outcome_unknown") from raw
                raise D3RecoveryRequired("d3_model_call_outcome_unknown")
            return raw
        finally:
            stopped.set()

    async def propose(self, request: D3ModelRequest, token: CancellationToken):
        self._check(request)
        current = request
        for attempt in range(2):
            evidence, _record = self._saved(current)
            if evidence is None:
                state = self.controller.control_state
                if (
                    token.is_cancelled
                    or state.task_run.status.value != "running"
                    or self.clock() >= state.task_run.deadline_at
                    or state.latest_checkpoint.sequence
                    != current.base_checkpoint_sequence
                ):
                    raise D3RecoveryRequired("d3_model_dispatch_stopped")
                self.ledger.begin_d3_operation(
                    D3OperationStart(
                        current.model_request_id,
                        current.task_run_id,
                        "outer_model"
                        if current.stage == "outer_turn"
                        else "inner_model",
                        self.clock(),
                        current.base_checkpoint_sequence,
                        current.context_sha256,
                        current.resource_ref,
                        current.role,
                        current.role_config_ref,
                    )
                )
                raw = await self._invoke(
                    current,
                    min(180.0, state.task_run.deadline_at - self.clock()),
                    token,
                )
                outcome: Literal[
                    "accepted", "invalid_structure", "rejected", "model_error", "stale"
                ] = "accepted"
                reason = None
                if isinstance(raw, ModelError):
                    outcome, reason, raw = (
                        "model_error",
                        raw.short_reason,
                        raw.to_dict(),
                    )
                else:
                    try:
                        self._parse(current, raw)
                    except D3ProposalRejected as exc:
                        outcome = "invalid_structure" if exc.structural else "rejected"
                        reason = exc.code
                state = self.controller.control_state
                if (
                    token.is_cancelled
                    or state.task_run.status.value != "running"
                    or self.clock() >= state.task_run.deadline_at
                    or state.latest_checkpoint.sequence
                    != current.base_checkpoint_sequence
                ):
                    outcome, reason = "stale", "d3_model_response_stale"
                evidence = D3ModelEvidence(
                    current.model_request_id,
                    current.task_run_id,
                    current.stage,
                    current.role_config_ref,
                    current.resource_ref,
                    current.base_checkpoint_sequence,
                    current.context_sha256,
                    current.correction_of_model_request_id,
                    outcome,
                    raw,
                    reason,
                    self.clock(),
                )
                checksum = self.evidence.save(evidence)
                self.ledger.complete_d3_operation(
                    D3OperationCompletion(
                        current.model_request_id,
                        evidence.finished_at,
                        "completed" if outcome == "accepted" else "rejected",
                        current.model_request_id,
                        checksum,
                    )
                )
            if evidence.outcome == "invalid_structure" and attempt == 0:
                current = replace(
                    current,
                    model_request_id=stable_id(
                        request.task_run_id, f"d3/{request.model_request_id}/correction"
                    ),
                    correction_of_model_request_id=current.model_request_id,
                    rejection_code=evidence.reason,
                )
                continue
            if evidence.outcome != "accepted":
                raise D3ProposalRejected(evidence.reason or evidence.outcome)
            rejection = self.evidence.read_rejection(current.model_request_id)
            if rejection is not None:
                raise D3ProposalRejected(rejection.code)
            self._check(current)
            return current.model_request_id, self._parse(current, evidence.raw)
        raise D3RecoveryRequired("d3_model_correction_unavailable")

    def reject(self, model_request_id: str, code: str) -> None:
        evidence = self.evidence.read(model_request_id)
        if evidence is None or evidence.outcome != "accepted":
            raise D3RecoveryRequired("d3_rejection_requires_known_model_result")
        self.evidence.save_rejection(
            D3ModelRejection(model_request_id, code, self.clock())
        )
