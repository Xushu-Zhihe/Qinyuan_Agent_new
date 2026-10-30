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
    python scripts/qymonitor.py planning RUN --json      # planning cost and observed outcomes
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
import math
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = REPO / ".test" / "live" / "nssctf"
FALLBACK_STATE_DIR = REPO / ".runs"
WORKBENCH_DB = REPO / "workbench_data" / "workbench.sqlite3"

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
    """Return control.sqlite inside *directory* or a couple of levels below it.

    CLI state dirs are <state>/runsNN/<task_run_id>/control.sqlite (one level).
    Workbench runs are <state>/<run_id>/agent-state/<task_run_id>/control.sqlite
    (two levels), so both layouts resolve.
    """
    direct = directory / "control.sqlite"
    if direct.is_file():
        return direct
    for pattern in ("*/control.sqlite", "*/*/control.sqlite"):
        for candidate in sorted(directory.glob(pattern)):
            return candidate
    return None


def run_label(run_dir: Path) -> str:
    """Display name: runsNN for CLI dirs, the workbench run id under agent-state."""
    parent = run_dir.parent
    if re.match(r"^runs\d+$", parent.name):
        return parent.name
    if parent.name == "agent-state" and parent.parent.is_dir():
        return parent.parent.name[:8]
    return parent.name


def state_dir(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path)
    if DEFAULT_STATE_DIR.is_dir():
        return DEFAULT_STATE_DIR
    return FALLBACK_STATE_DIR


def latest_run(base: Path) -> Path:
    numbered = [p for p in base.glob("runs*") if p.is_dir() and run_number(p) >= 0]
    if numbered:
        for candidate in sorted(numbered, key=run_number, reverse=True):
            db = locate_db(candidate)
            if db:
                return db.parent
    elif base.is_dir():
        # workbench layout: newest run id wins
        for candidate in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if candidate.is_dir():
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

    matches = [p for p in list(base.glob("*")) + list(base.glob("*/*")) + list(base.glob("*/*/*"))
               if p.is_dir() and p.name.startswith(ref)]
    resolved = [db.parent for db in (locate_db(p) for p in matches) if db is not None]
    if len(resolved) == 1:
        return resolved[0]
    if len(resolved) > 1:
        raise SystemExit("ambiguous id prefix %r: %s" % (ref, ", ".join(m.name for m in resolved)))
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


def workbench_run_state(run_dir: Path) -> dict | None:
    """Workbench runs keep their terminal status outside control.sqlite.

    task_runs stays 'running' when the CLI exits with 5, so a finished workbench
    run can look alive forever. Read the wrapper's own row when the layout
    matches <workbench>/runs/<run_id>/agent-state/<task_run_id>.
    """
    if run_dir.parent.name != "agent-state" or not WORKBENCH_DB.is_file():
        return None
    try:
        con = connect(WORKBENCH_DB)
    except sqlite3.Error:
        return None
    try:
        rows = query(con, """select run_id, status, exit_code, started_at, finished_at
                             from runs where agent_task_run_id = ?""", (run_dir.name,))
        if not rows:
            rows = query(con, """select run_id, status, exit_code, started_at, finished_at
                                 from runs where directory like ?""",
                         ("%" + run_dir.parent.parent.name + "%",))
    finally:
        con.close()
    return dict(rows[0]) if rows else None


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
def _sibling(path: Path, kind: str) -> Path:
    """<prefix>provider-result-<id>.json  ->  <prefix><kind>-<id>.json.

    Handles both evidence layouts seen in this repo:
      d8-output/d8-handoff-provider-result-<id>.json   (runs up to ~117)
      stage-evidence/stage-provider-result-<id>.json   (runs from ~118)
    """
    prefix, _, rest = path.name.partition("provider-result-")
    return path.parent / ("%s%s-%s" % (prefix, kind, rest))


def provider_results(run_dir: Path) -> list[tuple[str, Path]]:
    """(model_request_id, result file) pairs, newest file wins per id."""
    found: dict[str, Path] = {}
    for path in sorted(run_dir.rglob("*provider-result-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        identity = path.name.split("provider-result-", 1)[1]
        if identity.endswith(".json"):
            identity = identity[:-5]
        found.setdefault(identity, path)
    return list(found.items())


def inflight_calls(run_dir: Path) -> list[tuple[str, float]]:
    """Model requests written but not answered yet: (id, request mtime), oldest first.

    The request file is persisted before the call starts, so a request without a
    matching result means the model is still generating — that is not idleness.
    """
    done = {identity for identity, _ in provider_results(run_dir)}
    pending: dict[str, float] = {}
    for path in run_dir.rglob("*provider-request-*.json"):
        identity = path.name.split("provider-request-", 1)[1]
        if identity.endswith(".json"):
            identity = identity[:-5]
        if identity in done:
            continue
        pending.setdefault(identity, path.stat().st_mtime)
    return sorted(pending.items(), key=lambda item: item[1])


def stage_result_payloads(run_dir: Path):
    """Yield the parsed payload of every persisted stage result, oldest first."""
    for path in sorted(run_dir.rglob("stage-result-*.json"), key=lambda p: p.stat().st_mtime):
        yield path, read_payload(path)


def offered_tools(run_dir: Path) -> list[str]:
    """Tools the model was shown (from the frozen MCP catalog of this run)."""
    for path in sorted(run_dir.rglob("stage-mcp-catalog-*.json")):
        payload = read_payload(path)
        names = [t.get("qualified_name") for t in (payload.get("tools") or [])
                 if isinstance(t, dict) and t.get("qualified_name")]
        if names:
            return names
    return []


def tool_usage(run_dir: Path) -> tuple[Counter, dict]:
    """Valid proposed tools, including batch members; not physical dispatch counts."""
    counts: Counter = Counter()
    last: dict[str, str] = {}
    for _, payload in stage_result_payloads(run_dir):
        if payload.get('outcome') != 'valid':
            continue
        arguments = [((call.get('function') or {}).get('arguments')) or '{}'
            for call in (payload.get('tool_calls') or [])] or [payload.get('content')]
        for raw in arguments:
            try:
                obj = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            result = obj.get("result") if isinstance(obj.get("result"), dict) else obj
            proposed = ([result] if result.get('kind') == 'mcp_call' else
                result.get('calls', []) if result.get('kind') == 'mcp_calls' else [])
            for selection in proposed:
                name = selection.get('tool_name') or '?'
                counts[name] += 1
                last[name] = json.dumps(selection.get('arguments'), ensure_ascii=False)[:110]
    return counts, last


def correction_hints(run_dir: Path) -> list[dict]:
    """Bounded diagnostics the program sent back with a rejected output."""
    rows = []
    for path in sorted(run_dir.rglob("stage-contract-*.json"), key=lambda p: p.stat().st_mtime):
        payload = read_payload(path)
        correction = payload.get("correction")
        if isinstance(correction, dict) and correction.get("issues"):
            rows.append(dict(
                at=path.stat().st_mtime,
                identity=path.stem.split("contract-")[-1][:8],
                reason=correction.get("reason") or "-",
                issues=correction.get("issues") or [],
            ))
    return rows


def load_calls(run_dir: Path, con: sqlite3.Connection | None = None) -> list[dict]:
    """One row per model call, joined with the D4 ledger when available."""
    ledger = {}
    if con is not None:
        for row in query(con, "select operation_id, category, start_json, completion_json from d4_operations"):
            role = started = finished = result_kind = None
            try:
                start = json.loads(row["start_json"] or "{}")
                role = start.get("role")
                started = start.get("started_at")
            except (TypeError, json.JSONDecodeError):
                pass
            try:
                completion = json.loads(row["completion_json"] or "{}")
                finished, result_kind = completion.get("finished_at"), completion.get("result_kind")
            except (TypeError, json.JSONDecodeError):
                pass
            ledger[row["operation_id"]] = dict(category=row["category"], role=role,
                                              started_at=started, finished_at=finished, result_kind=result_kind)

    calls = []
    for identity, path in provider_results(run_dir):
        result = read_payload(path)
        transport = result.get("transport") or {}
        usage = transport.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}

        request = read_payload(_sibling(path, "provider-request"))
        contract = read_payload(_sibling(path, "contract"))
        # Newer layout keeps stage and correction inside stage-request-<id>.json.
        envelope = read_payload(_sibling(path, "request")).get("request") or {}
        entry = ledger.get(identity, {})

        prompt = usage.get("prompt_tokens") or 0
        hit = usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        calls.append(dict(
            id=identity,
            stage=(request.get("stage") or contract.get("stage") or envelope.get("stage")
                   or entry.get("category") or "unrecorded"),
            role=entry.get("role") or "-",
            category=entry.get("category") or "-",
            correction=bool(contract.get("correction_of_model_request_id")
                            or envelope.get("correction_of_model_request_id")),
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
            finished=entry.get("finished_at"),
            usage=usage,
            ledger_state=entry.get("result_kind"),
            ledger_known=identity in ledger,
            started_known=entry.get("started_at") is not None,
        ))
    calls.sort(key=lambda row: row["started"])
    return calls


def interval_seconds(intervals) -> float:
    """Union of observed intervals; parallel calls are not counted twice."""
    total, end = 0.0, None
    for begin, finish in sorted(intervals):
        total += max(0.0, finish - max(begin, end if end is not None else begin))
        end = max(finish, end if end is not None else finish)
    return total


def overlap_seconds(first, second):
    return interval_seconds((max(a, c), min(b, d)) for a, b in first for c, d in second if max(a, c) < min(b, d))


def add_usage(tokens, usage):
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        value = ((usage.get("completion_tokens_details") or {}).get(key) if key == "reasoning_tokens" else usage.get(key))
        metric = tokens.setdefault(key, dict(known_calls=0, total=None))
        if type(value) is int and value >= 0:
            metric["known_calls"] += 1
            metric["total"] = (metric["total"] or 0) + value


def review_report(run_dir, con, calls, operations, requests):
    """Observed ledger overlap and explicit source adoption, without semantic scoring."""
    results = {identity: read_payload(run_dir / "stage-evidence" / f"stage-result-{identity}.json") for identity in requests}
    spans = dict(review=[], inner=[], tool=[])
    tools, attempts, tool_intervals = {}, {}, {}
    coverage = Counter()
    for identity, operation in operations.items():
        start, end = operation["start"], operation["end"]
        begin, finish = start.get("started_at"), end.get("finished_at")
        request = requests.get(identity, {})
        kind = "tool" if start.get("category") == "tool_dispatch" else "review" if request.get("stage") == "stage_review" else "inner" if start.get("role") == "inner_action" else None
        if kind:
            coverage[kind + "_calls"] += 1
        known = all(type(value) in {int, float} and math.isfinite(value) for value in (begin, finish)) and finish >= begin
        if kind and known:
            spans[kind].append((begin, finish))
            coverage["timed_" + kind + "_calls"] += 1
            if kind == "tool":
                tool_intervals[identity] = (begin, finish)
        if kind == "tool":
            tools[identity] = operation
        if request.get("stage") == "stage_review":
            runtime = (request.get("execution_context") or {}).get("stage_runtime") or {}
            for source in runtime.get("review_members") or [request.get("proposal_operation_id")]:
                if source:
                    attempts.setdefault(source, []).append(identity)
    commits = {row["mutation_id"]: row["committed_at"] for row in query(con, "select mutation_id,committed_at from control_mutations")}
    queue = []
    for row in query(con, "select proposal_operation_id,state,review_operation_id from stage_review_queue order by rowid"):
        identity = row["proposal_operation_id"]
        source = operations.get(identity, {})
        ready = source.get("end", {}).get("finished_at")
        starts = [operations[attempt]["start"].get("started_at") for attempt in attempts.get(identity, [])]
        starts = [value for value in starts if isinstance(value, (float, int))]
        first = min(starts) if starts else None
        review = operations.get(row["review_operation_id"], {})
        queue.append(dict(row, attempt_ids=attempts.get(identity, []), proposal_finished_at=ready,
            first_review_started_at=first,
            proposal_ready_to_first_review_seconds=first-ready if first is not None and ready is not None and first >= ready else None,
            settled_at=commits.get(review.get("application")) if row["state"] == "settled" else None))
    observed = {identity for identity, (_, end) in tool_intervals.items()
        if tools[identity]["end"].get("result_kind") == "completed" and any(begin <= end < finish for begin, finish in spans["review"])}
    overlapping = {identity for identity, span in tool_intervals.items() if overlap_seconds([span], spans["review"]) > 0}
    facts = query(con, "select fact_id,source_ref,verification_ref from facts where status='verified'")
    referenced, missing_claims = set(), 0
    for fact in facts:
        if fact["source_ref"] in tools:
            referenced.add(fact["source_ref"])
        parts = (fact["verification_ref"] or "").split(":")
        request = requests.get(parts[1], {}) if len(parts) > 1 and parts[0] == "stage" else {}
        claim = request.get("proposal") or {}
        claim = claim if isinstance(claim, dict) else {}
        if len(parts) == 3 and parts[2].isdigit():
            candidates = claim.get("candidates") or []
            claim = candidates[int(parts[2])] if isinstance(candidates, list) and int(parts[2]) < len(candidates) else {}
            claim = claim if isinstance(claim, dict) else {}
        if not claim.get("support_refs"):
            missing_claims += 1
        for ref in claim.get("support_refs", []):
            parts = ref.split(":", 2)
            if len(parts) == 3 and parts[0] == "progress" and parts[1] in tools:
                referenced.add(parts[1])
    adopted, missing_achievements = set(), 0
    for row in query(con, "select achievement_json from stage_turns where achievement_json is not null"):
        try:
            achievement = json.loads(row["achievement_json"])
            refs = achievement.get("completion", {}).get("support_refs")
        except (TypeError, ValueError, AttributeError):
            refs = None
        if refs is None:
            missing_achievements += 1
        for ref in refs or []:
            parts = ref.split(":", 2)
            if len(parts) == 3 and parts[0] == "verified_fact":
                adopted.add(parts[1])
    unused = {identity for identity, operation in operations.items() if
        (operation["application"] or "").startswith("stage-discarded/") or results.get(identity, {}).get("outcome") == "superseded"}
    reused = {identity for identity, operation in operations.items() if (operation["application"] or "").startswith("stage-reused/")}
    costs = {}
    for name, identities in (("all_models", set(calls)), ("unadopted_generations", unused),
            ("reused_proposals", reused), ("corrections", {identity for identity, call in calls.items() if call["correction"]})):
        tokens = {}
        for identity in identities:
            add_usage(tokens, calls.get(identity, {}).get("usage") or {})
        costs[name] = dict(calls=len(identities), tokens=tokens)
    fences = [path.stem.removeprefix("stage-tool-dispatch-stop-") for path in (run_dir / "stage-evidence").glob("stage-tool-dispatch-stop-*.json")
        if read_payload(path).get("outcome") == "not_dispatched"]
    return dict(queue_available=bool(query(con, "select name from sqlite_master where name='stage_review_queue'")),
        timing_coverage=dict(coverage), queue=queue, queue_states=dict(Counter(item["state"] for item in queue)),
        branch_count=scalar(con, "select count(*) from stage_branches", default=None),
        overlap=dict(review_interval_union_seconds=interval_seconds(spans["review"]) if spans["review"] else None,
            review_with_inner_seconds=overlap_seconds(spans["review"], spans["inner"]) if spans["review"] and spans["inner"] else None,
            review_with_tools_seconds=overlap_seconds(spans["review"], spans["tool"]) if spans["review"] and spans["tool"] else None),
        exploration=dict(tools_overlapping_review=sorted(overlapping), observations_completed_during_review=sorted(observed),
            observations_referenced_by_stored_facts=sorted(observed & referenced),
            observations_without_recorded_fact_reference=sorted(observed - referenced),
            fact_ids_referenced_by_achievements=sorted(adopted),
            unresolved_fact_source_records=missing_claims, unresolved_achievement_support=missing_achievements,
            fact_sources_available=bool(query(con, "select name from pragma_table_info('facts') where name='source_ref'"))),
        cost=costs, monetary_cost=None, pricing_status="no_confirmed_pricing",
        unadopted_generation_ids=sorted(unused), fenced_dispatch_ids=sorted(fences),
        inconsistent_fence_ids=sorted(set(fences) & tools.keys()))


def planning_report(run_dir: Path, con: sqlite3.Connection) -> dict:
    """Read-only observations for comparison, not a causal ROI/quality score."""
    calls = {c["id"]: c for c in load_calls(run_dir, con)}
    unreadable_rows = 0
    operations, requests = {}, {}
    for row in query(con, "select operation_id, start_json, completion_json, application_ref from d4_operations"):
        try:
            start = json.loads(row["start_json"])
            completion = json.loads(row["completion_json"] or "{}")
            if not isinstance(start, dict) or not isinstance(completion, dict):
                raise ValueError("invalid operation record")
        except (TypeError, ValueError):
            unreadable_rows += 1
            continue
        operations[row["operation_id"]] = dict(start=start, end=completion, application=row["application_ref"])
        if start.get("category") == "tool_dispatch" or start.get("role") not in {"inner_action", "outer_planning"}:
            continue
        identity = row["operation_id"]
        saved = read_payload(run_dir / "stage-evidence" / ("stage-request-" + identity + ".json"))
        request = saved.get("request") or {}
        requests[identity] = request
        call = calls.setdefault(identity, dict(id=identity, stage=request.get("stage") or start["category"],
            started=start.get("started_at"), duration=None, usage={}, correction=bool(request.get("correction_of_model_request_id")),
            error=None, finish="-"))
        call.update(finished=completion.get("finished_at"), ledger_state=completion.get("result_kind"),
            applied=row["application_ref"] is not None, ledger_known=True, started_known=start.get("started_at") is not None)
    planning = {"stage_intent", "stage_roads", "stage_continuation"}
    grouped, intervals, plan_intervals = {}, [], []
    for call in calls.values():
        group = grouped.setdefault(call["stage"], dict(calls=0, cumulative_seconds=0.0,
            timing_known=0, corrections=0, errors=0, unsettled=0, ledger_unknown=0, applied=0, tokens={}))
        group["calls"] += 1
        group["corrections"] += bool(call["correction"])
        group["errors"] += bool(call["error"] or call.get("ledger_state") in {"failed", "rejected"})
        group["unsettled"] += bool(call.get("ledger_known") and call.get("ledger_state") is None)
        group["ledger_unknown"] += not call.get("ledger_known", False)
        group["applied"] += bool(call.get("applied"))
        begin, finish, duration = call["started"], call.get("finished"), call["duration"]
        if begin is not None and finish is not None:
            duration = max(0.0, finish - begin)
        if duration is not None:
            group["cumulative_seconds"] += duration
            group["timing_known"] += 1
            if begin is not None and call.get("started_known"):
                span = (begin, begin + duration)
                intervals.append(span)
                if call["stage"] in planning:
                    plan_intervals.append(span)
        usage = call.get("usage") or {}
        add_usage(group["tokens"], usage)
    turns = query(con, "select turn_id, achievement_json from stage_turns")
    outcomes = dict(stage_data_available=bool(query(con, "select name from sqlite_master where name='stage_turns'")),
        turns=len(turns), achieved_turns=sum(bool(r["achievement_json"]) for r in turns),
        roads={r[0]: r[1] for r in query(con, "select status, count(*) from stage_roads group by status")},
        stored_verified_facts=scalar(con, "select count(*) from facts where status='verified'"),
        task_status=load_task(con).get("status"))
    result = find_result(run_dir, run_dir.name)
    outcomes["overall_goal"] = (result.get("overall_goal") or {}).get("outcome") if result else None
    stops = sorted(run_dir.glob("dispatch-stop-*.json"), key=lambda p: p.stat().st_mtime)
    stop = read_payload(stops[-1]) if stops else {}
    outcomes["recorded_stop"] = stop.get("stop")
    outcomes["stop_cause"] = (stop.get("diagnostic") or {}).get("cause_code") or stop.get("reason")
    for group in grouped.values():
        if not group["timing_known"]:
            group["cumulative_seconds"] = None
    model_union = interval_seconds(intervals) if intervals else None
    plan_union = interval_seconds(plan_intervals) if intervals else None
    return dict(stages=grouped, planning_stages=sorted(planning), outcomes=outcomes,
        reviews=review_report(run_dir, con, calls, operations, requests),
        unreadable_ledger_rows=unreadable_rows,
        model_interval_union_seconds=model_union, planning_interval_union_seconds=plan_union,
        planning_share_of_observed_model_time=plan_union / model_union if model_union else None,
        notes=["Cumulative call time may overlap; interval union removes overlap. Missing timing/usage is unknown, not zero.",
            "Planning = stage_intent/roads/continuation. Reviews and completion checks are listed separately.",
            "Applied includes consumed rejected proposals; it does not mean accepted or successful.",
            "Achievements/Facts are stored records, not a fresh validity judgment; task_status may lag a recorded stop.",
            "Outcomes are observations, not proof that planning caused improvement; compare matched tasks/configurations.",
            "Reasoning tokens are part of completion tokens; never add them again.",
            "Semantic duplicate work and exploration beyond the criterion are not inferred from counts.",
            "Review overlap uses completed ledger intervals; it is not wall-clock time saved. Unsettled intervals are excluded.",
            "Proposal-ready to first review includes admission/restoration delay, not a measured queue insertion timestamp.",
            "Unreferenced observations are not classified as useless. Stored Fact/achievement references do not establish current validity or causation.",
            "Cost subsets overlap; do not add them. Missing usage and unconfirmed monetary pricing remain unknown.",
            "Tolerant monitor: evidence is not binding-validated; use analyze_run_timing.py for strict reports."])


def cmd_planning(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    con = connect(run_dir / "control.sqlite")
    try:
        report = planning_report(run_dir, con)
    finally:
        con.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("%-22s %5s %9s %7s %5s %5s %9s" % ("stage", "calls", "sum(s)", "timed", "corr", "error", "unsettled"))
    for stage, group in sorted(report["stages"].items()):
        print("%-22s %5d %9s %3d/%-3d %5d %5d %9d" % (stage, group["calls"],
            "%.1f" % group["cumulative_seconds"] if group["timing_known"] else "unknown",
            group["timing_known"], group["calls"], group["corrections"], group["errors"], group["unsettled"]))
        print("  tokens " + " | ".join("%s=%s (%d/%d calls)" % (name, fmt_int(metric["total"]), metric["known_calls"], group["calls"])
            for name, metric in group["tokens"].items()))
    share = report["planning_share_of_observed_model_time"]
    print("observed model union %ss | planning union %ss | share %s" % (
        "%.1f" % report["model_interval_union_seconds"] if report["model_interval_union_seconds"] is not None else "unknown",
        "%.1f" % report["planning_interval_union_seconds"] if report["planning_interval_union_seconds"] is not None else "unknown",
        "%.1f%%" % (share * 100) if share is not None else "unknown"))
    print("outcomes " + json.dumps(report["outcomes"], ensure_ascii=False))
    reviews = report["reviews"]
    print("review queue %s | branches %s" % (reviews["queue_states"] if reviews["queue_available"] else "unavailable", reviews["branch_count"]))
    print("observed review overlap " + json.dumps(reviews["overlap"]))
    exploration = reviews["exploration"]
    print("observations completed during review %d | with recorded Fact reference %d | without recorded reference %d" % (
        len(exploration["observations_completed_during_review"]), len(exploration["observations_referenced_by_stored_facts"]),
        len(exploration["observations_without_recorded_fact_reference"])))
    print("achievement-referenced Facts %d | unadopted generations %d | fenced dispatches %d" % (
        len(exploration["fact_ids_referenced_by_achievements"]), len(reviews["unadopted_generation_ids"]), len(reviews["fenced_dispatch_ids"])))
    for name, cost in reviews["cost"].items():
        print("cost %s: %d calls | %s" % (name, cost["calls"], " | ".join(
            "%s=%s (%d/%d calls)" % (key, fmt_int(value["total"]), value["known_calls"], cost["calls"]) for key, value in cost["tokens"].items())))
    print("monetary cost: unknown (no confirmed pricing); timing coverage " + json.dumps(reviews["timing_coverage"]))
    print("unknown ledger %d calls | unreadable ledger %d rows" % (
        sum(g["ledger_unknown"] for g in report["stages"].values()), report["unreadable_ledger_rows"]))
    for note in report["notes"]:
        print("note " + note)
    return 0


def last_file_activity(run_dir: Path) -> float | None:
    """Newest mtime anywhere under the run directory.

    runtime_events only advances on control commits and model calls only show up
    as provider evidence, so a long tool/experiment phase looks idle from both.
    Any evidence write bumps a file mtime, which is the honest "still working"
    signal.
    """
    newest = None
    try:
        for path in run_dir.rglob("*"):
            try:
                if not path.is_file():
                    continue
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
    except OSError:
        pass
    return newest


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
        stage_turns = scalar(con, "select count(*) from stage_turns", default=0)
        stage_roads = scalar(con, "select count(*) from stage_roads", default=0)
        road_states = {row[0]: row[1] for row in query(
            con, "select status, count(*) from stage_roads group by status")}
        last_file = last_file_activity(run_dir)
    finally:
        con.close()

    result = find_result(run_dir, run_dir.name)
    workbench = workbench_run_state(run_dir)
    now = time.time()
    started = task.get("started_at")
    deadline = task.get("deadline_at")
    last_call = max((c["started"] for c in calls), default=None)
    idle = now - max(last_call or 0, last_event or 0, last_file or 0)
    status = task.get("status") or "-"

    prompt = sum(c["prompt"] for c in calls)
    hit = sum(c["hit"] for c in calls)
    completion = sum(c["completion"] for c in calls)
    reasoning = sum(c["reasoning"] for c in calls)

    print("run        %s" % run_label(run_dir))
    print("task_run   %s" % run_dir.name)
    print("status     %s   started %s   elapsed %s   deadline %s   left %s" % (
        status, fmt_time(started), fmt_span(now - started) if started else "-",
        fmt_time(deadline), fmt_left(deadline, now)))
    if workbench:
        print("workbench  %s   exit %s   finished %s   (wrapper view; control.sqlite keeps status=running on exit 5)" % (
            workbench.get("status") or "-", workbench.get("exit_code"),
            _fmt_iso(workbench.get("finished_at"))))
    print("goal       %s" % clip(task.get("goal"), 96))
    legacy_empty = not any(counts.values()) and not facts_verified and not candidates
    if stage_turns and legacy_empty:
        print("shapes     legacy Turn/Intent/Plan/Execution tables empty — this run uses the v3 stage tables only")
    else:
        print("shapes     turns %d | intents %d %s | plans %d | executions %d | facts %d (verified %d) | candidates %d" % (
            counts["turns"], counts["intents"], dict(intents) or "", counts["plans"],
            counts["executions"], counts["facts"], facts_verified, candidates))
    if stage_turns:
        print("stage      %d turn(s) | %d road(s) %s   (v3 stage tables)" % (
            stage_turns, stage_roads, road_states or ""))
    print("events     %d   last %s (%s)   checkpoint %s" % (events, fmt_clock(last_event), fmt_age(last_event, now), checkpoint))
    # A run can be dead while task_runs still says running (no terminal commit).
    # The persisted CLI result is the authority on that.
    pending = inflight_calls(run_dir)
    if result and result.get("exit_code") is not None:
        liveness = "finished (exit %s)" % result["exit_code"]
    elif workbench and workbench.get("finished_at") and workbench.get("status") != "running":
        liveness = "finished (workbench %s, exit %s)" % (workbench["status"], workbench["exit_code"])
    elif status == "running" and pending:
        liveness = "generating %s" % fmt_span(now - pending[0][1])
    elif status == "running" and idle > args.stall:
        liveness = "IDLE %s" % fmt_span(idle)
    else:
        liveness = "live"
    print("calls      %d   last %s (%s, evidence %s)   %s" % (
        len(calls), fmt_clock(last_call), fmt_age(last_call, now),
        fmt_age(last_file, now), liveness))
    if pending:
        print("inflight   %d request(s) written, no result yet; oldest %s (%s)" % (
            len(pending), fmt_clock(pending[0][1]), fmt_span(now - pending[0][1])))
    if calls:
        print("tokens     prompt %s   cache %.1f%%   completion %s   reasoning %.0f%%" % (
            fmt_int(prompt), 100.0 * hit / prompt if prompt else 0.0,
            fmt_int(completion), 100.0 * reasoning / completion if completion else 0.0))
    offered = offered_tools(run_dir)
    used, _ = tool_usage(run_dir)
    if offered or used:
        top = " ".join("%s x%d" % (name, count) for name, count in used.most_common(3))
        print("tools      %d offered / %d called%s" % (
            len(offered), sum(used.values()), (" (" + top + ")") if used else " (none)"))
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
            stage_turns = scalar(con, "select count(*) from stage_turns", default=0)
            stage_roads = scalar(con, "select count(*) from stage_roads", default=0)
        finally:
            con.close()

        status = task.get("status") or "-"
        last_call = max((c["started"] for c in calls), default=None)
        last_file = last_file_activity(run_dir)
        idle = now - max(last_call or 0, last_event or 0, last_file or 0)
        prompt = sum(c["prompt"] for c in calls)
        hit = sum(c["hit"] for c in calls)
        completion = sum(c["completion"] for c in calls)
        reasoning = sum(c["reasoning"] for c in calls)
        deadline = task.get("deadline_at")

        flag = ""
        if status == "running" and idle > args.stall:
            flag = "  !! IDLE %s" % fmt_span(idle)
        print("[%s] %s %s  left %s  calls %d (last %s)  prompt %s cache %.1f%% comp %s reas %.0f%%%s" % (
            fmt_clock(now), run_label(run_dir), status,
            fmt_left(deadline, now),
            len(calls), fmt_age(last_call, now), fmt_int(prompt),
            100.0 * hit / prompt if prompt else 0.0,
            fmt_int(completion), 100.0 * reasoning / completion if completion else 0.0, flag))
        print("           events %d (last %s)  facts %d  exec %d  intents %s  budget %s%s" % (
            events, fmt_age(last_event, now), counts["facts"], counts["executions"],
            dict(intents) or "-", sum(charged.values()),
            ("  stage %dT/%dR" % (stage_turns, stage_roads)) if stage_turns else ""))

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

    print("%-4s %-8s %-18s %-14s %-16s %8s %10s %7s %8s %7s %-12s %s" % (
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
            fmt_int(call["reasoning"]), clip(call["finish"], 12), " ".join(flags)))
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
        stage_turns = query(con, "select turn_id, parent_turn_id, intent_json, planning_state, "
                                 "achievement_json, fact_refs_json from stage_turns")
        stage_roads = query(con, "select road_id, turn_id, ordinal, candidate_json, status "
                                 "from stage_roads order by turn_id, ordinal")
        # facts.turn_id points at the stage turn; fact_refs_json is the curated view only
        owned_facts = {row["turn_id"]: row["n"] for row in
                       query(con, "select turn_id, count(*) n from facts group by turn_id")} if stage_turns else {}
        if not stage_turns:
            turns = query(con, "select * from turns order by sequence")
            intents = query(con, "select * from intents order by priority")
            facts = query(con, "select turn_id, status, count(*) as n from facts group by turn_id, status")
    finally:
        con.close()

    if stage_turns:
        roads_by_turn: dict[str, list] = defaultdict(list)
        for row in stage_roads:
            roads_by_turn[row["turn_id"]].append(row)
        for turn in stage_turns:
            intent = json.loads(turn["intent_json"] or "{}")
            refs = json.loads(turn["fact_refs_json"] or "[]")
            parent = ("  parent %s" % turn["parent_turn_id"][:8]) if turn["parent_turn_id"] else ""
            print("Turn %s  [%s]  facts %d (view %d)%s" % (
                turn["turn_id"][:8], turn["planning_state"], owned_facts.get(turn["turn_id"], 0), len(refs), parent))
            print("   intent   %s" % clip(intent.get("description"), 78))
            print("   done when %s" % clip(intent.get("completion_criterion"), 78))
            for road in roads_by_turn.get(turn["turn_id"], []):
                candidate = json.loads(road["candidate_json"] or "{}")
                print("   Road %-2s [%-8s] %s" % (
                    road["ordinal"], road["status"], clip(candidate.get("description"), 70)))
        print("(v3 stage tables: %d turn(s), %d road(s))" % (len(stage_turns), len(stage_roads)))
        return 0

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


def cmd_tools(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    offered = offered_tools(run_dir)
    used, last = tool_usage(run_dir)

    print("offered    %d tools" % len(offered))
    grouped: dict[str, list[str]] = defaultdict(list)
    for name in offered:
        prefix, _, leaf = name.partition(".")
        grouped[prefix].append(leaf or name)
    for prefix in sorted(grouped):
        print("  %-12s %s" % (prefix, ", ".join(sorted(grouped[prefix]))))

    print()
    print("proposed   %d tool actions (valid decisions; inspect receipts for dispatch)" % sum(used.values()))
    if not used:
        print("  (no mcp_call recorded)")
    for name, count in used.most_common():
        print("  %-24s x%-3d last args %s" % (name, count, last.get(name, "")[:96]))
    unused = [n for n in offered if n not in used]
    if unused and args.list_unused:
        print()
        print("never called (%d): %s" % (len(unused), ", ".join(sorted(unused))))
    return 0


def cmd_hints(args) -> int:
    run_dir = resolve_run(args.run, state_dir(args.state_dir))
    rows = correction_hints(run_dir)
    if not rows:
        print("(no correction hints recorded in this run)")
        return 0
    print("%-8s %-10s %-22s %s" % ("clock", "op", "reason", "issues"))
    print("-" * 100)
    for row in rows:
        first = row["issues"][0] if row["issues"] else {}
        print("%-8s %-10s %-22s %s @ %s  expected=%s actual=%s" % (
            fmt_clock(row["at"]), row["identity"], clip(row["reason"], 22),
            first.get("code"), first.get("path"), first.get("expected"), first.get("actual")))
        for extra in row["issues"][1:4]:
            print("%-42s %s @ %s  expected=%s actual=%s" % (
                "", extra.get("code"), extra.get("path"), extra.get("expected"), extra.get("actual")))
    print("(%d correction(s))" % len(rows))
    return 0


def _fmt_iso(value) -> str:
    """Workbench stores ISO-8601 UTC strings; show them in local clock time."""
    if not value:
        return "-"
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return clip(value, 16)
    return fmt_time(stamp)


def cmd_challenge(args) -> int:
    """Bridge a workbench URL (#task/<challenge_id>) to its runs."""
    db = Path(getattr(args, "workbench_db", None) or WORKBENCH_DB)
    if not db.is_absolute():
        db = Path.cwd() / db
    if not db.is_file():
        raise SystemExit("workbench database not found: %s (use --workbench-db)" % db)

    con = connect(db)
    try:
        challenges = query(con, """select id, title, category, status, endpoint, deadline_minutes, updated_at
                                   from challenges order by updated_at desc""")
        if args.challenge:
            chosen = [row for row in challenges if row["id"].startswith(args.challenge)]
            if not chosen:
                raise SystemExit("no challenge matching %r in %s" % (args.challenge, db))
            if len(chosen) > 1:
                raise SystemExit("ambiguous challenge prefix %r: %s" % (
                    args.challenge, ", ".join(row["id"] for row in chosen)))
            challenge = chosen[0]
            runs = query(con, """select run_id, status, exit_code, started_at, finished_at, agent_task_run_id, directory
                                 from runs where challenge_id = ? order by started_at desc""", (challenge["id"],))
        else:
            runs = {}
            for row in challenges:
                runs[row["id"]] = query(con, """select run_id, status, exit_code, started_at
                                                from runs where challenge_id = ? order by started_at desc""",
                                        (row["id"],))
    finally:
        con.close()

    if not args.challenge:
        header = "%-10s %-18s %-9s %-18s %5s %-10s %s" % (
            "challenge", "title", "category", "status", "runs", "latest run", "latest status")
        print(header)
        print("-" * len(header))
        for row in challenges:
            entries = runs.get(row["id"]) or []
            latest = entries[0] if entries else None
            print("%-10s %-18s %-9s %-18s %5d %-10s %s" % (
                row["id"][:8], clip(row["title"], 18), clip(row["category"], 9),
                clip(row["status"], 18), len(entries),
                latest["run_id"][:8] if latest else "-", clip(latest["status"], 20) if latest else "-"))
        return 0

    print("challenge   %s" % challenge["id"])
    print("title       %s" % (challenge["title"] or "-"))
    print("category    %-12s status %-18s deadline %s min" % (
        challenge["category"] or "-", challenge["status"] or "-", challenge["deadline_minutes"]))
    print("endpoint    %s" % (challenge["endpoint"] or "-"))
    print("runs        %d" % len(runs))
    if runs:
        print("  %-10s %-13s %-18s %-5s %s" % ("run", "started", "status", "exit", "task_run"))
        for row in runs:
            task_run = row["agent_task_run_id"] or ""
            if not task_run and row["directory"]:
                # the workbench only fills agent_task_run_id once the run ends
                run_db = locate_db(Path(row["directory"]))
                if run_db:
                    try:
                        side = connect(run_db)
                        try:
                            task_run = scalar(side, "select task_run_id from task_runs") or ""
                        finally:
                            side.close()
                    except sqlite3.Error:
                        pass
            print("  %-10s %-13s %-18s %-5s %s" % (
                row["run_id"][:8], _fmt_iso(row["started_at"]), clip(row["status"], 18),
                row["exit_code"] if row["exit_code"] is not None else "-", (task_run or "-")[:8]))
        print()
        print("next        python scripts/qymonitor.py --state-dir %s show %s" % (
            Path(WORKBENCH_DB).parent.name + "/runs", runs[0]["run_id"][:8]))
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

    p = sub.add_parser("planning", help="planning investment and observed outcomes; no causal quality score")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--json", action="store_true", help="structured observations for matched comparisons")
    p.set_defaults(func=cmd_planning)

    p = sub.add_parser("tree", help="Turn / Intent structure")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("facts", help="facts with their turn")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("tools", help="MCP tools offered to the model vs actually called")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--list-unused", action="store_true", help="also list offered tools never called")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("hints", help="diagnostics sent back with a rejected output")
    p.add_argument("run", nargs="?", default="latest")
    p.set_defaults(func=cmd_hints)

    p = sub.add_parser("challenge", help="workbench challenge -> its runs (bridges the #task/<id> URL)")
    p.add_argument("challenge", nargs="?", help="challenge id or unique prefix; omit to list all")
    p.add_argument("--workbench-db", help="default: workbench_data/workbench.sqlite3")
    p.set_defaults(func=cmd_challenge)

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
