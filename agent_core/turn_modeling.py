"""Provider-neutral proposals for the confirmed D1 single-Turn contract."""

from __future__ import annotations

import asyncio
import hashlib
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, Protocol
from uuid import UUID

from agent_core.actions import (
    CompleteIntentAction,
    ProbeAction,
    parse_d2_action_envelope,
    parse_t6_action,
)
from agent_core.attachment_import import AttachmentImportReceipt
from agent_core.control_store import canonical_json
from agent_core.modeling import (
    CancellationToken,
    IntentCandidate,
    ModelError,
    ModelTransport,
)
from agent_core.turn_contracts import (
    D1Category,
    D1OperationCompletion,
    D1OperationStart,
    D1RecoveryRequired,
    D1ResultKind,
    D1RoleConfig,
    D1TurnBinding,
)

D1ModelOutcome = Literal[
    "accepted", "invalid_structure", "rejected", "model_error", "stale"
]


def stable_id(task_run_id: str, name: str) -> str:
    value = bytearray(
        hashlib.sha256(f"qinyuan/d1/v1/{task_run_id}/{name}".encode()).digest()[:16]
    )
    value[6] = (value[6] & 15) | 64
    value[8] = (value[8] & 63) | 128
    return str(UUID(bytes=bytes(value)))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentCopySha256Parameters:
    attachment_index: int


@dataclass(frozen=True, slots=True)
class AttachmentCopySha256Rule:
    parameters: AttachmentCopySha256Parameters
    rule_id: str = "attachment_copy_sha256"
    version: int = 1


@dataclass(frozen=True, slots=True)
class TurnProposal:
    turn_target: str
    completion_rule: AttachmentCopySha256Rule
    intents: tuple[IntentCandidate, ...]


class D1ProposalRejected(ValueError):
    def __init__(self, code: str, *, structural: bool = False) -> None:
        self.code = code
        self.structural = structural
        super().__init__(code)


def _fields(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise D1ProposalRejected("proposal_fields_invalid", structural=True)
    return value


def parse_turn_proposal(raw: object) -> TurnProposal:
    item = _fields(raw, {"turn_target", "completion_rule", "intents"})
    target = item["turn_target"]
    if not isinstance(target, str) or not target.strip() or len(target.encode()) > 4096:
        raise D1ProposalRejected("turn_target_invalid", structural=True)
    rule = _fields(item["completion_rule"], {"rule_id", "version", "parameters"})
    if (
        rule["rule_id"] != "attachment_copy_sha256"
        or type(rule["version"]) is not int
        or rule["version"] != 1
    ):
        raise D1ProposalRejected("completion_rule_unregistered")
    parameters = _fields(rule["parameters"], {"attachment_index"})
    index = parameters["attachment_index"]
    if type(index) is not int or index != 0:
        raise D1ProposalRejected("attachment_not_authorized")
    candidates = item["intents"]
    if not isinstance(candidates, list):
        raise D1ProposalRejected("intent_structure_invalid", structural=True)
    if len(candidates) != 1:
        raise D1ProposalRejected("single_intent_required")
    candidate = _fields(candidates[0], {"description", "intent_type"})
    description, intent_type = candidate["description"], candidate["intent_type"]
    if (
        not isinstance(description, str)
        or intent_type is not None
        and not isinstance(intent_type, str)
    ):
        raise D1ProposalRejected("intent_structure_invalid", structural=True)
    try:
        intent = IntentCandidate(description, intent_type)
    except (TypeError, ValueError) as exc:
        raise D1ProposalRejected("intent_structure_invalid", structural=True) from exc
    return TurnProposal(
        target,
        AttachmentCopySha256Rule(AttachmentCopySha256Parameters(index)),
        (intent,),
    )


@dataclass(frozen=True, slots=True)
class D1OuterContext:
    goal: str
    attachment: AttachmentImportReceipt


@dataclass(frozen=True, slots=True)
class D1InnerContext:
    turn_target: str
    binding: D1TurnBinding
    fixed_script: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class D1ModelRequest:
    model_request_id: str
    task_run_id: str
    base_checkpoint_sequence: int
    context_sha256: str
    role: Literal["outer_planning", "inner_action"]
    role_config_ref: str
    registration_mutation_id: str
    stage: Literal["outer", "probe", "complete"]
    context: D1OuterContext | D1InnerContext
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None


class D1ModelAdapterPort(ModelTransport[D1ModelRequest, object], Protocol):
    @property
    def role_config(self) -> D1RoleConfig: ...


@dataclass(frozen=True, slots=True)
class D1ModelEnvelope:
    model_request_id: str
    task_run_id: str
    base_checkpoint_sequence: int
    context_sha256: str
    role: Literal["outer_planning", "inner_action"]
    role_config_ref: str
    registration_mutation_id: str
    stage: Literal["outer", "probe", "complete"]
    correction_of_model_request_id: str | None
    rejection_code: str | None

    def __post_init__(self) -> None:
        categories: dict[str, D1Category] = {
            "outer": "outer_model",
            "probe": "inner_probe_model",
            "complete": "inner_complete_model",
        }
        category = categories.get(self.stage)
        if category is None:
            raise ValueError("unsupported_model_stage")
        D1OperationStart(
            self.model_request_id,
            self.task_run_id,
            category,
            0.0,
            self.base_checkpoint_sequence,
            self.context_sha256,
            self.role,
            self.role_config_ref,
            self.registration_mutation_id,
        )
        if self.correction_of_model_request_id is not None:
            UUID(self.correction_of_model_request_id)
            if not isinstance(self.rejection_code, str) or not self.rejection_code:
                raise ValueError("correction_requires_rejection_source")
        elif self.rejection_code is not None:
            raise ValueError("initial_request_has_no_rejection_source")

    @classmethod
    def from_request(cls, request: D1ModelRequest) -> D1ModelEnvelope:
        return cls(
            **{name: getattr(request, name) for name in cls.__dataclass_fields__}
        )


@dataclass(frozen=True, slots=True)
class D1ModelEvidence:
    model_request_id: str
    finished_at: float
    outcome: D1ModelOutcome
    raw: object
    rejection_code: str | None
    envelope: D1ModelEnvelope

    def __post_init__(self) -> None:
        if (
            not isinstance(self.envelope, D1ModelEnvelope)
            or self.envelope.model_request_id != self.model_request_id
            or self.outcome
            not in {"accepted", "invalid_structure", "rejected", "model_error", "stale"}
            or isinstance(self.finished_at, bool)
            or not isinstance(self.finished_at, (int, float))
            or not math.isfinite(self.finished_at)
        ):
            raise ValueError("invalid_model_evidence")


@dataclass(frozen=True, slots=True)
class D1ApplicationRejection:
    model_request_id: str
    task_run_id: str
    evidence_sha256: str
    checkpoint_sequence: int
    rejected_at: float
    rejection_code: str

    def __post_init__(self) -> None:
        if (
            type(self.checkpoint_sequence) is not int
            or self.checkpoint_sequence < 1
            or isinstance(self.rejected_at, bool)
            or not isinstance(self.rejected_at, (int, float))
            or not math.isfinite(self.rejected_at)
            or not isinstance(self.rejection_code, str)
            or not self.rejection_code.strip()
        ):
            raise ValueError("invalid_application_rejection")


class D1ModelEvidencePort(Protocol):
    def save(self, evidence: D1ModelEvidence) -> str: ...
    def read(self, model_request_id: str) -> D1ModelEvidence | None: ...
    def save_application_rejection(self, rejection: D1ApplicationRejection) -> None: ...
    def read_application_rejection(
        self, model_request_id: str
    ) -> D1ApplicationRejection | None: ...
    def save_dispatch_rejection(self, rejection: D1ApplicationRejection) -> None: ...
    def read_dispatch_rejection(
        self, model_request_id: str
    ) -> D1ApplicationRejection | None: ...


class D1AuditedModel:
    """One recorded provider call per ID; only structural errors get a correction."""

    def __init__(
        self,
        controller,
        ledger,
        evidence: D1ModelEvidencePort,
        adapter: D1ModelAdapterPort,
        config: D1RoleConfig,
        clock: Callable[[], float],
        d2_action_envelope: bool = False,
    ) -> None:
        self.controller = controller
        self.ledger = ledger
        self.evidence = evidence
        self.adapter = adapter
        self.config = config
        self.clock = clock
        self.d2_action_envelope = d2_action_envelope

    def _parse(self, raw: object, stage: str):
        if stage == "outer":
            return parse_turn_proposal(raw)
        try:
            action = (
                parse_d2_action_envelope(raw).action
                if self.d2_action_envelope
                else parse_t6_action(raw)
            )
        except (TypeError, ValueError) as exc:
            raise D1ProposalRejected(
                "action_structure_invalid", structural=True
            ) from exc
        if stage == "probe" and not isinstance(action, ProbeAction):
            raise D1ProposalRejected("probe_phase_action_rejected")
        if stage == "complete" and not isinstance(action, CompleteIntentAction):
            raise D1ProposalRejected("complete_phase_action_rejected")
        return action

    def review_requested(self, model_request_id: str) -> bool:
        """Read the persisted D2 wrapper flag without calling the model."""
        if not self.d2_action_envelope:
            return False
        evidence = self.evidence.read(model_request_id)
        if evidence is None or evidence.outcome != "accepted":
            raise D1RecoveryRequired("d2_action_evidence_missing")
        try:
            return parse_d2_action_envelope(evidence.raw).review_requested
        except (TypeError, ValueError) as exc:
            raise D1RecoveryRequired("d2_action_envelope_unavailable") from exc

    def _configuration(self, request: D1ModelRequest) -> None:
        try:
            actual = self.adapter.role_config
        except (AttributeError, ValueError, TypeError) as exc:
            raise D1RecoveryRequired("adapter_role_configuration_unavailable") from exc
        if actual != self.config:
            raise D1RecoveryRequired("adapter_role_configuration_changed")
        config = self.ledger.load_d1_run_config(request.task_run_id)
        expected = (
            None
            if config is None
            else (config.outer_role if request.stage == "outer" else config.inner_role)
        )
        expected_role = "outer_planning" if request.stage == "outer" else "inner_action"
        if (
            request.stage not in {"outer", "probe", "complete"}
            or request.role != expected_role
            or expected != self.config
            or request.role != self.config.role
            or request.role_config_ref != self.config.role_config_ref
        ):
            raise D1RecoveryRequired("role_configuration_changed")
        state = self.controller.control_state
        expected_context = (
            D1OuterContext if request.stage == "outer" else D1InnerContext
        )
        if (
            request.task_run_id != state.task_run.task_run_id
            or not isinstance(request.context, expected_context)
            or digest(asdict(request.context)) != request.context_sha256
        ):
            raise D1RecoveryRequired("model_context_changed")
        if request.stage != "outer":
            if not isinstance(request.context, D1InnerContext):
                raise D1RecoveryRequired("model_context_changed")
            binding = request.context.binding
            if (
                binding.task_run_id != request.task_run_id
                or self.ledger.load_d1_turn_binding(binding.turn_id) != binding
            ):
                raise D1RecoveryRequired("model_binding_changed")

    @staticmethod
    def _category(stage: str) -> D1Category:
        categories: dict[str, D1Category] = {
            "outer": "outer_model",
            "probe": "inner_probe_model",
            "complete": "inner_complete_model",
        }
        return categories[stage]

    def _saved(self, request: D1ModelRequest):
        saved = self.ledger.load_d1_operation(request.model_request_id)
        evidence = self.evidence.read(request.model_request_id)
        if saved is None:
            if evidence is not None:
                raise D1RecoveryRequired("model_evidence_without_admission")
            return None, None
        start = saved.start
        if (
            start.operation_id,
            start.task_run_id,
            start.category,
            start.base_checkpoint_sequence,
            start.context_sha256,
            start.role,
            start.role_config_ref,
            start.resource_ref,
        ) != (
            request.model_request_id,
            request.task_run_id,
            self._category(request.stage),
            request.base_checkpoint_sequence,
            request.context_sha256,
            request.role,
            request.role_config_ref,
            request.registration_mutation_id,
        ) or evidence is None:
            raise D1RecoveryRequired("model_request_envelope_mismatch")
        if evidence.envelope != D1ModelEnvelope.from_request(request):
            raise D1RecoveryRequired("model_request_envelope_mismatch")
        if request.correction_of_model_request_id is not None:
            parent_request = replace(
                request,
                model_request_id=request.correction_of_model_request_id,
                correction_of_model_request_id=None,
                rejection_code=None,
            )
            parent_record, parent_evidence = self._saved(parent_request)
            if (
                parent_record is None
                or parent_evidence is None
                or parent_evidence.outcome != "invalid_structure"
                or parent_evidence.rejection_code != request.rejection_code
                or parent_record.application_ref is not None
            ):
                raise D1RecoveryRequired("correction_source_unavailable")
        checksum = digest(asdict(evidence))
        result_kind = self._result_kind(evidence.outcome)
        if saved.completion is None:
            saved = self.ledger.complete_d1_operation(
                D1OperationCompletion(
                    request.model_request_id,
                    evidence.finished_at,
                    result_kind,
                    request.model_request_id,
                    checksum,
                )
            )
        elif (
            saved.completion.operation_id != request.model_request_id
            or saved.completion.finished_at != evidence.finished_at
            or saved.completion.evidence_ref != request.model_request_id
            or saved.completion.result_kind != result_kind
            or saved.completion.evidence_sha256 != checksum
        ):
            raise D1RecoveryRequired("model_evidence_mismatch")
        rejection = self.evidence.read_application_rejection(request.model_request_id)
        if rejection is not None:
            if (
                rejection.task_run_id != request.task_run_id
                or rejection.evidence_sha256 != checksum
            ):
                raise D1RecoveryRequired("application_rejection_mismatch")
            raise D1ProposalRejected(rejection.rejection_code)
        return saved, evidence

    @staticmethod
    def _result_kind(outcome: str) -> D1ResultKind:
        results: dict[str, D1ResultKind] = {
            "accepted": "completed",
            "invalid_structure": "rejected",
            "rejected": "rejected",
            "model_error": "failed",
            "stale": "stale",
        }
        return results[outcome]

    def reject_application(self, model_request_id: str, code: str) -> None:
        saved = self.ledger.load_d1_operation(model_request_id)
        evidence = self.evidence.read(model_request_id)
        if (
            saved is None
            or evidence is None
            or saved.completion is None
            or saved.completion.result_kind != "completed"
            or saved.application_ref is not None
            or saved.start.task_run_id
            != self.controller.control_state.task_run.task_run_id
            or saved.completion.evidence_sha256 != digest(asdict(evidence))
        ):
            raise D1RecoveryRequired("application_rejection_source_unavailable")
        existing = self.evidence.read_application_rejection(model_request_id)
        if existing is not None:
            if existing.rejection_code != code or existing.evidence_sha256 != digest(
                asdict(evidence)
            ):
                raise D1RecoveryRequired("application_rejection_conflict")
            return
        self.evidence.save_application_rejection(
            D1ApplicationRejection(
                model_request_id,
                saved.start.task_run_id,
                digest(asdict(evidence)),
                self.controller.control_state.latest_checkpoint.sequence,
                self.clock(),
                code,
            )
        )

    def reject_dispatch(self, model_request_id: str, code: str) -> None:
        """Persist a trusted pre-dispatch denial without changing the proposal application."""
        saved = self.ledger.load_d1_operation(model_request_id)
        evidence = self.evidence.read(model_request_id)
        if (
            saved is None
            or evidence is None
            or saved.completion is None
            or saved.completion.result_kind != "completed"
            or saved.start.task_run_id
            != self.controller.control_state.task_run.task_run_id
            or saved.completion.evidence_sha256 != digest(asdict(evidence))
            or not isinstance(code, str)
            or not code.strip()
        ):
            raise D1RecoveryRequired("dispatch_rejection_source_unavailable")
        existing = self.evidence.read_dispatch_rejection(model_request_id)
        if existing is not None:
            if existing.rejection_code != code or existing.evidence_sha256 != digest(
                asdict(evidence)
            ):
                raise D1RecoveryRequired("dispatch_rejection_conflict")
            return
        self.evidence.save_dispatch_rejection(
            D1ApplicationRejection(
                model_request_id,
                saved.start.task_run_id,
                digest(asdict(evidence)),
                self.controller.control_state.latest_checkpoint.sequence,
                self.clock(),
                code,
            )
        )

    def read_saved(
        self,
        model_request_id: str,
        stage: Literal["outer", "probe", "complete"],
        context: D1OuterContext | D1InnerContext,
        registration_mutation_id: str,
        token: CancellationToken,
    ):
        """Recheck an original request and its committed application without dispatch."""
        evidence = self.evidence.read(model_request_id)
        if evidence is None:
            raise D1RecoveryRequired("model_result_unknown_or_missing")
        envelope = evidence.envelope
        request = D1ModelRequest(**asdict(envelope), context=context)
        if (
            envelope.model_request_id != model_request_id
            or envelope.stage != stage
            or envelope.registration_mutation_id != registration_mutation_id
            or digest(asdict(context)) != envelope.context_sha256
        ):
            raise D1RecoveryRequired("model_request_envelope_mismatch")
        self._configuration(request)
        saved, evidence = self._saved(request)
        if evidence.outcome != "accepted":
            raise D1ProposalRejected(evidence.rejection_code or evidence.outcome)
        state = self.controller.control_state
        if (
            token.is_cancelled
            or state.task_run.status.value != "running"
            or self.clock() >= state.task_run.deadline_at
        ):
            raise D1RecoveryRequired("saved_model_result_stale")
        action = self._parse(evidence.raw, stage)
        if state.latest_checkpoint.sequence != envelope.base_checkpoint_sequence and (
            saved.application_ref is None
            or not self._application_matches(request, action, saved.application_ref)
        ):
            raise D1RecoveryRequired("saved_model_result_stale")
        return model_request_id, action

    def reconcile_application(
        self,
        model_request_id,
        stage,
        context,
        registration_mutation_id,
        application_ref,
        application_mutation_id,
        token,
    ):
        """Repair a missing audit marker only from the exact committed mutation."""
        evidence = self.evidence.read(model_request_id)
        if evidence is None:
            raise D1RecoveryRequired("model_result_unknown_or_missing")
        envelope = evidence.envelope
        request = D1ModelRequest(**asdict(envelope), context=context)
        if (
            envelope.stage != stage
            or envelope.registration_mutation_id != registration_mutation_id
            or digest(asdict(context)) != envelope.context_sha256
        ):
            raise D1RecoveryRequired("model_request_envelope_mismatch")
        self._configuration(request)
        record, evidence = self._saved(request)
        if evidence.outcome != "accepted":
            raise D1RecoveryRequired("applied_model_result_rejected")
        action = self._parse(evidence.raw, stage)
        committed = self.ledger.load_committed_mutation(
            request.task_run_id, application_mutation_id
        )
        operation = {
            "outer": "register_turn",
            "probe": "register_plan",
            "complete": "complete_verified_t6_intent",
        }[stage]
        if (
            token.is_cancelled
            or self.clock() >= self.controller.control_state.task_run.deadline_at
            or committed is None
            or committed.event.task_run_id != request.task_run_id
            or committed.event.details.get("operation") != operation
            or application_ref not in committed.event.details.get("entity_ids", [])
            or committed.checkpoint.sequence != envelope.base_checkpoint_sequence + 1
            or not self._application_matches(request, action, application_ref)
            or record.application_ref not in {None, application_ref}
        ):
            raise D1RecoveryRequired("model_application_commit_mismatch")
        self.ledger.mark_d1_operation_applied(model_request_id, application_ref)
        return action

    def _application_matches(self, request, action, reference: str) -> bool:
        state = self.controller.control_state
        if request.stage == "outer":
            binding = self.ledger.load_d1_turn_binding(reference)
            turn = next(
                (item for item in state.turns if item.turn_id == reference), None
            )
            intent = next(
                (
                    item
                    for item in state.intents
                    if binding and item.intent_id == binding.intent_id
                ),
                None,
            )
            return (
                binding is not None
                and binding.proposal_request_id == request.model_request_id
                and turn is not None
                and turn.turn_target == action.turn_target
                and turn.intent_ids == (binding.intent_id,)
                and intent is not None
                and (intent.description, intent.intent_type)
                == (action.intents[0].description, action.intents[0].intent_type)
                and binding.import_id == request.context.attachment.import_id
                and binding.expected_sha256 == request.context.attachment.sha256
            )
        binding = request.context.binding
        intent = next(
            (item for item in state.intents if item.intent_id == binding.intent_id),
            None,
        )
        turn = next(
            (item for item in state.turns if item.turn_id == binding.turn_id), None
        )
        if (
            intent is None
            or intent.turn_id != binding.turn_id
            or turn is None
            or turn.turn_target != request.context.turn_target
        ):
            return False
        if request.stage == "probe":
            plan = next(
                (item for item in state.plans if item.plan_id == reference), None
            )
            return (
                plan is not None
                and plan.intent_id == binding.intent_id
                and len(plan.steps) == 1
            )
        return (
            reference == binding.intent_id
            and intent.status.value == "succeeded"
            and action.fact_ids == request.context.fact_ids
            and all(
                any(
                    fact.fact_id == fact_id
                    and fact.turn_id == binding.turn_id
                    and fact.status.value == "verified"
                    for fact in state.facts
                )
                for fact_id in action.fact_ids
            )
        )

    def propose(self, request: D1ModelRequest, token: CancellationToken):
        steps = self._proposal_steps(request, token)
        try:
            invocation = next(steps)
            while True:
                current, timeout = invocation
                try:
                    raw = self.adapter.invoke(current, timeout, token)
                except Exception as exc:
                    raise D1RecoveryRequired("model_call_outcome_unknown") from exc
                invocation = steps.send(raw)
        except StopIteration as finished:
            return finished.value
        finally:
            steps.close()

    async def propose_async(self, request: D1ModelRequest, token: CancellationToken):
        """Only provider I/O runs in a worker; all durable writes stay on this thread."""
        steps = self._proposal_steps(request, token)
        try:
            invocation = next(steps)
            while True:
                current, timeout = invocation
                raw = await self._await_adapter(current, timeout, token)
                invocation = steps.send(raw)
        except StopIteration as finished:
            return finished.value
        finally:
            steps.close()

    async def _await_adapter(self, request, timeout, token):
        stopped, finished = threading.Event(), threading.Event()
        result = []

        class InvocationToken:
            @property
            def is_cancelled(self) -> bool:
                return stopped.is_set() or token.is_cancelled

        def invoke():
            try:
                result.append(
                    (True, self.adapter.invoke(request, timeout, InvocationToken()))
                )
            except BaseException as exc:  # noqa: BLE001 -- isolate all provider worker failures
                result.append((False, exc))
            finally:
                finished.set()

        threading.Thread(target=invoke, daemon=True, name="d1-model-call").start()
        end = asyncio.get_running_loop().time() + timeout
        try:
            while not finished.is_set():
                state = self.controller.control_state
                if (
                    token.is_cancelled
                    or state.task_run.status.value != "running"
                    or self.clock() >= state.task_run.deadline_at
                    or asyncio.get_running_loop().time() >= end
                ):
                    raise D1RecoveryRequired("model_call_outcome_unknown")
                await asyncio.sleep(
                    min(0.025, max(0, end - asyncio.get_running_loop().time()))
                )
            succeeded, raw = result[0]
            if not succeeded:
                raise D1RecoveryRequired("model_call_outcome_unknown") from raw
            return raw
        finally:
            # Worker output is local; a late response cannot write evidence or apply state.
            stopped.set()

    def _proposal_steps(self, request: D1ModelRequest, token: CancellationToken):
        self._configuration(request)
        current = request
        for attempt in range(2):
            state = self.controller.control_state
            saved, evidence = self._saved(current)
            if saved is None:
                if (
                    token.is_cancelled
                    or state.task_run.status.value != "running"
                    or self.clock() >= state.task_run.deadline_at
                ):
                    raise D1RecoveryRequired("model_dispatch_stopped")
                if state.latest_checkpoint.sequence != current.base_checkpoint_sequence:
                    raise D1RecoveryRequired("model_checkpoint_changed")
                category = self._category(current.stage)
                self.ledger.begin_d1_operation(
                    D1OperationStart(
                        current.model_request_id,
                        current.task_run_id,
                        category,
                        self.clock(),
                        current.base_checkpoint_sequence,
                        current.context_sha256,
                        current.role,
                        current.role_config_ref,
                        current.registration_mutation_id,
                    )
                )
                self._configuration(current)
                raw = yield (
                    current,
                    min(180.0, state.task_run.deadline_at - self.clock()),
                )
                outcome: D1ModelOutcome = "accepted"
                code = None
                if isinstance(raw, ModelError):
                    outcome, code, raw = "model_error", raw.short_reason, raw.to_dict()
                else:
                    try:
                        self._parse(raw, current.stage)
                    except D1ProposalRejected as exc:
                        outcome = "invalid_structure" if exc.structural else "rejected"
                        code = exc.code
                state = self.controller.control_state
                if (
                    token.is_cancelled
                    or state.task_run.status.value != "running"
                    or self.clock() >= state.task_run.deadline_at
                    or state.latest_checkpoint.sequence
                    != current.base_checkpoint_sequence
                ):
                    outcome, code = "stale", "model_response_stale"
                evidence = D1ModelEvidence(
                    current.model_request_id,
                    self.clock(),
                    outcome,
                    raw,
                    code,
                    D1ModelEnvelope.from_request(current),
                )
                checksum = self.evidence.save(evidence)
                self.ledger.complete_d1_operation(
                    D1OperationCompletion(
                        current.model_request_id,
                        evidence.finished_at,
                        self._result_kind(outcome),
                        current.model_request_id,
                        checksum,
                    )
                )
            if evidence.outcome == "invalid_structure" and attempt == 0:
                current = replace(
                    current,
                    model_request_id=stable_id(
                        request.task_run_id, request.stage + "/correction"
                    ),
                    correction_of_model_request_id=current.model_request_id,
                    rejection_code=evidence.rejection_code,
                )
                continue
            if evidence.outcome != "accepted":
                raise D1ProposalRejected(evidence.rejection_code or evidence.outcome)
            self._configuration(current)
            if (
                token.is_cancelled
                or state.task_run.status.value != "running"
                or self.clock() >= state.task_run.deadline_at
                or state.latest_checkpoint.sequence != current.base_checkpoint_sequence
            ):
                raise D1RecoveryRequired("saved_model_result_stale")
            return current.model_request_id, self._parse(evidence.raw, current.stage)
        raise D1ProposalRejected("structure_correction_exhausted")
