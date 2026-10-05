#! /usr/bin/env python3
#encoding: utf-8

#Filename: centroid.py
#Description: photo-browser 人员年代桶质心（步骤 6）—— pb_person_centroid 的唯一写入口
#
# 职责
# ----
#   1. recompute()      按 (personCode, bucketKey) 重算归一化均值向量并 upsert
#                       ——**人工确认后立即调用**，不做全量重跑
#   2. recomputePerson() 该人的全部桶（合并人员 / 批量确认后用）
#   3. loadAllCentroids() 分页把启用的质心读成 (matrix, index)，供矩阵乘比对
#   4. dropPerson() / dropBucket() 人员或桶被拆掉时清质心（不留会误匹配的僵尸向量）
#
# 三条硬口径
# -----------
#   ① **sampleCount < MIN_CENTROID_SAMPLES 的桶不启用**：一条样本的"质心"就是那条
#      样本本身，cosine(face, centroid) = 1.0 —— 等于「这张脸和它自己比」，
#      任何人都能被这张脸匹配上，FA 直接 100%。所以样本数下限不是"精度问题"，
#      是**正确性问题**。computeCentroid() 在低于下限时把 centroid 写成 NULL，
#      让"未启用"这件事在**库里就看得出来**，而不是只存在于代码的 if 里。
#   ② **必须归一化后再存**：ArcFace 的向量已经 L2 归一化，但**均值不再归一化**
#      （n 个单位向量的和长度 < n）。存未归一化的均值 -> 库里 cosine 用点积算
#      -> 分数随桶内样本数变化 -> 样本多的桶天生分数高，
#      匹配结果会朝「样本多的人」倾斜，而用户完全看不出来。**这是最容易中招的一类错。**
#   ③ **分页读**（CENTROID_PAGE_ROWS）：3 万个 2048 字节 BLOB 一次性取回 =
#      61MB bytes 对象 + 61MB 矩阵 = 122MB，把「内存 < 100MB」顶爆。
#      两遍扫描：第一遍 mode="light"（不含 BLOB）拿到元信息与行数，
#      第二遍分页取 BLOB 直接填进预分配的矩阵，峰值只有一个矩阵 + 一页。
#
# 质心防污染 + 兜底桶（DR-16③ / D-9，本次修正的**核心**）
# ------------------------------------------------------
#   质心**只用 pb_face.isConfirmed=1 的人工确认样本**计算。
#   为什么这是正确性而不是精度问题：自动归属（score ≥ T_HIGH）里必然混着误认样本，
#   一张误认的脸把质心拉偏 -> 后续更多脸被误认 -> **越错越错**，
#   而且库里**没有任何迹象**提示质心已经坏了（sampleCount 看着很健康）。
#   「我不同意」列表（personCode 非空 + isConfirmed=0 + isStranger=0）
#   就是这些误认样本的纠错入口，也是质心唯一的清洁来源。
#
#   启用优先级（三级，缺一不可）
#   ------------------------------
#     优先级 1  相邻年代桶        该桶**确认样本** ≥ 3   -> 主力，跨年龄最准
#     优先级 2  ALL 兜底桶        总确认样本≥ 3         -> 早期样本不足时仍能自动归属
#     优先级 3  无                总确认样本 < 3         -> **该人不参与自动匹配**，
#                                                                其脸全部走待确认队列
#   这三级**不需要额外的 if**，它们由 sampleCount 的语义自然成立：
#   桶行的 sampleCount = 该桶确认样本数、ALL 行的 sampleCount = 总确认样本数，
#   而 loadAllCentroids 一律按 sampleCount >= MIN_CENTROID_SAMPLES 装载。
#   写成显式分支反而会出现「两处门禁不一致」这类静默失效。
#
#   逃生口：basicSettings.CENTROID_CONFIRMED_ONLY=False 退回旧口径（全样本），
#   **只用于**回归对比与冷启动（用户还没确认过任何脸时的 S0 回归）。
#
# 只在主进程调用（与 faceStore 同一条单写入者纪律）
#   本模块 import sqliteCommon，**绝不允许**进子进程（步骤 5 的 pool 有静态自查）。
#
# 写库口径
# --------
#   业务键 (personCode, bucketKey) 上有 UNIQUE 索引（数据库设计.md §五），
#   所以一律走 upsert：重算是幂等的，跑两遍结果完全一样（验收第 4 条）。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/match
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np                                                # noqa: E402

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402
from engine.face import engine as faceEngine                      # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("centroid", "centroid.log")

#: 特征维度与字节数（转发 basicSettings，全项目只有这一个口径）
EMBEDDING_DIM: int = basicSettings.EMBEDDING_DIM
EMBEDDING_BYTES: int = basicSettings.EMBEDDING_BYTES

#: 缺省样本数下限
MIN_SAMPLES: int = basicSettings.MIN_CENTROID_SAMPLES

#: 分页行数
PAGE_ROWS: int = basicSettings.CENTROID_PAGE_ROWS

#: **兜底桶键**（转发 bucket.ALL_BUCKET，全项目只有这一个口径，见 bucket.py 的说明）
ALL_BUCKET: str = bucket.ALL_BUCKET

#: 防污染开关（转发 basicSettings.CENTROID_CONFIRMED_ONLY）
#: ⚠️ 模块级转发是**为了让单测能 monkeypatch 一次全生效**：
#:   loadFaceVectors 默认值取这个常量，而不是在默认参数里直接读 basicSettings。
CONFIRMED_ONLY: bool = basicSettings.CENTROID_CONFIRMED_ONLY


# ============================================================
# 一、纯计算：归一化均值
# ============================================================

def normalizedMean(vectors) -> object:
    """一组向量 -> 归一化后的均值 np.float32[512]；**无法归一化时返回 None**。

    为什么均值之后还要再 L2 归一化（口径 ②）
    ----------------------------------------
      n 个单位向量的算术均值长度是 sqrt(...)/n，**明显小于 1**，
      且长度随 n 单调变化。库里若存未归一化的均值，匹配侧（本项目用点积算余弦）
      算出来的分数就等于「真余弦 × 均值长度」——
      样本多的桶分数整体偏高，自动归属的门槛对不同人不一样，**而且不报错**。
      所以这里必须再归一化一次，让库里存的每一个质心都是单位向量。
      （推论：质心的 sampleCount 只用于「够不够样本」的门禁，不参与打分。）

    逐条累加而不是 np.mean：一边加一边跳过垃圾行（维度不对 / 零向量 / None），
    最后用**实际入队的条数**归一。若用 np.mean 整体除以 len(vectors)，
    中间跳过任何一条都会让结果偏小，且不留任何痕迹。
    """
    if vectors is None:
        return None
    total = None
    used = 0
    for raw in vectors:
        vec = raw if isinstance(raw, np.ndarray) else None
        if vec is None:
            if raw is None:
                continue
            try:
                vec = faceEngine.decodeEmbedding(raw)
            except (TypeError, ValueError):
                continue                   # 字节数不对 = 脏行，跳过并计数
        vec = np.asarray(vec, dtype=np.float32).ravel()
        if vec.size != EMBEDDING_DIM:
            continue
        total = vec.copy() if total is None else total + vec
        used += 1
    if total is None or used == 0:
        return None
    return faceEngine.l2normalize(total / float(used))


# ============================================================
# 二、读样本
# ============================================================

def loadFaceVectors(personCode: str, bucketKey: str,
                    confirmedOnly: bool = None) -> tuple:
    """取 (某人, 某桶) 的人脸向量。返回 (vectors, skipped)。

    vectors : list[np.float32[512]]
    skipped : {'none': 空值行数, 'badsize': 字节数不对, 'zero': 零向量,
               'unconfirmed': 因「只要确认样本」被排除的行数}

    confirmedOnly（防污染，DR-16③）
    --------------------------------
      None = 跟 basicSettings.CENTROID_CONFIRMED_ONLY（缺省 True）
      True  = **只取 isConfirmed=1 的人工确认样本**
      False = 退回旧口径（桶内全部样本），仅用于回归对比与冷启动

    bucketKey = bucket.ALL_BUCKET 时**不分桶**：取该人的全部（确认）样本。
      这就是兜底桶的样本来源（见文件头「质心防污染 + 兜底桶」）。

    为什么按 personCode 查回来再在 Python 里过滤
    --------------------------------------------
      生成层的 query_pb_face **没有 shotBucket / isConfirmed 过滤参数**
      （只有 recID/faceCode/photoCode/personCode 四个等值键 + nullFields）。要按这两个
      条件筛只有两条路：
        ① 给 pb_face.txt 加查询参数 —— 要改生成器 + 重生成 + 全库迁移 + 改所有调用方，
           为两个「一个人的脸只有几十张」的筛选不值得（而且老库不一致的风险很高，DR-13）；
        ② 查回这个人的脸（个位数到几百），在 Python 里过滤。**选 ②。**
      10 万人脸规模下，一个人的脸平均 20~30 张，全查回的代价可以忽略。
    """
    stat = {"none": 0, "badsize": 0, "zero": 0, "unconfirmed": 0}
    onlyConfirmed = CONFIRMED_ONLY if confirmedOnly is None else bool(confirmedOnly)
    rows = sqliteCommon.query_pb_face("pb_face", personCode=personCode)
    out = []
    wantAll = (str(bucketKey or "") == ALL_BUCKET)
    bucket = str(bucketKey or "")
    for row in rows:
        if not wantAll and str(row.get("shotBucket") or "") != bucket:
            continue
        if onlyConfirmed and not int(row.get("isConfirmed") or 0):
            # 自动样本（可能含误认）**绝不进质心**。计数上报而不是静默跳过 ——
            # 「这个桶怎么少了几张」是排障时第一个要回答的问题。
            stat["unconfirmed"] += 1
            continue
        blob = row.get("embedding")
        if blob is None:
            stat["none"] += 1
            continue
        try:
            vec = faceEngine.decodeEmbedding(blob)
        except (TypeError, ValueError):
            stat["badsize"] += 1
            continue
        if vec is None or float(np.linalg.norm(vec)) < 1e-9:
            stat["zero"] += 1
            continue
        out.append(vec)
    return out, stat


def listBucketsOf(personCode: str, confirmedOnly: bool = None) -> list:
    """某人当前**实际有样本**的年代桶键集合（升序）。

    用它而不是查 pb_person_centroid：那张表里可能有样本早就搬走了的僵尸桶
    （人脸被 split 走、桶被改口径），拿它当"该重算哪些桶"的依据会漏。

    ⚠️ **不含 ALL_BUCKET**：ALL 是虚拟桶（不对应任何 pb_face.shotBucket 值），
    由 recomputePerson 单独补算。混进来会让这里出现一个"没人待过的年代桶"。
    """
    onlyConfirmed = CONFIRMED_ONLY if confirmedOnly is None else bool(confirmedOnly)
    buckets = set()
    for row in sqliteCommon.query_pb_face("pb_face", personCode=personCode):
        bucketKey = str(row.get("shotBucket") or "")
        if not bucketKey or bucketKey == ALL_BUCKET:
            continue
        if onlyConfirmed and not int(row.get("isConfirmed") or 0):
            continue
        buckets.add(bucketKey)
    return sorted(buckets, key=lambda k: (int(k.split("-")[0]) if k[:1].isdigit()
                                          else 0, k))


# ============================================================
# 三、写质心
# ============================================================

def computeCentroid(personCode: str, bucketKey: str,
                    confirmedOnly: bool = None) -> dict:
    """算出 (某人, 某桶) 的质心。**不写库**，返回结果 dict。

    返回 {personCode, bucketKey, sampleCount, centroid, usable, skipped,
          isAllBucket, confirmedOnly}
      centroid 为 None 表示样本不足（不启用），**别拿它去算相似度**。
      bucketKey = ALL_BUCKET 表示兜底桶（该人的全部确认样本，不分桶）。
    """
    vectors, skipped = ([], {"none": 0, "badsize": 0, "zero": 0, "unconfirmed": 0})
    onlyConfirmed = CONFIRMED_ONLY if confirmedOnly is None else bool(confirmedOnly)
    isAll = (str(bucketKey or "") == ALL_BUCKET)
    if isAll or str(bucketKey or ""):
        vectors, skipped = loadFaceVectors(personCode, bucketKey, onlyConfirmed)
    return {
        "personCode": str(personCode or ""),
        "bucketKey": ALL_BUCKET if isAll else str(bucketKey or ""),
        "sampleCount": len(vectors),
        "centroid": normalizedMean(vectors),
        "usable": len(vectors),
        "skipped": skipped,
        "isAllBucket": isAll,
        "confirmedOnly": onlyConfirmed,
    }


def recompute(personCode: str, bucketKey: str, minSamples: int = None,
              confirmedOnly: bool = None) -> dict:
    """重算并 upsert pb_person_centroid。**人工确认后立即调这个。**

    为什么"立即"是硬要求
    ------------------
      质心是匹配的唯一依据。用户确认第 5 张脸之后如果还要等一次全量重跑才能生效，
      那么他在待确认队列里点下去的每一次确认都是**没有即时反馈**的 ——
      下一张脸还是用旧质心判，还是进灰区，于是他会怀疑"确认了也没用"，
      然后就不确认了。整个机制就靠这条即时反馈活着。
      单桶重算的成本是「这个人的脸数 × 512 维求和」，毫秒级，没有不做即时的理由。

    bucketKey
    ---------
      '1995-1999' 这样的年代桶：只用**该桶**的确认样本
      ALL_BUCKET   兜底桶：用**全部**确认样本（不分桶）
      空串/None    这张脸没有拍摄年份 -> **连行都不建**（见下）

    minSamples
    ---------
      低于下限的桶把 centroid 写 NULL、sampleCount 照实写。
      留一行 sampleCount=1/2 的记录是有价值的：它让「这个人只有一个样本，
      质心不可用」在库里查得出来，而不是表现为"这个人没有这个年代的桶"。
    """
    limit = MIN_SAMPLES if minSamples is None else int(minSamples)
    person = str(personCode or "")
    info = computeCentroid(person, bucketKey, confirmedOnly)
    count = int(info["sampleCount"])
    if not info["bucketKey"]:
        # 桶键为空 = 这张脸没有拍摄年份，本来就不参与跨桶比对。
        # **连行都不建**：建一条 bucketKey='' 的质心行没有任何用处，
        # 还会让"这个人在这个桶里有数据"看起来成立（库里查得到、实际不参与匹配）。
        _LOG.debug("%s 桶键为空（无拍摄年份），不建质心行", person)
        return {"personCode": person, "bucketKey": "", "sampleCount": count,
                "enabled": False, "skipped": info["skipped"]}
    enabled = bool(info["centroid"] is not None and count >= limit)
    row = {
        "personCode": person,
        "bucketKey": info["bucketKey"],
        "sampleCount": count,
        # 低于下限 -> 存 NULL（口径 ①）。**不要**存那个 1~2 样本的均值。
        "centroid": faceEngine.encodeEmbedding(info["centroid"]) if enabled else None,
        "modifyYMDHMS": misc.getTime(),
    }
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_person_centroid", [row],
        conflictColumns=("personCode", "bucketKey"),
        updateColumns=("centroid", "sampleCount", "modifyYMDHMS"),
        fillStandard=True,
        forceColumns=("centroid", "sampleCount"))
    if rtn == -2:                        # sqliteHandle.RET_ERROR（与 faceStore 同写法）
        raise RuntimeError("pb_person_centroid 写入失败: %s"
                           % sqliteCommon.dbHandle().lastErrMsg)
    if info["skipped"] and any(info["skipped"].values()):
        _LOG.warning("%s/%s 跳过脏向量 %s（空/长度不对/零向量/未确认）",
                     person, info["bucketKey"], info["skipped"])
    if not enabled and count:
        _LOG.info("%s/%s 仅 %d 个确认样本（<%d），质心不启用",
                  person, info["bucketKey"], count, limit)
    return {"personCode": person, "bucketKey": info["bucketKey"],
            "sampleCount": count, "enabled": enabled,
            "isAllBucket": info["isAllBucket"],
            "confirmedOnly": info["confirmedOnly"],
            "skipped": info["skipped"]}


def recomputePerson(personCode: str, minSamples: int = None,
                    confirmedOnly: bool = None) -> dict:
    """重算某人的**全部**桶（含 ALL 兜底桶），并把已经没有样本的桶质心清掉。

    ALL 桶一起算
    -----------
      兜底桶是「这个人能不能自动匹配」的最后一道保险：某张照片的脸落在
      一个还没有 3 个确认样本的年代桶里时，靠 ALL 桶才认得出。
      **只重算年代桶就等于把这道保险漏掉**，表现是"这张照片他明明确认过，
      却认不出来"。所以每次重算都把 ALL 算上（顺便清掉它自己的僵尸状态）。

    清僵尸桶是必须的：样本搬走了（比如 split 走了一张）而质心还在，
      那条向量会继续参与匹配 —— 它代表的分布已经不是这个人现在的分布了，
      而且 sampleCount 还停在旧值，看着还挺健康，**没有任何迹象提示它过期了**。
    """
    person = str(personCode or "")
    onlyConfirmed = CONFIRMED_ONLY if confirmedOnly is None else bool(confirmedOnly)
    buckets = listBucketsOf(person, onlyConfirmed) + [ALL_BUCKET]
    results = [recompute(person, one, minSamples, onlyConfirmed) for one in buckets]
    stale = [row for row in sqliteCommon.query_pb_person_centroid(
        "pb_person_centroid", personCode=person)
        if str(row.get("bucketKey") or "") not in buckets]
    for row in stale:
        dropBucket(person, str(row.get("bucketKey") or ""))
    return {"personCode": person, "buckets": results,
            "enabled": sum(1 for r in results if r["enabled"]),
            "dropped": len(stale), "confirmedOnly": onlyConfirmed}


def dropBucket(personCode: str, bucketKey: str) -> int:
    """删掉一条桶质心。返回删除行数。

    走**生成层**的 delete_pb_person_centroid（按 recID），不写裸 SQL。
    """
    deleted = 0
    for row in sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode=personCode, bucketKey=bucketKey):
        rtn = sqliteCommon.delete_pb_person_centroid("pb_person_centroid",
                                                     row["recID"], hardDelete=True)
        if rtn and rtn > 0:
            deleted += int(rtn)
    return deleted


def dropPerson(personCode: str) -> int:
    """删掉某人的全部桶质心。返回删除行数（合并人员时用）。

    为什么合并后要删**全部**而不是只删冲突桶：fromPerson 的向量已经整体搬给 toPerson，
    留着它们就等于 toPerson 同时被「旧质心」和「新质心」两套向量匹配，
    而两者的分桶口径可能不同（birthday 不同 -> 桶键不同），
    分数高低取决于哪套先被读到 —— **不可复现**。删干净 + 按 toPerson 重算才是唯一的确定性解。
    """
    deleted = 0
    for row in sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode=personCode):
        rtn = sqliteCommon.delete_pb_person_centroid("pb_person_centroid",
                                                     row["recID"], hardDelete=True)
        if rtn and rtn > 0:
            deleted += int(rtn)
    return deleted


def centroidOf(personCode: str, bucketKey: str, minSamples: int = None):
    """取一条质心 -> np.float32[512] 或 None（不存在 / 样本不足 / NULL）。"""
    limit = MIN_SAMPLES if minSamples is None else int(minSamples)
    for row in sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode=personCode, bucketKey=bucketKey):
        if int(row.get("sampleCount") or 0) < limit:
            return None
        blob = row.get("centroid")
        if blob is None:
            return None
        try:
            return faceEngine.decodeEmbedding(blob)
        except (TypeError, ValueError):
            return None
    return None


# ============================================================
# 四、全量加载 -> (matrix, index)
# ============================================================

class CentroidRow(object):
    """一条启用的桶质心（轻量对象，别用 dataclass：加载 3 万条时省内存）"""

    __slots__ = ("rowPos", "personCode", "bucketKey", "sampleCount", "recID")

    def __init__(self, rowPos, recID, personCode, bucketKey, sampleCount):
        self.rowPos = int(rowPos)
        self.recID = int(recID)
        self.personCode = str(personCode or "")
        self.bucketKey = str(bucketKey or "")
        self.sampleCount = int(sampleCount)

    def __repr__(self):
        return ("CentroidRow(#%d %s/%s n=%d)"
                % (self.rowPos, self.personCode, self.bucketKey, self.sampleCount))


class CentroidSubset(object):
    """**某个候选桶集合**下的质心子集，已按人聚成连续段。

    为什么必须"按人聚成连续段"
    ----------------------
      决策分数是「同一个人在候选桶里的**最大**分数」（硬约束：取 max，不取 mean）。
      一次矩阵乘 Q @ M.T 出来的是 (脸 × 桶质心) 的分数，要变成 (脸 × 人) 就得
      按人做 reduce。np.maximum.reduceat 能在 O(N) 内把连续段聚成一行，
      但前提是**同一个人的所有候选行必须相邻**。
      所以全量加载时按 (personCode, bucketKey) 排序，取子集时保持升序
      （flatnonzero 天然升序），连续性就自动成立。
      走不通的路：逐人 np.maximum.at —— 那是 Python 层循环，3 万行 × 每脸一次，
      慢两个数量级。
    """

    __slots__ = ("matrix", "personPos", "starts", "personCodes", "displayNames",
                 "bucketKeys", "rowPositions", "isWhole")

    def __init__(self, matrix, personPos, starts, personCodes, displayNames,
                 bucketKeys, rowPositions, isWhole=False):
        #: np.float32[N,512]（isWhole 时是全量矩阵的视图，不额外占内存）
        self.matrix = matrix
        #: np.int32[N] —— 每行属于 personCodes 的第几个
        self.personPos = personPos
        #: np.int64[P] —— 每个段在 N 维里的起始下标（reduceat 用）
        self.starts = starts
        #: list[str] —— 本子集里出现过的人（有序、去重）
        self.personCodes = personCodes
        #: dict[personCode -> displayName]（同分排序用）
        self.displayNames = displayNames
        #: list[str] —— 每行的桶键（Top-5 报告用）
        self.bucketKeys = bucketKeys
        #: np.int64[N] —— 每行在全量矩阵里的行号（回查用）
        self.rowPositions = rowPositions
        self.isWhole = bool(isWhole)

    def __len__(self):
        return int(self.matrix.shape[0])

    @property
    def personCount(self) -> int:
        return len(self.personCodes)

    def scoreAll(self, queryMatrix) -> object:
        """(M,512) @ 子集.T -> (M, P)，已按人取 max。"""
        if len(self) == 0 or self.personCount == 0:
            return np.zeros((queryMatrix.shape[0], 0), dtype=np.float32)
        sims = np.asarray(queryMatrix, dtype=np.float32) @ self.matrix.T
        if self.personCount == len(self):        # 每人只有一行，省一次 reduce
            return sims
        return np.maximum.reduceat(sims, self.starts, axis=1)

    def rowsOf(self, personIdx: int) -> tuple:
        """第 personIdx 个人在本子集里占的行区间 [lo, hi)（连续段，见类头说明）"""
        lo = int(self.starts[personIdx])
        hi = int(self.starts[personIdx + 1]) if personIdx + 1 < len(self.starts) \
            else int(self.matrix.shape[0])
        return lo, hi


class CentroidIndex(object):
    """全量质心索引：matrix（人×桶 顺序的向量矩阵）+ 定位与候选子集能力。"""

    #: 子集缓存条目数。1~2 就够：同一批人脸的候选桶往往完全相同，
    #: 缓存住能省掉「同一批 1000 张脸重复 gather 1000 次」的浪费；
    #: 再多就是纯占内存（每个子集都是一份矩阵拷贝）。
    SUBSET_CACHE_SIZE = 2

    def __init__(self, matrix, rows, personCodes, displayNames, minSamples,
                 dbFile="", skipped=0):
        #: np.float32[N,512]
        self.matrix = matrix
        self.rows = rows                              # list[CentroidRow]
        self.personCodes = personCodes                # 全库人员（升序，仅供查询）
        self.displayNames = displayNames              # dict[personCode -> displayName]
        self.minSamples = int(minSamples)
        self.dbFile = dbFile
        self.skipped = int(skipped)                  # 因样本不足/无向量被跳过的行数
        self._byKey = {}
        for row in rows:
            self._byKey[(row.personCode, row.bucketKey)] = row.rowPos
        self._byBucket = {}                           # bucketKey -> list[rowPos]
        for row in rows:
            self._byBucket.setdefault(row.bucketKey, []).append(row.rowPos)
        self._cache = {}

    # ---- 基本信息 ----
    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    @property
    def vectorCount(self) -> int:
        return int(self.matrix.shape[0])

    @property
    def bytes(self) -> int:
        return int(self.matrix.nbytes)

    def rowOf(self, personCode: str, bucketKey: str) -> int:
        """(人, 桶) -> 全量矩阵行号；没有返回 -1。"""
        return self._byKey.get((str(personCode or ""), str(bucketKey or "")), -1)

    def displayName(self, personCode: str) -> str:
        return self.displayNames.get(str(personCode or ""), "")

    def summary(self) -> dict:
        return {"vectors": self.vectorCount, "persons": len(self.personCodes),
                "bytes": self.bytes, "megabytes": round(self.bytes / 1048576.0, 2),
                "minSamples": self.minSamples, "skipped": self.skipped,
                "buckets": len(self._byBucket), "dbFile": self.dbFile}

    # ---- 候选子集 ----
    def positionsFor(self, bucketKeys) -> list:
        """候选桶键列表 -> 升序行号列表（**去重且升序**，升序是 reduceat 的前提）"""
        out = set()
        for key in bucketKeys or ():
            for pos in self._byBucket.get(str(key), ()):
                out.add(int(pos))
        return sorted(out)

    def subset(self, bucketKeys) -> CentroidSubset:
        """取候选桶的质心子集（带缓存）。返回的子集已按人聚成连续段。"""
        keys = tuple(sorted(set(str(k) for k in (bucketKeys or ()) if str(k or ""))))
        if keys in self._cache:
            return self._cache[keys]
        positions = self.positionsFor(keys)
        if not positions:
            sub = CentroidSubset(
                np.zeros((0, EMBEDDING_DIM), dtype=np.float32),
                np.zeros((0,), dtype=np.int32), np.zeros((0,), dtype=np.int64),
                [], dict(self.displayNames), [], np.zeros((0,), dtype=np.int64))
        else:
            posArr = np.asarray(positions, dtype=np.int64)
            whole = (len(positions) == self.vectorCount)
            mat = self.matrix if whole else np.ascontiguousarray(self.matrix[posArr])
            codes = [self.rows[p].personCode for p in positions]
            # 去重保序：同一人的多行必须落成连续段（CentroidSubset 的前提）
            # ⚠️ 这里用 dict 查表而不是 uniq.index(code)：后者是 O(P) 线性查找，
            #    3 万行 × 3 万人 = 9e8 次比较，加载一次要几十秒 —— 而且不报错，
            #    只是「启动变慢了」，很容易被当成"库里数据多就这样"而接受。
            uniq, personPos, starts, lookup = [], [], [], {}
            for code in codes:
                idx = lookup.get(code)
                if idx is None:
                    idx = len(uniq)
                    lookup[code] = idx
                    uniq.append(code)
                    starts.append(len(personPos))
                personPos.append(idx)
            sub = CentroidSubset(
                mat, np.asarray(personPos, dtype=np.int32),
                np.asarray(starts, dtype=np.int64), uniq,
                dict(self.displayNames), [self.rows[p].bucketKey for p in positions],
                posArr, isWhole=whole)
        if len(self._cache) >= self.SUBSET_CACHE_SIZE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[keys] = sub
        return sub

    def clearCache(self) -> None:
        self._cache.clear()


def _personDisplayNames() -> dict:
    """personCode -> displayName（全库一次取回）。

    为什么要 displayName 参与排序：验收要求「同分按 displayName 排」。
    用 personCode 排没有意义 —— 'P_ab12' < 'P_zz99' 与谁是谁无关，
    同分时排出来的顺序对用户是随机的。**所以这条要求不只是"为了好看"，
    它是让「同分时候选顺序可复现」的唯一办法。**
    """
    out = {}
    for row in sqliteCommon.query_pb_person("pb_person", mode="light"):
        code = str(row.get("personCode") or "")
        if code:
            out[code] = str(row.get("displayName") or code)
    return out


def loadAllCentroids(minSamples: int = None, dbFile: str = None,
                     progress=None) -> tuple:
    """全量加载启用的桶质心 -> **(matrix, index)**。

    参数
    ----
      minSamples : 样本数下限，None = basicSettings.MIN_CENTROID_SAMPLES
      dbFile     : 显式切库（DR-10：不传就**绝不**切）
      progress   : 可选回调 progress(已读行数, 总行数)

    返回
    ----
      matrix : np.float32[N,512]，行内是 L2 归一化的质心
      index  : CentroidIndex

    内存（硬指标：3 万条 < 100MB，见 开发计划 DR-12 与本文件头口径 ③）
    --------------------------------------------------------------
      矩阵本体 30000 × 512 × 4B = **61.4 MB**（这是下限，省不掉）
      元信息 3 万个 CentroidRow（__slots__）+ 索引字典 ≈ 8~12 MB
      分页缓冲 = 1 页 × 2048 B ≈ 4 MB（**不**与矩阵同时翻倍）
      ⇒ 约 75 MB，达标。

    两遍扫描，不是为了省事，是因为**必须先知道 N 才能预分配矩阵**：
      遍 1  mode="light"（生成层会剔除 BLOB 列）-> 拿到 (recID, personCode,
            bucketKey, sampleCount) 且不碰任何向量字节；
      遍 2  按 recID 升序分页取 "full"，**只把需要的行拷进预分配矩阵**。
    一次性 query 全表会同时持有「3 万个 2048 字节 bytes 对象」与矩阵 = 122MB，
    这就是 DR-12 里说的「全量加载 205MB」的同类问题。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)          # DR-10：必须显式才切库
    limit = MIN_SAMPLES if minSamples is None else int(minSamples)
    page = max(1, int(PAGE_ROWS))

    # ---- 遍 1：元信息（无 BLOB），筛出 sampleCount 达标的行 ----
    # ⚠️ mode="light" 由生成层**剔除所有 BLOB 列**，所以这一遍拿到的行里
    #    **压根没有 centroid 这个键**。曾经在这里判 `centroid is None` 想提前过滤，
    #    结果把**全部**行都过滤掉了（拿到的是 None 不是"没有值"），索引永远为空 ——
    #    而且不报错，只是"匹配不到任何人"。这类 bug 靠读代码是看不出来的。
    #    合法性（长度/零向量）只能在遍 2 拿到真字节之后判。
    metas = []
    offset = 0
    while True:
        rows = sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", mode="light", orderBy="recID",
            limitNum=page, offsetNum=offset)
        if not rows:
            break
        for row in rows:
            if int(row.get("sampleCount") or 0) < limit:
                continue
            metas.append((int(row["recID"]), str(row.get("personCode") or ""),
                          str(row.get("bucketKey") or ""),
                          int(row.get("sampleCount") or 0)))
        offset += len(rows)
        if progress:
            progress(offset, None)
    total = offset
    if progress:
        progress(total, total)

    # 排序键：先人后桶 —— 保证"同一人的候选行连续"，reduceat 的前提（见 CentroidSubset）
    metas.sort(key=lambda m: (m[1], m[2], m[0]))
    wanted = set(m[0] for m in metas)
    matrix = np.zeros((len(metas), EMBEDDING_DIM), dtype=np.float32)
    filled = np.zeros(len(metas), dtype=bool)
    rowPosOf = dict((m[0], i) for i, m in enumerate(metas))

    # ---- 遍 2：分页取 BLOB，直接填进预分配矩阵 ----
    offset = 0
    while True:
        rows = sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", mode="full", orderBy="recID",
            limitNum=page, offsetNum=offset)
        if not rows:
            break
        for row in rows:
            recID = int(row["recID"])
            pos = rowPosOf.get(recID)
            if pos is None:
                continue
            try:
                vec = faceEngine.decodeEmbedding(row.get("centroid"))
            except (TypeError, ValueError):
                _LOG.warning("质心 recID=%d/%s 字节数不是 %d，剔除（不猜、不补零）",
                             recID, row.get("bucketKey"), EMBEDDING_BYTES)
                continue
            if vec is None:
                _LOG.warning("质心 recID=%d/%s 为 NULL，剔除", recID,
                             row.get("bucketKey"))
                continue
            norm = float(np.linalg.norm(vec))
            if norm < 1e-9:
                # 零向量：留着会让 cosine 恒为 0，把这个人静默变成"永不匹配"，
                # 比直接剔除更难查（查的人只会看到"怎么老是匹配不上"）。
                _LOG.warning("质心 recID=%d/%s 是零向量，剔除", recID,
                             row.get("bucketKey"))
                continue
            # 库里的值万一没归一化（老库 / 手工塞进去的），这里补一次，
            # 保证「点积 == 余弦」这个前提对每一行都成立
            matrix[pos] = vec / norm
            filled[pos] = True
        offset += len(rows)
        if progress:
            progress(offset, total)

    # ---- 压实：剔掉没填上的行。**不留零行** ----
    # 留一行零向量在矩阵里，会让它在每次打分时贡献一个恒为 0 的相似度，
    # 顶掉一个真实候选的名额（Top-5 里出现一个永远 0 分的陌生人）。
    if not bool(filled.all()):
        good = np.flatnonzero(filled)
        dropped = len(metas) - len(good)
        _LOG.warning("质心装载：%d/%d 行可用，%d 行字节非法/为零已剔除",
                     len(good), len(metas), dropped)
        matrix = np.ascontiguousarray(matrix[good])
        metas = [metas[i] for i in good]
    else:
        _LOG.info("质心装载：%d 行全部可用", len(metas))

    rows = [CentroidRow(i, m[0], m[1], m[2], m[3]) for i, m in enumerate(metas)]
    persons = sorted(set(m[1] for m in metas))
    index = CentroidIndex(matrix, rows, persons, _personDisplayNames(), limit,
                          dbFile=dbFile or sqliteCommon.dbFilePath(),
                          skipped=max(0, total - len(metas)))
    return matrix, index


def loadFaceMatrix(faceRows: list) -> object:
    """一批 pb_face 行 -> np.float32[M,512]（跳过脏行，保持行序）。

    调用方需要自己知道**哪几行被跳过了** —— 所以这里不做静默过滤：
    跳过的行在返回矩阵里没有对应位置，行数对不上就是信号。
    """
    out = []
    for row in faceRows or ():
        blob = row.get("embedding") if isinstance(row, dict) else row
        if blob is None:
            continue
        try:
            vec = faceEngine.decodeEmbedding(blob)
        except (TypeError, ValueError):
            continue
        if vec is not None and float(np.linalg.norm(vec)) > 1e-9:
            out.append(vec)
    if not out:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    return np.ascontiguousarray(np.vstack(out), dtype=np.float32)


if __name__ == "__main__":
    print("centroid.py _VERSION:", _VERSION)
    print("向量: float32[%d] 小端 %d 字节" % (EMBEDDING_DIM, EMBEDDING_BYTES))
    print("样本数下限:", MIN_SAMPLES, " 分页行数:", PAGE_ROWS)
    print("质心样本口径: 只用 isConfirmed=1（CONFIRMED_ONLY=%s；"
          "False = 旧口径全样本，仅用于回归对比/冷启动）" % CONFIRMED_ONLY)
    print("兜底桶     : %r（该人全部确认样本；候选桶 = 相邻年代桶 ∪ {%s}）"
          % (ALL_BUCKET, ALL_BUCKET))
    for _name in sorted(basicSettings.MATCH_THRESHOLD_PRESETS):
        _lo, _hi = basicSettings.matchThresholds(_name)
        print("阈值预设 %-12s T_LOW=%.2f T_HIGH=%.2f" % (_name, _lo, _hi))
    _m, _i = loadAllCentroids()
    print("全量质心:", _i.summary())
    for _r in _i:
        print("   ", _r)
