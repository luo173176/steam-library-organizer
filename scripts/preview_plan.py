"""Rebuild the collection plan from the last processed records, offline.

Lets us iterate on plan rules without re-running the full scan (which hits the
network). Writes into ``output/_preview`` so real outputs stay untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steamlib import report  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()


def main() -> int:
    rows = json.loads((ROOT / "data" / "processed" / "records.json").read_text(encoding="utf-8"))
    records = []
    for row in rows:
        records.append(
            report.AppRecord(
                appid=str(row["appid"]),
                name=row["name"],
                installed=bool(row["installed"]),
                ownership_status=row["ownership_status"],
                app_type=row["app_type"],
                ownership_detail=row.get("ownership_detail", ""),
                library_path=row.get("library_path", ""),
                install_dir=row.get("install_dir", ""),
                size_on_disk_bytes=row.get("size_on_disk_bytes") or None,
                last_updated=row.get("last_updated") or None,
                last_played=row.get("last_played") or None,
                playtime_minutes=row.get("playtime_minutes") or None,
                primary_genre=row.get("primary_genre") or "待确认",
                secondary_tags=[t for t in (row.get("secondary_tags") or "").split("|") if t],
                confidence=float(row.get("confidence") or 0.0),
                evidence=row.get("evidence", ""),
                review_required=bool(row.get("review_required")),
                source=[s for s in (row.get("source") or "").split("|") if s],
            )
        )

    ctx = json.loads((ROOT / "data" / "processed" / "scan_context.json").read_text(encoding="utf-8"))
    primary = next((u for u in ctx.get("users") or [] if u.get("role") == "primary"), None)
    visible = {int(a) for a in (primary or {}).get("librarycache_appids") or []}

    out = ROOT / "output" / "_preview"
    out.mkdir(parents=True, exist_ok=True)
    plan = report.write_collection_plan(records, out, visible_appids=visible)
    apply_plan = plan["apply"]

    total = sum(c["count"] for c in apply_plan["collections"])
    print(f"收藏夹 {len(apply_plan['collections'])} 个，覆盖 {total} 个 AppID")
    for c in apply_plan["collections"]:
        print(f"  {c['count']:4d}  {c['name']}")

    planned = {int(a) for c in apply_plan["collections"] for a in c["appids"]}
    rest = sorted(visible - planned)
    print(f"\n客户端可见 {len(visible)}，计划覆盖 {len(planned)}，仍未被覆盖 {len(rest)}")
    by_id = {int(r.appid): r for r in records}
    for appid in rest:
        rec = by_id.get(appid)
        label = f"{rec.app_type}/{rec.ownership_status}" if rec else "不在清单"
        print(f"  {appid:>9}  {label:<28} {(rec.name if rec else '?')}")

    dupes = [a for a in planned if sum(a in {int(x) for x in c["appids"]} for c in apply_plan["collections"]) > 1]
    print(f"\n跨收藏夹重复的 AppID: {len(dupes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
