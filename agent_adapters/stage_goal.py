"""Stage handoff into the existing protected candidate and trusted feedback lifecycle."""

from uuid import uuid4

from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_core.goal_candidates import D7AnswerCandidate
from agent_core.goal_feedback import ExternalValidationFeedback, project_candidate_feedback
from agent_core.runtime import TaskRunStatus
from agent_core.stage_storage import _fail
from qinyuan_core import Category


class StageGoal:
    def __init__(self, app):
        self.app = app

    def handoff(self, request, value):
        app = self.app
        binding = app.evidence.read("candidate-binding", request.proposal_operation_id)
        if binding is None:
            binding = {"candidate_id": str(uuid4())}
            app.evidence.save("candidate-binding", request.proposal_operation_id, binding)
        identity = binding["candidate_id"]
        existing = app.store.load_d7_answer_candidate(identity)
        if existing is not None:
            state = project_candidate_feedback(app.task_id, identity, existing.evidence_version,
                app.store.list_d7_validation_feedback(app.task_id, identity, existing.evidence_version))
            return state.state not in {"rejected", "partial"}
        source = app.evidence.read_history(app.task_id, (value.source_ref,))[0]
        operation = app.store.load_d4_operation(source.record_id)
        result = app.evidence.read("mcp-result", source.record_id)
        claim = app.evidence.read("mcp-call", source.record_id)
        original = result["payload"].get("stdout", "").encode("utf-8") if result else b""
        if claim is not None and "experiment_request" in claim:
            app.web.attach()
            original = app.web.protected.read(result["payload"]["http_response"]["body_evidence_id"])
        if not value.candidate or result is None or value.candidate.encode("utf-8") not in original:
            _fail("stage.candidate_not_in_original_source", Category.POLICY)
        candidate = D7AnswerCandidate(app.task_id, identity, source.turn_id, source.intent_id, None, None,
            operation.completion.evidence_sha256, "mcp-result/" + source.record_id, "mcp")
        app.evidence.save("goal-candidate", identity, {"candidate": candidate.to_dict(),
            "proposal_operation_id": request.proposal_operation_id, "review_operation_id": request.model_request_id})
        protected = D7CandidateEvidenceService(app.directory, app.task_id,
            trusted_adapter_ids=("competition", "human"), clock=app.clock)
        protected.save_pending(candidate, value.candidate, request.proposal_operation_id)
        app.controller._reload_after_commit()
        app.controller.register_answer_candidate(candidate, identity, manual_confirmation=app.goal_validation == "human")
        return True

    def decide(self, candidate_id, action, operation_id):
        app = self.app
        if app.goal_validation != "human" or action not in {"accept", "continue", "end"}:
            _fail("stage.human_decision_invalid", Category.POLICY)
        candidate = app.store.load_d7_answer_candidate(candidate_id)
        if candidate is None or candidate.task_run_id != app.task_id:
            _fail("stage.human_candidate_missing", Category.POLICY)
        app.controller._reload_after_commit()
        replay = app.store.load_committed_mutation(app.task_id, operation_id)
        if replay is not None and action == "end" and app.state.task_run.failure_reason != "human_unverified_end":
            _fail("stage.human_decision_identity_changed", Category.POLICY)
        if replay is None:
            app.controller.advance_time(str(uuid4()))
            if app.state.task_run.status is not TaskRunStatus.AWAITING_CONFIRMATION:
                _fail("stage.human_confirmation_closed", Category.POLICY)
        if (replay is None or action == "end") and app.store.list_d7_answer_candidates(app.task_id)[-1].candidate_id != candidate_id:
            _fail("stage.human_candidate_stale", Category.POLICY)
        if action == "end":
            if replay is not None:
                app.controller._assert_finish_replay(replay, {TaskRunStatus.INCONCLUSIVE.value})
                return
            app.controller.conclude(TaskRunStatus.INCONCLUSIVE, operation_id, reason="human_unverified_end")
        else:
            app.controller.apply_external_validation_feedback(ExternalValidationFeedback(app.task_id, candidate_id,
                candidate.evidence_version, "accepted" if action == "accept" else "rejected",
                "human:" + operation_id, operation_id))

