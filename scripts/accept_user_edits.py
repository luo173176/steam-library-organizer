"""Freeze the client's current collections as the new baseline.

Run after the user hand-edits collections in the Steam UI. Writes only inside
the project (``output/preflight_store_snapshot.json``,
``output/user_retired_collections.json``); never touches Steam.

Why this exists: the plan describes what the tool proposed, but the user is the
owner of their library. Once they edit by hand, "differs from the plan" is no
longer a fault — the current store *is* the truth, and later verification must
compare against it instead of repeatedly reporting drift.

Names the tool proposed and the user deleted are recorded as *retired*, so the
next ``run_scan.py`` does not resurrect a bucket the user threw away.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import apply_collections as ac  # noqa: E402
import migrate_collections as mc  # noqa: E402


def main() -> int:
    store, account_id = ac.locate_store()
    live = mc.load_live(store)

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store": str(store),
        "account_id": account_id,
        "source": "user-edited baseline (accepted by request)",
        "collections": [
            {
                "key": key,
                "name": value.get("name"),
                "appids": [str(a) for a in value.get("added") or []],
            }
            for key, value in sorted(live.items(), key=lambda kv: str(kv[1].get("name")))
        ],
    }

    out = ROOT / "output" / "preflight_store_snapshot.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)

    # A marker so later verification does not keep re-reporting the user's own
    # edits as drift. Stores a fingerprint of the plan the edits were made
    # against: if the plan changes later, the marker no longer applies.
    plan_path = ROOT / "output" / "collection_plan.json"
    fingerprint = ""
    axis = ""
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        axis = plan.get("axis", "genre")
        fingerprint = "|".join(
            sorted(f"{c['name']}:{len(c['appids'])}" for c in plan["collections"])
        )
    marker = {
        "accepted_at": snapshot["captured_at"],
        "account_id": account_id,
        "collections": len(live),
        "appids": len({a for v in live.values() for a in v.get("added") or []}),
        "plan_axis": axis,
        "plan_fingerprint": fingerprint,
        "retired": [],
    }
    # Anything the plan wanted but the store no longer has is a user deletion.
    # Protected collections (the user's own folders) are never in the plan, so
    # they cannot be retired by accident.
    live_names = {str(v.get("name")) for v in live.values()}
    retired_path = ROOT / "output" / "user_retired_collections.json"
    previous: list[str] = []
    if retired_path.is_file():
        try:
            previous = [
                str(n) for n in json.loads(retired_path.read_text(encoding="utf-8")).get("retired") or []
            ]
        except (OSError, json.JSONDecodeError):
            previous = []
    retired: list[str] = list(previous)
    if plan_path.is_file():
        plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
        retired += [c["name"] for c in plan_doc["collections"] if c["name"] not in live_names]
    # Accumulate: a bucket the user deleted stays deleted even after the next
    # scan no longer proposes it. Protected folders are never retired.
    retired = sorted({n for n in retired if n and n not in {"收藏夹", "已隐藏"}})
    marker["retired"] = retired
    marker_path = ROOT / "output" / "user_edits_accepted.json"
    tmp_marker = marker_path.with_suffix(".json.tmp")
    tmp_marker.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_marker.replace(marker_path)

    tmp_retired = retired_path.with_suffix(".json.tmp")
    tmp_retired.write_text(
        json.dumps(
            {
                "accepted_at": snapshot["captured_at"],
                "retired": retired,
                "note": "用户手动删除的收藏夹名；run_scan 重建计划时不会再次创建。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp_retired.replace(retired_path)

    total = {a for v in live.values() for a in v.get("added") or []}
    print(f"已接受当前客户端状态为基线：{len(live)} 个收藏夹 / {len(total)} 个 AppID")
    if retired:
        print(f"记录用户删除的收藏夹（后续不再重建）：{'、'.join(retired)}")
    print(f"写入 {out}（Steam 目录未改动）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
