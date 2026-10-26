"""Resolve AppIDs that the default store endpoint refuses to describe.

The Chinese storefront (`cc=cn`) hides a number of entries: region-restricted
titles, delisted apps, and internal "Config" records. This module walks a
fixed, ordered chain of *public, read-only* sources and records which one
answered, so every filled-in field carries provenance.

Order (first hit wins, all results cached):
  1. store appdetails, cc=cn / schinese      (authoritative, region-local)
  2. store appdetails, cc=us / english       (same API, different region)
  3. steamcmd.net appinfo mirror             (third-party; lower confidence)

Nothing here authenticates, and nothing is written under the Steam directory.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import store

STEAMCMD = "https://api.steamcmd.net/v1/info/"
UA = "SteamLibraryOrganizer/1.0 (read-only catalog lookup)"

# steamcmd.net "type" -> Steam store type vocabulary used by classify.py
STEAMCMD_TYPE_MAP = {
    "game": "game",
    "demo": "demo",
    "dlc": "dlc",
    "tool": "tool",
    "config": "config",
    "music": "music",
    "video": "video",
    "hardware": "hardware",
    "series": "series",
    "beta": "beta",
    "mod": "mod",
}


class CatalogResolver:
    def __init__(self, cache_dir: str | Path, timeout: int = 25, delay: float = 1.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.delay = delay
        self.calls = 0
        self.failures: list[str] = []
        # Region fallback: same public endpoint, different storefront. Keep the
        # Chinese language so genre strings still match classify.py's vocabulary
        # (cc=us + l=schinese is served with Chinese genre names).
        self._fallback = store.StoreClient(self.cache_dir, lang="schinese", cc="us", timeout=timeout)

    # ---------- public ----------
    def resolve(self, appid: str, refresh: bool = False) -> dict[str, Any] | None:
        """Return a classify-compatible meta dict, or None if nothing answered."""
        appid = str(appid)
        cached = self.cache_dir / f"catalog_{appid}.json"
        if cached.is_file() and not refresh:
            try:
                payload = json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if payload is not None:
                return payload or None

        result = self._resolve_uncached(appid)
        cached.write_text(json.dumps(result or {}, ensure_ascii=False), encoding="utf-8")
        return result

    def resolve_many(self, appids: list[str], progress=None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        total = len(appids)
        for index, appid in enumerate(appids, 1):
            hit = self.resolve(appid)
            if hit:
                out[appid] = hit
            if progress:
                progress(index, total, appid, bool(hit))
            if index < total:
                time.sleep(self.delay)
        return out

    # ---------- internals ----------
    def _resolve_uncached(self, appid: str) -> dict[str, Any] | None:
        # 1/2: the same public store endpoint, two regions.
        data = self._fallback.details(appid)
        self.calls += 1
        if data and not data.get("__unavailable__"):
            meta = store.summarize(data)
            meta["catalog_source"] = "store:appdetails(cc=us)"
            meta["catalog_confidence"] = 0.9
            return meta

        # 3: public appinfo mirror.
        node = self._steamcmd(appid)
        if node:
            common = node.get("common") or {}
            raw_type = str(common.get("type") or "").strip()
            mapped = STEAMCMD_TYPE_MAP.get(raw_type.casefold(), "")
            name = str(common.get("name") or "").strip()
            if name:
                return {
                    "name": name,
                    "type": mapped,
                    "genres": [],
                    "categories": [],
                    "user_tags": [],
                    "short_description": "",
                    "developers": common.get("developers") or [],
                    "publishers": common.get("publishers") or [],
                    "release_date": "",
                    "catalog_source": f"steamcmd.net(appinfo type={raw_type or '?'})",
                    "catalog_confidence": 0.6,
                }
        return None

    def _steamcmd(self, appid: str) -> dict[str, Any] | None:
        req = urllib.request.Request(
            f"{STEAMCMD}{appid}",
            headers={"User-Agent": UA, "Accept": "application/json", "Connection": "close"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            self.failures.append(f"steamcmd {appid}: {exc}")
            return None
        return (payload.get("data") or {}).get(str(appid)) or None
