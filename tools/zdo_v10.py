# -*- coding: utf-8 -*-
"""1.0 chunk 记录结构验证：#A [flags2][pos12][hash4][rot?][conn?][segments]"""
import os, struct, math, json, collections, sys

LIVE = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'
CACHE = r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json'
with open(CACHE, encoding='utf-8') as f:
    HSLIB = {int(k): v for k, v in json.load(f).items()}

BIT_CONN, BIT_F, BIT_V3, BIT_Q, BIT_I, BIT_L, BIT_S, BIT_BA = 0, 1, 2, 3, 4, 5, 6, 7
BIT_PERS, BIT_DIST, BIT_T1, BIT_T2, BIT_ROT = 8, 9, 10, 11, 12


def find_flags_positions(b, delta=14, sector=0):
    """按 [flags2][sector?][pos12][hash4] 找所有记录起点"""
    N = len(b)
    out = []
    for p in range(0, N - 40):
        fl = struct.unpack_from('<H', b, p)[0]
        if not (fl & 0x0100) or (fl & 0x8000):
            continue
        hp = p + 2 + sector + 12
        if hp + 4 > N:
            continue
        if struct.unpack_from('<I', b, hp)[0] in HSLIB:
            pp = p + 2 + sector
            x, y, z = struct.unpack_from('<fff', b, pp)
            if math.isnan(x) or math.isnan(z) or abs(x) > 20000 or abs(z) > 20000 or abs(y) > 5000:
                continue
            out.append(p)
    return out


def read_num_items(b, p, mode):
    if mode == 8:                      # uint8，最高位续接
        n = b[p]; p += 1
        if n & 128:
            n = ((n & 127) << 8) | b[p]; p += 1
        return n, p
    else:                              # uint16
        n = struct.unpack_from('<H', b, p)[0]
        return n, p + 2


SEGS = [(BIT_F, 'f', 4), (BIT_V3, 'v3', 12), (BIT_Q, 'q', 16),
        (BIT_I, 'i', 4), (BIT_L, 'l', 8), (BIT_S, 's', 0), (BIT_BA, 'ba', 0)]


def decode10(b, p, num_mode=8):
    n = len(b)
    flags = struct.unpack_from('<H', b, p)[0]; p += 2
    pos = struct.unpack_from('<fff', b, p); p += 12
    phash = struct.unpack_from('<I', b, p)[0]; p += 4
    rot = None
    if flags & (1 << BIT_ROT):
        if p + rot_size > n: raise EOFError('rot')
        if rot_size == 4:
            rot = struct.unpack_from('<f', b, p)[0]; p += 4
        else:
            rot = struct.unpack_from('<fff', b, p); p += 12
    conn = None
    if flags & (1 << BIT_CONN):
        if p + 5 > n: raise EOFError('conn')
        conn = (b[p], struct.unpack_from('<i', b, p + 1)[0]); p += 5
    segs = {}
    for bit, name, sz in SEGS:
        if not (flags & (1 << bit)):
            continue
        cnt, p = read_num_items(b, p, num_mode)
        if cnt > 200:
            raise ValueError('seg %s count %d' % (name, cnt))
        for _ in range(cnt):
            if p + 4 > n: raise EOFError('seg ' + name)
            p += 4
            if name in ('s', 'ba'):
                if p + 4 > n: raise EOFError('len')
                ln = struct.unpack_from('<i', b, p)[0]; p += 4
                if ln < 0 or ln > 65535: raise ValueError('bad len %d' % ln)
                p += ln
            else:
                p += sz
            if p > n: raise EOFError('seg over')
        segs[name] = cnt
    return p - (p - (p)) if False else (p - (p - (p - p)))  # placeholder


# 干净重写返回值
def decode10b(b, p, num_mode=8, rot_size=4):
    n = len(b)
    start = p
    flags = struct.unpack_from('<H', b, p)[0]; p += 2
    pos = struct.unpack_from('<fff', b, p); p += 12
    phash = struct.unpack_from('<I', b, p)[0]; p += 4
    rot = None
    if flags & (1 << BIT_ROT):
        if p + rot_size > n: raise EOFError('rot')
        if rot_size == 4:
            rot = struct.unpack_from('<f', b, p)[0]; p += 4
        else:
            rot = struct.unpack_from('<fff', b, p); p += 12
    conn = None
    if flags & (1 << BIT_CONN):
        if p + 5 > n: raise EOFError('conn')
        conn = (b[p], struct.unpack_from('<i', b, p + 1)[0]); p += 5
    segs = {}
    for bit, name, sz in SEGS:
        if not (flags & (1 << bit)):
            continue
        cnt, p = read_num_items(b, p, num_mode)
        if cnt > 200:
            raise ValueError('seg %s count %d' % (name, cnt))
        for _ in range(cnt):
            if p + 4 > n: raise EOFError('hdr ' + name)
            p += 4
            if name in ('s', 'ba'):
                if p + 4 > n: raise EOFError('len')
                ln = struct.unpack_from('<i', b, p)[0]; p += 4
                if ln < 0 or ln > 65535: raise ValueError('bad len %d' % ln)
                p += ln
            else:
                p += sz
            if p > n: raise EOFError('over')
        segs[name] = cnt
    return p - start, {'flags': flags, 'pos': pos, 'hash': phash, 'rot': rot,
                       'conn': conn, 'segs': segs}


cf = sys.argv[1] if len(sys.argv) > 1 else '20_1e__1_16.chunk'
b = open(os.path.join(LIVE, cf), 'rb').read()
print('chunk=%s %d 字节' % (cf, len(b)))

pos_list = find_flags_positions(b)
print('记录起点候选: %d 个' % len(pos_list))
print('前 8 个起点: %s' % pos_list[:8])
print('起点奇偶分布: %s' % collections.Counter(p % 2 for p in pos_list).most_common())
if pos_list:
    print('头部长度推断 = %d 字节' % pos_list[0])

# ---- flags 值分布 ----
fl = collections.Counter(struct.unpack_from('<H', b, p)[0] for p in pos_list)
print('\n=== flags 值分布 top12 ===')
for v, c in fl.most_common(12):
    bits = [i for i in range(16) if v & (1 << i)]
    print('  0x%04x  x%-6d  位: %s' % (v, c, bits))

# ---- 连续解码（按起点序列验证，跳过中间未知区域）----
print('\n=== 用"起点序列"验证：每条记录按规范解码，消耗长度是否等于到下一支起点的距离 ===')
for num_mode, rot_size in ((8,4),(8,12),(16,4)):
    ok = bad = 0
    bad_samples = []
    for k in range(len(pos_list) - 1):
        p = pos_list[k]
        gap = pos_list[k + 1] - p
        try:
            used, st = decode10b(b, p, num_mode, rot_size)
        except Exception as e:
            bad += 1
            if len(bad_samples) < 4:
                bad_samples.append(('EXC ' + str(e), gap, struct.unpack_from('<H', b, p)[0]))
            continue
        if used == gap:
            ok += 1
        else:
            bad += 1
            if len(bad_samples) < 4:
                bad_samples.append(('len %d vs gap %d' % (used, gap), gap,
                                    struct.unpack_from('<H', b, p)[0]))
    tot = ok + bad
    print('  numItems=%d bit, rotation=%d 字节 → 吻合 %d/%d (%.1f%%)' % (num_mode * 8, rot_size, ok, tot, 100.0 * ok / max(1, tot)))
    for s in bad_samples:
        print('     例: %s (gap=%d flags=0x%04x)' % s)

# ---- 用最优配置 dump 几条 ----
print('\n=== 样本记录（numItems=uint8）===')
shown = 0
for k in range(min(len(pos_list) - 1, 400)):
    p = pos_list[k]
    gap = pos_list[k + 1] - p
    try:
        used, st = decode10b(b, p, 8, 4)
    except Exception:
        continue
    if used != gap:
        continue
    nm = HSLIB.get(st['hash'], '?')
    print('  @%-7d len=%-3d flags=0x%04x %-22s pos=(%.1f,%.1f,%.1f) rot=%s segs=%s'
          % (p, gap, st['flags'], nm, *st['pos'],
             None if st['rot'] is None else tuple(round(v, 2) for v in st['rot']), st['segs']))
    shown += 1
    if shown >= 12:
        break
