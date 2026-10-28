"""Contract17 reachable fixed-observation paths; no new tool or proof authority."""

from dataclasses import asdict
from itertools import permutations
import json

from agent_core.dual_llm_contracts import ExecutionRecoveryRequired
from agent_core.turn_modeling import digest


KEY = 'execution_feasibility'
ACTIONS = ('probe', 'complete_intent')


def requirements(choice):
    return ['complete_intent'] if choice['observation_mode'] == 'reuse_saved_proof' else list(ACTIONS)


def choices(ordinal, proofs, *, rolling=False):
    if ordinal not in (1, 2) and not rolling:
        return []
    template = 'size_then_sha256' if ordinal == 1 else 'sha256_only'
    result = [{'template': template, 'observation_mode': 'new_observation', 'source_proof_id': None}]
    if ordinal == 2:
        result += [{'template': template, 'observation_mode': 'reuse_saved_proof', 'source_proof_id': p}
                   for p in proofs]
    return result


def freeze(app, turn_id, intent_id, *, state=None):
    if getattr(app, 'web', None) is not None:
        return app.web.feasibility(turn_id, intent_id, state=state)
    state = state or app.state
    task = state.task_run.task_run_id
    intent = next((i for i in state.intents if i.intent_id == intent_id), None)
    if intent is None or intent.turn_id != turn_id:
        raise ExecutionRecoveryRequired('d17_execution_scope_changed')
    allowed, _ = app.validator.action_limits(task, turn_id, intent_id)
    plans = [p for p in state.plans if p.intent_id == intent_id]
    proofs, versions = [], []
    latest_ready = False
    for plan in plans:
        binding = app.store.load_d3_plan_binding(plan.plan_id)
        result = app.store.load_d3_plan_result(plan.plan_id)
        if binding is None:
            raise ExecutionRecoveryRequired('d17_plan_binding_missing')
        if result is not None and result.outcome == 'satisfied':
            if app.config.intent_execution_policy:
                app.conclusions.check_support(state, binding, result.proof_id, result.supporting_fact_id)
            else:
                app.validator._support(state, binding, result.proof_id, result.supporting_fact_id)
            proofs.append(result.proof_id)
        ready = result is not None and result.outcome == 'satisfied'
        if not ready and result is not None and result.outcome == 'no_new_information':
            pass
        elif not ready and binding.observation_mode == 'new_observation':
            proof = app.proof_store.read(app._id(f'proof/{plan.plan_id}'))
            if proof is not None:
                # The saved first observation can proceed through Fact review without a new probe.
                source = app.conclusions.observation(proof.proof_id)
                if source.plan_id != plan.plan_id or source.intent_id != intent_id:
                    raise ExecutionRecoveryRequired('d17_observation_scope_changed')
                ready = True
        elif not ready and binding.observation_mode == 'reuse_saved_proof':
            app.validator._support(state, binding, binding.source_proof_id, binding.source_fact_id)
            ready = True
        versions.append({'plan': asdict(plan), 'binding': asdict(binding),
                         'result': asdict(result) if result is not None else None, 'observation_ready': ready})
        latest_ready = ready
    rolling = bool(app.config.intent_execution_policy) or (app.config.contract_version == 22 and plans and
               app.store.load_d3_plan_result(plans[-1].plan_id) is not None and
               app.store.load_d3_plan_result(plans[-1].plan_id).outcome == 'no_new_information')
    next_choices = choices(len(plans) + 1, proofs, rolling=bool(rolling))
    continuation = [['complete_intent']] if latest_ready else [list(ACTIONS)]
    return {'version': 1, 'task_run_id': task, 'turn_id': turn_id, 'intent_id': intent_id,
            'allowed_action_kinds': [a for a in ACTIONS if a in allowed],
            'plan_state_sha256': digest(versions), 'continue_paths': continuation,
            'replan_paths': [requirements(c) for c in next_choices] if plans else [],
            'next_plan_choices': next_choices}


def policy(request, *, required=False):
    context = json.loads(request.context_summary)
    p = context.get(KEY)
    if p is None and not required:
        return None
    try:
        if (set(p) != {'version', 'task_run_id', 'turn_id', 'intent_id', 'allowed_action_kinds',
                       'plan_state_sha256', 'continue_paths', 'replan_paths', 'next_plan_choices'}
                or type(p['version']) is not int or p['version'] != 1
                or (p['task_run_id'], p['turn_id'], p['intent_id']) != (request.task_run_id, request.turn_id, request.intent_id)
                or p['allowed_action_kinds'] != [a for a in ACTIONS if a in p['allowed_action_kinds']]
                or len(p['plan_state_sha256']) != 64
                or any(not isinstance(paths, list) or any(path not in [['complete_intent'], list(ACTIONS)] for path in paths)
                       for paths in (p['continue_paths'], p['replan_paths']))):
            raise ValueError('policy')
    except (TypeError, KeyError, ValueError) as exc:
        raise ExecutionRecoveryRequired('d17_execution_policy_invalid') from exc
    return p


def paths_for(request, decision):
    p = policy(request)
    if p is None or request.review_scope == 'turn_end':
        return None
    context = json.loads(request.context_summary)
    if context.get('d8_progress', {}).get('mode') == 'restricted_repair':
        return None
    key = {'continue_current_intent': 'continue_paths', 'replan_current_intent': 'replan_paths'}.get(decision)
    return p[key] if key else None


def validate(raw, request):
    paths = paths_for(request, raw['decision'])
    if paths is None:
        return
    allowed = set(policy(request)['allowed_action_kinds'])
    for item in raw['constraints']:
        if item['kind'] == 'allowed_action_kinds':
            allowed &= set(item['allowed_action_kinds'])
    if not any(set(path) <= allowed for path in paths):
        raise ValueError('execution_path_unavailable')


def constrain_schema(schema, request):
    """Use the same path alternatives in Schema and business validation."""
    if policy(request) is None:
        return schema
    branches = []
    for decision in schema['properties']['decision']['enum']:
        paths = paths_for(request, decision)
        for path in paths if paths is not None else [None]:
            branch = json.loads(json.dumps(schema))
            branch['properties']['decision']['enum'] = [decision]
            if path is not None:
                legal = []
                allowed = policy(request)['allowed_action_kinds']
                if not set(path) <= set(allowed):
                    continue
                for count in range(1, len(allowed) + 1):
                    legal += [list(v) for v in permutations(allowed, count) if set(path) <= set(v)]
                action = branch['properties']['constraints']['items']['anyOf'][0]['properties']['allowed_action_kinds']
                action['enum'] = legal
            branches.append(branch)
    if not branches:
        raise ExecutionRecoveryRequired('d17_no_executable_review_path')
    return {'anyOf': branches}


def plan_choices(request, original):
    p = json.loads(request.history_context).get(KEY)
    if not isinstance(p, dict):
        return original
    allowed = set(p['allowed_action_kinds'])
    selected = [c for c in original if c in p['next_plan_choices'] and set(requirements(c)) <= allowed]
    if not selected:
        raise ExecutionRecoveryRequired('d17_no_executable_plan')
    return selected


def verify_review(app, request, raw, state):
    if (request.review_scope == 'turn_end'
            or json.loads(request.context_summary).get('d8_progress', {}).get('mode') == 'restricted_repair'):
        return
    saved = policy(request, required=True)
    if saved != freeze(app, request.turn_id, request.intent_id, state=state):
        raise ExecutionRecoveryRequired('d17_review_execution_state_changed')
    validate(raw, request)


def verify_plan(app, request, proposal, state):
    from agent_core.dual_llm_modeling import D3PlanProposal
    if not isinstance(proposal, D3PlanProposal):
        raise ExecutionRecoveryRequired('d17_plan_proposal_type_changed')
    saved = json.loads(request.history_context).get(KEY)
    if not isinstance(saved, dict) or not all(k in saved for k in ('turn_id', 'intent_id')):
        raise ExecutionRecoveryRequired('d17_plan_execution_state_changed')
    current = freeze(app, saved['turn_id'], saved['intent_id'], state=state)
    if current != saved:
        raise ExecutionRecoveryRequired('d17_plan_execution_state_changed')
    choice = {k: getattr(proposal, k) for k in ('template', 'observation_mode', 'source_proof_id')}
    if choice not in plan_choices(request, current['next_plan_choices']):
        raise ExecutionRecoveryRequired('d17_plan_action_constraint_conflict')
