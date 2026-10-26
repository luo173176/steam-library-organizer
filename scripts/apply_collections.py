"""Guarded writer for the Steam collection store.

Default is a dry run. A real write requires ``--apply`` AND Steam being fully
closed (steam.exe / steamwebhelper.exe / steamservice.exe absent).

Two files are written together, because the client keeps a per-namespace
version counter next to the store:

  config/cloudstorage/cloud-storage-namespace-1.json   the store itself
  config/cloudstorage/cloud-storage-namespaces.json    [[3,"0"],[1,"<maxver>"],[4,"0"]]

If namespace-1 gains entries with version 4358..4377 while namespaces.json
still says "4357", the client can treat its cached copy as newer. Both files
are therefore backed up, replaced atomically, and verified.

Safety properties:
  * never deletes or rewrites an existing key
  * backs up every touched file + sha256 before touching it
  * temp file -> fsync -> atomic replace
  * re-reads and verifies every planned key after writing
  * restores all backups automatically if verification fails
  * re-plans against the store as it is right now (fresh versions)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_collection_patch as bcp  # noqa: E402
from steamlib import scanner  # noqa: E402
from steamlib.paths import steam_root  # noqa: E402

BLOCKING = ("steam.exe", "steamwebhelper.exe", "steamservice.exe")


def steam_dir() -> Path:
    """Resolved lazily: importing this module must not fail on a bad path."""
    return steam_root()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def running_processes() -> list[str]:
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    found = []
    for line in out.splitlines():
        name = line.split(",", 1)[0].strip().strip('"').lower()
        if name in BLOCKING:
            found.append(name)
    return sorted(set(found))


def locate_store() -> tuple[Path, str]:
    steam = steam_dir()
    users = scanner.list_steam_users(str(steam))
    primary = next((u for u in users if u.get("role") == "primary"), None)
    if not primary:
        raise SystemExit("未找到主账号，停止。")
    path = (
        steam
        / "userdata"
        / primary["account_id"]
        / "config"
        / "cloudstorage"
        / "cloud-storage-namespace-1.json"
    )
    if not path.is_file():
        raise SystemExit(f"收藏夹存储文件不存在：{path}")
    return path, primary["account_id"]


def namespace_index(store: Path) -> Path:
    return store.parent / "cloud-storage-namespaces.json"


def regenerate_patch(store: Path) -> dict:
    plan_path = ROOT / "output" / "collection_plan.json"
    if not plan_path.is_file():
        raise SystemExit("缺少 output/collection_plan.json，请先运行 run_scan.py")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    patch = bcp.build_patch(json.loads(store.read_text(encoding="utf-8")), plan, int(time.time()))
    patch["source_store"] = str(store)
    return patch


def merge(raw: list, additions: list[dict]) -> list:
    """Append new entries. Existing pairs keep their order and content."""
    existing_keys = {str(item[0]) for item in raw if isinstance(item, list) and len(item) == 2}
    out = list(raw)
    for item in additions:
        if item["key"] in existing_keys:
            raise SystemExit(f"拒绝覆盖已存在的 key：{item['key']}")
        out.append([item["key"], item["payload"]])
    return out


def store_snapshot(store: Path) -> dict:
    """Authoritative before-state taken from the store file itself.

    The counts the client *displays* differ from the store's ``added`` length,
    so this records ``added`` verbatim per key.
    """
    out = []
    for item in json.loads(store.read_text(encoding="utf-8")):
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
        out.append(
            {
                "key": str(key),
                "name": decoded.get("name"),
                "appids": decoded.get("added") or [],
                "version": int(payload.get("version") or 0),
            }
        )
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store": str(store),
        "collections": sorted(out, key=lambda c: c["key"]),
    }


def bump_namespace_index(path: Path, new_max: int) -> tuple[list, list]:
    """Return (old, new) index docs with namespace 1's counter raised."""
    old = json.loads(path.read_text(encoding="utf-8"))
    new = []
    for pair in old:
        if isinstance(pair, list) and len(pair) == 2 and str(pair[0]) == "1":
            current = int(pair[1])
            new.append([pair[0], str(max(current, new_max))])
        else:
            new.append(pair)
    return old, new


def verify(store: Path, additions: list[dict], expected_max: int) -> list[str]:
    problems: list[str] = []
    raw = json.loads(store.read_text(encoding="utf-8"))
    live: dict[str, dict] = {}
    max_version = 0
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            continue
        key, payload = item
        if not isinstance(payload, dict):
            continue
        max_version = max(max_version, int(payload.get("version") or 0))
        if not str(key).startswith("user-collections.") or payload.get("is_deleted"):
            continue
        if not payload.get("value"):
            continue
        try:
            live[str(key)] = json.loads(payload["value"])
        except json.JSONDecodeError:
            problems.append(f"{key} 的 value 无法解析")
    for item in additions:
        decoded = live.get(item["key"])
        if not decoded:
            problems.append(f"缺少 {item['name']} ({item['key']})")
            continue
        expected = json.loads(item["payload"]["value"])
        if decoded.get("name") != expected["name"]:
            problems.append(f"{item['name']} 名称不符")
        if set(decoded.get("added") or []) != set(expected["added"]):
            problems.append(f"{item['name']} 的 AppID 集合不符")
        if decoded.get("removed"):
            problems.append(f"{item['name']} 的 removed 非空")
    if max_version != expected_max:
        problems.append(f"最大 version 为 {max_version}，预期 {expected_max}")
    idx = json.loads(namespace_index(store).read_text(encoding="utf-8"))
    ns1 = next((int(p[1]) for p in idx if isinstance(p, list) and str(p[0]) == "1"), None)
    if ns1 != expected_max:
        problems.append(f"namespaces.json 的 namespace-1 计数为 {ns1}，预期 {expected_max}")
    return problems


def write_report(
    store: Path,
    backup_dir: Path,
    hashes: dict[str, tuple[str, str]],
    additions: list[dict],
    skipped: list[dict],
    conflicts: list[dict],
    problems: list[str],
    restored: bool,
) -> Path:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = not problems and not restored
    lines = [
        "# Steam 收藏夹应用报告",
        "",
        f"状态：**{'已应用（APPLIED）' if ok else '回滚（ROLLED BACK）'}**",
        "",
        f"- 执行时间（UTC）：{stamp}",
        f"- 账号目录：`{store.parent.parent.parent.name}`",
        f"- 备份目录：`{backup_dir}`",
        f"- 新增收藏夹：{len(additions)} 个，"
        f"共 {sum(len(json.loads(i['payload']['value'])['added']) for i in additions)} 个 AppID",
        f"- 跳过（已存在且完整）：{len(skipped)} 个",
        f"- 同名冲突（已存在但缺条目，未处理）：{len(conflicts)} 个",
        "- 未删除、未修改任何既有收藏夹；未启动、下载、安装、卸载任何游戏。",
        "",
        "## 写入的文件",
        "",
        "| 文件 | 写入前 sha256 | 写入后 sha256 |",
        "| --- | --- | --- |",
    ]
    for name, (before, after) in hashes.items():
        lines.append(f"| `{name}` | `{before[:16]}…` | `{after[:16]}…` |")

    lines += ["", "## 新增明细", "", "| 收藏夹 | 条目数 | key |", "| --- | ---: | --- |"]
    for item in additions:
        count = len(json.loads(item["payload"]["value"])["added"])
        lines.append(f"| {item['name']} | {count} | `{item['key']}` |")
    for item in skipped:
        lines.append(f"| {item['name']}（跳过·已完整） | {item['existing_count']} | `{item['key']}` |")
    for item in conflicts:
        lines.append(
            f"| {item['name']}（冲突·缺 {len(item['missing'])}） | {item['existing_count']} | `{item['key']}` |"
        )

    lines += ["", "## 校验", ""]
    if problems:
        lines += ["失败项：", ""] + [f"- {p}" for p in problems]
        if restored:
            lines += ["", "已自动从备份恢复全部被写入的文件。"]
    else:
        lines.append("回读校验通过：每个新 key 的名称、AppID 集合与 version 计数均与计划一致。")
    lines += [
        "",
        "## 回滚方式",
        "",
        "1. 完全退出 Steam（含托盘图标）。",
        f"2. 将 `{backup_dir}` 里的文件复制回各自原路径。",
        "3. 重启 Steam，核对收藏夹恢复为执行前状态。",
        "",
    ]
    out = ROOT / "output" / "apply_report.md"
    tmp = out.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry run）")
    ap.add_argument(
        "--snapshot-only",
        action="store_true",
        help="只记录写入前的存储快照到 output/preflight_store_snapshot.json",
    )
    ap.add_argument(
        "--batch",
        type=int,
        default=0,
        help="每批写入多少个收藏夹（0=一次全部）；每批后回读校验，失败即回滚全部",
    )
    args = ap.parse_args()

    store, account_id = locate_store()
    idx_path = namespace_index(store)

    if args.snapshot_only:
        snap_path = ROOT / "output" / "preflight_store_snapshot.json"
        tmp_snap = snap_path.with_suffix(".json.tmp")
        tmp_snap.write_text(
            json.dumps(store_snapshot(store), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_snap.replace(snap_path)
        print(f"已记录写入前存储快照 {snap_path}")
        return 0

    patch = regenerate_patch(store)
    additions = patch["to_add"]
    skipped = patch["already_complete"]
    conflicts = patch["name_conflicts_needing_append"]

    print(f"账号: {account_id}")
    print(f"存储: {store}")
    print(f"索引: {idx_path}")
    print(f"新增: {len(additions)} 个收藏夹 / {patch['summary']['appids_to_add']} 个 AppID")
    print(f"已完整跳过: {len(skipped)}；同名冲突: {len(conflicts)}")
    for c in conflicts:
        print(f"  ! {c['name']} 已存在，缺 {len(c['missing'])} 个 AppID（本次不处理）")

    if not additions:
        print("\n没有需要新增的收藏夹。")
        return 0

    new_max = max(int(i["payload"]["version"]) for i in additions)
    old_idx, new_idx = bump_namespace_index(idx_path, new_max)
    print(f"namespaces.json: namespace-1 计数 {old_idx} -> {new_idx}")

    procs = running_processes()
    if procs:
        print(f"\nSteam 进程正在运行：{', '.join(procs)}")
        if args.apply:
            print("请先完全退出 Steam（含托盘图标）后重试。未做任何修改。")
            return 3
        print("（dry run 不受影响）")

    if not args.apply:
        if args.batch:
            batches = [
                additions[i : i + args.batch] for i in range(0, len(additions), args.batch)
            ]
            print(f"\n分批计划：{len(batches)} 批，每批最多 {args.batch} 个")
            for n, batch in enumerate(batches, 1):
                print(f"  第 {n} 批: " + "、".join(i["name"] for i in batch))
        print("\nDRY RUN：未写入任何文件。加 --apply 才会真正写入。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "backups" / f"cloudstorage_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    originals = {store: store.name, idx_path: idx_path.name}
    hashes: dict[str, tuple[str, str]] = {}
    manifest = []
    for path in originals:
        backup = backup_dir / path.name
        shutil.copy2(path, backup)
        before = sha256(path)
        if sha256(backup) != before:
            print(f"备份校验失败：{path}，中止。")
            return 4
        hashes[path.name] = (before, before)
        manifest.append(f"{before}  {path}")
    (backup_dir / "sha256.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"\n已备份到 {backup_dir}")

    def atomic_write(path: Path, doc) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        json.loads(tmp.read_text(encoding="utf-8"))
        tmp.replace(path)

    snap_path = ROOT / "output" / "preflight_store_snapshot.json"
    tmp_snap = snap_path.with_suffix(".json.tmp")
    tmp_snap.write_text(
        json.dumps(store_snapshot(store), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_snap.replace(snap_path)
    print(f"已记录写入前存储快照 {snap_path}")

    # Write in batches; verify after each. Any failure rolls back everything.
    batch_size = args.batch or len(additions)
    batches = [additions[i : i + batch_size] for i in range(0, len(additions), batch_size)]
    written: list[dict] = []
    problems: list[str] = []
    for n, batch in enumerate(batches, 1):
        print(f"\n[第 {n}/{len(batches)} 批] {len(batch)} 个收藏夹")
        raw = json.loads(store.read_text(encoding="utf-8"))
        atomic_write(store, merge(raw, batch))
        written.extend(batch)
        batch_max = max(int(i["payload"]["version"]) for i in batch)
        _, idx_doc = bump_namespace_index(idx_path, batch_max)
        atomic_write(idx_path, idx_doc)
        problems = verify(store, written, expected_max=batch_max)
        if problems:
            print("  校验失败：")
            for p in problems:
                print(f"    - {p}")
            break
        print(f"  校验通过（累计 {len(written)} 个）")

    for name in hashes:
        path = store if name == store.name else idx_path
        hashes[name] = (hashes[name][0], sha256(path))

    restored = False
    if problems:
        print("\n正在回滚全部写入：")
        for path in originals:
            shutil.copy2(backup_dir / path.name, path)
        restored = True
        for name in hashes:
            path = store if name == store.name else idx_path
            hashes[name] = (hashes[name][0], sha256(path))
        print("已恢复两个文件到写入前状态。")

    report = write_report(store, backup_dir, hashes, written, skipped, conflicts, problems, restored)
    print(f"报告已写入 {report}")
    if restored:
        return 5
    print("\n完成。请启动 Steam 并核对收藏夹，然后运行 python scripts\\verify_applied.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
