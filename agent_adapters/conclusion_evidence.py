"""Immutable D8 request/result evidence; no ordinary raw-response export."""

from dataclasses import asdict

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.conclusion_contracts import D8ModelRequest
from agent_core.dual_llm_contracts import D3RecoveryRequired


class FileD8Evidence:
    def __init__(self, directory, *, create=True):
        self.records = T6CommandRecorder(directory, create=create)

    def save_request(self, request: D8ModelRequest) -> None:
        self.records._write("d8-request", request.model_request_id, asdict(request))

    def read_request(self, request_id: str) -> D8ModelRequest | None:
        try:
            value = self.records._read("d8-request", request_id)
            result = D8ModelRequest.from_dict(value) if value is not None else None
            if result is not None and result.model_request_id != request_id:
                raise ValueError("request identity mismatch")
            return result
        except (T6EvidenceError, ValueError, TypeError, KeyError) as exc:
            raise D3RecoveryRequired("d8_request_evidence_unavailable") from exc

    def save_result(self, request_id: str, result: dict) -> None:
        self.records._write("d8-result", request_id, result)

    def read_result(self, request_id: str) -> dict | None:
        try:
            result = self.records._read("d8-result", request_id)
            if result is not None and result.get("model_request_id") != request_id:
                raise ValueError("result identity mismatch")
            return result
        except (T6EvidenceError, ValueError, TypeError, KeyError) as exc:
            raise D3RecoveryRequired("d8_result_evidence_unavailable") from exc


def check_evidence_conclusions(directory, store, config, task_run_id):
    """Validate plan-less reviewed conclusions using immutable local evidence only."""
    from types import SimpleNamespace
    from agent_core.conclusion_review import ConclusionReview
    from agent_core.evidence_conclusions import EvidenceConclusions
    from agent_core.conclusion_contracts import conclusion_digest
    from agent_adapters.output_evidence import FileD8OutputEvidence
    from agent_adapters.handoff_evidence import FileD8HandoffEvidence
    from agent_adapters.model_evidence import FileD3ModelEvidence
    from agent_adapters.observation_proof_store import FileD3ProofStore
    from agent_adapters.workspace_evidence import FileD3WorkspaceEvidence
    from agent_core.http_contracts import is_http
    records=[r for r in store.list_d8_conclusions(task_run_id) if r.plan_id is None]
    rows=store.load_control_state_rows(task_run_id)
    if not records:
        if any((f.verification_ref or '').startswith('d8e:') for f in rows.facts):
            raise D3RecoveryRequired('d22_fact_approval_missing')
        return
    app=SimpleNamespace(store=store,config=config,state=rows,
        output_boundary=SimpleNamespace(evidence=FileD8OutputEvidence(directory/'d8-output',create=False)),
        model_evidence=FileD3ModelEvidence(directory/'d3-models',create=False),
        sessions=SimpleNamespace(evidence=FileD8HandoffEvidence(directory/'d8-sessions',create=False)),
        redactor=SimpleNamespace(redact_value=lambda value:value,contains_sensitive=lambda value:False))
    review=ConclusionReview(SimpleNamespace(control_state=rows),store,FileD8Evidence(directory/'d8-conclusions',create=False),
        None,None,config,None,None,app.redactor,lambda:0,lambda:False)
    app.conclusions=review
    review.app=app
    manager=EvidenceConclusions(app)
    app.evidence_conclusions=manager
    http=None
    if is_http(config):
        from agent_adapters.http_capability import readonly_capability
        http=readonly_capability(directory,store,config)
        from agent_adapters.http_experiments import HttpExperiments
        app.web=http
        native=SimpleNamespace(app=app,evidence=app.output_boundary.evidence)
        app.mcp=SimpleNamespace(experiments=HttpExperiments(native))
    proofs=FileD3ProofStore(directory/'d3-proofs',create=False)
    workspace=FileD3WorkspaceEvidence(directory/'d3-workspace',create=False)
    def sources(observation):
        for source in observation.sources:
            if source['kind']=='mcp':
                current=manager.source(source['ref'],observation.intent_id)
                if current!=source:raise D3RecoveryRequired('d22_evidence_source_version_changed')
            elif source['kind']=='http' and http is not None:
                current=http.observation(source['ref'],historical=True)
                if (current.source_version_sha256!=source['version'] or current.execution_id!=source['source_ref']
                        or (current.task_run_id,current.turn_id,current.intent_id)!=(observation.task_run_id,observation.turn_id,observation.intent_id)
                        or any(source['data'].get(k)!=v for k,v in asdict(current).items())):
                    raise D3RecoveryRequired('d22_evidence_source_version_changed')
            elif source['kind']=='attachment':
                proof=proofs.read(source['ref'])
                receipt=workspace.read_digest(proof.observation_id) if proof else None
                binding=store.load_d3_plan_turn_binding(proof.plan_id) if proof else None
                if (proof is None or receipt is None or binding is None
                        or (proof.task_run_id,proof.turn_id,proof.intent_id)!=(observation.task_run_id,observation.turn_id,observation.intent_id)
                        or proof.source_execution_id!=source['source_ref']
                        or conclusion_digest(dict(proof=asdict(proof),receipt=asdict(receipt),binding=asdict(binding)))!=source['version']):
                    raise D3RecoveryRequired('d22_evidence_source_version_changed')
            else:raise D3RecoveryRequired('d22_evidence_source_unavailable')
    manager.validate_sources=sources
    for record in records:manager.validate_record(record,rows)
    for fact in rows.facts:
        if (fact.verification_ref or '').startswith('d8e:'):manager.validate_fact(fact,rows)
