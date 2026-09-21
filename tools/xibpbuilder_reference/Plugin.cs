// ============================================================================
//  XiBpBuilder —— 参考重建（REFERENCE RECONSTRUCTION）
// ============================================================================
//  ⚠️ 免责声明（务必先读）：
//   本文件是从本仓库 4 份实验文档（交接文档 / 服务端激活报告 / 锚点收官报告 /
//   地形重塑说明）里散落的真实 patch 代码 **重建** 出来的机制骨架，
//   **不是** 原版 XiBpBuilder v0.31.1（约 1500 行，未包含在本仓库）。
//
//   编译状态：2026-09-21 实机验证通过（.NET SDK 9 + Valheim dedicated server 1.0
//   + BepInEx 5，0 警告 0 错误，产物 28,672 字节 —— 见 PR #1 实测报告 / issue #2）。
//   注意：编译通过只证明「直接调用的成员」存在；**反射字符串**（AccessTools 里的
//   私有字段名/方法名）编译期不检查，仍须运行时验证。
//
//   代码来源标注：
//     ✓DOC  = 文档里有逐字代码，此处忠实誊录
//     ◐DOC  = 文档描述了机制/公式，代码结构由我按描述补全
//     REF   = 纯推断的管道胶水（配置声明、时序常量、错误处理），最可能出错
//
//   目的：让「启动 dedicated server 时处理地形+清障+建房」的机制变得可执行、
//   可对照，填补可行性调研里点名的"执行器不在仓库"硬缺口。
// ============================================================================

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using BepInEx;
using BepInEx.Configuration;   // issue #2：ConfigEntry<> 在此命名空间，缺了 28×CS0246
using BepInEx.Logging;
using HarmonyLib;
using UnityEngine;

namespace XiBpBuilder
{
    // ⚠ issue #9：GUID 决定 cfg 路径（BepInEx/config/<GUID>.cfg）。本骨架 GUID 与原版不同
    //（脱敏改名），旧 cfg 不通用。复用原版调好的 cfg：改回原 GUID 重编译，或把 cfg 文件
    // 改名为本 GUID 并逐键核对。bp_pipeline.py install --cfg-guid <GUID> 可指定写哪份 cfg。
    [BepInPlugin("com.world.bpbuild", "XiBpBuilder", "0.31.1-ref")]   // REF: 版本号标 -ref 以示区分
    public class Plugin : BaseUnityPlugin
    {
        internal static Plugin instance;
        internal static ManualLogSource Log;
        internal static Harmony harmony;

        // ---- 全局状态 ----
        internal static bool s_worldLoaded = false;      // ◐DOC: ServerLoadWorld postfix 置位
        internal static bool s_freezeWear  = true;       // ◐DOC: 手术窗口，默认冻磨损
        internal static bool s_buildDone   = false;      // ✓DOC: 收官报告用它修诊断时序
        internal static bool s_cleanupDone = false;
        internal static readonly HashSet<long> s_ourZdoIds = new HashSet<long>();  // 我们创建的件

        // ====================================================================
        //  配置（REF: 按交接文档 §5.3 的 cfg 全清单声明，类型/默认值可能有出入）
        // ====================================================================
        // [Build]
        internal static ConfigEntry<bool>   CfgEnabled, CfgForce, CfgReconcile, CfgPerPieceGround, CfgAutoDetectGround;
        internal static ConfigEntry<float>  CfgOriginX, CfgOriginZ, CfgYOffset, CfgGroundLayerPy, CfgBatchSize, CfgFrameDelay;
        internal static ConfigEntry<string> CfgPieceFile;
        // [Cleanup]
        internal static ConfigEntry<bool>   CfgCleanOn, CfgCleanNature, CfgWaitActivate, CfgOnlyPersistent, CfgClearAllInArea;
        internal static ConfigEntry<float>  CfgC1X, CfgC1Z, CfgR1, CfgProtectX, CfgProtectZ, CfgProtectR;
        // [Terrain]
        internal static ConfigEntry<bool>   CfgTerrainOn, CfgCarve, CfgTerrainDryRun;
        internal static ConfigEntry<float>  CfgEmbed, CfgMaxDelta, CfgMargin, CfgPlatformY;
        // [Activate]
        internal static ConfigEntry<bool>   CfgActOn;
        internal static ConfigEntry<float>  CfgActX, CfgActY, CfgActZ;
        // [Anchor]
        internal static ConfigEntry<bool>   CfgAnchorAuto;
        internal static ConfigEntry<float>  CfgMaxShift, CfgSinkStep;
        internal static ConfigEntry<int>    CfgAnchorTarget;
        // [Support]
        internal static ConfigEntry<bool>   CfgSupportOn, CfgSupportDiag;
        internal static ConfigEntry<string> CfgMarkKey;

        private void Awake()
        {
            instance = this;
            Log = Logger;

            // ---- 配置绑定（REF: 默认值取交接文档 §5.3 / 收官报告 §5 配方）----
            CfgEnabled   = Config.Bind("Build", "Enabled", true);
            CfgOriginX   = Config.Bind("Build", "OriginX", -262f);
            CfgOriginZ   = Config.Bind("Build", "OriginZ", 270f);
            CfgYOffset   = Config.Bind("Build", "YOffset", 0f);          // ✓DOC: 保持 0，让 Anchor 自动求解
            CfgGroundLayerPy = Config.Bind("Build", "GroundLayerPy", -1.60f);
            CfgAutoDetectGround = Config.Bind("Build", "AutoDetectGroundLayer", true);
            CfgPerPieceGround = Config.Bind("Build", "PerPieceGround", false);
            CfgForce     = Config.Bind("Build", "Force", false);         // ✓DOC: 铁律，恒 false
            CfgReconcile = Config.Bind("Build", "Reconcile", true);
            CfgBatchSize = Config.Bind("Build", "BatchSize", 60f);
            CfgFrameDelay= Config.Bind("Build", "FrameDelay", 10f);
            CfgPieceFile = Config.Bind("Build", "PieceFile", "bp_pieces.txt");

            CfgCleanOn   = Config.Bind("Cleanup", "Enabled", true);
            CfgCleanNature = Config.Bind("Cleanup", "CleanNature", true);
            CfgWaitActivate= Config.Bind("Cleanup", "WaitActivate", true);   // ✓DOC 坑
            CfgOnlyPersistent = Config.Bind("Cleanup", "OnlyPersistent", false); // ✓DOC: 必须 false
            CfgClearAllInArea = Config.Bind("Cleanup", "ClearAllInArea", true);  // ✓DOC: 别信白名单
            CfgC1X = Config.Bind("Cleanup", "Center1X", -262f);
            CfgC1Z = Config.Bind("Cleanup", "Center1Z", 270f);
            CfgR1  = Config.Bind("Cleanup", "Radius1", 32f);
            CfgProtectX = Config.Bind("Cleanup", "ProtectX", -305f);
            CfgProtectZ = Config.Bind("Cleanup", "ProtectZ", 267f);
            CfgProtectR = Config.Bind("Cleanup", "ProtectRadius", 20f);

            CfgTerrainOn = Config.Bind("Terrain", "Enabled", false);   // ◐DOC: 默认关（首选天然平地，避 delta 丢失）
            CfgCarve     = Config.Bind("Terrain", "Carve", false);
            CfgTerrainDryRun = Config.Bind("Terrain", "DryRun", false);
            CfgEmbed     = Config.Bind("Terrain", "Embed", 0.30f);
            CfgMaxDelta  = Config.Bind("Terrain", "MaxDelta", 8f);      // ✓DOC: 游戏硬限，别改大
            CfgMargin    = Config.Bind("Terrain", "Margin", 8f);
            CfgPlatformY = Config.Bind("Terrain", "PlatformY", 39.40f);

            CfgActOn = Config.Bind("Activate", "Enabled", true);        // ✓DOC: 总开关
            CfgActX  = Config.Bind("Activate", "X", -262f);
            CfgActY  = Config.Bind("Activate", "Y", 0f);
            CfgActZ  = Config.Bind("Activate", "Z", 270f);

            CfgAnchorAuto = Config.Bind("Anchor", "AutoSolve", true);
            CfgAnchorTarget = Config.Bind("Anchor", "Target", 200);     // ✓DOC: 甜区（判据升级后仅作参考）
            CfgMaxShift = Config.Bind("Anchor", "MaxShift", 2f);        // ✓DOC: 双向 ±2m
            CfgSinkStep = Config.Bind("Anchor", "SinkStep", 0.05f);

            CfgSupportOn   = Config.Bind("Support", "Enabled", false);  // ✓DOC: 锚点成功则不需锁
            CfgSupportDiag = Config.Bind("Support", "Diagnose", true);
            CfgMarkKey     = Config.Bind("Support", "MarkKey", "xiabp");

            // ---- Harmony：逐个显式注册（✓DOC 坑 B：不是 CreateAndPatchAll，忘注册会静默失效）----
            harmony = new Harmony("com.world.bpbuild");
            harmony.PatchAll(typeof(PatchServerLoadWorld));
            harmony.PatchAll(typeof(PatchActivate));
            harmony.PatchAll(typeof(PatchFreezeWear));
            harmony.PatchAll(typeof(PatchSupportLock));
            Log.LogInfo("XiBpBuilder(ref) 已加载，patch 注册 4 个");
        }

        // ====================================================================
        //  Patch 1 —— 挂载点（✓DOC 坑 A：ServerLoadWorld，不是 LoadWorld）
        // ====================================================================
        [HarmonyPatch(typeof(ZNet), "ServerLoadWorld")]
        public static class PatchServerLoadWorld
        {
            static void Postfix()
            {
                if (!CfgEnabled.Value) return;
                if (!ZNet.instance || !ZNet.instance.IsServer()) return;
                s_worldLoaded = true;
                s_freezeWear  = true;          // 手术窗口默认开启
                Log.LogInfo("捕获 ZNet.ServerLoadWorld 完成 → 启动编排");
                instance.StartCoroutine(instance.Orchestrate());
            }
        }

        // ====================================================================
        //  Patch 2 —— Activate：总开关（✓DOC 逐字，激活报告 §1.2）
        // ====================================================================
        [HarmonyPatch(typeof(Game), "FixedUpdate")]
        public static class PatchActivate
        {
            static void Postfix()
            {
                if (!CfgActOn.Value || !s_worldLoaded) return;
                var znet = ZNet.instance;
                if (znet == null || !znet.IsServer()) return;
                if (ZNet.GetConnectionStatus() != ZNet.ConnectionStatus.Connected) return;
                znet.SetReferencePosition(new Vector3(CfgActX.Value, CfgActY.Value, CfgActZ.Value));
            }
        }

        // ====================================================================
        //  Patch 3 —— FreezeWear：手术窗口（◐DOC，usagi最终结论 §6 描述了机制）
        // ====================================================================
        [HarmonyPatch(typeof(WearNTear), "UpdateWear")]
        public static class PatchFreezeWear
        {
            // REF: UpdateWear 的签名是 (float time)，据 WearNTear.decompiled.cs
            static bool Prefix()
            {
                return !s_freezeWear;   // 冻结期间 return false → 跳过原版 UpdateWear，零销毁干扰
            }
        }

        // ====================================================================
        //  Patch 4 —— Support Lock：可选兜底（✓DOC 逐字，交接文档 §3.5）
        // ====================================================================
        [HarmonyPatch(typeof(WearNTear), "UpdateSupport")]
        public static class PatchSupportLock
        {
            // ◐DOC: RefSupport = FieldRefAccess<WearNTear,float>("m_support")
            static readonly AccessTools.FieldRef<WearNTear, float> RefSupport =
                AccessTools.FieldRefAccess<WearNTear, float>("m_support");   // REF: 字段名待核

            static bool Prefix(WearNTear __instance)
            {
                if (!CfgSupportOn.Value) return true;
                var nv = __instance.GetComponent<ZNetView>();
                if (nv == null || !nv.IsValid()) return true;
                if (nv.GetZDO().GetInt(CfgMarkKey.Value, 0) != 1) return true;  // 只锁我们的件
                float max = MaxOf(__instance);
                RefSupport(__instance) = max;
                nv.GetZDO().Set("support", max);
                return false;   // 跳过原版计算
            }

            // ◐DOC: GetMaxSupport 是方法，反射调用（usagi §6 阈值表用的就是它）
            static readonly MethodInfo GetMaxSupport = AccessTools.Method(typeof(WearNTear), "GetMaxSupport");
            static float MaxOf(WearNTear w) => Convert.ToSingle(GetMaxSupport.Invoke(w, null));
        }

        // ====================================================================
        //  主编排协程（◐DOC: 段序 = 收官报告 §1.4 "落地→等实例化→等清理→求解→位移→复测→解冻"）
        // ====================================================================
        private IEnumerator Orchestrate()
        {
            Log.LogInfo("=== 编排开始 ===");

            // 等世界真正连上
            while (ZNet.GetConnectionStatus() != ZNet.ConnectionStatus.Connected) yield return null;
            yield return new WaitForSeconds(2f);   // REF: 等 zone 系统起来

            var pieces = LoadPieces();
            Log.LogInfo($"件清单载入 {pieces.Count} 件 | 落点 ({CfgOriginX.Value},{CfgOriginZ.Value})");
            if (pieces.Count == 0) { Log.LogWarning("件清单为空，中止"); yield break; }

            // 段1 已冻磨损（s_freezeWear=true 默认）。段0 Activate 由 FixedUpdate postfix 持续生效。
            // 等激活覆盖生效 + zone 长齐
            yield return StartCoroutine(WaitActivateSettled());

            // 段2 Cleanup（清障）
            if (CfgCleanOn.Value && (!HasFlag("cleanup_done") || CfgForce.Value))
                yield return StartCoroutine(CleanupTask());

            // 段3 Terrain（处理地形，默认关；首选天然平地避 delta 丢失）
            if (CfgTerrainOn.Value && (!HasFlag("terrain_done") || CfgForce.Value))
                yield return StartCoroutine(TerrainTask(pieces));

            // 段4 Build（建房）
            if (!HasFlag("bp_done") || CfgForce.Value)
                yield return StartCoroutine(BuildTask(pieces));
            else if (CfgReconcile.Value)
                yield return StartCoroutine(ReconcileTask(pieces));   // 段6 对账补齐
            s_buildDone = true;

            // 段5 Anchor（手术窗口内求解 + 整体位移）
            if (CfgAnchorAuto.Value)
                yield return StartCoroutine(AnchorSolveTask());

            // 解冻 → 真实 UpdateSupport 跑起来
            s_freezeWear = false;
            Log.LogInfo("解冻磨损，进入真实模拟观察");

            // 段7 Save
            yield return new WaitForSeconds(3f);   // REF: 让实例化稳定
            TriggerSave();

            if (CfgSupportDiag.Value) SupportDiag();
            Log.LogInfo("=== 编排完成，可停服删 BepInEx ===");
        }

        // ---- 等激活稳定（◐DOC: 收官 §1.4 等件实例化 ~2s；清理需 WaitActivate +24s）----
        private IEnumerator WaitActivateSettled()
        {
            float t = 0;
            while (t < 24f) { yield return new WaitForSeconds(1f); t += 1f; }   // REF: 24s 让 zone 长齐
            Log.LogInfo($"[激活] refPos=({CfgActX.Value},{CfgActY.Value},{CfgActZ.Value}) 覆盖生效");
        }

        // ====================================================================
        //  段2 Cleanup（◐DOC: 搬走而非删除；坑 #6 / 收官 §3 三坑）
        // ====================================================================
        private IEnumerator CleanupTask()
        {
            Log.LogInfo("[清理] 开始");
            int moved = 0;
            for (int round = 0; round < 60; round++)   // ✓DOC: 轮次上限 60
            {
                int hitThisRound = 0;
                foreach (var zdo in EnumAllZDO())
                {
                    if (s_ourZdoIds.Contains(zdo.m_uid.GetHashCode())) continue;   // REF: 保护我们的件
                    Vector3 p = zdo.GetPosition();
                    if (InProtectCircle(p)) continue;                              // ✓DOC: 保护圈
                    bool inClean = Dist2D(p, CfgC1X.Value, CfgC1Z.Value) < CfgR1.Value;
                    if (!inClean) continue;
                    if (!CfgClearAllInArea.Value && !IsNatureOrRuin(zdo)) continue;
                    if (CfgOnlyPersistent.Value && !zdo.Persistent) continue;      // ✓DOC: 默认 false，别跳过自然物
                    zdo.SetPosition(new Vector3(6000f, -400f, 6000f));             // ✓DOC: 搬到世界角落
                    moved++; hitThisRound++;
                }
                if (hitThisRound == 0) break;
                yield return new WaitForSeconds(1f);   // REF: 等下一批 zone 生成
            }
            Log.LogInfo($"[清理] 搬走 {moved} 个对象");
            WriteFlag("cleanup_done");
            s_cleanupDone = true;
        }

        // ====================================================================
        //  段3 Terrain（◐DOC: 交接文档 §3.4 公式 + 地形重塑说明 §三 双写）
        // ====================================================================
        private IEnumerator TerrainTask(List<Piece> pieces)
        {
            Log.LogInfo("[地形] 开始（Mode=flat / Carve=" + CfgCarve.Value + "）");
            // 目标：把落点整平到 PlatformY（+ 可选按地下结构开挖）
            var hmaps = FindNearbyHeightmaps(CfgOriginX.Value, CfgOriginZ.Value, 93f);
            Log.LogInfo($"[地形] 落点 93m 内取用 {hmaps.Count} 个 Heightmap");
            int changed = 0;
            foreach (var hc in hmaps)
            {
                var hmap   = hc.heightmap;
                var tcomp  = hc.terrainComp;
                var hpos   = hc.worldPos;                       // heightmap 中心世界坐标
                int width  = GetWidth(hmap);                    // ◐DOC: m_width，反射
                float scale= GetScale(hmap);                    // ◐DOC: m_scale，反射
                float half = width * scale * 0.5f;
                var levelDelta  = GetFloatArray(tcomp, "m_levelDelta");    // ✓DOC 字段名
                var smoothDelta = GetFloatArray(tcomp, "m_smoothDelta");   // ✓DOC
                var modified    = GetBoolArray(tcomp, "m_modifiedHeight"); // ✓DOC

                for (int i = 0; i <= width; i++)
                for (int j = 0; j <= width; j++)
                {
                    // ✓DOC 顶点坐标公式（无 +0.5）
                    float wx = hpos.x - half + j * scale;
                    float wz = hpos.z - half + i * scale;
                    float targetY = TargetTerrainY(wx, wz, pieces);   // ◐DOC: flat→PlatformY；carve→按地下件
                    if (float.IsNaN(targetY)) continue;
                    int index = i * (width + 1) + j;

                    float curLocal = GetHeight(hmap, j, i);     // ✓DOC: 参数顺序 (x,z)
                    float tgtLocal = targetY - hpos.y;
                    // ✓DOC 增量公式
                    float req = levelDelta[index] + smoothDelta[index] + tgtLocal - curLocal;
                    levelDelta[index]  = Mathf.Clamp(req, -CfgMaxDelta.Value, CfgMaxDelta.Value);
                    smoothDelta[index] = 0f;
                    modified[index]    = true;
                    // ✓DOC(地形重塑§三): heights 也要写，否则"没生效"。本地高度 = targetY - hpos.y
                    SetHeight(hmap, j, i, tgtLocal);
                    changed++;
                }
                // ✓DOC 重建调用链（顺序不能乱）
                RebuildTerrain(hmap, tcomp);
                yield return null;
            }
            Log.LogInfo($"[地形] 改动 {changed} 个顶点（DryRun={CfgTerrainDryRun.Value}）");
            if (!CfgTerrainDryRun.Value) WriteFlag("terrain_done");
        }

        // ====================================================================
        //  段4 Build（✓DOC: 交接文档坑 #2 创建时序）
        // ====================================================================
        private IEnumerator BuildTask(List<Piece> pieces)
        {
            Log.LogInfo("[落地] 开始");
            float cx = pieces.Average(p => p.x), cz = pieces.Average(p => p.z);   // ◐DOC: 蓝图中心
            // 更严谨用包围盒中心：(min+max)/2，与 land_official.py 一致
            float minX = pieces.Min(p=>p.x), maxX = pieces.Max(p=>p.x);
            float minZ = pieces.Min(p=>p.z), maxZ = pieces.Max(p=>p.z);
            cx = (minX+maxX)/2f; cz = (minZ+maxZ)/2f;
            float groundBase = CfgPlatformY.Value - CfgGroundLayerPy.Value;   // ◐DOC: H_base 反推

            int batch = 0, created = 0, fail = 0;
            foreach (var pc in pieces.OrderBy(p => p.y))   // ✓DOC: 按 py 低→高，先地基后上层
            {
                Vector3 world = new Vector3(
                    CfgOriginX.Value + (pc.x - cx),
                    groundBase + pc.y + CfgYOffset.Value,
                    CfgOriginZ.Value + (pc.z - cz));
                if (CreatePiece(pc, world)) { created++; } else { fail++; }

                if (++batch % (int)CfgBatchSize.Value == 0)
                {
                    Log.LogInfo($"[落地] 累计 {created}/{pieces.Count}");
                    for (int f = 0; f < (int)CfgFrameDelay.Value; f++) yield return null;  // ✓DOC: FrameDelay
                }
            }
            Log.LogInfo($"★ 落地完成：{created} 件，prefab 校验失败 {fail} 次");
            WriteFlag("bp_done");
        }

        private bool CreatePiece(Piece pc, Vector3 world)
        {
            // ✓DOC: 权威哈希优先用游戏 API；文件哈希兜底
            int hash = pc.hash;
            var prefab = ZNetScene.instance.GetPrefab(hash);
            if (prefab == null) { Log.LogWarning($"[落地] prefab 未找到 hash={hash} name={pc.name}"); return false; }

            var zdo = ZDOMan.instance.CreateNewZDO(world, hash);   // ✓DOC
            if (zdo == null) return false;
            zdo.SetPrefab(hash);                 // ✓DOC 坑 #2：不显式调 → hash 全 0 → 不可见
            zdo.SetPosition(world);              // ✓DOC
            zdo.SetRotation(pc.rot);             // ✓DOC: 全四元数（不要只传 yaw）
            zdo.Persistent = true;               // ✓DOC: flags bit8
            zdo.SetOwner(0L);                    // ✓DOC: 无主，等 Activate 认领
            zdo.Set(CfgMarkKey.Value, 1);        // ◐DOC: 打标，供 Support Lock / 保护圈识别

            if (zdo.GetPrefab() != hash) { Log.LogError($"[落地] 校验失败 {pc.name}"); return false; }  // ✓DOC 当场校验
            s_ourZdoIds.Add(zdo.m_uid.GetHashCode());   // REF
            return true;
        }

        // ====================================================================
        //  段5 Anchor（◐DOC: 收官报告 §1.2 d_i + §8.3 必死件判据 + §1.3 闭环 3 轮）
        // ====================================================================
        private IEnumerator AnchorSolveTask()
        {
            Log.LogInfo("[锚点] 求解开始（手术窗口内，磨损已冻）");
            // issue #8：失败必须可见。原版实现异常被吞、流程照走，表面「很成功」实则自动求解
            // 没生效（实测靠 cfg 手填 YOffset 兜底）。现改为：异常显式报错 + 结尾明确
            // 「自动求解未生效，已回退 cfg 手填 YOffset」，不让使用者误删手填参数。
            float totalShift = 0f;
            bool failed = false;
            for (int round = 0; round < 3; round++)     // ✓DOC: 最多 3 轮闭环
            {
                List<WearNTear> our = null;
                var ds = new List<float>();
                int mustDie = 0, anchor0 = 0, noCol = 0;
                float bestShift = 0f;
                try
                {
                    our = CollectOurPieces();           // 每轮重收：位移/重实例化后旧实例会失效
                    if (our.Count == 0) { Log.LogWarning("[锚点] 拿不到实例（mark 丢失或未实例化）"); failed = true; break; }
                    ds = MeasureRound(our, out mustDie, out anchor0, out noCol);   // 逐件求 d_i
                    // ✓DOC §8.3/§9.3 判据：选必死件最少的档，并列取更深
                    bestShift = PickShiftByMustDie(our);    // ◐DOC: 扫 [-MaxShift,+MaxShift]，step SinkStep
                }
                catch (Exception e)
                {
                    Log.LogError($"[锚点] 第{round+1}轮测量异常：{e}");
                    failed = true;
                    break;
                }
                ds.Sort();
                Log.LogInfo($"[锚点] 第{round+1}轮 必死={mustDie} 锚点={anchor0} 无碰撞体跳过={noCol}");
                if (Mathf.Approximately(bestShift, 0f) && mustDie == 0 && anchor0 >= CfgAnchorTarget.Value)
                {
                    Log.LogInfo("[锚点] 已达标，停止");
                    break;
                }
                try { ApplyShift(our, bestShift); }    // ◐DOC: 整体位移（改 ZDO 位置）
                catch (Exception e) { Log.LogError($"[锚点] 位移异常：{e}"); failed = true; break; }
                totalShift += bestShift;
                yield return new WaitForSeconds(0.5f); // REF: 等物理/几何更新（yield 必须在 try 外：迭代器语法限制）
            }
            if (failed)
                Log.LogWarning($"[锚点] ★ 自动求解未生效 —— YOffset 沿用 cfg 手填值 {CfgYOffset.Value}；"
                    + "若为 0 则整栋按贴地硬边界落地，请按实测手调（issue #8）");
            else
                Log.LogInfo($"★ [锚点] 求解结束：累计位移 {totalShift:F2}m");
        }

        // issue #8：测量段拆成无 yield 的普通方法（迭代器的 try/catch 内不许 yield），
        // 并对「无 Collider / 已销毁」的件免疫——这正是实测 NRE 的元凶：
        // CollectOurPieces 只碰 ZNetView 能活着返回，一进测量循环 GetComponent<Collider>().bounds 就炸。
        private List<float> MeasureRound(List<WearNTear> our, out int mustDie, out int anchor0, out int noCollider)
        {
            var ds = new List<float>();
            mustDie = anchor0 = noCollider = 0;
            foreach (var w in our)
            {
                if (w == null) { noCollider++; continue; }          // Unity 已销毁（== 重载判真）
                var col = w.GetComponent<Collider>();
                if (col == null) { noCollider++; continue; }        // 碰撞体在子物体/缺失 → 原版在此 NRE
                var b = col.bounds;
                float di = MeasureSinkToTerrain(b);        // ◐DOC: OverlapBox 下移命中 terrain
                if (di >= 0) ds.Add(di);
                if (IsMustDie(b)) mustDie++;               // ✓DOC: 盒内三者皆无 = 必死
                if (di <= 0.001f) anchor0++;               // 当前已接地
            }
            return ds;
        }

        // ✓DOC: OverlapBox(中心下移 d, size/2 + 0.15) 命中 terrain 层
        private float MeasureSinkToTerrain(Bounds b)
        {
            int terrainMask = LayerMask.GetMask("Default", "static_solid", "Default_small");  // REF: 层名待核
            Vector3 half = b.size / 2f + Vector3.one * 0.15f;   // ✓DOC: 那个 0.15 = WearNTear 余量
            for (float d = 0f; d <= CfgMaxShift.Value; d += CfgSinkStep.Value)
            {
                Vector3 c = b.center - Vector3.up * d;
                if (Physics.OverlapBox(c, half, Quaternion.identity, terrainMask).Length > 0)
                    return d;
            }
            return -1f;   // 够不到地形
        }

        // ✓DOC §8.3: 必死件 = 盒内既无地形、又无别的构件、又无其它物体
        private bool IsMustDie(Bounds b)
        {
            int mask = LayerMask.GetMask("Default", "static_solid", "Default_small", "piece");  // REF
            Vector3 half = b.size / 2f + Vector3.one * 0.15f;
            return Physics.OverlapBox(b.center, half, Quaternion.identity, mask).Length == 0;
        }

        // ====================================================================
        //  段6 Reconcile（✓DOC: 交接文档 §9.4）
        // ====================================================================
        private IEnumerator ReconcileTask(List<Piece> pieces)
        {
            Log.LogInfo("[对账] 开始");
            var have = new HashSet<string>();
            var dict = AccessTools.Field(typeof(ZDOMan), "m_objectsByID")     // ✓DOC 字段名
                             .GetValue(ZDOMan.instance) as IDictionary;
            if (dict == null) { Log.LogWarning("[对账] 拿不到 m_objectsByID，主动放弃（宁缺勿重）"); yield break; }  // ✓DOC
            foreach (DictionaryEntry e in dict)
            {
                var z = (ZDO)e.Value; int h = z.GetPrefab(); Vector3 p = z.GetPosition();
                if (Dist2D(p, CfgOriginX.Value, CfgOriginZ.Value) > 45f) continue;   // ✓DOC 45m
                have.Add(Key(h, p));   // ✓DOC: hash|round(x*4)|round(y*4)|round(z*4)
            }
            float cx = (pieces.Min(p=>p.x)+pieces.Max(p=>p.x))/2f;
            float cz = (pieces.Min(p=>p.z)+pieces.Max(p=>p.z))/2f;
            float groundBase = CfgPlatformY.Value - CfgGroundLayerPy.Value;
            int miss = 0;
            foreach (var pc in pieces.OrderBy(p=>p.y))   // ✓DOC: 低→高补建
            {
                Vector3 world = new Vector3(CfgOriginX.Value+(pc.x-cx), groundBase+pc.y+CfgYOffset.Value, CfgOriginZ.Value+(pc.z-cz));
                if (!have.Contains(Key(pc.hash, world))) { CreatePiece(pc, world); miss++; }
            }
            Log.LogInfo($"[对账] 命中 {have.Count} / 缺失补建 {miss}");
        }
        private static string Key(int h, Vector3 p) =>   // ✓DOC: 0.25m 量化，y 也要
            $"{h}|{Mathf.RoundToInt(p.x*4)}|{Mathf.RoundToInt(p.y*4)}|{Mathf.RoundToInt(p.z*4)}";

        // ====================================================================
        //  段7 Save（✓DOC: 反射 DelayedSave(true)；SaveWorldAndPlayerProfiles 会 NRE）
        // ====================================================================
        private void TriggerSave()
        {
            var m = AccessTools.Method(typeof(ZNet), "DelayedSave");   // REF: 返回协程
            if (m == null) { Log.LogWarning("[保存] 找不到 DelayedSave"); return; }
            var coro = m.Invoke(ZNet.instance, new object[] { true }) as IEnumerator;
            if (coro != null) { StartCoroutine(coro); Log.LogInfo("[保存] 已触发 DelayedSave(true)"); }
        }

        private void SupportDiag()
        {
            // ◐DOC §9.3: 遍历 WearNTear，读 m_support vs GetMinSupport，报告 starved 数
            var getMin = AccessTools.Method(typeof(WearNTear), "GetMinSupport");
            var refSup = AccessTools.FieldRefAccess<WearNTear, float>("m_support");
            int starved = 0, total = 0;
            foreach (var w in WearNTear.GetAllInstances())
            {
                total++;
                float sup = refSup(w), min = Convert.ToSingle(getMin.Invoke(w, null));
                if (sup < min - 0.001f) starved++;
            }
            Log.LogInfo($"[体检] WearNTear 共 {total}，支撑不足 {starved}（⚠️瞬时采样会漏，判崩塌看件数时序）");
        }

        // ====================================================================
        //  件清单加载（◐DOC: bp_pieces.txt = name|hash|x|y|z|yaw；建议升级全四元数）
        // ====================================================================
        private List<Piece> LoadPieces()
        {
            var list = new List<Piece>();
            string path = Path.Combine(Paths.ConfigPath, CfgPieceFile.Value);   // REF: BepInEx/config
            if (!File.Exists(path)) { Log.LogError($"件清单不存在: {path}"); return list; }
            int line = 0;
            foreach (var raw in File.ReadAllLines(path))
            {
                line++;
                var s = raw.Trim();
                if (s.Length == 0 || s.StartsWith("#")) continue;
                var f = s.Split('|');
                if (f.Length < 6) continue;
                Piece pc;
                try
                {
                    pc = new Piece {
                        name = f[0],
                        // issue #7：prefab hash 是 uint32——真实蓝图里 60.8% 的件超过 int32 上限，
                        // int.Parse 直接 OverflowException（第 0 步就崩）。Valheim 的 prefab API
                        // （GetPrefab/SetPrefab）用 int 存 uint32 位模式 → unchecked 保位转换。
                        hash = unchecked((int)uint.Parse(f[1], CultureInfo.InvariantCulture)),
                        x = float.Parse(f[2], CultureInfo.InvariantCulture),
                        y = float.Parse(f[3], CultureInfo.InvariantCulture),
                        z = float.Parse(f[4], CultureInfo.InvariantCulture),
                    };
                    float yaw = float.Parse(f[5], CultureInfo.InvariantCulture);
                    pc.rot = Quaternion.Euler(0f, yaw, 0f);   // REF: txt 只有 yaw；升级请改吃 bp_parse.py --json 的四元数
                }
                catch (Exception e)
                {
                    Log.LogError($"[清单] 第 {line} 行解析失败，跳过: {raw} ({e.Message})");
                    continue;
                }
                list.Add(pc);
            }
            return list;
        }

        // ====================================================================
        //  反射/工具助手（REF: 全部需对 assembly_valheim.dll 核对签名）
        // ====================================================================
        private class Piece { public string name; public int hash; public float x,y,z; public Quaternion rot; }

        private static IEnumerable<ZDO> EnumAllZDO()   // REF: 遍历 ZDOMan 全部 ZDO
        {
            var dict = AccessTools.Field(typeof(ZDOMan), "m_objectsByID").GetValue(ZDOMan.instance) as IDictionary;
            if (dict == null) yield break;
            foreach (DictionaryEntry e in dict) yield return (ZDO)e.Value;
        }

        private List<WearNTear> CollectOurPieces()   // ◐DOC: 落点 45m 内、带 mark 的实例
        {
            var outList = new List<WearNTear>();
            foreach (var w in WearNTear.GetAllInstances())
            {
                var nv = w.GetComponent<ZNetView>();
                if (nv == null || !nv.IsValid()) continue;
                if (nv.GetZDO().GetInt(CfgMarkKey.Value, 0) != 1) continue;
                outList.Add(w);
            }
            return outList;
        }

        private bool InProtectCircle(Vector3 p) =>
            Dist2D(p, CfgProtectX.Value, CfgProtectZ.Value) < CfgProtectR.Value;
        private static float Dist2D(Vector3 p, float x, float z) =>
            Mathf.Sqrt((p.x-x)*(p.x-x) + (p.z-z)*(p.z-z));
        private bool IsNatureOrRuin(ZDO z) { /* REF: 名字/prefab 判定，ClearAllInArea=true 时不走这条 */ return true; }

        // ---- Terrain 反射助手（REF: 字段/方法名据交接文档 §3.4，签名待核）----
        private struct HC { public object heightmap; public object terrainComp; public Vector3 worldPos; }
        private List<HC> FindNearbyHeightmaps(float x, float z, float r) { /* REF: Heightmap.GetAllInstance + 距离筛 */ return new List<HC>(); }
        private float TargetTerrainY(float wx, float wz, List<Piece> ps) { /* ◐DOC: flat→PlatformY；carve→地下件最低底面 */ return CfgPlatformY.Value; }
        private static int   GetWidth(object hmap)  => (int)AccessTools.Field(hmap.GetType(), "m_width").GetValue(hmap);
        private static float GetScale(object hmap)  => (float)AccessTools.Field(hmap.GetType(), "m_scale").GetValue(hmap);
        private static float[] GetFloatArray(object t, string f) => (float[])AccessTools.Field(t.GetType(), f).GetValue(t);
        private static bool[]  GetBoolArray (object t, string f) => (bool[]) AccessTools.Field(t.GetType(), f).GetValue(t);
        private static float GetHeight(object hmap, int x, int z) =>
            Convert.ToSingle(AccessTools.Method(hmap.GetType(), "GetHeight").Invoke(hmap, new object[]{ x, z }));
        private static void  SetHeight(object hmap, int x, int z, float h) { /* REF: m_heights[index]=h */ }
        private void RebuildTerrain(object hmap, object tcomp)
        {
            // ✓DOC 调用链：Save(false)→ApplyModifiers→Poke→UpdateCornerDepths→RebuildCollisionMesh→RebuildRenderMesh
            Invoke(tcomp, "Save", false);
            Invoke(tcomp, "ApplyModifiers");
            Invoke(hmap,  "Poke");
            Invoke(hmap,  "UpdateCornerDepths");
            Invoke(hmap,  "RebuildCollisionMesh");
            Invoke(hmap,  "RebuildRenderMesh");
        }
        private static void Invoke(object o, string method, params object[] args)
        {
            var m = AccessTools.Method(o.GetType(), method, args.Length>0 ? new[]{ args[0].GetType() } : null);
            m?.Invoke(o, args);
        }

        private void ApplyShift(List<WearNTear> our, float dy)   // ◐DOC: 整体位移改 ZDO 位置
        {
            foreach (var w in our)
            {
                if (w == null) continue;                            // issue #8：已销毁件跳过
                var nv = w.GetComponent<ZNetView>(); if (nv==null||!nv.IsValid()) continue;
                var z = nv.GetZDO(); Vector3 p = z.GetPosition();
                z.SetPosition(new Vector3(p.x, p.y + dy, p.z));
            }
        }
        private float PickShiftByMustDie(List<WearNTear> our)   // ◐DOC §8.3/§9.3: 扫档选必死最少、并列取更深
        {
            float best = 0f; int bestDie = int.MaxValue;
            for (float d = -CfgMaxShift.Value; d <= CfgMaxShift.Value + 1e-4f; d += CfgSinkStep.Value)
            {
                int die = 0;
                foreach (var w in our)
                {
                    if (w == null) { die++; continue; }             // 无法测量按必死计（保守）
                    var col = w.GetComponent<Collider>();
                    if (col == null) { die++; continue; }           // issue #8：同 MeasureRound 的 null 免疫
                    var b = col.bounds;
                    b.center += Vector3.up * d;   // 试位移后
                    if (IsMustDie(b)) die++;
                }
                if (die <= bestDie) { bestDie = die; best = d; }   // ✓DOC: <= → 并列取更深（后扫到的更大 d）
            }
            return -best;   // 下沉为负
        }

        // ---- flag 文件（✓DOC: 铁律，重跑删对应 flag）----
        private static string FlagPath(string n) => Path.Combine(Paths.ConfigPath, n + ".flag");
        private static bool HasFlag(string n) => File.Exists(FlagPath(n));
        private static void WriteFlag(string n) { if (!CfgForce.Value) File.WriteAllText(FlagPath(n), DateTime.Now.ToString("o")); }
    }
}
