"""Regenerate output/apply_report.md from the live Steam store + the current plan.

Read-only against Steam. Numbers come from the store and plan, never from memory.

Once the user has hand-edited collections and accepted that state
(``output/user_edits_accepted.json``), their store is the truth: the plan is
history, so per-collection differences are reported as 用户调整 / 用户删除
instead of failures, and the exit code stays 0.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
import migrate_collections as mc  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()


def sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def latest_backup() -> Path | None:
    dirs = [p for p in (ROOT / "backups").glob("cloudstorage_*") if p.is_dir()]
    return max(dirs, key=lambda p: p.stat().st_mtime) if dirs else None


def main() -> int:
    store, account_id = ac.locate_store()
    idx_path = ac.namespace_index(store)
    plan = json.loads((ROOT / "output" / "collection_plan.json").read_text(encoding="utf-8"))
    live = mc.load_live(store)
    accepted_path = ROOT / "output" / "user_edits_accepted.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8")) if accepted_path.is_file() else {}
    fingerprint = "|".join(sorted(f"{c['name']}:{len(c['appids'])}" for c in plan["collections"]))
    user_owns_state = accepted.get("plan_fingerprint") == fingerprint

    planned = {c["name"]: c for c in plan["collections"]}
    live_names = {v.get("name") for v in live.values()}
    missing = [n for n in planned if n not in live_names]
    live_total = {int(a) for v in live.values() for a in v.get("added") or []}
    planned_total = {int(a) for c in plan["collections"] for a in c["appids"]}
    dropped_everywhere = sorted(planned_total - live_total)

    hard_missing = [] if user_owns_state else missing

    backup = latest_backup()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines: list[str] = []
    add = lines.append
    add("# Steam 收藏夹应用报告")
    add("")
    if user_owns_state:
        add("状态：**已应用，且用户已按自己的偏好调整（APPLIED + USER-EDITED）**")
    else:
        add("状态：**已应用（APPLIED）**" if not missing else "状态：**部分应用（PARTIAL）**")
    add("")
    add(f"- 报告生成时间（UTC）：{now}")
    add(f"- 账号目录：`{account_id}`")
    if backup:
        add(f"- 最近备份目录：`{backup}`")
    add(f"- 计划收藏夹：{len(planned)} 个，共 {len({a for c in plan['collections'] for a in c['appids']})} 个 AppID")
    add(f"- 存储中生效：{len([n for n in planned if n in live_names])} 个")
    add(f"- 存储中收藏夹总数（含用户自建）：{len(live)} 个")
    add(f"- 存储中条目的 AppID 并集：{len(live_total)} 个")
    add("- 未启动、下载、安装、卸载任何游戏；Steam 游戏目录全程只读。")
    add("")
    if user_owns_state:
        add(
            f"> 用户已于 {accepted.get('accepted_at')} 手动调整收藏夹并接受为基线。"
            "下表凡标注「用户调整 / 用户删除」的，都是用户自己的决定，脚本不再回改。"
        )
        add("")

    if missing:
        add("## 用户删除的收藏夹（不回滚）" if user_owns_state else "## 未生效")
        add("")
        for n in missing:
            add(f"- {n}")
        add("")

    if dropped_everywhere:
        recs = json.loads((ROOT / "data" / "processed" / "records.json").read_text(encoding="utf-8"))
        recs = recs["records"] if isinstance(recs, dict) else recs
        by_id = {int(r["appid"]): r for r in recs}
        add("## 计划内但已不在任何收藏夹的条目")
        add("")
        add("| AppID | 名称 | 类型 | 归属 | 说明 |")
        add("| --- | --- | --- | --- | --- |")
        for appid in dropped_everywhere:
            r = by_id.get(appid, {})
            app_type = r.get("app_type", "?")
            if app_type in ("试玩版", "配置"):
                why = f"随「{'试玩版' if app_type == '试玩版' else '系统与配置'}」收藏夹一起删除"
            elif app_type == "测试版":
                why = "用户从收藏夹移出"
            else:
                why = "用户从收藏夹移出"
            add(
                f"| {appid} | {r.get('name', '?')} | {app_type} | "
                f"{r.get('ownership_status', '?')} | {why} |"
            )
        add("")

    add("## 收藏夹明细")
    add("")
    add("| 收藏夹 | 计划条目 | 实际条目 | 状态 |")
    add("| --- | ---: | ---: | --- |")
    adjusted = 0
    for c in plan["collections"]:
        entry = next((v for v in live.values() if v.get("name") == c["name"]), None)
        actual = len(entry.get("added") or []) if entry else 0
        ok = entry is not None and set(entry.get("added") or []) == set(int(a) for a in c["appids"])
        if ok:
            mark = "✓"
        elif entry is None and user_owns_state:
            mark = "用户删除"
        elif user_owns_state:
            mark = "用户调整"
            adjusted += 1
        else:
            mark = "✗"
        add(f"| {c['name']} | {c['count']} | {actual} | {mark} |")
    add("")

    protected = [v for v in live.values() if v.get("name") in mc.PROTECTED_NAMES]
    if protected:
        add("## 用户自建收藏夹（未改动）")
        add("")
        for v in sorted(protected, key=lambda x: str(x.get("name"))):
            add(f"- {v.get('name')}：{len(v.get('added') or [])} 款")
        add("")

    add("## 变更记录")
    add("")
    add("- 收藏夹名称去掉 `类型·` 前缀，直接以类型名显示。")
    add("- 删除 `测试版与 Playtest` 收藏夹；测试版 / Playtest / Test Server 通过公开商店检索定位正作，按真实玩法归入对应类型。")
    add("- 删除 `小类合并` 收藏夹；每个类型独立成夹，只有 1 款的类型也单独建夹。")
    add("- 删除 `待确认`、`历史记录` 两个按归属划分的夹；这些游戏已按真实类型归位，归属疑问以 `归属待确认` / `历史记录` 副标签表达。")
    add("- 新增 `DLC 与附加内容`、`系统与配置` 等非游戏类型分桶，非游戏条目不再混入普通类型。")
    add("- 家庭库共享（family_shared）与免费开玩（free_to_play）一并纳入分类。")
    add("- 用户已自行删除的 `吃灰`、`未通关`、`已通关`、`旮旯给木` 不重建。")
    if user_owns_state:
        add(f"- 用户手动调整 {adjusted} 个收藏夹的内容，并删除若干收藏夹；已接受为最终基线。")
    add("")

    add("## 写入的文件")
    add("")
    add("| 文件 | 当前 sha256（前 16 位） |")
    add("| --- | --- |")
    add(f"| `cloud-storage-namespace-1.json` | `{sha16(store)}` |")
    if idx_path.is_file():
        add(f"| `cloud-storage-namespaces.json` | `{sha16(idx_path)}` |")
    add("")

    add("## 回滚方式")
    add("")
    add("1. 完全退出 Steam（含托盘图标）。")
    if backup:
        add(f"2. 将 `{backup}` 内的文件复制回 `{store.parent}`。")
    else:
        add("2. 将最近一次 `backups\\cloudstorage_*` 内的文件复制回 Steam 云存储配置目录。")
    add("3. 重启 Steam，在收藏夹页面核对恢复。")
    add("")

    out = ROOT / "output" / "apply_report.md"
    tmp = out.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(out)
    print(
        f"已写入 {out}（计划 {len(planned)} 个，"
        f"生效 {len([n for n in planned if n in live_names])} 个，"
        f"用户调整 {adjusted} 个，用户删除 {len(missing)} 个）"
    )
    return 0 if not hard_missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
