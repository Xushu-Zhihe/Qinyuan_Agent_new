"""Evidence-bound explicit Fact requests; tool execution never creates a Fact."""
from dataclasses import asdict, dataclass
import json
from agent_core.conclusion_contracts import D8Conclusion, D8ReviewDecision, conclusion_digest
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.operation_accounting import _uuid, _sha
from agent_core.turn_modeling import stable_id

@dataclass(frozen=True)
class EvidenceCandidate:
    content: str
    reason: str
    evidence_refs: tuple[str, ...]
    inferred: bool

    def __post_init__(self):
        if (not isinstance(self.content, str)
                or not self.content.strip()
                or len(self.content) > 256
                or (not isinstance(self.reason, str))
                or (not self.reason.strip())
                or (len(self.reason) > 512)
                or ('\x00' in self.content + self.reason)
                or (type(self.inferred) is not bool)
                or (not 1 <= len(self.evidence_refs) <= 8)
                or (len(set(self.evidence_refs)) != len(self.evidence_refs))):
            raise ValueError('d22_evidence_candidate_invalid')
        for ref in self.evidence_refs:
            _uuid(ref, 'evidence_ref')

    @classmethod
    def parse(cls, raw):
        if (not isinstance(raw, dict)
                or set(raw) != {'kind', 'content', 'reason', 'evidence_refs', 'inferred'}
                or raw['kind'] != 'fact_candidate'
                or (not isinstance(raw['evidence_refs'], list))):
            raise ValueError('d22_evidence_candidate_fields_invalid')
        return cls(raw['content'], raw['reason'], tuple(raw['evidence_refs']), raw['inferred'])

@dataclass(frozen=True)
class EvidenceObservation:
    task_run_id: str
    turn_id: str
    intent_id: str
    sources: tuple[dict, ...]

    def __post_init__(self):
        for value in (self.task_run_id, self.turn_id, self.intent_id):
            _uuid(value, 'scope_id')
        if not 1 <= len(self.sources) <= 8 or len({s['ref'] for s in self.sources}) != len(self.sources):
            raise ValueError('d22_evidence_sources_invalid')
        for source in self.sources:
            _uuid(source['ref'], 'evidence_ref')
            _sha(source['version'], 'source_version')
            if source['kind'] not in {'mcp', 'http', 'attachment'}:
                raise ValueError('d22_evidence_kind_invalid')

    @property
    def source_version_sha256(self):
        return conclusion_digest(asdict(self))

    @property
    def plan_id(self):
        return None

@dataclass(frozen=True)
class EvidenceReviewRequest:
    model_request_id: str
    observation: EvidenceObservation
    stage: str
    role_config_ref: str
    base_checkpoint_sequence: int
    candidate: EvidenceCandidate
    goal: str
    intent_description: str
    existing_facts: tuple[dict, ...] = ()
    correction_of_model_request_id: str | None = None
    rejection_code: str | None = None

    @property
    def task_run_id(self):
        return self.observation.task_run_id

    @property
    def context_sha256(self):
        return conclusion_digest(asdict(self))

    @classmethod
    def from_dict(cls, raw):
        value = dict(raw)
        obs = dict(value['observation'])
        obs['sources'] = tuple(obs['sources'])
        value['observation'] = EvidenceObservation(**obs)
        candidate = dict(value['candidate'])
        candidate['evidence_refs'] = tuple(candidate['evidence_refs'])
        value['candidate'] = EvidenceCandidate(**candidate)
        value['existing_facts'] = tuple(value.get('existing_facts', ()))
        result = cls(**value)
        _uuid(result.model_request_id, 'model_request_id')
        if result.stage != 'fact_review':
            raise ValueError('d22_evidence_review_stage_invalid')
        return result

def resource(request):
    return request.observation.intent_id if isinstance(request, EvidenceReviewRequest) else request.observation.plan_id

def review_schema(request):
    from agent_core.output_contracts import obj, text, array
    decisions = ['accept', 'reject', 'insufficient', 'no_new_information']
    base = obj({'decision': {'type': 'string', 'enum': decisions}, 'reason': text(512), 'refined_content': {'type': ['string', 'null'], 'maxLength': 256}, 'merged_sources': {'type': ['array', 'null'], 'items': {'type': 'string', 'enum': list(request.candidate.evidence_refs)}, 'minItems': 1, 'uniqueItems': True}}, optional=('refined_content', 'merged_sources'))
    if request.existing_facts:
        return {'anyOf': [base, obj({'decision': {'type': 'string', 'const': 'reuse'}, 'reason': text(512), 'reuse_fact_id': {'type': 'string', 'enum': [f['fact_id'] for f in request.existing_facts]}})]}
    return base

def evidence_signatures(observation):
    """Content equality is a review hint; never an automatic merge decision."""
    values = []
    for source in observation.sources:
        data = source['data']
        if source['kind'] == 'mcp':
            result = data['result']
            content = {k: result.get(k) for k in ('status', 'exit_code', 'stdout', 'stderr', 'timed_out', 'code')}
        else:
            content = {k: data.get(k) for k in ('observed_sha256', 'byte_size', 'response_status')}
        values.append(conclusion_digest(dict(kind=source['kind'], content=content)))
    return sorted(set(values))

def parse_evidence_review(request, raw):
    decision = D8ReviewDecision.from_dict(raw)
    if decision.merged_sources and (not set(decision.merged_sources) <= set(request.candidate.evidence_refs)):
        raise ValueError('d22_review_source_not_proposed')
    if (decision.decision == 'reuse'
            and decision.reuse_fact_id not in {f['fact_id'] for f in request.existing_facts}):
        raise ValueError('d22_reuse_not_visible')
    content = decision.refined_content or request.candidate.content
    if (decision.decision == 'accept'
            and any((f['content'] == content
            and f.get('evidence_signatures') == evidence_signatures(request.observation) for f in request.existing_facts))):
        raise ValueError('d22_ambiguous_duplicate_conclusion')
    return decision

class EvidenceConclusions:

    def __init__(self, app):
        self.app = app
        self._validation_state = None

    @property
    def state(self):
        return self._validation_state if self._validation_state is not None else self.app.state

    def _id(self, name):
        return stable_id(self.state.task_run.task_run_id, 'd3/' + name)

    def source(self, ref, intent_id):
        app = self.app
        task = self.state.task_run.task_run_id
        op = app.store.load_d4_operation(ref)
        if (op is not None
                and op.start.category == 'tool_dispatch'
                and op.start.resource_ref.startswith('mcp/')):
            claim = app.output_boundary.evidence.read('mcp-call', ref)
            result = app.output_boundary.evidence.read('mcp-result', ref)
            if (claim is None
                    or result is None
                    or op.completion is None
                    or (claim['task_run_id'] != task)
                    or (claim['intent_id'] != intent_id)
                    or (op.start.task_run_id != task)
                    or (op.start.context_sha256 != conclusion_digest(claim))
                    or (result['request_sha256'] != conclusion_digest(claim))
                    or (op.completion.evidence_sha256 != conclusion_digest(result))
                    or (op.completion.evidence_ref != 'mcp-result/' + ref)
                    or (result['payload']['status'] not in {'completed', 'failed', 'rejected'})):
                raise D3RecoveryRequired('d22_evidence_source_unavailable')
            return dict(ref=ref, kind='mcp', version=conclusion_digest(dict(claim=claim, result=result)), source_ref=ref, data=app.redactor.redact_value(dict(call=claim['call'], result=result['payload'])))
        observed = app.conclusions.observation(ref, historical=True)
        if observed.task_run_id != task or observed.intent_id != intent_id:
            raise D3RecoveryRequired('d22_evidence_source_scope_changed')
        data = asdict(observed)
        if observed.rule_id == 'http_response_observed':
            from agent_core.conclusion_contracts import D8ModelRequest
            request = D8ModelRequest(self._id('evidence-projection/' + ref), observed, 'fact_proposal', app.config.inner_role.role_config_ref, self.state.latest_checkpoint.sequence, goal=self.state.task_run.request.goal, intent_description=next((i.description for i in self.state.intents if i.intent_id == intent_id)))
            data['protected_sources'] = app.web.model_context(request)
        return dict(ref=ref, kind='http' if observed.rule_id == 'http_response_observed' else 'attachment', version=observed.source_version_sha256, source_ref=observed.execution_id, data=app.redactor.redact_value(data))

    def catalog(self, intent_id):
        refs = []
        for op in self.app.store.list_d4_operations(self.app.state.task_run.task_run_id):
            if op.start.resource_ref.startswith('mcp/' + intent_id + '/') and op.completion is not None:
                refs.append(op.start.operation_id)
        for plan in self.app.state.plans:
            result = self.app.store.load_d3_plan_result(plan.plan_id)
            if plan.intent_id == intent_id and result is not None and (result.proof_id is not None):
                refs.append(result.proof_id)
        return [{k: s[k] for k in ('ref', 'kind', 'version')} for s in (self.source(r, intent_id) for r in dict.fromkeys(refs))]

    def validate_sources(self, observation):
        if any((self.source(s['ref'], observation.intent_id) != s for s in observation.sources)):
            raise D3RecoveryRequired('d22_evidence_source_version_changed')

    def validate_candidate(self, request, candidate):
        from agent_core.dual_llm_modeling import D3ProposalRejected
        packet=json.loads(request.history_context)
        if packet.get('intent_execution',{}).get('stop_requested'):
            raise D3ProposalRejected('d22_strategy_stop_summary_required')
        frozen = {s['ref']: s for s in packet.get('evidence_sources', [])}
        for ref in candidate.evidence_refs:
            if ref not in frozen:
                raise D3ProposalRejected('d22_evidence_not_visible')
            source = self.source(ref, request.context.intent_id)
            if source['version'] != frozen[ref]['version']:
                raise D3RecoveryRequired('d22_evidence_source_version_changed')
    parse_review = staticmethod(parse_evidence_review)

    def _request(self, proposal_id, intent_id, candidate):
        app = self.app
        intent = next((i for i in self.state.intents if i.intent_id == intent_id))
        identity = self._id('evidence-review/' + proposal_id)
        saved = app.conclusions.evidence.read_request(identity)
        if saved is not None:
            return saved
        from agent_core.intent_execution import facts
        existing = []
        for fact in facts(app, intent_id):
            stored = next((f for f in self.state.facts if f.fact_id == fact['fact_id']))
            if stored.verification_ref.startswith('d8e:'):
                prior = app.conclusions.evidence.read_request(stored.verification_ref[4:])
                observation = prior.observation
            else:
                observed = self.source(stored.verification_ref[3:], intent_id)
                observation = EvidenceObservation(self.state.task_run.task_run_id, intent.turn_id, intent_id, (observed,))
            existing.append(dict(fact_id=stored.fact_id, content=stored.content, ref=fact['ref'], evidence_signatures=evidence_signatures(observation)))
        observation = EvidenceObservation(self.state.task_run.task_run_id, intent.turn_id, intent_id, tuple((self.source(ref, intent_id) for ref in candidate.evidence_refs)))
        return EvidenceReviewRequest(identity, observation, 'fact_review', app.config.outer_role.role_config_ref, self.state.latest_checkpoint.sequence, candidate, self.state.task_run.request.goal, intent.description, tuple(existing))

    async def submit(self, proposal_id, intent_id, candidate, token):
        app = self.app
        old = next((r for r in app.store.list_d8_conclusions(self.state.task_run.task_run_id) if r.proposal_request_id == proposal_id), None)
        if old is not None:
            self.validate_record(old, self.state)
            app.store.mark_d3_operation_applied(proposal_id, old.record_id)
            return
        request = self._request(proposal_id, intent_id, candidate)
        final, decision = await app.conclusions._call(request, token)
        outcome = 'verified_fact' if decision.decision == 'reuse' or (decision.decision == 'accept' and (not candidate.inferred)) else 'reviewed_inference' if decision.decision == 'accept' else 'no_new_information' if decision.decision == 'no_new_information' else 'rejected' if decision.decision == 'reject' else 'insufficient'
        fact_id = decision.reuse_fact_id if decision.decision == 'reuse' else self._id('evidence-fact/' + proposal_id) if outcome == 'verified_fact' else None
        record = D8Conclusion(self._id('evidence-conclusion/' + proposal_id), request.task_run_id, request.observation.turn_id, intent_id, None, proposal_id, final, conclusion_digest(asdict(candidate)), request.observation.source_version_sha256, outcome, fact_id)
        changes = []
        if outcome == 'verified_fact' and decision.decision != 'reuse':
            from agent_core.control_store import UpsertFact
            from agent_core.domain import FactStatus
            changes.append(UpsertFact(fact_id, record.turn_id, decision.refined_content or candidate.content, request.observation.sources[0]['source_ref'], FactStatus.VERIFIED, 'd8e:' + final))
        changes.append(record)
        app.controller.commit_d3_transition('commit_evidence_conclusion', tuple(changes), (record.record_id,), outcome, record.record_id)
        app.store.mark_d3_operation_applied(proposal_id, record.record_id)
        from qinyuan_core import get_logger
        get_logger('ctrl').info('fact.review_committed',run=record.task_run_id,intent=intent_id,
            review=final,record=record.record_id,fact=fact_id,outcome=outcome,category='business')

    def validate_record(self, record, state):
        previous = self._validation_state
        self._validation_state = state
        try:
            return self._validate_record(record, state)
        finally:
            self._validation_state = previous

    def _validate_record(self, record, state):
        app = self.app
        request = app.conclusions.evidence.read_request(record.review_request_id)
        if not isinstance(request, EvidenceReviewRequest):
            raise D3RecoveryRequired('d22_evidence_review_missing')
        result = app.conclusions._known(request)
        if result is None or result['outcome'] != 'valid':
            raise D3RecoveryRequired('d22_evidence_review_unknown')
        decision = self.parse_review(request, result['raw'])
        candidate = request.candidate
        inner = app.model_evidence.read(record.proposal_request_id)
        operation = app.store.load_d3_operation(record.proposal_request_id)
        if (operation is None
                or operation.completion is None
                or inner is None
                or (operation.start.task_run_id != record.task_run_id)
                or (operation.start.context_sha256 != inner.context_sha256)
                or (operation.start.category != 'inner_model')
                or (operation.start.role != 'inner_action')
                or (not operation.start.resource_ref.startswith('plan:' + record.intent_id + ':'))
                or (operation.start.resource_ref != inner.resource_ref)
                or (operation.start.base_checkpoint_sequence != inner.base_checkpoint_sequence)
                or (operation.completion.evidence_ref != record.proposal_request_id)
                or (inner.role_config_ref != operation.start.role_config_ref)
                or (operation.start.role_config_ref != app.config.inner_role.role_config_ref)
                or (operation.completion.evidence_sha256 != conclusion_digest(asdict(inner)))
                or (operation.completion.result_kind != 'completed')
                or (app.model_evidence.read_rejection(record.proposal_request_id) is not None)
                or (inner.outcome != 'accepted')
                or (inner.task_run_id != record.task_run_id)
                or (inner.stage != 'inner_plan')
                or (EvidenceCandidate.parse(inner.raw) != candidate)):
            raise D3RecoveryRequired('d22_evidence_proposal_changed')
        saved = app.sessions.evidence.read('request', record.proposal_request_id)
        contract = app.output_boundary.evidence.read('contract', record.proposal_request_id)
        if (saved is None
                or contract is None
                or contract['source_request_sha256'] != conclusion_digest(saved['request'])
                or (contract['model_audit']['context_sha256'] != inner.context_sha256)):
            raise D3RecoveryRequired('d22_evidence_request_missing_or_changed')
        source = request.observation
        frozen = {s['ref']: s for s in json.loads(saved['request']['history_context']).get('evidence_sources', [])}
        if (record.task_run_id != source.task_run_id
                or record.turn_id != source.turn_id
                or record.intent_id != source.intent_id
                or (saved['intent_id'] != source.intent_id)
                or (saved['turn_id'] != source.turn_id)
                or (record.candidate_sha256 != conclusion_digest(asdict(candidate)))
                or (record.source_version_sha256 != source.source_version_sha256)
                or any((s['ref'] not in frozen
                or s['version'] != frozen[s['ref']]['version'] for s in source.sources))):
            raise D3RecoveryRequired('d22_evidence_conclusion_binding_changed')
        self.validate_sources(source)
        expected = 'verified_fact' if decision.decision == 'reuse' or (decision.decision == 'accept' and (not candidate.inferred)) else 'reviewed_inference' if decision.decision == 'accept' else 'no_new_information' if decision.decision == 'no_new_information' else 'rejected' if decision.decision == 'reject' else 'insufficient'
        expected_fact = decision.reuse_fact_id if decision.decision == 'reuse' else self._id('evidence-fact/' + record.proposal_request_id) if expected == 'verified_fact' else None
        if record.outcome != expected or record.fact_id != expected_fact:
            raise D3RecoveryRequired('d22_evidence_decision_changed')
        if decision.decision == 'reuse':
            fact = next((f for f in state.facts if f.fact_id == record.fact_id), None)
            if fact is None or fact.status.value != 'verified' or fact.turn_id != record.turn_id:
                raise D3RecoveryRequired('d22_reused_fact_not_effective')
        return source

    def validate_current_reuse(self, record):
        request=self.app.conclusions.evidence.read_request(record.review_request_id)
        result=self.app.conclusions.evidence.read_result(record.review_request_id)
        decision=self.parse_review(request,result['raw'])
        if decision.decision!='reuse':
            return
        from agent_core.intent_execution import facts
        frozen=next(f for f in request.existing_facts if f['fact_id']==decision.reuse_fact_id)
        current=next((f for f in facts(self.app,record.intent_id) if f['fact_id']==decision.reuse_fact_id),None)
        if current is None or current['ref']!=frozen['ref'] or current['content']!=frozen['content']:
            raise D3RecoveryRequired('d22_reused_fact_not_effective')

    def reconcile(self):
        for record in self.app.store.list_d8_conclusions(self.app.state.task_run.task_run_id):
            if record.plan_id is None:
                self.validate_record(record, self.app.state)
                self.app.store.mark_d3_operation_applied(record.proposal_request_id, record.record_id)

    def validate_fact(self, fact, state, pending=()):
        record = next((r for r in (*self.app.store.list_d8_conclusions(state.task_run.task_run_id), *pending) if r.plan_id is None and r.fact_id == fact.fact_id and (fact.verification_ref == 'd8e:' + r.review_request_id)), None)
        if record is None or record.outcome != 'verified_fact':
            raise D3RecoveryRequired('d22_fact_approval_missing')
        source = self.validate_record(record, state)
        request = self.app.conclusions.evidence.read_request(record.review_request_id)
        raw = self.app.conclusions.evidence.read_result(record.review_request_id)['raw']
        decision = self.parse_review(request, raw)
        if (fact.content != (decision.refined_content
                or request.candidate.content)
                or fact.source_ref != source.sources[0]['source_ref']
                or fact.turn_id != record.turn_id
                or (fact.status.value != 'verified')):
            raise D3RecoveryRequired('d22_fact_content_or_source_changed')
        return (record, source)
