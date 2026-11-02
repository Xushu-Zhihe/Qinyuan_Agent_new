"""Protocol preflight keeps host:port fixed and discovers the application layer."""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_adapters.target_probe import (
    endpoint_for,
    parse_endpoint,
    probe_target,
)


class _Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _banner_server(payload, delay=0.0):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        with connection:
            if delay:
                time.sleep(delay)
            connection.sendall(payload)
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, port


def test_parse_endpoint_accepts_auto_bare_and_explicit_schemes():
    assert parse_endpoint("auto://example.test:8443")[:3] == ("example.test", 8443, None)
    assert parse_endpoint("example.test:8443")[:3] == ("example.test", 8443, None)
    assert parse_endpoint("http://example.test:8080")[:3] == ("example.test", 8080, "http")
    assert endpoint_for("example.test", 443, "https") == "https://example.test:443"


def test_workbench_accepts_bare_and_auto_endpoint_without_choosing_protocol():
    from agent_adapters.workbench import endpoint
    assert endpoint("example.test:22062") == "auto://example.test:22062"
    assert endpoint("auto://example.test:22062") == "auto://example.test:22062"
    assert endpoint("http://example.test:22062") == "http://example.test:22062"


def test_http_is_detected_and_frozen_as_http():
    server, thread = _http_server()
    try:
        probe = probe_target(f"auto://127.0.0.1:{server.server_port}", timeout=0.8)
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert probe.protocol == "http"
    assert probe.endpoint == f"http://127.0.0.1:{server.server_port}"


def test_raw_tcp_banner_is_not_misread_as_http():
    thread, port = _banner_server(b"HELLO FROM RAW SERVICE\n")
    probe = probe_target(f"auto://127.0.0.1:{port}", timeout=0.8)
    thread.join(timeout=2)
    assert probe.protocol == "tcp"
    assert probe.endpoint == f"tcp://127.0.0.1:{port}"
    assert "HELLO FROM RAW SERVICE" in probe.evidence


def test_slow_banner_on_a_declared_http_endpoint_is_detected_as_tcp():
    """A crypto service entered as http://host:port may answer only after seconds.

    Arena question 12261 declared http://node4.anna.nssnode.cn:24208 but the
    service printed its sumcheck banner ~3s after connect.  The preflight must
    wait for that banner instead of trusting the declared scheme, otherwise the
    run is frozen as HTTP and can never talk to a raw TCP service.
    """
    thread, port = _banner_server(b"HELLO LATE SERVICE\n", delay=0.9)
    probe = probe_target(f"http://127.0.0.1:{port}", timeout=2.0)
    thread.join(timeout=3)
    assert probe.protocol == "tcp" and probe.detected
    assert probe.endpoint == f"tcp://127.0.0.1:{port}"
    assert "HELLO LATE SERVICE" in probe.evidence


def test_silent_raw_tcp_is_not_forced_to_http():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        connection.close()
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    probe = probe_target(f"auto://127.0.0.1:{port}", timeout=0.3)
    thread.join(timeout=2)
    assert probe.protocol == "tcp"
    assert "no HTTP/TLS response" in probe.evidence


def test_auto_endpoint_with_unreachable_target_fails_closed():
    # Bind then close a port so the endpoint is a definite refused address.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    try:
        probe_target(f"auto://127.0.0.1:{port}", timeout=0.2)
    except ValueError as exc:
        assert str(exc) == "target_probe.unreachable"
    else:
        raise AssertionError("auto probe must fail closed when unreachable")
