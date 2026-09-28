@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m agent_adapters.workbench %*
    goto finished
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m agent_adapters.workbench %*
    goto finished
)

where python >nul 2>nul
if not errorlevel 1 (
    python -m agent_adapters.workbench %*
    goto finished
)

echo Python was not found. Install Python 3.11-3.13 or run uv sync --frozen first.
pause
exit /b 1

:finished
set "workbenchExitCode=%errorlevel%"
if not "%workbenchExitCode%"=="0" pause
exit /b %workbenchExitCode%
