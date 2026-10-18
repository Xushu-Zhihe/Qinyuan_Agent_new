"""Persistent D7 Web TaskRun orchestration over the existing Controller."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agent_adapters.application import RecordedSandbox, evidence_components
from agent_adapters.deepseek_model import DeepSeekSettings
from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
from agent_adapters.web.http_adapter import (
    D7HttpAdapter,
    D7HttpRequest,
    D7HttpResponseEvidenceService,
)
from agent_adapters.web.model_adapter import (
    d7_model_transport_config_ref,
    invoke_d7_web_role,
)
from agent_adapters.web.request_contract import V3SimulationRequest
from agent_adapters.web.sandbox_transport import D7SandboxHttpTransport
from agent_adapters.web.web_request import D7WebRequest
from agent_adapters.target_network_cleanup import RecordedTargetNetwork
from agent_core.contracts import ModelBackend
from agent_core.control_state_repository import ControlStateRepository
from agent_core.controller import TaskRunController, TaskRunControllerRegistry
from agent_core.control_store import PlanValidationRecordDraft
from agent_core.domain import (
    Execution,
    ExecutionStatus,
    Fact,
    FactStatus,
    IntentStatus,
    Plan,
    PlanStatus,
    PlanStep,
    PlanValidationStatus,
    TurnStatus,
)
from agent_core.web_contracts import D7WebExecutionRecord, D7WebPlanRecord, D7WebRunConfig
from agent_core.goal_candidates import D7AnswerCandidate
from agent_core.operation_accounting import (
    D4OperationCompletion,
    D4OperationStart,
    D4RecoveryRequired,
)
from agent_core.modeling import IntentCandidate, ModelError, ModelErrorKind
from agent_core.runtime import TERMINAL_STATUSES, TaskRunStatus
from agent_core.turn_manager import TurnManager
from agent_core.turn_working_copy import TurnWorkingCopy
from agent_adapters.run_files import run_lock
from agent_core.sqlite_control_store import SqliteControlStore


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        .encode("utf-8")
    ).hexdigest()


class _Cancellation:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def is_cancelled(self) -> bool:
        return (self.directory / "cancel-request.json").exists()


class _D7ModelInvocationFailed(RuntimeError):
    def __init__(self, kind: ModelErrorKind, short_reason: str) -> None:
        self.kind = kind
        self.short_reason = short_reason
        super().__init__("d7_model_invocation_failed")


class _WebExecutionEvidence:
    def __init__(self, store: SqliteControlStore, task_run_id: str, endpoint: str) -> None:
        self.store = store
        self.task_run_id = task_run_id
        self.endpoint = endpoint

    def command_exists(self, command_ref: str) -> bool:
        return self.store.load_d7_web_plan(command_ref) is not None

    def output_exists(self, output_ref: str) -> bool:
        return any(
            record.state == "completed" and record.response_evidence_id == output_ref
            for record in self.store.list_d7_web_executions(self.task_run_id)
        )

    def lookup(self, execution_id: str):
        from agent_core.domain import ExecutionEvidence

        record = self.store.load_d7_web_execution(execution_id)
        if record is None or record.state != "completed" or record.finished_at is None:
            return None
        if record.task_run_id != self.task_run_id:
            return None
        return ExecutionEvidence(self.task_run_id, self.endpoint, record.finished_at)


class D7WebApplication:
    """Own one real Web run, its persistent control state, and HTTP session."""

    def __init__(self, directory: Path, declaration: dict, *, settings: DeepSeekSettings | None) -> None:
        self.directory = Path(directory)
        self.declaration = declaration
        self.task_run_id = declaration["task_run_id"]
        self.request = D7WebRequest.parse(declaration["request"])
        self.settings = settings
        self.store = SqliteControlStore.open(self.directory / "control.sqlite")
        self.evidence_components = evidence_components(self.directory, self.store)
        evidence = _WebExecutionEvidence(
            self.store,
            self.task_run_id,
            self.request.authorization.allowed_origin,
        )
        self.repository = ControlStateRepository(self.store, evidence)
        from Sandbox.docker_runtime import DockerRuntime
        from Sandbox.command_sandbox import T6TaskRunSandbox
        from Sandbox.settings import SandboxSettings
        from Sandbox.target_network import T6TargetNetwork

        runtime = DockerRuntime(SandboxSettings(), prepare_network=False)
        sandbox = T6TaskRunSandbox(runtime)
        target_network = RecordedTargetNetwork(T6TargetNetwork(runtime), self.directory)
        self._runtime = runtime
        self._sandbox = sandbox
        self.controller = TaskRunController(
            RecordedSandbox(sandbox, self.directory),
            clock=time.time,
            store=self.store,
            repository=self.repository,
            registry=TaskRunControllerRegistry(),
            target_network_adapter=target_network,
        )
        self.run_config = D7WebRunConfig.from_dict(declaration["d7_web_config"])
        stored = self.store.load_d7_web_run_config(self.task_run_id)
        if stored is not None and stored != self.run_config:
            raise ValueError("d7_web_persisted_configuration_mismatch")
        self.http_evidence = None
        self.http = None
        self.candidate_evidence = None
        self.token = _Cancellation(self.directory)

    @property
    def state(self):
        return self.controller.control_state

    def _attach_http(self) -> None:
        if self.http is not None:
            return
        self.http_evidence = D7HttpResponseEvidenceService(
            self.directory, self.task_run_id, create=True
        )
        task = self.state.task_run
        self.http = D7HttpAdapter(
            self.task_run_id,
            self.run_config.endpoint,
            self.http_evidence.store,
            transport=D7SandboxHttpTransport(
                self._sandbox,
                sandbox_id=task.sandbox_id,
                run_config=self.run_config,
                network_binding=task.target_network,
                target_network=self.controller._target_network,
            ),
        )
        self.candidate_evidence = D7CandidateEvidenceService(
            self.directory,
            self.task_run_id,
            trusted_adapter_ids=("d7_web_application",),
            create=True,
        )

    def _close(self) -> None:
        if self.http is not None:
            self.http.close()
        state = self.controller._persistent_state
        if (
            state is not None
            and (
                state.task_run.status in TERMINAL_STATUSES
                or state.task_run.status is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK
            )
        ):
            self.controller.close()
        self.store.close()
        self._runtime.close()

    def _can_resume_failed_model_call(self) -> bool:
        state = self.state
        if state.task_run.status is not TaskRunStatus.RUNNING or not state.turns:
            return False
        turn = state.turns[-1]
        if turn.status is not TurnStatus.RUNNING:
            return False
        running_intents = [
            item
            for item in state.intents
            if item.turn_id == turn.turn_id and item.status is IntentStatus.RUNNING
        ]
        if len(running_intents) != 1:
            return False
        intent = running_intents[0]
        records = self.store.list_d4_operations(self.task_run_id)
        if not records or any(record.completion is None for record in records):
            return False
        for record in records:
            if record.application_ref is None and not (
                record.start.category in {"planning", "action"}
                and record.completion.result_kind == "failed"
            ):
                return False
        if any(
            item.state != "completed"
            for item in self.store.list_d7_web_executions(self.task_run_id)
        ):
            return False
        matching = [
            record
            for record in records
            if record.start.category == "action"
            and record.start.resource_ref == intent.intent_id
            and record.start.role_config_ref == self.run_config.inner_role.role_config_ref
        ]
        if not matching:
            return False
        latest = max(matching, key=lambda record: record.start.started_at)
        return (
            latest.start.base_checkpoint_sequence == state.latest_checkpoint.sequence
            and latest.completion.result_kind == "failed"
            and latest.application_ref is None
            and latest.completion.evidence_sha256
            == hashlib.sha256(ModelErrorKind.INVALID_RESPONSE.value.encode()).hexdigest()
        )

    def run(self, operation: str) -> dict:
        try:
            if operation == "start":
                self.controller.start(
                    self.request.task_request,
                    ModelBackend.DEEPSEEK_OPENAI,
                    self.declaration["ids"]["start"],
                    source_root=self.request.source_root,
                    task_run_id=self.task_run_id,
                    d7_web_config=self.run_config,
                )
                self._attach_http()
                self._execute()
            elif operation == "cancel":
                self.controller.attach_for_cancellation(self.task_run_id)
                self.controller.cancel(self.declaration["ids"]["cancel"])
            elif operation == "cleanup":
                self.controller.attach(self.task_run_id)
                self.controller.cleanup_sandbox()
            elif operation == "resume":
                self.controller.attach(self.task_run_id)
                if self.controller.status() not in TERMINAL_STATUSES:
                    pending = self.store.list_d4_unresolved_operations(self.task_run_id)
                    started_http = any(
                        item.state == "started"
                        for item in self.store.list_d7_web_executions(self.task_run_id)
                    )
                    if pending or started_http:
                        raise D4RecoveryRequired("d7_web_started_operation_outcome_unknown")
                    if self.controller.status() is TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK:
                        pass
                    elif not self.state.turns and not self.store.list_d4_operations(self.task_run_id):
                        self._attach_http()
                        self._execute()
                    elif self._can_resume_failed_model_call():
                        self._attach_http()
                        self._execute_turn()
                    else:
                        raise D4RecoveryRequired("d7_web_resume_checkpoint_not_replayable")
            else:
                raise ValueError("unsupported_v3_operation")
            from agent_adapters.web.task_result import snapshot_v3_task_result

            return snapshot_v3_task_result(self.directory, self.declaration)
        finally:
            self._close()

    def _role_settings(self, role) -> DeepSeekSettings:
        if self.settings is None:
            raise ValueError("d7_model_settings_required")
        return replace(self.settings, model=role.model)

    def _invoke(self, role, stage: str, context: dict, resource_ref: str) -> tuple[dict, str]:
        if self.token.is_cancelled:
            raise D4RecoveryRequired("d7_cancel_requested_before_model_dispatch")
        category = "planning" if role.role == "outer_planning" else "action"
        prior = [
            record
            for record in self.store.list_d4_operations(self.task_run_id)
            if record.start.category == category
            and record.start.resource_ref == resource_ref
            and record.start.role_config_ref == role.role_config_ref
        ]
        if any(record.completion is None for record in prior):
            raise D4RecoveryRequired("d7_model_started_operation_outcome_unknown")
        attempt = len(prior) + 1
        material = (
            f"qinyuan:d7-web:model:{self.task_run_id}:"
            f"{self.state.latest_checkpoint.sequence}:{stage}:{resource_ref}"
        )
        if attempt > 1:
            material += f":attempt:{attempt}"
        operation_id = str(uuid5(NAMESPACE_URL, material))
        start = D4OperationStart(
            operation_id=operation_id,
            task_run_id=self.task_run_id,
            category=category,
            started_at=time.time(),
            base_checkpoint_sequence=self.state.latest_checkpoint.sequence,
            context_sha256=_digest(context),
            resource_ref=resource_ref,
            budget_policy_ref=self.run_config.d4_budget.budget_policy_ref,
            role=role.role,
            role_config_ref=role.role_config_ref,
        )
        existing = self.store.begin_d4_operation(start)
        if existing is not None:
            raise D4RecoveryRequired("d7_model_operation_result_requires_reconciliation")
        result = invoke_d7_web_role(
            role,
            self._role_settings(role),
            stage,
            context,
            min(180.0, max(1.0, self.state.task_run.deadline_at - time.time())),
            self.token,
        )
        if not isinstance(result, dict):
            kind = result.kind if isinstance(result, ModelError) else ModelErrorKind.INVALID_RESPONSE
            short_reason = (
                result.short_reason
                if isinstance(result, ModelError)
                else "d7_model_response_not_object"
            )
            completion = D4OperationCompletion(
                operation_id,
                time.time(),
                "failed",
                "d7-model-error",
                hashlib.sha256(str(kind).encode()).hexdigest(),
            )
            self.store.complete_d4_operation(completion)
            raise _D7ModelInvocationFailed(kind, short_reason)
        completion = D4OperationCompletion(
            operation_id,
            time.time(),
            "completed",
            "d7-model-response:" + operation_id,
            _digest(result),
        )
        self.store.complete_d4_operation(completion)
        return result, operation_id

    def _commit(self, factory) -> None:
        self.controller.commit_turn_operation(
            factory(TurnWorkingCopy(self.state, self.evidence_components[0])), str(uuid4())
        )

    def _register_turn(self) -> None:
        task = self.state.task_run
        context = {
            "goal": self.request.task_request.goal,
            "authorized_origin": self.run_config.endpoint,
            "verified_facts": [item.content for item in self.state.facts if item.status.value == "verified"],
            "prior_turns": [
                {"turn_target": turn.turn_target, "status": turn.status.value}
                for turn in self.state.turns
            ],
        }
        response, operation_id = self._invoke(self.run_config.outer_role, "web_intents", context, "turn-planning")
        candidates = tuple(
            IntentCandidate(item["description"], item["intent_type"])
            for item in response["candidates"]
        )
        target = response.get("turn_target") or self.request.task_request.goal
        registration = TurnManager.prepare_turn_registration(
            state=self.state,
            mutation_id=str(uuid4()),
            turn_target=target,
            candidates=candidates,
        )
        self.controller.commit_turn_registration(registration, str(uuid4()))
        self.store.mark_d4_operation_applied(operation_id, registration.turn_id)

    def _select_intent(self, turn_id: str) -> None:
        self._commit(lambda working: working.select_next_intent(turn_id))

    def _run_http_action(self, turn, intent, action: dict, operation_id: str) -> tuple[str, str]:
        from agent_adapters.web.http_adapter import D7HttpRequest

        request = D7HttpRequest(
            str(uuid4()),
            action["method"],
            action["path_query"],
            tuple((key, value) for key, value in action["headers"].items()),
            action["body"] or "",
        )
        method, path, headers, body = self.http.policy.validate(request)
        plan_id, execution_id = str(uuid4()), str(uuid4())
        request_value = {
            "method": method,
            "path_query": path,
            "headers": list(headers),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
        }
        request_hash = _digest(request_value)
        plan_record = D7WebPlanRecord(
            self.task_run_id,
            turn.turn_id,
            intent.intent_id,
            plan_id,
            method,
            request_hash,
            len(body),
            time.time(),
        )
        self.store.register_d7_web_plan(plan_record)
        self._commit(lambda working: working.register_plan(
            Plan(plan_id, intent.intent_id, (PlanStep(f"HTTP {method} {path[:160]}"),))
        ))
        self._commit(lambda working: working.start_plan(plan_id))
        validation_id = str(uuid4())
        self._commit(lambda working: working.append_plan_validation_record(
            PlanValidationRecordDraft(
                validation_id,
                plan_id,
                self.task_run_id,
                0,
                1,
                PlanValidationStatus.SUCCEEDED,
                None,
                "d7_web_action_validated",
                plan_id,
            )
        ))
        self._commit(lambda working: working.create_execution(
            Execution(execution_id, plan_id, 0, plan_id),
            plan_validation_id=validation_id,
        ))
        self._commit(lambda working: working.start_execution(execution_id))
        d4_id = str(uuid5(NAMESPACE_URL, f"qinyuan:d7-web:http:{request.operation_id}"))
        self.store.begin_d4_operation(D4OperationStart(
            d4_id,
            self.task_run_id,
            "tool_dispatch",
            time.time(),
            self.state.latest_checkpoint.sequence,
            request_hash,
            self.run_config.endpoint,
            self.run_config.d4_budget.budget_policy_ref,
        ))
        started = D7WebExecutionRecord(
            self.task_run_id,
            turn.turn_id,
            intent.intent_id,
            plan_id,
            execution_id,
            request.operation_id,
            "started",
            request_hash,
            len(body),
            time.time(),
        )
        self.store.start_d7_web_execution(started)
        try:
            result = self.http.execute(request)
        except Exception:
            unknown = replace(
                started,
                state="unknown",
                finished_at=time.time(),
            )
            self.store.complete_d7_web_execution(unknown)
            raise D4RecoveryRequired("d7_http_execution_outcome_unknown")
        completed = replace(
            started,
            state="completed",
            http_status=result.status,
            response_evidence_id=result.body_evidence_id,
            response_sha256=result.body_sha256,
            response_bytes=result.body_bytes,
            finished_at=time.time(),
        )
        self.store.complete_d7_web_execution(completed)
        self.store.complete_d4_operation(D4OperationCompletion(
            d4_id,
            completed.finished_at,
            "completed",
            result.body_evidence_id,
            result.body_sha256,
        ))
        self._commit(lambda working: working.complete_execution(
            execution_id=execution_id,
            output_ref=result.body_evidence_id,
        ))
        self._commit(lambda working: working.complete_plan(plan_id))
        self.store.mark_d4_operation_applied(d4_id, execution_id)
        if operation_id:
            self.store.mark_d4_operation_applied(operation_id, plan_id)
        return execution_id, result.body_text

    def _submit_candidate(self, turn, intent, response: dict, model_operation_id: str) -> None:
        execution_id = response["action"]["execution_id"]
        binding = self.store.load_d7_web_execution_binding(execution_id)
        if binding is None:
            raise ValueError("d7_candidate_execution_binding_missing")
        if (
            binding.task_run_id != self.task_run_id
            or binding.turn_id != turn.turn_id
            or binding.intent_id != intent.intent_id
            or response["action"]["evidence_version"] != binding.response_sha256
        ):
            raise ValueError("d7_candidate_execution_binding_mismatch")
        fact = Fact(
            str(uuid4()),
            turn.turn_id,
            f"Observed authorized HTTP response {binding.response_sha256[:16]}.",
            execution_id,
            FactStatus.CANDIDATE,
        )
        self._commit(lambda working: working.create_fact(fact))
        self._commit(lambda working: working.verify_fact(
            fact_id=fact.fact_id,
            turn_target_satisfied=True,
            evidence_supports_content=True,
        ))
        self._commit(lambda working: working.succeed_intent_with_fact(
            intent_id=intent.intent_id,
            fact_id=fact.fact_id,
        ))
        self._commit(lambda working: working.complete_turn(turn.turn_id))
        candidate = D7AnswerCandidate(
            task_run_id=self.task_run_id,
            candidate_id=str(uuid4()),
            turn_id=turn.turn_id,
            intent_id=intent.intent_id,
            plan_id=binding.plan_id,
            execution_id=execution_id,
            evidence_version=binding.response_sha256,
            source_ref=binding.response_evidence_id,
            source_kind="web",
            web_execution_binding=binding,
        )
        metadata = self.candidate_evidence.save_pending(
            candidate,
            response["action"]["value"],
            model_operation_id,
        )
        self.controller.register_answer_candidate(candidate, str(uuid4()))
        self.store.mark_d4_operation_applied(model_operation_id, candidate.candidate_id)

    def _execute_turn(self) -> bool:
        turn = self.state.turns[-1]
        running_intents = [
            item
            for item in self.state.intents
            if item.turn_id == turn.turn_id and item.status is IntentStatus.RUNNING
        ]
        if len(running_intents) > 1:
            raise D4RecoveryRequired("d7_web_multiple_running_intents")
        if not running_intents:
            self._select_intent(turn.turn_id)
            intent = next(
                item
                for item in self.state.intents
                if item.turn_id == turn.turn_id and item.status is IntentStatus.RUNNING
            )
        else:
            intent = running_intents[0]
        observations: list[dict[str, object]] = []
        for _ in range(48):
            if self.token.is_cancelled:
                return False
            latest = next((
                item for item in reversed(self.store.list_d7_web_executions(self.task_run_id))
                if item.turn_id == turn.turn_id and item.intent_id == intent.intent_id and item.state == "completed"
            ), None)
            context = {
                "goal": self.request.task_request.goal,
                "authorized_origin": self.run_config.endpoint,
                "turn_target": turn.turn_target,
                "intent": intent.description,
                "observations": observations[-8:],
                "latest_execution": None,
            }
            if latest is not None:
                body = self.http_evidence.read(latest.response_evidence_id)
                text = body[:65536].decode("utf-8", errors="ignore")
                context["latest_execution"] = {
                    "execution_id": latest.execution_id,
                    "evidence_version": latest.response_sha256,
                    "source_ref": latest.response_evidence_id,
                    "http_status": latest.http_status,
                    "response_text": text,
                    "response_text_truncated": latest.response_bytes > len(body[:65536]),
                }
            try:
                response, model_operation_id = self._invoke(
                    self.run_config.inner_role,
                    "web_action",
                    context,
                    intent.intent_id,
                )
            except _D7ModelInvocationFailed as exc:
                if (
                    exc.kind is ModelErrorKind.INVALID_RESPONSE
                    and exc.short_reason != "d7_web_model_request_rejected"
                ):
                    continue
                raise
            action = response["action"]
            if action["kind"] == "http_request":
                execution_id, body_text = self._run_http_action(turn, intent, action, model_operation_id)
                observations.append({"execution_id": execution_id, "response_text": body_text})
                continue
            if action["kind"] == "candidate_flag":
                if latest is None:
                    raise ValueError("d7_candidate_requires_completed_http_execution")
                self._submit_candidate(turn, intent, response, model_operation_id)
                return True
            if action["kind"] == "stop":
                self._commit(lambda working: working.mark_intent_inconclusive(intent_id=intent.intent_id))
                return False
        raise D4RecoveryRequired("d7_web_action_loop_budget_exhausted")

    def _execute(self) -> None:
        for _ in range(16):
            if self.state.task_run.status is not TaskRunStatus.RUNNING:
                return
            self._register_turn()
            if self._execute_turn():
                return
            if self.state.turns[-1].status not in {TurnStatus.COMPLETED, TurnStatus.INCONCLUSIVE, TurnStatus.FAILED}:
                return
        raise D4RecoveryRequired("d7_web_turn_budget_exhausted")


def run_d7_web_task(directory: Path, declaration: dict, *, operation: str, settings: DeepSeekSettings | None) -> dict:
    app = D7WebApplication(directory, declaration, settings=settings)
    return app.run(operation)


__all__ = ["D7WebApplication", "run_d7_web_task"]
