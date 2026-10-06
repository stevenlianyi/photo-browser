#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_review_fix.py
#Description: 修正步骤 R 单测 —— 纠错闭环（DR-16）：四态语义 / 质心防污染 / ALL 兜底桶
#             / 改判三态 / 纠错日志 / 撤销
#
# 这组用例对应 plan/step-prompts.md「修正步骤 R」第四节的 B/C/D 三段验收：
#   第 6 条  四态互斥且之和 == pb_face 总行数
#   第 7 条  质心**只**用 isConfirmed=1 的样本（防污染，逐字节可证）
#   第 8 条  ALL 兜底桶：桶样本不足但总确认样本够 -> 该人仍能被匹配到
#   第 9 条  总确认样本 < 3 的人**不启用任何质心**
#   第 10 条 shotBucket 为空的脸候选桶 = {ALL}（不再直接掉聚类）
#   第 12~14 条 fix 三态（assign / unknown / stranger）
#   第 15~17 条 merge -> undo 完整还原 / 不可撤销必须拒绝 / 每次写操作落日志
#   第 18 条 verifyLinks() 在改判与陌生人之后仍然 clean
#   第 19 条 纪律 ③ 回归：合影里 P 有 2 张脸，改判 1 张 -> 关联行必须还在
#
# 全部跑在 pytest 的 tmp 临时库上，**绝不动 d:\PhotoLib 正式库**。

import numpy as np
import pytest

from common import globalDefinition as comGD
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.face import engine as faceEngine
from engine.match import bucket as bucket
from engine.match import centroid as centroid
from engine.match import matcher as matcher
from processor.review import assigner as assigner
from processor.review import merger as merger


# ============================================================
# 一、夹具与建数据小工具
# ============================================================

def unit(seed: int):
    rng = np.random.RandomState(seed)
    return faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())


# ⚠️⚠️ R2 之后 pb_face.shotBucket 不再是一个「随手写的字面量」（DR-20/DR-22）
# --------------------------------------------------------------------------
#   它是**派生值**：bucketKeyOf(照片 shotYear, 主人 birthday)。
#   centroid.recompute/recomputePerson 执行前会做一致性前置检查
#   （centroid._assertBucketOrder -> rebucket.assertFacesFresh），
#   不等于「按当前主人的生日算出来」的那个就**直接抛错**。
#   所以夹具数据必须**自洽**：默认生日 + 默认拍摄年选成落在童年段（宽 3 年）
#   的一对，于是派生桶键恒为 FX_BKT（「2000-2002」）。字面量还在，
#   但它的含义从「随便起个名」变成了「按规则算出来就是这个」——这正是 R2 要钉住的东西。
#   成年段（宽 10 年）/ 降级等宽（宽 5 年）另有专门用例，见 test_rebucket.py。
FX_BIRTH: str = "1982-05-05"          # 默认生日
FX_SHOT: int = 2000                   # 默认拍摄年 -> 18 岁 -> 童年段
FX_BKT: str = bucket.bucketKeyOf(FX_SHOT, FX_BIRTH)          # "2000-2002"
FX_BKT_EQ: str = bucket.bucketKeyOf(FX_SHOT, None)            # "2000-2004" 降级等宽
FX_BKT_NONE: str = bucket.bucketKeyOf(None, FX_BIRTH)         # "" 无拍摄年份
#: 第二个人：生日 1985-06-01 + 拍摄年 2003 -> 18 岁 -> "2003-2005"
FX_BIRTH2: str = "1985-06-01"
FX_SHOT2: int = 2003
FX_BKT2: str = bucket.bucketKeyOf(FX_SHOT2, FX_BIRTH2)


def addPerson(code, name, birthday=FX_BIRTH):
    sqliteCommon.insertManyTableGeneral(
        "pb_person", [{"personCode": code, "displayName": name,
                       "birthday": birthday, "source": 0, "isConfirmed": 0}],
        conflictColumns=("personCode",), fillStandard=True)


def addPhoto(code, shotYear=FX_SHOT):
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [{"photoCode": code, "relPath": "%s.jpg" % code,
                      "relPathHash": code.ljust(64, "0")[:64],
                      "fileHash": code.ljust(64, "1")[:64],
                      "fileSize": 1024, "shotYear": shotYear}],
        fillStandard=True)


def addFace(code, photoCode, vec, bucketKey=None, person=None,
            confirmed=0, stranger=0):
    """⚠️ isStranger 必须**显式**写 0。

    pb_face 的 delFlag / isStranger 在 .txt 里没有 DEFAULT（delFlag 是 CHAR(1) NULL），
    漏掉它等于「不写这一列」而不是「写 0」——
    于是下面按 isStranger 判四态的用例会读到 None。
    """
    if bucketKey is None:
        bucketKey = derivedBucket(photoCode, person)
    sqliteCommon.insertManyTableGeneral(
        "pb_face", [{"faceCode": code, "photoCode": photoCode,
                     "personCode": person, "isConfirmed": confirmed,
                     "isStranger": stranger,
                     "shotBucket": (bucketKey or None),
                     "detScore": 0.9, "quality": 0.8,
                     "embedding": faceEngine.encodeEmbedding(vec)}],
        fillStandard=True)


def derivedBucket(photoCode, personCode):
    """(照片, 主人) -> 该脸**应该**的桶键。与生产同一个函数，不抄规则。"""
    shotYear = None
    for row in sqliteCommon.query_pb_photo("pb_photo", photoCode=photoCode,
                                           mode="light"):
        shotYear = row.get("shotYear")
    birthday = None
    if personCode:
        for row in sqliteCommon.query_pb_person("pb_person",
                                                personCode=personCode,
                                                mode="light"):
            birthday = row.get("birthday")
    return bucket.bucketKeyOf(shotYear, birthday)


def faceRow(faceCode):
    return sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)[0]


def centroidRow(personCode, bucketKey):
    rows = sqliteCommon.query_pb_person_centroid(
        "pb_person_centroid", personCode=personCode, bucketKey=bucketKey)
    return rows[0] if rows else None


def linkRows(personCode=None):
    return sqliteCommon.query_pb_photo_person("pb_photo_person",
                                              personCode=personCode)


def logs(opType=None):
    kwargs = {"opType": opType} if opType else {}
    return sqliteCommon.query_pb_review_log("pb_review_log", orderBy="recID",
                                            **kwargs)


def pendingFaces():
    """待确认队列 = personCode IS NULL AND isStranger=0"""
    return [row for row in sqliteCommon.query_pb_face(
        "pb_face", mode="light", nullFields=("personCode",))
        if not int(row.get("isStranger") or 0)]


def disputedFaces():
    """「我不同意」列表 = personCode 非空 AND isConfirmed=0 AND isStranger=0

    生成层没有 isConfirmed / isStranger 查询参数（见 centroid.loadFaceVectors
    的说明），所以在 Python 侧筛 —— 口径与 §4.5 的 SQL 逐条一致。
    """
    out = []
    for row in sqliteCommon.query_pb_face("pb_face", mode="light"):
        if str(row.get("personCode") or "") and not int(row.get("isConfirmed") or 0) \
                and not int(row.get("isStranger") or 0):
            out.append(row)
    return out


def fourStates():
    """四态计数（互斥）。返回 dict，四项之和 == pb_face 总行数。"""
    out = {"pending": 0, "disputed": 0, "confirmed": 0, "stranger": 0}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light", delFlag="*"):
        person = str(row.get("personCode") or "")
        if int(row.get("isStranger") or 0):
            out["stranger"] += 1
        elif int(row.get("isConfirmed") or 0):
            out["confirmed"] += 1
        elif not person:
            out["pending"] += 1
        else:
            out["disputed"] += 1
    return out


def meanBytesOf(faceCodes):
    """单测侧的**独立实现**：按库里存的向量算归一化均值 -> 2048 字节。

    刻意不复用被测代码路径（loadFaceVectors -> recompute），
    否则「质心等于它自己」这种同义反复会让字节级比对失去意义。
    """
    vecs = [faceEngine.decodeEmbedding(faceRow(code)["embedding"])
            for code in faceCodes]
    return faceEngine.encodeEmbedding(centroid.normalizedMean(vecs))


@pytest.fixture
def lib(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return temp_db


@pytest.fixture
def confirmedOnlyOn(monkeypatch):
    """把「质心只用确认样本」显式打开。

    缺省就是 True，这里显式钉住：将来若有人改了 basicSettings 的缺省值，
    这组用例不会悄悄变成在测旧口径。
    """
    monkeypatch.setattr(centroid, "CONFIRMED_ONLY", True)
    monkeypatch.setattr(basicSettings, "CENTROID_CONFIRMED_ONLY", True)
    return True


# ============================================================
# 二、第 7 条：质心防污染（本步最核心）
# ============================================================

class TestConfirmedOnlyCentroid:
    def test_unconfirmed_face_is_excluded_from_bucket(self, lib, confirmedOnlyOn):
        """**验收第 7 条前半**：桶里 2 张确认 + 1 张未确认 -> sampleCount 必须是 2。

        那张未确认的脸「属于别人」（向量与确认样本几乎无关）。
        如果它进了质心，桶质心被拉偏而 sampleCount 会显示 3 ——
        **库里看不出任何异常**，只有等匹配结果变差才发现。
        """
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        for i in range(2):
            addFace("fc_c%d" % i, "PH_1", unit(100 + i), FX_BKT, "P01", 1)
        addFace("fc_bad", "PH_1", unit(999), FX_BKT, "P01", 0)
        got = centroid.recompute("P01", FX_BKT)
        assert got["sampleCount"] == 2, "自动样本进了质心"
        assert got["skipped"]["unconfirmed"] == 1, "被排除的样本必须计数上报"
        assert got["enabled"] is False
        row = centroidRow("P01", FX_BKT)
        assert row["sampleCount"] == 2 and row["centroid"] is None

    def test_centroid_bytes_identical_with_and_without_auto_face(self, lib,
                                                                 confirmedOnlyOn):
        """**验收第 7 条后半（逐字节）**：质心与「只有确认样本时」逐字节相同。

        做法：先只放 3 张确认样本重算一次，存下那 2048 字节；
        再加一张**未确认**的（别人的）脸重算一次 —— 两次的字节必须**完全一致**。
        """
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        for i in range(3):
            addFace("fc_c%d" % i, "PH_1", unit(200 + i), FX_BKT, "P01", 1)
        centroid.recomputePerson("P01")
        cleanRow = centroidRow("P01", FX_BKT)
        cleanBytes = cleanRow["centroid"]
        assert cleanRow["sampleCount"] == 3 and cleanBytes is not None
        assert cleanBytes == meanBytesOf(["fc_c0", "fc_c1", "fc_c2"])

        addFace("fc_bad", "PH_1", unit(998), FX_BKT, "P01", 0)
        centroid.recomputePerson("P01")
        dirtyRow = centroidRow("P01", FX_BKT)
        assert dirtyRow["sampleCount"] == 3, \
            "确认 3 张 + 自动 1 张，sampleCount 必须仍是 3"
        assert dirtyRow["centroid"] == cleanBytes, \
            "质心被自动样本污染了（2048 字节级比对）"
        # 兜底桶同样只数确认样本
        allRow = centroidRow("P01", centroid.ALL_BUCKET)
        assert allRow["sampleCount"] == 3
        assert allRow["centroid"] == cleanBytes

    def test_all_bucket_ignores_bucket_split(self, lib, confirmedOnlyOn):
        """ALL 桶 = 该人**全部**确认样本，不分年代桶"""
        # 三个桶必须各自算得出（R2 之后 shotBucket 是派生值）：
        #   生日 1987-06-01 → 拍摄年 1998 / 2001 / 2005 = 11 / 14 / 18 岁
        #   → 童年段（宽 3 年）-> 三个不同桶键
        born, years = "1987-06-01", (1998, 2001, 2005)
        keys = tuple(bucket.bucketKeyOf(y, born) for y in years)
        assert len(set(keys)) == 3, "夹具前提：三个拍摄年要落在三个桶里"
        addPerson("P01", "爸爸", born)
        for seed, (year, key) in zip((300, 301, 302), zip(years, keys)):
            addPhoto("PH_%d" % seed, shotYear=year)
            addFace("fc_%d" % seed, "PH_%d" % seed, unit(seed), key, "P01", 1)
        info = centroid.recompute("P01", centroid.ALL_BUCKET)
        assert info["isAllBucket"] is True
        assert info["sampleCount"] == 3, "三个桶各 1 张，ALL 应聚成 3 张"
        assert info["enabled"] is True
        assert centroidRow("P01", centroid.ALL_BUCKET)["centroid"] == \
            meanBytesOf(["fc_300", "fc_301", "fc_302"])

    def test_switch_off_restores_old_behaviour(self, lib, monkeypatch):
        """CENTROID_CONFIRMED_ONLY=False -> 退回旧口径（全样本），仅用于回归对比"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        for i in range(2):
            addFace("fc_c%d" % i, "PH_1", unit(400 + i), FX_BKT, "P01", 1)
        addFace("fc_bad", "PH_1", unit(401), FX_BKT, "P01", 0)
        assert centroid.recompute("P01", FX_BKT)["sampleCount"] == 2
        monkeypatch.setattr(centroid, "CONFIRMED_ONLY", False)
        got = centroid.recompute("P01", FX_BKT)
        assert got["sampleCount"] == 3, "关掉开关就该用全部样本（旧口径）"
        assert got["confirmedOnly"] is False
        assert got["skipped"]["unconfirmed"] == 0

    def test_list_buckets_excludes_all(self, lib, confirmedOnlyOn):
        """**ALL 是虚拟桶**：listBucketsOf() 绝不能把它当成一个年代桶"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(500 + i), FX_BKT, "P01", 1)
        assert centroid.listBucketsOf("P01") == [FX_BKT]
        assert centroid.ALL_BUCKET not in centroid.listBucketsOf("P01")
        # recomputePerson 会额外算 ALL —— 它不是「一个年代桶」，是兜底桶
        stat = centroid.recomputePerson("P01")
        assert centroid.ALL_BUCKET in [r["bucketKey"] for r in stat["buckets"]]
        assert stat["dropped"] == 0, "ALL 行不该在重算时被当成僵尸桶清掉"
        assert centroidRow("P01", centroid.ALL_BUCKET) is not None


# ============================================================
# 三、第 8/9/10 条：ALL 兜底桶与三级启用
# ============================================================

class TestAllFallbackBucket:
    def test_bucket_shortfall_falls_back_to_all(self, lib, confirmedOnlyOn):
        """**验收第 8 条**：某桶确认样本只 1 张、总确认样本 4 张
        -> 该桶不启用、**ALL 桶启用且 sampleCount=4**，且**能被匹配到**。"""
        # 两个桶都必须各自算得出：生日 1987-06-01
        #   → 拍摄年 1998（14 岁）= BKT_A / 2001（16 岁）= BKT_B
        born = "1987-06-01"
        BKT_A, BKT_B = (bucket.bucketKeyOf(y, born) for y in (1998, 2001))
        assert BKT_A != BKT_B, "夹具前提：两个拍摄年要落在不同桶里"
        addPerson("P01", "爸爸", born)
        base = unit(600)
        addPhoto("PH_0", shotYear=1998)
        addFace("fc_b0", "PH_0", base, BKT_A, "P01", 1)                # 本桶 1 张
        for i in range(3):
            addPhoto("PH_o%d" % i, shotYear=2001)
            addFace("fc_o%d" % i, "PH_o%d" % i,
                    faceEngine.l2normalize(base + 0.01 * unit(610 + i)),
                    BKT_B, "P01", 1)                                  # 别的桶 3 张
        stat = centroid.recomputePerson("P01")
        rows = dict((r["bucketKey"], r) for r in stat["buckets"])
        assert rows[BKT_A]["sampleCount"] == 1
        assert rows[BKT_A]["enabled"] is False, "1 个样本的桶不该启用"
        assert rows[BKT_B]["enabled"] is True
        assert rows[centroid.ALL_BUCKET]["sampleCount"] == 4, \
            "ALL 桶必须汇总该人的全部确认样本"
        assert rows[centroid.ALL_BUCKET]["enabled"] is True

        # 「该人能被匹配到」：一张落在 BKT_A 的新脸，候选桶含 ALL -> 认得出
        addPhoto("PH_new", shotYear=1998)
        addFace("fc_new", "PH_new", faceEngine.l2normalize(base + 0.02 * unit(699)),
                BKT_A)
        _m, index = centroid.loadAllCentroids()
        got = matcher.match(faceRow("fc_new"), index=index)
        assert got.decision == matcher.DECISION_AUTO
        assert got.personCode == "P01"
        assert got.topCandidates[0].bucketKey == centroid.ALL_BUCKET, \
            "本桶没启用，命中的是 ALL 兜底桶"

    def test_person_with_two_confirmed_faces_has_no_centroid(self, lib,
                                                             confirmedOnlyOn):
        """**验收第 9 条**：总确认样本 2 张 -> **不启用任何质心**，
        loadAllCentroids 的索引里查无此人。"""
        addPerson("P01", "样本不足")
        addPhoto("PH_1")
        for i in range(2):
            addFace("fc_%d" % i, "PH_1", unit(700 + i), FX_BKT, "P01", 1)
        stat = centroid.recomputePerson("P01")
        assert stat["enabled"] == 0
        for row in stat["buckets"]:
            assert row["enabled"] is False
            assert row["sampleCount"] < basicSettings.MIN_CENTROID_SAMPLES
        _m, index = centroid.loadAllCentroids()
        assert len(index) == 0
        assert index.rowOf("P01", FX_BKT) == -1
        assert index.rowOf("P01", centroid.ALL_BUCKET) == -1

    def test_all_bucket_downgrades_when_samples_disappear(self, lib,
                                                          confirmedOnlyOn):
        """确认样本被搬走 -> ALL 行跟着降级（不留僵尸质心）"""
        addPerson("P01", "会变的人")
        addPhoto("PH_1")
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(800 + i), FX_BKT, "P01", 1)
        centroid.recomputePerson("P01")
        assert centroid.centroidOf("P01", centroid.ALL_BUCKET) is not None
        assigner.fix("fc_0", "unknown")
        assigner.fix("fc_1", "unknown")
        _m, index = centroid.loadAllCentroids()
        assert centroidRow("P01", centroid.ALL_BUCKET)["sampleCount"] == 1
        assert centroid.centroidOf("P01", centroid.ALL_BUCKET) is None
        assert len(index) == 0

    def test_face_without_bucket_uses_all(self, lib, confirmedOnlyOn):
        """**验收第 10 条**：shotBucket 为空 -> 候选桶 = {ALL}，能拿到分数"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        base = unit(900)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", base, FX_BKT, "P01", 1)
        centroid.recomputePerson("P01")
        addPhoto("PH_shot", shotYear=None)
        # ⚠️ 这张脸**已归属**且没有拍摄年（截图）。
        #   已归属 → 候选桶 = 相邻三桶（空）∪ {ALL} = {ALL}。
        #   若它**未归属**，DR-21 会把候选放宽成「全部已启用桶」，
        #   那条路径由 test_rebucket.py 专门覆盖。
        addFace("fc_shot", "PH_shot", base, None, "P01")
        assert faceRow("fc_shot")["shotBucket"] is None
        assert matcher.candidateBucketKeys("") == [centroid.ALL_BUCKET]
        _m, index = centroid.loadAllCentroids()
        got = matcher.match(faceRow("fc_shot"), index=index)
        assert got.candidateBuckets == [centroid.ALL_BUCKET]
        assert got.decision == matcher.DECISION_AUTO
        assert got.score > basicSettings.matchThresholds()[1]


# ============================================================
# 四、第 12~14 条：改判三态
# ============================================================

class TestFixActions:
    def _twoPersonsThreeEach(self, seedBase=1000):
        # 同生日 + 同拍摄年 -> 两人落在**同一个桶** FX_BKT
        #（本文件的多个用例都断言两人共享桶键）
        addPerson("P01", "爸爸", FX_BIRTH)
        addPerson("P02", "妈妈", FX_BIRTH)
        for who in ("P01", "P02"):
            for i in range(3):
                photo = "PH_%s%d" % (who, i)
                addPhoto(photo)
                addFace("fc_%s%d" % (who, i), photo,
                        unit(seedBase + i), FX_BKT)
        for i in range(3):
            assigner.confirm("fc_P01%d" % i, "P01")
        for i in range(3):
            assigner.confirm("fc_P02%d" % i, "P02")

    def test_fix_assign_recomputes_both_persons(self, lib, confirmedOnlyOn):
        """**验收第 12 条**：改判后**原人与新人**的质心都重算。

        只重算一边算不合格：原人的质心会永远停在「包含这张已经不属于他的脸」
        的旧值上，而且**不报错**。
        """
        self._twoPersonsThreeEach()
        assert (centroidRow("P01", FX_BKT)["sampleCount"],
                centroidRow("P02", FX_BKT)["sampleCount"]) == (3, 3)
        p02Before = centroid.centroidOf("P02", FX_BKT).copy()

        out = assigner.fix("fc_P010", "assign", "P02")
        assert out["changed"] is True
        assert out["fromPerson"] == "P01" and out["toPerson"] == "P02"
        face = faceRow("fc_P010")
        assert face["personCode"] == "P02"
        assert int(face["isConfirmed"]) == 1, "改判 = 人工确认"

        # ① 原人：样本少一条，且必须**跌出启用线**
        row01 = centroidRow("P01", FX_BKT)
        assert row01["sampleCount"] == 2
        assert row01["centroid"] is None, "原人掉到 2 个样本，质心必须失效"
        assert centroid.centroidOf("P01", FX_BKT) is None
        # ② 新人：多一条，且内容确实变了
        row02 = centroidRow("P02", FX_BKT)
        assert row02["sampleCount"] == 4 and row02["centroid"] is not None
        assert not np.array_equal(centroid.centroidOf("P02", FX_BKT), p02Before)
        # ③ 旧 linkKey 已删、新行 source=1
        assert sorted(r["linkKey"] for r in linkRows("P01")) == \
            ["PH_P011:P01", "PH_P012:P01"]
        newLink = [r for r in linkRows("P02") if r["linkKey"] == "PH_P010:P02"][0]
        assert int(newLink["source"]) == comGD.LINK_SOURCE_MANUAL
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_fix_unknown_goes_to_pending_queue(self, lib, confirmedOnlyOn):
        """**验收第 13 条**：fix('unknown') -> 该脸回待确认队列"""
        self._twoPersonsThreeEach()
        out = assigner.fix("fc_P010", "unknown")
        assert out["changed"] is True
        face = faceRow("fc_P010")
        assert face["personCode"] is None
        assert int(face["isConfirmed"]) == 0
        assert int(face["isStranger"]) == 0
        assert "fc_P010" in [r["faceCode"] for r in pendingFaces()]
        assert "fc_P010" not in [r["faceCode"] for r in disputedFaces()]
        # 关联行按纪律 ③ 删掉（这张照片里再没有脸属于 P01）
        assert sorted(r["linkKey"] for r in linkRows("P01")) == \
            ["PH_P011:P01", "PH_P012:P01"]
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 2
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_fix_stranger_leaves_all_three_collections(self, lib, confirmedOnlyOn):
        """**验收第 14 条**：陌生人**既不在待确认、也不在「我不同意」、也不参与匹配**"""
        self._twoPersonsThreeEach()
        out = assigner.fix("fc_P010", "stranger")
        assert out["changed"] is True
        face = faceRow("fc_P010")
        assert face["personCode"] is None
        assert int(face["isStranger"]) == 1
        assert "fc_P010" not in [r["faceCode"] for r in pendingFaces()]
        assert "fc_P010" not in [r["faceCode"] for r in disputedFaces()]
        # 质心里也不能有它（否则陌生人会通过质心间接"参与"匹配）
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 2
        assert sorted(r["linkKey"] for r in linkRows("P01")) == \
            ["PH_P011:P01", "PH_P012:P01"]
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_fix_unknown_clears_stranger_flag(self, lib, confirmedOnlyOn):
        """反向：陌生人 -> 置为未知 -> 回到待确认队列（isStranger 必须清 0）"""
        self._twoPersonsThreeEach()
        assigner.fix("fc_P010", "stranger")
        assigner.fix("fc_P010", "unknown")
        face = faceRow("fc_P010")
        assert face["personCode"] is None
        assert int(face["isStranger"]) == 0
        assert "fc_P010" in [r["faceCode"] for r in pendingFaces()]

    def test_fix_assign_clears_stranger_flag(self, lib, confirmedOnlyOn):
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(1100), FX_BKT)
        assigner.fix("fc_1", "stranger")
        assert int(faceRow("fc_1")["isStranger"]) == 1
        assigner.fix("fc_1", "assign", "P02")
        face = faceRow("fc_1")
        assert face["personCode"] == "P02"
        assert int(face["isStranger"]) == 0, "改判到某人之后不该还挂着陌生人标记"

    def test_fix_rejects_bad_arguments(self, lib):
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(1200), FX_BKT)
        with pytest.raises(assigner.AssignerError):
            assigner.fix("fc_1", "delete_everything")
        with pytest.raises(assigner.AssignerError):
            assigner.fix("fc_1", "assign")           # assign 必须给 personCode
        with pytest.raises(assigner.AssignerError):
            assigner.fix("fc_nope", "unknown")
        with pytest.raises(assigner.AssignerError):
            assigner.fix("fc_1", "assign", "P_nope")

    def test_batch_fix_reports_failures_and_recomputes(self, lib, confirmedOnlyOn):
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        for i in range(4):
            addPhoto("PH_%d" % i)
            addFace("fc_%d" % i, "PH_%d" % i, unit(1300 + i), FX_BKT)
        assigner.confirmPerson("P01", ["fc_0", "fc_1", "fc_2", "fc_3"])
        out = assigner.batchFix(["fc_0", "fc_1"], "assign", "P02")
        assert out["fixed"] == 2 and not out["failed"]
        assert faceRow("fc_0")["personCode"] == "P02"
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 2
        assert centroidRow("P02", FX_BKT)["sampleCount"] == 2
        out2 = assigner.batchFix(["fc_2", "fc_missing"], "unknown")
        assert out2["fixed"] == 1 and len(out2["failed"]) == 1
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_discipline3_link_survives_partial_fix(self, lib, confirmedOnlyOn):
        """**验收第 19 条**：合影里 P 有 2 张脸，改判1 张 -> **P 的关联行必须还在**。

        这是纪律 ③ 的核心：关联是 (照片 × 人)，不是 (照片 × 人 × 脸)。
        误删的后果是「人员时间轴上凭空少一张照片」，而且**没有任何报错**。
        """
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_1")
        addFace("fc_a", "PH_1", unit(1400), FX_BKT)
        addFace("fc_b", "PH_1", unit(1401), FX_BKT)
        assigner.confirm("fc_a", "P01")
        assigner.confirm("fc_b", "P01")
        assert len(linkRows("P01")) == 1
        assigner.fix("fc_a", "assign", "P02")
        assert [r["linkKey"] for r in linkRows("P01")] == ["PH_1:P01"], \
            "PH_1 里还剩一张脸属于 P01，关联行被误删了"
        # 反过来：最后一张脸也走之后，关联行才消失
        assigner.fix("fc_b", "assign", "P02")
        assert linkRows("P01") == []
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_unassign_is_fix_unknown(self, lib, confirmedOnlyOn):
        """unassign() 就是 fix('unknown') —— 不留两套语义"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(1500), FX_BKT)
        assigner.confirm("fc_1", "P01")
        out = assigner.unassign("fc_1")
        assert out["action"] == "unknown"
        assert faceRow("fc_1")["personCode"] is None
        assert int(faceRow("fc_1")["isConfirmed"]) == 0


# ============================================================
# 五、第 5/6/17 条：isConfirmed 语义 + 四态互斥 + 纠错日志
# ============================================================

class TestConfirmedSemanticsAndLog:
    def test_auto_assign_writes_zero_confirmed(self, lib, confirmedOnlyOn):
        """**根因那条**：自动归属必须写 isConfirmed=0，否则进不了「我不同意」"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(1600), FX_BKT)
        info = assigner.autoAssign("fc_1", "P01", 0.77)
        assert info["isConfirmed"] == 0
        assert int(faceRow("fc_1")["isConfirmed"]) == 0
        assert int(linkRows("P01")[0]["source"]) == comGD.LINK_SOURCE_AUTO
        assert [r["faceCode"] for r in disputedFaces()] == ["fc_1"]
        # 再确认一次 -> 转成「人工确认」，四态跟着变
        assigner.confirm("fc_1", "P01")
        assert int(faceRow("fc_1")["isConfirmed"]) == 1
        assert disputedFaces() == []
        assert int(linkRows("P01")[0]["source"]) == comGD.LINK_SOURCE_MANUAL

    def test_auto_sample_never_pollutes_centroid_end_to_end(self, lib,
                                                           confirmedOnlyOn):
        """端到端：确认 3 张 -> 新脸自动归属 -> **质心 sampleCount 仍然是 3**"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        base = unit(1700)
        for i in range(3):
            addFace("fc_s%d" % i, "PH_1", base, FX_BKT)
        assigner.confirmPerson("P01", ["fc_s0", "fc_s1", "fc_s2"])
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 3
        addPhoto("PH_2")
        addFace("fc_new", "PH_2", faceEngine.l2normalize(base + 0.01 * unit(1701)),
                FX_BKT)
        _m, index = centroid.loadAllCentroids()
        got = matcher.match(faceRow("fc_new"), index=index)
        assert got.decision == matcher.DECISION_AUTO
        assigner.applyAuto([got])
        assert int(faceRow("fc_new")["isConfirmed"]) == 0
        centroid.recomputePerson("P01")
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 3
        assert centroidRow("P01", centroid.ALL_BUCKET)["sampleCount"] == 3
        assert "fc_new" in [r["faceCode"] for r in disputedFaces()]

    def test_four_states_are_mutually_exclusive_and_exhaustive(self, lib,
                                                               confirmedOnlyOn):
        """**验收第 6 条**：四态之和 == pb_face 总行数（不重不漏）"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        for name in ("fc_pending", "fc_auto", "fc_confirmed", "fc_stranger"):
            addFace(name, "PH_1", unit(1800 + len(name)), FX_BKT)
        assigner.autoAssign("fc_auto", "P01", 0.61)
        assigner.confirm("fc_confirmed", "P01")
        assigner.fix("fc_stranger", "stranger")
        stat = fourStates()
        assert stat == {"pending": 1, "disputed": 1, "confirmed": 1, "stranger": 1}
        assert sum(stat.values()) == sqliteCommon.countTableGeneral("pb_face",
                                                                    delFlag="*")

    def test_every_write_operation_logs(self, lib, confirmedOnlyOn):
        """**验收第 17 条**：确认 / 自动 / 改判 / 置未知 / 陌生人 各落一条日志"""
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_1")
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(1900 + i), FX_BKT)
        assert len(logs()) == 0

        assigner.confirm("fc_0", "P01")
        assert len(logs(assigner.OP_ASSIGN)) == 1
        assigner.autoAssign("fc_1", "P01", 0.66)
        assert len(logs(assigner.OP_ASSIGN)) == 2
        assigner.fix("fc_0", "assign", "P02")
        fixLogs = logs(assigner.OP_FIX)
        assert len(fixLogs) == 1
        assert fixLogs[0]["fromPersonCode"] == "P01"
        assert fixLogs[0]["toPersonCode"] == "P02"
        assert fixLogs[0]["faceCode"] == "fc_0"
        assert int(fixLogs[0]["isRevertible"]) == 0
        assigner.fix("fc_1", "unknown")
        unknownLogs = logs(assigner.OP_UNKNOWN)
        assert len(unknownLogs) == 1
        assert unknownLogs[0]["fromPersonCode"] == "P01"
        # ⚠️ toPersonCode 为空：这一列必须**真的**被写成 NULL。
        #   用 update_* 写会是一次静默的空操作（见 logReview 的说明），
        #   库里会留着上一次的旧值 —— 那等于留下一条假记录。
        assert unknownLogs[0]["toPersonCode"] is None
        assigner.fix("fc_2", "stranger")
        strangerLogs = logs(assigner.OP_STRANGER)
        assert len(strangerLogs) == 1 and strangerLogs[0]["toPersonCode"] is None

    def test_log_from_person_code_null_is_really_null(self, lib):
        """**fromPersonCode 为空也必须真写成 NULL**（首次确认的常见情形）"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2000), FX_BKT)
        assigner.confirm("fc_1", "P01")
        row = logs(assigner.OP_ASSIGN)[0]
        assert row["fromPersonCode"] is None
        assert row["toPersonCode"] == "P01"
        assert row["photoCode"] == "PH_1"
        assert int(row["faceCount"]) == 1
        assert int(row["isRevertible"]) == 0
        assert row["opUser"] == basicSettings.REVIEW_LOG_USER
        assert len(str(row["opYMDHMS"] or "")) == 14

    def test_auto_log_records_similarity(self, lib):
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2100), FX_BKT)
        assigner.autoAssign("fc_1", "P01", 0.6123)
        row = logs(assigner.OP_ASSIGN)[0]
        assert float(row["similarity"]) == pytest.approx(0.6123, abs=1e-4)

    def test_manual_confirm_writes_no_fake_similarity(self, lib):
        """人工确认**不编造分数**（否则统计会把人工当成高置信自动匹配）"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2200), FX_BKT)
        assigner.confirm("fc_1", "P01")
        assert logs(assigner.OP_ASSIGN)[0]["similarity"] is None

    def test_batch_confirm_writes_one_batch_log(self, lib):
        addPerson("P01", "爸爸")
        for i in range(3):
            addPhoto("PH_%d" % i)
            addFace("fc_%d" % i, "PH_%d" % i, unit(2300 + i), FX_BKT)
        assigner.confirmPerson("P01", ["fc_0", "fc_1", "fc_2"])
        batch = logs(assigner.OP_BATCH_ASSIGN)
        assert len(batch) == 1
        assert int(batch[0]["faceCount"]) == 3
        assert batch[0]["toPersonCode"] == "P01"

    def test_log_rejects_unregistered_optype(self, lib):
        with pytest.raises(assigner.AssignerError):
            assigner.logReview("NOT_A_REAL_OP", faceCode="x")


# ============================================================
# 六、第 15/16 条：合并 -> 撤销
# ============================================================

class TestUndo:
    def _mergeFixture(self):
        addPerson("P01", "爸爸", FX_BIRTH)
        addPerson("P02", "妈妈", FX_BIRTH)
        for who, seed in (("P01", 2400), ("P02", 2500)):
            for i in range(3):
                photo = "PH_%s%d" % (who, i)
                addPhoto(photo)
                addFace("fc_%s%d" % (who, i), photo, unit(seed + i), FX_BKT)
        for i in range(3):
            assigner.confirm("fc_P01%d" % i, "P01")
        for i in range(3):
            assigner.confirm("fc_P02%d" % i, "P02")
        centroid.recomputePerson("P01")
        centroid.recomputePerson("P02")

    def test_merge_then_undo_restores_everything(self, lib, confirmedOnlyOn):
        """**验收第 15 条**：merge -> undo 完整还原
        （人脸归属 + pb_photo_person + 双方质心 + fromPerson 的 delFlag）"""
        self._mergeFixture()
        p01Vec = centroid.centroidOf("P01", FX_BKT).copy()
        p02Vec = centroid.centroidOf("P02", FX_BKT).copy()
        assert len(linkRows("P01")) == 3 and len(linkRows("P02")) == 3

        merged = merger.merge("P01", "P02")
        assert merged["logCode"], "合并必须落可撤销的日志"
        assert faceRow("fc_P010")["personCode"] == "P02"
        assert linkRows("P01") == []
        assert len(linkRows("P02")) == 6
        old = sqliteCommon.query_pb_person("pb_person", personCode="P01",
                                           delFlag="*")[0]
        assert old["delFlag"] == "1"
        assert merged["logCode"] in [r["logCode"] for r in merger.revertibleList()]

        out = merger.undo(merged["logCode"])
        assert out["facesRestored"] == 3
        # ① 人脸归属逐张还原
        for i in range(3):
            assert faceRow("fc_P01%d" % i)["personCode"] == "P01"
            assert int(faceRow("fc_P01%d" % i)["isConfirmed"]) == 1
        # ② 关联行还原
        assert sorted(r["linkKey"] for r in linkRows("P01")) == \
            ["PH_P010:P01", "PH_P011:P01", "PH_P012:P01"]
        # ③ 双方质心重算，且**内容**回到合并前（不是只看行数）
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 3
        assert np.allclose(centroid.centroidOf("P01", FX_BKT), p01Vec, atol=1e-6)
        assert np.allclose(centroid.centroidOf("P02", FX_BKT), p02Vec, atol=1e-6)
        # ④ fromPerson 的软删被撤销，合并痕迹也清掉
        restored = sqliteCommon.query_pb_person("pb_person", personCode="P01",
                                                delFlag="*")[0]
        assert restored["delFlag"] == "0"
        assert not str(restored["memo"] or "").startswith(merger._MERGE_MEMO)
        # ⑤ 原日志回填 revertedByLogCode，并写了一条 UNDO 日志
        head = sqliteCommon.query_pb_review_log("pb_review_log",
                                                logCode=merged["logCode"])[0]
        assert head["revertedByLogCode"] == out["undoLogCode"]
        undoRows = logs(assigner.OP_UNDO)
        assert len(undoRows) == 1 and int(undoRows[0]["faceCount"]) == 3
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_undo_split_restores_previous_state(self, lib, confirmedOnlyOn):
        self._mergeFixture()
        out = merger.split("fc_P010", "P09", "其实是舅舅")
        assert out["logCode"]
        assert faceRow("fc_P010")["personCode"] == "P09"
        assert int(faceRow("fc_P010")["isConfirmed"]) == 1
        back = merger.undo(out["logCode"])
        assert back["facesRestored"] == 1
        face = faceRow("fc_P010")
        assert face["personCode"] == "P01", "撤销要还原成拆分**之前**的归属人"
        assert int(face["isConfirmed"]) == 1
        assert "PH_P010:P01" in [r["linkKey"] for r in linkRows("P01")]
        assert centroidRow("P01", FX_BKT)["sampleCount"] == 3
        # P09 撤销后**一张脸都没有了** -> 它的年代桶质心必须被清掉
        #（不清的话那条向量会继续参与匹配，而它代表的分布已经不存在了）
        assert centroidRow("P09", FX_BKT) is None
        assert centroid.centroidOf("P09", centroid.ALL_BUCKET) is None
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_undo_split_to_unknown_restores_owner(self, lib, confirmedOnlyOn):
        """拆成「未归属」再撤销 -> personCode 回到原主"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2600), FX_BKT)
        assigner.confirm("fc_1", "P01")
        out = merger.split("fc_1")
        assert faceRow("fc_1")["personCode"] is None
        merger.undo(out["logCode"])
        assert faceRow("fc_1")["personCode"] == "P01"
        assert "PH_1:P01" in [r["linkKey"] for r in linkRows("P01")]

    def test_undo_refuses_non_revertible(self, lib):
        """**验收第 16 条**：对普通确认（isRevertible=0）必须报错拒绝"""
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2700), FX_BKT)
        assigner.confirm("fc_1", "P01")
        logCode = logs(assigner.OP_ASSIGN)[0]["logCode"]
        with pytest.raises(merger.MergeError) as err:
            merger.undo(logCode)
        assert "不可撤销" in str(err.value)
        # 数据没被动过
        assert faceRow("fc_1")["personCode"] == "P01"
        assert int(faceRow("fc_1")["isConfirmed"]) == 1

    def test_undo_refuses_twice(self, lib):
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2800), FX_BKT)
        assigner.confirm("fc_1", "P01")
        out = merger.split("fc_1", "P02")
        merger.undo(out["logCode"])
        with pytest.raises(merger.MergeError) as err:
            merger.undo(out["logCode"])
        assert "撤销过" in str(err.value)

    def test_undo_refuses_unknown_logcode(self, lib):
        with pytest.raises(merger.MergeError):
            merger.undo("RL_19700101000000_deadbe")
        with pytest.raises(merger.MergeError):
            merger.undo("")

    def test_revertible_list_excludes_used_and_non_revertible(self, lib):
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(2900), FX_BKT)
        addFace("fc_2", "PH_1", unit(2901), FX_BKT)
        assigner.confirm("fc_1", "P01")
        assert merger.revertibleList() == [], "普通确认不可撤销，不该进候选集"
        out = merger.split("fc_2", "P02")
        assert [r["logCode"] for r in merger.revertibleList()] == [out["logCode"]]
        merger.undo(out["logCode"])
        assert merger.revertibleList() == [], "已撤销的不该再出现在候选集里"

    def test_merge_log_has_member_rows(self, lib):
        """合并的成员行：撤销要精确知道哪些脸要搬回去"""
        self._mergeFixture()
        out = merger.merge("P01", "P02")
        members = [r for r in logs(assigner.OP_MERGE)
                   if r["logCode"] != out["logCode"]]
        assert sorted(r["faceCode"] for r in members) == \
            ["fc_P010", "fc_P011", "fc_P012"]
        for row in members:
            assert int(row["isRevertible"]) == 1
            assert "op=%s" % out["logCode"] in str(row["detail"])
        head = sqliteCommon.query_pb_review_log("pb_review_log",
                                                logCode=out["logCode"])[0]
        assert head["faceCode"] is None
        assert int(head["faceCount"]) == 3
        assert head["fromPersonCode"] == "P01" and head["toPersonCode"] == "P02"


# ============================================================
# 七、第 18 条：改判 / 陌生人之后 verifyLinks 仍然 clean
# ============================================================

class TestVerifyAfterFix:
    def test_verify_clean_after_all_three_actions(self, lib, confirmedOnlyOn):
        addPerson("P01", "爸爸")
        addPerson("P02", "妈妈")
        addPhoto("PH_1")
        for i in range(4):
            addFace("fc_%d" % i, "PH_1", unit(3000 + i), FX_BKT)
        for i in range(4):
            assigner.confirm("fc_%d" % i, "P01")
        assigner.syncLinks()
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()
        assigner.fix("fc_0", "assign", "P02")
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()
        assigner.fix("fc_1", "unknown")
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()
        assigner.fix("fc_2", "stranger")
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_pending_source_excludes_stranger(self, lib):
        """run_match.loadPendingFaces 是待确认队列的**唯一取数口**，
        它必须排除陌生人 —— 否则标记了陌生人的人脸照样会出现在队列里。"""
        from tools import run_match as runMatch
        addPerson("P01", "爸爸")
        addPhoto("PH_1")
        addFace("fc_1", "PH_1", unit(3100), FX_BKT)
        addFace("fc_2", "PH_1", unit(3101), FX_BKT)
        assigner.fix("fc_2", "stranger")
        codes = [r["faceCode"] for r in runMatch.loadPendingFaces()]
        assert codes == ["fc_1"]
        assert runMatch.countStates() == {"pending": 1, "disputed": 0,
                                          "confirmed": 0, "stranger": 1}


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行："
                     "python -m pytest code/src/test/test_review_fix.py -v")

