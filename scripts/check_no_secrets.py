"""Fail if the Steam Web API key (or anything key-shaped) leaked into output.

Reads the key from the same file the scan used, then greps every project
artifact (output, data, logs, tests, scripts) for it. The key is never printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git"}
TEXT_SUFFIXES = {
    ".md", ".json", ".csv", ".log", ".txt", ".py", ".toml", ".ini", ".cfg", ".yml", ".yaml",
}


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: check_no_secrets.py <key-file>")
        return 2
    key_path = Path(argv[0])
    key = ""
    for line in key_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key = line
            break
    if len(key) < 8:
        print("Key 文件内容异常，未执行检查")
        return 2

    hits: list[tuple[str, int]] = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path == key_path:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        if key in text:
            hits.append((str(path.relative_to(ROOT)), text.count(key)))

    print(f"已扫描 {scanned} 个文本文件（不含 Key 文件本身）")
    if hits:
        print("发现泄漏：")
        for name, count in hits:
            print(f"  {name}: {count} 处")
        return 1
    print("未发现 API Key 泄漏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
