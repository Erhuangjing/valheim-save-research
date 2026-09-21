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
⚠️ 方法论提醒（PR #1 实测）：**不要用字符串搜索校验 .NET 私有成员是否存在**——ECMA-335 的
`#Strings` 堆有后缀共享压缩，`SetPosition`/`LoadWorld`/`FixedUpdate` 都搜得到却实际存在/反之亦然；
直接调用的成员看编译结果，反射字符串只能运行时验证。

### 4. 核对 layer mask

`MeasureSinkToTerrain` / `IsMustDie` 里的 `LayerMask.GetMask(...)` 层名
要对照 `WearNTear.UpdateSupport` 反编译里用的实际 mask（piece/Default/static_solid/Default_small/terrain）。

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
| 3 Terrain | 双写 `m_heights`+`m_levelDelta`，增量公式，±8m | 无 +0.5；重建调用链；**delta 丢失风险→首选天然平地** |
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
