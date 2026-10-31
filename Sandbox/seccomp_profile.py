"""Narrow seccomp profile for the command sandbox.

The profile is the Docker default with two extra `personality` rules so gdb can
disable ASLR. It is serialized here because the Docker API expects the profile
content, not a path (the CLI reads the file and forwards the JSON).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().with_name("seccomp") / "analysis.json"


@lru_cache(maxsize=1)
def analysis_seccomp_profile() -> str:
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise ValueError("analysis seccomp profile must keep the default deny action")
    return json.dumps(data, separators=(",", ":"), sort_keys=True)
