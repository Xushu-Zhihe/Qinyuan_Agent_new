"""Resolve only the TaskRun-authorized TCP destination for a target forwarder."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


PRIVATE_IPV4_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                              ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_rfc1918_ipv4(value) -> bool:
    return value is not None and any(ipaddress.ip_address(value) in network for network in PRIVATE_IPV4_NETWORKS)


class TargetAddressError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    family: int
    ip: str
    port: int
    sockaddr: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class TargetAddress:
    host: str
    port: int
    literal_ip: str | None

    @classmethod
    def from_url(cls, url: str, *, allow_private_ipv4: bool = False) -> TargetAddress:
        if (
            not isinstance(url, str)
            or len(url) > 2048
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
        ):
            raise TargetAddressError("invalid_target")
        try:
            parsed = urlsplit(url)
            host, port = parsed.hostname, parsed.port
        except ValueError as exc:
            raise TargetAddressError("invalid_target") from exc
        if (
            parsed.scheme not in {"http", "https", "tcp"}
            or not host
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (parsed.scheme == "tcp" and (parsed.path or parsed.query))
            or "%" in host
        ):
            raise TargetAddressError("invalid_target")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and (literal.is_multicast or
                not literal.is_global and not (allow_private_ipv4 and is_rfc1918_ipv4(literal))):
            raise TargetAddressError("non_public_target")
        return cls(host=host, port=port, literal_ip=str(literal) if literal else None)

    def resolve_for_connection(self) -> tuple[ResolvedAddress, ...]:
        """Call for every new TCP connection; never reuse an earlier DNS answer."""
        if self.literal_ip is not None:
            address = ipaddress.ip_address(self.literal_ip)
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            sockaddr: tuple[object, ...] = (
                (str(address), self.port, 0, 0)
                if family == socket.AF_INET6
                else (str(address), self.port)
            )
            return (ResolvedAddress(family, str(address), self.port, sockaddr),)

        try:
            answers = socket.getaddrinfo(
                self.host, self.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
        except (OSError, ValueError) as exc:
            raise TargetAddressError("dns_failed") from exc

        allowed: list[ResolvedAddress] = []
        seen: set[tuple[int, str]] = set()
        for family, socktype, _protocol, _canonname, sockaddr in answers:
            if (
                family not in {socket.AF_INET, socket.AF_INET6}
                or socktype != socket.SOCK_STREAM
            ):
                continue
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except (ValueError, IndexError, TypeError):
                continue
            if (
                address.version != (6 if family == socket.AF_INET6 else 4)
                or not address.is_global
                or address.is_multicast
                or (
                    family == socket.AF_INET6
                    and (len(sockaddr) < 4 or sockaddr[3] != 0)
                )
            ):
                continue
            key = family, str(address)
            if key in seen:
                continue
            seen.add(key)
            actual_sockaddr: tuple[object, ...] = (
                (str(address), self.port, 0, 0)
                if family == socket.AF_INET6
                else (str(address), self.port)
            )
            allowed.append(
                ResolvedAddress(family, str(address), self.port, actual_sockaddr)
            )
        if not allowed:
            raise TargetAddressError("no_allowed_address")
        return tuple(allowed)
