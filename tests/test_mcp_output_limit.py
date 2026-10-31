"""Bounded tool output is previewed, but total byte counts stay observable."""
import inspect

from MCPs.Mcp_client.client import MCPExecutionResult, MCPManager
from Sandbox.docker_runtime import _BoundedByteBuffer
from Sandbox.settings import SandboxSettings


def test_mcp_preview_default_is_64k_characters_and_keeps_head_and_tail():
    manager = object.__new__(MCPManager)
    manager.output_character_limit = 64_000
    output = "A" * 50_000 + "B" * 50_000
    preview, truncated = manager._truncate_output(output)
    assert truncated is True
    assert preview.startswith("A")
    assert preview.endswith("B")
    assert "characters omitted" in preview
    assert len(preview) < len(output)
    assert inspect.signature(MCPManager).parameters["output_character_limit"].default == 64_000


def test_bounded_buffer_reports_original_total_bytes():
    buffer = _BoundedByteBuffer(1024)
    chunk = b"x" * 4096
    for _ in range(512):  # 2 MiB
        buffer.append(chunk)
    rendered, truncated, total = buffer.render()
    assert truncated is True
    assert total == 2 * 1024 * 1024
    assert len(rendered) < total
    assert b"bytes omitted by Sandbox Runtime" in rendered


def test_execution_payload_exposes_total_bytes():
    result = MCPExecutionResult(
        agent_id="task",
        sandbox_id="sandbox",
        qualified_name="shell.run_bash",
        executable="bash",
        arguments=(),
        status="completed",
        exit_code=0,
        stdout="preview",
        stderr="",
        timed_out=False,
        stdout_truncated=True,
        stdout_bytes=13_500_000,
    )
    payload = result.to_tool_payload()
    assert payload["stdout_truncated"] is True
    assert payload["stdout_bytes"] == 13_500_000


def test_sandbox_output_limit_is_environment_configurable(monkeypatch):
    monkeypatch.setenv("QINYUAN_SANDBOX_MAX_OUTPUT_BYTES", "1048576")
    settings = SandboxSettings.from_environment()
    assert settings.maximum_output_bytes_per_stream == 1_048_576
