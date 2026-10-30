"""MCP candidate provenance without synthetic Plans or Executions."""
from dataclasses import dataclass
from uuid import UUID
from agent_core.turn_modeling import digest


@dataclass(frozen=True, slots=True)
class MCPAnswerCandidateContext:
    goal: str
    task_run_id: str
    turn_id: str
    intent_id: str
    source_ref: str
    evidence_version: str

    def __post_init__(self):
        if not isinstance(self.source_ref,str):raise ValueError("mcp_candidate_source_invalid")
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
    from agent_core.dual_llm_modeling import D3CandidateContext
    from agent_core.answer_candidates import AnswerCandidateContext
    data["turn_binding"]=turn_binding_from_dict(data["turn_binding"])
    data["plan_binding"]=plan_binding_from_dict(data["plan_binding"])
    data["plan_result"]=D3PlanResult(**data["plan_result"])
    return AnswerCandidateContext(**data) if "goal" in data else D3CandidateContext(**data)


def supports_mcp_candidate(operation, intent_id):
    """The current candidate protocol supports ordinary calls, not experiment children."""
    parts = (operation.start.resource_ref or "").split("/")
    return operation.start.category == "tool_dispatch" and len(parts) == 3 and parts[:2] == ["mcp", intent_id]


def validate_mcp_source(store,candidate,evidence=None):
    """Ledger ownership first; trusted callers additionally verify immutable files."""
    operation_id=candidate.source_ref.removeprefix("mcp-result/")
    operation=store.load_d4_operation(operation_id)
    stage = store.load_stage_run_config(candidate.task_run_id) is not None
    parts = operation.start.resource_ref.split("/") if operation is not None else []
    experiment = stage and len(parts) == 5 and parts[:3] == ["mcp", candidate.intent_id, "experiment"]
    if (not candidate.source_ref.startswith("mcp-result/") or candidate.plan_id is not None
        or candidate.execution_id is not None or operation is None
        or operation.start.category!="tool_dispatch"
        or operation.start.task_run_id!=candidate.task_run_id
        or not (supports_mcp_candidate(operation,candidate.intent_id) or experiment)
        or operation.completion is None or operation.completion.result_kind!="completed"
        or operation.completion.evidence_ref!=candidate.source_ref
        or operation.completion.evidence_sha256!=candidate.evidence_version):
        raise ValueError("mcp_candidate_source_invalid")
    model_id=operation.start.resource_ref.split("/",2)[-1]
    if stage:
        from agent_core.stage_contracts import PlanningSession
        model_id = parts[3] if experiment else parts[2]
        model = store.load_d4_operation(model_id)
        turn = store.load_turn(candidate.task_run_id, candidate.turn_id)
        if (turn is None or not any(r.road_id == candidate.intent_id for r in turn.roads)
                or model is None or model.start.category != "action" or model.start.role != "inner_action"
                or model.start.task_run_id != candidate.task_run_id or model.application_ref != (model_id if experiment else operation_id)
                or model.completion is None or model.completion.result_kind != "completed"):
            raise ValueError("stage_candidate_source_invalid")
        if evidence is not None:
            evidence.observation(operation)
            source = evidence.read_model_result(model)
            claim = evidence.read("mcp-call", operation_id)
            result = evidence.read("mcp-result", operation_id)
            if (source.session != PlanningSession("road_execution", candidate.intent_id)
                    or claim is None or result is None or digest(result) != candidate.evidence_version
                    or digest(claim) != operation.start.context_sha256 or result["request_sha256"] != digest(claim)
                    or claim["task_run_id"] != candidate.task_run_id or claim["intent_id"] != candidate.intent_id
                    or claim["model_request_id"] != model_id or result["payload"]["status"] != "completed"):
                raise ValueError("stage_candidate_evidence_invalid")
        return operation
    model=store.load_d3_operation(model_id)
    if (model is None or model.start.task_run_id!=candidate.task_run_id or model.start.category!="inner_model"
        or model.application_ref!=operation_id or model.completion is None or model.completion.result_kind!="completed"):
        raise ValueError("mcp_candidate_model_binding_invalid")
    if evidence is not None:
        claim=evidence.read("mcp-call",operation_id); result=evidence.read("mcp-result",operation_id)
        if (claim is None or result is None or digest(result)!=candidate.evidence_version
            or digest(claim)!=operation.start.context_sha256 or result["request_sha256"]!=digest(claim)
            or claim["task_run_id"]!=candidate.task_run_id or claim["intent_id"]!=candidate.intent_id
            or claim["model_request_id"]!=model_id
            or result["payload"]["status"]!="completed"):
            raise ValueError("mcp_candidate_evidence_invalid")
    return operation
