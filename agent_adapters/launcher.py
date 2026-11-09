"""Local desktop bridge; the Workbench process retains ownership of all runs."""

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

from agent_adapters.launcher_settings import LauncherSettings, ROOT
from agent_adapters.launcher_setup import HIDDEN, LauncherSetup
from agent_adapters.workbench_contracts import WorkbenchError
from agent_adapters.windows_security import WindowsSecuritySetup
from qinyuan_core import configure, get_logger

logger = get_logger("sys")
ACTIONS = frozenset({"snapshot", "settings", "migrate", "clear_key", "service", "service_status", "open",
    "profiles", "profile_save", "profile_check", "profile_clear", "check", "prepare", "progress",
    "cancel_prepare", "security", "link", "stop_service", "close", "choose_background", "clear_background", "background_opacity"})
LINKS = {"python": "https://www.python.org/downloads/windows/", "uv": "https://docs.astral.sh/uv/getting-started/installation/",
         "docker": "https://docs.docker.com/desktop/setup/install/windows-install/",
         "webview": "https://developer.microsoft.com/microsoft-edge/webview2/"}


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        code = ctypes.c_ulong()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


class Desktop:
    def __init__(self, root=ROOT, data_dir=None):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir or self.root / "workbench_data").resolve()
        self.record = self.data_dir / ".desktop-instance.json"
        self.settings = LauncherSettings(self.root)
        self.setup = LauncherSetup(self.root, lambda: self.instance() is not None)
        self.security = WindowsSecuritySetup(self.data_dir)
        self.lock = threading.RLock()
        self.window = None
        self.closing = False

    def instance(self):
        if not self.record.exists():
            return None
        value = json.loads(self.record.read_text(encoding="utf-8"))
        if value.get("project") != str(self.root) or value.get("data_dir") != str(self.data_dir) or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535:
            raise WorkbenchError("launcher.instance_identity_mismatch", 409)
        if not process_alive(value.get("pid")):
            self.record.unlink()
            return None
        return value

    def request(self, method, path, value=None):
        instance = self.instance()
        if instance is None:
            raise WorkbenchError("launcher.service_not_started", 409)
        request = urllib.request.Request(f'http://127.0.0.1:{instance["port"]}{path}',
            data=None if value is None else json.dumps(value).encode(), method=method,
            headers={"Content-Type": "application/json", "X-Qinyuan-Desktop": instance["token"]})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise WorkbenchError(json.load(error).get("error", "launcher.service_rejected"), error.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise WorkbenchError("launcher.service_unreachable" if method == "GET" else "launcher.control_outcome_unknown", 409) from None

    def ensure_service(self):
        with self.lock:
            if self.instance():
                return self.request("GET", "/api/desktop")
            if not self.setup.python.exists():
                raise WorkbenchError("environment.python_missing", 409)
            if self.setup.snapshot()["status"] == "running":
                raise WorkbenchError("download.operation_in_progress", 409)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(QINYUAN_DESKTOP_TOKEN=secrets.token_urlsafe(32), QINYUAN_DESKTOP_INSTANCE=str(self.record))
            # Service output never includes model keys; retain existing run logging.
            process = subprocess.Popen([str(self.setup.python), "-m", "agent_adapters.workbench", "--port", "0", "--data-dir", str(self.data_dir)],
                cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=HIDDEN)
            for _ in range(80):
                if process.poll() is not None:
                    raise WorkbenchError("launcher.service_start_failed", 409)
                if self.record.exists():
                    return self.request("GET", "/api/desktop")
                time.sleep(0.1)
            raise WorkbenchError("launcher.service_start_timeout", 409)

    @contextmanager
    def profile_store(self):
        from agent_adapters.competition import CompetitionConnections
        from agent_adapters.workbench import Workbench, data_directory_lease
        from agent_adapters.launcher_settings import SecretStore
        with self.lock, data_directory_lease(self.data_dir):
            store = SimpleNamespace(database=self.data_dir / "workbench.sqlite3", lock=self.lock)
            store._db = lambda: Workbench._db(store)
            connections = CompetitionConnections(store)
            connections.credentials = SecretStore(self.root / ".launcher-secrets" / "competition")
            yield connections

    def invoke(self, action, value=None):
        """Expose only fixed desktop actions, never a shell or filesystem bridge."""
        try:
            if not isinstance(action, str) or action not in ACTIONS:
                raise WorkbenchError("launcher.action_invalid")
            data = self._invoke(action, value)
            return {"ok": True, "data": data}
        except (WorkbenchError, ValueError, OSError, json.JSONDecodeError) as error:
            fixed = str(error) if isinstance(error, ValueError) and str(error).startswith(("d8_", "d22_", "settings.")) else "launcher.configuration_invalid" if isinstance(error, ValueError) else "launcher.dependency_failed"
            code = getattr(error, "code", fixed)
            category = ("unknown" if "outcome_unknown" in code else "conflict" if any(part in code for part in ("changed", "in_use", "in_progress"))
                        else "policy" if "rejected" in code else "input" if "invalid" in code or "unsupported" in code else "capacity" if getattr(error, "errno", None) == 28 else "dependency")
            logger.warning("desktop.operation_rejected", action=action if isinstance(action, str) and action in ACTIONS else "invalid", category=category, code=code)
            return {"ok": False, "error": code, "category": category}

    def _invoke(self, action, value):
        if action == "snapshot":
            from agent_adapters.competition import PLATFORM_TEMPLATES
            return {"settings": self.settings.public(), "templates": list(PLATFORM_TEMPLATES),
                    "service": bool(self.instance()), "security": self.security.snapshot(), "project": str(self.root),
                    "background": self.settings.background()}
        if action == "choose_background":
            import webview
            selected = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp)",))
            return {"image": self.settings.background(selected[0])} if selected else None
        if action == "clear_background":
            self.settings.clear_background()
            return True
        if action == "background_opacity":
            return self.settings.save_opacity(value)
        if action == "settings":
            return self.settings.save(value)
        if action == "migrate":
            return self.settings.migrate_key(value)
        if action == "clear_key" and value in {"outer", "inner"}:
            return self.settings.clear_key(value)
        if action == "service":
            return self.ensure_service()
        if action == "service_status":
            return self.request("GET", "/api/desktop") if self.instance() else {"active": False}
        if action == "open":
            self.ensure_service()
            instance = self.instance()
            webbrowser.open(f'http://127.0.0.1:{instance["port"]}/#competition')
            return True
        if action == "profiles":
            if self.instance():
                return self.request("GET", "/api/competition/profiles")
            with self.profile_store() as store:
                return store.list()
        if action == "profile_save":
            value = dict(value)
            identity = value.pop("id", "")
            if identity and not re.fullmatch(r"[a-f0-9-]{36}", identity):
                raise WorkbenchError("competition_profile_invalid")
            if self.instance():
                return self.request("PUT" if identity else "POST", "/api/competition/profiles" + ("/" + identity if identity else ""), value)
            with self.profile_store() as store:
                return store.save(value, identity or None)
        if action in {"profile_check", "profile_clear"}:
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9-]{36}", value):
                raise WorkbenchError("competition_profile_invalid")
            if self.instance():
                return self.request("POST" if action == "profile_check" else "DELETE", "/api/competition/profiles/" + value + ("/check" if action == "profile_check" else "/credential"), {} if action == "profile_check" else None)
            with self.profile_store() as store:
                return store.check(value) if action == "profile_check" else store.clear(value)
        if action == "check":
            return self.setup.check(value, self.settings.public()["values"]["QINYUAN_SANDBOX_PROFILE"])
        if action == "prepare":
            return self.setup.start(value)
        if action == "progress":
            return self.setup.snapshot()
        if action == "cancel_prepare":
            return self.setup.cancel()
        if action == "security":
            return self.security.snapshot() if value is None else self.security.start(value)
        if action == "link" and value in LINKS:
            webbrowser.open(LINKS[value])
            return True
        if action == "stop_service":
            if self.instance():
                return self.request("POST", "/api/desktop", {"action": "shutdown"})
            return True
        if action == "close":
            if value not in {"background", "pause"}:
                raise WorkbenchError("launcher.close_invalid")
            if value == "pause" and self.instance():
                self.request("POST", "/api/desktop", {"action": "pause"})
                return {"waiting": True}
            self.closing = True
            self.window.destroy()
            return True
        raise WorkbenchError("launcher.action_invalid")

    def on_closing(self):
        if self.closing:
            return True
        self.window.evaluate_js("window.launcherClose()")
        return False


def main():
    parser = argparse.ArgumentParser(description="Qinyuan desktop launcher")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--project", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        import webview
    except ImportError:
        message = "桌面依赖未安装，请运行 start-launcher.cmd。WebView2 安装：https://developer.microsoft.com/microsoft-edge/webview2/"
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "Qinyuan Agent", 0x10)
        parser.exit(1, message + "\n")
    desktop = Desktop(root=args.project, data_dir=args.data_dir)
    log_path = desktop.data_dir / "launcher.log.jsonl"
    configure(console=False, jsonl=log_path)
    base_logger = logging.getLogger("qinyuan")
    handler = base_logger.handlers[0]
    rotating = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
    rotating.setFormatter(handler.formatter)
    handler.close()
    base_logger.handlers[:] = [rotating]
    class Bridge:
        def invoke(self, action, value=None):
            return desktop.invoke(action, value)
    assets = Path(getattr(sys, "_MEIPASS", ROOT)) / "frontend"
    desktop.window = webview.create_window("Qinyuan Agent", str(assets / "launcher.html"),
        js_api=Bridge(), width=1160, height=790, min_size=(900, 640))
    desktop.window.events.closing += desktop.on_closing
    try:
        webview.start(gui="edgechromium", icon=str(assets / "assets/launcher.ico"))
    except webview.WebViewException:
        logger.error("launcher.webview_start_failed", category="dependency", code="launcher.webview_start_failed")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, "WebView2 启动失败。请检查桌面依赖与 Runtime：https://developer.microsoft.com/microsoft-edge/webview2/", "Qinyuan Agent", 0x10)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
