from __future__ import annotations

import os


DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 300
DEFAULT_TEMPLATES_DIR = os.getenv(
    "NUCLEI_TEMPLATES_DIR",
    "/opt/nuclei-templates",
)
ALLOWED_SEVERITIES = frozenset(
    {"info", "low", "medium", "high", "critical", "unknown"}
)
