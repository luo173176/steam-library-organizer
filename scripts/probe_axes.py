"""Offline: build the plan on every axis and show the buckets (no Steam I/O)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steamlib import grouping, report  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()


def load_records() -> list[report.AppRecord]:
    payload = json.loads((ROOT / "output" / "games_inventory.json").read_text(encoding="utf-8"))
    out = []
    for row in payload["games"]:
        out.append(
            report.AppRecord(
                appid=str(row["appid"]),
                name=row["name"],
                installed=bool(row["installed"]),
                ownership_status=row["ownership_status"],
                app_type=row["app_type"],
                # 0 分钟是「从未游玩」，不是「未知」：不能用 `or None` 把 0 吃掉。
                playtime_minutes=(
                    int(row["playtime_minutes"]) if row["playtime_minutes"] != "" else None
                ),
                last_played=int(row["last_played"]) if row["last_played"] else None,
                primary_genre=row["primary_genre"],
                secondary_tags=row.get("secondary_tags") or [],
                meta=row.get("store") or {},
            )
        )
    return out


def main() -> int:
    records = load_records()
    for axis in grouping.names():
        with tempfile.TemporaryDirectory() as tmp:
            plan = report.write_collection_plan(records, Path(tmp), axis=axis)
        total = len({a for c in plan["plan"] for a in c["appids"]})
        print(f"\n=== {axis}（{grouping.resolve(axis)['label']}）："
              f"{len(plan['plan'])} 夹 / {total} AppID ===")
        for entry in plan["plan"]:
            print(f"  {entry['count']:5d}  {entry['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
