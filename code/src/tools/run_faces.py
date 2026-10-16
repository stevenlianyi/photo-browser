#! /usr/bin/env python3
#encoding: utf-8
#Filename: run_faces.py
#Description: photo-browser 人脸特征批量提取与落库（步骤 5 执行入口）
#
# 用法
# ----
#   # 给库里 scanState=0 的照片提取人脸并落库
#   python code\src\tools\run_faces.py
#
#   # 先跑 50 张看看
#   python code\src\tools\run_faces.py --limit 50
#
#   # 全量重提取（先删旧人脸行再插；换检测模型或修 bug 后用）
#   python code\src\tools\run_faces.py --replace
#
#   # R17：短边门槛放宽后重跑「库里还没有人脸」的老照片（**增量补充，不删旧行**）
#   #   短边>=1280 的大图门槛没变，所以用 --max-short-edge 1280 只打受益的那批
#   #   （实测 faceCount=0 里 7137 张短边<1280、11296 张 >=2048 无收益）
#   python code\src\tools\run_faces.py --only-empty-face --max-short-edge 1280 --limit 200
#
#   # R17 第二批：**增量补漏** —— 门槛放松后把「已经有脸的」老照片里漏掉的配角脸补上
#   #   （不删旧行、保留人工确认；这批占大头，短边<1600 的有 1.3 万张）
#   python code\src\tools\run_faces.py --refresh-small --max-short-edge 1600 --limit 500
#
#   # 指到临时库做实验（绝不动正式库）
#   python code\src\tools\run_faces.py --db d:\tmp\test.db --root d:\tmp\photos
#
#   # 只看看现状，不提取
#   python code\src\tools\run_faces.py --status
#
# 硬约束
# ------
#   * photo 目录只读；产物只有 pb_face 行、pb_photo 的 faceCount/scanState、人脸裁剪图
#   * 子进程只算、主进程单线程批量写库（单写入者，见 engine/face/pool.py 文件头）
#   * 幂等：同一张照片重跑 -> 同一 faceCode -> upsert，不产生重复行
#   * 单张失败不中断整轮，失败清单最后统一打印

import argparse
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                          # noqa: E402
from common import miscCommon as misc                                  # noqa: E402
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                      # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon      # noqa: E402
from engine.face import pool as facePool                               # noqa: E402
from engine.face import faceStore as faceStore                         # noqa: E402
from processor.media import thumbStore as thumbStore                  # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("runFaces", "runfaces.log")


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _ensureDb(dbFile: str = None, quiet: bool = False) -> str:
    """库/表不存在就建（幂等，走 tools.build_db，本文件不写一条 DDL）"""
    target = os.path.abspath(dbFile or paths.db_file())
    ok = True
    try:
        sqliteCommon.dbHandle(target)
        ok = sqliteCommon.chkTableExist("pb_photo")
    except Exception:
        ok = False
    if not ok:
        if not quiet:
            print("[建库] 表不存在，先建库: %s" % target)
        from tools import build_db as build_db
        build_db.build(dbFile=target, verbose=not quiet)
    return target


def _faceDirStat(thumbRoot: str) -> dict:
    """人脸图目录统计（thumbStats 只管 thumbs 子目录，faces 这里自己数）"""
    root = os.path.join(thumbRoot, thumbStore.FACE_SUBDIR)
    stat = {"root": root, "files": 0, "bytes": 0, "buckets": 0, "maxPerBucket": 0}
    if not os.path.isdir(root):
        return stat
    for name in sorted(os.listdir(root)):
        bucketDir = os.path.join(root, name)
        if not os.path.isdir(bucketDir):
            continue
        stat["buckets"] += 1
        count = 0
        for entry in os.scandir(bucketDir):
            if entry.is_file(follow_symlinks=False):
                count += 1
                try:
                    stat["bytes"] += int(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    pass
        stat["files"] += count
        stat["maxPerBucket"] = max(stat["maxPerBucket"], count)
    return stat


def _printStatus(dbFile: str) -> None:
    """现状：pb_photo 的 scanState 分布、pb_face 条数、人脸图落盘情况"""
    sqliteCommon.dbHandle(dbFile)
    print("=" * 68)
    print("现状  |  库 %s" % dbFile)
    print("=" * 68)
    rows = sqliteCommon.query_pb_photo("pb_photo", mode="light")
    dist = {}
    for row in rows:
        key = int(row.get("scanState") or 0)
        dist[key] = dist.get(key, 0) + 1
    print("pb_photo 共 %d 行，scanState 分布:" % len(rows))
    for state in sorted(dist):
        print("   %d %-10s %6d 张"
              % (state, comGD.SCAN_STATE_TEXT.get(state, "?"), dist[state]))
    faces = sqliteCommon.query_pb_face("pb_face", mode="light")
    print("pb_face  共 %d 行" % len(faces))
    withPerson = len([f for f in faces if f.get("personCode")])
    print("   其中已归属 personCode: %d（步骤 6 之前应为 0）" % withPerson)
    stat = _faceDirStat(paths.thumb_dir())
    print("thumb 根 %s" % paths.thumb_dir())
    print("   人脸图 %d 个 / %d 个分桶 / 单桶最多 %d / 共 %s"
          % (stat["files"], stat["buckets"], stat["maxPerBucket"],
             misc.humanSize(stat["bytes"])))
    bad = faceStore.countPhotoMismatch(dbFile, limit=300)
    print("抽查 300 张：faceCount 与实际条数不一致 %d 张" % len(bad))
    for one in bad[:10]:
        print("   %s  faceCount=%d 实际=%d  %s"
              % (one["photoCode"], one["faceCount"], one["realCount"], one["relPath"]))


def _run(args) -> int:
    dbFile = _ensureDb(args.db, quiet=args.quiet)
    if args.status:
        _printStatus(dbFile)
        return 0
    if dbFile:
        sqliteCommon.dbHandle(dbFile)              # DR-10：显式切库
    photoRoot = os.path.abspath(args.root or paths.photo_dir())
    thumbRoot = os.path.abspath(args.thumb or paths.thumb_dir())

    onlyPending = ((not args.all) and not args.only_empty_face
                   and not args.refresh_small)
    if args.only_empty_face or args.refresh_small:
        scope = ("faceCount=0 且 isMissing=0（R17 重跑老照片；**不看 scanState**）"
                 if args.only_empty_face else
                 "短边<%dpx 的**所有**照片（增量补漏，含已有脸的；不看 scanState）"
                 % (args.max_short_edge or 0))
        if args.max_short_edge:
            scope += " 且 短边<%dpx" % args.max_short_edge
    elif onlyPending:
        scope = "scanState=0（待扫描）"
    else:
        scope = "全部照片"
    print("=" * 68)
    print("人脸特征提取  |  库 %s" % dbFile)
    print("  photo=%s  thumb=%s" % (photoRoot, thumbRoot))
    print("  取数范围: %s   workers=%s   replace=%s"
          % (scope, args.workers or "auto", bool(args.replace)))
    if args.only_empty_face or args.refresh_small:
        # 这几句必须在**开跑之前**说：写正式库是不可逆的，而"和界面任务抢写锁"
        # 恰恰是这类批处理最容易踩的坑（现象是跑到一半 database is locked）
        print("  ⚠️ 本批会写库（插入/更新人脸行 + 更新 faceCount/scanState）：")
        print("     建议先停掉正在运行的服务，避免与界面上的扫描/认脸任务抢写锁。")
    if args.only_empty_face:
        print("     这些照片本来就没有人脸行，--replace 对它们没有实际影响。")
    elif args.refresh_small:
        print("     ⚠️ 增量补漏**不要加 --replace**：那会先删掉这张照片上已人工确认的"
              "脸；不带它时旧脸按 faceCode 原样 upsert（isConfirmed 不动）。")
    print("  质量阈值: detScore>=%s  短边>=%s  |yaw|<=%s"
          % (args.min_det,
             ("%spx（固定）" % args.min_edge) if args.min_edge
             else ("%spx + 小图按比例自适应" % basicSettings.MIN_FACE_EDGE
                   if not args.no_adaptive_edge else "%spx（固定）"
                   % basicSettings.MIN_FACE_EDGE),
             args.max_yaw))
    print("=" * 68)

    rows = faceStore.loadPhotoRows(dbFile=dbFile, limit=args.limit,
                                   offset=args.offset, onlyPending=onlyPending,
                                   photoRoot=photoRoot,
                                   orderBy="recID", descFlag=args.reverse,
                                   onlyEmptyFace=args.only_empty_face,
                                   maxShortEdge=args.max_short_edge,
                                   minRecID=args.min_rec_id,
                                   refreshSmall=args.refresh_small)
    print("待处理照片: %d 张" % len(rows))
    if not rows:
        print("没有待处理照片（换个 --all 或 --offset 试试）")
        return 0

    # 子进程不连库的自查先跑一遍：万一 engine/pool 里被谁加了 import，
    # 这时候就该炸，而不是等到跑了 3000 张才炸
    facePool.assertNoDatabaseImport()
    print("子进程连库自查: 通过（engine.py / pool.py 无数据库 import）")

    state = {"last": 0.0, "t0": time.time()}

    def _progress(done, total, result):
        now = time.time()
        if now - state["last"] < 1.0 and done != total:
            return
        state["last"] = now
        rate = done / max(now - state["t0"], 1e-6)
        print("   进度 %d/%d  %.1f 张/秒  剩余约 %.0f 秒"
              % (done, total, rate, (total - done) / max(rate, 1e-6)))

    engineKwargs = {"minDetScore": args.min_det, "maxYaw": args.max_yaw,
                    "modelPack": args.model,
                    "detSize": (args.det_size, args.det_size),
                    # 小图的短边门槛按比例放宽（1280x960 -> 48px），大图不变；
                    # --no-adaptive-edge 时退回"固定 64px"的老口径
                    "adaptiveMinEdge": (False if args.no_adaptive_edge else None),
                    "wantCrop": not args.no_crop,
                    # 被丢弃的人脸明细默认回传（pool.DEFAULT_ENGINE_KWARGS 已为 True），
                    # 这里不覆盖：收集是常开的，开关只控制"要不要打印"
                    "reportRejected": True}
    # ⚠️ 只在用户**显式**给了 --min-edge 时才传：传了就是固定门槛、
    #    自适应失效（见 engine.effectiveMinFaceEdge 的①）。缺省必须不传。
    if args.min_edge:
        engineKwargs["minFaceEdge"] = int(args.min_edge)
    out = faceStore.extractAndStore(
        rows, workers=args.workers, dbFile=dbFile,
        photoRoot=photoRoot, thumbRoot=thumbRoot,
        engineKwargs=engineKwargs,
        replaceFaces=args.replace, onProgress=_progress)

    pool = out["pool"]
    store = out["store"]
    timing = out["timing"]
    print("\n---- 本次运行汇总 ----")
    print("照片 %d 张   检出原始人脸 %d 张   入库 %d 张   无有效人脸 %d 张"
          % (pool["images"], pool["rawFaces"], pool["kept"], pool["noFace"]))
    if pool["dropped"]:
        print("质量过滤丢弃: " + "  ".join("%s=%d" % (k, v)
                                          for k, v in sorted(pool["dropped"].items())))
    else:
        print("质量过滤丢弃: 无")
    if args.audit and pool.get("droppedDetail"):
        print("\n---- 被丢弃人脸明细（--audit）----")
        for reason in sorted(pool["droppedDetail"]):
            one = pool["droppedDetail"][reason]
            print("  %s  %d 张" % (reason, one["count"]))
            # yaw 这一列存的是**带符号**的原值（正=脸朝右），
            # 判据用的是 |yaw| > 45，所以这里标 "yaw" 而不是 "|yaw|"
            for label, key, unit in (("detScore", "detScore", ""),
                                     ("短边", "shortEdge", "px"),
                                     ("yaw", "poseYaw", "度")):
                values = one.get(key) or []
                if values:
                    spread = "极差 %.3f" % (max(values) - min(values))
                    print("      %-9s [%.3f, %.3f]%s  %s  (n=%d)"
                          % (label, min(values), max(values), unit, spread, len(values)))
            for sample in one.get("samples") or ():
                print("      例: %-34s det=%.4f 短边=%.1f yaw=%s"
                      % (sample["file"], sample["detScore"], sample["shortEdge"],
                         sample["poseYaw"]))
        print()
    print("落库: %d 次事务 / pb_face %d 行 / pb_photo %d 行 / 删除旧行 %d"
          % (store["flushes"], store["faceRows"], store["photoRows"], store["deleted"]))
    print("人脸图: 新建 %d  命中 %d  失败 %d  -> %s\\faces\\"
          % (store["cropCreated"], store["cropCached"], store["cropFailed"],
             thumbRoot))
    print("进程池: %d 进程（%s）  墙钟 %.1f 秒" % (pool["workers"], pool["mode"],
                                                pool["wallTime"]))
    info = pool.get("engineInfo") or {}
    if info:
        print("引擎: 后端=%s  模块=%s  姿态可用=%s"
              % (info.get("backend"), info.get("loadedModules"),
                 info.get("poseReady")))
        if info.get("backendError"):
            print("  [ERROR] insightface 不可用已降级: %s" % info["backendError"])
        if not info.get("poseReady", False):
            print("  [WARN] 姿态不可用 -> yaw 过滤本轮不生效（人脸会偏多）")
    print("单张耗时(纯计算): 均值 %.3fs  p50 %.3fs  p95 %.3fs  max %.3fs  (n=%d)"
          % (timing["mean"], timing["p50"], timing["p95"], timing["max"],
             timing["count"]))
    if pool.get("workers", 0) > 1:
        print("  （workers>1 时每进程钉 1 个 ORT 线程防抢核，单张延迟随并行度变长是"
              "正常的；单张 <1s 按 workers=1 判）")
    if pool["failures"]:
        print("失败/异常 %d 类:" % len(pool["failures"]))
        for one in pool["failures"][:10]:
            print("   %s" % one)
    if store["cropFailed"]:
        print("⚠️  有 %d 张人脸图没落盘（库里的行已写入，只是头像位暂时空白）"
              % store["cropFailed"])
    return 0


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_faces.py", description="photo-browser 人脸特征提取与落库（步骤 5）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--db", default=None, help="库文件（缺省 paths.db_file()）")
    parser.add_argument("--root", default=None, help="原图根（缺省 paths.photo_dir()）")
    parser.add_argument("--thumb", default=None, help="缩略图/人脸图根（缺省 paths.thumb_dir()）")
    parser.add_argument("--workers", type=int, default=0, help="进程数（0=自动 cpu-1）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少张（0=不限）")
    parser.add_argument("--offset", type=int, default=0, help="跳过前 N 张（分页）")
    parser.add_argument("--all", action="store_true", help="连已提取过的一起重跑（不删旧行）")
    parser.add_argument("--only-empty-face", action="store_true",
                        help="只跑 faceCount=0 的照片（门槛放宽后重跑老照片用；"
                             "判据不看 scanState，故能捞到早就扫过的那些）")
    parser.add_argument("--refresh-small", action="store_true",
                        help="**增量补漏**：对短边<--max-short-edge **且已经有脸**的照片"
                             "重跑提取，把门槛放松后漏掉的配角脸补进来"
                             "（faceCount=0 的那些归 --only-empty-face，两批不重叠）。"
                             "⚠️ 不要与 --replace 同用（那会删掉已人工确认的脸）")
    parser.add_argument("--max-short-edge", type=int, default=0,
                        help="只处理短边 < Npx 的照片（0=不限）。配 --only-empty-face 用："
                             "短边门槛放宽只发生在短边 < 1280 的图上，大图重跑没有收益")
    parser.add_argument("--min-rec-id", type=int, default=0,
                        help="只处理 recID > N 的照片（**分批重跑的游标**）。"
                             "配 --only-empty-face 时**必须**用它推进：断点续跑靠它，"
                             "因为没检出人脸的照 faceCount 仍是 0、会永远留在候选集里")
    parser.add_argument("--replace", action="store_true",
                        help="重提取：先按 photoCode 删旧人脸行再插（换模型/修 bug 用）")
    parser.add_argument("--reverse", action="store_true", help="从最新的照片开始")
    parser.add_argument("--model", default="buffalo_l", help="模型包名")
    parser.add_argument("--det-size", type=int, default=640, help="检测边长")
    parser.add_argument("--min-det", type=float, default=basicSettings.MIN_DET_SCORE,
                        help="detScore 下限")
    parser.add_argument("--min-edge", type=int, default=None,
                        help="人脸框短边下限(px)。缺省不传 = %spx 且小图按比例自适应；"
                             "显式给出即固定值、自适应失效" % basicSettings.MIN_FACE_EDGE)
    parser.add_argument("--no-adaptive-edge", action="store_true",
                        help="关闭短边按图自适应：结果恒等于 %spx（老口径 / S0 对照）"
                             % basicSettings.MIN_FACE_EDGE)
    parser.add_argument("--max-yaw", type=float, default=basicSettings.MAX_YAW,
                        help="侧脸 |yaw| 上限(度)")
    parser.add_argument("--no-crop", action="store_true", help="不裁人脸图（只入库特征）")
    parser.add_argument("--audit", action="store_true",
                        help="打印被丢弃人脸的明细（每档的极值 + 3 条样本）")
    parser.add_argument("--status", action="store_true", help="只打印现状，不提取")
    parser.add_argument("--quiet", action="store_true", help="少打字")
    return parser


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv)
    print("run_faces _VERSION: %s" % _VERSION)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("\n[中断] 已提交的批次都已落库，未处理的照片下次重跑即可（幂等）")
        return 130
    except paths.PathLayoutError as e:
        print("[Error] 目录布局不合法: %s" % e)
        return 1
    except Exception as e:
        print("[Error] %s: %s" % (type(e).__name__, e))
        _LOG.error("run_faces 失败: %s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())


