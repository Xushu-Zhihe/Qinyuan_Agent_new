from __future__ import annotations

import unittest

from src.server import dirsearch_quick_scan


class DirsearchPlannerTests(unittest.TestCase):
    """只测试命令规划，不启动进程，也不执行宿主机 dirsearch。"""

    def test_quick_scan_returns_sandbox_command(self) -> None:
        result = dirsearch_quick_scan("http://target-web")
        plan = result["sandbox_command"]

        self.assertEqual(plan["tool"], "dirsearch")
        self.assertIn("http://target-web", plan["arguments"])
        self.assertGreaterEqual(plan["timeout_seconds"], 5)

    def test_unauthorized_target_is_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            dirsearch_quick_scan("https://example.com")


if __name__ == "__main__":
    unittest.main()
