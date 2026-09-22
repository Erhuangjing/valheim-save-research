# -*- coding: utf-8 -*-
"""清除角色档里的 devcommands 记录（统计字典条目）
结构: [count int32] + N x ( [len u8][key][float32] )
"""
import os, re, shutil, struct, time, subprocess


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
FCH = r'C:/Program Files (x86)/Steam/userdata/<STEAM3ID>/892970/remote/characters/<PLAYER1>.fch'
BKROOT = BACKUP_DIR
PAT = b'\x0bdevcommands\x00\x00\xa0\x40'      # len=11 + "devcommands" + float 5.0
PAT_SHORT = b'\x0bdevcommands'
ENTRY_LEN = 16
REWIND = 38                                    # 到字典 count 字段的距离

print('=== 1) 检查游戏进程 ===')
out = subprocess.run(['tasklist'], capture_output=True).stdout.decode('gbk', 'ignore')
running = [l.split()[0] for l in out.splitlines() if 'valheim' in l.lower()]
print('  运行中的 valheim 进程: %s' % (running if running else '无 ✓'))

print('\n=== 2) 备份 ===')
os.makedirs(BKROOT, exist_ok=True)
stamp = time.strftime('%Y%m%d-%H%M%S')
bk = os.path.join(BKROOT, '<PLAYER1>_%s.fch' % stamp)
shutil.copy2(FCH, bk)
print('  ✓ %s  (%d 字节)' % (bk, os.path.getsize(bk)))

print('\n=== 3) 分析 ===')
d = bytearray(open(FCH, 'rb').read())
print('  文件大小 %d' % len(d))
spots = [m.start() for m in re.finditer(re.escape(PAT), d)]
print('  完整条目命中 %d 处: %s' % (len(spots), spots))
if not spots:
    spots2 = [m.start() for m in re.finditer(re.escape(PAT_SHORT), d)]
    print('  短模式命中: %s' % spots2)
    for s in spots2:
        print('    +%d: %s' % (s, d[s:s + 20].hex(' ')))

edits = []
for s in spots:
    cpos = s - REWIND
    raw = struct.unpack_from('<i', d, cpos)[0]
    ok = 1 <= raw <= 64
    print('\n  条目 @%d' % s)
    print('    count 位置 @%d, 当前值 = %d %s' % (cpos, raw, '✓' if ok else '✗ 异常'))
    print('    条目字节: %s' % d[s:s + ENTRY_LEN].hex(' '))
    print('    count 字节: %s' % d[cpos:cpos + 4].hex(' '))
    if ok:
        edits.append((cpos, raw, s))

print('\n=== 4) 修改方案 ===')
if not edits:
    print('  ✗ 没找到可安全修改的条目')
else:
    print('  将执行 %d 处修改:' % len(edits))
    for cpos, raw, s in edits:
        print('    @%d: count %d -> %d' % (cpos, raw, raw - 1))
        print('    @%d: 删除 %d 字节 "devcommands" 条目' % (s, ENTRY_LEN))

if running:
    print('\n⚠ 游戏还在运行 → 暂不修改（会被覆盖）。请先完全退出 Valheim。')
else:
    print('\n=== 5) 执行修改 ===')
    for cpos, raw, s in sorted(edits, reverse=True):
        del d[s:s + ENTRY_LEN]
        struct.pack_into('<i', d, cpos, raw - 1)
    open(FCH, 'wb').write(bytes(d))
    print('  ✓ 已写回，新大小 %d' % len(d))
    d2 = open(FCH, 'rb').read()
    left = len(re.findall(re.escape(PAT_SHORT), d2))
    print('  复查: 剩余 "devcommands" 出现次数 = %d %s' % (left, '✓ 已清除' if left == 0 else '✗ 仍有残留'))
    for m in re.finditer(rb'\x03die\x00\x00\x80\x3f', d2):
        c = struct.unpack_from('<i', d2, m.start() - 4)[0]
        print('  校验字典 count = %d（邻近 "die" 条目）' % c)
