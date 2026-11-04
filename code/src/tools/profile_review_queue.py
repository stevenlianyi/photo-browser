#! /usr/bin/env python3
#encoding: utf-8

#Filename: profile_review_queue.py
#Description: 「待确认」队列接口的**性能度量**脚本（只读；只跑在库副本上）
#
# 为什么要这个脚本
# ----------------
#   用户报「点忽略/确认后等很久」。日志已经指出前端一次写操作后跟着 11 次串行
#   GET /api/review/pending，但**每个页请求有多重**只能靠量出来 ——
#   凭感觉优化会挑错靶子（例如以为是 SQLite 锁等待，而库其实是 WAL + 读写双连接，
#   写根本不阻塞读）。
#
#   所以本脚本把一次页请求拆成四段分别计时：
#     ① countPending()                  —— 每次请求都算一次全量 count
#     ② personCards()                   —— 人物卡（姓名 + 候选头像兜底）
#     ③ centroid.loadAllCentroids()     —— 质心矩阵（两遍全表扫描 + 解 BLOB）
#     ④ pendingQueue(offset=0 / 200)    —— 取一页并打分（= 上面 ②③ + 排序 + 矩阵乘）
#   再单独量「那条取页 SQL」本身，并打印它的 EXPLAIN QUERY PLAN ——
#   用来证明它到底走不走索引、要不要对全量待确认行做文件排序。
#
# ⚠️ 只读、且**拒绝在正式库上运行**：度量会全表扫、会拖慢正在跑的服务。
#    用法：
#      python -m tools.profile_review_queue --db "d:/PhotoLib/db/bench-queue.db"
#    先做副本：
#      Copy-Item "d:/PhotoLib/db/photolib.db" "d:/PhotoLib/db/bench-queue.db"
#
# 输出是给人看的表：环节 / 耗时(ms) / 备注。改造前后各跑一次即可对照。

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from common import paths as paths                                  # noqa: E402
from common import sqliteHandle as sqliteHandle                     # noqa: E402
from database import queryCommon as query                          # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.match import centroid                                  # noqa: E402
from processor.review import queue as reviewQueue                  # noqa: E402


def _refuseProduction(dbFile):
    """防呆：正式库上跑度量 = 给正在用的服务添堵，且全表扫很不礼貌。"""
    try:
        official = str(paths.db_file())
    except Exception:
        official = ""
    if official and os.path.normcase(os.path.abspath(dbFile)) == os.path.normcase(
        os.path.abspath(official)
    ):
        raise SystemExit(
            "[拒绝执行] 这是正式库（%s）。请先做一份副本再量：\n"
            "  Copy-Item \"%s\" \"d:/PhotoLib/db/bench-queue.db\"\n"
            "  python -m tools.profile_review_queue --db \"d:/PhotoLib/db/bench-queue.db\""
            % (official, official))


def _timeIt(label, func, repeats=3):
    """跑 repeats 次取**最快一次**（避免偶发 IO 抖动把结论带偏），返回 (ms, 结果)。

    取最快而不是平均：Python 侧这里量的是纯 CPU/SQL 成本，
    系统噪声只会让数字变大，不会让它变小 —— 最小值最接近真实成本。
    """
    best = None
    value = None
    for _ in range(max(1, int(repeats))):
        start = time.perf_counter()
        value = func()
        cost = (time.perf_counter() - start) * 1000.0
        if best is None or cost < best:
            best = cost
    return best, value


def _pendingPageSQL(limit=20, offset=200):
    """复刻 `queue._pendingRecIds` 落到 SQLite 上的那条语句（**只取 recID**）。

    ⚠️ 必须**逐字复刻**：口径是 `WHERE_PENDING`（personCode IS NULL + isStranger=0
       + delFlag='0'）、排序 quality DESC、LIMIT+OFFSET。R17b 之后取页是两段式
       （先 recID、再按 recID 回表），所以这里量的是第一段。
    """
    return ("SELECT recID FROM pb_face"
            " WHERE personCode IS NULL AND isStranger = 0 AND delFlag = '0'"
            " ORDER BY quality DESC, recID ASC LIMIT %d OFFSET %d"
            % (int(limit), int(offset)))


def _pendingCursorSQL(limit=20, quality=0.5, recId=1):
    """复刻「游标续取」（补页走的那条）——R17b 修「跳过一会儿就循环」用的。

    条件必须与 `queue._pendingRecIds(after=...)` 逐字一致（含 NULL 安全那四个分支）。
    """
    return ("SELECT recID FROM pb_face"
            " WHERE personCode IS NULL AND isStranger = 0 AND delFlag = '0'"
            " AND ((quality IS NOT NULL AND quality < %s)"
            " OR (quality IS NULL AND %s IS NOT NULL)"
            " OR (quality = %s AND recID > %s)"
            " OR (quality IS NULL AND %s IS NULL AND recID > %s))"
            " ORDER BY quality DESC, recID ASC LIMIT %d"
            % (quality, quality, quality, int(recId), quality, int(recId), int(limit)))


def _pendingPageSQLFull(limit=20, offset=200):
    """R17b **之前**的写法（整行 SELECT *，含 2KB 向量）—— 留作对照基线。"""
    return ("SELECT * FROM pb_face"
            " WHERE personCode IS NULL AND isStranger = 0 AND delFlag = '0'"
            " ORDER BY quality DESC LIMIT %d OFFSET %d" % (int(limit), int(offset)))


def _explain(sql):
    """EXPLAIN QUERY PLAN 的原始行（走生成层的读连接，不经 queryCommon 的 SELECT 闸门）。"""
    db = sqliteCommon.dbHandle()
    if db.executeRead("EXPLAIN QUERY PLAN " + sql, ()) == sqliteHandle.RET_ERROR:
        return []
    rows = db.fetchAll()
    out = []
    for row in rows:
        try:
            out.append(str(row["detail"]))
        except Exception:
            out.append(str(tuple(row)))
    return out


def _explainVariants():
    """几种写法的查询计划对照 —— 专治「明明加了索引却没生效」。

    典型病因（都**不报错**，只是慢）：
      · 部分索引的谓词与查询谓词不「语法等价」（例如索引写 `personCode IS NULL`、
        查询写 `personCode IS NULL AND delFlag=?`）→ 索引被忽略；
      · 排序方向/列顺序与索引不一致 → 退回 TEMP B-TREE 排序；
      · 生成层把 delFlag 写成**绑定参数**（`delFlag = ?`），参数不是常量，
        永远匹配不上索引里的 `'0'` / 谓词里写死的常量。
    """
    variants = (
        ("旧写法：整行 SELECT *",
         _pendingPageSQLFull(20, 200)),
        ("新写法：只取 recID",
         _pendingPageSQL(20, 200)),
        # ⚠️ 这里刻意**不**再用 `INDEXED BY` 去"证明"能走 quality 索引：
        #    quality 是 DECIMAL(6,4)（NUMERIC 亲和性），SQLite 不会用它的索引
        #    满足 ORDER BY（实测连全表 `ORDER BY quality` 都是 SCAN + B-TREE）。
        #    能省的只有"2.2 万次随机回表的判活成本"，不是"排序本身"。
        ("+ 覆盖索引列（isStranger/delFlag/quality）",
         "SELECT recID FROM pb_face WHERE personCode IS NULL AND isStranger = 0"
         " AND delFlag = '0' ORDER BY quality DESC LIMIT 20 OFFSET 200"),
    )
    db = sqliteCommon.dbHandle()
    # 先把库里的索引原样列出来：计划没用上索引时，第一个要排除的就是
    # 「索引根本没建」（migrate 没跑/跑在别的库上），而不是急着怀疑优化器。
    for row in sqliteCommon.indexList("pb_face"):
        print("  [index] %-40s unique=%s partial=%s"
              % (row.get("name"), row.get("unique"), row.get("partial")))
    for label, sql in variants:
        values = ("0",) if "?" in sql else ()
        if db.executeRead("EXPLAIN QUERY PLAN " + sql, values) == sqliteHandle.RET_ERROR:
            print("  %-34s <执行失败>" % label)
            continue
        details = [str(row["detail"]) for row in db.fetchAll()]
        print("  %-34s %s" % (label, " | ".join(details)))


def _tableCounts():
    """库规模（结论必须带上量级，否则「几毫秒」没有意义）。"""
    out = {}
    for label, sql in (("pb_photo", "SELECT COUNT(*) AS n FROM pb_photo"),
                       ("pb_face", "SELECT COUNT(*) AS n FROM pb_face"),
                       ("pb_face.pending",
                        "SELECT COUNT(*) AS n FROM pb_face WHERE personCode IS NULL"
                        " AND isStranger = 0 AND delFlag = '0'"),
                       ("pb_person", "SELECT COUNT(*) AS n FROM pb_person"),
                       ("pb_person_centroid",
                        "SELECT COUNT(*) AS n FROM pb_person_centroid")):
        out[label] = int(query.selectValue(sql, ()) or 0)
    return out


def profile(dbFile, repeats=3):
    sqliteCommon.dbHandle(dbFile)

    rows = []
    counts = _tableCounts()
    for label, value in counts.items():
        rows.append((label, "-", str(value)))

    ms, _ = _timeIt("countPending", reviewQueue.countPending, repeats)
    rows.append(("countPending()", "%.1f" % ms, "待确认条数（每次页请求都算）"))

    ms, _ = _timeIt("countStates", reviewQueue.countStates, repeats)
    rows.append(("countStates()", "%.1f" % ms, "四态 4 次 count（写接口回带角标用）"))

    ms, _ = _timeIt("countDisputed", reviewQueue.countDisputed, repeats)
    rows.append(("countDisputed()", "%.1f" % ms, "「我不同意」条数（侧栏第二个角标）"))

    for label, sql in (("confirmed", "SELECT COUNT(*) AS n FROM pb_face"
                                    " WHERE isConfirmed = 1 AND isStranger = 0"
                                    " AND delFlag = '0'"),
                       ("stranger", "SELECT COUNT(*) AS n FROM pb_face"
                                    " WHERE isStranger = 1 AND delFlag = '0'")):
        ms, _ = _timeIt("count_" + label, lambda s=sql: query.selectValue(s, ()), repeats)
        rows.append(("count_%s()" % label, "%.1f" % ms, "四态之一（角标并不需要）"))

    cards = {}

    def _cards():
        got = reviewQueue.personCards()
        cards.clear()
        cards.update(got)
        return got

    ms, _ = _timeIt("personCards", _cards, repeats)
    rows.append(("personCards()", "%.1f" % ms,
                 "%d 人（含头像兜底逐人回扫人脸）" % len(cards)))

    def _centroids():
        return centroid.loadAllCentroids()

    ms, got = _timeIt("loadAllCentroids", _centroids, 1)
    matrix = got[0] if got else None
    rows.append(("loadAllCentroids()", "%.1f" % ms,
                 "质心 %d 行" % (0 if matrix is None else int(matrix.shape[0]))))

    matrix, index = centroid.loadAllCentroids()
    ms, items = _timeIt(
        "pendingQueue(offset=0)",
        lambda: reviewQueue.pendingQueue(limit=20, offset=0, topN=5,
                                         matrix=matrix, index=index), repeats)
    rows.append(("pendingQueue(offset=0)", "%.1f" % ms, "%d 条" % len(items)))

    ms, items = _timeIt(
        "pendingQueue(offset=200)",
        lambda: reviewQueue.pendingQueue(limit=20, offset=200, topN=5,
                                         matrix=matrix, index=index), repeats)
    rows.append(("pendingQueue(offset=200)", "%.1f" % ms, "%d 条" % len(items)))

    # 只量「取页 SQL」本身：用来分离「SQL 慢」与「Python 打分慢」
    sql = _pendingPageSQL()
    ms, got = _timeIt("sql", lambda: query.selectList(sql, ()), repeats)
    rows.append(("SQL 新: 只取 recID", "%.1f" % ms, "%d 行" % len(got)))

    sqlFull = _pendingPageSQLFull()
    ms, got = _timeIt("sqlFull", lambda: query.selectList(sqlFull, ()), repeats)
    rows.append(("SQL 旧: 整行(2KB×N)", "%.1f" % ms, "%d 行" % len(got)))

    sqlCursor = _pendingCursorSQL()
    ms, got = _timeIt("sqlCursor", lambda: query.selectList(sqlCursor, ()), repeats)
    rows.append(("SQL 游标续取(补页)", "%.1f" % ms, "%d 行" % len(got)))

    print("库: %s" % os.path.abspath(dbFile))
    print("%-28s %-9s %s" % ("环节", "耗时(ms)", "备注"))
    print("-" * 78)
    for label, cost, note in rows:
        print("%-28s %-9s %s" % (label, cost, note))

    print("\nEXPLAIN QUERY PLAN（取一页的那条 SQL）:")
    for line in _explain(sql):
        print("  " + line)
    print("\nEXPLAIN QUERY PLAN（写法对照）:")
    _explainVariants()
    print("\n提示：`quality` 是 DECIMAL(6,4)（NUMERIC 亲和性），SQLite **不会**用"
          "任何 quality 索引去满足 ORDER BY —— 出现 `USE TEMP B-TREE` 是正常的。"
          "\n      该看的是：索引项里有没有 isStranger/delFlag（有 = 排序只碰索引项、"
          "不回表；没有 = 每次翻页 2.2 万次随机回表，几十毫秒）。")


def main(argv=None):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="profile_review_queue",
        description="待确认队列接口性能度量（只读，禁止在正式库上跑）")
    parser.add_argument("--db", default=None,
                        help="库副本路径（**必填**；缺省取正式库会被拒绝）")
    parser.add_argument("--repeats", type=int, default=3, help="每项重复次数（取最快）")
    parser.add_argument("--allow-production", action="store_true",
                        help="真的要在正式库上量吗（默认拒绝）")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    dbFile = os.path.abspath(args.db or paths.db_file())
    if not args.allow_production:
        _refuseProduction(dbFile)
    if not os.path.isfile(dbFile):
        raise SystemExit("库文件不存在: %s" % dbFile)
    profile(dbFile, repeats=args.repeats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
