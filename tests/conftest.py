"""测试分层。

三层，按「这个文件需要什么才能跑」判定，不按耗时猜：

    smoke  核心不变量，日常改动先跑这套（秒级）
    live   需要 Docker / 真实网络，默认跳过
    其余   普通单元与进程内集成

用法：

    pytest -m smoke        # 快速自检
    pytest                 # 默认：除 live 之外全部
    pytest -m live         # 只跑需要外部依赖的
    pytest -m ""           # 全量

注意：全量目前约 2400 个用例、20 分钟量级；日常不建议跑全量。
"""

from __future__ import annotations

import re

import pytest

# 需要 Docker、真实网络或真实容器的测试模块（文件名已去版本化）
LIVE_MODULES = frozenset(
    {
        "test_sandbox_http",
        "test_network",
        "test_task_run_sandbox",
    }
)

# 端到端：会拉起完整 CLI 子进程再跑一整轮，单个 4–5 秒
SLOW_NAME = re.compile(r"(child|launcher|subprocess|spawn)", re.IGNORECASE)

# 核心不变量：错误分类、领域模型、控制存储、候选队列、多轮链条
SMOKE_MODULES = frozenset(
    {
        "test_qinyuan_core_errors",
        "test_qinyuan_core_logging",
        "test_core_round1",
        "test_control_store",
        "test_pending_candidates",
        "test_chain_multi_turn",
        "test_actions",
        "test_controller",
    }
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: 需要 Docker / 真实网络等外部依赖")
    config.addinivalue_line("markers", "slow: 端到端，会拉起完整 CLI 子进程")
    config.addinivalue_line("markers", "smoke: 核心不变量，日常快速自检")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        module = getattr(item, "module", None)
        if module is None:
            continue
        name = module.__name__
        if name in LIVE_MODULES:
            item.add_marker(pytest.mark.live)
        if name in SMOKE_MODULES:
            item.add_marker(pytest.mark.smoke)
        if SLOW_NAME.search(item.name):
            item.add_marker(pytest.mark.slow)
