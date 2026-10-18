#!/usr/bin/env python3
"""
D22 Phase 5: Minimal Validation Test Runner
Run minimal validation tests for completed Phases 1 & 2
"""

import sys
import subprocess
from pathlib import Path


def run_tests():
    """Run pytest with minimal validation tests"""
    test_files = [
        "tests/test_mcp_execution_chain.py",
        "tests/test_fact_lifecycle.py"
    ]

    print("=" * 60)
    print("D22 Phase 5: Minimal Validation Testing")
    print("=" * 60)
    print()
    print("Testing scope:")
    print("  - Phase 1: MCP execution chain (action → executor → observation)")
    print("  - Phase 2: Fact lifecycle (proposal → review → storage)")
    print()

    # Check if pytest is available
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "--version"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print("ERROR: pytest not found. Install with: pip install pytest pytest-asyncio")
            return 1
    except FileNotFoundError:
        print("ERROR: Python not found in PATH")
        return 1

    # Run tests
    for test_file in test_files:
        if not Path(test_file).exists():
            print(f"WARNING: {test_file} not found, skipping")
            continue

        print(f"\nRunning {test_file}...")
        print("-" * 60)

        result = subprocess.run(
            ["python", "-m", "pytest", test_file, "-v", "--tb=short"],
            cwd=Path(__file__).parent.parent
        )

        if result.returncode != 0:
            print(f"\n✗ {test_file} FAILED")
            return 1
        else:
            print(f"\n✓ {test_file} PASSED")

    print()
    print("=" * 60)
    print("[PASS] All minimal validation tests passed")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
