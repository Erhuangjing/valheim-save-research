# -*- coding: utf-8 -*-
"""dump portal / tombstone 记录的原始字节，提取可读字符串"""
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
SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
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
