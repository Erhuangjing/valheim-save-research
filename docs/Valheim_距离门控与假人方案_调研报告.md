# Valheim 距离门控机制 &「假人激活区域」可行性调研

> 调研日期：2026-09-15
> 一手证据：本地专用服务端程序集 `testserver/valheim_server_Data/Managed/assembly_valheim.dll`（ILSpy 反编译，产物在 `decomp/`）
> 社区情报：GitHub / Thunderstore / 官方 FAQ

---

## 0. 结论先行

1. **你的猜测是对的，而且比你想的更极端。** 游戏不是"人物在附近才算"，而是**"有 peer 拥有这个 ZDO 才算"**。所有权（`IsOwner()`）是承重、磨损、驯化、繁殖、怪物 AI 的**统一总开关**。
2. **专用服务端自己什么都不算。** 服务端每物理帧把参考坐标硬编码写成 `(1000000, 0, 1000000)`——一个世界之外的点。所以 vanilla 专用服务端的 active area、物件实例化、承重计算**全部为空**。它只是个 ZDO 数据库 + 消息转发器。
3. **所以"蓝图落地后走进去就塌"不是幻觉，是必然。** 蓝图写入的 ZDO 在无人拥有时不校验承重（`GetSupport()` 直接返回最大值）；第一个玩家走进 96 米内拿到所有权的那一瞬间，`UpdateSupport()` 才开始算，**没接地链条的构件当场按 100% 伤害销毁**。
4. **"注入假人"要分成两种，效果完全相反：**
   - 只注入**网络层假 peer**（有 refPos、不跑模拟）→ 会把区域**冻结**，并且**抢走真实玩家的所有权**，属于负优化。
   - 让**服务端自己成为拥有者并跑模拟** → 这才是"激活"，而且服务端进程本身就带着全套游戏代码，**几乎不需要"假人"这个概念**，改的是 ref pos + 所有权归属。
5. **"有没有 API 能实时读取、控制里面的人"——有，而且已经有人做好了**：`ValheimMCP`（本地 HTTP/MCP 驱动控制台 + 离屏渲染出图）、`ValBridgeServer`（MCP 直接暴露 `place_piece` / `navigate_to_position` / `craft_item` 等原子操作）、`ValheimOne`（服务端 Web 后台 + 实时地图）、`ValheimWebLink`（HTTP + 玩家数据读写模块）。

---

## 1. 代码级证据链（全部来自本地反编译）

### 1.1 总开关：`IsOwner()`

所有权在 `ZDO` 上：`IsOwner()` → `Owner`（本机 session 是否等于该 ZDO 的 owner）；owner 值 `0` 表示无主。

| 系统 | 门控代码 | 含义 |
|---|---|---|
| 建筑承重/磨损 | `WearNTear.UpdateWear()` 第 529 行 `if (m_nview.IsOwner() && ShouldUpdate(time))` | **不是拥有者就不算** |
| 驯化 | `Tameable` 第 399 行 `if (m_nview.IsValid() && m_nview.IsOwner() && !IsTamed() && !IsHungry() ...)` | 驯化计时只在拥有者上跑 |
| 繁殖 | `Tameable` 第 471 / 484 行 `if (m_nview.IsOwner())` | 同上 |
| 怪物 AI | `MonsterAI.UpdateAI()` 第 347 行；第 205 行 `if (!m_nview.IsOwner() ...)` | AI 大脑跑在拥有者的机器上 |

来源：`decomp/WearNTear.decompiled.cs`、`decomp/Tameable.decompiled.cs`、`decomp/MonsterAI.decompiled.cs`

### 1.2 所有权怎么分配：只给 active area 里的 peer

`ZDOMan.ReleaseNearbyZDOS(refPosition, uid)`（`decomp/ZDOMan.decompiled.cs` 第 950–976 行）：

```csharp
if (tempNearObject.GetOwner() == uid) {
    if (!ZNetScene.InActiveArea(position, zone))
        tempNearObject.SetOwner(0L);            // 离开范围 → 释放，交还无主
}
else if ((!tempNearObject.HasOwner() || !IsInPeerActiveArea(position, tempNearObject.GetOwner()))
         && ZNetScene.InActiveArea(position, zone)) {
    tempNearObject.SetOwner(uid);               // 范围内且原主不在 → 归我
}
```

而 active area 的判定（`decomp/ZNetScene.decompiled.cs` 第 390–412 行）：

```csharp
private static bool PointInsideActiveArea(Vector2s zone, Vector3 point) {
    float zoneSize = ZoneSystem.instance.m_zoneSize;   // 64m
    float num = 1.5f;
    if (near == 1) num = 1f;                           // 64m
    bool flag = Utils.ChebyshevDistance(zonePos, point) <= num * zoneSize;
    if (near == 2 && !IsClassic)                        // 112m
        return (zonePos - point).sqrMagnitude < (zoneSize * 1.75f) * (zoneSize * 1.75f);
    return flag;
}
```

**结论：默认所有权半径 ≈ 1.5 个 zone ≈ 96 米**（以观察者所在 zone 中心为基准），某些档位下 112 米或 64 米。这就是"人物在边上一定距离内"的真实数字。

### 1.3 服务端为什么不参与：参考坐标被写死在世界外

`decomp/Game.decompiled.cs` 第 669–677 行：

```csharp
private void FixedUpdate() {
    if (ZNet.m_loadError) { Logout(); ... }
    ZNet.instance.SetReferencePosition(new Vector3(1000000f, 0f, 1000000f));  // ← 每物理帧
}
```

客户端有 `Player` 每帧把它覆盖回自己坐标（`Player.decompiled.cs` 第 709 / 1671 行），**专用服务端没有 local player，所以它永远停在 (1e6, 0, 1e6)**。

这直接导致两件事：

- `ZNetScene.CreateDestroyObjects()`（第 360–368 行）用 `ZNet.instance.GetReferencePosition()` 找物件 → 服务端**一个物件都不实例化**。
- `ZoneSystem.Update()`（第 1181–1190 行）在服务端只对 (1e6,0,1e6) 做 `CreateLocalZones`（等于没有），对每个 peer 只做 `CreateGhostZones`（只生成地形/据点数据，不留活体物件）。

社区文档互相印证：`ddormer/valheim-serverside` README 明确写着「On a dedicated server, `ZNet.instance.GetReferencePosition()` returns a position outside of the world and is not related to any player position.」

### 1.4 离开 active area 会怎样：承重被强制"满血"

`decomp/WearNTear.decompiled.cs` 第 523–539 行——**这是整份调研最关键的一段**：

```csharp
public void UpdateWear(float time) {
    if (!m_nview.IsValid()) return;
    if (m_nview.IsOwner() && ShouldUpdate(time)) {
        if (ZNetScene.instance.OutsideActiveArea(base.transform.position)) {
            float maxSupport = GetMaxSupport();
            if (!m_support.Equals(maxSupport))
                m_nview.GetZDO().Set(ZDOVars.s_support, maxSupport);   // 直接写满
            return;                                                     // 不算、不磨损
        }
        ...
        if (m_noSupportWear) {
            UpdateSupport();
            if (!HaveSupport()) num = 100f;      // → damage = 100/100 * health = 满血秒杀
        }
```

而 `GetSupport()`（第 411–426 行）在**无主**时同样直接返回 `GetMaxSupport()`。

### 1.5 这解释了「蓝图落地 → 走近 → 塌」的完整因果

1. 蓝图/服务端插件把构件作为 ZDO 写入（PlanBuild 直接 spawn，绕过摆放校验）。
2. 无主状态 → 服务端不实例化、不算承重 → 世界文件里躺着一堆"合法"构件。
3. 玩家走进 96 米 → `ReleaseNearbyZDOS` 把所有权给他。
4. 客户端该构件 `Awake` 后第一次 `UpdateWear` → `IsOwner()` 通过、不在 active area 外 → 跑 `UpdateSupport()`。
5. 接地链条缺失或水平/垂直损耗过大 → `HaveSupport()` = false → 当帧 100% 伤害 → 销毁。**表现为"人一走近就塌"，而不是"慢慢塌"。**

> 换个角度看：**"没塌"不等于"结构合法"，只是"还没人认领它"。** 这正好是我们可以利用的点——见第 3 节路径 C。

### 1.6 Valheim 1.0 新增的官方旋钮：SimulationDistance

`decomp/SimulationDistance.decompiled.cs`：1.0 引入了 0–5 档模拟距离设置，`ZNet.GetSyncedSimulationDistance()` 供 `ZNetScene` / `ZDOMan` 共用。

| 档位 | near | far | classic | 效果 |
|---|---|---|---|---|
| 0 | 1 | 2 | 是 | active area 缩到 64m（省性能） |
| 1 | 2 | 2 | 否 | 112m |
| 2 | 2 | 2 | 是 | **原版默认**（96m 判定） |
| 3 / 4 / 5 | 3 / 4 / 5 | 2 | 否 | 加载范围扩大到 7×7 / 9×9 / 11×11 zone |

**这给了我们一个零代码的"扩大计算范围"手段**：客户端把模拟距离调到 5，加载的 zone 范围显著变大（但注意 active area 判定本身仍受 1.5 zone 公式限制，扩大的是"加载/生成"半径，两者不完全等价——这一点我建议实测确认）。

---

## 2. 社区现成方案盘点

### 2.1 让服务端成为模拟者（=你说的"激活计算区域"的正统解）

| 项目 | 状态 | 做法 | 备注 |
|---|---|---|---|
| `ddormer/valheim-serverside`（Thunderstore: Serverside_Simulations） | 已停更，最后适配 0.220.5（2025-05） | 服务端创建并拥有 ZDO，本体模拟 | 公认的祖师爷，1.0 上未验证 |
| `Sergentval/Valheim-serverside`（ServerAuthority） | 活跃，2026-09 仍在提交 | ddormer 的继任者，「所有非玩家 ZDO 的服务端口所有权」 | 仍在 WIP；作者在研究 Avledet（自研服务端）+ TCP 直连 |
| `fire-VA/FiresGhettoNetworking` | 活跃，已适配 Valheim 1.0 | 主开关 `Enable Server-Side Simulation`：服务端加载所有玩家周围世界、驱动刷怪/事件、zone 增删权威 | 官方 README 明确警告：**ZDO 所有权转移（把生物 AI 搬到服务端）"不推荐"**，最未测试，会砸船/车 |
| `JereKuusela/Render_Limits` | 活跃 | `force_active` 命令把指定 zone 标记为常驻 active | 偏向渲染/客户端；服务端可行性需实测 |
| `Avledet`（crazicrafter1） | 个人项目 | **从零重写的 Valheim 服务端**，带 Lua modding API | 印证了"服务端可完全取代客户端模拟"这条路的可行性 |

### 2.2 实时读取 / 控制游戏（=你说的"API 帮我修房子陪我玩"）

| 项目 | 形态 | 能力 |
|---|---|---|
| `myrcutio/ValheimMCP`（Thunderstore: ValheimMCP） | BepInEx 插件，本地 HTTP + 原生 MCP | `POST /command` 跑任意控制台命令并回传输出、`GET /log` 尾日志、`render_view` 用**独立离屏相机**渲染指定坐标并返回 PNG。作者明说为 Claude Code 驱动而做；**无鉴权**，只绑 127.0.0.1 |
| `jneb802/ValBridgeServer` | BepInEx + GABP 协议 + GABS 编排器，MCP 暴露 | README 已实现：`player_get_position` / `player_get_health` / `run_command` / `znetscene_*`（预制体检索）/ `unity_*`（场景对象检视）。**`TOOLS.md` 是设计稿且已勾选完成**：`navigate_to_position`、`move_direction`、`jump`、`dodge`、`place_piece`、`remove_piece`、`craft_item`、`equip_item`、`get_visible_objects`（相机视锥 + 射线遮挡的真实视野）、`interact`、`pickup_nearby`……⚠️ README 与 TOOLS.md 不一致，实际可用集合需装一遍实测 |
| `HumanGenome/ValheimOne` | 服务端 mod + Web 后台 | 浏览器里跑服务端控制台（白名单）、实时地图（玩家/船/传送门），原版客户端可用 |
| `JFHeim/ValheimWebLink` | 服务端 mod，HTTP + Basic Auth | `/findobjects` 查箱子物品/传送门名；`/playerdata/get`、`/playerdata/set` **读写玩家数据**（模块化） |
| `JereKuusela/Server_devcommands` | 服务端 mod | 解锁专用服务端的 devcommands 给管理员 |
| `VentureValheim/Norse_Personality_Construction_System` | 服务端+客户端 | **行为像玩家的 NPC**（会复活、会随机走动、可成交任务/卖剑），目前是"最接近假人"的现成物 |

### 2.3 明确不存在的

**没有公开的「Valheim 无头机器人客户端」**。我用 GitHub API 检索 `valheim headless client`、`valheim bot`、`valheim ai agent` 等关键词：全部命中的都是 Discord 运维机器人、日志播报机器人，或运行在**游戏进程内**的插件。没有人做出"独立进程冒充玩家连上服务器"的东西。最接近的是 Avledet（自研服务端）和 Sergentval 的 ClientTcpJoin（客户端侧补丁）。

---

## 3. 「注入假人」四条路径可行性矩阵

| # | 路径 | 原理 | 能不能激活计算 | 风险 | 工程量 |
|---|---|---|---|---|---|
| **A** | 纯网络假 peer（伪造 `ZNetPeer` + refPos 塞进服务端 peer 列表） | 只骗过 `IsInPeerActiveArea` / `ReleaseNearbyZDOS` | ❌ **反而冻结**。ZDO 归它却没人跑模拟；更糟的是它会**挡住真实玩家抢所有权**（因为 `IsInPeerActiveArea` 判定为"原主仍在范围内"），玩家进来看到一堆僵死物件 | 高（改 netcode 内部结构，版本一升就崩） | 极大 |
| **B** | 服务端自己当模拟者（装 `ServerAuthority` / `FiresGhettoNetworking`） | 服务端创建并拥有 ZDO，本体跑 AI 与磨损 | ✅ 真激活，官方支持路径 | 中（CPU 暴涨；FiresGhetto 自评所有权转移"不推荐"） | 小（装插件） |
| **C** | **服务端 Harmony 篡改 ref pos + 强制服务端所有权** | `Game.FixedUpdate` 后置补丁把 ref pos 从 `(1e6,0,1e6)` 改成工地坐标；服务端 `IsOwner()` 自然成立，`WearNTear` / `Tameable` / `MonsterAI` 全套原版代码就地跑起来 | ✅ 真激活，且**不需要"假人"这个概念** | 中：等同于在服务端开一块"常驻模拟区"，CPU 与 ZDO 内存随之上升；`ZoneSystem` 会以 `SpawnMode.Full` 实例化该区域全部物件 | 中（自写 100–300 行插件，我们有全套反编译） |
| **D** | 真·无头客户端（第二份游戏进程 `-nographics` 登录当玩家） | 一个货真价实的 peer，客户端侧全流程模拟 | ✅ 最"正统"，驯化/AI/承重全真 | 高：Valheim 客户端强依赖 Unity 渲染与 GPU，需要虚拟显示/软渲染；社区无人做成功 | 极大 |

**我的判断（待你拍板）：C 是性价比最高的路线。** 理由是它复用了原版已经写好的全部模拟逻辑，而我们只需要骗过两行判断：ref pos 与所有权。副产品非常漂亮——**这就是"让服务器替我们当那个假人"**，而且服务端跑在 E:\ 那台 9700X 上，算力富余。

**同时 A 路径有个反直觉的用法值得记一笔**：既然无主 ZDO 的承重恒为最大值、且没人算就不会塌，那么**"落地后不让人进去"本身就是一种保命策略**。如果目标只是"把蓝图安全写进世界"，根本不需要假人，需要的是一份"验收清单"——这才是我们蓝图课题的真正闭环缺口。

---

## 4. 与我们课题的接口

| 课题痛点 | 本次调研给出的抓手 |
|---|---|
| 蓝图落地后承重崩塌 | 崩塌发生在**认领瞬间**，不是渐变。要么①提前算好合法承重（离地高度 vs 材料损耗），要么②让服务端先认领并按 C 路径真实模拟一遍，把不合法的构件在**离人**的时候暴露出来 |
| 蓝图摆放无从校验 | 服务端 `WearNTear.UpdateSupport()` 本身就是校验器。C 路径下可以做成"落地 → 服务端算 → 报告哪些构件会被销毁 → 我们再修蓝图"，形成闭环 |
| 养殖/驯化/农场离线不涨 | 同上，全部受 `IsOwner()` 门控；C 路径一并解决 |
| 需要"陪伴" | `ValheimMCP` + `ValBridgeServer` 已经给出控制台级与原子操作级的双手；配合 `render_view` 的离屏出图，Agent 可以"看见"并"动手" |

---

## 5. 待你决定的三个问题

1. **目标优先级**：我们要的是「蓝图安全落地（防塌）」还是「区域常驻模拟（养殖/驯化/防袭）」？前者几乎不需要改代码，后者直接上 C 路径。
2. **服务端版本锁定**：本地 `assembly_valheim.dll` 是 1.0 的专用服务端。C 路径的补丁点是 `Game.FixedUpdate` 与 `ZDOMan.ReleaseNearbyZDOS`，属于高频改动区，是否接受"每个小版本重新对一次签名"的维护成本？
3. **先做哪个**：是先把「蓝图承重预演器」做出来（离线算 support 链，不需要跑服务器），还是先做「C 路径的最小验证补丁」（把 ref pos 指到工地，看服务端日志里构件是否被销毁）？

---

## 附：本次产出的本地文件

- `decomp/*.decompiled.cs` — 反编译源码（ZoneSystem / ZDOMan / ZNetScene / WearNTear / ZDO / ZNet / Game / Player / ZNetView / SimulationDistance / Tameable / MonsterAI / Growup）
- `tools/ilspycmd.exe` — ILSpy 9.1 命令行反编译器（可复用）
- 原始程序集：`testserver/valheim_server_Data/Managed/assembly_valheim.dll`（2.5MB，非 `Assembly-CSharp.dll`，后者仅 23KB 是壳）
