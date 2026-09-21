# -*- coding: utf-8 -*-
"""从游戏程序集反查字段哈希名 + 全量扫 portal（candidates 法，不依赖 walk）"""
import os, sys, re, struct, json

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

def stable_hash(s):
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(s[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF

WANT = {0xa996a82a: '?int字段?', 0x2faea12f: '?str字段?', 0x34831ea2: 'creator'}
DLLS = [
    os.path.join(need('VH_SERVER', SERVER_DIR, 'dedicated server 安装目录'), 'valheim_server_Data/Managed/assembly_valheim.dll'),
    os.path.join(need('VH_SERVER', SERVER_DIR, 'dedicated server 安装目录'), 'valheim_server_Data/Managed/assembly_valheim.dll'),
]
print('=== 从程序集反查字段名 ===')
done = False
for dll in DLLS:
    if not os.path.exists(dll):
        print('  缺: %s' % dll); continue
    print('  扫 %s (%.1f MB)' % (dll, os.path.getsize(dll) / 1048576))
    data = open(dll, 'rb').read()
    found = {}
    for m in re.finditer(rb'[\x20-\x7e]{3,40}', data):
        s = m.group().decode('ascii')
        h = stable_hash(s)
        if h in WANT and h not in found:
            found[h] = s
    for h, name in WANT.items():
        print('    0x%08x  %-12s -> %s' % (h, name, found.get(h, '未找到')))
    done = True
    break

print('\n=== 全量扫 portal（candidates 法）===')
H = {}
with open(HASHLIB, encoding='utf-8') as f:
    H = {int(k): v for k, v in json.load(f).items()}
PORTAL = [h for h, n in H.items() if n == 'portal_wood'][0]
print('  portal_wood = 0x%08x' % PORTAL)

SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
pat = struct.pack('<I', PORTAL)
rows = []
for cf in sorted(f for f in os.listdir(SRC) if f.endswith('.chunk')):
    b = open(os.path.join(SRC, cf), 'rb').read()
    i = b.find(pat)
    while i >= 0:
        p = i - 14
        if p >= 0:
            x, y, z = struct.unpack_from('<fff', b, p + 2)
            if abs(x) < 20000 and abs(z) < 20000:
                rows.append((cf, p, x, y, z, b[p:p + 200]))
        i = b.find(pat, i + 1)

print('  共 %d 条 portal 记录' % len(rows))
for cf, p, x, y, z, raw in sorted(rows, key=lambda r: (r[0], r[1])):
    ss = [m.group().decode('ascii') for m in re.finditer(rb'[\x20-\x7e]{2,40}', raw)]
    ss = [s for s in ss if 'Steam_' in s or (len(s) <= 12 and s.isprintable() and not any(c.isdigit() for c in s[:1]) and s not in ('steam',))]
    fl = struct.unpack_from('<H', raw, 0)[0]
    tag = [s for s in ss if 'Steam_' not in s]
    print('  %-20s off=%-8d flags=0x%04x pos=(%9.1f, %6.1f, %9.1f)  tag=%s  %s' % (
        cf, p, fl, x, y, z, tag, [s for s in ss if 'Steam_' in s]))
