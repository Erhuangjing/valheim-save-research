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
        internal static readonly HashSet<ZDOID> s_ourZdoIds = new HashSet<ZDOID>();  // 本 run 创建的件（issue #12：位移的唯一权威集合）
        internal static readonly Dictionary<ZDOID, float> s_initialY = new Dictionary<ZDOID, float>();  // 首次位移前 y（Δy 单值校验）
        internal static bool s_instancesReady;   // WaitInstancesReady 结果载体（迭代器带不出返回值）

        // ====================================================================
        //  配置（REF: 按交接文档 §5.3 的 cfg 全清单声明，类型/默认值可能有出入）
        // ====================================================================
        // [Build]
        internal static ConfigEntry<bool>   CfgEnabled, CfgForce, CfgReconcile, CfgPerPieceGround, CfgAutoDetectGround, CfgAutoPlatformY;
        internal static ConfigEntry<float>  CfgOriginX, CfgOriginZ, CfgYOffset, CfgGroundLayerPy, CfgBatchSize, CfgFrameDelay, CfgRotation;
        internal static ConfigEntry<string> CfgPieceFile;
        // [Cleanup]
        internal static ConfigEntry<bool>   CfgCleanOn, CfgCleanNature, CfgWaitActivate, CfgOnlyPersistent, CfgClearAllInArea;
        internal static ConfigEntry<float>  CfgC1X, CfgC1Z, CfgR1, CfgProtectX, CfgProtectZ, CfgProtectR;
        // [Terrain]（PlanBuild 式逐点整地，规则见 TerrainTask 注释）
        internal static ConfigEntry<bool>   CfgTerrainOn, CfgTerrainDryRun, CfgVerifyOnReload;
        internal static ConfigEntry<float>  CfgEmbed, CfgMaxDelta, CfgPlatformY, CfgSkirt, CfgSkirtMax, CfgSkirtSlope, CfgPad, CfgCap, CfgLayerTol, CfgClose, CfgSink;
        internal static ConfigEntry<string> CfgTerrainPaint, CfgTerrainFile, CfgTerrainDump;
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
            // 落地高度 = 地面层件所在位置的**实测地形**中位数（游戏里量，不再用离线自然物代理值——
            // 代理值差几米就是「房子比地面高一截、门口跳不上去」）。false = 用 [Terrain] PlatformY 手填值
            CfgAutoPlatformY = Config.Bind("Build", "AutoPlatformY", true);
            // 蓝图朝向：绕蓝图包围盒中心旋转的角度（度，俯视顺时针 = Unity yaw；90 = 原来朝北的门改朝东）。
            // 件的位置与朝向、#Terrain、原版地形件一起转（PlanBuild 放置时 transform.rotation 的同款语义）
            CfgRotation  = Config.Bind("Build", "Rotation", 0f);

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

            CfgTerrainOn = Config.Bind("Terrain", "Enabled", false);
            CfgTerrainDryRun = Config.Bind("Terrain", "DryRun", false);    // 只算方案、出报告，一个顶点都不写
            CfgPlatformY = Config.Bind("Terrain", "PlatformY", 39.40f);    // [Build] AutoPlatformY=false 或测不到地形时用
            CfgEmbed     = Config.Bind("Terrain", "Embed", 0.10f);         // 地表压过件底多深（碰到地形才算接地）
            CfgSkirt     = Config.Bind("Terrain", "Skirt", 6f);            // 占地外过渡带最小宽度：平滑接回原地形，不留断崖
            // 过渡带按边界高差自适应加宽：smoothstep 最陡处坡度 = 1.5·Δ/D → D ≥ 1.5·Δ/tan(SkirtSlope)（实测填 3.9m、6m 宽 = 54°）
            CfgSkirtSlope = Config.Bind("Terrain", "SkirtSlope", 35f);     // 过渡带目标最大坡度（度）：门口要走得上去
            CfgSkirtMax  = Config.Bind("Terrain", "SkirtMax", 16f);        // 过渡带最大宽度（米）
            CfgPad       = Config.Bind("Terrain", "Pad", 0.5f);            // 件碰撞体包围盒外扩
            // 占地闭运算半径：贴地件（斜撑脚/柱脚）之间 ≤ 2·Close 的空隙填成同一块平台（= 玩家先用锄头整出整块房基）；0 = 关
            CfgClose     = Config.Bind("Terrain", "Close", 3f);
            // 比平台深 Sink 米以上的非地窖件（打进地里的深桩、下层露台的墙/台阶）不算占地：不给它们整平台，也不挖坑——
            // 下层露台交给蓝图 #Terrain，地窖交给「floor 件挖到件底」
            CfgSink      = Config.Bind("Terrain", "Sink", 1f);
            CfgCap       = Config.Bind("Terrain", "Cap", 1.0f);            // 件底高出平台超过它 = 不贴地（屋檐/二楼），不垫土
            // 地面层 = py ≤ GroundLayerPy + LayerTol。0.5 会卡在 0.5m 直方图档边界上（skeggoxmanor 主底层 −0.55 被 −1.05+0.5 刷掉）
            CfgLayerTol  = Config.Bind("Terrain", "LayerTol", 0.6f);
            CfgMaxDelta  = Config.Bind("Terrain", "MaxDelta", 8f);         // ✓ 游戏硬限 Heightmap.c_LevelMaxDelta：超了整段放弃，不硬改
            CfgTerrainPaint = Config.Bind("Terrain", "Paint", "Dirt");     // 占地刷泥地（原版锄头整地同款，免得草从地板里长出来）；空 = 不刷
            CfgTerrainFile  = Config.Bind("Terrain", "EntryFile", "bp_terrain.txt");      // 蓝图 #Terrain 段（PlanBuild 原格式）
            CfgTerrainDump  = Config.Bind("Terrain", "DumpFile", "bp_terrain_dump.csv");  // 逐顶点 改前/目标/改后，离线出报告用
            CfgVerifyOnReload = Config.Bind("Terrain", "VerifyOnReload", true);           // 下次起服时复核地形是否持久化

            CfgActOn = Config.Bind("Activate", "Enabled", true);        // ✓DOC: 总开关
            CfgActX  = Config.Bind("Activate", "X", -262f);
            CfgActY  = Config.Bind("Activate", "Y", 0f);
            CfgActZ  = Config.Bind("Activate", "Z", 270f);

            CfgAnchorAuto = Config.Bind("Anchor", "AutoSolve", true);
            CfgAnchorTarget = Config.Bind("Anchor", "Target", 200);     // ✓DOC: 甜区（判据升级后仅作参考）
            CfgMaxShift = Config.Bind("Anchor", "MaxShift", 2f);        // ✓DOC: 双向 ±2m
            CfgSinkStep = Config.Bind("Anchor", "SinkStep", 0.05f);

            // issue #13：默认必须开。锚点求解成功（必死=0）阻止不了解冻后的原生磨损销毁
            //（A/B 实测：false 掉件 3.7%~15.9%，true = ±0）；锁只作用于 mark==1 的本工具件
            CfgSupportOn   = Config.Bind("Support", "Enabled", true);
            CfgSupportDiag = Config.Bind("Support", "Diagnose", true);
            CfgMarkKey     = Config.Bind("Support", "MarkKey", "xiabp");

            // ---- Harmony：逐个显式注册（✓DOC 坑 B：不是 CreateAndPatchAll，忘注册会静默失效）----
            harmony = new Harmony("com.world.bpbuild");
            harmony.PatchAll(typeof(PatchServerLoadWorld));
            harmony.PatchAll(typeof(PatchActivate));
            harmony.PatchAll(typeof(PatchFreezeWear));
            harmony.PatchAll(typeof(PatchSupportLock));
            // issue #13 附：补丁自检。⚠ PatchAll(Type) 只补那一个类、不是整个程序集——
            // 新增补丁类忘注册会静默失效（实测两轮探测全假阴性的教训）。
            // 启动时打印实际挂载数与方法名，与预期不符立刻能看出来。
            var patched = Harmony.GetAllPatchedMethods().ToList();
            Log.LogInfo($"XiBpBuilder(ref) 已加载，patch 注册 4 个，实际挂载方法数 = {patched.Count}");
            foreach (var m in patched)
                Log.LogInfo($"[patch]    {m.DeclaringType?.Name}.{m.Name}");
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
            Log.LogInfo($"件清单载入 {pieces.Count} 件 | 落点 ({CfgOriginX.Value},{CfgOriginZ.Value}) | 朝向 {CfgRotation.Value:F1}°");
            if (pieces.Count == 0) { Log.LogWarning("件清单为空，中止"); yield break; }
            InitFrame(pieces);

            // 段1 已冻磨损（s_freezeWear=true 默认）。段0 Activate 由 FixedUpdate postfix 持续生效。
            // 等激活覆盖生效 + zone 长齐
            yield return StartCoroutine(WaitActivateSettled());

            // 段2 Cleanup（清障）
            if (CfgCleanOn.Value && (!HasFlag("cleanup_done") || CfgForce.Value))
                yield return StartCoroutine(CleanupTask());
            else if (HasFlag("cleanup_done"))
                Log.LogInfo("[清理] 跳过：cleanup_done 标志存在（重跑 = 删 BepInEx/config/cleanup_done.flag）");  // issue #16③

            // 落地高度：游戏里实测地形（件还没放，此时量到的就是原地形）；起伏超硬限直接不建
            if (!ResolvePlatformY(pieces)) { Log.LogError("=== 编排中止：落点不可用（见上），一件没放、一个顶点没改 ==="); yield break; }

            // 段4 Build（建房）。issue #12：只有「全新建」才允许锚点整体位移——
            // 对账补建路径只重建缺失件，位移会把新件从旧建筑上撕下来
            bool freshBuild = !HasFlag("bp_done") || CfgForce.Value;
            if (freshBuild)
                yield return StartCoroutine(BuildTask(pieces));
            else if (CfgReconcile.Value)
            {
                Log.LogInfo("[落地] 跳过（bp_done 存在）→ 对账补建缺失件；完整重建 = 删 bp_done.flag");  // issue #16③
                yield return StartCoroutine(ReconcileTask(pieces));   // 段6 对账补齐
            }
            else Log.LogWarning("[落地] 跳过（bp_done 存在且 Reconcile=false），本轮不动件");  // issue #16③
            s_buildDone = true;

            // 段3 Terrain：放在建造**之后**——要按件的真实碰撞体逐点整地（磨损冻结、支撑锁定中，件不会掉）。
            // 最终状态与「先整地再放件」相同，但件底位置是量出来的，不是按件名猜的。
            if (CfgTerrainOn.Value && (!HasFlag("terrain_done") || CfgForce.Value))
            {
                yield return StartCoroutine(WaitInstancesReady(30f));
                yield return StartCoroutine(TerrainTask(pieces));
            }
            else if (HasFlag("terrain_done"))
            {
                Log.LogInfo("[地形] 跳过：terrain_done 标志存在（重跑 = 删该 flag）");  // issue #16③
                if (CfgVerifyOnReload.Value) yield return StartCoroutine(TerrainVerifyReload());
            }
            else if (s_terrainOps.Count > 0)
                Log.LogWarning($"[地形] ⚠ 蓝图含 {s_terrainOps.Count} 个原版地形件（锄头整地/路面），[Terrain] Enabled=false → 未执行");

            // 段5 Anchor（手术窗口内求解 + 整体位移）
            if (CfgAnchorAuto.Value)
            {
                if (s_terrainShaped)
                    Log.LogInfo("[锚点] 跳过：地形已按件底逐点整好，整体位移只会让件离开刚整好的地面");
                else if (freshBuild)
                    yield return StartCoroutine(AnchorSolveTask());
                else
                    Log.LogWarning("[锚点] 跳过：对账补建路径只重建了缺失件，整体位移会撕裂建筑"
                        + "（完整求解 = 删 bp_done.flag 重跑；本次落点高度以 cfg YOffset 为准）");
            }

            // 解冻 → 真实 UpdateSupport 跑起来
            s_freezeWear = false;
            Log.LogInfo("解冻磨损，进入真实模拟观察");

            // 段7 Save
            yield return new WaitForSeconds(3f);   // REF: 让实例化稳定
            yield return StartCoroutine(SaveAndWait(60f));

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
                    if (s_ourZdoIds.Contains(zdo.m_uid)) continue;   // REF: 保护我们的件（issue #12：直接存 ZDOID）
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
        //  蓝图坐标 → 世界坐标（唯一出处）：绕蓝图包围盒中心转 Rotation，再平移到落点
        //  包围盒中心按件清单全集算（含原版地形件）—— 与 bp_reconcile.transform_pieces 同口径
        // ====================================================================
        internal static float s_cx, s_cz;
        internal static Quaternion s_yaw = Quaternion.identity;
        private static void InitFrame(List<Piece> pieces)
        {
            s_cx = (pieces.Min(p => p.x) + pieces.Max(p => p.x)) / 2f;
            s_cz = (pieces.Min(p => p.z) + pieces.Max(p => p.z)) / 2f;
            s_yaw = Quaternion.Euler(0f, CfgRotation.Value, 0f);
        }
        private static Vector3 ToWorld(float px, float py, float pz, float baseY)
        {
            Vector3 d = s_yaw * new Vector3(px - s_cx, 0f, pz - s_cz);
            return new Vector3(CfgOriginX.Value + d.x, baseY + py, CfgOriginZ.Value + d.z);
        }

        // ====================================================================
        //  落地高度（件放下之前在游戏里实测；同一落点的后续 run 沿用首测值）
        // ====================================================================
        internal static float s_platformY;
        private bool ResolvePlatformY(List<Piece> pieces)
        {
            // 对账补建 / 重载复核必须沿用首次测得的高度：地形改过之后再测，量到的就不是原地形了
            string saved = ReadRuntime("PlatformY");
            if (saved != null && HasFlag("bp_done"))
            {
                s_platformY = float.Parse(saved, CultureInfo.InvariantCulture);
                Log.LogInfo($"★ [落地] PlatformY = {s_platformY:F2}（沿用首次 run 实测值，bp_runtime.txt）");
                return true;
            }
            if (!CfgAutoPlatformY.Value)
            {
                s_platformY = CfgPlatformY.Value;
                Log.LogInfo($"★ [落地] PlatformY = {s_platformY:F2}（手填，AutoPlatformY=false）");
                WriteRuntime("PlatformY", s_platformY);
                return true;
            }
            var offs = new List<float>();     // 地面层件：实测地形 − 件 py → 整栋竖直偏移（中位数 = 挖填最少）
            var nearH = new List<float>();    // 贴地件（地面层往上 Cap 内）：可行性预检用
            var nearPy = new List<float>();
            foreach (var pc in pieces)
            {
                if (pc.y > CfgGroundLayerPy.Value + CfgLayerTol.Value + CfgCap.Value) continue;
                if (pc.y < CfgGroundLayerPy.Value - CfgSink.Value) continue;          // 深桩 / 下层结构：不贴原地形，不参与定高与预检
                var w = ToWorld(pc.x, 0f, pc.z, 0f);
                if (!Heightmap.GetHeight(w, out float h)) continue;
                nearH.Add(h);
                nearPy.Add(pc.y);
                if (Mathf.Abs(pc.y - CfgGroundLayerPy.Value) <= CfgLayerTol.Value) offs.Add(h - pc.y);
            }
            if (offs.Count == 0)
            {
                s_platformY = CfgPlatformY.Value;
                Log.LogWarning($"[落地] ⚠ 落点处 heightmap 未加载，测不到地形 → 退回手填 PlatformY={s_platformY:F2}");
                WriteRuntime("PlatformY", s_platformY);
                return true;
            }
            offs.Sort();
            float baseY = offs[offs.Count / 2];
            s_platformY = baseY + CfgGroundLayerPy.Value;
            float dev = 0f;
            for (int i = 0; i < nearH.Count; i++) dev = Mathf.Max(dev, Mathf.Abs(nearH[i] - (baseY + nearPy[i])));
            Log.LogInfo($"★ [落地] PlatformY = {s_platformY:F2}（自动：地面层 {offs.Count} 件「实测地形 − 件高」中位数，"
                + $"偏移范围 {offs[0]:F2}~{offs[offs.Count - 1]:F2}；贴地件处最大高差 {dev:F2}m）");
            float limit = Mathf.Min(CfgMaxDelta.Value, Heightmap.c_LevelMaxDelta);
            if (CfgTerrainOn.Value && dev > limit - 1f)
            {
                Log.LogError($"[落地] ★ 落点起伏过大：贴地件处地形与平台最大差 {dev:F1}m，逼近/超过游戏地形改造硬限 ±{limit:F0}m"
                    + "——整地必然出断崖，本次不建。换个更平的落点（bp_autosite）");
                return false;
            }
            if (!CfgTerrainOn.Value && dev > 1.5f)
                Log.LogWarning($"[落地] ⚠ 未开 [Terrain]，贴地件处原地形起伏 {dev:F1}m → 会有件悬空/埋进土里；建议开 [Terrain]");
            WriteRuntime("PlatformY", s_platformY);
            return true;
        }

        // ====================================================================
        //  段3 Terrain —— PlanBuild 式逐点整地（在件实例化之后跑：要用件的真实碰撞体）
        // ====================================================================
        //  写法照搬 PlanBuild `Blueprints/TerrainTools.cs`（sirskunkalot/PlanBuild，WTFPL）：
        //    只改 TerrainComp 的 m_levelDelta / m_smoothDelta / m_modifiedHeight（+ m_paintMask），然后
        //    ClaimOwnership → m_operations++ → Save(false) → Heightmap.Poke(0,false)，高度/碰撞/渲染交给游戏自己重算。
        //    绝不直写 Heightmap.m_heights：旧实现直写 heights + 自拼 6 步重建链，当场看着对，zone 重载按 delta
        //    重算就对不上；且不 ClaimOwnership 时 Save 是静默空操作（非 owner 不存盘 = 09-16 delta 丢失事故）。
        //  旧实现正式服实测的两个致命点：改的是落点 93m 内 7 块 heightmap 的**全部顶点**（~190m 见方压成同一高度，
        //  ±8m 截断处成斜坡/断崖、与没改的 zone 交界成断层），且不看保护圈。现在只改「占地 + 过渡带」。
        //
        //  逐点规则（米；件的位置全部取实测碰撞体包围盒，不按件名猜尺寸/原点）：
        //    PadY   = 地面层件（落地 y ≤ PlatformY + LayerTol）碰撞体底面中位数 + Embed
        //    占地   = 底面 ≤ PadY + Cap 的件，xz 包围盒外扩 Pad；地表 = clamp(该点最低件底 + Embed, PadY, PadY + Cap)
        //             → 比平台略高的件（Cap 内）垫土接住；打进地里的柱子/墙脚直接埋，不挖坑
        //    地窖   = 名字含 floor、底面 < PadY − 0.5 的件：挖到件底 + Embed（只挖件自身范围，墙体挡土）
        //    过渡带 = 占地外 Skirt 米：目标 = 最近占地点高度，权重 smoothstep(1 − d/Skirt)，平滑接回原地形
        //    蓝图 #Terrain = PlanBuild 放置时的 LevelTerrain（circle/square、smooth、paint）原样叠在后面
        //    原版地形件（mud_road 等：vbuild 蓝图的「地形段」）= 交给原版 TerrainComp.DoOperation
        //  放弃条件（一个顶点都不写）：满权重顶点落进保护圈，或需改 > MaxDelta（游戏硬限 ±8m）
        // ====================================================================
        internal static bool s_terrainShaped;
        private static readonly List<TerrainOpPiece> s_terrainOps = new List<TerrainOpPiece>();
        private struct TerrainOpPiece { public GameObject prefab; public Vector3 pos; public Quaternion rot; }

        private IEnumerator TerrainTask(List<Piece> pieces)
        {
            s_terrainShaped = false;
            string missing = TerrainReflectionMissing();
            if (missing != null)
            {
                Log.LogError($"[地形] ★ 反射取不到 {missing}（游戏更新改名了?）——本段放弃，不写 terrain_done");
                yield break;
            }
            TerrainPlan plan;
            try { plan = BuildTerrainPlan(pieces); }
            catch (Exception e) { Log.LogError($"[地形] ★ 生成整地方案异常：{e}"); yield break; }
            if (plan == null) yield break;                         // 原因已在方案里报过

            float limit = Mathf.Min(CfgMaxDelta.Value, Heightmap.c_LevelMaxDelta);
            List<TcWrite> writes;
            try { writes = PlanWrites(plan, limit); }
            catch (Exception e) { Log.LogError($"[地形] ★ 预演写入异常：{e}"); yield break; }
            Log.LogInfo($"[地形] 方案：{plan.ops.Count} 个顶点（占地 {plan.nFoot} / 地窖 {plan.nDig} / 过渡带 {plan.nSkirt} / "
                + $"#Terrain {plan.nEntryVerts}←{plan.nEntries} 条），涉及 {writes.Count} 个 TerrainComp；PadY={plan.padY:F2}；"
                + $"过渡带被保护圈截断 {plan.protSoft} 点、截到 ±{limit:F0}m {plan.clampedSoft} 点");
            if (plan.protHard > 0 || plan.over > 0)
            {
                if (plan.protHard > 0)
                    Log.LogError($"[地形] ★ 占地/蓝图地形有 {plan.protHard} 个顶点落在保护圈内（{CfgProtectX.Value},{CfgProtectZ.Value} r={CfgProtectR.Value}）"
                        + "——房子压到保护区了，一个顶点都不改。挪落点或缩小保护圈");
                if (plan.over > 0)
                    Log.LogError($"[地形] ★ {plan.over} 个满权重顶点需改超过 ±{limit:F0}m（游戏硬限），例：{plan.overSample}"
                        + "——硬改必成断崖，一个顶点都不改。换更平的落点");
                yield break;
            }
            if (CfgTerrainDryRun.Value)
            {
                WriteTerrainDump(plan, false);
                Log.LogInfo("[地形] DryRun：方案与改前高度已写 dump，一个顶点都没改");
                yield break;
            }

            foreach (var k in plan.protect)
                if (!plan.before.ContainsKey(k) && Heightmap.GetHeight(new Vector3(KX(k), 0f, KZ(k)), out float h0)) plan.before[k] = h0;
            // 一旦开始写，地形就是按件「当前位置」整的：之后无论验收过不过，都不许再做锚点整体位移
            s_terrainShaped = true;
            try { CommitWrites(writes); }
            catch (Exception e) { Log.LogError($"[地形] ★ 写入异常（可能已部分写入，不写 terrain_done，重跑会重算）：{e}"); yield break; }
            yield return StartCoroutine(WaitRegen(writes.Select(w => w.hm).ToList()));

            int vops = 0;
            if (s_terrainOps.Count > 0)
            {
                // 原版锄头件要在上面那次重算之后再做：原版 LevelTerrain 读的是 heightmap 当前高度
                foreach (var t in s_terrainOps)
                {
                    if (ApplyVanillaTerrainOp(t, plan)) vops++;
                    if (vops % 20 == 19) yield return null;
                }
                Log.LogInfo($"[地形] 原版地形件（锄头整地/路面）执行 {vops}/{s_terrainOps.Count} 个");
                yield return StartCoroutine(WaitRegen(null));
            }

            // 验收：满权重顶点的实际高度 vs 目标；占地露缝（地表比该点最低件底低 >0.15m）
            var after = WriteTerrainDump(plan, true);
            var errs = new List<float>();
            foreach (var kv in plan.ops)
            {
                if (kv.Value.w < 0.999f || plan.vanillaTouched.Contains(kv.Key)) continue;
                if (after.TryGetValue(kv.Key, out float h)) errs.Add(Mathf.Abs(h - kv.Value.a));
            }
            errs.Sort();
            float p95 = errs.Count > 0 ? errs[(int)(errs.Count * 0.95f)] : 0f, emax = errs.Count > 0 ? errs[errs.Count - 1] : 0f;
            // 露缝：占地顶点的地表比该点最低件底低 >0.15m = 地基露出来（正式服「房子一半地基漏出来」那种）。
            // 叠在梁/斜撑上的地板本来就不贴地，不算问题——所以按顶点上的最低件判，不按每个件判
            int gaps = 0, footN = 0;
            foreach (var kv in plan.minBottom)
            {
                if (plan.vanillaTouched.Contains(kv.Key) || !plan.role.TryGetValue(kv.Key, out char rr) || rr == 'E'
                    || !after.TryGetValue(kv.Key, out float h)) continue;
                footN++;
                if (h < kv.Value - 0.15f) gaps++;
            }
            Log.LogInfo($"★ [地形] 完成：改 {plan.ops.Count} 个顶点；满权重顶点误差 p95={p95:F3} max={emax:F3}m；"
                + $"占地 {footN} 个顶点中露缝 {gaps} 个；dump → {CfgTerrainDump.Value}");
            if (errs.Count == 0 || emax > 0.5f)
            {
                Log.LogError($"[地形] ★ 验收不过（{(errs.Count == 0 ? "读不回高度" : $"最大误差 {emax:F2}m > 0.5m")}）——不写 terrain_done");
                yield break;
            }
            WriteFlag("terrain_done");
        }

        // 第二次起服（terrain_done 已在）：按 dump 复核地形是否真的持久化（09-16 事故就是重载后 delta 丢了）
        private IEnumerator TerrainVerifyReload()
        {
            string path = Path.Combine(Paths.ConfigPath, CfgTerrainDump.Value);
            if (!File.Exists(path)) { Log.LogInfo("[地形] 重载复核：没有 dump 文件，跳过"); yield break; }
            yield return null;
            var all = File.ReadAllLines(path);
            var meta = all.Where(l => l.StartsWith("#")).ToList();          // 首行 # 元数据原样保留
            var lines = all.Where(l => !l.StartsWith("#")).ToArray();
            if (lines.Length < 2) yield break;
            var head = lines[0].Split(',').ToList();
            int ix = head.IndexOf("x"), iz = head.IndexOf("z"), ia = head.IndexOf("after");
            if (ix < 0 || iz < 0 || ia < 0) { Log.LogWarning("[地形] 重载复核：dump 缺 x/z/after 列"); yield break; }
            var errs = new List<float>();
            int lost = 0;
            int keep = head.Contains("reload") ? head.IndexOf("reload") : head.Count;   // 重复复核：覆盖旧的 reload 列
            var outL = new List<string> { string.Join(",", head.Take(keep)) + ",reload" };
            for (int n = 1; n < lines.Length; n++)
            {
                var f = lines[n].Split(',');
                string cell = "";
                if (f.Length > ia && f[ia].Length > 0)
                {
                    if (Heightmap.GetHeight(new Vector3(Inv(f[ix]), 0f, Inv(f[iz])), out float h))
                    { errs.Add(Mathf.Abs(h - Inv(f[ia]))); cell = h.ToString("F3", CultureInfo.InvariantCulture); }
                    else lost++;
                }
                outL.Add(string.Join(",", f.Take(keep)) + "," + cell);
            }
            File.WriteAllLines(path, meta.Concat(outL));
            errs.Sort();
            float mx = errs.Count > 0 ? errs[errs.Count - 1] : float.NaN, p95 = errs.Count > 0 ? errs[(int)(errs.Count * 0.95f)] : float.NaN;
            Log.LogInfo($"★ [地形] 重载复核：{errs.Count} 个顶点与落地当时比 |Δ| p95={p95:F3} max={mx:F3}m（未加载 {lost}）→ "
                + (errs.Count > 0 && mx < 0.05f ? "持久化 ✓" : "✗ 地形与落地当时不一致（delta 丢失?）"));
        }

        // ====================================================================
        //  段4 Build（✓DOC: 交接文档坑 #2 创建时序）
        // ====================================================================
        private IEnumerator BuildTask(List<Piece> pieces)
        {
            Log.LogInfo("[落地] 开始");
            float groundBase = s_platformY - CfgGroundLayerPy.Value;   // PlatformY 已在游戏里实测（ResolvePlatformY）

            int batch = 0, created = 0, fail = 0;
            s_terrainOps.Clear();
            foreach (var pc in pieces.OrderBy(p => p.y))   // ✓DOC: 按 py 低→高，先地基后上层
            {
                Vector3 world = ToWorld(pc.x, pc.y + CfgYOffset.Value, pc.z, groundBase);
                // 原版地形件（锄头整地 mud_road / 路面 paved_road…）不是建筑：建成 ZDO 会被反复实例化、反复改地形。
                // 按原版做法当「地形操作」在地形段执行（PlanBuild 放置蓝图时也是直接实例化让 TerrainOp 自己跑）
                var top = TerrainOpPrefab(pc.hash, pc.name);
                if (top != null) { s_terrainOps.Add(new TerrainOpPiece { prefab = top, pos = world, rot = s_yaw * pc.rot }); continue; }
                if (CreatePiece(pc, world)) { created++; } else { fail++; }

                if (++batch % (int)CfgBatchSize.Value == 0)
                {
                    Log.LogInfo($"[落地] 累计 {created}/{pieces.Count}");
                    for (int f = 0; f < (int)CfgFrameDelay.Value; f++) yield return null;  // ✓DOC: FrameDelay
                }
            }
            if (created == 0)
            {
                Log.LogError($"★ 落地完成：0 件（失败 {fail} 次）——件清单空或哈希全无效，"
                    + "不写 bp_done 标志（issue #17：避免后续运行静默跳过整个 Build 段）");
                yield break;
            }
            Log.LogInfo($"★ 落地完成：{created} 件，prefab 校验失败 {fail} 次"
                + (s_terrainOps.Count > 0 ? $"（另有 {s_terrainOps.Count} 个原版地形件交给地形段执行）" : ""));
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
            zdo.SetRotation(s_yaw * pc.rot);     // ✓DOC: 全四元数（不要只传 yaw）；蓝图朝向叠在件自身朝向之前
            zdo.Persistent = true;               // ✓DOC: flags bit8
            zdo.SetOwner(0L);                    // ✓DOC: 无主，等 Activate 认领
            zdo.Set(CfgMarkKey.Value, 1);        // ◐DOC: 打标，供 Support Lock / 保护圈识别

            if (zdo.GetPrefab() != hash) { Log.LogError($"[落地] 校验失败 {pc.name}"); return false; }  // ✓DOC 当场校验
            s_ourZdoIds.Add(zdo.m_uid);   // issue #12：存 ZDOID 本体（GetHashCode 不保唯一）
            return true;
        }

        // ====================================================================
        //  段5 Anchor（◐DOC: 收官报告 §1.2 d_i + §8.3 必死件判据 + §1.3 闭环 3 轮）
        // ====================================================================
        private IEnumerator AnchorSolveTask()
        {
            Log.LogInfo("[锚点] 求解开始（手术窗口内，磨损已冻）");
            // issue #8：失败必须可见（异常显式报错 + 回退声明）。
            // issue #12：位移对象 = 本 run 创建的全集（s_ourZdoIds），绝不用 mark key 反查
            // m_objectsByID——mark 持久化在世界里，混入历史遗留会把他人的建筑整体下沉。
            if (s_ourZdoIds.Count == 0)
            {
                Log.LogWarning("[锚点] 跳过：本 run 未创建件（s_ourZdoIds 为空）");
                yield break;
            }
            // issue #12：等实例化齐再统一位移——对未实例化 ZDO 调 SetPosition 可能不持久，
            // 且每轮按「已实例化子集」位移会让累计量因件而异（Δy 非单值的根因）。
            yield return StartCoroutine(WaitInstancesReady(30f));
            if (!s_instancesReady)
            {
                Log.LogWarning($"[锚点] ★ 实例化等待超时，自动求解未生效 —— YOffset 沿用 cfg 手填值 {CfgYOffset.Value}（issue #8/#12）");
                yield break;
            }

            float totalShift = 0f;
            bool failed = false;
            for (int round = 0; round < 3; round++)     // ✓DOC: 最多 3 轮闭环
            {
                int mustDie = 0, anchor0 = 0, noCol = 0;
                float bestShift = 0f;
                List<WearNTear> our = null;
                try
                {
                    our = CollectOurPieces();           // 测量用实例（要 Collider 几何）；位移不走它
                    if (our.Count == 0) { Log.LogWarning("[锚点] 拿不到实例（mark 丢失或未实例化）"); failed = true; break; }
                    MeasureRound(our, out mustDie, out anchor0, out noCol);   // 逐件求 d_i
                    // ✓DOC §8.3/§9.3 判据：选必死件最少的档，并列取更深
                    bestShift = PickShiftByMustDie(our);    // ◐DOC: 扫 [-MaxShift,+MaxShift]，step SinkStep
                }
                catch (Exception e)
                {
                    Log.LogError($"[锚点] 第{round+1}轮测量异常：{e}");
                    failed = true;
                    break;
                }
                Log.LogInfo($"[锚点] 第{round+1}轮 必死={mustDie} 锚点={anchor0} 无碰撞体跳过={noCol}");
                if (Mathf.Approximately(bestShift, 0f) && mustDie == 0 && anchor0 >= CfgAnchorTarget.Value)
                {
                    Log.LogInfo("[锚点] 已达标，停止");
                    break;
                }
                try { ApplyShiftOurZdos(bestShift); }  // issue #12：全集统一位移（不再按每轮实例化子集）
                catch (Exception e) { Log.LogError($"[锚点] 位移异常：{e}"); failed = true; break; }
                totalShift += bestShift;
                yield return new WaitForSeconds(0.5f); // REF: 等物理/几何更新（yield 必须在 try 外：迭代器语法限制）
            }

            if (failed)
            {
                Log.LogWarning($"[锚点] ★ 自动求解未生效 —— YOffset 沿用 cfg 手填值 {CfgYOffset.Value}；"
                    + "若为 0 则整栋按贴地硬边界落地，请按实测手调（issue #8）");
                yield break;
            }

            // issue #12：Δy 单值校验（全量只读，比抽样便宜）——期望每件 Δy == totalShift
            int checkedN = 0, mismatch = 0;
            foreach (var kv in s_initialY)
            {
                var z = ZDOMan.instance.GetZDO(kv.Key);
                if (z == null) continue;
                checkedN++;
                if (Mathf.Abs((z.GetPosition().y - kv.Value) - totalShift) > 0.05f) mismatch++;
            }
            Log.LogInfo($"[锚点] 位置校验：{checkedN} 件中 Δy≠累计位移 {mismatch} 件（容差 0.05m）");
            if (mismatch > 0)
                Log.LogWarning("[锚点] ⚠ Δy 非单值——未实例化件的位置可能未持久，复查位移时机（issue #12）");
            Log.LogInfo($"★ [锚点] 求解结束：累计位移 {totalShift:F2}m");
        }

        // issue #12：等「本 run 创建的件」实例化齐（数量稳定）再位移；结果经 s_instancesReady 带出
        private IEnumerator WaitInstancesReady(float timeout)
        {
            float t0 = Time.time;
            int stable = 0, last = -1;
            while (Time.time - t0 < timeout)
            {
                int live = 0;
                foreach (var id in s_ourZdoIds)
                {
                    var z = ZDOMan.instance.GetZDO(id);              // REF: 签名待核
                    if (z != null && ZNetScene.instance.FindInstance(z) != null) live++;
                }
                if (live >= s_ourZdoIds.Count)
                {
                    Log.LogInfo($"[锚点] 实例化就绪 {live}/{s_ourZdoIds.Count}（等待 {Time.time - t0:F1}s）");
                    s_instancesReady = true;
                    yield break;
                }
                if (live == last) stable++; else { stable = 0; last = live; }
                if (stable >= 3)
                {
                    Log.LogWarning($"[锚点] 实例化停滞在 {live}/{s_ourZdoIds.Count}（3 秒无增长），按现状继续");
                    s_instancesReady = true;
                    yield break;
                }
                yield return new WaitForSeconds(1f);
            }
            Log.LogWarning($"[锚点] 实例化等待超时（{timeout:F0}s）");
            s_instancesReady = false;
        }

        // issue #12：对「本 run 创建的全集」统一位移；首次位移前记录 y 供 Δy 校验
        private void ApplyShiftOurZdos(float dy)
        {
            if (s_initialY.Count == 0)
                foreach (var id in s_ourZdoIds)
                {
                    var z = ZDOMan.instance.GetZDO(id);
                    if (z != null) s_initialY[id] = z.GetPosition().y;
                }
            int shifted = 0, gone = 0;
            foreach (var id in s_ourZdoIds)
            {
                var z = ZDOMan.instance.GetZDO(id);                  // REF: 签名待核
                if (z == null) { gone++; continue; }
                Vector3 p = z.GetPosition();
                z.SetPosition(new Vector3(p.x, p.y + dy, p.z));
                shifted++;
            }
            Log.LogInfo($"[锚点] 位移 {dy:F2}m：{shifted} 件（ZDO 不存在 {gone}）");
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
            float groundBase = s_platformY - CfgGroundLayerPy.Value;
            int miss = 0, rebuilt = 0, rebuildFail = 0;
            s_terrainOps.Clear();
            foreach (var pc in pieces.OrderBy(p=>p.y))   // ✓DOC: 低→高补建
            {
                Vector3 world = ToWorld(pc.x, pc.y + CfgYOffset.Value, pc.z, groundBase);
                var top = TerrainOpPrefab(pc.hash, pc.name);    // 原版地形件本来就不留 ZDO，不算缺失（terrain_done 缺时交给地形段重做）
                if (top != null) { s_terrainOps.Add(new TerrainOpPiece { prefab = top, pos = world, rot = s_yaw * pc.rot }); continue; }
                if (!Present(have, pc.hash, world))
                {
                    miss++;
                    if (CreatePiece(pc, world)) rebuilt++; else rebuildFail++;   // issue #16①：报实际成功数
                }
            }
            Log.LogInfo($"[对账] 命中 {have.Count} / 缺失补建 {rebuilt} 成功（失败 {rebuildFail}，应补 {miss}）");
        }
        private static string Key(int h, Vector3 p) =>   // ✓DOC: 0.25m 量化，y 也要
            $"{h}|{Mathf.RoundToInt(p.x*4)}|{Mathf.RoundToInt(p.y*4)}|{Mathf.RoundToInt(p.z*4)}";
        // 精确 key 会把落在 0.25m 档边界上的件（存档 float 往返后舍入到隔壁档）误判为缺失 → 重复补建
        //（2026-09-23 实测：第二次起服走对账路径，3117 件里 177 件被重复建了一遍）。同 hash、三轴各 ±1 档内有 ZDO 即算在
        private static bool Present(HashSet<string> have, int h, Vector3 p)
        {
            int qx = Mathf.RoundToInt(p.x * 4), qy = Mathf.RoundToInt(p.y * 4), qz = Mathf.RoundToInt(p.z * 4);
            for (int dx = -1; dx <= 1; dx++)
            for (int dy = -1; dy <= 1; dy++)
            for (int dz = -1; dz <= 1; dz++)
                if (have.Contains($"{h}|{qx + dx}|{qy + dy}|{qz + dz}")) return true;
            return false;
        }

        // ====================================================================
        //  段7 Save（✓DOC: 反射 DelayedSave(true)；SaveWorldAndPlayerProfiles 会 NRE）
        // ====================================================================
        // 等 DelayedSave 协程真正跑完再往下走：「编排完成」之后外部随时可以停服（实测 Ctrl+Break 停不掉，只能强停）
        private IEnumerator SaveAndWait(float timeout)
        {
            var m = AccessTools.Method(typeof(ZNet), "DelayedSave");   // REF: 返回协程
            if (m == null) { Log.LogWarning("[保存] 找不到 DelayedSave"); yield break; }
            var coro = m.Invoke(ZNet.instance, new object[] { true }) as IEnumerator;
            if (coro == null) { Log.LogWarning("[保存] DelayedSave 未返回协程"); yield break; }
            bool done = false;
            StartCoroutine(Then(coro, () => done = true));
            Log.LogInfo("[保存] 已触发 DelayedSave(true)");
            float t0 = Time.time;
            while (!done && Time.time - t0 < timeout) yield return null;
            if (done) Log.LogInfo($"[保存] 存档写盘完成（{Time.time - t0:F1}s）");
            else Log.LogWarning($"[保存] ⚠ {timeout:F0}s 内没等到存档协程结束——停服前请确认已存盘");
        }
        private IEnumerator Then(IEnumerator inner, Action after)
        {
            yield return StartCoroutine(inner);
            after();
        }

        private void SupportDiag()
        {
            // ◐DOC §9.3: 遍历 WearNTear，读 m_support vs GetMinSupport，报告 starved 数
            // issue #16②：取值域自检——starved=0 无法区分「真没问题」与「反射拿错值」，
            // 故先验证反射可用，再输出值域与越界计数，让「读错了」变得可判定。
            var getMin = AccessTools.Method(typeof(WearNTear), "GetMinSupport");
            AccessTools.FieldRef<WearNTear, float> refSup = null;
            try { refSup = AccessTools.FieldRefAccess<WearNTear, float>("m_support"); }
            catch (Exception e) { Log.LogError($"[体检] 反射取 m_support 失败（字段名变了?）：{e.Message}——体检输出不可信"); return; }
            if (getMin == null) { Log.LogError("[体检] 反射取 GetMinSupport 失败（方法名变了?）——体检输出不可信"); return; }
            int starved = 0, total = 0, domainBad = 0;
            float lo = float.MaxValue, hi = float.MinValue;
            foreach (var w in WearNTear.GetAllInstances())
            {
                total++;
                float sup = refSup(w), min = Convert.ToSingle(getMin.Invoke(w, null));
                if (float.IsNaN(sup) || float.IsInfinity(sup) || sup < -1f || sup > 100000f) domainBad++;
                if (sup < lo) lo = sup; if (sup > hi) hi = sup;
                if (sup < min - 0.001f) starved++;
            }
            Log.LogInfo($"[体检] WearNTear 共 {total}，支撑不足 {starved}；support 值域 [{lo:F1},{hi:F1}]，"
                + $"越界 {domainBad}（⚠️瞬时采样会漏，判崩塌看件数时序；值域异常=反射拿错值，issue #16②）");
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

        // ---- Terrain 助手（PlanBuild 式）----
        // 反射成员 2026-09-23 对 assembly_valheim.dll 逐个核对可见性（改动前请重新核对，#19/#22 的教训）：
        //   TerrainComp（全 private）: float[] m_levelDelta/m_smoothDelta, bool[] m_modifiedHeight/m_modifiedPaint,
        //     Color[] m_paintMask, int m_operations, Vector3 m_lastOpPoint, float m_lastOpRadius,
        //     void Save(bool paintOnly), void DoOperation(Vector3 pos, Vector3 rot, TerrainOp.Settings)
        //   Heightmap: private List<float> m_heights；其余用到的（m_width/m_scale/IsDistantLod/Poke(int,bool)/
        //     VertexMaskToWorld/GetAndCreateTerrainCompiler/HaveQueuedRebuild/static GetHeight/c_LevelMaxDelta/m_paintMask*）均 public
        private static readonly FieldInfo FiHeights = AccessTools.Field(typeof(Heightmap), "m_heights");
        private static readonly FieldInfo FiLevel   = AccessTools.Field(typeof(TerrainComp), "m_levelDelta");
        private static readonly FieldInfo FiSmooth  = AccessTools.Field(typeof(TerrainComp), "m_smoothDelta");
        private static readonly FieldInfo FiModH    = AccessTools.Field(typeof(TerrainComp), "m_modifiedHeight");
        private static readonly FieldInfo FiPaint   = AccessTools.Field(typeof(TerrainComp), "m_paintMask");
        private static readonly FieldInfo FiModP    = AccessTools.Field(typeof(TerrainComp), "m_modifiedPaint");
        private static readonly FieldInfo FiOps     = AccessTools.Field(typeof(TerrainComp), "m_operations");
        private static readonly FieldInfo FiLastPt  = AccessTools.Field(typeof(TerrainComp), "m_lastOpPoint");
        private static readonly FieldInfo FiLastR   = AccessTools.Field(typeof(TerrainComp), "m_lastOpRadius");
        private static readonly MethodInfo MiSave   = AccessTools.Method(typeof(TerrainComp), "Save", new[] { typeof(bool) });
        private static readonly MethodInfo MiDoOp   = AccessTools.Method(typeof(TerrainComp), "DoOperation",
                                                        new[] { typeof(Vector3), typeof(Vector3), typeof(TerrainOp.Settings) });

        private static string TerrainReflectionMissing()
        {
            string[] names = { "Heightmap.m_heights", "TerrainComp.m_levelDelta", "TerrainComp.m_smoothDelta",
                               "TerrainComp.m_modifiedHeight", "TerrainComp.m_paintMask", "TerrainComp.m_modifiedPaint",
                               "TerrainComp.m_operations", "TerrainComp.m_lastOpPoint", "TerrainComp.m_lastOpRadius",
                               "TerrainComp.Save(bool)" };
            object[] got = { FiHeights, FiLevel, FiSmooth, FiModH, FiPaint, FiModP, FiOps, FiLastPt, FiLastR, MiSave };
            var miss = names.Where((n, i) => got[i] == null).ToList();
            return miss.Count == 0 ? null : string.Join(", ", miss);
        }

        private struct VOp { public float a, w; }      // 复合后的「目标高度 a、权重 w」：h' = h + w(a − h)
        private class PBox { public string name; public float pivotY; public Bounds b; public bool floor; }
        private class TerrainPlan
        {
            public readonly Dictionary<long, VOp> ops = new Dictionary<long, VOp>();
            public readonly Dictionary<long, char> role = new Dictionary<long, char>();   // P 占地 / B 地窖 / S 过渡带 / E #Terrain
            public readonly Dictionary<long, string> paint = new Dictionary<long, string>();
            public readonly Dictionary<long, float> before = new Dictionary<long, float>();
            public readonly Dictionary<long, float> minBottom = new Dictionary<long, float>();   // 占地顶点上最低件底（验收露缝用）
            public readonly HashSet<long> vanillaTouched = new HashSet<long>();
            public readonly HashSet<long> protect = new HashSet<long>();         // 本该改、因在保护圈内而跳过的顶点（dump 记 X，独立复核没动）
            public List<PBox> boxes;
            public float padY, skirt;
            public int nFoot, nDig, nSkirt, nFilled, nEntries, nEntryVerts, protHard, protSoft, over, clampedSoft;
            public string overSample = "";
        }
        private static long VKey(int x, int z) => ((long)x << 32) | (uint)z;
        private static int KX(long k) => (int)(k >> 32);
        private static int KZ(long k) => unchecked((int)(uint)k);

        // 顺序施加两次「拉向目标」的精确复合（PlanBuild 的 LevelTerrain 本质就是 h += w(a − h)）：
        //   先 (a1,w1) 再 (a2,w2) ≡ 一次 (A,W)：W = 1 − (1−w1)(1−w2)，A = (a1·w1·(1−w2) + a2·w2) / W
        // 所以整份方案对每个顶点只写一次 delta，不依赖 Poke 之后 heights 何时刷新。
        private static void Compose(TerrainPlan p, long k, float a, float w, char role)
        {
            if (w <= 1e-4f) return;
            if (p.ops.TryGetValue(k, out var o))
            {
                float W = 1f - (1f - o.w) * (1f - w);
                p.ops[k] = new VOp { a = (o.a * o.w * (1f - w) + a * w) / W, w = W };
            }
            else p.ops[k] = new VOp { a = a, w = w };
            p.role[k] = role;
        }

        // 本工具件（mark==1）在蓝图范围内的实测碰撞体。issue #8/#16 的「无碰撞体」多数是碰撞体挂在子物体上 → 取整棵子树
        private List<PBox> MeasureOurPieces(float radius, out int noCollider)
        {
            var list = new List<PBox>();
            noCollider = 0;
            float ox = CfgOriginX.Value, oz = CfgOriginZ.Value;
            foreach (var w in WearNTear.GetAllInstances())
            {
                if (w == null) continue;
                var nv = w.GetComponent<ZNetView>();
                if (nv == null || !nv.IsValid() || nv.GetZDO().GetInt(CfgMarkKey.Value, 0) != 1) continue;
                Vector3 p = w.transform.position;
                if (Dist2D(p, ox, oz) > radius + 3f) continue;                       // 半径筛：蓝图可能旋转过
                bool any = false;
                var b = new Bounds();
                foreach (var c in w.GetComponentsInChildren<Collider>())
                {
                    if (c == null || !c.enabled || c.isTrigger) continue;
                    if (!any) { b = c.bounds; any = true; } else b.Encapsulate(c.bounds);
                }
                if (!any) { noCollider++; continue; }
                string name = w.gameObject.name.Replace("(Clone)", "");
                list.Add(new PBox { name = name, pivotY = p.y, b = b,
                                    floor = name.IndexOf("floor", StringComparison.OrdinalIgnoreCase) >= 0 });
            }
            return list;
        }

        private TerrainPlan BuildTerrainPlan(List<Piece> pieces)
        {
            float bx0 = pieces.Min(p => p.x), bx1 = pieces.Max(p => p.x), bz0 = pieces.Min(p => p.z), bz1 = pieces.Max(p => p.z);
            var plan = new TerrainPlan { boxes = MeasureOurPieces(0.5f * Mathf.Sqrt((bx1 - bx0) * (bx1 - bx0) + (bz1 - bz0) * (bz1 - bz0)), out int noCol) };
            var ground = plan.boxes.Where(q => Mathf.Abs(q.pivotY - s_platformY) <= CfgLayerTol.Value)
                                   .Select(q => q.b.min.y).OrderBy(y => y).ToList();
            Log.LogInfo($"[地形] 开始：实测本工具件碰撞体 {plan.boxes.Count} 个（无碰撞体 {noCol}），地面层 {ground.Count} 个");
            if (ground.Count == 0)
            {
                Log.LogError("[地形] ★ 一个地面层件的碰撞体都没测到（未实例化?）——本段放弃，不写 terrain_done");
                return null;
            }
            float embed = CfgEmbed.Value, padY = ground[ground.Count / 2] + embed, capY = padY + CfgCap.Value;
            plan.padY = padY;

            // ① 占地 / 地窖：碰撞体包围盒逐点栅格化（地形顶点在整数世界坐标，scale = 1）
            var foot = new Dictionary<long, float>();
            var dig = new Dictionary<long, float>();
            foreach (var q in plan.boxes)
            {
                if (q.b.min.y > capY) continue;                                    // 屋檐 / 二楼：不贴地，不垫土
                float t = Mathf.Clamp(q.b.min.y + embed, padY, capY);
                bool cellar = q.floor && q.b.min.y < padY - 0.5f;
                if (!cellar && q.b.min.y < padY - CfgSink.Value) continue;          // 深桩 / 下层露台：不整平台不挖坑
                int x0 = Mathf.CeilToInt(q.b.min.x - CfgPad.Value), x1 = Mathf.FloorToInt(q.b.max.x + CfgPad.Value);
                int z0 = Mathf.CeilToInt(q.b.min.z - CfgPad.Value), z1 = Mathf.FloorToInt(q.b.max.z + CfgPad.Value);
                for (int x = x0; x <= x1; x++)
                for (int z = z0; z <= z1; z++)
                {
                    long k = VKey(x, z);
                    foot[k] = foot.TryGetValue(k, out float f) ? Mathf.Min(f, t) : t;
                    plan.minBottom[k] = plan.minBottom.TryGetValue(k, out float mb) ? Mathf.Min(mb, q.b.min.y) : q.b.min.y;
                    if (cellar && x >= q.b.min.x && x <= q.b.max.x && z >= q.b.min.z && z <= q.b.max.z)
                        dig[k] = dig.TryGetValue(k, out float d) ? Mathf.Min(d, q.b.min.y + embed) : q.b.min.y + embed;
                }
            }
            if (foot.Count == 0) { Log.LogError("[地形] ★ 占地为空（贴地件全被 Cap 排除?）——本段放弃"); return null; }

            // ② 平台 H = 占地 F 的闭运算（先膨胀 Close 再腐蚀 Close）：贴地件之间的空隙也整平到 PadY
            int gx0 = int.MaxValue, gx1 = int.MinValue, gz0 = int.MaxValue, gz1 = int.MinValue;
            foreach (var k in foot.Keys)
            {
                int x = KX(k), z = KZ(k);
                if (x < gx0) gx0 = x; if (x > gx1) gx1 = x; if (z < gz0) gz0 = z; if (z > gz1) gz1 = z;
            }
            float close = Mathf.Max(0f, CfgClose.Value), skirtMax = Mathf.Max(CfgSkirt.Value, CfgSkirtMax.Value);
            int M = Mathf.CeilToInt(close + skirtMax) + 2;
            gx0 -= M; gx1 += M; gz0 -= M; gz1 += M;
            int nx = gx1 - gx0 + 1, nz = gz1 - gz0 + 1;
            var dist = new float[nx * nz];
            var nearT = new float[nx * nz];
            var hard = new float[nx * nz];                        // 平台目标高度；NaN = 不在平台内
            for (int i = 0; i < dist.Length; i++) { dist[i] = float.MaxValue; hard[i] = float.NaN; }
            foreach (var kv in foot)
            {
                int i = (KX(kv.Key) - gx0) * nz + (KZ(kv.Key) - gz0);
                dist[i] = 0f;
                hard[i] = kv.Value;
            }
            if (close > 0f)
            {
                Chamfer(dist, nearT, nx, nz);                     // 到 F 的距离 → 膨胀 D = {dist ≤ close}
                var outD = new float[nx * nz];
                var dummy = new float[nx * nz];
                for (int i = 0; i < outD.Length; i++) outD[i] = dist[i] <= close ? float.MaxValue : 0f;
                Chamfer(outD, dummy, nx, nz);                     // 到 D 外的距离 → 腐蚀
                int filled = 0;
                for (int i = 0; i < outD.Length; i++)
                    if (float.IsNaN(hard[i]) && outD[i] > close + 0.01f
                        && !InProtectCircle(new Vector3(gx0 + i / nz, 0f, gz0 + i % nz)))   // 填补不进保护圈（圈伸进建筑凹口时）
                    { hard[i] = padY; filled++; }
                plan.nFilled = filled;
                for (int i = 0; i < dist.Length; i++) dist[i] = float.IsNaN(hard[i]) ? float.MaxValue : 0f;
            }
            for (int i = 0; i < dist.Length; i++) if (!float.IsNaN(hard[i])) nearT[i] = hard[i];

            // 过渡带宽度：按平台边界上「目标 − 现地形」的最大高差 Δ 自适应（smoothstep 最陡处坡度 = 1.5·Δ/D）
            float edgeD = 0f;
            for (int ix = 1; ix < nx - 1; ix++)
            for (int iz = 1; iz < nz - 1; iz++)
            {
                int i = ix * nz + iz;
                if (float.IsNaN(hard[i])) continue;
                if (!float.IsNaN(hard[i + nz]) && !float.IsNaN(hard[i - nz]) && !float.IsNaN(hard[i + 1]) && !float.IsNaN(hard[i - 1])) continue;
                if (Heightmap.GetHeight(new Vector3(gx0 + ix, 0f, gz0 + iz), out float h0)) edgeD = Mathf.Max(edgeD, Mathf.Abs(hard[i] - h0));
            }
            float skirt = plan.skirt = Mathf.Clamp(1.5f * edgeD / Mathf.Tan(Mathf.Clamp(CfgSkirtSlope.Value, 5f, 80f) * Mathf.Deg2Rad),
                                                   CfgSkirt.Value, skirtMax);
            Log.LogInfo($"[地形] 平台 = 占地 {foot.Count} + 闭运算填补 {plan.nFilled} 个顶点；边界最大高差 {edgeD:F2}m → "
                + $"过渡带宽 {skirt:F1}m（目标坡度 ≤ {CfgSkirtSlope.Value:F0}°）");
            Chamfer(dist, nearT, nx, nz);

            // ③ 方案：占地 / 地窖满权重；过渡带 smoothstep（两端斜率为 0，不起棱）
            string footPaint = (CfgTerrainPaint.Value ?? "").Trim();
            for (int ix = 0; ix < nx; ix++)
            for (int iz = 0; iz < nz; iz++)
            {
                int i = ix * nz + iz;
                float d = dist[i];
                if (d >= skirt) continue;
                int x = gx0 + ix, z = gz0 + iz;
                long k = VKey(x, z);
                bool isHard = d <= 0f;
                if (InProtectCircle(new Vector3(x, 0f, z))) { if (isHard) plan.protHard++; else plan.protSoft++; plan.protect.Add(k); continue; }
                if (isHard)
                {
                    bool isDig = dig.TryGetValue(k, out float dg);
                    Compose(plan, k, isDig ? dg : hard[i], 1f, isDig ? 'B' : 'P');
                    if (isDig) plan.nDig++; else plan.nFoot++;
                    if (footPaint.Length > 0) plan.paint[k] = footPaint;
                }
                else
                {
                    float s = 1f - d / skirt;
                    Compose(plan, k, nearT[i], s * s * (3f - 2f * s) * ProtectFade(x, z), 'S');
                    plan.nSkirt++;
                }
            }

            // ④ 蓝图 #Terrain：照 PlanBuild PlacementComponent.PlaceBlueprint（坐标变换与件完全一致）
            float baseY = s_platformY - CfgGroundLayerPy.Value;
            foreach (var e in LoadTerrainEntries())
            {
                bool square = e.shape.Equals("square", StringComparison.OrdinalIgnoreCase);
                if (!square && !e.shape.Equals("circle", StringComparison.OrdinalIgnoreCase))
                {
                    Log.LogWarning($"[地形] #Terrain 未知 shape={e.shape}，跳过（PlanBuild 同样只认 circle/square）");
                    continue;
                }
                Vector3 ew = ToWorld(e.x, e.y, e.z, baseY);
                float ex = ew.x, ez = ew.z, ey = ew.y;
                int ccx = Mathf.FloorToInt(ex + 0.5f), ccz = Mathf.FloorToInt(ez + 0.5f);   // = Heightmap.WorldToVertex 的取整
                float r = Mathf.Max(e.radius, 0.01f), ang = (e.rotation + CfgRotation.Value) * Mathf.Deg2Rad, co = Mathf.Cos(ang), si = Mathf.Sin(ang);
                int R = Mathf.CeilToInt(r * 1.4143f) + 1;
                for (int dx = -R; dx <= R; dx++)
                for (int dz = -R; dz <= R; dz++)
                {
                    float D;
                    if (square)
                    {
                        float u = co * dx - si * dz, v = si * dx + co * dz;          // PlanBuild TerrainTools.GetX/GetY
                        if (Mathf.Abs(u) > r || Mathf.Abs(v) > r) continue;
                        D = Mathf.Max(Mathf.Abs(u), Mathf.Abs(v)) / r;
                    }
                    else
                    {
                        float dd = Mathf.Sqrt(dx * dx + dz * dz);
                        if (dd > r) continue;
                        D = dd / r;
                    }
                    float m = (1f - D) >= e.smooth ? 1f : (1f - D) / e.smooth;       // PlanBuild CalculateSmooth
                    int x = ccx + dx, z = ccz + dz;
                    long k = VKey(x, z);
                    if (InProtectCircle(new Vector3(x, 0f, z))) { if (m >= 0.999f) plan.protHard++; else plan.protSoft++; plan.protect.Add(k); continue; }
                    Compose(plan, k, ey, m * ProtectFade(x, z), 'E');
                    plan.nEntryVerts++;
                    if (e.paint.Length > 0) plan.paint[k] = e.paint;
                }
                plan.nEntries++;
            }
            return plan;
        }

        // 保护圈外 3m 内把过渡带 / #Terrain 权重平滑压到 0：圈内原样不动，圈边也不留台阶
        private float ProtectFade(int x, int z)
        {
            if (CfgProtectR.Value <= 0f) return 1f;
            float d = Dist2D(new Vector3(x, 0f, z), CfgProtectX.Value, CfgProtectZ.Value) - CfgProtectR.Value;
            if (d >= 3f) return 1f;
            if (d <= 0f) return 0f;
            float t = d / 3f;
            return t * t * (3f - 2f * t);
        }

        // 两遍倒角距离变换（1 / √2 权重，与欧氏距离误差 < 8%），同时把最近源点的目标高度一路带过去
        private static void Chamfer(float[] dist, float[] nearT, int nx, int nz)
        {
            for (int pass = 0; pass < 2; pass++)
            {
                int s = pass == 0 ? 1 : -1;
                for (int a = 0; a < nx; a++)
                for (int b = 0; b < nz; b++)
                {
                    int ix = pass == 0 ? a : nx - 1 - a, iz = pass == 0 ? b : nz - 1 - b, i = ix * nz + iz;
                    Relax(dist, nearT, nx, nz, i, ix - s, iz, 1f);
                    Relax(dist, nearT, nx, nz, i, ix, iz - s, 1f);
                    Relax(dist, nearT, nx, nz, i, ix - s, iz - s, 1.41421356f);
                    Relax(dist, nearT, nx, nz, i, ix - s, iz + s, 1.41421356f);
                }
            }
        }
        private static void Relax(float[] dist, float[] nearT, int nx, int nz, int i, int jx, int jz, float c)
        {
            if (jx < 0 || jz < 0 || jx >= nx || jz >= nz) return;
            int j = jx * nz + jz;
            if (dist[j] + c < dist[i]) { dist[i] = dist[j] + c; nearT[i] = nearT[j]; }
        }

        // 蓝图 #Terrain 段（PlanBuild TerrainModEntry 原格式：shape;x;y;z;radius;rotation;smooth;paint）
        private struct TEntry { public string shape, paint; public float x, y, z, radius, smooth; public int rotation; }
        private List<TEntry> LoadTerrainEntries()
        {
            var list = new List<TEntry>();
            string path = Path.Combine(Paths.ConfigPath, CfgTerrainFile.Value);
            if (!File.Exists(path)) return list;
            foreach (var raw in File.ReadAllLines(path))
            {
                var s = raw.Trim();
                if (s.Length == 0 || s.StartsWith("#")) continue;
                var f = s.Split(';');
                if (f.Length < 8) { Log.LogWarning($"[地形] #Terrain 行字段不足 8 段，跳过：{s}"); continue; }
                try
                {
                    list.Add(new TEntry { shape = f[0].Trim(), x = Inv(f[1]), y = Inv(f[2]), z = Inv(f[3]), radius = Inv(f[4]),
                                          rotation = (int)Inv(f[5]), smooth = Inv(f[6]), paint = f[7].Trim() });
                }
                catch (Exception e) { Log.LogWarning($"[地形] #Terrain 行解析失败，跳过：{s}（{e.Message}）"); }
            }
            return list;
        }
        private static float Inv(string s) =>
            float.Parse(s.Trim().Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture);

        // 预演：算出每个 TerrainComp 要写的 delta（先不写）。跨 zone 边界的共享顶点，两侧 TerrainComp 各写一份 → 不留缝
        private class TcWrite
        {
            public Heightmap hm; public TerrainComp tc;
            public readonly List<int> idx = new List<int>(); public readonly List<float> lvl = new List<float>();
            public readonly List<int> pidx = new List<int>(); public readonly List<Color> pcol = new List<Color>();
        }
        private List<TcWrite> PlanWrites(TerrainPlan plan, float limit)
        {
            int x0 = int.MaxValue, x1 = int.MinValue, z0 = int.MaxValue, z1 = int.MinValue;
            foreach (var k in plan.ops.Keys)
            {
                int x = KX(k), z = KZ(k);
                if (x < x0) x0 = x; if (x > x1) x1 = x; if (z < z0) z0 = z; if (z > z1) z1 = z;
            }
            var hms = new List<Heightmap>();
            Heightmap.FindHeightmap(new Vector3((x0 + x1) / 2f, 0f, (z0 + z1) / 2f), Mathf.Max(x1 - x0, z1 - z0) * 0.75f + 2f, hms);
            var writes = new List<TcWrite>();
            foreach (var hm in hms)
            {
                if (hm == null || hm.IsDistantLod) continue;
                var heights = (List<float>)FiHeights.GetValue(hm);
                int w = hm.m_width;
                float sc = hm.m_scale;
                Vector3 hp = hm.transform.position;
                TcWrite tw = null;
                float[] L = null, Sd = null;
                for (int i = 0; i <= w; i++)
                for (int j = 0; j <= w; j++)
                {
                    long k = VKey(Mathf.RoundToInt(hp.x + (j - w / 2) * sc), Mathf.RoundToInt(hp.z + (i - w / 2) * sc));
                    if (!plan.ops.TryGetValue(k, out var op)) continue;
                    if (tw == null)
                    {
                        tw = new TcWrite { hm = hm, tc = hm.GetAndCreateTerrainCompiler() };
                        if (tw.tc == null) throw new InvalidOperationException($"heightmap@({hp.x:F0},{hp.z:F0}) 拿不到 TerrainComp");
                        L = (float[])FiLevel.GetValue(tw.tc);
                        Sd = (float[])FiSmooth.GetValue(tw.tc);
                    }
                    int idx = i * (w + 1) + j;                 // 行主序 z*(w+1)+x：#19 实测往返自检确认过
                    float cur = hp.y + heights[idx];
                    if (!plan.before.ContainsKey(k)) plan.before[k] = cur;
                    float nl = L[idx] + Sd[idx] + op.w * (op.a - cur);   // 原版 LevelTerrain 同式：smoothDelta 并入后清零
                    if (Mathf.Abs(nl) > limit)
                    {
                        if (op.w >= 0.999f) { if (plan.over++ < 5) plan.overSample += $"({KX(k)},{KZ(k)}) 需 {nl:+0.0;-0.0}m "; }
                        else plan.clampedSoft++;
                        nl = Mathf.Clamp(nl, -limit, limit);
                    }
                    tw.idx.Add(idx);
                    tw.lvl.Add(nl);
                }
                if (tw == null) continue;
                if (plan.paint.Count > 0)
                {
                    var P = (Color[])FiPaint.GetValue(tw.tc);
                    int pw = Mathf.RoundToInt(Mathf.Sqrt(P.Length));
                    for (int y = 0; y < pw; y++)
                    for (int x = 0; x < pw; x++)
                    {
                        Vector3 c = hm.VertexMaskToWorld(x, y);
                        if (!plan.paint.TryGetValue(VKey(Mathf.FloorToInt(c.x), Mathf.FloorToInt(c.z)), out string pn)) continue;
                        if (!TryPaintColor(pn, out Color col)) continue;
                        int pi = y * pw + x;
                        col.a = P[pi].a;                        // PlanBuild：alpha 通道保留原值
                        tw.pidx.Add(pi);
                        tw.pcol.Add(col);
                    }
                }
                writes.Add(tw);
            }
            return writes;
        }

        // 提交：PlanBuild TerrainTools.Save 原样 —— ClaimOwnership → 改数组 → m_operations++ → Save(false) → Poke(0,false)
        private void CommitWrites(List<TcWrite> writes)
        {
            foreach (var tw in writes)
            {
                tw.tc.GetComponent<ZNetView>()?.ClaimOwnership();       // 非 owner 时 Save 静默不存盘（重载后 delta 丢失的根因）
                var L = (float[])FiLevel.GetValue(tw.tc);
                var Sd = (float[])FiSmooth.GetValue(tw.tc);
                var M = (bool[])FiModH.GetValue(tw.tc);
                for (int n = 0; n < tw.idx.Count; n++) { int i = tw.idx[n]; L[i] = tw.lvl[n]; Sd[i] = 0f; M[i] = true; }
                if (tw.pidx.Count > 0)
                {
                    var P = (Color[])FiPaint.GetValue(tw.tc);
                    var MP = (bool[])FiModP.GetValue(tw.tc);
                    for (int n = 0; n < tw.pidx.Count; n++) { P[tw.pidx[n]] = tw.pcol[n]; MP[tw.pidx[n]] = true; }
                }
                FiOps.SetValue(tw.tc, (int)FiOps.GetValue(tw.tc) + 1);
                FiLastPt.SetValue(tw.tc, Vector3.zero);                 // 这两个只用来清草，PlanBuild 同样置零
                FiLastR.SetValue(tw.tc, 0f);
                MiSave.Invoke(tw.tc, new object[] { false });
                tw.hm.Poke(0, false);
            }
        }

        private static IEnumerator WaitRegen(List<Heightmap> hms)
        {
            for (int f = 0; f < 3; f++) yield return null;
            float t0 = Time.time;
            while (Time.time - t0 < 5f)
            {
                var list = hms ?? Heightmap.GetAllHeightmaps();
                if (!list.Any(h => h != null && h.HaveQueuedRebuild())) yield break;
                yield return null;
            }
            Log.LogWarning("[地形] ⚠ 等 heightmap 重算超时 5s，按现状继续");
        }

        private static bool TryPaintColor(string n, out Color c)
        {
            switch (n.Trim().ToLowerInvariant())
            {
                case "dirt": c = Heightmap.m_paintMaskDirt; return true;
                case "paved": c = Heightmap.m_paintMaskPaved; return true;
                case "cultivate": case "cultivated": c = Heightmap.m_paintMaskCultivated; return true;
                case "reset": c = Heightmap.m_paintMaskNothing; return true;
                default: c = default(Color); return false;
            }
        }

        // 原版地形件：ZNetScene 注册表没有的（非联网 prefab）去锄头/耕地机等工具的 PieceTable 里找
        private static Dictionary<int, GameObject> s_opPrefabs;
        private static GameObject TerrainOpPrefab(int hash, string name)
        {
            if (s_opPrefabs == null)
            {
                s_opPrefabs = new Dictionary<int, GameObject>();
                if (ObjectDB.instance != null)
                    foreach (var item in ObjectDB.instance.m_items)
                    {
                        var drop = item ? item.GetComponent<ItemDrop>() : null;
                        var pt = drop ? drop.m_itemData?.m_shared?.m_buildPieces : null;
                        if (pt == null) continue;
                        foreach (var go in pt.m_pieces)
                            if (go && go.GetComponent<TerrainOp>()) s_opPrefabs[go.name.GetStableHashCode()] = go;
                    }
                Log.LogInfo($"[地形] 原版地形件 prefab {s_opPrefabs.Count} 个：{string.Join(", ", s_opPrefabs.Values.Select(g => g.name).OrderBy(n => n))}");
            }
            if (s_opPrefabs.TryGetValue(hash, out var hit)) return hit;
            // 旧名 → 现行 _v2（BuildShare 老蓝图记的是 mud_road / path / paved_road / raise）
            if (name != null && !name.EndsWith("_v2") && s_opPrefabs.TryGetValue((name + "_v2").GetStableHashCode(), out hit)) return hit;
            var zp = ZNetScene.instance ? ZNetScene.instance.GetPrefab(hash) : null;
            return zp && zp.GetComponent<TerrainOp>() ? zp : null;
        }

        // = 原版 TerrainComp.RPC_ApplyOperation 里 owner 做的那一步（TerrainOp.Awake → ApplyOperation → RPC → DoOperation）
        private bool ApplyVanillaTerrainOp(TerrainOpPiece t, TerrainPlan plan)
        {
            var op = t.prefab.GetComponent<TerrainOp>();
            if (op == null || MiDoOp == null) return false;
            float r = op.m_settings.GetRadius();
            if (Dist2D(t.pos, CfgProtectX.Value, CfgProtectZ.Value) < CfgProtectR.Value + r) return false;   // 保护圈一点不碰
            var hms = new List<Heightmap>();
            Heightmap.FindHeightmap(t.pos, r + 1f, hms);
            foreach (var hm in hms)
            {
                if (hm == null || hm.IsDistantLod) continue;
                var tc = hm.GetAndCreateTerrainCompiler();
                if (tc == null) continue;
                tc.GetComponent<ZNetView>()?.ClaimOwnership();
                MiDoOp.Invoke(tc, new object[] { t.pos, t.rot.eulerAngles, op.m_settings });
            }
            int R = Mathf.CeilToInt(r) + 1, px = Mathf.RoundToInt(t.pos.x), pz = Mathf.RoundToInt(t.pos.z);
            for (int dx = -R; dx <= R; dx++)
            for (int dz = -R; dz <= R; dz++)
                plan.vanillaTouched.Add(VKey(px + dx, pz + dz));      // 这些顶点归原版操作管，不计入验收误差
            return true;
        }

        // 逐顶点 dump（首行 # 元数据）→ tools/bp_terrain_report.py 出坡度 / 误差 / 保护圈报告
        private Dictionary<long, float> WriteTerrainDump(TerrainPlan plan, bool withAfter)
        {
            var after = new Dictionary<long, float>();
            var sb = new System.Text.StringBuilder();
            sb.Append("# ").Append(string.Join(",", new[] {
                "PlatformY=" + F3(s_platformY), "PadY=" + F3(plan.padY), "OriginX=" + F3(CfgOriginX.Value), "OriginZ=" + F3(CfgOriginZ.Value),
                "ProtectX=" + F3(CfgProtectX.Value), "ProtectZ=" + F3(CfgProtectZ.Value), "ProtectR=" + F3(CfgProtectR.Value),
                "Skirt=" + F3(plan.skirt), "Embed=" + F3(CfgEmbed.Value), "Cap=" + F3(CfgCap.Value) })).Append('\n');
            sb.Append(withAfter ? "x,z,role,w,target,before,bottom,after\n" : "x,z,role,w,target,before,bottom\n");
            foreach (var kv in plan.ops)
            {
                int x = KX(kv.Key), z = KZ(kv.Key);
                // V = 原版地形件（作者的锄头整地/路面）后来又改过的顶点：最终形状归作者，不按本工具的目标验收
                char role = plan.vanillaTouched.Contains(kv.Key) ? 'V' : plan.role[kv.Key];
                sb.Append(x).Append(',').Append(z).Append(',').Append(role).Append(',')
                  .Append(F3(kv.Value.w)).Append(',').Append(F3(kv.Value.a)).Append(',')
                  .Append(plan.before.TryGetValue(kv.Key, out float b) ? F3(b) : "").Append(',')
                  .Append(plan.minBottom.TryGetValue(kv.Key, out float mb) ? F3(mb) : "");
                if (withAfter)
                {
                    sb.Append(',');
                    if (Heightmap.GetHeight(new Vector3(x, 0f, z), out float h)) { after[kv.Key] = h; sb.Append(F3(h)); }
                }
                sb.Append('\n');
            }
            foreach (var k in plan.protect)
            {
                if (plan.ops.ContainsKey(k)) continue;
                int x = KX(k), z = KZ(k);
                string b0 = plan.before.TryGetValue(k, out float bb) ? F3(bb) : "";
                sb.Append(x).Append(',').Append(z).Append(",X,0.000,").Append(b0).Append(',').Append(b0).Append(',');
                if (withAfter)
                {
                    sb.Append(',');
                    if (Heightmap.GetHeight(new Vector3(x, 0f, z), out float h)) sb.Append(F3(h));
                }
                sb.Append('\n');
            }
            File.WriteAllText(Path.Combine(Paths.ConfigPath, CfgTerrainDump.Value), sb.ToString());
            return after;
        }
        private static string F3(float v) => v.ToString("F3", CultureInfo.InvariantCulture);

        // 跨 run 的运行时状态（key=value）：PlatformY 必须沿用首测值（对账补建 / 重载复核 / 离线对账都要它）
        private static string RuntimePath => Path.Combine(Paths.ConfigPath, "bp_runtime.txt");
        private static string ReadRuntime(string key)
        {
            if (!File.Exists(RuntimePath)) return null;
            foreach (var l in File.ReadAllLines(RuntimePath))
            {
                int i = l.IndexOf('=');
                if (i > 0 && l.Substring(0, i).Trim() == key) return l.Substring(i + 1).Trim();
            }
            return null;
        }
        private static void WriteRuntime(string key, float v)
        {
            var kv = new Dictionary<string, string>();
            if (File.Exists(RuntimePath))
                foreach (var l in File.ReadAllLines(RuntimePath))
                {
                    int i = l.IndexOf('=');
                    if (i > 0) kv[l.Substring(0, i).Trim()] = l.Substring(i + 1).Trim();
                }
            kv[key] = v.ToString("F3", CultureInfo.InvariantCulture);
            File.WriteAllLines(RuntimePath, kv.Select(p => p.Key + "=" + p.Value));
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
