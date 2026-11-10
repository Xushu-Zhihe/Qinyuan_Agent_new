# Build from the project root: uv run --project desktop --group build --locked pyinstaller --distpath . --workpath desktop/build desktop/launcher.spec
from pathlib import Path

root = Path(SPECPATH).parent
analysis = Analysis([str(root / "agent_adapters/launcher.py")], pathex=[str(root)],
    binaries=[], datas=[(str(root / "frontend/launcher.html"), "frontend"),
                        (str(root / "frontend/launcher.css"), "frontend"),
                        (str(root / "frontend/launcher.js"), "frontend"),
                        (str(root / "frontend/assets/launcher.ico"), "frontend/assets"),
                        (str(root / "agent_adapters/windows_defender_setup.ps1"), "agent_adapters")],
    hiddenimports=["webview.platforms.edgechromium"], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["docker", "langchain_openai", "langchain_core", "mcp", "PIL", "pytest", "playwright"])
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, [], name="QinyuanLauncher",
          debug=False, strip=False, upx=False, console=False, icon=str(root / "frontend/assets/launcher.ico"))
