# -*- coding: utf-8 -*-
"""扫描所有传送门变体 prefab（不只 portal_wood），找 tag 与坐标"""
import os, sys, struct, json
TAG_HASH = 0x297c91ea

H = {}
with open(r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json', encoding='utf-8') as f:
    H = {int(k): v for k, v in json.load(f).items()}

# 所有含 portal 的 prefab
variants = {h: n for h, n in H.items() if 'portal' in n.lower()}
print('=== 含 portal 的 prefab 变体 (%d 个) ===' % len(variants))
for h, n in sorted(variants.items(), key=lambda x: x[1]):
    print('  %-28s 0x%08x' % (n, h))

SRC = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'
tagpat = struct.pack('<I', TAG_HASH)

print('\n=== 扫描存档 ===')
hits = []
for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    b = open(os.path.join(SRC, cf), 'rb').read()
    for ph, nm in variants.items():
        pat = struct.pack('<I', ph)
        i = b.find(pat)
        while i >= 0:
            p = i - 14
            if p >= 0:
                x, y, z = struct.unpack_from('<fff', b, p + 2)
                if abs(x) < 20000 and abs(z) < 20000:
                    raw = b[p:p + 300]
                    ti = raw.find(tagpat)
                    tag = None
                    if ti >= 0:
                        ln = raw[ti + 4]
                        tag = raw[ti + 5:ti + 5 + ln].decode('ascii', 'replace')
                    hits.append((cf, p, nm, x, y, z, struct.unpack_from('<H', raw, 0)[0], tag))
            i = b.find(pat, i + 1)

print('  命中 %d 条' % len(hits))
for cf, p, nm, x, y, z, fl, tag in sorted(hits, key=lambda r: r[2]):
    mark = ' ★★' if tag == '++' else ''
    print('  %-22s %-24s off=%-7d flags=0x%04x pos=(%9.1f,%6.1f,%9.1f) tag=%r%s' % (
        cf, nm, p, fl, x, y, z, tag, mark))
