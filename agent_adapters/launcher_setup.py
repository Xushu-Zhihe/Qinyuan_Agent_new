"""Read-only host checks and fixed project preparation commands."""

import ast
import json
import os
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from uuid import uuid4

from agent_adapters.workbench_contracts import WorkbenchError
from qinyuan_core import get_logger

HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
logger = get_logger("sys")


def image_definitions(root):
    def constants(relative):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        return {node.targets[0].id: node.value.value for node in tree.body
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)}
    profiles = constants("agent_adapters/docker_profile.py")
    network = constants("Sandbox/target_network.py")
    return {
        "linux": ("Sandbox/dockerfile", profiles["IMAGE_REF"], profiles["IMAGE_ID"]),
        "windows": ("Sandbox/windows.dockerfile", profiles["WINDOWS_IMAGE_REF"], profiles["WINDOWS_IMAGE_ID"]),
        "forwarder": ("Sandbox/target_forwarder.dockerfile", network["FORWARDER_IMAGE"], None),
    }


class LauncherSetup:
    def __init__(self, root, in_use):
        self.root, self.in_use = Path(root), in_use
        self.lock = threading.RLock()
        self.state = {"status": "idle", "action": None, "id": None}
        self.logs = deque(maxlen=250)
        self.process = None

    @property
    def python(self):
        return self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _run(self, command, timeout=15):
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, creationflags=HIDDEN)

    def check(self, group="all", profile="windows"):
        if group not in {"all", "basic", "images", "files"} or profile not in {"linux", "linux-heavy", "windows"}:
            raise WorkbenchError("environment.check_invalid")
        items = []
        def item(name, status, detail):
            items.append(dict(name=name, status=status, detail=detail))
        if group in {"all", "basic"}:
            for binary, label in (("uv", "uv"), ("docker", "Docker CLI")):
                item(label, "passed" if shutil.which(binary) else "failed", shutil.which(binary) or "未安装")
            item("Agent Python", "passed" if self.python.is_file() else "failed", str(self.python))
            if self.python.is_file():
                try:
                    result = self._run([str(self.python), "-c", "import sys; assert (3,11)<=sys.version_info<(3,14); print(sys.version.split()[0])"])
                    item("Python 版本", "passed" if result.returncode == 0 else "failed", result.stdout.strip() or "需要 Python 3.11–3.13")
                except subprocess.TimeoutExpired:
                    item("Python 版本", "unknown", "environment.python_timeout")
        if group in {"all", "files"}:
            for name in ("uv.lock", "pyproject.toml", "MCPs/Mcp_client/registry.json", "frontend/index.html"):
                item(name, "passed" if (self.root / name).is_file() else "failed", "文件存在" if (self.root / name).is_file() else "缺少文件")
            if self.python.is_file():
                try:
                    result = self._run([str(self.python), "-c", "import docker, dotenv, mcp, PIL, pydantic, langchain_openai; print('依赖可导入')"])
                    item("Agent 依赖", "passed" if result.returncode == 0 else "failed", "依赖可导入" if result.returncode == 0 else "environment.dependencies_missing")
                except subprocess.TimeoutExpired:
                    item("Agent 依赖", "unknown", "environment.dependencies_timeout")
        if group in {"all", "images"}:
            try:
                result = self._run(["docker", "info", "--format", "{{.ServerVersion}}"])
                item("Docker Engine", "passed" if result.returncode == 0 else "failed", result.stdout.strip() if result.returncode == 0 else "environment.docker_unavailable")
                if result.returncode == 0:
                    for key in (["linux", "windows"] if profile == "windows" else ["linux"]) + ["forwarder"]:
                        _, tag, expected = image_definitions(self.root)[key]
                        result = self._run(["docker", "image", "inspect", tag])
                        if result.returncode:
                            item(tag, "failed", "environment.image_missing")
                            continue
                        image = json.loads(result.stdout)[0]
                        valid = image["Id"] == expected if expected else image.get("Config", {}).get("Labels", {}).get("qinyuan.target.private_ipv4") == "rfc1918"
                        item(tag, "passed" if valid else "failed", image["Id"] if valid else "environment.image_identity_mismatch")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                item("Docker Engine", "unknown", "environment.docker_unavailable")
        return items

    def snapshot(self):
        with self.lock:
            return {**self.state, "logs": list(self.logs)}

    def start(self, action):
        if action not in {"dependencies", "linux", "windows", "forwarder"}:
            raise WorkbenchError("download.action_invalid")
        with self.lock:
            if self.state["status"] in {"running", "cancel_requested", "cancelled_unverified"}:
                raise WorkbenchError("download.operation_in_progress", 409)
            if action == "dependencies" and self.in_use():
                raise WorkbenchError("download.environment_in_use", 409)
            if action == "dependencies" and os.name == "nt":
                powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
                result = self._run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                    "@(Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object Path | Select-Object -ExpandProperty Path) | ConvertTo-Json -Compress"])
                if result.returncode:
                    raise WorkbenchError("download.environment_state_unknown", 409)
                paths = json.loads(result.stdout or "[]") or []
                paths = [paths] if isinstance(paths, str) else paths
                if str(self.python).casefold() in {str(Path(p)).casefold() for p in paths}:
                    raise WorkbenchError("download.environment_in_use", 409)
            self.state = {"status": "running", "action": action, "id": str(uuid4())}
            logger.info("download.preparation_started", operation_id=self.state["id"], action=action)
            self.logs.clear()
            threading.Thread(target=self._prepare, args=(action,), daemon=True).start()
        return self.snapshot()

    def _prepare(self, action):
        try:
            actions = ["linux", "windows"] if action == "windows" else [action]
            for step in actions:
                if action == "dependencies":
                    command = ["uv", "sync", "--locked"]
                else:
                    dockerfile, tag, _ = image_definitions(self.root)[step]
                    command = ["docker", "build", "--progress=plain", "--file", dockerfile, "--tag", tag]
                    if step == "linux":
                        command += ["--provenance=false", "--sbom=false"]
                    command += ["."]
                with self.lock:
                    if self.state["status"] != "running":
                        return
                    self.logs.append(f"[download] 开始 {step}")
                    self.process = subprocess.Popen(command, cwd=self.root, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=HIDDEN)
                    process = self.process
                for line in process.stdout:
                    # Build output may include upstream URLs; retain step diagnostics only.
                    clean = line.rstrip()[:500]
                    if not any(word in clean.lower() for word in ("authorization", "api_key", "token=", "https://", "http://")):
                        with self.lock:
                            self.logs.append("[download] " + clean)
                code = process.wait()
                with self.lock:
                    self.process = None
                    if self.state["status"] != "running":
                        self.state["status"] = "cancelled_unverified"
                        return
                if code:
                    raise WorkbenchError("download.command_failed", 409)
            checks = self.check("files" if action == "dependencies" else "images", "windows" if action == "windows" else "linux")
            with self.lock:
                self.state.update(status="completed" if all(v["status"] == "passed" for v in checks) else "needs_attention", checks=checks)
                logger.info("download.preparation_finished", operation_id=self.state.get("id"), action=action, status=self.state["status"])
        except (OSError, subprocess.SubprocessError, WorkbenchError) as exc:
            with self.lock:
                self.state.update(status="failed", error=getattr(exc, "code", "download.dependency_failed"))
                logger.warning("download.preparation_failed", operation_id=self.state.get("id"), action=action, code=self.state["error"], category="dependency")

    def cancel(self):
        with self.lock:
            if self.state["status"] == "running":
                self.state["status"] = "cancel_requested"
                if self.process is not None:
                    self.process.terminate()
                else:
                    self.state["status"] = "cancelled_unverified"
        return self.snapshot()
