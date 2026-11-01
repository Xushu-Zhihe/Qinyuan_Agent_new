"""A connect that never completed is re-dispatched; a connected failure is not."""

from __future__ import annotations

from agent_adapters.deepseek_roles import MAX_CONNECT_ATTEMPTS, _invoke_with_connect_retry
from agent_core.modeling import ModelError, ModelErrorKind


class Transport:
    """Each step is (result, diagnostic update) exactly as _invoke_payload leaves it."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0

    def _invoke_payload(self, payload, timeout_seconds, token, diagnostic=None):
        self.calls += 1
        result, update = self.steps.pop(0)
        diagnostic.update(update)
        return result


def never_connected(reason="model_connect_timeout", *, confirmed=True):
    return (ModelError(ModelErrorKind.TIMED_OUT, reason),
            {"connected_seconds": None, "worker_exit_confirmed": confirmed})


def connected_failure(reason, kind=ModelErrorKind.PROVIDER_FAILURE):
    return (ModelError(kind, reason), {"connected_seconds": 0.6, "worker_exit_confirmed": True})


ANSWER = ({"payload": "answer"}, {"connected_seconds": 0.4, "worker_exit_confirmed": True})


def test_never_connected_is_redispatched_until_it_succeeds():
    transport = Transport([never_connected(), never_connected(), ANSWER])
    result, attempts = _invoke_with_connect_retry(transport, {}, 30, object(), {})
    assert result == {"payload": "answer"} and attempts == 3 and transport.calls == 3


def test_http_transport_failure_without_a_connection_is_redispatched():
    transport = Transport([never_connected("http_transport_failure"), ANSWER])
    result, attempts = _invoke_with_connect_retry(transport, {}, 30, object(), {})
    assert result == {"payload": "answer"} and attempts == 2


def test_retries_are_bounded_by_max_connect_attempts():
    transport = Transport([never_connected() for _ in range(MAX_CONNECT_ATTEMPTS + 2)])
    result, attempts = _invoke_with_connect_retry(transport, {}, 30, object(), {})
    assert isinstance(result, ModelError) and result.short_reason == "model_connect_timeout"
    assert attempts == MAX_CONNECT_ATTEMPTS == transport.calls


def test_a_connection_that_came_up_is_never_redispatched():
    transport = Transport([connected_failure("http_transport_failure"), ANSWER])
    result, attempts = _invoke_with_connect_retry(transport, {}, 30, object(), {})
    assert isinstance(result, ModelError) and attempts == 1 and transport.calls == 1


def test_a_non_connect_timeout_keeps_one_attempt():
    transport = Transport([connected_failure("model_idle_timeout", ModelErrorKind.TIMED_OUT), ANSWER])
    result, attempts = _invoke_with_connect_retry(transport, {}, 30, object(), {})
    assert isinstance(result, ModelError) and attempts == 1 and transport.calls == 1


def test_an_unconfirmed_child_is_never_redispatched():
    transport = Transport([never_connected(confirmed=False), ANSWER])
    result, attempts = _invoke_with_connect_retry(transport, {}, 30, object(), {})
    assert isinstance(result, ModelError) and attempts == 1 and transport.calls == 1
