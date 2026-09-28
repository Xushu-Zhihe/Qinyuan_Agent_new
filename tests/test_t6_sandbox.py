"""T6 filesystem profile must preserve the isolated T5 profile."""

from uuid import uuid4

import pytest

from Sandbox.settings import SandboxSettings
from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.task_run_sandbox import TaskRunSandbox


class RuntimeStub:
    def __init__(self):
        self.settings = SandboxSettings()


def test_t6_profile_separates_immutable_input_from_executable_work():
    runtime = RuntimeStub()
    t5 = TaskRunSandbox(runtime)._spec()
    t6 = T6TaskRunSandbox(runtime)._spec()

    assert t5["tmpfs"]["/workspace"].startswith("rw,noexec")
    assert t6["read_only"] is True
    assert t6["network_mode"] == "none"
    assert t6["user"] == "10001:10001"
    assert t6["cap_drop"] == ["ALL"]
    assert t6["tmpfs"]["/workspace"].startswith("rw,noexec,nosuid,nodev")
    assert "uid=0,gid=0,mode=0755" in t6["tmpfs"]["/workspace"]
    assert t6["tmpfs"]["/workspace/work"].startswith("rw,exec,nosuid,nodev")
    assert "uid=10001,gid=10001,mode=0700" in t6["tmpfs"]["/workspace/work"]
    assert "/tmp" not in t6["tmpfs"]
    assert "Mounts" not in t6 and "volumes" not in t6
    assert t5["tmpfs"] != t6["tmpfs"]


def test_t6_network_name_is_exact_and_part_of_spec():
    name = "qinyuan-t6-in-" + str(uuid4())
    assert T6TaskRunSandbox(RuntimeStub(), private_network=name)._spec()["network_mode"] == name
    with pytest.raises(ValueError, match="invalid_t6_private_network"):
        T6TaskRunSandbox(RuntimeStub(), private_network="bridge")
