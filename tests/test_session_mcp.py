"""Planner-level checks for the tmux-backed interactive session MCP."""

import pytest

from MCPs.Session_Mcp.server import (
    session_capture,
    session_list,
    session_send,
    session_start,
    session_stop,
)


def test_start_uses_absolute_program_and_fixed_cwd():
    plan = session_start("dbg", "gdb", ["-q", "/workspace/work/t"])

    command = plan["sandbox_command"]
    assert command["tool"] == "tmux"
    assert command["arguments"][:6] == ["new-session", "-d", "-s", "qy-dbg", "-c", "/workspace/work"]
    assert command["arguments"][6:] == ["--", "/usr/bin/gdb", "-q", "/workspace/work/t"]


def test_start_rejects_bad_names_programs_and_cwd():
    with pytest.raises(ValueError):
        session_start("Bad Name", "gdb")
    with pytest.raises(ValueError):
        session_start("dbg", "/bin/sh")
    with pytest.raises(ValueError):
        session_start("dbg", "gdb", cwd="/tmp")
    with pytest.raises(ValueError):
        session_start("dbg", "gdb", cwd="/workspace/work/../input")


def test_send_is_literal_or_one_named_key():
    text = session_send("dbg", text="info registers rip")
    assert text["sandbox_command"]["arguments"][:5] == [
        "send-keys",
        "-t",
        "qy-dbg",
        "-l",
        "--",
    ]
    key = session_send("dbg", key="C-c")
    assert key["sandbox_command"]["arguments"] == ["send-keys", "-t", "qy-dbg", "C-c"]

    with pytest.raises(ValueError):
        session_send("dbg")
    with pytest.raises(ValueError):
        session_send("dbg", text="x", key="Enter")
    with pytest.raises(ValueError):
        session_send("dbg", key="F12")


def test_capture_is_bounded_and_list_stop_are_fixed():
    capture = session_capture("dbg", lines=200)
    assert capture["sandbox_command"]["arguments"][-2:] == ["-S", "-200"]
    with pytest.raises(ValueError):
        session_capture("dbg", lines=5000)

    assert session_list()["sandbox_command"]["tool"] == "tmux"
    assert session_stop("dbg")["sandbox_command"]["arguments"] == [
        "kill-session",
        "-t",
        "qy-dbg",
    ]
