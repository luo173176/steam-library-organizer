"""Read-only: for apps the client renders but no collection covers, is the
record's genre trustworthy enough to place it, or is it genuinely unknown?

Prints store evidence (genres / categories / type) so the decision is auditable
instead of guessed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steamlib.paths import project_root, steam_root  # noqa: E402

ROOT = project_root()


def main() -> int:
    steam = steam_root()
    ctx = json.loads((ROOT / "data" / "processed" / "scan_context.json").read_text(encoding="utf-8"))
    primary = next((u for u in ctx.get("users") or [] if u.get("role") == "primary"), None)
    if not primary:
        print("未找到主账号")
        return 2
    cache = steam / "userdata" / primary["account_id"] / "config" / "librarycache"
    visible = {int(p.stem) for p in cache.glob("*.json") if p.stem.isdigit()}

    recs = json.loads((ROOT / "data" / "processed" / "records.json").read_text(encoding="utf-8"))
    recs = recs["records"] if isinstance(recs, dict) else recs
    by_id = {int(r["appid"]): r for r in recs}

    store_dir = ROOT / "data" / "processed" / "store_cache"
    targets = sorted(a for a in visible if by_id.get(a, {}).get("primary_genre") in ("待确认", "其他", "", None))
    print(f"可见 {len(visible)}；其中类型为待确认/其他: {len(targets)}\n")
    for appid in targets:
        r = by_id[appid]
        raw = None
        for candidate in store_dir.glob(f"appdetails_*_{appid}.json"):
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            break
        print(f"{appid}  {r['name']}")
        print(f"   app_type={r['app_type']}  ownership={r['ownership_status']}  conf={r['confidence']}")
        if raw and not raw.get("__unavailable__"):
            print(f"   store.type={raw.get('type')}  genres={[g.get('description') for g in raw.get('genres', [])]}")
            print(f"   categories={[c.get('description') for c in raw.get('categories', [])][:8]}")
        else:
            print("   store: 无缓存")
        print(f"   evidence={r.get('evidence', '')[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
