"""Confirmed D9-3 profile. Never loads dotenv, pulls images or prepares networks."""

import json
import os
import time

import docker
from docker.errors import DockerException
from types import SimpleNamespace

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.workspace import D8DockerWorkspace
from Sandbox.docker_runtime import DockerRuntime, DockerRuntimeError
from Sandbox.settings import SandboxSettings
from Sandbox.windows_sandbox import WindowsSandboxSettings, WindowsTaskRunSandbox


STARTUP_READY_TIMEOUT_SECONDS = 10.0


IMAGE_REF = "qinyuan/pentest-sandbox:0.3.7"
IMAGE_ID = "sha256:f081157d62066f46141597c5ea191136e44e67253ed6c11b5b27e1ce7f46e4af"

WINDOWS_IMAGE_REF = "qinyuan/windows-sandbox:0.3.8"
WINDOWS_IMAGE_ID = "sha256:3cab772fadaeadfbe3629ada36a4e44c60841bf0da5429e55d63e1944c87118f"
WINDOWS_PROFILE = "windows-wine-v2"


def settings():
    return SandboxSettings(image=IMAGE_ID)


def windows_settings():
    return WindowsSandboxSettings(image=WINDOWS_IMAGE_ID)


def _active_profile():
    return os.environ.get("QINYUAN_SANDBOX_PROFILE", "linux").strip().lower()


def sandbox_config():
    # Freeze the complete effective container specification, not a hand-written
    # subset that could conceal new default mounts, network or resource limits.
    if _active_profile() == "windows":
        return windows_sandbox_config()
    configured = settings()
    profile = T6TaskRunSandbox(SimpleNamespace(settings=configured))
    return {"version": 1, "profile": "d3-fixed-readonly-v2", "image_ref": IMAGE_REF,
            "image_id": IMAGE_ID, "docker_host": configured.docker_host,
            "spec": profile._spec()}


def windows_sandbox_config():
    configured = windows_settings()
    profile = WindowsTaskRunSandbox(SimpleNamespace(settings=configured))
    return {"version": 1, "profile": WINDOWS_PROFILE, "image_ref": WINDOWS_IMAGE_REF,
            "image_id": WINDOWS_IMAGE_ID, "docker_host": configured.docker_host,
            "spec": profile._spec()}


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
            cold = (isinstance(error, DockerException) and any(word in detail for word in
                    ("connection refused", "connection aborted", "filenotfounderror", "winerror 2", "winerror 10061"))
                    and not any(word in detail for word in ("access denied", "permission denied", "winerror 5")))
            denied = isinstance(error, PermissionError) or any(word in detail for word in
                ("access denied", "permission denied", "winerror 5"))
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
    if _active_profile() == "windows":
        return _connect_profile(
            windows_sandbox_config(),
            windows_settings(),
            WindowsTaskRunSandbox,
            configuration,
            directory,
            wait_for_ready,
        )
    return _connect_profile(
        sandbox_config(),
        settings(),
        T6TaskRunSandbox,
        configuration,
        directory,
        wait_for_ready,
    )

