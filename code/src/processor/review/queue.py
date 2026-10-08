#! /usr/bin/env python3
#encoding: utf-8

#Filename: queue.py
#Description: photo-browser **两个队列**（待确认 / 「我不同意」）与四态计数（步骤 7）
#
# ⚠️⚠️ 本文件存在的全部理由：把两个队列的口径**钉死在一个地方**
# ------------------------------------------------------------------------
#   待确认队列   = `personCode IS NULL AND isStranger=0 AND delFlag='0'`
#   「我不同意」 = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0
#                  AND delFlag='0'`，**按 photoCode 分组**
#   人工确认     = `isConfirmed=1`（两个队列都不进）
#   陌生人       = `isStranger=1`（**两个队列都不进，也不进聚类**，永久排除）
#
#   两者由 personCode / isConfirmed / isStranger 三字段推导（数据库设计.md §4.5 四态表），
#   **不新增 matchType、不新建队列表**（D-4 / Q-3b）。这三个字段就是状态机本身。
#
# 为什么「绝对不要」写成 `personCode IS NULL OR isConfirmed=0`
# ------------------------------------------------------
#   自动归属（score >= T_HIGH）**不会**把 isConfirmed 置 1 —— DR-16③ 定死的：
#   isConfirmed 只表示「经人工确认」。于是 `OR isConfirmed=0` 会把
#   **全部自动归属**扫进待确认队列：10 万张照片里 4~8 万条，
#   队列爆炸，用户看一眼就放弃，功能等于没有。
#   「自动归属」的正确去处是**「我不同意」列表**（纠错入口，DR-16②）。
#
# ⚠️ pendingCount 口径必须拆开（DR-11）
#   pb_scan_job.pendingCount = 「疑似移动/重命名待确认**张数**」（步骤 3 的
#   movedToPhotoCode，非空计数）。本文件算的是「待确认**人脸**条数」。
#   **两个不同的计数，不许共用一个字段**，否则侧栏角标会把
#   「移动 12 张」显示成「12 张脸待确认」。本文件一律直接 SQL count。
#
# 不新建表（硬约束）
#   「待确认」用 personCode 可空表达（D-4）；「我不同意」用
#   personCode + isConfirmed 组合表达（Q-3b）。两张表都不建。
#
# 硬约束
#   * **只读**：本模块一个字都不写。改判走 assigner.fix()，确认走 assigner.confirm()。
#   * 不碰 photoDir：头像 / 人脸裁剪图的**路径**由 faceCode 推导（DR-1），
#     本模块只把 faceCode 带出去，让 api 层去 urls。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/review
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.match import bucket as bucket                        # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402
from engine.match import matcher as matcher                       # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("reviewQueue", "reviewqueue.log")

#: 待确认队列 = personCode 为空（IS NULL）。生成层只支持 nullFields，
#: `isStranger = 0` 在 Python 侧过滤 —— 与 movedToPhotoCode 同一个理由：
#: 不为一次筛选去改生成器 + 全库迁移。⚠️ 条件里**绝不能**加 isConfirmed。
PENDING_NULL_FIELDS: tuple = ("personCode",)

#: 四个 WHERE 条件串（**直接 SQL count** 的唯一出处）。
#: 写成常量而不是散落的字面量：这两个口径已经被写错过一次（DR-16），
#: 让它有且只有一个定义处，比在注释里写「别写错」可靠。
WHERE_PENDING: str = "personCode IS NULL AND isStranger = %s AND delFlag = %s"
WHERE_DISPUTED: str = ("personCode IS NOT NULL AND isConfirmed = %s"
                       " AND isStranger = %s AND delFlag = %s")
WHERE_CONFIRMED: str = "isConfirmed = %s AND isStranger = %s AND delFlag = %s"
WHERE_STRANGER: str = "isStranger = %s AND delFlag = %s"


# ============================================================
# 一、计数（直接 SQL，不读 pb_scan_job.pendingCount）
# ============================================================

def countPending() -> int:
    """待确认人脸条数。**验收口径**：必须等于
    `SELECT COUNT(*) FROM pb_face WHERE personCode IS NULL AND isStranger=0
      AND delFlag='0'`。"""
    return sqliteCommon.countWhereGeneral("pb_face", WHERE_PENDING,
                                          (0, comGD.DEL_FLAG_NO))


def countDisputed() -> int:
    """「我不同意」条数 = `personCode IS NOT NULL AND isConfirmed=0
    AND isStranger=0 AND delFlag='0'` 的行数。"""
    return sqliteCommon.countWhereGeneral("pb_face", WHERE_DISPUTED,
                                          (0, 0, comGD.DEL_FLAG_NO))


def countStates() -> dict:
    """四态计数（互斥）。侧栏角标要**两个数**：待确认 / 我不同意。"""
    return {
        "pending": countPending(),
        "disputed": countDisputed(),
        "confirmed": sqliteCommon.countWhereGeneral(
            "pb_face", WHERE_CONFIRMED, (1, 0, comGD.DEL_FLAG_NO)),
        "stranger": sqliteCommon.countWhereGeneral(
            "pb_face", WHERE_STRANGER, (1, comGD.DEL_FLAG_NO)),
    }


def faceCountByState() -> dict:
    """四态计数，**从装载到的行走出来**（与 countStates 互为交叉验证）。

    为什么要两个口径：SQL count 与 Python 过滤如果给出不同的数，
    说明生成层的 nullFields 过滤或 isStranger 侧过滤里有东西不对 ——
    而这类错**不会报错**，只会让队列悄悄少一批人。
    步骤 7 的验收第 5 条就是拿这两个数对齐。
    """
    out = {"pending": 0, "disputed": 0, "confirmed": 0, "stranger": 0}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light"):
        if int(row.get("isStranger") or 0):
            out["stranger"] += 1
        elif int(row.get("isConfirmed") or 0):
            out["confirmed"] += 1
        elif str(row.get("personCode") or ""):
            out["disputed"] += 1
        else:
            out["pending"] += 1
    return out


# ============================================================
# 二、人物卡片（候选人的头像 / 显示名）
# ============================================================

def personCards(needAvatarFallback: bool = True) -> dict:
    """{personCode: {displayName, avatarFaceCode}}。

    avatarFaceCode 的**兜底**：pb_person.avatarFaceCode 可能为空
    （人刚导入、一张脸都还没确认）。空头像在待确认队列里等于「一排灰块」，
    用户根本没法判断，所以兜底取「该人**人工确认**样本里 detScore 最高的那张脸」。
    刻意**不用自动样本**兜底：自动样本正是「机器认的、可能认错了」的那些，
    拿它当头像等于把可疑结果摆在最显眼的位置（DR-16③ 防污染的同一条思路）。

    ⚠️ 兜底是**按人逐个查**（query_pb_face(personCode=...) 走
       idx_pb_face_personCode），不是全表扫一遍再在 Python 里分：
       缺头像的只是刚导入的人（个位数），全表扫在 10 万行时是白跑一趟。
    """
    out = {}
    for row in sqliteCommon.query_pb_person("pb_person", mode="light"):
        code = str(row.get("personCode") or "")
        if not code:
            continue
        out[code] = {"displayName": str(row.get("displayName") or ""),
                     "avatarFaceCode": str(row.get("avatarFaceCode") or "")}
    if not needAvatarFallback:
        return out
    for code, card in out.items():
        if card["avatarFaceCode"]:
            continue
        bestScore = -1.0
        bestFace = ""
        for face in sqliteCommon.query_pb_face("pb_face", personCode=code,
                                                mode="light"):
            if not int(face.get("isConfirmed") or 0):
                continue
            if int(face.get("isStranger") or 0):
                continue
            faceCode = str(face.get("faceCode") or "")
            if not faceCode:
                continue
            try:
                score = float(face.get("detScore") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if score > bestScore:
                bestScore, bestFace = score, faceCode
        card["avatarFaceCode"] = bestFace
    return out


def _candidateDict(one: matcher.Candidate, cards: dict) -> dict:
    """matcher.Candidate -> 前端要的候选结构（含头像 faceCode）。

    姓名的**权威来源是 card**（personCards() 直读 pb_person.displayName），
    不是 one.displayName：matcher 那份是 CentroidIndex.displayNames 的引用，
    而它在 loadAllCentroids 时对空姓名用 personCode 兜过底
    （centroid._personDisplayNames 的 `or code`，那份要参与同分排序，
    不可能是空串）。于是只要**进程没重启**、质心索引还拿着旧值，
    `one.displayName` 就是一个非空的 'CS_BaoRui_Zhang' —— 真名反而被它盖掉，
    界面上就是一串代号。所以顺序必须是 card 优先。
    """
    card = cards.get(one.personCode) or {}
    return {"personCode": one.personCode,
            "displayName": card.get("displayName") or one.displayName or one.personCode,
            "avatarFaceCode": card.get("avatarFaceCode") or "",
            "similarity": round(float(one.score), 4),
            "bucketKey": one.bucketKey or None}


# ============================================================
# 三、待确认队列
# ============================================================

def iterPendingRows(pageRows: int = None, offset: int = 0,
                    orderBy: str = None, descFlag: bool = False):
    """**逐页吐出**待确认队列的原始行（mode=full：要带 embedding 给 matcher 用）。

    orderBy 缺省 recID —— 但那只是"稳定"，不是"有��先级"（见 pendingQueue 的说明）。

    为什么是生成器而不是一次性 list
    ------------------------------
      10 万张未归属的脸 × 2048 字节 embedding = 200MB（DR-12 的同一个坑）。
      队列要"全量排序后取前 N"时，唯一能同时满足「排序正确」与「内存有界」
      的做法就是：分页读 -> 逐页打分 -> 只留 top-K，其余立刻扔掉。
    """
    step = int(pageRows or basicSettings.QUEUE_PAGE_ROWS)
    at = int(offset or 0)
    while True:
        rows = sqliteCommon.query_pb_face(
            "pb_face", mode="full", orderBy=orderBy or "recID",
            descFlag=bool(descFlag), nullFields=PENDING_NULL_FIELDS,
            limitNum=step, offsetNum=at)
        if not rows:
            return
        for row in rows:
            if int(row.get("isStranger") or 0):
                continue
            yield row
        if len(rows) < step:
            return
        at += len(rows)


def loadPendingRows(limit: int = 0, offset: int = 0, pageRows: int = None,
                    orderBy: str = None, descFlag: bool = False) -> list:
    """待确认队列的**原始行**（一个窗口）。

    过滤口径 = `personCode IS NULL`（生成层）+ `isStranger=0`（Python 侧）
    + `delFlag='0'`（生成层缺省）。
    """
    out = []
    cap = int(limit or 0)
    for row in iterPendingRows(pageRows=pageRows, offset=offset,
                               orderBy=orderBy, descFlag=descFlag):
        out.append(row)
        if cap and len(out) >= cap:
            break
    return out


def _entryOf(row: dict, result, cards: dict) -> dict:
    best = result.score if result is not None else None
    return {
        "faceCode": str(row.get("faceCode") or ""),
        "photoCode": str(row.get("photoCode") or ""),
        "shotBucket": str(row.get("shotBucket") or "") or None,
        "detScore": (round(float(row["detScore"]), 4)
                     if row.get("detScore") is not None else None),
        "quality": (round(float(row["quality"]), 4)
                    if row.get("quality") is not None else None),
        "clusterCode": str(row.get("clusterCode") or "") or None,
        "similarity": (None if best is None else round(float(best), 4)),
        "decision": result.decision if result is not None else "",
        "reason": result.reason if result is not None else "",
        "topCandidates": [_candidateDict(c, cards)
                          for c in (result.topCandidates if result is not None else [])],
    }


def _queueOrder(item) -> tuple:
    """待确认队列的排序键：(-分数, 有簇优先, clusterCode, faceCode)

    ⚠️ 全序、无并列残留：clusterCode 相同的两条会再按 faceCode 分开，
    所以「两次查询顺序完全一致」—— 队列顺序抖动会被用户当成结果不可信。
    """
    return (-(item["similarity"] if item["similarity"] is not None else -1.0),
            0 if item["clusterCode"] else 1,
            item["clusterCode"] or "",
            item["faceCode"])


def pendingQueue(limit: int = 0, offset: int = 0, topN: int = None,
                 preset: str = None, matrix=None, index=None,
                 withCandidates: bool = True, sortFirst: bool = False,
                 orderBy: str = None, descFlag: bool = True) -> list:
    """待确认队列条目（每条带 Top-N 候选人物）。

    条目字段
    --------
      faceCode / photoCode : 定位这张脸
      shotBucket / detScore: 拍摄年代桶 / 检测置信度（UI 可据此提示"侧脸"）
      quality              : 综合质量分（**分页用的排序键**，见下）
      clusterCode          : 步骤 7 的簇编码（同一簇可批量处理）
      similarity           : 机器给出的最高分（**可空** = 没有可比质心）
      decision / reason    : 步骤 6 的三段式判定与原因码
      topCandidates        : [{personCode, displayName, avatarFaceCode,
                              similarity, bucketKey}, ...]

    ⚠️⚠️ 分页键必须是「**已落库**的列」，这是本函数最要紧的一条设计约束
    -----------------------------------------------------------
      生成层的 ORDER BY 只认白名单：pb_face 只有
      **recID / shotBucket / quality / faceCode** 四个可用。
      而队列「最该先确认」的主信号 similarity 是**算出来的**
      （步骤 6 明确：review 结果不落库，否则队列会以为已处理），
      没有任何可交给 SQL 的排序键。
      ⇒ 结论：**分页用 quality DESC（可排序、已落库、且永不变化），
        窗口内再按语义序排**。这不是折中的妥协，而是这份数据下
        唯一能拿到「全局正确分页」的办法。
      为什么 quality 够用：它衡量的是「这张脸本身清不清楚」
      （实测正式库 0.206~0.911、均值 0.590，全部非空）。
      脸清楚 -> 用户点开能认；脸糊 -> ��认了也是白认。
      而 similarity 会在质心长起来之后**持续变化**，拿它当分页键
      会出现「翻页时条目在眼前挪位」—— 那是分页最忌讳的事。
      ⇒ 关键论点：**队列是工单列表，处理完的条目会离队**。
        只要分页键稳定，page 1 永远是"当前最好的 20 张"，
        不需要每次重扫全量。

    窗口内的语义序（决定「这一页里先看哪一条」）
    ---------------------------------------------
      分数高 → 低；同分时**有簇的排前面**，再按 clusterCode、faceCode。
      ⚠️ 分数为空的排在最后：那是「库里根本没有可比的质心」（冷启动）。
      ⚠️ 「同分时簇优先」是实用需求：步骤 11 的效率来自
        「同一 clusterCode 批量改判」，而**冷启动时全员分数都是 None**
        （凑不够确认样本就没有质心），只按 faceCode 排会把簇成员打散。

    sortFirst —— 什么时候才需要全量排序
    -----------------------------------
      False（缺省）：一个 quality-DESC 窗口，O(1) 一页。**给 API 用。**
      True：分页扫全量、逐页打分，只保留 top-(offset+limit)。
        排序全局正确，内存仍有界，代价是每次调用走一遍全量未归属集合
        （正式库 73 张 <0.01s；10 万张约 40s）。
        **给 CLI 与「给我最该先确认的 N 张」这类一次性汇总用。**
    """
    if not sortFirst:
        rows = loadPendingRows(limit=limit, offset=offset,
                               orderBy=orderBy or basicSettings.QUEUE_ORDER_BY,
                               descFlag=descFlag)
        if not rows:
            return []
        out = _scoreRows(rows, topN, preset, matrix, index, withCandidates)
        out.sort(key=_queueOrder)
        return out

    cap = int(limit or 0)
    start = int(offset or 0)
    keep = (start + cap) if cap else 0
    pool = []
    batch = []
    for row in iterPendingRows():
        batch.append(row)
        if len(batch) < basicSettings.QUEUE_PAGE_ROWS:
            continue
        pool = _keepTop(pool, _scoreRows(batch, topN, preset, matrix, index,
                                         withCandidates), keep)
        batch = []
    if batch:
        pool = _keepTop(pool, _scoreRows(batch, topN, preset, matrix, index,
                                        withCandidates), keep)
    if not cap:
        pool.sort(key=_queueOrder)
        return pool
    return pool[start:start + cap]


def _scoreRows(rows, topN, preset, matrix, index, withCandidates) -> list:
    """一批行 -> 队列条目（含Top-N 候选）。质心只加载一次。"""
    if not rows:
        return []
    cards = personCards() if withCandidates else {}
    if not withCandidates:
        return [_entryOf(row, None, cards) for row in rows]
    if index is None:
        matrix, index = centroid.loadAllCentroids()
    results = matcher.matchMany(rows, matrix=matrix, index=index,
                                preset=preset, topN=topN)
    return [_entryOf(row, one, cards) for row, one in zip(rows, results)]


def _keepTop(pool: list, incoming: list, keep: int) -> list:
    """把 incoming 并进 pool，只保留排序键最小的 keep 条（keep=0 = 全留）。"""
    pool.extend(incoming)
    pool.sort(key=_queueOrder)
    if keep and len(pool) > keep:
        del pool[keep:]
    return pool


# ============================================================
# 四、「我不同意」列表
# ============================================================

def _lastAutoSimilarity(faceCode: str, personCode: str):
    """这张脸**当时**被认成这个人的余弦分数（取自 pb_review_log）。

    为什么 similarity 不在 pb_face 上
    ------------------------------
      同一个分数有两处消费者：pb_photo_person.confidence（关联行）与
      pb_review_log.similarity（操作留痕）。pb_face 上**故意没有**这一列：
      它是「某一次归属动作」的属性，不是「这张脸」的固有属性
      （同一张脸被改判三次就有三个分数，存哪一列都是错的）。
      所以这里回查日志 —— 顺便这正是日志表存在的意义（§4.9）。
    ⚠️ 人工确认**不写 similarity**（assigner.confirm 明确不编造分数），
       于是人工改判过的行这里返回 None，前端显示「人工改判」而不是「0.00」。
    """
    rows = sqliteCommon.query_pb_review_log(
        "pb_review_log", faceCode=str(faceCode), toPersonCode=str(personCode),
        orderBy="opYMDHMS", descFlag=True, limitNum=1)
    if not rows:
        return None
    value = rows[0].get("similarity")
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def disputedFaces(limit: int = 0, offset: int = 0) -> list:
    """「我不同意」的**人脸行**（不分组）。

    口径 = `personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0
    AND delFlag='0'`。生成层按 personCode 非空过滤 + Python 侧排掉
    isConfirmed=1 / isStranger=1。
    """
    step = int(limit or basicSettings.QUEUE_PAGE_ROWS)
    rows = []
    offset = int(offset or 0)
    while True:
        rowsIn = sqliteCommon.query_pb_face(
            "pb_face", mode="light", orderBy="recID",
            limitNum=step, offsetNum=offset)
        if not rowsIn:
            break
        for row in rowsIn:
            if not str(row.get("personCode") or ""):
                continue
            if int(row.get("isConfirmed") or 0) or int(row.get("isStranger") or 0):
                continue
            rows.append(row)
            if limit and len(rows) >= int(limit):
                return rows
        if len(rowsIn) < step:
            break
        offset += len(rowsIn)
    return rows


def disputedList(limit: int = 0, offset: int = 0, groupByPhoto: bool = True,
                 withSimilarity: bool = True) -> list:
    """「我不同意」列表 = 自动归属但未经人工确认的脸，**按 photoCode 分组**。

    用途（DR-16②）：用户浏览时发现「这张照片里那张脸认错了」，
    在这里一键改判；同一簇（clusterCode）还能批量改判（步骤 11 的
    POST /api/review/batch-fix）。所以每条脸都带 clusterCode。

    ⚠️ limit/offset 作用于**人脸行**，不是分组后的条目数：
       一张照片里可能认错好几个人，先按行分页才能把内存钉住。
       总条数用 countDisputed()（直接 SQL count）。
    """
    rows = disputedFaces(limit=limit, offset=offset)
    if not rows:
        return []
    cards = personCards()
    items = []
    for row in rows:
        personCode = str(row.get("personCode") or "")
        card = cards.get(personCode) or {}
        sim = _lastAutoSimilarity(str(row.get("faceCode") or ""),
                                  personCode) if withSimilarity else None
        items.append({
            "faceCode": str(row.get("faceCode") or ""),
            "photoCode": str(row.get("photoCode") or ""),
            "personCode": personCode,
            "displayName": card.get("displayName") or "",
            "avatarFaceCode": card.get("avatarFaceCode") or "",
            "similarity": sim,
            "detScore": (round(float(row["detScore"]), 4)
                         if row.get("detScore") is not None else None),
            "shotBucket": str(row.get("shotBucket") or "") or None,
            "clusterCode": str(row.get("clusterCode") or "") or None,
        })
    if not groupByPhoto:
        items.sort(key=lambda it: (-(it["similarity"] if it["similarity"] is not None
                                     else -1.0), it["faceCode"]))
        return items

    groups = {}
    for one in items:
        groups.setdefault(one["photoCode"], []).append(one)
    out = []
    for photoCode, faces in groups.items():
        faces.sort(key=lambda it: (-(it["similarity"] if it["similarity"] is not None
                                     else -1.0), it["faceCode"]))
        if len(faces) > basicSettings.DISPUTED_MAX_FACES_PER_PHOTO:
            faces = faces[:basicSettings.DISPUTED_MAX_FACES_PER_PHOTO]
        out.append({
            "photoCode": photoCode,
            "faceCount": len(faces),
            "personCodes": sorted(set(f["personCode"] for f in faces)),
            "faces": faces,
        })
    # 机器最有把握的排前面：用户最想否决的就是这些
    out.sort(key=lambda g: (-max((f["similarity"] if f["similarity"] is not None
                                 else -1.0) for f in g["faces"]), g["photoCode"]))
    return out


# ============================================================
# 四之二、簇视图（步骤 11 的「批量处理」入口）
# ============================================================
# ⚠️⚠️ clusterCode 是**内容指纹**，不是稳定身份
# ------------------------------------------------
#   编码 = sha256(簇内 faceCode 排序后拼接)[:12]，所以
#     · 同一份未归类集合重跑 -> 同一批编码（幂等，验收第 3 条）
#     · 簇成员**增减一张** -> 另一个簇 -> 编码随之改变
#   这不是缺陷，是「不建簇注册表」换来零状态维护的必然结果
#   （硬约束：不新建 pb_face_cluster 表，Q-3）。
#
#   ⇒ 由此定下的三条纪律（步骤 11 必须遵守，否则会出静默 bug）：
#     ① clusterCode **不得跨会话缓存**。UI 刷新后必须重新查
#        （clusterOverview / clusterMembers），拿旧编码去查会 0 行。
#     ② 任何「按簇操作」都要**先查后写**，且写完刷新簇列表：
#        用户确认掉一个成员后，剩下的脸可能分裂成两个新簇
#        —— 那是**正确行为**，UI 若还攥着旧编码就会显示「这个簇空了」。
#     ③ 需要「跨会话认出同一个未命名人物」时，锚点用
#        **representativeFaceCode**（detScore 最高那张）：
#        它比 clusterCode 稳定（成员变化不影响代表，
#        除非代表本人被确认掉了 —— 那时簇的归属本来就该变）。
#
# 为什么簇视图要单独一个函数（而不是让前端自己 group by）
#   簇大小是**聚合值**，SQL 表达不了（生成层没有 GROUP BY 通道，
#   业务层也不写裸 SQL）。这里一次**轻量全扫**
#   （mode="light" 不带 embedding，10 万行约 1~2 秒）在 Python 侧聚合，
#   换来「按簇大小排序 + 分页 + 代表样本」这三件前端真正需要的事。
#   逐条对比：sortFirst 的队列排序要读 embedding + 跑矩阵乘（10 万张约 40s），
#   这里便宜两个数量级 —— 因为它**不需要相似度**，只要 clusterCode 与 detScore。


def _pendingLightRows(pageRows: int = None):
    """待确认集合的**轻量行**（不带 embedding），逐页吐出。"""
    step = int(pageRows or basicSettings.QUEUE_PAGE_ROWS)
    at = 0
    while True:
        rows = sqliteCommon.query_pb_face(
            "pb_face", mode="light", orderBy="recID",
            nullFields=PENDING_NULL_FIELDS, limitNum=step, offsetNum=at)
        if not rows:
            return
        for row in rows:
            if int(row.get("isStranger") or 0):
                continue
            yield row
        if len(rows) < step:
            return
        at += len(rows)


def _bestDetRow(rows: list) -> dict:
    """代表样本 = detScore 最高的那一行（与 clustering.pickRepresentative 同规则）。

    detScore 为空/0 时**退化成"最后一行"**而不是"第一行"：
    队列里若所有脸都没有 detScore（脏数据），代表样本必须是**确定**的一张，
    否则每次调用挑出来的代表会跟着 SQL 返回顺序抖 —— 那正是
    「簇的代表样本在界面上自己变了」这种玄学 bug 的来源。
    """
    best = None
    bestScore = None
    for row in rows:
        try:
            score = float(row.get("detScore") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if bestScore is None or score > bestScore:
            best, bestScore = row, score
    return best if best is not None else (rows[0] if rows else {})


def clusterOverview(limit: int = 0, offset: int = 0, minSize: int = 0,
                    faceRows: list = None) -> list:
    """待确认集合的**簇概览**（按簇大小降序，同大小按 clusterCode）。

    返回
    ----
      [{clusterCode, size, representativeFaceCode, representativeDetScore,
        shotBucket, faceCodes, photoCodes}, ...]

    faceCodes / photoCodes 只在 size <= CLUSTER_DETAIL_MAX 时给出
    （大簇把全部 faceCode 列出来会把响应撑爆，UI 本来也要分页才画得下）。

    faceRows 参数只为单测与复用留口子（传入已取好的行就不再查库）。
    """
    groups = {}
    order = []
    for row in (faceRows if faceRows is not None else _pendingLightRows()):
        code = str(row.get("clusterCode") or "")
        if not code:
            continue
        if code not in groups:
            groups[code] = []
            order.append(code)
        groups[code].append(row)

    out = []
    for code in order:
        rows = groups[code]
        best = _bestDetRow(rows)
        detail = len(rows) <= basicSettings.CLUSTER_DETAIL_MAX
        out.append({
            "clusterCode": code,
            "size": len(rows),
            "representativeFaceCode": str(best.get("faceCode") or ""),
            "representativeDetScore": (round(float(best["detScore"]), 4)
                                       if best.get("detScore") is not None else None),
            "shotBucket": str(rows[0].get("shotBucket") or "") or None,
            "faceCodes": ([str(r.get("faceCode") or "") for r in rows]
                          if detail else None),
            "photoCodes": ([str(r.get("photoCode") or "") for r in rows]
                           if detail else None),
        })
    # 排序键里加 clusterCode：两个同大小的簇必须有**确定**先后，
    # 否则「翻页时同一个簇在第 1 页和第 2 页之间跳」。
    out.sort(key=lambda it: (-it["size"], it["clusterCode"]))
    if int(minSize or 0) > 1:
        out = [it for it in out if it["size"] >= int(minSize)]
    start = int(offset or 0)
    end = (start + int(limit)) if limit else None
    return out[start:end]


def clusterMembers(clusterCode: str, limit: int = 0, offset: int = 0) -> dict:
    """某个簇的**活成员**（现查，不缓存 —— 见本节开头的三条纪律）。

    簇成员随确认/否决实时变化（确认掉一个，剩下的可能分裂成新簇），
    所以这里是「以库为准」的唯一入口：步骤 11 每次操作前都该调它。
    返回 {clusterCode, size, truncated, members:[...]}，
    members 每项 {faceCode, photoCode, detScore, quality, personCode,
    isConfirmed, isStranger, shotBucket}。
    """
    code = str(clusterCode or "")
    out = {"clusterCode": code, "size": 0, "truncated": False, "members": []}
    if not code:
        return out
    at = int(offset or 0)
    cap = int(limit or 0)
    size = 0
    while True:
        rows = sqliteCommon.query_pb_face(
            "pb_face", mode="light", orderBy="recID",
            nullFields=PENDING_NULL_FIELDS, limitNum=basicSettings.QUEUE_PAGE_ROWS,
            offsetNum=at)
        if not rows:
            break
        hit = 0
        for row in rows:
            if int(row.get("isStranger") or 0):
                continue
            if str(row.get("clusterCode") or "") != code:
                continue
            hit += 1
            size += 1
            if cap and len(out["members"]) >= cap:
                out["truncated"] = True
                continue
            out["members"].append({
                "faceCode": str(row.get("faceCode") or ""),
                "photoCode": str(row.get("photoCode") or ""),
                "detScore": (round(float(row["detScore"]), 4)
                             if row.get("detScore") is not None else None),
                "quality": (round(float(row["quality"]), 4)
                            if row.get("quality") is not None else None),
                "personCode": str(row.get("personCode") or "") or None,
                "isConfirmed": int(row.get("isConfirmed") or 0),
                "isStranger": int(row.get("isStranger") or 0),
                "shotBucket": str(row.get("shotBucket") or "") or None,
            })
        if len(rows) < basicSettings.QUEUE_PAGE_ROWS:
            break
        at += len(rows)
        if not hit and at > basicSettings.QUEUE_PAGE_ROWS * 50:
            break        # 防御：clusterCode 全表都不存在，别把 10 万行读完
    out["size"] = size
    return out


def clusterStats() -> dict:
    """簇的总量口径（给侧栏角标 / CLI 概览用，不带成员明细）。"""
    items = clusterOverview()
    sizes = [it["size"] for it in items] or [0]
    return {"clusters": len(items), "faces": sum(sizes),
            "largest": max(sizes), "smallest": min(sizes),
            "noise": (faceCountByState()["pending"] - sum(sizes))}


# ============================================================
# 四之三、簇 -> 候选人物建议（**机器猜测，只用于排除，不能用于确认**）
# ============================================================
# ⚠️⚠️ 这段代码存在的唯一意义是「帮用户少点几下」，它**绝不能**被用来写
#    isConfirmed=1
# ------------------------------------------------------------
#   isConfirmed 的语义是「**经人工确认**」（数据库设计.md §4.5 / DR-16③）。
#   质心只用 isConfirmed=1 的样本，正是为了防「一张误认的脸拉偏质心 ->
#   越错越错」。若让机器按年龄/相似度去写 isConfirmed=1，
#   这个防污染机制就整个失效了 —— 而且是**静默**失效。
#
#   所以这里输出的叫「建议」而不是「结果」：
#     · 年龄只够**排除**（1950 年生的人不可能出现在 2013 年的照片里），
#       够不了**确认**（同龄的可能是任何人）；
#     · 没有任何一项输出是"结论"，UI 上必须让人点一下。
#
# 为什么还要它（明明 matcher 已经能给 Top-N 候选）
#   matcher 需要质心，而质心需要 isConfirmed=1 —— **冷启动时质心是空的**，
#   于是队列里所有人脸的 similarity 都是 None、Top-N 候选也是空的。
#   这就是「先有鸡还是先有蛋」的死结。而年龄是**唯一不依赖质心**的信号
#   （它来自联系人的生日 + 照片的年代），能把 10 个候选人缩到 2~3 个，
#   让用户点得动 —— 打破死结的办法不是让机器猜，而是**让人更容易点**。
#
# 年龄容差（岁）
#   ① 「拍照那一年」与「实际年龄」差 1~2 岁很常见（虚岁/证件年龄/跨年）；
#   ② 桶宽 3~10 年，shotBucket='2010-2014' 本身就给了 5 年跨度；
#   ③ 极端情况下联系人的生日填错（农历、身份证明上的）。
#   取 3 岁：能挡住"隔代人"，又不会因为一两岁误差把人误排掉。
PROPOSAL_AGE_TOLERANCE: int = 3

#: fit 三档的排序权重（与 matcher.rankCandidates 同口径：先看档位再看人）
_FIT_RANK: dict = {"age-ok": 0, "age-unknown": 1, "age-mismatch": 2}


def _isDiscriminative(candidates: list, totalPeople: int) -> bool:
    """这个簇的年龄筛选**有没有区分力**（实测本库恒为 False，必须如实报）。

    为什么要有这个标志（而不是默默给一串候选）
    -----------------------------------------
      正式库实测：14 个簇**全部**落在 2010-2014 这一个年代桶里，
      而 10 个联系人的年龄跨度是 1938~2007 —— 于是「age-ok」的条件
      把**所有人**都放进来了（正式库实测：每个簇 7 个 age-ok / 3 个无生日，
      age-mismatch 0 个）。也就是说年龄在这个库上**一条也没排掉**。
      此刻若还端出一串「候选」让用户参考，那就是在制造虚假的把握 ——
      用户会以为机器已经帮它筛过一遍了。
      ⇒ 一律如实报 discriminative=False，并明确告诉调用方：
        **这个库上唯一的办法是让人看图**，别拿年龄当依据。
      判据：age-ok 的人数 <= 联系人总数的一半，才算有区分力。
    """
    if totalPeople <= 0:
        return False
    ok = sum(1 for c in candidates if c["fit"] == "age-ok")
    return ok * 2 <= totalPeople

def _shotSpanOf(bucketKeys) -> tuple:
    """一批 shotBucket -> (最小拍摄年, 最大拍摄年)。**空/无法解析 -> (0, 0)**。

    ⚠️ parseBucketKey 返回的是 **(起始年, 结束年)**，不是 (起始年, 宽度) ——
       当成宽度用会算出 2010+2014-1=4023 这种离谱年份，然后所有年龄判定全错，
       而且**不报错**（只是把所有人都判成 age-mismatch）。
    为什么要按「跨度」而不是取单一年份：
      ① pb_face 只有 shotBucket（3~10 年宽），没有 shotYear —— 年份本来就是区间；
      ② 同一个簇里的人可能横跨好几年（旅行照），取单一年会偏 1~2 岁。
    """
    years = []
    for key in bucketKeys:
        got = bucket.parseBucketKey(key)
        if not got or not got[0]:
            continue
        years.append(int(got[0]))
        if got[1]:
            years.append(int(got[1]))
    if not years:
        return (0, 0)
    return (min(years), max(years))


def _fitOf(ageLow: int, ageHigh: int) -> str:
    """一个年龄区间算不算「这个年代该出现的人」。"""
    if ageLow > 100 or ageHigh < 0:
        return "age-mismatch"
    if ageHigh > 100 + PROPOSAL_AGE_TOLERANCE or ageLow < -PROPOSAL_AGE_TOLERANCE:
        return "age-mismatch"
    return "age-ok"


def clusterProposals(limit: int = 0, minSize: int = 0, persons: dict = None,
                     clusters: list = None) -> list:
    """给每个簇列出「年龄上可能」的候选人（**只排除，不确认**）。

    参数
    ----
      persons  : {personCode: {displayName, birthday}}；不给就现查 pb_person
      clusters : clusterOverview() 的结果；不给就现查

    返回
    ----
      [{clusterCode, size, representativeFaceCode, shotSpan, candidates, note}, ...]
      candidates 每项 {personCode, displayName, birthday, ageSpan, fit}
      fit ∈ "age-ok" / "age-unknown"（联系人生日为空）/ "age-mismatch"

    ⚠️ `age-mismatch` **不是**"这个人被排除了"（万一是本人填错了生日），
       只是在 UI 上排到最后、标灰。真正的永久排除只有 isStranger。
    """
    cards = persons if persons is not None else {
        str(p["personCode"]): {"displayName": str(p.get("displayName") or ""),
                               "birthday": str(p.get("birthday") or "")}
        for p in sqliteCommon.query_pb_person("pb_person", mode="light")}
    people = []
    for code in sorted(cards or {}):
        one = cards[code] or {}
        people.append((str(code), str(one.get("displayName") or ""),
                       str(one.get("birthday") or ""),
                       bucket.birthYearOf(one.get("birthday") or "")))

    items = clusters if clusters is not None else clusterOverview(minSize=minSize)
    out = []
    for one in items:
        low, high = _shotSpanOf([one.get("shotBucket") or ""])
        candidates = []
        for code, name, birthday, birthYear in people:
            if birthYear <= 0 or low <= 0:
                candidates.append({"personCode": code, "displayName": name,
                                   "birthday": birthday or None,
                                   "ageSpan": None, "fit": "age-unknown"})
                continue
            ageLow, ageHigh = low - birthYear, high - birthYear
            candidates.append({"personCode": code, "displayName": name,
                               "birthday": birthday or None,
                               "ageSpan": [ageLow, ageHigh],
                               "fit": _fitOf(ageLow, ageHigh)})
        # 排序：年龄相符 > 无生日 > 年龄不符；同档按 displayName 再按 personCode
        #（与 matcher.rankCandidates 同口径：给人看的顺序必须稳定可复现）
        candidates.sort(key=lambda c: (_FIT_RANK[c["fit"]], c["displayName"],
                                       c["personCode"]))
        out.append({
            "clusterCode": one["clusterCode"],
            "size": one["size"],
            "representativeFaceCode": one.get("representativeFaceCode"),
            "shotSpan": [low, high] if low > 0 else None,
            "candidates": candidates,
            "discriminative": _isDiscriminative(candidates, len(people)),
            "note": "机器只按年龄排除，**必须人工点确认**；isConfirmed=1 只能由人写",
        })
    if limit:
        out = out[:int(limit)]
    return out


# ============================================================
# 五、自检
# ============================================================

def selfCheck() -> dict:
    """两个队列的条数与四态计数（供 CLI --queues / 验收脚本直接打印）。

    ⚠️ `match=False` 是**真问题**，不是噪音：SQL count 与装载过滤给出不同的数，
       说明 WHERE 串与生成层的过滤条件里有东西对不上 —— 而这类错不报错，
       只会让队列悄悄少一批人。
    """
    bySql = countStates()
    byRow = faceCountByState()
    return {"bySql": bySql, "byRow": byRow, "match": bySql == byRow,
            "pendingCountField": _scanJobPendingCount()}


def _scanJobPendingCount() -> dict:
    """pb_scan_job.pendingCount 的实际值（**另一个计数**，别混用）。

    它记的是「疑似移动/重命名待确认**张数**」（步骤 3 的 movedToPhotoCode）。
    与 pendingFaceCount（待确认**人脸**条数）是两码事。
    """
    total = 0
    rows = 0
    for row in sqliteCommon.query_pb_scan_job("pb_scan_job", mode="light"):
        rows += 1
        try:
            total += int(row.get("pendingCount") or 0)
        except (TypeError, ValueError):
            continue
    return {"jobs": rows, "pendingCountSum": total,
            "note": "疑似移动/重命名待确认张数 != 待确认人脸条数（DR-11）"}


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("queue.py _VERSION:", _VERSION)
    print("库:", sqliteCommon.dbFilePath())
    print("四态(SQL count) :", countStates())
    print("四态(装载过滤)  :", faceCountByState())
    print("pendingCount 字段:", _scanJobPendingCount())
    _q = pendingQueue(limit=5, sortFirst=True)
    print("\n待确认队列前 %d 条:" % len(_q))
    for _it in _q:
        print("  %-34s 分%s 桶%-10s 机器分=%-7s 簇=%-22s %s"
              % (_it["faceCode"], _it["photoCode"][:8], _it["shotBucket"] or "(无)",
                 _it["similarity"], _it["clusterCode"] or "(无)",
                 "; ".join("%s/%.4f" % (c["displayName"] or c["personCode"],
                                        c["similarity"])
                           for c in _it["topCandidates"][:3])))
    _d = disputedList(limit=20)
    print("\n「我不同意」%d 张（分 %d 组）:" % (len(_d), sum(g["faceCount"] for g in _d)))
    for _g in _d[:5]:
        print("  照片 %s  %d 张脸" % (_g["photoCode"], _g["faceCount"]))
        for _f in _g["faces"]:
            print("     %-34s -> %-10s 当时相似度 %s"
                  % (_f["faceCode"], _f["displayName"] or _f["personCode"],
                     _f["similarity"]))
