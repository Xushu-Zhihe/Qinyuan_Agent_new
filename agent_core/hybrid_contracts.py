"""Contract21: a Turn exposes authorized sources; each Plan selects its capability."""
from dataclasses import dataclass,asdict
from agent_core.dual_llm_contracts import D3TurnBinding,D3PlanBinding,ExecutionRecoveryRequired
from agent_core.dual_llm_modeling import D3PlanProposal,parse_d3_plan_proposal
from agent_core.http_contracts import HttpTurnBinding,HttpPlanContext


@dataclass(frozen=True,slots=True)
class HybridTurnBinding(HttpTurnBinding):
    attachments:tuple[D3TurnBinding,...]=()
    kind:str='hybrid_origin'

    def __post_init__(self):
        HttpTurnBinding(self.binding_id,self.task_run_id,self.turn_id,self.endpoint,
                        self.authorization_id,self.proposal_request_id)
        if (self.kind!='hybrid_origin' or not isinstance(self.attachments,tuple)
                or not 1<=len(self.attachments)<=2
                or tuple(b.attachment_index for b in self.attachments)!=tuple(range(len(self.attachments)))
                or any(not isinstance(b,D3TurnBinding) or (b.task_run_id,b.turn_id,b.proposal_request_id)
                    !=(self.task_run_id,self.turn_id,self.proposal_request_id) for b in self.attachments)):
            raise ValueError('d21_hybrid_turn_sources_invalid')


@dataclass(frozen=True,slots=True)
class AttachmentPlanProposal(D3PlanProposal):
    attachment_index:int
    kind:str='attachment_observation'

    def __post_init__(self):
        parse_d3_plan_proposal({k:getattr(self,k) for k in ('template','observation_mode','source_proof_id')})
        if self.kind!='attachment_observation' or type(self.attachment_index) is not int or self.attachment_index not in {0,1}:
            raise ValueError('d21_attachment_plan_choice_invalid')


@dataclass(frozen=True,slots=True)
class AttachmentPlanBinding(D3PlanBinding):
    attachment_index:int=0
    kind:str='attachment_observation'

    def __post_init__(self):
        D3PlanBinding.__post_init__(self)
        if self.kind!='attachment_observation' or type(self.attachment_index) is not int or self.attachment_index not in {0,1}:
            raise ValueError('d21_attachment_plan_binding_invalid')


@dataclass(frozen=True,slots=True)
class HybridPlanContext(HttpPlanContext):
    file_proof_sources:tuple[tuple[int,str],...]=()


def attachment_binding(turn,index):
    if not isinstance(turn,HybridTurnBinding):raise ExecutionRecoveryRequired('d21_hybrid_turn_missing')
    matches=[b for b in turn.attachments if b.attachment_index==index]
    if len(matches)!=1:raise ExecutionRecoveryRequired('d21_attachment_not_authorized')
    return matches[0]


def file_plan_choice(proposal):
    return {k:getattr(proposal,k) for k in ('kind','attachment_index','template','observation_mode','source_proof_id')}
