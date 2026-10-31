"""Catalog ordering: dedicated tools first, utilities next, generic last."""

import pytest

from agent_adapters.catalog_order import (
    CATEGORIES,
    DEDICATED,
    UTILITY_TOOLS,
    category_from_goal,
    order_tools,
)


def _tools():
    names = []
    for category in CATEGORIES:
        for server in DEDICATED[category]:
            names.append(f"{server}.tool_{server}")
    names.extend(["download.install_tool", "web.search", "web.fetch"])
    names.extend(["shell.run_bash", "unknown.tool"])
    return [{"qualified_name": name} for name in dict.fromkeys(names)]


@pytest.mark.parametrize("category", CATEGORIES)
def test_dedicated_then_utilities_then_generic(category):
    tools = _tools()
    ordered = order_tools(tools, category)
    assert len(ordered) == len(tools)
    assert {t["qualified_name"] for t in ordered} == {t["qualified_name"] for t in tools}

    names = [t["qualified_name"] for t in ordered]
    dedicated_names = {
        f"{server}.tool_{server}" for server in DEDICATED[category]
    }
    dedicated_positions = [names.index(n) for n in dedicated_names if n in names]
    utility_positions = [names.index(n) for n in UTILITY_TOOLS]
    generic_positions = [names.index(n) for n in ("shell.run_bash", "unknown.tool")]

    assert dedicated_positions
    assert max(dedicated_positions) < min(utility_positions)
    assert max(utility_positions) < min(generic_positions)


@pytest.mark.parametrize("category", CATEGORIES)
def test_category_goals_are_parsed(category):
    goal = f"题型（任务提供）：{category}\n将题型作为可依据证据修正的分析背景"
    assert category_from_goal(goal) == category


def test_unknown_or_missing_category_falls_back_to_other():
    assert category_from_goal("no prefix") == "other"
    assert category_from_goal(None) == "other"
    assert category_from_goal("题型（任务提供）：blockchain") == "other"


def test_utility_order_is_download_then_search_then_fetch():
    tools = [{"qualified_name": name} for name in reversed(UTILITY_TOOLS)]
    tools.append({"qualified_name": "shell.run_bash"})
    ordered = [t["qualified_name"] for t in order_tools(tools, "misc")]
    assert ordered == [*UTILITY_TOOLS, "shell.run_bash"]


def test_web_http_experiment_is_dedicated_before_utilities():
    tools = [
        {"qualified_name": "shell.run_bash"},
        {"qualified_name": "download.install_tool"},
        {"qualified_name": "http.run_experiment"},
        {"qualified_name": "web.search"},
        {"qualified_name": "nmap.run_nmap_tcp_scan"},
    ]
    ordered = [t["qualified_name"] for t in order_tools(tools, "web")]
    assert ordered == [
        "nmap.run_nmap_tcp_scan",
        "http.run_experiment",
        "download.install_tool",
        "web.search",
        "shell.run_bash",
    ]


def test_pwn_and_crypto_specialised_prefixes_exist():
    pwn = [t["qualified_name"] for t in order_tools(_tools(), "pwn")]
    assert pwn[0].startswith("pwntools.")
    crypto = [t["qualified_name"] for t in order_tools(_tools(), "crypto")]
    assert crypto[0].startswith("crypto.")


def test_category_set_matches_the_workbench_taxonomy():
    from agent_adapters.workbench import VALID_CATEGORIES

    assert set(CATEGORIES) == VALID_CATEGORIES


def test_every_dedicated_prefix_is_a_known_tool_family():
    import json
    from pathlib import Path

    registry = json.loads(
        (Path(__file__).resolve().parents[1] / "MCPs" / "Mcp_client" / "registry.json").read_text(
            encoding="utf-8"
        )
    )
    known = set(registry["mcpServers"]) | {"http", "web"}
    for category, prefixes in DEDICATED.items():
        assert prefixes, category
        for prefix in prefixes:
            assert prefix in known, f"{category}: unknown prefix {prefix}"

