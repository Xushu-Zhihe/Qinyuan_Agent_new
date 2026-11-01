"""Stage handoff into the existing protected candidate and trusted feedback lifecycle."""

import re
from uuid import uuid4

from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_core.goal_candidates import D7AnswerCandidate
from agent_core.goal_feedback import ExternalValidationFeedback, project_candidate_feedback
from agent_core.runtime import TaskRunStatus
from agent_core.stage_storage import _fail
from qinyuan_core import Category

# The submission head is declared by the frozen task goal; it is data, not code.
_CANDIDATE = re.compile(rb"^([A-Za-z0-9_]{1,32})\{(.*)\}$", re.S)
_HEAD_LINE = re.compile(r"(?m)^提交 flag 头[:：]\s*([A-Za-z][A-Za-z0-9_]{0,31})\s*$")


class StageGoal:
    def __init__(self, app):
        self.app = app
        self._submission_head = None

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
        original = self.grounded_value(value.source_ref)
        token = self.submission_token(value.candidate)
        if not token or result is None or not self.candidate_in_source(token, original):
            _fail("stage.candidate_not_in_original_source", Category.POLICY)
        candidate = D7AnswerCandidate(app.task_id, identity, source.turn_id, source.intent_id, None, None,
            operation.completion.evidence_sha256, "mcp-result/" + source.record_id, "mcp")
        app.evidence.save("goal-candidate", identity, {"candidate": candidate.to_dict(),
            "proposal_operation_id": request.proposal_operation_id, "review_operation_id": request.model_request_id})
        protected = D7CandidateEvidenceService(app.directory, app.task_id,
            trusted_adapter_ids=("competition", "human"), clock=app.clock)
        protected.save_pending(candidate, token, request.proposal_operation_id)
        app.controller._reload_after_commit()
        app.controller.register_answer_candidate(candidate, identity, manual_confirmation=app.goal_validation == "human")
        return True

    def grounded_value(self, source_ref):
        """Original text a candidate must appear in; empty when the source is unavailable."""
        app = self.app
        try:
            source = app.evidence.read_history(app.task_id, (source_ref,))[0]
        except (IndexError, KeyError):
            return b""
        result = app.evidence.read("mcp-result", source.record_id)
        claim = app.evidence.read("mcp-call", source.record_id)
        if result is None:
            return b""
        original = result["payload"].get("stdout", "").encode("utf-8")
        if claim is not None and "experiment_request" in claim:
            app.web.attach()
            original = app.web.protected.read(result["payload"]["http_response"]["body_evidence_id"])
        return original

    def grounds(self, value):
        token = self.submission_token(value.candidate)
        return bool(token) and self.candidate_in_source(token, self.grounded_value(value.source_ref))

    def submission_head(self):
        """The task-declared submission head, or empty when no substitution is enabled."""
        if self._submission_head is not None:
            return self._submission_head
        try:
            goal = self.app.state.task_run.request.goal
        except (AttributeError, TypeError):
            self._submission_head = ""
            return ""
        match = _HEAD_LINE.search(goal or "")
        self._submission_head = match.group(1) if match is not None else ""
        return self._submission_head

    def submission_token(self, candidate):
        """The flag token to submit, not the model's surrounding explanation.

        Models sometimes append a derivation to the candidate field. Only the
        first ``<declared head>{...}`` token is submitted, and the source check
        still has to ground that exact token. With no declared head, the raw
        candidate keeps the legacy exact-match behaviour.
        """
        if not candidate:
            return None
        head = self.submission_head()
        if not head:
            return candidate
        pattern = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(head.encode("ascii"))
            + rb"\{[^{}\r\n]{1,4096}\}")
        match = pattern.search(candidate.encode("utf-8"))
        return match.group(0).decode("utf-8") if match is not None else None

    def candidate_in_source(self, candidate, original):
        """Exact source text, or the same body with only the declared submission head replaced.

        The body stays byte-identical. A source token such as ``wectf{body}``
        may ground ``NSSCTF{body}`` when the task declares NSSCTF, but an
        arbitrary head, a changed body, or a token embedded in a longer word
        never grounds a candidate.
        """
        if not candidate or not original:
            return False
        raw = candidate.encode("utf-8")
        if raw in original:
            return True
        head = self.submission_head()
        if not head:
            return False
        match = _CANDIDATE.fullmatch(raw)
        if match is None:
            return False
        token_head, body = match.group(1), match.group(2)
        if (token_head.decode("ascii") != head or not body or len(body) > 4096
                or b"{" in body or b"}" in body):
            return False
        pattern = re.compile(rb"(?<![A-Za-z0-9_])([A-Za-z0-9_]{1,16})\{"
            + re.escape(body) + rb"\}")
        for item in pattern.finditer(original):
            # A source head such as wectf/CCTF must contain a letter; numeric
            # braces from JSON or tool formatting do not qualify.
            if any(65 <= byte <= 90 or 97 <= byte <= 122 for byte in item.group(1)):
                return True
        return False

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

