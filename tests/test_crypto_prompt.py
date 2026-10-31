"""Prompt-level checks for the crypto tool routing guidance."""

import asyncio
from types import SimpleNamespace

from agent_core.stage_planning import (
    CRYPTO_TOOL_GUIDANCE,
    OFFLINE_TOOLING_GUIDANCE,
    crypto_tool_guidance,
)
from MCPs.Crypto_Mcp.server import mcp


def test_crypto_guidance_is_injected_only_for_action_with_crypto_tools():
    crypto = SimpleNamespace(
        stage="stage_action",
        tools=[{"qualified_name": "crypto.pari"}, {"qualified_name": "shell.run_bash"}],
    )
    assert crypto_tool_guidance(crypto) == CRYPTO_TOOL_GUIDANCE
    assert "crypto.lll" in CRYPTO_TOOL_GUIDANCE
    assert "bash script" in CRYPTO_TOOL_GUIDANCE

    assert crypto_tool_guidance(SimpleNamespace(stage="stage_action", tools=[])) == ""
    assert crypto_tool_guidance(
        SimpleNamespace(stage="stage_review", tools=[{"qualified_name": "crypto.pari"}])
    ) == ""
    assert crypto_tool_guidance(
        SimpleNamespace(stage="stage_action", tools=[{"qualified_name": "shell.run_bash"}])
    ) == ""


def test_offline_inventory_matches_the_crypto_image():
    assert "sympy" in OFFLINE_TOOLING_GUIDANCE
    assert "fpylll" in OFFLINE_TOOLING_GUIDANCE
    assert "gp (PARI/GP)" in OFFLINE_TOOLING_GUIDANCE
    assert "binwalk" in OFFLINE_TOOLING_GUIDANCE
    assert "unzip, zip, xxd" not in OFFLINE_TOOLING_GUIDANCE


def test_crypto_tools_describe_their_use_cases():
    tools = {tool.name: tool.description for tool in asyncio.run(mcp.list_tools())}

    assert "PARI/GP" in tools["pari"]
    assert "factorint" in tools["sympy"]
    assert "LLL" in tools["lll"] and "Coppersmith" in tools["lll"]
