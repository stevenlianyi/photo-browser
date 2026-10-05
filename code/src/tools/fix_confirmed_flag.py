#! /usr/bin/env python3
#encoding: utf-8

#Filename: fix_confirmed_flag.py
#Description: 存量 isConfirmed 语义回改（修正步骤 R 一次性脚本）——
#             把「自动归属却被写成 isConfirmed=1」的脸改回 0
#
# 为什么需要这个脚本（不修会怎样）
# --------------------------------
#   修正前 assign() 对**自动与人工一律**写 isConfirmed=1（硬编码），
#   于是库里已经自动归属过的脸全被标成「人工确认」。后果两条，都不报错：
#     ① 「我不同意」列表（personCode 非空 + isConfirmed=0 + isStranger=0）
#        永远是空的 -> 用户最高频的纠错场景（自动认错了人）没有入口；
#     ② 质心只取 isConfirmed=1 -> 全部由自动样本构成，防污染彻底失效。
#   代码改对之后，**存量数据不会自己变** —— 必须显式回改一次。
#
# 判定依据：pb_photo_person.source（0 自动 / 1 人工确认或改判）
# --------------------------------------------------------
#   关联行的 source 就是「这条归属是机器写的还是人点的」，
#   它在 pb_photo_person 里，语义与 pb_face.isConfirmed **一一对应**。
#   以 **faceCode 为准**：一张照片可能有多个关联行、一个人脸只对应一个 faceCode，
#   faceCode 才是「判定来源那张脸」。
#     · source=0 且 faceCode 非空 -> 该脸 isConfirmed 回改为 0
#     · source=1                 -> 保持 1（本来就是人工确认或改判）
#     · faceCode 为空             -> **跳过并计数**（无法定位到具体某张脸，
#                                    宁可漏改也不猜：猜错会把人工确认的脸标成
#                                    「机器认的」，等于凭空造出一条待纠错项）
#   ⚠️ 一张照片里同一个人可能有多张脸，关联行只记**其中一张**做 faceCode。
#      所以「没被 faceCode 指到」的脸**不动**（它可能本来就是人工确认的）。
#      宁可少改（漏改只是多一条「我不同意」候选项，用户点一下就消掉），
#      不可多改（把人工确认的脸错标成可否决 = 逼用户重做已完成的确认）。
#
# 幂等与安全
# ----------
#   * --dry-run 先打印将要改的行数与样例，确认后再实跑；
#   * 只改 isConfirmed 一列，**不动** personCode / delFlag / 任何向量；
#   * 已回改过的行（本来就是 0）不重复写；
#   * 跑完给出四态统计（待确认 / 我不同意 / 人工确认 / 陌生人）。
#
# 用法
# ----
#   python code\src\tools\fix_confirmed_flag.py --dry-run
#   python code\src\tools\fix_confirmed_flag.py --yes
#   python code\src\tools\fix_confirmed_flag.py --dry-run --db d:\tmp\x.db
#
# 硬约束：只改库里的一个标志位；photo 目录只读（本脚本根本不碰文件）。

import argparse
import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                              # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("fixConfirmedFlag", "fixconfirmedflag.log")

#: 样例打印条数（只看前几条就够判断口径对不对）
_SAMPLE_NUM: int = 5


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _fixBytes(rows: list) -> int:
    """把回改行按批落地（与扫描器同样的事务粒度）。

    ⚠️ 必须带齐 pb_face 的 NOT NULL 身份列 photoCode：upsert 走的是 INSERT 语义，
    缺列会撞 `NOT NULL constraint failed: pb_face.photoCode`（与 assigner._patchFace
    同一个坑）。所以行里除了 isConfirmed 还要带上这张脸自己的 photoCode。
    """
    return sqliteCommon.insertManyTableGeneral(
        "pb_face", rows,
        conflictColumns=("faceCode",),
        updateColumns=("isConfirmed", "modifyYMDHMS"),
        fillStandard=True,
        forceColumns=("isConfirmed",))


def plan(dbFile: str = None) -> dict:
    """扫一遍库，算出「谁该被回改」。**不写任何一行。**"""
    if dbFile:
        sqliteCommon.dbHandle(dbFile)             # DR-10：必须显式才切库
    toFix, keepManual, skipped, alreadyZero, unknownFace = [], 0, 0, 0, []
    for link in sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                   mode="light"):
        faceCode = str(link.get("faceCode") or "")
        source = int(link.get("source") or 0)
        if not faceCode:
            # 定位不到具体某张脸 -> 跳过并计数（理由见文件头）
            skipped += 1
            continue
        rows = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode, mode="light")
        if not rows:
            unknownFace.append(faceCode)
            continue
        if int(rows[0].get("isConfirmed") or 0) == 0:
            alreadyZero += 1
            continue
        if source == 0:
            if not str(rows[0].get("photoCode") or ""):
                # 脸自己的 photoCode 为空 -> upsert 会撞 NOT NULL，写不了
                skipped += 1
                continue
            toFix.append({"faceCode": faceCode,
                          "photoCode": str(rows[0].get("photoCode") or ""),
                          "personCode": str(link.get("personCode") or ""),
                          "confidence": link.get("confidence")})
        else:
            keepManual += 1
    return {"toFix": toFix, "keepManual": keepManual, "skipped": skipped,
            "alreadyZero": alreadyZero, "unknownFace": unknownFace,
            "dbFile": sqliteCommon.dbFilePath()}


def fourStates() -> dict:
    """四态统计（互斥口径，DR-16①）。四者之和 == pb_face 总行数。"""
    pending, disputed, confirmed, stranger, total = 0, 0, 0, 0, 0
    for row in sqliteCommon.query_pb_face("pb_face", mode="light", delFlag="*"):
        total += 1
        person = str(row.get("personCode") or "")
        isStranger = int(row.get("isStranger") or 0)
        isConfirmed = int(row.get("isConfirmed") or 0)
        if isStranger:
            stranger += 1
        elif isConfirmed:
            confirmed += 1
        elif not person:
            pending += 1
        else:
            disputed += 1
    return {"pending": pending, "disputed": disputed, "confirmed": confirmed,
            "stranger": stranger, "total": total,
            "sum": pending + disputed + confirmed + stranger}


def main(argv=None) -> int:
    _fixConsole()
    parser = argparse.ArgumentParser(
        prog="fix_confirmed_flag.py",
        description="回改存量 pb_face.isConfirmed（自动归属的写成 0）")
    parser.add_argument("--db", default="", help="库文件（缺省正式库）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报不改（默认行为，--yes 才实跑）")
    parser.add_argument("--yes", action="store_true", help="确认实跑")
    args = parser.parse_args(argv)
    # 默认只报不改：改生产数据必须显式 --yes（DR-13 的 spirit：不确认不动数据）
    dryRun = bool(args.dry_run) or not bool(args.yes)

    print("fix_confirmed_flag _VERSION: %s" % _VERSION)
    report = plan(args.db or None)
    toFix = report["toFix"]
    print("库            : %s" % report["dbFile"])
    print("待回改        : %d 张（关联 source=0 且脸当前 isConfirmed=1）" % len(toFix))
    print("保持人工确认  : %d 张（关联 source=1）" % report["keepManual"])
    print("已是0无需改   : %d 张" % report["alreadyZero"])
    print("跳过          : %d 条（关联行 faceCode 为空，无法定位到具体脸）"
          % report["skipped"])
    if report["unknownFace"]:
        print("⚠️  faceCode 查无此脸: %d 条（%s）"
              % (len(report["unknownFace"]), report["unknownFace"][:_SAMPLE_NUM]))
    if toFix:
        print("样例:")
        for one in toFix[:_SAMPLE_NUM]:
            print("   %-34s %-34s -> %-20s score=%s"
                  % (one["faceCode"], one["photoCode"], one["personCode"],
                     one["confidence"]))

    if dryRun:
        print("\n[dry-run] 未做任何修改。确认无误后加 --yes 实跑。")
        return 0

    written = 0
    now = misc.getTime()
    step = 500
    for begin in range(0, len(toFix), step):
        chunk = toFix[begin:begin + step]
        rtn, _cols = _fixBytes([{"faceCode": one["faceCode"],
                                 "photoCode": one["photoCode"],
                                 "isConfirmed": 0, "modifyYMDHMS": now}
                                for one in chunk])
        if rtn == -2:                        # sqliteHandle.RET_ERROR
            print("[Error] 第 %d 批写入失败: %s"
                  % (begin // step + 1, sqliteCommon.dbHandle().lastErrMsg))
            return 1
        written += len(chunk)
    print("\n实跑完成：回改 %d 行" % written)
    stat = fourStates()
    print("四态统计（回改后）:")
    print("   待确认    (personCode IS NULL AND isStranger=0)  = %d" % stat["pending"])
    print("   我不同意  (personCode 非空 AND isConfirmed=0 AND isStranger=0) = %d"
          % stat["disputed"])
    print("   人工确认  (isConfirmed=1)                        = %d" % stat["confirmed"])
    print("   陌生人    (isStranger=1)                        = %d" % stat["stranger"])
    print("   合计 %d / pb_face 总行数 %d -> %s"
          % (stat["sum"], stat["total"],
             "不重不漏 ✅" if stat["sum"] == stat["total"] else "**对不上，有问题**"))
    _LOG.info("isConfirmed 回改: %d 行，四态 %s", written, stat)
    return 0 if stat["sum"] == stat["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
