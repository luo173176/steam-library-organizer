---
name: steam-library-organizer
description: 盘点 Steam 账号里真正拥有的游戏（含未安装、家庭共享、免费），按用户选定的维度（默认游戏类型，也可按游玩时间、发售年份、是否安装、最近游玩、归属状态）分类，并在用户批准后写入 Steam 收藏夹。用于「整理 Steam 游戏」「按类型给游戏分类建收藏夹」「我到底有哪些游戏」「Steam 未分类太多」这类请求。
metadata:
  short-description: Steam 游戏库盘点与收藏夹分类整理
---

# Steam 游戏库整理

一次跑通：**账号级拥有清单 → 分类 → 收藏夹计划 → 用户批准 → 写入 → 校验**。
Steam 目录全程只读，唯一允许的写入是用户批准后的收藏夹存储。

## 第 0 步：开工前问清三件事（只问这一轮）

**分类维度**（默认游戏类型）

| 维度 | `--group-by` | 分出来的桶 |
| --- | --- | --- |
| 游戏类型（默认） | `genre` | 动作与冒险 / RPG / JRPG / 肉鸽 / 模拟经营 … |
| 游玩时间 | `playtime` | 从未游玩 / 1 小时内 / 1–10 / 10–50 / 50–200 / 200+ / 未知 |
| 发售年份 | `release` | 2020 后 / 2010–2019 / 2000–2009 / 2009 前 / 未知 |
| 是否已安装 | `installed` | 已安装 / 未安装 |
| 最近游玩 | `last-played` | 7 天 / 30 天 / 半年 / 一年 / 更久 / 从未 |
| 归属状态 | `ownership` | 本账号拥有 / 家庭共享 / 免费开玩 / 待确认 / 历史记录 |

用户没指定就默认 `genre`，并一句话说明可以换。用户说「按游玩时长分组」「按年份」时直接换维度，
不要混着建两套收藏夹。

**是否需要账号级核实**：只有官方 `GetOwnedGames` 能枚举"买了但没装过"的游戏。
需要 → 让用户**提前把 Key 放到工作区文件**（`<项目>\data\apikey.txt`），
**绝不要在对话里粘贴、不要回显、不要写进任何输出**。
拿 Key 的步骤只说一次：登录 Steam → https://steamcommunity.com/dev/apikey → 域名填 `localhost` → 存成该文件。
用户说不弄 Key 就跳过，直接做部分扫描，并在报告里写清缺口。

**是否包含家庭库共享**：需要组成员 SteamID64（公开资料即可）。家庭共享单独标 `family_shared`，
可以进类型收藏夹，但永远不算"本账号拥有"。

## 最短路径（照抄，别自己发明步骤）

```powershell
python <skill>\scripts\bootstrap_project.py          # 输出 PROJECT_ROOT=...，只建目录
cd <PROJECT_ROOT>                                    # 之后所有命令都在这里跑

python <skill>\scripts\preflight.py --key-file data\apikey.txt   # 一次看清环境/库/账号/Key
python <skill>\scripts\run_scan.py --key-file data\apikey.txt --group-by genre
python <skill>\scripts\probe_plan_coverage.py         # 必须 覆盖完整: True
python <skill>\scripts\build_collection_patch.py      # 干跑：要新增/补哪些
python <skill>\scripts\migrate_collections.py         # 干跑，看清增删
# 用户已手动调过收藏夹（存在 output\user_edits_accepted.json）→ 到此为止，别 --apply
# 全新账号、用户已批准 → 下一步
python <skill>\scripts\migrate_collections.py --apply # 需批准 + Steam 已完全退出
python <skill>\scripts\verify_applied.py              # 逐夹核对
python <skill>\scripts\write_apply_report.py          # 落 apply_report.md
python <skill>\scripts\check_no_secrets.py data\apikey.txt
```

换维度只重跑 `run_scan.py --group-by <axis>`，**不要手改 `collection_plan.json`**。
没有 Key 就省略 `--key-file`，扫描自动降级为部分扫描。
Steam 开着也能扫描；只有 `--apply` 要求它完全退出。

`migrate_collections.py` 检测到 `user_edits_accepted.json` 且计划与现场仍有差异时会**退出码 6
并拒绝写入**（因为按计划覆盖会退回用户的改动）。这是设计行为，不是故障；只有用户明确要求
「按计划重新覆盖」时才加 `--force`。

## 数据口径（不能含糊的地方）

| 状态 | 含义 | 能否进类型收藏夹 |
| --- | --- | --- |
| `owned_verified` | `GetOwnedGames` 确认拥有 | 是 |
| `family_shared` | 家庭成员拥有，本机可玩 | 是，标共享 |
| `free_to_play` | 免费开玩 | 是，标免费 |
| `not_owned_former` | 有历史记录但当前无拥有证据 | 是，副标签 `历史记录` |
| `unknown` | 客户端可见但归属查不到 | 是，副标签 `归属待确认` |
| `installed_only` / `other_local_account` | 仅本机安装 / 别的账号留下 | 否 |

- **已安装 ≠ 已拥有**：`appmanifest_*.acf` 只说明本机装过；`librarycache` 只是缓存。
- **家庭共享 ≠ 拥有**：单独状态，不并入拥有数。
- 字段缺失就留空，**不猜** AppID、时长、日期、类型。

## 分类规则（游戏类型维度）

证据优先级：官方/公开元数据 → Steam 公开标签 → 可靠公开资料 → 名称描述（仅辅助）。
分类看**玩法**，不看发行商、系列名或题材。

- 收藏夹名就是真实类型或真实应用类别，**不建** `待确认`、`小类合并`、`测试版与 Playtest`
  这类把分类失败伪装成分类的桶。证据不足 → `primary_genre=待确认` + `unresolved_games.csv`。
- **测试版 / Playtest / Test Server 是构建状态，不是类型**：继承正作类型，`测试版` 只作副标签。
  正作不在库里时用公开 `storesearch` + `appdetails` 定位，置信度下调，`evidence` 写清来源。
- **试玩版 / DLC / 模组 / 工具 / 系统配置**单独成夹，绝不混进普通类型。
- 只有 1 款的类型也单独建夹（`min_size=1`），不合并小类。

## 用户手动改过收藏夹之后

用户自己增删游戏、改归类、删收藏夹，是**用户的决定，不是故障**。不要"修正"回去：

```powershell
python <skill>\scripts\probe_live_diff.py        # 看清改了什么
python <skill>\scripts\accept_user_edits.py      # 存为新基线 + 记录被删除的夹名
```

`accept_user_edits.py` 写两个文件：

- `output\user_edits_accepted.json`：之后 `verify_applied.py` 把差异当参考项，不算失败
- `output\user_retired_collections.json`：被删掉的收藏夹名，之后 `run_scan.py` **不会重建**

## 收藏夹写入协议（唯一允许的写入）

成对更新这两个文件：

```
userdata\<AccountID>\config\cloudstorage\cloud-storage-namespace-1.json   收藏夹本体
userdata\<AccountID>\config\cloudstorage\cloud-storage-namespaces.json    版本计数
```

`namespace-1.json` 是 `[[key, payload], ...]`，`payload.value` 是**字符串**，里面才是
`{"id","name","added","removed"}`；删除用同 key 的 `is_deleted: true` 墓碑。
写入顺序：退出 Steam → 备份两个文件 + sha256 → 临时文件 + fsync + 原子替换 →
抬 `namespaces.json` → 读回校验 → 任一步不符立刻回滚。
细节与全部失败模式见 [references/store-format.md](references/store-format.md)。

不走 UI 点击：中文路径 + 客户端无 UIA 支持时既慢又不可验证。存储层写入可逐条校验、可完整回滚。

## 常见坑（都踩过，别重踩）

| 症状 | 原因与处理 |
| --- | --- |
| `UnicodeDecodeError: 0xd0` / `'NoneType' has no attribute 'lower'` | `tasklist` 输出是本地代码页，解码失败返回 None。用 skill 自带脚本，别自己写 subprocess 探测 |
| 客户端「未分类」里还有游戏 | `write_collection_plan` 必须传 `visible_appids`（主账号 `librarycache` 下的 AppID） |
| 收藏夹数量与存储不符 | 以存储的 `added` 为准；UI 会过滤隐藏/未安装/共享条目 |
| 写完客户端又把旧状态写回 | Steam 没完全退出。`steam.exe -shutdown` 后确认无进程再写 |
| `namespaces.json` 计数落后 | 客户端自己改过存储时会这样，不影响显示，脚本已降级为提示 |
| 明细全都"缺/多"但内容相同 | 字符串与整数比较。AppID 一律先转 `int` |
| `Demon's Souls` 被当成测试版 | 用整词正则 `(?<![a-z])demo(?![a-z])`，不要裸子串匹配 |
| 重跑后用户删掉的收藏夹又回来了 | 少了 `user_retired_collections.json`；先跑 `accept_user_edits.py` |

## 交付与红线

报告数字必须来自实际输出文件，不写示例值。收尾给：项目目录、扫描完整性（完整/部分）、
拥有 / 已安装 / 未安装数、分类覆盖率与待确认数、文件清单、测试结果、未完成事项、
以及需要用户做的**最少**下一步。

不启动、不下载、不安装、不卸载游戏；不改 manifest、存档、注册表、账号设置；
不删除用户现有收藏夹；API Key 不落盘到代码/CSV/JSON/Markdown/日志/命令历史。
