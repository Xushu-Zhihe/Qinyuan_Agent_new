"""On-demand tool catalog for specialized tooling."""

import json

from MCPs.Download_Mcp.server import list_catalog
from agent_core.stage_planning import TOOL_PREPARATION_GUIDANCE


def test_catalog_has_a_verified_native_upx_recipe():
    catalog = list_catalog()
    upx = next(entry for entry in catalog["native"] if entry["name"] == "upx")
    assert upx["url"].startswith("https://github.com/upx/upx/releases/download/")
    assert len(upx["sha256"]) == 64
    assert upx["archive_format"] == "tar.xz"
    assert upx["executable_path"].endswith("/upx")
    assert upx["self_test_args"] == ["--version"]


def test_catalog_separates_python_manual_and_unsupported_tools():
    catalog = list_catalog()
    python_names = {entry["name"] for entry in catalog["python"]}
    assert {"pyelftools", "lief", "unicorn", "keystone", "impacket"} <= python_names
    assert all(
        {"python_package", "python_version", "python_module", "self_test_args"} <= set(entry)
        for entry in catalog["python"]
    )
    assert catalog["manual"][0]["name"] == "apktool"
    assert catalog["manual"][0]["note"].startswith("jar is not an executable")
    assert any(entry["name"] == "ghidra" for entry in catalog["unsupported"])
    assert any(entry["name"] == "blutter" for entry in catalog["unsupported"])


def test_catalog_is_json_serializable_and_has_no_secrets():
    text = json.dumps(list_catalog())
    assert "OPENAI_API_KEY" not in text
    assert "sk-" not in text
    assert "QINYUAN_DOWNLOAD_ALLOWED_HOSTS" not in text


def test_tool_preparation_guidance_points_to_the_catalog():
    assert "download.list_catalog" in TOOL_PREPARATION_GUIDANCE
    assert "unsupported" in TOOL_PREPARATION_GUIDANCE
