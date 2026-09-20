# -*- coding: utf-8 -*-
"""dump portal / tombstone 记录的原始字节，提取可读字符串"""
import os, sys, struct, re
sys.path.insert(0, r'E:/wkbdfile/2026-08-17-16-03-22/ref')
from zdo_lib import hs, walk

H = hs()
SRC = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'

PORTAL = None
TOMB = None
for h, n in H.items():
    if n == 'portal_wood':
        PORTAL = h
    if n == 'Player_tombstone':
        TOMB = h
print('portal_wood = 0x%08x   Player_tombstone = 0x%08x' % (PORTAL, TOMB))


def strings_in(b):
    out = []
    for m in re.finditer(rb'[\x20-\x7e]{2,40}', b):
        out.append(m.group().decode('ascii'))
    return out


for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    b = open(os.path.join(SRC, cf), 'rb').read()
    try:
        steps, pos, table, end = walk(b)
    except Exception:
        continue
    for st, L, fl, ph in steps:
        if ph not in (PORTAL, TOMB):
            continue
        x, y, z = struct.unpack_from('<fff', b, st + 2)
        raw = b[st:st + L]
        kind = 'portal' if ph == PORTAL else 'tombstone'
        print('\n[%s] %s  off=%d  len=%d  flags=0x%04x  pos=(%.1f, %.1f, %.1f)' % (kind, cf, st, L, fl, x, y, z))
        print('   hex: %s' % raw[:70].hex(' '))
        ss = strings_in(raw)
        print('   可见字符串: %s' % (ss if ss else '（无）'))
