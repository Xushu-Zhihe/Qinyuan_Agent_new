"""Small, non-interactive LibcSearcher-compatible matcher for the sandbox."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


INDEX_PATH = Path(__file__).with_name("qinyuan_libcsearcher_index.json")


def _address(value: str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("symbol offsets must be integers or strings")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("symbol offsets must be integers or strings")
    return int(value.strip(), 0)


def _load_entries() -> list[dict[str, Any]]:
    with INDEX_PATH.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("libcsearcher index has no entries")
    return [entry for entry in entries if isinstance(entry, dict)]


def find_candidates(symbols: dict[str, str | int]) -> dict[str, Any]:
    """Match leaked addresses by the low 12 bits used by LibcSearcher."""

    if not isinstance(symbols, dict) or not symbols:
        raise ValueError("symbols must contain at least one leaked symbol")

    normalized = {name: _address(value) for name, value in symbols.items()}
    entries = _load_entries()
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        entry_symbols = entry.get("symbols", {})
        if not isinstance(entry_symbols, dict):
            continue
        matched = True
        for name, leaked in normalized.items():
            offset = entry_symbols.get(name)
            if not isinstance(offset, str) or (_address(offset) & 0xFFF) != (leaked & 0xFFF):
                matched = False
                break
        if not matched:
            continue
        candidates.append(
            {
                "id": entry.get("id"),
                "arch": entry.get("arch"),
                "family": entry.get("family"),
                "buildid": entry.get("buildid"),
                "sha256": entry.get("sha256"),
                "download_url": entry.get("download_url"),
                "symbols": {
                    name: entry_symbols[name]
                    for name in ("system", "str_bin_sh", "puts", "printf", "read", "write")
                    if name in entry_symbols
                },
            }
        )

    return {
        "source": "local_libcsearcher",
        "status": "ok" if candidates else "no_match",
        "exact": False,
        "unique_candidate": len(candidates) == 1,
        "ambiguous": len(candidates) > 1,
        "matched_symbol_count": len(normalized),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "next_step": (
            "Use the attached libc file as authoritative when available."
            if candidates
            else "Try additional leaked symbols or the online libc_database MCP."
        ),
    }
