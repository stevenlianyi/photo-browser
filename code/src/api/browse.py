#! /usr/bin/env python3
#encoding: utf-8

#Filename: browse.py
#Description: photo-browser 浏览类接口（步骤 9·P0）—— 时间线 / 照片列表 / 人物 / 详情
#
# 主要端点
# --------
#   GET /api/timeline按年月分组（三段式：年表 / 某年月份 / 某年某月照片）
#   GET /api/photos               分页 + 筛选 + 排序
#   GET /api/persons              人物网格（含 photoCount / 年代跨度）
#   GET /api/persons/{personCode} 人物详情（含各年代档分组统计）
#   GET /api/persons/{personCode}/faces     人脸样本（Tab2）
#   GET /api/persons/{personCode}/timeline  按年代档分组（Tab1）
#   GET /api/persons/{personCode}/places    这个人去过的地方（R5 新增，Tab1 下方区块）
#   GET /api/photos/{photoCode}   照片详情（含 faceCount / 出现的人 / EXIF）
#   GET /api/duplicates*          重复照片分组与并排对比
#   GET /api/overview             首屏计数
#
# ⚠️ R5（DR-37）：`/api/places*` **整个命名空间在 `api/place.py`**，
#   本模块不再注册任何 places 路由（理由写在下面第六节的说明里）。
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

_VERSION = "20261007"

_LOG = misc.setLogNew("apiBrowse", "apibrowse.log")

router = APIRouter(tags=["browse"])

#: 时间线上「日期读不出来」的分组键（shotYear 为空、takenAt 为空/畸形）。
#: 刻意**保留**这一组而不是过滤掉：扫描器对截图类会写 shotYear=NULL
#: （见 meta.resolveShotYear 的 SCREENSHOT 分支），过滤掉等于让这批照片
#: 在时间线上凭空消失，而用户「明明存过这张图」。
UNKNOWN_PERIOD: str = "unknown"

#: /api/photos 允许的排序字段 -> **可直接进 ORDER BY 的表达式**（含表别名 p）。
#: 白名单制：排序字段直接进 ORDER BY，不是值可以不校验；但它会**改变结果顺序**，
#: 写错一个名字用户只会觉得「排序不对」，所以宁可少给几个也不给错。
#:
#: ⚠️ 值是**表达式**而不是裸列名（原来拼的是 `p.%s`）：`shotYear` 必须按
#:    **有效拍摄年**排（DR-42），否则修正成 1960 的照片会排在 2019 那一段，
#:    而列表里显示的是 1960 —— 用户看到的是「排序坏了」。
PHOTO_SORT_COLUMNS: dict = {
    "takenAt": "p.takenAt",
    "shotYear": comGD.sqlEffectiveShotYear("p"),
    "fileSize": "p.fileSize",
    "recID": "p.recID",
    "width": "p.width",
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


def effectiveYearOf(row: dict):
    """pb_photo 行 -> **有效拍摄年** int / None（DR-42）。

    口径与 SQL 侧的 `comGD.sqlEffectiveShotYear()` **逐字等价**：
    人工修正值（shotYearOverride）优先，没有就回落到机器读到的 shotYear。
    Python 侧与 SQL 侧必须同源 —— 一边按修正年、一边按 EXIF 年，
    症状是「列表里显示 1960、按 1960 筛却筛不到」，且两处代码各自看都正确。
    """
    one = row or {}
    override = one.get("shotYearOverride")
    year = override if override not in (None, "") else one.get("shotYear")
    if year is None or str(year).strip() == "":
        return None
    try:
        return int(year)
    except (TypeError, ValueError):
        return None


def rotateDegOf(row: dict) -> int:
    """pb_photo 行 -> 人工旋转角度 int（DR-43）。**只认 0/90/180/270**。

    口径与 Python 侧唯一写入口 `processor/photoRotate.normalizeAngle` 一致：
    脏值（NULL / 空 / 非数字 / 越界）一律当 **0**（未修正）而不是原样透出 ——
    前端拿到一个 45 去 `rotate(45deg)` 会得到一个**斜着的**照片，
    而用户根本没有"任意角度"这个功能，谁也说不清它是从哪来的。
    """
    value = (row or {}).get("rotateDeg")
    if value is None or str(value).strip() == "":
        return 0
    try:
        angle = int(value)
    except (TypeError, ValueError):
        return 0
    return angle if angle in (0, 90, 180, 270) else 0


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
    # ⚠️ `placeZh` 不在行里，而是在这里现查（placeStore.nameZhOfPlace）。
    #    `pb_photo` 没有 placeCode 列，照片 -> 地点只能按**字符串**关联；
    #    用映射表而不是 SQL JOIN 的理由（重复 placeName 会让 JOIN
    #    把同一张照片返回两次）写在 placeStore.nameZhMap 的 docstring 里。
    #
    # ⚠️ R4b：这里的「地点」是**聚合键**（目录名优先），不是 `placeName` 这一列
    # ------------------------------------------------------------------
    #    `placeStore.placeKeyOf()` 是与 `pb_place` 聚合、`resolvePlaceFilter`
    #    筛选**逐字等价**的那把键（DR-32）。
    #    直接用 `row.get("placeName")` 的后果：那 598 张「目录名带地点」的
    #    照片在本接口里**没有地点**（它们的 placeName 是空的）——
    #    列表上看不出地点、点进去的地点链接也带不上值，而 114 张照片
    #    明明就躺在 `2013.07.26 华盛顿/` 目录里。
    placeName = placeStore.placeKeyOf(row.get("placeName"),
                                      row.get("placeNameDir")) or None
    # DR-42：对外一律给**有效拍摄年**（人工修正优先）。三个字段的分工：
    #   shotYear         —— 有效年（筛选口径、展示口径，与划分年代档同源）
    #   shotYearExif     —— 机器读到的原值（EXIF -> 文件名 -> mtime）
    #   shotYearOverride —— 人工修正值；None = 没修正过「恢复自动」才有意义
    # 行里若没有 shotYearOverride 列（某条 SQL 没选它）-> 视作"未修正"，
    # shotYear 即 shotYearExif，不会因为漏选列而把有效年算成 None。
    override = row.get("shotYearOverride")
    if override in ("", None):
        override = None
    exifYear = row.get("shotYear")
    return {
        "photoCode": photoCode,
        "thumbUrl": "/api/thumb/%s?size=400" % photoCode,
        "originalUrl": "/api/original/%s" % photoCode,
        "relPath": str(row.get("relPath") or ""),
        "hasGps": bool(hasGps),
        "takenAt": row.get("takenAt") or None,
        "shotYear": effectiveYearOf(row),
        "shotYearExif": None if exifYear is None else int(exifYear),
        "shotYearOverride": None if override is None else int(override),
        "placeName": placeName,
        # ⚠️ **显示名 = placeZh ?? placeName**（前端照此实现，别改契约）：
        #    placeZh 是境内地点的中文显示名，placeName 是**英文原值**。
        #    保留英文原值不是为了显示，而是**排障依据**：中文名对不对、
        #    是不是被 rebuild 冲掉了、境外是不是本来就该空，都要看它。
        #    界面上「地点」的 tooltip 可以同时显示两者。
        #    ⚠️ 不要用 placeZh **覆盖** placeName 返回 —— 那会让
        #    「英文原值是什么」这个问题在库里和接口里都再也答不了。
        "placeZh": (placeStore.nameZhOfPlace(placeName) or None) if placeName else None,
        "cameraModel": row.get("cameraModel") or None,
        "width": int(width) if width is not None else None,
        "height": int(height) if height is not None else None,
        # DR-43：人工旋转角度（0/90/180/270，0=未修正）。**显示属性**，不是识别事实。
        # ⚠️ 漏透出这一列的后果是最难发现的那种不一致：接口 200、字段缺失、
        #    「照片流里转了、地点详情里没转」——界面静默错，不报任何错。
        # ⚠️ 行里没有这一列（某条 SQL 没选它）-> 当 0（未修正），
        #    不能因为漏选列就让整张照片显示不出来。
        "rotateDeg": rotateDegOf(row),
        "fileSize": int(row.get("fileSize") or 0),
        "mimeType": row.get("mimeType") or None,
        "faceCount": int(row.get("faceCount") or 0),
        "isDuplicate": int(row.get("isDuplicate") or 0),
        "isMissing": int(row.get("isMissing") or 0),
        "scanState": int(row.get("scanState") or 0),
    }


def personSummary(row: dict, photoCount: int = 0, faceCount: int = 0,
                  confirmedFaceCount: int = 0, yearLow=None, yearHigh=None,
                  withCategories: bool = True, coverFaceCode: str = None) -> dict:
    """pb_person 行 -> 人物卡片 / 详情头。

    四个计数字段的口径（**验收第 16 条要逐个核对**）
    ---------------------------------------------
      photoCount        = pb_photo_person 里该人的关联行数
        ⚠️ **不是** SUM(pb_photo.faceCount)—— 那是「这个库一共有多少张脸」，
        不是「他出现在多少张照片里」。一张 3 人合影里他只算 1 张。
      faceCount         = pb_face 里 personCode = 他 的行数（**含自动归属**）
      confirmedFaceCount= 同上但 isConfirmed = 1（**只有这个进质心**，DR-16③）
      yearLow/yearHigh  = 他出现过的照片的 shotYear 最小/最大值（年代跨度）

    头像为什么是**两个字段**（DR-40）
    ----------------------------------
      `avatarFaceCode` = **用户指定的默认**（唯一写入口是
                         `PATCH /api/contacts/{personCode}`，DR-41；
                         正式库里绝大多数为空 —— 这就是卡片曾全是首字母的原因）
      `coverFaceCode`  = **实际展示的那张**（= `avatarFaceCode`；为空则回退到
                         调用方用 `personCoverOf()` 算出的「代表脸」）
      ⇒ 前端渲染只认 `coverFaceCode`（或直接用服务端拼好的 `thumbUrl`）。
      ⚠️ 代表脸是**展示层回退，绝不写库**：`avatarFaceCode` 为空是**合法状态**，
         不能变成「浏览一次就写一次库」。
    """
    if not row:
        return {}
    code = str(row.get("personCode") or "")
    # 头像两个字段：用户指定的默认（原样返回）vs 实际展示的那张（DR-40）
    avatarCode = row.get("avatarFaceCode") or None
    # ⚠️ `coverFaceCode` **必须由 `personCoversOf()` 解析好后传进来** ——
    #    那里已经判断过「默认那张脸还在不在他名下」，失效时给的就是代表脸。
    #    这里**不能**再写成 `avatarCode or coverFaceCode`：那会把
    #    「用户选过但已经被移除的那张脸」当成封面，卡片指向一个必然 404 的图。
    coverCode = str(coverFaceCode) if coverFaceCode else None
    out = {
        "personCode": code,
        "displayName": str(row.get("displayName") or ""),
        "familyName": row.get("familyName") or None,
        "familyGroupCode": row.get("familyGroupCode") or None,
        "relation": row.get("relation") or None,
        "email": row.get("email") or None,
        "phone": row.get("phone") or None,
        "birthday": row.get("birthday") or None,
        "avatarFaceCode": avatarCode,
        "coverFaceCode": coverCode,
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
        # ⚠️ 头像 URL 指向 coverCode（不是 avatarFaceCode）：默认没设过的人
        #    也要能显示照片，否则卡片又退回一片首字母（DR-40）。
        "thumbUrl": ("/api/face/%s" % coverCode) if coverCode else None,
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
            "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.shotYearOverride, p.rotateDeg, p.placeName,"
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
        "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.shotYearOverride, p.rotateDeg, p.placeName,"
        " p.cameraModel, p.width, p.height, p.fileSize, p.mimeType,"
        " p.faceCount, p.isDuplicate, p.isMissing, p.scanState"
        " FROM pb_photo p WHERE " + cond +
        " ORDER BY p.takenAt DESC, p.recID DESC LIMIT %s OFFSET %s",
        tuple(values) + (s, at))
    return {"ok": True, "scope": "photos", "total": total, "year": int(year),
            "month": int(month), "ym": ym, "unknownCount": unknownCount,
            "page": dto.pageBody([photoSummary(r) for r in rows], p, s, totalIn)}


def anchorOffsetOf(cond: str, values=(), sortColumn: str = "p.takenAt",
                   anchorPhotoCode: str = None):
    """锚点照片在**当前筛选 + `sortColumn DESC`** 下的行偏移（0 基）；不适用时返回 None。

    为什么需要它（DR-31 补：翻页范围落在「看不见的那一段」）
    --------------------------------------------------
      照片详情页从人物 / 地点详情进来时带 `?scope=`，左右箭头沿**那一批照片**
      翻，服务端按 `takenAt DESC` 分页（最新的在前）。而用户是从时间轴 /
      人脸样本里点进去的，很可能是**一张老照片**：某个人的 2686 张照片里，
      2005 年那张排在四十多页之后 —— 前端只拉了第 1 页（60 条），
      于是「当前照片不在已加载列表里」⇒ **两个箭头全禁用**，
      界面上就是「从人物库进来之后翻页坏了」，而且不报任何错。

      不做「前端从第 1 页顺序加载直到找到」：那是 40 次请求 + 2400 行
      没人会看的缩略图。让服务端把「它在这一批里的第几页」算出来，
      前端一次请求就能定位到它。

    ⚠️ 返回 None = **不定位**（锚点不存在 / 不满足筛选 / 排序键为空）：
       调用方保持原 `page` 参数 —— 宁可回第 1 页，也不要猜一个错的位置。
    ⚠️ 只在 **DESC + 排序键非空** 时定位。调用方只有「scope 翻页」一种，
       它的排序固定 `takenAt DESC`；NULL 在 ASC / DESC 下的位置得分别处理，
       猜错会让翻页跳到完全无关的一段 —— 比「不定位」坏得多。
    """
    code = str(anchorPhotoCode or "").strip()
    if not code:
        return None
    # 锚点必须先**在筛选结果里**（否则算出来的页是另一批照片的页码）
    rows = query.selectList(
        "SELECT p.recID AS recID, " + sortColumn + " AS sortValue FROM pb_photo p"
        " WHERE p.photoCode = %s AND " + cond,
        (code,) + tuple(values))
    if not rows or rows[0].get("sortValue") is None:
        return None
    value = rows[0].get("sortValue")
    recID = rows[0].get("recID")
    # DESC：排在锚点前面的 = 排序键更大的 + 键相同但 recID 更大的
    return int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE " + cond +
        " AND (" + sortColumn + " > %s"
        " OR (" + sortColumn + " = %s AND p.recID > %s))",
        tuple(values) + (value, value, recID)) or 0)


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
               desc: int = Query(default=1, description="1 倒序（时间线默认）/ 0 正序"),
               anchorPhotoCode: str = Query(default=None,
                                            description="锚点照片：返回它所在的那一页"
                                                        "（scope 翻页定位用，见 anchorOffsetOf）")):
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
    ⚠️ **但两套值都收**（DR-29③）：下拉现在给的是**中文名**（`nameZh`），
       所以这个参数可能是「新疆维吾尔自治区 · 和静县」，也可能是英文原值
       「CN, Xinjiang Uygur Zizhiqu, Araltobe」。翻译在
       `placeStore.resolvePlaceFilter()` 里做 —— 它把中文名查回那一行的
       `placeName` 再精确匹配，于是**两种写法返回同一批照片**。
       ⚠️ 仍然**不是 LIKE**：中文名这一侧同理，「…和静县」与「…和什托洛盖乡」
       模糊匹配会互相命中，用户会以为筛错了。

    锚点定位（`anchorPhotoCode`）
    --------------------------
      给了它，且这张照片**在本筛选结果里**时，返回的是**它所在的那一页**
      （入参 `page` 被忽略；响应里的 `page` 是算出来的页）——
      照片详情页的 scope 翻页靠它一次就落到正确位置，不必从第 1 页顺序翻。
      不适用（照片不满足筛选 / 排序键为空 / 排序是升序）时就当没给，
      按原 `page` 返回。见 `anchorOffsetOf()`。
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

    # DR-42：按**有效拍摄年**筛。人工把一张翻拍件修正成 1960 之后，
    # 它必须能被「1956-1965」这类区间筛出来 —— 用裸 p.shotYear 的话
    # 它仍按 EXIF 的 2019 参与筛选，而列表里显示的却是 1960：**看出来不一致**。
    # ⚠️ 表达式不匹配 idx_pb_photo_shotYear（单列索引），这一条会走全表扫；
    #    照片量级（万张）可接受，见 comGD.sqlEffectiveShotYear 的注释。
    effectiveYear = comGD.sqlEffectiveShotYear("p")
    if shotYearFrom is not None:
        where.append(effectiveYear + " >= %s")
        values.append(int(shotYearFrom))
    if shotYearTo is not None:
        where.append(effectiveYear + " <= %s")
        values.append(int(shotYearTo))
    if placeName:
        # ⚠️ 两套值都收（中文名 / 英文原值），翻译在 placeStore 里做。
        #   这里**只拿片段与参数**，不自己写 `p.placeName = %s` ——
        #   口径散在两处的话，将来改一处就会漏另一处，而症状是
        #   「中文下拉选不中、英文能选中」，极难联想到是口径分叉了。
        placeSql, placeValues = placeStore.resolvePlaceFilter(placeName)
        where.append(placeSql)
        values.extend(placeValues)
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
    # DR-31 补：入口声明了 scope 时，前端把「当前这张照片」当锚点传进来，
    # 服务端算出它在这一批里的页 —— 否则一张排在第 40 页的老照片会让
    # 「人物库进来」的左右箭头全禁用（见 anchorOffsetOf 的注释）。
    if int(desc):
        anchorAt = anchorOffsetOf(cond, values, sortColumn, anchorPhotoCode)
        if anchorAt is not None:
            p = anchorAt // s + 1
            at = dto.offsetOf(p, s)
    rows = query.selectList(
        "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.shotYearOverride, p.rotateDeg, p.placeName,"
        " p.cameraModel, p.width, p.height, p.fileSize, p.mimeType,"
        " p.faceCount, p.isDuplicate, p.isMissing, p.scanState"
        " FROM pb_photo p WHERE " + cond +
        " ORDER BY " + sortColumn + " %s, p.recID %s LIMIT %%s OFFSET %%s"
        % ("DESC" if int(desc) else "ASC",
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

    # ③ 年代跨度：关联行 JOIN pb_photo 取 MIN/MAX(**有效**拍摄年)（DR-42）。
    #    卡片上「1938-2024」这行数字与年代档必须同源，否则修正过的照片会让
    #    「跨度」与「时间轴里的年代档」互相矛盾。
    effectiveYear = comGD.sqlEffectiveShotYear("p")
    for row in query.selectList(
            "SELECT pp.personCode AS personCode, MIN(" + effectiveYear + ") AS yearLow,"
            " MAX(" + effectiveYear + ") AS yearHigh"
            " FROM pb_photo_person pp JOIN pb_photo p ON p.photoCode = pp.photoCode"
            " WHERE pp.personCode IN (" + marks + ")"
            " AND p.delFlag = %s GROUP BY pp.personCode",
            tuple(personCodes) + (comGD.DEL_FLAG_NO,)):
        code = str(row.get("personCode") or "")
        if code in out:
            out[code]["yearLow"] = _intOrNone(row.get("yearLow"))
            out[code]["yearHigh"] = _intOrNone(row.get("yearHigh"))
    return out


def personCoverOf(personCodes: list) -> dict:
    """一批人的**代表脸**：`{personCode: faceCode}`（DR-40）。

    为什么需要它
    ------------
      `pb_person.avatarFaceCode` 是「用户指定的默认」，而正式库里绝大多数为
      空 —— 它此前**根本没有写入入口**（`CONTACT_PATCH_COLUMNS` 不含它），
      于是人物卡片只能退回首字母，而这些人其实都有大量现成样本。
      代表脸就是「**没指定过默认时，展示哪一张**」。

    取值优先级（**人工确认优先**）
    -----------------------------
      isConfirmed DESC  用户逐张核对过的脸最可信
      detScore   DESC   检测分高的脸更大、更正
      quality    DESC   质量分（模糊 / 侧脸会被压下去）
      regYMDHMS  ASC    最后兜底，保证同分时结果**稳定** —— 否则同一页刷新
                        两次可能换一张脸，用户会以为数据在乱跳

    ⚠️ **一条批量 IN + Python 选优**（不是「批量 IN + 相关子查询」）——
       后者是 Σ(每人脸数²)：跑完一次匹配、自动归属把上千张脸挂到同一人名下之后，
       单这一条 SQL 就要 8.8 秒，而本函数是所有「头像从哪来」的唯一口径，
       拖住的是所有给人头像的接口（表现为人物库页面卡在骨架屏）。
       实现处的长注释记了完整的实测数据与口径不变的验证结论。

    ⚠️ **不做 `thumbStore.exists()` 探测**：列表接口不该为每行去碰文件系统
       （一页 24 次 stat，磁盘异常会把列表一起拖死）。裁剪图缺失由前端
       `@error` 回退首字母兜底 —— 那是展示层的事，不是数据层的事。

    ⚠️ 返回的字典**只含查得到的编码**：一个没有任何脸的人**不在字典里**，
       调用方用 `.get(code)` 拿到 None（= 前端退回首字母）。
    """
    codes = [str(one) for one in (personCodes or []) if str(one or "")]
    if not codes:
        return {}
    # ⚠️ 一次批量 IN 取**轻量列**，最优脸在 Python 里挑 —— 不用相关子查询
    # --------------------------------------------------------------------
    #   原写法是**一条**带相关子查询的批量 SQL：
    #     SELECT f.personCode, f.faceCode FROM pb_face f
    #      WHERE f.personCode IN (...) AND f.delFlag = %s
    #        AND f.faceCode = (SELECT g.faceCode FROM pb_face g
    #                          WHERE g.personCode = f.personCode AND g.delFlag = %s
    #                          ORDER BY g.isConfirmed DESC, ... LIMIT 1)
    #   外层用 personCode 索引扫出**这批人的全部脸**，而内层子查询对**扫到的
    #   每一张脸**都要重新排一次「这个人自己的脸」⇒ 复杂度 Σ(每人脸数²)。
    #
    #   平时看不出来：人工确认过的脸每人只有几张（正式库 <10 张）。
    #   但**跑完一次匹配**之后，自动归属会把上千张脸挂到同一个人名下
    #   （正式库实测某人 2686 张脸），于是这**一条** SQL 从毫秒涨到 **8.8 秒**
    #   （/api/persons 整体 8.9s，其余 30 条查询加起来 0.15s）。
    #   而它是「头像从哪来」的唯一口径 —— 人物库列表 / 人物详情 / 联系人 /
    #   家庭组 / 地点人物全走它 ⇒ 表现为**人物库页面一直停在骨架屏**上。
    #   （实测：改法前后 5.6s -> 0.037s，且 24 人选出的 faceCode 逐条一致。）
    #
    #   现在：**仍然一条批量查询**（不随页大小增长，见 test_api_person_avatar 的
    #   回归用例），但只取排序需要的轻量列（**不带 embedding**），
    #   在 Python 里按排序键取每人最小的那一个 —— 复杂度 O(Σ 每人脸数)。
    #   传输量有界：一页 24 人 = 几千行；全库调用（contacts）最多 = 全库归属行数。
    #   排序键与原来**逐字一致**：isConfirmed DESC → detScore DESC → quality DESC
    #   → regYMDHMS ASC（最后一项让同分时结果稳定，头像不会每次刷新换一张）。
    marks = ", ".join(["%s"] * len(codes))
    best = {}
    for row in query.selectList(
            "SELECT g.personCode AS personCode, g.faceCode AS faceCode,"
            " g.isConfirmed AS isConfirmed, g.detScore AS detScore,"
            " g.quality AS quality, g.regYMDHMS AS regYMDHMS"
            " FROM pb_face g WHERE g.personCode IN (" + marks + ")"
            " AND g.delFlag = %s",
            tuple(codes) + (comGD.DEL_FLAG_NO,)):
        code = str(row.get("personCode") or "")
        faceCode = str(row.get("faceCode") or "")
        if not code or not faceCode:
            continue
        # 全序、无并列残留：最后一维 regYMDHMS 是唯一化键（与 SQL 的 ORDER BY
        # 逐字对应）。缺值时用空串兜底，保证比较不抛异常。
        key = (-int(row.get("isConfirmed") or 0),
               -float(row.get("detScore") or 0.0),
               -float(row.get("quality") or 0.0),
               str(row.get("regYMDHMS") or ""))
        current = best.get(code)
        if current is None or key < current[0]:
            best[code] = (key, faceCode)
    return {code: one[1] for code, one in best.items()}


def _liveFaceOwnersOf(faceCodes: list) -> dict:
    """`{faceCode: personCode}` —— 只含**未软删且已归属**的脸。

    唯一用途：判断「用户指定的默认头像那张脸」现在还挂在不在这个人名下
    （见 `personCoversOf`）。一次 IN 查询，不逐张查。
    """
    codes = [str(one) for one in (faceCodes or []) if str(one or "")]
    if not codes:
        return {}
    marks = ", ".join(["%s"] * len(codes))
    out = {}
    for row in query.selectList(
            "SELECT f.faceCode AS faceCode, f.personCode AS personCode"
            " FROM pb_face f WHERE f.faceCode IN (" + marks + ")"
            " AND f.delFlag = %s",
            tuple(codes) + (comGD.DEL_FLAG_NO,)):
        code = str(row.get("faceCode") or "")
        owner = str(row.get("personCode") or "")
        if code and owner:
            out[code] = owner
    return out


def personCoversOf(rows: list) -> dict:
    """一批 `pb_person` 行 -> `{personCode: 实际展示用的 faceCode}`（DR-40）。

    **这是「人物头像长什么样」的唯一口径出口** —— 所有会给出头像的接口
    （`/api/persons`、`/api/persons/{code}`、`/api/contacts`、`/api/families/{code}`、
    `/api/places/{code}/persons`）都必须走它。散在各处的后果是同一个人的头像
    在人物库是照片、在联系人页是首字母，而两边**都不报错**。

    两级（先看用户选的，再退回代表脸）
    ---------------------------------
      ① 用户指定的默认（`pb_person.avatarFaceCode`）—— **前提是那张脸还活着**
         且仍属于这个人；
      ② 否则用代表脸（`personCoverOf`：人工确认优先 → detScore/quality 最高）。

    ⚠️ 为什么在这里判「还活着」（而不是在 fix/merge 里清空 `avatarFaceCode`）
       DR-41④ 的刻意选择：把清理铺进 `review.fix` / `merger.merge` 会把
       「头像」与「归属」两个正交的东西耦合起来，而**回退链本来就必须存在**
       （首次浏览时 `avatarFaceCode` 就是空的）。代价就是必须**在这里兜底** ——
       否则卡片会指向一张已删除的脸（`/api/face` 回 404 = 破图）。
       ⚠️ 顺带一个好处：**只有真的有人设过默认时才多查一次** ——
       正式库里 `avatarFaceCode` 绝大多数为空，这个 IN 查询直接不发生。
    """
    people = [row for row in (rows or []) if str(row.get("personCode") or "")]
    if not people:
        return {}
    codes = [str(row.get("personCode")) for row in people]
    covers = personCoverOf(codes)
    avatars = {}
    for row in people:
        avatar = str(row.get("avatarFaceCode") or "")
        if avatar:
            avatars[str(row.get("personCode"))] = avatar
    owners = _liveFaceOwnersOf(list(set(avatars.values()))) if avatars else {}
    out = {}
    for code in codes:
        avatar = avatars.get(code)
        if avatar and owners.get(avatar) == code:
            out[code] = avatar
        elif covers.get(code):
            out[code] = covers[code]
    return out


@router.get("/persons", summary="人物网格（photoCount / 代��跨度）")
def listPersons(page: int = Query(default=1, ge=1),
                size: int = Query(default=dto.DEFAULT_PAGE_SIZE),
                keyword: str = Query(default=None,
                                     description="姓名/拼音/ 邮箱 / 电话 模糊匹配"),
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
        # ⚠️ displayNamePinyin 是 `common/pinyin.personPinyin` 生成的**派生检索串**
        #   （"wangxiaoming wxm"），让用户能只用拼音就把人搜出来。
        #   不加它的话搜索框只认汉字 —— 看着能用，实际上半个名字库搜不到。
        #   ⚠️ 它**不在**下面的 SELECT 列表里，也**不进** personSummary：
        #   派生列不该出现在响应里（前端 PersonForm 会「看得见摸不着」）。
        where.append("(p.displayName LIKE %s OR p.displayNamePinyin LIKE %s"
                     " OR p.familyName LIKE %s OR p.email LIKE %s OR p.phone LIKE %s)")
        like = "%%%s%%" % str(keyword)
        values.extend([like, like, like, like, like])
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

    codes = [str(r.get("personCode") or "") for r in rows]
    stats = personStatsOf(codes)
    # 封面脸（DR-40）：**一次批量**解析本页所有人，不能放进下面的循环里查
    covers = personCoversOf(rows)
    items = []
    for row in rows:
        code = str(row.get("personCode") or "")
        one = stats.get(code) or {}
        items.append(personSummary(row, one.get("photoCount", 0),
                                   one.get("faceCount", 0),
                                   one.get("confirmedFaceCount", 0),
                                   one.get("yearLow"), one.get("yearHigh"),
                                   coverFaceCode=covers.get(code)))
    # 页内再排一次（见上面关于「页内有序」的说明）：SQL 侧用的是**同口径**的
    # 标量子查询，这里用批量聚合的结果 —— 两处必须同键同向，否则顺序会跳。
    if orderBy in ("photoCount", "faceCount"):
        field = orderBy
        items.sort(key=lambda it, _f=field: (
            (-int(it[_f]) if int(desc) else int(it[_f])), it["displayName"]))
    return dto.pageBody(items, p, s, total)


@router.get("/persons/{personCode}", summary="人物详情（含各年代档分组统计）")
def getPerson(personCode: str):
    """单个人物的完整档案。

    `buckets` 是 UI 设计 P-05 那条「按年代档分组的人脸样本」时间轴的数据源：
      [{bucketKey, photoCount, faceCount, confirmedCount, autoCount,
        centroidSampleCount, centroidEnabled}]
    - `centroidSampleCount < MIN_CENTROID_SAMPLES(3)` -> `centroidEnabled=False`，
      UI 就该显示「样本不足，该年代档不参与匹配」而不是画一条假的趋势线。
    - `autoCount` 高于 `confirmedCount` 是「识别质量偏低」的信号，
      UI 据此提示用户去纠错（设计稿 §4.5 的质心健康度提示）。
    """
    row = personRow(personCode, withDeleted=True)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    code = str(row.get("personCode"))
    stats = personStatsOf([code]).get(code, {})
    # 头像与列表页同口径（DR-40）：默认没设过时回退到代表脸，
    # 否则详情头部会显示「首字母」而列表卡片显示「照片」，同一页自相矛盾。
    out = personSummary(row, stats.get("photoCount", 0), stats.get("faceCount", 0),
                        stats.get("confirmedFaceCount", 0),
                        stats.get("yearLow"), stats.get("yearHigh"),
                        coverFaceCode=personCoversOf([row]).get(code))

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
    # 年代档内**照片数**必须按 shotBucket 聚合，不能按 takenAt 的年月
    # ⚠️ 步骤 12 修掉的实现缺口：原来这里group by的是
    #   `substr(p.takenAt,1,7)`（得到 "2013-05" 这样的键），
    #   下面却拿年代档键（"1998-2007"）去查同一个字典 —— **键的形状根本不同，
    #   于是每个年代档的 photoCount 恒为 0**，人物时间轴上的「N 张」永远是 0。
    #   这类「不报错、只是恒为 0」的缺陷最难查：接口 200、结构齐全、字段在。
    # 现在直接从 pb_face 出发按 shotBucket 统计 DISTINCT photoCode，
    # 且必须 JOIN pb_photo 过滤 delFlag（软删照片里的脸不该算进「他出现在多少张照片里」）。
    photosOfBucket = {}
    for one in query.selectList(
            "SELECT f.shotBucket AS bucketKey, COUNT(DISTINCT p.photoCode) AS cnt"
            " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
            " WHERE f.personCode = %s AND p.delFlag = %s GROUP BY f.shotBucket",
            (str(row.get("personCode")), comGD.DEL_FLAG_NO)):
        photosOfBucket[str(one.get("bucketKey") or "") or "(无拍摄年份)"] = \
            int(one.get("cnt") or 0)

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
# 四之二、P-05 的两个子资源（步骤 12）
# ============================================================

def faceStateOf(row: dict) -> str:
    """pb_face 行 -> 四态（DR-16①）。**全api 层唯一一份推导**。

    为什么抽出来
    ------------
      原来这段 if/elif 写在 getPhoto 里（给 P-03 画人脸框用），
      步骤 12 又要在 P-05 的「人脸样本」里按同一套四态分两段 ——
      两处各写一遍的后果不是冗余，而是**漂移**：照片详情页说「机器认的」、
      人物详情页说「已确认」，用户会以为是两张不同的脸。
    ⚠️ 顺序有讲究：**stranger 优先于一切**，其次「有主人但没确认」（disputed），
      最后才按 isConfirmed 分confirmed / pending —— 与前端 utils/faceState.js
      的 faceStateOf() 逐条对齐（改一处必须改另一处）。
    """
    if int((row or {}).get("isStranger") or 0):
        return "stranger"
    person = str((row or {}).get("personCode") or "")
    if person and int((row or {}).get("isConfirmed") or 0) != 1:
        return "disputed"
    return "confirmed" if person else "pending"


def _faceSummary(row: dict, similarity=None) -> dict:
    """pb_face 行 -> 人物详情 Tab2 的一张样本。"""
    code = str(row.get("faceCode") or "")
    state = faceStateOf(row)
    return {
        "faceCode": code,
        "photoCode": row.get("photoCode") or None,
        "thumbUrl": "/api/face/%s" % code,
        "shotBucket": row.get("shotBucket") or None,
        "state": state,
        "isConfirmed": int(row.get("isConfirmed") or 0),
        "personCode": row.get("personCode") or None,
        "quality": _floatOrNone(row.get("quality")),
        "detScore": _floatOrNone(row.get("detScore")),
        "similarity": similarity,
        "regYMDHMS": row.get("regYMDHMS") or None,
    }


def _similarityToPerson(row: dict, cache: dict):
    """这张脸与「它自己所在那个年代档的质心」的余弦相似度。

    ⚠️ **只作参考，不是判定依据**：它比的是同一个年代档的质心，而实际归属是
       在「相邻三年代档 ∪ ALL」里取 max ——所以这里的数天然偏低，
       界面上不能拿它当「为什么认成他」的答案（那要看操作历史）。
    为什么仍然要给：设计稿的 Tab2 每张样本下面标了相似度，
    没有这一列用户会以为「人工确认的和自动归属的只是画法不同」。
    算不出来（没有质心 / 没有向量 / 解码失败）一律 None，**绝不填 0.00** ——
    0.00 会被读成「非常不像」，而真相是「没法比」。
    """
    try:
        import numpy as np
        from engine.face import engine as faceEngine
    except ImportError:                                  # pragma: no cover
        return None
    key = (str(row.get("personCode") or ""), str(row.get("shotBucket") or ""))
    if key not in cache:
        from engine.match import centroid as centroid
        try:
            cache[key] = centroid.centroidOf(key[0], key[1])
        except Exception:                                # noqa: BLE001 读侧降级
            cache[key] = None
    center = cache.get(key)
    blob = row.get("embedding")
    if center is None or blob is None:
        return None
    try:
        vector = faceEngine.decodeEmbedding(blob)
        return round(float(np.dot(center, vector)), 4)
    except (TypeError, ValueError):
        return None


@router.get("/persons/{personCode}/faces",
            summary="某人的全部人脸样本（Tab2：人工确认 / 自动归属两段）")
def listPersonFaces(personCode: str,
                    state: str = Query(default=None,
                                       description="confirmed / disputed / pending / stranger；"
                                                   "不给则四态全给"),
                    bucketKey: str = Query(default=None, description="只看某个年代档"),
                    limit: int = Query(default=400, ge=1, le=2000)):
    """P-05 Tab2 的数据源。**只读**（移除/改判走 /api/review/fix 或 /api/review/split）。

    为什么两段要**分两段**而不是混着给
    ----------------------------------
      「人工确认」= 用户逐张核对过、可信；「自动归属」= 机器猜的、**可否决**。
      混在一列里用户无法判断哪些值得复核 —— 而那正是纠错入口（DR-16②）。
      响应里除了items，还给 `counts`（两段各自的真实条数），
      界面上的「人工确认 N / 自动 M」必须取这里的数，不能取本页 items 的长度
      （有 limit 时两个数会不一样）。

    ⚠️ limit 默认 400：一个人的脸在10 万张库里可能上千，
    一次全给会让「按年代档分组」的分段渲染失去意义。真要更多就分页翻。
    """
    row = personRow(personCode, withDeleted=True)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    if state and state not in ("confirmed", "disputed", "pending", "stranger"):
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "state 只支持 confirmed/disputed/pending/stranger，收到 %r" % state)

    where = ["f.personCode = %s", "f.delFlag = %s", "p.delFlag = %s"]
    values = [str(row.get("personCode")), comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO]
    if bucketKey:
        where.append("f.shotBucket = %s")
        values.append(str(bucketKey))
    cond = " AND ".join(where)

    counts = {"confirmed": 0, "disputed": 0, "pending": 0, "stranger": 0}
    for one in query.selectList(
            "SELECT f.isConfirmed AS isConfirmed, f.isStranger AS isStranger,"
            " COUNT(*) AS cnt FROM pb_face f JOIN pb_photo p"
            " ON p.photoCode = f.photoCode WHERE %s GROUP BY"
            " f.isConfirmed, f.isStranger" % cond, tuple(values)):
        state2 = faceStateOf({"personCode": str(row.get("personCode")),
                              "isConfirmed": one.get("isConfirmed"),
                              "isStranger": one.get("isStranger")})
        counts[state2] = counts.get(state2, 0) + int(one.get("cnt") or 0)

    # ⚠️ personCode **必须在 SELECT 里**：四态是靠它 + isConfirmed + isStranger
    #    推导的，少这一列的话每张脸都会被判成 pending —— 而 counts 那条查询
    #    是 GROUP BY 取的，照样正确，于是「计数对得上、列表全错」，
    #    是最难被发现的那种不一致。
    rows = query.selectList(
        "SELECT f.faceCode AS faceCode, f.photoCode AS photoCode,"
        " f.personCode AS personCode,"
        " f.shotBucket AS shotBucket, f.isConfirmed AS isConfirmed,"
        " f.isStranger AS isStranger, f.quality AS quality, f.detScore AS detScore,"
        " f.embedding AS embedding, f.regYMDHMS AS regYMDHMS,"
        " p.shotYear AS shotYear, p.shotYearOverride AS shotYearOverride,"
        " p.rotateDeg AS rotateDeg,"
        " p.takenAt AS takenAt, p.relPath AS relPath"
        " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
        " WHERE %s ORDER BY f.shotBucket ASC, f.regYMDHMS ASC LIMIT %%s" % cond,
        tuple(values) + (int(limit),))

    cache = {}
    items = []
    for one in rows:
        item = _faceSummary(one, _similarityToPerson(one, cache))
        # DR-42：样本上的年份 = **有效拍摄年**，与它所在的年代档同源。
        # 用裸 shotYear 的话，修正过年代的照片在「人脸样本」里显示 2019，
        # 而它的年代档是 1956-1965 —— 同一个条目自相矛盾。
        item["shotYear"] = effectiveYearOf(one)
        item["takenAt"] = one.get("takenAt") or None
        item["relPath"] = str(one.get("relPath") or "")
        item.pop("embedding", None)
        if state and item["state"] != state:
            continue
        items.append(item)
    return {"ok": True, "personCode": str(row.get("personCode")),
            "displayName": str(row.get("displayName") or ""),
            "counts": counts, "limit": int(limit),
            "truncated": (counts["confirmed"] + counts["disputed"]
                          + counts["pending"] + counts["stranger"]) > len(rows),
            "items": items}


@router.get("/persons/{personCode}/timeline",
            summary="某人的照片按年代档分组（人物时间轴 Tab1）")
def getPersonTimeline(personCode: str,
                      limitPerBucket: int = Query(default=12, ge=1, le=60)):
    """P-05 Tab1 的数据源，年代档键**就是 pb_face.shotBucket**（S0 结论：划分年代档是刚需）。

    ⚠️ 为什么每个年代档只给 N 张而不是全给：一个人可能有 500 张照片，
      一次塞进响应会让「按年代浏览」这个动作失去意义（用户想看的是分布，不是清单）。
      每个年代档给 `limitPerBucket` 张 + `photoTotal`，界面用 BucketTimeline 的
      「+N」折叠其余（该组件本来就为此设计）。

    ⚠️ **空年代档不出现**：没有照片的年代档直接不返回，而不是返回 photoTotal=0 ——
      后者渲染出来是一条永远 0 张的横带，用户会以为加载失败。
    """
    row = personRow(personCode, withDeleted=True)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    code = str(row.get("personCode"))

    totals = {}
    for one in query.selectList(
            "SELECT f.shotBucket AS bucketKey, COUNT(DISTINCT f.photoCode) AS cnt"
            " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
            " WHERE f.personCode = %s AND f.delFlag = %s AND p.delFlag = %s"
            " GROUP BY f.shotBucket", (code, comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO)):
        key = str(one.get("bucketKey") or "") or "(无拍摄年份)"
        totals[key] = int(one.get("cnt") or 0)

    photosByBucket = {}
    for one in query.selectList(
            "SELECT f.shotBucket AS bucketKey, p.photoCode AS photoCode,"
            " p.relPath AS relPath, p.takenAt AS takenAt,"
            " p.shotYear AS shotYear, p.shotYearOverride AS shotYearOverride,"
            " p.rotateDeg AS rotateDeg,"
            " p.faceCount AS faceCount, p.placeName AS placeName,"
            " p.cameraModel AS cameraModel, p.width AS width, p.height AS height,"
            " p.fileSize AS fileSize, p.mimeType AS mimeType,"
            " p.isDuplicate AS isDuplicate, p.isMissing AS isMissing,"
            " p.scanState AS scanState, p.lat AS lat, p.lon AS lon"
            " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
            " WHERE f.personCode = %s AND f.delFlag = %s AND p.delFlag = %s"
            " ORDER BY f.shotBucket ASC, p.takenAt ASC",
            (code, comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO)):
        key = str(one.get("bucketKey") or "") or "(无拍摄年份)"
        bucket = photosByBucket.setdefault(key, [])
        # ⚠️ 一张照片里同一个人可能有多张脸 -> 同一个 photoCode 会重复出现，
        #    必须按 photoCode 去重，否则「N 张」会被数成「N 张脸」
        if any(seen["photoCode"] == one.get("photoCode") for seen in bucket):
            continue
        bucket.append(photoSummary(one))

    groups = []
    for key in sorted(totals, key=lambda k: (0 if k[:1].isdigit() else 1, k)):
        photos = photosByBucket.get(key, [])
        if not photos:
            continue                                  # 空年代档不显示
        groups.append({"bucketKey": key, "count": totals.get(key, len(photos)),
                       "photoTotal": totals.get(key, len(photos)),
                       "photos": photos[:int(limitPerBucket)]})
    return {"ok": True, "personCode": code,
            "displayName": str(row.get("displayName") or ""),
            "limitPerBucket": int(limitPerBucket), "groups": groups}


@router.get("/persons/{personCode}/places",
            summary="这个人去过的地方（P-05 Tab1「时间轴」下方的区块）")
def getPersonPlaces(personCode: str,
                    size: int = Query(default=200, ge=1, le=dto.MAX_PAGE_SIZE)):
    """P-05 Tab1 的**第二块**：该人照片按地点聚合（**实时 join**，不落库）。

    与 `/persons/{code}/timeline`、`/persons/{code}/faces` 同一模块、同一风格 ——
    它们是一组兄弟端点，拆到别的模块只会让「人物相关的只读视图」散成两处。

    三条口径（每一条写错都会给出一个**看起来合理**的错答案）
    --------------------------------------------------------
    ① **聚合键 = `COALESCE(NULLIF(p.placeNameDir,''), p.placeName)`**（DR-32）。
       直接按 `p.placeName` 分组的话，598 张「目录名带地点」的照片
       （`placeName` 是空的）**全部被丢进 NULL 组**，于是「去过的地方」里
       只剩那 65 张 GPS 照片 —— 而接口 200、列表结构完好。
    ② **`nameZh` 按聚合键从 `pb_place` 取，不按 `placeCode` 取**（DR-36）。
       目录名地点的聚合键与 `placeCode` **不是一对一**（前者可能被重名归并，
       后者还可能是改名后归零的旧行）—— 用 placeCode 关联会静默取到
       **另一个地点**的中文名。
       这里走 `placeStore.nameZhMap()`（进程内快照，键就是聚合键），
       而不是 SQL JOIN：`pb_place` 的 `placeName` 可以**有重复行**
       （手工建的 + 聚合出来的），JOIN 会让同一个地点在列表里出现两次。
    ③ **两个计数必须都返回**：`photoTotal`（该人照片总数）与
       `locatedPhotoTotal`（其中有地点信息的）。界面上「没数据」有两种：
       「他本来就没几张照片」与「照片里没写地点」—— 不带这两个数，
       用户分不清「功能没做」与「地点线索没覆盖到」。
       ⚠️ 实测（R5 只读探查）：全库 `pb_photo_person` 206 行里，
       照片**带地点**的只有个位数 ⇒ 绝大多数人这个区块是空的。
       **这是数据现状，不是 bug**；有内容的前提是先在待确认队列里
       给「有地点的照片」确认人脸。

    排序：`lastShotYear` **倒序**（最近去过的在前）。SQLite 里 NULL 最小，
    所以没有年份的地点自然排在最后 —— 正是想要的，不用额外造哨兵值。
    """
    row = personRow(personCode, withDeleted=True)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    code = str(row.get("personCode"))
    key = placeStore.placeKeySql("pp")

    cond = ("j.personCode = %s AND j.delFlag = %s AND pp.delFlag = %s")
    args = [code, comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO]

    photoTotal = int(query.selectValue(
        "SELECT COUNT(DISTINCT pp.photoCode) AS rowNum FROM pb_photo_person j"
        " JOIN pb_photo pp ON pp.photoCode = j.photoCode WHERE " + cond,
        tuple(args), default=0) or 0)
    locatedPhotoTotal = int(query.selectValue(
        "SELECT COUNT(DISTINCT pp.photoCode) AS rowNum FROM pb_photo_person j"
        " JOIN pb_photo pp ON pp.photoCode = j.photoCode WHERE " + cond +
        " AND " + key + " IS NOT NULL AND " + key + " <> ''",
        tuple(args), default=0) or 0)

    nameMap = placeStore.nameZhMap()
    # DR-42：这里的年代跨度必须与年代档同源（人工修正优先）。
    # 本查询里 pb_photo 的别名是 `pp`（不是 browse 其余处的 `p`）。
    effectiveYear = comGD.sqlEffectiveShotYear("pp")
    items = []
    for one in query.selectList(
            # ⚠️ `GROUP BY` / `ORDER BY` 里写的是**表达式本身**，不是 `AS placeKey`
            #    这个别名：`queryCommon.checkColumns` 的列名白名单会拦下不在
            #    schema 里的裸标识符（placeStore.liveAggregatePlaces 处有实测记录）。
            "SELECT " + key + " AS placeKey,"
            " COUNT(DISTINCT pp.photoCode) AS photoCount,"
            " MIN(" + effectiveYear + ") AS firstShotYear,"
            " MAX(" + effectiveYear + ") AS lastShotYear"
            " FROM pb_photo_person j"
            " JOIN pb_photo pp ON pp.photoCode = j.photoCode"
            " WHERE " + cond + " AND " + key + " IS NOT NULL AND " + key + " <> ''"
            " GROUP BY " + key +
            " ORDER BY MAX(" + effectiveYear + ") DESC, " + key + " ASC LIMIT %s",
            tuple(args) + (int(size),)):
        placeKey = str(one.get("placeKey") or "")
        nameZh = str(nameMap.get(placeKey) or "") or None
        items.append({
            # ⚠️ 与字典路径同一条派生规则（`makePlaceCode`），所以这里给出的
            #    `placeCode` 与 `/api/places` 里的**是同一个值** ——
            #    前端可以放心拿它去跳地点详情。
            "placeCode": placeStore.makePlaceCode(placeKey),
            "placeName": placeKey,
            "nameZh": nameZh,
            "photoCount": int(one.get("photoCount") or 0),
            "firstShotYear": _intOrNone(one.get("firstShotYear")),
            "lastShotYear": _intOrNone(one.get("lastShotYear")),
        })
    return {"ok": True, "personCode": code,
            "displayName": str(row.get("displayName") or ""),
            "photoTotal": photoTotal,
            "locatedPhotoTotal": locatedPhotoTotal,
            "places": items}


@router.get("/duplicates", summary="重复照片分组（按内容指纹聚合）")
def listDuplicateGroups(page: int = Query(default=1, ge=1),
                        size: int = Query(default=dto.DEFAULT_PAGE_SIZE),
                        photoCode: str = Query(default=None,
                                               description="给了就只返回这张照片所属的那一组")):
    """重复照片（步骤 3 判定为 `isDuplicate=1`）按 `dupOfPhotoCode` 聚成组。

    两种「重复」在库里长得一样，必须靠标记区分（DR-11）
    --------------------------------------------------
      · **复制**：内容相同，但同内容的旧文件**还在**磁盘上 ->
        只标isDuplicate，不建移动链接（用户真的存了两份）
      · **移动/重命名**：同内容的旧文件**已经不在**磁盘上 ->
        新行 isDuplicate=1 + 旧行 movedToPhotoCode（**双向可查**）
      `kind` 字段就是给界面用的：移动提示「这其实是被改过名的同一张」，
      复制只提示「这份内容库里有另一份」。混着说会让用户去删错文件。

    ⚠️ 本端点**只读**。删除/取消重复标记走
       `/api/photos/{code}/mark-duplicate` 与 `/unmark-duplicate`（步骤 11 已做），
       而且这两个都只改库、**不删任何文件**（原图零风险）。
    """
    p, s = dto.clampPage(page, size)
    at = dto.offsetOf(p, s)

    if photoCode:
        one = photoRow(photoCode)
        if not one:
            raise dto.ApiError(dto.CODE_NOT_FOUND, "photoCode=%s 不存在" % photoCode)
        target = str(one.get("dupOfPhotoCode") or "") or str(photoCode)
        total = int(query.selectValue(
            "SELECT COUNT(*) AS rowNum FROM pb_photo"
            " WHERE delFlag = %s AND (dupOfPhotoCode = %s OR photoCode = %s)",
            (comGD.DEL_FLAG_NO, target, target)) or 0)
        rows = query.selectList(
            "SELECT * FROM pb_photo WHERE delFlag = %s"
            " AND (dupOfPhotoCode = %s OR photoCode = %s)"
            " ORDER BY recID ASC LIMIT %s OFFSET %s",
            (comGD.DEL_FLAG_NO, target, target, s, at))
    else:
        total = int(query.selectValue(
            "SELECT COUNT(*) AS rowNum FROM pb_photo"
            " WHERE delFlag = %s AND dupOfPhotoCode IS NOT NULL AND dupOfPhotoCode <> ''"
            " AND isMissing = 0", (comGD.DEL_FLAG_NO,)) or 0)
        rows = query.selectList(
            "SELECT * FROM pb_photo WHERE delFlag = %s"
            " AND dupOfPhotoCode IS NOT NULL AND dupOfPhotoCode <> '' AND isMissing = 0"
            " ORDER BY dupOfPhotoCode ASC, recID ASC LIMIT %s OFFSET %s",
            (comGD.DEL_FLAG_NO, s, at))

    groups = {}
    for one in rows:
        target = str(one.get("dupOfPhotoCode") or "") or str(one.get("photoCode") or "")
        item = groups.setdefault(target, {"dupOfPhotoCode": target, "kind": "copy",
                                          "photos": []})
        # 组内有movedToPhotoCode 指向组内另一行 => 这一组是「移动/重命名」而不是复制
        if str(one.get("movedToPhotoCode") or ""):
            item["kind"] = "moved"
        summary = photoSummary(one)
        summary["movedToPhotoCode"] = one.get("movedToPhotoCode") or None
        summary["dupOfPhotoCode"] = one.get("dupOfPhotoCode") or None
        item["photos"].append(summary)

    #⚠️ **被指向的那一行（dupOfPhotoCode 指向它）自己也必须进组**
    #   它自己的 dupOfPhotoCode 是 NULL，所以上面那条查询捞不到它。
    #   少了这一步，界面上「重复组」只有一张照片、连对比都做不了 ——
    #   而「对比」正是这个端点存在的理由。
    for target in list(groups.keys()):
        item = groups[target]
        if any(one.get("photoCode") == target for one in item["photos"]):
            continue
        origin = photoRow(target)
        if not origin:
            continue# 原件已被软删/缺失 -> 组里只有副本，如实呈现
        summary = photoSummary(origin)
        summary["dupOfPhotoCode"] = origin.get("dupOfPhotoCode") or None
        summary["movedToPhotoCode"] = origin.get("movedToPhotoCode") or None
        item["photos"].insert(0, summary)          # 原件排第一：它才是「本体」
    return dto.pageBody(list(groups.values()), p, s, total)


@router.get("/duplicates/compare",
            summary="两张重复照片的并排对比（同内容 / 不同内容 / 人脸是否一致）")
def compareDuplicates(photoCode: str, otherCode: str):
    """重复照片**对比接口**（P-02 复���照片入口的后端）。

    返回三块，缺一不可
    ------------------
      · `sameContent`：fileHash 是否逐字节相同（**判定的唯一依据**，
        不看尺寸/文件名 —— 改过名或重压过的图尺寸会变但内容一样）
      · `diff`：拍摄时间/尺寸/机型/地点的逐项差异，UI 只把不同的项高亮
      · `faces`：两边的脸按「同一个 personCode + 年代档」配对，
        用来回答「这两张里的同一个人是不是被认成了两个人」——
        那正是重复照片会**放大**错分的原因（同一张脸在两条记录里各归属一次）

    ⚠️ 任一 photoCode 不存在/已软删都 404：宁可报错也不要返回半份对比，
      「一半有数据一半空」的对比框比报错更容易让人误判。
    """
    left = photoRow(photoCode)
    right = photoRow(otherCode)
    if not left:
        raise dto.ApiError(dto.CODE_NOT_FOUND, "photoCode=%s 不存在或已软删" % photoCode)
    if not right:
        raise dto.ApiError(dto.CODE_NOT_FOUND, "otherCode=%s 不存在或已软删" % otherCode)

    def side(row: dict) -> dict:
        out = photoSummary(row)
        out["fileHash"] = str(row.get("fileHash") or "")
        out["fileSize"] = int(row.get("fileSize") or 0)
        out["orientation"] = row.get("orientation")
        out["isDuplicate"] = int(row.get("isDuplicate") or 0)
        out["dupOfPhotoCode"] = row.get("dupOfPhotoCode") or None
        out["movedToPhotoCode"] = row.get("movedToPhotoCode") or None
        out["lat"] = row.get("lat")
        out["lon"] = row.get("lon")
        return out

    leftSide, rightSide = side(left), side(right)
    hashLeft = str(left.get("fileHash") or "")
    hashRight = str(right.get("fileHash") or "")

    fields = (("takenAt", "拍摄时间"), ("width", "宽"), ("height", "高"),
              ("cameraModel", "机型"), ("placeName", "地点"), ("fileSize", "文件大小"))
    diff = [{"field": key, "label": label,
             "left": left.get(key), "right": right.get(key),
             "same": (left.get(key) == right.get(key))}
            for key, label in fields]

    def facesOf(code: str) -> list:
        out = []
        for one in sqliteCommon.query_pb_face("pb_face", photoCode=str(code),
                                              mode="light", orderBy="recID"):
            out.append({"faceCode": str(one.get("faceCode") or ""),
                        "thumbUrl": "/api/face/%s" % one.get("faceCode"),
                        "personCode": one.get("personCode") or None,
                        "shotBucket": one.get("shotBucket") or None,
                        "state": faceStateOf(one)})
        return out

    leftFaces, rightFaces = facesOf(str(photoCode)), facesOf(str(otherCode))
    # 按 personCode 配对（None 归None —— 两边都未归属也算「一致」）
    byPerson = {}
    for one in leftFaces:
        byPerson.setdefault(one["personCode"], []).append(one)
    paired = []
    for one in rightFaces:
        candidates = byPerson.get(one["personCode"]) or []
        mate = candidates.pop(0) if candidates else None
        paired.append({"right": one, "left": mate,
                       "consistent": mate is not None
                       and mate["state"] == one["state"]})
    unpairedLeft = [one for lst in byPerson.values() for one in lst]
    return {"ok": True, "sameContent": bool(hashLeft) and hashLeft == hashRight,
            "left": leftSide, "right": rightSide, "diff": diff,
            "faces": {"left": leftFaces, "right": rightFaces, "paired": paired,
                      "unpairedLeft": unpairedLeft}}


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
    # ⚠️ DR-43：`orientation` 必须**同时**在顶层给一份。
    #    前端的 `displaySize(photo, rotateDeg)` 读的是 `photo.orientation`
    #    （顶层），它要按 ①EXIF 方向 ②人工旋转 依次折算**显示方向**的宽高比；
    #    只放在 `exif.orientation` 里的话，orientation=6 的照片主图容器会按
    #    未纠正的横图比例撑开、而浏览器显示的是竖图 —— 人脸框整体错位，
    #    且这个错**只在带 EXIF 方向的照片上**出现（绝大多数照片看不出问题）。
    #    与 `/duplicates/compare` 的 side() 同一个做法（那里早就这么给了）。
    out["orientation"] = row.get("orientation")
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
                  # ⚠️ placeName **保留英文原值**，不要用 placeZh 覆盖它 ——
                  #    详情页的「地点」栏读 placeZh（显示中文），
                  #    而 placeName 是排障依据（「这个中文名是从哪个英文键算出来的」）。
                  #    前端契约：显示名 = placeZh ?? placeName。
                  "placeName": row.get("placeName") or None,
                  "placeZh": out.get("placeZh") or None}
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
        # ⚠️ 四态推导**只有一份**（faceStateOf），别在这里再写一遍 if/elif ——
        #    步骤 12 加了P-05 的人物样本页，两处各写一份必然漂移
        state = faceStateOf(face)
        if state == "pending":
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
# 六、GET /api/places*（R5 起**整个地点命名空间搬到了 `api/place.py`**）
# ============================================================
# ⚠️ 为什么搬走（不是"顺手重构"，是两处硬问题）
# --------------------------------------------
#   ① **两条同路径路由 = 静默遮蔽**：FastAPI 按注册顺序匹配，后注册的
#      `GET /api/places` 永远不会被调用，而 `/openapi.json` 里两条都「在」。
#      本项目的 `api/face.py` 文件头记录过同类事故（`/api/face/jobs`
#      被 `/api/face/{faceCode}` 吃掉）。
#   ② `GET /api/places/{placeCode}` 是**参数段通配**，会把同级的静态段
#      `/api/places/rebuild` 一起吃掉 —— 于是「方法写错」的 405 会退化成
#      「这个地点不存在」的 404（见 test_api_smoke 的 405 用例）。
#      通配段与静态段的先后必须在**同一个 router 内部**才管得住。
#   ⇒ `GET /api/places`、`POST /api/places/rebuild`、`_placeSummary` 全部
#     原样迁入 `api/place.py`（行为逐字未改，`test_api_places.py` 是它的
#     回归网）。本模块**不再注册任何 `/places*` 路由**。
#
# 本模块仍然提供全 api 层的只读行模型（`photoRow` / `photoSummary` /
# `personRow` / `personSummary` / `faceStateOf`），`api/place.py` 复用
# `photoSummary()`，所以「同一张照片在两个接口里的形状」只有一份定义。


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


def batchProgressOf(job: dict) -> dict:
    """**本批真实计数**。库里没有 per-batch 计数器，只能从 batchIndex + processedCount 推导。

    为什么推导规则必须写死在一个函数里
    ----------------------------------
      pb_scan_job 只存 `batchIndex`（已完成批数）与 `processedCount`（累计已处理），
      **没有**「本批已处理多少」这一列。而 P-07 的核心语义就是「本批 62/100、
      已暂停等待指示」—— 少了这个数，界面就只能给一个总进度，
      而总进度在 10 万张的库上永远是 0.06%。

      推导规则（三处都要用同一个，所以只写一遍）：
        · RUNNING      -> 正在跑的这一批 = processedCount - batchIndex × batchSize
        · PAUSED/IDLE  -> 刚跑完的那一批 = processedCount - (batchIndex-1) × batchSize
        · FAILED       -> 同PAUSED（可能只跑了一半，那正是要看清的数字）
        · DONE         -> 没有「当前批次」了，batchProcessed 给 null（界面显示「—」）

    ⚠️ 别写成 `processedCount % batchSize`：那是数学上成立、语义上错的 ——
      最后一批不满时余数是对的，但 PAUSED 状态要用「刚跑完那批」的数，
      正好等于余数；而中间批永远正好取模得 0，看起来「没在动」。
      而 DONE 时余数是一个没有意义的残留值。两种口径会在同一页上互相打架。
    """
    size = int((job or {}).get("batchSize") or 0)
    index = int((job or {}).get("batchIndex") or 0)
    processed = int((job or {}).get("processedCount") or 0)
    status = str((job or {}).get("jobStatus") or comGD.JOB_IDLE)
    if size <= 0:
        return {"batchSize": 0, "batchIndex": index, "batchProcessed": None}
    if status == comGD.JOB_DONE:
        done = None
    elif status == comGD.JOB_RUNNING:
        done = max(0, min(size, processed - index * size))
    else:
        done = max(0, min(size, processed - max(0, index - 1) * size)) if index > 0 else 0
    return {"batchSize": size, "batchIndex": index, "batchProcessed": done}


def _jobBrief(row: dict) -> dict:
    """pb_scan_job 行 -> 概览页 / 扫描台要的一行摘要。"""
    if not row:
        return {}
    out = {"jobCode": str(row.get("jobCode") or ""),
           "jobStatus": str(row.get("jobStatus") or comGD.JOB_IDLE),
           "jobStatusText": comGD.JOB_STATUS_TEXT.get(str(row.get("jobStatus") or ""), ""),
           # 任务类型：扫描与人脸识别共用 pb_scan_job，前端据此分栏显示。
           # 缺省 0 = 照片扫描 —— 老的扫描任务行没有这一列时也走得通。
           "jobType": int(row.get("jobType") or comGD.JOB_TYPE_SCAN),
           "jobTypeText": comGD.jobTypeText(row.get("jobType")),
           "processedCount": int(row.get("processedCount") or 0),
           "totalCount": int(row.get("totalCount") or 0),
           "addedCount": int(row.get("addedCount") or 0),
           "rootPath": str(row.get("rootPath") or ""),
           "lastCursor": row.get("lastCursor") or None,
           "startedYMDHMS": row.get("startedYMDHMS") or None,
           "finishedYMDHMS": row.get("finishedYMDHMS") or None}
    out.update(batchProgressOf(row))
    return out


# ============================================================
# 八、年代档工具（供人物详情与前端渲染共用）
# ============================================================

def bucketSpan(bucketKey: str) -> tuple:
    """年代档键 -> (起始年, 结束年)。解析不出返回 (0, 0)。

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
