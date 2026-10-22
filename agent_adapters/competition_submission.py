"""Durable one-candidate platform submission; no model/tool scheduling here."""
import hashlib
import time
from pathlib import Path
from uuid import UUID
from agent_adapters.handoff_evidence import FileD8HandoffEvidence
from agent_adapters.run_files import immutable_read
from agent_adapters.workbench_contracts import WorkbenchError
from agent_adapters.competition import submit_candidate
from agent_core.goal_feedback import ExternalValidationFeedback,project_candidate_feedback
from agent_core.turn_modeling import digest
from agent_core.runtime import TaskRunStatus
from qinyuan_core import get_logger


class SubmissionEvidence(FileD8HandoffEvidence):
    KINDS={"start","receipt"}


def frozen_binding(path,declaration):
    value=immutable_read(Path(path))
    if (not isinstance(value,dict) or set(value)!={"request_id","profile","question","platform_deadline_at"}
        or value["request_id"]!=declaration["request_id"]
        or declaration.get("contract_version")!=22):raise ValueError("competition_run_binding_invalid")
    return value


def submit(app,binding,credential,operation_id,*,reader=None,send=submit_candidate):
    """Caller holds the existing run lock. Saved receipts apply without another POST."""
    if str(UUID(operation_id))!=operation_id or UUID(operation_id).version!=4:raise ValueError("competition_operation_invalid")
    state=app.state; task=state.task_run
    if task.status not in {TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,TaskRunStatus.RUNNING,TaskRunStatus.ACHIEVED}:
        raise ValueError("competition_task_not_available")
    evidence=SubmissionEvidence(app.directory/"competition-submissions")
    saved=evidence.read("start",operation_id)
    candidates=app.store.list_d7_answer_candidates(task.task_run_id)
    if saved is not None:
        candidate=next((c for c in candidates if c.candidate_id==saved["candidate_id"]),None)
    else:
        candidate=next((c for c in reversed(candidates) if project_candidate_feedback(task.task_run_id,c.candidate_id,c.evidence_version,
            app.store.list_d7_validation_feedback(task.task_run_id,c.candidate_id,c.evidence_version)).state=="pending"),None)
    if candidate is None:raise ValueError("competition_pending_candidate_missing")
    base={"operation_id":operation_id,"task_run_id":task.task_run_id,"candidate_id":candidate.candidate_id,
          "evidence_version":candidate.evidence_version,"binding_sha256":digest(binding)}
    if saved is not None and any(saved.get(k)!=v for k,v in base.items()):raise ValueError("competition_submission_binding_changed")
    receipt=evidence.read("receipt",operation_id)
    if saved is not None and receipt is None:
        return {"state":"unknown","code":"competition_submission_unknown","operation_id":operation_id}
    if saved is None:
        if task.status is not TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:raise ValueError("competition_candidate_not_awaited")
        if time.time()>=task.deadline_at:raise ValueError("competition_deadline_reached")
        platform_deadline=binding["platform_deadline_at"]
        if platform_deadline is not None and time.time()>=platform_deadline:raise ValueError("competition_question_not_active")
        if reader is None:
            from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
            reader=D7CandidateEvidenceService(app.directory,task.task_run_id,
                trusted_adapter_ids=("competition",),create=False).read_port("competition")
        flag=reader.read(candidate.candidate_id)
        body_sha=hashlib.sha256(flag.encode("utf-8")).hexdigest()
        prior_receipt=None
        for path in evidence.records.directory.glob("d8-handoff-start-*.json"):
            previous_id=path.stem.removeprefix("d8-handoff-start-")
            previous=evidence.read("start",previous_id)
            known=evidence.read("receipt",previous_id)
            if previous["binding_sha256"]!=base["binding_sha256"]:raise ValueError("competition_prior_binding_changed")
            if known is None or known.get("verdict")=="unknown":
                return {"state":"unknown","code":"competition_submission_unknown","operation_id":previous_id}
            if previous["body_sha256"]==body_sha and known.get("verdict") in {"accepted","partial","rejected"}:prior_receipt=known
        start={**base,"body_sha256":body_sha}
        evidence.save("start",operation_id,start)
        try:
            receipt=dict(prior_receipt) if prior_receipt else send(binding["profile"],credential,binding["question"]["remote_ref"],flag)
        except WorkbenchError as error:
            known_rejection=error.code in {"competition_auth_failed","competition_request_rejected","competition_invalid_state","competition_submission_invalid","competition_rate_limited"}
            receipt={"code":error.code,"state":"blocked" if known_rejection else "unknown"}
        finally:flag=None
        evidence.save("receipt",operation_id,receipt)
    verdict=receipt.get("verdict")
    if verdict in {"accepted","partial","rejected"}:
        feedback=ExternalValidationFeedback(task.task_run_id,candidate.candidate_id,candidate.evidence_version,
            verdict,"competition:"+operation_id,operation_id)
        app.controller.apply_external_validation_feedback(feedback)
    get_logger("ctrl").info("competition.submission_result",run=task.task_run_id,candidate=candidate.candidate_id,
        operation=operation_id,result=verdict or receipt.get("state","unknown"),code=receipt.get("code"))
    return {"state":verdict or receipt.get("state","unknown"),"operation_id":operation_id,
            **{k:v for k,v in receipt.items() if k in {"code","correct_flag_count","total_flag_count","duplicate","platform_state"}}}
