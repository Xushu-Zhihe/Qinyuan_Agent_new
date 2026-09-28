"""Version-one synchronous CLI. The parent owns output and cancellation time."""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path
from uuid import uuid4

from agent_adapters.application import (
    AgentApplication,
    Cancellation,
    base_result,
    safe_error,
    snapshot_result,
)
from agent_adapters.cli_contract import IntentRunRequest, validate_authorization
from agent_adapters.deepseek_model import DeepSeekSettings
from agent_adapters.event_export import EventExporter, read_events
from agent_adapters.process_tree import ProcessTree
from agent_adapters.run_files import (
    encode,
    immutable_read,
    immutable_write,
    parse_json,
    read_json,
    run_lock,
    run_path,
)
from agent_adapters.simulated_runtime import validate_fixture
from agent_core.attachment_import import AttachmentImportRejected
from agent_core.runtime import TERMINAL_STATUSES


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_cli_arguments")


def parser():
    result = Parser(prog="qinyuan-agent-new")
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    run = commands.add_parser("run-intent")
    run.add_argument("--request", required=True)
    run.add_argument("--mode", choices=("real", "simulated"), required=True)
    run.add_argument("--fixture", help="JSON fixture, required only in simulated mode")
    run.add_argument("--env-file", default=".env")
    run.add_argument("--state-dir", required=True)
    run.add_argument("--events")
    task = commands.add_parser("run-task")
    task.add_argument("--request", required=True)
    task.add_argument("--mode", choices=("real", "simulated"), required=True)
    task.add_argument("--fixture", help="ordered v2 fixture, simulated mode only")
    task.add_argument("--env-file", default=".env")
    task.add_argument("--state-dir", required=True)
    task.add_argument("--events")
    for command in ("resume", "cancel", "status", "replay"):
        item = commands.add_parser(command)
        item.add_argument("--task-run-id", required=True)
        item.add_argument("--state-dir", required=True)
        if command == "resume":
            item.add_argument("--authorization")
            item.add_argument("--fixture", help="append-only v2 simulated fixture")
            group = item.add_mutually_exclusive_group()
            group.add_argument("--cleanup-only", action="store_true")
            group.add_argument("--verify-import", action="store_true")
            item.add_argument("--events")
        if command == "replay":
            item.add_argument("--after-sequence", type=int, default=0)
            item.add_argument("--limit", type=int, default=128)
    return result


def _snapshot(directory, declaration):
    try:
        if declaration["protocol_version"] == 2:
            from agent_adapters.task_result import snapshot_task_result

            return snapshot_task_result(directory, declaration)
        return snapshot_result(directory, declaration)
    except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
        result = base_result(declaration["task_run_id"], declaration["mode"])
        result["errors"].append(
            {"code": "state_read_failed", "category": type(exc).__name__}
        )
        return result


def _worker(
    gate,
    cancel,
    directory_text,
    declaration,
    operation,
    auth,
    settings,
    output_name,
    verify_import=False,
):
    # Only the supervisor owns stdout/stderr, including library diagnostics.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    if os.name != "nt":
        os.setsid()
    gate.wait()  # No external work until parent has installed tree ownership.
    directory = Path(directory_text)
    try:
        with run_lock(directory):
            if declaration["protocol_version"] == 2:
                _owned_task_operation(
                    directory, declaration, cancel, operation, settings, output_name,
                )
            else:
                _owned_operation(
                    directory, declaration, cancel, operation, auth, settings,
                    output_name, verify_import,
                )
    except ValueError as exc:
        if str(exc) != "task_run_busy":
            raise
        result = _snapshot(directory, declaration)
        result["errors"].append({"code": "task_run_busy"})
        result["exit_code"] = 4
        immutable_write(directory / output_name, result)


def _owned_task_operation(directory, declaration, cancel, operation, settings, output_name):
    from agent_adapters.task_application import TaskApplication

    app = None
    errors = []
    try:
        token = Cancellation(cancel, directory)
        app = TaskApplication(directory, declaration, token, settings=settings)
        if operation == "cancel":
            app.controller.attach(declaration["task_run_id"])
            if app.state.task_run.status not in TERMINAL_STATUSES:
                app.controller.cancel(declaration["ids"]["cancel"])
        elif operation == "cleanup":
            app.controller.attach(declaration["task_run_id"])
            app.controller.cleanup_sandbox()
        elif operation == "recover-import":
            app.controller.attach(declaration["task_run_id"])
            app.recover_imports()
        else:
            asyncio.run(app.execute(start=operation == "start"))
    except Exception as exc:  # noqa: BLE001 -- sanitized process boundary
        errors.append(safe_error(exc))
        if operation == "start" and app is not None and app.store.load_task_run(
            declaration["task_run_id"]
        ) is None:
            immutable_write(directory / "start-failure.json", errors[-1])
    finally:
        if app is not None:
            try:
                app.close()
            except Exception as exc:  # noqa: BLE001 -- cleanup result is separate
                errors.append(safe_error(exc))
    result = _snapshot(directory, declaration)
    result["errors"].extend(errors)
    if errors and result["exit_code"] == 4:
        result["exit_code"] = 5
    immutable_write(directory / output_name, result)


def _owned_operation(
    directory,
    declaration,
    cancel,
    operation,
    auth,
    settings,
    output_name,
    verify_import,
):
    app = None
    errors = []
    try:
        token = Cancellation(cancel, directory)
        app = AgentApplication(directory, declaration, token, settings=settings)
        if operation == "cancel":
            app.controller.attach(declaration["task_run_id"])
            if app.state.task_run.status not in TERMINAL_STATUSES:
                app.controller.cancel(declaration["ids"]["cancel"])
        elif operation == "cleanup":
            app.controller.attach(declaration["task_run_id"])
            app.controller.cleanup_sandbox()
        elif verify_import:
            app.controller.attach(declaration["task_run_id"])
            if not app.interrupt_if_needed():
                asyncio.run(
                    app.importer.recover_import(declaration["ids"]["import"], token)
                )
            if auth is not None and app.state.task_run.status not in TERMINAL_STATUSES:
                asyncio.run(
                    app.execute(start=False, authorization=auth, already_attached=True)
                )
        else:
            asyncio.run(app.execute(start=operation == "start", authorization=auth))
    except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
        errors.append(safe_error(exc))
        error = errors[-1]
        if operation == "start" and error.get("outcome") in {
            "not_started",
            "creation_failed",
        }:
            immutable_write(directory / "start-failure.json", error)
        if app is not None:
            try:
                if app.store.load_task_run(declaration["task_run_id"]) is not None:
                    app.interrupt_if_needed()
            except Exception as cleanup_exc:  # noqa: BLE001 -- report cleanup separately
                errors.append(safe_error(cleanup_exc))
    finally:
        if app is not None:
            try:
                app.close()
            except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
                errors.append(safe_error(exc))
    result = _snapshot(directory, declaration)
    result["errors"].extend(errors)
    if errors and result["exit_code"] == 0:
        result["exit_code"] = 3
    immutable_write(directory / output_name, result)


def supervise(
    directory,
    declaration,
    operation,
    *,
    auth=None,
    settings=None,
    events=None,
    verify_import=False,
):
    context = multiprocessing.get_context("spawn")
    cancel, gate = context.Event(), context.Event()
    output_name = "operation-" + str(uuid4()) + ".json"
    worker = context.Process(
        target=_worker,
        args=(
            gate,
            cancel,
            str(directory),
            declaration,
            operation,
            auth,
            settings,
            output_name,
            verify_import,
        ),
    )
    tree, exporter = None, None
    interrupted_at = None

    def interrupt(_number, _frame):
        nonlocal interrupted_at
        if interrupted_at is None:
            interrupted_at = time.monotonic()
        cancel.set()

    old = signal.signal(signal.SIGINT, interrupt)
    old_break = (
        signal.signal(signal.SIGBREAK, interrupt)
        if hasattr(signal, "SIGBREAK")
        else None
    )
    forced = False
    started = time.monotonic()
    try:
        worker.start()
        tree = ProcessTree(worker.pid)
        if events:
            exporter = EventExporter(
                Path(events), directory / "control.sqlite", declaration["task_run_id"]
            )
        gate.set()
        while worker.is_alive():
            if (
                operation != "cleanup"
                and (directory / "cancel-request.json").exists()
                and interrupted_at is None
            ):
                interrupt(None, None)
            if operation == "cancel" and interrupted_at is None:
                interrupted_at = started
            if interrupted_at is not None and time.monotonic() - interrupted_at >= 4.1:
                forced = True
                tree.close()
                worker.kill()
                break
            if exporter:
                exporter.tick()
            worker.join(0.025)
        worker.join(0.15)
        if exporter:
            exporter.finish.set()
            end = min(
                time.monotonic() + 2.1,
                interrupted_at + 4.5 if interrupted_at else float("inf"),
            )
            while not exporter.error and not exporter.done and time.monotonic() < end:
                exporter.tick()
                time.sleep(0.01)
            if not exporter.done and not exporter.error:
                exporter.error = "event_export_timeout"
        output = directory / output_name
        result = (
            immutable_read(output)
            if output.exists()
            else _snapshot(directory, declaration)
        )
        if forced or not output.exists():
            result["errors"].append(
                {
                    "code": "worker_interrupted"
                    if forced
                    else "worker_exited_without_result"
                }
            )
            if result["exit_code"] not in (5, 130):
                result["exit_code"] = 4
        if exporter:
            result["event_export"] = {
                "last_sequence": exporter.sequence,
                "error": exporter.error,
            }
            if exporter.error:
                result["errors"].append({"code": exporter.error})
                if result["exit_code"] in (0, 2, 3):
                    result["exit_code"] = 3
        immutable_write(directory / ("report-" + output_name), result)
        return result
    except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
        result = _snapshot(directory, declaration)
        result["errors"].append(safe_error(exc))
        if result["exit_code"] not in (4, 5, 130):
            result["exit_code"] = 4
        return result
    finally:
        if tree:
            tree.close()
        if worker.pid is not None:
            if worker.is_alive():
                worker.kill()
            worker.join(0.15)
            if not worker.is_alive():
                worker.close()
        if exporter:
            exporter.close()
        signal.signal(signal.SIGINT, old)
        if old_break is not None:
            signal.signal(signal.SIGBREAK, old_break)


def dispatch(args):
    settings = None
    if args.command == "run-task":
        from agent_adapters.task_cli import dispatch_run_task

        return dispatch_run_task(args)
    if args.command == "run-intent":
        value = (
            parse_json(sys.stdin.buffer.read(1024 * 1024 + 1))
            if args.request == "-"
            else read_json(Path(args.request))
        )
        request = IntentRunRequest.parse(value)
        fixture = None
        if args.mode == "simulated":
            if not args.fixture:
                raise ValueError("simulated_fixture_required")
            fixture = validate_fixture(read_json(Path(args.fixture)))
        else:
            if args.fixture:
                raise ValueError("fixture_forbidden_in_real_mode")
            settings = DeepSeekSettings.from_runtime_env(Path(args.env_file))
            if settings.api_key in encode(value).decode("ascii"):
                raise ValueError("credentials_in_request_rejected")
        # Existing importer performs the same checks again at dispatch (TOCTOU).
        from agent_core.attachment_importer import validate_local_attachment_source

        try:
            validate_local_attachment_source(
                str(Path.cwd().resolve()), request.task.target.attachments[0].value
            )
        except AttachmentImportRejected as exc:
            raise ValueError(exc.code) from None
        task_id = str(uuid4())
        directory = run_path(Path(args.state_dir), task_id)
        directory.mkdir(parents=True, exist_ok=False)
        declaration = {
            "protocol_version": 1,
            "task_run_id": task_id,
            "mode": args.mode,
            "request": value,
            "source_root": str(Path.cwd().resolve()),
            "fixture": fixture,
            "env_file": str(Path(args.env_file).resolve())
            if args.mode == "real"
            else None,
            "ids": {
                name: str(uuid4())
                for name in (
                    "start",
                    "import",
                    "registration",
                    "select",
                    "bind",
                    "finish",
                    "cancel",
                )
            },
        }
        immutable_write(directory / "run.json", declaration)
        return supervise(
            directory,
            declaration,
            "start",
            auth=value["authorization"],
            settings=settings,
            events=args.events,
        )
    directory = run_path(Path(args.state_dir), args.task_run_id)
    declaration = immutable_read(directory / "run.json")
    if declaration["task_run_id"] != args.task_run_id:
        raise ValueError("run_identity_mismatch")
    if declaration["protocol_version"] == 2:
        from agent_adapters.task_cli import dispatch_existing

        return dispatch_existing(args, directory, declaration)
    if args.command == "replay":
        events = read_events(
            directory / "control.sqlite",
            args.task_run_id,
            args.after_sequence,
            args.limit,
        )
        return {
            "protocol_version": 1,
            "task_run_id": args.task_run_id,
            "events": events,
            "next_sequence": events[-1]["sequence"] if events else args.after_sequence,
            "exit_code": 0,
        }
    result = _snapshot(directory, declaration)
    if args.command == "status":
        return result
    terminal = result["task_run"] and result["task_run"]["status"] in TERMINAL_STATUSES
    if args.command == "cancel":
        if terminal:
            return result
        if result["task_run"] is None:
            return result
        immutable_write(
            directory / "cancel-request.json", {"task_run_id": args.task_run_id}
        )
        try:
            with run_lock(directory):
                pass
        except ValueError:
            end = time.monotonic() + 4.7
            while time.monotonic() < end:
                result = _snapshot(directory, declaration)
                if result["cleanup"]["status"] == "succeeded":
                    return result
                time.sleep(0.04)
            return result
        return supervise(directory, declaration, "cancel")
    if args.cleanup_only:
        if not terminal:
            raise ValueError("cleanup_requires_terminal_task")
        if result["cleanup"]["status"] == "succeeded":
            return result
        return supervise(directory, declaration, "cleanup")
    if terminal:
        return result
    recovery = result.get("recovery", {})
    if (
        recovery.get("model_response_unknown")
        or recovery.get("tool_result_unknown")
        or (recovery.get("import_result_unknown") and not args.verify_import)
        or result["task_run"] is None
    ):
        return result
    auth = read_json(Path(args.authorization)) if args.authorization else None
    if auth is not None:
        validate_authorization(auth, declaration["request"]["intent"]["sandbox_path"])
    if auth is None and not args.verify_import:
        raise ValueError("current_authorization_required")
    if declaration["mode"] == "real" and auth is not None:
        settings = DeepSeekSettings.from_runtime_env(Path(declaration["env_file"]))
    return supervise(
        directory,
        declaration,
        "resume",
        auth=auth,
        settings=settings,
        events=args.events,
        verify_import=args.verify_import,
    )


def main(argv=None):
    try:
        result = dispatch(parser().parse_args(argv))
    except KeyboardInterrupt:
        result = base_result()
        result["errors"].append({"code": "interrupted_before_run_started"})
    except (ValueError, OSError) as exc:
        result = base_result()
        # Do not echo argparse input, invalid values or raw OS errors.
        code = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc).replace("_", "").isalnum()
            and len(str(exc)) <= 80
            else "input_or_configuration_rejected"
        )
        result.update(exit_code=2, errors=[{"code": code}])
    except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
        result = base_result()
        result["errors"].append(safe_error(exc))
    sys.stdout.write(encode(result).decode("ascii") + "\n")
    sys.stdout.flush()
    return result["exit_code"]
