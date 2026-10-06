"""Versioned, audited model proposals for the internal D3 chain."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, Protocol
from uuid import UUID, uuid4

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
    D3PlanResult,
    D3RecoveryRequired,
    D3RunConfig,
    D3TurnBinding,
)
from agent_core.goal_candidates import D7AnswerCandidate
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
    "inner_candidate",
]

MAX_D3_CANDIDATE_VALUE_BYTES = 16 * 1024


class D3ProposalRejected(ValueError):
    def __init__(
        self,
        code: str,
        *,
        structural: bool = False,
        retryable: bool = True,
        model_request_id: str | None = None,
    ) -> None:
        self.code = code
        self.structural = structural
        self.retryable = retryable
        self.model_request_id = model_request_id
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
class D3CandidateFlagAction:
    value: str
    execution_id: str
    evidence_version: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class D3ActionProposal:
    action: ProbeAction | CompleteIntentAction | D3CandidateFlagAction
    review_requested: bool


def _candidate_text(value: object, *, code: str, nonblank: bool) -> str:
    if (
        not isinstance(value, str)
        or not value
        or (nonblank and not value.strip())
        or "\x00" in value
    ):
        raise D3ProposalRejected(code)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise D3ProposalRejected(code) from exc
    return value


def _parse_d3_candidate_action(raw: object) -> D3ActionProposal:
    envelope = _fields(raw, {"action", "review_requested"})
    review_requested = envelope["review_requested"]
    if type(review_requested) is not bool:
        raise D3ProposalRejected("d3_review_requested_invalid", structural=True)
    action = _fields(
        envelope["action"],
        {"kind", "value", "execution_id", "evidence_version", "source_ref"},
    )
    if action["kind"] != "candidate_flag":
        raise D3ProposalRejected("d3_candidate_action_required")

    value = _candidate_text(
        action["value"], code="d3_candidate_value_invalid", nonblank=False
    )
    if len(value.encode("utf-8")) > MAX_D3_CANDIDATE_VALUE_BYTES:
        raise D3ProposalRejected("d3_candidate_value_too_large")

    execution_id = _candidate_text(
        action["execution_id"], code="d3_candidate_execution_id_invalid", nonblank=True
    )
    try:
        parsed_execution_id = UUID(execution_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise D3ProposalRejected("d3_candidate_execution_id_invalid") from exc
    if parsed_execution_id.version != 4 or str(parsed_execution_id) != execution_id:
        raise D3ProposalRejected("d3_candidate_execution_id_invalid")

    evidence_version = action["evidence_version"]
    if (
        not isinstance(evidence_version, str)
        or len(evidence_version) != 64
        or any(character not in "0123456789abcdef" for character in evidence_version)
    ):
        raise D3ProposalRejected("d3_candidate_evidence_version_invalid")

    source_ref = _candidate_text(
        action["source_ref"], code="d3_candidate_source_ref_invalid", nonblank=True
    )
    return D3ActionProposal(
        D3CandidateFlagAction(value, execution_id, evidence_version, source_ref),
        review_requested,
    )


def parse_d3_action(raw: object, stage: D3ModelStage) -> D3ActionProposal:
    if stage == "inner_candidate":
        return _parse_d3_candidate_action(raw)
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
class D3CandidateContext:
    """Trusted provenance supplied to the protected candidate model stage."""

    turn_binding: D3TurnBinding
    plan_binding: D3PlanBinding
    execution_id: str
    plan_result: D3PlanResult

    def __post_init__(self) -> None:
        if not isinstance(self.turn_binding, D3TurnBinding):
            raise ValueError("D3 candidate requires a typed Turn binding")
        if not isinstance(self.plan_binding, D3PlanBinding):
            raise ValueError("D3 candidate requires a typed Plan binding")
        if not isinstance(self.plan_result, D3PlanResult):
            raise ValueError("D3 candidate requires a typed Plan result")
        try:
            parsed = UUID(self.execution_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("D3 candidate requires a trusted Execution ID") from exc
        if parsed.version != 4 or str(parsed) != self.execution_id:
            raise ValueError("D3 candidate requires a trusted Execution ID")
        if (
            self.turn_binding.task_run_id != self.plan_binding.task_run_id
            or self.turn_binding.turn_id != self.plan_binding.turn_id
            or self.plan_result.task_run_id != self.plan_binding.task_run_id
            or self.plan_result.plan_id != self.plan_binding.plan_id
            or self.plan_result.artifact_version_id
            != self.plan_binding.artifact_version_id
            or self.plan_result.outcome != "satisfied"
        ):
            raise ValueError("D3 candidate provenance binding is inconsistent")


@dataclass(frozen=True, slots=True)
class D3ModelRequest:
    model_request_id: str
    task_run_id: str
    base_checkpoint_sequence: int
    role: Literal["outer_planning", "inner_action"]
    role_config_ref: str
    resource_ref: str
    stage: D3ModelStage
    context: D3OuterContext | D3PlanContext | D3ActionContext | D3CandidateContext
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None
    process_facts: tuple[dict[str, object], ...] = ()

    @property
    def context_sha256(self) -> str:
        if self.process_facts:
            return digest({"context": asdict(self.context), "process_facts": self.process_facts})
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
    retryable: bool = False


class D3ModelEvidencePort(Protocol):
    def save(self, evidence: D3ModelEvidence) -> str: ...
    def read(self, model_request_id: str) -> D3ModelEvidence | None: ...
    def save_rejection(self, rejection: D3ModelRejection) -> None: ...
    def read_rejection(self, model_request_id: str) -> D3ModelRejection | None: ...


class D3ProtectedCandidatePort(Protocol):
    def save_pending(
        self, candidate: object, value: str, model_request_id: str
    ) -> dict[str, object]: ...

    def load_pending_for_request(
        self, model_request_id: str
    ) -> tuple[object, dict[str, object]] | None: ...


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
            else D3CandidateContext
            if request.stage == "inner_candidate"
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

    async def propose_candidate(
        self,
        request: D3ModelRequest,
        token: CancellationToken,
        protected: D3ProtectedCandidatePort,
    ):
        """Run the candidate stage with body-only protected persistence.

        The model call is started once under the normal D3/D4 ``inner_model``
        ledger. A missing protected result after a started call is unresolved
        and is never dispatched again. A protected result is sufficient to
        repair the ordinary, body-free D3 evidence and later Controller commit.
        """
        if request.stage != "inner_candidate" or not isinstance(
            request.context, D3CandidateContext
        ):
            raise D3RecoveryRequired("d3_candidate_request_invalid")
        if not callable(getattr(protected, "save_pending", None)) or not callable(
            getattr(protected, "load_pending_for_request", None)
        ):
            raise D3RecoveryRequired("d3_candidate_protected_store_required")
        self._check(request)

        context = request.context
        expected_start = D3OperationStart(
            request.model_request_id,
            request.task_run_id,
            "inner_model",
            self.clock(),
            request.base_checkpoint_sequence,
            request.context_sha256,
            request.resource_ref,
            request.role,
            request.role_config_ref,
        )
        record = self.ledger.load_d3_operation(request.model_request_id)
        if record is not None:
            start = record.start
            if (
                start.task_run_id != expected_start.task_run_id
                or start.category != expected_start.category
                or start.base_checkpoint_sequence
                != expected_start.base_checkpoint_sequence
                or start.context_sha256 != expected_start.context_sha256
                or start.resource_ref != expected_start.resource_ref
                or start.role != expected_start.role
                or start.role_config_ref != expected_start.role_config_ref
            ):
                raise D3RecoveryRequired("d3_candidate_call_identity_changed")

        pending = None
        if record is None:
            pending = protected.load_pending_for_request(request.model_request_id)
            if pending is not None:
                raise D3RecoveryRequired("d3_candidate_result_without_started_call")
        elif record.completion is None:
            pending = protected.load_pending_for_request(request.model_request_id)
            if pending is None:
                raise D3RecoveryRequired("d3_candidate_model_call_outcome_unknown")
        if record is None:
            state = self.controller.control_state
            if (
                token.is_cancelled
                or state.task_run.status.value != "running"
                or self.clock() >= state.task_run.deadline_at
                or state.latest_checkpoint.sequence
                != request.base_checkpoint_sequence
            ):
                raise D3RecoveryRequired("d3_candidate_dispatch_stopped")
            self.ledger.begin_d3_operation(expected_start)
            raw = await self._invoke(
                request,
                min(180.0, state.task_run.deadline_at - self.clock()),
                token,
            )
            try:
                proposal = parse_d3_action(raw, "inner_candidate")
                action = proposal.action
                if not isinstance(action, D3CandidateFlagAction):
                    raise D3ProposalRejected("d3_candidate_action_required")
                if proposal.review_requested:
                    raise D3ProposalRejected("d3_candidate_review_not_supported")
                if (
                    action.execution_id != context.execution_id
                    or action.evidence_version != context.plan_result.evidence_sha256
                    or action.source_ref != context.plan_binding.artifact_version_id
                ):
                    raise D3ProposalRejected("d3_candidate_binding_rejected")
            except D3ProposalRejected as exc:
                safe_raw = {
                    "outcome": "rejected",
                    "reason": exc.code,
                    "structural": exc.structural,
                    "retryable": exc.retryable,
                }
                evidence = D3ModelEvidence(
                    request.model_request_id,
                    request.task_run_id,
                    request.stage,
                    request.role_config_ref,
                    request.resource_ref,
                    request.base_checkpoint_sequence,
                    request.context_sha256,
                    request.correction_of_model_request_id,
                    "invalid_structure" if exc.structural else "rejected",
                    safe_raw,
                    exc.code,
                    self.clock(),
                )
                checksum = self.evidence.save(evidence)
                self.ledger.complete_d3_operation(
                    D3OperationCompletion(
                        request.model_request_id,
                        evidence.finished_at,
                        "rejected",
                        request.model_request_id,
                        checksum,
                    )
                )
                raise D3ProposalRejected(
                    exc.code,
                    structural=exc.structural,
                    retryable=exc.retryable,
                    model_request_id=request.model_request_id,
                ) from exc

            candidate = D7AnswerCandidate(
                task_run_id=request.task_run_id,
                candidate_id=str(uuid4()),
                turn_id=context.plan_binding.turn_id,
                intent_id=context.plan_binding.intent_id,
                plan_id=context.plan_binding.plan_id,
                execution_id=context.execution_id,
                evidence_version=context.plan_result.evidence_sha256,
                source_ref=context.plan_binding.artifact_version_id,
            )
            metadata = protected.save_pending(
                candidate, action.value, request.model_request_id
            )
            pending = (candidate, metadata)
            safe_raw = self._candidate_safe_raw(candidate, metadata)
            evidence = D3ModelEvidence(
                request.model_request_id,
                request.task_run_id,
                request.stage,
                request.role_config_ref,
                request.resource_ref,
                request.base_checkpoint_sequence,
                request.context_sha256,
                request.correction_of_model_request_id,
                "accepted",
                safe_raw,
                None,
                self.clock(),
            )
            checksum = self.evidence.save(evidence)
            self.ledger.complete_d3_operation(
                D3OperationCompletion(
                    request.model_request_id,
                    evidence.finished_at,
                    "completed",
                    request.model_request_id,
                    checksum,
                )
            )
            record = self.ledger.load_d3_operation(request.model_request_id)
        elif record.completion is None:
            candidate, metadata = pending
            self._validate_pending_candidate(request, candidate, metadata)
            safe_raw = self._candidate_safe_raw(candidate, metadata)
            evidence = D3ModelEvidence(
                request.model_request_id,
                request.task_run_id,
                request.stage,
                request.role_config_ref,
                request.resource_ref,
                request.base_checkpoint_sequence,
                request.context_sha256,
                request.correction_of_model_request_id,
                "accepted",
                safe_raw,
                None,
                self.clock(),
            )
            checksum = self.evidence.save(evidence)
            self.ledger.complete_d3_operation(
                D3OperationCompletion(
                    request.model_request_id,
                    evidence.finished_at,
                    "completed",
                    request.model_request_id,
                    checksum,
                )
            )
            record = self.ledger.load_d3_operation(request.model_request_id)

        if record is None or record.completion is None:
            raise D3RecoveryRequired("d3_candidate_call_completion_missing")
        evidence = self.evidence.read(request.model_request_id)
        if evidence is None:
            raise D3RecoveryRequired("d3_candidate_call_evidence_missing")
        if record.completion.result_kind == "rejected":
            rejection = evidence.raw
            if (
                evidence.outcome not in {"rejected", "invalid_structure"}
                or evidence.reason is None
                or not isinstance(rejection, Mapping)
                or set(rejection)
                != {"outcome", "reason", "structural", "retryable"}
                or rejection.get("outcome") != "rejected"
                or rejection.get("reason") != evidence.reason
                or type(rejection.get("structural")) is not bool
                or type(rejection.get("retryable")) is not bool
                or evidence.outcome
                != ("invalid_structure" if rejection.get("structural") else "rejected")
                or evidence.model_request_id != request.model_request_id
                or evidence.task_run_id != request.task_run_id
                or evidence.stage != request.stage
                or evidence.role_config_ref != request.role_config_ref
                or evidence.resource_ref != request.resource_ref
                or evidence.base_checkpoint_sequence
                != request.base_checkpoint_sequence
                or evidence.context_sha256 != request.context_sha256
                or evidence.correction_of_model_request_id
                != request.correction_of_model_request_id
                or record.completion.evidence_ref != request.model_request_id
                or record.completion.evidence_sha256 != digest(asdict(evidence))
            ):
                raise D3RecoveryRequired("d3_candidate_rejection_evidence_changed")
            raise D3ProposalRejected(
                evidence.reason,
                structural=rejection["structural"],
                retryable=rejection["retryable"],
                model_request_id=request.model_request_id,
            )
        pending = protected.load_pending_for_request(request.model_request_id)
        if record.completion.result_kind != "completed" or pending is None:
            raise D3RecoveryRequired("d3_candidate_protected_result_missing")
        candidate, metadata = pending
        self._validate_pending_candidate(request, candidate, metadata)
        if (
            evidence.outcome != "accepted"
            or evidence.model_request_id != request.model_request_id
            or evidence.task_run_id != request.task_run_id
            or evidence.stage != request.stage
            or evidence.role_config_ref != request.role_config_ref
            or evidence.resource_ref != request.resource_ref
            or evidence.base_checkpoint_sequence != request.base_checkpoint_sequence
            or evidence.context_sha256 != request.context_sha256
            or evidence.correction_of_model_request_id
            != request.correction_of_model_request_id
            or record.completion.result_kind != "completed"
            or evidence.raw != self._candidate_safe_raw(candidate, metadata)
            or record.completion.evidence_ref != request.model_request_id
            or record.completion.evidence_sha256 != digest(asdict(evidence))
        ):
            raise D3RecoveryRequired("d3_candidate_completion_evidence_changed")
        return request.model_request_id, candidate

    @staticmethod
    def _candidate_safe_raw(candidate: object, metadata: Mapping[str, object]):
        if not isinstance(metadata, Mapping):
            raise D3RecoveryRequired("d3_candidate_protected_metadata_invalid")
        body_sha256 = metadata.get("body_sha256")
        byte_size = metadata.get("byte_size")
        if (
            not isinstance(body_sha256, str)
            or len(body_sha256) != 64
            or any(char not in "0123456789abcdef" for char in body_sha256)
            or type(byte_size) is not int
            or byte_size <= 0
        ):
            raise D3RecoveryRequired("d3_candidate_protected_metadata_invalid")
        return {
            "candidate_id": getattr(candidate, "candidate_id", None),
            "evidence_version": getattr(candidate, "evidence_version", None),
            "execution_id": getattr(candidate, "execution_id", None),
            "body_sha256": body_sha256,
            "byte_size": byte_size,
        }

    @staticmethod
    def _validate_pending_candidate(
        request: D3ModelRequest,
        candidate: object,
        metadata: Mapping[str, object],
    ) -> None:
        context = request.context
        if not isinstance(context, D3CandidateContext):
            raise D3RecoveryRequired("d3_candidate_context_invalid")
        if (
            not isinstance(candidate, D7AnswerCandidate)
            or candidate.task_run_id != request.task_run_id
            or candidate.turn_id != context.plan_binding.turn_id
            or candidate.intent_id != context.plan_binding.intent_id
            or candidate.plan_id != context.plan_binding.plan_id
            or candidate.execution_id != context.execution_id
            or candidate.evidence_version != context.plan_result.evidence_sha256
            or candidate.source_ref != context.plan_binding.artifact_version_id
            or metadata.get("model_request_id") != request.model_request_id
            or metadata.get("candidate_id") != candidate.candidate_id
            or metadata.get("task_run_id") != request.task_run_id
            or metadata.get("execution_id") != context.execution_id
            or metadata.get("evidence_version") != context.plan_result.evidence_sha256
        ):
            raise D3RecoveryRequired("d3_candidate_protected_binding_changed")

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

    async def propose(
        self,
        request: D3ModelRequest,
        token: CancellationToken,
        *,
        refresh: Callable[[D3ModelRequest], D3ModelRequest] | None = None,
        validate: Callable[[D3ModelRequest, object], None] | None = None,
    ):
        if request.stage == "inner_candidate":
            raise D3RecoveryRequired("d3_candidate_protected_store_required")
        self._check(request)
        d4_enabled = self.config.d4_budget is not None
        if d4_enabled and (refresh is None or validate is None):
            raise D3RecoveryRequired("d4_model_refresh_and_validation_required")
        current = request
        d4_refinement = d4_enabled and refresh is not None
        refinement_used = False

        while True:
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

            rejection_code = evidence.reason
            retryable = False
            if evidence.outcome == "accepted":
                rejection = self.evidence.read_rejection(current.model_request_id)
                rejection_code = rejection.code if rejection is not None else None
                retryable = rejection.retryable if rejection is not None else False
                if rejection_code is None:
                    self._check(current)
                    proposal = self._parse(current, evidence.raw)
                    if validate is not None:
                        try:
                            validate(current, proposal)
                        except D3ProposalRejected as exc:
                            self.reject(
                                current.model_request_id,
                                exc.code,
                                retryable=exc.retryable,
                            )
                            rejection_code = exc.code
                            retryable = exc.retryable
                    if rejection_code is None:
                        return current.model_request_id, proposal
            elif evidence.outcome in {"invalid_structure", "rejected"}:
                try:
                    self._parse(current, evidence.raw)
                except D3ProposalRejected as exc:
                    retryable = exc.retryable
            else:
                raise D3ProposalRejected(
                    rejection_code or evidence.outcome,
                    model_request_id=current.model_request_id,
                )

            legacy_structural_correction = (
                not d4_refinement
                and evidence.outcome == "invalid_structure"
                and current.correction_of_model_request_id is None
            )
            can_refine = (
                (
                    d4_refinement
                    and not refinement_used
                    and refresh is not None
                    and retryable
                )
                or legacy_structural_correction
            ) and (
                self.clock() < self.controller.control_state.task_run.deadline_at
                and not token.is_cancelled
                and self.controller.control_state.task_run.status.value == "running"
            )
            if can_refine:
                if d4_refinement:
                    if refresh is None:
                        raise D3RecoveryRequired("d4_model_refresh_unavailable")
                    refreshed = refresh(request)
                else:
                    refreshed = request
                if (
                    not isinstance(refreshed, D3ModelRequest)
                    or refreshed.task_run_id != request.task_run_id
                    or refreshed.stage != request.stage
                    or refreshed.role != request.role
                    or refreshed.role_config_ref != request.role_config_ref
                    or refreshed.resource_ref != request.resource_ref
                    or refreshed.correction_of_model_request_id is not None
                ):
                    raise D3RecoveryRequired("d3_refreshed_request_scope_changed")
                refinement_id = stable_id(
                    request.task_run_id,
                    f"d3/{request.model_request_id}/correction",
                )
                prior_refinement = self.ledger.load_d3_operation(refinement_id)
                current = replace(
                    refreshed,
                    model_request_id=refinement_id,
                    base_checkpoint_sequence=(
                        prior_refinement.start.base_checkpoint_sequence
                        if prior_refinement is not None
                        else refreshed.base_checkpoint_sequence
                    ),
                    correction_of_model_request_id=current.model_request_id,
                    rejection_code=rejection_code,
                )
                self._check(current)
                if (
                    prior_refinement is None
                    and current.base_checkpoint_sequence
                    != self.controller.control_state.latest_checkpoint.sequence
                ):
                    raise D3RecoveryRequired("d3_refreshed_request_checkpoint_stale")
                refinement_used = True
                continue
            if evidence.outcome == "accepted" and rejection_code is not None:
                raise D3ProposalRejected(
                    rejection_code, model_request_id=current.model_request_id
                )
            if evidence.outcome != "accepted":
                raise D3ProposalRejected(
                    rejection_code or evidence.outcome,
                    model_request_id=current.model_request_id,
                )
            raise D3RecoveryRequired("d3_model_refinement_unavailable")

    def reject(
        self, model_request_id: str, code: str, *, retryable: bool = False
    ) -> None:
        evidence = self.evidence.read(model_request_id)
        if evidence is None or evidence.outcome != "accepted":
            raise D3RecoveryRequired("d3_rejection_requires_known_model_result")
        self.evidence.save_rejection(
            D3ModelRejection(model_request_id, code, self.clock(), retryable)
        )
