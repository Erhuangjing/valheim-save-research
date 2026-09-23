---
name: valheim-blueprint-build
description: 把 Valheim 蓝图（PlanBuild .blueprint / BuildShare .vbuild / zip）按用户给的大致方位建进世界存档，玩家零 mod。流程：解析预检 → 在方位附近选点（避开主宅/护城河保护圈）→ 备份 → 服务端临时执行器（游戏内实测落地高度 → 放件 → 按件真实碰撞体做 PlanBuild 式逐点整地、回放蓝图 #Terrain 与原版锄头件）→ 地形与件数双验收 → 删净 BepInEx。用户说「用这个蓝图在我家旁边建个房子」「把 xx.blueprint 落到 (x,z)」「不装 mod 帮我建房」时使用。
---

# Valheim 蓝图落地（玩家零 mod）

所有命令在本仓库根目录执行（`tools/` 下纯标准库 Python 3.9+，零第三方依赖）。

## 能做 / 不能做

| 能做 | 不能做 |
|---|---|
| `.blueprint` / `.vbuild` / zip / PlanBuild 市场 blob 解析 | 纯离线改存档建整栋（实测证伪，见 `docs/Valheim_蓝图无mod落地_skill可行性调研.md`） |
| 在用户给的方位附近自动选点，避开保护圈；任意朝向（`--rotation`） | |
| 游戏内实测地形定落地高度：房子坐在地上，门口够得着 | 地形改动超过游戏硬限 ±8m（直接拒绝，不硬改） |
| 先整出整块房基 + 自适应缓坡接回原地形 + 地窖开挖 | 保留木牌文字 / 箱子内容等附加数据（件清单只存 name/位置/yaw） |
| 回放蓝图 `#Terrain`（PlanBuild 语义）与 vbuild 里的原版锄头件 | 斜梁等非纯 yaw 件的倾斜（按 yaw 落地，需目视） |
| 保护圈（主宅 + 护城河）内一个顶点都不改 | |

「零 mod」的准确含义：**玩家客户端零安装**；服务器只在落地窗口里临时装 BepInEx + 执行器，落地完删净，建筑本身是 100% 原版世界数据。

## 开工前必须问清（缺一不做）

1. **蓝图文件**路径。
2. **大致方位**：坐标 `(x, z)`，或「主宅东边 50 米」。后者要主宅坐标：用户给，或离线扫存档找玩家建筑聚落
   （`bp_reconcile.full_scan` + `bp_autosite.classify(name) == 'structure'` 按 32m 格聚类）。
   **朝向**：大门朝哪边（`--rotation` 度，俯视顺时针，绕蓝图中心；90 = 原来朝北的一面改朝东）。
   用户没要求就用 0（蓝图原朝向）；为了塞进保护圈外的空地而转，要先跟用户说明。
3. **保护圈**：一点都不能碰的区域（主宅 + 护城河 + 田地…）圆心与半径，**宁大勿小**。护城河是地形改动，
   离线扫描看不出来——问用户，或按「建筑外沿 + 护城河宽度」估。
4. **服务器**：先在**副本世界的测试服**跑通，再上正式服。要：服务器目录、**该服自己的启动脚本**（`--bat`；
   端口 / `-savedir` / `-world` 必须指向要改的那个世界）、存档世界目录。
5. **执行器**：`tools/xibpbuilder_reference/` 编译出的 `XiBpBuilder.dll`（只替换 csproj 的 `REPLACE_ME`，见其
   README）+ BepInExPack_Valheim 解包目录（`--bepinex-src`）。

## 铁律（违反任何一条就停下问用户）

1. **动档前必备份、必停服**。流水线按 `ExecutablePath` 只认目标服务器目录的进程——正式服和测试服同名同 exe，
   **绝不按进程名杀进程**，也绝不碰不是本次目标的服务器 / 存档 / 插件目录。
2. 正式服上线前，同一蓝图 + 同一方位先在副本世界跑一轮并通过验收。
3. 永不 `Force=true`；重跑某段 = 删对应 `BepInEx/config/*.flag`（`install` 会自动清，连同 `bp_runtime.txt`）。
4. 验收看数字：`run` 判据 + 地形报告 + 离线对账三样都过才算成功，不看「日志没报错」。
5. 执行器报「编排中止」「保护圈内有顶点」「需改超 ±8m」时**不重试同一参数**：按决策表换落点 / 换参数。

## 流程

```bash
W=runs/<蓝图名>_<日期>          # 工作目录：state / manifest / dump / 对比图 / 报告全在这
BP=<蓝图文件>; WORLD=<存档>/worlds_local/<世界名>; SRV=<服务器目录>; BAT=<该服启动脚本>

# 1 预检：件数、哈希命中、GroundLayerPy、#Terrain 段、原版锄头件、风险分级
python tools/bp_pipeline.py preflight --bp "$BP" --work $W
#   风险=high（含地下结构）→ 讲给用户：执行器会把 floor 件底下挖成地窖、深桩直接埋；同意后再继续

# 2 选点：只在用户给的方位附近找，整块（含外扩）不许碰保护圈
python tools/bp_pipeline.py autosite --work $W --world "$WORLD" --rotation <度> \
    --near-x <x> --near-z <z> --near-r 150 \
    --protect-x <px> --protect-z <pz> --protect-r <pr> \
    --max-spread 8 --max-burial 8 --site-margin 8
#   「起伏」是离线代理值（树/岩石高度）只用于排序；真实落地高度由执行器在游戏里实测。
#   PlatformY 贴近 30（海平面）的候选要剔掉。把前 3 个（坐标、离方位多远、起伏）给用户挑。

# 3 备份（停服状态）
python tools/bp_pipeline.py backup --work $W --world "$WORLD" --server "$SRV"

# 4 部署执行器（地形默认开；只想先看方案加 --terrain-dry-run）
python tools/bp_pipeline.py install --work $W --server "$SRV" \
    --bepinex-src <BepInExPack 目录> --plugin-dll <XiBpBuilder.dll> \
    --site-x <x> --site-z <z> --rotation <度> --protect-x <px> --protect-z <pz> --protect-r <pr>

# 5 起服落地：盯日志 → 编排完成（此时存档已写盘）→ 观察窗 → 自动停服
python tools/bp_pipeline.py run --work $W --server "$SRV" --bat "$BAT"

# 6 验收：离线对账 + 地形报告（三联对比图：改前 | 改后 | 高差）
python tools/bp_pipeline.py verify --work $W --world "$WORLD"
python tools/bp_terrain_report.py $W/bp_terrain_dump.csv --png $W/terrain.png

# 7 持久化复核（推荐）：flag 不清直接再起一次服；执行器按 dump 逐点重读地形，写 reload 列
python tools/bp_pipeline.py run --work $W --server "$SRV" --bat "$BAT"   # 日志「重载复核 … 持久化 ✓」
python tools/bp_terrain_report.py $W/bp_terrain_dump.csv                 # |reload − after| ≤ 0.05m

# 8 删净 BepInEx（玩家侧全程零安装）+ 报告
python tools/bp_pipeline.py teardown --work $W --server "$SRV"
python tools/bp_pipeline.py report --work $W
```

## 验收判据（全部满足才算成功）

| 来源 | 判据 | 含义 |
|---|---|---|
| `run` | 落点可用（无「编排中止」）、实例化 ≥ 98%、prefab 校验失败 0 | 件都放下了 |
| `run` | 地形验收：满权重顶点误差 ≤ 0.5m；**占地露缝** ≤ 0.5% | 房子坐在整好的地上，地基没露出来 |
| 地形报告 | 平台/过渡带断崖 = 0；保护圈内被改顶点 = 0 | 没有断层；护城河没被碰 |
| 地形报告 | 过渡带末端改动 < 0.3m | 与原地形无缝衔接 |
| `verify` | 存活率 = 100%（或用户接受的 `--min-rate`；会滚动的 `Cart` 等动态物件除外） | 件没丢 |
| 复核 run | `重载复核 … 持久化 ✓` | 地形在 zone 重载后还在 |

**断崖的两种口径**：一端是 `B`（地窖坑壁）/ `E`（蓝图 `#Terrain`：作者挖的院子、露台）/ `V`（vbuild 里作者的
锄头件）的，是**挡土墙**——蓝图设计本来如此，PlanBuild 回放同样是直壁，报告单独列出、不判失败，但要告诉用户
（「院子四周是 5m 石墙挡土」）；两端都是本工具生成的平台/过渡带（`P`/`S`）的才是断层，必须为 0。

给用户看：`terrain.png` + PlatformY、改动顶点数、挖填范围、外圈坡度、露缝、存活率。

## 出问题怎么办

| 现象（日志 / 报告） | 原因 | 处理 |
|---|---|---|
| `编排中止：落点起伏过大` | 贴地件处地形与平台高差逼近 ±8m 硬限 | 换更平的候选；**不要**调大 MaxDelta（游戏硬限） |
| `占地/蓝图地形有 N 个顶点落在保护圈内` | 房子压到保护区 | 挪落点远离保护圈；确认保护圈没画大 |
| `N 个满权重顶点需改超过 ±8m` | 局部陡坎 | 换落点，或挪几米避开陡坎 |
| 外圈坡太陡（门口上不去） | 平台与原地形高差大 | `[Terrain] SkirtSlope` 调小（如 30°）/ `SkirtMax` 调大，删 `terrain_done.flag` 重跑；或换更平的落点 |
| 房子明显高出 / 低于周围 | GroundLayerPy 推导错（看 preflight 的「方法」） | `install --ground-py <值>` 手填：院墙 / 锄头件 / 大门台阶所在层 |
| 露缝多 | 件碰撞体与平台高度不匹配 | 看对比图定位；`Cap`（默认 1m）调大后删 flag 重跑 |
| 地形验收不过（误差 > 0.5m） | heightmap 未重算 / 反射失败 | 看 `[地形] ★` 行；删 `terrain_done.flag` 重跑一次，仍不过就停下报告 |
| 重载复核漂移 | delta 没持久化 | 立刻停手，用备份回滚，报告给用户 |
| `服务器提前退出` / `'valheim_server' 不是内部或外部命令` | 启动脚本端口 / 存档不对，或 bat 是 LF 换行 | 看 `$W/server_stdout.log`；bat 必须 CRLF |

## 回滚

- **整世界回滚**：停服 → 把 `backup` 段的备份目录整个拷回存档位置（`report.md` 里有路径）。
- **只撤执行器**：`teardown` 已把 BepInEx 四件套移到 `$W/quarantine-*`，移回即恢复。
- 2026-09-22 及以前的执行器 `[Terrain]` 会把落点周围约 190m 见方整块压平（斜坡、断层、贴图错乱、不看保护圈）；
  受影响的世界只能用那次落地前的备份回滚，再用本 skill 重建。

## 地形段在做什么（解释给用户 / 改代码时看）

执行器照搬 PlanBuild（sirskunkalot/PlanBuild，WTFPL）`TerrainTools.cs` 的写法：只改 TerrainComp 的 level/smooth
delta 与 paint mask，然后 `ClaimOwnership → Save → Heightmap.Poke(0,false)`，高度 / 碰撞 / 渲染交给游戏自己重算。

0. **朝向**：整栋先绕蓝图包围盒中心转 `Rotation` 度（件的位置与朝向、`#Terrain`、锄头件一起转；
   `#Terrain` 方形的角度 = 条目角度 + 蓝图角度，同 PlanBuild 放置时的 transform.rotation）。
1. **落地高度**（件放下前）：地面层 = `|py − GroundLayerPy| ≤ 0.6` 的件；整栋偏移 = 中位数(实测地形 − 件 py)。
   GroundLayerPy 推导：院墙众数 → 原版锄头件中位数（作者当年整出的地表）→ py 直方图最低非孤立档。
2. **放件**（磨损冻结、支撑锁定，件不会掉）。
3. **逐点整地**（按件的真实碰撞体）：平台高度 = 地面层件底中位数；占地内地表贴到该点最低件底（略高的件垫土接住，
   柱脚直接埋）；斜撑脚/柱脚之间 ≤ 6m 的空隙整成同一块平台；比平台深 1m 以上的深桩 / 下层结构不整平台不挖坑；
   floor 件在平台以下 = 地窖，挖到件底；平台外 smoothstep 过渡带，宽度按边界高差自适应到坡度 ≤ 35°；
   保护圈外 3m 内改动平滑压到 0。
4. **蓝图 `#Terrain`**：按 PlanBuild 放置蓝图时的语义回放（circle/square、smooth、paint）。
5. **原版锄头件**（`mud_road` 等，旧名自动映射 `_v2`）：交给原版 `TerrainComp.DoOperation`。
6. 整过地的建筑**不做**锚点整体位移。

`[Terrain]` 参数：`Embed` 0.1、`Skirt` 6、`SkirtSlope` 35、`SkirtMax` 16、`Pad` 0.5、`Cap` 1、`Close` 3、`Sink` 1、
`LayerTol` 0.6、`MaxDelta` 8（游戏硬限）、`Paint` Dirt。

## 实测基准（2026-09-23，副本世界测试服）

| 蓝图 | 场景 | 结果 |
|---|---|---|
| skeggoxmanor（3117 件，48×57m，斜撑架空大宅） | 主宅旁、贴地件处原地形高差 4.8m、保护圈 r≈64m 压进过渡带 | 只改 2918 顶点（旧实现 29575 = 7 块整 zone）；挖填 −3.8 ~ +2.8m；平台误差 0；露缝 0/502；断崖 0；保护圈内 101 个跳过点 0 改动；外圈坡 p50 9° / p95 30°；对账 3116/3117（缺 1 辆推车）；重载复核 2918 点 max 0.001m |
| nelesstarterbase（2053 件，16 条 `#Terrain`，下沉院子 + 露台） | 平地 | `#Terrain` 回放 671 顶点、地窖 73；144 处断崖全是作者院子/露台的挡土墙（B/E），平台/过渡带 0；露缝 0/257；重载复核 max 0.001m；再起服对账补建 0（修复前误补 177） |
| longhouse.vbuild（1332 件 + 365 个锄头件） | 主宅东约 100m | 锄头件 365/365 原版执行；GroundLayerPy 取锄头件中位数 −0.58（直方图会误选 −6.83 深桩）；对账 1332/1332；平台/过渡带断崖 0 |

## 已知限制

- 游戏硬限：地形相对原始高度最多 ±8m。落点太陡只能换，不能硬改。
- 选点的「起伏」是离线代理估计（存档里没有原始地形高度，地形由种子生成），以执行器的落点起伏检查为准。
- 件清单只存 yaw：斜梁等非纯 yaw 件、木牌文字、箱子内容需人工补。
- 不开 `[Terrain]` 时仍走旧的锚点整体位移，其测量本身不可信（层掩码缺 terrain、碰撞体只取根节点），
  **建议始终开地形**。
