"""Contract15 immutable wire/materialization evidence; queries never repair state."""
from dataclasses import asdict
import json
import math

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.governance_policy import context, materialize, requires_binding
from agent_core.turn_modeling import digest

STAGES = {'inner_plan', 'inner_size', 'inner_sha256', 'inner_complete',
          'conclusion_repair', 'change_review', 'handoff_summary', 'refinement'}


def validate_value(request, stage, value):
    """Validate original value rules against frozen data, without querying SQLite."""
    from agent_core.conclusion_revision_contracts import D8ConclusionChange, D8ChangeDecision
    from agent_core.process_facts import D5Candidate, D5Claim
    from agent_core.handoff_contracts import D8SummaryCandidate, D8Capacity
    from agent_core.operation_accounting import _uuid
    if stage == 'refinement':
        D5Candidate(value['summary_text'], tuple(D5Claim.from_dict(item) for item in value['claims']))
    elif stage == 'change_review':
        decision = D8ChangeDecision.from_dict(value)
        ctx = context(request, stage)
        if decision.decision == 'accept' and (ctx['goal_supported'] and decision.goal_supported is not True
                or ctx['scope_explanation'] and not (decision.scope_explanation or '').strip()):
            raise ValueError('invalid conditional decision')
    elif stage == 'handoff_summary':
        D8SummaryCandidate.parse(value, set(context(request, stage)['refs']), D8Capacity(**request['capacity']))
    elif value.get('kind') == 'conclusion_change':
        D8ConclusionChange.parse_action(value)
    elif value.get('kind') == 'history_query':
        for key, item in value['query'].items():
            if key != 'record_type':
                _uuid(item, key)


class SelectionBindings:
    def __init__(self, app, output):
        self.app, self.output, self.store = app, output, app.store

    def freeze(self, request, contract):
        if contract['stage'] not in STAGES:
            return
        raw = asdict(request)
        if digest(raw) != contract['safe_request_sha256']:
            raise D3RecoveryRequired('s5_safe_request_changed')
        self.output.save('selection-request', contract['model_request_id'], {
            'binding_version': 1, 'contract_version': self.app.config.contract_version, 'task_run_id': contract['task_run_id'],
            'model_request_id': contract['model_request_id'], 'contract_sha256': digest(contract),
            'request': raw, 'selection_context_sha256': digest(context(raw, contract['stage']))})

    def _request(self, identity):
        try:
            return self._checked_request(identity)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            raise D3RecoveryRequired('s5_selection_request_changed') from exc

    def _checked_request(self, identity):
        record = self.output.read('selection-request', identity)
        contract = self.output.read('contract', identity)
        if (not isinstance(record, dict) or not isinstance(contract, dict)
                or self.app.config.contract_version not in {15, 16, 17, 18, 19, 20, 21} or contract.get('contract_version') != self.app.config.contract_version
                or contract.get('model_request_id') != identity or contract.get('stage') not in STAGES
                or set(record) != {'binding_version', 'contract_version', 'task_run_id', 'model_request_id',
                    'contract_sha256', 'request', 'selection_context_sha256'}
                or record.get('binding_version') != 1 or record.get('contract_version') != self.app.config.contract_version
                or record.get('task_run_id') != contract['task_run_id'] or record.get('model_request_id') != identity
                or record.get('contract_sha256') != digest(contract)
                or not isinstance(record.get('request'), dict)
                or digest(record['request']) != contract['safe_request_sha256']
                or record.get('selection_context_sha256') != digest(context(record['request'], contract['stage']))):
            raise D3RecoveryRequired('s5_selection_request_changed')
        if contract['model_audit'] is not None:
            snapshot = self.app.sessions.evidence.read('request', identity)
            original = snapshot['request'] if isinstance(snapshot, dict) else None
        elif contract['stage'] == 'refinement':
            source = self.app.refinement.evidence.read_request(identity)
            original = asdict(source) if source is not None else None
        else:
            port = self.app.handoff.evidence if contract['stage'] == 'handoff_summary' else self.app.revisions.evidence
            original = port.read('request', identity)
        if original is None or digest(original) != contract['source_request_sha256']:
            raise D3RecoveryRequired('s5_original_request_missing_or_changed')
        return record, contract

    def normalize(self, request, wire, clock):
        identity = request.attempt.model_request_id if hasattr(request, 'attempt') else request.model_request_id
        saved, contract = self._request(identity)
        stage = contract['stage']
        if not requires_binding(stage, wire):
            return wire
        wire = json.loads(json.dumps(wire, ensure_ascii=False, allow_nan=False))
        canonical = materialize(context(saved['request'], stage), stage, wire)
        validate_value(saved['request'], stage, canonical)
        self.output.save('selection-binding', identity, {
            'binding_version': 1, 'contract_version': self.app.config.contract_version, 'task_run_id': contract['task_run_id'],
            'model_request_id': identity, 'root_request_id': contract['root_request_id'],
            'stage': stage, 'contract_sha256': digest(contract), 'request_sha256': digest(saved),
            'wire_response': wire, 'canonical_response': canonical, 'finished_at': clock()})
        return canonical

    def _operation(self, request, contract, binding):
        op = self.store.load_d4_operation(contract['model_request_id'])
        audit = contract['model_audit']
        checkpoint = request['attempt']['base_checkpoint_sequence'] if contract['stage'] == 'refinement' else request['base_checkpoint_sequence']
        source_sha = (request['attempt']['context_sha256'] if contract['stage'] == 'refinement'
                      else audit['context_sha256'] if audit else contract['source_request_sha256'])
        if audit:
            category, role, resource = 'action', 'inner_action', audit['resource_ref']
        elif contract['stage'] == 'refinement':
            category, role, resource = 'refinement', 'outer_planning', request['attempt']['review_id']
        elif contract['stage'] == 'handoff_summary':
            category, role, resource = 'handoff_summary', 'outer_planning', request['turn_id'] or contract['task_run_id']
        else:
            inner = contract['stage'] == 'conclusion_repair'
            category, role, resource = ('action' if inner else 'fact_review'), ('inner_action' if inner else 'outer_planning'), request['actor_intent_id']
        if (op is None or op.start.task_run_id != contract['task_run_id']
                or op.start.role_config_ref != contract['role_config_ref']
                or op.start.category != category or op.start.role != role or op.start.resource_ref != resource
                or op.start.base_checkpoint_sequence != checkpoint or op.start.context_sha256 != source_sha
                or op.start.budget_policy_ref != self.app.config.d4_budget.budget_policy_ref
                or binding['finished_at'] < op.start.started_at):
            raise D3RecoveryRequired('s5_selection_operation_changed')
        return op

    def read(self, identity):
        binding = self.output.read('selection-binding', identity)
        if binding is None:
            return None
        saved, contract = self._request(identity)
        keys = {'binding_version', 'contract_version', 'task_run_id', 'model_request_id', 'root_request_id',
                'stage', 'contract_sha256', 'request_sha256', 'wire_response', 'canonical_response', 'finished_at'}
        if (not isinstance(binding, dict) or set(binding) != keys
                or binding.get('binding_version') != 1 or binding.get('contract_version') != self.app.config.contract_version
                or binding.get('task_run_id') != contract['task_run_id'] or binding.get('model_request_id') != identity
                or binding.get('root_request_id') != contract['root_request_id'] or binding.get('stage') != contract['stage']
                or binding.get('contract_sha256') != digest(contract) or binding.get('request_sha256') != digest(saved)
                or type(binding.get('finished_at')) not in {float, int} or not math.isfinite(binding['finished_at'])):
            raise D3RecoveryRequired('s5_selection_binding_changed')
        from agent_core.output_contracts import decode_response
        wire, issues, terminal, _ = decode_response(binding['wire_response'], contract['output_contract']['schema'])
        if issues or terminal or not requires_binding(contract['stage'], wire):
            raise D3RecoveryRequired('s5_selection_wire_changed')
        try:
            canonical = materialize(context(saved['request'], contract['stage']), contract['stage'], wire)
            validate_value(saved['request'], contract['stage'], canonical)
        except (ValueError, TypeError, KeyError) as exc:
            raise D3RecoveryRequired('s5_selection_canonical_changed') from exc
        if digest(canonical) != digest(binding['canonical_response']):
            raise D3RecoveryRequired('s5_selection_canonical_changed')
        self._operation(saved['request'], contract, binding)
        return binding

    def _native(self, identity, contract):
        stage = contract['stage']
        if contract['model_audit'] is not None:
            value = self.app.model_evidence.read(identity)
            return (None, None, False) if value is None else (value.raw, asdict(value), value.outcome == 'accepted')
        if stage == 'refinement':
            value = self.app.refinement.evidence.read_result(identity)
            candidate = self.app.refinement.evidence.read_candidate(identity)
            return (None, value, False) if candidate is None else (asdict(candidate), value, value is not None and value['outcome'] == 'accepted')
        port = self.app.handoff.evidence if stage == 'handoff_summary' else self.app.revisions.evidence
        value = port.read('result', identity)
        return (None, None, False) if value is None else (value['raw'], value, value['outcome'] == 'valid')

    def verify(self, request, canonical):
        identity = request.attempt.model_request_id if hasattr(request, 'attempt') else request.model_request_id
        _, contract = self._request(identity)
        if not requires_binding(contract['stage'], canonical):
            return
        record = self.read(identity)
        if record is None or digest(record['canonical_response']) != digest(canonical):
            raise D3RecoveryRequired('s5_selection_binding_missing_or_changed')
        if digest(asdict(request)) not in {contract['source_request_sha256'], contract['safe_request_sha256']}:
            raise D3RecoveryRequired('s5_selection_source_request_changed')

    def verify_identity(self, identity, canonical):
        contract = self.output.read('contract', identity)
        if isinstance(contract, dict) and contract.get('stage') in STAGES and requires_binding(contract['stage'], canonical):
            binding = self.read(identity)
            if binding is None or digest(binding['canonical_response']) != digest(canonical):
                raise D3RecoveryRequired('s5_selection_binding_missing_or_changed')

    def check_relation(self, identity, action):
        if action.operation != 'replace_support':
            return
        binding = self.read(identity)
        if binding is None:
            raise D3RecoveryRequired('s5_selection_binding_missing_or_changed')
        request, contract = self._request(identity)
        ctx = context(request['request'], contract['stage'])
        relation = ctx['relations'][binding['wire_response']['relation_ref']]
        goal, old_ref = self.app.revisions._relation_details(action, self.app.revisions._base())
        if old_ref != relation['old_ref'] or goal != relation['original_goal']:
            raise D3RecoveryRequired('s5_frozen_relation_changed')

    def repair_request(self, request):
        identity = request.attempt.model_request_id if hasattr(request, 'attempt') else request.model_request_id
        binding = self.read(identity)
        if binding is None:
            return
        self.verify(request, binding['canonical_response'])
        _, contract = self._request(identity)
        body, native, accepted = self._native(identity, contract)
        if native is not None:
            if accepted and digest(body) != digest(binding['canonical_response']):
                raise D3RecoveryRequired('s5_selection_native_changed')
            return
        op = self.store.load_d4_operation(identity)
        if op.completion is not None:
            raise D3RecoveryRequired('s5_native_result_missing_after_completion')
        raw, finished = binding['canonical_response'], binding['finished_at']
        if contract['model_audit'] is not None:
            from agent_core.dual_llm_modeling import D3ModelEvidence
            audit = contract['model_audit']
            self.app.model_evidence.save(D3ModelEvidence(identity, contract['task_run_id'], audit['stage'],
                audit['role_config_ref'], audit['resource_ref'], audit['base_checkpoint_sequence'], audit['context_sha256'],
                audit['correction_of_model_request_id'], 'accepted', raw, None, finished))
        elif contract['stage'] == 'refinement':
            from agent_core.process_facts import D5Candidate, D5Claim
            candidate = D5Candidate(raw['summary_text'], tuple(D5Claim.from_dict(item) for item in raw['claims']))
            self.app.refinement._validate_candidate(candidate, request.attempt)
            self.app.refinement.evidence.save_candidate(identity, candidate)
            self.app.refinement.evidence.save_result(identity, 'accepted', finished, 'candidate_accepted')
        else:
            value = {'model_request_id': identity, 'request_sha256': request.context_sha256,
                     'outcome': 'valid', 'raw': raw, 'finished_at': finished}
            if contract['stage'] == 'handoff_summary':
                value.update(reason=None, rejected_candidate_indices=[])
                self.app.handoff.evidence.save('result', identity, value)
            else:
                self.app.revisions.evidence.save('result', identity, value)

    def check_task(self, task):
        needs_repair = False
        for op in self.store.list_d4_operations(task):
            identity = op.start.operation_id
            contract = self.output.read('contract', identity)
            if not isinstance(contract, dict) or contract.get('stage') not in STAGES:
                continue
            self._request(identity)
            binding = self.read(identity)
            body, native, accepted = self._native(identity, contract)
            if accepted and requires_binding(contract['stage'], body):
                if binding is None or digest(body) != digest(binding['canonical_response']):
                    raise D3RecoveryRequired('s5_selection_binding_missing_or_changed')
                if op.completion is not None and (op.completion.result_kind != 'completed'
                        or op.completion.evidence_sha256 != digest(body if contract['stage'] == 'refinement' else native)
                        or op.completion.finished_at != native['finished_at']):
                    raise D3RecoveryRequired('s5_selection_completion_changed')
            if contract['stage'] == 'refinement' and native is not None and native['outcome'] == 'response_unknown':
                if self.store.is_unknown_refinement_sealed(identity):
                    continue
            needs_repair |= binding is not None and native is None or op.completion is None
        return needs_repair
