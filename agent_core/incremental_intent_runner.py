"""Run one Intent through audited, incremental T6 model decisions."""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import uuid4

from agent_core.action_invocation import AuditedT6ActionModel
from agent_core.action_model_request import build_action_model_request
from agent_core.command_contracts import (
    SandboxCommandAuthorization, SandboxCommandRequest, T6CommandRecoveryRequired,
)
from agent_core.command_gateway import T6CommandGateway
from agent_core.control_state_repository import ControlStateRepository
from agent_core.control_store import ControlStore
from agent_core.controller import TaskRunController
from agent_core.domain import ExecutionStatus, FailureType, IntentStatus, Plan, PlanStatus, PlanStep
from agent_core.intent_postconditions import (
    CheckStatus, IntentPostconditions, T6PostconditionVerifier,
)
from agent_core.modeling import CancellationToken, ModelError
from agent_core.observation_proof import T6ObservationProofVerifier
from agent_core.pending_candidates import PendingCandidateRecorder, PendingCandidateStorePort
from agent_core.runtime import TaskRunStatus
from agent_core.supervisor_contracts import IntentRunOutcome, IntentRunResult
from agent_core.action_audit import ActionInvocationRecord, ActionRecoveryRequired, T6ActionJournal
from agent_core.actions import (
    AbandonIntentAction, CandidateFlagAction, CompleteIntentAction, ProbeAction, T6Action,
)
from agent_core.turn_working_copy import TurnWorkingCopy


class IncrementalIntentRunner:
    """One selected Intent; each proposal is immutable before any side effect."""

    def __init__(
        self, *, controller: TaskRunController, store: ControlStore,
        repository: ControlStateRepository, journal: T6ActionJournal,
        model: AuditedT6ActionModel, gateway: T6CommandGateway,
        postconditions: T6PostconditionVerifier,
        proofs: T6ObservationProofVerifier,
        candidates: PendingCandidateRecorder,
        candidate_store: PendingCandidateStorePort,
        authorization: SandboxCommandAuthorization,
        cancellation_token: CancellationToken,
        clock: Callable[[], float] = time.time,
        fixture_budget=None,
    ) -> None:
        self.controller = controller
        self.store = store
        self.repository = repository
        self.journal = journal
        self.model = model
        self.gateway = gateway
        self.postconditions = postconditions
        self.proofs = proofs
        self.candidates = candidates
        self.candidate_store = candidate_store
        self.authorization = authorization
        self.token = cancellation_token
        self.clock = clock
        self.fixture_budget = fixture_budget

    @property
    def state(self):
        return self.controller.control_state

    def _result(self, intent_id: str, outcome: IntentRunOutcome, *, plan_id: str | None = None,
                evidence_ref: str | None = None, reason: str = "") -> IntentRunResult:
        return IntentRunResult(self.state.task_run.task_run_id, intent_id, outcome,
                               plan_id, evidence_ref, reason[:256])

    def _work(self) -> TurnWorkingCopy:
        return TurnWorkingCopy(self.state, self.gateway.recorder)

    def _commit(self, mutation_id: str, operation: str, operation_factory) -> None:
        prior = self.store.load_committed_mutation(
            self.state.task_run.task_run_id, mutation_id,
        )
        if prior is not None:
            if prior.event.details.get("operation") != operation:
                raise ActionRecoveryRequired("action mutation belongs to another operation")
            return
        self.controller.commit_turn_operation(operation_factory(), mutation_id)

    def _reconcile(self, record: ActionInvocationRecord, action: T6Action | ModelError) -> None:
        """Reject commits unrelated to the saved action before applying it."""
        start = record.start
        resources = start.resource_ids
        if self.state.latest_checkpoint.sequence < start.base_checkpoint_sequence:
            raise ActionRecoveryRequired("action checkpoint rolled back")
        known = {
            resources.register_mutation_id: "register_plan",
            resources.start_mutation_id: "start_plan",
            resources.complete_plan_mutation_id: "complete_plan",
            resources.fact_mutation_id: "commit_verified_t6_fact",
        }
        if isinstance(action, CompleteIntentAction):
            known[resources.complete_intent_mutation_id] = "complete_verified_t6_intent"
        elif isinstance(action, AbandonIntentAction):
            known[resources.complete_intent_mutation_id] = "record_target_mismatch"
        plan_id = self._probe_plan_id(record) if isinstance(action, ProbeAction) else None
        gateway_operations = {
            "append_plan_validation_record", "create_execution", "start_execution",
            "complete_execution", "record_step_timeout", "record_execution_failure",
            "record_command_generation_failure",
        }
        for mutation_id, commit in self.store.committed_mutations_after(
            start.task_run_id, start.base_checkpoint_sequence,
        ):
            operation = commit.event.details.get("operation")
            entity_ids = commit.event.details.get("entity_ids", [])
            if mutation_id in known:
                if operation != known[mutation_id]:
                    raise ActionRecoveryRequired("action mutation changed operation")
            elif operation in gateway_operations:
                if plan_id is None or not self._gateway_commit_belongs_to_action(
                    entity_ids, plan_id, resources.command_request_id,
                ):
                    raise ActionRecoveryRequired("unrelated gateway checkpoint")
            else:
                raise ActionRecoveryRequired("unrelated checkpoint after model action")

    def _gateway_commit_belongs_to_action(self, entity_ids, plan_id: str, request_id: str) -> bool:
        if not isinstance(entity_ids, list) or not entity_ids:
            return False
        scoped = {plan_id, request_id}
        scoped.update(
            item.plan_validation_id for item in self.state.plan_validation_records
            if item.plan_id == plan_id and item.step_index == 0
        )
        return bool(set(entity_ids) & scoped)

    def _probe_plan_id(self, record: ActionInvocationRecord) -> str:
        own_id = record.start.resource_ids.plan_id
        if any(item.plan_id == own_id for item in self.state.plans):
            return own_id
        history = self.journal.list_for_intent(record.start.task_run_id, record.start.intent_id)
        for prior in reversed(history):
            if prior.start.model_request_id == record.start.model_request_id:
                continue
            if prior.applied_at is None:
                continue
            prior_action = self.journal.read_result(prior)
            if not isinstance(prior_action, ProbeAction):
                continue
            prior_id = prior.start.resource_ids.plan_id
            receipt = self.gateway.recorder.read_record(prior.start.resource_ids.command_request_id)
            if (receipt is not None and receipt.status == "step_timeout"
                    and any(item.plan_id == prior_id for item in self.state.plans)):
                return prior_id
            break
        return own_id

    def _last_retry(self, intent_id: str):
        plans = [item for item in self.state.plans if item.intent_id == intent_id]
        if not plans or plans[-1].status is not PlanStatus.RUNNING:
            return None, None
        plan = plans[-1]
        records = self.journal.list_for_intent(self.state.task_run.task_run_id, intent_id)
        previous = next((item for item in reversed(records)
                         if item.start.resource_ids.plan_id == plan.plan_id
                         and item.applied_at is not None), None)
        if previous is None:
            raise ActionRecoveryRequired("running Plan has no completed action")
        action = self.journal.read_result(previous)
        if not isinstance(action, ProbeAction):
            raise ActionRecoveryRequired("running Plan is not a probe")
        return action, previous.start.resource_ids.command_request_id

    def _request(self, intent_id: str):
        previous, command_id = self._last_retry(intent_id)
        return build_action_model_request(
            self.state,
            self.repository.query_model_context(self.state.task_run.task_run_id),
            intent_id, str(uuid4()), self.gateway.recorder, self.candidate_store,
            self.postconditions.store, previous, command_id,
        )

    async def run(self, intent_id: str) -> IntentRunResult:
        return await self.resume(intent_id)

    def reconcile_terminal_actions(self) -> None:
        """Finish an audit marker after its terminal control commit survived a crash."""
        task_id = self.state.task_run.task_run_id
        for intent in self.state.intents:
            saved = self.journal.resume_unapplied(task_id, intent.intent_id)
            if saved is None:
                continue
            record, action = saved
            if intent.status not in {IntentStatus.SUCCEEDED, IntentStatus.FAILED}:
                continue
            self._reconcile(record, action)
            ids = record.start.resource_ids
            if isinstance(action, CompleteIntentAction):
                commit = self.store.load_committed_mutation(
                    task_id, ids.complete_intent_mutation_id,
                )
                if (intent.status is not IntentStatus.SUCCEEDED or commit is None
                        or commit.event.details.get("operation") != "complete_verified_t6_intent"):
                    raise ActionRecoveryRequired("Intent completion audit is incomplete")
            elif isinstance(action, AbandonIntentAction):
                commit = self.store.load_committed_mutation(
                    task_id, ids.complete_intent_mutation_id,
                )
                if (intent.status is not IntentStatus.FAILED or commit is None
                        or commit.event.details.get("operation") != "record_target_mismatch"
                        or not any(item.failure_type is FailureType.TARGET_MISMATCH
                                   and item.evidence_ref == action.execution_id
                                   for item in self.state.failure_summaries)):
                    raise ActionRecoveryRequired("abandon audit is incomplete")
            elif isinstance(action, ProbeAction):
                plan_id = self._probe_plan_id(record)
                plan = next((item for item in self.state.plans
                             if item.plan_id == plan_id), None)
                receipt = self.gateway.recorder.read_record(ids.command_request_id)
                if (intent.status is not IntentStatus.FAILED or plan is None
                        or plan.status is not PlanStatus.FAILED
                        or receipt is None or receipt.status != "step_timeout"
                        or receipt.plan_id != plan_id or not receipt.termination_confirmed):
                    raise ActionRecoveryRequired("terminal timeout audit is incomplete")
            else:
                raise ActionRecoveryRequired("unapplied terminal action")
            self.journal.mark_applied(record.start.model_request_id, self.clock())

    async def resume(self, intent_id: str) -> IntentRunResult:
        while True:
            if self.token.is_cancelled or self.state.task_run.status is not TaskRunStatus.RUNNING:
                return self._result(intent_id, "recovery_required", reason="task_not_running")
            if self.clock() >= self.state.task_run.deadline_at:
                self.controller.advance_time(str(uuid4()))
                return self._result(intent_id, "recovery_required", reason="deadline_exceeded")
            saved = self.journal.resume_unapplied(self.state.task_run.task_run_id, intent_id)
            if saved is None:
                if self.fixture_budget is not None and not self.fixture_budget.next_action_available():
                    return self._result(intent_id, "inconclusive", reason="fixture_exhausted")
                request = self._request(intent_id)
                proposed = self.model.propose(
                    request, self.state.latest_checkpoint.sequence,
                    self.state.task_run.deadline_at, self.token,
                )
                record, action = proposed.record, proposed.result
            else:
                record, action = saved
            self._reconcile(record, action)
            if isinstance(action, ModelError):
                self.journal.mark_applied(record.start.model_request_id, self.clock())
                if action.short_reason == "fixture_exhausted":
                    return self._result(intent_id, "inconclusive", reason="fixture_exhausted")
                return self._result(intent_id, "model_error", reason=action.short_reason)
            outcome = await self._apply(intent_id, record, action)
            if outcome is not None:
                return outcome

    async def _apply(self, intent_id: str, record: ActionInvocationRecord,
                     action: T6Action) -> IntentRunResult | None:
        ids = record.start.resource_ids
        if isinstance(action, ProbeAction):
            return await self._probe(intent_id, record, action)
        if isinstance(action, CandidateFlagAction):
            self.candidates.record(
                self.state, intent_id, action, ids.candidate_id, self.clock(),
            )
            self.journal.mark_applied(record.start.model_request_id, self.clock())
            return None
        if isinstance(action, CompleteIntentAction):
            self.controller.complete_verified_t6_intent(
                intent_id, action.fact_ids, ids.complete_intent_mutation_id,
            )
            self.journal.mark_applied(record.start.model_request_id, self.clock())
            return self._result(intent_id, "succeeded")
        if isinstance(action, AbandonIntentAction):
            execution = next((item for item in self.state.executions
                              if item.execution_id == action.execution_id), None)
            plan = next((item for item in self.state.plans
                         if execution is not None and item.plan_id == execution.plan_id), None)
            if (execution is None or execution.status is not ExecutionStatus.SUCCEEDED
                    or plan is None or plan.intent_id != intent_id
                    or plan.status is not PlanStatus.COMPLETED):
                raise ActionRecoveryRequired("abandon evidence is not a completed Intent execution")
            self._commit(ids.complete_intent_mutation_id, "record_target_mismatch",
                         lambda: self._work().record_target_mismatch(
                             plan_id=plan.plan_id, evidence_ref=action.execution_id,
                             short_reason=action.short_reason,
                         ))
            self.journal.mark_applied(record.start.model_request_id, self.clock())
            return self._result(intent_id, "target_mismatch_committed",
                                plan_id=plan.plan_id, evidence_ref=action.execution_id,
                                reason=action.short_reason)
        raise ActionRecoveryRequired("unsupported saved action")

    async def _probe(self, intent_id: str, record: ActionInvocationRecord,
                     action: ProbeAction) -> IntentRunResult | None:
        ids = record.start.resource_ids
        plan_id = self._probe_plan_id(record)
        retry = plan_id != ids.plan_id
        if retry:
            binding = self.postconditions.store.read(plan_id, intent_id)
            if (binding is None) != (not action.conditions) or (
                binding is not None and binding.conditions != action.conditions
            ):
                raise ActionRecoveryRequired("retry conditions changed")
        else:
            self._commit(ids.register_mutation_id, "register_plan",
                         lambda: self._work().register_plan(
                             Plan(plan_id, intent_id, (PlanStep("incremental probe"),)),
                         ))
            self._commit(ids.start_mutation_id, "start_plan",
                         lambda: self._work().start_plan(plan_id))
            if action.conditions:
                binding = IntentPostconditions(
                    self.state.task_run.task_run_id, intent_id, plan_id,
                    action.conditions,
                )
                saved_binding = self.postconditions.store.read(plan_id, intent_id)
                if saved_binding is None:
                    if next(item for item in self.state.plans
                            if item.plan_id == plan_id).status is not PlanStatus.RUNNING:
                        raise ActionRecoveryRequired("postconditions were not fixed before execution")
                    self.postconditions.bind(binding)
                elif saved_binding != binding:
                    raise ActionRecoveryRequired("postcondition binding differs from saved action")
        command = SandboxCommandRequest(
            ids.command_request_id, self.state.task_run.task_run_id, plan_id,
            0, action.script, action.stdin, action.working_directory,
            action.timeout_seconds,
        )
        try:
            result = await self.gateway.call(command, self.authorization, self.token)
        except T6CommandRecoveryRequired as exc:
            return self._result(intent_id, "recovery_required", reason=str(exc))
        status = result.record.status
        if status == "outcome_unknown":
            return self._result(intent_id, "recovery_required", reason=status)
        if status == "environment_fault" or result.action is not None and result.action.value == "retry_execution":
            self.journal.mark_applied(record.start.model_request_id, self.clock())
            return self._result(intent_id, "inconclusive", reason="environment_fault")
        if status == "step_timeout":
            self.journal.mark_applied(record.start.model_request_id, self.clock())
            if result.action is not None and result.action.value == "retry_step_command":
                return None
            evidence_ref = ids.command_request_id
            if not result.record.dispatched:
                validations = [item for item in self.state.plan_validation_records
                               if item.plan_id == plan_id and item.step_index == 0]
                if not validations:
                    raise ActionRecoveryRequired("timeout validation missing")
                evidence_ref = validations[-1].plan_validation_id
            return self._result(intent_id, "step_timeout", plan_id=plan_id,
                                evidence_ref=evidence_ref, reason="step_timeout_retry_exhausted")
        if status != "completed":
            self.journal.mark_applied(record.start.model_request_id, self.clock())
            return self._result(intent_id, "inconclusive", reason=status)
        self._commit(ids.complete_plan_mutation_id, "complete_plan",
                     lambda: self._work().complete_plan(plan_id))
        if action.conditions:
            condition_result = self.postconditions.verify(intent_id)
            if condition_result is CheckStatus.INCONCLUSIVE:
                return self._result(intent_id, "inconclusive", reason="postconditions_inconclusive")
            if condition_result is CheckStatus.SATISFIED:
                proof = self.proofs.issue(intent_id, ids.proof_id, plan_id)
                if proof.plan_id != plan_id:
                    raise ActionRecoveryRequired("observation proof plan changed")
                self.controller.commit_verified_t6_observation(
                    intent_id, ids.proof_id, ids.fact_id, ids.fact_mutation_id,
                    complete_intent=False,
                )
        self.journal.mark_applied(record.start.model_request_id, self.clock())
        return None
