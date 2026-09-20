# -*- coding: utf-8 -*-
"""锚点级 diff：把两个时间点的 chunk 按 (prefab, 原始坐标字节) 对齐，找出增删改"""
import os, sys, struct, math, json, collections

CACHE = r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json'
with open(CACHE, encoding='utf-8') as f:
    HSLIB = {int(k): v for k, v in json.load(f).items()}
BASE = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local'


def anchors(b):
    """返回 [(off, flags, posbytes, x,y,z, hash, name)]"""
    out = []
    n = len(b)
    for p in range(16, n - 18):
        fl, = struct.unpack_from('<H', b, p)
        if not (fl & 0x0100) or (fl & 0x8000):
            continue
        ph, = struct.unpack_from('<I', b, p + 14)
        if ph not in HSLIB:
            continue
        x, y, z = struct.unpack_from('<fff', b, p + 2)
        if math.isnan(x) or math.isnan(z) or abs(x) > 20000 or abs(z) > 20000 or abs(y) > 5000:
            continue
        out.append((p, fl, b[p + 2:p + 14], x, y, z, ph, HSLIB[ph]))
    return out


def load(rel):
    b = open(os.path.join(BASE, rel), 'rb').read()
    return b, anchors(b)


def report(relA, relB, labelA='A', labelB='B'):
    bA, anA = load(relA)
    bB, anB = load(relB)
    cntA, = struct.unpack_from('<i', bA, 2)
    cntB, = struct.unpack_from('<i', bB, 2)
    print('=' * 78)
    print('%s: %s' % (labelA, os.path.basename(relA)))
    print('%s: %s' % (labelB, os.path.basename(relB)))
    print('  %s: %d 字节 count=%d 锚点=%d  (识别率 %.2f%%)'
          % (labelA, len(bA), cntA, len(anA), 100.0 * len(anA) / cntA))
    print('  %s: %d 字节 count=%d 锚点=%d  (识别率 %.2f%%)'
          % (labelB, len(bB), cntB, len(anB), 100.0 * len(anB) / cntB))
    print('  文件差 %d 字节    count 差 %d 条' % (len(bB) - len(bA), cntB - cntA))

    kA = collections.defaultdict(list)
    kB = collections.defaultdict(list)
    for a in anA:
        kA[(a[2], a[6])].append(a)
    for a in anB:
        kB[(a[2], a[6])].append(a)

    onlyA = [k for k in kA if k not in kB]
    onlyB = [k for k in kB if k not in kA]
    both = [k for k in kA if k in kB]
    print('  仅 %s 有的对象: %d 条' % (labelA, len(onlyA)))
    print('  仅 %s 有的对象: %d 条' % (labelB, len(onlyB)))
    print('  两边都有: %d 条' % len(both))

    def top(keys, tag):
        c = collections.Counter(k[1] for k in keys)
        print('    %s 的 prefab 分布 top12:' % tag)
        for h, n in c.most_common(12):
            print('       %-30s x%d' % (HSLIB.get(h, hex(h)), n))

    if onlyA:
        top(onlyA, '仅 %s 有' % labelA)
    if onlyB:
        top(onlyB, '仅 %s 有' % labelB)

    # 共同对象：比较"记录到下一个锚点的字节跨度" + 记录头 64 字节
    changed = []
    for k in both:
        ra = kA[k][0]
        rb = kB[k][0]
        # 用该锚点与"同 prefab 同位置"记录起始处的 40 字节窗口比较
        wa = bA[ra[0]:ra[0] + 40]
        wb = bB[rb[0]:rb[0] + 40]
        if wa != wb:
            changed.append((k, ra, rb))
    print('  共同对象中「头 40 字节不同」的: %d 条' % len(changed))
    if changed:
        c = collections.Counter(k[1] for k, _, _ in changed)
        print('    变化对象的 prefab top12:')
        for h, n in c.most_common(12):
            print('       %-30s x%d' % (HSLIB.get(h, hex(h)), n))
        print('    前 6 个变化对象:')
        for k, ra, rb in changed[:6]:
            print('       %-26s pos=(%.2f,%.2f,%.2f)' % (k[1], ra[3], ra[4], ra[5]))
            print('          %s: %s' % (labelA, bA[ra[0]:ra[0] + 40].hex(' ')))
            print('          %s: %s' % (labelB, bB[rb[0]:rb[0] + 40].hex(' ')))


if __name__ == '__main__':
    report('WORLD_backup_auto-20260916-224637/20_1e__1_46.chunk',
           'WORLD/20_1e__1_48.chunk', '22:46', '22:48')
    report('WORLD_backup_auto-20260916-203929/20_1e__1_41.chunk',
           'WORLD/20_1e__1_48.chunk', '20:39', '22:48')
