# Valheim 蓝图落地 · 测试环境交接说明

> 目的：让接手的人能在**自己的机器**上完整复现这套流程的测试 —— 编译、部署、实机落地、离线对账、地形验证。
>
> 这份文档里的每个判据都是实测出来的，不是推测。凡是「文档推断」的地方我都标了 ⚠️。

---

## 一、需要准备什么

| 项 | 要求 | 说明 |
|---|---|---|
| Valheim 专用服务器 | 1.0.x，`valheim_server.exe` | headless 能跑（`-nographics -batchmode`） |
| .NET SDK | 6/7/8/9 任一 | 编译插件用；本项目实测 **.NET 9** |
| Python | 3.9+ | 跑 `tools/` 下脚本。**纯标准库，零第三方依赖** |
| BepInEx | 5.4.x（Valheim 专用服务端版） | 解压到服务器根目录，能看到 `BepInEx/core/` |
| 一份测试存档 | **必须是副本** | 别拿正式档做实验 |

### 三条隔离原则（务必遵守）

1. **副本存档** —— 把存档复制到独立目录，用 `-savedir` 指过去
2. **独立端口** —— 别和正式服撞（本项目测试用 `2467`）
3. **独立实例** —— 别在正式服上装/删插件

起服模板：

```bash
export SteamAppId=892970
./valheim_server.exe -nographics -batchmode \
  -name "xi_bp_test" -port 2467 -world "TESTWORLD" -password "<PASSWORD>" \
  -savedir "/abs/path/to/testsave" -public 0 \
  -saveinterval 30 -backups 10
```

> `-saveinterval 30` 让存档每 30 秒落盘一次，便于观察件数变化。正式跑可以不改。

---

## 二、编译插件

### 2.1 纯净编译（**唯一可信的编译口径**）

> 「纯净」= **只替换 csproj 里的路径占位符，一行代码不改**。
> 这条必须守 —— 否则你分不清失败是自己的临时补丁还是上游代码。

`tools/xibpbuilder_reference/XiBpBuilder.csproj` 里 7 个引用都是占位符：

```xml
<HintPath>REPLACE_ME\valheim_server_Data\Managed\assembly_valheim.dll</HintPath>
<HintPath>REPLACE_ME\BepInEx\core\BepInEx.dll</HintPath>
...
```

把 `REPLACE_ME` 换成你的服务器根目录（**用正斜杠，MSBuild 也认**；反斜杠在脚本里容易被当转义）：

```
REPLACE_ME\valheim_server_Data\Managed  →  /your/server/valheim_server_Data/Managed
REPLACE_ME\BepInEx\core                 →  /your/server/BepInEx/core
```

然后：

```bash
dotnet build XiBpBuilder.csproj -c Release
```

**判据**：`0 error`。产物在 `bin/Release/XiBpBuilder.dll`。

### 2.2 为什么必须 7 个引用

| 程序集 | 提供 | 缺了的典型症状 |
|---|---|---|
| `UnityEngine` | `MonoBehaviour` 等 | **77× CS0012** 级联（不是 CoreModule！） |
| `UnityEngine.CoreModule` | `Vector3` / `Quaternion` / `Mathf` | — |
| `UnityEngine.PhysicsModule` | `Physics.OverlapBox` | 锚点求解写不了 |
| `assembly_valheim` | `ZDO` / `ZNetScene` / `WearNTear` / `Heightmap` / `TerrainComp` | — |
| `assembly_utils` | `ZPackage` / `Utils`（1.0 起拆出） | — |
| `BepInEx` | `BaseUnityPlugin` / `ConfigEntry` | 缺 `using BepInEx.Configuration;` 会报 `ConfigEntry<>` 找不到 |
| `0Harmony` | `Harmony` / `HarmonyPatch` | — |

### 2.3 ⚠️ 编译过 ≠ 能跑

本项目实测两次栽在这：

- **#19**：`Heightmap.GetAllInstances()` 根本不存在（真名 `GetAllHeightmaps()`）；`ZoneSystem.GetZone` 是 `static`；`FindTerrainCompiler` 收的是**世界坐标**不是 zone id
- **#22**：编译全过，但 `Poke` 的真实签名是 `Poke(Int32, Boolean)` —— 运行期抛 `TargetParameterCountException`

**查真实签名的可靠办法**：用 .NET 反射读 `assembly_valheim.dll`，**不要用字符串搜索**
（会被 `#Strings` 堆的**后缀压缩**骗出假阴性 —— `Destroy` 是 `DestroyZDO` 的后缀时只存一份）。

最小工具（30 行，`net9.0` 控制台工程）：

```csharp
// 加载 assembly_valheim.dll，按类型名打印全部方法/字段（含泛型参数与 [static] 标记）
var asm = Assembly.LoadFrom(Path.Combine(MANAGED, "assembly_valheim.dll"));
foreach (var t in asm.GetTypes().Where(x => x.Name == args[0]))
    foreach (var m in t.GetMethods(BindingFlags.Public|BindingFlags.NonPublic|
                                   BindingFlags.Instance|BindingFlags.Static|
                                   BindingFlags.DeclaredOnly))
        Console.WriteLine($"{m.ReturnType.Name} {m.Name}({string.Join(", ",
            m.GetParameters().Select(p => p.ParameterType.Name))}) {（static 标记）}");
```

---

## 三、部署到测试服

### 方式 A：手动（可控，推荐首次用）

1. 编译产物 → 服务器 `BepInEx/plugins/XiBpBuilder.dll`
2. 生成 cfg → 服务器 `BepInEx/config/com.world.bpbuild.cfg`
3. 件清单 → 服务器 `BepInEx/config/bp_pieces.txt`
4. **删干净 flag**（见下）
5. 起服

> **cfg 的 GUID 必须和插件的 `BepInPlugin` 一致**。本项目骨架的 GUID 是 `com.world.bpbuild`
> （脱敏时把 `xiabeize` 换成 `world` 的结果）。**GUID 不匹配 → BepInEx 会新建一份全默认的 cfg，你调好的配置一个字都读不到**（这条踩过，见 issue #9）。

### 方式 B：用流水线（`bp_pipeline.py install`）

```bash
python tools/bp_pipeline.py install \
  --work  /tmp/bpwork \
  --server /path/to/server \
  --bepinex-src /path/to/BepInExPack_Valheim \
  --plugin-dll /path/to/XiBpBuilder.dll \
  --site-x 424 --site-z -384 --platform-y 52.26 --ground-py 1.52 \
  --cfg-guid com.world.bpbuild \
  --i-have-a-backup --dry-run
```

`--dry-run` 先看它要做什么。铁律 1 要求声明有备份，否则会拒绝执行。

### flag 文件决定走哪条路径（**最容易误判的一点**）

cfg 目录下三个 flag：

| flag | 存在时行为 | 想强制重跑就删掉 |
|---|---|---|
| `cleanup_done.flag` | 跳过清理段 | 删 |
| `terrain_done.flag` | 跳过地形段 | 删 |
| `bp_done.flag` | 走**对账补建**路径（只补缺失件，**跳过锚点位移**） | 删 → 走完整重建 |

**这是两条不同的代码路径，测试时一定看清日志走的是哪条**：
- 完整重建 = `[清理] 开始` + `[落地] 开始` + 锚点位移
- 对账补建 = `[清理] 跳过` + `[落地] 跳过（bp_done 存在）` + `[锚点] 跳过：对账补建路径…`

想测锚点/位移，**必须删 `bp_done.flag`**。本项目日志会给出一行原因提示（issue #16 的成果）。

---

## 四、测试流程（标准七步）

```bash
# 0) 确认无残留进程（重要：上一轮没死透会导致 DLL 被锁、端口冲突）
#    Windows: Get-Process valheim_server -ErrorAction SilentlyContinue | Stop-Process -Force
#    Linux:   pkill -f valheim_server

# 1) 恢复基线存档（从副本复制）
rm -rf <testsave>/worlds_local/TESTWORLD
cp -r <backup>/TESTWORLD_save_orig <testsave>/worlds_local/TESTWORLD

# 2) 写 cfg + 件清单、装 DLL、删 flag

# 3) 校验落点干净：落点 ±60m 内应只有自然物（几十~一百条记录），没有建筑件

# 4) 起服

# 5) 盯日志直到出现 "=== 编排完成，可停服删 BepInEx ==="

# 6) 再等 2~3 分钟（掉件发生在解冻后 25~75 秒内，之后收敛）

# 7) 停服 → 离线对账（双口径）
```

### 第 7 步的命令

```bash
# 含 y 口径
python tools/bp_reconcile.py --manifest <manifest.json> --world <存档目录> \
  --origin-x 424 --origin-z -384 --platform-y 52.26 --ground-py 1.52 --yoffset <累计位移> \
  --min-rate 0.95

# 忽略 y 口径（交叉验证）
python tools/bp_reconcile.py ... --ignore-y --min-rate 0.95
```

`--yoffset` 用日志里 `★ [锚点] 求解结束：累计位移 X` 的值；对账补建路径则用 cfg 里的 `YOffset`。

---

## 五、判据（**这节是重点**）

### 5.1 怎么数「件数」—— 别用错指标

| ❌ 会骗人的指标 | 为什么 |
|---|---|
| 落点窗口内的**总记录数** | 混入掉落物（`Wood`/`Stone`/`RoundLog`）与自然物 → 掉落物滚动就被误读成"崩塌" |
| `WearNTear.GetAllInstances().Count` | **区域卸载**会让它变小 —— 实测 1928→1639，**不是销毁** |
| 按坐标量化 key 直接 diff | 掉落物沉降 → key 变化 → 被记成"消失+新增" |

✅ **正确做法**：

1. **打标清点** —— 反射拿 `ZDOMan.m_objectsByID`，按自己的 mark key 数：
   ```csharp
   var dict = AccessTools.Field(typeof(ZDOMan), "m_objectsByID").GetValue(ZDOMan.instance) as IDictionary;
   int marked = 0;
   foreach (DictionaryEntry e in dict) if (((ZDO)e.Value).GetInt("xiabp", 0) == 1) marked++;
   ```
   这个数**不受实例化状态/区域加载影响**。
2. **对账匹配用 `(hash, round(x*4), round(z*4))`，忽略 y** ——
   y 有 **0.25 量化尖刺**（实测同一批件，`yo=+0.25` 命中 58、`0.00` 命中 0、`−0.25` 命中 122、`−1.25` 命中 1127）
   → 用 y 做匹配会得出完全错误的存活率。

### 5.2 怎么判断「件是掉了还是移动了」

对账会同时输出**缺失清单**和**盒内多出清单**。逐条比对 key：

```
缺失  MeadStaminaMedium [3234308413, 1669, -1520]
多出  MeadStaminaMedium [3234308413, 1669, -1519]     ← 同 hash，坐标差 0.25m → 是「移动」
```

同 hash + 坐标差 ≤1 个量化单位（0.25m）→ **移动**（掉落物物理沉降）；
完全找不到对应 → **才是销毁**。

本项目据此确认：某轮"缺 5 件"实际**结构件一件没少**。

### 5.3 双口径交叉验证（判断 Δy 漂移）

| 含 y | `--ignore-y` | 结论 |
|---|---|---|
| PASS | PASS | Δy 单值，位移实现正确 |
| **FAIL** | **PASS** | **存在 Δy 漂移**（位移把件移得不一致），不是真缺件 |
| FAIL | FAIL | 真的缺件 |

### 5.4 各阶段的健康区间（本项目基准）

| 指标 | 健康值 | 说明 |
|---|---|---|
| `落地完成：N 件，prefab 校验失败 0 次` | 校验失败 = **0** | 非 0 说明 prefab hash 或创建有问题 |
| `[锚点] 位移 ...m：N 件（ZDO 不存在 0）` | **全量 N 且三轮一致**；`ZDO 不存在 = 0` | 每轮件数不同 = 子集位移 bug |
| `[锚点] 位置校验：N 件中 Δy≠累计位移 M 件` | **M ≤ 3**（容差 0.05m） | 成百件 = Δy 非单值 |
| `[体检] ... support 值域 [a,b]，越界 0` | 值域**不能两边都是 0** | 恒 0 说明反射拿错字段，判据失效 |
| `[对账] 命中 X / 缺失补建 N 成功（失败 0，应补 K）` | 失败 **0**、成功 == 应补 | — |
| `[地形] 重建调用链缺 N 个方法` | **0** | 非 0 = 重建链没跑，改动可能不生效 |
| `[地形] 改动 N 个顶点` | N > 0 | 0 则不写 `terrain_done`（设计如此） |
| `TargetParameterCountException` / `NullReferenceException` | **0** | — |

### 5.5 地形段的验证方法（#22 相关）

```
Terrain.Enabled = true
Terrain.Carve   = true
```

期望日志：

```
[地形] 开始（Mode=flat / Carve=True）
[地形] 落点 93m 内取用 7 个 Heightmap
[地形] m_heights 布局自检 width=64 点(1,2)：写 X→Y，回读 Y，复原 X → 一致 ✓
[地形] heightmap@(...) 累计改动 N 顶点
[地形] 改动 N 个顶点（DryRun=False）
```

然后**必须**：

1. `BepInEx/config/terrain_done.flag` **被写入**（说明它认为真的改过）
2. 日志里**没有** `重建调用链缺`、**没有** `TargetParameterCountException`

**反向验证**（很重要）：故意把 `Poke` 的参数去掉、或删掉某个重建方法 → 应该走 `Miss` 分支（**优雅失败**，输出缺哪些方法）而**不是抛异常**，且**不写** `terrain_done`。

> `terrain_done` 的正确语义：**异常/重建不全 → 不写；改动顶点 0 → 不写；真改完 → 才写**。
> 这条是本项目口碑很好的设计 —— 避免「空跑也写 done、后续静默跳过」把错误状态永久固化。

---

## 六、排查手册（都踩过）

| 症状 | 原因 | 解法 |
|---|---|---|
| `WinError 1224: 请求的操作无法在使用用户映射区域打开的文件上执行` | DLL 被 OS 映射缓存锁住（进程没死透） | ① 先确认进程全退（等 8~12 秒）② 把旧 DLL `rename` 到 `_retired/` 再复制（**直接删也会失败**） |
| 复制 DLL 后行为没变 | 复制静默失败但旧文件还在 | **务必核对 SHA256 + 字节数** |
| 服务器起不来 / 立刻退出 | 端口被占 / 上一实例没死 | 查进程、换端口 |
| cfg 改了没生效 | **GUID 不匹配**（BepInEx 按 GUID 找 cfg 文件名） | 核对 `BepInExPlugin` 的 GUID 与 cfg 文件名 |
| 日志里一件事都没发生 | flag 让阶段全跳过了 | 删 `*.flag` |
| 一开地形就崩 | 见 #22（`Poke` 签名 + `TryInvoke` 参数匹配） | 修法见该 issue |
| 脚本报「环境太大 for exec」（`xargs`） | 环境变量过长 | 别用 `xargs`，改用管道或写文件 |
| Windows 下 `tasklist` 读出来乱码 | 输出是 **GBK** | Python 读要 `encoding='gbk'` |
| 进程数判断误报 | 某些环境下 shell 与 `tasklist` 计数不一致 | **以 `Get-CimInstance Win32_Process` 的 `ExecutablePath` 为准**（顺便能确认杀的是不是测试服而不是正式服） |
| 替换路径的脚本突然不工作 | **heredoc/字符串里的 `\v` `\B` 被当转义序列吃掉** | 用文件写脚本，别用内联 heredoc |
| 反射调用的方法参数个数不对 | `AccessTools.Method(type, name, paramTypes)` 的 `paramTypes` **必须完整** | 别只传 `args[0]` 的类型；无参时传 `Type.EmptyTypes` 而不是 `null` |

---

## 七、基准数据（供对照）

同一蓝图（1763 件）、同落点、同路径，唯一变量是 `Support.Enabled`：

| 设置 | 件数曲线 | 结构件丢失 |
|---|---|---|
| `false` | 1763 → 1728 → 1710 → **1698（−65）** | 有 |
| **`true`** | **1758 / 1763 = 99.72%** | **0**（缺的 5 件是掉落物沉降） |

完整重建/地形轮的实测值：

```
[对账] 命中 205 / 缺失补建 1763 成功（失败 0，应补 1763）
[锚点] 实例化就绪 1763/1763（等待 1.0s）
[锚点] 第1轮 必死=0 锚点=145 无碰撞体跳过=1603
[锚点] 位移 -0.35m：1763 件（ZDO 不存在 0）        × 3 轮一致
[锚点] 位置校验：1763 件中 Δy≠累计位移 2 件（容差 0.05m）
★ [锚点] 求解结束：累计位移 -1.05m
[体检] WearNTear 共 1837，支撑不足 0；support 值域 [100.0,1500.0]，越界 0
[地形] 落点 93m 内取用 7 个 Heightmap
[地形] 改动 29575 个顶点（DryRun=False）
```

**已知的非阻塞观察**：`无碰撞体跳过 = 1603 / 1763（91%）` 偏高。
方向是等 `Collider` 而非等 `ZNetView`（`WaitInstancesReady` 已把"实例化就绪"做到位，但碰撞体还有一帧级间隔）。

---

## 八、工具速查

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `bp_parse.py` | 解析蓝图（`.blueprint` / `.vbuild`） | `--json` `--txt` `--selftest` |
| `bp_autosite.py` | 离线选落点（平整/陆地/无人区/样本足/**埋深**） | `--manifest` `--world` `--max-burial` |
| `bp_reconcile.py` | 落地后离线对账 | `--ignore-y` `--min-rate` `--yoffset` |
| `bp_pipeline.py` | 七段流水线（预检→选点→备份→部署→跑→对账→删净→报告） | `preflight` / `install` / `run` / `verify` / `teardown` |
| `bp_pipeline.py install` | 一键部署（含 BepInEx） | `--cfg-guid` `--dry-run` `--i-have-a-backup` |

四个工具都自带 `--selftest`，改完先跑一遍（本项目当前：`bp_parse` 全过 / `bp_reconcile` **六案** / `bp_autosite` 四案 / `bp_pipeline` 三案）。

---

## 九、提交前自查

- [ ] 纯净编译（只改 csproj 占位符）→ `0 error`
- [ ] 四工具 `--selftest` 全过
- [ ] 实机完整重建跑通：`prefab 校验失败 0` + `Δy≠累计位移 ≤ 3`
- [ ] 对账**双口径都能跑完**且都 PASS
- [ ] `Terrain.Enabled=true` + `Carve=true`：**0 异常** + `重建调用链缺 0` + `terrain_done.flag` 已写
- [ ] 反向验证：去掉一个重建方法 → 走 `Miss` 分支而非抛异常
- [ ] 新增的 selftest 案能覆盖本次修复的分支

---

## 附：本项目的两条硬经验

1. **"编译通过"只覆盖一半。** 本项目连续两个 bug（#19 签名、#22 参数个数）都是**编译全过、运行必崩**。静态检查一个都抓不到 —— **"能不能跑"只能起真环境验。**

2. **用来自证的判据，必须先证明它自己在动。** 「支撑不足 0」曾被用来**排除**支撑假设，结果那次排除是错的（真凶是磨损系统）。现在改成打印取值域 `[100.0,1500.0]` + 越界计数 —— 一眼能分辨"真没问题"和"反射拿错字段"。**别用可能坏掉的判据下反向结论。**
