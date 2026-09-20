# -*- coding: utf-8 -*-
"""1) 反查字段哈希名  2) 扫所有 portal，看 tag 现状"""
import os, sys, struct, re
sys.path.insert(0, r'E:/wkbdfile/2026-08-17-16-03-22/ref')
from zdo_lib import hs, walk

H = hs()
print('=== 1) 字段哈希反查 ===')
for h in (0x297c91ea, 0x2faea12f, 0xa996a82a, 0x34831ea2, 0xcbfdeb6c):
    print('  0x%08x -> %s' % (h, H.get(h, '（不在名字表里）')))

print('\n=== 2) 扫所有 portal 记录的字符串 ===')
SRC = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'
PORTAL = None
for h, n in H.items():
    if n == 'portal_wood':
        PORTAL = h
print('  portal_wood = 0x%08x' % PORTAL)

for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    try:
        b = open(os.path.join(SRC, cf), 'rb').read()
    except Exception as e:
        continue
    try:
        steps, pos, table, end = walk(b)
    except Exception as e:
        print('  ! %s walk 失败 %s' % (cf, e))
        continue
    for st, L, fl, ph in steps:
        if ph != PORTAL:
            continue
        x, y, z = struct.unpack_from('<fff', b, st + 2)
        raw = b[st:st + L]
        ss = [m.group().decode('ascii') for m in re.finditer(rb'[\x20-\x7e]{1,40}', raw)]
        haspp = b'++' in raw
        print('  %-20s off=%-8d len=%-4d flags=0x%04x pos=(%.1f, %.1f, %.1f) %s' % (
            cf, st, L, fl, x, y, z, '<<< 这个含 ++' if haspp else ''))
        print('      字符串: %s' % ss)
