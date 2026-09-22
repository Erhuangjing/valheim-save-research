# -*- coding: utf-8 -*-
"""通用蓝图解析器：.blueprint / .vbuild / .zip / 市场 blob → 统一件清单（零 mod、纯离线）

格式一手依据（2026-09-20 核对）:
  .blueprint  PlanBuild `Blueprints/Blueprint.cs` + `PieceEntry.cs`（sirskunkalot/PlanBuild, master）
              header: #Name: / #Creator: / #Description: / #Category:
              section: #SnapPoints / #Terrain / #Pieces（状态机，默认 Pieces；其余 # 行是注释）
              piece 行: name;category;px;py;pz;qx;qy;qz;qw;additionalInfo;sx;sy;sz[;zdoData;chance]
                        └ 扩展字段 13/14 = Infinity Hammer / Expand World Data 追加，PlanBuild 忽略
  .vbuild     BuildShare 格式。PlanBuild `PieceEntry.FromVBuild` 与
              mikermeme/ValheimBuildConverter `vbuildCodec`（JS）两套独立实现一致：
              空白分隔: name qx qy qz qw px py pz   （≥8 段；四元数在前、位置在后）
  市场 blob   Blueprint.ToBlob(): Utils.Compress(=raw deflate) 包裹 [int32 行数][N×C# string][int32 缩略图长][png]
  .zip        Nexus/Thunderstore 常见分发形态，内含 .blueprint + 同名 .png 缩略图

与 PlanBuild 的两处有意分歧（更稳、不改变数值语义）:
  1) 逗号小数点兼容按"逐字段"处理（PlanBuild 是整行替换 `,`→`.`，会把 additionalInfo 里
     的英文逗号也换掉——那是它的已知怪癖，我们不跟进）
  2) name 截断 `(` 后缀与 PlanBuild 一致（`name.Split('(')[0]`）

用法:
  python3 bp_parse.py FILE [--json out.json] [--txt out.txt] [--structure-only]
  python3 bp_parse.py --selftest

输出:
  --json  全保真（四元数、scale、additionalInfo、zdoData、地形/吸附点段）
  --txt   XiBpBuilder 件清单兼容格式: name|hash|x|y|z|yaw(度)   ← 注意 yaw 单值有保真损失，
          非纯 yaw 旋转的件（45°/26° 斜面屋顶等）会单独统计并告警，落地前建议目视确认
  stdout  预检报告：件数/分类/包围盒/哈希命中率/GroundLayerPy 自动推导/地下结构/非纯yaw 计数

GroundLayerPy 自动推导 = 《Valheim_蓝图落地自动化_自动锚点求解_收官报告》§9.1 算法的移植:
  a) 有 wood_fence 院墙 → 取院墙 py 众数（《交接文档》§1.3 的做法）
  b) 无院墙 → py 直方图（0.5 米档），从最低档往上**跳过孤立低层**
     （与上方最近档之间隔着空档的层），取第一个非孤立档的最小 py
"""
import os
import sys
import json
import math
import struct
import zipfile
import argparse
import collections
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HASHLIB_PATH = os.path.join(REPO, 'format', 'prefab-hashlib.json')

# 地下结构判定阈值（XiBpBuilder [Terrain] DeepPy 默认值）
DEEP_PY = -1.2
STRUCT_CATS = ('Building',)

# 原版地形操作件（锄头/耕地机/镐的 TerrainOp prefab，hashlib + 本地化键 piece_levelground 等核对）。
# 它们不是建筑：执行一次就自毁、不留 ZDO。执行器按原版 TerrainOp 回放，离线对账不应期望它们存在。
# vbuild 蓝图没有 #Terrain 段，作者的锄头整地就以这些件的形式记在件清单里（longhouse：327 个 mud_road）
TERRAIN_OP_PIECES = frozenset({
    'mud_road', 'mud_road_v2',          # 整平地面（Level ground）
    'path', 'path_v2',                  # 小路
    'paved_road', 'paved_road_v2',      # 铺石路
    'raise', 'raise_v2',                # 抬高地面
    'cultivate', 'cultivate_v2', 'replant',
    'digg', 'digg_v2', 'digg_v3',
})


# ---------------------------------------------------------------- StableHash
def stable_hash(s):
    """Valheim StableHashCode：DJB2 双累加器，种子 5381、乘子 1566083941。
    已实测与游戏 ZNetScene.GetPrefabHash 一致（stone_stair → 389771597）。"""
    h1 = h2 = 5381
    for i in range(0, len(s), 2):
        h1 = ((h1 << 5) + h1) ^ ord(s[i])
        if i == len(s) - 1:
            break
        h2 = ((h2 << 5) + h2) ^ ord(s[i + 1])
    return (h1 + h2 * 1566083941) & 0xFFFFFFFF


_REV = None


def rev_lookup():
    """hash → name 反查表（format/prefab-hashlib.json，57,706 条）"""
    global _REV
    if _REV is None:
        try:
            with open(HASHLIB_PATH, encoding='utf-8') as f:
                _REV = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            _REV = {}
    return _REV


# ---------------------------------------------------------------- 基础解析
def _f(s):
    """InvariantFloat + 逗号小数点逐字段兼容"""
    if s is None:
        return 0.0
    s = s.strip().replace(',', '.')
    if not s:
        return 0.0
    return float(s)


def _json_str(s):
    """additionalInfo：PlanBuild 存的是 JSON 序列化字符串（'""' = 空）"""
    if s is None:
        return None
    s = s.strip()
    if not s or s == '""':
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, str) and v else (v if v else None)
    except ValueError:
        return s


def quat_to_yaw_deg(qx, qy, qz, qw):
    """Unity 四元数 → 绕 Y 轴 yaw（度，[0,360)）。
    yaw = atan2(2(xz+wy), 1-2(x²+y²))，纯 Y 旋转时精确。"""
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if n < 1e-9:
        return 0.0, True
    qx, qy, qz, qw = qx / n, qy / n, qz / n, qw / n
    yaw = math.degrees(math.atan2(2.0 * (qx * qz + qw * qy),
                                  1.0 - 2.0 * (qx * qx + qy * qy))) % 360.0
    pure = abs(qx) < 1e-4 and abs(qz) < 1e-4        # 纯 yaw ⇔ 无 X/Z 分量
    return yaw, pure


def parse_blueprint_lines(lines):
    """.blueprint 行序列 → dict（复刻 Blueprint.FromArray 的状态机）"""
    bp = {'format': 'blueprint', 'name': None, 'creator': None, 'description': None,
          'category': None, 'pieces': [], 'snappoints': [], 'terrain': [],
          'center': None, 'warnings': []}
    state = 'pieces'
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#Name:'):
            bp['name'] = line[6:]; continue
        if line.startswith('#Creator:'):
            bp['creator'] = line[9:]; continue
        if line.startswith('#Description:'):
            bp['description'] = _json_str(line[13:]) or line[13:]; continue
        if line.startswith('#Category:'):
            bp['category'] = line[10:] or None; continue
        if line.startswith('#center:'):                    # Expand World Data 中心件标记
            bp['center'] = line[8:]; continue
        if line == '#SnapPoints':
            state = 'snap'; continue
        if line == '#Terrain':
            state = 'terrain'; continue
        if line == '#Pieces':
            state = 'pieces'; continue
        if line.startswith('#'):                            # TerrainHeight/TerrainPaint 等扩展段
            if line.startswith('#TerrainHeight:') or line.startswith('#TerrainPaint:'):
                bp['warnings'].append('含 Infinity Hammer 地形快照段（%s…），本解析器不展开' % line[:24])
            continue
        if state == 'snap':
            p = line.split(';')
            if len(p) >= 3:
                bp['snappoints'].append({'x': _f(p[0]), 'y': _f(p[1]), 'z': _f(p[2])})
            continue
        if state == 'terrain':
            p = line.split(';')                              # shape;x;y;z;radius;rotation;smooth;paint
            if len(p) >= 8:
                bp['terrain'].append({'shape': p[0], 'x': _f(p[1]), 'y': _f(p[2]), 'z': _f(p[3]),
                                      'radius': _f(p[4]), 'rotation': int(_f(p[5])),
                                      'smooth': _f(p[6]), 'paint': p[7]})
            continue
        # ---- piece 行 ----
        p = line.split(';')
        if len(p) < 9:
            bp['warnings'].append('字段不足 9 段，跳过: %s' % line[:60])
            continue
        name = p[0].split('(')[0]
        qx, qy, qz, qw = _f(p[5]), _f(p[6]), _f(p[7]), _f(p[8])
        yaw, pure = quat_to_yaw_deg(qx, qy, qz, qw)
        sx, sy, sz = (_f(p[10]), _f(p[11]), _f(p[12])) if len(p) > 12 and p[10:13] != [''] * 3 else (1.0, 1.0, 1.0)
        piece = {'name': name, 'category': p[1] or 'Building',
                 'x': _f(p[2]), 'y': _f(p[3]), 'z': _f(p[4]),
                 'qx': qx, 'qy': qy, 'qz': qz, 'qw': qw,
                 'yaw': round(yaw, 4), 'pureYaw': pure,
                 'info': _json_str(p[9]),
                 'scale': [sx, sy, sz],
                 'zdoData': p[13] if len(p) > 13 and p[13].strip() else None,
                 'chance': _f(p[14]) if len(p) > 14 and p[14].strip() else None}
        bp['pieces'].append(piece)
    return bp


def parse_vbuild_lines(lines):
    """.vbuild（BuildShare）双方言（issue #17 补方言 2）：
    方言1（≥8 段）: name qx qy qz qw px py pz [zdoData chance]
    方言2（5/6 段）: name cos [sin] px py pz —— sin==0 时省略成 5 段（旧 BuildShare）
    判别特征（实测 longhouse.vbuild）：列数 5/6 且 cos²+sin²≈1；角度为 11.25° 整数倍（仅告警不强判）"""
    import math as _m
    bp = {'format': 'vbuild', 'name': None, 'creator': None, 'description': None,
          'category': None, 'pieces': [], 'snappoints': [], 'terrain': [],
          'center': None, 'warnings': []}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        has_dot = '.' in line
        if ',' in line and not has_dot:      # BuildConverter 的逗号兼容（整行无 . 才替换）
            line = line.replace(',', '.')
        p = line.split()
        if len(p) in (5, 6):
            # ---- 方言 2：name cos [sin] px py pz ----
            try:
                cos = _f(p[1])
                sin = _f(p[2]) if len(p) == 6 else 0.0
                px, py, pz = _f(p[-3]), _f(p[-2]), _f(p[-1])
            except Exception:
                bp['warnings'].append('5/6 段行数值解析失败，跳过: %s' % line[:60])
                continue
            if abs(cos * cos + sin * sin - 1.0) > 0.01:
                bp['warnings'].append('5/6 段行但 cos²+sin²≠1（非旋转分量方言），跳过: %s' % line[:60])
                continue
            yaw = _m.degrees(_m.atan2(sin, cos)) % 360.0
            if abs(yaw / 11.25 - round(yaw / 11.25)) > 0.02:
                bp['warnings'].append('方言2 角度非 11.25° 整数倍（%.2f°），仍按值采用: %s' % (yaw, line[:60]))
            half = _m.radians(yaw) / 2.0
            bp['pieces'].append({'name': p[0].split('(')[0], 'category': 'Building',
                                 'x': px, 'y': py, 'z': pz,
                                 'qx': 0.0, 'qy': round(_m.sin(half), 6), 'qz': 0.0,
                                 'qw': round(_m.cos(half), 6),
                                 'yaw': round(yaw, 4), 'pureYaw': True,
                                 'info': None, 'scale': [1.0, 1.0, 1.0],
                                 'zdoData': None, 'chance': None})
            continue
        if len(p) < 8:
            bp['warnings'].append('字段不足 8 段（也非 5/6 段方言2），跳过: %s' % line[:60])
            continue
        name = p[0].split('(')[0]
        qx, qy, qz, qw = _f(p[1]), _f(p[2]), _f(p[3]), _f(p[4])
        yaw, pure = quat_to_yaw_deg(qx, qy, qz, qw)
        bp['pieces'].append({'name': name, 'category': 'Building',
                             'x': _f(p[5]), 'y': _f(p[6]), 'z': _f(p[7]),
                             'qx': qx, 'qy': qy, 'qz': qz, 'qw': qw,
                             'yaw': round(yaw, 4), 'pureYaw': pure,
                             'info': None, 'scale': [1.0, 1.0, 1.0],
                             'zdoData': p[8] if len(p) > 8 and p[8].strip() else None,
                             'chance': _f(p[9]) if len(p) > 9 else None})
    return bp


def parse_market_blob(data):
    """PlanBuild 市场 blob：raw deflate/zlib/gzip 三种包裹都试。
    载荷 = [int32 行数][N × C# BinaryWriter string][int32 缩略图长][png]"""
    lines = None
    for wbits in (-15, 15, 47):
        try:
            raw = zlib.decompress(data, wbits)
        except zlib.error:
            continue
        try:
            off = 0
            n = struct.unpack_from('<i', raw, off)[0]; off += 4
            if not 0 < n < 5_000_000:
                continue
            out = []
            for _ in range(n):                       # C# string = 7bit 变长长度前缀 + UTF8
                ln, shift = 0, 0
                while True:
                    b = raw[off]; off += 1
                    ln |= (b & 0x7F) << shift
                    if not b & 0x80:
                        break
                    shift += 7
                out.append(raw[off:off + ln].decode('utf-8', 'replace')); off += ln
            lines = out
            break
        except (struct.error, IndexError, UnicodeDecodeError):
            continue
    if lines is None:
        raise ValueError('不是可识别的 PlanBuild blob（试过 raw deflate / zlib / gzip）')
    return parse_blueprint_lines(lines)


def load_any(path):
    """按扩展名 + 内容嗅探加载任意蓝图载体，返回 (bp, 来源说明)"""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.zip':
        with zipfile.ZipFile(path) as z:
            inner = [n for n in z.namelist()
                     if n.lower().endswith(('.blueprint', '.vbuild')) and not n.startswith('__MACOSX')]
            if not inner:
                raise ValueError('zip 里没有 .blueprint / .vbuild')
            name = inner[0]
            text = z.read(name).decode('utf-8-sig', 'replace')
            bp = (parse_vbuild_lines if name.lower().endswith('.vbuild')
                  else parse_blueprint_lines)(text.splitlines())
            bp['name'] = bp['name'] or os.path.splitext(os.path.basename(name))[0]
            return bp, 'zip:%s!%s' % (os.path.basename(path), name)
    data = open(path, 'rb').read()
    if ext == '.vbuild':
        return parse_vbuild_lines(data.decode('utf-8-sig', 'replace').splitlines()), path
    if ext == '.blueprint':
        return parse_blueprint_lines(data.decode('utf-8-sig', 'replace').splitlines()), path
    # 未知扩展名：先当文本嗅探，再当 blob
    try:
        text = data.decode('utf-8-sig')
        sample = [l for l in text.splitlines()[:200] if l.strip() and not l.startswith('#')]
        if sample and (';' in sample[0] or len(sample[0].split()) >= 8):
            fmt = 'vbuild' if ';' not in sample[0] and len(sample[0].split()) >= 8 else 'blueprint'
            bp = (parse_vbuild_lines if fmt == 'vbuild' else parse_blueprint_lines)(text.splitlines())
            if bp['pieces']:
                return bp, '%s（按 %s 嗅探）' % (path, fmt)
    except UnicodeDecodeError:
        pass
    return parse_market_blob(data), '%s（按市场 blob 解）' % path


# ---------------------------------------------------------------- 预检分析
def ground_layer_py(pieces):
    """自动推导地面层 py。返回 (py, 方法说明)。收官报告 §9.1 算法移植 + 地形件优先。"""
    fences = [p['y'] for p in pieces if p['name'] == 'wood_fence']
    if len(fences) >= 4:
        cnt = collections.Counter(round(y, 2) for y in fences)
        py, n = cnt.most_common(1)[0]
        return py, 'wood_fence 众数（%d 段院墙，%d 段同高）' % (len(fences), n)
    # 原版锄头件（mud_road 整地 / paved_road …）的 y 就是作者当时整出来的地表——比直方图可靠：
    # longhouse 的直方图会挑中打进地里 6~7m 的深桩（-6.83），照它落地主楼层会悬空 6m
    ops = sorted(p['y'] for p in pieces if p['name'] in TERRAIN_OP_PIECES)
    if len(ops) >= 10:
        return round(ops[len(ops) // 2], 2), '原版地形件中位数（%d 个锄头整地/路面件 = 作者当时的地表）' % len(ops)
    pieces = [p for p in pieces if p['name'] not in TERRAIN_OP_PIECES]
    bins = collections.defaultdict(list)
    for p in pieces:
        bins[math.floor(p['y'] / 0.5)].append(p['y'])
    if not bins:
        return 0.0, '空蓝图'
    keys = sorted(bins)
    for k in keys:
        if (k + 1) in bins:                 # 紧邻上一档非空 → 非孤立
            return min(bins[k]), 'py 直方图：最低非孤立档 [%.2f,%.2f)（跳过孤立低层）' % (k * 0.5, (k + 1) * 0.5)
    return min(bins[keys[0]]), 'py 直方图：全部孤立，取最低档'


def analyze(bp, structure_only=False):
    pieces = bp['pieces']
    if structure_only:
        pieces = [p for p in pieces if p['category'] in STRUCT_CATS]
    rep = {}
    rep['total'] = len(bp['pieces'])
    rep['selected'] = len(pieces)
    rep['cats'] = dict(collections.Counter(p['category'] for p in bp['pieces']))
    if not pieces:
        return rep
    xs = [p['x'] for p in pieces]; ys = [p['y'] for p in pieces]; zs = [p['z'] for p in pieces]
    rep['bbox'] = {'x': [round(min(xs), 2), round(max(xs), 2)],
                   'y': [round(min(ys), 2), round(max(ys), 2)],
                   'z': [round(min(zs), 2), round(max(zs), 2)],
                   'footprint_m': [round(max(xs) - min(xs), 1), round(max(zs) - min(zs), 1)]}
    rev = rev_lookup()
    hit = miss = 0
    missing = collections.Counter()
    for p in pieces:
        h = stable_hash(p['name'])
        p['hash'] = h
        nm = rev.get(h)
        if nm == p['name']:
            hit += 1
        else:
            miss += 1
            missing[p['name']] += 1
            p['hashNote'] = ('反查为 %s（名字冲突?）' % nm) if nm else '哈希库未收录（新构件/mod 构件?）'
    rep['hash_hit'] = hit
    rep['hash_miss'] = miss
    rep['hash_missing_names'] = missing.most_common(10)
    gpy, how = ground_layer_py(pieces)
    rep['ground_layer_py'] = gpy
    rep['ground_layer_method'] = how
    rep['underground'] = sum(1 for p in pieces if p['y'] < DEEP_PY)
    rep['below_ground_layer'] = sum(1 for p in pieces if p['y'] < gpy - 0.5)
    rep['non_pure_yaw'] = sum(1 for p in pieces if not p['pureYaw'])
    kinds = collections.Counter(p['name'] for p in pieces)
    rep['kinds'] = len(kinds)
    rep['top_kinds'] = kinds.most_common(8)
    rep['with_info'] = sum(1 for p in pieces if p['info'])
    rep['with_zdoData'] = sum(1 for p in pieces if p.get('zdoData'))
    rep['with_scale'] = sum(1 for p in pieces if p['scale'] != [1.0, 1.0, 1.0])
    rep['snappoints'] = len(bp['snappoints'])
    rep['terrain_mods'] = len(bp['terrain'])
    return rep


# ---------------------------------------------------------------- 输出
def to_txt(bp, pieces):
    """XiBpBuilder 件清单兼容: name|hash|x|y|z|yaw"""
    out = ['# 共 %d 件   由 bp_parse.py 生成（%s 格式）' % (len(pieces), bp['format'])]
    for p in pieces:
        out.append('%s|%d|%.4f|%.4f|%.4f|%.4f' % (p['name'], p.get('hash') or stable_hash(p['name']),
                                                  p['x'], p['y'], p['z'], p['yaw']))
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------- 自检
SELFTEST_BLUEPRINT = """#Name:selftest_house
#Creator:bp_parse
#Description:"a tiny fixture"
#Category:Testing
#SnapPoints
0;0;2
2;0;0
#Terrain
flat;0;0;0;4;0;0.5;dirt
#Pieces
wood_fence;Building;0;-0.7;0;0;0;0;1;"";1;1;1
wood_fence;Building;2;-0,7;0;0;0;0;1;"";1;1;1
wood_fence;Building;0;-0.7;2;0;0;0;1;"";1;1;1
wood_fence;Building;2;-0.7;2;0;0;0;1;"";1;1;1
stone_stair;Building;1;-6,3567;0,5;1;0;0;0;"";1;1;1
wood_wall_log_4x0.5(2);Building;3;-2.5;1;0.7071068;0;0;0.7071068;"";1;1;1
sign;Furniture;-1;0;0;0;0;0;1;"hello, world";1;1;1
piece_x;Building;0;0;0;0;1;0;0;"";1;1;1;infinite_health;0.35
short;line;only
"""

SELFTEST_VBUILD = """stone_floor_2x2 0 0 0 1 0 0 0
wood_gate(Left) 0 0.7071068 0 0 -2 0 1.5
wood_wall_45x1 0.7071068 0.7071068 2.5 1.5 -3.5
wood_floor 1 4 0 0.5
bad line
"""


def selftest():
    ok = True

    def check(label, cond):
        nonlocal ok
        print('  [%s] %s' % ('PASS' if cond else 'FAIL', label))
        ok = ok and bool(cond)

    print('== StableHash 已知值 ==')
    check('stone_stair = 389771597（交接文档实测）', stable_hash('stone_stair') == 389771597)
    check('wood_wall_log_4x0.5 = 214591025（激活报告补算）', stable_hash('wood_wall_log_4x0.5') == 214591025)
    rev = rev_lookup()
    if rev:
        check('哈希库反查 stone_stair 一致', rev.get(389771597) == 'stone_stair')
    else:
        print('  [SKIP] 哈希库不可读（%s）' % HASHLIB_PATH)

    print('== .blueprint fixture ==')
    bp = parse_blueprint_lines(SELFTEST_BLUEPRINT.splitlines())
    check('header 四件套', bp['name'] == 'selftest_house' and bp['creator'] == 'bp_parse'
          and bp['description'] == 'a tiny fixture' and bp['category'] == 'Testing')
    check('吸附点 2 / 地形段 1', len(bp['snappoints']) == 2 and len(bp['terrain']) == 1
          and bp['terrain'][0]['shape'] == 'flat')
    check('坏行跳过且告警', len(bp['pieces']) == 8 and any('字段不足' in w for w in bp['warnings']))
    check('逗号小数点兼容（-0,7 → -0.7 / -6,3567 → -6.3567）',
          abs(bp['pieces'][1]['y'] + 0.7) < 1e-6 and abs(bp['pieces'][4]['y'] + 6.3567) < 1e-4
          and abs(bp['pieces'][4]['z'] - 0.5) < 1e-6)
    check('name 截断 ( 后缀', bp['pieces'][5]['name'] == 'wood_wall_log_4x0.5')
    check('additionalInfo 含逗号不被破坏', bp['pieces'][6]['info'] == 'hello, world')
    check('扩展字段 zdoData/chance', bp['pieces'][7]['zdoData'] == 'infinite_health'
          and abs(bp['pieces'][7]['chance'] - 0.35) < 1e-6)
    check('pureYaw 判定（qw=1 → 纯；qx≠0 → 非纯）',
          bp['pieces'][0]['pureYaw'] and not bp['pieces'][5]['pureYaw'])
    check('yaw: (0,1,0,0) → 180°', abs(bp['pieces'][7]['yaw'] - 180.0) < 1e-3)

    print('== GroundLayerPy 推导（收官报告 §9.1 场景复现）==')
    # usagi 真实分布: -2.6 档 8 件孤立(上方 -2.5~-2.0 为空) → 跳过；-1.60 单件但紧邻 -1.50 大层 → 命中
    fake = ([{'name': 'darkwood_decowall', 'y': -2.6 + i * 0.001, 'x': 0, 'z': 0, 'category': 'Building',
              'qx': 0, 'qy': 0, 'qz': 0, 'qw': 1, 'yaw': 0, 'pureYaw': True, 'info': None,
              'scale': [1, 1, 1], 'zdoData': None, 'chance': None} for i in range(8)]
            + [{'name': 'stone_floor_2x2', 'y': -1.60, 'x': 0, 'z': 0, 'category': 'Building',
                'qx': 0, 'qy': 0, 'qz': 0, 'qw': 1, 'yaw': 0, 'pureYaw': True, 'info': None,
                'scale': [1, 1, 1], 'zdoData': None, 'chance': None}]
            + [{'name': 'wood_floor_1x1', 'y': -1.45 + (i % 5) * 0.02, 'x': i, 'z': 0, 'category': 'Building',
                'qx': 0, 'qy': 0, 'qz': 0, 'qw': 1, 'yaw': 0, 'pureYaw': True, 'info': None,
                'scale': [1, 1, 1], 'zdoData': None, 'chance': None} for i in range(80)])
    gpy, how = ground_layer_py(fake)
    check('孤立低层被跳过 → -1.60（不是 -2.6 / -1.10）', abs(gpy + 1.60) < 1e-6)
    gpy2, _ = ground_layer_py(bp['pieces'])
    check('wood_fence 众数路径 → -0.7', abs(gpy2 + 0.7) < 1e-6)
    # longhouse 型：几根深桩在 -6.8（直方图会误选），12 个 mud_road 在 -0.6 → 应取 -0.6
    lh = ([dict(fake[0], name='wood_pole2', y=-6.83 + i * 0.01) for i in range(3)]
          + [dict(fake[0], name='wood_beam', y=-6.8)]
          + [dict(fake[0], name='wood_floor', y=-0.4) for _ in range(20)]
          + [dict(fake[0], name='mud_road', y=-0.6 + (i % 3) * 0.02) for i in range(12)])
    gpy3, how3 = ground_layer_py(lh)
    check('原版地形件路径 → -0.6（不被深桩带偏）', abs(gpy3 + 0.58) < 0.03 and '地形件' in how3)

    print('== .vbuild fixture ==')
    vb = parse_vbuild_lines(SELFTEST_VBUILD.splitlines())
    check('方言1 四元数在前、位置在后', len(vb['pieces']) == 4
          and vb['pieces'][1]['name'] == 'wood_gate'
          and abs(vb['pieces'][1]['x'] + 2) < 1e-6 and abs(vb['pieces'][1]['z'] - 1.5) < 1e-6)
    check('category 固定 Building', all(p['category'] == 'Building' for p in vb['pieces']))
    d2a, d2b = vb['pieces'][2], vb['pieces'][3]
    check('方言2 6段（cos sin px py pz）: 45° / 坐标正确',
          d2a['name'] == 'wood_wall_45x1' and abs(d2a['yaw'] - 45.0) < 0.01
          and (d2a['x'], d2a['y'], d2a['z']) == (2.5, 1.5, -3.5))
    check('方言2 5段（sin 省略）: 0° / 坐标正确',
          d2b['name'] == 'wood_floor' and abs(d2b['yaw']) < 1e-6
          and (d2b['x'], d2b['y'], d2b['z']) == (4, 0, 0.5))
    check('方言2 四元数与 yaw 自洽（qy=sin(y/2) qw=cos(y/2)）',
          abs(d2a['qy'] - 0.3826834) < 1e-4 and abs(d2a['qw'] - 0.9238795) < 1e-4
          and d2a['pureYaw'])
    check('坏行告警', any('字段不足' in w for w in vb['warnings']))

    print('== 输出格式 ==')
    rep = analyze(bp)
    txt = to_txt(bp, bp['pieces'])
    first = [l for l in txt.splitlines() if not l.startswith('#')][0]
    check('txt 行 = name|hash|x|y|z|yaw', first.startswith('wood_fence|%d|' % stable_hash('wood_fence'))
          and len(first.split('|')) == 6)
    check('预检统计齐全', rep['total'] == 8 and rep['non_pure_yaw'] == 2 and rep['kinds'] == 5)

    print('\n%s' % ('全部通过 ✓' if ok else '有失败项 ✗'))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def main(argv):
    ap = argparse.ArgumentParser(description='Valheim 蓝图通用解析器（.blueprint/.vbuild/zip/blob）')
    ap.add_argument('file', nargs='?', help='蓝图文件路径')
    ap.add_argument('--json', help='输出全保真 JSON 到该路径')
    ap.add_argument('--txt', help='输出 XiBpBuilder 件清单（name|hash|x|y|z|yaw）到该路径')
    ap.add_argument('--structure-only', action='store_true', help='txt/统计只含 Building 类（过滤家具）')
    ap.add_argument('--selftest', action='store_true', help='跑内置自检')
    a = ap.parse_args(argv)

    if a.selftest or not a.file:
        return selftest()

    bp, src = load_any(a.file)
    if not bp['pieces']:
        # issue #17：解析出 0 件 = 格式未识别/文件损坏，必须致命退出（否则 preflight 报 0/0 ok）
        print('✗ 解析出 0 件（格式未识别或空文件）—— 致命错误，拒绝输出（issue #17）')
        for w in bp['warnings'][:8]:
            print('  [warn] %s' % w)
        return 1
    pieces = [p for p in bp['pieces']
              if (p['category'] in STRUCT_CATS if a.structure_only else True)]
    rep = analyze(bp, structure_only=a.structure_only)

    print('来源: %s（%s 格式）' % (src, bp['format']))
    print('名称: %s   作者: %s   分类: %s' % (bp['name'], bp['creator'], bp['category']))
    print('件数: %d（选用 %d）   构件种类: %s' % (rep['total'], rep['selected'], rep.get('kinds')))
    print('分类分布: %s' % rep['cats'])
    if 'bbox' in rep:
        b = rep['bbox']
        print('包围盒: X %.1f~%.1f  Y %.1f~%.1f  Z %.1f~%.1f（占地 %.1f × %.1f 米）'
              % (b['x'][0], b['x'][1], b['y'][0], b['y'][1], b['z'][0], b['z'][1],
                 b['footprint_m'][0], b['footprint_m'][1]))
        print('哈希命中: %d / 未命中: %d%s' % (rep['hash_hit'], rep['hash_miss'],
              ('   未命中名单: %s' % rep['hash_missing_names']) if rep['hash_miss'] else ''))
        print('GroundLayerPy = %.2f（%s）' % (rep['ground_layer_py'], rep['ground_layer_method']))
        print('地下件（py < %.1f）: %d   低于地面层 0.5 米以上: %d'
              % (DEEP_PY, rep['underground'], rep['below_ground_layer']))
        if rep['underground']:
            print('  ⚠ 含地下结构 → 落地需要地形开挖（地窖 = 崩塌高危，见交接文档 §6.2）')
        print('非纯 yaw 旋转件: %d%s' % (rep['non_pure_yaw'],
              '   ⚠ txt 件清单只存 yaw，这些件（斜面屋顶/斜梁等）落地后需目视确认' if rep['non_pure_yaw'] else ''))
        print('附加数据: info %d 件 / zdoData %d 件 / 非 1:1 缩放 %d 件'
              % (rep['with_info'], rep['with_zdoData'], rep['with_scale']))
        print('吸附点 %d / 地形修改段 %d' % (rep['snappoints'], rep['terrain_mods']))
        print('主要构件: %s' % rep['top_kinds'])
    for w in bp['warnings'][:8]:
        print('  [warn] %s' % w)

    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            json.dump({'source': src, 'meta': {k: bp[k] for k in
                       ('format', 'name', 'creator', 'description', 'category', 'center')},
                       'report': rep, 'pieces': pieces,
                       'snappoints': bp['snappoints'], 'terrain': bp['terrain']},
                      f, ensure_ascii=False, indent=1)
        print('✓ JSON → %s' % a.json)
    if a.txt:
        with open(a.txt, 'w', encoding='utf-8') as f:
            f.write(to_txt(bp, pieces))
        print('✓ 件清单 → %s' % a.txt)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
