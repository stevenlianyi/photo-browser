#! /usr/bin/env python3
#encoding: utf-8

#Filename: review.py
#Description: photo-browser 识别纠错闭环接口（步骤 9·P1，DR-16）—— 队列 + 改判 + 合并拆分 + 撤销
#
# 十二个端点
# -------------
#   GET  /api/review/pending            待确认队列（Top-5 候选按相似度降序）
#   GET  /api/review/disputed           「我不同意」列表（按 photoCode 分组）
#   GET  /api/review/pending/count      侧栏角标 -> { pendingCount, disputedCount }（两个数）
#   PUT  /api/review/{faceCode}/assign  首次确认（人工确认归属）
#   POST /api/review/batch-assign       批量确认（同一个人一批脸）
#   POST /api/review/fix                改判（assign / unknown / stranger）
#   POST /api/review/batch-fix          同一 clusterCode 批量改判
#   POST /api/review/merge              合并两个人员
#   POST /api/review/split              从某人拆出一张脸
#   POST /api/review/undo               撤销一条 isRevertible=1 的 SPLIT / MERGE
#   GET  /api/review/log                操作历史（排障：这张脸当初怎么被认成的）
#   GET  /api/review/revertible         当前可撤销的操作列表
#
# ⚠️⚠️ 本模块**只做编排**，一套写库逻辑都不自己实现
# ---------------------------------------------------
#   「删旧 linkKey + 写新 source=1 + 刷 shotBucket + 重算原人*与*新人全部年代档
#    + 同步 faceCount + 落 pb_review_log」这套东西是步骤 6/7 已经在
#   assigner / merger / rebucket / centroid 里**反复打磨过**的实现
#   （每个函数头上都写着「为什么顺序不能颠倒」「漏了不报错只会越改越乱」）。
#   在 api 层再写一遍的结果不是「更清晰」，而是**两套规则迟早分叉**：
#   改判接口漏了刷新年代档，而 assigner 那条路径刷了——
#   于是同一个人经UI 改判和经 CLI 改判会落到不同的年代档，
#   而两边都「看起来成功」。所以这里全部转发，一行写库代码都没有。
#
# 两个队列的口径（**唯一权威在 processor/review/queue.py**）
# --------------------------------------------------------
#   待确认    = `personCode IS NULL AND isStranger=0`（queue.WHERE_PENDING）
#   我不同意  = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0`
#               （queue.WHERE_DISPUTED）
#   ⚠️ 绝不能写成 `personCode IS NULL OR isConfirmed=0`—— 那会把**全部自动归属**
#      扫进待确认队列（10 万张里4~8 万条，队列爆炸且没人会用）。
#      本模块**不重新实现**这两个 WHERE，直接调 queue.countPending /
#      countDisputed / pendingQueue / disputedList —— 让口径只有一处定义。

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
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.review import assigner as assigner                 # noqa: E402
from processor.review import merger as merger                     # noqa: E402
from processor.review import queue as reviewQueue                 # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("apiReview", "apireview.log")

router = APIRouter(tags=["review"])

#: 批量确认/批量改判的单批上限。**必须有上限**：
#: 一个簇可以有几万张脸（误导入的陌生人），一次事务里塞进去会让
#: 「点一下按钮」变成「服务卡三十秒」，而且事务日志暴涨到无法回滚。
_REVIEW_BATCH_MAX: int = 500


def _clampFaceCodes(codes) -> list:
    """批量接口的人脸编码列表：去重 + 保留顺序 + 过上限闸。"""
    out = []
    seen = set()
    for one in (codes or ()):
        code = str(one or "").strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    if len(out) > _REVIEW_BATCH_MAX:
        raise dto.ApiError(
            dto.CODE_PARAM_INVALID,
            "一次最多处理 %d 张脸，收到 %d（请按簇分批）"
            % (_REVIEW_BATCH_MAX, len(out)),
            extra={"maxBatch": _REVIEW_BATCH_MAX, "received": len(out)})
    return out


def _counts() -> dict:
    """两个队列的当前计数（写操作后一并回给前端，省一次请求）。"""
    states = reviewQueue.countStates()
    return {"pendingCount": int(states.get("pending") or 0),
            "disputedCount": int(states.get("disputed") or 0)}


# ============================================================
# 一、队列
# ============================================================

@router.get("/review/pending", summary="待确认队列（含 Top-5 候选）")
def getPending(page: int = Query(default=1, ge=1),
               size: int = Query(default=20, le=dto.MAX_PAGE_SIZE),
               topN: int = Query(default=basicSettings.MATCH_TOP_CANDIDATES, ge=1, le=20,
                                 description="每位脸带几个候选人物（默认 %d）"
                                             % basicSettings.MATCH_TOP_CANDIDATES),
               preset: str = Query(default=None,
                                   description="阈值档位（保守/激进…）；缺省用 basicSettings"),
               sortFirst: int = Query(default=0,
                                      description="1 = 全量按语义序排序后再分页"
                                                  "（正确但慢：10 万张约 40s，"
                                                  "仅供「给我最该先确认的 N 张」"),
               clusterCode: str = Query(default=None,
                                        description="只看某个簇（⚠️ 不缓存，"
                                                    "确认掉成员后编码会变）")):
    """待确认队列 = `personCode IS NULL AND isStranger=0`。

    ⚠️ **分页键是 quality DESC**（queue.pendingQueue 里那条设计约束）：
      队列「最该先确认」的主信号 similarity 是**算出来的**，
      步骤 6 明确 review 结果不落库，所以没有任何可交给 SQL 的排序键。
      用 similarity 当分页键会出现「翻页时条目在眼前挪位」——
      那是分页最忌讳的事。窗口内再按语义序排（分数高→低、同分有簇优先）。
      代价是**全局有序分页拿不到**：page 1 永远是「当前 quality 最高的 20 张」，
      而队列是工单列表、处理完会离队，所以这个代价是可以接受的。
      确实需要全局有序时传 `sortFirst=1`。
    """
    p, s = dto.clampPage(page, size, defaultSize=20)
    at = dto.offsetOf(p, s)
    total = reviewQueue.countPending()

    if clusterCode:
        members = reviewQueue.clusterMembers(str(clusterCode))
        codes = [m["faceCode"] for m in members.get("members") or []]
        items = reviewQueue.pendingQueue(limit=s, offset=at, topN=topN,
                                         preset=preset)
        items = [one for one in items if one["faceCode"] in set(codes)]
        return dto.pageBody(items, p, s, min(len(codes), total))

    items = reviewQueue.pendingQueue(limit=s, offset=at, topN=topN,
                                     preset=preset, sortFirst=bool(sortFirst))
    # Top-N 候选按相似度**降序**（验收第 5 条）。matcher.rankCandidates 已经排过，
    # 这里再排一次是为了让「顺序正确」不依赖下游实现细节。
    for one in items:
        one["topCandidates"] = sorted(
            one.get("topCandidates") or [],
            key=lambda c: (-float(c.get("similarity") or 0.0), c.get("personCode") or ""))
    return dto.pageBody(items, p, s, total)


@router.get("/review/disputed", summary="「我不同意」列表（机器自动认错的申诉入口）")
def getDisputed(page: int = Query(default=1, ge=1),
                size: int = Query(default=20, le=dto.MAX_PAGE_SIZE),
                groupByPhoto: int = Query(default=1,
                                          description="1 按 photoCode 分组 / 0 平铺"),
                photoCode: str = Query(default=None, description="只看某张照片")):
    """「我不同意」= `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0`。

    ⚠️ **自动归属的脸就在这里**（DR-16②）。它们**不在**待确认队列里：
      队列只收personCode 为空的。这一条是整个纠错闭环的入口 ——
      用户浏览时发现「这张照片里那张脸认错了」，在这里一键改判。

    两套「总数」——**前端画分页器必须用 photoGroupTotal，不能用 total**
    ------------------------------------------------------------------
      `page`/`size` 作用在**人脸行**上（一张照片里可能认错好几个人，
      先按行分页才能把内存钉住 —— queue.disputedList 的同一个理由）。所以：

        total            = **人脸行数**（= queue.countDisputed()，验收第 11 条的口径）
        photoGroupTotal  = **分组后的照片组总数**（`COUNT(DISTINCT photoCode)`）
        items 数量        = 本页拿到的组数（<= size，且可能远小于 size）
        groupCount       = 本页这些分了几组（= len(items)）
        faceCountInPage  = 本页覆盖的人脸行数（groupByPhoto=0 时 = len(items)）

      ⚠️ 为什么必须**两个都给**：
         · `total` 是**数据口径**（验收第 11 条要拿它跟 SQL 逐行核对），
           但它不是「有多少页」 —— 拿它画分页器会翻进空页
           （37 张脸可能只分布在 5 张照片里，`total=37` 而实际只有 1 页）；
         · `photoGroupTotal` 是**分页口径**，前端拿它算总页数；
         · 两个都缺一个，UI 要么显示错的总数，要么翻出空页。
      代价：多一次 `COUNT(DISTINCT photoCode)`。走
      `idx_pb_face_personCode_isConfirmed` 这个部分索引
      （`WHERE isConfirmed=0 AND isStranger=0`），10 万张量级是几十毫秒。
    """
    p, s = dto.clampPage(page, size, defaultSize=20)
    at = dto.offsetOf(p, s)
    total = reviewQueue.countDisputed()
    groupTotal = _disputedGroupTotal(photoCode)

    items = reviewQueue.disputedList(limit=s, offset=at,
                                     groupByPhoto=bool(groupByPhoto))
    if photoCode:
        if groupByPhoto:
            items = [g for g in items if g.get("photoCode") == str(photoCode)]
        else:
            items = [f for f in items if f.get("photoCode") == str(photoCode)]
    faceCount = sum(int(g.get("faceCount") or 0) for g in items) if groupByPhoto else len(items)
    return {"ok": True, "scope": "groups" if groupByPhoto else "faces",
            "page": p, "size": s,
            "total": total,                       # 脸的行数（**数据口径**）
            "photoGroupTotal": groupTotal,        # 照片组数（**分页口径**）
            "items": items,
            "hasMore": p * s < total,
            "groupCount": len(items),
            "faceCountInPage": faceCount,
            "note": "分页器请用 photoGroupTotal 算总页数；total 是人脸行数，"
                    "拿它画分页器会翻进空页（37 张脸可能只在 5 张照片里）"}


def _disputedGroupTotal(photoCode: str = None) -> int:
    """「我不同意」涉及多少张**照片**（= `COUNT(DISTINCT photoCode)`）。

    ⚠️ 口径必须与 `queue.WHERE_DISPUTED` **逐字一致**，否则分页器算出的
       总页数会与 items 的实际分布对不上 —— 而那类错不会报错，只会
       让最后一页是空的（或者漏掉一页）。
       所以这里复用 queue 导出的 WHERE 常量，不自己再写一遍条件。
    """
    where = reviewQueue.WHERE_DISPUTED
    values = [0, 0, comGD.DEL_FLAG_NO]
    if photoCode:
        where += " AND photoCode = %s"
        values.append(str(photoCode))
    return int(query.selectValue(
        "SELECT COUNT(DISTINCT photoCode) AS rowNum FROM pb_face WHERE " + where,
        tuple(values)) or 0)


@router.get("/review/pending/count", summary="侧栏角标（pendingCount / disputedCount 两个数）")
def getPendingCount() -> dict:
    """**必须返回两个数**（验收第 14 条）。

    为什么不能只给一个「待处理」总数
    ------------------------------
      待确认与「我不同意」是**两种完全不同的动作**：前者是「这张脸我不认识」，
      后者是「这张脸我认识，机器认错人了」。用户在前者上要做的是「认人/新建」，
      在后者上要做的是「改判/否决」。合成一个数字会让 UI 只能显示
      「待处理 251」—— 用户点进去发现两件不同的事，角标就失去了意义。
    ⚠️ 这里的 pendingCount 是**人脸条数**，与pb_scan_job.pendingCount
       （疑似移动/重命名的**张数**，DR-11）**不是同一个东西**，别混用。
    """
    states = reviewQueue.countStates()
    return {"ok": True,
            "pendingCount": int(states.get("pending") or 0),
            "disputedCount": int(states.get("disputed") or 0),
            "confirmedCount": int(states.get("confirmed") or 0),
            "strangerCount": int(states.get("stranger") or 0),
            "movedPendingPhotos": _movedPending(),
            "warning": "pendingCount=待确认人脸；disputedCount=「我不同意」；"
                       "movedPendingPhotos=疑似移动/重命名的**张数**（另一个口径）"}


def _movedPending() -> int:
    total = 0
    for row in sqliteCommon.query_pb_scan_job("pb_scan_job", mode="light"):
        total += int(row.get("pendingCount") or 0)
    return total


@router.get("/review/clusters", summary="待确认集合的簇概览（批量改判的入口）")
def getClusters(page: int = Query(default=1, ge=1),
                size: int = Query(default=20, le=dto.MAX_PAGE_SIZE),
                minSize: int = Query(default=2, ge=1,
                                     description="只看规模 >= 该值的簇")):
    """簇概览（按簇大小降序）。

    ⚠️ **clusterCode 不许跨会话缓存**：它是「簇内 faceCode 排序后 sha256[:12]」
       的内容指纹 —— 确认掉一个成员，剩下的脸可能分裂成**两个新簇**，
       编码随之改变。所以任何按簇操作都要**先查后写**，写完刷新簇列表。
    """
    p, s = dto.clampPage(page, size, defaultSize=20)
    items = reviewQueue.clusterOverview(limit=0, minSize=minSize)
    start = (p - 1) * s
    return dto.pageBody(items[start:start + s], p, s, len(items))


@router.get("/review/clusters/{clusterCode}", summary="某个簇的活成员（现查，不缓存）")
def getClusterMembers(clusterCode: str,
                      size: int = Query(default=60, le=dto.MAX_PAGE_SIZE)):
    """取簇成员。**以库为准**：成员随确认/否决实时变化。"""
    return reviewQueue.clusterMembers(clusterCode, limit=size)


# ============================================================
# 二、确认
# ============================================================

@router.put("/review/{faceCode}/assign", summary="首次确认（人工确认这张脸属于某人）")
def assignFace(faceCode: str, body: dto.AssignBody) -> dict:
    """`assigner.confirm()`：isConfirmed=1、link source=1、**立即重算质心**。

    为什么要 PUT 而不是 POST：这一步的语义是「让这张脸指向某人」，
    重复 PUT 同一个人是**幂等**的（assigner._setBelong 有幂等分支，
    连 shotBucket 都会按当前生日刷一遍）。用 PUT 是为了让前端敢重试。

    ⚠️ `personCode` 必须先在 pb_person 里存在（弱外键，assigner 自己守）。
       不存在时返回 400 而不是 404：请求格式没错，是**引用**错了目标。
    """
    code = str(faceCode or "").strip()
    if not code:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "faceCode 不能为空")
    try:
        info = assigner.confirm(code, str(body.personCode or "").strip())
    except assigner.AssignerError as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    person = browse.personRow(str(body.personCode or ""))
    stats = browse.personStatsOf([str(body.personCode or "")]).get(
        str(body.personCode or ""), {})
    return dto.okBody(
        faceCode=info.get("faceCode"), photoCode=info.get("photoCode"),
        personCode=info.get("toPerson"), bucketKey=info.get("bucketKey") or None,
        changed=bool(info.get("changed")), logCode=info.get("logCode") or None,
        bucketRebucketed=bool(info.get("bucketRebucketed")),
        # 质心即时重算的结果（纪律①：确认完必须立刻生效，否则用户会得出
        # 「确认了也没用」的结论，然后不再确认 —— 整个机制就靠这条即时反馈活着）
        centroidBuckets=[one.get("bucketKey") for one in (info.get("centroids") or [])],
        person=dict(displayName=person.get("displayName"),
                    photoCount=int(stats.get("photoCount") or 0),
                    faceCount=int(stats.get("faceCount") or 0),
                    confirmedFaceCount=int(stats.get("confirmedFaceCount") or 0)),
        **_counts())


@router.post("/review/batch-assign", summary="批量确认（同一个人一批脸）")
def batchAssign(body: dto.BatchAssignBody) -> dict:
    """`assigner.confirmPerson()`：一个事务 + 一次重算 + **一条** BATCH_ASSIGN 日志。

    ⚠️ 日志是**一条**而不是 N 条：一次点击解决 N 张，日志里必须看得出
       「这是一批」；逐张落 20 条会把撤销按钮的候选集与排障都淹在噪音里。
    """
    codes = _clampFaceCodes(body.faceCodes)
    if not codes:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "faceCodes 不能为空")
    try:
        info = assigner.confirmPerson(str(body.personCode or "").strip(), codes)
    except assigner.AssignerError as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    stats = browse.personStatsOf([str(body.personCode or "")]).get(
        str(body.personCode or ""), {})
    return dto.okBody(
        personCode=info.get("personCode"),
        assigned=int(info.get("assigned") or 0),
        failed=info.get("failed") or [],
        centroidBuckets=[one.get("bucketKey") for one in (info.get("centroids") or [])],
        person=dict(photoCount=int(stats.get("photoCount") or 0),
                    faceCount=int(stats.get("faceCount") or 0),
                    confirmedFaceCount=int(stats.get("confirmedFaceCount") or 0)),
        **_counts())


# ============================================================
# 三、改判（DR-16②）
# ============================================================

@router.post("/review/fix", summary="改判（assign 改到某人 / unknown 置未知 / stranger 标陌生人）")
def fixFace(body: dto.FixBody) -> dict:
    """**统一改判入口**。单张走 `assigner.fix()`，多张走 `assigner.batchFix()`。

    三个动作与四态的对应（与 assigner.fix 的表一一对齐）
    -----------------------------------------------
      assign   -> personCode=新人, isConfirmed=1（进入人工确认）
      unknown  -> personCode=NULL, isConfirmed=0（**回待确认队列**）
      stranger -> personCode=NULL, isStranger=1（**永久排除**，两个队列都不进、也不聚类）

    ⚠️ **原人与新人都会被重算**（assigner._recomputePersons 收了两个人）。
      只重算一边会让原人的质心永远停在「包含这张已经不属于他的脸」的旧值上，
      而**不报错** —— 只表现为「越用越不准」。这是最容易漏的一处。
    ⚠️ 响应里的 `logCode` 为空表示**这次调用什么都没改**（幂等空操作，
      例如对一张本就未归属的脸 `unknown`）。那是正确行为：
      没改库就不该写日志（纪律④要防的是**静默改库**，不是强制记账）。
    """
    action = str(body.action or "").strip().lower()
    if action not in assigner.FIX_ACTIONS:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "action 只支持 %s，收到 %r"
                           % (sorted(assigner.FIX_ACTIONS.keys()), body.action))
    codes = _clampFaceCodes(list(body.faceCodes) +
                            ([body.faceCode] if body.faceCode else []))
    if not codes:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "必须给faceCode 或 faceCodes 之一")
    if action == "assign" and not str(body.personCode or "").strip():
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "action=assign 必须给 personCode")

    target = str(body.personCode or "").strip() or None
    if len(codes) == 1:
        try:
            info = assigner.fix(codes[0], action, target, reason=body.reason or "")
        except assigner.AssignerError as e:
            raise dto.ApiError(_codeOf(str(e)), str(e))
        return dto.okBody(action=action, single=True,
                          faceCode=info.get("faceCode"),
                          photoCode=info.get("photoCode"),
                          fromPersonCode=info.get("fromPerson") or None,
                          toPersonCode=info.get("toPerson") or None,
                          bucketKey=info.get("bucketKey") or None,
                          changed=bool(info.get("changed")),
                          droppedLinks=int(info.get("droppedLinks") or 0),
                          bucketRebucketed=bool(info.get("bucketRebucketed")),
                          logCode=info.get("logCode") or None,
                          reason=info.get("reason") or None,
                          # ⚠️ 这里原来**漏了** `_counts()`（批量分支有、单张分支没有），
                          #    后果是 UI 改判完一张脸后侧栏两个角标**不更新** ——
                          #    而设计稿 §4.6 要求「待确认 / 我不同意」两个数实时跟着变。
                          #    前端要么多打一次 /pending/count，要么像现在这样统一契约；
                          #    少一次往返、且不必让前端记得哪个端点带、哪个不带。
                          **_counts(),
                          **withCentroidOf(info, target, info.get("fromPerson")))

    try:
        info = assigner.batchFix(codes, action, target, reason=body.reason or "")
    except assigner.AssignerError as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    return dto.okBody(action=action, single=False, requested=len(codes),
                      fixed=int(info.get("fixed") or 0),
                      failed=info.get("failed") or [],
                      centroidBuckets=[one.get("bucketKey")
                                       for one in (info.get("centroids") or [])],
                      **_counts())


def withCentroidOf(info: dict, newPerson: str, oldPerson: str) -> dict:
    """改判响应里的「**双方**质心」信息（验收第 8 条要能在响应里看到证据）。"""
    out = {}
    for label, code in (("newPerson", newPerson), ("oldPerson", oldPerson)):
        person = str(code or "").strip()
        if not person:
            continue
        rows = sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode=person, orderBy="bucketKey")
        out[label] = {"personCode": person,
                      "bucketCount": len(rows),
                      "buckets": [{"bucketKey": str(r.get("bucketKey") or ""),
                                   "sampleCount": int(r.get("sampleCount") or 0),
                                   "enabled": bool(r.get("centroid"))}
                                  for r in rows],
                      "lastModified": max([str(r.get("modifyYMDHMS") or "")
                                           for r in rows] or [""])}
    out["centroidRebuilt"] = bool(info.get("centroids"))
    return out


@router.post("/review/batch-fix", summary="同一簇批量改判（一次点击解决 N 张）")
def batchFix(body: dto.BatchFixBody) -> dict:
    """`assigner.batchFix()`：**一个事务 + 一次重算**。

    为什么值得单独一个端点：逐张调 `/review/fix` = N 个事务 + N 次重算，
    中途失败还会留下「改了一半」的批次 —— 用户看到的是一个自相矛盾的列表。

    `faceCodes` 与 `clusterCode` **二选一**即可：
      给 clusterCode 时**现查活成员**（簇成员会随确认实时变化，
      拿旧编码查会 0 行 —— 见 queue 文件头「clusterCode 不许跨会话缓存」）。
    """
    action = str(body.action or "").strip().lower()
    if action not in assigner.FIX_ACTIONS:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "action 只支持 %s，收到 %r"
                           % (sorted(assigner.FIX_ACTIONS.keys()), body.action))
    codes = _clampFaceCodes(body.faceCodes)
    if not codes and body.clusterCode:
        members = reviewQueue.clusterMembers(str(body.clusterCode))
        codes = _clampFaceCodes([m.get("faceCode")
                                 for m in (members.get("members") or [])])
        if not codes:
            raise dto.ApiError(dto.CODE_NOT_FOUND,
                               "簇 %s 现在没有活成员（可能已被全部确认/否决，"
                               "簇编码随之改变 —— 刷新簇列表再试）" % body.clusterCode)
    if not codes:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "faceCodes 与 clusterCode 必须给一个")
    target = str(body.personCode or "").strip() or None
    if action == "assign" and not target:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "action=assign 必须给 personCode")
    try:
        info = assigner.batchFix(codes, action, target, reason=body.reason or "")
    except assigner.AssignerError as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    return dto.okBody(action=action, requested=len(codes),
                      clusterCode=body.clusterCode or None,
                      fixed=int(info.get("fixed") or 0),
                      failed=info.get("failed") or [],
                      centroidBuckets=[one.get("bucketKey")
                                       for one in (info.get("centroids") or [])],
                      **_counts())


# ============================================================
# 四、合并 / 拆分 / 撤销（merger，isRevertible=1）
# ============================================================

@router.post("/review/merge", summary="合并两个人员（不可逆，但可撤销）")
def mergePersons(body: dto.MergeBody) -> dict:
    """`merger.merge(fromPerson, toPerson)`。

    ⚠️ **服务端只做「参数复述所需的查询」，二次确认交给前端**：
      合并会把两个人的历史揉成一个，质心一旦被污染靠重算回不到原值
      （重算只能反映「现在这些脸」）。所以前端在点之前应该先看
      `GET /api/contacts/{code}/impact` 或 `merger.suggestSplitPersons`。
      本端点**不做**任何「你确定吗」的判断 —— 那只会让用户以为
      服务端在替他做决定。
    """
    try:
        info = merger.merge(str(body.fromPersonCode or "").strip(),
                            str(body.toPersonCode or "").strip())
    except merger.MergeError as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    stats = browse.personStatsOf([str(info.get("toPerson") or "")]).get(
        str(info.get("toPerson") or ""), {})
    return dto.okBody(
        fromPersonCode=info.get("fromPerson"), toPersonCode=info.get("toPerson"),
        faces=len(info.get("faces") or []), faceCodes=info.get("faces") or [],
        linksKept=len(info.get("linksKept") or []),
        linksDropped=len(info.get("linksDropped") or []),
        categories=info.get("categories") or [],
        centroidsDropped=int(info.get("centroidsDropped") or 0),
        centroidEnabled=int(info.get("enabled") or 0),
        logCode=info.get("logCode"), revertible=True,
        undoEndpoint="/api/review/undo",
        targetStats=dict(photoCount=int(stats.get("photoCount") or 0),
                         faceCount=int(stats.get("faceCount") or 0),
                         confirmedFaceCount=int(stats.get("confirmedFaceCount") or 0)),
        **_counts())


@router.post("/review/split", summary="从某人拆出一张脸")
def splitFace(body: dto.SplitBody) -> dict:
    """`merger.split(faceCode, newPersonCode)`。

    ⚠️ `personCode` 不给 = 置为**未归属**（回待确认队列）；
       给了但库里没有这个人 = **自动建档**（他多半已经想好了名字，
       让他先去联系人页建人再回来点一次，就是把一次修正拆成四步操作，
       待确认队列会被他放弃）。
    """
    try:
        info = merger.split(str(body.faceCode or "").strip(),
                            str(body.personCode or "").strip(),
                            displayName=str(body.displayName or ""),
                            birthday=body.birthday)
    except (merger.MergeError, assigner.AssignerError) as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    return dto.okBody(
        faceCode=info.get("faceCode"), photoCode=info.get("photoCode"),
        fromPersonCode=info.get("fromPerson") or None,
        toPersonCode=info.get("toPerson") or None,
        created=info.get("created") or None,
        bucketKey=info.get("bucketKey") or None,
        logCode=info.get("logCode"), revertible=bool(info.get("logCode")),
        # 与 fix 单张分支同理：拆分会改两侧照片数、也会把脸挪进/挪出待确认队列，
        # 前端侧栏两个角标必须同一次响应里就拿到，否则要点一次别的操作才对上。
        **_counts(),
        **withCentroidOf(info, info.get("toPerson"), info.get("fromPerson")))


@router.post("/review/undo", summary="撤销一条 SPLIT / MERGE")
def undoOperation(body: dto.UndoBody) -> dict:
    """`merger.undo(logCode)` —— **唯一的逆操作入口**。

    撤销做四件事（缺一不可，见 merger.undo 的说明）：
      ① 反向恢复 pb_face.personCode 与 isConfirmed（按日志里的 c0/p0 **逐位**还原）
      ② 反向恢复 pb_photo_person 关联（纪律③的**双向**版本：既补回 from 侧，
         也清理 to 侧 —— 合并时 to 的脸可能被搬空了，残留就是幽灵关联）
      ③ **重算涉及双方的质心**
      ④ 回填原记录的 `revertedByLogCode`，并写一条 opType=UNDO 的新日志

    ⚠️ 四道闸门任一不满足就报错，**绝不「尽力而为地做一半」**：
      撤销会搬动人脸归属，而那是用户逐张核对过的事实 ——
      「撤销了两次」造成的错分，用户只能在几百张照片里一张张找回来。
    """
    try:
        info = merger.undo(str(body.logCode or "").strip())
    except merger.MergeError as e:
        raise dto.ApiError(_codeOf(str(e)), str(e))
    fromPerson = str(info.get("fromPerson") or "")
    toPerson = str(info.get("toPerson") or "")
    return dto.okBody(
        logCode=info.get("logCode"), undoLogCode=info.get("undoLogCode"),
        opType=info.get("opType"),
        fromPersonCode=fromPerson or None, toPersonCode=toPerson or None,
        facesRestored=int(info.get("facesRestored") or 0),
        linksRestored=int(info.get("linksRestored") or 0),
        linksDropped=int(info.get("linksDropped") or 0),
        missingFaces=info.get("missingFaces") or [],
        # ⚠️ 下面这几个是 DR-42 年代修正（BUCKET_FIX）的撤销结果 ——
        #    对 SPLIT / MERGE 恒为 None/0/[]，一并透传可以让前端**不用**
        #    按 opType 分两套读法，也避免"撤销成功了但界面上说不出做了什么"。
        photoCode=info.get("photoCode") or None,
        restoredOverride=info.get("restoredOverride"),
        facesRebucketed=int(info.get("facesRebucketed") or 0),
        bucketsChanged=int(info.get("bucketsChanged") or 0),
        warnings=info.get("warnings") or [],
        centroidBuckets=[one.get("bucketKey") for one in (info.get("centroids") or [])],
        personStats={code: browse.personStatsOf([code]).get(code, {})
                     for code in (fromPerson, toPerson) if code},
        **_counts())


@router.get("/review/revertible", summary="当前可撤销的操作列表（撤销按钮的候选集）")
def getRevertible(size: int = Query(default=20, le=dto.MAX_PAGE_SIZE)):
    """`isRevertible=1 AND revertedByLogCode IS NULL` 的日志。

    ⚠️ **只有 SPLIT / MERGE 可撤销**。普通确认的逆操作是「再点一次改判」，
       状态机自己就回得去；而合并/拆分会**抹掉「谁本来属于谁」这个事实**，
       只能靠日志。所以这里不把 ASSIGN/FIX 列进来 —— 列进来会让撤销按钮
       在最常见的场景下变成一个必然报错的按钮。
    """
    items = merger.revertibleList(limit=size)
    # 撤销确认框里要写「把「张三」合并进「张三(2)」」—— 只有编码用户读不出来
    names = _displayNamesOf([one.get("fromPersonCode") for one in items]
                            + [one.get("toPersonCode") for one in items])
    for one in items:
        one["fromDisplayName"] = names.get(str(one.get("fromPersonCode") or "")) or None
        one["toDisplayName"] = names.get(str(one.get("toPersonCode") or "")) or None
    return {"ok": True, "total": len(items), "count": len(items), "items": items}


# ============================================================
# 五、操作历史（排障）
# ============================================================

@router.get("/review/log", summary="纠错操作历史（排障：这张脸当初怎么被认成这个人的）")
def getReviewLog(faceCode: str = Query(default=None, description="按人脸查"),
                 photoCode: str = Query(default=None, description="按照片查"),
                 personCode: str = Query(default=None,
                                         description="按「归属到的人」查"),
                 opType: str = Query(default=None, description="按操作类型过滤"),
                 page: int = Query(default=1, ge=1),
                 size: int = Query(default=50, le=200)):
    """pb_review_log 的只读视图。

    ⚠️ 它同时是**排障依据**与**撤销依据**：
      - 排障：「这张脸当初是以多少分数被谁认下的」查 `similarity`；
      - 撤销：SPLIT/MERGE 的 `detail` 里记着成员脸与它们原来的 isConfirmed。
      人工确认**不写 similarity**（那是在编造分数），所以这类行的 similarity 为空，
      前端应显示「人工改判」而不是 0.00。
    """
    p, s = dto.clampPage(page, size, defaultSize=50)
    at = dto.offsetOf(p, s)
    if not any((faceCode, photoCode, personCode, opType)):
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "至少给一个筛选条件（faceCode / photoCode / personCode / opType）"
                           "；**本接口刻意不支持裸查全表** —— 日志表会随使用线性增长")
    if opType and opType not in assigner.OP_TYPES:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "opType 只支持 %s" % list(assigner.OP_TYPES))

    where = ["delFlag = %s"]
    values = [comGD.DEL_FLAG_NO]
    for column, value in (("faceCode", faceCode), ("photoCode", photoCode),
                          ("toPersonCode", personCode), ("opType", opType)):
        if value:
            where.append("%s = %%s" % column)
            values.append(str(value))
    cond = " AND ".join(where)
    total = sqliteCommon.countWhereGeneral("pb_review_log", cond, tuple(values))
    rows = sqliteCommon.query_pb_review_log(
        "pb_review_log", faceCode=str(faceCode or ""), photoCode=str(photoCode or ""),
        toPersonCode=str(personCode or ""), opType=str(opType or ""),
        orderBy="recID", descFlag=True, limitNum=s, offsetNum=at)
    items = [_logItem(r, _displayNamesOf(
        [r.get("fromPersonCode"), r.get("toPersonCode")])) for r in rows]
    return dto.pageBody(items, p, s, total)


def _logItem(row: dict, names: dict = None) -> dict:
    """pb_review_log 行 -> 前端要的日志条目。

    `fromDisplayName` / `toDisplayName`（步骤 12 补）
    -----------------------------------------------
      界面上要写「把「张三」合并进「张三(2)」」而不是两个编码 ——
      而 `pb_person.displayName` 是 UNIQUE，导入时重名者会被加后缀，
      所以「显示真实 displayName，不能只显示姓」是硬要求（DR-16 补充②）。
      连查带软删的人（`withDeleted`）：被合并掉的档案正是最该在历史里被看见的，
      而它已经 delFlag='1' 了。
    """
    known = names or {}
    fromCode = row.get("fromPersonCode") or None
    toCode = row.get("toPersonCode") or None
    return {
        "logCode": str(row.get("logCode") or ""),
        "opType": str(row.get("opType") or ""),
        "faceCode": row.get("faceCode") or None,
        "photoCode": row.get("photoCode") or None,
        "fromPersonCode": fromCode,
        "toPersonCode": toCode,
        "fromDisplayName": known.get(str(fromCode or "")),
        "toDisplayName": known.get(str(toCode or "")),
        "similarity": (round(float(row["similarity"]), 4)
                       if row.get("similarity") is not None else None),
        "faceCount": int(row.get("faceCount") or 0),
        "detail": row.get("detail") or None,
        "isRevertible": int(row.get("isRevertible") or 0),
        "revertedByLogCode": row.get("revertedByLogCode") or None,
        "opUser": row.get("opUser") or None,
        "opYMDHMS": row.get("opYMDHMS") or None,
        "canRevert": (int(row.get("isRevertible") or 0) == 1
                      and not str(row.get("revertedByLogCode") or "")),
    }


def _displayNamesOf(codes) -> dict:
    """一批 personCode -> displayName（连软删一起看）。批量查，不逐个get。"""
    marks = sorted({str(c) for c in (codes or []) if c})
    if not marks:
        return {}
    sqlMarks = ", ".join(["%s"] * len(marks))
    out = {}
    for row in query.selectList(
            "SELECT personCode AS personCode, displayName AS displayName"
            " FROM pb_person WHERE personCode IN (" + sqlMarks + ")",
            tuple(marks)):
        out[str(row.get("personCode") or "")] = str(row.get("displayName") or "")
    return out


def _codeOf(errText: str) -> str:
    """业务异常文本 -> HTTP 错误码。

    刻意**按文本判别**而不是给业务层加异常类型：assigner/merger 抛的是
    自己的 AssignerError / MergeError（它们已经带了足够清楚的文本），
    而改异常类型要动步骤 6/7 的代码 —— 那两处的注释明确写着
    「这套逻辑只有一处实现」，动它们的收益不抵风险。
    判不出来的走CODE_PARAM_INVALID（400），因为绝大多数确实是参数/引用不对。
    """
    text = str(errText or "")
    if "不存在" in text or "查无" in text:
        return dto.CODE_NOT_FOUND
    if "非法" in text or "不可撤销" in text or "已被" in text:
        return dto.CODE_TASK_STATE_ILLEGAL
    return dto.CODE_PARAM_INVALID


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("review.py _VERSION:", _VERSION)
    print("库:", sqliteCommon.dbFilePath() or "(未连接)")
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    print("四态(SQL):", reviewQueue.countStates())
    print("四态(装载):", reviewQueue.faceCountByState())
    print("自检 match:", reviewQueue.selfCheck()["match"])
    print("可撤销:", len(merger.revertibleList()))
