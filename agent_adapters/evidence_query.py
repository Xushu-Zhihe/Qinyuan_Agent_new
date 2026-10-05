"""Read-only D5 evidence projection and explicit static HTML export."""

from __future__ import annotations

import html
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_adapters.process_fact_evidence import D5EvidenceError, FileD5Evidence
from agent_adapters.process_sources import D5SourceReader
from agent_adapters.run_files import immutable_read, run_path
from agent_core.control_candidate import validate_control_ownership
from agent_core.control_store import StorageError
from agent_core.process_facts import D5Attempt, D5ProcessFact
from agent_core.redaction import (
    D5Redactor,
    D5SensitiveValue,
    d5_sensitive_configuration_matches,
)
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_modeling import digest


class EvidenceQueryError(ValueError):
    """A whole-query input, run, or database failure."""


def sensitive_config_from_json(raw: str | None) -> dict[str, D5SensitiveValue] | None:
    """Parse ephemeral query configuration without returning untrusted error text."""
    if raw is None:
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            return None
        configuration = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(item, dict)
                or set(item) != {"version", "value"}
                or not isinstance(item["version"], str)
                or not item["version"].strip()
                or not isinstance(item["value"], str)
                or not item["value"].strip()
            ):
                return None
            configuration[key] = D5SensitiveValue(item["version"], item["value"])
        return configuration
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _source_projection(
    reader: D5SourceReader,
    task_run_id: str,
    source,
    redactor: D5Redactor,
    redaction_available: bool,
):
    projected = reader.project(task_run_id, source)
    content = projected.get("content")
    if projected.get("status") == "available" and isinstance(content, dict):
        # Evidence bodies expose only byte counts and SHA-256 values. The receipt's
        # fixed operation/status fields are metadata, separate from its body.
        body_fields = {
            "stdout_bytes",
            "stdout_sha256",
            "stderr_bytes",
            "stderr_sha256",
            "byte_size",
            "sha256",
        }
        metadata_fields = {
            "operation_type",
            "execution_id",
            "plan_id",
            "step_index",
            "status",
            "exit_code",
            "timed_out",
            "finished_at",
            "summary",
            "observation_id",
            "artifact_version_id",
            "observed_status",
            "rule_version",
            "proof_id",
            "model_request_id",
            "stage",
        }
        body = {key: value for key, value in content.items() if key in body_fields}
        metadata = {
            "identity": "source_metadata",
            **{key: value for key, value in content.items() if key in metadata_fields},
        }
        summary = metadata.get("summary")
        sensitive_summary = isinstance(summary, str) and redactor.contains_sensitive(
            summary
        )
        if not redaction_available or sensitive_summary:
            metadata["summary"] = None
            metadata["summary_status"] = "withheld"
            metadata["summary_reason"] = (
                "redaction_configuration_unavailable"
                if not redaction_available
                else "sensitive_content_detected"
            )
        else:
            metadata["summary_status"] = "available"
        projected = {
            **projected,
            "content": body or None,
            "metadata": metadata,
            "evidence_body": {
                "identity": "size_sha256_only",
                "status": "available" if body else "withheld",
                "content": body or None,
                "reason": None if body else "body_not_in_d5_whitelist",
            },
        }
    else:
        projected = {
            **projected,
            "content": None,
            "metadata": None,
            "evidence_body": {
                "identity": "size_sha256_only",
                "status": projected["status"],
                "content": None,
            },
        }
    return projected


def _read_attempt(evidence: FileD5Evidence, attempt: D5Attempt) -> dict[str, Any]:
    base = {
        "model_request_id": attempt.model_request_id,
        "review_id": attempt.review_id,
        "started_at": attempt.started_at,
        "source_count": len(attempt.sources),
        "sources": [asdict(source) for source in attempt.sources],
        "status": "available",
        "outcome": None,
        "reason": None,
        "finished_at": None,
    }
    try:
        request = evidence.read_request(attempt.model_request_id)
        result = evidence.read_result(attempt.model_request_id)
        candidate = evidence.read_candidate(attempt.model_request_id)
    except D5EvidenceError:
        return {**base, "status": "corrupt"}
    if request is None:
        return {**base, "status": "missing"}
    if request.attempt != attempt:
        return {**base, "status": "corrupt"}
    if result is None:
        return {**base, "status": "missing"}
    if result["outcome"] == "accepted" and candidate is None:
        return {**base, "status": "missing"}
    if (
        result["outcome"] == "response_unknown"
        and result["reason"] != "not_applied_response_unknown"
    ):
        return {**base, "status": "corrupt"}
    if result["outcome"] != "accepted" and candidate is not None:
        return {**base, "status": "corrupt"}
    return {
        **base,
        "outcome": result["outcome"],
        "reason": result["reason"],
        "finished_at": result["finished_at"],
    }


def _read_fact(
    evidence: FileD5Evidence,
    fact: D5ProcessFact,
    attempt: D5Attempt | None,
    *,
    redactor: D5Redactor,
    redaction_available: bool,
) -> dict[str, Any]:
    base = {
        "identity": "model_summary",
        "display_role": "process_fact_metadata",
        "process_fact_id": fact.process_fact_id,
        "version": fact.version,
        "review_id": fact.review_id,
        "model_request_id": fact.model_request_id,
        "created_at": fact.created_at,
        "evidence_sha256": fact.evidence_sha256,
        "conflicts": [asdict(conflict) for conflict in fact.conflicts],
        "claim_count": None,
        "summary_text": None,
        "claims": None,
    }
    if attempt is None or attempt.task_run_id != fact.task_run_id:
        return {**base, "status": "corrupt"}
    try:
        request = evidence.read_request(fact.model_request_id)
        result = evidence.read_result(fact.model_request_id)
        candidate = evidence.read_candidate(fact.model_request_id)
    except D5EvidenceError:
        return {**base, "status": "corrupt"}
    if request is None or result is None or candidate is None:
        return {**base, "status": "missing"}
    if (
        request.attempt != attempt
        or fact.sources != attempt.sources
        or fact.review_id != attempt.review_id
        or result["outcome"] != "accepted"
        or digest(asdict(candidate)) != fact.evidence_sha256
    ):
        return {**base, "status": "corrupt"}
    allowed_sources = {source.object_id: source for source in fact.sources}
    if any(
        ref not in allowed_sources
        or allowed_sources[ref].subject_ref != claim.subject_ref
        or allowed_sources[ref].observation_scope != claim.observation_scope
        for claim in candidate.claims
        for ref in claim.source_refs
    ):
        return {**base, "status": "corrupt"}
    base["claim_count"] = len(candidate.claims)
    if (
        not redaction_available
        or redactor.contains_sensitive(candidate.summary_text)
        or any(
            redactor.contains_sensitive(claim.claim_key)
            or redactor.contains_sensitive(claim.claim_value)
            for claim in candidate.claims
        )
    ):
        return {
            **base,
            "status": "withheld",
            "reason": (
                "redaction_configuration_unavailable"
                if not redaction_available
                else "sensitive_content_detected"
            ),
        }
    return {
        **base,
        "status": "available",
        "summary_text": candidate.summary_text,
        "claims": [asdict(claim) for claim in candidate.claims],
    }


def query_d5_evidence(
    state_dir: Path | None,
    task_run_id: str,
    *,
    sensitive_config: Mapping[str, D5SensitiveValue] | None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Read one run without migrating, creating, dispatching, or writing evidence."""
    try:
        if (state_dir is None) == (run_dir is None):
            raise EvidenceQueryError("evidence_directory_selector_invalid")
        try:
            task_id = UUID(task_run_id)
            if task_id.version != 4 or str(task_id) != task_run_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise EvidenceQueryError("invalid_task_run_id") from exc
        if run_dir is not None:
            directory = Path(run_dir).resolve()
        else:
            assert state_dir is not None
            directory = run_path(Path(state_dir), task_run_id)
        if not directory.is_dir():
            raise EvidenceQueryError("evidence_run_not_found")
        if state_dir is not None:
            declaration = immutable_read(directory / "run.json")
            if (
                not isinstance(declaration, dict)
                or declaration.get("task_run_id") != task_run_id
                or declaration.get("protocol_version") not in {1, 2}
            ):
                raise EvidenceQueryError("evidence_run_identity_invalid")
        database = directory / "control.sqlite"
        with SqliteControlStore.open_readonly(database) as store:
            if run_dir is not None and store.list_task_run_refs() != (task_run_id,):
                raise EvidenceQueryError("evidence_run_ownership_invalid")
            task = store.load_task_run(task_run_id)
            if task is None or task.task_run_id != task_run_id:
                raise EvidenceQueryError("evidence_run_not_found")
            try:
                config = store.load_d3_run_config(task_run_id)
            except (TypeError, ValueError):
                # An undecodable D3/D5 configuration cannot authorize free-text
                # display. Keep the independent structured evidence readable.
                config = None
            requirements = (
                config.d5_sensitive_requirements if config is not None else None
            )
            redaction_available = (
                config is not None
                and config.d5_enabled
                and d5_sensitive_configuration_matches(requirements, sensitive_config)
            )
            sensitive_values = (
                tuple(entry.value for entry in (sensitive_config or {}).values())
                if redaction_available
                else None
            )
            redactor = D5Redactor(sensitive_values)
            attempts: list[D5Attempt] = []
            attempt_errors: set[str] = set()
            for model_request_id in store.list_d5_attempt_refs(task_run_id):
                try:
                    attempt = store.load_d5_attempt(model_request_id)
                except (TypeError, ValueError, KeyError, StorageError):
                    attempt_errors.add(model_request_id)
                    continue
                if attempt is None or attempt.task_run_id != task_run_id:
                    attempt_errors.add(model_request_id)
                    continue
                attempts.append(attempt)
            facts: list[D5ProcessFact] = []
            fact_errors: set[str] = set()
            for process_fact_id in store.list_process_fact_refs(task_run_id):
                try:
                    fact = store.load_process_fact(process_fact_id)
                except (TypeError, ValueError, KeyError, StorageError):
                    fact_errors.add(process_fact_id)
                    continue
                if fact is None or fact.task_run_id != task_run_id:
                    fact_errors.add(process_fact_id)
                    continue
                facts.append(fact)
            attempts.sort(key=lambda item: item.started_at)
            facts.sort(key=lambda item: item.version)
            attempts_by_id = {attempt.model_request_id: attempt for attempt in attempts}
            evidence = FileD5Evidence(directory / "d5-process", create=False)
            reader = D5SourceReader(directory, store)
            sources: dict[str, dict[str, Any]] = {}
            records: tuple[D5Attempt | D5ProcessFact, ...] = (*attempts, *facts)
            for record in records:
                for source in record.sources:
                    sources.setdefault(
                        source.version_key,
                        _source_projection(
                            reader, task_run_id, source, redactor, redaction_available
                        ),
                    )

            def project_reference(object_id, kind):
                base = {
                    "reference": object_id,
                    "kind": kind,
                    "status": "missing",
                    "applicability": None,
                    "content": None,
                    "origin": None,
                }
                try:
                    source = reader.read(task_run_id, object_id, kind)
                except Exception:  # noqa: BLE001 -- a referenced receipt is isolated
                    return {**base, "status": "corrupt"}
                if source is None:
                    return base
                if source.version_key not in sources:
                    sources[source.version_key] = _source_projection(
                        reader, task_run_id, source, redactor, redaction_available
                    )
                return sources[source.version_key]

            control = store.load_control_state_rows(task_run_id)
            if control is None:
                raise EvidenceQueryError("evidence_run_not_found")
            validate_control_ownership(control)
            execution_rows = [
                {
                    "execution_id": execution.execution_id,
                    "plan_id": execution.plan_id,
                    "step_index": execution.step_index,
                    "status": execution.status.value,
                    "command_ref": execution.command_ref,
                    "output_ref": execution.output_ref,
                    "source": project_reference(execution.execution_id, "execution"),
                }
                for execution in control.executions
            ]
            execution_by_id = {row["execution_id"]: row for row in execution_rows}
            fact_rows_from_control = []
            for control_fact in control.facts:
                linked = []
                execution = execution_by_id.get(control_fact.source_ref)
                if execution is not None:
                    linked.append(execution["source"])
                if (
                    control_fact.verification_ref is not None
                    and control_fact.verification_ref.startswith("d3:")
                ):
                    linked.append(
                        project_reference(
                            control_fact.verification_ref.removeprefix("d3:"),
                            "observation_proof",
                        )
                    )
                verified = (
                    control_fact.status.value == "verified"
                    and control_fact.verification_ref is not None
                    and control_fact.verification_ref.startswith("d3:")
                    and bool(linked)
                    and all(item["status"] == "available" for item in linked)
                )
                fact_rows_from_control.append(
                    {
                        "identity": "verified_fact"
                        if control_fact.status.value == "verified"
                        else "fact",
                        "fact_id": control_fact.fact_id,
                        "turn_id": control_fact.turn_id,
                        "status": control_fact.status.value,
                        "verification_identity": (
                            control_fact.verification_ref.split(":", 1)[0]
                            if control_fact.verification_ref is not None
                            and ":" in control_fact.verification_ref
                            else "untyped"
                            if control_fact.verification_ref is not None
                            else "none"
                        ),
                        "verification_status": control_fact.status.value,
                        "verification_source_status": (
                            "available" if verified else "not_confirmed"
                        ),
                        "source_ref": control_fact.source_ref,
                        "verification_ref": control_fact.verification_ref,
                        "sources": linked,
                        "content_status": "withheld",
                        "content_reason": "free_text_not_in_d5_whitelist",
                    }
                )
            attempt_rows = []
            for attempt in attempts:
                row = _read_attempt(evidence, attempt)
                row["applied"] = any(
                    fact.model_request_id == attempt.model_request_id for fact in facts
                )
                row["sources"] = [
                    sources[source.version_key] for source in attempt.sources
                ]
                attempt_rows.append(row)
            for model_request_id in sorted(attempt_errors):
                attempt_rows.append(
                    {
                        "model_request_id": model_request_id,
                        "status": "corrupt",
                        "sources": [],
                    }
                )
            fact_rows = []
            for fact in facts:
                row = _read_fact(
                    evidence,
                    fact,
                    attempts_by_id.get(fact.model_request_id),
                    redactor=redactor,
                    redaction_available=redaction_available,
                )
                row["sources"] = [
                    sources[source.version_key] for source in fact.sources
                ]
                source_rows = row["sources"]
                applicability = [item.get("applicability") for item in source_rows]
                row["applicability"] = (
                    "unavailable"
                    if any(
                        item.get("status") != "available"
                        or item.get("applicability") not in {"current", "historical"}
                        for item in source_rows
                    )
                    else "historical"
                    if "historical" in applicability
                    else "current"
                )
                fact_rows.append(row)
            for process_fact_id in sorted(fact_errors):
                fact_rows.append(
                    {
                        "identity": "model_summary",
                        "process_fact_id": process_fact_id,
                        "status": "corrupt",
                        "sources": [],
                    }
                )
            from agent_adapters.sensitive_evidence_service import (
                query_sensitive_evidence,
            )

            protected = query_sensitive_evidence(directory, task_run_id)
            proof_recheck = {
                row["proof_id"]: row
                for row in protected.get("proof_recheck", [])
            }
            for source in sources.values():
                projection = proof_recheck.get(source.get("source", {}).get("object_id"))
                if projection is not None:
                    source["sensitive_evidence_ids"] = projection["evidence_ids"]
                    source["recheck_status"] = projection["recheck_status"]
            for row in fact_rows_from_control:
                verification_ref = row["verification_ref"]
                if isinstance(verification_ref, str) and verification_ref.startswith("d3:"):
                    projection = proof_recheck.get(verification_ref.removeprefix("d3:"))
                    if projection is not None:
                        row["sensitive_evidence_ids"] = projection["evidence_ids"]
                        row["recheck_status"] = projection["recheck_status"]
                        if projection["recheck_status"] == "evidence_deleted_unverifiable":
                            row["verification_source_status"] = "evidence_deleted_unverifiable"
            return {
                "protocol_version": 1,
                "task_run_id": task_run_id,
                "redaction_configuration": {
                    "status": "available" if redaction_available else "withheld",
                    "reason": (
                        None
                        if redaction_available
                        else "redaction_configuration_unavailable"
                    ),
                },
                "attempts": attempt_rows,
                "process_facts": fact_rows,
                "executions": execution_rows,
                "facts": fact_rows_from_control,
                "source_links": [sources[key] for key in sorted(sources)],
                **protected,
                "exit_code": 0,
            }
    except EvidenceQueryError:
        raise
    except Exception as exc:
        raise EvidenceQueryError("evidence_database_read_failed") from exc


def export_static_html(result: dict[str, Any], state_dir: Path, output: Path) -> None:
    """Write an explicit, text-only report outside the source state directory."""
    root = Path(state_dir).resolve()
    destination = Path(output).resolve()
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        raise EvidenceQueryError("evidence_export_must_be_outside_state_dir")
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    document = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Qinyuan evidence</title>"
        "<style>body{font:14px/1.5 system-ui,sans-serif;max-width:1100px;"
        "margin:32px auto;padding:0 20px;color:#17212b}pre{white-space:pre-wrap;"
        "overflow-wrap:anywhere;background:#f3f5f6;padding:16px;border:1px solid #ccd3d8}"
        "</style></head><body><h1>Qinyuan evidence</h1><pre>"
        + html.escape(encoded, quote=True)
        + "</pre></body></html>\n"
    )
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise EvidenceQueryError("evidence_export_exists") from exc
