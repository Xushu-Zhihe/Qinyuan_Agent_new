"""Explicit, single-scope Defender setup for the local Workbench operator."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from agent_adapters.workbench_contracts import WorkbenchError

logger = logging.getLogger(__name__)


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode("utf-16-le")).decode("ascii")


class WindowsSecuritySetup:
    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve()
        self.script = Path(__file__).with_name("windows_defender_setup.ps1").resolve()
        self.supported = os.name == "nt"
        self.lock = threading.Lock()
        self.state = {
            "supported": self.supported,
            "data_dir": str(self.root),
            "script_path": str(self.script),
            "pattern": str(self.root / "runs" / "*" / "agent-state" / "*" / "stage-evidence"),
            "status": "unknown" if self.supported else "not_applicable",
            "reason": "not_checked" if self.supported else "windows_only",
            "busy": False,
            "verified": False,
            "checked_at": None,
        }

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, action: str):
        if action not in {"Check", "Apply", "Remove"}:
            raise WorkbenchError("windows_security_invalid_action")
        with self.lock:
            if not self.supported:
                raise WorkbenchError("windows_security_unsupported", 409)
            if self.state["busy"]:
                raise WorkbenchError("windows_security_operation_in_progress", 409)
            # A pending mutation invalidates the previous status until checked again.
            self.state.update(busy=True, action=action, status="unknown", verified=False,
                              reason="awaiting_administrator_or_check", checked_at=None)
            for key in ("exact_rule_present", "real_time_protection_enabled", "tamper_protected", "error_type", "exit_code", "errno", "winerror"):
                self.state.pop(key, None)
            self.worker = threading.Thread(target=self._run, args=(action,), daemon=True)
            self.worker.start()
            return dict(self.state)

    def _invoke(self, action: str):
        # No browser-supplied paths or shell commands cross the privilege boundary.
        powershell = str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        with tempfile.TemporaryDirectory(prefix="qinyuan-defender-") as temporary:
            result_path = Path(temporary) / "result.json"
            # Keep the receipt owned/readable by the unelevated caller.
            # A new file created by the elevated child can deny the caller access.
            result_path.write_text("", encoding="utf-8")
            inner = (f"$ErrorActionPreference='Stop'; try {{ & {_ps_literal(str(self.script))} -Action {action} "
                     f"-DataDir {_ps_literal(str(self.root))} -ResultPath {_ps_literal(str(result_path))} }} "
                     "catch { $reason = if ($_.Exception -is [System.Management.Automation.PSSecurityException]) "
                     "{ 'script_execution_blocked' } else { 'helper_failed_or_blocked' }; "
                     f"$r = @{{action='{action}';pattern={_ps_literal(self.state['pattern'])};status='unknown';"
                     "verified=$false;reason=$reason;error_type=$_.Exception.GetType().Name}; "
                     f"[IO.File]::WriteAllText({_ps_literal(str(result_path))}, ($r | ConvertTo-Json), "
                     "(New-Object System.Text.UTF8Encoding($false))); exit 2 }")
            # Operator-approved exception for this fixed helper process only.
            # No Set-ExecutionPolicy or global/user policy change; GPO still wins.
            arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand " + _encoded(inner)
            # RunAs shows the standard UAC consent prompt only after an explicit click.
            outer = ("try { $p = Start-Process -FilePath " + _ps_literal(powershell)
                     + " -ArgumentList " + _ps_literal(arguments)
                     + " -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop; exit $p.ExitCode } "
                     "catch { $e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }; "
                     "if ($e -is [System.ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq 1223) { exit 1223 }; exit 3 }")
            process = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(outer)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result_path.is_file() and result_path.stat().st_size:
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                except OSError as exc:
                    return {"status": "unknown", "verified": False, "reason": "helper_result_read_failed",
                            "error_type": type(exc).__name__, "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}
                if not isinstance(result, dict) or result.get("action") != action or result.get("pattern") != self.state["pattern"]:
                    raise ValueError("unexpected_setup_result")
                return result
            return {"status": "unknown", "verified": False,
                    "reason": "administrator_cancelled" if process.returncode == 1223 else "helper_failed_or_blocked",
                    "exit_code": process.returncode}

    def _run(self, action: str):
        logger.info("windows_security.operation_started action=%s", action)
        try:
            result = self._invoke(action)
            status = result.get("status", "unknown")
            verified = result.get("verified") is True and status in {"excluded", "not_excluded"}
            # Only known diagnostic fields are projected, never arbitrary helper output.
            details = {key: result[key] for key in (
                "reason", "exact_rule_present", "real_time_protection_enabled", "tamper_protected", "error_type", "exit_code", "errno", "winerror",
            ) if key in result}
            with self.lock:
                self.state.update(details, busy=False, status=status if verified else "unknown", verified=verified,
                                  checked_at=datetime.now(UTC).isoformat(timespec="seconds"))
                final_status, reason = self.state["status"], self.state["reason"]
            logger.info("windows_security.operation_finished action=%s status=%s reason=%s", action, final_status, reason)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            with self.lock:
                self.state.update(busy=False, status="unknown", verified=False, reason="helper_failed_or_blocked",
                                  error_type=type(exc).__name__, checked_at=datetime.now(UTC).isoformat(timespec="seconds"))
            logger.warning("windows_security.helper_failed action=%s error_type=%s", action, type(exc).__name__)
