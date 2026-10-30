"""TaskRun-owned download preparation using an existing forwarder or a temporary worker."""
import asyncio
import hashlib
import io
import json
import os
import sys
from pathlib import Path, PurePosixPath
import tarfile
import time
from urllib.parse import urlsplit
from uuid import UUID
from docker.errors import NotFound
from Sandbox.tool_download_worker import MAX_DOWNLOAD_BYTES

DOWNLOAD_STOP_GRACE_SECONDS = 5.0
DOWNLOAD_TAR_OVERHEAD_BYTES = 1024 * 1024
DOWNLOAD_ALLOWED_HOSTS_ENV = "QINYUAN_DOWNLOAD_ALLOWED_HOSTS"


def _normalize_host(value):
    return str(value).strip().lower().rstrip(".")


def host_allowed(host, allowed_hosts):
    """Exact host, or a leading-dot suffix such as ``.githubusercontent.com``."""
    host = _normalize_host(host)
    if not host:
        return False
    for entry in allowed_hosts or ():
        entry = _normalize_host(entry)
        if not entry:
            continue
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


def _configured_allowed_hosts():
    raw = os.environ.get(DOWNLOAD_ALLOWED_HOSTS_ENV, "")
    return frozenset(
        item.strip().lower().rstrip(".")
        for item in raw.split(",")
        if item.strip()
    )


class ToolDownloadError(RuntimeError):
    """Known-ended preparation failure; no installation or target operation occurred."""


class ToolDownloadUnknown(RuntimeError):
    """Worker execution/cleanup has not been confirmed. Never automatically replay."""


def verified_artifacts(data, arguments, task_id, operation_id):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        if not members or members[0].name != "manifest.json" or members[0].size > 64 * 1024:
            raise ToolDownloadError("download.manifest_invalid")
        manifest = json.load(archive.extractfile(members[0]))
        if manifest.get("task_run_id") != task_id or manifest.get("operation_id") != operation_id:
            raise ToolDownloadError("download.binding_changed")
        artifacts = manifest.get("artifacts", [])
        if (not isinstance(artifacts, list)
                or len(members) != len(artifacts) + 1
                or len({m.name for m in members}) != len(members)):
            raise ToolDownloadError("download.manifest_invalid")
        if manifest.get("status") != "completed":
            raise ToolDownloadError(next(iter(manifest.get("errors") or []), "download.preparation_failed"))
        contents, total = {}, 0
        for info, record in zip(members[1:], artifacts, strict=True):
            path = PurePosixPath(info.name)
            if (not info.isfile() or info.name != record.get("name") or path.is_absolute() or ".." in path.parts
                    or (info.name != "native" and (len(path.parts) != 2 or path.parts[0] != "wheels" or path.suffix != ".whl"))
                    or info.size != record.get("size") or info.size < 0):
                raise ToolDownloadError("download.artifact_invalid")
            total += info.size
            if total > MAX_DOWNLOAD_BYTES:raise ToolDownloadError("download.size_exceeded")
            payload = archive.extractfile(info).read()
            actual = hashlib.sha256(payload).hexdigest()
            expected = arguments.get("sha256", "").lower() if info.name == "native" else record.get("sha256")
            if actual != expected or actual != record.get("sha256"):
                raise ToolDownloadError("download.sha256_mismatch")
            contents[info.name] = payload
        if not contents or ("native" not in contents and not arguments.get("python_package")):
            raise ToolDownloadError("download.artifact_missing")
        return manifest, contents


def installation_archive(contents, arguments, operation_id):
    output, directories = io.BytesIO(), set()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in contents.items():
            relative = ("sources/" + arguments["sha256"].lower() if name == "native" else
                        "wheels/" + operation_id + "/" + PurePosixPath(name).name)
            for parent in reversed(PurePosixPath(relative).parents):
                if str(parent) == "." or str(parent) in directories:continue
                info=tarfile.TarInfo(str(parent));info.type=tarfile.DIRTYPE;info.mode=0o700;info.uid=info.gid=10001
                archive.addfile(info);directories.add(str(parent))
            info=tarfile.TarInfo(relative);info.size=len(payload);info.mode=0o600;info.uid=info.gid=10001
            archive.addfile(info, io.BytesIO(payload))
            # The tar output already owns a copy; release each verified payload
            # so a single large artifact does not sit in memory three times.
            contents[name] = b""
    return output.getvalue()


class ToolDownload:
    def __init__(self, sandbox, task, operation_id, evidence, *, allowed_hosts=None,
                 forbidden_target=None):
        UUID(operation_id)
        self.sandbox, self.task, self.operation_id, self.evidence = sandbox, task, operation_id, evidence
        self.client = sandbox._runtime.client
        self.worker = self.exec_id = self.pid = self.stream = None
        self.temporary = False
        self.stopping = False
        if allowed_hosts is None:
            self.allowed_hosts = _configured_allowed_hosts()
        else:
            if isinstance(allowed_hosts, str):
                allowed_hosts = allowed_hosts.split(",")
            self.allowed_hosts = frozenset(
                _normalize_host(item) for item in allowed_hosts if _normalize_host(item)
            )
        self.forbidden_target = forbidden_target
        self.lease = {"task_run_id": task.task_run_id, "operation_id": operation_id}

    def record(self, **values):
        self.lease.update(values)
        self.evidence.save("mcp-download", self.operation_id, dict(self.lease))

    def owned(self, container, component):
        container.reload()
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if labels.get("qinyuan.task_run_id") != self.task.task_run_id or labels.get("qinyuan.component") != component:
            raise ToolDownloadUnknown("download.worker_binding_changed")

    def acquire(self, timeout):
        network = self.task.target_network
        if network is not None:
            self.worker = self.client.containers.get(network.forwarder_id)
            self.owned(self.worker, "t6-target-network")
            if self.worker.status != "running":raise ToolDownloadError("download.forwarder_not_running")
            memory = (self.worker.attrs.get("HostConfig") or {}).get("Memory")
            if isinstance(memory, int) and 0 < memory < 512 * 1024 * 1024:
                raise ToolDownloadError("download.forwarder_memory_insufficient")
        else:
            self.temporary = True
            name = "Ayui-download-" + self.task.task_run_id + "-" + self.operation_id
            labels = {"qinyuan.managed": "true", "qinyuan.component": "tool-download",
                      "qinyuan.task_run_id": self.task.task_run_id, "qinyuan.operation_id": self.operation_id}
            self.record(state="creating", name=name, temporary=True)
            try:
                self.worker = self.client.containers.create(image=self.sandbox._settings.image, name=name,
                    command=["python3", "-I", "-c", "import time; time.sleep("+str(timeout+DOWNLOAD_STOP_GRACE_SECONDS)+")"], entrypoint=[],
                    network="bridge", read_only=True, user="10001:10001", cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"], labels=labels,
                    tmpfs={"/tools-download": "rw,noexec,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700"},
                    mem_limit=self.sandbox._settings.memory_limit, memswap_limit=self.sandbox._settings.memory_swap_limit,
                    nano_cpus=self.sandbox._settings.cpu_limit, pids_limit=self.sandbox._settings.process_limit)
            except Exception:
                raise ToolDownloadUnknown("download.worker_creation_unknown") from None
            self.record(state="created", worker_id=self.worker.id)
            self.worker.start()
            self.owned(self.worker, "tool-download")
        self.record(state="ready", worker_id=self.worker.id, temporary=self.temporary)

    def interrupt(self):
        self.stopping = True
        if self.worker is None:return
        if self.temporary:
            self.owned(self.worker, "tool-download")
            self.worker.stop(timeout=1)
        elif self.pid is not None:
            # The PID comes from the trusted worker's first record, not remote content.
            self.worker.exec_run(["python3", "-I", "-c",
                "import os,signal; p=" + str(self.pid) + "; "
                "os.killpg(p,signal.SIGKILL) if os.getpgid(p)==p else os.kill(p,signal.SIGKILL)"], user="10001:10001")
            # A worker may already have exited. exec_inspect below decides, not the kill return code.
        if self.stream is not None:self.stream.close()

    def cleanup(self):
        if self.worker is None:
            if self.temporary:raise ToolDownloadUnknown("download.worker_creation_unknown")
            return
        if not self.temporary:
            if self.exec_id is not None:
                result=self.client.api.exec_inspect(self.exec_id)
                if result.get("Running"):
                    raise ToolDownloadUnknown("download.worker_exit_unconfirmed")
                if result.get("ExitCode") != 0:
                    # A killed worker cannot run its finally block. Remove only
                    # its UUID-scoped scratch directory after confirmed exit.
                    code=("import shutil; from pathlib import Path; p=Path('/tools-download')/"+repr(self.operation_id)+
                          "; shutil.rmtree(p) if p.exists() else None")
                    removed=self.worker.exec_run(["python3","-I","-c",code],user="10001:10001")
                    if removed.exit_code != 0:raise ToolDownloadUnknown("download.scratch_cleanup_unknown")
            return
        self.owned(self.worker, "tool-download")
        if self.worker.status not in {"created", "exited", "dead"}:
            self.worker.stop(timeout=1)
        self.worker.reload()
        if self.worker.status not in {"created", "exited", "dead"}:
            raise ToolDownloadUnknown("download.worker_cleanup_unknown")
        identity = self.worker.id
        self.worker.remove(force=False)
        try:self.client.containers.get(identity)
        except NotFound:return
        raise ToolDownloadUnknown("download.worker_cleanup_unknown")

    def _source_allowed(self, url):
        try:
            host = _normalize_host(urlsplit(url).hostname or "")
        except (TypeError, ValueError):
            return False
        return host_allowed(host, self.allowed_hosts)

    def prepare(self, arguments, timeout):
        url = arguments.get("url")
        if url and not self._source_allowed(url):
            raise ToolDownloadError("download.source_not_allowed")
        target = self.forbidden_target
        network = getattr(self.task, "target_network", None)
        if target is None and network is not None:
            target = network.target_url
        config = {"arguments": dict(arguments), "task_run_id": self.task.task_run_id,
                  "operation_id": self.operation_id, "timeout": timeout,
                  "allowed_hosts": sorted(self.allowed_hosts), "target": target}
        code = Path(__file__).with_name("tool_download_worker.py").read_text(encoding="utf-8")
        try:
            self.acquire(timeout)
            if self.stopping:raise ToolDownloadError("download.cancelled_before_dispatch")
            self.exec_id = self.client.api.exec_create(self.worker.id,
                ["python3", "-I", "-c", code, json.dumps(config)], user="10001:10001", stdout=True, stderr=True)["Id"]
            self.record(state="fetching", exec_id=self.exec_id)
            self.stream = self.client.api.exec_start(self.exec_id, stream=True, demux=True)
            prefix, data, stderr = bytearray(), bytearray(), bytearray()
            for stdout, error in self.stream:
                if error:stderr.extend(error[:max(0, 4096-len(stderr))])
                if not stdout:continue
                if self.pid is None:
                    prefix.extend(stdout)
                    if b"\n" not in prefix:
                        if len(prefix)>1024:raise ToolDownloadUnknown("download.worker_protocol_invalid")
                        continue
                    header, stdout = bytes(prefix).split(b"\n",1)
                    pid = json.loads(header).get("pid")
                    if type(pid) is not int or pid < 2:raise ToolDownloadUnknown("download.worker_protocol_invalid")
                    self.pid=pid
                    self.record(state="fetching", worker_pid=pid)
                data.extend(stdout)
                if len(data)>MAX_DOWNLOAD_BYTES+DOWNLOAD_TAR_OVERHEAD_BYTES:
                    self.interrupt()
                    raise ToolDownloadUnknown("download.transport_size_exceeded")
            inspection=self.client.api.exec_inspect(self.exec_id)
            if inspection.get("Running"):
                raise ToolDownloadUnknown("download.worker_exit_unconfirmed")
            if inspection.get("ExitCode") != 0:
                detail = bytes(stderr[-4096:]).decode("utf-8", errors="replace").strip()
                raise ToolDownloadError("download.worker_failed" + (": " + detail if detail else ""))
            manifest, contents=verified_artifacts(bytes(data), arguments, self.task.task_run_id,self.operation_id)
            data.clear()
            if self.stopping:raise ToolDownloadError("download.cancelled_before_install")
            # Recheck current TaskRun ownership before copying bytes into its existing container.
            self.sandbox._verify_active(self.task.task_run_id,self.task.sandbox_id)
            target=self.client.containers.get(self.task.sandbox_id)
            if not target.put_archive("/tools", installation_archive(contents,arguments,self.operation_id)):
                raise ToolDownloadUnknown("download.artifact_transfer_unknown")
            prepared = {"wheelhouse": "/tools/wheels/"+self.operation_id,
                        "wheel_hashes": {PurePosixPath(a["name"]).name:a["sha256"] for a in manifest["artifacts"] if a["name"].startswith("wheels/")},
                        "preparation_errors": list(manifest.get("errors") or [])}
            self.record(state="prepared", manifest=manifest, prepared=prepared)
            return prepared
        except ToolDownloadError as error:
            self.record(state="failed", code=str(error))
            raise
        except ToolDownloadUnknown:
            self.record(state="unknown")
            raise
        except Exception:
            self.record(state="unknown")
            raise ToolDownloadUnknown("download.outcome_unknown") from None
        finally:
            # A cleanup error must not turn a known failure into an unknown
            # outcome. The original known/unknown classification is preserved;
            # only a successful body whose cleanup is unconfirmed becomes Unknown.
            active = sys.exc_info()[0]
            try:self.cleanup()
            except Exception:
                self.record(cleanup="unknown")
                if active is None:
                    raise ToolDownloadUnknown("download.worker_cleanup_unknown") from None
            else:
                if self.temporary:self.record(cleanup="confirmed")

    async def run(self, arguments, timeout):
        worker=asyncio.create_task(asyncio.to_thread(self.prepare,arguments,timeout))
        try:
            return await asyncio.wait_for(asyncio.shield(worker),timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError) as stopped:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.interrupt),timeout=DOWNLOAD_STOP_GRACE_SECONDS)
                await asyncio.wait_for(asyncio.shield(worker),timeout=DOWNLOAD_STOP_GRACE_SECONDS)
            except Exception:pass
            if worker.done() and not worker.cancelled():
                error = worker.exception()
                if isinstance(error, ToolDownloadError):
                    # Prefer the real, known failure over a synthetic timeout that
                    # happened to race with the worker's last moment.  Keep the
                    # established cancellation code for a worker that observed the
                    # stop itself.
                    if str(error) != "download.cancelled_before_install":
                        raise error
                if not isinstance(error, ToolDownloadUnknown):
                    code = "download.preparation_cancelled" if isinstance(stopped, asyncio.CancelledError) else "download.preparation_timeout"
                    self.record(state="failed",code=code)
                    raise ToolDownloadError(code) from None
            self.record(state="unknown",code="download.worker_stop_unconfirmed")
            raise ToolDownloadUnknown("download.worker_stop_unconfirmed") from None
