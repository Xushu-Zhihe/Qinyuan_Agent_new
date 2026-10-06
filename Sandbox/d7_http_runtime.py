"""Bounded HTTP execution inside a TaskRun-owned target-network Sandbox."""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_core.control_store import TargetNetworkBinding
from agent_core.d7_web_contracts import (
    D7_WEB_MAX_HEADER_BYTES,
    D7_WEB_MAX_HEADER_COUNT,
    D7_WEB_MAX_HEADER_VALUE_BYTES,
    D7_WEB_MAX_REQUEST_BODY_BYTES,
    D7_WEB_MAX_RESPONSE_BODY_BYTES,
    D7_WEB_RESPONSE_HEADERS,
    D7WebAuthorization,
    D7WebRunConfig,
    canonical_web_origin,
)
from Sandbox.task_run_sandbox import TaskRunSandbox, TaskRunSandboxError

if TYPE_CHECKING:
    from Sandbox.target_network import T6TargetNetwork

MAX_D7_HTTP_REQUEST_BODY_BYTES = D7_WEB_MAX_REQUEST_BODY_BYTES
MAX_D7_HTTP_RESPONSE_BODY_BYTES = D7_WEB_MAX_RESPONSE_BODY_BYTES
MAX_D7_HTTP_HEADER_BYTES = D7_WEB_MAX_HEADER_BYTES
MAX_D7_HTTP_HEADER_COUNT = D7_WEB_MAX_HEADER_COUNT
MAX_D7_HTTP_HEADER_VALUE_BYTES = D7_WEB_MAX_HEADER_VALUE_BYTES
MAX_D7_HTTP_STDERR_BYTES = 4096
MAX_D7_HTTP_STDOUT_BYTES = 4 * 1024 * 1024
MAX_D7_HTTP_STDIN_BYTES = 256 * 1024

_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_RETURNED_RESPONSE_HEADERS = D7_WEB_RESPONSE_HEADERS

_HTTP_WORKER = r"""import base64,http.client,json,socket,ssl,sys

def fail():
    print('{"error":"d7_http_transport_failed"}',flush=True)
    raise SystemExit(1)

try:
    request=json.load(sys.stdin)
    expected={"scheme","host","port","authority","method","path_query","headers","body_b64","timeout_seconds","max_response_bytes","max_header_bytes","max_header_count","max_header_value_bytes"}
    if not isinstance(request,dict) or set(request)!=expected:
        fail()
    body=base64.b64decode(request["body_b64"],validate=True)
    if len(body)>65536 or request["max_response_bytes"]!=2097152:
        fail()
    host=request["host"]
    port=request["port"]
    timeout=request["timeout_seconds"]
    if request["scheme"]=="https":
        context=ssl.create_default_context()
        class AuthorizedHTTPSConnection(http.client.HTTPSConnection):
            def connect(self):
                sock=socket.create_connection(("target.qinyuan",self.port),self.timeout,self.source_address)
                self.sock=context.wrap_socket(sock,server_hostname=host)
        connection=AuthorizedHTTPSConnection("target.qinyuan",port,timeout=timeout,context=context)
    elif request["scheme"]=="http":
        connection=http.client.HTTPConnection("target.qinyuan",port,timeout=timeout)
    else:
        fail()
    connection.putrequest(request["method"],request["path_query"],skip_host=True,skip_accept_encoding=True)
    connection.putheader("Host",request["authority"])
    connection.putheader("Connection","close")
    connection.putheader("Accept-Encoding","identity")
    for name,value in request["headers"]:
        if name.lower()=="accept-encoding":
            if value.lower().strip()!="identity":
                fail()
            continue
        connection.putheader(name,value)
    connection.putheader("Content-Length",str(len(body)))
    connection.endheaders(body)
    response=connection.getresponse()
    response_body=response.read(request["max_response_bytes"]+1)
    if len(response_body)>request["max_response_bytes"]:
        fail()
    selected=[]
    total=0
    for name,value in response.getheaders():
        if name.lower() not in ("content-type","location","set-cookie"):
            continue
        encoded_name=name.lower().encode("ascii")
        encoded_value=value.encode("latin-1")
        if len(encoded_value)>request["max_header_value_bytes"]:
            fail()
        total+=len(encoded_name)+len(encoded_value)+4
        selected.append([name.lower(),value])
        if len(selected)>request["max_header_count"] or total>request["max_header_bytes"]:
            fail()
    result={"status":response.status,"headers":selected,"body_b64":base64.b64encode(response_body).decode("ascii")}
    print(json.dumps(result,separators=(",",":"),ensure_ascii=True),flush=True)
    connection.close()
except SystemExit:
    raise
except Exception:
    fail()
"""


@dataclass(frozen=True, slots=True)
class D7SandboxHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _fail(phase: str, task_run_id: str, sandbox_id: str, exc: Exception | None = None):
    raise TaskRunSandboxError(
        phase,
        "outcome_unknown" if exc is not None else "execution_rejected",
        task_run_id,
        (sandbox_id,),
        original_error=exc,
    ) from exc


def _unknown(
    phase: str, task_run_id: str, sandbox_id: str, exc: Exception | None = None
):
    raise TaskRunSandboxError(
        phase,
        "outcome_unknown",
        task_run_id,
        (sandbox_id,),
        original_error=exc,
    ) from exc


def _validate_header_limits(
    headers: object,
) -> tuple[tuple[tuple[str, str], ...], int]:
    if (
        not isinstance(headers, (tuple, list))
        or len(headers) > MAX_D7_HTTP_HEADER_COUNT
    ):
        raise ValueError("d7_http_headers_invalid")
    normalized: list[tuple[str, str]] = []
    total = 0
    seen: set[str] = set()
    for pair in headers:
        if (
            not isinstance(pair, (tuple, list))
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
        ):
            raise ValueError("d7_http_headers_invalid")
        name, value = pair
        lower = name.lower()
        try:
            name_bytes = name.encode("ascii", errors="strict")
            value_bytes = value.encode("latin-1", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("d7_http_header_rejected") from exc
        if (
            not name
            or any(not (char.isalnum() or char in "!#$%&'*+-.^_`|~") for char in name)
            or lower in _FORBIDDEN_REQUEST_HEADERS
            or lower in seen
            or any(
                (ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value
            )
            or len(value_bytes) > MAX_D7_HTTP_HEADER_VALUE_BYTES
            or lower == "accept-encoding"
            and value.strip().lower() != "identity"
        ):
            raise ValueError("d7_http_header_rejected")
        seen.add(lower)
        total += len(name_bytes) + len(value_bytes) + 4
        normalized.append((name, value))
    if total > MAX_D7_HTTP_HEADER_BYTES:
        raise ValueError("d7_http_headers_too_large")
    return tuple(normalized), total


class D7SandboxHttpRuntime:
    """The only Sandbox path that can send D7 HTTP through the target forwarder."""

    def __init__(self, sandbox: TaskRunSandbox) -> None:
        self.sandbox = sandbox

    def execute(
        self,
        sandbox_id: str,
        *,
        task_run_id: str,
        run_config: D7WebRunConfig,
        authorization: D7WebAuthorization,
        network_binding: TargetNetworkBinding,
        target_network: T6TargetNetwork,
        method: str,
        path_query: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        timeout_seconds: float,
    ) -> D7SandboxHttpResponse:
        self.sandbox._validate_task_id(task_run_id)
        binding = self.sandbox._binding(sandbox_id)
        if (
            binding.task_run_id != task_run_id
            or not isinstance(run_config, D7WebRunConfig)
            or run_config.task_run_id != task_run_id
            or not isinstance(authorization, D7WebAuthorization)
            or authorization != run_config.authorization
            or not isinstance(network_binding, TargetNetworkBinding)
            or network_binding.target_url != run_config.endpoint
            or canonical_web_origin(network_binding.target_url) != run_config.endpoint
        ):
            raise ValueError("d7_http_authorization_or_ownership_mismatch")
        if (
            not isinstance(method, str)
            or method not in authorization.allowed_methods
            or method
            not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
            or not isinstance(path_query, str)
            or not path_query.startswith("/")
            or path_query.startswith("//")
            or "\\" in path_query
            or any(ord(char) < 32 or ord(char) == 127 for char in path_query)
            or "#" in path_query
            or not isinstance(body, bytes)
            or len(body) > MAX_D7_HTTP_REQUEST_BODY_BYTES
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= self.sandbox._settings.maximum_timeout_seconds
        ):
            raise ValueError("d7_http_request_rejected")
        request_headers, _ = _validate_header_limits(headers)
        private_name = "qinyuan-t6-in-" + task_run_id
        if (
            getattr(self.sandbox, "private_network", None) != private_name
            or self.sandbox._spec().get("network_mode") != private_name
            or authorization.allowed_tool != "d7_http_request"
            or authorization.network_allowed is not True
        ):
            raise ValueError("d7_http_sandbox_profile_mismatch")
        if not callable(getattr(target_network, "bind", None)):
            raise TypeError("d7_http_target_network_adapter_required")

        origin = run_config.endpoint
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        host = parsed.hostname
        port = parsed.port
        if host is None or port is None:
            raise ValueError("d7_http_origin_invalid")
        authority_host = f"[{host}]" if ":" in host else host
        authority = f"{authority_host}:{port}"
        payload = {
            "scheme": parsed.scheme,
            "host": host,
            "port": port,
            "authority": authority,
            "method": method,
            "path_query": path_query,
            "headers": [list(item) for item in request_headers],
            "body_b64": base64.b64encode(body).decode("ascii"),
            "timeout_seconds": float(timeout_seconds),
            "max_response_bytes": MAX_D7_HTTP_RESPONSE_BODY_BYTES,
            "max_header_bytes": MAX_D7_HTTP_HEADER_BYTES,
            "max_header_count": MAX_D7_HTTP_HEADER_COUNT,
            "max_header_value_bytes": MAX_D7_HTTP_HEADER_VALUE_BYTES,
        }
        input_bytes = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
        if len(input_bytes) > MAX_D7_HTTP_STDIN_BYTES:
            raise ValueError("d7_http_request_envelope_too_large")

        if not binding.execution_lock.acquire(timeout=float(timeout_seconds)):
            _fail("http_dispatch", task_run_id, sandbox_id)
        try:
            with self.sandbox._lock:
                if not binding.accepting or self.sandbox._closed:
                    _fail("http_dispatch", task_run_id, sandbox_id)
            self.sandbox._verify_active(task_run_id, sandbox_id)
            target_network.bind(task_run_id, network_binding, terminal=False)
            with self.sandbox._admit(binding):
                execution = self.sandbox._runtime.client.api.exec_create(
                    container=sandbox_id,
                    cmd=[
                        "timeout",
                        "--signal=TERM",
                        "--kill-after=5s",
                        f"{max(1, math.ceil(float(timeout_seconds)))}s",
                        "python3",
                        "-I",
                        "-c",
                        _HTTP_WORKER,
                    ],
                    stdin=True,
                    stdout=True,
                    stderr=True,
                    tty=False,
                    privileged=False,
                    user="10001:10001",
                    workdir="/workspace",
                    environment={"PATH": "/usr/bin:/bin"},
                )
            exec_id = execution.get("Id") if isinstance(execution, dict) else None
            if not isinstance(exec_id, str) or not exec_id:
                _fail("http_dispatch", task_run_id, sandbox_id)
            with self.sandbox._admit(binding):
                sock = self.sandbox._runtime.client.api.exec_start(
                    exec_id, socket=True, tty=False
                )
            raw = getattr(sock, "_sock", sock)
            if hasattr(raw, "settimeout"):
                raw.settimeout(float(timeout_seconds))
            done = threading.Event()

            def interrupt_wait() -> None:
                while not done.wait(0.025):
                    if time.monotonic() - started >= timeout_seconds:
                        try:
                            raw.close()
                        except OSError:
                            pass
                        return

            started = time.monotonic()
            threading.Thread(target=interrupt_wait, daemon=True).start()
            raw.sendall(input_bytes)
            if hasattr(raw, "shutdown"):
                raw.shutdown(1)
            from docker.utils.socket import next_frame_header, read_exactly

            stdout = bytearray()
            stderr = bytearray()
            while True:
                stream, length = next_frame_header(raw)
                if stream < 0:
                    break
                if stream not in (1, 2) or length < 0:
                    _unknown("http_dispatch", task_run_id, sandbox_id)
                target = stdout if stream == 1 else stderr
                limit = (
                    MAX_D7_HTTP_STDOUT_BYTES
                    if stream == 1
                    else MAX_D7_HTTP_STDERR_BYTES
                )
                if len(target) + length > limit:
                    _unknown("http_dispatch", task_run_id, sandbox_id)
                target.extend(read_exactly(raw, length))
            if time.monotonic() - started >= timeout_seconds:
                _unknown("http_dispatch", task_run_id, sandbox_id)
            result = self.sandbox._runtime.client.api.exec_inspect(exec_id)
            if (
                result.get("Running") is not False
                or result.get("ExitCode") != 0
                or stderr
            ):
                _unknown("http_dispatch", task_run_id, sandbox_id)
            response = json.loads(stdout)
            if not isinstance(response, dict) or set(response) != {
                "status",
                "headers",
                "body_b64",
            }:
                _unknown("http_dispatch", task_run_id, sandbox_id)

            try:
                response_body = base64.b64decode(response["body_b64"], validate=True)
            except (ValueError, TypeError) as exc:
                _unknown("http_dispatch", task_run_id, sandbox_id, exc)
            response_headers = response["headers"]
            if (
                type(response["status"]) is not int
                or not 100 <= response["status"] <= 599
                or len(response_body) > MAX_D7_HTTP_RESPONSE_BODY_BYTES
                or not isinstance(response_headers, list)
                or len(response_headers) > MAX_D7_HTTP_HEADER_COUNT
                or any(
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or not isinstance(pair[0], str)
                    or pair[0].lower() not in _RETURNED_RESPONSE_HEADERS
                    or not isinstance(pair[1], str)
                    or len(pair[1].encode("latin-1", errors="strict"))
                    > MAX_D7_HTTP_HEADER_VALUE_BYTES
                    for pair in response_headers
                )
                or sum(
                    len(name.encode("ascii")) + len(value.encode("latin-1")) + 4
                    for name, value in response_headers
                )
                > MAX_D7_HTTP_HEADER_BYTES
            ):
                _unknown("http_dispatch", task_run_id, sandbox_id)
            return D7SandboxHttpResponse(
                response["status"],
                tuple((name.lower(), value) for name, value in response_headers),
                response_body,
            )
        except TaskRunSandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 - Docker and socket boundary
            _unknown("http_dispatch", task_run_id, sandbox_id, exc)
        finally:
            if "done" in locals():
                done.set()
            if "sock" in locals():
                try:
                    sock.close()
                except OSError:
                    pass
            binding.execution_lock.release()


__all__ = ["D7SandboxHttpResponse", "D7SandboxHttpRuntime"]
