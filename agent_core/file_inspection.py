"""The exact, read-only file inspection program approved for T3."""

from __future__ import annotations

from pathlib import PurePosixPath


FILE_TOOL = "file.inspect_file"


def validate_inspection_path(path: str) -> str:
    if not isinstance(path, str) or len(path.encode("utf-8")) > 4096:
        raise ValueError("invalid inspection path")
    if any(ord(char) < 32 for char in path) or "\\" in path:
        raise ValueError("invalid inspection path")
    parts = path.split("/")
    if (
        not path.startswith("/workspace/input/")
        or any(part in {"", ".", ".."} for part in parts[1:])
        or str(PurePosixPath(path)) != path
    ):
        raise ValueError("inspection requires an exact /workspace/input/ file path")
    return path


# Open every component relative to an already-open directory. The descriptor,
# not the original pathname, is passed to file, so path replacement cannot
# redirect inspection between authorization and reading the file.
FILE_INSPECTION_SCRIPT = """import errno
import os
import stat
import subprocess
import sys

def fail(code, reason):
    print(reason, file=sys.stderr)
    raise SystemExit(code)

fds = []
try:
    parts = sys.argv[1].split('/')
    if parts[:3] != ['', 'workspace', 'input'] or len(parts) < 4 or any(p in ('', '.', '..') for p in parts[1:]):
        fail(41, 'path_rejected')
    current = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fds.append(current)
    for part in parts[1:-1]:
        current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
        fds.append(current)
    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
    fds.append(fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        fail(41, 'non_regular_file')
    try:
        result = subprocess.run(
            ['/usr/bin/file', '--brief', '--error', '--dereference', '--', '/proc/self/fd/' + str(fd)],
            pass_fds=(fd,), env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, check=False,
        )
    except OSError:
        fail(42, 'file_program_unavailable')
    if result.returncode != 0:
        fail(43, 'file_program_failed')
except OSError as exc:
    if exc.errno == errno.ENOENT:
        fail(40, 'file_missing')
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV):
        fail(41, 'file_path_rejected')
    fail(42, 'file_environment_error')
finally:
    for fd in reversed(fds):
        os.close(fd)
"""


def inspection_arguments(path: str) -> tuple[str, ...]:
    return ("-I", "-c", FILE_INSPECTION_SCRIPT, validate_inspection_path(path))
