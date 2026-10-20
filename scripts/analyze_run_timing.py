"""Read-only D4 call timing; no model calls, migrations, or saved snapshots.

python scripts/analyze_run_timing.py RUN_DIRECTORY [--task-run-id UUID]
Intervals include dispatch-side overhead, not just provider generation time.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_adapters.output_evidence import FileD8OutputEvidence
from agent_core.sqlite_control_store import SqliteControlStore
from agent_core.turn_modeling import digest


def summarize(task, operations, evidence):
    stages, roles, calls = {}, {}, []
    unfinished, missing_requests, missing_results = [], [], []
    observed_end = task.started_at
    for operation in operations:
        start, end = operation.start, operation.completion
        observed_end = max(observed_end, start.started_at, end.finished_at if end else start.started_at)
        if start.role is None:
            continue
        identity = start.operation_id
        sent = evidence.read("provider-request", identity)
        result = evidence.read("provider-result", identity)
        contract = evidence.read("contract", identity)
        if sent is not None and (sent["task_run_id"] != task.task_run_id
                or sent["model_request_id"] != identity or sent["role_config_ref"] != start.role_config_ref):
            raise ValueError("timing_provider_request_binding_changed")
        if result is not None and (sent is None or result["model_request_id"] != identity
                or result["request_sha256"] != digest(sent)):
            raise ValueError("timing_provider_result_binding_changed")
        if contract is not None and (contract["task_run_id"] != task.task_run_id
                or contract["model_request_id"] != identity or contract["role_config_ref"] != start.role_config_ref
                or sent is not None and contract["stage"] != sent["stage"]):
            raise ValueError("timing_contract_binding_changed")
        if sent is None:
            missing_requests.append(identity)
        if sent is not None and result is None:
            missing_results.append(identity)
        if end is None:
            unfinished.append(identity)
        elif end.finished_at < start.started_at:
            raise ValueError("timing_operation_interval_invalid")
        transport = result["transport"] if result else {}
        usage = transport.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        row = dict(model_request_id=identity, role=start.role,
            stage=sent["stage"] if sent else contract["stage"] if contract else "unrecorded",
            seconds=end.finished_at-start.started_at if end else None,
            correction_of=contract["correction_of_model_request_id"] if contract else None,
            finish_reason=transport.get("finish_reason"), transport_end=transport.get("transport_end"),
            prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"))
        calls.append(row)
        for groups, key in ((stages, row["stage"]), (roles, start.role)):
            total = groups.setdefault(key, dict(calls=0, completed_intervals=0, seconds=0.0,
                corrections=0, finish_reasons={}, prompt_tokens=0, completion_tokens=0,
                reasoning_tokens=0, usage_observations=0))
            total["calls"] += 1
            total["corrections"] += row["correction_of"] is not None
            if row["seconds"] is not None:
                total["completed_intervals"] += 1
                total["seconds"] += row["seconds"]
            if row["finish_reason"] is not None:
                reasons = total["finish_reasons"]
                reasons[row["finish_reason"]] = reasons.get(row["finish_reason"], 0) + 1
            total["usage_observations"] += bool(usage)
            for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                total[name] += row[name] or 0
    endpoint = max(observed_end, task.finished_at or observed_end)
    return dict(task_run_id=task.task_run_id,
        timing_basis="D4 operation intervals; includes local dispatch overhead; overlapping sums are not wall time",
        usage_basis="reported usage only; reasoning_tokens are included in completion_tokens",
        started_at=task.started_at, observed_through=endpoint,
        observed_wall_seconds=endpoint-task.started_at, wall_is_lower_bound=task.finished_at is None,
        unfinished_model_operations=unfinished, missing_provider_requests=missing_requests,
        missing_provider_results=missing_results, roles=roles, stages=stages, calls=calls)


def analyze(directory, task_run_id=None):
    directory = Path(directory)
    with SqliteControlStore.open_readonly(directory / "control.sqlite") as store:
        if task_run_id is None:
            refs = store.list_task_run_refs()
            if len(refs) != 1:
                raise ValueError("timing_requires_one_task_or_explicit_id")
            task_run_id = refs[0]
        task = store.load_task_run(task_run_id)
        if task is None:
            raise ValueError("timing_task_not_found")
        return summarize(task, store.list_d4_operations(task_run_id),
                         FileD8OutputEvidence(directory / "d8-output", create=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--task-run-id")
    args = parser.parse_args()
    print(json.dumps(analyze(args.directory, args.task_run_id), ensure_ascii=False, indent=2))
