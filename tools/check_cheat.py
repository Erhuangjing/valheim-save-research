# -*- coding: utf-8 -*-
"""检查角色存档 + 世界 db2 里的作弊标记"""
import os, re, gzip, io, struct


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
def strings(data, minlen=4):
    return sorted(set(m.group().decode('ascii', 'replace')
                      for m in re.finditer(rb'[\x20-\x7e]{%d,60}' % minlen, data)))

KEYS = ['cheat', 'Cheat', 'CHEAT', 'devcommand', 'locked', 'Locked']

print('=' * 70)
print('1) 角色存档')
chdir = r'C:/Program Files (x86)/Steam/userdata/<STEAM3ID>/892970/remote/characters'
if os.path.isdir(chdir):
    for f in sorted(os.listdir(chdir)):
        if not f.endswith('.fch'):
            continue
        p = os.path.join(chdir, f)
        d = open(p, 'rb').read()
        ss = strings(d)
        hit = [s for s in ss if any(k in s for k in KEYS)]
        print('\n  %s  (%d 字节)' % (f, len(d)))
        print('    字符串: %s' % (ss[:25] if ss else '（无）'))
        print('    ★ 作弊相关命中: %s' % (hit if hit else '无'))
else:
    print('  目录不存在')

print()
print('=' * 70)
print('2) 世界 db2（16 字节头 + gzip）')
D = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
db2 = sorted(f for f in os.listdir(D) if f.endswith('.db2'))[-1]
p = os.path.join(D, db2)
raw = open(p, 'rb').read()
print('  %s  (%d 字节)' % (db2, len(raw)))
print('  头部 16 字节: %s' % raw[:16].hex(' '))
try:
    dec = gzip.decompress(raw[16:])
    print('  解压后 %d 字节' % len(dec))
    ss = strings(dec)
    hit = [s for s in ss if any(k in s for k in KEYS)]
    print('  字符串样本: %s' % ss[:20])
    print('  ★ 作弊相关命中: %s' % (hit if hit else '无'))
except Exception as e:
    print('  解压失败: %s' % e)

print()
print('=' * 70)
print('3) fwl2 里所有 globalKey（去重）')
fwl = sorted(f for f in os.listdir(D) if f.endswith('.fwl2'))[-1]
d = open(os.path.join(D, fwl), 'rb').read()
# 从 0x1600 附近开始是 key 区，直接提取所有长度前缀字符串
ss = strings(d, 4)
uniq = []
for s in ss:
    if s not in uniq:
        uniq.append(s)
print('  去重后 %d 条:' % len(uniq))
for s in uniq:
    tag = ''
    if 'preset ' in s:
        tag = '  (modifier 记录)'
    elif s.startswith('Steam_'):
        tag = '  (玩家ID)'
    print('    %r%s' % (s[:90], tag))
