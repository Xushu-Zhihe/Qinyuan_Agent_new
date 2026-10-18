from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_adapters.web.model_adapter import (
    D7_INNER_PROMPT_REF,
    D7_OUTER_PROMPT_REF,
    D7DeepSeekModelAdapter,
    build_d7_model_adapters,
    build_d7_model_payload,
    invoke_d7_web_role,
)
from agent_adapters.deepseek_model import DeepSeekSettings
from agent_core.dual_llm_modeling import D3ModelRequest, D3OuterContext
from agent_core.modeling import ModelError, ModelErrorKind
from agent_core.turn_contracts import D1RoleConfig


@dataclass(frozen=True)
class _Context:
    turn_target: str
    fixed_script: str


class FakePayloadTransport:
    def __init__(self, settings: DeepSeekSettings) -> None:
        self.settings = settings
        self.calls: list[tuple[dict[str, object], float, object]] = []
        self.response: object = {"fake": "response"}

    def _invoke_payload(self, payload, timeout_seconds, cancellation_token):
        self.calls.append((payload, timeout_seconds, cancellation_token))
        return self.response


def _role(role: str, model: str, prompt: str, parameters=()) -> D1RoleConfig:
    return D1RoleConfig(role, "deepseek", model, prompt, tuple(parameters))


def _outer_request(config: D1RoleConfig) -> D3ModelRequest:
    return D3ModelRequest(
        "request-1",
        "task-run-1",
        4,
        "outer_planning",
        config.role_config_ref,
        "resource-1",
        "outer_turn",
        D3OuterContext("inspect the authorized attachment", (), None, None),
    )


def _inner_request(config: D1RoleConfig) -> D3ModelRequest:
    return D3ModelRequest(
        "request-2",
        "task-run-1",
        5,
        "inner_action",
        config.role_config_ref,
        "resource-2",
        "inner_size",
        _Context("check the attachment", "fixed read-only operation"),
    )


def _patch_dotenv(monkeypatch, key: str = "file-only-secret") -> tuple[Path, list]:
    env_file = Path("selected-test.env")
    calls = []
    values = {
        "QINYUAN_LLM_BACKEND": "openai",
        "OPENAI_API_KEY": key,
        "OPENAI_BASE_URL": "https://api.deepseek.com",
        "QINYUAN_LLM_TIMEOUT_SECONDS": "12",
    }

    def dotenv_values(path, *, interpolate):
        calls.append((Path(path), interpolate))
        return values

    monkeypatch.setattr("dotenv.dotenv_values", dotenv_values)
    return env_file, calls


def test_outer_and_inner_roles_use_frozen_model_ids_and_registered_prompts(monkeypatch):
    outer = _role(
        "outer_planning",
        "outer-model-v3",
        D7_OUTER_PROMPT_REF,
        (("temperature", 0.2), ("top_p", 0.8), ("max_tokens", 4096)),
    )
    inner = _role(
        "inner_action",
        "inner-model-v3",
        D7_INNER_PROMPT_REF,
        (("max_tokens", 8192),),
    )
    transports: list[FakePayloadTransport] = []

    def factory(settings):
        transport = FakePayloadTransport(settings)
        transports.append(transport)
        return transport

    env_file, dotenv_calls = _patch_dotenv(monkeypatch)
    adapters = build_d7_model_adapters(
        outer, inner, env_file, transport_factory=factory
    )
    assert adapters.outer.role_config == outer
    assert adapters.inner.role_config == inner
    assert adapters.outer.settings.model == "outer-model-v3"
    assert adapters.inner.settings.model == "inner-model-v3"
    assert adapters.outer.settings.api_key == adapters.inner.settings.api_key

    adapters.outer.invoke(_outer_request(outer), 3, object())
    adapters.inner.invoke(_inner_request(inner), 4, object())
    outer_payload, _, _ = transports[0].calls[0]
    inner_payload, _, _ = transports[1].calls[0]
    assert outer_payload["model"] == "outer-model-v3"
    assert outer_payload["max_tokens"] == 4096
    assert outer_payload["temperature"] == 0.2
    assert outer_payload["top_p"] == 0.8
    assert inner_payload["model"] == "inner-model-v3"
    assert inner_payload["max_tokens"] == 8192
    assert "D3ModelRequest" not in json.dumps(outer_payload)
    assert "fixed read-only operation" in json.dumps(inner_payload)
    assert dotenv_calls == [(env_file, False), (env_file, False)]


def test_api_key_is_loaded_from_selected_env_file_not_process_environment(
    monkeypatch,
):
    secret = "file-secret-do-not-disclose"
    role = _role("outer_planning", "frozen-model", D7_OUTER_PROMPT_REF)
    transport_values = []

    def factory(settings):
        transport_values.append(settings)
        return FakePayloadTransport(settings)

    monkeypatch.setenv("OPENAI_API_KEY", "process-environment-secret")
    monkeypatch.setenv("QINYUAN_RUNTIME_API_KEY", "workbench-runtime-secret")
    env_file, dotenv_calls = _patch_dotenv(monkeypatch, secret)
    adapters = build_d7_model_adapters(
        role,
        _role("inner_action", "frozen-model", D7_INNER_PROMPT_REF),
        env_file,
        transport_factory=factory,
    )
    assert transport_values[0].api_key == secret
    assert "process-environment-secret" not in repr(adapters)
    assert "workbench-runtime-secret" not in repr(adapters)
    assert secret not in repr(adapters)

    adapter = adapters.outer
    payload = build_d7_model_payload(_outer_request(role), role, adapter.settings)
    assert secret not in json.dumps(payload)
    assert dotenv_calls == [(env_file, False), (env_file, False)]


@pytest.mark.parametrize(
    "parameters",
    [
        (("api_key", "secret"),),
        (("temperature", True),),
        (("temperature", float("nan")),),
        (("temperature", 2.1),),
        (("top_p", -0.1),),
        (("top_p", 1.1),),
        (("max_tokens", True),),
        (("max_tokens", 0),),
        (("max_tokens", 32769),),
    ],
)
def test_only_typed_bounded_model_parameters_are_accepted(parameters):
    with pytest.raises(ValueError):
        role = _role("inner_action", "model-v3", D7_INNER_PROMPT_REF, parameters)
        D7DeepSeekModelAdapter(role, DeepSeekSettings("model-v3", "test-key"))


def test_adapter_rejects_role_config_mismatch_before_transport():
    config = _role("inner_action", "inner-model", D7_INNER_PROMPT_REF)
    transport = FakePayloadTransport(DeepSeekSettings("inner-model", "test-key"))
    adapter = D7DeepSeekModelAdapter(config, transport.settings, transport=transport)
    result = adapter.invoke(_outer_request(config), 1, object())
    assert isinstance(result, ModelError)
    assert result.kind is ModelErrorKind.INVALID_RESPONSE
    assert transport.calls == []


def test_unregistered_prompt_or_provider_is_rejected():
    with pytest.raises(ValueError, match="prompt_template_unregistered"):
        D7DeepSeekModelAdapter(
            _role("outer_planning", "model", "outer-v2"),
            DeepSeekSettings("model", "test-key"),
        )
    with pytest.raises(ValueError, match="provider_unsupported"):
        D7DeepSeekModelAdapter(
            D1RoleConfig("outer_planning", "other", "model", D7_OUTER_PROMPT_REF),
            DeepSeekSettings("model", "test-key"),
        )


def test_web_outer_role_returns_ordered_intents_using_outer_prompt():
    config = _role("outer_planning", "outer-web-model", D7_OUTER_PROMPT_REF)
    settings = DeepSeekSettings("outer-web-model", "test-key")
    transport = FakePayloadTransport(settings)
    transport.response = {
        "candidates": [
            {"description": "inspect the response headers", "intent_type": "web"},
            {"description": "inspect the response body", "intent_type": None},
        ]
    }
    token = object()

    result = invoke_d7_web_role(
        config,
        settings,
        "web_intents",
        {
            "goal": "find the challenge answer",
            "authorized_origin": "http://example.test:28989",
        },
        8,
        token,
        transport=transport,
    )

    assert result == transport.response
    payload, timeout, passed_token = transport.calls[0]
    assert payload["model"] == "outer-web-model"
    assert timeout == 8
    assert passed_token is token
    assert "single HTTP request action" not in payload["messages"][0]["content"]
    assert "ordered independent Intent candidates" in payload["messages"][0]["content"]
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)
def test_web_inner_accepts_one_authorized_http_action(method):
    config = _role("inner_action", "inner-web-model", D7_INNER_PROMPT_REF)
    settings = DeepSeekSettings("inner-web-model", "test-key")
    transport = FakePayloadTransport(settings)
    transport.response = {
        "action": {
            "kind": "http_request",
            "method": method,
            "path_query": "/index.php?view=source",
            "headers": {"Accept": "text/html"},
            "body": "payload",
        }
    }

    result = invoke_d7_web_role(
        config,
        settings,
        "http_action",
        {"intent": "inspect source"},
        5,
        object(),
        transport=transport,
    )

    assert result == transport.response
    payload = transport.calls[0][0]
    assert payload["model"] == "inner-web-model"
    assert "exactly one HTTP request action" in payload["messages"][0]["content"]
    assert "write agent state" in payload["messages"][0]["content"]


@pytest.mark.parametrize(
    "action",
    [
        {
            "kind": "http_request",
            "method": "TRACE",
            "path_query": "/",
            "headers": {},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "CONNECT",
            "path_query": "/",
            "headers": {},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "GET",
            "path_query": "http://other.test/",
            "headers": {},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "GET",
            "path_query": "//other.test/",
            "headers": {},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "GET",
            "path_query": "/path#fragment",
            "headers": {},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "GET",
            "path_query": "/",
            "headers": {"Cookie": "session=secret"},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "GET",
            "path_query": "/",
            "headers": {"X-Test": "ok\r\nHost: evil"},
            "body": None,
        },
        {
            "kind": "http_request",
            "method": "POST",
            "path_query": "/",
            "headers": {},
            "body": "x" * (64 * 1024 + 1),
        },
        {
            "kind": "http_request",
            "method": "GET",
            "path_query": "/",
            "headers": {},
            "body": None,
            "extra": True,
        },
    ],
)
def test_web_http_action_rejects_out_of_contract_response(action):
    config = _role("inner_action", "inner-web-model", D7_INNER_PROMPT_REF)
    settings = DeepSeekSettings("inner-web-model", "test-key")
    transport = FakePayloadTransport(settings)
    transport.response = {"action": action}

    result = invoke_d7_web_role(
        config,
        settings,
        "http_action",
        {"intent": "inspect"},
        3,
        object(),
        transport=transport,
    )

    assert isinstance(result, ModelError)
    assert result.kind is ModelErrorKind.INVALID_RESPONSE


def test_web_candidate_is_single_unverified_value_bound_to_execution_evidence():
    config = _role("inner_action", "inner-web-model", D7_INNER_PROMPT_REF)
    settings = DeepSeekSettings("inner-web-model", "test-key")
    transport = FakePayloadTransport(settings)
    transport.response = {
        "action": {
            "kind": "candidate_flag",
            "value": "flag{candidate}",
            "execution_id": "6e288214-cf4a-4a82-a23d-6e570403fe0c",
            "evidence_version": "a" * 64,
            "source_ref": "response-evidence-v1",
        }
    }

    result = invoke_d7_web_role(
        config,
        settings,
        "answer_candidate",
        {"execution_id": "6e288214-cf4a-4a82-a23d-6e570403fe0c"},
        3,
        object(),
        transport=transport,
    )

    assert result == transport.response
    assert (
        "never claim acceptance"
        in transport.calls[0][0]["messages"][0]["content"].lower()
    )


def test_web_context_over_64_kib_is_rejected_before_transport():
    config = _role("inner_action", "inner-web-model", D7_INNER_PROMPT_REF)
    settings = DeepSeekSettings("inner-web-model", "test-key")
    transport = FakePayloadTransport(settings)

    result = invoke_d7_web_role(
        config,
        settings,
        "http_action",
        {"evidence": "x" * (64 * 1024)},
        3,
        object(),
        transport=transport,
    )

    assert isinstance(result, ModelError)
    assert result.kind is ModelErrorKind.INVALID_RESPONSE
    assert transport.calls == []


def test_web_transport_model_error_is_preserved():
    config = _role("inner_action", "inner-web-model", D7_INNER_PROMPT_REF)
    settings = DeepSeekSettings("inner-web-model", "test-key")
    transport = FakePayloadTransport(settings)
    expected = ModelError(ModelErrorKind.TIMED_OUT, "fake_timeout")
    transport.response = expected

    result = invoke_d7_web_role(
        config,
        settings,
        "http_action",
        {"intent": "inspect"},
        3,
        object(),
        transport=transport,
    )

    assert result is expected
