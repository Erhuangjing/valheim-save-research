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
    'owned':      re.compile(r'本进程拥有\s*=?\s*(\d+)'),
    'instready':  re.compile(r'实例化就绪|\[锚点\]\s*求解开始'),
    'round':      re.compile(r'\[锚点\]\s*第\s*(\d+)\s*轮.*?必死\s*=\s*(\d+).*?锚点\s*=\s*(\d+)'),
    'builtdone':  re.compile(r'★\s*落地完成：\s*(\d+)\s*件.*?校验失败\s*(\d+)\s*次'),
    'anchordone': re.compile(r'★\s*\[锚点\]\s*求解结束：累计位移\s*(-?[\d.]+)\s*m'),
    'recon':      re.compile(r'\[对账\]\s*命中\s*(\d+)\s*/\s*缺失补建\s*(\d+)'),
    'saved':      re.compile(r'\[保存\]\s*已触发'),
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


def server_alive():
    """探测 valheim_server 是否在跑。True/False/None=探测不了。"""
    try:
        if os.name == 'nt':
            out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq valheim_server.exe'],
                                 capture_output=True, text=True, timeout=20).stdout
            return 'valheim_server.exe' in out
        out = subprocess.run(['pgrep', '-fl', 'valheim_server'],
                             capture_output=True, text=True, timeout=10).stdout
        return bool(out.strip())
    except Exception:
        return None


def require_server_down(why):
    alive = server_alive()
    if alive is True:
        die('%s 前必须停服（DLL 占用 + 写入竞争，铁律 1）。请先关闭 valheim_server 再重试。' % why)
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
    manifest = {
        'source': src, 'format': bp['format'], 'name': bp['name'],
        'selected_only': bool(a.structure_only),
        'report': rep,
        'bbox_center': {'x': (min(xs) + max(xs)) / 2, 'z': (min(zs) + max(zs)) / 2},
        'pieces': [{'name': p['name'], 'hash': p.get('hash') or bp_parse.stable_hash(p['name']),
                    'x': p['x'], 'y': p['y'], 'z': p['z'], 'yaw': p['yaw'],
                    'category': p['category']} for p in pieces],
    }
    with open(os.path.join(a.work, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.work, 'pieces.txt'), 'w', encoding='utf-8') as f:
        f.write(bp_parse.to_txt(bp, pieces))

    # 风险门（铁律 5/7）
    risks, level = [], 'ok'
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
        risks.append('蓝图含 %d 段 #Terrain → [Terrain] 默认关，需要人工评估是否整平' % rep['terrain_mods'])
    ok = level != 'high'
    stage(st, 'preflight', ok=ok, risk_level=level, risks=risks,
          pieces=len(pieces), total=rep.get('total'), ground_py=rep.get('ground_layer_py'),
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
    require_server_down('备份')
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
        fw = max(32.0, max(xs) - min(xs))
        fd = max(32.0, max(zs) - min(zs))
    tile_w, tile_d = fw + 2 * a.site_margin, fd + 2 * a.site_margin
    records = bp_reconcile.full_scan(world, bp_reconcile.load_hashlib())
    sites, rej = bp_autosite.find_sites(records, tile_w=tile_w, tile_d=tile_d,
                                        water_level=a.water_level,
                                        min_samples=a.min_samples,
                                        max_spread=a.max_spread, top=a.top)
    print('扫描 %d 条记录 | 格子 %.0f×%.0f m | 拒绝统计: %s' % (len(records), tile_w, tile_d, rej))
    if not sites:
        print('✗ 无合格落点：放宽 --max-spread / --min-samples，或换区域'
              '（样本稀疏 = 少人踩点，可能空旷但无法验平，需进游戏目视确认）')
        stage(st, 'autosite', ok=False, rejects=rej)
        return False
    print('%-12s %-12s %-10s %-6s %-5s %s' % ('site_x', 'site_z', 'PlatformY', '起伏', '样本', '自然物(树/岩/采集)'))
    for q in sites:
        print('%-12.1f %-12.1f %-10.2f %-6.2f %-5d %d/%d/%d'
              % (q['site_x'], q['site_z'], q['platform_y'], q['spread'], q['samples'],
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
                  ('Force', 'false'), ('Reconcile', 'true'),
                  ('BatchSize', '60'), ('FrameDelay', '10'), ('PieceFile', 'bp_pieces.txt')])
    sec('Cleanup', [('Enabled', 'true'), ('CleanNature', 'true'), ('WaitActivate', 'true'),
                    ('OnlyPersistent', 'false'), ('ClearAllInArea', 'true'),
                    ('Center1X', p['site_x']), ('Center1Z', p['site_z']), ('Radius1', p['radius']),
                    ('ProtectX', p['protect_x']), ('ProtectZ', p['protect_z']), ('ProtectRadius', '20')])
    sec('Terrain', [('Enabled', 'false'), ('Carve', 'false'), ('DryRun', 'false'),
                    ('Embed', '0.3'), ('MaxDelta', '8'), ('Margin', '8'), ('PlatformY', p['platform_y'])])
    sec('Activate', [('Enabled', 'true'), ('X', p['site_x']), ('Y', '0'), ('Z', p['site_z'])])
    sec('Anchor', [('AutoSolve', 'true'), ('Target', '200'), ('MaxShift', '2'), ('SinkStep', '0.05')])
    sec('Support', [('Enabled', 'false')])
    return '\n'.join(L)


def cmd_install(a, st):
    if not st['stages'].get('backup', {}).get('ok') and not a.i_have_a_backup:
        die('install 前没有 backup 记录（铁律 1：动档前必备份）。确有备份请加 --i-have-a-backup')
    for it in FOUR_PIECE:
        if not os.path.exists(os.path.join(a.bepinex_src, it)):
            die('--bepinex-src 里找不到 %s（应是 BepInExPack_Valheim 解包目录）' % it)
    if not (os.path.isfile(a.plugin_dll)):
        die('--plugin-dll 不存在: %s' % a.plugin_dll)
    require_server_down('install')
    auto = st['stages'].get('autosite') or {}
    site_x = a.site_x if a.site_x is not None else auto.get('site_x')
    site_z = a.site_z if a.site_z is not None else auto.get('site_z')
    platform_y = a.platform_y if a.platform_y is not None else auto.get('platform_y')
    if site_x is None or site_z is None or platform_y is None:
        die('缺落点参数：--site-x/--site-z/--platform-y，或先跑 autosite 自动选点')
    params = {'site_x': site_x, 'site_z': site_z, 'platform_y': platform_y,
              'ground_py': a.ground_py if a.ground_py is not None
              else st['stages'].get('preflight', {}).get('ground_py', -1.6),
              'radius': a.radius, 'protect_x': a.protect_x, 'protect_z': a.protect_z}
    if auto.get('site_x') == site_x and auto.get('site_z') == site_z:
        print('  落点取自 autosite：(%s, %s) PlatformY=%s' % (site_x, site_z, platform_y))
    bep = os.path.join(a.server, 'BepInEx')
    actions = [('dir', os.path.join(a.bepinex_src, 'BepInEx'), bep),
               ('file', os.path.join(a.bepinex_src, 'winhttp.dll'), os.path.join(a.server, 'winhttp.dll')),
               ('file', os.path.join(a.bepinex_src, 'doorstop_config.ini'), os.path.join(a.server, 'doorstop_config.ini')),
               ('dir', os.path.join(a.bepinex_src, 'doorstop_libs'), os.path.join(a.server, 'doorstop_libs')),
               ('file', os.path.abspath(a.plugin_dll), os.path.join(bep, 'plugins', 'XiBpBuilder.dll')),
               ('file', os.path.join(a.work, 'pieces.txt'), os.path.join(bep, 'config', 'bp_pieces.txt'))]
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
    # 铁律 2：重跑 = 删 flag；本工具永不写 Force=true
    for fl in [f for f in os.listdir(os.path.join(bep, 'config')) if f.endswith('.flag')]:
        os.remove(os.path.join(bep, 'config', fl))
        print('  已清残留 flag: %s' % fl)
    stage(st, 'install', ok=True, params=params, cfg=a.cfg_guid + '.cfg')
    print('✓ install：四件套 + 插件 + bp_pieces.txt + cfg 就位（YOffset=0，Anchor 自动求解）')
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
    kw = dict(cwd=a.server, stdout=out_log, stderr=subprocess.STDOUT, shell=False)
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
            for k in ('owned', 'instready', 'builtdone', 'anchordone', 'recon', 'saved'):
                m = MARKERS[k].search(text)
                if m:
                    seen[k] = m.groups()
            seen['rounds'] += MARKERS['round'].findall(text)
            seen['health'] += MARKERS['health'].findall(text)
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

    # 判读（收官报告 §6 合格标准表）
    owned = int(seen.get('owned', ('0',))[0]) if 'owned' in seen else 0
    built = int(seen.get('builtdone', ('0', '0'))[0]) if 'builtdone' in seen else 0
    fail = int(seen.get('builtdone', ('0', '0'))[1]) if 'builtdone' in seen else -1
    sink = float(seen.get('anchordone', ('0',))[0]) if 'anchordone' in seen else 0.0
    stable = len(seen['health']) >= 2 and len({h[0] for h in seen['health'][-4:]}) == 1
    checks = {
        '激活生效(owned>0)': owned > 0,
        '实例化≥98%': built >= exp * 0.98,
        'prefab校验失败=0': fail == 0,
        '锚点收敛': 'anchordone' in seen,
        '进程内对账': 'recon' in seen,
        '已触发保存': 'saved' in seen,
        '件数时序稳定': stable if seen['health'] else None,   # None=无样本，需人工看日志
    }
    ok = all(v for v in checks.values() if v is not None)
    stage(st, 'run', ok=ok, owned=owned, built=built, fail=fail, sink=sink,
          rounds=seen['rounds'], health=seen['health'], checks={k: v for k, v in checks.items()})
    for k, v in checks.items():
        print('  %s %s' % ('✓' if v else ('—' if v is None else '✗'), k))
    print('✓ run：%s（owned=%d built=%d/%d sink=%.2f）' % ('合格' if ok else '不合格', owned, built, exp, sink))
    return ok


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
    exp = bp_reconcile.transform_pieces(manifest, p['site_x'], p['site_z'],
                                        p['platform_y'], p['ground_py'], 0.0, sink)
    found, chunk_files = bp_reconcile.scan_chunks_for_hashes(world, {e['hash'] for e in exp})
    res = bp_reconcile.reconcile(exp, found, a.margin)
    res['transform'] = {'origin': [p['site_x'], p['site_z']], 'platform_y': p['platform_y'],
                        'ground_py': p['ground_py'], 'sink': sink}
    res['pass'] = bp_reconcile.verdict(res, a.min_rate)
    with open(os.path.join(a.work, 'reconcile.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    stage(st, 'verify', ok=res['pass'], identity=ident, **{k: res[k] for k in
          ('expected', 'matched', 'missing', 'rate', 'extra_in_bbox_total')})
    print('离线对账：期望 %d | 匹配 %d | 缺失 %d | 盒内多出 %d → 存活率 %.2f%% %s'
          % (res['expected'], res['matched'], res['missing'], res['extra_in_bbox_total'],
             res['rate'] * 100, '✅' if res['pass'] else '❌'))
    for m in res['missing_list'][:10]:
        print('  [缺] %-32s ×%d' % (m['name'], m['count']))
    return res['pass']


# ---------------------------------------------------------------- 7. teardown
def cmd_teardown(a, st):
    if not st['stages'].get('verify', {}).get('ok') and not a.force:
        die('teardown 前需要 verify 合格（或 --force 明知故犯）')
    require_server_down('teardown')
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
                  '- 期望 %s 件，匹配 %s，缺失 %s，存活率 %.2f%%' % (v.get('expected'), v.get('matched'), v.get('missing'), (v.get('rate') or 0) * 100),
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

    ok = okA and okB
    print('selftest:', '两案 %s' % ('全部通过 ✓' if ok else '存在失败 ✗'))
    return 0 if ok else 1

# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description='蓝图落地七段流水线编排器（一条命令从解析到验收）')
    ap.add_argument('cmd', nargs='?', choices=STAGES + ['all'])
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
    ap.add_argument('--site-margin', type=float, default=16.0, help='autosite 格子外扩（米，默认 16）')
    ap.add_argument('--min-samples', type=int, default=8, help='autosite 每格最少样本数')
    ap.add_argument('--max-spread', type=float, default=2.0, help='autosite 格内起伏上限（米）')
    ap.add_argument('--water-level', type=float, default=30.0, help='海平面（默认 30）')
    ap.add_argument('--top', type=int, default=10, help='autosite 输出前 N 个候选')
    ap.add_argument('--sink', type=float, help='对账用整体位移（默认取 run 段日志解析值）')
    ap.add_argument('--min-rate', type=float, default=1.0, help='对账达标线（默认 1.0=零丢失）')
    ap.add_argument('--margin', type=float, default=15.0)
    ap.add_argument('--timeout', type=int, default=1800, help='run 段等「编排完成」的超时秒数')
    ap.add_argument('--observe', type=int, default=240, help='编排完成后的件数时序观察窗秒数')
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

    if a.cmd in ('install', 'all') and a.protect_x is None and a.site_x is not None:
        a.protect_x, a.protect_z = a.site_x - 43.0, a.site_z    # ◐DOC: 保护圈=落点外约 43m（usagi 案例）
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
