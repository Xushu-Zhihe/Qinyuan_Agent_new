"""T6 model input and transport remain separate from the T5 channel."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, replace
from uuid import uuid4

import pytest

from agent_adapters import deepseek_model
from agent_adapters.deepseek_model import (
    DeepSeekModelAdapter,
    DeepSeekSettings,
    DeepSeekT6ActionAdapter,
    t6_output_tokens_from_env,
)
from agent_core.contracts import Target
from agent_core.modeling import FactSnapshot, ModelErrorKind
from agent_core.pending_candidates import PendingCandidateContext
from agent_core.actions import (
    ProbeAction,
    T6ActionError,
    T6ActionValidationCode,
    T6ActionValidationFailure,
    parse_t6_action,
)
from agent_core.action_model_request import T6ActionModelRequest


@dataclass
class Token:
    cancelled: bool = False

    @property
    def is_cancelled(self):
        return self.cancelled


def request():
    return T6ActionModelRequest(
        str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()),
        "inspect the challenge", Target.from_dict({
            "attachments": [{"kind": "local_path", "path": "challenge.bin"}],
        }),
        "understand the binary", (), (), (), PendingCandidateContext((), False),
    )


def action_worker(pipe, settings, payload, timeout):
    assert payload["max_tokens"] == 8192
    assert "tools" not in payload
    prompt = payload["messages"][0]["content"]
    for action in ("probe", "abandon_intent", "candidate_flag"):
        assert f'"kind":"{action}"' in prompt
    assert '"kind":"complete_intent"' not in prompt
    assert "do not propose complete_intent" in prompt
    assert '"working_directory":"/workspace/work"' in prompt
    assert "/workspace/work/challenge.bin" in prompt
    assert "do not prepend input/" in prompt
    assert '"step_index":0' in prompt
    context = json.loads(payload["messages"][1]["content"])
    assert context["pending_candidates"] == {"entries": [], "history_clipped": False}
    assert context["intent_verified_fact_ids"] == []
    assert context["latest_verifiable_plan_fact_ids"] == []
    if context["correction_of_model_request_id"]:
        assert "previous action failed structure validation" in prompt
    pipe.send({"connected": True})
    pipe.send({"payload": {"action": {
        "kind": "probe", "script": "printf 'ok'", "working_directory": "/workspace/work",
        "timeout_seconds": 30, "conditions": [],
    }}})


def slow_worker(pipe, settings, payload, timeout):
    pipe.send({"connected": True})
    time.sleep(5)


def test_t6_request_and_adapter_produce_one_strict_action():
    item = request()
    adapter = DeepSeekT6ActionAdapter(
        DeepSeekSettings("deepseek-test", "secret-value"), worker=action_worker,
    )
    raw = adapter.invoke(item, 5, Token())
    action = parse_t6_action(raw)
    assert isinstance(action, ProbeAction)
    assert action.script == "printf 'ok'"
    assert action.conditions == ()
    assert item.context_payload()["target"] == {
        "endpoints": [], "attachments": [{"kind": "local_path", "path": "challenge.bin"}],
    }
    corrected = replace(
        item, correction_of_model_request_id=item.model_request_id,
        short_reason="invalid_t6_action_structure",
    )
    assert isinstance(parse_t6_action(adapter.invoke(corrected, 5, Token())), ProbeAction)


def test_t6_action_prompt_matches_parser_and_correction(monkeypatch):
    item = request()
    adapter = DeepSeekT6ActionAdapter(DeepSeekSettings("deepseek-test", "secret-value"))
    prompts = []

    def capture(payload, timeout, token):
        prompts.append(payload["messages"][0]["content"])
        return {"action": {
            "kind": "probe", "script": "printf ok", "working_directory": "/workspace/work",
            "timeout_seconds": 30, "conditions": [],
        }}

    monkeypatch.setattr(adapter._transport, "_invoke_payload", capture)
    assert isinstance(parse_t6_action(adapter.invoke(item, 5, Token())), ProbeAction)
    corrected = replace(
        item, correction_of_model_request_id=item.model_request_id,
        short_reason="invalid_t6_action_structure",
    )
    assert isinstance(parse_t6_action(adapter.invoke(corrected, 5, Token())), ProbeAction)
    fact_id = str(uuid4())
    with_fact = replace(
        item,
        verified_facts=(FactSnapshot(fact_id, "verified observation", "execution"),),
        intent_verified_fact_ids=(fact_id,),
        latest_verifiable_plan_fact_ids=(fact_id,),
    )
    assert isinstance(parse_t6_action(adapter.invoke(with_fact, 5, Token())), ProbeAction)
    for prompt in prompts:
        for action in ("probe", "abandon_intent", "candidate_flag"):
            assert f'"kind":"{action}"' in prompt
        assert '"working_directory":"/workspace/work"' in prompt
        assert "/workspace/work/challenge.bin" in prompt
        assert "do not prepend input/" in prompt
        assert '"step_index":0' in prompt
    assert all('"kind":"complete_intent"' not in prompt for prompt in prompts[:2])
    assert all("do not propose complete_intent" in prompt for prompt in prompts[:2])
    assert '"kind":"complete_intent"' in prompts[2]
    assert fact_id in prompts[2]
    assert "include at least one ID from latest_verifiable_plan_fact_ids" in prompts[2]
    assert "previous action failed structure validation" not in prompts[0]
    assert "previous action failed structure validation" in prompts[1]


def test_t6_adapter_does_not_accept_unwrapped_or_invalid_actions():
    item = request()
    adapter = DeepSeekT6ActionAdapter(
        DeepSeekSettings("deepseek-test", "secret-value"), worker=action_worker,
    )
    assert adapter.output_tokens == 8192
    assert DeepSeekT6ActionAdapter(adapter.settings, output_tokens=4096).output_tokens == 4096
    with pytest.raises(ValueError, match="invalid_t6_output_tokens"):
        DeepSeekT6ActionAdapter(adapter.settings, output_tokens=0)
    with pytest.raises(TypeError, match="t6_action_request_required"):
        adapter.invoke(object(), 1, Token())
    with pytest.raises(T6ActionError):
        parse_t6_action({"kind": "probe", "script": "printf 'ok'", "unexpected": 1})
    with pytest.raises(T6ActionError) as error:
        parse_t6_action({"kind": "unknown"})
    assert error.value.validation_code is T6ActionValidationCode.ACTION_KIND_INVALID
    with pytest.raises(T6ActionError) as error:
        parse_t6_action({})
    assert error.value.validation_code is T6ActionValidationCode.ACTION_FIELD_MISSING
    with pytest.raises(T6ActionError) as error:
        parse_t6_action({"kind": "probe", "unexpected": True})
    assert error.value.validation_code is T6ActionValidationCode.ACTION_FIELD_UNEXPECTED
    assert item.context_payload()["goal"] == "inspect the challenge"


@pytest.mark.parametrize(
    "code",
    [
        T6ActionValidationCode.PROVIDER_JSON_INVALID,
        T6ActionValidationCode.PROVIDER_ENVELOPE_INVALID,
        T6ActionValidationCode.PROVIDER_CHOICES_INVALID,
        T6ActionValidationCode.PROVIDER_CONTENT_INVALID,
    ],
)
def test_t6_provider_diagnostics_are_fixed_and_unwrapped_action_is_checked(code):
    item = request()
    adapter = DeepSeekT6ActionAdapter(DeepSeekSettings("deepseek-test", "secret-value"))

    def provider_failure(payload, timeout, token):
        return T6ActionValidationFailure(code)

    adapter._transport._invoke_payload = provider_failure
    result = adapter.invoke(item, 5, Token())
    assert result == T6ActionValidationFailure(code)

    def invalid_envelope(payload, timeout, token):
        return {"unexpected": {}}

    adapter._transport._invoke_payload = invalid_envelope
    result = adapter.invoke(item, 5, Token())
    assert result == T6ActionValidationFailure(
        T6ActionValidationCode.ACTION_ENVELOPE_INVALID
    )


def test_t6_output_token_setting_is_bounded(monkeypatch):
    monkeypatch.delenv("QINYUAN_T6_MAX_OUTPUT_TOKENS", raising=False)
    assert t6_output_tokens_from_env() == 8192
    monkeypatch.setenv("QINYUAN_T6_MAX_OUTPUT_TOKENS", "4096")
    assert t6_output_tokens_from_env() == 4096
    for value in ("0", "32769", "-1", "8.5", " 8192", ""):
        monkeypatch.setenv("QINYUAN_T6_MAX_OUTPUT_TOKENS", value)
        with pytest.raises(ValueError, match="invalid_t6_output_tokens"):
            t6_output_tokens_from_env()


@pytest.mark.parametrize("cancel", [False, True])
def test_t6_model_wait_terminates_on_timeout_or_cancel(cancel):
    adapter = DeepSeekT6ActionAdapter(
        DeepSeekSettings("deepseek-test", "secret-value", timeout_seconds=0.5),
        worker=slow_worker,
    )
    token = Token()
    timer = threading.Timer(0.1, lambda: setattr(token, "cancelled", True))
    if cancel:
        timer.start()
    try:
        result = adapter.invoke(request(), 0.5, token)
        assert result.kind is (
            ModelErrorKind.CANCELLED if cancel else ModelErrorKind.TIMED_OUT
        )
    finally:
        if cancel:
            timer.join()


def test_t6_http_response_limit_is_separate_from_t5(monkeypatch):
    content = json.dumps({"action": {"kind": "candidate_flag", "value": "x" * 70000}})
    body = json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "content": content,
    }}]}).encode()

    class FakeHTTP:
        sock = None
        status = 200

        def __init__(self, host, timeout):
            self.sock = self

        def connect(self):
            pass

        def settimeout(self, timeout):
            pass

        def request(self, method, path, **kwargs):
            assert method == "POST" and path == "/chat/completions"

        def getresponse(self):
            return self

        def read(self, limit):
            return body[:limit]

        def close(self):
            pass

    class Pipe:
        def __init__(self):
            self.values = []

        def send(self, value):
            self.values.append(value)

        def close(self):
            pass

    monkeypatch.setattr(deepseek_model.http.client, "HTTPSConnection", FakeHTTP)
    monkeypatch.setattr(deepseek_model.signal, "signal", lambda *args: None)
    settings = DeepSeekSettings("deepseek-test", "secret-value")
    t6 = Pipe()
    deepseek_model._t6_http_worker(t6, settings, {}, 5)
    assert t6.values[-1]["payload"]["action"]["value"] == "x" * 70000
    t5 = Pipe()
    deepseek_model._http_worker(t5, settings, {}, 5)
    assert t5.values[-1] == {
        "error": "invalid_response", "reason": "response_rejected",
    }
    assert DeepSeekModelAdapter(settings).settings is settings
