"""Contract11 fixed choices and immutable, independently checkable materialization."""

from dataclasses import asdict, dataclass
import math

from agent_core.actions import ProbeAction, action_payload
from agent_core.artifact_operations import fixed_size_script
from agent_core.attachment_workspace import fixed_digest_script
from agent_core.dual_llm_contracts import D3RecoveryRequired, D3TurnBinding, D3PlanBinding
from agent_core.dual_llm_modeling import (
    D3ActionContext, D3SharedArtifactContext, D8SessionExecutionRequest,
    D3ModelEvidence, parse_d3_action,
)
from agent_core.intent_postconditions import FileSha256
from agent_core.turn_modeling import digest


@dataclass(frozen=True, slots=True)
class FixedProbeContext(D3ActionContext):
    fixed_path: str = ""
    max_timeout_seconds: int = 300
    fixed_template_version: int = 1


def fixed_probe_policy(request):
    context = request.context
    if not isinstance(context, FixedProbeContext):
        raise D3RecoveryRequired("s1_fixed_context_required")
    turn, plan = context.turn_binding, context.plan_binding
    size = plan.template == "size_then_sha256" and context.step_index == 0
    expected_step = 0 if size or plan.template == "sha256_only" else 1
    stage = "inner_size" if size else "inner_sha256"
    script = fixed_size_script(context.fixed_path) if size else fixed_digest_script(context.fixed_path)
    if (request.stage != stage or context.step_index != expected_step
            or type(context.step_index) is not int
            or request.task_run_id != turn.task_run_id or plan.task_run_id != turn.task_run_id
            or plan.turn_id != turn.turn_id or request.resource_ref != f"{plan.plan_id}:{stage}"
            or context.fixed_script != script
            or context.fixed_working_directory != context.fixed_path.rsplit("/", 1)[0]
            or type(context.fixed_template_version) is not int or context.fixed_template_version != 1
            or type(context.max_timeout_seconds) is not int or not 1 <= context.max_timeout_seconds <= 300):
        raise D3RecoveryRequired("s1_fixed_context_binding_changed")
    return {"operation": "observe_size" if size else "observe_sha256",
            "template_version": 1, "max_timeout_seconds": context.max_timeout_seconds}


def fixed_probe_schema(request):
    from agent_core.output_contracts import obj, tagged, B
    policy = fixed_probe_policy(request)
    return obj({"action": tagged("fixed_probe", {
        "operation": {"type": "string", "enum": [policy["operation"]]},
        "timeout_seconds": {"type": "integer", "minimum": 1,
                            "maximum": policy["max_timeout_seconds"]}}), "review_requested": B})


def materialize(request, wire):
    from agent_core.output_contracts import decode_response
    decoded, issues, terminal, _ = decode_response(wire, fixed_probe_schema(request))
    if issues or terminal:
        raise D3RecoveryRequired("s1_fixed_wire_invalid")
    context = request.context
    conditions = (() if request.stage == "inner_size" else
                  (FileSha256(context.step_index, context.fixed_path, context.turn_binding.expected_sha256),))
    action = ProbeAction(context.fixed_script, b"", context.fixed_working_directory,
                        decoded["action"]["timeout_seconds"], conditions)
    return {"action": action_payload(action), "review_requested": decoded["review_requested"]}


class FixedProbeBindings:
    def __init__(self, app):
        self.app = app
        self.output = app.output_boundary.evidence

    def _request(self, identity):
        snapshot = self.app.sessions.evidence.read("request", identity)
        if not isinstance(snapshot, dict) or set(snapshot) - {"context_sha256"} != {"turn_id", "intent_id", "request"}:
            raise D3RecoveryRequired("s1_fixed_request_missing")
        try:
            value = dict(snapshot["request"])
            context = dict(value["context"])
            context["turn_binding"] = D3TurnBinding(**context["turn_binding"])
            from agent_core.http_contracts import plan_binding_from_dict
            context["plan_binding"] = plan_binding_from_dict(context["plan_binding"])
            context["supporting_fact_ids"] = tuple(context["supporting_fact_ids"])
            context["shared_artifacts"] = tuple(D3SharedArtifactContext(**i) for i in context["shared_artifacts"])
            value["context"] = FixedProbeContext(**context)
            value["process_facts"] = tuple(value["process_facts"])
            request = D8SessionExecutionRequest(**value)
        except (TypeError, ValueError, KeyError) as exc:
            raise D3RecoveryRequired("s1_fixed_request_invalid") from exc
        policy = fixed_probe_policy(request)
        if (request.model_request_id != identity
                or snapshot["turn_id"] != request.context.turn_binding.turn_id
                or snapshot["intent_id"] != request.context.plan_binding.intent_id):
            raise D3RecoveryRequired("s1_fixed_request_owner_changed")
        return request, policy

    def _base(self, request, finished_at):
        contract = self.output.read("contract", request.model_request_id)
        from agent_core.output_contracts import output_schema, matches_output_contract
        audit = {"stage": request.stage, "resource_ref": request.resource_ref,
                 "base_checkpoint_sequence": request.base_checkpoint_sequence,
                 "context_sha256": request.context_sha256, "role_config_ref": request.role_config_ref,
                 "correction_of_model_request_id": request.correction_of_model_request_id}
        root = request.correction_of_model_request_id or request.model_request_id
        if (not isinstance(contract, dict) or contract.get("contract_version") != self.app.config.contract_version
                or contract.get("model_request_id") != request.model_request_id
                or contract.get("task_run_id") != request.task_run_id
                or contract.get("role_config_ref") != self.app.config.inner_role.role_config_ref
                or contract.get("root_request_id") != root or contract.get("model_audit") != audit
                or contract.get("source_request_sha256") != digest(asdict(request))
                or not matches_output_contract(contract.get("output_contract"), request.stage, output_schema(request, contract_version=self.app.config.contract_version)[1], self.app.config.contract_version)
                or type(finished_at) not in {int, float} or not math.isfinite(finished_at)):
            raise D3RecoveryRequired("s1_fixed_contract_binding_changed")
        if root != request.model_request_id:
            original, _ = self._request(root)
            if (original.correction_of_model_request_id is not None
                    or original.task_run_id != request.task_run_id or original.stage != request.stage
                    or original.resource_ref != request.resource_ref):
                raise D3RecoveryRequired("s1_fixed_root_binding_changed")
        return {"binding_version": 1, "contract_version": self.app.config.contract_version, "stage": request.stage,
                "task_run_id": request.task_run_id, "model_request_id": request.model_request_id,
                "root_request_id": root, "role_config_ref": request.role_config_ref,
                "base_checkpoint_sequence": request.base_checkpoint_sequence,
                "source_request_sha256": digest(asdict(request)), "context_sha256": request.context_sha256,
                "contract_sha256": digest(contract), "policy": fixed_probe_policy(request),
                "finished_at": finished_at}

    def normalize(self, safe_request, wire):
        # Model workers read frozen files only; never query SQLite here.
        identity = safe_request.model_request_id
        request, _ = self._request(identity)
        contract = self.output.read("contract", identity)
        if not isinstance(contract, dict) or contract["safe_request_sha256"] != digest(asdict(safe_request)):
            raise D3RecoveryRequired("s1_fixed_safe_request_changed")
        canonical = materialize(request, wire)
        record = {**self._base(request, self.app.clock()), "wire_response": wire,
                  "canonical_response": canonical, "canonical_sha256": digest(canonical)}
        self.output.save("fixed-probe-binding", identity, record)
        return wire

    def read(self, identity):
        record = self.output.read("fixed-probe-binding", identity)
        if not isinstance(record, dict):
            raise D3RecoveryRequired("s1_fixed_binding_missing")
        if (self.output.read("diagnostic", identity) is not None
                or self.output.read("business-diagnostic", identity) is not None):
            raise D3RecoveryRequired("s1_fixed_diagnostic_conflict")
        request, _ = self._request(identity)
        canonical = materialize(request, record.get("wire_response"))
        expected = {**self._base(request, record.get("finished_at")),
                    "wire_response": record["wire_response"], "canonical_response": canonical,
                    "canonical_sha256": digest(canonical)}
        if record != expected:
            raise D3RecoveryRequired("s1_fixed_binding_changed")
        return request, record

    def proposal(self, identity, *, request=None, wire=None, check_operation=True):
        original, record = self.read(identity)
        if request is not None and asdict(request) != asdict(original):
            raise D3RecoveryRequired("s1_fixed_request_changed")
        if wire is not None and wire != record["wire_response"]:
            raise D3RecoveryRequired("s1_fixed_wire_binding_changed")
        if check_operation:
            self.known(identity)
        return parse_d3_action(record["canonical_response"], original.stage)

    def known(self, identity, *, repair=False, readonly=False):
        request, record = self.read(identity)
        operation = self.app.store.load_d3_operation(identity)
        if (operation is None or operation.start.task_run_id != request.task_run_id
                or operation.start.category != "inner_model" or operation.start.role != request.role
                or operation.start.role_config_ref != request.role_config_ref
                or operation.start.resource_ref != request.resource_ref
                or operation.start.context_sha256 != request.context_sha256
                or operation.start.base_checkpoint_sequence != request.base_checkpoint_sequence
                or operation.start.started_at > record["finished_at"]):
            raise D3RecoveryRequired("s1_fixed_operation_binding_changed")
        if request.correction_of_model_request_id is not None:
            root, _ = self._request(request.correction_of_model_request_id)
            started = self.app.store.load_d3_operation(root.model_request_id)
            contract = self.output.read("contract", root.model_request_id)
            if (started is None or started.start.task_run_id != root.task_run_id
                    or started.start.role != root.role or started.start.role_config_ref != root.role_config_ref
                    or started.start.category != "inner_model" or started.start.resource_ref != root.resource_ref
                    or started.start.context_sha256 != root.context_sha256
                    or started.start.base_checkpoint_sequence != root.base_checkpoint_sequence
                    or not isinstance(contract, dict)
                    or contract.get("source_request_sha256") != digest(asdict(root))):
                raise D3RecoveryRequired("s1_fixed_root_operation_changed")
        native = self.app.model_evidence.read(identity)
        expected = D3ModelEvidence(identity, request.task_run_id, request.stage, request.role_config_ref,
            request.resource_ref, request.base_checkpoint_sequence, request.context_sha256,
            request.correction_of_model_request_id, "accepted", record["wire_response"], None,
            record["finished_at"])
        if native is not None and native != expected:
            if (native.outcome != "stale" or native.raw != expected.raw
                    or {**asdict(native), "outcome": "accepted", "reason": None} != asdict(expected)):
                raise D3RecoveryRequired("s1_fixed_native_result_changed")
            expected = native
        if operation.completion is not None and (
                operation.completion.evidence_ref != identity
                or operation.completion.evidence_sha256 != digest(asdict(expected))
                or operation.completion.finished_at != expected.finished_at
                or operation.completion.result_kind != ("completed" if expected.outcome == "accepted" else "rejected")):
            raise D3RecoveryRequired("s1_fixed_completion_changed")
        if native is None and repair:
            self.app.model_evidence.save(expected)
        elif native is None and not repair and not readonly:
            raise D3RecoveryRequired("s1_fixed_native_result_missing")
        return expected

    def check_task(self, task, *, repair=False):
        needs_repair = False
        for operation in self.app.store.list_d3_operations(task):
            identity = operation.start.operation_id
            contract = self.output.read("contract", identity)
            native = self.app.model_evidence.read(identity)
            record = self.output.read("fixed-probe-binding", identity)
            fixed = (record is not None or native is not None and native.stage in {"inner_size", "inner_sha256"}
                     or operation.start.resource_ref.rsplit(":", 1)[-1] in {"inner_size", "inner_sha256"})
            if not fixed:
                continue
            if not isinstance(contract, dict) or contract.get("stage") not in {"inner_size", "inner_sha256"}:
                raise D3RecoveryRequired("s1_fixed_contract_missing")
            if record is not None:
                self.known(identity, repair=repair, readonly=not repair)
                needs_repair |= native is None
            elif native is not None and native.outcome == "accepted" and not (
                    isinstance(native.raw, dict) and native.raw.get("kind") == "conclusion_change"):
                raise D3RecoveryRequired("s1_fixed_binding_missing")
        return needs_repair

    def reconcile(self):
        self.check_task(self.app.state.task_run.task_run_id, repair=True)
