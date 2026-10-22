"""Concurrency and voluntary scheduling over real local state, with fake I/O."""
import asyncio
import json
import threading

import pytest
from test_http import http_case
from test_exploration_loop import install


def continue_review(request, **fields):
    return dict(decision="continue_current_intent", reason="The experiment still separates hypotheses.",
                evidence_refs=[request.history[-1].ref], suggestion=None, **fields)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_inner_advances_while_supervisor_waits_and_only_one_worker_is_started(http_case):
    c = http_case
    install(c, steps=6)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    inner_advanced = threading.Event()
    checkpoints = []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            checkpoints.append(envelope.request.base_checkpoint_sequence)
            assert inner_advanced.wait(3), "Inner waited for background review"
            return continue_review(envelope.request)
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) >= 4:
            inner_advanced.set()
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert inner_advanced.is_set() and len(checkpoints) == 1 and len(c.sends) == 6
    assert not c.app.store.list_d4_unresolved_operations(c.task)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_monitor_format_failure_has_one_correction_and_does_not_stop_tools(http_case):
    c = http_case
    install(c, steps=6)
    outer = c.models[0].invoke
    calls = []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            calls.append(envelope.request)
            return {"unknown_field": "not a valid review"}
        return outer(envelope, *args)
    c.models[0].invoke = review
    result = asyncio.run(c.app._run_once(c.token))
    c.app.supervision.poll()
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 6 and len(calls) == 2
    assert calls[1].correction_of_model_request_id == calls[0].model_request_id
    assert not c.app.supervision.stop(c.app.state.intents[0].intent_id)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_valid_stop_discards_inflight_decision_without_dispatch_or_technical_retry(http_case, monkeypatch):
    c = http_case
    install(c, steps=5, stop="inconclusive")
    outer, inner = c.models[0].invoke, c.models[1].invoke
    stop_applied = threading.Event()
    apply = c.app.supervision.apply
    def apply_result(request, result):
        apply(request, result)
        if result["outcome"] == "valid":
            stop_applied.set()
    c.app.supervision.apply = apply_result
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            return dict(decision="request_intent_resolution", reason="Repeated checks no longer separate the scoped hypotheses.",
                        evidence_refs=[envelope.request.history[-1].ref], suggestion=None)
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) >= 4:
            packet = json.loads(envelope.request.history_context)
            if packet["intent_execution"].get("stop_requested"):
                return dict(kind="intent_resolution", outcome="inconclusive", reason="Close this tested scope with uncertainty.",
                    evidence_refs=[i["ref"] for i in packet["items"]], fact_ids=[], next_intent_candidates=[])
            assert stop_applied.wait(3)
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert len(c.sends) == 4
    rows = c.app.store.list_d8_tree_records(c.task)
    assert not any(r.kind in {"suspended", "resumed"} for r in rows)
    assert c.app.state.intents[0].status.value == "inconclusive"
    notes = list(c.app.supervision.interruptions())
    assert len(notes) == 1
    identity, _, note = notes[0]
    assert note["kind"] == "http_request" and note["disposition"] == "not_dispatched"
    assert note["review_id"] and note["evidence_refs"] and note["reason"]
    assert "headers" not in note and "body" not in note
    assert c.app.model_evidence.read(identity).raw["kind"] == "http_request_saved"
    assert not any(r.model_request_id == identity for r in c.app.sessions.records())
    history = next(i for i in c.app.handoff.history() if i.record_id == "interruption/" + identity)
    assert history.identity == "strategy_interruption_not_execution_or_fact"
    assert history.record_type == "progress"
    from dataclasses import replace
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    read = c.app.model_evidence.read
    monkeypatch.setattr(c.app.model_evidence, "read", lambda key: replace(read(key), reason="changed") if key == identity else read(key))
    with pytest.raises(D3RecoveryRequired, match="d22_strategy_interruption_source_changed"):
        list(c.app.supervision.interruptions())


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("kind, decision", [("fact_candidate", "accept"), ("fact_candidate", "reject"),
    ("working_record", "accept"), ("intent_resolution", "accept")])
def test_stop_retains_completed_knowledge_for_original_review(http_case, kind, decision):
    c = http_case
    install(c, steps=4, stop="inconclusive", decision=decision)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    started, stopped = threading.Event(), threading.Event()
    retained, later, evaluated_positions = [], [], []
    apply = c.app.supervision.apply
    def applied(request, result):
        apply(request, result)
        if result["outcome"] == "valid":stopped.set()
    c.app.supervision.apply = applied
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            assert started.wait(3)
            return dict(decision="request_intent_resolution", reason="Review scope before further execution.",
                        evidence_refs=[envelope.request.history[-1].ref], suggestion=None)
        if kind == "intent_resolution" and envelope.output_contract["stage"] == "intent_evaluation":
            policy = json.loads(envelope.request.prior_summary)["s4_evaluation_policy"]
            evaluated_positions.append(policy)
            if len(evaluated_positions) == 1:
                return dict(decision="continue", reason="The retained independent position leaves a useful check.",
                    evidence_refs=[i.ref for i in envelope.request.history])
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) >= 4:
            context = json.loads(envelope.request.history_context)
            if not retained:
                retained.append(envelope.request.model_request_id)
                started.set()
                assert stopped.wait(3)
                refs = [s["ref"] for s in context["evidence_sources"]]
                if kind == "fact_candidate":
                    return dict(kind=kind, content="Recorded observations support this scoped conclusion.",
                        reason="Judge the saved evidence, not the interruption.", evidence_refs=refs, inferred=False)
                if kind == "working_record":
                    return dict(kind=kind, content="Keep this intermediate observation, not a verified Fact.", evidence_refs=[context["items"][-1]["ref"]])
            else:
                later.append(context)
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert len(c.sends) == 4
    identity, _, note = next(c.app.supervision.interruptions())
    assert identity == retained[0] and note["disposition"] == "retained_for_processing"
    assert any(r.model_request_id == identity for r in c.app.sessions.records())
    assert len(c.app.state.facts) == (1 if kind == "fact_candidate" and decision == "accept" else 0)
    if kind == "intent_resolution":
        assert evaluated_positions[0]["strategy_review"]["review_latest_evidence"]
        assert evaluated_positions[0]["inner_request_id"] == retained[0]
        assert sum(bool(p.get("strategy_review")) for p in evaluated_positions) == 1
        assert not later[0]["intent_execution"].get("stop_requested")
    if later:
        assert any(i["identity"] == "strategy_interruption_not_execution_or_fact" for i in later[0]["items"])
    # Reopening uses the same source identities; it must not replay tools or reviews.
    reopened = c.create()
    try:
        reopened.controller.attach(c.task)
        assert list(reopened.supervision.interruptions()) == list(c.app.supervision.interruptions())
        before = len(reopened.store.list_d4_operations(c.task))
        reopened.sessions.reconcile()
        assert len(reopened.store.list_d4_operations(c.task)) == before
    finally:
        reopened.store.close()


def test_mcp_interruption_records_tool_and_sources_without_arguments(tmp_path):
    from dataclasses import dataclass
    from types import SimpleNamespace as NS
    from uuid import uuid4
    from agent_core.strategy_supervision import StrategySupervision
    from agent_adapters.handoff_evidence import FileD8HandoffEvidence
    identity, intent, review = (str(uuid4()) for _ in range(3))
    evidence = FileD8HandoffEvidence(tmp_path)
    evidence.save("result", review, {"raw": {"evidence_refs": ["source"]}})
    @dataclass
    class Proposal:
        raw: dict
    model = Proposal({"kind": "mcp_call", "tool_name": "http.run_experiment", "arguments": {"script": "PRIVATE_ARGUMENT"}})
    applied = []
    app = NS(handoff=NS(evidence=evidence), model_evidence=NS(read=lambda _: model),
        sessions=NS(evidence=NS(read=lambda *args: {"intent_id": intent}), _safe=lambda _: None),
        redactor=NS(redact=lambda value: value), store=NS(mark_d3_operation_applied=lambda *args: applied.append(args)))
    supervisor = StrategySupervision(app)
    supervisor.stop = lambda _: {"review_id": review, "reason": "Reassess the bounded experiment."}
    request = NS(history_context="{}", task_run_id=str(uuid4()))
    assert supervisor.guard_decision(request, identity)
    note = evidence.read("strategy-discard", identity)
    assert note["kind"] == "mcp_call" and "http.run_experiment" in note["content"]
    assert note["model_request_id"] == identity and note["review_id"] == review
    assert "PRIVATE_ARGUMENT" not in json.dumps(note) and "arguments" not in note
    assert supervisor.guard_decision(request, identity) and len(applied) == 1


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_segments_append_history_and_dynamic_catalog_follows_it(http_case):
    from agent_adapters.deepseek_roles import build_payload
    c = http_case
    install(c, steps=5)
    asyncio.run(c.app._run_once(c.token))
    envelopes = [e for e in c.exploration_envelopes if e.output_contract["stage"] == "inner_plan"]
    payloads = [build_payload(e, c.app.config.inner_role, context_loader=c.app.web.model_context) for e in envelopes]
    assert len({p["messages"][0]["content"] for p in payloads}) == 1
    previous = []
    previous_wire = None
    for payload in payloads:
        messages = payload["messages"]
        history = json.loads(messages[1]["content"])
        if previous_wire is not None:
            assert messages[1]["content"].startswith(previous_wire[:-2])
        previous_wire = messages[1]["content"]
        assert history["segment_start"] == 1
        assert history["messages"][:len(previous)] == previous
        previous = history["messages"]
        assert messages[2]["role"] == "system" and "output_contract=" in messages[2]["content"]
        assert "history_context" in json.loads(messages[3]["content"])
    assert len(previous) > 12, "a sliding last-four window lost the stable history prefix"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("restart", [False, True])
def test_voluntary_yield_restores_same_session_without_technical_revisit(http_case, restart):
    c = http_case
    install(c, steps=7, stop="inconclusive")
    outer = c.models[0].invoke
    chosen, sessions, yielded, requests, deferred = [], {}, [], {}, []
    snapshots = []
    yield_requests = []
    yield_intent = c.app.supervision.yield_intent
    def preserve_and_project(*args):
        yield_intent(*args)
        from agent_adapters.web.task_result import snapshot_v3_task_result
        snapshots.append(snapshot_v3_task_result(c.directory, c.declaration))
        if restart:
            from test_chain_multi_turn import ProcessLoss
            raise ProcessLoss("process lost after durable strategy yield")
    c.app.supervision.yield_intent = preserve_and_project
    def review(envelope, *args):
        request, stage = envelope.request, envelope.output_contract["stage"]
        if stage == "tree_planning" and not c.app.state.turns:
            return dict(kind="candidates", intents=[
                dict(description="Test the first independent hypothesis.", intent_type=None, dependency_refs=[]),
                dict(description="Test the alternative entry hypothesis.", intent_type=None, dependency_refs=[])])
        if stage == "strategy_supervision":
            packet = json.loads(request.prior_summary)
            assert len(packet["turn_intents"]) == 2
            target = next(i for i in packet["turn_intents"] if i["status"] == "pending")
            refs = [request.history[-1].ref]
            return dict(decision="continue_current_intent", reason="Both directions remain feasible.", evidence_refs=refs,
                suggestion=dict(intent_id=target["intent_id"], reason="The alternative has a newly testable prerequisite.",
                    uncertainty="Its result is not yet observed.", current_direction_value="One discriminating check remains here.", evidence_refs=refs))
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] != "inner_plan":
            raise AssertionError(envelope.output_contract["stage"])
        request = envelope.request
        packet = json.loads(request.history_context)
        identity = request.context.intent_id
        sessions.setdefault(identity, set()).add(request.session_id)
        chosen.append(identity)
        advice = packet.get("strategy_advice")
        if advice and not yielded:
            if "reason" in advice and not deferred:
                deferred.append(advice["review_id"])
                return dict(kind="working_record", content="First compare the latest result, then reconsider the optional alternative.",
                            evidence_refs=[packet["items"][0]["ref"]])
            yielded.append(identity)
            yield_requests.append(request.model_request_id)
            return dict(kind="strategy_yield", review_id=advice["review_id"],
                reason="Voluntarily inspect the now-testable alternative first.",
                content="Original hypothesis remains open; four checks saved; next compare the response condition.")
        own_executions = requests.get(identity, 0)
        if (not yielded and own_executions < 4) or identity != (yielded or [None])[0] and not own_executions:
            requests[identity] = own_executions + 1
            return dict(kind="http_request", method="GET", path_query="/?branch="+str(len(chosen)), headers={}, body="")
        # Let a just-started background review finish while this model computes.
        if not yielded:
            return dict(kind="working_record", content="Current hypothesis remains open pending a comparison.",
                        evidence_refs=[packet["items"][0]["ref"]])
        return dict(kind="intent_resolution", outcome="inconclusive", reason="This bounded branch remains unresolved.",
            evidence_refs=[i["ref"] for i in packet["items"]], fact_ids=[], next_intent_candidates=[])
    c.models[0].invoke, c.models[1].invoke = review, choose
    if restart:
        from test_chain_multi_turn import ProcessLoss
        with pytest.raises(ProcessLoss):
            asyncio.run(c.app._run_once(c.token))
        c.app.store.close()
        c.app = c.create()
        async def finish_wait(token):
            c.clock.now = c.app.state.task_run.deadline_at
        c.app._wait_continuation = finish_wait
        result = asyncio.run(c.app.resume(c.task, c.token))
    else:
        result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    rows = c.app.store.list_d8_tree_records(c.task)
    assert sum(r.kind == "strategy_yielded" for r in rows) == 1, [
        (key, c.app.model_evidence.read_rejection(key), [(r.kind, c.app.handoff.evidence.read("pause", r.record_id))
         for r in rows if r.kind == "suspended"]) for key in yield_requests]
    assert sum(r.kind == "strategy_resumed" for r in rows) == 1
    assert not any(r.kind in {"suspended", "resumed"} for r in rows)
    assert all(len(ids) == 1 for ids in sessions.values())
    assert chosen[-1] == yielded[0]
    assert len(deferred) == 1
    from scripts.analyze_run_timing import analyze
    metrics = analyze(c.directory, c.task)["supervision"]
    assert metrics["counts"]["voluntary_yields"] == 1
    assert metrics["counts"]["advice_delivered"] >= 1
    assert not snapshots[0]["errors"], snapshots[0]
    parked = next(i for t in snapshots[0]["turns"] for i in t["intents"] if i["intent_id"] == yielded[0])
    assert parked["status"] == "suspended" and parked["suspension_kind"] == "strategy"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_optional_advice_can_be_ignored_without_forcing_a_switch(http_case):
    c = http_case
    install(c, steps=6)
    outer = c.models[0].invoke
    def review(envelope, *args):
        request, stage = envelope.request, envelope.output_contract["stage"]
        if stage == "tree_planning" and not c.app.state.turns:
            return dict(kind="candidates", intents=[
                dict(description="Test the current scoped hypothesis.", intent_type=None, dependency_refs=[]),
                dict(description="Inspect an independent alternative.", intent_type=None, dependency_refs=[])])
        if stage == "strategy_supervision":
            target = next(i for i in json.loads(request.prior_summary)["turn_intents"] if i["status"] == "pending")
            refs = [request.history[-1].ref]
            return dict(decision="continue_current_intent", reason="Current work remains meaningful.", evidence_refs=refs,
                suggestion=dict(intent_id=target["intent_id"], reason="The alternative may be cheaper.",
                    uncertainty="Its prerequisite remains uncertain.", current_direction_value="Current experiments are bounded.", evidence_refs=refs))
        return outer(envelope, *args)
    c.models[0].invoke = review
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 6
    assert not any(r.kind == "strategy_yielded" for r in c.app.store.list_d8_tree_records(c.task))
    assert [i.status.value for i in c.app.state.intents] == ["succeeded", "pending"]
    delivered = [json.loads(e.request.history_context).get("strategy_advice") for e in c.exploration_envelopes
                 if e.output_contract["stage"] == "inner_plan"]
    assert sum(bool(a and "reason" in a) for a in delivered) == 1
    from scripts.analyze_run_timing import analyze
    metrics = analyze(c.directory, c.task)["supervision"]
    assert metrics["counts"]["advice_delivered"] == 1
    assert metrics["counts"]["voluntary_yields"] == 0
    assert "useless" not in metrics["counts"]


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("continue_review,restart", [(False, False), (True, False), (True, True)])
def test_added_evidence_gets_one_current_review_and_inner_can_disagree(http_case, continue_review, restart):
    from dataclasses import replace
    from agent_adapters.web.http_adapter import D7HttpWireResponse
    from agent_core.output_contracts import output_schema, decode_response
    from agent_core.handoff_contracts import handoff_json
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    install(c, steps=6, stop=None if continue_review else "inconclusive")
    fifth = threading.Event()
    outer, inner = c.models[0].invoke, c.models[1].invoke
    ended, positions, legacy, continued = [], [], [], []
    class Transport:
        def send(self, **kwargs):
            c.sends.append(kwargs)
            if len(c.sends) == 5:
                fifth.set()
            return D7HttpWireResponse(200, (("Content-Type", "text/plain"),),
                b"new discriminating response" if len(c.sends) >= 5 else b"same old response")
    c.app.web.transport_factory = lambda _: Transport()
    async def finish_wait(token):
        c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish_wait
    apply = c.app.supervision.apply
    def apply_review(request, result):
        if result["outcome"] == "valid" and result["raw"]["decision"] == "request_intent_resolution":
            old = json.loads(request.prior_summary)
            old.pop("strategy_resolution_policy")
            previous = replace(request, prior_summary=handoff_json(old))
            legacy.append(c.app.supervision.validity(previous, result))
        return apply(request, result)
    c.app.supervision.apply = apply_review
    def review(envelope, *args):
        stage, request = envelope.output_contract["stage"], envelope.request
        if stage == "strategy_supervision":
            assert fifth.wait(8)
            return dict(decision="request_intent_resolution", reason="The frozen observations repeat.",
                        evidence_refs=[request.history[-1].ref], suggestion=None)
        if stage == "intent_evaluation":
            policy = json.loads(request.prior_summary)["s4_evaluation_policy"]
            if policy.get("strategy_review"):
                ended.append(policy)
                assert policy["strategy_review"]["new_evidence_refs"]
                assert set(policy["strategy_review"]["new_evidence_refs"]) <= {i.ref for i in request.history}
                assert set(policy["strategy_review"]["new_evidence_refs"]) <= set(policy["required_observation_refs"])
                return dict(decision="continue" if continue_review else "inconclusive",
                    reason="The new control justifies one discriminating comparison." if continue_review else "Close only this tested scope.",
                    evidence_refs=[i.ref for i in request.history])
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) >= 5:
            if c.app.supervision.pending:
                assert c.app.supervision.pending[1].wait(3)
                c.app.supervision.poll()
            context = json.loads(envelope.request.history_context)
            stop = context["intent_execution"].get("stop_requested")
            if stop and stop.get("review_latest_evidence"):
                positions.append(context)
                assert set(stop["new_evidence_refs"]) <= {i["ref"] for i in context["items"]}
                proposal = dict(kind="intent_resolution", outcome="review" if continue_review else "inconclusive",
                    reason="I disagree: the new control enables a discriminating experiment." if continue_review else "The observed scope supports closure.",
                    evidence_refs=[i["ref"] for i in context["items"]], fact_ids=[], next_intent_candidates=[])
                assert not decode_response(proposal, output_schema(envelope.request, contract_version=22)[1])[1]
                # A previously frozen stop did not permit an independent review position.
                if continue_review:
                    old = json.loads(envelope.request.history_context)
                    old["intent_execution"]["stop_requested"].pop("review_latest_evidence")
                    assert decode_response(proposal, output_schema(replace(envelope.request, history_context=handoff_json(old)), contract_version=22)[1])[1]
                # A second app sees the persisted pending review without dispatching anything.
                loaders = [m.context_loader for m in c.models]
                reopened = c.create()
                try:
                    reopened.controller.attach(c.task)
                    assert reopened.supervision.stop(request_intent := envelope.request.context.intent_id)["review_id"] == stop["review_id"]
                    assert reopened.supervision.stop(request_intent)["review_latest_evidence"]
                finally:
                    reopened.store.close()
                    # The fixture shares fake model objects across app instances.
                    for model, loader in zip(c.models, loaders):
                        model.context_loader = loader
                return proposal
        return inner(envelope, *args)
    commit = c.app.handoff._commit
    def commit_review(operation, record, *args, **kwargs):
        value = commit(operation, record, *args, **kwargs)
        if operation == "evaluate_d8_tree_intent" and record.kind == "evaluation_continued":
            continued.append(record)
            if restart:
                raise ProcessLoss("lost owner after committing current-evidence continue")
        return value
    c.models[0].invoke, c.models[1].invoke = review, choose
    c.app.handoff._commit = commit_review
    if restart:
        with pytest.raises(ProcessLoss):
            asyncio.run(c.app._run_once(c.token))
        c.app.store.close()
        c.app = c.create()
        c.app._wait_continuation = finish_wait
        result = asyncio.run(c.app.resume(c.task, c.token))
    else:
        result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert len(positions) == len(ended) == 1
    assert legacy == ["legacy_observation_added"]
    requests = list(c.app.supervision.calls())
    assert len(requests) == 1
    effect = c.app.handoff.evidence.read("strategy-effect", requests[0][1].model_request_id)
    assert effect["outcome"] == "applied" and effect["validity_reason"] == "new_evidence"
    assert len(c.sends) == (6 if continue_review else 5)
    assert c.app.supervision.stop(requests[0][1].intent_id) is None
    assert len(continued) == int(continue_review)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_lost_readonly_worker_is_classified_without_replaying_it(http_case):
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    install(c, steps=5)
    started, release = threading.Event(), threading.Event()
    outer, start = c.models[0].invoke, c.app.supervision.start
    calls = []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            calls.append(envelope.request)
            started.set()
            assert release.wait(8)
            return continue_review(envelope.request)
        return outer(envelope, *args)
    def lose_owner(intent):
        start(intent)
        assert started.wait(3)
        raise ProcessLoss("simulate losing the supervisor owner before any next dispatch")
    c.models[0].invoke, c.app.supervision.start = review, lose_owner
    try:
        with pytest.raises(ProcessLoss):
            asyncio.run(c.app._run_once(c.token))
        pending = c.app.supervision.pending
        c.app.supervision.pending = None
        operation = c.app.store.load_d4_operation(calls[0].model_request_id)
        assert c.app.handoff.reconcile_review_operation(operation)
        result = c.app.handoff.evidence.read("result", operation.start.operation_id)
        assert result["reason"] == "d22_strategy_worker_lost" and result["outcome"] == "model_error"
        assert not c.app.store.list_d4_unresolved_operations(c.task)
        assert len(calls) == 1 and len(c.sends) == 4
    finally:
        release.set()
        if "pending" in locals():
            assert pending[1].wait(3)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_unknown_tool_is_never_exempt_from_admission(http_case):
    from uuid import uuid4
    from agent_core.operation_accounting import D4OperationStart, D4RecoveryRequired
    c = http_case
    identity = str(uuid4())
    c.app.store.begin_d4_operation(D4OperationStart(identity, c.task, "tool_dispatch", c.app.clock(),
        c.app.state.latest_checkpoint.sequence, "1" * 64, "unknown-tool-result", c.app.config.d4_budget.budget_policy_ref))
    # Even a broad owner callback must not exempt an actual tool operation.
    c.app.store.live_strategy_operation = lambda key: True
    assert c.app.store.blocking_d4_operations(c.task)
    with pytest.raises(D4RecoveryRequired):
        c.app._check_dispatch()
    assert not c.sends and not any(m.calls for m in c.models)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("continue_once", [False, True])
def test_stop_before_dispatch_preserves_registered_plan_and_can_resume_it(http_case, continue_once):
    c = http_case
    install(c, steps=5, stop="inconclusive")
    outer, inner = c.models[0].invoke, c.models[1].invoke
    registered, decisions = [], []
    ready = threading.Event()
    commit = c.app.controller.commit_d3_transition
    def commit_then_review(operation, *args, **kwargs):
        value = commit(operation, *args, **kwargs)
        if operation == "register_d3_plan" and len(c.app.state.plans) == 5 and not registered:
            registered.append(c.app.state.plans[-1])
            ready.set()
            assert c.app.supervision.pending[1].wait(3)
            c.app.supervision.poll()
        return value
    c.app.controller.commit_d3_transition = commit_then_review
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            assert ready.wait(8)
            return dict(decision="request_intent_resolution", reason="Review the remaining value before another dispatch.",
                        evidence_refs=[envelope.request.history[-1].ref], suggestion=None)
        if envelope.output_contract["stage"] == "intent_evaluation" and continue_once and not decisions:
            decisions.append("continue")
            return dict(decision="continue", reason="The preserved registered experiment still separates the hypotheses.",
                        evidence_refs=[i.ref for i in envelope.request.history])
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            packet = json.loads(envelope.request.history_context)
            if packet["intent_execution"].get("stop_requested"):
                return dict(kind="intent_resolution", outcome="inconclusive", reason="Request a scoped value judgment.",
                    evidence_refs=[i["ref"] for i in packet["items"]], fact_ids=[], next_intent_candidates=[])
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert len(c.sends) == (5 if continue_once else 4) and len(c.app.state.plans) == 5
    original = registered[0]
    preserved = next(p for p in c.app.state.plans if p.plan_id == original.plan_id)
    assert preserved.steps == original.steps
    assert preserved.status.value == ("completed" if continue_once else "running")
    assert not any(r.kind in {"suspended", "resumed"} for r in c.app.store.list_d8_tree_records(c.task))


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_history_read_can_complete_while_supervisor_is_inflight(http_case):
    c = http_case
    install(c, steps=6)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    read, apply_read = threading.Event(), c.app.handoff.apply_read_action
    requested = []
    def save_read(*args):
        result = apply_read(*args)
        read.set()
        return result
    c.app.handoff.apply_read_action = save_read
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            assert read.wait(8)
            return continue_review(envelope.request)
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) == 4 and not requested:
            requested.append(envelope.request.model_request_id)
            return dict(kind="history_query", query={"intent_id": envelope.request.context.intent_id})
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert read.is_set() and len(requested) == 1 and len(c.sends) == 6


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_deadline_finishes_without_waiting_for_readonly_supervision(http_case):
    c = http_case
    install(c, steps=6)
    outer, start = c.models[0].invoke, c.app.supervision.start
    release, started = threading.Event(), threading.Event()
    workers = []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            started.set()
            assert release.wait(8)
            return continue_review(envelope.request)
        return outer(envelope, *args)
    def reach_deadline(intent):
        start(intent)
        assert started.wait(3)
        workers.append(c.app.supervision.pending)
        c.clock.now = c.app.state.task_run.deadline_at
        c.app._check_dispatch()
    c.models[0].invoke, c.app.supervision.start = review, reach_deadline
    try:
        result = asyncio.run(c.app._run_once(c.token))
        assert not result.recovery_required, result
        assert c.app.state.task_run.status.value == "deadline_exceeded" and len(c.sends) == 4
        assert c.app.supervision.pending is None
        assert not c.app.store.list_d4_unresolved_operations(c.task)
        record = c.app.handoff.evidence.read("result", workers[0][0].model_request_id)
        assert record["reason"] == "d22_strategy_local_attempt_retired"
    finally:
        release.set()
        if workers:
            assert workers[0][1].wait(3)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_more_unreviewed_notes_do_not_automatically_veto_a_strategy_stop(http_case):
    c = http_case
    install(c, steps=1, stop="inconclusive")
    outer, inner = c.models[0].invoke, c.models[1].invoke
    notes = []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            return dict(decision="request_intent_resolution", reason="Repetition has not introduced a discriminating experiment.",
                        evidence_refs=[envelope.request.history[-1].ref], suggestion=None)
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and c.sends:
            packet = json.loads(envelope.request.history_context)
            if packet["intent_execution"].get("stop_requested"):
                return dict(kind="intent_resolution", outcome="inconclusive", reason="Close the bounded repeated reasoning scope.",
                    evidence_refs=[i["ref"] for i in packet["items"]], fact_ids=[], next_intent_candidates=[])
            notes.append(envelope.request.model_request_id)
            assert len(notes) <= 8, "unreviewed notes evaded the strategy decision"
            return dict(kind="working_record", content="Repeat the same untested premise without an additional observation.",
                        evidence_refs=[packet["items"][0]["ref"]])
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    assert len(c.sends) == 1 and 4 <= len(notes) <= 6
    assert c.app.state.intents[0].status.value == "inconclusive"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_supervision_input_is_losslessly_shared_and_old_frozen_shape_is_unchanged(http_case):
    from dataclasses import replace
    from agent_adapters.deepseek_roles import build_payload
    from agent_core.handoff_contracts import handoff_json
    from scripts.analyze_run_timing import analyze
    c = http_case
    install(c, steps=6)
    outer, captured = c.models[0].invoke, []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "strategy_supervision":
            captured.append(envelope)
            return continue_review(envelope.request)
        return outer(envelope, *args)
    c.models[0].invoke = review
    result = asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required, result
    envelope = captured[0]
    request = envelope.request
    packet = json.loads(request.prior_summary)
    assert "recent_results" not in packet
    assert packet["exploration_progress"]["http_request_count"] >= 4
    assert packet["exploration_progress"]["remaining_seconds"] > 0
    for recent in packet["d8_progress"]["recent"]:
        original = c.app.sessions.evidence.read("decision", recent["ref"].split(":", 1)[1])
        assert recent["data"]["response"] == original["response"]
        assert recent["data"]["observation"] == original["observation"]
        assert "context" not in recent["data"]["request"]
        assert "history_context" not in recent["data"]["request"]
    payload = build_payload(envelope, c.app.config.outer_role, context_loader=c.app.web.model_context)
    historical = json.loads(payload["messages"][1]["content"])
    current = json.loads(payload["messages"][3]["content"])
    assert "history" not in current
    document = {**historical, **current}
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                node = document
                for part in value["$ref"][2:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    node = node[int(part)] if isinstance(node, list) else node[part]
                return expand(node)
            return {k: expand(v) for k, v in value.items()}
        return [expand(v) for v in value] if isinstance(value, list) else value
    expanded = expand(document)
    assert expanded["prior_summary"] == packet
    assert expanded["history"] == [json.loads(handoff_json(i.visible())) for i in request.history]
    assert '"$ref"' in json.dumps(document)
    assert "Distinguish feasibility from priority" in payload["messages"][2]["content"]
    assert "Applicable stage rules:" not in payload["messages"][0]["content"]
    # Removing the new data section represents the previously frozen request format.
    legacy_packet = {k: v for k, v in packet.items() if k != "exploration_progress"}
    legacy_packet["recent_results"] = [r["data"]["observation"] for r in packet["d8_progress"]["recent"]]
    for recent in legacy_packet["d8_progress"]["recent"]:
        recent["data"] = c.app.sessions.evidence.read("decision", recent["ref"].split(":", 1)[1])
    legacy = replace(request, prior_summary=handoff_json(legacy_packet))
    old_payload = build_payload(replace(envelope, request=legacy), c.app.config.outer_role)
    assert isinstance(json.loads(old_payload["messages"][3]["content"])["prior_summary"], str)
    assert "Applicable stage rules:" in old_payload["messages"][0]["content"]
    assert "Distinguish feasibility from priority" not in old_payload["messages"][2]["content"]
    assert len(json.dumps(payload).encode()) < len(json.dumps(old_payload).encode())
    assert payload["thinking"] == old_payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == old_payload["reasoning_effort"] == "high"
    report = analyze(c.directory, c.task)["supervision"]
    assert report["counts"]["calls"] == len(captured)
    assert report["counts"]["applied"] >= 1 and report["counts"]["stops_applied"] == 0
    assert report["counts"]["voluntary_yields"] == 0


@pytest.mark.parametrize("change,expected", [
    ("none", "current"), ("append", "new_evidence"), ("observation_change", "observation_changed"),
    ("knowledge_lost", "knowledge_invalidated"), ("source_lost", "cited_source_unavailable"),
    ("selection", "selection_changed"), ("constraint", "constraints_changed"),
    ("intent_ended", "intent_not_running"), ("task_ended", "task_not_running")])
def test_supervision_validity_separates_hard_changes_from_appends(change, expected):
    from types import SimpleNamespace as NS
    from agent_core.strategy_supervision import StrategySupervision
    items = [NS(ref="source", source_status="available", conflict=False),
             NS(ref="fact", source_status="available", conflict=False)]
    app = NS(state=NS(task_run=NS(status=NS(value="running")), intents=[NS(intent_id="intent", status=NS(value="running"))]),
        handoff=NS(history=lambda: items), store=NS(list_d3_review_applications=lambda _: []))
    supervisor = StrategySupervision(app)
    supervisor.epoch = lambda _: "epoch"
    observations = {"observation": "original"}
    supervisor.observation_signatures = lambda _: observations
    supervisor.knowledge_version = lambda _: "knowledge"
    frozen = dict(scope_ref="epoch", constraint_version=0, observation_signatures=dict(observations),
        knowledge_version="knowledge", knowledge_refs=["fact"], strategy_resolution_policy="review_latest_evidence")
    if change == "append": observations["new"] = "new signature"
    elif change == "observation_change": observations["observation"] = "changed"
    elif change == "knowledge_lost": items.pop()
    elif change == "source_lost": items[0].source_status = "unavailable"
    elif change == "selection": supervisor.epoch = lambda _: "other"
    elif change == "constraint": app.store.list_d3_review_applications = lambda _: [NS(constraint_version=1)]
    elif change == "intent_ended": app.state.intents[0].status.value = "inconclusive"
    elif change == "task_ended": app.state.task_run.status.value = "deadline_exceeded"
    request = NS(intent_id="intent", task_run_id="task", prior_summary=json.dumps(frozen))
    result = {"raw": {"decision": "request_intent_resolution", "evidence_refs": ["source"]}}
    assert supervisor.validity(request, result) == expected
    assert supervisor.applicable(request, result) == (expected in {"current", "new_evidence"})


def test_nullable_object_diagnostics_keep_acceptance_and_report_actual_fields():
    from agent_core.output_contracts import obj, text, shape_issues
    schema = {"anyOf": [{"type": "null"}, obj({"reason": text(), "evidence_refs": {"type": "array", "items": text()}})]}
    assert not shape_issues(None, schema)
    assert not shape_issues({"reason": "scoped reason", "evidence_refs": []}, schema)
    issues = shape_issues({"reason": 7}, schema)
    assert {i.path for i in issues} == {"$/reason", "$/evidence_refs"}
    assert all(i.expected != "null" for i in issues)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("boundary",["normal","commit_gap","unconfirmed","unexpected","known_response"])
def test_supervision_poll_failure_settles_cancelled_inner_before_technical_wait(http_case,monkeypatch,boundary):
    import time
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    from agent_core.modeling import ModelError,ModelErrorKind
    c=http_case
    install(c,steps=6)
    outer,inner=c.models[0].invoke,c.models[1].invoke
    entered,exited,failed=threading.Event(),threading.Event(),threading.Event()
    release=threading.Event()
    if boundary=='unconfirmed':monkeypatch.setattr('agent_core.dual_llm_modeling.MODEL_CANCEL_GRACE_SECONDS',0.03)
    cancelled=[]
    save=c.app.model_evidence.save
    injected=[]
    def persist(evidence):
        if boundary=='commit_gap' and evidence.reason=='model_cancelled' and not injected:
            injected.append(True)
            raise D3RecoveryRequired('d8_handoff_evidence_write_recovery_required')
        return save(evidence)
    monkeypatch.setattr(c.app.model_evidence,'save',persist)
    def review(envelope,*args):
        if envelope.output_contract['stage']=='strategy_supervision':
            if envelope.request.correction_of_model_request_id is None:return {'bad':'format'}
            assert entered.wait(3)
            return continue_review(envelope.request)
        return outer(envelope,*args)
    def choose(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan' and len(c.sends)>=4 and not entered.is_set() and boundary!='known_response':
            cancelled.append(envelope.request.model_request_id)
            entered.set()
            until=time.monotonic()+5
            while not args[-1].is_cancelled and time.monotonic()<until:time.sleep(0.002)
            assert args[-1].is_cancelled
            if boundary=='unconfirmed':assert release.wait(5)
            exited.set()
            return ModelError(ModelErrorKind.CANCELLED,'model_cancelled')
        return inner(envelope,*args)
    poll,start=c.app.supervision.poll,c.app.supervision.start
    def start_correction(intent):
        start(intent)
        pending=c.app.supervision.pending
        if pending and pending[0].correction_of_model_request_id is None:
            assert pending[1].wait(3)
            poll()
            poll()
    monkeypatch.setattr(c.app.supervision,'start',start_correction)
    def interrupted_poll():
        pending=c.app.supervision.pending
        if (pending and pending[1].is_set() and pending[0].correction_of_model_request_id
                and entered.is_set() and not failed.is_set()):
            failed.set()
            if boundary=='unexpected':raise RuntimeError('private first failure must not be saved')
            raise D3RecoveryRequired('d8_handoff_evidence_write_recovery_required')
        return poll()
    c.models[0].invoke,c.models[1].invoke=review,choose
    if boundary=='known_response':
        adapter=c.app.inner.adapter.invoke
        def completed_before_interrupt(request,timeout,token):
            value=adapter(request,timeout,token)
            if request.stage=='inner_plan' and len(c.sends)>=4 and not entered.is_set():
                cancelled.append(request.model_request_id)
                entered.set()
                until=time.monotonic()+5
                while not token.is_cancelled and time.monotonic()<until:time.sleep(0.002)
                assert token.is_cancelled
                exited.set()
            return value
        monkeypatch.setattr(c.app.inner.adapter,'invoke',completed_before_interrupt)
    monkeypatch.setattr(c.app.supervision,'poll',interrupted_poll)
    result=asyncio.run(c.app._run_once(c.token))
    assert result.recovery_required and failed.is_set(),result
    assert len(cancelled)==1
    identity=cancelled[0]
    diagnostic=c.app.output_boundary.evidence.read('call-interruption',identity)
    assert diagnostic['phase']=='supervision_poll'
    assert diagnostic['failure']['exception_types'][0]==('RuntimeError' if boundary=='unexpected' else 'D3RecoveryRequired')
    assert 'private' not in json.dumps(diagnostic)
    if boundary=='unexpected':assert result.stop_reason=='d22_strategy_result_processing_failed'
    if boundary=='unconfirmed':
        try:
            assert diagnostic['adapter_exit_confirmed'] is False and diagnostic['outcome']=='unknown'
            c.app._reconcile_known_d4_operations()
            assert c.app.store.load_d3_operation(identity).completion is None
            assert c.app.store.load_d4_operation(identity).completion is None
            assert len(c.sends)==4
        finally:
            release.set()
            assert exited.wait(1)
            assert c.app.inner.active_calls[identity][0].wait(1)
        return
    assert exited.wait(1) and diagnostic['adapter_exit_confirmed'] is True
    assert (c.app.store.load_d3_operation(identity).completion is None)==(boundary=='commit_gap')
    c.app._reconcile_known_d4_operations()
    assert c.app.store.load_d3_operation(identity).completion is not None
    assert c.app.store.load_d4_operation(identity).completion is not None
    evidence=c.app.model_evidence.read(identity)
    if boundary=='known_response':assert evidence.outcome=='accepted' and evidence.raw['kind']=='http_request_saved'
    else:assert evidence.outcome=='model_error' and evidence.reason=='model_cancelled'
    assert c.app.supervision.pending is None
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    assert len(c.sends)==4
    resumed=asyncio.run(c.app._run_once(c.token))
    assert not resumed.recovery_required,resumed
    assert len(c.sends)==6
    corrections=[c.app.model_evidence.read(o.start.operation_id) for o in c.app.store.list_d3_operations(c.task)
        if o.start.category=='inner_model']
    assert sum(e is not None and e.correction_of_model_request_id==identity for e in corrections)==(0 if boundary=='known_response' else 1)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("state",["cancelled","response","unconfirmed","wrong_binding","active_adapter","bound_decision"])
def test_repair_only_bound_cancelled_provider_result_never_replays(http_case,monkeypatch,state):
    from copy import deepcopy
    from types import SimpleNamespace as NS
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    from agent_core.turn_modeling import digest
    c=http_case
    install(c,steps=1)
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    op=next(o for o in c.app.store.list_d3_operations(c.task) if o.start.category=='inner_model')
    identity=op.start.operation_id
    boundary=c.app.output_boundary
    contract=boundary.evidence.read('contract',identity)
    frozen=boundary.evidence.read('selection-request',identity)
    envelope=dict(request=frozen['request'],output_contract=contract['output_contract'],
        instruction=contract['instruction'],correction=contract['correction'])
    sent=dict(model_request_id=identity,task_run_id=c.task,stage=contract['stage'],role_config_ref=op.start.role_config_ref,
        envelope_sha256=digest(envelope))
    provider=dict(model_request_id=identity,request_sha256=digest(sent),
        error=dict(kind='cancelled',code='model_cancelled'),
        transport=dict(transport_end='cancelled',worker_exit_confirmed=True))
    if state in {'response','bound_decision'}:provider.update(error=None,transport=dict(transport_end='response_received',worker_exit_confirmed=True))
    if state=='unconfirmed':provider['transport']['worker_exit_confirmed']=False
    if state=='wrong_binding':provider['request_sha256']='0'*64
    live=threading.Event()
    if state=='active_adapter':c.app.inner.active_calls[identity]=(live,[])
    read=boundary.evidence.read
    def saved(kind,key):
        if key==identity:
            if kind=='provider-request':return deepcopy(sent)
            if kind=='provider-result':return deepcopy(provider)
            if kind=='diagnostic':return None
            if kind=='call-interruption' and state=='bound_decision':
                return dict(task_run_id=c.task,model_request_id=identity,request_sha256=contract['source_request_sha256'],
                    adapter_exit_confirmed=True,outcome='cancelled',finished_at=c.clock())
        return read(kind,key)
    monkeypatch.setattr(boundary.evidence,'read',saved)
    native=c.app.model_evidence.read
    if state=='bound_decision':
        original=native(identity)
        monkeypatch.setattr(c.app.selection_bindings,'read',lambda key:
            dict(canonical_response=original.raw,finished_at=original.finished_at) if key==identity else None)
    captured=[]
    monkeypatch.setattr(c.app.model_evidence,'read',lambda key:None if key==identity else native(key))
    monkeypatch.setattr(c.app.model_evidence,'save',lambda evidence:captured.append(evidence))
    before=len(c.sends),len(c.models[1].calls)
    if state=='wrong_binding':
        with pytest.raises(D3RecoveryRequired,match='cancelled_call_binding_changed'):boundary.reconcile_formats()
    else:boundary.reconcile_formats()
    if state=='cancelled':
        assert len(captured)==1 and captured[0].reason=='model_cancelled'
        assert captured[0].outcome=='model_error'
    elif state=='bound_decision':
        assert len(captured)==1 and captured[0].outcome=='accepted' and captured[0].raw==original.raw
    else:assert not captured
    assert (len(c.sends),len(c.models[1].calls))==before
    c.app.inner.active_calls.pop(identity,None)
