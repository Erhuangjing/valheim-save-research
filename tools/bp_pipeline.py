# -*- coding: utf-8 -*-
"""bp_pipeline —— 蓝图落地一条命令编排器（七段流水线，可行性调研 §4.2 的落地实现）。

把「解析预检 → 自动选点 → 备份 → 部署执行器 → headless 落地盯日志 → 离线对账 → 删净 BepInEx → 报告」
串成可断点续跑的阶段机：阶段状态落 <work>/state.json，任何一段失败即停，重跑只补缺的段。

    python bp_pipeline.py preflight --bp usagi.blueprint --work runs/usagi1
    python bp_pipeline.py autosite  --work runs/usagi1 --world "<存档根>/worlds_local/WORLD"
    python bp_pipeline.py backup    --work runs/usagi1 --world "<存档根>/worlds_local/WORLD"
    python bp_pipeline.py install   --work runs/usagi1 --server "<服务器安装目录>" \
                                     --bepinex-src "<BepInExPack 解包目录>" --plugin-dll XiBpBuilder.dll \
                                     --site-x -262 --site-z 270 --platform-y 39.4
    python bp_pipeline.py run       --work runs/usagi1 --server "<服务器安装目录>" [--bat start_headless_server.bat]
    python bp_pipeline.py verify    --work runs/usagi1
    python bp_pipeline.py teardown  --work runs/usagi1 --server "<服务器安装目录>"
    python bp_pipeline.py report    --work runs/usagi1
    # 或一条龙（参数取并集，任何一段失败即停）：
    python bp_pipeline.py all --bp ... --work ... --world ... --server ... --bepinex-src ... --plugin-dll ...

内建铁律（可行性调研 §4.4，违反即拒绝执行）：
  1. 动档前必备份、必停服（install/run/teardown 都做进程探测）
  2. 重跑 = install 时清掉 BepInEx/config/*.flag；永不生成 Force=true
  3. 对账名字+位置双匹配（bp_reconcile，坑 C.2）
  4. 崩塌判据看件数时序，不看「支撑不足=0」（run 段的观察窗采样）
  5. 地下结构（preflight 报 underground>0）默认劝退，all 需 --allow-high-risk 才继续
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bp_parse            # noqa: E402
import bp_reconcile        # noqa: E402
import bp_autosite         # noqa: E402

CFG_GUID = 'com.world.bpbuild'          # 参考骨架的 BepInPlugin GUID；真版插件 GUID 不同（issue #9），用 --cfg-guid 指定
FOUR_PIECE = ['winhttp.dll', 'doorstop_config.ini', 'doorstop_libs', 'BepInEx']
STAGES = ['preflight', 'autosite', 'backup', 'install', 'run', 'verify', 'teardown', 'report']

# run 段盯日志的标记（对齐 xibpbuilder_reference/Plugin.cs 的 Log.LogInfo 文案；
# 兼容真版 v0.31 的「实例化就绪」措辞 —— 收官报告 §6 合格标准表）
MARKERS = {
    # 参考插件打印「[激活] refPos=... 覆盖生效」；真版 v0.31 打印「本进程拥有 N」——两种都认
    'owned':      re.compile(r'本进程拥有\s*=?\s*(\d+)|\[激活\]\s*refPos=.*?覆盖生效'),
    'platform':   re.compile(r'★\s*\[落地\]\s*PlatformY\s*=\s*(-?[\d.]+)'),
    'terrain':    re.compile(r'★\s*\[地形\]\s*完成：改\s*(\d+)\s*个顶点；满权重顶点误差\s*p95=([\d.]+)\s*max=([\d.]+)m；'
                             r'占地\s*(\d+)\s*个顶点中露缝\s*(\d+)\s*个'),
    'terrainerr': re.compile(r'\[地形\]\s*★[^\n]*'),
    'abort':      re.compile(r'===\s*编排中止[^\n]*'),
    'anchorskip': re.compile(r'\[锚点\]\s*跳过：地形已按件底'),
    'reload':     re.compile(r'★\s*\[地形\]\s*重载复核：(\d+)\s*个顶点.*?max=([\d.]+|NaN)m[^\n]*'),
    'observe_end': re.compile(r'★\s*\[观测\]\s*结束：(\d+)s\s*内本工具件\s*(\d+)\s*→\s*(\d+)'),
    'observe_line': re.compile(r'\[观测\]\s*t=[^\n]*'),
    'doomed':     re.compile(r'★\s*\[承重预演\]\s*删掉插件后预计(?:一件不塌|会塌\s*(\d+)\s*件)[^\n]*'),
    'instready':  re.compile(r'实例化就绪|\[锚点\]\s*求解开始'),
    'round':      re.compile(r'\[锚点\]\s*第\s*(\d+)\s*轮.*?必死\s*=\s*(\d+).*?锚点\s*=\s*(\d+)'),
    'builtdone':  re.compile(r'★\s*落地完成：\s*(\d+)\s*件.*?校验失败\s*(\d+)\s*次'),
    'anchordone': re.compile(r'★\s*\[锚点\]\s*求解结束：累计位移\s*(-?[\d.]+)\s*m'),
    'recon':      re.compile(r'\[对账\]\s*命中\s*(\d+)\s*/\s*缺失补建\s*(\d+)'),
    'saved':      re.compile(r'\[保存\]\s*(存档写盘完成|已触发)'),
    'finish':     re.compile(r'===\s*编排完成'),
    'health':     re.compile(r'\[体检\].*?共\s*(\d+).*?支撑不足\s*(\d+)'),
}


# ---------------------------------------------------------------- 状态机
def state_load(work):
    p = os.path.join(work, 'state.json')
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return {'work': os.path.abspath(work), 'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'stages': {}}


def state_save(work, st):
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, 'state.json'), 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def stage(st, name, **kv):
    st['stages'][name] = dict(st['stages'].get(name) or {}, ts=time.strftime('%Y-%m-%d %H:%M:%S'), **kv)


def die(msg, code=2):
    print('✗ ' + msg)
    sys.exit(code)


def server_alive(server_dir=None):
    """探测 valheim_server 是否在跑。True/False/None=探测不了。
    给了 server_dir 就只看从该目录启动的进程——正式服与测试服同名同 exe，按进程名判会被
    另一台服误伤（正式服开着时测试服的 install/teardown 全被拒）；杀进程同理必须按路径。"""
    try:
        if os.name == 'nt':
            out = subprocess.run(['powershell', '-NoProfile', '-Command',
                                  "Get-CimInstance Win32_Process -Filter \"Name='valheim_server.exe'\" "
                                  "| ForEach-Object { $_.ExecutablePath }"],
                                 capture_output=True, text=True, timeout=30).stdout
            paths = [p.strip() for p in out.splitlines() if p.strip()]
        else:
            out = subprocess.run(['pgrep', '-a', 'valheim_server'],
                                 capture_output=True, text=True, timeout=10).stdout
            paths = [l.split(None, 1)[1] if ' ' in l else l for l in out.splitlines() if l.strip()]
        if server_dir is None:
            return bool(paths)
        root = os.path.normcase(os.path.abspath(server_dir))
        return any(os.path.normcase(os.path.abspath(p)).startswith(root + os.sep) for p in paths)
    except Exception:
        return None


def kill_server(server_dir):
    """只杀从 server_dir 启动的 valheim_server（正式服同名同 exe，绝不能按进程名杀）。"""
    root = os.path.normcase(os.path.abspath(server_dir))
    if os.name == 'nt':
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='valheim_server.exe'\" | Where-Object { $_.ExecutablePath -and "
              "[IO.Path]::GetFullPath($_.ExecutablePath).ToLower().StartsWith('%s') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
              % (root.lower() + os.sep).replace("'", "''"))
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, timeout=60)
    else:
        subprocess.run(['pkill', '-f', os.path.join(server_dir, 'valheim_server')], capture_output=True, timeout=30)


def require_server_down(why, server_dir=None):
    alive = server_alive(server_dir)
    if alive is True:
        die('%s 前必须停服（DLL 占用 + 写入竞争，铁律 1）。请先关闭 %s 的 valheim_server 再重试。'
            % (why, server_dir or '所有'))
    if alive is None:
        print('⚠ 探测不到进程列表，假定已停服（请自行确认）')


# ---------------------------------------------------------------- 1+2. preflight
def cmd_preflight(a, st):
    bp, src = bp_parse.load_any(a.bp)
    pieces = [p for p in bp['pieces']
              if (p['category'] in bp_parse.STRUCT_CATS if a.structure_only else True)]
    rep = bp_parse.analyze(bp, structure_only=a.structure_only)
    xs = [p['x'] for p in pieces] or [0.0]
    zs = [p['z'] for p in pieces] or [0.0]
    ops = [p for p in pieces if p['name'] in bp_parse.TERRAIN_OP_PIECES]
    manifest = {
        'source': src, 'format': bp['format'], 'name': bp['name'],
        'selected_only': bool(a.structure_only),
        'report': rep,
        # 包围盒中心按 pieces.txt 全集算（含原版地形件）—— 与执行器 BuildTask 同一口径，否则坐标整体错位
        'bbox_center': {'x': (min(xs) + max(xs)) / 2, 'z': (min(zs) + max(zs)) / 2},
        # 原版地形件执行即自毁、不留 ZDO → 不进对账期望集
        'pieces': [{'name': p['name'], 'hash': p.get('hash') or bp_parse.stable_hash(p['name']),
                    'x': p['x'], 'y': p['y'], 'z': p['z'], 'yaw': p['yaw'],
                    'category': p['category']} for p in pieces if p['name'] not in bp_parse.TERRAIN_OP_PIECES],
        'terrain_op_pieces': len(ops),
        'terrain': bp['terrain'],
    }
    with open(os.path.join(a.work, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.work, 'pieces.txt'), 'w', encoding='utf-8') as f:
        f.write(bp_parse.to_txt(bp, pieces))
    # 蓝图 #Terrain 段原样转交执行器（PlanBuild TerrainModEntry 格式）；没有也写空文件，覆盖服务器上的旧版残留
    with open(os.path.join(a.work, 'terrain.txt'), 'w', encoding='utf-8') as f:
        f.write('# 蓝图 #Terrain 段 %d 条（PlanBuild 格式 shape;x;y;z;radius;rotation;smooth;paint，蓝图坐标）\n'
                % len(bp['terrain']))
        for e in bp['terrain']:
            f.write('%s;%.6f;%.6f;%.6f;%.6f;%d;%.6f;%s\n' % (e['shape'], e['x'], e['y'], e['z'], e['radius'],
                                                            e['rotation'], e['smooth'], e['paint']))

    # 风险门（铁律 5/7）
    risks, level = [], 'ok'
    if not pieces:
        risks.append('解析出 0 件（格式未识别或空文件）—— 拒绝通过，不许 0/0 报 ok（issue #17）')
        level = 'high'
    if rep.get('underground'):
        risks.append('含地下结构 %d 件 → 落地需地形开挖 = 崩塌+delta 丢失双高危（铁律 7：默认劝退）' % rep['underground'])
        level = 'high'
    if rep.get('hash_miss'):
        # issue #3：hashlib.json 是「名字表」不是「有效性表」——查不到 ≠ hash 无效。
        # hash 由名字经 StableHash 直接算出，1.0 新构件/命名变体（如 wood_wall_log_4x0.5）
        # 常未收录进表但 hash 有效可落地（实测该件落地时「哈希与游戏不一致 0 个」）。
        # 有效性判据在运行时（ZNetScene.GetPrefab），离线不可判定 → 只作 warning，不阻断。
        risks.append('名字未收录 %d 件（hash 由名字直接算出，通常仍有效，不影响其余件落地）：%s —— '
                     '落地时若运行时日志报「哈希与游戏不一致 / prefab 未找到」再回补 prefab 名单'
                     % (rep['hash_miss'], rep.get('hash_missing_names')))
        level = 'medium' if level == 'ok' else level
    if rep.get('with_zdoData') or rep.get('with_info'):
        risks.append('info %d 件 / zdoData %d 件：件清单只存 name|hash|x|y|z|yaw，附加数据不随行'
                     % (rep.get('with_info', 0), rep.get('with_zdoData', 0)))
        level = 'medium' if level == 'ok' else level
    if rep.get('non_pure_yaw'):
        risks.append('非纯 yaw 旋转 %d 件（斜屋顶/斜梁）→ 落地后需目视确认' % rep['non_pure_yaw'])
    if rep.get('terrain_mods'):
        risks.append('蓝图含 %d 段 #Terrain → 地形段按 PlanBuild 放置语义回放（LevelTerrain + smooth + paint）'
                     % rep['terrain_mods'])
    if ops:
        risks.append('含 %d 个原版地形件（%s）→ 不建成 ZDO，地形段按原版 TerrainOp 执行'
                     % (len(ops), sorted({p['name'] for p in ops})))
    ok = level != 'high'
    stage(st, 'preflight', ok=ok, risk_level=level, risks=risks,
          pieces=len(manifest['pieces']), terrain_ops=len(ops), total=rep.get('total'), ground_py=rep.get('ground_layer_py'),
          bbox=rep.get('bbox'), hash_hit=rep.get('hash_hit'), hash_miss=rep.get('hash_miss'))
    print('✓ preflight：%d/%d 件（structure_only=%s）  GroundLayerPy=%.2f  风险=%s'
          % (len(pieces), rep.get('total'), a.structure_only, rep.get('ground_layer_py', 0), level))
    for r in risks:
        print('  [风险] ' + r)
    print('  产物: manifest.json / pieces.txt')
    return ok


# ---------------------------------------------------------------- 3. backup
def cmd_backup(a, st):
    if not os.path.isdir(a.world):
        die('--world 不是目录: %s' % a.world)
    files = os.listdir(a.world)
    if not any(f.endswith('.chunk') for f in files):
        die('世界目录里没有 .chunk（1.0 chunked 布局才支持；旧版单文件 .db 不在支持范围）')
    require_server_down('备份', a.server)          # 给 --server 只查该目录的进程（正式服同名，别误伤）
    dest = os.path.join(a.work, 'backup-' + time.strftime('%Y%m%d-%H%M%S'))
    if a.dry_run:
        print('[dry-run] copytree %s → %s' % (a.world, dest))
        stage(st, 'backup', ok=True, dry=True, dest=dest)
        return True
    shutil.copytree(a.world, dest)
    n = sum(len(fs) for _, _, fs in os.walk(dest))
    sz = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dest) for f in fs)
    if not any(f.endswith('.chunk') for f in os.listdir(dest)):
        die('备份产物校验失败（没有 .chunk），请检查磁盘空间')
    stage(st, 'backup', ok=True, dest=dest, files=n, bytes=sz)
    print('✓ backup：%d 个文件 %.1f MB → %s' % (n, sz / 1e6, dest))
    return True


# ---------------------------------------------------------------- 2.5 autosite（只读，不需先备份）
def cmd_autosite(a, st):
    world = a.world or st['stages'].get('backup', {}).get('dest')
    if not world or not os.path.isdir(world):
        die('找不到世界目录：--world（autosite 只读扫描，不需要先备份）')
    fw = fd = 32.0
    man = os.path.join(a.work, 'manifest.json')
    if os.path.isfile(man):                       # 有 manifest 就按蓝图占地定格子
        with open(man, encoding='utf-8') as f:
            m = json.load(f)
        xs = [q['x'] for q in m['pieces']] or [0]
        zs = [q['z'] for q in m['pieces']] or [0]
        fw, fd = bp_autosite.rotated_extent(max(xs) - min(xs), max(zs) - min(zs), a.rotation)
        fw, fd = max(32.0, fw), max(32.0, fd)
    tile_w, tile_d = fw + 2 * a.site_margin, fd + 2 * a.site_margin
    records = bp_reconcile.full_scan(world, bp_reconcile.load_hashlib())
    near = (a.near_x, a.near_z, a.near_r) if a.near_x is not None and a.near_z is not None else None
    protect = (a.protect_x, a.protect_z, a.protect_r) if a.protect_x is not None and a.protect_z is not None else None
    sites, rej = bp_autosite.find_sites(records, tile_w=tile_w, tile_d=tile_d,
                                        water_level=a.water_level,
                                        min_samples=a.min_samples,
                                        max_spread=a.max_spread, top=a.top,
                                        max_burial=a.max_burial, near=near, protect=protect)
    print('扫描 %d 条记录 | 格子 %.0f×%.0f m | 拒绝统计: %s' % (len(records), tile_w, tile_d, rej))
    if not sites:
        print('✗ 无合格落点：放宽 --max-spread / --min-samples，或换区域'
              '（样本稀疏 = 少人踩点，可能空旷但无法验平，需进游戏目视确认）')
        stage(st, 'autosite', ok=False, rejects=rej)
        return False
    print('%-12s %-12s %-10s %-6s %-8s %-8s %-5s %s' % ('site_x', 'site_z', 'PlatformY', '起伏', '埋深p95', '埋深max', '样本', '自然物(树/岩/采集)'))
    for q in sites:
        print('%-12.1f %-12.1f %-10.2f %-6.2f %-8.2f %-8.2f %-5d %d/%d/%d'
              % (q['site_x'], q['site_z'], q['platform_y'], q['spread'],
                 q['burial_p95'], q['burial_max'], q['samples'],
                 q['trees'], q['rocks'], q['pickables']))
    b = sites[0]
    stage(st, 'autosite', ok=True, site_x=b['site_x'], site_z=b['site_z'],
          platform_y=b['platform_y'], stats=b, rejects=rej, tile=[tile_w, tile_d])
    print('✓ autosite：推荐 (%.1f, %.1f) PlatformY=%.2f —— install 自动采用，--site-x/--site-z 可手动覆盖'
          % (b['site_x'], b['site_z'], b['platform_y']))
    return True

# ---------------------------------------------------------------- 4. install
def gen_cfg(p):
    """生成 BepInEx cfg（键名对齐 xibpbuilder_reference/Plugin.cs 的 Config.Bind）。"""
    L = []
    def sec(s, kv):
        L.append('[%s]' % s)
        for it in kv:
            if len(it) == 3:
                L.append('## %s' % it[2])
                L.append('%s = %s' % (it[0], it[1]))
            else:
                L.append('%s = %s' % (it[0], it[1]))
        L.append('')
    sec('Build', [('Enabled', 'true'), ('OriginX', p['site_x']), ('OriginZ', p['site_z']),
                  ('YOffset', '0', '锚点自动求解生效时保持 0；若日志出现「[锚点] 求解失败/自动求解未生效」，'
                                   '必须手填（0 = 贴地硬边界），issue #8'),
                  ('GroundLayerPy', p['ground_py']),
                  ('AutoDetectGroundLayer', 'true'), ('PerPieceGround', 'false'),
                  ('Force', 'false'), ('Reconcile', 'true' if p.get('reconcile', True) else 'false'),
                  ('BatchSize', '60'), ('FrameDelay', '10'), ('PieceFile', 'bp_pieces.txt'),
                  ('AutoPlatformY', 'true', '落地高度在游戏里实测（地面层件位置的地形中位数）；false = 用 [Terrain] PlatformY'),
                  ('Rotation', p.get('rotation', 0), '蓝图朝向（度，俯视顺时针，绕蓝图包围盒中心）：90 = 原来朝北的门改朝东')])
    sec('Cleanup', [('Enabled', 'true'), ('CleanNature', 'true'), ('WaitActivate', 'true'),
                    ('OnlyPersistent', 'false'),
                    ('ClearAllInArea', 'false', '只搬自然物；系统 ZDO（_TerrainCompiler 整格地形等）、玩家件、容器、墓碑永远不动'),
                    ('Center1X', p['site_x']), ('Center1Z', p['site_z']), ('Radius1', p['radius']),
                    ('ProtectX', p['protect_x']), ('ProtectZ', p['protect_z']), ('ProtectRadius', p.get('protect_r', 20))])
    sec('Terrain', [('Enabled', 'true' if p.get('terrain', True) else 'false',
                     'PlanBuild 式逐点整地：占地整平 + 过渡带 + 地窖 + 蓝图 #Terrain + 锄头件（作者地面采样点）+ 接地修补；保护圈内不改'),
                    ('DryRun', 'true' if p.get('terrain_dry') else 'false'),
                    ('PlatformY', p['platform_y'], 'AutoPlatformY=false 或测不到地形时才用（autosite 离线代理估计）'),
                    ('Embed', '0.1'), ('Skirt', p.get('skirt', 6), '过渡带最小宽度；按边界高差自动加宽到坡度 ≤ SkirtSlope'),
                    ('SkirtSlope', '35'), ('SkirtMax', '16'), ('Pad', '0.5'), ('Cap', '1'), ('LayerTol', '0.6'),
                    ('Close', '3', '贴地件之间 ≤ 2×Close 米的空隙整成同一块平台（玩家先整出整块房基）；0 = 关'),
                    ('Sink', '1', '比平台深 Sink 米以上的非地窖件（深桩/下层露台）不整平台不挖坑'),
                    ('MaxDelta', '8', '游戏硬限 ±8m：任一满权重顶点超限 → 整段放弃，一个顶点都不改'),
                    ('GroundFix', 'true', '承重预演（原版 UpdateSupport 跑到收敛）后，给删掉插件会塌的最底层件接地：埋住的挖出来、悬空的垫起来，最多 3 轮'),
                    ('GroundFixMax', '7.5', '接地修补单点最多挖 / 垫多少米（垫超过 Cap 只给包围盒高 ≥1.5m 的柱/墙/桩）'),
                    ('RestoreFile', 'bp_terrain_restore.txt' if p.get('terrain_restore') else '',
                     '整格地形还原（落地前做一次）：tools/tc_restore_plan.py 从旧存档算出；空 = 不做'),
                    ('Paint', 'Dirt'), ('EntryFile', 'bp_terrain.txt'), ('DumpFile', 'bp_terrain_dump.csv'),
                    ('VerifyOnReload', 'true')])
    sec('Activate', [('Enabled', 'true'), ('X', p['site_x']), ('Y', '0'), ('Z', p['site_z'])])
    dm = p.get('demolish') or [0, 0, 0]
    sec('Demolish', [('Enabled', 'true' if dm[2] > 0 else 'false', '落地前销毁圈内带标记（MarkKey）的旧件，不掉建材；有东西的箱子不拆'),
                     ('X', dm[0]), ('Z', dm[1]), ('Radius', dm[2])])
    sec('Anchor', [('AutoSolve', 'true'), ('Target', '200'), ('MaxShift', '2'), ('SinkStep', '0.05')])
    sec('Support', [('Enabled', 'true' if p.get('support', True) else 'false',
                     'issue #13：默认开，落地窗口里锁住本工具件的支撑。⚠ 锁着就测不到真实承重——'
                     '删掉插件后是否会塌，用 bp_pipeline.py observe（关锁 + 区域激活 + 观测件数时序）验'),
                    ('Diagnose', 'true'),
                    ('ObserveMinutes', p.get('observe_minutes', 0), '承重观测时长（分钟，0 = 不观测）'),
                    ('ObserveInterval', '10')])
    return '\n'.join(L)


def cmd_install(a, st):
    if not st['stages'].get('backup', {}).get('ok') and not a.i_have_a_backup:
        die('install 前没有 backup 记录（铁律 1：动档前必备份）。确有备份请加 --i-have-a-backup')
    for it in FOUR_PIECE:
        if not os.path.exists(os.path.join(a.bepinex_src, it)):
            die('--bepinex-src 里找不到 %s（应是 BepInExPack_Valheim 解包目录）' % it)
    if not (os.path.isfile(a.plugin_dll)):
        die('--plugin-dll 不存在: %s' % a.plugin_dll)
    require_server_down('install', a.server)
    if not os.path.isfile(os.path.join(a.work, 'terrain.txt')):
        die('work 目录缺 terrain.txt（旧版 preflight 产物），请重跑 preflight')
    auto = st['stages'].get('autosite') or {}
    site_x = a.site_x if a.site_x is not None else auto.get('site_x')
    site_z = a.site_z if a.site_z is not None else auto.get('site_z')
    platform_y = a.platform_y if a.platform_y is not None else auto.get('platform_y')
    if site_x is None or site_z is None or platform_y is None:
        die('缺落点参数：--site-x/--site-z/--platform-y，或先跑 autosite 自动选点')
    # 清理半径必须盖住整个占地 + 过渡带（旧默认 32m 盖不住 48×57m 的大宅：角上的树会留在屋里）
    radius = a.radius
    man_path = os.path.join(a.work, 'manifest.json')
    if os.path.isfile(man_path):
        with open(man_path, encoding='utf-8') as f:
            b = (json.load(f).get('report') or {}).get('bbox') or {}
        if b:
            half_diag = 0.5 * ((b['x'][1] - b['x'][0]) ** 2 + (b['z'][1] - b['z'][0]) ** 2) ** 0.5
            radius = max(radius, round(half_diag + a.skirt + 4, 1))
    params = {'site_x': site_x, 'site_z': site_z, 'platform_y': platform_y,
              'ground_py': a.ground_py if a.ground_py is not None
              else st['stages'].get('preflight', {}).get('ground_py', -1.6),
              'radius': radius, 'protect_x': a.protect_x, 'protect_z': a.protect_z, 'protect_r': a.protect_r,
              'rotation': a.rotation % 360.0,
              'terrain': not a.no_terrain, 'terrain_dry': a.terrain_dry_run, 'skirt': a.skirt,
              'demolish': [a.demolish_x, a.demolish_z, a.demolish_r] if a.demolish_r else None,
              'terrain_restore': bool(a.terrain_restore)}
    if a.terrain_restore and not os.path.isfile(a.terrain_restore):
        die('--terrain-restore 文件不存在: %s' % a.terrain_restore)
    if auto.get('site_x') == site_x and auto.get('site_z') == site_z:
        print('  落点取自 autosite：(%s, %s) PlatformY=%s' % (site_x, site_z, platform_y))
    bep = os.path.join(a.server, 'BepInEx')
    actions = [('dir', os.path.join(a.bepinex_src, 'BepInEx'), bep),
               ('file', os.path.join(a.bepinex_src, 'winhttp.dll'), os.path.join(a.server, 'winhttp.dll')),
               ('file', os.path.join(a.bepinex_src, 'doorstop_config.ini'), os.path.join(a.server, 'doorstop_config.ini')),
               ('dir', os.path.join(a.bepinex_src, 'doorstop_libs'), os.path.join(a.server, 'doorstop_libs')),
               ('file', os.path.abspath(a.plugin_dll), os.path.join(bep, 'plugins', 'XiBpBuilder.dll')),
               ('file', os.path.join(a.work, 'pieces.txt'), os.path.join(bep, 'config', 'bp_pieces.txt')),
               ('file', os.path.join(a.work, 'terrain.txt'), os.path.join(bep, 'config', 'bp_terrain.txt'))]
    if a.terrain_restore:
        actions.append(('file', os.path.abspath(a.terrain_restore), os.path.join(bep, 'config', 'bp_terrain_restore.txt')))
    if a.dry_run:
        for _, s, d in actions:
            print('[dry-run] %s → %s' % (s, d))
        print('[dry-run] 写 cfg → %s（GroundLayerPy=%.2f）' % (os.path.join(bep, 'config', a.cfg_guid + '.cfg'), params['ground_py']))
        stage(st, 'install', ok=True, dry=True, params=params, cfg_guid=a.cfg_guid)
        return True
    # 已有 BepInEx → 先隔离旧的（可回滚，不做不可逆删除）
    if os.path.exists(bep):
        old = os.path.join(a.work, 'quarantine-install-' + time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(old, exist_ok=True)
        for it in FOUR_PIECE:
            src = os.path.join(a.server, it)
            if os.path.exists(src):
                shutil.move(src, os.path.join(old, it))
        print('⚠ 服务器原有 BepInEx 痕迹已隔离到 %s' % old)
    for kind, s, d in actions:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        (shutil.copytree if kind == 'dir' else shutil.copy2)(s, d)
    with open(os.path.join(bep, 'config', a.cfg_guid + '.cfg'), 'w', encoding='utf-8') as f:
        f.write(gen_cfg(params))
    # 铁律 2：重跑 = 删 flag；本工具永不写 Force=true。
    # 运行时状态（首测 PlatformY）与地形 dump 属于上一轮落点，全新落地必须一起清，否则会沿用旧高度
    cfg_dir = os.path.join(bep, 'config')
    for fl in [f for f in os.listdir(cfg_dir) if f.endswith('.flag') or f in ('bp_runtime.txt', 'bp_terrain_dump.csv', 'bp_observe_lost.csv')]:
        os.remove(os.path.join(cfg_dir, fl))
        print('  已清残留: %s' % fl)
    stage(st, 'install', ok=True, params=params, cfg=a.cfg_guid + '.cfg')
    print('✓ install：四件套 + 插件 + bp_pieces.txt + bp_terrain.txt + cfg 就位（落地高度游戏内实测；地形 %s；清理半径 %.0fm；保护圈 r=%.0fm）'
          % ('逐点整地' if params['terrain'] and not params['terrain_dry'] else ('只出方案' if params['terrain'] else '不改'),
             params['radius'], params['protect_r']))
    return True


# ---------------------------------------------------------------- 5. run
def _tail(fp_store, path, out):
    """读 path 新增字节 → out(markers 更新用)。"""
    if not os.path.exists(path):
        return
    pos = fp_store.get(path, 0)
    sz = os.path.getsize(path)
    if sz <= pos:
        return
    with open(path, 'rb') as f:
        f.seek(pos)
        chunk = f.read(sz - pos)
    fp_store[path] = sz
    out.append(chunk.decode('utf-8', 'replace'))


def cmd_run(a, st):
    exp = st['stages'].get('preflight', {}).get('pieces')
    if not exp:
        die('run 前需要 preflight（state 里没有件数）')
    if not st['stages'].get('install', {}).get('ok'):
        die('run 前需要 install')
    bat = a.bat or os.path.join(a.server, 'start_headless_server.bat')
    if not os.path.isfile(bat):
        die('启动脚本不存在: %s（用 --bat 指定）' % bat)
    logs = [os.path.join(a.server, 'BepInEx', 'LogOutput.log'),
            os.path.join(a.server, 'BepInEx', 'config', 'bpbuild.log')]
    if a.dry_run:
        print('[dry-run] 启动 %s（cwd=%s），盯 %s' % (bat, a.server, ' + '.join(logs)))
        print('[dry-run] 判读标记：本进程拥有 / 实例化就绪≥98% / 锚点轮次 / 落地完成 / 对账 / 保存 / 编排完成')
        stage(st, 'run', ok=True, dry=True)
        return True

    print('▶ 启动服务器：%s' % bat)
    out_log = open(os.path.join(a.work, 'server_stdout.log'), 'wb')
    # 官方启动脚本写的是裸 `valheim_server`：环境里有 NoDefaultCurrentDirectoryInExePath（agent 宿主常见的安全默认）
    # 时 cmd 不在当前目录找 exe → 「不是内部或外部命令」。只对子进程去掉这一个变量
    env = {k: v for k, v in os.environ.items() if k.upper() != 'NODEFAULTCURRENTDIRECTORYINEXEPATH'}
    kw = dict(cwd=a.server, env=env, stdout=out_log, stderr=subprocess.STDOUT, shell=False)
    if os.name == 'nt':
        kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([bat], **kw)
    fp_store, buf, seen = {}, [], {'rounds': [], 'health': []}
    t0 = time.time()
    finish_at = None
    while True:
        if proc.poll() is not None:
            die('服务器提前退出（rc=%s），看 %s' % (proc.returncode, out_log.name))
        for lp in logs:
            _tail(fp_store, lp, buf)
        if buf:
            text = ''.join(buf)
            buf = []
            with open(os.path.join(a.work, 'bp_markers.log'), 'a', encoding='utf-8') as f:
                f.write(text)
            for k in ('owned', 'instready', 'builtdone', 'anchordone', 'recon', 'saved',
                      'platform', 'terrain', 'anchorskip', 'reload', 'abort', 'observe_end', 'doomed'):
                m = MARKERS[k].search(text)
                if m:
                    seen[k] = m.groups() if m.groups() else (m.group(0),)
            seen['rounds'] += MARKERS['round'].findall(text)
            seen['health'] += MARKERS['health'].findall(text)
            seen.setdefault('terrainerr', []).extend(MARKERS['terrainerr'].findall(text))
            for ln in MARKERS['observe_line'].findall(text):
                seen.setdefault('observe', []).append(ln)
                print('  ' + ln)                                # 承重观测时序实时打出来
            if 'abort' in seen and finish_at is None:
                finish_at = time.time() - a.observe          # 编排中止不会再有「编排完成」：立即停服，别等超时
                print('✗ 编排中止：%s' % seen['abort'][0])
            if MARKERS['finish'].search(text) and finish_at is None:
                finish_at = time.time()
                print('✓ 编排完成标记出现，进入观察窗（铁律 4：件数时序判稳）…')
        if finish_at is None and time.time() - t0 > a.timeout:
            proc.kill()
            die('超时 %ds 未等到「编排完成」标记，已强停。看 bp_markers.log 排查' % a.timeout)
        if finish_at is not None and time.time() - finish_at > a.observe:
            break
        time.sleep(1)
    # 优雅停服（存档已 DelayedSave，超时再强杀）
    print('■ 观察窗结束，停服…')
    try:
        if os.name == 'nt':
            proc.send_signal(subprocess.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(30)
    except Exception:
        proc.kill()
    out_log.close()
    # proc 只是外层 bat；valheim_server 是它的子进程，可能还在存盘。等它自己退，超时再按路径只杀本目录的进程
    for _ in range(60):
        if server_alive(a.server) is not True:
            break
        time.sleep(1)
    else:
        print('⚠ 服务器 60s 未退出，按 ExecutablePath 强停（只杀 %s 下的进程）' % a.server)
        kill_server(a.server)

    # 判读（收官报告 §6 合格标准表）
    owned = 1 if 'owned' in seen else 0
    built = int(seen.get('builtdone', ('0', '0'))[0]) if 'builtdone' in seen else 0
    fail = int(seen.get('builtdone', ('0', '0'))[1]) if 'builtdone' in seen else -1
    sink = float(seen.get('anchordone', ('0',))[0]) if 'anchordone' in seen else 0.0
    # [体检] 参考插件只打一次：单样本判不了时序，交人工（None），不许当成「不稳」
    stable = (len({h[0] for h in seen['health'][-4:]}) == 1) if len(seen['health']) >= 2 else None
    params = st['stages']['install'].get('params') or {}
    terrain_on = params.get('terrain') and not params.get('terrain_dry')
    cfg_dir = os.path.join(a.server, 'BepInEx', 'config')
    runtime = {}
    rt = os.path.join(cfg_dir, 'bp_runtime.txt')
    if os.path.isfile(rt):
        with open(rt, encoding='utf-8-sig') as f:
            runtime = dict(l.strip().split('=', 1) for l in f if '=' in l)
    terrain = None
    if 'terrain' in seen:
        n, p95, mx, fn, gp = seen['terrain']
        terrain = {'verts': int(n), 'err_p95': float(p95), 'err_max': float(mx), 'foot_verts': int(fn), 'gaps': int(gp)}
    for fn in ('bp_terrain_dump.csv', 'bp_observe_lost.csv'):   # 逐顶点地形 / 观测期塌损件明细
        if os.path.isfile(os.path.join(cfg_dir, fn)):
            shutil.copy2(os.path.join(cfg_dir, fn), os.path.join(a.work, fn))
    # 承重预演（落地时、磨损冻结中跑原版承重到收敛）：只是预测，真塌不塌以 observe 为准
    doomed = None
    if 'doomed' in seen:
        doomed = int(seen['doomed'][0] or 0)
        print('%s 承重预演：删掉插件后预计%s' % ('✓' if doomed == 0 else '⚠', '一件不塌' if doomed == 0
              else '会塌 %d 件（接地修补够不着的；observe 会真塌给你看）' % doomed))
    # 承重观测（支撑锁关 + 区域激活）：件数时序不减 = 删掉插件后也不会塌
    obs = None
    if 'observe_end' in seen:
        secs, n0, n1 = (int(v) for v in seen['observe_end'])
        obs = {'seconds': secs, 'start': n0, 'end': n1, 'lost': n0 - n1,
               'last': (seen.get('observe') or [None])[-1], 'series': seen.get('observe', [])}
        lost_csv = os.path.join(a.work, 'bp_observe_lost.csv')
        if n1 < n0 and os.path.isfile(lost_csv):
            obs['lost_by'] = lost_summary(lost_csv)
        stage(st, 'observe', ok=n1 >= n0, **obs)
        print('%s 承重观测 %ds：本工具件 %d → %d%s' % ('✓' if n1 >= n0 else '✗', secs, n0, n1,
              '（一件没塌）' if n1 >= n0 else '（塌/损 %d 件）' % (n0 - n1)))
        for row in obs.get('lost_by', [])[:12]:
            print('    塌 %-28s %-9s ×%-3d 蓝图 py %s' % (row['prefab'], row['mat'], row['n'], row['py']))
    if 'reload' in seen and 'builtdone' not in seen:
        # 复核 run（flag 都在、没新建件）：看地形持久化 +（有的话）承重观测，别拿全新落地的判据去判
        n, mx = seen['reload'][0], seen['reload'][1]
        ok = mx != 'NaN' and float(mx) <= 0.05
        stage(st, 'run_reload', ok=ok, verts=int(n), max_diff=None if mx == 'NaN' else float(mx),
              line=seen['reload'][-1] if len(seen['reload']) > 2 else None)
        print('%s 重载复核：%s 个顶点 |reload − 落地时| max=%sm → %s'
              % ('✓' if ok else '✗', n, mx, '持久化 ✓（地形在 zone 重载后原样还在）' if ok else '✗ 地形与落地当时不一致'))
        return ok and (obs is None or obs['lost'] <= 0)
    checks = {
        '落点可用(未中止)': 'abort' not in seen,
        '激活生效': owned > 0,
        '实例化≥98%': built >= exp * 0.98,
        'prefab校验失败=0': fail == 0,
        # 整过地的建筑不做整体位移（位移只会让件离开刚整好的地面）
        '锚点收敛/按地形跳过': 'anchordone' in seen or 'anchorskip' in seen,
        '地形验收(误差≤0.5m)': (terrain is not None) if terrain_on else None,
        # 露缝 = 占地顶点地表比该点最低件底低 >0.15m（地基露出来）；按顶点判，叠在梁上的地板不算
        '占地露缝≤0.5%': (terrain['gaps'] <= max(2, terrain['foot_verts'] * 0.005)) if terrain else None,
        '进程内对账': ('recon' in seen) if 'builtdone' not in seen else None,   # 只有对账补建路径才有
        '已触发保存': 'saved' in seen,
        '件数时序稳定': stable,   # None=样本不足，需人工看日志
        '承重观测：件数不减': (obs['lost'] <= 0) if obs else None,
    }
    ok = all(v for v in checks.values() if v is not None)
    stage(st, 'run', ok=ok, owned=owned, built=built, fail=fail, sink=sink,
          platform_y=float(runtime['PlatformY']) if 'PlatformY' in runtime else None,
          terrain=terrain, terrain_errors=seen.get('terrainerr', [])[:10],
          abort=seen.get('abort', [None])[0], predicted_collapse=doomed,
          rounds=seen['rounds'], health=seen['health'], checks={k: v for k, v in checks.items()})
    for k, v in checks.items():
        print('  %s %s' % ('✓' if v else ('—' if v is None else '✗'), k))
    for e in seen.get('terrainerr', [])[:5]:
        print('  [地形] %s' % e)
    print('✓ run：%s（built=%d/%d PlatformY=%s sink=%.2f%s）'
          % ('合格' if ok else '不合格', built, exp, runtime.get('PlatformY', '?'), sink,
             '' if not terrain else '，地形误差 max %.2fm、占地露缝 %d/%d' % (terrain['err_max'], terrain['gaps'], terrain['foot_verts'])))
    return ok


def lost_summary(path):
    """bp_observe_lost.csv（执行器观测结束写：prefab,mat,px,py,pz,wx,wy,wz）→ 按 prefab+材质聚合，数量降序"""
    groups = {}
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            g = groups.setdefault((r['prefab'], r.get('mat', '-')), [])
            g.append(float(r['py']))
    rows = [{'prefab': k[0], 'mat': k[1], 'n': len(v),
             'py': '%.1f' % v[0] if len(v) == 1 else '%.1f~%.1f' % (min(v), max(v))} for k, v in groups.items()]
    return sorted(rows, key=lambda r: -r['n'])


# ---------------------------------------------------------------- 5b. observe（承重验收）
def cmd_observe(a, st):
    """在已落地的建筑上验承重：关支撑锁 + 关对账补建 + 开观测，flag 全保留（不重建、不改地形），起服看件数时序。
    区域激活让服务器替玩家把工地实例化、跑原版承重 —— 锁一关，该塌的就会真塌（= 删掉插件后玩家走近时的样子）。"""
    p = dict(st['stages'].get('install', {}).get('params') or {})
    if not p:
        die('observe 前需要 install（state 里没有落点参数）')
    require_server_down('observe', a.server)
    p.update(support=False, reconcile=False, observe_minutes=a.observe_minutes)
    cfg = os.path.join(a.server, 'BepInEx', 'config', a.cfg_guid + '.cfg')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write(gen_cfg(p))
    print('✓ cfg 改为承重观测：支撑锁关、对账补建关、最长观测 %.0f 分钟（件数与承重色连续 30s 不变即提前结束；flag 全保留：不重建、不改地形）' % a.observe_minutes)
    a.timeout = max(a.timeout, int(a.observe_minutes * 60) + 600)
    return cmd_run(a, st)


# ---------------------------------------------------------------- 6. verify
def cmd_verify(a, st):
    man_path = os.path.join(a.work, 'manifest.json')
    if not os.path.isfile(man_path):
        die('run 前需要 preflight（没有 manifest.json）')
    world = a.world or st['stages'].get('backup', {}).get('dest')
    if not world or not os.path.isdir(world):
        die('找不到世界目录：--world 或先做 backup')
    p = st['stages'].get('install', {}).get('params')
    if not p:
        die('找不到 install 参数（先 install）')
    sink = a.sink if a.sink is not None else st['stages'].get('run', {}).get('sink', 0.0)
    # 验收前置（issue #5）：索引恒等式 sum(chunk 头计数) == 索引 total，不成立即拒绝
    ident = bp_reconcile.identity_check(world)
    if ident['ok'] is False:
        die('验收前置检查失败：索引 total=%s ≠ 各 chunk 头计数之和=%s —— 存档损坏或布局不匹配，'
            '拒绝出存活率' % (ident['index'], ident['chunk_sum']))
    if ident['ok'] is True:
        print('✓ 索引恒等式：各 chunk 计数之和 == 索引 total == %d（%d 个 zone）'
              % (ident['index'], ident['zones']))
    with open(man_path, encoding='utf-8') as f:
        manifest = json.load(f)
    # 落地高度以执行器游戏内实测为准（run 段从 bp_runtime.txt 读回）；autosite 的离线代理值只是初值
    platform_y = st['stages'].get('run', {}).get('platform_y')
    if platform_y is None:
        platform_y = p['platform_y']
    exp = bp_reconcile.transform_pieces(manifest, p['site_x'], p['site_z'],
                                        platform_y, p['ground_py'], 0.0, sink, rotation=p.get('rotation', 0.0))
    found, chunk_files = bp_reconcile.scan_chunks_for_hashes(world, {e['hash'] for e in exp})
    res = bp_reconcile.reconcile(exp, found, a.margin)
    res['transform'] = {'origin': [p['site_x'], p['site_z']], 'platform_y': platform_y,
                        'ground_py': p['ground_py'], 'sink': sink, 'rotation': p.get('rotation', 0.0)}
    res['pass'] = bp_reconcile.verdict(res, a.min_rate)
    with open(os.path.join(a.work, 'reconcile.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    stage(st, 'verify', ok=res['pass'], identity=ident, **{k: res[k] for k in
          ('expected', 'matched', 'missing', 'moved', 'rate', 'rate_moved', 'extra_in_bbox_total')})
    print('离线对账：期望 %d | 匹配 %d | 缺失 %d（其中挪位 %d）| 盒内多出 %d → 存活率 %.2f%%（算上挪位 %.2f%%）%s'
          % (res['expected'], res['matched'], res['missing'], res['moved'], res['extra_in_bbox_total'],
             res['rate'] * 100, res['rate_moved'] * 100, '✅' if res['pass'] else '❌'))
    if res['moved']:
        print('  挪位（件还在，被游戏贴地 / 滚动了：水平 ≤1m、高差 ≤3m）：%s'
              % ', '.join('%s×%d' % kv for kv in res['moved_by'].items()))
    for m in res['missing_list'][:10]:
        print('  [缺] %-32s ×%d' % (m['name'], m['count']))
    return res['pass']


# ---------------------------------------------------------------- 7. teardown
def cmd_teardown(a, st):
    if not st['stages'].get('verify', {}).get('ok') and not a.force:
        die('teardown 前需要 verify 合格（或 --force 明知故犯）')
    require_server_down('teardown', a.server)
    q = os.path.join(a.work, 'quarantine-' + time.strftime('%Y%m%d-%H%M%S'))
    if a.dry_run:
        print('[dry-run] 移走四件套 → %s' % q)
        stage(st, 'teardown', ok=True, dry=True, quarantine=q)
        return True
    os.makedirs(q, exist_ok=True)
    moved = []
    for it in FOUR_PIECE:
        src = os.path.join(a.server, it)
        if os.path.exists(src):
            shutil.move(src, os.path.join(q, it))
            moved.append(it)
    stage(st, 'teardown', ok=True, quarantine=q, moved=moved)
    print('✓ teardown：已隔离 %s（可回滚：把 %s 里的四项移回服务器目录即可）' % (moved, q))
    print('  服务器已恢复无 mod 状态；玩家侧全程零安装。')
    return True


# ---------------------------------------------------------------- 报告
def cmd_report(a, st):
    lines = ['# 蓝图落地报告', '', '- 生成时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'),
             '- 工作目录：`%s`' % st['work'], '']
    rows = [('段', '状态', '关键结果')]
    sep = ['|---|---|---|']
    for name in STAGES:
        s = st['stages'].get(name) or {}
        if not s:
            rows.append((name, '未执行', ''))
        elif name == 'report' or s.get('dry'):
            rows.append((name, 'dry-run', ''))
        elif name == 'preflight':
            rows.append((name, '✅' if s['ok'] else '❌', '%d 件，风险 %s' % (s.get('pieces', 0), s.get('risk_level'))))
        elif name == 'autosite':
            if s.get('ok'):
                q = s.get('stats') or {}
                rows.append((name, '✅', '落点 (%s, %s) 高 %s，起伏 %.2fm / %d 样本'
                             % (s.get('site_x'), s.get('site_z'), s.get('platform_y'),
                                q.get('spread', 0), q.get('samples', 0))))
            else:
                rows.append((name, '❌', '无合格落点：%s' % s.get('rejects')))
        elif name == 'backup':
            rows.append((name, '✅' if s['ok'] else '❌', '`%s`' % s.get('dest', '')))
        elif name == 'install':
            rows.append((name, '✅' if s['ok'] else '❌', '落点 (%s, %s) PlatformY=%s' % (s['params']['site_x'], s['params']['site_z'], s['params']['platform_y'])))
        elif name == 'run':
            rows.append((name, '✅' if s['ok'] else '❌', 'owned=%s built=%s/%s sink=%s' % (s.get('owned'), s.get('built'), st['stages'].get('preflight', {}).get('pieces'), s.get('sink'))))
        elif name == 'verify':
            ident = '，索引恒等式 ✓（total=%s）' % s['identity']['index'] \
                if (s.get('identity') or {}).get('ok') else ''
            rows.append((name, '✅' if s['ok'] else '❌',
                         '匹配 %s/%s（%.2f%%）%s' % (s.get('matched'), s.get('expected'),
                                                     (s.get('rate') or 0) * 100, ident)))
        elif name == 'teardown':
            rows.append((name, '✅' if s['ok'] else '❌', '隔离区 `%s`' % s.get('quarantine', '')))
        else:
            rows.append((name, '✅', ''))
    lines += ['| ' + ' | '.join(rows[0]) + ' |', '|' + '---|' * 3]
    lines += ['| ' + ' | '.join(r) + ' |' for r in rows[1:]]
    lines += ['']
    pf = st['stages'].get('preflight') or {}
    if pf.get('risks'):
        lines += ['## 风险与处理', ''] + ['- ' + r for r in pf['risks']] + ['']
    run = st['stages'].get('run') or {}
    if run.get('checks'):
        lines += ['## 日志判读（收官报告 §6 合格标准）', '']
        lines += ['| 判据 | 结果 |', '|---|---|']
        for k, v in run['checks'].items():
            lines.append('| %s | %s |' % (k, '✓' if v else ('—（无样本，人工复核）' if v is None else '✗')))
        lines.append('')
        if run.get('rounds'):
            lines += ['锚点轮次（必死/锚点）：' + ' → '.join('第%s轮 %s/%s' % r for r in run['rounds']) , '']
    v = st['stages'].get('verify') or {}
    if v:
        lines += ['## 离线对账（坑 C.2 双匹配）', '',
                  '- 期望 %s 件，匹配 %s，缺失 %s（其中挪位 %s：植被贴地 / 推车滚动，件还在），存活率 %.2f%%（算上挪位 %.2f%%）'
                  % (v.get('expected'), v.get('matched'), v.get('missing'), v.get('moved', 0), (v.get('rate') or 0) * 100,
                     (v.get('rate_moved') or v.get('rate') or 0) * 100),
                  '- 明细：`reconcile.json`', '']
    td = st['stages'].get('teardown') or {}
    if td.get('quarantine'):
        lines += ['## 回滚', '',
                  '- BepInEx 四件套已隔离：`%s`（移回服务器目录 = 恢复执行器）' % td['quarantine'],
                  '- 整世界回滚：停服后把 `%s` 整目录拷回存档位置' % st['stages'].get('backup', {}).get('dest', '(backup)'),
                  '- 复核作弊标记：`python tools/check_cheat.py`（devcommands 会锁成就，铁律）', '']
    lines += ['## 铁律遵守情况', '',
              '- 备份先行：%s' % ('✓' if st['stages'].get('backup', {}).get('ok') else '✗ 未备份'),
              '- Force=true：从未使用（本工具不提供该开关）',
              '- 对账双匹配：hash + (x,y,z)×4 量化（y 含在内）',
              '- 崩塌判据：件数时序（非「支撑不足=0」瞬时采样）', '']
    out = os.path.join(a.work, 'report.md')
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    stage(st, 'report', ok=True, path=out)
    print('✓ report → %s' % out)
    return True




# ---------------------------------------------------------------- 自检（issue #3 回归）
def selftest():
    """两案回归：
    A. 名字未收录（hash 合法、名字不在 hashlib.json）→ warning 级，不得出现「必失败」（issue #3）
    B. 地下结构 → 仍须 high 级劝退（铁律 7 不放松）
    """
    import tempfile
    tmp = tempfile.mkdtemp(prefix='bppipe_selftest_')

    def run_case(name, body):
        bp = os.path.join(tmp, name + '.blueprint')
        with open(bp, 'w', encoding='utf-8') as f:
            f.write('#Name:%s\n#Creator:selftest\n#Description:""\n#Category:Building\n#Pieces\n' % name)
            f.write(body)
        work = os.path.join(tmp, name + '_work')
        os.makedirs(work, exist_ok=True)
        a = argparse.Namespace(bp=bp, work=work, structure_only=False)
        st = state_load(work)
        cmd_preflight(a, st)
        return st['stages']['preflight']

    # A：wood_wall_log_4x0.5 = 真实存在但名字表未收录（issue #3 实证样本）
    sA = run_case('caseA', 'wood_floor;Building;0;0;0;0;0;0;1;"";1;1;1\n'
                           'wood_wall_log_4x0.5;Building;2;0;0;0;0;0;1;"";1;1;1\n')
    okA = (sA['risk_level'] != 'high' and sA['ok'] is True
           and any('未收录' in r for r in sA['risks'])
           and all('必失败' not in r for r in sA['risks']))
    print('A 名字未收录→warning : level=%s ok=%s → %s'
          % (sA['risk_level'], sA['ok'], 'PASS' if okA else 'FAIL'))

    # B：py=-1.5 < DEEP_PY=-1.2 → 地下结构，必须 high + 劝退
    sB = run_case('caseB', 'wood_floor;Building;0;-1.5;0;0;0;0;1;"";1;1;1\n')
    okB = (sB['risk_level'] == 'high' and sB['ok'] is False
           and any('地下结构' in r for r in sB['risks']))
    print('B 地下结构→high 劝退 : level=%s ok=%s → %s'
          % (sB['risk_level'], sB['ok'], 'PASS' if okB else 'FAIL'))

    # C：解析出 0 件 → 必须 high + 拒绝，绝不 0/0 报 ok（issue #17）
    sC = run_case('caseC', 'this is not a valid piece line\n')
    okC = (sC['risk_level'] == 'high' and sC['ok'] is False
           and any('0 件' in r for r in sC['risks']))
    print('C 0件→high 拒绝     : level=%s ok=%s → %s'
          % (sC['risk_level'], sC['ok'], 'PASS' if okC else 'FAIL'))

    # D：#Terrain 段 → terrain.txt（PlanBuild 原格式）；原版地形件留在 pieces.txt 交执行器、不进对账期望集；
    #    包围盒中心按全集算（含地形件，与执行器 BuildTask 同口径）
    bpD = os.path.join(tmp, 'caseD.blueprint')
    with open(bpD, 'w', encoding='utf-8') as f:
        f.write('#Name:caseD\n#Terrain\nsquare;1.5;-2;0;4;45;0.3;Paved\n#Pieces\n'
                'wood_floor;Building;0;0;0;0;0;0;1;"";1;1;1\n'
                'mud_road;Building;10;-0.2;0;0;0;0;1;"";1;1;1\n')
    workD = os.path.join(tmp, 'caseD_work')
    os.makedirs(workD, exist_ok=True)
    stD = state_load(workD)
    cmd_preflight(argparse.Namespace(bp=bpD, work=workD, structure_only=False), stD)
    with open(os.path.join(workD, 'manifest.json'), encoding='utf-8') as f:
        mD = json.load(f)
    tl = [l for l in open(os.path.join(workD, 'terrain.txt'), encoding='utf-8') if not l.startswith('#')]
    pl = [l for l in open(os.path.join(workD, 'pieces.txt'), encoding='utf-8') if not l.startswith('#')]
    okD = (len(tl) == 1 and tl[0].split(';')[0] == 'square' and tl[0].strip().endswith(';Paved')
           and int(tl[0].split(';')[5]) == 45
           and [p['name'] for p in mD['pieces']] == ['wood_floor'] and mD['terrain_op_pieces'] == 1
           and len(pl) == 2 and abs(mD['bbox_center']['x'] - 5.0) < 1e-9
           and stD['stages']['preflight']['pieces'] == 1)
    print('D #Terrain/地形件   : terrain.txt %d 行、期望集 %s、pieces.txt %d 行、中心 x=%.1f → %s'
          % (len(tl), [p['name'] for p in mD['pieces']], len(pl), mD['bbox_center']['x'], 'PASS' if okD else 'FAIL'))

    ok = okA and okB and okC and okD
    print('selftest:', '四案 %s' % ('全部通过 ✓' if ok else '存在失败 ✗'))
    return 0 if ok else 1

# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description='蓝图落地七段流水线编排器（一条命令从解析到验收）')
    ap.add_argument('cmd', nargs='?', choices=STAGES + ['all', 'observe'])
    ap.add_argument('--bp', help='蓝图文件（preflight/all）')
    ap.add_argument('--work', help='运行目录（state/manifest/备份/报告都放这）')
    ap.add_argument('--world', help='世界存档目录（含 .chunk/.chunks）')
    ap.add_argument('--server', help='dedicated server 安装目录')
    ap.add_argument('--bepinex-src', help='BepInExPack_Valheim 解包目录（四件套来源）')
    ap.add_argument('--plugin-dll', help='编译好的 XiBpBuilder.dll')
    ap.add_argument('--bat', help='服务器启动脚本（默认 <server>/start_headless_server.bat）')
    ap.add_argument('--cfg-guid', default=CFG_GUID, help='插件 BepInPlugin GUID（决定 cfg 文件名；'
                       '用真版插件时传其 GUID，否则 cfg 读不到，issue #9）')
    ap.add_argument('--site-x', type=float, help='落点 X')
    ap.add_argument('--site-z', type=float, help='落点 Z')
    ap.add_argument('--platform-y', type=float, help='落点地表高度（cfg PlatformY）')
    ap.add_argument('--ground-py', type=float, help='蓝图地面层 py（默认取 preflight 自动值）')
    ap.add_argument('--radius', type=float, default=32.0, help='清理半径（默认 32）')
    ap.add_argument('--protect-x', type=float, help='保护圈中心 X（默认=落点外 43m）')
    ap.add_argument('--protect-z', type=float, help='保护圈中心 Z')
    ap.add_argument('--protect-r', type=float, default=20.0,
                    help='保护圈半径（米，默认 20）：主宅 + 护城河要整个圈进去——清理与地形都不碰圈内')
    ap.add_argument('--no-terrain', action='store_true', help='不改地形（只放件；原地形起伏大时会有件悬空/埋土）')
    ap.add_argument('--terrain-dry-run', action='store_true', help='地形只算方案、出 dump，不写一个顶点')
    ap.add_argument('--skirt', type=float, default=6.0, help='占地外过渡带宽度（米，默认 6）：平滑接回原地形')
    ap.add_argument('--demolish-x', type=float, default=0.0, help='install：拆旧圈圆心 x（旧版执行器 / 上一次建的房子）')
    ap.add_argument('--demolish-z', type=float, default=0.0, help='install：拆旧圈圆心 z')
    ap.add_argument('--demolish-r', type=float, default=0.0, help='install：拆旧圈半径；0 = 不拆')
    ap.add_argument('--terrain-restore', help='install：整格地形还原文件（tc_restore_plan.py 生成）')
    ap.add_argument('--rotation', type=float, default=0.0,
                    help='蓝图朝向（度，俯视顺时针，绕包围盒中心；90 = 原来朝北的门改朝东）。autosite 按转后占地选格子')
    ap.add_argument('--site-margin', type=float, default=16.0, help='autosite 格子外扩（米，默认 16）')
    ap.add_argument('--near-x', type=float, help='autosite 只在这附近找（用户给的大致方位）')
    ap.add_argument('--near-z', type=float)
    ap.add_argument('--near-r', type=float, default=80.0, help='autosite 搜索半径（米，默认 80）')
    ap.add_argument('--min-samples', type=int, default=8, help='autosite 每格最少样本数')
    ap.add_argument('--max-spread', type=float, default=2.0, help='autosite 格内起伏上限（米）')
    ap.add_argument('--max-burial', type=float, default=1.0, help='autosite 埋深阈值（米，默认 1.0；实测 >1m 掉件率开始上行）')
    ap.add_argument('--water-level', type=float, default=30.0, help='海平面（默认 30）')
    ap.add_argument('--top', type=int, default=10, help='autosite 输出前 N 个候选')
    ap.add_argument('--sink', type=float, help='对账用整体位移（默认取 run 段日志解析值）')
    ap.add_argument('--min-rate', type=float, default=1.0, help='对账达标线（默认 1.0=零丢失）')
    ap.add_argument('--margin', type=float, default=15.0)
    ap.add_argument('--timeout', type=int, default=1800, help='run 段等「编排完成」的超时秒数')
    ap.add_argument('--observe', type=int, default=240, help='编排完成后的件数时序观察窗秒数')
    ap.add_argument('--observe-minutes', type=float, default=2.0,
                    help='observe 命令：插件内承重观测最长时长（分钟，默认 2；支撑锁关、区域激活，每 10s 记件数 + 承重色，连续 30s 不变提前结束）')
    ap.add_argument('--structure-only', action='store_true', help='只落地 Building 类件（过滤家具）')
    ap.add_argument('--i-have-a-backup', action='store_true', help='跳过备份检查（自知有备份时）')
    ap.add_argument('--allow-high-risk', action='store_true', help='地下结构也硬闯（默认劝退）')
    ap.add_argument('--force', action='store_true', help='teardown 不看 verify 结果')
    ap.add_argument('--dry-run', action='store_true', help='只打印将执行的动作')
    ap.add_argument('--selftest', action='store_true', help='跑内置回归自检（issue #3）')
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if not a.cmd:
        ap.error('cmd 必填（%s 或 --selftest）' % '/'.join(STAGES))
    if not a.work:
        ap.error('--work 必填（或 --selftest）')

    if a.cmd in ('install', 'all') and (a.protect_x is None or a.protect_z is None):
        # 旧默认「落点西 43m、r=20」是 usagi 那一例的主宅位置，换个落点就成了压在新房子上的幽灵保护圈
        # （48m 宽的大宅直接触发「占地落进保护圈」整段放弃）。没给就不设，并明说。
        a.protect_x, a.protect_z, a.protect_r = 0.0, 0.0, 0.0
        print('⚠ 未指定保护圈（--protect-x/--protect-z/--protect-r）：清理与整地不保护任何既有建筑。'
              '落点旁有主宅/护城河时务必圈进去')
    os.makedirs(a.work, exist_ok=True)
    st = state_load(a.work)

    if a.cmd == 'preflight':
        ok = cmd_preflight(a, st)
    elif a.cmd == 'backup':
        ok = cmd_backup(a, st)
    elif a.cmd == 'autosite':
        ok = cmd_autosite(a, st)
    elif a.cmd == 'install':
        ok = cmd_install(a, st)
    elif a.cmd == 'run':
        ok = cmd_run(a, st)
    elif a.cmd == 'verify':
        ok = cmd_verify(a, st)
    elif a.cmd == 'teardown':
        ok = cmd_teardown(a, st)
    elif a.cmd == 'observe':
        ok = cmd_observe(a, st)
    elif a.cmd == 'report':
        ok = cmd_report(a, st)
    else:  # all：逐段跑，失败的段重跑，已 ok 的跳过
        need = {'preflight': ['--bp'], 'autosite': ['--world'], 'backup': ['--world'],
                'install': ['--server', '--bepinex-src', '--plugin-dll'],
                'run': ['--server'], 'verify': [], 'teardown': ['--server'], 'report': []}
        for name in STAGES:
            if st['stages'].get(name, {}).get('ok') and not st['stages'].get(name, {}).get('dry'):
                print('· %s 已完成，跳过（删除 <work>/state.json 里该段可重跑）' % name)
                continue
            if name == 'autosite' and None not in (a.site_x, a.site_z, a.platform_y):
                print('· 已显式给落点，跳过 autosite')
                continue
            missing = [f for f in need[name] if getattr(a, f.lstrip('-').replace('-', '_')) is None]
            if missing:
                die('all 缺参数 %s（%s 段需要）' % (missing, name))
            print('\n===== %s =====' % name.upper())
            ok = globals()['cmd_' + name](a, st)
            if not ok:
                break
            if name == 'preflight' and st['stages']['preflight']['risk_level'] == 'high' and not a.allow_high_risk:
                die('蓝图含高危风险（见上），默认劝退。确要硬闯加 --allow-high-risk')
    state_save(a.work, st)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
