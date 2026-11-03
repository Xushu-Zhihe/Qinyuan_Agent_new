"""Show the private on-demand tool download counter (never cleared)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Sandbox.tool_usage import counter_path, counts  # noqa: E402


def main() -> None:
    data = counts()
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"counter: {counter_path()}")
    if not data:
        print("no on-demand tool downloads recorded")
        return
    print(f"{'tool':28} {'count':>6}  first_at                  last_at")
    for name, row in data.items():
        print(f"{name:28} {row['count']:>6}  {row['first_at']:24}  {row['last_at']}")


if __name__ == "__main__":
    main()
