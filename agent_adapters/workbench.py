"""Loopback-only workbench for challenge preparation and run inspection.

The workbench owns its draft database and uploaded files. It does not write the
Agent control database or turn a draft into a TaskRun.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import frontend
from agent_adapters.workbench_contracts import ACTIVE_RUN_STATUSES, WorkbenchError
from agent_core.runtime import TERMINAL_STATUSES

FRONTEND = Path(frontend.__file__).resolve().parent
MAX_JSON_BYTES = 64 * 1024
MAX_ATTACHMENTS = 16
VALID_CATEGORIES = {"pwn", "reverse", "web", "misc", "crypto", "other"}
VALID_PROVIDERS = {"deepseek", "openai_compatible"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise WorkbenchError("invalid_id") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise WorkbenchError("invalid_id")
    return value


def endpoint(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise WorkbenchError("endpoint_required")
    from Sandbox.target_address import TargetAddress, TargetAddressError

    try:
        TargetAddress.from_url(value)
    except TargetAddressError as exc:
        raise WorkbenchError("invalid_endpoint") from exc
    return value


def challenge_fields(value: object) -> tuple[str, str, str, str, int, int]:
    required = {"title", "category", "description", "deadline_minutes", "step_timeout_seconds"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"endpoint", "http_methods"}:
        raise WorkbenchError("invalid_challenge_fields")
    title, category, description = (value[key] for key in ("title", "category", "description"))
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise WorkbenchError("invalid_title")
    if category not in VALID_CATEGORIES:
        raise WorkbenchError("invalid_category")
    if not isinstance(description, str) or len(description) > 16000:
        raise WorkbenchError("invalid_description")
    target = value.get("endpoint")
    if target is not None and target != "":
        target = endpoint(target)
    else:
        target = ""
    deadline, step_timeout = value["deadline_minutes"], value["step_timeout_seconds"]
    if type(deadline) is not int or not 1 <= deadline <= 7 * 24 * 60:
        raise WorkbenchError("invalid_deadline")
    if type(step_timeout) is not int or not 1 <= step_timeout <= min(300, deadline * 60):
        raise WorkbenchError("invalid_step_timeout")
    return title.strip(), category, description, target, deadline, step_timeout


def http_methods(value):
    from agent_core.web_contracts import D7_WEB_ALLOWED_METHODS
    if (not isinstance(value, list) or not value or any(not isinstance(v, str) or v not in D7_WEB_ALLOWED_METHODS for v in value)
            or len(value) != len(set(value))):
        raise WorkbenchError('invalid_http_methods')
    return value


class Workbench:
    def __init__(
        self,
        root: Path,
        *,
        protocol_version: int | None = None,
        d7_mode: str | None = None,
        execution_profile: str = "d7",
    ):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads = self.root / "attachments"
        self.uploads.mkdir(exist_ok=True)
        self.database = self.root / "workbench.sqlite3"
        self.lock = threading.RLock()
        self.provider = "deepseek"
        self.model = ""
        self.base_url = "https://api.deepseek.com"
        self.api_key = ""
        # Preserve legacy v2 by default; v3 defaults to its isolated simulation
        # profile unless the operator explicitly selects the real Web profile.
        if protocol_version is None:
            try:
                protocol_version = int(os.environ.get("QINYUAN_WORKBENCH_PROTOCOL_VERSION", "2"))
            except ValueError as exc:
                raise ValueError("unsupported_workbench_protocol_version") from exc
        if protocol_version not in {2, 3}:
            raise ValueError("unsupported_workbench_protocol_version")
        explicit_d7_mode = d7_mode is not None
        if execution_profile not in {"d7", "d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} or (
            execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} and protocol_version != 3
        ):
            raise ValueError("unsupported_execution_profile")
        if d7_mode is None and execution_profile == "d7":
            d7_mode = "simulated"
        if d7_mode not in {None, "simulated", "real"} or (
            explicit_d7_mode and protocol_version != 3
        ):
            raise ValueError("unsupported_d7_mode")
        self.protocol_version = protocol_version
        self.d7_mode = d7_mode
        self.execution_profile = execution_profile
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    deadline_minutes INTEGER NOT NULL DEFAULT 60,
                    step_timeout_seconds INTEGER NOT NULL DEFAULT 60,
                    status TEXT NOT NULL DEFAULT 'draft',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    challenge_id TEXT NOT NULL REFERENCES challenges(id),
                    name TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attachments_challenge ON attachments(challenge_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(challenges)")}
            if "deadline_minutes" not in columns:
                db.execute("ALTER TABLE challenges ADD COLUMN deadline_minutes INTEGER NOT NULL DEFAULT 60")
            if "step_timeout_seconds" not in columns:
                db.execute("ALTER TABLE challenges ADD COLUMN step_timeout_seconds INTEGER NOT NULL DEFAULT 60")
            if 'http_methods_json' not in columns:
                db.execute("ALTER TABLE challenges ADD COLUMN http_methods_json TEXT NOT NULL DEFAULT '[\"GET\",\"HEAD\",\"OPTIONS\"]'")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    challenge_id TEXT NOT NULL REFERENCES challenges(id),
                    directory TEXT NOT NULL,
                    agent_task_run_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_challenge_updated ON runs(challenge_id, updated_at DESC);
            """)
        from agent_adapters.workbench_runner import Runner
        self.runner = Runner(self)
        from agent_adapters.competition import CompetitionConnections
        self.competitions = CompetitionConnections(self)

    @staticmethod
    def error(code, status=HTTPStatus.BAD_REQUEST):
        return WorkbenchError(code, status)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _get(self, db, challenge_id):
        row = db.execute("SELECT * FROM challenges WHERE id=?", (canonical_id(challenge_id),)).fetchone()
        if row is None:
            raise WorkbenchError("challenge_not_found", HTTPStatus.NOT_FOUND)
        result = dict(row)
        methods = result.pop('http_methods_json')
        if self.execution_profile in {'d8_v20','d8_v21','d8_v22'}:
            result['http_methods'] = http_methods(json.loads(methods))
        result["archived"] = bool(result["archived"])
        result["attachments"] = [dict(item) for item in db.execute(
            "SELECT id,name,byte_size,sha256,created_at FROM attachments WHERE challenge_id=? ORDER BY created_at,id",
            (challenge_id,),
        )]
        run = db.execute(
            "SELECT run_id,agent_task_run_id,status,directory,started_at,finished_at,exit_code,cancel_requested,result_json,updated_at "
            "FROM runs WHERE challenge_id=? ORDER BY rowid DESC LIMIT 1",
            (challenge_id,),
        ).fetchone()
        result["run"] = self._run_dict(run, has_remote_target=bool(result["endpoint"])) if run else None
        if result["run"] and result["run"]["status"] == "recovery_required":
            result["status"] = "recovery_required"
        elif result["status"] == "recovery_required" and result["run"] and result["run"]["status"] == "cleanup_confirmed":
            result["status"] = "cleanup_confirmed"
        return result

    @staticmethod
    def _run_dict(row, *, has_remote_target=False):
        if row is None:
            return None
        result = dict(row)
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        outcome = result["result"]
        cleanup = outcome.get("cleanup") if isinstance(outcome, dict) else None
        task = outcome.get("task_run") if isinstance(outcome, dict) else None
        if (has_remote_target and result["status"] not in {"starting", "running", "cancelling", "cleaning"}
                and isinstance(outcome, dict) and outcome.get("protocol_version") in {2, 3}
                and isinstance(task, dict) and task.get("status") in TERMINAL_STATUSES
                and isinstance(cleanup, dict) and cleanup.get("status") == "succeeded"
                and cleanup.get("target_network_confirmed") is not True):
            outcome["cleanup"] = {**cleanup, "status": "not_confirmed"}
            result["status"] = "recovery_required"
        if (result["status"] == "recovery_required" and isinstance(outcome, dict)
                and outcome.get("protocol_version") in {2, 3}
                and isinstance(outcome.get("task_run"), dict)
                and outcome["task_run"].get("status") in TERMINAL_STATUSES
                and isinstance(outcome.get("cleanup"), dict)
                and outcome["cleanup"].get("status") == "succeeded"):
            result["status"] = "cleanup_confirmed"
        return result

    def _insert_run(self, run_id, challenge_id, directory):
        timestamp = now()
        with self.lock, self._db() as db:
            current = self._get(db, challenge_id)
            if current["archived"] or current["status"] in ACTIVE_RUN_STATUSES:
                raise WorkbenchError("challenge_already_running", HTTPStatus.CONFLICT)
            db.execute(
                "INSERT INTO runs(run_id,challenge_id,directory,status,started_at,updated_at) VALUES(?,?,?,?,?,?)",
                (run_id, challenge_id, str(directory), "starting", timestamp, timestamp),
            )
            db.execute("UPDATE challenges SET status='starting',updated_at=? WHERE id=?", (timestamp, challenge_id))

    def _started_run(self, run_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT challenge_id,cancel_requested FROM runs WHERE run_id=?", (run_id,)).fetchone()
            status = "cancelling" if row["cancel_requested"] else "running"
            db.execute("UPDATE runs SET status=?,updated_at=? WHERE run_id=?", (status, now(), run_id))
            db.execute("UPDATE challenges SET status=?,updated_at=? WHERE id=?", (status, now(), row["challenge_id"]))

    def _mark_cancel_requested(self, run_id):
        with self.lock, self._db() as db:
            db.execute(
                "UPDATE runs SET cancel_requested=1,status='cancelling',updated_at=? WHERE run_id=? AND status IN ('starting','running','cancelling','paused','recovery_required')",
                (now(), run_id),
            )
            db.execute("UPDATE challenges SET status='cancelling',updated_at=? WHERE id=(SELECT challenge_id FROM runs WHERE run_id=? AND status='cancelling')", (now(), run_id))

    def _mark_cleanup_started(self, run_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT challenge_id,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise WorkbenchError("cleanup_not_available", HTTPStatus.CONFLICT)
            current = self._get(db, row["challenge_id"])["run"]
            if current is None or current["run_id"] != run_id or current["status"] != "recovery_required":
                raise WorkbenchError("cleanup_not_available", HTTPStatus.CONFLICT)
            updated = db.execute(
                "UPDATE runs SET status='cleaning',updated_at=? WHERE run_id=? AND status=?",
                (now(), run_id, row["status"]),
            )
            if updated.rowcount != 1:
                raise WorkbenchError("cleanup_not_available", HTTPStatus.CONFLICT)
            db.execute(
                "UPDATE challenges SET status='cleaning',updated_at=? WHERE id=(SELECT challenge_id FROM runs WHERE run_id=?)",
                (now(), run_id),
            )

    def _finish_run(self, run_id, *, status, exit_code, result, agent_task_run_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT challenge_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise WorkbenchError("run_not_found", HTTPStatus.NOT_FOUND)
            timestamp = now()
            db.execute(
                "UPDATE runs SET status=?,agent_task_run_id=?,finished_at=?,exit_code=?,result_json=?,updated_at=? WHERE run_id=?",
                (status, agent_task_run_id, timestamp, exit_code, json.dumps(result, ensure_ascii=False), timestamp, run_id),
            )
            latest = db.execute("SELECT run_id FROM runs WHERE challenge_id=? ORDER BY rowid DESC LIMIT 1", (row[0],)).fetchone()
            if latest[0] == run_id:
                db.execute("UPDATE challenges SET status=?,updated_at=? WHERE id=?", (status, timestamp, row[0]))

    def _set_challenge_status(self, challenge_id, status):
        with self.lock, self._db() as db:
            db.execute("UPDATE challenges SET status=?,updated_at=? WHERE id=?", (status, now(), challenge_id))

    def _current_run(self, challenge_id):
        with self.lock, self._db() as db:
            return self._get(db, challenge_id)["run"]

    def list(self):
        with self.lock, self._db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM challenges ORDER BY updated_at DESC,id DESC")]
            return [self._get(db, item) for item in ids]

    def get(self, challenge_id):
        with self.lock, self._db() as db:
            return self._get(db, challenge_id)

    def create(self, value):
        title, category, description, target, deadline, step_timeout = challenge_fields(value)
        methods = http_methods(value.get('http_methods', ['GET', 'HEAD', 'OPTIONS']))
        if 'http_methods' in value and self.execution_profile not in {'d8_v20','d8_v21','d8_v22'}:
            raise WorkbenchError('http_methods_profile_unsupported')
        challenge_id, timestamp = str(uuid4()), now()
        with self.lock, self._db() as db:
            db.execute(
                "INSERT INTO challenges(id,title,category,description,endpoint,deadline_minutes,step_timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (challenge_id, title, category, description, target, deadline, step_timeout, timestamp, timestamp),
            )
            if self.execution_profile in {'d8_v20','d8_v21','d8_v22'}:
                db.execute('UPDATE challenges SET http_methods_json=? WHERE id=?', (json.dumps(methods), challenge_id))
            return self._get(db, challenge_id)

    def update(self, challenge_id, value):
        title, category, description, target, deadline, step_timeout = challenge_fields(value)
        with self.lock, self._db() as db:
            current = self._get(db, challenge_id)
            methods = http_methods(value.get('http_methods', current.get('http_methods', ['GET', 'HEAD', 'OPTIONS'])))
            if 'http_methods' in value and self.execution_profile not in {'d8_v20','d8_v21','d8_v22'}:
                raise WorkbenchError('http_methods_profile_unsupported')
            if current["status"] != "draft" or current["archived"]:
                raise WorkbenchError("challenge_not_editable", HTTPStatus.CONFLICT)
            db.execute(
                "UPDATE challenges SET title=?,category=?,description=?,endpoint=?,deadline_minutes=?,step_timeout_seconds=?,updated_at=? WHERE id=?",
                (title, category, description, target, deadline, step_timeout, now(), challenge_id),
            )
            if self.execution_profile in {'d8_v20','d8_v21','d8_v22'}:
                db.execute('UPDATE challenges SET http_methods_json=? WHERE id=?', (json.dumps(methods), challenge_id))
            return self._get(db, challenge_id)

    def archive(self, challenge_id):
        with self.lock, self._db() as db:
            current = self._get(db, challenge_id)
            if current["status"] in ACTIVE_RUN_STATUSES:
                raise WorkbenchError("cancel_running_challenge_first", HTTPStatus.CONFLICT)
            db.execute("UPDATE challenges SET archived=1,updated_at=? WHERE id=?", (now(), challenge_id))
            return self._get(db, challenge_id)

    def restore(self, challenge_id):
        with self.lock, self._db() as db:
            self._get(db, challenge_id)
            db.execute("UPDATE challenges SET archived=0,updated_at=? WHERE id=?", (now(), challenge_id))
            return self._get(db, challenge_id)

    def purge(self, challenge_id, confirmation):
        with self.lock:
            with self._db() as db:
                current = self._get(db, challenge_id)
                if current["status"] in ACTIVE_RUN_STATUSES or not current["archived"]:
                    raise WorkbenchError("archive_terminal_challenge_first", HTTPStatus.CONFLICT)
                if confirmation != current["title"]:
                    raise WorkbenchError("confirmation_title_mismatch")
                attachment_paths = []
                for item in current["attachments"]:
                    path = self.uploads / challenge_id / item["id"]
                    if path.is_symlink():
                        raise WorkbenchError("attachment_path_rejected", HTTPStatus.CONFLICT)
                    attachment_paths.append(path)
                folder = self.uploads / challenge_id
                run_directories = []
                for row in db.execute("SELECT run_id FROM runs WHERE challenge_id=?", (challenge_id,)):
                    run_directory = self.root / "runs" / canonical_id(row["run_id"])
                    if run_directory.is_symlink():
                        raise WorkbenchError("run_path_rejected", HTTPStatus.CONFLICT)
                    run_directories.append(run_directory)
                db.execute("DELETE FROM runs WHERE challenge_id=?", (challenge_id,))
                db.execute("DELETE FROM attachments WHERE challenge_id=?", (challenge_id,))
                db.execute("DELETE FROM challenges WHERE id=?", (challenge_id,))
            for path in attachment_paths:
                path.unlink(missing_ok=True)
            if folder.exists():
                folder.rmdir()
            for run_directory in run_directories:
                if run_directory.exists():
                    shutil.rmtree(run_directory)

    def add_attachment(self, challenge_id, name, source, size):
        if not isinstance(name, str) or not name or len(name) > 255 or name in {".", ".."} or any(
            char in name for char in ("/", "\\", "\x00")
        ):
            raise WorkbenchError("invalid_attachment_name")
        if size < 0:
            raise WorkbenchError("invalid_content_length")
        with self.lock, self._db() as db:
            current = self._get(db, challenge_id)
            if current["status"] != "draft" or current["archived"]:
                raise WorkbenchError("challenge_not_editable", HTTPStatus.CONFLICT)
            if len(current["attachments"]) >= MAX_ATTACHMENTS:
                raise WorkbenchError("attachment_count_exceeded")
            attachment_id = str(uuid4())
            folder = self.uploads / challenge_id
            folder.mkdir(exist_ok=True)
            partial = folder / (attachment_id + ".part")
            final = folder / attachment_id
            digest = hashlib.sha256()
            remaining = size
            try:
                with partial.open("xb") as output:
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise WorkbenchError("incomplete_attachment")
                        output.write(block)
                        digest.update(block)
                        remaining -= len(block)
                    output.flush()
                    os.fsync(output.fileno())
                partial.replace(final)
                db.execute(
                    "INSERT INTO attachments(id,challenge_id,name,byte_size,sha256,created_at) VALUES(?,?,?,?,?,?)",
                    (attachment_id, challenge_id, name, size, digest.hexdigest(), now()),
                )
                db.execute("UPDATE challenges SET updated_at=? WHERE id=?", (now(), challenge_id))
                return self._get(db, challenge_id)
            except BaseException:
                partial.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                raise

    def remove_attachment(self, challenge_id, attachment_id):
        canonical_id(attachment_id)
        with self.lock, self._db() as db:
            current = self._get(db, challenge_id)
            if current["status"] != "draft" or current["archived"]:
                raise WorkbenchError("challenge_not_editable", HTTPStatus.CONFLICT)
            if not any(item["id"] == attachment_id for item in current["attachments"]):
                raise WorkbenchError("attachment_not_found", HTTPStatus.NOT_FOUND)
            path = self.uploads / challenge_id / attachment_id
            if path.is_symlink():
                raise WorkbenchError("attachment_path_rejected", HTTPStatus.CONFLICT)
            path.unlink(missing_ok=True)
            db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
            db.execute("UPDATE challenges SET updated_at=? WHERE id=?", (now(), challenge_id))
            return self._get(db, challenge_id)

    def model_status(self):
        with self.lock:
            return {
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url,
                "key_configured": bool(self.api_key),
                "protocol_version": self.protocol_version,
                "dual_llm_enabled": hasattr(self, 'outer_model') and hasattr(self, 'inner_model'),
                "outer_model": getattr(self, 'outer_model', None),
                "inner_model": getattr(self, 'inner_model', None),
            }

    def configure_model(self, value):
        # Support both single-model and dual-LLM configuration
        if isinstance(value, dict) and "outer_model" in value and "inner_model" in value:
            # Dual-LLM configuration mode
            if set(value) != {"outer_model", "inner_model", "api_key"}:
                raise WorkbenchError("invalid_dual_model_fields")
            outer = value["outer_model"]
            inner = value["inner_model"]
            api_key = value["api_key"]

            # Validate outer model
            if not isinstance(outer, str) or not outer.strip() or len(outer) > 128:
                raise WorkbenchError("invalid_outer_model")
            # Validate inner model
            if not isinstance(inner, str) or not inner.strip() or len(inner) > 128:
                raise WorkbenchError("invalid_inner_model")
            # Validate API key
            if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 8192 or any(ord(char) < 32 for char in api_key):
                raise WorkbenchError("invalid_model_key")

            with self.lock:
                self.outer_model = outer.strip()
                self.inner_model = inner.strip()
                self.api_key = api_key
                # Set model to outer_model as default (used for protocol < 3)
                # Protocol 3 will use outer_model and inner_model separately
                self.model = outer.strip()
                return self.model_status()

        # Legacy single-model configuration mode
        if not isinstance(value, dict) or set(value) != {"provider", "model", "base_url", "api_key"}:
            raise WorkbenchError("invalid_model_fields")
        provider, model, base_url, api_key = (value[key] for key in ("provider", "model", "base_url", "api_key"))
        if provider not in VALID_PROVIDERS or not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise WorkbenchError("invalid_model_configuration")
        if provider == "deepseek":
            if base_url not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
                raise WorkbenchError("invalid_deepseek_url")
        elif not isinstance(base_url, str) or len(base_url) > 2048:
            raise WorkbenchError("invalid_model_url")
        if provider == "openai_compatible":
            parsed = urlsplit(base_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise WorkbenchError("invalid_model_url")
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 8192 or any(ord(char) < 32 for char in api_key):
            raise WorkbenchError("invalid_model_key")
        with self.lock:
            self.provider, self.model, self.base_url, self.api_key = provider, model.strip(), base_url.rstrip("/"), api_key
            # Clear dual-LLM fields if they exist
            if hasattr(self, 'outer_model'):
                delattr(self, 'outer_model')
            if hasattr(self, 'inner_model'):
                delattr(self, 'inner_model')
            return self.model_status()

    def clear_model_key(self):
        with self.lock:
            self.api_key = ""
            return self.model_status()


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchServer

    def log_message(self, _format, *_args):
        # Requests can contain user-supplied paths and query strings.
        return

    def _trusted(self):
        host = self.headers.get("Host", "").lower()
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            raise WorkbenchError("invalid_host", HTTPStatus.FORBIDDEN)
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://{item}" for item in allowed}:
            raise WorkbenchError("invalid_origin", HTTPStatus.FORBIDDEN)

    def _send(self, status, content, content_type="application/json; charset=utf-8"):
        data = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True

    def _json(self):
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise WorkbenchError("json_required")
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise WorkbenchError("invalid_content_length") from exc
        if not 0 <= size <= MAX_JSON_BYTES:
            raise WorkbenchError("invalid_content_length")
        try:
            return json.loads(self.rfile.read(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchError("invalid_json") from exc

    def _dispatch(self):
        self._trusted()
        parsed = urlsplit(self.path)
        parts = parsed.path.strip("/").split("/")
        if self.command == "GET" and parsed.path in {"/", "/index.html", "/app.js", "/run-view.js", "/competition.js", "/styles.css", "/run.css"}:
            name = "index.html" if parsed.path == "/" else parsed.path[1:]
            mime = "text/html" if name.endswith(".html") else "text/javascript" if name.endswith(".js") else "text/css"
            self._send(HTTPStatus.OK, (FRONTEND / name).read_bytes(), mime + "; charset=utf-8")
            return
        if parts == ["api", "health"] and self.command == "GET":
            self._send(HTTPStatus.OK, {"status": "ready", "agent_execution": "connected", "capabilities": {
                "current_cli": (
                    "run-task-v3-d8"
                    if self.server.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}
                    else f"run-task-v3-{self.server.workbench.d7_mode}"
                    if self.server.workbench.protocol_version == 3
                    else "t6_run_task_real"
                ),
                "protocol_version": self.server.workbench.protocol_version,
                "d7_mode": self.server.workbench.d7_mode if self.server.workbench.protocol_version == 3 else None,
                "t6_scheduler": self.server.workbench.protocol_version == 2,
                "remote_network": (
                    self.server.workbench.execution_profile in {'d8_v20','d8_v21','d8_v22'} or
                    self.server.workbench.execution_profile not in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} and (
                    self.server.workbench.protocol_version == 2
                    or self.server.workbench.protocol_version == 3
                    and self.server.workbench.d7_mode == "real"
                    )
                ),
                **({"execution_profile": self.server.workbench.execution_profile,
                    "contract_version": int(self.server.workbench.execution_profile.removeprefix("d8_v")),
                    "mode_selection_required": self.server.workbench.d7_mode is None,
                    "real_supported": True, "real_model": "deepseek-flash",
                    "real_sandbox_backend": "docker" if self.server.workbench.execution_profile in {"d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} else "local_simulated"}
                   if self.server.workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} else {}),
            }})
            return
        if parts == ["api", "competition", "profiles"]:
            if self.command == "GET":
                self._send(HTTPStatus.OK, self.server.workbench.competitions.list())
            elif self.command == "POST":
                self._send(HTTPStatus.CREATED, self.server.workbench.competitions.save(self._json()))
            else:raise WorkbenchError("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if len(parts) in {4, 5} and parts[:3] == ["api", "competition", "profiles"]:
            identity = canonical_id(parts[3])
            service = self.server.workbench.competitions
            if len(parts) == 4 and self.command == "PUT":
                self._send(HTTPStatus.OK, service.save(self._json(), identity))
            elif len(parts) == 5 and parts[4] == "credential" and self.command == "DELETE":
                self._send(HTTPStatus.OK, service.clear(identity))
            elif len(parts) == 5 and parts[4] == "check" and self.command == "POST":
                if self._json():raise WorkbenchError("competition_check_fields_invalid")
                self._send(HTTPStatus.OK, service.check(identity))
            else:raise WorkbenchError("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if parts == ["api", "model"]:
            if self.command == "GET":
                self._send(HTTPStatus.OK, self.server.workbench.model_status())
            elif self.command == "PUT":
                self._send(HTTPStatus.OK, self.server.workbench.configure_model(self._json()))
            else:
                raise WorkbenchError("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if parts == ["api", "model", "key"] and self.command == "DELETE":
            self._send(HTTPStatus.OK, self.server.workbench.clear_model_key())
            return
        if parts == ["api", "challenges"]:
            if self.command == "GET":
                self._send(HTTPStatus.OK, self.server.workbench.list())
            elif self.command == "POST":
                self._send(HTTPStatus.CREATED, self.server.workbench.create(self._json()))
            else:
                raise WorkbenchError("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if len(parts) >= 3 and parts[:2] == ["api", "challenges"]:
            # Batch cleanup route - must be checked before canonical_id validation
            if len(parts) == 3 and parts[2] == "batch-cleanup" and self.command == "POST":
                workbench = self.server.workbench
                challenge_ids = self._json().get("challenge_ids", [])
                if not isinstance(challenge_ids, list) or len(challenge_ids) > 50:
                    raise WorkbenchError("invalid_batch_cleanup_request")
                results = workbench.runner.batch_force_delete(challenge_ids)
                self._send(HTTPStatus.OK, {"results": results})
                return
            # Regular challenge routes
            challenge_id = canonical_id(parts[2])
            workbench = self.server.workbench
            if len(parts) == 3:
                if self.command == "GET":
                    self._send(HTTPStatus.OK, workbench.get(challenge_id))
                elif self.command == "PUT":
                    self._send(HTTPStatus.OK, workbench.update(challenge_id, self._json()))
                else:
                    raise WorkbenchError("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
                return
            if len(parts) == 4 and parts[3] == "run":
                if self.command == "POST":
                    if workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
                        body = self._json() if int(self.headers.get("Content-Length", "0")) else {}
                        if not isinstance(body, dict) or set(body) - {"mode"}:
                            raise WorkbenchError("invalid_run_request", 400)
                        result = workbench.runner.start(challenge_id, mode=body.get("mode"))
                    else:
                        result = workbench.runner.start(challenge_id)
                    self._send(HTTPStatus.ACCEPTED, result)
                elif self.command == "GET":
                    self._send(HTTPStatus.OK, workbench.runner.status(challenge_id))
                else:
                    raise WorkbenchError("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
                return
            if len(parts) == 4 and parts[3] == "events" and self.command == "GET":
                self._send(HTTPStatus.OK, workbench.runner.events(challenge_id))
                return
            if len(parts) == 4 and parts[3] == "progress" and self.command == "GET":
                self._send(HTTPStatus.OK, workbench.runner.progress(challenge_id))
                return
            if len(parts) == 5 and parts[3] == "executions" and self.command == "GET":
                self._send(HTTPStatus.OK, workbench.runner.execution(challenge_id, canonical_id(parts[4])))
                return
            if len(parts) == 4 and parts[3] == "cancel" and self.command == "POST":
                self._send(HTTPStatus.ACCEPTED, workbench.runner.cancel(challenge_id))
                return
            if len(parts) == 4 and parts[3] == 'resume' and self.command == 'POST':
                body = self._json()
                if body:
                    raise WorkbenchError('d9_resume_fields_invalid')
                try:
                    result = workbench.runner.resume(challenge_id)
                except WorkbenchError:
                    raise
                except ValueError as exc:
                    raise WorkbenchError('d9_resume_input_invalid') from exc
                self._send(HTTPStatus.ACCEPTED, result)
                return
            if len(parts) == 4 and parts[3] == 'validation-check' and self.command == 'POST':
                try:
                    result = workbench.runner.validation_check(challenge_id, self._json())
                except WorkbenchError:
                    raise
                except ValueError as exc:
                    raise WorkbenchError('d9_validation_check_fields_invalid') from exc
                self._send(HTTPStatus.ACCEPTED, result)
                return
            if len(parts) == 4 and parts[3] == "cleanup" and self.command == "POST":
                self._send(HTTPStatus.ACCEPTED, workbench.runner.cleanup(challenge_id))
                return
            if len(parts) == 4 and parts[3] in {"archive", "restore", "purge"} and self.command == "POST":
                action = parts[3]
                result = workbench.archive(challenge_id) if action == "archive" else workbench.restore(challenge_id) if action == "restore" else workbench.purge(challenge_id, self._json().get("confirm_title"))
                self._send(HTTPStatus.OK, {"purged": True} if action == "purge" else result)
                return
            if len(parts) == 4 and parts[3] == "attachments" and self.command == "POST":
                name = parse_qs(parsed.query, keep_blank_values=True).get("name", [""])[0]
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                except ValueError as exc:
                    raise WorkbenchError("invalid_content_length") from exc
                self._send(HTTPStatus.CREATED, workbench.add_attachment(challenge_id, name, self.rfile, size))
                return
            if len(parts) == 5 and parts[3] == "attachments" and self.command == "DELETE":
                self._send(HTTPStatus.OK, workbench.remove_attachment(challenge_id, parts[4]))
                return
        raise WorkbenchError("not_found", HTTPStatus.NOT_FOUND)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        try:
            self._dispatch()
        except WorkbenchError as exc:
            self._send(exc.status, {"error": exc.code})
        except (OSError, sqlite3.Error):
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "storage_failure"})


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, workbench):
        super().__init__(address, Handler)
        self.workbench = workbench

    def server_close(self):
        # A workbench owns the child processes it started.  Stop them before
        # releasing the listening socket so a restart cannot orphan a run.
        self.workbench.runner.shutdown()
        super().server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="qinyuan-workbench",
        description="Start the local challenge-management backend, browser UI, and Agent process runner.",
    )
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument("--data-dir", type=Path, default=Path("workbench_data"))
    parser.add_argument("--protocol-version", type=int, choices=(2, 3), default=None)
    parser.add_argument("--d7-mode", choices=("simulated", "real"), default=None)
    parser.add_argument("--execution-profile", choices=("d7", "d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"), default=None)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        # Public launcher defaults to D8; explicit legacy protocol/profile
        # selections and historical Python assembly retain their old behavior.
        protocol = args.protocol_version
        if protocol is None:
            protocol = int(os.environ.get("QINYUAN_WORKBENCH_PROTOCOL_VERSION", "3"))
        profile = args.execution_profile or ("d8_v22" if protocol == 3 else "d7")
        workbench = Workbench(
            args.data_dir,
            protocol_version=protocol,
            d7_mode=args.d7_mode,
            execution_profile=profile,
        )
        server = WorkbenchServer(("127.0.0.1", args.port), workbench)
    except ValueError as exc:
        parser.exit(2, f"Cannot start backend: {exc}.\n")
    except OSError as exc:
        if isinstance(exc, PermissionError):
            parser.exit(1, "Cannot start backend: permission denied for the data directory or local port.\n")
        if getattr(exc, "winerror", None) == 10048 or exc.errno in {48, 98, 10048}:
            parser.exit(1, f"Port {args.port} is already in use. Check the running backend or choose --port <number>.\n")
        parser.exit(1, f"Cannot start backend: {type(exc).__name__}. Check the data directory and local port.\n")
    except sqlite3.Error:
        parser.exit(1, "Cannot open the workbench database. Check the data directory and permissions.\n")
    with server:
        print("Qinyuan local workbench backend", flush=True)
        print(f"Open: http://127.0.0.1:{server.server_port}/", flush=True)
        print(f"Data: {workbench.root}", flush=True)
        print("Management API: ready (challenges, attachments, model settings)", flush=True)
        if workbench.protocol_version == 3:
            if workbench.execution_profile in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
                print(f"Agent execution: protocol v3 / D8 contract {int(workbench.execution_profile.removeprefix('d8_v'))} (select model mode explicitly; local simulated Sandbox)", flush=True)
            elif workbench.d7_mode == "real":
                print("Agent execution: run-task protocol v3 real (authorized single-origin Web)", flush=True)
            else:
                print("Agent execution: run-task protocol v3 simulated (attachment_digest_only; no network)", flush=True)
        else:
            print("Agent execution: T6 run-task real (0-16 local attachments, 64 MiB total; public target endpoint optional)", flush=True)
        print("Keep this terminal open. Press Ctrl+C to stop the backend.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nWorkbench backend stopped.", flush=True)


if __name__ == "__main__":
    main()
