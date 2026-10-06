"""Interactive, operator-authorized viewing for protected D7 candidates."""

from __future__ import annotations

import codecs
import hashlib
import math
import os
import re
from dataclasses import dataclass
from typing import Protocol

from agent_adapters.v7.candidate_evidence import (
    D7CandidateEvidenceError,
    D7CandidateEvidenceService,
)

OPERATOR_CANDIDATE_VIEW_ADAPTER_ID = "operator_candidate_view"
IS_WINDOWS = os.name == "nt"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class _BinaryOutput(Protocol):
    def write(self, value: bytes) -> int: ...

    def flush(self) -> None: ...


class _InteractiveInput(Protocol):
    encoding: str | None

    def isatty(self) -> bool: ...

    def fileno(self) -> int: ...

    def readline(self, size: int = -1) -> str: ...


class _InteractiveOutput(Protocol):
    encoding: str | None
    buffer: _BinaryOutput

    def isatty(self) -> bool: ...

    def fileno(self) -> int: ...

    def write(self, value: str) -> int: ...

    def flush(self) -> None: ...


class D7CandidateOperatorViewError(RuntimeError):
    """A fixed, body-free error code for operator candidate viewing."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class D7CandidateOperatorViewResult:
    task_run_id: str
    candidate_id: str
    evidence_version: str
    status: str = "viewed"

    def to_dict(self) -> dict[str, str]:
        return {
            "task_run_id": self.task_run_id,
            "candidate_id": self.candidate_id,
            "evidence_version": self.evidence_version,
            "status": self.status,
        }


def _windows_console_handle(stream: _InteractiveInput | _InteractiveOutput) -> bool:
    """Accept only a character console handle, excluding files and pipes."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle_value = msvcrt.get_osfhandle(stream.fileno())
        handle = wintypes.HANDLE(handle_value)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_file_type = kernel32.GetFileType
        get_file_type.argtypes = (wintypes.HANDLE,)
        get_file_type.restype = wintypes.DWORD
        get_console_mode = kernel32.GetConsoleMode
        get_console_mode.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_console_mode.restype = wintypes.BOOL

        mode = wintypes.DWORD()
        return (
            get_file_type(handle) == 0x0002  # FILE_TYPE_CHAR
            and bool(get_console_mode(handle, ctypes.byref(mode)))
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _require_interactive_utf8(
    stdin: _InteractiveInput,
    stdout: _InteractiveOutput,
) -> None:
    try:
        if not stdin.isatty() or not stdout.isatty():
            raise D7CandidateOperatorViewError(
                "d7_candidate_view_requires_terminal"
            )
    except D7CandidateOperatorViewError:
        raise
    except Exception as exc:
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_requires_terminal"
        ) from exc

    if not callable(getattr(getattr(stdout, "buffer", None), "write", None)):
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_requires_terminal"
        )
    try:
        if codecs.lookup(stdout.encoding or "").name != "utf-8":
            raise D7CandidateOperatorViewError(
                "d7_candidate_view_terminal_encoding_unsupported"
            )
    except D7CandidateOperatorViewError:
        raise
    except (LookupError, TypeError, ValueError) as exc:
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_terminal_encoding_unsupported"
        ) from exc

    if IS_WINDOWS and (
        not _windows_console_handle(stdin) or not _windows_console_handle(stdout)
    ):
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_requires_terminal"
        )


def _validated_binding(
    service: D7CandidateEvidenceService,
    candidate_id: str,
    evidence_version: str,
) -> dict[str, object]:
    if not isinstance(evidence_version, str) or _SHA256_RE.fullmatch(evidence_version) is None:
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_evidence_version_invalid"
        )
    binding = service.query(candidate_id)
    required = {
        "task_run_id",
        "candidate_id",
        "execution_id",
        "evidence_version",
        "body_sha256",
        "byte_size",
        "created_at",
        "body_status",
        "body_ref",
        "body",
    }
    if (
        not isinstance(binding, dict)
        or not required <= binding.keys()
        or binding.get("task_run_id") != service.task_run_id
        or binding.get("candidate_id") != candidate_id
        or binding.get("body") is not None
        or binding.get("body_status") != "withheld"
        or not isinstance(binding.get("execution_id"), str)
        or not isinstance(binding.get("evidence_version"), str)
        or not isinstance(binding.get("body_sha256"), str)
        or _SHA256_RE.fullmatch(binding["body_sha256"]) is None
        or type(binding.get("byte_size")) is not int
        or not 1 <= binding["byte_size"] <= 16 * 1024
        or type(binding.get("created_at")) not in {float, int}
        or not math.isfinite(binding["created_at"])
    ):
        raise D7CandidateOperatorViewError("d7_candidate_view_binding_invalid")
    if binding["evidence_version"] != evidence_version:
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_evidence_version_mismatch"
        )
    return binding


def _display_binding(stdout: _InteractiveOutput, binding: dict[str, object]) -> None:
    display = (
        "Protected candidate binding\n"
        f"TaskRun: {binding['task_run_id']}\n"
        f"Candidate: {binding['candidate_id']}\n"
        f"Execution: {binding['execution_id']}\n"
        f"Evidence version: {binding['evidence_version']}\n"
        f"Body SHA-256: {binding['body_sha256']}\n"
        f"UTF-8 bytes: {binding['byte_size']}\n"
        "Enter the exact candidate ID to display its body: "
    )
    try:
        stdout.write(display)
        stdout.flush()
    except Exception as exc:
        raise D7CandidateOperatorViewError("d7_candidate_view_output_failed") from exc


def _confirmed_candidate_id(stdin: _InteractiveInput, candidate_id: str) -> None:
    try:
        value = stdin.readline(80)
    except Exception as exc:
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_confirmation_failed"
        ) from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if value != candidate_id:
        raise D7CandidateOperatorViewError(
            "d7_candidate_view_confirmation_rejected"
        )


class D7CandidateOperatorViewer:
    """Read one named candidate only after an interactive exact-ID confirmation."""

    def __init__(
        self,
        service: D7CandidateEvidenceService,
        *,
        adapter_id: str = OPERATOR_CANDIDATE_VIEW_ADAPTER_ID,
    ) -> None:
        if not isinstance(service, D7CandidateEvidenceService):
            raise D7CandidateOperatorViewError("d7_candidate_view_service_invalid")
        if not isinstance(adapter_id, str) or not adapter_id:
            raise D7CandidateOperatorViewError("d7_candidate_view_adapter_invalid")
        self.service = service
        self.adapter_id = adapter_id

    def view(
        self,
        candidate_id: str,
        *,
        evidence_version: str,
        stdin: _InteractiveInput,
        stdout: _InteractiveOutput,
    ) -> D7CandidateOperatorViewResult:
        _require_interactive_utf8(stdin, stdout)
        binding = _validated_binding(self.service, candidate_id, evidence_version)
        _display_binding(stdout, binding)
        _confirmed_candidate_id(stdin, candidate_id)

        body = self.service.read_port(self.adapter_id).read(candidate_id)
        try:
            payload = body.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise D7CandidateEvidenceError(
                "d7_candidate_body_integrity_failed"
            ) from exc
        if (
            len(payload) != binding["byte_size"]
            or hashlib.sha256(payload).hexdigest() != binding["body_sha256"]
        ):
            raise D7CandidateEvidenceError("d7_candidate_body_integrity_failed")

        try:
            written = stdout.buffer.write(payload)
            stdout.flush()
        except Exception as exc:
            raise D7CandidateOperatorViewError(
                "d7_candidate_view_output_failed"
            ) from exc
        if written != len(payload):
            raise D7CandidateOperatorViewError("d7_candidate_view_output_failed")

        return D7CandidateOperatorViewResult(
            task_run_id=str(binding["task_run_id"]),
            candidate_id=candidate_id,
            evidence_version=str(binding["evidence_version"]),
        )


__all__ = [
    "D7CandidateOperatorViewError",
    "D7CandidateOperatorViewResult",
    "D7CandidateOperatorViewer",
    "OPERATOR_CANDIDATE_VIEW_ADAPTER_ID",
]
