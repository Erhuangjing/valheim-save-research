# -*- coding: utf-8 -*-
"""地形验收报告：读执行器导出的逐顶点 dump（bp_terrain_dump.csv），出数字 + 一张对比图。

dump 由 XiBpBuilder 地形段写出（首行 `# PlatformY=..,PadY=..,ProtectX=..` 元数据）：
    x,z,role,w,target,before,bottom,after[,reload]
    role: P 平台（占地 + 闭运算填补）/ B 地窖 / S 过渡带 / E 蓝图 #Terrain /
          X 本该改、因在保护圈内被跳过（before/after 都是游戏里实测 → 独立复核「一点没动」）/
          V 原版地形件（vbuild 里作者的锄头整地/路面）后来又改过 —— 最终形状归作者，与 E 同等对待
    w = 拉向 target 的权重（1 = 整平到位）；bottom = 该平台顶点上最低件的碰撞体底面

判据（对应正式服实测的两类事故）：
  断崖   相邻顶点（1m）高差 > --cliff（默认 1.5m）。一端是 B/E/V 的记「挡土墙」（地窖坑壁、作者用 #Terrain 挖的
         院子/露台——PlanBuild 回放同样是直壁，属设计）；两端都是本工具生成的平台/过渡带才判失败（= 断层）
  露缝   平台顶点地表比 bottom 低 >0.15m（地基露出来）
  保护圈 圈内被改的顶点数必须为 0（主宅 + 护城河）
  平台   满权重顶点 |after − target| 的 p95 / max
  持久化 有 reload 列时 |reload − after| 的 max（第二次起服复核；09-16 事故 = 重载后 delta 丢失）

    python bp_terrain_report.py runs/x/bp_terrain_dump.csv [--png runs/x/terrain.png] [--json out.json]
    python bp_terrain_report.py --selftest
"""
import argparse
import json
import math
import os
import struct
import sys
import zlib


def load(path):
    meta, rows, head = {}, [], None
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                for kv in line[1:].split(','):
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        meta[k.strip()] = float(v)
                continue
            if head is None:
                head = line.split(',')
                continue
            f_ = line.split(',')
            r = {}
            for k, v in zip(head, f_):
                r[k] = v if k == 'role' else (float(v) if v != '' else None)
            rows.append(r)
    return meta, rows


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def analyze(meta, rows, cliff=1.5):
    grid = {(int(r['x']), int(r['z'])): r for r in rows}
    rep = {'verts': len(rows), 'roles': {}}
    for r in rows:
        rep['roles'][r['role']] = rep['roles'].get(r['role'], 0) + 1
    has_after = any(r.get('after') is not None for r in rows)
    col = 'after' if has_after else 'target'
    # 平台精度（满权重顶点）
    # V = 原版地形件后来又改过（作者设计），不按本工具目标算误差
    errs = [abs(r['after'] - r['target']) for r in rows
            if has_after and r['w'] >= 0.999 and r['role'] != 'V' and r.get('after') is not None]
    rep['flat_err_p95'] = _pct(errs, 0.95)
    rep['flat_err_max'] = max(errs) if errs else None
    # 改动量
    d = [r[col] - r['before'] for r in rows if r.get(col) is not None and r.get('before') is not None]
    rep['delta_min'] = min(d) if d else None
    rep['delta_max'] = max(d) if d else None
    rep['changed'] = sum(1 for v in d if abs(v) > 0.05)
    # 断崖 / 坡度：只看 dump 内部相邻边（dump 外是没动过的原地形，过渡带末端 w→0 已与之连续）
    steps, cliffs = [], []
    for (x, z), r in grid.items():
        for dx, dz in ((1, 0), (0, 1)):
            q = grid.get((x + dx, z + dz))
            if q is None or r.get(col) is None or q.get(col) is None:
                continue
            s = abs(r[col] - q[col])
            steps.append(s)
            if s > cliff:
                cliffs.append({'x': x, 'z': z, 'to': [x + dx, z + dz], 'step': round(s, 2),
                               'roles': r['role'] + q['role']})
    rep['slope_deg_p99'] = round(math.degrees(math.atan(_pct(steps, 0.99))), 1) if steps else None
    rep['slope_deg_max'] = round(math.degrees(math.atan(max(steps))), 1) if steps else None
    rep['cliffs'] = len(cliffs)
    # 一端是 B（地窖坑壁）或 E（蓝图 #Terrain：作者挖的院子/露台，PlanBuild 回放同样是直壁）= 挡土墙，属设计；
    # 两端都是本工具自己生成的平台/过渡带（P/S/X）才是我们整出来的断层
    ours = [c for c in cliffs if not set(c['roles']) & set('BEV')]
    rep['cliffs_retaining'] = len(cliffs) - len(ours)
    rep['cliffs_ours'] = len(ours)
    rep['cliff_samples'] = sorted(ours or cliffs, key=lambda c: -c['step'])[:8]
    # 露缝：平台顶点的地表比该点最低件底低 >0.15m（dump 有 bottom 列时）= 地基露出来
    gp = [r for r in rows if r['role'] == 'P' and r.get('bottom') is not None and r.get(col) is not None]
    rep['gap_checked'] = len(gp)
    rep['gaps'] = sum(1 for r in gp if r[col] < r['bottom'] - 0.15)
    # 过渡带末端连续性：w 很小的点几乎没动
    tail = [abs(r[col] - r['before']) for r in rows if r['role'] == 'S' and r['w'] < 0.05
            and r.get(col) is not None and r.get('before') is not None]
    rep['skirt_tail_max_delta'] = max(tail) if tail else None
    # 保护圈
    pr = meta.get('ProtectR', 0.0)
    if pr > 0:
        px, pz = meta.get('ProtectX', 0.0), meta.get('ProtectZ', 0.0)
        rep['protect_touched'] = sum(1 for r in rows if math.hypot(r['x'] - px, r['z'] - pz) < pr
                                     and r.get(col) is not None and r.get('before') is not None
                                     and abs(r[col] - r['before']) > 0.01)
    else:
        rep['protect_touched'] = None
    # 持久化
    rl = [abs(r['reload'] - r['after']) for r in rows if r.get('reload') is not None and r.get('after') is not None]
    rep['reload_checked'] = len(rl)
    rep['reload_max_diff'] = max(rl) if rl else None
    rep['basis'] = col
    return rep


def verdict(rep):
    bad = []
    if rep['flat_err_max'] is not None and rep['flat_err_max'] > 0.5:
        bad.append('平台误差 %.2fm > 0.5m' % rep['flat_err_max'])
    if rep['cliffs_ours']:
        bad.append('平台/过渡带有 %d 处断崖' % rep['cliffs_ours'])
    if rep['gap_checked'] and rep['gaps'] > max(2, rep['gap_checked'] * 0.005):
        bad.append('平台露缝 %d/%d 个顶点（地基露出来）' % (rep['gaps'], rep['gap_checked']))
    if rep['protect_touched']:
        bad.append('保护圈内改了 %d 个顶点' % rep['protect_touched'])
    if rep['reload_max_diff'] is not None and rep['reload_max_diff'] > 0.05:
        bad.append('重载后地形漂移 %.2fm（delta 未持久化?）' % rep['reload_max_diff'])
    return bad


# ---------------------------------------------------------------- 纯标准库 PNG
def _png(path, w, h, rgb_rows):
    raw = b''.join(b'\x00' + bytes(row) for row in rgb_rows)
    def chunk(t, data):
        return struct.pack('>I', len(data)) + t + data + struct.pack('>I', zlib.crc32(t + data) & 0xFFFFFFFF)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def _ramp(t):
    """高度色带：深蓝 → 青 → 绿 → 黄 → 棕（0..1）"""
    stops = [(0.0, (30, 60, 150)), (0.3, (60, 170, 200)), (0.55, (90, 170, 80)), (0.8, (220, 200, 90)), (1.0, (140, 90, 50))]
    t = min(1.0, max(0.0, t))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            u = (t - a) / (b - a)
            return tuple(int(ca[i] + (cb[i] - ca[i]) * u) for i in range(3))
    return stops[-1][1]


def _diverge(v, lim):
    """高差色带：挖（蓝）← 0 白 → 填（红）"""
    t = max(-1.0, min(1.0, v / lim if lim else 0.0))
    if t < 0:
        return (int(255 + t * 205), int(255 + t * 175), 255)
    return (255, int(255 - t * 175), int(255 - t * 205))


def render(meta, rows, path, scale=4):
    """三联图：改前 | 改后 | 改后−改前；黑点 = 满权重边界，红圈 = 保护圈。"""
    col = 'after' if any(r.get('after') is not None for r in rows) else 'target'
    xs = [int(r['x']) for r in rows]; zs = [int(r['z']) for r in rows]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    W, H = x1 - x0 + 1, z1 - z0 + 1
    grid = {(int(r['x']), int(r['z'])): r for r in rows}
    hs = [r[k] for r in rows for k in ('before', col) if r.get(k) is not None]
    lo, hi = min(hs), max(hs)
    dl = max([abs(r[col] - r['before']) for r in rows if r.get(col) is not None and r.get('before') is not None] or [1.0])
    gap = 6
    img_w = (W * 3) * scale + gap * 2
    img_h = H * scale
    px, pz, pr = meta.get('ProtectX', 0.0), meta.get('ProtectZ', 0.0), meta.get('ProtectR', 0.0)
    out = []
    for iy in range(img_h):
        z = z1 - iy // scale                    # 北在上
        row = []
        for panel in range(3):
            for ix in range(W * scale):
                x = x0 + ix // scale
                r = grid.get((x, z))
                if r is None or r.get('before') is None:
                    c = (235, 235, 235)
                elif panel == 0:
                    c = _ramp((r['before'] - lo) / (hi - lo or 1))
                elif panel == 1:
                    c = _ramp(((r.get(col) or r['before']) - lo) / (hi - lo or 1))
                else:
                    c = _diverge((r.get(col) or r['before']) - r['before'], dl)
                if r is not None and r['w'] >= 0.999:
                    edge = any(grid.get((x + a, z + b)) is None or grid[(x + a, z + b)]['w'] < 0.999
                               for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1)))
                    if edge and (ix % scale == 0 or (iy % scale) == 0):
                        c = (20, 20, 20)
                if pr > 0 and abs(math.hypot(x - px, z - pz) - pr) < 0.6:
                    c = (220, 30, 30)
                row.extend(c)
            if panel < 2:
                row.extend((255, 255, 255) * gap)
        out.append(row)
    _png(path, img_w, img_h, out)
    return {'png': path, 'size': [img_w, img_h], 'height_range': [round(lo, 2), round(hi, 2)], 'delta_scale': round(dl, 2)}


def print_report(meta, rep, bad):
    print('dump：%d 个顶点 %s | PlatformY=%s PadY=%s | 依据列=%s'
          % (rep['verts'], rep['roles'], meta.get('PlatformY'), meta.get('PadY'), rep['basis']))
    if rep['flat_err_max'] is not None:
        print('平台（满权重顶点）误差 p95=%.3f max=%.3f m' % (rep['flat_err_p95'], rep['flat_err_max']))
    if rep['delta_min'] is not None:
        print('改动量 %.2f ~ %+.2f m，实际变化 >5cm 的顶点 %d 个' % (rep['delta_min'], rep['delta_max'], rep['changed']))
    print('坡度 p99=%s° max=%s°；断崖(>1.5m/1m) %d 处 = 挡土墙（地窖/蓝图地形）%d + 平台/过渡带 %d'
          % (rep['slope_deg_p99'], rep['slope_deg_max'], rep['cliffs'], rep['cliffs_retaining'], rep['cliffs_ours']))
    if rep['gap_checked']:
        print('平台露缝（地表低于该点最低件底 >0.15m）：%d / %d' % (rep['gaps'], rep['gap_checked']))
    for c in rep['cliff_samples'][:4]:
        print('   断崖 (%d,%d)→(%d,%d) %.2fm [%s]' % (c['x'], c['z'], c['to'][0], c['to'][1], c['step'], c['roles']))
    if rep['skirt_tail_max_delta'] is not None:
        print('过渡带末端（w<0.05）最大改动 %.3f m（= 接回原地形的最后一级台阶，<0.3m 即无缝）' % rep['skirt_tail_max_delta'])
    print('保护圈内被改顶点：%s' % ('未设保护圈' if rep['protect_touched'] is None else rep['protect_touched']))
    if rep['reload_checked']:
        print('重载复核：%d 个顶点，|reload−after| max=%.3f m' % (rep['reload_checked'], rep['reload_max_diff']))
    print('结论：%s' % ('✅ 通过' if not bad else '❌ ' + '；'.join(bad)))


# ---------------------------------------------------------------- 自检
def selftest():
    import tempfile
    ok = True

    def check(label, cond):
        nonlocal ok
        print('  [%s] %s' % ('PASS' if cond else 'FAIL', label))
        ok = ok and bool(cond)

    def make(cliff_at=None, protect=True, drift=0.0):
        """10×10 平台（目标 50）+ 6m smoothstep 过渡带接回 45~55 的斜坡原地形"""
        rows = []
        for x in range(-11, 12):
            for z in range(-11, 12):
                before = 50 + 0.4 * x                           # 原地形斜坡
                d = max(abs(x) - 5, abs(z) - 5, 0)
                if d == 0:
                    role, w = 'P', 1.0
                elif d < 6:
                    s = 1 - d / 6.0
                    role, w = 'S', s * s * (3 - 2 * s)
                else:
                    continue
                after = before + w * (50 - before)
                if cliff_at and (x, z) == cliff_at:
                    after += 3.0
                rows.append({'x': x, 'z': z, 'role': role, 'w': w, 'target': 50.0, 'before': before,
                             'after': after, 'reload': after + drift})
        meta = {'PlatformY': 50.0, 'PadY': 50.0, 'ProtectX': 30.0 if protect else 0.0, 'ProtectZ': 0.0,
                'ProtectR': 10.0 if protect else 0.0}
        return meta, rows

    print('== 干净方案 ==')
    meta, rows = make()
    rep = analyze(meta, rows)
    check('平台误差 0', rep['flat_err_max'] < 1e-6)
    check('无断崖', rep['cliffs'] == 0)
    check('过渡带末端几乎不动（<0.3m）', rep['skirt_tail_max_delta'] is None or rep['skirt_tail_max_delta'] < 0.3)
    check('保护圈（远处）0 改动', rep['protect_touched'] == 0)
    check('结论通过', not verdict(rep))
    print('== 注入断崖 ==')
    meta, rows = make(cliff_at=(7, 0))
    rep = analyze(meta, rows)
    check('检出过渡带断崖', rep['cliffs_ours'] >= 1 and verdict(rep))
    print('== 蓝图地形挡土墙（E 与 P 相邻 3m 高差）不判失败 ==')
    meta, rows = make()
    for r in rows:
        if r['x'] == 0 and r['z'] == 0:
            r['role'], r['after'] = 'E', r['after'] - 3.0
            r['target'] = r['reload'] = r['after']       # 蓝图地形点：到位（目标即作者的高度），且已持久化
    rep = analyze(meta, rows)
    check('记为挡土墙、结论仍通过', rep['cliffs_retaining'] >= 1 and rep['cliffs_ours'] == 0 and not verdict(rep))
    print('== 露缝 ==')
    meta, rows = make()
    for r in rows:
        r['bottom'] = r['after'] - 0.1 if r['role'] == 'P' else None
    rows[0]['role'] = 'P'; rows[0]['bottom'] = rows[0]['after'] + 1.0
    rows[1]['role'] = 'P'; rows[1]['bottom'] = rows[1]['after'] + 1.0
    rows[2]['role'] = 'P'; rows[2]['bottom'] = rows[2]['after'] + 1.0
    rep = analyze(meta, rows)
    check('检出 3 个露缝顶点', rep['gaps'] == 3 and any('露缝' in b for b in verdict(rep)))
    print('== 保护圈压在方案上 ==')
    meta, rows = make()
    meta['ProtectX'], meta['ProtectR'] = 8.0, 3.0
    rep = analyze(meta, rows)
    check('检出保护圈内改动', (rep['protect_touched'] or 0) > 0 and any('保护圈' in b for b in verdict(rep)))
    print('== 重载漂移 ==')
    meta, rows = make(drift=0.3)
    rep = analyze(meta, rows)
    check('检出 delta 未持久化', any('重载' in b for b in verdict(rep)))
    print('== 出图 ==')
    meta, rows = make()
    p = os.path.join(tempfile.mkdtemp(prefix='bpterr_'), 't.png')
    info = render(meta, rows, p)
    with open(p, 'rb') as f:
        sig = f.read(8)
    W = max(r['x'] for r in rows) - min(r['x'] for r in rows) + 1
    check('PNG 文件头正确、尺寸 = 3 联', sig == b'\x89PNG\r\n\x1a\n' and info['size'][0] == W * 3 * 4 + 12)
    print('\n%s' % ('全部通过 ✓' if ok else '有失败项 ✗'))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description='地形验收报告（执行器 dump → 数字 + 三联对比图）')
    ap.add_argument('dump', nargs='?', help='bp_terrain_dump.csv')
    ap.add_argument('--png', help='输出对比图（改前 | 改后 | 高差）')
    ap.add_argument('--json', help='输出 JSON')
    ap.add_argument('--cliff', type=float, default=1.5, help='断崖阈值：相邻 1m 顶点高差（米）')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args(argv)
    if a.selftest or not a.dump:
        return selftest()
    meta, rows = load(a.dump)
    if not rows:
        print('✗ dump 为空')
        return 1
    rep = analyze(meta, rows, a.cliff)
    bad = verdict(rep)
    print_report(meta, rep, bad)
    if a.png:
        info = render(meta, rows, a.png)
        print('✓ 对比图 → %s（%d×%d，高度 %s，高差色标 ±%.2fm）'
              % (info['png'], info['size'][0], info['size'][1], info['height_range'], info['delta_scale']))
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump({'meta': meta, 'report': rep, 'fail': bad}, f, ensure_ascii=False, indent=1)
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
