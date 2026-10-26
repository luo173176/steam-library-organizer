"""Read-only: exactly which AppIDs the user moved compared with the current plan.

Separates "one game moved from collection A to B" from "game left every
collection", so the hand edits can be described without inferring anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()


def live_store(path: Path) -> dict[str, set[int]]:
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
    store_path, account_id = ac.locate_store()
    live = live_store(store_path)
    plan = json.loads((ROOT / "output" / "collection_plan.json").read_text(encoding="utf-8"))
    planned = {c["name"]: {int(a) for a in c["appids"]} for c in plan["collections"]}

    recs = json.loads((ROOT / "data" / "processed" / "records.json").read_text(encoding="utf-8"))
    recs = recs["records"] if isinstance(recs, dict) else recs
    names = {int(r["appid"]): r for r in recs}

    was = {a for s in planned.values() for a in s}
    now = {a for s in live.values() for a in s}
    print(f"账号 {account_id}；计划 {len(planned)} 夹 / {len(was)} AppID；"
          f"实际 {len(live)} 夹 / {len(now)} AppID")

    moved: dict[int, tuple[list[str], list[str]]] = {}
    for name in sorted(set(planned) | set(live)):
        p = planned.get(name, set())
        l = live.get(name, set())
        for a in p - l:
            moved.setdefault(a, ([], []))[0].append(name)
        for a in l - p:
            moved.setdefault(a, ([], []))[1].append(name)

    print(f"\n与计划不同的 AppID: {len(moved)}")
    for a, (was_in, now_in) in sorted(moved.items()):
        r = names.get(a, {})
        print(
            f"  {a:>9}  {r.get('app_type', '?'):<6} {r.get('primary_genre', '?'):<22}"
            f" {r.get('name', '?')}\n"
            f"             计划: {was_in or '(无)'} → 实际: {now_in or '(无)'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
