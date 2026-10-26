"""Exact diff: accepted baseline (preflight_store_snapshot) -> live store.

Read-only. Tells us precisely which collections and which AppIDs the user
changed after the last accepted baseline, so the hand edits can be described
accurately instead of guessed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib.paths import project_root, steam_root  # noqa: E402


def read_store(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
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
            out[decoded["name"]] = {
                "appids": {int(a) for a in (decoded.get("added") or [])},
                "key": str(key),
            }
    return out


def main() -> int:
    root = project_root()
    store_path, account_id = ac.locate_store()
    base_path = root / "output" / "preflight_store_snapshot.json"
    base_raw = json.loads(base_path.read_text(encoding="utf-8"))
    base = {
        c["name"]: {"appids": {int(a) for a in c["appids"]}, "key": c["key"]}
        for c in base_raw["collections"]
    }
    live = read_store(store_path)

    recs = json.loads((root / "data/processed/records.json").read_text(encoding="utf-8"))
    recs = recs["records"] if isinstance(recs, dict) else recs
    names = {int(r["appid"]): r for r in recs}

    print(f"基线: {base_path.name}（{base_raw['captured_at']}）{len(base)} 个收藏夹")
    print(f"当前: {len(live)} 个收藏夹")

    gone = [n for n in base if n not in live]
    new = [n for n in live if n not in base]
    print(f"\n删除收藏夹 {len(gone)}: {gone}")
    print(f"新增收藏夹 {len(new)}: {new}")

    moved: dict[int, tuple[str, str]] = {}
    for name in base:
        if name not in live:
            continue
        out_ = base[name]["appids"] - live[name]["appids"]
        in_ = live[name]["appids"] - base[name]["appids"]
        for a in out_:
            print(f"  移出 {name}: {a} {names.get(a, {}).get('name', '?')}")
        for a in in_:
            print(f"  移入 {name}: {a} {names.get(a, {}).get('name', '?')}")
        for a in out_:
            moved.setdefault(a, (name, ""))
        for a in in_:
            prev, _ = moved.get(a, ("", ""))
            moved[a] = (prev, name)

    print(f"\n真正改了收藏夹归属的 AppID: {len(moved)}")
    for a, (was, now) in sorted(moved.items()):
        print(f"  {a:>9}  {was or '(无)'} → {now or '(无)'}   {names.get(a, {}).get('name', '?')}")

    all_live = {x for v in live.values() for x in v["appids"]}
    all_base = {x for v in base.values() for x in v["appids"]}
    print(f"\n合计 AppID: 基线 {len(all_base)} → 当前 {len(all_live)}")
    print(f"彻底移出所有收藏夹: {sorted(all_base - all_live)}")
    cache = Path(steam_root()) / "userdata" / account_id / "config" / "librarycache"
    visible = {int(p.stem) for p in cache.glob("*.json") if p.stem.isdigit()}
    print(f"\n其中客户端仍会显示（=会落到「未分类」）:")
    for a in sorted(all_base - all_live):
        if a in visible:
            r = names.get(a, {})
            print(f"  {a:>9}  {r.get('app_type', '?'):<6} {r.get('primary_genre', '?'):<20} {r.get('name', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
