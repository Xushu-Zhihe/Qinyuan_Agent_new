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
    intervals = []
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
        if end is not None:
            intervals.append((start.started_at, end.finished_at))
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
                reasoning_tokens=0, usage_observations=0, max_prompt_tokens=None))
            total["calls"] += 1
            total["corrections"] += row["correction_of"] is not None
            if row["seconds"] is not None:
                total["completed_intervals"] += 1
                total["seconds"] += row["seconds"]
            if row["finish_reason"] is not None:
                reasons = total["finish_reasons"]
                reasons[row["finish_reason"]] = reasons.get(row["finish_reason"], 0) + 1
            total["usage_observations"] += bool(usage)
            if row["prompt_tokens"] is not None:
                total["max_prompt_tokens"] = max(total["max_prompt_tokens"] or 0, row["prompt_tokens"])
            for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                total[name] += row[name] or 0
    merged = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([begin, finish])
    endpoint = max(observed_end, task.finished_at or observed_end)
    return dict(model_interval_union_seconds=sum(b-a for a,b in merged),
        model_interval_sum_seconds=sum(b-a for a,b in intervals), task_run_id=task.task_run_id,
        timing_basis="D4 operation intervals; includes local dispatch overhead; overlapping sums are not wall time",
        usage_basis="reported usage only; reasoning_tokens are included in completion_tokens",
        started_at=task.started_at, observed_through=endpoint,
        observed_wall_seconds=endpoint-task.started_at, wall_is_lower_bound=task.finished_at is None,
        unfinished_model_operations=unfinished, missing_provider_requests=missing_requests,
        missing_provider_results=missing_results, roles=roles, stages=stages, calls=calls)


def summarize_cache(task, operations, evidence, probe_path, *, decisions=()):
    """Join final-attempt probes by identity and audit digest, never size/mtime.

    Old build-time probes cannot be corrected retroactively without their text.
    They remain explicitly excluded from prefix conclusions. Missing usage is
    unknown, not zero; system/user prefix lengths must not be added together.
    """
    from collections import Counter, defaultdict
    from statistics import median

    probes, ignored = defaultdict(list), Counter()
    for line in Path(probe_path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            ignored["incomplete_or_invalid_lines"] += 1
            continue
        if not isinstance(row, dict) or row.get("phase") != "provider_attempt":
            ignored["legacy_records"] += 1
        elif row.get("task_run_id") != task.task_run_id:
            ignored["other_task_records"] += 1
        else:
            probes[row.get("request_id")].append(row)

    def actions(contract):
        schema = contract["output_contract"]["schema"]
        return dict(Counter(branch.get("properties", {}).get("kind", {}).get("const", "object")
                            for branch in schema.get("anyOf", [schema])))

    calls, previous, stages, roles, intents = [], {}, defaultdict(list), defaultdict(list), defaultdict(list)
    intent_by_request = {record.model_request_id: record.intent_id for record in decisions}
    for operation in operations:
        start = operation.start
        sent = evidence.read("provider-request", start.operation_id) if start.role else None
        if sent is None:
            continue
        identity, stage = start.operation_id, sent["stage"]
        key = (stage, sent["role_config_ref"], digest(sent["parameters"]))
        result = evidence.read("provider-result", identity) or {}
        usage = result.get("transport", {}).get("usage") or {}
        item = dict(model_request_id=identity, role=start.role, intent_id=intent_by_request.get(identity),
                    stage=stage, started_at=start.started_at,
                    operation_completed=operation.completion is not None,
                    prompt_tokens=usage.get("prompt_tokens"),
                    cache_hit_tokens=usage.get("prompt_cache_hit_tokens"),
                    cache_miss_tokens=usage.get("prompt_cache_miss_tokens"),
                    probe_status="missing", prefix_status="unavailable")
        candidates = probes.pop(identity, [])
        row = candidates[0] if len(candidates) == 1 else None
        if len(candidates) > 1:
            item["probe_status"] = "duplicate_attempt_records"
        if row is not None:
            bound = (row.get("request_sha256") == digest(sent)
                     and row.get("stage") == stage and row.get("role") == start.role
                     and row.get("role_config_ref") == sent["role_config_ref"]
                     and row.get("parameters_sha256") == key[2]
                     and row.get("request_utf8_bytes") == sent["request_utf8_bytes"])
            item["probe_status"] = "matched" if bound else "binding_mismatch"
            if bound:
                item.update(system_chars=row["system_chars"], user_chars=row["user_chars"],
                            output_contract_keys=row.get("output_contract_keys"), messages=row.get("messages"))
                prev = previous.get(key)
                if prev is None:
                    item["prefix_status"] = "first_observed_attempt" if row.get("previous_request_id") is None else "unmatched_predecessor"
                elif (prev["probe_status"] == "matched"
                      and row.get("previous_request_id") == prev["model_request_id"]):
                    item.update(prefix_status="comparable", previous_model_request_id=prev["model_request_id"],
                                system_prefix_chars=row["system_prefix_chars"],
                                user_prefix_chars=row["user_prefix_chars"],
                                user_prefix_follows_identical_system=row["user_prefix_follows_identical_system"],
                                messages_prefix_chars=row.get("messages_prefix_chars"),
                                messages_prefix_utf8_bytes=row.get("messages_prefix_utf8_bytes"),
                                first_changed_message=row.get("first_changed_message"),
                                output_contract_key_order_changed=prev.get("output_contract_keys") != row.get("output_contract_keys"))
                    offset = row.get("markers", {}).get("output_contract=")
                    if offset is not None and not row["user_prefix_follows_identical_system"]:
                        # Negative = before JSON; zero = first JSON character.
                        item["first_difference_from_contract_json_chars"] = row["system_prefix_chars"] - offset - len("output_contract=")
                else:
                    item["prefix_status"] = "comparison_chain_unavailable"
        prev = previous.get(key)
        if prev is not None:
            item["since_previous_attempt_seconds"] = start.started_at - prev["started_at"]
            left = evidence.read("contract", prev["model_request_id"])
            right = evidence.read("contract", identity)
            if left is not None and right is not None:
                item["frozen_contract_changes"] = dict(
                    schema_changed=left["output_contract"]["schema"] != right["output_contract"]["schema"],
                    action_branches_before=actions(left), action_branches_after=actions(right),
                    instruction_changed=left["instruction"] != right["instruction"],
                    correction_changed=left["correction"] != right["correction"])
        previous[key] = item
        calls.append(item)
        stages[stage].append(item)
        roles[start.role].append(item)
        if item["intent_id"] is not None:
            intents[item["intent_id"]].append(item)

    def aggregate(rows):
        known = [r for r in rows if type(r["prompt_tokens"]) is int and type(r["cache_hit_tokens"]) is int]
        prefixes = [r["system_prefix_chars"] for r in rows if r["prefix_status"] == "comparable"]
        gaps = [r["since_previous_attempt_seconds"] for r in rows if "since_previous_attempt_seconds" in r]
        prompt = sum(r["prompt_tokens"] for r in known)
        hit = sum(r["cache_hit_tokens"] for r in known)
        return dict(attempts=len(rows), cache_usage_observations=len(known),
            prompt_tokens=prompt, cache_hit_tokens=hit, cache_hit_ratio=hit / prompt if prompt else None,
            matched_probes=sum(r["probe_status"] == "matched" for r in rows),
            comparable_prefixes=len(prefixes), median_system_prefix_chars=median(prefixes) if prefixes else None,
            median_attempt_gap_seconds=median(gaps) if gaps else None)
    return dict(basis="final provider attempts joined to D4/audit IDs; first observed is not cold cache",
                prefix_units="Unicode code points, not tokens; user prefix reusable only after identical system",
                ignored_records=dict(ignored), unmatched_attempt_records=sum(map(len, probes.values())),
                stages={key: aggregate(rows) for key, rows in stages.items()},
                roles={key: aggregate(rows) for key, rows in roles.items()},
                intents={key: aggregate(rows) for key, rows in intents.items()},
                unattributed_inner_attempts=sum(r["role"] == "inner_action" and r["intent_id"] is None for r in calls),
                calls=calls)


def summarize_supervision(task, operations, decisions, tree, handoff, sessions):
    """Observable effects only: continuing is not evidence of wasted review."""
    from collections import Counter, defaultdict
    delivered, choices = set(), defaultdict(Counter)
    for row in decisions:
        frozen = sessions.read("request", row.model_request_id)
        if frozen is None:
            continue
        advice = json.loads(frozen["request"].get("history_context", "{}") or "{}").get("strategy_advice")
        if not advice:
            continue
        review = advice["review_id"]
        if "reason" in advice:
            delivered.add(review)
        saved = sessions.read("decision", row.record_id)
        if saved is not None:
            if digest(saved) != row.evidence_sha256:
                raise ValueError("timing_strategy_decision_binding_changed")
            choices[review][saved["response"].get("kind", "other")] += 1
    yields = Counter()
    for row in tree:
        if row.kind == "strategy_yielded":
            note = handoff.read("strategy-yield", row.record_id)
            if note is not None:
                yields[note["review_id"]] += 1
    intercepted = Counter()
    for op in operations:
        note = handoff.read("strategy-discard", op.start.operation_id)
        if note:
            intercepted[note["review_id"]] += note["disposition"] == "not_dispatched"
    calls, totals, validity_reasons = [], Counter(), Counter()
    for op in operations:
        if op.start.role is None:
            continue
        request = handoff.read("request", op.start.operation_id)
        if not request or request.get("stage") != "strategy_supervision":
            continue
        if request["task_run_id"] != task.task_run_id or digest(request) != op.start.context_sha256:
            raise ValueError("timing_strategy_request_binding_changed")
        identity = op.start.operation_id
        result, effect = handoff.read("result", identity), handoff.read("strategy-effect", identity)
        if any(v is not None and v["request_sha256"] != digest(request) for v in (result, effect)):
            raise ValueError("timing_strategy_result_binding_changed")
        raw = (result or {}).get("raw") or {}
        outcome = effect["outcome"] if effect else "pending_or_unapplied"
        totals[outcome] += 1
        validity = (effect or {}).get("validity_reason", "legacy_unclassified")
        validity_reasons[validity] += 1
        if raw.get("suggestion"):
            totals["suggestions_proposed"] += 1
            totals["suggestions_applied"] += outcome == "applied"
        stop = outcome == "applied" and raw.get("decision") == "request_intent_resolution"
        totals["stops_proposed"] += raw.get("decision") == "request_intent_resolution"
        totals["stops_applied"] += stop
        totals["continues_applied"] += outcome == "applied" and raw.get("decision") == "continue_current_intent"
        calls.append(dict(model_request_id=identity, outcome=outcome, validity_reason=validity, decision=raw.get("decision"),
            suggestion_intent=(raw.get("suggestion") or {}).get("intent_id"),
            advice_delivered=identity in delivered, voluntary_yields=yields[identity],
            intercepted_proposals=intercepted[identity], subsequent_decision_kinds=dict(choices[identity])))
    return dict(basis="observable events; no-switch is not uselessness; decisions do not prove causal benefit",
                counts={**dict(totals), "calls": len(calls), "advice_delivered": len(delivered),
                        "voluntary_yields": sum(yields.values())}, validity_reasons=dict(validity_reasons), calls=calls)



def summarize_activity(task, store, output, sessions, handoff):
    """Count durable identities, not filenames or presumed semantic progress."""
    from collections import Counter
    actions, tools, http, reads = Counter(), Counter(), Counter(), Counter()
    experiment_sessions = set()
    for row in store.list_d8_decisions(task.task_run_id):
        data = sessions.read('decision', row.record_id)
        if data is None or digest(data) != row.evidence_sha256:
            raise ValueError('timing_decision_binding_changed')
        if data['request']['stage'] != 'inner_plan':
            continue
        raw = data['response']
        kind = raw.get('kind', 'unclassified')
        tool = raw.get('tool_name')
        actions[kind + (':' + tool if kind == 'mcp_call' and tool else '')] += 1
        if tool == 'http.run_experiment' and output.read('experiment-result', row.model_request_id) is not None:
            experiment_sessions.add(row.model_request_id)
    for op in store.list_d4_operations(task.task_run_id):
        if op.start.category != 'tool_dispatch':
            continue
        identity = op.start.operation_id
        claim = output.read('mcp-call', identity)
        if claim is not None:
            if claim['task_run_id'] != task.task_run_id:
                raise ValueError('timing_tool_owner_changed')
            tool = claim['call']['tool_name']
            tools[tool + (':completed' if op.completion is not None else ':pending')] += 1
            if tool == 'http.run_experiment':
                http['experiment:' + ('completed' if op.completion is not None else 'pending')] += 1
        else:
            binding = store.load_d3_plan_binding(op.start.resource_ref)
            from agent_core.http_contracts import HttpPlanBinding
            if not isinstance(binding, HttpPlanBinding):
                continue
            stopped = output.read('http-dispatch-stop', identity)
            result = output.read('http-result', identity)
            state = 'not_dispatched' if stopped else 'completed' if op.completion is not None else 'pending'
            http['standalone:' + state] += 1
    for identity, start, completion in store.list_d8_reads(task.task_run_id):
        snapshot = handoff.read('read-snapshot', start['snapshot_id'])
        if snapshot is None or digest(snapshot) != start['snapshot_sha256']:
            raise ValueError('timing_read_snapshot_changed')
        reads['completed' if completion is not None else 'pending'] += 1
        reads['next_page' if snapshot['read_input']['cursor'] else 'initial'] += 1
    blocks = set()
    for path in handoff.records.directory.glob('d8-handoff-pause-*.json'):
        identity = path.stem.removeprefix('d8-handoff-pause-')
        record = handoff.read('pause', identity)
        if record.get('task_run_id') == task.task_run_id and record.get('scope') in {'intent', 'task'}:
            blocks.add((record['checkpoint_sequence'], record.get('intent_id'), record['kind']))
    return dict(basis='durable applied decisions, operation starts/completions and read starts; counts do not measure quality',
        applied_inner_decisions=dict(actions), tools=dict(tools), http=dict(http),
        completed_experiment_sessions=len(experiment_sessions), history_reads=dict(reads),
        distinct_capacity_blocks=len(blocks))

def analyze(directory, task_run_id=None, *, probe_path=None):
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
        operations = store.list_d4_operations(task_run_id)
        evidence = FileD8OutputEvidence(directory / "d8-output", create=False)
        report = summarize(task, operations, evidence)
        if (directory / "d8-handoff").is_dir() and (directory / "d8-sessions").is_dir():
            from agent_adapters.handoff_evidence import FileD8HandoffEvidence
            report['activity'] = summarize_activity(task, store, evidence,
                FileD8HandoffEvidence(directory / 'd8-sessions', create=False),
                FileD8HandoffEvidence(directory / 'd8-handoff', create=False))
            report["supervision"] = summarize_supervision(task, operations,
                store.list_d8_decisions(task_run_id), store.list_d8_tree_records(task_run_id),
                FileD8HandoffEvidence(directory / "d8-handoff", create=False),
                FileD8HandoffEvidence(directory / "d8-sessions", create=False))
        if probe_path is not None:
            report["cache"] = summarize_cache(task, operations, evidence, probe_path,
                decisions=store.list_d8_decisions(task_run_id))
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--task-run-id")
    parser.add_argument("--probe", type=Path, help="Optional final-attempt cache diagnostic JSONL")
    args = parser.parse_args()
    print(json.dumps(analyze(args.directory, args.task_run_id, probe_path=args.probe), ensure_ascii=False, indent=2))
