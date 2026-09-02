from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from MCPs.analysis_common import MAX_ARGUMENT_BYTES, validate_timeout
from MCPs.fastmcp_compat import FastMCP


_SYMBOL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OFFSET_PATTERN = re.compile(r"0[xX][0-9a-fA-F]+|[0-9]+")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")


mcp = FastMCP(
    "qinyuan-libc-database-command-planner",
    instructions=(
        "Query libc.rip for leaked libc symbol offsets or a libc identifier. "
        "Use this as an online fallback after checking any attached libc file "
        "and the local libcsearcher MCP. "
        "The endpoint is fixed and the request runs through curl inside the "
        "current Agent sandbox; this server never downloads Ubuntu packages."
    ),
    json_response=True,
)


def _validate_symbol(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES
    ):
        raise ValueError("symbol must be a non-empty name")
    if "\x00" in value or _SYMBOL_PATTERN.fullmatch(value) is None:
        raise ValueError("symbol contains invalid characters")
    return value


def _validate_offset(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES
    ):
        raise ValueError("offset must be a non-empty value")
    if "\x00" in value or _OFFSET_PATTERN.fullmatch(value) is None:
        raise ValueError("offset must be a decimal or hexadecimal value")
    return value


def _validate_identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES
    ):
        raise ValueError("identifier must be a non-empty value")
    if "\x00" in value or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError("identifier contains invalid characters")
    return value


def _curl_timeout(timeout_seconds: int) -> tuple[int, str]:
    timeout = validate_timeout(timeout_seconds)
    return timeout, str(timeout)


@mcp.tool(title="Find Matching Libc")
def find_libc(symbols: dict[str, str], timeout_seconds: int = 60) -> dict[str, Any]:
    """Find entries matching leaked symbol addresses or offsets."""

    if not isinstance(symbols, dict) or not 1 <= len(symbols) <= 32:
        raise ValueError("symbols must contain between 1 and 32 entries")
    normalized_symbols = [
        (_validate_symbol(symbol), _validate_offset(offset))
        for symbol, offset in symbols.items()
    ]
    timeout, curl_timeout = _curl_timeout(timeout_seconds)
    payload = json.dumps(
        {"symbols": dict(normalized_symbols)},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    arguments = [
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        curl_timeout,
        "--header",
        "Content-Type: application/json",
        "--request",
        "POST",
        "--data-binary",
        payload,
        "https://libc.rip/api/find",
    ]
    return {
        "sandbox_command": {
            "tool": "curl",
            "arguments": arguments,
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "find",
            "symbol_count": len(normalized_symbols),
        },
    }


@mcp.tool(title="Dump Libc Database Entry")
def dump_libc(identifier: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """Print metadata and paths for one libc-database identifier."""

    normalized_identifier = _validate_identifier(identifier)
    timeout, curl_timeout = _curl_timeout(timeout_seconds)
    encoded_identifier = quote(normalized_identifier, safe="")
    return {
        "sandbox_command": {
            "tool": "curl",
            "arguments": [
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                curl_timeout,
                f"https://libc.rip/api/dump?id={encoded_identifier}",
            ],
            "timeout_seconds": timeout,
        },
        "request": {
            "operation": "dump",
            "identifier": normalized_identifier,
        },
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
