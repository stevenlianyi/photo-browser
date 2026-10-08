#! /usr/bin/env python3
#encoding: utf-8

#Filename: placeFinalize.py
#Description: 地点侧收尾（填 placeNameDir → 重建 pb_place → 补 nameZh）—— 步骤 R4b 收尾
#
# 为什么要单独一个模块（而不是让调用方自己按顺序调三个函数）
# ------------------------------------------------------
#   「硬顺序」本身就是这套逻辑的核心知识，**只能有一份实现**：
#       ① dirNamePlace.scanDirNames()   填 pb_photo.placeNameDir
#       ② placeStore.rebuildPlaces()    按聚合键（目录名优先）重建字典
#       ③ placeNameZh.rebuildNameZh()  （可选）补 GPS 地点的中文名
#   ⚠️ 顺序反了会怎样：先 ② 再 ① ⇒ 这一轮重建读到的 `placeNameDir` 还是空的
#      （或上一轮的旧值），**新填的目录名这一轮完全不生效** —— 而
#      `rebuildPlaces()` 会照常打印「更新 N 个地点」，看起来一切正常。
#      这正是 R4b 最容易踩、最难发现的坑，所以顺序不能靠调用方记得住：
#      扫描收尾（`scanScheduler.runBatch` 的 DONE 分支）与 CLI（`--rebuild`）
#      都走这里，一份实现。
#
# 三条纪律（缺一条就会在某个真实场景里静默出错）
# ------------------------------------------
#   ① **失败互不牵连**：三步各自 try/except，异常进 `errors` 并记日志。
#      ① 失败仍要跑 ②（不能因为"填列失败"就不重建字典 —— 那会把一次小故障
#      放大成"地点全都没了"）；任何一步失败都**不影响扫描任务本身**
#      （扫描已经 DONE 了，不能因为收尾出问题把它改成 FAILED）。
#   ② **只填空、不覆盖**：`refresh` 默认 False。自动收尾**绝不允许**覆盖已有值 ——
#      「目录改名留下的旧值」与「用户手工改过的值」在数据上长得一模一样，
#      工具没有资格替人判断。改名走显式 `--scan-dir --apply --refresh`。
#      收尾会把**漂移**（旧值 ≠ 现在解出的值）报出来，让人看见。
#   ③ **报告不静默**：返回值带 `drift`（改名证据）与 `suspects`（疑似地点：
#      判据没认出来的新写法），调用方把它们打进日志/接口响应，别丢。
#
# 调用方
# ------
#   · `schedule/scanScheduler.py` —— 扫描真的跑完（DONE）之后自动收尾
#   · `tools/place_cli.py`        —— `--rebuild` 全链路
#   ⚠️ `POST /api/places/rebuild`（手动刷新字典）目前**只做 ②**，不补扫 ① ——
#      需要"新目录立刻进字典"时请用 CLI `--rebuild` 或等扫描收尾。

import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))              # .../processor/place
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))             # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                              # noqa: E402
from config import basicSettings as basicSettings                  # noqa: E402

from processor.place import dirNamePlace as dirNamePlace           # noqa: E402
from processor.place import placeStore as placeStore               # noqa: E402

_VERSION = "20261008"

_LOG = misc.setLogNew("placeFinalize", "placefinalize.log")


def finalizePlaces(dbFile: str = None, refresh: bool = False,
                   withNameZh: bool = None, source: str = "") -> dict:
    """跑完地点侧的三步（**硬顺序**），返回报告。**不抛异常**。

    dbFile      : 显式切库（None = 当前库；DR-10 的纪律）
    refresh     : 是否覆盖 `placeNameDir` 的**漂移行**（默认 False = 只填空）。
                  ⚠️ 自动收尾恒为 False；只有用户显式下令才 True。
    withNameZh  : 是否跑第 ③ 步（None = 读 `basicSettings.PLACE_FINALIZE_NAME_ZH`）
    source      : 调用来源（只进日志，便于回答"这次是谁触发的"）

    返回
    ----
    dict —— {ok, source, dbFile, elapsed,
             scan, rebuild, nameZh, drift, suspects, errors}
      ok       : 三步**都**成功才算 True（有 errors 就是 False，但步骤照跑）
      scan     : `scanDirNames()` 的完整报告（含 candidates/written/drift/suspects）
      rebuild  : `rebuildPlaces()` 的统计（含 placeCount/zeroed/orphans）
      nameZh   : `rebuildNameZh()` 的统计；跳过时为 None
      drift    : 目录改名的证据（`[{photoCode, relPath, old, now}]`）——
                 **不覆盖**，只报出来（见纪律 ②）
      suspects : 疑似地点（判据没认出的新写法）—— **不采纳**，只报出来
      errors   : 每一步的失败原因（形如 `["rebuild: RuntimeError: ..."]`）

    ⚠️ 三步都写库，因此**调用方必须持有单写入者门闩**（扫描收尾时在
       `runBatch` 的锁内，CLI 时是独立进程/用户操作）。
    """
    started = time.time()
    out = {"ok": False, "source": str(source or ""), "dbFile": "",
           "scan": None, "rebuild": None, "nameZh": None,
           "drift": [], "suspects": [], "errors": []}

    # ---- ① 填 pb_photo.placeNameDir（只填空）----
    try:
        out["scan"] = dirNamePlace.scanDirNames(dryRun=False, dbFile=dbFile,
                                                refresh=bool(refresh))
        out["drift"] = list(out["scan"].get("drift") or [])
        out["suspects"] = list(out["scan"].get("suspects") or [])
        out["dbFile"] = out["scan"].get("dbFile") or ""
    except Exception as e:                                        # noqa: BLE001
        # ⚠️ ① 失败**不**跳过 ②：填列失败只意味着"这一轮没有新线索"，
        #    而已经填过的那些照样该进字典 —— 不能把一次小故障放大成"地点全没了"。
        out["errors"].append("scan: %s: %s" % (type(e).__name__, e))
        _LOG.error("finalizePlaces: 第①步 scanDirNames 失败（继续第②步）: %s: %s",
                   type(e).__name__, e)

    # ---- ② 重建地点字典（聚合键 = 目录名优先）----
    try:
        out["rebuild"] = placeStore.rebuildPlaces(dbFile=dbFile)
    except Exception as e:                                        # noqa: BLE001
        out["errors"].append("rebuild: %s: %s" % (type(e).__name__, e))
        _LOG.error("finalizePlaces: 第②步 rebuildPlaces 失败: %s: %s",
                   type(e).__name__, e)

    # ---- ③（可选）补 GPS 地点的中文名：只填 NULL，绝不覆盖 ----
    wantZh = bool(basicSettings.PLACE_FINALIZE_NAME_ZH
                  if withNameZh is None else withNameZh)
    if wantZh:
        try:
            # 懒 import：`placeNameZh` 会碰可选依赖（shapely / 离线区划数据），
            # 不跑第③步的人不该为它付 import 代价（与 meta.py 的懒加载同纪律）。
            from processor.place import placeNameZh as placeNameZh
            out["nameZh"] = placeNameZh.rebuildNameZh(dryRun=False, dbFile=dbFile)
        except Exception as e:                                    # noqa: BLE001
            out["errors"].append("nameZh: %s: %s" % (type(e).__name__, e))
            _LOG.error("finalizePlaces: 第③步 rebuildNameZh 失败: %s: %s",
                       type(e).__name__, e)

    out["elapsed"] = round(time.time() - started, 3)
    out["ok"] = not out["errors"]
    _LOG.info("finalizePlaces[%s]: ok=%s 用时 %.2fs 扫描=%s 字典=%s 中文名=%s 漂移=%d 疑似=%d",
              out["source"] or "-", out["ok"], out["elapsed"],
              (out["scan"] or {}).get("written"),
              (out["rebuild"] or {}).get("placeCount"),
              (out["nameZh"] or {}).get("filled"),
              len(out["drift"]), len(out["suspects"]))
    return out


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("placeFinalize.py _VERSION:", _VERSION)
    print("开关 PLACE_FINALIZE_AFTER_SCAN:", basicSettings.PLACE_FINALIZE_AFTER_SCAN)
    print("开关 PLACE_FINALIZE_NAME_ZH  :", basicSettings.PLACE_FINALIZE_NAME_ZH)
    print("库                          :", "（由 finalizePlaces 决定；本自检不写库）")
    print("三步（**顺序不能反**）：")
    print("  ① dirNamePlace.scanDirNames(dryRun=False)  填 pb_photo.placeNameDir")
    print("  ② placeStore.rebuildPlaces()               按聚合键重建 pb_place")
    print("  ③ placeNameZh.rebuildNameZh()              （可选）补中文名，只填 NULL")
