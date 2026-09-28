"""Version-two whole-task request and deterministic simulation fixture."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from agent_adapters.cli_contract import fields
from agent_core.actions import parse_t6_action
from agent_core.command_contracts import MAX_OUTPUT_BYTES, validate_working_directory
from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.modeling import IntentCandidate
from Sandbox.target_address import TargetAddress


@dataclass(frozen=True, slots=True)
class TaskRunRequest:
    task: TaskRequest

    @classmethod
    def parse(cls, value: object) -> TaskRunRequest:
        fields(value, ("protocol_version", "task_request", "authorization"))
        if not isinstance(value, dict):
            raise ValueError("invalid_request_fields")
        if type(value["protocol_version"]) is not int or value["protocol_version"] != 2:
            raise ValueError("unsupported_protocol_version")
        task_value = value["task_request"]
        fields(task_value, ("goal", "target"), ("deadline_seconds", "model_backend"))
        fields(task_value["target"], ("attachments",), ("endpoints",))
        task = TaskRequest.from_dict(task_value)
        if task.model_backend not in {None, ModelBackend.DEEPSEEK_OPENAI}:
            raise ValueError("model_backend_conflict")
        endpoints = task.target.endpoints
        if len(endpoints) > 1:
            raise ValueError("t6_requires_one_target_endpoint")
        if endpoints:
            TargetAddress.from_url(endpoints[0].url)
        if len(task.target.attachments) > 16 or any(
            item.kind != "local_path" for item in task.target.attachments
        ):
            raise ValueError("t6_requires_local_attachments")
        fields(value["authorization"], ("allowed_tools", "allowed_target_urls"))
        if (value["authorization"]["allowed_tools"] != ["sandbox.command"]
                or value["authorization"]["allowed_target_urls"]
                != ([endpoints[0].url] if endpoints else [])):
            raise ValueError("exact_t6_authorization_required")
        return cls(task)


def _decoded(value: object, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise ValueError("invalid_simulated_base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_simulated_base64") from exc
    if len(decoded) > limit:
        raise ValueError("simulated_bytes_too_large")
    return decoded


def validate_task_fixture(value: object) -> dict:
    fields(value, ("turns", "actions", "commands"))
    if not isinstance(value, dict):
        raise ValueError("invalid_request_fields")
    turns, actions, commands = (value[name] for name in ("turns", "actions", "commands"))
    if not isinstance(turns, list) or not turns:
        raise ValueError("invalid_simulated_turns")
    if not isinstance(actions, list) or not isinstance(commands, list):
        raise ValueError("invalid_simulated_sequences")
    for turn in turns:
        fields(turn, ("candidates",))
        candidates = turn["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("invalid_simulated_candidates")
        for candidate in candidates:
            fields(candidate, ("description",), ("intent_type",))
            IntentCandidate(candidate["description"], candidate.get("intent_type"))
    for action in actions:
        parse_t6_action(action)
    for command in commands:
        fields(command, ("exit_code", "stdout_base64", "stderr_base64"),
               ("confirmed_timeout", "work_files"))
        timeout = command.get("confirmed_timeout", False)
        if type(timeout) is not bool or (
            timeout and command["exit_code"] is not None
        ) or (not timeout and type(command["exit_code"]) is not int):
            raise ValueError("invalid_simulated_command_exit")
        _decoded(command["stdout_base64"], MAX_OUTPUT_BYTES)
        _decoded(command["stderr_base64"], MAX_OUTPUT_BYTES)
        files = command.get("work_files", {})
        if not isinstance(files, dict):
            raise ValueError("invalid_simulated_work_files")
        total = 0
        for path, encoded in files.items():
            validate_working_directory(path)
            if path == "/workspace/work":
                raise ValueError("invalid_simulated_work_file_path")
            total += len(_decoded(encoded, 64 * 1024 * 1024))
            if total > 64 * 1024 * 1024:
                raise ValueError("simulated_work_files_too_large")
    return value
