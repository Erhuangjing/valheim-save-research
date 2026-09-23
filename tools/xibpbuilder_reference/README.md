# XiBpBuilder 参考重建（REFERENCE ONLY）

这是《Valheim_服务端实时构建机制_详解》的配套代码骨架，回答「启动 dedicated server 时怎么做到
处理地形 + 清除障碍 + 构建房屋」。

## ⚠️ 这是什么、不是什么

| | |
|---|---|
| ✅ 是 | 从本仓库 4 份实验文档里散落的**真实 patch 代码**重建的**机制骨架**，把七段启动时序拼成可读、可对照的实现 |
| ❌ 不是 | 原版 XiBpBuilder v0.31.1（约 1500 行，**未包含在本仓库**） |

**编译状态**：2026-09-21 实机验证通过（.NET SDK 9.0.313 + Valheim dedicated server 1.0 + BepInEx 5，
0 警告 0 错误，产物 `bin/Release/XiBpBuilder.dll` 28,672 字节；8 个协程状态机完整——见 PR #1 实测报告）。
2026-09-23 Terrain 段重写为 PlanBuild 式逐点整地（见下节「Terrain 段」）后按同一口径（只替换 `REPLACE_ME`）
重新编译：0 警告 0 错误，产物 61,952 字节；副本世界测试服三个蓝图实机验收（skeggoxmanor / nelesstarterbase /
longhouse，数字见 `skills/valheim-blueprint-build/SKILL.md`「实测基准」）。
**运行状态**：隔离环境端到端实测通过（清障→落地→保存，1805/1805 零丢失、prefab 校验失败 0、
坐标公式反推精确吻合）。已修的运行期问题分三轮：
第一轮（#7 uint32 hash 溢出 / #8 锚点 NRE 静默 / #9 GUID 致 cfg 不通用）；
第二轮（5 蓝图 8 轮实测，#12~#17）：**#13 Support 默认改 true**（锚点成功≠免磨损，A/B 实测
false 掉件 3.7%~15.9%、true ±0）、**#12 锚点位移重构**（全集统一位移，见下）、#14 Terrain
三处空桩补实现 + 空跑不写 flag、#17 `.vbuild` 方言2、#16 三处观测性缺口；
第三轮（#19）：Terrain 段**编译不过**的 3 处 API 修正 + 重建链目标对象修正 + `m_heights` 布局
运行时自检，详见下节。

### 已知运行期行为（2026-09-22 第二轮实测后）

| 项 | 行为 | 依据 |
|---|---|---|
| `Support.Enabled` | **默认 true**。「锚点成功则不需锁」已被 A/B 实测证伪 | #13 |
| 锚点位移对象 | **本 run 创建的全集**（`s_ourZdoIds`），等实例化齐后每轮统一位移，结束后全量 Δy 校验；**对账补建路径跳过锚点**（只重建缺失件，整体位移会撕裂建筑，以 cfg `YOffset` 为准） | #12 |
| ⚠ PatchAll 坑 | `PatchAll(Type)` 只补那一个类，**不是整个程序集**——新增补丁类必须逐个注册；启动时打印 `GetAllPatchedMethods()` 实际挂载数自检 | #13 附 |
| Terrain | **2026-09-23 重写为 PlanBuild 式逐点整地**（下节）：只改占地 + 过渡带；保护圈内一个顶点都不改；任一满权重顶点需改超 ±8m 整段放弃；验收不过不写 `terrain_done` | 本轮 |
| 落地高度 | `[Build] AutoPlatformY=true`：件放下前在游戏里实测地形，整栋竖直偏移 = 中位数(地形高 − 件 py)；首测值存 `bp_runtime.txt`，对账补建 / 重载复核沿用 | 本轮 |
| 锚点 | 整过地的建筑**跳过**整体位移。⚠ 旧测量本身不可信：层掩码缺 `terrain`、碰撞体只取根节点（91% 件「无碰撞体」）→ 与地形无关地恒定 −1.05m；不开 Terrain 时仍在用，待修 | 本轮 |
| 判掉件口径 | 清点 mark ZDO（不用总记录数——会混入掉落物/自然物）；支撑体检输出值域与越界计数（防「反射拿错值」被当真没问题） | #16 |

### Terrain 段：PlanBuild 式逐点整地（2026-09-23 重写）

**为什么重写**（正式服实测事故链）：旧实现改的是落点 93m 内 7 块 heightmap 的**全部顶点**
（日志里的「改动 29575 个顶点」= 7 × 65²，约 190m 见方压成同一高度），±8m 截断处成斜坡/断崖、与没改的
zone 交界成断层；直写 `m_heights` + 自拼 6 步重建链，zone 重载按 delta 重算就对不上；不 `ClaimOwnership`
时 `TerrainComp.Save` 对非 owner 是静默空操作（09-16 重载后 delta 丢失的根因）；且完全不看保护圈。

**现在的写法**（照搬 PlanBuild `Blueprints/TerrainTools.cs`，sirskunkalot/PlanBuild，WTFPL）：只改 TerrainComp 的
`m_levelDelta / m_smoothDelta / m_modifiedHeight`（+ `m_paintMask / m_modifiedPaint`），然后
`ClaimOwnership → m_operations++ → Save(false) → Heightmap.Poke(0,false)`，高度/碰撞/渲染交给游戏自己重算。
每个顶点的全部操作先在内存里精确复合（`h' = h + w(a − h)` 顺序复合仍是同形），每个 TerrainComp 只写一次；
跨 zone 边界的共享顶点两侧各写一份 → 不留缝。

**时序**：实测 PlatformY → 放件（磨损冻结、支撑锁定）→ 等实例化 → 量本工具件的**真实碰撞体**（整棵子树）
→ 生成方案 → 预演（保护圈 / ±8m 检查，不过则一个顶点都不写）→ 提交 → 等重算 → 原版地形件 → 验收 → `terrain_done`。

| 规则 | 做法 |
|---|---|
| 朝向 | `[Build] Rotation`（度，俯视顺时针 = Unity yaw）：所有「蓝图 → 世界」换算只走 `ToWorld()`——绕包围盒中心转、再平移到落点；件朝向 = yaw × 件自身朝向；`#Terrain` 方形角度 = 条目角度 + Rotation；离线 `bp_reconcile.transform_pieces(rotation=)` 同式 |
| 落地高度 | 地面层 = \|py − GroundLayerPy\| ≤ LayerTol（**上下都有界**：院墙模式下只设上界会把下层露台/地窖全算进来，整栋抬高 4m）；整栋偏移 = 中位数(实测地形 − 件 py) |
| 平台高度 PadY | 地面层件碰撞体底面中位数 + Embed |
| 占地 | 底面在 [PadY − Sink, PadY + Cap] 的件，xz 包围盒外扩 Pad；地表 = clamp(该点最低件底 + Embed, PadY, PadY + Cap)：略高的件垫土接住，略低的（柱脚）直接埋 |
| 平台 | 占地做闭运算（半径 Close）：斜撑脚/柱脚之间 ≤ 2·Close 的空隙也整平到 PadY（= 先整出整块房基）；更大的内院保留 |
| 深件 | 比 PadY 深 Sink 以上的非地窖件（深桩、下层露台的墙/台阶）不整平台、不挖坑——下层交给蓝图 #Terrain |
| 地窖 | 名字含 floor、底面 < PadY − 0.5 的件：只在件自身范围挖到件底 + Embed（墙体挡土） |
| 过渡带 | 平台外倒角距离变换 + smoothstep；宽度按平台边界最大高差自适应，使最陡处 ≤ SkirtSlope（1.5·Δ/D），夹在 [Skirt, SkirtMax] |
| 保护圈 | 圈内顶点跳过（dump 记 `X` 行供独立复核）；圈外 3m 内把过渡带 / #Terrain 权重平滑压到 0，圈边不留台阶 |
| 蓝图 `#Terrain` | PlanBuild `PlacementComponent.PlaceBlueprint` 原样：circle/square（含旋转）、`CalculateSmooth`、paint |
| 原版地形件 | `mud_road_v2` 等（旧名自动映射 `_v2`）不建 ZDO，交给原版 `TerrainComp.DoOperation`（= RPC_ApplyOperation 里 owner 做的那步） |

**反射成员**（2026-09-23 对 `assembly_valheim.dll` 逐个核对**可见性**，改动前请重新核对）：

| 成员 | 可见性 |
|---|---|
| `TerrainComp.m_levelDelta / m_smoothDelta / m_modifiedHeight / m_paintMask / m_modifiedPaint / m_operations / m_lastOpPoint / m_lastOpRadius` | private → `AccessTools.Field`（启动地形段时缺任何一个即整段放弃并点名） |
| `TerrainComp.Save(bool paintOnly)`、`DoOperation(Vector3 pos, Vector3 rot, TerrainOp.Settings)` | private → `AccessTools.Method`（**完整参数类型**，#22 教训） |
| `Heightmap.m_heights`（`List<float>`，行主序 `z*(w+1)+x`） | private |
| `Heightmap.Poke(int,bool) / VertexMaskToWorld / GetAndCreateTerrainCompiler / HaveQueuedRebuild / IsDistantLod / static GetHeight / c_LevelMaxDelta / m_paintMaskDirt…` | public，直接调 |

**验收**：满权重顶点重算后误差（>0.5m 不写 flag）；占地露缝（地表比该点最低件底低 >0.15m）；
`bp_terrain_dump.csv` 逐顶点 改前/目标/改后（+ 下次起服 `VerifyOnReload` 追加 `reload` 列）→
`tools/bp_terrain_report.py` 出断崖 / 保护圈 / 持久化判据和三联对比图。

初版有 4 处引用/using 层面的编译错误（41 处报错全是级联），已按 issue #2 修复：

| # | 位置 | 修复 |
|---|---|---|
| 1 | csproj | 补 `<Reference Include="UnityEngine">`（**MonoBehaviour 定义在 UnityEngine.dll，不在 CoreModule**） |
| 2 | csproj | 补 `<Reference Include="UnityEngine.PhysicsModule">`（`Physics.OverlapBox`） |
| 3 | csproj | 补 `<Reference Include="assembly_utils">`（ZPackage/Utils） |
| 4 | Plugin.cs | 补 `using BepInEx.Configuration;`（`ConfigEntry<>`） |

注意：编译通过只证明**直接调用的成员**存在；`AccessTools` 反射字符串编译期不检查，仍须运行时验证（见下）。

## 代码来源标注（Plugin.cs 里逐段标了）

- `✓DOC` —— 文档里有逐字代码，忠实誊录（Activate patch、Support Lock、顶点公式、增量 delta 公式、
  创建时序、Reconcile keying、Save 反射、OverlapBox+0.15、必死件判据…）
- `◐DOC` —— 文档描述了机制/公式，代码结构按描述补全（FreezeWear、地形遍历、锚点闭环、编排协程）
- `REF` —— 纯推断的胶水（配置声明、layer mask、协程时序常量、反射助手签名）——**最可能出错的部分**

## 部署前必做的事

### 1. 改工程引用路径：csproj 里 7 个 `REPLACE_ME`

完整引用清单（**7 个程序集**，缺任何一个都编译失败；`Managed` = `<服务器>\valheim_server_Data\Managed\`，`Core` = `<服务器>\BepInEx\core\`）：

| 程序集 | 路径 | 缺了会怎样 |
|---|---|---|
| `assembly_valheim` | `Managed\` | ZDO/ZNet/WearNTear 全找不到 |
| `assembly_utils` | `Managed\` | ZPackage/Utils 相关 CS0246 |
| `UnityEngine` | `Managed\` | **77× CS0012「MonoBehaviour 在未引用的程序集中定义」级联** |
| `UnityEngine.CoreModule` | `Managed\` | Vector3/Quaternion/Mathf 找不到 |
| `UnityEngine.PhysicsModule` | `Managed\` | `Physics.OverlapBox`（锚点求解）找不到 |
| `BepInEx` | `Core\` | BaseUnityPlugin/Paths 找不到 |
| `0Harmony` | `Core\` | Harmony/HarmonyPatch 找不到 |

### 2. 已知编译错误 → 根因对照

| 症状 | 根因 |
|---|---|
| 大量 `CS0012: 类型"MonoBehaviour"在未引用的程序集中定义` | 缺 `UnityEngine.dll` 引用（不是代码问题；现代 Unity 拆分里 MonoBehaviour 在 UnityEngine.dll 本体） |
| `CS0246: 未能找到类型或命名空间名"ConfigEntry<>"` | 缺 `using BepInEx.Configuration;`（代码层疏漏，改引用路径覆盖不到） |
| `Physics.OverlapBox` 报找不到 | 缺 `UnityEngine.PhysicsModule` 引用 |

### 3. 逐一核对反射签名

所有 `AccessTools.Field/Method/FieldRefAccess` 的私有字段名/方法名
（`m_support` / `m_objectsByID` / `m_levelDelta` / `m_width` / `GetHeight` / `DelayedSave` / `GetMinSupport`…）
对 `assembly_valheim.dll` 用 ILSpy 核对——用交接文档 §9.2 的 `apiscan` 工具最快。
Terrain 段的签名已按 issue #19 的反射枚举核对（见上表）；可复用的枚举工具 = `apipeek`
（`ALL:<类型名>` / `FIELD:<类型>.<字段>` 两种模式，加载 `valheim_server_Data/Managed/assembly_valheim.dll`
打印全部方法/字段签名含泛型参数与 `[static]` 标记）。
⚠️ 方法论提醒（PR #1 实测）：**不要用字符串搜索校验 .NET 私有成员是否存在**——ECMA-335 的
`#Strings` 堆有后缀共享压缩，`SetPosition`/`LoadWorld`/`FixedUpdate` 都搜得到却实际存在/反之亦然；
直接调用的成员看编译结果，反射字符串只能运行时验证。

### 4. 核对 layer mask

`MeasureSinkToTerrain` / `IsMustDie` 里的 `LayerMask.GetMask(...)` 层名
要对照 `WearNTear.UpdateSupport` 反编译里用的实际 mask（piece/Default/static_solid/Default_small/terrain）。

## ⚠️ GUID 与 cfg（issue #9，部署前必读）

BepInEx 按 **GUID** 决定 cfg 路径（`BepInEx/config/<GUID>.cfg`）。**本骨架 GUID 为
`com.world.bpbuild`，与原版不同（脱敏改名），旧 cfg 不通用**——直接部署会新建一份全默认值的
cfg，你调好的配置被完全绕开（实测症状：日志落点 = 代码默认值而非 cfg 值）。

| 想用什么配置 | 怎么做 |
|---|---|
| 原版那份调好的 cfg | 把 `Plugin.cs` 的 `[BepInPlugin("...")]` GUID 改回原版值重编译；或把 cfg 文件改名为 `com.world.bpbuild.cfg` 并逐键核对 |
| `bp_pipeline.py install` 生成的 cfg | 默认写 `com.world.bpbuild.cfg`；部署真版插件时传 `--cfg-guid <真版GUID>` |

**YOffset 的地位（issue #8/#12）**：全新建路径锚点自动求解正常时保持 0；**若日志出现
「[锚点] 求解失败 / 自动求解未生效」，YOffset 必须手填**（0 = 贴地硬边界），cfg 注释里已带
此提示。**对账补建路径（bp_done 已存在）不跑锚点**——只重建缺失件，整体位移会撕裂建筑，
该路径下 YOffset 就是唯一的高度来源。

## 构建（在配好引用的 Windows 机器上）

```bash
cd tools/xibpbuilder_reference
dotnet build XiBpBuilder.csproj -c Release
# 产物 bin/Release/XiBpBuilder.dll → 停服后 cp 到 <服务器>\BepInEx\plugins\
```

## 七段机制速查（详见机制详解文档）

| 段 | 机制 | 关键点 |
|---|---|---|
| 挂载 | `ZNet.ServerLoadWorld` postfix | 不是 `LoadWorld`（坑 A：新世界不走它） |
| 0 Activate | `Game.FixedUpdate` postfix 覆盖 refPos | **总开关**，服务端认领 ~96m 内持久 ZDO |
| 1 Freeze | `WearNTear.UpdateWear` prefix return false | 手术窗口，防地基先塌 |
| 2 Cleanup | `ZDO.SetPosition(6000,-400,6000)` | 搬走不删除；等激活后扫；清非持久；保护圈 |
| 3 Terrain | PlanBuild 式：只改 TerrainComp delta → ClaimOwnership → Save(false) → Poke(0,false) | **建造之后**按件真实碰撞体逐点整地；只改占地 + 自适应过渡带；保护圈不碰；±8m 超限整段放弃 |
| 4 Build | `CreateNewZDO`+`SetPrefab`+`SetPosition/Rotation`+`SetOwner(0)` | 当场校验 `GetPrefab()==hash`；按 py 低→高 |
| 5 Anchor | 逐件 `OverlapBox` 测 d_i → 必死件判据 → 整体位移 | 判据是必死件不是锚点数；闭环≤3 轮；**整过地时跳过** |
| 6 Reconcile | 反射 `m_objectsByID`，key=hash\|x\|y\|z(×4量化) | 拿不到就放弃，宁缺勿重 |
| 7 Save | 反射 `ZNet.DelayedSave(true)` | `SaveWorldAndPlayerProfiles` 会 NRE |

## 与 skill 流水线的关系

这份代码 = 可行性调研里七段流水线的**第 4~5 段（部署执行器 + headless 落地）的执行体**。
skill 负责它前后的纯离线部分：`bp_parse.py`（解析/预检）→ 备份 → 生成 cfg → 起服盯日志 →
离线对账（`bp_reconcile.py`）→ 删 BepInEx 四件套 → 出报告。
**该编排已入库**：`tools/bp_pipeline.py` 一条命令串起七段（preflight/autosite/backup/install/run/verify/
teardown/report，断点续跑、dry-run、铁律内建）；对账用 `tools/bp_reconcile.py`（坑 C.2 双匹配）。
