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
def transform_pieces(manifest, ox, oz, platform_y, ground_py, yoffset, sink):
    """蓝图局部坐标 → 世界坐标（与参考插件 BuildPieces 同式）。返回带 key 的列表。"""
    cx = manifest['bbox_center']['x']
    cz = manifest['bbox_center']['z']
    out = []
    for p in manifest['pieces']:
        wx = ox + (p['x'] - cx)
        wy = (platform_y - ground_py) + p['y'] + yoffset + sink
        wz = oz + (p['z'] - cz)
        out.append({'name': p['name'], 'hash': p.get('hash') or stable_hash(p['name']),
                    'x': wx, 'y': wy, 'z': wz,
                    'key': key_of(p.get('hash') or stable_hash(p['name']), wx, wy, wz)})
    return out


def key_of(h, x, y, z):
    """交接文档 §9.4：hash|round(x*4)|round(y*4)|round(z*4)。round 同 C# ToEven。"""
    return (h, round(x * 4), round(y * 4), round(z * 4))


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
    """全景模式：逐字节启发式扫所有记录（慢，仅供 识别率/全景 观察）。"""
    records = []
    for fn in sorted(f for f in os.listdir(world_dir) if f.endswith('.chunk')):
        with open(os.path.join(world_dir, fn), 'rb') as f:
            data = f.read()
        n = len(data)
        for p in range(0, n - 18):
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


# ---------------------------------------------------------------- 对账
def reconcile(expected, found_records, margin=15.0):
    """期望（transform_pieces 输出）vs 扫描记录 → 结果字典。"""
    exp = collections.Counter(e['key'] for e in expected)
    name_of = {}
    for e in expected:
        name_of[e['key']] = e['name']
    found = collections.Counter(key_of(h, x, y, z) for h, x, y, z in found_records)

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
    xs = [e['x'] for e in expected]; ys = [e['y'] for e in expected]; zs = [e['z'] for e in expected]
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    z0, z1 = min(zs) - margin, max(zs) + margin
    extras = collections.Counter()
    for k, cnt in found.items():
        if k in exp:
            continue
        h, qx, qy, qz = k
        x, y, z = qx / 4.0, qy / 4.0, qz / 4.0
        if x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z <= z1:
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
    """合成 1.0 chunk 文件：16B 头（offset2=int32 记录数）+ 记录 + 段字节。"""
    hdr = bytearray(16)
    struct.pack_into('<i', hdr, 2, len(recs))
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

    print('A 全量对账      : matched=%d missing=%d → %s' % (rA['matched'], rA['missing'], 'PASS' if okA else 'FAIL'))
    print('B 缺失+y漂移0.30: matched=%d missing=%d extras=%d → %s'
          % (rB['matched'], rB['missing'], rB['extra_in_bbox_total'], 'PASS' if okB else 'FAIL'))
    print('C 盒外干扰+全景 : extras=%d 全景记录=%d → %s' % (rC['extra_in_bbox_total'], len(full), 'PASS' if okC else 'FAIL'))
    ok = okA and okB and okC
    print('selftest:', '19/19 风格三案 %s' % ('全部通过 ✓' if ok else '存在失败 ✗'))
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
    ap.add_argument('--min-rate', type=float, default=1.0, help='达标线（默认 1.0 = 零丢失）')
    ap.add_argument('--margin', type=float, default=15.0, help='extras 判定包围盒外扩（米）')
    ap.add_argument('--full-scan', action='store_true', help='附加逐字节全景扫描（慢）')
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

    exp = transform_pieces(manifest, a.origin_x, a.origin_z, a.platform_y,
                           a.ground_py, a.yoffset, a.sink)
    found, chunk_files = scan_chunks_for_hashes(a.world, {e['hash'] for e in exp})
    res = reconcile(exp, found, a.margin)
    if a.full_scan:
        hslib = load_hashlib()
        res['full_scan_records'] = len(full_scan(a.world, hslib))
        res['chunk_files'] = chunk_files
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
