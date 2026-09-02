from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import RLock

from Sandbox.contracts import SandboxRuntime
from Sandbox.models import (
    CommandRequest,
    CommandResult,
    SandboxMounts,
    SandboxSession,
    SandboxStatus,
)
from Sandbox.settings import SandboxSettings

class SandboxError(RuntimeError):
    pass

class SandboxNotFoundError(SandboxError):
    pass

class SandboxPolicyError(SandboxError):
    pass

class SandboxService:
    """
    一个 Agent对应一个独立沙盒。

    Docker 的创建、执行和销毁由 SandboxRuntime实现。
    SandboxService只负责 Agent与沙盒的生命周期映射。
    """

    def __init__(
        self,
        runtime: SandboxRuntime,
        settings: SandboxSettings | None = None,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or SandboxSettings()

        self._sessions: dict[str,SandboxSession] = {}     
        self._agent_locks: dict[str, RLock] = {}

        self._registry_lock = RLock()
        self._closed = False

    def start_for_agent(
        self,
        agent_id: str,
        mounts: SandboxMounts | None = None,
    ) -> SandboxSession:
        """
        为一个 Agent 创建或获取专属沙盒。

        不在 _registry_lock 中创建 Docker 容器，因此不同 Agent 的容器
        可以并行启动。

        同一个 Agent 使用自己的 agent_lock，避免并发创建两个容器。
        """

        normalized_agent_id = agent_id.strip()

        if not normalized_agent_id:
            raise SandboxError("agent_id cannot be empty")
        configured_mounts = mounts or SandboxMounts()

        with self._registry_lock:
            if self._closed:
                raise SandboxError("SandboxService is closed")

            # 生命周期锁在 Agent 首次出现时创建，并保留到 Service 关闭。
            agent_lock = self._agent_locks.setdefault(
                normalized_agent_id,
                RLock(),
            )

        # 这里只锁定当前 Agent，不阻塞其他 Agent 创建容器。
        with agent_lock:
            with self._registry_lock:
                if self._closed:
                    raise SandboxError("SandboxService is closed")

                existing_session = self._sessions.get(
                    normalized_agent_id
                )

                if (
                    existing_session is not None
                    and existing_session.status == SandboxStatus.RUNNING
                ):
                    if existing_session.mounts != configured_mounts:
                        raise SandboxError(
                            "cannot change mounts for a running sandbox"
                        )
                    return existing_session

            # Docker 创建容器可能较慢，不能持有全局 registry lock。
            sandbox_id = self.runtime.create(normalized_agent_id, configured_mounts)

            try:
                with self._registry_lock:
                    # 创建容器期间 Service 可能被其他线程关闭。
                    if self._closed:
                        raise SandboxError(
                            "SandboxService was closed while the sandbox "
                            "was being created"
                        )

                    session = SandboxSession(
                        agent_id=normalized_agent_id,
                        sandbox_id=sandbox_id,
                        status=SandboxStatus.RUNNING,
                        mounts=configured_mounts,
                    )

                    self._sessions[normalized_agent_id] = session
                    return session

            except Exception:
                # 容器已创建但无法注册时，必须回收，避免孤儿容器。
                try:
                    self.runtime.destroy(sandbox_id)
                except Exception:
                    pass
                raise

    def execute(
        self,
        agent_id: str,
        request: CommandRequest,
    ) -> CommandResult:
        self._validate_command(request)

        with self._registry_lock:
            session = self._sessions.get(agent_id)
            agent_lock = self._agent_locks.get(agent_id)

        if session is None or agent_lock is None:
            raise SandboxNotFoundError(
                f"Agent {agent_id!r} does not have a sandbox"
            )

        # An interrupted command may still own agent_lock while Docker drains
        # its output stream. Reject the quarantined sandbox before waiting for
        # that lock so later tool calls cannot trigger another host timeout.
        if session.status != SandboxStatus.RUNNING:
            raise SandboxError(
                f"Sandbox {session.sandbox_id!r} is not running"
            )

        with agent_lock:
            with self._registry_lock:
                current_session = self._sessions.get(agent_id)

            if(
                current_session is None
                or current_session.sandbox_id != session.sandbox_id
            ):
                raise SandboxNotFoundError(
                    f"Sandbox for agent {agent_id!r} was removed"
                )

            if current_session.status != SandboxStatus.RUNNING:
                raise SandboxError(
                    f"Sandbox {current_session.sandbox_id!r} is not running"
                )
            
            return self.runtime.execute(
                sandbox_id=session.sandbox_id,
                request=request,
            )

    def stop_for_agent(self, agent_id: str) -> None:
        """停止并删除指定 Agent 的容器。"""

        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            return

        with self._registry_lock:
            agent_lock = self._agent_locks.get(normalized_agent_id)

        if agent_lock is None:
            return

        with agent_lock:
            with self._registry_lock:
                session = self._sessions.get(normalized_agent_id)

            if session is None:
                return

            # 先销毁容器。销毁失败时保留 session，便于再次清理。
            self.runtime.destroy(session.sandbox_id)

            with self._registry_lock:
                current = self._sessions.get(normalized_agent_id)

                if (
                    current is not None
                    and current.sandbox_id == session.sandbox_id
                ):
                    self._sessions.pop(normalized_agent_id, None)

    def interrupt_for_agent(self, agent_id: str) -> str | None:
        """Abort an in-flight command without deleting its active output stream.

        This deliberately does not acquire the per-Agent lock: execute() holds
        that lock for the lifetime of a Docker output stream, so waiting for it
        would make cancellation depend on the operation being cancelled.
        """

        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            return None

        with self._registry_lock:
            current = self._sessions.get(normalized_agent_id)
            if current is None:
                return None
            if current.status != SandboxStatus.RUNNING:
                return current.sandbox_id
            self._sessions[normalized_agent_id] = replace(
                current,
                status=SandboxStatus.STOPPED,
            )

        # Stop first so Docker can close the exec stream. Deletion is performed
        # later by the normal lifecycle path after execute() releases its lock.
        self.runtime.interrupt(current.sandbox_id)
        return current.sandbox_id

    def get_session(self,agent_id: str) -> SandboxSession | None :
        with self._registry_lock:
            return self._sessions.get(agent_id)

    def stop_all(self) -> None:
        """
        停止所有 Agent 沙盒。

        使用 _agent_locks 而不是仅使用 _sessions，
        这样 close() 也会等待正在创建容器的 Agent。
        """

        with self._registry_lock:
            agent_ids = tuple(self._agent_locks.keys())

        errors: list[str] = []

        for agent_id in agent_ids:
            try:
                self.stop_for_agent(agent_id)
            except Exception as error:
                errors.append(f"{agent_id}: {error}")

        if errors:
            raise SandboxError(
                "Failed to stop one or more sandboxes: "
                + "; ".join(errors)
            )

    def close(self) -> None:
        """停止全部容器并关闭 Docker Client。"""

        with self._registry_lock:
            if self._closed:
                return

            # 先禁止创建新的 Agent 沙盒。
            self._closed = True

        errors: list[str] = []

        try:
            self.stop_all()
        except Exception as error:
            errors.append(str(error))

        try:
            self.runtime.close()
        except Exception as error:
            errors.append(f"failed to close sandbox runtime: {error}")

        with self._registry_lock:
            self._sessions.clear()
            self._agent_locks.clear()

        if errors:
            raise SandboxError("; ".join(errors))

    def detach(self) -> None:
        """Close this process' runtime connection without destroying sandboxes."""

        with self._registry_lock:
            if self._closed:
                return
            self._closed = True

        try:
            self.runtime.close()
        finally:
            with self._registry_lock:
                self._sessions.clear()
                self._agent_locks.clear()

    def _validate_command(self,request: CommandRequest) -> None:

        tool_name = request.tool.strip()

        if not tool_name:
            raise SandboxPolicyError("Tool cannot be empty")

        if(
            Path(tool_name).name != tool_name
            or "/" in tool_name
            or "\\" in tool_name
        ):
            raise SandboxPolicyError(
                "Tool must be a command name, not a filesystem path"
            )

        if tool_name not in self.settings.allowed_tools:
            raise SandboxPolicyError(
                f"Tool {tool_name!r} is not allowed"
            )

        if not (
            1
            <=request.timeout_seconds
            <=self.settings.maximum_timeout_seconds
        ):
            raise SandboxPolicyError(
                "Command timeout is outside the allowed range"
            )

        if any(
            not isinstance(argument,str)
            for argument in request.arguments
        ):
            raise SandboxPolicyError(
                "Every command argument must be a string"
            )

        if any(
            "\x00" in argument
            for argument in request.arguments
        ):
            raise SandboxPolicyError(
                "Command arguments cannot contain null bytes"
            )
