#!/usr/bin/env python3
"""Qinyuan run monitor — read-only, stdlib only, tolerant of broken runs.

Replaces the pile of ad-hoc scripts under .test/live/nssctf/ (_watch*.py,
_poll*.py, cache_report.py, _facts_watch.py, _tail_analysis.py, ...) with one
entry point. Every command opens the run's control.sqlite with ``?mode=ro``
and never writes to the run directory.

    python scripts/qymonitor.py runs                     # list recent runs
    python scripts/qymonitor.py show  runs117            # one-screen summary
    python scripts/qymonitor.py watch runs117            # live panel, 5s tick
    python scripts/qymonitor.py events runs117 --tail 40 # committed events
    python scripts/qymonitor.py calls runs117            # per-call table
    python scripts/qymonitor.py cache runs117            # prompt-cache by stage
    python scripts/qymonitor.py tree  runs117            # Turn / Intent structure
    python scripts/qymonitor.py facts runs117            # facts and their turn
    python scripts/qymonitor.py sql   runs117 "select ..." --limit 20

A "run" argument may be any of:
  * a path to ``runsNN/<task_run_id>/`` or to ``runsNN/`` (one level is scanned)
  * ``runsNN`` or ``latest`` under the state dir
  * a task_run_id or a unique prefix of one

Why not agent_core? A monitor must keep working while the code under test is
mid-refactor or the run is half-broken. This module only knows the persisted
formats (SQLite tables, d8-output evidence files, JSON result), so it cannot
fail because of an import or a schema migration.

For the strict, binding-validated timing report use scripts/analyze_run_timing.py;
it imports agent_core and refuses to report on mismatched evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = REPO / ".test" / "live" / "nssctf"
FALLBACK_STATE_DIR = REPO / ".runs"

# Result/event files written by PowerShell redirection are UTF-16 with a BOM.
BOMS = {b"\xff\xfe": "utf-16", b"\xfe\xff": "utf-16", b"\xef\xbb\xbf": "utf-8-sig"}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for bom, encoding in BOMS.items():
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    return raw.decode("utf-8", errors="replace")


def read_payload(path: Path) -> dict:
    """Evidence files wrap their content as {"payload": {...}}; accept both."""
    try:
        data = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}


def fmt_time(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")


def fmt_clock(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")


def fmt_age(ts, now: float | None = None) -> str:
    if not ts:
        return "-"
    delta = (now if now is not None else time.time()) - float(ts)
    if delta < 0:
        return "now"
    if delta < 90:
        return "%ds" % int(delta)
    if delta < 5400:
        return "%dm" % int(delta // 60)
    if delta < 172800:
        return "%dh%02dm" % (int(delta // 3600), int(delta % 3600 // 60))
    return "%dd" % int(delta // 86400)


def fmt_span(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 3600:
        return "%s%dm%02ds" % (sign, seconds // 60, seconds % 60)
    return "%s%dh%02dm" % (sign, seconds // 3600, seconds % 3600 // 60)


def fmt_left(deadline, now: float | None = None) -> str:
    """Deadline as remaining time, or 'passed by X' once it is behind us."""
    if not deadline:
        return "-"
    remaining = float(deadline) - (now if now is not None else time.time())
    return ("passed by " + fmt_span(-remaining)) if remaining < 0 else fmt_span(remaining)


def fmt_int(value) -> str:
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return "-"


def clip(text, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def run_number(path: Path) -> int:
    match = re.match(r"^runs(\d+)$", path.name)
    return int(match.group(1)) if match else -1


# --------------------------------------------------------------------------
# run resolution
# --------------------------------------------------------------------------
def locate_db(directory: Path) -> Path | None:
    """Return control.sqlite inside *directory* or one level below it."""
    direct = directory / "control.sqlite"
    if direct.is_file():
        return direct
    for candidate in sorted(directory.glob("*/control.sqlite")):
        return candidate
    return None


def state_dir(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path)
    if DEFAULT_STATE_DIR.is_dir():
        return DEFAULT_STATE_DIR
    return FALLBACK_STATE_DIR


def latest_run(base: Path) -> Path:
    numbered = [p for p in base.glob("runs*") if p.is_dir() and run_number(p) >= 0]
    if not numbered:
        raise SystemExit("no runs* directories under %s" % base)
    for candidate in sorted(numbered, key=run_number, reverse=True):
        db = locate_db(candidate)
        if db:
            return db.parent
    raise SystemExit("no control.sqlite found under %s" % base)


def resolve_run(ref: str | None, base: Path) -> Path:
    if not ref or ref.lower() in {"latest", "-1"}:
        return latest_run(base)

    path = Path(ref).expanduser()
    if not path.is_absolute() and not path.exists():
        path = Path.cwd() / path
    if path.exists():
        db = locate_db(path)
        if db:
            return db.parent
        raise SystemExit("no control.sqlite under %s" % path)

    named = base / ref
    if named.is_dir():
        db = locate_db(named)
        if db:
            return db.parent

    matches = [p for p in base.glob("*/*") if p.is_dir() and p.name.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit("ambiguous task_run_id prefix %r: %s" % (ref, ", ".join(m.name for m in matches)))
    raise SystemExit("cannot resolve run %r under %s" % (ref, base))


# --------------------------------------------------------------------------
# database access
# --------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3.0)
    con.row_factory = sqlite3.Row
    return con


def query(con: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def scalar(con: sqlite3.Connection, sql: str, params=(), default=None):
    rows = query(con, sql, params)
    if not rows:
        return default
    value = rows[0][0]
    return default if value is None else value


def table_counts(con: sqlite3.Connection, names) -> dict:
    return {name: scalar(con, "select count(*) from %s" % name, default=0) for name in names}


def load_task(con: sqlite3.Connection) -> dict:
    rows = query(con, "select * from task_runs limit 1")
    return dict(rows[0]) if rows else {}


def intent_status_counts(con: sqlite3.Connection) -> dict:
    return {row[0]: row[1] for row in query(con, "select status, count(*) from intents group by status")}


def budget_usage(con: sqlite3.Connection) -> tuple[Counter, dict]:
    """Per-category charged operations plus their completion states."""
    charged = Counter()
    states = Counter()
    for row in query(con, "select category, charged, completion_json from d4_operations"):
        if not row["charged"]:
            continue
        charged[row["category"]] += 1
        raw = row["completion_json"]
        kind = "started"
        if raw:
            try:
                kind = json.loads(raw).get("result_kind") or "completed"
            except (TypeError, json.JSONDecodeError):
                kind = "unknown"
        states[kind] += 1
    return charged, dict(states)


# --------------------------------------------------------------------------
# evidence readers
# --------------------------------------------------------------------------
def load_calls(run_dir: Path, con: sqlite3.Connection | None = None) -> list[dict]:
    """One row per model call, joined with the D4 ledger when available."""
    outdir = run_dir / "d8-output"
    if not outdir.is_dir():
        return []

    ledger = {}
    if con is not None:
        for row in query(con, "select operation_id, category, start_json, completion_json from d4_operations"):
            role = started = finished = None
            try:
                start = json.loads(row["start_json"] or "{}")
                role = start.get("role")
                started = start.get("started_at")
            except (TypeError, json.JSONDecodeError):
                pass
            try:
                finished = json.loads(row["completion_json"] or "{}").get("finished_at")
            except (TypeError, json.JSONDecodeError):
                pass
            ledger[row["operation_id"]] = dict(category=row["category"], role=role,
                                              started_at=started, finished_at=finished)

    calls = []
    for path in sorted(outdir.glob("d8-handoff-provider-result-*.json")):
        identity = path.stem[len("d8-handoff-provider-result-"):]
        result = read_payload(path)
        transport = result.get("transport") or {}
        usage = transport.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}

        request = read_payload(outdir / ("d8-handoff-provider-request-%s.json" % identity))
        contract = read_payload(outdir / ("d8-handoff-contract-%s.json" % identity))
        entry = ledger.get(identity, {})

        prompt = usage.get("prompt_tokens") or 0
        hit = usage.get("prompt_cache_hit_tokens") or usage.get("prompt_tokens_details", {}).get("cached_tokens") or 0
        calls.append(dict(
            id=identity,
            stage=request.get("stage") or contract.get("stage") or entry.get("category") or "unrecorded",
            role=entry.get("role") or "-",
            category=entry.get("category") or "-",
            correction=bool(contract.get("correction_of_model_request_id")),
            started=entry.get("started_at") or path.stat().st_mtime,
            duration=transport.get("elapsed_seconds"),
            prompt=prompt,
            hit=hit,
            completion=usage.get("completion_tokens") or 0,
            reasoning=details.get("reasoning_tokens") or 0,
            finish=transport.get("finish_reason") or ("error" if result.get("error") else "-"),
            http=transport.get("http_status"),
            stream_end=transport.get("transport_end"),
            error=result.get("error"),
        ))
    calls.sort(key=lambda row: row["started"])
    return calls


def find_result(run_dir: Path, task_run_id: str) -> dict | None:
    """Locate the persisted JSON result for this run (stdoutNN.json, UTF-16)."""
    seen = []
    for base in (run_dir.parent, run_dir.parent.parent):
        if base.is_dir() and base not in seen:
            seen.append(base)
    for base in seen:
        for candidate in sorted(base.glob("stdout*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            try:
                data = json.loads(read_text(candidate))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("task_run_id") == task_run_id:
                return data
    return None


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def index_results(base: Path) -> dict:
    """task_run_id -> parsed result, newest file wins. One scan for the whole listing."""
    index: dict[str, dict] = {}
    for path in sorted(base.glob("stdout*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(read_text(path))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("task_run_id"):
            index.setdefault(data["task_run_id"], data)
    return index


def cmd_runs(args) -> int:
    base = state_dir(args.state_dir)
    numbered = [p for p in base.glob("runs*") if p.is_dir()]
    numbered.sort(key=lambda p: (run_number(p), p.stat().st_mtime), reverse=True)
    results = index_results(base) if args.with_result else {}

    header = "%-9s %-38s %-9s %5s %5s %5s %6s %10s %s" % (
        "run", "task_run", "status", "turns", "intt", "facts", "events", "last event", "recovery")
    print(header)
    print("-" * len(header))
    shown = 0
    for candidate in numbered:
        if args.limit and shown >= args.limit:
            break
        db = locate_db(candidate)
        if not db:
            continue
        run_dir = db.parent
        try:
            con = connect(db)
        except sqlite3.Error as exc:
            print("%-9s %-38s  (db error: %s)" % (candidate.name, run_dir.name, exc))
            continue
        try:
            task = load_task(con)
            counts = table_counts(con, ("turns", "intents", "facts"))
            last_event = scalar(con, "select max(timestamp) from runtime_events")
            events = scalar(con, "select count(*) from runtime_events", default=0)
        finally:
            con.close()
        stale = ""
        if task.get("status") == "running" and task.get("deadline_at") and time.time() > task["deadline_at"]:
            stale = " [deadline passed]"
        recovery = ""
        if args.with_result:
            result = results.get(run_dir.name)
            if result:
                recovery = (result.get("recovery") or {}).get("reason") or ("exit %s" % result.get("exit_code"))
        print("%-9s %-38s %-9s %5d %5d %5d %6d %10s %s%s" % (
            candidate.name, run_dir.name[:38], task.get("status") or "-",
            counts["turns"], counts["intents"], counts["facts"],
            events, fmt_age(last_event), recovery, stale))
        shown += 1
    if not shown:
        print("(no readable runs under %s)" % base)
    return 0


def cmd_show(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        task = load_task(con)
        counts = table_counts(con, ("turns", "intents", "plans", "executions", "facts"))
        intents = intent_status_counts(con)
        facts_verified = scalar(con, "select count(*) from facts where status='verified'", default=0)
        candidates = scalar(con, "select count(*) from d7_answer_candidates", default=0)
        events = scalar(con, "select count(*) from runtime_events", default=0)
        last_event = scalar(con, "select max(timestamp) from runtime_events")
        checkpoint = scalar(con, "select max(sequence) from checkpoints", default=0)
        charged, states = budget_usage(con)
        calls = load_calls(run_dir, con)
    finally:
        con.close()

    result = find_result(run_dir, run_dir.name)
    now = time.time()
    started = task.get("started_at")
    deadline = task.get("deadline_at")
    last_call = max((c["started"] for c in calls), default=None)
    idle = now - max(last_call or 0, last_event or 0)
    status = task.get("status") or "-"

    prompt = sum(c["prompt"] for c in calls)
    hit = sum(c["hit"] for c in calls)
    completion = sum(c["completion"] for c in calls)
    reasoning = sum(c["reasoning"] for c in calls)

    print("run        %s" % run_dir.parent.name)
    print("task_run   %s" % run_dir.name)
    print("status     %s   started %s   elapsed %s   deadline %s   left %s" % (
        status, fmt_time(started), fmt_span(now - started) if started else "-",
        fmt_time(deadline), fmt_left(deadline, now)))
    print("goal       %s" % clip(task.get("goal"), 96))
    print("shapes     turns %d | intents %d %s | plans %d | executions %d | facts %d (verified %d) | candidates %d" % (
        counts["turns"], counts["intents"], dict(intents) or "", counts["plans"],
        counts["executions"], counts["facts"], facts_verified, candidates))
    print("events     %d   last %s (%s)   checkpoint %s" % (events, fmt_clock(last_event), fmt_age(last_event, now), checkpoint))
    print("calls      %d   last %s (%s)   %s" % (
        len(calls), fmt_clock(last_call), fmt_age(last_call, now),
        "IDLE %s" % fmt_span(idle) if status == "running" and idle > args.stall else "live"))
    if calls:
        print("tokens     prompt %s   cache %.1f%%   completion %s   reasoning %.0f%%" % (
            fmt_int(prompt), 100.0 * hit / prompt if prompt else 0.0,
            fmt_int(completion), 100.0 * reasoning / completion if completion else 0.0))
    if charged:
        parts = " ".join("%s %d" % (k, v) for k, v in charged.most_common())
        print("budget     %s   (total %d, %s)" % (parts, sum(charged.values()), states or "-"))
    if task.get("failure_reason"):
        print("failure    %s" % task["failure_reason"])
    if result:
        recovery = result.get("recovery") or {}
        print("result     exit %s   overall %s   recovery %s%s" % (
            result.get("exit_code"),
            (result.get("overall_goal") or {}).get("outcome") or "-",
            recovery.get("required"),
            (" (%s)" % recovery.get("reason")) if recovery.get("reason") else ""))
    return 0


def cmd_watch(args) -> int:
    base = state_dir(args.state_dir)
    while True:
        run_dir = resolve_run(args.run, base)
        now = time.time()
        try:
            con = connect(run_dir / "control.sqlite")
        except sqlite3.Error as exc:
            print("[%s] %s db error: %s" % (fmt_clock(now), run_dir.name[:8], exc))
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        try:
            task = load_task(con)
            counts = table_counts(con, ("facts", "executions", "intents", "turns"))
            intents = intent_status_counts(con)
            events = scalar(con, "select count(*) from runtime_events", default=0)
            last_event = scalar(con, "select max(timestamp) from runtime_events")
            charged, _ = budget_usage(con)
            calls = load_calls(run_dir, con)
        finally:
            con.close()

        status = task.get("status") or "-"
        last_call = max((c["started"] for c in calls), default=None)
        idle = now - max(last_call or 0, last_event or 0)
        prompt = sum(c["prompt"] for c in calls)
        hit = sum(c["hit"] for c in calls)
        completion = sum(c["completion"] for c in calls)
        reasoning = sum(c["reasoning"] for c in calls)
        deadline = task.get("deadline_at")

        flag = ""
        if status == "running" and idle > args.stall:
            flag = "  !! IDLE %s" % fmt_span(idle)
        print("[%s] %s %s  left %s  calls %d (last %s)  prompt %s cache %.1f%% comp %s reas %.0f%%%s" % (
            fmt_clock(now), run_dir.parent.name, status,
            fmt_left(deadline, now),
            len(calls), fmt_age(last_call, now), fmt_int(prompt),
            100.0 * hit / prompt if prompt else 0.0,
            fmt_int(completion), 100.0 * reasoning / completion if completion else 0.0, flag))
        print("           events %d (last %s)  facts %d  exec %d  intents %s  budget %s" % (
            events, fmt_age(last_event, now), counts["facts"], counts["executions"],
            dict(intents) or "-", sum(charged.values())))

        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_events(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        sql = "select sequence, type, timestamp, status, details_json from runtime_events"
        params: list = []
        if args.type:
            sql += " where type like ?"
            params.append("%%%s%%" % args.type)
        sql += " order by sequence"
        rows = query(con, sql, params)
    finally:
        con.close()

    if args.tail:
        rows = rows[-args.tail:]
    previous = None
    print("%-6s %-8s %-7s %-24s %s" % ("seq", "clock", "dt", "type", "detail"))
    print("-" * 96)
    for row in rows:
        stamp = row["timestamp"]
        delta = "-" if previous is None else "+%ds" % max(0, int(stamp - previous))
        previous = stamp
        detail = ""
        try:
            payload = json.loads(row["details_json"] or "{}")
            detail = payload.get("operation") or payload.get("result") or ""
            ids = payload.get("entity_ids") or []
            if ids:
                detail = "%s [%d]" % (detail, len(ids))
        except (TypeError, json.JSONDecodeError):
            pass
        print("%-6s %-8s %-7s %-24s %s" % (
            row["sequence"], fmt_clock(stamp), delta, clip(row["type"], 24), clip(detail, 56)))
    print("(%d events)" % len(rows))
    return 0


def cmd_calls(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        calls = load_calls(run_dir, con)
    finally:
        con.close()
    if args.errors:
        total = len(calls)
        calls = [c for c in calls if c["finish"] not in ("stop", "-") or c["error"]]
        if not calls:
            print("(all %d calls finished with stop; nothing matches --errors)" % total)
            return 0
    if args.tail:
        calls = calls[-args.tail:]
    if not calls:
        print("no completed model calls recorded")
        return 0

    print("%-4s %-8s %-18s %-14s %-16s %8s %10s %7s %8s %7s %-7s %s" % (
        "#", "clock", "stage", "role", "category", "dur", "prompt", "hit%", "compl", "reas", "finish", "flags"))
    print("-" * 128)
    for index, call in enumerate(calls, 1):
        flags = []
        if call["correction"]:
            flags.append("corr")
        if call["http"] not in (None, 200):
            flags.append("http%s" % call["http"])
        if call["stream_end"] and call["stream_end"] != "response_received":
            flags.append(str(call["stream_end"]))
        rate = 100.0 * call["hit"] / call["prompt"] if call["prompt"] else 0.0
        print("%-4d %-8s %-18s %-14s %-16s %7s %10s %6.1f%% %8s %7s %-7s %s" % (
            index, fmt_clock(call["started"]), clip(call["stage"], 18), clip(call["role"], 14),
            clip(call["category"], 16),
            ("%.1fs" % call["duration"]) if call["duration"] is not None else "-",
            fmt_int(call["prompt"]), rate, fmt_int(call["completion"]),
            fmt_int(call["reasoning"]), clip(call["finish"], 7), " ".join(flags)))
    return 0


def cmd_cache(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    calls = load_calls(run_dir, None)
    if not calls:
        print("no completed model calls recorded")
        return 0

    grouped: dict[str, list[dict]] = {}
    for call in calls:
        grouped.setdefault(call["stage"], []).append(call)

    header = "%-22s %5s %11s %10s %8s %9s %6s %9s" % (
        "stage", "calls", "prompt", "hit", "rate%", "max_hit", "corr", "reasoning")
    print(header)
    print("-" * len(header))

    def line(name, rows):
        prompt = sum(r["prompt"] for r in rows)
        hit = sum(r["hit"] for r in rows)
        return "%-22s %5d %11s %10s %7.2f%% %9s %6d %9s" % (
            name, len(rows), fmt_int(prompt), fmt_int(hit),
            100.0 * hit / prompt if prompt else 0.0,
            fmt_int(max(r["hit"] for r in rows)), sum(1 for r in rows if r["correction"]),
            fmt_int(sum(r["reasoning"] for r in rows)))

    for stage, rows in sorted(grouped.items(), key=lambda item: -sum(r["prompt"] for r in item[1])):
        print(line(clip(stage, 22), rows))
    print("-" * len(header))
    print(line("TOTAL", calls))
    prompt = sum(c["prompt"] for c in calls)
    hit = sum(c["hit"] for c in calls)
    completion = sum(c["completion"] for c in calls)
    reasoning = sum(c["reasoning"] for c in calls)
    print()
    print("completion %s   reasoning share %.1f%%   finish %s" % (
        fmt_int(completion), 100.0 * reasoning / completion if completion else 0.0,
        dict(Counter(c["finish"] for c in calls))))
    if prompt:
        print("cache hit %s / %s = %.2f%%   (hit price is 1/50 of miss on official pricing)" % (
            fmt_int(hit), fmt_int(prompt), 100.0 * hit / prompt))
    return 0


def cmd_tree(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        turns = query(con, "select * from turns order by sequence")
        intents = query(con, "select * from intents order by priority")
        facts = query(con, "select turn_id, status, count(*) as n from facts group by turn_id, status")
    finally:
        con.close()

    fact_counts: dict[tuple, int] = {}
    for row in facts:
        fact_counts[(row["turn_id"], row["status"])] = row["n"]

    by_turn: dict[str, list] = {}
    for row in intents:
        by_turn.setdefault(row["turn_id"], []).append(row)

    for turn in turns:
        total = sum(v for (turn_id, _), v in fact_counts.items() if turn_id == turn["turn_id"])
        print("Turn %s  [%s]  %s   facts %d" % (
            turn["sequence"], turn["status"], clip(turn["turn_target"], 64), total))
        for intent in by_turn.get(turn["turn_id"], []):
            print("   %s  %-11s  %s" % (
                intent["intent_id"][:8], intent["status"], clip(intent["description"], 76)))
    if not turns:
        print("(no turns yet)")
    return 0


def cmd_facts(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        rows = query(con, """select f.fact_id, f.status, f.content, f.turn_id, t.sequence as turn_seq,
                                    f.verification_ref
                             from facts f left join turns t on t.turn_id = f.turn_id
                             order by f.created_event_sequence""")
    finally:
        con.close()
    if not rows:
        print("(no facts)")
        return 0
    print("%-10s %-10s %-6s %s" % ("fact", "status", "turn", "content"))
    print("-" * 110)
    for row in rows:
        print("%-10s %-10s %-6s %s" % (
            row["fact_id"][:8], row["status"], row["turn_seq"] if row["turn_seq"] is not None else "-",
            clip(row["content"], 84)))
    print("(%d facts)" % len(rows))
    return 0


def cmd_sql(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        try:
            rows = con.execute(args.statement).fetchall()
        except sqlite3.Error as exc:
            print("sql error: %s" % exc)
            return 1
    finally:
        con.close()
    if not rows:
        print("(0 rows)")
        return 0
    columns = rows[0].keys()
    print(" | ".join(columns))
    print("-" * 80)
    for row in rows[: args.limit]:
        print(" | ".join(str(row[name]) for name in columns))
    if len(rows) > args.limit:
        print("... (%d rows, showing %d)" % (len(rows), args.limit))
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qymonitor",
        description="Read-only monitor for Qinyuan TaskRuns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--state-dir", help="directory holding runsNN/ (default: %s)" % DEFAULT_STATE_DIR)
    parser.add_argument("--ansi", action="store_true",
                        help="keep the console codepage instead of forcing UTF-8 output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("runs", help="list recent runs")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--with-result", action="store_true", help="also read stdoutNN.json (slower)")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("show", help="one-screen summary of a run")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--stall", type=int, default=180, help="seconds of inactivity flagged as IDLE")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("watch", help="live status panel")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("-i", "--interval", type=float, default=10.0)
    p.add_argument("--stall", type=int, default=180)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("events", help="committed runtime events")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--tail", type=int, default=30)
    p.add_argument("--type", help="substring filter on event type")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("calls", help="per-call table: stage, duration, tokens, cache, finish")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--tail", type=int)
    p.add_argument("--errors", action="store_true", help="only non-stop finishes")
    p.set_defaults(func=cmd_calls)

    p = sub.add_parser("cache", help="prompt-cache report by stage")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("tree", help="Turn / Intent structure")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("facts", help="facts with their turn")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("sql", help="read-only SQL escape hatch")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("statement")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_sql)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # The DSH / PowerShell harness reads native output as UTF-8 while Python
        # on Windows defaults to the ANSI codepage; forcing UTF-8 keeps the goal
        # text readable. Pass --ansi to keep the native codepage instead.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.ansi:
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except sqlite3.Error as exc:
        print("database error: %s" % exc)
        print("hint: a live run keeps control.sqlite in WAL mode; if the -shm file is missing,")
        print("      copy control.sqlite, -wal and -shm together to a scratch directory and")
        print("      point this tool at the copy.")
        return 1
    except SystemExit:
        raise


if __name__ == "__main__":
    raise SystemExit(main())
