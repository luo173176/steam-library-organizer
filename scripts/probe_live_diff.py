"""Read-only: what does the client store contain now, versus the last plan?

Used after the user hand-edits collections in the Steam UI. Writes nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
import migrate_collections as mc  # noqa: E402
from steamlib.paths import project_root, steam_root  # noqa: E402

ROOT = project_root()


def main() -> int:
    store, account_id = ac.locate_store()
    live = mc.load_live(store)
    plan = json.loads((ROOT / "output" / "collection_plan.json").read_text(encoding="utf-8"))

    # What the client actually renders — the set that feeds its 「未分类」 view.
    cache = Path(steam_root()) / "userdata" / account_id / "config" / "librarycache"
    visible = {int(p.stem) for p in cache.glob("*.json") if p.stem.isdigit()}

    live_by_name = {v.get("name"): v for v in live.values()}
    planned = {c["name"]: c for c in plan["collections"]}

    print(f"账号 {account_id}")
    print(f"客户端现有收藏夹 {len(live)} 个：")
    for name in sorted(live_by_name):
        print(f"  {len(live_by_name[name].get('added') or []):4d}  {name}")

    print("\n== 与上次计划相比 ==")
    for name in sorted(set(planned) | set(live_by_name)):
        want = planned.get(name)
        got = live_by_name.get(name)
        if want and not got:
            print(f"  [已删除] {name}（计划 {want['count']} 款）")
            continue
        if got and not want:
            print(f"  [新增·非计划] {name}（{len(got.get('added') or [])} 款）")
            continue
        w = {int(a) for a in want["appids"]}
        g = {int(a) for a in got.get("added") or []}
        if w == g:
            continue
        names = {str(r["appid"]): r["name"] for r in json.loads(
            (ROOT / "data" / "processed" / "records.json").read_text(encoding="utf-8"))}
        for appid in sorted(w - g):
            print(f"  [移出] {name}: {appid} {names.get(str(appid), '?')}")
        for appid in sorted(g - w):
            print(f"  [移入] {name}: {appid} {names.get(str(appid), '?')}")

    total = {a for v in live.values() for a in v.get("added") or []}
    print(f"\n客户端收藏夹合计 {len(total)} 个不同 AppID")
    planned_all = {int(a) for c in plan["collections"] for a in c["appids"]}
    names = {str(r["appid"]): r for r in json.loads(
        (ROOT / "data" / "processed" / "records.json").read_text(encoding="utf-8"))}
    dropped = sorted(planned_all - {int(a) for a in total})
    print(f"计划内、现已不在任何收藏夹：{len(dropped)}")
    for appid in dropped:
        r = names.get(str(appid), {})
        shown = "可见" if appid in visible else "不显示"
        print(
            f"  {appid:>8}  {shown:<4} {r.get('app_type', '?'):<6} {r.get('ownership_status', '?'):<15}"
            f" {r.get('primary_genre', '?'):<24} {r.get('name', '?')}"
        )
    uncovered = sorted(visible - {int(a) for a in total})
    print(f"\n客户端可见但未进任何收藏夹（会出现在「未分类」）：{len(uncovered)}")
    for appid in uncovered:
        r = names.get(str(appid), {})
        print(
            f"  {appid:>8}  {r.get('app_type', '?'):<6} {r.get('ownership_status', '?'):<15}"
            f" {r.get('name', '?')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
