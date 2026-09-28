"""Version-two CLI entry while T5 run-intent remains unchanged."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from uuid import uuid4

from agent_adapters.deepseek_model import (
    DeepSeekSettings,
    t6_output_tokens_from_env,
    t6_step_timeout_from_env,
)
from agent_adapters.event_export import read_events
from agent_adapters.run_files import (
    encode,
    immutable_read,
    immutable_write,
    parse_json,
    read_json,
    run_lock,
    run_path,
)
from agent_adapters.task_application import prepare_attachments
from agent_adapters.task_contract import TaskRunRequest, validate_task_fixture
from agent_adapters.task_result import snapshot_task_result
from agent_core.runtime import TERMINAL_STATUSES


def _input(path: str):
    return (
        parse_json(sys.stdin.buffer.read(1024 * 1024 + 1))
        if path == "-" else read_json(Path(path))
    )


def dispatch_run_task(args):
    from agent_adapters.cli import supervise

    raw = _input(args.request)
    request = TaskRunRequest.parse(raw)
    fixture = None
    settings = None
    output_tokens = t6_output_tokens_from_env()
    step_timeout = t6_step_timeout_from_env()
    if args.mode == "simulated":
        if not args.fixture:
            raise ValueError("simulated_fixture_required")
        fixture = validate_task_fixture(read_json(Path(args.fixture)))
    else:
        if args.fixture:
            raise ValueError("fixture_forbidden_in_real_mode")
        settings = DeepSeekSettings.from_runtime_env(Path(args.env_file))
        if settings.api_key in encode(raw).decode("utf-8"):
            raise ValueError("credentials_in_request_rejected")
    source_root = str(Path.cwd().resolve())
    attachments = prepare_attachments(source_root, request)
    task_id = str(uuid4())
    directory = run_path(Path(args.state_dir), task_id)
    directory.mkdir(parents=True, exist_ok=False)
    declaration = {
        "protocol_version": 2,
        "task_run_id": task_id,
        "mode": args.mode,
        "request": raw,
        "source_root": source_root,
        "attachments": [
            {"name": item.name, "byte_size": item.byte_size, "sha256": item.sha256}
            for item in attachments
        ],
        "fixture": fixture,
        "env_file": str(Path(args.env_file).resolve()) if args.mode == "real" else None,
        "t6_output_tokens": output_tokens,
        "t6_step_timeout_seconds": step_timeout,
        "ids": {
            "start": str(uuid4()), "cancel": str(uuid4()),
            "authorization": str(uuid4()),
            "imports": [str(uuid4()) for _ in attachments],
            "copies": [str(uuid4()) for _ in attachments],
        },
    }
    immutable_write(directory / "run.json", declaration)
    return supervise(directory, declaration, "start", settings=settings, events=args.events)


def _current_fixture(directory: Path, declaration: dict) -> dict:
    updates = sorted(directory.glob("fixture-update-*.json"), key=lambda path: path.stat().st_mtime_ns)
    return immutable_read(updates[-1]) if updates else declaration["fixture"]


def dispatch_existing(args, directory: Path, declaration: dict):
    from agent_adapters.cli import supervise

    task_id = declaration["task_run_id"]
    if args.command == "replay":
        events = read_events(directory / "control.sqlite", task_id,
                             args.after_sequence, args.limit)
        return {
            "protocol_version": 2, "task_run_id": task_id, "events": events,
            "next_sequence": events[-1]["sequence"] if events else args.after_sequence,
            "exit_code": 0,
        }
    result = snapshot_task_result(directory, declaration)
    if args.command == "status":
        return result
    terminal = result["task_run"] and result["task_run"]["status"] in TERMINAL_STATUSES
    if args.command == "cancel":
        if terminal or result["task_run"] is None:
            return result
        immutable_write(directory / "cancel-request.json", {"task_run_id": task_id})
        try:
            with run_lock(directory):
                pass
        except ValueError:
            end = time.monotonic() + 4.7
            while time.monotonic() < end:
                result = snapshot_task_result(directory, declaration)
                if result["cleanup"]["status"] == "succeeded":
                    return result
                time.sleep(0.04)
            result["errors"].append({"code": "cancel_cleanup_not_confirmed"})
            result["exit_code"] = 5
            return result
        return supervise(directory, declaration, "cancel")
    if args.cleanup_only:
        if not terminal:
            raise ValueError("cleanup_requires_terminal_task")
        if result["cleanup"]["status"] == "succeeded":
            return result
        return supervise(directory, declaration, "cleanup")
    if args.verify_import:
        if declaration["mode"] != "real" or args.authorization or args.fixture:
            raise ValueError("invalid_import_recovery_request")
        if terminal or result["recovery"]["reason"] != "import_outcome_unknown":
            return result
        return supervise(directory, declaration, "recover-import")
    if terminal or result["recovery"]["required"]:
        return result
    if args.authorization:
        auth = read_json(Path(args.authorization))
        if auth != declaration["request"]["authorization"]:
            raise ValueError("authorization_changed")
    else:
        raise ValueError("current_authorization_required")
    settings = None
    if declaration["mode"] == "real":
        if args.fixture:
            raise ValueError("fixture_forbidden_in_real_mode")
        if t6_output_tokens_from_env() != declaration["t6_output_tokens"]:
            raise ValueError("t6_output_tokens_changed")
        if t6_step_timeout_from_env() != declaration.get("t6_step_timeout_seconds", 60):
            raise ValueError("t6_step_timeout_changed")
        settings = DeepSeekSettings.from_runtime_env(Path(declaration["env_file"]))
    elif args.fixture:
        updated = validate_task_fixture(read_json(Path(args.fixture)))
        original = _current_fixture(directory, declaration)
        if any(updated[name][:len(original[name])] != original[name]
               for name in ("turns", "actions", "commands")):
            raise ValueError("simulated_fixture_history_changed")
        with run_lock(directory):
            immutable_write(directory / ("fixture-update-" + str(uuid4()) + ".json"), updated)
    return supervise(directory, declaration, "resume", settings=settings, events=args.events)
