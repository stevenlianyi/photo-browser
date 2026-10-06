#! /usr/bin/env python3
#encoding: utf-8

#Filename: placeStore.py
#Description: 地点字典（pb_place）的聚合与查询 —— 步骤 9 为 /api/places 加
#
# 为什么要这张表（原来只在 pb_photo.placeName 上 GROUP BY）
# ------------------------------------------------------
#   1. `/api/places` 每次请求都要 `GROUP BY placeName` 扫全表：
#      10 万张实测 **p50 95ms**，且随库线性增长。字典表只有几十~几百行。
#   2. 没有可引用的**地点编码**，就无法做「地点 → 照片」的反向索引，
#      筛选只能退化成字符串 LIKE。
#   3. 同一地点的多种写法（`北京` / `北京市` / `Beijing`）永远是三条并列记录，
#      地图上就是三个点 —— 有了字典表才有地方收敛（步骤 12）。
#   4. 地图要中心点，否则每次都要 `AVG(lat)` 现算。
#
# 两条纪律
# --------
#   * **只读入口是纯读**：`listPlaces()` 不写任何一行。
#     「补齐字典」必须走显式的 `rebuildPlaces()`（由 POST /api/places/rebuild 触发）。
#     在 GET 里顺手回填等于让只读接口有副作用 —— 缓存了代理和重试就没法自证清白。
#   * **派生缓存，不是真值**：`photoCount` / `firstShotYear` / `lastShotYear` /
#     `centerLat` / `centerLon` / `placeName` 全部可从 `pb_photo` 重算。
#     所以 `rebuildPlaces()` 是**幂等全量复算**（不清表、按 placeCode upsert），
#     而不做增量维护 —— 增量维护需要挂在扫描链路里，那条链路上多一个写点，
#     就多一处「扫描成功但字典没更新」的静默不一致。
#
# ⚠️ 为什么不落 pb_review_log
# ------------------------
#   硬约束「每个写操作都要落 pb_review_log」针对的是**会影响识别结果或人工判断**的
#   改动（人脸归属、人员档案）。重建地点字典是**纯派生缓存**：
#   一个字段都不来自人工输入，且可从 pb_photo 完全重算 100% 还原。
#   它和「生成缩略图」是同一类操作，而缩略图也不落纠错日志。
#   真要为审计留痕，看 pb_place.modifyYMDHMS（= 最后一次复算时刻）就够了。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/place
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.contact import contactCommon as contact            # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("placeStore", "placestore.log")

#: 地点编码前缀（与 personCode 的 `UI_`/`CS_`/`VC_` 同一套「前缀可读」约定）
PLACE_CODE_PREFIX: str = "PL_"

#: `source` 取值
SOURCE_SYSTEM: int = 0        # 系统聚合（rebuildPlaces 产出）
SOURCE_MANUAL: int = 1        # 手工建档（步骤 12；本步不产生）

#: upsert 时会**被复算覆盖**的列。
#: ⚠️ 刻意不含 `source`：那是「这一行当初怎么来的」，不是派生值，
#:   复算绝不能把手工行的 source 打回 0（否则「手工地点」这个概念就没了）。
REBUILD_COLUMNS: tuple = ("placeName", "photoCount", "firstShotYear",
                          "lastShotYear", "centerLat", "centerLon")

#: `GET /api/places` 允许的**主排序字段**（白名单常量，不是用户输入）。
#: ⚠️ 这里只放**一个**列，方向由 `desc` 决定；
#:   次级排序（tie-breaker）是**固定 ASC** 的 `placeName, placeCode`，见 listPlaces。
#:   为什么不在本表里写一串「次级列」：那样 `desc` 会把**每一列**都翻成 DESC
#:   —— 于是「按张数降序」时同名次的地点按名字**倒序**排，
#:   与用户预期（以及列表接口的通行做法）相反，且分页顺序看起来"乱跳"。
#:   次级键的作用只是「让顺序确定」，它不该跟着主键的方向翻转。
PLACE_SORT_SQL: dict = {
    "photoCount": "photoCount",
    "placeName": "placeName",
    "lastShotYear": "lastShotYear",
    "firstShotYear": "firstShotYear",
    "placeCode": "placeCode",
}

#: 固定次级排序（永远 ASC）：先按名字，同名再按编码。
#: ⚠️ 编码兜底不是多余的：`placeName` 在同一张表里可以有重复行
#:   （手工建的 + 聚合出来的），只按名字排会让两行的相对顺序不确定 ->
#:   同一页请求两次可能给出不同的行。
PLACE_TIE_BREAK_COLUMNS: tuple = ("placeName", "placeCode")


def makePlaceCode(placeName: str) -> str:
    """placeName -> placeCode（幂等键）。

    ⚠️ 复用 `contactCommon.sanitizeCodeKey`，**不另写一份**：
       「业务键 -> 可读 ASCII 编码」这件事只能有一套规则。
       两份实现必然在某次改动后漂移，而漂移的后果是
       「同一个地点在两次 rebuild 之间换了编码」-> upsert 认不出旧行 ->
       **字典里出现两条同名地点**，且不报错。
       （`sanitizeCodeKey` 对中文名会退化成 sha1 前缀，这对地点名是常态，
        也是它必须稳定的原因 —— 它已经是 PB 里多个编码的共用底层。）
    """
    text = str(placeName or "").strip()
    if not text:
        return ""
    return ("%s%s" % (PLACE_CODE_PREFIX, contact.sanitizeCodeKey(text)))[:64]


def rebuildPlaces(dbFile: str = None) -> dict:
    """从 `pb_photo` **全量复算**地点字典（幂等）。返回统计。

    步骤
    ----
      1. `GROUP BY placeName` 聚合出 张数 / 起止年 / 中心点
      2. 按 `placeCode` upsert 进 `pb_place`（`REBUILD_COLUMNS`，不动 `source`）
      3. 把「**本次没出现在 pb_photo 里**」的行 `photoCount` 置 0
         —— 不删行（手工地点/历史地点要留着），但缓存值必须归零，
            否则地图上会有一个「0 张照片却显示 300 张」的幽灵点

    返回
    ----
    dict —— {total, groups, created, updated, zeroed, unnamed}
      total   : pb_photo 里 placeName 非空且未删的照片数
      groups  : 聚合出的地点数（= 字典表应有多少个有效地点）
      created / updated : 本次 upsert 的新建 / 更新行数
      zeroed  : 被归零的行数（pb_photo 里已不再出现）
      unnamed : placeName 为空因而**不进字典**的照片数（截图类；
                它们不是"某个地点"，硬塞一个"未知"地点只会污染地图）

    ⚠️ 为什么是**全量**而不是增量
    --------------------------
      增量必须挂在扫描链路上（每写 N 张照片就更新对应地点行）。
      `pb_photo` 的行会被更新（重扫、EXIF 修正、去重标记），
      增量维护要覆盖所有这些路径才不漂 —— 而漏掉任何一条都是
      「照片数慢慢对不上，且没人知道是从哪一次开始错的」。
      全量复算在 10 万张规模下是一条 `GROUP BY`（实测 < 150ms），
      由用户显式点「刷新地点」触发，代价完全可以接受。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)                  # DR-10：必须显式才切库
    db = sqliteCommon.dbHandle()
    if not query.tableExists("pb_place"):
        raise RuntimeError(
            "pb_place 表不存在 —— 这是步骤 9 新增的表，升级后请先跑一次 "
            "tools\\build_db.py 补建（它只建缺的表，不动已有数据）")

    total = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo"
        " WHERE delFlag = %s AND placeName IS NOT NULL AND placeName <> ''",
        (comGD.DEL_FLAG_NO,), default=0) or 0)
    unnamed = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo"
        " WHERE delFlag = %s AND (placeName IS NULL OR placeName = '')",
        (comGD.DEL_FLAG_NO,), default=0) or 0)

    groups = query.selectList(
        "SELECT p.placeName AS placeName,"
        " COUNT(*) AS photoCount,"
        " MIN(p.shotYear) AS firstShotYear,"
        " MAX(p.shotYear) AS lastShotYear,"
        " AVG(p.lat) AS centerLat,"
        " AVG(p.lon) AS centerLon"
        " FROM pb_photo p"
        " WHERE p.delFlag = %s AND p.placeName IS NOT NULL AND p.placeName <> ''"
        " GROUP BY p.placeName"
        " ORDER BY photoCount DESC, p.placeName ASC",
        (comGD.DEL_FLAG_NO,))

    now = misc.getTime()
    rows, seen = [], set()
    for one in groups:
        name = str(one.get("placeName") or "")
        code = makePlaceCode(name)
        if not code:
            continue
        seen.add(code)
        rows.append({
            "placeCode": code,
            "placeName": name,
            "source": SOURCE_SYSTEM,
            "photoCount": int(one.get("photoCount") or 0),
            "firstShotYear": one.get("firstShotYear"),
            "lastShotYear": one.get("lastShotYear"),
            "centerLat": one.get("centerLat"),
            "centerLon": one.get("centerLon"),
        })

    created = updated = 0
    if rows:
        # 先记下哪些 placeCode 已存在。
        # ⚠️ 为什么要先查一遍：`insertManyTableGeneral` 只回「影响行数」，
        #    分不出新建还是更新 —— 而这两个数字是要给用户看的
        #    （「本次新建 3 个地点、更新 12 个」）。地点数是几十~几百量级，
        #    这一遍额外的查可以忽略。
        existed = set()
        for one in rows:
            if sqliteCommon.query_pb_place("pb_place", placeCode=one["placeCode"],
                                           mode="light"):
                existed.add(one["placeCode"])
        for one in rows:
            one["modifyYMDHMS"] = now
        # ⚠️ `source` **刻意不进 updateColumns、也不进 forceColumns**：
        #     · 不进 updateColumns -> 冲突行（已存在的地点）的 source 原样保留，
        #       手工行（source=1）不会在复算里被打回 0
        #       —— 否则「手工地点」这个概念就没了；
        #     · 不进 forceColumns -> 新建行走 INSERT 列清单，source 取字典里的
        #       0（系统聚合）。而 forceColumns 的语义是「缺该键的行按 NULL 写入」，
        #       把 source 塞进去会让新建行的 source 变成 NULL 而不是 0。
        #    「已存在的行不能被覆盖」这件事，靠 updateColumns 就够，别动 forceColumns。
        #
        # ⚠️ `insertManyTableGeneral` 返回的是 **(影响行数, 实际列名元组)**，
        #    不是裸 int —— 直接拿它比 `< 0` 会 TypeError。
        rtn = sqliteCommon.insertManyTableGeneral(
            "pb_place", rows, conflictColumns=("placeCode",),
            updateColumns=REBUILD_COLUMNS + ("modifyYMDHMS",),
            fillStandard=True,
            # 这四列必须强制：整批都为 NULL 的列会被 normalizeDataSet 丢掉，
            # 于是「某地点的照片全丢了 GPS」时旧的中心点会永久残留，
            # 而库里看不出任何异常（与 shotYear 那个静默脏数据同类）。
            forceColumns=("firstShotYear", "lastShotYear",
                          "centerLat", "centerLon"))
        affected = rtn[0] if isinstance(rtn, tuple) else int(rtn or 0)
        if affected < 0:
            raise RuntimeError("rebuildPlaces 写 pb_place 失败: %s" % db.lastErrMsg)
        created = len(rows) - len(existed)
        updated = len(existed)

    # 归零：pb_photo 里已不再出现的地点行（**不删行**，只把缓存值归零）
    zeroed = 0
    stale = query.selectList(
        "SELECT g.placeCode AS placeCode FROM pb_place g"
        " WHERE g.delFlag = %s AND COALESCE(g.photoCount, 0) <> 0",
        (comGD.DEL_FLAG_NO,))
    for one in stale:
        code = str(one.get("placeCode") or "")
        if code and code not in seen:
            # ⚠️ updateTableGeneral 出错时**返回 0 而不是负数**（见它的实现），
            #    所以只能按「> 0 才算真改了」来计数；错把 0 记成"已归零"
            #    会让统计数字看着对、实际漏改，而漏改的表现是
            #    「地图上有个 0 张照片却显示 300 张的幽灵点」。
            rtn = sqliteCommon.updateTableGeneral(
                "pb_place", "placeCode = %s", (code,),
                {"photoCount": 0, "modifyYMDHMS": now})
            if rtn > 0:
                zeroed += 1

    out = {"total": total, "groups": len(rows), "created": created,
           "updated": updated, "zeroed": zeroed, "unnamed": unnamed,
           "placeCount": len(rows)}
    _LOG.info("rebuildPlaces: %s" % out)
    return out


def listPlaces(keyword: str = "", minPhotoCount: int = None,
               orderBy: str = "photoCount", desc: bool = True,
               delFlag: str = None, limitNum: int = 0, offsetNum: int = 0) -> dict:
    """按条件读地点字典（**纯读，一行都不写**）。返回 `{items, total}`。

    ⚠️ 本函数不看 `pb_photo` —— 它只读字典表。
       字典为空（还没 rebuild 过）时返回空集，由调用方决定要不要降级成实时聚合。
    """
    if not query.tableExists("pb_place"):
        return {"items": [], "total": 0, "missingTable": True}

    where = ["g.delFlag = %s"]
    values = [comGD.DEL_FLAG_NO if delFlag is None else str(delFlag)]
    if delFlag == "*":
        where, values = ["1 = 1"], []
    if keyword:
        where.append("g.placeName LIKE %s")
        values.append("%%%s%%" % str(keyword).strip())
    if minPhotoCount is not None:
        where.append("COALESCE(g.photoCount, 0) >= %s")
        values.append(int(minPhotoCount))

    total = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_place g WHERE " + " AND ".join(where),
        tuple(values), default=0) or 0)

    primary = PLACE_SORT_SQL.get(str(orderBy or "photoCount"),
                                 PLACE_SORT_SQL["photoCount"])
    # ⚠️ 方向只作用于**主排序键**；次级键固定 ASC（见 PLACE_SORT_SQL 的说明）。
    #    这样「按张数降序」时同名次的地点仍按名字升序，顺序确定且分页稳定。
    tieBreak = ", ".join("g.%s ASC" % col for col in PLACE_TIE_BREAK_COLUMNS)
    orderSql = "g.%s %s, %s" % (primary, "DESC" if desc else "ASC", tieBreak)
    sql = ("SELECT g.placeCode AS placeCode, g.placeName AS placeName,"
           " g.source AS source, g.photoCount AS photoCount,"
           " g.firstShotYear AS firstShotYear, g.lastShotYear AS lastShotYear,"
           " g.centerLat AS centerLat, g.centerLon AS centerLon,"
           " g.modifyYMDHMS AS modifyYMDHMS"
           " FROM pb_place g WHERE " + " AND ".join(where) +
           " ORDER BY " + orderSql)
    if limitNum and limitNum > 0:
        sql += " LIMIT %s OFFSET %s"
        values = values + [int(limitNum), int(offsetNum or 0)]
    items = query.selectList(sql, tuple(values))
    return {"items": items, "total": total, "missingTable": False}


def countPlaces(delFlag: str = None) -> int:
    """字典里的地点数（`/api/overview` 之类用）。表不存在时返回 0。"""
    if not query.tableExists("pb_place"):
        return 0
    flag = comGD.DEL_FLAG_NO if delFlag is None else str(delFlag)
    where = "1 = 1" if flag == "*" else "delFlag = %s"
    values = () if flag == "*" else (flag,)
    return int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_place WHERE " + where,
        values, default=0) or 0)


#: 给 `GET /api/places?live=1` 用的实时聚合（字典表缺失或未 rebuild 时的降级路径）。
#: ⚠️ 这条才是"慢"的那条（全表 GROUP BY）—— 只在必要时走，响应用 `source` 标明。
def liveAggregatePlaces(limitNum: int = 200, keyword: str = "",
                        orderBy: str = "photoCount", desc: bool = True) -> list:
    """直接从 `pb_photo` 现算地点分组（**降级路径**）。

    ⚠️ 排序口径必须与字典路径**完全一致**（同一张 PLACE_SORT_SQL +
       同一套固定次级键）：否则前端传 `orderBy=placeName` 时，
       降级模式下会拿到另一种顺序 —— 用户看到的是「刷新了一下，
       列表顺序变了」，而两条路径的数据其实一样。
    """
    where = ["p.delFlag = %s", "p.placeName IS NOT NULL", "p.placeName <> ''"]
    values = [comGD.DEL_FLAG_NO]
    if keyword:
        where.append("p.placeName LIKE %s")
        values.append("%%%s%%" % str(keyword).strip())
    primary = PLACE_SORT_SQL.get(str(orderBy or "photoCount"),
                                 PLACE_SORT_SQL["photoCount"])
    # 次级键：实时聚合**只有 placeName 可用**（placeCode 是现推的，
    # 没进 SELECT/GROUP BY，SQLite 里排不了）。所以次级键就是名字本身，
    # 且只在主键不是 placeName 时才追加 —— 否则会出现
    # `placeName DESC, placeName ASC` 这种自相矛盾的组合。
    orderSql = "p.placeName %s" % ("DESC" if desc else "ASC")
    if primary != "placeName":
        orderSql = "%s %s, placeName ASC" % (primary, "DESC" if desc else "ASC")
    sql = ("SELECT p.placeName AS placeName, COUNT(*) AS photoCount,"
           " MIN(p.shotYear) AS firstShotYear, MAX(p.shotYear) AS lastShotYear,"
           " AVG(p.lat) AS centerLat, AVG(p.lon) AS centerLon"
           " FROM pb_photo p WHERE " + " AND ".join(where) +
           " GROUP BY p.placeName ORDER BY " + orderSql)
    if limitNum and limitNum > 0:
        sql += " LIMIT %s"
        values.append(int(limitNum))
    return query.selectList(sql, tuple(values))


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("placeStore.py _VERSION:", _VERSION)
    print("PLACE_CODE_PREFIX     :", PLACE_CODE_PREFIX)
    print("库                    :", sqliteCommon.dbFilePath() or "(未连接)")
    for _name in ("北京", "北京市", "", "San Francisco", "上海"):
        print("   makePlaceCode(%-16r) = %r" % (_name, makePlaceCode(_name)))
