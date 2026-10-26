# Steam 收藏夹存储格式（实测）

路径：

```
<Steam>\userdata\<AccountID>\config\cloudstorage\cloud-storage-namespace-1.json
<Steam>\userdata\<AccountID>\config\cloudstorage\cloud-storage-namespaces.json
```

`<AccountID>` 是 SteamID3（32 位账号 ID），不是 SteamID64。
它等于 `userdata` 下的目录名，也等于 `loginusers.vdf` 里 `MostRecent=1` 那条记录的 32 位值。

## 结构

`cloud-storage-namespace-1.json` 是 `[[key, payload], ...]` 数组。收藏夹条目：

```json
["user-collections.uc-XXXXXXXXXXXX", {
  "key": "user-collections.uc-XXXXXXXXXXXX",
  "timestamp": 1761462000,
  "value": "{\"id\":\"uc-XXXXXXXXXXXX\",\"name\":\"动作与冒险\",\"added\":[220,320],\"removed\":[]}",
  "version": "4440",
  "conflictResolutionMethod": "custom",
  "strMethodId": "union-collections"
}]
```

- `value` 是**字符串**，里面才是 JSON。改内容要重新序列化字符串，不能直接改对象。
- `added` / `removed` 里混有字符串和整数（客户端自己写的时候不统一），比较前统一转 `int`。
- `version` 是字符串，数值上大于本文件所有其他条目即可。
- 删除收藏夹：同 key 写 `{"is_deleted": true, ...}` 墓碑，不要从数组里移除。
- `cloud-storage-namespaces.json` 形如 `[[3,"0"],[1,"4440"],[4,"0"]]`，namespace 1 的计数应 ≥ 最大 version。

## 写入顺序

1. Steam 完全退出（`steam.exe`、`steamwebhelper.exe`、`steamservice.exe` 都不在）。
2. 备份两个文件 + sha256 到项目 `backups\cloudstorage_<时间戳>\`。
3. 写 `namespace-1.json`：临时文件 → flush → `os.fsync` → `Path.replace` 原子替换。
4. 写 `namespaces.json`，把 namespace 1 抬到新的最大 version。
5. 读回校验每个 key 的 `name` 与 `added` 集合。
6. 任一步不符 → 用备份恢复两个文件并报告。

## 客户端行为（会影响校验）

- 用户在 UI 里手动改动后，客户端会重写 `namespace-1.json`，但**不保证**同步抬高
  `namespaces.json` 的计数。这不是故障，收藏夹显示正常；校验脚本已降级为提示。
- UI 显示的数量可能小于 `added` 长度（隐藏、未安装、家庭共享的过滤），一律以 `added` 为准。
- 客户端会在同目录生成 `cloud-storage-namespace-1.modified.json` 之类的 2 字节占位文件，属正常。

## 判断账号是否为主账号

`<Steam>\config\loginusers.vdf` 里每个账号节点带 `MostRecent` 与 `Timestamp`。
`MostRecent=1` 的是当前默认账号；有多个时按 `Timestamp` 取最新。
没有该标记时不要猜，直接停下来问用户要写哪个账号。
