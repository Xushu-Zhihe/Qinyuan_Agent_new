"""D9-4 candidate checkpoint between local success and child expansion."""

from agent_core.http_contracts import plan_turn_binding
from dataclasses import asdict, dataclass

from agent_core.domain import IntentStatus, ExecutionStatus, PlanStatus
from agent_core.dual_llm_contracts import D3PlanBinding, D3PlanResult, D3TurnBinding, D3RecoveryRequired
from agent_core.turn_modeling import digest
from agent_core.dual_llm_modeling import D3CandidateContext, D3ModelRequest, D8SessionExecutionRequest
from agent_core.goal_feedback import project_candidate_feedback


@dataclass(frozen=True)
class AnswerCandidateContext(D3CandidateContext):
    goal: str


class AnswerCandidates:
    def __init__(self, app, protected_factory):
        self.app, self.store = app, app.store
        self.evidence = app.output_boundary.evidence
        self.protected_factory = protected_factory

    def _identity(self, intent_id):
        return self.app._id('d9-4/candidate/' + intent_id)

    def _request(self, intent):
        from agent_core.competition_candidates import MCPAnswerCandidateContext,provenance,validate_mcp_source
        identity = self._identity(intent.intent_id)
        saved = self.app.sessions.evidence.read('request', identity)
        if saved is not None:
            try:
                data = dict(saved['request'])
                context = dict(data['context'])
                from agent_core.competition_candidates import restore_context
                data['context'] = restore_context(context)
                data['process_facts'] = tuple(data['process_facts'])
                request = D8SessionExecutionRequest(**data)
            except (TypeError, ValueError, KeyError) as exc:
                raise D3RecoveryRequired('d9_candidate_request_invalid') from exc
            if (request.model_request_id != identity or request.task_run_id != self.app.state.task_run.task_run_id
                    or provenance(request.context)['intent_id'] != intent.intent_id
                    or provenance(request.context)['turn_id'] != intent.turn_id
                    or request.context.goal != self.app.state.task_run.request.goal):
                raise D3RecoveryRequired('d9_candidate_request_binding_changed')
            self.app.sessions.freeze(request)
            return request
        if self.store.load_d3_operation(identity) is not None:
            raise D3RecoveryRequired('d9_candidate_frozen_request_missing')
        state = self.app.state
        plans = {p.plan_id: p for p in state.plans
                 if p.intent_id == intent.intent_id and p.status is PlanStatus.COMPLETED}
        execution = next((e for e in reversed(state.executions)
                          if e.plan_id in plans and e.status is ExecutionStatus.SUCCEEDED
                          and e.output_ref is not None), None)
        mcp = [r for r in self.store.list_d4_operations(state.task_run.task_run_id)
               if (r.start.resource_ref or '').startswith('mcp/'+intent.intent_id+'/') and r.completion is not None
               and r.completion.result_kind == 'completed'] if self.app.config.contract_version == 22 else []
        latest = mcp[-1] if mcp else None
        # Serialized execution order comes from the durable ledger, not an
        # absent ExecutionSnapshot.finished_at or ambiguous equal timestamps.
        order = {r.start.operation_id: n for n, r in enumerate(self.store.list_d4_operations(state.task_run.task_run_id))} if latest else {}
        if latest is not None and execution is not None and execution.execution_id not in order:
            raise D3RecoveryRequired('d9_candidate_source_missing')
        if latest is not None and (execution is None or order[latest.start.operation_id] > order[execution.execution_id]):
            context=MCPAnswerCandidateContext(None,None,None,None,state.task_run.request.goal,
                state.task_run.task_run_id,intent.turn_id,intent.intent_id,
                latest.completion.evidence_ref,latest.completion.evidence_sha256)
            from types import SimpleNamespace
            validate_mcp_source(self.store,SimpleNamespace(**provenance(context)),self.evidence)
        else:
            if execution is None:
                raise D3RecoveryRequired('d9_candidate_source_missing')
            binding = self.store.load_d3_plan_binding(execution.plan_id)
            result = self.store.load_d3_plan_result(execution.plan_id)
            turn = plan_turn_binding(self.store,binding) if binding else None
            if (binding is None or result is None or turn is None or result.outcome not in
                    ({'satisfied','no_new_information','observed'} if self.app.config.contract_version==22 else {'satisfied'})):
                raise D3RecoveryRequired('d9_candidate_source_not_satisfied')
            context = AnswerCandidateContext(turn, binding, execution.execution_id, result, state.task_run.request.goal)
        role = self.app.config.inner_role
        request = D3ModelRequest(identity, state.task_run.task_run_id, state.latest_checkpoint.sequence,
            role.role, role.role_config_ref, 'answer-candidate/' + intent.intent_id, 'inner_candidate', context)
        return self.app.sessions.wrap(self.app.handoff.wrap_execution_request(request))

    async def after_success(self, intent, token):
        if intent.status is not IntentStatus.SUCCEEDED:
            raise D3RecoveryRequired('d9_candidate_requires_local_success')
        request = self._request(intent)
        identity, candidate = await self.app.inner.propose_candidate(request, token, self.protected_factory())
        if token.is_cancelled:
            raise D3RecoveryRequired('d9_candidate_registration_cancelled')
        model = self.app.model_evidence.read(identity)
        decision = {'version': 1, 'task_run_id': request.task_run_id, 'intent_id': intent.intent_id,
                    'model_request_id': identity, 'request_sha256': digest(asdict(request)),
                    'result_sha256': digest(asdict(model)),
                    'candidate': asdict(candidate) if candidate is not None else None}
        self.evidence.save('answer-decision', identity, decision)
        if candidate is None:
            self.store.mark_d3_operation_applied(identity, 'd9_no_candidate')
            self.app.sessions.reconcile()
            return False
        registered = self.store.load_d7_answer_candidate(candidate.candidate_id)
        if registered is None:
            self.app.controller.register_answer_candidate(candidate,
                self.app._id('d9-4/register/' + candidate.candidate_id))
            if self.store.load_d7_answer_candidate(candidate.candidate_id) is None:
                return True
        elif registered != candidate:
            raise D3RecoveryRequired('d9_registered_candidate_changed')
        self.store.mark_d3_operation_applied(identity, candidate.candidate_id)
        if self.app.state.task_run.status.value == 'running':
            self.app.sessions.reconcile()
        rows = self.store.list_d7_validation_feedback(request.task_run_id, candidate.candidate_id,
                                                       candidate.evidence_version)
        projection = project_candidate_feedback(request.task_run_id, candidate.candidate_id,
                                                candidate.evidence_version, rows)
        return projection.state not in {'rejected','partial'}

    async def reconcile_known(self, token):
        from agent_core.completion_evaluation import check_candidate_task
        check_candidate_task(self.app.config, self.evidence, self.app.sessions.evidence,
                             self.app.model_evidence, self.store, self.app.state.task_run.task_run_id)
        for operation in self.store.list_d3_operations(self.app.state.task_run.task_run_id):
            if (operation.application_ref is not None and operation.start.resource_ref.startswith('answer-candidate/')
                    and self.evidence.read('answer-decision', operation.start.operation_id) is None):
                raise D3RecoveryRequired('d9_candidate_decision_missing')
            if (operation.start.category == 'inner_model'
                    and operation.start.resource_ref.startswith('answer-candidate/')
                    and operation.application_ref is None):
                intent_id = operation.start.resource_ref.removeprefix('answer-candidate/')
                intent = next((i for i in self.app.state.intents if i.intent_id == intent_id), None)
                if intent is None or operation.start.operation_id != self._identity(intent_id):
                    raise D3RecoveryRequired('d9_candidate_operation_owner_changed')
                await self.after_success(intent, token)

    def feedback_history(self):
        rows = []
        task = self.app.state.task_run.task_run_id
        for candidate in self.store.list_d7_answer_candidates(task):
            feedback = self.store.list_d7_validation_feedback(task, candidate.candidate_id, candidate.evidence_version)
            projection = project_candidate_feedback(task, candidate.candidate_id, candidate.evidence_version, feedback)
            for record in feedback:
                rows.append((candidate, record, projection.state))
        return rows

    async def apply_feedback(self, feedback, token):
        """Trusted in-process adapter boundary; never accepts a public verdict file."""
        if token.is_cancelled:
            raise D3RecoveryRequired('d9_feedback_cancelled_before_application')
        from agent_core.controller import CommittedStateReloadError
        try:
            self.app.controller.apply_external_validation_feedback(feedback)
        except CommittedStateReloadError:
            return self.app.result('d9_feedback_reload_required', recovery_required=True)
        if self.app._d7_task_has_feedback_conflict(feedback.task_run_id):
            return self.app.result('d9_feedback_conflict', recovery_required=True)
        if self.app.state.task_run.status.value == 'running':
            return await self.app.run(token)
        return self.app.result('d9_feedback_' + feedback.verdict)

    def check(self, selection, checker):
        task = self.app.state.task_run.task_run_id
        request = check_request(self.store, task, selection, self.app.state.latest_checkpoint.sequence + 1)
        return self.app.controller.run_external_validation_check(request, checker)


def check_request(store, task, selection, checkpoint):
    from agent_core.goal_feedback import (ExternalValidationCheckRequest,
        ExternalValidationConflictSource, validate_external_check_binding)
    prior = store.load_d7_external_validation_check(selection['query_id'])
    if prior is not None:
        request = prior.request
        if any(getattr(request, k) != v for k, v in selection.items()) or request.task_run_id != task:
            raise D3RecoveryRequired('d9_validation_query_identity_changed')
    else:
        candidate = store.load_d7_answer_candidate(selection['candidate_id'])
        if candidate is None or candidate.task_run_id != task or candidate.evidence_version != selection['evidence_version']:
            raise D3RecoveryRequired('d9_validation_candidate_binding_changed')
        records = store.list_d7_validation_feedback(task, candidate.candidate_id, candidate.evidence_version)
        projection = project_candidate_feedback(task, candidate.candidate_id, candidate.evidence_version, records)
        by_id = {r.operation_id: r for r in records}
        conflicts = tuple(ExternalValidationConflictSource.from_record(by_id[i])
                          for i in projection.conflict_operation_ids)
        request = ExternalValidationCheckRequest(task_run_id=task,
            checkpoint_sequence=checkpoint, conflict_sources=conflicts, **selection)
        validate_external_check_binding(request, records)
    return request


def check_no_candidate(config, output, sessions, native, operation):
    """Read-only validation; repairing a saved result remains an explicit operation."""
    identity = operation.start.operation_id
    frozen = sessions.read('request', identity)
    contract = output.read('contract', identity)
    if not isinstance(frozen, dict) or not isinstance(contract, dict):
        raise D3RecoveryRequired('d9_candidate_frozen_request_missing')
    request = frozen['request']
    if (contract.get('source_request_sha256') != digest(request)
            or native.model_request_id != identity or native.task_run_id != operation.start.task_run_id
            or native.stage != 'inner_candidate' or native.role_config_ref != config.inner_role.role_config_ref
            or native.context_sha256 != operation.start.context_sha256
            or native.base_checkpoint_sequence != operation.start.base_checkpoint_sequence
            or native.resource_ref != operation.start.resource_ref):
        raise D3RecoveryRequired('d9_no_candidate_binding_changed')
    completion = operation.completion
    if completion is not None and (completion.result_kind != 'completed'
            or completion.evidence_ref != identity or completion.evidence_sha256 != digest(asdict(native))):
        raise D3RecoveryRequired('d9_no_candidate_completion_changed')
    decision = output.read('answer-decision', identity)
    expected = {'version': 1, 'task_run_id': operation.start.task_run_id,
                'intent_id': request['context'].get('intent_id') or request['context']['plan_binding']['intent_id'],
                'model_request_id': identity, 'request_sha256': digest(request),
                'result_sha256': digest(asdict(native)), 'candidate': None}
    if decision is not None and decision != expected:
        raise D3RecoveryRequired('d9_candidate_decision_changed')
    if operation.application_ref is not None and operation.application_ref != 'd9_no_candidate':
        raise D3RecoveryRequired('d9_candidate_application_changed')
    return completion is None or decision is None or operation.application_ref is None
