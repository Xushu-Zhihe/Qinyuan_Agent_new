"""Rolling Intent execution through the actual HTTP control/evidence boundaries."""
import asyncio
import json
from dataclasses import replace
import pytest
from test_http import http_case


def install(case, *, steps=3, stop="succeeded", continue_once=False, no_fact=False):
    original_inner, original_outer = case.models[1].invoke, case.models[0].invoke
    calls = {"plans": 0, "reviews": 0}
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            request = envelope.request
            context = json.loads(request.history_context)
            assert context["intent_execution"]["policy"] == "rolling_v1"
            closure = context["intent_execution"].get("stop_requested")
            if len(case.sends) >= steps or closure:
                items = context["items"]
                available = context["intent_execution"]["facts"]
                return dict(kind="intent_resolution", outcome="inconclusive" if closure else stop,
                    reason="Tested the finite declared scope; unresolved outside that scope. Revisit if prerequisites change.",
                    evidence_refs=[i["ref"] for i in items],
                    fact_ids=[i["fact_id"] for i in available] if stop == "succeeded" and not closure else [],
                    next_intent_candidates=[])
            calls["plans"] += 1
            return dict(kind="http_request", method="GET", path_query="/?candidate="+str(calls["plans"]), headers={}, body="")
        assert envelope.output_contract["stage"] != "inner_complete"
        return original_inner(envelope, *args)
    def outer(envelope, *args):
        stage = envelope.output_contract["stage"]
        if stage == "fact_review" and no_fact:
            return dict(decision="no_new_information", reason="Keep execution evidence; no durable conclusion.")
        if stage == "fact_review":
            return dict(decision="accept", reason="Distinct bounded observation.",
                refined_content="Candidate "+str(len(case.sends))+" yielded this request-scoped response.")
        if stage == "intent_evaluation":
            calls["reviews"] += 1
            return dict(decision="continue" if continue_once and calls["reviews"] == 1 else stop,
                reason="Need one additional check." if continue_once and calls["reviews"] == 1 else
                       "Only the tested finite scope is resolved; no global impossibility claim.",
                evidence_refs=[i.ref for i in envelope.request.history])
        if stage == "tree_planning" and envelope.request.turn_id:
            return dict(kind="exhausted", reason="No further direction in this controlled case.", evidence_refs=[])
        return original_outer(envelope, *args)
    case.models[1].invoke, case.models[0].invoke = inner, outer
    return calls


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_three_observations_before_intent_completion(http_case):
    c = http_case
    calls = install(c, steps=3)
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 3 and len(c.app.state.facts) == 3
    assert all(p.status.value == "completed" for p in c.app.state.plans)
    assert calls["reviews"] == 1
    assert c.app.state.intents[0].status.value == "succeeded"


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("stop", ["failed", "inconclusive"])
def test_legitimate_stop_without_fact_is_a_reviewed_result(http_case, stop):
    c = http_case
    install(c, steps=1, stop=stop, no_fact=True)
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert not c.app.state.facts and len(c.sends) == 1
    assert c.app.state.intents[0].status.value == stop
    history = c.app.handoff.history()
    summaries = [json.loads(i.content) for i in history if i.record_type == "progress" and
                 json.loads(i.content).get("identity") == "reviewed_intent_result_not_verified_fact"]
    assert summaries and "tested finite scope" in summaries[0]["reviewed_summary"]


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_insufficient_completion_review_keeps_intent_running(http_case):
    c = http_case
    calls = install(c, steps=1, continue_once=True)
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 1 and calls["reviews"] == 2
    rows = c.app.store.list_d8_tree_records(c.task)
    assert any(r.kind == "evaluation_continued" for r in rows)
    assert not any(r.kind == "inconclusive" for r in rows)


def test_old_run_configuration_does_not_acquire_rolling_policy():
    from agent_adapters.deepseek_roles import role_config
    from agent_core.dual_llm_contracts import D3RunConfig, D3Authorization, d3_payload
    from uuid import uuid4
    outer, inner = role_config("outer_planning"), role_config("inner_action")
    config = D3RunConfig(outer, inner, D3Authorization(str(uuid4()), ()), outer.role_config_ref)
    assert "intent_execution_policy" not in d3_payload(config)
    assert D3RunConfig.from_dict(json.loads(json.dumps(d3_payload(config)))) == config
    with pytest.raises(ValueError):
        replace(config, intent_execution_policy="rolling_v1")


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_saved_end_review_resumes_without_repeating_tool_or_models(http_case, monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    calls = install(c, steps=1, stop="failed", no_fact=True)
    commit = c.app.handoff._commit
    def lost(operation, record, *args, **kwargs):
        if operation == "evaluate_d8_tree_intent":
            raise ProcessLoss("after known outer decision, before control commit")
        return commit(operation, record, *args, **kwargs)
    monkeypatch.setattr(c.app.handoff, "_commit", lost)
    with pytest.raises(ProcessLoss):
        asyncio.run(c.app.run(c.token))
    assert len(c.sends) == 1 and calls["reviews"] == 1
    c.app.store.close()
    c.app = c.create()
    result = asyncio.run(c.app.resume(c.task, c.token))
    assert result.stop_reason == "search_exhausted", result
    assert len(c.sends) == 1 and calls["reviews"] == 1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_multi_source_outer_payload_and_measurement_are_bound(http_case):
    from agent_adapters.deepseek_roles import build_payload
    c = http_case
    install(c, steps=3)
    assert asyncio.run(c.app.run(c.token)).stop_reason == "d9_awaiting_external_feedback"
    record = next(r for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "succeeded")
    from agent_core.handoff_contracts import D8HandoffRequest
    request = D8HandoffRequest.from_dict(c.app.handoff.evidence.read("request", record.model_request_id))
    projection = c.app.web.model_context(request)
    assert len(projection["http_observations"]) == 3
    measured = c.app.output_boundary.evidence.read("input-measurement", record.model_request_id)
    assert measured["payload_utf8_bytes"] > 0 and measured["input_tokens"] is None
    assert "protected_source_projection" not in measured  # counts only, no bodies
    frozen = c.app.sessions.evidence.read("request", record.inner_request_id)
    context = json.loads(frozen["request"]["session_context"])
    assert context["compaction"]["raw_decisions_retained"] is True
    assert len(context["messages"]) <= 12


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_periodic_strategy_stop_collects_inner_result_summary(http_case):
    c = http_case
    install(c, steps=20, no_fact=True, stop="inconclusive")
    original = c.models[0].invoke
    def outer(envelope, *args):
        value = original(envelope, *args)
        if envelope.output_contract["stage"] == "progress_review":
            value["decision"] = "abandon_current_intent"
            value["progress"]["classification"] = "repetitive"
        return value
    c.models[0].invoke = outer
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert 1 <= len(c.sends) < 20
    assert c.app.state.intents[0].status.value == "inconclusive"
    assert any("reviewed_intent_result_not_verified_fact" in i.content for i in c.app.handoff.history())


def test_public_simulated_rolling_file_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_adapters import task_cli
    from agent_adapters.simulation import simulation_config
    from test_public_simulation import request
    raw = request(tmp_path)
    raw.update(execution_profile="d8_v22", d8_config=simulation_config(rolling=True))
    from test_public_simulation import start
    result, args = start(tmp_path, monkeypatch, raw)
    assert result["errors"] == [], result
    assert result["goal_evaluation"] == "not_evaluated"
    assert len(result["verified_facts"]) == 1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("decision", ["reject", "insufficient"])
def test_rejected_fact_returns_to_inner_with_observation_preserved(http_case, decision):
    c = http_case
    install(c, steps=1, stop="inconclusive")
    original = c.models[0].invoke
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "fact_review":
            return dict(decision=decision, reason="The claim is unsupported; keep the observation only.")
        return original(envelope, *args)
    c.models[0].invoke = outer
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert len(c.sends) == 1 and not c.app.state.facts
    plan, = c.app.state.plans
    assert c.app.store.load_d3_plan_result(plan.plan_id).outcome == "observed"
    record, = c.app.store.list_d8_conclusions(c.task)
    assert record.outcome == ("rejected" if decision == "reject" else "insufficient")


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_inner_review_request_can_continue_without_new_fact(http_case):
    c = http_case
    install(c, steps=1, stop="inconclusive")
    original = c.models[1].invoke
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "fact_proposal":
            return dict(kind="request_review", reason="Need a different check before asserting a conclusion.")
        return original(envelope, *args)
    c.models[1].invoke = inner
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert len(c.sends) == 1 and not c.app.state.facts
    assert c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id).outcome == "observed"


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_refined_fact_details_are_readable_without_repeating_execution(http_case):
    c = http_case
    install(c, steps=1)
    original = c.models[1].invoke
    reads = []
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            context = json.loads(envelope.request.history_context)
            if context["intent_execution"]["facts"] and not reads:
                ref = context["intent_execution"]["facts"][0]["ref"]
                reads.append(ref)
                return dict(kind="history_details", refs=[ref])
        return original(envelope, *args)
    c.models[1].invoke = inner
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(reads) == 1 and len(c.sends) == 1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_compacted_decision_can_be_retrieved_by_exact_ref(http_case):
    c = http_case
    install(c, steps=3)
    original = c.models[1].invoke
    read_refs = []
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and len(c.sends) == 3 and not read_refs:
            compact = json.loads(envelope.request.session_context).get("compaction")
            if compact:
                ref = compact["omitted_decision_refs"][0]
                read_refs.append(ref)
                return dict(kind="history_details", refs=[ref])
        return original(envelope, *args)
    c.models[1].invoke = inner
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(read_refs) == 1 and len(c.sends) == 3


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_uncertain_progress_can_continue_a_concrete_check(http_case):
    c = http_case
    install(c, steps=3)
    original = c.models[0].invoke
    def outer(envelope, *args):
        value = original(envelope, *args)
        if envelope.output_contract["stage"] == "progress_review":
            value["progress"]["classification"] = "inconclusive"
            value["progress"]["reason"] = "One more specific observation can resolve the local criterion."
        return value
    c.models[0].invoke = outer
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 3


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_child_planning_keeps_all_parent_supporting_facts(http_case):
    c = http_case
    install(c, steps=3)
    inner_original, outer_original = c.models[1].invoke, c.models[0].invoke
    supports = []
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_candidate":
            return dict(action=dict(kind="no_candidate"))
        return inner_original(envelope, *args)
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and envelope.request.turn_id:
            supports.extend(i.record_id for i in envelope.request.history if i.record_type == "verified_fact")
        return outer_original(envelope, *args)
    c.models[1].invoke, c.models[0].invoke = inner, outer
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert set(supports) == {f.fact_id for f in c.app.state.facts}


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_replan_after_multiple_completed_steps_preserves_prior_evidence(http_case):
    c = http_case
    install(c, steps=5)
    original = c.models[0].invoke
    decisions = []
    def outer(envelope, *args):
        value = original(envelope, *args)
        if envelope.output_contract["stage"] == "progress_review" and len(c.sends) >= 3 and not decisions:
            value["decision"] = "replan_current_intent"
            decisions.append(value["decision"])
        return value
    c.models[0].invoke = outer
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 5 and len(decisions) == 1
    assert len(c.app.state.facts) == 5



def test_public_file_intent_can_continue_three_plans(tmp_path, monkeypatch):
    from agent_adapters.simulation import simulation_config, SimulationModel
    from test_public_simulation import request, start
    original = SimulationModel.invoke
    def model(self, envelope, *args):
        stage = envelope.output_contract["stage"]
        if stage == "inner_plan":
            request = envelope.request
            if request.context.prior_plan_ids and len(request.context.prior_plan_ids) < 3:
                return dict(template="sha256_only", observation_mode="new_observation", source_proof_id=None)
        if stage == "fact_review" and getattr(envelope.request, "existing_facts", ()):
            return dict(decision="reuse", reason="Same scoped digest conclusion; preserve every observation.",
                        reuse_fact_id=envelope.request.existing_facts[0]["fact_id"])
        return original(self, envelope, *args)
    monkeypatch.setattr(SimulationModel, "invoke", model)
    raw = request(tmp_path)
    raw.update(execution_profile="d8_v22", d8_config=simulation_config(rolling=True))
    result, args = start(tmp_path, monkeypatch, raw)
    assert result["errors"] == [], result
    assert len(result["verified_facts"]) == 1
    from agent_core.sqlite_control_store import SqliteControlStore
    with SqliteControlStore.open_readonly(args.state_dir / result["task_run_id"] / "control.sqlite") as store:
        rows = store.load_control_state_rows(result["task_run_id"])
        assert len(rows.plans) == 3 and all(p.status.value == "completed" for p in rows.plans)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_committed_resolution_repairs_inner_application_marker(http_case, monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    calls = install(c, steps=1, stop="failed", no_fact=True)
    mark = c.app.store.mark_d3_operation_applied
    def lost(identity, reference):
        evidence = c.app.model_evidence.read(identity)
        if evidence is not None and isinstance(evidence.raw, dict) and evidence.raw.get("kind") == "intent_resolution":
            raise ProcessLoss("end committed before inner application marker")
        return mark(identity, reference)
    monkeypatch.setattr(c.app.store, "mark_d3_operation_applied", lost)
    with pytest.raises(ProcessLoss):
        asyncio.run(c.app.run(c.token))
    assert c.app.state.intents[0].status.value == "failed"
    c.app.store.close()
    c.app = c.create()
    result = asyncio.run(c.app.resume(c.task, c.token))
    assert result.stop_reason == "search_exhausted", result
    assert len(c.sends) == 1 and calls["reviews"] == 1
    record = next(r for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "failed")
    assert c.app.store.load_d3_operation(record.inner_request_id).application_ref == record.record_id


@pytest.mark.parametrize("saved_observation", [True, False])
def test_failed_review_requires_saved_observation_not_model_assertion(saved_observation):
    from types import SimpleNamespace
    from agent_core.intent_execution import parse_evaluation
    data = {"observation": {"mcp_tool_result": {"status": "completed", "stdout": "bounded negative observation"}}} if saved_observation else {"decision": "I think this failed"}
    item = SimpleNamespace(ref="saved-ref", record_type="progress", source_status="available", conflict=False,
        identity="historical_decision_not_verified", content=json.dumps(data))
    request = SimpleNamespace(history=(item,))
    policy = {"version": 2, "support_fact_refs": [], "proposal": {"outcome": "failed"}}
    raw = dict(decision="failed", reason="The observed condition negates this bounded local goal.", evidence_refs=[item.ref])
    if saved_observation:
        assert parse_evaluation(request, raw, policy)["decision"] == "failed"
    else:
        with pytest.raises(ValueError, match="negative_observation_required"):
            parse_evaluation(request, raw, policy)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_missing_end_review_source_is_not_silently_presented_as_available(http_case, monkeypatch):
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    c = http_case
    install(c, steps=1, stop="failed", no_fact=True)
    assert asyncio.run(c.app.run(c.token)).stop_reason == "search_exhausted"
    record = next(r for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "failed")
    read = c.app.handoff.evidence.read
    monkeypatch.setattr(c.app.handoff.evidence, "read", lambda kind, identity:
        None if kind == "request" and identity == record.model_request_id else read(kind, identity))
    with pytest.raises(D3RecoveryRequired, match="d22_resolution_request_missing"):
        c.app.handoff.history()
    assert len(c.sends) == 1


@pytest.mark.parametrize("with_fact", [False, True])
def test_resolution_success_schema_matches_native_support_requirement(with_fact):
    from agent_core.intent_execution import resolution_schema, parse_resolution
    from agent_core.output_contracts import shape_issues
    from types import SimpleNamespace
    packet = dict(items=[dict(ref="source", source_status="available", conflict=False)],
        intent_execution=dict(facts=[dict(fact_id="fact", ref="source")] if with_fact else []),
        execution_feasibility=dict(allowed_action_kinds=["probe", "complete_intent"]))
    request = SimpleNamespace(history_context=json.dumps(packet))
    raw = dict(kind="intent_resolution", outcome="succeeded", reason="Bounded result.",
               evidence_refs=["source"], fact_ids=[], next_intent_candidates=[])
    assert shape_issues(raw, resolution_schema(request))
    with pytest.raises(ValueError, match="support_required"):
        parse_resolution(raw, request)
    if with_fact:
        raw["fact_ids"] = ["fact"]
        assert not shape_issues(raw, resolution_schema(request))
        assert parse_resolution(raw, request).fact_ids == ("fact",)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("repeat_invalid", [False, True])
def test_resolution_reference_correction_is_specific_and_bounded(http_case, repeat_invalid):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c = http_case
    install(c, steps=1)
    original = c.models[1].invoke
    attempts, refs, suggested = [], [], []
    original_outer = c.models[0].invoke
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and envelope.request.turn_id:
            suggested.extend(envelope.request.inner_suggestions)
        return original_outer(envelope, *args)
    c.models[0].invoke = outer
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_candidate":
            return {"action": {"kind": "no_candidate"}}
        raw = original(envelope, *args)
        if isinstance(raw, dict) and raw.get("kind") == "intent_resolution":
            packet = json.loads(envelope.request.history_context)
            facts = packet["intent_execution"]["facts"]
            assert all(set(f) == {"fact_id", "ref"} for f in facts)
            raw["evidence_refs"] = [f["ref"] for f in facts]
            other = next(i["ref"] for i in packet["items"] if i["record_id"] == c.app.state.executions[0].execution_id)
            bad = not attempts or repeat_invalid
            raw["next_intent_candidates"] = [dict(description="Check a different scoped direction.", intent_type=None,
                reason="The saved observation motivates another check.", dependency_refs=["missing-ref" if bad else other],
                evidence_refs=[other])]
            attempts.append(envelope)
            refs.append(other)
            if len(attempts) == 2:
                issues = envelope.correction["issues"]
                assert any(i["path"] == "$/next_intent_candidates/0/dependency_refs/0" and
                           i["code"] == "enum_mismatch" for i in issues), envelope.correction
        return raw
    c.models[1].invoke = inner
    result = asyncio.run(c.app.run(c.token))
    assert len(attempts) == 2 and len(c.sends) == 1
    assert len(c.app.state.facts) == 1
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    if repeat_invalid:
        assert result.stop_reason != "d9_awaiting_external_feedback"
        assert len(snapshot_v3_task_result(c.directory, c.declaration)["verified_facts"]) == 1
        c.app.store.close()
        c.app = c.create()
        asyncio.run(c.app.resume(c.task, c.token))
        assert len(attempts) == 2 and len(c.sends) == 1
    else:
        assert result.stop_reason == "search_exhausted", result
        assert suggested and suggested[0].dependency_refs == (refs[-1],)
        saved = c.app.model_evidence.read(attempts[-1].request.model_request_id)
        assert refs[-1] not in saved.raw["evidence_refs"]


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_completion_only_review_never_registers_a_new_http_plan(http_case):
    from agent_core.output_contracts import shape_issues
    c = http_case
    install(c, steps=12)
    old_outer, old_inner = c.models[0].invoke, c.models[1].invoke
    denied, restricted = [], []
    def outer(envelope, *args):
        raw = old_outer(envelope, *args)
        if envelope.output_contract["stage"] == "progress_review":
            raw["constraints"] = [dict(kind="allowed_action_kinds", scope_kind="intent",
                                       allowed_action_kinds=["complete_intent"])]
        return raw
    def inner(envelope, *args):
        raw = old_inner(envelope, *args)
        if envelope.output_contract["stage"] != "inner_plan":return raw
        context = json.loads(envelope.request.history_context)
        if "probe" in context["execution_feasibility"]["allowed_action_kinds"]:return raw
        assert shape_issues(dict(kind="http_request", method="GET", path_query="/forbidden", headers={}, body=""),
                            envelope.output_contract["schema"])
        restricted.append(len(c.sends))
        if not denied:
            denied.append(True)
            return dict(kind="http_request", method="GET", path_query="/forbidden", headers={}, body="")
        assert envelope.correction["issues"]
        return dict(kind="intent_resolution", outcome="succeeded", reason="Existing saved support resolves this scope.",
            evidence_refs=[i["ref"] for i in context["items"]],
            fact_ids=[i["fact_id"] for i in context["intent_execution"]["facts"]], next_intent_candidates=[])
    c.models[0].invoke, c.models[1].invoke = outer, inner
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(restricted) == 2 and len(set(restricted)) == 1
    assert len(c.sends) == restricted[0] == len(c.app.state.plans)
    assert all(p.status.value == "completed" for p in c.app.state.plans)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("status", [401, 402, 403])
def test_known_provider_access_failure_is_durable_and_not_redispatched(http_case, status):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c = http_case
    calls = []
    def failed(envelope, *args):
        calls.append(envelope)
        return ModelError(ModelErrorKind.PROVIDER_FAILURE, "deepseek_http_" + str(status))
    c.models[0].invoke = failed
    expected = "d8_provider_payment_required" if status == 402 else "d8_provider_auth_failed"
    first = asyncio.run(c.app.run(c.token))
    assert first.stop_reason == expected, first
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    assert snapshot_v3_task_result(c.directory, c.declaration)["recovery"]["reason"] == expected
    c.app.store.close()
    c.app = c.create()
    second = asyncio.run(c.app.resume(c.task, c.token))
    assert second.stop_reason == expected and len(calls) == 1
    assert not c.sends


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_known_provider_failure_saved_before_completion_repairs_on_resume(http_case, monkeypatch):
    from agent_core.modeling import ModelError, ModelErrorKind
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    calls = []
    def failed(*args):
        calls.append(True)
        return ModelError(ModelErrorKind.PROVIDER_FAILURE, "deepseek_http_402")
    c.models[0].invoke = failed
    def lost(*args):raise ProcessLoss("after known provider result, before completion")
    monkeypatch.setattr(c.app.store, "complete_d4_operation", lost)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    c.app.store.close()
    c.app = c.create()
    result = asyncio.run(c.app.resume(c.task, c.token))
    assert result.stop_reason == "d8_provider_payment_required", result
    assert len(calls) == 1 and not c.app.store.list_d4_unresolved_operations(c.task)


@pytest.mark.parametrize("transient", [False, True])
def test_result_projection_only_retries_identified_storage_busy(monkeypatch, tmp_path, transient):
    import sqlite3
    from agent_adapters.web import task_result as projection
    calls = []
    def read(*args):
        calls.append(True)
        if transient and len(calls) == 2:return {"verified_facts": [{"fact_id": "saved"}], "errors": []}
        if transient:
            error = sqlite3.OperationalError("private storage detail")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        raise ValueError("private corrupt evidence detail")
    monkeypatch.setattr(projection, "_snapshot_v3_task_result", read)
    monkeypatch.setattr(projection.time, "sleep", lambda _: None)
    result = projection.snapshot_v3_task_result(tmp_path, dict(task_run_id="saved-run", mode="real"))
    assert len(calls) == (2 if transient else 1)
    if transient:assert result["verified_facts"] == [{"fact_id": "saved"}]
    else:
        assert result["errors"][0]["code"] == "state_read_failed"
        assert not result["verified_facts"] and "private" not in json.dumps(result)


@pytest.mark.parametrize("invalid", ["unavailable", "conflict", "version_changed", "unreliable"])
def test_next_direction_sources_still_require_effective_frozen_versions(invalid):
    from types import SimpleNamespace
    from agent_core.intent_execution import parse_resolution, validate_resolution
    items = [dict(ref=r, source_status="available", conflict=False, intent_id="intent", record_type="progress")
             for r in ("observed:v1", "next:v1")]
    if invalid == "unavailable":items[1]["source_status"] = "unavailable"
    if invalid == "conflict":items[1]["conflict"] = True
    request = dict(context=dict(intent_id="intent"), history_context=json.dumps(dict(
        items=items, intent_execution=dict(facts=[]))))
    raw = dict(kind="intent_resolution", outcome="inconclusive", reason="This bounded direction is closed.",
        evidence_refs=["observed:v1"], fact_ids=[], next_intent_candidates=[dict(
            description="Try a different direction.", intent_type=None, reason="Check the saved source.",
            evidence_refs=["next:v1"], dependency_refs=[])])
    live = [SimpleNamespace(**dict(i, ref="next:v2" if invalid == "version_changed" and i["ref"] == "next:v1" else i["ref"]))
            for i in items]
    app = SimpleNamespace(handoff=SimpleNamespace(history=lambda: live),
        revisions=SimpleNamespace(reliable=lambda item: invalid != "unreliable"))
    proposal = parse_resolution(raw, request)
    with pytest.raises(ValueError, match="d22_resolution_source_unavailable"):
        validate_resolution(app, request, proposal)


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_fact_support_reference_is_corrected_without_a_new_observation(http_case):
    c = http_case
    install(c, steps=1)
    original = c.models[1].invoke
    attempts = []
    def inner(envelope, *args):
        raw = original(envelope, *args)
        if isinstance(raw, dict) and raw.get("kind") == "intent_resolution":
            context = json.loads(envelope.request.history_context)
            attempts.append(envelope)
            if len(attempts) == 1:
                raw["evidence_refs"] = [next(i["ref"] for i in context["items"]
                    if i["record_id"] == c.app.state.executions[0].execution_id)]
            else:
                assert envelope.correction["reason"] == "d22_resolution_fact_not_effective"
                assert envelope.correction["issues"][0]["path"] == "$/fact_ids"
        return raw
    c.models[1].invoke = inner
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(attempts) == 2 and len(c.sends) == 1 and len(c.app.state.facts) == 1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_outer_selects_second_candidate_and_support_tracks_the_selected_subset(http_case):
    from agent_core.output_contracts import shape_issues
    from agent_core.intent_execution import parse_evaluation, selected_fact_refs
    from agent_core.handoff_contracts import D8HandoffRequest
    c = http_case
    install(c, steps=2)
    original = c.models[0].invoke
    evaluations = []
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":
            policy = json.loads(envelope.request.prior_summary)["s4_evaluation_policy"]
            assert policy["version"] == 3 and len(policy["support_fact_refs"]) == 2
            result = dict(decision="succeeded", reason="The second scoped observation suffices; first remains valid but is redundant.",
                          evidence_refs=[policy["support_fact_refs"][1]])
            assert not shape_issues(result, envelope.output_contract["schema"])
            assert parse_evaluation(envelope.request, result, policy) == result
            evaluations.append((envelope, result))
            return result
        return original(envelope, *args)
    c.models[0].invoke = outer
    assert asyncio.run(c.app.run(c.token)).stop_reason == "d9_awaiting_external_feedback"
    record = next(r for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "succeeded")
    request = D8HandoffRequest.from_dict(c.app.handoff.evidence.read("request", record.model_request_id))
    policy = json.loads(request.prior_summary)["s4_evaluation_policy"]
    from agent_core.intent_execution import evaluation_schema, observation_refs
    schema = evaluation_schema(request, policy)
    for decision, refs in [("succeeded", [policy["support_fact_refs"][1]]), ("failed", [observation_refs(request)[0]]), ("continue", [policy["goal_ref"]])]:
        sample = dict(decision=decision, reason="A meaningful scoped decision.", evidence_refs=refs)
        assert not shape_issues(sample, schema)
        assert parse_evaluation(request, sample, policy) == sample
    for decision in ("succeeded", "failed"):
        bad = dict(decision=decision, reason="Unsupported.", evidence_refs=[policy["goal_ref"]])
        assert shape_issues(bad, schema)
        with pytest.raises(ValueError):
            parse_evaluation(request, bad, policy)
    assert shape_issues(dict(decision="continue", reason="  ", evidence_refs=[policy["goal_ref"]]), schema)
    assert record.support_fact_id == c.app.state.facts[1].fact_id != request.support_fact_id
    assert len(c.app.state.facts) == 2 and selected_fact_refs(policy, evaluations[0][1]) == [policy["support_fact_refs"][1]]
    # Historical requests keep the old all-required contract; no stored evidence is rewritten.
    old = dict(policy, version=2)
    with pytest.raises(ValueError, match="success_support_required"):
        parse_evaluation(request, evaluations[0][1], old)
    from agent_adapters.deepseek_roles import build_payload
    system = build_payload(evaluations[0][0], c.app.config.outer_role)["messages"][0]["content"]
    assert "not mandatory citations" in system and "Succeeded must cite support_fact_ref" not in system


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("unknown", [False, True])
def test_failed_end_review_is_durable_pending_not_business_inconclusive(http_case, unknown):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c = http_case
    install(c, steps=1)
    original = c.models[0].invoke
    attempts = []
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":
            attempts.append(envelope)
            policy = json.loads(envelope.request.prior_summary)["s4_evaluation_policy"]
            return dict(decision="succeeded", reason="Incomplete support choice.", evidence_refs=[policy["goal_ref"]])
        return original(envelope, *args)
    c.models[0].invoke = outer
    first = asyncio.run(c.app.run(c.token))
    assert first.stop_reason == "d22_awaiting_adjudication" and first.recovery_required, first
    assert len(attempts) == 2 and len(c.sends) == len(c.app.state.facts) == 1
    assert attempts[1].correction["issues"][0]["path"] == "$/evidence_refs"
    assert c.app.state.intents[0].status.value == "running"
    assert not any(r.kind in {"failed", "inconclusive", "succeeded", "turn_exhausted"} for r in c.app.store.list_d8_tree_records(c.task))
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    assert snapshot_v3_task_result(c.directory, c.declaration)["recovery"]["reason"] == "d22_awaiting_adjudication"
    if unknown:
        from uuid import uuid4
        from agent_core.operation_accounting import D4OperationStart
        c.app.store.begin_d4_operation(D4OperationStart(str(uuid4()), c.task, "action", c.clock(),
            c.app.state.latest_checkpoint.sequence, "0" * 64, "controlled-unknown-call", c.app.config.d4_budget.budget_policy_ref,
            "inner_action", c.app.config.inner_role.role_config_ref))
    before = len(c.app.store.list_d4_operations(c.task))
    c.app.store.close()
    c.app = c.create()
    second = asyncio.run(c.app.resume(c.task, c.token))
    assert second.stop_reason == ("d4_operation_outcome_unknown" if unknown else "d22_awaiting_adjudication"), second
    assert second.recovery_required
    assert len(c.app.store.list_d4_operations(c.task)) == before and len(attempts) == 2 and len(c.sends) == 1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
@pytest.mark.parametrize("crash", [False, True])
def test_working_observation_skips_fact_review_and_recovers_committed_progress(http_case, monkeypatch, crash):
    import agent_core.intent_execution as execution
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    install(c, steps=2)
    original_inner, original_outer = c.models[1].invoke, c.models[0].invoke
    notes, fact_reviews = [], []
    def inner(envelope, *args):
        if envelope.output_contract["stage"] == "fact_proposal" and len(c.sends) == 1:
            notes.append(envelope.request.model_request_id)
            return dict(kind="working_record", content="Unreviewed observation: inspect another candidate; this is not a conclusion.",
                        evidence_refs=[envelope.request.observation.proof_id])
        return original_inner(envelope, *args)
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "fact_review":
            fact_reviews.append(envelope)
        return original_outer(envelope, *args)
    c.models[1].invoke, c.models[0].invoke = inner, outer
    if crash:
        apply = execution.apply_working
        def lost(*args):
            raise ProcessLoss("observed committed before working application marker")
        monkeypatch.setattr(execution, "apply_working", lost)
        with pytest.raises(ProcessLoss):
            asyncio.run(c.app.run(c.token))
        assert len(c.sends) == 1 and not c.app.state.facts
        monkeypatch.setattr(execution, "apply_working", apply)
        c.app.store.close()
        c.app = c.create()
        result = asyncio.run(c.app.resume(c.task, c.token))
    else:
        result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 2 and len(fact_reviews) == len(c.app.state.facts) == len(notes) == 1
    first_plan = c.app.state.plans[0]
    assert c.app.store.load_d3_plan_result(first_plan.plan_id).outcome == "observed"
    assert c.app.store.load_d4_operation(notes[0]).application_ref is not None
    history = c.app.handoff.history()
    note = next(i for i in history if i.identity == "unreviewed_working_record")
    assert note.record_type == "progress"
    catalog = c.app.evidence_conclusions.catalog(c.app.state.intents[0].intent_id)
    assert note.ref not in {s["ref"] for s in catalog}
    from agent_core.evidence_conclusions import EvidenceCandidate
    from types import SimpleNamespace
    with pytest.raises(ValueError):
        EvidenceCandidate("An unsupported conclusion.", "Work record only.", (note.ref,), False)
    candidate = EvidenceCandidate("An unsupported conclusion.", "Work record only.", (note.record_id.removeprefix("decision/"),), False)
    request = SimpleNamespace(history_context=json.dumps({"evidence_sources": catalog}), context=SimpleNamespace(intent_id=c.app.state.intents[0].intent_id))
    with pytest.raises(ValueError):
        c.app.evidence_conclusions.validate_candidate(request, candidate)
    assert not any(r.outcome == "reviewed_inference" for r in c.app.store.list_d8_conclusions(c.task))


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_inner_working_record_continues_same_intent_without_new_tool_or_fact_review(http_case):
    c = http_case
    install(c, steps=1)
    inner, outer = c.models[1].invoke, c.models[0].invoke
    notes, fact_reviews = [], []
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            packet = json.loads(envelope.request.history_context)
            if packet["intent_execution"]["facts"] and not notes:
                notes.append(envelope.request.model_request_id)
                return dict(kind="working_record", content="Hypothesis: the observed response may be a shared handler; not verified.",
                    evidence_refs=[packet["intent_execution"]["facts"][0]["ref"]])
        return inner(envelope, *args)
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "fact_review":fact_reviews.append(envelope)
        return outer(envelope, *args)
    c.models[1].invoke, c.models[0].invoke = choose, review
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == len(c.app.state.facts) == len(notes) == len(fact_reviews) == 1
    row = c.app.store.load_d4_operation(notes[0])
    assert row.application_ref and row.completion
    assert any(i.identity == "unreviewed_working_record" for i in c.app.handoff.history())


from test_output import output_case


@pytest.mark.parametrize("output_case", ["rolling"], indirect=True)
def test_attachment_observation_can_remain_a_working_record_without_fact_review(output_case):
    c = output_case
    def choose(envelope, raw):
        if envelope.output_contract["stage"] == "fact_proposal":
            return dict(kind="working_record", content="Unreviewed copy observation; end this exploration without a durable conclusion.",
                        evidence_refs=[envelope.request.observation.proof_id])
        if envelope.output_contract["stage"] == "inner_plan" and c.app.state.plans:
            context = json.loads(envelope.request.history_context)
            return dict(kind="intent_resolution", outcome="inconclusive", reason="This exploratory direction is closed within its tested scope.",
                evidence_refs=[i["ref"] for i in context["items"]], fact_ids=[], next_intent_candidates=[])
        return raw
    def review(envelope, raw):
        if envelope.output_contract["stage"] == "intent_evaluation":
            return dict(decision="inconclusive", reason="Legitimate bounded strategy closure, no impossibility claim.",
                        evidence_refs=[i.ref for i in envelope.request.history])
        return raw
    original_inner = c.models["inner"].invoke
    def invoke_inner(envelope, *args):
        if envelope.output_contract["stage"] == "fact_proposal" or envelope.output_contract["stage"] == "inner_plan" and c.app.state.plans:
            c.models["inner"].calls.append(envelope)
            return choose(envelope, {})
        return original_inner(envelope, *args)
    c.models["inner"].invoke = invoke_inner
    original_handoff = c.models["handoff"].invoke
    def invoke_handoff(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":
            c.models["handoff"].calls.append(envelope)
            return review(envelope, {})
        return original_handoff(envelope, *args)
    c.models["handoff"].invoke = invoke_handoff
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", (result.stop_reason, [(e.request.stage, type(getattr(e.request, "context", None)).__name__) for e in c.models["inner"].calls], [(o.start.role, o.start.operation_id) for o in c.app.store.list_d4_unresolved_operations(c.task)])
    assert not c.app.state.facts and not c.models["fact"].calls
    assert len(c.app.state.plans) == 1
    observed = c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id)
    assert observed.outcome == "observed" and c.app.proof_store.read(observed.proof_id)
    assert any(i.identity == "unreviewed_working_record" for i in c.app.handoff.history())


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_mcp_result_working_record_and_legal_end_without_formal_fact(http_case):
    from test_mcp_link import Tools
    from agent_adapters.mcp_plan_executor import MCPPlanExecutor
    c = http_case
    tools = Tools(c.task, c.app.state.task_run.sandbox_id)
    c.app.mcp = MCPPlanExecutor(c.app, tools)
    c.app.mcp.authorize(c.task)
    original_outer = c.models[0].invoke
    notes = []
    def choose(envelope, *args):
        if envelope.output_contract["stage"] != "inner_plan":
            raise AssertionError(envelope.output_contract["stage"])
        context = json.loads(envelope.request.history_context)
        if not context.get("evidence_sources"):
            return dict(kind="mcp_call", tool_name="shell.run_bash", arguments=dict(script="printf test"))
        if not notes:
            source = next(i for i in context["items"] if i["identity"] == "historical_decision_not_verified")
            notes.append(envelope.request.model_request_id)
            return dict(kind="working_record", content="Unreviewed command result; no formal conclusion is warranted.", evidence_refs=[source["ref"]])
        return dict(kind="intent_resolution", outcome="inconclusive", reason="Exploratory direction closed within observed scope.",
            evidence_refs=[i["ref"] for i in context["items"]], fact_ids=[], next_intent_candidates=[])
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":
            return dict(decision="inconclusive", reason="Legitimate strategic closure, not a technical verdict.",
                        evidence_refs=[i.ref for i in envelope.request.history])
        assert envelope.output_contract["stage"] != "fact_review"
        return original_outer(envelope, *args)
    c.models[1].invoke, c.models[0].invoke = choose, review
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert len(tools.calls) == len(notes) == 1 and not c.sends and not c.app.state.facts
    assert c.app.state.plans == () and c.app.state.executions == ()
    assert any(i.identity == "unreviewed_working_record" for i in c.app.handoff.history())


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_next_turn_required_history_uses_only_outer_selected_fact_support(http_case, monkeypatch):
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    install(c, steps=3)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    selected, candidates = [], []
    def choose_outer(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":
            policy = json.loads(envelope.request.prior_summary)["s4_evaluation_policy"]
            candidates.extend(policy["support_fact_refs"])
            selected.extend(policy["support_fact_refs"][1:])
            return dict(decision="succeeded", reason="The last two scoped observations jointly cover the goal.", evidence_refs=selected)
        if envelope.output_contract["stage"] == "tree_planning" and envelope.request.turn_id:
            return dict(kind="candidates", intents=[dict(description="Explore an independent next direction.", intent_type=None, dependency_refs=[])])
        return outer(envelope, *args)
    def choose_inner(envelope, *args):
        if envelope.output_contract["stage"] == "inner_candidate":return dict(action=dict(kind="no_candidate"))
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = choose_outer, choose_inner
    new_plan = c.app._new_plan
    async def stop_at_child(intent_id, *args, **kwargs):
        if len(c.app.state.turns) == 2:
            raise ProcessLoss("child registered; inspect its required support before execution")
        return await new_plan(intent_id, *args, **kwargs)
    monkeypatch.setattr(c.app, "_new_plan", stop_at_child)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    child = c.app.state.turns[-1]
    _, _, context = c.app.handoff.select("execution", child.turn_id, child.intent_ids[0])
    assert len(selected) == 2 and set(selected) <= set(context["required_refs"])
    assert candidates[0] not in context["required_refs"]
    record = next(r for r in c.app.store.list_d8_tree_records(c.task) if r.kind == "succeeded")
    from agent_core.intent_execution import result_summary
    assert result_summary(c.app, record)["evidence_refs"] == selected


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_specific_business_rejection_is_bound_and_reaches_the_one_correction(http_case, monkeypatch):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c = http_case
    install(c, steps=1)
    parse, invoke = c.app.handoff._parse, c.models[0].invoke
    rejected, attempts = [], []
    def reject_once(request, raw):
        if request.stage == "intent_evaluation" and not rejected:
            rejected.append(request.model_request_id)
            raise ValueError("d22_resolution_success_support_required")
        return parse(request, raw)
    def outer(envelope, *args):
        if envelope.output_contract["stage"] == "intent_evaluation":attempts.append(envelope)
        return invoke(envelope, *args)
    monkeypatch.setattr(c.app.handoff, "_parse", reject_once)
    c.models[0].invoke = outer
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(attempts) == 2 and len(c.sends) == 1
    issue = attempts[1].correction["issues"][0]
    assert issue == dict(code="d22_resolution_success_support_required", path="$/evidence_refs", expected="at_least_one_candidate_fact", actual="rejected")
    diagnostic = c.app.evaluation_bindings.read(rejected[0], check_operation=True)
    assert diagnostic["diagnostic_version"] == 2 and diagnostic["result"]["raw"] is None
    projected = snapshot_v3_task_result(c.directory, c.declaration)
    assert not projected["recovery"]["required"], projected
    assert any(row["stage"] == "intent_evaluation" and row["diagnostic_version"] == 2 and row["issues"] == [issue] for row in projected["business_diagnostics"])
