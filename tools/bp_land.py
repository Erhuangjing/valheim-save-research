# -*- coding: utf-8 -*-
"""把蓝图真正落地到副本世界（离线写档版）"""
import os, sys, shutil, struct, collections, math
sys.path.insert(0, HERE)
from zdo_lib import hs, walk, patch_counts, insert_bytes


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
SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
TS = need('VH_WORLD_TEST', os.environ.get('VH_WORLD_TEST'), '测试存档根目录')
CHUNK = '20_1e__1_16.chunk'
IDX = '_main.21.chunks'
INSERT_AT = 916096
BP = os.path.join(ROOT, 'tools', 'bp_pieces.txt')
DST_NAME = 'XILAND'

H = hs()
b = open(os.path.join(SRC, CHUNK), 'rb').read()
steps, pos, table, end = walk(b)

# --- 1. 解析蓝图 ---
pieces = []
for line in open(BP, encoding='utf-8'):
    s = line.strip()
    if not s or s.startswith('#'):
        continue
    f = s.split('|')
    if len(f) < 6:
        continue
    try:
        pieces.append((f[0], float(f[2]), float(f[3]), float(f[4]), float(f[5])))
    except ValueError:
        continue
print('蓝图件数: %d' % len(pieces))
xs = [p[1] for p in pieces]; zs = [p[3] for p in pieces]; ys = [p[2] for p in pieces]
cx = (min(xs) + max(xs)) / 2; cz = (min(zs) + max(zs)) / 2
print('蓝图范围: X %.1f~%.1f (%.1f m), Z %.1f~%.1f (%.1f m), Y %.1f~%.1f'
      % (min(xs), max(xs), max(xs)-min(xs), min(zs), max(zs), max(zs)-min(zs), min(ys), max(ys)))

# --- 2. 建模板表：prefab 名 -> 记录字节 ---
want = set(p[0] for p in pieces)
tmpl = {}
for st, L, fl, ph in steps:
    nm = H.get(ph)
    if nm in want and nm not in tmpl:
        tmpl[nm] = b[st:st + L]
print('直接找到模板: %d / %d 种' % (len(tmpl), len(want)))
missing = want - set(tmpl)
if missing:
    print('缺模板: %s' % sorted(missing))

# 借模板：按 flags 相同 + 长度尽量接近
byflags = collections.defaultdict(list)
for st, L, fl, ph in steps:
    byflags[fl].append(b[st:st + L])
for nm in missing:
    # 用长度 38/39 的常见模板兜底（flags 0x1912 的 wood_floor 最接近建筑件）
    for st, L, fl, ph in steps:
        if H.get(ph) == 'wood_floor':
            t = bytearray(b[st:st + L])
            struct.pack_into('<I', t, 14, int(next(p[1] for p in pieces if p[0] == nm) if False else 0) or 0)
            tmpl[nm] = bytes(t)
            break
    # 改成正确 hash
    t = bytearray(tmpl[nm])
    # 从 ref 里算 hash
    def stable_hash(s):
        h1 = h2 = 5381
        for i in range(0, len(s), 2):
            h1 = ((h1 << 5) + h1) ^ ord(s[i])
            if i == len(s) - 1: break
            h2 = ((h2 << 5) + h2) ^ ord(s[i+1])
        return (h1 + h2 * 1566083941) & 0xFFFFFFFF
    struct.pack_into('<I', t, 14, stable_hash(nm))
    tmpl[nm] = bytes(t)

# --- 3. 算落点地面高度 ---
ORIGIN_X, ORIGIN_Z = -262.0, 270.0
near = []
for st, L, fl, ph in steps:
    x, y, z = struct.unpack_from('<fff', b, st + 2)
    if abs(x - ORIGIN_X) < 45 and abs(z - ORIGIN_Z) < 45:
        near.append(y)
near.sort()
GROUND_Y = near[len(near)//2] if near else 36.6
print('落点 (%g, %g)：周边 %d 个物件的 Y 中位数 = %.2f' % (ORIGIN_X, ORIGIN_Z, len(near), GROUND_Y))

# --- 4. 生成记录 ---
blob = bytearray()
used = collections.Counter()
for nm, bx, by, bz, yaw in pieces:
    t = tmpl.get(nm)
    if t is None:
        continue
    r = bytearray(t)
    wx = ORIGIN_X + (bx - cx)
    wy = GROUND_Y + by
    wz = ORIGIN_Z + (bz - cz)
    struct.pack_into('<fff', r, 2, wx, wy, wz)
    blob += r
    used[nm] += 1
print('生成记录: %d 条 (%d 字节)' % (sum(used.values()), len(blob)))

# --- 5. 写入副本世界 ---
dst = os.path.join(TS, DST_NAME)
if os.path.isdir(dst):
    shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(SRC, dst)
insert_bytes(os.path.join(dst, CHUNK), INSERT_AT, bytes(blob))
info = patch_counts(os.path.join(dst, CHUNK), os.path.join(dst, IDX), CHUNK, sum(used.values()))
print('[%s] chunk %s | 索引 %s | 条目 %s' % (DST_NAME, info['chunk_count'], info['idx_total'], info['entry']))
print('落点: (%.1f, %.1f, %.1f)，蓝图占地 %.1f x %.1f m'
      % (ORIGIN_X, GROUND_Y, ORIGIN_Z, max(xs)-min(xs), max(zs)-min(zs)))
print('主要构件: %s' % used.most_common(8))
