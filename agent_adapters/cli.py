"""Version-one synchronous CLI. The parent owns output and cancellation time."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import multiprocessing
import os
import signal
import sys
import time
import unicodedata
from pathlib import Path
from uuid import UUID, uuid4

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
from qinyuan_core import classify, configure, get_logger


def snapshot_result(*args, **kwargs):
    """Lazy legacy v1 snapshot hook kept patchable for compatibility tests."""
    from agent_adapters.application import snapshot_result as _snapshot_result

    return _snapshot_result(*args, **kwargs)


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
    evidence = commands.add_parser("evidence")
    evidence.add_argument("--task-run-id", required=True)
    directory = evidence.add_mutually_exclusive_group(required=True)
    directory.add_argument(
        "--state-dir", help="root of the legacy TaskRun directory layout"
    )
    directory.add_argument(
        "--run-dir", help="exact owned run directory, no run.json required"
    )
    evidence.add_argument(
        "--html", help="explicit static report path outside the selected run directory"
    )
    sensitive = commands.add_parser("evidence-sensitive")
    sensitive.add_argument("action", choices=("view", "delete"))
    sensitive.add_argument("--run-dir", required=True)
    sensitive.add_argument("--task-run-id", required=True)
    sensitive.add_argument("--evidence-id", required=True)
    candidate = commands.add_parser("candidate-read")
    candidate.add_argument("--task-run-id", required=True)
    candidate.add_argument("--candidate-id", required=True)
    candidate.add_argument("--evidence-version", required=True)
    directory = candidate.add_mutually_exclusive_group(required=True)
    directory.add_argument("--state-dir")
    directory.add_argument("--run-dir")
    for command in ("resume", "cancel", "cleanup", "status", "replay", "validation-check", "competition-submit", "competition-end", "operator-control", "human-decision"):
        item = commands.add_parser(command)
        if command == "status":
            selector = item.add_mutually_exclusive_group(required=True)
            selector.add_argument("--task-run-id")
            selector.add_argument("--request-id")
        else:
            item.add_argument("--task-run-id", required=True)
        directory = item.add_mutually_exclusive_group(required=True)
        directory.add_argument("--state-dir")
        directory.add_argument("--run-dir")
        if command == "resume":
            item.add_argument("--authorization")
            item.add_argument("--fixture", help="append-only v2 simulated fixture")
            group = item.add_mutually_exclusive_group()
            group.add_argument("--cleanup-only", action="store_true")
            group.add_argument("--verify-import", action="store_true")
            item.add_argument("--events")
        elif command in {"cancel", "cleanup"}:
            item.add_argument("--events")
        elif command == "operator-control":
            item.add_argument("--operation-id",required=True)
            item.add_argument("--action",required=True,choices=("pause","resume","extend_question","extend_overall"))
            item.add_argument("--seconds",type=int)
        elif command == "human-decision":
            item.add_argument("--candidate-id", required=True)
            item.add_argument("--operation-id", required=True)
            item.add_argument("--decision", required=True, choices=("accept", "continue", "end"))
            item.add_argument("--events")
        elif command in {"competition-submit","competition-end"}:
            item.add_argument("--binding-file",required=True)
            item.add_argument("--operation-id",required=True)
            item.add_argument("--events")
        elif command == 'validation-check':
            for field in ('candidate-id', 'evidence-version', 'submission-operation-id', 'query-id'):
                item.add_argument('--' + field, required=True)
            item.add_argument('--events')
        if command == "replay":
            item.add_argument("--after-sequence", type=int, default=0)
            item.add_argument("--limit", type=int, default=128)
    return result


def _persisted_protocol_version(declaration):
    if not isinstance(declaration, dict):
        raise ValueError("invalid_run_declaration")
    version = declaration.get("protocol_version")
    if type(version) is not int or version not in {1, 2, 3}:
        raise ValueError("unsupported_persisted_protocol_version")
    return version


def _snapshot(directory, declaration):
    protocol_version = _persisted_protocol_version(declaration)
    try:
        if protocol_version == 3:
            return _snapshot_v3(directory, declaration)
        if protocol_version == 2:
            from agent_adapters.task_result import snapshot_task_result

            return snapshot_task_result(directory, declaration)
        return snapshot_result(directory, declaration)
    except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
        from agent_adapters.application import base_result

        result = base_result(declaration["task_run_id"], declaration["mode"])
        result["errors"].append(
            {"code": "state_read_failed", "category": type(exc).__name__}
        )
        return result


def _snapshot_v3(directory: Path, declaration: dict) -> dict:
    """Read the v3 TaskRun result through its versioned result adapter."""
    from agent_adapters.web.task_result import snapshot_v3_task_result

    return snapshot_v3_task_result(directory, declaration)


def _candidate_read_run_directory(args) -> tuple[Path, dict]:
    """Validate the immutable v3 declaration and SQLite ownership before DPAPI."""
    if args.run_dir is not None:
        supplied = Path(args.run_dir)
        if supplied.is_symlink() or not supplied.is_dir():
            raise ValueError("d7_candidate_run_directory_invalid")
        directory = supplied.resolve()
    else:
        directory = run_path(Path(args.state_dir), args.task_run_id)
    declaration_path = directory / "run.json"
    if declaration_path.is_symlink() or not declaration_path.is_file():
        raise ValueError("d7_candidate_run_declaration_invalid")
    declaration = immutable_read(declaration_path)
    if (
        not isinstance(declaration, dict)
        or declaration.get("task_run_id") != args.task_run_id
        or declaration.get("protocol_version") != 3
    ):
        raise ValueError("d7_candidate_run_declaration_invalid")
    database = directory / "control.sqlite"
    if database.is_symlink() or not database.is_file():
        raise ValueError("d7_candidate_ownership_invalid")
    from agent_core.sqlite_control_store import SqliteControlStore

    try:
        with SqliteControlStore.open_readonly(database) as ledger:
            if (
                ledger.list_task_run_refs() != (args.task_run_id,)
                or ledger.load_task_run(args.task_run_id) is None
            ):
                raise ValueError("d7_candidate_ownership_invalid")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("d7_candidate_ownership_invalid") from exc
    return directory, declaration


def _existing_task_directory(args) -> Path:
    """Resolve a previously-created run without allowing selector mixing."""
    run_dir = getattr(args, "run_dir", None)
    state_dir = getattr(args, "state_dir", None)
    task_id = getattr(args, "task_run_id", None)
    if run_dir is not None:
        if state_dir is not None or not isinstance(task_id, str):
            raise ValueError("invalid_run_directory_selector")
        directory = Path(run_dir)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("invalid_task_directory")
        return directory.resolve()
    if state_dir is None or not isinstance(task_id, str):
        raise ValueError("invalid_run_directory_selector")
    return run_path(Path(state_dir), task_id)


def _request_status_error(request_id, code: str, exit_code: int) -> dict:
    return {
        "protocol_version": 3,
        "request_id": request_id,
        "errors": [{"code": code}],
        "exit_code": exit_code,
    }


def _safe_request_receipt(receipt) -> dict:
    return {
        "request_id": receipt.request_id,
        "state": receipt.state,
        "task_run_id": receipt.task_run_id,
        "reason": receipt.reason,
        "staging_status": receipt.staging_status,
        "conflict_count": receipt.conflict_count,
        "request_exit_code": receipt.request_exit_code,
        "attachments": [
            {
                "index": item.index,
                "name": item.name,
                "byte_size": item.byte_size,
                "sha256": item.sha256,
            }
            for item in receipt.attachments
        ],
    }


def _safe_overall_goal_summary(value) -> dict | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("outcome")
        not in {"accepted", "rejected", "pending", "unknown", "not_evaluated"}
    ):
        raise ValueError("run_result_unavailable")
    allowed = {
        "schema_version",
        "outcome",
        "candidate_count",
        "accepted_count",
        "rejected_count",
        "pending_count",
    }
    summary = {key: item for key, item in value.items() if key in allowed}
    for key in allowed - {"schema_version", "outcome"}:
        if key in summary and (type(summary[key]) is not int or summary[key] < 0):
            raise ValueError("run_result_unavailable")
    return summary


def _status_by_request_id(args) -> dict:
    request_id = args.request_id
    if getattr(args, "run_dir", None) is not None or getattr(args, "state_dir", None) is None:
        return _request_status_error(request_id, "request_status_requires_state_dir", 2)
    try:
        parsed = UUID(request_id) if isinstance(request_id, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != request_id:
        return _request_status_error(request_id, "request_id_invalid", 2)

    from agent_adapters.web.request_staging import (
        D7RequestStager,
        RequestStagingError,
    )

    try:
        stager = D7RequestStager.open_existing(Path(args.state_dir), clock=time.time)
        receipt = stager.read_receipt(request_id)
    except (KeyError, OSError, RequestStagingError, TypeError, ValueError):
        return _request_status_error(request_id, "request_receipt_unavailable", 5)
    if receipt is None:
        return _request_status_error(request_id, "request_receipt_not_found", 2)

    result = {
        "protocol_version": 3,
        "request_id": request_id,
        "request_receipt": _safe_request_receipt(receipt),
        "exit_code": 0,
    }
    task_run_id = receipt.task_run_id
    if task_run_id is None:
        return result

    try:
        directory = run_path(Path(args.state_dir), task_run_id)
        declaration_path = directory / "run.json"
        if (
            not directory.is_dir()
            or declaration_path.is_symlink()
            or not declaration_path.is_file()
        ):
            raise ValueError("run_result_unavailable")
        declaration = immutable_read(declaration_path)
        if (
            not isinstance(declaration, dict)
            or declaration.get("task_run_id") != task_run_id
            or declaration.get("request_id") != request_id
            or type(declaration.get("protocol_version")) is not int
            or declaration["protocol_version"] != 3
        ):
            raise ValueError("run_result_unavailable")
        run_result = _snapshot_v3(directory, declaration)
        if (
            not isinstance(run_result, dict)
            or run_result.get("task_run_id") != task_run_id
            or any(
                item.get("code") in {"state_read_failed", "run_result_unavailable"}
                for item in run_result.get("errors", [])
                if isinstance(item, dict)
            )
            or (
                "run_exit_code" in run_result
                and (
                    type(run_result["run_exit_code"]) is not int
                    or run_result["run_exit_code"] not in {0, 3, 4, 5, 130}
                )
            )
        ):
            raise ValueError("run_result_unavailable")
        task_run = run_result.get("task_run")
        run_status = task_run.get("status") if isinstance(task_run, dict) else None
        run_exit_code = run_result.get("run_exit_code")
        if run_exit_code is not None and run_status not in {
            "achieved",
            "assumed_achieved",
            "partial",
            "inconclusive",
            "failed",
            "cancelled",
            "deadline_exceeded",
        }:
            raise ValueError("run_result_unavailable")
        if run_status is not None and run_status not in {
            "running",
            "awaiting_confirmation",
            "awaiting_external_feedback",
            "achieved",
            "assumed_achieved",
            "partial",
            "inconclusive",
            "failed",
            "cancelled",
            "deadline_exceeded",
        }:
            raise ValueError("run_result_unavailable")
        goal_evaluation = run_result.get("goal_evaluation", "unknown")
        cleanup_status = run_result.get("cleanup_status", "unconfirmed")
        recovery_status = run_result.get("recovery_status", "required")
        if goal_evaluation not in {"accepted", "rejected", "unknown", "not_evaluated"}:
            raise ValueError("run_result_unavailable")
        if cleanup_status not in {"confirmed", "unconfirmed", "not_required", "not_confirmed"}:
            raise ValueError("run_result_unavailable")
        if recovery_status not in {"required", "not_required", "unknown"}:
            raise ValueError("run_result_unavailable")
        safe_run_result = {
            "task_run_id": task_run_id,
            "run_status": run_status,
            "goal_evaluation": goal_evaluation,
            "overall_goal_summary": _safe_overall_goal_summary(
                run_result.get("overall_goal_summary")
            ),
            "cleanup_status": cleanup_status,
            "recovery_status": recovery_status,
        }
        if run_exit_code is not None:
            safe_run_result["run_exit_code"] = run_exit_code
        result["run_result"] = safe_run_result
        return result
    except (KeyError, OSError, TypeError, ValueError):
        unavailable = _request_status_error(request_id, "run_result_unavailable", 5)
        unavailable["request_receipt"] = result["request_receipt"]
        return unavailable


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
    protocol_version = _persisted_protocol_version(declaration)
    if protocol_version == 3:
        raise ValueError("protocol_v3_lifecycle_unavailable")
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
            if protocol_version == 2:
                _owned_task_operation(
                    directory,
                    declaration,
                    cancel,
                    operation,
                    settings,
                    output_name,
                )
            else:
                _owned_operation(
                    directory,
                    declaration,
                    cancel,
                    operation,
                    auth,
                    settings,
                    output_name,
                    verify_import,
                )
    except ValueError as exc:
        if str(exc) != "task_run_busy":
            raise
        result = _snapshot(directory, declaration)
        result["errors"].append({"code": "task_run_busy"})
        result["exit_code"] = 4
        immutable_write(directory / output_name, result)


def _owned_task_operation(
    directory, declaration, cancel, operation, settings, output_name
):
    from agent_adapters.application import Cancellation, safe_error
    from agent_adapters.task_application import TaskApplication

    app = None
    errors = []
    try:
        token = Cancellation(cancel, directory)
        app = TaskApplication(directory, declaration, token, settings=settings)
        if operation == "cancel":
            app.controller.attach_for_cancellation(declaration["task_run_id"])
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
        if (
            operation == "start"
            and app is not None
            and app.store.load_task_run(declaration["task_run_id"]) is None
        ):
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
    from agent_adapters.application import AgentApplication, Cancellation, safe_error
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
    from agent_adapters.application import safe_error

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
                Path(events),
                directory / "control.sqlite",
                declaration["task_run_id"],
                append_existing=operation != "start",
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
    if args.command == "status" and getattr(args, "request_id", None) is not None:
        return _status_by_request_id(args)
    if args.command == "candidate-read":
        if not (
            sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
        ):
            raise ValueError("d7_candidate_view_requires_terminal")
        try:
            if codecs.lookup(sys.stderr.encoding or "").name != "utf-8":
                raise ValueError("d7_candidate_view_terminal_encoding_unsupported")
        except LookupError as exc:
            raise ValueError("d7_candidate_view_terminal_encoding_unsupported") from exc
        directory, _declaration = _candidate_read_run_directory(args)
        from agent_adapters.web.candidate_evidence import D7CandidateEvidenceService
        from agent_adapters.web.candidate_operator_view import (
            D7CandidateOperatorViewer,
            OPERATOR_CANDIDATE_VIEW_ADAPTER_ID,
        )

        service = D7CandidateEvidenceService(
            directory,
            args.task_run_id,
            trusted_adapter_ids=(OPERATOR_CANDIDATE_VIEW_ADAPTER_ID,),
            create=False,
        )
        viewed = D7CandidateOperatorViewer(service).view(
            args.candidate_id,
            evidence_version=args.evidence_version,
            stdin=sys.stdin,
            stdout=sys.stderr,
        )
        return viewed.to_dict() | {"exit_code": 0}
    if args.command == "evidence-sensitive":
        if args.action == "view" and not (
            sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
        ):
            raise ValueError("d6_sensitive_view_requires_terminal")
        from agent_adapters.protected_evidence import ProtectedEvidenceError
        from agent_adapters.sensitive_evidence_service import (
            TrustedSensitiveEvidenceService,
        )

        if args.action == "view":
            try:
                terminal_encoding = codecs.lookup(sys.stderr.encoding or "").name
            except LookupError as exc:
                raise ProtectedEvidenceError("d6_evidence_terminal_encoding_unsupported") from exc
            if terminal_encoding != "utf-8":
                raise ProtectedEvidenceError("d6_evidence_terminal_encoding_unsupported")

        service = TrustedSensitiveEvidenceService(
            Path(args.run_dir), args.task_run_id, create=False
        )
        if args.action == "view":
            prepared_display: list[bytes] = []

            def prepare_display(body: bytes) -> None:
                try:
                    text = body.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise ProtectedEvidenceError("d6_evidence_body_not_text") from exc
                # Escape terminal controls before any byte reaches the screen.
                display = "".join(
                    f"\\u{ord(char):04x}"
                    if unicodedata.category(char) in {"Cc", "Cf", "Cs"}
                    else char
                    for char in text
                )
                try:
                    prepared_display.append((display + "\n").encode("utf-8", errors="strict"))
                except UnicodeEncodeError as exc:
                    raise ProtectedEvidenceError("d6_evidence_display_failed") from exc

            service.view(args.evidence_id, validate_body=prepare_display)
            payload = prepared_display[0]
            written = sys.stderr.buffer.write(payload)
            if written != len(payload):
                raise ProtectedEvidenceError("d6_evidence_display_failed")
            sys.stderr.flush()
            return {"task_run_id": args.task_run_id, "evidence_id": args.evidence_id, "status": "viewed", "exit_code": 0}
        result = service.delete(args.evidence_id)
        return {"task_run_id": args.task_run_id, "evidence_id": args.evidence_id, "status": result["status"], "exit_code": 0}
    if args.command == "evidence":
        from agent_adapters.evidence_query import (
            export_static_html,
            query_d5_evidence,
            sensitive_config_from_json,
        )

        result = query_d5_evidence(
            Path(args.state_dir) if args.state_dir is not None else None,
            args.task_run_id,
            sensitive_config=sensitive_config_from_json(
                os.environ.get("QINYUAN_AGENT_D5_SENSITIVE_CONFIG")
            ),
            run_dir=Path(args.run_dir) if args.run_dir is not None else None,
        )
        if args.html:
            export_static_html(
                result, Path(args.state_dir or args.run_dir), Path(args.html)
            )
        return result
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
    directory = _existing_task_directory(args)
    declaration = immutable_read(directory / "run.json")
    protocol_version = _persisted_protocol_version(declaration)
    if declaration.get("task_run_id") != args.task_run_id:
        raise ValueError("run_identity_mismatch")
    if args.command in {'validation-check','competition-submit','competition-end','operator-control','human-decision'} and protocol_version != 3:
        raise ValueError('d9_validation_check_profile_unsupported')
    if protocol_version == 2:
        from agent_adapters.task_cli import dispatch_existing

        return dispatch_existing(args, directory, declaration)
    if protocol_version == 3:
        from agent_adapters.task_cli import dispatch_v3_existing

        return dispatch_v3_existing(args, directory, declaration)
    if args.command == "cleanup":
        raise ValueError("cleanup_not_supported_for_protocol_version")
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
    # CLI 的 stdout 是 JSON、stderr 必须为空。日志默认完全静默，
    # 要排查问题再显式打开：QY_LOG_JSONL=<path> 或 QY_LOG_CONSOLE=1。
    configure(
        level=os.environ.get("QY_LOG_LEVEL", "WARNING"),
        console=bool(os.environ.get("QY_LOG_CONSOLE")),
        jsonl=os.environ.get("QY_LOG_JSONL"),
    )
    from contextlib import ExitStack
    with ExitStack() as ownership:
        try:
            args=parser().parse_args(argv)
            root=None
            if args.command=="run-task":root=Path(args.state_dir).resolve().parent
            elif args.command in {"resume","cancel","cleanup","competition-submit","competition-end"}:
                root=_existing_task_directory(args).parent.parent
            if root is not None:
                binding=root/"competition-binding.json"
                if binding.is_file() and "schedule" in immutable_read(binding):
                    ownership.enter_context(run_lock(root,lock_name="worker.lock"))
            result = dispatch(args)
            if result is None:
                return 0
        except KeyboardInterrupt:
            from agent_adapters.application import base_result

            result = base_result()
            result["errors"].append({"code": "interrupted_before_run_started"})
        except Exception as exc:  # noqa: BLE001 -- sanitized CLI/process boundary
            category, code, retryable = classify(exc)
            get_logger("ctrl").error(
                "cli.unhandled",
                code=code,
                category=str(category),
                retryable=retryable,
                exc=type(exc).__name__,
            )
            from agent_adapters.application import base_result, safe_error

            from agent_adapters.protected_evidence import ProtectedEvidenceError
            from agent_adapters.web.candidate_evidence import D7CandidateEvidenceError
            from agent_core.stage_storage import StageStateError
            from agent_adapters.web.candidate_operator_view import (
                D7CandidateOperatorViewError,
            )

            if isinstance(exc, ProtectedEvidenceError):
                result = base_result()
                result.update(exit_code=2, errors=[{"code": exc.code}])
            elif isinstance(exc, (D7CandidateEvidenceError, D7CandidateOperatorViewError)):
                result = base_result()
                result.update(exit_code=2, errors=[{"code": exc.code}])
            elif isinstance(exc, StageStateError):
                from qinyuan_core import Category
                result = base_result()
                result.update(exit_code=2 if category in {Category.INPUT, Category.POLICY, Category.CONFLICT} else 5,
                    errors=[dict(code=code, category=str(category), phase="stage_control")])
            elif isinstance(exc, (ValueError, OSError)):
                result = base_result()
                code = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and str(exc).replace("_", "").isalnum()
                    and len(str(exc)) <= 80
                    else "input_or_configuration_rejected"
                )
                result.update(exit_code=2, errors=[{"code": code}])
            else:
                result = base_result()
                result["errors"].append(safe_error(exc))
        sys.stdout.write(encode(result).decode("ascii") + "\n")
        sys.stdout.flush()
        return result["exit_code"]
