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

MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_REDIRECTS = 5


def public_addresses(host, port, forbidden):
    if host.lower() in forbidden:
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


def fetch(url, path, expected, remaining, forbidden):
    for redirect in range(MAX_REDIRECTS + 1):
        value = source_url(url)
        addresses = public_addresses(value.hostname, value.port or 443, forbidden)
        connection = http.client.HTTPSConnection(value.hostname, value.port or 443,
                                                  timeout=min(30, remaining()), context=ssl.create_default_context())
        family, socktype, proto, _, address = addresses[0]
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
                url = urljoin(url, response.getheader("location"))
                continue
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
        finally:
            connection.close()
            plain.close()
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
    if host.lower() in forbidden:raise ValueError('download.target_not_allowed')
    candidates=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not candidates or any(not ipaddress.ip_address(a[4][0]).is_global or a[4][0] in forbidden for a in candidates):
        raise ValueError('download.non_public_source')
    # Pin the checked address; urllib3 keeps the original TLS identity/SNI.
    return connect((candidates[0][4][0],port),timeout,source_address,socket_options)
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
    target = config.get("target")
    if target:
        parsed = urlsplit(target)
        forbidden.add(parsed.hostname.lower())
        try:forbidden.update(a[4][0] for a in socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM))
        except socket.gaierror:pass
    arguments = config["arguments"]
    manifest = {"operation_id": config["operation_id"], "task_run_id": config["task_run_id"], "artifacts": []}
    errors = []
    try:
        if arguments.get("url"):
            try:
                artifact = fetch(arguments["url"], root / "native", arguments["sha256"].lower(), remaining, forbidden)
                manifest["artifacts"].append(artifact)
            except (ValueError, OSError, http.client.HTTPException, TimeoutError) as error:
                errors.append(str(error) if str(error).startswith("download.") else "download.transport_failed")
        # Prepare an explicitly configured fallback even when native fetch succeeds:
        # archive extraction or self-test can still fail later in the offline sandbox.
        if arguments.get("python_package"):
            wheels = root / "wheels"
            wheels.mkdir()
            with (root / "pip.log").open("wb") as log:
                result = subprocess.run([sys.executable, "-I", "-c", PIP_GUARD, json.dumps(sorted(forbidden)),
                    "--isolated", "download", "--disable-pip-version-check", "--no-cache-dir", "--no-input",
                    "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "--timeout", "30", "--retries", "0",
                    "--dest", str(wheels), arguments["python_package"] + "==" + arguments["python_version"]],
                    stdout=log, stderr=subprocess.STDOUT, timeout=remaining(), check=False,
                    env={**os.environ, "PIP_CONFIG_FILE": os.devnull, "TMPDIR": str(root)})
            if result.returncode:errors.append("download.python_resolution_failed")
            else:
                total = 0
                for file in sorted(wheels.iterdir()):
                    if not file.is_file() or file.is_symlink() or file.suffix != ".whl":
                        raise ValueError("download.invalid_wheel")
                    total += file.stat().st_size
                    if total > MAX_DOWNLOAD_BYTES:raise ValueError("download.size_exceeded")
                    with file.open("rb") as source:
                        sha256 = hashlib.file_digest(source, "sha256").hexdigest()
                    manifest["artifacts"].append({"name": "wheels/" + file.name,
                        "size": file.stat().st_size, "sha256": sha256})
        manifest.update(status="completed" if manifest["artifacts"] else "failed", errors=errors)
    except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
        manifest.update(status="failed", errors=errors + [str(error) if str(error).startswith("download.") else "download.preparation_failed"])
        manifest["artifacts"] = []
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
