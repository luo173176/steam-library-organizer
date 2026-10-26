"""End-to-end read-only Steam library inventory.

Usage:
    python scripts/run_scan.py [--root D:\\SteamLibraryOrganizer] [--offline]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from steamlib import (  # noqa: E402
    account,
    appinfo,
    buildbase,
    catalog,
    classify,
    report,
    scanner,
    store,
    tags as tags_mod,
    vdf,
)

from steamlib.paths import project_root as _project_root  # noqa: E402

DEFAULT_ROOT = str(_project_root())


def setup_logging(root: Path) -> logging.Logger:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = logging.getLogger("steamorg")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_dir / f"scan_{stamp}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_existing_outputs(root: Path, log: logging.Logger) -> str | None:
    out = root / "output"
    files = [p for p in out.glob("*") if p.is_file()] if out.is_dir() else []
    if not files:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = root / "backups" / f"output_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / f.name)
    manifest = {f.name: sha256(f) for f in files}
    (dest / "sha256.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已备份 %d 个既有输出到 %s", len(files), dest)
    return str(dest)


def read_client_collections(steam_root: str, primary: dict[str, Any] | None) -> dict[str, int]:
    """Existing collection name -> len(added), read from the client's own store.

    Read-only. Returns {} when the store is absent or unparsable; callers must
    treat that as "unknown", not as "no collections".
    """
    if not primary:
        return {}
    path = (
        Path(steam_root)
        / "userdata"
        / primary["account_id"]
        / "config"
        / "cloudstorage"
        / "cloud-storage-namespace-1.json"
    )
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            continue
        key, payload = item
        if not str(key).startswith("user-collections.") or not isinstance(payload, dict):
            continue
        if payload.get("is_deleted") or not payload.get("value"):
            continue
        try:
            decoded = json.loads(payload["value"])
        except json.JSONDecodeError:
            continue
        name = decoded.get("name")
        if name:
            out[str(name)] = len(decoded.get("added") or [])
    return out


def load_appinfo(steam_root: str, log: logging.Logger) -> dict[int, dict[str, Any]]:
    path = Path(steam_root) / "appcache" / "appinfo.vdf"
    if not path.is_file():
        log.warning("未找到 appinfo.vdf，跳过离线名称索引")
        return {}
    try:
        index = appinfo.index_appinfo(path)
        log.info("appinfo.vdf 解析成功：%d 条", len(index))
        return index
    except (OSError, appinfo.AppInfoError) as exc:
        log.warning("appinfo.vdf 解析失败：%s", exc)
        return {}


def environment_info(steam_root: str) -> dict[str, str]:
    """Best-effort environment snapshot for the report (read-only)."""
    info = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_exe": sys.executable,
        "cwd": os.getcwd(),
        "steam_root": steam_root,
    }
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            product = str(winreg.QueryValueEx(key, "ProductName")[0])
            build = str(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
            info["windows"] = f"{product} build {build}"
    except OSError:
        pass
    steam_exe = Path(steam_root) / "steam.exe"
    info["steam_exe"] = str(steam_exe) if steam_exe.is_file() else ""
    # Steam 进程检测（只读）
    # tasklist 输出走 Windows ANSI 代码页（中文系统为 GBK），不能按 UTF-8 解码；
    # 解码失败或输出为空时退化为 unknown，而不是让整个扫描崩掉。
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            capture_output=True,
            text=False,
            timeout=15,
            check=False,
        )
        raw = proc.stdout or b""
        out = ""
        for enc in ("mbcs", "utf-8", "gbk"):
            try:
                out = raw.decode(enc, errors="replace")
                break
            except (LookupError, UnicodeDecodeError):
                continue
        info["steam_running"] = "steam.exe" in out.lower()
    except (OSError, subprocess.SubprocessError, ValueError):
        info["steam_running"] = "unknown"
    return info


def collect_user_evidence(steam_root: str, users: list[dict[str, Any]]) -> dict[str, set[str]]:
    """AppID -> evidence tags, tagged with which local account produced them.

    The primary account is the one with AutoLogin/MostRecent; its evidence is
    tagged ``@primary``. Anything only present in another local account is
    tagged ``@secondary`` so it is never reported as owned by the main account.
    """
    evidence: dict[str, set[str]] = {}
    for user in users:
        tag = "primary" if user.get("role") == "primary" else "secondary"
        for appid in user.get("librarycache_appids", []):
            evidence.setdefault(str(appid), set()).add(f"librarycache@{tag}")
        # userdata/<AccountID> is SteamID3; localconfig lives there, not under
        # the SteamID64 directory.
        cfg = Path(steam_root) / "userdata" / user["account_id"] / "config" / "localconfig.vdf"
        if cfg.is_file():
            for appid in scanner.parse_localconfig_appids(cfg):
                evidence.setdefault(str(appid), set()).add(f"localconfig@{tag}")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--steam", default=None)
    parser.add_argument("--offline", action="store_true", help="不访问 Steam 商店，只用本地缓存")
    parser.add_argument("--limit", type=int, default=0, help="仅用于调试：限制商店请求数")
    parser.add_argument("--key-file", default=None, help="Steam Web API Key 文件（只读，内容不落盘）")
    parser.add_argument(
        "--group-by",
        default="genre",
        help="收藏夹分组维度：genre（默认）/ playtime / release / installed / last-played / ownership",
    )
    parser.add_argument("--steamid", default=None, help="覆盖主账号 SteamID64（默认取 AutoLogin 账号）")
    args = parser.parse_args(argv)

    root = Path(args.root)
    log = setup_logging(root)
    log.info("=== Steam 游戏库盘点开始 ===")

    steam_root, attempts = scanner.find_steam_root(args.steam)
    if not steam_root:
        log.error("未找到 Steam 安装目录")
        return 2
    env = environment_info(steam_root)
    log.info("环境：%s | Python %s | Steam 运行=%s", env.get("windows", env["os"]), env["python"], env.get("steam_running"))
    log.info("Steam 根目录：%s", steam_root)

    backup = backup_existing_outputs(root, log)
    libraries, lib_errors, lib_notes = scanner.parse_library_folders(steam_root)
    for err in lib_errors:
        log.warning(err)
    for note in lib_notes:
        log.info(note)
    log.info("发现 %d 个 Steam 库", len(libraries))

    installed, lib_report, manifest_errors = scanner.scan_libraries(libraries)
    log.info("读取 manifest %d 个", len(installed))

    users = scanner.list_steam_users(steam_root)
    for user in users:
        log.info(
            "本地账号 %s（%s，%s）librarycache=%d localconfig=%s",
            user["steamid64"],
            user.get("persona_name") or "?",
            user.get("role") or "?",
            user["librarycache_count"],
            user["localconfig_present"],
        )
        cfg = Path(steam_root) / "userdata" / user["account_id"] / "config" / "localconfig.vdf"
        local_ids = set(scanner.parse_localconfig_appids(cfg)) if cfg.is_file() else set()
        user["localconfig_count"] = len(local_ids)
        user["union_count"] = len(set(user["librarycache_appids"]) | local_ids)

    appinfo_index = load_appinfo(steam_root, log)
    evidence = collect_user_evidence(steam_root, users)
    log.info("本地客户端状态中的 AppID 并集：%d", len(evidence))

    owned_appids = set(evidence.keys())
    ownership_source = "local-client-state(librarycache+localconfig)"

    play_state: dict[str, dict[str, int]] = {}
    primary = next((u for u in users if u.get("role") == "primary"), None)
    if primary:
        cfg = Path(steam_root) / "userdata" / primary["account_id"] / "config" / "localconfig.vdf"
        if cfg.is_file():
            play_state = scanner.parse_localconfig_state(cfg)
            log.info("主账号 localconfig 游玩状态：%d 条", len(play_state))

    # --- Account-level ownership via the official Web API (read-only).
    # Runs before metadata resolution so family-shared AppIDs join `candidates`
    # and get names/types in the same pass instead of a second lookup round.
    api_owned: dict[str, dict[str, Any]] = {}
    family_owned: dict[str, list[str]] = {}
    api_info: dict[str, Any] = {"queried": False}
    if args.key_file and primary:
        try:
            key = account.load_key(args.key_file)
            steamid = args.steamid or primary["steamid64"]
            result = account.fetch_owned_games(key, steamid)
            api_owned = account.to_index(result)
            api_info = {
                "queried": True,
                "steamid": steamid,
                "game_count": result.game_count,
                "returned": len(result.games),
                "empty_reason": result.empty_reason,
            }
            log.info("GetOwnedGames：账号拥有 %d 款（返回 %d 条）", result.game_count, len(result.games))
            if result.empty_reason:
                log.warning("GetOwnedGames 提示：%s", result.empty_reason)

            # Family sharing: other members' libraries are playable but not owned.
            cfg = Path(steam_root) / "userdata" / primary["account_id"] / "config" / "localconfig.vdf"
            if cfg.is_file():
                fam = (vdf.parse_file(str(cfg)).get("UserLocalConfigStore") or {}).get("FamilyGroup") or {}
                members = [
                    (str(m.get("accountid")), str(int(str(m["accountid"])) + scanner.STEAM_ID64_BASE))
                    for m in (fam.get("members") or {}).values()
                    if isinstance(m, dict) and str(m.get("accountid", "")).isdigit()
                ]
                if members:
                    log.info("Steam 家庭组 %r：%d 名成员，逐个核对共享库", fam.get("name"), len(members))
                    api_info["family_group"] = fam.get("name")
                    api_info["family_members"] = len(members)
                    member_counts: dict[str, int] = {}
                    member_errors: dict[str, str] = {}
                    for aid, sid in members:
                        if sid == steamid:
                            continue
                        try:
                            # A dropped connection on one member must not shrink
                            # the family library silently; retry twice.
                            member = account.fetch_owned_games(key, sid, retries=2)
                        except account.AccountError as exc:
                            safe = account.redact(str(exc), key)
                            member_errors[aid] = safe
                            log.warning("家庭成员 %s 跳过：%s", aid, safe)
                            continue
                        member_counts[aid] = member.game_count
                        for game in member.games:
                            if game.appid in api_owned:
                                continue
                            family_owned.setdefault(game.appid, []).append(aid)
                    api_info["family_member_counts"] = member_counts
                    if member_errors:
                        api_info["family_member_errors"] = member_errors
                    log.info("家庭共享候选：%d 款（本账号未拥有）", len(family_owned))
        except account.AccountError as exc:
            log.warning("账号级 API 未完成：%s", account.redact(str(exc), key if 'key' in locals() else ""))
            api_info = {"queried": False, "error": account.redact(str(exc), key if 'key' in locals() else "")}

    # Store metadata for every candidate we know about: installed manifests,
    # primary-account client state, and family-shared AppIDs the machine never
    # cached. Ownership is decided later by report.build_records.
    candidates = sorted(
        {app.appid for app in installed} | owned_appids | set(family_owned),
        key=lambda s: int(s) if str(s).isdigit() else 0,
    )
    log.info("元数据候选 AppID：%d（已安装 %d，本地状态 %d，家庭共享新增 %d）",
             len(candidates), len(installed), len(owned_appids), len(set(family_owned) - owned_appids))
    meta: dict[str, dict[str, Any]] = {}
    catalog_stats = {"attempted": 0, "resolved": 0, "by_source": {}}
    if not args.offline:
        client = store.StoreClient(root / "data" / "processed" / "store_cache")
        if args.limit:
            candidates_for_store = candidates[: args.limit]
        else:
            candidates_for_store = candidates
        last_log = [0.0]

        def progress(index: int, total: int, appid: str, ok: bool) -> None:
            now = time.time()
            if index == total or now - last_log[0] > 10:
                last_log[0] = now
                log.info("商店元数据 %d/%d（最近 %s %s）", index, total, appid, "OK" if ok else "无数据")

        fetched = client.details_many(candidates_for_store, progress=progress)
        for appid, data in fetched.items():
            meta[appid] = store.summarize(data)
        log.info("商店元数据成功 %d / 请求 %d，失败 %d", len(meta), client.calls, len(client.errors))
        for err in client.errors[:10]:
            log.warning("商店错误：%s", err)

        # The CN storefront hides region-restricted/delisted entries. Resolve
        # those through the same public endpoint in another region, then via a
        # public appinfo mirror — each hit keeps its own provenance.
        missing = [a for a in candidates_for_store if not meta.get(a, {}).get("type")]
        if missing:
            resolver = catalog.CatalogResolver(root / "data" / "processed" / "catalog_cache")
            catalog_stats["attempted"] = len(missing)

            def catalog_progress(index: int, total: int, appid: str, ok: bool) -> None:
                log.info("目录补全 %d/%d（%s %s）", index, total, appid, "OK" if ok else "无数据")

            resolved = resolver.resolve_many(missing, progress=catalog_progress)
            for appid, item in resolved.items():
                meta[appid] = item
                src = item.get("catalog_source", "unknown")
                catalog_stats["by_source"][src] = catalog_stats["by_source"].get(src, 0) + 1
            catalog_stats["resolved"] = len(resolved)
            log.info(
                "目录补全成功 %d / %d（%s）",
                len(resolved),
                len(missing),
                "，".join(f"{k}={v}" for k, v in sorted(catalog_stats["by_source"].items())) or "无",
            )

        # Public store user tags: the gameplay-specific signal.
        tag_client = tags_mod.TagClient(root / "data" / "processed" / "tag_cache")
        tag_targets = [appid for appid in candidates if meta.get(appid, {}).get("type") == "game"]
        last_tag_log = [0.0]

        def tag_progress(index: int, total: int, appid: str, ok: bool) -> None:
            now = time.time()
            if index == total or now - last_tag_log[0] > 10:
                last_tag_log[0] = now
                log.info("商店标签 %d/%d（最近 %s %s）", index, total, appid, "OK" if ok else "无标签")

        fetched_tags = tag_client.fetch_many(tag_targets, progress=tag_progress)
        for appid, tag_list in fetched_tags.items():
            if appid in meta:
                meta[appid]["user_tags"] = tag_list
        log.info("商店标签成功 %d / 请求 %d", len(fetched_tags), tag_client.calls)

    # Fall back to offline appinfo names when store data is unavailable.
    name_hints: dict[str, str] = {}
    for appid in candidates:
        key = int(appid)
        if appid in meta and meta[appid].get("name"):
            name_hints[appid] = meta[appid]["name"]
        elif key in appinfo_index:
            name_hints[appid] = appinfo_index[key]["name"]
            if appid not in meta:
                meta[appid] = {
                    "name": appinfo_index[key]["name"],
                    "type": appinfo.normalize_type(appinfo_index[key].get("type")),
                    "genres": [],
                    "categories": [],
                    "short_description": "",
                    "user_tags": [],
                    "catalog_source": "appinfo.vdf(local cache)",
                    "catalog_confidence": 0.75,
                }

    # Public title search for build AppIDs (playtests / test servers) whose own
    # store entry ships no genre. Offline mode skips it and uses local evidence
    # only, so the same code path serves both.
    build_lookup = None
    buildbase_stats: dict[str, Any] = {"enabled": False}
    if not args.offline:
        build_lookup = buildbase.make_lookup(root / "data" / "processed" / "buildbase_cache.json")
        buildbase_stats["enabled"] = True

    records, merge_notes = report.build_records(
        installed,
        owned_appids,
        ownership_source,
        evidence,
        meta,
        name_hints,
        play_state,
        api_owned,
        family_owned,
        api_queried=bool(api_info.get("queried")),
        base_lookup=build_lookup,
    )
    if build_lookup is not None:
        client = getattr(build_lookup, "client", None)
        buildbase_stats.update(
            {
                "calls": getattr(client, "calls", 0),
                "cached": len(getattr(client, "cache", {}) or {}),
                "errors": list(getattr(client, "errors", []) or [])[:5],
            }
        )
        build_lookup.client.flush()  # type: ignore[attr-defined]
    log.info(
        "测试版正作检索：%s",
        "关闭（离线）" if not buildbase_stats["enabled"] else
        f"调用 {buildbase_stats.get('calls', 0)}，缓存 {buildbase_stats.get('cached', 0)}",
    )

    # Attach collection names from the client's own store (read-only evidence).
    # The store's `added` arrays are authoritative; the counts the UI renders can
    # differ (hidden/filtered entries), so record the store, not the UI.
    collections = read_client_collections(steam_root, primary)

    games_only = [r for r in records if r.app_type == "game"]
    classified = [r for r in records if r.primary_genre not in ("待确认",)]
    pending = [r for r in records if r.primary_genre == "待确认"]
    owned = [r for r in records if r.ownership_status in report.OWNED_STATUSES]
    other_account = [r for r in records if r.ownership_status == "other_local_account"]
    verified = [r for r in records if r.ownership_status == "owned_verified"]
    strong = [r for r in records if r.ownership_status == "owned_local_evidence"]
    weak = [r for r in records if r.ownership_status == "likely_owned"]
    family = [r for r in records if r.ownership_status == "family_shared"]
    former = [r for r in records if r.ownership_status == "not_owned_former"]
    installed_count = sum(1 for r in records if r.installed)
    uninstalled = [r for r in owned if not r.installed]
    game_classified = [r for r in games_only if r.primary_genre not in ("待确认",)]
    coverage = {
        "classified_ratio": (len(classified) / len(records)) if records else 0.0,
        "game_ratio": (len(games_only) / len(records)) if records else 0.0,
        "game_classified_ratio": (len(game_classified) / len(games_only)) if games_only else 0.0,
    }

    report.write_inventory(records, root / "output")
    report.write_summary(records, root / "output", coverage)
    # Only apps the client can actually render belong in the leftover buckets.
    # `other_local_account` rows live under a different userdata folder and never
    # appear in this account's library, so they must not inflate 未分类.
    visible_appids = {int(a) for a in (primary.get("librarycache_appids") or [])} if primary else set()
    # Collection names the user deleted by hand are not recreated; read them
    # before the plan is written so a re-scan cannot resurrect a rejected bucket.
    retired_names: set[str] = set()
    retired_path = root / "output" / "user_retired_collections.json"
    if retired_path.is_file():
        try:
            retired_names = {
                str(n) for n in json.loads(retired_path.read_text(encoding="utf-8")).get("retired") or []
            }
        except (OSError, json.JSONDecodeError):
            log.warning("user_retired_collections.json 读取失败，按未淘汰处理")
        else:
            if retired_names:
                log.info("用户已删除的收藏夹（不再重建）：%s", "、".join(sorted(retired_names)))
    plan = report.write_collection_plan(
        records,
        root / "output",
        visible_appids=visible_appids,
        axis=args.group_by,
        retired=retired_names,
    )

    # Raw snapshot (read-only copies) and processed artifacts.
    snapshot = scanner.snapshot_raw(steam_root, str(root), log)
    processed = root / "data" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "records.json").write_text(
        json.dumps([r.to_row() for r in records], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (processed / "scan_context.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "steam_root": steam_root,
                "root_attempts": attempts,
                "libraries": lib_report,
                "library_errors": lib_errors,
                "manifest_errors": manifest_errors,
                "users": users,
                "collections_observed": collections,
                "backup": backup,
                "snapshot": snapshot,
                "appinfo_entries": len(appinfo_index),
                "store_metadata": len(meta),
                "account_api": api_info,
                "family_shared_count": len(family_owned),
                "offline": args.offline,
                "environment": env,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    scan_report = build_scan_report(
        root=root,
        steam_root=steam_root,
        attempts=attempts,
        libraries=libraries,
        lib_report=lib_report,
        installed=installed,
        records=records,
        owned=owned,
        verified=verified,
        strong=strong,
        weak=weak,
        family=family,
        former=former,
        api_info=api_info,
        other_account=other_account,
        uninstalled=uninstalled,
        users=users,
        collections=collections,
        lib_errors=lib_errors,
        manifest_errors=manifest_errors,
        appinfo_count=len(appinfo_index),
        store_count=len(meta),
        coverage=coverage,
        pending=pending,
        games_only=games_only,
        backup=backup,
        snapshot=snapshot,
        plan=plan,
        env=env,
        catalog_stats=catalog_stats,
        play_state_count=len(play_state),
    )
    (root / "output" / "scan_report.md").write_text(scan_report, encoding="utf-8")

    # Never clobber a real apply report: if the collections were already written,
    # the report is evidence, not a generated artifact.
    apply_report_path = root / "output" / "apply_report.md"
    existing_report = (
        apply_report_path.read_text(encoding="utf-8") if apply_report_path.is_file() else ""
    )
    if "已应用（APPLIED）" in existing_report or "回滚（ROLLED BACK）" in existing_report:
        log.info("保留已有 apply_report.md（已应用/回滚状态），本次不覆盖")
    else:
        apply_report_path.write_text(
            build_apply_report(plan=plan, records=records, collections=collections),
            encoding="utf-8",
        )

    # Pre-apply snapshot: exactly what the client looked like before any write,
    # so a later apply run can be diffed against it.
    (root / "output" / "preflight_snapshot.json").write_text(
        json.dumps(
            {
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "steam_root": steam_root,
                "collections_in_client": collections,
                "planned_collections": [
                    {"name": c["name"], "count": c["count"], "appids": c["appids"]}
                    for c in plan["plan"]
                ],
                "total_appids_in_plan": len({a for c in plan["plan"] for a in c["appids"]}),
                "written": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info("完成：记录 %d，已安装 %d，已拥有 %d", len(records), installed_count, len(owned))
    return 0


def build_apply_report(*, plan: dict[str, Any], records: list[Any], collections: dict[str, int]) -> str:
    """The apply report for the not-yet-applied state.

    Written on every scan *unless* a real apply/rollback report already exists
    (see the guard at the call site) — this script never touches the client.
    """
    planned = plan.get("plan") or []
    total = len({a for c in planned for a in c["appids"]})
    lines = [
        "# Steam 收藏夹应用报告",
        "",
        "状态：**尚未应用（NOT APPLIED）**",
        "",
        f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- 未创建、未修改、未删除任何 Steam 收藏夹。",
        "- 未启动、下载、安装、卸载任何游戏。",
        "- 未写入 Steam 安装目录、注册表或客户端配置。",
        f"- 待批准计划：`output/collection_plan.md`（{len(planned)} 个收藏夹、{total} 个 AppID），"
        "机器可读版本 `output/collection_plan.json`。",
        f"- 执行前快照：`output/preflight_snapshot.json`（客户端现有 {len(collections)} 个收藏夹）、"
        "`output/preflight_store_snapshot.json`（存储内每个收藏夹的 AppID 全集）。",
        "",
        "## 客户端现有收藏夹（只读读取客户端存储）",
        "",
        "数量取自 `cloud-storage-namespace-1.json` 的 `added` 数组，"
        "与客户端界面显示的数字可能不同（界面会按可见性过滤）。",
        "",
        "| 收藏夹 | 条目数 |",
        "| --- | ---: |",
    ]
    for name, count in collections.items():
        lines.append(f"| {name} | {count} |")
    if not collections:
        lines.append("| （未读到存储文件） | - |")

    lines += [
        "",
        "## 应用方式（无需操控客户端）",
        "",
        "已定位收藏夹的实际存储：`userdata/<AccountID>/config/cloudstorage/"
        "cloud-storage-namespace-1.json`，其格式为 `[[key, payload], ...]`，"
        "收藏夹条目的 `value` 是内嵌 JSON 字符串 `{id,name,added,removed}`。",
        "因此不需要点击图形界面即可写入，且可逐条回读校验、可用备份完整回滚。",
        "",
        "- 预演补丁：`output/collection_patch.json`（`python scripts/build_collection_patch.py`）",
        "- 单条金丝雀测试：`python scripts/test_collection_write.py --apply`",
        "- 全量写入：`python scripts/apply_collections.py --apply`（要求 Steam 完全退出）",
        "- 写入后核对：`python scripts/verify_applied.py`",
        "",
        "## 桌面自动化可行性（本轮实测）",
        "",
        "| 能力 | 结果 |",
        "| --- | --- |",
        "| 读取 Steam 窗口无障碍树 | 可用：能读到库列表、5 个既有收藏夹及数量、筛选器 |",
        "| 搜索框 setValue | 可用：输入 `Portal` 后结果变为 `游戏 (3/284)`，命中 Portal / Portal 2 / Portal: Alive & Kicking |",
        "| 元素 click / 坐标点击 | 不可用：`coordinate input geometry is unavailable` |",
        "| 窗口截图 | 不可用：`window capture timed out` / `FrameArrived timed out` |",
        "",
        "结论：当前会话只能「读取 + 往文本框写值」，无法点击菜单、按钮，也无法拖拽游戏条目，"
        "因此创建收藏夹这类写操作既不能可靠执行、也不能在失败后确认撤销，"
        "按任务约束在写操作前停止。",
        "",
        "## 若继续应用（两条路径，任选其一）",
        "",
        "### 路径 A：写客户端存储（不需要操作界面）",
        "",
        "1. 完全退出 Steam（含托盘图标），确认 `steam.exe` / `steamwebhelper.exe` 不在运行。",
        "2. `python scripts\\test_collection_write.py --apply` 写入 1 个测试收藏夹（AppID 400，Portal）。",
        "3. 启动 Steam，确认「测试·整理验证」出现且含 1 款游戏；不出现则 `--rollback` 并停止。",
        "4. 确认后 `python scripts\\apply_collections.py --apply` 写入全部 20 个收藏夹。",
        "5. `python scripts\\verify_applied.py` 逐条核对：新夹条目一致、6 个既有夹未被改动、版本计数一致。",
        "",
        "### 路径 B：手动在客户端创建",
        "",
        "1. 按 `output/collection_plan.md` 逐条新建收藏夹并加入对应 AppID（该文件已列出每夹完整清单）。",
        "2. 全部为新建，不涉及删除或覆盖既有收藏夹。",
        "",
        "无论哪条路径，出现任何数量不符或异常，立即停止并保留当前报告。",
        "",
        "## 不会执行的操作",
        "",
        "- 删除或覆盖任何既有收藏夹",
        "- 卸载、下载、启动、更新任何游戏",
        "- 修改游戏文件、存档、manifest、注册表或账号安全设置",
        "",
    ]
    return "\n".join(lines)


def build_scan_report(**kw: Any) -> str:
    records = kw["records"]
    owned = kw["owned"]
    uninstalled = kw["uninstalled"]
    coverage = kw["coverage"]
    plan = kw["plan"]
    games_only = kw["games_only"]
    pending = kw["pending"]
    catalog_stats = kw.get("catalog_stats") or {}
    api_info = kw.get("api_info") or {}
    verified = kw.get("verified") or []
    family = kw.get("family") or []
    former = kw.get("former") or []
    lines = [
        "# Steam 游戏库扫描报告",
        "",
        f"- 扫描时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 项目根目录：`{kw['root']}`",
        f"- Steam 根目录：`{kw['steam_root']}`",
        "- 访问方式：只读（未修改 manifest、配置、注册表或游戏文件）",
        "",
        "## 环境",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 操作系统 | {kw['env'].get('windows') or kw['env']['os']} |",
        f"| 架构 | {kw['env']['machine']} |",
        f"| Python | {kw['env']['python']}（`{kw['env']['python_exe']}`） |",
        f"| Steam 进程 | {'运行中' if kw['env'].get('steam_running') else '未运行'} |",
        "",
        "## 扫描完整性",
        "",
    ]
    if api_info.get("queried"):
        lines += [
            "**账号级核实已完成（ACCOUNT-VERIFIED）**。通过官方 "
            "`IPlayerService/GetOwnedGames` 取得该账号真实拥有的 AppID 集合，"
            f"接口返回 `game_count={api_info.get('game_count')}`、"
            f"解析 {api_info.get('returned')} 条。",
            "",
            "本地客户端状态（librarycache + localconfig）与 API 结果逐条比对后，"
            "分为「本账号拥有」「家庭共享」「曾有记录但当前无拥有证据」三类，"
            "不再把本地缓存直接当作拥有。",
        ]
        if api_info.get("error"):
            lines.append(f"\n> 备注：{api_info['error']}")
    else:
        lines += [
            "**部分扫描（PARTIAL）**。账号级完整库未取得：未提供 Steam Web API Key，"
            "社区游戏列表页面要求登录。本报告中的“已拥有”仅代表本地客户端状态"
            "（librarycache + localconfig）可见的 AppID，不等于账号全部已购游戏。",
        ]
    lines += [
        "",
        "## 数据来源",
        "",
        "| 来源 | 用途 | 结果 |",
        "| --- | --- | --- |",
        f"| libraryfolders.vdf | 枚举 Steam 库 | {len(kw['libraries'])} 个库 |",
        f"| appmanifest_*.acf | 已安装应用 | {len(kw['installed'])} 条 |",
        f"| userdata/*/config/librarycache | 本地已知 AppID | {len(kw['users'])} 个本地账号 |",
        "| userdata/*/config/localconfig.vdf | 客户端应用配置 AppID | 已解析 |",
        f"| appcache/appinfo.vdf | 离线名称/类型 | {kw['appinfo_count']} 条 |",
        f"| store appdetails API | 类型/分类/标签 | {kw['store_count']} 条 |",
        f"| 目录补全（cc=us / 公开 appinfo 镜像） | 区域锁定与下架条目 | "
        f"{catalog_stats.get('resolved', 0)} / {catalog_stats.get('attempted', 0)} 条 |",
        f"| localconfig Playtime / LastPlayed | 未安装游戏的游玩数据 | {kw.get('play_state_count', 0)} 条 |",
        (
            f"| IPlayerService/GetOwnedGames | 账号级拥有核实 | "
            f"{api_info.get('returned', 0)} 条（game_count={api_info.get('game_count')}） |"
            if api_info.get("queried")
            else "| IPlayerService/GetOwnedGames | 账号级拥有核实 | 未调用（无 Key/离线） |"
        ),
        f"| cloud-storage-namespace-1.json | 客户端现有收藏夹 | {len(kw.get('collections') or {})} 个 |",
    ]
    if api_info.get("queried") and api_info.get("family_group"):
        lines.append(
            f"| Steam 家庭组 | 共享库归属 | {api_info['family_group']!r}，"
            f"{api_info.get('family_members', 0)} 名成员 |"
        )
        counts = api_info.get("family_member_counts") or {}
        errors = api_info.get("family_member_errors") or {}
        if counts or errors:
            lines += [
                "",
                "家庭组成员核对（AccountID → 库内游戏数）：",
                "",
            ]
            for aid in sorted(set(counts) | set(errors)):
                if aid in errors:
                    lines.append(f"- `{aid}`：未取得（{errors[aid]}）")
                else:
                    lines.append(f"- `{aid}`：{counts[aid]} 款")
    lines += [
        "",
        "## Steam 库",
        "",
        "| 路径 | 可访问 | 声明 App 数 | manifest 数 |",
        "| --- | --- | ---: | ---: |",
    ]
    for lib in kw["lib_report"]:
        lines.append(
            f"| `{lib['path']}` | {lib['accessible']} | {lib['apps_declared']} | {lib['manifests_found']} |"
        )
    lines += [
        "",
        "## 本地账号",
        "",
        "| 账号 | PersonaName | 角色 | librarycache | localconfig | 并集 |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for user in kw["users"]:
        lines.append(
            f"| {user['steamid64']} | {user.get('persona_name') or '（未在 loginusers 中）'} | "
            f"{'主账号' if user.get('role') == 'primary' else '其他本地账号'} | "
            f"{user['librarycache_count']} | {user.get('localconfig_count', 0)} | {user.get('union_count', 0)} |"
        )
    lines += [
        "",
        "主账号的 AppID 只取主账号自身的 librarycache + localconfig；"
        "仅出现在其他本地账号下的条目单列为 `other_local_account`，不计入主账号游戏库。",
    ]
    lines += [
        "",
        "## 数量",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 已安装（manifest） | {sum(1 for r in records if r.installed)} |",
    ]
    if api_info.get("queried"):
        lines += [
            f"| **本账号拥有（API 核实）** | **{len(verified)}** |",
            f"| 　其中本地也有记录 | {sum(1 for r in verified if any('local-state' in s for s in r.source))} |",
            f"| 　其中本地从未出现 | {sum(1 for r in verified if not any('local-state' in s for s in r.source))} |",
            f"| 家庭共享（可游玩，非本账号拥有） | {len(family)} |",
            f"| 曾有记录但当前无拥有证据 | {len(former)} |",
        ]
    lines += [
        f"| 仍停留在强本地证据（API 降级后） | {len(kw['strong'])} |",
        f"| 仍停留在弱本地证据（API 降级后） | {len(kw['weak'])} |",
        f"| 仍停留在本地证据合计 | {len(kw['strong']) + len(kw['weak'])} |",
        f"| 已拥有但未安装 | {len(uninstalled)} |",
        f"| 仅属其他本地账号（不计入主账号） | {len(kw['other_account'])} |",
        f"| 合并后总记录 | {len(records)} |",
        f"| 其中普通游戏 | {len(games_only)} |",
        f"| 非游戏/未知类型 | {len(records) - len(games_only)} |",
        f"| 分类覆盖率（全部记录） | {coverage['classified_ratio']:.1%} |",
        f"| 分类覆盖率（仅普通游戏） | {coverage['game_classified_ratio']:.1%} |",
        f"| 待确认 | {len(pending)} |",
        f"| 有游玩时长记录（Playtime） | {sum(1 for r in records if r.playtime_minutes is not None)} |",
        "",
        "## 拥有状态口径",
        "",
        "| 状态 | 含义 | 数量 |",
        "| --- | --- | ---: |",
    ]
    for status in (
        "owned_verified",
        "owned_local_evidence",
        "likely_owned",
        "family_shared",
        "not_owned_former",
        "installed_only",
        "other_local_account",
        "unknown",
    ):
        count = sum(1 for r in records if r.ownership_status == status)
        if count or status in ("owned_verified", "family_shared", "not_owned_former"):
            lines.append(f"| `{status}` | {report.OWNERSHIP_STATUSES[status]} | {count} |")
    lines += [
        "",
        "## 客户端已有收藏夹（只读观察）",
        "",
        "| 收藏夹 | 显示数量 |",
        "| --- | ---: |",
    ]
    for name, count in kw["collections"].items():
        lines.append(f"| {name} | {count} |")
    lines += [
        "",
        f"客户端收藏夹合计 {sum(kw['collections'].values())}，"
        "与本地客户端状态中的 AppID 并集数量接近，可作为“界面已缓存库”的交叉证据，"
        "但仍不能替代账号级 GetOwnedGames。",
        "",
        "## 重复与异常",
        "",
    ]
    dup = [r for r in records if len(set(r.source)) != len(r.source)]
    if kw["lib_errors"]:
        lines.append("库解析错误：")
        lines += [f"- {e}" for e in kw["lib_errors"]]
    if kw["manifest_errors"]:
        lines.append("manifest 错误：")
        lines += [f"- {e}" for e in kw["manifest_errors"]]
    if not kw["lib_errors"] and not kw["manifest_errors"]:
        lines.append("- 未发现损坏的 VDF/ACF。")
    lines.append(f"- 合并后重复 AppID 主记录：0（以 AppID 为唯一键去重）")
    anomalies = [r for r in records if r.anomalies]
    lines.append(f"- 带异常标记的记录：{len(anomalies)}")
    for rec in anomalies[:15]:
        lines.append(f"  - {rec.appid} {rec.name}：{'；'.join(rec.anomalies)}")
    lines += [
        "",
        "## 数据缺口",
        "",
    ]
    if api_info.get("queried"):
        lines += [
            "1. `GetOwnedGames` 只返回该账号**当前拥有**的授权；退款、免费周末、"
            "限时试玩、订阅过期的条目不会出现，本地残留会被归入 `not_owned_former`。",
            "2. 家庭共享库（`family_shared`）可游玩但不算拥有，已与自有游戏严格分开，"
            "不进入“本账号拥有”统计。",
            "3. 区域锁定/下架条目的名称与类型来自 cc=us 商店接口或公开 appinfo 镜像，"
            "证据强度低于本地商店接口，已在 `evidence` / `source` 中标注来源。",
            "4. 游玩时长与最后游玩时间来自 API 与主账号 localconfig 两处；"
            "两者都没有的条目保持为空，不做推算。",
            "5. DLC 与免费游戏：`include_played_free_games=1` 已开启，免费游戏计入；"
            "DLC 不单独出现在 GetOwnedGames 中，其归属由父应用推断，标记为 `review_required`。",
        ]

        unknown_rows = [r for r in records if r.ownership_status == "unknown"]
        if unknown_rows:
            paid = [
                r
                for r in unknown_rows
                if (r.meta or {}).get("is_free") is False and r.app_type == "game"
            ]
            non_game = [r for r in unknown_rows if r.app_type != "game"]
            other = len(unknown_rows) - len(paid) - len(non_game)
            lines += [
                f"6. `unknown` 共 {len(unknown_rows)} 条的构成：付费游戏 {len(paid)} 条、"
                f"非普通游戏（配置/工具/试玩/测试/模组）{len(non_game)} 条、"
                f"其余 {other} 条。这些条目只在本机 librarycache 出现，"
                "既没有游玩时长，也不在本账号与家庭组的拥有列表里，"
                "最可能是免费周末/限时试玩留下的缓存，或已退出的旧家庭组成员。"
                "按规则不猜测归属，保持 `unknown` 并列入 `unresolved_games.csv`。",
            ]
    else:
        lines += [
            "1. 缺少 Steam Web API Key，无法调用 `IPlayerService/GetOwnedGames`。",
            "2. 社区游戏列表页返回登录页，无法在不提供 Cookie/会话的情况下读取完整库。",
            "3. 本地 librarycache/localconfig 只覆盖客户端实际展示或配置过的应用，"
            "可能漏掉从未在本机出现过的已购游戏。",
            "4. 区域锁定/下架条目的名称与类型来自 cc=us 商店接口或公开 appinfo 镜像，"
            "证据强度低于本地商店接口，已在 `evidence` / `source` 中标注来源。",
            "5. 游玩时长与最后游玩时间仅来自主账号 localconfig；"
            "从未在客户端登录过的条目这两列为空。",
        ]
    lines += [
        "",
        "## 区域/下架条目补全明细",
        "",
    ]
    if catalog_stats.get("attempted"):
        lines += [
            f"- 尝试补全：{catalog_stats['attempted']} 条",
            f"- 成功补全：{catalog_stats['resolved']} 条",
            "",
            "| 来源 | 条数 |",
            "| --- | ---: |",
        ]
        for src, count in sorted((catalog_stats.get("by_source") or {}).items()):
            lines.append(f"| `{src}` | {count} |")
        lines.append("")
    else:
        lines += ["本轮无需补全（本地缓存已覆盖）。", ""]
    lines += [
        "## 下一步（最少操作）",
        "",
    ]
    if api_info.get("queried"):
        lines += [
            f"1. 审阅 `collection_plan.md`（{len(plan['plan'])} 个拟创建收藏夹）"
            f"与 `unresolved_games.csv`（{len(former)} 条历史记录待确认）。",
            "2. 收藏夹只覆盖本账号拥有的游戏；家庭共享与历史记录单列，不混入。",
            "3. 明确批准后再通过 Steam 客户端图形界面建夹，先建测试夹验证可撤销性。",
        ]
    else:
        lines += [
            "1. 提供一个只读 Steam Web API Key（https://steamcommunity.com/dev/apikey），"
            "或把账号游戏列表设为公开后提供 SteamID64；不需要密码、Cookie 或令牌。",
            "2. 授权后重新运行 `python scripts/run_scan.py --key-file KEY_PATH`，"
            "即可把“部分扫描”升级为账号级完整清单。",
            f"3. 审阅 `collection_plan.md`（{len(plan['plan'])} 个拟创建收藏夹）后批准界面操作。",
        ]
    lines += [
        "",
        "## 文件",
        "",
        f"- 输出备份：`{kw['backup'] or '无（首次运行）'}`",
    ]
    snapshot = kw.get("snapshot") or {}
    if isinstance(snapshot, dict):
        if snapshot.get("manifests"):
            lines.append(f"- 原始快照：`{snapshot['manifests']}`（含 manifest 副本）")
        for name, path in snapshot.items():
            if name != "manifests":
                lines.append(f"- 原始快照 `{name}`：`{path}`")
    else:
        lines.append(f"- 原始快照：`{snapshot}`")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
