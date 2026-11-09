"""Confirmed D9-3 profiles. Never loads dotenv, pulls images or prepares networks."""

import json
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace

import docker
from docker.errors import DockerException

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.workspace import D8DockerWorkspace
from Sandbox.docker_runtime import DockerRuntime, DockerRuntimeError
from Sandbox.settings import LinuxHeavySandboxSettings, SandboxSettings
from Sandbox.windows_sandbox import WindowsSandboxSettings, WindowsTaskRunSandbox


STARTUP_READY_TIMEOUT_SECONDS = 10.0


IMAGE_REF = "qinyuan/pentest-sandbox:0.3.8"
IMAGE_ID = "sha256:bb71169cd4dc94a43f9d5b2609113da1dcaa49ad94fab410378c03d6407aa164"

WINDOWS_IMAGE_REF = "qinyuan/windows-sandbox:0.3.9"
WINDOWS_IMAGE_ID = "sha256:141d9534ad0459238b9b0223abf45772a8ae547eb027bcfddc08c8eeb66893b7"


@dataclass(frozen=True)
class _Profile:
    label: str
    image_ref: str
    image_id: str
    settings: type[SandboxSettings]
    sandbox: type


# One table, one lookup: resource numbers live in the settings classes and the
# frozen label changes whenever any of them does (an old run then fails with
# configuration_mismatch instead of silently reusing different limits).
_PROFILES = {
    "linux": _Profile(
        "d3-fixed-readonly-v4",
        IMAGE_REF,
        IMAGE_ID,
        SandboxSettings,
        T6TaskRunSandbox,
    ),
    "linux-heavy": _Profile(
        "linux-heavy-v1",
        IMAGE_REF,
        IMAGE_ID,
        LinuxHeavySandboxSettings,
        T6TaskRunSandbox,
    ),
    "windows": _Profile(
        "windows-wine-v3",
        WINDOWS_IMAGE_REF,
        WINDOWS_IMAGE_ID,
        WindowsSandboxSettings,
        WindowsTaskRunSandbox,
    ),
}


def _profile(name: str | None = None) -> _Profile:
    configured = os.environ.get("QINYUAN_SANDBOX_PROFILE", "linux")
    if name is None:
        from agent_adapters.launcher_settings import ROOT, env_values
        values = env_values(ROOT / ".env")
        if values.get("QINYUAN_DESKTOP_SETTINGS") == "1":
            configured = values.get("QINYUAN_SANDBOX_PROFILE") or configured
    key = (name or configured).strip().lower()
    try:
        return _PROFILES[key]
    except KeyError:
        # A typo must not fall back to the default profile.
        raise ValueError(f"unknown QINYUAN_SANDBOX_PROFILE: {key}") from None


def _settings(profile: _Profile) -> SandboxSettings:
    return profile.settings(image=profile.image_id)


def _sandbox_config(profile: _Profile) -> dict:
    configured = _settings(profile)
    # Freeze the complete effective container specification, not a hand-written
    # subset that could conceal new default mounts, network or resource limits.
    return {
        "version": 1,
        "profile": profile.label,
        "image_ref": profile.image_ref,
        "image_id": profile.image_id,
        "docker_host": configured.docker_host,
        "spec": profile.sandbox(SimpleNamespace(settings=configured))._spec(),
    }


def settings() -> SandboxSettings:
    return _settings(_profile())


def windows_settings() -> SandboxSettings:
    return _settings(_profile("windows"))


def sandbox_config() -> dict:
    return _sandbox_config(_profile())


def windows_sandbox_config() -> dict:
    return _sandbox_config(_profile("windows"))


def _wait_ready(configured):
    """Read-only daemon probes; never create containers or replay commands."""
    from qinyuan_core import get_logger
    log = get_logger("sys")
    started = time.monotonic()
    deadline = started + STARTUP_READY_TIMEOUT_SECONDS
    waiting = False
    while True:
        client = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:raise TimeoutError("Docker readiness deadline reached")
            client = docker.DockerClient(base_url=configured.docker_host, version="auto",
                timeout=min(configured.docker_connect_timeout_seconds, remaining),
                max_pool_size=configured.docker_max_pool_size)
            client.api.trust_env = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:raise TimeoutError("Docker readiness deadline reached")
            client.api.timeout = min(configured.docker_connect_timeout_seconds, remaining)
            if client.ping() is not True:raise DockerRuntimeError("docker_startup_invalid_response")
            log.info("docker.startup_ready", category="environment", elapsed_seconds=round(time.monotonic()-started,3))
            return
        except Exception as error:
            detail = str(error).lower()
            denied = isinstance(error, PermissionError) or any(word in detail for word in
                ("access denied", "permission denied", "winerror 5"))
            # 这里的 DockerException 基本都来自连接/命名管道：Docker Desktop
            # 的 VM 或引擎管道尚未就绪。Windows 的错误文本会本地化（中文系统上
            # 没有 "file not found" 这类英文子串），所以除权限拒绝外一律按冷启动
            # 重试到 deadline，而不是立刻判定为永久失败。
            cold = isinstance(error, DockerException) and not denied
            if denied or not cold and not DockerRuntime._is_transient(error):
                log.warning("docker.startup_failed", code="docker_startup_permission_denied" if denied else "docker_startup_failed",
                    category="environment", exception_type=type(error).__name__)
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("docker.startup_timeout", code="docker_startup_not_ready", category="environment",
                    timeout_seconds=STARTUP_READY_TIMEOUT_SECONDS, exception_type=type(error).__name__)
                raise DockerRuntimeError("docker_startup_not_ready: Docker engine was not ready within 10 seconds") from error
            if not waiting:
                log.info("docker.startup_wait", category="environment", timeout_seconds=STARTUP_READY_TIMEOUT_SECONDS)
                waiting = True
        finally:
            if client is not None:client.close()
        time.sleep(min(0.25, remaining))


def _connect_profile(expected, configured, sandbox_type, configuration, directory, wait_for_ready):
    if json.dumps(configuration, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("d9_docker_configuration_mismatch")
    if wait_for_ready:
        _wait_ready(configured)
    runtime = DockerRuntime(configured, prepare_network=False)
    sandbox = sandbox_type(runtime)
    try:
        return sandbox, D8DockerWorkspace(sandbox, directory)
    except BaseException:
        sandbox.close()
        raise


def connect(configuration, directory, *, wait_for_ready=False):
    profile = _profile()
    return _connect_profile(
        _sandbox_config(profile),
        _settings(profile),
        profile.sandbox,
        configuration,
        directory,
        wait_for_ready,
    )
