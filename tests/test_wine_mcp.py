"""Planner checks for the Wine MCP and the Windows profile."""

import pytest

from MCPs.Wine_Mcp.server import (
    wine_capture,
    wine_debug,
    wine_init,
    wine_run,
    wine_stop,
)
from Sandbox.command_sandbox import T6TaskRunSandbox
from Sandbox.settings import SandboxSettings
from Sandbox.windows_sandbox import WindowsSandboxSettings, WindowsTaskRunSandbox


class RuntimeStub:
    def __init__(self, settings):
        self.settings = settings


def test_windows_profile_has_bigger_resources_and_wine_prefix():
    spec = WindowsTaskRunSandbox(RuntimeStub(WindowsSandboxSettings()))._spec()

    assert spec["mem_limit"] == "2g"
    assert spec["memswap_limit"] == "2g"
    assert spec["nano_cpus"] == 1_500_000_000
    assert spec["pids_limit"] == 256
    assert "size=1g" in spec["tmpfs"]["/workspace/work"]
    assert spec["environment"]["WINEPREFIX"] == "/workspace/work/.wine"
    assert spec["environment"]["WINEARCH"] == "win64"
    # the default profile is untouched
    default = T6TaskRunSandbox(RuntimeStub(SandboxSettings()))._spec()
    assert default["mem_limit"] == "512m"
    assert "WINEPREFIX" not in default["environment"]


def test_wine_init_and_run_plans():
    init = wine_init()
    assert init["sandbox_command"]["tool"] == "wine"
    assert init["sandbox_command"]["arguments"] == ["wineboot", "-u"]

    run = wine_run("/workspace/input/hello.exe", ["arg1"])
    assert run["sandbox_command"]["arguments"] == ["/workspace/input/hello.exe", "arg1"]
    with pytest.raises(ValueError):
        wine_run("/etc/passwd")
    with pytest.raises(ValueError):
        wine_run("/workspace/work/a.exe", ["x" * 4096])


def test_wine_debug_uses_a_tmux_session():
    plan = wine_debug("/workspace/work/hello.exe", name="wine")
    args = plan["sandbox_command"]["arguments"]
    assert plan["sandbox_command"]["tool"] == "tmux"
    assert args[:4] == ["new-session", "-d", "-s", "qy-wine"]
    assert "/usr/bin/wine" in args
    assert "winedbg" in args


def test_wine_session_helpers_are_bounded():
    assert wine_capture(name="wine")["sandbox_command"]["tool"] == "tmux"
    with pytest.raises(ValueError):
        wine_capture(lines=1)
    assert wine_stop()["sandbox_command"]["arguments"] == ["kill-session", "-t", "qy-wine"]

def test_profile_dispatch_by_environment(monkeypatch):
    from agent_adapters import docker_profile

    monkeypatch.delenv("QINYUAN_SANDBOX_PROFILE", raising=False)
    assert docker_profile.sandbox_config()["profile"] == "d3-fixed-readonly-v2"

    monkeypatch.setenv("QINYUAN_SANDBOX_PROFILE", "windows")
    assert docker_profile.sandbox_config()["profile"] == "windows-wine-v1"


