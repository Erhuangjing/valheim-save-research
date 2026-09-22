# Valheim 蓝图自动落地插件 · 使用说明

**状态：已装好，但处于「未启用」状态**（配置文件里 `Enabled = false`），你确认落点后改一个字就能用。

---

## 一、这是什么

一个跑在**服务器端**的 BepInEx 插件。启动服务器时，它会用 Valheim 自己的 ZDO API 把蓝图里的 1,805 个建筑件**真实创建**到你的世界里 —— 不是幽灵件、不是模组方块。

| | PlanBuild 方案 | **本插件方案** |
|---|---|---|
| 你自己要装 mod | 要（客户端 + 服务器） | **不用** |
| 朋友要装 mod | 不用 | 不用 |
| 落地后需要留 mod | 要 | **不用（可删）** |
| 操作方式 | 游戏内蓝图 rune 手摆 | **启动服务器自动完成** |

---

## 二、已装了什么（都在服务器目录，**没覆盖任何原有文件**）

```
<VALHEIM_SERVER_DIR>\
├── winhttp.dll                    ← 新增（BepInEx 入口，DLL 注入方式）
├── doorstop_config.ini            ← 新增
├── doorstop_libs\                 ← 新增
└── BepInEx\                       ← 新增
    ├── core\                      ← BepInEx 本体 + Harmony
    ├── plugins\XiBpBuilder.dll    ← ★ 我写的插件
    └── config\
        ├── bp_pieces.txt          ← 1,805 件「只要结构」
        ├── bp_pieces_full.txt     ← 2,053 件「含家具」（备选）
        ├── com.world.bpbuild.cfg  ← ★ 配置（落点在这里改）
        └── bpbuild.log            ← 运行日志
```

**完全可逆**：删掉 `winhttp.dll` + `doorstop_config.ini` + `doorstop_libs` + `BepInEx` 这四个东西，服务器就恢复原样。

---

## 三、怎么启用（就一步）

打开 `BepInEx\config\com.world.bpbuild.cfg`，把：

```ini
Enabled = false
```

改成：

```ini
Enabled = true
```

然后**双击 `start_headless_server.bat` 启动服务器**。启动过程中会看到日志（也在 `BepInEx\config\bpbuild.log`）：

```
件清单载入 1805 件 | 落点 (-266,37,270)
捕获 ZNet.LoadWorld 完成
第 1 批创建 150 个，累计 150/1805
...
★ 全部 1805 件创建完成（哈希与游戏不一致 0 个）
```

约 **10 秒**建完，然后自动保存。进游戏就能看到。

**建完会自动写一个 `BepInEx\config\bp_built.flag`，之后重启服务器不会重复创建。**（想再建一次就删掉这个文件）

---

## 四、落点在哪 / 怎么调

当前预设落点：**(-266, 36.6, 269.5)** —— 你家主宅 (-305, 267) 东侧约 40 米，两栋不重叠。

在 `com.world.bpbuild.cfg` 里改这三行即可：

```ini
OriginX = -266      ← 东西方向
OriginY = 36.6      ← 高度（地面），建筑会以此往上长
OriginZ = 269.5     ← 南北方向
```

**怎么定坐标**：进游戏走到想放的位置，按 **F5** 打开控制台，输入 `pos` → 会显示你的当前坐标，把它填进这三个值即可（Y 用你脚下的地面高度）。

> 建筑是"以蓝图中心为基准"展开的，占地约 28 × 30 米。

---

## 五、⚠️ 重要：不满意怎么撤销

**建筑一旦落地就是世界数据，插件不做删除功能。** 所以：

### 方式 1：从快照恢复（推荐，最干净）

我已经在你落地前存了一份世界快照：

```
<BACKUP_DIR>\pre_blueprint_20260914-181738\WORLD\
```

不满意的话，**先停服**，然后把 `save\worlds_local\WORLD` 整个替换成这份快照，重启即可完全还原。

**⚠️ 注意**：恢复会把"快照之后的所有游戏进度"也一起回退。所以**试之前最好别先玩很久** —— 建议流程是：启用 → 看效果 → 满意才继续玩。

### 方式 2：只要回滚建筑、保留进度

告诉我，我写一个「按件清单删除建筑」的配套插件（按位置+构件类型匹配，只删这批件）。这个可以做，只是要多花点时间验证删除逻辑，所以没跟创建功能一起上。

---

## 六、技术细节（想了解的话）

- **原理**：Harmony 补丁挂在 `ZNet.LoadWorld` 上，世界加载完成后，逐条调用
  `ZDOMan.instance.CreateNewZDO(pos, hash)` + `SetPosition` / `SetRotation` / `Persistent=true` / `SetOwner(0)`
- **构件哈希**：用游戏自己的 `ZNetScene.GetPrefabHash(prefab)`，实测与我外部计算的 StableHash **100% 一致**（不一致 0/1805）
- **保存**：反射调用 `ZNet.DelayedSave(true)` 协程（`SaveWorldAndPlayerProfiles()` 走 RPC 路径会 NRE，不能用）
- **不需要人在游戏里**：实测在服务器无玩家的状态下就能创建 + 保存成功（ZDO 数量 83,136 → 84,941，chunk 归入正确的 `20_1e`）；配置项 `WaitForZoneLoad` / `IgnoreZoneCheck` 保留了"等区域加载"的模式以备特殊地形用
- **已实测验证**：创建后重新加载该世界 → `load 84,941 zdos`、零异常、`Game server connected`
- **兼容性**：**与 `-crossplay` 不兼容**（BepInEx 通病），你的服务器没开 crossplay ✓

---

## 七、为什么之前那条路（裸写存档）走不通

顺带说明一下前面那一大轮逆向的结论：Valheim 1.0 的 chunk 里，ZDO 记录是**按每个 prefab 自己的字段定义顺序**序列化的，还夹着一个"额外数据区"，两区边界与记录数/长度严格绑定。20 组对照实验证明：手工追加/插入记录必崩，只能等长替换，且批量替换会破坏字段长度前缀。

**结论是：写存档这条路不可能可靠实现**。而这次走"游戏进程内调 API"，等于让游戏自己按它的规则写 —— 一次就通了。
