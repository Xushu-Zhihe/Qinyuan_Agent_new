"""D8-2 contracts and durable internal handoff behavior."""

import asyncio
import json
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from agent_core.operation_accounting import (
    D4BudgetPolicy, D4OperationConflict, D4OperationStart,
    D8BudgetPolicy, D8HandoffBudgetPolicy,
)
from test_d8_conclusion_review import case
from test_d8_conclusion_review import Inner, FactReviewer, Refinement
from test_d3_chain import Clock, Model, ReviewModel, Sandbox, Token, ProcessLoss
from agent_adapters.multi_turn_application import D3Application
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_core.contracts import TaskRequest
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig, d3_payload
from agent_core.handoff_contracts import D8Capacity, D8CapacityPolicy, D8HandoffConfig, D8SummaryCandidate
from agent_core.turn_contracts import D1RoleConfig
from agent_core.dual_llm_contracts import D3RecoveryRequired
from agent_core.control_store import StorageError
from agent_core.modeling import ModelError, ModelErrorKind


def test_handoff_budget_is_explicit_and_older_contracts_reject_it():
    budget = D8HandoffBudgetPolicy()
    assert D8HandoffBudgetPolicy.from_dict(asdict(budget)) == budget
    assert sum(budget.category_limits.values()) == budget.total == 60
    assert budget.category_limits["handoff_summary"] == 4
    assert len({D4BudgetPolicy().budget_policy_ref,
                D8BudgetPolicy().budget_policy_ref, budget.budget_policy_ref}) == 3
    for older in (D4BudgetPolicy, D8BudgetPolicy):
        with pytest.raises(ValueError):
            older.from_dict(asdict(budget))
    for update in ({"total": 61}, {"handoff_summary": 5}, {"fact_review": 9}):
        with pytest.raises(ValueError):
            replace(budget, **update)


def test_existing_d8_run_cannot_admit_handoff_or_gain_budget(case):
    app, _, _, _, clock, _, _, task_id = case[:8]
    before = app.store.d4_budget_usage(task_id)
    start = D4OperationStart(
        str(uuid4()), task_id, "handoff_summary", clock(),
        app.state.latest_checkpoint.sequence, "0" * 64, task_id,
        app.config.d4_budget.budget_policy_ref, "outer_planning",
        app.config.outer_role.role_config_ref,
    )
    assert start.charged
    with pytest.raises(D4OperationConflict):
        app.store.begin_d4_operation(start)
    assert app.store.d4_budget_usage(task_id) == before
    assert app.store.load_d4_operation(start.operation_id) is None


class HandoffModel:
    def __init__(self, role):
        self.role_config, self.calls = role, []
        self.child_created = False
        self.modify = lambda request, raw: raw

    def invoke(self, request, timeout, token):
        self.calls.append(request)
        if request.stage == "handoff_summary":
            raw = {"turn_body": "Stage progress only; inspect labelled references.",
                   "global_body": "Search path summary; this is model-organized information.",
                   "ordered_refs": [request.history[0].ref] if request.history else [],
                   "open_questions": [{"text": "Which direction should be checked next?", "refs": []}]}
        elif request.stage == "intent_evaluation":
            support = next(i.ref for i in request.history if i.record_id == request.support_fact_id)
            raw = {"decision": "succeeded", "reason": "The stated local copy observation is supported.",
                   "evidence_refs": [support]}
        elif request.stage == "tree_planning":
            if request.turn_id is None or not self.child_created:
                if request.turn_id is not None:
                    self.child_created = True
                count = 2 if request.turn_id is None else 1
                raw = {"kind": "candidates", "turn_target": request.goal,
                       "completion_rule": {"rule_id": "attachment_copy_sha256", "version": 1,
                                           "parameters": {"attachment_index": 0}},
                       "intents": [{"description": f"Check local authorized copy by strategy {n}",
                                    "intent_type": "file_sha256", "dependency_refs": []} for n in range(count)]}
            else:
                raw = {"kind": "exhausted", "reason": "No further local branch is proposed.", "evidence_refs": []}
        else:
            raise AssertionError(request.stage)
        return self.modify(request, raw)


@pytest.fixture
def tree(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.txt").write_bytes(b"D8-2 authorized local copy")
    outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-d8-2-v1")
    inner_role = D1RoleConfig("inner_action", "fake", "same-model", "inner-d8-2-v1")
    policy = D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 131072, 8192)
    config = D3RunConfig(outer_role, inner_role, D3Authorization(str(uuid4()), (0,)),
                         outer_role.role_config_ref, contract_version=3,
                         d4_budget=D8HandoffBudgetPolicy(), d5_enabled=True,
                         d5_sensitive_requirements=(), d8_enabled=True,
                         handoff_config=D8HandoffConfig(policy, policy))
    sandbox, clock, token = Sandbox(), Clock(), Token()
    inner, handoff = Inner(inner_role), HandoffModel(outer_role)

    def create():
        return D3Application(tmp_path / "run", config=config, sandbox=sandbox,
                             import_sandbox=sandbox, workspace_sandbox=sandbox,
                             command_sandbox=sandbox, outer_model=Model(outer_role), inner_model=inner,
                             review_model=ReviewModel(outer_role), fact_review_model=FactReviewer(outer_role),
                             refinement_model=Refinement(outer_role), handoff_model=handoff,
                             capacity_port=FakeUnicodeCapacity(), clock=clock)
    app = create()
    task_id = app.start(TaskRequest.from_dict({"goal": "Observe the authorized local copy",
                                               "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]}}), source)
    result = [app, handoff, inner, sandbox, clock, token, create, task_id]
    yield result
    result[0].store.close()


def tree_run(tree):
    return asyncio.run(tree[0].run(tree[5]))


def tree_reopen(tree):
    tree[0].store.close()
    tree[0] = tree[6]()
    tree[0].controller.attach(tree[7])
    tree[0]._bound_task_run_id = tree[7]
    return tree[0]


def test_actual_tree_handoff_child_and_parent_sibling(tree):
    result = tree_run(tree)
    app = tree[0]
    assert result.stop_reason == "search_exhausted"
    root, child = app.state.turns
    assert child.parent_turn_id == root.turn_id
    assert child.triggering_intent_id == root.intent_ids[0]
    assert all(i.status.value == "succeeded" for i in app.state.intents)
    assert all(t.status.value == "completed" for t in app.state.turns)
    assert len(app.state.facts) == 3 and app.state.task_run.status.value == "running"
    versions = app.store.list_d8_handoffs(tree[7])
    assert sum(v.turn_id == root.turn_id for v in versions) > 1
    assert {v.origin for v in versions} <= {"model_organized", "trusted_code_fallback"}
    planning = [r for r in tree[1].calls if r.stage == "tree_planning"]
    assert planning[1].support_fact_id is not None
    assert any(i.identity == "verified_observation_only" for i in planning[1].history)
    assert tree[3].digest_calls == 3 and len(tree[3].command_calls) == 6
    before = len(tree[1].calls)
    tree_reopen(tree)
    assert tree_run(tree).stop_reason == "search_exhausted"
    assert len(tree[1].calls) == before


def test_summary_uses_frozen_capacity_and_partial_precise_refs():
    raw = {"turn_body": "x" * 1500, "global_body": "summary", "ordered_refs": ["frozen-v1"],
           "open_questions": [{"text": "Unresolved", "refs": []}]}
    with pytest.raises(ValueError):
        D8SummaryCandidate.parse(raw, {"frozen-v1", "unranked-v2"}, D8Capacity())
    parsed = D8SummaryCandidate.parse(raw, {"frozen-v1", "unranked-v2"}, D8Capacity(turn_body=1600))
    assert parsed.ordered_refs == ("frozen-v1",) and parsed.open_questions[0][1] == ()
    with pytest.raises(ValueError):
        D8SummaryCandidate.parse({**raw, "ordered_refs": ["frozen-v1", "frozen-v1"]},
                                 {"frozen-v1"}, D8Capacity(turn_body=1600))


def test_v3_explicit_config_does_not_upgrade_v1_v2(tree):
    config = tree[0].config
    assert D3RunConfig.from_dict(json.loads(json.dumps(d3_payload(config)))) == config
    for version in (1, 2):
        with pytest.raises(ValueError):
            replace(config, contract_version=version)


def test_unknown_summary_never_falls_back_or_calls_again(tree):
    def lost(request, raw):
        if request.stage == "handoff_summary":
            raise TimeoutError("response unknown")
        return raw
    tree[1].modify = lost
    assert tree_run(tree).recovery_required
    assert not tree[0].store.list_d8_handoffs(tree[7])
    calls = len(tree[1].calls)
    tree_reopen(tree)
    assert tree_run(tree).recovery_required
    assert len(tree[1].calls) == calls


def test_known_summary_failure_has_labelled_code_fallback(tree):
    def fail(request, raw):
        return ModelError(ModelErrorKind.REFUSED, "deterministic refusal") if request.stage == "handoff_summary" else raw
    tree[1].modify = fail
    assert tree_run(tree).stop_reason == "search_exhausted"
    rows = tree[0].store.list_d8_handoffs(tree[7])
    assert rows and all(row.origin == "trusted_code_fallback" for row in rows)
    for row in rows:
        data = tree[0].handoff.evidence.read("handoff", row.evidence_ref)
        assert data["identity"] == "handoff_information_not_verified"
        assert data["ordered_refs"] == []
        assert set(data["unordered_refs"]) == {i["record_type"] + ":" + i["record_id"] + ":" + i["source_version"] for i in data["all_items"]}


@pytest.mark.parametrize("failure", ["completion", "commit", "reload"])
def test_saved_joint_summary_survives_fault_without_duplicate_call(tree, monkeypatch, failure):
    app = tree[0]
    fired = False
    if failure == "completion":
        original = app.store.complete_d4_operation
        def fail_once(completion):
            nonlocal fired
            operation = app.store.load_d4_operation(completion.operation_id)
            if not fired and operation.start.category == "handoff_summary":
                fired = True
                raise StorageError("local completion fault")
            return original(completion)
        monkeypatch.setattr(app.store, "complete_d4_operation", fail_once)
    elif failure == "commit":
        original = app.controller.commit_d3_transition
        def fail_once(operation, *args, **kwargs):
            nonlocal fired
            if not fired and operation == "append_d8_handoff":
                fired = True
                raise StorageError("local commit fault")
            return original(operation, *args, **kwargs)
        monkeypatch.setattr(app.controller, "commit_d3_transition", fail_once)
    else:
        original = app.controller._reload_after_commit
        def fail_once(commit=None):
            nonlocal fired
            if not fired and commit is not None and commit.event.details["operation"] == "append_d8_handoff":
                fired = True
                raise StorageError("reload interrupted after commit")
            return original(commit)
        monkeypatch.setattr(app.controller, "_reload_after_commit", fail_once)
    assert tree_run(tree).recovery_required and fired
    first_id = next(r.model_request_id for r in tree[1].calls if r.stage == "handoff_summary")
    tree_reopen(tree)
    assert tree_run(tree).stop_reason == "search_exhausted"
    assert sum(r.model_request_id == first_id for r in tree[1].calls) == 1
    rows = tree[0].store.list_d8_handoffs(tree[7])
    assert [r.global_version for r in rows] == list(range(1, len(rows) + 1))
    assert tree[0].store._connection.execute("SELECT COUNT(*) FROM d8_history_index").fetchone()[0] > 0


def test_partial_candidate_dependency_rejection_preserves_valid_sibling(tree):
    def reject_first(request, raw):
        if request.stage == "tree_planning" and request.turn_id is None:
            raw["intents"][0]["dependency_refs"] = ["unknown-version"]
        return raw
    tree[1].modify = reject_first
    assert tree_run(tree).stop_reason == "search_exhausted"
    root = tree[0].state.turns[0]
    assert len(root.intent_ids) == 1
    assert "strategy 1" in tree[0].state.intents[0].description
    request = tree[1].calls[0]
    saved = tree[0].handoff.evidence.read("result", request.model_request_id)
    assert saved["rejected_candidate_indices"] == [0]


def test_all_dependency_candidates_rejected_are_not_exhaustion(tree):
    def reject_all(request, raw):
        if request.stage == "tree_planning" and raw["kind"] == "candidates":
            for candidate in raw["intents"]:
                candidate["dependency_refs"] = ["unseen-version"]
        return raw
    tree[1].modify = reject_all
    assert tree_run(tree).recovery_required
    assert not tree[0].state.turns
    assert not tree[0].store.list_d8_tree_records(tree[7])
    assert len(tree[1].calls) == 2
    assert all(tree[0].handoff.evidence.read("result", r.model_request_id)["reason"] == "d8_all_dependency_candidates_rejected" for r in tree[1].calls)


def test_index_read_replays_exact_query_and_pages_by_frozen_positions(tree):
    assert tree_run(tree).stop_reason == "search_exhausted"
    app, request_id = tree[0], str(uuid4())
    first = app.handoff.read_history(query={}, request_id=request_id)
    assert first["has_more"] and 0 < len(first["items"]) <= 8
    assert len(json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 2048
    before = app.store.d4_budget_usage(tree[7])
    with pytest.raises(D3RecoveryRequired):
        app.handoff.read_history(query={"record_type": "verified_fact"}, request_id=request_id)
    assert app.handoff.read_history(query={}, request_id=request_id) == first
    second = app.handoff.read_history(cursor=first["next_cursor"])
    assert not {i["ref"] for i in first["items"]} & {i["ref"] for i in second["items"]}
    assert app.store.d4_budget_usage(tree[7]) == before
    assert len(app.store.list_d8_reads(tree[7])) == 2


def test_local_read_started_then_interrupted_recovers_once_original_snapshot(tree, monkeypatch):
    assert tree_run(tree).stop_reason == "search_exhausted"
    app, request_id = tree[0], str(uuid4())
    original = app.handoff._page
    def lost(*args):
        raise ProcessLoss("after local read admission")
    monkeypatch.setattr(app.handoff, "_page", lost)
    with pytest.raises(ProcessLoss):
        app.handoff.read_history(query={"record_type": "verified_fact"}, request_id=request_id)
    assert len(app.store.list_d8_reads(tree[7])) == 1
    monkeypatch.setattr(app.handoff, "_page", original)
    with pytest.raises(D3RecoveryRequired):
        app.handoff.read_history(query={"record_type": "verified_fact"}, request_id=request_id)
    old = app.handoff.capacity()
    app.handoff.increase_capacity({"read_return": 4096})
    recovered = app.handoff.resume_read(request_id)
    assert recovered["capacity_version"] == old.version
    assert len(json.dumps(recovered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= old.capacity.read_return
    tree_reopen(tree)
    assert tree[0].handoff.resume_read(request_id) == recovered
    assert len(tree[0].store.list_d8_reads(tree[7])) == 1


def test_local_read_result_repairs_completion_without_re_reading(tree, monkeypatch):
    assert tree_run(tree).stop_reason == "search_exhausted"
    app, request_id = tree[0], str(uuid4())
    original = app.store.complete_d8_read
    monkeypatch.setattr(app.store, "complete_d8_read", lambda *args: (_ for _ in ()).throw(StorageError("completion fault")))
    with pytest.raises(StorageError):
        app.handoff.read_history(query={}, request_id=request_id)
    saved = app.handoff.evidence.read("read-result", request_id)
    monkeypatch.setattr(app.store, "complete_d8_read", original)
    monkeypatch.setattr(app.handoff, "_page", lambda *args: pytest.fail("completed local read must not repeat"))
    assert app.handoff.resume_read(request_id) == saved
    assert app.store.load_d8_read(request_id)[1] is not None


def test_eight_read_starts_are_separate_and_survive_reopen(tree):
    assert tree_run(tree).stop_reason == "search_exhausted"
    before = tree[0].store.d4_budget_usage(tree[7])
    for _ in range(8):
        tree[0].handoff.read_history(query={"record_type": "verified_fact"})
    tree_reopen(tree)
    with pytest.raises(StorageError, match="d8_read_budget_exhausted"):
        tree[0].handoff.read_history(query={})
    assert len(tree[0].store.list_d8_reads(tree[7])) == 8
    assert tree[0].store.d4_budget_usage(tree[7]) == before


def test_inner_can_query_history_before_plan_without_virtual_plan_or_tool(tree):
    inner, original = tree[2], tree[2].invoke
    query_requested = False
    def query_once(request, timeout, token):
        nonlocal query_requested
        if request.stage == "inner_plan" and not query_requested:
            query_requested = True
            return {"kind": "history_query", "query": {"record_type": "intent"}}
        return original(request, timeout, token)
    inner.invoke = query_once
    assert tree_run(tree).stop_reason == "search_exhausted"
    app = tree[0]
    assert len(app.state.plans) == 3 and len(app.state.executions) == 6
    assert len(app.store.list_d8_reads(tree[7])) == 1
    counts = dict(app.store.d4_budget_usage(tree[7]).category_counts)
    assert counts["action"] == 16
    plans = [r for r in inner.calls if r.stage == "inner_plan"]
    assert json.loads(plans[0].history_context)["retrieved"]
    assert not json.loads(plans[1].history_context)["retrieved"]


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_cancel_or_deadline_during_summary_does_not_apply(tree, stop):
    def stopped(request, raw):
        if request.stage == "handoff_summary":
            if stop == "cancel":
                tree[5].is_cancelled = True
            else:
                tree[4].now = tree[0].state.task_run.deadline_at
        return raw
    tree[1].modify = stopped
    tree_run(tree)
    assert not tree[0].store.list_d8_handoffs(tree[7])
    calls = len(tree[1].calls)
    tree_run(tree)
    assert len(tree[1].calls) == calls


def test_capacity_pause_then_explicit_independent_version_resumes(tree):
    def large_goal(request, raw):
        if request.stage == "tree_planning" and request.turn_id is None:
            raw["intents"][0]["description"] = "x" * 12000
        return raw
    tree[1].modify = large_goal
    result = tree_run(tree)
    assert result.recovery_required and tree[0].handoff.pause_report["kind"] == "initial_history"
    assert not tree[0].state.plans and not tree[3].command_calls
    before = tree[0].handoff.capacity()
    version = tree[0].handoff.increase_capacity({"initial_history": 20000})
    assert version.capacity.visible_history == before.capacity.visible_history
    result = tree_run(tree)
    assert result.recovery_required and tree[0].handoff.pause_report["kind"] == "visible_history"
    tree[0].handoff.increase_capacity({"initial_history": 65536, "visible_history": 65536})
    result = tree_run(tree)
    assert result.stop_reason == "search_exhausted"
    assert tree[0].config.d4_budget.total == 60
    with pytest.raises(ValueError):
        tree[0].handoff.increase_capacity({"read_count": 9})


def test_inner_history_request_correction_is_bound_and_charged(tree):
    original, asked = tree[2].invoke, False
    def malformed_once(request, timeout, token):
        nonlocal asked
        if request.stage == "inner_plan" and not asked:
            asked = True
            return {"kind": "history_query", "query": {}, "unexpected": True}
        return original(request, timeout, token)
    tree[2].invoke = malformed_once
    assert tree_run(tree).stop_reason == "search_exhausted"
    app = tree[0]
    corrected = next(call for call in app.store.list_d3_operations(tree[7])
                     if (request := app.model_evidence.read(call.start.operation_id)) is not None
                     and request.correction_of_model_request_id is not None)
    assert app.handoff.evidence.read("execution-context", corrected.start.operation_id) is not None
    assert dict(app.store.d4_budget_usage(tree[7]).category_counts)["action"] == 16


def test_detail_pages_preserve_labels_and_exact_body_ranges(tree):
    def long_root(request, raw):
        if request.stage == "tree_planning" and request.turn_id is None:
            raw["intents"][0]["description"] = "R" * 3000
        return raw
    tree[1].modify = long_root
    tree[0].handoff.increase_capacity({"initial_history": 32768, "visible_history": 32768})
    assert tree_run(tree).stop_reason == "search_exhausted"
    app = tree[0]
    item = next(i for i in app.handoff.history() if i.record_id == app.state.turns[0].intent_ids[0])
    page = app.handoff.read_history(refs=[item.ref])
    content = page["items"][0]["content"]
    assert page["has_more"]
    assert page["items"][0]["end"] < page["items"][0]["total"]
    while page["has_more"]:
        previous_end = page["items"][0]["end"]
        page = app.handoff.read_history(cursor=page["next_cursor"])
        assert page["items"][0]["start"] == previous_end
        assert page["items"][0]["identity"] == "trusted_state"
        content += page["items"][0]["content"]
    assert content == item.content


def test_ninth_required_fact_is_not_dropped_by_normal_target(tree, monkeypatch):
    from agent_core.handoff_contracts import D8HistoryItem
    assert tree_run(tree).stop_reason == "search_exhausted"
    app = tree[0]
    root, intent = app.state.turns[0], app.state.intents[0]
    items = tuple(D8HistoryItem(str(uuid4()), tree[7], "verified_fact", root.turn_id,
                               intent.intent_id, None, "Simulated historical observation for selection unit test.",
                               (), str(n) * 64, 1, "verified_observation_only", "historical", "available")
                  for n in range(1, 10))
    actual = app.handoff.history()
    monkeypatch.setattr(app.handoff, "history", lambda: (*actual, *items))
    monkeypatch.setattr(app.handoff, "_dependencies", lambda _: tuple(i.ref for i in items))
    app.handoff.increase_capacity({"initial_history": 16384})
    selected, _, packet = app.handoff.select("execution", root.turn_id, intent.intent_id)
    assert all(i in selected for i in items)
    # Nine explicit dependencies plus the current Plan's committed support Fact.
    assert sum(i.record_type == "verified_fact" for i in selected) == 10
    assert all(i.ref in packet["required_refs"] for i in selected if i.record_type == "verified_fact")
    assert set(i.ref for i in items) <= set(packet["required_refs"])


def test_known_read_capacity_failure_is_saved_not_left_unknown(tree, monkeypatch):
    from agent_core.handoff_contracts import D8HistoryItem
    assert tree_run(tree).stop_reason == "search_exhausted"
    app = tree[0]
    item = D8HistoryItem(str(uuid4()), tree[7], "progress", None, None, None,
                         "Simulated safe metadata only.", tuple(str(uuid4()) for _ in range(80)),
                         "1" * 64, 1, "trusted_state", "historical", "available")
    monkeypatch.setattr(app.handoff, "history", lambda: (item,))
    identity = str(uuid4())
    failed = app.handoff.read_history(refs=[item.ref], request_id=identity)
    assert failed["error"] == "capacity_paused" and failed["items"] == []
    assert app.store.load_d8_read(identity)[1] is not None
    app.handoff.increase_capacity({"read_return": 8192})
    assert app.handoff.resume_read(identity) == failed
    result = app.handoff.read_history(refs=[item.ref])
    assert result["items"][0]["content"] == item.content
    assert len(app.store.list_d8_reads(tree[7])) == 2


def test_joint_summary_transaction_rolls_back_index_application_and_checkpoint(tree, monkeypatch):
    import agent_core.sqlite_control_store as schema
    original, fired, checkpoint = schema.save_handoff_mutation, False, None
    def fail_inside_transaction(store, mutation, sequence):
        nonlocal fired, checkpoint
        original(store, mutation, sequence)
        if not fired and mutation.operation == "append_d8_handoff":
            fired = True
            checkpoint = mutation.base_checkpoint_sequence
            raise StorageError("fault after summary/index/application inserts")
    monkeypatch.setattr(schema, "save_handoff_mutation", fail_inside_transaction)
    assert tree_run(tree).recovery_required and fired
    app = tree[0]
    assert not app.store.list_d8_handoffs(tree[7])
    assert app.store._connection.execute("SELECT COUNT(*) FROM d8_history_index").fetchone()[0] == 0
    assert app.state.latest_checkpoint.sequence == checkpoint
    request = next(r for r in tree[1].calls if r.stage == "handoff_summary")
    assert app.store.load_d4_operation(request.model_request_id).application_ref is None
    tree_reopen(tree)
    assert tree_run(tree).stop_reason == "search_exhausted"
    assert sum(r.model_request_id == request.model_request_id for r in tree[1].calls) == 1


def test_full_request_capacity_checks_output_reserve_before_admission(tree):
    app = tree[0]
    policy = app.config.handoff_config.outer_capacity
    request, _ = app.handoff._request("tree_planning", None)
    from agent_core.handoff_contracts import handoff_json
    length = len(handoff_json(request))
    assert not FakeUnicodeCapacity().fits(request, replace(policy, total_units=length + policy.output_reserve_units - 1))
    assert FakeUnicodeCapacity().fits(request, replace(policy, total_units=length + policy.output_reserve_units))
    class RefusingCapacity:
        def fits(self, actual, frozen_policy):
            assert frozen_policy == policy
            return False
    app.handoff.capacity_port = RefusingCapacity()
    assert tree_run(tree).recovery_required
    assert not tree[1].calls and not tree[2].calls and not tree[3].command_calls
    assert app.store.d4_budget_usage(tree[7]).total_used == 1  # attachment import only


def test_corrupt_original_read_snapshot_never_switches_to_current_query(tree, monkeypatch):
    assert tree_run(tree).stop_reason == "search_exhausted"
    app, identity = tree[0], str(uuid4())
    original = app.handoff._page
    monkeypatch.setattr(app.handoff, "_page", lambda *args: (_ for _ in ()).throw(ProcessLoss("local interruption")))
    with pytest.raises(ProcessLoss):
        app.handoff.read_history(query={}, request_id=identity)
    monkeypatch.setattr(app.handoff, "_page", original)
    read = app.handoff.evidence.read
    snapshot_id = app.store.load_d8_read(identity)[0]["snapshot_id"]
    def corrupt(kind, key):
        result = read(kind, key)
        return {**result, "positions": [999]} if kind == "read-snapshot" and key == snapshot_id else result
    monkeypatch.setattr(app.handoff.evidence, "read", corrupt)
    monkeypatch.setattr(app.handoff, "history", lambda: pytest.fail("must not query current history"))
    with pytest.raises(D3RecoveryRequired, match="d8_read_snapshot_unavailable"):
        app.handoff.resume_read(identity)
    assert len(app.store.list_d8_reads(tree[7])) == 1
    assert app.store.load_d8_read(identity)[1] is None


def test_all_model_roles_use_capacity_port_and_fact_review_stops_before_admission(tree):
    app, measured = tree[0], []
    class CheckingCapacity:
        refuse = True
        def fits(self, request, policy):
            measured.append(request)
            if self.refuse and getattr(request, "stage", None) == "fact_review":
                return False
            return FakeUnicodeCapacity().fits(request, policy)
    port = CheckingCapacity()
    app.handoff.capacity_port = port
    assert tree_run(tree).recovery_required
    assert not app.state.facts
    assert dict(app.store.d4_budget_usage(tree[7]).category_counts)["fact_review"] == 0
    assert tree[3].digest_calls == 1 and len(tree[3].command_calls) == 2
    port.refuse = False
    assert tree_run(tree).stop_reason == "search_exhausted"
    assert {"fact_proposal", "fact_review", "inner_plan", "inner_size", "inner_sha256", "inner_complete",
            "tree_planning", "handoff_summary", "intent_evaluation"} <= {
                getattr(r, "stage", None) for r in measured}
    assert any(type(r).__name__ == "D2ReviewRequest" for r in measured)
    assert app.refinement.request_capacity_check == app.handoff.check_role_request_capacity
    assert tree[3].digest_calls == 3 and len(tree[3].command_calls) == 6


def test_actual_schema23_readonly_and_upgrade_keep_d8_1_history_and_budget(case):
    import sqlite3
    from agent_core.sqlite_control_store import SqliteControlStore, CURRENT_SCHEMA_VERSION
    assert asyncio.run(case[0].run(case[5])).stop_reason == "turn_completed_sandbox_retained"
    app, task = case[0], case[7]
    config, conclusions = app.config, app.store.list_d8_conclusions(task)
    facts, calls = app.state.facts, app.store.list_d4_operations(task)
    database = app.directory / "control.sqlite"
    app.store.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE d8_conclusion_revisions")
    connection.execute("DELETE FROM schema_versions WHERE version=25")
    for table in ("d8_handoff_versions", "d8_capacity_versions", "d8_tree_records", "d8_history_index", "d8_history_reads"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DELETE FROM schema_versions WHERE version=24")
    connection.commit()
    connection.close()
    original = database.read_bytes()
    readonly = SqliteControlStore.open_readonly(database)
    assert readonly._schema_version == 23
    assert readonly.list_d8_conclusions(task) == conclusions and not readonly.list_d8_handoffs(task)
    readonly.close()
    assert database.read_bytes() == original
    upgraded = SqliteControlStore.open(database, clock=case[4])
    assert upgraded._schema_version == CURRENT_SCHEMA_VERSION
    assert upgraded.load_d3_run_config(task) == config and config.d4_budget.total == 56
    assert upgraded.list_d8_conclusions(task) == conclusions
    assert upgraded.load_control_state_rows(task).facts == facts
    assert upgraded.list_d4_operations(task) == calls
    assert not upgraded.list_d8_capacities(task) and not upgraded.list_d8_handoffs(task)
    assert not upgraded.list_d8_tree_records(task) and not upgraded.list_d8_reads(task)
    upgraded.close()


def test_refinement_capacity_gate_precedes_start_and_model(tmp_path):
    from test_d5_chain import make_d5_scenario, close_scenario
    scenario = make_d5_scenario(tmp_path)
    measured = []
    def stop(request):
        measured.append(request)
        raise D3RecoveryRequired("d8_context_paused:complete_model_request")
    app = scenario[0]
    try:
        # Exercise the existing refinement component's opt-in capacity port;
        # legacy construction does not install it automatically.
        assert app.refinement.request_capacity_check is None
        app.refinement.request_capacity_check = stop
        assert asyncio.run(app.run(scenario[6])).recovery_required
        assert measured and type(measured[0]).__name__ == "D5RefinementRequest"
        assert not scenario[7].calls
        assert dict(app.store.d4_budget_usage(app.state.task_run.task_run_id).category_counts)["refinement"] == 0
    finally:
        close_scenario(scenario)


def test_handoff_evidence_conflict_is_safe_recovery_and_does_not_overwrite(tmp_path):
    from agent_adapters.handoff_evidence import FileD8HandoffEvidence
    evidence, identity = FileD8HandoffEvidence(tmp_path / "safe-evidence"), str(uuid4())
    evidence.save("pause", identity, {"safe": ["original"]})
    evidence.save("pause", identity, {"safe": ("original",)})
    with pytest.raises(D3RecoveryRequired, match="d8_handoff_evidence_write_recovery_required"):
        evidence.save("pause", identity, {"safe": ["changed"]})
    assert evidence.read("pause", identity) == {"safe": ["original"]}
