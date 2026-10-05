#! /usr/bin/env python3
#encoding: utf-8

#Filename: cli.py
#Description: photo-browser 统一命令行入口（步骤 8：联系人导入）
#
# 命令
# ----
#   import-csv --file<path>CSV 导入（**主力通道**，Outlook 导出）
#   import-vcard  --path <dir|file>       vCard 导入（3.0 / 4.0，含 KIND:group 家庭组）
#   list-families                看家庭组与建议合并项
#
# 共同参数
# --------
#   --db<path>   指到临时库做实验（绝不动正式库）
#   --dry-run    只算不写（不建档案、不归档原件）
#   --owner      写入 pb_person.ownerID
#   --batch      分批提交的行数（默认 basicSettings.IMPORT_BATCH_ROWS）
#   --no-archive 不把原件复制到 <dbDir>\imports\
#
# 本步的三条硬约束
# ----------------
#   1. **只建档案，绝不关联照片**：这两个导入命令只写 pb_person /
#      pb_person_category / pb_family，照片归属由人脸识别负责（步骤 5-7）。
#   2. **半自动**：同姓多人只**建议**合并家庭组，不自动建；
#      只有通讯录里显式写了 KIND:group 的组才落 pb_family。
#   3. **原图只读**：本命令只读 CSV/vcf 文本，绝不碰 photo 目录；
#      归档是「复制」原件到 <dbDir>\imports\，用户的原文件不动。
#
# 退出码
# ------
#   0 成功（可以有 warning）
#   1 有写库失败/ 未完成
#   2 参数或路径非法（前置校验没过，**一行都没写**）
#   130 用户中断（已落库的部分仍然有效，导入是分批提交的）

import argparse
import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                                  # noqa: E402
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                      # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon      # noqa: E402
from processor.contact import contactCommon as contact                # noqa: E402
from processor.contact import csv_import as csvImport                  # noqa: E402
from processor.contact import vcard_import as vcardImport              # noqa: E402
from tools import build_db as build_db                                # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("cliMain", "climain.log")


def _ensureDb(dbFile: str = None, quiet: bool = False) -> str:
    """库/表不存在就建（幂等，走 tools.build_db；本文件不写一条 DDL）"""
    target = os.path.abspath(dbFile or paths.db_file())
    sqliteOk = True
    try:
        sqliteCommon.dbHandle(target)
        sqliteOk = sqliteCommon.chkTableExist("pb_person")
    except Exception:
        sqliteOk = False
    if not sqliteOk:
        if not quiet:
            print("[建库] 表不存在，先建库: %s" % target)
        build_db.build(dbFile=target, verbose=not quiet)
    return target


def _prepare(args) -> str:
    """统一的启动准备：落路径覆盖 -> 校验布局 -> 建可写目录 -> 显式指定库句柄"""
    dbFile = _ensureDb(args.db, quiet=getattr(args, "quiet", False))
    paths.setRootOverride(db=dbFile)
    paths.validate_layout()
    paths.ensure_dirs()
    sqliteCommon.dbHandle(dbFile)          # DR-10：必须显式
    return dbFile


def _errPathExit(message: str) -> int:
    from common import globalDefinition as comGD
    sys.stderr.write("[Error] %s（%s=%d）\n"
                     % (message, comGD.errText(comGD.ERR_PATH_NOT_FOUND),
                        comGD.ERR_PATH_NOT_FOUND))
    return 2


# ============================================================
# 一、import-csv
# ============================================================

def cmd_import_csv(args) -> int:
    """CSV（主力通道）导入"""
    src = os.path.abspath(args.file)
    if not os.path.isfile(src):
        return _errPathExit("CSV 文件不存在: %s" % src)
    dbFile = _prepare(args)
    print("==== photo-browser 联系人导入 · CSV ====")
    print("cli _VERSION: %s" % _VERSION)
    print("库文件      : %s" % dbFile)
    planned = csvImport.plan(src, dbFile)
    csvImport.report(planned)
    if args.dry_run:
        print("\n[dry-run] 未写任何一行，也未归档原件。")
        return 0
    summary = csvImport.apply(planned, ownerID=args.owner or "",
                              archive=not args.no_archive, batchRows=args.batch)
    csvImport.printSummary(summary)
    return 1 if summary["failed"] else 0


# ============================================================
# 二、import-vcard
# ============================================================

def cmd_import_vcard(args) -> int:
    """vCard（3.0 / 4.0，含 KIND:group）导入"""
    src = os.path.abspath(args.path)
    if not os.path.exists(src):
        return _errPathExit("vCard 路径不存在: %s" % src)
    dbFile = _prepare(args)
    print("==== photo-browser 联系人导入 · vCard ====")
    print("cli _VERSION: %s" % _VERSION)
    print("库文件      : %s" % dbFile)
    planned = vcardImport.plan(src, dbFile)
    vcardImport.report(planned)
    if args.dry_run:
        print("\n[dry-run] 未写任何一行，也未归档原件。")
        return 0
    summary = vcardImport.apply(planned, ownerID=args.owner or "",
                                archive=not args.no_archive, batchRows=args.batch)
    vcardImport.printSummary(summary)
    return 1 if summary["failed"] else 0


# ============================================================
# 三、list-families
# ============================================================

def cmd_list_families(args) -> int:
    """列家庭组 + 同姓建议合并项"""
    dbFile = _prepare(args)
    print("==== 家庭组 ====")
    print("库文件: %s" % dbFile)
    families = sqliteCommon.query_pb_family("pb_family", delFlag="0", mode="light",
                                            orderBy="recID")
    if not families:
        print("（还没有任何家庭组。家庭组只来自通讯录里显式声明的 KIND:group ——"
              "同姓只是**建议**，需人工确认后才建。）")
    for row in families:
        members = sqliteCommon.query_pb_person("pb_person", mode="light",
                                               familyGroupCode=row["familyCode"])
        print("  %-22s %-20s %d 人" % (row["familyCode"], row["familyName"],
                                       len(members)))
        for one in members:
            print("      %-26s %-20s 关系=%s"
                  % (one["personCode"], one["displayName"],
                     one.get("relation") or "-"))
    # 同姓建议：从库里现有人名册现算（导入时给过的建议可能已经处理完了）
    buckets = {}
    offset = 0
    pageRows = basicSettings.IMPORT_PAGE_ROWS
    while True:
        got = sqliteCommon.query_pb_person("pb_person", mode="light", delFlag="0",
                                           orderBy="recID", limitNum=pageRows,
                                           offsetNum=offset)
        if not got:
            break
        for row in got:
            familyName = str(row.get("familyName") or "").strip()
            if familyName:
                buckets.setdefault(familyName, []).append(row)
        if len(got) < pageRows:
            break
        offset += pageRows
    suggestions = []
    for familyName, group in buckets.items():
        if len(group) < 2:
            continue
        codes = set(str(row.get("familyGroupCode") or "") for row in group)
        codes.discard("")
        if len(codes) == 1:
            continue                     # 已经同组
        suggestions.append((familyName, group))
    suggestions.sort(key=lambda x: (-len(x[1]), x[0]))
    print("")
    print("---- 同姓多人（**建议**合并为一个家庭组，本命令不会自动建）----")
    if not suggestions:
        print("（没有需要建议的同姓分组）")
    for familyName, group in suggestions[:args.limit]:
        print("  · %-10s %d 人：%s"
              % (familyName, len(group),
                 "、".join(str(row["displayName"]) for row in group[:10])))
    if len(suggestions) > args.limit:
        print("  ...（其余 %d 组省略，用 --limit 调整）"
              % (len(suggestions) - args.limit))
    print("")
    print("确认某组确实是一家人之后，用 vCard 的 KIND:group 导入即可正式建组"
          "（本项目刻意不做「一键按姓建组」：同姓不等于一家人）。")
    return 0


# ============================================================
# 四、参数表
# ============================================================

def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo-browser-cli",
        description="photo-browser 联系人导入（CSV / vCard -> pb_person / pb_family）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command")

    def addCommon(target):
        target.add_argument("--db", default=None,
                            help="库文件（缺省 paths.db_file()；实验请指临时库）")
        target.add_argument("--owner", default=None,
                            help="写入 pb_person.ownerID（归属用户）")
        target.add_argument("--quiet", action="store_true", help="少打印")

    pCsv = sub.add_parser("import-csv", help="导入 CSV（主力通道）")
    addCommon(pCsv)
    pCsv.add_argument("--file", required=True, help="CSV 路径（Outlook 导出）")
    pCsv.add_argument("--dry-run", action="store_true", help="只打印计划，不写库不归档")
    pCsv.add_argument("--batch", type=int, default=None,
                      help="分批提交行数（缺省 %d）" % basicSettings.IMPORT_BATCH_ROWS)
    pCsv.add_argument("--no-archive", action="store_true",
                      help="不把原件复制到 <dbDir>\\imports\\")
    pCsv.set_defaults(func=cmd_import_csv)

    pVcf = sub.add_parser("import-vcard", help="导入 vCard（目录或单文件）")
    addCommon(pVcf)
    pVcf.add_argument("--path", required=True, help="vCard 文件或所在目录")
    pVcf.add_argument("--dry-run", action="store_true", help="只打印计划，不写库不归档")
    pVcf.add_argument("--batch", type=int, default=None,
                      help="分批提交行数（缺省 %d）" % basicSettings.IMPORT_BATCH_ROWS)
    pVcf.add_argument("--no-archive", action="store_true",
                      help="不把原件复制到 <dbDir>\\imports\\")
    pVcf.set_defaults(func=cmd_import_vcard)

    pFam = sub.add_parser("list-families", help="列家庭组 + 同姓建议合并项")
    addCommon(pFam)
    pFam.add_argument("--limit", type=int, default=20, help="建议项最多列几组")
    pFam.set_defaults(func=cmd_list_families)
    return parser


def main(argv=None) -> int:
    contact._fixConsole()
    parser = buildParser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if os.environ.get("PHOTO_BROWSER_QUIET"):
        args.quiet = True
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[中断] 已在批边界停下；**已落库的部分仍然有效**"
              "（导入是分批提交的），重跑同一文件不会重复建档。", file=sys.stderr)
        return 130
    except contact.ContactImportError as e:
        print("[Error] %s" % e, file=sys.stderr)
        _LOG.error("cli 前置校验失败: %s", e)
        return 2
    except paths.PathLayoutError as e:
        print("[PathLayoutError] %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        print("[Error] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        _LOG.error("cli 异常: %s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
