"""Narrow HTTP schemas and model-selected tree goals for contract20."""
from dataclasses import dataclass
import json
from agent_core.handoff_contracts import checked_refs
from agent_core.modeling import IntentCandidate
from agent_core.dual_llm_contracts import D3RecoveryRequired


@dataclass(frozen=True)
class HttpTurnProposal:
    turn_target: str
    intents: tuple[IntentCandidate, ...]


def policy(request):
    value=json.loads(request.prior_summary).get('d20_http_policy')
    if not isinstance(value,dict) or set(value) not in (
            {'endpoint','allowed_methods','authorization_sha256'},
            {'endpoint','allowed_methods','authorization_sha256','attachment_indices'}):
        raise D3RecoveryRequired('d20_http_planning_policy_missing')
    return value


def schema(request):
    from agent_core.output_contracts import tagged,array,INTENT,REFS,text,S
    from agent_core.intent_planning import candidate_limit, candidate_schema
    if request.stage=='tree_planning':
        policy(request)
        return {'anyOf':[candidate_schema(request,{'intents':array(INTENT,1,candidate_limit(request))}),
            tagged('exhausted',{'reason':text(512),'evidence_refs':REFS})]}
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


def planning(request,raw,revisions):
    from agent_core.handoff import D8PlanningResult,D8DependencyCandidatesRejected
    from agent_core.intent_planning import candidate_limit, without_order_reason
    raw = without_order_reason(request, raw)
    if not isinstance(raw,dict):raise ValueError('d20_http_planning_invalid')
    if raw.get('kind')=='exhausted':
        if set(raw)!={'kind','reason','evidence_refs'} or not isinstance(raw['reason'],str) or not raw['reason'].strip():
            raise ValueError('d20_http_exhaustion_invalid')
        checked_refs(raw['evidence_refs'],{i.ref for i in request.history})
        return D8PlanningResult(None,(),(),raw['reason'])
    maximum=candidate_limit(request)
    if (set(raw)!={'kind','intents'} or raw['kind']!='candidates' or not isinstance(raw['intents'],list)
            or not raw['intents'] or maximum is not None and len(raw['intents'])>maximum):
        raise ValueError('d20_http_planning_invalid')
    available={i.ref for i in request.history}
    kept,deps,rejected=[],[],[]
    for index,item in enumerate(raw['intents']):
        if not isinstance(item,dict) or set(item)!={'description','intent_type','dependency_refs'}:
            raise ValueError('d20_http_intent_fields_invalid')
        refs=checked_refs(item['dependency_refs'],available)
        if any(i.source_status!='available' or i.conflict or revisions and not revisions.reliable(i)
               for i in request.history if i.ref in refs):
            rejected.append(index)
            continue
        kept.append(IntentCandidate(item['description'],item['intent_type']))
        deps.append(refs)
    if not kept:raise D8DependencyCandidatesRejected(rejected)
    return D8PlanningResult(HttpTurnProposal(request.goal,tuple(kept)),tuple(deps),tuple(rejected))


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
        raise D3RecoveryRequired('d20_http_evaluation_sources_missing')
    return dict(version=1,intent_id=intent_id,local_goal=intent.description,plan_id=plan.plan_id,
        template='http_request',observation_mode='new_observation',goal_ref=goal.ref,support_fact_ref=fact.ref,
        required_observation_refs=[execution.ref],
        capability_scope='saved_authorized_http_response_only_not_whole_task_verification',
        goal_interpretation=GOAL_INTERPRETATION,
        http_observation=asdict(source),history_sha256=digest([asdict(i) for i in history]))
