"""Opt-in, content-free diagnostics of final provider attempts, never preflight.

Lengths and prefix offsets are Unicode code points or UTF-8 bytes, not tokens.
Previous messages stay in process memory only. A process restart starts a new
comparison chain; neither the first record nor an attempt proves a cache miss
or that the provider received/billed the request.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

_LOCK = threading.Lock()
_PREVIOUS = {}
MARKERS = ("Current branch limits", "Rolling Intent:", "output_contract=", " correction=")


def _common_prefix(a, b):
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def record(*, payload, sent, role):
    """Observe the exact transport input; diagnostics cannot fail the task."""
    path = os.environ.get("QINYUAN_CACHE_PROBE")
    if not path:
        return
    try:
        from agent_core.turn_modeling import digest
        system, user = (message["content"] for message in payload["messages"])
        parameters_sha = digest(sent["parameters"])
        key = (str(Path(path).resolve()), sent["task_run_id"], sent["role_config_ref"],
               parameters_sha, sent["stage"])
        wire = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        row = {
            "phase": "provider_attempt", "ts": time.time(), "process_id": os.getpid(),
            "task_run_id": sent["task_run_id"], "request_id": sent["model_request_id"],
            "role": role, "role_config_ref": sent["role_config_ref"], "stage": sent["stage"],
            "request_sha256": digest(sent), "parameters_sha256": parameters_sha,
            "payload_sha256": hashlib.sha256(wire).hexdigest(),
            "request_utf8_bytes": len(wire),
            "system_chars": len(system), "user_chars": len(user),
            "system_utf8_bytes": len(system.encode("utf-8")),
            "user_utf8_bytes": len(user.encode("utf-8")),
            "system_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "user_sha256": hashlib.sha256(user.encode("utf-8")).hexdigest(),
            "markers": {marker: system.find(marker) for marker in MARKERS if marker in system},
        }
        contract_start = row["markers"].get("output_contract=")
        if contract_start is not None:
            contract, _ = json.JSONDecoder().raw_decode(system[contract_start + len("output_contract="):])
            row["output_contract_keys"] = list(contract)
        with _LOCK:
            previous = _PREVIOUS.get(key)
            row.update(previous_request_id=None, system_prefix_chars=None, user_prefix_chars=None,
                       user_prefix_follows_identical_system=None)
            if previous is not None:
                identity, prev_system, prev_user = previous
                row.update(previous_request_id=identity,
                           system_prefix_chars=_common_prefix(prev_system, system),
                           user_prefix_chars=_common_prefix(prev_user, user),
                           user_prefix_follows_identical_system=prev_system == system)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            _PREVIOUS[key] = (sent["model_request_id"], system, user)
    except Exception:
        # Opt-in diagnostic I/O is not an execution or recovery decision.
        return
