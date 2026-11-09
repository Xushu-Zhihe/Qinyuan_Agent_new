@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 goto missing_uv
rem The desktop interpreter is isolated from the Agent environment it prepares.
uv run --project desktop --locked python -m agent_adapters.launcher %*
set "launcherExitCode=%errorlevel%"
if not "%launcherExitCode%"=="0" pause
exit /b %launcherExitCode%
:missing_uv
echo uv was not found. Install it from https://docs.astral.sh/uv/getting-started/installation/
pause
exit /b 1
