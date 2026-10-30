"""按通道分类的结构化日志。

通道固定五个，对应排查时最先要分的五类问题：

    sys    启动、配置、环境
    ctrl   编排与状态机（Turn / Intent / 预算 / 停止）
    model  模型调用与契约校验
    exec   沙箱、命令、MCP 工具
    store  持久化、证据、文件

每条记录固定带 channel 与 event；有实体时带 run / turn / intent / exec / fact 的 ID，
有错误时带 code。console 输出人类可读单行，jsonl 输出机器可读行——两者同一份数据。

额外字段统一放在 LogRecord 的 qy 字典里，避免与 logging 保留属性撞名。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import Category, Layer, QinyuanError

CHANNELS = ("sys", "ctrl", "exec", "model", "store")
ID_FIELDS = ("run", "turn", "intent", "exec", "fact")
ROOT = "qinyuan"

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("qinyuan_log_context", default={})


class UnknownChannel(QinyuanError):
    category = Category.INPUT
    layer = Layer.CTRL
    code = "log.unknown_channel"


def bind(**ids: Any):
    """在当前执行上下文里绑定 run/turn/intent 等 ID，返回用于还原的 token。"""
    merged = dict(_CONTEXT.get())
    merged.update(ids)
    return _CONTEXT.set(merged)


def unbind(token) -> None:
    _CONTEXT.reset(token)


def _payload(record: logging.LogRecord) -> dict[str, Any]:
    return getattr(record, "qy", None) or {"channel": "sys", "event": record.getMessage()}


class _Text(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = _payload(record)
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        parts = [f"{stamp}.{int(record.msecs):03d}", f"{record.levelname:<5}", str(d.get("channel", "-")), str(d.get("event", "-"))]
        for key in ID_FIELDS:
            value = d.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        for key in ("operation", "request", "phase", "category", "code", "branch", "event_index",
                    "field_path", "observed_type", "finish_seen", "done_seen", "issue_codes", "in_flight", "error_type", "action_count",
                    "record_kind", "record_id", "actual_bytes", "limit_bytes", "dispatched", "fact_source", "operation_admission"):
            value = d.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        if d.get("msg"):
            parts.append(str(d["msg"]))
        return " ".join(parts)


class _Json(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = _payload(record)
        out: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "channel": d.get("channel", "sys"),
            "event": d.get("event", record.getMessage()),
        }
        for key, value in d.items():
            if key not in ("channel", "event") and value not in (None, ""):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info).splitlines()[0]
        return json.dumps(out, ensure_ascii=False)


class Channel:
    """一个通道的写入口。level 由 logger 统一过滤。"""

    __slots__ = ("name", "_logger")

    def __init__(self, name: str) -> None:
        self.name = name
        self._logger = logging.getLogger(f"{ROOT}.{name}")

    def _emit(self, level: int, event: str, fields: dict[str, Any]) -> None:
        record: dict[str, Any] = {"channel": self.name, "event": event}
        record.update(_CONTEXT.get())
        record.update(fields)
        self._logger.log(level, event, extra={"qy": record}, stacklevel=3)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        """记录一条带堆栈的 error；只在 except 块里用。"""
        record: dict[str, Any] = {"channel": self.name, "event": event}
        record.update(_CONTEXT.get())
        record.update(fields)
        self._logger.error(event, exc_info=True, extra={"qy": record}, stacklevel=3)


def get_logger(channel: str) -> Channel:
    if channel not in CHANNELS:
        raise UnknownChannel(f"未知日志通道: {channel!r}，可选 {CHANNELS}")
    return Channel(channel)


def configure(
    *,
    level: str = "INFO",
    console: bool = True,
    jsonl: str | Path | None = None,
) -> None:
    """装配日志出口。入口处调用一次。

    没有任何出口时挂 NullHandler，避免 logging.lastResort 往 stderr 打字——
    CLI 的 stdout 是 JSON 结果、stderr 必须为空，这是既有契约。
    """
    logger = logging.getLogger(ROOT)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False
    if console:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_Text())
        logger.addHandler(handler)
    if jsonl:
        path = Path(jsonl)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(_Json())
        logger.addHandler(handler)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())


@contextmanager
def timed_phase(channel: str, phase: str, **ids):
    """Inclusive local phase timing; nested intervals must not be added as wall time."""
    started_at, started = time.time(), time.perf_counter()
    outcome, error_type = "completed", None
    try:
        yield
    except BaseException as exc:
        outcome, error_type = "failed", type(exc).__name__
        raise
    finally:
        get_logger(channel).info("phase.finished", phase=phase, category="performance",
            started_at=started_at, finished_at=time.time(),
            seconds=round(time.perf_counter()-started, 6), timing_basis="inclusive",
            outcome=outcome, error_type=error_type, **ids)
