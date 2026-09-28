"""Deterministic v2 fixture adapters; no network or Docker access."""

from __future__ import annotations

import base64
import hashlib
from uuid import uuid4

from agent_core.action_audit import T6ActionJournal
from agent_core.actions import ProbeAction
from agent_core.command_contracts import T6CommandResult
from agent_core.control_store import TargetNetworkBinding
from agent_core.intent_generation import IntentGenerationResult
from agent_core.intent_postconditions import FileDigestObservation
from agent_core.modeling import (
    ModelError,
    ModelErrorKind,
    ModelRequestType,
    NormalizedIntentResponse,
)


class SimulatedTargetNetwork:
    def create(self, task_run_id: str, target_url: str) -> TargetNetworkBinding:
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        return TargetNetworkBinding(
            digest(task_run_id + "/private"),
            digest(task_run_id + "/egress"),
            digest(task_run_id + "/forwarder"),
            digest(target_url),
            target_url,
        )

    def bind(self, task_run_id: str, binding: TargetNetworkBinding, *, terminal: bool) -> None:
        return None

    def destroy(self, task_run_id: str, binding: TargetNetworkBinding) -> None:
        return None


class SimulatedTaskSandbox:
    def __init__(self) -> None:
        self.bindings: dict[str, str] = {}
        self.target_network_binding: TargetNetworkBinding | None = None

    def configure_target_network(self, task_run_id: str, binding: TargetNetworkBinding) -> None:
        self.target_network_binding = binding

    def create(self, task_run_id: str) -> str:
        sandbox_id = "simulated-" + str(uuid4())
        self.bindings[task_run_id] = sandbox_id
        return sandbox_id

    def bind(self, task_run_id: str, sandbox_id: str, *, terminal: bool) -> None:
        if self.bindings.get(task_run_id) not in {None, sandbox_id}:
            raise ValueError("simulated_sandbox_binding_conflict")
        self.bindings[task_run_id] = sandbox_id

    def quiesce(self, sandbox_id: str) -> None:
        return None

    def interrupt(self, sandbox_id: str) -> None:
        return None

    def destroy(self, sandbox_id: str) -> None:
        self.bindings = {task: value for task, value in self.bindings.items()
                         if value != sandbox_id}

    def close(self) -> None:
        return None


class TaskFixtureRuntime:
    def __init__(self, fixture: dict, controller, journal: T6ActionJournal) -> None:
        self.fixture = fixture
        self.controller = controller
        self.journal = journal
        self.quiesced = False

    def _records(self):
        state = self.controller.control_state
        records = [record for intent in state.intents
                   for record in self.journal.list_for_intent(
                       state.task_run.task_run_id, intent.intent_id,
                   )]
        return sorted(records, key=lambda item: (item.start.started_at,
                                                  item.start.model_request_id))

    def next_action_available(self) -> bool:
        records = self._records()
        index = len(records)
        if index >= len(self.fixture["actions"]):
            return False
        if self.fixture["actions"][index].get("kind") != "probe":
            return True
        prior_probes = sum(
            isinstance(self.journal.read_result(record), ProbeAction)
            for record in records if record.completion is not None
            and record.completion.result_kind == "action"
        )
        return prior_probes < len(self.fixture["commands"])

    def invoke(self, request, timeout_seconds, cancellation_token):
        if cancellation_token.is_cancelled:
            raise ValueError("simulated_model_cancelled")
        if getattr(request, "request_type", None) is ModelRequestType.INTENT_GENERATION:
            index = len(self.controller.control_state.turns)
            if index >= len(self.fixture["turns"]):
                return ModelError(ModelErrorKind.PROVIDER_FAILURE, "fixture_exhausted")
            return NormalizedIntentResponse(self.fixture["turns"][index]["candidates"])
        records = self._records()
        current = next((item for item in records
                        if item.start.model_request_id == request.model_request_id), None)
        if current is None:
            raise ValueError("simulated_action_claim_missing")
        index = records.index(current)
        if index >= len(self.fixture["actions"]):
            return ModelError(ModelErrorKind.PROVIDER_FAILURE, "fixture_exhausted")
        action = dict(self.fixture["actions"][index])
        state = self.controller.control_state
        if action.get("kind") == "complete_intent" and action.get("fact_ids") == [
            "$latest_verified_fact"
        ]:
            facts = [item for item in state.facts if item.status.value == "verified"]
            if not facts:
                raise ValueError("simulated_fact_placeholder_unresolved")
            action["fact_ids"] = [facts[-1].fact_id]
        if action.get("execution_id") == "$latest_completed_execution":
            executions = [item for item in state.executions
                          if item.status.value == "succeeded"]
            if not executions:
                raise ValueError("simulated_execution_placeholder_unresolved")
            action["execution_id"] = executions[-1].execution_id
        return action

    def _command_entry(self, request_id: str) -> dict:
        probes = []
        for record in self._records():
            if record.completion is None or record.completion.result_kind != "action":
                continue
            action = self.journal.read_result(record)
            if isinstance(action, ProbeAction):
                probes.append(record)
        current = next((index for index, item in enumerate(probes)
                        if item.start.resource_ids.command_request_id == request_id), None)
        if current is None or current >= len(self.fixture["commands"]):
            raise ValueError("simulated_command_not_declared")
        return self.fixture["commands"][current]

    async def execute(self, approved, token) -> T6CommandResult:
        if token.is_cancelled or self.quiesced:
            raise ValueError("simulated_sandbox_not_accepting")
        entry = self._command_entry(approved.request.request_id)
        timed_out = entry.get("confirmed_timeout", False)
        return T6CommandResult(
            approved.sandbox_id,
            entry["exit_code"],
            base64.b64decode(entry["stdout_base64"], validate=True),
            base64.b64decode(entry["stderr_base64"], validate=True),
            False, False, timed_out, True, True, True,
        )

    def quiesce(self, sandbox_id: str) -> None:
        self.quiesced = True

    def hash_file(self, task_run_id: str, sandbox_id: str, path: str) -> FileDigestObservation:
        state = self.controller.control_state
        if state.task_run.task_run_id != task_run_id or state.task_run.sandbox_id != sandbox_id:
            raise ValueError("simulated_digest_scope_mismatch")
        execution = next((item for item in reversed(state.executions)
                          if item.status.value == "succeeded"), None)
        if execution is None:
            return FileDigestObservation("missing")
        files = self._command_entry(execution.execution_id).get("work_files", {})
        if path not in files:
            return FileDigestObservation("missing")
        return FileDigestObservation(
            "present", hashlib.sha256(base64.b64decode(files[path], validate=True)).hexdigest(),
        )


class FixtureTurnGenerator:
    def __init__(self, inner, runtime: TaskFixtureRuntime):
        self.inner = inner
        self.runtime = runtime

    async def generate_next(self, task_run_id, turn_target, token):
        if len(self.runtime.controller.control_state.turns) >= len(self.runtime.fixture["turns"]):
            return IntentGenerationResult(
                None, ModelError(ModelErrorKind.PROVIDER_FAILURE, "fixture_exhausted"), False,
            )
        return await self.inner.generate_next(task_run_id, turn_target, token)
