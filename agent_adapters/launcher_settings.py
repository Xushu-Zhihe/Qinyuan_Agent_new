"""Desktop configuration and Windows current-user protected credentials."""

import ctypes
import base64
import hashlib
import json
import math
import os
import re
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from agent_adapters.workbench_contracts import WorkbenchError

def project_root():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    directory = Path(sys.executable).resolve().parent
    return next((p for p in (directory, *directory.parents) if (p / "pyproject.toml").is_file() and (p / "Sandbox").is_dir()), directory)


ROOT = project_root()
TIMEOUTS = {
    "QINYUAN_LLM_TIMEOUT_SECONDS": (600, "调用总时限", "秒"),
    "QINYUAN_LLM_CONNECT_TIMEOUT_SECONDS": (10, "连接超时", "秒"),
    "QINYUAN_LLM_FIRST_OUTPUT_TIMEOUT_SECONDS": (180, "首次输出超时", "秒"),
    "QINYUAN_LLM_IDLE_TIMEOUT_SECONDS": (60, "输出停顿超时", "秒"),
}
DEFAULTS = {"QINYUAN_MODEL": "deepseek-flash", "OPENAI_BASE_URL": "https://api.deepseek.com",
            "QINYUAN_BACKGROUND_OPACITY": "30",
            "QINYUAN_SANDBOX_PROFILE": "windows", "QINYUAN_LAUNCH_PROFILE": "",
            "QINYUAN_LAUNCH_CONCURRENCY": "1", "QINYUAN_LAUNCH_QUESTION_MINUTES": "60",
            "QINYUAN_LAUNCH_OVERALL_MINUTES": "180", "QINYUAN_INNER_MODEL": "",
            "QINYUAN_INNER_BASE_URL": ""}
DEFAULTS.update({key: str(value[0]) for key, value in TIMEOUTS.items()})
_LOCK = threading.RLock()


def env_values(path):
    from dotenv import dotenv_values
    return dotenv_values(path, interpolate=False) if Path(path).exists() else {}


def _crypt(data, *, decrypt=False):
    if os.name != "nt":
        raise WorkbenchError("settings.windows_credentials_required", 409)
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    source = ctypes.create_string_buffer(data)
    incoming, outgoing = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char))), Blob()
    dll = ctypes.WinDLL("crypt32", use_last_error=True)
    if decrypt:
        ok = dll.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        ok = dll.CryptProtectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    if not ok:
        raise WorkbenchError("settings.credential_protection_failed", 409)
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


class SecretStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".bin")

    def get(self, key, default=None):
        path = self._path(key)
        if not path.exists():
            return default
        if path.is_symlink():
            raise WorkbenchError("settings.credential_path_rejected", 409)
        return _crypt(path.read_bytes(), decrypt=True).decode("utf-8")

    def __setitem__(self, key, value):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        encrypted = _crypt(value.encode())
        # Verify before publishing; never keep a plaintext migration backup.
        if _crypt(encrypted, decrypt=True) != value.encode():
            raise WorkbenchError("settings.credential_verification_failed", 409)
        temporary = path.with_suffix(".tmp")
        with _LOCK:
            temporary.write_bytes(encrypted)
            temporary.replace(path)

    def pop(self, key, default=None):
        self._path(key).unlink(missing_ok=True)
        return default

    def configured(self, key):
        return self._path(key).is_file()


def role_values(role, path=None):
    path = Path(path or ROOT / ".env")
    values = env_values(path)
    inner = role == "inner_action" and bool(values.get("QINYUAN_INNER_MODEL"))
    model = values.get("QINYUAN_INNER_MODEL" if inner else "QINYUAN_MODEL") or DEFAULTS["QINYUAN_MODEL"]
    endpoint = values.get("QINYUAN_INNER_BASE_URL" if inner else "OPENAI_BASE_URL") or DEFAULTS["OPENAI_BASE_URL"]
    return values, model, endpoint, "inner" if inner else "outer"


def model_secret_key(role, endpoint):
    return "model:" + role + ":" + endpoint


class LauncherSettings:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.path = self.root / ".env"
        self.secrets = SecretStore(self.root / ".launcher-secrets")

    def revision(self):
        return hashlib.sha256(self.path.read_bytes() if self.path.exists() else b"").hexdigest()

    def background(self, source=None):
        path = Path(source) if source is not None else self.root / ".launcher-background"
        if not path.exists() and source is None:
            return None
        with path.open("rb") as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise WorkbenchError("settings.background_too_large")
        mime = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if data.startswith(b"\xff\xd8\xff")
                else "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
        if mime is None:
            raise WorkbenchError("settings.background_format_invalid")
        if source is not None:
            with _LOCK:
                temporary = self.root / ".launcher-background.tmp"
                temporary.write_bytes(data)
                temporary.replace(self.root / ".launcher-background")
        return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")

    def clear_background(self):
        with _LOCK:
            (self.root / ".launcher-background").unlink(missing_ok=True)

    def save_opacity(self, value):
        if not isinstance(value, dict) or set(value) != {"opacity", "revision"} or type(value["opacity"]) is not int or not 0 <= value["opacity"] <= 100:
            raise WorkbenchError("settings.background_opacity_invalid")
        with _LOCK:
            if value["revision"] != self.revision():
                raise WorkbenchError("settings.configuration_changed", 409)
            self._write({"QINYUAN_BACKGROUND_OPACITY": str(value["opacity"])})
        return {"revision": self.revision()}

    def public(self):
        values = env_values(self.path)
        from agent_adapters.deepseek_roles import model_capacity_policy
        capacity = {}
        for role, name in (("outer_planning", "outer"), ("inner_action", "inner")):
            _, model, endpoint, _ = role_values(role, self.path)
            try:
                capacity[name] = asdict(model_capacity_policy(model, endpoint))
            except ValueError:
                capacity[name] = None
        return {"values": {key: values.get(key) or default for key, default in DEFAULTS.items()},
                "capacity": capacity,
                "revision": self.revision(), "key_configured": {name: self.secrets.configured(model_secret_key(name, role_values(role, self.path)[2])) for role, name in (("outer_planning", "outer"), ("inner_action", "inner"))},
                "plaintext_key_present": bool(values.get("OPENAI_API_KEY")),
                "timeouts": {key: {"default": v[0], "label": v[1], "unit": v[2]} for key, v in TIMEOUTS.items()}}

    def save(self, request):
        if not isinstance(request, dict) or set(request) != {"values", "revision", "keys"}:
            raise WorkbenchError("settings.fields_invalid")
        values = request["values"]
        if not isinstance(values, dict) or set(values) != set(DEFAULTS) or not isinstance(request["keys"], dict) or request["keys"].keys() - {"outer", "inner"}:
            raise WorkbenchError("settings.fields_invalid")
        if any(not isinstance(v, str) or len(v) > 2048 or any(ord(c) < 32 for c in v) for v in values.values()):
            raise WorkbenchError("settings.value_invalid")
        for key in TIMEOUTS:
            try:
                valid = math.isfinite(float(values[key])) and float(values[key]) > 0
            except ValueError:
                valid = False
            if not valid:
                raise WorkbenchError("settings.timeout_invalid")
        for key, maximum in (("QINYUAN_LAUNCH_CONCURRENCY", 3), ("QINYUAN_LAUNCH_QUESTION_MINUTES", 10080), ("QINYUAN_LAUNCH_OVERALL_MINUTES", 10080)):
            if not values[key].isdigit() or not 1 <= int(values[key]) <= maximum:
                raise WorkbenchError("settings.budget_invalid")
        if values["QINYUAN_SANDBOX_PROFILE"] not in {"linux", "linux-heavy", "windows"}:
            raise WorkbenchError("settings.sandbox_invalid")
        if not values["QINYUAN_BACKGROUND_OPACITY"].isascii() or not values["QINYUAN_BACKGROUND_OPACITY"].isdigit() or not 0 <= int(values["QINYUAN_BACKGROUND_OPACITY"]) <= 100:
            raise WorkbenchError("settings.background_opacity_invalid")
        if bool(values["QINYUAN_INNER_MODEL"]) != bool(values["QINYUAN_INNER_BASE_URL"]):
            raise WorkbenchError("settings.role_connection_invalid")
        # Use the real verified capacity policy, including separate role endpoints.
        from agent_adapters.deepseek_roles import model_capacity_policy
        model_capacity_policy(values["QINYUAN_MODEL"], values["OPENAI_BASE_URL"])
        if values["QINYUAN_INNER_MODEL"]:
            model_capacity_policy(values["QINYUAN_INNER_MODEL"], values["QINYUAN_INNER_BASE_URL"])
        with _LOCK:
            if request["revision"] != self.revision():
                raise WorkbenchError("settings.configuration_changed", 409)
            for role, key in request["keys"].items():
                if not isinstance(key, str) or len(key) > 8192 or any(ord(c) < 32 for c in key):
                    raise WorkbenchError("settings.credential_invalid")
                if key:
                    endpoint = values["QINYUAN_INNER_BASE_URL"] if role == "inner" else values["OPENAI_BASE_URL"]
                    self.secrets[model_secret_key(role, endpoint)] = key
            self._write({**values, "QINYUAN_DESKTOP_SETTINGS": "1", "QINYUAN_LLM_BACKEND": "openai",
                         "QINYUAN_ALLOW_CUSTOM_ENDPOINT": "1" if values["OPENAI_BASE_URL"] != DEFAULTS["OPENAI_BASE_URL"] or (values["QINYUAN_INNER_MODEL"] and values["QINYUAN_INNER_BASE_URL"] != DEFAULTS["OPENAI_BASE_URL"]) else "0"})
        return self.public()

    def _write(self, updates, *, remove=()):
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        remaining = dict(updates)
        lines = []
        for line in text.splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
            key = match[1] if match else None
            if key in remove:
                continue
            if key in updates:
                if key in remaining:
                    lines.append(key + "=" + json.dumps(remaining.pop(key), ensure_ascii=False))
            else:
                lines.append(line)
        lines.extend(key + "=" + json.dumps(value, ensure_ascii=False) for key, value in remaining.items())
        temporary = self.path.with_suffix(".desktop.tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def migrate_key(self, revision):
        with _LOCK:
            if revision != self.revision():
                raise WorkbenchError("settings.configuration_changed", 409)
            key = env_values(self.path).get("OPENAI_API_KEY")
            if key:
                self.secrets[model_secret_key("outer", role_values("outer_planning", self.path)[2])] = key
                self._write({"QINYUAN_DESKTOP_SETTINGS": "1"}, remove=("OPENAI_API_KEY",))
        return self.public()

    def clear_key(self, role):
        endpoint = role_values("inner_action" if role == "inner" else "outer_planning", self.path)[2]
        self.secrets.pop(model_secret_key(role, endpoint))
        return self.public()
