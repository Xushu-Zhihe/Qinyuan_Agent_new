"""D8 public behavior with Fake roles/Sandbox and real evidence/SQLite."""

import asyncio
import json
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from agent_adapters.multi_turn_application import D3Application
from agent_core.conclusion_contracts import D8Assertion, D8Candidate, parse_d8_proposal
from agent_core.contracts import TaskRequest
from agent_core.domain import IntentStatus, PlanStatus
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig, d3_payload
from agent_core.operation_accounting import D4BudgetPolicy, D8BudgetPolicy
from agent_core.control_store import StorageError
from agent_core.turn_contracts import D1RoleConfig
from test_d3_chain import Clock, Model, ReviewModel, Sandbox, Token


class Inner(Model):
    def __init__(self, role):
        super().__init__(role)
        self.fact_calls = []
        self.response = lambda request: {
            "kind": "candidate", "content": "The observed copy digest matches the frozen import.",
            "reason": "The saved trusted observation proves only this copy version.",
            "evidence_refs": [request.observation.proof_id],
            "assertion": {"rule_id": "attachment_copy_sha256", "rule_version": 1,
                          "artifact_version_id": request.observation.artifact_version_id,
                          "observation_id": request.observation.observation_id},
        }

    def invoke(self, request, timeout_seconds, token):
        if request.stage != "fact_proposal":
            return super().invoke(request, timeout_seconds, token)
        self.fact_calls.append(request)
        return self.response(request)


class FactReviewer:
    def __init__(self, role):
        self.role_config = role
        self.calls = []
        self.response = lambda request: {"decision": "accept", "reason": "Supported in the stated observation scope."}

    def invoke(self, request, timeout, token):
        self.calls.append(request)
        return self.response(request)


class Refinement:
    def __init__(self, role):
        self.role_config = role

    def invoke(self, request, timeout, token):
        return {}  # Known, nonblocking D5 refinement failure; unrelated to D8 acceptance.


@pytest.fixture
def case(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.txt").write_bytes(b"D8 local attachment")
    outer_role = D1RoleConfig("outer_planning", "fake", "same-model", "outer-d8-v1")
    inner_role = D1RoleConfig("inner_action", "fake", "same-model", "inner-d8-v1")
    config = D3RunConfig(outer_role, inner_role, D3Authorization(str(uuid4()), (0,)),
                         outer_role.role_config_ref, d4_budget=D8BudgetPolicy(),
                         d5_enabled=True, d5_sensitive_requirements=(), d8_enabled=True)
    sandbox, clock, token = Sandbox(), Clock(), Token()
    outer, inner, reviews = Model(outer_role), Inner(inner_role), ReviewModel(outer_role)
    outer.candidates = 1
    fact_reviewer = FactReviewer(outer_role)

    def create():
        return D3Application(tmp_path / "run", config=config, sandbox=sandbox,
                             import_sandbox=sandbox, workspace_sandbox=sandbox,
                             command_sandbox=sandbox, outer_model=outer, inner_model=inner,
                             review_model=reviews, fact_review_model=fact_reviewer,
                             refinement_model=Refinement(outer_role), clock=clock)

    app = create()
    task_id = app.start(TaskRequest.from_dict({"goal": "Verify the authorized copy",
                                               "target": {"attachments": [{"kind": "local_path", "path": "sample.txt"}]}}), source)
    value = [app, sandbox, inner, fact_reviewer, clock, token, create, task_id, reviews]
    yield value
    value[0].store.close()


def run(case):
    return asyncio.run(case[0].run(case[5]))


def reopen(case):
    case[0].store.close()
    case[0] = case[6]()
    case[0].controller.attach(case[7])
    case[0]._bound_task_run_id = case[7]
    return case[0]


def test_verified_fact_requires_outer_review_and_keeps_observation_scope(case):
    result = run(case)
    app, sandbox, inner, reviewer = case[:4]
    assert result.stop_reason == "turn_completed_sandbox_retained"
    assert len(app.state.facts) == len(reviewer.calls) == len(inner.fact_calls) == 1
    fact = app.state.facts[0]
    assert "First saved observation" in fact.content and "Whole task not evaluated" in fact.content
    assert len(fact.content) <= 256
    record, = app.store.list_d8_conclusions(case[7])
    assert record.outcome == "verified_fact" and record.fact_id == fact.fact_id
    assert app.store.load_d4_operation(record.review_request_id).application_ref == record.record_id
    assert sandbox.digest_calls == 1 and len(sandbox.command_calls) == 2
    counts = dict(app.store.d4_budget_usage(case[7]).category_counts)
    assert counts["fact_review"] == 1 and counts["action"] == 5
    assert app.state.task_run.status.value == "running"


@pytest.mark.parametrize("decision", ["reject", "insufficient"])
def test_refusal_feedback_then_revision_does_not_repeat_tools(case, decision):
    reviewer = case[3]
    reviewer.response = lambda request: {"decision": decision if len(reviewer.calls) == 1 else "accept",
                                          "reason": "Narrow the conclusion to the saved copy observation."}
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    records = case[0].store.list_d8_conclusions(case[7])
    assert [r.outcome for r in records] == ["rejected" if decision == "reject" else "insufficient", "verified_fact"]
    assert case[2].fact_calls[1].feedback[0].reason == "Narrow the conclusion to the saved copy observation."
    assert len(case[0].state.facts) == 1
    assert case[1].digest_calls == 1 and len(case[1].command_calls) == 2


def test_reviewed_inference_is_separate_readonly_and_cannot_complete_intent(case):
    inner = case[2]
    original = inner.response
    def inference(request):
        if len(inner.fact_calls) == 1:
            return {**original(request), "assertion": None, "content": "A second independent check may be useful."}
        raise TimeoutError("response unknown")
    inner.response = inference
    result = run(case)
    app = case[0]
    assert result.recovery_required
    assert not app.state.facts and all(i.status is not IntentStatus.SUCCEEDED for i in app.state.intents)
    assert app.state.plans[-1].status is PlanStatus.RUNNING
    before = app.store._connection.total_changes
    rows = app.conclusions.read_inferences(case[7])
    assert rows[0].record.outcome == "reviewed_inference"
    assert rows[0].source_status == "available" and rows[0].scope_status == "active"
    assert rows[0].candidate.content == "A second independent check may be useful."
    assert app.store._connection.total_changes == before


def test_registered_assertion_failure_is_not_downgraded_to_inference(case):
    inner = case[2]
    original = inner.response
    def wrong_subject(request):
        candidate = original(request)
        if len(inner.fact_calls) == 1:
            candidate["assertion"]["artifact_version_id"] = str(uuid4())
        return candidate
    inner.response = wrong_subject
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    assert [r.outcome for r in case[0].store.list_d8_conclusions(case[7])] == ["rejected", "verified_fact"]
    assert not case[0].conclusions.read_inferences(case[7])


def test_unknown_review_is_not_reissued_after_reopen(case):
    reviewer = case[3]
    def unknown(_):
        raise TimeoutError("lost model result")
    reviewer.response = unknown
    result = run(case)
    assert result.recovery_required and len(reviewer.calls) == 1 and not case[0].state.facts
    reopen(case)
    assert run(case).recovery_required
    assert len(reviewer.calls) == 1 and case[1].digest_calls == 1
    assert not case[0].store.list_d8_conclusions(case[7])


def test_one_format_correction_is_charged_and_can_succeed(case):
    reviewer = case[3]
    reviewer.response = lambda request: {"decision": "accept", "reason": "Supported."} if request.correction_of_model_request_id else {"decision": "accept"}
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    assert len(reviewer.calls) == 2
    assert dict(case[0].store.d4_budget_usage(case[7]).category_counts)["fact_review"] == 2


def test_second_format_failure_pauses_and_is_not_retried(case):
    case[3].response = lambda request: {"decision": "accept"}
    assert run(case).recovery_required
    assert len(case[3].calls) == 2 and not case[0].state.facts
    reopen(case)
    assert run(case).recovery_required and len(case[3].calls) == 2


def test_wrong_source_is_refused_before_outer_review(case):
    original = case[2].response
    case[2].response = lambda request: {**original(request), "evidence_refs": [str(uuid4())]}
    assert run(case).recovery_required
    assert not case[3].calls and not case[0].state.facts


def test_old_budget_and_serialization_remain_explicit(case):
    config = case[0].config
    assert D3RunConfig.from_dict(json.loads(json.dumps(d3_payload(config)))) == config
    legacy = replace(config, d4_budget=D4BudgetPolicy(), d8_enabled=False)
    assert "d8_enabled" not in d3_payload(legacy)
    assert D3RunConfig.from_dict(json.loads(json.dumps(d3_payload(legacy)))) == legacy
    with pytest.raises(ValueError):
        D4BudgetPolicy.from_dict(asdict(D8BudgetPolicy()))
    with pytest.raises(ValueError):
        replace(config, d8_enabled=False)
    with pytest.raises(ValueError):
        replace(config, d4_budget=D4BudgetPolicy())


@pytest.mark.parametrize("update", [{"content": "x" * 257}, {"reason": "x" * 513},
                                     {"evidence_refs": "not-an-array"}, {"assertion": {"rule_id": "unsupported"}}])
def test_invalid_candidate_contract_is_not_truncated(update):
    raw = {"kind": "candidate", "content": "Observation", "reason": "Evidence",
           "evidence_refs": [str(uuid4())], "assertion": None, **update}
    with pytest.raises((ValueError, TypeError)):
        parse_d8_proposal(raw)


def test_unknown_assertion_version_cannot_become_inference():
    with pytest.raises(ValueError):
        D8Assertion("attachment_copy_sha256", 2, str(uuid4()), str(uuid4()))
    candidate = D8Candidate("Observation", "Evidence", (str(uuid4()),), None)
    assert D8Candidate.from_dict(json.loads(json.dumps(asdict(candidate)))) == candidate


@pytest.mark.parametrize("boundary", ["completion", "commit", "reload"])
def test_saved_review_survives_persistence_interruption_without_recalling_models(case, monkeypatch, boundary):
    app = case[0]
    if boundary == "completion":
        original = app.store.complete_d4_operation
        def interrupted(value):
            call = app.store.load_d4_operation(value.operation_id)
            if call.start.category == "fact_review":
                raise StorageError("injected completion interruption")
            return original(value)
        monkeypatch.setattr(app.store, "complete_d4_operation", interrupted)
    elif boundary == "commit":
        original = app.controller.commit_d3_transition
        def interrupted(operation, *args, **kwargs):
            if operation == "complete_d3_plan":
                raise StorageError("injected commit interruption")
            return original(operation, *args, **kwargs)
        monkeypatch.setattr(app.controller, "commit_d3_transition", interrupted)
    else:
        original = app.controller._reload_after_commit
        def interrupted(commit):
            records = app.store.list_d8_conclusions(case[7])
            if records:
                from agent_core.controller import CommittedStateReloadError
                raise CommittedStateReloadError("injected reload interruption")
            return original(commit)
        monkeypatch.setattr(app.controller, "_reload_after_commit", interrupted)
    assert run(case).recovery_required
    assert len(case[3].calls) == 1
    reopen(case)
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    assert len(case[3].calls) == len(case[2].fact_calls) == 1
    assert len(case[0].state.facts) == len(case[0].store.list_d8_conclusions(case[7])) == 1
    assert case[1].digest_calls == 1 and len(case[1].command_calls) == 2


def test_review_budget_is_separate_cumulative_and_not_reset_by_reopen(case):
    case[3].response = lambda request: {"decision": "reject", "reason": "Unsupported conclusion."}
    result = run(case)
    assert result.stop_reason.startswith("budget_exhausted:")
    assert len(case[3].calls) == 8 and not case[0].state.facts
    counts = dict(case[0].store.d4_budget_usage(case[7]).category_counts)
    assert counts["fact_review"] == 8 and counts["review"] == 1
    previous_proposals = len(case[2].fact_calls)
    reopen(case)
    assert run(case).stop_reason.startswith("budget_exhausted:")
    assert len(case[3].calls) == 8 and len(case[2].fact_calls) == previous_proposals


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_cancel_and_deadline_during_review_never_apply_approval(case, stop):
    def stop_then_accept(request):
        if stop == "cancel":
            case[5].is_cancelled = True
        else:
            case[4].now = case[0].state.task_run.deadline_at + 1
        return {"decision": "accept", "reason": "Supported."}
    case[3].response = stop_then_accept
    run(case)
    assert not case[0].state.facts and not case[0].store.list_d8_conclusions(case[7])
    assert len(case[3].calls) == 1
    assert not any(r.stage == "inner_complete" for r in case[2].calls)


def test_sensitive_candidate_is_not_saved_or_sent_to_outer(case):
    original = case[2].response
    case[2].response = lambda request: {**original(request), "content": "password=example-d8-sensitive"}
    assert run(case).stop_reason == "d8_model_sensitive_rejected"
    assert not case[3].calls and not case[0].state.facts
    files = list((case[0].directory / "d8-conclusions").rglob("*.json"))
    assert files and all(b"example-d8-sensitive" not in p.read_bytes() for p in files)


def test_changed_candidate_binding_cannot_reuse_approval(case, monkeypatch):
    original = case[0].conclusions.propose
    async def changed(*args):
        proposal_id, record = await original(*args)
        return proposal_id, replace(record, candidate_sha256="0" * 64)
    monkeypatch.setattr(case[0].conclusions, "propose", changed)
    assert run(case).stop_reason == "d8_approval_binding_changed"
    assert not case[0].state.facts and not case[0].store.list_d8_conclusions(case[7])


def test_inner_can_request_existing_review_and_replan_after_inference(case):
    original = case[2].response
    def choose(request):
        if len(case[2].fact_calls) == 1:
            return {**original(request), "assertion": None, "content": "Another independent observation may help."}
        if len(case[2].fact_calls) == 2:
            return {"kind": "request_review", "reason": "Request a new short Plan."}
        return original(request)
    case[2].response = choose
    case[8].decision_for = lambda request: "replan_current_intent" if request.review_scope == "action" else "continue_current_intent"
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    assert len(case[0].state.plans) == 2
    assert case[0].state.plans[0].status is PlanStatus.SUPERSEDED
    assert case[1].digest_calls == 2  # Explicitly reviewed new Plan, not a replay of the old tool.
    assert [r.outcome for r in case[0].store.list_d8_conclusions(case[7])] == ["reviewed_inference", "verified_fact"]
    before = case[0].store._connection.total_changes
    view, = case[0].conclusions.read_inferences(case[7])
    assert view.source_status == "available" and view.scope_status == "ended"
    assert view.candidate.content == "Another independent observation may help."
    path = case[0].conclusions.evidence.records._path("d8-result", view.record.review_request_id)
    path.write_bytes(b"{corrupt history")
    unavailable, = case[0].conclusions.read_inferences(case[7])
    assert unavailable.source_status == "unavailable" and unavailable.candidate is None
    assert case[0].store._connection.total_changes == before


def test_schema22_readonly_and_compatible_migration_do_not_enable_d8(tmp_path):
    import sqlite3
    import agent_core.sqlite_control_store as schema
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    for version in range(1, 23):
        for statement in schema._sql_statements(getattr(schema, f"MIGRATION_{version}")):
            connection.execute(statement)
        connection.execute("INSERT INTO schema_versions VALUES (?, ?)", (version, 1_800_000_000))
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with schema.SqliteControlStore.open_readonly(path) as store:
        assert store._schema_version == 22
        assert store.list_d8_conclusions(str(uuid4())) == ()
    assert path.read_bytes() == before
    with schema.SqliteControlStore.open(path, clock=Clock()) as store:
        assert store._schema_version == schema.CURRENT_SCHEMA_VERSION
        assert not store.list_d8_conclusions(str(uuid4()))
        assert not store.list_task_run_refs()


@pytest.mark.parametrize("changed_field", ["task_run_id", "turn_id", "plan_id", "source_version_sha256"])
def test_foreign_or_changed_scope_cannot_reuse_approval(case, monkeypatch, changed_field):
    original = case[0].conclusions.propose
    async def changed(*args):
        proposal_id, record = await original(*args)
        value = "0" * 64 if changed_field.endswith("sha256") else str(uuid4())
        return proposal_id, replace(record, **{changed_field: value})
    monkeypatch.setattr(case[0].conclusions, "propose", changed)
    assert run(case).recovery_required
    assert not case[0].state.facts and not case[0].store.list_d8_conclusions(case[7])


def test_corrupt_saved_review_does_not_apply_or_recall_model(case, monkeypatch):
    app = case[0]
    original = app.store.complete_d4_operation
    def interrupted(value):
        call = app.store.load_d4_operation(value.operation_id)
        if call.start.category == "fact_review":
            path = app.conclusions.evidence.records._path("d8-result", value.operation_id)
            path.write_bytes(b"{corrupt evidence")
            raise StorageError("injected interrupted completion")
        return original(value)
    monkeypatch.setattr(app.store, "complete_d4_operation", interrupted)
    assert run(case).recovery_required
    reopen(case)
    assert run(case).recovery_required
    assert len(case[3].calls) == 1 and not case[0].state.facts


def test_all_nine_limits_sum_to_56_and_stay_charged_after_reopen(case):
    from agent_core.operation_accounting import D4BudgetExhausted, D4OperationCompletion, D4OperationStart
    app = case[0]
    limits = app.config.d4_budget.category_limits
    assert sum(limits.values()) == 56
    for category, limit in limits.items():
        role = app.config.outer_role if category in {"planning", "review", "refinement", "fact_review"} else app.config.inner_role if category == "action" else None
        for index in range(limit):
            operation_id = str(uuid4())
            start = D4OperationStart(operation_id, case[7], category, case[4].now,
                                     app.state.latest_checkpoint.sequence, "0" * 64,
                                     f"budget/{category}/{index}", app.config.d4_budget.budget_policy_ref,
                                     role.role if role else None, role.role_config_ref if role else None)
            app.store.begin_d4_operation(start)
            app.store.complete_d4_operation(D4OperationCompletion(operation_id, case[4].now,
                                                                  "failed", operation_id, "0" * 64))
    before = app.store.d4_budget_usage(case[7])
    assert before.total_used == 56 and dict(before.category_counts) == limits
    reopen(case)
    assert case[0].store.d4_budget_usage(case[7]) == before
    with pytest.raises(D4BudgetExhausted):
        case[0].store.begin_d4_operation(replace(start, operation_id=str(uuid4())))
    assert not case[3].calls and not case[2].calls


def test_fact_gate_preserves_original_action_review_request(case):
    case[2].review_when = lambda request: request.stage == "inner_sha256"
    assert run(case).stop_reason == "turn_completed_sandbox_retained"
    action_reviews = [r for r in case[8].calls if r.review_scope == "action"]
    assert len(action_reviews) == 1 and len(case[3].calls) == 1
    assert len(case[0].state.facts) == 1
    assert case[1].digest_calls == 1 and len(case[1].command_calls) == 2


def test_controller_refuses_fact_without_atomic_approval_record(case, monkeypatch):
    original = case[0].control.complete_plan
    def without_approval(binding, result, execution_id, *, conclusion, content):
        return original(binding, result, execution_id, content=content)
    monkeypatch.setattr(case[0].control, "complete_plan", without_approval)
    assert run(case).stop_reason == "d8_fact_approval_missing"
    assert not case[0].state.facts and not case[0].store.list_d8_conclusions(case[7])
    assert case[0].state.plans[-1].status is PlanStatus.RUNNING


def test_schema22_migration_preserves_legacy_run_config_facts_and_calls(tmp_path):
    import sqlite3
    from agent_core.sqlite_control_store import SqliteControlStore, CURRENT_SCHEMA_VERSION
    from test_d4_chain import close_scenario, make_d4_scenario, reopen_scenario
    legacy = make_d4_scenario(tmp_path, attachment_count=1)
    try:
        app = legacy[0]
        assert asyncio.run(app.run(legacy[6])).stop_reason == "turn_completed_sandbox_retained"
        task_id = app.state.task_run.task_run_id
        config = app.store.load_d3_run_config(task_id)
        facts = app.state.facts
        operations = app.store.list_d4_operations(task_id)
        path = app.directory / "control.sqlite"
        app.store.close()
        # Remove the exact newer additions to recreate an actual schema22 database.
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE d8_refinement_process_facts")
            connection.execute("DROP TABLE d8_refinement_corrections")
            connection.execute("DELETE FROM schema_versions WHERE version=27")
            connection.execute("DROP TABLE d8_decisions")
            connection.execute("DELETE FROM schema_versions WHERE version=26")
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE d8_conclusion_revisions")
            connection.execute("DELETE FROM schema_versions WHERE version=25")
            for table in ("d8_handoff_versions", "d8_capacity_versions", "d8_tree_records", "d8_history_index", "d8_history_reads"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DELETE FROM schema_versions WHERE version=24")
            connection.execute("DROP TABLE d8_conclusions")
            connection.execute("DELETE FROM schema_versions WHERE version=23")
        before = path.read_bytes()
        with SqliteControlStore.open_readonly(path) as readonly:
            assert readonly._schema_version == 22
            assert readonly.load_d3_run_config(task_id) == config
            assert readonly.list_d4_operations(task_id) == operations
            assert readonly.list_d8_conclusions(task_id) == ()
        assert path.read_bytes() == before
        app = reopen_scenario(legacy)
        assert app.store._schema_version == CURRENT_SCHEMA_VERSION and app.conclusions is None
        assert app.state.facts == facts and app.store.load_d3_run_config(task_id) == config
        assert app.store.list_d4_operations(task_id) == operations
        assert config.d4_budget.total == 48 and not config.d8_enabled
        assert not app.store.list_d8_conclusions(task_id)
        calls_before = len(legacy[2].calls) + len(legacy[3].calls)
        assert asyncio.run(app.run(legacy[6])).stop_reason == "turn_completed_sandbox_retained"
        assert len(legacy[2].calls) + len(legacy[3].calls) == calls_before
    finally:
        close_scenario(legacy)
