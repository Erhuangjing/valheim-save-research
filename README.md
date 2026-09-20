# Valheim Save Format Research / 英灵神殿存档格式研究

对 **Valheim 1.0**（2026-09-09 发布，Deep North）世界存档格式的逆向工程笔记与工具集。

Reverse-engineering notes and tooling for Valheim 1.0's world save format
(`.fwl2` / `.db2` / `.chunks` / `.chunk`).

> 所有结论均来自实测（20+ 组对照实验、跨多个存档代际验证），并在可能的地方与社区开源实现交叉核对。
> All findings are empirically verified and cross-checked against community open-source implementations where available.

---

## 一、1.0 存档容器格式

一个世界 = `worlds_local/<世界名>/` 一个文件夹：

| 文件 | 格式 | 内容 |
|---|---|---|
| `_main.<N>.fwl2` | 明文 | 世界名 / 种子 / worldUID / worldVersion / globalKeys |
| `_main.<N>.db2` | **16 字节头 + zlib(wbits=47)** | 全局静态结构注册表 + locations |
| `_main.<N>.chunks` | 明文索引 | `[ver u16][总ZDO数 u32][zone数 u32]` + N×11 字节条目 |
| `<x>_<y>__<gen>_<save>.chunk` | **未压缩** | 该 zone 的 ZDO 记录流 |
| `_main.<N>.ok` | 4 字节 | 写完标记，内容 = `worldVersion`（1.0 为 `29 00 00 00` = 41） |

关键机制：

- **`N` 是保存代际号，只保留最新一代** —— 每次自动保存都会把上一代**直接删除**，不留历史
- **只有"变脏"的 chunk 会被重写**，chunk 文件名尾部的 `_<save>` 是该 chunk **自己的版本号**，与世界保存代际无关（实测同目录里版本号从 1 到 72 并存）
- **地形 delta 不以 ZDO 形式存储**，而是记录流里夹杂的二进制数据块
- 传送门配对（`ConnectPortals`）**只在服务器加载世界时批量执行一次**，运行时新建的门不会自动参与

### fwl2 布局

```
uint32  外层 payload 长度        ← 老格式 .fwl 同样有这 4 字节
int32   worldVersion             ← 1.0 = 41
string  name
string  seedName                 ← 界面显示的种子字符串
int32   seed                     ← 内部数值种子
int64   uid                      （worldUID）
…       uid 之后布局与旧版不同
string… startingGlobalKeys       ← 尾部；含 boss 进度与 modifier 键
```

---

## 二、ZDO 记录编码

社区两套独立实现（C++ / Java）与本文实测一致：

```
flags    (uint16 小端)
sector   (Vector2s)
position (Vector3 = 3×float32)
prefab   (int32, StableHash)
  ├ bit0  → connectionType(byte) + connectionHash(int32)
  ├ bit1  → floats 段
  ├ bit2  → vector3s 段
  ├ bit3  → quats 段
  ├ bit4  → ints 段
  ├ bit5  → longs 段
  ├ bit6  → strings 段
  ├ bit7  → byteArrays 段
  └ bit12 → rotation
```

**每个字段段结构相同**：`[数量 N][N × (字段哈希 int32 + 值)]`

- **数量 N 是变长编码**（worldVersion ≥ 33）：1 字节，最高位为续接标志时读第二字节
- **byteArray 的值** = `int32 长度前缀 + 数据`
- **string 段** = `[count][N × (hash int32 + len u8 + utf8)]`

**StableHashCode**（prefab / 字段名 → 32 位哈希）：DJB2 双累加器变体，种子 `5381`、乘子 `1566083941`。

⚠️ **常见误判**：不同 prefab 的字段集不同，但**编码规则完全统一**——不需要为每个 prefab 写编解码器，一套通用 flags 编解码器即可。

---

## 三、写入存档

### 能做什么

| 操作 | 结论 |
|---|---|
| 读取全量 ZDO（名称/坐标/朝向） | ✅ 可靠 |
| **等长替换**单条记录 | ✅ 可行 |
| **插入新记录**（新增单个物件） | ✅ 可行（需同步三处计数） |
| 不等长替换已有记录 | ❌ 后续偏移全部错位 |
| 批量重排记录 | ❌ 必然崩 |

**插入的正确做法**（见 `tools/insert_portal.py`）：

1. **复制同类现有记录当模板**，不要自己拼字段（挑 flags 最简单的那条）
2. 改坐标：记录 +2 起的 3×f32
3. 改字符串字段时**长度字节和内容都要改**
4. 插到目标 chunk 的**记录流末尾**（尾部结构之前）
5. **同步三处计数**：chunk 头 count、`.chunks` 索引总数、该条目 zdo 数

### 不能做什么

**离线裸改存档来"落地整栋建筑"走不通**。社区共识 + 源码印证：所有世界编辑类 mod（PlanBuild 等）都必须在游戏进程内调用 `ZNetScene.instance.CreateObject(ZDOMan.instance.GetZDO(zdoid))`。

「不装 mod、用 skill 直接落地 `.blueprint` / `.vbuild` 蓝图」的系统性可行性调研（含六条路线证据矩阵、
唯一实测走通的"服务端临时执行器"链路、skill 七段流水线设计）见
`docs/Valheim_蓝图无mod落地_skill可行性调研.md`。

**唯一实测走通的路线 = 启动 dedicated server 时进程内实时构建**（处理地形 + 清障 + 建房，客户端零 mod、
落地后删净 BepInEx）。完整启动时序与全部 Harmony patch 见 `docs/Valheim_服务端实时构建机制_详解.md`，
配套参考代码骨架见 `tools/xibpbuilder_reference/`（**从实验日志重建、未编译验证**，见其 README 免责）。

---

## 四、已知坑

| 现象 | 机制 |
|---|---|
| `ArgumentOutOfRangeException: count` @ `ReadBytes(count)` | 破坏了 byteArray 的 int32 长度前缀 |
| `EndOfStreamException` @ `ZDO.Load` | 记录边界错位 |
| 追加 2 条以上记录必崩 | 尾部额外数据区被"喂饱"后失效 |
| 只改文件头计数就崩 | 头部 int32 ZDO 数是唯一解析依据 |
| zone spawn 数据溢出 | 每个 zone 的生成时间戳列表**上限 255**（1 字节计数），溢出即字节级错位且不可修复 |

---

## 五、成就锁定与「作弊标记」

Valheim 1.0 起，控制台命令（`devcommands` / `spawn` 等）会在**角色档 + 世界存档**上打标记 → 成就永久锁定。
1.0.12（2026-09-11）起官方提供洗白命令（**仅 Steam**）：游戏内 F5 → `yesiuseddevcommandsbutiwantmyachievementsanyway`（不补发已错过的成就）。

离线清除角色档标记的方法见 `tools/clear_devcommands.py`：
`.fch` 里命令/击杀统计是 `Dictionary<string,float>`，序列化为 `[count int32] + N × ([len u8][key][float32])`；
「用过控制台」的记录就是其中的 `devcommands` 条目——删掉它并把 count 减 1 即可。**同一文件里这份数据存两份**，都要改。

---

## 六、目录结构

```
docs/      研究文档（技术报告、实验记录、社区参考结论、复盘）
format/    prefab 名 ↔ 哈希对照表（57,706 条）
tools/     解析与改写脚本
tools/xibpbuilder_reference/   服务端实时构建插件的参考代码骨架（重建、未编译验证）
```

### tools 说明

| 脚本 | 用途 |
|---|---|
| `zdo_lib.py` | 核心库：chunk 记录流解析（`walk` / `candidates`）、计数同步、字节插入 |
| `zdo_v10.py` | ZDO 记录解析（1.0） |
| `chunks_index.py` | `.chunks` 索引读写 |
| `db2_probe.py` | `.db2` 解压与结构探查 |
| `insert_portal.py` | **在指定坐标插入一个传送门**（模板复制法，含完整流程） |
| `land_official.py` | 蓝图落地（密度法定 chunk + 自动锚点） |
| `anchor_diff.py` | 锚点差分定位 |
| `bp_parse.py` | **通用蓝图解析器**（`.blueprint` / `.vbuild` / zip / 市场 blob → 件清单 + 落地预检报告，含 GroundLayerPy 自动推导） |
| `bp_reconcile.py` | **落地离线对账**（期望件 vs 存档实况，hash+位置双匹配 y 量化；坑 C.2 的入库版，含 selftest） |
| `bp_pipeline.py` | **七段流水线编排器**（解析预检→备份→部署执行器→headless 盯日志→离线对账→删净 BepInEx→报告，一条命令；铁律内建，支持断点续跑与 dry-run） |
| `clear_devcommands.py` | **清除角色档的作弊标记**（含进程检查 + 备份 + dry-run） |
| `check_cheat.py` | 检查角色/世界存档里的作弊标记 |
| `check_fields.py` / `find_field_name.py` | 字段哈希反查 |
| `scan_all_portals.py` / `precise_portal.py` | 传送门扫描与字段提取 |

**使用前请先备份存档**，并确保服务器/游戏已完全退出。

---

## 七、参考：社区开源实现

| 项目 | 语言 | 说明 |
|---|---|---|
| [Avledet](https://github.com/crazicrafter1/Avledet) | C++ | Valheim 服务端重写，ZDO 打包/解包实现完整 |
| [valheim-save-tools](https://github.com/Kakoen/valheim-save-tools) | Java | 存档读写（`MAX_SUPPORTED_WORLD_VERSION = 34`，**未适配 1.0**） |
| [ZDO Hashes](https://github.com/Valheim-Modding/Wiki/wiki/ZDO-Hashes) | — | 社区维护的字段名 ↔ 哈希表 |
| [LessZdoZoneCorruption](https://github.com/ASharpPen/Valheim.LessZdoZoneCorruption) | C# | zone 数据上限（255）的一手源码 |
| [valheim-save_recovery](https://github.com/JereKuusela/valheim-save_recovery) | — | zone spawn 机制说明 |
| [PlanBuild](https://github.com/sirskunkalot/PlanBuild) | C# | 进程内落地建筑的实现参考 |

---

## 免责声明

本项目仅供**个人存档研究、数据恢复与单机/私服自用**。所有脚本都会直接改写存档文件，
**使用前务必自行备份**。作者不对因使用本工具造成的存档损坏或数据丢失负责。

本项目与 Iron Gate Studio 无关，未获其授权或认可。Valheim 是 Iron Gate Studio 的商标。
