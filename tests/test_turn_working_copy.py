from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from agent_core import (
    ControlStateRepository,
    ControlMutation,
    CommittedStateReloadError,
    AppendFailureSummary,
    AppendPlanValidationRecord,
    DomainError,
    DomainTransitionError,
    Execution,
    ExecutionEvidence,
    ExecutionStatus,
    Fact,
    FactStatus,
    FailureObservation,
    FailureType,
    IdempotencyConflictError,
    IntentCandidate,
    IntentStatus,
    InvalidTransition,
    ModelBackend,
    Plan,
    PlanStatus,
    PlanStep,
    PlanValidationRecordDraft,
    PlanValidationStatus,
    SqliteControlStore,
    StaleControlStateError,
    StorageError,
    TaskRequest,
    TaskRunController,
    TaskRunControllerRegistry,
    TaskRunStatus,
    TurnManager,
    TurnOperation,
    TurnStatus,
    TurnWorkingCopy,
    UpsertTurn,
    UpsertIntent,
    UpsertPlan,
    UpsertExecution,
    UpsertFact,
    UpsertTaskRun,
)


@dataclass
class Clock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Sandbox:
    calls: list[str] = field(default_factory=list)
    fail_destroy: bool = False

    def create(self, task_run_id: str) -> str:
        self.calls.append("create")
        return f"sandbox-{task_run_id}"

    def interrupt(self, sandbox_id: str) -> None:
        self.calls.append("interrupt")

    def destroy(self, sandbox_id: str) -> None:
        self.calls.append("destroy")
        if self.fail_destroy:
            raise RuntimeError("fake cleanup failure")


@dataclass
class Evidence:
    commands: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    records: dict[str, ExecutionEvidence] = field(default_factory=dict)

    def command_exists(self, command_ref: str) -> bool:
        return command_ref in self.commands

    def output_exists(self, output_ref: str) -> bool:
        return output_ref in self.outputs

    def lookup(self, execution_id: str) -> ExecutionEvidence | None:
        return self.records.get(execution_id)


class Scenario:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            path
            or Path(__file__).resolve().parents[1]
            / ".test"
            / "runtime-work"
            / f"t1-{uuid4()}.sqlite3"
        )
        self.clock = Clock()
        self.sandbox = Sandbox()
        self.evidence = Evidence()
        self.connect()
        self.controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "verify local fake evidence",
                    "target": {"endpoints": [{"url": "http://example.test:8080"}]},
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
        )
        registration = TurnManager.prepare_turn_registration(
            state=self.state,
            mutation_id=str(uuid4()),
            turn_target="verify entry",
            candidates=(IntentCandidate("first"), IntentCandidate("second")),
        )
        self.controller.commit_turn_registration(registration, str(uuid4()))
        self.turn_id = registration.turn_id

    def connect(self) -> None:
        self.store = SqliteControlStore.open(self.path, clock=self.clock)
        self.repository = ControlStateRepository(self.store, self.evidence)
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
        )

    def reopen(self) -> None:
        task_run_id = self.state.task_run.task_run_id
        self.store.close()
        self.connect()
        self.controller.attach(task_run_id)

    @property
    def state(self):
        return self.controller.control_state

    @property
    def work(self) -> TurnWorkingCopy:
        return TurnWorkingCopy(self.state, self.evidence)

    def commit(self, operation: TurnOperation, mutation_id: str | None = None):
        return self.controller.commit_turn_operation(
            operation, mutation_id or str(uuid4())
        )

    def start_plan(self, plan_id: str = "plan-1", *, steps: int = 1) -> str:
        intent_id = self.commit(self.work.select_next_intent(self.turn_id)).value
        self.commit(
            self.work.register_plan(
                Plan(
                    plan_id,
                    intent_id,
                    tuple(PlanStep(f"step {i}") for i in range(steps)),
                )
            )
        )
        self.commit(self.work.start_plan(plan_id))
        return intent_id

    def validation(
        self,
        validation_id: str = "validation-1",
        *,
        plan_id: str = "plan-1",
        step_index: int | None = 0,
        attempt: int = 1,
        failure: FailureType | None = None,
    ) -> PlanValidationRecordDraft:
        command_ref = (
            f"command-{plan_id}-{step_index}"
            if failure is None and step_index is not None
            else None
        )
        if command_ref is not None:
            self.evidence.commands.add(command_ref)
        return PlanValidationRecordDraft(
            validation_id,
            plan_id,
            self.state.task_run.task_run_id,
            step_index,
            attempt,
            PlanValidationStatus.SUCCEEDED
            if failure is None
            else PlanValidationStatus.FAILED,
            failure,
            "bounded fake diagnostic",
            command_ref,
        )

    def create_execution(
        self,
        execution_id: str = "execution-1",
        *,
        plan_id: str = "plan-1",
        step: int = 0,
    ):
        validation = self.validation(
            f"validation-{execution_id}", plan_id=plan_id, step_index=step
        )
        self.commit(self.work.append_plan_validation_record(validation))
        execution = Execution(execution_id, plan_id, step, validation.command_ref)
        self.commit(
            self.work.create_execution(
                execution, plan_validation_id=validation.plan_validation_id
            )
        )
        return execution, validation

    def output(self, execution_id: str) -> str:
        output_ref = f"output-{execution_id}"
        self.evidence.outputs.add(output_ref)
        self.evidence.records[execution_id] = ExecutionEvidence(
            self.state.task_run.task_run_id, "http://example.test:8080", self.clock.now
        )
        return output_ref

    def succeed_execution(
        self, execution_id: str = "execution-1", *, plan_id: str = "plan-1"
    ):
        execution, validation = self.create_execution(execution_id, plan_id=plan_id)
        self.commit(self.work.start_execution(execution_id))
        self.commit(
            self.work.complete_execution(
                execution_id=execution_id, output_ref=self.output(execution_id)
            )
        )
        self.commit(self.work.complete_plan(plan_id))
        return execution, validation

    def fail_execution(self, execution_id: str, observation: FailureObservation):
        return self.work.record_execution_failure(
            execution_id=execution_id,
            status=ExecutionStatus.FAILED,
            output_ref=self.output(execution_id),
            observation=observation,
            short_reason="bounded fake failure",
        )

    def counts(self) -> tuple[int, ...]:
        with sqlite3.connect(self.path) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "runtime_events",
                    "checkpoints",
                    "control_mutations",
                    "failure_summaries",
                )
            )


@pytest.fixture
def scenario():
    value = Scenario()
    try:
        yield value
    finally:
        value.store.close()


def test_success_chain_is_committed_and_recoverable_without_new_sandbox(scenario):
    s = scenario
    intent_id = s.start_plan()
    s.succeed_execution()
    fact = Fact("fact-1", s.turn_id, "verified entry", "execution-1")
    s.commit(s.work.create_fact(fact))
    assert s.repository.query_model_context(s.state.task_run.task_run_id).facts == ()
    before = s.state
    operation = s.work.verify_fact(
        fact_id=fact.fact_id, turn_target_satisfied=True, evidence_supports_content=True
    )
    assert s.state == before
    assert fact.status is FactStatus.CANDIDATE
    counts = s.counts()
    assert s.commit(operation).value == "verified"
    assert s.counts()[:3] == tuple(value + 1 for value in counts[:3])
    s.commit(s.work.succeed_intent_with_fact(intent_id=intent_id, fact_id=fact.fact_id))
    s.commit(s.work.complete_turn(s.turn_id))
    s.reopen()
    assert s.state.turns[0].status is TurnStatus.COMPLETED
    assert s.state.facts[0].status is FactStatus.VERIFIED
    assert (
        len(s.repository.query_model_context(s.state.task_run.task_run_id).facts) == 1
    )
    assert s.sandbox.calls == ["create"]
    s.commit(s.work.conclude_task(TaskRunStatus.PARTIAL))
    assert s.state.task_run.finished_at == s.clock.now
    assert s.sandbox.calls == ["create", "destroy"]


def test_serial_short_plans_keep_order_and_survive_reopen(scenario):
    s = scenario
    intent_id = s.start_plan("probe-1")
    with pytest.raises(DomainTransitionError, match="previous Plan must be completed"):
        s.work.register_plan(Plan("probe-2", intent_id, (PlanStep("next probe"),)))

    s.succeed_execution("execution-1", plan_id="probe-1")
    s.commit(s.work.register_plan(Plan("probe-2", intent_id, (PlanStep("next probe"),))))
    s.commit(s.work.start_plan("probe-2"))
    s.succeed_execution("execution-2", plan_id="probe-2")
    s.reopen()

    assert [item.plan_id for item in s.state.plans] == ["probe-1", "probe-2"]
    assert [item.status for item in s.state.plans] == [
        PlanStatus.COMPLETED, PlanStatus.COMPLETED,
    ]
    with sqlite3.connect(s.path) as connection:
        assert connection.execute(
            "SELECT plan_id, sequence FROM plans WHERE intent_id = ? ORDER BY sequence",
            (intent_id,),
        ).fetchall() == [("probe-1", 1), ("probe-2", 2)]


def test_same_prepared_delta_replays_but_changed_content_conflicts(scenario):
    s = scenario
    s.start_plan()
    record = s.validation()
    first = s.work.append_plan_validation_record(record)
    changed = s.work.append_plan_validation_record(
        replace(record, short_reason="changed")
    )
    mutation_id = str(uuid4())
    original = s.commit(first, mutation_id)
    counts = s.counts()
    s.clock.now += 1
    s.reopen()
    replay = s.commit(first, mutation_id)
    assert replay.commit.replayed
    assert replay.commit.event == original.commit.event
    assert replay.commit.checkpoint == original.commit.checkpoint
    with pytest.raises(IdempotencyConflictError):
        s.commit(changed, mutation_id)
    assert s.counts() == counts


def test_stale_delta_rejected_without_publishing_working_copy(scenario):
    s = scenario
    first = s.work.select_next_intent(s.turn_id)
    s.commit(first)
    before, counts = s.state, s.counts()
    with pytest.raises(StaleControlStateError):
        s.commit(first)
    assert s.state == before
    assert s.counts() == counts


def test_failure_transaction_rolls_back_every_parent_summary_event_and_checkpoint(
    scenario, monkeypatch
):
    s = scenario
    s.start_plan()
    record = s.validation(failure=FailureType.MODEL_ERROR)
    s.commit(s.work.append_plan_validation_record(record))
    operation = s.work.record_command_generation_failure(
        plan_validation_id=record.plan_validation_id
    )
    before, counts = s.state, s.counts()
    original = s.store._insert_event

    def fail_event(*args):
        raise sqlite3.OperationalError("injected transaction failure")

    monkeypatch.setattr(s.store, "_insert_event", fail_event)
    with pytest.raises(StorageError, match="injected transaction failure"):
        s.commit(operation)
    assert s.state == before
    assert s.repository.load_control_state(before.task_run.task_run_id) == before
    assert s.counts() == counts
    assert s.sandbox.calls == ["create"]
    monkeypatch.setattr(s.store, "_insert_event", original)
    result = s.commit(operation)
    assert result.value == "model_error"
    assert result.commit.event.type == "control_state_committed"
    assert s.state.task_run.status is TaskRunStatus.RUNNING
    assert s.state.task_run.finished_at is None
    assert s.state.plans[0].status is PlanStatus.RUNNING
    assert s.state.intents[0].status is IntentStatus.RUNNING
    assert s.state.turns[0].status is TurnStatus.RUNNING
    assert s.counts() == tuple(value + 1 for value in counts)
    assert s.sandbox.calls == ["create"]


def test_model_error_replay_does_not_trigger_cleanup(scenario):
    s = scenario
    s.start_plan()
    record = s.validation(failure=FailureType.MODEL_ERROR)
    s.commit(s.work.append_plan_validation_record(record))
    operation = s.work.record_command_generation_failure(
        plan_validation_id=record.plan_validation_id
    )
    mutation_id = str(uuid4())
    first = s.commit(operation, mutation_id)
    assert first.value == "model_error"
    counts = s.counts()
    calls = list(s.sandbox.calls)
    assert s.state.task_run.status is TaskRunStatus.RUNNING
    assert s.commit(operation, mutation_id).commit.replayed
    assert s.counts() == counts
    assert s.sandbox.calls == calls


def test_reload_failure_hides_old_cache_and_attach_can_replay_committed_delta(
    scenario, monkeypatch
):
    s = scenario
    operation = s.work.select_next_intent(s.turn_id)
    mutation_id = str(uuid4())

    def unavailable(*args):
        raise StorageError("read unavailable")

    monkeypatch.setattr(s.repository, "load_control_state", unavailable)
    with pytest.raises(StorageError, match="read unavailable"):
        s.commit(operation, mutation_id)
    with pytest.raises(StorageError, match="cache is invalid"):
        _ = s.state
    task_run_id = operation.task_run_id
    s.store.close()
    s.connect()
    s.controller.attach(task_run_id)
    counts = s.counts()
    assert s.commit(operation, mutation_id).commit.replayed
    assert s.counts() == counts


@pytest.mark.parametrize("second_succeeds", [True, False])
def test_command_retry_consumption_survives_restart(scenario, second_succeeds):
    s = scenario
    s.start_plan()
    first = s.validation(failure=FailureType.ENVIRONMENT_FAULT)
    s.commit(s.work.append_plan_validation_record(first))
    s.reopen()  # Evidence append alone must not count as consumption.
    assert (
        s.commit(
            s.work.record_command_generation_failure(
                plan_validation_id=first.plan_validation_id
            )
        ).value
        == "retry_command_generation"
    )
    s.reopen()
    with pytest.raises(DomainError, match="already been consumed"):
        s.work.record_command_generation_failure(
            plan_validation_id=first.plan_validation_id
        )
    second = s.validation(
        "validation-2",
        attempt=2,
        failure=None if second_succeeds else FailureType.ENVIRONMENT_FAULT,
    )
    s.commit(s.work.append_plan_validation_record(second))
    s.reopen()
    if second_succeeds:
        s.commit(
            s.work.create_execution(
                Execution("execution-1", "plan-1", 0, second.command_ref),
                plan_validation_id=second.plan_validation_id,
            )
        )
        assert len(s.state.executions) == 1
    else:
        assert (
            s.commit(
                s.work.record_command_generation_failure(
                    plan_validation_id=second.plan_validation_id
                )
            ).value
            == "try_next_intent"
        )
        assert s.state.plans[0].status is PlanStatus.INCONCLUSIVE
    assert (
        s.repository.query_model_context(s.state.task_run.task_run_id).failure_summaries
        == ()
    )
    assert s.sandbox.calls == ["create"]


@pytest.mark.parametrize("retry_result", ["pending", "running", "succeeded", "failed"])
def test_execution_retry_is_bounded_across_restarts(scenario, retry_result):
    s = scenario
    s.start_plan()
    first, validation = s.create_execution()
    s.commit(s.work.start_execution(first.execution_id))
    assert (
        s.commit(
            s.fail_execution(
                first.execution_id, FailureObservation(infrastructure_diagnostic=True)
            )
        ).value
        == "retry_execution"
    )
    s.reopen()
    with pytest.raises(DomainError, match="reuse"):
        s.work.create_execution(
            Execution("wrong", first.plan_id, 0, first.command_ref),
            plan_validation_id="another-validation",
        )
    retry = replace(first, execution_id="execution-2")
    s.commit(
        s.work.create_execution(retry, plan_validation_id=validation.plan_validation_id)
    )
    if retry_result != "pending":
        s.commit(s.work.start_execution(retry.execution_id))
    if retry_result == "succeeded":
        s.commit(
            s.work.complete_execution(
                execution_id=retry.execution_id, output_ref=s.output(retry.execution_id)
            )
        )
    elif retry_result == "failed":
        assert (
            s.commit(
                s.fail_execution(
                    retry.execution_id,
                    FailureObservation(infrastructure_diagnostic=True),
                )
            ).value
            == "try_next_intent"
        )
    s.reopen()
    with pytest.raises(DomainTransitionError):
        s.work.create_execution(
            replace(first, execution_id="execution-3"),
            plan_validation_id=validation.plan_validation_id,
        )
    assert len(s.state.executions) == 2
    assert s.sandbox.calls == ["create"]


def test_t6_step_timeout_allows_one_changed_command_across_restart(scenario):
    s = scenario
    s.start_plan()
    first, _ = s.create_execution()
    s.commit(s.work.start_execution(first.execution_id))
    assert s.commit(
        s.work.record_step_timeout(
            execution_id=first.execution_id,
            output_ref=s.output(first.execution_id),
        )
    ).value == "retry_step_command"
    s.reopen()

    second_validation = replace(
        s.validation("validation-2", attempt=2),
        command_ref="command-adjusted",
    )
    s.evidence.commands.add(second_validation.command_ref)
    s.commit(s.work.append_plan_validation_record(second_validation))
    second = Execution("execution-2", "plan-1", 0, second_validation.command_ref)
    s.commit(
        s.work.create_execution(
            second, plan_validation_id=second_validation.plan_validation_id
        )
    )
    s.commit(s.work.start_execution(second.execution_id))
    assert s.commit(
        s.work.record_step_timeout(
            execution_id=second.execution_id,
            output_ref=s.output(second.execution_id),
        )
    ).value == "try_next_intent"
    s.reopen()
    assert s.state.intents[0].status is IntentStatus.FAILED
    assert s.state.plans[0].status is PlanStatus.FAILED
    assert [item.failure_type for item in s.state.failure_summaries] == [
        FailureType.STEP_TIMEOUT,
        FailureType.STEP_TIMEOUT,
    ]


def test_t6_mcp_budget_timeout_counts_without_docker_execution(scenario):
    s = scenario
    s.start_plan()
    first = s.validation("validation-budget-1", failure=FailureType.STEP_TIMEOUT)
    s.commit(s.work.append_plan_validation_record(first))
    assert s.commit(
        s.work.record_command_generation_failure(plan_validation_id=first.plan_validation_id)
    ).value == "retry_step_command"
    assert not s.state.executions
    s.reopen()

    second = s.validation(
        "validation-budget-2", attempt=2, failure=FailureType.STEP_TIMEOUT
    )
    s.commit(s.work.append_plan_validation_record(second))
    assert s.commit(
        s.work.record_command_generation_failure(plan_validation_id=second.plan_validation_id)
    ).value == "try_next_intent"
    assert not s.state.executions
    assert s.state.intents[0].status is IntentStatus.FAILED


def test_execution_serialization_also_applies_to_different_steps(scenario):
    s = scenario
    s.start_plan(steps=2)
    first, _ = s.create_execution()
    second, _ = s.create_execution("execution-2", step=1)
    s.commit(s.work.start_execution(first.execution_id))
    before = s.state
    with pytest.raises(DomainTransitionError, match="already running"):
        s.work.start_execution(second.execution_id)
    assert s.state == before


@pytest.mark.parametrize(
    "failure,status",
    [
        (FailureType.POLICY_REJECTED, PlanStatus.FAILED),
        (FailureType.PARAMETER_ERROR, PlanStatus.FAILED),
        (FailureType.OUTCOME_UNKNOWN, PlanStatus.INCONCLUSIVE),
        (FailureType.MODEL_ERROR, PlanStatus.RUNNING),
    ],
)
@pytest.mark.parametrize("phase", ["command", "execution"])
def test_failure_routing_is_atomic_and_only_target_mismatch_enters_context(
    scenario, failure, status, phase
):
    s = scenario
    s.start_plan()
    if phase == "command":
        record = s.validation(failure=failure)
        s.commit(s.work.append_plan_validation_record(record))
        operation = s.work.record_command_generation_failure(
            plan_validation_id=record.plan_validation_id
        )
    else:
        execution, _ = s.create_execution()
        s.commit(s.work.start_execution(execution.execution_id))
        observation = FailureObservation(
            policy_rejected=failure is FailureType.POLICY_REJECTED,
            parameter_error=failure is FailureType.PARAMETER_ERROR,
            model_error=failure is FailureType.MODEL_ERROR,
        )
        operation = s.fail_execution(execution.execution_id, observation)
    before = s.state
    assert before.plans[0].status is PlanStatus.RUNNING
    s.commit(operation)
    s.reopen()
    assert s.state.plans[0].status is status
    assert s.state.failure_summaries[0].failure_type is failure
    assert (
        s.repository.query_model_context(s.state.task_run.task_run_id).failure_summaries
        == ()
    )
    if failure is FailureType.MODEL_ERROR:
        assert s.state.task_run.status is TaskRunStatus.RUNNING
        assert s.state.intents[0].status is IntentStatus.RUNNING
        assert s.state.turns[0].status is TurnStatus.RUNNING
    else:
        selected = s.commit(s.work.select_next_intent(s.turn_id)).value
        assert selected == s.state.intents[1].intent_id


def test_target_mismatch_backtracks_and_exhaustion_can_conclude(scenario):
    s = scenario
    s.start_plan()
    s.succeed_execution()
    s.commit(
        s.work.record_target_mismatch(
            plan_id="plan-1",
            evidence_ref="execution-1",
            short_reason="entry did not satisfy target",
        )
    )
    s.reopen()
    s.start_plan("plan-2")
    record = s.validation(
        "validation-2", plan_id="plan-2", failure=FailureType.POLICY_REJECTED
    )
    s.commit(s.work.append_plan_validation_record(record))
    assert (
        s.commit(
            s.work.record_command_generation_failure(
                plan_validation_id=record.plan_validation_id
            )
        ).value
        == "turn_inconclusive"
    )
    context = s.repository.query_model_context(s.state.task_run.task_run_id)
    assert len(context.failure_summaries) == 1
    assert context.failure_summaries[0].failure_type is FailureType.TARGET_MISMATCH
    s.commit(s.work.conclude_task(TaskRunStatus.INCONCLUSIVE))
    assert s.state.task_run.status is TaskRunStatus.INCONCLUSIVE


@pytest.mark.parametrize(
    "case", ["foreign_task", "outside_scope", "expired", "verifier_rejected"]
)
def test_fact_verification_rejections_persist(scenario, case):
    s = scenario
    s.start_plan()
    s.succeed_execution()
    s.commit(s.work.create_fact(Fact("fact-1", s.turn_id, "candidate", "execution-1")))
    record = s.evidence.records["execution-1"]
    if case == "foreign_task":
        record = replace(record, task_run_id="other")
    elif case == "outside_scope":
        record = replace(record, target_ref="http://outside.test")
    elif case == "expired":
        record = replace(record, observed_at=s.state.task_run.deadline_at + 1)
    s.evidence.records["execution-1"] = record
    assert (
        s.commit(
            s.work.verify_fact(
                fact_id="fact-1",
                turn_target_satisfied=True,
                evidence_supports_content=case != "verifier_rejected",
            )
        ).value
        == "rejected"
    )
    s.reopen()
    assert s.state.facts[0].status is FactStatus.REJECTED
    assert s.repository.query_model_context(s.state.task_run.task_run_id).facts == ()


def test_preverified_fact_and_mutable_inputs_cannot_bypass_working_copy(scenario):
    s = scenario
    s.start_plan()
    s.succeed_execution()
    fact = Fact("fact-1", s.turn_id, "candidate", "execution-1")
    operation = s.work.create_fact(fact)
    fact.content = "changed after preparation"
    fact.status = FactStatus.VERIFIED
    with pytest.raises(DomainTransitionError, match="candidate"):
        s.work.create_fact(fact)
    s.commit(operation)
    assert s.state.facts[0].content == "candidate"
    assert s.state.facts[0].status is FactStatus.CANDIDATE


def test_rejected_domain_calculation_does_not_consume_evidence(scenario):
    s = scenario
    s.start_plan()
    record = s.validation()
    s.commit(s.work.append_plan_validation_record(record))
    work, before = s.work, s.state
    s.evidence.commands.add("wrong-command")
    with pytest.raises(DomainError, match="must match"):
        work.create_execution(
            Execution("execution-1", "plan-1", 0, "wrong-command"),
            plan_validation_id=record.plan_validation_id,
        )
    assert s.state == before
    s.commit(
        work.create_execution(
            Execution("execution-1", "plan-1", 0, record.command_ref),
            plan_validation_id=record.plan_validation_id,
        )
    )
    assert len(s.state.executions) == 1


def test_uncommitted_validation_cannot_be_used_and_retry_requires_consumption(scenario):
    s = scenario
    s.start_plan()
    record = s.validation(failure=FailureType.ENVIRONMENT_FAULT)
    operation = s.work.append_plan_validation_record(record)
    with pytest.raises(DomainError, match="does not exist"):
        s.work.record_command_generation_failure(
            plan_validation_id=record.plan_validation_id
        )
    s.commit(operation)
    second = s.validation("validation-2", attempt=2)
    with pytest.raises(DomainTransitionError, match="must be consumed"):
        s.work.append_plan_validation_record(second)


def test_deadline_blocks_new_delta_but_allows_exact_replay_then_explicit_cleanup(
    scenario,
):
    s = scenario
    operation = s.work.select_next_intent(s.turn_id)
    mutation_id = str(uuid4())
    s.commit(operation, mutation_id)
    plan = s.work.register_plan(
        Plan("plan-1", s.state.intents[0].intent_id, (PlanStep("step"),))
    )
    before = s.state
    s.clock.now = s.state.task_run.deadline_at
    with pytest.raises(InvalidTransition, match="advance_time"):
        s.commit(plan)
    assert s.state == before
    assert s.commit(operation, mutation_id).commit.replayed
    s.controller.advance_time(str(uuid4()))
    assert s.state.task_run.status is TaskRunStatus.DEADLINE_EXCEEDED
    assert s.sandbox.calls == ["create", "interrupt", "destroy"]


def test_running_recovery_does_not_replay_execution(scenario):
    s = scenario
    s.start_plan()
    execution, _ = s.create_execution()
    operation = s.work.start_execution(execution.execution_id)
    s.commit(operation)
    s.reopen()
    recovered = s.controller.recover_task_run(str(uuid4()))
    assert recovered.state.task_run.status is TaskRunStatus.INCONCLUSIVE
    assert recovered.state.executions[0].status is ExecutionStatus.RUNNING
    with pytest.raises(InvalidTransition, match="running TaskRun"):
        s.commit(operation)
    assert s.sandbox.calls == ["create"]


def test_cancel_rejects_prepared_delta(scenario):
    s = scenario
    operation = s.work.select_next_intent(s.turn_id)
    s.controller.cancel(str(uuid4()))
    before, counts = s.state, s.counts()
    with pytest.raises(InvalidTransition, match="running TaskRun"):
        s.commit(operation)
    assert s.state == before
    assert s.counts() == counts
    assert s.sandbox.calls == ["create", "interrupt", "destroy"]


def test_persistent_controller_cannot_be_used_as_mutable_legacy_manager(scenario):
    s = scenario
    manager = TurnManager(s.controller, s.evidence, object())
    with pytest.raises(DomainTransitionError, match="TurnWorkingCopy"):
        manager.select_next_intent(s.turn_id)


def test_other_controller_checkpoint_wins_over_stale_cached_state(scenario):
    s = scenario
    operation = s.work.select_next_intent(s.turn_id)
    other_store = SqliteControlStore.open(s.path, clock=s.clock)
    try:
        other = TaskRunController(
            s.sandbox,
            clock=s.clock,
            store=other_store,
            repository=ControlStateRepository(other_store, s.evidence),
            registry=TaskRunControllerRegistry(),
        )
        other.attach(s.state.task_run.task_run_id)
        other.commit_turn_operation(operation, str(uuid4()))
        counts = s.counts()
        with pytest.raises(StaleControlStateError):
            s.commit(operation)
        assert s.counts() == counts
        assert s.state.intents[0].status is IntentStatus.PENDING
    finally:
        other_store.close()


def test_one_sandbox_survives_new_turn_registration(scenario):
    s = scenario
    intent_id = s.start_plan()
    s.succeed_execution()
    s.commit(s.work.create_fact(Fact("fact-1", s.turn_id, "fact", "execution-1")))
    s.commit(
        s.work.verify_fact(
            fact_id="fact-1", turn_target_satisfied=True, evidence_supports_content=True
        )
    )
    s.commit(s.work.succeed_intent_with_fact(intent_id=intent_id, fact_id="fact-1"))
    s.commit(s.work.complete_turn(s.turn_id))
    s.reopen()
    registration = TurnManager.prepare_turn_registration(
        state=s.state,
        mutation_id=str(uuid4()),
        turn_target="next target",
        candidates=(IntentCandidate("next path"),),
    )
    s.controller.commit_turn_registration(registration, str(uuid4()))
    s.commit(s.work.select_next_intent(registration.turn_id))
    assert s.state.turns[1].sequence == 2
    assert s.state.turns[1].status is TurnStatus.RUNNING
    assert s.sandbox.calls == ["create"]


def database_dump(s: Scenario) -> tuple[str, ...]:
    with sqlite3.connect(s.path) as connection:
        return tuple(connection.iterdump())


def raw_mutation(
    s: Scenario, operation: TurnOperation, mutation_id: str
) -> ControlMutation:
    return ControlMutation.create(
        task_run_id=operation.task_run_id,
        mutation_id=mutation_id,
        operation=operation.operation,
        request=s.state.task_run.request,
        effective_model_backend=s.state.task_run.effective_model_backend,
        base_checkpoint_sequence=operation.base_checkpoint_sequence,
        state_changes=operation.state_changes,
        event=operation.event(timestamp=s.clock.now),
    )


@pytest.mark.parametrize("entry", ["controller", "store"])
@pytest.mark.parametrize(
    "kind",
    ["turn", "intent", "plan", "execution", "fact", "validation", "failure", "graft"],
)
def test_cross_task_changes_leave_both_tasks_untouched(scenario, entry, kind):
    s = scenario
    foreign = Scenario(path=s.path)
    try:
        foreign.start_plan()
        foreign.succeed_execution()
        foreign.commit(
            foreign.work.create_fact(
                Fact("foreign-fact", foreign.turn_id, "candidate", "execution-1")
            )
        )
        turn, intent = foreign.state.turns[0], foreign.state.intents[0]
        changes = {
            "turn": (
                UpsertTurn(
                    s.turn_id,
                    s.state.task_run.task_run_id,
                    1,
                    "changed",
                    (*s.state.turns[0].intent_ids, intent.intent_id),
                    TurnStatus.PENDING,
                ),
            ),
            "intent": (
                UpsertIntent(
                    intent.intent_id,
                    turn.turn_id,
                    "changed",
                    1,
                    None,
                    IntentStatus.FAILED,
                ),
            ),
            "plan": (
                UpsertPlan(
                    "injected-plan",
                    intent.intent_id,
                    (PlanStep("step"),),
                    PlanStatus.PENDING,
                ),
            ),
            "execution": (
                UpsertExecution(
                    "injected-execution",
                    "plan-1",
                    0,
                    "foreign-command",
                    ExecutionStatus.PENDING,
                    None,
                ),
            ),
            "fact": (
                UpsertFact(
                    "foreign-fact",
                    turn.turn_id,
                    "changed",
                    "execution-1",
                    FactStatus.CANDIDATE,
                ),
            ),
            "validation": (
                AppendPlanValidationRecord(
                    replace(
                        foreign.validation(
                            "injected-validation", failure=FailureType.MODEL_ERROR
                        ),
                        task_run_id=s.state.task_run.task_run_id,
                    )
                ),
            ),
            "failure": (
                AppendFailureSummary(
                    FailureType.TARGET_MISMATCH, "changed", "execution-1"
                ),
            ),
            "graft": (
                UpsertTurn(
                    s.turn_id,
                    s.state.task_run.task_run_id,
                    1,
                    "changed",
                    (*s.state.turns[0].intent_ids, intent.intent_id),
                    TurnStatus.PENDING,
                ),
                UpsertIntent(
                    intent.intent_id,
                    s.turn_id,
                    "grafted",
                    3,
                    None,
                    IntentStatus.PENDING,
                ),
                UpsertPlan(
                    "injected-plan",
                    intent.intent_id,
                    (PlanStep("step"),),
                    PlanStatus.PENDING,
                ),
            ),
        }[kind]
        # A permitted result name must not allow hidden foreign changes in the same delta.
        operation = TurnOperation(
            s.state.task_run.task_run_id,
            s.state.latest_checkpoint.sequence,
            "record_target_mismatch",
            changes,
            ("execution-1",),
            "try_next_intent",
        )
        before, foreign_before, dump = s.state, foreign.state, database_dump(s)
        calls = list(s.sandbox.calls)
        with pytest.raises(StorageError):
            if entry == "controller":
                s.commit(operation)
            else:
                s.store.commit(raw_mutation(s, operation, str(uuid4())))
        assert database_dump(s) == dump
        assert s.state == before
        assert (
            foreign.repository.load_control_state(foreign_before.task_run.task_run_id)
            == foreign_before
        )
        assert s.sandbox.calls == calls
    finally:
        foreign.store.close()


@pytest.mark.parametrize("entry", ["controller", "store"])
def test_new_parent_child_delta_validates_final_graph_even_when_child_comes_first(
    scenario, entry
):
    s = scenario
    # Both Intent and Plan are new. The child precedes the parent in the delta.
    intent_id = "new-intent"
    turn = s.state.turns[0]
    intent = UpsertIntent(
        intent_id, s.turn_id, "new path", 3, None, IntentStatus.PENDING
    )
    turn_change = UpsertTurn(
        turn.turn_id,
        turn.task_run_id,
        turn.sequence,
        turn.turn_target,
        (*turn.intent_ids, intent_id),
        turn.status,
    )
    plan = UpsertPlan("new-plan", intent_id, (PlanStep("step"),), PlanStatus.PENDING)
    operation = TurnOperation(
        s.state.task_run.task_run_id,
        s.state.latest_checkpoint.sequence,
        "register_plan",
        (plan, intent, turn_change),
        ("new-plan",),
        "new-plan",
    )
    if entry == "controller":
        s.commit(operation)
    else:
        s.store.commit(raw_mutation(s, operation, str(uuid4())))
    loaded = s.repository.load_control_state(s.state.task_run.task_run_id)
    assert loaded.plans[0].intent_id == intent_id


@pytest.mark.parametrize(
    "phase", ["validation", "execution", "output", "fact", "verify", "terminal"]
)
@pytest.mark.parametrize("unavailable", ["missing", "exception"])
def test_candidate_evidence_failure_does_not_write_or_advance_and_same_mutation_can_retry(
    scenario, monkeypatch, phase, unavailable
):
    s = scenario
    s.start_plan()
    if phase == "validation":
        draft = s.validation()
        operation = s.work.append_plan_validation_record(draft)
        port_method = "command_exists"
    elif phase == "execution":
        draft = s.validation()
        s.commit(s.work.append_plan_validation_record(draft))
        operation = s.work.create_execution(
            Execution("execution-1", "plan-1", 0, draft.command_ref),
            plan_validation_id=draft.plan_validation_id,
        )
        port_method = "command_exists"
    else:
        s.create_execution()
        s.commit(s.work.start_execution("execution-1"))
        output_ref = s.output("execution-1")
        if phase == "output":
            operation = s.work.complete_execution(
                execution_id="execution-1", output_ref=output_ref
            )
            port_method = "output_exists"
        elif phase == "terminal":
            operation = s.fail_execution(
                "execution-1", FailureObservation(model_error=True)
            )
            port_method = "output_exists"
        else:
            s.commit(
                s.work.complete_execution(
                    execution_id="execution-1", output_ref=output_ref
                )
            )
            fact = Fact("fact-1", s.turn_id, "candidate", "execution-1")
            operation = s.work.create_fact(fact)
            if phase == "verify":
                s.commit(operation)
                operation = s.work.verify_fact(
                    fact_id="fact-1",
                    turn_target_satisfied=True,
                    evidence_supports_content=True,
                )
            port_method = "lookup"
    before, dump, calls = s.state, database_dump(s), list(s.sandbox.calls)
    mutation_id = str(uuid4())
    original = getattr(s.evidence, port_method)

    def broken(*args):
        if unavailable == "exception":
            raise RuntimeError("evidence service offline")
        return None if port_method == "lookup" else False

    monkeypatch.setattr(s.evidence, port_method, broken)
    with pytest.raises(StorageError) as error:
        s.commit(operation, mutation_id)
    assert not isinstance(error.value, CommittedStateReloadError)
    assert database_dump(s) == dump
    assert s.state == before
    assert s.sandbox.calls == calls
    assert (
        s.store.load_committed_mutation(before.task_run.task_run_id, mutation_id)
        is None
    )
    monkeypatch.setattr(s.evidence, port_method, original)
    result = s.commit(operation, mutation_id)
    assert result.commit.replayed is False
    committed_dump, committed_calls = database_dump(s), list(s.sandbox.calls)
    # Evidence need not remain available in order to replay the durable result.
    monkeypatch.setattr(s.evidence, port_method, broken)
    replay = s.commit(operation, mutation_id)
    assert replay.commit.replayed
    assert replay.commit.event == result.commit.event
    assert replay.commit.checkpoint == result.commit.checkpoint
    assert database_dump(s) == committed_dump
    assert s.sandbox.calls == committed_calls
    changed = replace(
        operation, entity_ids=(*operation.entity_ids, "changed-fingerprint")
    )
    with pytest.raises(IdempotencyConflictError):
        s.commit(changed, mutation_id)


def test_post_commit_read_failure_explicitly_reports_commit_and_replay_works_with_invalid_cache(
    scenario, monkeypatch
):
    s = scenario
    s.start_plan()
    record = s.validation()
    operation = s.work.append_plan_validation_record(record)
    mutation_id = str(uuid4())
    original_commit = s.store.commit
    counts = s.counts()

    def commit_then_evidence_outage(mutation):
        result = original_commit(mutation)
        monkeypatch.setattr(s.evidence, "command_exists", lambda ref: False)
        return result

    monkeypatch.setattr(s.store, "commit", commit_then_evidence_outage)
    with pytest.raises(CommittedStateReloadError, match="mutation committed") as error:
        s.commit(operation, mutation_id)
    assert error.value.committed is True
    assert error.value.commit is not None
    assert not error.value.commit.replayed
    assert s.counts()[:3] == tuple(value + 1 for value in counts[:3])
    with pytest.raises(StorageError, match="cache is invalid"):
        _ = s.state
    dump = database_dump(s)
    replay = s.commit(operation, mutation_id)
    assert replay.commit.replayed
    assert database_dump(s) == dump
    assert s.sandbox.calls == ["create"]
    with pytest.raises(StorageError, match="cache is invalid"):
        _ = s.state


@pytest.mark.parametrize(
    "status", [PlanStatus.COMPLETED, PlanStatus.INCONCLUSIVE, PlanStatus.FAILED]
)
@pytest.mark.parametrize("entry", ["controller", "store"])
def test_running_execution_cannot_have_terminal_plan_in_ordinary_operation(
    scenario, status, entry
):
    s = scenario
    s.start_plan()
    s.create_execution()
    s.commit(s.work.start_execution("execution-1"))
    with pytest.raises(DomainTransitionError, match="Execution is running"):
        s.work.complete_plan("plan-1")
    plan = s.state.plans[0]
    operation = TurnOperation(
        s.state.task_run.task_run_id,
        s.state.latest_checkpoint.sequence,
        "complete_plan",
        (UpsertPlan(plan.plan_id, plan.intent_id, plan.steps, status),),
        (plan.plan_id,),
        "completed",
    )
    before, dump = s.state, database_dump(s)
    with pytest.raises(StorageError, match="running parents"):
        if entry == "controller":
            s.commit(operation)
        else:
            s.store.commit(raw_mutation(s, operation, str(uuid4())))
    assert database_dump(s) == dump
    assert s.state == before
    assert s.sandbox.calls == ["create"]
    s.reopen()
    result = s.controller.recover_task_run(str(uuid4()))
    assert result.state.task_run.status is TaskRunStatus.INCONCLUSIVE
    assert result.state.executions[0].status is ExecutionStatus.RUNNING


@pytest.mark.parametrize(
    "tamper",
    [
        "operation",
        "missing_parent",
        "execution_output",
        "task_status",
        "wrong_execution",
        "extra_change",
    ],
)
def test_recovery_exception_requires_named_operation_and_exact_combination(
    scenario, tamper
):
    s = scenario
    s.start_plan()
    s.create_execution()
    s.commit(s.work.start_execution("execution-1"))
    mutation = s.controller._build_recovery_mutation(
        s.state,
        mutation_id=str(uuid4()),
        base_checkpoint_sequence=s.state.latest_checkpoint.sequence,
    )
    changes = mutation.state_changes
    event, operation = mutation.event, mutation.operation
    if tamper == "operation":
        operation = "record_execution_failure"
        event = replace(
            event,
            details={
                **event.details,
                "operation": operation,
                "result": "turn_inconclusive",
            },
        )
    elif tamper == "missing_parent":
        changes = tuple(item for item in changes if not isinstance(item, UpsertIntent))
    elif tamper == "execution_output":
        changes = tuple(
            replace(item, status=ExecutionStatus.FAILED, output_ref="fabricated-output")
            if isinstance(item, UpsertExecution)
            else item
            for item in changes
        )
    elif tamper == "task_status":
        changes = tuple(
            replace(item, status=TaskRunStatus.FAILED)
            if isinstance(item, UpsertTaskRun)
            else item
            for item in changes
        )
    elif tamper == "wrong_execution":
        changes = tuple(
            replace(item, command_ref="replacement-command")
            if isinstance(item, UpsertExecution)
            else item
            for item in changes
        )
    else:
        changes = (
            *changes,
            AppendFailureSummary(
                FailureType.ENVIRONMENT_FAULT, "invented", "execution-1"
            ),
        )
    invalid = ControlMutation.create(
        task_run_id=mutation.task_run_id,
        mutation_id=mutation.mutation_id,
        operation=operation,
        request=mutation.request,
        effective_model_backend=mutation.effective_model_backend,
        base_checkpoint_sequence=mutation.base_checkpoint_sequence,
        state_changes=changes,
        event=event,
    )
    before, dump = s.state, database_dump(s)
    with pytest.raises(StorageError):
        s.store.commit(invalid)
    assert database_dump(s) == dump
    assert s.state == before
    assert s.sandbox.calls == ["create"]
