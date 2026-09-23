# tools/ 目录说明

这里有两类脚本，**定位完全不同**，用之前先看这张表。

| 类别 | 文件 | 定位 | 能用吗 |
|---|---|---|---|
| **工具**（5 个） | `bp_parse.py` / `bp_reconcile.py` / `bp_autosite.py` / `bp_pipeline.py` / `bp_terrain_report.py` | 为复用而设计：自洽、带 `--selftest`、参数化 | ✅ clone 下来即可用 |
| **研究脚本**（17 个） | 其余 `*.py`（含 `zdo_lib.py` 库） | 当时的**实验过程记录**，内部硬编码了具体世界名 / chunk 文件名 / 备份目录名 | ⚠️ 见下方说明 |

---

## 一、五个工具

> 给 agent 用的完整流程（问什么、跑什么、怎么验收、出错怎么办）在 **`skills/valheim-blueprint-build/SKILL.md`**。

```bash
# 自检（不依赖任何外部数据）
python tools/bp_parse.py          --selftest
python tools/bp_reconcile.py      --selftest
python tools/bp_autosite.py       --selftest
python tools/bp_pipeline.py       --selftest
python tools/bp_terrain_report.py --selftest

# 离线挑落点：只在大致方位附近找、避开保护圈（主宅 + 护城河）
python tools/bp_autosite.py --world "$VH_WORLD" --manifest out/manifest.json \
    --near-x -300 --near-z 260 --near-r 120 --protect-x -302 --protect-z 262 --protect-r 40

# 地形验收：执行器 dump → 断崖 / 保护圈 / 平台误差 / 持久化 + 三联对比图
python tools/bp_terrain_report.py runs/1/bp_terrain_dump.csv --png runs/1/terrain.png

# 解析蓝图 → manifest / 件清单
python tools/bp_parse.py <某.blueprint> --json out/manifest.json --txt out/pieces.txt

# 离线挑落点（扫存档找天然平地）
python tools/bp_autosite.py --world "$VH_WORLD" --manifest out/manifest.json --top 10

# 落地后对账
python tools/bp_reconcile.py --manifest out/manifest.json --world "$VH_WORLD" \
    --origin-x -262 --origin-z 270 --platform-y 39.4 --ground-py -1.6 --min-rate 1.0

# 七段编排（一条命令跑完 preflight → report）
python tools/bp_pipeline.py all --bp <某.blueprint> --work runs/1 \
    --world "$VH_WORLD" --server "$VH_SERVER" --i-have-a-backup

# 承重验收（落地后必做）：关支撑锁 + 区域激活，原版承重跑 5 分钟，看件数时序与承重色；
# 塌件逐件写 runs/1/bp_observe_lost.csv（prefab、材质、蓝图坐标）
python tools/bp_pipeline.py observe --work runs/1 --server "$VH_SERVER" --bat <启动脚本> --observe-minutes 5
```

`bp_pipeline.py` 有**铁律内建**：动档前必须有备份记录（`--i-have-a-backup`），
`install` 段会隔离服务器原有 BepInEx 而非删除，`Force` 永远写 `false`。

---

## 二、环境变量（路径配置）

所有脚本的外部路径都走环境变量，**不再硬编码任何本机绝对路径**。
未设置时脚本会直接报错并給出变量名，不会抛莫名的 `FileNotFoundError`。

| 变量 | 含义 | 示例 |
|---|---|---|
| `VH_WORLD_ROOT` | `worlds_local` 根目录（下面有多个世界） | `<服务器>/save/worlds_local` |
| `VH_WORLD` | **单个**世界目录（含 `.chunk` / `.chunks`） | `<服务器>/save/worlds_local/<世界名>` |
| `VH_SERVER` | Valheim dedicated server 安装目录 | `<你的 Steam 库>/steamapps/common/Valheim dedicated server` |
| `VH_BACKUP` | 备份输入/输出根目录 | 任意可写目录 |
| `VH_WORLD_TEST` | 测试用存档根目录（`bp_land.py` 用） | 任意 |

`VH_WORLD` 与 `VH_WORLD_ROOT` 的区别是「指向世界本身」还是「指向世界的父目录」，
两个脚本族各用其中一个，按报错提示填即可。

**仓库内资源不需要配置**：`format/prefab-hashlib.json`（57,706 条 prefab 名 ↔ 哈希）
由脚本按「自身所在目录」自动定位，clone 下来就能用。

```bash
# 例（Git Bash）
export VH_WORLD_ROOT="<你的 Steam 库>/steamapps/common/Valheim dedicated server/save/worlds_local"
export VH_WORLD="$VH_WORLD_ROOT/<世界名>"
export VH_SERVER="<你的 Steam 库>/steamapps/common/Valheim dedicated server"
```

---

## 三、关于那 17 个研究脚本

它们是逆向 1.0 存档格式那段时间的**实验记录**，每份对应一次具体实验。特征是内部硬编码了当时的实验参数，例如：

```python
# chunks_index.py —— 对比两个具体时间点的备份
'20:39': 'WORLD_backup_auto-20260916-203929',
'22:46': 'WORLD_backup_auto-20260916-224637',

# zdo_v10.py —— 默认只看某一个 chunk
cf = sys.argv[1] if len(sys.argv) > 1 else '20_1e__1_16.chunk'
```

**所以它们不是「配置一下就能跑」的工具**：除了环境变量，还要把里面的世界名 / chunk 名 /
备份目录名换成你自己的。这么设计是刻意的 —— 这些是**证据留存**，保留原始参数
才能让当时的结论可复核。

**要用的时候怎么挑**：

| 想干什么 | 看哪个 |
|---|---|
| 按坐标 / 名字找实体、解析 ZDO 记录 | `zdo_lib.py`（库，`hs()` / `walk()` / `candidates()`）、`zdo_v10.py` |
| 看 chunk 索引表、统计各 zone 的 ZDO 数 | `chunks_index.py`、`chunk_walk10.py` |
| 解析 `db2`（gzip）看全局键 | `db2_probe.py` |
| 对比两个时间点的地形 / 实体差异 | `anchor_diff.py` |
| 传送门相关（列举 / 精确定位） | `precise_portal.py`、`scan_all_portals.py`、`dump_portal.py` |
| 清除角色档的 `devcommands` 标记 | `clear_devcommands.py` |
| 反查字段名 / 哈希 | `find_field_name.py` |

**`zdo_lib.py` 是唯一被其他脚本 import 的模块**，所以它的改动影响面最大 ——
本次改造后它从「读本机 `ref/hashlib.json`」变成了「读仓库内 `format/prefab-hashlib.json`」，
这是让这批脚本在别人机器上**至少能 import 成功**的关键一步。

---

## 四、依赖

只用 Python 标准库（`os` / `struct` / `json` / `gzip` / `zlib` / `hashlib` 等）。
无第三方依赖，无需 `pip install`。
