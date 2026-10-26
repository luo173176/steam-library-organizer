"""Create the project skeleton in the right place, then print the root path.

Rule (from the user's spec): D:\\SteamLibraryOrganizer first, E: second, and if
neither drive is usable, stop and report — never silently write elsewhere.

Prints a single line `PROJECT_ROOT=<path>` so a caller can capture it.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

CANDIDATES = [("D", Path(r"D:\SteamLibraryOrganizer")), ("E", Path(r"E:\SteamLibraryOrganizer"))]
SUBDIRS = ["scripts", "data/raw", "data/processed", "output", "logs", "backups", "tests"]


def drive_usable(letter: str, probe: Path) -> tuple[bool, str]:
    root = Path(f"{letter}:\\")
    if not root.exists():
        return False, f"{letter}: 盘不存在"
    try:
        probe.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"{letter}: 盘不可写（{type(exc).__name__}）"
    with tempfile.NamedTemporaryFile(dir=probe, prefix=".writetest_", delete=False) as fh:
        fh.write(b"ok")
        tmp = Path(fh.name)
    try:
        tmp.unlink()
    except OSError:
        return False, f"{letter}: 盘无法删除测试文件"
    return True, f"{letter}: 可写"


def main() -> int:
    rejected: list[str] = []
    for letter, target in CANDIDATES:
        ok, why = drive_usable(letter, target)
        if not ok:
            rejected.append(why)
            print(f"[跳过] {why}", file=sys.stderr)
            continue
        for sub in SUBDIRS:
            (target / sub).mkdir(parents=True, exist_ok=True)
        print(f"PROJECT_ROOT={target}")
        if rejected:
            print("[说明] " + "；".join(rejected), file=sys.stderr)
        return 0

    print(
        "D 盘和 E 盘都不可用，未创建任何文件。\n"
        "请先确认磁盘盘符，或手动指定一个可写目录后设置 STEAMORG_ROOT。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    os.environ.pop("STEAMORG_ROOT", None)
    raise SystemExit(main())
