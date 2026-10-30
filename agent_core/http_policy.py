"""Narrow HTTP schemas and model-selected tree goals for contract20."""
import json
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired




def policy(request):
    value=json.loads(request.prior_summary).get('d20_http_policy')
    if not isinstance(value,dict) or set(value) not in (
            {'endpoint','allowed_methods','authorization_sha256'},
            {'endpoint','allowed_methods','authorization_sha256','attachment_indices'}):
        raise ExecutionRecoveryRequired('d20_http_planning_policy_missing')
    return value


def schema(request):
    from agent_core.output_contracts import tagged,S
    if request.stage=='tree_planning':
        policy(request)
        from agent_core.intent_planning import direction_schema
        return direction_schema(request)
    if request.stage=='inner_plan':
        authorization=json.loads(request.history_context)['d20_http_policy']
        http=tagged('http_request',{'method':{'type':'string','enum':authorization['allowed_methods']},
            'path_query':S,'headers':{'type':'object','properties':{},'required':[],'additionalProperties':S},'body':S})
        if getattr(request.context.turn_binding,'kind',None)=='hybrid_origin':
            from agent_core.output_contracts import obj
            return {'anyOf':[http,*[obj({k:{'type':'null' if v is None else 'integer' if type(v) is int else 'string','enum':[v]}
                for k,v in c.items()}) for c in attachment_choices(request.context, rolling="intent_execution" in json.loads(request.history_context))]]}
        return http
    raise ValueError('d20_http_schema_stage_invalid')


def attachment_choices(context, *, rolling=False):
    from agent_core.hybrid_contracts import HybridTurnBinding
    if not isinstance(context.turn_binding,HybridTurnBinding):return []
    if len(context.prior_plan_ids)>=2 and not rolling:return []
    template='size_then_sha256' if not context.prior_plan_ids else 'sha256_only'
    choices=[dict(kind='attachment_observation',attachment_index=b.attachment_index,template=template,
        observation_mode='new_observation',source_proof_id=None) for b in context.turn_binding.attachments]
    if context.prior_plan_ids:
        choices += [dict(kind='attachment_observation',attachment_index=index,template=template,
            observation_mode='reuse_saved_proof',source_proof_id=proof)
            for index,proof in getattr(context,'file_proof_sources',())]
    return choices


from agent_core.intent_planning import parse_directions as planning


GOAL_INTERPRETATION = (
    "local_goal states what this intent must establish, not how to establish it. "
    "The executed request may use a different method or path than any wording in "
    "local_goal; judge whether the saved observation establishes or rules out the "
    "goal, not whether the request matches that wording. Use inconclusive only when "
    "the evidence can neither establish nor rule out the goal."
)


def evaluation(app,intent_id,fact_id,history):
    from dataclasses import asdict
    from agent_core.turn_modeling import digest
    intent=next(i for i in app.state.intents if i.intent_id==intent_id)
    plan=[p for p in app.state.plans if p.intent_id==intent_id][-1]
    result=app.store.load_d3_plan_result(plan.plan_id)
    source=app.web.observation(result.proof_id,historical=True)
    goal=next((i for i in history if i.record_id==intent_id),None)
    fact=next((i for i in history if i.record_id==fact_id),None)
    execution=next((i for i in history if i.record_id==source.execution_id),None)
    if goal is None or fact is None or execution is None:
        raise ExecutionRecoveryRequired('d20_http_evaluation_sources_missing')
    return dict(version=1,intent_id=intent_id,local_goal=intent.description,plan_id=plan.plan_id,
        template='http_request',observation_mode='new_observation',goal_ref=goal.ref,support_fact_ref=fact.ref,
        required_observation_refs=[execution.ref],
        capability_scope='saved_authorized_http_response_only_not_whole_task_verification',
        goal_interpretation=GOAL_INTERPRETATION,
        http_observation=asdict(source),history_sha256=digest([asdict(i) for i in history]))
