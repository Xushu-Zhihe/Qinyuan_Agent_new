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
    if (not isinstance(value,dict) or set(value)-{"schedule","attachment_id"}!={"request_id","profile","question","platform_deadline_at"}
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
            app.store.list_d7_validation_feedback(task.task_run_id,c.candidate_id,c.evidence_version)).state in {"pending","unknown"}),None)
    if candidate is None:raise ValueError("competition_pending_candidate_missing")
    base={"operation_id":operation_id,"task_run_id":task.task_run_id,"candidate_id":candidate.candidate_id,
          "evidence_version":candidate.evidence_version,"binding_sha256":digest(binding)}
    if saved is not None and any(saved.get(k)!=v for k,v in base.items()):raise ValueError("competition_submission_binding_changed")
    receipt=evidence.read("receipt",operation_id)
    if saved is not None and receipt is None:
        app.controller.apply_external_validation_feedback(ExternalValidationFeedback(task.task_run_id,candidate.candidate_id,
            candidate.evidence_version,"unknown","competition-start:"+operation_id,operation_id))
        return {"state":"unknown","code":"competition_submission_unknown","operation_id":operation_id}
    if saved is None:
        from agent_core.operator_controls import check_admission
        check_admission(app.store,task.task_run_id)
        if task.status is not TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:raise ValueError("competition_candidate_not_awaited")
        if app.clock()>=task.deadline_at:raise ValueError("competition_deadline_reached")
        platform_deadline=binding["platform_deadline_at"]
        if platform_deadline is not None and app.clock()>=platform_deadline:raise ValueError("competition_question_not_active")
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
            if previous["candidate_id"]==candidate.candidate_id and (known is None or known.get("verdict") in {"accepted","partial","rejected","unknown"} or known.get("state")=="unknown"):
                return submit(app,binding,credential,previous_id,reader=reader,send=send)
            if known is None or known.get("verdict")=="unknown" or known.get("state")=="unknown":
                return {"state":"unknown","code":"competition_submission_unknown","operation_id":previous_id}
            if previous["body_sha256"]==body_sha and known.get("verdict") in {"accepted","partial","rejected"}:prior_receipt=known
        start={**base,"body_sha256":body_sha}
        evidence.save("start",operation_id,start)
        try:
            receipt=dict(prior_receipt) if prior_receipt else send(binding["profile"],credential,binding["question"]["remote_ref"],flag)
        except WorkbenchError as error:
            known_rejection=error.code in {"competition_auth_failed","competition_request_rejected","competition_invalid_state","competition_submission_invalid","competition_rate_limited"}
            receipt={"code":error.code,**({"state":"blocked"} if known_rejection else {"verdict":"unknown"})}
        finally:flag=None
        evidence.save("receipt",operation_id,receipt)
    verdict=receipt.get("verdict") or ("unknown" if receipt.get("state")=="unknown" else None)
    if verdict in {"accepted","partial","rejected","unknown"}:
        feedback=ExternalValidationFeedback(task.task_run_id,candidate.candidate_id,candidate.evidence_version,
            verdict,"competition:"+operation_id,operation_id)
        app.controller.apply_external_validation_feedback(feedback)
    get_logger("ctrl").info("competition.submission_result",run=task.task_run_id,candidate=candidate.candidate_id,
        operation=operation_id,result=verdict or receipt.get("state","unknown"),code=receipt.get("code"))
    return {"state":verdict or receipt.get("state","unknown"),"operation_id":operation_id,
            **{k:v for k,v in receipt.items() if k in {"code","correct_flag_count","total_flag_count","duplicate","platform_state"}}}


def feedback_progress(directory,feedback):
    if feedback.source_ref!="competition:"+feedback.operation_id:return {}
    evidence=SubmissionEvidence(Path(directory)/"competition-submissions",create=False)
    start=evidence.read("start",feedback.operation_id);receipt=evidence.read("receipt",feedback.operation_id)
    if (start is None or receipt is None or start["task_run_id"]!=feedback.task_run_id
        or start["candidate_id"]!=feedback.candidate_id or start["evidence_version"]!=feedback.evidence_version
        or (receipt.get("verdict") or ("unknown" if receipt.get("state")=="unknown" else None))!=feedback.verdict):raise ValueError("competition_feedback_receipt_changed")
    return {k:v for k,v in receipt.items() if k in {"correct_flag_count","total_flag_count","duplicate","platform_state"}}


def end_unavailable(app,binding,credential,operation_id,*,read=None):
    """Read-only platform confirmation; never manufactures a Flag verdict."""
    from agent_adapters.competition import read_question
    from agent_core.runtime import TERMINAL_STATUSES
    if str(UUID(operation_id))!=operation_id or UUID(operation_id).version!=4:raise ValueError("competition_operation_invalid")
    if app.state.task_run.status in TERMINAL_STATUSES:return {"state":"already_terminal"}
    if app.store.blocking_d4_operations(app.state.task_run.task_run_id):raise ValueError("operator_unknown_operation")
    if app.clock()>=app.state.task_run.deadline_at:
        app.controller.advance_time(operation_id)
        return {"state":"local_deadline_exceeded"}
    read=read or read_question
    item=read(binding["profile"],credential,binding["question"]["remote_ref"])
    state=item["state"] if item is not None else None
    reasons={"failed":"competition_platform_failed","expired":"competition_platform_expired","abandoned":"competition_platform_abandoned"}
    if state not in reasons:raise ValueError("competition_platform_not_ended")
    evidence=SubmissionEvidence(app.directory/"competition-end")
    evidence.save("receipt",operation_id,{"task_run_id":app.state.task_run.task_run_id,
        "binding_sha256":digest(binding),"state":state})
    app.controller.platform_unavailable(reasons[state],operation_id)
    return {"state":state}
