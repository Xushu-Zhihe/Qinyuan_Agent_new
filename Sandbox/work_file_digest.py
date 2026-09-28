"""Fixed-code, read-only SHA-256 observation inside the TaskRun Sandbox."""

from __future__ import annotations

import json

from agent_core.command_contracts import validate_working_directory
from agent_core.intent_postconditions import FileDigestObservation


DIGEST_SCRIPT = r"""import hashlib,json,os,stat,sys
path=sys.argv[1];root='/workspace/work'
parts=path[len(root)+1:].split('/')
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    for part in parts[:-1]:
        nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        os.close(fd);fd=nextfd
    item=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
    try:
        if not stat.S_ISREG(os.fstat(item).st_mode):
            print(json.dumps({'status':'unsafe'}));sys.exit(0)
        digest=hashlib.sha256()
        while True:
            block=os.read(item,65536)
            if not block:break
            digest.update(block)
        print(json.dumps({'status':'present','sha256':digest.hexdigest()}))
    finally:os.close(item)
except FileNotFoundError:
    print(json.dumps({'status':'missing'}))
except OSError:
    print(json.dumps({'status':'unsafe'}))
finally:os.close(fd)
"""


class T6FileDigest:
    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox

    def hash_file(
        self, task_run_id: str, sandbox_id: str, path: str
    ) -> FileDigestObservation:
        validate_working_directory(path)
        if path == "/workspace/work":
            raise ValueError("digest_path_is_directory")
        binding = self.sandbox._binding(sandbox_id)
        if binding.task_run_id != task_run_id:
            raise ValueError("digest_task_mismatch")
        with binding.execution_lock:
            self.sandbox._verify_active(task_run_id, sandbox_id)
            api = self.sandbox._runtime.client.api
            try:
                with self.sandbox._admit(binding):
                    execution = api.exec_create(
                        sandbox_id,
                        ["python3", "-I", "-c", DIGEST_SCRIPT, path],
                        stdin=False,
                        stdout=True,
                        stderr=True,
                        tty=False,
                        privileged=False,
                        user="10001:10001",
                        workdir="/workspace/work",
                        environment={"PATH": "/usr/bin:/bin"},
                    )
                result = api.exec_start(
                    execution["Id"], stream=False, demux=True, tty=False
                )
                info = api.exec_inspect(execution["Id"])
                if (
                    not isinstance(result, tuple)
                    or len(result) != 2
                    or not isinstance(result[0], bytes)
                    or len(result[0]) > 256
                    or result[1]
                    or info.get("Running") is not False
                    or info.get("ExitCode") != 0
                ):
                    self.sandbox.quiesce(sandbox_id)
                    return FileDigestObservation("unknown")
                data = json.loads(result[0])
                if not isinstance(data, dict):
                    self.sandbox.quiesce(sandbox_id)
                    return FileDigestObservation("unknown")
                observation = FileDigestObservation(data["status"], data.get("sha256"))
                if observation.status == "unknown":
                    self.sandbox.quiesce(sandbox_id)
                return observation
            except Exception:
                self.sandbox.quiesce(sandbox_id)
                return FileDigestObservation("unknown")
