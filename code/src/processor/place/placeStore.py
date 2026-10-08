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
#   * **`nameZh` 不在派生集合里**（DR-28）：它是**显示名**，来自外部地理数据集
#     或用户手填，`rebuildPlaces()` 绝不能碰。详见 REBUILD_COLUMNS 处的警告。
#
# ⚠️ R4b（DR-30/DR-32）：聚合键 = 「目录名优先」
# --------------------------------------------
#   `rebuildPlaces()` 的聚合键从 `pb_photo.placeName` 变成了
#   `COALESCE(NULLIF(placeNameDir,''), placeName)`（见 `placeKeySql()`）。
#   原因：`placeName` 只有 GPS 逆地理一条来源（实测全库 **65 张**），
#   而目录名线索覆盖 **598 张（9 倍）** —— 仍按 placeName 聚合的话，
#   那 598 张**永远进不了地点字典**（DR-30）。
#   连带变化**缺一不可**：筛选入口 `resolvePlaceFilter()` 与降级路径
#   `liveAggregatePlaces()` 也必须用**同一条键**。否则会出现
#   「地点列表说 114 张、点进去 0 张」这种自相矛盾 —— 接口 200、
#   两处代码各自自洽、没有任何报错，只是合不上。
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

_VERSION = "20261008"

_LOG = misc.setLogNew("placeStore", "placestore.log")

#: 地点编码前缀（与 personCode 的 `UI_`/`CS_`/`VC_` 同一套「前缀可读」约定）
PLACE_CODE_PREFIX: str = "PL_"

#: `source` 取值
SOURCE_SYSTEM: int = 0        # 系统聚合（rebuildPlaces 产出）
SOURCE_MANUAL: int = 1        # 手工建档（步骤 12；本步不产生）

#: upsert 时会**被复算覆盖**的列。
#: ⚠️ 刻意不含 `source`：那是「这一行当初怎么来的」，不是派生值，
#:   复算绝不能把手工行的 source 打回 0（否则「手工地点」这个概念就没了）。
#:
#: ⚠️⚠️ **`placeNameDir` 也不进这个元组**（DR-32，步骤 R4b）
#: ------------------------------------------------------
#:   它看着像派生值（就在 `pb_photo` 里、`rebuildPlaces()` 一读就有），
#:   其实是**非派生列**：由 `dirNamePlace.scanDirNames()` 只填空地填进去，
#:   来源是目录名（磁盘布局），**不是**从别的列算出来的。
#:   放进来会怎样：`pb_place` 根本没有这一列 —— 万一将来加了，
#:   复算就会用「上一轮填的目录名」覆盖掉用户刚改过的值，而且不报错。
#:   与 `nameZh` 是同一条纪律：**只填 NULL/空的列，绝不覆盖已有值**。
#:
#: ⚠️⚠️⚠️ **`nameZh` 绝不能进这个元组**（DR-28，步骤 R4a）
#: ------------------------------------------------------
#:   上面每一个成员都能从 `pb_photo` 100% 重算。`nameZh` **不能**——
#:   它来自两处 `pb_photo` 里根本没有的东西：
#:     ① 外部离线地理数据集（`placeNameZh.zhNameOf()` 的 point-in-polygon）；
#:     ② 用户手填（「外婆家」这种不是行政区名的地点）。
#:   一旦把它写进这里，后果是**每轮 `rebuildPlaces()` 都把中文冲回英文**，
#:   而且**不报错**：upsert 成功、张数对、接口 200、字段在，只有值变了。
#:   这是最难发现的一类退化（界面出问题时数据层看起来完全健康），
#:   所以这条警告写在常量旁边，而不是只写在文档里。
#:   补中文名走 `placeNameZh.rebuildNameZh()`，它**只填 NULL、绝不覆盖**。
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


#: **地点聚合键**（R4b · DR-32）：目录名优先，没有目录名时退回 GPS 逆地理的 `placeName`。
#:
#: 为什么要有这一条（DR-30/DR-32）
#: -----------------------------
#:   `pb_photo.placeName` 只有 GPS 逆地理一条来源，实测正式库 2137 张里只有 65 张
#:   有值；而目录名（`placeNameDir`）覆盖 598 张。地点字典若仍按 `placeName` 聚合，
#:   目录名那 598 张**永远进不了地点字典**。
#:
#: ⚠️ 派生 `placeCode` 的基础从「GPS 的 placeName」变成了「这一条聚合键」
#:    ⇒ 目录名（`relPath` 的一部分）**会被用户改动**：改一次名就产生一批新
#:    `placeCode`、旧行 `photoCount` 归零（DR-32 的连带风险）。旧行只归零不删除、
#:    手工行永不归零、孤儿 `nameZh` 要报告 —— 三条缺一不可，见 rebuildPlaces。
#:
#: ⚠️⚠️ `placeKeySql()`（SQL 口径）与 `placeKeyOf()`（Python 口径）
#:    **必须逐字等价**，否则「字典里显示 598 张、点进去筛出 0 张」——
#:    接口 200、结构齐全、两条路径各自自洽，就是合不上。
#:    这与 `makePlaceCode` 只留一套派生规则是同一个理由。
#: ⚠️ `NULLIF(x, '')` 把**空串**归一成 NULL（空串不是地点），`COALESCE` 再退回
#:    `placeName`。用 `COALESCE` 而不是 `||` 拼接：拼接会把「有目录名但 placeName
#:    为空」变成 NULL 之外的东西，且 '' 与 NULL 的差别会渗进 GROUP BY。
#: ⚠️ 这个模板里有**两个** `%s`（同一个表别名写两次）。别自己 `%` —— 用
#:    `placeKeySql(alias)`，两次填同一个值这件事交给它做，省得写出
#:    `COALESCE(NULLIF(p.x,''), q.y)` 这种不报错但语义错的 SQL。
PLACE_KEY_SQL: str = "COALESCE(NULLIF(%s.placeNameDir, ''), %s.placeName)"


def placeKeySql(alias: str = "p") -> str:
    """聚合键的 SQL 片段（把两个别名占位一次填好）。"""
    name = str(alias or "p")
    return PLACE_KEY_SQL % (name, name)


def placeKeyOf(placeName: str, placeNameDir: str) -> str:
    """Python 口径的聚合键（`PLACE_KEY_SQL` 的等价物）：目录名优先，空则退回 placeName。

    用途：**逐行**的场景（照片列表把每张照片的地点在应用层算出来）。
    SQL 口径的用途：聚合（`GROUP BY`）与筛选（`WHERE ... = %s`）。
    两处必须给同一个答案 —— 见 PLACE_KEY_SQL 的警告。
    """
    text = str(placeNameDir or "").strip()
    if text:
        return text
    return str(placeName or "").strip()


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
     1. 按**聚合键**聚合出 张数 / 起止年 / 中心点
        ⚠️ 聚合键 = `COALESCE(NULLIF(placeNameDir,''), placeName)`（**目录名优先**，DR-32）
           —— 改前是 `GROUP BY placeName`，那让「有目录名线索但没 GPS」的
           598 张照片**永远进不了地点字典**（DR-30）。
     2. 按 `placeCode`（**由聚合键派生**）upsert 进 `pb_place`
        （`REBUILD_COLUMNS`，不动 `source`、不碰 `nameZh`）
     3. 把「**本次没出现在 pb_photo 里**」的行 `photoCount` 置 0
        —— 不删行（手工地点/历史地点要留着），但缓存值必须归零，
           否则地图上会有一个「0 张照片却显示 300 张」的幽灵点
     4. 报出**孤儿 `nameZh`**（`photoCount=0` 但 `nameZh` 非空）

    返回
    ----
    dict —— {total, groups, created, updated, zeroed, unnamed, placeCount,
             orphans, orphanCount, manualZeroSkipped}
      total   : 聚合键非空且未删的照片数（= 进字典的照片数）
      groups  : 聚合出的地点数（= 字典表应有多少个有效地点）
      created / updated : 本次 upsert 的新建 / 更新行数
      zeroed  : 被归零的行数（pb_photo 里已不再出现的**系统**行）
      manualZeroSkipped : 本次**没出现**但因是手工行（`source=1`）而**拒绝归零**的行数
      unnamed : 聚合键为空因而**不进字典**的照片数（人名目录 / 截图类；
                它们不是"某个地点"，硬塞一个"未知"地点只会污染地图）
      orphans : 孤儿 `nameZh` 行（见下）

    ⚠️ 目录名是**用户的输入**，所以这个函数必须做三件"防静默丢数据"的事（DR-32）
    ---------------------------------------------------------------------
      ① **旧行只归零、不删除** —— 改一次目录名就换一批 `placeCode`，
         旧行被归零但留着（它可能还挂着用户手工填的 memo/nameZh）；
      ② **手工行（`source=1`）永不归零** —— 手工地点是用户的劳动成果，
         「照片搬走了」不等于「这个地点该消失」（与 DR-19「只停用不删除」同理）；
      ③ **孤儿 `nameZh` 必须报出来** —— 手工填过的中文名会因为一次改名而
         变成孤儿行（它挂的 `placeCode` 再也不会被认领）。**静默丢掉用户
         填过的名字**是这一步最不能接受的失败方式，所以宁可多一行输出。

    ⚠️ 归零的范围包含**软删行**（与 DR-33 有关）
    ----------------------------------------
      `fix_placeholder_geo` 把「加纳」那条幽灵地点**软删**（`delFlag='1'`）之后，
      它的 `photoCount=26` 还在 —— 而旧的归零循环只扫 `delFlag='0'`，
      于是重跑多少次 `rebuildPlaces()` 都清不掉（DR-33 承诺的「重跑即可清零」
      在旧实现下根本做不到）。现在按 `delFlag='*'` 扫，但**只改 photoCount**，
      绝不动 `delFlag`（软删是用户的决定，复算没有资格复活它）。

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

    key = placeKeySql("p")
    # DR-42：pb_place 的 firstShotYear / lastShotYear 是**年代跨度缓存**，
    # 必须与年代桶、照片列表同源（人工修正优先）—— 否则地点卡片上的
    # 「1938-2024」与点进去看到的照片年份互相矛盾，而两处各自都对。
    effectiveYear = comGD.sqlEffectiveShotYear("p")
    total = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p"
        " WHERE p.delFlag = %s AND " + key + " IS NOT NULL AND " + key + " <> ''",
        (comGD.DEL_FLAG_NO,), default=0) or 0)
    unnamed = int(query.selectValue(
        "SELECT COUNT(*) AS rowNum FROM pb_photo p"
        " WHERE p.delFlag = %s AND (" + key + " IS NULL OR " + key + " = '')",
        (comGD.DEL_FLAG_NO,), default=0) or 0)

    groups = query.selectList(
        "SELECT " + key + " AS placeKey,"
        " COUNT(*) AS photoCount,"
        " MIN(" + effectiveYear + ") AS firstShotYear,"
        " MAX(" + effectiveYear + ") AS lastShotYear,"
        " AVG(p.lat) AS centerLat,"
        " AVG(p.lon) AS centerLon"
        " FROM pb_photo p"
        " WHERE p.delFlag = %s AND " + key + " IS NOT NULL AND " + key + " <> ''"
        # GROUP BY / ORDER BY 写**表达式本身**而不是 `AS placeKey` 这个别名：
        # 理由与 liveAggregatePlaces 那处注释相同（别名的解析优先级 + 列名白名单校验）。
        " GROUP BY " + key +
        " ORDER BY photoCount DESC, " + key + " ASC",
        (comGD.DEL_FLAG_NO,))

    now = misc.getTime()
    rows, seen = [], set()
    for one in groups:
        # ⚠️ 这里取的是**聚合键**（`placeKey`），不是 `placeName`。
        #    写成 placeName 的后果是「聚合按新键、落库按旧键」——
        #    张数与被聚合的键对不上，而每一行看起来都是合法地点名。
        name = str(one.get("placeKey") or "")
        code = makePlaceCode(name)
        if not code:
            continue
        seen.add(code)
        rows.append({
            "placeCode": code,
            "placeName": name,
            "source": SOURCE_SYSTEM,
            "photoCount": int(one.get("photoCount") or 0),
            # ⚠️ 目录名地点**没有坐标**（DR-34）：那 598 张照片的 `lat/lon` 全为 NULL，
            #    于是 `AVG` 给的是 NULL —— 这是**正确**结果，别把 NULL 兜成 0
            #    （0,0 正好是「几内亚湾」那个占位坐标，等于把地点搬到海里）。
            #    `forceColumns` 里带上这两列，正是为了让 NULL 真的写进去。
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
    #
    # ⚠️ 四个容易写错的地方（每一个都会静默留下幽灵点或丢掉用户数据）
    #    ① **全部行都扫**（没有 delFlag 过滤）：DR-33 的「加纳」幽灵是**软删行**，
    #       只扫 delFlag='0' 的话它那 26 张永远清不掉 —— 而 DR-33 承诺
    #       「重跑 rebuild 即可清零」。归零只写 photoCount 一列，
    #       `delFlag` 不进 dataSet，软删状态一个字节都不动；
    #       不加过滤也顺手盖住了「delFlag 为 NULL 的异常行」这种边角。
    #    ② **手工行（source=1）永不归零**：手工地点是用户的劳动成果，
    #       「照片搬走了」不等于「这个地点该消失」（DR-19 同理）。
    #       ⚠️ 旧实现**没有这条判断**（`stale` 只筛了 photoCount<>0），
    #          所以「手工行永不归零」在那之前只是文档里的承诺。
    #    ③ 计零只能按 `> 0`：`updateTableGeneral` 出错时**返回 0 而不是负数**
    #       （见它的实现），错把 0 记成"已归零"会让统计看着对、实际漏改。
    #    ④ 归零**不做增量**：`seen` 只看本次聚合到的 `placeCode`。
    zeroed = manualZeroSkipped = 0
    stale = query.selectList(
        "SELECT g.placeCode AS placeCode, g.source AS source FROM pb_place g"
        " WHERE COALESCE(g.photoCount, 0) <> 0")
    for one in stale:
        code = str(one.get("placeCode") or "")
        if not code or code in seen:
            continue
        if int(one.get("source") or 0) == SOURCE_MANUAL:
            manualZeroSkipped += 1
            _LOG.info("rebuildPlaces: 手工行拒绝归零 %s（source=1，照片已搬走但行留着）",
                      code)
            continue
        rtn = sqliteCommon.updateTableGeneral(
            "pb_place", "placeCode = %s", (code,),
            {"photoCount": 0, "modifyYMDHMS": now})
        if rtn > 0:
            zeroed += 1

    # ---- 孤儿 nameZh 报告（DR-32③）----
    # 场景：用户手工给某地点填了中文名，后来把那批照片的目录改了名
    # ⇒ 旧 placeCode 再也不会被认领 ⇒ 那一行被归零，而中文名还挂在上面。
    # ⚠️ **必须让用户看见**：那是人填过的字，静默留在库里等于丢了
    #    （界面上它不再出现、也不会报错，谁都不会发现）。
    orphans = orphanNameZh()

    out = {"total": total, "groups": len(rows), "created": created,
           "updated": updated, "zeroed": zeroed, "unnamed": unnamed,
           "placeCount": len(rows), "manualZeroSkipped": manualZeroSkipped,
           "orphanCount": len(orphans), "orphans": orphans}
    _LOG.info("rebuildPlaces: %s" % {k: v for k, v in out.items() if k != "orphans"})
    if orphans:
        _LOG.warning("rebuildPlaces: 发现 %d 条孤儿 nameZh（photoCount=0 但中文名非空）"
                     "—— 改名/搬照片造成的，请人工确认是否要删或改：%s"
                     % (len(orphans), [one.get("placeName") for one in orphans[:10]]))
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
        # ⚠️ keyword 同时匹配 `placeName` 与 `nameZh`（DR-29③）。
        #   下拉框给的是**中文名**（「阿勒泰市」），而 placeName 是英文聚合键
        #   （"CN, Xinjiang Uygur Zizhiqu, Araltobe"）—— 只匹配前者的话，
        #   用户在中文下拉里看到「阿勒泰市」却搜不到任何东西。
        #   ⚠️ 这里用 LIKE 而下面 `/api/photos` 的地点筛选用 `=`：两者语义不同。
        #     · 这里是**搜索框**（用户主动打字，本就是要模糊找）
        #     · 那里是**筛选器**（值来自下拉的既定选项，模糊匹配会让用户
        #       以为筛错了 —— 「北京」与「北京市」在库里是两个不同字符串）
        where.append("(g.placeName LIKE %s OR g.nameZh LIKE %s)")
        like = "%%%s%%" % str(keyword).strip()
        values.extend([like, like])
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
           " g.nameZh AS nameZh,"
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
def resolvePlaceFilter(value: str) -> tuple:
    """地点筛选值 -> (sql 片段, 参数)。**两套都支持**（DR-29③）。

    判定顺序
    --------
     ① `value` 命中 `pb_place.nameZh`（中文名）-> 用**那一行的 placeName**
        做精确匹配。`?placeName=阿勒泰市` 与 `?placeName=CN, Xinjiang Uygur
        Zizhiqu, Araltobe` 因此返回**同一批照片**。
     ② 否则 `value` 本身当 `placeName` 精确匹配（**回退兼容旧调用**：
        收藏的 URL、老前端、脚本里的调用都还能用）
     ③ 两边都没命中 -> **原样精确匹配，返回空集，不报错**
        （「筛不出东西」与「参数非法」是两件事：后者才该 400）

    ⚠️ R4b：匹配的是**聚合键**（`COALESCE(NULLIF(placeNameDir,''), placeName)`）
    ------------------------------------------------------------------
      不跟着改会怎样（一条静默的自我矛盾）：地点列表按聚合键聚合出
      「华盛顿 114 张」，而点进去筛的是 `p.placeName = '华盛顿'` ——
      那 114 张照片的 `placeName` 全是**空**，于是筛出 **0 张**。
      接口 200、列表有行、点进去空白，没有任何一处会报错。
      ⇒ 筛选与聚合必须用**同一条键**（`placeKeySql`）。

    ⚠️ 仍然是**精确匹配**，不要改成 LIKE
    ------------------------------------
      现有注释已经说明理由：「北京」与「北京市」在库里是两个不同字符串，
      模糊匹配会让用户以为自己筛错了。中文名这一侧同理 ——
      「新疆维吾尔自治区 · 和静县」与「… · 和什托洛盖乡」LIKE 会互相命中。
    """
    key = placeKeySql("p")
    text = str(value or "").strip()
    if not text:
        return (key + " = %s", ("",))
    if not query.tableExists("pb_place"):
        return (key + " = %s", (text,))
    # ⚠️ 同一个中文名**可能对应多个不同的 placeName**：手工行被用户命名成
    #   「新疆维吾尔自治区 · 和静县」之后，它与聚合行同名中文、不同英文键。
    #   只取第一行会让「那个地点的照片少了一半」，而界面上完全看不出少了。
    #   ⚠️ 而**两行 placeName 相同**（手工 + 聚合）**不需要 IN** ——
    #   `placeName = '北京'` 本来就同时命中两行。这两个场景很容易混，
    #   判据是「去重后还剩几个不同的 placeName」，不是「有几行」。
    rows = query.selectList(
        "SELECT g.placeName AS placeName FROM pb_place g"
        " WHERE g.delFlag = %s AND g.nameZh = %s"
        " LIMIT 20", (comGD.DEL_FLAG_NO, text))
    names = []
    for one in rows:
        got = str(one.get("placeName") or "")
        if got and got not in names:
            names.append(got)
    if not names:
        return (key + " = %s", (text,))
    if len(names) == 1:
        return (key + " = %s", (names[0],))
    marks = ", ".join(["%s"] * len(names))
    return (key + " IN (%s)" % marks, tuple(names))


#: `placeName -> nameZh` 的进程内快照（`nameZhMap()` 的缓存体）。
#: ⚠️ 这是**非派生缓存**，与 photoCount 那些同类：数据真值在库里，
#:   这里只是省掉「每张照片查一次库」。
_NAMEZH_CACHE: dict = {}
_NAMEZH_CACHE_ON = False
#: 上面那份缓存是从**哪个库文件**读的。sqliteCommon.dbHandle 是进程级单例，
#: 而测试与「临时库」场景会**在同一个进程里反复切库** —— 缓存不记库文件的话，
#: 上一个库的中文名会漏进下一个库的响应，而那正好是「测试偶发挂、
#: 正式库一切正常」的典型症状。记下库路径，比记时间戳可靠。
_NAMEZH_CACHE_DB: str = ""


def nameZhMap(refresh: bool = False) -> dict:
    """`{placeName: nameZh}` 映射（**只含 nameZh 非空的行**）。进程内缓存。

    ⚠️⚠️ 为什么照片端**用这张表而不是 SQL JOIN**（这是对原设计的**有意偏离**）
    --------------------------------------------------------------------------
      原设计是 `LEFT JOIN pb_place g ON g.placeName = p.placeName`。
      在当前数据下它能跑，但有一个**静默**的正确性陷阱：
      `placeStore` 自己的注释写着「`placeName` 在同一张表里**可以有重复行**
      （手工建的 + 聚合出来的）」。一旦出现重复行，那个 JOIN 会让
      **同一张照片在列表里出现两次** —— 而 `total` 是另一条 SQL 单独算的，
      于是响应变成「total=60、items 里有重复项」，接口 200、结构齐全，
      没有一行代码会报错。
      而 JOIN 唯一买到的东西（省一次查询）在照片列表上根本不成立：
      一页 60 张 × 一次索引查 = 60 次 O(log n)，而这里是 60 次 O(1) 字典查。
      ⇒ 用映射表。**真正的约束**（pb_photo 没有 placeCode，只能按字符串
        placeName 关联）仍然成立，只是关联发生在应用层而不是 SQL 层。

    refresh
    -------
      True 时强制重读。`rebuildNameZh()` 写完库会自己调一次 ——
      同一个进程里跑完回填再查接口，必须看到新值（这是「工具跑完立刻
      在界面上验证」这条工作流的前提）。
    """
    global _NAMEZH_CACHE, _NAMEZH_CACHE_ON, _NAMEZH_CACHE_DB
    dbMark = str(sqliteCommon.dbFilePath() or "")
    if _NAMEZH_CACHE_ON and not refresh and _NAMEZH_CACHE_DB == dbMark:
        return _NAMEZH_CACHE
    cache = {}
    if query.tableExists("pb_place"):
        for one in query.selectList(
                "SELECT g.placeName AS placeName, g.nameZh AS nameZh"
                " FROM pb_place g"
                " WHERE g.delFlag = %s AND g.nameZh IS NOT NULL AND g.nameZh <> ''",
                (comGD.DEL_FLAG_NO,)):
            key = str(one.get("placeName") or "")
            if key:
                cache[key] = str(one.get("nameZh") or "")
    _NAMEZH_CACHE = cache
    _NAMEZH_CACHE_ON = True
    _NAMEZH_CACHE_DB = dbMark
    return cache


def nameZhOfPlace(placeName: str) -> str:
    """某个 `placeName` 的中文名；没有就返回 ""（调用方按「回退英文」处理）。"""
    if not placeName:
        return ""
    return nameZhMap().get(str(placeName), "")


def liveAggregatePlaces(limitNum: int = 200, keyword: str = "",
                        orderBy: str = "photoCount", desc: bool = True) -> list:
    """直接从 `pb_photo` 现算地点分组（**降级路径**）。

    ⚠️ 排序口径必须与字典路径**完全一致**（同一张 PLACE_SORT_SQL +
       同一套固定次级键）：否则前端传 `orderBy=placeName` 时，
       降级模式下会拿到另一种顺序 —— 用户看到的是「刷新了一下，
       列表顺序变了」，而两条路径的数据其实一样。

    ⚠️ R4b：这条路径也**必须**换成聚合键（DR-32④）
    --------------------------------------------
      「字典表未 rebuild 时」（表空 / `?live=1`）走的就是这条 —— 数据源是
      `pb_photo` 的 `GROUP BY`。它要是还按 `p.placeName` 聚合，
      降级模式下给的是**另一批地点名**（只有 65 张 GPS 那批），
      与 rebuild 之后的字典路径**不是同一个结果**，而两条路径都会
      返回 200 + 结构齐全的数据 —— 用户只会觉得「刷新了一下，地点少了一半」。
      ⇒ 两条路径必须用同一把键（`placeKeySql`）。
    """
    key = placeKeySql("p")
    where = ["p.delFlag = %s", key + " IS NOT NULL", key + " <> ''"]
    values = [comGD.DEL_FLAG_NO]
    if keyword:
        # ⚠️ 降级路径**只能按聚合键搜**，搜不到中文名 nameZh。
        #    原因：这条路径的数据源是 `pb_photo` 的 GROUP BY，而 nameZh
        #    存在 `pb_place` 里 —— 字典表此刻正因为「空」才走到这里。
        #    这不是缺陷而是降级的定义：降级时能少给什么就少给什么，
        #    但**不能**为了「搜索也好用」就在 GET 里顺手回填字典
        #    （那会让只读接口有副作用，见文件头第一条纪律）。
        where.append(key + " LIKE %s")
        values.append("%%%s%%" % str(keyword).strip())
    primary = PLACE_SORT_SQL.get(str(orderBy or "photoCount"),
                                 PLACE_SORT_SQL["photoCount"])
    # 次级键：实时聚合**只有聚合键本身可用**（`placeCode` 是现推的，
    # 没进 SELECT/GROUP BY，SQLite 里排不了）。所以次级键就是名字本身，
    # 且只在主键不是它时才追加 —— 否则会出现
    # `placeKey DESC, placeKey ASC` 这种自相矛盾的组合。
    # ⚠️ `orderBy=placeCode` 这条分支：字典路径按 `placeCode` 排，
    #    而实时路径**算不出 placeCode**（它由 sanitizeCodeKey 派生，
    #    SQL 里没有这个函数）。旧实现会直接拼出 `placeCode DESC` ->
    #    SQLite 报 `no such column` -> `selectList` 吞掉错误返回 **[]**
    #    （接口 200、地点列表空，最难查的一类）。现在退化成按聚合键排：
    #    次序与字典路径不完全一致，但**是确定的、且不报错**。
    # ⚠️ `GROUP BY` / `ORDER BY` 里写的是**表达式本身**，不是 `AS placeName` 那个别名。
    #    两个理由：
    #      ① `placeName` 既是**输出别名**又是**表里的真实列名**，SQLite 的解析
    #         优先级对人来说不直观 —— 写别名等于把「按聚合键分组」这件事
    #         交给一条需要查文档才能确认的规则；
    #      ② 更硬的一条：`queryCommon.checkColumns` 的**列名白名单校验**会拦下
    #         不在 schema 里的裸标识符。写 `GROUP BY placeKey` 而 SELECT 里
    #         没有 `AS placeKey` 时，它会直接抛 QuerySqlError（实测踩过）。
    if primary in ("placeName", "placeCode"):
        orderSql = key + " %s" % ("DESC" if desc else "ASC")
    else:
        orderSql = "%s %s, %s ASC" % (primary, "DESC" if desc else "ASC", key)
    # DR-42：降级路径（实时聚合）必须与字典路径同口径 —— 两条路径算出的
    # 年代跨度不一样时，症状是「切到 live 之后年份跳了」，且两边各自都跑得通。
    effectiveYear = comGD.sqlEffectiveShotYear("p")
    sql = ("SELECT " + key + " AS placeName, COUNT(*) AS photoCount,"
           " MIN(" + effectiveYear + ") AS firstShotYear,"
           " MAX(" + effectiveYear + ") AS lastShotYear,"
           " AVG(p.lat) AS centerLat, AVG(p.lon) AS centerLon"
           " FROM pb_photo p WHERE " + " AND ".join(where) +
           " GROUP BY " + key + " ORDER BY " + orderSql)
    if limitNum and limitNum > 0:
        sql += " LIMIT %s"
        values.append(int(limitNum))
    return query.selectList(sql, tuple(values))


# ============================================================
# 六、巡检用只读报告（`tools/place_cli.py --audit` 的三项）
# ============================================================
# 三个函数都**纯读**（实现全在 SQL 的 SELECT 里，一行都不写）。
# 为什么放在 placeStore 而不是 CLI 里：`pb_place` 的 SQL 只应出自这一个模块
# （见 __init__.py 的说明），CLI 只负责排版。
def orphanNameZh() -> list:
    """**孤儿 `nameZh`**：`photoCount = 0` 但 `nameZh` 非空的行。

    ⚠️ 为什么要单独报出来（DR-32③）：`nameZh` 是**人填过/算出来的显示名**，
       而 `photoCount=0` 意味着这个 `placeCode` 已经没有任何照片认领它
       —— 通常是「目录改名」或「照片搬走」造成的。这一行仍然会出现在
       `listPlaces(delFlag='*')` 里，但界面上**看不到它**（默认过滤 0 张的
       地点与否），于是用户手工填的中文名就这么静默地留在库里了。
    ⚠️ `source=1`（手工行）也一并报：手工行**不会被归零**（见 rebuildPlaces），
       所以它一般不会出现在这里；真出现了说明它当初就是 0 张。
    """
    if not query.tableExists("pb_place"):
        return []
    return query.selectList(
        "SELECT g.placeCode AS placeCode, g.placeName AS placeName,"
        " g.nameZh AS nameZh, g.source AS source, g.photoCount AS photoCount,"
        " g.modifyYMDHMS AS modifyYMDHMS"
        " FROM pb_place g"
        " WHERE COALESCE(g.photoCount, 0) = 0"
        "   AND g.nameZh IS NOT NULL AND g.nameZh <> ''"
        " ORDER BY g.recID ASC")


def duplicateNameZhRows() -> list:
    """**重名 `nameZh`** 分组（DR-36）：同一个中文名挂了多行地点。

    ⚠️ 这是 R4a 暴露的**已有**问题、不是 R4b 引入的：
       `PL_CN_Beijing_Datun`（n=8）与 `PL_CN_Beijing_Wangjing`（n=7）的
       `nameZh` **都是「北京市 · 朝阳区」** —— `placeCode` 由 GPS 的街道级
       `placeName` 派生，而 `nameZh` 来自区县级行政区划，多个街道级地点
       折叠到同一个区县名。
    ⚠️ R4b **只报告、不归并**（DR-36 明确要求：三种处理思路要等用户定）。
       擅自合并会让 `placeCode` 漂移（DR-28 警告过的幂等键漂移）。
    """
    if not query.tableExists("pb_place"):
        return []
    return query.selectList(
        "SELECT g.nameZh AS nameZh, COUNT(*) AS rowNum,"
        " SUM(COALESCE(g.photoCount, 0)) AS photoCount,"
        " GROUP_CONCAT(g.placeName, ' || ') AS placeNames"
        " FROM pb_place g"
        " WHERE g.delFlag = %s AND g.nameZh IS NOT NULL AND g.nameZh <> ''"
        " GROUP BY g.nameZh HAVING COUNT(*) > 1"
        " ORDER BY rowNum DESC, g.nameZh ASC",
        (comGD.DEL_FLAG_NO,))


def placesWithoutCenter() -> list:
    """**无中心点**的地点（`centerLat` 为 NULL）。

    ⚠️ 目录名地点**全部**落在这里（DR-34）：那 598 张照片没有 GPS，
       `AVG(lat)` 就是 NULL。这不是缺陷而是事实 —— 地图要接受
       「有地点、没坐标」并**跳过它并报告**，**不要**去猜一个坐标
       （猜出来的点会以假乱真地出现在地图上）。
    """
    if not query.tableExists("pb_place"):
        return []
    return query.selectList(
        "SELECT g.placeCode AS placeCode, g.placeName AS placeName,"
        " g.nameZh AS nameZh, g.photoCount AS photoCount,"
        " g.centerLat AS centerLat, g.centerLon AS centerLon"
        " FROM pb_place g"
        " WHERE g.delFlag = %s AND g.centerLat IS NULL"
        " ORDER BY COALESCE(g.photoCount, 0) DESC, g.placeName ASC",
        (comGD.DEL_FLAG_NO,))


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("placeStore.py _VERSION:", _VERSION)
    print("PLACE_CODE_PREFIX     :", PLACE_CODE_PREFIX)
    print("REBUILD_COLUMNS       :", REBUILD_COLUMNS,
          "（**nameZh / placeNameDir 刻意不在内**）")
    print("聚合键 SQL            :", placeKeySql("p"), "（R4b：目录名优先，DR-32）")
    print("库                    :", sqliteCommon.dbFilePath() or "(未连接)")
    for _name in ("北京", "北京市", "", "San Francisco", "上海"):
        print("   makePlaceCode(%-16r) = %r" % (_name, makePlaceCode(_name)))
    for _pair in (("", "2013.07.26 华盛顿"), ("CN, Beijing, Datun", ""),
                  ("", ""), ("CN, Beijing, Datun", "华盛顿")):
        print("   placeKeyOf(%-24r, %-20r) = %r"
              % (_pair[0], _pair[1], placeKeyOf(_pair[0], _pair[1])))
    for _value in ("阿勒泰市", "新疆维吾尔自治区 · 和静县", "北京", "不存在的地点"):
        print("   resolvePlaceFilter(%-24r) = %r" % (_value, resolvePlaceFilter(_value)))
