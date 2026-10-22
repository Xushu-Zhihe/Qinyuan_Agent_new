"""MCP candidate provenance without synthetic Plans or Executions."""
from dataclasses import dataclass
from uuid import UUID
from agent_core.dual_llm_modeling import D3CandidateContext
from agent_core.turn_modeling import digest


@dataclass(frozen=True, slots=True)
class MCPAnswerCandidateContext(D3CandidateContext):
    goal: str
    task_run_id: str
    turn_id: str
    intent_id: str
    source_ref: str
    evidence_version: str

    def __post_init__(self):
        if any(value is not None for value in (self.turn_binding,self.plan_binding,self.execution_id,self.plan_result)):
            raise ValueError("mcp_candidate_must_not_fabricate_execution")
        for value in (self.task_run_id,self.turn_id,self.intent_id,self.source_ref.removeprefix("mcp-result/")):
            parsed=UUID(value)
            if str(parsed)!=value or parsed.version not in {4,5}:raise ValueError("mcp_candidate_identity_invalid")
        if not self.source_ref.startswith("mcp-result/") or len(self.evidence_version)!=64 or any(c not in "0123456789abcdef" for c in self.evidence_version):
            raise ValueError("mcp_candidate_source_invalid")


def provenance(context):
    if isinstance(context,MCPAnswerCandidateContext):
        return dict(task_run_id=context.task_run_id,turn_id=context.turn_id,intent_id=context.intent_id,
                    plan_id=None,execution_id=None,evidence_version=context.evidence_version,
                    source_ref=context.source_ref,source_kind="mcp")
    return dict(task_run_id=context.plan_binding.task_run_id,turn_id=context.plan_binding.turn_id,
                intent_id=context.plan_binding.intent_id,plan_id=context.plan_binding.plan_id,
                execution_id=context.execution_id,evidence_version=context.plan_result.evidence_sha256,
                source_ref=context.plan_binding.artifact_version_id,source_kind="d3")


def restore_context(data):
    data=dict(data)
    if "source_ref" in data:return MCPAnswerCandidateContext(**data)
    from agent_core.http_contracts import turn_binding_from_dict,plan_binding_from_dict
    from agent_core.dual_llm_contracts import D3PlanResult
    from agent_core.answer_candidates import AnswerCandidateContext
    data["turn_binding"]=turn_binding_from_dict(data["turn_binding"])
    data["plan_binding"]=plan_binding_from_dict(data["plan_binding"])
    data["plan_result"]=D3PlanResult(**data["plan_result"])
    return AnswerCandidateContext(**data) if "goal" in data else D3CandidateContext(**data)


def validate_mcp_source(store,candidate,evidence=None):
    """Ledger ownership first; trusted callers additionally verify immutable files."""
    operation_id=candidate.source_ref.removeprefix("mcp-result/")
    operation=store.load_d4_operation(operation_id)
    if (not candidate.source_ref.startswith("mcp-result/") or candidate.plan_id is not None
        or candidate.execution_id is not None or operation is None
        or operation.start.task_run_id!=candidate.task_run_id
        or not operation.start.resource_ref.startswith("mcp/"+candidate.intent_id+"/")
        or operation.completion is None or operation.completion.result_kind!="completed"
        or operation.completion.evidence_ref!=candidate.source_ref
        or operation.completion.evidence_sha256!=candidate.evidence_version):
        raise ValueError("mcp_candidate_source_invalid")
    if evidence is not None:
        claim=evidence.read("mcp-call",operation_id); result=evidence.read("mcp-result",operation_id)
        if (claim is None or result is None or digest(result)!=candidate.evidence_version
            or digest(claim)!=operation.start.context_sha256 or result["request_sha256"]!=digest(claim)
            or claim["task_run_id"]!=candidate.task_run_id or claim["intent_id"]!=candidate.intent_id
            or result["payload"]["status"]!="completed"):
            raise ValueError("mcp_candidate_evidence_invalid")
    return operation
