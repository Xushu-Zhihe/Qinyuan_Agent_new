from __future__ import annotations

from dataclasses import replace

import pytest

from agent_core.domain import FailureType
from agent_core.file_type_fact import (
    FileTypeFactCandidate,
    FileTypeOutputRejected,
    fact_content,
    observed_file_type,
)
from agent_core.tool_contracts import ExecutionRecord


def successful_record(stdout: str = "ASCII text\n") -> ExecutionRecord:
    return ExecutionRecord(
        request_id="request-1",
        task_run_id="task-1",
        sandbox_id="sandbox-1",
        plan_id="plan-1",
        step_index=0,
        authorization_id="auth-1",
        execution_id="execution-1",
        command_ref="command-1",
        output_ref="execution-1",
        started_at=1.0,
        finished_at=2.0,
        exit_code=0,
        failure_type=None,
        short_reason="",
        stdout=stdout,
        termination_confirmed=True,
    )


@pytest.mark.parametrize("stdout", ["ASCII text", "ASCII text\n", "ASCII text\r\n"])
def test_accepts_one_exact_line_and_only_one_optional_final_newline(
    stdout: str,
) -> None:
    observed = observed_file_type(successful_record(stdout))
    assert observed == "ASCII text"
    assert fact_content(observed) == "工具文件类型识别结果：ASCII text"
    assert (
        FileTypeFactCandidate(
            "/workspace/input/sample.txt", observed, "execution-1"
        ).observed_type
        == observed
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        " \u3000 ",
        "ASCII text\n\n",
        "ASCII text\r\n\r\n",
        "ASCII text\nPython source",
        "ASCII\ttext",
        "ASCII\x00text",
        "ASCII\x7ftext",
        "ASCII\x85text",
        "ASCII\u2028text",
        "ASCII\u2029text",
        "ASCII text\r",
        "A" * 201,
    ],
)
def test_rejects_unsupported_output(stdout: str) -> None:
    with pytest.raises(FileTypeOutputRejected):
        observed_file_type(successful_record(stdout))


@pytest.mark.parametrize(
    "changes",
    [
        {"exit_code": 1},
        {"failure_type": FailureType.ENVIRONMENT_FAULT},
        {"timed_out": True},
        {"cancelled": True},
        {"termination_confirmed": False},
        {"stdout_truncated": True},
        {"stderr_truncated": True},
        {"stderr": "warning"},
    ],
)
def test_rejects_incomplete_or_unsuccessful_execution(
    changes: dict[str, object],
) -> None:
    with pytest.raises(FileTypeOutputRejected):
        observed_file_type(replace(successful_record(), **changes))


def test_preserves_spaces_and_case_without_inferring_source_language() -> None:
    record = successful_record(" ASCII text ")
    assert observed_file_type(record) == " ASCII text "
    assert (
        fact_content(observed_file_type(record)) == "工具文件类型识别结果： ASCII text "
    )
    candidate = FileTypeFactCandidate(
        "/workspace/input/sample.txt", "Python source", "execution-1"
    )
    assert candidate.observed_type != observed_file_type(record)
    with pytest.raises(ValueError):
        FileTypeFactCandidate(
            "/workspace/input/sample.txt", "ASCII\ttext", "execution-1"
        )
