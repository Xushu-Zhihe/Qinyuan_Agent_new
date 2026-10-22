"""Immutable D7 candidate bindings and control-commit metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from agent_core.goal_feedback import (
    ExternalValidationCheckCompletion,
    ExternalValidationCheckRequest,
    ExternalValidationFeedback,
)
from agent_core.web_contracts import D7WebRunConfig


def _uuid4(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4")


def _uuid4_or5(value: object, name: str) -> None:
    try:
        parsed = UUID(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5") from exc
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical UUID4 or UUID5")


def _text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text")


@dataclass(frozen=True, slots=True)
class D7WebExecutionBinding:
    """Immutable provenance for one completed Web HTTP execution."""

    task_run_id: str
    turn_id: str
    intent_id: str
    plan_id: str
    execution_id: str
    http_operation_id: str
    response_evidence_id: str
    response_sha256: str
    response_bytes: int

    def __post_init__(self) -> None:
        for name in (
            "task_run_id",
            "plan_id",
            "execution_id",
            "http_operation_id",
            "response_evidence_id",
        ):
            _uuid4(getattr(self, name), name)
        for name in ("turn_id", "intent_id"):
            _uuid4_or5(getattr(self, name), name)
        if (
            not isinstance(self.response_sha256, str)
            or len(self.response_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.response_sha256)
        ):
            raise ValueError("response_sha256 must be lowercase SHA-256")
        if (
            type(self.response_bytes) is not int
            or self.response_bytes < 0
            or self.response_bytes > 2 * 1024 * 1024
        ):
            raise ValueError("response_bytes is outside the D7 Web response limit")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "task_run_id": self.task_run_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "execution_id": self.execution_id,
            "http_operation_id": self.http_operation_id,
            "response_evidence_id": self.response_evidence_id,
            "response_sha256": self.response_sha256,
            "response_bytes": self.response_bytes,
        }

    @classmethod
    def from_dict(cls, value: object) -> D7WebExecutionBinding:
        fields = {
            "schema_version",
            "task_run_id",
            "turn_id",
            "intent_id",
            "plan_id",
            "execution_id",
            "http_operation_id",
            "response_evidence_id",
            "response_sha256",
            "response_bytes",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("D7 Web execution binding fields are invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported D7 Web execution binding schema")
        return cls(
            task_run_id=value["task_run_id"],  # type: ignore[arg-type]
            turn_id=value["turn_id"],  # type: ignore[arg-type]
            intent_id=value["intent_id"],  # type: ignore[arg-type]
            plan_id=value["plan_id"],  # type: ignore[arg-type]
            execution_id=value["execution_id"],  # type: ignore[arg-type]
            http_operation_id=value["http_operation_id"],  # type: ignore[arg-type]
            response_evidence_id=value["response_evidence_id"],  # type: ignore[arg-type]
            response_sha256=value["response_sha256"],  # type: ignore[arg-type]
            response_bytes=value["response_bytes"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class D7AnswerCandidate:
    """Bind one answer candidate to a completed execution and saved evidence."""

    task_run_id: str
    candidate_id: str
    turn_id: str
    intent_id: str
    plan_id: str | None
    execution_id: str | None
    evidence_version: str
    source_ref: str
    source_kind: str = "d3"
    web_execution_binding: D7WebExecutionBinding | None = None

    def __post_init__(self) -> None:
        for name in ("task_run_id", "candidate_id"):
            _uuid4(getattr(self, name), name)
        if self.source_kind != "mcp":
            for name in ("plan_id", "execution_id"):_uuid4(getattr(self,name),name)
        for name in ("turn_id", "intent_id"):
            _uuid4_or5(getattr(self, name), name)
        if (
            not isinstance(self.evidence_version, str)
            or len(self.evidence_version) != 64
            or any(character not in "0123456789abcdef" for character in self.evidence_version)
        ):
            raise ValueError("evidence_version must be lowercase SHA-256")
        _text(self.source_ref, "source_ref")
        if self.source_kind == "d3":
            if self.web_execution_binding is not None:
                raise ValueError("D3 candidate cannot carry a Web execution binding")
        elif self.source_kind == "web":
            binding = self.web_execution_binding
            if not isinstance(binding, D7WebExecutionBinding):
                raise ValueError("Web candidate requires a Web execution binding")
            if (
                binding.task_run_id != self.task_run_id
                or binding.turn_id != self.turn_id
                or binding.intent_id != self.intent_id
                or binding.plan_id != self.plan_id
                or binding.execution_id != self.execution_id
                or binding.response_sha256 != self.evidence_version
                or binding.response_evidence_id != self.source_ref
            ):
                raise ValueError("Web candidate provenance does not match its binding")
        elif self.source_kind == "mcp":
            if self.plan_id is not None or self.execution_id is not None or self.web_execution_binding is not None:
                raise ValueError("MCP candidate cannot carry a Plan or Execution")
            if not self.source_ref.startswith("mcp-result/"):raise ValueError("MCP source reference invalid")
            _uuid4(self.source_ref.removeprefix("mcp-result/"), "MCP operation")
        else:
            raise ValueError("source_kind must be d3, web or mcp")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 3 if self.source_kind == "mcp" else 1 if self.source_kind == "d3" else 2,
            "task_run_id": self.task_run_id,
            "candidate_id": self.candidate_id,
            "turn_id": self.turn_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "execution_id": self.execution_id,
            "evidence_version": self.evidence_version,
            "source_ref": self.source_ref,
        }
        if self.source_kind == "mcp":result["source_kind"] = "mcp"
        if self.source_kind == "web":
            result["source_kind"] = "web"
            assert self.web_execution_binding is not None
            result["web_execution_binding"] = self.web_execution_binding.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object) -> D7AnswerCandidate:
        common_fields = {
            "schema_version", "task_run_id", "candidate_id", "turn_id",
            "intent_id", "plan_id", "execution_id", "evidence_version",
            "source_ref",
        }
        if not isinstance(value, Mapping):
            raise TypeError("D7 answer candidate must be an object")
        schema_version = value.get("schema_version")
        if type(schema_version) is not int or schema_version not in {1, 2, 3}:
            raise ValueError("unsupported D7 answer candidate schema")
        source_kind = "d3"
        web_execution_binding = None
        if schema_version == 1:
            if set(value) != common_fields:
                raise ValueError("D7 answer candidate fields are invalid")
        elif schema_version == 3:
            if value.get("source_kind") != "mcp" or set(value) != common_fields | {"source_kind"}:
                raise ValueError("MCP candidate fields are invalid")
            source_kind = "mcp"
        else:
            if value.get("source_kind") != "web" or set(value) != {
                *common_fields,
                "source_kind",
                "web_execution_binding",
            }:
                raise ValueError("D7 answer candidate source fields are invalid")
            source_kind = "web"
            web_execution_binding = D7WebExecutionBinding.from_dict(
                value["web_execution_binding"]
            )
        return cls(
            task_run_id=value["task_run_id"],  # type: ignore[arg-type]
            candidate_id=value["candidate_id"],  # type: ignore[arg-type]
            turn_id=value["turn_id"],  # type: ignore[arg-type]
            intent_id=value["intent_id"],  # type: ignore[arg-type]
            plan_id=value["plan_id"],  # type: ignore[arg-type]
            execution_id=value["execution_id"],  # type: ignore[arg-type]
            evidence_version=value["evidence_version"],  # type: ignore[arg-type]
            source_ref=value["source_ref"],  # type: ignore[arg-type]
            source_kind=source_kind,
            web_execution_binding=web_execution_binding,
        )


@dataclass(frozen=True, slots=True)
class D7CommitMetadata:
    """Typed D7 payload carried inside one Controller-owned commit."""

    candidate: D7AnswerCandidate | None = None
    feedback: ExternalValidationFeedback | None = None
    check_request: ExternalValidationCheckRequest | None = None
    check_completion: ExternalValidationCheckCompletion | None = None
    run_config: D7WebRunConfig | None = None

    def __post_init__(self) -> None:
        payloads = (
            self.candidate,
            self.feedback,
            self.check_request,
            self.check_completion,
            self.run_config,
        )
        if sum(payload is not None for payload in payloads) != 1:
            raise ValueError("D7 commit metadata requires exactly one payload")
        if self.candidate is not None and not isinstance(self.candidate, D7AnswerCandidate):
            raise ValueError("candidate must be a D7AnswerCandidate")
        if self.feedback is not None and not isinstance(
            self.feedback, ExternalValidationFeedback
        ):
            raise ValueError("feedback must be an ExternalValidationFeedback")
        if self.check_request is not None and not isinstance(
            self.check_request, ExternalValidationCheckRequest
        ):
            raise ValueError("check_request must be an ExternalValidationCheckRequest")
        if self.check_completion is not None and not isinstance(
            self.check_completion, ExternalValidationCheckCompletion
        ):
            raise ValueError(
                "check_completion must be an ExternalValidationCheckCompletion"
            )
        if self.run_config is not None and not isinstance(
            self.run_config, D7WebRunConfig
        ):
            raise ValueError("run_config must be a D7WebRunConfig")

    def to_dict(self) -> dict[str, object]:
        if self.candidate is not None:
            return {"kind": "candidate", "candidate": self.candidate.to_dict()}
        if self.feedback is not None:
            return {"kind": "feedback", "feedback": self.feedback.to_dict()}
        if self.check_request is not None:
            return {"kind": "check_request", "request": self.check_request.to_dict()}
        if self.check_completion is not None:
            return {
                "kind": "check_completion",
                "completion": self.check_completion.to_dict(),
            }
        assert self.run_config is not None
        return {"kind": "web_run_config", "config": self.run_config.to_dict()}


__all__ = ["D7AnswerCandidate", "D7CommitMetadata", "D7WebExecutionBinding"]
