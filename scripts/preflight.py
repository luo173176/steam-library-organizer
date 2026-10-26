"""One-shot preflight: is this machine ready to run the scan?

Read-only, no Steam writes, no network. Prints a compact checklist and exits
non-zero only when a *blocking* item is missing (no Steam, no project root).
A missing API key is reported as a downgrade, not a failure: the scan then
runs as a partial scan.

Never prints the key itself, only whether the file exists and its length.
"""

from __future__ import annotations

import platform
import sys
from argparse import ArgumentParser
from pathlib import Path
import os

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib import scanner  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

KEY_CANDIDATES = ["data/apikey.txt", "data/steam_api_key.txt", "apikey.txt"]


def find_key(root: Path, explicit: str | None = None) -> tuple[Path | None, str, bool]:
    """Return (path, human state, usable). The key text never leaves this function."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_path = os.environ.get("STEAMORG_KEY_FILE")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates += [root / rel for rel in KEY_CANDIDATES]

    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return path, "存在但读不出来", False
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not lines:
            return path, "存在但为空", False
        return path, f"就绪（{len(lines[0])} 字符，内容不显示）", True
    if os.environ.get("STEAM_API_KEY", "").strip():
        return None, "已从环境变量 STEAM_API_KEY 提供", True
    return None, "未找到", False


def main() -> int:
    ap = ArgumentParser(description="环境预检（只读，不访问网络）")
    ap.add_argument("--key-file", default=None, help="Steam Web API Key 文件路径（内容不回显）")
    args = ap.parse_args()

    root = project_root()
    print(f"[项目] {root}")
    print(f"[系统] {platform.system()} {platform.release()} / Python {platform.python_version()}")

    steam, attempts = scanner.find_steam_root()
    if not steam:
        print(f"[Steam] 未找到（尝试 {attempts} 处）")
        print("\n阻塞：找不到 Steam 安装目录。设置 STEAMORG_STEAM 指向 Steam 根目录后重试。")
        return 2
    print(f"[Steam] {steam}")

    procs = ac.running_processes()
    print(f"[客户端] {'运行中：' + ', '.join(procs) if procs else '未运行（扫描没问题；写入前必须退出）'}")

    libs, errors, notes = scanner.parse_library_folders(steam)
    accessible = [lib for lib in libs if lib.accessible]
    print(f"[库] 共 {len(libs)} 个，可访问 {len(accessible)} 个")
    for lib in libs:
        mark = "✓" if lib.accessible else "✗ 离线"
        print(f"   {mark} {lib.path}")
    for err in errors:
        print(f"   ! {err}")
    for note in notes:
        print(f"   · {note}")

    users = scanner.list_steam_users(steam)
    if not users:
        print("[账号] 未在 loginusers.vdf 找到账号（将以本地缓存为准）")
    for user in users:
        print(
            f"[账号] {user['persona_name'] or '?'}  steamid64={user['steamid64']}"
            f"  account_id={user['account_id']}  role={user.get('role')}"
            f"  librarycache={user['librarycache_count']}"
        )

    key_path, key_state, key_ok = find_key(root, args.key_file)
    print(
        f"[Key] {key_state}"
        + (f"  {key_path}" if key_path else f"（可放 {root / 'data' / 'apikey.txt'}）")
    )
    if not key_ok:
        print("      没有 Key 也能跑，但结果是「部分扫描」：只覆盖本机见过的应用。")

    print("\n就绪。" if accessible else "\n注意：没有可访问的 Steam 库，扫描会得到空清单。")
    return 0 if accessible else 3


if __name__ == "__main__":
    raise SystemExit(main())
