#! /usr/bin/env python3
#encoding: utf-8

#Filename: place_cli.py
#Description: 地点维度的命令行入口（步骤 R4b）—— 目录名线索接入 + 巡检 + 全链路重建
#
# 为什么需要它（而不是只留 HTTP 接口）
# ----------------------------------
#   ① `placeNameDir` 的填列**不在扫描链路上**（本轮刻意不改扫描器，见下），
#      只有工具能触发 —— 目录改名之后必须手动跑。
#   ② 地点归并得对不对**看界面看不出来**（DR-29）：114 张照片被归成 1 个地点
#      还是 114 个，必须看**清单**。`--audit` 就是那份清单。
#
# 四个动作（互斥，一次只做一个）
# ----------------------------
#   --scan-dir            从 relPath 解析并填 `pb_photo.placeNameDir`
#                         （缺省 **dry-run**：只报告将填多少行 + 完整采纳/排除清单；
#                           实跑要加 --apply）
#   --audit               巡检报告（七项，本步的主要验收产物）
#   --rebuild             全链路：scan-dir -> rebuildPlaces ->（可选）rebuildNameZh
#   --photo <photoCode>   单张照片的地点解析过程（排障用）
#
#   --rebuild-only        **只跑 rebuildPlaces**（等价于 `POST /api/places/rebuild`）。
#                         发现「该填没填」时会**明确拒绝**并提示先跑 --scan-dir ——
#                         这就是那个「反序不会静默给出旧结果」的闸（验收第 21 条）。
#
# ⚠️ 硬顺序（本文件的核心知识，别改）
# --------------------------------
#     ① scanDirNames()            填 pb_photo.placeNameDir
#     ② placeStore.rebuildPlaces() 按新聚合键（目录名优先）重建地点字典
#     ③ placeNameZh.rebuildNameZh()（可选）补 GPS 地点的中文名
#   反了会怎样：先 ② 再 ① ⇒ 这一轮重建读到的 `placeNameDir` 还是空的（或旧值），
#   **新填的目录名这一轮完全不生效**，而 `rebuildPlaces()` 会打印
#   「更新 N 个地点」，看起来完全正常。
#   ⚠️ 这三步的**唯一实现**在 `processor/place/placeFinalize.finalizePlaces()`，
#      本文件的 `--rebuild` 只是它的一个入口（扫描收尾是另一个入口）——
#      别在本文件里再写一遍顺序。
#
# ⚠️ 什么时候要人动手（**新增不用，改名才用**）
# ---------------------------------------
#   · 新增目录 / 新照片：**自动** —— 扫描跑完（DONE）后由
#     `scanScheduler.runBatch` 触发的收尾步骤处理（`PLACE_FINALIZE_AFTER_SCAN`）。
#   · 目录改名：**必须手动**两条 ——
#       python code\src\tools\place_cli.py --scan-dir --apply --refresh
#       python code\src\tools\place_cli.py --rebuild-only
#     为什么自动收尾不代劳：改名的旧值与"用户手工改过的值"在数据上**长得一样**，
#     自动覆盖会静默吃掉用户填过的名字 ⇒ 收尾只填空，并把**漂移**报出来。
#   · 判据认不出来（新命名风格，如 `2016_05_01_三亚`）：进「疑似地点」清单
#     （只报告、不改采纳），确认后加 `basicSettings.DIR_PLACE_ALIAS` 或黑名单。
#   ⚠️ 扫描器（runner）从头到尾不写 `placeNameDir`；重扫只更新 `relPath`。
#
# 硬约束：只改数据库里的列；**photo 目录只读**（本工具根本不碰文件）。
#
# 硬约束：只改数据库里的列；**photo 目录只读**（本工具根本不碰文件）。

import argparse
import json
import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))              # .../src/tools
_SRC_DIR = os.path.dirname(_HERE_DIR)                              # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.place import dirNamePlace as dirNamePlace          # noqa: E402
from processor.place import placeStore                            # noqa: E402

_VERSION = "20261008"

#: `--audit` 里「地点清单」的打印条数上限（0 = 全部）。
#: ⚠️ 默认全部：这份清单是给人**核对**的（DR-29：地点归得对不对必须看清单），
#:    截断成 top N 就失去意义了。地点数是几十~几百量级，打得下。
_SAMPLE_ROWS: int = 0


def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _line(char="-", width=78) -> str:
    return char * width


def _rule(title: str) -> None:
    print("")
    print(_line("="))
    print(title)
    print(_line("="))


# ============================================================
# 一、--scan-dir
# ============================================================
def doScanDir(apply: bool, refresh: bool, dbFile: str, asJson: bool) -> int:
    """填列。**缺省 dry-run**（改生产数据必须显式 --apply，与 fix_placeholder_geo 同纪律）。"""
    report = dirNamePlace.scanDirNames(dryRun=not apply, dbFile=dbFile or None,
                                       refresh=refresh)
    if asJson:
        print(json.dumps(report, ensure_ascii=False, default=str))
        return 0

    print("库                : %s" % report["dbFile"])
    print("模式              : %s%s"
          % ("**实跑（写库）**" if apply else "dry-run（未写库）",
             " + **--refresh（覆盖漂移行）**" if refresh else ""))
    print("")
    print("---- pb_photo 字段级总数 ----")
    stat = report["fieldStats"]
    print("  照片总行数                    : %d" % stat["total"])
    print("  placeNameDir 非空             : %d" % stat["placeNameDir"])
    print("  placeName   非空              : %d" % stat["placeName"])
    print("  两者都空（聚合键为空）        : %d" % stat["bothEmpty"])
    print("")
    print("---- 本次动作 ----")
    print("  将填 / 已填 placeNameDir      : %d（实跑写入 %d）"
          % (report["candidates"], report["written"]))
    print("  已有值、原样不动（只填空）    : %d（其中本次按 --refresh 覆盖 %d）"
          % (report["alreadyFilled"], report["refreshed"]))
    print("  解不出名字（进不了字典）      : %d（其中从祖父目录取到的 %d）"
          % (report["noClue"], report["viaAncestor"]))
    print("")
    print("---- 疑似地点（被排除、但像地名；只报告不改采纳，%d 个）----"
          % len(report["suspects"]))
    if report["suspects"]:
        print("  %-34s %5s  %s" % ("目录名", "张数", "为什么被报出来"))
        for one in report["suspects"]:
            print("  %-34s %5d  %s"
                  % (one["dir"][:34], one["count"], one["hint"]))
        print("  ⚠️ 确认是地点 ⇒ basicSettings.DIR_PLACE_ALIAS（`目录名=地点名`；"
              "左侧以 `^` 开头可写**正则**，一条盖一族）")
        print("     确认不是   ⇒ basicSettings.DIR_PLACE_CONFIRMED_NOT_PLACE"
              "（记录一下、往后不再报；它**不改变判定**）")
    else:
        print("  （无）")
    print("")
    print("---- 漂移检查（库里有值 ≠ 现在解出来的值，%d 行）----" % len(report["drift"]))
    if report["drift"]:
        print("  %-30s %-22s %-22s %s" % ("photoCode", "库里存量", "现在解出", "relPath"))
        for one in report["drift"][:40]:
            print("  %-30s %-22s %-22s %s"
                  % (one["photoCode"][:30], one["old"][:22], one["now"][:22],
                     one["relPath"][:60]))
        if len(report["drift"]) > 40:
            print("  …… 还有 %d 行" % (len(report["drift"]) - 40))
        print("  ⚠️ 目录改过名（或用户手工改过值）。默认**不覆盖**；")
        print("     确认要按现在磁盘上的目录名刷新，加 --refresh（它只覆盖漂移行）。")
    else:
        print("  （无）")
    print("")
    print("---- 采纳 / 排除清单（按父目录名，全量 %d 个，按张数降序）----"
          % len(report["dirStats"]))
    print("  %-34s %5s  %-6s %-20s %s" % ("目录名", "张数", "判定", "地点名", "理由"))
    for one in report["dirStats"]:
        print("  %-34s %5d  %-6s %-20s %s"
              % (one["dir"][:34], one["count"],
                 "采纳" if one["isPlace"] else "排除",
                 one["placeName"] or "-", one["reasonText"]))
    if report["samples"]:
        print("")
        print("---- 采纳样例（relPath -> placeNameDir）----")
        for one in report["samples"]:
            print("  %-60s -> %s" % (one["relPath"][:60], one["placeNameDir"]))
    print("")
    for line in report["nextSteps"]:
        print("  >", line)
    return 0


# ============================================================
# 二、--audit（七项）
# ============================================================
def collectAudit(dbFile: str) -> dict:
    """把七项巡检数据收集成一个 dict（打印与 --json 共用一份数据）。"""
    scan = dirNamePlace.scanDirNames(dryRun=True, dbFile=dbFile or None)
    listed = placeStore.listPlaces(delFlag="*", limitNum=0)
    places = list(listed["items"])
    ghosts = [one for one in places if int(one.get("photoCount") or 0) == 0]
    return {
        "dbFile": scan["dbFile"],
        "fieldStats": scan["fieldStats"],
        "pending": {"candidates": scan["candidates"],
                    "alreadyFilled": scan["alreadyFilled"],
                    "noClue": scan["noClue"],
                    "viaAncestor": scan["viaAncestor"]},
        "drift": scan["drift"],
        "suspects": scan["suspects"],
        "dirStats": scan["dirStats"],
        "places": places,
        "ghosts": ghosts,
        "orphans": placeStore.orphanNameZh(),
        "duplicateNameZh": placeStore.duplicateNameZhRows(),
        "noCenter": placeStore.placesWithoutCenter(),
        "missingTable": bool(listed.get("missingTable")),
    }


def doAudit(dbFile: str, asJson: bool) -> int:
    """巡检报告（**纯读**，一行都不写）。"""
    data = collectAudit(dbFile)
    if asJson:
        print(json.dumps(data, ensure_ascii=False, default=str))
        return 0

    print("库                : %s" % data["dbFile"])
    print("placeStore._VERSION: %s   dirNamePlace._VERSION: %s"
          % (placeStore._VERSION, dirNamePlace._VERSION))

    # ---- ① 字段级总数 ----
    _rule("① 字段级总数（pb_photo）")
    stat = data["fieldStats"]
    print("  照片总数                      : %d" % stat["total"])
    print("  placeNameDir 非空             : %d" % stat["placeNameDir"])
    print("  placeName   非空              : %d" % stat["placeName"])
    print("  两者都空（聚合键为空）        : %d" % stat["bothEmpty"])
    print("  ⚠️ 进地点字典的行数 = placeNameDir 非空 + (placeName 非空且无目录名)")

    # ---- ② 采纳/排除清单 ----
    _rule("② 采纳 / 排除清单（父目录名，全量 %d 个）" % len(data["dirStats"]))
    adopted = [one for one in data["dirStats"] if one["isPlace"]]
    print("  采纳 %d 个目录 / 合计 %d 张；排除 %d 个目录 / 合计 %d 张"
          % (len(adopted), sum(one["count"] for one in adopted),
             len(data["dirStats"]) - len(adopted),
             sum(one["count"] for one in data["dirStats"] if not one["isPlace"])))
    print("  %-34s %5s  %-6s %-20s %s" % ("目录名", "张数", "判定", "地点名", "理由"))
    for one in data["dirStats"]:
        print("  %-34s %5d  %-6s %-20s %s"
              % (one["dir"][:34], one["count"],
                 "采纳" if one["isPlace"] else "排除",
                 one["placeName"] or "-", one["reasonText"]))
    print("  ⚠️ 以上是**父目录名**的判定；若某些行是靠祖父目录取到的名字，"
          "它们已计入「将填」但不在上面（本次 %d 行）"
          % data["pending"]["viaAncestor"])
    if data["drift"]:
        print("  ⚠️ **漂移 %d 行**（库里的 placeNameDir ≠ 现在从 relPath 解出来的值）"
              "—— 目录改过名而字典没跟上。修法：--scan-dir --apply --refresh 然后 --rebuild-only"
              % len(data["drift"]))
        for one in data["drift"][:10]:
            print("      %-30s %-20s -> %-20s %s"
                  % (one["photoCode"][:30], one["old"][:20], one["now"][:20],
                     one["relPath"][:56]))
    print("  ⚠️ **疑似地点 %d 个**（被排除、但像地名 —— 判据没认出的新写法？）"
          % len(data["suspects"]))
    for one in data["suspects"]:
        print("      %-34s %5d 张  %s" % (one["dir"][:34], one["count"], one["hint"]))

    # ---- ③ 地点清单 ----
    _rule("③ 地点清单（pb_place 全表，含软删；共 %d 行）" % len(data["places"]))
    print("  %-32s %-26s %-6s %6s %5s %5s %9s %9s"
          % ("placeCode", "placeName", "nameZh", "source", "count", "起", "centerLat", "centerLon"))
    for one in data["places"]:
        print("  %-32s %-26s %-6s %6s %5s %5s %9s %9s"
              % (str(one.get("placeCode") or "")[:32],
                 str(one.get("placeName") or "")[:26],
                 str(one.get("nameZh") or "-")[:6],
                 one.get("source"), one.get("photoCount"),
                 one.get("firstShotYear"),
                 str(one.get("centerLat"))[:9], str(one.get("centerLon"))[:9]))
    totalCount = sum(int(one.get("photoCount") or 0) for one in data["places"])
    print("  photoCount 合计 = %d" % totalCount)

    # ---- ④ 幽灵行 ----
    _rule("④ 幽灵行（photoCount = 0，%d 行）" % len(data["ghosts"]))
    for one in data["ghosts"]:
        print("  %-32s %-26s nameZh=%-14s source=%s"
              % (str(one.get("placeCode") or "")[:32],
                 str(one.get("placeName") or "")[:26],
                 str(one.get("nameZh") or "-"), one.get("source")))
    if not data["ghosts"]:
        print("  （无）")

    # ---- ⑤ 孤儿 nameZh ----
    _rule("⑤ 孤儿 nameZh（photoCount=0 但 nameZh 非空，%d 行）" % len(data["orphans"]))
    for one in data["orphans"]:
        print("  %-32s %-26s nameZh=%-16s source=%s"
              % (str(one.get("placeCode") or "")[:32],
                 str(one.get("placeName") or "")[:26],
                 str(one.get("nameZh") or "-"), one.get("source")))
    if not data["orphans"]:
        print("  （无。⚠️ 它是「改名/搬照片把用户填过的中文名变成孤儿」的警报器，")
        print("     为 0 才算干净 —— 为 0 不代表这条检查没跑，见第 ⑤ 项的构造办法）")

    # ---- ⑥ 重名 nameZh ----
    _rule("⑥ 重名 nameZh（同一个中文名挂多行，DR-36；%d 组）"
          % len(data["duplicateNameZh"]))
    for one in data["duplicateNameZh"]:
        print("  nameZh=%-22s 行数=%-3s 照片合计=%-5s 涉及: %s"
              % (str(one.get("nameZh") or "")[:22], one.get("rowNum"),
                 one.get("photoCount"), one.get("placeNames")))
    if not data["duplicateNameZh"]:
        print("  （无）")
    print("  ⚠️ R4b **只报告不归并**（DR-36 明确要求：三种处理思路要等用户定）")

    # ---- ⑦ 无中心点 ----
    _rule("⑦ 无中心点地点（centerLat 为 NULL，%d 个）" % len(data["noCenter"]))
    for one in data["noCenter"]:
        print("  %-32s %-26s count=%-5s nameZh=%s"
              % (str(one.get("placeCode") or "")[:32],
                 str(one.get("placeName") or "")[:26],
                 one.get("photoCount"), str(one.get("nameZh") or "-")))
    print("  ⚠️ 目录名地点**全部**在这里（DR-34：它们没有 GPS，诚实留 NULL）——")
    print("     地图要**跳过并报告**，不要给它们猜一个坐标（猜出来的点会以假乱真）")
    return 0


# ============================================================
# 三、--rebuild / --rebuild-only
# ============================================================
def doRebuild(apply: bool, onlyRebuild: bool, skipNameZh: bool,
              force: bool, dbFile: str) -> int:
    """全链路（或只 rebuild），**按硬顺序**执行。"""
    pending = dirNamePlace.pendingScanCount(dbFile or None)
    print("顺序闸：未填 placeNameDir %d 行，其中**现在能解出名字**的 %d 行"
          % (pending["unfilled"], pending["candidates"]))

    if onlyRebuild:
        if pending["candidates"] > 0 and not force:
            print("")
            print("[拒绝执行] 你正在**反序**执行：placeNameDir 还有 %d 行该填没填。"
                  % pending["candidates"])
            print("           先 rebuild 再填列 ⇒ 这一轮重建读到的还是旧聚合键，")
            print("           新目录名**完全不生效** —— 而 rebuildPlaces 仍会打印")
            print("           「更新 N 个地点」，看起来一切正常。")
            print("")
            print("           正确顺序（**必须先 scan-dir**）：")
            print("             python code\\src\\tools\\place_cli.py --scan-dir --apply")
            print("             python code\\src\\tools\\place_cli.py --rebuild-only")
            print("           （或者直接 --rebuild —— 它会自己按正确顺序走完全链路）")
            print("           确认要用旧口径跑一遍，加 --force。")
            return 1
        if pending["candidates"] > 0:
            print("[--force] 明知有 %d 行该填未填，仍按旧聚合键重建。"
                  % pending["candidates"])
    else:
        # ---- 全链路：三步的唯一实现是 placeFinalize.finalizePlaces() ----
        # ⚠️ 本文件**不再自己写一遍 ① ② ③**：顺序是这套逻辑的核心知识，
        #    两份实现必然在某次改动后漂移（照抄 placeStore 文件头那条纪律）。
        from processor.place import placeFinalize as placeFinalize
        report = placeFinalize.finalizePlaces(dbFile=dbFile or None,
                                              withNameZh=not skipNameZh,
                                              source="cli:--rebuild")
        _printFinalize(report)
        print("")
        print(_line("="))
        print("⚠️ 什么时候还要人动手（**新增不用，改名才用**）")
        print("   新增目录 / 新照片：扫描跑完会自动收尾（PLACE_FINALIZE_AFTER_SCAN），无需操作")
        print("   目录改名：必须手动两条 ——")
        print("     python code\\src\\tools\\place_cli.py --scan-dir --apply --refresh")
        print("     python code\\src\\tools\\place_cli.py --rebuild-only")
        print("   （自动收尾**只填空不覆盖**：改名旧值与手工改过的值长得一样，")
        print("     工具不敢替你判断 —— 所以它只把「漂移」报出来。）")
        return 0 if report.get("ok") else 1

    # ---- `--rebuild-only`：只做 ②（等价 POST /api/places/rebuild）----
    print("")
    print("② placeStore.rebuildPlaces（按聚合键：目录名优先）")
    info = placeStore.rebuildPlaces(dbFile=dbFile or None)
    print("   地点数 %d（新建 %d / 更新 %d）｜进字典照片 %d ｜未命名 %d"
          % (info["placeCount"], info["created"], info["updated"],
             info["total"], info["unnamed"]))
    print("   归零 %d 行（手工行拒绝归零 %d 行）"
          % (info["zeroed"], info["manualZeroSkipped"]))
    if info["orphanCount"]:
        print("   ⚠️ 孤儿 nameZh %d 条（photoCount=0 但中文名非空）—— 请人工确认："
              % info["orphanCount"])
        for one in info["orphans"]:
            print("      %-32s %-24s nameZh=%s"
                  % (str(one.get("placeCode") or "")[:32],
                     str(one.get("placeName") or "")[:24],
                     str(one.get("nameZh") or "-")))
    else:
        print("   孤儿 nameZh：0 条")

    # ---- ③ 可选：补 GPS 地点的中文名 ----
    if skipNameZh:
        print("")
        print("③ rebuildNameZh 已按 --skip-name-zh 跳过")
    else:
        from processor.place import placeNameZh as placeNameZh
        print("")
        print("③ placeNameZh.rebuildNameZh（只填 NULL，绝不覆盖）")
        zh = placeNameZh.rebuildNameZh(dbFile=dbFile or None)
        print("   候选 %d 行，补了 %d 行（其中手工行 %d）｜reason 分布 %s"
              % (zh["candidates"], zh["filled"], zh["manualFilled"], zh["byReason"]))

    print("")
    print("⚠️ 本命令**只重建字典、不补扫目录名**（= POST /api/places/rebuild 的口径）。")
    print("   有「该填没填」的行时会被上面的顺序闸拦下 —— 那是刻意的：")
    print("   先 rebuild 再填列 ⇒ 新目录名这一轮完全不生效，而输出看着一切正常。")
    return 0


def _printFinalize(report: dict) -> None:
    """打印 `placeFinalize.finalizePlaces()` 的报告（CLI 共用一份排版）。"""
    print("")
    print("① scanDirNames（填 pb_photo.placeNameDir；只填空）")
    scan = report.get("scan") or {}
    if scan:
        print("   已写 %d 行；已有值原样不动 %d 行；解不出名字 %d 行"
              % (scan.get("written"), scan.get("alreadyFilled"), scan.get("noClue")))
    else:
        print("   ⚠️ 第①步没跑成（见下面的 errors）—— 第②步仍然执行了")
    print("")
    print("② placeStore.rebuildPlaces（按聚合键：目录名优先）")
    info = report.get("rebuild") or {}
    if info:
        print("   地点数 %d（新建 %d / 更新 %d）｜进字典照片 %d ｜未命名 %d"
              % (info.get("placeCount"), info.get("created"), info.get("updated"),
                 info.get("total"), info.get("unnamed")))
        print("   归零 %d 行（手工行拒绝归零 %d 行）"
              % (info.get("zeroed"), info.get("manualZeroSkipped")))
        orphans = list(info.get("orphans") or [])
        if orphans:
            print("   ⚠️ 孤儿 nameZh %d 条（photoCount=0 但中文名非空）—— 请人工确认："
                  % len(orphans))
            for one in orphans:
                print("      %-32s %-24s nameZh=%s"
                      % (str(one.get("placeCode") or "")[:32],
                         str(one.get("placeName") or "")[:24],
                         str(one.get("nameZh") or "-")))
        else:
            print("   孤儿 nameZh：0 条")
    else:
        print("   ⚠️ 第②步没跑成（见下面的 errors）")
    print("")
    print("③ placeNameZh.rebuildNameZh（只填 NULL，绝不覆盖）")
    zh = report.get("nameZh")
    if zh:
        print("   候选 %d 行，补了 %d 行（其中手工行 %d）｜reason 分布 %s"
              % (zh.get("candidates"), zh.get("filled"), zh.get("manualFilled"),
                 zh.get("byReason")))
    else:
        print("   （已按 --skip-name-zh 跳过）")
    print("")
    print("---- 漂移 / 疑似地点（都**不覆盖、不采纳**，只报出来）----")
    drift = list(report.get("drift") or [])
    suspects = list(report.get("suspects") or [])
    if drift:
        print("  ⚠️ 漂移 %d 行（目录改名）：%s"
              % (len(drift), ", ".join("%s -> %s" % (one.get("old"), one.get("now"))
                                       for one in drift[:5])))
        print("     处理办法：--scan-dir --apply --refresh（显式覆盖漂移行）")
    else:
        print("  漂移：0 行")
    if suspects:
        print("  ⚠️ 疑似地点 %d 个（判据没认出的新写法）：%s"
              % (len(suspects), ", ".join("%s(%d 张)" % (one.get("dir"), one.get("count"))
                                         for one in suspects[:5])))
        print("     确认是地点 ⇒ DIR_PLACE_ALIAS（`目录名=地点名`）；不是 ⇒ DIR_PLACE_BLACKLIST")
    else:
        print("  疑似地点：0 个")
    errors = list(report.get("errors") or [])
    print("")
    print("  用时 %.2fs｜%s" % (report.get("elapsed") or 0,
                              "三步都成功" if report.get("ok") else "有失败步骤"))
    for one in errors:
        print("  !! %s" % one)


# ============================================================
# 四、--photo（单张排障）
# ============================================================
def doPhoto(photoCode: str, dbFile: str) -> int:
    """把一张照片的地点**怎么算出来的**逐步打出来（排障用）。"""
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=str(photoCode or ""),
                                       delFlag="*")
    if not rows:
        print("[Error] 找不到 photoCode = %r 的照片" % photoCode)
        return 1
    row = rows[0]
    relPath = str(row.get("relPath") or "")
    print("photoCode        : %s" % row.get("photoCode"))
    print("relPath          : %s" % relPath)
    print("delFlag          : %s" % row.get("delFlag"))
    print("")
    print("---- 逐层看目录名（DIR_PLACE_MAX_UP=%s）----"
          % dirNamePlace.basicSettings.DIR_PLACE_MAX_UP)
    for up in range(1, 4):
        name = dirNamePlace._dirNameOf(relPath, up)
        if not name:
            print("  第%d层：不存在（再往上就是 photo 根，止）" % up)
            break
        got = dirNamePlace.parseDirName(name)
        print("  第%d层：%-34s %-5s placeName=%-20s %s"
              % (up, name[:34], "采纳" if got["isPlace"] else "排除",
                 got["placeName"] or "-", got["reasonText"]))
    print("")
    got = dirNamePlace.resolveDirPlace(relPath)
    print("解析结果         : level=%s dirName=%r -> placeNameDir=%r"
          % (got.get("level"), got.get("dirName"), got.get("placeName")))
    print("库里落的值       : placeNameDir=%r" % row.get("placeNameDir"))
    print("                   placeName   =%r（GPS 逆地理；目录名地点这里**本来就是空**）"
          % row.get("placeName"))
    if not str(row.get("placeNameDir") or "") and got.get("placeName"):
        print("    ⚠️ 库里 placeNameDir 是空、而解析得出名字 ⇒ 还没跑 --scan-dir"
              "（或它是新照片 / 刚改过名）")
    elif str(row.get("placeNameDir") or "") and \
            str(row.get("placeNameDir")) != str(got.get("placeName") or ""):
        print("    ⚠️ 库里 placeNameDir 与解析结果**不一致** ⇒ 目录改过名（漂移）。"
              "默认不覆盖：需要 --scan-dir --apply --refresh")
    key = placeStore.placeKeyOf(row.get("placeName"), row.get("placeNameDir"))
    code = placeStore.makePlaceCode(key)
    print("聚合键（有效地点）: %r" % key)
    print("派生 placeCode   : %r" % code)
    found = sqliteCommon.query_pb_place("pb_place", placeCode=code, delFlag="*") \
        if code else []
    if not found:
        print("pb_place 里的行  : （没有 —— 还没 rebuild，或该地点已被改名后归零）")
    else:
        one = found[0]
        print("pb_place 里的行  : placeName=%r nameZh=%r source=%s photoCount=%s "
              "center=(%s, %s) delFlag=%s"
              % (one.get("placeName"), one.get("nameZh"), one.get("source"),
                 one.get("photoCount"), one.get("centerLat"), one.get("centerLon"),
                 one.get("delFlag")))
        print("显示名（契约）   : nameZh ?? placeName = %r"
              % (one.get("nameZh") or one.get("placeName")))
    return 0


# ============================================================
# 五、命令行
# ============================================================
def main(argv=None) -> int:
    _fixConsole()
    parser = argparse.ArgumentParser(
        prog="place_cli.py",
        description="地点维度命令行（步骤 R4b）：目录名线索接入 / 巡检 / 全链路重建",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan-dir", action="store_true",
                       help="从 relPath 解析并填 pb_photo.placeNameDir（缺省 dry-run）")
    group.add_argument("--audit", action="store_true", help="巡检报告（七项）")
    group.add_argument("--rebuild", action="store_true",
                       help="全链路：scan-dir -> rebuildPlaces ->（可选）rebuildNameZh")
    group.add_argument("--rebuild-only", action="store_true", dest="rebuildOnly",
                       help="只跑 rebuildPlaces（等价 POST /api/places/rebuild）；"
                            "反序时会明确拒绝")
    group.add_argument("--photo", default="", help="单张照片的地点解析过程（排障）")
    parser.add_argument("--apply", action="store_true",
                        help="--scan-dir 实跑写库（缺省 dry-run，只报不改）")
    parser.add_argument("--refresh", action="store_true",
                        help="--scan-dir 覆盖**漂移行**（库里存量 ≠ 现在解出来的值）。"
                             "目录改名后必须加它，否则旧名字永远改不掉；"
                             "缺省绝不覆盖（用户手工改过的值要留着）")
    parser.add_argument("--force", action="store_true",
                        help="--rebuild-only 在「该填没填」时仍按旧聚合键跑")
    parser.add_argument("--skip-name-zh", action="store_true", dest="skipNameZh",
                        help="--rebuild 跳过 rebuildNameZh（不补中文名）")
    parser.add_argument("--json", action="store_true", help="--scan-dir/--audit 输出 JSON")
    parser.add_argument("--db", default="", help="库文件（缺省正式库）")
    args = parser.parse_args(argv)

    print("place_cli.py _VERSION : %s" % _VERSION)
    print("dirNamePlace._VERSION : %s" % dirNamePlace._VERSION)
    print("placeStore._VERSION   : %s" % placeStore._VERSION)
    _bs = dirNamePlace.basicSettings
    print("判据 DIR_PLACE_PATTERN: %s" % _bs.DIR_PLACE_PATTERN)
    # 黑名单有几十项（已人工确认的排除项），逐条打印会把每次命令的输出淹掉，
    # 所以这里只报**项数**；要看内容去 basicSettings 或 --scan-dir 的清单。
    print("黑名单/已确认/别名表  : %d 项 / %d 项 / %d 项"
          % (len(_bs.DIR_PLACE_BLACKLIST), len(_bs.DIR_PLACE_CONFIRMED_NOT_PLACE),
             len(_bs.DIR_PLACE_ALIAS)))
    print("疑似门槛              : 含年份 >=%d 张 / 含中文 >=%d 张"
          % (_bs.DIR_PLACE_SUSPECT_MIN_YEAR, _bs.DIR_PLACE_SUSPECT_MIN))
    print("扫描后自动收尾        : %s（含中文名 %s）"
          % (_bs.PLACE_FINALIZE_AFTER_SCAN, _bs.PLACE_FINALIZE_NAME_ZH))

    if args.scan_dir:
        return doScanDir(args.apply, args.refresh, args.db, args.json)
    if args.audit:
        return doAudit(args.db, args.json)
    if args.rebuild:
        return doRebuild(True, False, args.skipNameZh, args.force, args.db)
    if args.rebuildOnly:
        return doRebuild(False, True, args.skipNameZh, args.force, args.db)
    if args.photo:
        return doPhoto(args.photo, args.db)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
