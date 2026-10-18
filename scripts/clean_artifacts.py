"""清理测试与运行产物。

用法：
    python scripts/clean_artifacts.py            # 只报告
    python scripts/clean_artifacts.py --apply    # 真删

只删已知前缀，不递归猜。产物目录都是可再生的，git 里也没有跟踪。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (相对路径, 是否 glob)
TARGETS = [
    ("tests/.runtime-work*", True),
    ("tests/.test", False),
    ("tests/.test-work", False),
    ("tests/.sandbox-work", False),
    ("tests/.stage-work", False),
    ("tests/.cancel-work", False),
    ("tests/.d7-*", True),
    ("tests/.workbench-*", True),
    (".test", False),
    (".test-temp-*", True),
    (".test-work", False),
    ("test-artifacts", False),
    (".pytest_cache", False),
    ("pytest-of-*", True),
    (".ruff_cache", False),
    (".mypy_cache", False),
]


def size_of(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理测试与运行产物")
    parser.add_argument("--apply", action="store_true", help="真正删除，默认只报告")
    args = parser.parse_args(argv)

    freed = 0
    removed = 0
    for pattern, is_glob in TARGETS:
        paths = ROOT.glob(pattern) if is_glob else [ROOT / pattern]
        for path in paths:
            if not path.is_dir():
                continue
            size = size_of(path)
            freed += size
            removed += 1
            print(f"{'删除' if args.apply else '可删'} {path.relative_to(ROOT)}  {size / 1e6:.1f} MB")
            if args.apply:
                shutil.rmtree(path, ignore_errors=True)

    print(f"\n合计 {removed} 个目录，{freed / 1e6:.1f} MB" + ("" if args.apply else "；加 --apply 执行"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
