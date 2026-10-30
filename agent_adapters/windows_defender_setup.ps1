# Run Apply/Remove explicitly from an administrator Windows PowerShell window.
# Check never changes Defender settings. No sample is downloaded or executed.
[CmdletBinding()]
param(
    [ValidateSet('Check', 'Apply', 'Remove')][string]$Action = 'Check',
    [string]$DataDir = (Join-Path (Get-Location).Path 'workbench_data'),
    [string]$ResultPath = ''
)

function Test-DefenderSetupAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-DefenderEvidencePath([string]$Root) {
    $tool = Get-ChildItem -Path "$env:ProgramData\Microsoft\Windows Defender\Platform\*\MpCmdRun.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    if (-not $tool) { $tool = Join-Path $env:ProgramFiles 'Windows Defender\MpCmdRun.exe' }
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw 'defender_cli_unavailable' }
    $runs = Join-Path $Root 'runs'
    $newRuns = -not (Test-Path -LiteralPath $runs)
    $probe = Join-Path $runs ([Guid]::NewGuid().ToString('D'))
    $agentState = Join-Path $probe 'agent-state'
    $task = Join-Path $agentState ([Guid]::NewGuid().ToString('D'))
    $evidence = Join-Path $task 'stage-evidence'
    # Only empty directories created here are removed, without recursive delete.
    try {
        New-Item -ItemType Directory -Path $evidence -Force -ErrorAction Stop | Out-Null
        $output = @(& $tool -CheckExclusion -Path $evidence 2>&1 | ForEach-Object { [string]$_ })
        $code = $LASTEXITCODE
        return [pscustomobject]@{ExitCode=$code;Output=$output}
    }
    finally {
        foreach ($directory in @($evidence, $task, $agentState, $probe)) {
            if ([IO.Directory]::Exists($directory)) { [IO.Directory]::Delete($directory, $false) }
        }
        if ($newRuns -and [IO.Directory]::Exists($runs)) {
            # Another run may have started meanwhile; never remove its files.
            if (-not (Get-ChildItem -LiteralPath $runs -Force | Select-Object -First 1)) {
                [IO.Directory]::Delete($runs, $false)
            }
        }
    }
}

function Invoke-DefenderEvidenceSetup([string]$RequestedAction, [string]$RequestedDataDir) {
    $result = [ordered]@{action=$RequestedAction;status='unknown';reason=$null;pattern=$null;verified=$false}
    try {
        $result.reason = 'invalid_data_directory'
        if ($RequestedDataDir.IndexOfAny([char[]]'*?') -ge 0) { throw 'data_directory_must_not_contain_wildcards' }
        $root = [IO.Path]::GetFullPath($RequestedDataDir)
        $result.pattern = Join-Path $root 'runs\*\agent-state\*\stage-evidence'
        if ($PSVersionTable.PSEdition -ne 'Desktop') {
            $result.reason = 'use_windows_powershell_exe'
            return [pscustomobject]$result
        }
        if (-not (Test-DefenderSetupAdministrator)) {
            $result.reason = 'administrator_required_to_verify_or_configure'
            return [pscustomobject]$result
        }
        $result.reason = 'exclusions_read_failed'
        $rules = @((Get-MpPreference -ErrorAction Stop).ExclusionPath)
        if ($rules | Where-Object { $_ -match 'N/A|administrator' }) {
            $result.reason = 'exclusions_not_visible'
            return [pscustomobject]$result
        }
        if ($RequestedAction -eq 'Remove') {
            $result.reason = 'rule_removal_failed'
            if ($rules -contains $result.pattern) {
                Remove-MpPreference -ExclusionPath $result.pattern -ErrorAction Stop
            }
            if (@((Get-MpPreference -ErrorAction Stop).ExclusionPath) -contains $result.pattern) {
                throw 'rule_removal_not_confirmed'
            }
        }
        if ($RequestedAction -eq 'Apply') {
            $result.reason = 'rule_configuration_failed'
            if (-not (Test-Path -LiteralPath $root -PathType Container)) {
                New-Item -ItemType Directory -Path $root -Force -ErrorAction Stop | Out-Null
            }
            if ($rules -notcontains $result.pattern) {
                Add-MpPreference -ExclusionPath $result.pattern -ErrorAction Stop
            }
            if (@((Get-MpPreference -ErrorAction Stop).ExclusionPath) -notcontains $result.pattern) {
                throw 'rule_write_not_confirmed_or_managed_policy'
            }
        }
        $result.exact_rule_present = @((Get-MpPreference -ErrorAction Stop).ExclusionPath) -contains $result.pattern
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            $result.reason = 'data_directory_not_found'
            return [pscustomobject]$result
        }
        $result.reason = 'defender_path_check_unavailable'
        $check = Test-DefenderEvidencePath $root
        if ($check.ExitCode -in @(0, 1)) {
            $result.status = if ($check.ExitCode -eq 0) { 'excluded' } else { 'not_excluded' }
            $result.verified = $true
            $result.reason = $null
        }
        else { $result.reason = 'defender_path_check_unavailable' }
        # Protection flags are informational; their failure does not undo a path check.
        $protection = Get-MpComputerStatus -ErrorAction SilentlyContinue
        if ($protection) {
            $result.real_time_protection_enabled = $protection.RealTimeProtectionEnabled
            $result.tamper_protected = $protection.IsTamperProtected
        }
        return [pscustomobject]$result
    }
    catch {
        $result.status = 'unknown'
        $result.verified = $false
        $result.error_type = $_.Exception.GetType().Name
        return [pscustomobject]$result
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $setupResult = Invoke-DefenderEvidenceSetup $Action $DataDir
    if ($ResultPath) {
        [IO.File]::WriteAllText($ResultPath, ($setupResult | ConvertTo-Json -Depth 3), (New-Object System.Text.UTF8Encoding($false)))
    }
    $setupResult | ConvertTo-Json -Depth 3
    if ($setupResult.status -eq 'excluded') { exit 0 }
    if ($setupResult.status -eq 'not_excluded') { exit 1 }
    exit 2
}
