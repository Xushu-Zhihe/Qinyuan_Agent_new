"""Non-secret operational settings that may also live in the repository .env.

API keys and other secrets are intentionally not resolved here: each component
that needs them keeps its own explicit opt-in.  This module only serves
operational keys that are safe to share with sandbox/tooling processes.
"""

from __future__ import annotations

import os
from pathlib import Path


ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
OPERATIONAL_KEYS = frozenset({
    "QINYUAN_DOWNLOAD_ALLOWED_HOSTS",
    "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS",
})


def _read_from_file(path: Path, name: str):
    try:
        from dotenv import dotenv_values

        value = dotenv_values(path, interpolate=False).get(name)
        return value if isinstance(value, str) else None
    except Exception:
        pass
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def configured(name: str, *, path: Path | None = None):
    """Process environment first, then the .env file for allowlisted keys.

    Secrets are never served: a name outside OPERATIONAL_KEYS returns None even
    when it exists in the environment or in .env.
    """
    if name not in OPERATIONAL_KEYS:
        return None
    value = os.environ.get(name)
    if value is not None:
        return value
    env_path = Path(path) if path is not None else ENV_FILE
    if not env_path.is_file():
        return None
    return _read_from_file(env_path, name)
