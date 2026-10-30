"""Defender configuration tests use mocks only; never alter host security."""

import json
import os
import base64
import re
import subprocess
import threading
from pathlib import Path

import pytest

from agent_adapters.windows_security import WindowsSecuritySetup, _encoded, _ps_literal
from agent_adapters.workbench_contracts import WorkbenchError


def test_explicit_operation_scope_progress_and_single_admission(tmp_path, monkeypatch):
    setup = WindowsSecuritySetup(tmp_path / "data with 'quote")
    setup.supported = True
    setup.state["supported"] = True
    entered, release = threading.Event(), threading.Event()
    actions = []

    def invoke(action):
        actions.append(action)
        entered.set()
        assert release.wait(3)
        return {"status": "excluded", "verified": True, "reason": None, "exact_rule_present": True,
                "real_time_protection_enabled": True, "unrelated_secret": "do-not-project"}

    monkeypatch.setattr(setup, "_invoke", invoke)
    assert setup.snapshot()["reason"] == "not_checked"
    assert actions == []  # GET/snapshot must never launch a process or show UAC.
    assert setup.start("Apply")["busy"] is True
    assert entered.wait(3)
    try:
        with pytest.raises(WorkbenchError, match="operation_in_progress"):
            setup.start("Remove")
        assert setup.snapshot()["verified"] is False
    finally:
        release.set()
        setup.worker.join(3)
    final = setup.snapshot()
    assert final["status"] == "excluded" and final["verified"] and not final["busy"]
    assert final["checked_at"] and "unrelated_secret" not in final
    assert final["data_dir"] == str(setup.root)
    assert final["pattern"] == str(setup.root / "runs" / "*" / "agent-state" / "*" / "stage-evidence")
    assert actions == ["Apply"]


@pytest.mark.parametrize("failure", ["cancelled", "invalid_result", "blocked"])
def test_failed_or_cancelled_setup_never_reports_configured(tmp_path, monkeypatch, failure):
    setup = WindowsSecuritySetup(tmp_path)
    setup.supported = True

    def invoke(_action):
        if failure == "blocked":
            raise OSError("test permission failure")
        if failure == "invalid_result":
            return {"status": "excluded", "verified": False, "reason": "defender_path_check_unavailable"}
        return {"status": "unknown", "verified": False, "reason": "administrator_cancelled", "exit_code": 1223}

    monkeypatch.setattr(setup, "_invoke", invoke)
    setup.start("Apply")
    setup.worker.join(3)
    state = setup.snapshot()
    assert state["status"] == "unknown" and not state["verified"] and not state["busy"]
    assert state["reason"]
    with pytest.raises(WorkbenchError, match="invalid_action"):
        setup.start("disable_realtime_protection")


@pytest.mark.skipif(os.name != "nt", reason="Windows helper command construction")
def test_uac_helper_arguments_are_fixed_and_result_is_bound(tmp_path, monkeypatch):
    setup = WindowsSecuritySetup(tmp_path / "space and 'quote")
    commands = []
    native_run = subprocess.run

    def fake_run(argv, **kwargs):
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        outer = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert "-Verb RunAs -WindowStyle Hidden -Wait" in outer
        assert "ExecutionPolicy" not in argv
        assert "-ExecutionPolicy Bypass -EncodedCommand" in outer
        encoded = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)", outer).group(1)
        inner = base64.b64decode(encoded).decode("utf-16-le")
        assert "Set-ExecutionPolicy" not in inner and "Set-ExecutionPolicy" not in outer
        assert _ps_literal(str(setup.root)) in inner
        assert _ps_literal(str(setup.script)) in inner
        commands.extend([outer, inner])
        result_path = re.search(r"-ResultPath '((?:[^']|'')*)'", inner).group(1).replace("''", "'")
        assert Path(result_path).is_file() and Path(result_path).read_bytes() == b""
        Path(result_path).write_text(json.dumps({"action": "Check", "pattern": setup.state["pattern"],
                                               "status": "excluded", "verified": True}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("agent_adapters.windows_security.subprocess.run", fake_run)
    assert setup._invoke("Check")["verified"] is True
    assert len(commands) == 2
    # Parse both generated commands without executing either (no UAC).
    source = ",".join(_ps_literal(command) for command in commands)
    parser = (f"$count=0; foreach ($source in @({source})) {{ $tokens=$null;$errors=$null;"
              "[System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors) | Out-Null;"
              "$count+=@($errors).Count }; Write-Output $count")
    parsed = native_run([str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                         "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(parser)],
                        capture_output=True, check=True, timeout=15)
    assert parsed.stdout.strip() == b"0"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell contract test")
def test_powershell_check_apply_remove_with_fake_defender(tmp_path):
    # All OS configuration cmdlets, the admin test and path checker are replaced.
    script = Path(__file__).resolve().parents[1] / "agent_adapters/windows_defender_setup.ps1"
    driver = r"""
    # Execute only function definitions for this unit test. Do not load the CLI
    # entrypoint or change execution policy; OS-changing functions are mocked.
    $tokens=$null; $errors=$null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(SCRIPT, [ref]$tokens, [ref]$errors)
    $definitions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)
    foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
    $script:rules = @('C:\unrelated-exclusion')
    $script:admin = $true; $script:probe = 1; $script:adds = 0
    function Test-DefenderSetupAdministrator { return $script:admin }
    function Test-DefenderEvidencePath([string]$Root) { return [pscustomobject]@{ExitCode=$script:probe} }
    function Get-MpPreference { [CmdletBinding()]param() return [pscustomobject]@{ExclusionPath=$script:rules} }
    function Add-MpPreference { [CmdletBinding()]param([string]$ExclusionPath)
        $script:rules += $ExclusionPath; $script:adds += 1; $script:probe = 0 }
    function Remove-MpPreference { [CmdletBinding()]param([string]$ExclusionPath)
        $script:rules = @($script:rules | Where-Object { $_ -ne $ExclusionPath }); $script:probe = 1 }
    function Get-MpComputerStatus { [CmdletBinding()]param()
        return [pscustomobject]@{RealTimeProtectionEnabled=$true;IsTamperProtected=$true} }
    $root = ROOT
    $before = Invoke-DefenderEvidenceSetup Check $root
    $applied = Invoke-DefenderEvidenceSetup Apply $root
    $again = Invoke-DefenderEvidenceSetup Apply $root
    $removed = Invoke-DefenderEvidenceSetup Remove $root
    $remaining = @($script:rules)
    $script:probe = 0
    $broader = Invoke-DefenderEvidenceSetup Check $root
    $script:admin = $false
    $unprivileged = Invoke-DefenderEvidenceSetup Check $root
    $script:admin = $true
    function Add-MpPreference { [CmdletBinding()]param([string]$ExclusionPath) throw 'policy_denied' }
    $blocked = Invoke-DefenderEvidenceSetup Apply $root
    $tokens=$null; $errors=$null
    [System.Management.Automation.Language.Parser]::ParseFile(SCRIPT, [ref]$tokens, [ref]$errors) | Out-Null
    [ordered]@{before=$before;applied=$applied;again=$again;removed=$removed;remaining=$remaining;
        broader=$broader;adds=$script:adds;unprivileged=$unprivileged;blocked=$blocked;parse_errors=@($errors).Count} | ConvertTo-Json -Depth 5
    """.replace("SCRIPT", _ps_literal(str(script))).replace("ROOT", _ps_literal(str(tmp_path)))
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(driver)],
                            capture_output=True, check=True, timeout=20)
    data = json.loads(result.stdout)
    assert data["before"] is not None, result.stderr.decode("utf-8", errors="replace")
    assert data["parse_errors"] == 0
    assert data["before"]["status"] == "not_excluded"
    assert data["applied"]["status"] == "excluded" and data["applied"]["exact_rule_present"] is True
    assert data["again"]["verified"] and data["adds"] == 1
    assert data["removed"]["status"] == "not_excluded" and data["removed"]["exact_rule_present"] is False
    assert data["remaining"] == ["C:\\unrelated-exclusion"]
    assert data["broader"]["status"] == "excluded" and data["broader"]["exact_rule_present"] is False
    assert data["unprivileged"]["status"] == "unknown" and data["unprivileged"]["verified"] is False
    assert data["blocked"]["reason"] == "rule_configuration_failed" and data["blocked"]["status"] == "unknown"


@pytest.mark.skipif(os.name != "nt", reason="Windows helper receipt contract")
@pytest.mark.parametrize("outcome", ["cancelled", "receipt_denied"])
def test_precreated_receipt_keeps_cancel_and_read_failure_distinct(tmp_path, monkeypatch, outcome):
    setup = WindowsSecuritySetup(tmp_path)
    original_read = Path.read_text

    def fake_run(argv, **_kwargs):
        outer = base64.b64decode(argv[-1]).decode("utf-16-le")
        inner = base64.b64decode(re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)", outer).group(1)).decode("utf-16-le")
        receipt = Path(re.search(r"-ResultPath '((?:[^']|'')*)'", inner).group(1).replace("''", "'"))
        assert receipt.is_file() and receipt.stat().st_size == 0
        if outcome == "cancelled":
            return subprocess.CompletedProcess(argv, 1223)
        receipt.write_text(json.dumps({"action": "Check", "pattern": setup.state["pattern"],
                                      "status": "excluded", "verified": True}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    def read(path, *args, **kwargs):
        if path.name == "result.json" and outcome == "receipt_denied":
            raise PermissionError(13, "fake access denied", str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr("agent_adapters.windows_security.subprocess.run", fake_run)
    monkeypatch.setattr(Path, "read_text", read)
    result = setup._invoke("Check")
    assert result["status"] == "unknown" and not result["verified"]
    if outcome == "cancelled":
        assert result["reason"] == "administrator_cancelled" and result["exit_code"] == 1223
    else:
        assert result["reason"] == "helper_result_read_failed" and result["errno"] == 13
