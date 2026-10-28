"""Current public assembly; original staging, controls, sandbox and candidate services."""

import asyncio
import json
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from agent_adapters.stage_application import StageApplication
from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_adapters.run_files import run_lock
from agent_adapters.web.simulated_application import _LocalDigestSandbox, _Token, _d3_request
from agent_core.runtime import TaskRunStatus, TERMINAL_STATUSES


class StageSimulationModel:
    """Fixed local control exercise, never a real target or model capability claim."""
    def __init__(self, role):
        self.role_config = role

    def invoke(self, envelope, timeout, token):
        from agent_core.output_contracts import D8ModelResponse
        request = envelope.request
        history, turn = request.context.history, request.context.turn
        facts = [i.ref for i in history if i.record_type == "verified_fact"]
        observations = [i for i in history if i.identity == "tool_observation_not_fact"
            and i.intent_id == request.context.session.owner_id]
        if request.stage == "stage_review":
            value = dict(decision="accept", reason="controlled local review")
            if isinstance(request.proposal, tuple):
                value["accepted_road_indices"] = list(range(len(request.proposal)))
        elif request.stage == "stage_intent":
            value = dict(kind="turn_intent", description=request.context.goal,
                completion_criterion="one supported local observation", support_refs=[])
        elif request.stage == "stage_roads":
            value = dict(kind="roads", roads=[dict(description="observe the authorized local input", support_refs=[])])
        elif request.stage == "stage_action":
            if not observations:
                imports = [json.loads(i.content) for i in history if i.identity == "imported_attachment_not_fact"]
                script = "sha256sum " + imports[0]["path"] if imports else "printf simulated"
                value = dict(kind="mcp_call", tool_name="shell.run_bash", arguments=dict(script=script))
            elif not facts:
                text = json.loads(observations[-1].content)["stdout"]
                value = dict(kind="fact_candidate", content="Local observation: " + text[:192],
                    reason="original controlled observation", support_refs=[observations[-1].ref], inferred=False)
            else:
                value = dict(kind="road_resolution", outcome="succeeded", reason="local observation complete", support_refs=facts)
        elif request.stage == "stage_completion":
            value = dict(kind="turn_completion", reason="local criterion checked", support_refs=facts)
        elif request.stage == "stage_continuation":
            if request.context.feedback_refs:
                value = dict(kind="exhausted", reason="the fixed simulator has no new direction", support_refs=list(request.context.feedback_refs))
            else:
                source = next(i for i in reversed(history) if i.identity == "tool_observation_not_fact")
                value = dict(kind="request_final_validation", reason="human must judge the local candidate", support_refs=facts,
                    candidate=json.loads(source.content)["stdout"].strip().split()[0], source_ref=source.ref)
        elif request.stage == "stage_summary":
            value = dict(content="Unverified local simulation index; original evidence is retained.")
        else:
            value = dict(decision="continue_current_intent", reason="controlled read-only supervision", support_refs=[])
        return D8ModelResponse(json.dumps(value))


class SimulatedMCP:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.app = None
    async def start_all(self): pass
    async def stop_all(self): pass
    def list_registered_tools(self):
        return [SimpleNamespace(qualified_name="shell.run_bash")]
    def bind_agent(self, agent_id, *, allowed_tools):
        if agent_id != self.app.task_id or allowed_tools != ["shell.run_bash"]:
            raise ValueError("stage.simulation_scope_invalid")
        return self
    def list_tool_specs(self):
        return [dict(qualified_name="shell.run_bash", description="Fixed controlled local observation", tags=["shell"],
            input_schema=dict(type="object", properties=dict(script=dict(type="string")), required=["script"], additionalProperties=False))]
    async def call_tool(self, name, arguments):
        from MCPs.Mcp_client.client import MCPExecutionResult
        task = self.app.state.task_run
        script = arguments["script"]
        allowed = {"sha256sum /workspace/input/d3-attachment-" + str(i): i
            for i in self.app.config.authorization.attachment_indices}
        if name != "shell.run_bash" or script not in {*allowed, "printf simulated"}:
            raise ValueError("stage.simulated_command_not_supported")
        if script in allowed:
            readback = await self.sandbox.readback(task.sandbox_id, script.split(" ", 1)[1], 60, self.app._token)
            text = readback.sha256
        else:
            text = "simulated-observation"
        return MCPExecutionResult(task.task_run_id, task.sandbox_id, name, "bash", ("-lc", script),
            "completed", 0, text, "", False)


def build_application(directory, declaration, operation):
    from agent_adapters.simulation import parse_request, validate_real_declaration, _LifecycleModel
    _, config = parse_request(declaration["request"])
    resources, network = None, None
    if declaration["mode"] == "real":
        from agent_adapters.deepseek_roles import D8DeepSeekModel, EstimatedCapacity, settings_from_file
        from agent_adapters.docker_profile import connect
        from agent_adapters.application import RecordedSandbox
        validate_real_declaration(directory, declaration)
        resources, sandbox = connect(declaration["request"]["d8_config"]["sandbox_config"], directory,
            wait_for_ready=operation == "start")
        lifecycle = RecordedSandbox(resources, directory)
        capacity = EstimatedCapacity(config.outer_role, config.inner_role)
        outer, inner = _LifecycleModel(config.outer_role), _LifecycleModel(config.inner_role)
        if operation in {"start", "resume"}:
            outer = D8DeepSeekModel(config.outer_role, settings_from_file(config.outer_role, declaration["env_file"]))
            inner = D8DeepSeekModel(config.inner_role, settings_from_file(config.inner_role, declaration["env_file"]))
        if config.web_authorization is not None or config.tcp_authorization is not None:
            from agent_adapters.target_network_cleanup import RecordedTargetNetwork
            from Sandbox.target_network import T6TargetNetwork
            network = RecordedTargetNetwork(T6TargetNetwork(resources._runtime, allow_private_ipv4=True), directory)
        manager = None
    else:
        lifecycle = sandbox = _LocalDigestSandbox(directory)
        capacity = FakeUnicodeCapacity()
        outer, inner = StageSimulationModel(config.outer_role), StageSimulationModel(config.inner_role)
        manager = SimulatedMCP(sandbox)
    try:
        app = StageApplication(directory, config=config, sandbox=lifecycle, workspace_sandbox=sandbox,
            import_sandbox=sandbox, http_sandbox=resources if config.web_authorization is not None else None,
            inner_model=inner, outer_model=outer, clock=time.time, capacity_port=capacity,
            mcp_manager=manager, target_network_adapter=network, goal_validation=declaration["goal_validation"])
        if manager is not None:
            manager.app = app
        elif operation in {"start", "resume"}:
            from agent_adapters.public_web import from_env_file
            app.mcp.public_web = from_env_file(declaration["env_file"], inner.settings)
        return app, resources
    except BaseException:
        if resources is not None:
            resources.close()
        raise


def run_stage_task(directory, declaration, *, operation="start", finalize_export=None,
        competition_request=None, validation_selection=None, validation_checker=None, human_decision=None):
    return asyncio.run(_run_stage_task(directory, declaration, operation=operation, finalize_export=finalize_export,
        competition_request=competition_request, validation_selection=validation_selection,
        validation_checker=validation_checker, human_decision=human_decision))


async def _run_stage_task(directory, declaration, *, operation, finalize_export,
        competition_request, validation_selection, validation_checker, human_decision):
    """Keep MCP startup, calls and teardown on the same loop and owning task."""
    from agent_adapters.web.task_result import snapshot_v3_task_result
    from agent_core import operator_controls
    if operation != "start":
        from agent_core.sqlite_control_store import SqliteControlStore
        from agent_core.stage_storage import StageRecoveryRequired
        with SqliteControlStore.open_readonly(directory / "control.sqlite") as store:
            if store.load_stage_run_config(declaration["task_run_id"]) is None:
                raise StageRecoveryRequired("stage.legacy_task_not_supported")
    app, resources = build_application(directory, declaration, operation)
    task_id, token = declaration["task_run_id"], _Token(directory)
    supplemental = {}
    try:
        if operation == "start":
            from agent_adapters.run_files import immutable_read
            controls_dir = directory.parent.parent / "operator-start-controls"
            controls_dir.mkdir(exist_ok=True)
            with run_lock(controls_dir), run_lock(directory):
                request = _d3_request(declaration)
                binding_file = directory.parent.parent / "competition-binding.json"
                if binding_file.is_file():
                    from agent_adapters.competition_submission import frozen_binding
                    binding = frozen_binding(binding_file, declaration)
                    caps = [value for value in (binding["platform_deadline_at"], binding.get("schedule", {}).get("overall_deadline_at")) if value is not None]
                    if caps:
                        remaining = int(min(caps) - time.time())
                        if remaining < 1:
                            raise ValueError("competition_question_not_active")
                        request = replace(request, deadline_seconds=min(request.deadline_seconds, remaining))
                app.start(request, source_root=declaration["source_root"], task_run_id=task_id)
                if binding_file.is_file() and "schedule" in binding:
                    app.controller.submit_operator_control(app.store, task_id, declaration["request_id"], "configure", {
                        "question_seconds": binding["schedule"]["question_seconds"],
                        "overall_deadline_at": binding["schedule"]["overall_deadline_at"],
                        "platform_deadline_at": binding["platform_deadline_at"],
                        "overall_ceiling_at": binding["schedule"].get("overall_ceiling_at")})
                    operator_controls.enable(app.controller)
                    queued = sorted((immutable_read(path) for path in controls_dir.glob("*.json")),
                        key=lambda item: (item["requested_at"], item["request"]["operation_id"]))
                    for entry in queued:
                        command = entry["request"]
                        if command["request_id"] != declaration["request_id"]:
                            raise ValueError("operator_start_scope_invalid")
                        app.controller.submit_operator_control(app.store, task_id, command["operation_id"], command["action"], command["payload"])
                    pause_file = directory.parent.parent / "operator-pause-request.json"
                    if pause_file.is_file():
                        command = immutable_read(pause_file)
                        if command["request_id"] != declaration["request_id"]:
                            raise ValueError("operator_start_scope_invalid")
                        app.controller.submit_operator_control(app.store, task_id, command["operation_id"], "pause", {})
        else:
            app.bind(task_id)
        if operator_controls.current(app.store, task_id) is not None:
            operator_controls.enable(app.controller)
        app.store.operator_pause_probe = lambda: (directory.parent.parent / "operator-pause-request.json").exists()
        if declaration["mode"] == "real" and operation in {"start", "resume"}:
            app.models.outer.bind_runtime(token, lambda: app.state.task_run.deadline_at)
            app.models.inner.bind_runtime(token, lambda: app.state.task_run.deadline_at)
        if operation in {"start", "resume"}:
            while True:
                try:
                    report = await app.run(token)
                except operator_controls.OperatorPaused:
                    supplemental["dispatch"] = dict(status="paused", reason="operator_paused")
                    break
                except Exception as error:
                    from agent_core.stage_storage import StageRecoveryRequired
                    if not isinstance(error, StageRecoveryRequired) or error.code != "stage.dispatch_stopped":
                        raise
                    with run_lock(directory):
                        app.controller._reload_after_commit()
                        if token.is_cancelled:
                            app.controller.cancel(str(uuid4()))
                        else:
                            app.controller.advance_time(str(uuid4()))
                        if app.state.task_run.status is TaskRunStatus.RUNNING:
                            raise
                    break
                task = app.state.task_run
                if task.status is TaskRunStatus.AWAITING_CONFIRMATION:
                    # The worker remains available; short locked ticks leave manual commands independent.
                    await app.mcp.close()
                    while app.state.task_run.status is TaskRunStatus.AWAITING_CONFIRMATION:
                        try:
                            with run_lock(directory):
                                app.controller._reload_after_commit()
                                if token.is_cancelled:
                                    app.controller.cancel(str(uuid4()))
                                else:
                                    app.controller.advance_time(str(uuid4()))
                        except ValueError as error:
                            if str(error) != "task_run_busy":
                                raise
                        await asyncio.sleep(0.1)
                    continue
                if report.reason in {"stage.exhausted", "stage.root_planning_exhausted", "stage.root_blocked"}:
                    app.controller._reload_after_commit()
                    app.controller.conclude(TaskRunStatus.INCONCLUSIVE, str(uuid4()), reason=report.reason)
                break
        else:
            with run_lock(directory):
                app.controller._reload_after_commit()
                if operation == "human-decision":
                    app.goal.decide(**human_decision)
                elif operation == "cancel":
                    app.controller.cancel(declaration["ids"]["cancel"])
                elif operation == "cleanup":
                    app.controller.cleanup_sandbox()
                elif operation in {"competition-submit", "competition-end"}:
                    from agent_adapters.competition_submission import submit, end_unavailable
                    execute = submit if operation == "competition-submit" else end_unavailable
                    supplemental["competition_submission" if operation == "competition-submit" else "competition_end"] = execute(app, **competition_request)
                elif operation == "validation-check":
                    from agent_core.answer_candidates import check_request
                    request = check_request(app.store, task_id, validation_selection, app.state.latest_checkpoint.sequence + 1)
                    checked = app.controller.run_external_validation_check(request, validation_checker)
                    supplemental["validation_check"] = dict(state=checked.result.verdict, query_id=request.query_id)
                else:
                    raise ValueError("stage.public_operation_invalid")
        if app.state.task_run.status in TERMINAL_STATUSES:
            await app.mcp.close()
            from agent_adapters.web.task_result import _cleanup
            with run_lock(directory):
                app.controller._reload_after_commit()
                if _cleanup(directory, app.state.task_run)[1] != "confirmed":
                    app.controller.cleanup_sandbox()
    finally:
        primary = sys.exception()
        cleanup_errors = []
        try:
            if finalize_export is not None:
                finalize_export()
        except BaseException as error:
            cleanup_errors.append(("export", error))
        try:
            await app.close()
        except BaseException as error:
            cleanup_errors.append(("application", error))
        try:
            if resources is not None:
                resources.close()
        except BaseException as error:
            cleanup_errors.append(("resources", error))
        from agent_adapters.output_evidence import safe_failure_details
        from qinyuan_core import get_logger
        for phase, error in cleanup_errors:
            get_logger("ctrl").error("stage.public_cleanup_failed", phase=phase,
                **safe_failure_details(error, include_chain=True))
            if primary is not None:
                primary.add_note("stage.public_cleanup_failed:" + phase)
        if primary is None and cleanup_errors:
            for phase, _ in cleanup_errors[1:]:
                cleanup_errors[0][1].add_note("stage.public_cleanup_failed:" + phase)
            raise cleanup_errors[0][1]
    result = snapshot_v3_task_result(directory, declaration)
    result.update(supplemental)
    if result.get("cleanup_status") == "confirmed":
        from agent_adapters.web.request_staging import D7RequestStager
        D7RequestStager.open_existing(directory.parent, clock=time.time).remove_task_run_payload(
            declaration["request_id"], task_id, directory)
    return result
