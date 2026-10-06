#! /usr/bin/env python3
#encoding: utf-8

#Filename: rebucket_cli.py
#Description: photo-browser 年代桶重刷 CLI（修正步骤 R2 / DR-20 + DR-22）——
#             刷 pb_face.shotBucket、按需重算质心、只读巡检桶口径
#
# 这个工具解决什么
# ----------------
#   faceStore 落的是等宽 5 年**占位桶**，而质心的桶键是「拍摄年 + 出生年」
#   算出来的**自适应**桶（0-18 岁 3 年 / 18+ 10 年）。两套口径错开的后果是
#   「质心按一套建、脸表按另一套取」-> 取不到候选、匹配率归零、**库里不报错**
#   （DR-20）。修法是补一个「重刷」过程（方案 A：保留落库 shotBucket）。
#
# ⚠️ 日常**不需要**记得跑它
#   写入路径已经接好了：归属（assigner._setBelong）、退回未归属/陌生人
#   （assigner.fix）、合并/撤销（merger）、拆人（merger.split -> assigner）、
#   联系人导入后（contactCommon.applyPlan / tools/import_contacts）
#   都会自动刷桶 + 重算质心。本工具只干两件事：
#     ① **存量**一次性刷干净（升级到 R2 之后的第一次）；
#     ② **巡检**（--audit）：把「桶口径不一致」这类静默失配变成一条明确结论。
#
# ⚠️ --recompute **不是**一条「只重算」的旁路（DR-22）
#   它必须跟在刷桶之后：本工具的实现是「先刷、再算」，
#   所以**没有**任何一条代码路径能跳过刷桶直接重算质心。
#   而 centroid.recompute/recomputePerson 自己也有前置检查
#   （centroid._assertBucketOrder）：脸表桶键与当前口径不符就**抛错**，
#   并在消息里给出该跑的命令 —— 顺序反了不会被默默算成错的质心。
#
# 用法
# ----
#   # 巡检（只读，不写任何一行）—— 什么时候都该先跑一次看现状
#   python code\src\tools\rebucket_cli.py --audit
#
#   # 全库刷桶（先看要改什么）
#   python code\src\tools\rebucket_cli.py --all --dry-run
#   python code\src\tools\rebucket_cli.py --all
#
#   # 全库刷桶 + 重算质心（**存量刷干净的正确姿势**：先刷后算）
#   python code\src\tools\rebucket_cli.py --all --recompute
#
#   # 某个人 / 某张照片
#   python code\src\tools\rebucket_cli.py --personP_xxx
#   python code\src\tools\rebucket_cli.py --photo PH_xxx --recompute
#
#   # 只刷「已归属 + 主人生日合法」的那些（10 万张脸里绝大多数是未归属的，
#   # 它们本来就该是等宽降级桶，全量重算对它们是纯浪费）
#   python code\src\tools\rebucket_cli.py --all --only-adaptive
#
# 硬约束：只改数据库里的一个列（pb_face.shotBucket）；
#         photo 目录一律只读（本工具根本不碰文件）。

import argparse
import json
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                                  # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon      # noqa: E402
from engine.match import rebucket as rebucket                         # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("rebucketCli", "rebucketcli.log")

#: 明细打印条数（只看前几条就够判断口径对不对）
_SAMPLE_NUM: int = 8


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _printSamples(title: str, samples: list) -> None:
    if not samples:
        return
    print("  %s:" % title)
    for one in samples[:_SAMPLE_NUM]:
        print("     face=%-34s shotYear=%-6s生日=%-12s 桶 %s -> %s"
              % (one.get("faceCode", "-"), one.get("shotYear", "-"),
                 one.get("birthday", one.get("personCode", "-")) or "-",
                 one.get("old", "-"), one.get("new", "-")))
    if len(samples) > _SAMPLE_NUM:
        print("     …… 另有 %d 条" % (len(samples) - _SAMPLE_NUM))


def _recomputeScope(personCode: str = "", photoCode: str = "") -> dict:
    """刷完桶之后重算质心。**刷桶一定在前**（DR-22）。

    范围怎么定：
      --photo  -> 该照片里出现过的人（去重）
      --person -> 就这一个人
      --all    -> 全库（每人都来一遍）
    """
    from engine.match import centroid as centroid
    start = time.perf_counter()
    done = buckets = enabled = dropped = failed = 0
    if photoCode:
        codes = []
        for row in sqliteCommon.query_pb_face("pb_face", photoCode=photoCode,
                                              mode="light"):
            code = str(row.get("personCode") or "")
            if code and code not in codes:
                codes.append(code)
    elif personCode:
        codes = [personCode]
    else:
        codes = [str(p.get("personCode") or "")
                 for p in sqliteCommon.query_pb_person("pb_person", mode="light")
                 if str(p.get("personCode") or "")]
    for code in codes:
        try:
            stat = centroid.recomputePerson(code)
        except (rebucket.BucketStaleError, RuntimeError) as e:
            # 前置检查拦下来了 —— 这是 DR-22 要的行为：明确报错，不默默算错的
            failed += 1
            print("  ! %s 重算被前置检查拦下：%s" % (code, e))
            continue
        done += 1
        buckets += len(stat["buckets"])
        enabled += int(stat["enabled"])
        dropped += int(stat["dropped"])
    return {"persons": done, "buckets": buckets, "enabled": enabled,
            "dropped": dropped, "failed": failed,
            "elapsed": round(time.perf_counter() - start, 3)}


def cmdAudit(args) -> int:
    report = rebucket.auditBuckets()
    print("库            :", sqliteCommon.dbFilePath())
    print()
    print(rebucket.formatAudit(report))
    if args.json:
        print()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["clean"] else 1


def cmdPerson(args) -> int:
    info = rebucket.rebucketPerson(args.person, dryRun=args.dry_run)
    if info.get("error"):
        print("[Error]", info["error"])
        return 2
    _printOne("person", info)
    if args.recompute and not args.dry_run:
        rep = _recomputeScope(personCode=args.person)
        _printRecompute(rep)
    return 0


def cmdPhoto(args) -> int:
    info = rebucket.rebucketPhoto(args.photo, dryRun=args.dry_run)
    if info.get("error"):
        print("[Error]", info["error"])
        return 2
    _printOne("photo", info)
    if args.recompute and not args.dry_run:
        rep = _recomputeScope(photoCode=args.photo)
        _printRecompute(rep)
    return 0


def cmdAll(args) -> int:
    def _progress(done, total):
        if done % 5000 == 0:
            print("   ...已处理 %d 张（库内约 %s 张）" % (done, total or "?"))

    info = rebucket.rebucketAll(progress=_progress,
                               onlyAdaptive=args.only_adaptive,
                               dryRun=args.dry_run)
    print("处理脸数      : %d 张（其中已改桶 %d 张，落库 %d 张）"
          % (info["faces"], info["changed"], info["written"]))
    print("  自适应桶    : %d 张（有生日 -> 童年 3 年 / 成年 10 年）"
          % info["adaptive"])
    print("  等宽降级    : %d 张（无生日或未归属，**保持等宽是对的**）"
          % info["degraded"])
    print("  无拍摄年份  : %d 张（shotBucket 落NULL，不参与跨桶比对）"
          % info["noShotYear"])
    if info.get("skippedByOnlyAdaptive"):
        print("  --only-adaptive 跳过: %d 张（未归属或主人无生日，"
              "刷了也不会变）" % info["skippedByOnlyAdaptive"])
    print("耗时          : %.2fs" % info["elapsed"])
    _printSamples("改桶样例", info["samples"])
    if info["changed"] == 0 and not args.dry_run:
        print("\n没有任何一张脸需要改桶 —— 说明脸表已经是当前口径。")
    if args.recompute and not args.dry_run:
        rep = _recomputeScope()
        _printRecompute(rep)
    return 0


def _printOne(scope: str, info: dict) -> None:
    tag = "pb_person" if scope == "person" else "pb_photo"
    print("%-13s: %s" % (tag, info.get("personCode") or info.get("photoCode")))
    print("  脸数        : %d 张（改桶 %d 张）" % (info["faces"], info["changed"]))
    if info.get("oldBuckets") or info.get("newBuckets"):
        print("  桶键集合改前: %s" % (", ".join(info.get("oldBuckets") or ()) or "(无)"))
        print("  桶键集合改后: %s" % (", ".join(info.get("newBuckets") or ()) or "(无)"))
    _printSamples("改桶样例", info.get("samples") or [])


def _printRecompute(rep: dict) -> None:
    print("重算质心      : %d 人、%d 个桶（启用 %d、清僵尸桶 %d），耗时 %.2fs"
          % (rep["persons"], rep["buckets"], rep["enabled"], rep["dropped"],
             rep["elapsed"]))
    if rep["failed"]:
        print("  ⚠ %d 人被前置检查拦下（脸表桶键与当前口径不符）——"
              "先解决它们，质心才算得出来" % rep["failed"])


def buildParser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rebucket_cli.py",
        description="刷 pb_face.shotBucket（自适应分桶）+ 可选重算质心（先刷后算）")
    p.add_argument("--db", default="", help="库文件（缺省正式库；实验请指临时库）")
    p.add_argument("--all", action="store_true", help="全库刷桶")
    p.add_argument("--person", default="", help="只刷某个人（personCode）")
    p.add_argument("--photo", default="", help="只刷某张照片（photoCode）")
    p.add_argument("--audit", action="store_true", help="只读巡检（不写任何一行）")
    p.add_argument("--only-adaptive", action="store_true",
                   help="只刷「已归属 + 主人生日合法」的那些（--all 时有效）")
    p.add_argument("--dry-run", action="store_true",
                   help="只报不改（**默认就是只读**，加 --yes 才实跑）")
    p.add_argument("--yes", action="store_true", help="确认实跑（改生产数据必须显式给）")
    p.add_argument("--recompute", action="store_true",
                   help="刷完之后重算质心（**先刷后算**，没有只重算的旁路）")
    p.add_argument("--json", action="store_true", help="--audit 时附带 JSON 输出")
    return p


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv)
    print("rebucket_cli.py _VERSION: %s" % _VERSION)
    if args.db:
        sqliteCommon.dbHandle(args.db)             # DR-10：必须显式才切库

    # 默认只报不改：改生产数据必须显式 --yes（与 fix_confirmed_flag.py 同一口径）
    dryRun = bool(args.dry_run) or not bool(args.yes)

    if args.audit:
        return cmdAudit(args)
    if args.recompute and not (args.all or args.person or args.photo):
        print("[Error] --recompute 必须配--all / --person / --photo 之一"
              "（它必须跟在刷桶之后；不给范围就会变成一条「全库只重算」的旁路，"
              "而那正是 DR-22 要禁止的）")
        return 2
    if not (args.all or args.person or args.photo):
        buildParser().print_help()
        return 2
    if dryRun:
        print("[dry-run] 未写任何一行。确认无误后加 --yes 实跑。")

    rebucket.assertBucketWidthFallback()        # 降级桶宽与 faceStore 对齐
    if args.all:
        rc = cmdAll(args)
    elif args.person:
        rc = cmdPerson(args)
    else:
        rc = cmdPhoto(args)
    _LOG.info("rebucket_cli: all=%s person=%s photo=%s audit=%s dryRun=%s "
              "recompute=%s", args.all, args.person, args.photo, args.audit,
              dryRun, args.recompute)
    return rc


if __name__ == "__main__":
    sys.exit(main())