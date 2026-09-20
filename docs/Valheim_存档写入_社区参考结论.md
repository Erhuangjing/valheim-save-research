# Valheim 存档写入 · 社区参考结论核查

日期：2026-09-14
核查对象：`Valheim_存档写入_技术报告.md`
方法：GitHub API 仓库/代码检索 + 一手源码抓取 + 社区文档 + 搜索引擎（含中英文）

---

## 零、一句话结论

**分成两层看，答案完全相反：**

| 层面 | 社区研究状况 |
|---|---|
| **1.0 新容器格式**（`fwl2` / `db2` / `.chunks` / `.chunk` 的字节布局） | ❌ **全网研究空白**。发布 5 天，没有任何公开实现或复现，你手上的很可能是目前最详细的一份记录 |
| **ZDO 记录编解码机制**（你 20 组实验背后的那套规则） | ✅ **社区研究得非常透**，有两套独立开源实现，而且你的每一条实测结论都能被逐条对上号 |

**但有一处需要修正**：报告第四节判断"必须为每个 prefab 实现字段编解码器"，这个结论**把难度估高了**。真实规则是一套统一的 `flags 位掩码 + 分段 KV 表`，社区早已公开，不需要按 prefab 逐个实现。

---

## 一、检索覆盖与结果

| 检索面 | 结果 |
|---|---|
| GitHub 仓库搜索 `valheim fwl2` / `valheim db2` | **0 结果** |
| GitHub issue 搜索（1.0 存档格式相关） | 12 条全为服务器备份/部署工具，无格式剖析 |
| 主流存档工具兼容状态 | 全部**未适配 1.0**（详见第二节） |
| 社区文档（Valheim-Modding Wiki） | 有 ZDO 哈希页面，**无容器格式文档** |
| 官方（Iron Gate） | 只有高层文字描述，**无字节规范** |
| "手改 chunk 导致载入失败"的公开复现 | **未找到**任何 1.0 相关案例 |
| ZDO 编解码的一手实现 | ✅ 找到 **2 套独立实现**（C++ / Java）+ 官方社区 wiki |

---

## 二、第一层：1.0 容器格式 = 研究空白

**主流工具状态（均为一手核实）：**

| 工具 | 状态 |
|---|---|
| `Kakoen/valheim-save-tools` | 最后提交 2024-08；源码常量 `MAX_SUPPORTED_WORLD_VERSION = 34`，而 1.0 是 **41** → **完全不支持** |
| `JereKuusela` 系（World Edit Commands 1.77.0 / Upgrade World 1.82.0） | 2026-09-13 仍活跃，但全走 BepInEx **进程内 API**，README 里没有 `fwl2`/`db2`/`chunk` 任何字样 |
| `supercraft` World & Modpack Doctor、DoomHosting Seed Finder | 声称"支持 1.0"，但只是**目录级**或**只读 `_main.N.fwl2` 元数据**（world name / seed / modifiers），不解 `db2`/`.chunk` 字节 |
| `runeberry/ValheimServerGUI` PR#81、`jnsartwell/valheim-hetzner-iac` PR#43 | 2026-09-10，仅做 tar / 整目录处理 |

**关键时间线**（影响研究窗口）：

- `0.221.13` PTR（2026-05-06）：chunked 存档系统首次公开测试 — 已有 4 个月可研究窗口
- `1.0` 发布（2026-09-09）：Deep North，Unity 引擎升级，**没有公开测试分支给 mod 作者**
- 当前版本：`1.0.12`（Steam）/ `1.0.10`（其它平台），网络版本 39，**WorldVersion 41**
- Iron Gate 在 PTR 说明里的原话：*"需要提醒的是，这个系统还没有和 mod 一起测试过。"*

**结论**：4 个月的 PTR 窗口里，社区把精力放在了"目录级适配"（备份 / 迁移 / 只读元数据），**没有人往下钻到字节层**。你的报告是这条线上的第一份。

---

## 三、第二层：ZDO 编解码 = 社区成熟，可直接参考

### 3.1 两套独立开源实现

| 项目 | 语言 | 价值 |
|---|---|---|
| **`crazicrafter1/Avledet`** | C++（Valheim 服务端重写） | `library/include/ZDO.h` + `library/src/ZDO.cpp` —— 完整的 ZDO 打包/解包实现 |
| **`Kakoen/valheim-save-tools`** | Java | `Zdo.java` / `ZPackage.java` —— 存档侧的读写实现，含 `ReverseHashcodeLookup` |

两套实现在 **flags 位定义上完全一致** —— 这是很强的交叉验证。

### 3.2 ZDO 记录的真实结构（两套实现共识）

```
flags (uint16, 小端)        ← 位掩码，决定后面出现哪些字段段
sector   (Vector2s)
position (Vector3, 3×float32)
prefab   (int32, StableHash)
  ├ bit12 置位 → rotation (Vector3, 欧拉角，单位度)
  ├ bit0  置位 → connectionType(1B) + connectionHash(int32)
  ├ bit1  置位 → floats 段
  ├ bit2  置位 → vector3s 段
  ├ bit3  置位 → quats 段
  ├ bit4  置位 → ints 段
  ├ bit5  置位 → longs 段
  ├ bit6  置位 → strings 段
  └ bit7  置位 → byteArrays 段
```

**每个字段段（flags bit1~bit7）的内部结构完全相同：**

```
数量 N (变长编码)
N × [ 字段哈希(int32) + 值 ]
     其中 byteArray 的值 = 长度前缀(int32) + 数据
```

**数量 N 的变长编码**（`ZPackage.readNumItems`，源码原文）：

```java
if (worldVersion < 33) return readChar();      // 旧版：2 字节
int num = readByte();
if ((num & 128) != 0)                          // 最高位 = 续接标志
    num = ((num & 127) << 8) | readByte();     // 1 或 2 字节
```

### 3.3 flags 位索引（Avledet `ZDO.h` 原文）

| 位 | 含义 | 位 | 含义 |
|---|---|---|---|
| 0 | Connection | 7 | ByteArray |
| 1 | Float | 8 | Persistent |
| 2 | Vec3 | 9 | Distant |
| 3 | Quat | 10–11 | Type（取值 0–3） |
| 4 | Int | **12** | **Rotation** |
| 5 | Long | | |
| 6 | String | | |

⚠️ 两套实现**都只定义到 bit 12** —— 也就是说它们覆盖的是 **1.0 之前的版本**。

### 3.4 官方社区文档

- **Valheim-Modding Wiki · ZDO Hashes** — 字段名 → 哈希对照（社区维护，自述 incomplete）：`https://github.com/Valheim-Modding/Wiki/wiki/ZDO-Hashes`
- **StableHashCode 算法**已是公认算法，有 Java / C# / Python **多语言独立复刻**（DJB2 双累加器变体：`5381` / `1566083941`）—— 与你报告 2.2 节逆出的算法一致

---

## 四、逐条对照：你的实验结论 ←→ 社区机制

| # | 你的实测现象 | 社区对应机制 | 印证 |
|---|---|---|---|
| 1 | 等长替换成功（G1/G3），不等长替换失败（G2，仅差 13B） | ZDO 记录长度完全由 flags 决定，**后续所有字段偏移强耦合**，改 1 字节即全盘错位 | ✅ |
| 2 | `ArgumentOutOfRangeException: count` @ `BinaryReader.ReadBytes(count)` | `readLengthPrefixedByteArray()` = `readBytes(readInt32())` —— 你破坏的正是那个 **int32 长度前缀** | ✅ **精确对应源码** |
| 3 | 追加 1 条不崩、**≥2 条必崩**（含"原样未修改的真实记录"E1） | 尾部"额外数据区"充当了缓冲：多读的 1 条记录被它"喂饱"了，第 2 条就不够了 | ✅ 自洽 |
| 4 | 只改计数不加记录 → EndOfStream（CT2） | 头部 int32 ZDO 数是**唯一解析依据**，改大即读越界 | ✅ |
| 5 | 批量替换 1,121 条崩溃 | 大规模重排后命中 zone 的 **spawn 数据上限 255**（见第六节） | ⚠️ 待排查 |
| 6 | 必须挂游戏进程内才能落地 | PlanBuild 源码即 `ZNetScene.instance.CreateObject(ZDOMan.instance.GetZDO(zdoid))` | ✅ **源码印证** |
| 7 | `_main.N.ok` = `29 00 00 00`，"版本标记非校验和" | `0x29` = **41**，正是 1.0 的 WorldVersion | ✅ **判断正确** |

**第 6 条的完整语境**：社区所有世界编辑类 mod（PlanBuild / JereKuusela 全系 / Infinity Hammer）**一律在进程内调用游戏 API**；离线侧工具（SaveTools / BuildConverter）**只产出中间格式**（blueprint / vbuild / CSV），再交回 mod 导入。**没有任何"纯离线新增建筑并成功"的公开案例** —— 你的结论与社区实践完全一致。

---

## 五、需要修正的三处（价值最高的部分）

### 修正 1 ⭐ 不是"每个 prefab 各一套序列化"，而是"统一 flags + 分段 KV"

报告第四节原文：

> Valheim 的 ZDO 是**按每个 prefab 自己的字段定义顺序**序列化的（不是通用 KV 表）……因此要做到可靠写入，必须完整实现"每个 prefab 的字段编解码器"

**实际不是这样。** 编码规则对**所有** prefab 完全统一，就是第三节那套 `flags + 分段 KV`。不同 prefab 的差别只是"有哪些字段段、段里有哪些字段"，**规则本身是通用且已公开的**。

**这意味着：**

- ❌ 不需要为 57,707 个 prefab 各写一个编解码器
- ✅ 只需要实现**一套约 100 行的通用编解码器**（flags 解位 + 8 种字段段循环）
- 报告里 `woodwall`(51B) / `wood_floor_1x1`(38B) / `Rock_4`(39B) / `Beech1`(39B) 长度不同，正是**同一个 flags 机制**在不同字段组合下的自然结果 —— 那几串"字段标识"（如 `01 a2 1e 83 34`）就是 `字段哈希(int32)`，不是 prefab 私有标记

**这是本次核查最有行动价值的结论：报告第四节把难度从"实现一套规范"误判成了"实现六万套规范"。**

### 修正 2 `26 5e cb` 不是"3 字节标记"

报告 2.3 节把它读作"rotation 字段标记（仅当该字段存在）"。

按小端 `uint16` 读，前两字节 `26 5e` = **`0x5E26`**：

```
0x5E26 = 0101 1110 0010 0110b
  bit 1 (Float)      = 1  ✓
  bit 2 (Vec3)       = 1  ✓
  bit 5 (Long)       = 1  ✓
  bit 6 (String)     = 1  ✓
  bit 8 (Persistent) = 1  ✓
  bit 9 (Distant)    = 1  ✓
  bit 10-11 (Type)   = 01 → 1
  bit 12 (Rotation)  = 1  ✓  ← 关键
  bit 14             = 1  ← 超出社区已定义范围
```

**第 12 位置位** —— 而 bit12 在 Avledet 和 Kakoen 两套独立实现里都定义为"写 rotation 字段"。这与你"该标记仅在朝向存在时出现、其后紧跟朝向角"的观察**完全吻合**。

→ 也就是说：**你观察到的是 flags 本身**，不是额外标记；是 2 字节不是 3 字节（第三个字节 `cb` 属于下一个字段）。
→ 顺带一个线索：**bit 14 也被置位**，而社区两套实现都只定义到 bit12 —— 这提示 **1.0 新增了标志位**，也正是现有社区实现没覆盖 1.0 的原因之一。

**另外**：报告记录的字段偏移疑似存在 **~6 字节基准偏差**（`flags(2) + sector(4)`）。建议用这套已知结构重新对齐一次偏移量。

### 修正 3 补强：`29 00` 的来源已确认

`0x29` = 十进制 **41**。官方服务器在 1.0 上载入世界时，控制台会打印：

```
ZDOMan.LoadChunks - ... WorldVersion: 41 [DeepNorth]
```

你读到的 `_main.N.ok` 里的 `29 00 00 00` 和 chunk 文件开头的 `29 00`，**同源，都是 WorldVersion = 41**。这一条你判断对了，现在有官方日志作为外部佐证。

---

## 六、你很可能踩到了一个已知的坑：zone spawn 上限 255

两条独立一手来源：

**1. `ASharpPen/Valheim.LessZdoZoneCorruption`** —— 源码 `src/Fixes/ZdoBlockOverflow.cs`：

```csharp
data.Count >= 255)
```

mod 描述原文：*"Valheim 最近开始对每个 entity 可持久化/同步的数据条目数量设置了一个特定上限……举例来说，每个区域要记录生物何时可以/应该生成。如果使用改变生成或袭击的 mod，这个列表会变得大得多。列表一旦足够大，就很可能出现意外错误，而唯一真正靠谱的解决办法大概是找一个能清除损坏 ZDO 的工具。"*

**2. `JereKuusela/valheim-save_recovery`** —— README 明确写道：每个 spawn 条目存储在**区域对象（zone object）**上；数量用 **1 字节**写入，**256+ 会溢出 255**；**一旦损坏不可修复**。

**为什么这对你重要：**

- 数量用**1 字节**写 —— 超限直接**字节级错位**，症状正是你遇到的 `EndOfStream` / `count` 越界
- 你报告里"单 chunk 批量替换 1,121 条"的实验（I1/I2/I3）失败，**值得优先排查是否触发这个上限**
- 你说的 chunk 末尾"**额外数据区**（末尾 ≥44 字节，含跨 chunk 重复的结构片段）"—— 高度怀疑就是 **zone 对象的 spawn 时间戳表**（每个 chunk 一个 zone，所以结构重复）

---

## 七、社区先例：离线改档的失败案例（同一族错误）

| 来源 | 操作 | 结果 |
|---|---|---|
| `Kakoen/valheim-save-tools` issue #6 | 复制 `Vendor_BlackForest` 条目、改坐标 | `NullPointerException at ZPackage.writeString / PrefabLocation.save` |
| 同上 issue #79 | 解析错位 | 读到负偏移、`not a valid UTF-8 character` |
| Steam 社区讨论 | 载入损坏存档 | `ZDO.Load → ZPackage.ReadInt → EndOfStreamException: Unable to read beyond the end of the stream` |
| 你 | 追加/批量替换 ZDO | `EndOfStream` / `ArgumentOutOfRangeException: count` |

**→ 同一族错误。** 区别在于：之前的案例都是"读错位"，你是**第一个系统性做到"写"这一侧并给出 20 组对照实验的**。

---

## 八、可直接参考的资源清单

| 资源 | URL | 可靠度 |
|---|---|---|
| Avledet（C++ ZDO 打包/解包实现） | `github.com/crazicrafter1/Avledet` | 一手源码 |
| valheim-save-tools（Java 存档读写） | `github.com/Kakoen/valheim-save-tools` | 一手源码 |
| Valheim-Modding Wiki · ZDO Hashes | `github.com/Valheim-Modding/Wiki/wiki/ZDO-Hashes` | 官方社区文档 |
| LessZdoZoneCorruption（255 上限） | `github.com/ASharpPen/Valheim.LessZdoZoneCorruption` | 一手源码 |
| valheim-save_recovery（zone spawn 机制） | `github.com/JereKuusela/valheim-save_recovery` | 一手文档 |
| PlanBuild（进程内落地） | `github.com/sirskunkalot/PlanBuild` → `BlueprintManager.cs` | 一手源码 |
| Expand World Data（进程内 ZDO 读写） | `github.com/JereKuusela/valheim-expand_world_data` | 一手源码 |
| 最大公开 prefab 哈希表 | `github.com/mikermeme/ValheimBuildConverter` → `master_hash_lookup.json`（约 2.2 万条，其中约 1.37 万条可用 StableHash 复算命中，覆盖全部建筑件） | 一手（已复算） |
| 运行时 prefab dump 工具 | Thunderstore: `Skarif/ValheimPrefabParser`（约 6,284 个 prefab） | 一手 |
| StableHashCode 多语言复刻 | `github.com/jarrettv/ValHelp`（C#）、Avledet 的 `dn_hash_exporter.py` | 一手 |

---

## 九、建议的下一步

1. **用第三节的 flags 表重新标注你的 chunk 记录** —— 大概率能一次读通整个字段区，不再需要"猜字段边界"
2. **优先攻克"额外数据区"** —— 先验证它是不是 zone 的 spawn 表（含 255 上限）。建议做 **round-trip 验证**：原样读 → 原样写回 → 比对字节是否 100% 一致。这是验证"是否真的读懂格式"最快的方法
3. **放弃"每个 prefab 写编解码器"的路线** —— 实现一套通用 flags 编解码器即可（修正 1）
4. **重新核对 offset 基准**（修正 2 提到的 ~6 字节偏差），以及 **bit 13/14 这两个 1.0 新增位**的含义
5. **落地建筑仍然建议走 PlanBuild** —— 你原报告的推荐路径，社区实践完全支持

---

## 附：你这份报告的独立价值

需要说清楚的是，以上"社区有研究"的部分，**都是 1.0 之前的版本**（世界版本 ≤ 34）。

你的实验是在 **WorldVersion 41** 上做的，而：

- 没有任何公开实现覆盖 41
- 你发现的"额外数据区"、bit14 新标志位、"追加≥2条必崩"这些现象，**社区没有对应记录**
- 你的 20 组对照实验，是**首次有人系统性地把"写"这一侧的行为记录下来**

所以准确的表述是：**你站在社区已铺好的 ZDO 规范之上，第一次走到了 1.0 新容器格式的边界之外**。第二、三节的内容能让你少走很多弯路，但第四、六节要解决的问题，目前确实只有你自己在推进。
