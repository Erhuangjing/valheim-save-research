# -*- coding: utf-8 -*-
"""1) 解析 _main.xx.chunks 索引表  2) 定位每个 chunk 的记录流末尾与尾部区段"""
import os, struct, math, json, collections

CACHE = r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json'
with open(CACHE, encoding='utf-8') as f:
    HSLIB = {int(k): v for k, v in json.load(f).items()}
BASE = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local'
DIRS = {
    '20:39': 'WORLD_backup_auto-20260916-203929',
    '22:46': 'WORLD_backup_auto-20260916-224637',
    '22:48': 'WORLD',
}

print('#' * 78)
print('# 1) _main.xx.chunks 索引表')
for tag, d in DIRS.items():
    dp = os.path.join(BASE, d)
    fn = [f for f in os.listdir(dp) if f.endswith('.chunks')][0]
    b = open(os.path.join(dp, fn), 'rb').read()
    ver, = struct.unpack_from('<H', b, 0)
    total, = struct.unpack_from('<I', b, 2)
    n, = struct.unpack_from('<I', b, 6)
    print('-' * 78)
    print('【%s】%s  %d 字节  ver=%d  合计=%d  zone数=%d'
          % (tag, fn, len(b), ver, total, n))
    rec = (len(b) - 10) // n
    print('  每条 = %d 字节' % rec)
    s = 0
    for i in range(n):
        o = 10 + i * rec
        seg = b[o:o + rec]
        zx, zz, v1, v2 = seg[0], seg[1], seg[2], seg[3]
        rest = seg[4:]
        vals = [struct.unpack_from('<I', rest, k)[0] for k in range(0, len(rest) - 3)]
        print('    zone %02x_%02x  v%d_%d   rest=%s  u32候选=%s'
              % (zx, zz, v1, v2, rest.hex(' '), vals))
        s += 1

print()
print('#' * 78)
print('# 2) 每个 chunk 最后一个锚点 与 尾部区段')
for tag, d in DIRS.items():
    dp = os.path.join(BASE, d)
    print('-' * 78)
    print('【%s】' % tag)
    for fn in sorted(f for f in os.listdir(dp) if f.endswith('.chunk')):
        b = open(os.path.join(dp, fn), 'rb').read()
        n = len(b)
        last = None
        for p in range(n - 18, 16, -1):
            fl, = struct.unpack_from('<H', b, p)
            if not (fl & 0x0100) or (fl & 0x8000):
                continue
            ph, = struct.unpack_from('<I', b, p + 14)
            if ph not in HSLIB:
                continue
            x, y, z = struct.unpack_from('<fff', b, p + 2)
            if math.isnan(x) or abs(x) > 20000 or abs(z) > 20000 or abs(y) > 5000:
                continue
            last = (p, ph)
            break
        tail = n - (last[0] if last else 0)
        print('  %-22s %8d B  尾锚点@%-8s 尾段=%6d B  %s'
              % (fn, n, last[0] if last else '-', tail,
                 (HSLIB.get(last[1], '?') if last else '')))

print()
print('#' * 78)
print('# 3) 主宅 chunk 末尾 320 字节 hex')
for tag, fn in (('20:39', 'WORLD_backup_auto-20260916-203929/20_1e__1_41.chunk'),
                ('22:46', 'WORLD_backup_auto-20260916-224637/20_1e__1_46.chunk'),
                ('22:48', 'WORLD/20_1e__1_48.chunk')):
    b = open(os.path.join(BASE, fn), 'rb').read()
    print('-' * 78)
    print('【%s】%s  %d 字节' % (tag, os.path.basename(fn), len(b)))
    for k in range(len(b) - 320, len(b), 32):
        print('   @%-8d %s' % (k, b[k:k + 32].hex(' ')))
