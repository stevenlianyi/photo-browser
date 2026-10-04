#! /usr/bin/env python3
#encoding: utf-8

# Filename: gen_thumbs.py
# Description: photo-browser 缩略图批量生成（步骤 4）
#
# 用法
# ----
#   # 给库里全部照片生成 400px 缩略图，8 进程
#   python code\src\tools\gen_thumbs.py --size 400 --workers 8
#
#   # 只补缺失的（断点续跑语义，幂等；顺手清掉残留 .tmp）
#   python code\src\tools\gen_thumbs.py --size 400 --resume
#
#   # 先只跑 100 张看看效果
#   python code\src\tools\gen_thumbs.py --size 400 --limit 100
#
#   # 三个尺寸一起出（前端切尺寸就不用现场生成）
#   python code\src\tools\gen_thumbs.py --size 200 400 800 --workers 8
#
#   # 质量改了要全量重生成
#   python code\src\tools\gen_thumbs.py --size 400 --rebuild
#
#   # 只看看现状，不生成
#   python code\src\tools\gen_thumbs.py --status
#
# 指到临时库/临时目录做实验（绝不动正式库）
#   python code\src\tools\gen_thumbs.py --db d:\tmp\test.db --root d:\tmp\photos --thumb d:\tmp\thumbs
#
# 硬约束
# ------
#   * **photo 目录只读**：本工具只读原图，所有产物落thumb\；
#   * **批量一律进程池**（CPU 解码密集，见 thumbMaker 里的取舍说明）；
#   * 缩略图**不入库**，路径由 fileHash + size 推导；
#   * 缺库自动建表（走 tools.build_db，幂等），不手写一条 DDL；
#   * 单张失败不中断整轮，失败清单最后统一打印。

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
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.media import thumbMaker as thumbMaker                  # noqa: E402
from processor.media import thumbStore as thumbStore                  # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("genThumbs", "genthumbs.log")


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


def _parseSizes(values) -> list:
    """--size 200 400 800 / --size 400 -> [200, 400, 800]（保持给定顺序，去重）"""
    sizes = []
    for one in (values or [thumbStore.THUMB_DEFAULT_SIZE]):
        for token in str(one).replace(",", " ").split():
            try:
                width = int(token)
            except ValueError:
                continue
            if width in thumbStore.THUMB_SIZES and width not in sizes:
                sizes.append(width)
    if not sizes:
        raise SystemExit("--size 必须是 %s 之一" % (list(thumbStore.THUMB_SIZES),))
    return sizes


def _cleanTmp(thumbRoot: str) -> int:
    """清掉残留的 *.tmp（中断后自愈；.tmp 本来就永远不该存在）"""
    found = thumbStore.findTmpFiles(thumbRoot)
    for one in found:
        try:
            os.remove(one)
            _LOG.warning("gen_thumbs 清理残留临时文件: %s" % one)
        except OSError as e:
            _LOG.error("gen_thumbs 清理失败 %s: %s" % (one, e))
    return len(found)


def _queryRows(dbFile: str, limit: int = 0, offset: int = 0,
               orderDesc: bool = False) -> list:
    """按 recID 取 pb_photo 行（mode='light' 剔除 BLOB 列，10 万行也不涨内存）"""
    return sqliteCommon.query_pb_photo("pb_photo", delFlag="0", mode="light",
                                      orderBy="recID", descFlag=orderDesc,
                                      limitNum=int(limit or 0), offsetNum=int(offset or 0))


def _progressPrinter(quiet: bool, total: int):
    """返回一个 onProgress 回调：每 50 张打一行（10 万张不许刷屏，开发计划 §6.4）"""
    state = {"last": 0.0}

    def _onProgress(done: int, totalCount: int, one: dict) -> None:
        if quiet:
            return
        now = time.time()
        if done == totalCount or now - state["last"] >= 1.0:
            state["last"] = now
            rate = done / max(0.001, now - _START[0])
            print("  进度 %6d/%-6d  %5.1f 张/秒" % (done, totalCount, rate), flush=True)

    _START = [time.time()]
    return _onProgress


def _printSummary(summaries: dict, sizes: list, elapsed: float, thumbRoot: str) -> None:
    print("")
    print("---- 本次生成汇总 ----")
    for size in sizes:
        one = summaries.get(size) or {}
        # 失败里把"原图坏了"单独拎出来：看到"失败 9 张"没信息量，
        # 看到"其中 9 张是原图解不出像素"才知道要不要去重新拷贝一次
        tail = ""
        if one.get("failedDecode"):
            tail = "（其中原图损坏 %d）" % one["failedDecode"]
        print("  %dpx : 共 %d | 新生成 %d | 命中 %d | 失败 %d%s | %s | %d 进程 | %.1fs"
              % (size, one.get("total", 0), one.get("created", 0), one.get("cached", 0),
                 one.get("failed", 0), tail, misc.humanSize(one.get("bytesTotal", 0)),
                 one.get("workers", 0), one.get("elapsed", 0.0)))
    stat = thumbStore.thumbStats(thumbRoot=thumbRoot)
    print("  落盘 : %s" % stat["thumbRoot"])
    print("         文件 %d 个 / 分桶 %d 个 / 单桶最多 %d 个（桶 %s）"
          % (stat["files"], stat["buckets"], stat["maxPerBucket"], stat["maxBucketName"] or "-"))
    print("         总体积 %s" % misc.humanSize(stat["bytes"]))
    print("  残留 : *.tmp %d 个（**必须是 0**）" % len(thumbStore.findTmpFiles(thumbRoot)))
    print("  用时 : %.1f 秒" % elapsed)
    failures = []
    for size in sizes:
        for one in (summaries.get(size) or {}).get("failures", []):
            failures.append((size, one))
    if failures:
        print("  失败明细（前 10 条）:")
        for size, one in failures[:10]:
            print("    [%dpx] %s [%s] -> %s"
                  % (size, one.get("photoCode") or one.get("relPath"),
                     one.get("errCode") or "-", one.get("errMsg")))
        # 批量重试对"原图坏了"毫无意义，明确说清楚免得人反复重跑
        broken = [f for f in failures if f[1].get("errCode") == thumbMaker.ERR_DECODE]
        if broken:
            print("  ↑ 标为 [%s] 的 %d 张是**原图解不出像素**（截断/全零/扩展名骗人），"
                  % (thumbMaker.ERR_DECODE, len(broken)))
            print("    重跑多少次都一样；要让它们出图只能从原始设备重新拷贝文件。")


def cmd_gen(args) -> int:
    sizes = _parseSizes(args.size)
    dbFile = _ensureDb(args.db, quiet=args.quiet)
    photoRoot = os.path.abspath(args.root or paths.photo_dir())
    thumbRoot = os.path.abspath(args.thumb or paths.thumb_dir())

    if not os.path.isdir(photoRoot):
        print("[Error] 原图根不存在或不是目录: %s" % photoRoot, file=sys.stderr)
        return 1

    if not args.quiet:
        print("==== photo-browser 缩略图批量生成 ====")
        print("  原图根 : %s（**只读**）" % photoRoot)
        print("  缩略图 : %s" % thumbRoot)
        print("  库文件 : %s" % dbFile)
        print("  尺寸   : %s（WebP quality=%d method=%d）"
              % ("/".join(str(s) for s in sizes), basicSettings.THUMB_QUALITY,
                 basicSettings.THUMB_WEBP_METHOD))
        print("  模式   : %s" % ("**全量重生成**" if args.rebuild else "只补缺失（幂等续跑）"))
        print("")

    cleaned = _cleanTmp(thumbRoot)
    if cleaned and not args.quiet:
        print("[清理] 残留 *.tmp %d 个已删除" % cleaned)

    rows = _queryRows(dbFile, limit=args.limit, offset=args.offset,
                      orderDesc=bool(args.reverse))
    if not rows:
        print("库里没有照片（先跑 tools\\scan_cli.py scan）。")
        return 1
    if not args.quiet:
        print("待处理 %d 张（offset=%d limit=%s）"
              % (len(rows), args.offset, args.limit or "不限"))

    start = time.time()
    summaries = {}
    for size in sizes:
        if not args.quiet:
            print("---- %dpx ----" % size)
        summaries[size] = thumbMaker.make_thumbs_bulk(
            rows, workers=args.workers, size=size,
            photoRoot=photoRoot, thumbRoot=thumbRoot, force=bool(args.rebuild),
            chunkSize=args.chunk, onProgress=_progressPrinter(args.quiet, len(rows)))
    elapsed = time.time() - start
    if not args.quiet:
        _printSummary(summaries, sizes, elapsed, thumbRoot)

    totalFailed = sum((summaries.get(s) or {}).get("failed", 0) for s in sizes)
    return 1 if totalFailed and args.strict else 0


def cmd_status(args) -> int:
    """只统计现状，不生成"""
    root = os.path.abspath(args.thumb or paths.thumb_dir())
    stat = thumbStore.thumbStats(thumbRoot=root)
    print("==== 缩略图现状 ====")
    print("  目录   : %s（存在=%s）" % (stat["thumbRoot"], stat["exists"]))
    print("  文件   : %d 个 / 总体积 %s" % (stat["files"], misc.humanSize(stat["bytes"])))
    print("  分桶   : %d 个，单桶最多 %d 个（桶 %s）"
          % (stat["buckets"], stat["maxPerBucket"], stat["maxBucketName"] or "-"))
    print("  人脸桶 : %d 个" % len(thumbStore.listBucketDirs(thumbStore.FACE_SUBDIR, root)))
    print("  残留   : *.tmp %d 个" % len(thumbStore.findTmpFiles(root)))
    print("  计数   : %s" % misc.jsonDumps(thumbMaker.countersSnapshot(), ensure_ascii=False))
    return 0


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_thumbs",
        description="photo-browser 缩略图批量生成（进程池 / 原子写 / 路径可推导）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command")

    def addCommon(target):
        target.add_argument("--db", default=None, help="库文件（缺省 paths.db_file()）")
        target.add_argument("--root", default=None,
                            help="原图根（缺省 paths.photo_dir()，**只读**）")
        target.add_argument("--thumb", default=None, help="缩略图根（缺省 paths.thumb_dir()）")
        target.add_argument("--quiet", action="store_true", help="少打印")

    pGen = sub.add_parser("gen", help="生成缩略图")
    addCommon(pGen)
    pGen.add_argument("--size", nargs="+", default=[str(thumbStore.THUMB_DEFAULT_SIZE)],
                      help="目标宽，可给多个：--size 200 400 800（合法值 %s）"
                           % (list(thumbStore.THUMB_SIZES),))
    pGen.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1),
                      help="进程数（缺省 CPU-1）")
    pGen.add_argument("--limit", type=int, default=0, help="只处理前 N 张（0=全部）")
    pGen.add_argument("--offset", type=int, default=0, help="跳过前 N 张（分页用）")
    pGen.add_argument("--reverse", action="store_true", help="按 recID 倒序取")
    pGen.add_argument("--chunk", type=int, default=None,
                      help="每个子进程任务携带多少张（缺省 %d）" % basicSettings.THUMB_BULK_CHUNK)
    pGen.add_argument("--resume", action="store_true",
                      help="断点续跑：清残留 .tmp + 只补缺失（默认即此行为）")
    pGen.add_argument("--rebuild", action="store_true", help="**忽略已有，全部重生成**")
    pGen.add_argument("--strict", action="store_true", help="有失败即返回非 0 退出码")
    pGen.set_defaults(func=cmd_gen)

    pStatus = sub.add_parser("status", help="只看现状（不生成）")
    addCommon(pStatus)
    pStatus.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    _fixConsole()
    parser = buildParser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    print("gen_thumbs _VERSION: %s" % _VERSION)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[中断] 已停止。磁盘上不会留下 *.tmp 半文件"
              "（中断时正在写的那个 .tmp 可能残留，下次跑会自动清）。", file=sys.stderr)
        return 130
    except paths.PathLayoutError as e:
        print("[PathLayoutError] %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        print("[Error] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        _LOG.error("gen_thumbs 异常: %s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
