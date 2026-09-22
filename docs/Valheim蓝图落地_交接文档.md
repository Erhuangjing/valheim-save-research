# Valheim 蓝图离线落地项目 · 交接文档

> **给接手方(人或 AI)**:本文档是自包含的。你不需要任何前置对话记录。
> 读完第 0、1、8 节就能知道"要干什么、现在卡在哪、下一步该做什么";第 3~7 节是已经验证过的技术细节与踩坑清单。
> 深度技术参考另有一份 1200+ 行的技能文档,见第 11 节。

---

## 0. 三十秒速览

**目标**:把社区蓝图(`.blueprint`)的 1805 个建筑构件,**离线写进 Valheim 专用服务器存档**,不装任何 mod(因为客户端也要装 mod 才能生效,朋友太多不现实),同时保留原有存档和玩家自己盖的房子。

**做法**:写了一个 BepInEx 插件(`XiBpBuilder.dll`),挂在专用服务器的 `ZNet.LoadWorld` 之后,用游戏自己的 API 创建 ZDO 物件 + 重塑地形。

**现状(2026-09-15)**:功能链路已经全部打通,但**最新一版(v0.18)刚部署、尚未实测**。

**最关键的 3 条结论**(都是实测出来的,不是推断):

1. **不要手工拼 ZDO 字节**。截稿前最稳的路径是在插件里调 `ZDOMan.CreateNewZDO(pos, hash)` + 显式 `zdo.SetPrefab(hash)`,避开所有二进制细节与插入点边界问题。
2. **地形改动的正确写法是"只改 `TerrainComp.m_levelDelta`、用增量公式"**,不要直接写 `Heightmap.m_heights`(会被游戏重算覆盖)。顶点坐标**没有 +0.5 偏移**。
3. **建筑稳定性不要指望"摆位"**。实测:地下结构"埋在土里"照样塌 50%;`WearNTear` 判定接地走射线(`FindSupportPoint`)而不是碰撞体相交。**唯一可靠的兜底是接管 `WearNTear.UpdateSupport()`**。

---

## 1. 任务与约束

### 1.1 需求
- 用户在一个 Valheim 专用服务器世界(`WORLD`)里玩,想把社区蓝图 `Nelesstarterbase` 落地成一栋真实建筑
- 只要**结构件**,不要家具/装饰(已过滤,1805 件)

### 1.2 硬约束(决定了技术路线)
| 约束 | 后果 |
|---|---|
| **不装 mod**(客户端一个个装依赖太麻烦) | 不能用 PlanBuild 这类 mod;必须在服务器端把结果"做进存档" |
| 服务器是专用服务器,不是本地主机 | 无法用游戏内指令;`devcommands` 会**永久禁用成就** |
| 玩家自己盖的主宅不能动 | 清理/开挖都要有保护圈(中心 −305,267,半径 20 米) |
| 原存档要能回滚 | 每次改动前备份整个世界目录 |
| 服务器运行时 DLL 被锁 | 部署插件前**必须先停服** |

### 1.3 落点参数(已固化)
```
落点中心        (-262, 270)
蓝图包围盒中心  (-9.85, -2.45)   ← 蓝图坐标,用于换算世界坐标
地面层 py        -0.70             ← wood_fence(院墙)的 py 众数,自动检测得到
基准 H_base      40.10             ← py=0 对应的世界 Y;= 平台高度 39.40 − (−0.70)
平台高度         39.40
地窖底           Y 33.54(py −6.557)
世界坐标换算     worldX = CfgX + (bpX − cx)   worldZ = CfgZ + (bpZ − cz)
                 worldY = H_base + bpY + yOffset
```

---

## 2. 环境与路径

### 2.1 关键路径
| 用途 | 路径 |
|---|---|
| 专用服务器根目录 | `<VALHEIM_SERVER_DIR>` |
| 世界存档 | `<服务器>\save\worlds_local\WORLD\` |
| 游戏本体(取 resources.assets 用) | `<VALHEIM_DIR>` |
| 游戏程序集(API 扫描用) | `<服务器>\valheim_server_Data\Managed\assembly_valheim.dll` |
| **插件源码** | `<WORK>/bpbuild/Plugin.cs` |
| 插件工程文件 | `<WORK>/bpbuild/XiBpBuilder.csproj` |
| 编译产物 | `<工程>\bin\Release\XiBpBuilder.dll` |
| **插件部署位置** | `<服务器>\BepInEx\plugins\XiBpBuilder.dll` |
| **插件配置** | `<服务器>\BepInEx\config\com.world.bpbuild.cfg` |
| **运行日志** | `<服务器>\BepInEx\config\bpbuild.log` |
| 件清单(蓝图转换结果) | `<服务器>\BepInEx\config\bp_pieces.txt` |
| 阶段标记文件 | `<服务器>\BepInEx\config\{bp_done,cleanup_done,terrain_done}.flag` |
| 参考/分析脚本 | `tools/` |
| 蓝图原文件 | `<BLUEPRINT>` |
| 备份 | `<BACKUP_DIR>\` |
| 技能文档(深度参考) | `C:\Users\<USER>\.workbuddy\skills\valheim-dedicated-server\SKILL.md` |

### 2.2 工具链与环境限制
- **Windows + Git Bash**(不是 cmd,不是 PowerShell 优先)
- **.NET SDK 9.0.313**,插件目标框架 `netstandard2.1`
- **被安全策略拦截、不可用**:`cmd.exe`、`Start-Process`、`Add-Type`、COM 对象
- **服务器运行时插件 DLL 被占用** → 部署前必须确认 `valheim_server.exe` 没在跑

### 2.3 编译与部署(照抄即可)
```bash
# 编译
cd "<WORK>/bpbuild"
"/c/Program Files/dotnet/dotnet" build XiBpBuilder.csproj -c Release -v q --nologo

# 部署(先确认服务器已停)
cp "<WORK>/bpbuild/bin/Release/XiBpBuilder.dll" \
   "<VALHEIM_SERVER_DIR>/BepInEx/plugins/XiBpBuilder.dll"
```

---

## 3. 技术全景(已打通的链路)

### 3.1 存档文件结构(Valheim 1.0)

一个世界由四件套组成:

| 文件 | 说明 |
|---|---|
| `_main.<N>.fwl2` | 可读文本元数据(世界名、种子、时间、玩家档案等) |
| `_main.<N>.db2` | 全局数据库(ZDO 主库)。16 字节头 + zlib 压缩(**wbits=47**) |
| `_main.<N>.chunks` | chunk 索引:magic u16 + 头部总数 i32 + 条目数 i32 + 每条 **11 字节** `[y, x, gen, save, zdo]` |
| `<x>_<y>__<gen>_<save>.chunk` | 单个区块:版本 u16 + 记录条数 i32 + 记录流 + 尾部结构 |

**实测**:`.db2` 大小恒定在 150,448 字节;**地形数据不在 db2 里,存在 `.chunk` 文件中**。

### 3.2 ZDO 记录格式(1.0 实测)

```
[flags u16][pos 3×f32][prefab hash u32][短字段…][字段区: cnt + cnt×(字段哈希 u32 + 值)]
```

- `flags` 的 **bit8 = Persistent**,创建的持久物件恒置位
- **没有 sector 字段**(社区 Avledet 规范里有,1.0 已删除 —— 照着老规范写会错位)
- rotation 是 4 字节 float

**关键性质**:记录长度 = `f(flags, prefab)` **唯一确定** → 同一个 prefab 的件天然等长。
这就是"等长替换"(如把墓碑搬走)能直接改字节的原因。

**插入点必须落在记录边界上**(用 `candidates(b)[-1]` 定位)。插进尾部结构会导致 `EndOfStreamException`;
插到文件末尾是"假成功"(越界被静默吞掉)。

> ⚠️ **虽然解析出了这些,但最终没有走手工拼字节路线**。原因见第 7 节坑 #1。

### 3.3 prefab 哈希算法

Valheim 用 `StableHash`:
```
h1 = h2 = 5381
奇数位字符异或进 h1,偶数位异或进 h2(每次 h = h*33 ^ c)
result = (h1 + h2 * 1566083941) & 0xFFFFFFFF
```
名字库来源:`resources.assets` 里的字符串 + `StreamingAssets/SoftRef/manifest*`。
**实测 57,707 个名字,识别率 99.6%**。

⚠️ **但落地时不要用这个自己算的哈希**,要用游戏权威的 `ZNetScene.GetPrefabHash(prefab GameObject)`。
实测两者一致(`stone_stair`: 389771597),但用游戏 API 更保险。

### 3.4 地形数据结构

| 字段 | 位置 | 说明 |
|---|---|---|
| `Heightmap.m_heights` | `List<float>` | **游戏实际使用的高度** |
| `TerrainComp.m_levelDelta` | `float[]` | 存档里的"相对原始地形的偏移" |
| `TerrainComp.m_smoothDelta` | `float[]` | 平滑偏移 |
| `TerrainComp.m_modifiedHeight` | `bool[]` | 是否被玩家改过 |

**权威顶点坐标公式**(参照社区实现 EarthWorks 的 `GetWorldVertex`):
```csharp
float halfSize = hmap.m_width * hmap.m_scale * 0.5f;
float worldX = hpos.x - halfSize + j * m_scale;
float worldZ = hpos.z - halfSize + i * m_scale;
int   index  = i * (m_width + 1) + j;
```
- **没有 `+0.5f`**(早期版本多加 0.5 米,导致整块地形偏移 —— 这是坑 #5)
- 每块 64×64 米,65×65 个顶点,`m_scale = 1.0`,即**顶点精度 1 米**
- **边界顶点被相邻 Heightmap 共享** → 相邻块也要一起改

**写入方式(必须用增量公式)**:
```csharp
// delta_new = delta_old + smooth_old + (目标高度 − 当前高度)
float curLocal = hmap.GetHeight(j, i);          // 注意参数顺序 (x, z)
float tgtLocal = targetWorldY - hpos.y;
float req = levelDelta[index] + smoothDelta[index] + tgtLocal - curLocal;
levelDelta[index] = Mathf.Clamp(req, -8f, 8f);  // 游戏内部限制 c_LevelMaxDelta = 8
smoothDelta[index] = 0f;
modifiedHeight[index] = true;
```
写完后按顺序:取 `ZNetView` 所有权 → `TerrainComp.Save(false)` → `ApplyModifiers()` → `Poke(0, false)` →
`UpdateCornerDepths()` → `RebuildCollisionMesh()` → `RebuildRenderMesh()`。

**硬限制**:地形 delta 是**相对原始地形**的,游戏内部 clamp 在 **±8 米**。地窖往往要挖 5~8 米,
如果原始地形本身高出平台 2 米,`−8` 会先被吃满、角落挖不到底(实测差 0.26 米,可接受)。

### 3.5 建筑支撑机制(★ 最重要的一节)

```
WearNTear.UpdateSupport()   ← 结构完整性的唯一入口(private)
  ├─ GetMinSupport()  低于它 → 判定"支撑不足" → 开始累积损坏 → 最终 Destroy
  ├─ GetMaxSupport()  该件支撑上限
  └─ HaveSupport()    = GetSupport() >= GetMinSupport()
木头实测:max = 100,min = 10
support 值存在 ZDO 的 "support" 键里
```

**支撑是连锁传递的**:接地件拿满支撑,相邻件按距离衰减取值。

**实测反直觉结论**:
- 把地下结构"埋进地形"**并不能**让它们接地,埋着照样塌(见第 6 节数据)
- Valheim 找支撑走 `WearNTear.FindSupportPoint(Vector3, WearNTear, Collider)` 射线检测,
  **不是** collider 相交判定

**可靠解法 —— 接管 `UpdateSupport`**(社区成熟做法,`valheim-no-wear-and-tear` / `AzuWearNTearPatches` 同思路):
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

---

## 4. 蓝图解析

### 4.1 蓝图 → 件清单
蓝图原文件是 JSON(PlanBuild 格式),已转换成 `bp_pieces.txt`:
```
# 共 1805 件   地板基准 py=0.7
# 格式: name|hash|x|y(相对地面)|z|yaw(度)
stone_stair|389771597|-16.3252|-6.3567|4.8853|180.0000
```

### 4.2 蓝图 py 分层语义(这栋房子)
```
py −0.70   院墙 wood_fence(地面层)          ← 对齐到地表
py −0.045  一层木地板
py  0.326  二层木地板 / 石墙
py  9.33   屋顶最高点
── 以下为地下 ──
py −0.674  地窖天花板(stone_floor + woodwall)
py −1.873  天花板横梁(wood_beam,**沿 X 每 2 米一根**)
py −2.674  地窖门(wood_gate ×2 @ x=−7.22)
py −2.37 ~ −5.37  地窖石墙(4 层叠放,每层 1 米)
py −5.857  地窖地板 stone_floor_2x2
py −6.357  地窖最深处的石阶
```
- **地窖空间**(蓝图坐标):X ∈ [−19.3, −5.0],Z ∈ [−6.8, 7.9]
- **入口石阶**:5 级,Z = −5.15 / −3.15 两列(4 米宽),X 从 −10.22(py −5.674)向东升到 −2.22(py −1.674),
  **每级 2 米跨度升高 1 米**
- **实心阶梯**:每级台阶底下都垫了 `stone_floor_2x2` → 地形应挖到"整个阶梯结构的最底面"

---

## 5. 插件架构(XiBpBuilder)

### 5.1 挂载点
```csharp
[HarmonyPatch(typeof(ZNet), "LoadWorld")]
static void Postfix() {
    StartCoroutine(CleanupTask());        // [清理]
    StartCoroutine(TerrainTask());        // [地形]
    StartCoroutine(Run());                // [落地]
    StartCoroutine(TeleportWatch());      // [传送](服务器端无效,仅保留)
    StartCoroutine(SupportDiagTask());    // [支撑体检]
}
```

### 5.2 四个功能段
| 段 | 作用 | 标记文件 |
|---|---|---|
| `[Cleanup]` | 清理两片废弃建筑 + 落点自然物(石头/树),带保护圈 | `cleanup_done.flag` |
| `[Terrain]` | 重塑地形:整平平台 + 开挖地窖 + 入口阶梯 | `terrain_done.flag` |
| `[Build]` | 落地 1805 件;支持"对账补齐" | `bp_done.flag` |
| `[Support]` | 锁定支撑 + 输出支撑体检报告 | 无 |

**阶段跳过规则**:对应 flag 存在且 `Force=false` 就跳过。**要重跑某一段就删对应的 flag 文件**(不要让 `Force=true`,那会把三段全部重跑,重复创建件)。

### 5.3 配置项全清单
```ini
[Build]
Enabled = true                  # 总开关
OriginX = -262 / OriginZ = 270  # 落点中心
YOffset = 0                     # 整体抬高/降低
GroundLayerPy = -0.70           # 设计地面层 py
AutoDetectGroundLayer = true    # 从 wood_fence 众数自动检测
PerPieceGround = false          # false=整栋统一基准(推荐)
Force = false                   # 忽略 bp_done.flag 强制重跑
Reconcile = true                # ★ v0.18 对账补齐:只补缺失的件
AutoFindFlat = false            # 地形重塑开启时关掉
SearchRadius = 140
BatchSize = 60 / FrameDelay = 10
PieceFile = bp_pieces.txt

[Cleanup]
Enabled = true
Center1X/Z = -262,270  Radius1 = 30
Center2X/Z = -354,378  Radius2 = 30
ProtectX/Z = -305,267  ProtectRadius = 20   # 玩家主宅,绝不碰
CleanNature = true                          # 连自然物一起清
NatureFilter = Rock,MineRock,FirTree,Pinetree,Beech,Birch,...
PrefabFilter = woodwall,wood_wall,wood_floor,wood_roof,...

[Terrain]
Enabled = true
Mode = flat                     # flat=整平平台
Carve = true                    # ★ v0.18 按地下结构开挖
CellarRect = -19.3,-6.8,-5.0,7.9      # 地窖开挖矩形(蓝图坐标)
EntranceRect = -11.6,-6.6,3.6,-1.6    # 入口阶梯通道矩形
DeepPy = -1.2                   # py 低于此值视为地下结构件
Embed = 0.30                    # 构件底部埋入地形的深度
CellarFloorPy = -5.85           # 地窖底兜底值(会被自动推算覆盖)
StairTopBpX = -2.22 / StairTopPy = -1.674 / StairStepDx = -2.0
StairRampLen = 4.0              # 顶级石阶往东的过渡斜坡长度
MaxDelta = 8                    # 游戏硬限制,别改大
Margin = 8.0                    # 平台比建筑每边多出的宽度
DryRun = false                  # true=只统计不写入

[Support]
Enabled = true                  # ★ v0.18 锁定本插件件的支撑
MarkKey = xiabp                 # ZDO 标记键
Diagnose = true                 # 输出支撑体检报告
```

---

## 6. 实测数据与结论

### 6.1 已跑通的里程碑
| 项 | 结果 |
|---|---|
| 蓝图件数 | 1805(过滤掉家具后) |
| prefab 解析 | 36 种构件,成功 36,失败 0 |
| 落地 | `load 103,385 zdos from 13 Chunks`,零异常 |
| prefab 校验 | 0 失败(`zdo.GetPrefab()` 与预期 hash 一致) |
| 地形 Heightmap | 加载 43 个,落点 93 米内取用 5 个,改动 2060 个顶点,写入校验 0 失败 |
| 地形改动量 | −6.01 ~ +2.44 米,平均 1.64 米 |

### 6.2 塌方对账(★ 核心数据,离线读存档比对得出)
```
存活 1643 / 1805        塌掉 162 件(9.0%)

按蓝图 py 分档:
  py −6.50  塌  17 /  17  (100%)   ← 地窖最深
  py −6.00  塌  39 /  47  ( 83%)
  py −5.50  塌  20 /  53  ( 38%)
  py −5.00  塌   4 /  20  ( 20%)
  py −4.50  塌  28 /  55  ( 51%)
  py −4.00  塌   4 /  16  ( 25%)
  py −3.50  塌  26 /  60  ( 43%)
  py −3.00  塌  10 /  32  ( 31%)
  py −2.50  塌   7 /  57  ( 12%)
  py −2.00  塌   7 /  68  ( 10%)
  py −1.50 及以上的全部 0%

塌掉的构件类型:
  stone_stair        22 / 31   (71%)
  stone_pillar        8 / 10   (80%)
  wood_wall_log_4x0.5 5 /  6   (83%)
  stone_wall_2x1     54 /115   (47%)
  stone_floor_2x2    37 / 91   (41%)
  wood_wall_log       8 / 16   (50%)

塌掉件 Y 范围 33.74 ~ 38.43(中位 35.73)
存活件 Y 范围 34.24 ~ 49.43(中位 40.43)
```

**读法**:塌的全部集中在地窖层,地面以上一件没塌。当时地窖是"被整平、埋在土里"的状态 ——
**所以"埋在土里更稳"这个假设被数据推翻了**。

### 6.3 被推翻 / 修正过的假设(重要,避免重走弯路)
| 曾经的假设 | 实测结论 |
|---|---|
| 手工拼 ZDO 字节插入 chunk 最直接 | 插入点边界极难处理,已改用游戏 API 创建(API 路线一次成功) |
| 用最低件当对齐基准 | ❌ 会让整栋抬高 6 米、院墙悬空。正确基准 = **地面层 py(院墙众数)** |
| 地形写 `Heightmap.m_heights` | ❌ 会被 `ApplyModifiers()` 重算覆盖。**只改 `m_levelDelta`、用增量公式** |
| 顶点坐标要 `+0.5f` | ❌ 官方公式**没有** +0.5,多了就整块偏移 |
| 地下结构埋在土里更稳 | ❌ 埋着照样塌 51%(见 6.2) |
| "挖出地窖"一定更不稳 | ⚠️ 待 v0.18 验证。之前塌是因为**没有锁支撑**,而不是"挖开"本身 |
| `ZDOMan.DestroyZDO` 能删物件 | ❌ 服务器端不生效(异步队列)。**改用 `SetPosition` 搬到远处** |

---

## 7. 踩坑清单(症状 → 根因 → 解法)

**#1 手工拼字节的插入点**
症状:`EndOfStreamException` 或改了没效果。
根因:插到尾部结构里,或插到文件末尾(越界被吞)。
解法:插入点必须在记录边界 `candidates(b)[-1]`。**但最终推荐放弃这条路,直接用游戏 API。**

**#2 prefab hash 全 0 → 房子不可见**
根因:`CreateNewZDO(pos, hash)` 之后**没有显式调 `zdo.SetPrefab(hash)`**。
解法:创建后立刻 `SetPrefab` + `SetPosition` + `SetRotation` + `SetOwner(0L)`,并当场校验 `zdo.GetPrefab()`。

**#3 落地后整栋抬高、院墙悬空**
根因:对齐基准取了"最低件"。
解法:基准 = 设计地面层 py(自动取 `wood_fence` 的 py 众数)。

**#4 地形改了但看不到 / 不持久**
根因:只写 `m_heights`,被 `ApplyModifiers()` 重算覆盖。
解法:改 `m_levelDelta`(增量公式)+ 完整的重建调用链(见 3.4)。

**#5 地形整块偏移**
根因:顶点坐标多加了 `0.5f`。
解法:用 `origin − halfSize + idx * scale`。

**#6 `DestroyZDO` 删不掉东西**
症状:连续 15 轮扫描都命中同一批 3254 个物件。
根因:服务器端 `DestroyZDO` 走异步队列(`m_destroySendList`),实际不生效。
解法:**改用 `zdo.SetPosition(6000, −400, 6000)` 搬到世界角落**,第二轮扫描命中 0。

**#7 服务器上找不到 `-savedir`**
症状:客户端目录里生成了一个空世界。
解法:启动参数必须带 `-savedir` 指向目标世界目录。误生成的世界要改名留档,别直接删。

**#8 `Force=true` 导致重复落地 3610 件**
解法:正式部署保持 `Force=false`;要重跑就**删对应的 flag 文件**(精确到某一段)。

**#9 支撑崩塌(反复多轮)**
症状:上线后房子持续崩塌。
演进过程:抬高基准 → 加重埋深 → 加地窖保护 → 改 CellarMode → 修坐标 → …
**最终解法**:接管 `WearNTear.UpdateSupport()`(见 3.5),不再依赖摆位。

**#10 服务器端拿不到 Player 对象**
症状:想实现"传送玩家回家",但 `Player.GetAllPlayers()` 和 `FindObjectsOfType<Player>()` 都返回空。
根因:专用服务器上玩家位置是**客户端权威**,服务器端 `ZNet peers = 1`。
解法:服务器端无法传送玩家,只能靠客户端指令(但 `devcommands`/`goto` **会永久禁用成就**,不可逆)。

**#11 「逐件贴合最近构件」开挖的陷阱(★ v0.18 踩到)**
症状:地窖剖面采样显示 X 从 −19 到 −9 全是同一个高度,地窖完全挖不下去。
根因:地窖天花板那排 `wood_beam`(py −1.873)在 Z=−5.09 排成一长列,离任意探针只有 **0.96 米**,
"最近构件"启发式每次都命中这些**高位梁**,把地形托在 Y38.4。
解法:**开挖必须用确定性分区规则**(矩形 + 阶梯函数),不要用"找最近构件"这种启发式。

**#12 构件半高算错导致多挖**
症状:自动推算的地窖底偏深 0.25 米。
根因:`wood_wall_log_4x0.5` 按 `wood_wall` 前缀算成半高 1.0,但它的名字里就写着"长4米高0.5米"。
解法:半高表要**按具体 prefab 名逐条列**(见下表),别只按前缀粗分。

| 构件 | 半高 | 备注 |
|---|---|---|
| `stone_floor_*` | 0.25 | 厚 0.5 |
| `stone_wall_*` / `stone_stair` | 0.5 | 高 1 |
| `stone_arch` / `stone_pillar` / `wood_pole*` | 1.0 | 高 2 |
| `woodwall` / `wood_wall` | 1.0 | |
| **`wood_wall_log_4x0.5`** | **0.25** | ★ 名字里 4x0.5 = 高 0.5 米 |
| `wood_wall_half` / `wood_wall_quarter` | 0.5 / 0.25 | |
| `wood_floor*` | 0.05 | 厚 0.1 |
| `wood_beam*` / `wood_roof*` | 0.1 | 截面很薄 |
| `wood_stair` | 0.5 | |

**#13 编译后忘了替换服务器目录的 DLL**
症状:改了半天没效果。白跑一整轮。
解法:编译后**必须手动 `cp` 到 `<服务器>\BepInEx\plugins\`**,并检查文件大小/时间戳。

---

## 8. 当前状态与下一步

### 8.1 已完成
- ✅ 存档格式逆向(四件套、ZDO 记录、prefab 哈希)
- ✅ 地形数据结构与写入方法(权威公式 + 增量 delta)
- ✅ 插件 v0.18:清理 / 地形开挖 / 落地 / 对账补齐 / 支撑锁定 / 支撑体检
- ✅ 蓝图 → 件清单转换(`bp_pieces.txt`,1805 件)
- ✅ 离线分析工具链(`ref/` 下的 Python 脚本)
- ✅ v0.18 已编译部署,**世界已备份**

### 8.2 v0.18 待验证的内容(接手第一件事)
**v0.18 是「未实测」状态**,需要起服跑一遍,重点看 `<服务器>\BepInEx\config\bpbuild.log`:

1. `[地形] ★★ 开挖模式` + `开挖分解：地窖空腔底 N 点 | 入口阶梯 M 点 | 平台 K 点`
2. `[地形] 最深处结构底面 py = ? → 自动地窖底 Y = ?`(预期约 33.54 / py −6.557)
3. `[地形] 超限裁剪` 的数量(预期少量,地窖角落会差 0.26 米左右)
4. `[对账] ZDOMan 共 N 个 ZDO…命中 M / 1805 → 缺失 X`(**预期缺失 ≈ 162 件**)
5. `[体检] …支撑不足共 N 个,其中我们的件 M 个`(预期锁定后为 0)
6. 复查:在游戏里从入口石阶走进地窖,确认**能下去、窖门和石阶都在**

### 8.3 已知的未决问题
| 问题 | 状态 |
|---|---|
| 入口顶级石阶 → 平台的过渡 | 已加 4 米缓坡(`StairRampLen`),未实测通行性 |
| 地窖角落因 ±8 米限制挖不到底 | 预期差 ~0.26 米,接受 |
| 开挖后地窖地板可能悬空 0.45 米 | 靠支撑锁定兜底,但视觉上地板会"浮" 0.45 米,需观察 |
| 支撑锁定后所有件都显示满支撑(绿色) | 预期行为,等价于"给这批件发免塌证" |
| 玩家主宅附近的自然地形有没有被误伤 | 保护圈半径 20 米,建议复查 |

### 8.4 如果 v0.18 效果不好
回滚方式(两选一):
```bash
# A. 只回滚插件
cp "<服务器>/BepInEx/plugins/XiBpBuilder.dll.v17" "<服务器>/BepInEx/plugins/XiBpBuilder.dll"

# B. 回滚整个世界(会丢掉 v0.18 的所有改动)
rm -rf "<服务器>/save/worlds_local/WORLD"
cp -r "<BACKUP_DIR>/WORLD" \
      "<服务器>/save/worlds_local/"
```

---

## 9. 操作手册

### 9.1 起服 / 测试流程
1. 确认 `valheim_server.exe` 已停
2. 删掉要重跑那一段的 flag 文件(如 `<服务器>\BepInEx\config\terrain_done.flag`)
3. 启动服务器(带 `-savedir` 指向 `save/worlds_local/WORLD`)
4. 看 `bpbuild.log`,确认各段输出符合预期
5. 进游戏检查

### 9.2 离线分析工具(`ref/` 目录)
| 脚本 | 用途 |
|---|---|
| `analyze_collapse.py` | ★ 读 chunk 文件 + 比对蓝图,报告"塌了多少、塌在哪一层、什么类型" |
| `analyze_layers.py` / `bp_py_map.py` | 蓝图层级分布分析 |
| `gen_carve_preview.py` | ★ 离线复现开挖规则,生成俯视 + 剖面预览 HTML |
| `terrain_preview.py` / `bp_html.py` | 地形/蓝图可视化 |
| `zdo_lib.py` | ZDO 二进制读写基础库(`hs()` / `candidates()` / `walk()` / `insert_bytes()`) |
| `apiscan/Program.cs` | **API 扫描工具**:读 `assembly_valheim.dll` 的 metadata,列出任意类型的字段与方法签名(含 private) |

**`apiscan` 用法**(找游戏内部 API 时非常有用):
```bash
cd "tools/apiscan"
"/c/Program Files/dotnet/dotnet" run --project apiscan.csproj -c Release -- \
  "<VALHEIM_SERVER_DIR>\valheim_server_Data\Managed\assembly_valheim.dll" \
  "WearNTear" "support|placed|health"
```

### 9.3 诊断方法(不用起客户端)
在插件里遍历场景对象读取真实支撑值:
```csharp
var arr = UnityEngine.Object.FindObjectsOfType<WearNTear>();
var RefSupport = AccessTools.FieldRefAccess<WearNTear, float>("m_support");
var MMin = AccessTools.Method(typeof(WearNTear), "GetMinSupport");
float sup = RefSupport(w);
float mn = Convert.ToSingle(MMin.Invoke(w, null));
bool starved = sup < mn - 0.001f;    // ← 就是会塌的那批
```

### 9.4 对账补齐的原理(把塌掉的件补回来)
落地过一次后 `bp_done.flag` 会阻止重建 → **塌掉的件会永久缺失**。
不要删 flag 重跑(会变双份),而是**对账**:
```csharp
// 用 ZDOMan 的权威数据,不依赖 zone 是否实例化成 GameObject
var f = AccessTools.Field(typeof(ZDOMan), "m_objectsByID");
var dict = f.GetValue(ZDOMan.instance) as System.Collections.IDictionary;
foreach (System.Collections.DictionaryEntry e in dict) {
    var z = e.Value as ZDO;
    int h = z.GetPrefab();  Vector3 p = z.GetPosition();
    if (超出落点 45 米) continue;
    have.Add(h + "|" + round(x*4) + "|" + round(y*4) + "|" + round(z*4));  // 0.25 米量化
}
// 用同样的 key 公式算蓝图每件的期望位置,不在 have 里的就是缺失的
```
要点:
- **y 也要一起量化**(同一 (x,z) 常叠放多件,如石墙 py −5.374 与 −3.374)
- 拿不到 `m_objectsByID` **就主动放弃**,宁缺勿重
- 期望位置必须和**当初落地时**用同一套公式(含同一个 `groundBase`)
- 补建时**按 py 从低到高排序**(先地基后上层)

---

## 10. 参考资源(社区项目,都已读过源码)

| 项目 | 价值 |
|---|---|
| `MaikiOS/EarthWorks` | ★ **地形首选参考**:顶点坐标、delta 写入、批次保存、回滚快照 |
| `PlanBuild`(作者 sirsk89) | ★ 蓝图落地流程、`WearNTear` 的 Harmony patch 写法、`PlanPiece` 支撑判定 |
| `mrnotsoevil/valheim-no-wear-and-tear` | ★ **支撑锁定**的最小实现(patch `UpdateSupport` / `UpdateWear` / `SetHealthVisual`) |
| `Azumatt/AzuWearNTearPatches` | 同思路 + ServerSync,配置项设计可参考 |
| `Skarif/ValheimPerformanceOverhaul` | 提到 `WearNTear.GetSupport()` 结果缓存、异步初始化(说明支撑计算的代价) |
| `Digitalroot-Valheim/...HeightmapUnlimitedJvL` | 解除 ±8 米地形限制的 hook 方式 |
| `JereKuusela/BetterContinents` | 世界生成期的 heightmap 处理 |
| `Frogger/GroundReset` | 地形 delta 的读写与还原 |
| valheimcheats.com 的构件页 | 每件的 `max/min support` 数值(如 Workbench: max 100 / min 10) |

**搜索时有效的关键词**:`Valheim ZDO format`、`Valheim WearNTear UpdateSupport`、
`Valheim blueprint spawn structure collapse`、`Valheim heightmap levelDelta`。

---

## 11. 文件清单

### 11.1 代码与插件
| 文件 | 说明 |
|---|---|
| `bpbuild/Plugin.cs` | ★ 插件源码(v0.18,约 1500 行) |
| `bpbuild/XiBpBuilder.csproj` | 工程文件(`netstandard2.1`) |
| `bpbuild/bin/Release/XiBpBuilder.dll` | 编译产物 |
| `<服务器>/BepInEx/plugins/XiBpBuilder.dll` | 部署位置 |
| `<服务器>/BepInEx/plugins/XiBpBuilder.dll.v17` | 上一版备份 |

### 11.2 数据与配置
| 文件 | 说明 |
|---|---|
| `<服务器>/BepInEx/config/bp_pieces.txt` | 1805 件清单(蓝图转换结果) |
| `<服务器>/BepInEx/config/bp_pieces_full.txt` | 含家具的完整清单 |
| `<服务器>/BepInEx/config/com.world.bpbuild.cfg` | 插件配置 |
| `Nelesstarterbase_只要结构.blueprint` | 蓝图原文件(过滤后) |
| `ref/hashlib.json` | 57,707 个 prefab 名字→哈希映射 |

### 11.3 报告与预览
| 文件 | 说明 |
|---|---|
| `Valheim_1.0记录格式_实测核查.md` | 存档格式实测记录 |
| `Valheim_存档写入_技术报告.md` | 早期技术方案 |
| `Valheim_存档写入_社区参考结论.md` | 社区调研结论 |
| `Valheim_蓝图落地_操作说明.md` | 操作手册 |
| `WORLD_地窖开挖方案_v18.html` | ★ v0.18 开挖方案预览(俯视 + 剖面) |
| `WORLD_蓝图落地预览.html` | 落地效果预览 |
| `新房子在哪_俯视对照图.html` | 落点与老宅的位置对照 |

### 11.4 深度技术参考(最重要的外部文档)
**`C:\Users\<USER>\.workbuddy\skills\valheim-dedicated-server\SKILL.md`** —— 1200+ 行,分 17 节,
覆盖从开服、参数调优、存档格式、ZDO 结构、prefab 哈希、地形写入、支撑机制到开挖规则的全部细节。
**本文档是它的浓缩版,遇到本文没讲透的地方直接查它。**

核心章节:
- 十二~十四节:存档二进制、蓝图落地、对齐基准与地形处理
- 十五节:崩塌诊断与实测结论
- 十六节:地形写入的权威做法(官方顶点公式、delta 增量写入、±8 米限制)
- 十七节:★ 建筑支撑机制 + 按蓝图开挖地窖(v0.18 的全部结论)

---

## 12. 协作约定(和这位用户配合时的注意事项)

**沟通风格**
- 用**简体中文**、第一人称、轻松口语化;称呼用户用"她"
- **指令非常简短**("继续""开始""好了吗""试试"),需要自己判断下一步该做什么
- 喜欢**表格 / 编号**呈现结果,尤其是带**显式通过/失败列**的检查报告

**工作方式**
- **先给方案预览,再动手**:她习惯先看 HTML 预览图 / 流程图初稿,确认方向后再执行完整任务
- **先测试再全量执行**:例如"先在文档加一句测试,满意后再更新整个表格"
- **反馈驱动迭代**:她会先观察执行过程,再给出关键洞察(本项目里"栅栏那层才是地面"就是她看出来的)
- **明确要求先查资料**:多次强调"不要重复造轮子,先搜社区 / GitHub 有没有人研究过"
- 起服务常用非常规端口,用完会明确要求停掉

**技术偏好**
- 讨厌"每个客户端都要装 mod"的方案 → 优先服务器端方案
- **重视备份与可回滚性**
- 会主动检查"有没有落盘敏感信息"(服务器密码等只能通过内存通道使用,禁止写盘)

**做完事要做的两件事**
1. 往 `.workbuddy/memory/YYYY-MM-DD.md` 追加工作记录
2. 如果发现可复用的新方法,更新 `valheim-dedicated-server` 技能文档

---

## 附:一句话总结给接手方

> 技术链路已经全部打通(存档逆向 → 地形写入 → 建筑支撑),插件 v0.18 已经能"按蓝图开挖地窖 + 补齐塌掉的件 + 锁死支撑"。
> **你接手第一件事:停服 → 起服 → 把 `bpbuild.log` 里「开挖分解 / 对账命中数 / 支撑体检」三段读一遍,和 8.2 节的预期值对照。**
> 如果对不上,查第 7 节的坑清单;方向错了,第 8.4 节有回滚命令。
