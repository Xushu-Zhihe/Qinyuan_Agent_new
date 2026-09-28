"""Create one ordered Turn from an audited model result."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Protocol
from uuid import UUID, uuid4

from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import ControlStateSnapshot, canonical_json
from agent_core.controller import TaskRunController
from agent_core.model_invocation_audit import (
    ModelInvocationAuditPort,
    ModelInvocationConflict,
    ModelInvocationRecord,
    ModelInvocationRecoveryRequired,
)
from agent_core.modeling import (
    CancellationToken,
    IntentGenerationResponse,
    ModelError,
    ModelInvocationCoordinator,
    ModelRequest,
    ModelRequestType,
)
from agent_core.runtime import TaskRunStatus
from agent_core.turn_manager import TurnManager


class TurnGenerationAuditPort(ModelInvocationAuditPort, Protocol):
    def list_unapplied_turn_generations(
        self, task_run_id: str
    ) -> tuple[ModelInvocationRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class IntentGenerationResult:
    turn_id: str | None
    model_error: ModelError | None
    replayed: bool


def _stable_request_id(task_run_id: str, mutation_id: str) -> str:
    raw = bytearray(hashlib.sha256(
        f"qinyuan/intent-generation/v1/{task_run_id}/{mutation_id}".encode()
    ).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _context_digest(request: ModelRequest) -> str:
    payload = {
        "goal": request.goal,
        "target": request.target.to_dict(),
        "turn_target": request.turn_target,
        "verified_facts": [asdict(item) for item in request.verified_facts],
        "failure_summaries": [asdict(item) for item in request.failure_summaries],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class OrderedIntentGenerator:
    def __init__(
        self,
        controller: TaskRunController,
        repository: ControlStateRepository,
        audit: TurnGenerationAuditPort,
        coordinator: ModelInvocationCoordinator,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if coordinator.invocation_audit is not audit:
            raise ValueError("Intent generation requires the same durable model audit")
        self._controller = controller
        self._repository = repository
        self._audit = audit
        self._coordinator = coordinator
        self._clock = clock

    async def resume_unapplied(
        self, task_run_id: str, cancellation_token: CancellationToken
    ) -> IntentGenerationResult | None:
        """Find the persisted Turn claim; never invent a replacement call ID."""
        records = self._audit.list_unapplied_turn_generations(task_run_id)
        if not records:
            return None
        first = records[0].start
        if any(
            item.start.registration_mutation_id != first.registration_mutation_id
            for item in records
        ):
            raise ModelInvocationRecoveryRequired("multiple unapplied Turn generations")
        assert first.turn_target is not None
        assert first.registration_mutation_id is not None
        return await self.generate(
            task_run_id, first.turn_target, first.registration_mutation_id,
            cancellation_token,
        )

    async def generate(
        self,
        task_run_id: str,
        turn_target: str,
        registration_mutation_id: str,
        cancellation_token: CancellationToken,
    ) -> IntentGenerationResult:
        try:
            parsed = UUID(registration_mutation_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("registration_mutation_id must be UUID4") from exc
        if parsed.version != 4 or str(parsed) != registration_mutation_id:
            raise ValueError("registration_mutation_id must be UUID4")
        if not isinstance(turn_target, str) or not turn_target.strip():
            raise ValueError("turn_target is required")
        state = self._state(task_run_id)
        turn_id = TurnManager.registration_turn_id(task_run_id, registration_mutation_id)
        previous = self._audit.list_model_invocations(task_run_id, turn_id)
        if previous:
            if previous[0].start.turn_target != turn_target:
                raise ModelInvocationConflict("Turn target differs from the audited request")
            completed = next((item for item in reversed(previous)
                              if item.completion is not None and isinstance(
                                  item.completion.result, IntentGenerationResponse)), None)
            if completed is not None:
                return self._apply(state, completed, cancellation_token, replayed=True)
        if cancellation_token.is_cancelled or state.task_run.status is not TaskRunStatus.RUNNING:
            raise ModelInvocationConflict("Turn generation requires a live TaskRun")
        if self._clock() >= state.task_run.deadline_at:
            raise ModelInvocationConflict("TaskRun deadline elapsed before Turn generation")
        context = self._repository.query_model_context(task_run_id)
        request = ModelRequest(
            model_request_id=_stable_request_id(task_run_id, registration_mutation_id),
            task_run_id=task_run_id,
            turn_id=turn_id,
            request_type=ModelRequestType.INTENT_GENERATION,
            goal=state.task_run.request.goal,
            target=state.task_run.request.target,
            turn_target=turn_target,
            verified_facts=context.facts,
            failure_summaries=context.failure_summaries,
            registration_mutation_id=registration_mutation_id,
            base_checkpoint_sequence=state.latest_checkpoint.sequence,
        )
        request = replace(request, context_sha256=_context_digest(request))
        result = await self._coordinator.invoke_async(request, cancellation_token)
        if isinstance(result, ModelError):
            return IntentGenerationResult(None, result, bool(previous))
        if not isinstance(result, IntentGenerationResponse):
            raise ModelInvocationConflict("model did not return ordered Intent candidates")
        records = self._audit.list_model_invocations(task_run_id, turn_id)
        completed = next((item for item in reversed(records)
                          if item.completion is not None and isinstance(
                              item.completion.result, IntentGenerationResponse)), None)
        if (completed is None or completed.completion is None
            or completed.completion.result != result):
            raise ModelInvocationRecoveryRequired("generated candidates have no committed audit result")
        return self._apply(self._state(task_run_id), completed, cancellation_token, replayed=False)

    def _apply(
        self,
        state: ControlStateSnapshot,
        record: ModelInvocationRecord,
        cancellation_token: CancellationToken,
        *,
        replayed: bool,
    ) -> IntentGenerationResult:
        start = record.start
        assert record.completion is not None
        assert start.registration_mutation_id is not None
        assert start.turn_target is not None
        response = record.completion.result
        assert isinstance(response, IntentGenerationResponse)
        for turn in state.turns:
            if turn.turn_id == start.intent_id:
                existing = tuple(
                    (item.description, item.intent_type)
                    for intent_id in turn.intent_ids
                    for item in state.intents if item.intent_id == intent_id
                )
                expected = tuple((item.description, item.intent_type)
                                 for item in response.candidates)
                if turn.turn_target != start.turn_target or existing != expected:
                    raise ModelInvocationConflict("registered Turn differs from its audited result")
                return IntentGenerationResult(turn.turn_id, None, True)
        if (cancellation_token.is_cancelled
            or state.task_run.status is not TaskRunStatus.RUNNING
            or self._clock() >= state.task_run.deadline_at
            or state.latest_checkpoint.sequence != start.base_checkpoint_sequence):
            raise ModelInvocationRecoveryRequired("audited Turn result is no longer applicable")
        context = self._repository.query_model_context(start.task_run_id)
        current_request = ModelRequest(
            model_request_id=start.model_request_id,
            task_run_id=start.task_run_id,
            turn_id=start.intent_id,
            request_type=ModelRequestType.INTENT_GENERATION,
            goal=state.task_run.request.goal,
            target=state.task_run.request.target,
            turn_target=start.turn_target,
            verified_facts=context.facts,
            failure_summaries=context.failure_summaries,
        )
        if _context_digest(current_request) != start.context_sha256:
            raise ModelInvocationRecoveryRequired("audited Turn context no longer matches")
        registration = TurnManager.prepare_turn_registration(
            state=state,
            mutation_id=start.registration_mutation_id,
            turn_target=start.turn_target,
            candidates=response.candidates,
        )
        if registration.turn_id != start.intent_id:
            raise ModelInvocationConflict("registration identity differs from model audit")
        commit = self._controller.commit_turn_registration(
            registration, start.registration_mutation_id
        )
        if commit.commit.event.details.get("operation") != "register_turn":
            raise ModelInvocationRecoveryRequired("Turn registration was preempted")
        return IntentGenerationResult(registration.turn_id, None, replayed)

    def _state(self, task_run_id: str) -> ControlStateSnapshot:
        state = self._repository.load_control_state(task_run_id)
        if state is None or state.task_run.task_run_id != self._controller.control_state.task_run.task_run_id:
            raise ModelInvocationConflict("controller and repository do not own the same TaskRun")
        return state


class AuditedTurnGenerator:
    """Adapt the audited generator to the supervisor's single-Turn port."""

    def __init__(self, generator: OrderedIntentGenerator) -> None:
        self._generator = generator

    async def generate_next(
        self,
        task_run_id: str,
        turn_target: str,
        cancellation_token: CancellationToken,
    ) -> IntentGenerationResult:
        prior = await self._generator.resume_unapplied(
            task_run_id, cancellation_token
        )
        if prior is not None:
            return prior
        return await self._generator.generate(
            task_run_id, turn_target, str(uuid4()), cancellation_token
        )
