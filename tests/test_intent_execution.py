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
                if (case.app.config.intent_execution_policy == "continuous_v1" and stop == "succeeded"
                        and not no_fact and not closure and not context["intent_execution"]["facts"]):
                    return dict(kind="fact_candidate", content="The bounded original observation supports this Intent.",
                        reason="Review original evidence before resolving the Intent.",
                        evidence_refs=[context["evidence_sources"][-1]["ref"]], inferred=False)
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
    raw["d8_config"]["intent_execution_policy"] = "rolling_v1"  # This test exercises the original finite runner.
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
    raw["d8_config"]["intent_execution_policy"] = "rolling_v1"  # This test exercises the original finite runner.
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


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_meaningful_progress_can_request_goal_resolution_without_forced_failure(http_case):
    c = http_case
    calls = install(c, steps=100)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    requested = []
    def review(envelope, *args):
        if envelope.output_contract["stage"] == "progress_review":
            context = json.loads(envelope.request.context_summary)
            requested.append(envelope)
            return dict(decision="request_intent_resolution", rationale="The local goal is already covered; another request would explore a different goal.",
                source_ids=list(envelope.request.source_versions), constraints=[],
                progress=dict(classification="meaningful", reason="Useful observations suffice for the current bounded goal.",
                              evidence_refs=[context["d8_progress"]["recent"][-1]["ref"]]))
        return outer(envelope, *args)
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            context = json.loads(envelope.request.history_context)
            stop = context["intent_execution"].get("stop_requested")
            if stop and stop["decision"] == "request_intent_resolution":
                return dict(kind="intent_resolution", outcome="succeeded", reason="Saved formal observations cover this local goal.",
                    evidence_refs=[i["ref"] for i in context["items"]],
                    fact_ids=[i["fact_id"] for i in context["intent_execution"]["facts"]], next_intent_candidates=[])
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d9_awaiting_external_feedback", result
    assert len(c.sends) == 2 and len(requested) == calls["reviews"] == 1
    assert c.app.state.intents[0].status.value == "succeeded"
    assert any(r.decision == "request_intent_resolution" for r in c.app.store.list_d3_review_applications(c.task))

    from agent_adapters.web.task_result import snapshot_v3_task_result
    assert not snapshot_v3_task_result(c.directory,c.declaration)["recovery"]["required"]


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_long_intent_reaches_bounded_budget_closure_after_more_than_eight_reviews(http_case):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c = http_case
    install(c, steps=100, no_fact=True, stop="inconclusive")
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "d22_closure_completed_budget_stop", result
    assert c.app.review_ledger.review_call_count(c.task) > 8 and len(c.sends) > 16
    assert c.app.state.intents[0].status.value == "inconclusive" and not c.app.state.facts
    usage = c.app.store.d4_budget_usage(c.task)
    assert usage.total_used <= c.app.config.d4_budget.total
    assert all(n <= c.app.config.d4_budget.category_limits[k] for k, n in usage.category_counts)
    before = len(c.app.store.list_d4_operations(c.task)), len(c.sends)
    assert not c.app.store.list_d4_unresolved_operations(c.task)
    projected = snapshot_v3_task_result(c.directory, c.declaration)
    assert projected["recovery"]["reason"] == "d22_closure_completed_budget_stop", projected
    c.app.store.close()
    c.app = c.create()
    resumed = asyncio.run(c.app.resume(c.task, c.token))
    assert resumed.stop_reason == result.stop_reason, resumed
    assert (len(c.app.store.list_d4_operations(c.task)), len(c.sends)) == before


@pytest.mark.parametrize("available,has_fact,source,closing", [(6,True,False,False),(5,True,False,True),(8,False,True,False),(6,False,False,True),(7,False,True,True),(4,False,True,True)])
def test_budget_reserve_boundary_keeps_one_bounded_end_path(monkeypatch,available,has_fact,source,closing):
    from types import SimpleNamespace
    import agent_core.intent_execution as execution
    from agent_core.operation_accounting import D8HandoffBudgetPolicy
    saved = {}
    evidence = SimpleNamespace(read=lambda kind, identity: saved.get(identity), save=lambda kind, identity, value: saved.setdefault(identity,value))
    budget = D8HandoffBudgetPolicy()
    counts = {"action": budget.action - available}
    app = SimpleNamespace(config=SimpleNamespace(intent_execution_policy="rolling_v1",d4_budget=budget,authorization=SimpleNamespace(attachment_indices=())),
        state=SimpleNamespace(task_run=SimpleNamespace(task_run_id="controlled"),latest_checkpoint=SimpleNamespace(sequence=1),plans=[]),
        handoff=SimpleNamespace(evidence=evidence),web=object(),_id=lambda name:name,
        sessions=SimpleNamespace(records=lambda _:[]),
        evidence_conclusions=SimpleNamespace(catalog=lambda _: ["source"] if source else []),
        store=SimpleNamespace(d4_budget_usage=lambda _:SimpleNamespace(category_counts=tuple(counts.items())),
            list_d4_operations=lambda _:[],list_d8_conclusions=lambda _:[],list_d3_review_applications=lambda _:[]))
    monkeypatch.setattr(execution,"facts",lambda *args:["fact"] if has_fact else [])
    required=execution.closure_requirements(app,"intent")
    assert required=={"action":2 if has_fact or not source else 4,"review":2,"handoff_summary":2,**({"fact_review":2} if source and not has_fact else {})}
    execution.prepare_closure(app,"intent")
    assert bool(execution.closure_marker(app,"intent")) is closing
    if closing:
        stop=execution.stop_request(app,"intent")
        assert stop["decision"]=="request_intent_resolution"
        assert stop["history_reads_allowed"] is (available >= required["action"] + 2)
        # Closure remains latched even if a formal Fact subsequently reduces the reserve requirement.
        monkeypatch.setattr(execution,"facts",lambda *args:["new-fact"])
        execution.prepare_closure(app,"intent")
        assert execution.closure_marker(app,"intent")


def test_insufficient_closure_budget_pauses_without_inventing_an_intent_result(monkeypatch):
    from types import SimpleNamespace
    import agent_core.intent_execution as execution
    saved={}
    app=SimpleNamespace(config=SimpleNamespace(intent_execution_policy="rolling_v1"),
        state=SimpleNamespace(task_run=SimpleNamespace(task_run_id="controlled")),
        handoff=SimpleNamespace(evidence=SimpleNamespace(save=lambda kind,identity,value:saved.setdefault(identity,value))),
        store=SimpleNamespace(list_d4_operations=lambda _:[]),_id=lambda name:name)
    monkeypatch.setattr(execution,"closure_marker",lambda *args:None)
    monkeypatch.setattr(execution,"closure_requirements",lambda *args:dict(action=2,review=2,handoff_summary=2))
    monkeypatch.setattr(execution,"remaining_budget",lambda *args:dict(action=1,review=2,handoff_summary=2))
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    with pytest.raises(D3RecoveryRequired,match="d22_closure_budget_unavailable"):
        execution.prepare_closure(app,"intent")
    assert saved=={"budget-stop/intent":{"intent_id":"intent","reason":"d22_closure_budget_unavailable"}}


@pytest.mark.parametrize("http_case",["rolling"],indirect=True)
def test_budget_closure_continue_pauses_instead_of_forcing_a_business_end(http_case,monkeypatch):
    import agent_core.intent_execution as execution
    c=http_case
    calls=install(c,steps=100,continue_once=True)
    prepare=execution.prepare_closure
    def at_boundary(app,intent_id):
        if c.sends and execution.closure_marker(app,intent_id) is None:
            app.handoff.evidence.save("pause",app._id("budget-closure/"+intent_id),{"intent_id":intent_id,"checkpoint_sequence":app.state.latest_checkpoint.sequence})
        prepare(app,intent_id)
    monkeypatch.setattr(execution,"prepare_closure",at_boundary)
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="d22_closure_requires_more_budget",result
    assert len(c.sends)==len(c.app.state.facts)==calls["reviews"]==1
    assert c.app.state.intents[0].status.value=="running"
    assert not any(r.kind in {"succeeded","failed","inconclusive"} for r in c.app.store.list_d8_tree_records(c.task))
    before=len(c.app.store.list_d4_operations(c.task))
    c.app.store.close();c.app=c.create()
    assert asyncio.run(c.app.resume(c.task,c.token)).stop_reason==result.stop_reason
    assert len(c.app.store.list_d4_operations(c.task))==before and len(c.sends)==1


@pytest.mark.parametrize("http_case",["rolling"],indirect=True)
@pytest.mark.parametrize("accept",[True,False])
def test_closure_can_nominate_one_formal_fact_from_saved_evidence(http_case,monkeypatch,accept):
    import agent_core.intent_execution as execution
    from agent_core.evidence_conclusions import EvidenceReviewRequest
    c=http_case
    install(c,steps=100,no_fact=True,stop="inconclusive")
    prepare=execution.prepare_closure
    def at_boundary(app,intent_id):
        if c.sends and execution.closure_marker(app,intent_id) is None:
            app.handoff.evidence.save("pause",app._id("budget-closure/"+intent_id),{"intent_id":intent_id,"checkpoint_sequence":app.state.latest_checkpoint.sequence})
        prepare(app,intent_id)
    monkeypatch.setattr(execution,"prepare_closure",at_boundary)
    inner,outer=c.models[1].invoke,c.models[0].invoke
    candidates=[]
    def choose(envelope,*args):
        if envelope.output_contract["stage"]=="inner_plan":
            context=json.loads(envelope.request.history_context)
            stop=context["intent_execution"].get("stop_requested")
            if stop:
                if stop.get("fact_candidate_allowed"):
                    candidates.append(envelope)
                    return dict(kind="fact_candidate",content="The recorded request returned a scoped HTTP observation.",reason="Use the original saved request and response.",evidence_refs=[context["evidence_sources"][0]["ref"]],inferred=False)
                return dict(kind="intent_resolution",outcome="succeeded" if accept else "inconclusive",reason="The accepted scoped evidence suffices." if accept else "The bounded exploration has unresolved support and is strategically closed.",evidence_refs=[i["ref"] for i in context["items"]],fact_ids=[i["fact_id"] for i in context["intent_execution"]["facts"]],next_intent_candidates=[])
        return inner(envelope,*args)
    def review(envelope,*args):
        if isinstance(envelope.request,EvidenceReviewRequest):return dict(decision="accept" if accept else "reject",reason="Controlled conclusion review.")
        if envelope.output_contract["stage"]=="intent_evaluation":return dict(decision="succeeded" if accept else "inconclusive",reason="The bounded reviewed scope supports this disposition.",evidence_refs=[i.ref for i in envelope.request.history])
        return outer(envelope,*args)
    c.models[1].invoke,c.models[0].invoke=choose,review
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="d22_closure_completed_budget_stop",result
    assert len(c.sends)==len(candidates)==1 and len(c.app.state.facts)==int(accept)
    assert c.app.state.intents[0].status.value==("succeeded" if accept else "inconclusive")


@pytest.mark.parametrize("output_case",["rolling"],indirect=True)
def test_goal_resolution_request_finishes_the_admitted_file_plan_before_handoff(output_case):
    c=output_case
    inner=c.models["inner"].invoke;handoff=c.models["handoff"].invoke
    notes=[];reviews=[]
    def choose(envelope,*args):
        if envelope.output_contract["stage"]=="inner_plan":
            context=json.loads(envelope.request.history_context)
            stop=context["intent_execution"].get("stop_requested")
            if stop:
                c.models["inner"].calls.append(envelope)
                return dict(kind="intent_resolution",outcome="inconclusive",reason="The requested scoped end summary retains the completed observation.",evidence_refs=[i["ref"] for i in context["items"]],fact_ids=[],next_intent_candidates=[])
            if len(notes)<2:
                notes.append(envelope.request.model_request_id)
                c.models["inner"].calls.append(envelope)
                return dict(kind="working_record",content="Unreviewed preparation hypothesis.",evidence_refs=[context["items"][0]["ref"]])
        return inner(envelope,*args)
    def progress(envelope,raw):
        if envelope.output_contract["stage"]=="progress_review":
            reviews.append(envelope)
            return {**raw,"decision":"request_intent_resolution","constraints":[],"progress":{**raw["progress"],"classification":"meaningful","reason":"Collect an end proposal after the admitted observation finishes."}}
        return raw
    def end(envelope,*args):
        if envelope.output_contract["stage"]=="intent_evaluation":return dict(decision="inconclusive",reason="Legitimate bounded closure retaining the saved observation.",evidence_refs=[i.ref for i in envelope.request.history])
        return handoff(envelope,*args)
    c.models["inner"].invoke=choose;c.models["review"].transform=progress;c.models["handoff"].invoke=end
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="search_exhausted",result
    assert len(reviews)==1 and len(c.app.state.plans)==1 and len(c.app.state.executions)==2
    assert c.app.store.load_d3_plan_result(c.app.state.plans[0].plan_id).outcome=="observed"
    assert not c.app.state.facts and not c.models["fact"].calls


@pytest.mark.parametrize("accepted",[True,False])
def test_known_current_decision_is_reconciled_before_recomputing_budget_reserve(monkeypatch,accepted):
    from types import SimpleNamespace
    import agent_core.intent_execution as execution
    call=SimpleNamespace(completion=object(),application_ref=None,start=SimpleNamespace(operation_id="known",role="inner_action",base_checkpoint_sequence=7))
    app=SimpleNamespace(config=SimpleNamespace(intent_execution_policy="rolling_v1"),state=SimpleNamespace(task_run=SimpleNamespace(task_run_id="controlled"),latest_checkpoint=SimpleNamespace(sequence=7)),
        store=SimpleNamespace(list_d4_operations=lambda _:[call]),
        sessions=SimpleNamespace(evidence=SimpleNamespace(read=lambda *args:{"intent_id":"intent","request":{"stage":"inner_plan"}})),
        model_evidence=SimpleNamespace(read=lambda _:SimpleNamespace(outcome="accepted" if accepted else "invalid_structure")))
    monkeypatch.setattr(execution,"closure_requirements",lambda *args:pytest.fail("known result must reach its original bounded recovery before a fresh reserve gate"))
    execution.prepare_closure(app,"intent")


@pytest.mark.parametrize("http_case",["rolling"],indirect=True)
def test_budget_summary_commit_recovers_without_redispatch(http_case,monkeypatch):
    import agent_core.intent_execution as execution
    from test_chain_multi_turn import ProcessLoss
    c=http_case
    calls=install(c,steps=100,no_fact=True,stop="inconclusive")
    prepare=execution.prepare_closure
    def at_boundary(app,intent_id):
        if c.sends and execution.closure_marker(app,intent_id) is None:
            app.handoff.evidence.save("pause",app._id("budget-closure/"+intent_id),{"intent_id":intent_id,"checkpoint_sequence":app.state.latest_checkpoint.sequence})
        prepare(app,intent_id)
    monkeypatch.setattr(execution,"prepare_closure",at_boundary)
    summarize=c.app.handoff.summarize
    async def lost(turn_id,token):
        result=await summarize(turn_id,token)
        if any(r.kind=="inconclusive" for r in c.app.store.list_d8_tree_records(c.task)):
            raise ProcessLoss("summary committed before resource pause receipt")
        return result
    monkeypatch.setattr(c.app.handoff,"summarize",lost)
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    assert c.app.state.intents[0].status.value=="inconclusive"
    before=len(c.app.store.list_d4_operations(c.task)),len(c.sends)
    c.app.store.close();c.app=c.create()
    result=asyncio.run(c.app.resume(c.task,c.token))
    assert result.stop_reason=="d22_closure_completed_budget_stop",result
    assert (len(c.app.store.list_d4_operations(c.task)),len(c.sends))==before
    assert calls["reviews"]==1


@pytest.mark.parametrize("http_case", ["rolling"], indirect=True)
def test_replan_progress_cannot_skip_summary_before_next_inner_request(http_case):
    c = http_case
    install(c, steps=5, stop="inconclusive", no_fact=True)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    seen = {"reviews": 0, "summaries": 0, "compacted": 0}
    def review(envelope, *args):
        raw = outer(envelope, *args)
        if envelope.output_contract["stage"] == "progress_review":
            seen["reviews"] += 1
            return {**raw, "decision": "replan_current_intent",
                    "progress": {**raw["progress"], "classification": "repetitive"}}
        if envelope.output_contract["stage"] == "handoff_summary":seen["summaries"] += 1
        return raw
    def choose(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan" and seen["reviews"]:
            assert seen["summaries"] >= seen["reviews"]
            session = json.loads(envelope.request.session_context)
            if "compaction" in session:
                seen["compacted"] += 1
                assert session["compaction"]["raw_decisions_retained"]
                assert len(session["messages"]) <= 12
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = review, choose
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "search_exhausted", result
    assert seen["reviews"] >= 2 and seen["compacted"] >= 1
    assert len(c.sends) == 5 and len(c.app.sessions.records()) >= 10


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_technical_siblings_get_one_neighbor_revisit_and_same_session(http_case):
    from agent_core.output_contracts import D8ModelResponse
    c = http_case
    outer, inner = c.models[0].invoke, c.models[1].invoke
    calls, sessions = [], {}
    roots = []
    def plan(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and envelope.request.turn_id is None:
            roots.append(envelope)
            if len(roots) > 1:return dict(kind="exhausted", reason="No new authorized route remains.", evidence_refs=[])
            return dict(kind="candidates", intents=[dict(description="Independent approach "+str(n), intent_type=None, dependency_refs=[]) for n in range(3)])
        return outer(envelope, *args)
    def fail(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            actor = envelope.request.context.intent_id
            packet = json.loads(envelope.request.session_context)
            sessions.setdefault(actor, set()).add(packet["session_id"])
            calls.append((actor, envelope.request.model_request_id, envelope.request.correction_of_model_request_id))
            return D8ModelResponse("{}", "stop")
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = plan, fail
    waits = []
    run_once = c.app._run_once
    results = []
    async def observe_once(token):
        result = await run_once(token)
        results.append(result.stop_reason)
        return result
    c.app._run_once = observe_once
    async def end_at_deadline(token):
        waits.append([(i.description, i.status.value) for i in c.app.state.intents])
        c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = end_at_deadline
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "task_run_deadline_exceeded", result
    ids = [i.intent_id for i in sorted(c.app.state.intents, key=lambda i: i.priority)]
    attempts = [calls[n][0] for n in range(0, len(calls), 2)]
    assert attempts == [ids[0], ids[1], ids[0], ids[2], ids[1]], (attempts, results, waits, [(r.kind,r.intent_id) for r in c.app.store.list_d8_tree_records(c.task)])
    assert len(calls) == 10 and len({i[1] for i in calls}) == 10
    assert all(calls[n+1][2] == calls[n][1] for n in range(0, len(calls), 2))
    assert all(len(values) == 1 for values in sessions.values())
    assert len(roots) == 2 and waits and not c.sends
    assert all(i.status.value == "suspended" for i in c.app.state.intents)
    rows = c.app.store.list_d8_tree_records(c.task)
    assert sum(r.kind == "resumed" for r in rows) == 2
    assert not any(r.kind in {"failed", "inconclusive"} for r in rows)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_one_continuation_root_preserves_original_declarations(http_case):
    from agent_core.output_contracts import D8ModelResponse
    c = http_case
    outer = c.models[0].invoke
    roots = []
    def plan(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and envelope.request.turn_id is None:
            roots.append(envelope.request)
            return dict(kind="candidates", intents=[dict(description="Independent root approach " + str(len(roots)), intent_type=None, dependency_refs=[])])
        return outer(envelope, *args)
    c.models[0].invoke = plan
    c.models[1].invoke = lambda *args: D8ModelResponse("{}", "stop")
    waiting = []
    async def finish(token):
        waiting.append(True)
        c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish
    result = asyncio.run(c.app.run(c.token))
    assert result.stop_reason == "task_run_deadline_exceeded"
    assert len(roots) == 2 and waiting
    turns = sorted(c.app.state.turns, key=lambda t: t.sequence)
    assert len(turns) == 2 and all(t.parent_turn_id is None for t in turns)
    assert turns[0].intent_ids != turns[1].intent_ids
    assert len(turns[0].intent_ids) == len(turns[1].intent_ids) == 1
    assert all(t.turn_target == c.app.state.task_run.request.goal for t in turns)
    assert all(i.status.value == "suspended" for i in c.app.state.intents)
    assert not c.sends


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("stage", ["inner_plan", "review", "intent_evaluation"])
@pytest.mark.parametrize("failure", ["http_503", "timeout", "worker_lost"])
def test_known_transient_provider_response_shares_one_retry(http_case, stage, failure):
    from agent_core.modeling import ModelError, ModelErrorKind
    from agent_core.review_contracts import D2ModelFailure
    from agent_core.output_contracts import D8ModelResponse
    c = http_case
    install(c, steps=1, stop="failed", no_fact=True)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    calls = []
    def invoke(original, envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and c.app.state.turns:
            return dict(kind="exhausted", reason="No further route.", evidence_refs=[])
        if envelope.output_contract["stage"] == stage:
            request = envelope.request
            identity = getattr(request, "model_request_id", None) or request.review_id
            calls.append(identity)
            transport = ({"transport_end": "http_error", "http_status": 503} if failure == "http_503"
                         else {"transport_end": failure, "http_status": 200, "worker_exit_confirmed": True})
            c.app.output_boundary.evidence.save("provider-result", identity,
                {"transport": transport})
            return (D2ModelFailure("provider_failure", failure) if stage == "review"
                    else ModelError(ModelErrorKind.TIMED_OUT if failure == "timeout" else ModelErrorKind.PROVIDER_FAILURE, failure))
        return original(envelope, *args)
    c.models[0].invoke = lambda envelope,*args:invoke(outer,envelope,*args)
    c.models[1].invoke = lambda envelope,*args:invoke(inner,envelope,*args)
    reasons = []
    once = c.app._run_once
    async def capture(token):
        result = await once(token)
        reasons.append(result.stop_reason)
        return result
    c.app._run_once = capture
    async def finish(token):c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish
    asyncio.run(c.app.run(c.token))
    assert len(calls) == 2 and len(set(calls)) == 2, (stage, calls, reasons)
    assert c.app.state.intents[0].status.value == "suspended", (stage, reasons)
    assert len(c.sends) == (1 if stage == "intent_evaluation" else 0)


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("fault", ["timeout", "unconfirmed_exit", "payment"])
def test_unknown_model_result_blocks_siblings_without_redispatch(http_case, fault):
    from agent_core.modeling import ModelError, ModelErrorKind
    c = http_case
    outer = c.models[0].invoke
    def plan(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning":
            return dict(kind="candidates", intents=[dict(description="Unknown approach " + str(n), intent_type=None, dependency_refs=[]) for n in range(2)])
        return outer(envelope,*args)
    calls = []
    def unknown(envelope,*args):
        calls.append(envelope.request.model_request_id)
        if fault == "payment":
            c.app.output_boundary.evidence.save("provider-result", envelope.request.model_request_id,
                {"transport":{"transport_end":"http_error","http_status":402}})
            return ModelError(ModelErrorKind.PROVIDER_FAILURE,"deepseek_http_402")
        if fault == "unconfirmed_exit":
            c.app.output_boundary.evidence.save("provider-result", envelope.request.model_request_id,
                {"transport": {"transport_end": "timeout", "worker_exit_confirmed": False}})
        return ModelError(ModelErrorKind.TIMED_OUT,"transport_timeout")
    c.models[0].invoke, c.models[1].invoke = plan, unknown
    waits = []
    async def finish(token):
        waits.append(True)
        if len(waits) == 2:c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish
    result = asyncio.run(c.app.run(c.token))
    assert len(calls) == 1 and len(waits) == 2
    assert not c.sends and not any(r.kind in {"suspended","resumed"} for r in c.app.store.list_d8_tree_records(c.task))
    assert result.stop_reason == "task_run_deadline_exceeded"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_confirmed_model_timeout_suspends_and_explores_sibling(http_case):
    from agent_core.modeling import ModelError, ModelErrorKind
    c = http_case
    install(c, steps=1, stop="failed", no_fact=True)
    outer, inner = c.models[0].invoke, c.models[1].invoke
    calls = []
    def plan(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning":
            return (dict(kind="exhausted", reason="No further route.", evidence_refs=[]) if c.app.state.turns else
                    dict(kind="candidates", intents=[dict(description=name, intent_type=None, dependency_refs=[])
                         for name in ("Technical route", "Independent sibling")]))
        return outer(envelope, *args)
    def execute(envelope, *args):
        if envelope.output_contract["stage"] == "inner_plan":
            owner = next(i for i in c.app.state.intents if i.intent_id == envelope.request.context.intent_id)
            calls.append(owner.description)
            if owner.description == "Technical route":
                c.app.output_boundary.evidence.save("provider-result", envelope.request.model_request_id,
                    {"transport": {"transport_end": "timeout", "worker_exit_confirmed": True}})
                return ModelError(ModelErrorKind.TIMED_OUT, "model_total_timeout")
        return inner(envelope, *args)
    c.models[0].invoke, c.models[1].invoke = plan, execute
    async def finish(token):c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish
    asyncio.run(c.app.run(c.token))
    assert calls[:3] == ["Technical route", "Technical route", "Independent sibling"]
    assert calls.count("Technical route") == 4  # One correction, then one sibling-triggered revisit.
    assert c.sends and c.app.state.intents[0].status.value == "suspended"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
@pytest.mark.parametrize("cleanup_unknown", [False, True])
def test_deadline_at_wait_boundary_commits_terminal_before_resource_cleanup(http_case, cleanup_unknown):
    c = http_case
    install(c, steps=1, stop="failed", no_fact=True)
    outer = c.models[0].invoke
    def plan(envelope, *args):
        if envelope.output_contract["stage"] == "tree_planning" and c.app.state.turns:
            return dict(kind="exhausted", reason="No further route.", evidence_refs=[])
        return outer(envelope, *args)
    c.models[0].invoke = plan
    removed = []
    def destroy(task, binding):
        assert c.app.store.load_task_run(task).status.value == "deadline_exceeded" and c.sandbox.destroyed
        removed.append(task)
        if cleanup_unknown:raise RuntimeError("controlled cleanup failure")
    c.app.controller._target_network.destroy = destroy
    once = c.app._run_once
    async def boundary(token):
        result = await once(token)
        c.clock.now = c.app.state.task_run.deadline_at
        return result
    c.app._run_once = boundary
    result = asyncio.run(c.app.run(c.token))
    assert c.app.state.task_run.status.value == "deadline_exceeded"
    assert removed == [c.task] and bool(result.recovery_required) == cleanup_unknown
    assert result.stop_reason == ("task_run_deadline_cleanup_unknown" if cleanup_unknown else "task_run_deadline_exceeded")


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_continuous_counts_are_audit_only_but_deadline_still_blocks(http_case):
    from agent_core.operation_accounting import D4OperationStart, D4OperationCompletion, D4OperationConflict
    from agent_core.turn_modeling import stable_id, digest
    c = http_case
    def start(n):
        return D4OperationStart(stable_id(c.task, "audit/" + str(n)), c.task, "action", c.clock.now,
            c.app.state.latest_checkpoint.sequence, digest({"n":n}), "test-audit",
            c.app.config.d4_budget.budget_policy_ref, "inner_action", c.app.config.inner_role.role_config_ref)
    for n in range(c.app.config.d4_budget.total + 1):
        call = start(n)
        c.app.store.begin_d4_operation(call)
        c.app.store.complete_d4_operation(D4OperationCompletion(call.operation_id,c.clock.now,"failed",call.operation_id,digest({"n":n})))
    usage = c.app.store.d4_budget_usage(c.task)
    assert usage.total_used == c.app.config.d4_budget.total + 1
    assert dict(usage.category_counts)["action"] > c.app.config.d4_budget.category_limits["action"]
    c.clock.now = c.app.state.task_run.deadline_at
    with pytest.raises(D4OperationConflict):c.app.store.begin_d4_operation(start(999))


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_waiting_restart_never_replans_root_again(http_case):
    from agent_core.output_contracts import D8ModelResponse
    from test_chain_multi_turn import ProcessLoss
    c = http_case
    outer = c.models[0].invoke
    roots, inner = [], []
    def plan(envelope,*args):
        if envelope.output_contract["stage"] == "tree_planning":
            roots.append(envelope.request.model_request_id)
            if len(roots) > 1:return dict(kind="exhausted",reason="No new direction.",evidence_refs=[])
        return outer(envelope,*args)
    def fail(envelope,*args):
        inner.append(envelope.request.model_request_id)
        return D8ModelResponse("{}","stop")
    c.models[0].invoke,c.models[1].invoke = plan,fail
    async def interrupt(token):raise ProcessLoss("process stopped while waiting")
    c.app._wait_continuation = interrupt
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    before = len(c.app.store.list_d4_operations(c.task)),list(roots),list(inner)
    c.app.store.close()
    c.app = c.create()
    async def finish(token):c.clock.now = c.app.state.task_run.deadline_at
    c.app._wait_continuation = finish
    result = asyncio.run(c.app.resume(c.task,c.token))
    assert result.stop_reason == "task_run_deadline_exceeded"
    assert (len(c.app.store.list_d4_operations(c.task)),roots,inner) == before
    assert len(roots) == 2 and len(inner) == 2


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_parked_child_backtracks_to_parent_sibling_without_business_revisit(http_case):
    from agent_core.output_contracts import D8ModelResponse
    c = http_case
    async def no_final_candidate(*args):return False
    c.app.answers.after_success = no_final_candidate  # Isolate tree routing from external final-answer feedback.
    outer, inner = c.models[0].invoke, c.models[1].invoke
    root_calls, explored = [], []
    def plan(envelope,*args):
        request, stage = envelope.request,envelope.output_contract["stage"]
        if stage == "tree_planning":
            if request.turn_id is None:
                root_calls.append(request.model_request_id)
                if len(root_calls)>1:return dict(kind="exhausted",reason="No new authorized direction.",evidence_refs=[])
                names=["Parent success", "Parent alternative"]
            else:names=["Child technical"]
            return dict(kind="candidates",intents=[dict(description=n,intent_type=None,dependency_refs=[]) for n in names])
        if stage == "fact_review":
            return dict(decision="accept",reason="Scoped observation.",refined_content="A saved observation for "+request.intent_description)
        if stage == "intent_evaluation":
            actor=next(i for i in c.app.state.intents if i.intent_id==request.intent_id)
            return dict(decision="succeeded" if actor.description=="Parent success" else "failed",reason="Bounded local scope reviewed.",evidence_refs=[i.ref for i in request.history])
        return outer(envelope,*args)
    def choose(envelope,*args):
        if envelope.output_contract["stage"] != "inner_plan":return inner(envelope,*args)
        request=envelope.request
        actor=next(i for i in c.app.state.intents if i.intent_id==request.context.intent_id)
        explored.append(actor.description)
        if actor.description=="Child technical":return D8ModelResponse("{}","stop")
        context=json.loads(request.history_context)
        plans=[p for p in c.app.state.plans if p.intent_id==actor.intent_id]
        if not plans:return dict(kind="http_request",method="GET",path_query="/?actor="+str(actor.priority),headers={},body="")
        if actor.description=="Parent success" and not context["intent_execution"]["facts"]:
            return dict(kind="fact_candidate",content="The bounded parent observation is supported.",
                reason="Original observation within the parent scope.",
                evidence_refs=[context["evidence_sources"][-1]["ref"]],inferred=False)
        return dict(kind="intent_resolution",outcome="succeeded" if actor.description=="Parent success" else "failed",
            reason="Finite local scope tested.",evidence_refs=[i["ref"] for i in context["items"]],
            fact_ids=[i["fact_id"] for i in context["intent_execution"]["facts"]],next_intent_candidates=[])
    c.models[0].invoke,c.models[1].invoke=plan,choose
    async def finish(token):c.clock.now=c.app.state.task_run.deadline_at
    c.app._wait_continuation=finish
    result=asyncio.run(c.app.run(c.token))
    assert result.stop_reason=="task_run_deadline_exceeded"
    statuses={i.description:i.status.value for i in c.app.state.intents}
    assert statuses=={"Parent success":"succeeded","Parent alternative":"failed","Child technical":"suspended"},(statuses,explored)
    assert explored.index("Child technical") < explored.index("Parent alternative")
    assert explored.count("Child technical")==2 and len(c.sends)==2
    assert not any(r.kind=="resumed" for r in c.app.store.list_d8_tree_records(c.task))


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_continuation_root_rejects_unchanged_direction_using_shared_correction(http_case):
    from agent_core.output_contracts import D8ModelResponse
    c = http_case
    outer=c.models[0].invoke
    roots=[]
    def plan(envelope,*args):
        if envelope.output_contract["stage"] == "tree_planning":
            roots.append(envelope.request)
            name="Old direction" if len(roots)<3 else "Different solution chain"
            return dict(kind="candidates",intents=[dict(description=name,intent_type=None,dependency_refs=[])])
        return outer(envelope,*args)
    c.models[0].invoke=plan
    c.models[1].invoke=lambda *args:D8ModelResponse("{}","stop")
    async def finish(token):c.clock.now=c.app.state.task_run.deadline_at
    c.app._wait_continuation=finish
    asyncio.run(c.app.run(c.token))
    assert len(roots)==3 and roots[2].correction_of_model_request_id==roots[1].model_request_id
    assert len(c.app.state.turns)==2
    assert {i.description for i in c.app.state.intents}=={"Old direction","Different solution chain"}


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_end_review_process_loss_recovers_local_suspension_without_call_replay(http_case):
    from agent_core.modeling import ModelError, ModelErrorKind
    from test_chain_multi_turn import ProcessLoss
    c=http_case
    install(c,steps=1,stop="failed",no_fact=True)
    outer=c.models[0].invoke
    reviews=[]
    def fail(envelope,*args):
        if envelope.output_contract["stage"]=="tree_planning" and c.app.state.turns:
            return dict(kind="exhausted",reason="No further direction.",evidence_refs=[])
        if envelope.output_contract["stage"]=="intent_evaluation":
            request=envelope.request
            reviews.append(request.model_request_id)
            c.app.output_boundary.evidence.save("provider-result",request.model_request_id,
                {"transport":{"transport_end":"http_error","http_status":503}})
            return ModelError(ModelErrorKind.PROVIDER_FAILURE,"http_503")
        return outer(envelope,*args)
    c.models[0].invoke=fail
    original=c.app.handoff.call
    async def lose(request,token):
        result=await original(request,token)
        if request.stage=="intent_evaluation":raise ProcessLoss("ended review before suspension commit")
        return result
    c.app.handoff.call=lose
    with pytest.raises(ProcessLoss):asyncio.run(c.app.run(c.token))
    assert len(reviews)==2 and c.app.state.intents[0].status.value=="running"
    c.app.store.close();c.app=c.create()
    async def finish(token):c.clock.now=c.app.state.task_run.deadline_at
    c.app._wait_continuation=finish
    result=asyncio.run(c.app.resume(c.task,c.token))
    assert result.stop_reason=="task_run_deadline_exceeded" and len(reviews)==2 and len(c.sends)==1
    assert c.app.state.intents[0].status.value=="suspended"


@pytest.mark.parametrize("http_case", ["continuous"], indirect=True)
def test_local_suspension_public_view_keeps_diagnostics_without_global_recovery(http_case):
    from agent_core.output_contracts import D8ModelResponse
    from agent_adapters.web.task_result import snapshot_v3_task_result
    c=http_case
    outer=c.models[0].invoke
    def plan(envelope,*args):
        if envelope.output_contract["stage"]=="tree_planning" and c.app.state.turns:
            return dict(kind="exhausted",reason="No further direction.",evidence_refs=[])
        return outer(envelope,*args)
    c.models[0].invoke=plan
    c.models[1].invoke=lambda *args:D8ModelResponse("{}","stop")
    commit=c.app.handoff._commit
    views=[]
    def capture(operation,*args,**kwargs):
        result=commit(operation,*args,**kwargs)
        if operation=="suspend_d8_intent":views.append(snapshot_v3_task_result(c.directory,c.declaration))
        return result
    c.app.handoff._commit=capture
    async def finish(token):c.clock.now=c.app.state.task_run.deadline_at
    c.app._wait_continuation=finish
    asyncio.run(c.app.run(c.token))
    assert len(views)==1
    view=views[0]
    assert view["errors"]==[] and not view["recovery"]["required"],view
    assert view["task_run"]["status"]=="running" and view["model_format_diagnostics"]
    assert view["budget"]["total_limit"] is None
    assert all(v is None for v in view["budget"]["category_limits"].values())



def install_repeated_resolution(case,change='observation'):
    from copy import deepcopy
    from test_exploration_loop import install as exploration
    exploration(case,steps=1,stop='inconclusive')
    inner,outer=case.models[1].invoke,case.models[0].invoke
    state=dict(proposals=[],reviews=0,changed=False,proposal=None)
    def choose(envelope,*args):
        if envelope.output_contract['stage']=='inner_plan' and case.sends:
            h=json.loads(envelope.request.history_context)
            if state['proposal'] is None:
                ref=next(i['ref'] for i in h['items'] if i['record_id']==case.app.state.executions[0].execution_id)
                state['proposal']=dict(kind='intent_resolution',outcome='review',reason='This bounded deliverable needs the outer judgment.',evidence_refs=[ref],fact_ids=[],next_intent_candidates=[])
            if len(state['proposals'])>=2 and not state['changed']:
                state['changed']=True
                if change=='observation':return dict(kind='http_request',method='GET',path_query='/new-observation',headers={},body='')
                if change=='read':return dict(kind='history_query',query={})
                if change=='working':return dict(kind='working_record',content='Read additional source lines; local assumption is now bounded.',evidence_refs=state['proposal']['evidence_refs'])
                if change=='fact':return dict(kind='fact_candidate',content='The saved request returned the scoped recorded response.',reason='Direct original observation, distinct from the unresolved causal hypothesis.',evidence_refs=[h['evidence_sources'][0]['ref']],inferred=False)
                if change=='proposal':state['proposal']['reason']='The deliverable now includes a complete discriminator and explicit uncertainty.'
            state['proposals'].append(envelope.request.model_request_id)
            return deepcopy(state['proposal'])
        return inner(envelope,*args)
    def review(envelope,*args):
        if envelope.output_contract['stage']=='intent_evaluation':
            state['reviews']+=1
            assert state['reviews']<=2,'Repeated unchanged input should reuse the existing continue'
            return dict(decision='continue' if state['reviews']==1 else 'inconclusive',
                reason='One concrete original-scope check remains.' if state['reviews']==1 else 'Close the tested scope with its limits.',
                evidence_refs=[i.ref for i in envelope.request.history])
        return outer(envelope,*args)
    case.models[1].invoke,case.models[0].invoke=choose,review
    return state


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('change',['observation','working','fact','proposal','read'])
def test_identical_continuation_is_reused_but_new_material_is_reviewed(http_case,change):
    c=http_case
    state=install_repeated_resolution(c,change)
    result=asyncio.run(c.app._run_once(c.token))
    assert not result.recovery_required,result
    assert state['reviews']==2 and len(state['proposals'])==3
    assert c.app.state.intents[0].status.value=='inconclusive'
    assert len([r for r in c.app.store.list_d8_tree_records(c.task) if r.kind=='evaluation_continued'])==1
    reused=c.app.store.load_d3_operation(state['proposals'][1])
    assert reused.application_ref is not None
    record=next(r for r in c.app.sessions.records() if r.model_request_id==state['proposals'][1])
    assert c.app.sessions.payload(record)['observation']['intent_review']['reused'] is True
    assert len(c.sends)==(2 if change=='observation' else 1)
    from agent_adapters.web.task_result import snapshot_v3_task_result
    snapshot=snapshot_v3_task_result(c.directory,c.declaration)
    assert not snapshot['errors'],snapshot


@pytest.mark.parametrize('http_case',['continuous'],indirect=True)
@pytest.mark.parametrize('boundary',['receipt','decision'])
def test_reused_continuation_receipt_recovers_before_apply_and_detects_tampering(http_case,monkeypatch,boundary):
    from test_chain_multi_turn import ProcessLoss
    from agent_core.dual_llm_contracts import D3RecoveryRequired
    c=http_case
    state=install_repeated_resolution(c)
    mark=c.app.store.mark_d3_operation_applied
    interrupted=[]
    def lost(identity,reference):
        if boundary=='receipt' and len(state['proposals'])==2 and identity==state['proposals'][1] and not interrupted:
            interrupted.append(identity)
            raise ProcessLoss('cached continuation receipt saved before application marker')
        return mark(identity,reference)
    monkeypatch.setattr(c.app.store,'mark_d3_operation_applied',lost)
    save=c.app.sessions.evidence.save
    def save_then_lost(kind,key,value):
        result=save(kind,key,value)
        if (boundary=='decision' and kind=='decision' and len(state['proposals'])==2
                and value['metadata']['model_request_id']==state['proposals'][1] and not interrupted):
            interrupted.append(state['proposals'][1])
            raise ProcessLoss('cached decision file saved before session control commit')
        return result
    monkeypatch.setattr(c.app.sessions.evidence,'save',save_then_lost)
    with pytest.raises(ProcessLoss):asyncio.run(c.app._run_once(c.token))
    assert state['reviews']==1 and len(c.sends)==1
    c.app.store.close();c.app=c.create()
    c.app.run=c.app._run_once
    result=asyncio.run(c.app.resume(c.task,c.token))
    assert not result.recovery_required,result
    assert state['reviews']==2 and len(state['proposals'])==3 and len(c.sends)==2
    row=next(r for r in c.app.sessions.records() if r.model_request_id==interrupted[0])
    operation=c.app.store.load_d3_operation(interrupted[0])
    read=c.app.handoff.evidence.read
    def changed(kind,key):
        data=read(kind,key)
        return {**data,'intent_id':'changed'} if kind=='decision' and key==operation.application_ref else data
    monkeypatch.setattr(c.app.handoff.evidence,'read',changed)
    with pytest.raises(D3RecoveryRequired,match='d22_continuation_reuse_changed'):
        c.app.sessions.validate_record(row,c.app.state)



def test_continuation_scope_tracks_material_and_authority_not_bookkeeping():
    from types import SimpleNamespace as NS
    from agent_core.intent_execution import continuation_scope
    item=NS(intent_id='actor',record_id='observation',ref='source:v1',source_status='available',conflict=False,identity='trusted_state')
    history=[item]
    reviews=[]
    epoch=['selection-1']
    read_count=[0]
    app=NS(state=NS(task_run=NS(task_run_id='task')),handoff=NS(history=lambda:history,read_count=lambda actor:read_count[0]),
        store=NS(list_d3_review_applications=lambda task:reviews),supervision=NS(epoch=lambda actor:epoch[0]))
    before=continuation_scope(app,'actor')
    for prefix in ('tree/','decision/'):
        history.append(NS(intent_id='actor',record_id=prefix+'bookkeeping',ref='new-progress',source_status='available',conflict=False,identity='trusted_state'))
    assert continuation_scope(app,'actor')==before
    reviews.append(NS(constraint_version=2))
    assert continuation_scope(app,'actor')!=before
    reviews.clear()
    read_count[0]=1
    assert continuation_scope(app,'actor')!=before
    read_count[0]=0
    epoch[0]='selection-2'
    assert continuation_scope(app,'actor')!=before
    epoch[0]='selection-1'
    item.source_status='unavailable'
    assert continuation_scope(app,'actor')!=before
    item.source_status='available';item.ref='source:v2'
    assert continuation_scope(app,'actor')!=before


def test_new_strategy_stop_cannot_be_overridden_by_cached_continue(monkeypatch):
    from types import SimpleNamespace as NS
    import agent_core.intent_execution as execution
    writes=[]
    app=NS(config=NS(intent_execution_policy='continuous_v1'),_id=lambda key:key,
        handoff=NS(evidence=NS(read=lambda *args:None,save=lambda *args:writes.append(args))),
        sessions=NS(evidence=NS(read=lambda *args:{'request':{'history_context':json.dumps(
            {'intent_execution':{'last_review':{'record_id':'old'}}})}})))
    monkeypatch.setattr(execution,'stop_request',lambda *args:{'review_id':'new-supervision'})
    assert not execution.reuse_continuation(app,'request','actor',None)
    assert not writes


@pytest.mark.parametrize("http_case", ["rolling", "continuous"], indirect=True)
@pytest.mark.parametrize("unknown", [False, True])
def test_end_review_crossing_deadline_settles_only_known_operations(http_case, unknown):
    from uuid import uuid4
    from agent_core.operation_accounting import D4OperationStart
    c = http_case
    install(c, steps=1)
    original = c.app.handoff.call
    reviews = []
    async def finish_review(request, token):
        final, decision = await original(request, token)
        if request.stage == "intent_evaluation":
            reviews.append(final.model_request_id)
            if unknown:
                c.app.store.begin_d4_operation(D4OperationStart(str(uuid4()), c.task, "tool_dispatch", c.clock(),
                    c.app.state.latest_checkpoint.sequence, "0" * 64, "controlled-unknown-tool",
                    c.app.config.d4_budget.budget_policy_ref))
            c.clock.now = c.app.state.task_run.deadline_at + 0.01
        return final, decision
    c.app.handoff.call = finish_review
    result = asyncio.run(c.app.run(c.token))
    assert len(reviews) == len(c.sends) == 1
    assert not any(r.kind in {"failed", "inconclusive", "succeeded"}
                   for r in c.app.store.list_d8_tree_records(c.task))
    if unknown:
        assert result.stop_reason == "d4_operation_outcome_unknown" and result.recovery_required
        assert c.app.state.task_run.status.value == "running" and not c.sandbox.destroyed
    else:
        assert result.stop_reason == "task_run_deadline_exceeded" and not result.recovery_required
        assert c.app.state.task_run.status.value == "deadline_exceeded" and c.sandbox.destroyed
