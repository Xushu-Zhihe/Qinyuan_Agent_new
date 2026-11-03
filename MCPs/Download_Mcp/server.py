from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from MCPs.analysis_common import MAX_ARGUMENT_BYTES, validate_timeout
from MCPs.fastmcp_compat import FastMCP
from Sandbox.env_settings import configured as configured_operational
from Sandbox.tool_usage import record as record_tool_download


_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,63}")
_MODULE_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_ARCHIVE_FORMATS = frozenset({"raw", "zip", "tar", "tar.gz", "tar.xz"})
_RESERVED_NAMES = frozenset({
    "sh", "bash", "dash", "python", "python3", "pip", "pip3", "timeout",
    "sleep", "curl", "wget", "nmap", "gdb", "file", "readelf", "objdump",
    "nm", "strings", "php", "pwsh", "powershell", "tar", "unzip", "zip",
    "subfinder", "amass", "httpx", "ffuf", "dirsearch", "nuclei", "jadx",
})
_DEFAULT_BOOTSTRAP_TIMEOUT = 300


mcp = FastMCP(
    "qinyuan-download-command-planner",
    instructions=(
        "Prepare bounded tool installation commands for the current Agent "
        "sandbox. The current TaskRun prepares verified artifacts through its controlled fetcher, "
        "then installs locally under /tools without direct Internet access; "
        "when native installation fails, a pinned Python package fallback may "
        "be attempted. This server never downloads to or executes on the host. "
        "Only use sources authorized by the challenge and provide a SHA256 for "
        "native artifacts."
    ),
    json_response=True,
)


def _configured_timeout() -> int:
    raw = configured_operational("QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS")
    if raw is None:
        raw = str(_DEFAULT_BOOTSTRAP_TIMEOUT)
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if not 5 <= value <= 300:
        raise ValueError(
            "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS must be between 5 and 300"
        )
    return value


def _effective_timeout(value: int | None) -> int:
    configured = _configured_timeout()
    if value is None:
        return configured
    requested = validate_timeout(value)
    return min(requested, configured)


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or _NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe non-empty name")
    if value.lower() in _RESERVED_NAMES:
        raise ValueError(f"{label} must not shadow a platform executable")
    return value


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("url must be a non-empty URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("url must use HTTPS")
    return value


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return value.lower()


def _validate_archive_format(value: str) -> str:
    if value not in _ARCHIVE_FORMATS:
        raise ValueError(f"archive_format must be one of {sorted(_ARCHIVE_FORMATS)}")
    return value


def _validate_args(value: list[str], label: str = "self_test_args") -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError(f"{label} must contain at most 8 strings")
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError(f"{label} must contain non-empty strings")
        if len(item.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError(f"{label} contains an argument that is too long")
    return value


def _validate_optional_package(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _PACKAGE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe package name")
    return value


def _validate_optional_version(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("python_version must be a pinned package version")
    return value


def _validate_module(value: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if _MODULE_PATTERN.fullmatch(value) is None:
        raise ValueError("python_module must be a dotted Python module name")
    return value


def _script(**values: object) -> str:
    from MCPs.Download_Mcp.install_script import render
    return render(values)


def _bootstrap_plan(script: str, timeout_seconds: int) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": ["-c", script],
            "timeout_seconds": timeout_seconds,
        },
        "request": {"operation": "tool_bootstrap"},
    }


_TOOL_CATALOG = {
    "policy": {
        "max_native_bytes": 240 * 1024 * 1024,
        "tools_tmpfs_bytes": 256 * 1024 * 1024,
        "python_version": "11",
        "note": "Only listed recipes are supported; unsupported entries are capability gaps, not retry targets.",
    },
    "native": [
        {
            "name": "upx",
            "url": "https://github.com/upx/upx/releases/download/v5.3.0/upx-5.3.0-amd64_linux.tar.xz",
            "sha256": "79cea8a50b6fa102d10c7b79a419cd77dead3cc2d78661de2dc5e5815dc505c1",
            "archive_format": "tar.xz",
            "executable_path": "upx-5.3.0-amd64_linux/upx",
            "self_test_args": ["--version"],
        },
    ],
    "python": [
        {"name": "pyelftools", "python_package": "pyelftools", "python_version": "11", "python_module": "elftools",
         "self_test_args": ["-c", "from elftools.elf.elffile import ELFFile; print('ok')"]},
        {"name": "lief", "python_package": "lief", "python_version": "11", "python_module": "lief",
         "self_test_args": ["-c", "import lief; print(lief.__version__)"]},
        {"name": "unicorn", "python_package": "unicorn", "python_version": "11", "python_module": "unicorn",
         "self_test_args": ["-c", "import unicorn; print(unicorn.__version__)"]},
        {"name": "keystone", "python_package": "keystone-engine", "python_version": "11", "python_module": "keystone",
         "self_test_args": ["-c", "import keystone; print(keystone.__version__)"]},
        {"name": "ropper", "python_package": "ropper", "python_version": "11", "python_module": "ropper",
         "self_test_args": ["-c", "import ropper; print('ok')"]},
        {"name": "ropgadget", "python_package": "ROPgadget", "python_version": "11", "python_module": "ropgadget",
         "self_test_args": ["-c", "import ropgadget; print('ok')"]},
        {"name": "volatility3", "python_package": "volatility3", "python_version": "11", "python_module": "volatility3",
         "self_test_args": ["-c", "import volatility3; print('ok')"]},
        {"name": "oletools", "python_package": "oletools", "python_version": "11", "python_module": "oletools",
         "self_test_args": ["-c", "import oletools; print('ok')"]},
        {"name": "impacket", "python_package": "impacket", "python_version": "11", "python_module": "impacket",
         "self_test_args": ["-c", "import impacket; print(impacket.__version__)"]},
        {"name": "pypykatz", "python_package": "pypykatz", "python_version": "11", "python_module": "pypykatz",
         "self_test_args": ["-c", "import pypykatz; print('ok')"]},
        {"name": "frida-tools", "python_package": "frida-tools", "python_version": "11", "python_module": "frida_tools",
         "self_test_args": ["-c", "import frida_tools; print('ok')"]},
        {"name": "angr", "python_package": "angr", "python_version": "11", "python_module": "angr",
         "self_test_args": ["-c", "import angr; print(angr.__version__)"]},
    ],
    "manual": [
        {
            "name": "apktool",
            "purpose": "APK decode/rebuild",
            "url": "https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar",
            "sha256": "dbf930b076c6b9be08d57c449cacefc3bdd6b71ebd59b3066fc0e1f5b14f9423",
            "note": "jar is not an executable; download it, then run: java -jar apktool_3.0.3.jar",
        },
    ],
    "unsupported": [
        {"name": "ghidra", "reason": "official zip exceeds 240 MiB download cap and needs a JDK; image change required"},
        {"name": "radare2", "reason": "release has no Linux static executable; source build required"},
        {"name": "rizin", "reason": "static archive is 137 MiB and extraction exceeds the 256 MiB /tools tmpfs"},
        {"name": "blutter", "reason": "requires Dart SDK and a build step; not a single artifact"},
        {"name": "reflutter", "reason": "requires Frida and a patched engine; not a single artifact"},
        {"name": "frida-server", "reason": "device/architecture-specific; push to the target, not local install"},
        {"name": "dnspy/ilspycmd", "reason": "no dotnet SDK/runtime in the image"},
        {"name": "tshark/tcpdump/exiftool/steghide/zsteg", "reason": "apt/multi-file/Perl dependencies; image change required"},
        {"name": "metasploit/john/hashcat/hydra", "reason": "build/runtime/dictionary dependencies; not a single artifact"},
        {"name": "sqlmap/nikto/whatweb", "reason": "scripts/multi-file with no stable release digest; image change required"},
    ],
}


@mcp.tool(title="List On-Demand Tool Catalog")
def list_catalog() -> dict[str, Any]:
    """Return supported on-demand tool recipes and known-unsupported tools."""
    return _TOOL_CATALOG


@mcp.tool(title="Install Native Tool With Python Fallback")
def install_tool(
    name: str,
    url: str | None = None,
    sha256: str | None = None,
    archive_format: str = "raw",
    executable_path: str = "",
    self_test_args: list[str] | None = None,
    python_package: str | None = None,
    python_version: str | None = None,
    python_module: str = "",
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Install a verified native artifact, then a pinned Python fallback.

    Supply either url+sha256 (native) or python_package+python_version
    (fallback); python_package alone is rejected. archive_format accepts raw,
    zip, tar, tar.gz or tar.xz.
    """

    normalized_name = _validate_name(name, "name")
    normalized_url = _validate_url(url) if url is not None else None
    normalized_sha = _validate_sha256(sha256) if sha256 is not None else None
    if normalized_url is not None and normalized_sha is None:
        raise ValueError("sha256 is required when url is provided")
    if normalized_url is None and normalized_sha is not None:
        raise ValueError("url is required when sha256 is provided")
    normalized_format = _validate_archive_format(archive_format)
    if normalized_format != "raw" and not executable_path:
        raise ValueError("executable_path is required for archives")
    if "\x00" in executable_path or len(executable_path.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise ValueError("executable_path is invalid or too long")
    normalized_python_package = _validate_optional_package(
        python_package, "python_package"
    )
    normalized_python_version = _validate_optional_version(python_version)
    normalized_python_module = _validate_module(python_module)
    if (normalized_python_package is None) != (normalized_python_version is None):
        raise ValueError("python_package and python_version must be provided together")
    if not normalized_url and not normalized_python_package:
        raise ValueError("native URL or Python fallback must be provided")
    normalized_args = _validate_args([] if self_test_args is None else self_test_args)
    timeout = _effective_timeout(timeout_seconds)
    plan = _bootstrap_plan(
        _script(
            name=normalized_name,
            url=normalized_url or "",
            sha256=normalized_sha or "",
            archive_format=normalized_format,
            executable_path=executable_path,
            self_test_args=normalized_args,
            python_package=normalized_python_package or "",
            python_version=normalized_python_version or "",
            python_module=normalized_python_module,
            timeout_seconds=timeout,
        ),
        timeout,
    )
    record_tool_download(normalized_name)
    return plan


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
