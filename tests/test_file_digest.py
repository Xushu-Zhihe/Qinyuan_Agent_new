"""Unknown trusted digest execution must close future Sandbox admission."""

import threading
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from Sandbox.work_file_digest import T6FileDigest


@pytest.mark.parametrize("stage", ["create", "start", "inspect", "unknown", "invalid"])
def test_unknown_digest_result_quiesces_without_retry(stage):
    calls = []
    stopped = []
    task_id = str(uuid4())
    binding = SimpleNamespace(task_run_id=task_id, execution_lock=threading.Lock())

    def create(*args, **kwargs):
        calls.append("create")
        assert kwargs["user"] == "10001:10001"
        if stage == "create":
            raise TimeoutError("lost creation response")
        return {"Id": "digest"}

    def start(*args, **kwargs):
        calls.append("start")
        if stage == "start":
            raise TimeoutError("lost digest output")
        if stage == "invalid":
            return (b"malformed", b"")
        return (b'{"status":"unknown"}', b"")

    def inspect(*args):
        calls.append("inspect")
        if stage == "inspect":
            raise TimeoutError("lost exit state")
        return {"Running": False, "ExitCode": 0}

    sandbox = SimpleNamespace(
        _binding=lambda _: binding,
        _verify_active=lambda *_: None,
        _admit=lambda _: nullcontext(),
        _runtime=SimpleNamespace(
            client=SimpleNamespace(
                api=SimpleNamespace(
                    exec_create=create,
                    exec_start=start,
                    exec_inspect=inspect,
                )
            )
        ),
        quiesce=lambda identity: stopped.append(identity),
    )
    result = T6FileDigest(sandbox).hash_file(
        task_id, "sandbox", "/workspace/work/result"
    )
    assert result.status == "unknown"
    assert stopped == ["sandbox"]
    assert calls.count("create") == 1
    assert not binding.execution_lock.locked()
