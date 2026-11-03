"""Non-secret operational settings resolve from the repo .env without secrets."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from Sandbox import env_settings
from Sandbox.tool_download import ToolDownload, _configured_allowed_hosts, host_allowed


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(env_settings, "ENV_FILE", path)
    monkeypatch.delenv("QINYUAN_DOWNLOAD_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS", raising=False)
    return path


def test_download_allowlist_falls_back_to_env_file(env_file):
    env_file.write_text(
        "QINYUAN_DOWNLOAD_ALLOWED_HOSTS=github.com,.githubusercontent.com\n",
        encoding="utf-8",
    )
    hosts = _configured_allowed_hosts()
    assert hosts == frozenset({"github.com", ".githubusercontent.com"})
    assert host_allowed("github.com", hosts)
    assert host_allowed("release-assets.githubusercontent.com", hosts)
    assert not host_allowed("evil.test", hosts)


def test_download_broker_accepts_github_from_env_file(env_file):
    env_file.write_text(
        "QINYUAN_DOWNLOAD_ALLOWED_HOSTS=github.com,.githubusercontent.com\n",
        encoding="utf-8",
    )
    broker = ToolDownload(
        SimpleNamespace(_runtime=SimpleNamespace(client=object())),
        SimpleNamespace(task_run_id=str(uuid4()), target_network=None),
        str(uuid4()),
        SimpleNamespace(),
    )
    assert broker._source_allowed("https://github.com/upx/upx/releases/download/v5.3.0/x")
    assert not broker._source_allowed("https://evil.test/x")


def test_process_environment_wins_over_env_file(env_file, monkeypatch):
    env_file.write_text("QINYUAN_DOWNLOAD_ALLOWED_HOSTS=file.test\n", encoding="utf-8")
    monkeypatch.setenv("QINYUAN_DOWNLOAD_ALLOWED_HOSTS", "env.test")
    assert _configured_allowed_hosts() == frozenset({"env.test"})


def test_secrets_in_env_file_are_never_served(env_file):
    env_file.write_text(
        "OPENAI_API_KEY=sk-secret\nQINYUAN_DOWNLOAD_ALLOWED_HOSTS=github.com\n",
        encoding="utf-8",
    )
    assert env_settings.configured("OPENAI_API_KEY") is None
    assert env_settings.configured("QINYUAN_DOWNLOAD_ALLOWED_HOSTS") == "github.com"


def test_bootstrap_timeout_falls_back_to_env_file(env_file):
    env_file.write_text("QINYUAN_TOOL_BOOTSTRAP_TIMEOUT_SECONDS=180\n", encoding="utf-8")
    from MCPs.Download_Mcp import server

    assert server._configured_timeout() == 180


def test_missing_env_file_keeps_fail_closed(env_file):
    assert _configured_allowed_hosts() == frozenset()
    from MCPs.Download_Mcp import server

    assert server._configured_timeout() == 300
