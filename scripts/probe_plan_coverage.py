"""Does the plan leave anything out? Read-only. Exit 1 when coverage is short.

Two questions, both must be answered yes:
  1. every playable app (owned / family-shared / free) is in some collection
  2. every app the primary account's client can render is in some collection
     (so the client's 「未分类」 list ends up empty)

Paths come from ``steamlib.paths``; nothing is hardcoded, so the same check
works inside the project and inside the skill copy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steamlib.paths import project_root, steam_root  # noqa: E402

ROOT = project_root()


def retired_names() -> set[str]:
    path = ROOT / "output" / "user_retired_collections.json"
    if not path.is_file():
        return set()
    try:
        return {str(n) for n in json.loads(path.read_text(encoding="utf-8")).get("retired") or []}
    except (OSError, json.JSONDecodeError):
        return set()


def intentionally_left_out(row: dict, retired: set[str]) -> str:
    """Why an app is not in any planned collection, or "" when it should be."""
    app_type = row.get("app_type")
    if app_type == "试玩版" and "试玩版" in retired:
        return "用户删除了「试玩版」收藏夹"
    if app_type == "配置" and "系统与配置" in retired:
        return "用户删除了「系统与配置」收藏夹"
    name = str(row.get("name") or "").lower()
    if app_type == "配置" and ("dlc" in name or "entitlement" in name) and "DLC 与附加内容" in retired:
        return "用户删除了「DLC 与附加内容」收藏夹"
    return ""


def first_primary_account(steam: Path) -> str | None:
    """Read the primary account id from scan_context, else from loginusers.vdf."""
    ctx_path = ROOT / "data" / "processed" / "scan_context.json"
    if ctx_path.is_file():
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        for user in ctx.get("users") or []:
            if user.get("role") == "primary" and user.get("account_id"):
                return str(user["account_id"])
    login = steam / "config" / "loginusers.vdf"
    if not login.is_file():
        return None
    from steamlib import scanner  # noqa: E402

    users = scanner.list_steam_users(str(steam))
    primary = next((u for u in users if u.get("role") == "primary"), None)
    return str(primary["account_id"]) if primary else None


def main() -> int:
    steam = steam_root()
    inv = json.loads((ROOT / "output" / "games_inventory.json").read_text(encoding="utf-8"))
    if isinstance(inv, dict):
        inv = inv.get("games") or inv.get("records") or []
    plan = json.loads((ROOT / "output" / "collection_plan.json").read_text(encoding="utf-8"))

    planned = {int(a) for c in plan["collections"] for a in c["appids"]}
    retired = retired_names()

    should = [
        g
        for g in inv
        if g.get("ownership_status") in ("owned_verified", "family_shared", "free_to_play")
    ]
    should_games = [g for g in should if g.get("app_type") == "game"]
    should_other = [g for g in should if g.get("app_type") != "game"]

    print(f"应覆盖（owned_verified + family_shared + free_to_play）: {len(should)} 条")
    print(f"  其中普通游戏: {len(should_games)}，其他类型: {len(should_other)}")
    print(f"计划实际覆盖: {len(planned)} 个 AppID")

    missing_games = [g for g in should_games if int(g["appid"]) not in planned]
    missing_other = [
        g
        for g in should_other
        if int(g["appid"]) not in planned and not intentionally_left_out(g, retired)
    ]
    skipped_other = [
        g
        for g in should_other
        if int(g["appid"]) not in planned and intentionally_left_out(g, retired)
    ]

    print(f"\n普通游戏遗漏: {len(missing_games)}")
    for g in missing_games:
        print(f"  {g['appid']:>8}  {g.get('ownership_status'):<16} {g.get('app_type'):<6} {g['name']}")

    print(f"\n非游戏类遗漏: {len(missing_other)}")
    for g in missing_other:
        print(f"  {g['appid']:>8}  {g.get('ownership_status'):<16} {g.get('app_type'):<6} {g['name']}")

    if skipped_other:
        print(f"\n按用户意愿不纳入（收藏夹已被用户删除）: {len(skipped_other)}")
        for g in skipped_other:
            print(
                f"  {g['appid']:>8}  {g.get('app_type'):<6} {g['name']}"
                f"  ← {intentionally_left_out(g, retired)}"
            )

    # 2. Client-visible coverage
    account = first_primary_account(steam)
    uncovered: list[int] = []
    visible: set[int] = set()
    if account:
        cache = steam / "userdata" / account / "config" / "librarycache"
        visible = {int(p.stem) for p in cache.glob("*.json") if p.stem.isdigit()}
        by_id = {int(g["appid"]): g for g in inv}
        uncovered_raw = sorted(visible - planned)
        uncovered = []
        skipped_visible = []
        for appid in uncovered_raw:
            g = by_id.get(appid)
            if g and intentionally_left_out(g, retired):
                skipped_visible.append(appid)
                continue
            uncovered.append(appid)
        print(f"\n客户端可见 {len(visible)} 个应用；未被任何收藏夹覆盖: {len(uncovered)}")
        for appid in uncovered:
            g = by_id.get(appid)
            label = f"{g.get('app_type')}/{g.get('ownership_status')}" if g else "不在清单"
            print(f"  {appid:>9}  {label:<28} {g['name'] if g else '?'}")
        if skipped_visible:
            print(f"其中 {len(skipped_visible)} 个属于用户已删除的收藏夹（按意愿不覆盖）：")
            for appid in skipped_visible:
                g = by_id.get(appid) or {}
                print(f"  {appid:>9}  {g.get('app_type', '?'):<6} {g.get('name', '?')}")
    else:
        print("\n未找到主账号，跳过客户端可见性核对（只核对拥有/共享/免费覆盖）")

    ok = not missing_games and not missing_other and not uncovered
    print(f"\n覆盖完整: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
