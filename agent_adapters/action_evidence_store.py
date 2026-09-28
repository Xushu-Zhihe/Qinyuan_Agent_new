"""Immutable structured model-action results outside the control database."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from agent_adapters.command_recorder import T6CommandRecorder, T6EvidenceError
from agent_core.control_store import canonical_json
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.action_audit import ActionResult, ActionResultKind
from agent_core.actions import T6ActionError, action_payload, parse_t6_action


def _digest(value: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class T6ActionEvidenceStore:
    def __init__(self, directory: Path, *, create: bool = True) -> None:
        self._records = T6CommandRecorder(directory, create=create)

    def save(
        self, model_request_id: str, finished_at: float,
        kind: ActionResultKind, result: ActionResult,
    ) -> str:
        if not isinstance(finished_at, (int, float)) or not math.isfinite(finished_at):
            raise ValueError("invalid_action_finish_time")
        if kind not in {"action", "invalid_structure", "model_error"}:
            raise ValueError("invalid_action_result_kind")
        if kind == "action":
            if isinstance(result, ModelError):
                raise ValueError("action_result_kind_mismatch")
            payload = action_payload(result)
            parse_t6_action(payload)
        else:
            if not isinstance(result, ModelError):
                raise ValueError("action_result_kind_mismatch")
            if kind == "invalid_structure" and result.kind is not ModelErrorKind.INVALID_RESPONSE:
                raise ValueError("invalid_structure_error_kind")
            payload = {"kind": result.kind.value, "short_reason": result.short_reason,
                       "evidence_ref": result.evidence_ref}
        value: dict[str, object] = {
            "model_request_id": model_request_id, "finished_at": finished_at,
            "result_kind": kind, "result": payload,
        }
        self._records._write("model-action", model_request_id, value)
        return _digest(value)

    def read(
        self, model_request_id: str,
    ) -> tuple[float, ActionResultKind, ActionResult, str] | None:
        value = self._records._read("model-action", model_request_id)
        if value is None:
            return None
        try:
            if set(value) != {"model_request_id", "finished_at", "result_kind", "result"}:
                raise ValueError("invalid_action_evidence_fields")
            if value["model_request_id"] != model_request_id:
                raise ValueError("action_evidence_id_mismatch")
            finished_at = value["finished_at"]
            if not isinstance(finished_at, (int, float)) or not math.isfinite(finished_at):
                raise ValueError("invalid_action_finish_time")
            kind = value["result_kind"]
            if kind == "action":
                result: ActionResult = parse_t6_action(value["result"])
            elif kind in {"invalid_structure", "model_error"}:
                payload = value["result"]
                if not isinstance(payload, dict) or set(payload) != {
                    "kind", "short_reason", "evidence_ref",
                }:
                    raise ValueError("invalid_action_error")
                error = ModelError(
                    ModelErrorKind(payload["kind"]), payload["short_reason"],
                    payload["evidence_ref"],
                )
                if kind == "invalid_structure" and error.kind is not ModelErrorKind.INVALID_RESPONSE:
                    raise ValueError("invalid_structure_error_kind")
                result = error
            else:
                raise ValueError("invalid_action_result_kind")
            return finished_at, kind, result, _digest(value)
        except (KeyError, TypeError, ValueError, T6ActionError) as exc:
            raise T6EvidenceError("invalid_model_action_evidence") from exc
