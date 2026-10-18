"""Confirmed D9-3 profile. Never loads dotenv, pulls images or prepares networks."""

import json
from types import SimpleNamespace

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.workspace import D8DockerWorkspace
from Sandbox.docker_runtime import DockerRuntime
from Sandbox.settings import SandboxSettings


IMAGE_REF = "qinyuan/pentest-sandbox:0.3.4"
IMAGE_ID = "sha256:e38c60b0111ce3f9a478f9b1b09e67fa5f07f15e0ff018b62dfcfee789a26581"


def settings():
    return SandboxSettings(image=IMAGE_ID)


def sandbox_config():
    # Freeze the complete effective container specification, not a hand-written
    # subset that could conceal new default mounts, network or resource limits.
    configured = settings()
    profile = T6TaskRunSandbox(SimpleNamespace(settings=configured))
    return {"version": 1, "profile": "d3-fixed-readonly-v1", "image_ref": IMAGE_REF,
            "image_id": IMAGE_ID, "docker_host": configured.docker_host,
            "spec": profile._spec()}


def connect(configuration, directory):
    if json.dumps(configuration, sort_keys=True) != json.dumps(sandbox_config(), sort_keys=True):
        raise ValueError("d9_docker_configuration_mismatch")
    runtime = DockerRuntime(settings(), prepare_network=False)
    sandbox = T6TaskRunSandbox(runtime)
    try:
        return sandbox, D8DockerWorkspace(sandbox, directory)
    except BaseException:
        sandbox.close()
        raise
