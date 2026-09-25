"""Result contract for one bound Intent's direct tool-calling loop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, get_args
from uuid import uuid4

from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import PersistedIntentGoal, StorageError
from agent_core.controller import TaskRunController
from agent_core.domain import (
    ExecutionEvidencePort,
    ExecutionStatus,
    FailureAction,
    FailureType,
    IntentStatus,
    Plan,
    PlanStatus,
)
from agent_core.file_type_fact import FileTypeFactCandidate
from agent_core.file_type_verifier import (
    FileTypeToolEvidencePort,
    FileTypeVerificationError,
    FileTypeVerifier,
)
from agent_core.intent_action_application import (
    IntentActionApplication,
    IntentApplicationStorePort,
    IntentPlanApplication,
)
from agent_core.intent_goal_evaluator import (
    IntentGoalEvaluation,
    IntentGoalEvaluatorPort,
)
from agent_core.model_invocation_audit import (
    ModelInvocationAuditPort,
    ModelInvocationBudgetExhausted,
    ModelInvocationRecord,
    ModelInvocationRecoveryRequired,
)
from agent_core.modeling import (
    CancellationToken,
    FileTypeCandidateAction,
    InspectFileAction,
    IntentActionContext,
    IntentActionResponse,
    IntentContext,
    ModelError,
    ModelInvocationCoordinator,
    ModelRequest,
    ModelRequestType,
    PlanGenerationResponse,
    StopIntentAction,
    ToolObservation,
)
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.tool_contracts import (
    ScopedToolGatewayPort,
    ToolAuthorization,
    ToolRecoveryRequired,
    ToolRequest,
    ToolResult,
)
from agent_core.tool_policy import FileToolPolicy
from agent_core.turn_working_copy import TurnWorkingCopy

IntentLoopStopReason = Literal[
    "intent_satisfied",
    "model_stopped",
    "candidate_rejected",
    "budget_exhausted",
    "tool_failed",
    "recovery_required",
    "model_error",
    "interrupted",
]


@dataclass(frozen=True, slots=True)
class IntentLoopResult:
    task_run_id: str
    intent_id: str
    evaluation: IntentGoalEvaluation
    stop_reason: IntentLoopStopReason
    plan_id: str | None
    execution_ids: tuple[str, ...]
    verified_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.task_run_id, str)
            or not self.task_run_id
            or not isinstance(self.intent_id, str)
            or not self.intent_id
            or self.evaluation.task_run_id != self.task_run_id
            or self.evaluation.intent_id != self.intent_id
        ):
            raise ValueError("loop result and evaluation must name the same Intent")
        if self.stop_reason not in get_args(IntentLoopStopReason):
            raise ValueError("unsupported Intent loop stop reason")
        if self.plan_id is not None and (
            not isinstance(self.plan_id, str) or not self.plan_id
        ):
            raise ValueError("plan_id must be a non-empty ID or None")
        if (
            not isinstance(self.execution_ids, tuple)
            or any(not isinstance(item, str) or not item for item in self.execution_ids)
            or len(set(self.execution_ids)) != len(self.execution_ids)
        ):
            raise ValueError("execution_ids must contain unique Execution IDs")
        if self.verified_fact_ids != self.evaluation.verified_fact_ids:
            raise ValueError(
                "result must list exactly the evaluation's supporting Facts"
            )
        if (
            self.stop_reason == "intent_satisfied"
            and self.evaluation.outcome != "satisfied"
        ):
            raise ValueError("Intent success requires a satisfied evaluation")


class IntentActionRejected(RuntimeError):
    """Stop this run; the caller must decide how to handle the running Intent."""

    def __init__(
        self,
        code: Literal[
            "path_out_of_scope", "unsupported_plan_steps", "repeated_inspection"
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


class IntentLoopStore(IntentApplicationStorePort, ModelInvocationAuditPort, Protocol):
    def load_intent_goal(self, intent_id: str) -> PersistedIntentGoal | None: ...


class IntentLoopEvidence(FileTypeToolEvidencePort, ExecutionEvidencePort, Protocol):
    pass


class _LoopInterrupted(Exception):
    pass


class IntentToolCallingLoop:
    """One persistent Intent; all domain changes remain Controller-owned."""

    def __init__(
        self,
        *,
        controller: TaskRunController,
        model: ModelInvocationCoordinator,
        gateway: ScopedToolGatewayPort,
        verifier: FileTypeVerifier,
        evaluator: IntentGoalEvaluatorPort,
        evidence: IntentLoopEvidence,
        store: IntentLoopStore,
        context: ControlStateRepository,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if model.invocation_audit is not store:
            raise ValueError(
                "T4 requires the coordinator and loop to share one invocation audit"
            )
        self._controller = controller
        self._model = model
        self._gateway = gateway
        self._verifier = verifier
        self._evaluator = evaluator
        self._evidence = evidence
        self._store = store
        self._context = context
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def _state(self):
        return self._controller.control_state

    @property
    def _work(self) -> TurnWorkingCopy:
        return TurnWorkingCopy(self._state, self._evidence)

    def _records(self, intent_id: str) -> tuple[ModelInvocationRecord, ...]:
        return self._store.list_model_invocations(
            self._state.task_run.task_run_id, intent_id
        )

    def _plan(self, intent_id: str):
        return next((p for p in self._state.plans if p.intent_id == intent_id), None)

    def _executions(self, intent_id: str):
        plan = self._plan(intent_id)
        executions = {
            e.execution_id: e
            for e in self._state.executions
            if plan and e.plan_id == plan.plan_id
        }
        ordered = []
        for record in self._records(intent_id):
            if record.start.request_type is not ModelRequestType.INTENT_ACTION:
                continue
            claim = self._store.load_action_application(record.start.model_request_id)
            if claim is not None:
                for request_id in (claim.tool_request_id, claim.retry_request_id):
                    if request_id in executions:
                        ordered.append(executions.pop(request_id))
        ordered.extend(executions.values())
        return tuple(ordered)

    def _result(self, intent_id: str, reason: IntentLoopStopReason) -> IntentLoopResult:
        task_id = self._state.task_run.task_run_id
        evaluation = self._evaluator.evaluate(task_id, intent_id)
        plan = self._plan(intent_id)
        return IntentLoopResult(
            task_id,
            intent_id,
            evaluation,
            reason,
            plan.plan_id if plan else None,
            tuple(e.execution_id for e in self._executions(intent_id)),
            evaluation.verified_fact_ids,
        )

    def _interrupt(self, token: CancellationToken) -> bool:
        task = self._state.task_run
        if task.status in TERMINAL_STATUSES:
            return True
        if task.deadline_at <= self._clock():
            self._controller.advance_time(str(uuid4()))
            return True
        if token.is_cancelled:
            self._controller.cancel(str(uuid4()))
            return True
        return False

    def _interrupted(self, intent_id: str) -> IntentLoopResult:
        for execution in self._executions(intent_id):
            finished = self._evidence.read_completed(execution.execution_id)
            if finished is None or not finished.record.termination_confirmed:
                return self._result(intent_id, "recovery_required")
        return self._result(intent_id, "interrupted")

    async def run(
        self,
        intent_id: str,
        authorization: ToolAuthorization,
        cancellation_token: CancellationToken,
    ) -> IntentLoopResult:
        async with self._lock:
            binding = self._store.load_intent_goal(intent_id)
            if (
                binding is None
                or binding.goal.task_run_id != self._state.task_run.task_run_id
                or not any(i.intent_id == intent_id for i in self._state.intents)
            ):
                raise FileTypeVerificationError("state_conflict")
            try:
                return await self._run(
                    intent_id, binding.goal.path, authorization, cancellation_token
                )
            except (ToolRecoveryRequired, ModelInvocationRecoveryRequired):
                return self._result(intent_id, "recovery_required")
            except ModelInvocationBudgetExhausted:
                return self._result(intent_id, "budget_exhausted")
            except _LoopInterrupted:
                self._interrupt(cancellation_token)
                return self._interrupted(intent_id)
            except asyncio.CancelledError:
                self._controller.cancel(str(uuid4()))
                return self._interrupted(intent_id)

    async def _run(
        self, intent_id, goal_path, authorization, token
    ) -> IntentLoopResult:
        if self._interrupt(token):
            return self._interrupted(intent_id)
        intent = next(i for i in self._state.intents if i.intent_id == intent_id)
        if intent.status is IntentStatus.SUCCEEDED:
            if (
                self._evaluator.evaluate(
                    self._state.task_run.task_run_id, intent_id
                ).outcome
                != "satisfied"
            ):
                raise FileTypeVerificationError("state_conflict")
            return self._result(intent_id, "intent_satisfied")
        # Existing gateway failures remain inspectable below, without new calls.
        if (
            intent.status is not IntentStatus.RUNNING
            or self._state.task_run.status is not TaskRunStatus.RUNNING
        ) and not self._records(intent_id):
            raise FileTypeVerificationError("state_conflict")
        plan_stop = await self._prepare_plan(intent_id, goal_path, token)
        if plan_stop is not None:
            return self._result(intent_id, plan_stop)

        records = [
            r
            for r in self._records(intent_id)
            if r.start.request_type is ModelRequestType.INTENT_ACTION
        ]
        index = 0
        while True:
            if self._interrupt(token):
                return self._interrupted(intent_id)
            if index < len(records):
                record = records[index]
                index += 1
                if record.completion is None:
                    raise ModelInvocationRecoveryRequired(
                        "model action has no committed result"
                    )
                if record.completion.result_kind == "invalid_structure":
                    if any(
                        r.start.correction_of_model_request_id
                        == record.start.model_request_id
                        for r in records
                    ):
                        continue
                    if record.start.correction_of_model_request_id is not None:
                        return self._result(intent_id, "model_error")
                    record = await self._invoke(intent_id, goal_path, token, record)
                    fresh = True
                else:
                    fresh = False
            else:
                record = await self._invoke(intent_id, goal_path, token)
                fresh = True
            if self._interrupt(token):
                return self._interrupted(intent_id)
            if record.completion is None:
                raise ModelInvocationRecoveryRequired(
                    "model action has no committed result"
                )
            response = record.completion.result
            if isinstance(response, ModelError):
                return self._result(intent_id, "model_error")
            if not isinstance(response, IntentActionResponse):
                raise StorageError("Intent action audit has a different response")
            application = self._store.load_action_application(
                record.start.model_request_id
            )
            if application is None:
                if not fresh:
                    raise ModelInvocationRecoveryRequired(
                        "historical action has no application declaration"
                    )
                application = self._declare_action(record, response)
            if (
                application.action != response.action
                or application.intent_id != intent_id
                or application.task_run_id != self._state.task_run.task_run_id
                or application.plan_id != record.start.plan_id
            ):
                raise StorageError("action declaration differs from audited result")
            stop = await self._apply_action(
                application, goal_path, authorization, token
            )
            if stop is not None:
                return self._result(intent_id, stop)
            # Successful old actions are consumed once. A newly completed call
            # may include its correction; refresh without consuming it twice.
            if fresh:
                records = [
                    r
                    for r in self._records(intent_id)
                    if r.start.request_type is ModelRequestType.INTENT_ACTION
                ]
                index = len(records)

    def _request(
        self, intent_id, goal_path, request_type, model_request_id
    ) -> ModelRequest:
        state = self._state
        intent = next(i for i in state.intents if i.intent_id == intent_id)
        turn = next(t for t in state.turns if t.turn_id == intent.turn_id)
        context = self._context.query_model_context(state.task_run.task_run_id)
        action_context = None
        if request_type is ModelRequestType.INTENT_ACTION:
            plan = self._plan(intent_id)
            assert plan is not None
            observation = None
            executions = self._executions(intent_id)
            if executions:
                execution = executions[-1]
                finished = self._evidence.read_completed(execution.execution_id)
                if finished is None:
                    raise ToolRecoveryRequired(
                        "Execution has no completed tool request"
                    )
                record = finished.record
                stdout = record.stdout[:4096]
                stderr = record.stderr[: 4096 - len(stdout)]
                observation = ToolObservation(
                    execution.execution_id,
                    execution.status,
                    stdout,
                    stderr,
                    record.stdout_truncated,
                    record.stderr_truncated,
                    len(stdout) + len(stderr) < len(record.stdout) + len(record.stderr),
                )
            action_context = IntentActionContext(
                intent_id, plan.plan_id, 0, goal_path, observation
            )
        return ModelRequest(
            model_request_id,
            state.task_run.task_run_id,
            turn.turn_id,
            request_type,
            state.task_run.request.goal,
            state.task_run.request.target,
            turn.turn_target,
            context.facts,
            context.failure_summaries,
            IntentContext(intent_id, intent.description),
            action_context=action_context,
        )

    async def _invoke(
        self,
        intent_id,
        goal_path,
        token,
        correction_record=None,
        request_type=ModelRequestType.INTENT_ACTION,
    ) -> ModelInvocationRecord:
        request_id = (
            correction_record.start.model_request_id
            if correction_record
            else str(uuid4())
        )
        request = self._request(intent_id, goal_path, request_type, request_id)
        await self._model.invoke_async(request, token)
        records = self._records(intent_id)
        matches = [
            r
            for r in records
            if r.start.model_request_id == request_id
            or r.start.correction_of_model_request_id == request_id
        ]
        if not matches:
            if self._interrupt(token):
                raise _LoopInterrupted
            raise StorageError("model coordinator did not persist its invocation")
        return matches[-1]

    async def _prepare_plan(
        self, intent_id, goal_path, token
    ) -> IntentLoopStopReason | None:
        records = [
            r
            for r in self._records(intent_id)
            if r.start.request_type is ModelRequestType.PLAN_GENERATION
        ]
        fresh = not records
        if records:
            record = records[-1]
            if record.completion is None:
                raise ModelInvocationRecoveryRequired(
                    "Plan call has no committed result"
                )
            if (
                record.completion.result_kind == "invalid_structure"
                and record.start.correction_of_model_request_id is None
            ):
                record = await self._invoke(
                    intent_id,
                    goal_path,
                    token,
                    record,
                    ModelRequestType.PLAN_GENERATION,
                )
                fresh = True
        else:
            if self._plan(intent_id) is not None:
                raise ModelInvocationRecoveryRequired(
                    "existing Plan has no application declaration"
                )
            record = await self._invoke(
                intent_id,
                goal_path,
                token,
                request_type=ModelRequestType.PLAN_GENERATION,
            )
        if self._interrupt(token):
            return "interrupted"
        if record.completion is None:
            raise ModelInvocationRecoveryRequired("Plan call has no committed result")
        response = record.completion.result
        if isinstance(response, ModelError):
            return "model_error"
        if not isinstance(response, PlanGenerationResponse):
            raise StorageError("Plan audit has a different response")
        if len(response.candidate.steps) != 1:
            raise IntentActionRejected("unsupported_plan_steps")
        claim = self._store.load_plan_application(record.start.model_request_id)
        if claim is None:
            if not fresh:
                raise ModelInvocationRecoveryRequired(
                    "historical Plan has no application declaration"
                )
            claim = IntentPlanApplication(
                record.start.model_request_id,
                self._state.task_run.task_run_id,
                intent_id,
                response.candidate.steps,
                str(uuid4()),
                str(uuid4()),
                str(uuid4()),
            )
            self._store.save_plan_application(claim)
        if (
            claim.task_run_id != self._state.task_run.task_run_id
            or claim.intent_id != intent_id
            or claim.steps != response.candidate.steps
        ):
            raise StorageError("Plan declaration differs from audited result")
        plan = self._plan(intent_id)
        if plan is None:
            self._controller.commit_turn_operation(
                self._work.register_plan(Plan(claim.plan_id, intent_id, claim.steps)),
                claim.register_mutation_id,
            )
            plan = self._plan(intent_id)
        if plan is None or plan.plan_id != claim.plan_id or plan.steps != claim.steps:
            raise StorageError("committed Plan differs from its declaration")
        if plan.status is PlanStatus.PENDING:
            self._controller.commit_turn_operation(
                self._work.start_plan(plan.plan_id), claim.start_mutation_id
            )
        return None

    def _declare_action(self, record, response) -> IntentActionApplication:
        fields = {}
        if isinstance(response.action, InspectFileAction):
            fields = {
                key: str(uuid4())
                for key in (
                    "tool_request_id",
                    "retry_request_id",
                    "complete_plan_mutation_id",
                )
            }
        elif isinstance(response.action, FileTypeCandidateAction):
            fields = {
                key: str(uuid4())
                for key in (
                    "proof_id",
                    "fact_id",
                    "commit_fact_mutation_id",
                    "complete_intent_mutation_id",
                )
            }
        claim = IntentActionApplication(
            record.start.model_request_id,
            record.start.task_run_id,
            record.start.intent_id,
            record.start.plan_id,
            response.action,
            **fields,
        )
        self._store.save_action_application(claim)
        return claim

    async def _apply_action(
        self, claim, goal_path, authorization, token
    ) -> IntentLoopStopReason | None:
        if self._interrupt(token):
            return self._interrupted(claim.intent_id).stop_reason
        action = claim.action
        if isinstance(action, StopIntentAction):
            return "model_stopped"
        if isinstance(action, InspectFileAction):
            if (
                not FileToolPolicy()
                .authorize_intent_path(goal_path=goal_path, requested_path=action.path)
                .allowed
            ):
                raise IntentActionRejected("path_out_of_scope")
            return await self._inspect(claim, authorization, token)
        assert isinstance(action, FileTypeCandidateAction)
        try:
            candidate = FileTypeFactCandidate(
                action.path, action.observed_type, action.execution_id
            )
        except ValueError:
            return "candidate_rejected"
        fact = next((f for f in self._state.facts if f.fact_id == claim.fact_id), None)
        if fact is None:
            try:
                self._verifier.verify_candidate(
                    intent_id=claim.intent_id,
                    candidate=candidate,
                    proof_id=claim.proof_id,
                )
            except FileTypeVerificationError as exc:
                if exc.code == "candidate_rejected":
                    return "candidate_rejected"
                raise
        if self._interrupt(token):
            return self._interrupted(claim.intent_id).stop_reason
        self._controller.commit_verified_file_type_fact(
            claim.intent_id,
            candidate,
            claim.proof_id,
            claim.fact_id,
            claim.commit_fact_mutation_id,
        )
        evaluation = self._evaluator.evaluate(claim.task_run_id, claim.intent_id)
        if self._interrupt(token):
            return self._interrupted(claim.intent_id).stop_reason
        if evaluation.outcome == "satisfied":
            self._controller.complete_satisfied_intent(
                claim.intent_id, evaluation, claim.complete_intent_mutation_id
            )
            return "intent_satisfied"
        raise FileTypeVerificationError("state_conflict")

    async def _inspect(
        self, claim, authorization, token
    ) -> IntentLoopStopReason | None:
        owned_ids = {claim.tool_request_id, claim.retry_request_id}
        if any(
            e.status is ExecutionStatus.SUCCEEDED and e.execution_id not in owned_ids
            for e in self._executions(claim.intent_id)
        ):
            raise IntentActionRejected("repeated_inspection")
        for attempt, request_id in enumerate(
            (claim.tool_request_id, claim.retry_request_id)
        ):
            if self._interrupt(token):
                return self._interrupted(claim.intent_id).stop_reason
            request = ToolRequest(
                request_id,
                claim.task_run_id,
                claim.plan_id,
                0,
                "file.inspect_file",
                claim.action.path,
            )
            receipt = self._evidence.read_request_receipt(request_id)
            finished = self._evidence.read_completed(request_id)
            if receipt is not None:
                if (
                    receipt.request != request
                    or receipt.sandbox_id != self._state.task_run.sandbox_id
                ):
                    raise StorageError("tool request differs from action declaration")
                if finished is None:
                    raise ToolRecoveryRequired("tool request has an unknown result")
                self._check_tool_result(request, finished, receipt.authorization)
            else:
                if finished is not None or any(
                    e.execution_id == request_id
                    for e in self._executions(claim.intent_id)
                ):
                    raise StorageError("tool request receipt is missing")
                finished = await self._gateway.call(request, authorization, token)
                self._check_tool_result(request, finished, authorization)
            record = finished.record
            if self._interrupt(token):
                return self._interrupted(claim.intent_id).stop_reason
            if record.failure_type is FailureType.OUTCOME_UNKNOWN or (
                record.execution_id is not None and not record.termination_confirmed
            ):
                return "recovery_required"
            if record.failure_type is None:
                plan = self._plan(claim.intent_id)
                if plan.status is PlanStatus.RUNNING:
                    self._controller.commit_turn_operation(
                        self._work.complete_plan(claim.plan_id),
                        claim.complete_plan_mutation_id,
                    )
                elif plan.status is not PlanStatus.COMPLETED:
                    raise FileTypeVerificationError("state_conflict")
                return None
            if not (
                attempt == 0
                and record.failure_type is FailureType.ENVIRONMENT_FAULT
                and finished.action
                in {
                    FailureAction.RETRY_EXECUTION,
                    FailureAction.RETRY_COMMAND_GENERATION,
                }
            ):
                return "tool_failed"
        return "tool_failed"

    def _check_tool_result(
        self, request: ToolRequest, result: ToolResult, authorization: ToolAuthorization
    ) -> None:
        record = result.record
        if (
            record.request_id != request.request_id
            or record.task_run_id != request.task_run_id
            or record.plan_id != request.plan_id
            or record.step_index != request.step_index
            or record.sandbox_id != self._state.task_run.sandbox_id
            or record.authorization_id != authorization.authorization_id
        ):
            raise StorageError("tool result does not match its request")
        if record.execution_id is not None:
            execution = next(
                (
                    e
                    for e in self._state.executions
                    if e.execution_id == record.execution_id
                ),
                None,
            )
            if (
                record.execution_id != request.request_id
                or record.output_ref != request.request_id
                or execution is None
                or execution.plan_id != request.plan_id
                or execution.step_index != request.step_index
                or execution.command_ref != record.command_ref
                or not FileToolPolicy().authorize(request, authorization).allowed
                or not FileToolPolicy()
                .validate_recorded_command(
                    request, self._evidence.load_command(execution.command_ref)
                )
                .allowed
            ):
                raise StorageError("tool result does not match committed Execution")
            if self._state.task_run.status not in TERMINAL_STATUSES and (
                execution.output_ref != record.output_ref
                or (
                    record.failure_type is None
                    and execution.status is not ExecutionStatus.SUCCEEDED
                )
            ):
                raise ToolRecoveryRequired(
                    "tool result and committed Execution are not settled"
                )
        elif record.failure_type is None:
            raise StorageError("successful tool result has no Execution")
