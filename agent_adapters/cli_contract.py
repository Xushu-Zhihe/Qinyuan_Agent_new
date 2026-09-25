"""Strict version-one CLI parsing; only the local operator grants authority."""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.contracts import ModelBackend, TaskRequest
from agent_core.file_inspection import FILE_TOOL, validate_inspection_path


def fields(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= set(value)
        or set(value) - set(required) - set(optional)
    ):
        raise ValueError("invalid_request_fields")


@dataclass(frozen=True)
class IntentRunRequest:
    task: TaskRequest
    description: str
    attachment_index: int
    sandbox_path: str

    @classmethod
    def parse(cls, value):
        fields(value, ("protocol_version", "task_request", "intent", "authorization"))
        if type(value["protocol_version"]) is not int or value["protocol_version"] != 1:
            raise ValueError("unsupported_protocol_version")
        task = value["task_request"]
        fields(task, ("goal", "target"), ("deadline_seconds", "model_backend"))
        fields(task["target"], ("attachments",), ("endpoints",))
        if task["target"].get("endpoints"):
            raise ValueError("t5_network_targets_not_supported")
        attachments = task["target"]["attachments"]
        if not isinstance(attachments, list) or len(attachments) != 1:
            raise ValueError("t5_requires_one_local_attachment")
        fields(attachments[0], ("kind", "path"))
        if attachments[0]["kind"] != "local_path":
            raise ValueError("t5_requires_local_attachment")
        parsed = TaskRequest.from_dict(task)
        if parsed.model_backend not in {None, ModelBackend.DEEPSEEK_OPENAI}:
            raise ValueError("model_backend_conflict")
        if parsed.deadline_seconds <= 0:
            raise ValueError("invalid_task_deadline")
        intent = value["intent"]
        fields(intent, ("description", "attachment_index", "sandbox_path"))
        if (
            type(intent["attachment_index"]) is not int
            or intent["attachment_index"] != 0
        ):
            raise ValueError("attachment_index_must_be_zero")
        if (
            not isinstance(intent["description"], str)
            or not intent["description"].strip()
        ):
            raise ValueError("invalid_intent_description")
        path = validate_inspection_path(intent["sandbox_path"])
        validate_authorization(value["authorization"], path)
        return cls(parsed, intent["description"], 0, path)


def validate_authorization(value, path):
    fields(value, ("allowed_tools", "allowed_paths"))
    if value["allowed_tools"] != [FILE_TOOL] or value["allowed_paths"] != [path]:
        raise ValueError("exact_file_authorization_required")
    return value
