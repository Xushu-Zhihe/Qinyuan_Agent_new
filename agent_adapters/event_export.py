"""Read committed events; isolate file writes from CLI cancellation."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import signal
import sqlite3
import threading
import time
from pathlib import Path

from agent_adapters.run_files import encode
from agent_core.control_store import CONTROL_OPERATION_RULES
from agent_core.dual_llm_contracts import D3RunConfig
from agent_core.redaction import (
    D5Redactor,
    d5_sensitive_configuration_matches,
)
from agent_core.runtime import TaskRunStatus

_WITHHELD = "[WITHHELD]"
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12, 13, 14, 15}$"
)
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_EVENT_DETAIL_KEYS = {
    "operation",
    "entity_ids",
    "result",
    "reason",
    "import_id",
    "intent_id",
    "verified_fact_ids",
    "proof_ref",
    "path",
    "observed_type",
    "review_root_id",
    "review_generation",
    "review_id",
    "application_ref",
    "context_version",
    "constraint_version",
    "paused_gate",
    "turn_id",
    "review_scope",
    "action_candidate_id",
    "pause_reason",
    "withheld_fields",
}
_SAFE_OPERATIONS = frozenset(CONTROL_OPERATION_RULES)
_SAFE_EVENT_TYPES = frozenset(
    rule.event_type for rule in CONTROL_OPERATION_RULES.values()
)
_SAFE_RESULTS = frozenset(TaskRunStatus) | frozenset(
    result
    for rule in CONTROL_OPERATION_RULES.values()
    for result in rule.allowed_results
)
_SAFE_REASONS = {"single_intent_slice_finished"}
_SAFE_REVIEW_SCOPES = {"intent_end", "turn_end"}
_ID_EVENT_KEYS = {
    "entity_ids",
    "verified_fact_ids",
    "import_id",
    "intent_id",
    "review_root_id",
    "review_id",
    "turn_id",
    "action_candidate_id",
}
_DIGEST_EVENT_KEYS = {"context_version", "constraint_version", "sha256", "digest"}


def _read_d5_configuration(connection, task_run_id, supplied):
    """Return (is_d5, redactor-or-None), without exposing configuration errors."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='d3_runs'"
    ).fetchone()
    if table is None:
        return False, None
    row = connection.execute(
        "SELECT config_json FROM d3_runs WHERE task_run_id=?", (task_run_id,)
    ).fetchone()
    if row is None:
        return False, None
    try:
        config = D3RunConfig.from_dict(json.loads(row["config_json"]))
    except Exception:  # noqa: BLE001 -- corrupted declarations fail closed
        return True, None
    if not config.d5_enabled:
        return False, None
    requirements = config.d5_sensitive_requirements
    if requirements is None:
        return True, None
    configuration = supplied
    if configuration is None:
        from agent_adapters.evidence_query import sensitive_config_from_json

        configuration = sensitive_config_from_json(
            os.environ.get("QINYUAN_AGENT_D5_SENSITIVE_CONFIG")
        )
    if configuration is None and not requirements:
        configuration = {}
    if not d5_sensitive_configuration_matches(requirements, configuration):
        return True, None
    values = tuple(item.value for item in (configuration or {}).values())
    return True, D5Redactor(values)


def _safe_event_value(key, value):
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_safe_event_value(key, item) for item in value]
    if isinstance(value, dict):
        result = {}
        withheld_fields = 0
        for item_key, item in value.items():
            if item_key in _EVENT_DETAIL_KEYS:
                result[item_key] = _safe_event_value(item_key, item)
            else:
                withheld_fields += 1
        if withheld_fields:
            result["withheld_fields"] = withheld_fields
        return result
    if not isinstance(value, str):
        return _WITHHELD
    if key == "operation" and value in _SAFE_OPERATIONS:
        return value
    if key == "type" and value in _SAFE_EVENT_TYPES:
        return value
    if key == "status" and value in _SAFE_RESULTS:
        return value
    if key == "result" and value in _SAFE_RESULTS:
        return value
    if key == "reason" and value in _SAFE_REASONS:
        return value
    if key == "review_scope" and value in _SAFE_REVIEW_SCOPES:
        return value
    if key in _ID_EVENT_KEYS or key.endswith(("_id", "_sha256")) or key == "result":
        if _UUID.fullmatch(value) or _SHA256.fullmatch(value) or value.isdigit():
            return value
    if key == "proof_ref" and value.startswith("t6:") and _UUID.fullmatch(value[3:]):
        return value
    if key in _DIGEST_EVENT_KEYS and (_SHA256.fullmatch(value) or value.isdigit()):
        return value
    return _WITHHELD


def _project_event(event, *, is_d5, redactor):
    if not is_d5:
        return event
    if redactor is not None:
        projected = redactor.redact_value(event)
        return {
            **projected,
            "protocol_version": 2,
            "redaction_status": "available",
            "redaction_reason": None,
        }
    details = _safe_event_value("details", event["details"])
    return {
        **event,
        "protocol_version": 2,
        "redaction_status": "withheld",
        "redaction_reason": "sensitive_configuration_unavailable",
        "type": _safe_event_value("type", event["type"]),
        "status": _safe_event_value("status", event["status"]),
        "details": details,
    }


def read_events(
    database: Path, task_run_id, after_sequence=0, limit=128, *, sensitive_config=None
):
    if type(after_sequence) is not int or after_sequence < 0 or not 1 <= limit <= 512:
        raise ValueError("invalid_event_cursor")
    if not database.exists():
        return []
    with sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1
    ) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='checkpoints'"
        ).fetchone():
            return []
        is_d5, redactor = _read_d5_configuration(conn, task_run_id, sensitive_config)
        rows = conn.execute(
            "SELECT e.* FROM runtime_events e WHERE e.task_run_id=? AND e.sequence>? "
            "AND e.sequence<=(SELECT MAX(watermark_event_sequence) FROM checkpoints WHERE task_run_id=?) "
            "ORDER BY e.sequence LIMIT ?",
            (task_run_id, after_sequence, task_run_id, limit),
        ).fetchall()
        return [
            _project_event(
                {
                    "protocol_version": 1,
                    "task_run_id": row["task_run_id"],
                    "sequence": row["sequence"],
                    "type": row["type"],
                    "timestamp": row["timestamp"],
                    "status": row["status"],
                    "details": json.loads(row["details_json"]),
                },
                is_d5=is_d5,
                redactor=redactor,
            )
            for row in rows
        ]


def _existing_event_sequence(path, database, task_id):
    """Validate an existing JSONL file as a prefix of committed run events."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("event_export_path_invalid")
    if not path.exists():
        return 0
    sequence = 0
    with path.open("rb") as stream:
        while True:
            lines = []
            for _ in range(512):
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise ValueError("event_export_prefix_invalid")
                lines.append(line[:-1])
            if not lines:
                return sequence
            try:
                existing = [json.loads(line.decode("utf-8")) for line in lines]
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("event_export_prefix_invalid") from exc
            if any(not isinstance(item, dict) for item in existing):
                raise ValueError("event_export_prefix_invalid")
            page = read_events(
                Path(database),
                task_id,
                sequence,
                limit=len(existing),
            )
            if len(page) != len(existing) or existing != page:
                raise ValueError("event_export_prefix_invalid")
            sequence = page[-1]["sequence"]


def _event_writer(pipe, path, database, task_id, finish, *, append_existing):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Only small acknowledgements travel over IPC, never event batches.
        sequence = _existing_event_sequence(path, database, task_id) if append_existing else 0
        with Path(path).open("ab" if append_existing else "xb") as stream:
            pipe.send({"ready": True, "sequence": sequence})
            while True:
                pipe.send({"writing_at": time.monotonic()})
                batch = read_events(Path(database), task_id, sequence)
                if batch:
                    stream.writelines(encode(item) + b"\n" for item in batch)
                    stream.flush()
                    os.fsync(stream.fileno())
                    sequence = batch[-1]["sequence"]
                    pipe.send({"sequence": sequence})
                elif finish.is_set():
                    pipe.send({"done": True})
                    return
                else:
                    pipe.send({"sequence": sequence})
                    finish.wait(0.025)
    except (OSError, ValueError, sqlite3.Error):
        try:
            pipe.send({"error": "event_export_failed"})
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


def _writer(pipe, path, database, task_id, finish):
    _event_writer(pipe, path, database, task_id, finish, append_existing=False)


def _append_writer(pipe, path, database, task_id, finish):
    _event_writer(pipe, path, database, task_id, finish, append_existing=True)


class EventExporter:
    def __init__(
        self,
        path: Path,
        database: Path,
        task_id: str,
        *,
        worker=_writer,
        append_existing: bool = False,
    ):
        if append_existing and worker is not _writer:
            raise ValueError("append_existing_requires_default_writer")
        ctx = multiprocessing.get_context("spawn")
        self.pipe, child = ctx.Pipe(duplex=False)
        self.finish = ctx.Event()
        self.process = ctx.Process(
            target=_append_writer if append_existing else worker,
            args=(child, str(path), str(database), task_id, self.finish),
        )
        self.process.start()
        child.close()
        self.pending_at = time.monotonic()
        self.error = None
        self.sequence = 0
        self.done = False
        self.closed = False
        self._tick_lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor = None

    def start_monitoring(self):
        """Drain acknowledgements while a synchronous public application runs."""
        if self._monitor is not None or self.closed:
            raise ValueError("event_export_monitor_already_started")
        def monitor():
            while not self._monitor_stop.is_set() and not self.done and not self.error:
                self.tick()
                self._monitor_stop.wait(0.01)
        self._monitor = threading.Thread(target=monitor, daemon=True, name="event-export-monitor")
        self._monitor.start()

    def tick(self):
        with self._tick_lock:
            self._tick()

    def _tick(self):
        if self.error or self.done or self.closed:
            return
        try:
            while self.pipe.poll():
                response = self.pipe.recv()
                if "error" in response:
                    self.error = response["error"]
                    self.close()
                    return
                if "writing_at" in response:
                    self.pending_at = response["writing_at"]
                else:
                    self.pending_at = None
                    self.sequence = response.get("sequence", self.sequence)
                    self.done = response.get("done", False)
                if self.done:
                    return
            if self.pending_at is not None and time.monotonic() - self.pending_at >= 2:
                self.error = "event_export_timeout"
            elif not self.process.is_alive():
                self.error = "event_export_failed"
        except (OSError, EOFError, ValueError):
            self.error = "event_export_failed"
        if self.error:
            self.close()

    def close(self):
        self._monitor_stop.set()
        with self._tick_lock:
            self._close()
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(0.1)

    def _close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(0.1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(0.1)
        if not self.process.is_alive():
            self.process.close()
        self.pipe.close()
