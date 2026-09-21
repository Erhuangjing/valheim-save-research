# -*- coding: utf-8 -*-
"""精确提取每条 portal 的 tag（按 tag hash 定位）+ 检查插入点上下文"""
import os, sys, struct, json

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
sys.path.insert(0, HERE)

TAG_HASH = 0x297c91ea      # stable_hash("tag")
CRE_HASH = 0x34831ea2      # stable_hash("creator")
STR1_HASH = 0x2faea12f
INT_HASH = 0xa996a82a

H = {}
with open(HASHLIB, encoding='utf-8') as f:
    H = {int(k): v for k, v in json.load(f).items()}
PORTAL = [h for h, n in H.items() if n == 'portal_wood'][0]

SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
pat = struct.pack('<I', PORTAL)
tagpat = struct.pack('<I', TAG_HASH)
crepat = struct.pack('<I', CRE_HASH)
intpat = struct.pack('<I', INT_HASH)

print('=== 所有 portal 记录的精确字段 ===')
rows = []
for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    b = open(os.path.join(SRC, cf), 'rb').read()
    i = b.find(pat)
    while i >= 0:
        p = i - 14
        if p >= 0:
            x, y, z = struct.unpack_from('<fff', b, p + 2)
            if abs(x) < 20000 and abs(z) < 20000:
                raw = b[p:p + 260]
                rows.append((cf, p, x, y, z, struct.unpack_from('<H', raw, 0)[0], raw))
        i = b.find(pat, i + 1)

def read_str(raw, h):
    i = raw.find(h)
    if i < 0:
        return None
    ln = raw[i + 4]
    return raw[i + 5:i + 5 + ln].decode('ascii', 'replace')

for cf, p, x, y, z, fl, raw in sorted(rows, key=lambda r: (r[0], r[1])):
    tag = read_str(raw, tagpat)
    cre = read_str(raw, crepat)
    s1 = read_str(raw, struct.pack('<I', STR1_HASH))
    ii = raw.find(intpat)
    iv = struct.unpack_from('<i', raw, ii + 4)[0] if ii >= 0 else None
    mark = ' ★★' if tag == '++' else ''
    print('  %-20s off=%-8d flags=0x%04x pos=(%9.1f,%6.1f,%9.1f)' % (cf, p, fl, x, y, z))
    print('        tag=%r  int(%08x)=%s  str1=%r%s' % (tag, INT_HASH, iv, s1, mark))

print('\n=== 插入点上下文（1e_26__1_1.chunk）===')
cp = os.path.join(SRC, '1e_26__1_1.chunk')
b = open(cp, 'rb').read()
print('  文件大小 %d，头计数 %d' % (len(b), struct.unpack_from('<i', b, 2)[0]))
for off in (119270, 120270, 120370, 120447):
    if off + 60 <= len(b):
        seg = b[off:off + 60]
        flags = struct.unpack_from('<H', b, off)[0]
        ph = struct.unpack_from('<I', b, off + 14)[0]
        xx, yy, zz = struct.unpack_from('<fff', b, off + 2)
        print('  +%d: flags=0x%04x prefab=%s pos=(%.1f,%.1f,%.1f)' % (
            off, flags, H.get(ph, '0x%08x' % ph), xx, yy, zz))
        print('        %s' % seg[:48].hex(' '))
print('  尾部 20 字节: %s' % b[-20:].hex(' '))
