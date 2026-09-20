# XiBpBuilder 参考重建（REFERENCE ONLY）

这是《Valheim_服务端实时构建机制_详解》的配套代码骨架，回答「启动 dedicated server 时怎么做到
处理地形 + 清除障碍 + 构建房屋」。

## ⚠️ 这是什么、不是什么

| | |
|---|---|
| ✅ 是 | 从本仓库 4 份实验文档里散落的**真实 patch 代码**重建的**机制骨架**，把七段启动时序拼成可读、可对照的实现 |
| ❌ 不是 | 原版 XiBpBuilder v0.31.1（约 1500 行，在用户机器 `E:\wkbdfile\2026-08-17-16-03-22\bpbuild\Plugin.cs`） |
| ❌ 不是 | 编译验证过的成品——本沙箱无 Valheim 程序集 / BepInEx / dotnet，**从未编译** |

## 代码来源标注（Plugin.cs 里逐段标了）

- `✓DOC` —— 文档里有逐字代码，忠实誊录（Activate patch、Support Lock、顶点公式、增量 delta 公式、
  创建时序、Reconcile keying、Save 反射、OverlapBox+0.15、必死件判据…）
- `◐DOC` —— 文档描述了机制/公式，代码结构按描述补全（FreezeWear、地形遍历、锚点闭环、编排协程）
- `REF` —— 纯推断的胶水（配置声明、layer mask、协程时序常量、反射助手签名）——**最可能出错的部分**

## 部署前必须做的三件事

1. **改工程引用路径**：`XiBpBuilder.csproj` 里三个 `REPLACE_ME`（游戏程序集 / BepInEx 核心 / UnityEngine 模块）。
2. **逐一核对反射签名**：所有 `AccessTools.Field/Method/FieldRefAccess` 的私有字段名/方法名
   （`m_support` / `m_objectsByID` / `m_levelDelta` / `m_width` / `GetHeight` / `DelayedSave` / `GetMinSupport`…）
   必须对 `assembly_valheim.dll` 用 ILSpy 核对——用交接文档 §9.2 的 `apiscan` 工具最快。
3. **核对 layer mask**：`MeasureSinkToTerrain` / `IsMustDie` 里的 `LayerMask.GetMask(...)` 层名
   要对照 `WearNTear.UpdateSupport` 反编译里用的实际 mask（piece/Default/static_solid/Default_small/terrain）。

## 构建（在配好引用的 Windows 机器上）

```bash
cd bpbuild_reference
"/c/Program Files/dotnet/dotnet" build XiBpBuilder.csproj -c Release -v q --nologo
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
离线对账（`zdo_lib.py`）→ 删 BepInEx 四件套 → 出报告。
