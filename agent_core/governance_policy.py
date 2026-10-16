"""Contract15 frozen choices; preserve explicit evidence and native business gates."""
from dataclasses import asdict
import json

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.turn_modeling import digest


class MixedClaimSources(ValueError):
    """Known D5 business rejection, never a new format-correction opportunity."""


def freeze_policy(app, packet):
    from agent_core.conclusion_revisions import ConclusionRevisions
    from agent_core.conclusion_revision_contracts import D8ConclusionChange, D8RevisionTarget
    visible = ConclusionRevisions.visible_from_context(json.dumps(packet))
    refs = {item.ref for item in visible}
    base, heads, conflicts, _ = app.revisions._projection(app.revisions._base())
    targets, relations = {}, {}
    for root, head in sorted(heads.items()):
        if head['last_ref'] not in refs or not any(
                getattr(item, 'logical_root_id', None) == root
                and item.revision == head['version'] and item.last_version_ref == head['last_ref']
                for item in visible):
            continue
        target = {'root_id': root, 'version': head['version'], 'ref': head['last_ref'],
                  'record_type': head['kind'], 'adopted': head['ref'] is not None}
        token = 'governance/' + digest(target)
        targets[token] = target
        for owner in app.state.intents:
            for kind in ('parent_support', 'intent_dependency'):
                action = D8ConclusionChange('replace_support', (D8RevisionTarget(root, head['version'], head['last_ref']),),
                    head['last_ref'], None, (), 'Frozen relation selection', None, kind, owner.intent_id)
                try:
                    goal, old_ref = app.revisions._relation_details(action, base)
                except D3RecoveryRequired as exc:
                    if str(exc) not in {'d8_parent_success_relation_missing',
                                        'd8_support_relation_version_changed',
                                        'd8_dependency_relation_version_changed'}:
                        raise
                    continue
                relation = {'target': token, 'relation_kind': kind, 'relation_owner_id': owner.intent_id,
                            'old_ref': old_ref, 'original_goal': goal}
                relations['relation/' + digest(relation)] = relation
    return {'version': 1, 'targets': targets, 'relations': relations,
            'open_pairs': [list(pair) for pair in sorted(conflicts)]}


def context(raw_request, stage):
    """Pure JSON context, derived only from the already frozen request."""
    from agent_core.handoff_contracts import D8HistoryItem
    if stage == 'refinement':
        sources = raw_request['attempt']['sources']
        return {'sources': sources, 'process_facts': raw_request['process_facts']}
    if stage == 'change_review':
        action = raw_request['candidate']
        return {'goal_supported': action['operation'] == 'replace_support' and action['relation_kind'] == 'parent_support',
                'scope_explanation': action['operation'] == 'resolve_conflict' and action['resolution'] == 'different_scope'}
    if stage == 'handoff_summary':
        return {'refs': [D8HistoryItem.from_dict(item).ref for item in raw_request['history']]}
    packet = json.loads(raw_request['history_context'])
    policy = packet.get('s5_policy')
    if not isinstance(policy, dict) or type(policy.get('version')) is not int or policy.get('version') != 1:
        raise D3RecoveryRequired('s5_frozen_policy_unavailable')
    visible = list(packet['items'])
    cursors = []
    for retrieval in packet.get('retrieved', []):
        visible.extend(retrieval['read']['items'])
        if retrieval['read'].get('next_cursor') is not None:
            cursors.append(retrieval['read']['next_cursor'])
    return {**policy, 'refs': list(dict.fromkeys(item.get('ref') or D8HistoryItem.from_dict(item).ref for item in visible)),
            'replacement_refs': [item['ref'] for item in visible if item.get('ref')
                and item.get('record_type') in {'verified_fact', 'reviewed_inference'}],
            'cursors': list(dict.fromkeys(cursors))}


def choice(values):
    return {'type': 'string', 'enum': list(dict.fromkeys(values))}


def reads_schema(ctx):
    from agent_core.output_contracts import obj, tagged, array, NS
    fields = {key: NS for key in ('turn_id', 'intent_id', 'artifact_version_id')}
    fields['record_type'] = {'anyOf': [choice(('verified_fact', 'reviewed_inference', 'progress', 'intent', 'turn')), {'type': 'null'}]}
    return [tagged('history_query', {'query': obj(fields, optional=tuple(fields))}),
            tagged('history_details', {'refs': {**array(choice(ctx['refs']), 1, 2), 'uniqueItems': True}}),
            tagged('history_next_page', {'cursor': choice(ctx['cursors'])})]


def change_schema(ctx):
    from agent_core.output_contracts import tagged, array, text, NS
    branches = []
    for operation in ('correct', 'withdraw', 'restore', 'report_conflict', 'resolve_conflict', 'replace_support', 'link_verified'):
        allowed = [token for token, target in ctx['targets'].items()
                   if (target['adopted'] if operation in {'correct', 'withdraw', 'report_conflict'} else
                       not target['adopted'] if operation == 'restore' else True)]
        if not allowed:
            continue
        count = 2 if operation in {'report_conflict', 'resolve_conflict'} else 1
        fields = {'operation': {'type': 'string', 'const': operation},
                  'targets': {**array(choice(allowed), count, count), 'uniqueItems': True},
                  'replacement_ref': NS, 'content': {'type': ['string', 'null'], 'maxLength': 256},
                  'evidence_refs': {**array(choice(ctx['refs']), 0, 8), 'uniqueItems': True},
                  'reason': text(512), 'resolution': {'type': 'null'}, 'relation_ref': {'type': 'null'}}
        if operation == 'resolve_conflict':
            fields['resolution'] = choice(('keep_first', 'keep_second', 'withdraw_both', 'unresolved', 'different_scope'))
        if operation == 'replace_support':
            for token, relation in ctx['relations'].items():
                branches.append(tagged('conclusion_change', {**fields,
                    'targets': {**array(choice([relation['target']]), 1, 1), 'uniqueItems': True},
                    'relation_ref': {'type': 'string', 'const': token}, 'replacement_ref': choice(ctx['replacement_refs']),
                    'content': {'type': 'null'}}))
            continue
        if operation in {'withdraw', 'report_conflict', 'resolve_conflict'}:
            fields.update(replacement_ref={'type': 'null'}, content={'type': 'null'})
        elif operation == 'link_verified':
            fields.update(replacement_ref=choice(ctx['replacement_refs']), content={'type': 'null'})
        branches.append(tagged('conclusion_change', fields))
    return {'anyOf': branches} if branches else {'type': 'object', 'properties': {}, 'required': ['unavailable'], 'additionalProperties': False}


def review_schema(ctx):
    from agent_core.output_contracts import obj, text, NB, NS
    branches = []
    for decision in ('accept', 'reject', 'insufficient'):
        fields = {'decision': {'type': 'string', 'const': decision}, 'reason': text(512)}
        if ctx['goal_supported']:
            fields['goal_supported'] = {'type': 'boolean', 'const': True} if decision == 'accept' else NB
        if ctx['scope_explanation']:
            fields['scope_explanation'] = text(512) if decision == 'accept' else NS
        branches.append(obj(fields))
    return {'anyOf': branches}


def refinement_schema(ctx):
    from agent_core.output_contracts import obj, array, text
    refs = [source['object_id'] for source in ctx['sources']]
    conflicts = [obj({'process_fact_id': {'type': 'string', 'const': fact['process_fact_id']},
                      'claim_index': {'type': 'integer', 'enum': list(range(len(fact['claims'])))}})
                 for fact in ctx['process_facts'] if fact.get('claims')]
    conflict_field = array({'anyOf': conflicts}, 0) if conflicts else array({'type': 'null'}, 0, 0)
    claim = obj({'claim_key': text(128), 'claim_value': text(512),
        'source_refs': {**array(choice(refs), 1, 32), 'uniqueItems': True},
        'conflicts_with': conflict_field}, optional=('conflicts_with',))
    return obj({'summary_text': text(2048), 'claims': array(claim, 0, 8)})


def patch_schema(request, stage, schema):
    if stage not in {'inner_plan', 'inner_size', 'inner_sha256', 'inner_complete',
                     'conclusion_repair', 'change_review', 'handoff_summary', 'refinement'}:
        return schema
    ctx = context(asdict(request), stage)
    if stage == 'change_review':
        return review_schema(ctx)
    if stage == 'refinement':
        return refinement_schema(ctx)
    if stage == 'handoff_summary':
        from agent_core.output_contracts import obj, array, text
        fields = dict(schema['properties'])
        fields['ordered_refs'] = {**array(choice(ctx['refs'])), 'uniqueItems': True}
        fields['open_questions'] = array(obj({'text': text(256), 'refs': {**array(choice(ctx['refs'])), 'uniqueItems': True}}), 0, 8)
        return obj(fields)
    branches = []
    for branch in schema['anyOf']:
        kind = branch.get('properties', {}).get('kind', {}).get('const')
        if kind == 'conclusion_change':
            changes = change_schema(ctx)
            branches.extend(changes.get('anyOf', []))
        elif kind in {'history_query', 'history_details', 'history_next_page'}:
            continue
        else:
            branches.append(branch)
    if stage in {'inner_plan', 'conclusion_repair'}:
        branches.extend(reads_schema(ctx))
    return {'anyOf': branches}


def requires_binding(stage, raw):
    return stage in {'change_review', 'handoff_summary', 'refinement'} or isinstance(raw, dict) and raw.get('kind') in {
        'conclusion_change', 'history_query', 'history_details', 'history_next_page'}


def materialize(ctx, stage, wire):
    import copy
    value = copy.deepcopy(wire)
    if stage == 'change_review':
        value.setdefault('goal_supported', None)
        value.setdefault('scope_explanation', None)
    elif stage == 'refinement':
        sources = {source['object_id']: source for source in ctx['sources']}
        for claim in value['claims']:
            claim.setdefault('conflicts_with', [])
            selected = [sources[ref] for ref in claim['source_refs']]
            identities = {(s['subject_ref'], s['observation_scope']) for s in selected}
            if len(identities) != 1:
                raise MixedClaimSources('s5_claim_sources_inconsistent')
            claim['subject_ref'], claim['observation_scope'] = next(iter(identities))
    elif value.get('kind') == 'conclusion_change':
        value['targets'] = [{key: ctx['targets'][ref][key] for key in ('root_id', 'version', 'ref')} for ref in value['targets']]
        token = value.pop('relation_ref')
        relation = ctx['relations'][token] if token is not None else None
        value.update(relation_kind=relation['relation_kind'] if relation else None,
                     relation_owner_id=relation['relation_owner_id'] if relation else None)
    elif value.get('kind') == 'history_query':
        value['query'] = {key: item for key, item in value['query'].items() if item is not None}
    return value
