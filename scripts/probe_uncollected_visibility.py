"""Why are these AppIDs uncollected? Read-only cross-check.

For every AppID present in the plan but absent from the live store, report:
  - whether the primary account's librarycache renders it (client-visible)
  - ownership status / app type / genre
That separates "user deliberately removed a game" from "the plan was wrong".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib.paths import project_root, steam_root  # noqa: E402


def main() -> int:
    root = project_root()
    steam = steam_root()
    store_path, account_id = ac.locate_store()

    live: set[int] = set()
    for item in json.loads(store_path.read_text(encoding="utf-8")):
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
        live.update(int(a) for a in (decoded.get("added") or []))

    plan = json.loads((root / "output/collection_plan.json").read_text(encoding="utf-8"))
    planned = {int(a) for c in plan["collections"] for a in c["appids"]}
    recs = json.loads((root / "data/processed/records.json").read_text(encoding="utf-8"))
    recs = recs["records"] if isinstance(recs, dict) else recs
    by_id = {int(r["appid"]): r for r in recs}

    cache = Path(steam) / "userdata" / account_id / "config" / "librarycache"
    visible = {int(p.stem) for p in cache.glob("*.json") if p.stem.isdigit()}
    print(f"librarycache 可见 {len(visible)} 个；收藏夹覆盖 {len(live)} 个")

    dropped = sorted(planned - live)
    print(f"\n计划内但当前不在任何收藏夹: {len(dropped)}")
    print(f"{'appid':>9}  {'可见':<4} {'type':<6} {'ownership':<15} {'genre':<22} name")
    for a in dropped:
        r = by_id.get(a, {})
        print(
            f"{a:>9}  {'是' if a in visible else '否':<4} "
            f"{r.get('app_type', '?'):<6} {r.get('ownership_status', '?'):<15} "
            f"{r.get('primary_genre', '?'):<22} {r.get('name', '?')}"
        )

    uncovered = sorted(visible - live)
    print(f"\n客户端可见但未分类: {len(uncovered)}")
    for a in uncovered:
        r = by_id.get(a, {})
        print(f"{a:>9}  {r.get('app_type', '?'):<6} {r.get('ownership_status', '?'):<15} {r.get('name', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
