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
        from agent_core.strategy_supervision import StrategySupervision
        app.supervision = StrategySupervision(app)
        app.store.live_strategy_operation = app.supervision.live

    def _id(self, name):
        return stable_id(self.app.state.task_run.task_run_id, "d8-4/" + name)

    @staticmethod
    def history_request(request):
        value = {k: v for k, v in request.items() if k not in {"session_context", "history_context", "process_facts"}}
        advice = json.loads(request.get("history_context", "{}") or "{}").get("strategy_advice")
        if advice and "reason" in advice:
            value["strategy_advice"] = advice
        return value

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
        notice = self.app.conclusions.optional_failure(request_id)
        if notice is not None and notice["failed_request_id"] == request_id:
            return {"kind": "technical_result", "reason": "optional_format_exhausted",
                    "identity": "unreviewed_optional_format_failure"}
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
        from qinyuan_core.logging import timed_phase
        with timed_phase("model", "session_assembly", run=self.app.state.task_run.task_run_id,
                         request=request.model_request_id):
            return self._wrap(request)

    def _wrap(self, request):
        is_conclusion = isinstance(request, (D8ModelRequest, D8ChangeRequest))
        if not is_conclusion and request.role != "inner_action":
            return request
        if is_conclusion and request.stage != "fact_proposal":
            return request
        turn_id, intent_id = self._scope(request)
        saved = self.evidence.read("request", request.model_request_id)
        cls = (D8SessionChangeRequest if isinstance(request, D8ChangeRequest)
               else D8SessionConclusionRequest if isinstance(request, D8ModelRequest) else D8SessionExecutionRequest)
        values = {f.name: getattr(request, f.name) for f in fields(request)
                  if f.name not in {"session_id", "session_context"}}
        if saved is None:
            phase = "conclusion_repair" if isinstance(request, D8ChangeRequest) else "execution"
            _, _, current = self.app.handoff.select(phase, turn_id, intent_id)
            records = self.records(intent_id)
            summary_record, summary = self.app.handoff._latest_summary()
            continuous = self.app.config.intent_execution_policy == "continuous_v1"
            compact = bool(self.app.config.intent_execution_policy and summary and summary_record.origin == "model_organized" and current["global_source_status"] == "available")
            segment_start = 1
            payloads = {}
            def payload_for(record):
                if record.record_id not in payloads:
                    payloads[record.record_id] = self.payload(record)
                return payloads[record.record_id]

            target_start = segment_start
            if continuous and records:
                prior = self.evidence.read("request", records[-1].model_request_id)
                previous = json.loads(prior["request"].get("session_context", "{}")) if prior else {}
                segment_start = target_start = previous.get("segment_start", 1) if compact else 1
                # Only the summary's exact source versions can replace older dialog.
                covered = {(i["record_id"], i["source_version"]) for i in summary.get("all_items", ())} if compact else set()
                recent_start = records[-4].ordinal if len(records) >= 4 else 1
                for record in records:
                    if record.ordinal < segment_start:
                        continue
                    if record.ordinal >= recent_start or ("decision/" + record.record_id, conclusion_digest(asdict(record))) not in covered:
                        break
                    target_start = record.ordinal + 1
                history_bytes = len(handoff_json([payload_for(r) for r in records if r.ordinal >= segment_start]).encode("utf-8"))
                if history_bytes > self.app.handoff.capacity().capacity.visible_history:
                    segment_start = target_start
            session_id = self._id("session/" + intent_id)

            def context_for(start):
                omitted = [r for r in records if r.ordinal < start] if continuous else records[:-4] if compact else []
                visible = [r for r in records if r.ordinal >= start] if continuous else records[-4:] if compact else records
                messages = []
                for record in visible:
                    data = payload_for(record)
                    messages.extend([
                        {"role": "user", "content": data["request"], "identity": "historical_request_not_current_authorization"},
                        {"role": "assistant", "content": data["response"], "identity": "historical_model_result_not_verified"},
                        {"role": "tool", "content": data["observation"], "identity": "historical_execution_projection_not_current_approval"}])
                packet = {"messages": messages, "current_history": current,
                          "session_scope": "intent", "session_id": session_id,
                          "turn_id": turn_id, "intent_id": intent_id}
                if continuous:
                    packet["segment_start"] = start
                if self.app.config.intent_execution_policy:
                    packet["working_record_policy"] = "unreviewed_v1"
                if omitted:
                    decision_refs = {i.record_id: i.ref for i in self.app.handoff.history()}
                    packet["compaction"] = {"identity": "summary_and_recent_dialog_not_full_history",
                        "summary_id": summary_record.record_id,
                        "omitted_decision_refs": [decision_refs["decision/" + r.record_id] for r in omitted],
                        "details_via": "history_query/history_details", "raw_decisions_retained": True}
                return handoff_json(packet)

            context = context_for(segment_start)
            if continuous and target_start > segment_start:
                draft = cls(**values, session_id=session_id, session_context=context)
                if not self.app.handoff._fits_complete_request(draft, self.app.config.handoff_config.inner_capacity):
                    # Preflight is read-only. Freeze/measurement happens only after
                    # this one bounded compaction, never twice under the same ID.
                    context = context_for(target_start)
        else:
            if saved["turn_id"] != turn_id or saved["intent_id"] != intent_id:
                raise D3RecoveryRequired("d8_session_request_owner_changed")
            session_id = saved["request"]["session_id"]
            context = saved["request"]["session_context"]
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
            context_sha256 = getattr(request, "context_sha256", None)
            if isinstance(context_sha256, str):
                value["context_sha256"] = context_sha256
            self._safe(value)
            length = len(request.session_context) + len(getattr(request, "history_context", ""))
            capacity = self.app.handoff.capacity()
            if length > capacity.capacity.visible_history and self.app.config.intent_execution_policy != "continuous_v1":
                self.app.handoff._pause("visible_history", (), capacity.capacity.visible_history, length)
            # Continuous mode admits the actual shared provider payload below;
            # duplicated pre-projection sections are not an independent hard stop.
            self.app.handoff.check_role_request_capacity(request)
            self.evidence.save("request", request.model_request_id, value)
            if isinstance(request, D8SessionExecutionRequest):
                self.app.handoff.freeze_execution_request(request)
        else:
            self.app.handoff.check_role_request_capacity(request)

    def reconcile(self, *, review_signal=None):
        from agent_core.intent_execution import reconcile_working
        reconcile_working(self.app)
        known = {r.root_request_id for r in self.records()}
        for call in self.store.list_d4_operations(self.app.state.task_run.task_run_id):
            if call.start.role != "inner_action" or call.completion is None:
                continue
            interruption = self.app.handoff.evidence.read("strategy-discard", call.start.operation_id)
            if interruption and interruption.get("disposition") != "retained_for_processing":
                if call.application_ref is None:
                    self.store.mark_d3_operation_applied(call.start.operation_id, call.start.operation_id)
                continue
            optional = self.app.conclusions.finish_optional_failure(call.start.operation_id)
            if not optional and call.application_ref is None and call.start.operation_id != review_signal:
                continue
            call = self.store.load_d4_operation(call.start.operation_id)
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
                                      "technical_failure" if optional else "review_requested" if call.application_ref is None else "applied")
            safe_request = self.history_request(request)
            plan_id = (request.get("observation", {}).get("plan_id")
                       or (request.get("context", {}).get("plan_binding") or {}).get("plan_id"))
            executions = [e for e in self.app.state.executions if e.plan_id == plan_id]
            observed = {"application_ref": call.application_ref,
                        "completed_kind": call.completion.result_kind,
                        "execution_records": [asdict(e) for e in executions],
                        "facts": [asdict(f) for f in self.app.state.facts
                                  if f.source_ref in {e.execution_id for e in executions}],
                        "identity": "historical_observation_requires_current_governance_view"}
            if self.app.mcp is not None and call.application_ref is not None:
                tool_result = self.app.mcp.feedback(call.application_ref)
                if tool_result is not None:
                    observed["mcp_tool_result"] = tool_result
            if raw.get("kind") == "intent_resolution" and call.application_ref == self.app._id("continuation-reuse/" + call.start.operation_id):
                from agent_core.intent_execution import validate_continuation_reuse
                observed["intent_review"] = validate_continuation_reuse(self.app, call.start.operation_id)
            conclusion=next((r for r in self.store.list_d8_conclusions(call.start.task_run_id)
                if r.plan_id is None and r.proposal_request_id==call.start.operation_id),None)
            if conclusion is not None:
                reviewed=self.app.conclusions.evidence.read_result(conclusion.review_request_id)
                observed["conclusion_review"]={"outcome":conclusion.outcome,"fact_id":conclusion.fact_id,
                    "decision":reviewed["raw"]["decision"],"reason":reviewed["raw"]["reason"]}
            notice = self.app.conclusions.optional_failure(call.start.operation_id)
            if notice is not None:
                observed["optional_conclusion_failure"] = notice
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
        technical = record.disposition == "technical_failure"
        if call is not None and call.application_ref == self.app._id("continuation-reuse/" + record.model_request_id):
            from agent_core.intent_execution import validate_continuation_reuse
            if data["observation"].get("intent_review") != validate_continuation_reuse(self.app, record.model_request_id):
                raise D3RecoveryRequired("d22_continuation_reuse_changed")
        if technical and not self.app.conclusions.optional_ready(record.model_request_id):
            raise D3RecoveryRequired("d22_optional_failure_binding_changed")
        if (call is None or call.start.task_run_id != record.task_run_id or call.completion is None
                or call.application_ref is None and not signal and not technical or call.start.role != "inner_action"
                or request is None or root != record.root_request_id
                or (request["turn_id"], request["intent_id"]) != (record.turn_id, record.intent_id)
                or not signal and data["observation"]["application_ref"] != call.application_ref
                or signal and (data["request"]["stage"] != "fact_proposal"
                               or data["response"].get("kind") != "request_review")
                or handoff_json(data["response"]) != handoff_json(self._result(record.model_request_id, request["request"]))
                or data["request"] != self.history_request(request["request"])):
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
                if self.app.config.contract_version in {13, 14, 15, 16, 17, 18, 19, 20, 21, 22}:
                    from agent_core.progress_policy import policy_for
                    from agent_core.review_contracts import D2ReviewRequest
                    request = D2ReviewRequest(**{**call["request"], "source_versions": tuple(call["request"]["source_versions"])})
                    watermark = policy_for(request)["decision_watermark"]
                progress = context["d8_progress"]
                if (watermark < 4
                        or progress != self.review_context(intent_id, watermark,
                            mode=progress.get("mode", "ordinary"), execution_refs=progress.get("execution_refs"))):
                    raise D3RecoveryRequired("d8_progress_applied_window_changed")
                result.append((watermark, application, call))
        return result

    def execution_refs(self, intent_id):
        """Known completed tools only; failed/duplicate observations still count."""
        plans = {p.plan_id for p in self.app.state.plans if p.intent_id == intent_id}
        executions = {e.execution_id for e in self.app.state.executions
                      if e.plan_id in plans and e.status.value != "running"}
        return [o.start.operation_id for o in self.store.list_d4_operations(self.app.state.task_run.task_run_id)
                if o.completion is not None and o.start.category == "tool_dispatch"
                and (o.start.operation_id in executions
                     or o.start.resource_ref.startswith("mcp/" + intent_id + "/"))]

    def consolidation_context(self, intent_id):
        """One optional reminder per existing four-tool window; no new calls or Facts."""
        if self.app.config.intent_execution_policy != "continuous_v1":
            return None
        previous = ()
        for row in reversed(self.records(intent_id)):
            saved = self.evidence.read("request", row.model_request_id)
            context = json.loads(saved["request"].get("history_context", "{}") or "{}") if saved else {}
            marker = context.get("knowledge_checkpoint")
            if marker is not None:
                previous = marker["execution_refs"]
                break
        refs = self.execution_refs(intent_id)
        if len(set(refs) - set(previous)) < 4:
            return None
        return {"identity": "optional_phase_consolidation_not_stop_or_fact", "execution_refs": refs}

    def handoff_progress(self, intent_id):
        """Reference known progress rather than invent a summary after a model failure."""
        rows = self.records(intent_id)[-4:]
        for row in rows:
            self.payload(row)
        order = {ref: n for n, ref in enumerate(self.execution_refs(intent_id))}
        sources = self.app.evidence_conclusions.catalog(intent_id)
        sources.sort(key=lambda s: order.get(self.app.evidence_conclusions.source(s["ref"], intent_id)["source_ref"], -1))
        return {"decision_refs": ["progress:decision/" + r.record_id + ":" + conclusion_digest(asdict(r)) for r in rows],
                "evidence_sources": sources[-4:]}

    def validate_handoff_progress(self, intent_id, progress):
        refs = {"progress:decision/" + r.record_id + ":" + conclusion_digest(asdict(r)) for r in self.records(intent_id)}
        if (set(progress) != {"decision_refs", "evidence_sources"}
                or any(r not in refs for r in progress["decision_refs"])
                or any(self.app.evidence_conclusions.source(s["ref"], intent_id)["version"] != s["version"]
                       for s in progress["evidence_sources"])):
            raise D3RecoveryRequired("d22_handoff_progress_source_changed")

    def due(self, intent_id):
        reviews = self.reviews(intent_id)
        rows = self.records(intent_id)
        watermark = reviews[-1][0] if reviews else 0
        if self.app.config.intent_execution_policy != "continuous_v1":
            return len(rows) >= watermark + 4
        previous = (json.loads(reviews[-1][2]["request"]["context_summary"])["d8_progress"]
                    if reviews else {})
        supervisor = getattr(self.app, "supervision", None)
        attempts = supervisor.watermark(intent_id) if supervisor else []
        if attempts:
            previous = attempts[-1]
            watermark = previous["decision_count"]
        if len(set(self.execution_refs(intent_id)) - set(previous.get("execution_refs", ()))) >= 4:
            return True
        # Pure reasoning/history loops must not evade review by never executing.
        idle = 0
        for row in rows[watermark:]:
            data = self.payload(row)
            if data["request"]["stage"] != "inner_plan":
                continue
            kind = data["response"].get("kind")
            if kind in {"http_request", "http_request_saved", "mcp_call", "attachment_observation"} or "template" in data["response"]:
                idle = 0
            else:
                idle += 1
        return idle >= 4

    def review_context(self, intent_id, through=None, *, mode="ordinary", execution_refs=None, compact=False):
        rows = self.records(intent_id)[:through]
        stages = {}
        executions = set()
        for row in rows:
            data = self.payload(row)
            stage = data["request"]["stage"]
            stages[stage] = stages.get(stage, 0) + 1
            executions.update(e["execution_id"] for e in data["observation"]["execution_records"])
        tools = {}
        if (self.app.config.intent_execution_policy == "continuous_v1" and through is None) or execution_refs is not None:
            current = self.execution_refs(intent_id)
            refs = current if execution_refs is None else execution_refs
            if current[:len(refs)] != refs:
                raise D3RecoveryRequired("d22_progress_execution_window_changed")
            tools = {"execution_refs": refs}
            executions.update(refs)
        recent = []
        for row in rows[-4:]:
            data = self.payload(row)
            if compact:
                # Retain decisions and observations, not a nested copy of their old input.
                data = {**data, "request": {k: v for k, v in self.history_request(data["request"]).items()
                                          if k != "context"}}
            recent.append({"ref": "d8-decision:" + row.record_id, "ordinal": row.ordinal, "data": data})
        return {**tools, **({"intent_execution_policy": "rolling_v1"} if self.app.config.intent_execution_policy else {}),
                "decision_count": len(rows), "window": 4, "continue_decisions": 4, "mode": mode,
                "cumulative": {"decision_stage_counts": stages, "saved_execution_count": len(executions),
                               "identity": "historical_progress_not_goal_verification"},
                "recent": recent}

    async def before_decision(self, intent_id, receipts, token, *, restricted=False):
        self.reconcile()
        self.app.supervision.poll()
        from agent_core.intent_execution import stop_request
        if self.app.config.intent_execution_policy and stop_request(self.app, intent_id) is not None:
            return False
        reviews = self.reviews(intent_id)
        if reviews and reviews[-1][2]["response"]["decision"] == "pause_current_intent":
            raise D3RecoveryRequired("d8_progress_" + reviews[-1][2]["response"]["progress"]["classification"])
        intent = next(i for i in self.app.state.intents if i.intent_id == intent_id)
        if intent.status.value != "running":
            return False
        if not self.due(intent_id):
            await self.organize_if_needed(intent_id, token)
            return False
        if self.app.config.intent_execution_policy == "continuous_v1" and not restricted:
            self.app.supervision.start(intent_id)
            await self.organize_if_needed(intent_id, token)
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
        # Organize before routing: replan may immediately assemble another inner request.
        if self.app.config.intent_execution_policy == "continuous_v1":
            await self.organize_if_needed(intent_id, token)
        elif self.app.config.intent_execution_policy and output.decision != "pause_current_intent":
            await self.app.handoff.summarize(intent.turn_id, token)
        return await self.app._route(review_id, output, intent_id, receipts, token)

    async def organize_if_needed(self, intent_id, token):
        if self.app.config.intent_execution_policy != "continuous_v1":
            return
        rows = self.records(intent_id)
        if len(rows) <= 4:
            return
        _, summary = self.app.handoff._latest_summary()
        covered = {i["record_id"] for i in summary.get("all_items", ())} if summary else set()
        if all("decision/" + r.record_id in covered for r in rows[:-4]):
            return
        if len(handoff_json([self.payload(r) for r in rows]).encode("utf-8")) > self.app.handoff.capacity().capacity.visible_history:
            intent = next(i for i in self.app.state.intents if i.intent_id == intent_id)
            await self.app.handoff.summarize(intent.turn_id, token)

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
        if self.app.config.intent_execution_policy:
            success = next((r for r in self.app.store.list_d8_tree_records(self.app.state.task_run.task_run_id)
                            if r.intent_id == intent_id and r.kind == "succeeded"), None)
            if success is None:
                raise D3RecoveryRequired("d22_next_suggestions_without_success")
            raw = self.app.model_evidence.read(success.inner_request_id).raw
            from agent_core.intent_execution import parse_saved_resolution
            return success.inner_request_id, parse_saved_resolution(self.app, success.inner_request_id, raw).next_intent_candidates
        rows = [r for r in self.records(intent_id)
                if self.payload(r)["request"]["stage"] == "inner_complete"]
        if not rows:
            raise D3RecoveryRequired("d8_child_completion_suggestions_missing")
        data = self.payload(rows[-1])
        from agent_core.session_contracts import parse_next_intents
        snapshot = self.evidence.read("request", rows[-1].model_request_id)
        suggestions = parse_next_intents(data["response"]["next_intent_candidates"],
                                         self.visible(snapshot["request"]["session_context"]),
                                         maximum=None if self.app.config.contract_version == 22 else 2)
        return rows[-1].model_request_id, suggestions
