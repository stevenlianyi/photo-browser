#! /usr/bin/env python3
#encoding: utf-8

# Filename: scan_cli.py
# Description: photo-browser 扫描器命令行入口（步骤 3）
#
# 用法
# ----
#   # 扫描默认根目录（paths.photo_dir()），跑到完
#   python code\src\tools\scan_cli.py scan
#
#   # 指定根 + 每批 100 张（跑完一批就 PAUSED，等你敲回车继续下一批）
#   python code\src\tools\scan_cli.py scan --root d:\PhotoLib\photo --batch-size 100
#
#   # 后台一次跑完（不等交互）
#   python code\src\tools\scan_cli.py scan --all
#
#   # 断点续扫：拿上次那个 jobCode 接着扫
#   python code\src\tools\scan_cli.py scan --resume SJ_20261004120000_ab12cd --all
#
#   # 看状态 / 看任务列表 / 只数一下有多少张
#   python code\src\tools\scan_cli.py status --resume SJ_...
#   python code\src\tools\scan_cli.py list
#   python code\src\tools\scan_cli.py count --root d:\PhotoLib\photo
#
#   # 指到临时库做实验（绝不动正式库）
#   python code\src\tools\scan_cli.py scan --db d:\tmp\test.db --root d:\tmp\photos --all
#
# 硬约束
# ------
#   * photo 目录**只读**：本工具只 stat / 读文件，**不写、不删、不改名**；
#   * 缺库自动建表（走 tools.build_db，幂等），不手写一条 DDL；
#   * 单写入者：同一时刻只跑一个任务，已在跑会直接报错退出而不是排队。

import argparse
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                                  # noqa: E402
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402
from processor.scanner import walker as walker                        # noqa: E402
from schedule import scanScheduler as scanScheduler              # noqa: E402
from tools import build_db as build_db                                # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("scanCli", "scancli.log")


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
    sqliteOk = True
    try:
        from database.auto_generated import sqliteCommon as sqliteCommon
        sqliteCommon.dbHandle(target)
        sqliteOk = sqliteCommon.chkTableExist("pb_photo")
    except Exception:
        sqliteOk = False
    if not sqliteOk:
        if not quiet:
            print("[建库] 表不存在，先建库: %s" % target)
        build_db.build(dbFile=target, verbose=not quiet)
    return target


def _printBanner(root: str, dbFile: str, batchSize: int) -> None:
    print("==== photo-browser 扫描器 ====")
    print("  扫描根 : %s（只读，绝不写入）" % root)
    print("  库文件 : %s" % dbFile)
    print("  批大小 : %d 张/ 批（跑满即 PAUSED，等「继续下一批」）" % batchSize)
    print("  事务   : %d 行/ 次 executemany" % basicSettings.COMMIT_ROWS_PER_TXN)
    print("  hash   : %s / %d MB 分块"
          % (basicSettings.HASH_ALGORITHM, basicSettings.HASH_CHUNK_SIZE // 1024 // 1024))
    print("")


def _errPathExit(message: str) -> int:
    """统一的「路径不存在」退出码（与 globalDefinition.ERR_PATH_NOT_FOUND 对齐）"""
    from common import globalDefinition as comGD
    sys.stderr.write("[Error] %s（%s=%d）\n"
                     % (message, comGD.errText(comGD.ERR_PATH_NOT_FOUND),
                        comGD.ERR_PATH_NOT_FOUND))
    return 1


def cmd_scan(args) -> int:
    """扫描主命令"""
    dbFile = _ensureDb(args.db, quiet=args.quiet)
    root = os.path.abspath(args.root or paths.photo_dir())
    if not os.path.isdir(root):
        return _errPathExit("扫描根不存在或不是目录: %s" % root)

    sched = scanScheduler.ScanScheduler(dbFile=dbFile, ownerID=args.owner)
    if not args.quiet:
        _printBanner(root, dbFile, args.batch_size or basicSettings.BATCH_SIZE)

    # ---- 1) 拿任务 ----
    if args.resume:
        job = sched.getJob(args.resume)
        if job is None:
            print("[Error] --resume 的 jobCode 不存在: %s" % args.resume, file=sys.stderr)
            return 2
        if str(job.get("jobStatus")) == "DONE":
            print("任务已是DONE，无需续扫: %s" % args.resume)
            print(sched.describe(args.resume)["text"])
            return 0
        jobCode = args.resume
        if not args.quiet:
            print("续扫任务: %s" % jobCode)
    else:
        job = sched.createJob(root=root, batchSize=args.batch_size,
                              jobCode=args.job_code, countTotal=True)
        jobCode = job["jobCode"]
        if not args.quiet:
            print("新建任务: %s（发现 %s 个文件）" % (jobCode, job.get("totalCount")))

    # ---- 2) 跑 ----
    if args.all:
        summary = sched.runUntilDone(jobCode, maxBatches=args.max_batches,
                                     batchSize=args.batch_size)
        if not args.quiet:
            _printSummary(sched, jobCode, summary)
        if not summary.get("done") and summary.get("errMsg") \
                and "maxBatches" not in summary["errMsg"]:
            print("[Error] 扫描未完成: %s" % summary["errMsg"], file=sys.stderr)
            return 1
        return 0

    # 交互模式：跑一批 -> PAUSED -> 等回车继续，直到 DONE
    print("交互模式：每批 %d 张，跑完自动 PAUSED，回车继续下一批，Ctrl+C 退出"
          % (args.batch_size or basicSettings.BATCH_SIZE))
    index = 0
    while True:
        batchResult = sched.runBatch(jobCode, batchSize=args.batch_size)
        if not batchResult.get("ok"):
            print("[Error] 批次失败: %s" % batchResult.get("errMsg"), file=sys.stderr)
            print(sched.describe(jobCode)["text"])
            return 1
        result = batchResult.get("result") or {}
        index += 1
        print("第 %d 批: %s | 累计 %s" % (index, sched.describe(jobCode)["text"],
                                        result.get("counts")))
        if batchResult.get("done"):
            print("全部完成。")
            break
        try:
            input("  已 PAUSED，回车继续下一批（Ctrl+C 退出，稍后用 --resume 续扫）...")
        except (EOFError, KeyboardInterrupt):
            print("\n已停在批边界。续扫："
                  "python code\\src\\tools\\scan_cli.py scan --resume %s" % jobCode)
            break
    if not args.quiet:
        _printSummary(sched, jobCode, None)
    return 0


def _printSummary(sched, jobCode: str, summary) -> None:
    print("")
    print("---- 本次运行汇总 ----")
    if summary is not None:
        print("  批次      : %d" % summary.get("batches", 0))
        print("  用时      : %s 秒" % summary.get("elapsed", "?"))
        print("  索引分页  : %d 行/页（10 万行实测：峰值内存 157MB -> 3MB）"
              % basicSettings.SCAN_INDEX_PAGE)
        counts = summary.get("counts") or {}
        print("  新增      : %s" % counts.get("added", 0))
        print("  跳过(幂等): %s" % counts.get("skipped", 0))
        print("  内容变更  : %s" % counts.get("updated", 0))
        print("  重复      : %s（疑似移动/重命名 %s，纯复制 %s）"
              % (counts.get("duplicate", 0), counts.get("moved", 0),
                 counts.get("copied", 0)))
        print("  移动回链  : %s 条旧记录已写 movedToPhotoCode（待用户确认，路径未改）"
              % counts.get("moveLinked", 0))
        print("  标缺失    : %s（回来了 %s，**记录一律没删**）"
              % (counts.get("missing", 0), counts.get("recovered", 0)))
    print("  " + sched.describe(jobCode)["text"])


def cmd_status(args) -> int:
    """看任务状态"""
    if not args.resume:
        rows = scanScheduler.ScanScheduler(dbFile=args.db).listJobs(5)
        if not rows:
            print("还没有任何扫描任务。")
        for job in rows:
            print("  %s" % job["jobCode"])
        return 0
    sched = scanScheduler.ScanScheduler(dbFile=args.db)
    info = sched.describe(args.resume)
    if not info.get("ok"):
        print("[Error] %s" % info.get("errMsg"), file=sys.stderr)
        return 2
    print(info["text"])
    return 0


def cmd_list(args) -> int:
    """任务列表"""
    sched = scanScheduler.ScanScheduler(dbFile=args.db)
    rows = sched.listJobs(args.limit)
    if not rows:
        print("还没有任何扫描任务。")
        return 0
    print("%-26s %-8s %7s %7s %7s %7s %-20s" %
          ("jobCode", "状态", "总数", "已处理", "新增", "跳过", "最后游标"))
    for job in rows:
        print("%-26s %-8s %7s %7s %7s %7s %-20s" % (
            job.get("jobCode"), job.get("jobStatus"), job.get("totalCount"),
            job.get("processedCount"), job.get("addedCount"), job.get("skippedCount"),
            (job.get("lastCursor") or "(无)")[:20]))
    return 0


def cmd_count(args) -> int:
    """只数一下有多少张（不 hash、不入库），顺带看看排除目录与白名单生效没"""
    root = os.path.abspath(args.root or paths.photo_dir())
    if not os.path.isdir(root):
        return _errPathExit("扫描根不存在: %s" % root)
    start = time.time()
    items = walker.listPhotoFiles(root)
    totalSize = sum(os.path.getsize(item[1]) for item in items)
    print("扫描根      : %s" % root)
    print("照片文件数  : %d（耗时 %.2fs，只 stat 未读内容）" % (len(items), time.time() - start))
    print("总体积      : %s" % misc.humanSize(totalSize))
    print("扩展名白名单: %d 种" % len(basicSettings.PHOTO_EXTS))
    print("排除目录    : %s" % ", ".join(sorted(basicSettings.EXCLUDED_DIR_NAMES)))
    for item in items[:10]:
        print("   %s" % item[0])
    if len(items) > 10:
        print("   ...（其余 %d 条省略）" % (len(items) - 10))
    return 0


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan_cli",
        description="photo-browser 扫描器（遍历 + 双 hash + EXIF + 增量入库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command")

    def addCommon(target):
        target.add_argument("--db", default=None,
                            help="库文件（缺省 paths.db_file()）")
        target.add_argument("--root", default=None,
                            help="扫描根（缺省 paths.photo_dir()，**只读**）")
        target.add_argument("--quiet", action="store_true", help="少打印")

    pScan = sub.add_parser("scan", help="执行扫描")
    addCommon(pScan)
    pScan.add_argument("--batch-size", type=int, default=None,
                       help="每批处理张数（缺省 %d）" % basicSettings.BATCH_SIZE)
    pScan.add_argument("--resume", default=None, help="续扫指定的 jobCode（从 lastCursor 接着扫）")
    pScan.add_argument("--job-code", default=None, help="自定义 jobCode（幂等键，已存在则复用）")
    pScan.add_argument("--all", action="store_true",
                       help="一次跑到 DONE（不逐批等确认）")
    pScan.add_argument("--max-batches", type=int, default=None,
                       help="--all 时最多跑几批（留着下次继续）")
    pScan.add_argument("--owner", default=None, help="写入 pb_photo.ownerID")
    pScan.set_defaults(func=cmd_scan)

    pStatus = sub.add_parser("status", help="看任务状态")
    addCommon(pStatus)
    pStatus.add_argument("--resume", default=None, help="jobCode（不给则列最近 5 个）")
    pStatus.set_defaults(func=cmd_status)

    pList = sub.add_parser("list", help="任务列表")
    addCommon(pList)
    pList.add_argument("--limit", type=int, default=20)
    pList.set_defaults(func=cmd_list)

    pCount = sub.add_parser("count", help="只数文件数（不 hash 不入库）")
    addCommon(pCount)
    pCount.set_defaults(func=cmd_count)
    return parser


def main(argv=None) -> int:
    _fixConsole()
    parser = buildParser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    print("scan_cli _VERSION: %s" % _VERSION)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[中断] 已在批边界停下，任务保持可续扫状态。", file=sys.stderr)
        return 130
    except paths.PathLayoutError as e:
        print("[PathLayoutError] %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        print("[Error] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        _LOG.error("scan_cli 异常: %s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
