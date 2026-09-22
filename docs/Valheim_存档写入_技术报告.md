# Valheim 1.0 存档写入可行性 · 技术报告

日期：2026-09-14
对象世界：`WORLD`（专用服务器，save number 21，83,136 个 ZDO / 7 个 chunk）

---

## 一、结论速览

| 能力 | 状态 | 说明 |
|---|---|---|
| **读** 存档 | ✅ 完全可行 | 能定位每一个物件（名称 / 坐标 / 朝向），可做平面图、统计、导出蓝图 |
| **改** 世界级数据 | ✅ 可行 | 世界规则键、boss 进度、传送门/死亡惩罚、玩家名单（fwl2 + db2 明文层） |
| **改** 单条 ZDO 记录 | ⚠️ 可写但不可靠 | 少量（1~2 条）不报错，批量后必然崩 |
| **新增** ZDO 记录 | ❌ 不可行 | chunk 的记录数 / 记录长度是硬约束，改动即加载失败 |
| **落地整栋社区蓝图** | ❌ 裸写存档走不通 | 见第四节根因 |

**推荐路径：用 PlanBuild（只需服务器 + 建造者本人客户端装，好友零安装）。**

---

## 二、破解进展（实打实的成果）

### 2.1 存档结构（Valheim 1.0 新格式）

一个世界 = 一个文件夹：

| 文件 | 格式 | 内容 |
|---|---|---|
| `_main.<N>.fwl2` | 明文 | 世界名 / 种子 / worldUID / 世界规则键 / 玩家历史 |
| `_main.<N>.db2` | **16 字节头 + gzip**（`zlib(wbits=47)` 从 offset 16） | locations（12,426）、全局键、额外数据 |
| `_main.<N>.chunks` | `29 00` + int32 **总ZDO数** + int32 条目数 + N×11 字节条目 | 索引：条目 = `[y, x, gen, saveNum(int32), zdoCount(int32)]`（**x/y 与文件名顺序相反**） |
| `<x>_<y>__<gen>_<save>.chunk` | `29 00` + int32 ZDO数 + 记录区 + **额外数据区** | ZDO 主记录 |
| `_main.<N>.ok` | 4 字节 `29 00 00 00` | 版本标记，非校验和 |

### 2.2 prefab 哈希算法（已破解）

Valheim 的 StableHash：

```python
def stable_hash(s):
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1: break
        h2 = ((h2 << 5) + h2) ^ ord(s[i+1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF
```

名字来源：`valheim_Data/resources.assets` 的引号串 + `StreamingAssets/SoftRef/manifest(_extended)` → **57,707 个名字**，实测世界物件识别覆盖率 **99.6%**。

### 2.3 ZDO 记录布局（实测交叉验证）

```
[0:12]   位置 X, Y, Z（3×float32 LE）
[12:16]  prefab 哈希（stable hash）
[16:20]  ZDOID（uint32）
[20:23]  26 5e cb —— rotation 字段标记（仅当该字段存在）
[23:27]  ★ 朝向角（float32，单位为度，可 >360 表示累积旋转）
[27:…]   字段区（按字段排列，含 a0 60 4d 1d 等字段标识）
[末尾]  记录结束标记（0x19 / 0x09 / 0x11 / 0x1b 变体）
```

记录长度可变（实测 15 ~ 190 字节），主变体为 **38 字节**（建筑件标准形态）。

**朝向字段的确认证据**：
- `wood_fence`（斜向铺的栅栏）9 条记录的该字段值各不相同（111.6° / 135.2° / 169.8° / 187.5° …）
- 她家主宅的 29 面墙该字段**全为 0°**（正交建筑）
- 程序生成的废墟石塔呈**全方向均匀分布**

### 2.4 能读出来的世界信息（已实测）

- 81,880 个 ZDO 带名称 + 坐标；其中玩家建造件 5,007 个
- 她家主宅：中心 (-305, 267)，占地 27×22m，116 件（已导出为标准 `.blueprint`）
- 基地平面图已渲染（`WORLD_基地平面图.html`）

---

## 三、写入实验证据链（20 组对照，全部在副本世界上跑）

| # | 实验 | 操作 | 结果 |
|---|---|---|---|
| 1 | CTRL | 原样副本 | ✅ 0 异常，83,136 zdos |
| 2 | W1 | 追加 **1 条** 51B 记录 + 改计数 | ✅ 加载成功 |
| 3 | CT2 | 只改计数（不加记录） | ❌ EndOfStream |
| 4 | T1 | 追加 1 条 38B 记录 | ❌ EndOfStream |
| 5 | TA/TB | 追加 1 条（不同位置/朝向） | ✅ 成功 |
| 6 | TC/D2/D3 | 追加 2~5 条 | ❌ 全部失败 |
| 7 | **E1** | **追加 2 条"原样未修改"的真实记录** | ❌ **失败** |
| 8 | E3/E4/E5 | 追加连续真实记录段（2/5/50 条） | ❌ 全部失败 |
| 9 | F1/F2/F3 | 在文件开头 / 中间 / 末尾前 64B **插入** 2 条 | ❌ 全部失败 |
| 10 | **G1** | **就地替换 2 条（等长 51B→51B）** | ✅ **成功** |
| 11 | **G3** | **就地替换 + 换成完全不同的 prefab** | ✅ **成功** |
| 12 | **G2** | **不等长替换（51B→38B，文件变短 13B）** | ❌ **失败** |
| 13 | HA~HD | 跨 chunk 改 1 条记录的位置 | ✅ 成功 |
| 14 | I1 | 单 chunk 批量替换 1,121 条 | ❌ `ArgumentOutOfRangeException: count` |
| 15 | I2/I3 | 单 chunk 批量替换 1,095 / 315 条 | ❌ EndOfStream |
| 16 | PILOT2 | 跨 6 chunk 全量替换 1,805 条（蓝图全件） | ❌ EndOfStream |

### 关键结论

1. **记录数不可变**：任何形式的新增（末尾追加 / 任意位置插入），无论内容是"我改的"还是"游戏原样的真实记录"，**≥2 条必崩**。
2. **单条长度不可变**：等长替换成功，不等长替换失败（即使只差 13 字节）。
3. **"追加 1 条成功"是容错假象**：解析越界发生在文件末尾被吞掉，那条记录并未被真正正确读取。
4. **批量改写会破坏字段语义**：`ArgumentOutOfRangeException: count` 出现在 `BinaryReader.ReadBytes(count)` —— 说明我覆盖坏了某个 **byte-array 字段的长度前缀**。

---

## 四、根本原因

Valheim 的 ZDO 是**按每个 prefab 自己的字段定义顺序**序列化的（不是通用 KV 表）。不同 prefab 的字段集不同：

```
woodwall (51B，玩家建筑)    : [pos][hash][ID][26 5e cb][rot][01 2a a8 96 a9][01 00 00 00][01 a2 1e 83 34][…][ff ff ff ff][32 19]
wood_floor_1x1 (38B)        : [pos][hash][ID][26 5e cb][rot][01 a0 60 4d 1d][01 00 00 00][12 19]
Rock_4 (39B)                : [pos][hash][?][01 04 60 79 e0][scale ×3][04 11]
Beech1 树 (39B)             : [pos][hash][?][…][04 11]
```

同时 chunk 文件里存在**记录区 + 额外数据区（末尾 ≥44 字节，含跨 chunk 重复的结构片段）**，两区边界与记录数/记录长度严格绑定。

**因此：要做到可靠写入，必须完整实现"每个 prefab 的字段编解码器" + "额外数据区的维护逻辑"。** 这已经不是"改档"，而是重写游戏的序列化层 —— 也正是为什么 PlanBuild 这类模组必须**挂在游戏进程内**（它直接调用游戏的 ZDO API 创建对象）。

---

## 五、建议路径

### 首选：PlanBuild（官方文档已核实）

- 安装范围：**服务器 + 建造者本人客户端**；**没装的好友照样能进服正常玩**（官方原文：*"It is possible for clients not using PlanBuild to connect to a server using it. Those clients won't see any planned pieces but are still able to play."*）
- 她是管理员 → 官方规则 *"Admins are always allowed"* → **可直接落地蓝图，无需材料**
- 蓝图文件已备好：`Nelesstarterbase_只要结构.blueprint`（1,805 件，已剔除 248 件家具）
- 依赖：BepInExPack 5.4.2350 + HookGenPatcher + Jötunn 2.30.0 + PlanBuild 0.18.5（均已适配 1.0）
- 注意：**crossplay 与 mod 不兼容**（她的服务器没开 crossplay ✓）

### 仍然可用的"改档"能力（只读 + 世界级）

- 导出/分析建筑、生成平面图、统计资源
- 修改世界规则（boss 进度、传送门、死亡惩罚、资源倍率）
- 改世界名 / 玩家名单
- 世界备份与快照

---

## 六、测试基础设施（保留）

- 可用副本：`<TEST_SAVE_DIR>\worlds_local\WORLD_CTRL`（0 异常）
- 测试服启动模板：
  ```
  SteamAppId=892970 ./valheim_server.exe -nographics -batchmode -name "t" -port 24XX \
      -world "WORLD_CTRL" -password <REDACTED_PASSWORD> \
      -savedir "<TEST_SAVE_DIR>" -public 0
  ```
- 全部实验日志：`Valheim_WORLD_backup_20260912\test_logs\`

**正式世界全程未被改动**（最后修改时间仍是 09-13 22:06 她自己的存档）。
