"""Contract20 HTTP bindings. A response is an observation, never a goal verdict."""

from dataclasses import dataclass

from agent_core.dual_llm_contracts import _uuid, _sha, _time, D3PlanBinding, D3TurnBinding
from agent_core.conclusion_contracts import D8Observation
from agent_core.v7.web_contracts import D7WebRunConfig, canonical_web_origin
from agent_core.dual_llm_modeling import D3PlanContext


@dataclass(frozen=True, slots=True)
class HttpTurnBinding:
    binding_id: str
    task_run_id: str
    turn_id: str
    endpoint: str
    authorization_id: str
    proposal_request_id: str
    kind: str = 'http_origin'

    def __post_init__(self):
        for field in ('binding_id','task_run_id','turn_id','authorization_id','proposal_request_id'):
            _uuid(getattr(self,field),field)
        if self.endpoint != canonical_web_origin(self.endpoint) or self.kind != 'http_origin':
            raise ValueError('d20_http_turn_binding_invalid')


@dataclass(frozen=True, slots=True)
class HttpPlanBinding:
    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    proposal_request_id: str
    execution_id: str
    request_evidence_id: str
    request_sha256: str
    rule_binding_sha256: str
    replan_review_id: str | None = None
    template: str = 'http_request'
    observation_mode: str = 'new_observation'
    kind: str = 'http_request'

    def __post_init__(self):
        for field in ('task_run_id','turn_id','intent_id','plan_id','proposal_request_id',
                      'execution_id','request_evidence_id'):
            _uuid(getattr(self,field),field)
        _sha(self.request_sha256,'request_sha256')
        _sha(self.rule_binding_sha256,'rule_binding_sha256')
        if self.replan_review_id is not None:_uuid(self.replan_review_id,'replan_review_id')
        if (self.kind,self.template,self.observation_mode) != ('http_request','http_request','new_observation'):
            raise ValueError('d20_http_plan_binding_invalid')

    @property
    def artifact_version_id(self):
        # The immutable protected request is a versioned evidence object, not a file copy.
        return self.request_evidence_id

    @property
    def source_proof_id(self):return None

    @property
    def source_fact_id(self):return None


def turn_binding_from_dict(value):
    if value.get('kind')=='hybrid_origin':
        from agent_core.hybrid_contracts import HybridTurnBinding
        return HybridTurnBinding(**(value|{'attachments':tuple(D3TurnBinding(**b) for b in value['attachments'])}))
    return HttpTurnBinding(**value) if value.get('kind') == 'http_origin' else D3TurnBinding(**value)


def plan_binding_from_dict(value):
    if value.get('kind')=='attachment_observation':
        from agent_core.hybrid_contracts import AttachmentPlanBinding
        return AttachmentPlanBinding(**value)
    return HttpPlanBinding(**value) if value.get('kind') == 'http_request' else D3PlanBinding(**value)


def plan_turn_binding(store,binding):
    if binding is None:return None
    if getattr(binding,'kind',None)=='attachment_observation':
        return store.load_d3_plan_turn_binding(binding.plan_id)
    return store.load_d3_turn_binding(binding.turn_id)


@dataclass(frozen=True, slots=True)
class HttpPlanProposal:
    request_evidence_id: str
    request_sha256: str
    method: str
    kind: str = 'http_request_saved'

    def __post_init__(self):
        from agent_core.v7.web_contracts import D7_WEB_ALLOWED_METHODS
        _uuid(self.request_evidence_id,'request_evidence_id')
        _sha(self.request_sha256,'request_sha256')
        if self.kind != 'http_request_saved' or self.method not in D7_WEB_ALLOWED_METHODS:
            raise ValueError('d20_http_plan_proposal_invalid')


@dataclass(frozen=True, slots=True)
class HttpPlanContext(D3PlanContext):
    http_sources: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class HttpObservation(D8Observation):
    rule_id: str = 'http_response_observed'
    observation_scope: str = 'saved_http_response'
    method: str = ''
    endpoint: str = ''
    response_status: int = 0
    request_sha256: str = ''
    response_evidence_id: str = ''
    rule_binding_sha256: str = ''

    def __post_init__(self):
        from agent_core.v7.web_contracts import D7_WEB_ALLOWED_METHODS
        for field in ('task_run_id','turn_id','intent_id','plan_id','artifact_version_id',
                      'observation_id','proof_id','execution_id','response_evidence_id'):
            _uuid(getattr(self,field),field)
        for field in ('observed_sha256','source_version_sha256','request_sha256','rule_binding_sha256'):
            _sha(getattr(self,field),field)
        _time(self.observed_at,'observed_at')
        if (self.rule_id != 'http_response_observed' or self.rule_version != 1
                or self.observation_scope != 'saved_http_response'
                or self.verification_status != 'verified' or self.execution_status != 'succeeded'
                or self.method not in D7_WEB_ALLOWED_METHODS
                or self.endpoint != canonical_web_origin(self.endpoint)
                or type(self.response_status) is not int or not 100 <= self.response_status <= 599
                or type(self.byte_size) is not int or not 0 <= self.byte_size <= 2*1024*1024):
            raise ValueError('d20_http_observation_invalid')

    def fact_content(self):
        return (f'Saved HTTP {self.method} response {self.response_status}; body SHA-256 '
                f'{self.observed_sha256}, {self.byte_size} bytes; proof {self.proof_id}.')

    @property
    def source_execution_id(self):return self.execution_id


@dataclass(frozen=True, slots=True)
class HttpRunConfig(D7WebRunConfig):
    """Reuse the transport port without adopting D7 role/session orchestration."""

    def __post_init__(self):
        from agent_core.operation_accounting import D8HandoffBudgetPolicy
        _uuid(self.task_run_id,'task_run_id')
        _uuid(self.request_id,'request_id')
        if (self.endpoint != canonical_web_origin(self.endpoint)
                or self.endpoint != self.authorization.allowed_origin
                or self.outer_role.prompt_template not in {'qinyuan.d8.outer.v15','qinyuan.d8.outer.v16','qinyuan.d8.outer.v17'}
                or self.inner_role.prompt_template != self.outer_role.prompt_template.replace('outer','inner')
                or not isinstance(self.d4_budget,D8HandoffBudgetPolicy)
                or self.schema_version != 1):
            raise ValueError('d20_http_runtime_binding_invalid')
        if not self.model_transport_config_ref.startswith('sha256:'):
            raise ValueError('d20_http_transport_reference_invalid')
        _sha(self.model_transport_config_ref[7:],'model_transport_config_ref')


def is_http(config):
    return config is not None and config.contract_version in {20,21,22} and config.web_authorization is not None
