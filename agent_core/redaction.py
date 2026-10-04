"""D5 redaction for configured secrets and common credential forms."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace


@dataclass(frozen=True, slots=True)
class D5SensitiveValue:
    """Trusted in-memory configuration; never serialized with run state."""

    version: str
    value: str = field(repr=False)


def d5_sensitive_configuration_matches(
    requirements: tuple[tuple[str, str], ...] | None,
    configuration: Mapping[str, D5SensitiveValue] | None,
) -> bool:
    if requirements is None:
        return False
    if configuration is None:
        return not requirements
    if not isinstance(configuration, Mapping):
        return False
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(entry, D5SensitiveValue)
        or not isinstance(entry.version, str)
        or not entry.version.strip()
        or not isinstance(entry.value, str)
        or not entry.value.strip()
        for key, entry in configuration.items()
    ):
        return False
    return all(
        key in configuration and configuration[key].version == version
        for key, version in requirements
    )


_REDACTED = "[REDACTED]"
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>\b(?:password|passwd|pwd|token|api[_-]?key|secret|access[_-]?key)\b[\"']?\s*[:=]\s*)"
    r"(?!\[REDACTED(?: PRIVATE KEY)?\])"
    r"(?:(?P<quote>[\"'])(?P<quoted>(?:\\[\s\S]|(?!(?P=quote))[^\\])*)(?P=quote)|(?P<bare>[^\s,;\}\]\&]+))"
)
_AUTH_VALUE = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+)"
    r"[A-Za-z0-9._~+/=-]+"
)
_URL_USERINFO = re.compile(
    r"(?i)(?P<prefix>\b[a-z][a-z0-9+.-]*://)(?!\[REDACTED\])[^/@\s]+@"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)


class D5Redactor:
    def __init__(self, sensitive_values: tuple[str, ...] | None = None) -> None:
        self.configured = sensitive_values is not None
        if sensitive_values is None:
            sensitive_values = ()
        if not isinstance(sensitive_values, tuple) or any(
            not isinstance(value, str) or not value for value in sensitive_values
        ):
            raise ValueError("sensitive_values must contain non-empty strings")
        self._sensitive_values = tuple(
            sorted(set(sensitive_values), key=len, reverse=True)
        )

    def redact(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
        value = _URL_USERINFO.sub(
            lambda match: match["prefix"] + _REDACTED + "@", value
        )
        value = _AUTH_VALUE.sub(lambda match: match["prefix"] + _REDACTED, value)
        value = _CREDENTIAL_ASSIGNMENT.sub(self._redact_assignment, value)
        for secret in self._sensitive_values:
            value = value.replace(secret, _REDACTED)
        return value

    def contains_sensitive(self, text: str) -> bool:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if any(secret in text for secret in self._sensitive_values):
            return True
        if (
            _PRIVATE_KEY.search(text) is not None
            or _URL_USERINFO.search(text) is not None
            or _AUTH_VALUE.search(text) is not None
        ):
            return True
        for match in _CREDENTIAL_ASSIGNMENT.finditer(text):
            captured = match.group("quoted") or match.group("bare")
            if captured and captured != _REDACTED:
                return True
        return False

    def redact_value(self, value: object) -> object:
        if is_dataclass(value) and not isinstance(value, type):
            return self.redact_value(asdict(value))
        if isinstance(value, str):
            return self.redact(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("mapping keys must be strings")
                result[self.redact(key)] = self.redact_value(item)
            return result
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        raise TypeError("value must contain only JSON-compatible data")

    def redact_model_input(self, value: object) -> object:
        """Redact readable text while preserving typed IDs, enum values, and digests."""
        if is_dataclass(value) and not isinstance(value, type):
            updates = {}
            for item in fields(value):
                current = getattr(value, item.name)
                if item.name.endswith(("_id", "_ref", "_sha256")) or item.name in {
                    "role",
                    "stage",
                    "status",
                    "kind",
                    "version",
                    "attempt",
                    "base_checkpoint_sequence",
                    "attachment_index",
                    "step_index",
                    "exit_code",
                    "network_allowed",
                    "stdout_truncated",
                    "stderr_truncated",
                    "context_clipped",
                    "review_generation",
                    "review_attempt",
                    "review_scope",
                    "decision",
                    "outcome",
                }:
                    continue
                if is_dataclass(current):
                    updates[item.name] = self.redact_model_input(current)
                elif isinstance(current, tuple):
                    updates[item.name] = tuple(
                        self.redact_model_input(part) for part in current
                    )
                elif isinstance(current, list):
                    updates[item.name] = [
                        self.redact_model_input(part) for part in current
                    ]
                elif isinstance(current, Mapping):
                    updates[item.name] = {
                        key: self.redact_model_input(part)
                        for key, part in current.items()
                    }
                elif isinstance(current, str):
                    # Context summaries are serialized JSON; redact their decoded fields.
                    if item.name in {"context_summary", "summary_json"}:
                        try:
                            parsed = __import__("json").loads(current)
                        except (ValueError, TypeError):
                            updates[item.name] = self.redact(current)
                        else:
                            updates[item.name] = __import__("json").dumps(
                                self.redact_value(parsed),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                    else:
                        updates[item.name] = self.redact(current)
                else:
                    updates[item.name] = current
            return replace(value, **updates)
        return self.redact_value(value)

    @staticmethod
    def _redact_assignment(match: re.Match[str]) -> str:
        quote = match.group("quote")
        if quote is not None:
            return f"{match.group('prefix')}{quote}{_REDACTED}{quote}"
        return f"{match.group('prefix')}{_REDACTED}"
