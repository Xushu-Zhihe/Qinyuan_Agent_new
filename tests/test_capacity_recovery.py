"""Automatic recovery and source coverage: local I/O only, no providers or Docker."""
import asyncio
import json
from dataclasses import replace
from uuid import uuid4
from types import SimpleNamespace

import pytest

from agent_adapters.handoff_evidence import FileD8HandoffEvidence
from agent_adapters.web.http_adapter import D7HttpWireResponse
from agent_adapters.web.task_result import snapshot_v3_task_result
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.handoff_contracts import handoff_json
from agent_core.output_contracts import D8OutputRequest
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.intent_planning import PUBLIC_KNOWLEDGE_GUIDANCE
from test_exploration_loop import install
from test_http import http_case


def test_capacity_projection_ignores_old_states_and_checks_owner(tmp_path):
    evidence = FileD8HandoffEvidence(tmp_path / "evidence")
    task, actor = str(uuid4()), str(uuid4())
    receipt = dict(kind="complete_model_request", task_run_id=task, checkpoint_sequence=4,
        intent_id=actor, scope="task", organization_attempted=True,
        next_action="wait_for_context_change_or_deadline")
    evidence.save("pause", str(uuid4()), receipt)
    assert evidence.capacity_block(task, 4) == receipt
    assert evidence.capacity_block(task, 5) is None
    with pytest.raises(D3RecoveryRequired, match="owner_changed"):
        evidence.capacity_block(str(uuid4()), 4)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("summary_outcome", ["valid", "invalid", "unknown"])
def test_full_request_rejection_organizes_below_history_threshold_and_continues(http_case, monkeypatch, summary_outcome):
    c = http_case
    install(c, steps=6, stop="inconclusive")
    outer = c.models[0].invoke
    summary_calls = []
    def review(envelope,*args):
        if envelope.output_contract["stage"] == "handoff_summary":
            summary_calls.append(envelope.request.model_request_id)
            if summary_outcome == "invalid":return {"unknown_field":"not a summary"}
            if summary_outcome == "unknown":return ModelError(ModelErrorKind.TIMED_OUT,"model_total_timeout")
        return outer(envelope,*args)
    c.models[0].invoke = review
    class Transport:
        def send(self, **kwargs):
            c.sends.append(kwargs)
            return D7HttpWireResponse(200, (("Content-Type", "text/plain"),), b"x" * 6000)
    c.app.web.transport_factory = lambda _: Transport()
    port = c.app.handoff.capacity_port
    fits = port.fits
    threshold = []
    rejected = []
    def limited(request, policy):
        accepted = fits(request, policy)
        business = request.request if isinstance(request, D8OutputRequest) else request
        if getattr(business, "stage", None) != "inner_plan" or not getattr(business, "session_context", ""):
            return accepted
        packet = json.loads(business.session_context)
        if len(packet["messages"]) > 12 and not threshold:
            payload_bytes = port.last_measurement["payload_utf8_bytes"]
            shorter = replace(business, session_context=handoff_json({**packet,"messages":packet["messages"][-12:]}))
            envelope = c.app.output_boundary._contract(shorter)[0]
            fits(envelope, policy)
            short_bytes = port.last_measurement["payload_utf8_bytes"]
            fits(request, policy)
            threshold.append((payload_bytes + short_bytes) // 2)
            records = c.app.sessions.records(packet["intent_id"])
            assert len(handoff_json([c.app.sessions.payload(r) for r in records]).encode()) < c.app.handoff.capacity().capacity.visible_history
        if threshold and port.last_measurement["payload_utf8_bytes"] > threshold[0]:
            rejected.append(business.model_request_id)
            return False
        return accepted
    monkeypatch.setattr(port,"fits",limited)
    result = asyncio.run(c.app._run_once(c.token))
    if summary_outcome != "valid":
        assert result.recovery_required and len(c.sends) == 5, result
        assert c.app.state.task_run.status.value == "running" and not c.app.state.facts
        if summary_outcome == "unknown":
            assert "unknown" in result.stop_reason and len(summary_calls) == 1
            assert c.app.store.blocking_d4_operations(c.task)
        else:
            assert len(summary_calls) == 2  # The original shared correction, not a new retry loop.
            before = len(summary_calls)
            asyncio.run(c.app._run_once(c.token))
            assert len(summary_calls) == before and len(c.sends) == 5
        return
    # The synthetic small window can legitimately block a later end request;
    # this scenario proves exploration advances beyond the first five-step block.
    assert len(c.sends) == 6 and rejected, result
    assert c.app.state.task_run.status.value == "running"
    assert c.app.store.load_d4_operation(rejected[0]) is None
    assert c.app.sessions.evidence.read("request", rejected[0]) is None
    versions = c.app.store.list_d8_handoffs(c.task)
    summaries = [c.app.handoff.evidence.read("request", v.model_request_id) for v in versions if v.model_request_id]
    assert any(any(i["record_id"].startswith("decision/") for i in v["history"]) for v in summaries)
    compact = [e.request for e in c.exploration_envelopes if e.output_contract["stage"] == "inner_plan"
               and "compaction" in json.loads(e.request.session_context)]
    assert compact
    for request in compact:
        packet = json.loads(request.session_context)
        summary = c.app.handoff.evidence.read("handoff", packet["compaction"]["summary_id"])
        assert packet["compaction"]["raw_decisions_retained"]
        assert len(packet["messages"]) >= (3 if summary.get("capacity_recovery") else 12)
    assert all(c.app.sessions.payload(r) for r in c.app.sessions.records())
    assert not c.app.state.facts
    # A successful capacity recovery may now retain one recent record, so a second
    # automatic summary is no longer necessary. Explicitly check later inheritance.
    rows = c.app.sessions.records()
    asyncio.run(c.app.handoff.summarize(rows[-1].turn_id, c.token,
        decision_prefix=rows[:-1], capacity_recovery=True))
    versions = c.app.store.list_d8_handoffs(c.task)
    chained = [(v,c.app.handoff.evidence.read("handoff",v.evidence_ref)) for v in versions]
    chained = [(v,d) for v,d in chained if len(d.get("covered_refs",())) > len(d["actual_read_refs"])]
    assert chained  # A later prefix inherits earlier coverage without claiming to reread it.
    record,data = chained[-1]
    altered = {**data,"covered_refs":[]}
    from agent_core.conclusion_contracts import conclusion_digest
    original_read = c.app.handoff.evidence.read
    with monkeypatch.context() as patch:
        patch.setattr(c.app.handoff.evidence,"read",lambda kind,key: altered if kind=="handoff" and key==record.evidence_ref else original_read(kind,key))
        with pytest.raises(D3RecoveryRequired,match="summary_coverage_binding_changed"):
            c.app.handoff.validate_mutation(SimpleNamespace(state_changes=(replace(record,evidence_sha256=conclusion_digest(altered)),)),
                SimpleNamespace(latest_checkpoint=SimpleNamespace(sequence=record.base_checkpoint_sequence)))
    inner = next(e.request for e in c.exploration_envelopes if e.output_contract["stage"] == "inner_plan")
    assert PUBLIC_KNOWLEDGE_GUIDANCE in json.loads(inner.history_context)["progress_rule"]
    monitors = [e.request for e in c.exploration_envelopes if e.output_contract["stage"] == "strategy_supervision"]
    assert monitors and all(PUBLIC_KNOWLEDGE_GUIDANCE in json.loads(r.prior_summary)["progress_rule"] for r in monitors)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_shared_capacity_stays_running_and_is_visible_without_repeated_models(http_case, monkeypatch):
    c = http_case
    fits = c.app.handoff.capacity_port.fits
    def reject_inner(request, policy):
        result = fits(request,policy)
        stage = request.output_contract["stage"] if isinstance(request,D8OutputRequest) else request.stage
        return result and stage != "inner_plan"
    monkeypatch.setattr(c.app.handoff.capacity_port,"fits",reject_inner)
    result = asyncio.run(c.app._run_once(c.token))
    assert result.recovery_required and c.app.sessions.capacity_error(result.stop_reason)
    assert c.app.state.task_run.status.value == "running"
    assert all(i.status.value != "suspended" for i in c.app.state.intents)
    assert not c.sends and not c.app.state.facts
    block = c.app.handoff.evidence.capacity_block(c.task,c.app.state.latest_checkpoint.sequence)
    assert block["scope"] == "task" and block["next_action"] == "wait_for_context_change_or_deadline"
    projected = snapshot_v3_task_result(c.directory,c.declaration)
    assert projected.get("capacity_block") == block, projected.get("errors")
    assert projected["recovery"]["reason"] == "d8_context_capacity_blocked"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_only_local_block_with_a_fitting_sibling_is_suspended(http_case, monkeypatch):
    c = http_case
    outer = c.models[0].invoke
    def two_directions(envelope,*args):
        raw = outer(envelope,*args)
        if envelope.output_contract["stage"] == "tree_planning" and not c.app.state.turns:
            raw = {**raw,"intents":[dict(description=label,intent_type=None,dependency_refs=[])
                for label in ("First authorized alternative","Second authorized alternative")]}
        return raw
    c.models[0].invoke = two_directions
    fits = c.app.handoff.capacity_port.fits
    first = []
    def reject_first(request,policy):
        result = fits(request,policy)
        business = request.request if isinstance(request,D8OutputRequest) else request
        if getattr(business,"stage",None) == "inner_plan":
            actor = business.context.intent_id
            if not first:first.append(actor)
            return result and actor != first[0]
        return result
    monkeypatch.setattr(c.app.handoff.capacity_port,"fits",reject_first)
    class Transport:
        def send(self,**kwargs):
            c.sends.append(kwargs)
            c.token.is_cancelled = True
            return D7HttpWireResponse(200,(("Content-Type","text/plain"),),b"observed")
    c.app.web.transport_factory = lambda _: Transport()
    result = asyncio.run(c.app._run_once(c.token))
    assert c.app.state.intents[0].status.value == "suspended", result
    assert c.app.state.intents[1].status.value == "running", result
    assert len(c.sends) == 1
    assert not c.app.state.facts
    notes = [c.app.handoff.evidence.read("pause",r.record_id) for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "suspended"]
    assert notes[0]["reason"] == "d8_context_paused:local_session_capacity"
    assert not any("capacity-probe" in op.start.resource_ref for op in c.app.store.list_d4_operations(c.task))


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("kind", ["complete_model_request", "initial_history"])
def test_root_block_before_any_intent_is_visible_and_deadline_still_settles(http_case, monkeypatch, kind):
    c = http_case
    selected = c.app.handoff.select
    attempts = []
    def blocked(phase,*args,**kwargs):
        if phase == "tree_planning":
            attempts.append(1)
            c.app.handoff._pause(kind,(),100,None)
        return selected(phase,*args,**kwargs)
    monkeypatch.setattr(c.app.handoff,"select",blocked)
    waits = []
    async def wait(token):
        waits.append(1)
        block = c.app.handoff.evidence.capacity_block(c.task,c.app.state.latest_checkpoint.sequence)
        assert block and block["scope"] == "task" and block["intent_id"] is None
        if len(waits) == 2:c.clock.now = c.app.state.task_run.deadline_at
    monkeypatch.setattr(c.app,"_wait_continuation",wait)
    result = asyncio.run(c.app.run(c.token))
    assert len(attempts) == 1 and len(waits) == 2
    assert result.stop_reason == "task_run_deadline_exceeded" and c.app.state.task_run.status.value == "deadline_exceeded"
    assert not c.app.state.intents and not c.app.state.facts and not c.sends
    assert not c.models[0].calls and not c.models[1].calls


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_two_large_decisions_can_recover_without_discarding_last_or_uncovered(http_case, monkeypatch):
    c = http_case
    install(c, steps=3, stop="inconclusive")
    fits = c.app.handoff.capacity_port.fits
    blocked = []
    def limited(request, policy):
        accepted = fits(request, policy)
        business = request.request if isinstance(request, D8OutputRequest) else request
        if getattr(business, 'stage', None) != 'inner_plan' or not getattr(business, 'session_context', ''):
            return accepted
        packet = json.loads(business.session_context)
        if len(packet['messages']) > 3 and not packet.get('compaction'):
            blocked.append(business.model_request_id)
            return False
        return accepted
    monkeypatch.setattr(c.app.handoff.capacity_port, 'fits', limited)
    result = asyncio.run(c.app._run_once(c.token))
    assert len(c.sends) == 3 and blocked, result
    summaries = [c.app.handoff.evidence.read('handoff', r.evidence_ref)
                 for r in c.app.store.list_d8_handoffs(c.task)]
    assert any(d.get('capacity_recovery') for d in summaries)
    compact = [json.loads(e.request.session_context) for e in c.exploration_envelopes
               if e.output_contract['stage']=='inner_plan' and json.loads(e.request.session_context).get('compaction')]
    assert compact and any(len(p['messages']) == 3 for p in compact)
    for packet in compact:
        summary = c.app.handoff.evidence.read('handoff', packet['compaction']['summary_id'])
        covered = set(summary.get('covered_refs', summary['actual_read_refs']))
        assert set(packet['compaction']['omitted_decision_refs']) <= covered
        assert packet['messages'][-2]['role'] == 'assistant' and packet['messages'][-1]['role'] == 'tool'
    assert all(c.app.sessions.payload(r) for r in c.app.sessions.records())
    assert not c.app.state.facts
