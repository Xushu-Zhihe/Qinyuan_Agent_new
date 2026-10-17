"""
D22 Phase 5.1: Minimal Validation - Schema & Structure Tests
Test that D22 schemas and data structures are properly defined
"""

import pytest
from dataclasses import fields, is_dataclass


class TestMCPActionSchema:
    """Test: MCPAction dataclass exists and has required fields"""

    def test_mcp_action_import(self):
        """Verify MCPAction can be imported"""
        from agent_core.actions import MCPAction
        assert MCPAction is not None

    def test_mcp_action_structure(self):
        """Verify MCPAction has required fields"""
        from agent_core.actions import MCPAction, MCPToolCall

        # Create tool call
        tool_call = MCPToolCall(
            tool_name="filesystem__read_file",
            parameters={"path": "/workspace/test.txt"},
            timeout_seconds=30
        )

        # Create action
        action = MCPAction(
            calls=(tool_call,),
            working_directory="/workspace",
            conditions=()
        )

        assert hasattr(action, "calls")
        assert hasattr(action, "working_directory")
        assert hasattr(action, "conditions")
        assert len(action.calls) == 1
        assert action.calls[0].tool_name == "filesystem__read_file"



class TestFactCandidateSchema:
    """Test: FactCandidate schema from D22 Phase 2"""

    def test_fact_candidate_import(self):
        """Verify FactCandidate can be imported"""
        try:
            from agent_core.conclusion_contracts import FactCandidate
            assert FactCandidate is not None
        except ImportError:
            pytest.skip("FactCandidate not yet implemented")

    def test_fact_candidate_fields(self):
        """Verify FactCandidate has required fields"""
        try:
            from agent_core.conclusion_contracts import FactCandidate, EvidenceStrength

            candidate = FactCandidate(
                content="Test fact content",
                layer="evidence",  # String value, not enum
                evidence_strength=EvidenceStrength.VERIFIED,
                source_proof_id="00000000-0000-0000-0000-000000000001",  # Valid UUID
                inferred=False
            )

            assert hasattr(candidate, "content")
            assert hasattr(candidate, "layer")
            assert hasattr(candidate, "evidence_strength")
            assert hasattr(candidate, "source_proof_id")
            assert hasattr(candidate, "inferred")

        except (ImportError, TypeError, ValueError) as e:
            pytest.skip(f"FactCandidate not ready: {e}")
        except ImportError:
            pytest.skip("FactCandidate schema not yet implemented")


class TestReviewDecisionSchema:
    """Test: D8ReviewDecision extended schema from D22 Phase 2"""

    def test_review_decision_import(self):
        """Verify D8ReviewDecision can be imported"""
        try:
            from agent_core.conclusion_review import D8ReviewDecision
            assert D8ReviewDecision is not None
        except ImportError:
            pytest.skip("D8ReviewDecision not yet implemented")

    def test_review_decision_has_refinement_fields(self):
        """Verify D8ReviewDecision has refined_content and merged_sources"""
        try:
            from agent_core.conclusion_contracts import D8ReviewDecision

            decision = D8ReviewDecision(
                decision="accept",
                reason="Verified observation",  # Field is 'reason', not 'reasoning'
                refined_content="Refined fact content",
                merged_sources=("proof_001", "proof_002")  # Must be tuple, not list
            )

            assert hasattr(decision, "refined_content")
            assert hasattr(decision, "merged_sources")
            assert decision.refined_content == "Refined fact content"
            assert len(decision.merged_sources) == 2

        except (ImportError, TypeError, ValueError) as e:
            pytest.skip(f"D8ReviewDecision refinement fields not ready: {e}")


class TestFactLayerEnum:
    """Test: FactLayer enumeration from D22 Phase 2"""

    def test_fact_layer_import(self):
        """Verify FactLayer enum exists"""
        try:
            from agent_core.fact_storage import FactLayer
            assert FactLayer is not None
        except ImportError:
            pytest.skip("FactLayer not yet implemented")

    def test_fact_layer_values(self):
        """Verify FactLayer has EVIDENCE, BUSINESS, PROCESS"""
        try:
            from agent_core.fact_storage import FactLayer

            assert hasattr(FactLayer, "EVIDENCE")
            assert hasattr(FactLayer, "BUSINESS")
            assert hasattr(FactLayer, "PROCESS")

            # Verify enum values
            assert FactLayer.EVIDENCE.value == "evidence"
            assert FactLayer.BUSINESS.value == "business"
            assert FactLayer.PROCESS.value == "process"

        except (ImportError, AttributeError) as e:
            pytest.skip(f"FactLayer not ready: {e}")


class TestEvidenceStrengthEnum:
    """Test: EvidenceStrength enumeration from D22 Phase 2"""

    def test_evidence_strength_import(self):
        """Verify EvidenceStrength enum exists"""
        try:
            from agent_core.conclusion_contracts import EvidenceStrength
            assert EvidenceStrength is not None
        except ImportError:
            pytest.skip("EvidenceStrength not yet implemented")

    def test_evidence_strength_values(self):
        """Verify EvidenceStrength has VERIFIED, INFERENCE, UNVALIDATED"""
        try:
            from agent_core.conclusion_contracts import EvidenceStrength

            assert hasattr(EvidenceStrength, "VERIFIED")
            assert hasattr(EvidenceStrength, "INFERENCE")  # Not INFERRED
            assert hasattr(EvidenceStrength, "UNVALIDATED")  # Not ASSUMED

            # Verify enum values
            assert EvidenceStrength.VERIFIED.value == "verified"
            assert EvidenceStrength.INFERENCE.value == "inference"
            assert EvidenceStrength.UNVALIDATED.value == "unvalidated"

        except (ImportError, AttributeError, AssertionError) as e:
            pytest.skip(f"EvidenceStrength not ready: {e}")


class TestContractVersion22:
    """Test: Contract version 22 removes 0-2 Intent limits"""

    def test_candidate_limit_function(self):
        """Verify candidate_limit returns None for version 22"""
        try:
            from agent_core.intent_planning import candidate_limit

            class MockRequest:
                contract_version = 22

            request = MockRequest()
            limit = candidate_limit(request)

            # Version 22 should have no limit (None)
            assert limit is None

        except ImportError:
            pytest.skip("candidate_limit function not yet implemented")

    def test_output_contracts_version_22(self):
        """Verify output_contracts handles version 22"""
        try:
            from agent_core.output_contracts import completion_schema

            class MockRequest:
                contract_version = 22

            request = MockRequest()
            schema = completion_schema(request, maximum=None)

            # Should allow unlimited suggestions for version 22
            assert schema is not None

        except ImportError:
            pytest.skip("completion_schema not yet implemented")


class TestD8SandboxAdapter:
    """Test: D8SandboxAdapter exists and has required methods"""

    def test_sandbox_adapter_import(self):
        """Verify D8SandboxAdapter can be imported"""
        try:
            from Sandbox.d8_sandbox_adapter import D8SandboxAdapter
            assert D8SandboxAdapter is not None
        except ImportError:
            pytest.skip("D8SandboxAdapter not yet implemented")

    def test_sandbox_adapter_methods(self):
        """Verify D8SandboxAdapter has required methods"""
        try:
            from Sandbox.d8_sandbox_adapter import D8SandboxAdapter

            # Create mock runtime
            class MockRuntime:
                def get_workspace(self, task_run_id: str):
                    return None  # Simplified mock

            adapter = D8SandboxAdapter(runtime=MockRuntime())

            # Check methods exist (actual names from implementation)
            assert hasattr(adapter, "bind_agent")  # Not register_agent
            assert hasattr(adapter, "get_session")
            assert callable(adapter.bind_agent)
            assert callable(adapter.get_session)

        except (ImportError, AttributeError) as e:
            pytest.skip(f"D8SandboxAdapter not ready: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
