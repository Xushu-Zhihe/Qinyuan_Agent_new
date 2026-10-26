"""Controlled U2 persistence/audit boundaries; no providers, Docker or targets."""

import hashlib
import sqlite3
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.control_store import (
    ControlMutation, StaleControlStateError, StorageError,
    UpsertTaskRun, canonical_json,
)
from agent_core.domain import IntentStatus
from agent_core.handoff_contracts import D8HistoryItem
from agent_core.operation_accounting import (
    D4OperationCompletion, D4OperationConflict, D4OperationStart, D4RecoveryRequired,
    D8HandoffBudgetPolicy,
)
from agent_core.runtime import RuntimeEvent, TaskRunStatus
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.stage_contracts import (
    PlanningExhausted, PlanningSession, RoadCandidate, RoadResolution, RoadSnapshot,
    StageModelEvidence, StageReviewBinding, StageRunConfig, StageStateChange,
    StageTurnSnapshot, TurnAchievement, TurnCompletion, TurnIntent, TurnPlanningReview,
    TurnPlanningState,
)
from agent_core.stage_storage import StageStateError, proposal_digest
from agent_core.turn_contracts import D1RoleConfig


def uid():
    return str(uuid4())


class Evidence:
    def __init__(self):
        self.outputs = {}
        self.history = {}

    def read_model_result(self, operation):
        return self.outputs[operation.start.operation_id]

    def read_history(self, task_id, refs):
        return tuple(self.history[r] for r in refs)


class Case:
    def __init__(self, path):
        self.path = path
        self.now = 1800000000.0
        self.evidence = Evidence()
        self.store = SqliteControlStore.open(path, clock=lambda: self.now, stage_evidence=self.evidence)
        self.task_id, self.root_id = uid(), uid()
        self.request = TaskRequest.from_dict({"goal": "independently verify the result", "deadline_seconds": 3600,
            "target": {"endpoints": [{"url": "http://example.test:8080"}]}})
        self.config = StageRunConfig(
            D1RoleConfig("outer_planning", "fake", "outer", "outer"),
            D1RoleConfig("inner_action", "fake", "inner", "inner"), D8HandoffBudgetPolicy())

    @property
    def sequence(self):
        rows = self.store.load_control_state_rows(self.task_id)
        return rows.latest_checkpoint.sequence if rows else 0

    def start(self, *, stage=True, d3=None):
        task = UpsertTaskRun(self.task_id, self.request, ModelBackend.CODEX_CLI, self.now,
            self.now + 3600, TaskRunStatus.RUNNING, "fake-sandbox")
        changes = (task,)
        if stage:
            changes += (StageStateChange(config=self.config,
                sessions=(PlanningSession("turn_planning", self.root_id),)),)
        mutation = ControlMutation.create(task_run_id=self.task_id, mutation_id=uid(), operation="start_task_run",
            request=self.request, effective_model_backend=ModelBackend.CODEX_CLI, base_checkpoint_sequence=0,
            state_changes=changes, d3=d3, event=RuntimeEvent("task_run_started", self.task_id, self.now,
                TaskRunStatus.RUNNING, {"operation": "start_task_run", "entity_ids": [self.task_id], "result": self.task_id}))
        self.store.commit(mutation)

    def operation(self, session, value, *, category="planning", review_of=None):
        role = self.config.outer_role if category == "review" else self.config.inner_role
        operation = D4OperationStart(uid(), self.task_id, category, self.now, self.sequence,
            "a" * 64, f"session/{session.scope}/{session.owner_id}", self.config.budget.budget_policy_ref,
            role.role, role.role_config_ref)
        self.store.begin_d4_operation(operation)
        digest = hashlib.sha256(canonical_json(asdict(value) if not isinstance(value, tuple)
            else [asdict(item) for item in value]).encode()).hexdigest()
        self.evidence.outputs[operation.operation_id] = StageModelEvidence(session, operation.context_sha256,
            digest, value, review_of[0] if review_of else None, proposal_digest(review_of[1]) if review_of else None)
        self.store.complete_d4_operation(D4OperationCompletion(operation.operation_id, self.now,
            "completed", "evidence/" + operation.operation_id, digest))
        return operation.operation_id

    def reviewed(self, session, proposal, *, indices=(), decision="accept", category="planning"):
        proposal_id = self.operation(session, proposal, category=category)
        review_id = self.operation(session, TurnPlanningReview(decision, "supported", (), indices),
            category="review", review_of=(proposal_id, proposal))
        return StageReviewBinding(proposal_id, review_id)

    def mutation(self, change, *, mutation_id=None, sequence=None):
        return ControlMutation.create(task_run_id=self.task_id, mutation_id=mutation_id or uid(),
            operation="apply_stage_state", request=self.request, effective_model_backend=ModelBackend.CODEX_CLI,
            base_checkpoint_sequence=self.sequence if sequence is None else sequence, state_changes=(change,),
            event=RuntimeEvent("control_state_committed", self.task_id, self.now, TaskRunStatus.RUNNING,
                {"operation": "apply_stage_state", "entity_ids": [t.turn_id for t in change.turns], "result": "applied"}))

    def apply(self, change):
        return self.store.commit(self.mutation(change))

    def root(self):
        intent = TurnIntent("obtain a readable archive", "open the expected file")
        binding = self.reviewed(PlanningSession("turn_planning", self.root_id), intent)
        turn = StageTurnSnapshot(self.root_id, intent, TurnPlanningState.ACTIVE)
        self.apply(StageStateChange(turns=(turn,), reviews=(binding,)))
        return turn

    def roads(self, turn):
        candidates = (RoadCandidate("inspect the format"), RoadCandidate("inspect the producer"))
        binding = self.reviewed(PlanningSession("turn_planning", turn.turn_id), candidates, indices=(0, 1))
        turn = replace(turn, roads=tuple(RoadSnapshot(uid(), item, IntentStatus.PENDING) for item in candidates))
        self.apply(StageStateChange(turns=(turn,), reviews=(binding,)))
        return turn

    def fact(self, turn_id):
        item = D8HistoryItem(uid(), self.task_id, "verified_fact", turn_id, None, None,
            "the archive is readable", ("observation",), "b" * 64, self.sequence,
            "source-identity", "current", "available")
        self.evidence.history[item.ref] = item
        return item.ref


@pytest.fixture
def case(tmp_path):
    case = Case(tmp_path / "control.sqlite3")
    case.start()
    yield case
    case.store.close()


def test_roundtrip_replay_and_session_scope(case):
    turn = case.roads(case.root())
    session = PlanningSession("turn_planning", turn.turn_id)
    logical_id = case.store.load_stage_session(case.task_id, session)
    mutation = case.mutation(StageStateChange(turns=(turn,)))
    first = case.store.commit(mutation)
    assert case.store.commit(mutation).replayed
    assert case.sequence == first.checkpoint.sequence
    case.store.close()
    case.store = SqliteControlStore.open(case.path, clock=lambda: case.now, stage_evidence=case.evidence)
    assert case.store.load_turn(case.task_id, turn.turn_id) == turn
    assert case.store.load_stage_session(case.task_id, session) == logical_id
    road_session = PlanningSession("road_execution", turn.roads[0].road_id)
    assert case.store.load_stage_session(case.task_id, road_session) != logical_id


def test_child_registration_is_atomic_and_keeps_achievement(case):
    parent = case.root()
    fact = case.fact(parent.turn_id)
    completion = TurnCompletion("opened the expected file", (fact,))
    binding = case.reviewed(PlanningSession("turn_planning", parent.turn_id), completion, category="action")
    parent = replace(parent, fact_refs=(fact,), achievement=TurnAchievement(completion, binding.review_operation_id))
    case.apply(StageStateChange(turns=(parent,), reviews=(binding,)))
    child = StageTurnSnapshot(uid(), TurnIntent("parse the archive", "recover a valid record", (fact,)),
        TurnPlanningState.ACTIVE, parent.turn_id)
    child_binding = case.reviewed(PlanningSession("turn_planning", parent.turn_id), child.intent)
    waiting = replace(parent, planning_state=TurnPlanningState.WAITING_CHILD)
    before = case.sequence
    case.store._connection.execute("CREATE TRIGGER inject_failure BEFORE INSERT ON stage_turns "
        f"WHEN NEW.turn_id='{child.turn_id}' BEGIN SELECT RAISE(ABORT,'injected'); END")
    mutation = case.mutation(StageStateChange(turns=(waiting, child), reviews=(child_binding,)))
    with pytest.raises(StorageError):
        case.store.commit(mutation)
    assert case.sequence == before
    assert case.store.load_turn(case.task_id, parent.turn_id) == parent
    assert case.store.load_turn(case.task_id, child.turn_id) is None
    assert case.store.load_d4_operation(child_binding.review_operation_id).application_ref is None
    case.store._connection.execute("DROP TRIGGER inject_failure")
    case.store.commit(mutation)
    assert case.store.load_turn(case.task_id, parent.turn_id).achievement == parent.achievement
    assert case.store.load_turn(case.task_id, child.turn_id) == child


@pytest.mark.parametrize("field", ["intent", "parent_turn_id", "achievement"])
def test_immutable_turn_fields(case, field):
    turn = case.root()
    if field == "achievement":
        ref = case.fact(turn.turn_id)
        completion = TurnCompletion("opened", (ref,))
        binding = case.reviewed(PlanningSession("turn_planning", turn.turn_id), completion, category="action")
        turn = replace(turn, achievement=TurnAchievement(completion, binding.review_operation_id))
        case.apply(StageStateChange(turns=(turn,), reviews=(binding,)))
        value = None
    elif field == "intent":
        value = replace(turn.intent, completion_criterion="a cookie exists")
    else:
        value = uid()
    with pytest.raises(StageStateError, match="immutable_turn"):
        case.apply(StageStateChange(turns=(replace(turn, **{field: value}),)))


@pytest.mark.parametrize("defect", ["review_source", "proposal_digest", "scope", "source_unavailable", "cross_task_source", "checkpoint"])
def test_review_and_history_bindings(case, defect):
    turn = case.root()
    ref = case.fact(turn.turn_id)
    completion = TurnCompletion("opened", (ref,))
    binding = case.reviewed(PlanningSession("turn_planning", turn.turn_id), completion, category="action")
    evidence = case.evidence.outputs[binding.review_operation_id]
    if defect == "review_source":
        evidence = replace(evidence, evidence_sha256="c" * 64)
    elif defect == "proposal_digest":
        evidence = replace(evidence, proposal_sha256="c" * 64)
    elif defect == "scope":
        evidence = replace(evidence, session=PlanningSession("road_execution", uid()))
    elif defect == "source_unavailable":
        case.evidence.history[ref] = replace(case.evidence.history[ref], source_status="unavailable")
    elif defect == "checkpoint":
        record = case.store.load_d4_operation(binding.proposal_operation_id)
        stale = replace(record.start, base_checkpoint_sequence=case.sequence - 1)
        case.store._connection.execute("UPDATE d4_operations SET start_json=? WHERE operation_id=?",
            (canonical_json(asdict(stale)), stale.operation_id))
    else:
        case.evidence.history[ref] = replace(case.evidence.history[ref], task_run_id=uid())
    case.evidence.outputs[binding.review_operation_id] = evidence
    with pytest.raises(StageStateError):
        case.apply(StageStateChange(turns=(replace(turn,
            achievement=TurnAchievement(completion, binding.review_operation_id)),), reviews=(binding,)))
    assert case.store.load_turn(case.task_id, turn.turn_id).achievement is None


def test_role_admission_is_scoped_and_unknown_is_not_replayed(case):
    session = PlanningSession("turn_planning", case.root_id)
    wrong = D4OperationStart(uid(), case.task_id, "planning", case.now, case.sequence, "a" * 64,
        "root", case.config.budget.budget_policy_ref, "outer_planning", case.config.outer_role.role_config_ref)
    with pytest.raises(D4OperationConflict, match="registered framework"):
        case.store.begin_d4_operation(wrong)
    with pytest.raises(D4OperationConflict, match="frozen configuration"):
        case.store.begin_d4_operation(replace(wrong, role="inner_action"))
    inner = replace(wrong, role="inner_action", role_config_ref=case.config.inner_role.role_config_ref)
    assert case.store.begin_d4_operation(inner) is None
    with pytest.raises(D4RecoveryRequired):
        case.store.begin_d4_operation(inner)
    before = case.sequence
    with pytest.raises(StageStateError, match="pending_operations"):
        case.apply(StageStateChange(sessions=(session,)))
    assert case.sequence == before


def test_ordinary_task_cannot_admit_stage_planning_or_stage_writes(tmp_path):
    case = Case(tmp_path / "plain.sqlite3")
    try:
        case.start(stage=False)
        operation = D4OperationStart(uid(), case.task_id, "planning", case.now, case.sequence,
            "a" * 64, "root", case.config.budget.budget_policy_ref, "inner_action", case.config.inner_role.role_config_ref)
        with pytest.raises(D4OperationConflict):
            case.store.begin_d4_operation(operation)
        with pytest.raises(StageStateError, match="run_unsupported"):
            case.apply(StageStateChange(sessions=(PlanningSession("turn_planning", case.root_id),)))
        assert case.sequence == 1
    finally:
        case.store.close()


def test_stale_and_conflicting_mutations(case):
    turn = case.root()
    a = case.mutation(StageStateChange(turns=(turn,)))
    b = case.mutation(StageStateChange(sessions=(PlanningSession("turn_planning", uid()),)))
    case.store.commit(a)
    with pytest.raises(StaleControlStateError):
        case.store.commit(b)
    altered = case.mutation(StageStateChange(sessions=(PlanningSession("turn_planning", uid()),)),
        mutation_id=a.mutation_id, sequence=a.base_checkpoint_sequence)
    with pytest.raises(D4OperationConflict, match="control commit start differs"):
        case.store.commit(altered)
    assert case.sequence == a.checkpoint.sequence


def test_suspended_road_is_not_exhaustion(case):
    turn = case.roads(case.root())
    a, b = turn.roads
    turn = replace(turn, roads=(replace(a, status=IntentStatus.RUNNING), b))
    case.apply(StageStateChange(turns=(turn,)))
    turn = replace(turn, roads=(replace(turn.roads[0], status=IntentStatus.SUSPENDED), b))
    case.apply(StageStateChange(turns=(turn,)))
    binding = case.reviewed(PlanningSession("turn_planning", turn.turn_id), PlanningExhausted("no routes", ()))
    with pytest.raises(StageStateError, match="open_roads"):
        case.apply(StageStateChange(turns=(replace(turn, planning_state=TurnPlanningState.EXHAUSTED),), reviews=(binding,)))
    with pytest.raises(StageStateError, match="accepted_review_not_applied"):
        case.apply(StageStateChange(reviews=(binding,)))
    assert case.store.load_d4_operation(binding.review_operation_id).application_ref is None


def test_closed_road_is_not_reopened(case):
    turn = case.roads(case.root())
    a, b = turn.roads
    turn = replace(turn, roads=(replace(turn.roads[0], status=IntentStatus.RUNNING), b))
    case.apply(StageStateChange(turns=(turn,)))
    ref = case.fact(turn.turn_id)
    verdict = RoadResolution(IntentStatus.SUCCEEDED, "opened", (ref,))
    binding = case.reviewed(PlanningSession("road_execution", a.road_id), verdict, category="action")
    turn = replace(turn, roads=(replace(turn.roads[0], status=IntentStatus.SUCCEEDED), b))
    case.apply(StageStateChange(turns=(turn,), reviews=(binding,)))
    with pytest.raises(StageStateError, match="closed_or_changed"):
        case.apply(StageStateChange(turns=(replace(turn, roads=(replace(turn.roads[0], status=IntentStatus.RUNNING), b)),)))


def test_reorder_preserves_rows_and_non_pending_road_position(case):
    turn = case.roads(case.root())
    candidates = tuple(r.candidate for r in turn.roads)
    binding = case.reviewed(PlanningSession("turn_planning", turn.turn_id), candidates, indices=(1, 0))
    turn = replace(turn, roads=tuple(reversed(turn.roads)))
    case.apply(StageStateChange(turns=(turn,), reviews=(binding,)))
    assert case.store.load_turn(case.task_id, turn.turn_id) == turn
    turn = replace(turn, roads=(replace(turn.roads[0], status=IntentStatus.RUNNING), turn.roads[1]))
    case.apply(StageStateChange(turns=(turn,)))
    with pytest.raises(StageStateError, match="reordered"):
        case.apply(StageStateChange(turns=(replace(turn, roads=tuple(reversed(turn.roads))),)))


def test_historical_achievement_survives_source_withdrawal_but_new_use_fails(case):
    turn = case.root()
    ref = case.fact(turn.turn_id)
    completion = TurnCompletion("opened", (ref,))
    binding = case.reviewed(PlanningSession("turn_planning", turn.turn_id), completion, category="action")
    turn = replace(turn, fact_refs=(ref,), achievement=TurnAchievement(completion, binding.review_operation_id))
    case.apply(StageStateChange(turns=(turn,), reviews=(binding,)))
    case.evidence.history[ref] = replace(case.evidence.history[ref], source_status="unavailable")
    case.apply(StageStateChange(turns=(turn,)))
    assert case.store.load_turn(case.task_id, turn.turn_id).achievement == turn.achievement
    assert case.store.read_history(case.task_id, (ref,))[0].source_status == "unavailable"
    candidates = (RoadCandidate("reuse unavailable archive", (ref,)),)
    binding = case.reviewed(PlanningSession("turn_planning", turn.turn_id), candidates, indices=(0,))
    with pytest.raises(StageStateError, match="source_not_effective"):
        case.apply(StageStateChange(turns=(replace(turn, roads=(RoadSnapshot(uid(), candidates[0], IntentStatus.PENDING),)),),
            reviews=(binding,)))


def test_existing_configured_d4_task_keeps_outer_planning_boundary(tmp_path):
    from agent_core.dual_llm_contracts import D3Authorization, D3CommitMetadata, D3RunConfig
    from agent_core.operation_accounting import D4BudgetPolicy

    case = Case(tmp_path / "ordinary-d4.sqlite3")
    case.request = TaskRequest.from_dict({"goal": "read input", "deadline_seconds": 3600,
        "target": {"endpoints": [], "attachments": [{"kind": "local_path", "path": "input.bin"}]}})
    config = D3RunConfig(case.config.outer_role, case.config.inner_role,
        D3Authorization(uid(), (0,)), case.config.outer_role.role_config_ref)
    config = replace(config, d4_budget=D4BudgetPolicy())
    try:
        case.start(stage=False, d3=D3CommitMetadata(config=config))
        operation = D4OperationStart(uid(), case.task_id, "planning", case.now,
            case.sequence, "a" * 64, "root", config.d4_budget.budget_policy_ref,
            "inner_action", config.inner_role.role_config_ref)
        with pytest.raises(D4OperationConflict, match="registered framework"):
            case.store.begin_d4_operation(operation)
    finally:
        case.store.close()


def test_root_without_evidence_adapter_cannot_be_registered(case):
    intent = TurnIntent("read archive", "open expected file")
    binding = case.reviewed(PlanningSession("turn_planning", case.root_id), intent)
    case.store.stage_evidence = None
    with pytest.raises(StageStateError, match="evidence_unconfigured"):
        case.apply(StageStateChange(turns=(StageTurnSnapshot(case.root_id, intent, TurnPlanningState.ACTIVE),),
            reviews=(binding,)))
    assert case.sequence == 1


def test_late_commit_keeps_known_result_without_dispatch_or_replay(case):
    intent = TurnIntent("read archive", "open expected file")
    binding = case.reviewed(PlanningSession("turn_planning", case.root_id), intent)
    case.now += 3600
    with pytest.raises(StageStateError, match="task_not_active"):
        case.apply(StageStateChange(turns=(StageTurnSnapshot(case.root_id, intent, TurnPlanningState.ACTIVE),),
            reviews=(binding,)))
    result = case.store.load_d4_operation(binding.review_operation_id)
    assert result.completion is not None and result.application_ref is None
    assert case.store.load_turn(case.task_id, case.root_id) is None


def test_new_run_cannot_use_old_final_success_or_domain_write(case):
    from agent_core.control_store import UpsertTurn
    from agent_core.domain import TurnStatus

    goal = case.store.load_task_run(case.task_id)
    fake_success = UpsertTaskRun(case.task_id, case.request, ModelBackend.CODEX_CLI,
        case.now, case.now + 3600, TaskRunStatus.ACHIEVED, goal.sandbox_id,
        finished_at=case.now)
    change = ControlMutation.create(task_run_id=case.task_id, mutation_id=uid(), operation="finish_task_run",
        request=case.request, effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=case.sequence, state_changes=(fake_success,),
        event=RuntimeEvent("task_run_finished", case.task_id, case.now, TaskRunStatus.ACHIEVED,
            {"operation": "finish_task_run", "entity_ids": [case.task_id], "result": "achieved"}))
    with pytest.raises(StageStateError, match="final_validation_not_connected"):
        case.store.commit(change)
    turn_id = uid()
    raw_turn = UpsertTurn(turn_id, case.task_id, 1, case.request.goal, (), TurnStatus.PENDING)
    old_write = ControlMutation.create(task_run_id=case.task_id, mutation_id=uid(), operation="register_turn",
        request=case.request, effective_model_backend=ModelBackend.CODEX_CLI,
        base_checkpoint_sequence=case.sequence, state_changes=(raw_turn,),
        event=RuntimeEvent("control_state_committed", case.task_id, case.now, TaskRunStatus.RUNNING,
            {"operation": "register_turn", "entity_ids": [turn_id], "result": turn_id}))
    with pytest.raises(StageStateError, match="legacy_domain_write_rejected"):
        case.store.commit(old_write)
    assert case.store.load_task_run(case.task_id).status is TaskRunStatus.RUNNING


def test_schema_update_rolls_back_and_does_not_adopt_existing_tasks(tmp_path, monkeypatch):
    from agent_core import stage_storage

    case = Case(tmp_path / "previous.sqlite3")
    case.start(stage=False)
    task_id = case.task_id
    before = case.store.load_task_run(task_id)
    case.store.close()
    with sqlite3.connect(case.path) as db:
        for table in ("stage_sessions", "stage_roads", "stage_turns", "stage_runs"):
            db.execute("DROP TABLE " + table)
        db.execute("DELETE FROM schema_versions WHERE version=32")
    schema = stage_storage.SCHEMA
    monkeypatch.setattr(stage_storage, "SCHEMA", schema.replace("CREATE TABLE stage_turns", "CREATE TABL stage_turns"))
    with pytest.raises(StorageError, match="schema migration failed"):
        SqliteControlStore.open(case.path)
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 31
        assert db.execute("SELECT name FROM sqlite_master WHERE name='stage_runs'").fetchone() is None
    monkeypatch.setattr(stage_storage, "SCHEMA", schema)
    with SqliteControlStore.open(case.path) as store:
        assert store.load_task_run(task_id) == before
        assert store.load_stage_run_config(task_id) is None
        with pytest.raises(StageStateError, match="run_unsupported"):
            store.load_turn(task_id, uid())


def test_stage_rejections_are_classified_without_result_text(case):
    from qinyuan_core import Category, classify

    turn = case.root()
    with pytest.raises(StageStateError) as error:
        case.apply(StageStateChange(turns=(replace(turn, intent=replace(turn.intent, description="a secret raw prompt")),)))
    assert classify(error.value) == (Category.CONFLICT, "stage.immutable_turn_changed", False)
    assert "a secret raw prompt" not in str(error.value)


@pytest.mark.parametrize("change", [
    StageStateChange(sessions=(PlanningSession("invalid", "bad-id"),)),
    StageStateChange(reviews=("untyped-review",)),
])
def test_malformed_changes_are_classified_before_writing(case, change):
    from qinyuan_core import Category, classify

    with pytest.raises(StageStateError) as error:
        case.apply(change)
    assert classify(error.value)[0] is Category.INPUT
    assert case.sequence == 1
