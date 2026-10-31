"""T6 filesystem/security profile must preserve the isolated T5 profile."""

import json
from uuid import uuid4

import pytest

from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.seccomp_profile import analysis_seccomp_profile
from Sandbox.settings import SandboxSettings
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
    assert t6["tmpfs"]["/tmp"].startswith("rw,noexec,nosuid,nodev")
    assert t6["tmpfs"]["/home/sandbox"].startswith("rw,")
    assert "uid=10001,gid=10001,mode=0700" in t6["tmpfs"]["/home/sandbox"]
    assert t6["environment"]["HOME"] == "/home/sandbox"
    assert t6["environment"]["LANG"] == "C.UTF-8"
    assert t6["environment"]["TERM"]
    assert "/opt/qinyuan/python/bin" in t6["environment"]["PATH"]
    assert "Mounts" not in t6 and "volumes" not in t6
    assert t5["tmpfs"] != t6["tmpfs"]


def test_only_the_command_sandbox_relaxes_personality_for_aslr():
    runtime = RuntimeStub()
    t5 = TaskRunSandbox(runtime)._spec()
    t6 = T6TaskRunSandbox(runtime)._spec()

    assert t5["security_opt"] == ["no-new-privileges:true"]
    assert "no-new-privileges:true" in t6["security_opt"]
    assert any(option.startswith("seccomp=") for option in t6["security_opt"])
    assert not any(option.startswith("seccomp=") for option in t5["security_opt"])


def test_analysis_seccomp_profile_keeps_default_deny_and_adds_aslr():
    profile = json.loads(analysis_seccomp_profile())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["defaultErrnoRet"] == 1
    values = {
        rule["args"][0]["value"]
        for rule in profile["syscalls"]
        if rule.get("names") == ["personality"] and rule.get("args")
    }
    assert {262144, 262152} <= values
    # The stock socket restrictions are still present.
    assert any(rule.get("names") == ["socket"] for rule in profile["syscalls"])


def test_t6_network_name_is_exact_and_part_of_spec():
    name = "qinyuan-t6-in-" + str(uuid4())
    assert T6TaskRunSandbox(RuntimeStub(), private_network=name)._spec()["network_mode"] == name
    with pytest.raises(ValueError, match="invalid_t6_private_network"):
        T6TaskRunSandbox(RuntimeStub(), private_network="bridge")

