"""Public Steam Store metadata access with caching and rate limiting."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://store.steampowered.com/api/appdetails"
UA = "SteamLibraryOrganizer/1.0 (read-only metadata)"


class StoreClient:
    def __init__(self, cache_dir: str | Path, lang: str = "schinese", cc: str = "cn", timeout: int = 20):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.lang = lang
        self.cc = cc
        self.timeout = timeout
        self.calls = 0
        self.errors: list[str] = []

    def _cache_path(self, appid: str) -> Path:
        return self.cache_dir / f"appdetails_{self.lang}_{self.cc}_{appid}.json"

    def details(self, appid: str, refresh: bool = False) -> dict[str, Any] | None:
        cached = self._cache_path(appid)
        if cached.is_file() and not refresh:
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        url = f"{API}?appids={appid}&l={self.lang}&cc={self.cc}"
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            self.errors.append(f"{appid}: {exc}")
            return None
        finally:
            self.calls += 1
        entry = payload.get(str(appid), {})
        data = entry.get("data") if entry.get("success") else None
        if data is None:
            # Negative cache to avoid hammering the endpoint on every run.
            data = {"__unavailable__": True}
        cached.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def details_many(self, appids: list[str], delay: float = 1.1, progress=None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        total = len(appids)
        for index, appid in enumerate(appids, 1):
            was_cached = self._cache_path(appid).is_file()
            data = self.details(appid)
            if data and not data.get("__unavailable__"):
                out[appid] = data
            if progress:
                progress(index, total, appid, bool(data and not data.get("__unavailable__")))
            # Only throttle real network calls; cached reads are local file I/O.
            if index < total and not was_cached:
                time.sleep(delay)
        return out


def summarize(data: dict[str, Any] | None) -> dict[str, Any]:
    """Extract only the fields needed for classification."""
    if not data or data.get("__unavailable__"):
        return {}
    return {
        "name": data.get("name", ""),
        "type": data.get("type", ""),
        "user_tags": [],
        "is_free": data.get("is_free"),
        "short_description": data.get("short_description", ""),
        "genres": [g.get("description", "") for g in data.get("genres", []) if isinstance(g, dict)],
        "categories": [c.get("description", "") for c in data.get("categories", []) if isinstance(c, dict)],
        "developers": data.get("developers", []),
        "publishers": data.get("publishers", []),
        "release_date": (data.get("release_date") or {}).get("date", ""),
        "coming_soon": (data.get("release_date") or {}).get("coming_soon", False),
        "metacritic": (data.get("metacritic") or {}).get("score"),
        "recommendations": (data.get("recommendations") or {}).get("total"),
    }
