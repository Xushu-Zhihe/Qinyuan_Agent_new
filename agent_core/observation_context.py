"""Bounded, unverified command observations for the next T6 model action."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal

from agent_core.action_audit import ActionRecoveryRequired
from agent_core.command_contracts import T6CommandRecorderPort
from agent_core.control_store import ControlStateSnapshot, canonical_json
from agent_core.domain import EXECUTION_TERMINAL_STATUSES

MAX_RECENT_EXECUTIONS = 8
MAX_OBSERVATION_CONTEXT_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class T6Observation:
    execution_id: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    context_clipped: bool


def _display(data: bytes) -> str:
    decoded = data.decode("utf-8", errors="surrogateescape")
    result: list[str] = []
    for character in decoded:
        number = ord(character)
        if 0xDC80 <= number <= 0xDCFF:
            result.append(f"\\x{number - 0xDC00:02x}")
        elif number < 32 and character not in "\n\r\t" or number == 127:
            result.append(f"\\x{number:02x}")
        elif character == "\\":
            result.append("\\\\")
        else:
            result.append(character)
    return "".join(result)


def _size(observations: list[T6Observation]) -> int:
    return len(canonical_json([asdict(item) for item in observations]).encode("utf-8"))


def _display_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(value):
        if value.startswith("\\x", index) and index + 4 <= len(value):
            tokens.append(value[index:index + 4])
            index += 4
        elif value.startswith("\\\\", index):
            tokens.append("\\\\")
            index += 2
        else:
            tokens.append(value[index])
            index += 1
    return tokens


def _largest_prefix(
    newer: list[T6Observation], clipped: T6Observation,
    field: Literal["stdout", "stderr"], tokens: list[str],
    max_context_bytes: int = MAX_OBSERVATION_CONTEXT_BYTES,
) -> str:
    low, high = 0, len(tokens)
    while low < high:
        middle = (low + high + 1) // 2
        prefix = "".join(tokens[:middle])
        candidate = (
            replace(clipped, stdout=prefix) if field == "stdout"
            else replace(clipped, stderr=prefix)
        )
        if _size(newer + [candidate]) <= max_context_bytes:
            low = middle
        else:
            high = middle - 1
    return "".join(tokens[:low])


def _fit_latest(
    newer: list[T6Observation], observation: T6Observation,
) -> T6Observation | None:
    if _size(newer + [observation]) <= MAX_OBSERVATION_CONTEXT_BYTES:
        return observation
    clipped = replace(observation, context_clipped=True)
    if _size(newer + [replace(clipped, stdout="", stderr="")]) > MAX_OBSERVATION_CONTEXT_BYTES:
        return None
    # Reserve some room for stderr when both streams overflow; then let stdout
    # use any space stderr did not need. Escape sequences remain whole.
    baseline = _size(newer + [replace(clipped, stdout="", stderr="")])
    stderr_reserve = (
        (MAX_OBSERVATION_CONTEXT_BYTES - baseline) // 4
        if clipped.stderr else 0
    )
    stdout = _largest_prefix(
        newer, replace(clipped, stdout="", stderr=""),
        "stdout", _display_tokens(clipped.stdout),
        MAX_OBSERVATION_CONTEXT_BYTES - stderr_reserve,
    )
    stderr = _largest_prefix(
        newer, replace(clipped, stdout=stdout, stderr=""),
        "stderr", _display_tokens(clipped.stderr),
    )
    stdout = _largest_prefix(
        newer, replace(clipped, stdout="", stderr=stderr),
        "stdout", _display_tokens(clipped.stdout),
    )
    return replace(clipped, stdout=stdout, stderr=stderr)


def recent_t6_observations(
    state: ControlStateSnapshot, intent_id: str, commands: T6CommandRecorderPort,
) -> tuple[T6Observation, ...]:
    """Read at most eight latest completed receipts; never infer missing output."""
    intent = next((item for item in state.intents if item.intent_id == intent_id), None)
    if intent is None:
        raise ValueError("intent_not_found")
    plan_ids = [item.plan_id for item in state.plans if item.intent_id == intent_id]
    plan_order = {plan_id: index for index, plan_id in enumerate(plan_ids)}
    executions = [
        item for item in state.executions if item.plan_id in plan_order
    ]
    if any(item.status not in EXECUTION_TERMINAL_STATUSES for item in executions):
        raise ActionRecoveryRequired("Intent has an unfinished Execution")
    # The committed snapshot orders executions by Plan sequence and insertion.
    selected = executions[-MAX_RECENT_EXECUTIONS:]
    observations: list[T6Observation] = []
    for execution in reversed(selected):
        if execution.output_ref is None:
            raise ActionRecoveryRequired("terminal Execution has no output reference")
        finished = commands.read_completed(execution.execution_id)
        if finished is None:
            raise ActionRecoveryRequired("Execution has no completed command receipt")
        record = finished.record
        if (
            record.request_id != execution.execution_id
            or record.request_id != execution.output_ref
            or record.task_run_id != state.task_run.task_run_id
            or record.plan_id != execution.plan_id
            or record.step_index != execution.step_index
            or not record.termination_confirmed
            or record.status == "outcome_unknown"
        ):
            raise ActionRecoveryRequired("Execution and command receipt disagree")
        current = T6Observation(
            execution.execution_id, execution.status.value, record.exit_code,
            _display(record.stdout), _display(record.stderr),
            record.stdout_truncated, record.stderr_truncated, False,
        )
        fitted = _fit_latest(observations, current)
        if fitted is None:
            break
        observations.append(fitted)
        if fitted.context_clipped:
            break
    ordered = list(reversed(observations))
    if ordered and len(ordered) < len(executions):
        ordered[0] = replace(ordered[0], context_clipped=True)
    return tuple(ordered)
