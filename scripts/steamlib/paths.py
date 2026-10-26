"""Resolve the project root and the Steam root without hardcoding either.

Two overrides make the same scripts usable from a skill, a temp dir, or the
user's own project folder:

  STEAMORG_ROOT   project directory containing output/ data/ backups/
  STEAMORG_STEAM  Steam client root (skips registry discovery)

Without overrides the project root is inferred from this file's location
(``<project>/scripts/steamlib/paths.py``) and the Steam root comes from the
registry/env/common-path discovery in ``scanner``.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    override = os.environ.get("STEAMORG_ROOT")
    if override:
        path = Path(override).expanduser()
        if not path.is_dir():
            raise SystemExit(
                f"STEAMORG_ROOT 指向的目录不存在：{path}\n"
                "先用 scripts/bootstrap_project.py 创建项目目录，或修正该变量。"
            )
        return path.resolve()
    # Running from inside the project (the normal case) beats guessing from the
    # file location, because a skill copy lives outside the user's project.
    cwd = Path.cwd()
    if (cwd / "output").is_dir() and (cwd / "scripts").is_dir():
        return cwd
    raise SystemExit(
        "无法确定项目根目录。请先运行 bootstrap_project.py 创建项目，"
        "然后 cd 到该目录（或设置 STEAMORG_ROOT）。"
    )


def steam_root() -> Path:
    override = os.environ.get("STEAMORG_STEAM")
    if override:
        path = Path(override).expanduser()
        if not path.is_dir():
            raise SystemExit(f"STEAMORG_STEAM 指向的目录不存在：{path}")
        return path
    from . import scanner  # local import: scanner imports nothing from here

    found, attempts = scanner.find_steam_root()
    if not found:
        raise SystemExit(
            "未找到 Steam 安装目录。请设置环境变量 STEAMORG_STEAM 指向 Steam 根目录。"
            f"已尝试：{attempts}"
        )
    return Path(found)
