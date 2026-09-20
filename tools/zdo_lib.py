# -*- coding: utf-8 -*-
"""Valheim 1.0 chunk 记录流读写库（实测格式）
记录 = [flags u16][pos 3xf32][prefab hash u32][短字段][字段区]
       flags bit8(Persistent) 置位；长度由 (flags, prefab) 唯一决定
chunk = [WorldVersion u16][count i32][记录流][尾部结构]
"""
import os, struct, math, json, collections

HSLIB_CACHE = r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json'
_HSLIB = None


def hs():
    global _HSLIB
    if _HSLIB is None:
        with open(HSLIB_CACHE, encoding='utf-8') as f:
            _HSLIB = {int(k): v for k, v in json.load(f).items()}
    return _HSLIB


def candidates(b):
    """候选记录起点"""
    H = hs()
    N = len(b)
    out = []
    for p in range(0, N - 40):
        fl = struct.unpack_from('<H', b, p)[0]
        if not (fl & 0x0100) or (fl & 0x8000):
            continue
        if struct.unpack_from('<I', b, p + 14)[0] not in H:
            continue
        x, y, z = struct.unpack_from('<fff', b, p + 2)
        if math.isnan(x) or math.isnan(z) or abs(x) > 20000 or abs(z) > 20000 or abs(y) > 5000:
            continue
        out.append(p)
    return out


def build_len_table(b, pos):
    t = collections.defaultdict(collections.Counter)
    for k in range(len(pos) - 1):
        p = pos[k]
        fl = struct.unpack_from('<H', b, p)[0]
        ph = struct.unpack_from('<I', b, p + 14)[0]
        t[(fl, ph)][pos[k + 1] - p] += 1
    return t


def walk(b):
    """返回 (steps, pos, table, end)  steps=[(start,len,flags,prefab)]"""
    N = len(b)
    pos = candidates(b)
    table = build_len_table(b, pos)
    cset = set(pos)
    p = pos[0]
    steps = []
    while p < N - 8:
        fl = struct.unpack_from('<H', b, p)[0]
        ph = struct.unpack_from('<I', b, p + 14)[0] if p + 18 <= N else 0
        nxt = None
        cands = table.get((fl, ph))
        if cands:
            for L, _ in cands.most_common():
                if p + L in cset:
                    nxt = p + L
                    break
        if nxt is None:
            import bisect
            i = bisect.bisect_right(pos, p)
            if i < len(pos):
                nxt = pos[i]
            else:
                break
        steps.append((p, nxt - p, fl, ph))
        p = nxt
    end = steps[-1][0] + steps[-1][1] if steps else pos[0]
    return steps, pos, table, end


def patch_counts(chunk_path, idx_path, chunk_name, delta):
    """同步更新三处计数：chunk 头 count、.chunks 头部总数、该条目 zdo 数"""
    b = bytearray(open(chunk_path, 'rb').read())
    c0 = struct.unpack_from('<i', b, 2)[0]
    struct.pack_into('<i', b, 2, c0 + delta)
    open(chunk_path, 'wb').write(bytes(b))
    idx = bytearray(open(idx_path, 'rb').read())
    t0 = struct.unpack_from('<i', idx, 2)[0]
    struct.pack_into('<i', idx, 2, t0 + delta)
    n = struct.unpack_from('<i', idx, 6)[0]
    hit = None
    for k in range(n):
        off = 10 + k * 11
        y, x, gen = idx[off], idx[off + 1], idx[off + 2]
        sv, z = struct.unpack_from('<ii', idx, off + 3)
        nm = '%02x_%02x__%d_%d.chunk' % (x, y, gen, sv)
        if nm == chunk_name:
            struct.pack_into('<i', idx, off + 7, z + delta)
            hit = (nm, z, z + delta)
            break
    open(idx_path, 'wb').write(bytes(idx))
    return {'chunk_count': (c0, c0 + delta), 'idx_total': (t0, t0 + delta), 'entry': hit}


def insert_bytes(chunk_path, at, blob):
    b = bytearray(open(chunk_path, 'rb').read())
    b[at:at] = blob
    open(chunk_path, 'wb').write(bytes(b))


def read_records(b, start, count):
    """读 count 条连续记录的字节（用 walk 得到的边界）"""
    steps, pos, table, end = walk(b)
    out = bytearray()
    n = 0
    for st, L, fl, ph in steps:
        if st < start:
            continue
        out += b[st:st + L]
        n += 1
        if n >= count:
            break
    return bytes(out), n
