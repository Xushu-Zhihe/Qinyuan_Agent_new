"""D8-4 safe conversation replay and a checkpoint before more inner work."""

import json
from dataclasses import asdict, fields

from agent_core.conclusion_contracts import D8ModelRequest, conclusion_digest
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.dual_llm_modeling import D8SessionExecutionRequest
from agent_core.handoff_contracts import handoff_json
from agent_core.session_contracts import D8DecisionRecord, D8SessionConclusionRequest, D8SessionChangeRequest
from agent_core.conclusion_revision_contracts import D8ChangeRequest
from agent_core.turn_modeling import stable_id


class IntentSessions:
    def __init__(self, app, evidence):
        self.app, self.store, self.evidence = app, app.store, evidence

    def _id(self, name):
        return stable_id(self.app.state.task_run.task_run_id, "d8-4/" + name)

    def records(self, intent_id=None):
        rows = self.store.list_d8_decisions(self.app.state.task_run.task_run_id)
        return tuple(r for r in rows if intent_id is None or r.intent_id == intent_id)

    def _scope(self, request):
        if isinstance(request, D8ChangeRequest):
            return next(i.turn_id for i in self.app.state.intents if i.intent_id == request.actor_intent_id), request.actor_intent_id
        if isinstance(request, D8ModelRequest):
            return request.observation.turn_id, request.observation.intent_id
        intent_id = getattr(request.context, "intent_id", None)
        if intent_id is None:
            intent_id = request.context.plan_binding.intent_id
        return next(i.turn_id for i in self.app.state.intents if i.intent_id == intent_id), intent_id

    def payload(self, record):
        data = self.evidence.read("decision", record.record_id)
        if (data is None or conclusion_digest(data) != record.evidence_sha256
                or data["metadata"] != asdict(record) | {"evidence_sha256": ""}):
            raise D3RecoveryRequired("d8_session_decision_evidence_unavailable")
        return data

    def _result(self, request_id, request):
        if "actor_intent_id" in request:
            saved = self.app.revisions.evidence.read("request", request_id)
            result = self.app.revisions._known(D8ChangeRequest.from_dict(saved)) if saved is not None else None
            if result is None or result["outcome"] != "valid":
                raise D3RecoveryRequired("d8_session_repair_result_unavailable")
            return result["raw"]
        if request["stage"] == "fact_proposal":
            result = self.app.conclusions.evidence.read_result(request_id)
            if result is None or result["outcome"] != "valid":
                raise D3RecoveryRequired("d8_session_conclusion_result_unavailable")
            return result["raw"]
        result = self.app.model_evidence.read(request_id)
        if result is None or result.outcome != "accepted":
            raise D3RecoveryRequired("d8_session_model_result_unavailable")
        return result.raw

    def _safe(self, value):
        if self.app.conclusions._sensitive(value):
            raise D3RecoveryRequired("d8_session_sensitive_content_rejected")

    def wrap(self, request):
        is_conclusion = isinstance(request, (D8ModelRequest, D8ChangeRequest))
        if not is_conclusion and request.role != "inner_action":
            return request
        if is_conclusion and request.stage != "fact_proposal":
            return request
        turn_id, intent_id = self._scope(request)
        saved = self.evidence.read("request", request.model_request_id)
        if saved is None:
            phase = "conclusion_repair" if isinstance(request, D8ChangeRequest) else "execution"
            _, _, current = self.app.handoff.select(phase, turn_id, intent_id)
            messages = []
            for record in self.records(intent_id):
                data = self.payload(record)
                messages.append({"role": "user", "content": data["request"],
                                 "identity": "historical_request_not_current_authorization"})
                messages.append({"role": "assistant", "content": data["response"],
                                 "identity": "historical_model_result_not_verified"})
                messages.append({"role": "tool", "content": data["observation"],
                                 "identity": "historical_execution_projection_not_current_approval"})
            session_id = self._id("session/" + intent_id)
            context = handoff_json({"messages": messages, "current_history": current,
                                    "session_scope": "intent", "session_id": session_id,
                                    "turn_id": turn_id, "intent_id": intent_id})
        else:
            if saved["turn_id"] != turn_id or saved["intent_id"] != intent_id:
                raise D3RecoveryRequired("d8_session_request_owner_changed")
            session_id = saved["request"]["session_id"]
            context = saved["request"]["session_context"]
        cls = (D8SessionChangeRequest if isinstance(request, D8ChangeRequest)
               else D8SessionConclusionRequest if isinstance(request, D8ModelRequest) else D8SessionExecutionRequest)
        values = {f.name: getattr(request, f.name) for f in fields(request)
                  if f.name not in {"session_id", "session_context"}}
        result = cls(**values, session_id=session_id, session_context=context)
        self.freeze(result)
        return result

    def freeze(self, request):
        if isinstance(request, (D8SessionExecutionRequest, D8SessionConclusionRequest, D8SessionChangeRequest)):
            turn_id, intent_id = self._scope(request)
            packet = json.loads(request.session_context)
            if (request.session_id != self._id("session/" + intent_id)
                    or packet.get("session_id") != request.session_id
                    or packet.get("session_scope") != "intent"
                    or (packet.get("turn_id"), packet.get("intent_id")) != (turn_id, intent_id)):
                raise D3RecoveryRequired("d8_session_scope_changed")
            value = {"turn_id": turn_id, "intent_id": intent_id, "request": asdict(request)}
            self._safe(value)
            length = len(request.session_context) + len(getattr(request, "history_context", ""))
            capacity = self.app.handoff.capacity()
            if length > capacity.capacity.visible_history:
                self.app.handoff._pause("visible_history", (), capacity.capacity.visible_history, length)
            self.app.handoff.check_role_request_capacity(request)
            self.evidence.save("request", request.model_request_id, value)
            if isinstance(request, D8SessionExecutionRequest):
                self.app.handoff.freeze_execution_request(request)
        else:
            self.app.handoff.check_role_request_capacity(request)

    def reconcile(self, *, review_signal=None):
        known = {r.root_request_id for r in self.records()}
        for call in self.store.list_d4_operations(self.app.state.task_run.task_run_id):
            if (call.start.role != "inner_action" or call.completion is None
                    or call.application_ref is None and call.start.operation_id != review_signal):
                continue
            snapshot = self.evidence.read("request", call.start.operation_id)
            if snapshot is None:
                raise D3RecoveryRequired("d8_session_applied_request_missing")
            request = snapshot["request"]
            root = request.get("correction_of_model_request_id") or call.start.operation_id
            if root in known:
                continue
            raw = self._result(call.start.operation_id, request)
            rows = self.records(snapshot["intent_id"])
            record = D8DecisionRecord(self._id("decision/" + root), call.start.task_run_id,
                                      snapshot["turn_id"], snapshot["intent_id"], len(rows) + 1,
                                      root, call.start.operation_id, "0" * 64,
                                      "review_requested" if call.application_ref is None else "applied")
            safe_request = {k: v for k, v in request.items()
                            if k not in {"session_context", "history_context", "process_facts"}}
            plan_id = (request.get("observation", {}).get("plan_id")
                       or request.get("context", {}).get("plan_binding", {}).get("plan_id"))
            executions = [e for e in self.app.state.executions if e.plan_id == plan_id]
            observed = {"application_ref": call.application_ref,
                        "completed_kind": call.completion.result_kind,
                        "execution_records": [asdict(e) for e in executions],
                        "facts": [asdict(f) for f in self.app.state.facts
                                  if f.source_ref in {e.execution_id for e in executions}],
                        "identity": "historical_observation_requires_current_governance_view"}
            data = {"metadata": asdict(record) | {"evidence_sha256": ""},
                    "request": safe_request, "response": raw, "observation": observed}
            frozen = self.evidence.read("decision", record.record_id)
            if frozen is not None:
                data = frozen  # Finish the original append; do not resample later state.
            self._safe(data)
            self.evidence.save("decision", record.record_id, data)
            checksum = conclusion_digest(data)
            record = D8DecisionRecord(**(asdict(record) | {"evidence_sha256": checksum}))
            self.app.controller.commit_d3_transition(
                "append_d8_decision", (record,), (record.record_id,), record.record_id, record.record_id)
            known.add(root)

    def validate_record(self, record, state):
        data = self.payload(record)
        call = self.store.load_d4_operation(record.model_request_id)
        request = self.evidence.read("request", record.model_request_id)
        root = request["request"].get("correction_of_model_request_id") or record.model_request_id if request else None
        signal = record.disposition == "review_requested"
        if (call is None or call.start.task_run_id != record.task_run_id or call.completion is None
                or call.application_ref is None and not signal or call.start.role != "inner_action"
                or request is None or root != record.root_request_id
                or (request["turn_id"], request["intent_id"]) != (record.turn_id, record.intent_id)
                or not signal and data["observation"]["application_ref"] != call.application_ref
                or signal and (data["request"]["stage"] != "fact_proposal"
                               or data["response"].get("kind") != "request_review")
                or handoff_json(data["response"]) != handoff_json(self._result(record.model_request_id, request["request"]))
                or data["request"] != {k: v for k, v in request["request"].items()
                                       if k not in {"session_context", "history_context", "process_facts"}}):
            raise D3RecoveryRequired("d8_session_decision_binding_changed")

    def reviews(self, intent_id):
        result = []
        for application in self.store.list_d3_review_applications(self.app.state.task_run.task_run_id):
            if application.intent_id != intent_id or application.paused_gate:
                continue
            root = self.app.review_ledger.read_root(application.review_root_id)
            call = next((c for c in root.get("calls", ()) if c.get("review_id") == application.review_id), None) if isinstance(root, dict) else None
            if call is None:
                raise D3RecoveryRequired("d8_progress_review_evidence_missing")
            context = json.loads(call["request"]["context_summary"])
            if "d8_progress" in context:
                self.app.validator._review_call(application.task_run_id, application.review_id)
                cited = {s[12:] for s in application.source_ids if s.startswith("d8-decision:")}
                rows = [r for r in self.records(intent_id) if r.record_id in cited]
                watermark = max((r.ordinal for r in rows), default=0)
                if (watermark < 4 or context["d8_progress"] != self.review_context(intent_id, watermark,
                                                        mode=context["d8_progress"].get("mode", "ordinary"))):
                    raise D3RecoveryRequired("d8_progress_applied_window_changed")
                result.append((watermark, application, call))
        return result

    def due(self, intent_id):
        reviews = self.reviews(intent_id)
        count = len(self.records(intent_id))
        return count >= (reviews[-1][0] if reviews else 0) + 4

    def review_context(self, intent_id, through=None, *, mode="ordinary"):
        rows = self.records(intent_id)[:through]
        stages = {}
        executions = set()
        for row in rows:
            data = self.payload(row)
            stage = data["request"]["stage"]
            stages[stage] = stages.get(stage, 0) + 1
            executions.update(e["execution_id"] for e in data["observation"]["execution_records"])
        return {"decision_count": len(rows), "window": 4, "continue_decisions": 4, "mode": mode,
                "cumulative": {"decision_stage_counts": stages, "saved_execution_count": len(executions),
                               "identity": "historical_progress_not_goal_verification"},
                "recent": [{"ref": "d8-decision:" + r.record_id,
                            "ordinal": r.ordinal, "data": self.payload(r)} for r in rows[-4:]]}

    async def before_decision(self, intent_id, receipts, token, *, restricted=False):
        self.reconcile()
        reviews = self.reviews(intent_id)
        if reviews and reviews[-1][2]["response"]["decision"] == "pause_current_intent":
            raise D3RecoveryRequired("d8_progress_" + reviews[-1][2]["response"]["progress"]["classification"])
        intent = next(i for i in self.app.state.intents if i.intent_id == intent_id)
        if intent.status.value != "running" or not self.due(intent_id):
            return False
        rows = self.records(intent_id)
        sources = (rows[-1].model_request_id,)
        root_id = self.app._id("review/action/" + rows[-1].model_request_id)
        pending = self.app.review_ledger.read_root(root_id)
        if isinstance(pending, dict) and pending.get("last_request"):
            original = pending["last_request"]
            marker = json.loads(original["context_summary"]).get("d8_progress")
            if marker is not None:
                if (original["task_run_id"] != self.app.state.task_run.task_run_id
                        or original["intent_id"] != intent_id
                        or marker["decision_count"] != len(rows)
                        or marker["mode"] != ("restricted_repair" if restricted else "ordinary")):
                    raise D3RecoveryRequired("d8_pending_progress_scope_changed")
                # A combined review may also include observation/rejection sources.
                # Recover the complete original snapshot instead of asking again.
                sources = tuple(original["source_versions"])
        previous = self.app._restricted_progress
        self.app._restricted_progress = restricted
        try:
            review_id, output = self.app._review(intent.turn_id, intent_id, "action",
                                                rows[-1].model_request_id,
                                                sources)
        finally:
            self.app._restricted_progress = previous
        if restricted:
            if output.decision == "pause_current_intent":
                raise D3RecoveryRequired("d8_progress_" + output.progress.classification)
            return False
        return await self.app._route(review_id, output, intent_id, receipts, token)

    @staticmethod
    def visible(request):
        packet = json.loads(request if isinstance(request, str) else request.session_context)["current_history"]
        result = set()
        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get("ref"), str):
                    result.add(value["ref"])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(packet)
        return result

    def next_suggestions(self, intent_id):
        self.reconcile()
        rows = [r for r in self.records(intent_id)
                if self.payload(r)["request"]["stage"] == "inner_complete"]
        if not rows:
            raise D3RecoveryRequired("d8_child_completion_suggestions_missing")
        data = self.payload(rows[-1])
        from agent_core.session_contracts import parse_next_intents
        snapshot = self.evidence.read("request", rows[-1].model_request_id)
        suggestions = parse_next_intents(data["response"]["next_intent_candidates"],
                                         self.visible(snapshot["request"]["session_context"]))
        return rows[-1].model_request_id, suggestions
