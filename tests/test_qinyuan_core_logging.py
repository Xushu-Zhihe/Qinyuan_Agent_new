"""日志分类的快速单测：写到 tmp_path，不产生仓库内产物。"""

from __future__ import annotations

import json
import logging

import pytest

from qinyuan_core import CHANNELS, bind, configure, get_logger, unbind
from qinyuan_core.logging import UnknownChannel


def _close() -> None:
    logger = logging.getLogger("qinyuan")
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
    logger.handlers.clear()


def test_unknown_channel_is_classified():
    with pytest.raises(UnknownChannel) as info:
        get_logger("nope")
    assert info.value.code == "log.unknown_channel"
    assert info.value.category.value == "input"


def test_channel_set_is_small_and_fixed():
    assert CHANNELS == ("sys", "ctrl", "exec", "model", "store")


def test_jsonl_carries_channel_event_ids_and_code(tmp_path):
    path = tmp_path / "run.jsonl"
    configure(console=False, jsonl=path)
    log = get_logger("exec")
    log.info("command.done", exec="e1", intent="i1")
    log.error("command.failed", exec="e2", code="exec.timeout")
    _close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["channel"] for row in rows] == ["exec", "exec"]
    assert [row["event"] for row in rows] == ["command.done", "command.failed"]
    assert rows[0]["exec"] == "e1" and rows[0]["intent"] == "i1"
    assert rows[1]["code"] == "exec.timeout"
    assert rows[1]["level"] == "ERROR"


def test_context_binding_reaches_every_channel(tmp_path):
    path = tmp_path / "run.jsonl"
    configure(console=False, jsonl=path)
    token = bind(run="r1", turn="t1")
    try:
        get_logger("ctrl").info("turn.start")
        get_logger("store").info("fact.write", fact="f1")
    finally:
        unbind(token)
    _close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {(row["channel"], row["run"], row["turn"]) for row in rows} == {
        ("ctrl", "r1", "t1"),
        ("store", "r1", "t1"),
    }
    assert rows[1]["fact"] == "f1"


def test_level_filter_keeps_errors(tmp_path, capsys):
    path = tmp_path / "run.jsonl"
    configure(level="WARNING", console=True, jsonl=path)
    log = get_logger("model")
    log.info("should.drop")
    log.warning("model.retry", code="model.timeout")
    _close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["model.retry"]
    assert "model.retry" in capsys.readouterr().err


def test_stream_diagnostics_are_visible_in_console_and_jsonl(tmp_path, capsys):
    path = tmp_path / "run.jsonl"
    configure(console=True, jsonl=path)
    fields = dict(run="run1", request="request1", phase="stage_action", category="dependency",
        code="provider_stream_invalid", branch="tool_id_changed", event_index=3,
        field_path="$.choices[0].delta.tool_calls[0].id", observed_type="str", finish_seen=False, done_seen=False)
    get_logger("model").warning("model.stream_rejected", **fields)
    _close()
    text = capsys.readouterr().err
    row = json.loads(path.read_text(encoding="utf-8"))
    for key, value in fields.items():
        assert f"{key}={value}" in text and row[key] == value
