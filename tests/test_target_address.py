"""Target forwarding must keep the exact port and recheck every DNS answer."""

import socket

import pytest

from Sandbox.target_address import TargetAddress, TargetAddressError


def test_domain_reresolves_and_filters_rebound_private_addresses(monkeypatch):
    replies = iter(
        [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 9999))],
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9999)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 9999)),
            ],
        ]
    )
    calls = []

    def lookup(*args, **kwargs):
        calls.append((args, kwargs))
        return next(replies)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    target = TargetAddress.from_url("tcp://example.org:24009")
    first = target.resolve_for_connection()
    assert first[0].sockaddr == ("93.184.215.14", 24009)
    with pytest.raises(TargetAddressError, match="no_allowed_address"):
        target.resolve_for_connection()
    assert len(calls) == 2
    assert calls[0][0] == ("example.org", 24009)


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "169.254.1.1", "10.0.0.2", "192.0.2.1", "2001:db8::1"]
)
def test_dns_rejects_non_global_addresses(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 9001, 0, 0) if family == socket.AF_INET6 else (ip, 9001)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, "", sockaddr)],
    )
    target = TargetAddress.from_url("tcp://example.org:9001")
    with pytest.raises(TargetAddressError, match="no_allowed_address"):
        target.resolve_for_connection()


def test_literal_ip_is_fixed_without_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("literal IP must not use DNS"),
    )
    target = TargetAddress.from_url("tcp://10.0.0.2:24009")
    assert target.resolve_for_connection()[0].sockaddr == ("10.0.0.2", 24009)


@pytest.mark.parametrize(
    "url",
    [
        "tcp://example.org",
        "tcp://example.org:0",
        "tcp://example.org:9001/path",
        "tcp://user@example.org:9001",
        "tcp://example.org:9001#fragment",
        "tcp://example.org:9001\n",
        "ftp://example.org:9001",
    ],
)
def test_rejects_invalid_authorization_url(url):
    with pytest.raises(TargetAddressError, match="invalid_target"):
        TargetAddress.from_url(url)


def test_dns_failure_does_not_fall_back_to_prior_address(monkeypatch):
    target = TargetAddress.from_url("https://example.org:443/path")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(socket.gaierror()),
    )
    with pytest.raises(TargetAddressError, match="dns_failed"):
        target.resolve_for_connection()
