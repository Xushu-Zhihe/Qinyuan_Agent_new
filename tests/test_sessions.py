"""D8-4 public behavior through the real internal scheduler and persistence."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_adapters.multi_turn_application import D3Application
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.handoff_contracts import D8CapacityPolicy, D8HandoffConfig
from agent_core.operation_accounting import D8HandoffBudgetPolicy
from agent_core.conclusion_revision_contracts import D8ChangeRequest
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.turn_contracts import D1RoleConfig
from test_chain_multi_turn import Clock, Model, ReviewModel, Sandbox, Token, ProcessLoss
from test_conclusion_review import Inner, FactReviewer, Refinement
from test_handoff import HandoffModel


@pytest.mark.parametrize("summary", ["legacy plain text", "[]", "1"])
def test_legacy_review_summary_does_not_require_progress_json(summary):
    from agent_core.review_contracts import D2ReviewRequest
    from agent_core.dual_llm_contracts import D3ReviewOutput, parse_d3_review_output
    request = D2ReviewRequest(str(uuid4()), str(uuid4()), 1, 1, "action",
                              str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()),
                              1, "legacy-context", ("saved-source",), 0, summary)
    raw = {"decision": "continue_current_intent", "rationale": "Saved observation",
           "source_ids": ["saved-source"], "constraints": []}
    assert type(parse_d3_review_output(raw, request)) is D3ReviewOutput


class SessionInner(Inner):
    def __init__(self, role):
        super().__init__(role)
        self.transform = lambda request, raw: raw
        self.repair_calls = []

    def invoke(self, request, timeout, token):
        if isinstance(request, D8ChangeRequest):
            self.repair_calls.append(request)
            return self.transform(request, {"kind": "history_query", "query": {}})
        raw = super().invoke(request, timeout, token)
        if request.stage == "inner_complete" and isinstance(raw, dict):
            raw = {**raw, "next_intent_candidates": []}
        return self.transform(request, raw)


class ProgressModel(ReviewModel):
    classification = "meaningful"
    decision = "continue_current_intent"

    def __init__(self, role):
        super().__init__(role)
        self.transform = lambda request, raw: raw

    def invoke(self, request):
        raw = super().invoke(request)
        summary = json.loads(request.context_summary)
        if "d8_progress" in summary:
            raw = {**raw, "decision": self.decision,
                   "progress": {"classification": self.classification,
                                "reason": "The saved decisions provide explicit progress observations.",
                                "evidence_refs": [summary["d8_progress"]["recent"][-1]["ref"]]}}
        return self.transform(request, raw)


@pytest.fixture
def session_case(tmp_path, request):
    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.txt").write_bytes(b"D8-4 authorized local observation")
    outer = D1RoleConfig("outer_planning", "fake", "same-model", "outer-d8-4-v1")
    inner_role = D1RoleConfig("inner_action", "fake", "same-model", "inner-d8-4-v1")
    policy = D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 262144, 8192)
    config = D3RunConfig(outer, inner_role, D3Authorization(str(uuid4()), (0,)),
                         outer.role_config_ref, contract_version=4 if getattr(request, "param", None) == "legacy_v4" else 5,
                         d4_budget=D8HandoffBudgetPolicy(), d5_enabled=True,
                         d5_sensitive_requirements=(), d8_enabled=True,
                         handoff_config=D8HandoffConfig(policy, policy))
    sandbox, clock, token = Sandbox(), Clock(), Token()
    inner, handoff, reviewer = SessionInner(inner_role), HandoffModel(outer), ProgressModel(outer)
    def planning(request, raw):
        if request.stage == "tree_planning" and raw["kind"] == "candidates":
            if request.turn_id is not None:
                return {"kind": "exhausted", "reason": "This internal slice has no further direction.",
                        "evidence_refs": [request.history[0].ref]}
            return {**raw, "intents": raw["intents"][:1]}
        return raw
    handoff.modify = planning
    def create():
        return D3Application(tmp_path / "run", config=config, sandbox=sandbox,
                             import_sandbox=sandbox, workspace_sandbox=sandbox,
                             command_sandbox=sandbox, outer_model=Model(outer), inner_model=inner,
                             review_model=reviewer, fact_review_model=FactReviewer(outer),
                             refinement_model=Refinement(outer), handoff_model=handoff,
                             capacity_port=FakeUnicodeCapacity(), clock=clock)
    app = create()
    task = app.start(TaskRequest.from_dict({"goal": "Observe the authorized local copy",
                                            "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]}}), source)
    if getattr(request, "param", None) != "default_capacity":
        app.handoff.increase_capacity({"initial_history": 32768, "visible_history": 131072})
    case = [app, handoff, inner, reviewer, sandbox, clock, token, create, task]
    yield case
    case[0].store.close()


def run(case):
    return asyncio.run(case[0].run(case[6]))


def reopen(case):
    case[0].store.close()
    case[0] = case[7]()
    case[0].controller.attach(case[8])
    case[0]._bound_task_run_id = case[8]
    return case[0]


def test_actual_session_progress_and_complete_handoff(session_case):
    case = session_case
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    app = case[0]
    decisions = app.sessions.records()
    assert [r.ordinal for r in decisions] == [1, 2, 3, 4, 5]
    assert len({r.root_request_id for r in decisions}) == 5
    requests = [r for r in case[2].calls if hasattr(r, "session_id")]
    assert len({r.session_id for r in requests}) == 1
    assert json.loads(requests[0].session_context)["messages"] == []
    assert len(json.loads(requests[-1].session_context)["messages"]) == 12
    progress = [r for r in case[3].calls if "d8_progress" in json.loads(r.context_summary)]
    assert len(progress) == 1
    assert json.loads(progress[0].context_summary)["d8_progress"]["decision_count"] == 4
    planning = [r for r in case[1].calls if r.stage == "tree_planning" and r.turn_id is not None]
    assert len(planning) == 1 and planning[0].inner_suggestions == ()
    assert planning[0].suggestion_request_id == decisions[-1].model_request_id
    before = (len(case[2].calls), len(case[3].calls), len(case[4].command_calls))
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert before == (len(case[2].calls), len(case[3].calls), len(case[4].command_calls))


def test_inconclusive_progress_pauses_without_fifth_inner_call(session_case):
    case = session_case
    case[3].classification, case[3].decision = "inconclusive", "pause_current_intent"
    result = run(case)
    assert result.stop_reason == "d8_progress_inconclusive", result
    assert len(case[0].sessions.records()) == 4
    assert not any(r.stage == "inner_complete" for r in case[2].calls)
    before = (len(case[2].calls), len(case[3].calls), len(case[4].command_calls))
    reopen(case)
    assert run(case).stop_reason == "d8_progress_inconclusive"
    assert before == (len(case[2].calls), len(case[3].calls), len(case[4].command_calls))


def test_repetitive_without_sibling_abandons_and_exhausts(session_case):
    case = session_case
    case[3].classification, case[3].decision = "repetitive", "abandon_current_intent"
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    assert case[0].state.intents[0].status.value == "abandoned"
    assert case[0].state.task_run.status.value == "running"
    assert len(case[0].sessions.records()) == 4


def test_history_only_rounds_are_checked_at_four_eight_and_twelve(session_case):
    case = session_case
    case[2].transform = lambda request, raw: ({"kind": "history_query", "query": {}}
        if request.stage == "inner_plan" and len([r for r in case[2].calls if r.stage == "inner_plan"]) <= 8 else raw)
    assert run(case).stop_reason == "search_exhausted"
    checks = [json.loads(r.context_summary)["d8_progress"] for r in case[3].calls
              if "d8_progress" in json.loads(r.context_summary)]
    assert [p["decision_count"] for p in checks] == [4, 8, 12]
    assert checks[0]["cumulative"]["saved_execution_count"] == 0
    assert len(case[0].store.list_d8_reads(case[8])) == 8
    assert len(case[4].command_calls) == 2


def test_progress_replan_preserves_session_and_count(session_case):
    case = session_case
    def first_replan(request, raw):
        if "d8_progress" in json.loads(request.context_summary) and len(case[0].state.plans) == 1:
            return {**raw, "decision": "replan_current_intent"}
        return raw
    case[3].transform = first_replan
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    assert len(case[0].state.plans) == 2
    plans = [r for r in case[2].calls if r.stage == "inner_plan"]
    assert plans[0].session_id == plans[1].session_id
    assert json.loads(plans[1].session_context)["messages"]
    assert [r.ordinal for r in case[0].sessions.records()] == list(range(1, 9))
    checks = [json.loads(r.context_summary)["d8_progress"]["decision_count"] for r in case[3].calls
              if "d8_progress" in json.loads(r.context_summary)]
    assert checks == [4]  # Eighth decision ends the Intent; no redundant periodic review.


def test_decision_file_before_commit_replays_once(session_case, monkeypatch):
    case = session_case
    original = case[0].controller.commit_d3_transition
    def crash(operation, *args, **kwargs):
        if operation == "append_d8_decision":
            raise ProcessLoss()
        return original(operation, *args, **kwargs)
    monkeypatch.setattr(case[0].controller, "commit_d3_transition", crash)
    with pytest.raises(ProcessLoss):
        run(case)
    assert len(case[2].calls) == 1
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert len([r for r in case[2].calls if r.stage == "inner_plan"]) == 1
    assert len(case[4].command_calls) == 2
    assert [r.ordinal for r in case[0].sessions.records()] == [1, 2, 3, 4, 5]


def test_saved_progress_response_applies_without_reasking(session_case, monkeypatch):
    case = session_case
    original = case[0].review_coordinator._apply_callback
    def crash(request, output):
        if "d8_progress" in json.loads(request.context_summary):
            raise ProcessLoss()
        return original(request, output)
    monkeypatch.setattr(case[0].review_coordinator, "_apply_callback", crash)
    with pytest.raises(ProcessLoss):
        run(case)
    before = len(case[3].calls)
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert len([r for r in case[3].calls if "d8_progress" in json.loads(r.context_summary)]) == 1
    assert len(case[3].calls) == before + 1  # Only the existing tail review is new.


def test_progress_format_correction_charged_but_not_an_inner_round(session_case):
    case = session_case
    def correction(request, raw):
        if "d8_progress" in json.loads(request.context_summary) and request.review_attempt == 1:
            return {k: v for k, v in raw.items() if k != "progress"}
        return raw
    case[3].transform = correction
    assert run(case).stop_reason == "search_exhausted"
    checks = [r for r in case[3].calls if "d8_progress" in json.loads(r.context_summary)]
    assert len(checks) == 2 and [r.review_attempt for r in checks] == [1, 2]
    assert len(case[0].sessions.records()) == 5
    assert dict(case[0].store.d4_budget_usage(case[8]).category_counts)["review"] == 5


def test_inner_correction_is_one_logical_decision(session_case):
    case = session_case
    case[2].transform = lambda request, raw: ({"unexpected": "discard this invalid reply"}
        if request.stage == "inner_plan" and request.correction_of_model_request_id is None else raw)
    assert run(case).stop_reason == "search_exhausted"
    rows = case[0].sessions.records()
    assert len(rows) == 5 and rows[0].root_request_id != rows[0].model_request_id
    assert dict(case[0].store.d4_budget_usage(case[8]).category_counts)["action"] == 6
    assert "discard this invalid reply" not in json.dumps(case[0].sessions.payload(rows[0]))
    assert case[0].sessions.payload(rows[0])["request"]["rejection_code"]


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_progress_response_after_stop_cannot_apply_or_dispatch_fifth(session_case, stop):
    case = session_case
    def stopping(request, raw):
        if "d8_progress" in json.loads(request.context_summary):
            if stop == "cancel":
                case[6].is_cancelled = True
            else:
                case[5].now = case[0].state.task_run.deadline_at
        return raw
    case[3].transform = stopping
    run(case)
    assert not any(r.stage == "inner_complete" for r in case[2].calls)
    assert not case[0].sessions.reviews(case[0].state.intents[0].intent_id)


@pytest.mark.parametrize("session_case", ["default_capacity"], indirect=True)
def test_session_capacity_pause_then_operator_increase_without_tool_replay(session_case):
    case = session_case
    result = run(case)
    assert result.stop_reason == "d8_context_paused:visible_history", result
    before = len(case[2].calls)
    case[0].handoff.increase_capacity({"initial_history": 32768, "visible_history": 131072})
    assert run(case).stop_reason == "search_exhausted"
    assert len(case[4].command_calls) == 2 and len(case[2].calls) > before
    capacities = case[0].store.list_d8_capacities(case[8])
    assert [c.version for c in capacities] == [1, 2]
    assert capacities[0].capacity.visible_history == 16384


@pytest.mark.parametrize("classification", ["meaningful", "repetitive", "inconclusive"])
def test_restricted_repair_shares_session_and_counter_but_never_opens_tools(session_case, monkeypatch, classification):
    case = session_case
    def halt_before_fact(*args):
        raise ProcessLoss()
    monkeypatch.setattr(case[0].conclusions, "propose", halt_before_fact)
    with pytest.raises(ProcessLoss):
        run(case)
    assert len(case[0].sessions.records()) == 3
    actor = case[0].state.intents[0].intent_id
    original_session = case[2].calls[0].session_id
    asyncio.run(case[0].repair_conclusions(actor, str(uuid4()), case[6]))
    assert len(case[2].repair_calls) == 1 and case[2].repair_calls[0].session_id == original_session
    case[3].classification = classification
    case[3].decision = "continue_current_intent" if classification == "meaningful" else "pause_current_intent"
    if classification == "meaningful":
        asyncio.run(case[0].repair_conclusions(actor, str(uuid4()), case[6]))
        assert len(case[2].repair_calls) == 2
    else:
        with pytest.raises(D3RecoveryRequired, match="d8_progress_" + classification):
            asyncio.run(case[0].repair_conclusions(actor, str(uuid4()), case[6]))
        assert len(case[2].repair_calls) == 1
    checks = [json.loads(r.context_summary)["d8_progress"] for r in case[3].calls
              if "d8_progress" in json.loads(r.context_summary)]
    assert len(checks) == 1 and checks[0]["mode"] == "restricted_repair"
    assert checks[0]["decision_count"] == 4
    assert len(case[4].command_calls) == 2
    assert case[0].state.intents[0].status.value == "running"
    assert not case[0].state.facts


@pytest.mark.parametrize("decision", ["accept", "adjust", "reject"])
def test_next_intent_suggestion_is_only_registered_by_outer_planning(session_case, decision):
    case = session_case
    text = "Follow the saved authorized observation"
    def suggest(request, raw):
        if request.stage == "inner_complete":
            packet = json.loads(request.session_context)["current_history"]
            ref = next(i["ref"] for i in packet["items"] if i["identity"] == "verified_observation_only")
            return {**raw, "next_intent_candidates": [{"description": text, "intent_type": None,
                         "dependency_refs": [], "reason": "The saved observation supports considering this direction.",
                         "evidence_refs": [ref]}]}
        return raw
    case[2].transform = suggest
    original = case[1].modify
    def plan(request, raw):
        if request.stage == "tree_planning" and request.turn_id is not None and raw["kind"] == "candidates":
            assert len(request.inner_suggestions) == 1 and request.inner_suggestions[0].description == text
            if decision == "reject":
                return original(request, raw)
            accepted = text if decision == "accept" else "A revised authorized observation direction"
            return {**raw, "intents": [{"description": accepted, "intent_type": None, "dependency_refs": []}]}
        return original(request, raw)
    case[1].modify = plan
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    children = [t for t in case[0].state.turns if t.parent_turn_id]
    if decision == "reject":
        assert not children and len(case[4].command_calls) == 2
    else:
        assert len(children) == 1
        chosen = next(i for i in case[0].state.intents if i.turn_id == children[0].turn_id)
        assert chosen.description == (text if decision == "accept" else "A revised authorized observation direction")
        firsts = [r for r in case[2].calls if r.stage == "inner_plan"]
        assert firsts[0].session_id != firsts[1].session_id
        assert json.loads(firsts[1].session_context)["messages"] == []
        assert json.loads(firsts[1].session_context)["current_history"]["items"]
    original_suggestion = case[0].sessions.payload(case[0].sessions.records()[4])["response"]["next_intent_candidates"][0]
    assert original_suggestion["description"] == text


def test_progress_budget_exhaustion_never_allows_fifth_decision(session_case, monkeypatch):
    case = session_case
    from agent_core.operation_accounting import D4BudgetExhausted
    original = case[0].store.begin_d4_operation
    def limit(start):
        if start.category == "review" and len(case[0].sessions.records()) >= 4:
            raise D4BudgetExhausted("review")
        return original(start)
    monkeypatch.setattr(case[0].store, "begin_d4_operation", limit)
    run(case)
    assert not any(r.stage == "inner_complete" for r in case[2].calls)
    assert len(case[0].sessions.records()) == 4
    assert len(case[4].command_calls) == 2


def test_real_review_eight_limit_survives_reopen_and_stops_next_inner_decision(session_case):
    case = session_case
    case[1].modify = lambda request, raw: raw
    result = run(case)
    assert result.dispatch_status == "stopped"
    usage = case[0].store.d4_budget_usage(case[8])
    assert dict(usage.category_counts)["review"] == 8 and usage.total_used <= 60
    current = next(i for i in case[0].state.intents if i.status.value == "running")
    assert len(case[0].sessions.records(current.intent_id)) == 4
    assert not any(r.stage == "inner_complete" and json.loads(r.session_context)["intent_id"] == current.intent_id
                   for r in case[2].calls)
    before = (len(case[2].calls), len(case[3].calls), len(case[4].command_calls))
    reopen(case)
    run(case)
    assert case[0].store.d4_budget_usage(case[8]) == usage
    assert before == (len(case[2].calls), len(case[3].calls), len(case[4].command_calls))
    firsts = [r for r in case[2].calls if r.stage == "inner_plan"]
    assert len(firsts) == 3 and len({r.session_id for r in firsts}) == 3
    assert all(json.loads(r.session_context)["messages"] == [] for r in firsts)


def test_unknown_inner_result_reopen_does_not_reask(session_case, monkeypatch):
    case = session_case
    original = case[0].model_evidence.save
    def loss(evidence):
        if evidence.stage == "inner_size":
            raise ProcessLoss()
        return original(evidence)
    monkeypatch.setattr(case[0].model_evidence, "save", loss)
    with pytest.raises(ProcessLoss):
        run(case)
    before = (len(case[2].calls), len(case[4].command_calls))
    reopen(case)
    result = run(case)
    assert result.recovery_required
    assert before == (len(case[2].calls), len(case[4].command_calls))
    assert len(case[0].sessions.records()) == 1


def test_corrupt_decision_file_pauses_without_replacing_history(session_case, monkeypatch):
    case = session_case
    def halt(*args):
        raise ProcessLoss()
    monkeypatch.setattr(case[0].conclusions, "propose", halt)
    with pytest.raises(ProcessLoss):
        run(case)
    record = case[0].sessions.records()[0]
    path = case[0].sessions.evidence.records.directory / ("d8-handoff-decision-" + record.record_id + ".json")
    assert path.exists()
    path.write_bytes(b"corrupt original history")
    before = (len(case[2].calls), len(case[4].command_calls))
    reopen(case)
    assert run(case).recovery_required
    assert path.read_bytes() == b"corrupt original history"
    assert before == (len(case[2].calls), len(case[4].command_calls))


@pytest.mark.parametrize("invalid", ["missing", "source", "classification", "decision"])
def test_invalid_progress_never_grants_continue(session_case, invalid):
    case = session_case
    def broken(request, raw):
        if "d8_progress" not in json.loads(request.context_summary):
            return raw
        if invalid == "missing":
            return {k: v for k, v in raw.items() if k != "progress"}
        p = dict(raw["progress"])
        if invalid == "source":
            p["evidence_refs"] = ["d8-decision:" + str(uuid4())]
        elif invalid == "classification":
            p["classification"] = "assumed_achieved"
        else:
            p["classification"] = "repetitive"  # continue is incompatible.
        return {**raw, "progress": p}
    case[3].transform = broken
    result = run(case)
    assert result.recovery_required
    assert len([r for r in case[3].calls if "d8_progress" in json.loads(r.context_summary)]) == 2
    assert not any(r.stage == "inner_complete" for r in case[2].calls)
    assert case[0].state.intents[0].status.value == "running"


def test_sensitive_conclusion_is_not_added_to_conversation(session_case):
    case = session_case
    case[2].transform = lambda request, raw: ({**raw, "reason": "password=example-d8-4-secret"}
                                              if request.stage == "fact_proposal" else raw)
    result = run(case)
    assert result.recovery_required and not case[0].state.facts
    files = list((case[0].directory / "d8-sessions").glob("*.json"))
    assert files and all(b"example-d8-4-secret" not in p.read_bytes() for p in files)
    assert len(case[0].sessions.records()) == 3


def test_fourth_decision_review_request_and_periodic_check_are_one_call(session_case):
    case = session_case
    case[2].transform = lambda request, raw: ({"kind": "request_review", "reason": "Review this saved progress."}
        if request.stage == "fact_proposal" and len(case[2].fact_calls) == 1 else raw)
    result = run(case)
    assert result.stop_reason == "search_exhausted", result
    checks = [r for r in case[3].calls if "d8_progress" in json.loads(r.context_summary)]
    assert len(checks) == 1 and json.loads(checks[0].context_summary)["d8_progress"]["decision_count"] == 4
    assert dict(case[0].store.d4_budget_usage(case[8]).category_counts)["review"] == 4
    assert len(case[0].sessions.records()) == 6
    assert case[0].sessions.records()[3].disposition == "review_requested"


def test_combined_review_saved_response_and_pending_signal_recover_once(session_case, monkeypatch):
    case = session_case
    case[2].transform = lambda request, raw: ({"kind": "request_review", "reason": "Review this saved progress."}
        if request.stage == "fact_proposal" and len(case[2].fact_calls) == 1 else raw)
    original = case[0].review_coordinator._apply_callback
    def crash(request, output):
        if "d8_progress" in json.loads(request.context_summary):
            raise ProcessLoss()
        return original(request, output)
    monkeypatch.setattr(case[0].review_coordinator, "_apply_callback", crash)
    with pytest.raises(ProcessLoss):
        run(case)
    assert len(case[0].sessions.records()) == 4
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert len([r for r in case[3].calls if "d8_progress" in json.loads(r.context_summary)]) == 1
    assert len(case[2].fact_calls) == 2 and len(case[4].command_calls) == 2


def test_applied_progress_audit_tampering_cannot_change_pause_to_continue(session_case):
    case = session_case
    case[3].classification, case[3].decision = "inconclusive", "pause_current_intent"
    assert run(case).stop_reason == "d8_progress_inconclusive"
    path = case[0].directory / "d3-reviews" / "review-ledger.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    # Change the persisted response without changing the immutable Controller digest.
    for root in ledger["roots"].values():
        for call in root.get("calls", []):
            if isinstance(call.get("response"), dict) and "progress" in call["response"]:
                call["response"]["progress"]["classification"] = "meaningful"
                call["response"]["decision"] = "continue_current_intent"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    before = (len(case[2].calls), len(case[4].command_calls))
    reopen(case)
    assert run(case).recovery_required
    assert before == (len(case[2].calls), len(case[4].command_calls))


@pytest.mark.parametrize("session_case", ["legacy_v4"], indirect=True)
def test_actual_schema25_readonly_and_upgrade_do_not_backfill_sessions(session_case):
    import sqlite3
    from agent_core.sqlite_control_store import SqliteControlStore, CURRENT_SCHEMA_VERSION
    case = session_case
    path = case[0].directory / "control.sqlite"
    config = case[0].config
    # A v4 run with the same prior behavior, not a v5 run whose identity is downgraded.
    case[0].store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE d8_refinement_process_facts")
        connection.execute("DROP TABLE d8_refinement_corrections")
        connection.execute("DELETE FROM schema_versions WHERE version=27")
        connection.execute("DROP TABLE d8_decisions")
        connection.execute("DELETE FROM schema_versions WHERE version=26")
    before = path.read_bytes()
    with SqliteControlStore.open_readonly(path) as readonly:
        assert readonly._schema_version == 25
        assert readonly.load_d3_run_config(case[8]).contract_version == 4
        assert readonly.list_d8_decisions(case[8]) == ()
    assert path.read_bytes() == before
    upgraded = SqliteControlStore.open(path, clock=case[5])
    assert upgraded._schema_version == CURRENT_SCHEMA_VERSION == 27
    assert upgraded.list_d8_decisions(case[8]) == ()
    assert upgraded.load_d3_run_config(case[8]) == config
    assert upgraded.load_d3_run_config(case[8]).d4_budget.total == 60
    case[0].store = upgraded


@pytest.mark.parametrize("failure", ["transaction", "reload"])
def test_decision_append_transaction_and_postcommit_reload_recover_without_duplicate(session_case, monkeypatch, failure):
    case = session_case
    from agent_core.control_store import StorageError
    import agent_core.sqlite_control_store as sqlite_store
    failed = False
    if failure == "transaction":
        original = sqlite_store.save_handoff_mutation
        def insert(store, mutation, sequence):
            nonlocal failed
            original(store, mutation, sequence)
            if mutation.operation == "append_d8_decision" and not failed:
                failed = True
                raise StorageError("injected decision transaction rollback")
        monkeypatch.setattr(sqlite_store, "save_handoff_mutation", insert)
    else:
        original = case[0].store.load_control_state_rows
        def reload(task):
            nonlocal failed
            if (not failed and not case[0].store._connection.in_transaction
                    and case[0].store.list_d8_decisions(task)):
                failed = True
                raise StorageError("injected committed decision reload failure")
            return original(task)
        monkeypatch.setattr(case[0].store, "load_control_state_rows", reload)
    result = run(case)
    assert failed and result.recovery_required
    if failure == "reload":
        assert result.stop_reason.startswith("d8_controller_reload_required:")
        assert result.verified_fact_ids == ()
    assert len(case[0].store.list_d8_decisions(case[8])) == (0 if failure == "transaction" else 1)
    if failure == "transaction":
        monkeypatch.setattr(sqlite_store, "save_handoff_mutation", original)
    reopen(case)
    assert run(case).stop_reason == "search_exhausted"
    assert len([r for r in case[2].calls if r.stage == "inner_plan"]) == 1
    assert len(case[4].command_calls) == 2
    assert [r.ordinal for r in case[0].sessions.records()] == [1, 2, 3, 4, 5]
