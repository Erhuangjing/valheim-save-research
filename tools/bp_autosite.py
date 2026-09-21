# -*- coding: utf-8 -*-
"""AutoFindFlat —— 离线自动挑落点（七段流水线的「落点选择」缺口，调研 §4.3 最后一格）。

为什么只能做"代理测高"：
  1.0 存档里**没有**基岩地形高度——地形由种子程序生成（种子明文在 `_main.<N>.fwl2`，
  但复刻 WorldGenerator 是另一个量级的工程）。存档里有的、且能离线读的：
  - 持久 ZDO 记录（锚点法识别率实测 99.11%，地形delta核查报告 §2.1）；
    其中树 / 岩石 / 可采集物都是贴地生成 → **它们的 y ≈ 地表高度**；
  - 只有玩家改造过的区域才有地形 delta 块（对我们选"天然平地"恰好无用）。

判据（对齐落地实践）：
  - 平坦：样本 y 的 p95−p5 ≤ max_spread（默认 2m，锚点求解 MaxShift=±2m 的安全余量）；
  - 陆地：样本中位数 > 水位+缓冲（水位 30，缓冲 1m）；
  - 无人区：候选格内零建筑件（前缀启发式识别玩家结构）；
  - 样本够：≥ min_samples（默认 8），否则标 sparse（罕见踩点区可能整片没记录—— caveat）。

产出：合格格按（起伏, 自然物数）升序 = 又平又干净优先；第一名即推荐落点，
PlatformY = 样本中位数（round 2 位，供 cfg 直填）。

用法：
    python bp_autosite.py --world <世界目录> [--manifest runs/x/manifest.json] \
        [--margin 16] [--min-samples 8] [--max-spread 2] [--water-level 30] [--top 10] [--json out.json]
    python bp_autosite.py --selftest
"""
import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bp_reconcile        # noqa: E402  复用 full_scan / load_hashlib（同一套锚点启发式）

WATER_LEVEL = 30.0         # Valheim 海平面（usagi 落点 PlatformY=39.4 可对照）

# 贴地自然物（y ≈ 地表）。前缀按 format/prefab-hashlib.json 实测命名校准：
#   树 = tree1 / Beech1 / Birch1|2 / Oak1 / PineTree / FirTree / SwampTree1 / Yggdrasil /
#        AshlandsTree* / Stubbe(树桩)；岩 = Rock* / rock1 / silvervein / mudpile；
#        灌木采集 = Pickable_* / Bush01(_raspberry/_blueberry…)。前缀启发式，误伤/误漏只影响统计。
NATURE_TREE = ('tree', 'beech', 'birch', 'oak', 'pinetree', 'firtree', 'spruce',
               'yggdrasil', 'ashlands', 'swamptree', 'stubbe')
NATURE_ROCK = ('rock', 'silvervein', 'mudpile')
NATURE_PICK = ('pickable', 'bush', 'shrub', 'vines', 'raspberry', 'blueberry',
               'thistle', 'mushroom', 'carrot', 'turnip', 'barley', 'flax')

# 玩家结构启发式（误漏只是少一层保护、误伤只是多拒一个格子，都可控；REF：非精确清单）
STRUCTURE_PREFIXES = (
    'wood_', 'stone_', 'iron_', 'darkwood_', 'grausten_', 'granite_', 'marble_',
    'blackmarble_', 'ashwood_', 'crystal_', 'piece_', 'portal', 'sign', 'ward',
    'guardstone', 'windmill', 'spinningwheel', 'blastfurnace', 'fermenter',
    'smelter', 'kiln', 'forge', 'cauldron', 'braziers', 'lantern', 'campfire',
    'firepit', 'hearth', 'chest', 'karve', 'raft', 'cart',
)

BIN = 8.0                  # 样本空间分箱粒度（米）


def classify(name):
    n = name.lower()
    if n.startswith(NATURE_TREE):
        return 'tree'
    if n.startswith(NATURE_ROCK):
        return 'rock'
    if n.startswith(NATURE_PICK):
        return 'pickable'
    if n.startswith(STRUCTURE_PREFIXES):
        return 'structure'
    return 'other'


def _pct(sorted_ys, q):
    return sorted_ys[min(len(sorted_ys) - 1, int(round(q * (len(sorted_ys) - 1))))]


def find_sites(records, tile_w=64.0, tile_d=64.0, water_level=WATER_LEVEL,
               min_samples=8, max_spread=2.0, top=10):
    """records = [(hash, x, y, z)]（bp_reconcile.full_scan 输出）。

    返回 (sites, reject_counts)：
      sites   = [{'site_x','site_z','platform_y','spread','samples','trees','rocks',
                  'pickables','nature_total','buildings'}, ...] 按 (spread, nature_total) 升序
      rejects = {'built': n, 'sparse': n, 'water': n, 'slope': n}
    """
    samples = []            # (x, y, z, kind)
    builds = []             # (x, z)
    for h, x, y, z in records:
        kind = classify(_init_name_cache().get(h, ''))
        if kind in ('tree', 'rock', 'pickable'):
            samples.append((x, y, z, kind))
        elif kind == 'structure':
            builds.append((x, z))

    if not samples:
        return [], {'built': 0, 'sparse': 0, 'water': 0, 'slope': 0, 'no_samples': 1}

    # 空间分箱
    sbin, bbin = defaultdict(list), defaultdict(list)
    for x, y, z, kind in samples:
        sbin[(int(x // BIN), int(z // BIN))].append((x, y, z, kind))
    for x, z in builds:
        bbin[(int(x // BIN), int(z // BIN))].append((x, z))

    xs = [s[0] for s in samples]
    zs = [s[2] for s in samples]
    step = max(8.0, min(tile_w, tile_d) / 2.0)
    hw, hd = tile_w / 2.0, tile_d / 2.0

    def bins_for(cx, cz):
        for bx in range(int((cx - hw) // BIN), int((cx + hw) // BIN) + 1):
            for bz in range(int((cz - hd) // BIN), int((cz + hd) // BIN) + 1):
                yield bx, bz

    sites, rejects = [], defaultdict(int)
    cx = min(xs) + hw
    while cx <= max(xs) - hw + 1e-9:
        cz = min(zs) + hd
        while cz <= max(zs) - hd + 1e-9:
            ss = [s for bx, bz in bins_for(cx, cz) for s in sbin.get((bx, bz), [])]
            nb = sum(len(bbin.get((bx, bz), [])) for bx, bz in bins_for(cx, cz))
            if nb > 0:
                rejects['built'] += 1
            elif len(ss) < min_samples:
                rejects['sparse'] += 1
            else:
                ys = sorted(s[1] for s in ss)
                med = ys[len(ys) // 2]
                if med <= water_level + 1.0:
                    rejects['water'] += 1
                else:
                    spread = _pct(ys, 0.95) - _pct(ys, 0.05)
                    if spread > max_spread:
                        rejects['slope'] += 1
                    else:
                        sites.append({
                            'site_x': round(cx, 1), 'site_z': round(cz, 1),
                            'platform_y': round(med, 2), 'spread': round(spread, 2),
                            'samples': len(ss),
                            'trees': sum(1 for s in ss if s[3] == 'tree'),
                            'rocks': sum(1 for s in ss if s[3] == 'rock'),
                            'pickables': sum(1 for s in ss if s[3] == 'pickable'),
                            'buildings': nb,
                        })
            cz += step
        cx += step

    sites.sort(key=lambda s: (s['spread'], s['samples']))
    return sites[:top], dict(rejects)


# ---------------------------------------------------------------- 名字缓存
_NAME_CACHE = None


def _init_name_cache():
    global _NAME_CACHE
    if _NAME_CACHE is None:
        _NAME_CACHE = bp_reconcile.load_hashlib()
    return _NAME_CACHE


# ---------------------------------------------------------------- 自检
def selftest():
    """五个试验区：A 平地 / B 陡坡 / C 水下 / D 有建筑 / E 样本稀疏。"""
    import random
    rnd = random.Random(7)
    recs = []
    lib = _init_name_cache()
    hs = bp_reconcile.stable_hash

    def put(name, x, y, z):
        recs.append((hs(name), x, y, z))

    for i in range(40):                                   # A：平地空地
        put('Beech1', 100 + rnd.uniform(0, 64), 40 + rnd.uniform(-0.25, 0.25), 100 + rnd.uniform(0, 64))
    for i in range(40):                                   # B：9.6m 落差的坡
        x = 300 + rnd.uniform(0, 64)
        put('Birch1', x, 40 + (x - 300) * 0.15, 100 + rnd.uniform(0, 64))
    for i in range(30):                                   # C：水下（y≈28.5）
        put('Rock3', 500 + rnd.uniform(0, 50), 28.5 + rnd.uniform(-0.2, 0.2), 100 + rnd.uniform(0, 50))
    for i in range(20):                                   # D：平地但有人建了房
        put('FirTree', 700 + rnd.uniform(0, 50), 41 + rnd.uniform(-0.2, 0.2), 100 + rnd.uniform(0, 50))
    for i in range(6):
        put('wood_floor', 720 + i * 3, 42, 120)
    for i in range(3):                                    # E：样本稀疏
        put('Yggdrasil', 900 + i * 8, 39.5, 100 + i * 8)

    assert lib.get(hs('wood_floor')) == 'wood_floor'
    assert classify('Beech1') == 'tree' and classify('Rock7_long') == 'rock' \
           and classify('PineTree') == 'tree' and classify('Bush01_raspberry') == 'pickable'
    assert classify('Pickable_Mushroom') == 'pickable' and classify('wood_floor') == 'structure'

    sites, rej = find_sites(recs, tile_w=48, tile_d=48)
    okA = bool(sites) and abs(sites[0]['platform_y'] - 40) < 1.0 and sites[0]['spread'] < 0.8 \
        and 90 <= sites[0]['site_x'] <= 170
    okB = all(90 <= s['site_x'] <= 170 or 290 <= s['site_x'] <= 370 or
              s['platform_y'] > 31 for s in sites)      # 无水下/陡坡混入合格区
    okC = rej.get('slope', 0) > 0 and rej.get('water', 0) > 0 and rej.get('built', 0) > 0 \
        and rej.get('sparse', 0) > 0
    print('A 推荐落点     : (%.1f, %.1f) 高 %.2f 起伏 %.2f 样本 %d → %s'
          % (sites[0]['site_x'], sites[0]['site_z'], sites[0]['platform_y'],
             sites[0]['spread'], sites[0]['samples'], 'PASS' if okA else 'FAIL'))
    print('B 合格区纯净   : %d 个合格格全部在平地/高地 → %s' % (len(sites), 'PASS' if okB else 'FAIL'))
    print('C 四类拒绝计数 : slope=%d water=%d built=%d sparse=%d → %s'
          % (rej.get('slope', 0), rej.get('water', 0), rej.get('built', 0), rej.get('sparse', 0),
             'PASS' if okC else 'FAIL'))
    ok = okA and okB and okC
    print('selftest:', '三案 %s' % ('全部通过 ✓' if ok else '存在失败 ✗'))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description='AutoFindFlat：离线扫存档挑天然平地落点（自然物 y 代理测高）')
    ap.add_argument('--world', help='世界存档目录（含 .chunk）')
    ap.add_argument('--manifest', help='manifest.json（用蓝图占地定格子尺寸）')
    ap.add_argument('--margin', type=float, default=16.0, help='格子=占地+2×margin（默认 16）')
    ap.add_argument('--min-samples', type=int, default=8)
    ap.add_argument('--max-spread', type=float, default=2.0, help='格内 p95-p5 起伏上限（米）')
    ap.add_argument('--water-level', type=float, default=WATER_LEVEL)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--json', help='结果 JSON 输出路径')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not a.world:
        ap.error('--world 必填（或 --selftest）')

    fw = fd = 32.0
    if a.manifest and os.path.isfile(a.manifest):
        with open(a.manifest, encoding='utf-8') as f:
            m = json.load(f)
        xs = [p['x'] for p in m['pieces']] or [0]
        zs = [p['z'] for p in m['pieces']] or [0]
        fw = max(32.0, max(xs) - min(xs))
        fd = max(32.0, max(zs) - min(zs))
    tile_w, tile_d = fw + 2 * a.margin, fd + 2 * a.margin

    hslib = _init_name_cache()
    records = bp_reconcile.full_scan(a.world, hslib)
    sites, rej = find_sites(records, tile_w=tile_w, tile_d=tile_d,
                            water_level=a.water_level, min_samples=a.min_samples,
                            max_spread=a.max_spread, top=a.top)
    print('扫描记录 %d 条 | 格子 %.0f×%.0f m | 判据: 起伏≤%.1fm 水位线 %.0f 样本≥%d'
          % (len(records), tile_w, tile_d, a.max_spread, a.water_level, a.min_samples))
    print('拒绝统计: %s' % (rej or '（无）'))
    if not sites:
        print('✗ 没有合格落点。放宽 --max-spread / --min-samples，或换一片区域（稀疏=少人踩点，'
              '可能反而空旷但无法验平）')
        return 1
    print('%-14s %-14s %-8s %-7s %-6s %s' % ('site_x', 'site_z', 'PlatformY', '起伏', '样本', '自然物(树/岩/采集)'))
    for s in sites:
        print('%-14.1f %-14.1f %-8.2f %-7.2f %-6d %d/%d/%d'
              % (s['site_x'], s['site_z'], s['platform_y'], s['spread'], s['samples'],
                 s['trees'], s['rocks'], s['pickables']))
    b = sites[0]
    print('推荐落点: (%.1f, %.1f)  PlatformY=%.2f  （又平又干净优先；起服前建议游戏内目视复核一次）'
          % (b['site_x'], b['site_z'], b['platform_y']))
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump({'tile': [tile_w, tile_d], 'criteria': {'max_spread': a.max_spread,
                       'water_level': a.water_level, 'min_samples': a.min_samples},
                       'rejects': rej, 'sites': sites, 'best': b}, f, ensure_ascii=False, indent=1)
        print('✓ 结果 → %s' % a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
