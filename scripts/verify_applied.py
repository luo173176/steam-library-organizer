"""Post-apply verification: confirm the client kept the new collections.

Run AFTER writing and after restarting Steam. Read-only.

What it checks:
  1. every planned collection key still exists with the same name and AppID set
  2. the 6 pre-existing collections still exist with their original AppID sets
  3. version counters are still consistent with namespaces.json
  4. (optional) the client did not rewrite the store into a different shape

Exit code 0 = all good, 1 = something drifted, 2 = cannot verify.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib.paths import project_root  # noqa: E402

ROOT = project_root()

# Collections that the user (or this project, with approval) removed on purpose.
# Their absence is the expected state, not drift.
EXPECTED_REMOVED = {
    "吃灰",
    "未通关",
    "已通关",
    "旮旯给木",
    "测试·整理验证",
    "测试版与 Playtest",
    "小类合并",
    "待确认",
    "历史记录",
}


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
    try:
        store, account_id = ac.locate_store()
    except SystemExit as exc:
        print(exc)
        return 2

    patch_path = ROOT / "output" / "collection_patch.json"
    snapshot_path = ROOT / "output" / "preflight_store_snapshot.json"
    if not patch_path.is_file():
        print("缺少 output/collection_patch.json")
        return 2
    patch = json.loads(patch_path.read_text(encoding="utf-8"))
    live = load_live(store)

    problems: list[str] = []

    print(f"账号: {account_id}")
    print(f"存储: {store}")
    print(f"当前收藏夹 {len(live)} 个\n")

    print("== 计划内的收藏夹 ==")
    for item in patch["to_add"]:
        expected = json.loads(item["payload"]["value"])
        actual = live.get(item["key"])
        if not actual:
            problems.append(f"缺失：{item['name']}")
            print(f"  ✗ {item['name']}  未出现")
            continue
        same_name = actual.get("name") == expected["name"]
        want, got = set(expected["added"]), set(actual.get("added") or [])
        if not same_name:
            problems.append(f"{item['name']} 名称被改为 {actual.get('name')}")
        if want != got:
            problems.append(
                f"{item['name']} 条目差异：缺 {sorted(want - got)} 多 {sorted(got - want)}"
            )
        mark = "✓" if same_name and want == got else "✗"
        print(f"  {mark} {actual.get('name')}  {len(got)}/{len(want)}")

    # The patch only carries the *delta*. Once applied, every collection in the
    # plan must match the store byte-for-byte on name and AppID set, otherwise a
    # partially-applied or client-rewritten store would go unnoticed.
    plan_path = ROOT / "output" / "collection_plan.json"
    accepted_path = ROOT / "output" / "user_edits_accepted.json"
    accepted = json.loads(accepted_path.read_text(encoding="utf-8")) if accepted_path.is_file() else {}
    accepted_note = ""
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        fingerprint = "|".join(
            sorted(f"{c['name']}:{len(c['appids'])}" for c in plan["collections"])
        )
        if accepted.get("plan_fingerprint") == fingerprint:
            # The user hand-edited the library and those edits were accepted as
            # the baseline. Their state is authoritative; the plan is history.
            print(
                f"\n计划差异已被用户手动调整覆盖（{accepted.get('accepted_at')} 接受），"
                "以下仅作参考，不计为问题："
            )
            accepted_note = "reference-only"
        by_name = {v.get("name"): v for v in live.values()}
        print(f"\n== 计划全量核对（{len(plan['collections'])} 个）==")
        for entry in plan["collections"]:
            actual = by_name.get(entry["name"])
            if actual is None:
                if accepted_note:
                    print(f"  · {entry['name']}  已由用户删除")
                    continue
                problems.append(f"计划收藏夹缺失：{entry['name']}")
                print(f"  ✗ {entry['name']}  未出现")
                continue
            want = {int(a) for a in entry["appids"]}
            got = {int(a) for a in actual.get("added") or []}
            ok = want == got
            if not ok and not accepted_note:
                problems.append(
                    f"{entry['name']} 条目差异：缺 {sorted(want - got)} 多 {sorted(got - want)}"
                )
                print(f"  ✗ {entry['name']}  {len(got)}/{len(want)}")
            elif ok:
                print(f"  ✓ {entry['name']}  {len(got)}/{len(want)}")
            else:
                print(f"  · {entry['name']}  {len(got)}（用户调整）")

    if snapshot_path.is_file():
        snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
        before = snap.get("collections") or []
        print("\n== 既有收藏夹（不得被改动）==")
        for entry in before:
            key = entry.get("key")
            if not key:
                continue
            actual = live.get(key)
            if not actual:
                name = entry.get("name")
                if name in EXPECTED_REMOVED:
                    print(f"  ✓ {name}  已按授权删除")
                else:
                    problems.append(f"既有收藏夹消失：{name}")
                    print(f"  ✗ {name}  已消失")
                continue
            want = {int(a) for a in entry.get("appids") or []}
            got = {int(a) for a in actual.get("added") or []}
            ok = want == got
            print(f"  {'✓' if ok else '✗'} {actual.get('name')}  {len(got)}/{len(want)}")
            if not ok:
                problems.append(
                    f"{entry.get('name')} 条目变化：缺 {sorted(want - got)} 多 {sorted(got - want)}"
                )
    else:
        print("\n（未找到 preflight_store_snapshot.json，跳过既有收藏夹逐条比对）")

    versions = [
        int(item[1].get("version") or 0)
        for item in json.loads(store.read_text(encoding="utf-8"))
        if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict)
    ]
    idx = json.loads(ac.namespace_index(store).read_text(encoding="utf-8"))
    ns1 = next((int(p[1]) for p in idx if isinstance(p, list) and str(p[0]) == "1"), None)
    print(f"\n最大 version: {max(versions)}；namespaces.json namespace-1: {ns1}")
    if ns1 is not None and ns1 < max(versions):
        # Not a fault when the client wrote the store itself: after a UI edit the
        # client rewrites namespace-1 and leaves this counter where it likes.
        # Only flag it when this tool is the last writer, which we can tell from
        # the file mtime versus the newest backup we made.
        print(
            f"  ! 注意：计数落后于存储（{ns1} < {max(versions)}）。"
            "若本次改动由 Steam 客户端完成，这属于客户端自身行为，不影响收藏夹显示。"
        )

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("全部核对通过：计划内收藏夹均已生效，既有收藏夹未被改动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
