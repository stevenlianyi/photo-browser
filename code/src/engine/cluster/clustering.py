#! /usr/bin/env python3
#encoding: utf-8

#Filename: clustering.py
#Description: photo-browser 未归类人脸聚类的**读库与落库**层（步骤 7）
#
# 与 dbscan.py 的分工
# ------------------
#   dbscan.py     纯计算（不 import database.*），可脱离数据库单测
#   clustering.py 唯一读 pb_face.embedding 的地方 + 唯一写 clusterCode 的地方
#
# 聚类集合口径（硬约束，改这里之前先读数据库设计.md D-4 与开发计划.md DR-16）
# ------------------------------------------------------------------------
#   `personCode IS NULL AND isStranger=0 AND delFlag='0'`
#   * **已归类（personCode 非空）绝不重新聚类**：它们已绑在某个人身上，
#     簇对它们没有意义；重新聚类会让用户刚点的确认"换了个簇编号"。
#     验收第 4 条就是数这个：聚类前后 personCode 非空的行数必须**完全相等**。
#   * **isStranger=1 既不进队列也不进聚类**（永久排除是用户的决定）。
#   * delFlag='0'：软删的人脸不该长出簇来。
#   ⚠️ 条件里**不能有 isConfirmed**：自动归属的 isConfirmed 本来就是 0，
#      加上它等于把待确认队列扩成「未归属 + 全部自动归属」（DR-16①的坑）。
#
# 写clusterCode 的三个关键决定
# ---------------------------
#   ① **走 upsert（insertManyTableGeneral + forceColumns），不走 update_***。
#      噪声点必须能把 clusterCode **清成 NULL**；而生成层的 update_* 遇 None
#      会整条丢掉该列 —— 那是一次「返回 0 行、不报错、库里原封不动」的静默空操作
#      （assigner._patchFace 的注释里记着这个坑：撤销归属曾因此一直是原值）。
#   ② **只写聚类集合里的行**。已归类的脸如果顺手清clusterCode，
#      步骤 11「同一簇批量改判」就断了线索（那张脸当初和谁聚在一起是有用的）。
#      所以「已归类的行」在默认模式下**一次都不碰**。
#   ③ **不写 pb_review_log**。聚类是机器行为，不是人工操作；
#      而 pb_review_log 是「这张脸当初怎么被认成这个人的」的唯一排障依据
#      （数据库设计.md §4.9），灌几万条机器日志只会淹没真正要查的那几条。
#   ④ **不写 photoDir**：只读 embedding，不裁图、不碰缩略图。
#
# 幂等（验收第 3 条）
# ----------------
#   clusterCode 由「簇内 faceCode 排序后哈希」决定（dbscan.clusterCodeOf），
#   所以同一份数据重跑一定得到同一批编码。applyCluster() 会回报
#   unchanged / changed / cleared 三个数 —— 重跑时 changed 应当为 0。
#   `--rebuild` 是**另一个**动作：先把全表clusterCode 清成NULL再重算，
#   用于「改了 eps 之后想把旧簇彻底作废」，它会碰到已归类的行（明确要求才做）。

import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/cluster
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np# noqa: E402

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.cluster import dbscan as dbscan                       # noqa: E402
from engine.face import engine as faceEngine# noqa: E402

_VERSION = "20261005"

_LOG = misc.setLogNew("clustering", "clustering.log")

#: 聚类集合 = personCode 为空。生成层只支持 nullFields（IS NULL），
#: isStranger=0 在 Python 侧过滤（与 centroid/run_match 同一个理由：
#: 不为一次筛选去改生成器 + 全库迁移）。
PENDING_NULL_FIELDS: tuple = ("personCode",)

#: 写 pb_face 走 upsert 时**必须带齐**的 NOT NULL 身份列
#: （upsert 是 INSERT 语义，缺列会撞 NOT NULL constraint failed，
#:   而不是「更新不到」—— 与步骤 3 faceStore 踩过的坑同一个）。
FACE_IDENTITY_COLUMNS: tuple = ("photoCode",)


class ClusteringError(Exception):
    """落库/装载环节的失败（DB 写不进、行不存在等）。"""


# ============================================================
# 一、结果对象
# ============================================================

class ClusteringResult(object):
    """一次聚类的完整结果（CLI 与验收脚本都只读它）。"""

    __slots__ = ("eps", "minSamples", "backend", "faceCodes", "photoCodes",
                 "labels", "codes", "clusters", "noiseCount", "skipped",
                 "loadSec", "clusterSec", "assignedCount", "previousCodes")

    def __init__(self, eps, minSamples, backend):
        self.eps = float(eps)
        self.minSamples = int(minSamples)
        self.backend = str(backend or "")
        self.faceCodes = []# 与 labels / codes 等长同序
        self.photoCodes = []
        self.labels = np.zeros((0,), dtype=np.int32)
        self.codes = []          # 逐行簇编码（噪声点 = ""，落库写 NULL）
        self.clusters = []       # list[dbscan.Cluster]
        self.noiseCount = 0
        self.skipped = 0         # 无可用 embedding 而没进聚类的行数
        self.loadSec = 0.0
        self.clusterSec = 0.0
        self.assignedCount = 0
        self.previousCodes = {}  # faceCode -> 原clusterCode（算 unchanged 用）

    @property
    def faceCount(self) -> int:
        return len(self.faceCodes)

    def toDict(self) -> dict:
        stat = dbscan.summarize(self.clusters, self.faceCount, self.noiseCount)
        stat.update({"eps": self.eps, "minSamples": self.minSamples,
                     "backend": self.backend, "skipped": self.skipped,
                     "loadSec": round(self.loadSec, 3),
                     "clusterSec": round(self.clusterSec, 3),
                     "assigned": self.assignedCount,
                     "noiseLabel": dbscan.LABEL_NOISE,
                     "noisePlaceholder": comGD.CLUSTER_NOISE})
        return stat

    def __repr__(self):
        return "ClusteringResult(%d 张 -> %d 簇 + %d 噪声, eps=%.2f, %s)" % (
            self.faceCount, len(self.clusters), self.noiseCount, self.eps,
            self.backend)


# ============================================================
# 二、装载未归类集合
# ============================================================

def loadUnclassified(limit: int = 0, offset: int = 0, pageRows: int = None,
                     dbFile: str = None, progress=None) -> tuple:
    """取**未归类且非陌生人**的人脸行（**分页**，与步骤 3/5 同一理由）。

    返回 (rows, skipped)
      rows    : 有可用 embedding 的行，**逐行与矩阵一一对应**
      skipped : 因 embedding 缺失/非法而没进聚类的行数

    为什么自己建矩阵而不用 centroid.loadFaceMatrix
    -------------------------------------------
      loadFaceMatrix 会**静默跳过**脏行（它明确把「哪几行被跳过」的责任推给调用方）。
      聚类不能这样：一张脸的下标错了，clusterCode 就会写到**别人**脸上，
      而且不报错。宁可在这里显式剔掉并报数，也不让它静默错位。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)                    # DR-10：显式才切库
    step = int(pageRows or basicSettings.CLUSTER_PAGE_ROWS)
    cap = int(limit or 0)
    rows = []
    skipped = 0
    seen = 0
    while True:
        rowsIn = sqliteCommon.query_pb_face(
            "pb_face", mode="full", orderBy="recID",
            nullFields=PENDING_NULL_FIELDS,
            limitNum=step, offsetNum=int(offset) + seen)
        if not rowsIn:
            break
        for row in rowsIn:
            seen += 1
            if int(row.get("isStranger") or 0):
                continue
            vec = _decodedVector(row.get("embedding"))
            if vec is None:
                skipped += 1
                continue
            row["_vec"] = vec
            rows.append(row)
            if cap and len(rows) >= cap:
                if progress:
                    progress(len(rows), seen)
                return rows, skipped
        if len(rowsIn) < step:
            break
        if progress:
            progress(len(rows), seen)
    return rows, skipped


def _decodedVector(blob):
    """BLOB -> 归一化向量；脏数据返回 None（**不抛**，脏行不该中断整批）。"""
    if blob is None:
        return None
    try:
        raw = faceEngine.decodeEmbedding(blob)
    except (TypeError, ValueError):
        return None
    return faceEngine.l2normalize(raw)


def faceMatrixOf(rows: list) -> object:
    """行列表 -> (N, D) float32 矩阵（**逐位对应**，行序不变）。"""
    if not rows:
        return np.zeros((0, basicSettings.EMBEDDING_DIM), dtype=np.float32)
    return np.ascontiguousarray(np.vstack([r["_vec"] for r in rows]),
                                dtype=np.float32)


# ============================================================
# 三、聚类（读库 -> 纯计算）
# ============================================================

def clusterUnclassified(eps: float = None, minSamples: int = None,
                        rows: list = None, limit: int = 0, backend: str = None,
                        dbFile: str = None, progress=None) -> ClusteringResult:
    """对未归类集合聚类。**不写库**（落库是 applyCluster 的事）。

    流程：分页装载 -> 组矩阵 -> dbscan -> 规范化编号 -> 生成簇与逐行编码
    """
    value = float(basicSettings.DBSCAN_EPS if eps is None else eps)
    need = int(basicSettings.DBSCAN_MIN_SAMPLES if minSamples is None else minSamples)
    picked = dbscan.resolveBackend(backend)

    start = time.perf_counter()
    if rows is None:
        rows, skipped = loadUnclassified(limit=limit, dbFile=dbFile, progress=progress)
    else:
        skipped = 0
    loadSec = time.perf_counter() - start

    result = ClusteringResult(value, need, picked)
    result.loadSec = loadSec
    result.skipped = int(skipped)
    result.faceCodes = [str(r.get("faceCode") or "") for r in rows]
    result.photoCodes = [str(r.get("photoCode") or "") for r in rows]
    result.previousCodes = dict((result.faceCodes[i], str(r.get("clusterCode") or ""))
                                for i, r in enumerate(rows))
    if not rows:
        return result

    matrix = faceMatrixOf(rows)
    start = time.perf_counter()
    labels = dbscan.dbscan(matrix, eps=value, minSamples=need, backend=picked)
    labels = dbscan.canonicalizeLabels(labels, result.faceCodes)
    result.clusterSec = time.perf_counter() - start

    result.labels = labels
    result.codes = dbscan.clusterCodes(result.faceCodes, labels)
    result.noiseCount = int((labels == dbscan.LABEL_NOISE).sum())
    result.clusters = dbscan.buildClusters(
        result.faceCodes, labels, vectors=matrix,
        detScores=[r.get("detScore") for r in rows])
    result.assignedCount = sum(1 for c in result.codes if c)
    return result


# ============================================================
# 四、落库（写 clusterCode）
# ============================================================

def _patchClusterCode(faceCode: str, photoCode: str, clusterCode) -> int:
    """写一行的 clusterCode。**能写 NULL**（噪声点要清成空）。

    与 assigner._patchFace 同理：走 upsert 而非 update_*，
    否则 clusterCode=None 会被normalizeDataSet 整条丢掉 -> 静默空操作。
    """
    row = {"faceCode": str(faceCode), "photoCode": str(photoCode or "")}
    row["clusterCode"] = (str(clusterCode) if str(clusterCode or "") else None)
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_face", [row], conflictColumns=("faceCode",),
        updateColumns=("clusterCode",), fillStandard=True,
        forceColumns=("clusterCode",))
    if rtn == -2:                                    # sqliteHandle.RET_ERROR
        raise ClusteringError("pb_face.clusterCode 写入失败: %s"
                              % sqliteCommon.dbHandle().lastErrMsg)
    return rtn


def applyCluster(result: ClusteringResult, dryRun: bool = False,
                 batch: int = None) -> dict:
    """把聚类结果写进 pb_face.clusterCode。返回统计。

    ⚠️ **只写聚类集合里的行**（见文件头「写 clusterCode 的三个关键决定」②）：
       已归类的人脸一次都不碰，验收第 4 条「已归类人脸数量聚类前后不变」由此保证。
    ⚠️ 噪声点写 **NULL**（不是 `_NOISE_`）：「不属于任何簇」用空值表达才自洽，
       否则「查未聚类的脸」要从 `IS NULL` 变成枚举排除，漏一个就错一批。
    """
    stat = {"faces": result.faceCount, "written": 0, "unchanged": 0,
            "changed": 0, "cleared": 0, "failed": 0}
    step = int(batch or basicSettings.CLUSTER_WRITE_BATCH)
    for begin in range(0, result.faceCount, step):
        chunk = range(begin, min(result.faceCount, begin + step))
        for i in chunk:
            code = result.codes[i]
            faceCode = result.faceCodes[i]
            was = result.previousCodes.get(faceCode, "")
            if (code or "") == (was or ""):
                stat["unchanged"] += 1        # 幂等：编码没变就不必写
                continue
            if dryRun:
                stat["changed" if code else "cleared"] += 1
                stat["written"] += 1
                continue
            try:
                _patchClusterCode(faceCode, result.photoCodes[i], code)
            except ClusteringError as e:
                stat["failed"] += 1
                _LOG.error("clusterCode 写入失败 %s: %s", faceCode, e)
                continue
            stat["written"] += 1
            if code:
                stat["changed"] += 1
            else:
                stat["cleared"] += 1
    return stat


def clearClusterCodes(onlyPending: bool = True, dryRun: bool = False,
                      batch: int = None) -> dict:
    """把 clusterCode 清成 NULL。**`--rebuild` 的第一步**。

    onlyPending=True（缺省）只清聚类集合里的行 —— 与 applyCluster 的边界一致。
    onlyPending=False 会**连已归类的行一起清**，那是 --rebuild 明确要求的
    「旧簇彻底作废」，代价是步骤 11 的「同一簇批量改判」失去历史线索。
    """
    stat = {"rows": 0, "cleared": 0, "skipped": 0, "failed": 0}
    nullFields = PENDING_NULL_FIELDS if onlyPending else ()
    offset = 0
    step = int(batch or basicSettings.CLUSTER_PAGE_ROWS)
    while True:
        rows = sqliteCommon.query_pb_face(
            "pb_face", mode="light", orderBy="recID", nullFields=nullFields,
            limitNum=step, offsetNum=offset)
        if not rows:
            break
        coded = [r for r in rows
                 if str(r.get("clusterCode") or "")
                 and not (onlyPending and int(r.get("isStranger") or 0))]
        stat["rows"] += len(rows)
        stat["skipped"] += len(rows) - len(coded)
        for row in coded:
            if dryRun:
                stat["cleared"] += 1
                continue
            try:
                _patchClusterCode(str(row.get("faceCode") or ""),
                                  str(row.get("photoCode") or ""), None)
                stat["cleared"] += 1
            except ClusteringError as e:
                stat["failed"] += 1
                _LOG.error("清clusterCode 失败 %s: %s", row.get("faceCode"), e)
        offset += len(rows)
        if len(rows) < step:
            break
    return stat


# ============================================================
# 五、现状
# ============================================================

def stateCounts() -> dict:
    """四态计数（**全部直接 SQL count**，不读 pb_scan_job.pendingCount）。

    ⚠️ pendingCount 是「疑似移动/重命名待确认**张数**」（步骤 3 的 movedToPhotoCode），
       与这里的「待确认**人脸**条数」是两个不同的计数，**不能混用同一个字段**
       （DR-11）。侧栏角标要显示的是后者。
    """
    where = {
        "pending": "personCode IS NULL AND isStranger = %s AND delFlag = %s",
        "disputed": ("personCode IS NOT NULL AND isConfirmed = %s"
                     " AND isStranger = %s AND delFlag = %s"),
        "confirmed": "isConfirmed = %s AND isStranger = %s AND delFlag = %s",
        "stranger": "isStranger = %s AND delFlag = %s",
    }
    return {
        "pending": sqliteCommon.countWhereGeneral(
            "pb_face", where["pending"], (0, comGD.DEL_FLAG_NO)),
        "disputed": sqliteCommon.countWhereGeneral(
            "pb_face", where["disputed"], (0, 0, comGD.DEL_FLAG_NO)),
        "confirmed": sqliteCommon.countWhereGeneral(
            "pb_face", where["confirmed"], (1, 0, comGD.DEL_FLAG_NO)),
        "stranger": sqliteCommon.countWhereGeneral(
            "pb_face", where["stranger"], (1, comGD.DEL_FLAG_NO)),
    }


def clusterStats() -> dict:
    """库里现有的簇概况（只读，供 --status）。

    三个数分开报，别混：
      codedRows  : clusterCode 非空的行数
      clusters   : 不同clusterCode 的个数
      assigned   : 簇里**已归类**的行数（默认不参与重聚，但仍保留簇线索）
      noise      : isStranger=1 却带着簇编码的行数（正常应为 0；
                    非 0 说明有人把陌生人又拉回来了，--rebuild 可以清掉）
    """
    rows = sqliteCommon.query_pb_face("pb_face", mode="light")
    clusters = {}
    assigned = 0
    noise = 0
    for row in rows:
        code = str(row.get("clusterCode") or "")
        if not code:
            continue
        one = clusters.setdefault(code, {"size": 0, "assigned": 0, "bestDet": 0.0})
        one["size"] += 1
        if str(row.get("personCode") or ""):
            one["assigned"] += 1
            assigned += 1
            continue
        if int(row.get("isStranger") or 0):
            noise += 1
            continue
        try:
            one["bestDet"] = max(one["bestDet"], float(row.get("detScore") or 0.0))
        except (TypeError, ValueError):
            pass
    return {"codedRows": sum(c["size"] for c in clusters.values()),
            "clusters": len(clusters), "assigned": assigned, "noise": noise,
            "detail": clusters}


# ============================================================
# 六、contact sheet 落点（**非库产物**，见 basicSettings 的说明）
# ============================================================
# 为什么放在 clustering.py 而不是 tools/cluster_cli.py
#   ① 落点路径是**数据口径的一部分**：换机器/改 THUMB_ROOT 后要能找到旧拼图，
#      所以它必须有一个唯一的推导函数，不能散在 CLI 里拼字符串；
#   ② 「清掉过期拼图」需要知道当前有效的 clusterCode 集合 ——
#      那正是本模块的产出。
#
# ⚠️ 拼图是**给人眼看的临时产物**，不是库数据：
#   不进 pb_* 任何表、没有 fileHash/faceCode 索引、删掉不影响任何功能。
#   但它**会过期**：clusterCode 是内容指纹（见 queue.py「四之二」），
#   簇成员一变编码就变，旧拼图会静默变成「一个已经不存在的簇的照片」。
#   对着过期拼图下结论比没有拼图更糟，所以每次写完必须清残留。

def sheetDir(create: bool = False) -> str:
    """contact sheet 目录绝对路径 = <thumb>/cluster/。"""
    outDir = os.path.join(paths.thumb_dir(), basicSettings.CLUSTER_SHEET_SUBDIR)
    if create and not os.path.isdir(outDir):
        os.makedirs(outDir, exist_ok=True)
    return outDir


def purgeStaleSheets(validCodes=None) -> list:
    """删掉「不再对应任何当前簇」的拼图，返回删掉的文件名列表。

    ⚠️⚠️ validCodes=None / 空 -> **一个都不删**，直接返回。
       这里的语义必须是「我不该删」而不是「我认为全过期了」：
       调用方（CLI）算不出当前簇集合时传 None 是常事（聚类失败、库读不出来），
       若把它当成空集合，整个目录会被清空 —— 而簇**一个都没变**。
       这类「默认值等于最危险动作」的接口设计是典型的自伤。
    ⚠️ 只认 `<clusterCode>.jpg` 这一种文件名，其它文件一律不动：
       这里是 thumb 目录下的删除操作，宁可漏删也不能误删
       （万一人把别的东西放在那儿，它不该被这个函数清掉）。
    """
    if not validCodes:
        return []
    outDir = sheetDir()
    if not os.path.isdir(outDir):
        return []
    keep = set(str(c) for c in (validCodes or ()))
    removed = []
    try:
        names = os.listdir(outDir)
    except OSError as e:
        _LOG.error("purgeStaleSheets: 读不了 %s (%s)" % (outDir, e))
        return []
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.lower() != ".jpg" or not base.startswith("cluster_"):
            continue
        if base in keep:
            continue
        target = os.path.join(outDir, name)
        try:
            os.remove(target)
            removed.append(name)
        except OSError as e:
            _LOG.error("purgeStaleSheets: 删 %s 失败 (%s)" % (target, e))
    if removed:
        _LOG.info("purgeStaleSheets: 清掉 %d 张过期拼图（簇编码已变）" % len(removed))
    return sorted(removed)

if __name__ == "__main__":
    _fixConsole = getattr(sys.stdout, "reconfigure", None)
    if _fixConsole:
        _fixConsole(encoding="utf-8", errors="replace")
    print("clustering.py _VERSION:", _VERSION)
    print("库          :", sqliteCommon.dbFilePath())
    print("聚类集合    : personCode IS NULL AND isStranger=0 AND delFlag='0'")
    print("四态计数    :", stateCounts())
    _res = clusterUnclassified()
    print("本次聚类    :", _res)
    print("  统计      :", _res.toDict())
    for _c in _res.clusters[:10]:
        print("   %-22s n=%-3d rep=%-34s det=%.4f 簇内均值相似度 %.4f"
              % (_c.clusterCode, _c.size, _c.representativeFaceCode,
                 _c.representativeDetScore or 0.0, _c.meanSimilarity or 0.0))
    print("  噪声点    : %d（clusterCode 写 NULL，%s 只是查询层占位）"
          % (_res.noiseCount, comGD.CLUSTER_NOISE))
