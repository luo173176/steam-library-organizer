"""Compare the LIVE Steam collection store against the visible library.

Read-only. Answers one question: which appids the client shows are in no
collection at all right now (i.e. what the user sees under 「未分类」).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_collections as ac  # noqa: E402
from steamlib.paths import project_root  # noqa: E402


def read_live(store_path: Path) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for item in json.loads(store_path.read_text(encoding="utf-8")):
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
        name = decoded.get("name")
        if name:
            out[str(name)] = [int(a) for a in (decoded.get("added") or [])]
    return out


def main() -> int:
    root = project_root()
    try:
        store_path, account_id = ac.locate_store()
    except SystemExit as exc:
        print(exc)
        return 2
    live = read_live(store_path)
    covered: dict[int, list[str]] = {}
    for name, appids in live.items():
        for a in appids:
            covered.setdefault(a, []).append(name)
    print(f"账号 {account_id}")
    print(f"收藏夹（按名称排序）：")
    for name in sorted(live):
        print(f"  {name}\t{len(live[name])}")

    ctx = json.loads((root / "data/processed/scan_context.json").read_text(encoding="utf-8"))
    visible = {int(x) for x in ctx.get("visible_appids", [])}
    recs = json.loads((root / "data/processed/records.json").read_text(encoding="utf-8"))
    recs = recs["records"] if isinstance(recs, dict) else recs
    names = {int(r["appid"]): r["name"] for r in recs}

    print(f"收藏夹: {len(live)} 个，条目并集 {len(covered)} 个 AppID")
    print(f"客户端可见: {len(visible)}")
    missing = sorted(visible - set(covered))
    print(f"未分类: {len(missing)}")
    for a in missing:
        print(f"  {a}\t{names.get(a, '(不在 records 中)')}")
    extra = sorted(set(covered) - visible)
    print(f"收藏夹中但客户端不显示: {len(extra)}")
    for a in extra:
        print(f"  {a}\t{names.get(a, '?')}\t{','.join(covered[a])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
