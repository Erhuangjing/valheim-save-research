# -*- coding: utf-8 -*-
"""1) 反查字段哈希名  2) 扫所有 portal，看 tag 现状"""
import os, sys, struct, re
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
H = hs()
print('=== 1) 字段哈希反查 ===')
for h in (0x297c91ea, 0x2faea12f, 0xa996a82a, 0x34831ea2, 0xcbfdeb6c):
    print('  0x%08x -> %s' % (h, H.get(h, '（不在名字表里）')))

print('\n=== 2) 扫所有 portal 记录的字符串 ===')
SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
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
