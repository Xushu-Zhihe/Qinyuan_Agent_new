"""Version-two CLI entry while T5 run-intent remains unchanged."""

from __future__ import annotations

import sys
import time
from dataclasses import asdict
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
from agent_adapters.task_contract import TaskRunRequest, validate_task_fixture
from agent_adapters.task_result import snapshot_task_result
from agent_core.runtime import TERMINAL_STATUSES


def _dispatch_v3_run_task(args, raw):
    """Stage and execute the temporary v3 simulation envelope.

    The v3 wire envelope is deliberately handled before the legacy TaskRun
    parser.  This keeps v1/v2 behavior byte-for-byte compatible while making
    request staging and capability rejection observable even when the full
    D3 simulator is unavailable.
    """
    from agent_adapters.web.request_contract import V3SimulationRequest
    from agent_adapters.web.request_staging import D7RequestStager

    if "execution_profile" in raw:
        from agent_adapters.simulation import parse_request
        request, _ = parse_request(raw)
    else:
        request = V3SimulationRequest.parse(raw)
    d8_real = raw.get("execution_profile") in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"} and raw.get("mode") == "real"
    env_file = None
    if d8_real:
        from agent_adapters.deepseek_roles import settings_from_file
        if getattr(args, "fixture", None):
            raise ValueError("fixture_forbidden_in_real_mode")
    state_dir = Path(args.state_dir).resolve()
    stager = D7RequestStager(state_dir, clock=time.time)
    stage = stager.stage(
        request_id=request.request_id,
        source_root=request.source_root,
        attachment_paths=request.attachments,
        request_identity={
            "protocol_version": 3,
            "task_request": request.task_request,
            "authorization": request.authorization,
            "outer_role": request.outer_role.role_config_ref,
            "inner_role": request.inner_role.role_config_ref,
            "d4_budget": request.d4_budget.budget_policy_ref,
            **({"execution_profile": raw["execution_profile"], "mode": raw["mode"],
                "d8_config": raw["d8_config"]} if "execution_profile" in raw else {}),
        },
    )
    receipt = stage.receipt
    if stage.conflict:
        raise ValueError("request_id_idempotency_conflict")
    if receipt.state == "input_staging_error":
        return {
            "protocol_version": 3,
            "request_id": request.request_id,
            "request_receipt": _safe_request_receipt(receipt),
            "errors": [{"code": receipt.reason or "attachment_staging_failed"}],
            "exit_code": 2,
            "task_run_id": None,
        }

    if receipt.state == "task_run_created":
        # A repeated request ID is an idempotent lookup. A prior crash after
        # receipt commit is reported as unknown by the projection; it is never
        # treated as permission to create a second TaskRun.
        task_id = receipt.task_run_id
        if task_id is None:
            raise ValueError("request_task_run_binding_missing")
        directory = run_path(state_dir, task_id)
        declaration_path = directory / "run.json"
        if declaration_path.is_symlink() or not declaration_path.is_file():
            return {
                "protocol_version": 3,
                "request_id": request.request_id,
                "request_receipt": _safe_request_receipt(receipt),
                "task_run_id": task_id,
                "errors": [{"code": "run_result_unavailable"}],
                "exit_code": 5,
            }
        declaration = immutable_read(declaration_path)
        if (
            not isinstance(declaration, dict)
            or declaration.get("protocol_version") != 3
            or declaration.get("task_run_id") != task_id
            or declaration.get("request_id") != request.request_id
        ):
            raise ValueError("request_task_run_binding_mismatch")
        from agent_adapters.web.task_result import snapshot_v3_task_result

        result = snapshot_v3_task_result(directory, declaration)
        result["request_id"] = request.request_id
        result["request_receipt"] = _safe_request_receipt(receipt)
        result["exit_code"] = _v3_run_operation_exit(result)
        return result

    # Current D3 simulator can bind at most two attachments and each staged
    # item is capped at 16 MiB.  Preserve the complete request receipt, then
    # remove staged payload bytes on a capability rejection.
    unsupported = len(request.attachments) not in (0, 1, 2) or any(
        item.byte_size > 16 * 1024 * 1024 for item in receipt.attachments
    )
    if unsupported:
        receipt = stager.record_outcome(
            request.request_id,
            state="capability_not_supported",
            reason="v3_attachment_capability_not_supported",
        )
        staging_status = stager.remove_rejected_payload(request.request_id)
        if staging_status != "removed":
            receipt = stager.record_outcome(
                request.request_id,
                state="capability_not_supported",
                staging_status="cleanup_pending",
            )
        return {
            "protocol_version": 3,
            "request_id": request.request_id,
            "request_receipt": _safe_request_receipt(receipt),
            "errors": [{"code": "capability_not_supported"}],
            "exit_code": 2,
            "task_run_id": None,
        }

    if d8_real:
        env_file = Path(args.env_file).resolve()
        settings_from_file(request.outer_role, env_file)
        settings_from_file(request.inner_role, env_file)
    task_id = str(uuid4())
    directory = run_path(state_dir, task_id)
    directory.mkdir(parents=True, exist_ok=False)
    staged_payload = stager.move_to_task_run(request.request_id, directory)
    staged_paths = []
    if staged_payload is not None:
        staged_paths = [
            (staged_payload / f"{index:02d}.input").relative_to(directory).as_posix()
            for index in range(len(receipt.attachments))
        ]
    declaration = {
        "protocol_version": 3,
        "task_run_id": task_id,
        "request_id": request.request_id,
        "mode": "real" if d8_real else "simulated",
        "request": raw,
        "source_root": str(directory.resolve()),
        "staged_attachment_paths": staged_paths,
        "attachments": [asdict(item) for item in receipt.attachments],
        "capabilities": ["attachment_digest_only"],
        "role_config_refs": {
            "outer": request.outer_role.role_config_ref,
            "inner": request.inner_role.role_config_ref,
        },
        "d4_budget": asdict(request.d4_budget),
        "d4_budget_policy": asdict(request.d4_budget),
        "ids": {"start": str(uuid4()), "cancel": str(uuid4())},
    }
    if "execution_profile" in raw:
        declaration.update(execution_profile=raw["execution_profile"],
                           contract_version=int(raw["execution_profile"].removeprefix("d8_v")))
        declaration["capabilities"] = ["attachment_digest_only", "simulation"]
    if d8_real:
        from agent_adapters.simulation import real_declaration_binding
        backend = raw["d8_config"]["sandbox_backend"]
        declaration.update(env_file=str(env_file), sandbox_backend=backend)
        declaration["capabilities"] = ["attachment_digest_only", "d8_real_models",
                                       "docker_task_run_sandbox" if backend == "docker" else "local_simulated_sandbox"]
        endpoints = raw['task_request']['target'].get('endpoints', [])
        if endpoints and raw.get('execution_profile') == 'd8_v22':
            protocol = endpoints[0]['url'].split(':', 1)[0]
            declaration['capabilities'] += ['task_run_target_network', 'mcp', 'tcp' if protocol == 'tcp' else 'http']
        if raw.get('execution_profile') == 'd8_v20' and endpoints:
            declaration['capabilities'] = ['d8_http', 'd8_real_models', 'docker_task_run_sandbox',
                                           'task_run_origin_bound_target_network']
        immutable_write(directory / "d8-runtime.json", real_declaration_binding(declaration))
    immutable_write(directory / "run.json", declaration)
    receipt = stager.record_outcome(
        request.request_id,
        state="task_run_created",
        task_run_id=task_id,
    )

    def _failure_phase():
        from agent_core.sqlite_control_store import SqliteControlStore

        started = None
        try:
            database = directory / "control.sqlite"
            if database.is_file():
                with SqliteControlStore.open_readonly(database) as store:
                    started = store.load_task_run(task_id) is not None
            else:
                started = False
        except Exception:
            pass  # Unknown state must not be reported as a confirmed startup failure.
        return "execution" if started is True else "start" if started is False else "dispatch"

    def _record_failure(exc):
        from agent_adapters.output_evidence import safe_failure_details
        from agent_adapters.web.task_result import snapshot_v3_task_result

        phase = _failure_phase()
        prefix = ("d8_real" if d8_real else "simulation" if "execution_profile" in raw
                  else "d7_simulation")
        error_code = f"{prefix}_{phase}_failed"
        failure = {"code": error_code, "phase": phase, **safe_failure_details(exc)}
        immutable_write(
            directory / ("dispatch-stop-" + str(uuid4()) + ".json"),
            {"stop": "recovery_required", "reason": error_code, "diagnostic": failure},
        )
        snapshot = snapshot_v3_task_result(directory, declaration)
        snapshot.setdefault("errors", []).append(failure)
        if not snapshot.get("recovery", {}).get("required"):
            snapshot["recovery"] = {"required": True, "reason": error_code}
        snapshot["recovery_status"] = "required"
        return snapshot

    try:
        result = _run_v3_simulated_operation(
            directory, declaration, operation="start", events=args.events
        )
    except Exception as exc:  # noqa: BLE001 -- sanitized persisted run boundary
        # 启动阶段失败时控制数据库还没建，没有任何已记录状态会被重放，因此
        # 重开一次是安全的：冷启动的容器引擎握手超时变成"多等一次"，而不是
        # 丢掉整轮。数据库一旦落盘就不再重试，避免掩盖真实缺陷。
        if (directory / "control.sqlite").is_file():
            result = _record_failure(exc)
        else:
            try:
                result = _run_v3_simulated_operation(
                    directory, declaration, operation="start", events=args.events
                )
            except Exception as retry_exc:  # noqa: BLE001
                result = _record_failure(retry_exc)
    result["request_id"] = request.request_id
    result["request_receipt"] = _safe_request_receipt(receipt)
    result["exit_code"] = _v3_run_operation_exit(result)
    return result


def _v3_run_operation_exit(result: dict) -> int:
    if result.get("recovery", {}).get("required"):
        return 5
    if result.get("event_export", {}).get("error"):
        return 3
    return result.get("run_exit_code", 4)


def _has_unresolved_external_validation(result: dict) -> bool:
    candidates = result.get("answer_candidates", [])
    checks = result.get("external_validation_checks", [])
    if not isinstance(candidates, list) or not isinstance(checks, list):
        return True

    unresolved_unknowns: set[tuple[str, str, str]] = set()
    candidate_states: dict[tuple[str, str], str] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return True
        candidate_id = candidate.get("candidate_id")
        evidence_version = candidate.get("evidence_version")
        state = candidate.get("validation_state")
        if not all(isinstance(value, str) for value in (candidate_id, evidence_version, state)):
            return True
        if state not in {"pending", "accepted", "rejected", "unknown", "conflict"}:
            return True
        key = (candidate_id, evidence_version)
        candidate_states[key] = state
        if state in {"unknown", "conflict"}:
            return True
        feedback = candidate.get("feedback", [])
        if not isinstance(feedback, list):
            return True
        unknown_ids = {
            item.get("operation_id")
            for item in feedback
            if isinstance(item, dict)
            and item.get("verdict") == "unknown"
            and item.get("feedback_kind") != "reconciliation"
            and isinstance(item.get("operation_id"), str)
        }
        resolved_ids = {
            item.get("resolves_operation_id")
            for item in feedback
            if isinstance(item, dict)
            and item.get("feedback_kind") == "reconciliation"
            and isinstance(item.get("resolves_operation_id"), str)
        }
        unresolved_unknowns.update(
            (candidate_id, evidence_version, operation_id)
            for operation_id in unknown_ids - resolved_ids
        )

    feedback_records = result.get("external_validation_feedback", [])
    if not isinstance(feedback_records, list):
        return True
    for feedback in feedback_records:
        if not isinstance(feedback, dict):
            return True
        if feedback.get("verdict") != "unknown":
            continue
        candidate_id = feedback.get("candidate_id")
        evidence_version = feedback.get("evidence_version")
        operation_id = feedback.get("operation_id")
        if not all(
            isinstance(value, str)
            for value in (candidate_id, evidence_version, operation_id)
        ):
            return True
        key = (candidate_id, evidence_version)
        if candidate_states.get(key) not in {"accepted", "rejected", "partial"} or (
            candidate_id, evidence_version, operation_id
        ) in unresolved_unknowns:
            return True

    task = result.get("task_run")
    awaiting_feedback = isinstance(task, dict) and task.get("status") == "awaiting_external_feedback"
    if awaiting_feedback and any(state == "pending" for state in candidate_states.values()):
        return True

    for check in checks:
        if not isinstance(check, dict):
            return True
        if check.get("status") == "started":
            return True
        if check.get("verdict") == "unknown":
            candidate_id = check.get("candidate_id")
            evidence_version = check.get("evidence_version")
            submission_operation_id = check.get("submission_operation_id")
            if not all(
                isinstance(value, str)
                for value in (candidate_id, evidence_version, submission_operation_id)
            ):
                return True
            key = (candidate_id, evidence_version)
            if candidate_states.get(key) not in {"accepted", "rejected", "partial"} or (
                candidate_id, evidence_version, submission_operation_id
            ) in unresolved_unknowns:
                return True
    return bool(unresolved_unknowns)


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
            {"index": item.index, "name": item.name, "byte_size": item.byte_size,
             "sha256": item.sha256}
            for item in receipt.attachments
        ],
    }


def _finish_event_export(exporter) -> tuple[int, str | None]:
    exporter.finish.set()
    end = time.monotonic() + 2.1
    while not exporter.error and not exporter.done and time.monotonic() < end:
        exporter.tick()
        time.sleep(0.01)
    if not exporter.done and not exporter.error:
        exporter.error = "event_export_timeout"
    sequence, error = exporter.sequence, exporter.error
    exporter.close()
    return sequence, error


def _run_v3_simulated_operation(
    directory: Path, declaration: dict, *, operation: str, events: str | None, competition_request=None
) -> dict:
    from agent_adapters.web.simulated_application import run_simulated_task
    extra={}
    if competition_request is not None:
        from agent_adapters.simulation import run_simulated_task
        extra["competition_request"]=competition_request

    if not events:
        return run_simulated_task(directory, declaration, operation=operation, **extra)

    from agent_adapters.event_export import EventExporter

    exporter = EventExporter(
        Path(events),
        directory / "control.sqlite",
        declaration["task_run_id"],
        append_existing=operation != "start",
    )
    exporter.start_monitoring()
    finished: dict = {}

    def finish_once():
        # The exporter keeps the control database open; the projection must not run
        # while that is true, and finishing twice would tick a closed exporter.
        if "value" not in finished:
            finished["value"] = _finish_event_export(exporter)
        return finished["value"]

    try:
        result = run_simulated_task(directory, declaration, operation=operation,
                                    finalize_export=finish_once, **extra)
    except BaseException:
        finish_once()
        raise
    sequence, error = finish_once()
    result["event_export"] = {"last_sequence": sequence, "error": error}
    if error:
        result.setdefault("errors", []).append({"code": error})
        result["exit_code"] = (
            5 if result.get("recovery", {}).get("required") else 3
        )
    return result


def _d7_web_settings(declaration: dict):
    """Load v3 Web credentials from its frozen local env-file reference."""
    from agent_adapters.web.model_adapter import (
        _settings_from_env_file,
        d7_model_transport_config_ref,
    )
    from agent_adapters.web.web_request import D7WebRequest
    from agent_core.web_contracts import D7WebRunConfig

    request = D7WebRequest.parse(declaration["request"])
    env_file = Path(declaration["env_file"])
    if env_file.is_symlink() or not env_file.is_file():
        raise ValueError("d7_model_configuration_unavailable")
    transport_ref = d7_model_transport_config_ref(env_file)
    expected = request.run_config(declaration["task_run_id"], transport_ref)
    persisted = D7WebRunConfig.from_dict(declaration["d7_web_config"])
    if persisted != expected:
        raise ValueError("d7_web_configuration_changed")
    return _settings_from_env_file(request.outer_role, env_file)


def _run_v3_web_operation(
    directory: Path,
    declaration: dict,
    *,
    operation: str,
    events: str | None,
) -> dict:
    from agent_adapters.web.task_result import snapshot_v3_task_result
    from agent_adapters.web.web_application import run_d7_web_task
    from agent_adapters.event_export import EventExporter

    settings = (
        _d7_web_settings(declaration)
        if operation in {"start", "resume"}
        else None
    )
    exporter = None
    if events:
        if operation == "start":
            from agent_core.sqlite_control_store import SqliteControlStore

            with SqliteControlStore.open(directory / "control.sqlite"):
                pass
        exporter = EventExporter(
            Path(events),
            directory / "control.sqlite",
            declaration["task_run_id"],
            append_existing=operation != "start",
        )
        exporter.start_monitoring()
    try:
        result = run_d7_web_task(
            directory, declaration, operation=operation, settings=settings
        )
    except Exception as exc:  # noqa: BLE001 -- sanitized persisted run boundary
        try:
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)[-8:]
            immutable_write(
                directory / ("dispatch-stop-" + str(uuid4()) + ".json"),
                {
                    "stop": "recovery_required",
                    "reason": type(exc).__name__,
                    "frames": [
                        {
                            "file": Path(frame.filename).name,
                            "function": frame.name,
                            "line": frame.lineno,
                        }
                        for frame in frames
                    ],
                },
            )
        except (OSError, ValueError):
            pass
        result = snapshot_v3_task_result(directory, declaration)
        result.setdefault("errors", []).append({"code": "d7_web_operation_failed"})
    if exporter is not None:
        sequence, error = _finish_event_export(exporter)
        result["event_export"] = {"last_sequence": sequence, "error": error}
        if error:
            result.setdefault("errors", []).append({"code": error})
    result["exit_code"] = (
        5
        if result.get("recovery", {}).get("required")
        else 3
        if result.get("event_export", {}).get("error")
        else result.get("run_exit_code", 4)
    )
    return result


def _dispatch_v3_real_run_task(args, raw: dict) -> dict:
    from agent_adapters.web.model_adapter import d7_model_transport_config_ref
    from agent_adapters.web.request_staging import D7RequestStager
    from agent_adapters.web.web_request import D7WebRequest
    from agent_adapters.web.task_result import snapshot_v3_task_result

    if args.fixture:
        raise ValueError("fixture_forbidden_in_real_mode")
    request = D7WebRequest.parse(raw)
    env_file = Path(args.env_file).resolve()
    if env_file.is_symlink() or not env_file.is_file():
        raise ValueError("d7_model_configuration_unavailable")
    transport_ref = d7_model_transport_config_ref(env_file)
    # Validate credentials and provider settings before creating durable run state.
    from agent_adapters.web.model_adapter import _settings_from_env_file

    _settings_from_env_file(request.outer_role, env_file)
    _settings_from_env_file(request.inner_role, env_file)

    state_dir = Path(args.state_dir).resolve()
    stager = D7RequestStager(state_dir, clock=time.time)
    stage = stager.stage(
        request_id=request.request_id,
        source_root=request.source_root,
        attachment_paths=(),
        request_identity=raw,
    )
    receipt = stage.receipt
    if stage.conflict:
        raise ValueError("request_id_idempotency_conflict")
    if receipt.state == "task_run_created":
        if receipt.task_run_id is None:
            raise ValueError("request_task_run_binding_missing")
        existing_directory = run_path(state_dir, receipt.task_run_id)
        declaration_path = existing_directory / "run.json"
        if declaration_path.is_symlink() or not declaration_path.is_file():
            return {
                "protocol_version": 3,
                "request_id": request.request_id,
                "request_receipt": _safe_request_receipt(receipt),
                "task_run_id": receipt.task_run_id,
                "errors": [{"code": "run_result_unavailable"}],
                "exit_code": 5,
            }
        declaration = immutable_read(declaration_path)
        if (
            not isinstance(declaration, dict)
            or declaration.get("task_run_id") != receipt.task_run_id
            or declaration.get("request_id") != request.request_id
        ):
            raise ValueError("request_task_run_binding_mismatch")
        result = snapshot_v3_task_result(existing_directory, declaration)
        result["request_id"] = request.request_id
        result["request_receipt"] = _safe_request_receipt(receipt)
        result["exit_code"] = _v3_run_operation_exit(result)
        return result
    if receipt.state == "input_staging_error":
        return {
            "protocol_version": 3,
            "request_id": request.request_id,
            "request_receipt": _safe_request_receipt(receipt),
            "errors": [{"code": receipt.reason or "request_staging_failed"}],
            "exit_code": 2,
        }

    task_id = str(uuid4())
    directory = run_path(state_dir, task_id)
    directory.mkdir(parents=True, exist_ok=False)
    run_config = request.run_config(task_id, transport_ref)
    declaration = {
        "protocol_version": 3,
        "task_run_id": task_id,
        "request_id": request.request_id,
        "mode": "real",
        "request": raw,
        "source_root": request.source_root,
        "env_file": str(env_file),
        "d7_web_config": run_config.to_dict(),
        "role_config_refs": {
            "outer": request.outer_role.role_config_ref,
            "inner": request.inner_role.role_config_ref,
        },
        "d4_budget": asdict(request.d4_budget),
        "capabilities": ["d7_http_request"],
        "ids": {"start": str(uuid4()), "cancel": str(uuid4())},
    }
    immutable_write(directory / "run.json", declaration)
    receipt = stager.record_outcome(
        request.request_id, state="task_run_created", task_run_id=task_id
    )
    result = _run_v3_web_operation(
        directory, declaration, operation="start", events=args.events
    )
    result["request_id"] = request.request_id
    result["request_receipt"] = _safe_request_receipt(receipt)
    return result


def _input(path: str):
    return (
        parse_json(sys.stdin.buffer.read(1024 * 1024 + 1))
        if path == "-" else read_json(Path(path))
    )


def dispatch_run_task(args):
    raw = _input(args.request)
    if isinstance(raw, dict):
        version = raw.get("protocol_version")
        if type(version) is int and version == 3:
            # Keep the historical error for the old one-field probe used by
            # callers to detect whether a v3 assembler is installed.  A
            # complete envelope follows the real v3 validation path below.
            if set(raw) == {"protocol_version"}:
                raise ValueError("protocol_v3_not_supported")
            if "execution_profile" in raw:
                from agent_adapters.simulation import parse_request
                parse_request(raw)
                if raw["mode"] != args.mode:
                    raise ValueError("d8_mode_mismatch")
                return _dispatch_v3_run_task(args, raw)
            if args.mode == "real":
                return _dispatch_v3_real_run_task(args, raw)
            return _dispatch_v3_run_task(args, raw)
        if type(version) is not int or version != 2:
            raise ValueError("unsupported_protocol_version")
    from agent_adapters.cli import supervise
    request = TaskRunRequest.parse(raw)
    from agent_adapters.task_application import prepare_attachments
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


def dispatch_v3_existing(args, directory: Path, declaration: dict):
    """Versioned read/lifecycle boundary for v3 runs.

    v3 state is always projected by d7_task_result.  Mutating operations are
    intentionally conservative until a committed D3 worker is attached: a
    read of an already terminal run is idempotent; an in-flight mutation is
    reported as requiring recovery rather than redispatched blindly.
    """
    from agent_adapters.web.task_result import snapshot_v3_task_result
    from agent_adapters.event_export import read_events

    task_id = declaration["task_run_id"]
    if getattr(args, "task_run_id", task_id) != task_id:
        raise ValueError("task_run_ownership_mismatch")

    def operation_result(result: dict, *, exit_code: int | None = None) -> dict:
        """Keep the command result code independent from persisted run_exit_code."""
        if exit_code is None:
            errors = result.get("errors", ())
            unreadable = any(
                isinstance(item, dict)
                and item.get("code") in {
                    "state_read_failed",
                    "task_run_state_unavailable",
                    "checkpoint_ownership_invalid",
                    "run_result_unavailable",
                }
                for item in errors
            )
            if args.command in {"status", "replay"}:
                exit_code = 5 if unreadable else 0
            else:
                exit_code = (
                    5
                    if result.get("recovery", {}).get("required") or unreadable
                    else 3
                    if result.get("event_export", {}).get("error")
                    else 0
                )
        result["exit_code"] = exit_code
        return result

    if args.command == "replay":
        try:
            events = read_events(directory / "control.sqlite", task_id,
                                 args.after_sequence, args.limit)
        except (OSError, ValueError):
            return {
                "protocol_version": 3,
                "task_run_id": task_id,
                "events": [],
                "next_sequence": args.after_sequence,
                "errors": [{"code": "replay_unavailable"}],
                "exit_code": 5,
            }
        return {"protocol_version": 3, "task_run_id": task_id,
                "events": events,
                "next_sequence": events[-1]["sequence"] if events else args.after_sequence,
                "exit_code": 0}
    if args.command == "operator-control":
        from agent_core.sqlite_control_store import SqliteControlStore
        from agent_core.controller import TaskRunController
        from agent_core.control_store import StorageError
        import sqlite3
        try:
            with SqliteControlStore.open(directory/"control.sqlite") as store:
                payload={} if args.seconds is None else {"seconds":args.seconds}
                control=TaskRunController.submit_operator_control(store,task_id,args.operation_id,args.action,payload)
            return {"protocol_version":3,"task_run_id":task_id,"operator_control":control,"exit_code":0}
        except ValueError as error:
            return {"protocol_version":3,"task_run_id":task_id,"errors":[{"code":str(error)}],"exit_code":2}
        except (StorageError,sqlite3.Error,OSError):
            return {"protocol_version":3,"task_run_id":task_id,"errors":[{"code":"operator_command_outcome_unknown"}],"exit_code":5}
    result = snapshot_v3_task_result(directory, declaration)
    if args.command in {'competition-submit','competition-end'}:
        try:
            payload=parse_json(sys.stdin.buffer.read(16385),16384)
            if not isinstance(payload,dict) or set(payload)!={"credential"} or not isinstance(payload["credential"],str):
                raise ValueError("competition_credentials_invalid")
            from agent_adapters.competition_submission import frozen_binding
            binding=frozen_binding(args.binding_file,declaration)
        except (ValueError, OSError):
            result.setdefault("errors",[]).append({"code":"competition_submission_input_invalid"})
            return operation_result(result,exit_code=2)
        try:
            return operation_result(_run_v3_simulated_operation(directory,declaration,operation=args.command,
                events=getattr(args,"events",None),competition_request={"binding":binding,
                "credential":payload["credential"],"operation_id":args.operation_id}))
        except Exception as error:
            from agent_adapters.output_evidence import safe_failure_details
            from qinyuan_core import get_logger
            trace=error.__traceback__
            while trace is not None and trace.tb_next is not None:trace=trace.tb_next
            get_logger("ctrl").error("competition.submission_failed",run=task_id,
                category="recovery",code="competition_end_recovery_required" if args.command=="competition-end" else "competition_submission_recovery_required",
                location={"module":trace.tb_frame.f_globals.get("__name__","unknown"),"line":trace.tb_lineno} if trace else None,
                **safe_failure_details(error,include_chain=True))
            result=snapshot_v3_task_result(directory,declaration)
            failure_code="competition_end_recovery_required" if args.command=="competition-end" else "competition_submission_recovery_required"
            result.setdefault("errors",[]).append({"code":failure_code})
            result["recovery"]={"required":True,"reason":failure_code}
            return operation_result(result,exit_code=5)
    if args.command == 'validation-check':
        from agent_adapters.validation_check import dispatch_check
        selection = {key: getattr(args, key) for key in
                     ('candidate_id', 'evidence_version', 'submission_operation_id', 'query_id')}
        try:
            return dispatch_check(directory, declaration, selection, events=getattr(args, 'events', None))
        except ValueError:
            result.setdefault('errors', []).append({'code': 'd9_validation_check_rejected'})
            return operation_result(result, exit_code=2)
    if args.command == "status":
        return operation_result(result)
    if args.command in {"cancel", "resume", "cleanup"}:
        if args.command == "resume" and getattr(args, "cleanup_only", False):
            operation = "cleanup"
        else:
            operation = args.command
        task = result.get("task_run")
        if operation == "resume":
            recovery = result.get("recovery", {})
            if task is None:
                return operation_result(result)
            if recovery.get("required"):
                # D3 resume reconciles only committed operation completions,
                # then stops on unresolved side effects before dispatch.  The
                # external feedback query/conflict ports are not wired to the
                # CLI, so those states and unknown categories stay read-only.
                if (
                    recovery.get("reason") != "operation_or_evidence_unknown"
                    or _has_unresolved_external_validation(result)
                ):
                    result["errors"] = [
                        *result.get("errors", []),
                        {"code": "recovery_required"},
                    ]
                    return operation_result(result, exit_code=5)
            elif task.get("status") in TERMINAL_STATUSES:
                # Terminal replay never retries cleanup implicitly.
                return operation_result(result)
        if operation == "cancel" and (
            task is None or task.get("status") in TERMINAL_STATUSES
        ):
            return operation_result(result)
        if operation == "cleanup" and result.get("cleanup_status") == "confirmed":
            return operation_result(result)
        if declaration.get("mode") == "real" and declaration.get("execution_profile") not in {"d8_v6", "d8_v7", "d8_v8", "d8_v9", "d8_v10", "d8_v11", "d8_v12", "d8_v13", "d8_v14", "d8_v15", "d8_v16", "d8_v17", "d8_v18", "d8_v19", "d8_v20", "d8_v21", "d8_v22"}:
            if getattr(args, "authorization", None) or getattr(args, "fixture", None):
                raise ValueError("v3_real_recovery_uses_persisted_configuration")
            try:
                result = _run_v3_web_operation(
                    directory,
                    declaration,
                    operation=operation,
                    events=getattr(args, "events", None),
                )
            except Exception:  # noqa: BLE001 -- lifecycle outcome is independently projected
                result.setdefault("errors", []).append(
                    {"code": "d7_web_lifecycle_operation_unknown"}
                )
                return operation_result(result, exit_code=5)
            return operation_result(result, exit_code=result.get("exit_code", 5))
        try:
            result = _run_v3_simulated_operation(
                directory,
                declaration,
                operation=operation,
                events=getattr(args, "events", None),
            )
        except Exception:  # noqa: BLE001 -- operation status is separately reported
            result.setdefault("errors", []).append({"code": "v3_lifecycle_operation_unknown"})
            return operation_result(result, exit_code=5)
        return operation_result(result)
    raise ValueError("unsupported_v3_operation")
