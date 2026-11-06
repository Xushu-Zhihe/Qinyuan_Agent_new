"""Finite MCP sequences; references copy settled data without evaluating code."""

from copy import deepcopy
from dataclasses import dataclass
import re
import json

from agent_core.actions import MCPCall
from agent_core.dual_llm_contracts import ExecutionRecoveryRequired


@dataclass(frozen=True, slots=True)
class StageToolSequence:
    steps: tuple[dict, ...]


def step_id(task_id, parent, index):
    from agent_core.turn_modeling import stable_id
    return stable_id(task_id, "mcp/" + parent + "/step/" + str(index))


def pointer(value, path):
    if path == "":
        return value
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise ValueError("stage.sequence_reference_invalid")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise ValueError("array_index_invalid")
                value = value[int(part)]
            else:
                value = value[part]
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError("stage.sequence_reference_missing") from None
    return value


def validate_sequence(steps, tools):
    from jsonschema import Draft202012Validator
    from agent_core.stage_planning import _closed_schema
    catalog = {t["qualified_name"]: _closed_schema(t["input_schema"]) for t in tools}
    for index, step in enumerate(steps):
        targets = []
        for binding in step.get("bindings", ()):
            target = binding["target"]
            if (binding["step"] >= index or not target.startswith("/")
                    or re.search(r"~(?![01])", target)
                    or binding["path"] and not binding["path"].startswith("/")):
                raise ValueError("stage.sequence_reference_invalid")
            if any(target == old or target.startswith(old + "/") or old.startswith(target + "/") for old in targets):
                raise ValueError("stage.sequence_binding_conflict")
            targets.append(target)
        condition = step.get("condition")
        if condition and condition["step"] >= index:
            raise ValueError("stage.sequence_reference_invalid")
        # Validate literal portions now; bound values receive the full original
        # tool validation only after resolution, immediately before dispatch.
        for error in Draft202012Validator(catalog[step["tool_name"]]).iter_errors(step["arguments"]):
            path = "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in error.absolute_path)
            covered = any(path == t or path.startswith(t + "/") for t in targets)
            if error.validator == "required":
                missing = [k for k in error.validator_value if k not in error.instance]
                prefix = "" if not error.absolute_path else path
                covered = all(prefix + "/" + k.replace("~", "~0").replace("/", "~1") in targets for k in missing)
            if not covered:
                raise ValueError("stage.sequence_arguments_invalid")


def settled_payload(evidence, task_id, parent, index):
    from agent_core.turn_modeling import digest
    identity = step_id(task_id, parent, index)
    operation = evidence.store.load_d4_operation(identity)
    if operation is None or operation.completion is None:
        raise ExecutionRecoveryRequired("stage.sequence_result_unknown")
    claim = evidence.read("mcp-call", identity)
    result = evidence.read("mcp-result", identity)
    if (not claim or not result or claim.get("model_request_id") != parent
            or claim.get("sequence_step") != index or claim.get("task_run_id") != task_id
            or operation.start.context_sha256 != digest(claim)
            or result["request_sha256"] != digest(claim)
            or operation.completion.evidence_sha256 != digest(result)
            or operation.completion.evidence_ref != "mcp-result/" + identity):
        raise ExecutionRecoveryRequired("stage.sequence_result_changed")
    return result["payload"]


def resolve_step(evidence, task_id, parent, sequence, index):
    step = sequence.steps[index]
    for prior in range(index):
        payload = settled_payload(evidence, task_id, parent, prior)
        if payload["status"] not in {"completed", "failed", "rejected"}:
            raise ExecutionRecoveryRequired("stage.sequence_result_unknown")
    if index and (payload["status"] != "completed" or payload.get("exit_code") not in {None, 0}
            or payload.get("timed_out")) and not step.get("condition"):
        return None
    def source(reference):
        payload = settled_payload(evidence, task_id, parent, reference["step"])
        if reference.get("json_stdout"):
            if payload.get("stdout_truncated") or payload.get("exit_code") not in {None, 0} or payload["status"] != "completed":
                raise ValueError("stage.sequence_json_unavailable")
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate_json_key")
                    result[key] = value
                return result
            def reject_constant(value):
                raise ValueError("nonfinite_json")
            try:
                payload = json.loads(payload["stdout"], object_pairs_hook=pairs, parse_constant=reject_constant)
            except (ValueError, KeyError, TypeError):
                raise ValueError("stage.sequence_json_unavailable") from None
        return pointer(payload, reference["path"])
    condition = step.get("condition")
    if condition:
        actual, expected = source(condition), condition["equals"]
        if isinstance(actual, bool) != isinstance(expected, bool) or actual != expected:
            return None
    arguments = deepcopy(step["arguments"])
    for binding in step.get("bindings", ()):
        container_path, _, key = binding["target"].rpartition("/")
        container = pointer(arguments, container_path)
        key = key.replace("~1", "/").replace("~0", "~")
        value = deepcopy(source(binding))
        try:
            if isinstance(container, list):
                container[int(key)] = value
            else:
                container[key] = value
        except (IndexError, TypeError, ValueError):
            raise ValueError("stage.sequence_reference_invalid") from None
    return MCPCall(step["tool_name"], arguments)
