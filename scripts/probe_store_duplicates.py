"""Read-only: is any AppID listed in more than one collection right now?"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import apply_collections as ac  # noqa: E402
import migrate_collections as mc  # noqa: E402


def main() -> int:
    store, _ = ac.locate_store()
    live = mc.load_live(store)
    where: dict[str, list[str]] = defaultdict(list)
    for value in live.values():
        for appid in value.get("added") or []:
            where[str(appid)].append(value.get("name") or "?")
    dupes = {a: n for a, n in where.items() if len(n) > 1}
    print(f"收藏夹 {len(live)} 个；唯一 AppID {len(where)} 个；重复归属 {len(dupes)} 个")
    for appid, names in sorted(dupes.items(), key=lambda kv: kv[1]):
        print(f"  {appid:>9}  {' + '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
