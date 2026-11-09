"""Bounded explanations of a frozen Stage rejection; never change admission."""

import json
import re
from dataclasses import asdict, replace

from agent_core.output_contracts import D8FormatIssue, diagnostic_branches

SOURCE_REASONS = {
    'source_type_not_allowed_for_this_action': 'This source record type is not accepted by this action.',
    'catalog_visible_but_not_admitted_by_this_field': 'The exact source is visible in the catalog, but is not admitted by this frozen field.',
    'source_belongs_to_another_task': 'This source belongs to another task.',
    'source_conflict': 'This source has an unresolved conflict.',
    'source_unavailable': 'The original source is not currently available in the frozen input.',
    'source_not_effective': 'This source version is no longer effective.',
    'source_identity_not_allowed_for_fact_observation': 'This progress record is not an eligible tool observation.',
    'observation_not_owned_by_this_road': 'This observation belongs to a different Turn or Road.',
    'version_not_allowed_by_frozen_field': 'The source record is known, but this version is not allowed by the frozen field.',
    'not_admitted_by_frozen_field': 'The source is known, but the frozen field does not admit it; no more specific cause is proven.',
    'not_in_frozen_sources': 'This exact reference is absent from the currently loaded sources and catalog; older messages do not grant eligibility.',
}


def _field(spec, value, parts):
    if 'anyOf' in spec:
        from agent_core.output_contracts import shape_issues
        choices = diagnostic_branches(spec['anyOf'], value)
        spec = min(choices, key=lambda branch: len(shape_issues(value, branch)))
    if not parts:
        return spec, value
    key, *rest = parts
    if isinstance(value, dict) and key in value:
        return _field(spec.get('properties', {}).get(key, {}), value[key], rest)
    if isinstance(value, list) and key.isdigit() and int(key) < len(value):
        return _field(spec.get('items', {}), value[int(key)], rest)
    return {}, None


def _source_reason(request, ref, allowed, kind):
    loaded = {item.ref: asdict(item) for item in request.context.history}
    catalog = {item['ref']: item for item in request.history_catalog}
    item = loaded.get(ref) or catalog.get(ref)
    if item:
        if item.get('task_run_id') != request.task_run_id:
            return 'source_belongs_to_another_task'
        if item.get('conflict'):
            return 'source_conflict'
        if item.get('source_status') != 'available':
            return 'source_unavailable'
        if item.get('current_status', 'effective') not in (None, 'effective'):
            return 'source_not_effective'
        if allowed and all(x.startswith('progress:') for x in allowed) and item['record_type'] != 'progress':
            return 'source_type_not_allowed_for_this_action'
        if ref not in loaded:
            return 'catalog_visible_but_not_admitted_by_this_field'
        if kind in {'fact_candidate', 'fact_candidates', 'fact_revision'} and item['record_type'] == 'progress':
            if item.get('identity') not in {'tool_observation_not_fact', 'public_reference_not_target_observation'}:
                return 'source_identity_not_allowed_for_fact_observation'
            if item.get('turn_id') != getattr(request.context.turn, 'turn_id', None) or item.get('intent_id') != request.context.session.owner_id:
                return 'observation_not_owned_by_this_road'
    if any(x.rsplit(':', 1)[0] == ref.rsplit(':', 1)[0] for x in allowed):
        return 'version_not_allowed_by_frozen_field'
    return 'not_admitted_by_frozen_field' if item else 'not_in_frozen_sources'


def output_diagnostic(request, raw, contract, *, parser_issues=()):
    from agent_core.stage_planning import (stage_output_issues, decode_stage_response,
        correction_hint, CORRECTION_HINT_LIMIT, BUSINESS_ISSUE_CODES)
    spec = contract['schema']
    issues = stage_output_issues(request, raw, result_schema=spec,
        normalization_policy=contract.get('output_normalization'))
    if not issues:
        issues = list(parser_issues) or [D8FormatIssue('stage_value_invalid', '$', 'valid_stage_value', 'invalid')]
    value, _, _, _ = decode_stage_response(request, raw, result_schema={
        'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': True},
        normalization_policy=contract.get('output_normalization'))
    wrapped = isinstance(value, dict) and set(value) == {'result'}
    if wrapped:
        value = value['result']
    native = bool(getattr(raw, 'tool_calls', None))
    prefix = '/result' if native or wrapped else ''
    hints, explained = [], []
    reference_failure = False
    kind = value.get('kind', request.stage) if isinstance(value, dict) else request.stage
    for issue in issues[:8]:
        path = issue.path
        if prefix and path.startswith('$/') and not path.startswith(('$/result', '$/tool_calls')):
            path = '$' + prefix + path[1:]
        parts = path[2:].split('/') if path.startswith('$/') else []
        if parts[:1] == ['result']:
            parts = parts[1:]
        field, actual = _field(spec, value, parts)
        expected, observed = issue.expected, issue.actual
        allowed = field.get('enum', [])
        if issue.code in {'enum_mismatch', 'stage.fact_source_ineligible'} and isinstance(actual, str) and any(
                key in parts for key in ('support_refs', 'source_ref', 'refs', 'evidence_refs', 'target_refs', 'replacement_ref')):
            reference_failure = True
            safe_ref = actual if re.fullmatch(r'(progress|verified_fact|reviewed_inference):[A-Za-z0-9/_-]{1,100}:[0-9a-f]{64}', actual) else '(unrecognized reference)'
            observed = safe_ref
            expected = _source_reason(request, actual, allowed, kind)
            examples = '; '.join(str(x) for x in allowed[:4])
            hints.append(f'{kind} {path}: rejected {safe_ref}; {expected}. {SOURCE_REASONS[expected]} '
                f'Allowed by this frozen field ({len(allowed)} total): {examples}'
                + (f'; {len(allowed)-4} more omitted.' if len(allowed) > 4 else '.')
                + ' Preserve valid supports; read original evidence for a new decision if needed. Reading alone does not grant eligibility; do not replay tools.')
        elif issue.code in {'enum_mismatch', 'constant_mismatch'}:
            if parts == ['kind']:
                allowed = list(dict.fromkeys(branch.get('properties', {}).get('kind', {}).get('const')
                    for branch in spec.get('anyOf', [spec])))
                allowed = [item for item in allowed if item is not None]
                expected = 'offered_kinds=' + ','.join(allowed)
                cause = ('This Turn already has a recorded achievement.' if actual == 'turn_completion'
                    and request.context.turn is not None and request.context.turn.achievement is not None
                    else 'This action is not offered by the frozen decision; do not infer missing evidence from its absence.')
                hints.append(f'{path}: {expected}. {cause}')
                explained.append(replace(issue, path=path, expected=expected))
                continue
            if 'enum' in field or 'const' in field:
                expected = 'allowed_values=' + json.dumps(allowed if 'enum' in field else [field['const']], ensure_ascii=True)[:400]
            hints.append(f'{path}: {expected}; keep the original decision meaning.')
        elif issue.code == 'string_too_long':
            hints.append(f'{path}: {expected}; observed {observed}. Shorten this field; do not move more content into it.')
        else:
            limits = {key: field[key] for key in ('minItems', 'maxItems', 'minimum', 'maximum', 'minLength', 'maxLength', 'pattern') if key in field}
            if limits:
                expected = json.dumps(limits, ensure_ascii=True)
            if isinstance(actual, list):
                observed = f'item_count={len(actual)}'
            elif type(actual) in (int, float):
                observed = str(actual)
            hints.append(f'{path}: {issue.code}; expected {expected}; observed {observed}.')
        explained.append(replace(issue, path=path, expected=expected, actual=observed))
    # Keep each error's explanation before spending space on optional guidance.
    per_issue = max(200, (CORRECTION_HINT_LIMIT - 100) // max(1, len(hints)))
    hint = ' '.join(x[:per_issue] for x in hints)
    extra = correction_hint(request, [i for i in issues if i.code not in {
        'enum_mismatch', 'constant_mismatch', 'string_too_long', 'stage.fact_source_ineligible'}
        and not i.path.endswith('/source_ref')])
    if len(hint) + len(extra) < CORRECTION_HINT_LIMIT:
        hint += extra
    return dict(issues=[asdict(i) for i in explained], hint=hint,
        reason='output_business_invalid' if reference_failure or any(i.code in BUSINESS_ISSUE_CODES for i in explained) else 'output_format_invalid')
