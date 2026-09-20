# -*- coding: utf-8 -*-
"""从游戏程序集反查字段哈希名 + 全量扫 portal（candidates 法，不依赖 walk）"""
import os, sys, re, struct, json
sys.path.insert(0, r'E:/wkbdfile/2026-08-17-16-03-22/ref')

def stable_hash(s):
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(s[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF

WANT = {0xa996a82a: '?int字段?', 0x2faea12f: '?str字段?', 0x34831ea2: 'creator'}
DLLS = [
    r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/valheim_server_Data/Managed/assembly_valheim.dll',
    r'D:/SteamLibrary/steamapps/common/Valheim/valheim_Data/Managed/assembly_valheim.dll',
]
print('=== 从程序集反查字段名 ===')
done = False
for dll in DLLS:
    if not os.path.exists(dll):
        print('  缺: %s' % dll); continue
    print('  扫 %s (%.1f MB)' % (dll, os.path.getsize(dll) / 1048576))
    data = open(dll, 'rb').read()
    found = {}
    for m in re.finditer(rb'[\x20-\x7e]{3,40}', data):
        s = m.group().decode('ascii')
        h = stable_hash(s)
        if h in WANT and h not in found:
            found[h] = s
    for h, name in WANT.items():
        print('    0x%08x  %-12s -> %s' % (h, name, found.get(h, '未找到')))
    done = True
    break

print('\n=== 全量扫 portal（candidates 法）===')
H = {}
with open(r'E:/wkbdfile/2026-08-17-16-03-22/ref/hashlib.json', encoding='utf-8') as f:
    H = {int(k): v for k, v in json.load(f).items()}
PORTAL = [h for h, n in H.items() if n == 'portal_wood'][0]
print('  portal_wood = 0x%08x' % PORTAL)

SRC = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'
pat = struct.pack('<I', PORTAL)
rows = []
for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    b = open(os.path.join(SRC, cf), 'rb').read()
    i = b.find(pat)
    while i >= 0:
        p = i - 14
        if p >= 0:
            x, y, z = struct.unpack_from('<fff', b, p + 2)
            if abs(x) < 20000 and abs(z) < 20000:
                rows.append((cf, p, x, y, z, b[p:p + 200]))
        i = b.find(pat, i + 1)

print('  共 %d 条 portal 记录' % len(rows))
for cf, p, x, y, z, raw in sorted(rows, key=lambda r: (r[0], r[1])):
    ss = [m.group().decode('ascii') for m in re.finditer(rb'[\x20-\x7e]{2,40}', raw)]
    ss = [s for s in ss if 'Steam_' in s or (len(s) <= 12 and s.isprintable() and not any(c.isdigit() for c in s[:1]) and s not in ('steam',))]
    fl = struct.unpack_from('<H', raw, 0)[0]
    tag = [s for s in ss if 'Steam_' not in s]
    print('  %-20s off=%-8d flags=0x%04x pos=(%9.1f, %6.1f, %9.1f)  tag=%s  %s' % (
        cf, p, fl, x, y, z, tag, [s for s in ss if 'Steam_' in s]))
