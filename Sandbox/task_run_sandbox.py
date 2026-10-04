"""T2 TaskRun adapter. No tool authorization, scheduler, or automatic recovery.

The SDK transport/output reader is shared with the legacy Docker runtime. The
legacy create/adopt/destroy paths are deliberately not used: their retry and
network semantics do not implement the TaskRun contract.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock, RLock
from typing import Iterator, Literal
from uuid import UUID

from docker.errors import APIError, NotFound
from docker.utils import parse_bytes

from Sandbox.docker_runtime import DockerRuntime
from Sandbox.models import CommandRequest, CommandResult
from Sandbox.settings import SandboxSettings


class TaskRunSandboxError(RuntimeError):
    """Bounded diagnostic with original errors retained separately, never printed."""

    def __init__(
        self,
        phase: str,
        outcome: str,
        task_run_id: str,
        sandbox_ids: tuple[str, ...] = (),
        *,
        original_error: Exception | None = None,
        cleanup_error: Exception | None = None,
    ) -> None:
        self.phase = phase
        self.outcome = outcome
        self.task_run_id = task_run_id
        self.sandbox_ids = sandbox_ids
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        super().__init__(f"TaskRun Sandbox {phase}: {outcome} (TaskRun {task_run_id})")


@dataclass
class _Binding:
    task_run_id: str
    sandbox_id: str
    accepting: bool = True
    # False only for a resource this process created or verified while active.
    # Terminal recovery cannot prove no stop request is still pending in Docker.
    stop_requested: bool = False
    execution_lock: Lock = field(default_factory=Lock)
    cleanup_lock: RLock = field(default_factory=RLock)


class TaskRunSandbox:
    """One isolated container per TaskRun; execute is an internal runtime seam.

    T2 uses fixed test scripts; T3 dispatches a Policy-verified file wrapper.
    An executable allowlist
    is not a Python script security policy; T3 must supply Policy/MCP upstream.
    """

    def __init__(self, runtime: DockerRuntime) -> None:
        self._runtime = runtime
        self._settings = runtime.settings
        self._lock = RLock()
        self._bindings: dict[str, _Binding] = {}
        self._attempted: set[str] = set()
        self._image_id: str | None = None
        self._closed = False

    @classmethod
    def connect(cls, settings: SandboxSettings | None = None) -> TaskRunSandbox:
        # Explicit settings, no dotenv loading or implicit credential inheritance.
        return cls(DockerRuntime(settings, prepare_network=False))

    @staticmethod
    def _validate_task_id(task_run_id: str) -> None:
        parsed = UUID(task_run_id)
        if parsed.version != 4 or str(parsed) != task_run_id:
            raise ValueError("TaskRun Sandbox requires a canonical UUID4 TaskRun ID")

    @staticmethod
    def _labels(task_run_id: str) -> dict[str, str]:
        return {
            "qinyuan.managed": "true",
            "qinyuan.component": "task-run-sandbox",
            "qinyuan.task_run_id": task_run_id,
        }

    def _spec(self) -> dict[str, object]:
        s = self._settings
        return {
            "image": s.image,
            "command": ["sleep", "infinity"],
            "auto_remove": False,
            "network_mode": "none",
            "read_only": True,
            "tmpfs": {
                "/workspace": f"rw,noexec,nosuid,nodev,size={s.workspace_size},uid=10001,gid=10001,mode=0700",
                "/tmp": f"rw,noexec,nosuid,nodev,size={s.temporary_size},mode=1777",
            },
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": s.memory_limit,
            "memswap_limit": s.memory_swap_limit,
            "nano_cpus": s.cpu_limit,
            "pids_limit": s.process_limit,
            "user": "10001:10001",
            "working_dir": "/workspace",
        }

    def _fingerprint(self) -> str:
        return sha256(json.dumps(self._spec(), sort_keys=True).encode()).hexdigest()

    def _image(self) -> str:
        if self._image_id is None:
            # Read only: never pull or build an image as part of this adapter.
            image = self._runtime.client.images.get(self._settings.image)
            if image.attrs.get("Config", {}).get("Volumes"):
                raise ValueError("T2 image must not declare anonymous volumes")
            self._image_id = str(image.id)
        return self._image_id

    def _owned(self, container, task_run_id: str) -> None:
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        if any(labels.get(k) != v for k, v in self._labels(task_run_id).items()):
            raise TaskRunSandboxError(
                "inspect", "ownership_mismatch", task_run_id, (str(container.id),)
            )

    def _configured(self, container, task_run_id: str) -> None:
        attrs = container.attrs
        config = attrs.get("Config") or {}
        host = attrs.get("HostConfig") or {}
        net = attrs.get("NetworkSettings") or {}
        labels = config.get("Labels") or {}
        expected_network = self._spec()["network_mode"]
        expected_host = {
            "NetworkMode": expected_network,
            "ReadonlyRootfs": True,
            "Privileged": False,
            "AutoRemove": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": self._spec()["tmpfs"],
            "Memory": parse_bytes(self._settings.memory_limit),
            "MemorySwap": parse_bytes(self._settings.memory_swap_limit),
            "NanoCpus": self._settings.cpu_limit,
            "PidsLimit": self._settings.process_limit,
        }
        mismatch = (
            labels.get("qinyuan.config_sha256") != self._fingerprint()
            or attrs.get("Image") != self._image()
            or config.get("User") != "10001:10001"
            or config.get("WorkingDir") != "/workspace"
            or config.get("Cmd") != self._spec()["command"]
            or config.get("Volumes")
            or any(host.get(k) != v for k, v in expected_host.items())
            or any(
                host.get(k)
                for k in (
                    "Binds",
                    "Mounts",
                    "PortBindings",
                    "PublishAllPorts",
                    "CapAdd",
                    "Devices",
                    "DeviceRequests",
                    "VolumesFrom",
                    "Links",
                )
            )
            or host.get("PidMode") == "host"
            or host.get("IpcMode") == "host"
            or set((net.get("Networks") or {}).keys()) != {expected_network}
            or any((net.get("Ports") or {}).values())
            or any(m.get("Type") != "tmpfs" for m in attrs.get("Mounts", ()))
        )
        if mismatch:
            raise TaskRunSandboxError(
                "inspect", "configuration_mismatch", task_run_id, (str(container.id),)
            )

    def _inspect(self, task_run_id: str, sandbox_id: str):
        try:
            container = self._runtime.client.containers.get(sandbox_id)
            container.reload()
            if str(container.id) != sandbox_id:
                raise TaskRunSandboxError(
                    "inspect", "identity_mismatch", task_run_id, (sandbox_id,)
                )
            self._owned(container, task_run_id)
            return container
        except NotFound:
            return None
        except TaskRunSandboxError:
            raise
        except Exception as exc:
            raise TaskRunSandboxError(
                "inspect",
                "outcome_unknown",
                task_run_id,
                (sandbox_id,),
                original_error=exc,
            ) from exc

    def inspect_presence(
        self, task_run_id: str, sandbox_id: str
    ) -> Literal["present", "absent", "unknown"]:
        """Read resource presence without stopping, deleting, or retrying it."""
        self._validate_task_id(task_run_id)
        try:
            return "absent" if self._inspect(task_run_id, sandbox_id) is None else "present"
        except TaskRunSandboxError:
            return "unknown"

    def _verify_active(self, task_run_id: str, sandbox_id: str) -> None:
        try:
            container = self._inspect(task_run_id, sandbox_id)
            if container is None:
                raise TaskRunSandboxError("bind", "missing", task_run_id, (sandbox_id,))
            self._configured(container, task_run_id)
            if container.status != "running":
                raise TaskRunSandboxError(
                    "bind", "not_running", task_run_id, (sandbox_id,)
                )
        except TaskRunSandboxError:
            raise
        except Exception as exc:
            raise TaskRunSandboxError(
                "inspect",
                "outcome_unknown",
                task_run_id,
                (sandbox_id,),
                original_error=exc,
            ) from exc

    def create(self, task_run_id: str) -> str:
        self._validate_task_id(task_run_id)
        # Serialize reservation only, not different TaskRuns' Docker requests.
        with self._lock:
            if self._closed:
                raise TaskRunSandboxError("create", "closed", task_run_id)
            if task_run_id in self._attempted:
                raise TaskRunSandboxError("create", "already_attempted", task_run_id)
            self._attempted.add(task_run_id)
        labels = {
            **self._labels(task_run_id),
            "qinyuan.config_sha256": self._fingerprint(),
        }
        container = None
        create_sent = False
        try:
            self._image()
            create_sent = True
            # No retry: a timeout may have created the resource on the daemon.
            spec = {**self._spec(), "image": self._image()}
            container = self._runtime.client.containers.create(
                **spec, name=f"qinyuan-task-{task_run_id}", labels=labels
            )
            container.start()  # At most once; never start from attach/recovery.
            container.reload()
            self._owned(container, task_run_id)
            self._configured(container, task_run_id)
            if container.status != "running":
                raise TaskRunSandboxError(
                    "create", "not_running", task_run_id, (str(container.id),)
                )
        except Exception as original:
            if not create_sent:
                raise TaskRunSandboxError(
                    "create", "creation_failed", task_run_id, original_error=original
                ) from original
            known_ids: tuple[str, ...] = (
                (str(container.id),) if container is not None else ()
            )
            try:
                found = self._runtime.client.containers.list(
                    all=True,
                    filters={
                        "label": [
                            f"{k}={v}" for k, v in self._labels(task_run_id).items()
                        ]
                    },
                )
                known_ids = tuple(
                    sorted(set(known_ids + tuple(str(c.id) for c in found)))
                )
                # Never choose an arbitrary match or create a replacement.
                if len(known_ids) != 1:
                    raise TaskRunSandboxError(
                        "create", "outcome_unknown", task_run_id, known_ids
                    )
                candidate = self._inspect(task_run_id, known_ids[0])
                if candidate is None:
                    raise TaskRunSandboxError(
                        "create", "outcome_unknown", task_run_id, known_ids
                    )
                self._configured(candidate, task_run_id)
                if candidate.status == "running":
                    container = candidate
                else:
                    # Removal of an owned, confirmed stopped/created resource is
                    # safe. Never stop a container after an uncertain start.
                    if candidate.status not in {"created", "exited", "dead"}:
                        raise TaskRunSandboxError(
                            "create", "outcome_unknown", task_run_id, known_ids
                        )
                    candidate.remove(force=False)
                    if self._inspect(task_run_id, str(candidate.id)) is not None:
                        raise TaskRunSandboxError(
                            "remove", "outcome_unknown", task_run_id, known_ids
                        )
                    raise TaskRunSandboxError(
                        "create",
                        "creation_failed",
                        task_run_id,
                        known_ids,
                        original_error=original,
                    )
            except Exception as reconciliation:
                if (
                    isinstance(reconciliation, TaskRunSandboxError)
                    and reconciliation.outcome == "creation_failed"
                ):
                    raise reconciliation from original
                definitive = (
                    isinstance(original, APIError)
                    and 400 <= original.status_code < 500
                    and not known_ids
                )
                raise TaskRunSandboxError(
                    "create",
                    "creation_failed" if definitive else "outcome_unknown",
                    task_run_id,
                    known_ids,
                    original_error=original,
                    cleanup_error=reconciliation,
                ) from original
        assert container is not None
        sandbox_id = str(container.id)
        with self._lock:
            if self._closed:
                raise TaskRunSandboxError(
                    "create", "outcome_unknown", task_run_id, (sandbox_id,)
                )
            self._bindings[sandbox_id] = _Binding(task_run_id, sandbox_id)
        return sandbox_id

    def bind(self, task_run_id: str, sandbox_id: str, *, terminal: bool) -> None:
        self._validate_task_id(task_run_id)
        with self._lock:
            if self._closed:
                raise TaskRunSandboxError("bind", "closed", task_run_id, (sandbox_id,))
            if any(
                b.task_run_id == task_run_id and b.sandbox_id != sandbox_id
                for b in self._bindings.values()
            ):
                raise TaskRunSandboxError(
                    "bind", "identity_mismatch", task_run_id, (sandbox_id,)
                )
            existing = self._bindings.get(sandbox_id)
            if existing is not None and existing.task_run_id != task_run_id:
                raise TaskRunSandboxError(
                    "bind", "ownership_mismatch", task_run_id, (sandbox_id,)
                )
            if existing is not None and not terminal and not existing.accepting:
                raise TaskRunSandboxError(
                    "bind", "execution_rejected", task_run_id, (sandbox_id,)
                )
        if not terminal:
            self._verify_active(task_run_id, sandbox_id)
        with self._lock:
            if self._closed:
                raise TaskRunSandboxError("bind", "closed", task_run_id, (sandbox_id,))
            self._attempted.add(task_run_id)
            if existing is None:
                self._bindings[sandbox_id] = _Binding(
                    task_run_id, sandbox_id, not terminal, terminal
                )
            elif terminal:
                existing.accepting = False

    def _binding(self, sandbox_id: str) -> _Binding:
        with self._lock:
            binding = self._bindings.get(sandbox_id)
            if binding is None:
                raise ValueError("Sandbox ID is not bound to this adapter")
            return binding

    def quiesce(self, sandbox_id: str) -> None:
        binding = self._binding(sandbox_id)
        with self._lock:
            binding.accepting = False

    def execute(self, sandbox_id: str, request: CommandRequest) -> CommandResult:
        binding = self._binding(sandbox_id)
        if (
            not isinstance(request, CommandRequest)
            or request.tool != "python3"
            or not isinstance(request.arguments, tuple)
            or any(
                not isinstance(arg, str) or "\x00" in arg for arg in request.arguments
            )
            or not isinstance(request.timeout_seconds, int)
            or isinstance(request.timeout_seconds, bool)
            or not 1
            <= request.timeout_seconds
            <= self._settings.maximum_timeout_seconds
        ):
            raise ValueError("TaskRun execution requires python3 and a bounded command timeout")
        with binding.execution_lock:
            with self._lock:
                if not binding.accepting:
                    raise TaskRunSandboxError(
                        "execute",
                        "execution_rejected",
                        binding.task_run_id,
                        (sandbox_id,),
                    )
            self._verify_active(binding.task_run_id, sandbox_id)
            try:
                return self._runtime.execute(
                    sandbox_id, request, admission=lambda: self._admit(binding)
                )
            except TaskRunSandboxError:
                raise
            except Exception as exc:
                raise TaskRunSandboxError(
                    "execute",
                    "outcome_unknown",
                    binding.task_run_id,
                    (sandbox_id,),
                    original_error=exc,
                ) from exc

    @contextmanager
    def _admit(self, binding: _Binding) -> Iterator[None]:
        with self._lock:
            if not binding.accepting or self._closed:
                raise TaskRunSandboxError(
                    "execute",
                    "execution_rejected",
                    binding.task_run_id,
                    (binding.sandbox_id,),
                )
            yield

    def interrupt(self, sandbox_id: str) -> None:
        binding = self._binding(sandbox_id)
        self.quiesce(sandbox_id)
        with binding.cleanup_lock:
            container = self._inspect(binding.task_run_id, sandbox_id)
            stream_error = None
            try:
                self._runtime._close_output_stream(sandbox_id)
            except Exception as exc:
                stream_error = exc
            if container is not None and container.status in {
                "running",
                "paused",
                "restarting",
            }:
                if binding.stop_requested:
                    raise TaskRunSandboxError(
                        "stop",
                        "outcome_unknown",
                        binding.task_run_id,
                        (sandbox_id,),
                        original_error=stream_error,
                    )
                binding.stop_requested = (
                    True  # Set before dispatch, including timeouts.
                )
                try:
                    container.stop(timeout=self._settings.docker_stop_timeout_seconds)
                except Exception as exc:
                    raise TaskRunSandboxError(
                        "stop",
                        "outcome_unknown",
                        binding.task_run_id,
                        (sandbox_id,),
                        original_error=exc,
                        cleanup_error=stream_error,
                    ) from exc
                container = self._inspect(binding.task_run_id, sandbox_id)
            if container is not None and container.status not in {
                "created",
                "exited",
                "dead",
            }:
                raise TaskRunSandboxError(
                    "stop", "outcome_unknown", binding.task_run_id, (sandbox_id,)
                )
            if stream_error is not None:
                raise TaskRunSandboxError(
                    "interrupt",
                    "outcome_unknown",
                    binding.task_run_id,
                    (sandbox_id,),
                    original_error=stream_error,
                )

    def destroy(self, sandbox_id: str) -> None:
        binding = self._binding(sandbox_id)
        self.quiesce(sandbox_id)
        with binding.cleanup_lock:
            self.interrupt(sandbox_id)
            container = self._inspect(binding.task_run_id, sandbox_id)
            if container is None:
                return
            if container.status not in {"created", "exited", "dead"}:
                raise TaskRunSandboxError(
                    "remove", "outcome_unknown", binding.task_run_id, (sandbox_id,)
                )
            try:
                container.remove(force=False)
            except NotFound:
                return
            except Exception as exc:
                raise TaskRunSandboxError(
                    "remove",
                    "outcome_unknown",
                    binding.task_run_id,
                    (sandbox_id,),
                    original_error=exc,
                ) from exc
            if self._inspect(binding.task_run_id, sandbox_id) is not None:
                raise TaskRunSandboxError(
                    "remove", "outcome_unknown", binding.task_run_id, (sandbox_id,)
                )

    def cleanup_uncommitted(
        self, task_run_id: str, sandbox_id: str | None = None
    ) -> None:
        """Explicit cleanup using a failed create's identity, never a broad prune.

        The caller must supply the identifiers from TaskRunSandboxError. A
        running resource with uncertain creation/stop is left for manual action.
        """
        self._validate_task_id(task_run_id)
        if sandbox_id is None:
            try:
                found = self._runtime.client.containers.list(
                    all=True,
                    filters={
                        "label": [
                            f"{k}={v}" for k, v in self._labels(task_run_id).items()
                        ]
                    },
                )
            except Exception as exc:
                raise TaskRunSandboxError(
                    "cleanup", "outcome_unknown", task_run_id, original_error=exc
                ) from exc
            ids = tuple(str(c.id) for c in found)
            if len(ids) != 1:
                # An empty query after an ambiguous create does not prove that
                # the original create request cannot finish later.
                raise TaskRunSandboxError(
                    "cleanup", "outcome_unknown", task_run_id, ids
                )
            sandbox_id = ids[0]
        self.bind(task_run_id, sandbox_id, terminal=True)
        self.destroy(sandbox_id)

    def close(self) -> None:
        """Close admission and SDK connections; does not implicitly delete resources."""
        with self._lock:
            self._closed = True
            for binding in self._bindings.values():
                binding.accepting = False
        self._runtime.close()
