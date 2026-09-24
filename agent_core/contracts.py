from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from urllib.parse import urlsplit


class TaskRequestError(ValueError):
    """Raised when a task request violates the confirmed input contract."""


class ModelBackend(StrEnum):
    """The model backend names currently accepted by the public request."""

    CODEX_CLI = "codex_cli"
    CLAUDE_CODE_CLI = "claude_code_cli"
    DEEPSEEK_OPENAI = "deepseek_openai"


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskRequestError(f"{field_name} must be a non-empty string")
    return value


def _array(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TaskRequestError(f"{field_name} must be an array")
    return value


def _validate_endpoint_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"}:
        if not parsed.hostname:
            raise TaskRequestError(
                "target.endpoints[].url must contain a host for http/https"
            )
        return

    if parsed.scheme == "tcp":
        if not parsed.hostname:
            raise TaskRequestError(
                "target.endpoints[].url must contain a host for tcp"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise TaskRequestError(
                "target.endpoints[].url must contain a valid tcp port"
            ) from exc
        if port is None:
            raise TaskRequestError(
                "target.endpoints[].url must contain a tcp port"
            )
        return

    raise TaskRequestError(
        "target.endpoints[].url must use http://, https://, or tcp://host:port"
    )


@dataclass(frozen=True, slots=True)
class Endpoint:
    url: str

    @classmethod
    def from_dict(cls, value: Any) -> "Endpoint":
        if not isinstance(value, Mapping):
            raise TaskRequestError("each target endpoint must be an object")
        url = _required_string(value.get("url"), "target.endpoints[].url")
        _validate_endpoint_url(url)
        return cls(url=url)


@dataclass(frozen=True, slots=True)
class Attachment:
    """An attachment reference before the later import/materialization phase."""

    kind: str
    value: str

    @classmethod
    def from_dict(cls, value: Any) -> "Attachment":
        if not isinstance(value, Mapping):
            raise TaskRequestError("each target attachment must be an object")

        kind = _required_string(value.get("kind"), "target.attachments[].kind")
        if kind == "local_path":
            path = _required_string(
                value.get("path"),
                "target.attachments[].path",
            )
            # The source is resolved against the CLI process cwd later.  This
            # round only enforces the confirmed relative-path requirement.
            if PurePosixPath(path).is_absolute() or PureWindowsPath(path).anchor:
                raise TaskRequestError(
                    "target.attachments[].path must be relative to the CLI cwd"
                )
            return cls(kind=kind, value=path)

        if kind == "url":
            # URL scheme/duplicate-name/import policy is intentionally deferred
            # to the attachment import round.
            return cls(
                kind=kind,
                value=_required_string(
                    value.get("url"),
                    "target.attachments[].url",
                ),
            )

        raise TaskRequestError(
            "target.attachments[].kind must be 'local_path' or 'url'"
        )

    def to_dict(self) -> dict[str, str]:
        if self.kind == "local_path":
            return {"kind": self.kind, "path": self.value}
        return {"kind": self.kind, "url": self.value}


@dataclass(frozen=True, slots=True)
class Target:
    endpoints: tuple[Endpoint, ...] = ()
    attachments: tuple[Attachment, ...] = ()

    @classmethod
    def from_dict(cls, value: Any) -> "Target":
        if not isinstance(value, Mapping):
            raise TaskRequestError("target must be an object")

        endpoint_values = _array(value.get("endpoints", []), "target.endpoints")
        attachment_values = _array(
            value.get("attachments", []),
            "target.attachments",
        )
        endpoints = tuple(Endpoint.from_dict(item) for item in endpoint_values)
        attachments = tuple(
            Attachment.from_dict(item) for item in attachment_values
        )
        if not endpoints and not attachments:
            raise TaskRequestError(
                "target.endpoints or target.attachments must contain at least one item"
            )
        return cls(endpoints=endpoints, attachments=attachments)

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {
            "endpoints": [{"url": item.url} for item in self.endpoints],
            "attachments": [item.to_dict() for item in self.attachments],
        }


@dataclass(frozen=True, slots=True)
class TaskRequest:
    goal: str
    target: Target
    deadline_seconds: int = 3600
    model_backend: ModelBackend | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "TaskRequest":
        if not isinstance(value, Mapping):
            raise TaskRequestError("TaskRequest must be a JSON object")

        goal = _required_string(value.get("goal"), "goal")
        target = Target.from_dict(value.get("target"))

        deadline = value.get("deadline_seconds", 3600)
        if isinstance(deadline, bool) or not isinstance(deadline, int):
            raise TaskRequestError("deadline_seconds must be an integer")

        backend_value = value.get("model_backend")
        backend: ModelBackend | None
        if backend_value is None:
            backend = None
        else:
            if not isinstance(backend_value, str):
                raise TaskRequestError("model_backend must be a string")
            try:
                backend = ModelBackend(backend_value)
            except ValueError as exc:
                raise TaskRequestError(
                    "model_backend must be codex_cli, claude_code_cli, or "
                    "deepseek_openai"
                ) from exc

        return cls(
            goal=goal,
            target=target,
            deadline_seconds=deadline,
            model_backend=backend,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "goal": self.goal,
            "target": self.target.to_dict(),
            "deadline_seconds": self.deadline_seconds,
        }
        if self.model_backend is not None:
            result["model_backend"] = self.model_backend.value
        return result


def resolve_model_backend(
    request_backend: str | ModelBackend | None,
    cli_backend: str | ModelBackend | None,
) -> ModelBackend | None:
    """Resolve the two confirmed selection sources without choosing a default."""

    def normalize(value: str | ModelBackend | None) -> ModelBackend | None:
        if value is None:
            return None
        try:
            return value if isinstance(value, ModelBackend) else ModelBackend(value)
        except ValueError as exc:
            raise TaskRequestError(
                "model backend must be codex_cli, claude_code_cli, or deepseek_openai"
            ) from exc

    request_value = normalize(request_backend)
    cli_value = normalize(cli_backend)
    if request_value is not None and cli_value is not None:
        if request_value != cli_value:
            raise TaskRequestError(
                "TaskRequest model_backend conflicts with the CLI backend"
            )
        return request_value
    return request_value or cli_value
