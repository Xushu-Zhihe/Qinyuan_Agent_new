"""构造一次真实 web 任务的请求文件（本地生成，供 CLI 读取）。

    python scripts/make_web_task.py <url> <goal> <out.json>
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agent_adapters import deepseek_roles  # noqa: E402
from agent_adapters.simulation import real_config  # noqa: E402
from agent_core.operation_accounting import D8HandoffBudgetPolicy  # noqa: E402
from agent_core.web_contracts import canonical_web_origin  # noqa: E402


def build(url: str, goal: str, *, timeout: int = 60, deadline: int = 1800) -> dict:
    origin = canonical_web_origin(url)
    return {
        "protocol_version": 3,
        "request_id": str(uuid.uuid4()),
        "execution_profile": "d8_v22",
        "mode": "real",
        "d8_config": real_config(contract_version=22, http=True, http_timeout_seconds=timeout),
        "task_request": {
            "goal": goal,
            "deadline_seconds": deadline,
            "target": {
                "source_root": str(Path.cwd()),
                "endpoints": [{"url": url}],
                "attachments": [],
            },
        },
        "authorization": {
            "authorization_id": str(uuid.uuid4()),
            "allowed_origin": origin,
            "allowed_methods": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            "allowed_tool": "d7_http_request",
            "network_allowed": True,
            "attachment_indices": [],
        },
        "dual_llm_config": {
            "outer_role": asdict(deepseek_roles.role_config("outer_planning", version=17)),
            "inner_role": asdict(deepseek_roles.role_config("inner_action", version=17)),
            "d4_budget": asdict(D8HandoffBudgetPolicy()),
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    raw = build(argv[0], argv[1])
    # 与 CLI 从磁盘读取时的形态保持一致（元组会变成列表）
    raw = json.loads(json.dumps(raw))
    from agent_adapters.simulation import parse_request

    try:
        parse_request(raw)
    except Exception as exc:  # noqa: BLE001 - 构造期校验，直接暴露原因
        print(f"请求校验失败: {exc}")
        return 1
    Path(argv[2]).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
