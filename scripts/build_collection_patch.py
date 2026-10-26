"""Generate the exact collection-store entries for the plan (dry run).

Reads (never writes) the client's collection store at
``userdata/<AccountID>/config/cloudstorage/cloud-storage-namespace-1.json``,
which this machine's own data showed is a JSON array of ``[key, payload]``
pairs whose ``value`` is a JSON *string* holding ``{id, name, added, removed}``.

Output: ``output/collection_patch.json`` — the entries that a separate,
guarded apply step would merge in, plus a diff against what already exists.
Nothing in the Steam directory is modified here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from steamlib import scanner  # noqa: E402
from steamlib.paths import steam_root  # noqa: E402

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+*"


def collection_id(name: str) -> str:
    """Deterministic id so re-running the generator cannot create duplicates."""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    token = base64.b64encode(digest)[:12].decode("ascii")
    mapped = "".join(ALPHABET[ord(ch) % len(ALPHABET)] for ch in token)
    return f"uc-{mapped}"


def load_store(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def decode_entries(raw: list) -> dict[str, dict]:
    """key -> {id, name, added, removed, deleted} for every collection entry."""
    out: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            continue
        key, payload = item
        if not str(key).startswith("user-collections."):
            continue
        if payload.get("is_deleted"):
            out[str(key)] = {"deleted": True}
            continue
        value = payload.get("value")
        if not value:
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        out[str(key)] = {
            "id": decoded.get("id"),
            "name": decoded.get("name"),
            "added": decoded.get("added") or [],
            "removed": decoded.get("removed") or [],
            "deleted": False,
        }
    return out


def build_patch(raw: list, plan: dict, now: int) -> dict:
    """Pure merge planner: existing store entries + plan -> patch document.

    Never mutates ``raw`` or ``plan``. Keeps every existing key untouched and
    only produces new entries for names that do not exist yet.
    """
    existing = decode_entries(raw)
    existing_by_name = {
        entry["name"]: key for key, entry in existing.items() if not entry.get("deleted")
    }

    versions = [
        int(item[1].get("version") or 0)
        for item in raw
        if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict)
    ]
    next_version = (max(versions) if versions else 0) + 1

    additions: list[dict] = []
    already: list[dict] = []
    conflicts: list[dict] = []
    taken_keys = {str(item[0]) for item in raw if isinstance(item, list) and len(item) == 2}
    planned_keys: set[str] = set()
    planned_names: set[str] = set()

    for entry in plan.get("collections", []):
        name = entry["name"]
        appids = [int(a) for a in entry["appids"]]
        if name in existing_by_name:
            key = existing_by_name[name]
            current = set(existing[key]["added"])
            missing = [a for a in appids if a not in current]
            record = {
                "name": name,
                "key": key,
                "existing_count": len(current),
                "planned_count": len(appids),
                "missing": missing,
                "extra": sorted(current - set(appids)),
            }
            if missing or record["extra"]:
                conflicts.append(record)
            else:
                already.append(record)
            continue

        cid = collection_id(name)
        key = f"user-collections.{cid}"
        # Deterministic id could in principle collide with an unrelated live
        # key; skip rather than overwrite someone else's collection.
        if key in taken_keys or key in planned_keys or name in planned_names:
            conflicts.append(
                {
                    "name": name,
                    "key": key,
                    "existing_count": 0,
                    "planned_count": len(appids),
                    "missing": appids,
                }
            )
            continue
        planned_keys.add(key)
        planned_names.add(name)

        payload_value = json.dumps(
            {"id": cid, "name": name, "added": appids, "removed": []},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        additions.append(
            {
                "key": key,
                "payload": {
                    "key": key,
                    "timestamp": now,
                    "value": payload_value,
                    "version": str(next_version),
                    "conflictResolutionMethod": "custom",
                    "strMethodId": "union-collections",
                },
                "name": name,
                "count": len(appids),
            }
        )
        next_version += 1

    return {
        "generated_at": now,
        "source_store": "",
        "steam_must_be_closed": True,
        "writes_performed": False,
        "existing_collections": [
            {"name": v["name"], "key": k, "count": len(v["added"])}
            for k, v in sorted(existing.items(), key=lambda kv: str(kv[1].get("name")))
            if not v.get("deleted")
        ],
        "to_add": additions,
        "already_complete": already,
        "name_conflicts_needing_append": conflicts,
        "summary": {
            "new_collections": len(additions),
            "appids_to_add": sum(a["count"] for a in additions),
            "already_complete": len(already),
            "conflicts": len(conflicts),
        },
    }


def main() -> int:
    steam = str(steam_root())
    users = scanner.list_steam_users(steam)
    primary = next((u for u in users if u.get("role") == "primary"), None)
    if not primary:
        print("未找到主账号")
        return 2

    store_path = (
        Path(steam)
        / "userdata"
        / primary["account_id"]
        / "config"
        / "cloudstorage"
        / "cloud-storage-namespace-1.json"
    )
    plan_path = ROOT / "output" / "collection_plan.json"
    if not store_path.is_file():
        print(f"收藏夹存储文件不存在：{store_path}")
        return 2
    if not plan_path.is_file():
        print("缺少 output/collection_plan.json，请先运行 run_scan.py")
        return 2

    now = int(time.time())
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    patch = build_patch(load_store(store_path), plan, now)
    patch["source_store"] = str(store_path)
    additions = patch["to_add"]
    already = patch["already_complete"]
    conflicts = patch["name_conflicts_needing_append"]
    out = ROOT / "output" / "collection_patch.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)

    print(f"已生成 {out}")
    print(f"现有收藏夹 {len(patch['existing_collections'])} 个")
    print(f"拟新增收藏夹 {len(additions)} 个，共 {patch['summary']['appids_to_add']} 个 AppID")
    print(f"同名且已完整：{len(already)}；同名但缺条目：{len(conflicts)}")
    for item in additions:
        print(f"  + {item['count']:3d}  {item['name']}  ({item['key']})")
    for item in conflicts:
        print(f"  ! 已存在 {item['name']}，缺 {len(item['missing'])} 个 AppID")
    print("\n未写入 Steam 目录（dry run）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
