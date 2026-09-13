# Dot-source before running verification commands in this PowerShell session.
$projectTestRoot = Join-Path (Split-Path -Parent $PSScriptRoot) '.test'
$projectTempDirectory = Join-Path $projectTestRoot 'tmp'
New-Item -ItemType Directory -Path $projectTempDirectory -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $projectTestRoot 'pytest-cache') -Force | Out-Null
$env:TEMP = $projectTempDirectory
$env:TMP = $projectTempDirectory
$env:TMPDIR = $projectTempDirectory
$env:PYTHONPYCACHEPREFIX = Join-Path $projectTestRoot 'pycache'
$env:UV_CACHE_DIR = Join-Path $projectTestRoot 'uv-cache'
$env:RUFF_CACHE_DIR = Join-Path $projectTestRoot 'ruff-cache'
$env:MYPY_CACHE_DIR = Join-Path $projectTestRoot 'mypy-cache'
