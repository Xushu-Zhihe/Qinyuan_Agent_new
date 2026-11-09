"""Optional browser interaction checks using the desktop verification environment."""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from agent_adapters.launcher_settings import DEFAULTS, TIMEOUTS


def test_four_module_navigation_settings_and_preparation(tmp_path):
    root = Path(__file__).resolve().parent.parent
    settings = {"values": DEFAULTS, "revision": "test-revision", "key_configured": {"outer": False, "inner": False},
                "plaintext_key_present": False, "timeouts": {k: {"default": v[0], "label": v[1], "unit": v[2]} for k, v in TIMEOUTS.items()}}
    fixture = {"settings": settings, "templates": [
        {"id": "arena", "name": "NSSCTF Arena", "base_url": "https://www.nssctf.cn", "credential_label": "Agent Token", "note": "平台领取题目；单题推进。"},
        {"id": "benchmark", "name": "TSecBench", "base_url": "", "credential_label": "BENCHMARK_TOKEN", "note": "支持 1–3 个题目槽位；获取提示会扣分。"}], "service": False}
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1160, "height": 790}, device_scale_factor=1)
        errors, calls = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        def invoke(action, value):
            calls.append((action, value))
            data = None
            if action == "snapshot": data = fixture
            elif action == "profiles": data = []
            elif action == "security": data = {"status": "unknown", "supported": True, "reason": "administrator_required_to_verify_or_configure", "patterns": [], "busy": False}
            elif action == "settings":
                fixture["settings"]["values"] = value["values"]
                data = fixture["settings"]
            elif action == "choose_background":
                import base64
                data = {"image": "data:image/png;base64," + base64.b64encode((root / "frontend/assets/launcher.png").read_bytes()).decode()}
            elif action == "background_opacity": data = {"revision": "appearance-revision"}
            elif action == "check": data = [{"name": "Docker Engine", "status": "passed", "detail": "可用"}]
            elif action in {"prepare", "progress"}: data = {"status": "completed", "logs": ["[download] 核验完成"]}
            elif action == "service_status": data = {"active": True}
            return {"ok": True, "data": data}
        page.expose_function("desktopInvoke", invoke)
        page.add_init_script("window.pywebview={api:{invoke:(action,value)=>window.desktopInvoke(action,value)}};")
        page.goto((root / "frontend/launcher.html").as_uri())
        page.get_by_role("button", name="TSecBench", exact=False).wait_for()
        page.get_by_role("button", name="TSecBench", exact=False).click()
        assert page.locator("#launch-concurrency").is_enabled()
        screenshot = os.environ.get("QINYUAN_UI_SCREENSHOT")
        if screenshot:
            page.screenshot(path=screenshot, full_page=True)
        page.locator('[data-tab="settings"]').click()
        page.locator("#shared-inner").uncheck()
        page.locator('[data-key="QINYUAN_INNER_MODEL"]').fill("deepseek-v4-pro")
        page.locator('[data-key="QINYUAN_INNER_BASE_URL"]').fill("https://api.deepseek.com")
        page.get_by_role("button", name="保存模型连接").click()
        page.wait_for_function("document.getElementById('notice').textContent.includes('配置已保存')")
        saved = next(v for a, v in calls if a == "settings")
        assert saved["values"]["QINYUAN_INNER_MODEL"] == "deepseek-v4-pro"
        page.locator('[data-pane="appearance"]').click()
        page.locator("#choose-background").click()
        page.wait_for_function("document.body.classList.contains('has-background')")
        page.locator("#background-opacity").fill("65")
        page.locator("#background-opacity").dispatch_event("input")
        page.locator("#background-opacity").dispatch_event("change")
        page.wait_for_function("document.getElementById('launcher-background').style.opacity === '0.65'")
        assert page.locator("#background-opacity-value").text_content() == "65%"
        assert next(v for a,v in calls if a=="background_opacity")["opacity"] == 65
        page.locator("#clear-background").click()
        page.wait_for_function("!document.body.classList.contains('has-background')")
        page.locator('[data-tab="checks"]').click()
        page.get_by_text("Docker Engine", exact=True).wait_for()
        page.locator('[data-tab="downloads"]').click()
        page.locator('[data-pane="build"]').click()
        page.locator('[data-prepare="windows"]').click()
        page.wait_for_function("document.getElementById('download-state').textContent.includes('已完成')")
        assert ("prepare", "windows") in calls
        page.evaluate("window.launcherClose()")
        page.locator("#close-dialog[open]").wait_for()
        assert errors == []
        browser.close()


def test_workbench_keeps_two_views_and_task_editor(tmp_path):
    root = Path(__file__).resolve().parent.parent
    record = tmp_path / "instance.json"
    env = {**os.environ, "QINYUAN_DESKTOP_INSTANCE": str(record), "QINYUAN_DESKTOP_TOKEN": "ui-verification-session"}
    process = subprocess.Popen([str(root / ".venv/Scripts/python.exe"), "-m", "agent_adapters.workbench", "--port", "0", "--data-dir", str(tmp_path / "data")],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(100):
            if record.exists(): break
            assert process.poll() is None, "Workbench failed to start"
            time.sleep(.1)
        instance = json.loads(record.read_text(encoding="utf-8"))
        url = f'http://127.0.0.1:{instance["port"]}'
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            assert page.locator(".nav-item").count() == 2
            page.locator('[data-view="competition"]').click()
            page.get_by_role("heading", name="比赛控制", exact=True).wait_for()
            playwright.expect(page.locator("#competition-status")).to_contain_text("桌面")
            page.locator('[data-view="tasks"]').click()
            page.locator("#new-task").click()
            page.locator("#task-title").fill("UI verification")
            page.locator("#save-task").click()
            import re
            playwright.expect(page.locator("#task-id")).to_have_value(re.compile(r"[a-f0-9-]{36}"))
            assert errors == []
            browser.close()
        request = urllib.request.Request(url + "/api/desktop", data=b'{"action":"shutdown"}',
            headers={"Content-Type": "application/json", "X-Qinyuan-Desktop": instance["token"]})
        with urllib.request.urlopen(request) as response: assert response.status == 200
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(10)
