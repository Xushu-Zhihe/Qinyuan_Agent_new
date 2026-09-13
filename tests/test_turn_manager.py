from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_core import (
    DomainError,
    DomainTransitionError,
    Execution,
    ExecutionEvidence,
    ExecutionStatus,
    Fact,
    FactStatus,
    FailureAction,
    FailureObservation,
    FailureType,
    Intent,
    IntentStatus,
    Plan,
    PlanValidationRecord,
    PlanValidationStatus,
    PlanStatus,
    TaskRequest,
    TaskRunController,
    TaskRunStatus,
    Turn,
    TurnManager,
    TurnStatus,
    classify_failure,
)


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeSandbox:
    created: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)

    def create(self, task_run_id: str) -> str:
        self.created.append(task_run_id)
        return f"sandbox-{task_run_id}"

    def interrupt(self, sandbox_id: str) -> None:
        return None

    def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)


@dataclass
class FakePlanValidationEvidence:
    records: dict[str, PlanValidationRecord] = field(default_factory=dict)

    def lookup(self, plan_validation_id: str) -> PlanValidationRecord | None:
        return self.records.get(plan_validation_id)


@dataclass
class FakeExecutionEvidence:
    command_refs: set[str] = field(default_factory=set)
    output_refs: set[str] = field(default_factory=set)
    records: dict[str, ExecutionEvidence] = field(default_factory=dict)
    plan_validations: FakePlanValidationEvidence = field(
        default_factory=FakePlanValidationEvidence
    )

    def command_exists(self, command_ref: str) -> bool:
        return command_ref in self.command_refs

    def output_exists(self, output_ref: str) -> bool:
        return output_ref in self.output_refs

    def lookup(self, execution_id: str) -> ExecutionEvidence | None:
        return self.records.get(execution_id)


def make_manager(
    *,
    deadline_seconds: int = 3600,
) -> tuple[TurnManager, FakeExecutionEvidence, FakeClock]:
    clock = FakeClock()
    controller = TaskRunController(FakeSandbox(), clock=clock)
    controller.start(
        TaskRequest.from_dict(
            {
                "goal": "找到 flag",
                "target": {"endpoints": [{"url": "http://example.test:8080"}]},
                "deadline_seconds": deadline_seconds,
            }
        )
    )
    evidence = FakeExecutionEvidence()
    return TurnManager(controller, evidence, evidence.plan_validations), evidence, clock


def add_plan_validation(
    manager: TurnManager,
    evidence: FakeExecutionEvidence,
    *,
    plan_validation_id: str,
    plan_id: str,
    step_index: int | None = 0,
    attempt: int = 1,
    status: PlanValidationStatus = PlanValidationStatus.SUCCEEDED,
    failure_type: FailureType | None = None,
    short_reason: str = "command generated",
    command_ref: str | None = None,
    observed_at: float | None = None,
    task_run_id: str | None = None,
) -> PlanValidationRecord:
    if (
        command_ref is None
        and status is PlanValidationStatus.SUCCEEDED
        and step_index is not None
    ):
        command_ref = "command-1"
    record = PlanValidationRecord(
        plan_validation_id=plan_validation_id,
        plan_id=plan_id,
        task_run_id=task_run_id or manager.task_run_id,
        step_index=step_index,
        attempt=attempt,
        status=status,
        failure_type=failure_type,
        short_reason=short_reason,
        command_ref=command_ref,
        observed_at=(
            manager._controller.task_run.started_at
            if observed_at is None
            else observed_at
        ),
    )
    evidence.plan_validations.records[plan_validation_id] = record
    return record


def register_turn(
    manager: TurnManager,
    *,
    intent_specs: tuple[tuple[str, int], ...],
    turn_id: str = "turn-1",
    sequence: int = 1,
) -> tuple[Turn, tuple[Intent, ...]]:
    intents = tuple(
        Intent(
            intent_id=intent_id,
            turn_id=turn_id,
            description=f"try {intent_id}",
            priority=priority,
        )
        for intent_id, priority in intent_specs
    )
    turn = Turn(
        turn_id=turn_id,
        task_run_id=manager.task_run_id,
        sequence=sequence,
        turn_target="确认入口",
        intent_ids=tuple(intent.intent_id for intent in intents),
    )
    manager.register_turn(turn, intents)
    return turn, intents


def start_plan(
    manager: TurnManager,
    intent_id: str,
    *,
    plan_id: str = "plan-1",
    step_descriptions: tuple[str, ...] = ("使用已授权工具确认入口",),
) -> Plan:
    plan = Plan.from_step_dicts(
        plan_id=plan_id,
        intent_id=intent_id,
        steps=[{"description": description} for description in step_descriptions],
    )
    manager.register_plan(plan)
    return manager.start_plan(plan_id)


def complete_successful_execution(
    manager: TurnManager,
    evidence: FakeExecutionEvidence,
    *,
    execution_id: str,
    plan_id: str,
    command_ref: str = "command-1",
    output_ref: str = "output-1",
    target_ref: str = "http://example.test:8080",
    observed_at: float = 100.0,
    task_run_id: str | None = None,
    plan_validation_id: str | None = None,
) -> Execution:
    evidence.command_refs.add(command_ref)
    evidence.output_refs.add(output_ref)
    plan_validation_id = plan_validation_id or f"validation-{execution_id}"
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id=plan_validation_id,
        plan_id=plan_id,
        command_ref=command_ref,
    )
    execution = Execution(
        execution_id=execution_id,
        plan_id=plan_id,
        step_index=0,
        command_ref=command_ref,
    )
    manager.create_execution(execution, plan_validation_id=plan_validation_id)
    manager.start_execution(execution_id)
    manager.complete_execution(
        execution_id=execution_id,
        status=ExecutionStatus.SUCCEEDED,
        output_ref=output_ref,
    )
    evidence.records[execution_id] = ExecutionEvidence(
        task_run_id=task_run_id or manager.task_run_id,
        target_ref=target_ref,
        observed_at=observed_at,
    )
    return execution


def test_plan_steps_priority_and_execution_output_contracts_are_enforced() -> None:
    with pytest.raises(DomainError, match="non-empty"):
        Plan.from_step_dicts(plan_id="plan", intent_id="intent", steps=[])
    with pytest.raises(DomainError, match="description"):
        Plan.from_step_dicts(
            plan_id="plan",
            intent_id="intent",
            steps=[{"description": ""}],
        )
    with pytest.raises(DomainError, match="non-terminal"):
        Execution(
            execution_id="execution",
            plan_id="plan",
            step_index=0,
            command_ref="command",
            output_ref="output",
        )
    with pytest.raises(DomainError, match="terminal"):
        Execution(
            execution_id="execution",
            plan_id="plan",
            step_index=0,
            command_ref="command",
            status=ExecutionStatus.FAILED,
        )

    manager, _, _ = make_manager()
    duplicate_turn = Turn(
        turn_id="turn-duplicate",
        task_run_id=manager.task_run_id,
        sequence=1,
        turn_target="确认入口",
        intent_ids=("intent-a", "intent-b"),
    )
    with pytest.raises(DomainError, match="priority"):
        manager.register_turn(
            duplicate_turn,
            (
                Intent("intent-a", "turn-duplicate", "a", 1),
                Intent("intent-b", "turn-duplicate", "b", 1),
            ),
        )


def test_scheduler_uses_priority_then_backtracks_after_target_mismatch() -> None:
    manager, evidence, _ = make_manager()
    _, intents = register_turn(
        manager,
        intent_specs=(("intent-b", 2), ("intent-a", 1)),
    )

    assert manager.select_next_intent("turn-1").intent_id == "intent-a"
    start_plan(manager, "intent-a")
    complete_successful_execution(
        manager,
        evidence,
        execution_id="execution-a",
        plan_id="plan-1",
    )
    assert manager.complete_plan("plan-1") is PlanStatus.COMPLETED

    assert manager.record_target_mismatch(
        plan_id="plan-1",
        evidence_ref="execution-a",
        short_reason="命令执行成功，但未满足当前轮目标",
    ) is FailureAction.TRY_NEXT_INTENT
    assert intents[1].status is IntentStatus.FAILED
    assert manager.plans["plan-1"].status is PlanStatus.COMPLETED
    assert [item.failure_type for item in manager.strategy_failure_summaries] == [
        FailureType.TARGET_MISMATCH
    ]
    assert manager.select_next_intent("turn-1").intent_id == "intent-b"


def test_target_mismatch_requires_a_successful_execution() -> None:
    manager, evidence, _ = make_manager()
    register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent("turn-1")
    start_plan(manager, "intent-a")
    evidence.command_refs.add("command-1")
    evidence.output_refs.add("output-1")
    execution = Execution("execution-1", "plan-1", 0, "command-1")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
    )
    manager.create_execution(execution, plan_validation_id="validation-1")
    manager.start_execution("execution-1")
    manager.complete_execution(
        execution_id="execution-1",
        status=ExecutionStatus.FAILED,
        output_ref="output-1",
    )
    manager.complete_plan("plan-1")

    with pytest.raises(DomainTransitionError, match="successful Execution"):
        manager.record_target_mismatch(
            plan_id="plan-1",
            evidence_ref="execution-1",
            short_reason="目标不匹配",
        )


def test_command_generation_environment_fault_retries_once_without_execution() -> None:
    manager, evidence, _ = make_manager()
    turn, intents = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")

    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.ENVIRONMENT_FAULT,
        short_reason="MCP 连接断开",
    )
    assert manager.record_command_generation_failure(
        plan_validation_id="validation-1",
    ) is FailureAction.RETRY_COMMAND_GENERATION
    assert manager.executions == {}
    assert manager.failures[-1].evidence_ref == "validation-1"
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-2",
        plan_id="plan-1",
        attempt=2,
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.ENVIRONMENT_FAULT,
        short_reason="MCP 连接断开",
    )
    assert manager.record_command_generation_failure(
        plan_validation_id="validation-2",
    ) is FailureAction.TURN_INCONCLUSIVE
    assert manager.plans["plan-1"].status is PlanStatus.INCONCLUSIVE
    assert intents[0].status is IntentStatus.INCONCLUSIVE
    assert turn.status is TurnStatus.INCONCLUSIVE
    assert manager.strategy_failure_summaries == ()


def test_command_generation_retry_can_create_execution_on_second_attempt() -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.ENVIRONMENT_FAULT,
        short_reason="MCP 连接断开",
    )
    assert manager.record_command_generation_failure(
        plan_validation_id="validation-1",
    ) is FailureAction.RETRY_COMMAND_GENERATION

    evidence.command_refs.add("command-1")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-2",
        plan_id="plan-1",
        attempt=2,
        short_reason="command generated after retry",
    )
    execution = manager.create_execution(
        Execution("execution-1", "plan-1", 0, "command-1"),
        plan_validation_id="validation-2",
    )
    assert execution.execution_id == "execution-1"
    assert manager.executions == {"execution-1": execution}


@pytest.mark.parametrize(
    "case",
    [
        "other_task_run",
        "unknown_plan",
        "wrong_step",
        "after_deadline",
        "attempt_gap",
        "failed_validation",
    ],
)
def test_execution_rejects_invalid_command_generation_evidence(case: str) -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(
        manager,
        "intent-a",
        step_descriptions=("确认入口", "确认版本"),
    )
    evidence.command_refs.add("command-1")

    record_plan_id = "plan-1"
    record_kwargs: dict[str, object] = {}
    if case == "other_task_run":
        record_kwargs["task_run_id"] = "other-task-run"
    elif case == "unknown_plan":
        record_plan_id = "unknown-plan"
    elif case == "wrong_step":
        record_kwargs["step_index"] = 1
    elif case == "after_deadline":
        record_kwargs["observed_at"] = manager._controller.task_run.deadline_at + 1
    elif case == "attempt_gap":
        record_kwargs["attempt"] = 2
    elif case == "failed_validation":
        record_kwargs.update(
            {
                "status": PlanValidationStatus.FAILED,
                "failure_type": FailureType.POLICY_REJECTED,
            }
        )
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id=record_plan_id,
        **record_kwargs,
    )

    with pytest.raises(DomainError):
        manager.create_execution(
            Execution("execution-1", "plan-1", 0, "command-1"),
            plan_validation_id="validation-1",
        )
    assert manager.executions == {}


def test_plan_validation_records_are_consumed_once() -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")
    evidence.command_refs.add("command-1")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
    )
    manager.create_execution(
        Execution("execution-1", "plan-1", 0, "command-1"),
        plan_validation_id="validation-1",
    )

    with pytest.raises(DomainError, match="already been consumed"):
        manager.create_execution(
            Execution("execution-2", "plan-1", 0, "command-1"),
            plan_validation_id="validation-1",
        )


def test_plan_validation_attempts_are_scoped_by_plan_and_step() -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(
        manager,
        "intent-a",
        step_descriptions=("确认入口", "确认版本"),
    )

    for plan_validation_id, step_index in (
        ("plan-level-1", None),
        ("step-0-1", 0),
        ("step-1-1", 1),
    ):
        add_plan_validation(
            manager,
            evidence,
            plan_validation_id=plan_validation_id,
            plan_id="plan-1",
            step_index=step_index,
            status=PlanValidationStatus.FAILED,
            failure_type=FailureType.ENVIRONMENT_FAULT,
            short_reason="MCP 连接断开",
        )
        assert manager.record_command_generation_failure(
            plan_validation_id=plan_validation_id,
        ) is FailureAction.RETRY_COMMAND_GENERATION

    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="step-1-gap",
        plan_id="plan-1",
        step_index=1,
        attempt=3,
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.ENVIRONMENT_FAULT,
        short_reason="MCP 连接断开",
    )
    with pytest.raises(DomainError, match="next attempt"):
        manager.record_command_generation_failure(plan_validation_id="step-1-gap")

    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="step-0-2",
        plan_id="plan-1",
        step_index=0,
        attempt=2,
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.ENVIRONMENT_FAULT,
        short_reason="MCP 连接断开",
    )
    assert manager.record_command_generation_failure(
        plan_validation_id="step-0-2",
    ) is FailureAction.TURN_INCONCLUSIVE


def test_command_generation_cannot_use_target_mismatch() -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.TARGET_MISMATCH,
        short_reason="未生成命令",
    )

    with pytest.raises(DomainError, match="completed Execution"):
        manager.record_command_generation_failure(plan_validation_id="validation-1")
    assert manager.plans["plan-1"].status is PlanStatus.RUNNING


def test_execution_environment_fault_retries_once_with_the_same_command() -> None:
    manager, evidence, _ = make_manager()
    turn, intents = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")
    evidence.command_refs.update({"command-1", "command-other"})
    evidence.output_refs.update({"output-1", "output-2"})

    first = Execution("execution-1", "plan-1", 0, "command-1")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
    )
    manager.create_execution(first, plan_validation_id="validation-1")
    manager.start_execution(first.execution_id)
    assert manager.record_execution_failure(
        execution_id=first.execution_id,
        status=ExecutionStatus.FAILED,
        output_ref="output-1",
        observation=FailureObservation(infrastructure_diagnostic=True),
        short_reason="Sandbox 进程崩溃",
    ) is FailureAction.RETRY_EXECUTION

    with pytest.raises(DomainError, match="reuse"):
        manager.create_execution(
            Execution("execution-other", "plan-1", 0, "command-other"),
            plan_validation_id="validation-1",
        )

    retry = Execution("execution-2", "plan-1", 0, "command-1")
    manager.create_execution(retry, plan_validation_id="validation-1")
    manager.start_execution(retry.execution_id)
    assert manager.record_execution_failure(
        execution_id=retry.execution_id,
        status=ExecutionStatus.FAILED,
        output_ref="output-2",
        observation=FailureObservation(infrastructure_diagnostic=True),
        short_reason="Sandbox 进程再次崩溃",
    ) is FailureAction.TURN_INCONCLUSIVE
    assert manager.plans["plan-1"].status is PlanStatus.INCONCLUSIVE
    assert intents[0].status is IntentStatus.INCONCLUSIVE
    assert turn.status is TurnStatus.INCONCLUSIVE


@pytest.mark.parametrize(
    ("observation", "failure_type"),
    [
        (FailureObservation(policy_rejected=True), FailureType.POLICY_REJECTED),
        (FailureObservation(parameter_error=True), FailureType.PARAMETER_ERROR),
    ],
)
def test_policy_and_parameter_failures_fail_the_plan_without_retry(
    observation: FailureObservation,
    failure_type: FailureType,
) -> None:
    manager, evidence, _ = make_manager()
    turn, intents = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")

    failure_type = classify_failure(observation)
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
        status=PlanValidationStatus.FAILED,
        failure_type=failure_type,
        short_reason="请求被拒绝",
    )
    assert manager.record_command_generation_failure(
        plan_validation_id="validation-1",
    ) is FailureAction.TURN_INCONCLUSIVE
    assert manager.plans["plan-1"].status is PlanStatus.FAILED
    assert intents[0].status is IntentStatus.FAILED
    assert manager.failures[-1].failure_type is failure_type


def test_unknown_execution_outcome_makes_the_plan_inconclusive() -> None:
    manager, evidence, _ = make_manager()
    turn, intents = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")
    evidence.command_refs.add("command-1")
    evidence.output_refs.add("output-1")
    execution = Execution("execution-1", "plan-1", 0, "command-1")
    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
    )
    manager.create_execution(execution, plan_validation_id="validation-1")
    manager.start_execution(execution.execution_id)

    assert manager.record_execution_failure(
        execution_id=execution.execution_id,
        status=ExecutionStatus.TIMED_OUT,
        output_ref="output-1",
        observation=FailureObservation(),
        short_reason="命令超时且无明确诊断",
    ) is FailureAction.TURN_INCONCLUSIVE
    assert manager.plans["plan-1"].status is PlanStatus.INCONCLUSIVE
    assert intents[0].status is IntentStatus.INCONCLUSIVE
    assert manager.failures[-1].failure_type is FailureType.OUTCOME_UNKNOWN


def test_model_error_fails_the_turn_and_task_run() -> None:
    manager, evidence, _ = make_manager()
    turn, intents = register_turn(manager, intent_specs=(("intent-a", 1),))
    manager.select_next_intent(turn.turn_id)
    start_plan(manager, "intent-a")

    add_plan_validation(
        manager,
        evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.MODEL_ERROR,
        short_reason="模型调用失败",
    )
    assert manager.record_command_generation_failure(
        plan_validation_id="validation-1",
    ) is FailureAction.TASK_FAILED
    assert manager.plans["plan-1"].status is PlanStatus.FAILED
    assert intents[0].status is IntentStatus.FAILED
    assert turn.status is TurnStatus.FAILED
    assert manager._controller.task_run.status is TaskRunStatus.FAILED


def test_verified_fact_completes_its_intent_and_turn() -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    intent = manager.select_next_intent(turn.turn_id)
    start_plan(manager, intent.intent_id)
    complete_successful_execution(
        manager,
        evidence,
        execution_id="execution-1",
        plan_id="plan-1",
    )
    manager.complete_plan("plan-1")
    fact = manager.create_fact(
        Fact(
            fact_id="fact-1",
            turn_id=turn.turn_id,
            content="发现可访问的入口",
            source_ref="execution-1",
        )
    )

    assert fact.status is FactStatus.CANDIDATE
    assert manager.verify_fact(
        fact_id=fact.fact_id,
        turn_target_satisfied=True,
        evidence_supports_content=True,
    ) is FactStatus.VERIFIED
    assert manager.succeed_intent_with_fact(
        intent_id=intent.intent_id,
        fact_id=fact.fact_id,
    ) is IntentStatus.SUCCEEDED
    assert manager.complete_turn(turn.turn_id) is TurnStatus.COMPLETED


@pytest.mark.parametrize(
    "case",
    [
        "other_task_run",
        "out_of_scope_target",
        "after_deadline",
        "missing_command",
        "missing_output",
        "verifier_rejected",
    ],
)
def test_fact_verification_rejects_invalid_evidence_or_verifier_conclusion(
    case: str,
) -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    intent = manager.select_next_intent(turn.turn_id)
    start_plan(manager, intent.intent_id)
    evidence_task_run_id = manager.task_run_id
    target_ref = "http://example.test:8080"
    observed_at = 100.0
    if case == "other_task_run":
        evidence_task_run_id = "other-task-run"
    elif case == "out_of_scope_target":
        target_ref = "http://outside.test:8080"
    elif case == "after_deadline":
        observed_at = manager._controller.task_run.deadline_at + 1
    complete_successful_execution(
        manager,
        evidence,
        execution_id="execution-1",
        plan_id="plan-1",
        target_ref=target_ref,
        observed_at=observed_at,
        task_run_id=evidence_task_run_id,
    )
    manager.complete_plan("plan-1")
    fact = manager.create_fact(
        Fact("fact-1", turn.turn_id, "候选结论", "execution-1")
    )
    if case == "missing_command":
        evidence.command_refs.remove("command-1")
    elif case == "missing_output":
        evidence.output_refs.remove("output-1")

    assert manager.verify_fact(
        fact_id=fact.fact_id,
        turn_target_satisfied=case != "verifier_rejected",
        evidence_supports_content=True,
    ) is FactStatus.REJECTED


def test_task_run_conclusions_require_the_corresponding_fact_state() -> None:
    manager, evidence, _ = make_manager()
    turn, _ = register_turn(manager, intent_specs=(("intent-a", 1),))
    intent = manager.select_next_intent(turn.turn_id)
    start_plan(manager, intent.intent_id)
    complete_successful_execution(
        manager,
        evidence,
        execution_id="execution-1",
        plan_id="plan-1",
    )
    manager.complete_plan("plan-1")
    fact = manager.create_fact(Fact("fact-1", turn.turn_id, "入口", "execution-1"))
    manager.verify_fact(
        fact_id=fact.fact_id,
        turn_target_satisfied=True,
        evidence_supports_content=True,
    )
    manager.succeed_intent_with_fact(intent_id=intent.intent_id, fact_id=fact.fact_id)
    manager.complete_turn(turn.turn_id)
    assert manager.conclude_task(TaskRunStatus.PARTIAL) is TaskRunStatus.PARTIAL

    other, other_evidence, _ = make_manager()
    turn, _ = register_turn(other, intent_specs=(("intent-a", 1),))
    other.select_next_intent(turn.turn_id)
    start_plan(other, "intent-a")
    add_plan_validation(
        other,
        other_evidence,
        plan_validation_id="validation-1",
        plan_id="plan-1",
        status=PlanValidationStatus.FAILED,
        failure_type=FailureType.POLICY_REJECTED,
        short_reason="策略拒绝",
    )
    assert other.record_command_generation_failure(
        plan_validation_id="validation-1",
    ) is FailureAction.TURN_INCONCLUSIVE
    assert other.conclude_task(TaskRunStatus.INCONCLUSIVE) is TaskRunStatus.INCONCLUSIVE
