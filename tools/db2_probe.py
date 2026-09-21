# -*- coding: utf-8 -*-
"""解压 _main.xx.db2（ZDO 数据库）并在其中定位地形记录"""
import os, struct, json, zlib, collections


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
CACHE = HASHLIB
with open(CACHE, encoding='utf-8') as f:
    HSLIB = {int(k): v for k, v in json.load(f).items()}

TARGETS = {
    'TerrainCompiler': 0x5465856a,
    'Heightmap': 0x16044d2b,
    'LevelTerrain': 0xdf8de8c9,
    'ZoneCtrl': 0x7988b013,
}
BASE = need('VH_WORLD_ROOT', WORLD_ROOT, 'worlds_local 根目录')
DIRS = {
    '20:39': 'WORLD_backup_auto-20260916-203929',
    '22:46': 'WORLD_backup_auto-20260916-224637',
    '22:48': 'WORLD',
}

for tag, d in DIRS.items():
    dp = os.path.join(BASE, d)
    names = [f for f in sorted(os.listdir(dp)) if f.endswith(('.db2', '.fwl2', '.chunks', '.ok'))]
    print('=' * 74)
    print('【%s】 %s' % (tag, names))
    for fn in names:
        b = open(os.path.join(dp, fn), 'rb').read()
        print('  --- %s  %d 字节  头: %s' % (fn, len(b), b[:24].hex(' ')))
        data = None
        for off in (0, 2, 4, 6, 8, 12, 16, 20, 28, 32):
            for wbits in (15, -15, 31):
                try:
                    dz = zlib.decompressobj(wbits)
                    out = dz.decompress(b[off:])
                    if len(out) > 64:
                        data = out
                        print('      ★ 解压成功 off=%d wbits=%d → %d 字节' % (off, wbits, len(out)))
                        break
                except Exception:
                    pass
            if data:
                break
        if data is None:
            print('      （不可解压，按明文处理）')
            data = b
        hits = collections.Counter()
        for nm, h in TARGETS.items():
            pat = struct.pack('<I', h)
            s = 0
            while True:
                i = data.find(pat, s)
                if i < 0:
                    break
                hits[nm] += 1
                if hits[nm] <= 3:
                    lo = max(0, i - 24)
                    print('        %-18s @%-8d 前置: %s' % (nm, i, data[lo:i + 16].hex(' ')))
                s = i + 1
        print('      命中合计: %s' % (dict(hits) if hits else '无'))
        if data is not b:
            fl, = struct.unpack_from('<H', data, 0)
            cnt, = struct.unpack_from('<i', data, 2)
            print('      解压流头: [0:2]=%d  [2:6]=%d  总长=%d' % (fl, cnt, len(data)))
