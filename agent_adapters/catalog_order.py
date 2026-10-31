"""Order the frozen tool catalog by challenge category.

Evidence for the mapping:
- category values: ``agent_adapters.workbench.VALID_CATEGORIES``;
- category source: ``workbench_runner.challenge_goal`` writes
  ``题型（任务提供）：<category>`` as the first line of the TaskRun goal;
- server ownership: ``MCPs/Mcp_client/registry.json`` tags and server names.

The order is: category-specific tools, then the three cross-category utility
tools (download.install_tool, web.search, web.fetch), then everything else.
Keeping utilities immediately after the dedicated block makes them visible
without burying them at the end of a 60+ tool catalog.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

CATEGORIES = ("pwn", "reverse", "web", "misc", "crypto", "other")

# Server/tool prefixes that are specific to one category, in display order.
DEDICATED: dict[str, tuple[str, ...]] = {
    "pwn": ("pwntools", "gdb", "libcsearcher", "libc_database", "binutils", "kernel", "session", "file"),
    "reverse": ("binutils", "file", "jadx", "pe", "gdb", "session", "wine"),
    "web": ("nmap", "subfinder", "amass", "httpx", "ffuf", "dirsearch", "nuclei", "curl", "php", "http"),
    "crypto": ("crypto", "pycryptodome", "z3"),
    "misc": ("file", "binutils", "session"),
    "other": ("file", "binutils"),
}

# Cross-category utilities; they go after the dedicated block but before the
# remaining generic tools.
UTILITY_TOOLS = ("download.install_tool", "web.search", "web.fetch")

_CATEGORY = re.compile(r"^题型（任务提供）：\s*([A-Za-z]+)")
_ALIASES = {"re": "reverse", "rev": "reverse", "pwnable": "pwn", "web-security": "web"}


def category_from_goal(goal: object) -> str:
    """Read the category prefix written by ``challenge_goal``; default other."""

    if not isinstance(goal, str):
        return "other"
    match = _CATEGORY.match(goal.lstrip())
    if match is None:
        return "other"
    value = _ALIASES.get(match.group(1).lower(), match.group(1).lower())
    return value if value in DEDICATED else "other"


def _qualified(tool: Mapping[str, Any]) -> str:
    return str(tool.get("qualified_name", ""))


def order_tools(tools: Iterable[Mapping[str, Any]], category: str) -> list[Mapping[str, Any]]:
    """Stable sort: dedicated, utilities, generic; never drops or adds tools."""

    items = list(tools)
    dedicated = DEDICATED.get(category, DEDICATED["other"])
    server_rank = {server: index for index, server in enumerate(dedicated)}
    utility_rank = {name: index for index, name in enumerate(UTILITY_TOOLS)}

    def key(pair: tuple[int, Mapping[str, Any]]):
        index, tool = pair
        name = _qualified(tool)
        if name in utility_rank:
            return (1, utility_rank[name], index)
        server = name.split(".", 1)[0]
        if server in server_rank:
            return (0, server_rank[server], index)
        return (2, 0, index)

    return [tool for _, tool in sorted(enumerate(items), key=key)]
