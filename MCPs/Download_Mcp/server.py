from __future__ import annotations

import json
import os
import re
from textwrap import dedent
from typing import Any
from urllib.parse import urlsplit

from MCPs.analysis_common import MAX_ARGUMENT_BYTES, validate_timeout
from MCPs.fastmcp_compat import FastMCP


_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,63}")
_MODULE_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_ARCHIVE_FORMATS = frozenset({"raw", "zip", "tar", "tar.gz", "tar.xz"})
_DEFAULT_BOOTSTRAP_TIMEOUT = 300
_BOOTSTRAP_BUDGET = 300


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
    raw = os.environ.get(
        "QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS",
        str(_DEFAULT_BOOTSTRAP_TIMEOUT),
    )
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
    encoded = {key: json.dumps(value, ensure_ascii=True) for key, value in values.items()}
    script = dedent(
        f"""
        import hashlib
        import json
        import os
        import shutil
        import subprocess
        import sys
        import tarfile
        import tempfile
        import time
        import urllib.request
        import zipfile
        from pathlib import Path, PurePosixPath

        NAME = {encoded['name']}
        URL = {encoded['url']}
        SHA256 = {encoded['sha256']}
        ARCHIVE_FORMAT = {encoded['archive_format']}
        EXECUTABLE_PATH = {encoded['executable_path']}
        SELF_TEST_ARGS = {encoded['self_test_args']}
        PYTHON_PACKAGE = {encoded['python_package']}
        PYTHON_VERSION = {encoded['python_version']}
        PYTHON_MODULE = {encoded['python_module']}
        TIMEOUT = {encoded['timeout_seconds']}
        MAX_BYTES = 256 * 1024 * 1024
        ROOT = Path('/tools')
        OFFLINE = os.environ.get('QINYUAN_OFFLINE_TOOL_INSTALL') == '1'
        WHEELHOUSE = os.environ.get('QINYUAN_TOOL_WHEELHOUSE', '')
        WHEEL_HASHES = json.loads(os.environ.get('QINYUAN_TOOL_WHEEL_HASHES', '{{}}'))
        DEADLINE = time.monotonic() + TIMEOUT

        def remaining():
            value = DEADLINE - time.monotonic()
            if value <= 0:
                raise TimeoutError('tool bootstrap budget exhausted')
            return value

        def safe_relative(value):
            path = PurePosixPath(value.replace('\\\\', '/'))
            if path.is_absolute() or '..' in path.parts or not path.parts:
                raise ValueError('archive contains an unsafe path')
            return Path(*path.parts)

        def extract_zip(source, destination):
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    relative = safe_relative(member.filename)
                    target = destination / relative
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                        raise ValueError('archive symlinks are not allowed')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source_file, target.open('wb') as target_file:
                        shutil.copyfileobj(source_file, target_file)

        def extract_tar(source, destination):
            with tarfile.open(source, 'r:*') as archive:
                for member in archive.getmembers():
                    relative = safe_relative(member.name)
                    target = destination / relative
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise ValueError('archive links and special files are not allowed')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        raise ValueError('archive member cannot be read')
                    with source_file, target.open('wb') as target_file:
                        shutil.copyfileobj(source_file, target_file)

        def run_self_test(path):
            if not SELF_TEST_ARGS:
                return
            result = subprocess.run(
                [str(path), *SELF_TEST_ARGS],
                cwd=str(path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=min(15, remaining()),
                check=False,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError('native tool self-test failed')

        def install_native():
            if not URL or not SHA256:
                raise ValueError('native installation requires URL and SHA256')
            ROOT.mkdir(parents=True, exist_ok=True)
            staging = ROOT / ('.download-' + NAME)
            install_root = ROOT / 'apps' / NAME
            staging.unlink(missing_ok=True)
            if install_root.exists():
                shutil.rmtree(install_root)
            source = ROOT / 'sources' / SHA256.lower()
            if OFFLINE:
                if not source.is_file():
                    raise ValueError('verified native source was not prepared')
                shutil.copyfile(source, staging)
            else:
                request = urllib.request.Request(URL, headers={{'User-Agent': 'qinyuan-agent'}})
                total = 0
                with urllib.request.urlopen(request, timeout=min(30, remaining())) as response, staging.open('wb') as target:
                    while True:
                        chunk = response.read(min(1024 * 1024, int(remaining())))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError('download exceeds 256 MiB')
                        target.write(chunk)
            digest = hashlib.sha256(staging.read_bytes()).hexdigest()
            if digest != SHA256:
                raise ValueError('download SHA256 does not match')
            if ARCHIVE_FORMAT == 'raw':
                executable = ROOT / 'bin' / NAME
                executable.parent.mkdir(parents=True, exist_ok=True)
                staging.replace(executable)
            else:
                install_root.mkdir(parents=True, exist_ok=True)
                if ARCHIVE_FORMAT == 'zip':
                    extract_zip(staging, install_root)
                else:
                    extract_tar(staging, install_root)
                executable = install_root / safe_relative(EXECUTABLE_PATH)
                if not executable.is_file():
                    raise ValueError('configured executable is missing from archive')
            executable.chmod(0o755)
            run_self_test(executable)
            staging.unlink(missing_ok=True)
            return executable

        def install_python():
            if not PYTHON_PACKAGE or not PYTHON_VERSION:
                raise ValueError('Python fallback is not configured')
            target = ROOT / 'python'
            target.mkdir(parents=True, exist_ok=True)
            package = PYTHON_PACKAGE + '==' + PYTHON_VERSION
            environment = dict(os.environ)
            environment['PYTHONPATH'] = str(target) + os.pathsep + environment.get('PYTHONPATH', '')
            offline_args = []
            if OFFLINE:
                wheelhouse = Path(WHEELHOUSE)
                if not WHEEL_HASHES or not wheelhouse.is_relative_to(ROOT / 'wheels'):
                    raise ValueError('verified Python wheels were not prepared')
                for name, expected in WHEEL_HASHES.items():
                    wheel = wheelhouse / name
                    if wheel.parent != wheelhouse or not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
                        raise ValueError('prepared Python wheel changed')
                offline_args = ['--no-index', '--find-links', str(wheelhouse)]
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '--no-input',
                 '--only-binary=:all:', *offline_args, '--target', str(target), package],
                timeout=remaining(), check=True, env=environment,
            )
            if PYTHON_MODULE:
                subprocess.run(
                    [sys.executable, '-c', 'import ' + PYTHON_MODULE],
                    timeout=min(15, remaining()),
                    check=True,
                    env=environment,
                )
            return target

        ROOT.mkdir(parents=True, exist_ok=True)
        receipt_dir = ROOT / 'receipts'
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = receipt_dir / (NAME + '.json')
        binding = hashlib.sha256(json.dumps([URL, SHA256, ARCHIVE_FORMAT, EXECUTABLE_PATH, SELF_TEST_ARGS,
            PYTHON_PACKAGE, PYTHON_VERSION, PYTHON_MODULE], sort_keys=True).encode()).hexdigest()
        if (receipt.is_file() and json.loads(receipt.read_text()).get('request_sha256') == binding
                and Path(json.loads(receipt.read_text())['path']).exists()):
            print(json.dumps({{'status': 'already_installed', 'path': json.loads(receipt.read_text())['path']}}))
            raise SystemExit(0)
        errors = []
        installed = None
        if URL:
            try:
                installed = install_native()
            except Exception as error:
                errors.append('native: ' + type(error).__name__)
        if installed is None:
            try:
                installed = install_python()
            except Exception as error:
                errors.append('python: ' + type(error).__name__)
        if installed is None:
            raise RuntimeError('tool installation failed: ' + '; '.join(errors))
        receipt.write_text(json.dumps({{'name': NAME, 'path': str(installed), 'errors': errors, 'request_sha256': binding}}), encoding='utf-8')
        print(json.dumps({{'status': 'installed', 'path': str(installed), 'fallback_errors': errors}}))
        """
    ).strip()
    return script


def _bootstrap_plan(script: str, timeout_seconds: int) -> dict[str, Any]:
    return {
        "sandbox_command": {
            "tool": "python3",
            "arguments": ["-c", script],
            "timeout_seconds": timeout_seconds,
        },
        "bootstrap": {"compensate_seconds": _BOOTSTRAP_BUDGET},
        "request": {"operation": "tool_bootstrap"},
    }


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

    archive_format accepts raw, zip, tar, tar.gz or tar.xz.
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
    return _bootstrap_plan(
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
