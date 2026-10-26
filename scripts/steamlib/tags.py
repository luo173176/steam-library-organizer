"""Fetch public Steam store user tags (read-only HTML scrape, cached).

Steam's appdetails API only exposes coarse genres/categories. The store page
carries community tags such as 类魂系列 / 类 Rogue / 农场模拟, which are the
strongest public signal for gameplay-based classification.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_TAG_RE = re.compile(r'class="app_tag"[^>]*>\s*([^<]+?)\s*<', re.S)


class TagClient:
    def __init__(self, cache_dir: str | Path, timeout: int = 25):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.calls = 0
        self.failures: list[str] = []

    def _path(self, appid: str) -> Path:
        return self.cache_dir / f"{appid}.json"

    def tags(self, appid: str) -> list[str] | None:
        cached = self._path(appid)
        if cached.is_file():
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        url = f"https://store.steampowered.com/app/{appid}/?l=schinese&cc=cn"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
                "Cookie": "birthtime=0; wants_mature_content=1; lastagecheckage=1-0-1990",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            found = [t.strip() for t in _TAG_RE.findall(body) if t.strip()]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.failures.append(f"{appid}: {exc}")
            return None
        finally:
            self.calls += 1
        cached.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        return found

    def fetch_many(self, appids: list[str], delay: float = 1.2, progress=None) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        total = len(appids)
        for index, appid in enumerate(appids, 1):
            was_cached = self._path(appid).is_file()
            tags = self.tags(appid)
            if tags:
                out[appid] = tags
            if progress:
                progress(index, total, appid, bool(tags))
            if index < total and not was_cached:
                time.sleep(delay)
        return out
