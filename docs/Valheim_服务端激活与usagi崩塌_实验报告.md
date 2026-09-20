# Valheim 服务端激活区域 · usagi-forest-lodge 崩塌观测报告

> 实验日期：2026-09-15 · 世界 `XIUSAGI`（拷贝自 `pre_blueprint_20260914-181738` 干净基线）
> 插件：XiBpBuilder v0.26.2 · 端口 2466 · **全程无客户端接入，纯服务端**

---

## 0. 三十秒结论

| # | 问题 | 结果 |
|---|---|---|
| ① | 服务端激活区域能不能做出来？ | ✅ **一个 Harmony postfix 就够**，所有权链路自动打通，服务端认领全部 4612 件 |
| ② | usagi 蓝图会不会塌？ | ✅ **基本不塌**：存活 4589 / 4625 = **99.22%**，只丢 36 件（0.78%） |
| ③ | 丢的件在哪？ | **全部集中在 Y 40.25~41.75（蓝图 py −1.50~0.00）= 地基接触那一层** |
| ④ | 地基以上稳不稳？ | ✅ **py 0.00 以上 100% 完好**（24 米高的木石结构，一件没掉） |
| ⑤ | 稳定吗？ | ✅ 前 30 秒丢 36 件后，**连续 7 分钟 16 次采样零变化** |

**一句话**：地基能咬住地面，这栋楼就是稳的。上一轮"塌 9%"的元凶是**挖开的地窖制造了空气腔**，不是建筑本身不成立。

---

## 1. 功能一：服务端激活区域

### 1.1 为什么需要它（前序反编译结论）

原版专用服务端每个物理帧把参考坐标写死在世界之外：

```csharp
// Game.FixedUpdate 第 676 行
ZNet.instance.SetReferencePosition(new Vector3(1000000f, 0f, 1000000f));
```

客户端有 `Player.LateUpdate` 覆盖回自身位置；专用服务端没有 local player，永远停在世界外。
连锁后果就是"什么都不算"：

| 环节 | 代码位置 | 后果 |
|---|---|---|
| `ZoneSystem.CreateLocalZones(refPos)` | ZoneSystem 1181 行 | 一个 zone 都不加载 |
| `ZNetScene.CreateDestroyObjects()` | ZNetScene 362 行 | 一个物件都不实例化 |
| `ZDOMan.ReleaseZDOS` → `ReleaseNearbyZDOS(refPos, sessionID)` | ZDOMan 929 行 | 服务端拿不到任何 ZDO 所有权 |
| `WearNTear.UpdateWear` → `OutsideActiveArea(pos)` | WearNTear 531 行 | 恒为真 → 把 support 写成最大值后直接 return |

补充：`WearNTearUpdater.Update()`（WearNTearUpdater 30–42 行）**每秒**遍历
`WearNTear.GetAllInstances()` 调 `UpdateWear`，**自身没有任何距离门控**。
所以门控 100% 在 `UpdateWear` 内部 —— 这也意味着只要前四环解决，支撑计算就会自动跑起来。

### 1.2 实现（v0.26 新增 `[Activate]` 段）

```csharp
[HarmonyPatch(typeof(Game), "FixedUpdate")]
public static class PatchActivate
{
    static void Postfix()
    {
        if (!CfgActOn.Value || !s_worldLoaded) return;
        var znet = ZNet.instance;
        if (znet == null || !znet.IsServer()) return;
        if (ZNet.GetConnectionStatus() != ZNet.ConnectionStatus.Connected) return;
        znet.SetReferencePosition(new Vector3(CfgActX.Value, CfgActY.Value, CfgActZ.Value));
    }
}
```

**就这一个 postfix —— 没有"假人"、没有网络 peer、没有额外的所有权代码。**

因为 `ZDOMan.ReleaseZDOS` 第 929 行本来就给服务端自己发一次所有权：

```csharp
ReleaseNearbyZDOS(ZNet.instance.GetReferencePosition(), m_sessionID);              // 服务端自己
foreach (ZDOPeer peer in m_peers) ReleaseNearbyZDOS(peer.m_peer.m_refPos, peer.m_peer.m_uid);
```

refPos 一旦指回工地，`IsInPeerActiveArea` 里 `uid == m_sessionID` 的分支就成立，
服务端自然认领落点附近的持久 ZDO（该函数 950–976 行，只处理 `Persistent` 的件）。

### 1.3 实测验证（运行时读数）

```
[激活] 开启 → 激活点 (-262.0, 0.0, 270.0)
[激活] refPos=(-262,0,270) 实例=4861 我们的件=4612 本进程拥有=4612 ★支撑不足=0 support[34.0~1500.0] 覆盖帧=2250
[激活] refPos=(-262,0,270) 实例=4835 我们的件=4589 本进程拥有=4589 ★支撑不足=0 support[21.7~1500.0] 覆盖帧=24751
```

| 指标 | 激活前 | 激活后 |
|---|---|---|
| refPos | (1000000, 0, 1000000) | **(-262, 0, 270)** |
| 实例化的 WearNTear | **0** | **4861** |
| 其中本插件的件 | 0 | **4612** |
| **其中服务端自己拥有的** | **0** | **4612（100%）** |
| 覆盖帧数 | **0** | 24751（持续生效） |

**`本进程拥有 = 我们的件` 这一项是本次实验最关键的证据**：
它证明服务端不是"看着"这些件，而是真的成了它们的 owner，也就是真的在跑 `UpdateSupport`。

### 1.4 一个采样陷阱（重要）

`★支撑不足=0` **不能**当作"没有件在挨饿"的证据。
`UpdateWear` 发现 `HaveSupport()` 为假时会**当帧造成 100% 伤害**，
件在 1 秒内就消失了，所以瞬时采样几乎永远抓不到"正挨饿"的状态。

**判断崩塌要用「件数随时间的变化」，不是「支撑不足数」。**

---

## 2. 功能二：usagi 蓝图落地 + 崩塌观测

### 2.1 蓝图转换

`usagi-forest-lodge.blueprint` 是 PlanBuild 文本格式（`name;category;x;y;z;qx;qy;qz;qw;...`）：

| 项 | 值 |
|---|---|
| 总件数 | 5427 |
| 分类 | Building 4625 · Furniture 683 · Misc 99 · Crafting 20 |
| 本次采用 | **仅 Building 4625 件**（结构件，排除家具装饰） |
| 包围盒 | X 26.0 m × Z 38.1 m × Y 24.75 m |
| 构件种类 | 53 种 |
| 转换脚本 | `ref/usagi_convert.py` → `testserver/BepInEx/config/usagi_pieces.txt` |

**GroundLayerPy = −1.60 的推导**：石砌基础 `stone_wall_*` 中心 py=−1.10，半高 0.5
→ 底面 −1.60。该蓝图没有 `wood_fence` 院墙，自动检测会失效，必须手动指定。
（插件语义：`worldY = groundBase + py`，所以 py=−1.60 的平面正好对齐地形平台。）

### 2.2 落地结果

```
第 82 批：新建 17，累计 4625/4625，待建 0
★ 落地完成：共 4625 件（null 0 次，zone 未加载跳过 8485 次，prefab 校验失败 0 次）
最终落点：(-262, 270)  基准 Y = 41.35
```

零失败、零校验错误。

### 2.3 崩塌对账（离线读存档，与运行时读数完全吻合）

存活 **4589 / 4625** = **99.22%**，丢失 **36 件（0.78%）**。

**丢失构件的类型分布：**

| 构件 | 丢失 | 总数 | 比例 |
|---|---|---|---|
| `stone_wall_1x1` | 15 | 151 | 10% |
| `stone_wall_2x1` | 13 | 321 | 4% |
| `stone_stair` | 6 | 21 | 29% |
| `stone_floor_2x2` | 2 | 8 | 25% |

→ **全部是石砌基础构件**。木结构、屋顶、梁柱一件没丢。

**按蓝图相对高度 py 分档：**

| py 区间 | 丢失 / 总数 | 比例 |
|---|---|---|
| −1.50 | 6 / 80 | 8% |
| −1.00 | 29 / 128 | 23% |
| 0.00 | 1 / 94 | 1% |
| **+0.50 ~ +22.15** | **0 / 4300+** | **0%** |

丢失件 Y 范围 **40.25 ~ 41.75**（基准 41.35），也就是**紧贴地形表面的那一层**。
地基以上 24 米高的结构 **100% 完好**。

**空间分布**：丢失件分散在周边各格（格(−3,+1) 丢 8/108、(−3,0) 丢 4/111、(−2,−3) 丢 4/89…），
不是集中在单一角落 —— 符合"地形起伏导致局部基础没咬住"的解释。

### 2.4 稳定性

```
11:50:53  件数=4612 拥有=4612 支撑不足=0
11:51:23  件数=4589 拥有=4589 支撑不足=0
11:51:53  件数=4589  ...
（此后 7 分钟 16 次采样恒定 4589，support 下限稳定在 21.7，服务端日志 0 报错）
```

**前 30 秒丢完 36 件后完全收敛**，再没有掉过一件。

---

## 3. 与上一轮"塌 9%"的对照

| | 上一轮 Nelesstarterbase | 本轮 usagi-forest-lodge |
|---|---|---|
| 件数 | 1805 | 4625 |
| 地形处理 | 整平平台 **+ 开挖地窖** | **仅整平平台** |
| 崩塌 | 162 件 / **9.0%** | 36 件 / **0.78%** |
| 分布 | 全在地窖层（py −6.5 处 100% 全塌） | 全在地基接触层（py ≥ 0 全完好） |
| 支撑锁定 | 最终靠 `UpdateSupport` 接管兜底 | **本次未开**（观察真实行为） |

**推论**：上一轮的塌方不是"建筑不成立"，而是**挖开地窖后，窖内的件四周没有地形、
向下射线又打不到地面 → 判定无支撑 → 当帧销毁**。
本轮把楼直接放在整平平台上，地基一层就吃住了地面，上面 24 米完全稳定。

---

## 4. 踩坑记录（本次新增，都值得记进技能）

### 坑 A：全新世界不走 `ZNet.LoadWorld`

`ZNet.ServerLoadWorld()` 里是：

```csharp
if (m_world.IsChunkedSave()) LoadWorld(); else LoadOldWorld();
```

而 `m_chunkedSave` 是从存档元数据 `_main.<N>.fwl2` 读出来的布尔值。
**全新世界第一次加载时它是 false → 走 `LoadOldWorld()`，`LoadWorld()` 根本不被调用。**
日志特征：只有 `Load world: XXX` + `missing XXX.db`，没有 `ZNet.LoadWorld:`。

→ 解法：挂点改到共同入口 **`ZNet.ServerLoadWorld`**，两条路径都能触发。
→ 顺带发现：`testsave/` 里既有的 XIT14/XIT15/XITER2 其实是**从 WORLD 复制**的
（内部世界名仍是 WORLD），所以它们走 chunked 分支 —— 这也解释了为什么以前没踩到。

### 坑 B：补丁必须逐个显式注册

插件用的是 `h.PatchAll(typeof(X))` 逐个注册，**不是** `Harmony.CreateAndPatchAll`。
v0.26.1 新增的 `PatchActivate` 忘了加进注册列表 →
补丁静默不生效，日志里 refPos 恒为 (1000000,0,1000000)、覆盖帧=0。
**现象是"功能完全不工作"，但没有任何报错。**

### 坑 C：对账脚本的两个误判源

1. `hashlib.json`（57,706 条）**缺 `wood_wall_log_4x0.5`**，导致这类件在扫描时"隐身"。
   用已校验过的 StableHash（`stone_stair` → 389771597 一致）补算：214591025。
2. 旧的匹配函数**只比位置、不比名字**，会把相邻的其他构件误判成同一个件。
   修正为"名字 + 位置"双匹配后，离线结果 4589 与运行时读数**完全一致**。

---

## 5. 结论与下一步

### 已确认

1. **服务端激活区域可行且极简**：一个 `Game.FixedUpdate` postfix，无需假人/客户端。
2. **它可以替代之前的"支撑锁定"兜底方案**：本次 `Support.Enabled=false`，
   服务端在真实模拟下只丢 0.78% 且立刻收敛 —— 说明只要摆位正确，本来就不需要锁。
3. **"埋进地形"不是解药**（前序结论）也**不是必须**：
   正确做法是让基础底面**正好落在地表**，而不是埋进去或架空。
4. 副产物：**服务端从此也能"看到"世界**（实例化 + AI + 支撑计算），
   不必人驻守，也不需要客户端。

### 可选的下一步

| 方向 | 说明 |
|---|---|
| A | **把 `[Activate]` 装到正式服**：验证无人驻守时蓝图的长期稳定性（含怪物/天气侵蚀） |
| B | **关掉 `Support.Enabled` 重跑上一轮的 Nelesstarterbase**：验证"地窖开挖"是不是唯一元凶 |
| C | **用激活区域做离线预演器**：不开客户端就能批量验证任意蓝图的落地稳定性 |
| D | 把 `GroundLayerPy` 自动推导做成通用功能（当前是手工按半高表算的） |

---

## 附：本次产物

| 文件 | 说明 |
|---|---|
| `bpbuild/Plugin.cs` | 插件 v0.26.2，新增 `[Activate]` 段（配置项 6 个 + 补丁 + 体检协程） |
| `ref/usagi_convert.py` | usagi 蓝图 → 件清单转换器（含 StableHash 兜底） |
| `ref/usagi_pieces.txt` | 4625 件清单 |
| `ref/usagi_collapse.py` | 崩塌对账脚本（名字+位置双匹配） |
| `testserver/BepInEx/config/com.world.bpbuild.cfg` | 实验配置（支撑锁定关闭、激活开启） |
| `testserver/start_test_usagi.bat` | 测试服启动脚本（端口 2466，saveinterval 300） |
| `testsave/worlds_local/XIUSAGI/` | 实验世界（含落地结果，save 24） |
| `testserver/BepInEx/config/bpbuild.log` | 完整运行日志（含 16 次激活体检采样） |
