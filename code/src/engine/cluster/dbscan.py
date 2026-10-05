#! /usr/bin/env python3
#encoding: utf-8

#Filename: dbscan.py
#Description: photo-browser 未归类人脸聚类（步骤 7）——纯 numpy DBSCAN + clusterCode 幂等
#
# 做什么
# ------
#   把**识别不出来的人脸**（personCode IS NULL AND isStranger=0）聚成若干簇，
#   每个簇写一个 clusterCode 到 pb_face.clusterCode（Q-3：**不建pb_face_cluster 表**）。
#   簇 = 「很可能是同一个人但库里还没有这个人」的候选，用户在待确认队列里
#   一次性处理一簇，而不是逐张脸点。
#
# 只对未归类集合聚类（硬约束）
# --------------------------
#   聚类集合 = `personCode IS NULL AND isStranger=0 AND delFlag='0'`。
#   ① **已归类人脸（personCode 非空）绝不能被重新聚类**：
#      它们已经绑在某个人身上，簇信息对它们没有意义；而一旦把已归类的脸
#      拉进来重排簇，用户早上刚确认完的「妈妈」下午就换了簇编号 ——
#      界面上表现为「我刚点的确认丢了」，是这类工具最劝退的一类故障。
#   ② **isStranger=1 的脸既不进队列也不进聚类**：陌生人标记是用户的**永久排除**
#      决定（DR-16①），把它拉回聚类等于推翻用户的操作。
#   ③ **不写 photoDir**：本模块只读 embedding，不裁图、不碰缩略图。
#   ④ **不写 pb_review_log**：聚类是**机器行为**，不是人工操作。
#      pb_review_log 的语义是「这张脸当初怎么被认成这个人的」的唯一排障依据
#      （数据库设计.md §4.9），灌入几万条机器聚类日志只会淹没真正要查的那几条。
#
# 为什么用余弦距离而不是欧氏距离
# ----------------------------
#   embedding 是 L2 归一化过的单位向量，这个前提下：
#     * 余弦相似度 = 点积
#     * **余弦距离 = 1 - 点积**（欧氏距离恰好等于 √2 × 余弦距离，是单调的仿射变换）
#   所以「余弦距离 <= eps」等价于「点积 >= 1 - eps」，邻居判定可以**一次矩阵乘**
#   全部算完 —— 这才是能在这个规模上跑起来的关键（matcher.py 的同一套理由）。
#   ⚠️ 本模块仍会**自己再归一化一次**：不依赖「调用方已经归一化过」这个前提。
#      embedding 若哪天被手工塞进库、或是老版本写的未归一化值，
#      少了这一步 cos 会算出大于 1 的相似度，邻居关系静默错乱且**不报错**。
#
# 为什么要分块（规模）
# ------------------
#   DBSCAN 是 O(N²) 的算法，峰值内存 = 分片行数 × N × 4 字节。
#   一次性算全量相似度矩阵：N=10 万时是 10^10 个 float32 = 40GB，必炸。
#   所以按 DBSCAN_CHUNK_ROWS 分片算，且**按 N 自动收缩分片**
#   （DBSCAN_SIM_CELLS 上限，约 10MB 临时矩阵）——
#   内存峰值不随库规模漂移，这是步骤 3（DR-12分页）与步骤 6（惰性加载质心）
#   反复强调的那条纪律，在聚类上的对应物。
#   邻接关系按 CSR 思路存成「每行一个下标数组」，内存 O(边数) 而非 O(N²)。
#   ⚠️ 边数是真正会炸的东西：库里若有一大批互相都近似的重复向量，
#      边数会从 O(N) 涨到 O(N²)。DBSCAN_MAX_EDGES 就是这个护栏 ——
#      **明确报错让人来调 eps**，远好过把内存吃光换一堆垃圾簇。
#
# clusterCode 幂等（验收第 3 条）
# ------------------------------
#   编码规则：`cluster_<sha256(排序后的 faceCode 列表)[:12]>`。
#   **内容寻址**：同一个簇（成员集合相同）重跑一定得到同一个编码，
#   不依赖运行时间、不依赖随机数、不依赖库里已有的任何行。
#   这比「自增序号」或「uuid」强得多 —— 后两者重跑就会产生新编码，
#   而 clusterCode 已经被步骤 11 的「同一簇批量改判」当成主键在用。
#   ⚠️ 成员集合**变了**编码就会变（内容寻址的必然）。这不是缺陷：
#      簇里多了一张脸，它本来就是另一个对象了。旧编码会随本次运行被整体覆盖，
#      **不会**留下孤儿编码（每次运行都写全量未归类集合）。
#   ⚠️ 已经归类的脸**不清 clusterCode**：它们退出了聚类集合，但「这张脸当初
#      和哪些脸聚在一起」对「簇内批量改判」仍然有用，清掉反而丢信息。
#
# 硬约束
# ------
#   * **纯计算**：本模块不import database.*、不写库、不读库。
#     读集合（clustering.py）与写clusterCode（clustering.py）是另一层的事。
#   * 不裁图、不碰 photoDir、不碰缩略图。
#   * eps / min_samples 一律从 basicSettings 取默认值，允许调用方覆盖。
#   * 同长同序：clusterCodes() 返回的列表与传入的 faceCodes **逐位对应**，
#     绝不静默缩短（静默缩短 = 把 A 的脸写成 B 的簇，且不报错）。

import hashlib
import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/cluster
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np                                                # noqa: E402

from common import miscCommon as misc                             # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("dbscan", "dbscan.log")

#: 噪声点标签。DBSCAN 的约定：不属于任何簇。
#:
#: ⚠️ **落库时噪声点的 clusterCode 写 NULL（而不是这个字符串）**：
#:    「不属于任何簇」与「未归属」一样，用**空值**表达才自洽 ——
#:    库里 clusterCode IS NULL 的行 = 「没进任何簇」，
#:    而 `_NOISE_` 只供查询层/前端需要非空字符串时占位
#:    （comGD.CLUSTER_NOISE 就是这个用途，clustering.py 会原样带出去）。
#:    写成字符串的代价是「查未聚类的脸」这条 SQL 从 `IS NULL` 变成
#:    `NOT IN ('_NOISE_', 'cluster_...')` —— 枚举漏一个就错一批。
LABEL_NOISE: int = -1

#: 内部用的「还没被看过」标记（扩展结束后必须全部变成 LABEL_NOISE）
LABEL_UNVISITED: int = -2


class ClusterError(Exception):
    """聚类前置条件不满足（规模护栏触发、维度不对……）。

    刻意**不静默降级**：宁可让调用方看到明确失败，
    也不要返回一个「看起来跑完了」的错簇集合。
    """


# ============================================================
# 一、结果对象
# ============================================================

class Cluster(object):
    """一个簇。**__slots__**：几万簇时别用 dict。"""

    __slots__ = ("clusterCode", "faceCodes", "size", "representativeFaceCode",
                 "representativeDetScore", "centroid", "meanSimilarity")

    def __init__(self, clusterCode, faceCodes, representativeFaceCode="",
                 representativeDetScore=None, centroid=None, meanSimilarity=None):
        self.clusterCode = str(clusterCode or "")
        self.faceCodes = list(faceCodes or ())
        self.size = len(self.faceCodes)
        self.representativeFaceCode = str(representativeFaceCode or "")
        self.representativeDetScore = representativeDetScore
        self.centroid = centroid
        self.meanSimilarity = meanSimilarity

    def toDict(self) -> dict:
        return {"clusterCode": self.clusterCode, "size": self.size,
                "faceCodes": list(self.faceCodes),
                "representativeFaceCode": self.representativeFaceCode or None,
                "representativeDetScore": self.representativeDetScore,
                "meanSimilarity": self.meanSimilarity}

    def __repr__(self):
        return "Cluster(%s n=%d rep=%s)" % (self.clusterCode, self.size,
                                             self.representativeFaceCode or "-")


# ============================================================
# 二、归一化
# ============================================================

def normalizeRows(matrix):
    """按行 L2 归一化；**零向量行原样保留**（由调用方在装载时剔除）。

    为什么这里不丢零行
    ------------------
      丢行会让「返回的下标」与「传入的行」错位，而本模块所有 API 都承诺
      **下标与入参逐位对应**。要丢就在装载阶段丢（clustering.loadFaceMatrix
      已经是这个口径），算法层只负责把向量摆正。
    """
    arr = np.asarray(matrix, dtype=np.float32)
    if arr.ndim != 2 or arr.size == 0:
        return arr.reshape((0, basicSettings.EMBEDDING_DIM)).astype(np.float32)
    norm = np.linalg.norm(arr, axis=1, keepdims=True)
    safe = np.where(norm < 1e-9, 1.0, norm)          # 零向量不除，留在原处
    return np.ascontiguousarray(arr / safe, dtype=np.float32)


# ============================================================
# 三、邻接关系（分块矩阵乘）
# ============================================================

def chunkRowsFor(count: int, configured: int = None) -> int:
    """分片行数：取「配置上限」与「按 N 收缩」里更小的那个。

    为什么要收缩
    ------------
      临时相似度矩阵是 (分片行数 × N) 个 float32。不收缩的话，
      N 从 1000 涨到 100000 时峰值从 1MB 涨到 100MB ——
      而这正是 DR-12 在步骤 3/6 反复治的病（规模一涨内存就顶爆验收指标）。
      DBSCAN_SIM_CELLS 是**与 N 无关**的内存预算，按它反解分片行数，
      峰值就锁死在约 10MB，与库规模无关。
    """
    total = max(1, int(count or 0))
    limit = int(configured or basicSettings.DBSCAN_CHUNK_ROWS)
    limit = max(1, limit)
    budget = max(1, int(basicSettings.DBSCAN_SIM_CELLS) // total)
    return max(1, min(limit, budget))


def neighborGraph(vectors, eps: float, chunkRows: int = None,
                  maxEdges: int = None) -> list:
    """每行的邻居下标（**不含自己**），int32 数组的list。

    参数
    ----
      vectors : 已归一化的 (N, D) float32
      eps     : 余弦距离阈值。邻居判定 =点积 >= 1 - eps
      maxEdges: 边数上限，超过抛 ClusterError（不静默截断）

    为什么用 `>= 1 - eps` 而不是算距离
    -------------------------------
      距离 = 1 - 点积 是**单调递减**的仿射变换，阈值可以直接搬过去，
      省掉一次 (N, N) 的临时距离矩阵（内存直接省一半）。
      边界口径：距离恰好等于 eps 算邻居（`<=`），所以点积用 `>=`。
      这与 matcher.decide() 的边界纪律一致（分数相等时往「有证据」那边靠）。
    """
    arr = np.asarray(vectors, dtype=np.float32)
    total = int(arr.shape[0]) if arr.ndim == 2 else 0
    if total == 0:
        return []
    threshold = 1.0 - float(eps)
    step = chunkRowsFor(total, chunkRows)
    edgeCap = int(maxEdges if maxEdges is not None else basicSettings.DBSCAN_MAX_EDGES)
    out = [None] * total
    edges = 0
    for begin in range(0, total, step):
        end = min(total, begin + step)
        # (分片行数 × N)：一次矩阵乘算完这一片对全体的相似度
        sims = arr[begin:end] @ arr.T
        mask = sims >= threshold
        for offset in range(end - begin):
            i = begin + offset
            row = mask[offset]
            row[i] = False                      # 自己不是自己的邻居
            picked = np.flatnonzero(row).astype(np.int32, copy=False)
            out[i] = picked
            edges += int(picked.size)
        del sims, mask
        if edgeCap > 0 and edges > edgeCap:
            raise ClusterError(
                "邻接边数超过上限 %d（已算到 %d 条，规模 N=%d）。"
                "这是「库里有一大批互相都很近似的向量」的典型征兆"
                "（重复向量 / 同一个人的大量近重复照片）。"
                "请调大 --eps 让阈值更严，或先用 --limit 分批聚类。" % (edgeCap, edges, total))
    return out


# ============================================================
# 四、DBSCAN 本体
# ============================================================

def _expandLabels(neighbors: list, minSamples: int) -> object:
    """邻接表 -> 标签数组（经典 DBSCAN 的队列扩展，噪声点标签 LABEL_NOISE）。

    为什么自己写而不用「两两比距离 + 存 N×N 矩阵」
    ---------------------------------------------
      N=1 万时 N×N 的 float32 是 400MB，int8 也是 100MB ——
      都是「为了算一次聚类先把内存吃光」。邻接表（CSR 思路）只要 O(边数)。

    扩展顺序与结果无关（可复现）
    --------------------------
      DBSCAN 的簇划分**与访问顺序无关**：密度可达关系是等价关系，
      同一连通分量必得同一个标签，不同连通分量必得不同标签。
      标签**编号**会随顺序变（所以 dbscan() 之后还要 canonicalize），
      但「谁和谁在一起」不变 —— 而 clusterCode 是内容寻址的，
      所以编号抖动不会污染 clusterCode。
    """
    total = len(neighbors)
    labels = np.full(total, LABEL_UNVISITED, dtype=np.int32)
    need = int(minSamples)
    clusterId = 0
    for seed in range(total):
        if labels[seed] != LABEL_UNVISITED:
            continue
        # min_samples **含自己**（sklearn 口径）：邻居数 + 1 >= min_samples
        if int(neighbors[seed].size) + 1 < need:
            labels[seed] = LABEL_NOISE
            continue
        labels[seed] = clusterId
        stack = [int(x) for x in neighbors[seed]]
        while stack:
            at = stack.pop()
            if labels[at] == LABEL_NOISE:
                # 先前被判成噪声，但被当前簇的某个核心点够到 -> 边界点，归入本簇
                labels[at] = clusterId
                continue
            if labels[at] != LABEL_UNVISITED:
                continue
            labels[at] = clusterId
            if int(neighbors[at].size) + 1 >= need:
                stack.extend(int(x) for x in neighbors[at])
        clusterId += 1
    # 兜底：正常走完不会有残留（每个下标都在上面被赋过值），
    # 但真出现时按噪声处理，绝不把 -2 写进库里
    if bool((labels == LABEL_UNVISITED).any()):
        _LOG.warning("标签扩展后仍有 %d 个未访问下标，按噪声处理",
                     int((labels == LABEL_UNVISITED).sum()))
        labels[labels == LABEL_UNVISITED] = LABEL_NOISE
    return labels


def _labelsByNumpy(vectors, eps: float, minSamples: int, chunkRows: int = None,
                   maxEdges: int = None) -> object:
    neighbors = neighborGraph(vectors, eps, chunkRows=chunkRows, maxEdges=maxEdges)
    return _expandLabels(neighbors, minSamples)


def _labelsBySklearn(vectors, eps: float, minSamples: int) -> object:
    """sklearn 后端（**可选**，装了才用；没装会明确报错而不是悄悄换算法）。

    ⚠️ metric="cosine" + algorithm="brute"：向量已归一化，cosine 与
       euclidean 在此前提下是单调仿射关系，**簇划分应与自实现一致**；
       brute是唯一不依赖近邻索引近似的路径（可复现性要求）。
       这一致性由test_dbscan.py 的后端对照用例盯着。
    """
    try:
        from sklearn.cluster import DBSCAN as _SkDBSCAN
    except ImportError as e:                     # pragma: no cover
        raise ClusterError("指定了 sklearn 后端但没装 scikit-learn: %s" % e)
    model = _SkDBSCAN(eps=float(eps), min_samples=int(minSamples),
                      metric="cosine", algorithm="brute")
    return np.asarray(model.fit_predict(np.asarray(vectors, dtype=np.float32)),
                      dtype=np.int32)


def resolveBackend(backend: str = None) -> str:
    """后端选择。auto = 有 sklearn 用 sklearn，否则 numpy。

    探测结果**每次都真去试 import**（不缓存到模块变量）：
    sklearn 有可能被装在跑完之后的环境里，缓存会把这个变化吃掉。
    代价可以忽略 —— import 是系统页缓存里的常数级操作，
    而本函数一个进程里只会被调一次（dbscan 入口）。
    """
    name = str(backend or basicSettings.DBSCAN_BACKEND or "auto").strip().lower()
    if name not in basicSettings.DBSCAN_BACKEND_ALL:
        raise ClusterError("未知的聚类后端 %r（可选 %s）"
                           % (name, list(basicSettings.DBSCAN_BACKEND_ALL)))
    if name != "auto":
        if name == "sklearn":
            try:
                import sklearn  # noqa: F401
            except ImportError:
                raise ClusterError(
                    "指定了 --backend sklearn 但当前环境没装 scikit-learn；"
                    "本项目默认不引它（见 basicSettings.DBSCAN_BACKEND 的理由），"
                    "请改用 --backend numpy。")
        return name
    try:
        import sklearn  # noqa: F401
        return "sklearn"
    except ImportError:
        return "numpy"


def dbscan(vectors, eps: float = None, minSamples: int = None,
           backend: str = None, chunkRows: int = None,
           maxEdges: int = None) -> object:
    """DBSCAN 主入口。返回 int32 标签数组（长度 = 行数，噪声 = LABEL_NOISE）。

    参数
    ----
      vectors   : (N, D) 向量矩阵，**不要求已归一化**（本函数自己归一化）
      eps       : 余弦距离阈值；None = basicSettings.DBSCAN_EPS
      minSamples: 核心点最少样本数（**含自己**）；None = DBSCAN_MIN_SAMPLES
      backend   : auto / numpy / sklearn；None = basicSettings.DBSCAN_BACKEND

    前置校验
    --------
      N=0 直接返回空数组（不抛错）：「没有待聚类的脸」是完全正常的状态，
      抛异常会让 CLI 在空库上直接崩掉。
      eps<=0 / minSamples<1 抛 ClusterError：这两种是**配错**，
      静默接受只会得到一堆单点簇或全噪声，且不报错。
    """
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape((1, -1)) if arr.size else arr.reshape((0, basicSettings.EMBEDDING_DIM))
    if arr.ndim != 2:
        raise ClusterError("向量矩阵必须是二维 (N, D)，收到 shape=%s" % (arr.shape,))
    value = float(basicSettings.DBSCAN_EPS if eps is None else eps)
    need = int(basicSettings.DBSCAN_MIN_SAMPLES if minSamples is None else minSamples)
    if not (0.0 < value <= 2.0):
        raise ClusterError("eps 必须在 (0, 2]（余弦距离上限 2），收到 %r" % value)
    if need < 1:
        raise ClusterError("min_samples 至少为 1，收到 %r" % need)
    total = int(arr.shape[0])
    if total == 0:
        return np.zeros((0,), dtype=np.int32)

    q = normalizeRows(arr)
    zeroAt = np.flatnonzero(np.linalg.norm(arr, axis=1) < 1e-9)
    if zeroAt.size:
        # 零向量与任何人都「距离 2」，理论上必然是噪声；显式钉死，
        # 免得某个后端对零向量给出别的行为（sklearn 的 cosine 会 nan）
        _LOG.warning("聚类输入含 %d 个零向量行，按噪声处理", int(zeroAt.size))

    picked = resolveBackend(backend)
    if picked == "sklearn":
        labels = _labelsBySklearn(q, value, need)
    else:
        labels = _labelsByNumpy(q, value, need, chunkRows=chunkRows, maxEdges=maxEdges)
    labels = np.asarray(labels, dtype=np.int32).reshape(-1)
    if labels.size != total:
        raise ClusterError("标签数(%d)与输入行数(%d)不一致 —— 这是本模块最忌讳的错位"
                           % (labels.size, total))
    if zeroAt.size:
        labels[zeroAt] = LABEL_NOISE
    return labels


def canonicalizeLabels(labels, faceCodes) -> object:
    """按「簇内最小 faceCode」重排标签编号，使输出顺序也与后端/顺序无关。

    为什么需要它：簇**划分**已经与访问顺序无关（见 _expandLabels 的说明），
    但**编号**会变。步骤 11 的contact sheet 与 CLI 打印都按编号遍历，
    编号抖动会被当成「结果不可信」。重排是纯展示层的确定性保障。
    """
    arr = np.asarray(labels, dtype=np.int32)
    if arr.size == 0:
        return arr
    codes = [str(c or "") for c in faceCodes]
    groups = {}
    for i, label in enumerate(arr):
        if int(label) == LABEL_NOISE:
            continue
        groups.setdefault(int(label), []).append(i)
    if not groups:
        return arr
    # 排序键：簇内最小 faceCode（faceCode 全局唯一 => 键唯一 => 顺序确定）
    order = sorted(groups.keys(),
                   key=lambda lab: min(codes[i] for i in groups[lab]))
    remap = {old: new for new, old in enumerate(order)}
    out = np.full(arr.shape, LABEL_NOISE, dtype=np.int32)
    for old, new in remap.items():
        out[arr == old] = new
    return out


# ============================================================
# 五、clusterCode（幂等编码）
# ============================================================

def clusterCodeOf(faceCodes, prefix: str = None, hashLen: int = None) -> str:
    """一簇的**内容寻址**编码：`cluster_<sha256(排序后的 faceCode)[:12]>`。

    为什么必须排序后再哈希
    ----------------------
      不排序的话，同一个簇在不同运行里因行顺序不同而得到不同编码 ——
      而 pb_face 的返回顺序恰恰是不保证稳定的（改了索引、换了机器、
      一次 vacuum 就可能变）。排序把「顺序」这个无关变量消掉，
      编码才真的只取决于**成员集合**。
    ⚠️ 分隔符用换行而不是空串：空串拼接下 ["ab","c"] 与 ["a","bc"] 会撞成同一个哈希。
    """
    items = sorted(str(code or "") for code in (faceCodes or ()))
    if not items:
        return ""
    digest = hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()
    head = str(basicSettings.CLUSTER_CODE_PREFIX if prefix is None else prefix)
    return head + digest[:int(basicSettings.CLUSTER_CODE_HASH_LEN if hashLen is None
                              else hashLen)]


def clusterCodes(faceCodes, labels) -> list:
    """逐行给簇编码。**噪声点返回空串**（落库时写 NULL，不是 "_NOISE_"）。

    返回列表与 faceCodes **等长同序**。
    """
    arr = np.asarray(labels, dtype=np.int32)
    codes = [str(c or "") for c in faceCodes]
    if arr.size != len(codes):
        raise ClusterError("标签数(%d)与 faceCode 数(%d)不一致" % (arr.size, len(codes)))
    groups = {}
    for i, label in enumerate(arr):
        if int(label) == LABEL_NOISE:
            continue
        groups.setdefault(int(label), []).append(codes[i])
    byLabel = {lab: clusterCodeOf(items) for lab, items in groups.items()}
    return [byLabel.get(int(lab), "") for lab in arr]


# ============================================================
# 六、簇对象（含代表样本）
# ============================================================

def pickRepresentative(detScores, indexes) -> int:
    """簇代表在**簇内下标**里的位置：取 detScore 最高的那张脸。

    为什么按 detScore 挑，而不是按「离质心最近」
    -------------------------------------------
      detScore 是 SCRFD 的检测置信度，语义是「这张脸框得准不准」，
      与「像不像簇里其他人」是**两件事**。代表样本是给用户看的缩略图，
      用户判断「这几个是不是同一个人」靠的是脸清晰不清晰 ——
      一张模糊的框哪怕在向量空间里最居中，展示出来也没用。
      detScore 恰好存了，不用白不用；同分时取 faceCode 最小的那张
      （下面由调用方按 faceCode 兜底），保证确定性。
    """
    bestAt = -1
    bestScore = -1.0
    for pos, i in enumerate(indexes or ()):
        try:
            score = float(detScores[i])
        except (TypeError, ValueError, IndexError):
            score = 0.0
        if score > bestScore:
            bestScore, bestAt = score, pos
    return bestAt if bestAt >= 0 else 0


def buildClusters(faceCodes, labels, vectors=None, detScores=None,
                  minSimilarity: float = None) -> list:
    """标签 -> list[Cluster]（按代表 faceCode 升序，便于人工按序抽查）。

    parameters
    ----------
      faceCodes  : 与 labels 等长同序
      vectors    : (N, D)，给了就顺便算簇质心与平均相似度（供 CLI 报告）
      detScores  : 长度 N 的序列，取代表样本用

    簇质心 = 簇内向量均值再归一化（与 pb_person_centroid 同一口径），
    只是**不落库** —— 未命名人物还没有 personCode，质心存进
    pb_person_centroid 会凭空造人。
    """
    arr = np.asarray(labels, dtype=np.int32)
    codes = [str(c or "") for c in faceCodes]
    if arr.size != len(codes):
        raise ClusterError("标签数(%d)与 faceCode 数(%d)不一致" % (arr.size, len(codes)))
    q = normalizeRows(vectors) if vectors is not None else None
    if detScores is None:
        detScores = [0.0] * len(codes)

    groups = {}
    for i, label in enumerate(arr):
        if int(label) == LABEL_NOISE:
            continue
        groups.setdefault(int(label), []).append(i)

    out = []
    for label in sorted(groups):
        items = groups[label]
        at = pickRepresentative(detScores, items)
        centroid = None
        meanSim = None
        if q is not None and q.size:
            block = q[items]
            vec = block.mean(axis=0)
            norm = float(np.linalg.norm(vec))
            if norm > 1e-9:
                centroid = (vec / norm).astype(np.float32)
            if len(items) > 1 and minSimilarity is None:
                sims = block @ block.T
                iu = np.triu_indices(len(items), k=1)
                meanSim = float(sims[iu].mean()) if iu[0].size else 1.0
            elif minSimilarity is not None:
                meanSim = float(minSimilarity)
        try:
            repScore = float(detScores[items[at]])
        except (TypeError, ValueError, IndexError):
            repScore = None
        out.append(Cluster(clusterCodeOf([codes[i] for i in items]),
                           [codes[i] for i in items],
                           representativeFaceCode=codes[items[at]],
                           representativeDetScore=repScore,
                           centroid=centroid, meanSimilarity=meanSim))
    out.sort(key=lambda c: c.representativeFaceCode)
    return out


def summarize(clusters: list, faceCount: int = 0, noiseCount: int = 0) -> dict:
    """一批聚类的分布统计（CLI / 验收脚本直接打印）。"""
    sizes = sorted((c.size for c in clusters or ()), reverse=True)
    return {
        "faces": int(faceCount or 0),
        "clusters": len(clusters or ()),
        "noise": int(noiseCount or 0),
        "largest": sizes[0] if sizes else 0,
        "minSize": sizes[-1] if sizes else 0,
        "meanSize": (sum(sizes) / float(len(sizes))) if sizes else 0.0,
    }


# ============================================================
# 七、自检用合成数据（**不是**测试框架，但单测复用同一份定义）
# ============================================================

def makeSyntheticBlobs(blobCount: int = 3, perBlob: int = 6, dim: int = None,
                       spread: float = 0.015, seed: int = 7) -> object:
    """造「几个团 + 团内抖动」的归一化向量矩阵（给自检与单测用）。

    为什么不能直接用标准正态随机向量当「同一个人」
    ----------------------------------------------
      512 维里两个独立高斯向量的余弦**约等于 0**（实测 -0.078 ~ 0.064，
      维度一高就近乎正交），余弦距离 ≈ 1 > eps=0.45，于是「6 张同一个人的脸」
      会被判成 6 个噪声点。单测若这么造数据，测的就不是聚类算法而是
      「随机向量恰好有多正交」，换个维度就全挂 —— 这是最典型的**假测试**。
      正解：先取单位中心向量，再叠一个**小**高斯抖动后重新归一化。

    ⚠️ spread 与实际相似度的对应关系是**实测**的（4 团 × 4 张，512 维）
       spread=0.004 -> 簇内 cos 0.991~0.993
       spread=0.015 -> 簇内 cos 0.881~0.906（缺省；簇间 cos <= 0.051）
       spread=0.030 -> 簇内 cos 0.637~0.698 ← **已贴近 eps=0.45 的下限 0.55**
       所以缺省取 0.015：既有干净的分簇，又不会被「簇内相似度」
       意外掉进/掉出 eps 阈值 —— 后者会让「测的是算法」变成「测的是数据」。
       真实人脸的簇内均值相似度实测在 0.65~0.78（见 clustering.py 自检输出），
       也就是说**真实数据比这里更难**。
    """
    width = int(dim or basicSettings.EMBEDDING_DIM)
    groups = max(1, int(blobCount))
    each = max(1, int(perBlob))
    rng = np.random.default_rng(int(seed))
    centers = rng.normal(size=(groups, width)).astype(np.float32)
    centers /= np.maximum(np.linalg.norm(centers, axis=1, keepdims=True), 1e-9)
    rows = []
    for i in range(groups):
        block = centers[i] + np.float32(spread) * rng.normal(
            size=(each, width)).astype(np.float32)
        rows.append(block / np.maximum(
            np.linalg.norm(block, axis=1, keepdims=True), 1e-9))
    return np.ascontiguousarray(np.vstack(rows), dtype=np.float32)


if __name__ == "__main__":
    print("dbscan.py _VERSION:", _VERSION)
    print("eps=%s  min_samples=%s  后端=%s(实际 %s)"
          % (basicSettings.DBSCAN_EPS, basicSettings.DBSCAN_MIN_SAMPLES,
             basicSettings.DBSCAN_BACKEND, resolveBackend()))
    print("分片上限 %d 行 / 临时矩阵 %d 个元素（约 %.1f MB）/ 边数上限 %d"
          % (basicSettings.DBSCAN_CHUNK_ROWS, basicSettings.DBSCAN_SIM_CELLS,
             basicSettings.DBSCAN_SIM_CELLS * 4 / 1048576.0,
             basicSettings.DBSCAN_MAX_EDGES))
    print("clusterCode 规则: %s<sha256(排序后faceCode)[:%d]>"
          % (basicSettings.CLUSTER_CODE_PREFIX, basicSettings.CLUSTER_CODE_HASH_LEN))
    print("噪声标签 %d（落库写 NULL；%s 只作查询层占位）"
          % (LABEL_NOISE, comGD.CLUSTER_NOISE))

    _m = makeSyntheticBlobs(blobCount=3, perBlob=6, spread=0.015, seed=7)
    _codesIn = ["F%02d" % i for i in range(_m.shape[0])]
    _lab = canonicalizeLabels(dbscan(_m, eps=0.45, minSamples=3, backend="numpy"),
                              _codesIn)
    _cl = buildClusters(_codesIn, _lab, vectors=_m, detScores=[0.9] * len(_codesIn))
    print("自检 3 团 x 6 张 -> 标签 %s" % _lab.tolist())
    print("  簇 %d 个: %s" % (len(_cl),
                             [ "%s(n=%d,rep=%s)" % (c.clusterCode, c.size,
                                                    c.representativeFaceCode)
                               for c in _cl ]))
    _again = clusterCodes(_codesIn, canonicalizeLabels(
        dbscan(_m, eps=0.45, minSamples=3, backend="numpy"), _codesIn))
    print("  幂等校验 %s（clusterCode=%s）"
          % ("OK" if _again == clusterCodes(_codesIn, _lab) else "FAILED", _again[0]))
