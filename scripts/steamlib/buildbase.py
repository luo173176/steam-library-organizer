"""Find the retail game behind a playtest/beta build by public title search.

Some build AppIDs (`Neighbors: Suburban Warfare - Playtest`, `Rainbow Six Siege
- Test Server`) ship no store metadata of their own, so the local cache cannot
say what genre they are. The public store search endpoint can name the retail
game, and that entry carries genres.

Read-only, no authentication, results cached on disk with provenance.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"
UA = "SteamLibraryOrganizer/1.0 (read-only catalog lookup)"

# Build qualifiers stripped before searching.
_SUFFIXES = (
    " - playtest",
    " playtest",
    " - test server",
    " test server",
    " experimental server",
    " - beta",
    " beta",
    " closed beta",
    " open beta",
    " demo",
    " 测试服",
    " 测试版",
    "（测试版）",
    "(测试版)",
    " 测试",
    "测试",
)


def query_of(name: str) -> str:
    text = name.strip()
    low = text.casefold()
    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if low.endswith(suffix):
                trimmed = text[: len(text) - len(suffix)].strip(" -–—:：")
                # Never strip a title down to nothing (`测试` alone is not a name).
                if len(trimmed) < 2:
                    continue
                text = trimmed
                low = text.casefold()
                changed = True
    return text


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", text.casefold()) if len(t) > 1}


def _get(url: str, timeout: int) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class BuildBaseLookup:
    def __init__(self, cache_path: str | Path, timeout: int = 20, delay: float = 1.0):
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.delay = delay
        self.calls = 0
        self.errors: list[str] = []
        try:
            self.cache: dict[str, Any] = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.cache = {}
        self._dirty = False

    def flush(self) -> None:
        if not self._dirty:
            return
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.cache_path)
        self._dirty = False

    def resolve(self, build_name: str, known: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """Return {appid,name,genres,type,source} for the retail game, or None."""
        query = query_of(build_name)
        if len(query) < 3:
            return None
        cache_key = query.casefold()
        if cache_key in self.cache:
            return self.cache[cache_key] or None

        hit = self._search(query, known)
        self.cache[cache_key] = hit or {}
        self._dirty = True
        self.flush()
        if hit:
            time.sleep(self.delay)
        return hit

    # ---------- internals ----------
    def _search(self, query: str, known: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        want = _tokens(query)
        try:
            data = _get(
                f"{SEARCH}?{urllib.parse.urlencode({'term': query, 'l': 'english', 'cc': 'us'})}",
                self.timeout,
            )
            self.calls += 1
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            self.errors.append(f"search {query!r}: {exc}")
            return None

        best: tuple[float, dict[str, Any]] | None = None
        for item in (data.get("items") or [])[:8]:
            # storesearch labels retail entries "app"; appdetails calls the same
            # thing "game". Both are what we want; "dlc"/"bundle"/"music" are not.
            if str(item.get("type") or "app").casefold() not in ("app", "game"):
                continue
            appid = str(item.get("id") or "")
            name = str(item.get("name") or "")
            if not appid.isdigit() or not name:
                continue
            got = _tokens(name)
            if not got or not want:
                continue
            overlap = len(want & got) / max(len(want), 1)
            if overlap < 0.5:
                continue
            score = overlap + (0.2 if name.casefold().startswith(query.casefold()) else 0.0)
            if best is None or score > best[0]:
                best = (score, {"appid": appid, "name": name, "score": round(score, 3)})

        if best is None:
            return None

        candidate = best[1]
        appid = candidate["appid"]

        # If we already scanned the retail game, its classification is better
        # evidence than a fresh genre list — reuse it.
        if appid in known:
            donor = known[appid]
            candidate.update(
                {
                    "type": donor.get("type") or "game",
                    "genres": donor.get("genres") or [],
                    "source": f"owned-record:{appid}",
                    "known_genre": donor.get("primary_genre"),
                    "known_tags": donor.get("secondary_tags") or [],
                    "known_confidence": donor.get("confidence"),
                }
            )
            return candidate

        detail = self._details(appid)
        if not detail:
            return None
        candidate.update(
            {
                "type": detail.get("type") or "",
                "genres": [g.get("description", "") for g in detail.get("genres") or []],
                "source": f"store:storesearch+appdetails:{appid}",
            }
        )
        return candidate

    def _details(self, appid: str) -> dict[str, Any] | None:
        for lang, cc in (("schinese", "us"), ("schinese", "cn"), ("english", "us")):
            try:
                payload = _get(
                    f"{DETAILS}?{urllib.parse.urlencode({'appids': appid, 'l': lang, 'cc': cc})}",
                    self.timeout,
                )
                self.calls += 1
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                self.errors.append(f"details {appid}: {exc}")
                continue
            node = payload.get(str(appid)) or {}
            if node.get("success") and node.get("data"):
                return node["data"]
        return None


def make_lookup(cache_path: str | Path) -> Callable[[str, dict[str, dict[str, Any]]], dict[str, Any] | None]:
    client = BuildBaseLookup(cache_path)

    def lookup(build_name: str, known: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        try:
            return client.resolve(build_name, known)
        except Exception as exc:  # noqa: BLE001 - a lookup failure must not abort a scan
            client.errors.append(f"{build_name!r}: {exc}")
            return None

    lookup.client = client  # type: ignore[attr-defined]
    return lookup
