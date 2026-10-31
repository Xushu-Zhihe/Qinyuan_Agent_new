"""Bounded target protocol preflight: endpoint first, application protocol second.

The workbench authorizes one host:port.  The URL scheme recorded by the UI is
only a hint: a crypto service can be entered as http://host:port while actually
speaking a raw TCP/banner protocol.  This module probes that exact endpoint
once, before the Agent starts, and returns the protocol to freeze.
"""
from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit


_PROTOCOLS = frozenset({"http", "https", "tcp"})


@dataclass(frozen=True, slots=True)
class TargetProbe:
    protocol: str
    host: str
    port: int
    endpoint: str
    evidence: str
    detected: bool = True


def _host_text(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def endpoint_for(host: str, port: int, protocol: str) -> str:
    if protocol not in _PROTOCOLS:
        raise ValueError("target_probe.invalid_protocol")
    return f"{protocol}://{_host_text(host)}:{port}"


def parse_endpoint(value: object) -> tuple[str, int, str | None, str]:
    """Return host, port, explicit scheme (or None for auto), original endpoint."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise ValueError("target_probe.endpoint_invalid")
    raw = value.strip()
    explicit: str | None = None
    candidate = raw
    for scheme in ("http", "https", "tcp", "auto"):
        prefix = scheme + "://"
        if raw.lower().startswith(prefix):
            explicit = scheme if scheme != "auto" else None
            candidate = raw[len(prefix):]
            break
    else:
        if "://" in raw:
            raise ValueError("target_probe.endpoint_invalid")
        candidate = raw
    try:
        parsed = urlsplit("tcp://" + candidate)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValueError("target_probe.endpoint_invalid") from exc
    if (
        not host
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("target_probe.endpoint_invalid")
    return host, port, explicit, raw


def _recv_http(host: str, port: int, timeout: float, *, tls: bool) -> str | None:
    context = ssl.create_default_context() if tls else None
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            raw.settimeout(timeout)
            sock = context.wrap_socket(raw, server_hostname=host) if context else raw
            try:
                sock.sendall(
                    f"HEAD / HTTP/1.1\r\nHost: {host}\r\n"
                    "User-Agent: qinyuan-target-probe\r\nConnection: close\r\n\r\n".encode()
                )
                response = sock.recv(4096)
            finally:
                if sock is not raw:
                    sock.close()
    except (OSError, ssl.SSLError):
        return None
    if response.startswith(b"HTTP/"):
        return response[:64].decode("latin-1", errors="replace")
    return None


def probe_target(value: object, *, timeout: float = 1.5) -> TargetProbe:
    host, port, explicit, original = parse_endpoint(value)
    timeout = max(0.2, min(float(timeout), 3.0))
    banner = b""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            raw.settimeout(min(0.6, timeout))
            try:
                banner = raw.recv(4096)
            except socket.timeout:
                banner = b""
    except OSError:
        if explicit in _PROTOCOLS:
            return TargetProbe(explicit, host, port, endpoint_for(host, port, explicit),
                               "probe_unreachable; explicit scheme used", detected=False)
        raise ValueError("target_probe.unreachable") from None

    if banner:
        if banner.startswith(b"HTTP/"):
            return TargetProbe("http", host, port, endpoint_for(host, port, "http"),
                               "banner=" + banner[:64].decode("latin-1", errors="replace"))
        return TargetProbe("tcp", host, port, endpoint_for(host, port, "tcp"),
                           "banner=" + banner[:64].decode("latin-1", errors="replace"))

    # No server banner: distinguish HTTPS, HTTP, and client-first raw TCP.
    tls_http = _recv_http(host, port, timeout, tls=True)
    if tls_http is not None:
        return TargetProbe("https", host, port, endpoint_for(host, port, "https"),
                           "tls_http=" + tls_http)
    plain_http = _recv_http(host, port, timeout, tls=False)
    if plain_http is not None:
        return TargetProbe("http", host, port, endpoint_for(host, port, "http"),
                           "http=" + plain_http)
    if explicit in _PROTOCOLS:
        return TargetProbe(explicit, host, port, endpoint_for(host, port, explicit),
                           "no HTTP/TLS response; explicit scheme used", detected=False)
    return TargetProbe("tcp", host, port, endpoint_for(host, port, "tcp"),
                       "no HTTP/TLS response; raw TCP")
