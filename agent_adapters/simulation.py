"""Public D8 core assembly; model mode is separate from the local Sandbox."""

import asyncio
import json
import time
from dataclasses import asdict, replace
from uuid import uuid4

from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_adapters.multi_turn_application import D3Application
from agent_adapters.web.request_contract import V3SimulationRequest
from agent_adapters.web.simulated_application import _LocalDigestSandbox, _Token, _d3_request
from agent_core.actions import CompleteIntentAction, ProbeAction, action_payload
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.handoff_contracts import D8Capacity, D8CapacityPolicy, D8HandoffConfig
from agent_core.intent_postconditions import FileSha256
from agent_core.operation_accounting import D4BudgetPolicy, D8HandoffBudgetPolicy
from agent_core.output_contracts import D8OutputRequest
from agent_core.process_facts import D5RefinementRequest
from agent_core.review_contracts import D2ReviewRequest
from agent_core.runtime import TaskRunStatus, TERMINAL_STATUSES


def simulation_config(*, rolling=False):
    """Confirmed explicit preset; character counts are not provider tokens."""
    policy = D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 262144, 8192)
    return {
        **({"intent_execution_policy": "rolling_v1"} if rolling else {}),
        "handoff_config": asdict(D8HandoffConfig(
            policy, policy, D8Capacity()
        )),
        "d5_sensitive_requirements": [],
    }


def real_config(*, contract_version=6, http=False, http_timeout_seconds=60, rolling=True):
    from agent_adapters.deepseek_roles import CAPACITY_POLICY, TRANSPORT
    result = {"handoff_config": asdict(D8HandoffConfig(
                CAPACITY_POLICY, CAPACITY_POLICY,
                D8Capacity())),
            "d5_sensitive_requirements": [], "model_transport": dict(TRANSPORT),
            "sandbox_backend": "local_simulated"}
    if contract_version in {18, 19, 20, 21, 22}:
        from agent_adapters.docker_profile import sandbox_config
        result.update(sandbox_backend="docker", sandbox_config=sandbox_config())
    if contract_version == 22 and rolling:
        result["intent_execution_policy"] = "rolling_v1"
    if http:
        if contract_version not in {15, 16, 17, 18, 19, 20, 21, 22}:
            raise ValueError('d20_http_requires_contract15_or_higher')
        result['http_profile'] = 'qinyuan.d7.taskrun-forwarder.v1'
        if type(http_timeout_seconds) is not int or not 1 <= http_timeout_seconds <= 300:
            raise ValueError('d20_http_timeout_invalid')
        result['http_timeout_seconds'] = http_timeout_seconds
    return result


def parse_request(raw: object) -> tuple[V3SimulationRequest, D3RunConfig | None]:
    required = {"protocol_version", "request_id", "task_request", "authorization",
                "dual_llm_config", "execution_profile", "mode", "d8_config"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("invalid_d8_request_fields")
    if not isinstance(raw["execution_profile"], str) or raw["execution_profile"] not in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
        raise ValueError("unsupported_execution_profile")
    version = int(raw["execution_profile"].removeprefix("d8_v"))
    endpoints = raw.get('task_request', {}).get('target', {}).get('endpoints', [])
    http = bool(endpoints) and version in {15, 16, 17, 18, 19, 20, 21, 22}
    web_authorization = None
    if http:
        from agent_core.web_contracts import D7WebAuthorization, canonical_web_origin
        if (raw.get('mode') != 'real' or not isinstance(endpoints, list) or len(endpoints) != 1
                or not isinstance(endpoints[0], dict) or set(endpoints[0]) != {'url'}
                or version==20 and raw['task_request']['target'].get('attachments', [])):
            raise ValueError('d20_http_requires_real_one_origin_without_attachments')
        web_authorization = D7WebAuthorization.from_dict(
            raw['authorization']|{'attachment_indices':[]} if version in {21,22} else raw['authorization'])
        if web_authorization.allowed_origin != canonical_web_origin(endpoints[0]['url']):
            raise ValueError('d20_http_endpoint_authorization_mismatch')
    if type(raw["protocol_version"]) is not int or raw["protocol_version"] != 3:
        raise ValueError("unsupported_protocol_version")
    if not isinstance(raw["mode"], str) or raw["mode"] not in {"simulated", "real"}:
        raise ValueError("mode_required")
    extra = raw["d8_config"]
    is_real = raw["mode"] == "real"
    expected_fields = {"handoff_config", "d5_sensitive_requirements"}
    if is_real:
        expected_fields |= {"model_transport", "sandbox_backend"}
        if version in {18, 19, 20, 21, 22}:
            expected_fields.add("sandbox_config")
        if http:
            expected_fields.add('http_profile')
            expected_fields.add('http_timeout_seconds')
    if isinstance(extra, dict) and "intent_execution_policy" in extra:
        if version != 22 or extra["intent_execution_policy"] != "rolling_v1":
            raise ValueError("d22_intent_execution_policy_invalid")
        expected_fields.add("intent_execution_policy")
    if (not isinstance(extra, dict)
            or set(extra) != expected_fields
            or extra["d5_sensitive_requirements"] != []):
        raise ValueError("invalid_d8_configuration")
    if is_real and json.dumps(extra, sort_keys=True) != json.dumps(real_config(contract_version=version, http=http,
            http_timeout_seconds=extra.get('http_timeout_seconds',60), rolling='intent_execution_policy' in extra), sort_keys=True):
        raise ValueError("d8_real_configuration_mismatch")
    handoff = D8HandoffConfig.from_dict(extra["handoff_config"])
    for policy in (handoff.outer_capacity, handoff.inner_capacity):
        if not is_real and (policy.policy_name != "fake-json-v1" or policy.measurement_unit != "unicode_codepoints"):
            raise ValueError("unsupported_d8_simulation_capacity")
    dual = raw["dual_llm_config"]
    if not isinstance(dual, dict) or set(dual) != {"outer_role", "inner_role", "d4_budget"}:
        raise ValueError("invalid_v3_dual_llm_config")
    budget = D8HandoffBudgetPolicy.from_dict(dual["d4_budget"])
    # Reuse the legacy envelope's attachment/authorization checks, without
    # changing its frozen 48-call budget contract or the caller's request.
    legacy = {k: raw[k] for k in required - {"execution_profile", "mode", "d8_config"}}
    legacy["dual_llm_config"] = {**dual, "d4_budget": asdict(D4BudgetPolicy())}
    if http:
        legacy['task_request'] = {**raw['task_request'], 'target': {
            **raw['task_request']['target'], 'endpoints': []}}
        legacy['authorization'] = dict(authorization_id=web_authorization.authorization_id,
            attachment_indices=raw['authorization']['attachment_indices'] if version in {21,22} else [],
            allowed_tool='d3_fixed_readonly', network_allowed=False)
    request = replace(V3SimulationRequest.parse(legacy), d4_budget=budget)
    if http:
        task_request = {**raw['task_request'], 'target': {**raw['task_request']['target'],
            'endpoints': [{'url': web_authorization.allowed_origin}]}}
        request = replace(request, task_request=task_request)
    if is_real:
        from agent_adapters.deepseek_roles import validate_role
        validate_role(request.outer_role, version=version - 5)
        validate_role(request.inner_role, version=version - 5)
    if len(request.attachments) > 2:
        # The public staging envelope accepts up to sixteen attachments;
        # capability rejection and its receipt precede any runnable D3 config.
        return request, None
    config = D3RunConfig(
        request.outer_role, request.inner_role,
        D3Authorization(request.authorization["authorization_id"],
                        tuple(request.authorization["attachment_indices"])),
        request.outer_role.role_config_ref, contract_version=version,
        d4_budget=budget, d5_enabled=True, d5_sensitive_requirements=(),
        d8_enabled=True, handoff_config=handoff,
        web_authorization=web_authorization,
        http_timeout_seconds=extra['http_timeout_seconds'] if http else None,
        intent_execution_policy=extra.get('intent_execution_policy'),
    )
    return (replace(request, authorization=raw['authorization']) if http else request), config


class SimulationModel:
    def __init__(self, role):
        self.role_config = role

    def invoke(self, envelope, timeout_seconds=None, token=None):
        if token is not None and token.is_cancelled:
            raise RuntimeError("d8_simulation_cancelled")
        if not isinstance(envelope, D8OutputRequest):
            raise ValueError("d8_simulation_output_contract_required")
        request = envelope.request
        if isinstance(request, D5RefinementRequest):
            return {"summary_text": "Simulation progress only; the final goal is not verified.", "claims": []}
        if isinstance(request, D2ReviewRequest):
            result = {
                "decision": "acknowledge_turn_end" if request.review_scope == "turn_end"
                else "continue_current_intent",
                "rationale": "Deterministic simulation of the saved observation path.",
                "source_ids": list(request.source_versions), "constraints": [],
            }
            context = json.loads(request.context_summary)
            if "d8_progress" in context:
                result["progress"] = {
                    "classification": "meaningful", "reason": "The bounded observation has recorded progress.",
                    "evidence_refs": [context["d8_progress"]["recent"][-1]["ref"]],
                }
            return result
        stage = request.stage
        if stage == "tree_planning":
            if request.turn_id is not None:
                return {"kind": "exhausted", "reason": "No further simulation branch is proposed.",
                        "evidence_refs": [request.history[0].ref] if request.history else []}
            result = {"kind": "candidates", "turn_target": request.goal,
                    "completion_rule": {"rule_id": "attachment_copy_sha256", "version": 1,
                                        "parameters": {"attachment_index": 0}},
                    "intents": [{"description": "Observe the authorized local copy digest",
                                 "intent_type": "file_sha256", "dependency_refs": []}]}
            if "turn_target" not in envelope.output_contract["schema"]["anyOf"][0]["properties"]:
                from agent_core.planning_assertion import planning_policy
                result.pop("turn_target")
                result["completion_rule"].pop("version")
                result["completion_rule"]["parameters"]["attachment_index"] = planning_policy(request)["attachment_indices"][0]
            return result
        if stage == "handoff_summary":
            return {"turn_body": "Simulation observation progress; inspect labelled sources.",
                    "global_body": "Bounded simulation; the final goal remains unverified.",
                    "ordered_refs": [item.ref for item in request.history], "open_questions": []}
        if stage == "intent_evaluation":
            ref = next(item.ref for item in request.history if item.record_id == request.support_fact_id)
            return {"decision": "succeeded", "reason": "The saved copy observation supports this local goal.",
                    "evidence_refs": [ref]}
        if stage == "fact_proposal":
            observation = request.observation
            result = {"kind": "candidate", "content": "The observed copy digest matches the frozen import.",
                    "reason": "Only this saved copy version is covered.",
                    "evidence_refs": [observation.proof_id],
                    "assertion": {"rule_id": "attachment_copy_sha256", "rule_version": 1,
                                  "artifact_version_id": observation.artifact_version_id,
                                  "observation_id": observation.observation_id}}
            assertion = envelope.output_contract["schema"]["anyOf"][0]["properties"]["assertion"]["anyOf"][1]
            if set(assertion["properties"]) == {"rule_id"}:
                result["assertion"] = {"rule_id": observation.rule_id}
            return result
        if stage == "fact_review":
            return {"decision": "accept", "reason": "Supported within the fixed observation scope."}
        context = request.context
        if stage == "inner_plan":
            rolling = json.loads(getattr(request, "history_context", "{}") or "{}").get("intent_execution")
            if rolling and rolling["facts"]:
                return dict(kind="intent_resolution", outcome="succeeded",
                    reason="The authorized local observation meets the local criteria; overall goal remains unverified.",
                    fact_ids=[i["fact_id"] for i in rolling["facts"]],
                    evidence_refs=[i["ref"] for i in rolling["facts"]], next_intent_candidates=[])
            return {"template": "size_then_sha256", "observation_mode": "new_observation",
                    "source_proof_id": None}
        if stage == "inner_complete":
            return {"action": action_payload(CompleteIntentAction(context.supporting_fact_ids)),
                    "review_requested": False, "next_intent_candidates": []}
        if stage == "inner_candidate" and "anyOf" in envelope.output_contract['schema']['properties']['action']:
            return {"action": {"kind": "no_candidate"}}
        if stage in {"inner_size", "inner_sha256"}:
            if envelope.output_contract["schema"]["anyOf"][0]["properties"]["action"]["properties"]["kind"].get("const") == "fixed_probe":
                from agent_core.fixed_probe import fixed_probe_policy
                policy = fixed_probe_policy(request)
                return {"action": {"kind": "fixed_probe", "operation": policy["operation"],
                                   "timeout_seconds": min(60, policy["max_timeout_seconds"])},
                        "review_requested": False}
            conditions = () if stage == "inner_size" else (FileSha256(
                context.step_index, context.fixed_script.rsplit(" ", 1)[-1],
                context.turn_binding.expected_sha256),)
            action = ProbeAction(context.fixed_script, b"", context.fixed_working_directory, 60, conditions)
            return {"action": action_payload(action), "review_requested": False}
        raise ValueError("unsupported_d8_simulation_stage")


class _LifecycleModel:
    """Cancel/cleanup reconciles evidence and must never generate new output."""
    def __init__(self, role):
        self.role_config = role

    def invoke(self, *args):
        raise ValueError("d8_lifecycle_model_dispatch_forbidden")


def run_simulated_task(directory, declaration, *, operation="start", validation_selection=None, validation_checker=None, finalize_export=None):
    from agent_adapters.web.task_result import snapshot_v3_task_result
    from agent_adapters.web.request_staging import D7RequestStager

    _, config = parse_request(declaration["request"])
    if config is None:
        raise ValueError("d8_attachment_capability_not_supported")
    if (declaration.get("execution_profile") != declaration["request"]["execution_profile"]
            or declaration.get("contract_version") != config.contract_version
            or declaration.get("mode") != declaration["request"]["mode"]
            or declaration.get("d4_budget_policy") != asdict(config.d4_budget)):
        raise ValueError("d8_frozen_configuration_mismatch")
    if config.contract_version in {18, 19, 20, 21, 22} and declaration["mode"] == "real" and operation in {"cancel", "cleanup"}:
        previous = snapshot_v3_task_result(directory, declaration)
        terminal = previous.get("task_run", {}).get("status") in {item.value for item in TERMINAL_STATUSES}
        if terminal and (operation == "cancel" or previous.get("cleanup_status") == "confirmed"):
            return previous
    sandbox = _LocalDigestSandbox(directory) if not (declaration["mode"] == "real" and config.contract_version in {18, 19, 20, 21, 22}) else None
    resources = None
    target_network = None
    outer, inner = SimulationModel(config.outer_role), SimulationModel(config.inner_role)
    capacity = FakeUnicodeCapacity()
    if declaration["mode"] == "real":
        from agent_adapters.deepseek_roles import D8DeepSeekModel, EstimatedCapacity, settings_from_file
        validate_real_declaration(directory, declaration)
        capacity = EstimatedCapacity(config.outer_role, config.inner_role)
        outer, inner = _LifecycleModel(config.outer_role), _LifecycleModel(config.inner_role)
        if operation in {"start", "resume"}:
            outer = D8DeepSeekModel(config.outer_role, settings_from_file(config.outer_role, declaration["env_file"]))
            inner = D8DeepSeekModel(config.inner_role, settings_from_file(config.inner_role, declaration["env_file"]))
        if config.contract_version in {18, 19, 20, 21, 22}:
            from agent_adapters.docker_profile import connect
            from agent_adapters.application import RecordedSandbox
            resources, sandbox = connect(declaration["request"]["d8_config"]["sandbox_config"], directory)
            if config.web_authorization is not None:
                from agent_adapters.target_network_cleanup import RecordedTargetNetwork
                from Sandbox.target_network import T6TargetNetwork
                target_network = RecordedTargetNetwork(T6TargetNetwork(resources._runtime), directory)
    try:
        app = D3Application(
            directory, config=config, sandbox=RecordedSandbox(resources, directory) if resources else sandbox,
            import_sandbox=sandbox, workspace_sandbox=sandbox,
            command_sandbox=sandbox, http_sandbox=resources if config.web_authorization is not None else None, outer_model=outer,
            inner_model=inner, review_model=outer, refinement_model=outer,
            fact_review_model=outer, handoff_model=outer, capacity_port=capacity,
            sensitive_config={}, clock=time.time,
            target_network_adapter=target_network,
        )
    except BaseException:
        if resources is not None:
            resources.close()
        raise
    task_id, token = declaration["task_run_id"], _Token(directory)
    report = None
    check_record = None
    writer_closed = False

    def close_writer():
        nonlocal writer_closed
        if writer_closed:
            return
        writer_closed = True
        try:
            state = app.controller._persistent_state
            if state is not None and state.task_run.status in TERMINAL_STATUSES:
                app.controller.close()
        finally:
            # Paused runs retain the container, not the database/SDK handles.
            app.store.close()

    async def dispatch(resume=False):
        try:
            return await app.resume(task_id, token) if resume else await app.run(token)
        finally:
            if app.mcp is not None:
                await app.mcp.close()

    try:
        if config.contract_version == 22 and declaration["mode"] == "real" and operation in {"start", "resume"}:
            from agent_adapters.mcp_plan_executor import MCPPlanExecutor
            link = MCPPlanExecutor(app)
            if operation == "start":
                link.authorize(task_id)
            if link.enabled(task_id):
                app.mcp = link
        if operation == "start":
            app.start(_d3_request(declaration), directory, task_run_id=task_id)
            if declaration["mode"] == "real":
                outer.bind_runtime(token, app.state.task_run.deadline_at)
                inner.bind_runtime(token, app.state.task_run.deadline_at)
            if not declaration.get("staged_attachment_paths") and config.web_authorization is None:
                app.controller.conclude(TaskRunStatus.INCONCLUSIVE, str(uuid4()),
                                        reason="single_intent_slice_finished")
                app.controller.cleanup_sandbox()
            else:
                report = asyncio.run(dispatch())
        elif operation == "resume":
            if declaration["mode"] == "real":
                rows = app.store.load_control_state_rows(task_id)
                if rows is None:
                    raise ValueError("d8_runtime_state_unavailable")
                outer.bind_runtime(token, rows.task_run.deadline_at)
                inner.bind_runtime(token, rows.task_run.deadline_at)
            report = asyncio.run(dispatch(resume=True))
        elif operation == "cancel":
            report = app.cancel(task_id)
        elif operation == "cleanup":
            report = app.resume_cleanup(task_id)
        elif operation == 'validation-check' and config.contract_version in {19, 20, 21, 22}:
            from agent_adapters.run_files import run_lock
            with run_lock(directory):
                app.controller.attach(task_id)
                app._bound_task_run_id = task_id
                if app.store.load_d3_run_config(task_id) != config:
                    raise ValueError('d3_frozen_run_configuration_changed')
                if token.is_cancelled:
                    raise ValueError('d9_validation_check_cancelled')
                check_record = app.answers.check(validation_selection, validation_checker)
        else:
            raise ValueError("unsupported_v3_operation")
        # Stop the background event exporter before projecting: it still watches the
        # same SQLite while the run is live, and its open handles are what make the
        # in-process projection fail while a fresh read of the same state succeeds.
        if finalize_export is not None:
            finalize_export()
        close_writer()
        result = snapshot_v3_task_result(directory, declaration)
        if check_record is not None:
            result['validation_check'] = {'state': check_record.result.verdict,
                                         'query_id': check_record.request.query_id}
        if report is not None and report.recovery_required:
            reason = result.get("recovery", {}).get("reason")
            # The runtime knows why it stopped; a projection-side placeholder must
            # never hide that. Prefer the concrete stop reason whenever one exists.
            if not reason or reason == "operation_or_evidence_unknown":
                reason = report.stop_reason or "operation_or_evidence_unknown"
            result["recovery"] = {"required": True, "reason": reason}
            result["recovery_status"] = "required"
            if report.stop_reason.startswith("d8_controller_reload_required:"):
                result["verified_facts"] = []
                result["recovery"]["reason"] = "d8_controller_reload_required"
        if result.get("cleanup_status") == "confirmed":
            D7RequestStager.open_existing(directory.parent, clock=time.time).remove_task_run_payload(
                declaration["request_id"], task_id, directory)
        return result
    finally:
        try:
            if app.web is not None:
                app.web.close()
        finally:
            try:
                close_writer()
            finally:
                if resources is not None:
                    resources.close()


def real_declaration_binding(declaration):
    return {"version": 1, "task_run_id": declaration["task_run_id"],
            "request_id": declaration["request_id"], "mode": declaration["mode"],
            "execution_profile": declaration["execution_profile"],
            "contract_version": declaration["contract_version"],
            "sandbox_backend": declaration["sandbox_backend"],
            "env_file": declaration["env_file"],
            "d8_config": declaration["request"]["d8_config"],
            "role_config_refs": declaration["role_config_refs"]}


def validate_real_declaration(directory, declaration):
    from agent_adapters.run_files import immutable_read
    path = directory / "d8-runtime.json"
    if (path.is_symlink() or not path.is_file()
            or declaration.get("sandbox_backend") != ("docker" if declaration.get("contract_version") in {18, 19, 20, 21, 22} else "local_simulated")
            or declaration.get("sandbox_backend") != declaration["request"]["d8_config"]["sandbox_backend"]
            or immutable_read(path) != real_declaration_binding(declaration)):
        raise ValueError("d8_real_frozen_configuration_mismatch")
