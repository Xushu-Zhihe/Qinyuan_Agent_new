"""Workbench API contract; no model, Docker, or challenge network calls."""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
from pathlib import Path

from agent_adapters.workbench import Workbench, WorkbenchServer


def call(port, method, path, body=None, *, origin=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"127.0.0.1:{port}"}
    if origin:
        headers["Origin"] = origin
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif isinstance(body, bytes):
        headers["Content-Type"] = "application/octet-stream"
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = response.read()
    connection.close()
    return response.status, json.loads(result)


def challenge(name="one"):
    return {
        "title": name,
        "category": "pwn",
        "description": "local test",
        "endpoint": "http://example.test:24009/",
        "deadline_minutes": 60,
        "step_timeout_seconds": 60,
    }


def test_workbench_lifecycle_and_secret_boundary():
    Path(".test").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".test") as temporary:
        store = Workbench(Path(temporary))
        server = WorkbenchServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        try:
            status, first = call(port, "POST", "/api/challenges", challenge())
            assert status == 201 and first["attachments"] == []
            status, missing_key = call(port, "POST", f"/api/challenges/{first['id']}/run")
            assert status == 409 and missing_key["error"] == "model_key_not_configured"
            status, progress = call(port, "GET", f"/api/challenges/{first['id']}/progress")
            assert status == 200 and progress["progress"] is None
            status, health = call(port, "GET", "/api/health")
            assert health["capabilities"]["current_cli"] == "t6_run_task_real"
            status, second = call(port, "POST", "/api/challenges", challenge("two"))
            assert status == 201 and second["id"] != first["id"]
            data = b"binary\x00content"
            status, uploaded = call(port, "POST", f"/api/challenges/{first['id']}/attachments?name=pwn", data)
            assert status == 201 and uploaded["attachments"][0]["byte_size"] == len(data)
            assert (store.uploads / first["id"] / uploaded["attachments"][0]["id"]).read_bytes() == data
            assert len(Workbench(Path(temporary)).list()) == 2
            status, rejected = call(port, "POST", f"/api/challenges/{first['id']}/purge", {"confirm_title": "one"})
            assert status == 409 and rejected["error"] == "archive_terminal_challenge_first"
            status, archived = call(port, "POST", f"/api/challenges/{first['id']}/archive", {})
            assert status == 200 and archived["archived"] is True
            status, _ = call(port, "POST", f"/api/challenges/{first['id']}/restore", {})
            assert status == 200
            status, _ = call(port, "POST", f"/api/challenges/{first['id']}/archive", {})
            assert status == 200
            status, mismatch = call(port, "POST", f"/api/challenges/{first['id']}/purge", {"confirm_title": "wrong"})
            assert status == 400 and mismatch["error"] == "confirmation_title_mismatch"
            status, removed = call(port, "POST", f"/api/challenges/{first['id']}/purge", {"confirm_title": "one"})
            assert status == 200 and removed["purged"] is True
            assert not (store.uploads / first["id"]).exists()
            assert len(Workbench(Path(temporary)).list()) == 1
            secret = "test-secret-only-in-process"
            status, model = call(port, "PUT", "/api/model", {
                "provider": "openai_compatible", "model": "test-model", "base_url": "https://model.example.test/v1", "api_key": secret,
            })
            assert status == 200 and model["key_configured"] is True
            assert secret not in json.dumps(model)
            assert secret not in (store.database).read_bytes().decode("utf-8", errors="ignore")
            assert Workbench(Path(temporary)).model_status()["key_configured"] is False
            status, cleared = call(port, "DELETE", "/api/model/key")
            assert status == 200 and cleared["key_configured"] is False
            status, denied = call(port, "GET", "/api/health", origin="http://evil.example")
            assert status == 403 and denied["error"] == "invalid_origin"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_workbench_streams_attachment_beyond_old_16_mib_limit():
    Path(".test").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".test") as temporary:
        store = Workbench(Path(temporary))
        task = store.create(challenge())
        from io import BytesIO

        body = b"x" * (16 * 1024 * 1024 + 1)
        result = store.add_attachment(task["id"], "large.bin", BytesIO(body), len(body))
        assert result["attachments"][0]["byte_size"] == len(body)
        assert (store.uploads / task["id"] / result["attachments"][0]["id"]).stat().st_size == len(body)


def test_remote_endpoint_is_optional_but_a_supplied_target_needs_a_port():
    Path(".test").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".test") as temporary:
        store = Workbench(Path(temporary))
        server = WorkbenchServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        try:
            for category, endpoint_value in (("misc", None), ("crypto", "")):
                payload = challenge(category)
                payload["category"] = category
                if endpoint_value is None:
                    del payload["endpoint"]
                else:
                    payload["endpoint"] = endpoint_value
                status, created = call(port, "POST", "/api/challenges", payload)
                assert status == 201 and created["endpoint"] == "" and created["attachments"] == []
                assert store.get(created["id"])["endpoint"] == ""

            invalid_budget = {**payload, "step_timeout_seconds": 301}
            status, rejected = call(port, "PUT", f"/api/challenges/{created['id']}", invalid_budget)
            assert status == 400 and rejected["error"] == "invalid_step_timeout"

            targeted = challenge("targeted")
            status, created = call(port, "POST", "/api/challenges", targeted)
            assert status == 201 and created["endpoint"] == targeted["endpoint"]
            targeted["endpoint"] = None
            status, updated = call(port, "PUT", f"/api/challenges/{created['id']}", targeted)
            assert status == 200 and updated["endpoint"] == ""

            for invalid in ("http://example.test/", "tcp://example.test", "http://example.test:0/"):
                targeted["endpoint"] = invalid
                status, rejected = call(port, "PUT", f"/api/challenges/{created['id']}", targeted)
                assert status == 400 and rejected["error"] == "invalid_endpoint"
                assert store.get(created["id"])["endpoint"] == ""
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
