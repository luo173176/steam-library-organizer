"""Migrate the collection store from the old plan to the current one.

Old state: 20 collections named ``类型·<category>`` written earlier.
New state: plain category names, family-shared games included.

What this does:
  1. writes the new collections (plain names)
  2. deletes the superseded ``类型·`` entries the user no longer wants,
     using the client's own tombstone format (``is_deleted: true``)
  3. never touches collections that are neither old-plan nor new-plan
     (e.g. the user's own 收藏夹 / 已隐藏, or 测试·整理验证)

Default: dry run. ``--apply`` writes. Requires Steam fully closed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
import build_collection_patch as bcp  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()

# Collections we created earlier and that the new plan supersedes.
OLD_PREFIX = "类型·"
# Our own canary artifact; not part of the plan and should not linger.
CANARY_NAME = "测试·整理验证"
# Explicitly retired collections: superseded by per-genre buckets (v3 plan).
RETIRED_NAMES = {"测试版与 Playtest", "小类合并", "待确认", "历史记录"}
# Never delete these, whatever else happens.
PROTECTED_NAMES = {"收藏夹", "已隐藏"}


def load_live(store: Path) -> dict[str, dict]:
    live: dict[str, dict] = {}
    for item in json.loads(store.read_text(encoding="utf-8")):
        if not isinstance(item, list) or len(item) != 2:
            continue
        key, payload = item
        if not str(key).startswith("user-collections.") or not isinstance(payload, dict):
            continue
        if payload.get("is_deleted") or not payload.get("value"):
            continue
        try:
            live[str(key)] = json.loads(payload["value"])
        except json.JSONDecodeError:
            continue
    return live


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help="用户已接受自己的手动调整时，仍然按计划覆盖（会回滚用户改动）",
    )
    ap.add_argument("--keep-test", action="store_true",
                    help="保留金丝雀测试收藏夹（默认删除）")
    args = ap.parse_args()

    store, account_id = ac.locate_store()
    idx_path = ac.namespace_index(store)
    live = load_live(store)

    plan = json.loads((ROOT / "output" / "collection_plan.json").read_text(encoding="utf-8"))
    planned_names = {c["name"] for c in plan["collections"]}

    # 1. New collections still missing
    patch = bcp.build_patch(
        json.loads(store.read_text(encoding="utf-8")), plan, int(time.time())
    )
    additions = patch["to_add"]
    updates = patch["name_conflicts_needing_append"]  # same name, wrong contents

    # 2. Old collections to retire
    retire = []
    for key, val in live.items():
        name = val.get("name") or ""
        if name in PROTECTED_NAMES or name in planned_names:
            continue
        if (
            name.startswith(OLD_PREFIX)
            or name in RETIRED_NAMES
            or (name == CANARY_NAME and not args.keep_test)
        ):
            retire.append({"key": key, "name": name, "count": len(val.get("added") or [])})

    print(f"账号: {account_id}")
    print(f"存储: {store}")
    print(f"\n现有收藏夹 {len(live)} 个")
    print(f"新增: {len(additions)} 个 / {sum(a['count'] for a in additions)} 个 AppID")
    print(f"更新内容: {len(updates)} 个")
    for u in updates:
        print(
            f"  ~ {u['name']}  现有 {u['existing_count']} -> 计划 {u['planned_count']}"
            f"（补 {len(u['missing'])}，去 {len(u.get('extra') or [])}）"
        )
    print(f"淘汰旧命名: {len(retire)} 个")
    for r in retire:
        print(f"  - {r['name']}  ({r['count']} 款)  {r['key']}")

    # The user's own edits are the truth once accepted. Re-applying the plan
    # would undo them (move games back, resurrect deleted buckets), so this is a
    # stop sign rather than a convenience: --force is the only way past it.
    accepted_path = ROOT / "output" / "user_edits_accepted.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8")) if accepted_path.is_file() else {}
    fingerprint = "|".join(sorted(f"{c['name']}:{len(c['appids'])}" for c in plan["collections"]))
    if (
        accepted.get("plan_fingerprint") == fingerprint
        and (updates or retire)
        and not args.force
    ):
        print(
            f"\n用户已于 {accepted.get('accepted_at')} 手动调整并接受为基线。"
            f"按计划写入会覆盖其改动（{len(updates)} 个收藏夹内容、"
            f"{len(retire)} 个收藏夹删除）。"
        )
        print("如需按计划重新覆盖，加 --force；否则不要执行 --apply。")
        return 6

    retire_names = {r["name"] for r in retire}
    kept = [
        v["name"]
        for v in live.values()
        if v.get("name") and v["name"] not in retire_names
    ]
    print(f"\n保留不动: {sorted(kept)}")

    if not additions and not retire and not updates:
        print("\n无需变更。")
        return 0

    procs = ac.running_processes()
    if procs:
        print(f"\nSteam 正在运行：{', '.join(procs)}")
        if args.apply:
            print("请先完全退出 Steam（含托盘图标）。未做任何修改。")
            return 3

    if not args.apply:
        print("\nDRY RUN：未写入。加 --apply 才会真正写入。")
        return 0

    # ---- real write ----
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "backups" / f"cloudstorage_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for path in (store, idx_path):
        shutil.copy2(path, backup_dir / path.name)
        before = ac.sha256(path)
        if ac.sha256(backup_dir / path.name) != before:
            print(f"备份校验失败：{path}")
            return 4
        hashes[path.name] = before
    (backup_dir / "sha256.txt").write_text(
        "\n".join(f"{v}  {k}" for k, v in hashes.items()) + "\n", encoding="utf-8"
    )
    print(f"\n已备份到 {backup_dir}")

    raw = json.loads(store.read_text(encoding="utf-8"))
    versions = [
        int(i[1].get("version") or 0)
        for i in raw
        if isinstance(i, list) and len(i) == 2 and isinstance(i[1], dict)
    ]
    next_version = max(versions) + 1
    now = int(time.time())

    # Add new entries
    existing_keys = {str(i[0]) for i in raw if isinstance(i, list) and len(i) == 2}
    out = list(raw)
    for item in additions:
        if item["key"] in existing_keys:
            print(f"拒绝覆盖 {item['key']}")
            return 4
        out.append([item["key"], item["payload"]])

    # Tombstone the retired ones, in place (same key, is_deleted)
    retire_keys = {r["key"] for r in retire}
    for i, item in enumerate(out):
        if not isinstance(item, list) or len(item) != 2:
            continue
        key = str(item[0])
        if key in retire_keys:
            out[i] = [
                key,
                {
                    "key": key,
                    "timestamp": now,
                    "is_deleted": True,
                    "version": str(next_version),
                },
            ]
            next_version += 1

    # Update collections whose contents drifted from the plan: rewrite `value`
    # with the planned AppID list, keeping the same key so the client sees an
    # edit rather than a delete+create.
    plan_by_name = {c["name"]: [int(a) for a in c["appids"]] for c in plan["collections"]}
    updated = 0
    for i, item in enumerate(out):
        if not isinstance(item, list) or len(item) != 2:
            continue
        key, payload = item
        if not isinstance(payload, dict) or payload.get("is_deleted") or not payload.get("value"):
            continue
        try:
            decoded = json.loads(payload["value"])
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue  # e.g. collection-bootstrap-complete stores "true"
        name = decoded.get("name")
        if name not in plan_by_name:
            continue
        want = plan_by_name[name]
        if set(decoded.get("added") or []) == set(want):
            continue
        new_value = json.dumps(
            {
                "id": decoded.get("id"),
                "name": name,
                "added": want,
                "removed": decoded.get("removed") or [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        new_payload = dict(payload)
        new_payload["value"] = new_value
        new_payload["version"] = str(next_version)
        new_payload["timestamp"] = now
        out[i] = [key, new_payload]
        next_version += 1
        updated += 1
    print(f"已就地更新 {updated} 个收藏夹的内容")

    def atomic_write(path: Path, doc) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        json.loads(tmp.read_text(encoding="utf-8"))
        tmp.replace(path)

    atomic_write(store, out)
    _, new_idx = ac.bump_namespace_index(idx_path, next_version - 1)
    atomic_write(idx_path, new_idx)
    print(f"已写入：新增 {len(additions)}，标记删除 {len(retire)}")

    # verify
    live2 = load_live(store)
    problems = []
    names2 = {v.get("name") for v in live2.values()}
    for c in plan["collections"]:
        if c["name"] not in names2:
            problems.append(f"新收藏夹缺失：{c['name']}")
            continue
        entry = next(v for v in live2.values() if v.get("name") == c["name"])
        if set(entry.get("added") or []) != set(int(a) for a in c["appids"]):
            problems.append(f"{c['name']} AppID 集合不符")
    for r in retire:
        if r["name"] in names2:
            problems.append(f"旧收藏夹未删除：{r['name']}")
    for name in PROTECTED_NAMES:
        if name in {v.get("name") for v in live.values()} and name not in names2:
            problems.append(f"受保护收藏夹被删除：{name}")

    if problems:
        print("\n校验失败，回滚：")
        for p in problems:
            print(f"  - {p}")
        for path in (store, idx_path):
            shutil.copy2(backup_dir / path.name, path)
        print("已恢复。")
        return 5

    print(f"\n校验通过：现有 {len(live2)} 个收藏夹")
    for v in sorted(live2.values(), key=lambda x: str(x.get("name"))):
        print(f"  {len(v.get('added') or []):4d}  {v.get('name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
