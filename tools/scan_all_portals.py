# -*- coding: utf-8 -*-
"""扫描所有传送门变体 prefab（不只 portal_wood），找 tag 与坐标"""
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
TAG_HASH = 0x297c91ea

H = {}
with open(HASHLIB, encoding='utf-8') as f:
    H = {int(k): v for k, v in json.load(f).items()}

# 所有含 portal 的 prefab
variants = {h: n for h, n in H.items() if 'portal' in n.lower()}
print('=== 含 portal 的 prefab 变体 (%d 个) ===' % len(variants))
for h, n in sorted(variants.items(), key=lambda x: x[1]):
    print('  %-28s 0x%08x' % (n, h))

SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
tagpat = struct.pack('<I', TAG_HASH)

print('\n=== 扫描存档 ===')
hits = []
for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    b = open(os.path.join(SRC, cf), 'rb').read()
    for ph, nm in variants.items():
        pat = struct.pack('<I', ph)
        i = b.find(pat)
        while i >= 0:
            p = i - 14
            if p >= 0:
                x, y, z = struct.unpack_from('<fff', b, p + 2)
                if abs(x) < 20000 and abs(z) < 20000:
                    raw = b[p:p + 300]
                    ti = raw.find(tagpat)
                    tag = None
                    if ti >= 0:
                        ln = raw[ti + 4]
                        tag = raw[ti + 5:ti + 5 + ln].decode('ascii', 'replace')
                    hits.append((cf, p, nm, x, y, z, struct.unpack_from('<H', raw, 0)[0], tag))
            i = b.find(pat, i + 1)

print('  命中 %d 条' % len(hits))
for cf, p, nm, x, y, z, fl, tag in sorted(hits, key=lambda r: r[2]):
    mark = ' ★★' if tag == '++' else ''
    print('  %-22s %-24s off=%-7d flags=0x%04x pos=(%9.1f,%6.1f,%9.1f) tag=%r%s' % (
        cf, nm, p, fl, x, y, z, tag, mark))
