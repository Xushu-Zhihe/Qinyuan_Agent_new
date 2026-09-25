"""Durable application declarations and process-local filesystem ownership."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4


def encode(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":")
    ).encode()


def read_json(path: Path, limit=1024 * 1024):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    return parse_json(data, limit)


def parse_json(data: bytes, limit=1024 * 1024):
    if len(data) > limit:
        raise ValueError("json_size_limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(
        data,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non_finite_json")),
    )


def immutable_write(path: Path, value):
    payload = encode(value)
    envelope = encode({"payload": value, "sha256": hashlib.sha256(payload).hexdigest()})
    pending = path.parent / (".pending-" + str(uuid4()))
    try:
        with pending.open("xb") as stream:
            stream.write(envelope)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(pending, path)
        except FileExistsError:
            if immutable_read(path) != value:
                raise ValueError("run_declaration_conflict") from None
    finally:
        pending.unlink(missing_ok=True)


def immutable_read(path: Path):
    data = read_json(path)
    if not isinstance(data, dict) or set(data) != {"payload", "sha256"}:
        raise ValueError("run_declaration_corrupt")
    if hashlib.sha256(encode(data["payload"])).hexdigest() != data["sha256"]:
        raise ValueError("run_declaration_corrupt")
    return data["payload"]


def run_path(root: Path, task_id: str):
    parsed = UUID(task_id)
    if parsed.version != 4 or str(parsed) != task_id:
        raise ValueError("invalid_task_run_id")
    root = root.resolve()
    path = root / task_id
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("invalid_task_directory")
    return path


@contextmanager
def run_lock(directory: Path):
    stream = (directory / "operation.lock").open("a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError("task_run_busy") from None
    try:
        yield
    finally:
        stream.seek(0)
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
