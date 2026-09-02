from __future__ import annotations

from dataclasses import dataclass, field, replace
from uuid import UUID

import pytest

from agent_core import (
    DomainError,
    Fact,
    FactSnapshot,
    FailureSummary,
    FailureType,
    IntentContext,
    IntentGenerationResponse,
    ModelContractError,
    ModelError,
    ModelErrorKind,
    ModelInvocationCoordinator,
    ModelRequest,
    ModelRequestType,
    PlanGenerationResponse,
    Target,
)
from agent_core.fake_model_adapters import (
    FakeClaudeCodeAdapter,
    FakeCodexAdapter,
    FakeDeepSeekAdapter,
)


@dataclass
class FakeCancellationToken:
    cancelled: bool = False

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled


@dataclass
class FakeDeadline:
    values: list[float]
    calls: int = 0

    def remaining_seconds(self) -> float:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


@dataclass
class FakeErrorEvidence:
    records: set[tuple[str, str]] = field(default_factory=set)

    def exists(self, task_run_id: str, evidence_ref: str) -> bool:
        return (task_run_id, evidence_ref) in self.records


def make_request(
    *,
    request_type: ModelRequestType = ModelRequestType.INTENT_GENERATION,
    failure_summaries: tuple[FailureSummary, ...] = (),
    verified_facts: tuple[FactSnapshot, ...] = (),
) -> ModelRequest:
    return ModelRequest(
        model_request_id="request-1",
        task_run_id="task-run-1",
        turn_id="turn-1",
        request_type=request_type,
        goal="找到 flag",
        target=Target.from_dict(
            {"endpoints": [{"url": "https://example.test/challenge"}]}
        ),
        turn_target="确认开放入口",
        verified_facts=verified_facts,
        failure_summaries=failure_summaries,
        intent=(
            IntentContext(intent_id="intent-1", description="确认入口")
            if request_type is ModelRequestType.PLAN_GENERATION
            else None
        ),
    )


def coordinator(adapter: object, deadline: FakeDeadline | None = None) -> ModelInvocationCoordinator:
    return ModelInvocationCoordinator(
        adapter,  # type: ignore[arg-type]
        deadline or FakeDeadline([60]),
        FakeErrorEvidence(),
    )


@pytest.mark.parametrize(
    ("adapter", "raw"),
    [
        (
            FakeCodexAdapter,
            {
                "event": "completed",
                "payload": {
                    "intents": [
                        {"description": "检查首页", "intent_type": "recon"}
                    ]
                },
            },
        ),
        (
            FakeClaudeCodeAdapter,
            {
                "type": "message",
                "content": [
                    {
                        "type": "json",
                        "data": {"intents": [{"description": "检查首页"}]},
                    }
                ],
            },
        ),
        (
            FakeDeepSeekAdapter,
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"intents": [{"description": "检查首页"}]}'
                        }
                    }
                ]
            },
        ),
    ],
)
def test_fake_adapters_normalize_intent_candidates(adapter: object, raw: object) -> None:
    result = coordinator(adapter([raw])).invoke(make_request(), FakeCancellationToken())  # type: ignore[operator]

    assert isinstance(result, IntentGenerationResponse)
    assert result.candidates[0].description == "检查首页"


@pytest.mark.parametrize(
    ("adapter", "raw"),
    [
        (
            FakeCodexAdapter,
            {"event": "completed", "payload": {"steps": [{"description": "确认服务"}]}},
        ),
        (
            FakeClaudeCodeAdapter,
            {
                "type": "message",
                "content": [
                    {"type": "json", "data": {"steps": [{"description": "确认服务"}]}}
                ],
            },
        ),
        (
            FakeDeepSeekAdapter,
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"steps": [{"description": "确认服务"}]}'
                        }
                    }
                ]
            },
        ),
    ],
)
def test_fake_adapters_normalize_plan_candidate(adapter: object, raw: object) -> None:
    result = coordinator(adapter([raw])).invoke(  # type: ignore[operator]
        make_request(request_type=ModelRequestType.PLAN_GENERATION),
        FakeCancellationToken(),
    )

    assert isinstance(result, PlanGenerationResponse)
    assert result.candidate.steps[0].description == "确认服务"


def test_fact_content_limit_applies_to_snapshots_and_domain_updates() -> None:
    snapshot = FactSnapshot("fact-1", "a" * 256, "execution-1")
    assert snapshot.content == "a" * 256
    with pytest.raises(ModelContractError, match="256"):
        FactSnapshot("fact-2", "a" * 257, "execution-1")

    fact = Fact("fact-1", "turn-1", "😀" * 256, "execution-1")
    with pytest.raises(DomainError, match="256"):
        fact.content = "😀" * 257
    assert fact.content == "😀" * 256


def test_model_request_rejects_non_target_mismatch_failure_memory() -> None:
    invalid_summary = FailureSummary(
        failure_type=FailureType.OUTCOME_UNKNOWN,
        short_reason="无法确认结果",
        evidence_ref="execution-1",
    )
    with pytest.raises(ModelContractError, match="target_mismatch"):
        make_request(failure_summaries=(invalid_summary,))


def test_model_request_requires_plan_intent_and_forbids_intent_on_intent_generation() -> None:
    request = make_request()
    with pytest.raises(ModelContractError, match="plan_generation requires"):
        replace(
            request,
            request_type=ModelRequestType.PLAN_GENERATION,
        )
    with pytest.raises(ModelContractError, match="must not include"):
        replace(
            request,
            intent=IntentContext("intent-1", "错误上下文"),
        )


def test_raw_empty_response_is_not_corrected() -> None:
    adapter = FakeCodexAdapter([None])
    result = coordinator(adapter).invoke(make_request(), FakeCancellationToken())

    assert result == ModelError(
        kind=ModelErrorKind.EMPTY_RESPONSE,
        short_reason="synthetic provider returned no content",
    )
    assert len(adapter.invocations) == 1


def test_structured_empty_response_is_corrected_once_with_fresh_id() -> None:
    adapter = FakeCodexAdapter(
        [
            {"event": "completed", "payload": {"intents": []}},
            {"event": "completed", "payload": {"intents": [{"description": "改正后"}]}},
        ]
    )
    result = coordinator(adapter).invoke(make_request(), FakeCancellationToken())

    assert isinstance(result, IntentGenerationResponse)
    assert result.candidates[0].description == "改正后"
    assert len(adapter.invocations) == 2
    original, correction = (call.request for call in adapter.invocations)
    assert correction.correction_of_model_request_id == original.model_request_id
    assert correction.short_reason == "intent candidates must not be empty"
    assert correction.model_request_id != original.model_request_id
    UUID(correction.model_request_id)


@pytest.mark.parametrize(
    ("responses", "expected_kind"),
    [
        (
            [
                {"event": "completed", "payload": {"intents": "not an array"}},
                {"event": "completed", "payload": {"intents": "still invalid"}},
            ],
            ModelErrorKind.INVALID_RESPONSE,
        ),
        (
            [
                {"event": "completed", "payload": {"intents": []}},
                {"event": "completed", "payload": {"intents": []}},
            ],
            ModelErrorKind.EMPTY_RESPONSE,
        ),
    ],
)
def test_corrected_structural_failure_ends_without_a_second_correction(
    responses: list[object], expected_kind: ModelErrorKind
) -> None:
    adapter = FakeCodexAdapter(responses)
    result = coordinator(adapter).invoke(make_request(), FakeCancellationToken())

    assert isinstance(result, ModelError)
    assert result.kind is expected_kind
    assert len(adapter.invocations) == 2


@pytest.mark.parametrize(
    ("adapter", "raw", "expected_kind"),
    [
        (
            FakeCodexAdapter,
            {"event": "error", "code": "authentication_failed", "message": "denied"},
            ModelErrorKind.AUTHENTICATION_FAILED,
        ),
        (
            FakeClaudeCodeAdapter,
            {"type": "error", "error_type": "timed_out", "message": "slow"},
            ModelErrorKind.TIMED_OUT,
        ),
        (
            FakeDeepSeekAdapter,
            {"error": {"code": "refused", "message": "declined"}},
            ModelErrorKind.REFUSED,
        ),
    ],
)
def test_provider_errors_are_not_corrected(
    adapter: object, raw: object, expected_kind: ModelErrorKind
) -> None:
    fake = adapter([raw])  # type: ignore[operator]
    result = coordinator(fake).invoke(make_request(), FakeCancellationToken())

    assert isinstance(result, ModelError)
    assert result.kind is expected_kind
    assert len(fake.invocations) == 1


def test_deadline_is_rechecked_before_correction() -> None:
    adapter = FakeCodexAdapter(
        [
            {"event": "completed", "payload": {"intents": []}},
            {"event": "completed", "payload": {"intents": [{"description": "不会被使用"}]}},
        ]
    )
    deadline = FakeDeadline([900, 12.5])
    result = coordinator(adapter, deadline).invoke(make_request(), FakeCancellationToken())

    assert isinstance(result, IntentGenerationResponse)
    assert [call.timeout_seconds for call in adapter.invocations] == [600, 12.5]
    assert deadline.calls == 2


def test_expired_deadline_does_not_invoke_adapter() -> None:
    adapter = FakeCodexAdapter(
        [{"event": "completed", "payload": {"intents": [{"description": "unused"}]}}]
    )
    result = coordinator(adapter, FakeDeadline([0])).invoke(
        make_request(), FakeCancellationToken()
    )

    assert isinstance(result, ModelError)
    assert result.kind is ModelErrorKind.TIMED_OUT
    assert adapter.invocations == []


def test_cancelled_request_wins_over_expired_deadline_without_invocation() -> None:
    adapter = FakeCodexAdapter([])
    result = coordinator(adapter, FakeDeadline([0])).invoke(
        make_request(), FakeCancellationToken(cancelled=True)
    )

    assert isinstance(result, ModelError)
    assert result.kind is ModelErrorKind.CANCELLED
    assert adapter.invocations == []


def test_cancellation_before_correction_stops_second_adapter_call() -> None:
    token = FakeCancellationToken()

    @dataclass
    class CancellingDeadline(FakeDeadline):
        def remaining_seconds(self) -> float:
            value = super().remaining_seconds()
            if self.calls == 2:
                token.cancelled = True
            return value

    adapter = FakeCodexAdapter(
        [
            {"event": "completed", "payload": {"intents": []}},
            {"event": "completed", "payload": {"intents": [{"description": "unused"}]}},
        ]
    )
    result = coordinator(adapter, CancellingDeadline([60, 60])).invoke(
        make_request(), token
    )

    assert isinstance(result, ModelError)
    assert result.kind is ModelErrorKind.CANCELLED
    assert len(adapter.invocations) == 1


def test_fake_adapter_observes_cancellation_before_and_during_invocation() -> None:
    adapter = FakeCodexAdapter(
        [{"event": "completed", "payload": {"intents": [{"description": "unused"}]}}]
    )
    before = adapter.invoke(make_request(), 10, FakeCancellationToken(cancelled=True))
    assert isinstance(before, ModelError)
    assert before.kind is ModelErrorKind.CANCELLED
    assert adapter.invocations == []

    token = FakeCancellationToken()

    def cancel_during(*_args: object) -> object:
        token.cancelled = True
        return {"event": "completed", "payload": {"intents": [{"description": "unused"}]}}

    adapter = FakeCodexAdapter([cancel_during])
    during = adapter.invoke(make_request(), 10, token)
    assert isinstance(during, ModelError)
    assert during.kind is ModelErrorKind.CANCELLED
    assert len(adapter.invocations) == 1


def test_model_error_evidence_must_belong_to_current_task_run() -> None:
    adapter = FakeCodexAdapter([])

    @dataclass
    class ErrorAdapter:
        error: ModelError
        calls: int = 0

        def invoke(self, *_args: object) -> ModelError:
            self.calls += 1
            return self.error

    invalid_error = ErrorAdapter(
        ModelError(
            kind=ModelErrorKind.PROVIDER_FAILURE,
            short_reason="provider log available",
            evidence_ref="other-task-record",
        )
    )
    invalid = ModelInvocationCoordinator(
        invalid_error,
        FakeDeadline([60]),
        FakeErrorEvidence(),
    ).invoke(make_request(), FakeCancellationToken())
    assert invalid == ModelError(
        kind=ModelErrorKind.INVALID_RESPONSE,
        short_reason="model error evidence_ref is invalid for the current TaskRun",
    )
    assert invalid.to_dict() == {
        "kind": "invalid_response",
        "short_reason": "model error evidence_ref is invalid for the current TaskRun",
    }
    assert invalid_error.calls == 1

    valid_error = ErrorAdapter(
        ModelError(
            kind=ModelErrorKind.PROVIDER_FAILURE,
            short_reason="provider log available",
            evidence_ref="model-record-1",
        )
    )
    evidence = FakeErrorEvidence({("task-run-1", "model-record-1")})
    valid = ModelInvocationCoordinator(valid_error, FakeDeadline([60]), evidence).invoke(
        make_request(), FakeCancellationToken()
    )
    assert valid == valid_error.error
