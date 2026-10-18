"""
Tests for MCP Capability (Phase 1, Task 1.2).

Tests MCP tool execution through MCPCapability without requiring
full MCPManager implementation (uses mocks).
"""

import pytest
from dataclasses import dataclass
from typing import Any, Mapping


class TestMCPCapabilityImport:
    """Test: MCPCapability can be imported"""

    def test_import_mcp_capability(self):
        """Verify MCPCapability module exists"""
        from agent_adapters.mcp_capability import MCPCapability
        assert MCPCapability is not None

    def test_import_mcp_execution_result(self):
        """Verify MCPExecutionResult dataclass exists"""
        from agent_adapters.mcp_capability import MCPExecutionResult
        assert MCPExecutionResult is not None

    def test_import_mcp_plan_binding(self):
        """Verify MCPPlanBinding dataclass exists"""
        from agent_adapters.mcp_capability import MCPPlanBinding
        assert MCPPlanBinding is not None


class TestMCPCapabilityStructure:
    """Test: MCPCapability has required methods"""

    def test_mcp_capability_init(self):
        """Verify MCPCapability constructor signature"""
        from agent_adapters.mcp_capability import MCPCapability

        # Mock dependencies
        class MockMCPManager:
            def list_registered_tools(self):
                return {}

        class MockSandboxAdapter:
            def get_session(self, agent_id):
                return None

        capability = MCPCapability(
            mcp_manager=MockMCPManager(),
            sandbox_adapter=MockSandboxAdapter(),
        )

        assert capability is not None
        assert hasattr(capability, 'mcp_manager')
        assert hasattr(capability, 'sandbox_adapter')

    def test_mcp_capability_has_run_plan(self):
        """Verify run_plan method exists"""
        from agent_adapters.mcp_capability import MCPCapability

        class MockMCPManager:
            def list_registered_tools(self):
                return {}

        class MockSandboxAdapter:
            def get_session(self, agent_id):
                return None

        capability = MCPCapability(
            mcp_manager=MockMCPManager(),
            sandbox_adapter=MockSandboxAdapter(),
        )

        assert hasattr(capability, 'run_plan')
        assert callable(capability.run_plan)


class TestMCPExecutionResult:
    """Test: MCPExecutionResult dataclass"""

    def test_mcp_execution_result_fields(self):
        """Verify MCPExecutionResult has required fields"""
        from agent_adapters.mcp_capability import MCPExecutionResult

        result = MCPExecutionResult(
            tool_name="test_tool",
            success=True,
            output='{"result": "ok"}',
            execution_time_ms=123,
        )

        assert result.tool_name == "test_tool"
        assert result.success is True
        assert result.output == '{"result": "ok"}'
        assert result.execution_time_ms == 123


class TestMCPPlanBinding:
    """Test: MCPPlanBinding dataclass"""

    def test_mcp_plan_binding_fields(self):
        """Verify MCPPlanBinding has base D3PlanBinding fields + MCP fields"""
        from agent_adapters.mcp_capability import MCPPlanBinding
        from agent_core.actions import MCPAction, MCPToolCall

        action = MCPAction(
            calls=(
                MCPToolCall(
                    tool_name="test_tool",
                    parameters={"arg": "value"},
                    timeout_seconds=30,
                ),
            ),
            working_directory="/workspace",
            conditions=(),
        )

        binding = MCPPlanBinding(
            task_run_id="task_123",
            turn_id="turn_456",
            intent_id="intent_789",
            plan_id="plan_abc",
            observation_mode="new_observation",
            artifact_version_id="artifact_v1",
            proposal_request_id="proposal_001",
            rule_binding_sha256="a" * 64,
            execution_id="exec_xyz",
            kind="mcp_action",
            action=action,
            agent_id="agent_001",
        )

        # Base D3PlanBinding fields
        assert binding.task_run_id == "task_123"
        assert binding.turn_id == "turn_456"
        assert binding.plan_id == "plan_abc"

        # MCP-specific fields
        assert binding.kind == "mcp_action"
        assert binding.action == action
        assert binding.agent_id == "agent_001"
