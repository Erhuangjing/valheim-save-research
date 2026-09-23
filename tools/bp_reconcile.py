# -*- coding: utf-8 -*-
"""离线存活对账（七段流水线第 6 段）：蓝图期望件 vs 世界存档实况。

填补《skill 可行性调研》§4.3 缺口表中的「对账脚本（usagi_collapse.py）未入库」。
匹配规则 = 交接文档 §9.4 进程内对账同款 key：
    hash | round(x*4) | round(y*4) | round(z*4)
即「名字 + 位置双匹配、y 也量化」（坑 C.2：同 (x,z) 可叠放多件，y 不量化必误判）。

存档布局（1.0 chunked，本仓库 zdo_v10/chunks_index/anchor_diff 实测同款）：
    <世界目录>/ _main.*.chunks（索引） + _main.*.chunk（记录流）
    记录 = [flags u16][pos 3×f32][prefab hash u32][rot?][conn?][segments]
    有效记录启发式：flags 含 0x0100（有位置）、不含 0x8000、hash 在哈希库、坐标 sane。

坐标换算与参考插件 BuildPieces 完全一致（tools/xibpbuilder_reference/Plugin.cs）：
    wx = OriginX + (px - cx)      # cx/cz = 选用件包围盒中心
    wy = (PlatformY - GroundLayerPy) + py + YOffset + sink
    wz = OriginZ + (pz - cz)

用法：
    python bp_reconcile.py --manifest runs/h1/manifest.json --world <世界目录> \
        --origin-x -262 --origin-z 270 --platform-y 39.4 --ground-py -1.6 \
        [--sink -0.35] [--min-rate 1.0] [--margin 15] [--out runs/h1/reconcile.json]
    python bp_reconcile.py --selftest
"""
import argparse
import collections
import json
import math
import os
import struct
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HASHLIB_PATH = os.path.join(REPO, 'format', 'prefab-hashlib.json')


# ---------------------------------------------------------------- 哈希
def stable_hash(s):
    """Valheim StableHash（与 bp_parse.py / land_official.py 同款实现）。"""
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(s[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF


def load_hashlib():
    with open(HASHLIB_PATH, encoding='utf-8') as f:
        return {int(k): v for k, v in json.load(f).items()}


# ---------------------------------------------------------------- 期望侧
def transform_pieces(manifest, ox, oz, platform_y, ground_py, yoffset, sink, ignore_y=False, rotation=0.0):
    """蓝图局部坐标 → 世界坐标（与参考插件 ToWorld 同式）。返回带 key 的列表。

    rotation（度，俯视顺时针 = Unity yaw）：绕蓝图包围盒中心旋转，与插件 [Build] Rotation 一致：
    Quaternion.Euler(0,R,0) * (dx,0,dz) = (dx·cosR + dz·sinR, −dx·sinR + dz·cosR)

    ignore_y（实测方法论）：位移缺陷会让 y 出 0.25 量化尖刺（+0.25→58 / −0.25→122 /
    −1.25→1127 件），拿 y 匹配会得出错误的存活率。交叉验证时可用 (hash,x,z) 三元组匹配；
    坑 C.2 的默认仍含 y（同 (x,z) 叠放多件需 y 区分），两口径都在输出里报告。
    """
    cx = manifest['bbox_center']['x']
    cz = manifest['bbox_center']['z']
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    out = []
    for p in manifest['pieces']:
        dx, dz = p['x'] - cx, p['z'] - cz
        wx = ox + dx * c + dz * s
        wy = (platform_y - ground_py) + p['y'] + yoffset + sink
        wz = oz - dx * s + dz * c
        h = p.get('hash') or stable_hash(p['name'])
        out.append({'name': p['name'], 'hash': h, 'x': wx, 'y': wy, 'z': wz,
                    'key': key_of(h, wx, wy, wz, use_y=not ignore_y)})
    return out


def key_of(h, x, y, z, use_y=True):
    """交接文档 §9.4：hash|round(x*4)|round(y*4)|round(z*4)。round 同 C# ToEven。

    use_y=False → (hash, x, z) 三元组（y 有 0.25 量化尖刺时的交叉验证口径）。
    """
    if use_y:
        return (h, round(x * 4), round(y * 4), round(z * 4))
    return (h, round(x * 4), round(z * 4))


def key_xyz(k, ignore_y=False):
    """key_of 的逆：key → (x, y, z)。

    ⚠️ issue #20：key 的元数**随 ignore_y 变化**（4 元组 / 3 元组），任何按 key 反算坐标的
    地方都必须走这里，不能直接 `h, qx, qy, qz = k` —— 三元组会被硬解崩（ValueError）。
    ignore_y 时 y 无法还原，返回 None，调用方须跳过 y 相关判定。
    """
    if ignore_y:
        _, qx, qz = k
        return qx / 4.0, None, qz / 4.0
    _, qx, qy, qz = k
    return qx / 4.0, qy / 4.0, qz / 4.0


# ---------------------------------------------------------------- 存档侧
def _sane(x, y, z):
    return not (math.isnan(x) or math.isnan(z) or math.isnan(y)
                or abs(x) > 20000 or abs(z) > 20000 or abs(y) > 5000)


def scan_chunks_for_hashes(world_dir, hashes):
    """快速模式：只找期望 hash 的记录（bytes.find 驱动，真实大文件也快）。

    返回 [(hash, x, y, z)]。hash 出现在段数据里造成的假阳性由 flags 检查滤掉大部分，
    残余噪声只可能污染 extras，不可能污染 matched/missing（key 必须精确相等）。
    """
    records = []
    chunk_files = sorted(f for f in os.listdir(world_dir) if f.endswith('.chunk'))
    if not chunk_files:
        raise SystemExit('✗ 世界目录里没有 .chunk 文件（%s）\n'
                         '  1.0 chunked 布局才有 .chunk；旧版单文件 .db 不在本工具支持范围' % world_dir)
    pats = [(h, struct.pack('<I', h)) for h in hashes]
    for fn in chunk_files:
        with open(os.path.join(world_dir, fn), 'rb') as f:
            data = f.read()
        n = len(data)
        for h, pat in pats:
            i = data.find(pat)
            while i >= 0:
                p = i - 14                      # 记录起点（hash 前面是 12B 坐标 + 2B flags）
                if p >= 0:
                    fl, = struct.unpack_from('<H', data, p)
                    if (fl & 0x0100) and not (fl & 0x8000):
                        x, y, z = struct.unpack_from('<fff', data, p + 2)
                        if _sane(x, y, z):
                            records.append((h, x, y, z))
                i = data.find(pat, i + 1)
    return records, chunk_files


def full_scan(world_dir, hslib):
    """全景模式：逐字节启发式扫所有记录。

    ⚠️ 观察用，不可用于验收（issue #5）：启发式要求 hash 在名字表里，实测识别率
    ≈99.8%（259,823/260,459），会漏「名字表未收录」的合法记录与个别 flags 组合，
    造成验收假阴性。验收走 scan_chunks_for_hashes（hash 定向，不受名字表影响）
    + identity_check / precise_scan（索引恒等式与逐 chunk 计数核对）。
    """
    records = []
    for fn in sorted(f for f in os.listdir(world_dir) if f.endswith('.chunk')):
        with open(os.path.join(world_dir, fn), 'rb') as f:
            data = f.read()
        n = len(data)
        for p in range(6, n - 18):          # 6B 头 [ver u16][count i32] 之后才是记录流
            fl, = struct.unpack_from('<H', data, p)
            if not (fl & 0x0100) or (fl & 0x8000):
                continue
            ph, = struct.unpack_from('<I', data, p + 14)
            if ph not in hslib:
                continue
            x, y, z = struct.unpack_from('<fff', data, p + 2)
            if _sane(x, y, z):
                records.append((ph, x, y, z))
    return records


# ---------------------------------------------------------------- 索引恒等式与精确扫描（issue #5）
def index_total(world_dir):
    """读 _main.<N>.chunks 索引头部 → (ver, total_zdo, zone_count)；无索引返回 None。

    布局（chunks_index.py 实测）：[ver u16][total u32][zone 数 u32] + zone 条目。
    """
    idx = sorted(f for f in os.listdir(world_dir) if f.endswith('.chunks'))
    if not idx:
        return None
    with open(os.path.join(world_dir, idx[0]), 'rb') as f:
        b = f.read(10)
    if len(b) < 10:
        return None
    ver, total, n = struct.unpack_from('<HII', b, 0)
    return ver, total, n


def chunk_counts(world_dir):
    """各 .chunk 文件头 [WorldVersion u16][ZDO count i32] 的记录数。"""
    out = {}
    for fn in sorted(f for f in os.listdir(world_dir) if f.endswith('.chunk')):
        with open(os.path.join(world_dir, fn), 'rb') as f:
            hdr = f.read(6)
        out[fn] = struct.unpack_from('<i', hdr, 2)[0] if len(hdr) >= 6 else None
    return out


def identity_check(world_dir):
    """验收前置恒等式：sum(各 chunk 头计数) == 索引 total。

    实测该恒等式在精确解析下成立（260,459 == 260,459）。返回
    {'index': total 或 None, 'chunk_sum': sum, 'ok': True/False/None(无索引无法判定)}。
    """
    idx = index_total(world_dir)
    counts = chunk_counts(world_dir)
    s = sum(c for c in counts.values() if c is not None)
    return {'index': idx[1] if idx else None, 'chunk_sum': s,
            'zones': len(counts), 'ok': (idx[1] == s) if idx else None}


def precise_scan(world_dir):
    """精确扫描（验收级）：锚点启发式去掉「hash 在名字表」过滤 + 逐 chunk 计数核对。

    与 full_scan 的差异：不查名字表 → 「表未收录 hash」的合法记录也能读出；
    并用 chunk 头计数核对每个文件是否「找齐」（found == header count）。
    返回 {'records': [(hash,x,y,z)], 'per_chunk': [{file,want,found,exact}],
          'found': 总命中, 'want': 总应查, 'exact_chunks': n}。
    仍非逐段解析（变长段未解码），flags 组合类漏检以 found<want 形式暴露，不隐藏。
    """
    records, per_chunk = [], []
    for fn in sorted(f for f in os.listdir(world_dir) if f.endswith('.chunk')):
        with open(os.path.join(world_dir, fn), 'rb') as f:
            data = f.read()
        want = struct.unpack_from('<i', data, 2)[0] if len(data) >= 6 else None
        n = len(data)
        found = 0
        for p in range(6, n - 18):          # 跳过 6B 头
            fl, = struct.unpack_from('<H', data, p)
            if not (fl & 0x0100) or (fl & 0x8000):
                continue
            x, y, z = struct.unpack_from('<fff', data, p + 2)
            if not _sane(x, y, z):
                continue
            # 反假阳性（0 填充/跨字段误读）：真实 ZDO 的世界坐标不会是退化值，hash 不会为 0
            if abs(x) < 1e-6 or abs(z) < 1e-6:
                continue
            h, = struct.unpack_from('<I', data, p + 14)
            if h == 0:
                continue
            records.append((h, x, y, z))
            found += 1
        per_chunk.append({'file': fn, 'want': want, 'found': found,
                          'exact': (want is not None and found == want)})
    return {'records': records, 'per_chunk': per_chunk,
            'found': len(records), 'want': sum(c['want'] or 0 for c in per_chunk),
            'exact_chunks': sum(1 for c in per_chunk if c['exact'])}


# ---------------------------------------------------------------- 对账
def reconcile(expected, found_records, margin=15.0, ignore_y=False):
    """期望（transform_pieces 输出）vs 扫描记录 → 结果字典。expected 的 key 须已按 ignore_y 生成。

    ignore_y=True 时 key 是 (hash,x,z) 三元组：匹配与 extras 判定都退化为只按 x/z
    （y 已被证明不可信，见 transform_pieces 的 0.25 量化尖刺说明）。
    """
    exp = collections.Counter(e['key'] for e in expected)
    name_of = {}
    for e in expected:
        name_of[e['key']] = e['name']
    found = collections.Counter(key_of(h, x, y, z, use_y=not ignore_y) for h, x, y, z in found_records)

    matched = missing = 0
    missing_list = []
    for k, cnt in exp.items():
        m = min(cnt, found.get(k, 0))
        matched += m
        if cnt > m:
            missing += cnt - m
            missing_list.append({'name': name_of[k], 'key': list(k), 'count': cnt - m})
    missing_list.sort(key=lambda d: -d['count'])

    # extras：只统计期望包围盒 + margin 内的多出记录（盒外是全世界原有物件，不计）
    # issue #20：key 元数随 ignore_y 变（3 元组），反算坐标一律走 key_xyz；ignore_y 时
    # 还原不出 y，extras 判定退化为只按 x/z（此时 y 本来就不可信）。
    xs = [e['x'] for e in expected]; ys = [e['y'] for e in expected]; zs = [e['z'] for e in expected]
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    z0, z1 = min(zs) - margin, max(zs) + margin
    extras = collections.Counter()
    for k, cnt in found.items():
        if k in exp:
            continue
        x, y, z = key_xyz(k, ignore_y)
        if not (x0 <= x <= x1 and z0 <= z <= z1):
            continue
        if y is not None and not (y0 <= y <= y1):
            continue
        extras[k] = cnt

    total = sum(exp.values())
    rate = matched / total if total else 1.0
    return {
        'expected': total, 'matched': matched, 'missing': missing,
        'rate': round(rate, 6),
        'missing_list': missing_list[:50],
        'extra_in_bbox': {str(list(k)): c for k, c in extras.most_common(20)},
        'extra_in_bbox_total': sum(extras.values()),
        'margin_m': margin,
    }


def verdict(res, min_rate):
    return res['matched'] == res['expected'] or res['rate'] >= min_rate


# ---------------------------------------------------------------- 自检
def _synth_chunk(path, recs):
    """合成 1.0 chunk 文件：6B 头 [WorldVersion u16=41][count i32]（真实格式，§2.1）+ 记录流。"""
    hdr = bytearray(struct.pack('<Hi', 41, len(recs)))
    out = bytes(hdr)
    for h, x, y, z in recs:
        out += struct.pack('<HfffI', 0x0104, x, y, z, h)   # flags=0x0104（有位置+V3 段）
        out += b'\x02\x00\x00\x00'                          # 段数据（I 型一个）
        out += b'\x00\x00\x00\x00\x00\x00'                  # 记录间填充
    with open(path, 'wb') as f:
        f.write(out)


def selftest():
    hslib = load_hashlib()
    names = sorted(set(hslib.values()))[:40]                # 取 40 个真实构件名
    pieces = [{'name': nm, 'hash': stable_hash(nm),
               'x': (i % 7) * 2.0 - 6.0, 'y': (i % 5) * 1.5 - 3.0, 'z': (i % 11) * 2.0 - 10.0}
              for i, nm in enumerate(names)]
    manifest = {'pieces': pieces, 'bbox_center': {'x': 0.0, 'z': 0.0}}
    ox, oz, plat, gpy, sink = -262.0, 270.0, 39.4, -1.6, -0.35
    exp = transform_pieces(manifest, ox, oz, plat, gpy, 0.0, sink)

    tmp = tempfile.mkdtemp(prefix='bprec_')
    world = os.path.join(tmp, 'WORLD')
    os.makedirs(world)

    def recs_of(sub):
        return [(e['hash'], e['x'], e['y'], e['z']) for e in sub]

    # A. 全量：应 40/40 通过
    _synth_chunk(os.path.join(world, '_main.0.chunk'), recs_of(exp))
    found, _ = scan_chunks_for_hashes(world, {e['hash'] for e in exp})
    rA = reconcile(exp, found)
    okA = rA['matched'] == 40 and rA['missing'] == 0 and verdict(rA, 1.0)

    # B. 删 3 件 + 2 件 y 挪 0.30m（出 0.25m 量化桶）→ missing=5、盒内 extras=2
    world2 = os.path.join(tmp, 'W2'); os.makedirs(world2)
    sub = [e for i, e in enumerate(exp) if i not in (0, 1, 2)]
    sub = [dict(e, y=e['y'] + 0.30) if i in (5, 6) else e for i, e in enumerate(sub)]
    _synth_chunk(os.path.join(world2, '_main.0.chunk'), recs_of(sub))
    found2, _ = scan_chunks_for_hashes(world2, {e['hash'] for e in exp})
    rB = reconcile(exp, found2)
    okB = (rB['matched'] == 35 and rB['missing'] == 5 and rB['extra_in_bbox_total'] == 2
           and not verdict(rB, 1.0) and verdict(rB, 0.85))

    # C. 盒外同 hash 干扰件 → 不进 extras；全景模式识别率 100%
    world3 = os.path.join(tmp, 'W3'); os.makedirs(world3)
    far = [(exp[0]['hash'], 8000.0, 30.0, 8000.0)]
    _synth_chunk(os.path.join(world3, '_main.0.chunk'), recs_of(exp) + far)
    found3, _ = scan_chunks_for_hashes(world3, {e['hash'] for e in exp})
    rC = reconcile(exp, found3)
    full = full_scan(world3, hslib)
    okC = rC['extra_in_bbox_total'] == 0 and len(full) == 41

    # D（issue #5）：未收录 hash 的记录 full_scan 漏 / precise_scan 命中 / 索引恒等式成立
    world4 = os.path.join(tmp, 'W4'); os.makedirs(world4)
    _synth_chunk(os.path.join(world4, '_main.0.chunk'),
                 [(0xDEADBEEF, 1.0, 40.0, 2.0)] + recs_of(exp[:3]))   # 0xDEADBEEF 不在哈希库
    with open(os.path.join(world4, '_main.0.chunks'), 'wb') as f:
        f.write(struct.pack('<HII', 41, 4, 1) + b'\x00\x00\x00\x00')
    ident = identity_check(world4)
    prec = precise_scan(world4)
    fulln = len(full_scan(world4, hslib))
    okD = (ident['ok'] is True and ident['index'] == 4
           and len(prec['records']) == 4 and prec['per_chunk'][0]['exact']
           and fulln == 3)

    # E（实测方法论③）：y 出 0.25 尖刺时——含 y 口径必 FAIL，--ignore-y 口径必须 PASS
    worldE = os.path.join(tmp, 'WE'); os.makedirs(worldE)
    recsE = [(h, x, y + 0.25, z) for h, x, y, z in recs_of(exp)]     # 全体 y +0.25（量化出桶）
    _synth_chunk(os.path.join(worldE, '_main.0.chunk'), recsE)
    foundE, _ = scan_chunks_for_hashes(worldE, {e['hash'] for e in exp})
    expNoY = [dict(e, key=key_of(e['hash'], e['x'], e['y'], e['z'], use_y=False)) for e in exp]
    rE1 = reconcile(exp, foundE)                                      # 含 y（默认）
    rE2 = reconcile(expNoY, foundE, ignore_y=True)                    # 忽略 y
    okE = (rE1['matched'] < rE1['expected']) and (rE2['matched'] == rE2['expected'])

    # F（issue #20）：--ignore-y **且 found 含盒内多余记录** —— extras 分支不能硬解 4 元组。
    # 修复前：ValueError: not enough values to unpack (expected 4, got 3)。
    # E 案 found ≡ exp（没有多余记录）→ 永远走不到 extras 分支，所以漏掉了这个崩溃。
    worldF = os.path.join(tmp, 'WF'); os.makedirs(worldF)
    movedF = [(h, x + 1.0, y + 0.25, z) for h, x, y, z in recs_of(exp)[:2]]   # x 出 0.25 桶 → ignore_y 下也是多余
    recsF = ([(h, x, y + 0.25, z) for h, x, y, z in recs_of(exp)] + movedF
             + [(exp[0]['hash'], 8000.0, 30.0, 8000.0)])                  # 盒外干扰：不计入 extras
    _synth_chunk(os.path.join(worldF, '_main.0.chunk'), recsF)
    foundF, _ = scan_chunks_for_hashes(worldF, {e['hash'] for e in exp})
    rF = reconcile(expNoY, foundF, ignore_y=True)      # ← 修复前在这行抛 ValueError
    okF = (rF['matched'] == rF['expected'] and rF['extra_in_bbox_total'] == 2)

    print('A 全量对账      : matched=%d missing=%d → %s' % (rA['matched'], rA['missing'], 'PASS' if okA else 'FAIL'))
    print('B 缺失+y漂移0.30: matched=%d missing=%d extras=%d → %s'
          % (rB['matched'], rB['missing'], rB['extra_in_bbox_total'], 'PASS' if okB else 'FAIL'))
    print('C 盒外干扰+全景 : extras=%d 全景记录=%d → %s' % (rC['extra_in_bbox_total'], len(full), 'PASS' if okC else 'FAIL'))
    print('D 精确vs全景    : 恒等式%s precise=%d/%d exact=%s full=%d → %s'
          % ('✓' if ident['ok'] else '✗', prec['found'], prec['want'],
             prec['per_chunk'][0]['exact'], fulln, 'PASS' if okD else 'FAIL'))
    print('E y尖刺双口径   : 含y %d/%d（应FAIL） 忽略y %d/%d（应全中） → %s'
          % (rE1['matched'], rE1['expected'], rE2['matched'], rE2['expected'],
             'PASS' if okE else 'FAIL'))
    print('F 忽略y+盒内多余 : 匹配 %d/%d extras=%d（应=2，盒外不计） → %s'
          % (rF['matched'], rF['expected'], rF['extra_in_bbox_total'], 'PASS' if okF else 'FAIL'))
    # G：旋转与插件 ToWorld 同式（Unity yaw 俯视顺时针：东 → 南）；转 360° 回到原位
    mG = {'bbox_center': {'x': 0.0, 'z': 0.0},
          'pieces': [{'name': 'wood_floor', 'x': 1.0, 'y': 0.0, 'z': 0.0}, {'name': 'wood_floor', 'x': 0.0, 'y': 0.0, 'z': 2.0}]}
    g90 = transform_pieces(mG, 100.0, 200.0, 40.0, 0.0, 0.0, 0.0, rotation=90.0)
    g360 = transform_pieces(mG, 100.0, 200.0, 40.0, 0.0, 0.0, 0.0, rotation=360.0)
    near = lambda a, b: abs(a - b) < 1e-9
    okG = (near(g90[0]['x'], 100.0) and near(g90[0]['z'], 199.0)       # (+1,0) → (0,−1)：东转到南
           and near(g90[1]['x'], 102.0) and near(g90[1]['z'], 200.0)   # (0,+2) → (+2,0)：北转到东
           and near(g360[0]['x'], 101.0) and near(g360[0]['z'], 200.0))
    print('G 旋转同插件式 : 90° 东→南 (%.1f,%.1f)、北→东 (%.1f,%.1f)；360° 复原 → %s'
          % (g90[0]['x'], g90[0]['z'], g90[1]['x'], g90[1]['z'], 'PASS' if okG else 'FAIL'))
    ok = okA and okB and okC and okD and okE and okF and okG
    print('selftest:', '七案 %s' % ('全部通过 ✓' if ok else '存在失败 ✗'))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description='蓝图落地离线存活对账（坑 C.2 双匹配）')
    ap.add_argument('--manifest', help='preflight/bp_parse --json 产出的期望清单')
    ap.add_argument('--world', help='世界存档目录（含 .chunk/.chunks，1.0 布局）')
    ap.add_argument('--origin-x', type=float)
    ap.add_argument('--origin-z', type=float)
    ap.add_argument('--platform-y', type=float, help='落点地表高度（cfg PlatformY）')
    ap.add_argument('--ground-py', type=float, help='蓝图地面层 py（cfg GroundLayerPy）')
    ap.add_argument('--yoffset', type=float, default=0.0)
    ap.add_argument('--sink', type=float, default=0.0, help='锚点求解最终整体位移（有符号，下沉为负）')
    ap.add_argument('--rotation', type=float, default=0.0, help='蓝图朝向（度，俯视顺时针；= cfg [Build] Rotation）')
    ap.add_argument('--min-rate', type=float, default=1.0, help='达标线（默认 1.0 = 零丢失）')
    ap.add_argument('--margin', type=float, default=15.0, help='extras 判定包围盒外扩（米）')
    ap.add_argument('--ignore-y', action='store_true',
                    help='匹配 key 去掉 y（(hash,x,z) 三元组）——y 有 0.25 量化尖刺时的交叉验证口径；'
                         '默认含 y（坑 C.2：同 (x,z) 可叠放多件）')
    ap.add_argument('--full-scan', action='store_true', help='附加全景扫描（观察用，不可用于验收：识别率≈99.8%，见 full_scan 文档）')
    ap.add_argument('--out', help='结果 JSON 输出路径')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not (a.manifest and a.world):
        ap.error('--manifest 与 --world 必填（或 --selftest）')
    if None in (a.origin_x, a.origin_z, a.platform_y, a.ground_py):
        ap.error('--origin-x/--origin-z/--platform-y/--ground-py 必填')

    with open(a.manifest, encoding='utf-8') as f:
        manifest = json.load(f)
    if 'bbox_center' not in manifest:                      # 兼容 bp_parse --json 裸输出
        pcs = manifest['pieces']
        manifest['bbox_center'] = {
            'x': (min(p['x'] for p in pcs) + max(p['x'] for p in pcs)) / 2,
            'z': (min(p['z'] for p in pcs) + max(p['z'] for p in pcs)) / 2}

    # 验收前置（issue #5）：索引恒等式不成立 → 存档损坏/解析失配，拒绝出存活率
    ident = identity_check(a.world)
    if ident['ok'] is False:
        print('✗ 验收前置检查失败：索引 total=%s ≠ 各 chunk 头计数之和=%s —— '
              '存档可能损坏或布局版本不匹配，拒绝输出存活率（宁可拒绝，不出可疑数字）'
              % (ident['index'], ident['chunk_sum']))
        return 2
    if ident['ok'] is True:
        print('✓ 索引恒等式：各 chunk 计数之和 == 索引 total == %d（%d 个 zone）'
              % (ident['index'], ident['zones']))

    exp = transform_pieces(manifest, a.origin_x, a.origin_z, a.platform_y,
                           a.ground_py, a.yoffset, a.sink, ignore_y=a.ignore_y, rotation=a.rotation)
    found, chunk_files = scan_chunks_for_hashes(a.world, {e['hash'] for e in exp})
    res = reconcile(exp, found, a.margin, ignore_y=a.ignore_y)
    if a.ignore_y:
        print('⚠ 交叉验证口径：匹配不含 y（--ignore-y）。若本结果 PASS 而含 y 口径 FAIL → 存在 Δy 漂移（issue #12 类缺陷）；'
              '本口径下 extras 判定也退化为只按 x/z（y 不可信）')
    res['identity'] = ident
    if a.full_scan:
        if a.min_rate >= 1.0:
            print('⚠ full_scan 为观察用（识别率≈99.8%，不可用于验收）；verdict 走 hash 定向扫描，不受影响')
        hslib = load_hashlib()
        prec = precise_scan(a.world)
        res['full_scan_records'] = len(full_scan(a.world, hslib))
        res['precise_scan'] = {k: prec[k] for k in ('found', 'want', 'exact_chunks')}
        res['chunk_files'] = chunk_files
        print('全景（观察用）: full_scan=%d precise=%d/%d exact_chunks=%d/%d'
              % (res['full_scan_records'], prec['found'], prec['want'],
                 prec['exact_chunks'], len(prec['per_chunk'])))
    res['transform'] = {'origin': [a.origin_x, a.origin_z], 'platform_y': a.platform_y,
                        'ground_py': a.ground_py, 'yoffset': a.yoffset, 'sink': a.sink}
    res['pass'] = verdict(res, a.min_rate)

    print('期望 %d 件 | 扫描命中 hash 记录 %d 条（%d 个 chunk 文件）'
          % (res['expected'], len(found), len(chunk_files)))
    print('匹配 %d | 缺失 %d | 盒内多出 %d → 存活率 %.2f%%'
          % (res['matched'], res['missing'], res['extra_in_bbox_total'], res['rate'] * 100))
    for m in res['missing_list'][:10]:
        print('  [缺] %-32s ×%d  key=%s' % (m['name'], m['count'], m['key']))
    if res['extra_in_bbox']:
        print('  ⚠ 盒内多出（可能是段数据噪声/旧残留，逐条核对面别急着删）:')
        for k, c in list(res['extra_in_bbox'].items())[:5]:
            print('      %s ×%d' % (k, c))
    print('结论: %s（达标线 %.4f）' % ('✅ PASS' if res['pass'] else '❌ FAIL', a.min_rate))
    if a.out:
        with open(a.out, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print('✓ 结果 → %s' % a.out)
    return 0 if res['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
