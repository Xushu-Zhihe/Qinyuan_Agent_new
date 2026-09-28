from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core import (
    ControlStateRepository,
    Execution,
    ExecutionEvidence,
    Fact,
    IntentCandidate,
    IntentRunResult,
    IntentStatus,
    FailureType,
    ModelBackend,
    ModelError,
    ModelErrorKind,
    Plan,
    PlanStep,
    PlanValidationRecordDraft,
    PlanValidationStatus,
    RunSupervisor,
    SqliteControlStore,
    SupervisorRecoveryRequired,
    TaskRequest,
    TaskRunController,
    TaskRunControllerRegistry,
    TaskRunStatus,
    TurnManager,
    TurnStatus,
    TurnWorkingCopy,
)
from agent_core.intent_generation import IntentGenerationResult

NOW = 1_800_000_000.0


@dataclass
class Sandbox:
    calls: list[str] = field(default_factory=list)

    def create(self, task_run_id: str) -> str:
        self.calls.append("create")
        return f"sandbox-{task_run_id}"

    def interrupt(self, sandbox_id: str) -> None:
        self.calls.append("interrupt")

    def destroy(self, sandbox_id: str) -> None:
        self.calls.append("destroy")


class Evidence:
    def __init__(self) -> None:
        self.commands: set[str] = set()
        self.outputs: set[str] = set()
        self.records: dict[str, ExecutionEvidence] = {}

    def command_exists(self, command_ref: str) -> bool:
        return command_ref in self.commands

    def output_exists(self, output_ref: str) -> bool:
        return output_ref in self.outputs

    def lookup(self, execution_id: str) -> ExecutionEvidence | None:
        return self.records.get(execution_id)


@dataclass
class Token:
    cancelled: bool = False

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled


class Scenario:
    def __init__(self, root: Path, count: int = 2, *, initial_turn: bool = True) -> None:
        self.now = NOW
        self.clock = lambda: self.now
        self.sandbox = Sandbox()
        self.evidence = Evidence()
        self.store = SqliteControlStore.open(root / "control.sqlite", clock=self.clock)
        self.repository = ControlStateRepository(self.store, self.evidence)
        self.controller = TaskRunController(
            self.sandbox,
            clock=self.clock,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
        )
        self.task_id = str(uuid4())
        self.controller.start(
            TaskRequest.from_dict(
                {
                    "goal": "solve challenge",
                    "target": {"endpoints": [{"url": "http://example.test:8080"}]},
                    "deadline_seconds": 120,
                }
            ),
            ModelBackend.CODEX_CLI,
            str(uuid4()),
            task_run_id=self.task_id,
        )
        self.turn_id = None
        if initial_turn:
            registration = TurnManager.prepare_turn_registration(
                state=self.controller.control_state,
                mutation_id=str(uuid4()),
                turn_target="solve challenge",
                candidates=tuple(IntentCandidate(f"approach {index}") for index in range(count)),
            )
            self.controller.commit_turn_registration(registration, str(uuid4()))
            self.turn_id = registration.turn_id

    @property
    def state(self):
        return self.controller.control_state

    @property
    def work(self) -> TurnWorkingCopy:
        return TurnWorkingCopy(self.state, self.evidence)

    def commit(self, operation) -> None:
        self.controller.commit_turn_operation(operation, str(uuid4()))


class ScriptedRunner:
    def __init__(self, scenario: Scenario, outcomes: list[str]) -> None:
        self.scenario = scenario
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def run(self, intent_id: str) -> IntentRunResult:
        self.calls.append(intent_id)
        outcome = self.outcomes.pop(0)
        if outcome == "step_timeout":
            return self._timeout_twice(intent_id)
        if outcome not in {"target_mismatch", "target_mismatch_committed", "succeeded"}:
            return IntentRunResult(
                self.scenario.task_id, intent_id, outcome, reason=outcome
            )

        plan_id = f"plan-{len(self.calls)}"
        validation_id = f"validation-{len(self.calls)}"
        execution_id = f"execution-{len(self.calls)}"
        self.scenario.commit(
            self.scenario.work.register_plan(
                Plan(plan_id, intent_id, (PlanStep("run"),))
            )
        )
        self.scenario.commit(self.scenario.work.start_plan(plan_id))
        command_ref = f"command-{len(self.calls)}"
        self.scenario.evidence.commands.add(command_ref)
        self.scenario.commit(
            self.scenario.work.append_plan_validation_record(
                PlanValidationRecordDraft(
                    validation_id,
                    plan_id,
                    self.scenario.task_id,
                    0,
                    1,
                    PlanValidationStatus.SUCCEEDED,
                    None,
                    "command generated",
                    command_ref,
                )
            )
        )
        self.scenario.commit(
            self.scenario.work.create_execution(
                Execution(execution_id, plan_id, 0, command_ref),
                plan_validation_id=validation_id,
            )
        )
        self.scenario.commit(self.scenario.work.start_execution(execution_id))
        output_ref = f"output-{len(self.calls)}"
        self.scenario.evidence.outputs.add(output_ref)
        self.scenario.evidence.records[execution_id] = ExecutionEvidence(
            self.scenario.task_id, "http://example.test:8080", NOW
        )
        self.scenario.commit(
            self.scenario.work.complete_execution(
                execution_id=execution_id, output_ref=output_ref
            )
        )
        self.scenario.commit(self.scenario.work.complete_plan(plan_id))
        if outcome == "succeeded":
            fact_id = f"fact-{len(self.calls)}"
            self.scenario.commit(
                self.scenario.work.create_fact(
                    Fact(
                        fact_id,
                        next(
                            item.turn_id
                            for item in self.scenario.state.intents
                            if item.intent_id == intent_id
                        ),
                        "verified observation",
                        execution_id,
                    )
                )
            )
            self.scenario.commit(
                self.scenario.work.verify_fact(
                    fact_id=fact_id,
                    turn_target_satisfied=True,
                    evidence_supports_content=True,
                )
            )
            self.scenario.commit(
                self.scenario.work.succeed_intent_with_fact(
                    intent_id=intent_id, fact_id=fact_id
                )
            )
            return IntentRunResult(self.scenario.task_id, intent_id, "succeeded")
        if outcome == "target_mismatch_committed":
            self.scenario.commit(self.scenario.work.record_target_mismatch(
                plan_id=plan_id, evidence_ref=execution_id,
                short_reason="target did not match",
            ))
            return IntentRunResult(
                self.scenario.task_id, intent_id, outcome,
                plan_id, execution_id, "target did not match",
            )
        return IntentRunResult(
            self.scenario.task_id,
            intent_id,
            "target_mismatch",
            plan_id,
            execution_id,
            "target did not match",
        )

    def _timeout_twice(self, intent_id: str) -> IntentRunResult:
        plan_id = f"timeout-plan-{len(self.calls)}"
        self.scenario.commit(self.scenario.work.register_plan(
            Plan(plan_id, intent_id, (PlanStep("probe"),))
        ))
        self.scenario.commit(self.scenario.work.start_plan(plan_id))
        latest_execution = ""
        for attempt in (1, 2):
            command_ref = f"timeout-command-{len(self.calls)}-{attempt}"
            validation_id = f"timeout-validation-{len(self.calls)}-{attempt}"
            latest_execution = f"timeout-execution-{len(self.calls)}-{attempt}"
            self.scenario.evidence.commands.add(command_ref)
            self.scenario.commit(self.scenario.work.append_plan_validation_record(
                PlanValidationRecordDraft(
                    validation_id, plan_id, self.scenario.task_id, 0, attempt,
                    PlanValidationStatus.SUCCEEDED, None, "command authorized", command_ref,
                )
            ))
            self.scenario.commit(self.scenario.work.create_execution(
                Execution(latest_execution, plan_id, 0, command_ref),
                plan_validation_id=validation_id,
            ))
            self.scenario.commit(self.scenario.work.start_execution(latest_execution))
            self.scenario.evidence.outputs.add(latest_execution)
            self.scenario.evidence.records[latest_execution] = ExecutionEvidence(
                self.scenario.task_id, None, NOW,
            )
            self.scenario.commit(self.scenario.work.record_step_timeout(
                execution_id=latest_execution, output_ref=latest_execution,
            ))
        return IntentRunResult(
            self.scenario.task_id, intent_id, "step_timeout", plan_id,
            latest_execution, "step_timeout_retry_exhausted",
        )


class Generator:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.calls = 0

    async def generate_next(
        self, task_run_id: str, turn_target: str, token: Token
    ) -> IntentGenerationResult:
        assert task_run_id == self.scenario.task_id
        assert not token.is_cancelled
        self.calls += 1
        registration = TurnManager.prepare_turn_registration(
            state=self.scenario.state,
            mutation_id=str(uuid4()),
            turn_target=turn_target,
            candidates=(IntentCandidate(f"generated {self.calls}"),),
        )
        self.scenario.controller.commit_turn_registration(registration, str(uuid4()))
        return IntentGenerationResult(registration.turn_id, None, False)


def test_first_turn_is_generated_from_committed_goal(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, initial_turn=False)
    runner = ScriptedRunner(scenario, ["inconclusive"])
    generator = Generator(scenario)

    result = asyncio.run(
        RunSupervisor(scenario.controller, scenario.evidence, runner, generator=generator).run()
    )

    assert result.stop == "paused_inconclusive"
    assert result.generated_next_turn
    assert generator.calls == 1
    assert scenario.state.turns[0].turn_target == scenario.state.task_run.request.goal
    assert len(runner.calls) == 1


def test_first_turn_model_error_does_not_create_turn_or_dispatch(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, initial_turn=False)

    class ErrorGenerator:
        async def generate_next(self, task_run_id, turn_target, token):
            return IntentGenerationResult(
                None, ModelError(ModelErrorKind.PROVIDER_FAILURE, "provider_down"), False
            )

    runner = ScriptedRunner(scenario, [])
    result = asyncio.run(
        RunSupervisor(scenario.controller, scenario.evidence, runner, generator=ErrorGenerator()).run()
    )
    assert result.stop == "model_error"
    assert result.reason == "provider_down"
    assert scenario.state.task_run.status is TaskRunStatus.RUNNING
    assert not scenario.state.turns and not runner.calls


def test_generator_without_committed_turn_requires_recovery(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, initial_turn=False)

    class EmptyGenerator:
        async def generate_next(self, task_run_id, turn_target, token):
            return IntentGenerationResult(str(uuid4()), None, False)

    runner = ScriptedRunner(scenario, [])
    with pytest.raises(SupervisorRecoveryRequired, match="did_not_register_expected_turn"):
        asyncio.run(
            RunSupervisor(scenario.controller, scenario.evidence, runner, generator=EmptyGenerator()).run()
        )
    assert not runner.calls


def test_supervisor_runs_priority_order_and_pauses_unknown(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    runner = ScriptedRunner(scenario, ["target_mismatch", "inconclusive"])
    result = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())

    assert result.stop == "paused_inconclusive"
    assert len(runner.calls) == 2
    intents = sorted(scenario.state.intents, key=lambda item: item.priority)
    assert [item.status for item in intents] == [IntentStatus.FAILED, IntentStatus.INCONCLUSIVE]
    assert scenario.state.turns[0].status is TurnStatus.INCONCLUSIVE
    assert scenario.state.task_run.status is TaskRunStatus.RUNNING


def test_exhausted_step_timeout_tries_sibling_then_pauses(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    runner = ScriptedRunner(scenario, ["step_timeout", "inconclusive"])
    result = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())

    assert result.stop == "paused_inconclusive"
    assert len(runner.calls) == 2
    assert [item.status for item in scenario.state.intents] == [
        IntentStatus.FAILED, IntentStatus.INCONCLUSIVE,
    ]
    assert scenario.state.turns[0].status is TurnStatus.INCONCLUSIVE


def test_exhausted_step_timeout_alone_pauses_without_new_turn(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["step_timeout"])
    generator = Generator(scenario)
    result = asyncio.run(RunSupervisor(
        scenario.controller, scenario.evidence, runner, generator=generator,
    ).run())

    assert result.stop == "paused_inconclusive"
    assert result.reason == "step_timeout_retry_exhausted"
    assert scenario.state.turns[0].status is TurnStatus.INCONCLUSIVE
    assert generator.calls == 0


def test_undispatched_timeout_twice_pauses_without_execution(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)

    class UndispatchedTimeoutRunner:
        async def run(self, intent_id):
            plan_id = "budget-plan"
            scenario.commit(scenario.work.register_plan(
                Plan(plan_id, intent_id, (PlanStep("probe"),))
            ))
            scenario.commit(scenario.work.start_plan(plan_id))
            latest = ""
            for attempt in (1, 2):
                latest = f"budget-validation-{attempt}"
                scenario.commit(scenario.work.append_plan_validation_record(
                    PlanValidationRecordDraft(
                        latest, plan_id, scenario.task_id, 0, attempt,
                        PlanValidationStatus.FAILED, FailureType.STEP_TIMEOUT,
                        "step budget exhausted before dispatch", None,
                    )
                ))
                scenario.commit(scenario.work.record_command_generation_failure(
                    plan_validation_id=latest,
                ))
            return IntentRunResult(
                scenario.task_id, intent_id, "step_timeout", plan_id, latest,
                "step_timeout_retry_exhausted",
            )

    result = asyncio.run(RunSupervisor(
        scenario.controller, scenario.evidence, UndispatchedTimeoutRunner(),
    ).run())
    assert result.stop == "paused_inconclusive"
    assert not scenario.state.executions
    assert scenario.state.turns[0].status is TurnStatus.INCONCLUSIVE


def test_inconclusive_turn_does_not_generate_on_reentry(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["inconclusive"])
    generator = Generator(scenario)

    first = asyncio.run(
        RunSupervisor(scenario.controller, scenario.evidence, runner, generator=generator).run()
    )
    second = asyncio.run(
        RunSupervisor(scenario.controller, scenario.evidence, runner, generator=generator).run()
    )

    assert first.stop == second.stop == "paused_inconclusive"
    assert second.reason == "turn_already_inconclusive"
    assert len(scenario.state.turns) == 1
    assert len(runner.calls) == 1
    assert generator.calls == 0


def test_all_target_mismatches_generate_and_resume_next_turn(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    runner = ScriptedRunner(scenario, ["target_mismatch", "target_mismatch", "inconclusive"])
    generator = Generator(scenario)
    result = asyncio.run(
        RunSupervisor(
            scenario.controller, scenario.evidence, runner, generator=generator
        ).run()
    )

    assert result.stop == "paused_inconclusive"
    assert result.generated_next_turn
    assert generator.calls == 1
    assert len(runner.calls) == 3
    assert [item.status for item in scenario.state.turns] == [
        TurnStatus.FAILED,
        TurnStatus.INCONCLUSIVE,
    ]


def test_runner_committed_mismatches_continue_without_double_commit(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    runner = ScriptedRunner(
        scenario,
        ["target_mismatch_committed", "target_mismatch_committed", "inconclusive"],
    )
    generator = Generator(scenario)
    result = asyncio.run(RunSupervisor(
        scenario.controller, scenario.evidence, runner, generator=generator,
    ).run())
    assert result.stop == "paused_inconclusive"
    assert len(runner.calls) == 3
    assert generator.calls == 1
    assert [item.status for item in scenario.state.turns] == [
        TurnStatus.FAILED, TurnStatus.INCONCLUSIVE,
    ]
    failures = [item for item in scenario.state.failure_summaries
                if item.failure_type is FailureType.TARGET_MISMATCH]
    assert len(failures) == 2


def test_model_error_stops_dispatch_but_preserves_task_run(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["model_error"])
    result = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())

    assert result.stop == "model_error"
    assert scenario.state.task_run.status is TaskRunStatus.RUNNING
    assert scenario.state.turns[0].status is TurnStatus.RUNNING
    assert scenario.state.intents[0].status is IntentStatus.RUNNING
    assert scenario.state.failure_summaries[-1].failure_type.value == "model_error"


def test_model_error_is_replayed_without_dispatching_again(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)

    class CountingRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, intent_id: str) -> IntentRunResult:
            self.calls += 1
            return IntentRunResult(
                scenario.task_id, intent_id, "model_error", reason="provider_down"
            )

    runner = CountingRunner()
    first = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())
    second = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())

    assert first.stop == second.stop == "model_error"
    assert second.reason == "provider_down"
    assert runner.calls == 1


def test_recovery_required_preserves_running_intent_and_turn(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["recovery_required"])

    result = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())

    assert result.stop == "recovery_required"
    assert scenario.state.task_run.status is TaskRunStatus.RUNNING
    assert scenario.state.turns[0].status is TurnStatus.RUNNING
    assert scenario.state.intents[0].status is IntentStatus.RUNNING

    second = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())
    assert second.stop == "recovery_required"
    assert len(runner.calls) == 1


def test_running_intent_uses_explicit_runner_resume(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    intent_id = scenario.state.intents[0].intent_id
    scenario.commit(scenario.work.select_next_intent(scenario.turn_id))

    class ResumableRunner:
        def __init__(self):
            self.resumes = 0

        async def run(self, intent_id):
            raise AssertionError("already running Intent must use resume")

        async def resume(self, resumed_intent_id):
            assert resumed_intent_id == intent_id
            self.resumes += 1
            return IntentRunResult(scenario.task_id, intent_id, "inconclusive")

    runner = ResumableRunner()
    result = asyncio.run(RunSupervisor(
        scenario.controller, scenario.evidence, runner,
    ).run_once())
    assert result.stop == "paused_inconclusive"
    assert runner.resumes == 1


def test_success_completes_only_the_current_turn(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["succeeded"])
    result = asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, runner).run())

    assert result.stop == "turn_completed"
    assert scenario.state.turns[0].status is TurnStatus.COMPLETED
    assert scenario.state.intents[0].status is IntentStatus.SUCCEEDED
    assert scenario.state.task_run.status is TaskRunStatus.RUNNING


def test_reentry_completes_turn_after_intent_success_commit(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=2)
    first_intent = scenario.state.intents[0].intent_id
    scenario.commit(scenario.work.select_next_intent(scenario.turn_id))
    runner = ScriptedRunner(scenario, ["succeeded"])
    asyncio.run(runner.run(first_intent))
    assert scenario.state.intents[0].status is IntentStatus.SUCCEEDED
    assert scenario.state.turns[0].status is TurnStatus.RUNNING

    result = asyncio.run(RunSupervisor(
        scenario.controller, scenario.evidence, runner,
    ).run_once())
    assert result.stop == "turn_completed"
    assert result.intent_id == first_intent
    assert scenario.state.turns[0].status is TurnStatus.COMPLETED
    assert len(runner.calls) == 1


def test_runner_scope_mismatch_requires_recovery(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)

    class BadRunner:
        async def run(self, intent_id: str) -> IntentRunResult:
            return IntentRunResult("other-task", intent_id, "inconclusive")

    with pytest.raises(SupervisorRecoveryRequired, match="scope_mismatch"):
        asyncio.run(RunSupervisor(scenario.controller, scenario.evidence, BadRunner()).run_once())
    assert scenario.state.intents[0].status is IntentStatus.RUNNING


def test_cancelled_token_stops_before_dispatch(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["model_error"])
    token = Token(cancelled=True)
    result = asyncio.run(
        RunSupervisor(scenario.controller, scenario.evidence, runner).run_once(token)
    )

    assert result.stop == "cancelled"
    assert not runner.calls
    assert scenario.state.task_run.status is TaskRunStatus.CANCELLED


def test_deadline_stops_before_dispatch(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path, count=1)
    runner = ScriptedRunner(scenario, ["model_error"])
    scenario.now = NOW + 120
    result = asyncio.run(
        RunSupervisor(scenario.controller, scenario.evidence, runner).run_once()
    )

    assert result.stop == "deadline_exceeded"
    assert not runner.calls
    assert scenario.state.task_run.status is TaskRunStatus.DEADLINE_EXCEEDED
