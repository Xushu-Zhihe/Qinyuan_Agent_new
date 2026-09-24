"""Exact file-type candidate and output rules for the first T4 fact."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from agent_core.file_inspection import validate_inspection_path
from agent_core.tool_contracts import ExecutionRecord, require_id


FACT_PREFIX = "工具文件类型识别结果："
MAX_OBSERVED_TYPE_LENGTH = 200


class FileTypeOutputRejected(ValueError):
    """The recorded output cannot support a first-round file-type fact."""


def _valid_observed_type(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_OBSERVED_TYPE_LENGTH
        and any(not char.isspace() for char in value)
        and all(
            unicodedata.category(char) != "Cc" and char not in "\u2028\u2029"
            for char in value
        )
    )


@dataclass(frozen=True, slots=True)
class FileTypeFactCandidate:
    path: str
    observed_type: str
    execution_id: str

    def __post_init__(self) -> None:
        validate_inspection_path(self.path)
        require_id(self.execution_id)
        if not _valid_observed_type(self.observed_type):
            raise ValueError("observed_type must be one bounded nonblank text line")


def observed_file_type(record: ExecutionRecord) -> str:
    if (
        record.exit_code != 0
        or record.failure_type is not None
        or record.timed_out
        or record.cancelled
        or not record.termination_confirmed
        or record.stdout_truncated
        or record.stderr_truncated
        or record.stderr
    ):
        raise FileTypeOutputRejected(
            "execution does not have complete successful output"
        )

    output = record.stdout
    if output.endswith("\r\n"):
        output = output[:-2]
    elif output.endswith("\n"):
        output = output[:-1]
    if not _valid_observed_type(output):
        raise FileTypeOutputRejected("file-type output is not one bounded text line")
    return output


def fact_content(observed_type: str) -> str:
    if not _valid_observed_type(observed_type):
        raise ValueError("observed_type must be one bounded nonblank text line")
    return FACT_PREFIX + observed_type
