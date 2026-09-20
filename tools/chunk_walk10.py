# -*- coding: utf-8 -*-
"""1.0 chunk 记录流顺序解析器（带分支回溯 + 合法性自检）
记录 = [flags u16][pos 3xf32][hash u32][rot?][conn?][segments]
"""
import os, sys, struct, math, json, collections

CACHE = r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json'
with open(CACHE, encoding='utf-8') as f:
    HSLIB = {int(k): v for k, v in json.load(f).items()}

BIT_CONN, BIT_F, BIT_V3, BIT_Q, BIT_I, BIT_L, BIT_S, BIT_BA = 0, 1, 2, 3, 4, 5, 6, 7
BIT_PERS, BIT_ROT = 8, 12

SEGS = [(BIT_F, 'f', 4), (BIT_V3, 'v3', 12), (BIT_Q, 'q', 16),
        (BIT_I, 'i', 4), (BIT_L, 'l', 8), (BIT_S, 's', 0), (BIT_BA, 'ba', 0)]

HEAD = 16


def read_num(b, p):
    n = b[p]; p += 1
    if n & 128:
        n = ((n & 127) << 8) | b[p]; p += 1
    return n, p


def decode(b, p, rot_size):
    """返回 (used, state) 或抛异常"""
    n = len(b)
    start = p
    flags, = struct.unpack_from('<H', b, p); p += 2
    pos = struct.unpack_from('<fff', b, p); p += 12
    phash, = struct.unpack_from('<I', b, p); p += 4
    rot = None
    if flags & (1 << BIT_ROT):
        if rot_size == 0:
            pass
        elif rot_size == 2:
            if p + 2 > n: raise EOFError('rot2')
            rot = struct.unpack_from('<H', b, p)[0]; p += 2
        elif rot_size == 4:
            if p + 4 > n: raise EOFError('rot4')
            rot = struct.unpack_from('<f', b, p)[0]; p += 4
        else:
            if p + 12 > n: raise EOFError('rot12')
            rot = struct.unpack_from('<fff', b, p); p += 12
    conn = None
    if flags & (1 << BIT_CONN):
        if p + 5 > n: raise EOFError('conn')
        conn = (b[p], struct.unpack_from('<i', b, p + 1)[0]); p += 5
    segs = {}
    for bit, name, sz in SEGS:
        if not (flags & (1 << bit)):
            continue
        cnt, p = read_num(b, p)
        if cnt > 4000: raise ValueError('seg %s cnt %d' % (name, cnt))
        for _ in range(cnt):
            if p + 4 > n: raise EOFError('hdr ' + name)
            p += 4
            if name in ('s', 'ba'):
                if p + 4 > n: raise EOFError('len')
                ln, = struct.unpack_from('<i', b, p); p += 4
                if ln < 0 or ln > 1 << 20: raise ValueError('bad len %d' % ln)
                p += ln
            else:
                p += sz
            if p > n: raise EOFError('over ' + name)
        segs[name] = cnt
    return p - start, {'flags': flags, 'pos': pos, 'hash': phash,
                       'rot': rot, 'conn': conn, 'segs': segs}


def looks_like_record(b, p):
    n = len(b)
    if p + 18 > n:
        return False
    fl, = struct.unpack_from('<H', b, p)
    if not (fl & (1 << BIT_PERS)) or (fl & 0x8000):
        return False
    ph, = struct.unpack_from('<I', b, p + 14)
    if ph not in HSLIB:
        return False
    x, y, z = struct.unpack_from('<fff', b, p + 2)
    if math.isnan(x) or math.isnan(z) or abs(x) > 20000 or abs(z) > 20000 or abs(y) > 5000:
        return False
    return True


def walk(b, start=HEAD, rot_candidates=(4, 12, 0, 2), verbose=False):
    p = start
    recs = []
    fail = None
    while p < len(b) - 18:
        pick = None
        for rs in rot_candidates:
            try:
                used, st = decode(b, p, rs)
            except Exception:
                continue
            nxt = p + used
            if nxt >= len(b) - 18:
                pick = (used, st, rs); break
            if looks_like_record(b, nxt):
                pick = (used, st, rs); break
        if pick is None:
            fail = p
            break
        used, st, rs = pick
        recs.append((p, used, rs, st))
        p += used
    return recs, p, fail


if __name__ == '__main__':
    BASE = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local'
    for rel in sys.argv[1:]:
        fp = os.path.join(BASE, rel)
        b = open(fp, 'rb').read()
        cnt, = struct.unpack_from('<i', b, 2)
        recs, endp, fail = walk(b)
        print('=' * 72)
        print('%s' % rel)
        print('  文件 %d 字节   头部声明 count=%d' % (len(b), cnt))
        print('  解析出记录 %d 条   走到 %d   剩余 %d' % (len(recs), endp, len(b) - endp))
        print('  失败点 = %s' % fail)
        if recs:
            lens = collections.Counter(r[1] for r in recs)
            print('  记录长度 top8: %s' % lens.most_common(8))
            rs = collections.Counter(r[2] for r in recs)
            print('  rot_size 分布: %s' % rs.most_common())
            hs = collections.Counter(r[3]['hash'] for r in recs)
            print('  出现最多的 prefab hash top10:')
            for h, c in hs.most_common(10):
                print('     0x%08x  %-28s x%d' % (h, HSLIB.get(h, '<未知>'), c))
