@echo off
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem Preferred interpreter: a base CPython that can open the Docker named pipe.
rem
rem On some Windows setups the .venv launcher is denied
rem   CreateFile("\\.\pipe\dockerDesktopLinuxEngine") -> error 5
rem while the uv-managed base interpreter succeeds with the same packages.
rem workbench_runner spawns every agent subprocess with sys.executable, so the
rem workbench itself must run under an interpreter that can reach Docker;
rem otherwise every run stops in phase=start with error docker_unavailable.
rem ---------------------------------------------------------------------------
set "QINYUAN_BASE_PYTHON=%APPDATA%\uv\python\cpython-3.13-windows-x86_64-none\python.exe"

if exist "%QINYUAN_BASE_PYTHON%" if exist ".venv\Lib\site-packages\docker" (
    set "PYTHONPATH=%CD%;%CD%\.venv\Lib\site-packages;%CD%\.venv\Lib\site-packages\win32;%CD%\.venv\Lib\site-packages\win32\lib"
    set "PATH=%CD%\.venv\Lib\site-packages\pywin32_system32;%PATH%"
    "%QINYUAN_BASE_PYTHON%" -m agent_adapters.workbench %*
    goto finished
)

rem --- Fallbacks for machines without the uv-managed interpreter -------------
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
