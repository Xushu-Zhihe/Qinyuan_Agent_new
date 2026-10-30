"""Trusted one-shot fetcher, executed in an egress-capable container, never the host."""
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit, urljoin

# Leave headroom inside the 256 MiB /tools and /tools-download tmpfs for the
# manifest, pip log, receipt and filesystem overhead.
MAX_DOWNLOAD_BYTES = 240 * 1024 * 1024
MAX_REDIRECTS = 5


def _host_allowed(host, allowed_hosts):
    host = str(host).strip().lower().rstrip(".")
    if not host:
        return False
    for entry in allowed_hosts or ():
        entry = str(entry).strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def public_addresses(host, port, forbidden, allowed_hosts=None):
    host = str(host).lower().rstrip(".")
    if allowed_hosts is not None and not _host_allowed(host, allowed_hosts):
        raise ValueError("download.source_not_allowed")
    if host in forbidden:
        raise ValueError("download.target_not_allowed")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global or a[4][0] in forbidden for a in addresses):
        raise ValueError("download.non_public_source")
    return addresses


def source_url(url):
    value = urlsplit(url)
    if (value.scheme != "https" or not value.hostname or value.username is not None
            or value.password is not None or value.fragment or any(ord(c) < 33 for c in url)):
        raise ValueError("download.https_source_required")
    return value


def fetch(url, path, expected, remaining, forbidden, allowed_hosts=None):
    for redirect in range(MAX_REDIRECTS + 1):
        value = source_url(url)
        addresses = public_addresses(value.hostname, value.port or 443, forbidden, allowed_hosts)
        last_error = None
        redirect_to = None
        for family, socktype, proto, _, address in addresses:
            connection = http.client.HTTPSConnection(value.hostname, value.port or 443,
                                                      timeout=min(30, remaining()), context=ssl.create_default_context())
            plain = socket.socket(family, socktype, proto)
            try:
                plain.settimeout(min(30, remaining()))
                plain.connect(address)
                connection.sock = connection._context.wrap_socket(plain, server_hostname=value.hostname)
                connection.request("GET", (value.path or "/") + ("?" + value.query if value.query else ""),
                                   headers={"User-Agent": "Qinyuan-tool-download", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    if redirect == MAX_REDIRECTS or not response.getheader("location"):
                        raise ValueError("download.redirect_limit")
                    redirect_to = urljoin(url, response.getheader("location"))
                    break
                if response.status != 200:
                    raise ValueError("download.http_" + str(response.status))
                total, digest = 0, hashlib.sha256()
                with path.open("wb") as target:
                    while True:
                        connection.sock.settimeout(min(30, remaining())) if connection.sock is not None else None
                        chunk = response.read(64 * 1024)
                        if not chunk:break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:raise ValueError("download.size_exceeded")
                        digest.update(chunk)
                        target.write(chunk)
                actual = digest.hexdigest()
                if actual != expected:raise ValueError("download.sha256_mismatch")
                return {"name": "native", "sha256": actual, "size": total, "url": url}
            except (OSError, http.client.HTTPException) as error:
                # A dual-stack host may return IPv6 first while the container has
                # no IPv6 route. Try every resolved address before giving up.
                last_error = error
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            finally:
                connection.close()
                plain.close()
        if redirect_to is not None:
            url = redirect_to
            continue
        if last_error is not None:
            raise last_error
        raise OSError("download.transport_failed")
    raise ValueError("download.redirect_limit")


# pip runs only in the trusted egress worker. Each outgoing request must remain
# HTTPS on public PyPI hosts, including redirects and dependency resolution.
PIP_GUARD = r"""
import ipaddress,json,os,socket,sys
from urllib.parse import urlsplit
from pip._vendor.requests.sessions import Session
from pip._vendor.urllib3.util import connection
forbidden=set(json.loads(sys.argv[1]))
send=Session.send
connect=connection.create_connection

def checked_send(self, request, **kwargs):
    value=urlsplit(request.url)
    if (value.scheme!='https' or value.hostname not in {'pypi.org','files.pythonhosted.org'} or value.port not in {None,443}
            or value.username is not None or value.password is not None):
        raise ValueError('download.python_source_not_allowed')
    return send(self,request,**kwargs)

def checked_connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
    host,port=address
    if host.lower().rstrip('.') in forbidden:raise ValueError('download.target_not_allowed')
    candidates=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not candidates or any(not ipaddress.ip_address(a[4][0]).is_global or a[4][0] in forbidden for a in candidates):
        raise ValueError('download.non_public_source')
    # Pin a checked address; urllib3 keeps the original TLS identity/SNI.
    # Try every resolved address: IPv6-first hosts must not fail on IPv4-only
    # container networks.
    last=None
    for candidate in candidates:
        try:
            return connect((candidate[4][0],port),timeout,source_address,socket_options)
        except OSError as error:
            last=error
    raise last if last is not None else OSError('download.transport_failed')
Session.send=checked_send
connection.create_connection=checked_connect
from pip._internal.cli.main import main
raise SystemExit(main(sys.argv[2:]))
"""


def main(config):
    try:os.setsid()
    except PermissionError:
        if os.getpgrp() != os.getpid():raise
    print(json.dumps({"pid": os.getpid()}), flush=True)
    root = Path("/tools-download") / config["operation_id"]
    root.mkdir(mode=0o700)
    deadline = time.monotonic() + config["timeout"]
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:raise TimeoutError("download.deadline_exceeded")
        return value
    forbidden = set(config.get("forbidden", []))
    allowed_hosts = config.get("allowed_hosts", [])
    target = config.get("target")
    if target:
        parsed = urlsplit(target)
        if parsed.hostname:
            forbidden.add(parsed.hostname.lower().rstrip("."))
            try:forbidden.update(a[4][0] for a in socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM))
            except (socket.gaierror, TypeError, ValueError):pass
    arguments = config["arguments"]
    manifest = {"operation_id": config["operation_id"], "task_run_id": config["task_run_id"], "artifacts": []}
    errors = []

    def code(error, default):
        text = str(error)
        return text if text.startswith("download.") else default

    try:
        if arguments.get("url"):
            try:
                artifact = fetch(arguments["url"], root / "native", arguments["sha256"].lower(),
                                 remaining, forbidden, allowed_hosts)
                manifest["artifacts"].append(artifact)
            except (ValueError, OSError, http.client.HTTPException, TimeoutError) as error:
                errors.append(code(error, "download.transport_failed"))
        # Prepare an explicitly configured fallback even when native fetch succeeds:
        # archive extraction or self-test can still fail later in the offline sandbox.
        # Keep native and Python failures independent: a bad wheel must not discard
        # an already verified native artifact.
        if arguments.get("python_package"):
            try:
                wheels = root / "wheels"
                wheels.mkdir()
                with (root / "pip.log").open("wb") as log:
                    result = subprocess.run([sys.executable, "-I", "-c", PIP_GUARD, json.dumps(sorted(forbidden)),
                        "--isolated", "download", "--disable-pip-version-check", "--no-cache-dir", "--no-input",
                        "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "--timeout", "30", "--retries", "0",
                        "--dest", str(wheels), arguments["python_package"] + "==" + arguments["python_version"]],
                        stdout=log, stderr=subprocess.STDOUT, timeout=remaining(), check=False,
                        env={**os.environ, "PIP_CONFIG_FILE": os.devnull, "TMPDIR": str(root)})
                if result.returncode:
                    errors.append("download.python_resolution_failed")
                else:
                    total = 0
                    native_bytes = sum(a["size"] for a in manifest["artifacts"]
                                       if a["name"] == "native")
                    for file in sorted(wheels.iterdir()):
                        if not file.is_file() or file.is_symlink() or file.suffix != ".whl":
                            raise ValueError("download.invalid_wheel")
                        total += file.stat().st_size
                        if native_bytes + total > MAX_DOWNLOAD_BYTES:raise ValueError("download.size_exceeded")
                        with file.open("rb") as source:
                            sha256 = hashlib.file_digest(source, "sha256").hexdigest()
                        manifest["artifacts"].append({"name": "wheels/" + file.name,
                            "size": file.stat().st_size, "sha256": sha256})
            except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
                # Do not expose a partially validated wheel set. A native
                # artifact, if already fetched, remains usable on its own.
                manifest["artifacts"] = [
                    item for item in manifest["artifacts"]
                    if not item["name"].startswith("wheels/")
                ]
                errors.append(code(error, "download.python_preparation_failed"))
        # Partial success is meaningful: the host may still install the native
        # artifact even when the optional Python fallback failed.
        manifest.update(status="completed" if manifest["artifacts"] else "failed", errors=errors)
    except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
        manifest.update(status="completed" if manifest["artifacts"] else "failed",
                        errors=errors + [code(error, "download.preparation_failed")])
    try:
        data = json.dumps(manifest).encode()
        import io
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            info = tarfile.TarInfo("manifest.json");info.size=len(data)
            archive.addfile(info, io.BytesIO(data))
            for artifact in manifest["artifacts"]:
                archive.add(root / artifact["name"], arcname=artifact["name"], recursive=False)
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
