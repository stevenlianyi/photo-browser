#! /usr/bin/env python3
#encoding: utf-8

#Filename: backfill_person_pinyin.py
#Description: 存量 pb_person.displayNamePinyin 回填 —— 给已经有的人补上拼音检索串
#
# 为什么需要这个脚本（不跑会怎样）
# -------------------------------
#   displayNamePinyin 是**加列时新加的派生列**，`build_db.py --migrate` 只能
#   `ALTER TABLE ... ADD COLUMN` 把它补成 NULL（纯元数据操作，不动数据）。
#   而拼音检索是靠这一列的 —— 于是刚迁移完的那台机器上：
#     「新建的人」能按拼音搜到，「上个月就建好的人」一个都搜不到。
#   搜索看起来时灵时不灵，而这正是最难排查的那种坏法。
#   ⇒ 必须显式回填一次。**代码改对之后，存量数据不会自己变。**
#
# 为什么 pinyin 值算错时也会被改
# ------------------------------
#   本脚本**不是**「只补 NULL」，而是「算一遍，与库里不同就改」。
#   因为 pypinyin 的词表会升级、多音字会修：改了算法之后老拼音串就成了错的。
#   代价是每次跑都要全表算一遍拼音（几千行，秒级），换来的是「跑一次就一定对」。
#
# 幂等与安全
# ----------
#   * --dry-run 先打印将要改的行数与样例，确认后再实跑（默认就是 dry-run）；
#   * **只改 displayNamePinyin 一列**，不动 personCode / displayName / delFlag；
#   * 值相同的行不写；
#   * 逐行 update（不走 upsert）：upsert 的 INSERT 分支要求 displayName NOT NULL，
#     得额外带一列名字，而这里根本不需要写名字 —— 少写一列就少一个写错的机会。
#
# 用法
# ----
#   python code\src\tools\backfill_person_pinyin.py --dry-run
#   python code\src\tools\backfill_person_pinyin.py --yes
#   python code\src\tools\backfill_person_pinyin.py --dry-run --db d:\tmp\x.db
#
# 前置：先跑 `build_db.py --migrate` 把列补出来，再跑本脚本。
# 硬约束：只改库里一列派生值；photo 目录只读（本脚本根本不碰文件）。

import argparse
import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                              # noqa: E402
from common import pinyin as pinyin                                # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("backfillPersonPinyin", "backfillpersonpinyin.log")

#: 样例打印条数（只看前几条就够判断口径对不对）
_SAMPLE_NUM: int = 8


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _hasColumn() -> bool:
    """列在不在 —— 少跑一次 --migrate 就该在这里明确报错，而不是后面
    逐行 no such column。"""
    names = [row["name"] for row in sqliteCommon.tableInfo("pb_person")]
    return "displayNamePinyin" in names


def plan(dbFile: str = None) -> dict:
    """扫一遍库，算出「谁的拼音该被回填」。**不写任何一行。**"""
    if dbFile:
        sqliteCommon.dbHandle(dbFile)             # DR-10：必须显式才切库
    toFix, unchanged, empty = [], 0, 0
    for row in sqliteCommon.query_pb_person("pb_person", mode="light", delFlag="*"):
        name = str(row.get("displayName") or "")
        family = str(row.get("familyName") or "")
        want = pinyin.personPinyin(name, family)
        got = str(row.get("displayNamePinyin") or "")
        if not want:
            # 名字与姓都为空的行：算不出拼音，留 NULL（而不是写 ""）
            # —— "" 会让 `LIKE '%%'` 之外的比较语义变模糊。
            empty += 1
            continue
        if got == want:
            unchanged += 1
            continue
        toFix.append({"personCode": str(row.get("personCode") or ""),
                      "displayName": name, "familyName": family,
                      "got": got, "want": want})
    return {"toFix": toFix, "unchanged": unchanged, "empty": empty,
            "total": len(toFix) + unchanged + empty,
            "dbFile": sqliteCommon.dbFilePath()}


def _write(code: str, value: str) -> bool:
    rtn = sqliteCommon.updateTableGeneral(
        "pb_person", "personCode = %s", (code,), {"displayNamePinyin": value})
    return rtn != -2                          # sqliteHandle.RET_ERROR


def main(argv=None) -> int:
    _fixConsole()
    parser = argparse.ArgumentParser(
        prog="backfill_person_pinyin.py",
        description="回填存量 pb_person.displayNamePinyin（拼音检索列）")
    parser.add_argument("--db", default="", help="库文件（缺省正式库）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报不改（默认行为，--yes 才实跑）")
    parser.add_argument("--yes", action="store_true", help="确认实跑")
    args = parser.parse_args(argv)
    # 默认只报不改：改生产数据必须显式 --yes（DR-13 的 spirit：不确认不动数据）
    dryRun = bool(args.dry_run) or not bool(args.yes)

    print("backfill_person_pinyin _VERSION: %s" % _VERSION)
    print("pypinyin 可用: %s" % ("是" if pinyin._HAS_PINYIN else
                                  "**否 —— 回填出来只有英文/数字，拼音仍然搜不到**"))
    if args.db:
        sqliteCommon.dbHandle(args.db)
    if not _hasColumn():
        print("\n[Error] pb_person 里还没有 displayNamePinyin 列。"
              "先跑：\n    python code\\src\\tools\\build_db.py --migrate")
        return 2

    report = plan(args.db or None)
    toFix = report["toFix"]
    print("库            : %s" % report["dbFile"])
    print("pb_person 总行: %d" % report["total"])
    print("待回填        : %d 行" % len(toFix))
    print("已正确        : %d 行（值与算出来的一致，不重复写）" % report["unchanged"])
    print("算不出拼音    : %d 行（displayName 与 familyName 都为空，保持 NULL）"
          % report["empty"])
    if toFix:
        print("样例（%-d 条）:" % min(len(toFix), _SAMPLE_NUM))
        for one in toFix[:_SAMPLE_NUM]:
            print("   %-28s %-16s 旧=%-22s -> 新=%s"
                  % (one["personCode"], one["displayName"],
                     one["got"] or "(NULL)", one["want"]))

    if dryRun:
        print("\n[dry-run] 未做任何修改。确认无误后加 --yes 实跑。")
        return 0

    written, failed = 0, []
    for one in toFix:
        if _write(one["personCode"], one["want"]):
            written += 1
        else:
            failed.append(one["personCode"])
    print("\n实跑完成：回填 %d 行，失败 %d 行" % (written, len(failed)))
    if failed:
        print("[Error] 失败的 personCode（最多 %d 条）: %s"
              % (_SAMPLE_NUM, failed[:_SAMPLE_NUM]))
        _LOG.error("拼音回填失败 %d 行: %s", len(failed), failed[:50])
        return 1
    print("复检：")
    again = plan(args.db or None)
    print("   待回填 %d 行（应为 0）" % len(again["toFix"]))
    _LOG.info("拼音回填: 写入 %d 行, 原本已正确 %d 行",
              written, report["unchanged"])
    return 0 if not again["toFix"] else 1


if __name__ == "__main__":
    sys.exit(main())