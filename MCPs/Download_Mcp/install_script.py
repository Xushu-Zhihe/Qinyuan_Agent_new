"""Sandbox installation script template.

Kept as a plain (non-f-string) template so ordinary Python braces, file modes
and archive checks remain readable.  Tokens are replaced with JSON literals by
``render``.
"""

from __future__ import annotations

import json
import re


_TOKEN = re.compile(r"__([A-Z0-9_]+)__")
_TEMPLATE = r'''
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

NAME = __NAME__
URL = __URL__
SHA256 = __SHA256__
ARCHIVE_FORMAT = __ARCHIVE_FORMAT__
EXECUTABLE_PATH = __EXECUTABLE_PATH__
SELF_TEST_ARGS = __SELF_TEST_ARGS__
PYTHON_PACKAGE = __PYTHON_PACKAGE__
PYTHON_VERSION = __PYTHON_VERSION__
PYTHON_MODULE = __PYTHON_MODULE__
TIMEOUT = __TIMEOUT_SECONDS__
MAX_BYTES = 240 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20000
MAX_EXTRACTED_BYTES = 240 * 1024 * 1024
RESERVE_BYTES = 8 * 1024 * 1024
ROOT = Path('/tools')
OFFLINE = os.environ.get('QINYUAN_OFFLINE_TOOL_INSTALL') == '1'
WHEELHOUSE = os.environ.get('QINYUAN_TOOL_WHEELHOUSE', '')
WHEEL_HASHES = json.loads(os.environ.get('QINYUAN_TOOL_WHEEL_HASHES', '{}'))
PREPARATION_ERRORS = json.loads(os.environ.get('QINYUAN_TOOL_PREPARATION_ERRORS', '[]'))
DEADLINE = time.monotonic() + TIMEOUT


def remaining():
    value = DEADLINE - time.monotonic()
    if value <= 0:
        raise TimeoutError('tool bootstrap budget exhausted')
    return value


def reason(error):
    text = ' '.join(str(error).split())
    value = type(error).__name__ + (': ' + text if text else '')
    return value[:300]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tail(path, limit=4096):
    try:
        with path.open('rb') as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - limit))
            data = source.read(limit)
    except OSError:
        return ''
    return data.decode('utf-8', errors='replace').strip()


def safe_relative(value):
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('archive contains an unsafe path')
    return Path(*path.parts)


def check_extraction_capacity(total):
    if total > MAX_EXTRACTED_BYTES:
        raise ValueError('archive expands beyond the extracted-size limit')
    free = shutil.disk_usage(ROOT).free
    if total + RESERVE_BYTES > free:
        raise ValueError('not enough /tools space for extracted archive')


def extract_zip(source, destination):
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError('archive contains too many members')
        total = 0
        for member in members:
            if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError('archive symlinks are not allowed')
            if not member.is_dir():
                total += max(0, member.file_size)
        check_extraction_capacity(total)
        for member in members:
            relative = safe_relative(member.filename)
            target = destination / relative
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(((member.external_attr >> 16) & 0o777) or 0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source_file, target.open('wb') as target_file:
                shutil.copyfileobj(source_file, target_file)
            target.chmod(((member.external_attr >> 16) & 0o777) or 0o644)


def extract_tar(source, destination):
    with tarfile.open(source, 'r:*') as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError('archive contains too many members')
        total = 0
        for member in members:
            if not member.isfile() and not member.isdir():
                raise ValueError('archive links and special files are not allowed')
            if member.isfile():
                total += max(0, member.size)
        check_extraction_capacity(total)
        for member in members:
            relative = safe_relative(member.name)
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod((member.mode & 0o777) or 0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = archive.extractfile(member)
            if source_file is None:
                raise ValueError('archive member cannot be read')
            with source_file, target.open('wb') as target_file:
                shutil.copyfileobj(source_file, target_file)
            target.chmod((member.mode & 0o777) or 0o644)


def run_self_test(path):
    if not SELF_TEST_ARGS:
        return
    result = subprocess.run(
        [str(path), *SELF_TEST_ARGS],
        cwd=str(path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=min(15, remaining()),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError('native tool self-test failed')


def install_native():
    if not URL or not SHA256:
        raise ValueError('native installation requires URL and SHA256')
    ROOT.mkdir(parents=True, exist_ok=True)
    staging = ROOT / ('.download-' + NAME)
    install_root = ROOT / 'apps' / NAME
    install_root_bin = install_root / 'bin'
    staging.unlink(missing_ok=True)
    if install_root.exists():
        shutil.rmtree(install_root)
    source = ROOT / 'sources' / SHA256.lower()
    if OFFLINE:
        if not source.is_file():
            detail = '; '.join(str(item) for item in PREPARATION_ERRORS)
            raise ValueError('verified native source was not prepared' + (': ' + detail if detail else ''))
        # Hard-link instead of copy: the source and the staged file must never
        # occupy the small /tools tmpfs twice at the same time, while the
        # source stays available for a later receipt-verified reinstall.
        try:
            os.link(source, staging)
        except OSError:
            shutil.copyfile(source, staging)
    else:
        request = urllib.request.Request(URL, headers={'User-Agent': 'qinyuan-agent'})
        total = 0
        with urllib.request.urlopen(request, timeout=min(30, remaining())) as response, staging.open('wb') as target:
            while True:
                chunk = response.read(min(1024 * 1024, max(1, int(remaining()))))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError('download exceeds 240 MiB')
                target.write(chunk)
    if shutil.disk_usage(ROOT).free < RESERVE_BYTES:
        raise ValueError('not enough /tools space for installation')
    if sha256_file(staging) != SHA256:
        raise ValueError('download SHA256 does not match')
    if ARCHIVE_FORMAT == 'raw':
        executable = install_root_bin / NAME
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
        staging.unlink(missing_ok=True)
    executable.chmod(0o755)
    run_self_test(executable)
    return executable


def install_python():
    if not PYTHON_PACKAGE or not PYTHON_VERSION:
        raise ValueError('Python fallback is not configured')
    # One target per tool: a later install cannot overwrite this tool's
    # dependencies.  The shared /tools/python and /opt/qinyuan/python remain
    # on PYTHONPATH through the wrapper.
    target = ROOT / 'python' / NAME
    target.mkdir(parents=True, exist_ok=True)
    (target / '.qinyuan-tool').write_text(NAME, encoding='utf-8')
    if shutil.disk_usage(ROOT).free < RESERVE_BYTES:
        raise ValueError('not enough /tools space for Python fallback')
    package = PYTHON_PACKAGE + '==' + PYTHON_VERSION
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(target) + os.pathsep + environment.get('PYTHONPATH', '')
    # /tmp is only 64 MiB in the TaskRun profile, and some sandbox profiles do
    # not mount it at all. Keep pip's unpack directory under /tools instead.
    pip_tmp = ROOT / ('.tmp-' + NAME)
    pip_tmp.mkdir(parents=True, exist_ok=True)
    environment['TMPDIR'] = str(pip_tmp)
    environment['TMP'] = str(pip_tmp)
    environment['TEMP'] = str(pip_tmp)
    offline_args = []
    if OFFLINE:
        wheelhouse = Path(WHEELHOUSE)
        if not WHEEL_HASHES or not wheelhouse.is_relative_to(ROOT / 'wheels'):
            raise ValueError('verified Python wheels were not prepared')
        for name, expected in WHEEL_HASHES.items():
            wheel = wheelhouse / name
            if wheel.parent != wheelhouse or not wheel.is_file() or sha256_file(wheel) != expected:
                raise ValueError('prepared Python wheel changed')
        for entry in wheelhouse.iterdir():
            if entry.name not in WHEEL_HASHES:
                raise ValueError('unexpected file in prepared Python wheelhouse')
        offline_args = ['--no-index', '--find-links', str(wheelhouse)]
    log_path = ROOT / 'receipts' / (NAME + '.pip.log')
    try:
        with log_path.open('wb') as log:
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '--no-input',
                 '--upgrade', '--only-binary=:all:', *offline_args, '--target', str(target), package],
                stdout=log, stderr=subprocess.STDOUT, timeout=remaining(), check=True, env=environment,
            )
    except Exception as error:
        detail = tail(log_path)
        raise RuntimeError('pip install failed' + (': ' + detail if detail else '')) from error
    finally:
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            shutil.rmtree(pip_tmp, ignore_errors=True)
        except OSError:
            pass
    if PYTHON_MODULE:
        result = subprocess.run(
            [sys.executable, '-c', 'import ' + PYTHON_MODULE],
            timeout=min(15, remaining()),
            check=False,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError('python module import failed: ' + PYTHON_MODULE)
    wrapper = ROOT / 'apps' / NAME / 'bin' / 'python3'
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    pythonpath = environment.get('PYTHONPATH', '').rstrip(os.pathsep)
    wrapper.write_text(
        '#!/bin/sh\n'
        'export PYTHONPATH=' + shlex.quote(pythonpath) + '\n'
        'exec ' + shlex.quote(sys.executable) + ' "$@"\n',
        encoding='utf-8',
    )
    wrapper.chmod(0o755)
    return wrapper, target


def read_receipt():
    try:
        data = json.loads(receipt.read_text())
        if data.get('request_sha256') != binding:
            return None
        path = Path(data['path'])
        if not path.exists():
            return None
        if data.get('kind') == 'python':
            target = data.get('python_target')
            if target:
                target_path = Path(target)
                if not target_path.is_dir() or not path.is_file():
                    return None
                if PYTHON_MODULE:
                    result = subprocess.run(
                        [str(path), '-c', 'import ' + PYTHON_MODULE],
                        timeout=min(15, remaining()), check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    if result.returncode != 0:
                        return None
            else:
                # Backward compatibility with the old shared /tools/python receipt.
                if not path.is_dir():
                    return None
                if PYTHON_MODULE:
                    environment = dict(os.environ)
                    environment['PYTHONPATH'] = str(path) + os.pathsep + environment.get('PYTHONPATH', '')
                    result = subprocess.run(
                        [sys.executable, '-c', 'import ' + PYTHON_MODULE],
                        timeout=min(15, remaining()), check=False, env=environment,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    if result.returncode != 0:
                        return None
        elif data.get('kind') == 'native':
            if not path.is_file():
                return None
            expected = data.get('target_sha256')
            if not expected or sha256_file(path) != expected:
                return None
        else:
            return None
        return data
    except (OSError, TypeError, ValueError, KeyError):
        return None


def write_receipt(value):
    temporary = receipt.with_name('.receipt-' + NAME + '.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temporary, receipt)


def prune_artifacts():
    """Drop sources/wheelhouses no valid receipt can reinstall from."""
    sources = set()
    wheelhouses = set()
    python_targets = set()
    receipts = ROOT / 'receipts'
    if receipts.is_dir():
        for item in receipts.glob('*.json'):
            try:
                data = json.loads(item.read_text())
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(data.get('source_sha256'), str):
                sources.add(data['source_sha256'].lower())
            if isinstance(data.get('wheelhouse'), str):
                wheelhouses.add(os.path.normpath(data['wheelhouse']))
            if isinstance(data.get('python_target'), str):
                python_targets.add(os.path.normpath(data['python_target']))
    source_dir = ROOT / 'sources'
    if source_dir.is_dir():
        for item in source_dir.iterdir():
            if item.name.lower() not in sources:
                try:
                    item.unlink()
                except OSError:
                    pass
    wheel_dir = ROOT / 'wheels'
    if wheel_dir.is_dir():
        for item in wheel_dir.iterdir():
            if os.path.normpath(str(item)) not in wheelhouses:
                try:
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
                except OSError:
                    pass
    python_dir = ROOT / 'python'
    if python_dir.is_dir():
        for item in python_dir.iterdir():
            # Only remove directories this installer created; never remove a
            # legacy package directory directly under /tools/python.
            if (not item.is_dir() or not (item / '.qinyuan-tool').is_file()
                    or os.path.normpath(str(item)) in python_targets):
                continue
            try:
                shutil.rmtree(item)
            except OSError:
                pass


ROOT.mkdir(parents=True, exist_ok=True)
receipt_dir = ROOT / 'receipts'
receipt_dir.mkdir(parents=True, exist_ok=True)
receipt = receipt_dir / (NAME + '.json')
binding = hashlib.sha256(json.dumps([URL, SHA256, ARCHIVE_FORMAT, EXECUTABLE_PATH, SELF_TEST_ARGS,
    PYTHON_PACKAGE, PYTHON_VERSION, PYTHON_MODULE], sort_keys=True).encode()).hexdigest()
existing = read_receipt()
if existing is not None:
    print(json.dumps({'status': 'already_installed', 'path': existing['path']}))
    raise SystemExit(0)
errors = []
installed = None
installed_kind = None
python_target = None
if URL:
    try:
        installed = install_native()
        installed_kind = 'native'
    except Exception as error:
        errors.append('native: ' + reason(error))
if installed is None:
    try:
        installed, python_target = install_python()
        installed_kind = 'python'
    except Exception as error:
        errors.append('python: ' + reason(error))
if installed is None:
    raise RuntimeError('tool installation failed: ' + '; '.join(errors))
target_sha256 = sha256_file(installed) if installed_kind == 'native' else None
write_receipt({'name': NAME, 'kind': installed_kind, 'path': str(installed),
               'target_sha256': target_sha256,
               'source_sha256': SHA256.lower() if installed_kind == 'native' and SHA256 else None,
               'python_target': str(python_target) if python_target is not None else None,
               'wheelhouse': os.path.normpath(WHEELHOUSE) if installed_kind == 'python' and WHEELHOUSE else None,
               'errors': errors, 'request_sha256': binding})
try:
    prune_artifacts()
except OSError:
    pass
print(json.dumps({'status': 'installed', 'path': str(installed), 'fallback_errors': errors}))
'''


def render(values: dict[str, object]) -> str:
    encoded = {
        key.upper(): json.dumps(value, ensure_ascii=True)
        for key, value in values.items()
    }

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in encoded:
            raise KeyError(f"missing installer token {key}")
        return encoded[key]

    return _TOKEN.sub(replace, _TEMPLATE).strip()
