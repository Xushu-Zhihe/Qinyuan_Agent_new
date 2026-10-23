from __future__ import annotations

import base64
import contextlib
import io
import json
import socket
import ssl
import struct
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_adapters.web.http_adapter import D7HttpError
from agent_adapters.web.sandbox_transport import D7SandboxHttpTransport
from agent_core.control_store import TargetNetworkBinding
from agent_core.web_contracts import D7WebAuthorization, D7WebRunConfig
from agent_core.operation_accounting import D4BudgetPolicy
from agent_core.turn_contracts import D1RoleConfig
from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.http_runtime import _HTTP_WORKER
from Sandbox.settings import SandboxSettings
from Sandbox.task_run_sandbox import _Binding

ORIGIN = "http://node4.anna.nssctf.cn:28989"


def _config(task_run_id: str):
    authorization = D7WebAuthorization(
        str(uuid4()),
        ORIGIN,
        ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"),
    )
    return D7WebRunConfig(
        task_run_id=task_run_id,
        request_id=str(uuid4()),
        endpoint=ORIGIN,
        authorization=authorization,
        outer_role=D1RoleConfig(
            "outer_planning", "deepseek", "outer-model", "qinyuan.d7.outer.v1"
        ),
        inner_role=D1RoleConfig(
            "inner_action", "deepseek", "inner-model", "qinyuan.d7.inner.v1"
        ),
        d4_budget=D4BudgetPolicy(),
        model_transport_config_ref="sha256:" + "a" * 64,
    )


def _network_binding():
    return TargetNetworkBinding(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        ORIGIN,
    )


def _frame(stream: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream, len(payload)) + payload


class _RawSocket:
    def __init__(self, response: bytes, *, read_error: Exception | None = None):
        self.response = bytearray(response)
        self.read_error = read_error
        self.sent = bytearray()
        self.shutdowns: list[int] = []

    def sendall(self, value: bytes) -> None:
        self.sent.extend(value)

    def shutdown(self, how: int) -> None:
        self.shutdowns.append(how)

    def recv(self, count: int) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        if not self.response:
            return b""
        result = bytes(self.response[:count])
        del self.response[:count]
        return result

    def settimeout(self, _value: float) -> None:
        pass

    def close(self) -> None:
        pass


class _ExecApi:
    def __init__(self, output: bytes, *, read_error: Exception | None = None):
        self.output = output
        self.read_error = read_error
        self.create_calls: list[dict[str, object]] = []
        self.start_calls: list[str] = []
        self.raw: _RawSocket | None = None

    def exec_create(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"Id": "d7-http-exec"}

    def exec_start(self, exec_id, **_kwargs):
        self.start_calls.append(exec_id)
        self.raw = _RawSocket(self.output, read_error=self.read_error)
        return SimpleNamespace(_sock=self.raw, close=self.raw.close)

    def exec_inspect(self, _exec_id):
        return {"Running": False, "ExitCode": 0}


class _TargetNetwork:
    def __init__(self):
        self.bind_calls = []

    def bind(self, task_run_id, binding, *, terminal):
        self.bind_calls.append((task_run_id, binding, terminal))


class _Runtime:
    def __init__(self, api):
        self.settings = SandboxSettings()
        self.client = SimpleNamespace(api=api)


def _sandbox(task_run_id: str, api: _ExecApi):
    sandbox_id = "sandbox-1"
    sandbox = T6TaskRunSandbox(
        _Runtime(api), private_network="qinyuan-t6-in-" + task_run_id
    )
    sandbox._bindings[sandbox_id] = _Binding(task_run_id, sandbox_id)
    sandbox._verify_active = lambda *_args: None
    return sandbox, sandbox_id


def _worker_output(body: bytes = b"ok", headers=()):
    payload = json.dumps(
        {
            "status": 200,
            "headers": [list(pair) for pair in headers],
            "body_b64": base64.b64encode(body).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return _frame(1, payload)


@pytest.fixture(autouse=True)
def _fake_docker_frames(monkeypatch):
    import docker.utils.socket

    def read_exactly(sock, count):
        result = bytearray()
        while len(result) < count:
            chunk = sock.recv(count - len(result))
            if not chunk:
                raise OSError("socket closed")
            result.extend(chunk)
        return bytes(result)

    def next_frame_header(sock):
        try:
            frame = read_exactly(sock, 8)
        except OSError:
            return -1, -1
        return frame[0], struct.unpack(">L", frame[4:])[0]

    monkeypatch.setattr(docker.utils.socket, "read_exactly", read_exactly)
    monkeypatch.setattr(docker.utils.socket, "next_frame_header", next_frame_header)


def test_d7_http_dispatch_uses_stdin_fixed_worker_and_forwarder_binding():
    task_run_id = str(uuid4())
    config = _config(task_run_id)
    api = _ExecApi(
        _worker_output(b"<title>result</title>", (("content-type", "text/html"),))
    )
    sandbox, sandbox_id = _sandbox(task_run_id, api)
    network = _TargetNetwork()
    transport = D7SandboxHttpTransport(
        sandbox,
        sandbox_id=sandbox_id,
        run_config=config,
        network_binding=_network_binding(),
        target_network=network,
    )
    result = transport.send(
        origin=transport_origin(config.endpoint),
        method="POST",
        path_query="/search?q=one",
        headers=(("Cookie", "session=secret"), ("Content-Type", "text/plain")),
        body=b"body-secret",
        timeout_seconds=3,
        max_response_bytes=2 * 1024 * 1024,
    )

    assert result.status == 200
    assert result.body == b"<title>result</title>"
    assert network.bind_calls == [(task_run_id, _network_binding(), False)]
    call = api.create_calls[0]
    assert call["stdin"] is True
    assert call["tty"] is False
    assert call["privileged"] is False
    assert call["user"] == "10001:10001"
    assert call["environment"] == {"PATH": "/usr/bin:/bin"}
    command = call["cmd"]
    assert isinstance(command, list)
    assert "session=secret" not in " ".join(command)
    assert "body-secret" not in " ".join(command)
    assert "-c" in command and "target.qinyuan" in str(command)
    assert api.raw is not None
    sent = json.loads(api.raw.sent)
    assert sent["headers"][0] == ["Cookie", "session=secret"]
    assert base64.b64decode(sent["body_b64"]) == b"body-secret"
    assert api.raw.sent.endswith(b"\n") and not api.raw.shutdowns


def test_d7_http_authorization_mismatch_fails_before_network_or_exec():
    task_run_id = str(uuid4())
    config = _config(task_run_id)
    api = _ExecApi(_worker_output())
    sandbox, sandbox_id = _sandbox(task_run_id, api)
    other = D7WebAuthorization(
        str(uuid4()),
        ORIGIN,
        ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"),
    )
    network = _TargetNetwork()

    with pytest.raises(ValueError, match="d7_http_authorization_or_ownership_mismatch"):
        sandbox.execute_d7_http(
            sandbox_id,
            task_run_id=task_run_id,
            run_config=config,
            authorization=other,
            network_binding=_network_binding(),
            target_network=network,
            method="GET",
            path_query="/",
            headers=(),
            body=b"",
            timeout_seconds=3,
        )

    assert network.bind_calls == []
    assert api.create_calls == []
    assert api.start_calls == []


def test_d7_http_request_header_limits_reject_before_network_or_exec():
    task_run_id = str(uuid4())
    config = _config(task_run_id)
    api = _ExecApi(_worker_output())
    sandbox, sandbox_id = _sandbox(task_run_id, api)
    network = _TargetNetwork()
    transport = D7SandboxHttpTransport(
        sandbox,
        sandbox_id=sandbox_id,
        run_config=config,
        network_binding=_network_binding(),
        target_network=network,
    )

    with pytest.raises(D7HttpError, match="d7_http_dispatch_rejected"):
        transport.send(
            origin=transport_origin(config.endpoint),
            method="GET",
            path_query="/",
            headers=(("X-Large", "a" * (8 * 1024 + 1)),),
            body=b"",
            timeout_seconds=3,
            max_response_bytes=2 * 1024 * 1024,
        )

    assert network.bind_calls == []
    assert api.create_calls == []
    assert api.start_calls == []


def test_d7_http_response_header_limits_fail_closed_after_dispatch():
    task_run_id = str(uuid4())
    config = _config(task_run_id)
    headers = tuple(("content-type", "text/plain") for _ in range(101))
    api = _ExecApi(_worker_output(headers=headers))
    sandbox, sandbox_id = _sandbox(task_run_id, api)
    network = _TargetNetwork()
    transport = D7SandboxHttpTransport(
        sandbox,
        sandbox_id=sandbox_id,
        run_config=config,
        network_binding=_network_binding(),
        target_network=network,
    )

    with pytest.raises(D7HttpError, match="outcome_unknown"):
        transport.send(
            origin=transport_origin(config.endpoint),
            method="GET",
            path_query="/",
            headers=(),
            body=b"",
            timeout_seconds=3,
            max_response_bytes=2 * 1024 * 1024,
        )

    assert len(api.create_calls) == 1
    assert len(api.start_calls) == 1


@pytest.mark.parametrize("host", ["node4.anna.nssctf.cn", "10.0.1.5"])
def test_fixed_worker_sends_to_forwarder_and_does_not_follow_redirects(monkeypatch, host):
    class FakeHttpSocket:
        def __init__(self):
            self.sent = bytearray()
            self.response = io.BytesIO(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: /next\r\n"
                b"Set-Cookie: session=secret; Path=/\r\n"
                b"Content-Type: text/plain\r\n"
                b"X-Ignored: hidden\r\n"
                b"Content-Length: 2\r\n\r\n"
                b"ok"
            )

        def settimeout(self, _timeout):
            pass

        def setsockopt(self, *_args):
            pass

        def sendall(self, value):
            self.sent.extend(value)

        def makefile(self, _mode):
            return self.response

        def close(self):
            self.response.close()

    fake_socket = FakeHttpSocket()
    addresses = []

    def connect(address, *_args):
        addresses.append(address)
        return fake_socket

    monkeypatch.setattr(socket, "create_connection", connect)
    request = {
        "scheme": "http",
        "host": host,
        "port": 28989,
        "authority": f"{host}:28989",
        "method": "GET",
        "path_query": "/start",
        "headers": [["Cookie", "session=secret"]],
        "body_b64": "",
        "timeout_seconds": 2,
        "max_response_bytes": 2 * 1024 * 1024,
        "max_header_bytes": 64 * 1024,
        "max_header_count": 100,
        "max_header_value_bytes": 8 * 1024,
    }
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()+b"\n")))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(_HTTP_WORKER, {})  # noqa: S102 - execute the fixed worker under a fake socket

    response = json.loads(output.getvalue())
    assert addresses == [("target.qinyuan", 28989)]
    assert f"Host: {host}:28989\r\n".encode() in fake_socket.sent
    assert b"Cookie: session=secret\r\n" in fake_socket.sent
    assert response["status"] == 302
    assert response["headers"] == [
        ["location", "/next"],
        ["set-cookie", "session=secret; Path=/"],
        ["content-type", "text/plain"],
    ]
    assert base64.b64decode(response["body_b64"]) == b"ok"


@pytest.mark.parametrize("host", ["node4.anna.nssctf.cn", "10.0.1.5"])
def test_fixed_worker_uses_authorized_hostname_for_verified_tls_sni(monkeypatch, host):
    class FakeHttpSocket:
        def __init__(self):
            self.sent = bytearray()
            self.response = io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
            )

        def settimeout(self, _timeout):
            pass

        def setsockopt(self, *_args):
            pass

        def sendall(self, value):
            self.sent.extend(value)

        def makefile(self, _mode):
            return self.response

        def close(self):
            self.response.close()

    class FakeSSLContext:
        def __init__(self):
            self.server_names = []

        def wrap_socket(self, raw, *, server_hostname):
            self.server_names.append(server_hostname)
            return raw

    fake_socket = FakeHttpSocket()
    context = FakeSSLContext()
    addresses = []
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, *_args: addresses.append(address) or fake_socket,
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)
    request = {
        "scheme": "https",
        "host": host,
        "port": 28989,
        "authority": f"{host}:28989",
        "method": "GET",
        "path_query": "/",
        "headers": [],
        "body_b64": "",
        "timeout_seconds": 2,
        "max_response_bytes": 2 * 1024 * 1024,
        "max_header_bytes": 64 * 1024,
        "max_header_count": 100,
        "max_header_value_bytes": 8 * 1024,
    }
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()+b"\n")))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(_HTTP_WORKER, {})  # noqa: S102 - execute the fixed worker under a fake socket

    assert addresses == [("target.qinyuan", 28989)]
    assert context.server_names == [host]
    assert json.loads(output.getvalue())["status"] == 200


def test_d7_http_unknown_result_is_not_redispatched():
    task_run_id = str(uuid4())
    config = _config(task_run_id)
    api = _ExecApi(b"", read_error=OSError("stream lost"))
    sandbox, sandbox_id = _sandbox(task_run_id, api)
    network = _TargetNetwork()
    transport = D7SandboxHttpTransport(
        sandbox,
        sandbox_id=sandbox_id,
        run_config=config,
        network_binding=_network_binding(),
        target_network=network,
    )

    with pytest.raises(Exception, match="outcome_unknown"):
        transport.send(
            origin=transport_origin(config.endpoint),
            method="POST",
            path_query="/submit",
            headers=(),
            body=b"payload",
            timeout_seconds=3,
            max_response_bytes=2 * 1024 * 1024,
        )

    assert len(api.create_calls) == 1
    assert len(api.start_calls) == 1


def transport_origin(endpoint: str):
    from agent_adapters.web.http_adapter import D7HttpOrigin

    return D7HttpOrigin.parse(endpoint)
