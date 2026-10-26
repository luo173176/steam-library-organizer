"""Two-tier gameplay classification from public Steam metadata.

Tier 1 (family) comes from Steam's own store genre list, which is coarse but
reliable about the broad kind of game: 角色扮演 -> RPG family, 策略 -> strategy
family, 动作/冒险 -> action-adventure family, and so on.

Tier 2 (specific category) only re-splits *inside* that family using community
tags and curated title hints. This is what stops a single mood tag such as
恐怖 or 第一人称射击 from dragging an RPG into the wrong bucket: a tag may only
refine the family that Steam already asserted.

Anything with no usable evidence stays 待确认 / 其他 and is flagged for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES = [
    "动作与冒险",
    "角色扮演 RPG",
    "JRPG",
    "CRPG",
    "魂系与高难度动作",
    "第一人称射击 FPS",
    "第三人称射击 TPS",
    "策略与战棋",
    "即时战略 RTS",
    "4X 与大战略",
    "肉鸽 Roguelike / Roguelite",
    "卡牌与牌组构筑",
    "模拟经营",
    "城市建造与工厂自动化",
    "生存与制作",
    "沙盒",
    "恐怖与生存惊悚",
    "解谜与探索",
    "平台跳跃",
    "竞速与驾驶",
    "体育",
    "格斗",
    "休闲与派对",
    "MMO 与在线多人",
    "视觉小说与互动叙事",
    "音乐与节奏",
    "教育与其他",
    "其他",
    "待确认",
]

NON_GAME_TYPES = {
    "dlc": "DLC",
    "demo": "试玩版",
    "tool": "工具",
    "software": "软件",
    "music": "原声音乐",
    "video": "视频",
    "series": "系列",
    "hardware": "硬件",
    "config": "配置",
    "beta": "测试版",
    "mod": "模组",
}

# Steam genre -> (family default, family members allowed to refine it)
FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "角色扮演": ("角色扮演 RPG", ("JRPG", "CRPG", "魂系与高难度动作", "肉鸽 Roguelike / Roguelite")),
    "动作": ("动作与冒险", ("魂系与高难度动作", "第一人称射击 FPS", "第三人称射击 TPS",
                          "格斗", "平台跳跃", "恐怖与生存惊悚", "肉鸽 Roguelike / Roguelite",
                          "角色扮演 RPG", "MMO 与在线多人", "解谜与探索")),
    "冒险": ("动作与冒险", ("恐怖与生存惊悚", "解谜与探索", "视觉小说与互动叙事",
                          "平台跳跃", "生存与制作", "休闲与派对")),
    "策略": ("策略与战棋", ("4X 与大战略", "即时战略 RTS", "卡牌与牌组构筑",
                          "城市建造与工厂自动化", "肉鸽 Roguelike / Roguelite",
                          "MMO 与在线多人", "休闲与派对")),
    "模拟": ("模拟经营", ("城市建造与工厂自动化", "生存与制作", "沙盒", "休闲与派对")),
    "独立": ("其他", ("恐怖与生存惊悚", "解谜与探索", "视觉小说与互动叙事",
                     "肉鸽 Roguelike / Roguelite", "平台跳跃", "生存与制作",
                     "模拟经营", "休闲与派对")),
    "休闲": ("休闲与派对", ("解谜与探索", "音乐与节奏", "视觉小说与互动叙事", "平台跳跃")),
    "大型多人在线": ("MMO 与在线多人", ()),
    "竞速": ("竞速与驾驶", ()),
    "体育": ("体育", ("格斗",)),
}

# Refinement votes, scoped to the families listed as their keys.
TAG_VOTES: dict[str, dict[str, int]] = {
    "角色扮演 RPG": {"角色扮演": 2, "动作角色扮演": 2},
    "MMO 与在线多人": {"大型多人在线": 3, "多人在线战术竞技": 3, "moba": 3,
                       "大型多人在线角色扮演": 3},
    "JRPG": {"日系角色扮演": 3, "jrpg": 3},
    "CRPG": {"crpg": 3, "跑团": 3, "等角视角": 1},
    # 类魂系列 is a specific mechanic tag, so it outweighs the generic
    # 角色扮演 / 动作角色扮演 tags that an action RPG also carries.
    "魂系与高难度动作": {"类魂系列": 4, "类魂": 4, "souls-like": 4, "低容错": 2},
    "肉鸽 Roguelike / Roguelite": {
        "类 rogue": 3, "轻度 rogue": 3, "动作类 rogue": 3, "roguelike": 3,
        "roguelite": 3, "永久死亡": 2,
    },
    "第一人称射击 FPS": {"第一人称射击": 3, "fps": 3, "竞技场射击": 2},
    "第三人称射击 TPS": {"第三人称射击": 3, "刷宝射击游戏": 3},
    "格斗": {"格斗": 3, "2d 格斗": 3, "3d 格斗": 3},
    "平台跳跃": {"平台游戏": 3, "2d 平台": 3, "3d 平台": 3, "精确平台": 3,
                 "类银河战士恶魔城": 3, "平台解谜": 2},
    "恐怖与生存惊悚": {"恐怖": 3, "心理恐怖": 3, "生存恐怖": 3, "惊悚": 2},
    "解谜与探索": {"解谜": 2, "指向点击": 3, "步行模拟": 3},
    "视觉小说与互动叙事": {"视觉小说": 3, "恋爱模拟": 3, "自选历险体验": 2},
    "生存与制作": {"开放世界生存制作": 3, "生存": 2, "制作": 2},
    "城市建造与工厂自动化": {"城市营造": 3, "自动化": 3, "殖民模拟": 3, "基地建设": 2},
    "4X 与大战略": {"4x": 3, "大战略": 3},
    "即时战略 RTS": {"即时战略": 3, "rts": 3},
    "卡牌与牌组构筑": {"卡牌游戏": 3, "牌组构建": 3},
    "音乐与节奏": {"节奏": 3, "音游": 3, "音乐游戏": 3},
    "沙盒": {"沙盒": 2},
    "模拟经营": {"农场模拟": 3, "生活模拟": 2, "经营": 3, "时间管理": 2},
    "休闲与派对": {"社交聚会游戏": 3, "社交聚会": 3, "派对": 2, "阖家": 2,
                   "桌上游戏": 3, "棋盘游戏": 3, "社交推理": 3, "益智问答": 3},
    "动作与冒险": {"动作冒险": 3, "砍杀": 2},
    "策略与战棋": {"回合制战斗": 2, "战术": 2, "塔防": 3, "回合制": 1},
    "竞速与驾驶": {"竞速": 3, "驾驶": 3, "赛车": 3},
    "体育": {"体育": 3, "足球": 3, "篮球": 3},
}

NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("elden ring", "魂系与高难度动作"),
    ("dark souls", "魂系与高难度动作"),
    ("sekiro", "魂系与高难度动作"),
    ("black myth", "魂系与高难度动作"),
    ("nioh", "魂系与高难度动作"),
    ("baldur's gate", "CRPG"),
    ("divinity: original sin", "CRPG"),
    ("disco elysium", "CRPG"),
    ("pillars of eternity", "CRPG"),
    ("final fantasy", "JRPG"),
    ("persona ", "JRPG"),
    ("dragon quest", "JRPG"),
    ("tales of ", "JRPG"),
    ("trails of", "JRPG"),
    ("xenoblade", "JRPG"),
    ("civilization", "4X 与大战略"),
    ("stellaris", "4X 与大战略"),
    ("crusader kings", "4X 与大战略"),
    ("hearts of iron", "4X 与大战略"),
    ("total war", "4X 与大战略"),
    ("factorio", "城市建造与工厂自动化"),
    ("satisfactory", "城市建造与工厂自动化"),
    ("cities: skylines", "城市建造与工厂自动化"),
    ("oxygen not included", "城市建造与工厂自动化"),
    ("rimworld", "城市建造与工厂自动化"),
    ("frostpunk", "城市建造与工厂自动化"),
    ("age of empires", "即时战略 RTS"),
    ("starcraft", "即时战略 RTS"),
    ("warcraft", "即时战略 RTS"),
    ("xcom", "策略与战棋"),
    ("fire emblem", "策略与战棋"),
    ("into the breach", "策略与战棋"),
    ("slay the spire", "卡牌与牌组构筑"),
    ("balatro", "卡牌与牌组构筑"),
    ("hearthstone", "卡牌与牌组构筑"),
    ("the binding of isaac", "肉鸽 Roguelike / Roguelite"),
    ("hades", "肉鸽 Roguelike / Roguelite"),
    ("dead cells", "肉鸽 Roguelike / Roguelite"),
    ("risk of rain", "肉鸽 Roguelike / Roguelite"),
    ("vampire survivors", "肉鸽 Roguelike / Roguelite"),
    ("brotato", "肉鸽 Roguelike / Roguelite"),
    ("stardew valley", "模拟经营"),
    ("the sims", "模拟经营"),
    ("planet coaster", "模拟经营"),
    ("planet zoo", "模拟经营"),
    ("dave the diver", "模拟经营"),
    ("house flipper", "模拟经营"),
    ("terraria", "生存与制作"),
    ("valheim", "生存与制作"),
    ("subnautica", "生存与制作"),
    ("palworld", "生存与制作"),
    ("project zomboid", "生存与制作"),
    ("don't starve", "生存与制作"),
    ("hollow knight", "平台跳跃"),
    ("celeste", "平台跳跃"),
    ("ori and the", "平台跳跃"),
    ("cuphead", "平台跳跃"),
    ("shovel knight", "平台跳跃"),
    ("blasphemous", "平台跳跃"),
    ("portal", "解谜与探索"),
    ("the witness", "解谜与探索"),
    ("outer wilds", "解谜与探索"),
    ("the talos principle", "解谜与探索"),
    ("baba is you", "解谜与探索"),
    ("superliminal", "解谜与探索"),
    ("resident evil", "恐怖与生存惊悚"),
    ("outlast", "恐怖与生存惊悚"),
    ("dead space", "恐怖与生存惊悚"),
    ("silent hill", "恐怖与生存惊悚"),
    ("phasmophobia", "恐怖与生存惊悚"),
    ("lethal company", "恐怖与生存惊悚"),
    ("backrooms", "恐怖与生存惊悚"),
    ("counter-strike", "第一人称射击 FPS"),
    ("metro: last light", "第一人称射击 FPS"),
    ("metro last light", "第一人称射击 FPS"),
    ("call of duty", "第一人称射击 FPS"),
    ("battlefield", "第一人称射击 FPS"),
    ("apex legends", "第一人称射击 FPS"),
    ("valorant", "第一人称射击 FPS"),
    ("overwatch", "第一人称射击 FPS"),
    ("doom", "第一人称射击 FPS"),
    ("halo", "第一人称射击 FPS"),
    ("warframe", "第三人称射击 TPS"),
    ("the division", "第三人称射击 TPS"),
    ("helldivers", "第三人称射击 TPS"),
    ("street fighter", "格斗"),
    ("tekken", "格斗"),
    ("mortal kombat", "格斗"),
    ("guilty gear", "格斗"),
    ("beat saber", "音乐与节奏"),
    ("osu!", "音乐与节奏"),
    ("muse dash", "音乐与节奏"),
    ("danganronpa", "视觉小说与互动叙事"),
    ("steins;gate", "视觉小说与互动叙事"),
    ("ace attorney", "视觉小说与互动叙事"),
    ("coffee talk", "视觉小说与互动叙事"),
    ("among us", "休闲与派对"),
    ("goose goose duck", "休闲与派对"),
    ("fall guys", "休闲与派对"),
    ("human fall flat", "休闲与派对"),
    ("it takes two", "休闲与派对"),
    ("overcooked", "休闲与派对"),
    ("pummel party", "休闲与派对"),
    ("chained together", "休闲与派对"),
)

SECONDARY_TAGS = {
    "单人": ("单人",),
    "多人": ("多人", "在线合作", "局域网合作"),
    "合作": ("合作", "在线合作", "同屏/分屏合作"),
    "本地多人": ("本地多人", "4 人本地", "同屏/分屏"),
    "开放世界": ("开放世界",),
    "剧情丰富": ("剧情丰富", "故事架构丰富"),
    "独立": ("独立",),
    "抢先体验": ("抢先体验",),
    "免费游玩": ("免费开玩",),
    "VR": ("虚拟现实",),
    "可模组化": ("可模组化",),
    "控制器": ("控制器",),
}

# Store entries that are really desktop software / benchmarks sold as "game".
# Steam types them as game because the storefront has no better bucket, but they
# must not be mixed into normal game collections.
SOFTWARE_GENRES = {
    "实用工具",
    "软件培训",
    "网络出版",
    "动画制作和建模",
    "设计和插画",
    "设计与插画",
    "照片编辑",
    "视频制作",
    "音频制作",
    "游戏开发",
}

SOFTWARE_NAME_HINTS = (
    "steamworks common redistributables",
    "steam linux runtime",
    "steamvr",
    "proton ",
    "wallpaper engine",
    "lossless scaling",
    "obs studio",
    "soundpad",
    "blender",
    "vtube studio",
    "megapicker",
    "rpg maker",
    "性能测试工具",
    "benchmark",
    "加速器",
)

PLAYTEST_PATTERNS = (
    " playtest",
    "playtest",
    " test server",
    "test server",
    "experimental server",
    "测试版",
    "测试服",
    "（测试服）",
    "(测试服)",
    "（测试）",
    "(测试)",
    " 测试",
    " beta",
    "demo",
    "试用版",
)

# Name-only hints used when the store/appinfo type is missing entirely. They are
# auxiliary evidence (task rule: titles may only support, never replace, real
# metadata), so anything matched here keeps review_required=True.
#
# English markers must be whole words: "Demon's Souls" starts with "demo" but is
# a retail game, so a bare substring test would mislabel it.
_NAME_DEMO_RE = re.compile(r"(?<![a-z])demo(?![a-z])")
_NAME_PLAYTEST_RE = re.compile(
    r"(?<![a-z])(?:playtest|test\s*server|experimental\s*server|beta)(?![a-z])"
)
_NAME_PLAYTEST_HINTS = ("测试服", "测试版", "（测试）", "(测试)")


def is_build_name(name: str) -> bool:
    """True when the title names a playtest / beta build (not a demo)."""
    low = normalize(name)
    if _NAME_PLAYTEST_RE.search(low):
        return True
    if any(h in low for h in _NAME_PLAYTEST_HINTS):
        return True
    # A trailing 「测试」 is a build marker; 「测试」 mid-sentence usually is not.
    return low.endswith("测试")

# "第一人称射击" is a well-known Steam joke tag (崩坏3, 夏日狂想曲, 东北之夏 all
# carry it). A real shooter always carries at least one more shooting signal,
# so a lone FPS tag is not allowed to define the category on its own.
SHOOT_CORROBORATING_TAGS = (
    "射击",
    "第一人称",
    "第三人称",
    "枪械",
    "射击游戏",
    "竞技场射击",
    "刷宝射击游戏",
)

# Steam genres that indicate an actual game rather than desktop software.
GAME_GENRES = {
    "动作",
    "冒险",
    "角色扮演",
    "策略",
    "模拟",
    "休闲",
    "独立",
    "竞速",
    "体育",
    "大型多人在线",
}


def normalize(text: str) -> str:
    return text.casefold().strip()


def _position_weight(index: int) -> float:
    """Steam store pages show a fixed top-20 tag list.

    The list is roughly ordered by community relevance, so a mood tag that
    only shows up at slot 18 (e.g. 心理恐怖 on Undertale) must not carry the
    same weight as the first tag. Position weighting is what keeps a single
    late "恐怖" tag from reclassifying a non-horror game.
    """
    if index < 5:
        return 1.0
    if index < 10:
        return 0.75
    if index < 15:
        return 0.5
    return 0.25


@dataclass
class Classification:
    app_type: str
    primary: str
    secondary: list[str] = field(default_factory=list)
    evidence: str = ""
    confidence: float = 0.0
    review_required: bool = True


def _family_of(genres: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """Pick the most specific Steam genre that defines a family."""
    best: tuple[int, str, tuple[str, ...]] | None = None
    # Steam's genre list is not a priority list: an action game that also ships
    # racing (GTA V) or a strategy mode (Ghostrunner 2) carries those tags as
    # extras. Ranking 竞速/策略/体育 above 动作 let a secondary mode define the
    # whole family, so only 角色扮演/模拟 (which describe the core loop) and
    # 大型多人在线 (which changes who you play with) outrank the action/adventure
    # pair. Ties fall back to Steam's own list order.
    priority = {"角色扮演": 6, "模拟": 5, "大型多人在线": 3, "动作": 2, "冒险": 2,
                "策略": 2, "竞速": 2, "体育": 2, "休闲": 1, "独立": 0}
    for g in genres:
        key = normalize(g)
        if key in FAMILIES:
            rank = priority.get(key, 0)
            if best is None or rank > best[0]:
                best = (rank, key, FAMILIES[key][1])
    if best is None:
        return None
    return FAMILIES[best[1]]


def classify_app(
    name: str,
    meta: dict[str, Any] | None,
    installed_name: str | None = None,
    is_dlc_by_parent: bool = False,
) -> Classification:
    """Classify one app, keeping build qualifiers out of the genre decision.

    A playtest / beta / test-server build is a *build*, not a genre. Its genre
    comes from the same evidence as any other game, and 测试版 survives only as
    a secondary tag so the client can still show it alongside its retail game.
    """
    cls = _classify_core(name, meta, installed_name, is_dlc_by_parent)
    if not is_build_name(name):
        return cls
    if cls.app_type not in ("game", "unknown"):
        return cls
    secondary = list(cls.secondary)
    if "测试版" not in secondary:
        secondary.append("测试版")
    if cls.app_type == "unknown" and cls.primary == "待确认":
        # Build detected, genre still unknown: report it as a playtest build
        # rather than an unidentifiable app. The merge step fills the genre in
        # from the matching retail entry.
        return Classification(
            app_type="测试版",
            primary="待确认",
            secondary=secondary,
            evidence=f"名称 {name!r} 含 Playtest/测试服 字样，但缺少商店类型元数据",
            confidence=0.5,
            review_required=True,
        )
    return Classification(
        app_type="测试版",
        primary=cls.primary,
        secondary=secondary,
        evidence=cls.evidence,
        confidence=cls.confidence,
        review_required=cls.review_required,
    )


def _classify_core(
    name: str,
    meta: dict[str, Any] | None,
    installed_name: str | None = None,
    is_dlc_by_parent: bool = False,
) -> Classification:
    meta = meta or {}
    store_type = normalize(str(meta.get("type", "") or ""))

    if store_type in NON_GAME_TYPES:
        label = NON_GAME_TYPES[store_type]
        return Classification(
            app_type=label,
            primary="其他",
            secondary=[label],
            evidence=f"Steam 商店 type={store_type} → {label}",
            confidence=0.95,
            review_required=False,
        )
    if is_dlc_by_parent and not store_type:
        return Classification(
            app_type="DLC",
            primary="其他",
            secondary=["DLC"],
            evidence="父应用为 DLC，本地无独立商店元数据",
            confidence=0.6,
            review_required=True,
        )
    # A title that names itself a playtest / test server / beta is a *build*
    # qualifier, not a genre. Keep it as a secondary tag and let the normal
    # genre pipeline decide the category, so playtests sit next to their real
    # genre instead of piling into one catch-all bucket.
    if not store_type:
        # No store/appinfo type at all. A title that names itself a playtest,
        # test server or demo is still auxiliary evidence only, so it gets a
        # type but stays flagged for review instead of silently counting as a
        # normal game. Everything else stays 待确认.
        name_lower = normalize(name)
        if _NAME_DEMO_RE.search(name_lower):
            return Classification(
                app_type="试玩版",
                primary="其他",
                secondary=["试玩版"],
                evidence=f"缺少商店类型；名称 {name!r} 含 Demo 字样（辅助证据，待人工确认）",
                confidence=0.5,
                review_required=True,
            )
        # No store metadata at all: genre cannot be derived here. The merge step
        # later inherits the genre from the matching retail entry.
        return Classification(
            app_type="unknown",
            primary="待确认",
            secondary=[],
            evidence="缺少商店类型元数据，无法确认是否为普通游戏",
            confidence=0.0,
            review_required=True,
        )
    if store_type != "game":
        return Classification(
            app_type="unknown",
            primary="待确认",
            secondary=[],
            evidence=f"未识别的商店类型 {store_type!r}",
            confidence=0.0,
            review_required=True,
        )

    genres = [str(g).strip() for g in meta.get("genres", []) if str(g).strip()]
    categories = [str(c).strip() for c in meta.get("categories", []) if str(c).strip()]
    user_tags = [str(t).strip() for t in meta.get("user_tags", []) if str(t).strip()]
    name_lower = normalize(name)

    # Software / benchmark / launcher entries masquerading as store type=game.
    # A real game genre (模拟 / 休闲 / 策略 …) means the entry is playable content;
    # only pure-utility genre sets or explicit name hints count as software.
    genre_keys = {normalize(g) for g in genres}
    has_game_genre = bool(genre_keys & {normalize(g) for g in GAME_GENRES})
    software_genre = bool(genre_keys & {normalize(g) for g in SOFTWARE_GENRES})
    software_tag = "软件" in {normalize(t) for t in user_tags[:6]}
    is_software = (
        any(normalize(h) in name_lower for h in SOFTWARE_NAME_HINTS)
        or (software_genre and not has_game_genre)
        or (software_genre and software_tag and "实用工具" in genre_keys)
    )
    if is_software:
        label = "工具"
        evidence = (
            f"商店类型 game，但 genres={genres or '无'} / 名称 {name!r} 指向桌面软件或基准测试"
        )
        if "软件" in {normalize(t) for t in user_tags}:
            evidence += "；用户标签含“软件”"
        return Classification(
            app_type=label,
            primary="其他",
            secondary=[label],
            evidence=evidence,
            confidence=0.9,
            review_required=False,
        )

    tag_set = {normalize(t) for t in user_tags + categories}
    tag_pos = {}
    for index, tag in enumerate(user_tags):
        tag_pos.setdefault(normalize(tag), index)

    def vote(category: str) -> tuple[float, list[str]]:
        # 射击类必须由 Steam 的“动作”类型背书。成人游戏常被玩家打上
        # “第一人称射击”作为玩笑标签（夏日狂想曲 / 东北之夏 / 不双修就去世），
        # 这些条目的商店类型只有冒险 / 角色扮演，不能据此归入 FPS。
        if category in ("第一人称射击 FPS", "第三人称射击 TPS") and "动作" not in genre_keys:
            return 0.0, []
        # 崩坏3 / 夏日狂想曲这类条目连“动作”类型都带，但整套标签里只有
        # 一个孤零零的“第一人称射击”。真正的射击游戏一定还会带 射击 /
        # 第一人称 / 第三人称 / 枪械 中至少一个，因此要求第二个独立信号。
        if category in ("第一人称射击 FPS", "第三人称射击 TPS"):
            if not any(normalize(t) in {normalize(x) for x in SHOOT_CORROBORATING_TAGS} for t in user_tags):
                return 0.0, []
        weighted: list[tuple[float, str]] = []
        for tag, weight in TAG_VOTES.get(category, {}).items():
            key = normalize(tag)
            if key not in tag_set:
                continue
            weighted.append((weight * _position_weight(tag_pos.get(key, 20)), tag))
        if not weighted:
            return 0.0, []
        # 同一类别里常有多个近义标签（恐怖 / 心理恐怖 / 生存恐怖）。若简单相加，
        # 一个只排在第 7 位的“恐怖”会靠数量压过排在第 1 位的核心机制标签。
        # 因此取最靠前的那条为主证据，其余按 0.3 递减计入。
        weighted.sort(reverse=True)
        score = weighted[0][0] + 0.3 * sum(w for w, _ in weighted[1:])
        hits = [t for _, t in weighted]
        return score, hits

    secondary: list[str] = []
    for label, keys in SECONDARY_TAGS.items():
        if any(normalize(k) in tag_set for k in keys):
            secondary.append(label)
    for g in genres:
        if g not in secondary and normalize(g) != "独立":
            secondary.append(g)

    family = _family_of(genres)
    name_blob = normalize(" ".join(filter(None, [name, installed_name or ""])))

    if family is None:
        # No Steam genre at all. A decisive tag vote may still classify it
        # (e.g. 永恒轮回 has no genres but carries 多人在线战术竞技), but only
        # when the score is unambiguous; otherwise stay 待确认.
        tag_cat, tag_score, tag_hits = None, 0.0, []
        for category in TAG_VOTES:
            score, hits = vote(category)
            if score > tag_score:
                tag_cat, tag_score, tag_hits = category, score, hits
        for hint, cat in NAME_HINTS:
            if normalize(hint) in name_blob:
                return Classification(
                    app_type="game",
                    primary=cat,
                    secondary=secondary,
                    evidence=f"无商店类型元数据；名称命中 {hint!r}",
                    confidence=0.5,
                    review_required=True,
                )
        if tag_cat and tag_score >= 3.0:
            return Classification(
                app_type="game",
                primary=tag_cat,
                secondary=secondary,
                evidence=f"无 Steam genres；标签={'、'.join(tag_hits)} → {tag_cat}",
                confidence=round(min(0.75, 0.5 + 0.05 * tag_score), 2),
                review_required=True,
            )
        return Classification(
            app_type="game",
            primary="待确认",
            secondary=secondary,
            evidence="商店返回 game 类型但无可用类型/标签证据",
            confidence=0.0,
            review_required=True,
        )

    family_default, allowed = family
    best_cat, best_score, best_hits = family_default, 0.0, []
    for category in allowed:
        score, hits = vote(category)
        if score > best_score:
            best_cat, best_score, best_hits = category, score, hits

    # A strong tag vote outside the family may still win when the family
    # itself produced no tag evidence (e.g. Stardew Valley lists 角色扮演 but
    # its defining tags are 农场模拟 / 生活模拟).
    cross_cat, cross_score, cross_hits = None, 0.0, []
    for category, mapping in TAG_VOTES.items():
        if category in allowed:
            continue
        score, hits = vote(category)
        if score > cross_score:
            cross_cat, cross_score, cross_hits = category, score, hits
    # Only a decisive cross-family signal may override the family result.
    if cross_cat and cross_score >= 3 and cross_score > best_score:
        best_cat, best_score, best_hits = cross_cat, cross_score, cross_hits

    evidence = f"Steam 类型={'/'.join(genres) or '无'} → 家族 {family_default}"
    confidence = 0.55 if not best_hits else 0.6
    review = True

    if best_hits and best_score >= 2.0:
        evidence += f"；标签={'、'.join(best_hits)} → {best_cat}"
        confidence = round(min(0.92, 0.60 + 0.05 * best_score), 2)
        review = best_score < 3
    else:
        best_cat, best_hits = family_default, []
        evidence += "；无细分标签命中，归入家族默认类"

    # A curated exact title can refine or correct the family guess, but only
    # when it agrees with the family or the family had no tag evidence.
    for hint, cat in NAME_HINTS:
        if normalize(hint) not in name_blob:
            continue
        if cat in allowed or not best_hits:
            if cat != best_cat:
                evidence += f"；名称命中 {hint!r} → {cat}"
                best_cat = cat
                confidence = max(confidence, 0.7)
                review = True
        break

    return Classification(
        app_type="game",
        primary=best_cat,
        secondary=secondary,
        evidence=evidence,
        confidence=round(min(confidence, 0.95), 2),
        review_required=review or confidence < 0.65,
    )
