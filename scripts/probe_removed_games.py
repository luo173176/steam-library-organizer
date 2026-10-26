"""Which games left every collection since the last write? Read-only.

Compares a named backup snapshot against the live store and separates:
  - real games (app_type == "game") that the user removed
  - non-game rows that disappeared together with a deleted app-type bucket
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib.paths import project_root, steam_root  # noqa: E402


def collections(path: Path) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for item in json.loads(path.read_text(encoding="utf-8")):
        if not isinstance(item, list) or len(item) != 2:
            continue
        key, payload = item
        if not str(key).startswith("user-collections.") or not isinstance(payload, dict):
            continue
        if payload.get("is_deleted") or not payload.get("value"):
            continue
        try:
            decoded = json.loads(payload["value"])
        except json.JSONDecodeError:
            continue
        if decoded.get("name"):
            out[str(decoded["name"])] = {int(a) for a in (decoded.get("added") or [])}
    return out


def main() -> int:
    root = project_root()
    store_path, account_id = ac.locate_store()
    backups = sorted(p for p in (root / "backups").glob("cloudstorage_*") if p.is_dir())
    before_path = Path(sys.argv[1]) if len(sys.argv) > 1 else backups[0] / "cloud-storage-namespace-1.json"
    before = collections(before_path)
    now = collections(store_path)

    recs = json.loads((root / "data/processed/records.json").read_text(encoding="utf-8"))
    recs = recs["records"] if isinstance(recs, dict) else recs
    names = {int(r["appid"]): r for r in recs}
    cache = Path(steam_root()) / "userdata" / account_id / "config" / "librarycache"
    visible = {int(p.stem) for p in cache.glob("*.json") if p.stem.isdigit()}

    was = {a for s in before.values() for a in s}
    is_ = {a for s in now.values() for a in s}
    gone = sorted(was - is_)
    print(f"基准: {before_path.parent.name}（{len(before)} 夹 / {len(was)} AppID）")
    print(f"当前: {len(now)} 夹 / {len(is_)} AppID")
    print(f"完全移出收藏夹: {len(gone)}\n")

    games = [a for a in gone if names.get(a, {}).get("app_type") == "game"]
    others = [a for a in gone if a not in games]
    print(f"其中普通游戏: {len(games)}")
    for a in games:
        r = names.get(a, {})
        print(
            f"  {a:>9}  {'可见' if a in visible else '不显示':<4} "
            f"{r.get('ownership_status', '?'):<15} {r.get('primary_genre', '?'):<22} {r.get('name', '?')}"
        )
    print(f"\n非普通游戏: {len(others)}")
    for a in others:
        r = names.get(a, {})
        print(
            f"  {a:>9}  {'可见' if a in visible else '不显示':<4} "
            f"{r.get('app_type', '?'):<6} {r.get('name', '?')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
