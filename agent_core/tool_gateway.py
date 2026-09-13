"""The T3 single-call pipeline; scheduling and retries remain caller-owned."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol
from uuid import uuid4

from agent_core.control_store import (
    CommittedOperation,
    ControlStateSnapshot,
    PlanValidationRecordDraft,
    TurnOperation,
)
from agent_core.domain import (
    Execution,
    ExecutionStatus,
    FailureAction,
    FailureObservation,
    FailureType,
    PlanStatus,
    PlanValidationStatus,
)
from agent_core.modeling import CancellationToken
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.tool_contracts import (
    ExecutionRecord,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    ToolAuthorization,
    ToolCallError,
    ToolCommandPlannerPort,
    ToolPolicyPort,
    ToolRecorderPort,
    ToolRecoveryRequired,
    ToolRequest,
    ToolResult,
    ToolSandboxPort,
)
from agent_core.turn_working_copy import TurnWorkingCopy


class ToolControllerPort(Protocol):
    @property
    def control_state(self) -> ControlStateSnapshot: ...

    def commit_turn_operation(
        self, operation: TurnOperation, mutation_id: str
    ) -> CommittedOperation: ...

    def cancel(self, mutation_id: str) -> CommittedOperation: ...

    def advance_time(self, mutation_id: str) -> CommittedOperation | None: ...


class ToolGateway:
    def __init__(
        self,
        *,
        controller: ToolControllerPort,
        policy: ToolPolicyPort,
        planner: ToolCommandPlannerPort,
        sandbox: ToolSandboxPort,
        recorder: ToolRecorderPort,
        clock: Callable[[], float],
    ) -> None:
        self._controller = controller
        self._policy = policy
        self._planner = planner
        self._sandbox = sandbox
        self._recorder = recorder
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def _state(self) -> ControlStateSnapshot:
        return self._controller.control_state

    @property
    def _work(self) -> TurnWorkingCopy:
        return TurnWorkingCopy(self._state, self._recorder)

    def _commit(self, operation: TurnOperation) -> CommittedOperation:
        return self._controller.commit_turn_operation(operation, str(uuid4()))

    def _remaining(self) -> float:
        remaining = self._state.task_run.deadline_at - self._clock()
        if not math.isfinite(remaining):
            raise ValueError("invalid task clock")
        return remaining

    def _interrupt(self, token: CancellationToken) -> bool:
        if self._state.task_run.status in TERMINAL_STATUSES:
            return True
        if self._remaining() <= 0:
            self._controller.advance_time(str(uuid4()))
            return True
        if token.is_cancelled:
            self._controller.cancel(str(uuid4()))
            return True
        return False

    def _validate_context(self, request: ToolRequest) -> None:
        state = self._state
        if request.task_run_id != state.task_run.task_run_id:
            raise ValueError("request belongs to another TaskRun")
        plan = next((p for p in state.plans if p.plan_id == request.plan_id), None)
        if plan is None or not 0 <= request.step_index < len(plan.steps):
            raise ValueError("request Plan or step is not registered")

    def _validate_active(self, request: ToolRequest) -> None:
        state = self._state
        if state.task_run.status is not TaskRunStatus.RUNNING:
            raise ToolRecoveryRequired("TaskRun is no longer running")
        plan = next(p for p in state.plans if p.plan_id == request.plan_id)
        if plan.status is not PlanStatus.RUNNING:
            raise ToolRecoveryRequired("Plan is no longer running")
        if any(e.status is ExecutionStatus.RUNNING for e in state.executions):
            raise ToolRecoveryRequired(
                "an Execution is already running or requires recovery"
            )
        for execution in state.executions:
            if execution.output_ref is not None:
                record = self._recorder.read_record(execution.output_ref)
                if record is None or not record.termination_confirmed:
                    raise ToolRecoveryRequired(
                        "prior execution termination is not confirmed"
                    )

    async def call(
        self,
        request: ToolRequest,
        authorization: ToolAuthorization,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        async with self._lock:
            self._validate_context(request)
            decision = self._policy.authorize(request, authorization)
            sandbox_id = self._state.task_run.sandbox_id
            replay = self._recorder.begin(request, authorization, sandbox_id)
            if replay is not None:
                return replay
            started_at = self._clock()
            if self._interrupt(cancellation_token):
                return self._finish_terminal(
                    request, authorization, sandbox_id, started_at
                )
            self._validate_active(request)
            if not decision.allowed:
                if any(
                    (e.plan_id, e.step_index) == (request.plan_id, request.step_index)
                    for e in self._state.executions
                ):
                    return self._rejected_retry(
                        request,
                        authorization,
                        sandbox_id,
                        started_at,
                        decision.short_reason,
                    )
                return self._generation_failure(
                    request,
                    authorization,
                    sandbox_id,
                    started_at,
                    FailureType.POLICY_REJECTED,
                    decision.short_reason,
                )

            # Execution retry consumes the original command and validation, as
            # required by T1. A new request ID is mandatory for an explicit retry.
            previous = [
                e
                for e in self._state.executions
                if (e.plan_id, e.step_index) == (request.plan_id, request.step_index)
            ]
            command_ref: str
            validation_id: str
            if previous:
                first = previous[0]
                failures = [
                    f
                    for f in self._state.failure_summaries
                    if f.evidence_ref == first.execution_id
                ]
                if (
                    len(previous) != 1
                    or len(failures) != 1
                    or failures[0].failure_type is not FailureType.ENVIRONMENT_FAULT
                ):
                    raise ToolRecoveryRequired(
                        "step already executed; no explicit environment retry remains"
                    )
                command_ref = first.command_ref
                command = self._recorder.load_command(command_ref)
                validation_id = next(
                    v.plan_validation_id
                    for v in self._state.plan_validation_records
                    if v.command_ref == command_ref
                )
            else:
                task = asyncio.create_task(
                    self._planner.prepare(request, min(20.0, self._remaining()))
                )
                planning_end = asyncio.get_running_loop().time() + min(
                    20.0, self._remaining()
                )
                try:
                    while not task.done():
                        if self._interrupt(cancellation_token):
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            return self._finish_terminal(
                                request, authorization, sandbox_id, started_at
                            )
                        if asyncio.get_running_loop().time() >= planning_end:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            raise ToolCallError(
                                FailureType.ENVIRONMENT_FAULT, "mcp_timed_out"
                            )
                        await asyncio.wait({task}, timeout=0.025)
                    command = task.result()
                except asyncio.CancelledError:
                    self._controller.cancel(str(uuid4()))
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self._finish_terminal(
                        request, authorization, sandbox_id, started_at
                    )
                    raise
                except ToolCallError as exc:
                    if self._interrupt(cancellation_token):
                        return self._finish_terminal(
                            request, authorization, sandbox_id, started_at
                        )
                    return self._generation_failure(
                        request,
                        authorization,
                        sandbox_id,
                        started_at,
                        exc.failure_type,
                        exc.short_reason,
                    )
                except Exception:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    raise
                if self._interrupt(cancellation_token):
                    return self._finish_terminal(
                        request, authorization, sandbox_id, started_at
                    )
                self._validate_active(request)
                command_ref = str(uuid4())
                validation_id = str(uuid4())

            decision = self._policy.validate_command(request, command)
            if not decision.allowed:
                if previous:
                    return self._rejected_retry(
                        request,
                        authorization,
                        sandbox_id,
                        started_at,
                        decision.short_reason,
                    )
                return self._generation_failure(
                    request,
                    authorization,
                    sandbox_id,
                    started_at,
                    FailureType.POLICY_REJECTED,
                    decision.short_reason,
                )
            # Recheck task time and authorization immediately before durable
            # execution admission. No model-owned authorization is consulted.
            if self._interrupt(cancellation_token):
                return self._finish_terminal(
                    request, authorization, sandbox_id, started_at
                )
            if not previous:
                self._recorder.save_command(command_ref, command)
                draft = self._validation(
                    request, validation_id, command_ref, None, "command_authorized"
                )
                self._commit(self._work.append_plan_validation_record(draft))
            execution = Execution(
                request.request_id, request.plan_id, request.step_index, command_ref
            )
            self._commit(
                self._work.create_execution(execution, plan_validation_id=validation_id)
            )
            self._commit(self._work.start_execution(execution.execution_id))
            if self._interrupt(cancellation_token):
                return self._finish_terminal(
                    request,
                    authorization,
                    sandbox_id,
                    started_at,
                    execution.execution_id,
                    command_ref,
                )

            timeout = min(command.timeout_seconds, math.floor(self._remaining()))
            effective_timeout_seconds = timeout if timeout >= 1 else None
            if timeout < 1:
                # Do not round a fractional remaining budget up beyond Deadline.
                result = SandboxExecutionResult(sandbox_id, None, timed_out=True)
            else:
                dispatch = replace(command, timeout_seconds=timeout)
                try:
                    result = await self._execute(
                        SandboxExecutionRequest(sandbox_id, dispatch),
                        cancellation_token,
                    )
                except Exception as original_error:
                    # Do not conceal a Controller commit/reload/cleanup error.
                    # A separate audit can still say the execution is unknown,
                    # including when the committed cache is no longer readable.
                    interrupted = ExecutionRecord(
                        request.request_id,
                        request.task_run_id,
                        sandbox_id,
                        request.plan_id,
                        request.step_index,
                        authorization.authorization_id,
                        execution.execution_id,
                        command_ref,
                        request.request_id,
                        started_at,
                        max(started_at, self._clock()),
                        None,
                        FailureType.OUTCOME_UNKNOWN,
                        "execution_pipeline_interrupted",
                        effective_timeout_seconds=effective_timeout_seconds,
                    )
                    try:
                        self._recorder.save_record(interrupted)
                    except Exception:
                        original_error.add_note(
                            "Independent execution audit could not be saved; request requires recovery."
                        )
                    raise
            terminal = self._state.task_run.status in TERMINAL_STATUSES
            failure, reason = self._classify(result)
            if terminal:
                failure, reason = (
                    FailureType.OUTCOME_UNKNOWN,
                    "task_terminated_during_execution",
                )
            record = self._record(
                request,
                authorization,
                sandbox_id,
                started_at,
                execution_id=execution.execution_id,
                command_ref=command_ref,
                failure=failure,
                reason=reason,
                result=result,
                effective_timeout_seconds=effective_timeout_seconds,
            )
            if not terminal and not result.termination_confirmed:
                self._sandbox.quiesce(sandbox_id)
            record = self._recorder.save_record(record)
            action = None
            if not terminal:
                if failure is None:
                    self._commit(
                        self._work.complete_execution(
                            execution_id=execution.execution_id,
                            output_ref=record.output_ref,
                        )
                    )
                else:
                    status = (
                        ExecutionStatus.TIMED_OUT
                        if result.timed_out
                        else ExecutionStatus.FAILED
                    )
                    observation = FailureObservation(
                        policy_rejected=failure is FailureType.POLICY_REJECTED,
                        parameter_error=failure is FailureType.PARAMETER_ERROR,
                        infrastructure_diagnostic=failure
                        is FailureType.ENVIRONMENT_FAULT,
                    )
                    committed = self._commit(
                        self._work.record_execution_failure(
                            execution_id=execution.execution_id,
                            status=status,
                            output_ref=record.output_ref,
                            observation=observation,
                            short_reason=reason,
                        )
                    )
                    if committed.value is None:
                        raise ToolRecoveryRequired(
                            "committed failure is missing its action"
                        )
                    action = FailureAction(committed.value)
            outcome = ToolResult(record, action)
            self._recorder.finish(outcome)
            return outcome

    async def _execute(
        self, request: SandboxExecutionRequest, token: CancellationToken
    ) -> SandboxExecutionResult:
        task = asyncio.create_task(self._sandbox.execute(request))
        end = asyncio.get_running_loop().time() + request.command.timeout_seconds
        try:
            while not task.done():
                if self._interrupt(token):
                    # Cleanup has already been committed and attempted by the
                    # Controller. Give the local output reader a bounded drain.
                    await asyncio.wait({task}, timeout=0.1)
                    break
                if asyncio.get_running_loop().time() >= end:
                    break
                await asyncio.wait({task}, timeout=0.025)
            self._interrupt(token)
            if task.done():
                try:
                    result = task.result()
                except Exception:
                    return SandboxExecutionResult(request.sandbox_id, None)
                if result.sandbox_id != request.sandbox_id:
                    return SandboxExecutionResult(request.sandbox_id, None)
                return result
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return SandboxExecutionResult(request.sandbox_id, None, timed_out=True)
        except asyncio.CancelledError:
            self._controller.cancel(str(uuid4()))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return SandboxExecutionResult(request.sandbox_id, None)
        except Exception:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Persistence/cleanup errors must remain visible, never be recast as
            # ordinary tool failures. Task exceptions alone are unknown output.
            raise

    def _validation(
        self,
        request: ToolRequest,
        validation_id: str,
        command_ref: str | None,
        failure: FailureType | None,
        reason: str,
    ) -> PlanValidationRecordDraft:
        attempt = 1 + sum(
            1
            for v in self._state.plan_validation_records
            if (v.plan_id, v.step_index) == (request.plan_id, request.step_index)
        )
        return PlanValidationRecordDraft(
            validation_id,
            request.plan_id,
            request.task_run_id,
            request.step_index,
            attempt,
            PlanValidationStatus.FAILED if failure else PlanValidationStatus.SUCCEEDED,
            failure,
            reason,
            command_ref,
        )

    def _generation_failure(
        self,
        request: ToolRequest,
        authorization: ToolAuthorization,
        sandbox_id: str,
        started_at: float,
        failure: FailureType,
        reason: str,
    ) -> ToolResult:
        record = self._recorder.save_record(
            self._record(
                request,
                authorization,
                sandbox_id,
                started_at,
                failure=failure,
                reason=reason,
            )
        )
        validation_id = str(uuid4())
        self._commit(
            self._work.append_plan_validation_record(
                self._validation(request, validation_id, None, failure, reason)
            )
        )
        committed = self._commit(
            self._work.record_command_generation_failure(
                plan_validation_id=validation_id
            )
        )
        if committed.value is None:
            raise ToolRecoveryRequired("committed failure is missing its action")
        result = ToolResult(record, FailureAction(committed.value))
        self._recorder.finish(result)
        return result

    def _rejected_retry(
        self,
        request: ToolRequest,
        authorization: ToolAuthorization,
        sandbox_id: str,
        started_at: float,
        reason: str,
    ) -> ToolResult:
        # Reject the new request without consuming or replacing the already
        # committed command validation required for an environment retry.
        record = self._recorder.save_record(
            self._record(
                request,
                authorization,
                sandbox_id,
                started_at,
                failure=FailureType.POLICY_REJECTED,
                reason=reason,
            )
        )
        result = ToolResult(record)
        self._recorder.finish(result)
        return result

    def _finish_terminal(
        self,
        request: ToolRequest,
        authorization: ToolAuthorization,
        sandbox_id: str,
        started_at: float,
        execution_id: str | None = None,
        command_ref: str | None = None,
    ) -> ToolResult:
        record = self._recorder.save_record(
            self._record(
                request,
                authorization,
                sandbox_id,
                started_at,
                execution_id=execution_id,
                command_ref=command_ref,
                failure=FailureType.OUTCOME_UNKNOWN,
                reason="task_terminated_before_dispatch",
            )
        )
        result = ToolResult(record)
        self._recorder.finish(result)
        return result

    def _record(
        self,
        request: ToolRequest,
        authorization: ToolAuthorization,
        sandbox_id: str,
        started_at: float,
        *,
        execution_id: str | None = None,
        command_ref: str | None = None,
        failure: FailureType | None = None,
        reason: str,
        result: SandboxExecutionResult | None = None,
        effective_timeout_seconds: int | None = None,
    ) -> ExecutionRecord:
        state = self._state
        result = result or SandboxExecutionResult(sandbox_id, None)
        return ExecutionRecord(
            request.request_id,
            request.task_run_id,
            sandbox_id,
            request.plan_id,
            request.step_index,
            authorization.authorization_id,
            execution_id,
            command_ref,
            request.request_id,
            started_at,
            max(started_at, self._clock()),
            result.exit_code,
            failure,
            reason,
            result.stdout,
            result.stderr,
            result.stdout_truncated,
            result.stderr_truncated,
            result.timed_out
            or state.task_run.status is TaskRunStatus.DEADLINE_EXCEEDED,
            state.task_run.status is TaskRunStatus.CANCELLED,
            result.termination_confirmed,
            effective_timeout_seconds=effective_timeout_seconds,
        )

    @staticmethod
    def _classify(result: SandboxExecutionResult) -> tuple[FailureType | None, str]:
        if not result.termination_confirmed:
            return FailureType.OUTCOME_UNKNOWN, "execution_termination_unknown"
        if result.timed_out:
            return FailureType.OUTCOME_UNKNOWN, "command_timed_out"
        mapping = {
            0: (None, "command_completed"),
            40: (FailureType.PARAMETER_ERROR, "file_missing"),
            41: (FailureType.POLICY_REJECTED, "file_path_rejected"),
            42: (FailureType.ENVIRONMENT_FAULT, "file_environment_error"),
            43: (FailureType.OUTCOME_UNKNOWN, "file_program_failed"),
        }
        if result.exit_code is None:
            return FailureType.OUTCOME_UNKNOWN, "execution_outcome_unknown"
        return mapping.get(
            result.exit_code, (FailureType.OUTCOME_UNKNOWN, "execution_outcome_unknown")
        )
