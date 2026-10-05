#! /usr/bin/env python3
#encoding: utf-8

#Filename: run_match.py
#Description: photo-browser 人脸匹配执行入口（步骤 6）
#
# 用法
# ----
#   # 看现状（人员 / 质心 / 待处理人脸）
#   python code\src\tools\run_match.py --status
#
#   # 重建全部质心（改过分桶规则或批量导入人脸之后）
#   python code\src\tools\run_match.py --rebuild-centroids
#
#   # 跑一遍匹配，只看结果不落库（默认行为）
#   python code\src\tools\run_match.py --run
#
#   # 用「激进」阈值跑，并把自动归属落库
#   python code\src\tools\run_match.py --run --preset aggressive --assign
#
#   # 待确认队列（Top-5 候选）
#   python code\src\tools\run_match.py --queue --limit 20
#
#   # 一致性核对 / 修复
#   python code\src\tools\run_match.py --verify
#   python code\src\tools\run_match.py --sync-links --dry-run
#
#   # 指到临时库做实验（绝不动正式库）
#   python code\src\tools\run_match.py --run --db d:\tmp\test.db
#
# 硬约束
# ------
#   * photo 目录只读；本工具只碰数据库（读 pb_face/写 pb_face.personCode、
#     pb_photo_person、pb_person_centroid）
#   * 单写入者：只在主进程单线程跑
#   * --assign 只写 decision=auto 的；review/cluster **故意不落库**
#     （review 落库会让待确认队列以为已处理；cluster 落库会让步骤 7 没得聚）
#   * 幂等：同一批脸跑两次结果完全一致；再跑一次 --assign 不会产生新行

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
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                      # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon      # noqa: E402
from engine.match import bucket as bucket                               # noqa: E402
from engine.match import centroid as centroid                           # noqa: E402
from engine.match import matcher as matcher                             # noqa: E402
from processor.review import assigner as assigner                       # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("runMatch", "runmatch.log")

#: 待处理人脸 = personCode 为空（数据库设计.md D-4 修正后的口径）
#:
#: ⚠️ 修正前这里是「personCode 为空 **或** isConfirmed=0」。按 DR-16 的新语义
#:    （自动归属 = isConfirmed=0），那个条件会把**全部自动归属**卷进待确认队列
#:    （10 万张 ≈ 4~8 万条，队列爆炸，用户看一眼就放弃）。修正后的四态：
#:      待确认    = personCode IS NULL AND isStranger=0
#:      我不同意  = personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0
#:    ⚠️ isStranger **没有查询参数**（生成层只支持 recID/faceCode/photoCode/
#:       personCode + nullFields），所以「排除陌生人」在 Python 侧做 ——
#:       与 centroid.loadFaceVectors 同一个理由（不为一个筛选去改生成器 + 全库迁移）。
PENDING_NULL_FIELDS = ("personCode",)


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
        sqliteCommon.dbHandle(target)              # DR-10：显式才切库
    if not sqliteCommon.chkTableExist("pb_face"):
        if not quiet:
            print("[建库] 表不存在，先建库: %s" % target)
        from tools import build_db as build_db
        build_db.build(dbFile=target, verbose=not quiet)
    return target


def loadPendingFaces(limit: int = 0, offset: int = 0, onlyPending: bool = True) -> list:
    """取待判定的人脸行（**分页**，与步骤 3/5 同一理由）。

    onlyPending=True  -> personCode IS NULL **且 isStranger=0**（待确认队列）
    onlyPending=False -> 全部人脸（改了阈值之后要把已自动归属的脸也重跑一遍）
    """
    rows = sqliteCommon.query_pb_face(
        "pb_face", mode="full", orderBy="recID",
        nullFields=PENDING_NULL_FIELDS if onlyPending else (),
        limitNum=int(limit or 0), offsetNum=int(offset or 0))
    if not onlyPending:
        return rows
    return [row for row in rows if not int(row.get("isStranger") or 0)]


def countStates() -> dict:
    """四态计数（互斥，口径见 PENDING_NULL_FIELDS 上方的说明）。

    侧栏角标要**两个数**（待确认 / 我不同意），不能混用一个——
    混用的话「自动归属的 4 万张」会把待确认队列的角标顶成一个吓人的数字，
    而那 4 万张里的绝大多数用户其实并不打算逐张否决。
    """
    out = {"pending": 0, "disputed": 0, "confirmed": 0, "stranger": 0}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light"):
        person = str(row.get("personCode") or "")
        if int(row.get("isStranger") or 0):
            out["stranger"] += 1
        elif int(row.get("isConfirmed") or 0):
            out["confirmed"] += 1
        elif not person:
            out["pending"] += 1
        else:
            out["disputed"] += 1
    return out


# ============================================================
# 一、--status
# ============================================================

def cmdStatus(args) -> int:
    db = _ensureDb(args.db)
    print("库          :", db)
    people = sqliteCommon.query_pb_person("pb_person", mode="light")
    live = [p for p in people if str(p.get("delFlag") or "0") == "0"]
    print("人员        : %d 个（软删 %d）" % (len(live), len(people) - len(live)))
    withBirthday = [p for p in live if str(p.get("birthday") or "").strip()]
    print("  有生日    : %d 个（决定走自适应分桶还是等宽降级）" % len(withBirthday))
    centroidRows = sqliteCommon.query_pb_person_centroid("pb_person_centroid",
                                                         mode="light")
    enabled = [r for r in centroidRows
               if int(r.get("sampleCount") or 0) >= basicSettings.MIN_CENTROID_SAMPLES
               and r.get("centroid") is not None]
    allBucket = [r for r in enabled if str(r.get("bucketKey") or "") == bucket.ALL_BUCKET]
    print("质心        : %d 行，其中**启用** %d 行（sampleCount>=%d 且有向量）"
          % (len(centroidRows), len(enabled), basicSettings.MIN_CENTROID_SAMPLES))
    print("  含兜底桶  : %d 行（bucketKey=%s，样本不足时靠它兜住）"
          % (len(allBucket), bucket.ALL_BUCKET))
    print("质心口径    : 只用 isConfirmed=1（CENTROID_CONFIRMED_ONLY=%s）"
          % basicSettings.CENTROID_CONFIRMED_ONLY)
    states = countStates()
    faces = sum(states.values())
    print("人脸        : %d 条（待确认 %d / 我不同意 %d / 人工确认 %d / 陌生人 %d）"
          % (faces, states["pending"], states["disputed"],
             states["confirmed"], states["stranger"]))
    if states["disputed"]:
        print("  「我不同意」= 自动归属但未经人工确认（可一键改判，是纠错入口）")
    if states["stranger"]:
        print("  陌生人 %d 张已永久排除（不进待确认队列，也不参与聚类）"
              % states["stranger"])
    # 按桶看人脸分布：哪几个桶在挑大梁，一眼就能看出来
    dist = {}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light",
                                          nullFields=PENDING_NULL_FIELDS):
        if int(row.get("isStranger") or 0):
            continue
        key = str(row.get("shotBucket") or "(无年份)")
        dist[key] = dist.get(key, 0) + 1
    if dist:
        print("待确认人脸的拍摄桶分布（前 10）:")
        for key, count in sorted(dist.items(), key=lambda kv: -kv[1])[:10]:
            print("   %-12s %d" % (key, count))
    return 0


# ============================================================
# 二、--rebuild-centroids
# ============================================================

def cmdRebuild(args) -> int:
    _ensureDb(args.db)
    codes = ([args.person] if args.person else
             [str(p["personCode"]) for p in
              sqliteCommon.query_pb_person("pb_person", mode="light")])
    total = enabled = dropped = 0
    start = time.perf_counter()
    for code in codes:
        stat = centroid.recomputePerson(code)
        total += len(stat["buckets"])
        enabled += stat["enabled"]
        dropped += stat["dropped"]
        print("  %-10s 桶 %-2d 启用 %-2d 清僵尸桶 %d"
              % (code, len(stat["buckets"]), stat["enabled"], stat["dropped"]))
    print("共 %d 人：重算 %d 个桶，启用 %d，清僵尸桶 %d，耗时 %.2fs"
          % (len(codes), total, enabled, dropped, time.perf_counter() - start))
    return 0


# ============================================================
# 三、--run
# ============================================================

def cmdRun(args) -> int:
    db = _ensureDb(args.db)
    preset = args.preset or basicSettings.MATCH_THRESHOLD_PRESET
    tLow, tHigh = basicSettings.matchThresholds(preset)
    print("库          :", db)
    print("阈值预设    : %s  T_LOW=%.2f  T_HIGH=%.2f  相邻桶=%d  Top-%d"
          % (preset, tLow, tHigh, basicSettings.MATCH_NEIGHBOR_BUCKETS,
             basicSettings.MATCH_TOP_CANDIDATES))

    start = time.perf_counter()
    matrix, index = centroid.loadAllCentroids()
    loadSec = time.perf_counter() - start
    info = index.summary()
    print("质心装载    : %d 条向量 / %d 人 / %.2f MB，耗时 %.2fs"
          % (info["vectors"], info["persons"], info["megabytes"], loadSec))
    if info["skipped"]:
        print("  跳过 %d 行（sampleCount < %d 或无向量）"
              % (info["skipped"], basicSettings.MIN_CENTROID_SAMPLES))
    if not info["vectors"]:
        print("没有启用的质心 -> 所有人脸都会判成 cluster/no_centroid。")
        print("先跑 --rebuild-centroids（或先人工确认至少 3 张脸）。")
        return 0

    faces = loadPendingFaces(args.limit, 0, onlyPending=not args.all)
    if not faces:
        print("没有待判定的人脸。")
        return 0
    start = time.perf_counter()
    results = matcher.matchMany(faces, matrix=matrix, index=index, preset=preset)
    matchSec = time.perf_counter() - start
    stat = matcher.summarize(results)
    print("比对        : %d 张脸，耗时 %.3fs（%.2f ms/张）"
          % (stat["total"], matchSec, matchSec * 1000.0 / max(1, stat["total"])))
    print("判定分布    : 自动归属 %d / 待确认 %d / 进聚类 %d"
          % (stat["auto"], stat["review"], stat["cluster"]))
    print("  原因码    : %s" % json.dumps(stat["byReason"], ensure_ascii=False))

    if args.assign and stat["auto"]:
        writeStart = time.perf_counter()
        applied = assigner.applyAuto(results, dbFile=db)
        print("落库        : 写 %d 条（失败 %d），重算 %d 个桶，耗时 %.2fs"
              % (applied["written"], applied["failed"], applied["centroids"],
                 time.perf_counter() - writeStart))
    elif stat["auto"]:
        print("（未指定 --assign：自动归属的结果**没有落库**）")

    if args.detail:
        print("\n前 %d 条明细:" % min(len(results), args.detail))
        for one in results[:args.detail]:
            top = " | ".join("%s %.4f" % (c.displayName or c.personCode, c.score)
                             for c in one.topCandidates[:3])
            print("  %-34s %-10s %-8s %-14s score=%s  %s"
                  % (one.faceCode, one.bucketKey or "(无)", one.decision,
                     one.reason, "-" if one.score is None else "%.4f" % one.score,
                     top))
    return 0


# ============================================================
# 四、--queue（待确认队列）
# ============================================================

def cmdQueue(args) -> int:
    _ensureDb(args.db)
    preset = args.preset or basicSettings.MATCH_THRESHOLD_PRESET
    matrix, index = centroid.loadAllCentroids()
    faces = loadPendingFaces(args.limit, 0, onlyPending=True)
    if not faces:
        print("待确认队列为空。")
        return 0
    results = matcher.matchMany(faces, matrix=matrix, index=index, preset=preset)
    review = [r for r in results if r.decision == matcher.DECISION_REVIEW]
    review.sort(key=lambda r: (-(r.score or 0.0), r.faceCode))
    print("待确认 %d / 共 %d（预设 %s）" % (len(review), len(results), preset))
    for one in review[:args.limit or 20]:
        top = "; ".join("%s %.4f@%s" % (c.displayName or c.personCode,
                                       c.score, c.bucketKey)
                        for c in one.topCandidates)
        print("  %-34s 桶 %-10s score=%.4f  %s"
              % (one.faceCode, one.bucketKey or "(无)", one.score or 0.0, top))
    return 0


# ============================================================
# 五、--verify / --sync-links
# ============================================================

def cmdVerify(args) -> int:
    _ensureDb(args.db)
    report = assigner.verifyLinks(args.person)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["clean"] else 1


def cmdSyncLinks(args) -> int:
    _ensureDb(args.db)
    stat = assigner.syncLinks(args.person, dryRun=args.dry_run)
    print(json.dumps(stat, ensure_ascii=False, indent=2))
    return 0


# ============================================================
# 六、--buckets（只算桶，不碰库）
# ============================================================

def cmdBuckets(args) -> int:
    print("分桶规则    : age<=%d 每 %d 年 / age>%d 每 %d 年 / 出生年未知降级等宽 %d 年"
          % (bucket.CHILD_MAX_AGE, bucket.CHILD_WIDTH, bucket.CHILD_MAX_AGE,
             bucket.ADULT_WIDTH, bucket.EQUAL_WIDTH))
    print("相邻桶      : 本桶 + 前后各 %d 桶（[B0-1, B0, B0+1]）"
          % basicSettings.MATCH_NEIGHBOR_BUCKETS)
    for shotYear in (args.year or []):
        for born in (args.birth or [None]):
            key = bucket.bucketKeyAdaptive(shotYear, born)
            print("  shotYear=%-6s birthYear=%-6s age=%-3s -> %-10s 邻居 %s"
                  % (shotYear, born or "未知",
                     bucket.ageOf(shotYear, born), key or "(空/不参与比对)",
                     bucket.neighborBucketKeys(key)))
    return 0


# ============================================================
# main
# ============================================================

def buildParser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="photo-browser 人脸分桶 + 质心 + 匹配（步骤 6）")
    p.add_argument("--db", default="", help="库文件（默认正式库；给了就只动那个库）")
    p.add_argument("--preset", default="",
                   choices=sorted(basicSettings.MATCH_THRESHOLD_PRESETS.keys()),
                   help="阈值预设（缺省 %s）" % basicSettings.MATCH_THRESHOLD_PRESET)
    p.add_argument("--person", default="", help="只针对某个人（personCode）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少张脸")
    p.add_argument("--detail", type=int, default=0, help="打印前 N 条明细")
    p.add_argument("--year", type=int, action="append", help="--buckets 用的拍摄年")
    p.add_argument("--birth", type=int, action="append", help="--buckets 用的出生年")
    p.add_argument("--status", action="store_true", help="看现状")
    p.add_argument("--rebuild-centroids", action="store_true", help="重建全部质心")
    p.add_argument("--run", action="store_true", help="跑匹配")
    p.add_argument("--all", action="store_true",
                   help="连已自动归属的脸也重跑（改了阈值之后用）")
    p.add_argument("--assign", action="store_true", help="把 auto 结果落库")
    p.add_argument("--queue", action="store_true", help="看待确认队列")
    p.add_argument("--verify", action="store_true", help="一致性核对")
    p.add_argument("--sync-links", action="store_true", help="按 pb_face 重建关联行")
    p.add_argument("--dry-run", action="store_true", help="--sync-links 只报不写")
    p.add_argument("--buckets", action="store_true", help="只演示分桶规则")
    return p


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv)
    print("run_match.py _VERSION:", _VERSION)
    if args.status:
        return cmdStatus(args)
    if args.rebuild_centroids:
        return cmdRebuild(args)
    if args.run:
        return cmdRun(args)
    if args.queue:
        return cmdQueue(args)
    if args.verify:
        return cmdVerify(args)
    if args.sync_links:
        return cmdSyncLinks(args)
    if args.buckets:
        return cmdBuckets(args)
    buildParser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
