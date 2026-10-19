"""Structural store gates for HTTP mutations; file evidence is checked by the attached capability."""
from dataclasses import asdict
from agent_core.http_contracts import HttpTurnBinding,HttpPlanBinding,is_http
from agent_core.control_store import StorageError,UpsertPlan,UpsertExecution,UpsertFact,AppendPlanValidationRecord
from agent_core.domain import PlanStatus,ExecutionStatus,IntentStatus,TurnStatus,PlanValidationStatus,FactStatus
from agent_core.dual_llm_contracts import D3CommitMetadata,D3Outcome
from agent_core.conclusion_contracts import D8Conclusion
from agent_core.turn_modeling import digest


def valid_target(config,request):
    return (is_http(config) and (not request.target.attachments or config.contract_version in {21,22}
            and len(request.target.attachments)==len(config.authorization.attachment_indices)
            and all(a.kind=='local_path' for a in request.target.attachments)) and len(request.target.endpoints)==1
            and request.target.endpoints[0].url==config.web_authorization.allowed_origin)


def valid_turn(config,binding):
    from agent_core.hybrid_contracts import HybridTurnBinding
    return (is_http(config) and isinstance(binding,HttpTurnBinding)
            and (not isinstance(binding,HybridTurnBinding) or config.contract_version in {21,22}
                 and tuple(b.attachment_index for b in binding.attachments)==config.authorization.attachment_indices)
            and binding.endpoint==config.web_authorization.allowed_origin
            and binding.authorization_id==config.web_authorization.authorization_id)


def validate_mutation(store,mutation,config):
    if not is_http(config):return False
    operation=mutation.operation
    if operation not in {'register_d3_plan','replan_d3_plan','append_plan_validation_record',
                         'create_execution','start_execution','finish_d3_execution','complete_d3_plan'}:
        return False
    from agent_core.hybrid_contracts import AttachmentPlanBinding,attachment_binding
    candidate=mutation.d3.plan_binding if mutation.d3 and mutation.d3.plan_binding else None
    if candidate is None:
        plan_id=next((c.record.plan_id if isinstance(c,AppendPlanValidationRecord) else c.plan_id
            for c in mutation.state_changes if isinstance(c,(UpsertPlan,UpsertExecution,AppendPlanValidationRecord))),None)
        candidate=store.load_d3_plan_binding(plan_id) if plan_id else None
    if isinstance(candidate,AttachmentPlanBinding):
        turn=store.load_d3_turn_binding(candidate.turn_id)
        selected=attachment_binding(turn,candidate.attachment_index)
        if (config.contract_version not in {21,22} or not valid_turn(config,turn)
                or candidate.rule_binding_sha256!=digest(asdict(selected))):
            raise StorageError('d21_attachment_plan_not_authorized')
        return False  # The original file store gates still run.
    rows=store.load_control_state_rows(mutation.task_run_id)
    changes=mutation.state_changes
    metadata=mutation.d3
    if rows is None or rows.task_run.status.value!='running' or mutation.d7 is not None:
        raise StorageError('d20_http_mutation_outside_active_run')
    if operation not in {'register_d3_plan','replan_d3_plan','complete_d3_plan'} and metadata is not None:
        raise StorageError('d20_http_unexpected_metadata')
    if operation in {'register_d3_plan','replan_d3_plan'}:
        binding=metadata.plan_binding if metadata else None
        plans=[c for c in changes if isinstance(c,UpsertPlan)]
        if (not isinstance(binding,HttpPlanBinding) or not plans or len(plans)!=len(changes)
                or metadata!=D3CommitMetadata(plan_binding=binding,outcomes=metadata.outcomes)):
            raise StorageError('d20_http_plan_binding_required')
        owner=next((i for i in rows.intents if i.intent_id==binding.intent_id),None)
        prior=[p for p in rows.plans if p.intent_id==binding.intent_id]
        previous=store.load_d3_plan_result(prior[-1].plan_id) if prior else None
        continuation=(config.contract_version==22 and previous is not None
                      and previous.outcome=='no_new_information')
        rolling = bool(config.intent_execution_policy and prior and prior[-1].status is PlanStatus.COMPLETED and binding.replan_review_id is None)
        plan=plans[-1]
        turn=store.load_d3_turn_binding(binding.turn_id)
        if (owner is None or owner.status is not IntentStatus.RUNNING or owner.turn_id!=binding.turn_id
                or binding.task_run_id!=mutation.task_run_id or not valid_turn(config,turn)
                or binding.rule_binding_sha256!=digest(asdict(turn)) or len(prior)>=2 and not continuation and not config.intent_execution_policy
                or (plan.plan_id,plan.intent_id,plan.status)!=(binding.plan_id,binding.intent_id,PlanStatus.RUNNING)
                or len(plan.steps)!=1 or plan.steps[0].description!='authorized HTTP request'
                or bool(prior and not rolling)!=(operation=='replan_d3_plan')
                or bool(prior and not rolling)!=(binding.replan_review_id is not None)
                or len(plans)!=(2 if prior and not rolling else 1)
                or any(p.plan_id==binding.plan_id for p in rows.plans)
                or next(t for t in rows.turns if t.turn_id==binding.turn_id).status is not TurnStatus.RUNNING):
            raise StorageError('d20_http_plan_scope_changed')
        proposal=store.load_d3_operation(binding.proposal_request_id)
        if (proposal is None or proposal.start.task_run_id!=mutation.task_run_id
                or proposal.start.category!='inner_model' or proposal.completion is None
                or proposal.completion.result_kind!='completed' or proposal.application_ref is not None):
            raise StorageError('d20_http_plan_proposal_missing')
        if prior and not rolling:
            if continuation:
                approved=any(r.plan_id==prior[-1].plan_id and r.outcome=='no_new_information'
                             and r.review_request_id==binding.replan_review_id
                             for r in store.list_d8_conclusions(mutation.task_run_id))
            else:
                review=store.load_d3_review_application(mutation.task_run_id,binding.replan_review_id)
                approved=(review is not None and review.intent_id==binding.intent_id
                          and review.decision=='replan_current_intent' and not review.paused_gate)
            reason='no_new_information' if continuation else 'replanned'
            if (not approved or len(plans)!=2 or plans[0].plan_id!=prior[-1].plan_id
                    or plans[0].status is not PlanStatus.SUPERSEDED
                    or plans[0].intent_id!=prior[-1].intent_id or plans[0].steps!=prior[-1].steps
                    or metadata.outcomes!=(D3Outcome('plan',prior[-1].plan_id,'superseded',reason,
                        (binding.replan_review_id,binding.proposal_request_id)),)):
                raise StorageError('d20_http_replan_review_missing')
        elif metadata.outcomes:
            raise StorageError('d20_http_unexpected_plan_outcomes')
    elif operation=='append_plan_validation_record':
        if len(changes)!=1 or not isinstance(changes[0],AppendPlanValidationRecord):
            raise StorageError('d20_http_validation_shape_invalid')
        change=changes[0].record
        binding=store.load_d3_plan_binding(change.plan_id)
        if (not isinstance(binding,HttpPlanBinding) or change.command_ref!='http-request:'+binding.request_sha256
                or change.task_run_id!=mutation.task_run_id or change.step_index!=0 or change.attempt!=1
                or change.status is not PlanValidationStatus.SUCCEEDED or change.failure_type is not None
                or change.short_reason!='authorized_http_request'):
            raise StorageError('d20_http_validation_binding_changed')
    elif operation in {'create_execution','start_execution','finish_d3_execution'}:
        if len(changes)!=1 or not isinstance(changes[0],UpsertExecution):
            raise StorageError('d20_http_execution_shape_invalid')
        change=changes[0]
        binding=store.load_d3_plan_binding(change.plan_id)
        old=next((e for e in rows.executions if e.execution_id==change.execution_id),None)
        status={'create_execution':ExecutionStatus.PENDING,'start_execution':ExecutionStatus.RUNNING,
                'finish_d3_execution':ExecutionStatus.SUCCEEDED}[operation]
        expected={'create_execution':None,'start_execution':ExecutionStatus.PENDING,
                  'finish_d3_execution':ExecutionStatus.RUNNING}[operation]
        if (not isinstance(binding,HttpPlanBinding) or binding.execution_id!=change.execution_id
                or change.step_index!=0 or change.command_ref!='http-request:'+binding.request_sha256
                or change.status is not status or (old.status if old else None) is not expected
                or change.output_ref!=('http-response:'+change.execution_id if status is ExecutionStatus.SUCCEEDED else None)):
            raise StorageError('d20_http_execution_binding_changed')
    else:
        result=metadata.plan_result if metadata else None
        binding=store.load_d3_plan_binding(result.plan_id) if result else None
        facts=[c for c in changes if isinstance(c,UpsertFact)]
        plans=[c for c in changes if isinstance(c,UpsertPlan)]
        records=[c for c in changes if isinstance(c,D8Conclusion)]
        observed = bool(config.intent_execution_policy and result.outcome == "observed")
        no_new = config.contract_version == 22 and result.outcome == 'no_new_information'
        reused = (config.contract_version == 22 and not facts and result.supporting_fact_id is not None
                  and any(f.fact_id == result.supporting_fact_id for f in rows.facts))
        if (not isinstance(binding,HttpPlanBinding) or result.outcome not in ({'satisfied','no_new_information','observed'} if config.intent_execution_policy else {'satisfied','no_new_information'} if config.contract_version==22 else {'satisfied'})
                or metadata!=D3CommitMetadata(plan_result=result) or len(plans)!=1 or (len(records)>1 if observed else len(records)!=1)
                or len(changes)!=1+len(records)+len(facts) or len(facts)!=(0 if observed or no_new or reused else 1)
                or (any(r.outcome not in {'rejected','insufficient','reviewed_inference'} for r in records) if observed else records[0].outcome!=('no_new_information' if no_new else 'verified_fact'))
                or any(r.fact_id!=result.supporting_fact_id for r in records)
                or result.artifact_version_id!=binding.request_evidence_id
                or result.observation_id!=binding.execution_id or result.proof_id!=binding.execution_id
                or store.load_d3_plan_result(result.plan_id) is not None
                or facts and (facts[0].fact_id!=result.supporting_fact_id
                    or facts[0].source_ref!=binding.execution_id or facts[0].verification_ref!='d3:'+binding.execution_id
                    or facts[0].turn_id!=binding.turn_id or facts[0].status is not FactStatus.VERIFIED)
                or plans[0].plan_id!=binding.plan_id or plans[0].status is not PlanStatus.COMPLETED):
            raise StorageError('d20_http_completion_binding_changed')
    if operation not in {'register_d3_plan','replan_d3_plan'}:
        plan=next((p for p in rows.plans if p.plan_id==binding.plan_id),None)
        intent=next((i for i in rows.intents if i.intent_id==binding.intent_id),None)
        if (plan is None or plan.status is not PlanStatus.RUNNING or intent is None
                or intent.status is not IntentStatus.RUNNING or intent.turn_id!=binding.turn_id
                or binding.task_run_id!=mutation.task_run_id):
            raise StorageError('d20_http_plan_not_active')
        if operation=='complete_d3_plan' and (plans[0].intent_id!=plan.intent_id or plans[0].steps!=plan.steps):
            raise StorageError('d20_http_completed_plan_changed')
    return True
