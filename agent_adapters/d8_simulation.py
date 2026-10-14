"""Public D8 core assembly; model mode is separate from the local Sandbox."""

import asyncio
import json
import time
from dataclasses import asdict, replace
from uuid import uuid4

from agent_adapters.handoff_evidence import FakeUnicodeCapacity
from agent_adapters.multi_turn_application import D3Application
from agent_adapters.v7.request_contract import V3SimulationRequest
from agent_adapters.v7.simulated_application import _LocalDigestSandbox, _Token, _d3_request
from agent_core.actions import CompleteIntentAction, ProbeAction, action_payload
from agent_core.dual_llm_contracts import D3Authorization, D3RunConfig
from agent_core.handoff_contracts import D8Capacity, D8CapacityPolicy, D8HandoffConfig
from agent_core.intent_postconditions import FileSha256
from agent_core.operation_accounting import D4BudgetPolicy, D8HandoffBudgetPolicy
from agent_core.output_contracts import D8OutputRequest
from agent_core.process_facts import D5RefinementRequest
from agent_core.review_contracts import D2ReviewRequest
from agent_core.runtime import TaskRunStatus, TERMINAL_STATUSES


def simulation_config():
    """Confirmed explicit preset; character counts are not provider tokens."""
    policy = D8CapacityPolicy("fake-json-v1", "unicode_codepoints", 262144, 8192)
    return {
        "handoff_config": asdict(D8HandoffConfig(
            policy, policy, D8Capacity(initial_history=32768, visible_history=131072)
        )),
        "d5_sensitive_requirements": [],
    }


def real_config():
    from agent_adapters.d8_model import CAPACITY_POLICY, TRANSPORT
    return {"handoff_config": asdict(D8HandoffConfig(
                CAPACITY_POLICY, CAPACITY_POLICY,
                D8Capacity(initial_history=32768, visible_history=131072))),
            "d5_sensitive_requirements": [], "model_transport": dict(TRANSPORT),
            "sandbox_backend": "local_simulated"}


def parse_request(raw: object) -> tuple[V3SimulationRequest, D3RunConfig | None]:
    required = {"protocol_version", "request_id", "task_request", "authorization",
                "dual_llm_config", "execution_profile", "mode", "d8_config"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("invalid_d8_request_fields")
    if not isinstance(raw["execution_profile"], str) or raw["execution_profile"] not in {"d8_v6", "d8_v7"}:
        raise ValueError("unsupported_execution_profile")
    version = 7 if raw["execution_profile"] == "d8_v7" else 6
    if type(raw["protocol_version"]) is not int or raw["protocol_version"] != 3:
        raise ValueError("unsupported_protocol_version")
    if not isinstance(raw["mode"], str) or raw["mode"] not in {"simulated", "real"}:
        raise ValueError("mode_required")
    extra = raw["d8_config"]
    is_real = raw["mode"] == "real"
    expected_fields = {"handoff_config", "d5_sensitive_requirements"}
    if is_real:
        expected_fields |= {"model_transport", "sandbox_backend"}
    if (not isinstance(extra, dict)
            or set(extra) != expected_fields
            or extra["d5_sensitive_requirements"] != []):
        raise ValueError("invalid_d8_configuration")
    if is_real and json.dumps(extra, sort_keys=True) != json.dumps(real_config(), sort_keys=True):
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
    request = replace(V3SimulationRequest.parse(legacy), d4_budget=budget)
    if is_real:
        from agent_adapters.d8_model import validate_role
        validate_role(request.outer_role, version=2 if version == 7 else 1)
        validate_role(request.inner_role, version=2 if version == 7 else 1)
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
    )
    return request, config


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
            return {"kind": "candidates", "turn_target": request.goal,
                    "completion_rule": {"rule_id": "attachment_copy_sha256", "version": 1,
                                        "parameters": {"attachment_index": 0}},
                    "intents": [{"description": "Observe the authorized local copy digest",
                                 "intent_type": "file_sha256", "dependency_refs": []}]}
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
            return {"kind": "candidate", "content": "The observed copy digest matches the frozen import.",
                    "reason": "Only this saved copy version is covered.",
                    "evidence_refs": [observation.proof_id],
                    "assertion": {"rule_id": "attachment_copy_sha256", "rule_version": 1,
                                  "artifact_version_id": observation.artifact_version_id,
                                  "observation_id": observation.observation_id}}
        if stage == "fact_review":
            return {"decision": "accept", "reason": "Supported within the fixed observation scope."}
        context = request.context
        if stage == "inner_plan":
            return {"template": "size_then_sha256", "observation_mode": "new_observation",
                    "source_proof_id": None}
        if stage == "inner_complete":
            return {"action": action_payload(CompleteIntentAction(context.supporting_fact_ids)),
                    "review_requested": False, "next_intent_candidates": []}
        if stage in {"inner_size", "inner_sha256"}:
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


def run_simulated_task(directory, declaration, *, operation="start"):
    from agent_adapters.v7.task_result import snapshot_v3_task_result
    from agent_adapters.v7.request_staging import D7RequestStager

    _, config = parse_request(declaration["request"])
    if config is None:
        raise ValueError("d8_attachment_capability_not_supported")
    if (declaration.get("execution_profile") != declaration["request"]["execution_profile"]
            or declaration.get("contract_version") != config.contract_version
            or declaration.get("mode") != declaration["request"]["mode"]
            or declaration.get("d4_budget_policy") != asdict(config.d4_budget)):
        raise ValueError("d8_frozen_configuration_mismatch")
    sandbox = _LocalDigestSandbox(directory)
    outer, inner = SimulationModel(config.outer_role), SimulationModel(config.inner_role)
    capacity = FakeUnicodeCapacity()
    if declaration["mode"] == "real":
        from agent_adapters.d8_model import D8DeepSeekModel, EstimatedCapacity, settings_from_file
        validate_real_declaration(directory, declaration)
        capacity = EstimatedCapacity(config.outer_role, config.inner_role)
        outer, inner = _LifecycleModel(config.outer_role), _LifecycleModel(config.inner_role)
        if operation in {"start", "resume"}:
            outer = D8DeepSeekModel(config.outer_role, settings_from_file(config.outer_role, declaration["env_file"]))
            inner = D8DeepSeekModel(config.inner_role, settings_from_file(config.inner_role, declaration["env_file"]))
    app = D3Application(
        directory, config=config, sandbox=sandbox, import_sandbox=sandbox,
        workspace_sandbox=sandbox, command_sandbox=sandbox, outer_model=outer,
        inner_model=inner, review_model=outer, refinement_model=outer,
        fact_review_model=outer, handoff_model=outer, capacity_port=capacity,
        sensitive_config={}, clock=time.time,
    )
    task_id, token = declaration["task_run_id"], _Token(directory)
    report = None
    try:
        if operation == "start":
            app.start(_d3_request(declaration), directory, task_run_id=task_id)
            if declaration["mode"] == "real":
                outer.bind_runtime(token, app.state.task_run.deadline_at)
                inner.bind_runtime(token, app.state.task_run.deadline_at)
            if not declaration.get("staged_attachment_paths"):
                app.controller.conclude(TaskRunStatus.INCONCLUSIVE, str(uuid4()),
                                        reason="single_intent_slice_finished")
                app.controller.cleanup_sandbox()
            else:
                report = asyncio.run(app.run(token))
        elif operation == "resume":
            if declaration["mode"] == "real":
                rows = app.store.load_control_state_rows(task_id)
                if rows is None:
                    raise ValueError("d8_runtime_state_unavailable")
                outer.bind_runtime(token, rows.task_run.deadline_at)
                inner.bind_runtime(token, rows.task_run.deadline_at)
            report = asyncio.run(app.resume(task_id, token))
        elif operation == "cancel":
            report = app.cancel(task_id)
        elif operation == "cleanup":
            report = app.resume_cleanup(task_id)
        else:
            raise ValueError("unsupported_v3_operation")
        result = snapshot_v3_task_result(directory, declaration)
        if report is not None and report.recovery_required:
            result["recovery"] = {"required": True, "reason": "operation_or_evidence_unknown"}
            result["recovery_status"] = "required"
            if report.stop_reason.startswith("d8_controller_reload_required:"):
                result["verified_facts"] = []
                result["recovery"]["reason"] = "d8_controller_reload_required"
        if result.get("cleanup_status") == "confirmed":
            D7RequestStager.open_existing(directory.parent, clock=time.time).remove_task_run_payload(
                declaration["request_id"], task_id, directory)
        return result
    finally:
        state = app.controller._persistent_state
        if state is not None and state.task_run.status in TERMINAL_STATUSES:
            app.close()
        else:
            # The confirmed public pause boundary retains the run and Sandbox;
            # close() itself intentionally requires terminal/feedback state.
            app.store.close()


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
            or declaration.get("sandbox_backend") != "local_simulated"
            or immutable_read(path) != real_declaration_binding(declaration)):
        raise ValueError("d8_real_frozen_configuration_mismatch")
