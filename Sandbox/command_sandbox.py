"""TaskRun Sandbox profile for an isolated command workspace."""

from __future__ import annotations

from uuid import UUID

from Sandbox.task_run_sandbox import TaskRunSandbox

REAPER_SCRIPT = """import os,time
while True:
    try: os.waitpid(-1,0)
    except ChildProcessError: time.sleep(0.05)
"""


class T6TaskRunSandbox(TaskRunSandbox):
    def __init__(self, runtime, *, private_network: str | None = None) -> None:
        super().__init__(runtime)
        if private_network is not None:
            prefix = "qinyuan-t6-in-"
            try:
                task_id = UUID(private_network[len(prefix) :])
            except (ValueError, AttributeError) as exc:
                raise ValueError("invalid_t6_private_network") from exc
            if (
                not private_network.startswith(prefix)
                or task_id.version != 4
                or private_network != prefix + str(task_id)
            ):
                raise ValueError("invalid_t6_private_network")
        self.private_network = private_network

    def configure_target_network(self, task_run_id: str, binding) -> None:
        self._validate_task_id(task_run_id)
        if binding.target_url is None:
            raise ValueError("invalid_t6_target_network_binding")
        name = "qinyuan-t6-in-" + task_run_id
        with self._lock:
            if self._attempted or self._bindings or self._closed:
                raise ValueError("t6_sandbox_already_bound")
            if self.private_network is not None and self.private_network != name:
                raise ValueError("t6_private_network_conflict")
            self.private_network = name

    def _spec(self) -> dict[str, object]:
        spec = super()._spec()
        # Timeout cleanup kills orphaned model processes too. PID 1 must reap
        # their exit status so repeated steps cannot exhaust the process limit.
        spec["command"] = ["python3", "-I", "-c", REAPER_SCRIPT]
        if self.private_network is not None:
            spec["network_mode"] = self.private_network
        # 覆盖父类的 tmpfs 表，所以必须把 /tmp 一并带回来：根文件系统只读时，
        # 缺了它任何默认写临时目录的工具都会拿到 EROFS。
        spec["environment"] = {
            "PATH": "/tools/bin:/opt/dirsearch/bin:/opt/microsoft/powershell/7:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "/tools/python:/opt/qinyuan/python",
            "QINYUAN_OFFLINE_TOOL_INSTALL": "1",
        }
        spec["tmpfs"] = {
            "/tools": "rw,exec,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700",
            "/workspace": (
                f"rw,noexec,nosuid,nodev,size={self._settings.workspace_size},"
                "uid=0,gid=0,mode=0755"
            ),
            "/workspace/work": (
                f"rw,exec,nosuid,nodev,size={self._settings.workspace_size},"
                "uid=10001,gid=10001,mode=0700"
            ),
            "/tmp": (
                f"rw,noexec,nosuid,nodev,size={self._settings.temporary_size},"
                "mode=1777"
            ),
        }
        return spec

    def execute_d7_http(
        self,
        sandbox_id: str,
        *,
        task_run_id: str,
        run_config,
        authorization,
        network_binding,
        target_network,
        method: str,
        path_query: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        timeout_seconds: float,
    ):
        """Send one frozen D7 HTTP action through this TaskRun's forwarder."""
        from Sandbox.http_runtime import D7SandboxHttpRuntime

        return D7SandboxHttpRuntime(self).execute(
            sandbox_id,
            task_run_id=task_run_id,
            run_config=run_config,
            authorization=authorization,
            network_binding=network_binding,
            target_network=target_network,
            method=method,
            path_query=path_query,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
