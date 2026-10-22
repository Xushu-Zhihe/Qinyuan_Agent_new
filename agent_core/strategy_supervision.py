"""Read-only background strategy computation; the scheduler owns all effects."""

import json
import threading
from dataclasses import asdict, dataclass, replace

from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.handoff_contracts import D8HandoffRequest, D8TreeRecord, handoff_json
from agent_core.modeling import ModelError
from agent_core.operation_accounting import D4OperationStart
from agent_core.turn_modeling import digest


GUIDANCE = (
    "Evaluate current progress against the unchanged Intent and compare the Turn alternatives. "
    "No new Fact, repeated responses, or elapsed time alone do not prove useless work. "
    "Consider hypothesis discrimination, finite enumeration coverage, prerequisites and remaining cost. "
    "Judge evidence rather than reviewer authority; do not prefer continuing or switching merely by role. "
    "Request Intent resolution only with cited grounds to stop the exploration; "
    "this is not an Intent verdict or TaskRun cancellation. An optional suggestion compares one pending "
    "sibling with the current direction using evidence and explicit uncertainty. Explain why continuing "
    "the current direction may still help. Do not demand acceptance or call an alternative proven."
)


COMPARISON_GUIDANCE = (
    " Distinguish feasibility from priority: a route can remain possible while a sibling offers a better "
    "next discriminating experiment. Compare prerequisites, remaining cost, finite coverage and remaining "
    "time, without invented probabilities. A new body size or response is not itself information gain; "
    "tie it to a hypothesis supported, contradicted or still unresolved. Separate script/measurement "
    "failure from target counterevidence. Request-shape or response groups are descriptive: cookies, "
    "conditions and timing may differ, and repeated observations can be useful controls. Do not infer "
    "uselessness from count alone. Review recent_tools as individual completed requests even when one "
    "model-selected experiment issued many of them. Use original decisions, working records and "
    "observations for the hypothesis; statistics are not verified knowledge. Explain the new judgment "
    "and its decisive refs concisely instead of repeating the full history. An optional alternative can "
    "be preferable without the current Intent being completed or invalid. Suggestions remain voluntary. "
    "Meaningful finite enumeration needs an explicit tested domain and increasing coverage or a "
    "discriminating next check; an endlessly growing list is not a closure criterion. Separate success "
    "coverage from scoped negative findings and review checkpoints. A request for resolution opens a "
    "review of the latest evidence, including the inner's disagreement; it is not a required failure verdict."
)


def schema(request):
    from agent_core.output_contracts import obj, array, text
    packet = json.loads(request.prior_summary)
    refs = {"type": "string", "enum": [i.ref for i in request.history]}
    targets = [i["intent_id"] for i in packet["turn_intents"]
               if i["status"] == "pending" and i["intent_id"] != request.intent_id]
    advice = {"type": "null"}
    if targets:
        advice = {"anyOf": [advice, obj({
            "intent_id": {"type": "string", "enum": targets}, "reason": text(512),
            "uncertainty": text(512), "current_direction_value": text(512),
            "evidence_refs": {**array(refs, 1, 8), "uniqueItems": True}})]}
    return obj({"decision": {"type": "string", "enum": ["continue_current_intent", "request_intent_resolution"]},
                "reason": text(), "evidence_refs": {**array(refs, 1, 8), "uniqueItems": True,
                    "contains": {"type": "string", "const": packet["latest_decision_ref"]}, "minContains": 1},
                "suggestion": advice}, optional=("suggestion",))


def parse(request, raw):
    from agent_core.output_contracts import decode_response
    value, issues, terminal, _ = decode_response(raw, schema(request))
    if issues or terminal or not value["reason"].strip() or "\x00" in value["reason"]:
        raise ValueError("d22_supervision_output_invalid")
    if value.get("suggestion") and any(not value["suggestion"][k].strip() or "\x00" in value["suggestion"][k]
            for k in ("reason", "uncertainty", "current_direction_value")):
        raise ValueError("d22_supervision_suggestion_invalid")
    return value


@dataclass(frozen=True)
class StrategyYield:
    review_id: str
    reason: str
    content: str


def yield_schema(request):
    from agent_core.output_contracts import tagged, text
    context = request.get("history_context", "{}") if isinstance(request, dict) else getattr(request, "history_context", "{}")
    advice = json.loads(context or "{}").get("strategy_advice")
    if not advice:
        return None
    return tagged("strategy_yield", {"review_id": {"type": "string", "const": advice["review_id"]},
                                     "reason": text(512), "content": text(512)})


def parse_yield(request, raw):
    if not isinstance(raw, dict) or raw.get("kind") != "strategy_yield":
        return None
    from agent_core.output_contracts import decode_response
    shape = yield_schema(request)
    if shape is None:
        raise ValueError("d22_strategy_advice_unavailable")
    value, issues, terminal, _ = decode_response(raw, shape)
    if issues or terminal or any(not value[k].strip() or "\x00" in value[k] for k in ("reason", "content")):
        raise ValueError("d22_strategy_yield_invalid")
    return StrategyYield(value["review_id"], value["reason"], value["content"])


class StrategySupervision:
    def __init__(self, app):
        self.app = app
        self.pending = None
        self.retry = None
        self.polling = False

    @property
    def enabled(self):
        return self.app.config.intent_execution_policy == "continuous_v1"

    def live(self, identity):
        return self.pending is not None and self.pending[0].model_request_id == identity

    def calls(self, intent_id=None):
        for op in self.app.store.list_d4_operations(self.app.state.task_run.task_run_id):
            if op.start.category != "review":
                continue
            saved = self.app.handoff.evidence.read("request", op.start.operation_id)
            if saved and saved.get("stage") == "strategy_supervision" and (intent_id is None or saved["intent_id"] == intent_id):
                yield op, D8HandoffRequest.from_dict(saved)

    def watermark(self, intent_id):
        return [json.loads(r.prior_summary)["d8_progress"] for _, r in self.calls(intent_id)]

    def _log(self, event, request, **fields):
        from qinyuan_core import get_logger
        get_logger("ctrl").info(event, run=request.task_run_id, intent=request.intent_id,
                                request=request.model_request_id, **fields)

    def epoch(self, intent_id):
        return next((r.record_id for r in reversed(self.app.store.list_d8_tree_records(self.app.state.task_run.task_run_id))
                     if r.intent_id == intent_id and r.kind in {"selection", "resumed", "strategy_resumed"}), None)

    def observation_signatures(self, intent_id):
        signatures = {}
        for identity in self.app.sessions.execution_refs(intent_id):
            result = self.app.web.result(identity) if self.app.web else None
            if result:
                signatures[identity] = digest({k: result[k] for k in ("method", "status", "body_sha256", "body_bytes")})
            elif self.app.mcp is not None:
                result = self.app.mcp.feedback(identity)
                if result:
                    if result.get('http_response'):
                        signatures[identity] = digest({k: result['http_response'][k] for k in ('method','status','body_sha256','body_bytes')})
                    else:
                        signatures[identity] = digest({k: v for k, v in result.items() if k != "command"})
        return signatures

    def knowledge_version(self, intent_id):
        return digest(sorted(i.ref for i in self.app.handoff.history() if i.intent_id == intent_id
                             and i.record_type in {"verified_fact", "reviewed_inference"}))

    def exploration_progress(self, intent_id):
        """Deterministic completed-request coverage; never a semantic progress verdict."""
        app = self.app
        refs = app.sessions.execution_refs(intent_id)
        requests, responses, tools, experiments, recent = {}, {}, {}, set(), []
        http_count, failures = 0, []
        for identity in refs:
            result = app.web.result(identity) if app.web else None
            claim = None
            if result is not None:
                method, signature = result["method"], result["request_sha256"]
                tool, response, status = "http", result, "completed"
            elif app.mcp is not None and (result := app.mcp.feedback(identity)) is not None:
                claim = app.mcp.evidence.read("mcp-call", identity)
                tool = claim["call"]["tool_name"]
                response, status = result.get("http_response"), result["status"]
                method = response["method"] if response else None
                source = claim.get("experiment_request")
                signature = source["sha256"] if source else digest(claim["call"])
                if source:
                    experiments.add(claim["model_request_id"])
            else:
                # Attachment tools keep their existing trusted progress records.
                operation = app.store.load_d4_operation(identity)
                if operation.start.resource_ref.startswith("mcp/"):
                    raise D3RecoveryRequired("mcp.result_binding_changed")
                tool, response, status, signature, method = "attachment", None, operation.completion.result_kind, None, None
            tools[tool] = tools.get(tool, 0) + 1
            if signature is not None:
                requests.setdefault(tool + "/" + signature, []).append(identity)
            if response:
                http_count += 1
                key = digest({k: response[k] for k in ("status", "body_sha256", "body_bytes")})
                responses.setdefault(key, {"status": response["status"], "body_sha256": response["body_sha256"],
                    "body_bytes": response["body_bytes"], "operation_ids": []})["operation_ids"].append(identity)
            if status in {"failed", "rejected"}:
                failures.append(identity)
            recent.append({"operation_id": identity, "tool": tool, "method": method, "result": result})
        experiment_results = []
        for identity in sorted(experiments):
            saved = app.mcp.experiments.result(identity)
            experiment_results.append({"model_request_id": identity, "status": saved["status"] if saved else "not_recorded",
                "code": saved.get("code") if saved else None, "output": saved.get("output") if saved else None})
        return {"identity": "execution_coverage_not_information_gain_or_fact", "completed_tool_count": len(refs),
            "http_request_count": http_count, "experiment_session_count": len(experiments),
            "tool_counts": tools, "known_failed_or_rejected": failures, "experiment_results": experiment_results,
            "request_shape_groups": [{"signature": key, "operation_ids": ids} for key, ids in requests.items()],
            "response_groups": list(responses.values()), "recent_tools": recent[-4:],
            "remaining_seconds": max(0, app.state.task_run.deadline_at - app.clock())}

    def start(self, intent_id):
        self.poll()
        if self.pending or not self.enabled or self.stop(intent_id) is not None:
            return
        app = self.app
        app._check_role()
        intent = next(i for i in app.state.intents if i.intent_id == intent_id)
        selected, _, packet = app.handoff.select("execution", intent.turn_id, intent_id)
        history = list(selected)
        # Full current-Turn alternatives and recent observations are mandatory here.
        ids = {i.intent_id for i in app.state.intents if i.turn_id == intent.turn_id}
        recent = set(app.sessions.execution_refs(intent_id)[-4:])
        for item in app.handoff.history():
            if (item.record_type == "intent" and item.record_id in ids or item.record_id in recent) and item.ref not in {i.ref for i in history}:
                history.append(item)
        decisions = app.sessions.records(intent_id)
        last_ref = "decision/" + decisions[-1].record_id if decisions else app.sessions.execution_refs(intent_id)[-1]
        latest = next(i for i in app.handoff.history() if i.record_id == last_ref)
        history = [i for i in history if i.ref != latest.ref] + [latest]
        packet.update(turn_intents=[asdict(i) for i in app.state.intents if i.turn_id == intent.turn_id],
                      latest_decision_ref=latest.ref,
                      scope_ref=self.epoch(intent_id),
                      knowledge_version=self.knowledge_version(intent_id),
                      strategy_resolution_policy="review_latest_evidence",
                      knowledge_refs=sorted(i.ref for i in app.handoff.history() if i.intent_id == intent_id
                          and i.record_type in {"verified_fact", "reviewed_inference"}
                          and i.source_status == "available" and not i.conflict),
                      observation_signatures=self.observation_signatures(intent_id),
                      d8_progress=app.sessions.review_context(intent_id, compact=True),
                      exploration_progress=self.exploration_progress(intent_id),
                      constraint_version=max((r.constraint_version for r in app.store.list_d3_review_applications(app.state.task_run.task_run_id)), default=0))
        identity = app._id("supervision/" + intent_id + "/" + digest(packet["d8_progress"]))
        capacity = app.handoff.capacity()
        request = D8HandoffRequest(identity, app.state.task_run.task_run_id, intent.turn_id, intent_id,
            "strategy_supervision", app.config.outer_role.role_config_ref, app.state.latest_checkpoint.sequence,
            capacity.version, capacity.capacity, next(t.turn_target for t in app.state.turns if t.turn_id == intent.turn_id),
            tuple(history), handoff_json(packet))
        app.output_boundary.prepare(request)
        app.handoff.evidence.save("request", identity, asdict(request))
        app.store.begin_d4_operation(D4OperationStart(identity, request.task_run_id, "review", app.clock(),
            request.base_checkpoint_sequence, request.context_sha256, request.turn_id,
            app.config.d4_budget.budget_policy_ref, "outer_planning", request.role_config_ref))
        self._launch(request)

    def _launch(self, request):
        finished, stopped, values = threading.Event(), threading.Event(), []
        self.pending = request, finished, values, stopped
        timeout = min(180.0, self.app.state.task_run.deadline_at - self.app.clock())
        token = self.app._token
        class InvocationToken:
            @property
            def is_cancelled(self):
                return stopped.is_set() or token is not None and token.is_cancelled
        def compute():
            try:
                values.append((True, self.app.handoff.model.adapter.invoke(request, timeout, InvocationToken())))
            except Exception as exc:
                values.append((False, type(exc).__name__))
            finally:
                finished.set()
        threading.Thread(target=compute, daemon=True, name="qinyuan-strategy-review").start()
        self._log("strategy.started", request)

    def poll(self):
        if not self.polling and self.pending is None and self.retry is not None:
            if self.app.store.blocking_d4_operations(self.app.state.task_run.task_run_id) or self.app._dispatch_stopped():
                return
            retry = replace(self.retry, base_checkpoint_sequence=self.app.state.latest_checkpoint.sequence)
            self.app.output_boundary.prepare(retry)
            self.app.handoff.evidence.save("request", retry.model_request_id, asdict(retry))
            self.app.store.begin_d4_operation(D4OperationStart(retry.model_request_id, retry.task_run_id, "review", self.app.clock(),
                retry.base_checkpoint_sequence, retry.context_sha256, retry.turn_id,
                self.app.config.d4_budget.budget_policy_ref, "outer_planning", retry.role_config_ref))
            self.retry = None
            self._launch(retry)
        if self.polling or self.pending is None or not self.pending[1].is_set():
            return
        self.polling = True
        try:
            request, _, values = self.pending[:3]
            ok, raw = values[0]
            error_type = raw if not ok else None
            outcome, reason = "valid", None
            if not ok or isinstance(raw, ModelError):
                outcome, reason, raw = "model_error", "d22_strategy_provider_failed", None
            elif self.app.conclusions._sensitive(raw):
                outcome, reason, raw = "sensitive_rejected", "d22_strategy_sensitive_rejected", None
            else:
                try:
                    parse(request, raw)
                except (ValueError, TypeError, KeyError):
                    outcome, reason, raw = "invalid", "d22_strategy_format_rejected", None
            result = dict(model_request_id=request.model_request_id, request_sha256=request.context_sha256,
                          outcome=outcome, reason=reason, raw=raw, finished_at=self.app.clock(), rejected_candidate_indices=[])
            if error_type is not None:
                result["error_type"] = error_type
            self.app.handoff.evidence.save("result", request.model_request_id, result)
            self.app.handoff._known_call(request)
            self.pending = None
            if outcome != "valid" and request.correction_of_model_request_id is None and not self.app._dispatch_stopped():
                self.retry = replace(request, model_request_id=self.app._id("supervision/correction/" + request.model_request_id),
                                correction_of_model_request_id=request.model_request_id, rejection_code=reason)
                self.apply(request, result)
                return
            self.apply(request, result)
        finally:
            self.polling = False

    def validity(self, request, result):
        """Hard changes invalidate authority; appended evidence requests a current review."""
        app = self.app
        if app.state.task_run.status.value != "running":
            return "task_not_running"
        intent = next((i for i in app.state.intents if i.intent_id == request.intent_id), None)
        if intent is None or intent.status.value != "running":
            return "intent_not_running"
        frozen = json.loads(request.prior_summary)
        if self.epoch(request.intent_id) != frozen["scope_ref"]:
            return "selection_changed"
        current = {i.ref: i for i in app.handoff.history()}
        cited = set(result["raw"]["evidence_refs"]) | set((result["raw"].get("suggestion") or {}).get("evidence_refs", ()))
        if any(ref not in current or current[ref].source_status != "available" or current[ref].conflict for ref in cited):
            return "cited_source_unavailable"
        version = max((r.constraint_version for r in app.store.list_d3_review_applications(request.task_run_id)), default=0)
        if version != frozen["constraint_version"]:
            return "constraints_changed"
        if result["raw"]["decision"] == "continue_current_intent":
            return "current"
        observations = self.observation_signatures(request.intent_id)
        original = frozen["observation_signatures"]
        if frozen.get("strategy_resolution_policy") == "review_latest_evidence":
            if any(ref not in current or current[ref].source_status != "available" or current[ref].conflict
                   for ref in frozen["knowledge_refs"]):
                return "knowledge_invalidated"
            if any(observations.get(identity) != signature for identity, signature in original.items()):
                return "observation_changed"
            if (set(observations) - set(original)
                    or self.knowledge_version(request.intent_id) != frozen["knowledge_version"]):
                return "new_evidence"
        else:
            if self.knowledge_version(request.intent_id) != frozen["knowledge_version"]:
                return "legacy_knowledge_changed"
            if any(identity not in original and signature not in original.values() for identity, signature in observations.items()):
                return "legacy_observation_added"
        return "current"

    def applicable(self, request, result):
        return self.validity(request, result) in {"current", "new_evidence"}

    def apply(self, request, result):
        identity = request.model_request_id
        effect = self.app.handoff.evidence.read("strategy-effect", identity)
        created = effect is None
        if effect is None:
            reason = self.validity(request, result) if result["outcome"] == "valid" else result.get("reason")
            effect = {"request_sha256": request.context_sha256, "validity_reason": reason,
                      "outcome": "applied" if reason in {"current", "new_evidence"} else "stale" if result["outcome"] == "valid" else "unavailable"}
            self.app.handoff.evidence.save("strategy-effect", identity, effect)
        if result["outcome"] == "valid":
            self.app.store.mark_d4_operation_applied(identity, identity)
        if effect["request_sha256"] != request.context_sha256:
            raise D3RecoveryRequired("d22_strategy_effect_binding_changed")
        if created:
            self._log("strategy.finished", request, outcome=effect["outcome"], code=effect.get("validity_reason", result.get("reason")),
                      decision=(result.get("raw") or {}).get("decision"),
                      suggestion_intent=((result.get("raw") or {}).get("suggestion") or {}).get("intent_id"),
                      category="technical_failure" if effect["outcome"] == "unavailable" else "strategy")

    def reconcile(self, request):
        if self.live(request.model_request_id):
            return
        if self.app.handoff.evidence.read("result", request.model_request_id) is None:
            # The orphan is a read-only supervisor, never an unknown tool outcome.
            self.app.handoff.evidence.save("result", request.model_request_id,
                dict(model_request_id=request.model_request_id, request_sha256=request.context_sha256,
                     outcome="model_error", raw=None, reason="d22_strategy_worker_lost", finished_at=self.app.clock(), rejected_candidate_indices=[]))
        result = self.app.handoff._known_call(request)
        self.apply(request, result)

    def retire(self):
        """End the local read-only attempt, without claiming the provider replied."""
        self.retry = None
        if self.pending is None:
            return
        self.pending[3].set()
        request = self.pending[0]
        self.pending = None
        result = dict(model_request_id=request.model_request_id, request_sha256=request.context_sha256,
                      outcome="model_error", raw=None, reason="d22_strategy_local_attempt_retired",
                      finished_at=self.app.clock(), rejected_candidate_indices=[])
        self.app.handoff.evidence.save("result", request.model_request_id, result)
        self.app.handoff._known_call(request)
        self.apply(request, result)

    def stop(self, intent_id):
        for op, request in reversed(list(self.calls(intent_id))):
            effect = self.app.handoff.evidence.read("strategy-effect", request.model_request_id)
            result = self.app.handoff.evidence.read("result", request.model_request_id)
            if not effect or effect["outcome"] != "applied" or not result or result["raw"]["decision"] != "request_intent_resolution":
                continue
            if not self.applicable(request, result):
                continue
            for row in self.app.store.list_d8_tree_records(request.task_run_id):
                if row.intent_id == intent_id and row.inner_request_id:
                    saved = self.app.sessions.evidence.read("request", row.inner_request_id)
                    context = json.loads(saved["request"]["history_context"]) if saved else {}
                    if (context.get("intent_execution", {}).get("stop_requested") or {}).get("review_id") == request.model_request_id:
                        return None
                    # A retained end proposal can predate the stop. Its committed
                    # outer review still consumes this exact resolution request.
                    evaluated = self.app.handoff.evidence.read("request", row.model_request_id)
                    policy = json.loads(evaluated["prior_summary"]).get("s4_evaluation_policy", {}) if evaluated else {}
                    if policy.get("strategy_review", {}).get("review_id") == request.model_request_id:
                        return None
            stop = {"review_id": request.model_request_id, "decision": "request_intent_resolution", "reason": result["raw"]["reason"]}
            frozen = json.loads(request.prior_summary)
            if frozen.get("strategy_resolution_policy") == "review_latest_evidence":
                added = set(self.observation_signatures(intent_id)) - set(frozen["observation_signatures"])
                history = self.app.handoff.history()
                stop.update(review_latest_evidence=True, evidence_refs=result["raw"]["evidence_refs"],
                    new_evidence_refs=[i.ref for i in history if i.record_id in added
                        or i.intent_id == intent_id and i.record_type in {"verified_fact", "reviewed_inference"}
                        and i.ref not in frozen["knowledge_refs"] and i.source_status == "available" and not i.conflict])
            return stop
        return None

    def advice(self, intent_id):
        app = self.app
        for op, request in reversed(list(self.calls(intent_id))):
            effect = app.handoff.evidence.read("strategy-effect", request.model_request_id)
            result = app.handoff.evidence.read("result", request.model_request_id)
            suggestion = (result or {}).get("raw") or {}
            suggestion = suggestion.get("suggestion")
            if not effect or effect["outcome"] != "applied" or not suggestion:
                continue
            target = next((i for i in app.state.intents if i.intent_id == suggestion["intent_id"] and i.turn_id == request.turn_id and i.status.value == "pending"), None)
            if target is None:
                continue
            # Once delivered, the frozen session retains it; never nag on each call.
            for row in app.sessions.records(intent_id):
                frozen = app.sessions.evidence.read("request", row.model_request_id)
                prior = json.loads(frozen["request"].get("history_context", "{}")) if frozen else {}
                old = prior.get("strategy_advice")
                if old and old["intent_id"] == target.intent_id and old["evidence_refs"] == suggestion["evidence_refs"]:
                    return {"review_id": request.model_request_id, "intent_id": target.intent_id,
                            "evidence_refs": suggestion["evidence_refs"],
                            "identity": "previously_delivered_optional_choice_reason_in_session_history"}
            return {**suggestion, "review_id": request.model_request_id, "description": target.description,
                    "identity": "optional_strategy_suggestion_not_instruction_or_fact"}
        return None

    def yield_intent(self, request_id, proposal):
        app = self.app
        saved = app.sessions.evidence.read("request", request_id)
        advice = json.loads(saved["request"]["history_context"])["strategy_advice"]
        target = next((i for i in app.state.intents if i.intent_id == advice["intent_id"] and i.turn_id == saved["turn_id"] and i.status.value == "pending"), None)
        if target is None:
            raise ValueError("d22_strategy_target_unavailable")
        from agent_core.control_store import UpsertIntent
        from agent_core.domain import IntentStatus
        current = next(i for i in app.state.intents if i.intent_id == saved["intent_id"])
        record = D8TreeRecord(app._id("strategy-yield/" + request_id), app.state.task_run.task_run_id,
            current.turn_id, current.intent_id, "strategy_yielded", request_id)
        app.handoff.evidence.save("strategy-yield", record.record_id,
            {"target_intent_id": target.intent_id, "review_id": proposal.review_id,
             "reason": proposal.reason, "content": proposal.content, "identity": "strategy_yield_not_technical_failure"})
        app.handoff._commit("yield_d8_strategy", record, (
            UpsertIntent(current.intent_id, current.turn_id, current.description, current.priority, current.intent_type, IntentStatus.SUSPENDED),
            UpsertIntent(target.intent_id, target.turn_id, target.description, target.priority, target.intent_type, IntentStatus.RUNNING)))
        app.store.mark_d3_operation_applied(request_id, record.record_id)

    def guard_decision(self, request, identity):
        prior = self.app.handoff.evidence.read("strategy-discard", identity)
        if prior is not None:
            return prior.get("disposition") != "retained_for_processing"
        context = json.loads(getattr(request, "history_context", "{}") or "{}")
        intent_id = (self.app.sessions.evidence.read("request", identity) or {}).get("intent_id")
        stop = self.stop(intent_id) if intent_id else None
        if not stop or context.get("intent_execution", {}).get("stop_requested"):
            return False
        model = self.app.model_evidence.read(identity)
        raw = model.raw
        kind = raw.get("kind") or raw.get("action", {}).get("kind") or "decision"
        if kind == "http_request_saved":kind = "http_request"
        retained = kind in {"fact_candidate", "working_record", "intent_resolution"}
        review = self.app.handoff.evidence.read("result", stop["review_id"])
        content = raw.get("content") if retained else None
        if not content:
            content = (raw["reason"] if kind == "intent_resolution" else "Unexecuted MCP proposal: " + raw["tool_name"] if kind == "mcp_call"
                       else "Preserved proposal: " + kind + "; see model_request_id for the original decision.")
        note = {"identity": "known_decision_retained_at_strategy_stop" if retained else "known_decision_superseded_by_strategy_stop",
            "intent_id": intent_id, "model_request_id": identity, "review_id": stop["review_id"],
            "kind": kind, "disposition": "retained_for_processing" if retained else "not_dispatched",
            "content": self.app.redactor.redact(content), "reason": self.app.redactor.redact(stop["reason"]),
            "evidence_refs": review["raw"]["evidence_refs"],
            "proposal_sha256": digest(asdict(model)), "review_sha256": digest(review)}
        self.app.sessions._safe(note)
        self.app.handoff.evidence.save("strategy-discard", identity, note)
        if not retained:
            self.app.store.mark_d3_operation_applied(identity, identity)
        from qinyuan_core import get_logger
        get_logger("ctrl").info("strategy.interruption_recorded", run=request.task_run_id,
            intent=intent_id, request=identity, review=stop["review_id"], kind=kind, disposition=note["disposition"])
        return not retained

    def interruptions(self):
        """Known proposal notes are reasoning context, never execution evidence."""
        for call in self.app.store.list_d3_operations(self.app.state.task_run.task_run_id):
            if call.start.category != "inner_model" or call.completion is None:
                continue
            identity = call.start.operation_id
            note = self.app.handoff.evidence.read("strategy-discard", identity)
            if note is None:
                continue
            snapshot = self.app.sessions.evidence.read("request", identity)
            if snapshot is None or snapshot["intent_id"] != note["intent_id"]:
                raise D3RecoveryRequired("d22_strategy_interruption_owner_changed")
            if "proposal_sha256" in note:
                model = self.app.model_evidence.read(identity)
                review = self.app.handoff.evidence.read("result", note["review_id"])
                if (model is None or digest(asdict(model)) != note["proposal_sha256"]
                        or review is None or digest(review) != note["review_sha256"]):
                    raise D3RecoveryRequired("d22_strategy_interruption_source_changed")
            yield identity, snapshot["turn_id"], note

    def resume_candidate(self, turn_id):
        app = self.app
        rows = app.store.list_d8_tree_records(app.state.task_run.task_run_id)
        resumed = {r.model_request_id for r in rows if r.kind == "strategy_resumed"}
        for row in reversed(rows):
            if row.kind != "strategy_yielded" or row.turn_id != turn_id or row.model_request_id in resumed:
                continue
            owner = next(i for i in app.state.intents if i.intent_id == row.intent_id)
            if owner.status.value != "suspended":
                continue
            note = app.handoff.evidence.read("strategy-yield", row.record_id)
            target = next(i for i in app.state.intents if i.intent_id == note["target_intent_id"])
            if target.status.value in {"pending", "running"}:
                continue
            if target.status.value == "succeeded" and not any(r.intent_id == target.intent_id and r.kind == "expansion_exhausted" for r in rows) and not any(t.triggering_intent_id == target.intent_id for t in app.state.turns):
                continue
            return row
        return None

    def resume(self, row):
        from agent_core.control_store import UpsertIntent
        from agent_core.domain import IntentStatus
        app = self.app
        intent = next(i for i in app.state.intents if i.intent_id == row.intent_id)
        record = D8TreeRecord(app._id("strategy-resume/" + row.record_id), row.task_run_id, row.turn_id,
                              row.intent_id, "strategy_resumed", row.model_request_id)
        app.handoff._commit("resume_d8_strategy", record, (UpsertIntent(intent.intent_id, intent.turn_id,
            intent.description, intent.priority, intent.intent_type, IntentStatus.RUNNING),))


def validate_transition(store, mutation, state):
    from agent_core.control_store import StorageError, UpsertIntent
    from agent_core.domain import IntentStatus, ExecutionStatus
    record = next(r for r in mutation.state_changes if isinstance(r, D8TreeRecord))
    changes = [c for c in mutation.state_changes if isinstance(c, UpsertIntent)]
    old = next((i for i in state.intents if i.intent_id == record.intent_id), None)
    call = store.load_d3_operation(record.model_request_id)
    if (old is None or record.turn_id != old.turn_id or record.support_fact_id is not None
            or call is None or call.completion is None or call.start.task_run_id != mutation.task_run_id
            or call.start.role != "inner_action" or call.start.category != "inner_model"
            or any(e.status is ExecutionStatus.RUNNING for e in state.executions)):
        raise StorageError("d22_strategy_transition_scope_invalid")
    by_id = {i.intent_id: i for i in changes}
    changed = by_id.get(old.intent_id)
    if changed is None or (changed.description, changed.priority, changed.intent_type) != (old.description, old.priority, old.intent_type):
        raise StorageError("d22_strategy_target_rewritten")
    if record.kind == "strategy_yielded":
        targets = [i for i in changes if i.intent_id != old.intent_id]
        target = next((i for i in state.intents if targets and i.intent_id == targets[0].intent_id), None)
        if (mutation.operation != "yield_d8_strategy" or len(changes) != 2 or len(mutation.state_changes) != 3
                or old.status is not IntentStatus.RUNNING or changed.status is not IntentStatus.SUSPENDED
                or call.application_ref is not None or call.completion.result_kind != "completed"
                or target is None or target.turn_id != old.turn_id or target.status is not IntentStatus.PENDING
                or targets[0].status is not IntentStatus.RUNNING
                or (targets[0].description, targets[0].priority, targets[0].intent_type) != (target.description, target.priority, target.intent_type)):
            raise StorageError("d22_strategy_yield_invalid")
    elif (record.kind != "strategy_resumed" or mutation.operation != "resume_d8_strategy" or len(changes) != 1
            or len(mutation.state_changes) != 2 or old.status is not IntentStatus.SUSPENDED
            or changed.status is not IntentStatus.RUNNING or call.application_ref is None
            or any(i.status is IntentStatus.RUNNING for i in state.intents if i.turn_id == old.turn_id)):
        raise StorageError("d22_strategy_resume_invalid")
    else:
        originals = [r for r in store.list_d8_tree_records(mutation.task_run_id)
                     if r.intent_id == record.intent_id and r.model_request_id == record.model_request_id]
        if not any(r.kind == "strategy_yielded" and r.record_id == call.application_ref for r in originals) or any(r.kind == "strategy_resumed" for r in originals):
            raise StorageError("d22_strategy_resume_lineage_invalid")


def validate_yield_note(app, record, mutation):
    from agent_core.control_store import UpsertIntent
    proposal = app.validator._known_model(record.task_run_id, record.model_request_id, "inner_plan")
    frozen = app.sessions.evidence.read("request", record.model_request_id)
    advice = json.loads(frozen["request"]["history_context"])["strategy_advice"]
    note = app.handoff.evidence.read("strategy-yield", record.record_id)
    target = next(c for c in mutation.state_changes if isinstance(c, UpsertIntent) and c.intent_id != record.intent_id)
    if (not isinstance(proposal, StrategyYield) or advice["review_id"] != proposal.review_id
            or advice["intent_id"] != target.intent_id
            or note != {"target_intent_id": target.intent_id, "review_id": proposal.review_id,
                        "reason": proposal.reason, "content": proposal.content, "identity": "strategy_yield_not_technical_failure"}):
        raise D3RecoveryRequired("d22_strategy_yield_evidence_changed")
