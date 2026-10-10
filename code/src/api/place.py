#! /usr/bin/env python3
#encoding: utf-8

#Filename: place.py
#Description: 地点浏览接口（步骤 R5）—— /api/places* 整个命名空间
#
# 六个端点（**纯读 5 个 + 一个显式 405 占位**）
# --------------------------------------------
#   GET  /api/places                       地点列表（字典；groupByNameZh=1 按中文名归并）
#   POST /api/places/rebuild               重建地点字典（幂等，从 browse.py 原样搬来）
#   GET  /api/places/rebuild               **只为保住 405**，见下面「为什么单列这一条」
#   GET  /api/places/{placeCode}           地点详情（含同组下钻 placeCodes / members）
#   GET  /api/places/{placeCode}/photos    该地点的照片（分页 + 全年份计数）
#   GET  /api/places/{placeCode}/persons   出现在该地点的人（**实时 join**，DR-26/28）
#
# 为什么 `/api/places*` 整个命名空间要收在一个模块里
# ------------------------------------------------
#   ① **两条同路径路由 = 静默遮蔽**：FastAPI 按注册顺序匹配，后注册的那条
#      永远不会被调用，而 `/openapi.json` 里两条都「在」。本项目的
#      `api/face.py` 文件头已经记录过同类事故（`/api/face/jobs` 被
#      `/api/face/{faceCode}` 吃掉）。所以这里不新增第二条 `/api/places`，
#      而是把列表端点**搬进本模块**（原 `browse.py` 的 `listPlaces`）。
#   ② `{placeCode}` 是**参数段通配**，它会吃掉同级的静态段：
#      `GET /api/places/rebuild` 本该由 Starlette 的路由器回 **405**
#      （`POST` 才是它的方法，见 `test_api_smoke.test_methodNotAllowedIs405…`），
#      一旦 `{placeCode}` 先匹配上就会变成 404「这个地点不存在」——
#      一个**看起来完全合理**的错（调用方会去查地点名，而真相是方法写错了）。
#      所以下面显式声明一条 `GET /api/places/rebuild`，抛 405 + `Allow: POST`，
#      保留原来的语义。它**不进 OpenAPI**（`include_in_schema=False`），
#      免得 `/docs` 里出现一个「看起来可用」的 GET。
#
# 三条纪律（与 browse.py 同款）
# ----------------------------
#   ① **不裸 SQL**：一律经 `database.queryCommon`（只读出口）与
#      `database.auto_generated.sqliteCommon`（等值查询/写入）；
#   ② **不写库**（除 `POST /places/rebuild` 那条本来就写派生缓存的）；
#   ③ **不碰 photoDir**：只把 photoCode 带出去，取图由 `api/static.py` 负责。
#
# ⚠️ 地点口径一个字都不改（R5 硬约束）
# ------------------------------------
#   `placeCode` / 聚合键 / `nameZh` 全部沿用 `processor.place.placeStore` 的定义：
#     · 聚合键 = `COALESCE(NULLIF(placeNameDir,''), placeName)`（`placeKeySql()`）
#     · `pb_place.placeName` **就是**聚合键（`rebuildPlaces()` 落的）
#     · `placeCode` 只由 `makePlaceCode()` 派生
#   本模块只做「读 + 展示层归并」，不新增列、不改派生逻辑。
#
# ⚠️ 「重名归并」是**展示分组**，不是数据合并（DR-36 方案①）
# --------------------------------------------------------
#   3 组重名（各 2 行）在列表里并成 1 行，但 `placeCode` 一个都不改：
#   归并项带着 `placeCodes` / `members` 下钻，详情里能看出
#   「这 15 张里哪 8 张来自大屯、哪 7 张来自望京」。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import browse                                            # noqa: E402
from api import dto                                               # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from database import queryCommon as query                         # noqa: E402
from processor.place import placeFinalize as placeFinalize        # noqa: E402
from processor.place import placeStore as placeStore              # noqa: E402

from fastapi import APIRouter, HTTPException, Query               # noqa: E402

_VERSION = "20261008"

_LOG = misc.setLogNew("apiPlace", "apiplace.log")

router = APIRouter(tags=["place"])

#: 列表里**默认不展示**的行：`photoCount = 0`（DR-33 的幽灵行）。
#: 只在 `groupByNameZh=1`（展示层路径）上生效 —— 未归并的字典路径保持
#: 步骤 9 以来的行为（会把 0 张的行也给出来），否则 `test_api_places` 里
#: 「归零但留行」那条断言的语义会被本步悄悄改掉。
SKIP_ZERO_ON_GROUP = True

#: 详情/子资源都能接受的「已归并同组」参数名
_PLACE_CODES_DESC = "已归并的同组地点（逗号分隔的 placeCode）；不给则自动取同 nameZh 的行"


# ============================================================
# 一、行读取与整形
# ============================================================

def _intOrNone(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _floatOrNone(value, digits: int = 6):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _splitCodes(text) -> list:
    """`"a,b,c"` -> `["a", "b", "c"]`（去空、去重、保序）。"""
    codes = [one.strip() for one in str(text or "").split(",")]
    out = []
    for one in codes:
        if one and one not in out:
            out.append(one)
    return out


def _placeRows(codes: list = None, placeCode: str = None) -> list:
    """读 `pb_place` 的行（未软删）。`codes` 给定时按给定顺序返回。"""
    if placeCode:
        rows = sqlitePlaceRows(placeCode=[str(placeCode)])
    elif codes:
        rows = sqlitePlaceRows(placeCode=[str(one) for one in codes])
        order = {code: index for index, code in enumerate(codes)}
        rows.sort(key=lambda one: order.get(str(one.get("placeCode") or ""), 1 << 30))
    else:
        rows = []
    return rows


#: `pb_place` 行的**唯一一份**列清单（详情、同组兄弟行、批量读三处共用）。
#: ⚠️ 复制三遍的后果不是"多几行"：将来给 `pb_place` 加一列（比如 DR-36 方案②
#:    的 `parentPlaceCode`），只补了其中一处 —— 于是详情的 `members` 里有这一列、
#:    列表里没有，而两条路径都**跑得通**。
_PLACE_COLUMNS: str = (
    "g.placeCode AS placeCode, g.placeName AS placeName,"
    " g.nameZh AS nameZh, g.source AS source, g.photoCount AS photoCount,"
    " g.firstShotYear AS firstShotYear, g.lastShotYear AS lastShotYear,"
    " g.centerLat AS centerLat, g.centerLon AS centerLon,"
    " g.modifyYMDHMS AS modifyYMDHMS")


def sqlitePlaceRows(placeCode: list = None) -> list:
    """按 placeCode 批量读字典行（就是 `listPlaces` 那套 SELECT 的 IN 版）。

    ⚠️ 走 `queryCommon` 而不是生成层：生成层的 `query_pb_place` 一次只收
       一个 placeCode，而详情的「同组下钻」天然是**一批编码**。
    """
    codes = [str(one) for one in (placeCode or []) if str(one or "")]
    if not codes:
        return []
    marks = ", ".join(["%s"] * len(codes))
    sql = ("SELECT " + _PLACE_COLUMNS +
           " FROM pb_place g WHERE g.delFlag = %s AND g.placeCode IN (%s)"
           % ("%s", marks))
    return query.selectList(sql, tuple([comGD.DEL_FLAG_NO] + codes))


def placeItem(row: dict, source: str) -> dict:
    """`pb_place` 行 -> 单点条目（与步骤 9 的 `_placeSummary` 逐字同构）。

    ⚠️ 保持同构的理由：前端 `store/photos.js` 的筛选下拉直接吃这个形状，
       两处 Drift 的后果是「同一个地点在列表里能点、在下拉里点不中」。
    """
    name = str(row.get("placeName") or "")
    return {"placeCode": str(row.get("placeCode") or "")
                        or placeStore.makePlaceCode(name),
            "placeName": name,
            "nameZh": (str(row.get("nameZh")) or None) if row.get("nameZh") else None,
            "photoCount": int(row.get("photoCount") or 0),
            "yearLow": _intOrNone(row.get("firstShotYear") or row.get("yearLow")),
            "yearHigh": _intOrNone(row.get("lastShotYear") or row.get("yearHigh")),
            "centerLat": _floatOrNone(row.get("centerLat")),
            "centerLon": _floatOrNone(row.get("centerLon")),
            "sourceKind": int(row.get("source") or 0),
            "dataSource": source}


def _memberOf(row: dict) -> dict:
    """归并项里的**一个来源点**（下钻用：它是「这 N 张里的哪几张」）。"""
    return {"placeCode": str(row.get("placeCode") or ""),
            "placeName": str(row.get("placeName") or ""),
            "nameZh": (str(row.get("nameZh")) or None) if row.get("nameZh") else None,
            "photoCount": int(row.get("photoCount") or 0),
            "firstShotYear": _intOrNone(row.get("firstShotYear")),
            "lastShotYear": _intOrNone(row.get("lastShotYear")),
            "centerLat": _floatOrNone(row.get("centerLat")),
            "centerLon": _floatOrNone(row.get("centerLon"))}


def groupByNameZh(rows: list) -> dict:
    """把字典行按 `nameZh` 归并（**展示层**，DR-36 方案①）。返回分组统计。

    规则
    ----
      · `nameZh` 非空且相同 -> 并成一项（张数求和、年份取并集最值、
        坐标取第一个非空值 —— 同组都是 GPS 地点时才有坐标，目录名地点没有）；
      · `nameZh` 为空 -> **各自成组**（目录名地点的名字本身就是中文，
        不能因为「都没有 nameZh」就把「华盛顿」和「纽约」并成一项）；
      · 第一项保留**组内首次出现的 placeCode** 作为主编码（下钻入口），
        但 `placeCodes` / `members` 里**一个都不丢**。

    返回 `{items, ungroupedTotal, mergedGroups, filteredZero}`
    """
    buckets, order = {}, []
    for one in rows:
        name = str(one.get("placeName") or "")
        nameZh = str(one.get("nameZh") or "")
        # ⚠️ 空 nameZh 的分组键必须**带上 placeName**：只用 "" 当键的话
        #    「华盛顿」「纽约」「大都会博物馆」会被并成一行 ——
        #    这不是归并，是把用户的地点在界面上抹掉。
        key = nameZh if nameZh else ("\x00" + name)
        if key not in buckets:
            buckets[key] = {"placeCode": str(one.get("placeCode") or ""),
                            "placeName": name,
                            "nameZh": nameZh or None,
                            "photoCount": 0,
                            "firstShotYear": None,
                            "lastShotYear": None,
                            "centerLat": None,
                            "centerLon": None,
                            "sourceKind": int(one.get("source") or 0),
                            "placeCodes": [],
                            "members": []}
            order.append(key)
        group = buckets[key]
        group["photoCount"] += int(one.get("photoCount") or 0)
        low, high = _intOrNone(one.get("firstShotYear")), _intOrNone(one.get("lastShotYear"))
        if low is not None:
            group["firstShotYear"] = (low if group["firstShotYear"] is None
                                      else min(group["firstShotYear"], low))
        if high is not None:
            group["lastShotYear"] = (high if group["lastShotYear"] is None
                                     else max(group["lastShotYear"], high))
        if group["centerLat"] is None and one.get("centerLat") is not None:
            group["centerLat"] = _floatOrNone(one.get("centerLat"))
            group["centerLon"] = _floatOrNone(one.get("centerLon"))
        code = str(one.get("placeCode") or "")
        if code and code not in group["placeCodes"]:
            group["placeCodes"].append(code)
        group["members"].append(_memberOf(one))

    items = [buckets[key] for key in order]
    merged = len([g for g in items if len(g["placeCodes"]) > 1])
    filtered = 0
    if SKIP_ZERO_ON_GROUP:
        kept = []
        for one in items:
            if int(one["photoCount"]) > 0:
                kept.append(one)
            else:
                filtered += 1
        items = kept
        merged = len([g for g in items if len(g["placeCodes"]) > 1])
    return {"items": items, "ungroupedTotal": len(rows),
            "mergedGroups": merged, "filteredZero": filtered}


def _sortGrouped(items: list, orderBy: str, desc: bool) -> list:
    """归并后的排序：主键方向跟着 `desc`，次级键（名字）**固定 ASC**（同字典路径）。

    ⚠️ 实现用的是「**两次稳定排序**」：先按名字 ASC 排一遍，再按主键排。
       这样主键的 `reverse` 只翻主键，次级键仍是升序 —— 与
       `placeStore.PLACE_SORT_SQL` 的固定次级键是同一条规矩。
       写成 `sorted(key=(主键, 名字), reverse=desc)` 的话，名字会跟着一起倒序，
       于是「按张数降序」时同张数的两个地点名字倒着排（用户会觉得"乱跳"）。

    ⚠️ 每个主键**必须给出「缺失值怎么排」**：`firstShotYear` / `lastShotYear`
       在目录名地点上都是有值的，但手工建的地点可能没有 —— 一旦出现 None，
       直接比较会在排序里 TypeError（500），而报错信息看不出是排序键缺值。
       这里统一当成 -1（降序时排在最后，正是我们要的）。
    """
    keys = {
        "photoCount": lambda one: int(one.get("photoCount") or 0),
        "placeName": lambda one: str(one.get("placeName") or ""),
        "placeCode": lambda one: str(one.get("placeCode") or ""),
        "lastShotYear": lambda one: _intOrNone(one.get("lastShotYear")) or -1,
        "firstShotYear": lambda one: _intOrNone(one.get("firstShotYear")) or -1,
    }
    pick = keys.get(str(orderBy or "photoCount"), keys["photoCount"])
    byName = sorted(items, key=lambda one: str(one.get("placeName") or ""))
    return sorted(byName, key=pick, reverse=bool(desc))


def placeKeysOf(rows: list) -> list:
    """一组字典行 -> 去重后的**聚合键**列表（= `pb_place.placeName`）。

    ⚠️ 这里的关键事实：`rebuildPlaces()` 把**聚合键本身**落在 `placeName` 列上
       （见 placeStore 的 STEP 2）。所以「按地点筛照片」= 用这些值去比
       `placeKeySql("p")`，两处是同一条键 —— 用 `placeCode` 比不了
       （`pb_photo` 根本没有这一列，R4b 的注释里写明了）。
    """
    keys = []
    for one in rows:
        name = str(one.get("placeName") or "").strip()
        if name and name not in keys:
            keys.append(name)
    return keys


def keysWhere(alias: str, keys: list) -> tuple:
    """聚合键列表 -> `(sql 片段, 参数)`。空列表给一个恒假条件（不返回全部照片）。"""
    if not keys:
        return ("1 = 0", [])
    marks = ", ".join(["%s"] * len(keys))
    return ("%s IN (%s)" % (placeStore.placeKeySql(alias), marks), list(keys))


# ============================================================
# 二、GET /api/places（列表）
# ============================================================

@router.get("/places", summary="地点列表（字典；groupByNameZh=1 按中文名归并）")
def listPlaces(page: int = Query(default=1, ge=1),
               size: int = Query(default=200, le=2000, description="单页条数，上限 2000"),
               keyword: str = Query(default=None,
                                    description="地点名模糊匹配（同时匹配中文名 nameZh）"),
               minPhotoCount: int = Query(default=None, ge=0,
                                          description="只返回照片数 >= 此值的地点"),
               orderBy: str = Query(default="photoCount",
                                    description="photoCount / placeName / "
                                                "lastShotYear / firstShotYear / placeCode"),
               desc: int = Query(default=1),
               live: int = Query(default=0,
                                 description="1 = 强制走实时聚合（降级路径，慢）"),
               groupByNameZh: int = Query(default=0,
                                          description="1 = 按中文名归并同名地点"
                                                      "（展示层分组，placeCode 不变）"),
               year: int = Query(default=None, ge=1, le=9999,
                                 description="只看**该年有照片**的地点（展示层筛选，"
                                             "需配 groupByNameZh=1）"),
               hasPerson: int = Query(default=None,
                                      description="1 只看**有人物关联**的地点"
                                                  "（实时 join，需配 groupByNameZh=1）")):
    """按地点聚合的照片张数（降序）。分页体与其它列表接口一致。

    数据来自 **`pb_place` 地点字典表**，不是每次现算。
    `source="dictionary"` 读字典；`"lib"` 是字典表不存在/为空时的**降级实时聚合**。

    `groupByNameZh=1`（R5 新增，展示层归并 · DR-36 方案①）
    ---------------------------------------------------
      `placeCode` 由**街道级** GPS 名派生，而 `nameZh` 来自**区县级**行政区划 ⇒
      多个地点折叠成同一个中文名（实测 3 组）。归并只在**这里**发生：
        · 同 `nameZh` 的行并成一项，`photoCount` 求和、年份取并集最值
        · 行为顺带**过滤 `photoCount = 0`** 的幽灵行（DR-33：它是派生缓存
          被清空后留下的空壳，界面上不该出现）
        · 每一项多带 `placeCodes` / `members`（下钻用）与 `mergedGroups` 计数
      ⚠️ 归并**不改任何 placeCode**，也不是把照片混成一堆：`members` 里
        每个来源点各自的张数都在。

    ⚠️ 不传 `groupByNameZh` 时行为与步骤 9 **逐字一致**（含 `total` 把
       0 张的幽灵行算进去）—— 「归零但留行」是 rebuild 的既定语义，
       本步不替它改口径。

    ⚠️ `total` 是**不同地点的个数**（不是照片数）；`photoCount` 才是张数。
       `photoCount` 是**缓存值**，扫描新照片后请调 `POST /api/places/rebuild`。
    """
    p, s = dto.clampPage(page, size, defaultSize=200)
    at = dto.offsetOf(p, s)
    if orderBy not in placeStore.PLACE_SORT_SQL:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "orderBy 只支持 %s，收到 %r"
                           % (sorted(placeStore.PLACE_SORT_SQL), orderBy))

    useLive = bool(int(live)) or not query.tableExists("pb_place")
    if bool(int(groupByNameZh)):
        return _groupedList(p, s, at, keyword, minPhotoCount, orderBy,
                            bool(int(desc)), useLive, year=year, hasPerson=hasPerson)
    # ⚠️ `year` / `hasPerson` **只在归并路径上实现**，并且**显式报错**而不是
    #    静默忽略：它们是「展示层的筛选」（按年、按有没有人），而
    #    `pb_place` 的缓存列里没有「年份集合」与「在场人数」——
    #    未归并路径要把它们做对就得改分页口径（先筛后翻），
    #    而静默忽略会让调用方以为筛过了：界面显示 24 个地点、
    #    其中一半不符合条件，没有任何地方会报错。
    if year is not None or hasPerson is not None:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "year / hasPerson 是展示层筛选，请配合 groupByNameZh=1 使用"
                           "（未归并路径的 total 与分页口径与它们不兼容）")

    fromDict = placeStore.listPlaces(keyword=keyword or "",
                                     minPhotoCount=minPhotoCount,
                                     orderBy=orderBy, desc=bool(int(desc)),
                                     limitNum=s, offsetNum=at) if not useLive \
        else {"items": [], "total": 0, "missingTable": True}
    if fromDict["total"] > 0 and not useLive:
        items = [placeItem(r, "dictionary") for r in fromDict["items"]]
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
    rows = placeStore.liveAggregatePlaces(limitNum=2000, keyword=keyword or "",
                                          orderBy=orderBy, desc=bool(int(desc)))
    items = [placeItem(r, "lib") for r in rows]
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


def _itemKeys(item: dict) -> list:
    """归并项 -> 它的全部聚合键（= 成员的 placeName）。"""
    return [str(one.get("placeName") or "") for one in (item.get("members") or [])
            if str(one.get("placeName") or "")]


def personCountsOf(keys: list) -> dict:
    """一批聚合键 -> `{key: 在场人数}`（**实时 join**，DR-26）。

    ⚠️ 一次 GROUP BY 算一批键，而不是「每个地点查一次」：24 个地点就是 24 次
       往返，而它明明可以是一条 SQL（与 `personCountOf` 的单点版本共用键）。
    """
    if not keys:
        return {}
    where, values = keysWhere("p", keys)
    out = {}
    for one in query.selectList(
            "SELECT " + placeStore.placeKeySql("p") + " AS placeKey,"
            " COUNT(DISTINCT j.personCode) AS personCount"
            " FROM pb_photo_person j JOIN pb_photo p ON p.photoCode = j.photoCode"
            " WHERE j.delFlag = %s AND p.delFlag = %s"
            " AND j.personCode IS NOT NULL AND j.personCode <> '' AND " + where +
            " GROUP BY " + placeStore.placeKeySql("p"),
            tuple([comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO] + list(values))):
        out[str(one.get("placeKey") or "")] = int(one.get("personCount") or 0)
    return out


def yearsOf(keys: list) -> dict:
    """一批聚合键 -> `{key: set(shotYear)}`（展示层的「按年筛地点」用）。

    ⚠️ 为什么要**年份集合**而不是用列表里的 `firstShotYear`..`lastShotYear`
       区间去判：区间是**近似**——某地点 2009 与 2019 各有一张、2015 一张没有，
       区间判断会说「2015 年他去过这里」，而那是假的。年份集合是 663 行的
       一次 GROUP BY，代价可以忽略。
    """
    if not keys:
        return {}
    where, values = keysWhere("p", keys)
    # DR-42：这里给出的年份集合必须与照片上的年份同源（有效拍摄年），
    # 否则「按年筛地点」会把修正过的照片留在它 EXIF 那一年里。
    effectiveYear = comGD.sqlEffectiveShotYear("p")
    out = {}
    for one in query.selectList(
            "SELECT " + placeStore.placeKeySql("p") + " AS placeKey,"
            " " + effectiveYear + " AS shotYear FROM pb_photo p"
            " WHERE p.delFlag = %s AND " + where +
            " GROUP BY " + placeStore.placeKeySql("p") + ", " + effectiveYear,
            tuple([comGD.DEL_FLAG_NO] + list(values))):
        year = _intOrNone(one.get("shotYear"))
        if year is None:
            continue
        out.setdefault(str(one.get("placeKey") or ""), set()).add(year)
    return out


def coversOf(keys: list) -> dict:
    """一批聚合键 -> `{key: {"photoCode": 封面, "rotateDeg": 人工旋转角度}}`。

    ⚠️ 卡片封面必须走 `/api/thumb`（红线），所以这里只回 `photoCode`，
       由前端拼缩略图 URL —— 回一个文件路径就等于把「哪儿能取图」
       这件事漏给了两个地方（前端 + `api/static.py`）。
    ⚠️ DR-43：`rotateDeg` **必须跟封面一起给**。封面是 4:3 固定框 + `object-cover`，
       少这个值的话「照片流里转了、地点卡片上没转」—— 同一个封面在这里是躺着的，
       而接口 200、结构齐全，只有那一个字段缺失（静默错，最难发现的那种）。
    """
    if not keys:
        return {}
    where, values = keysWhere("p", keys)
    covers = {}
    for one in query.selectList(
            "SELECT " + placeStore.placeKeySql("p") + " AS placeKey,"
            " p.photoCode AS photoCode, p.rotateDeg AS rotateDeg FROM pb_photo p"
            " WHERE p.delFlag = %s AND " + where +
            " AND p.photoCode IS NOT NULL AND p.photoCode <> ''"
            " ORDER BY p.takenAt DESC, p.recID DESC",
            tuple([comGD.DEL_FLAG_NO] + list(values))):
        covers.setdefault(str(one.get("placeKey") or ""),
                          {"photoCode": str(one.get("photoCode") or ""),
                           "rotateDeg": browse.rotateDegOf(one)})
    return covers


def attachCards(items: list) -> list:
    """给**本页**的归并项补上卡片要用的三个字段（封面 + 在场人数 + 年份集合）。

    ⚠️ 只在切页**之后**做：全量 663 张照片取封面是没必要的（一页只有
       最多 200 行），而「按地点全量取一次封面」在 10 万张规模下会变成
       一条返回 10 万行的查询。
    """
    keys = []
    for item in items:
        for key in _itemKeys(item):
            if key not in keys:
                keys.append(key)
    covers = coversOf(keys)
    counts = personCountsOf(keys)
    years = yearsOf(keys)
    for item in items:
        mine = _itemKeys(item)
        cover = next((covers.get(k) for k in mine if covers.get(k)), None)
        item["coverPhotoCode"] = (cover or {}).get("photoCode")
        # DR-43：封面的显示角度（前端 4:3 固定框要转 img + scale(4/3)，见 PlacesView）
        item["coverRotateDeg"] = int((cover or {}).get("rotateDeg") or 0)
        item["personCount"] = sum(int(counts.get(k) or 0) for k in mine)
        merged = set()
        for key in mine:
            merged |= years.get(key) or set()
        item["years"] = sorted(merged, reverse=True)
    return items


def _groupedList(p: int, s: int, at: int, keyword, minPhotoCount, orderBy: str,
                 desc: bool, useLive: bool, year: int = None,
                 hasPerson: int = None) -> dict:
    """`groupByNameZh=1` 的实现：**先全量读、再筛、再归并、最后切页**。

    ⚠️ 为什么不能「先分页再归并」：归并会让总行数变少，offset 的含义随之改变 ——
       第 2 页会漏掉/重复一部分地点，而且**每页看起来都正常**。
       地点是几十~几百量级（实测 28 行），全量读的代价可以忽略。

    ⚠️ `year` / `hasPerson` 也必须在**切页之前**筛：它们是「哪些地点要出现」，
       不是「这一页里显示哪几条」。放到切页后筛的话，第 1 页会说
       「24 个地点」而里面有 12 个不符合条件，翻页时数字还会变。
    """
    if useLive:
        rows = placeStore.liveAggregatePlaces(limitNum=0, keyword=keyword or "",
                                              orderBy=orderBy, desc=desc)
        source = "lib"
    else:
        # ⚠️ `minPhotoCount` 交给 placeStore 做（它的 LIKE / >= 都在同一处）；
        #    归并路径额外把 0 张的行滤掉（见 SKIP_ZERO_ON_GROUP）。
        rows = placeStore.listPlaces(keyword=keyword or "",
                                     minPhotoCount=minPhotoCount,
                                     orderBy=orderBy, desc=desc,
                                     limitNum=0)["items"]
        source = "dictionary"
    grouped = groupByNameZh(rows)
    items = _sortGrouped(grouped["items"], orderBy, desc)

    if hasPerson is not None or year is not None:
        allKeys = []
        for item in items:
            for key in _itemKeys(item):
                if key not in allKeys:
                    allKeys.append(key)
        kept = []
        wantPerson = int(hasPerson) if hasPerson is not None else 0
        counts = personCountsOf(allKeys) if wantPerson else {}
        years = yearsOf(allKeys) if year is not None else {}
        for item in items:
            mine = _itemKeys(item)
            if wantPerson and sum(int(counts.get(k) or 0) for k in mine) <= 0:
                continue
            if year is not None and int(year) not in set(
                    y for k in mine for y in (years.get(k) or ())):
                continue
            kept.append(item)
        items = kept

    pageItems = attachCards(items[at:at + s])
    body = dto.pageBody([dict(one, dataSource=source) for one in pageItems],
                        p, s, len(items))
    body["source"] = source
    body["grouped"] = True
    body["dictionaryRebuilt"] = source == "dictionary"
    body["dictionaryRebuiltAt"] = next(
        (str(one.get("modifyYMDHMS") or "") for one in rows
         if one.get("modifyYMDHMS")), "") or None
    # ⚠️ 三个诊断数字直接回给调用方（验收第 1/3 条要靠它们说清「28 -> 24」）：
    #    ungroupedTotal 是**归并前读到的字典行数**（keyword/minPhotoCount
    #    这些筛选已经生效，但 0 张的幽灵行还在里面），
    #    filteredZero 是被归并路径滤掉的幽灵行数，mergedGroups 是被并掉的组数。
    #    ⇒ 恒等式：`ungroupedTotal - filteredZero - mergedGroups == total`。
    #    少回其中任何一个，「列表里少了两行」都只能靠人去数。
    body["ungroupedTotal"] = grouped["ungroupedTotal"]
    body["filteredZero"] = grouped["filteredZero"]
    body["mergedGroups"] = grouped["mergedGroups"]
    body["note"] = ("按 nameZh 归并（展示层分组，placeCode 未改）；"
                    "photoCount 是缓存值，扫描新照片后请调 POST /api/places/rebuild 刷新")
    return body


# ============================================================
# 三、POST /api/places/rebuild
# ============================================================

@router.post("/places/rebuild", summary="重建地点字典（pb_place 全量幂等复算）")
def rebuildPlaces():
    """从 `pb_photo` 全量复算 `pb_place`（**幂等**：不清表，按 placeCode upsert）。

    ⚠️ R4b 起：这里跑的是**地点侧全链路**（`placeFinalize.finalizePlaces`）
        ① 填 `pb_photo.placeNameDir`（**只填空**，来自目录名线索）
        ② 重建 `pb_place`（聚合键 = 目录名优先，DR-32）
        ③ 补 `nameZh`（只填 NULL，绝不覆盖；`PLACE_FINALIZE_NAME_ZH` 可关）

    ⚠️ **为什么不落 `pb_review_log`**：本接口只重建纯派生缓存，一个字段都不
       来自人工输入，且可从 `pb_photo` 100% 重算还原 —— 和「生成缩略图」同类。
       审计要看 `pb_place.modifyYMDHMS`（= 最后一次复算时刻）。

    返回里除 rebuild 统计外还有三个诊断字段：
      scanFilled  ：本次新填了多少行 `placeNameDir`
      driftCount  ：**目录改名**的漂移行数（只报不覆盖）
      suspectCount：判据没认出来的**疑似地点**目录数（只报不采纳）
    """
    try:
        report = placeFinalize.finalizePlaces(source="api:places/rebuild")
    except RuntimeError as e:                 # 兜底：finalizePlaces 内部已不抛
        raise dto.ApiError(dto.CODE_DB_ERROR, str(e))
    info = report.get("rebuild")
    if not info:
        raise dto.ApiError(dto.CODE_DB_ERROR,
                           "；".join(report.get("errors") or []) or "重建地点字典失败")
    scan = report.get("scan") or {}
    return dto.okBody(source="rebuilt", **info,
                      scanFilled=scan.get("written"),
                      scanAlreadyFilled=scan.get("alreadyFilled"),
                      driftCount=len(report.get("drift") or []),
                      suspectCount=len(report.get("suspects") or []),
                      suspects=[one.get("dir") for one in
                                (report.get("suspects") or [])[:10]],
                      nameZhFilled=(report.get("nameZh") or {}).get("filled"),
                      finalizeErrors=list(report.get("errors") or []))


@router.get("/places/rebuild", include_in_schema=False)
def placesRebuildOnlyPost():
    """**只为保住 405**：`/api/places/rebuild` 只收 POST。

    ⚠️ 下面 `/{placeCode}` 是参数段通配，会把这条静态路径一起吃掉 ——
       于是 `GET /api/places/rebuild` 从「405 方法不对」退化成
       「404 这个地点不存在」，调用方会去查地点名，而真相是方法写错了。
       显式声明一条、抛 405 + `Allow: POST`，把语义原样还回去
       （错误体由 `dto` 的 405 处理器统一生成，与自动 405 逐字一致）。
    """
    raise HTTPException(status_code=405, detail="Method Not Allowed",
                        headers={"Allow": "POST"})


# ============================================================
# 四、GET /api/places/{placeCode}（详情）
# ============================================================

def _detailRows(placeCode: str, placeCodes: str) -> list:
    """详情/子资源共用：定位「一组」字典行。

    给了 `?placeCodes=a,b,c` 就用它（列表项下钻时带的**已归并同组**）；
    否则按 nameZh 自动取全同名的行（用户直接敲 URL / 收藏夹进来的路径）。
    """
    codes = _splitCodes(placeCodes)
    if codes:
        rows = _placeRows(codes=codes)
        if not rows:
            raise dto.ApiError(dto.CODE_NOT_FOUND,
                               "placeCodes=%s 在 pb_place 里都不存在" % placeCodes)
        return rows
    main = _placeRows(placeCode=placeCode)
    if not main:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "placeCode=%s 在 pb_place 里不存在" % placeCode)
    nameZh = str(main[0].get("nameZh") or "")
    if not nameZh:
        return main
    # ⚠️ 自动并同名的行：这是「重名归并」在**详情**侧的对应物 ——
    #    列表并了一项，点进来必须还是那一项（否则「15 张」点进去只有 8 张，
    #    而接口 200、页面正常，没有任何地方会报错）。
    siblings = query.selectList(
        "SELECT " + _PLACE_COLUMNS +
        " FROM pb_place g WHERE g.delFlag = %s AND g.nameZh = %s"
        " ORDER BY COALESCE(g.photoCount, 0) DESC, g.placeCode ASC",
        (comGD.DEL_FLAG_NO, nameZh))
    rows = [one for one in siblings
            if str(one.get("placeCode")) == str(main[0].get("placeCode"))] + \
           [one for one in siblings
            if str(one.get("placeCode")) != str(main[0].get("placeCode"))]
    return rows or main


def _placeBase(rows: list) -> dict:
    """一组字典行 -> 详情头（张数求和 / 年份并集 / 坐标取首个非空）。"""
    total = 0
    low = high = None
    lat = lon = None
    for one in rows:
        total += int(one.get("photoCount") or 0)
        first, last = _intOrNone(one.get("firstShotYear")), _intOrNone(one.get("lastShotYear"))
        low = first if low is None else (min(low, first) if first is not None else low)
        high = last if high is None else (max(high, last) if last is not None else high)
        if lat is None and one.get("centerLat") is not None:
            lat, lon = _floatOrNone(one.get("centerLat")), _floatOrNone(one.get("centerLon"))
    main = rows[0]
    return {"placeCode": str(main.get("placeCode") or ""),
            "placeName": str(main.get("placeName") or ""),
            "nameZh": (str(main.get("nameZh")) or None) if main.get("nameZh") else None,
            "photoCount": total,
            "firstShotYear": low,
            "lastShotYear": high,
            "centerLat": lat,
            "centerLon": lon,
            # ⚠️ `hasCoord` 是给界面用的**布尔**：11/28 个目录名地点
            #    centerLat/Lon 全是 NULL（DR-34），界面据此**不显示**坐标/地图，
            #    而不是显示一个 "0, 0"（那正好是几内亚湾，DR-25 踩过的坑）。
            "hasCoord": lat is not None and lon is not None,
            "placeCodes": [str(one.get("placeCode") or "") for one in rows],
            "members": [_memberOf(one) for one in rows],
            "merged": len(rows) > 1}


def personCountOf(keys: list) -> int:
    """该地点**实时**关联到的人数（DR-26：不落库，一次 DISTINCT）。

    ⚠️ 聚合键只能挂在 **`pb_photo`** 的别名上：`pb_photo_person` 里
       **没有** `placeNameDir` / `placeName` 这两列（它只存 photoCode +
       personCode），把键写成 `pp.placeNameDir` 会直接查不到列。
    """
    where, values = keysWhere("p", keys)
    return int(query.selectValue(
        "SELECT COUNT(DISTINCT j.personCode) AS rowNum FROM pb_photo_person j"
        " JOIN pb_photo p ON p.photoCode = j.photoCode"
        " WHERE j.delFlag = %s AND p.delFlag = %s AND j.personCode IS NOT NULL"
        " AND j.personCode <> '' AND " + where,
        tuple([comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO] + list(values)),
        default=0) or 0)


@router.get("/places/{placeCode}", summary="地点详情（含同组下钻 placeCodes / members）")
def getPlace(placeCode: str,
             placeCodes: str = Query(default=None, description=_PLACE_CODES_DESC)):
    """地点详情：张数 / 年份跨度 / 坐标 / **同组下钻**。

    ⚠️ `photoCount` 取的是**字典里的缓存值之和**（与列表口径一致）。
       它与 `/photos` 的 `total` 可能差几张（扫描后没 rebuild）——
       界面上若两处不一致，先请用户点一次「刷新地点」，这是既定口径，
       见 `placeStore` 文件头「派生缓存不是真值」。

    ⚠️ `members[]` 是**下钻**的依据：归并成「北京市 · 朝阳区 15 张」之后，
       详情里要能看出「大屯 8 张 / 望京 7 张」。丢掉 members 就是
       「把同组的照片混成一堆而丢失来源」—— DR-36 明确禁止。
    """
    rows = _detailRows(placeCode, placeCodes)
    out = _placeBase(rows)
    out["dataSource"] = "dictionary"
    out["personCount"] = personCountOf(placeKeysOf(rows))
    return out


# ============================================================
# 五、GET /api/places/{placeCode}/photos
# ============================================================

@router.get("/places/{placeCode}/photos",
            summary="该地点的照片（分页；带全年份计数供按年分组）")
def listPlacePhotos(placeCode: str,
                    placeCodes: str = Query(default=None, description=_PLACE_CODES_DESC),
                    page: int = Query(default=1, ge=1),
                    size: int = Query(default=dto.DEFAULT_PAGE_SIZE),
                    year: int = Query(default=None, description="只看某一年"),
                    anchorPhotoCode: str = Query(default=None,
                                                 description="锚点照片：返回它所在的那一页"
                                                             "（scope 翻页定位用）")):
    """`/photos` 的「地点已定」版本：**不需要前端拼 placeName**。

    ⚠️ 为什么不让前端直接调 `/photos?placeName=…`
        · 归并项有**多个** placeCode，前端得自己把它们翻译成一串聚合键 ——
          那是数据层的事（`placeCodes` 属于 `pb_place` 的知识）；
        · 多一层翻译就多一处 Drift：某天归并规则变了，前端那份列表静默过期。
        `/photos` 仍然是「按单个地点名筛」的入口，两者共用同一把键
        （`placeStore.placeKeySql`），所以结果必然一致。

    ⚠️ `years[]` 是**整个地点**的按年计数（一次 GROUP BY），不是本页的 ——
      界面用它的数字做年份标题；用本页 items 现算的话，
      第一页只会有「2013 年 60 张」这种**看着对但其实是分页边界**的数字。

    锚点定位（`anchorPhotoCode`）
    --------------------------
      与 `/photos` 同一套语义（见 `browse.anchorOffsetOf`）：给了它且这张照片
      在本筛选结果里时，返回**它所在的那一页**（响应里的 `page` 是算出来的）。
      照片详情页从「地点详情」点进来时带 `?scope=place:`，靠它一次落到正确位置
      —— 否则这个地点的老照片排在几十页之后，左右箭头会全禁用。
    """
    rows = _detailRows(placeCode, placeCodes)
    keys = placeKeysOf(rows)
    base = _placeBase(rows)
    if not keys:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "placeCode=%s 没有任何可用聚合键" % placeCode)

    p, s = dto.clampPage(page, size)
    at = dto.offsetOf(p, s)
    where, values = keysWhere("p", keys)
    # DR-42：年筛选与「年代直方图」都走**有效拍摄年**，与照片列表里显示的年份同源。
    effectiveYear = comGD.sqlEffectiveShotYear("p")
    cond = ["p.delFlag = %s", where]
    args = [comGD.DEL_FLAG_NO] + list(values)
    if year is not None:
        cond.append(effectiveYear + " = %s")
        args.append(int(year))
    sqlCond = " AND ".join(cond)

    total = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE " + sqlCond,
        tuple(args), default=0) or 0)
    # DR-31 补：scope 翻页的首屏定位（排序固定 takenAt DESC，见 browse.anchorOffsetOf）
    anchorAt = browse.anchorOffsetOf(sqlCond, args, "p.takenAt", anchorPhotoCode)
    if anchorAt is not None:
        p = anchorAt // s + 1
        at = dto.offsetOf(p, s)
    years = [{"year": _intOrNone(one.get("shotYear")),
              "count": int(one.get("cnt") or 0)}
             for one in query.selectList(
                 "SELECT " + effectiveYear + " AS shotYear, COUNT(*) AS cnt FROM pb_photo p"
                 " WHERE " + sqlCond + " GROUP BY " + effectiveYear +
                 " ORDER BY " + effectiveYear + " DESC", tuple(args))]
    photos = query.selectList(
        # ⚠️ `placeNameDir` 与 `lat/lon` **必须出现在 SELECT 里**：
        #    `browse.photoSummary()` 用 placeNameDir 算聚合键（否则那 598 张
        #    目录名照片在这一页上「没有地点」），用 lat/lon 判 hasGps。
        #    这两列没带的话接口 200、结构齐全，只是地点与 GPS 角标全空。
        "SELECT p.photoCode, p.relPath, p.takenAt, p.shotYear, p.shotYearOverride,"
        # ⚠️ R9 / DR-43：rotateDeg **必须**在这里（R5 新增的第 6 处照片 SELECT）。
        #    少这一列的症状是「照片流里转了、地点详情里没转」——
        #    接口 200、结构齐全、只有那一个字段缺失，界面静默错。
        " p.rotateDeg,"
        " p.placeName, p.placeNameDir, p.lat, p.lon,"
        " p.cameraModel, p.width, p.height, p.fileSize, p.mimeType,"
        " p.faceCount, p.isDuplicate, p.isMissing, p.scanState"
        " FROM pb_photo p WHERE " + sqlCond +
        " ORDER BY p.takenAt DESC, p.recID DESC LIMIT %s OFFSET %s",
        tuple(args) + (s, at))
    body = dto.pageBody([browse.photoSummary(one) for one in photos], p, s, total)
    body["place"] = {"placeCode": base["placeCode"], "placeName": base["placeName"],
                     "nameZh": base["nameZh"], "placeCodes": base["placeCodes"],
                     "photoCount": base["photoCount"]}
    body["years"] = years
    body["year"] = _intOrNone(year)
    return body


# ============================================================
# 六、GET /api/places/{placeCode}/persons
# ============================================================

@router.get("/places/{placeCode}/persons",
            summary="出现在该地点的人（实时 join pb_photo_person，DR-26）")
def listPlacePersons(placeCode: str,
                     placeCodes: str = Query(default=None, description=_PLACE_CODES_DESC),
                     size: int = Query(default=200, ge=1, le=dto.MAX_PAGE_SIZE)):
    """「当时在场的人」：`pb_photo_person ⋈ pb_photo` 现算，**不落库**（DR-26/28）。

    为什么没有关联表
    ----------------
      改判一张脸、停用一个人、导入新联系人 —— 冗余的关联表**三处都得同步**，
      漏一处就是错的统计，而漏了不报错。实时 join 在这个规模是毫秒级
      （`pb_photo_person` 有 personCode 索引）。

    ⚠️ 实测数据现状（R5 只读探查）：`pb_photo` 里 663 张有地点的照片中，
       **已关联到人物的只有个位数**（`pb_photo_person` 全表 206 行，
       其中照片带地点的很少）。所以本接口**大多数地点返回空数组 —— 这是数据
       现状不是 bug**。空的时候界面必须给**引导文案**（去待确认队列确认人脸），
       而不是一片空白：只写「暂无」会让人分不清「没做」与「真没有」。

    返回 `locatedPhotoTotal` / `photoTotal` 正是为了让那句引导能带上数字。
    """
    rows = _detailRows(placeCode, placeCodes)
    keys = placeKeysOf(rows)
    if not keys:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "placeCode=%s 没有任何可用聚合键" % placeCode)
    where, values = keysWhere("p", keys)
    args = [comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO] + list(values)
    # DR-42：某人在这地点的年代跨度用**有效拍摄年**（与年代档同源）
    effectiveYear = comGD.sqlEffectiveShotYear("p")

    personRows = query.selectList(
        "SELECT j.personCode AS personCode,"
        " COUNT(DISTINCT j.photoCode) AS photoCount,"
        " MIN(" + effectiveYear + ") AS firstShotYear,"
        " MAX(" + effectiveYear + ") AS lastShotYear,"
        " COUNT(DISTINCT j.photoCode) AS locatedPhotoCount"
        " FROM pb_photo_person j JOIN pb_photo p ON p.photoCode = j.photoCode"
        " WHERE j.delFlag = %s AND p.delFlag = %s"
        " AND j.personCode IS NOT NULL AND j.personCode <> '' AND " + where +
        " GROUP BY j.personCode ORDER BY photoCount DESC, j.personCode ASC"
        " LIMIT %s",
        tuple(args) + (int(size),))
    # 封面脸：**先批量解析**再进循环（DR-40）—— 逐人查就是 N+1
    # ⚠️ `personCoversOf` 要的是**人物行**（它得看 `avatarFaceCode` 还在不在），
    #    所以这里先把人物行收齐。`personRow` 这一步本身是既有的逐人查询，
    #    不在本步的改动范围（改它要动的地方比这一行注释多得多）。
    foundRows = {str(one.get("personCode") or ""):
                 (browse.personRow(str(one.get("personCode") or ""),
                                   withDeleted=True) or {})
                 for one in personRows}
    covers = browse.personCoversOf(list(foundRows.values()))

    items = []
    for one in personRows:
        code = str(one.get("personCode") or "")
        found = foundRows.get(code) or {}
        avatar = found.get("avatarFaceCode") or None
        # 头像与人物库/联系人同口径（DR-40）：默认（还活着才认）> 代表脸 > 无
        cover = covers.get(code) or None
        items.append({"personCode": code,
                      "displayName": str(found.get("displayName") or code),
                      "delFlag": str(found.get("delFlag") or comGD.DEL_FLAG_NO),
                      "avatarFaceCode": avatar,
                      "coverFaceCode": cover,
                      "thumbUrl": ("/api/face/%s" % cover) if cover else None,
                      # 通讯录头像 = 头像三级回退的最后一级（见 browse.personSummary）
                      "contactAvatarUrl": (("/api/avatar/%s" % code)
                                           if found.get("avatarFile") else None),
                      "photoCount": int(one.get("photoCount") or 0),
                      "firstShotYear": _intOrNone(one.get("firstShotYear")),
                      "lastShotYear": _intOrNone(one.get("lastShotYear")),
                      "detailUrl": "/api/persons/%s" % code})

    photoTotal = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p WHERE p.delFlag = %s AND " + where,
        tuple([comGD.DEL_FLAG_NO] + list(values)), default=0) or 0)
    return {"placeCode": str(rows[0].get("placeCode") or ""),
            "placeCodes": [str(one.get("placeCode") or "") for one in rows],
            "photoTotal": photoTotal,
            "personTotal": len(items),
            "items": items}
