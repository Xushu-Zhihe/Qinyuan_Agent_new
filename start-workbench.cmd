@echo off
setlocal
cd /d "%~dp0"

rem Workbench and its agent/MCP subprocesses share the project interpreter.
if not exist ".venv\Scripts\python.exe" goto missing_venv
".venv\Scripts\python.exe" -m agent_adapters.workbench %*
set "workbenchExitCode=%errorlevel%"
if not "%workbenchExitCode%"=="0" pause
exit /b %workbenchExitCode%

:missing_venv
echo Project .venv was not found. Run uv sync --frozen first.
pause
exit /b 1
