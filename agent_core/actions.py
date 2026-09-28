"""Strict model proposals for one incremental Intent decision."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TypeAlias

from agent_core.command_contracts import (
    MAX_COMMAND_SECONDS,
    MAX_SCRIPT_BYTES,
    MAX_STDIN_BYTES,
    validate_working_directory,
)
from agent_core.intent_postconditions import FileSha256, OutputContains, Postcondition


class T6ActionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProbeAction:
    script: str
    stdin: bytes
    working_directory: str
    timeout_seconds: int
    conditions: tuple[Postcondition, ...]


@dataclass(frozen=True, slots=True)
class CompleteIntentAction:
    fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AbandonIntentAction:
    execution_id: str
    short_reason: str


@dataclass(frozen=True, slots=True)
class CandidateFlagAction:
    value: str
    execution_id: str


T6Action: TypeAlias = (
    ProbeAction | CompleteIntentAction | AbandonIntentAction | CandidateFlagAction
)


def action_payload(action: T6Action) -> dict[str, object]:
    if isinstance(action, ProbeAction):
        conditions: list[dict[str, object]] = []
        for condition in action.conditions:
            conditions.append({
                "kind": "output_contains" if isinstance(condition, OutputContains) else "file_sha256",
                **asdict(condition),
            })
        return {
            "kind": "probe",
            "script": action.script,
            "stdin_base64": base64.b64encode(action.stdin).decode("ascii"),
            "working_directory": action.working_directory,
            "timeout_seconds": action.timeout_seconds,
            "conditions": conditions,
        }
    if isinstance(action, CompleteIntentAction):
        return {"kind": "complete_intent", "fact_ids": list(action.fact_ids)}
    if isinstance(action, AbandonIntentAction):
        return {"kind": "abandon_intent", "execution_id": action.execution_id,
                "short_reason": action.short_reason}
    if isinstance(action, CandidateFlagAction):
        return {"kind": "candidate_flag", "value": action.value,
                "execution_id": action.execution_id}
    raise TypeError("unsupported_t6_action")


def _fields(
    value: object, required: set[str], optional: set[str] | None = None
) -> Mapping:
    if not isinstance(value, Mapping):
        raise T6ActionError("action_must_be_object")
    keys = set(value)
    if (
        not all(isinstance(key, str) for key in keys)
        or not required <= keys
        or keys - required - (optional or set())
    ):
        raise T6ActionError("invalid_action_fields")
    return value


def _text(value: object, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise T6ActionError(f"invalid_{field}")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise T6ActionError(f"{field}_too_large")
    return value


def _condition(value: object) -> Postcondition:
    item = _fields(
        value, {"kind", "step_index"}, {"stream", "literal", "path", "expected_sha256"}
    )
    if item["step_index"] != 0 or type(item["step_index"]) is not int:
        raise T6ActionError("invalid_condition_step")
    try:
        if item["kind"] == "output_contains":
            _fields(item, {"kind", "step_index", "stream", "literal"})
            return OutputContains(0, item["stream"], item["literal"])
        if item["kind"] == "file_sha256":
            _fields(item, {"kind", "step_index", "path", "expected_sha256"})
            return FileSha256(0, item["path"], item["expected_sha256"])
    except (TypeError, ValueError) as exc:
        raise T6ActionError("invalid_condition") from exc
    raise T6ActionError("invalid_condition_kind")


def parse_t6_action(raw: object) -> T6Action:
    item = _fields(
        raw,
        {"kind"},
        {
            "script",
            "stdin_base64",
            "working_directory",
            "timeout_seconds",
            "conditions",
            "fact_ids",
            "execution_id",
            "short_reason",
            "value",
        },
    )
    kind = item["kind"]
    try:
        if kind == "probe":
            _fields(
                item,
                {
                    "kind",
                    "script",
                    "working_directory",
                    "timeout_seconds",
                    "conditions",
                },
                {"stdin_base64"},
            )
            script = _text(item["script"], "script", MAX_SCRIPT_BYTES)
            encoded = item.get("stdin_base64", "")
            if not isinstance(encoded, str) or len(encoded) > 4 * (
                (MAX_STDIN_BYTES + 2) // 3
            ):
                raise T6ActionError("invalid_stdin_base64")
            try:
                stdin = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise T6ActionError("invalid_stdin_base64") from exc
            if len(stdin) > MAX_STDIN_BYTES:
                raise T6ActionError("stdin_too_large")
            directory = item["working_directory"]
            validate_working_directory(directory)
            timeout = item["timeout_seconds"]
            if type(timeout) is not int or not 1 <= timeout <= MAX_COMMAND_SECONDS:
                raise T6ActionError("invalid_timeout")
            raw_conditions = item["conditions"]
            if not isinstance(raw_conditions, list) or len(raw_conditions) > 32:
                raise T6ActionError("invalid_conditions")
            return ProbeAction(
                script,
                stdin,
                directory,
                timeout,
                tuple(_condition(c) for c in raw_conditions),
            )
        if kind == "complete_intent":
            _fields(item, {"kind", "fact_ids"})
            ids = item["fact_ids"]
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(value, str) or not value.strip() for value in ids)
                or len(ids) != len(set(ids))
            ):
                raise T6ActionError("invalid_fact_ids")
            return CompleteIntentAction(tuple(ids))
        if kind == "abandon_intent":
            _fields(item, {"kind", "execution_id", "short_reason"})
            reason = item["short_reason"]
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 256
                or "\x00" in reason
            ):
                raise T6ActionError("invalid_short_reason")
            return AbandonIntentAction(
                _text(item["execution_id"], "execution_id", 128),
                reason,
            )
        if kind == "candidate_flag":
            _fields(item, {"kind", "value", "execution_id"})
            return CandidateFlagAction(
                _text(item["value"], "candidate_value", 4096),
                _text(item["execution_id"], "execution_id", 128),
            )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, T6ActionError):
            raise
        raise T6ActionError("invalid_action_value") from exc
    raise T6ActionError("unsupported_action_kind")
