#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_dbscan.py
#Description: 步骤 7 单测 —— 聚类正确性 / clusterCode 幂等/ **两个队列的四态口径**
#
# 分三段
# ------
#   一聚类算法（纯计算，**不连数据库**）
#   二 clusterCode 幂等（纯计算）
#   三 四态口径与两个队列（**连 pytest 临时库**，绝不碰 d:\PhotoLib 正式库）
#
# 为什么第一、二段刻意不连库
# --------------------------
#   算法正确性与"upsert 有没有真的写进去"是两件正交的事，混在一起测时，
#   算法写错会被库的噪声掩盖、库写错会被算法的假阳性掩盖。
#   matcher.py 的单测也是这个口径（test_matcher.py：「决策逻辑的验证不该依赖
#   建库与数据准备」）。
#
# ⚠️ 第三段是**DR-16 修正点的回归锁**
#   「构造 N 条自动归属数据，待确认队列仍为 0 条」这类断言一旦不写进单测，
#   下次有人把队列条件改回 `OR isConfirmed=0` 没人会发现 ——
#   而那不会报错，只会让 10 万张的家庭照片库长出 8 万条待确认。
#
# 全部跑在 pytest 的 tmp 临时库上，**绝不动 d:\PhotoLib 正式库**。

import os

import numpy as np
import pytest

from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.cluster import clustering as clustering
from engine.cluster import dbscan as dbscan
from engine.face import engine as faceEngine
from processor.review import queue as reviewQueue


# ============================================================
# 〇、共用小工具
# ============================================================

def codesOf(count: int, prefix: str = "fc_") -> list:
    return ["%s%02d" % (prefix, i) for i in range(count)]


def groupsOf(labels) -> dict:
    """{簇号: [下标, ...]}（不含噪声点）"""
    out = {}
    for i, label in enumerate(labels):
        if int(label) == dbscan.LABEL_NOISE:
            continue
        out.setdefault(int(label), []).append(i)
    return out


def near(center, count: int, spread: float = 0.015, seed: int = 1) -> list:
    """围绕 center 生成 count 个「像同一个人」的向量（spread 语义见 dbscan 里的实测表）。"""
    rng = np.random.default_rng(seed)
    base = np.asarray(center, dtype=np.float32)
    out = []
    for _ in range(count):
        vec = base + np.float32(spread) * rng.normal(
            size=base.size).astype(np.float32)
        out.append(faceEngine.l2normalize(vec))
    return out


def unit(seed: int):
    rng = np.random.RandomState(seed)
    return faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())


# ============================================================
# 一、聚类算法（纯计算）
# ============================================================

class TestDbscanCorrectness:
    def test_three_blobs_become_three_clusters(self):
        """3 团 × 6 张 -> 恰好 3 个簇，且**成员一位不差**"""
        m = dbscan.makeSyntheticBlobs(blobCount=3, perBlob=6, seed=11)
        labels = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy")
        groups = groupsOf(labels)
        assert len(groups) == 3, "期望 3 簇，得到 %d簇（标签 %s）" % (len(groups),
                                                                labels.tolist())
        for items in groups.values():
            assert len(items) == 6
            # 同一簇的成员必须来自同一团：块内下标 0-5 / 6-11 / 12-17
            assert max(items) - min(items) == 5

    def test_singletons_are_noise(self):
        """互不相似（正交）的脸全是噪声点：min_samples=3 时凑不出核心点"""
        m = np.vstack([unit(700 + i) for i in range(8)])
        labels = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy")
        assert labels.tolist() == [dbscan.LABEL_NOISE] * 8

    def test_min_samples_one_keeps_every_face(self):
        """min_samples=1：每张脸自成一簇，噪声点为 0"""
        m = np.vstack([unit(720 + i) for i in range(5)])
        labels = dbscan.dbscan(m, eps=0.45, minSamples=1, backend="numpy")
        assert dbscan.LABEL_NOISE not in labels.tolist()
        assert len(set(labels.tolist())) == 5

    def test_identical_vectors_form_one_cluster(self):
        """同一张脸被提取多次（完全相同的向量）-> 1 个簇，成员齐全"""
        m = np.vstack([unit(999)] * 5)
        labels = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy")
        assert len(set(labels.tolist())) == 1
        assert labels.tolist() == [0] * 5

    def test_border_point_joins_the_cluster(self):
        """边界点必须被核心点"拉进"簇，而不是留在噪声里。

        构造一条**密度可达链**：每一步都在当前向量的**正交方向**上偏一个固定角度，
        于是「相邻两点的余弦」可以被精确控制（cos = 1/sqrt(1+s^2)）。
        s=1.0 时实测：lag1 cos=0.707（距离 0.29 < eps=0.45 -> 是邻居），
        lag2 cos=0.481（距离 0.52 > eps -> 不是邻居）。
        于是 x1/x2 是核心点（各有 2 个邻居），x0/x4 只有 1 个邻居 -> **边界点**。
        经典 DBSCAN 里边界点应当被吸收进簇：若扩展时忘了把已判噪声的点改成簇成员，
        x0/x4 就会留在噪声里 —— 而人眼看到的是「这几张明显是同一个人」。
        """
        rng = np.random.default_rng(4242)
        x = [faceEngine.l2normalize(
            rng.normal(size=basicSettings.EMBEDDING_DIM).astype(np.float32))]
        for _ in range(4):
            prev = x[-1]
            d = rng.normal(size=basicSettings.EMBEDDING_DIM).astype(np.float32)
            d = d - np.float32(d.dot(prev)) * prev             # 正交化
            d = d / np.linalg.norm(d)
            x.append(faceEngine.l2normalize(prev + np.float32(1.0) * d))
        m = np.vstack(x)
        sim = m @ m.T
        thr = 1.0 - 0.45
        assert sim[0, 1] >= thr, "相邻点必须是邻居"
        assert sim[0, 2] < thr, "隔一个点必须**不是**邻居（否则测不到边界点）"
        labels = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy")
        assert dbscan.LABEL_NOISE not in labels.tolist(), \
            "边界点被漏成噪声：%s" % labels.tolist()
        assert len(set(labels.tolist())) == 1, labels.tolist()

    def test_eps_controls_merging(self):
        """eps 是**可配置**的；且**被聚到的脸数随 eps 单调不减**"""
        m = dbscan.makeSyntheticBlobs(blobCount=4, perBlob=4, seed=21)
        # 缺省 spread 下实测簇内 cos 0.881~0.906、簇间 -0.072~0.051，所以：
        tight = dbscan.dbscan(m, eps=0.05, minSamples=3, backend="numpy")
        mid = dbscan.dbscan(m, eps=0.15, minSamples=3, backend="numpy")
        loose = dbscan.dbscan(m, eps=0.98, minSamples=3, backend="numpy")
        assert len(groupsOf(tight)) == 0, "eps=0.05（cos>=0.95）不该成簇"
        assert len(groupsOf(mid)) == 4, "eps=0.15（cos>=0.85）应当 4 团各自成簇"
        assert len(groupsOf(loose)) == 1, "eps=0.98（cos>=0.02）应当全并成 1 簇"
        # 注意：**簇数**不是单调的（4 -> 1，合并会让簇数变少）。
        # 真正单调的是「有多少张脸被归进了某个簇」—— 那才是用户看到的东西
        # （eps 越大，机器越敢把脸归到一起，绝不会越调越不敢）。
        def covered(labels):
            return sum(len(v) for v in groupsOf(labels).values())
        assert covered(tight) <= covered(mid) <= covered(loose)
        assert covered(loose) == 16

    def test_min_samples_controls_merging(self):
        """min_samples 越大越保守：2 时成簇，8 时同团也凑不够核心点"""
        m = dbscan.makeSyntheticBlobs(blobCount=3, perBlob=4, seed=31)
        loose = len(groupsOf(dbscan.dbscan(m, eps=0.45, minSamples=2, backend="numpy")))
        strict = len(groupsOf(dbscan.dbscan(m, eps=0.45, minSamples=8, backend="numpy")))
        assert strict <= loose
        assert strict == 0, "min_samples=8 而每团只有 4 张 -> 全部噪声，%d" % strict

    def test_empty_and_single_row(self):
        """空输入返回空数组（不抛错）；单行必然是噪声（凑不出 min_samples）"""
        empty = dbscan.dbscan(np.zeros((0, basicSettings.EMBEDDING_DIM), dtype=np.float32),
                              eps=0.45, minSamples=3, backend="numpy")
        assert empty.size == 0
        one = dbscan.dbscan(np.vstack([unit(5)]), eps=0.45, minSamples=3, backend="numpy")
        assert one.tolist() == [dbscan.LABEL_NOISE]

    def test_zero_vector_is_noise_not_crash(self):
        """零向量行必须被判噪声，且**不改变其它行的簇划分**"""
        good = dbscan.makeSyntheticBlobs(blobCount=2, perBlob=4, seed=41)
        base = dbscan.dbscan(good, eps=0.45, minSamples=3, backend="numpy").tolist()
        m = np.vstack([good, np.zeros((1, basicSettings.EMBEDDING_DIM), dtype=np.float32)])
        labels = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy").tolist()
        assert labels[-1] == dbscan.LABEL_NOISE
        assert labels[:-1] == base, "加一行零向量把别人的簇划分改了"

    def test_bad_parameters_raise(self):
        m = np.vstack([unit(1), unit(2)])
        with pytest.raises(dbscan.ClusterError):
            dbscan.dbscan(m, eps=0, minSamples=3, backend="numpy")
        with pytest.raises(dbscan.ClusterError):
            dbscan.dbscan(m, eps=0.45, minSamples=0, backend="numpy")
        with pytest.raises(dbscan.ClusterError):
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="faiss")

    def test_row_count_mismatch_raises(self):
        """标签数与 faceCode 数不一致必须报错（静默错位会把 A 的脸写进B 的簇）"""
        with pytest.raises(dbscan.ClusterError):
            dbscan.clusterCodes(["a", "b", "c"], np.zeros((2,), dtype=np.int32))

    def test_edge_cap_aborts_instead_of_exploding(self):
        """边数超护栏必须**明确报错**，不能默默吃光内存"""
        m = np.vstack([unit(60 + i) for i in range(12)])
        with pytest.raises(dbscan.ClusterError):
            dbscan.dbscan(m, eps=1.9, minSamples=1, backend="numpy", maxEdges=4)

    def test_chunking_does_not_change_result(self):
        """分片只是内存策略，**不得改变结果**（不同机器分片数不同）"""
        m = dbscan.makeSyntheticBlobs(blobCount=5, perBlob=5, seed=51)
        one = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy",
                            chunkRows=10_000).tolist()
        many = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy",
                             chunkRows=1).tolist()
        assert one == many, "分片改变了聚类结果"

    def test_chunk_rows_shrinks_with_scale(self):
        """N 涨到 10 万时分片必须自动收缩（峰值内存不随规模漂移）"""
        small = dbscan.chunkRowsFor(1000, 256)
        big = dbscan.chunkRowsFor(100_000, 256)
        assert small == 256
        assert big < small
        assert small * 1000 <= basicSettings.DBSCAN_SIM_CELLS
        assert big * 100_000 <= basicSettings.DBSCAN_SIM_CELLS

    def test_sklearn_backend_agrees_with_numpy(self):
        """装了 sklearn 时两种后端必须给出**同一个簇划分**。

        为什么这条要盯：clusterCode 是内容寻址的，划分一旦随后端变，
        同一份数据在两台机器上会得到两批编码 —— 而这类错不报错，
        只会表现为「昨天确认过的簇今天不见了」。
        """
        sklearn = pytest.importorskip("sklearn.cluster")
        assert sklearn is not None
        m = dbscan.makeSyntheticBlobs(blobCount=4, perBlob=5, seed=61)
        codes = codesOf(m.shape[0])
        byNumpy = dbscan.canonicalizeLabels(
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy"), codes)
        bySk = dbscan.canonicalizeLabels(
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="sklearn"), codes)
        assert byNumpy.tolist() == bySk.tolist()
        assert dbscan.clusterCodes(codes, byNumpy) == dbscan.clusterCodes(codes, bySk)

    def test_resolve_backend_rejects_unknown(self):
        with pytest.raises(dbscan.ClusterError):
            dbscan.resolveBackend("faiss")

    def test_unnormalized_input_is_handled(self):
        """传进来的向量没归一化也要算对（cos 不能 > 1把邻居关系搞乱）"""
        m = dbscan.makeSyntheticBlobs(blobCount=2, perBlob=4, seed=71)
        scaled = (m * np.float32(37.5)).astype(np.float32)   # 长度全变
        labels = dbscan.dbscan(scaled, eps=0.45, minSamples=3, backend="numpy")
        assert len(groupsOf(labels)) == 2, labels.tolist()


# ============================================================
# 二、clusterCode 幂等
# ============================================================

class TestClusterCodeIdempotent:
    def test_same_members_same_code(self):
        a = ["fc_01", "fc_02", "fc_03"]
        assert dbscan.clusterCodeOf(a) == dbscan.clusterCodeOf(list(a))

    def test_code_is_order_independent(self):
        """**打乱顺序编码不变** —— 这是幂等的核心（库的返回顺序不保证稳定）"""
        base = ["fc_%02d" % i for i in range(12)]
        want = dbscan.clusterCodeOf(base)
        rng = np.random.default_rng(3)
        for _ in range(5):
            shuffled = list(base)
            rng.shuffle(shuffled)
            assert dbscan.clusterCodeOf(shuffled) == want

    def test_code_changes_when_members_change(self):
        """成员变了就是另一个簇，编码必须跟着变（内容寻址的必然）"""
        base = ["fc_%02d" % i for i in range(4)]
        assert dbscan.clusterCodeOf(base) != dbscan.clusterCodeOf(base + ["fc_04"])
        assert dbscan.clusterCodeOf(base) != dbscan.clusterCodeOf(base[:3])

    def test_no_delimiter_collision(self):
        """分隔符必须防[a+b] 与 [ab] 撞车（空串拼接会撞）"""
        assert dbscan.clusterCodeOf(["ab", "c"]) != dbscan.clusterCodeOf(["a", "bc"])

    def test_code_fits_varchar64(self):
        big = ["fc_%03d" % i for i in range(500)]
        code = dbscan.clusterCodeOf(big)
        assert len(code) <= 64, "clusterCode 字段是 VARCHAR(64)"
        assert code.startswith(basicSettings.CLUSTER_CODE_PREFIX)

    def test_empty_members_gives_empty_code(self):
        assert dbscan.clusterCodeOf([]) == ""

    def test_rerun_produces_identical_codes(self):
        """**整体重跑**：同一份数据两次跑，逐行编码逐位相同（验收第 3 条）"""
        m = dbscan.makeSyntheticBlobs(blobCount=4, perBlob=5, seed=81)
        codes = codesOf(m.shape[0])
        first = dbscan.canonicalizeLabels(
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy"), codes)
        second = dbscan.canonicalizeLabels(
            dbscan.dbscan(m.copy(), eps=0.45, minSamples=3, backend="numpy"), codes)
        assert dbscan.clusterCodes(codes, first) == dbscan.clusterCodes(codes, second)
        assert first.tolist() == second.tolist()

    def test_noise_rows_get_empty_code(self):
        """噪声点的编码是空串（落库写 NULL），**不是 `_NOISE_`**"""
        m = np.vstack([unit(90 + i) for i in range(4)])
        labels = dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy")
        codes = dbscan.clusterCodes(codesOf(4), labels)
        assert codes == ["", "", "", ""]

    def test_codes_are_one_on_one_with_input(self):
        """等长同序：第 i 个编码必须是第 i 张脸的（错位 = 把 A 写进 B 的簇）"""
        m = dbscan.makeSyntheticBlobs(blobCount=3, perBlob=4, seed=91)
        codes = codesOf(m.shape[0])
        labels = dbscan.canonicalizeLabels(
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy"), codes)
        out = dbscan.clusterCodes(codes, labels)
        assert len(out) == len(codes)
        clusters = dbscan.buildClusters(codes, labels, vectors=m)
        for one in clusters:
            for faceCode in one.faceCodes:
                at = codes.index(faceCode)
                assert out[at] == one.clusterCode

    def test_canonical_labels_are_order_stable(self):
        """打乱输入顺序 -> 同一簇拿到**同一个** label（编号也确定）"""
        m = dbscan.makeSyntheticBlobs(blobCount=3, perBlob=4, seed=101)
        codes = codesOf(m.shape[0])
        labels = dbscan.canonicalizeLabels(
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy"), codes)
        groups = groupsOf(labels)
        order = [min(items) for _lab, items in sorted(groups.items())]
        assert order == sorted(order), "簇编号没有按最小 faceCode 排序"
        for items in groups.values():
            assert labels[items[0]] == labels[items[-1]]


# ============================================================
# 三、代表样本与簇对象
# ============================================================

class TestRepresentative:
    def test_representative_is_highest_det_score(self):
        """代表样本 = detScore 最高的那张（前端展示用）"""
        labels = np.array([0, 0, 0, 0], dtype=np.int32)
        det = [0.70, 0.95, 0.80, 0.10]
        one = dbscan.buildClusters(codesOf(4), labels, detScores=det)[0]
        assert one.representativeFaceCode == "fc_01"
        assert one.representativeDetScore == pytest.approx(0.95)

    def test_representative_handles_missing_det_score(self):
        labels = np.array([0, 0], dtype=np.int32)
        one = dbscan.buildClusters(["a", "b"], labels, detScores=[None, None])[0]
        assert one.representativeFaceCode in ("a", "b")

    def test_cluster_carries_centroid_and_size(self):
        m = dbscan.makeSyntheticBlobs(blobCount=2, perBlob=5, seed=111)
        codes = codesOf(10)
        labels = dbscan.canonicalizeLabels(
            dbscan.dbscan(m, eps=0.45, minSamples=3, backend="numpy"), codes)
        clusters = dbscan.buildClusters(codes, labels, vectors=m, detScores=[0.9] * 10)
        assert len(clusters) == 2
        for one in clusters:
            assert one.size == 5
            assert one.centroid is not None and one.centroid.shape == (
                basicSettings.EMBEDDING_DIM,)
            assert one.meanSimilarity > 1.0 - basicSettings.DBSCAN_EPS, \
                "簇内均值相似度应高于 eps 对应的相似度下限"
        stat = dbscan.summarize(clusters, faceCount=10, noiseCount=0)
        assert stat["clusters"] == 2 and stat["largest"] == 5 and stat["noise"] == 0


# ============================================================
# 四、四态口径与两个队列（**连临时库**）
# ============================================================
# ⚠️⚠️ 这是 DR-16 修正点的回归锁（验收第 5/6/7/8 条）
#   自动归属的 isConfirmed 本来就是 0，所以队列条件一旦写成
#   `personCode IS NULL OR isConfirmed=0`，**全部自动归属**会被扫进待确认队列。
#   这个错不报错、不缺函数，只会让队列悄悄长到几万条 —— 只能靠断言钉住。

def addPhoto(code, shotYear=2013):
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [{"photoCode": code, "relPath": "%s.jpg" % code,
                      "relPathHash": code.ljust(64, "0")[:64],
                      "fileHash": code.ljust(64, "1")[:64],
                      "fileSize": 1024, "shotYear": shotYear}],
        fillStandard=True)


def addPerson(code, name, avatar=None):
    sqliteCommon.insertManyTableGeneral(
        "pb_person", [{"personCode": code, "displayName": name,
                       "birthday": "1985-03-07", "avatarFaceCode": avatar,
                       "source": 0, "isConfirmed": 0}],
        conflictColumns=("personCode",), fillStandard=True)


def addFace(code, photoCode, vec, person=None, confirmed=0, stranger=0,
            bucket="2010-2014", detScore=0.9, cluster=None):
    row = {"faceCode": code, "photoCode": photoCode, "personCode": person,
           "isConfirmed": confirmed, "isStranger": stranger, "shotBucket": bucket,
           "detScore": detScore, "quality": 0.8, "delFlag": "0"}
    if cluster is not None:
        row["clusterCode"] = cluster
    if vec is not None:
        row["embedding"] = faceEngine.encodeEmbedding(vec)
    sqliteCommon.insertManyTableGeneral("pb_face", [row], fillStandard=True)


@pytest.fixture
def lib(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return temp_db


class TestQueueScope:
    def test_ten_auto_assigned_faces_leave_pending_empty(self, lib):
        """**验收第 6 条（DR-16 修正点）**：10 条自动归属 -> 待确认队列 0 条"""
        addPerson("P01", "爸爸")
        addPhoto("PH_A0")
        for i in range(10):
            addFace("fc_A%d" % i, "PH_A0", unit(500 + i), person="P01", confirmed=0)
        assert reviewQueue.countPending() == 0, \
            "自动归属被算进待确认队列了（isConfirmed=0 被当成了未归属）"
        assert reviewQueue.countDisputed() == 10
        assert reviewQueue.pendingQueue(limit=50) == []
        groups = reviewQueue.disputedList(limit=50)
        assert sum(g["faceCount"] for g in groups) == 10, "「我不同意」应当 10 张"
        states = reviewQueue.countStates()
        assert states == {"pending": 0, "disputed": 10, "confirmed": 0, "stranger": 0}

    def test_pending_matches_sql_count(self, lib):
        """**验收第 5 条**：队列条数 = 直接 SQL count（不读 pendingCount）"""
        addPhoto("PH_P0")
        for i in range(7):
            addFace("fc_P%d" % i, "PH_P0", unit(600 + i))
        assert reviewQueue.countPending() == 7
        assert len(reviewQueue.pendingQueue(limit=50)) == 7
        assert reviewQueue.selfCheck()["match"], "SQL count 与装载过滤不一致"

    def test_stranger_is_in_neither_queue(self, lib):
        """**验收第 7 条**：isStranger=1 既不进待确认也不进「我不同意」"""
        addPerson("P01", "爸爸")
        addPhoto("PH_S0")
        for i in range(4):
            addFace("fc_S%d" % i, "PH_S0", unit(700 + i), stranger=1)
        assert reviewQueue.countPending() == 0
        assert reviewQueue.countDisputed() == 0
        assert reviewQueue.pendingQueue(limit=50) == []
        assert reviewQueue.disputedList(limit=50) == []
        assert reviewQueue.countStates()["stranger"] == 4

    def test_disputed_count_matches_rows(self, lib):
        """**验收第 8 条**：「我不同意」条数 = personCode 非空且 isConfirmed=0 的行数"""
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_D0")
        for i in range(6):
            addFace("fc_D%d" % i, "PH_D0", unit(800 + i), person="P01", confirmed=0)
        for i in range(3):
            addFace("fc_E%d" % i, "PH_D0", unit(900 + i), person="P02", confirmed=1)
        assert reviewQueue.countDisputed() == 6
        groups = reviewQueue.disputedList(limit=50)
        assert sum(g["faceCount"] for g in groups) == 6
        assert groups[0]["photoCode"] == "PH_D0"

    def test_confirmed_faces_are_in_neither_queue(self, lib):
        addPerson("P01", "爸爸")
        addPhoto("PH_C0")
        for i in range(5):
            addFace("fc_C%d" % i, "PH_C0", unit(950 + i), person="P01", confirmed=1)
        assert reviewQueue.countPending() == 0
        assert reviewQueue.countDisputed() == 0
        assert reviewQueue.countStates()["confirmed"] == 5

    def test_soft_deleted_faces_are_excluded(self, lib):
        """delFlag='1' 的脸不该出现在任何队列里"""
        addPhoto("PH_X0")
        addFace("fc_X0", "PH_X0", unit(960))
        sqliteCommon.updateTableGeneral("pb_face", "faceCode = %s", ("fc_X0",),
                                        {"delFlag": "1"})
        assert reviewQueue.countPending() == 0
        assert reviewQueue.pendingQueue(limit=10) == []

    def test_disputed_groups_by_photo_and_reports_similarity(self, lib):
        """「我不同意」按 photoCode 分组，且带**当时的** similarity"""
        from processor.review import assigner as assigner
        addPerson("P01", "爸爸")
        addPhoto("PH_G0")
        addPhoto("PH_G1")
        for i in range(5):
            addFace("fc_G%d" % i, "PH_G0", unit(970 + i))
        for i in range(2):
            addFace("fc_H%d" % i, "PH_G1", unit(980 + i))
        for i in range(5):
            assigner.autoAssign("fc_G%d" % i, "P01", 0.61 + i * 0.01, recompute=False)
        for i in range(2):
            assigner.autoAssign("fc_H%d" % i, "P01", 0.55, recompute=False)
        groups = reviewQueue.disputedList(limit=50)
        assert len(groups) == 2, "应当按 photoCode 分成 2 组"
        byPhoto = dict((g["photoCode"], g) for g in groups)
        assert byPhoto["PH_G0"]["faceCount"] == 5
        assert byPhoto["PH_G1"]["faceCount"] == 2
        # 机器最有把握的那张排最前（用户最想否决的就是它）
        assert groups[0]["photoCode"] == "PH_G0"
        sims = set(f["similarity"] for f in byPhoto["PH_G0"]["faces"])
        assert None not in sims, "自动归属必须能查到当时的 similarity"
        assert max(sims) >= 0.65, "相似度取的是**最近一次**归属的值：%s" % sims
        assert byPhoto["PH_G0"]["faces"][0]["displayName"] == "爸爸"

    def test_pending_queue_orders_clustered_faces_first(self, lib):
        """冷启动时全员分数都是 None，此时队列必须**簇内优先**才可用。

        不这么排的话，簇成员会被 faceCode 打散到队列各处，
        步骤 11 的「同一簇批量改判」就无从下手。
        """
        addPhoto("PH_Q0")
        for i in range(6):
            addFace("fc_Q%d" % i, "PH_Q0", unit(2000 + i), cluster="cluster_zzz")
        items = reviewQueue.pendingQueue(limit=6, sortFirst=True)
        assert len(items) == 6
        assert all(it["clusterCode"] == "cluster_zzz" for it in items)
        assert [it["faceCode"] for it in items] == sorted(it["faceCode"]
                                                          for it in items)

    def test_pending_queue_sort_first_beats_recid_window(self, lib):
        """sortFirst=True 给的是**全局**前 N；缺省是 recID 窗口内的前 N。

        两者都不是 bug，是两种不同的东西：生成层能ORDER BY 的字段只有
        recID/shotBucket/quality/faceCode，而队列主排序键 similarity 是
        算出来的（步骤 6 明确不落库），没有可交给 SQL 的排序键。
        """
        addPhoto("PH_W0")
        for i in range(4):
            addFace("fc_W%d" % i, "PH_W0", unit(2100 + i))
        for i in range(4, 8):
            addFace("fc_W%d" % i, "PH_W0", unit(2100 + i), cluster="cluster_q1")
        window = reviewQueue.pendingQueue(limit=2)
        assert [it["faceCode"] for it in window] == ["fc_W0", "fc_W1"], \
            "缺省应当是 recID 窗口"
        top = reviewQueue.pendingQueue(limit=2, sortFirst=True)
        assert all(it["clusterCode"] == "cluster_q1" for it in top), \
            "全局排序应当先给簇内成员，实际 %s" % [it["faceCode"] for it in top]

    def test_pending_queue_sort_first_pages_without_overlap(self, lib):
        """sortFirst 的分页必须**不重不漏**（否则翻页会漏脸/重复脸）"""
        addPhoto("PH_Z0")
        for i in range(9):
            addFace("fc_Z%d" % i, "PH_Z0", unit(2200 + i), cluster="cluster_z")
        seen = []
        for at in (0, 3, 6):
            seen.extend(it["faceCode"]
                        for it in reviewQueue.pendingQueue(limit=3, offset=at,
                                                          sortFirst=True))
        assert len(seen) == 9
        assert len(set(seen)) == 9, "分页出现重复：%s" % seen

    def test_person_card_falls_back_to_confirmed_face(self, lib):
        addPerson("P01", "爸爸")                 # 没有 avatarFaceCode
        addPhoto("PH_V0")
        addFace("fc_V0", "PH_V0", unit(990), person="P01", confirmed=1, detScore=0.71)
        addFace("fc_V1", "PH_V0", unit(991), person="P01", confirmed=1, detScore=0.93)
        addFace("fc_V2", "PH_V0", unit(992), person="P01", confirmed=0, detScore=0.99)
        cards = reviewQueue.personCards()
        assert cards["P01"]["avatarFaceCode"] == "fc_V1", \
            "兜底不能选自动样本（那正是可能认错的那批）"


# ============================================================
# 五、聚类落库（连临时库）
# ============================================================

class TestClusterWrite:
    def _seed(self):
        addPerson("P01", "爸爸")
        addPhoto("PH_K0")
        # 6 张同一个人的未归类脸（一个团）
        base = unit(1200)
        for i in range(6):
            addFace("fc_K%d" % i, "PH_K0", near(base, 1, seed=1200 + i)[0])
        # 4 张已归类的脸（**绝不能被重新聚类**）
        for i in range(4):
            addFace("fc_L%d" % i, "PH_K0", unit(1300 + i), person="P01", confirmed=1)

    def test_only_unclassified_faces_get_clustered(self, lib):
        """**验收第 4 条**：只对 `personCode IS NULL AND isStranger=0` 聚类"""
        self._seed()
        result = clustering.clusterUnclassified(eps=0.45, minSamples=3,
                                                backend="numpy")
        assert result.faceCount == 6, "聚类集合里混进了已归类的脸：%d" % result.faceCount
        assert len(result.clusters) == 1
        assert sorted(result.clusters[0].faceCodes) == sorted(
            "fc_K%d" % i for i in range(6))

    def test_assigned_count_unchanged_by_clustering(self, lib):
        """**验收第 4 条**：聚类前后 personCode 非空的行数完全不变"""
        self._seed()
        before = sqliteCommon.countWhereGeneral(
            "pb_face", "personCode IS NOT NULL", ())
        clustering.applyCluster(clustering.clusterUnclassified(eps=0.45, minSamples=3,
                                                              backend="numpy"))
        after = sqliteCommon.countWhereGeneral(
            "pb_face", "personCode IS NOT NULL", ())
        assert before == after == 4
        for row in sqliteCommon.query_pb_face("pb_face", personCode="P01", mode="light"):
            assert not str(row.get("clusterCode") or ""), \
                "已归类的脸被写上了 clusterCode（%s）" % row.get("faceCode")

    def test_apply_is_idempotent(self, lib):
        """**验收第 3 条**：重跑不产生新编码（第二次全部unchanged）"""
        self._seed()
        first = clustering.applyCluster(
            clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy"))
        assert first["changed"] == 6
        result2 = clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy")
        assert result2.codes == clustering.clusterUnclassified(
            eps=0.45, minSamples=3, backend="numpy").codes
        second = clustering.applyCluster(result2)
        assert second["written"] == 0 and second["changed"] == 0
        assert second["unchanged"] == 6

    def test_noise_faces_get_null_cluster_code(self, lib):
        """噪声点的 clusterCode 落库为 NULL（**不是 `_NOISE_`**）"""
        addPhoto("PH_N0")
        for i in range(5):
            addFace("fc_N%d" % i, "PH_N0", unit(1400 + i))     # 互不相似 -> 噪声
        result = clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy")
        assert len(result.clusters) == 0 and result.noiseCount == 5
        clustering.applyCluster(result)
        for i in range(5):
            row = sqliteCommon.query_pb_face("pb_face", faceCode="fc_N%d" % i)[0]
            assert row["clusterCode"] is None, "噪声点应写 NULL，实际 %r" % row["clusterCode"]

    def test_stranger_faces_are_not_clustered(self, lib):
        """isStranger=1 的脸不进聚类集合"""
        addPhoto("PH_T0")
        base = unit(1500)
        for i in range(6):
            addFace("fc_T%d" % i, "PH_T0", near(base, 1, seed=1500 + i)[0], stranger=1)
        result = clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy")
        assert result.faceCount == 0, "陌生人被拉进聚类了"

    def test_dry_run_writes_nothing(self, lib):
        self._seed()
        stat = clustering.applyCluster(
            clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy"),
            dryRun=True)
        assert stat["changed"] == 6
        for row in sqliteCommon.query_pb_face("pb_face", mode="light"):
            assert not str(row.get("clusterCode") or ""), "dry-run 竟然写库了"

    def test_rebuild_clears_cluster_codes(self, lib):
        """--rebuild：清空后再重算，编码仍然是同一批（内容寻址）"""
        self._seed()
        first = clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy")
        clustering.applyCluster(first)
        cleared = clustering.clearClusterCodes(onlyPending=False)
        assert cleared["cleared"] == 6
        again = clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy")
        assert again.codes == first.codes, "清空重算后编码变了 -> 不是内容寻址"

    def test_state_counts_match_queue(self, lib):
        """clustering.stateCounts 与 queue.countStates 必须一致（同一个口径）"""
        addPerson("P01", "爸爸")
        addPhoto("PH_M0")
        for i in range(3):
            addFace("fc_M%d" % i, "PH_M0", unit(1600 + i), person="P01", confirmed=0)
        for i in range(2):
            addFace("fc_MB%d" % i, "PH_M0", unit(1700 + i), person="P01", confirmed=1)
        for i in range(4):
            addFace("fc_MS%d" % i, "PH_M0", unit(1800 + i), stranger=1)
        for i in range(5):
            addFace("fc_MP%d" % i, "PH_M0", unit(1900 + i))
        assert clustering.stateCounts() == reviewQueue.countStates()
        assert reviewQueue.countPending() == 5

    def test_skipped_dirty_rows_are_reported(self, lib):
        """没有向量的脏行必须**报数**并跳过（绝不静默错位）"""
        addPhoto("PH_D0")
        base = unit(2000)
        for i in range(4):
            addFace("fc_D%d" % i, "PH_D0", near(base, 1, seed=2000 + i)[0])
        sqliteCommon.insertManyTableGeneral(
            "pb_face", [{"faceCode": "fc_DIRTY", "photoCode": "PH_D0",
                         "personCode": None, "isConfirmed": 0, "isStranger": 0,
                         "shotBucket": "2010-2014", "embedding": b"\x00" * 7}],
            fillStandard=True)
        result = clustering.clusterUnclassified(eps=0.45, minSamples=3, backend="numpy")
        assert result.skipped == 1
        assert result.faceCount == 4
        assert "fc_DIRTY" not in "".join(result.faceCodes)


# ============================================================
# 六、簇视图与建议单（步骤 11 的入口；只读）
# ============================================================

class TestClusterView:
    def test_overview_aggregates_and_orders_by_size(self, lib):
        addPhoto("PH_V0")
        for i in range(3):
            addFace("fc_V%d" % i, "PH_V0", unit(2300 + i), cluster="cluster_bbb")
        for i in range(5, 9):
            addFace("fc_V%d" % i, "PH_V0", unit(2400 + i), cluster="cluster_aaa")
        for i in range(9, 12):
            addFace("fc_V%d" % i, "PH_V0", unit(2500 + i))   # 噪声，无簇
        items = reviewQueue.clusterOverview()
        assert [it["clusterCode"] for it in items] == ["cluster_aaa", "cluster_bbb"]
        assert [it["size"] for it in items] == [4, 3]
        assert reviewQueue.clusterStats()["noise"] == 3

    def test_overview_same_size_is_stable(self, lib):
        """同大小的簇必须有**确定**先后，否则翻页时同一个簇会在两页间跳"""
        addPhoto("PH_S0")
        for i in range(6):
            addFace("fc_S%d" % i, "PH_S0", unit(2600 + i),
                    cluster="cluster_%d" % (i % 2))
        first = [it["clusterCode"] for it in reviewQueue.clusterOverview()]
        for _ in range(3):
            assert [it["clusterCode"]
                    for it in reviewQueue.clusterOverview()] == first
        assert first == ["cluster_0", "cluster_1"]

    def test_overview_picks_highest_detscore_as_representative(self, lib):
        addPhoto("PH_R0")
        addFace("fc_R0", "PH_R0", unit(2700), cluster="cluster_r", detScore=0.70)
        addFace("fc_R1", "PH_R0", unit(2701), cluster="cluster_r", detScore=0.95)
        one = reviewQueue.clusterOverview()[0]
        assert one["representativeFaceCode"] == "fc_R1"
        assert one["representativeDetScore"] == pytest.approx(0.95)

    def test_overview_minsize_filters(self, lib):
        addPhoto("PH_M0")
        for i in range(3):
            addFace("fc_M%d" % i, "PH_M0", unit(2800 + i), cluster="cluster_big")
        for i in range(3, 5):
            addFace("fc_M%d" % i, "PH_M0", unit(2900 + i), cluster="cluster_small")
        assert len(reviewQueue.clusterOverview(minSize=3)) == 1

    def test_overview_excludes_assigned_and_stranger(self, lib):
        """已归类/陌生人的脸不进簇概览（它们已经不在待确认集合里）"""
        addPerson("P01", "爸爸")
        addPhoto("PH_X0")
        for i in range(3):
            addFace("fc_X%d" % i, "PH_X0", unit(3000 + i),
                    cluster="cluster_x", person="P01", confirmed=1)
        for i in range(3, 6):
            addFace("fc_X%d" % i, "PH_X0", unit(3100 + i),
                    cluster="cluster_y", stranger=1)
        assert reviewQueue.clusterOverview() == []

    def test_cluster_members_is_live(self, lib):
        """clusterMembers 现查：确认掉一个成员后 size 立刻变（内容指纹的纪律）"""
        from processor.review import assigner as assigner
        addPerson("P01", "爸爸")
        addPhoto("PH_L0")
        for i in range(4):
            addFace("fc_L%d" % i, "PH_L0", unit(3200 + i), cluster="cluster_live")
        info = reviewQueue.clusterMembers("cluster_live")
        assert info["size"] == 4 and len(info["members"]) == 4
        assigner.confirm("fc_L0", "P01", recompute=False)
        after = reviewQueue.clusterMembers("cluster_live")
        assert after["size"] == 3, "已确认的脸必须离簇（否则界面会重复劳动）"
        assert "fc_L0" not in [m["faceCode"] for m in after["members"]]

    def test_cluster_members_unknown_code_is_empty_not_error(self, lib):
        addPhoto("PH_U0")
        for i in range(3):
            addFace("fc_U%d" % i, "PH_U0", unit(3300 + i), cluster="cluster_known")
        info = reviewQueue.clusterMembers("cluster_never_existed")
        assert info["size"] == 0 and info["members"] == []
        assert reviewQueue.clusterMembers("")["size"] == 0

    def test_cluster_members_truncates_but_reports_true_size(self, lib):
        addPhoto("PH_T0")
        for i in range(7):
            addFace("fc_T%d" % i, "PH_T0", unit(3400 + i), cluster="cluster_t")
        info = reviewQueue.clusterMembers("cluster_t", limit=3)
        assert len(info["members"]) == 3
        assert info["size"] == 7, "截断的是明细，不是总数（UI 要显示真实规模）"
        assert info["truncated"] is True


class TestProposal:
    """建议单**只能按年龄排除**，且必须如实报告它有没有区分力。"""

    def test_shot_span_parses_bucket_range(self):
        assert reviewQueue._shotSpanOf(["2010-2014"]) == (2010, 2014)
        assert reviewQueue._shotSpanOf(["2010-2014", "2015-2019"]) == (2010, 2019)
        assert reviewQueue._shotSpanOf([""]) == (0, 0)
        assert reviewQueue._shotSpanOf(["ALL"]) == (0, 0)

    def test_age_filter_is_weak_by_design(self):
        """年龄筛选只能挡住**极端离谱**的情况 —— 这正是它在本库无区分力的原因。

        1938 年生的人在 2013 年的照片里是 72~76 岁，**age-ok 是正确的**
        （照片里确实可能有老人）；5 岁小孩也是 age-ok。
        真正被挡住的只有「年龄 > 100」与「负���龄（生日晚于照片）」。
        """
        assert reviewQueue._fitOf(72, 76) == "age-ok", "老人也是 age-ok"
        assert reviewQueue._fitOf(3, 7) == "age-ok"
        assert reviewQueue._fitOf(-2, 2) == "age-ok", "1~2 岁的虚岁误差不该误排"
        assert reviewQueue._fitOf(101, 105) == "age-mismatch", "101 岁不可能"
        assert reviewQueue._fitOf(-8, -4) == "age-mismatch", "生日晚于照片"

    def test_proposal_lists_all_and_flags_no_discrimination(self, lib):
        """**本库实测无区分力**：14 个簇全在 2010-2014，7/10 联系人年龄都符"""
        addPhoto("PH_P0")
        for i in range(3):
            addFace("fc_P%d" % i, "PH_P0", unit(3500 + i), cluster="cluster_p")
        persons = {
            "P_A": {"displayName": "A", "birthday": "1971-02-10"},
            "P_B": {"displayName": "B", "birthday": "1969-12-17"},
            "P_C": {"displayName": "C", "birthday": "2007-05-14"},
            "P_D": {"displayName": "D", "birthday": ""},
        }
        out = reviewQueue.clusterProposals(persons=persons)
        assert len(out) == 1
        one = out[0]
        assert one["shotSpan"] == [2010, 2014]
        assert len(one["candidates"]) == 4, "一个都不能少（排除不是删除）"
        assert one["discriminative"] is False, \
            "4 个候选人里 3 个年龄符合 -> 必须承认没有区分力"
        assert "人工" in one["note"], "必须写明这一步不能代替人确认"

    def test_proposal_is_deterministic(self, lib):
        addPhoto("PH_D0")
        for i in range(3):
            addFace("fc_D%d" % i, "PH_D0", unit(3600 + i), cluster="cluster_d")
        persons = {"P_%d" % i: {"displayName": "N%d" % i, "birthday": "1980-01-01"}
                   for i in range(5)}
        first = [c["personCode"] for c in
                 reviewQueue.clusterProposals(persons=persons)[0]["candidates"]]
        for _ in range(3):
            got = [c["personCode"] for c in
                   reviewQueue.clusterProposals(persons=persons)[0]["candidates"]]
            assert got == first, "候选顺序抖动会被当成结果不可信"


class TestSheetDir:
    """contact sheet 目录是**非库产物**：路径唯一推导 + 过期必清。"""

    def test_sheet_dir_follows_thumb_root(self, lib, tmp_path):
        from common import paths as paths
        want = os.path.join(paths.thumb_dir(), basicSettings.CLUSTER_SHEET_SUBDIR)
        assert clustering.sheetDir() == want
        assert basicSettings.CLUSTER_SHEET_SUBDIR in basicSettings.DERIVED_THUMB_SUBDIRS

    def test_derived_subdir_is_declared(self):
        from processor.media import thumbStore as thumbStore
        assert thumbStore.isDerivedSubdir(basicSettings.CLUSTER_SHEET_SUBDIR) is True
        assert thumbStore.isDerivedSubdir("thumbs") is False
        assert thumbStore.isDerivedSubdir("faces") is False

    def test_purge_removes_only_stale_sheets(self, lib, tmp_path):
        outDir = clustering.sheetDir(create=True)
        keep = os.path.join(outDir, "cluster_keep.jpg")
        stale = os.path.join(outDir, "cluster_stale.jpg")
        other = os.path.join(outDir, "notes.txt")
        for path in (keep, stale, other):
            with open(path, "wb") as hFile:
                hFile.write(b"x")
        removed = clustering.purgeStaleSheets(["cluster_keep"])
        assert removed == ["cluster_stale.jpg"]
        assert os.path.isfile(keep), "当前簇的拼图被误删了"
        assert os.path.isfile(other), "非 cluster_*.jpg 被误删了"
        assert not os.path.isfile(stale)

    def test_purge_without_codes_does_nothing(self, lib):
        outDir = clustering.sheetDir(create=True)
        path = os.path.join(outDir, "cluster_keep.jpg")
        with open(path, "wb") as hFile:
            hFile.write(b"x")
        assert clustering.purgeStaleSheets(None) == []
        assert os.path.isfile(path), "validCodes=None 时不许删任何东西"
