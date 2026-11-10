"""Desktop configuration, preparation and service boundaries without external calls."""

import base64
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_adapters import launcher_settings as settings_module
from agent_adapters.launcher import Desktop
from agent_adapters.launcher_settings import DEFAULTS, LauncherSettings, SecretStore, model_secret_key
from agent_adapters.launcher_setup import LauncherSetup, image_definitions
from agent_adapters.workbench_contracts import WorkbenchError


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    # Deterministic stand-in tests publication/migration, not DPAPI itself.
    monkeypatch.setattr(settings_module, "_crypt", lambda b, decrypt=False: base64.b64decode(b) if decrypt else base64.b64encode(b))
    return LauncherSettings(tmp_path)


def request(settings, **values):
    public = settings.public()
    return {"values": {**public["values"], **values}, "revision": public["revision"], "keys": {"outer": "test-outer-key", "inner": "test-inner-key"}}


def test_settings_atomic_update_conflict_and_secret_migration(settings):
    settings.path.write_text('# operator comment\nOTHER="literal${VALUE}"\nOPENAI_API_KEY="old-key"\n', encoding="utf-8")
    changed = request(settings)
    settings.save(changed)
    text = settings.path.read_text(encoding="utf-8")
    assert '# operator comment' in text and 'OTHER="literal${VALUE}"' in text
    assert "test-outer-key" not in text
    with pytest.raises(WorkbenchError, match="configuration_changed"):
        settings.save(changed)
    settings.migrate_key(settings.revision())
    assert "OPENAI_API_KEY" not in settings.path.read_text(encoding="utf-8")
    assert settings.secrets.get(model_secret_key("outer", DEFAULTS["OPENAI_BASE_URL"])) == "old-key"
    assert "old-key" not in json.dumps(settings.public())


def test_gui_configuration_reaches_each_real_adapter_and_freezes_live_binding(settings, monkeypatch):
    from agent_adapters import deepseek_roles as roles
    monkeypatch.setattr(roles, "_BOUND_ENDPOINTS", {})
    settings.save(request(settings, QINYUAN_INNER_MODEL="deepseek-v4-pro", QINYUAN_INNER_BASE_URL="https://api.deepseek.com"))
    outer, inner = roles.role_config("outer_planning", version=17), roles.role_config("inner_action", version=17)
    outer_settings, inner_settings = (roles.settings_from_file(r, settings.path) for r in (outer, inner))
    assert (outer_settings.model, outer_settings.api_key) == ("deepseek-flash", "test-outer-key")
    assert (inner_settings.model, inner_settings.api_key) == ("deepseek-v4-pro", "test-inner-key")
    model = roles.D8DeepSeekModel(inner, inner_settings, transport=object())
    assert dict(model.role_config.parameters)["reasoning_effort"] == "high"
    settings.save(request(settings, QINYUAN_INNER_MODEL="deepseek-v4-flash"))
    # Changing defaults must not invalidate an already assembled live model.
    roles.validate_role(inner)
    assert model.settings.model == "deepseek-v4-pro"
    assert roles.role_config("inner_action", version=17).model == "deepseek-v4-flash"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "text"])
def test_invalid_timeouts_do_not_publish_configuration(settings, value):
    with pytest.raises(WorkbenchError, match="timeout_invalid"):
        settings.save(request(settings, QINYUAN_LLM_CONNECT_TIMEOUT_SECONDS=value))
    assert not settings.path.exists()


def test_separate_role_endpoints_freeze_separate_capacity_and_credentials(settings):
    from agent_adapters import deepseek_roles as roles
    from agent_adapters.simulation import real_config
    settings.save(request(settings, QINYUAN_INNER_MODEL="DeepSeek-V4.1-Flash", QINYUAN_INNER_BASE_URL="https://api.scnet.cn/api/llm/v1"))
    frozen = real_config(contract_version=6)
    assert frozen["handoff_config"]["outer_capacity"] != frozen["handoff_config"]["inner_capacity"]
    assert frozen["model_transport"]["inner_base_url"] == "https://api.scnet.cn/api/llm/v1"
    assert roles.settings_from_file(roles.role_config("inner_action", version=17), settings.path).api_key == "test-inner-key"
    # A different endpoint must never inherit a credential saved for another source.
    changed = request(settings, QINYUAN_MODEL="DeepSeek-V4.1-Flash", OPENAI_BASE_URL="https://api.scnet.cn/api/llm/v1")
    changed["keys"] = {}
    assert settings.save(changed)["key_configured"]["outer"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_round_trip_and_ciphertext(tmp_path):
    store = SecretStore(tmp_path)
    store["outer"] = "only-a-test-key"
    assert store.get("outer") == "only-a-test-key"
    assert b"only-a-test-key" not in next(tmp_path.iterdir()).read_bytes()
    store.pop("outer")
    assert store.get("outer") is None


def test_fixed_build_commands_order_and_root_context(tmp_path, monkeypatch):
    from agent_adapters import launcher_setup as module
    source = Path(__file__).resolve().parent.parent
    definitions = image_definitions(source)
    setup = LauncherSetup(source, lambda: False)
    calls = []
    class Process:
        stdout = ["#1 CACHED\n", "https://private.example/token=hidden\n"]
        def wait(self): return 0
    monkeypatch.setattr(module.subprocess, "Popen", lambda command, **kw: calls.append((command, kw)) or Process())
    monkeypatch.setattr(setup, "check", lambda *args: [{"status": "failed", "name": "image", "detail": "environment.image_identity_mismatch"}])
    setup.state = {"status": "running", "action": "windows"}
    setup._prepare("windows")
    assert [call[0][call[0].index("--file") + 1] for call in calls] == [definitions[k][0] for k in ("linux", "windows")]
    assert all(command[-1] == "." and kw["cwd"] == source for command, kw in calls)
    assert "--provenance=false" in calls[0][0]
    assert setup.snapshot()["status"] == "needs_attention"
    assert "private.example" not in json.dumps(setup.snapshot())


def test_preparation_rejects_live_environment_and_unknown_actions(tmp_path):
    setup = LauncherSetup(tmp_path, lambda: True)
    with pytest.raises(WorkbenchError, match="environment_in_use"):
        setup.start("dependencies")
    with pytest.raises(WorkbenchError, match="action_invalid"):
        setup.start("arbitrary shell")


def test_desktop_bridge_rejects_unknown_host_action(tmp_path):
    desktop = Desktop(root=tmp_path)
    assert desktop.invoke("shell", "whoami")["error"] == "launcher.action_invalid"
    assert desktop.invoke("service_status")["data"] == {"running": False, "active": False}


def test_standalone_workbench_registers_for_desktop_status(tmp_path):
    import time
    root=Path(__file__).resolve().parent.parent
    directory=tmp_path/'data'
    env={k:v for k,v in os.environ.items() if k not in {'QINYUAN_DESKTOP_TOKEN','QINYUAN_DESKTOP_INSTANCE'}}
    process=subprocess.Popen([str(root/'.venv/Scripts/python.exe'),'-m','agent_adapters.workbench',
        '--port','0','--data-dir',str(directory)],cwd=root,env=env,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    desktop=Desktop(root=root,data_dir=directory)
    try:
        for _ in range(100):
            if desktop.record.exists():break
            assert process.poll() is None
            time.sleep(.1)
        status=desktop.invoke('service_status')
        assert status['ok'] and status['data']['running'] is True and status['data']['active'] is False
        desktop.request('POST','/api/desktop',{'action':'shutdown'})
        assert process.wait(10)==0
        assert desktop.invoke('service_status')['data']['running'] is False
    finally:
        if process.poll() is None:process.terminate();process.wait(10)


def test_native_close_callback_never_waits_for_javascript(tmp_path):
    desktop = Desktop(root=tmp_path)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    def evaluate(script):
        assert script == "window.launcherClose(); void 0"
        entered.set()
        assert release.wait(3)
    desktop.window = SimpleNamespace(evaluate_js=evaluate)
    desktop.on_loaded()
    results = []
    def close():
        results.append(desktop.on_closing())
        returned.set()
    caller = threading.Thread(target=close)
    caller.start()
    try:
        assert returned.wait(1), "Closing callback blocked the native UI thread"
        assert entered.wait(1)
        assert results == [False] and not desktop.closing
    finally:
        release.set(); caller.join(3)
    desktop.closing = True
    assert desktop.on_closing() is True


def test_close_without_working_page_preserves_background(tmp_path):
    desktop = Desktop(root=tmp_path)
    assert desktop.on_closing() is True
    desktop.on_loaded()
    closed = []
    def broken_page(_): raise RuntimeError("PRIVATE")
    desktop.window = SimpleNamespace(evaluate_js=broken_page, destroy=lambda: closed.append(True))
    desktop._show_close_prompt()
    assert closed == [True] and desktop.closing


def test_profiles_can_be_prepared_without_starting_workbench(tmp_path):
    desktop = Desktop(root=tmp_path)
    value = {"name": "local profile", "adapter": "arena", "base_url": "https://www.nssctf.cn", "timeout_seconds": 10}
    saved = desktop.invoke("profile_save", value)
    assert saved["ok"]
    assert desktop.invoke("profiles")["data"][0]["id"] == saved["data"]["id"]
    assert not desktop.record.exists()


def test_data_lease_blocks_duplicate_service_and_offline_edits(tmp_path):
    from agent_adapters.workbench import data_directory_lease
    with data_directory_lease(tmp_path):
        with pytest.raises(WorkbenchError, match="data_directory_in_use"):
            data_directory_lease(tmp_path)
    with data_directory_lease(tmp_path):
        pass


def test_background_copy_opacity_persistence_and_removal(settings, tmp_path):
    source = Path(__file__).resolve().parent.parent / "frontend/assets/launcher.png"
    original = settings.public()
    image = settings.background(source)
    assert image.startswith("data:image/png;base64,")
    assert LauncherSettings(settings.root).background() == image
    result = settings.save_opacity({"opacity": 65, "revision": original["revision"]})
    assert result["revision"] == settings.revision()
    assert LauncherSettings(settings.root).public()["values"]["QINYUAN_BACKGROUND_OPACITY"] == "65"
    assert not settings.public()["values"].get("QINYUAN_DESKTOP_SETTINGS")
    with pytest.raises(WorkbenchError, match="configuration_changed"):
        settings.save_opacity({"opacity": 30, "revision": original["revision"]})
    invalid = tmp_path / "fake.png"
    invalid.write_text("not an image", encoding="utf-8")
    with pytest.raises(WorkbenchError, match="background_format_invalid"):
        settings.background(invalid)
    assert settings.background() == image
    with pytest.raises(WorkbenchError, match="background_opacity_invalid"):
        settings.save_opacity({"opacity": 101, "revision": result["revision"]})
    settings.clear_background()
    assert settings.background() is None


def test_desktop_api_requires_session_and_preserves_origin_check(tmp_path, monkeypatch):
    import http.client
    from agent_adapters.workbench import Workbench, WorkbenchServer
    monkeypatch.setenv("QINYUAN_DESKTOP_TOKEN", "test-session-token")
    store = Workbench(tmp_path)
    server = WorkbenchServer(("127.0.0.1", 0), store)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        for token, origin, expected in (("", None, 403), ("wrong", None, 403), ("test-session-token", "https://other.test", 403), ("test-session-token", None, 200)):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            headers = {"X-Qinyuan-Desktop": token}
            if origin: headers["Origin"] = origin
            connection.request("GET", "/api/desktop", headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            assert response.status == expected
            if expected == 200:
                assert data["active"] is False and data["data_dir"] == str(tmp_path)
                assert "OPENAI_API_KEY" not in data["defaults"]
            connection.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(3)


def test_automatic_security_probe_never_requests_elevation(tmp_path, monkeypatch):
    import base64
    from agent_adapters.windows_security import WindowsSecuritySetup
    calls = []
    def run(command, **kwargs):
        calls.append(base64.b64decode(command[-1]).decode("utf-16le"))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    setup = WindowsSecuritySetup(tmp_path)
    result = setup._invoke("Probe")
    assert len(calls) == 1 and "RunAs" not in calls[0] and "Start-Process" not in calls[0]
    assert "-Action Check" in calls[0]
    assert result["status"] == "unknown"
