#! /usr/bin/env python3
#encoding: utf-8

#Filename: seed_step11_verify.py
#Description: 步骤 11 验收用的**一次性**数据准备脚本（不���产品代码路径）
#
# 为什么需要它
# ------------
#   正式库里 disputedCount = 0，而「我不同意」Tab 的每一条验收都要求有数据。
#   原因是 DR-16 的设计本身：质心**只用 isConfirmed=1 的样本**生成，
#   用户一次都没确认过 -> 一个质心都没有 -> 全库 0 自动归属 ->
#   「我不同意」列表自然是空的。**这是正确行为，不是 bug**
#   （步骤 R 的冷启动提示里就写了这一点）。
#
#   所以要看到「机器认的、可否决」这批脸，必须先把路走通：
#     ① 确认某个人的 3+ 张脸（走**真实接口** PUT /review/{faceCode}/assign）
#        -> 服务端立刻重算质心（这一段就是在验「质心防污染 + 即时生效」）
#     ② 跑 tools/run_match.py --run --assign 让匹配器把 auto 结果落库
#        -> 这一步之后库里才有 isConfirmed=0 且 personCode 非空的脸
#
# ⚠️ 它只操作**验收副本**（--db 指向的库），并且拒绝在正式库上运行。
#    误指正式库直接抛错退出 —— 这类脚本最危险的用法就是「顺手跑一下」。

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from common import paths as paths                                  # noqa: E402
from database import queryCommon as query                          # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402


def _refuseProduction(dbFile):
    """防呆：正式库上跑这个脚本会污染用户的真实数据。"""
    try:
        official = str(paths.db_file())
    except Exception:
        official = ""
    if official and os.path.normcase(os.path.abspath(dbFile)) == os.path.normcase(
        os.path.abspath(official)
    ):
        raise SystemExit(
            "[拒绝执行] 这是正式库（%s）。本脚本只允许跑在验收副本上：\n"
            "  先 Copy-Item d:\\PhotoLib\\db\\photolib.db d:\\PhotoLib\\db\\step11-verify.db\n"
            "  再用 --db 指向副本。" % official
        )


def seed(dbFile, need=3):
    """挑同簇的人（>= need 张脸），返回 [{clusterCode, faceCodes, photoCodes}]。

    ⚠️ 这里只**读**库做「挑人」；真正的确认由调用方走 HTTP 接口完成 ——
       用接口而不是直接写库，才能同时验收「PUT /assign 会重算质心」这条。
    """
    _refuseProduction(dbFile)
    sqliteCommon.dbHandle(dbFile)

    rows = query.selectList(
        "SELECT clusterCode FROM pb_face"
        " WHERE personCode IS NULL AND isStranger = 0 AND delFlag = '0'"
        " AND clusterCode IS NOT NULL"
        " GROUP BY clusterCode HAVING COUNT(*) >= %d"
        " ORDER BY COUNT(*) DESC LIMIT 5" % int(need), ())
    out = []
    for row in rows:
        cluster = str(row.get("clusterCode") or "")
        faces = query.selectList(
            "SELECT faceCode, photoCode FROM pb_face"
            " WHERE clusterCode = %s AND personCode IS NULL"
            " AND isStranger = 0 AND delFlag = '0' LIMIT %d" % ("%s", int(need)),
            (cluster,))
        if len(faces) >= need:
            out.append({
                "clusterCode": cluster,
                "faceCodes": [str(f.get("faceCode")) for f in faces],
                "photoCodes": sorted({str(f.get("photoCode")) for f in faces}),
            })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="步骤 11 验收：挑一个可确认的聚类簇")
    parser.add_argument("--db", required=True, help="验收副本库路径（**不能是正式库**）")
    parser.add_argument("--need", type=int, default=3, help="需要同簇人脸数（>=3 才有质心）")
    args = parser.parse_args(argv)

    groups = seed(str(args.db), args.need)
    for one in groups:
        print("cluster=%s faces=%d" % (one["clusterCode"], len(one["faceCodes"])))
        for code in one["faceCodes"]:
            print("    %s" % code)
    if not groups:
        print("[提示] 没有满足条件的簇（需要同簇 >= %d 张未归属人脸）" % args.need)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
