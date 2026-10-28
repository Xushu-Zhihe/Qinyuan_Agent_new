"""Policy-gated fixed read-only dispatch without TaskRun-wide quiesce."""

from __future__ import annotations

from agent_core.http_contracts import plan_turn_binding
import asyncio
import base64
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from agent_core.artifact_operations import fixed_size_script
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.command_contracts import (
    AuthorizedCommand,
    SandboxCommandAuthorization,
    SandboxCommandPolicy,
    SandboxCommandRequest,
    SandboxCommandResult,
    T6CommandResult,
    T6ExecutionRecord,
)
from agent_core.control_store import PlanValidationRecordDraft, UpsertExecution
from agent_core.cross_run_artifact_contracts import D6ArtifactReference
from agent_core.domain import Execution, ExecutionStatus, PlanValidationStatus
from agent_core.dual_llm_contracts import (
    D3OperationCompletion,
    D3OperationStart,
    D3PlanBinding,
    ExecutionRecoveryRequired,
    D3RunConfig,
    D3TurnBinding,
)
from agent_core.execution_authorization import D6Policy
from agent_core.modeling import CancellationToken
from agent_core.runtime import TaskRunStatus
from agent_core.turn_modeling import digest, stable_id
from agent_core.turn_working_copy import TurnWorkingCopy


def _record_digest(record: T6ExecutionRecord) -> str:
    payload = asdict(record)
    payload["stdout"] = base64.b64encode(record.stdout).decode("ascii")
    payload["stderr"] = base64.b64encode(record.stderr).decode("ascii")
    return digest(payload)


def _request_digest(request: SandboxCommandRequest) -> str:
    return digest(
        {
            "request_id": request.request_id,
            "task_run_id": request.task_run_id,
            "plan_id": request.plan_id,
            "step_index": request.step_index,
            "script": request.script,
            "stdin_base64": base64.b64encode(request.stdin).decode("ascii"),
            "working_directory": request.working_directory,
            "timeout_seconds": request.timeout_seconds,
        }
    )


class D3FixedCommandPolicy(SandboxCommandPolicy):
    def __init__(
        self,
        config: D3RunConfig,
        turn: D3TurnBinding,
        plan: D3PlanBinding,
        ledger,
        workspace,
    ) -> None:
        self.config = config
        self.turn = turn
        self.plan = plan
        self.ledger = ledger
        self.workspace = workspace

    def authorize(self, request, authorization, state, *, now, step_limit_seconds):
        if (
            self.ledger.load_d3_run_config(self.turn.task_run_id) != self.config
            or plan_turn_binding(self.ledger,self.plan) != self.turn
            or self.ledger.load_d3_plan_binding(self.plan.plan_id) != self.plan
            or self.config.authorization.authorization_id
            != authorization.authorization_id
            or self.turn.attachment_index
            not in self.config.authorization.attachment_indices
            or request.task_run_id != self.turn.task_run_id
            or request.plan_id != self.plan.plan_id
            or self.plan.turn_id != self.turn.turn_id
            or request.stdin != b""
        ):
            raise ValueError("d3_fixed_authorization_mismatch")
        artifact = self.workspace.read_artifact(self.plan.artifact_version_id)
        claim = artifact.claim
        if (
            claim.task_run_id != self.plan.task_run_id
            or claim.turn_id != self.plan.turn_id
            or claim.intent_id != self.plan.intent_id
            or claim.plan_id != self.plan.plan_id
            or claim.import_id != self.turn.import_id
            or claim.expected_byte_size != self.turn.expected_byte_size
            or claim.expected_sha256 != self.turn.expected_sha256
            or request.working_directory != claim.path.rsplit("/", 1)[0]
        ):
            raise ValueError("d3_fixed_artifact_mismatch")
        expected_script = (
            fixed_size_script(claim.path)
            if self.plan.template == "size_then_sha256" and request.step_index == 0
            else fixed_digest_script(claim.path)
            if request.step_index
            == (1 if self.plan.template == "size_then_sha256" else 0)
            else None
        )
        active_intent = next(
            (item for item in state.intents if item.intent_id == self.plan.intent_id),
            None,
        )
        active_turn = next(
            (item for item in state.turns if item.turn_id == self.turn.turn_id), None
        )
        if (
            expected_script is None
            or request.script != expected_script
            or active_intent is None
            or active_intent.status.value != "running"
            or active_turn is None
            or active_turn.status.value != "running"
        ):
            raise ValueError("d3_fixed_command_mismatch")
        d6_authorization = self.config.d6_authorization
        if d6_authorization is not None:
            if request.task_run_id != d6_authorization.task_run_id:
                raise ValueError("d6_authorization_task_mismatch")
            try:
                source = d6_authorization.local_artifacts[self.turn.attachment_index]
            except IndexError:
                raise ValueError("d6_asset_not_authorized") from None
            if not isinstance(source, D6ArtifactReference) or (
                source.sha256 != claim.expected_sha256
                or self.turn.expected_sha256 != source.sha256
            ):
                raise ValueError("d6_asset_not_authorized")
            D6Policy(d6_authorization).authorize(
                "fixed_size"
                if request.script == fixed_size_script(claim.path)
                else "fixed_sha256",
                request.task_run_id,
                source,
            )
        return super().authorize(
            request,
            authorization,
            state,
            now=now,
            step_limit_seconds=step_limit_seconds,
        )


class D3FixedCommandGateway:
    def __init__(
        self,
        controller,
        ledger,
        commands,
        sandbox,
        policy: D3FixedCommandPolicy,
        clock: Callable[[], float],
    ) -> None:
        self.controller = controller
        self.ledger = ledger
        self.commands = commands
        self.sandbox = sandbox
        self.policy = policy
        self.clock = clock

    @property
    def state(self):
        return self.controller.control_state

    def _commit_work(self, operation, key: str):
        mutation_id = stable_id(self.state.task_run.task_run_id, key)
        return self.controller.commit_turn_operation(operation, mutation_id)

    def _work(self):
        return TurnWorkingCopy(self.state, self.commands)

    def _record(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        approved: AuthorizedCommand,
        result: T6CommandResult,
        status: str,
        command_ref: str,
        started_at: float,
    ) -> T6ExecutionRecord:
        return T6ExecutionRecord(
            request.request_id,
            request.task_run_id,
            approved.sandbox_id,
            request.plan_id,
            request.step_index,
            authorization.authorization_id,
            command_ref,
            started_at,
            max(started_at, self.clock()),
            result.exit_code,
            result.stdout,
            result.stderr,
            result.stdout_truncated,
            result.stderr_truncated,
            status == "step_timeout",
            result.termination_confirmed,
            result.stream_complete,
            approved.effective_timeout_seconds,
            status,
            result.dispatched,
        )

    @staticmethod
    def _classify(result: T6CommandResult, sandbox_id: str) -> str:
        if result.sandbox_id != sandbox_id:
            return "outcome_unknown"
        if result.timed_out and result.termination_confirmed:
            return "step_timeout"
        if (
            not result.dispatched
            or not result.termination_confirmed
            or not result.stream_complete
            or result.exit_code is None
        ):
            return "outcome_unknown"
        return "completed"

    async def _execute(
        self,
        approved: AuthorizedCommand,
        token: CancellationToken,
    ) -> T6CommandResult:
        try:
            return await asyncio.wait_for(
                self.sandbox.execute(approved, token),
                timeout=approved.effective_timeout_seconds + 3,
            )
        except (Exception, asyncio.CancelledError):
            return T6CommandResult(
                approved.sandbox_id,
                None,
                b"",
                b"",
                False,
                False,
                False,
                False,
                False,
                True,
            )

    def _complete_known(
        self,
        request: SandboxCommandRequest,
        completed: SandboxCommandResult,
    ) -> SandboxCommandResult:
        record = completed.record
        operation = self.ledger.load_d3_operation(request.request_id)
        if (
            operation is None
            or operation.start.category != "tool_dispatch"
            or operation.start.context_sha256 != _request_digest(request)
            or operation.start.resource_ref != request.request_id
            or record.task_run_id != request.task_run_id
            or record.plan_id != request.plan_id
            or record.step_index != request.step_index
        ):
            raise ExecutionRecoveryRequired("d3_tool_evidence_mismatch")
        if record.status == "outcome_unknown":
            raise ExecutionRecoveryRequired("d3_tool_outcome_unknown")
        if operation.completion is None:
            self.ledger.complete_d3_operation(
                D3OperationCompletion(
                    request.request_id,
                    record.finished_at,
                    "completed"
                    if record.status == "completed" and record.exit_code == 0
                    else "failed",
                    request.request_id,
                    _record_digest(record),
                )
            )
        elif (
            operation.completion.evidence_sha256 != _record_digest(record)
            or operation.completion.evidence_ref != request.request_id
        ):
            raise ExecutionRecoveryRequired("d3_tool_completion_mismatch")
        execution = next(
            (
                item
                for item in self.state.executions
                if item.execution_id == request.request_id
            ),
            None,
        )
        if execution is None or execution.status not in {
            ExecutionStatus.RUNNING,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
        }:
            raise ExecutionRecoveryRequired("d3_tool_execution_unavailable")
        if execution.status is ExecutionStatus.RUNNING:
            if record.status == "completed" and record.exit_code == 0:
                self._commit_work(
                    self._work().complete_execution(
                        execution_id=request.request_id,
                        output_ref=request.request_id,
                    ),
                    f"d3/execution/{request.request_id}/finish",
                )
            else:
                self.controller.commit_d3_transition(
                    "finish_d3_execution",
                    (
                        UpsertExecution(
                            execution.execution_id,
                            execution.plan_id,
                            execution.step_index,
                            execution.command_ref,
                            ExecutionStatus.TIMED_OUT
                            if record.status == "step_timeout"
                            else ExecutionStatus.FAILED,
                            request.request_id,
                        ),
                    ),
                    (request.request_id,),
                    request.request_id,
                    stable_id(
                        request.task_run_id, f"d3/execution/{request.request_id}/finish"
                    ),
                )
        if record.status != "completed" or record.exit_code != 0:
            raise ExecutionRecoveryRequired("d3_fixed_tool_failed")
        self.ledger.mark_d3_operation_applied(request.request_id, request.request_id)
        return completed

    async def call(
        self,
        request: SandboxCommandRequest,
        authorization: SandboxCommandAuthorization,
        token: CancellationToken,
    ) -> SandboxCommandResult:
        state = self.state
        if (
            token.is_cancelled
            or state.task_run.status is not TaskRunStatus.RUNNING
            or self.clock() >= state.task_run.deadline_at
        ):
            raise ExecutionRecoveryRequired("d3_tool_dispatch_stopped")
        existing = self.ledger.load_d3_operation(request.request_id)
        completed = self.commands.read_completed(request.request_id)
        if existing is not None:
            if completed is None:
                record = self.commands.read_record(request.request_id)
                if record is None or record.status == "outcome_unknown":
                    raise ExecutionRecoveryRequired("d3_tool_outcome_unknown")
                completed = SandboxCommandResult(record)
                self.commands.finish(completed)
            self.commands.replay(request, authorization, state.task_run.sandbox_id)
            return self._complete_known(request, completed)
        if (
            completed is not None
            or self.commands.read_record(request.request_id) is not None
        ):
            raise ExecutionRecoveryRequired("d3_tool_record_without_start")
        approved = self.policy.authorize(
            request,
            authorization,
            state,
            now=self.clock(),
            step_limit_seconds=60,
        )
        self.ledger.begin_d3_operation(
            D3OperationStart(
                request.request_id,
                request.task_run_id,
                "tool_dispatch",
                self.clock(),
                state.latest_checkpoint.sequence,
                _request_digest(request),
                request.request_id,
            )
        )
        self.commands.begin(request, authorization, approved.sandbox_id)
        command_ref = stable_id(request.task_run_id, f"d3/command/{request.request_id}")
        self.commands.save_command(
            command_ref, approved, ("bash", "-c", request.script)
        )
        validation = PlanValidationRecordDraft(
            stable_id(request.task_run_id, f"d3/validation/{request.request_id}"),
            request.plan_id,
            request.task_run_id,
            request.step_index,
            1,
            PlanValidationStatus.SUCCEEDED,
            None,
            "d3_fixed_command_authorized",
            command_ref,
        )
        self._commit_work(
            self._work().append_plan_validation_record(validation),
            f"d3/validation/{request.request_id}/commit",
        )
        self._commit_work(
            self._work().create_execution(
                Execution(
                    request.request_id, request.plan_id, request.step_index, command_ref
                ),
                plan_validation_id=validation.plan_validation_id,
            ),
            f"d3/execution/{request.request_id}/create",
        )
        self._commit_work(
            self._work().start_execution(request.request_id),
            f"d3/execution/{request.request_id}/start",
        )
        started_at = self.clock()
        result = await self._execute(
            replace(
                approved,
                deadline_monotonic=time.monotonic()
                + approved.effective_timeout_seconds,
            ),
            token,
        )
        status = self._classify(result, approved.sandbox_id)
        if (
            token.is_cancelled
            or self.state.task_run.status is not TaskRunStatus.RUNNING
            or self.clock() >= self.state.task_run.deadline_at
        ):
            status = "outcome_unknown"
        record = self._record(
            request,
            authorization,
            approved,
            result,
            status,
            command_ref,
            started_at,
        )
        self.commands.save_record(record)
        completed = SandboxCommandResult(record)
        self.commands.finish(completed)
        return self._complete_known(request, completed)
