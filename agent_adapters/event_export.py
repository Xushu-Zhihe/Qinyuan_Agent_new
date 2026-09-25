"""Read committed events; isolate file writes from CLI cancellation."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import time
from pathlib import Path

from agent_adapters.run_files import encode


def read_events(database: Path, task_run_id, after_sequence=0, limit=128):
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
        rows = conn.execute(
            "SELECT e.* FROM runtime_events e WHERE e.task_run_id=? AND e.sequence>? "
            "AND e.sequence<=(SELECT MAX(watermark_event_sequence) FROM checkpoints WHERE task_run_id=?) "
            "ORDER BY e.sequence LIMIT ?",
            (task_run_id, after_sequence, task_run_id, limit),
        ).fetchall()
        return [
            {
                "protocol_version": 1,
                "task_run_id": row["task_run_id"],
                "sequence": row["sequence"],
                "type": row["type"],
                "timestamp": row["timestamp"],
                "status": row["status"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]


def _writer(pipe, path, database, task_id, finish):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Only small acknowledgements travel over IPC, never event batches.
        with Path(path).open("xb") as stream:
            pipe.send({"ready": True})
            sequence = 0
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


class EventExporter:
    def __init__(self, path: Path, database: Path, task_id: str, *, worker=_writer):
        ctx = multiprocessing.get_context("spawn")
        self.pipe, child = ctx.Pipe(duplex=False)
        self.finish = ctx.Event()
        self.process = ctx.Process(
            target=worker, args=(child, str(path), str(database), task_id, self.finish)
        )
        self.process.start()
        child.close()
        self.pending_at = time.monotonic()
        self.error = None
        self.sequence = 0
        self.done = False
        self.closed = False

    def tick(self):
        if self.error or self.done:
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
