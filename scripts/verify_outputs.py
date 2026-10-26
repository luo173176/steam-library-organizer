"""Validate the generated report set and print a compact summary.

Read-only: opens the output files, never modifies them. Exit code 0 only when
all required files exist, are non-empty, and the inventory/JSON counts agree.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()
OUT = ROOT / "output"

REQUIRED = [
    "games_inventory.csv",
    "games_inventory.json",
    "classification_review.csv",
    "classification_summary.md",
    "scan_report.md",
    "collection_plan.md",
    "collection_plan.json",
    "collection_patch.json",
    "unresolved_games.csv",
    "apply_report.md",
    "approval_request.md",
    "preflight_snapshot.json",
    "preflight_store_snapshot.json",
]


def main() -> int:
    problems: list[str] = []
    print(f"项目根目录: {ROOT}")
    print(f"校验时间(UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("\n[文件]")
    for name in REQUIRED:
        path = OUT / name
        if not path.is_file():
            problems.append(f"缺少 {name}")
            print(f"  ✗ {name} 缺失")
            continue
        size = path.stat().st_size
        if size == 0:
            problems.append(f"{name} 为空")
            print(f"  ✗ {name} 空文件")
            continue
        print(f"  ✓ {name:28s} {size:>8} bytes")

    print("\n[数据一致性]")
    csv_rows = list(csv.DictReader((OUT / "games_inventory.csv").open(encoding="utf-8-sig")))
    payload = json.loads((OUT / "games_inventory.json").read_text(encoding="utf-8"))
    print(f"  CSV 记录数: {len(csv_rows)}")
    print(f"  JSON 记录数: {payload['count']} / 数组 {len(payload['games'])}")
    if not (len(csv_rows) == payload["count"] == len(payload["games"])):
        problems.append("CSV 与 JSON 记录数不一致")
    if len({r["appid"] for r in csv_rows}) != len(csv_rows):
        problems.append("CSV 存在重复 AppID")

    required_fields = [
        "appid", "name", "installed", "ownership_status", "ownership_detail", "app_type",
        "library_path", "install_dir", "size_on_disk_bytes", "last_updated", "last_played",
        "playtime_minutes", "primary_genre", "secondary_tags", "confidence", "evidence",
        "review_required", "source",
    ]
    missing_fields = [f for f in required_fields if f not in csv_rows[0]]
    print(f"  字段完整: {'✓' if not missing_fields else '✗ ' + ','.join(missing_fields)}")
    if missing_fields:
        problems.append("缺少字段 " + ",".join(missing_fields))

    # 契约：不得出现「已安装但状态仍是 unknown」或「未安装却写成已拥有」的旧错误。
    bad_installed = [
        r["appid"] for r in csv_rows if r["installed"] == "True" and r["ownership_status"] == "unknown"
    ]
    if bad_installed:
        problems.append("已安装但拥有状态为 unknown: " + ",".join(bad_installed[:5]))
    bad_owner = [
        r["appid"]
        for r in csv_rows
        if r["ownership_status"] == "owned_verified" and r["source"].find("api:GetOwnedGames") < 0
    ]
    if bad_owner:
        problems.append("owned_verified 缺少 API 证据: " + ",".join(bad_owner[:5]))

    print("\n[分布]")
    print("  拥有状态:", dict(Counter(r["ownership_status"] for r in csv_rows)))
    print("  应用类型:", dict(Counter(r["app_type"] for r in csv_rows)))
    print("  复核标记:", dict(Counter(r["review_required"] for r in csv_rows)))
    print("  已安装:", sum(1 for r in csv_rows if r["installed"] == "True"))
    print("  待确认:", sum(1 for r in csv_rows if r["primary_genre"] == "待确认"))

    unresolved = list(csv.DictReader((OUT / "unresolved_games.csv").open(encoding="utf-8-sig")))
    print(f"  待核查条目: {len(unresolved)}")

    # 收藏夹补丁：必须与计划一致，且仍标记为未写入。
    patch = json.loads((OUT / "collection_patch.json").read_text(encoding="utf-8"))
    plan = json.loads((OUT / "collection_plan.json").read_text(encoding="utf-8"))
    plan_names = {c["name"]: len(c["appids"]) for c in plan["collections"]}
    patch_names = {i["name"]: i["count"] for i in patch["to_add"]}
    patch_names.update({i["name"]: i["planned_count"] for i in patch["already_complete"]})
    # A collection that already exists but is missing entries is neither "add"
    # nor "complete"; leaving it out made the cross-check fail on partial plans.
    patch_names.update(
        {i["name"]: i["planned_count"] for i in patch["name_conflicts_needing_append"]}
    )
    print(
        f"  收藏夹补丁: 新增 {len(patch['to_add'])} / 已完整 {len(patch['already_complete'])}"
        f" / 冲突 {len(patch['name_conflicts_needing_append'])}"
        f" / 已写入 {patch['writes_performed']}"
    )
    if patch["writes_performed"]:
        problems.append("collection_patch.json 声称已写入，与 dry run 预期不符")
    if patch_names != plan_names:
        diff = {k: (patch_names.get(k), plan_names.get(k)) for k in set(patch_names) | set(plan_names) if patch_names.get(k) != plan_names.get(k)}
        problems.append(f"收藏夹补丁与计划不一致: {diff}")
    for item in patch["to_add"]:
        decoded = json.loads(item["payload"]["value"])
        if len(decoded["added"]) != item["count"]:
            problems.append(f"补丁条目 {item['name']} 的 added 数与 count 不符")
            break

    print("\n结果:", "通过" if not problems else "失败")
    for p in problems:
        print("  -", p)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
