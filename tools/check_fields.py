# -*- coding: utf-8 -*-
"""算候选字段名的 stable_hash + dump 各 portal 的字段值对比"""
import os, sys, struct
sys.path.insert(0, HERE)
from zdo_lib import hs, walk


# ── issue #6：路径解析 ──────────────────────────────────────────────
# 仓库内资源自动定位；外部路径走环境变量（缺省时报错清晰，不再硬编码本机路径）
#   VH_WORLD_ROOT  worlds_local 根目录
#   VH_WORLD       单个世界目录（含 .chunk / .chunks）
#   VH_SERVER      Valheim dedicated server 安装目录
#   VH_BACKUP      备份输入/输出根目录
HERE = os.path.dirname(os.path.abspath(__file__))          # tools/
ROOT = os.path.dirname(HERE)                               # 仓库根
HASHLIB = os.path.join(ROOT, 'format', 'prefab-hashlib.json')
WORLD_ROOT = os.environ.get('VH_WORLD_ROOT')
WORLD_DIR = os.environ.get('VH_WORLD')
SERVER_DIR = os.environ.get('VH_SERVER')
BACKUP_DIR = os.environ.get('VH_BACKUP')


def need(var, val, hint):
    """外部路径缺省时给清晰报错，而不是抛莫名的 FileNotFoundError"""
    if not val:
        raise SystemExit(
            '✗ 需要设置环境变量 %s（%s）\n'
            '  例：export %s="<你的路径>"'
            % (var, hint, var))
    return val
# ────────────────────────────────────────────────────────────────────
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
SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
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
