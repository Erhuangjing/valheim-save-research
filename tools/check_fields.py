# -*- coding: utf-8 -*-
"""算候选字段名的 stable_hash + dump 各 portal 的字段值对比"""
import os, sys, struct
sys.path.insert(0, r'E:/wkbdfile/2026-08-17-16-03-22/ref')
from zdo_lib import hs, walk

def stable_hash(s):
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(s[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF

print('=== 候选字段名的 stable_hash ===')
CANDS = ['tag', 'creator', 'connected', 'text', 'target', 'portal', 'owner', 'name',
         'health', 'use_all', 'connection', 'target_fwd', 'target_rev', 'portal_tag',
         'tag_name', 'socket', 'PlayerID', 'creatorID', 'spawn_time', 'tameable']
M = {}
for c in CANDS:
    h = stable_hash(c)
    M[h] = c
    print('  %-14s 0x%08x' % (c, h))

TARGETS = [0x297c91ea, 0x2faea12f, 0xa996a82a, 0x34831ea2, 0xcbfdeb6c]
print('\n=== 记录里出现的字段哈希 对应关系 ===')
for t in TARGETS:
    print('  0x%08x -> %s' % (t, M.get(t, '✗ 不匹配任何候选名')))

print('\n=== 各 portal 记录的字段区对比 ===')
H = hs()
SRC = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'
PORTAL = [h for h, n in H.items() if n == 'portal_wood'][0]
for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    try:
        b = open(os.path.join(SRC, cf), 'rb').read()
        steps, pos, table, end = walk(b)
    except Exception:
        continue
    for st, L, fl, ph in steps:
        if ph != PORTAL:
            continue
        x, y, z = struct.unpack_from('<fff', b, st + 2)
        raw = b[st:st + L]
        print('\n  --- %s off=%d len=%d flags=0x%04x pos=(%.1f, %.1f, %.1f)' % (cf, st, L, fl, x, y, z))
        # 前缀（到 int 段之前）
        print('      前缀: %s' % raw[18:34].hex(' '))
        # 找所有 4 字节小端 in 段
        seg_int = raw.find(struct.pack('<I', 0xa996a82a))
        if seg_int >= 0:
            v = struct.unpack_from('<i', raw, seg_int + 4)[0]
            print('      [int 0xa996a82a] = %d' % v)
        seg_lng = raw.find(struct.pack('<I', 0x34831ea2))
        if seg_lng >= 0:
            v = struct.unpack_from('<q', raw, seg_lng + 4)[0]
            print('      [long 0x34831ea2] = %d' % v)
