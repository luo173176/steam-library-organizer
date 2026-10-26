# steam-library-organizer

一个 Codex Skill：盘点 Steam 账号里**真正拥有**的游戏（含未安装、家庭共享、免费），
按你选的维度分类，生成收藏夹计划，并在你批准后写入 Steam 客户端。

Steam 安装目录全程只读，唯一写入是收藏夹存储，且需显式 `--apply`。

## 为什么不用 UI 点击

Steam 客户端没有 UIA 支持，中文路径下窗口截图和元素点击都不稳定，且无法验证。
收藏夹本体存在一个可读可校验的 JSON 里，写入可逐条核对、可完整回滚——所以走存储层。

## 安装

```powershell
# 方式一：拷到 Codex skills 目录
git clone https://github.com/luo173176/steam-library-organizer "$env:USERPROFILE\.codex\skills\steam-library-organizer"

# 方式二：只拷 SKILL.md + scripts/ + references/ + agents/ 到 $CODEX_HOME\skills\steam-library-organizer
```

装好后直接说「整理我的 Steam 游戏库」，或显式调用 `$steam-library-organizer`。

## 用法

```powershell
python <skill>\scripts\bootstrap_project.py          # 建 D:\SteamLibraryOrganizer（不可用则 E:），打印 PROJECT_ROOT=
cd <PROJECT_ROOT>

python <skill>\scripts\preflight.py --key-file data\apikey.txt   # 环境/库/账号/Key 一次看清
python <skill>\scripts\run_scan.py --key-file data\apikey.txt --group-by genre
python <skill>\scripts\probe_plan_coverage.py        # 必须输出 覆盖完整: True
python <skill>\scripts\build_collection_patch.py     # 干跑
python <skill>\scripts\migrate_collections.py        # 干跑，看清增删
python <skill>\scripts\migrate_collections.py --apply # 需批准 + Steam 已完全退出
python <skill>\scripts\verify_applied.py
python <skill>\scripts\write_apply_report.py
```

### 分类维度

`--group-by` 支持：

| 值 | 含义 |
| --- | --- |
| `genre`（默认） | 游戏类型：动作与冒险 / RPG / JRPG / 肉鸽 / 模拟经营 … |
| `playtime` | 从未游玩 / 1 小时内 / 1–10 / 10–50 / 50–200 / 200+ / 未知 |
| `release` | 2020 后 / 2010–2019 / 2000–2009 / 2009 前 / 未知 |
| `installed` | 已安装 / 未安装 |
| `last-played` | 7 天 / 30 天 / 半年 / 一年 / 更久 / 从未 |
| `ownership` | 本账号拥有 / 家庭共享 / 免费开玩 / 待确认 / 历史记录 |

## 关于 API Key

要枚举「买了但从没装过」的游戏，只有官方 `IPlayerService/GetOwnedGames` 能做到，它需要：

1. Steam Web API Key：登录后到 <https://steamcommunity.com/dev/apikey>，域名填 `localhost`
2. 账号的「游戏详情」隐私设为公开

把 Key 存成 `<项目>\data\apikey.txt`。**不要贴进对话、不要提交进仓库**——
脚本只在内存里读它，日志与报告里会替换成 `***REDACTED***`，交付前会跑
`check_no_secrets.py` 复查。

没有 Key 也能跑，结果是「部分扫描」：只覆盖本机客户端见过的应用，
报告里会明确标为 PARTIAL 并列出缺口。

## 数据口径

| 状态 | 含义 |
| --- | --- |
| `owned_verified` | API 确认拥有 |
| `family_shared` | 家庭成员拥有，可玩不算拥有 |
| `free_to_play` | 免费开玩 |
| `not_owned_former` | 有历史记录但当前无拥有证据 |
| `unknown` | 客户端可见但归属查不到 |
| `installed_only` / `other_local_account` | 仅本机安装 / 别的账号留下 |

已安装不等于已拥有；家庭共享不等于拥有；字段缺失就留空，不猜。

## 分类规则

- 收藏夹名必须是真实类型或真实应用类别，不建 `待确认`、`小类合并`、`测试版与 Playtest` 这类伪分类桶。
- 测试版 / Playtest / Test Server 是**构建状态**，继承正作类型，`测试版` 只作副标签。
- 试玩版 / DLC / 模组 / 工具 / 系统配置单独成夹，不混进普通类型。
- 只有 1 款的类型也单独建夹，不合并小类。

## 你手动改过收藏夹之后

用户的编辑就是事实，脚本不会「修正」回去：

```powershell
python <skill>\scripts\probe_live_diff.py      # 看清改了什么
python <skill>\scripts\accept_user_edits.py    # 存为新基线 + 记录被删掉的夹名
```

之后 `verify_applied.py` 把差异当参考项；`run_scan.py` 不会重建你删掉的收藏夹；
`migrate_collections.py --apply` 会以退出码 6 拒绝覆盖，除非显式 `--force`。

## 写入协议

成对更新（先退出 Steam、先备份、原子写、读回校验、失败回滚）：

```
userdata\<AccountID>\config\cloudstorage\cloud-storage-namespace-1.json
userdata\<AccountID>\config\cloudstorage\cloud-storage-namespaces.json
```

格式细节与失败模式见 [references/store-format.md](references/store-format.md)。

## 不做什么

不启动、不下载、不安装、不卸载游戏；不改 manifest、存档、注册表、账号设置；
不删用户现有收藏夹；不把 API Key 写进任何文件。

## License

MIT
