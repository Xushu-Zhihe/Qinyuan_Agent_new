"""Strict model proposals for one incremental Intent decision."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TypeAlias

from agent_core.command_contracts import (
    MAX_COMMAND_SECONDS,
    MAX_SCRIPT_BYTES,
    MAX_STDIN_BYTES,
    validate_working_directory,
)
from agent_core.intent_postconditions import FileSha256, OutputContains, Postcondition


class T6ActionValidationCode(StrEnum):
    """Bounded diagnostics for a rejected T6 model response."""

    PROVIDER_JSON_INVALID = "provider_json_invalid"
    PROVIDER_ENVELOPE_INVALID = "provider_envelope_invalid"
    PROVIDER_CHOICES_INVALID = "provider_choices_invalid"
    PROVIDER_CONTENT_INVALID = "provider_content_invalid"
    ACTION_ENVELOPE_INVALID = "action_envelope_invalid"
    ACTION_FIELDS_INVALID = "action_fields_invalid"
    ACTION_FIELD_MISSING = "action_field_missing"
    ACTION_FIELD_UNEXPECTED = "action_field_unexpected"
    ACTION_KIND_INVALID = "action_kind_invalid"
    ACTION_VALUE_INVALID = "action_value_invalid"
    RETRY_CONDITIONS_INVALID = "retry_conditions_invalid"


@dataclass(frozen=True, slots=True)
class T6ActionValidationFailure:
    """A diagnostic without retaining provider or model-controlled content."""

    code: T6ActionValidationCode


class T6ActionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        validation_code: T6ActionValidationCode = T6ActionValidationCode.ACTION_VALUE_INVALID,
    ) -> None:
        super().__init__(message)
        self.validation_code = validation_code


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


@dataclass(frozen=True, slots=True)
class D2ActionEnvelope:
    """The D2-only wrapper around the two actions allowed in the D2 slice.

    D1 keeps accepting the unwrapped action object through :func:`parse_t6_action`.
    This type exists at the D2 model boundary so the review request can be recorded
    together with the action candidate without changing that legacy contract.
    """

    action: ProbeAction | CompleteIntentAction
    review_requested: bool


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
        raise T6ActionError(
            "action_must_be_object",
            validation_code=T6ActionValidationCode.ACTION_FIELDS_INVALID,
        )
    keys = set(value)
    if not all(isinstance(key, str) for key in keys):
        raise T6ActionError(
            "invalid_action_fields",
            validation_code=T6ActionValidationCode.ACTION_FIELDS_INVALID,
        )
    if not required <= keys:
        raise T6ActionError(
            "invalid_action_fields",
            validation_code=T6ActionValidationCode.ACTION_FIELD_MISSING,
        )
    if keys - required - (optional or set()):
        raise T6ActionError(
            "invalid_action_fields",
            validation_code=T6ActionValidationCode.ACTION_FIELD_UNEXPECTED,
        )
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
    except T6ActionError:
        raise
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
    raise T6ActionError(
        "unsupported_action_kind",
        validation_code=T6ActionValidationCode.ACTION_KIND_INVALID,
    )


def parse_d2_action_envelope(raw: object) -> D2ActionEnvelope:
    """Parse the strict D2 action envelope without executing the action.

    The nested action uses the established D1/T6 parser so its field and value
    limits remain identical.  D2 currently carries only ``probe`` and
    ``complete_intent``; candidate flags and abandonment remain D1-only actions.
    """

    if not isinstance(raw, Mapping):
        raise T6ActionError(
            "d2_action_envelope_must_be_object",
            validation_code=T6ActionValidationCode.ACTION_ENVELOPE_INVALID,
        )
    keys = set(raw)
    if any(not isinstance(key, str) for key in keys):
        raise T6ActionError(
            "invalid_d2_action_envelope_fields",
            validation_code=T6ActionValidationCode.ACTION_FIELDS_INVALID,
        )
    if "action" not in keys or "review_requested" not in keys:
        raise T6ActionError(
            "invalid_d2_action_envelope_fields",
            validation_code=T6ActionValidationCode.ACTION_FIELD_MISSING,
        )
    if keys - {"action", "review_requested"}:
        raise T6ActionError(
            "invalid_d2_action_envelope_fields",
            validation_code=T6ActionValidationCode.ACTION_FIELD_UNEXPECTED,
        )
    review_requested = raw["review_requested"]
    if type(review_requested) is not bool:
        raise T6ActionError(
            "invalid_review_requested",
            validation_code=T6ActionValidationCode.ACTION_VALUE_INVALID,
        )
    action = parse_t6_action(raw["action"])
    if not isinstance(action, (ProbeAction, CompleteIntentAction)):
        raise T6ActionError(
            "d2_action_kind_not_supported",
            validation_code=T6ActionValidationCode.ACTION_KIND_INVALID,
        )
    return D2ActionEnvelope(action, review_requested)
