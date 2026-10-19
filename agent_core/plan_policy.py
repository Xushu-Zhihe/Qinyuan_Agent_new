"""Contract16 Plan choices derived from the frozen business request; no I/O."""

from agent_core.dual_llm_contracts import D3RecoveryRequired


def policy_for(request):
    from agent_core.dual_llm_modeling import D3PlanContext, D3SharedArtifactContext
    if isinstance(request, dict):
        from types import SimpleNamespace
        from agent_core.dual_llm_contracts import D3TurnBinding
        value = dict(request['context'])
        value['turn_binding'] = D3TurnBinding(**value['turn_binding'])
        value['prior_plan_ids'] = tuple(value['prior_plan_ids'])
        value['reusable_proof_ids'] = tuple(value['reusable_proof_ids'])
        value['shared_artifacts'] = tuple(D3SharedArtifactContext(**v) for v in value['shared_artifacts'])
        request = SimpleNamespace(stage=request['stage'], task_run_id=request['task_run_id'],
                                  context=D3PlanContext(**value), history_context=request.get('history_context', ''))
    context = request.context
    if (request.stage != 'inner_plan' or not isinstance(context, D3PlanContext)
            or context.turn_binding.task_run_id != request.task_run_id
            or len(set(context.prior_plan_ids)) != len(context.prior_plan_ids)
            or len(set(context.reusable_proof_ids)) != len(context.reusable_proof_ids)):
        raise D3RecoveryRequired('s6_plan_context_invalid')
    ordinal = len(context.prior_plan_ids) + 1
    import json
    context_packet = json.loads(getattr(request, "history_context", "") or "{}")
    rolling = "completion_facts" in context_packet or "intent_execution" in context_packet
    if ordinal not in {1, 2} and not rolling or ordinal == 1 and context.reusable_proof_ids:
        raise D3RecoveryRequired('s6_plan_choices_unavailable')
    template = 'size_then_sha256' if ordinal == 1 else 'sha256_only'
    choices = [{'template': template, 'observation_mode': 'new_observation', 'source_proof_id': None}]
    if ordinal == 2 or rolling:
        choices += [{'template': template, 'observation_mode': 'reuse_saved_proof', 'source_proof_id': ref}
                    for ref in context.reusable_proof_ids]
    if getattr(request, 'history_context', ''):
        from agent_core.execution_feasibility import plan_choices
        choices = plan_choices(request, choices)
    return {'version': 1, 'ordinal': ordinal, 'allowed_choices': choices}


def plan_schema(request):
    from agent_core.output_contracts import obj
    return {'anyOf': [obj({key: {'type': 'null' if value is None else 'string', 'enum': [value]}
                           for key, value in choice.items()})
                      for choice in policy_for(request)['allowed_choices']]}


def validate(request, proposal):
    from agent_core.dual_llm_modeling import D3ProposalRejected
    value = {key: getattr(proposal, key) for key in ('template', 'observation_mode', 'source_proof_id')}
    if value not in policy_for(request)['allowed_choices']:
        raise D3ProposalRejected('d3_plan_template_or_source_unavailable')
