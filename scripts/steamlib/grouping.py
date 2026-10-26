"""Alternative grouping axes for the collection plan.

Genre is the default because it is the only axis with real evidence per app.
The others are requested by the user and are computed strictly from local or
API facts — never guessed:

  genre        primary_genre (classify.py)
  playtime     playtime_minutes from the client / GetOwnedGames; unknown stays
               its own bucket instead of being counted as "0 hours"
  release      release year parsed from the store's release_date
  installed     installed / not installed (from appmanifest files)
  last-played  last_played unix timestamp, bucketed by recency
  ownership    ownership_status, i.e. the ownership ladder itself

Every axis returns (label, sort_key) and a fixed bucket order, so the plan is
deterministic and empty buckets simply do not appear.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

UNKNOWN = "未知"


def _playtime(rec: Any) -> str:
    minutes = getattr(rec, "playtime_minutes", None)
    if minutes is None:
        return "游玩时间未知"
    if minutes <= 0:
        return "从未游玩"
    if minutes < 60:
        return "1 小时内"
    hours = minutes / 60
    if hours < 10:
        return "1–10 小时"
    if hours < 50:
        return "10–50 小时"
    if hours < 200:
        return "50–200 小时"
    return "200 小时以上"


PLAYTIME_ORDER = [
    "从未游玩",
    "1 小时内",
    "1–10 小时",
    "10–50 小时",
    "50–200 小时",
    "200 小时以上",
    "游玩时间未知",
]


def _release_year(rec: Any) -> int | None:
    meta = getattr(rec, "meta", None) or {}
    raw = str(meta.get("release_date") or "")
    match = re.search(r"(19|20)\d{2}", raw)
    return int(match.group(0)) if match else None


def _release(rec: Any) -> str:
    year = _release_year(rec)
    if year is None:
        return "发售年份未知"
    if year >= 2020:
        return "2020 年后发售"
    if year >= 2010:
        return "2010–2019 发售"
    if year >= 2000:
        return "2000–2009 发售"
    return "2009 年前发售"


RELEASE_ORDER = [
    "2020 年后发售",
    "2010–2019 发售",
    "2000–2009 发售",
    "2009 年前发售",
    "发售年份未知",
]


def _installed(rec: Any) -> str:
    return "已安装" if getattr(rec, "installed", False) else "未安装"


INSTALLED_ORDER = ["已安装", "未安装"]


def _last_played(rec: Any) -> str:
    ts = getattr(rec, "last_played", None)
    if not ts:
        return "从未启动"
    try:
        when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return "最近游玩未知"
    days = (datetime.now(timezone.utc) - when).days
    if days <= 7:
        return "最近 7 天玩过"
    if days <= 30:
        return "最近 30 天玩过"
    if days <= 180:
        return "最近半年玩过"
    if days <= 365:
        return "最近一年玩过"
    return "一年以上没玩"


LAST_PLAYED_ORDER = [
    "最近 7 天玩过",
    "最近 30 天玩过",
    "最近半年玩过",
    "最近一年玩过",
    "一年以上没玩",
    "从未启动",
    "最近游玩未知",
]


OWNERSHIP_LABELS = {
    "owned_verified": "本账号拥有",
    "owned_local_evidence": "本账号拥有（本地证据）",
    "likely_owned": "可能拥有（弱证据）",
    "family_shared": "家庭共享",
    "free_to_play": "免费开玩",
    "not_owned_former": "历史记录",
    "installed_only": "仅本地安装",
    "unknown": "归属待确认",
    "other_local_account": "其他账号",
}


def _ownership(rec: Any) -> str:
    return OWNERSHIP_LABELS.get(getattr(rec, "ownership_status", ""), "归属待确认")


OWNERSHIP_ORDER = [
    "本账号拥有",
    "本账号拥有（本地证据）",
    "可能拥有（弱证据）",
    "家庭共享",
    "免费开玩",
    "仅本地安装",
    "归属待确认",
    "历史记录",
]


def _genre(rec: Any) -> str:
    return getattr(rec, "primary_genre", UNKNOWN)


AXES: dict[str, dict[str, Any]] = {
    "genre": {"label": "游戏类型", "key": _genre, "order": None},
    "playtime": {"label": "游玩时间", "key": _playtime, "order": PLAYTIME_ORDER},
    "release": {"label": "发售年份", "key": _release, "order": RELEASE_ORDER},
    "installed": {"label": "是否已安装", "key": _installed, "order": INSTALLED_ORDER},
    "last-played": {"label": "最近游玩", "key": _last_played, "order": LAST_PLAYED_ORDER},
    "ownership": {"label": "归属状态", "key": _ownership, "order": OWNERSHIP_ORDER},
}


def names() -> list[str]:
    return list(AXES)


def resolve(axis: str) -> dict[str, Any]:
    key = (axis or "genre").strip().casefold()
    if key not in AXES:
        raise SystemExit(
            f"未知分类维度：{axis}。可用：{', '.join(AXES)}"
        )
    return AXES[key]


def bucket(axis: str, rec: Any) -> str:
    fn: Callable[[Any], str] = resolve(axis)["key"]
    return fn(rec)


def ordered_labels(axis: str, present: list[str]) -> list[str]:
    """Sort bucket labels for display: fixed order first, rest alphabetically."""
    order = resolve(axis)["order"]
    if not order:
        return present
    rank = {label: i for i, label in enumerate(order)}
    return sorted(present, key=lambda label: (rank.get(label, len(order)), label))
