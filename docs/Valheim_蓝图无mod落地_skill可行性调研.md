# Valheim 蓝图「无 mod 落地」× Agent Skill 可行性调研

> 调研日期：2026-09-20
> 问题：**`.planbuild` 的 `.blueprint` 或 `.vbuild` 文件，能否在"不安装 mod"的前提下，用 skill 直接落地到世界存档？**
> 依据：本仓库 14 份实验文档的实测证据链（2026-09-12 ~ 09-16）+ 本轮新增的一手源码核对
> （PlanBuild `Blueprint.cs` / `PieceEntry.cs`、ValheimBuildConverter `vbuildCodec`、Expand World Data `blueprints.md`）
> + 本轮新增 PoC `tools/bp_parse.py`（自检 17/17 通过，真实社区蓝图 403 件全解析）。

---

## 0. 三十秒结论

把问题拆成**解析层**（读懂蓝图文件）和**写入层**（把建筑放进世界并让它活下来）两层，答案完全相反：

| 层面 | 零 mod？ | 结论 |
|---|---|---|
| **解析层**：`.blueprint` / `.vbuild` → 件清单 | ✅ 完全零 mod | 两者都是**纯文本**，规范已一手核实并双实现交叉验证；`tools/bp_parse.py` 已实现（含 GroundLayerPy 自动推导、哈希命中率预检） |
| **写入层 · 纯离线改档**（任何进程外工具直接改 chunk） | 严格零 mod | ❌ **整栋建筑不可行**——本项目 20 组对照实验证伪 + 社区零公开成功案例（§3.1）。离线只支持：只读分析、等长替换、**单条**记录插入 |
| **写入层 · 服务端临时执行器**（落地后整体删除） | **客户端零 mod**；服务端临时装 BepInEx | ✅ **唯一实测走通的路径**——本项目 XiBpBuilder v0.31.1：R2~R23 二十余轮迭代、4625/4625 零丢失、全程 headless 零客户端接入 |
| **Skill 编排** | — | ✅ 可行：解析 / 预检 / 备份 / 对账 / 报告五段**纯离线可 skill 化**；"落地"动作本身必须委托给执行器（§4） |

**一句话回答**：
> "skill + 纯离线改档"落地整栋蓝图**不可能**（不是难度问题，是实测证伪）；
> "skill 编排 + 服务端临时执行器、落地后删干净"可以做到**客户端零安装、事后服务器零残留**，
> 且本项目已把这条链路全自动跑通——skill 化只剩工程封装，不是机制问题。

另外两条硬结论（同样来自实测，不是推断）：

1. **`.planbuild` 不是文件扩展名**。Valheim 全生态的蓝图文件只有 `.blueprint`（PlanBuild）、`.vbuild`（BuildShare）、`.yml`（KGvalheim Blueprint）、`.rewind`（Rewind，二进制）。"planbuild"是 mod 名（§1.1）。
2. **"能写进去" ≠ "落地成功"**。落地要过两道门：数据写入 + 建筑存活（WearNTear 支撑机制）。第二道门**没有任何离线解**——支撑判定依赖游戏内物理测量，这是执行器不可被 skill 脚本替代的根本原因（§2）。

---

## 1. 三种"格式"核实

### 1.1 `.planbuild`：不存在的扩展名

检索面：Thunderstore（PlanBuild 0.19.x / Buildheim / Blueprint by KGvalheim）、Nexus、GitHub API。

| 你手上可能的东西 | 实际是什么 | 怎么解析 |
|---|---|---|
| `xxx.blueprint` | PlanBuild 原生格式（文本） | ✅ `bp_parse.py` |
| `xxx.vbuild` | BuildShare 格式（文本，PlanBuild 官方兼容读取） | ✅ `bp_parse.py` |
| `xxx.zip` | Nexus/Thunderstore 分发压缩包（内含 `.blueprint` + 同名 `.png` 缩略图） | ✅ `bp_parse.py` 直接读 zip |
| 市场/blob 二进制 | PlanBuild Blueprint Marketplace 的传输形态：`Utils.Compress`（raw deflate）包裹 `[int32 行数][N×C# string][int32 png长][png]`（`Blueprint.ToBlob`） | ✅ `bp_parse.py` 三种 zlib 包裹全试 |
| "planbuild 文件夹里的文件" | Reddit 语境下的 `BepInEx/config/PlanBuild/blueprints/` 目录，里面装的还是 `.blueprint` | 同上 |

### 1.2 `.blueprint` 规范（一手：PlanBuild `Blueprints/Blueprint.cs` + `PieceEntry.cs`）

```
#Name:<名称>            ← 头部（均可缺省；缺 Name 时用文件名）
#Creator:<作者>
#Description:<JSON字符串>
#Category:<分类>
#SnapPoints             ← 段切换标记（状态机，默认 Pieces 段）
x;y;z                   ← 吸附点，每行一个
#Terrain
shape;x;y;z;radius;rotation;smooth;paint   ← 地形修改段（TerrainModEntry）
#Pieces
name;category;px;py;pz;qx;qy;qz;qw;info;sx;sy;sz[;zdoData;chance]
```

要点与兼容怪癖（全部按 PlanBuild 源码逐条核实）：

- **分号分隔、InvariantCulture 浮点**；老文件可能用逗号小数点（PlanBuild 整行 `,`→`.` 替换）
- `name.Split('(')[0]`：带 `(2)` 之类后缀的名字被截断
- `info`（第 10 段）是 **JSON 序列化字符串**（`""`=空）：木牌文字、箱子内容、盔甲架姿势等都在这（`detectAdditionalInfo` 可再细分）
- `sx;sy;sz`（11~13 段）缺省 = 1:1；**四元数是完整姿态**，不是 yaw
- 扩展字段 14/15 = `zdoData;chance`（Infinity Hammer 写、Expand World Data 读，PlanBuild 忽略）；
  `#center:` 标记中心件、`#TerrainHeight:` / `#TerrainPaint:` 是 Infinity Hammer 的地形快照段
- 其余 `#` 开头行 = 注释

### 1.3 `.vbuild` 规范（BuildShare）

**空白分隔**（不是分号），**四元数在前、位置在后**：

```
name qx qy qz qw px py pz
```

两套独立实现逐字段一致（交叉验证）：

| 实现 | 证据 |
|---|---|
| PlanBuild `PieceEntry.FromVBuild`（C#） | `parts[0]`=name（同样截断 `(`）、`parts[1..4]`=四元数、`parts[5..7]`=位置；category 固定 `Building` |
| ValheimBuildConverter `vbuildCodec`（JS，mikermeme） | `p.length < 8` 跳过、`normalizeQuat(p[1..4])`、`pos(p[5..7])`、逗号小数点仅在整行无 `.` 时替换 |

### 1.4 解析层结论

**零 mod 100% 可行，且已经完成**：`tools/bp_parse.py`（本轮新增）

- 输入 `.blueprint` / `.vbuild` / `.zip` / 市场 blob，自动嗅探
- 输出：全保真 JSON（四元数/scale/info/zdoData/吸附点/地形段）+ XiBpBuilder 件清单兼容 txt（`name|hash|x|y|z|yaw`）
- 预检报告：件数/分类/包围盒/**哈希命中率**（`format/prefab-hashlib.json` 反查 + StableHash 兜底补算，
  正是激活报告坑 C.1 `wood_wall_log_4x0.5` 缺表的解法）/**GroundLayerPy 自动推导**（收官报告 §9.1
  "跳过孤立低层"算法首次移植成 Python，自检里用 usagi 真实 py 分布复现出 −1.60）/地下件计数/非纯 yaw 件计数
- 实测：自检 17/17 通过；真实社区蓝图 `valheim_emblem.blueprint`（408 行）解析 403 件、哈希命中 403/403、
  正确检出 402 件非纯 yaw 旋转与 134 件 0.1 缩放 sign

---

## 2. "落地"是两道门，不是一道

**门 1 · 写入**：建筑以 ZDO 形式持久化进世界。
**门 2 · 存活**：`WearNTear` 支撑机制不把它销毁。

门 2 才是本项目 R2~R23 二十余轮实验（崩塌归因八轮闭环 → 锚点自动求解十六轮收官 → 通用化 R20~R23）证明的真正难点，关键机制（全部反编译 + 实测）：

```
专用服务端把 refPos 写死在 (1000000, 0, 1000000) → 什么都不实例化、什么都不计算
玩家走进 ~96 米 → ReleaseNearbyZDOS 把所有权交给玩家 → UpdateSupport 开始算
支撑 = 地面锚点（包围盒命中地形薄壳）起步、件对件乘性衰减传递
木构件下限 10 / 石构件下限 100（差 10 倍）→ 链尾石构件跌破下限 → 当帧满血销毁
```

所以"人一走近房子就塌"不是 bug 是必然（《距离门控与假人方案》§1.5）。**没塌 ≠ 结构合法，只是还没人认领。**

对"无 mod 落地"的直接推论：

- 门 2 的判定（`Physics.OverlapBox` 命中地形薄壳、支撑链衰减）**只能在游戏进程内测量**——
  锚点自动求解 2.3 秒收敛靠的就是实例化后的真实物理测量（收官报告 §1.3"预测≠实测，差 42 件"）
- 地形贴合是门 2 的输入，而地形 delta 的写入**即使走进程内 API 也出过持久化事故**
  （09-16 事件：zone 卸载重载后 delta 丢失、地表复原、房子从 1658 件塌到 197 件）
- 结论：**不存在任何"离线把门 2 也解决掉"的方案**。离线最多做预检（bp_parse.py 的地下件/GroundLayerPy
  报告就是落地前的风险预警），存活验证必须在进程内闭环。

---

## 3. 六条路线可行性矩阵（证据篇）

| # | 路线 | mod 足迹 | 整栋落地 | 判定 |
|---|---|---|---|---|
| 1 | 纯离线改 chunk 文件 | **零** | ❌ 实测证伪 | 只配做"小改"skill |
| 1b | 离线新建 chunk 文件（未测变体） | 零 | ❓ 未验证，预判低 | 不推荐（§3.1.4） |
| 2 | 原版控制台 / devcommands | 零 | ❌ 无此能力 | — |
| 3 | 外部输入自动化（键鼠宏） | 零 | ❌ 不现实 | — |
| 4 | PlanBuild / Buildheim 等 | 客户端+服务端常驻 | ✅ | 被"不装 mod"约束排除 |
| 5 | **服务端临时执行器（本项目已证）** | **客户端零；服务端临时、事后可删净** | ✅ **4625/4625** | ★ 唯一实测走通 |
| 6 | Expand World Data 当地点生成 | 服务端常驻 | ✅（仅未生成 zone） | 旁支，不适用已有存档 |

### 3.1 路线 1：纯离线改档 —— ❌（证据最厚的一条）

#### 3.1.1 二十组对照实验（《存档写入_技术报告》§三，副本世界实测）

| 操作 | 结果 |
|---|---|
| 追加 1 条自制记录 + 同步计数 | ✅（但早期版本是"容错假象"，精确边界后才真成功） |
| 追加 ≥2 条（**含游戏原样的真实记录**） | ❌ 全部 EndOfStream |
| 任意位置插入 2 条（文件开头/中间/末尾前） | ❌ 全部失败 |
| 等长替换 1~2 条（同 prefab 天然等长） | ✅ |
| 不等长替换（只差 13 字节） | ❌ |
| 批量等长替换 315 / 1,095 / 1,121 条 | ❌（`ArgumentOutOfRangeException: count` = 破坏了 byteArray 长度前缀） |
| 跨 6 chunk 全量替换 1,805 条（蓝图全件） | ❌ |

#### 3.1.2 机制解释（为什么批量必死）

- 记录长度 = `f(flags, prefab)` 唯一确定，记录流**紧密排列无间隙** → 改一条的长度，后面全部错位
- 记录流里**夹杂非 ZDO 数据块**（地形 delta，无 prefab 记录头，靠"锚点间隔 ≥120 字节"才能定位——
  《地形delta存档侧核查》§四）→ "记录区末尾"不是一个干净的追加位置
- chunk 头 count 是**唯一解析依据**；`.chunks` 索引还有总数、条目数两处要同步
- ⚠️ 诚实备注：「≥2 条必崩」的机制解释在两代文档间有过演变（早期归因"尾部额外数据区缓冲"，
  格式核查后归因"插入点边界计算错误"并建议重测）。**但无论机制是哪版，工程事实一致**：
  本项目从未让离线批量插入成功过一次，而进程内 API 路线一次就通（1805 件零异常）。
  `land_official.py` / `bp_land.py` 就是那条被放弃的离线路线的遗物，保留作反面教材。

#### 3.1.3 社区侧印证（《存档写入_社区参考结论》§四第 6 条）

- PlanBuild / JereKuusela 全系 / Infinity Hammer：**一律进程内调 API**
  （`ZNetScene.instance.CreateObject(ZDOMan.instance.GetZDO(zdoid))`）
- 离线侧工具（valheim-save-tools / ValheimBuildConverter）**只产出中间格式**，从不写存档
- **全网没有一例"纯离线新增建筑并成功加载"的公开案例**；公开案例全是同一族读错位报错

#### 3.1.4 未测变体：离线**新建** chunk 文件（而不是改现有 chunk）

理论上绕开"插入点/尾部结构"问题的唯一离线思路：给目标 zone 造一个全新
`<x>_<y>__<gen>_<save>.chunk` + `.chunks` 索引新条目，让游戏加载时自己实例化。预判**不可行/不值得**：

| 未知/风险 | 说明 |
|---|---|
| 尾部结构语义未解 | 实测尾部 2~136 字节不等，内容是什么、新文件该写什么，无人知道 |
| `gen` 号语义未验证 | zone 代际与文件名校验关系未测；不匹配可能被静默丢弃 |
| 已生成 vs 未生成 zone | 已生成 zone 的地形/自然物数据在旧 chunk 里，新 chunk 与之如何合并未知 |
| 255 上限 | zone spawn 数据 1 字节计数溢出即不可修复损坏（LessZdoZoneCorruption 一手源码） |
| **门 2 无解** | 就算记录全部加载成功：地形没整平、锚点没求解 → 玩家一走近就塌（§2）。离线无法预演支撑 |

结论：投入产出比为负。真想验证也应先起测试服做 4 组小实验（收官报告同款方法论），但即便全过，门 2 依然过不去。

#### 3.1.5 离线**能**做什么（skill 可承载的"小改"清单）

| 操作 | 状态 | 现成工具 |
|---|---|---|
| 全量只读分析（物件清单/平面图/统计/导出蓝图/崩塌对账） | ✅ 可靠 | `zdo_lib.py` `zdo_v10.py` `chunk_walk10.py` |
| 等长替换单条记录（搬墓碑、改朝向角） | ✅ | 同 prefab 克隆天然等长 |
| **单条**记录插入（传送门级"小落地"） | ✅ 需同步三处计数 | `insert_portal.py`（模板复制法完整流程） |
| 世界级数据（fwl2 明文层：规则键/boss 进度/玩家名单） | ✅ | README §三 |
| 角色档作弊标记清除 | ✅ | `clear_devcommands.py` |

### 3.2 路线 2：原版控制台 / devcommands —— ❌

- 原版控制台**没有**任何"在坐标上放构件"的命令；`spawn` 只出不带姿态的物件且落点跟准星
- 专用服务端**没有交互控制台**（`Server_devcommands` 本身就是 mod）
- `devcommands` 会打成就锁标记（1.0.12 起仅 Steam 可洗白，且不补发）——
  这正是本项目放弃控制台路线、发明 `[Activate]` refPos 覆盖的原因之一

### 3.3 路线 3：外部输入自动化（AHK/键鼠宏模拟原版锤子）—— ❌

无坐标级精度、吸附/材料/支撑校验全部失控、千件级工程量、版本更新即失效。一行都不值得写。

### 3.4 路线 4：客户端 mod 路线（对照，被约束排除）

| mod | 安装面 | 备注 |
|---|---|---|
| PlanBuild 0.19.x | 服务器 + 建造者客户端 | 社区首选；好友零安装可进服（官方 FAQ 原文）；管理员可免材料直接建 |
| Buildheim | 仅客户端 | Litematica 式全息图 + 逐层自动建；"No server mod required"，但建造者自己必须装 |
| Blueprint (KGvalheim) | 客户端 | `.yml` 原生格式，兼容读 `.blueprint`/`.vbuild` |

用户约束"不安装 mod"（客户端一个个装依赖不现实）→ 全部排除。列在这里只为界定问题边界。

### 3.5 路线 5：服务端临时执行器 —— ✅ 唯一实测走通（本项目主成果）

**"无 mod"的严格语义**：客户端零安装；服务端只在落地窗口内临时存在 BepInEx + 插件，
落地完成即可把 `winhttp.dll` / `doorstop_config.ini` / `doorstop_libs/` / `BepInEx/` 四件套整体删除，
恢复纯原版服务端。**建筑本身是 100% 原版世界数据（ZDO），不依赖任何 mod 存在。**

已实证的完整链路（XiBpBuilder v0.26 → v0.31.1，R2~R23 二十余轮迭代）：

```
停服 → 部署插件 → 起服（headless，零客户端）
  [Cleanup]  清落点自然物（等激活后再扫；非持久对象也要清；区域兜底）
  [Terrain]  可选：整平/开挖（改 m_levelDelta 增量公式，±8 米硬限）
  [Build]    CreateNewZDO + SetPrefab + SetPosition + SetRotation + Persistent
  [Activate] Game.FixedUpdate postfix 把 refPos 从 (1e6,0,1e6) 覆盖到工地
             → 服务端自己认领全部件、真实跑 UpdateSupport（"假人"问题的极简解）
  [Anchor]   冻结磨损手术窗口内：逐件测 d_i → 必死件判据自动选档 → 整体位移 → 解冻
             （R23：GroundLayerPy 与位移全自动，配置里只剩落点坐标手填）
  → DelayedSave → 停服 → 离线对账（chunk walk + 名字位置双匹配）→ 删 BepInEx 四件套
```

成绩单：usagi-forest-lodge 4625 件 **0 丢失**（三次独立达成：R16/R19/R23）；锚点求解 2.3 秒；
对账用离线工具闭环（`存活 == 4625`），**验证环节本身零 mod**。

成就：不使用 devcommands → 角色/世界档不打标记（可用 `check_cheat.py` 复核）。

**残余风险（skill 必须写进决策树的四条）**：

| 风险 | 证据 | 缓解 |
|---|---|---|
| 地形 delta 持久化事故 | 09-16 事件：zone 卸载重载后 delta 丢失 → 地表复原 → 房子 1658→197 件 | **优先选天然平地落点、`[Terrain]` 零改动或最小改动**；靠 `[Anchor]` 下沉埋入适配现有地形，而不是改地形适配蓝图 |
| crossplay 不兼容 | BepInEx 通病 | 落地窗口内关 crossplay；删净后恢复 |
| 版本漂移 | Harmony 签名跟游戏小版本走 | skill 固化"每次游戏更新先在副本世界跑一轮对账"的铁律 |
| 件清单姿态保真 | `bp_pieces.txt` 只存 yaw，斜面屋顶（45°/26°）可能歪（操作说明已知限制） | 清单格式升级为全四元数（`bp_parse.py` 的 JSON 输出已带 `qx..qw`，执行器侧改 `SetRotation(Quaternion)` 即可） |

### 3.6 路线 6：Expand World Data 把蓝图当"地点"生成 —— 旁支

JereKuusela 的 Expand World Data 支持在**世界生成期**把 `.blueprint`/`.vbuild`（含地形快照段）
作为 location 刷进**尚未生成的 zone**。也是服务端 mod（常驻），且只对未探索区域/新地图有效——
不满足"落进已有存档的指定位置"，列此仅为格式生态完整性。

---

## 4. Skill 化设计（"使用 skill"的正面回答）

### 4.1 定位

**Skill 的手脚在离线侧，落地动作委托给执行器；skill 负责编排、判读、对账、报告。**
这不是妥协，是机制决定的分工：门 2（支撑存活）只能在进程内闭环（§2），
而进程外的每一步（解析/预检/备份/对账/清理）本项目都已验证可纯 Python 完成。
《交接文档》提到的 1200+ 行 `valheim-dedicated-server` SKILL.md 就是本设计的雏形，本文是它的可行性定稿。

### 4.2 七段流水线

| 段 | 动作 | 工具 | 需要 mod？ | skill 化程度 |
|---|---|---|---|---|
| 1 输入归一 | `.blueprint`/`.vbuild`/zip/blob → 统一件清单（JSON+txt） | **`bp_parse.py`（本轮新增）** | 否 | ✅ 100% |
| 2 预检报告 | 哈希命中率 / 地下件 / GroundLayerPy / 非纯 yaw / 占地 / 落点风险评级 | `bp_parse.py` + `prefab-hashlib.json` | 否 | ✅ 100% |
| 3 备份 | 整个世界目录快照（停服状态下） | shell | 否 | ✅ 100% |
| 4 部署执行器 | 停服检查 → 拷 BepInEx 四件套 + 插件 → 生成 cfg（落点/YOffset=0/AutoSolve） | skill 生成命令，Windows 侧执行 | **临时** | ⚠️ 编排 100%，执行需环境 |
| 5 headless 落地 | 起服 → 盯 `bpbuild.log` 五段标记（开挖分解/对账命中/锚点收敛/件数时序/体检） | 日志判读规则已固化（收官报告 §6 合格标准表） | 临时 | ⚠️ 同上 |
| 6 离线对账 | 停服 → chunk walk → 名字+位置双匹配 → 存活数 == 蓝图件数 | `zdo_lib.py` + 对账脚本（**双匹配**，坑 C.2） | 否 | ✅ 100% |
| 7 清理+报告 | 删 BepInEx 四件套 → `check_cheat.py` 复核 → 出带通过/失败列的报告表 | shell + 本仓库工具 | 否 | ✅ 100% |

### 4.3 资产映射：本仓库已有什么、还缺什么

| 环节 | 已有资产 | 缺口 |
|---|---|---|
| 格式解析 | **`bp_parse.py` ✅（本轮补齐）** | — |
| prefab 哈希 | `format/prefab-hashlib.json`（57,706 条）+ StableHash（双实测锚点值） | 库缺口用 StableHash 兜底已内建 |
| 存档只读/对账 | `zdo_lib.py`（walk/candidates/计数同步）、`zdo_v10.py`、`chunks_index.py`、`anchor_diff.py` | —（**`bp_reconcile.py` 已入库**：hash+位置双匹配、y 量化 + 索引恒等式前置检查，selftest 四案全过） |
| 执行器 | 设计/配置/日志判读全部文档化（交接文档 §5、收官报告 §5 配方） | Plugin.cs 本体仍在用户 Windows 机器；**参考重建已入库**（`tools/xibpbuilder_reference/`，实机编译已通过，反射签名待运行时验证） |
| 编排 | 操作手册两份（插件版 v0.4 / 使用说明） | —（**`bp_pipeline.py` 已入库**：七段一条命令，断点续跑 + dry-run + 铁律内建） |
| 落点选择 | `[Cleanup]` 保护圈、天然平地案例 (-345,315) | —（**`bp_autosite.py` 已入库**：自然物 y 代理测高自动挑平地，产出落点+PlatformY；caveat：基岩高度由种子生成、不在存档里，少人踩点区无样本需进游戏目视确认） |
| 铁律 | 坑清单 13+3 条、采样陷阱、"件数时序判崩塌" | 提炼进 SKILL.md 决策树 |

### 4.4 SKILL.md 应固化的铁律（从踩坑清单提炼）

1. 动档前**必备份**、**必停服**（DLL 占用 + 写入竞争）
2. 重跑某段删对应 `.flag`，**永远不要 `Force=true`**（重复落地 3610 件的教训）
3. 单次读数一律不可信，**连续采样到稳态 + 带正对照**（09-16 初版结论反转的教训）
4. **判断崩塌看件数时序，不看"支撑不足=0"**（当帧销毁抓不到瞬时态）
5. 对账必须**名字+位置双匹配**、y 也量化（同 (x,z) 叠放多件）
6. 落地前先看**锚点数/必死件数**，不看就是碰运气（收官报告 §7-D 原话）
7. 蓝图含地下结构（`bp_parse.py` 预检报警）→ 地窖开挖 = 崩塌高危 + delta 丢失高危，默认劝退或降级方案

---

## 5. 最终结论与建议

### 5.1 对原问题的直接回答

| 问法 | 答案 |
|---|---|
| skill + **纯离线**（任何环节零 mod）落地整栋蓝图？ | **不可能。** 20 组对照实验证伪 + 社区零案例 + 门 2 无离线解。这不是"还没人做到"，是"机制上做不到可靠" |
| skill + **客户端零 mod、服务端临时执行器、事后删净**？ | **可行，且已实测成熟**（4625/4625 零丢失、全自动锚点求解、headless）。skill 化 = 工程封装，缺口清单见 §4.3 |
| skill 解析 `.planbuild`/`.blueprint`/`.vbuild`？ | **可行，已完成**（`bp_parse.py`；`.planbuild` 扩展名不存在，实际载体全部覆盖） |

### 5.2 按约束强度的决策树

```
接受"服务器临时装 BepInEx、落地后删净"？
├─ 是 → 路线 5：skill 编排七段流水线（推荐；本文 §4 即蓝图）
│        ├─ 落点有天然平地 → [Terrain] 关闭，纯 [Anchor] 下沉适配（最稳，09-16 事故免疫）
│        └─ 必须整平/开挖 → 接受 delta 丢失风险窗口，落地后按 §3.5 缓解表执行
└─ 否（任何环节、任何时刻都不允许 mod）
     ├─ 要建筑本体 → 无解。唯一近似：单条插入级"小落地"（insert_portal.py 上限）
     └─ 退而求其次 → "施工向导"skill：bp_parse.py 解析 → 分层施工图/俯视 HTML/材料清单
        → 玩家用原版锤子手搭（零 mod 能交付的最大成果，本仓库工具链完全支撑）
```

### 5.3 如果继续推进（优先级排序）

1. **把 `Plugin.cs`（v0.31.1）收进仓库** + 构建说明 —— skill 发布的唯一硬缺口
2. 件清单格式升级全四元数（bp_parse.py JSON 已就绪，执行器侧一行改动）
3. 对账脚本按"名字+位置双匹配"重写入库（补 §4.3 缺口）
4. 编排脚本：`停服检查 → 备份 → 部署 → 起服 → 日志判读 → 对账 → 清理` 一条命令
5. `AutoFindFlat`：离线扫 chunk 里现有物件 Y 分布 + 起服后地形采样，把最后一个手填参数消灭

---

## 附录 A · 证据索引

**本仓库**（全部实测）：
`Valheim_存档写入_技术报告`（20 组实验）· `Valheim_1.0记录格式_实测核查`（格式修正与"需重测"备注）·
`Valheim_存档写入_社区参考结论`（社区侧交叉验证）· `Valheim蓝图落地_交接文档`（插件全链路 + 13 坑）·
`Valheim_服务端激活与usagi崩塌_实验报告`（[Activate] 原理与 0.78% 丢失）·
`usagi崩塌归因_六轮迭代复盘` / `_最终结论`（锚点机制、石木下限差 10 倍）·
`Valheim_蓝图落地自动化_自动锚点求解_收官报告`（R16~R23 全自动零丢失、GroundLayerPy 算法）·
`Valheim_距离门控与假人方案_调研报告`（所有权/refPos 反编译证据链）·
`Valheim_地形delta存档侧核查_20260916`（delta 丢失事故 + 地形块交织证据）·
`Valheim_蓝图落地_使用说明` / `_操作说明`（部署与回滚手册）· `地形重塑完成说明`（±8 米限制、heights/delta 双写）

**外部一手来源**（本轮核对）：
PlanBuild `Blueprints/{Blueprint,PieceEntry,SnapPointEntry,TerrainModEntry}.cs`（github.com/sirskunkalot/PlanBuild@master）·
ValheimBuildConverter `index.html`/`codecs-ext.js`（github.com/mikermeme/ValheimBuildConverter@main）·
Expand World Data `docs/blueprints.md`（github.com/JereKuusela/valheim-expand_world_data）·
Thunderstore PlanBuild 0.19.x / Buildheim / Blueprint(KGvalheim) 页面 ·
真实样例 `valheim_emblem.blueprint`（github.com/dsterentyev/img2bpl.pl）

## 附录 B · bp_parse.py 快速上手

```bash
python3 tools/bp_parse.py --selftest                    # 17 项自检
python3 tools/bp_parse.py 蓝图.blueprint                 # 预检报告
python3 tools/bp_parse.py 蓝图.vbuild --structure-only \
        --json out.json --txt bp_pieces.txt             # 过滤家具 + 双格式输出
python3 tools/bp_parse.py 下载包.zip                     # 直接读 zip 内的蓝图
```
