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
    "ip", ["127.0.0.1", "169.254.1.1", "10.0.0.2", "192.0.2.1", "224.0.0.1", "2001:db8::1"]
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
    target = TargetAddress.from_url("tcp://10.0.0.2:24009", allow_private_ipv4=True)
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


@pytest.mark.parametrize("address",["10.0.0.0","10.255.255.255","172.16.0.0","172.31.255.255","192.168.0.0","192.168.255.255"])
def test_private_literal_requires_explicit_capability_and_keeps_exact_port(monkeypatch,address):
    with pytest.raises(TargetAddressError,match="non_public_target"):
        TargetAddress.from_url(f"http://{address}:8080")
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:pytest.fail("literal must not resolve DNS"))
    target=TargetAddress.from_url(f"http://{address}:8080",allow_private_ipv4=True)
    assert [i.sockaddr for i in target.resolve_for_connection()]==[(address,8080)]


@pytest.mark.parametrize("address",["127.0.0.1","0.0.0.0","169.254.169.254","100.64.0.1","192.0.2.1","198.18.0.1","224.0.0.1","255.255.255.255","[::1]","[fc00::1]","[::ffff:10.0.0.1]"])
def test_private_capability_does_not_allow_other_special_literals(address):
    with pytest.raises(TargetAddressError,match="non_public_target"):
        TargetAddress.from_url(f"http://{address}:8080",allow_private_ipv4=True)


def test_private_capability_does_not_allow_domain_to_resolve_private(monkeypatch):
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,"",("10.0.0.1",8080))])
    target=TargetAddress.from_url("http://example.org:8080",allow_private_ipv4=True)
    with pytest.raises(TargetAddressError,match="no_allowed_address"):target.resolve_for_connection()


@pytest.fixture
def forwarder(monkeypatch):
    import runpy,sys
    from pathlib import Path
    from Sandbox import target_address
    monkeypatch.setattr(sys,"path",list(sys.path))
    monkeypatch.setitem(sys.modules,"target_address",target_address)
    return runpy.run_path(str(Path(__file__).resolve().parents[1]/"Sandbox"/"target_forwarder.py"))


def test_forwarder_requires_private_start_argument(forwarder):
    parse=forwarder["target_from_arguments"]
    with pytest.raises(TargetAddressError,match="non_public_target"):parse(["http://10.0.1.5:8080"])
    assert parse(["--allow-private-ipv4","http://10.0.1.5:8080"]).port==8080
    for args in ([],["--allow-private-ipv4"],["--unknown","http://10.0.1.5:8080"],["--allow-private-ipv4","http://127.0.0.1:8080"]):
        with pytest.raises(TargetAddressError):parse(args)


def test_forwarder_reaps_exited_helpers(forwarder, monkeypatch):
    import threading
    stop = threading.Event()
    calls = []
    def waitpid(*args):
        calls.append(args)
        stop.set()
        return (123, 0)
    monkeypatch.setattr(forwarder["os"], "waitpid", waitpid)
    forwarder["reap_children"](stop)
    assert calls == [(-1, getattr(forwarder["os"], "WNOHANG", 1))]


@pytest.mark.parametrize("code,expected",[(111,"target_connection_refused"),(101,"target_network_unreachable"),(None,"target_connect_timeout")])
def test_forwarder_classifies_known_connect_failure_without_retrying_request(forwarder,monkeypatch,code,expected):
    import errno
    from types import SimpleNamespace
    error=TimeoutError() if code is None else OSError(errno.ECONNREFUSED if code==111 else errno.ENETUNREACH,"controlled")
    calls=[];events=[];released=[]
    class Sock:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def close(self):pass
        def settimeout(self,*args):pass
        def connect(self,address):calls.append(address);raise error
    monkeypatch.setattr(forwarder["socket"],"socket",lambda *a:Sock())
    forwarder["serve_connection"].__globals__["emit"]=lambda event,**details:events.append((event,details))
    target=TargetAddress.from_url("http://10.0.1.5:8080",allow_private_ipv4=True)
    forwarder["serve_connection"](Sock(),target,SimpleNamespace(release=lambda:released.append(True)))
    assert calls==[("10.0.1.5",8080)] and released==[True]
    assert events==[("target_connection_failed",{"reasons":[expected]})]
