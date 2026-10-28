from __future__ import annotations

import json
import re
from typing import Any

from MCPs.analysis_common import MAX_ARGUMENT_BYTES, python_command, validate_timeout
from MCPs.fastmcp_compat import FastMCP


LIBCSEARCHER_INSTRUCTIONS = (
    "Use this local tool only when ELF/Pwn/glibc analysis needs libc symbol "
    "offset matching. It matches leaked addresses by symbol offsets and may "
    "return multiple possible libc versions; candidates are not guaranteed "
    "to be exact and can be tried or narrowed with additional leaks. If the "
    "challenge supplies a libc file under /workspace/input, inspect and use that "
    "file as authoritative before relying on this database. If there is no "
    "local match, use the existing libc_database MCP for the online libc.rip "
    "fallback. This server never performs network requests or downloads."
)
_SYMBOL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


mcp = FastMCP(
    "qinyuan-libcsearcher-command-planner",
    instructions=LIBCSEARCHER_INSTRUCTIONS,
    json_response=True,
)


def _validate_symbols(symbols: dict[str, str]) -> dict[str, str]:
    if not isinstance(symbols, dict) or not 1 <= len(symbols) <= 32:
        raise ValueError("symbols must contain between 1 and 32 entries")
    normalized: dict[str, str] = {}
    for name, value in symbols.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name.encode("utf-8")) > MAX_ARGUMENT_BYTES
            or _SYMBOL_PATTERN.fullmatch(name) is None
        ):
            raise ValueError("symbol names must contain only letters, digits, and underscores")
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError("symbol offsets must be integers or strings")
        text = str(value).strip()
        if not text or len(text.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("symbol offsets must be non-empty values")
        try:
            int(text, 0)
        except ValueError as exc:
            raise ValueError("symbol offsets must be decimal or hexadecimal values") from exc
        normalized[name] = text
    return normalized


@mcp.tool(title="Find Local Libc Candidates")
def find_candidates(symbols: dict[str, str], timeout_seconds: int = 60) -> dict[str, Any]:
    """Find one or more local libc candidates from leaked symbol addresses."""

    normalized = _validate_symbols(symbols)
    timeout = validate_timeout(timeout_seconds)
    script = (
        "import json; "
        "from qinyuan_libcsearcher import find_candidates; "
        f"print(json.dumps(find_candidates({json.dumps(normalized, ensure_ascii=True)}), "
        "ensure_ascii=True, sort_keys=True))"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "local_libc_candidate_match",
            "symbol_count": len(normalized),
            "candidate_semantics": "multiple_candidates_possible",
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
