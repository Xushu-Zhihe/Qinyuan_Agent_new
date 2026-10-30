"""Fixed TCP forwarder run only in a TaskRun-scoped sidecar container."""

from __future__ import annotations

import errno
import json
import os
import select
import socket
import sys
import threading
import time

sys.path.insert(0, "/opt/qinyuan")
from target_address import TargetAddress, TargetAddressError

MAX_ACTIVE_CONNECTIONS = 16
BUFFER_BYTES = 65536
IDLE_TIMEOUT_SECONDS = 120


def emit(event: str, **details: object) -> None:
    print(json.dumps({"event": event, **details}, sort_keys=True), flush=True)


def relay(client: socket.socket, upstream: socket.socket) -> None:
    sockets = (client, upstream)
    while True:
        ready, _, _ = select.select(sockets, (), (), IDLE_TIMEOUT_SECONDS)
        if not ready:
            return
        for source in ready:
            block = source.recv(BUFFER_BYTES)
            if not block:
                return
            destination = upstream if source is client else client
            destination.sendall(block)


def serve_connection(
    client: socket.socket, target: TargetAddress, limit: threading.Semaphore
) -> None:
    try:
        with client:
            try:
                addresses = target.resolve_for_connection()
            except TargetAddressError as exc:
                emit("target_rejected", reason=exc.code)
                return
            failures = set()
            for address in addresses:
                upstream = None
                try:
                    upstream = socket.socket(address.family, socket.SOCK_STREAM)
                    upstream.settimeout(5)
                    upstream.connect(address.sockaddr)
                except OSError as error:
                    code = ("target_connect_timeout" if isinstance(error, TimeoutError) else
                            "target_connection_refused" if error.errno == errno.ECONNREFUSED else
                            "target_network_unreachable" if error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH} else
                            "target_connect_failed")
                    failures.add(code)
                    if upstream is not None:
                        upstream.close()
                    continue
                with upstream:
                    emit("target_connected", ip=address.ip, port=address.port)
                    relay(client, upstream)
                return
            emit("target_connection_failed", reasons=sorted(failures))
    except OSError:
        emit("target_relay_failed")
    finally:
        limit.release()


def target_from_arguments(arguments):
    private = len(arguments) == 2 and arguments[0] == "--allow-private-ipv4"
    if not private and len(arguments) != 1:
        raise TargetAddressError("invalid_target_arguments")
    return TargetAddress.from_url(arguments[-1], allow_private_ipv4=private)


def reap_children(stop=None):
    """PID 1 must reap exec/pip children or a long task accumulates zombies."""
    # WNOHANG is Unix-only; the forwarded process runs in the Linux sidecar.
    # The fallback keeps the unit test importable on Windows.
    wait_flag = getattr(os, "WNOHANG", 1)
    while stop is None or not stop.is_set():
        try:
            pid, _status = os.waitpid(-1, wait_flag)
        except ChildProcessError:
            time.sleep(0.1)
            continue
        if pid == 0:
            time.sleep(0.1)


def main() -> int:
    try:
        target = target_from_arguments(sys.argv[1:])
    except TargetAddressError:
        return 2
    threading.Thread(target=reap_children, name="child-reaper", daemon=True).start()
    family = socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", target.port))
    listener.listen(MAX_ACTIVE_CONNECTIONS)
    limit = threading.Semaphore(MAX_ACTIVE_CONNECTIONS)
    emit("ready", port=target.port)
    with listener:
        while True:
            client, _ = listener.accept()
            if not limit.acquire(blocking=False):
                client.close()
                emit("connection_limit")
                continue
            threading.Thread(
                target=serve_connection,
                args=(client, target, limit),
                daemon=True,
            ).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
