"""Private on-demand tool download counter."""

from Sandbox import tool_usage


def _use_temp_counter(tmp_path, monkeypatch):
    path = tmp_path / "counts.sqlite"
    monkeypatch.setenv(tool_usage.COUNTER_ENV, str(path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return path


def test_counter_increments_and_is_not_cleared(tmp_path, monkeypatch):
    _use_temp_counter(tmp_path, monkeypatch)
    tool_usage.record("upx")
    tool_usage.record("upx")
    tool_usage.record("lief")
    data = tool_usage.counts()
    assert data["upx"]["count"] == 2
    assert data["lief"]["count"] == 1

    # A later record keeps the existing state instead of resetting it.
    tool_usage.record("upx")
    assert tool_usage.counts()["upx"]["count"] == 3


def test_install_tool_records_a_download_request(tmp_path, monkeypatch):
    _use_temp_counter(tmp_path, monkeypatch)
    from MCPs.Download_Mcp.server import install_tool, list_catalog

    upx = next(entry for entry in list_catalog()["native"] if entry["name"] == "upx")
    plan = install_tool(**upx)
    assert plan["request"]["operation"] == "tool_bootstrap"
    assert tool_usage.counts()["upx"]["count"] == 1


def test_pytest_runs_do_not_pollute_the_real_counter(tmp_path, monkeypatch):
    _use_temp_counter(tmp_path, monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_tool_usage.py::fake")
    tool_usage.record("upx")
    assert tool_usage.counts() == {}


def test_default_counter_is_private_and_under_workbench_data():
    assert "workbench_data" in str(tool_usage.DEFAULT_COUNTER)
    assert tool_usage.DEFAULT_COUNTER.name.endswith(".sqlite")
