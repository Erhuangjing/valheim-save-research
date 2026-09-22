# -*- coding: utf-8 -*-
"""正式落地：把蓝图写进正式世界（动态算插入点 + 备份 + 验证）"""
import os, sys, time, shutil, struct, collections
sys.path.insert(0, HERE)
from zdo_lib import hs, candidates, patch_counts, insert_bytes


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
BKROOT = BACKUP_DIR
BP = os.path.join(ROOT, 'tools', 'bp_pieces.txt')
OX, OZ = -262.0, 270.0
H = hs()


def stable_hash(s):
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(s[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF


# --- 0. 找文件 ---
files = os.listdir(SRC)
idx_name = sorted(f for f in files if f.endswith('.chunks'))[0]
chunks = sorted(f for f in files if f.endswith('.chunk'))
print('索引: %s' % idx_name)
print('chunk: %s' % chunks)

# 落点属于哪个 chunk：用"落点附近有物件"的密度判定（快：只搜特定 hash 的字节位置）
def stable_hash2(t):
    h1 = h2 = 5381
    for i in range(0, len(t), 2):
        h1 = ((h1 << 5) + h1) ^ ord(t[i])
        if i == len(t) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(t[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF


PROBE = [stable_hash2(x) for x in ('woodwall', 'wood_floor', 'wood_roof_45', 'stone_wall_2x1', 'wood_beam')]
score = {}
for cf in chunks:
    b2 = open(os.path.join(SRC, cf), 'rb').read()
    n = 0
    for ph in PROBE:
        pat = struct.pack('<I', ph)
        i = b2.find(pat)
        while i >= 0:
            if i >= 16:
                x, y, z = struct.unpack_from('<fff', b2, i - 12)
                if abs(x - OX) < 160 and abs(z - OZ) < 160:
                    n += 1
            i = b2.find(pat, i + 1)
    score[cf] = n
    print('  %-24s 落点 160 米内建筑件: %d' % (cf, n))
target = max(score, key=score.get)
print('=> 落点所属 chunk: %s' % target)

if target is None:
    print('✗ 没找到落点所属 chunk')
    sys.exit(1)

b = open(os.path.join(SRC, target), 'rb').read()
pos = candidates(b)
print()
print('记录起点候选 %d 个，首=%d 末=%d' % (len(pos), pos[0], pos[-1]))
INSERT_AT = pos[-1]
print('★ 插入点（最后一个记录边界） = %d，文件 %d 字节，其后剩 %d 字节' % (INSERT_AT, len(b), len(b) - INSERT_AT))

# --- 1. 检查落点区域是否已有建筑（避免和她新造的重叠）---
sector = collections.Counter()
for p in range(0, len(b) - 40):
    fl = struct.unpack_from('<H', b, p)[0]
    if not (fl & 0x0100) or (fl & 0x8000):
        continue
    ph = struct.unpack_from('<I', b, p + 14)[0]
    nm = H.get(ph) or ''
    if not nm:
        continue
    x, y, z = struct.unpack_from('<fff', b, p + 2)
    if abs(x - OX) < 18 and abs(z - OZ) < 18:
        sector[nm] += 1
build = {k: v for k, v in sector.items() if any(t in k.lower() for t in
         ('wall', 'floor', 'roof', 'beam', 'pole', 'stone', 'piece', 'gate', 'stair', 'bed', 'chest'))}
print()
print('落点 36 米见方内已有物件: %d 种' % len(sector))
print('  其中建筑类: %s' % (build if build else '无 ✓（落点是空地）'))

# --- 2. 模板 ---
pieces = []
for line in open(BP, encoding='utf-8'):
    s = line.strip()
    if not s or s.startswith('#'):
        continue
    f = s.split('|')
    try:
        pieces.append((f[0], float(f[2]), float(f[3]), float(f[4])))
    except (ValueError, IndexError):
        continue
xs = [p[1] for p in pieces]; zs = [p[3] for p in pieces]
cx = (min(xs) + max(xs)) / 2; cz = (min(zs) + max(zs)) / 2
want = set(p[0] for p in pieces)

tmpl = {}
for p in range(0, len(b) - 40):
    fl = struct.unpack_from('<H', b, p)[0]
    if not (fl & 0x0100) or (fl & 0x8000):
        continue
    ph = struct.unpack_from('<I', b, p + 14)[0]
    nm = H.get(ph)
    if nm in want and nm not in tmpl:
        # 取该记录长度：找下一个候选
        import bisect
        i = bisect.bisect_right(pos, p)
        if i < len(pos):
            L = pos[i] - p
            tmpl[nm] = b[p:p + L]
print()
print('模板: 直接找到 %d / %d 种' % (len(tmpl), len(want)))
missing = sorted(want - set(tmpl))
if missing:
    print('  借用模板: %s' % missing)
    base = None
    for p in range(0, len(b) - 40):
        fl = struct.unpack_from('<H', b, p)[0]
        if not (fl & 0x0100) or (fl & 0x8000):
            continue
        if H.get(struct.unpack_from('<I', b, p + 14)[0]) == 'wood_floor':
            import bisect
            i = bisect.bisect_right(pos, p)
            if i < len(pos):
                base = b[p:pos[i]]
                break
    for nm in missing:
        t = bytearray(base)
        struct.pack_into('<I', t, 14, stable_hash(nm))
        tmpl[nm] = bytes(t)

# --- 3. 地面高度 ---
near = []
for p in pos:
    x, y, z = struct.unpack_from('<fff', b, p + 2)
    if abs(x - OX) < 45 and abs(z - OZ) < 45:
        near.append(y)
near.sort()
GY = near[len(near) // 2] if near else 40.5
print('地面高度基准 = %.2f（%d 个样本）' % (GY, len(near)))

# --- 4. 备份 ---
stamp = time.strftime('%Y%m%d-%H%M%S')
bk = os.path.join(BKROOT, 'pre_official_land_' + stamp)
shutil.copytree(SRC, os.path.join(bk, 'WORLD'))
print()
print('✓ 备份: %s' % bk)
n_bk = len(os.listdir(os.path.join(bk, 'WORLD')))
print('  备份包含 %d 个文件' % n_bk)

# --- 5. 生成并插入 ---
blob = bytearray()
used = collections.Counter()
for nm, bx, by, bz in pieces:
    t = tmpl.get(nm)
    if t is None:
        continue
    r = bytearray(t)
    struct.pack_into('<fff', r, 2, OX + (bx - cx), GY + by, OZ + (bz - cz))
    blob += r
    used[nm] += 1
cp = os.path.join(SRC, target)
ip = os.path.join(SRC, idx_name)
insert_bytes(cp, INSERT_AT, bytes(blob))
info = patch_counts(cp, ip, target, sum(used.values()))
print()
print('✓ 已插入 %d 件 (%d 字节) @%d' % (sum(used.values()), len(blob), INSERT_AT))
print('  chunk 计数: %s' % (info['chunk_count'],))
print('  索引总数  : %s' % (info['idx_total'],))
print('  条目      : %s' % (info['entry'],))
print('  文件大小  : %d → %d' % (len(b), len(b) + len(blob)))
print('  落点      : (%.1f, %.1f, %.1f)' % (OX, GY, OZ))
