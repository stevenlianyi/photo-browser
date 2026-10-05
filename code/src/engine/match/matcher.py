#! /usr/bin/env python3
#encoding: utf-8

#Filename: matcher.py
#Description: photo-browser 人脸匹配决策（步骤 6）—— 相邻三桶取 max + 三段式判定
#
# 决策链路（开发计划 §3.2，逐字实现）
# ----------------------------------
#   候选桶 = [B0-1, B0, B0+1] 中**所有有样本（sampleCount>=3）的桶**
#            **∪ {ALL}**（兜底桶：该人全部确认样本，不分年代 —— DR-16③）
#   score  = **max**( cosine(face, centroid[bucket]) )      <- 取 max，不取 mean
#   score >= T_HIGH          -> 自动归属（source=0，**isConfirmed=0**）
#   T_LOW <= score < T_HIGH  -> 待人工确认，记 Top-5 候选
#   score <  T_LOW           -> 未知人脸，进聚类（步骤 7）
#
# 为什么候选集合里必须有 ALL（DR-16③，这是本轮返工的一条硬修正）
# ---------------------------------------------------------
#   ① **shotBucket 为空的脸（截图 / EXIF 缺失）原来直接掉进聚类**。
#      neighborBucketKeys("") 返回 []，一张脸都匹配不上 -> 「库里明明有他，
#      这张截图却认不出来」。并入 ALL 之后它的候选桶就是 {ALL}，能拿到分数。
#      代价是**必须重新想清楚"没有拍摄年份"意味着什么**：ALL 桶是"不分年代"的
#      兜底向量，对一张无年份的脸来说它恰恰是**最合适**的候选
#      （无年份 = 无年代信息，而 ALL 正是唯一不含年代假设的向量）。
#   ② 某人的确认样本分散在 2~3 个年代桶、每个都不足 3 张时（早期用户最常见的状态），
#      没有 ALL 桶就**一个桶都启用不了** -> 质心索引里查无此人 -> 自动归属恒为 0。
#   ③ 跨年龄漂移：脸落在 B0，但此人在 B0 附近没有 3 个确认样本，ALL 桶仍能给出
#      一个跨年代的平均分布，保证「认得出」这件事不因样本分布而时灵时不灵。
#   ⚠️ 取 max 的语义不变：ALL 只是**多一个候选行**，不是"优先用它"。
#      谁分数高谁赢，所以 ALL 不会盖掉一个更准的年代桶。
#
# 为什么必须取 max 而不是 mean（硬约束）
# --------------------------------------
#   一张脸只会在**一个**年代桶里最像本人，另一个桶里的向量是"这个人在另一个年纪"
#   （或者根本是别人 —— 3 桶 × 3 万人里混进陌生人的概率不低）。
#   取 mean 会把「本桶很像」和「邻桶不像」平均掉，判出来是"半信半疑"，
#   于是**所有照片都掉进灰区**，待确认队列爆炸，功能等于没有。
#   取 max 的语义才与决策一致：只要有一个年代说得通，就算这个人。
#   代价是 FA 会略升（邻桶里另一个人的高分被当成本人的），
#   这个代价由「Top-5 候选 + 人工确认」兜住，而不是靠改算法。
#
# 规模口径（开发计划 DR-12）
# --------------------------
#   **不引 FAISS / sqlite-vec**：3 万 × 512 float32 = 61MB，numpy 一次矩阵乘
#   3 万列，1000 张脸一批 = 1.5e10 次乘加 ≈ BLAS 单次 0.1~0.3 秒（多线程），
#   远低于「一次特征提取 0.5 秒」。引向量库的收益（10^7 级）在 3 万这个规模是负的：
#   多一个依赖、多一套索引要与库同步、多一个"索引和库不一致"的故障模式。
#   ⚠️ 但**不做无脑全量矩阵乘**：真正该算的是候选桶那几行。
#      一次人脸比对 = 3 个桶键 -> 取出这些桶下的质心行（通常几百行）-> 矩阵乘。
#      这与"全量暴力搜索"结果完全相同（不相交的桶对这张脸不可能成为候选），
#      但算量小 1~2 个数量级，且不需要把 61MB 全部 gather 一遍。
#      CentroidIndex.subset() 做的正是这件事，且带 2 槽缓存。
#
# 硬约束
# ------
#   * **只写库不做 IO**：不读 photoDir、不裁图、不碰缩略图。
#   * 不在 matcher 里写库。判定结果由调用方决定要不要落库
#     （processor.review.assigner 负责），本模块**纯计算**，可反复跑、可复现。
#   * 阈值只从 basicSettings.matchThresholds() 取，不接受散落的字面量。
#
# 可复现性（验收第 4 条）
# ----------------------
#   同一批人脸跑两次结果必须完全一致。三处可能翻车的都已钉死：
#     ① 质心加载按 (personCode, bucketKey) **排序**，不依赖 SQLite 的返回顺序；
#     ② 候选子集的行号**升序去重**，reduceat 段边界因此确定；
#     ③ Top-5 排序键是 (-score, displayName, personCode) 三元组，
#        分数是 float32 位模式，相同时才看后面两项 —— 全序、无并列残留。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/match
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np                                                # noqa: E402

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from engine.face import engine as faceEngine                      # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("matcher", "matcher.log")

#: 三段式决策的三个取值
DECISION_AUTO: str = "auto"          # >= T_HIGH，自动归属
DECISION_REVIEW: str = "review"      # 灰区，进待确认队列
DECISION_CLUSTER: str = "cluster"    # 未知人脸，进聚类（步骤 7）
DECISION_ALL: tuple = (DECISION_AUTO, DECISION_REVIEW, DECISION_CLUSTER)

#: 原因码（**决策之外的第二维信息**：同样是 auto，为什么 auto 必须能解释）
REASON_MATCHED: str = "matched"          # 正常命中候选桶的质心
REASON_NO_BUCKET: str = "no_bucket"      # shotYear 无有效桶 -> 只有 ALL 兜底桶可试，
                                        # 而 ALL 桶里也没有启用的质心（= 无从比较）
REASON_NO_CENTROID: str = "no_centroid"  # 候选桶里没有任何启用的质心（库是空的/都 <3 样本）
REASON_NO_EMBEDDING: str = "no_embedding"# 行里没有向量（脏数据）
REASON_LOW_SCORE: str = "low_score"      # 有候选但 max 分数低于 T_LOW

#: 批量比对时一次处理多少张脸。
#: 理由：临时矩阵是 (M, N)，M=256、N=3 万时 = 256×30000×4B = **30MB**。
#: M 越大越省 gather 次数，但这个临时矩阵是实打实的内存。
#: 256 是「30MB 临时 + 一次 gather」的平衡点。
BATCH_FACES: int = 256

#: Top-N 选取时，若「与第 N 名同分的人」超过这个数，就退化成全量交给 Python 排序。
#: 病态场景（库里塞了一堆重复向量，所有人分数完全相同）才触发；
#: 正常库里同分的人是个位数，5 个候选的挑选成本可以忽略。
_TOP_SCAN_LIMIT: int = 512


# ============================================================
# 一、结果对象
# ============================================================

class Candidate(object):
    """Top-N 候选里的一项。**__slots__**：一批脸 × 5 个候选，别用 dict。"""

    __slots__ = ("personCode", "displayName", "score", "bucketKey")

    def __init__(self, personCode, displayName, score, bucketKey=""):
        self.personCode = str(personCode or "")
        self.displayName = str(displayName or "")
        self.score = float(score)
        self.bucketKey = str(bucketKey or "")

    def toDict(self) -> dict:
        return {"personCode": self.personCode, "displayName": self.displayName,
                "score": round(self.score, 4), "bucketKey": self.bucketKey}

    def __repr__(self):
        return "Candidate(%s/%s %.4f@%s)" % (self.personCode, self.displayName,
                                            self.score, self.bucketKey)


class MatchResult(object):
    """一张脸的判定结果。

    字段
    ----
      faceCode / photoCode : 原样带回（落库时要用）
      bucketKey            : 这张脸所在桶（空 = 不可比）
      candidateBuckets     : 实际参与的候选桶键（升序）
      decision             : DECISION_AUTO / REVIEW / CLUSTER
      reason               : 原因码
      score                : max 余弦；无候选时为 None（**不是 0**）
      personCode           : AUTO 才有值（= Top-1 的人）
      topCandidates        : list[Candidate]，长度 <= topN
      tLow / tHigh         : 本次判定实际用的阈值（**记进结果**，
                             否则事后无法解释「为什么这张脸没自动归属」）
    """

    __slots__ = ("faceCode", "photoCode", "bucketKey", "candidateBuckets",
                 "decision", "reason", "score", "personCode", "topCandidates",
                 "tLow", "tHigh", "preset")

    def __init__(self, faceCode="", photoCode="", bucketKey="",
                 candidateBuckets=(), decision=DECISION_CLUSTER,
                 reason=REASON_LOW_SCORE, score=None, personCode="",
                 topCandidates=(), tLow=0.0, tHigh=0.0, preset=""):
        self.faceCode = str(faceCode or "")
        self.photoCode = str(photoCode or "")
        self.bucketKey = str(bucketKey or "")
        self.candidateBuckets = list(candidateBuckets)
        self.decision = str(decision)
        self.reason = str(reason)
        self.score = (None if score is None else float(score))
        self.personCode = str(personCode or "")
        self.topCandidates = list(topCandidates)
        self.tLow = float(tLow)
        self.tHigh = float(tHigh)
        self.preset = str(preset or "")

    @property
    def isAuto(self) -> bool:
        return self.decision == DECISION_AUTO

    @property
    def isReview(self) -> bool:
        return self.decision == DECISION_REVIEW

    @property
    def isCluster(self) -> bool:
        return self.decision == DECISION_CLUSTER

    def toDict(self) -> dict:
        return {
            "faceCode": self.faceCode, "photoCode": self.photoCode,
            "bucketKey": self.bucketKey or None,
            "candidateBuckets": list(self.candidateBuckets),
            "decision": self.decision, "reason": self.reason,
            "score": (None if self.score is None else round(self.score, 4)),
            "personCode": self.personCode or None,
            "topCandidates": [c.toDict() for c in self.topCandidates],
            "tLow": self.tLow, "tHigh": self.tHigh, "preset": self.preset,
        }

    def __repr__(self):
        return ("MatchResult(%s %s score=%s %s top=%d)"
                % (self.faceCode or "-", self.decision,
                   "-" if self.score is None else "%.4f" % self.score,
                   self.reason, len(self.topCandidates)))


# ============================================================
# 二、纯判定（可单测，不碰数据库）
# ============================================================

def decide(score, tLow: float = None, tHigh: float = None,
           preset: str = None) -> tuple:
    """分数 -> (decision, reason)。**三段式的全部逻辑就在这 4 行。**

    边界用 ">=" / "<" 而不是 ">" / "<="：
      score == tHigh 算自动归属（>= 口径，与 S3/MVP_plan 的写法一致）
      score == tLow  算进聚类（< 口径）
      两者不一致不是笔误：tHigh 是"我敢自动认"的证据线，tLow 是"我敢说没见过"的证据线，
      都取闭区间端点等于把证据线本身也算成证据。
    """
    low, high = _thresholds(tLow, tHigh, preset)
    if score is None:
        return DECISION_CLUSTER, REASON_NO_CENTROID
    value = float(score)
    if value >= high:
        return DECISION_AUTO, REASON_MATCHED
    if value >= low:
        return DECISION_REVIEW, REASON_MATCHED
    return DECISION_CLUSTER, REASON_LOW_SCORE


def _thresholds(tLow=None, tHigh=None, preset: str = None) -> tuple:
    """(tLow, tHigh) 解析。给了就用给的，否则走 basicSettings 当前预设。"""
    if tLow is None or tHigh is None:
        dLow, dHigh = basicSettings.matchThresholds(preset)
        tLow = dLow if tLow is None else float(tLow)
        tHigh = dHigh if tHigh is None else float(tHigh)
    return float(tLow), float(tHigh)


#: 分数量化位数（用于**排序与上报**）。
#:
#: 为什么要量化而不是直接比 float32
#: --------------------------------
#:   BLAS 的矩阵乘对**数学上完全相同**的两行也可能给出差 1 个 ULP 的结果
#:   （SIMD 分道 / 内存对齐不同 -> 累加顺序不同）。实测：同一份质心被 5 个人共用时，
#:   5 个人的分数里有一个是 0.993893027305603、其余是 0.9938929677009583。
#:   于是「同分 -> 按 displayName 排」这条规则**根本不会触发**，
#:   候选顺序由 1e-7 的数值噪声决定 —— 换台机器 / 换个 BLAS 线程数就可能换顺序。
#:   把分数量化到 1e-6 再排序，噪声被抹平，这条规则才真的生效。
#:   顺带的好处：与库里存的值一致（confidence 是 DECIMAL(6,4)，本来就量化过），
#:   上报给 UI 的分数与入库的分数不会在末位打架。
#:   ⚠️ 1e-6 远小于两档阈值的最小间隔（本项目是 0.01 级别），**不影响任何决策**。
SCORE_DECIMALS: int = 6


def quantizeScore(score):
    """分数量化到 SCORE_DECIMALS 位（排序与上报统一走这里）"""
    if score is None:
        return None
    return round(float(score), SCORE_DECIMALS)


def rankCandidates(scoredPeople: dict, displayNames: dict = None,
                   topN: int = None) -> list:
    """{personCode: score} -> Top-N 候选列表（**稳定**：同分按 displayName，再按 personCode）。

    为什么要 displayName 而不是 personCode 兜底
    ----------------------------------------
      personCode 是 'P_' + uuid4 之类的随机串，按它排出来的同分顺序对用户是随机的，
      重跑一次顺序就变 —— 而待确认队列是给人看的，顺序抖动会被当成"结果不可信"。
      displayName 是人给的、稳定可读的排序依据；真撞名（两人都叫"妈妈"）时
      再用 personCode 兜底，那才是纯技术性 tie-break。

    ⚠️ **分数先量化再排序**（quantizeScore，见 SCORE_DECIMALS）：
       不量化的话，"同分"这个前提在 BLAS 的 1 ULP 噪声下根本不成立，
       displayName 这条规则会形同虚设。

    ⚠️ displayName 排的是 **Unicode 码位序**，不是拼音序。
       这是有意的：同分只是兜底排序（分数才是主排序），为它引 pypinyin 不划算；
       而码位序是稳定全序 —— 「两次跑一定一样」比「按拼音看起来顺」更重要。
       中文用户看到的顺序未必顺，但可复现才是这条规则存在的理由。
    """
    limit = int(topN or basicSettings.MATCH_TOP_CANDIDATES)
    names = displayNames or {}
    items = [(str(code), quantizeScore(score))
             for code, score in (scoredPeople or {}).items()]
    items.sort(key=lambda it: (-it[1], str(names.get(it[0], "")), it[0]))
    return [Candidate(code, names.get(code, ""), score) for code, score in items[:limit]]


def bucketKeyOfFace(face: dict) -> str:
    """取人脸所在桶键。**只认 shotBucket 这一列**。

    为什么不现场用 shotYear 重算：shotBucket 是**落库那一刻**写下的口径，
    而 pb_face 没有 shotYear 列（只有 photoCode，得回查 pb_photo）。
    质心是按 shotBucket 建的，匹配就必须按同一列取 —— 两边口径不一致时，
    最坏情况是这张脸去和"另一个年份区间"的人比，
    分数看着正常（0.4 左右），但语义完全错了，而且**不报错**。
    """
    if isinstance(face, dict):
        return str(face.get("shotBucket") or "")
    return str(face or "")


def candidateBucketKeys(bucketKey: str, neighbor: int = None) -> list:
    """这张脸的**候选桶集合**（升序、去重）：`[B0-1, B0, B0+1] ∪ {ALL}`。

    DR-16③ 的唯一入口（match 与 matchMany 都走它，两处不许分叉）。

    三个边界
    --------
      * bucketKey 为空（截图 / EXIF 缺失）-> neighborBucketKeys("") 返回 []
        -> 候选桶就是 `["ALL"]`。**这类脸现在也能匹配了**，
        不再因为"没有拍摄年份"直接掉进聚类（那正是本轮修掉的 bug）。
      * ALL 恒定并入，与 bucketKey 无关：它是兜底向量，不是"某一个年代"。
      * 返回**升序**（ALL 排在最前，'A' < '1'）：候选顺序影响同分时的稳定性，
        批量分组也依赖它可哈希（matchMany 用它当 dict 的 key）。
    """
    keys = set(bucket.neighborBucketKeys(bucketKey, neighbor))
    keys.add(bucket.ALL_BUCKET)
    return sorted(keys)


def faceEmbedding(face):
    """从 pb_face 行 / 2048 字节 / ndarray 里取出归一化向量。取不到返回 None。"""
    if face is None:
        return None
    if isinstance(face, np.ndarray):
        vec = face
    elif isinstance(face, (bytes, bytearray, memoryview)):
        try:
            vec = faceEngine.decodeEmbedding(face)
        except (TypeError, ValueError):
            return None
    elif isinstance(face, dict):
        blob = face.get("embedding")
        if blob is None:
            return None
        if isinstance(blob, (bytes, bytearray, memoryview)):
            try:
                vec = faceEngine.decodeEmbedding(blob)
            except (TypeError, ValueError):
                return None
        else:
            vec = blob
    else:
        return None
    if vec is None:
        return None
    vec = np.asarray(vec, dtype=np.float32).ravel()
    if vec.size != basicSettings.EMBEDDING_DIM:
        return None
    return vec


def _personScoresFromRows(simRow, subset) -> object:
    """逐行分数 (N,) -> 逐人分数 (P,)，按人取 **max**（硬约束）。"""
    if subset.personCount == len(subset):        # 每人只有一行，省一次 reduce
        return simRow
    return np.maximum.reduceat(simRow, subset.starts)


def _selectTopPeople(personScores, topN: int) -> object:
    """挑出「可能进 Top-N」的人的下标。

    为什么不全排序
    --------------
      候选桶下可能有 3 万个人。全排序是每张脸 3 万 × log(3万) ≈ 45 万次比较，
      1 万张脸就是 4.5 亿次 —— 比矩阵乘本身贵一个数量级，而矩阵乘才是"正事"。

    为什么 cut 这一刀是**精确**的
    --------------------------
      cut = 第 topN 高的分数。`personScores >= cut` 取出来的集合，
      恰好包含「所有可能进 Top-N 的人」加上「与第 topN 名同分的人」。
      同分的处理本来就交给 rankCandidates 的 (-score, displayName, personCode)
      三元组，所以多带几个同分的人进来**不影响结果**，只影响排序规模。
      ⇒ Top-N 结果与"全排序后取前 N"**逐位相同**。
    """
    total = int(personScores.shape[0])
    if total == 0:
        return np.zeros((0,), dtype=np.int64)
    if total <= topN:
        return np.arange(total, dtype=np.int64)
    cutIndex = total - topN
    cut = float(np.partition(personScores, cutIndex)[cutIndex])
    sel = np.flatnonzero(personScores >= cut)
    if sel.size > _TOP_SCAN_LIMIT:
        # 全员同分这种病态情况（库里只有重复向量之类）：直接全交出去，
        # 正确性优先于速度 —— 这种情况下一张脸慢一点无所谓
        return np.arange(total, dtype=np.int64)
    return sel


def _topFromRowScores(simRow, subset, topN: int) -> tuple:
    """逐行分数 -> (bestPersonIdx, bestScore, topCandidates)。

    候选与命中桶键一起产出：**不要**再回头逐行扫矩阵去找"是哪一行命中的"
    （那是 O(N) 次 512 维点积 = 每张脸几十毫秒，比打分本身贵 100 倍，
    而且代码上一眼看不出来，只表现为"匹配跑得特别慢"）。
    正确做法：人的行本来就是连续段（CentroidSubset 的前提），
    在**已经算好的** simRow 那一段里取 argmax 就行。
    """
    personScores = _personScoresFromRows(simRow, subset)
    if personScores.size == 0:
        return -1, 0.0, []
    bestAt = int(np.argmax(personScores))
    bestScore = quantizeScore(personScores[bestAt])
    sel = _selectTopPeople(personScores, topN)
    picked = {}
    for p in sel:
        p = int(p)
        lo, hi = subset.rowsOf(p)
        if hi <= lo:
            continue
        local = simRow[lo:hi]
        at = int(np.argmax(local))
        picked[subset.personCodes[p]] = (quantizeScore(local[at]),
                                         subset.bucketKeys[lo + at])
    top = rankCandidates(dict((code, v[0]) for code, v in picked.items()),
                         subset.displayNames, topN)
    for one in top:
        one.bucketKey = picked.get(one.personCode, (0.0, ""))[1]
    return bestAt, bestScore, top


# ============================================================
# 三、单张匹配
# ============================================================

def match(face, matrix=None, index=None, tLow: float = None, tHigh: float = None,
          preset: str = None, topN: int = None, neighbor: int = None) -> MatchResult:
    """判定一张脸。**纯计算**（index 已加载时全程不碰数据库）。

    参数
    ----
      face    : pb_face 行 dict（要 faceCode/photoCode/shotBucket/embedding）
      matrix  : loadAllCentroids() 的第一个返回值；不传则从 index.matrix 取
      index   : CentroidIndex；**不传就现场 loadAllCentroids()**（单测/CLI 方便，
                批量场景务必自己加载一次传进来，否则每张脸都重载一次库）
      preset  : 阈值预设名；None = basicSettings 当前预设

    决策
    ----
      无向量             -> cluster / no_embedding
      候选桶无启用质心   -> cluster / no_centroid
                          （无 shotBucket 且 ALL 桶也空 -> cluster / no_bucket）
      否则 score=max(相邻年代桶 + ALL 兜底桶的全部质心)，按三段式落档
    """
    low, high = _thresholds(tLow, tHigh, preset)
    limit = int(topN or basicSettings.MATCH_TOP_CANDIDATES)
    presetName = preset or basicSettings.MATCH_THRESHOLD_PRESET
    faceCode = str((face or {}).get("faceCode") or "") if isinstance(face, dict) else ""
    photoCode = str((face or {}).get("photoCode") or "") if isinstance(face, dict) else ""
    # ⚠️ 局部变量**不能**叫 bucket：同名会遮蔽 engine.match.bucket 模块，
    #    紧接着的 bucket.neighborBucketKeys 就变成 str 的属性 -> AttributeError。
    #    这种错在只跑一次时看不出来（第一次调用时模块还没被遮蔽），
    #    而 matchMany 是在循环里逐张脸调用 —— 于是"第一张脸算对了，
    #    从第二张开始全炸"，排查起来非常莫名其妙。
    bucketKey = bucketKeyOfFace(face)

    vec = faceEmbedding(face)
    if vec is None:
        return MatchResult(faceCode, photoCode, bucketKey,
                           candidateBucketKeys(bucketKey, neighbor),
                           DECISION_CLUSTER, REASON_NO_EMBEDDING, None, "", [],
                           low, high, presetName)

    if index is None:
        matrix, index = centroid.loadAllCentroids()
    mat = matrix if matrix is not None else index.matrix
    candidates = candidateBucketKeys(bucketKey, neighbor)
    subset = index.subset(candidates)
    if subset.personCount == 0:
        # ⚠️ 原因码要区分「没有年代可比」与「有年代但库里没质心」：
        #    DR-16 之后无 shotBucket 的脸**仍会尝试**（候选桶 = {ALL}），
        #    只有连 ALL 都没有启用的质心时才真的无从比较 —— 这时 no_bucket 更贴切。
        return MatchResult(faceCode, photoCode, bucketKey, candidates,
                           DECISION_CLUSTER,
                           REASON_NO_BUCKET if not bucketKey else REASON_NO_CENTROID,
                           None, "", [], low, high, presetName)

    simRow = np.asarray(subset.matrix, dtype=np.float32) @ vec.astype(np.float32)
    bestAt, bestScore, top = _topFromRowScores(simRow, subset, limit)
    decision, reason = decide(bestScore, low, high)
    return MatchResult(faceCode, photoCode, bucketKey, candidates, decision, reason,
                       bestScore,
                       subset.personCodes[bestAt] if decision == DECISION_AUTO else "",
                       top, low, high, presetName)


# ============================================================
# 四、批量匹配（矩阵乘）
# ============================================================

def matchMany(faces: list, matrix=None, index=None, tLow: float = None,
              tHigh: float = None, preset: str = None, topN: int = None,
              neighbor: int = None, batchFaces: int = None) -> list:
    """一批脸一次性比对。返回与 faces **等长同序**的 MatchResult 列表。

    同长同序是硬要求：调用方拿着这个列表 zip 回自己的数据去写库，
    顺序一旦对不上就会把 A 的脸写成 B 的归属，而且**不报错**。
    脏行（无桶/无向量）在结果列表里占着位置（decision=cluster），
    **绝不静默缩短** —— 静默缩短是这类库最典型的数据错位来源。

    分组
    ----
      按「候选桶键集合」分组：同一组共用一次 subset 拷贝与一次矩阵乘。
      候选桶集合 = `[B0-1, B0, B0+1] ∪ {ALL}`（candidateBucketKeys），所以
      真实库里绝大多数脸落在同一两个年代桶 + ALL，分组后
      1000 张脸通常只做 1~2 次矩阵乘。

    分片
    ----
      组内再按 batchFaces 切片，避免 (M, P) 临时矩阵过大（见 BATCH_FACES 注释）。
    """
    low, high = _thresholds(tLow, tHigh, preset)
    limit = int(topN or basicSettings.MATCH_TOP_CANDIDATES)
    step = int(batchFaces or BATCH_FACES)
    presetName = preset or basicSettings.MATCH_THRESHOLD_PRESET
    faceList = list(faces or ())
    results = [None] * len(faceList)
    if not faceList:
        return results

    if index is None:
        matrix, index = centroid.loadAllCentroids()
    mat = matrix if matrix is not None else index.matrix

    # ---- 第 1 遍：解析每张脸，分组 ----
    groups = {}                                   # 候选桶元组 -> [faceIndex, ...]
    for i, one in enumerate(faceList):
        faceCode = str(one.get("faceCode") or "") if isinstance(one, dict) else ""
        photoCode = str(one.get("photoCode") or "") if isinstance(one, dict) else ""
        bucketKey = bucketKeyOfFace(one)          # 别名见 match() 里的遮蔽警告
        # ⚠️ 这里**不再**按shotBucket 为空提前判 no_bucket（DR-16③）：
        #    没有拍摄年份的脸候选桶就是 {ALL}（兜底桶），照样能比——
        #    原来在这个分支直接 return，等于让**所有截图里的人脸**永远认不出来。
        vec = faceEmbedding(one)
        if vec is None:
            results[i] = MatchResult(faceCode, photoCode, bucketKey,
                                     candidateBucketKeys(bucketKey, neighbor),
                                     DECISION_CLUSTER, REASON_NO_EMBEDDING, None, "",
                                     [], low, high, presetName)
            continue
        keys = tuple(candidateBucketKeys(bucketKey, neighbor))
        groups.setdefault(keys, []).append((i, vec, faceCode, photoCode, bucketKey))

    # ---- 第 2 遍：每组一次 subset + 分片矩阵乘 ----
    for keys, items in groups.items():
        subset = index.subset(list(keys))
        if subset.personCount == 0:
            for i, _vec, faceCode, photoCode, bk in items:
                results[i] = MatchResult(faceCode, photoCode, bk, list(keys),
                                         DECISION_CLUSTER,
                                         REASON_NO_BUCKET if not bk
                                         else REASON_NO_CENTROID,
                                         None, "", [], low, high, presetName)
            continue
        for begin in range(0, len(items), step):
            chunk = items[begin:begin + step]
            query = np.ascontiguousarray(
                np.vstack([c[1] for c in chunk]), dtype=np.float32)
            # (M, N)：一次矩阵乘打完这一片所有脸 x 所有候选桶质心
            sims = query @ subset.matrix.T
            for row, (i, _vec, faceCode, photoCode, bk) in enumerate(chunk):
                bestAt, bestScore, top = _topFromRowScores(sims[row], subset, limit)
                decision, reason = decide(bestScore, low, high)
                results[i] = MatchResult(
                    faceCode, photoCode, bk, list(keys), decision, reason,
                    bestScore,
                    subset.personCodes[bestAt] if decision == DECISION_AUTO else "",
                    top, low, high, presetName)
    return results


# ============================================================
# 五、汇总
# ============================================================

def summarize(results: list) -> dict:
    """一批判定的分布统计（供 CLI / 验收脚本直接打印）。"""
    out = {"total": len(results or ()), "auto": 0, "review": 0, "cluster": 0}
    out["byReason"] = {}
    for one in results or ():
        out[one.decision] = out.get(one.decision, 0) + 1
        out["byReason"][one.reason] = out["byReason"].get(one.reason, 0) + 1
    return out


if __name__ == "__main__":
    print("matcher.py _VERSION:", _VERSION)
    for _name in sorted(basicSettings.MATCH_THRESHOLD_PRESETS):
        _lo, _hi = basicSettings.matchThresholds(_name)
        print("阈值预设 %-12s T_LOW=%.2f  [%.2f,%.2f) 灰区  T_HIGH=%.2f 自动归属"
              % (_name, _lo, _lo, _hi, _hi))
    _matrix, _index = centroid.loadAllCentroids()
    print("质心:", _index.summary())
    print("相邻桶宽: 童年 %d 年 / 成年 %d 年 / 降级 %d 年"
          % (bucket.CHILD_WIDTH, bucket.ADULT_WIDTH, bucket.EQUAL_WIDTH))
    print("候选桶  : 相邻年代桶 ∪ {%s}（兜底桶）" % bucket.ALL_BUCKET)
    print("样例:", match({"faceCode": "fc_demo", "shotBucket": "2015-2017"}))
    print("样例(无拍摄年份，候选桶=%s):"
          % candidateBucketKeys(""),
          match({"faceCode": "fc_shot", "shotBucket": None,
                 "embedding": faceEngine.encodeEmbedding(
                     np.zeros(basicSettings.EMBEDDING_DIM, dtype=np.float32) + 1.0)}))
