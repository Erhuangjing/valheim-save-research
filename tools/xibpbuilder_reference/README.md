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
第三轮（#19 Terrain 段 3 处 API 签名 + 重建链调用对象）与 #22（`Poke` 参数 + `TryInvoke` 类型匹配）之后，
2026-09-22 按同一口径（只替换 `REPLACE_ME`）重新编译：0 警告 0 错误。签名依据见下「Terrain 段：反射实测 API」；
`Terrain.Enabled=true + Carve=true` 的实机结果见 issue #22。
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
| Terrain | 空桩已补真实实现（API 见下节；`m_heights` 直写前先做布局往返自检）；取 0 个 heightmap、改 0 个顶点、布局自检失败、重建链缺方法，**四种情况都不写 `terrain_done`** | #14 / #19 |
| 判掉件口径 | 清点 mark ZDO（不用总记录数——会混入掉落物/自然物）；支撑体检输出值域与越界计数（防「反射拿错值」被当真没问题） | #16 |

### Terrain 段：反射实测 API（issue #19，改代码前先看这张表）

成员名/签名来自对 `assembly_valheim.dll` 的**反射枚举**（发 issue 方的 `apipeek`，绕开 `#Strings`
堆后缀压缩造成的字符串搜索假阴性），不是文档推断。**字符串搜索校验不了这些**（见下「逐一核对反射签名」）。

| 用途 | 实测签名 | 踩过的坑 |
|---|---|---|
| 取附近 heightmap | `static void Heightmap.FindHeightmap(Vector3 point, float radius, List<Heightmap> out)`；全量：`static List<Heightmap> Heightmap.GetAllHeightmaps()` | ❌ `Heightmap.GetAllInstances()` **不存在**——那是 `get_Instances()`（`List<IMonoUpdater>`）。本次两条都用：半径筛 + 筛不到时全量兜底 |
| 取 TerrainComp | `TerrainComp Heightmap.GetAndCreateTerrainCompiler()`（实例方法，缺失时创建）；`static TerrainComp TerrainComp.FindTerrainCompiler(Vector3 worldPos)`（只返回已存在的） | ❌ `ZoneSystem.GetZone` 是**静态**且返回 `Vector2s`（zone id），`FindTerrainCompiler` 要的是**世界坐标**——原写法既有 CS0176 又有 CS1503 |
| 高度数组 | `List<float> Heightmap.m_heights`（`(width+1)²` 行主序，`index = z*(width+1)+x`） | ❌ 原写法 `(float[])…` 编译能过、运行必抛 `InvalidCastException` |
| 重建链 | `TerrainComp.Save(bool)` + **Heightmap** 侧 `ApplyModifiers() / Poke(int delayed, bool paintOnly) / UpdateCornerDepths() / RebuildCollisionMesh() / RebuildRenderMesh()` | ❌ 原写法把 `ApplyModifiers` 调在 TerrainComp 上，而 `m?.Invoke` 会**静默吞掉**「方法不存在」→ 链断一环不报错。现在缺方法记名 + 拒绝写 `terrain_done`。❌ issue #22：`Poke` **带 2 参**；反射取方法必须传**完整**参数类型数组（无参传空数组，不是 `null`——`null` = 任取同名重载，Invoke 时抛 `TargetParameterCountException`） |
| 同一个高度 | `float Heightmap.GetHeight(int x, int z)`（**参数序 (x,z)**；文档实证：`hmap.GetHeight(j, i)`） | 同名重载可能不止一个 → 反射取方法时显式指定 `(int,int)` |

**`m_heights` 布局的运行时自检**：`(width+1)² 行主序` 与 `index 算法` 一直是文档推断、从未实证，
所以 `HeightLayoutVerified()` 在写入前把一个顶点挪 1 米再用游戏自己的 `GetHeight` 回读、再复原，
两个方向都对上才继续；对不上就报 Error 并放弃本段（不写 `terrain_done`）。
探测点取 **x≠z**（(1,2)）—— 对称点在「行主序」与「转置」下算出同一个 index，检不出 x/z 转置。
`DryRun=true` 现在**真的不动地形**（原来照样写 `m_heights`/delta），且跳过重建链。

**仍未做的一条**：文档链首步「取 ZNetView 所有权」没实现——本骨架跑在**服务器端**，服务器本来就是
owner，暂按不需要处理；若将来多人联机下出现地形不同步/不裂，再补这一步。
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
| 3 Terrain | 双写 `m_heights`+`m_levelDelta`，增量公式，±8m | 无 +0.5；重建调用链（`ApplyModifiers` 在 Heightmap 侧）；`m_heights` 是 `List<float>` + 布局自检；**delta 丢失风险→首选天然平地** |
| 4 Build | `CreateNewZDO`+`SetPrefab`+`SetPosition/Rotation`+`SetOwner(0)` | 当场校验 `GetPrefab()==hash`；按 py 低→高 |
| 5 Anchor | 逐件 `OverlapBox` 测 d_i → 必死件判据 → 整体位移 | 判据是必死件不是锚点数；闭环≤3 轮 |
| 6 Reconcile | 反射 `m_objectsByID`，key=hash\|x\|y\|z(×4量化) | 拿不到就放弃，宁缺勿重 |
| 7 Save | 反射 `ZNet.DelayedSave(true)` | `SaveWorldAndPlayerProfiles` 会 NRE |

## 与 skill 流水线的关系

这份代码 = 可行性调研里七段流水线的**第 4~5 段（部署执行器 + headless 落地）的执行体**。
skill 负责它前后的纯离线部分：`bp_parse.py`（解析/预检）→ 备份 → 生成 cfg → 起服盯日志 →
离线对账（`bp_reconcile.py`）→ 删 BepInEx 四件套 → 出报告。
**该编排已入库**：`tools/bp_pipeline.py` 一条命令串起七段（preflight/autosite/backup/install/run/verify/
teardown/report，断点续跑、dry-run、铁律内建）；对账用 `tools/bp_reconcile.py`（坑 C.2 双匹配）。
