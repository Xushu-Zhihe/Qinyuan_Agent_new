from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from agent_core import (
    CONFIRMATION_WINDOW_SECONDS,
    Attachment,
    InvalidTransition,
    ModelBackend,
    Target,
    TaskRequest,
    TaskRequestError,
    TaskRunController,
    TaskRunStatus,
    resolve_model_backend,
)


def request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "goal": "找到flag",
        "target": {
            "endpoints": [{"url": "http://example.org:8080/path"}],
            "attachments": [
                {"kind": "local_path", "path": "attachments/a.bin"},
                {"kind": "url", "url": "https://example.org/b.zip"},
            ],
        },
    }
    payload.update(overrides)
    return payload


@dataclass
class FakeClock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeSandbox:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def create(self, task_run_id: str) -> str:
        sandbox_id = f"sandbox-{task_run_id}"
        self.calls.append(("create", sandbox_id))
        return sandbox_id

    def interrupt(self, sandbox_id: str) -> None:
        self.calls.append(("interrupt", sandbox_id))

    def destroy(self, sandbox_id: str) -> None:
        self.calls.append(("destroy", sandbox_id))


def test_task_request_validates_confirmed_shape_and_round_trips() -> None:
    request = TaskRequest.from_dict(
        request_payload(deadline_seconds=42, model_backend="deepseek_openai")
    )

    assert request.goal == "找到flag"
    assert request.deadline_seconds == 42
    assert request.model_backend is ModelBackend.DEEPSEEK_OPENAI
    assert request.target.endpoints[0].url == "http://example.org:8080/path"
    assert request.target.attachments == (
        Attachment(kind="local_path", value="attachments/a.bin"),
        Attachment(kind="url", value="https://example.org/b.zip"),
    )
    assert request.to_dict()["target"] == request_payload()["target"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"goal": "", "target": {"endpoints": [{"url": "http://a"}]}}, "goal"),
        ({"goal": "x", "target": {}}, "at least one item"),
        (
            {
                "goal": "x",
                "target": {"endpoints": [{"url": "ftp://example.org"}]},
            },
            "http://",
        ),
        (
            {
                "goal": "x",
                "target": {"endpoints": [{"url": "tcp://example.org"}]},
            },
            "tcp port",
        ),
        (
            {
                "goal": "x",
                "target": {
                    "attachments": [{"kind": "local_path", "path": "C:\\a.bin"}]
                },
            },
            "relative",
        ),
        (
            {
                "goal": "x",
                "target": {"attachments": [{"kind": "other", "url": "x"}]},
            },
            "local_path",
        ),
        (
            {
                "goal": "x",
                "target": {"endpoints": [{"url": "http://a"}]},
                "deadline_seconds": True,
            },
            "integer",
        ),
    ],
)
def test_task_request_rejects_invalid_known_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(TaskRequestError, match=message):
        TaskRequest.from_dict(payload)


def test_model_backend_conflict_is_rejected_without_a_default() -> None:
    assert resolve_model_backend(None, None) is None
    assert resolve_model_backend("codex_cli", None) is ModelBackend.CODEX_CLI
    assert resolve_model_backend(None, "claude_code_cli") is ModelBackend.CLAUDE_CODE_CLI
    assert resolve_model_backend("deepseek_openai", "deepseek_openai") is ModelBackend.DEEPSEEK_OPENAI
    with pytest.raises(TaskRequestError, match="conflicts"):
        resolve_model_backend("codex_cli", "deepseek_openai")


def test_start_creates_one_sandbox_and_emits_global_task_run_id() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    controller = TaskRunController(sandbox, clock=clock)

    task_run = controller.start(TaskRequest.from_dict(request_payload()))

    UUID(task_run.task_run_id)
    assert task_run.started_at == 100.0
    assert task_run.deadline_at == 3700.0
    assert task_run.status is TaskRunStatus.RUNNING
    assert sandbox.calls == [("create", task_run.sandbox_id)]
    assert controller.events[0].type == "task_run_started"
    assert controller.events[0].task_run_id == task_run.task_run_id


def test_confirmation_expires_to_assumed_achieved_after_three_minutes() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    controller = TaskRunController(sandbox, clock=clock)
    task_run = controller.start(TaskRequest.from_dict(request_payload()))

    clock.now += 1
    assert controller.mark_candidate_found() is TaskRunStatus.AWAITING_CONFIRMATION
    clock.now += CONFIRMATION_WINDOW_SECONDS - 1
    assert controller.poll() is TaskRunStatus.AWAITING_CONFIRMATION
    clock.now += 1
    assert controller.poll() is TaskRunStatus.ASSUMED_ACHIEVED
    assert sandbox.calls == [
        ("create", task_run.sandbox_id),
        ("destroy", task_run.sandbox_id),
    ]


def test_external_achieved_confirmation_finishes_and_destroys_sandbox() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    controller = TaskRunController(sandbox, clock=clock)
    task_run = controller.start(TaskRequest.from_dict(request_payload()))
    controller.mark_candidate_found()

    status = controller.handle_control_message(
        {
            "type": "completion",
            "task_run_id": task_run.task_run_id,
            "status": "achieved",
        }
    )

    assert status is TaskRunStatus.ACHIEVED
    assert sandbox.calls == [
        ("create", task_run.sandbox_id),
        ("destroy", task_run.sandbox_id),
    ]


def test_wrong_completion_message_cannot_finish_a_task_run() -> None:
    controller = TaskRunController(FakeSandbox(), clock=FakeClock())
    task_run = controller.start(TaskRequest.from_dict(request_payload()))
    controller.mark_candidate_found()

    with pytest.raises(InvalidTransition, match="does not match"):
        controller.handle_control_message(
            {
                "type": "completion",
                "task_run_id": "another-run",
                "status": "achieved",
            }
        )
    assert task_run.status is TaskRunStatus.AWAITING_CONFIRMATION


def test_model_failure_is_terminal_and_cleans_up_sandbox() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    controller = TaskRunController(sandbox, clock=clock)
    task_run = controller.start(TaskRequest.from_dict(request_payload()))

    assert controller.model_failed("provider unavailable") is TaskRunStatus.FAILED
    assert task_run.failure_reason == "provider unavailable"
    assert sandbox.calls == [
        ("create", task_run.sandbox_id),
        ("destroy", task_run.sandbox_id),
    ]


def test_deadline_has_precedence_and_interrupts_before_destroy() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    controller = TaskRunController(sandbox, clock=clock)
    task_run = controller.start(
        TaskRequest.from_dict(request_payload(deadline_seconds=10))
    )
    controller.mark_candidate_found()

    clock.now = task_run.deadline_at
    assert controller.poll() is TaskRunStatus.DEADLINE_EXCEEDED
    assert sandbox.calls == [
        ("create", task_run.sandbox_id),
        ("interrupt", task_run.sandbox_id),
        ("destroy", task_run.sandbox_id),
    ]


def test_cancel_is_terminal_and_destroy_is_idempotent() -> None:
    clock = FakeClock()
    sandbox = FakeSandbox()
    controller = TaskRunController(sandbox, clock=clock)
    task_run = controller.start(TaskRequest.from_dict(request_payload()))

    assert controller.cancel() is TaskRunStatus.CANCELLED
    with pytest.raises(InvalidTransition, match="terminal"):
        controller.cancel()
    assert sandbox.calls == [
        ("create", task_run.sandbox_id),
        ("destroy", task_run.sandbox_id),
    ]
