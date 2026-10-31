from __future__ import annotations

import json
from typing import Any

from MCPs.analysis_common import (
    needs_stage,
    python_command,
    stage_python_prefix,
    staged_path,
    validate_sandbox_path,
    validate_timeout,
)
from MCPs.fastmcp_compat import FastMCP


mcp = FastMCP(
    "qinyuan-pe-command-planner",
    instructions=(
        "Prepare read-only PE inspection scripts for the current Agent sandbox. "
        "The scripts use pefile and capstone inside the container; artifacts "
        "below /workspace/input are staged to an executable path first. This "
        "server never reads files or executes Python itself."
    ),
    json_response=True,
)

_MAX_IMPORTS = 200
_MAX_STRINGS = 200
_MAX_DISASM = 64


def _target(path: str) -> str:
    normalized = validate_sandbox_path(path)
    if needs_stage(normalized):
        return staged_path(normalized)
    return normalized


def _prelude(path: str) -> str:
    source = validate_sandbox_path(path)
    target = _target(path)
    if needs_stage(source):
        return stage_python_prefix(source, target)
    return f"_qy_dst = {target!r}\n"


def _header() -> str:
    return (
        "import json\n"
        "import pefile\n"
        "def _flags(value):\n"
        "    return [name for bit, name in (\n"
        "        (0x00000020, 'CNT_CODE'), (0x00000040, 'CNT_INITIALIZED_DATA'),\n"
        "        (0x00000080, 'CNT_UNINITIALIZED_DATA'), (0x20000000, 'MEM_EXECUTE'),\n"
        "        (0x40000000, 'MEM_READ'), (0x80000000, 'MEM_WRITE')) if value & bit]\n"
    )


def _json_print() -> str:
    return "print(json.dumps(result, sort_keys=True))\n"


@mcp.tool(title="Inspect PE Headers")
def inspect_pe(path: str, timeout_seconds: int = 30) -> dict[str, Any]:
    """Return headers, sections, imports, exports and mitigations as JSON."""

    target = _target(path)
    timeout = validate_timeout(timeout_seconds)
    script = (
        _prelude(path)
        + _header()
        + f"_path = {target!r}\n"
        "pe = pefile.PE(_path, fast_load=True)\n"
        "pe.parse_data_directories(directories=[\n"
        "    pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],\n"
        "    pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT'],\n"
        "    pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_LOAD_CONFIG']])\n"
        "oh = pe.OPTIONAL_HEADER\n"
        "dll = oh.DllCharacteristics\n"
        "result = {\n"
        "    'path': _path,\n"
        "    'machine': hex(pe.FILE_HEADER.Machine),\n"
        "    'subsystem': oh.Subsystem,\n"
        "    'image_base': hex(oh.ImageBase),\n"
        "    'entry_point': hex(oh.AddressOfEntryPoint),\n"
        "    'size_of_image': oh.SizeOfImage,\n"
        "    'checksum': hex(oh.CheckSum),\n"
        "    'dll_characteristics': hex(dll),\n"
        "    'mitigations': {\n"
        "        'aslr': bool(dll & 0x40),\n"
        "        'dep': bool(dll & 0x100),\n"
        "        'cfg': bool(dll & 0x4000),\n"
        "        'high_entropy_va': bool(dll & 0x20),\n"
        "        'no_seh': bool(dll & 0x400),\n"
        "    },\n"
        "    'sections': [\n"
        "        {'name': s.Name.rstrip(b'\\x00').decode('latin-1'),\n"
        "         'va': hex(s.VirtualAddress), 'vsize': s.Misc_VirtualSize,\n"
        "         'raw_size': s.SizeOfRawData, 'flags': _flags(s.Characteristics)}\n"
        "        for s in pe.sections],\n"
        "}\n"
        "imports = []\n"
        "count = 0\n"
        "for entry in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []):\n"
        "    functions = []\n"
        "    for item in entry.imports:\n"
        "        if count >= 200: break\n"
        "        name = item.name.decode('latin-1') if item.name else None\n"
        "        functions.append(name or ('ordinal:%d' % item.ordinal))\n"
        "        count += 1\n"
        "    imports.append({'dll': entry.dll.decode('latin-1'), 'functions': functions})\n"
        "    if count >= 200: break\n"
        "result['imports'] = imports\n"
        "exports = []\n"
        "for entry in getattr(pe, 'DIRECTORY_ENTRY_EXPORT', []):\n"
        "    for symbol in entry.symbols[:200]:\n"
        "        name = symbol.name.decode('latin-1') if symbol.name else None\n"
        "        exports.append(name or ('ordinal:%d' % symbol.ordinal))\n"
        "result['exports'] = exports[:200]\n"
        "pe.close()\n"
        + _json_print()
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pe_headers", "path": target},
    }


@mcp.tool(title="Extract PE Strings")
def pe_strings(
    path: str,
    min_length: int = 4,
    limit: int = 200,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Extract printable ASCII strings, bounded by count and length."""

    target = _target(path)
    timeout = validate_timeout(timeout_seconds)
    if isinstance(min_length, bool) or not isinstance(min_length, int) or not 4 <= min_length <= 32:
        raise ValueError("min_length must be between 4 and 32")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_STRINGS:
        raise ValueError(f"limit must be between 1 and {_MAX_STRINGS}")
    script = (
        _prelude(path)
        + "import json, re\n"
        + f"_path = {target!r}\n"
        + f"_pattern = re.compile(rb'[\\x20-\\x7e]{{{min_length},}}')\n"
        + "data = open(_path, 'rb').read()\n"
        + f"values = [m.group().decode('latin-1') for m in _pattern.finditer(data)][:{limit}]\n"
        + "print(json.dumps({'path': _path, 'count': len(values), 'strings': values}, sort_keys=True))\n"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pe_strings", "path": target, "min_length": min_length, "limit": limit},
    }


@mcp.tool(title="Disassemble PE At RVA")
def pe_disassemble(
    path: str,
    rva: int,
    count: int = 32,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Disassemble up to 64 instructions starting at a PE RVA."""

    target = _target(path)
    timeout = validate_timeout(timeout_seconds)
    if isinstance(rva, bool) or not isinstance(rva, int) or not 0 <= rva <= 0xFFFFFFFF:
        raise ValueError("rva must be an unsigned 32-bit integer")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= _MAX_DISASM:
        raise ValueError(f"count must be between 1 and {_MAX_DISASM}")
    script = (
        _prelude(path)
        + "import json\n"
        + "import pefile\n"
        + "from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_MODE_32\n"
        + f"_path = {target!r}\n"
        + "pe = pefile.PE(_path, fast_load=True)\n"
        + f"offset = pe.get_offset_from_rva({rva})\n"
        + "data = open(_path, 'rb').read()[offset:offset + 512]\n"
        + "mode = CS_MODE_64 if pe.FILE_HEADER.Machine == 0x8664 else CS_MODE_32\n"
        + "md = Cs(CS_ARCH_X86, mode)\n"
        + f"rows = [{{'address': hex(i.address), 'mnemonic': i.mnemonic, 'op_str': i.op_str}} for i in md.disasm(data, {rva})][:{count}]\n"
        + "pe.close()\n"
        + f"payload = {{'path': _path, 'rva': hex({rva}), 'instructions': rows}}\n"
        + "print(json.dumps(payload, sort_keys=True))\n"
    )
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": python_command(script),
            "timeout_seconds": timeout,
        },
        "request": {"operation": "pe_disassemble", "path": target, "rva": rva, "count": count},
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
