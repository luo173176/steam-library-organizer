"""Account-level ownership via the official Steam Web API.

``IPlayerService/GetOwnedGames`` is the only supported way to enumerate what
an account actually owns, including games that were never installed on this
machine. It needs a Web API key and a SteamID64, and it honours the profile's
"Game details" privacy setting.

Credential handling
-------------------
The key is read at call time from a file path or an environment variable and
is never written to disk, logs, reports, CSV/JSON or command history. Every
error path is sanitised because ``urllib`` puts the full URL (which carries the
key as a query parameter) into its exception text.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENDPOINT = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
REDACTED = "***REDACTED***"


class AccountError(RuntimeError):
    """Raised with a message that is already safe to log."""


@dataclass
class OwnedGame:
    appid: str
    name: str
    playtime_forever: int
    playtime_2weeks: int | None
    rtime_last_played: int | None
    img_icon_url: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "appid": self.appid,
            "name": self.name,
            "playtime_forever": self.playtime_forever,
            "playtime_2weeks": self.playtime_2weeks,
            "rtime_last_played": self.rtime_last_played,
            "img_icon_url": self.img_icon_url,
        }


@dataclass
class OwnedGamesResult:
    steamid: str
    game_count: int
    games: list[OwnedGame]
    http_status: int
    empty_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.empty_reason


def redact(text: str, key: str) -> str:
    """Strip a credential from any string before it can reach a log."""
    if key and key in text:
        return text.replace(key, REDACTED)
    return text


def load_key(key_file: str | Path | None = None, env_var: str = "STEAM_API_KEY") -> str:
    """Read the key without echoing it.

    Priority: explicit file, then environment variable. A file is preferred
    because an environment variable leaks into child processes and shell
    history, while the file is only read by this process.
    """
    if key_file:
        path = Path(key_file)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise AccountError(f"读取 Key 文件失败：{type(exc).__name__}") from None
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
        raise AccountError("Key 文件里没有可用的内容")
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    raise AccountError(f"未提供 API Key：请使用 --key-file，或设置 {env_var}")


def _http_message(code: int) -> str:
    return {
        400: "请求参数有误（HTTP 400）",
        401: "API Key 无效、已失效或与 SteamID 不匹配（HTTP 401）",
        403: "访问被拒绝（HTTP 403）：Key 权限或账号可见性限制",
        404: "接口不存在（HTTP 404）",
        429: "请求过于频繁（HTTP 429），请稍后重试",
        500: "Steam 服务端错误（HTTP 500）",
        503: "Steam 服务暂不可用（HTTP 503）",
    }.get(code, f"HTTP {code}")


def fetch_owned_games(
    key: str,
    steamid: str,
    *,
    timeout: int = 30,
    include_free: bool = True,
    include_appinfo: bool = True,
    retries: int = 0,
    backoff: float = 1.5,
) -> OwnedGamesResult:
    """Call GetOwnedGames and normalise the response.

    A private "Game details" setting makes Steam return an empty ``response``
    object, which is *not* the same as owning zero games. That case raises a
    clear ``AccountError`` instead of being reported as an empty library.
    """
    params = {
        "key": key,
        "steamid": steamid,
        "include_appinfo": 1 if include_appinfo else 0,
        "include_played_free_games": 1 if include_free else 0,
        "format": "json",
    }
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SteamLibraryOrganizer/1.0 (read-only inventory)",
            "Accept": "application/json",
        },
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read()
            break
        except urllib.error.HTTPError as exc:
            # Retry only transient server-side failures; a 401/403 will not fix
            # itself and must not burn the rate limit.
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                time.sleep(backoff * attempt)
                continue
            # Do not chain: the original exception carries the key-bearing URL.
            raise AccountError(_http_message(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                attempt += 1
                time.sleep(backoff * attempt)
                continue
            raise AccountError(f"网络错误：{type(exc).__name__}（已重试 {attempt} 次）") from None

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise AccountError("接口返回的内容不是合法 JSON") from None

    response = payload.get("response")
    if response is None:
        raise AccountError("接口响应缺少 response 字段")
    if not response:
        raise AccountError(
            "接口返回空 response：账号的「游戏详情」隐私未设为公开，或 SteamID 与 Key 不匹配"
        )

    games_raw = response.get("games") or []
    count = response.get("game_count")
    if count is None:
        count = len(games_raw)

    games: list[OwnedGame] = []
    for item in games_raw:
        try:
            appid = int(item["appid"])
        except (KeyError, TypeError, ValueError):
            continue
        games.append(
            OwnedGame(
                appid=str(appid),
                name=str(item.get("name") or ""),
                playtime_forever=int(item.get("playtime_forever") or 0),
                playtime_2weeks=(
                    int(item["playtime_2weeks"])
                    if item.get("playtime_2weeks") is not None
                    else None
                ),
                rtime_last_played=(
                    int(item["rtime_last_played"])
                    if item.get("rtime_last_played") is not None
                    else None
                ),
                img_icon_url=str(item.get("img_icon_url") or ""),
            )
        )

    reason = ""
    if not games and not count:
        # A genuinely empty library is possible but rare; flag it for review
        # rather than silently reporting "0 games owned".
        reason = "接口返回 0 款游戏：确认该账号确实为空库，否则检查隐私设置"

    return OwnedGamesResult(
        steamid=str(steamid),
        game_count=int(count),
        games=games,
        http_status=status,
        empty_reason=reason,
    )


def to_index(result: OwnedGamesResult) -> dict[str, dict[str, Any]]:
    """appid -> normalised row, for merging into the inventory."""
    return {game.appid: game.to_row() for game in result.games}
