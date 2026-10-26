"""Merge, classify and write all deliverables (CSV/JSON/Markdown)."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import classify as classify_mod
from . import grouping

FIELDS = [
    "appid",
    "name",
    "installed",
    "ownership_status",
    "ownership_detail",
    "app_type",
    "library_path",
    "install_dir",
    "size_on_disk_bytes",
    "last_updated",
    "last_played",
    "playtime_minutes",
    "primary_genre",
    "secondary_tags",
    "confidence",
    "evidence",
    "review_required",
    "source",
]

# Ownership ladder, strongest first. Every value is a distinct claim and must
# never be collapsed into a generic "owned" flag.
OWNERSHIP_STATUSES = {
    "owned_verified": "官方 GetOwnedGames 明确返回该 AppID",
    "owned_local_evidence": "本地客户端状态有配置级证据（localconfig）",
    "likely_owned": "仅 librarycache 缓存，弱证据",
    "family_shared": "本账号未拥有，家庭成员拥有，可共享游玩",
    "free_to_play": "免费游戏；无需购买即可游玩，不在 GetOwnedGames 中",
    "installed_only": "本地已安装但账号归属无法确认",
    "other_local_account": "仅出现在本机其他 Steam 账号下",
    "not_owned_former": "曾有游玩记录但当前无人拥有（退款/过期/免费周末）",
    "unknown": "无法确认",
}

# Statuses that count as "this account owns it".
OWNED_STATUSES = ("owned_verified", "owned_local_evidence", "likely_owned")
# Statuses that are playable right now without owning a paid licence.
# Free-to-play titles never appear in GetOwnedGames, so they are their own tier.
ACCESS_STATUSES = ("family_shared", "free_to_play")


@dataclass
class AppRecord:
    appid: str
    name: str
    installed: bool
    ownership_status: str
    app_type: str
    ownership_detail: str = ""
    library_path: str = ""
    install_dir: str = ""
    size_on_disk_bytes: int | None = None
    last_updated: int | None = None
    last_played: int | None = None
    # Client-reported minutes played (localconfig Playtime). None = not known.
    playtime_minutes: int | None = None
    primary_genre: str = "待确认"
    secondary_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: str = ""
    review_required: bool = True
    source: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "name": self.name,
            "installed": self.installed,
            "ownership_status": self.ownership_status,
            "ownership_detail": self.ownership_detail,
            "app_type": self.app_type,
            "library_path": self.library_path,
            "install_dir": self.install_dir,
            "size_on_disk_bytes": "" if self.size_on_disk_bytes is None else self.size_on_disk_bytes,
            "last_updated": "" if self.last_updated is None else self.last_updated,
            "last_played": "" if self.last_played is None else self.last_played,
            "playtime_minutes": "" if self.playtime_minutes is None else self.playtime_minutes,
            "primary_genre": self.primary_genre,
            "secondary_tags": "|".join(self.secondary_tags),
            "confidence": self.confidence,
            "evidence": self.evidence,
            "review_required": self.review_required,
            "source": "|".join(self.source),
        }


def build_records(
    installed: list[Any],
    owned_appids: Iterable[str],
    ownership_source: str,
    user_evidence: dict[str, set[str]],
    meta: dict[str, dict[str, Any]],
    name_hints: dict[str, str],
    play_state: dict[str, dict[str, int]] | None = None,
    api_owned: dict[str, dict[str, Any]] | None = None,
    family_owned: dict[str, list[str]] | None = None,
    api_queried: bool | None = None,
    base_lookup: Callable[[str, dict[str, dict[str, Any]]], dict[str, Any] | None] | None = None,
) -> tuple[list[AppRecord], list[str]]:
    """Merge manifests, account state and API ownership with AppID as the key.

    Ownership is resolved in strict order of evidence strength:

    1. ``owned_verified`` — GetOwnedGames returned the AppID for this account.
    2. ``owned_local_evidence`` / ``likely_owned`` — local client state only.
    3. ``family_shared`` — another family member owns it; playable, not ours.
    4. ``not_owned_former`` — local playtime exists but nobody owns it now.
    5. ``unknown`` — nothing but a stale cache entry.

    A weaker source never upgrades a stronger one, and ``family_shared`` is
    never reported as ownership.
    """
    notes: list[str] = []
    records: dict[str, AppRecord] = {}
    play_state = play_state or {}
    api_owned = api_owned or {}
    family_owned = family_owned or {}
    # ``api_queried`` distinguishes "the API ran and confirmed nothing" from
    # "the API was never called". Without it an account that legitimately owns
    # zero games would keep every local cache row marked as owned.
    if api_queried is None:
        api_queried = bool(api_owned or family_owned)

    for app in installed:
        rec = AppRecord(
            appid=app.appid,
            name=app.name,
            installed=True,
            ownership_status="unknown",
            app_type="unknown",
            library_path=app.library_path,
            install_dir=app.installdir,
            size_on_disk_bytes=app.size_on_disk,
            last_updated=app.last_updated,
            last_played=app.last_played,
            source=[f"manifest:{app.manifest_path}"],
            anomalies=list(app.anomalies),
        )
        records[app.appid] = rec

    for appid in sorted(set(owned_appids), key=lambda s: int(s) if str(s).isdigit() else 0):
        appid = str(appid)
        evidence = user_evidence.get(appid, set())
        # 兼容不带账号后缀的旧调用：裸标签视为主账号证据。
        primary_evidence = {e for e in evidence if "@" not in e or e.endswith("@primary")}
        secondary_evidence = {e for e in evidence if e.endswith("@secondary")}
        if primary_evidence:
            status = (
                "owned_local_evidence"
                if any(e.startswith("localconfig") for e in primary_evidence)
                else "likely_owned"
            )
        elif secondary_evidence:
            # 只在另一个本地账号下出现过：不能算主账号拥有。
            status = "other_local_account"
        else:
            status = "unknown"
        if appid in records:
            rec = records[appid]
            rec.source.append(f"account:{ownership_source}")
            if evidence:
                rec.source.append("local-state:" + ",".join(sorted(evidence)))
            # 已安装但只出现在别的账号下时，保留 installed_only 语义。
            if status != "other_local_account" or rec.ownership_status == "unknown":
                rec.ownership_status = status
        else:
            records[appid] = AppRecord(
                appid=appid,
                name=name_hints.get(appid, f"appid_{appid}"),
                installed=False,
                ownership_status=status,
                app_type="unknown",
                source=[f"account:{ownership_source}"] + (["local-state:" + ",".join(sorted(evidence))] if evidence else []),
            )

    # --- API-verified ownership: the strongest claim, applied last on purpose.
    for appid, info in api_owned.items():
        appid = str(appid)
        rec = records.get(appid)
        if rec is None:
            rec = AppRecord(
                appid=appid,
                name=name_hints.get(appid) or str(info.get("name") or f"appid_{appid}"),
                installed=False,
                ownership_status="owned_verified",
                app_type="unknown",
                source=["api:GetOwnedGames"],
            )
            records[appid] = rec
        rec.ownership_status = "owned_verified"
        rec.ownership_detail = (
            f"GetOwnedGames 返回；累计游玩 {info.get('playtime_forever', 0)} 分钟"
        )
        if "api:GetOwnedGames" not in rec.source:
            rec.source.append("api:GetOwnedGames")
        if rec.name.startswith("appid_") and info.get("name"):
            rec.name = str(info["name"])
        if rec.playtime_minutes is None and info.get("playtime_forever") is not None:
            rec.playtime_minutes = int(info["playtime_forever"])
        if rec.last_played is None and info.get("rtime_last_played"):
            rec.last_played = int(info["rtime_last_played"])
        notes.append(f"API 核实拥有：{appid} {rec.name}")

    # --- Family sharing: playable, explicitly NOT owned by this account.
    for appid, owners in family_owned.items():
        appid = str(appid)
        rec = records.get(appid)
        if rec is None:
            records[appid] = AppRecord(
                appid=appid,
                name=name_hints.get(appid, f"appid_{appid}"),
                installed=False,
                ownership_status="family_shared",
                ownership_detail=f"家庭成员 {', '.join(owners)} 拥有",
                app_type="unknown",
                source=["api:GetOwnedGames(family member)"],
            )
            continue
        if rec.ownership_status in ("owned_verified", "other_local_account"):
            continue
        rec.ownership_status = "family_shared"
        rec.ownership_detail = f"家庭成员 {', '.join(owners)} 拥有；本账号未拥有"
        rec.source.append("api:GetOwnedGames(family member)")

    # Client-recorded playtime fills last_played/playtime for uninstalled games.
    # It is only applied to the primary account's own entries and never guessed.
    for appid, rec in records.items():
        state = play_state.get(appid)
        if not state or rec.ownership_status in ("other_local_account", "unknown"):
            continue
        if rec.last_played is None and state.get("last_played"):
            rec.last_played = int(state["last_played"])
            rec.source.append("local-state:localconfig@primary:LastPlayed")
        if rec.playtime_minutes is None and state.get("playtime_minutes") is not None:
            rec.playtime_minutes = int(state["playtime_minutes"])
            rec.source.append("local-state:localconfig@primary:Playtime")

    # Downgrade what the API explicitly did not confirm. Only runs when the API
    # was actually queried, otherwise local evidence stands as before.
    #
    # Free-to-play titles are the important exception: GetOwnedGames omits them
    # for accounts that never bought anything, yet they stay playable forever.
    # Calling them "no longer owned" would be wrong, so they get their own
    # status instead of being lumped in with refunds and expired licences.
    if api_queried:
        for appid, rec in records.items():
            if rec.ownership_status in ("owned_verified", "other_local_account"):
                continue
            if rec.ownership_status == "family_shared":
                continue
            meta_entry = meta.get(appid) or {}
            is_free = bool(meta_entry.get("is_free"))
            played = (rec.playtime_minutes or 0) > 0
            if is_free:
                rec.ownership_status = "free_to_play"
                rec.ownership_detail = (
                    "商店标记为免费游戏；无需购买即可游玩，"
                    "因此不出现在 GetOwnedGames 中"
                )
                rec.source.append("store:appdetails:is_free=1")
            elif played:
                rec.ownership_status = "not_owned_former"
                rec.ownership_detail = (
                    f"本地有 {rec.playtime_minutes} 分钟游玩记录，但 GetOwnedGames 与家庭组均未返回该 AppID"
                )
                rec.anomalies.append("有游玩记录但当前无拥有证据（可能退款/免费周末/订阅过期）")
            elif rec.installed:
                rec.ownership_status = "installed_only"
                rec.anomalies.append("本地已安装但 API 未确认拥有")
            else:
                rec.ownership_status = "unknown"
                rec.ownership_detail = "仅缓存残留，无游玩记录，API 未返回"

    # A manifest alone never proves ownership: it may be a shared/leftover install.
    for appid, rec in records.items():
        if rec.installed and rec.ownership_status == "unknown":
            rec.ownership_status = "installed_only"
            rec.anomalies.append("本地已安装但账号拥有状态无法确认")

    for appid, rec in records.items():
        if appid in meta:
            rec.meta = meta[appid]
            store_name = meta[appid].get("name")
            if store_name and (rec.name.startswith("appid_") or rec.name != store_name):
                if rec.name.startswith("appid_"):
                    rec.name = store_name
                elif store_name.casefold() != rec.name.casefold():
                    rec.anomalies.append(f"名称不一致：本地={rec.name} / 商店={store_name}")
            rec.source.append("store:appdetails")
        cls = classify_mod.classify_app(rec.name, rec.meta, installed_name=rec.install_dir)
        rec.app_type = cls.app_type
        rec.primary_genre = cls.primary
        rec.secondary_tags = cls.secondary
        rec.confidence = cls.confidence
        rec.evidence = cls.evidence
        # review_required tracks classification/data quality only; ownership
        # uncertainty is already explicit in ownership_status.
        rec.review_required = cls.review_required or bool(rec.anomalies)

    # Network lookups go last, only for builds the local evidence could not
    # place. Passing None keeps the merge fully offline.
    _inherit_genre_for_playtests(records, base_lookup=base_lookup)

    # Ownership doubt travels as a secondary tag, not as a collection name: the
    # game keeps its real genre, and the client still surfaces the caveat.
    for rec in records.values():
        if rec.app_type not in ("game", "测试版"):
            continue
        if rec.ownership_status in ("unknown", "installed_only"):
            tag = "归属待确认"
        elif rec.ownership_status == "not_owned_former":
            tag = "历史记录"
        else:
            continue
        if tag not in rec.secondary_tags:
            rec.secondary_tags.append(tag)

    return sorted(records.values(), key=lambda r: (r.primary_genre, r.name.casefold())), notes


# Build qualifiers that say nothing about genre. Stripping them leaves the name
# of the retail game the build belongs to.
_BUILD_SUFFIXES = (
    " - playtest",
    " playtest",
    " - test server",
    " test server",
    " experimental server",
    " - beta",
    " beta",
    " 测试版",
    " 测试服",
    " 测试",
    "测试版",
    "测试服",
    "测试",
    "（测试版）",
    "(测试版)",
    " - demo",
    " demo",
    " 试用版",
    " 试玩版",
    " playtest version",
)


def _base_title(name: str) -> str:
    """`The Finals Playtest` -> `the finals`; used only for lookups."""
    text = name.casefold().strip()
    changed = True
    while changed:
        changed = False
        for suffix in _BUILD_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _inherit_genre_for_playtests(
    records: dict[str, AppRecord],
    base_lookup: Callable[[str, dict[str, dict[str, Any]]], dict[str, Any] | None] | None = None,
) -> None:
    """Give playtest/beta/demo rows the genre of the game they belong to.

    A playtest is a *build*, not a genre: 「皇帝麻将 Playtest」 belongs in
    卡牌与牌组构筑 next to 皇帝游戏, not in a bucket called 测试版. Steam often
    ships no genres for these AppIDs, so the retail entry is the best evidence.

    Only inherits when the source record has a real, evidence-backed genre
    (not 待确认/其他), and records the inheritance in `evidence` + `source` so
    the classification stays auditable.

    When no retail entry exists locally (the playtest is owned/downloaded but
    the base game is not on this account), `base_lookup` resolves the retail
    game through public store search. Those hits are marked with the retail
    AppID and keep a lower confidence, because the link is by title, not by a
    declared appinfo relationship.
    """
    by_title: dict[str, AppRecord] = {}
    for rec in records.values():
        if rec.app_type != "game":
            continue
        if rec.primary_genre in ("待确认", "其他"):
            continue
        by_title.setdefault(_base_title(rec.name), rec)

    # What we already know about every app, keyed by AppID. A playtest's base
    # game may be in this very inventory, in which case its classification is
    # stronger evidence than anything fetched over the network.
    known: dict[str, dict[str, Any]] = {
        rec.appid: {
            "name": rec.name,
            "type": "game" if rec.app_type == "game" else rec.app_type,
            "genres": rec.meta.get("genres") or [],
            "primary_genre": rec.primary_genre,
            "secondary_tags": rec.secondary_tags,
            "confidence": rec.confidence,
        }
        for rec in records.values()
    }

    for rec in records.values():
        if rec.app_type not in ("测试版", "试玩版"):
            continue
        if rec.primary_genre not in ("待确认", "其他"):
            continue
        base = _base_title(rec.name)
        if not base:
            continue
        donor = by_title.get(base)
        if donor is None:
            # Prefix match covers subtitles: `PUBG: Experimental Server` ->
            # `PUBG: BATTLEGROUNDS`, `猛兽派对测试` -> `猛兽派对`.
            candidates = [
                (title, src)
                for title, src in by_title.items()
                if len(title) >= 3 and (title.startswith(base) or base.startswith(title))
            ]
            if candidates:
                candidates.sort(key=lambda kv: -len(kv[0]))
                donor = candidates[0][1]
        if donor is None:
            if base_lookup is None:
                continue
            remote = base_lookup(rec.name, known)
            if not remote:
                continue
            if remote.get("known_genre"):
                # The retail game is in our own inventory: reuse its verified
                # classification verbatim rather than re-deriving it.
                rec.primary_genre = remote["known_genre"]
                rec.secondary_tags = list(
                    dict.fromkeys([*rec.secondary_tags, *(remote.get("known_tags") or [])])
                )[:12]
                conf = min(float(remote.get("known_confidence") or 0.7), 0.8)
            else:
                probe = classify_mod.classify_app(
                    remote.get("name") or rec.name,
                    {
                        "type": remote.get("type") or "game",
                        "genres": remote.get("genres") or [],
                        "user_tags": [],
                        "short_description": "",
                        "categories": [],
                    },
                )
                if probe.primary in ("待确认", "其他"):
                    continue
                rec.primary_genre = probe.primary
                rec.secondary_tags = list(
                    dict.fromkeys([*rec.secondary_tags, *probe.secondary])
                )[:12]
                conf = min(probe.confidence, 0.7)
            rec.confidence = round(conf, 2)
            rec.evidence = (
                f"测试/试玩构建，自身无类型元数据；公开商店检索匹配到正作"
                f"《{remote.get('name')}》({remote.get('appid')})，按该作玩法归类"
                f"（依据：{remote.get('source')}）"
            )
            rec.source.append(f"buildbase:{remote.get('appid')}")
            rec.review_required = True
            continue
        rec.primary_genre = donor.primary_genre
        rec.secondary_tags = list(
            dict.fromkeys([*rec.secondary_tags, *donor.secondary_tags])
        )[:12]
        rec.confidence = round(min(donor.confidence, 0.75), 2)
        rec.evidence = (
            f"测试/试玩构建，无独立类型元数据；继承正作《{donor.name}》的"
            f"主分类 {donor.primary_genre}（置信度 {donor.confidence}）"
        )
        rec.source.append(f"inherit:{donor.appid}")
        rec.review_required = True


def write_inventory(records: list[AppRecord], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "games_inventory.csv"
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec.to_row())
    tmp.replace(csv_path)

    json_path = output_dir / "games_inventory.json"
    tmp_json = json_path.with_suffix(".json.tmp")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(records),
        "games": [
            {
                **{k: v for k, v in rec.to_row().items() if k != "secondary_tags"},
                "secondary_tags": rec.secondary_tags,
                "anomalies": rec.anomalies,
                "store": {
                    "type": rec.meta.get("type"),
                    "genres": rec.meta.get("genres", []),
                    "categories": rec.meta.get("categories", []),
                    "user_tags": rec.meta.get("user_tags", []),
                    "developers": rec.meta.get("developers", []),
                    "publishers": rec.meta.get("publishers", []),
                    "release_date": rec.meta.get("release_date"),
                },
            }
            for rec in records
        ],
    }
    tmp_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_json.replace(json_path)

    review_path = output_dir / "classification_review.csv"
    tmp_review = review_path.with_suffix(".csv.tmp")
    with tmp_review.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["appid", "name", "primary_genre", "secondary_tags", "evidence", "confidence", "review_required"]
        )
        for rec in records:
            writer.writerow(
                [
                    rec.appid,
                    rec.name,
                    rec.primary_genre,
                    "|".join(rec.secondary_tags),
                    rec.evidence,
                    rec.confidence,
                    rec.review_required,
                ]
            )
    tmp_review.replace(review_path)

    unresolved_path = output_dir / "unresolved_games.csv"
    tmp_unresolved = unresolved_path.with_suffix(".csv.tmp")
    with tmp_unresolved.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["appid", "name", "issue", "ownership_status", "app_type", "source", "detail"])
        for rec in records:
            issues: list[str] = []
            # Only list entries whose ownership or classification still needs a
            # human decision; API-verified ownership removes the old blanket
            # "needs API verification" note for every weak local row.
            if rec.ownership_status == "not_owned_former":
                issues.append("有游玩记录但当前无拥有证据（退款/免费周末/订阅过期），需人工确认是否保留")
            elif rec.ownership_status == "family_shared":
                issues.append("家庭共享可玩，非本账号拥有")
            elif rec.ownership_status == "likely_owned":
                issues.append("拥有状态为弱本地证据（仅 librarycache）")
            elif rec.ownership_status == "installed_only":
                issues.append("仅有本地安装记录，无法确认拥有状态")
            elif rec.ownership_status == "other_local_account":
                issues.append("仅出现在本机其他 Steam 账号下，不属于主账号")
            elif rec.ownership_status == "unknown":
                issues.append("仅缓存残留，无拥有证据")
            if rec.primary_genre == "待确认":
                issues.append("类型未确认")
            if rec.app_type == "unknown":
                issues.append("应用类型未知")
            if rec.anomalies:
                issues.append("数据异常")
            if not issues:
                continue
            writer.writerow(
                [
                    rec.appid,
                    rec.name,
                    "；".join(issues),
                    rec.ownership_status,
                    rec.app_type,
                    "|".join(rec.source),
                    "；".join(rec.anomalies),
                ]
            )
    tmp_unresolved.replace(unresolved_path)


def write_summary(records: list[AppRecord], output_dir: Path, coverage: dict[str, Any]) -> None:
    counts = Counter(rec.primary_genre for rec in records)
    games_only = [r for r in records if r.app_type == "game"]
    reviewed = [r for r in records if r.review_required]
    lines = [
        "# 游戏分类汇总",
        "",
        f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 总记录数：{len(records)}",
        f"- 普通游戏记录：{len(games_only)}",
        f"- 待人工复核：{len(reviewed)}",
        f"- 分类覆盖率（全部记录）：{coverage.get('classified_ratio', 0):.1%}",
        f"- 分类覆盖率（仅普通游戏）：{coverage.get('game_classified_ratio', 0):.1%}",
        f"- 未确认类型（待确认）：{counts.get('待确认', 0)}",
        "",
        "## 主分类数量",
        "",
        "| 主分类 | 数量 |",
        "| --- | ---: |",
    ]
    for category in classify_mod.CATEGORIES:
        if counts.get(category):
            lines.append(f"| {category} | {counts[category]} |")
    lines += [
        "",
        "## 分类规则",
        "",
        "1. **先分应用类型**：按 Steam 商店 `type` 区分 game / dlc / demo / tool / music / video；",
        "   再把商店标成 game 但实为桌面软件的条目（实用工具 / 视频制作 / 动画建模 / 性能测试 / 启动器）",
        "   归为「工具」，把 Playtest / 测试版归为「测试版」，两者都不进普通游戏收藏夹。",
        "2. **两层分类**：第一层用 Steam 官方 genres 定「家族」（动作冒险 / 角色扮演 / 策略 / 模拟 / 休闲 / MMO …），",
        "   第二层只在该家族允许的细分类型内，用商店公开社区标签加权投票（核心机制标签权重 3–4，",
        "   情绪/题材标签权重 1–2，并按标签在页面的排名位置衰减）。",
        "3. **跨家族覆盖**需决定性证据：跨家族得分 ≥3 且高于家族内最高分才允许改写，",
        "   因此单条「恐怖」不会把 RPG 拉进恐怖类，单条「第一人称射击」也不会把成人视觉小说拉进 FPS。",
        "4. 射击类（FPS/TPS）必须同时有 Steam「动作」类型背书；无 genres 时只有 ≥3 分的决定性标签才能定类。",
        "5. 主分类之外保留玩法/模式标签作为副标签（单人、多人、合作、开放世界、剧情丰富、VR…），允许交叉类型。",
        "6. 无任何可用证据 → 「待确认」，置信度 0，进入人工复核；有家族无细分 → 家族默认类，置信度 0.55–0.6。",
        "7. 发行商、开发商、系列名不作为类型依据，仅作描述性元数据。",
        "8. 置信度 = 0.60 + 0.05 × 加权得分（上限 0.92）；得分 <3 时标记人工复核。",
        "",
        "## 未分类与待确认",
        "",
    ]
    pending = [r for r in records if r.primary_genre in ("待确认", "其他")]
    if not pending:
        lines.append("无。")
    else:
        lines += ["| AppID | 名称 | 主分类 | 原因 |", "| --- | --- | --- | --- |"]
        for rec in pending:
            lines.append(f"| {rec.appid} | {rec.name} | {rec.primary_genre} | {rec.evidence} |")
    (output_dir / "classification_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_collection_plan(
    records: list[AppRecord],
    output_dir: Path,
    min_size: int = 1,
    visible_appids: set[int] | None = None,
    axis: str = "genre",
    retired: set[str] | None = None,
) -> dict[str, Any]:
    """Build the collection plan. Every collection name is a real genre or a
    real app type — never a catch-all and never an ownership stand-in.

    ``axis`` selects what the collections group by. The default, ``genre``, is
    the only axis with per-app evidence; the others are explicit user choices
    (see ``grouping.AXES``) and label their buckets as the fact they describe.

    Rules that shape the output:

      * ``min_size`` defaults to 1, so a one-game category still gets its own
        collection instead of being poured into「小类合并」.
      * A genre collection states genre only. An app the client renders joins
        its real genre whether or not the licence is confirmed; ownership doubt
        travels as the 归属待确认 / 历史记录 secondary tag written in
        ``build_records``, and ``ownership_status`` stays authoritative in the
        inventory and unresolved_games.csv.
      * Apps the client does not render and that are not confirmed playable are
        left out: no genre claim, no noise.
      * Non-game rows (DLC / 试玩版 / 模组 / 工具 / 配置) get their own app-type
        collections, so they never pollute a genre.
      * 测试版 / Playtest is a build state, not a genre: it keeps its inherited
        genre and carries 「测试版」 as a secondary tag.

    ``visible_appids`` is the primary account's ``librarycache`` set. When it is
    None the plan is built as if everything were visible (offline / tests).

    ``retired`` holds collection names the user deleted by hand. They are not
    recreated: declining a bucket is the user's call, not drift to repair.

    Names are the plain category name (no ``类型·`` prefix) so the client list
    reads cleanly.
    """
    retired_names = {str(n) for n in (retired or set())}
    has_api = any(r.ownership_status == "owned_verified" for r in records)
    owned_statuses = ("owned_verified",) if has_api else (
        "owned_verified", "owned_local_evidence", "likely_owned"
    )
    plan_statuses = owned_statuses + ("family_shared", "free_to_play")
    spec = grouping.resolve(axis)
    axis_label = spec["label"]
    # 测试版 / Playtest 是构建状态而非类型：只要继承了正作的类型，就按类型归夹，
    # 并保留「测试版」作为副标签，不再单独建一个 测试版与 Playtest 收藏夹。
    genre_grouped = ("game", "测试版")

    def visible(rec: AppRecord) -> bool:
        if visible_appids is None:
            return True
        try:
            return int(rec.appid) in visible_appids
        except (TypeError, ValueError):
            return False

    # Genre collections. A game joins its real genre when it is playable or
    # when the client renders it; ownership never rewrites the category name.
    games = [
        r
        for r in records
        if r.app_type in genre_grouped
        and (axis != "genre" or r.primary_genre not in ("待确认", "其他"))
        and (r.ownership_status in plan_statuses or visible(r))
    ]
    if axis != "genre":
        # A non-genre axis can group any app type honestly (a DLC has a release
        # year too), so widen to everything the client shows or we know is ours.
        games = [
            r
            for r in records
            if (r.ownership_status in plan_statuses or visible(r))
        ]

    # Games with no usable genre evidence. No collection is created for them:
    # a bucket named after a classification failure is not a genre.
    unclassified = [
        r
        for r in records
        if r.app_type in genre_grouped
        and r.primary_genre in ("待确认", "其他")
        and (r.ownership_status in plan_statuses or visible(r))
    ]
    access_only = [
        r for r in records
        if r.app_type == "game" and r.ownership_status in ("family_shared", "free_to_play")
    ]
    grouped: dict[str, list[AppRecord]] = defaultdict(list)
    for rec in games:
        grouped[grouping.bucket(axis, rec)].append(rec)

    plan: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    category_order = (
        classify_mod.CATEGORIES if axis == "genre" else grouping.ordered_labels(axis, list(grouped))
    )
    for category in category_order:
        items = grouped.get(category, [])
        if not items or category in retired_names:
            continue
        entry = {
            "name": category,
            "category": category,
            "count": len(items),
            "appids": [r.appid for r in items],
            "games": [r.name for r in items],
            "owned": sum(1 for r in items if r.ownership_status in owned_statuses),
            "shared": sum(1 for r in items if r.ownership_status == "family_shared"),
            "free": sum(1 for r in items if r.ownership_status == "free_to_play"),
        }
        if len(items) >= min_size and category not in ("待确认", "其他"):
            plan.append(entry)
        else:
            deferred.append(entry)

    # No catch-all merge bucket: with min_size=1 every category above already
    # became its own collection. `deferred` now only ever holds 待确认 / 其他,
    # which are reported separately instead of being blended into a fake genre.

    # Non-game entries may also be unconfirmed; they still need a home so the
    # client shows no leftovers. `plan_statuses` stays the honest gate for
    # *games*, while non-game buckets accept every status the client displays.
    special_statuses = plan_statuses + ("unknown", "installed_only", "not_owned_former")

    def special_rows(pred) -> list[AppRecord]:
        out = []
        for r in records:
            if not pred(r):
                continue
            if r.ownership_status in plan_statuses or (
                r.ownership_status in special_statuses and visible(r)
            ):
                out.append(r)
        return out

    special = {
        "DLC 与附加内容": [
            r
            for r in special_rows(
                lambda x: x.app_type == "DLC"
                or (
                    x.app_type == "配置"
                    and ("dlc" in x.name.lower() or "entitlement" in x.name.lower())
                )
            )
        ],
        "试玩版": [
            r for r in special_rows(lambda x: x.app_type == "试玩版")
        ],
        "模组与插件": [
            r for r in special_rows(lambda x: x.app_type == "模组")
        ],
        "工具与软件": [r for r in special_rows(lambda x: x.app_type in ("工具", "软件"))],
        # Internal Steam entries (Steam Client / Screenshots / Game Notes / a
        # Linux HL1 client …). They are real library rows but never games, so
        # they get their own bucket instead of polluting 工具与软件.
        "系统与配置": [
            r
            for r in special_rows(
                lambda x: x.app_type == "配置"
                and not ("dlc" in x.name.lower() or "entitlement" in x.name.lower())
            )
        ],
    }

    # No 待确认 / 历史记录 collections: an ownership question is not a genre, and
    # on this library every one of those apps already has a real genre. The
    # question itself stays visible via the 归属待确认 / 历史记录 secondary tags
    # and in unresolved_games.csv.
    #
    # App-type buckets only apply on the genre axis. On a non-genre axis every
    # app already has exactly one fact bucket (its playtime, its year …), and a
    # second app-type collection would put it in two places at once.
    for name, items in (special.items() if axis == "genre" else ()):
        if items and name not in retired_names:
            plan.append(
                {
                    "name": name,
                    "category": name,
                    "count": len(items),
                    "appids": [r.appid for r in items],
                    "games": [r.name for r in items],
                    "owned": sum(1 for r in items if r.ownership_status in owned_statuses),
                    "shared": sum(1 for r in items if r.ownership_status == "family_shared"),
                    "free": sum(1 for r in items if r.ownership_status == "free_to_play"),
                }
            )

    lines = [
        "# Steam 收藏夹整理计划",
        "",
        f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- 应用状态与逐夹核对结果见 `apply_report.md`（本文件只描述计划内容）。",
        f"- 分类维度：**{axis_label}**（`{axis}`）",
        f"- 统计口径：{'官方 API 核实拥有（owned_verified）' if has_api else '本地证据（未做账号级核实）'}"
        " + 家庭共享",
        f"- 纳入分类的普通游戏：{len(games)}"
        f"（本账号拥有 {sum(1 for r in games if r.ownership_status in owned_statuses)}"
        f" + 家庭共享 {sum(1 for r in games if r.ownership_status == 'family_shared')}"
        f" + 免费游戏 {sum(1 for r in games if r.ownership_status == 'free_to_play')}）",
        f"- 拟创建收藏夹：{len(plan)}",
        (
            "- 已按用户要求删除、不再重建的收藏夹："
            + "、".join(sorted(retired_names))
            if retired_names
            else "- 无用户删除的收藏夹"
        ),
        (
            f"- 未单独立夹：{len(deferred)} 个（"
            + "、".join(f"{d['category']} {d['count']}" for d in deferred)
            + "），归入对应的诚实分桶而非虚构类型"
            if deferred
            else "- 每个类别都单独成夹，无遗留"
        ),
        "",
        "## 执行前置条件",
        "",
        "1. 本文件只是预览，未对 Steam 客户端做任何写入。",
        "2. 需要你明确批准后才写入。两条路径等价：(A) 直接写客户端收藏夹存储",
        "   `userdata/<AccountID>/config/cloudstorage/cloud-storage-namespace-1.json`",
        "   （配合 `cloud-storage-namespaces.json` 的版本计数，脚本已实现校验与回滚）；",
        "   (B) 在客户端图形界面按本清单手动新建。",
        "3. 不删除、不覆盖现有收藏夹；同名收藏夹先核对后跳过或追加。",
        "4. 先创建一个测试收藏夹验证新增/显示/撤销，再分批执行。",
        "",
        "## 拟创建收藏夹",
        "",
        "| 收藏夹 | 数量 | 拥有/共享/免费 | 游戏 |",
        "| --- | ---: | --- | --- |",
    ]
    for entry in plan:
        preview = "、".join(entry["games"][:12])
        if len(entry["games"]) > 12:
            preview += f" 等 {len(entry['games'])} 款"
        mix = (
            f"{entry.get('owned', entry['count'])}"
            f"/{entry.get('shared', 0)}"
            f"/{entry.get('free', 0)}"
        )
        lines.append(f"| {entry['name']} | {entry['count']} | {mix} | {preview} |")
    if deferred:
        lines += [
            "",
            "## 未单独建夹的类别",
            "",
            "以下类别没有单独建夹，游戏也没有丢失：它们归入「待确认」等诚实分桶，"
            "在补齐类型证据后可随时移入真实类型夹。",
            "",
            "| 类别 | 数量 | 游戏 |",
            "| --- | ---: | --- |",
        ]
        for entry in deferred:
            lines.append(f"| {entry['category']} | {entry['count']} | {'、'.join(entry['games'])} |")
    lines += [
        "",
        "## 未纳入收藏夹计划",
        "",
        "- 客户端根本不会显示、且无法确认可游玩的条目（如仅存在于其他 Steam 账号）",
        "- 已安装但无法确认归属的条目（installed_only）",
        "- 没有任何可用类型证据的条目（本轮为 0）",
        "- 其他本地账号留下的条目（other_local_account）",
        "",
        "家庭共享与免费游戏已纳入上面的类型收藏夹，并在「拥有/共享/免费」列分别标注数量。",
        "",
        "拥有状态未确认（unknown）或仅有历史记录（not_owned_former）的游戏，只要客户端可见，"
        "就已经按真实类型进入上面的收藏夹；它们的归属疑问以 `归属待确认` / `历史记录` 副标签"
        "保留在游戏清单中，不另建按归属划分的收藏夹。",
        "",
    ]

    # Everything the apply step needs, per collection, so the client UI can be
    # driven without re-deriving names or re-reading the whole inventory.
    apply_plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "axis": axis,
        "axis_label": axis_label,
        "basis": (
            "owned_verified+family_shared+free_to_play" if has_api
            else "local-evidence+family_shared+free_to_play"
        ),
        "status": "not_applied",
        "collections": [
            {
                "name": entry["name"],
                "count": entry["count"],
                "appids": entry["appids"],
                "games": entry["games"],
                "owned": entry.get("owned", entry["count"]),
                "shared": entry.get("shared", 0),
                "free": entry.get("free", 0),
            }
            for entry in plan
        ],
        "deferred": [
            {"category": entry["category"], "count": entry["count"], "appids": entry["appids"]}
            for entry in deferred
        ],
        "excluded": {
            "family_shared": 0,
            "not_owned_former": sum(1 for r in records if r.ownership_status == "not_owned_former"),
            "unconfirmed": sum(1 for r in records if r.ownership_status in ("unknown", "installed_only")),
            "other_local_account": sum(1 for r in records if r.ownership_status == "other_local_account"),
        },
    }

    tmp_md = output_dir / "collection_plan.md.tmp"
    tmp_md.write_text("\n".join(lines), encoding="utf-8")
    tmp_md.replace(output_dir / "collection_plan.md")
    tmp_json = output_dir / "collection_plan.json.tmp"
    tmp_json.write_text(json.dumps(apply_plan, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_json.replace(output_dir / "collection_plan.json")
    return {"plan": plan, "deferred": deferred, "apply": apply_plan}
