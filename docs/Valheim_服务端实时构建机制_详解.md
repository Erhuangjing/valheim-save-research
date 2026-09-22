# Valheim 专用服务端「实时构建」机制详解

> 日期：2026-09-21
> 问题：**启动 dedicated server 的时候，怎么做到「处理地形 + 清除障碍 + 构建房屋」？机制是什么？**
> 定位：这是《Valheim_蓝图无mod落地_skill可行性调研》里**路线 5（唯一实测走通）**的展开。
> 依据：本仓库 4 份实验文档里散落的真实 patch 代码，本文把它们拼成一条完整启动时序。
> 配套：`tools/xibpbuilder_reference/`（从实验日志重建的参考 Plugin.cs，**未经编译验证**，见其 README 免责）。

---

## 0. 一图流程：启动时序

```
valheim_server.exe 启动（-savedir 指向目标世界）
        │
        ▼
  ZNet.ServerLoadWorld()  ← Harmony Postfix 挂这里（不是 LoadWorld，见 §2 坑 A）
        │  s_worldLoaded = true
        ▼
  ┌─────────────────────────── 编排协程（一条主协程串起五段）────────────────────────────┐
  │                                                                                      │
  │  段0 [Activate]  Game.FixedUpdate postfix 每物理帧把 refPos 覆盖到工地坐标            │
  │        └─► 服务端自己认领落点 ~96m 内的持久 ZDO（ReleaseNearbyZDOS 的 sessionID 分支）│
  │            → 物件被实例化 + WearNTear 开始跑 UpdateSupport                            │
  │                                                                                      │
  │  段1 [Freeze]    激活放行前先冻磨损：WearNTear.UpdateWear prefix 直接 return false    │
  │        └─► 「手术窗口」：件活过来、被服务端拥有、但暂不参与销毁判定（见 §7）           │
  │                                                                                      │
  │  段2 [Cleanup]   清落点障碍：等 zone 长齐 → SetPosition(6000,-400,6000) 搬走          │
  │        └─► 石头/树/废弃建筑，带保护圈（玩家主宅绝不碰）                               │
  │                                                                                      │
  │  段3 [Terrain]   处理地形：整平平台 /（可选）开挖地窖                                 │
  │        └─► 双写 Heightmap.m_heights + TerrainComp.m_levelDelta（增量公式，±8m clamp） │
  │            → Save→ApplyModifiers→Poke→UpdateCornerDepths→RebuildCollision/RenderMesh  │
  │                                                                                      │
  │  段4 [Build]     构建房屋：逐件 CreateNewZDO + SetPrefab + SetPosition/Rotation       │
  │        └─► 分批（BatchSize 60 / FrameDelay 10），当场校验 GetPrefab()==hash           │
  │                                                                                      │
  │  段5 [Anchor]    锚点自动求解：实测每件 d_i → 必死件判据选档 → 整体位移 → 复测        │
  │        └─► 收敛后解冻磨损（Freeze 关闭）→ 真实 UpdateSupport 跑起来，验证不塌         │
  │                                                                                      │
  │  段6 [Reconcile] 对账补齐：读 ZDOMan.m_objectsByID，缺的件按 py 低→高补建             │
  │                                                                                      │
  │  段7 [Save]      反射调 ZNet.DelayedSave(true) 协程落盘（SaveWorldAndPlayerProfiles NRE）│
  └──────────────────────────────────────────────────────────────────────────────────────┘
        │  写 bp_done.flag / cleanup_done.flag / terrain_done.flag
        ▼
  停服 → 删 BepInEx 四件套 → 纯原版服务端，建筑是 100% 原生世界数据
```

**关键认知**：这七段里，段 0（激活）是**总开关**——没有它，服务端把 refPos 写死在世界外
`(1000000,0,1000000)`，一个物件都不实例化、`WearNTear` 一律返回满支撑直接 return，
地形/清理/锚点全都无从测量。整条链能成立，全靠这一个 `Game.FixedUpdate` postfix。

---

## 1. 为什么「启动时实时构建」成立，而「离线改档」不成立

| | 离线改档（路线 1） | 启动时实时构建（路线 5） |
|---|---|---|
| 谁写 ZDO | 你的 Python 直接拼字节塞进 chunk | **游戏自己的 `ZDOMan.CreateNewZDO`** |
| 记录长度/边界 | 你负责，错 1 字节全崩 | 游戏负责，天然合法 |
| 地形 delta | 记录流里交织的二进制块，无 prefab 头，离线定位即高危 | 游戏 `TerrainCompiler` 官方 API 写，100% 兼容 |
| 支撑存活（门 2） | **无法离线计算**，玩家一走近就塌 | 服务端实例化后**真实跑 `UpdateSupport`**，当场验证 |
| 实测结果 | 批量必崩（20 组对照） | 4625/4625 零丢失 |

一句话：路线 5 把「写数据」和「验存活」都交回游戏进程，用**服务端自己当那个"假人"**去认领并模拟，
所以它同时过了门 1（写入）和门 2（存活）——这是纯离线永远做不到的。

---

## 2. 挂载点：`ServerLoadWorld` Postfix（坑 A）

```csharp
[HarmonyPatch(typeof(ZNet), "ServerLoadWorld")]
static void Postfix() {
    s_worldLoaded = true;
    instance.StartCoroutine(instance.Orchestrate());   // 主编排协程
}
```

**为什么是 `ServerLoadWorld` 不是 `LoadWorld`**（激活报告坑 A，一手反编译）：

```csharp
// ZNet.ServerLoadWorld() 内部：
if (m_world.IsChunkedSave()) LoadWorld(); else LoadOldWorld();
```

`m_chunkedSave` 是从 `_main.<N>.fwl2` 读的布尔。**全新世界第一次加载它是 false → 走 `LoadOldWorld()`，
`LoadWorld()` 根本不被调用**。挂 `LoadWorld` 会在新世界上静默不触发（日志只有 `Load world:` 没有 `ZNet.LoadWorld:`）。
挂共同入口 `ServerLoadWorld` 两条路径都能触发。

> 早期版本文档里写的是 `[HarmonyPatch(typeof(ZNet), "LoadWorld")]`——那是坑 A 修正**之前**的挂点，
> 因为测试世界都是从 WORLD 复制的（内部名仍是 WORLD、走 chunked 分支）才没暴露。正式版必须用 `ServerLoadWorld`。

---

## 3. 段 0 · Activate：把服务端 refPos 指到工地（总开关）

```csharp
[HarmonyPatch(typeof(Game), "FixedUpdate")]
public static class PatchActivate {
    static void Postfix() {
        if (!CfgActOn.Value || !s_worldLoaded) return;
        var znet = ZNet.instance;
        if (znet == null || !znet.IsServer()) return;
        if (ZNet.GetConnectionStatus() != ZNet.ConnectionStatus.Connected) return;
        znet.SetReferencePosition(new Vector3(CfgActX.Value, CfgActY.Value, CfgActZ.Value));
    }
}
```

**就这一个 postfix**——没有假人、没有网络 peer、没有额外所有权代码。原理（`ZDOMan.ReleaseZDOS` 929 行）：

```csharp
ReleaseNearbyZDOS(ZNet.instance.GetReferencePosition(), m_sessionID);   // ← 服务端给自己发所有权
foreach (ZDOPeer peer in m_peers) ReleaseNearbyZDOS(peer.m_peer.m_refPos, peer.m_peer.m_uid);
```

refPos 一旦指回工地，`IsInPeerActiveArea` 里 `uid == m_sessionID` 的分支成立，
服务端自然认领落点 ~96m（1.5 zone）内的**持久** ZDO。连锁后果（全部原版代码自动跑起来）：

| 环节 | 效果 |
|---|---|
| `ZNetScene.CreateDestroyObjects()` | 物件被实例化成 GameObject |
| `WearNTear.UpdateWear/UpdateSupport` | 支撑计算真的在跑（服务端是 owner） |
| 怪物 AI / 驯化 / 繁殖 | 一并激活（副作用，本文不展开） |

**实测证据**（激活报告 §1.3）：`本进程拥有 = 我们的件 = 4612（100%）`——证明服务端不是"看着"，是真的成了 owner。

⚠️ **激活是双刃剑**：它让 `UpdateSupport` 跑起来的同时，也让**没接地的件当帧满血销毁**。
所以必须在激活生效**之前**先冻磨损（段 1），否则地基那层会在你量几何之前就掉光。这就是"手术窗口"的由来（§7）。

---

## 4. 段 2 · Cleanup：清除障碍

**核心手法：搬走，不是删除**（交接文档坑 #6）——

```csharp
// DestroyZDO 在服务端走异步队列 m_destroySendList，实际不生效（连扫 15 轮命中同一批）
zdo.SetPosition(6000f, -400f, 6000f);   // 搬到世界角落，第二轮扫描命中 0
```

清理协程要点（收官报告 §3 修掉的三个坑，全是实测教训）：

| 要点 | 配置 | 为什么 |
|---|---|---|
| **等激活后再扫** | `WaitActivate=true`，激活生效后 +24s | 清理跑在 zone 加载前的话，自然物还没被 zone 系统生成，扫 83145 个 ZDO 只命中 43 个 |
| **必须清非持久对象** | `OnlyPersistent=false` | 自然物（石头/树）**绝大多数不是持久 ZDO**，`if(!Persistent) continue` 会直接跳过它们 |
| **区域兜底、别信名字白名单** | `ClearAllInArea=true` | 本地自然物不在 ZNetScene 网络 prefab 表里，`GetPrefab(hash)` 返回 null → 名字为空 → 白名单永远匹配不上 |
| **保护圈** | `ProtectX/Z + ProtectRadius` | 玩家主宅、墓碑、传送门、本插件的件永久保护 |
| 多轮 | 轮次上限 60 | zone 会持续分批生成新对象，15 轮清不完 |

---

## 5. 段 3 · Terrain：处理地形（最容易写错的一段）

### 5.1 权威顶点坐标公式（交接文档 §3.4，参照 EarthWorks `GetWorldVertex`）

```csharp
float halfSize = hmap.m_width * hmap.m_scale * 0.5f;
float worldX   = hpos.x - halfSize + j * m_scale;   // ★ 没有 +0.5f（坑 #5：多加就整块偏移）
float worldZ   = hpos.z - halfSize + i * m_scale;
int   index    = i * (m_width + 1) + j;
```

每块 64×64m、65×65 顶点、`m_scale=1.0`（顶点精度 1m）；**边界顶点被相邻 Heightmap 共享 → 相邻块一起改**。

### 5.2 增量 delta 公式（必须用增量，不能直接写目标高度）

```csharp
// delta_new = delta_old + smooth_old + (目标高度 − 当前高度)
float curLocal = hmap.GetHeight(j, i);              // 注意参数顺序 (x, z)
float tgtLocal = targetWorldY - hpos.y;
float req = levelDelta[index] + smoothDelta[index] + tgtLocal - curLocal;
levelDelta[index]   = Mathf.Clamp(req, -8f, 8f);    // 游戏硬限 c_LevelMaxDelta = 8
smoothDelta[index]  = 0f;
modifiedHeight[index] = true;
```

### 5.3 ★ 两份高度数据都要写（地形重塑完成说明 §三，最大的坑）

```
Heightmap.m_heights      ← 游戏实际使用的高度（只写 delta 的话，地面纹丝不动 = "没生效"）
                           ⚠ 类型是 List<float>（不是 float[]，issue #19③：显式转 float[] 编译能过、运行抛 InvalidCastException）
TerrainComp.m_levelDelta ← 相对原始高度的偏移，供存档序列化持久化
```

**只改 delta → 表面看不到效果；只改 heights → 存档不持久、重载被 ApplyModifiers 覆盖。两个都写。**

### 5.4 写完的重建调用链（顺序不能乱）

```
取 ZNetView 所有权 → TerrainComp.Save(false) → ApplyModifiers() → Poke(0, false)
→ UpdateCornerDepths() → RebuildCollisionMesh() → RebuildRenderMesh()
```

⚠ **调用对象别搞错（issue #19，反射实测）**：这条链上只有 `Save(bool)` 在 **TerrainComp** 侧，
`ApplyModifiers` / `Poke` / `UpdateCornerDepths` / `RebuildCollisionMesh` / `RebuildRenderMesh`
**全在 Heightmap 侧**。参考骨架原来把 `ApplyModifiers` 调在 TerrainComp 上，而
`AccessTools.Method` 找不到方法时 `m?.Invoke` 会**静默跳过** → 链断一环却不报错。
写反射调用时务必让「方法不存在」变成一条显式日志。

同理，取 heightmap / TerrainComp 的正确入口是（同为反射实测）：
`Heightmap.FindHeightmap(Vector3, float, List<Heightmap>)` 或 `Heightmap.GetAllHeightmaps()`（**没有** `GetAllInstances()`）、
`Heightmap.GetAndCreateTerrainCompiler()`（会创建缺失的 TerrainComp）——
`ZoneSystem.GetZone` 是静态且返回 zone id，不能喂给 `FindTerrainCompiler`（它要世界坐标）。

### 5.5 ±8m 硬限 + delta 丢失事故（必须知道的风险）

- **±8m**：delta 是相对原始地形的，clamp 在 ±8m。地窖挖 5~8m、若原始地形本身高出平台 2m，
  `−8` 先被吃满、角落挖不到底（实测差 0.26m，可接受）。
- **⚠️ 09-16 delta 丢失事故**（地形delta存档侧核查 §九）：进程内写的 delta，在玩家**脱离活跃区、
  zone 卸载重载**后丢失 → 地表复原 → 房子从 1658 件塌到 197 件。**这是路线 5 目前最大的未决风险。**
  缓解：**优先选天然平地落点、`[Terrain]` 零改动或最小改动**，靠段 5 锚点下沉去适配现有地形，
  而不是改地形去适配蓝图——不写 delta 就没有丢失风险。

---

## 6. 段 4 · Build：构建房屋

### 6.1 单件创建时序（交接文档坑 #2，一字不能少）

```csharp
var zdo = ZDOMan.instance.CreateNewZDO(worldPos, prefabHash);
zdo.SetPrefab(prefabHash);          // ★ 不显式调 → hash 全 0 → 房子不可见
zdo.SetPosition(worldPos);
zdo.SetRotation(quat);              // 建议全四元数，不要只传 yaw（斜面屋顶会歪）
zdo.Persistent = true;              // 持久物件恒置位（flags bit8）
zdo.SetOwner(0L);                   // 无主，等激活时段 0 认领
// 当场校验：
if (zdo.GetPrefab() != prefabHash) LogFail();   // prefab 校验失败 0 次才算成功
```

### 6.2 坐标换算（交接文档 §1.3）

```
worldX = OriginX + (bpX − blueprintCenterX)
worldZ = OriginZ + (bpZ − blueprintCenterZ)
worldY = H_base  + bpY + YOffset        // H_base = 地面层 py 对应的世界 Y
```

`H_base` 由 GroundLayerPy 反推（`bp_parse.py` 已能自动算 GroundLayerPy，见解析器预检报告）。

### 6.3 分批与件清单

- `BatchSize=60 / FrameDelay=10`：每批 60 件、隔 10 帧，越大越快但越卡；1805 件约 10~50s，4625 件约 2 分钟。
- 件清单格式 `name|hash|x|y|z|yaw`（`bp_parse.py --txt` 产出）。
  **保真升级**：txt 只存 yaw，斜面屋顶（45°/26°）可能歪；建议执行器改吃 `bp_parse.py --json` 的全四元数。

---

## 7. 段 5 · Anchor：锚点自动求解（手术窗口，最精妙的一段）

### 7.1 鸡生蛋死结

- 件必须先**被实例化**才能量几何 → 实例化要求 refPos 指过来（激活）；
- 可一旦激活，`UpdateWear` 立刻开算 → 地基那层先掉 29 件，几何从此量不准。

**解法：激活放行前先冻磨损**（段 1 的 `WearNTear.UpdateWear` prefix `return false`），
让件活过来、被拥有、但不参与销毁判定。求解 + 整体位移都在这个"零销毁干扰"窗口里做完，再解冻。

### 7.2 逐件求 d_i（完全复刻游戏判定，收官报告 §1.2）

```csharp
// d_i = 这一件下沉多少米，包围盒才碰到地形
// 复刻 WearNTear.UpdateSupport 845/870/902 行：
Physics.OverlapBox(bounds.center - Vector3.up * d, bounds.size / 2f + 0.15f, terrainMask);
//                                              ↑ 那个 0.15 就是 WearNTear 自己加的余量 = 硬边界宽度
```

把全部 `d_i` 排序 → 得到完整的 `anchorCount(d)` 累积曲线，一步知道"下沉多少拿多少锚点"。

### 7.3 判据是「必死件数」不是「锚点数」（收官报告 §8，反直觉但决定性）

地形碰撞体是**高度场薄壳**，每件有一段"接地窗口"（上方不接地 / 与壳相交接地 / 沉到壳下又不接地）。
所以锚点数**非单调**（下沉 1.4m 拿 604 锚点，反而比 0.4m 的 221 更差）。真正的判据是**必死件数**：

```
必死件 = 盒内既无地形、又无别的构件、又无其它物体  → UpdateSupport 里 support 归零那条路 → 当帧满血销毁
```

| 下沉量 | 锚点数 | **必死件数** |
|---|---|---|
| 0.0m | 33 | **0** |
| 0.4m | 221 | 133 |
| 1.4m | 604（峰值） | 526（最多）← 选锚点峰值恰好最惨 |

选档规则：**必死件最少的档，并列时取下沉更深/锚点更多的**（R23：`>` 改 `>=`）。

### 7.4 闭环收敛（预测≠实测，最多 3 轮）

单发求解会偏差（预测 298 锚点、实测只有 256，差 42 件）。改成实测驱动：
每轮以**真实测出的锚点/必死件**为准，不达标就基于**当前实际位置**再求一轮。

```
落地完成 → 等件实例化(~2s) → 等清理完成 → 实测求解 → 整体位移 → 复测 → 解冻
实测：第1轮 必死0/锚点41 → 下沉0.20m；第2轮 必死0/锚点221 → 下沉0.20m；第3轮 必死0 → 停
★ 累计下沉 0.45m，锚点 221，全程 2.3s → 离线复核 4625/4625 存活
```

解冻后，真实 `UpdateSupport` 跑起来，因为已经咬住地面 → 不塌。

---

## 8. 段 5b · Support Lock（可选兜底）+ 段 6 · Reconcile（对账补齐）

### 8.1 支撑锁定（社区成熟做法，锚点求解成功时**不需要**开）

```csharp
[HarmonyPatch(typeof(WearNTear), "UpdateSupport")]
static bool Prefix(WearNTear __instance) {
    var nv = __instance.GetComponent<ZNetView>();
    if (nv == null || !nv.IsValid()) return true;
    if (nv.GetZDO().GetInt("xiabp", 0) != 1) return true;   // 只锁我们自己落的件
    RefSupport(__instance) = MaxOf(__instance);             // FieldRefAccess<WearNTear,float>("m_support")
    nv.GetZDO().Set("support", MaxOf(__instance));
    return false;                                           // 跳过原版计算
}
```

> ⚠️ **2026-09-22 实测更正（issue #13）**：上面的收官结论**错了**。锚点求解成功（必死=0）
> 只保证「支撑几何」成立，阻止不了解冻后**原生磨损系统**的销毁（调用栈：
> `WearNTearUpdater.UpdateWearNTear → UpdateWear → ApplyDamage → Destroy → ZNetScene.Destroy`）。
> 5 蓝图 8 轮 A/B 实测：`Support.Enabled=false` 掉件 **3.7%~15.9%**（锚点成功的几轮照样掉），
> `=true` **±0**、磨损销毁事件 0 条。**默认必须开**；锁仍只作用于 mark==1 的本工具件。
> 「靠锚点是真稳」对**支撑**成立，对**磨损**不成立——两者是不同的销毁路径。

### 8.2 对账补齐（把塌掉/漏掉的件补回来，交接文档 §9.4）

落地一次后 `bp_done.flag` 阻止重建 → 缺的件会永久缺失。**不要删 flag 重跑（会变双份）**，而是对账：

```csharp
// 用 ZDOMan 权威数据，不依赖 zone 是否实例化成 GameObject
var dict = AccessTools.Field(typeof(ZDOMan), "m_objectsByID").GetValue(ZDOMan.instance) as IDictionary;
foreach (DictionaryEntry e in dict) {
    var z = (ZDO)e.Value; int h = z.GetPrefab(); Vector3 p = z.GetPosition();
    if (超出落点 45m) continue;
    have.Add(h + "|" + round(p.x*4) + "|" + round(p.y*4) + "|" + round(p.z*4));  // 0.25m 量化，y 也要
}
// 用同一套公式（含同一 groundBase）算蓝图每件期望 key，不在 have 里的 = 缺失 → 按 py 低→高补建
```

要点：拿不到 `m_objectsByID` 就**主动放弃，宁缺勿重**；y 也量化（同 (x,z) 常叠放多件）。

---

## 9. 段 7 · Save + 收尾

```csharp
// SaveWorldAndPlayerProfiles() 走 RPC 路径会 NRE，不能用
var m = AccessTools.Method(typeof(ZNet), "DelayedSave");
instance.StartCoroutine((IEnumerator)m.Invoke(ZNet.instance, new object[]{ true }));
```

**不需要人在游戏里**：服务端无玩家时就能创建 + 保存成功（实测 ZDO 83,136 → 84,941，chunk 归入正确 zone）。

收尾（回无 mod 状态）：停服 → 删 `winhttp.dll` + `doorstop_config.ini` + `doorstop_libs/` + `BepInEx/` 四件套
→ 纯原版服务端。建筑是原生 ZDO，永久留存，好友零安装可见。

---

## 10. Flag 与重跑语义（血泪坑）

| 段 | flag | 重跑方式 |
|---|---|---|
| Cleanup | `cleanup_done.flag` | 删对应 flag |
| Terrain | `terrain_done.flag` | 删对应 flag |
| Build | `bp_done.flag` | 删对应 flag（但对已落地过的世界用 Reconcile，别删） |

**铁律：`Force=false` 恒定；要重跑某段就删那一段的 flag。**
`Force=true` 会把三段全重跑 → 重复创建（实测落地 3610 件 = 双份，坑 #8）。

---

## 11. Harmony patch 点全表（本文引用的所有补丁，一表看清）

| # | 目标 | 类型 | 作用 | 段 | 来源 |
|---|---|---|---|---|---|
| 1 | `ZNet.ServerLoadWorld` | Postfix | 启动编排协程（坑 A：不是 LoadWorld） | 挂载 | 激活报告坑A |
| 2 | `Game.FixedUpdate` | Postfix | 覆盖 refPos 到工地 = 激活总开关 | 段0 | 激活报告 §1.2 |
| 3 | `WearNTear.UpdateWear` | Prefix | 手术窗口冻磨损（return false） | 段1 | usagi最终结论 §6 |
| 4 | `WearNTear.UpdateSupport` | Prefix | 支撑锁定（可选兜底） | 段5b | 交接文档 §3.5 |

其余段（Cleanup/Terrain/Build/Anchor/Reconcile/Save）不是 patch，是**编排协程里调游戏 API**：
`ZDOMan.CreateNewZDO` / `TerrainCompiler`+`Heightmap` / `Physics.OverlapBox` / `ZDO.SetPosition` / 反射 `DelayedSave`。

---

## 12. 已知风险与铁律（部署前必读）

**风险**

1. **地形 delta 丢失**（09-16 事故）：改地形后玩家脱离活跃区、zone 卸载重载可能丢 delta → 塌。
   → 首选天然平地 + 零地形改动 + 锚点下沉适配。
2. **crossplay 不兼容**：BepInEx 通病，落地窗口内关 crossplay，删净后恢复。
3. **版本漂移**：Harmony 签名跟游戏小版本走，每次更新先在副本世界跑一轮对账。
4. **姿态保真**：件清单 yaw 单值会让斜面屋顶歪 → 升级全四元数。

**铁律**（从 13+3 条踩坑清单提炼）

1. 动档前必备份、必停服（DLL 占用 + 写入竞争）。
2. 重跑删对应 flag，永不 `Force=true`。
3. 单次读数不可信，连续采样到稳态 + 带正对照（09-16 初版结论反转的教训）。
4. **判断崩塌看件数时序，不看"支撑不足=0"**（当帧销毁抓不到瞬时态）。
5. 对账必须名字+位置双匹配、y 也量化。
6. 落地前先看锚点数/必死件数，不看就是碰运气。

---

## 附：参考实现

`tools/xibpbuilder_reference/`（本轮从上述实验日志重建）：

- `Plugin.cs` —— 七段编排 + 4 个 Harmony patch 的参考骨架
- `XiBpBuilder.csproj` —— netstandard2.1 工程（7 个程序集引用，见该目录 README）
- `README.md` —— 免责声明 + 构建指引 + 已知编译错误对照

原版真身（约 1500 行）未包含在本仓库；参考骨架已于 2026-09-21 实机编译验证通过
（.NET SDK 9 + DS 1.0 + BepInEx 5，0 警告 0 错误，见 PR #1 实测报告 / issue #2），
并在隔离环境完成端到端实测：**清障→落地→保存 1805/1805 零丢失、prefab 校验失败 0、
坐标公式反推精确吻合**（run/teardown 段）。实测暴露并已修三处运行期问题：
#7 hash 按 uint32 位模式解析（int.Parse 溢出）、#8 锚点求解 null 保护 + 失败显式化、
#9 GUID 变更致 cfg 不通用（见参考 README「GUID 与 cfg」）。
编译通过只覆盖「直接调用的成员」；`AccessTools` 反射字符串编译期不检查，运行时行为仍须实测。
