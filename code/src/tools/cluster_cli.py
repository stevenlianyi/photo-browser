#! /usr/bin/env python3
#encoding: utf-8

#Filename: cluster_cli.py
#Description: photo-browser 未归类人脸聚类执行入口（步骤 7）
#
# 用法
# ----
#   # 看现状（人脸四态 / 库里已有的簇）
#   python code\src\tools\cluster_cli.py --status
#
#   # 聚类（缺省即basicSettings 的 eps=0.45 / min_samples=3）
#   python code\src\tools\cluster_cli.py
#
#   # 只看结果不落库
#   python code\src\tools\cluster_cli.py --dry-run
#
#   # 调参（eps / min_samples 全部可配置，不必改代码）
#   python code\src\tools\cluster_cli.py --eps 0.5 --min-samples 2
#
#   # 改了 eps 之后把旧簇彻底作废再重算（会清已归类行上的簇编码）
#   python code\src\tools\cluster_cli.py --rebuild --dry-run
#
#   # 每簇拼一张 contact sheet 供肉眼抽查（临时拼图，写到 <thumb>/cluster）
#   python code\src\tools\cluster_cli.py --contact-sheet --top 3
#
#   # 两个队列的条数与内容（待确认 / 「我不同意」）
#   python code\src\tools\cluster_cli.py --queues --limit 10
#   python code\src\tools\cluster_cli.py --disputed --limit 40
#
#   # 指到临时库做实验（绝不动正式库）
#   python code\src\tools\cluster_cli.py --db d:\tmp\test.db
#
# 硬约束
# ------
#   * photo 目录**只读**；本工具只碰数据库（读 pb_face.embedding /
#     写 pb_face.clusterCode）与 thumb\cluster 下的临时拼图
#   * 聚类集合 = `personCode IS NULL AND isStranger=0 AND delFlag='0'`：
#     **已归类人脸绝不重新聚类**，isStranger=1 既不进队列也不进聚类
#   * **不写 pb_review_log**（聚类是机器行为，不是人工操作）
#   * 单写入者：只在主进程单线程跑
#   * 幂等：同一份数据重跑clusterCode 完全稳定（内容寻址，见 dbscan.clusterCodeOf）
#   * --rebuild 是**唯一**会碰已归类行的动作，且必须显式指定

import argparse
import json
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc# noqa: E402
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                      # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon      # noqa: E402
from engine.cluster import clustering as clustering                     # noqa: E402
from processor.review import assigner as assigner                       # noqa: E402
from engine.cluster import dbscan as dbscan                             # noqa: E402
from processor.media import thumbStore as thumbStore                   # noqa: E402
from processor.review import queue as reviewQueue                       # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("clusterCli", "clustercli.log")

#: contact sheet 的输出子目录（转发自 basicSettings，落点推导在 clustering.sheetDir）
CONTACT_DIRNAME: str = basicSettings.CLUSTER_SHEET_SUBDIR


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _ensureDb(dbFile: str = None, quiet: bool = False) -> str:
    target = os.path.abspath(dbFile or paths.db_file())
    if dbFile:
        sqliteCommon.dbHandle(target)                  # DR-10：显式才切库
    if not sqliteCommon.chkTableExist("pb_face"):
        if not quiet:
            print("[建库] 表不存在，先建库: %s" % target)
        from tools import build_db as build_db
        build_db.build(dbFile=target, verbose=not quiet)
    return target


# ============================================================
# 一、--status
# ============================================================

def cmdStatus(args) -> int:
    db = _ensureDb(args.db)
    print("库              :", db)
    print("聚类集合        : personCode IS NULL AND isStranger=0 AND delFlag='0'")
    states = reviewQueue.countStates()
    print("人脸四态        : 待确认 %d / 我不同意 %d / 人工确认 %d / 陌生人 %d"
          % (states["pending"], states["disputed"], states["confirmed"],
             states["stranger"]))
    print("  待确认口径    : SELECT COUNT(*) FROM pb_face WHERE %s"
          % reviewQueue.WHERE_PENDING.replace("%s", "0").replace(
              "delFlag = 0", "delFlag = '0'"))
    print("  我不同意口径  : SELECT COUNT(*) FROM pb_face WHERE %s"
          % reviewQueue.WHERE_DISPUTED.replace("%s", "0").replace(
              "delFlag = 0", "delFlag = '0'"))
    print("  pendingCount  : %s"
          % json.dumps(reviewQueue._scanJobPendingCount(), ensure_ascii=False))
    stat = clustering.clusterStats()
    print("已有簇          : %d 个，带簇编码的行 %d（其中已归类 %d，陌生人 %d）"
          % (stat["clusters"], stat["codedRows"], stat["assigned"], stat["noise"]))
    if stat["noise"]:
        print("  ⚠️ 陌生人却带着簇编码 %d 行（不该发生；--rebuild 可清）" % stat["noise"])
    print("聚类参数        : eps=%s  min_samples=%s  后端=%s"
          % (args.eps if args.eps is not None else basicSettings.DBSCAN_EPS,
             args.min_samples if args.min_samples is not None
             else basicSettings.DBSCAN_MIN_SAMPLES,
             dbscan.resolveBackend(args.backend)))
    for code, one in sorted(stat["detail"].items(),
                            key=lambda kv: -kv[1]["size"])[:10]:
        print("   %-22s %d 张（已归类 %d）最高 detScore %.4f"
              % (code, one["size"], one["assigned"], one["bestDet"]))
    return 0


# ============================================================
# 二、--run（缺省动作）
# ============================================================

def cmdRun(args) -> int:
    db = _ensureDb(args.db)
    eps = args.eps if args.eps is not None else basicSettings.DBSCAN_EPS
    need = args.min_samples if args.min_samples is not None \
        else basicSettings.DBSCAN_MIN_SAMPLES
    backend = dbscan.resolveBackend(args.backend)
    print("库              :", db)
    print("参数            : eps=%.4f  min_samples=%d  后端=%s%s"
          % (eps, need, backend, "  **--dry-run 不落库**" if args.dry_run else ""))

    if args.rebuild:
        cleared = clustering.clearClusterCodes(onlyPending=False, dryRun=args.dry_run)
        print("--rebuild 清簇  : 扫 %d 行，清 %d 行%s（%d 行本来就没簇编码）"
              % (cleared["rows"], cleared["cleared"],
                 "（dry-run）" if args.dry_run else "", cleared["skipped"]))
        if cleared["failed"]:
            print("  ⚠️ 清理失败 %d 行" % cleared["failed"])

    before = clustering.stateCounts()
    assignedBefore = before["confirmed"] + before["disputed"]
    print("聚类前已归类人脸: %d（人工确认 %d + 我不同意 %d）"
          % (assignedBefore, before["confirmed"], before["disputed"]))

    start = time.perf_counter()
    try:
        result = clustering.clusterUnclassified(eps=eps, minSamples=need,
                                               limit=args.limit, backend=backend,
                                               dbFile=db, progress=_progress())
    except dbscan.ClusterError as e:
        print("[聚类中止] %s" % e)
        return 2
    totalSec = time.perf_counter() - start

    stat = result.toDict()
    print("\n聚类集合        : %d 张脸（装载 %.3fs，脏行 %d）"
          % (stat["faces"], stat["loadSec"], stat["skipped"]))
    if not stat["faces"]:
        print("没有未归类的人脸（personCode IS NULL AND isStranger=0）。")
        return 0
    print("聚类            : %d 个簇 + %d 个噪声点，耗时 %.3fs（%.2f ms/张）"
          % (stat["clusters"], stat["noise"], stat["clusterSec"],
             stat["clusterSec"] * 1000.0 / max(1, stat["faces"])))
    print("  簇规模        : 最大 %d / 最小 %d / 平均 %.2f"
          % (stat["largest"], stat["minSize"], stat["meanSize"]))
    print("  噪声点        : %d 张，clusterCode 写 NULL（%s 只是查询层占位）"
          % (stat["noise"], stat["noisePlaceholder"]))
    print("  端到端        : %.3fs" % totalSec)

    if not args.detail and not args.contact_sheet:
        print("\n前10 个簇:")
        for one in result.clusters[:10]:
            print("   %-22s n=%-3d rep=%-34s det=%.4f 簇内均值相似度 %.4f"
                  % (one.clusterCode, one.size, one.representativeFaceCode,
                     one.representativeDetScore or 0.0, one.meanSimilarity or 0.0))
    if args.detail:
        print("\n簇明细:")
        for one in result.clusters:
            print("   %-22s n=%-3d rep=%-34s det=%.4f 均值相似度 %.4f"
                  % (one.clusterCode, one.size, one.representativeFaceCode,
                     one.representativeDetScore or 0.0, one.meanSimilarity or 0.0))
            if args.detail > 1:
                for faceCode in one.faceCodes:
                    print("        %s" % faceCode)

    writeStat = clustering.applyCluster(result, dryRun=args.dry_run)
    print("\n落库            : 写 %d / 编码未变 %d / 新编码 %d / 清空 %d / 失败 %d%s"
          % (writeStat["written"], writeStat["unchanged"], writeStat["changed"],
             writeStat["cleared"], writeStat["failed"],
             "（dry-run，未真写）" if args.dry_run else ""))
    if not args.dry_run and writeStat["changed"] == 0 and writeStat["cleared"] == 0:
        print("  幂等校验      : 本次**没有产生任何新编码**（重跑结果稳定）")

    after = clustering.stateCounts()
    assignedAfter = after["confirmed"] + after["disputed"]
    print("\n聚类后人脸四态  : 待确认 %d / 我不同意 %d / 人工确认 %d / 陌生人 %d"
          % (after["pending"], after["disputed"], after["confirmed"],
             after["stranger"]))
    print("  已归类人脸    : 聚类前 %d -> 聚类后 %d %s"
          % (assignedBefore, assignedAfter,
             "（不变，验收第 4 条通过）" if assignedBefore == assignedAfter
             else "**变了！有人把已归类的脸改了归属**"))
    if after["pending"] != before["pending"]:
        print("  ⚠️ 待确认条数变了（%d -> %d）：聚类**不得**改动 personCode/isStranger"
              % (before["pending"], after["pending"]))

    if args.contact_sheet:
        print()
        made = writeContactSheets(result, top=args.top, cells=args.cells)
        print("contact sheet   : 写了 %d 张 -> %s" % (len(made), made[0] if made else "-"))
        print("  每张图= 一个簇，一行 = 簇内成员（带绿框那张= detScore最高的代表样本）")
        print("  肉眼抽查：同一行的人脸**确实是一个人**才算聚类可信")
        # ⚠️ 必做：clusterCode 是内容指纹，簇成员变了编码就变。
        #    留着旧拼图 = 让人对着一个**已经不存在的簇**下结论。
        stale = clustering.purgeStaleSheets([one.clusterCode for one in result.clusters])
        if stale:
            print("  清掉过期拼图  : %d 张（簇编码已变，见 queue.py「四之二」）"
                  % len(stale))
    return 0


def _progress():
    last = [0.0]

    def _report(kept, seen):
        now = time.perf_counter()
        if now - last[0] < 0.5 and kept:
            return
        last[0] = now
        print("  装载中... 保留 %d / 已扫 %d 行" % (kept, seen))
    return _report


# ============================================================
# 三、contact sheet（临时拼图，供肉眼抽查）
# ============================================================

def writeContactSheets(result, top: int = 0, cells: int = 6, cellSize: int = 96) -> list:
    """每簇拼一张 jpg，写到 `<thumbRoot>\\cluster\\`，返回文件路径列表。

    为什么是「临时拼图」而不是正式产物
    ------------------------------
      验收第 1 条只要求「输出 contact sheet 抽查」。这东西是**给人眼用的
      一次性证据**，不是产品功能（步骤 11 的 UI 会直接渲染人脸裁剪图，
      不需要预拼图）。所以它不写进thumbStore 的索引、不进库、
      删掉也不影响任何东西 —— 放在 thumbRoot 下是为了**绝不碰 photo 目录**。

    ⚠️ 裁剪图可能不在磁盘（步骤 5 没跑过 / 被人删了）：那一格画灰底 +
       faceCode 缩写，**不中断整张图**。抽查工具因为缺图而中断，
       等于告诉使用者「这簇不用看了」—— 那才是真正的误导。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:  # pragma: no cover
        print("[contact sheet] 需要 Pillow: %s" % e)
        return []
    outDir = clustering.sheetDir(create=True)
    if not os.path.isdir(outDir):
        print("[contact sheet] 建目录失败: %s" % outDir)
        return []
    clusters = result.clusters[:int(top or 0)] if top else result.clusters
    made = []
    for one in clusters:
        faces = one.faceCodes[:int(cells or 6)]
        pad, labelH = 6, 14
        width = len(faces) * (cellSize + pad) + pad
        height = cellSize + pad * 2 + labelH
        canvas = Image.new("RGB", (width, height), (245, 245, 245))
        draw = ImageDraw.Draw(canvas)
        draw.text((pad, pad), "%s  n=%d  rep_det=%.4f"
                  % (one.clusterCode, one.size, one.representativeDetScore or 0.0),
                  fill=(20, 20, 20))
        for i, faceCode in enumerate(faces):
            x0 = pad + i * (cellSize + pad)
            y0 = pad + labelH
            try:
                absPath = thumbStore.face_abspath(faceCode)
            except Exception:                      # 编码不合法等
                absPath = ""
            tile = None
            if absPath and os.path.isfile(absPath):
                try:
                    with Image.open(absPath) as im:
                        tile = im.convert("RGB")
                except OSError:
                    tile = None
            if tile is None:
                draw.rectangle([x0, y0, x0 + cellSize, y0 + cellSize],
                               fill=(210, 210, 210))
                draw.text((x0 + 2, y0 + cellSize // 2), faceCode[:8], fill=(90, 90, 90))
            else:
                tile = tile.resize((cellSize, cellSize))
                # 代表样本（detScore 最高的那张）加绿框 —— 抽查时先看它
                color = (0, 150, 0) if faceCode == one.representativeFaceCode else (170, 170, 170)
                canvas.paste(tile, (x0, y0))
                draw.rectangle([x0, y0, x0 + cellSize - 1, y0 + cellSize - 1],
                               outline=color, width=2)
        outPath = os.path.join(outDir, "%s.jpg" % one.clusterCode)
        try:
            canvas.save(outPath, "JPEG", quality=88)
        except OSError as e:
            print("[contact sheet] 写 %s 失败: %s" % (outPath, e))
            continue
        made.append(outPath)
    return made


# ============================================================
# 四、--queues / --disputed
# ============================================================

def cmdQueues(args) -> int:
    db = _ensureDb(args.db)
    print("库              :", db)
    report = reviewQueue.selfCheck()
    print("四态(SQL count) : %s" % json.dumps(report["bySql"], ensure_ascii=False))
    print("四态(装载过滤)  : %s" % json.dumps(report["byRow"], ensure_ascii=False))
    print("两个口径一致    : %s" % ("是" if report["match"] else "**否，要查**"))
    print("pendingCount 字段: %s"
          % json.dumps(report["pendingCountField"], ensure_ascii=False))
    print("  ⚠️ 上面这个 pendingCount 是「疑似移动/重命名待确认**张数**」，"
          "与待确认**人脸**条数不是一回事（DR-11）")

    items = reviewQueue.pendingQueue(limit=args.limit or 10, offset=args.offset,
                                     sortFirst=not args.window)
    print("\n待确认队列      : %d 条（口径 personCode IS NULL AND isStranger=0）"
          % reviewQueue.countPending())
    for one in items:
        print("  %-34s 桶 %-10s 机器分 %-7s 簇 %-22s %s"
              % (one["faceCode"], one["shotBucket"] or "(无)",
                 one["similarity"], one["clusterCode"] or "(无)",
                 "; ".join("%s/%.4f" % (c["displayName"] or c["personCode"],
                                        c["similarity"])
                           for c in one["topCandidates"][:3])))
    if not items:
        print("  （空）")
    return 0


def cmdDisputed(args) -> int:
    db = _ensureDb(args.db)
    print("库              :", db)
    print("「我不同意」列表: %d 条（口径 personCode IS NOT NULL AND isConfirmed=0"
          " AND isStranger=0）" % reviewQueue.countDisputed())
    groups = reviewQueue.disputedList(limit=args.limit or 20, offset=args.offset)
    if not groups:
        print("  （空）")
        return 0
    for one in groups:
        print("  照片 %s  %d 张脸" % (one["photoCode"], one["faceCount"]))
        for face in one["faces"]:
            print("     %-34s -> %-10s 当时相似度 %-7s 簇 %s"
                  % (face["faceCode"], face["displayName"] or face["personCode"],
                     face["similarity"], face["clusterCode"] or "(无)"))
    return 0


# ============================================================
# 五、--explain（只看判定原因，不写库）
# ============================================================

def cmdExplain(args) -> int:
    _ensureDb(args.db)
    eps = args.eps if args.eps is not None else basicSettings.DBSCAN_EPS
    need = args.min_samples if args.min_samples is not None \
        else basicSettings.DBSCAN_MIN_SAMPLES
    print("eps=%.4f（= 余弦相似度 >= %.4f 算邻居）  min_samples=%d" % (eps, 1.0 - eps, need))
    print("改参数的影响：eps 越大簇越大越容易把两个人凑一簇；"
          "min_samples 越大越保守（3 = 至少3 张脸互相像）。")
    for value in (0.35, 0.40, 0.45, 0.50, 0.55):
        result = clustering.clusterUnclassified(eps=value, minSamples=need)
        stat = result.toDict()
        print("  eps=%.2f -> 相似度>=%.2f : %d 簇 + %d 噪声 / 最大簇 %d"
              % (value, 1.0 - value, stat["clusters"], stat["noise"], stat["largest"]))
    return 0


def cmdPropose(args) -> int:
    _ensureDb(args.db)
    # verify 交叉核对：pb_face 与 pb_photo_person 是否一致。
    # ⚠️ 聚类**不该**动 personCode，所以 verify 的结论应当与聚类前完全相同；
    #    出现差异就说明有人把聚类写成了归属 —— 那是要立刻查的错。
    verify = assigner.verifyLinks()
    print("库            :", sqliteCommon.dbFilePath())
    print("一致性核对    : %s（orphanLink %d / missingLink %d / badFaceCode %d）"
          % ("干净" if verify["clean"] else "**有问题**", len(verify.get("orphanLink", [])),
             len(verify.get("missingLink", [])), len(verify.get("badFaceCode", []))))
    stat = reviewQueue.clusterStats()
    print("簇            : %d 个 / 覆盖 %d 张脸 / 噪声 %d（min_samples=%d）"
          % (stat["clusters"], stat["faces"], stat["noise"],
             args.min_samples or basicSettings.DBSCAN_MIN_SAMPLES))
    print()
    print("候选人物建议（**只按年龄排除，不能代替你确认**）:")
    anyWeak = False
    for one in reviewQueue.clusterProposals(limit=args.limit or 10,
                                            minSize=args.min_size):
        span = one["shotSpan"]
        print("  %-22s n=%-3d 拍摄年 %s"
              % (one["clusterCode"], one["size"],
                 ("%d-%d" % (span[0], span[1])) if span else "(未知)"))
        if not one["discriminative"]:
            anyWeak = True
        for c in one["candidates"]:
            if c["fit"] == "age-mismatch":
                continue
            age = ("%d~%d 岁" % (c["ageSpan"][0], c["ageSpan"][1])
                   if c["ageSpan"] else "生日未知")
            print("      %-9s %-16s %-12s %s"
                  % ("[年龄符]" if c["fit"] == "age-ok" else "[无生日]",
                     c["displayName"], age, c["personCode"]))
    if anyWeak:
        print()
        print("  ⚠️ discriminative=False：本库所有簇都落在同一个年代桶(2010-2014)，")
        print("     年龄条件把 10 个联系人里的 7 个都放进来了 —— **一条也没排掉**。")
        print("     这个库上唯一有效的办法是**你看图**（contact sheet 已生成），")
        print("     别拿年龄当依据。")
    return 0


# ============================================================
# 六、--clusters / --members（簇视图，只读）
# ============================================================

def cmdClusters(args) -> int:
    _ensureDb(args.db)
    stat = reviewQueue.clusterStats()
    print("库          :", sqliteCommon.dbFilePath())
    print("簇概览      : %d 个簇 / 覆盖 %d 张脸 / 最大 %d / 最小 %d / 噪声 %d"
          % (stat["clusters"], stat["faces"], stat["largest"],
             stat["smallest"], stat["noise"]))
    print("排序=簇大小降序，同大小按 clusterCode（保证翻页不跳）")
    for one in reviewQueue.clusterOverview(limit=args.limit or 15,
                                           minSize=args.min_size):
        print("  %-22s n=%-3d 代表 %-34s det=%-7s 桶 %s"
              % (one["clusterCode"], one["size"], one["representativeFaceCode"],
                 "-" if one["representativeDetScore"] is None
                 else "%.4f" % one["representativeDetScore"],
                 one["shotBucket"] or "(无)"))
    if args.detail > 1:
        print()
        for one in reviewQueue.clusterOverview(limit=args.detail):
            print("  %s n=%d" % (one["clusterCode"], one["size"]))
            for faceCode in (one["faceCodes"] or []):
                print("      %s" % faceCode)
    return 0


def cmdMembers(args) -> int:
    _ensureDb(args.db)
    info = reviewQueue.clusterMembers(args.members, limit=args.limit or 0)
    print("簇          : %s（**现查，勿缓存**）" % info["clusterCode"])
    print("成员        : %d 张%s" % (info["size"],
                                     "（明细已截断）" if info["truncated"] else ""))
    for one in info["members"]:
        print("  %-34s 分%-10s det=%-7s 桶 %-10s 归属 %s"
              % (one["faceCode"], one["photoCode"],
                 "-" if one["detScore"] is None else "%.4f" % one["detScore"],
                 one["shotBucket"] or "(无)", one["personCode"] or "(未归属)"))
    if not info["size"]:
        print("  （这个 clusterCode 现在查不到成员 —— 它是**内容指纹**，"
              "簇成员变了就是另一个编码了，见 queue.py「四之二」的说明）")
    return 0


# ============================================================
# main
# ============================================================

def buildParser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="photo-browser 未归类人脸聚类 + 两个队列（步骤 7）")
    p.add_argument("--db", default="", help="库文件（默认正式库；给了就只动那个库）")
    p.add_argument("--eps", type=float, default=None,
                   help="余弦距离阈值（缺省 %s；= 相似度 >= %.2f 算邻居）"
                        % (basicSettings.DBSCAN_EPS, 1.0 - basicSettings.DBSCAN_EPS))
    p.add_argument("--min-samples", type=int, default=None, dest="min_samples",
                   help="核心点最少样本数，含自己（缺省 %s）"
                        % basicSettings.DBSCAN_MIN_SAMPLES)
    p.add_argument("--backend", default="",
                   choices=list(basicSettings.DBSCAN_BACKEND_ALL),
                   help="聚类后端（缺省 %s = 装了 sklearn 用它，否则 numpy）"
                        % basicSettings.DBSCAN_BACKEND)
    p.add_argument("--limit", type=int, default=0, help="最多处理多少张脸（0 = 全部）")
    p.add_argument("--offset", type=int, default=0, help="队列起始偏移")
    p.add_argument("--window", action="store_true",
                   help="--queues 按 recID 取窗口（缺省按 %s 倒序，"
                        "分页语义才是全局正确的）" % basicSettings.QUEUE_ORDER_BY)
    p.add_argument("--detail", type=int, default=0, help="打印簇明细（>1 时连成员 faceCode 也打）")
    p.add_argument("--min-size", type=int, default=0, dest="min_size",
                   help="--clusters 只列 size >= 该值的簇（0 = 不限）")
    p.add_argument("--top", type=int, default=0, help="contact sheet 只拼前 N 个簇（0 = 全部）")
    p.add_argument("--cells", type=int, default=6, help="每张拼图放几张脸（缺省 6）")
    p.add_argument("--dry-run", action="store_true", help="只报不写")
    p.add_argument("--rebuild", action="store_true",
                   help="先清空全表 clusterCode 再重算（**会碰已归类的行**）")
    p.add_argument("--contact-sheet", action="store_true", dest="contact_sheet",
                   help="每簇拼一张 jpg 到 <thumb>\\cluster\\ 供肉眼抽查")
    p.add_argument("--status", action="store_true", help="看现状")
    p.add_argument("--queues", action="store_true", help="两个队列的条数与待确认内容")
    p.add_argument("--disputed", action="store_true", help="只看「我不同意」列表")
    p.add_argument("--clusters", action="store_true",
                   help="簇概览（按簇大小降序 + 代表样本，步骤 11 的批量入口）")
    p.add_argument("--members", default="",
                   help="查某个簇的活成员（clusterCode，**现查不缓存**）")
    p.add_argument("--explain", action="store_true", help="扫一遍不同 eps 的簇数")
    p.add_argument("--propose", action="store_true",
                   help="簇 -> 候选人物建议（只按年龄排除） + verify 交叉核对")
    return p


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv)
    print("cluster_cli.py _VERSION:", _VERSION)
    if args.status:
        return cmdStatus(args)
    if args.queues:
        return cmdQueues(args)
    if args.disputed:
        return cmdDisputed(args)
    if args.clusters:
        return cmdClusters(args)
    if args.members:
        return cmdMembers(args)
    if args.propose:
        return cmdPropose(args)
    if args.explain:
        return cmdExplain(args)
    return cmdRun(args)


if __name__ == "__main__":
    sys.exit(main())
