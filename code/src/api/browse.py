#! /usr/bin/env python3
#encoding: utf-8

#Filename: browse.py
#Description: photo-browser 浏览类接口（步骤 9·P0）—— 时间线 / 照片列表 / 人物 / 详情
#
# 五个端点
# ----------
#   GET /api/timeline按年月分组（三段式：年表 / 某年月份 / 某年某月照片）
#   GET /api/photos               分页 + 筛选 + 排序
#   GET /api/persons              人物网格（含 photoCount / 年代跨度）
#   GET /api/persons/{personCode} 人物详情（含各年代桶分组统计）
#   GET /api/photos/{photoCode}   照片详情（含 faceCount / 出现的人 / EXIF）
#
# 三条纪律
# --------
#   ① **不裸 SQL**：区间比较 / 分组聚合走 database.queryCommon（只读出口），
#      等值查询走生成层 sqliteCommon.query_pb_*。本模块不 import sqlite3。
#   ② **不写库**：整个 browse 模块**一个字都不写**（含不发 review 日志）。
#      任何要改归属的动作都在 api/review.py，且必须经 assigner/merger。
#   ③ **不碰 photoDir**：只把 fileHash / relPath 带出去，取图由 api/static.py
#      完成（那里有 Range / ETag / 按需生成）。缩略图路径由 fileHash 推导，
#      **库里没有 thumbPath 字段**，本模块也不新增。
#
# 关于 /api/timeline 为什么不做成「一条SQL 全部分页」
# --------------------------------------------------
#   10 万张的时间线是一条**按年月降序的有序流**。用 OFFSET 分页它，
#   意味着第 500 页要 OFFSET 30000 —— 深分页问题最严重的形态。
#   所以这里按**年月分段**：
#     · 不传 year/month -> 一次 GROUP BY 拿到年月索引（首屏只要这个）
#     · 传 year        -> 只返回该年的月份分组（走 idx_pb_photo_shotYear）
#     · 传 year+month  -> 返回该月的照片（OFFSET 被**天然限制在一个月内**）
#   这样任何时刻的 offset 都被「一个月」这个物理量级兜住了。
#
#   ⚠️ 月份分组为什么用 substr(takenAt,1,7) 而不是 strftime
#      takenAt 是 **UTC ISO8601**（"2023-05-01T04:00:00Z"，见 scanner/meta.py），
#      SQLite 的 strftime 认得它但会**按本地时区**再偏移一次 —— 在东八区
#      "2013-01-01T00:30:00Z" 会被算成 2012-12。substr 是纯字符串切，
#      不做任何时区运算，这才是我们要的口径。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import dto                                               # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402
from processor.place import placeStore as placeStore              # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("apiBrowse", "apibrowse.log")

router = APIRouter(tags=["browse"])

#: 时间线上「日期读不出来」的分组键（shotYear 为空、takenAt 为空/畸形）。
#: 刻意**保留**这一组而不是过滤掉：扫描器对截图类会写 shotYear=NULL
#: （见 meta.resolveShotYear 的 SCREENSHOT 分支），过滤掉等于让这批照片
#: 在时间线上凭空消失，而用户「明明存过这张图」。
UNKNOWN_PERIOD: str = "unknown"

#: /api/photos 允许的排序字段 -> pb_photo 列。白名单制：排序字段直接进
#: ORDER BY，不是值可以不校验；但它会**改变结果顺序**，写错一个名字用户
#: 只会觉得「排序不对」，所以宁可少给几个也不给错。
PHOTO_SORT_COLUMNS: dict = {
    "takenAt": "takenAt",
    "shotYear": "shotYear",
    "fileSize": "fileSize",
    "recID": "recID",
    "width": "width",
}

#: /api/persons 允许的排序字段 -> (升序 SQL, 降序 SQL)。
#: ⚠️ 值里只有**白名单常量与列名**，没有用户输入 —— 它们直接进 ORDER BY。
#: ⚠️ `photoCount` / `faceCount` 是**聚合计数，不在 pb_person 上**，
#:   所以要用标量子查询按人算。把它们写成别名再 ORDER BY 是**无效 SQL**
#:   （同层ORDER BY 里看不到本层的别名，而且它也不是 schema 列 ——
#:    queryCommon.checkColumns 会当场拒掉）。
PERSON_SORT_SQL: dict = {
    # ⚠️ photoCount 的子查询口径**必须与 personStatsOf() 的 ① 逐字一致**
    #    （含 JOIN pb_photo AND delFlag='0'）：两处不一致时，用户会看到
    #    「按照片数排序，但列表里显示的照片数不是这个数」。
    "photoCount": ("(SELECT COUNT(*) FROM pb_photo_person pp"
                   " JOIN pb_photo ph ON ph.photoCode = pp.photoCode"
                   " WHERE pp.personCode = p.personCode"
                   " AND ph.delFlag = '%s') ASC, p.displayName ASC" % comGD.DEL_FLAG_NO,
                   "(SELECT COUNT(*) FROM pb_photo_person pp"
                   " JOIN pb_photo ph ON ph.photoCode = pp.photoCode"
                   " WHERE pp.personCode = p.personCode"
                   " AND ph.delFlag = '%s') DESC, p.displayName ASC" % comGD.DEL_FLAG_NO),
    "faceCount": ("(SELECT COUNT(*) FROM pb_face pf"
                  " WHERE pf.personCode = p.personCode) ASC, p.displayName ASC",
                  "(SELECT COUNT(*) FROM pb_face pf"
                  " WHERE pf.personCode = p.personCode) DESC, p.displayName ASC"),
    "displayName": ("p.displayName ASC", "p.displayName DESC"),
    "personCode": ("p.personCode ASC", "p.personCode DESC"),
    "birthday": ("p.birthday ASC, p.displayName ASC",
                 "p.birthday DESC, p.displayName ASC"),
}


# ============================================================
# 一、行读取与结构化（**本模块是全 api 层的只读模型出口**）
#    review.py / contacts.py 复用 photoRow / faceRow / personRow /
#    photoSummary / personSummary —— 一处定义，各处口径一致。
# ============================================================

def photoRow(photoCode: str, dbFile: str = None) -> dict:
    """按 photoCode 取一行 pb_photo（未软删）。查不到返回 {}。"""
    if not photoCode:
        return {}
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=str(photoCode),
                                      delFlag=comGD.DEL_FLAG_NO, limitNum=1)
    return rows[0] if rows else {}


def faceRow(faceCode: str, dbFile: str = None) -> dict:
    """按 faceCode 取一行 pb_face（未软删）。查不到返回 {}。"""
    if not faceCode:
        return {}
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    rows = sqliteCommon.query_pb_face("pb_face", faceCode=str(faceCode),
                                     delFlag=comGD.DEL_FLAG_NO, limitNum=1)
    return rows[0] if rows else {}


def personRow(personCode: str, withDeleted: bool = False) -> dict:
    """按 personCode 取一行 pb_person。查不到返回 {}。

    withDeleted=True 时连软删（delFlag='1'）一起看 —— 撤销合并 / 停用后
    排查要用到，否则「这个人怎么查不到了」会变成一个无解的问题。
    """
    code = str(personCode or "")
    if not code:
        return {}
    rows = sqliteCommon.query_pb_person(
        "pb_person", personCode=code,
        delFlag="*" if withDeleted else comGD.DEL_FLAG_NO, limitNum=1)
    return rows[0] if rows else {}


def categoriesOf(personCode: str) -> list:
    """一个人的分类标签（升序）。"""
    out = []
    for row in sqliteCommon.query_pb_person_category(
            "pb_person_category", personCode=str(personCode or ""),
            orderBy="category"):
        name = str(row.get("category") or "")
        if name:
            out.append(name)
    return sorted(set(out))


def photoSummary(row: dict) -> dict:
    """pb_photo 行 -> 照片列表项。

    刻意**不带** fileHash / relPathHash / dupOfPhotoCode：
    它们是内部指纹，出现在列表响应里只会让人误以为前端该拿它去拼路径
    （缩略图路径由 fileHash 推导，但**由服务端 api/static.py 负责**，
    前端拿 photoCode 就够了）。fileSize / 尺寸 / 时间这些是 UI 要显示的。
    """
    if not row:
        return {}
    photoCode = str(row.get("photoCode") or "")
    width = row.get("width")
    height = row.get("height")
    # ⚠️ placeName **不能**当「有 GPS」的判据：它只在 lat/lon 都存在时才填，
    #    但坐标查不到地名（海里 / 落在离线数据集外）时仍然是空 —— 用它做角标会漏标。
    hasGps = row.get("lat") is not None and row.get("lon") is not None
    return {
        "photoCode": photoCode,
        "thumbUrl": "/api/thumb/%s?size=400" % photoCode,
        "originalUrl": "/api/original/%s" % photoCode,
        "relPath": str(row.get("relPath") or ""),
        "hasGps": bool(hasGps),
        "takenAt": row.get("takenAt") or None,
        "shotYear": row.get("shotYear") if row.get("shotYear") is not None else None,
        "placeName": row.get("placeName") or None,
        "cameraModel": row.get("cameraModel") or None,
        "width": int(width) if width is not None else None,
        "height": int(height) if height is not None else None,
        "fileSize": int(row.get("fileSize") or 0),
        "mimeType": row.get("mimeType") or None,
        "faceCount": int(row.get("faceCount") or 0),
        "isDuplicate": int(row.get("isDuplicate") or 0),
        "isMissing": int(row.get("isMissing") or 0),
        "scanState": int(row.get("scanState") or 0),
    }


def personSummary(row: dict, photoCount: int = 0, faceCount: int = 0,
                  confirmedFaceCount: int = 0, yearLow=None, yearHigh=None,
                  withCategories: bool = True) -> dict:
    """pb_person 行 -> 人物卡片 / 详情头。

    四个计数字段的口径（**验收第 16 条要逐个核对**）
    ---------------------------------------------
      photoCount        = pb_photo_person 里该人的关联行数
        ⚠️ **不是** SUM(pb_photo.faceCount)—— 那是「这个库一共有多少张脸」，
        不是「他出现在多少张照片里」。一张 3 人合影里他只算 1 张。
      faceCount         = pb_face 里 personCode = 他 的行数（**含自动归属**）
      confirmedFaceCount= 同上但 isConfirmed = 1（**只有这个进质心**，DR-16③）
      yearLow/yearHigh  = 他出现过的照片的 shotYear 最小/最大值（年代跨度）
    """
    if not row:
        return {}
    code = str(row.get("personCode") or "")
    out = {
        "personCode": code,
        "displayName": str(row.get("displayName") or ""),
        "familyName": row.get("familyName") or None,
        "familyGroupCode": row.get("familyGroupCode") or None,
        "relation": row.get("relation") or None,
        "email": row.get("email") or None,
        "phone": row.get("phone") or None,
        "birthday": row.get("birthday") or None,
        "avatarFaceCode": row.get("avatarFaceCode") or None,
        "source": int(row.get("source") or 0),
        "isConfirmed": int(row.get("isConfirmed") or 0),
        "delFlag": str(row.get("delFlag") or comGD.DEL_FLAG_NO),
        "memo": row.get("memo") or None,
        "photoCount": int(photoCount or 0),
        "faceCount": int(faceCount or 0),
        "confirmedFaceCount": int(confirmedFaceCount or 0),
        "autoFaceCount": max(0, int(faceCount or 0) - int(confirmedFaceCount or 0)),
        "yearLow": int(yearLow) if yearLow else None,
        "yearHigh": int(yearHigh) if yearHigh else None,
        "thumbUrl": ("/api/face/%s" % row.get("avatarFaceCode")
                     if row.get("avatarFaceCode") else None),
        "detailUrl": "/api/persons/%s" % code,
    }
    out["categories"] = categoriesOf(code) if withCategories else []
    return out


def _intOrNone(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _floatOrNone(value, digits: int = 4):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _inClause(values, column: str = "personCode") -> tuple:
    """把一串编码变成 `col IN (%s,%s,...)` + 绑定值。

    ⚠️ **列名由调用方给的是白名单常量**，值一律进参数元组。
       这是「业务层禁止裸 SQL」与「筛选必须支持 IN」两条硬约束的交点。
    """
    codes = [str(v) for v in (values or ()) if str(v or "")]
    if not codes:
        return "", []
    return " AND %s IN (%s)" % (column, ", ".join(["%s"] * len(codes))), codes


# ============================================================
# 二、GET /api/timeline
# ============================================================

def _yearMonthIndex() -> tuple:
    """全库「年月 -> 张数」索引（一次 GROUP BY）。

    返回 (groups, unknownCount)
      groups      : [{ym:"2013-07", year:2013, month:7, count:120}, ...] 按 ym 降序
      unknownCount: takenAt 为空/畸形、读不出年月的张数（**单列一组，不丢**）

    ⚠️ 一次全扫+ 一次临时 B 树。10 万张实测在 100~200ms 量级（见验收第 3 条），
       这是「首屏 <500ms」的主要成本，也是**唯一**的成本 —— 所以这一段
       不做任何按需生成缩略图之类的事。
    """
    rows = query.selectList(
        "SELECT IFNULL(substr(p.takenAt, 1, 7), %s) AS ym,"
        " COUNT(*) AS cnt FROM pb_photo p"
        " WHERE p.delFlag = %s GROUP BY ym ORDER BY ym DESC",
        (UNKNOWN_PERIOD, comGD.DEL_FLAG_NO))
    groups, unknown = [], 0
    for row in rows:
        key = str(row.get("ym") or "")
        cnt = int(row.get("cnt") or 0)
        if key == UNKNOWN_PERIOD:
            unknown = cnt
            continue
        if len(key) != 7 or not key[:4].isdigit() or not key[5:].isdigit():
            unknown += cnt
            continue
        groups.append({"ym": key, "year": int(key[:4]), "month": int(key[5:]),
                       "count": cnt})
    return groups, unknown


def _monthGroupsOf(groups: list, year: int) -> list:
    return [dict(g) for g in groups if int(g["year"]) == int(year)]


def _yearIndex(groups: list) -> list:
    """年月分组 -> 年份索引 [{year, count, months}]（count 是**该年总数**）。

    ⚠️ `count` 必须由 months **求和**得到，不能取「第一项的 count」——
      那个第一项是某个月的计数（2013 年的第一项是 2013-07，count=1），
      于是「2013 年有 3 张」会被报成 1。这类错不报错，
      只是时间线上的年份标题后面跟了个错的数字。
    """
    order = []
    buckets = {}
    for one in groups:
        year = int(one["year"])
        if year not in buckets:
            buckets[year] = []
            order.append(year)
        buckets[year].append(dict(one))
    return [{"year": year, "count": sum(m["count"] for m in buckets[year]),
             "months": buckets[year]} for year in order]


@router.get("/timeline", summary="时间线（按年月分组；分段拉取，不用深 OFFSET）")
def getTimeline(year: int = Query(default=None, ge=1, le=9999,
                                 description="给则只返回该年的月份分组"),
                month: int = Query(default=None, ge=1, le=12,
                                   description="与 year 同时给 -> 返回该月的照片"),
                unknown: int = Query(default=0,
                                     description="**1 = 取「日期未知」那批照片**"
                                                 "（截图类，takenAt 为空读不出年月）。"
                                                 "与 year/month 互斥"),
                page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
                size: int = Query(default=dto.DEFAULT_PAGE_SIZE,
                                  description="单页条数，上限 %d" % dto.MAX_PAGE_SIZE),
                hasFace: int = Query(default=None, description="只取含人脸的照片（1/0）"),
                personCode: str = Query(default=None, description="只看某人出现的月份/照片")):
    """四种模式，一个端点（前端不需要记住四套路径）：

    1. **不带 year / unknown** -> `{ scope:"years", years:[{year, count, months:[...]}] }`
       首屏模式：一次 GROUP BY 拿到全部年月索引，**没有深分页**。
    2. **带 year 不带 month** -> `{ scope:"months", year, months:[...], count }`
       走 `idx_pb_photo_shotYear`，只扫该年的行。
    3. **带 year + month** -> `{ scope:"photos", year, month, page:{page,size,total,items} }`
       OFFSET 被「一个月」兜住 —— 这就是「时间线用分段替代深分页」的落点。
    4. **`unknown=1`** -> `{ scope:"unknown", page:{...} }`
       取那批 `takenAt` 为空的照片（截图类，`meta.resolveShotYear` 写 NULL）。

    ⚠️ 为什么必须有第 4 种模式（而不是只给一个 `unknownCount` 数字）
       `shotYear` / `takenAt` 为空的照片**不在任何年月分组里** ——
       既不进年表，也不会出现在按年翻页的结果里。只报一个数字等于让这批
       照片在 UI 上**消失**：用户看到「另有 711 张日期未知」却没有任何
       入口能翻到它们。
       ⚠️ 别以为这是边角数据：本项目正式库实测 **2137 张里有 711 张**
          （**33%**：截图、微信图片、无 EXIF 的扫描件）。只给一个数字
          等于让三分之一的内容在时间线上不可达 —— 这也是加这个模式的原因。
       ⚠️ 它们的排序键只能用 `recID`（`takenAt` 是 NULL，排不了时间），
          所以按入库顺序倒序 —— 这是这批数据唯一稳定的序。
    """
    if int(unknown) and (year is not None or month is not None):
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "unknown=1 与 year/month 互斥："
                           "「日期未知」那批照片本来就不属于任何年月（请二选一）")

    groups, unknownCount = _yearMonthIndex()
    total = sum(g["count"] for g in groups) + unknownCount

    if int(unknown):
        p, s = dto.clampPage(page, size)
        at = dto.offsetOf(p, s)
        where = ["p.delFlag = %s", "(p.takenAt IS NULL OR p.takenAt = %s)"]
        values = [comGD.DEL_FLAG_NO, ""]
        if hasFace is not None:
            where.append("p.faceCount %s 0" % (">" if int(hasFace) else "="))
        if personCode:
            where.append("EXISTS (SELECT 1 FROM pb_photo_person pp"
                         " WHERE pp.photoCode = p.photoCode"
                         " AND pp.personCode = %s)")
            values.append(str(personCode))
        cond = " AND ".join(where)
        totalIn = int(query.selectValue(
            "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE " + cond,
            tuple(values)) or 0)
        rows = query.selectList(
            "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.placeName,"
            " p.cameraModel, p.width, p.height, p.fileSize, p.mimeType,"
            " p.faceCount, p.isDuplicate, p.isMissing, p.scanState"
            " FROM pb_photo p WHERE " + cond +
            " ORDER BY p.recID DESC LIMIT %s OFFSET %s", tuple(values) + (s, at))
        return {"ok": True, "scope": "unknown", "total": total,
                "unknownCount": unknownCount,
                "note": "这批照片的 takenAt 为空（截图类），读不出年月，"
                        "时间线上没有它们的位置；排序只能按入库顺序（recID）倒序",
                "page": dto.pageBody([photoSummary(r) for r in rows], p, s, totalIn)}

    if year is None:
        years = _yearIndex(groups)
        return {"ok": True, "scope": "years", "total": total,
                "unknownCount": unknownCount, "count": len(years), "items": years}

    months = _monthGroupsOf(groups, year)
    yearCount = sum(m["count"] for m in months)
    if month is None:
        return {"ok": True, "scope": "months", "total": total, "year": int(year),
                "count": yearCount, "unknownCount": unknownCount, "items": months}

    p, s = dto.clampPage(page, size)
    at = dto.offsetOf(p, s)
    ym = "%04d-%02d" % (int(year), int(month))

    where = ["p.delFlag = %s", "substr(p.takenAt, 1, 7) = %s"]
    values = [comGD.DEL_FLAG_NO, ym]
    if hasFace is not None:
        where.append("p.faceCount %s 0" % (">" if int(hasFace) else "="))
    if personCode:
        # ⚠️ 右括号必须成对 —— 少一个整条 SQL 直接语法错误。
        #    这个分支曾经漏过 `)`（且当时没有用例覆盖 personCode+timeline 这条路），
        #    现在由 test_timelineMonthWithPersonFilter 盯着。
        where.append("EXISTS (SELECT 1 FROM pb_photo_person pp"
                     " WHERE pp.photoCode = p.photoCode"
                     " AND pp.personCode = %s)")
        values.append(str(personCode))

    cond = " AND ".join(where)
    countRow = query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE " + cond, tuple(values))
    totalIn = int(countRow or 0)
    rows = query.selectList(
        "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.placeName,"
        " p.cameraModel, p.width, p.height, p.fileSize, p.mimeType,"
        " p.faceCount, p.isDuplicate, p.isMissing, p.scanState"
        " FROM pb_photo p WHERE " + cond +
        " ORDER BY p.takenAt DESC, p.recID DESC LIMIT %s OFFSET %s",
        tuple(values) + (s, at))
    return {"ok": True, "scope": "photos", "total": total, "year": int(year),
            "month": int(month), "ym": ym, "unknownCount": unknownCount,
            "page": dto.pageBody([photoSummary(r) for r in rows], p, s, totalIn)}


# ============================================================
# 三、GET /api/photos
# ============================================================

@router.get("/photos", summary="照片列表（分页 + 筛选 + 排序）")
def listPhotos(page: int = Query(default=1, ge=1),
               size: int = Query(default=dto.DEFAULT_PAGE_SIZE),
               personCode: str = Query(default=None, description="只看某人出现的照片"),
               personCodes: str = Query(default=None,
                                        description="多人，逗号分隔；配合 mode=and|or"),
               mode: str = Query(default="or", description="多人语义：or 任一 / and 合影"),
               shotYearFrom: int = Query(default=None, description="拍摄年下界（含）"),
               shotYearTo: int = Query(default=None, description="拍摄年上界（含）"),
               placeName: str = Query(default=None, description="地点精确匹配（非模糊）"),
               hasFace: int = Query(default=None, description="1 只看有人脸 / 0 只看无人脸"),
               isDuplicate: int = Query(default=None, description="1 只看重复照片"),
               isMissing: int = Query(default=None, description="1 只看库里有磁盘上没有的"),
               keyword: str = Query(default=None, description="路径 / 地点 / 机型 模糊匹配"),
               orderBy: str = Query(default="takenAt", description="排序字段（白名单）"),
               desc: int = Query(default=1, description="1 倒序（时间线默认）/ 0 正序")):
    """照片网格 / 列表。分页统一 `{page,size,total,items}`。

    多人筛选（AND / OR）
    ------------------
      `or`（默认）：照片里出现这些人中**任意一个**即可
        → `photoCode IN (SELECT photoCode FROM pb_photo_person WHERE personCode IN ...)`
      `and`：必须是**合影**（这些人同框）
        → 同一子查询加 `GROUP BY photoCode HAVING COUNT(DISTINCT personCode) = n`
      ⚠️ 关联走 `pb_photo_person`（照片 × 人），**不是** `pb_face` ——
         关联表才是「这张照片里有没有这个人」的权威，且有
         `idx_pb_photo_person_personCode` 索引可用。

    ⚠️ `placeName` 是**精确匹配**而不是 LIKE：地点来自逆地理编码，
       「北京」和「北京市」在库里就是两个不同的字符串，
       模糊匹配会让用户以为自己筛错了。UI 的地点下拉给的是库里的原值。
    """
    p, s = dto.clampPage(page, size)
    at = dto.offsetOf(p, s)
    if orderBy not in PHOTO_SORT_COLUMNS:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "orderBy 只支持 %s，收到 %r"
                           % (sorted(PHOTO_SORT_COLUMNS.keys()), orderBy))
    if str(mode).lower() not in ("or", "and"):
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "mode 只支持 or / and，收到 %r" % mode)

    where = ["p.delFlag = %s"]
    values = [comGD.DEL_FLAG_NO]

    codes = [c.strip() for c in str(personCodes or "").split(",") if c.strip()]
    if personCode:
        codes = [str(personCode)] + codes
    codes = list(dict.fromkeys(codes))
    if codes:
        marks = ", ".join(["%s"] * len(codes))
        if str(mode).lower() == "and" and len(codes) > 1:
            where.append("p.photoCode IN (SELECT pp.photoCode FROM pb_photo_person pp"
                         " WHERE pp.personCode IN (%s)"
                         " GROUP BY pp.photoCode"
                         " HAVING COUNT(DISTINCT pp.personCode) = %s)" % (marks, "%s"))
            values.extend(codes)
            values.append(len(codes))
        else:
            where.append("p.photoCode IN (SELECT pp.photoCode FROM pb_photo_person pp"
                         " WHERE pp.personCode IN (%s))" % marks)
            values.extend(codes)

    if shotYearFrom is not None:
        where.append("p.shotYear >= %s")
        values.append(int(shotYearFrom))
    if shotYearTo is not None:
        where.append("p.shotYear <= %s")
        values.append(int(shotYearTo))
    if placeName:
        where.append("p.placeName = %s")
        values.append(str(placeName))
    if hasFace is not None:
        where.append("p.faceCount %s 0" % (">" if int(hasFace) else "="))
    if isDuplicate is not None:
        where.append("p.isDuplicate = %s")
        values.append(int(bool(isDuplicate)))
    if isMissing is not None:
        where.append("p.isMissing = %s")
        values.append(int(bool(isMissing)))
    if keyword:
        # ⚠️ LIKE 的值必须走参数；但通配符本身是**用户输入的一部分**，
        #    所以这里用「包一层通配」而不是把 % 拼进 sqlstr。
        where.append("(p.relPath LIKE %s OR p.placeName LIKE %s"
                     " OR p.cameraModel LIKE %s)")
        like = "%%%s%%" % str(keyword)
        values.extend([like, like, like])

    cond = " AND ".join(where)
    total = int(query.selectValue("SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE " + cond,
                                 tuple(values)) or 0)
    sortColumn = PHOTO_SORT_COLUMNS[orderBy]
    rows = query.selectList(
        "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.placeName,"
        " p.cameraModel, p.width, p.height, p.fileSize, p.mimeType,"
        " p.faceCount, p.isDuplicate, p.isMissing, p.scanState"
        " FROM pb_photo p WHERE " + cond +
        " ORDER BY p.%s %s, p.recID %s LIMIT %%s OFFSET %%s"
        % (sortColumn, "DESC" if int(desc) else "ASC",
           "DESC" if int(desc) else "ASC"),
        tuple(values) + (s, at))
    return dto.pageBody([photoSummary(r) for r in rows], p, s, total)


# ============================================================
# 四、人物统计（**一次 IN 查询算一批人的四个数**，不逐人查库）
# ============================================================

def personStatsOf(personCodes: list) -> dict:
    """一批人的 {photoCount, faceCount, confirmedFaceCount, yearLow, yearHigh}。

    为什么必须「批量 IN」而不是逐人循环
    ------------------------------
      `/api/persons` 一页 60 个人，逐人 5 个查询 = 300 次往返；
      而人名册在真实库里只有几十~几百人，**一次全查比一页一查还便宜**。
      这里按传入的 personCodes 批量算，调用方传一页的编码即可。
      （若传空列表 = 全库所有未删人员，一次算完，contacts 列表直接复用。）
    """
    out = {code: {"photoCount": 0, "faceCount": 0, "confirmedFaceCount": 0,
                  "yearLow": None, "yearHigh": None}
           for code in personCodes}
    if not personCodes:
        return out
    # ⚠️ 这里的 IN 列表用**拼接**（拼的是「%s, %s, ...」的**个数**），
    #   不是用 `% marks` 格式化 —— 因为 SQL 串里还有真正的 `%s` 占位符
    #   （delFlag 等），`%` 运算符会把它们一起吃掉，报「not enough
    #   arguments for format string」，而报错位置离病根十万八千里。
    marks = ", ".join(["%s"] * len(personCodes))

    # ① 关联行数 = 照片数
    #    ⚠️ 必须 JOIN pb_photo 过滤 delFlag='0'：**软删照片里出现的人不该被算进
    #    「他出现在多少张照片里」**。关联行本身保留（不删行），否则恢复时
    #    重建不出原样；只在计数时排除。这也是 personStatsOf 与
    #    PERSON_SORT_SQL["photoCount"] 必须逐字一致的原因 —— 两处口径不同
    #    会出现「按照片数排序的结果与列表里显示的照片数对不上」。
    for row in query.selectList(
            "SELECT pp.personCode AS personCode, COUNT(*) AS cnt"
            " FROM pb_photo_person pp"
            " JOIN pb_photo ph ON ph.photoCode = pp.photoCode"
            " WHERE pp.personCode IN (" + marks + ")"
            " AND ph.delFlag = %s GROUP BY pp.personCode",
            tuple(personCodes) + (comGD.DEL_FLAG_NO,)):
        code = str(row.get("personCode") or "")
        if code in out:
            out[code]["photoCount"] = int(row.get("cnt") or 0)

    # ② 脸数 / 确认脸数（一次 SUM(CASE) 拿两个数）
    for row in query.selectList(
            "SELECT f.personCode AS personCode, COUNT(*) AS faceCount,"
            " SUM(CASE WHEN f.isConfirmed = 1 THEN 1 ELSE 0 END) AS confirmedCount"
            " FROM pb_face f WHERE f.personCode IN (" + marks + ")"
            " AND f.delFlag = %s GROUP BY f.personCode",
            tuple(personCodes) + (comGD.DEL_FLAG_NO,)):
        code = str(row.get("personCode") or "")
        if code in out:
            out[code]["faceCount"] = int(row.get("faceCount") or 0)
            out[code]["confirmedFaceCount"] = int(row.get("confirmedCount") or 0)

    # ③ 年代跨度：关联行 JOIN pb_photo 取 MIN/MAX(shotYear)
    for row in query.selectList(
            "SELECT pp.personCode AS personCode, MIN(p.shotYear) AS yearLow,"
            " MAX(p.shotYear) AS yearHigh"
            " FROM pb_photo_person pp JOIN pb_photo p ON p.photoCode = pp.photoCode"
            " WHERE pp.personCode IN (" + marks + ")"
            " AND p.delFlag = %s GROUP BY pp.personCode",
            tuple(personCodes) + (comGD.DEL_FLAG_NO,)):
        code = str(row.get("personCode") or "")
        if code in out:
            out[code]["yearLow"] = _intOrNone(row.get("yearLow"))
            out[code]["yearHigh"] = _intOrNone(row.get("yearHigh"))
    return out


@router.get("/persons", summary="人物网格（photoCount / 代��跨度）")
def listPersons(page: int = Query(default=1, ge=1),
                size: int = Query(default=dto.DEFAULT_PAGE_SIZE),
                keyword: str = Query(default=None, description="姓名/ 邮箱 / 电话 模糊匹配"),
                category: str = Query(default=None,
                                      description="分类筛选（family/friend/colleague...）"),
                familyGroupCode: str = Query(default=None, description="家庭组筛选"),
                delFlag: str = Query(default=None, description="0 未停用 / 1 已停用"),
                hasFace: int = Query(default=None, description="1 只看有人脸的"),
                orderBy: str = Query(default="photoCount",
                                     description="排序字段（白名单）"),
                desc: int = Query(default=1)):
    """人物网格。

    `orderBy=photoCount/faceCount` 是**排序**不是**筛选**：
      这两个值来自 pb_photo_person / pb_face 的聚合，不在 pb_person 上。
      做法是**在 ORDER BY 里放标量子查询**（见 PERSON_SORT_SQL），
      所以**分页是全局有序的**，页边界不会与全局顺序不一致。
      ⚠️ 代价：每个人都要跑一次相关子查询算 `COUNT(*)`。
         人名册是**小表**（真实库几十~几百条，本项目正式库 2027 条），
         这一层代价远小于「先全取再在 Python 里排」——
         后者在库长大之后会一次性把整张人名册读进内存。
      取回一页之后还会用批量聚合的结果**再排一次**，两处同键同向，
      目的是让响应里的 items 顺序与 SQL 分页顺序严格一致（不依赖数据库
      对相关子查询的求值顺序）。
    """
    p, s = dto.clampPage(page, size)
    if orderBy not in PERSON_SORT_SQL:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "orderBy 只支持 %s，收到 %r"
                           % (sorted(PERSON_SORT_SQL.keys()), orderBy))
    at = dto.offsetOf(p, s)

    where = []
    values = []
    if delFlag in comGD.DEL_FLAG_ALL:
        where.append("p.delFlag = %s")
        values.append(delFlag)
    else:
        where.append("p.delFlag = %s")
        values.append(comGD.DEL_FLAG_NO)
    if familyGroupCode:
        where.append("p.familyGroupCode = %s")
        values.append(str(familyGroupCode))
    if keyword:
        where.append("(p.displayName LIKE %s OR p.familyName LIKE %s"
                     " OR p.email LIKE %s OR p.phone LIKE %s)")
        like = "%%%s%%" % str(keyword)
        values.extend([like, like, like, like])
    if category:
        where.append("EXISTS (SELECT 1 FROM pb_person_category c"
                     " WHERE c.personCode = p.personCode AND c.category = %s"
                     " AND c.delFlag = %s)")
        values.extend([str(category), comGD.DEL_FLAG_NO])
    if hasFace is not None:
        where.append(("EXISTS (SELECT 1 FROM pb_face f WHERE f.personCode = p.personCode"
                      " AND f.delFlag = %s)" if int(hasFace) else
                      "NOT EXISTS (SELECT 1 FROM pb_face f"
                      " WHERE f.personCode = p.personCode AND f.delFlag = %s)"))
        values.append(comGD.DEL_FLAG_NO)

    cond = " AND ".join(where) if where else "1 = 1"
    total = int(query.selectValue("SELECT COUNT(*) AS rowNum FROM pb_person p WHERE " + cond,
                                 tuple(values)) or 0)
    rows = query.selectList(
        "SELECT p.personCode AS personCode, p.displayName AS displayName,"
        " p.familyName AS familyName, p.familyGroupCode AS familyGroupCode,"
        " p.relation AS relation, p.email AS email, p.phone AS phone,"
        " p.birthday AS birthday, p.avatarFaceCode AS avatarFaceCode,"
        " p.source AS source, p.isConfirmed AS isConfirmed, p.delFlag AS delFlag,"
        " p.memo AS memo FROM pb_person p WHERE " + cond +
        " ORDER BY " + PERSON_SORT_SQL[orderBy][1 if int(desc) else 0] +
        " LIMIT %s OFFSET %s", tuple(values) + (s, at))

    stats = personStatsOf([str(r.get("personCode") or "") for r in rows])
    items = []
    for row in rows:
        one = stats.get(str(row.get("personCode") or "")) or {}
        items.append(personSummary(row, one.get("photoCount", 0),
                                   one.get("faceCount", 0),
                                   one.get("confirmedFaceCount", 0),
                                   one.get("yearLow"), one.get("yearHigh")))
    # 页内再排一次（见上面关于「页内有序」的说明）：SQL 侧用的是**同口径**的
    # 标量子查询，这里用批量聚合的结果 —— 两处必须同键同向，否则顺序会跳。
    if orderBy in ("photoCount", "faceCount"):
        field = orderBy
        items.sort(key=lambda it, _f=field: (
            (-int(it[_f]) if int(desc) else int(it[_f])), it["displayName"]))
    return dto.pageBody(items, p, s, total)


@router.get("/persons/{personCode}", summary="人物详情（含各年代桶分组统计）")
def getPerson(personCode: str):
    """单个人物的完整档案。

    `buckets` 是 UI 设计 P-05 那条「按年代桶分组的人脸样本」时间轴的数据源：
      [{bucketKey, photoCount, faceCount, confirmedCount, autoCount,
        centroidSampleCount, centroidEnabled}]
    - `centroidSampleCount < MIN_CENTROID_SAMPLES(3)` -> `centroidEnabled=False`，
      UI 就该显示「样本不足，该桶不参与匹配」而不是画一条假的趋势线。
    - `autoCount` 高于 `confirmedCount` 是「识别质量偏低」的信号，
      UI 据此提示用户去纠错（设计稿 §4.5 的质心健康度提示）。
    """
    row = personRow(personCode, withDeleted=True)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    stats = personStatsOf([str(row.get("personCode"))]).get(str(row.get("personCode")), {})
    out = personSummary(row, stats.get("photoCount", 0), stats.get("faceCount", 0),
                        stats.get("confirmedFaceCount", 0),
                        stats.get("yearLow"), stats.get("yearHigh"))

    buckets = {}
    for face in sqliteCommon.query_pb_face("pb_face",
                                          personCode=str(row.get("personCode")),
                                          mode="light", orderBy="shotBucket"):
        key = str(face.get("shotBucket") or "") or "(无拍摄年份)"
        item = buckets.setdefault(key, {"bucketKey": key, "faceCount": 0,
                                        "confirmedCount": 0})
        item["faceCount"] += 1
        if int(face.get("isConfirmed") or 0):
            item["confirmedCount"] += 1
    photosOfBucket = {}
    for link in query.selectList(
            "SELECT substr(p.takenAt, 1, 7) AS ym, COUNT(*) AS cnt"
            " FROM pb_photo_person pp JOIN pb_photo p ON p.photoCode = pp.photoCode"
            " WHERE pp.personCode = %s AND p.delFlag = %s GROUP BY ym",
            (str(row.get("personCode")), comGD.DEL_FLAG_NO)):
        photosOfBucket[str(link.get("ym") or "")] = int(link.get("cnt") or 0)

    centroidRows = {}
    for one in sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode=str(row.get("personCode")),
            orderBy="bucketKey"):
        key = str(one.get("bucketKey") or "")
        samples = int(one.get("sampleCount") or 0)
        centroidRows[key] = {"sampleCount": samples,
                             "enabled": bool(one.get("centroid")) and samples > 0}
    for key, item in buckets.items():
        item["autoCount"] = item["faceCount"] - item["confirmedCount"]
        item["centroidSampleCount"] = int((centroidRows.get(key) or {}).get("sampleCount", 0))
        item["centroidEnabled"] = bool((centroidRows.get(key) or {}).get("enabled", False))
        item["photoCount"] = int(photosOfBucket.get(key, 0))
    out["buckets"] = [buckets[k] for k in sorted(buckets,
                                                key=lambda k: (0 if k[:1].isdigit() else 1, k))]
    out["centroids"] = [{"bucketKey": k,
                         "sampleCount": v["sampleCount"],
                         "enabled": v["enabled"]}
                        for k, v in sorted(centroidRows.items())]
    out["family"] = {}
    groupCode = str(row.get("familyGroupCode") or "")
    if groupCode:
        families = sqliteCommon.query_pb_family("pb_family", familyCode=groupCode, limitNum=1)
        if families:
            out["family"] = {"familyCode": groupCode,
                             "familyName": str(families[0].get("familyName") or "")}
    out["totalConfirmedBuckets"] = sum(
        1 for v in centroidRows.values() if v["enabled"])
    return out


# ============================================================
# 五、GET /api/photos/{photoCode}
# ============================================================

@router.get("/photos/{photoCode}", summary="照片详情（faceCount / 出现的人 / EXIF）")
def getPhoto(photoCode: str):
    """单张照片详情（P-03 侧栏的全部数据）。

    `faces[]` 每项带 `state`（四态，UI 靠它决定人脸框画实线/虚线/点线）：
      pending   待确认（personCode 为空）
      confirmed 人工确认（isConfirmed=1）
      disputed  机器自动归属、未确认（**可否决**）
      stranger  陌生人（永久排除）
    这四态由 personCode / isConfirmed / isStranger 三字段推导（DR-16①），
    **不新增 matchType 字段**。
    """
    row = photoRow(photoCode)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "photoCode=%s 在 pb_photo 里不存在" % photoCode)
    out = photoSummary(row)
    out["exif"] = {
        "takenAt": row.get("takenAt") or None,
        "cameraModel": row.get("cameraModel") or None,
        "width": row.get("width"),
        "height": row.get("height"),
        "orientation": row.get("orientation"),
        "mimeType": row.get("mimeType") or None,
        "fileSize": int(row.get("fileSize") or 0),
        "relPath": str(row.get("relPath") or ""),
        "scannedYMDHMS": row.get("scannedYMDHMS") or None,
    }
    out["gps"] = {"lat": row.get("lat"), "lon": row.get("lon"),
                  "placeName": row.get("placeName") or None}
    out["duplicates"] = {"isDuplicate": int(row.get("isDuplicate") or 0),
                         "dupOfPhotoCode": row.get("dupOfPhotoCode") or None,
                         "movedToPhotoCode": row.get("movedToPhotoCode") or None}
    # ⚠️ fileHash **只在详情里给**，不给列表（photoSummary 刻意不给，见该函数注释）：
    #    列表里出现它只会让人以为前端该拿它拼路径；详情页要展示「文件 hash」是
    #    另一回事 —— 那是给人看的排障信息（判断两张图是不是同一份文件）。
    out["fileHash"] = str(row.get("fileHash") or "")
    out["relPathHash"] = str(row.get("relPathHash") or "")

    faces, persons, unknownCount, strangerCount = [], {}, 0, 0
    for face in sqliteCommon.query_pb_face("pb_face", photoCode=str(photoCode),
                                           mode="light", orderBy="recID"):
        person = str(face.get("personCode") or "")
        stranger = int(face.get("isStranger") or 0)
        confirmed = int(face.get("isConfirmed") or 0)
        if stranger:
            state = "stranger"
        elif person and not confirmed:
            state = "disputed"
        elif person:
            state = "confirmed"
        else:
            state = "pending"
            unknownCount += 1
        if stranger:
            strangerCount += 1
        faces.append({
            "faceCode": str(face.get("faceCode") or ""),
            "thumbUrl": "/api/face/%s" % face.get("faceCode"),
            "bbox": face.get("bbox") or None,
            "detScore": _floatOrNone(face.get("detScore")),
            "quality": _floatOrNone(face.get("quality")),
            "poseYaw": _floatOrNone(face.get("poseYaw"), 2),
            "posePitch": _floatOrNone(face.get("posePitch"), 2),
            "shotBucket": face.get("shotBucket") or None,
            "clusterCode": face.get("clusterCode") or None,
            "personCode": person or None,
            "isConfirmed": confirmed,
            "isStranger": stranger,
            "state": state,
        })
        if person:
            persons.setdefault(person, {"personCode": person, "displayName": "",
                                        "avatarFaceCode": None,
                                        "confirmedFaceCount": 0, "autoFaceCount": 0})
            if confirmed:
                persons[person]["confirmedFaceCount"] += 1
            else:
                persons[person]["autoFaceCount"] += 1

    for code, one in persons.items():
        found = personRow(code)
        if found:
            one["displayName"] = str(found.get("displayName") or code)
            one["avatarFaceCode"] = found.get("avatarFaceCode") or None
            one["thumbUrl"] = ("/api/face/%s" % found.get("avatarFaceCode")
                               if found.get("avatarFaceCode") else None)
    out["faceCount"] = int(row.get("faceCount") or 0)
    out["faces"] = faces
    out["persons"] = sorted(persons.values(), key=lambda it: it["displayName"])
    out["unknownFaceCount"] = unknownCount
    out["strangerFaceCount"] = strangerCount
    out["stateCounts"] = {"pending": unknownCount, "confirmed": 0, "disputed": 0,
                          "stranger": strangerCount}
    for one in faces:
        if one["state"] in ("confirmed", "disputed"):
            out["stateCounts"][one["state"]] += 1
    return out


# ============================================================
# 六、GET /api/places（P2，供筛选下拉与后续地图）
# ============================================================

@router.get("/places", summary="地点聚合（供筛选下拉 / 后续地图）")
def listPlaces(page: int = Query(default=1, ge=1),
               size: int = Query(default=200, le=2000, description="单页条数，上限 2000"),
               keyword: str = Query(default=None, description="地点名模糊匹配"),
               minPhotoCount: int = Query(default=None, ge=0,
                                          description="只返回照片数 >= 此值的地点"),
               orderBy: str = Query(default="photoCount",
                                    description="photoCount / placeName / "
                                                "lastShotYear / firstShotYear / placeCode"),
               desc: int = Query(default=1),
               live: int = Query(default=0,
                                 description="1 = 强制走实时聚合（降级路径，慢）")):
    """按地点聚合的照片张数（降序）。分页体与其它列表接口一致。

    ⚠️ 数据来自 **`pb_place` 地点字典表**（步骤 9 新增），不是每次现算。
       代价对比（10 万张实测）：字典表只扫几十~几百行，
       而现算的 `GROUP BY placeName` 是全表扫 **p50 95ms** 且随库线性增长。

    响应里的 `source` 字段说明本次数据**从哪来**
    -------------------------------------------------
      `"dictionary"` —— 读的是 `pb_place`（正常路径）
      `"lib"`        —— 字典表不存在或**一行都没有**（还没 rebuild 过），
                        降级成实时全表聚合。**这条是慢的那条。**
                        `dictionaryRebuilt=false` 时前端应提示「点一次刷新地点」

    ⚠️ `photoCount` 是**缓存值**：扫描新照片后不会自动更新。
       `modifyYMDHMS`（响应根的 `dictionaryRebuiltAt`）就是最后一次复算的时刻。
       要刷新请调 `POST /api/places/rebuild`。
    ⚠️ `total` 是**不同地点的个数**（不是照片数）：筛选下拉要的是
       「有多少个地点可选」，而每一项里的 photoCount 才是张数。
    """
    p, s = dto.clampPage(page, size, defaultSize=200)
    at = dto.offsetOf(p, s)
    if orderBy not in placeStore.PLACE_SORT_SQL:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "orderBy 只支持 %s，收到 %r"
                           % (sorted(placeStore.PLACE_SORT_SQL), orderBy))

    useLive = bool(int(live)) or not query.tableExists("pb_place")
    fromDict = placeStore.listPlaces(keyword=keyword or "",
                                     minPhotoCount=minPhotoCount,
                                     orderBy=orderBy, desc=bool(int(desc)),
                                     limitNum=s, offsetNum=at) if not useLive \
        else {"items": [], "total": 0, "missingTable": True}
    if fromDict["total"] > 0 and not useLive:
        items = [_placeSummary(r, "dictionary") for r in fromDict["items"]]
        rebuiltAt = next((str(r.get("modifyYMDHMS") or "")
                          for r in fromDict["items"] if r.get("modifyYMDHMS")), "")
        body = dto.pageBody(items, p, s, fromDict["total"])
        body["source"] = "dictionary"
        body["dictionaryRebuilt"] = True
        body["dictionaryRebuiltAt"] = rebuiltAt or None
        body["note"] = ("photoCount 是缓存值（最后一次复算 = dictionaryRebuiltAt）；"
                        "扫描新照片后请调 POST /api/places/rebuild 刷新")
        return body

    # ---- 降级：实时聚合（慢，但一定拿得到数据）----
    # 排序参数照传：两条路径的顺序必须一致（否则用户会看到"刷新一下顺序变了"）
    rows = placeStore.liveAggregatePlaces(limitNum=2000, keyword=keyword or "",
                                          orderBy=orderBy, desc=bool(int(desc)))
    items = [_placeSummary(r, "lib") for r in rows]
    body = dto.pageBody(items[at:at + s], p, s, len(items))
    body["source"] = "lib"
    body["dictionaryRebuilt"] = False
    body["dictionaryRebuiltAt"] = None
    body["note"] = ("pb_place 地点字典%s —— 本次是实时全表聚合（慢）。"
                    "调一次 POST /api/places/rebuild 建好字典后，"
                    "这个接口就只扫字典表了"
                    % ("表不存在（升级后请先跑一次 tools\\build_db.py）"
                       if not query.tableExists("pb_place") else "还是空的"))
    return body


def _placeSummary(row: dict, source: str) -> dict:
    """`pb_place` 行 / 实时聚合行 -> 统一的地点条目。

    ⚠️ 实时聚合行**没有** placeCode（它压根没进过字典表），这里当场按
       `makePlaceCode(placeName)` 补一个 —— 用的就是 rebuild 时那套派生规则，
       所以前端拿到的编码在「降级」与「字典」两条路径下**是同一个值**，
       切换路径不会让已缓存的 URL/选中项失效。
    """
    name = str(row.get("placeName") or "")
    return {"placeCode": str(row.get("placeCode") or "") or placeStore.makePlaceCode(name),
            "placeName": name,
            "photoCount": int(row.get("photoCount") or 0),
            # yearLow/yearHigh 与其它接口口径一致（旧字段名保留，前端不用改）
            "yearLow": _intOrNone(row.get("firstShotYear") or row.get("yearLow")),
            "yearHigh": _intOrNone(row.get("lastShotYear") or row.get("yearHigh")),
            # 6 位小数 ≈ 0.1m 精度，对地图打点足够；与列定义 DECIMAL(10,7) 同量级
            "centerLat": _floatOrNone(row.get("centerLat"), 6),
            "centerLon": _floatOrNone(row.get("centerLon"), 6),
            "sourceKind": int(row.get("source") or 0),
            "dataSource": source}


@router.post("/places/rebuild", summary="重建地点字典（pb_place 全量幂等复算）")
def rebuildPlaces():
    """从 `pb_photo` 全量复算 `pb_place`（**幂等**：不清表，按 placeCode upsert）。

    为什么要有这个接口
    ------------------
      `pb_place` 里的 `photoCount` 等是**派生缓存**。扫描新照片之后它不会自动更新
      （增量维护要挂在扫描链路的每个写点上，漏一处就是「照片数慢慢对不上」）。
      所以刷新是一次显式的、可重复执行的全量复算。

    ⚠️ **为什么不落 `pb_review_log`**：硬约束「每个写操作都要落日志」针对的是
      **会影响识别结果或人工判断**的改动（人脸归属、人员档案）。本接口只重建
      纯派生缓存，一个字段都不来自人工输入，且可从 `pb_photo` 100% 重算还原 ——
      和「生成缩略图」同类，而缩略图也不落纠错日志。
      审计要看 `pb_place.modifyYMDHMS`（= 最后一次复算时刻）。
    """
    try:
        info = placeStore.rebuildPlaces()
    except RuntimeError as e:
        raise dto.ApiError(dto.CODE_DB_ERROR, str(e))
    return dto.okBody(source="rebuilt", **info)


# ============================================================
# 七、GET /api/overview（侧栏/概览页的角标，聚合一次给全）
# ============================================================

@router.get("/overview", summary="概览（侧栏角标 / P-01 概览页）")
def getOverview():
    """一次给全前端首屏要的全部计数，**省掉前端 5 个并行请求**。

    ⚠️ 两个"待确认"不是一个数：
      pendingCount  = 人脸待确认条数（queue.countPending）
      disputedCount = 「我不同意」条数（queue.countDisputed，机器自动认的）
    pb_scan_job.pendingCount 是**另一个东西**（疑似移动/重命名的**张数**，DR-11），
    在这里叫 `movedPendingPhotos`，绝不混进上面两个。
    """
    from processor.review import queue as reviewQueue
    from schedule import scanScheduler as scheduler

    states = reviewQueue.countStates()
    photoTotal = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo WHERE delFlag = %s",
        (comGD.DEL_FLAG_NO,)) or 0)
    personTotal = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_person WHERE delFlag = %s",
        (comGD.DEL_FLAG_NO,)) or 0)
    moved = 0
    for one in sqliteCommon.query_pb_scan_job("pb_scan_job", mode="light"):
        moved += int(one.get("pendingCount") or 0)
    latest = scheduler.ScanScheduler(verbose=False).listJobs(1)
    return {"ok": True, "photoCount": photoTotal, "personCount": personTotal,
            "pendingCount": int(states.get("pending") or 0),
            "disputedCount": int(states.get("disputed") or 0),
            "confirmedCount": int(states.get("confirmed") or 0),
            "strangerCount": int(states.get("stranger") or 0),
            "movedPendingPhotos": moved,
            "latestJob": _jobBrief(latest[0]) if latest else None}


def _jobBrief(row: dict) -> dict:
    """pb_scan_job 行 -> 概览页要的一行摘要。"""
    if not row:
        return {}
    return {"jobCode": str(row.get("jobCode") or ""),
            "jobStatus": str(row.get("jobStatus") or comGD.JOB_IDLE),
            "jobStatusText": comGD.JOB_STATUS_TEXT.get(str(row.get("jobStatus") or ""), ""),
            "processedCount": int(row.get("processedCount") or 0),
            "totalCount": int(row.get("totalCount") or 0),
            "addedCount": int(row.get("addedCount") or 0),
            "finishedYMDHMS": row.get("finishedYMDHMS") or None}


# ============================================================
# 八、桶工具（供人物详情与前端渲染共用）
# ============================================================

def bucketSpan(bucketKey: str) -> tuple:
    """桶键 -> (起始年, 结束年)。解析不出返回 (0, 0)。

    转发 bucket.parseBucketKey，不在这里重写一遍区间推导
    （queue._shotSpanOf 的注释里记过一次「把 (起, 宽) 当成 (起, 止)」的错，
    同一个坑不能再踩第二遍）。
    """
    got = bucket.parseBucketKey(str(bucketKey or ""))
    if not got:
        return (0, 0)
    return (int(got[0] or 0), int(got[1] or 0))


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("browse.py _VERSION:", _VERSION)
    print("库:", sqliteCommon.dbFilePath() or "(未连接)")
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    print("照片排序白名单:", sorted(PHOTO_SORT_COLUMNS.keys()))
    print("人物排序白名单:", sorted(PERSON_SORT_SQL.keys()))
    _groups, _unknown = _yearMonthIndex()
    print("年月分组 %d 个，日期未知 %d 张" % (len(_groups), _unknown))
    print("前 5 个年月:", _groups[:5])
