#! /usr/bin/env python3
#encoding: utf-8

#Filename: placeFix.py
#Description: photo-browser 单张照片地点修正（DR-52）—— pb_photo.placeNameOverride 的唯一写入口
#
# 要解决的现象：逆地理 / 目录名给的地点**不一定对**（GPS 漂了几十公里、目录名只是
#   "大概那一片"）。用户看着一张照片能一眼说出"这是外婆家"，而库里没有一处能记下它。
#
# 为什么另存一列而不是改 `placeName` / `placeNameDir`
# -------------------------------------------------
#   · `placeName` 是**扫描器写的派生值**（重扫整列覆盖）；`placeNameDir` 是目录名线索。
#     人工判断混进这两列，下一次重扫 / 目录名填空都会把它**静默冲掉**（不报错）。
#   · 人工修正的生命周期与它们都不同：由用户显式改、显式撤销，不跟随任何自动流程。
#   ⇒ 另存 `placeNameOverride`，并让它进**聚合键的最优先一段**
#     （`placeStore.PLACE_KEY_SQL`）—— 地点列表 / 筛选 / 照片摘要自动跟着变。
#
# ⚠️ 「自由输入一个新地点名」还牵出第二列：`pb_place.nameSource`（DR-53）
# ---------------------------------------------------------------------
#   用户输入一个地理数据集不认识的词（「外婆家」）时，`rebuildPlaces()` 会**新建**
#   一行地点 —— 而它的 `nameZh` 是空的，下一次 `rebuildNameZh()` 就会按坐标把它
#   填成区县名，用户输入的名字**在显示层消失**（数据层却完全正常）。
#   ⇒ `applyFix()` 在 rebuild 之后把**本次新建**的那一行标成
#     `nameSource='manual'`（人工命名）→ 人工行不进 `rebuildNameZh()` 的候选集。
#   ⚠️ **只标新建的行**：已有地点一律不碰 `nameZh`/`nameSource`
#      （它可能已经有人工名，或该由地理数据集命名）。判据 = 改之前 `exists=False`。
#
# ⚠️ 三条「不许碰它」的纪律（与 placeNameDir / rotateDeg 之所以安全同一条理由：
#   **它是非派生列，任何自动流程都只读不写**。本步 runner.py 因此**零改动**）：
#   ① **不进** `runner._META_FULL_COLUMNS`（重扫白名单）-> 否则重扫一次全部清零；
#   ② **不进** `placeStore.REBUILD_COLUMNS`（复算列）-> 否则每轮 rebuild 覆盖；
#   ③ **不被** `dirNamePlace.scanDirNames()` 的填空逻辑碰到 -> 否则扫描收尾覆盖。
#
# ⚠️ 改完地点**必须立刻** `rebuildPlaces()`（用户明确要求，本步的核心体验）：
#   `pb_place.photoCount` 是**派生缓存**，只由 `rebuildPlaces()` 复算。不 rebuild 就会
#   出现「照片详情说华盛顿、地点列表说纽约 98 张」的自相矛盾（接口 200、两处各自自洽）。
#   所以 `applyFix()` 落库后**同步**调一次全量复算，让"提交后立刻正确"可验收
#   （**刻意不做异步后台 rebuild** —— 那会让接口先返回、列表稍后才变）。
#
# ⚠️ 不做批量（用户明确）；**不落 pb_review_log**：地点修改不改变任何识别事实
#   （不动 bbox / 质心 / shotBucket / 归属 / faceCount）且完全可逆，与 DR-43 旋转同口径。

import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/place
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

from processor.place import placeNameZh as placeNameZh            # noqa: E402
from processor.place import placeStore as placeStore              # noqa: E402

_VERSION = "20261011"

_LOG = misc.setLogNew("placeFix", "placefix.log")

#: 地点名长度上限 —— 与 `pb_photo.placeNameOverride VARCHAR(128)` 逐字对齐。
#: ⚠️ 超长不截断而是 400：截断是"用户以为存了完整名字、实际被砍了"，而不报错。
MAX_NAME_LEN: int = 128

#: `current.source` 的取值（说明"现在这个地点是从哪来的"）
SOURCE_OVERRIDE: str = "override"      # 人工修正（placeNameOverride 非空）
SOURCE_DIR: str = "dir"                # 目录名（placeNameDir）
SOURCE_GPS: str = "gps"                # 逆地理（placeName）
SOURCE_NONE: str = "none"              # 三段全空（截图/人名目录类，没有地点线索）


class PlaceFixError(Exception):
    """参数非法 / 状态不允许（api 层映射成 400）。"""


# ============================================================
# 零、内部工具
# ============================================================

def _photoRow(photoCode: str) -> dict:
    """取一行 pb_photo（**含软删行** —— 修正一张已软删的照片不该 404）。查不到抛错。"""
    code = str(photoCode or "").strip()
    if not code:
        raise PlaceFixError("photoCode 不能为空")
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=code,
                                      delFlag="*", limitNum=1)
    if not rows:
        raise PlaceFixError("photoCode=%s 在 pb_photo 里不存在" % code)
    return rows[0]


def validateName(placeName):
    """外部传进来的目标地点名 -> 写入值（str 或 None）。

    None / `""`  -> None（= **恢复自动**：把 override 写回 SQL NULL）
    `"   "`（全空白）-> 抛错。⚠️ 它与 `""` 是**两件事**：前者是用户手滑，
        后者是"我就是要恢复自动"。混在一起会让"输了一串空格"变成
        "静默把用户的修正清掉"。
    超长 -> 抛错（**不截断**：截断是"用户以为存了完整名字、实际被砍了"，且不报错）。
    """
    if placeName is None:
        return None
    text = str(placeName)
    if text == "":
        return None
    stripped = text.strip()
    if not stripped:
        raise PlaceFixError("地点名不能只有空白字符（要清除修正请传空字符串或 null）")
    if len(stripped) > MAX_NAME_LEN:
        raise PlaceFixError("地点名最长 %d 个字符，收到 %d 个"
                            % (MAX_NAME_LEN, len(stripped)))
    return stripped


def _overrideOf(row: dict) -> str:
    """行里的 `placeNameOverride` -> 去空白后的 str 或 None（空串=未修正）。"""
    text = str((row or {}).get("placeNameOverride") or "").strip()
    return text or None


def _sourceOf(override: str, placeNameDir: str, placeName: str) -> str:
    """「现在这个地点是从哪来的」——三段优先，与聚合键**同一顺序**。"""
    if str(override or "").strip():
        return SOURCE_OVERRIDE
    if str(placeNameDir or "").strip():
        return SOURCE_DIR
    if str(placeName or "").strip():
        return SOURCE_GPS
    return SOURCE_NONE


def _nameZhOf(placeKey: str) -> str:
    """某个聚合键的中文显示名（没有就返回 ""）。"""
    if not placeKey:
        return ""
    return placeStore.nameZhOfPlace(str(placeKey)) or ""


def _existsOf(placeKey: str) -> dict:
    """该聚合键在 `pb_place` 里是否已有行（未软删）+ 它的中文名。

    ⚠️ 只看行是否存在，不看 `photoCount` —— 张数用 `_photoCountOf` **现算**。
    """
    if not placeKey:
        return {"exists": False, "nameZh": ""}
    if not query.tableExists("pb_place"):
        return {"exists": False, "nameZh": ""}
    rows = query.selectList(
        "SELECT g.placeName AS placeName, g.nameZh AS nameZh FROM pb_place g"
        " WHERE g.delFlag = %s AND g.placeName = %s LIMIT 1",
        (comGD.DEL_FLAG_NO, str(placeKey)))
    if not rows:
        return {"exists": False, "nameZh": ""}
    return {"exists": True, "nameZh": str(rows[0].get("nameZh") or "")}


def _photoCountOf(placeKey: str) -> int:
    """某聚合键**当前**的照片数（**实时**算，不读 `pb_place.photoCount` 缓存）。

    ⚠️ 为什么不读字典缓存：`applyFix()` 落库后会立刻 `rebuildPlaces()`，
       预览里"改后会变成 N"必须与**重建之后**的值一致。缓存可能是上一次复算的
       快照（扫描新照片后没 rebuild），拿它预测会给出提交后对不上的数字。
    """
    if not placeKey:
        return 0
    key = placeStore.placeKeySql("p")
    return int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p"
        " WHERE p.delFlag = %s AND " + key + " = %s",
        (comGD.DEL_FLAG_NO, str(placeKey)), default=0) or 0)


# ============================================================
# 一、预览（**只读，一行都不写**）
# ============================================================

def previewFix(photoCode: str, placeName=None) -> dict:
    """这张照片的地点改成 `placeName` 之后会变成什么样。**纯读**。

    参数 placeName：目标地点名；**None / "" = 恢复自动**（回到目录名/GPS 的自动值）。

    字段分工：
      current.source  : override / dir / gps / none —— 现在这个地点从哪来
      autoPlaceKey    : 清掉 override 后会回到哪里（前端显示「自动：纽约」）
      target          : 目标地点（exists=False + photoCount=0 表示这会**新建一行**）
      counts.from/to  : 两个地点的张数与"改后"张数
      fromBecomesEmpty: from 地点改完会变成 0 张（前端据此提示一句）
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    relPath = str(row.get("relPath") or "")
    placeNameNow = str(row.get("placeName") or "")
    placeDirNow = str(row.get("placeNameDir") or "")
    overrideNow = _overrideOf(row)

    # ⚠️ 两个口径都从**同一把键**算出来（placeStore.placeKeyOf），别在这里另写一遍
    #    优先级：那正是"接口返回的地点"与"聚合/筛选用的地点"分叉的起点。
    autoKey = placeStore.placeKeyOf(placeNameNow, placeDirNow)
    currentKey = placeStore.placeKeyOf(placeNameNow, placeDirNow, overrideNow)

    writeValue = validateName(placeName)                     # None = 恢复自动
    targetKey = autoKey if writeValue is None else writeValue
    changed = (writeValue != overrideNow)

    currentCount = _photoCountOf(currentKey)
    targetCount = _photoCountOf(targetKey)
    exists = _existsOf(targetKey)
    # ⚠️ from == to 时**不要**把同一张照片算两次：after 直接等于原值。
    sameKey = bool(currentKey) and currentKey == targetKey
    fromAfter = currentCount if sameKey else max(0, currentCount - 1)
    toAfter = targetCount if sameKey else targetCount + 1

    return {
        "photoCode": code,
        "relPath": relPath,
        "changed": bool(changed),
        "restoreAuto": bool(writeValue is None),
        "current": {"placeKey": currentKey or None,
                    "nameZh": _nameZhOf(currentKey) or None,
                    "source": _sourceOf(overrideNow, placeDirNow, placeNameNow)},
        "target": {"placeKey": targetKey or None,
                   "nameZh": (exists.get("nameZh") or None) or None,
                   "exists": bool(exists.get("exists")),
                   "photoCount": int(targetCount)},
        "autoPlaceKey": autoKey or None,
        "counts": {"from": {"placeKey": currentKey or None,
                            "photoCount": int(currentCount), "after": int(fromAfter)},
                   "to": {"placeKey": targetKey or None,
                          "photoCount": int(targetCount), "after": int(toAfter)}},
        # ⚠️ 只对"真的会掉到 0"报 True：from==to 时 currentCount 不动，绝不误报。
        "fromBecomesEmpty": bool(changed and currentKey and not sameKey
                                 and fromAfter <= 0),
        "note": ("修正的是**这张照片的地点**（进 `placeNameOverride`）：原值都不改写，"
                 "随时可「恢复自动」；提交后会自动重建地点字典，让张数立刻正确。"),
    }


# ============================================================
# 二、落库 + 自动重建地点字典
# ============================================================

def _writeOverride(photoCode: str, value) -> int:
    """写 `pb_photo.placeNameOverride`。

    ⚠️ 必须走 `updateTableGeneral`：值**原样**进参数元组，None 就是 SQL NULL
       ——「恢复自动」写的就是 NULL。走 upsert 的话整批为 None 的列会被
       normalizeDataSet 丢掉，「恢复自动」就变成一次**静默空操作**（同 photoTimeFix）。
    """
    rtn = sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (str(photoCode),),
        {"placeNameOverride": value, "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise PlaceFixError("pb_photo.placeNameOverride 写入失败: %s"
                            % sqliteCommon.dbHandle().lastErrMsg)
    if rtn is not None and rtn < 0:
        raise PlaceFixError("pb_photo.placeNameOverride 写入返回 %r" % (rtn,))
    return rtn


def _markManualNamed(placeKey: str) -> bool:
    """把聚合键对应的地点行标成**人工命名**（`nameSource='manual'`）。

    ⚠️ 只写 `nameSource`，**不写 `nameZh`**：此时 `nameZh` 该显示的就是
       `placeName` 本身（用户输入的那个名字已经**就是**聚合键），再往
       `nameZh` 里抄一遍是纯冗余，而且会让「中文显示名」这一列混进英文键。
       界面的回落逻辑（`placeDisplayName`：`nameZh || placeName`）本来就给出同一个字。

    为什么必须有人做这件事（DR-53）：新建的地点行只可能来自这次的自由输入，
      它的 `nameZh`/`nameSource` 都是空 —— 于是下一次 `rebuildNameZh()`
      （`POST /api/places/rebuild` 或一次扫描收尾）会按坐标把它填成区县名，
      用户输入的「外婆家」**在显示层消失**，而接口一路 200。
    """
    code = placeStore.makePlaceCode(str(placeKey or ""))
    if not code:
        return False
    rtn = sqliteCommon.updateTableGeneral(
        "pb_place", "placeCode = %s", (code,),
        {"nameSource": placeNameZh.NAME_SOURCE_MANUAL,
         "modifyYMDHMS": misc.getTime()})
    if rtn == -2 or (rtn is not None and rtn < 0):
        _LOG.warning("placeFix: 标记人工命名失败 placeCode=%s: %s",
                     code, sqliteCommon.dbHandle().lastErrMsg)
        return False
    return bool(rtn and rtn > 0)


def applyFix(photoCode: str, placeName=None) -> dict:
    """改成 `placeName`（None / "" = 恢复自动）并**自动重建地点字典**。

    返回 = `previewFix` 的全部字段 + `rebuildSummary`（复算统计）
         + `rebuildElapsedMs`（全量复算耗时，毫秒）。
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    overrideNow = _overrideOf(row)
    writeValue = validateName(placeName)
    #: 目标聚合键 + 「改之前这个地点在库里有没有行」—— 决定③要不要标人工命名
    targetKey = writeValue if writeValue is not None else placeStore.placeKeyOf(
        row.get("placeName"), row.get("placeNameDir"), None)
    targetExisted = bool(_existsOf(targetKey).get("exists")) if targetKey else False

    # 幂等：值没变就什么都不做（**不写库、不 rebuild**）——
    # 重复点一次「修正」不该多跑一遍全量复算（那是几万行的一次 GROUP BY）。
    if writeValue == overrideNow:
        preview = previewFix(code, placeName)
        preview["changed"] = False
        preview["rebuildSummary"] = None
        preview["rebuildElapsedMs"] = 0
        preview["note"] = "目标地点与当前值相同，没有改动（幂等）"
        return preview

    # ---- ① 写 override（唯一那个写点）----
    _writeOverride(code, writeValue)

    # ---- ② 立刻全量复算地点字典（用户明确要求：提交后计数立刻正确）----
    # ⚠️ 同步、不做"异步后台 rebuild"：见文件头。幂等（不清表、按 placeCode upsert）。
    started = time.time()
    summary = placeStore.rebuildPlaces()
    elapsedMs = int(round((time.time() - started) * 1000))

    # ---- ③ 新建的地点 = 人工命名的地点（DR-53，见 _markManualNamed）----
    # ⚠️ 必须在 rebuild **之后**：那一行是 rebuild 刚创建出来的。
    namedManual = False
    if writeValue is not None and targetKey and not targetExisted:
        namedManual = _markManualNamed(targetKey)

    out = previewFix(code, placeName)
    out["changed"] = True
    out["rebuildSummary"] = {"created": int(summary.get("created") or 0),
                             "updated": int(summary.get("updated") or 0),
                             "zeroed": int(summary.get("zeroed") or 0),
                             "placeCount": int(summary.get("placeCount") or 0),
                             "newPlaceNamedManual": bool(namedManual)}
    out["rebuildElapsedMs"] = elapsedMs
    _LOG.info("地点修正 %s：override %r -> %r，重建字典 created=%d updated=%d "
              "zeroed=%d，新地点人工命名=%s，耗时 %d ms",
              code, overrideNow, writeValue,
              out["rebuildSummary"]["created"], out["rebuildSummary"]["updated"],
              out["rebuildSummary"]["zeroed"], namedManual, elapsedMs)
    return out


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("placeFix.py _VERSION:", _VERSION)
    print("改的列      : pb_photo.placeNameOverride（None=NULL，最长 %d 字符）"
          % MAX_NAME_LEN)
    print("聚合键      : 人工修正 > 目录名 > GPS（placeStore.placeKeySql）")
    print("联动        : ①写 override -> ②rebuildPlaces()（同步全量复算）")
    print("日志        : 不落 pb_review_log（不改变任何识别事实，见文件头）")
    print("库          :", sqliteCommon.dbFilePath())
