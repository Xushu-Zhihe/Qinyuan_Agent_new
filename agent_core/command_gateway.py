"""One audited script dispatch; model decisions and retries stay caller-owned."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from typing import Callable
from uuid import uuid4

from agent_core.control_store import PlanValidationRecordDraft
from agent_core.domain import (
    Execution,
    ExecutionStatus,
    FailureAction,
    FailureObservation,
    FailureType,
    PlanValidationStatus,
)
from agent_core.modeling import CancellationToken
from agent_core.runtime import TERMINAL_STATUSES
from agent_core.command_contracts import (
    AuthorizedCommand,
    SandboxCommandAuthorization,
    SandboxCommandPolicy,
    SandboxCommandRequest,
    SandboxCommandResult,
    T6CommandExecutionError,
    T6CommandRecorderPort,
    T6CommandRecoveryRequired,
    T6CommandResult,
    T6CommandSandboxPort,
    T6ExecutionRecord,
)
from agent_core.tool_gateway import ToolControllerPort
from agent_core.turn_working_copy import TurnWorkingCopy


class T6CommandGateway:
    def __init__(
        self,
        *,
        controller: ToolControllerPort,
        policy: SandboxCommandPolicy,
        sandbox: T6CommandSandboxPort,
        recorder: T6CommandRecorderPort,
        clock: Callable[[], float],
        step_limit_seconds: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.controller = controller
        self.policy = policy
        self.sandbox = sandbox
        self.recorder = recorder
        self.clock = clock
        self.step_limit_seconds = step_limit_seconds
        self.monotonic = monotonic
        self._lock = asyncio.Lock()

    @property
    def state(self):
        return self.controller.control_state

    @property
    def work(self):
        return TurnWorkingCopy(self.state, self.recorder)

    def _commit(self, operation):
        return self.controller.commit_turn_operation(operation, str(uuid4()))

    def _interrupt(self, token: CancellationToken) -> bool:
        if self.state.task_run.status in TERMINAL_STATUSES:
            return True
        if self.clock() >= self.state.task_run.deadline_at:
            self.controller.advance_time(str(uuid4()))
            return True
        if token.is_cancelled:
            self.controller.cancel(str(uuid4()))
            return True
        return False

    def _validation(self, request, command_ref, failure=None):
        return PlanValidationRecordDraft(
            str(uuid4()),
            request.plan_id,
            request.task_run_id,
            request.step_index,
            1
            + sum(
                (v.plan_id, v.step_index) == (request.plan_id, request.step_index)
                for v in self.state.plan_validation_records
            ),
            PlanValidationStatus.FAILED if failure else PlanValidationStatus.SUCCEEDED,
            failure,
            failure.value if failure else "command_authorized",
            command_ref,
        )

    def _record(
        self,
        request,
        authorization,
        started,
        approved: AuthorizedCommand | None,
        command_ref,
        result,
        status,
    ):
        return T6ExecutionRecord(
            request.request_id,
            request.task_run_id,
            self.state.task_run.sandbox_id,
            request.plan_id,
            request.step_index,
            authorization.authorization_id,
            command_ref,
            started,
            max(started, self.clock()),
            result.exit_code,
            result.stdout,
            result.stderr,
            result.stdout_truncated,
            result.stderr_truncated,
            status == "step_timeout",
            result.termination_confirmed,
            result.stream_complete,
            approved.effective_timeout_seconds
            if approved and result.dispatched
            else None,
            status,
            result.dispatched,
        )

    def _empty(self, *, confirmed=True, dispatched=False):
        return T6CommandResult(
            self.state.task_run.sandbox_id,
            None,
            b"",
            b"",
            False,
            False,
            False,
            confirmed,
            True,
            dispatched,
        )

    def _before_execution_failure(self, request, authorization, started, failure):
        record = self._record(
            request,
            authorization,
            started,
            None,
            None,
            self._empty(),
            failure.value,
        )
        self.recorder.save_record(record)
        draft = self._validation(request, None, failure)
        self._commit(self.work.append_plan_validation_record(draft))
        commit = self._commit(
            self.work.record_command_generation_failure(
                plan_validation_id=draft.plan_validation_id,
            )
        )
        result = SandboxCommandResult(record, FailureAction(commit.value))
        self.recorder.finish(result)
        return result

    def _check_unfinished(self):
        for execution in self.state.executions:
            if execution.status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
                raise T6CommandRecoveryRequired("unfinished_execution")
            if execution.output_ref:
                record = self.recorder.read_record(execution.output_ref)
                if (
                    record is None
                    or record.status == "outcome_unknown"
                    or not record.termination_confirmed
                ):
                    raise T6CommandRecoveryRequired("prior_execution_requires_recovery")

    def _environment_retry(self, request, approved):
        previous = [
            e
            for e in self.state.executions
            if (e.plan_id, e.step_index) == (request.plan_id, request.step_index)
        ]
        if not previous:
            return None
        last = previous[-1]
        failures = [
            f
            for f in self.state.failure_summaries
            if f.evidence_ref == last.execution_id
        ]
        if len(failures) != 1:
            raise T6CommandRecoveryRequired("step_already_executed")
        if failures[0].failure_type is FailureType.STEP_TIMEOUT:
            return None
        if (
            failures[0].failure_type is not FailureType.ENVIRONMENT_FAULT
            or len(previous) != 1
        ):
            raise T6CommandRecoveryRequired("step_not_retryable")
        command = self.recorder.read_command(last.command_ref)
        expected = {
            "argv": ("bash", "-c", request.script),
            "stdin_sha256": request.stdin_sha256,
            "stdin_size": len(request.stdin),
            "working_directory": request.working_directory,
            "sandbox_id": approved.sandbox_id,
        }
        if command is None or any(
            getattr(command, k) != v for k, v in expected.items()
        ):
            raise T6CommandRecoveryRequired("environment_retry_command_changed")
        validation = next(
            v
            for v in self.state.plan_validation_records
            if v.command_ref == last.command_ref
        )
        return last.command_ref, validation.plan_validation_id

    async def call(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        cancellation_token: CancellationToken,
    ) -> SandboxCommandResult:
        async with self._lock:
            if request.task_run_id != self.state.task_run.task_run_id:
                raise ValueError("request_task_mismatch")
            plan = next(
                (p for p in self.state.plans if p.plan_id == request.plan_id), None
            )
            if plan is None or request.step_index >= len(plan.steps):
                raise ValueError("plan_step_not_found")
            sandbox_id = self.state.task_run.sandbox_id
            # Exact replay is read-only even after task termination or deadline.
            replay = self.recorder.replay(request, authorization, sandbox_id)
            if replay is not None:
                return replay
            if self._interrupt(cancellation_token):
                raise T6CommandRecoveryRequired("task_not_running")
            self._check_unfinished()
            started = self.clock()
            try:
                approved = self.policy.authorize(
                    request,
                    authorization,
                    self.state,
                    now=started,
                    step_limit_seconds=self.step_limit_seconds,
                )
            except ValueError:
                # A rejected retry cannot consume the first command's receipt.
                if any(
                    (v.plan_id, v.step_index) == (request.plan_id, request.step_index)
                    for v in self.state.plan_validation_records
                ):
                    raise
                self.recorder.begin(request, authorization, sandbox_id)
                return self._before_execution_failure(
                    request,
                    authorization,
                    started,
                    FailureType.POLICY_REJECTED,
                )
            end = self.monotonic() + approved.effective_timeout_seconds
            approved = replace(approved, deadline_monotonic=end)
            retry = self._environment_retry(request, approved)
            self.recorder.begin(request, authorization, sandbox_id)
            if self._interrupt(cancellation_token):
                raise T6CommandRecoveryRequired("task_terminated_before_dispatch")
            if retry is None and math.floor(end - self.monotonic()) < 1:
                return self._before_execution_failure(
                    request,
                    authorization,
                    started,
                    FailureType.STEP_TIMEOUT,
                )
            if retry:
                command_ref, validation_id = retry
            else:
                command_ref = str(uuid4())
                self.recorder.save_command(
                    command_ref, approved, ("bash", "-c", request.script)
                )
                draft = self._validation(request, command_ref)
                self._commit(self.work.append_plan_validation_record(draft))
                validation_id = draft.plan_validation_id
            self._commit(
                self.work.create_execution(
                    Execution(
                        request.request_id,
                        request.plan_id,
                        request.step_index,
                        command_ref,
                    ),
                    plan_validation_id=validation_id,
                )
            )
            self._commit(self.work.start_execution(request.request_id))
            try:
                if self._interrupt(cancellation_token):
                    result = self._empty()
                    status = "outcome_unknown"
                elif math.floor(end - self.monotonic()) < 1:
                    result = self._empty()
                    status = "step_timeout"
                else:
                    result, status = await self._execute(approved, cancellation_token)
                terminal = self._interrupt(cancellation_token)
                if terminal:
                    status = "outcome_unknown"
                record = self._record(
                    request,
                    authorization,
                    started,
                    approved,
                    command_ref,
                    result,
                    status,
                )
                if status == "outcome_unknown" and not terminal:
                    self.sandbox.quiesce(sandbox_id)
                self.recorder.save_record(record)
                action = None
                if not terminal:
                    if status == "completed":
                        self._commit(
                            self.work.complete_execution(
                                execution_id=request.request_id,
                                output_ref=request.request_id,
                            )
                        )
                    elif status == "step_timeout":
                        commit = self._commit(
                            self.work.record_step_timeout(
                                execution_id=request.request_id,
                                output_ref=request.request_id,
                            )
                        )
                        action = FailureAction(commit.value)
                    else:
                        commit = self._commit(
                            self.work.record_execution_failure(
                                execution_id=request.request_id,
                                output_ref=request.request_id,
                                status=ExecutionStatus.FAILED,
                                observation=FailureObservation(
                                    infrastructure_diagnostic=status
                                    == "environment_fault",
                                ),
                                short_reason=status,
                            )
                        )
                        action = FailureAction(commit.value)
                outcome = SandboxCommandResult(record, action)
                self.recorder.finish(outcome)
                return outcome
            except BaseException:
                # An incomplete claim prevents both exact and new-ID dispatch
                # on restart. Never overwrite an already saved output record.
                self.sandbox.quiesce(sandbox_id)
                raise

    async def _execute(self, approved, token):
        task = asyncio.create_task(self.sandbox.execute(approved, token))
        try:
            # Runtime owns the deadline and confirmed process cleanup. A lost
            # runtime response cannot be turned into a retryable timeout.
            while not task.done():
                if self._interrupt(token):
                    break
                if self.monotonic() >= approved.deadline_monotonic + 3:
                    break
                await asyncio.wait({task}, timeout=0.025)
            if not task.done():
                self.sandbox.quiesce(approved.sandbox_id)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return self._empty(confirmed=False, dispatched=True), "outcome_unknown"
            try:
                result = task.result()
            except T6CommandExecutionError as exc:
                return self._empty(
                    confirmed=not exc.dispatched, dispatched=exc.dispatched
                ), ("outcome_unknown" if exc.dispatched else "environment_fault")
            except Exception:
                return self._empty(confirmed=False, dispatched=True), "outcome_unknown"
            if (
                result.sandbox_id != approved.sandbox_id
                or not result.termination_confirmed
            ):
                return self._empty(confirmed=False, dispatched=True), "outcome_unknown"
            if result.timed_out:
                return result, "step_timeout"
            if (
                not result.stream_complete
                or result.exit_code is None
                or not result.dispatched
            ):
                return result, "outcome_unknown"
            return result, "completed"
        except asyncio.CancelledError:
            self.controller.cancel(str(uuid4()))
            raise
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
