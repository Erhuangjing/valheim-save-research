# -*- coding: utf-8 -*-
"""检查角色存档 + 世界 db2 里的作弊标记"""
import os, re, gzip, io, struct

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
D = r'D:/SteamLibrary/steamapps/common/Valheim dedicated server/save/worlds_local/WORLD'
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
