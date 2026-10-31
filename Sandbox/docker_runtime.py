from __future__ import annotations

import re
import time
from contextlib import AbstractContextManager, nullcontext, suppress
from hashlib import sha256
from threading import Lock
from typing import Callable, TypeVar

import docker
import requests
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from Sandbox.models import CommandRequest, CommandResult, SandboxMounts
from Sandbox.settings import SandboxSettings


_T = TypeVar("_T")

class DockerRuntimeError(RuntimeError):
    pass


def _connection_failure_message(error: BaseException) -> str:
    """Keep the actionable Docker daemon error without dumping a traceback."""

    detail = " ".join(str(error).split())
    if not detail:
        return "Cannot connect to Docker."
    # Docker Desktop may include a long socket/HTTP diagnostic. Keep provider
    # stderr bounded while retaining the permission/endpoint hint users need.
    if len(detail) > 300:
        detail = detail[:297] + "..."
    return f"Cannot connect to Docker: {detail}"


def _compact_error(error: BaseException) -> str:
    detail = " ".join(str(error).split())
    return detail[:300] + ("..." if len(detail) > 300 else "")


class DockerRuntime:
    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        prepare_network: bool = True,
    ) -> None:
        self.settings = settings or SandboxSettings()

        client = None

        try:
            # 初始化阶段使用较短的超时。
            client = self._with_retries(
                lambda: docker.DockerClient(
                    base_url=self.settings.docker_host,
                    version="auto",
                    timeout=self.settings.docker_connect_timeout_seconds,
                    max_pool_size=self.settings.docker_max_pool_size,
                ),
                action="connect to Docker daemon",
            )
            client.api.trust_env = False

            version_info = self._with_retries(
                client.version,
                action="check Docker daemon version",
            )

            if not isinstance(version_info, dict):
                raise DockerRuntimeError(
                    "Docker returned an invalid version response"
                )

            if not version_info.get("ApiVersion"):
                raise DockerRuntimeError(
                    "Docker version response does not contain ApiVersion"
                )

            # 控制面必须快速失败。长命令输出使用按需创建的独立客户端。
            client.api.timeout = self.settings.docker_control_timeout_seconds

            self.client = client
            self._stream_client = None
            self._stream_client_lock = Lock()
            self._active_output_streams: dict[str, object] = {}
            self._active_output_streams_lock = Lock()
            if prepare_network:
                self._ensure_network()

        except DockerRuntimeError:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            raise

        except Exception as error:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

            raise DockerRuntimeError(_connection_failure_message(error)) from error

    def _with_retries(
        self,
        operation: Callable[[], _T],
        *,
        action: str,
    ) -> _T:
        """Retry only daemon transport and 5xx failures with bounded backoff."""

        settings = getattr(self, "settings", None)
        attempts = getattr(settings, "docker_operation_retries", 3)
        backoff = getattr(settings, "docker_retry_backoff_seconds", 0.25)
        for attempt in range(attempts):
            try:
                return operation()
            except Exception as error:
                if not self._is_transient(error):
                    raise
                if attempt + 1 >= attempts:
                    raise DockerRuntimeError(
                        f"Docker {action} failed after {attempts} attempts: "
                        f"{_compact_error(error)}"
                    ) from error
                delay = backoff * (2**attempt)
                if delay:
                    time.sleep(delay)
        raise AssertionError("unreachable retry loop")

    def _stream_api(self):
        """Return the long-timeout API client used only for exec output."""

        with self._stream_client_lock:
            if self._stream_client is None:
                client = docker.DockerClient(
                    base_url=self.settings.docker_host,
                    version=self.client.api.api_version,
                    timeout=self.settings.docker_api_timeout_seconds,
                    max_pool_size=self.settings.docker_max_pool_size,
                )
                client.api.trust_env = False
                self._stream_client = client
            return self._stream_client.api

    def _register_output_stream(self, sandbox_id: str, stream: object) -> None:
        with self._active_output_streams_lock:
            self._active_output_streams[sandbox_id] = stream

    def _close_output_stream(
        self,
        sandbox_id: str,
        *,
        expected: object | None = None,
    ) -> None:
        """Close one active Docker socket so its reader can leave the host thread."""

        with self._active_output_streams_lock:
            stream = self._active_output_streams.get(sandbox_id)
            if stream is None or (expected is not None and stream is not expected):
                return
            self._active_output_streams.pop(sandbox_id, None)

        close_stream = getattr(stream, "close", None)
        if callable(close_stream):
            close_stream()

    @staticmethod
    def _is_transient(error: BaseException) -> bool:
        if isinstance(error, PermissionError):
            return False
        if isinstance(error, APIError):
            status = getattr(error, "status_code", None)
            return status is None or status >= 500
        if isinstance(error, DockerException):
            detail = str(error).lower()
            return any(
                marker in detail
                for marker in ("eof", "timeout", "connection reset", "unavailable")
            ) and "access denied" not in detail
        return isinstance(
            error,
            (
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError,
                TimeoutError,
                OSError,
            ),
        )

    def create(self, agent_id: str, mounts: SandboxMounts) -> str:
        self._ensure_image_exists()

        existing = self._adopt_existing(agent_id, mounts)
        if existing is not None:
            return existing

        container_name = self._container_name(agent_id)
        tmpfs = {
            "/tmp": (
                "rw,noexec,nosuid,nodev,"
                f"size={self.settings.temporary_size},"
                "mode=1777"
            ),
            "/tools": (
                "rw,exec,nosuid,nodev,"
                "size=256m,uid=10001,gid=10001,mode=0700"
            ),
        }
        volumes: dict[str, dict[str, str]] = {}
        working_dir = "/workspace"

        if mounts.work_dir is None:
            tmpfs["/workspace"] = (
                "rw,noexec,nosuid,nodev,"
                f"size={self.settings.workspace_size},"
                "uid=10001,gid=10001,mode=0700"
            )
        else:
            volumes[str(mounts.work_dir)] = {"bind": "/work", "mode": "rw"}
            working_dir = "/work"

        if mounts.challenge_dir is not None:
            volumes[str(mounts.challenge_dir)] = {
                "bind": "/challenge",
                "mode": "ro",
            }

        container = None
        try:
            container = self._with_retries(
                lambda: self.client.containers.create(
                    image=self.settings.image,
                    name=container_name,
                    command=["sleep", "infinity"],
                    auto_remove=False,
                    network=self.settings.network,
                    read_only=True,
                    tmpfs=tmpfs,
                    volumes=volumes or None,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    mem_limit=self.settings.memory_limit,
                    memswap_limit=self.settings.memory_swap_limit,
                    nano_cpus=self.settings.cpu_limit,
                    pids_limit=self.settings.process_limit,
                    user="10001:10001",
                    working_dir=working_dir,
                    environment={
                        "HOME": "/tmp/qinyuan-home",
                        "PATH": "/tools/bin:/opt/dirsearch/bin:/opt/microsoft/powershell/7:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "PYTHONPATH": "/tools/python:/opt/qinyuan/python",
                        "XDG_CACHE_HOME": "/tmp/qinyuan-cache",
                        "XDG_CONFIG_HOME": "/tmp/qinyuan-config",
                        "DOTNET_CLI_HOME": "/tmp/qinyuan-dotnet",
                        "POWERSHELL_TELEMETRY_OPTOUT": "1",
                    },
                    labels={
                        "qinyuan.managed": "true",
                        "qinyuan.component": "sandbox",
                        "qinyuan.agent_id": agent_id,
                        "qinyuan.mount_fingerprint": self._mount_fingerprint(mounts),
                    },
                ),
                action="create sandbox",
            )

            # Creating and starting are separate Docker operations. Retrying
            # containers.run() after a start-side 5xx attempts to create the
            # deterministic name again and hides the original runtime error
            # behind a misleading name conflict.
            self._with_retries(
                container.start,
                action="start sandbox",
            )

            self._with_retries(container.reload, action="inspect created sandbox")

            if container.status != "running":
                logs = self._with_retries(
                    lambda: container.logs(
                        stdout=True,
                        stderr=True,
                        tail=100,
                    ),
                    action="read sandbox startup logs",
                ).decode("utf-8", errors="replace")

                self.destroy(container.id)

                raise DockerRuntimeError(
                        f"Sandbox failed to start: {logs}"
                )

            return container.id

        except APIError as error:
            # A second process may have won the deterministic-name race, or
            # Docker may have returned EOF after creating the detached
            # container. Adopt the already-running container instead of
            # creating a second sandbox for the same Agent.
            adopted = self._adopt_existing(agent_id, mounts)
            if adopted is not None:
                return adopted
            if container is not None:
                with suppress(Exception):
                    self._remove_container(container)
            detail = getattr(error, "explanation", None) or _compact_error(error)
            raise DockerRuntimeError(
                f"Failed to create sandbox for agent {agent_id!r}: {detail}"
            ) from error
        except DockerRuntimeError as error:
            # _with_retries converts a final transport failure into a runtime
            # error. The detached container may nevertheless exist server
            # side, so check ownership before reporting failure.
            if container is None or any(
                marker in str(error)
                for marker in (
                    "create sandbox",
                    "start sandbox",
                    "inspect created sandbox",
                )
            ):
                adopted = self._adopt_existing(agent_id, mounts)
                if adopted is not None:
                    return adopted
                # _adopt_existing removes a matching non-running container.
                # Avoid issuing a second removal for the same object below.
                container = None
            if container is not None:
                with suppress(Exception):
                    self._remove_container(container)
            raise
        except Exception as error:
            if container is not None:
                with suppress(Exception):
                    self._remove_container(container)
            raise DockerRuntimeError(
                f"Failed to initialize sandbox for agent {agent_id!r}"
            ) from error

    def _adopt_existing(
        self,
        agent_id: str,
        mounts: SandboxMounts,
    ) -> str | None:
        """Reuse the one running container owned by an Agent.

        Stopped containers are safe to remove. Running containers are never
        removed during startup cleanup, which prevents a second invocation from
        deleting a live Agent sandbox and creating a replacement.
        """

        containers = self._managed_containers(agent_id)
        running = []
        for container in containers:
            try:
                self._with_retries(
                    container.reload,
                    action="inspect owned sandbox",
                )
            except NotFound:
                continue
            if getattr(container, "status", "") == "running":
                running.append(container)
            else:
                self._remove_container(container)

        if len(running) > 1:
            ids = ", ".join(str(item.id) for item in running)
            raise DockerRuntimeError(
                f"Agent {agent_id!r} owns multiple running sandboxes: {ids}. "
                "Stop the stale containers before retrying."
            )
        if not running:
            return None

        container = running[0]
        labels = getattr(container, "labels", None) or {}
        if not labels:
            attrs = getattr(container, "attrs", {}) or {}
            labels = (attrs.get("Config") or {}).get("Labels") or {}
        fingerprint = labels.get("qinyuan.mount_fingerprint")
        expected = self._mount_fingerprint(mounts)
        if fingerprint is not None and fingerprint != expected:
            raise DockerRuntimeError(
                f"Agent {agent_id!r} already has a sandbox with different mounts"
            )
        return str(container.id)

    def _managed_containers(self, agent_id: str) -> list[object]:
        list_method = getattr(self.client.containers, "list", None)
        if not callable(list_method):
            return []
        return list(
            self._with_retries(
                lambda: list_method(
                    all=True,
                    filters={
                        "label": [
                            "qinyuan.managed=true",
                            "qinyuan.component=sandbox",
                            f"qinyuan.agent_id={agent_id}",
                        ]
                    },
                ),
                action=f"find sandbox for agent {agent_id!r}",
            )
        )

    def _remove_container(self, container: object) -> None:
        try:
            self._with_retries(
                container.reload,
                action="inspect sandbox before removal",
            )
        except NotFound:
            return
        if getattr(container, "status", "") == "running":
            self._stop_container(container)

        remove = getattr(container, "remove", None)
        if not callable(remove):
            return
        try:
            self._with_retries(
                lambda: remove(force=False),
                action="remove sandbox",
            )
        except NotFound:
            return

    def _stop_container(self, container: object) -> None:
        stop = getattr(container, "stop", None)
        if not callable(stop):
            raise DockerRuntimeError("Docker container does not support stop")
        try:
            # Stop is destructive and may still be running inside Docker after
            # the client times out. Retrying it creates overlapping kill
            # requests against the same containerd task.
            stop(
                timeout=self.settings.docker_stop_timeout_seconds,
            )
            self._with_retries(container.reload, action="inspect stopped sandbox")
        except NotFound:
            return
        except DockerRuntimeError:
            raise
        except Exception as error:
            raise DockerRuntimeError(
                f"Docker stop sandbox failed: {_compact_error(error)}"
            ) from error
        if getattr(container, "status", "") == "running":
            raise DockerRuntimeError(
                f"Sandbox {getattr(container, 'id', 'unknown')!r} is still running"
            )

    @classmethod
    def _container_name(cls, agent_id: str) -> str:
        normalized = cls._normalize_agent_id(agent_id)
        identity = sha256(agent_id.encode("utf-8")).hexdigest()[:12]
        return f"qinyuan-{normalized}-{identity}"

    @staticmethod
    def _mount_fingerprint(mounts: SandboxMounts) -> str:
        challenge = str(mounts.challenge_dir or "")
        work = str(mounts.work_dir or "")
        return sha256(f"challenge={challenge}\nwork={work}".encode("utf-8")).hexdigest()

    def execute(
        self,
        sandbox_id: str,
        request: CommandRequest,
        *,
        admission: Callable[[], AbstractContextManager[None]] | None = None,
    ) -> CommandResult:
        """
        在指定 Agent 的容器中执行命令。

        安全性质：
        1. 不调用 Shell。
        2. 命令和参数使用 argv 列表。
        3. 使用非 root 用户。
        4. 使用 GNU timeout 限制执行时间。
        5. 流式读取输出，防止超大输出占满宿主机内存。
        """

        container = self._get_running_container(sandbox_id)

        command = [
            "timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{request.timeout_seconds}s",
            *request.argv,
        ]

        stdout_buffer = _BoundedByteBuffer(
            self.settings.maximum_output_bytes_per_stream
        )
        stderr_buffer = _BoundedByteBuffer(
            self.settings.maximum_output_bytes_per_stream
        )

        output_stream = None

        try:
            # 使用 Docker 低级 API，是为了流式读取 stdout/stderr。
            configured_workdir = (
                (container.attrs.get("Config") or {}).get("WorkingDir")
                or "/workspace"
            )
            # T2 serializes dispatch against closing execution admission. Do not
            # hold this guard while draining output: cancellation must progress.
            with admission() if admission is not None else nullcontext():
                execution = self.client.api.exec_create(
                    container=container.id,
                    cmd=command,
                    stdout=True,
                    stderr=True,
                    stdin=False,
                    tty=False,
                    privileged=False,
                    user="10001:10001",
                    workdir=configured_workdir,
                )

            exec_id = execution.get("Id")
            if not isinstance(exec_id, str) or not exec_id:
                raise DockerRuntimeError(
                    "Docker did not return a valid exec ID"
                )

            # This request was admitted at exec_create. Do not block terminal
            # stop on the streaming handshake (it has a longer read timeout).
            output_stream = self._stream_api().exec_start(
                exec_id=exec_id,
                detach=False,
                tty=False,
                stream=True,
                demux=True,
            )
            try:
                with admission() if admission is not None else nullcontext():
                    self._register_output_stream(sandbox_id, output_stream)
            except Exception:
                output_stream.close()
                output_stream = None
                raise

            for output in output_stream:
                if isinstance(output, tuple):
                    stdout_chunk, stderr_chunk = output
                    stdout_buffer.append(stdout_chunk)
                    stderr_buffer.append(stderr_chunk)

                elif isinstance(output, bytes):
                    # 理论上 demux=True 时应该返回 tuple。
                    # 保留这个分支是为了兼容不同 Docker API 行为。
                    stdout_buffer.append(output)

            execution_info = self.client.api.exec_inspect(exec_id)
            exit_code = execution_info.get("ExitCode")

            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise DockerRuntimeError(
                    "Docker did not return a valid exit code"
                )

        except DockerRuntimeError:
            raise

        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to execute command in sandbox {sandbox_id!r}: "
                f"{error.explanation}"
            ) from error

        except Exception as error:
            raise DockerRuntimeError(
                f"Failed to read command output from sandbox "
                f"{sandbox_id!r}"
            ) from error

        finally:
            if output_stream is not None:
                self._close_output_stream(
                    sandbox_id,
                    expected=output_stream,
                )

        stdout_bytes, stdout_truncated, stdout_total = stdout_buffer.render()
        stderr_bytes, stderr_truncated, stderr_total = stderr_buffer.render()

        return CommandResult(
            sandbox_id=sandbox_id,
            exit_code=exit_code,
            stdout=self._decode_output(stdout_bytes),
            stderr=self._decode_output(stderr_bytes),

            # 137 还可能是 OOM Kill 或外部 SIGKILL，不能直接视为超时。
            timed_out=exit_code == 124,

            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            termination_confirmed=execution_info.get("Running") is False,
            stdout_bytes=stdout_total,
            stderr_bytes=stderr_total,
        )

    def interrupt(self, sandbox_id: str) -> None:
        """Stop a sandbox so Docker can close active exec streams safely."""

        stream_error: Exception | None = None
        try:
            # This is the host-side cancellation boundary. It releases the
            # Python reader even when containerd never emits an exit event.
            self._close_output_stream(sandbox_id)
        except Exception as error:
            stream_error = error

        try:
            container = self._with_retries(
                lambda: self.client.containers.get(sandbox_id),
                action=f"find sandbox {sandbox_id!r}",
            )
            self._with_retries(container.reload, action="inspect sandbox to interrupt")
            if getattr(container, "status", "") == "running":
                self._stop_container(container)
        except NotFound:
            if stream_error is None:
                return
        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to interrupt sandbox {sandbox_id!r}: {error.explanation}"
            ) from error
        except DockerRuntimeError as error:
            raise DockerRuntimeError(
                f"Failed to interrupt sandbox {sandbox_id!r}: {error}"
            ) from error
        if stream_error is not None:
            raise DockerRuntimeError(
                f"Failed to close sandbox {sandbox_id!r} output stream: "
                f"{_compact_error(stream_error)}"
            ) from stream_error

    def destroy(self,sandbox_id: str) -> None:
        """
        先停止再删除 Docker 容器。

        如果容器已经不存在，直接返回。
        因此该方法可以安全地重复调用。
        """

        try:
            container = self._with_retries(
                lambda: self.client.containers.get(sandbox_id),
                action=f"find sandbox {sandbox_id!r}",
            )

            self._remove_container(container)

        except NotFound:
            return

        except APIError as error:
            raise DockerRuntimeError(
                f"Failed to destroy sandbox {sandbox_id!r}:"
                f"{error.explanation}"
            ) from error

        except DockerRuntimeError as error:
            raise DockerRuntimeError(
                f"Failed to destroy sandbox {sandbox_id!r}: {error}"
            ) from error

    def cleanup_agent_containers(self, agent_id: str) -> tuple[str, ...]:
        """Remove containers owned by one stable Agent execution identity."""

        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise ValueError("agent_id cannot be empty")
        removed: list[str] = []
        for container in self._managed_containers(normalized_agent_id):
            try:
                self._with_retries(
                    container.reload,
                    action="inspect stale sandbox",
                )
            except NotFound:
                continue
            if getattr(container, "status", "") == "running":
                continue
            container_id = str(container.id)
            self._remove_container(container)
            removed.append(container_id)
        return tuple(removed)

    def destroy_agent_containers(self, agent_id: str) -> tuple[str, ...]:
        """Remove every container owned by one logical Agent lifecycle."""

        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise ValueError("agent_id cannot be empty")
        removed: list[str] = []
        for container in self._managed_containers(normalized_agent_id):
            container_id = str(container.id)
            self._remove_container(container)
            removed.append(container_id)
        return tuple(removed)

    def close(self) -> None:
        """
        关闭 Docker SDK客户端连接

        该方法不会自动删除容器
        SandboxService.close() 会先执行 stop_all()
        然后再调用这个方法
        """
        active_streams = tuple(
            getattr(self, "_active_output_streams", {}).keys()
        )
        for sandbox_id in active_streams:
            with suppress(Exception):
                self._close_output_stream(sandbox_id)
        stream_client = getattr(self, "_stream_client", None)
        if stream_client is not None:
            stream_client.close()
        self.client.close()

    def _ensure_network(self) -> None:
        """
        创建或检查 Docker 网络。

        默认 internal=True。公网授权靶场必须显式配置独立的非 internal
        网络，避免无意中改变既有隔离网络的安全属性。

        目标靶场容器后续也需要加入该网络
        """

        try:
            network = self._with_retries(
                lambda: self.client.networks.get(self.settings.network),
                action=f"find Docker network {self.settings.network!r}",
            )

        except NotFound:
            try:
                network = self._with_retries(
                    lambda: self.client.networks.create(
                        name=self.settings.network,
                        driver="bridge",
                        internal=self.settings.network_internal,
                        check_duplicate=True,
                        labels={
                            "qinyuan.managed": "true",
                            "qinyuan.component": "sandbox-network",
                        },
                    ),
                    action=f"create Docker network {self.settings.network!r}",
                )


            except APIError as error:
                if error.status_code != 409:
                    raise DockerRuntimeError(
                        "Failed to create sandbox network:"
                        f"{error.explanation}"
                    ) from error

                network = self._with_retries(
                    lambda: self.client.networks.get(self.settings.network),
                    action=f"find Docker network {self.settings.network!r}",
                )

        self._with_retries(network.reload, action="inspect Docker network")

        actual_internal = bool(network.attrs.get("Internal", False))
        if actual_internal != self.settings.network_internal:
            raise DockerRuntimeError(
                f"Docker network {self.settings.network!r} internal setting "
                f"is {actual_internal}, expected "
                f"{self.settings.network_internal}"
            )

    def _ensure_image_exists(self) -> None:
        """
        检查沙盒镜像是否已经构建。

        镜像不存在时不自动从互联网拉取，
        防止使用未经确认的外部镜像
        """

        try:
            self._with_retries(
                lambda: self.client.images.get(self.settings.image),
                action=f"find sandbox image {self.settings.image!r}",
            )

        except ImageNotFound as error:
            raise DockerRuntimeError(
                f"Sandbox image {self.settings.image!r} does not exist."
                " Build the sandbox image before starting agents."
            ) from error

    def _get_running_container(self,sandbox_id:str):
        """
        获取容器并确认容器处于运行状态
        """

        try:
            container = self._with_retries(
                lambda: self.client.containers.get(sandbox_id),
                action=f"find sandbox {sandbox_id!r}",
            )

        except NotFound as error:
            raise DockerRuntimeError(
                f"Sandbox {sandbox_id!r} does not exist"
            ) from error

        self._with_retries(
            container.reload,
            action=f"inspect sandbox {sandbox_id!r}",
        )

        if container.status != "running":
            raise DockerRuntimeError(
                f"Sandbox {sandbox_id!r} is not running;"
                f"current status: {container.status}"
            )

        return container

    @staticmethod
    def _normalize_agent_id(agent_id: str) -> str:
        """
        将 Agent ID转换为安全的 Docker容器名称片段
        非字母、数字、下划线、点号和连字符汇编替换为连字符
        """

        normalized = re.sub(
            r"[^a-zA-Z0-9_.-]",
            "-",
            agent_id,
        ).strip(".-")

        return normalized[:40] or "agent"

    @staticmethod
    def _decode_output(output: bytes | None) -> str:
        """
        将 Docker返回的字节输出转换为字符串

        errors="replace"可以避免工具输出包含非 UTF-8字节时
        导致整个 Agent流程异常
        """

        if output is None:
            return ""

        return output.decode(
            "utf-8",
            errors="replace",
        )

class _BoundedByteBuffer:
    """
    有界字节缓冲区。

    Docker 命令可能产生几十 MB 甚至更多输出。如果先完整读取再截断，
    宿主机仍然会占用大量内存。

    这个缓冲区只保留：
    - 输出开头的一半
    - 输出结尾的一半
    """

    def __init__(self, limit: int) -> None:
        if limit < 2:
            raise ValueError("buffer limit must be at least 2")

        self.limit = limit
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit

        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0

    def append(self, data: bytes | None) -> None:
        """追加一段 Docker 输出，但不会让缓冲区无限增长。"""

        if not data:
            return

        self.total_bytes += len(data)
        remaining = data

        if len(self.head) < self.head_limit:
            take_count = min(
                self.head_limit - len(self.head),
                len(remaining),
            )
            self.head.extend(remaining[:take_count])
            remaining = remaining[take_count:]

        if remaining:
            self.tail.extend(remaining)

            if len(self.tail) > self.tail_limit:
                del self.tail[:-self.tail_limit]

    def render(self) -> tuple[bytes, bool, int]:
        """返回最终字节内容、是否截断，以及截断前的总字节数。"""

        if self.total_bytes <= self.limit:
            return bytes(self.head + self.tail), False, self.total_bytes

        omitted = (
            self.total_bytes
            - len(self.head)
            - len(self.tail)
        )

        marker = (
            f"\n...[{omitted} bytes omitted by Sandbox Runtime]...\n"
        ).encode("utf-8")

        return bytes(self.head) + marker + bytes(self.tail), True, self.total_bytes
