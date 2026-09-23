# -*- coding: utf-8 -*-
"""整格地形还原：离线读世界存档里的地形编译器（_TerrainCompiler ZDO 的 TCData），算出执行器 [Terrain] RestoreFile。只读存档。

为什么需要：旧版执行器的清理会把落点半径内的**所有** ZDO 搬到世界角落——其中 zone 中心的 _TerrainCompiler 一走，
那一整格的地形修改（玩家的护城河、地基…）就没了，和相邻格交界处齐刷刷一道断层。在旧址重建时先把那一格整格还原。

1.0 chunk 里这类记录是精简格式：[flags u16][zone 中心 x i16][z i16][hash u32 = _TerrainCompiler][byte-array 个数]
  [key u32 = "TCData"][len i32][gzip(ZPackage)]；ZPackage = int ver; int ops; v3 lastOpPoint; f lastOpRadius;
  int N; N×(bool mod[; f level; f smooth]); int M; M×(bool pmod[; f r,g,b,a])   （TerrainComp.Save / Load）

    python bp_terrain_restore.py scan   <world_dir> [--near x z r]
        列出地形编译器（zone 中心、改过的顶点数、delta 范围）；拿「旧版落地前的备份」和「现在」各跑一次，对比哪一格没了
    python bp_terrain_restore.py plan   <base_world> <now_world> <cx> <cz> <out.txt>
        底 = base 里这格；叠 = now 里 base 没改过、now 改过的顶点（玩家后来的活）；zone 边上的共用顶点照抄相邻格 now 的值
    python bp_terrain_restore.py verify <after_world> <base_world> <now_world> <cx> <cz> <protect_x> <protect_z> <protect_r>
        跑完后核查：保护圈内这格 = base（后来新挖的除外）、四条接缝两侧一致、相邻格没被动过（本次过渡带除外）
    python bp_terrain_restore.py --selftest
"""
import argparse
import gzip
import math
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bp_reconcile as R                                     # noqa: E402

TC_HASH = R.stable_hash('_TerrainCompiler') & 0xffffffff
TCDATA_KEY = R.stable_hash('TCData') & 0xffffffff
W = 65                                                       # heightmap 64 格 → 65×65 顶点


def parse_tc(raw):
    b = gzip.decompress(raw)
    p = 24                                                   # ver, ops, lastOpPoint(3f), lastOpRadius
    ver, ops = struct.unpack_from('<ii', b, 0)
    n, = struct.unpack_from('<i', b, p); p += 4
    mod, lvl, smo = [False] * n, [0.0] * n, [0.0] * n
    for i in range(n):
        m = b[p] != 0; p += 1
        mod[i] = m
        if m:
            lvl[i], smo[i] = struct.unpack_from('<ff', b, p); p += 8
    m2, = struct.unpack_from('<i', b, p); p += 4
    pmod, col = [False] * m2, [None] * m2
    for i in range(m2):
        m = b[p] != 0; p += 1
        pmod[i] = m
        if m:
            col[i] = struct.unpack_from('<ffff', b, p); p += 16
    return {'ver': ver, 'ops': ops, 'mod': mod, 'lvl': lvl, 'smo': smo, 'pmod': pmod, 'col': col}


def load(world_dir):
    """{(zone 中心 x, z): 解码后的地形编译器}"""
    key, tch = struct.pack('<I', TCDATA_KEY), struct.pack('<I', TC_HASH)
    out = {}
    for fn in sorted(os.listdir(world_dir)):
        if not fn.endswith('.chunk'):
            continue
        b = open(os.path.join(world_dir, fn), 'rb').read()
        i = 0
        while True:
            j = b.find(key, i)
            if j < 0:
                break
            i = j + 4
            if b[j - 5:j - 1] != tch:
                continue
            cx, cz = struct.unpack_from('<hh', b, j - 9)
            ln, = struct.unpack_from('<i', b, j + 4)
            d = parse_tc(b[j + 8:j + 8 + ln])
            d['file'] = fn
            out[(cx, cz)] = d
    return out


def dh(d, i):
    return (d['lvl'][i] + d['smo'][i]) if d and d['mod'][i] else 0.0


def seam_pairs(dx, dz):
    """本格与 (dx,dz) 方向邻格共用的顶点：[(本格下标, 邻格下标)]"""
    out = []
    for k in range(W):
        if dx:
            out.append((k * W + (0 if dx < 0 else W - 1), k * W + (W - 1 if dx < 0 else 0)))
        else:
            out.append(((0 if dz < 0 else W - 1) * W + k, (W - 1 if dz < 0 else 0) * W + k))
    return out


def plan(B, N, cx, cz):
    b, n = B[(cx, cz)], N.get((cx, cz))
    mod, lvl, smo, pmod, col = list(b['mod']), list(b['lvl']), list(b['smo']), list(b['pmod']), list(b['col'])
    kept = 0
    if n:
        for i in range(W * W):
            if n['mod'][i] and not mod[i]:
                mod[i], lvl[i], smo[i] = True, n['lvl'][i], n['smo'][i]; kept += 1
        for i in range(len(pmod)):
            if n['pmod'][i] and not pmod[i]:
                pmod[i], col[i] = True, n['col'][i]
    fixed = 0
    for dx, dz in ((-64, 0), (64, 0), (0, -64), (0, 64)):
        nb = N.get((cx + dx, cz + dz))
        for i, j in seam_pairs(dx, dz):
            want = (nb['mod'][j], nb['lvl'][j], nb['smo'][j]) if nb else (False, 0.0, 0.0)
            if abs((want[1] + want[2] if want[0] else 0.0) - (lvl[i] + smo[i] if mod[i] else 0.0)) > 1e-4:
                fixed += 1
            mod[i], lvl[i], smo[i] = want
    lines = ['zone %d %d %d %d' % (cx, cz, W * W, len(pmod))]
    lines += ['h %d %.6f %.6f' % (i, lvl[i], smo[i]) for i in range(W * W) if mod[i]]
    lines += ['p %d %.6f %.6f %.6f %.6f' % ((i,) + tuple(col[i])) for i in range(len(pmod)) if pmod[i]]
    return lines, {'heights': sum(mod), 'base': sum(b['mod']), 'kept_new': kept, 'seam_fixed': fixed, 'paint': sum(pmod)}


def verify(A, B, N, cx, cz, px, pz, pr):
    a, b, n = A[(cx, cz)], B[(cx, cz)], N.get((cx, cz))
    rep = {'protect_checked': 0, 'protect_diff': [], 'seam_checked': 0, 'seam_bad': 0, 'neighbors': {}}
    for i in range(W * W):
        wx, wz = cx + i % W - 32, cz + i // W - 32
        if math.hypot(wx - px, wz - pz) >= pr or i % W in (0, W - 1) or i // W in (0, W - 1):
            continue
        rep['protect_checked'] += 1
        if abs(dh(a, i) - dh(b, i)) > 1e-3:
            later = n is not None and n['mod'][i] and not b['mod'][i] and abs(dh(a, i) - dh(n, i)) <= 1e-3
            rep['protect_diff'].append((wx, wz, 'kept_new' if later else 'bad'))
    for dx, dz in ((-64, 0), (64, 0), (0, -64), (0, 64)):
        nb = A.get((cx + dx, cz + dz))
        for i, j in seam_pairs(dx, dz):
            rep['seam_checked'] += 1
            if abs(dh(a, i) - dh(nb, j)) > 1e-3:
                rep['seam_bad'] += 1
    for dx, dz in ((-64, 0), (64, 0), (0, -64), (0, 64), (-64, 64), (-64, -64), (64, 64), (64, -64)):
        k = (cx + dx, cz + dz)
        if k in A or k in N:
            rep['neighbors'][k] = sum(1 for i in range(W * W) if abs(dh(A.get(k), i) - dh(N.get(k), i)) > 1e-4)
    rep['ok'] = rep['seam_bad'] == 0 and all(t == 'kept_new' for *_, t in rep['protect_diff'])
    return rep


# ---------------------------------------------------------------- 自检
def _tc_blob(mod_vals, paint=()):
    """合成 TCData：mod_vals = {i: (level, smooth)}"""
    pk = bytearray(struct.pack('<ii', 1, len(mod_vals)) + struct.pack('<ffff', 0, 0, 0, 0) + struct.pack('<i', W * W))
    for i in range(W * W):
        if i in mod_vals:
            pk += b'\x01' + struct.pack('<ff', *mod_vals[i])
        else:
            pk += b'\x00'
    pk += struct.pack('<i', W * W)
    for i in range(W * W):
        pk += (b'\x01' + struct.pack('<ffff', 0.5, 0, 0, 1)) if i in paint else b'\x00'
    return gzip.compress(bytes(pk))


def _chunk(path, zones):
    """合成 chunk：头 6 字节 + 若干条精简格式的地形编译器记录（前后夹点垃圾）"""
    out = bytearray(struct.pack('<Hi', 41, len(zones)))
    for (cx, cz), blob in zones.items():
        out += b'\xaa' * 7 + struct.pack('<H', 0x2d80) + struct.pack('<hh', cx, cz) + struct.pack('<I', TC_HASH)
        out += b'\x01' + struct.pack('<I', TCDATA_KEY) + struct.pack('<i', len(blob)) + blob
    open(path, 'wb').write(bytes(out))


def selftest():
    ok = True

    def check(label, cond):
        nonlocal ok
        print('  [%s] %s' % ('PASS' if cond else 'FAIL', label))
        ok = ok and bool(cond)

    tmp = tempfile.mkdtemp(prefix='tcr_')
    base, now, after = (os.path.join(tmp, n) for n in ('base', 'now', 'after'))
    for d in (base, now, after):
        os.makedirs(d)
    moat = {r * W + 3: (-4.0, 0.0) for r in range(10, 50)}             # 这格西边 x=-253 一道护城河
    moat.update({r * W + 0: (-4.0, 0.0) for r in range(10, 50)})       # 接缝上也有（与西邻一致）
    west_edge = {r * W + 64: (-4.0, 0.0) for r in range(10, 50)}       # 西邻格东边 = 同一道护城河
    _chunk(os.path.join(base, 'a.chunk'), {(-256, 256): _tc_blob(moat, paint={5}), (-320, 256): _tc_blob(west_edge)})
    # now：这格的编译器被旧版清理搬走了，只剩玩家后来挖的一个点
    _chunk(os.path.join(now, 'a.chunk'), {(-256, 256): _tc_blob({20 * W + 40: (-0.3, 0.0)}), (-320, 256): _tc_blob(west_edge)})
    B, N = load(base), load(now)
    check('解出 2 个地形编译器、zone 中心正确', set(B) == {(-256, 256), (-320, 256)})
    check('解码改高顶点数', sum(B[(-256, 256)]['mod']) == 80 and sum(B[(-256, 256)]['pmod']) == 1)
    lines, st = plan(B, N, -256, 256)
    check('还原 = 底 80 + 保留玩家后来挖的 1', st['heights'] == 81 and st['kept_new'] == 1)
    check('西接缝与邻格一致（无需校正）', st['seam_fixed'] == 0)
    # 模拟执行器写回：after = 还原结果；再把保护圈外的一个点改掉（本次过渡带），邻格不动
    mv = {int(l.split()[1]): (float(l.split()[2]), float(l.split()[3])) for l in lines if l.startswith('h ')}
    _chunk(os.path.join(after, 'a.chunk'), {(-256, 256): _tc_blob(mv), (-320, 256): _tc_blob(west_edge)})
    rep = verify(load(after), B, N, -256, 256, -270.0, 256.0, 25.0)
    check('核查通过：保护圈内 = 底、接缝一致', rep['ok'] and rep['seam_bad'] == 0 and rep['protect_checked'] > 0)
    # 反例：接缝被写坏
    mv2 = dict(mv); mv2[15 * W + 0] = (-1.0, 0.0)
    _chunk(os.path.join(after, 'a.chunk'), {(-256, 256): _tc_blob(mv2), (-320, 256): _tc_blob(west_edge)})
    rep2 = verify(load(after), B, N, -256, 256, -270.0, 256.0, 25.0)
    check('接缝写坏 → 核查不过', not rep2['ok'] and rep2['seam_bad'] == 1)
    print('selftest:', '全部通过 ✓' if ok else '存在失败 ✗')
    return 0 if ok else 1


def main():
    if '--selftest' in sys.argv:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan'); s.add_argument('world'); s.add_argument('--near', nargs=3, type=float)
    p = sub.add_parser('plan'); p.add_argument('base'); p.add_argument('now'); p.add_argument('cx', type=int)
    p.add_argument('cz', type=int); p.add_argument('out')
    v = sub.add_parser('verify')
    for n in ('after', 'base', 'now'):
        v.add_argument(n)
    for n in ('cx', 'cz'):
        v.add_argument(n, type=int)
    for n in ('px', 'pz', 'pr'):
        v.add_argument(n, type=float)
    a = ap.parse_args()
    if a.cmd == 'scan':
        tcs = load(a.world)
        sel = {k: d for k, d in tcs.items() if not a.near or max(abs(k[0] - a.near[0]), abs(k[1] - a.near[1])) <= a.near[2] + 32}
        print('地形编译器 %d 个（列出 %d 个）' % (len(tcs), len(sel)))
        for k, d in sorted(sel.items()):
            dl = [dh(d, i) for i in range(len(d['mod']))]
            print('  zone 中心 %-12s ops=%-5d 改高 %4d/%d  Δ[%6.2f,%6.2f]  刷漆 %4d  %s'
                  % (k, d['ops'], sum(d['mod']), len(d['mod']), min(dl), max(dl), sum(d['pmod']), d['file']))
    elif a.cmd == 'plan':
        lines, st = plan(load(a.base), load(a.now), a.cx, a.cz)
        with open(a.out, 'w', encoding='utf-8') as f:
            f.write('# 整格地形还原：底=%s 叠=%s\n' % (os.path.basename(a.base.rstrip('/\\')), os.path.basename(a.now.rstrip('/\\'))))
            f.write('\n'.join(lines) + '\n')
        print('zone (%d,%d)：改高 %d（底 %d + 保留后来新挖 %d，接缝校正 %d）；刷漆 %d → %s'
              % (a.cx, a.cz, st['heights'], st['base'], st['kept_new'], st['seam_fixed'], st['paint'], a.out))
    else:
        rep = verify(load(a.after), load(a.base), load(a.now), a.cx, a.cz, a.px, a.pz, a.pr)
        kept = [p for p in rep['protect_diff'] if p[2] == 'kept_new']
        bad = [p for p in rep['protect_diff'] if p[2] == 'bad']
        print('保护圈内顶点 %d 个：与还原底不同 %d（其中玩家后来新挖、按设计保留 %d）' % (rep['protect_checked'], len(rep['protect_diff']), len(kept)))
        for p in bad[:8]:
            print('   ✗ (%d,%d)' % p[:2])
        print('接缝顶点 %d 个，两侧不一致 %d 个' % (rep['seam_checked'], rep['seam_bad']))
        for k, c in sorted(rep['neighbors'].items()):
            print('邻格 %s 与跑之前相比改变 %d 个顶点%s' % (k, c, '' if c == 0 else '（应只在本次过渡带里）'))
        print('结论：%s' % ('✅ 还原到位、接缝一致' if rep['ok'] else '❌ 见上'))
        return 0 if rep['ok'] else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
