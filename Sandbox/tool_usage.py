"""Private, persistent counter for agent-requested on-demand tool downloads.

The counter lives under workbench_data (gitignored) and is never cleared by
code.  It records the agent-facing download request; it is deliberately not
exposed through the MCP response so it cannot influence the model.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path


COUNTER_ENV = "QINYUAN_TOOL_DOWNLOAD_COUNTER"
DEFAULT_COUNTER = Path(__file__).resolve().parent.parent / "workbench_data" / "tool_download_counts.sqlite"


def counter_path() -> Path:
    raw = os.environ.get(COUNTER_ENV)
    return Path(raw) if raw else DEFAULT_COUNTER


def record(name: str) -> None:
    """Increment one tool's counter. Never raises into the download path."""
    if not name:
        return
    # Local test runs must not pollute the operator's private usage count.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    path = counter_path()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=5) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tool_download_counts ("
                "name TEXT PRIMARY KEY, count INTEGER NOT NULL, "
                "first_at TEXT NOT NULL, last_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO tool_download_counts(name, count, first_at, last_at) "
                "VALUES(?, 1, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "count = count + 1, last_at = excluded.last_at",
                (name, now, now),
            )
            connection.commit()
    except (OSError, sqlite3.Error):
        # Counting must never break a download request.
        return


def counts() -> dict[str, dict[str, object]]:
    """Read-only view for the operator; returns {} when no counter exists."""
    path = counter_path()
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT name, count, first_at, last_at FROM tool_download_counts ORDER BY count DESC, name"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        name: {"count": count, "first_at": first_at, "last_at": last_at}
        for name, count, first_at, last_at in rows
    }
