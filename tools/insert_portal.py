# -*- coding: utf-8 -*-
"""在死亡小岛上插入一个 tag='++' 的传送门

用法:
  python insert_portal.py --dry <世界目录>   # 演练（不动正式档）
  python insert_portal.py --real             # 正式执行（必须先停服）

记录结构（实测）:
  [flags u16][pos 3xf32][prefab u32][短字段][字段段...]
  string 段 = [count][ N x (hash i32 + len + utf8) ]
"""
import os, sys, struct, time, shutil, collections

sys.path.insert(0, HERE)
from zdo_lib import hs, walk, candidates, patch_counts, insert_bytes


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
REAL_SRC = need('VH_WORLD', WORLD_DIR, '世界存档目录（含 .chunk）')
# 三个墓碑的坐标（用来定位目标 chunk）
TOMBS = [(3692.3, 29.4, -97.4), (3698.5, 29.6, -93.6), (3702.6, 30.9, -78.3)]
# 新传送门落点
PORTAL_POS = (0.0, 30.0, 0.0)   # 示例坐标
NEW_TAG = b'++'


def find_chunk_with_tomb(src):
    """返回包含岛上墓碑的那个 chunk 文件名（按 prefab hash 扫，避免浮点精度问题）"""
    ph_pat = struct.pack('<I', 0xA31E0923)   # Player_tombstone
    for cf in sorted(f for f in os.listdir(src) if f.endswith('.chunk')):
        b = open(os.path.join(src, cf), 'rb').read()
        i = b.find(ph_pat)
        while i >= 0:
            p = i - 14
            if p >= 0:
                x, y, z = struct.unpack_from('<fff', b, p + 2)
                if 3600 < x < 3800 and -150 < z < -30:
                    print('  %-22s 命中岛上墓碑 @ (%.1f, %.1f, %.1f)' % (cf, x, y, z))
                    return cf
            i = b.find(ph_pat, i + 1)
    return None


def find_portal_template(src):
    """找一条带 tag 的 portal_wood 记录当模板，返回 (bytes, tag)"""
    for cf in sorted(f for f in os.listdir(src) if f.endswith('.chunk')):
        b = open(os.path.join(src, cf), 'rb').read()
        try:
            steps, pos, table, end = walk(b)
        except Exception:
            continue
        for st, L, fl, ph in steps:
            if H.get(ph) != 'portal_wood':
                continue
            raw = b[st:st + L]
            for cand in (b'shop1', b'shop'):
                if cand in raw:
                    print('  模板取自 %s off=%d len=%d tag=%s' % (cf, st, L, cand.decode()))
                    return raw, cand
    return None, None


def build_portal(tmpl, old_tag):
    """改坐标 + 换 tag + 换 creator"""
    r = bytearray(tmpl)
    struct.pack_into('<fff', r, 2, *PORTAL_POS)
    k = r.find(old_tag)
    assert k > 0 and r[k - 1] == len(old_tag), 'tag 定位失败'
    # [hash 4B][len 1B][tag] -> [hash 4B][2][++]
    head = bytes(r[:k - 1]) + bytes([len(NEW_TAG)]) + NEW_TAG
    # creator 换成她自己的 Steam ID（23 字符等长替换，零风险）
    OLD_SID = b'Steam_<REDACTED_PLAYER2>'
    NEW_SID = b'Steam_<REDACTED_OWNER>'
    assert len(OLD_SID) == len(NEW_SID)
    if OLD_SID in head:
        head = head.replace(OLD_SID, NEW_SID)
        print('  creator 已换成 Steam_<REDACTED_OWNER>（她自己）')
    return head


def run(src, real):
    print('源目录: %s' % src)
    print('\n[1] 定位目标 chunk')
    target = find_chunk_with_tomb(src)
    if not target:
        print('✗ 没找到含墓碑的 chunk'); return False

    print('\n[2] 找传送门模板')
    tmpl, old_tag = find_portal_template(src)
    if not tmpl:
        print('✗ 没找到模板'); return False

    print('\n[3] 构造新记录')
    blob = build_portal(tmpl, old_tag)
    print('  模板 %d 字节 -> 新记录 %d 字节' % (len(tmpl), len(blob)))
    print('  hex: %s' % blob.hex(' '))
    print('  坐标: (%.1f, %.1f, %.1f)   tag: %s' % (PORTAL_POS + (NEW_TAG.decode(),)))
    x, y, z = struct.unpack_from('<fff', blob, 2)
    print('  回读校验: (%.1f, %.1f, %.1f)  prefab=0x%08x' % (x, y, z, struct.unpack_from('<I', blob, 14)[0]))

    print('\n[4] 找插入点')
    cp = os.path.join(src, target)
    b = open(cp, 'rb').read()
    steps, pos, table, end = walk(b)
    c0 = struct.unpack_from('<i', b, 2)[0]
    print('  %s: 头 count=%d, walk 记录数=%d, 记录流结束=%d, 文件=%d' % (target, c0, len(steps), end, len(b)))
    print('  ★ 插入点 = %d（记录流末尾，尾部结构前）' % end)

    idxs = sorted(f for f in os.listdir(src) if f.endswith('.chunks'))
    idx_name = idxs[-1]
    ip = os.path.join(src, idx_name)
    print('  索引文件: %s' % idx_name)

    if not real:
        print('\n[DRY-RUN] 不改文件，仅报告将要发生的变化')
        print('  chunk 头 count: %d -> %d' % (c0, c0 + 1))
        ib = open(ip, 'rb').read()
        print('  索引总 ZDO 数: %d -> %d' % (struct.unpack_from('<i', ib, 2)[0], struct.unpack_from('<i', ib, 2)[0] + 1))
        return True

    print('\n[5] 写入')
    insert_bytes(cp, end, blob)
    info = patch_counts(cp, ip, target, 1)
    print('  ✓ 已插入 %d 字节到 %s @%d' % (len(blob), target, end))
    print('  chunk count: %s' % (info['chunk_count'],))
    print('  idx total  : %s' % (info['idx_total'],))
    print('  entry      : %s' % (info['entry'],))

    print('\n[6] 验证')
    b2 = open(cp, 'rb').read()
    c1 = struct.unpack_from('<i', b2, 2)[0]
    cands = candidates(b2)
    hit = []
    for p in cands:
        xx, yy, zz = struct.unpack_from('<fff', b2, p + 2)
        if abs(xx - PORTAL_POS[0]) < 0.5 and abs(zz - PORTAL_POS[2]) < 0.5:
            hit.append((p, xx, yy, zz, H.get(struct.unpack_from('<I', b2, p + 14)[0])))
    print('  chunk 头 count: %d -> %d' % (c0, c1))
    print('  新记录命中: %s' % (hit if hit else '✗ 未找到'))
    if hit:
        p = hit[0][0]
        raw2 = b2[p:p + len(blob)]
        print('  回读 tag: %s' % raw2[-3:].hex(' '))
    return len(hit) > 0


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else '--dry'
    if mode == '--dry':
        src = sys.argv[2] if len(sys.argv) > 2 else REAL_SRC
        ok = run(src, False)
    else:
        src = sys.argv[2] if len(sys.argv) > 2 else REAL_SRC
        ok = run(src, True)
    print('\n%s' % ('完成' if ok else '失败'))
