"""Read-only discovery of Steam roots, libraries, manifests and user caches."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import vdf

_APPID_FILE = re.compile(r"^appmanifest_(\d+)\.acf$", re.I)

# SteamID64 = AccountID + this base (individual account base).
STEAM_ID64_BASE = 76561197960265728


def _read_registry_value(path: str, name: str) -> str | None:
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def _read_registry_value_hklm(path: str, name: str) -> str | None:
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE,):
        try:
            with winreg.OpenKey(hive, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value)
        except OSError:
            continue
    return None


@dataclass
class Library:
    path: str
    accessible: bool
    source: str
    apps_declared: dict[str, int] = field(default_factory=dict)
    manifest_count: int = 0
    error: str | None = None


@dataclass
class InstalledApp:
    appid: str
    name: str
    installdir: str
    library_path: str
    size_on_disk: int | None
    last_updated: int | None
    last_played: int | None
    state_flags: int | None
    last_owner: str | None
    manifest_path: str
    raw: dict[str, Any]
    anomalies: list[str] = field(default_factory=list)


def find_steam_root(explicit: str | None = None) -> tuple[str | None, list[dict[str, str]]]:
    """Locate the Steam client root using registry then common paths."""
    attempts: list[dict[str, str]] = []
    candidates: list[tuple[str, str]] = []
    if explicit:
        candidates.append((explicit, "argument"))

    for path, label in (
        (r"Software\Valve\Steam", "HKCU"),
        (r"Software\WOW6432Node\Valve\Steam", "HKCU-WOW64"),
    ):
        val = _read_registry_value(path, "SteamPath")
        if val:
            candidates.append((val.replace("/", "\\"), f"registry:{label}:SteamPath"))
        val = _read_registry_value(path, "SteamExe")
        if val:
            candidates.append((str(Path(val.replace("/", "\\")).parent), f"registry:{label}:SteamExe"))

    for path in (r"SOFTWARE\WOW6432Node\Valve\Steam", r"SOFTWARE\Valve\Steam"):
        val = _read_registry_value_hklm(path, "InstallPath")
        if val:
            candidates.append((val, f"registry:HKLM:{path}"))

    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            candidates.append((str(Path(base) / "Steam"), f"env:{env}"))

    for drive in ("C:", "D:", "E:", "F:"):
        for sub in ("Program Files (x86)\\Steam", "Program Files\\Steam", "Steam", "SteamLibrary"):
            candidates.append((f"{drive}\\{sub}", "common-path"))

    seen: set[str] = set()
    for raw_path, source in candidates:
        try:
            resolved = str(Path(raw_path).resolve(strict=False))
        except OSError:
            continue
        key = resolved.lower()
        if key in seen:
            continue
        seen.add(key)
        steamapps = Path(resolved) / "steamapps"
        exists = Path(resolved).is_dir()
        has_apps = steamapps.is_dir()
        attempts.append(
            {
                "path": resolved,
                "source": source,
                "exists": str(exists).lower(),
                "has_steamapps": str(has_apps).lower(),
            }
        )
        if exists and has_apps:
            return resolved, attempts
    return None, attempts


def parse_library_folders(steam_root: str) -> tuple[list[Library], list[str], list[str]]:
    """Parse every libraryfolders.vdf found under the root (config + steamapps)."""
    errors: list[str] = []
    notes: list[str] = []
    libraries: list[Library] = []
    candidates = [
        Path(steam_root) / "steamapps" / "libraryfolders.vdf",
        Path(steam_root) / "config" / "libraryfolders.vdf",
    ]
    docs: list[tuple[str, vdf.VdfObject]] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            doc = vdf.parse_file(str(candidate))
            docs.append((str(candidate), doc))
        except (OSError, vdf.VdfError) as exc:
            errors.append(f"无法解析 {candidate}: {exc}")

    seen: set[str] = set()
    for source_path, doc in docs:
        root_obj = doc.get("libraryfolders")
        if not isinstance(root_obj, dict):
            errors.append(f"{source_path} 缺少 libraryfolders 根对象")
            continue
        for key, entry in root_obj.items():
            if not isinstance(entry, dict):
                continue
            raw_path = vdf.first_value(entry, "path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                errors.append(f"{source_path} 中条目 {key} 缺少 path")
                continue
            path = raw_path.replace("\\\\", "\\")
            norm = str(Path(path))
            lowered = norm.lower()
            if lowered in seen:
                notes.append(f"重复库路径已忽略: {norm}")
                continue
            seen.add(lowered)
            apps: dict[str, int] = {}
            apps_obj = vdf.first_value(entry, "apps")
            if isinstance(apps_obj, dict):
                for appid, size in apps_obj.items():
                    try:
                        apps[str(appid)] = int(size)
                    except (TypeError, ValueError):
                        apps[str(appid)] = -1
            accessible = (Path(norm) / "steamapps").is_dir()
            libraries.append(
                Library(
                    path=norm,
                    accessible=accessible,
                    source=source_path,
                    apps_declared=apps,
                )
            )

    root_norm = str(Path(steam_root))
    if not any(lib.path.lower() == root_norm.lower() for lib in libraries):
        libraries.insert(
            0,
            Library(
                path=root_norm,
                accessible=(Path(root_norm) / "steamapps").is_dir(),
                source="steam-root-fallback",
            ),
        )
    return libraries, errors, notes


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_manifest(path: Path, library_path: str) -> tuple[InstalledApp | None, str | None]:
    try:
        doc = vdf.parse_file(str(path))
        state = vdf.flatten_app_state(doc)
    except (OSError, vdf.VdfError) as exc:
        return None, f"{path}: {exc}"

    appid = vdf.first_value(state, "appid")
    name = vdf.first_value(state, "name")
    installdir = vdf.first_value(state, "installdir")
    anomalies: list[str] = []
    if not appid:
        appid = _APPID_FILE.match(path.name).group(1)  # type: ignore[union-attr]
        anomalies.append("manifest 缺少 appid，已使用文件名")
    appid = str(appid).strip()
    if not name:
        name = f"appid_{appid}"
        anomalies.append("manifest 缺少 name")
    if not installdir:
        installdir = ""
        anomalies.append("manifest 缺少 installdir")
    size = _int_or_none(vdf.first_value(state, "SizeOnDisk"))
    if size is None:
        anomalies.append("manifest 缺少 SizeOnDisk")
    for key in ("LastUpdated", "LastPlayed", "StateFlags", "LastOwner"):
        if vdf.first_value(state, key) in (None, ""):
            anomalies.append(f"manifest 缺少 {key}")
    return (
        InstalledApp(
            appid=appid,
            name=str(name).strip(),
            installdir=str(installdir).strip(),
            library_path=library_path,
            size_on_disk=size,
            last_updated=_int_or_none(vdf.first_value(state, "LastUpdated")),
            last_played=_int_or_none(vdf.first_value(state, "LastPlayed")),
            state_flags=_int_or_none(vdf.first_value(state, "StateFlags")),
            last_owner=(
                str(vdf.first_value(state, "LastOwner"))
                if vdf.first_value(state, "LastOwner")
                else None
            ),
            manifest_path=str(path),
            raw={k: (v if not isinstance(v, dict) else "<object>") for k, v in state.items()},
            anomalies=anomalies,
        ),
        None,
    )


def scan_libraries(libraries: Iterable[Library]) -> tuple[list[InstalledApp], list[dict[str, Any]], list[str]]:
    apps: list[InstalledApp] = []
    library_report: list[dict[str, Any]] = []
    errors: list[str] = []
    for lib in libraries:
        lib_dir = Path(lib.path) / "steamapps"
        entry: dict[str, Any] = {
            "path": lib.path,
            "accessible": lib.accessible,
            "source": lib.source,
            "apps_declared": len(lib.apps_declared),
            "manifests_found": 0,
            "errors": [],
        }
        if not lib.accessible:
            entry["errors"].append("库不可访问（磁盘离线、权限不足或路径不存在）")
            library_report.append(entry)
            continue
        try:
            files = sorted(lib_dir.glob("appmanifest_*.acf"))
        except OSError as exc:
            entry["errors"].append(f"枚举失败: {exc}")
            library_report.append(entry)
            continue
        entry["manifests_found"] = len(files)
        lib.manifest_count = len(files)
        for path in files:
            if not _APPID_FILE.match(path.name):
                continue
            app, err = read_manifest(path, lib.path)
            if err:
                entry["errors"].append(err)
                errors.append(err)
            elif app:
                apps.append(app)
        library_report.append(entry)
    return apps, library_report, errors


def list_steam_users(steam_root: str) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    userdata = Path(steam_root) / "userdata"
    if not userdata.is_dir():
        return users
    login_map: dict[str, dict[str, str]] = {}
    loginusers = Path(steam_root) / "config" / "loginusers.vdf"
    if loginusers.is_file():
        try:
            doc = vdf.parse_file(str(loginusers))
            root = doc.get("users", {})
            if isinstance(root, dict):
                for sid, info in root.items():
                    if isinstance(info, dict):
                        login_map[str(sid)] = {
                            "persona_name": str(vdf.first_value(info, "PersonaName", "")),
                            "account_name": str(vdf.first_value(info, "AccountName", "")),
                            "most_recent": str(vdf.first_value(info, "MostRecent", "0")),
                            "auto_login": str(vdf.first_value(info, "AutoLogin", "0")),
                            "timestamp": str(vdf.first_value(info, "timestamp", "")),
                        }
        except (OSError, vdf.VdfError):
            pass

    for folder in sorted(userdata.iterdir()):
        if not folder.is_dir() or not folder.name.isdigit():
            continue
        # userdata 目录名是 32 位 AccountID（SteamID3），不是 SteamID64。
        # 直接把目录名当 SteamID64 会算错账号，也会导致 loginusers 姓名查不到。
        raw = int(folder.name)
        if raw > STEAM_ID64_BASE:
            sid = str(raw)
            account_id = str(raw - STEAM_ID64_BASE)
        else:
            account_id = str(raw)
            sid = str(raw + STEAM_ID64_BASE)
        info = login_map.get(sid, {})
        cache_dir = folder / "config" / "librarycache"
        cache_ids: list[str] = []
        if cache_dir.is_dir():
            cache_ids = sorted(
                (p.stem for p in cache_dir.glob("*.json") if p.stem.isdigit()),
                key=lambda s: int(s),
            )
        users.append(
            {
                "steamid64": sid,
                "account_id": account_id,
                # 由调用方在拿到全部账号后按 AutoLogin/MostRecent 判定；这里先留空。
                "role": "",
                "persona_name": info.get("persona_name", ""),
                "account_name_present": bool(info.get("account_name")),
                "most_recent": info.get("most_recent") == "1",
                "auto_login": info.get("auto_login") == "1",
                "login_timestamp": info.get("timestamp", ""),
                "localconfig_present": (folder / "config" / "localconfig.vdf").is_file(),
                "librarycache_present": cache_dir.is_dir(),
                "librarycache_appids": cache_ids,
                "librarycache_count": len(cache_ids),
            }
        )

    # 主账号判定：AutoLogin=1 优先，其次 MostRecent=1，再其次登录时间戳最新。
    def rank(u: dict[str, Any]) -> tuple[int, int, int]:
        return (
            1 if u.get("auto_login") else 0,
            1 if u.get("most_recent") else 0,
            int(u.get("login_timestamp") or 0),
        )

    if users:
        primary = max(users, key=rank)
        for u in users:
            u["role"] = "primary" if u is primary else "secondary"
    return users


def parse_localconfig_appids(path: Path) -> list[str]:
    """Extract app IDs under Software/Valve/Steam/apps from localconfig.vdf."""
    try:
        doc = vdf.parse_file(str(path))
    except (OSError, vdf.VdfError):
        return []
    node: Any = doc
    if isinstance(node, dict) and "UserLocalConfigStore" in node:
        node = node["UserLocalConfigStore"]
    for key in ("Software", "Valve", "Steam", "apps"):
        node = vdf.first_value(node, key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return []
    return sorted((k for k in node.keys() if str(k).isdigit()), key=lambda s: int(s))


def parse_localconfig_state(path: Path) -> dict[str, dict[str, int]]:
    """Per-AppID playtime state recorded by the client (read-only).

    ``Software/Valve/Steam/apps/<appid>`` carries ``LastPlayed`` (unix seconds)
    and ``Playtime`` (minutes). Both are the client's own numbers, so they are
    real evidence for games that are owned but no longer installed — the
    manifest cannot supply them. Missing keys are simply absent; nothing is
    inferred.
    """
    try:
        doc = vdf.parse_file(str(path))
    except (OSError, vdf.VdfError):
        return {}
    node: Any = doc
    if isinstance(node, dict) and "UserLocalConfigStore" in node:
        node = node["UserLocalConfigStore"]
    for key in ("Software", "Valve", "Steam", "apps"):
        node = vdf.first_value(node, key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return {}
    out: dict[str, dict[str, int]] = {}
    for appid, state in node.items():
        if not str(appid).isdigit() or not isinstance(state, dict):
            continue
        entry: dict[str, int] = {}
        last_played = _int_or_none(vdf.first_value(state, "LastPlayed"))
        playtime = _int_or_none(vdf.first_value(state, "Playtime"))
        if last_played:
            entry["last_played"] = last_played
        if playtime is not None:
            entry["playtime_minutes"] = playtime
        if entry:
            out[str(appid)] = entry
    return out


def snapshot_raw(steam_root: str, project_root: str, log) -> dict[str, str]:
    """Copy read-only source snapshots into data/raw (never modifies Steam)."""
    raw_dir = Path(project_root) / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = raw_dir / f"snapshot_{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}

    sources = [
        Path(steam_root) / "steamapps" / "libraryfolders.vdf",
        Path(steam_root) / "config" / "libraryfolders.vdf",
        Path(steam_root) / "config" / "loginusers.vdf",
    ]
    for src in sources:
        if not src.is_file():
            continue
        rel = src.relative_to(Path(steam_root))
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(src.read_bytes())
            copied[str(rel)] = str(dest)
        except OSError as exc:
            log.warning("快照复制失败 %s: %s", src, exc)

    # Manifests: keep name+content so offline libraries remain reproducible.
    manifest_dir = target / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_index: list[dict[str, Any]] = []
    for lib_path in _library_paths_from_snapshot(steam_root, log):
        lib = Path(lib_path) / "steamapps"
        if not lib.is_dir():
            manifest_index.append({"library": lib_path, "accessible": False})
            continue
        for acf in sorted(lib.glob("appmanifest_*.acf")):
            try:
                data = acf.read_bytes()
            except OSError as exc:
                manifest_index.append({"library": lib_path, "file": acf.name, "error": str(exc)})
                continue
            dest = manifest_dir / f"{lib_path.replace(':','')}_{acf.name}".replace("\\", "_")
            dest.write_bytes(data)
            manifest_index.append(
                {
                    "library": lib_path,
                    "file": acf.name,
                    "bytes": len(data),
                    "sha256": __import__("hashlib").sha256(data).hexdigest(),
                }
            )
    (target / "manifest_index.json").write_text(
        json.dumps(manifest_index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    copied["manifests"] = str(manifest_dir)
    return copied


def _library_paths_from_snapshot(steam_root: str, log) -> list[str]:
    libs, errors, _ = parse_library_folders(steam_root)
    for err in errors:
        log.warning(err)
    return [lib.path for lib in libs]
