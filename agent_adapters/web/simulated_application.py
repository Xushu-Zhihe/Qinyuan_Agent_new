"""Local-only D7 simulation assembly over the existing D3 application."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from agent_adapters.multi_turn_application import D3Application
from agent_adapters.run_files import immutable_read, immutable_write
from agent_core.actions import CompleteIntentAction, ProbeAction, action_payload
from agent_core.attachment_import import AttachmentImportReadback
from agent_core.command_contracts import T6CommandResult
from agent_core.contracts import TaskRequest, Target
from agent_core.domain import TurnStatus
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.intent_postconditions import FileDigestObservation, FileSha256
from agent_core.modeling import CancellationToken
from agent_core.runtime import TaskRunStatus
from agent_core.turn_contracts import D1RoleConfig
from agent_core.artifact_operations import fixed_size_script
from agent_core.attachment_workspace import fixed_digest_script


class _Token(CancellationToken):
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    @property
    def is_cancelled(self) -> bool:
        return (self.run_dir / "cancel-request.json").exists()


class _DeterministicModel:
    """D3 protocol fixture. It never calls a model transport."""

    def __init__(
        self,
        role_config: D1RoleConfig,
        candidate_values: tuple[str, ...] = ("simulation-only-pending-candidate",),
    ) -> None:
        self.role_config = role_config
        self.candidate_values = candidate_values
        self._candidate_calls = 0

    def invoke(self, request, timeout_seconds, token):
        if token.is_cancelled:
            raise RuntimeError("d7_simulation_cancelled")
        if request.stage == "outer_turn":
            return {
                "turn_target": "verify the authorized attachment digest",
                "completion_rule": {
                    "rule_id": "attachment_copy_sha256",
                    "version": 1,
                    "parameters": {"attachment_index": 0},
                },
                "intents": [{"description": "inspect attachment digest", "intent_type": "file_sha256"}],
            }
        if request.stage == "inner_plan":
            context = request.context
            return {
                "template": "sha256_only" if context.prior_plan_ids else "size_then_sha256",
                "observation_mode": "new_observation",
                "source_proof_id": None,
            }
        if request.stage == "inner_candidate":
            context = request.context
            index = min(self._candidate_calls, len(self.candidate_values) - 1)
            self._candidate_calls += 1
            return {
                "action": {
                    "kind": "candidate_flag",
                    "value": self.candidate_values[index],
                    "execution_id": context.execution_id,
                    "evidence_version": context.plan_result.evidence_sha256,
                    "source_ref": context.plan_binding.artifact_version_id,
                },
                "review_requested": False,
            }
        context = request.context
        if request.stage == "inner_complete":
            action = CompleteIntentAction(context.supporting_fact_ids)
        else:
            conditions = ()
            if request.stage == "inner_sha256":
                conditions = (
                    FileSha256(
                        context.step_index,
                        context.fixed_script.rsplit(" ", 1)[-1],
                        context.turn_binding.expected_sha256,
                    ),
                )
            action = ProbeAction(
                context.fixed_script,
                b"",
                context.fixed_working_directory,
                60,
                conditions,
            )
        return {"action": action_payload(action), "review_requested": False}


class _DeterministicReview:
    def __init__(self, role_config: D1RoleConfig) -> None:
        self.role_config = role_config

    def invoke(self, request):
        return {
            "decision": "acknowledge_turn_end"
            if request.review_scope == "turn_end"
            else "continue_current_intent",
            "rationale": "local D7 simulation fixture",
            "source_ids": list(request.source_versions),
            "constraints": [],
        }


class _LocalDigestSandbox:
    """Per-TaskRun file store implementing only the D3 fixed read operations."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory / "d7-simulated-sandbox"
        self.directory.mkdir(exist_ok=True)
        self.active_path = self.directory / "active.json"

    def _path(self, sandbox_id: str, virtual_path: str) -> Path:
        active = immutable_read(self.active_path) if self.active_path.exists() else None
        if not isinstance(active, dict) or active.get("sandbox_id") != sandbox_id:
            raise ValueError("d7_simulated_sandbox_owner_mismatch")
        return self.directory / (hashlib.sha256(virtual_path.encode("utf-8")).hexdigest() + ".data")

    def create(self, task_run_id: str) -> str:
        sandbox_id = "d7-simulated-" + task_run_id
        immutable_write(self.active_path, {"task_run_id": task_run_id, "sandbox_id": sandbox_id})
        return sandbox_id

    def bind(self, task_run_id: str, sandbox_id: str, *, terminal: bool) -> None:
        if sandbox_id != "d7-simulated-" + task_run_id:
            raise ValueError("d7_simulated_sandbox_owner_mismatch")
        if not terminal:
            active = immutable_read(self.active_path) if self.active_path.exists() else None
            if active != {"task_run_id": task_run_id, "sandbox_id": sandbox_id}:
                immutable_write(self.active_path, {"task_run_id": task_run_id, "sandbox_id": sandbox_id})

    def quiesce(self, sandbox_id: str) -> None:
        return None

    def interrupt(self, sandbox_id: str) -> None:
        return None

    def destroy(self, sandbox_id: str) -> None:
        active = immutable_read(self.active_path) if self.active_path.exists() else None
        if active is not None and active.get("sandbox_id") != sandbox_id:
            raise ValueError("d7_simulated_sandbox_owner_mismatch")
        self.active_path.unlink(missing_ok=True)
        for item in self.directory.glob("*.data"):
            item.unlink()
        # The sandbox adapter is constructed with the TaskRun agent directory;
        # persist the cleanup receipt beside the run declaration so the v3
        # result projector and the controller observe the same ownership root.
        immutable_write(
            self.directory.parent / "cleanup.json",
            {"sandbox_id": sandbox_id, "status": "succeeded", "at": __import__("time").time()},
        )

    def close_admission(self, sandbox_id: str) -> None:
        return None

    async def create_and_readback(self, sandbox_id, path, content, timeout_seconds, token):
        if token.is_cancelled:
            raise RuntimeError("d7_simulation_cancelled")
        target = self._path(sandbox_id, path)
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError("d7_simulated_import_conflict")
        else:
            target.write_bytes(content)
        return self._readback(target)

    async def copy_exclusive_and_readback(self, sandbox_id, source_path, target_path, timeout_seconds, token):
        if token.is_cancelled:
            raise RuntimeError("d7_simulation_cancelled")
        source, target = self._path(sandbox_id, source_path), self._path(sandbox_id, target_path)
        content = source.read_bytes()
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError("d7_simulated_copy_conflict")
        else:
            target.write_bytes(content)
        return self._readback(target)

    async def readback(self, sandbox_id, path, timeout_seconds, token):
        return self._readback(self._path(sandbox_id, path))

    @staticmethod
    def _readback(path: Path) -> AttachmentImportReadback:
        value = path.read_bytes()
        return AttachmentImportReadback(len(value), hashlib.sha256(value).hexdigest())

    async def execute(self, approved, token):
        request = approved.request
        path = request.script.rsplit(" ", 1)[-1]
        stored = self._path(approved.sandbox_id, path)
        if not stored.is_file():
            payload = {"status": "missing"}
        elif request.script == fixed_size_script(path):
            payload = {"status": "present", "byte_size": stored.stat().st_size}
        elif request.script == fixed_digest_script(path):
            payload = {"status": "present", "sha256": hashlib.sha256(stored.read_bytes()).hexdigest()}
        else:
            raise ValueError("d7_simulated_command_rejected")
        return T6CommandResult(
            approved.sandbox_id, 0, json.dumps(payload).encode("utf-8"), b"",
            False, False, False, True, True, True,
        )

    async def hash_file(self, task_run_id, sandbox_id, path, timeout_seconds, token):
        try:
            value = self._path(sandbox_id, path).read_bytes()
        except FileNotFoundError:
            return FileDigestObservation("missing")
        except Exception:  # noqa: BLE001 - unknown sandbox read remains inconclusive
            return FileDigestObservation("unknown")
        return FileDigestObservation("present", hashlib.sha256(value).hexdigest())

    async def stop_intent_processes(self, claim, sandbox_id, timeout_seconds, token):
        from agent_core.artifact_workspace import D3StopReadback
        return D3StopReadback((), claim.process_refs, ())

    async def query_intent_processes(self, claim, sandbox_id, timeout_seconds, token):
        from agent_core.artifact_workspace import D3StopReadback
        return D3StopReadback((), claim.process_refs, ())

    def close(self) -> None:
        return None


def _frozen_config(declaration: dict) -> D3RunConfig:
    from agent_core.operation_accounting import D4BudgetPolicy

    request = declaration["request"]
    dual = request["dual_llm_config"]
    auth = request["authorization"]
    outer = D1RoleConfig.from_dict(dual["outer_role"])
    inner = D1RoleConfig.from_dict(dual["inner_role"])
    return D3RunConfig(
        outer,
        inner,
        D3Authorization(auth["authorization_id"], tuple(auth["attachment_indices"])),
        outer.role_config_ref,
        contract_version=2,
        d4_budget=D4BudgetPolicy.from_dict(declaration["d4_budget_policy"]),
        d5_enabled=True,
        d5_sensitive_requirements=(),
    )


def _d3_request(declaration: dict) -> TaskRequest:
    raw = declaration["request"]["task_request"]
    rewritten = dict(raw)
    target = dict(raw["target"])
    if declaration.get('execution_profile') in {'d8_v15', 'd8_v16', 'd8_v17', 'd8_v18', 'd8_v19', 'd8_v20', 'd8_v21', 'd8_v22'} and target.get('endpoints'):
        # Rewrite attachments to use staged paths
        staged = declaration.get("staged_attachment_paths", [])
        if staged:
            target["attachments"] = [
                {"kind": "local_path", "path": item} for item in staged
            ]
            rewritten["target"] = target
            return TaskRequest.from_dict(rewritten)
        return TaskRequest.from_dict(raw)
    staged = declaration.get("staged_attachment_paths", [])
    target["attachments"] = [
        {"kind": "local_path", "path": item} for item in staged
    ]
    target["endpoints"] = []
    rewritten["target"] = target
    if staged:
        return TaskRequest.from_dict(rewritten)
    goal = raw.get("goal")
    deadline = raw.get("deadline_seconds", 3600)
    if not isinstance(goal, str) or not goal.strip() or "\x00" in goal:
        raise ValueError("invalid_v3_task_goal")
    if type(deadline) is not int or deadline <= 0:
        raise ValueError("invalid_v3_task_deadline")
    return TaskRequest(goal, Target((), ()), deadline)


def _make_application(
    directory: Path,
    declaration: dict,
    *,
    candidate_values: tuple[str, ...] = ("simulation-only-pending-candidate",),
) -> tuple[D3Application, _LocalDigestSandbox]:
    if (
        not isinstance(candidate_values, tuple)
        or not candidate_values
        or any(not isinstance(value, str) or not value for value in candidate_values)
    ):
        raise ValueError("d7_simulation_candidate_values_invalid")
    config = _frozen_config(declaration)
    sandbox = _LocalDigestSandbox(directory)
    return D3Application(
        directory,
        config=config,
        sandbox=sandbox,
        import_sandbox=sandbox,
        workspace_sandbox=sandbox,
        command_sandbox=sandbox,
        outer_model=_DeterministicModel(config.outer_role),
        inner_model=_DeterministicModel(config.inner_role, candidate_values),
        review_model=_DeterministicReview(config.outer_role),
        clock=__import__("time").time,
        refinement_model=_DeterministicModel(config.outer_role),
        sensitive_config={},
    ), sandbox


def _propose_completed_turn_candidate(app: D3Application, directory: Path, token) -> None:
    from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService

    candidate_evidence = D7CandidateEvidenceService(
        directory,
        app.state.task_run.task_run_id,
        trusted_adapter_ids=("d7_candidate_stage",),
    )
    asyncio.run(app.propose_d7_candidate(token, candidate_evidence))


def run_simulated_task(
    directory: Path,
    declaration: dict,
    *,
    operation: str = "start",
    finalize_export=None,
) -> dict:
    """Run or recover one local simulated D3 TaskRun and return its projection."""
    if "execution_profile" in declaration:
        if declaration["execution_profile"] not in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
            raise ValueError("unsupported_execution_profile")
        from agent_adapters.simulation import run_simulated_task as run_d8
        return run_d8(Path(directory), declaration, operation=operation,
                     finalize_export=finalize_export)
    from agent_adapters.web.task_result import snapshot_v3_task_result

    directory = Path(directory)
    task_id = declaration["task_run_id"]
    app, sandbox = _make_application(directory, declaration)
    token = _Token(directory)
    try:
        if operation == "start":
            task_request = _d3_request(declaration)
            app.start(
                task_request,
                directory,
                task_run_id=task_id,
            )
            # Handle 0 attachment case: conclude immediately as inconclusive
            if not declaration.get("staged_attachment_paths"):
                app.controller.conclude(
                    TaskRunStatus.INCONCLUSIVE,
                    str(uuid4()),
                    reason="single_intent_slice_finished",
                )
                app.controller.cleanup_sandbox()
            else:
                asyncio.run(app.run(token))
                if (
                    app.state.task_run.status is TaskRunStatus.RUNNING
                    and app.state.turns
                    and app.state.turns[-1].status is TurnStatus.COMPLETED
                ):
                    _propose_completed_turn_candidate(app, directory, token)
        else:
            if operation == "resume":
                asyncio.run(app.resume(task_id, token))
                if (
                    app.state.task_run.status
                    in {
                        TaskRunStatus.RUNNING,
                        TaskRunStatus.AWAITING_EXTERNAL_FEEDBACK,
                    }
                    and app.state.turns
                    and app.state.turns[-1].status is TurnStatus.COMPLETED
                ):
                    _propose_completed_turn_candidate(app, directory, token)
            elif operation == "cancel":
                app.cancel(task_id)
            elif operation == "cleanup":
                app.resume_cleanup(task_id)
            else:
                raise ValueError("unsupported_v3_operation")
        result = snapshot_v3_task_result(directory, declaration)
        if (
            result.get("cleanup_status") == "confirmed"
            and declaration.get("request_id")
        ):
            from agent_adapters.web.request_staging import D7RequestStager

            stager = D7RequestStager.open_existing(
                # ``directory`` is ``<state-dir>/<task-run-id>``.  Request
                # receipts live under that state-dir, alongside the run
                # directory, so moving up twice selects the Workbench run
                # root and loses the receipt ledger.
                Path(directory).parent, clock=__import__("time").time
            )
            stager.remove_task_run_payload(
                declaration["request_id"], task_id, directory
            )
        return result
    finally:
        app.close()


__all__ = ["run_simulated_task"]
