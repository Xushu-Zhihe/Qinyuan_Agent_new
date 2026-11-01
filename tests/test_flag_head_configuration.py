"""Declared submission head: challenge storage, goal plumbing and competition defaults."""

import sqlite3

import pytest

from agent_adapters.competition_schedule import _flag_head
from agent_adapters.workbench import Workbench, WorkbenchError
from agent_adapters.workbench_runner import challenge_goal


def make_workbench(tmp_path):
    return Workbench(tmp_path, protocol_version=3, execution_profile="d8_v22", d7_mode="simulated")


def challenge_value(**extra):
    value = dict(title="head test", category="pwn", description="find the flag",
        deadline_minutes=60, step_timeout_seconds=60)
    value.update(extra)
    return value


def test_challenge_flag_head_is_stored_and_rendered_in_the_goal(tmp_path):
    workbench = make_workbench(tmp_path)
    created = workbench.create(challenge_value(flag_head="NSSCTF"))
    assert created["flag_head"] == "NSSCTF"
    assert "提交 flag 头：NSSCTF" in challenge_goal(created)
    plain = workbench.create(challenge_value())
    assert plain["flag_head"] == "" and "提交 flag 头" not in challenge_goal(plain)
    updated = workbench.update(created["id"], challenge_value(flag_head="flag"))
    assert updated["flag_head"] == "flag"
    assert "提交 flag 头：flag" in challenge_goal(updated)


@pytest.mark.parametrize("value", ["NSS CTF", "NSSCTF{}", "9CTF", "x" * 33])
def test_invalid_flag_head_is_rejected(tmp_path, value):
    workbench = make_workbench(tmp_path)
    with pytest.raises(WorkbenchError, match="invalid_flag_head"):
        workbench.create(challenge_value(flag_head=value))


def test_old_challenge_table_gets_the_flag_head_column(tmp_path):
    path = tmp_path / "workbench.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE challenges (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
                description TEXT NOT NULL, endpoint TEXT NOT NULL,
                deadline_minutes INTEGER NOT NULL DEFAULT 60,
                step_timeout_seconds INTEGER NOT NULL DEFAULT 60,
                status TEXT NOT NULL DEFAULT 'draft', archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE attachments (
                id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, name TEXT NOT NULL,
                byte_size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
    workbench = make_workbench(tmp_path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(challenges)")}
    assert "flag_head" in columns
    assert workbench.create(challenge_value(flag_head="NSSCTF"))["flag_head"] == "NSSCTF"


def test_competition_flag_head_defaults_and_explicit_empty():
    assert _flag_head(None, "arena") == "NSSCTF"
    assert _flag_head(None, "benchmark") == "flag"
    assert _flag_head("", "arena") == ""       # explicit empty disables substitution
    assert _flag_head(" CTF ", "arena") == "CTF"
    for value in ("bad head", "flag{}", "9flag", "x" * 33):
        with pytest.raises(WorkbenchError, match="competition_session_invalid"):
            _flag_head(value, "arena")
