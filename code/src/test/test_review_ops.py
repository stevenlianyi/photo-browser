#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_review_ops.py
#Description: 步骤 6 单测 —— 质心重算 / 归属写入 / 合并拆分（**需要临时库**）
#
# 为什么这组必须连真库
# ------------------
#   测的不只是算术，还有三件靠"看代码觉得对"永远验不出来的事：
#     ① upsert 在 (personCode, bucketKey) 上真的幂等（跑两遍不产生第二行）
#     ② 关联行 linkKey 重算后真的不撞 UNIQUE（合并时最常见的崩溃点）
#     ③ sampleCount < 3 时库里那列**真的是 NULL**（"不启用"必须在数据里看得见，
#        而不是只活在代码的 if 里）
#   这三条都要 SQLite 参与才算验证过。
#
# 全部跑在 pytest 的 tmp 临时库上，**绝不动 d:\PhotoLib 正式库**。

import numpy as np
import pytest

from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.face import engine as faceEngine
from engine.match import centroid as centroid
from engine.match import matcher as matcher
from processor.review import assigner as assigner
from processor.review import merger as merger


# ============================================================
# 一、夹具：造一个「两个��� + 若干张脸」的临时库
# ============================================================

def unit(seed: int):
    rng = np.random.RandomState(seed)
    return faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())


# ⚠️ 三个 add* 都必须带 fillStandard=True
# --------------------------------------------------------------
#   生成层的所有 query_* / countTableGeneral 默认过滤 `delFlag = '0'`，
#   而 pb_*.txt 里 delFlag 是 `CHAR(1) NULL`（**没有 DEFAULT**）。
#   漏掉 fillStandard -> delFlag 落 NULL -> 写进去的行**查不出来**：
#   countTableGeneral 返回 0，query_* 返回空列表，而且 insert 还"成功"返回行数。
#   这是本项目最容易踩的一个静默坑（步骤 3/5 的生产代码都记得传），
#   在这里显式写出来，免得后人以为"不传也行"。

def addPerson(code, name, birthday=None, avatar=None):
    sqliteCommon.insertManyTableGeneral(
        "pb_person", [{"personCode": code, "displayName": name,
                       "birthday": birthday, "avatarFaceCode": avatar,
                       "source": 0, "isConfirmed": 0}],
        conflictColumns=("personCode",), fillStandard=True)


def addPhoto(code, shotYear=2013):
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [{"photoCode": code, "relPath": "%s.jpg" % code,
                      "relPathHash": code.ljust(64, "0")[:64],
                      "fileHash": code.ljust(64, "1")[:64],
                      "fileSize": 1024, "shotYear": shotYear}],
        fillStandard=True)


def addFace(code, photoCode, vec, bucket="2000-2002", person=None,
            confirmed=0):
    sqliteCommon.insertManyTableGeneral(
        "pb_face", [{"faceCode": code, "photoCode": photoCode,
                     "personCode": person, "isConfirmed": confirmed,
                     "shotBucket": bucket, "detScore": 0.9, "quality": 0.8,
                     "embedding": faceEngine.encodeEmbedding(vec)}],
        fillStandard=True)


class Api(object):
    """建数据 + 读回查的小工具。

    ⚠️ 辅助函数刻意**放在模块级**再挂成 staticmethod，而不是在 fixture 里定义闭包、
       再在类体里引用。Python 的一个老坑：**类体不参与闭包** ——
       类体里写 `addPerson = staticmethod(addPerson)` 会去全局找 addPerson，
       于是 NameError；报错行在类定义处，与真正原因（作用域）毫无关系，
       排查时极易误判成"函数定义顺序不对"。
    """
    addPerson = staticmethod(addPerson)
    addPhoto = staticmethod(addPhoto)
    addFace = staticmethod(addFace)

    @staticmethod
    def centroidOf(personCode, bucketKey):
        return centroid.centroidOf(personCode, bucketKey)

    @staticmethod
    def centroidRow(personCode, bucketKey):
        rows = sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode=personCode, bucketKey=bucketKey)
        return rows[0] if rows else None

    @staticmethod
    def faceOf_(faceCode):
        return sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)[0]

    @staticmethod
    def links(personCode=None):
        return sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                  personCode=personCode)


@pytest.fixture
def lib(temp_db):
    """临时库（8 张表已建）+ 上面那套建数据工具。返回 (dbFile, Api)。"""
    sqliteCommon.dbHandle(temp_db)
    return temp_db, Api


@pytest.fixture
def twoPersons(lib):
    """P01 有 5 张脸（桶 2000-2002），P02 有 4 张脸（桶 2003-2005）"""
    _db, api = lib
    api.addPerson("P01", "爸爸", "1985-03-07")
    api.addPerson("P02", "妈妈", "1987-06-01")
    for i in range(5):
        photo = "PH_A%d" % i
        api.addPhoto(photo)
        api.addFace("fc_A%d" % i, photo, unit(100 + i), "2000-2002", "P01", 1)
    for i in range(4):
        photo = "PH_B%d" % i
        api.addPhoto(photo)
        api.addFace("fc_B%d" % i, photo, unit(200 + i), "2003-2005", "P02", 1)
    return lib


# ============================================================
# 二、质心：归一化均值 / 幂等 / 样本数门禁（验收第 3、7 条）
# ============================================================

class TestCentroidStore:
    def test_recompute_writes_normalized_mean(self, twoPersons):
        centroid.recompute("P01", "2000-2002")
        vec = twoPersons[1].centroidOf("P01", "2000-2002")
        assert vec is not None
        assert float(np.linalg.norm(vec)) == pytest.approx(1.0, abs=1e-5)
        assert vec.size == basicSettings.EMBEDDING_DIM

    def test_sample_count_is_recorded(self, twoPersons):
        info = centroid.recompute("P01", "2000-2002")
        assert info["sampleCount"] == 5
        assert info["enabled"] is True
        assert twoPersons[1].centroidRow("P01", "2000-2002")["sampleCount"] == 5

    def test_upsert_is_idempotent(self, twoPersons):
        """重算两遍不能产生第二行（(personCode,bucketKey) 上有 UNIQUE）"""
        centroid.recompute("P01", "2000-2002")
        first = twoPersons[1].centroidOf("P01", "2000-2002").copy()
        centroid.recompute("P01", "2000-2002")
        centroid.recompute("P01", "2000-2002")
        rows = sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode="P01", bucketKey="2000-2002")
        assert len(rows) == 1
        assert np.allclose(twoPersons[1].centroidOf("P01", "2000-2002"), first,
                           atol=1e-6), "重算结果必须逐位可复现"

    def test_below_threshold_is_not_enabled(self, lib):
        """**验收第 3 条**：sampleCount < 3 的桶不参与匹配。

        这里从两头验：写库侧 centroid 必须是 NULL；加载侧这一行根本不该进索引。
        """
        _db, api = lib
        api.addPerson("P01", "样本不足")
        api.addPhoto("PH_X0")
        for i in range(2):
            api.addFace("fc_X%d" % i, "PH_X0", unit(300 + i), "2000-2002", "P01", 1)
        info = centroid.recompute("P01", "2000-2002")
        assert info["sampleCount"] == 2
        assert info["enabled"] is False
        row = api.centroidRow("P01", "2000-2002")
        assert row is not None and row["sampleCount"] == 2
        assert row["centroid"] is None, "样本不足却写了质心 -> 会拿 1 个样本当原型"
        assert centroid.centroidOf("P01", "2000-2002") is None
        _matrix, index = centroid.loadAllCentroids()
        assert index.rowOf("P01", "2000-2002") == -1, "样本不足的桶进了索引"
        assert len(index) == 0

    def test_crossing_threshold_enables_the_bucket(self, lib):
        """补到第 3 张脸 -> 桶当场启用（不需要全量重跑）"""
        _db, api = lib
        api.addPerson("P01", "刚够三人")
        api.addPhoto("PH_Y0")
        for i in range(2):
            api.addFace("fc_Y%d" % i, "PH_Y0", unit(310 + i), "2000-2002", "P01", 1)
        assert centroid.recompute("P01", "2000-2002")["enabled"] is False
        api.addFace("fc_Y2", "PH_Y0", unit(312), "2000-2002", "P01", 1)
        assert centroid.recompute("P01", "2000-2002")["enabled"] is True
        assert api.centroidRow("P01", "2000-2002")["centroid"] is not None

    def test_skips_dirty_vectors_and_counts_them(self, lib):
        """脏向量（空/长度不对）被跳过且**计数上报**，不静默改变分母"""
        _db, api = lib
        api.addPerson("P01", "有脏数据")
        api.addPhoto("PH_Z0")
        for i in range(3):
            api.addFace("fc_Z%d" % i, "PH_Z0", unit(320 + i), "2000-2002", "P01", 1)
        sqliteCommon.insertManyTableGeneral(
            "pb_face", [{"faceCode": "fc_Zbad", "photoCode": "PH_Z0",
                         "personCode": "P01", "isConfirmed": 1,
                         "shotBucket": "2000-2002", "embedding": b"\x00" * 100}], fillStandard=True)
        info = centroid.recompute("P01", "2000-2002")
        assert info["sampleCount"] == 3
        assert info["skipped"]["badsize"] == 1

    def test_empty_bucket_key_writes_no_centroid(self, lib):
        """无拍摄年份的脸不参与跨桶比对 -> 不该有质心"""
        _db, api = lib
        api.addPerson("P01", "无年份")
        api.addPhoto("PH_W0", shotYear=None)
        for i in range(4):
            api.addFace("fc_W%d" % i, "PH_W0", unit(330 + i), None, "P01", 1)
        info = centroid.recompute("P01", "")
        assert info["enabled"] is False
        assert api.centroidRow("P01", "") is None

    def test_recompute_person_cleans_stale_buckets(self, lib):
        """样本搬走后的僵尸桶必须被清掉（否则它会继续参与匹配）"""
        _db, api = lib
        api.addPerson("P01", "会搬走的人")
        api.addPhoto("PH_V0")
        for i in range(3):
            api.addFace("fc_V%d" % i, "PH_V0", unit(340 + i), "2000-2002", "P01", 1)
        centroid.recompute("P01", "2000-2002")
        centroid.recompute("P01", "2003-2005")
        assert len(sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode="P01")) == 2
        assigner.unassign("fc_V0")               # 桶掉到 2 个样本
        centroid.recomputePerson("P01")
        keys = [r["bucketKey"] for r in sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode="P01")]
        assert "2000-2002" in keys and "2003-2005" not in keys
        assert api.centroidRow("P01", "2000-2002")["centroid"] is None

    def test_normalized_mean_ignores_garbage(self):
        good = unit(400)
        assert centroid.normalizedMean([]) is None
        assert centroid.normalizedMean(None) is None
        assert centroid.normalizedMean([None, b"\x00" * 10, np.zeros(3)]) is None
        out = centroid.normalizedMean([good, None, b"x" * 7, good])
        assert out is not None
        assert float(np.linalg.norm(out)) == pytest.approx(1.0, abs=1e-5)

    def test_load_all_centroids_is_sorted_and_indexed(self, twoPersons):
        centroid.recompute("P01", "2000-2002")
        centroid.recompute("P02", "2003-2005")
        matrix, index = centroid.loadAllCentroids()
        assert len(index) == 2
        assert matrix.shape == (2, basicSettings.EMBEDDING_DIM)
        assert matrix.dtype == np.float32
        # 排序键是 (personCode, bucketKey)：同一人的候选行必须连续（reduceat 的前提）
        assert [r.personCode for r in index] == ["P01", "P02"]
        assert index.rowOf("P01", "2000-2002") == 0
        assert index.displayName("P01") == "爸爸"
        sub = index.subset(["1997-1999", "2000-2002", "2003-2005"])
        assert sub.personCount == 2
        assert sub.starts.tolist() == [0, 1]

    def test_subset_covers_person_rows_contiguously(self, lib):
        """一个人横跨多个候选桶时，reduceat 的段边界必须正确"""
        _db, api = lib
        api.addPerson("P01", "一人三桶")
        base = unit(500)
        for i, key in enumerate(("1997-1999", "2000-2002", "2003-2005")):
            api.addPhoto("PH_M%d" % i)
            for j in range(3):
                api.addFace("fc_M%d_%d" % (i, j), "PH_M%d" % i,
                            base if j == 0 else unit(510 + i * 3 + j),
                            key, "P01", 1)
            centroid.recompute("P01", key)
        _m, index = centroid.loadAllCentroids()
        sub = index.subset(["1997-1999", "2000-2002", "2003-2005"])
        assert len(sub) == 3 and sub.personCount == 1
        assert sub.rowsOf(0) == (0, 3)
        scores = sub.scoreAll(base.reshape(1, -1))
        assert scores.shape == (1, 1)
        # 逐桶算一遍，验证 reduceat 出来的就是「三桶里的最大值」
        each = [float(sub.matrix[r] @ base) for r in range(len(sub))]
        assert float(scores[0][0]) == pytest.approx(max(each), abs=1e-5), \
            "逐人分数必须是各桶分数的 max"
        # 量级校验：每桶 3 个样本（1 个 base + 2 个随机），归一化均值与 base 的余弦
        # 理论值 1/sqrt(3)≈0.577（随机向量并非严格正交，只能给区间）。
        # 要验的是「明显小于 1.0」—— 桶质心是**均值**，不是那一条样本。
        assert 0.45 < float(scores[0][0]) < 0.70, \
            "人对自己的桶质心应在 1/sqrt(3) 附近，而不是 1.0"

    def test_zero_norm_centroid_is_dropped_on_load(self, lib):
        """零向量必须被剔除：留着会让这个人静默变成"永不匹配\""""
        _db, api = lib
        api.addPerson("P01", "零向量")
        api.addPhoto("PH_0")
        for i in range(3):
            api.addFace("fc_0%d" % i, "PH_0", unit(600 + i), "2000-2002", "P01", 1)
        centroid.recompute("P01", "2000-2002")
        sqliteCommon.updateTableGeneral(
            "pb_person_centroid", "personCode = %s", ("P01",),
            {"centroid": b"\x00" * basicSettings.EMBEDDING_BYTES})
        _m, index = centroid.loadAllCentroids()
        assert len(index) == 0, "零向量进了索引"


# ============================================================
# 三、归属写入（验收第 7 条：assign 后质心立即更新）
# ============================================================

class TestAssign:
    def test_assign_writes_face_and_link(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_1", "PH_1", unit(700), "2000-2002")
        info = assigner.assign("fc_1", "P01", 1, 0.91)
        assert info["changed"] is True
        face = api.faceOf_("fc_1")
        assert face["personCode"] == "P01"
        assert int(face["isConfirmed"]) == 1
        links = api.links("P01")
        assert len(links) == 1
        assert links[0]["linkKey"] == "PH_1:P01"
        assert float(links[0]["confidence"]) == pytest.approx(0.91, abs=1e-4)
        assert int(links[0]["source"]) == 1

    def test_assign_is_idempotent(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_1", "PH_1", unit(701), "2000-2002")
        first = assigner.assign("fc_1", "P01", 1)
        second = assigner.assign("fc_1", "P01", 1)
        assert second["changed"] is False
        assert len(api.links("P01")) == 1
        assert len(sqliteCommon.query_pb_face("pb_face", faceCode="fc_1")) == 1
        assert first["linkKey"] == second["linkKey"]

    def test_assign_recomputes_centroid_immediately(self, lib):
        """**验收第 7 条**：assign 后该 person 的质心**立即**可见地更新"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        for i in range(2):
            api.addFace("fc_%d" % i, "PH_1", unit(710 + i), "2000-2002")
        assert api.centroidRow("P01", "2000-2002") is None, "还没重算过"
        info = assigner.assign("fc_0", "P01", 1)
        row = api.centroidRow("P01", "2000-2002")
        assert row is not None, "确认后没建质心行"
        assert row["sampleCount"] == 1 and row["centroid"] is None
        assigner.assign("fc_1", "P01", 1)
        row = api.centroidRow("P01", "2000-2002")
        assert row["sampleCount"] == 2
        assert info["centroids"], "assign 必须返回重算结果"

    def test_crossing_threshold_on_third_confirm(self, lib):
        """第 3 次确认让桶越过启用线 —— 那一桶必须**当场**建起来"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        for i in range(3):
            api.addFace("fc_%d" % i, "PH_1", unit(720 + i), "2000-2002")
        assigner.confirmPerson("P01", ["fc_0", "fc_1", "fc_2"])
        row = api.centroidRow("P01", "2000-2002")
        assert row["sampleCount"] == 3
        assert row["centroid"] is not None
        assert api.centroidOf("P01", "2000-2002") is not None

    def test_reassign_recomputes_both_persons(self, lib):
        """**纪律 ②**：改判要把旧人的质心也重算（漏了会"越用越不准"）"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPerson("P02", "妈妈")
        api.addPhoto("PH_1")
        for i in range(3):
            api.addFace("fc_%d" % i, "PH_1", unit(730 + i), "2000-2002")
        for i in range(3):
            api.addFace("fc_o%d" % i, "PH_1", unit(740 + i), "2000-2002")
        for code in ("fc_0", "fc_1", "fc_2"):
            assigner.assign(code, "P01", 1)
        for code in ("fc_o0", "fc_o1", "fc_o2"):
            assigner.assign(code, "P02", 1)
        assert api.centroidRow("P01", "2000-2002")["sampleCount"] == 3
        assert api.centroidRow("P02", "2000-2002")["sampleCount"] == 3
        assigner.assign("fc_0", "P02", 1)
        assert api.centroidRow("P01", "2000-2002")["sampleCount"] == 2, \
            "P01 的质心没跟着少一条样本"
        assert api.centroidRow("P02", "2000-2002")["sampleCount"] == 4
        assert api.centroidRow("P01", "2000-2002")["centroid"] is None, \
            "P01 掉到 2 个样本，质心应当失效"

    def test_manual_confirm_writes_no_fake_score(self, lib):
        """人工确认**不编造分数**：写个假分数会让统计把人工当成高置信自动匹配"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_1", "PH_1", unit(750), "2000-2002")
        assigner.assign("fc_1", "P01", 1)
        assert api.links("P01")[0]["confidence"] is None

    def test_auto_assign_writes_score(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_1", "PH_1", unit(751), "2000-2002")
        assigner.assign("fc_1", "P01", 0, 0.77)
        link = api.links("P01")[0]
        assert float(link["confidence"]) == pytest.approx(0.77, abs=1e-4)
        assert int(link["source"]) == 0

    def test_one_link_per_photo_and_person(self, lib):
        """一张合影里 3 张脸属于同一个人 -> **只有 1 行**关联"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        for i in range(3):
            api.addFace("fc_%d" % i, "PH_1", unit(760 + i), "2000-2002")
        assigner.confirmPerson("P01", ["fc_0", "fc_1", "fc_2"])
        links = api.links("P01")
        assert len(links) == 1
        assert links[0]["faceCode"] in ("fc_0", "fc_1", "fc_2")

    def test_link_kept_while_other_faces_remain(self, lib):
        """这张照片里还有别的脸属于他 -> 关联**不能删**"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_0", "PH_1", unit(770), "2000-2002")
        api.addFace("fc_1", "PH_1", unit(771), "2000-2002")
        assigner.assign("fc_0", "P01", 1)
        assigner.assign("fc_1", "P01", 1)
        assigner.unassign("fc_1")
        assert len(api.links("P01")) == 1, "还有一张脸属于他，关联被误删了"

    def test_unassign_drops_link_when_last_face_goes(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_0", "PH_1", unit(780), "2000-2002")
        assigner.assign("fc_0", "P01", 1)
        info = assigner.unassign("fc_0")
        assert info["droppedLinks"] == 1
        assert api.links("P01") == []
        assert api.faceOf_("fc_0")["personCode"] is None
        assert int(api.faceOf_("fc_0")["isConfirmed"]) == 0

    def test_unassign_on_unassigned_face_is_noop(self, lib):
        _db, api = lib
        api.addPhoto("PH_1")
        api.addFace("fc_0", "PH_1", unit(790), "2000-2002")
        info = assigner.unassign("fc_0")
        assert info["changed"] is False

    def test_confirm_person_batch(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        for i in range(4):
            api.addPhoto("PH_C%d" % i)
            api.addFace("fc_C%d" % i, "PH_C%d" % i, unit(800 + i), "2000-2002")
        out = assigner.confirmPerson("P01", ["fc_C0", "fc_C1", "fc_C2", "fc_C3"])
        assert out["assigned"] == 4 and not out["failed"]
        assert api.centroidRow("P01", "2000-2002")["sampleCount"] == 4
        assert len(api.links("P01")) == 4

    def test_confirm_person_reports_failures(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_0", "PH_1", unit(810), "2000-2002")
        out = assigner.confirmPerson("P01", ["fc_0", "fc_missing"])
        assert out["assigned"] == 1
        assert len(out["failed"]) == 1
        assert "fc_missing" in out["failed"][0]["faceCode"]

    def test_assign_unknown_face_or_person_raises(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_0", "PH_1", unit(820), "2000-2002")
        with pytest.raises(assigner.AssignerError):
            assigner.assign("fc_nope", "P01", 1)
        with pytest.raises(assigner.AssignerError):
            assigner.assign("fc_0", "P_nope", 1)

    def test_apply_auto_only_writes_auto(self, lib):
        """applyAuto 只写 auto；review/cluster **故意不落库**"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_1", "PH_1", unit(830), "2000-2002")
        face = unit(831)
        low, high = basicSettings.matchThresholds()
        results = [
            matcher.MatchResult("fc_1", "PH_1", "2000-2002", [], "auto",
                                "matched", high + 0.1, "P01", [], low, high),
            matcher.MatchResult("fc_1", "PH_1", "2000-2002", [], "review",
                                "matched", (low + high) / 2, "", [], low, high),
            matcher.MatchResult("fc_1", "PH_1", "2000-2002", [], "cluster",
                                "low_score", low - 0.2, "", [], low, high),
        ]
        stat = assigner.applyAuto(results)
        assert stat["auto"] == 1 and stat["written"] == 1
        assert stat["review"] == 1 and stat["cluster"] == 1
        assert len(api.links("P01")) == 1
        del face


# ============================================================
# 四、合并 / 拆分（验收第 8 条：无孤儿记录）
# ============================================================

class TestMerge:
    def test_merge_moves_faces_and_rebuilds_centroids(self, twoPersons):
        _db, api = twoPersons
        centroid.recompute("P01", "2000-2002")
        centroid.recompute("P02", "2003-2005")
        before = api.centroidOf("P01", "2000-2002").copy()
        out = merger.merge("P01", "P02")
        assert out["faces"] == ["fc_A%d" % i for i in range(5)]
        # 脸全归 P02
        for i in range(5):
            assert api.faceOf_("fc_A%d" % i)["personCode"] == "P02"
        # 质心删干净后按 P02 **现有的脸**重算
        assert api.centroidOf("P01", "2000-2002") is None
        assert api.centroidRow("P02", "2000-2002")["sampleCount"] == 5
        assert api.centroidRow("P02", "2003-2005")["sampleCount"] == 4
        del before

    def test_merge_links_no_orphan_and_no_duplicate(self, twoPersons):
        """**验收第 8 条**：合并后关联与脸一致，无孤儿、无重复"""
        _db, api = twoPersons
        for i in range(5):
            assigner.assign("fc_A%d" % i, "P01", 1)
        for i in range(4):
            assigner.assign("fc_B%d" % i, "P02", 1)
        assert len(api.links("P01")) == 5
        merger.merge("P01", "P02")
        assert api.links("P01") == [], "P01 留下了孤儿关联"
        assert len(api.links("P02")) == 4 + 5
        report = assigner.verifyLinks()
        assert report["clean"], report

    def test_merge_same_photo_collapses_to_one_link(self, twoPersons):
        """两人在**同一张照片**里都被认过 -> 收敛成 1 行（linkKey 会撞 UNIQUE）"""
        _db, api = twoPersons
        api.addPhoto("PH_SAME")
        api.addFace("fc_S1", "PH_SAME", unit(850), "2000-2002")
        api.addFace("fc_S2", "PH_SAME", unit(851), "2003-2005")
        assigner.assign("fc_S1", "P01", 1)
        assigner.assign("fc_S2", "P02", 1)
        assigner.syncLinks()
        out = merger.merge("P01", "P02")
        assert "PH_SAME:P01" in out["linksDropped"]
        rows = sqliteCommon.query_pb_photo_person("pb_photo_person")
        assert [r["linkKey"] for r in rows].count("PH_SAME:P02") == 1
        assert assigner.verifyLinks()["clean"], assigner.verifyLinks()

    def test_merge_moves_centroid_without_unique_violation(self, twoPersons):
        """两人在**同一个桶**上都有质心 -> 硬搬会撞 (personCode,bucketKey) UNIQUE"""
        _db, api = twoPersons
        for i in range(3):
            api.addFace("fc_C%d" % i, "PH_A0", unit(860 + i), "2003-2005", "P02", 1)
        centroid.recompute("P01", "2000-2002")
        centroid.recompute("P02", "2003-2005")
        assert len(api.centroidRow("P02", "2003-2005")["centroid"]) == \
            basicSettings.EMBEDDING_BYTES
        merger.merge("P01", "P02")           # 不应抛 UNIQUE 异常
        rows = sqliteCommon.query_pb_person_centroid("pb_person_centroid")
        assert all(r["personCode"] == "P02" for r in rows)
        assert len([r for r in rows if r["bucketKey"] == "2003-2005"]) == 1

    def test_merge_moves_avatar_and_soft_deletes_source(self, twoPersons):
        _db, api = twoPersons
        sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", ("P01",),
                                        {"avatarFaceCode": "fc_A0"})
        out = merger.merge("P01", "P02")
        row = sqliteCommon.query_pb_person("pb_person", personCode="P02")[0]
        assert row["avatarFaceCode"] == "fc_A0", "P02 本来没头像，应继承 P01 的"
        old = sqliteCommon.query_pb_person("pb_person", personCode="P01",
                                           delFlag="*")[0]
        assert old["delFlag"] == "1", "P01 应被软删（保留审计与回退可能）"
        assert old["memo"], "软删要留痕"
        assert out["fromPerson"] == "P01"

    def test_merge_keeps_existing_avatar(self, twoPersons):
        _db, api = twoPersons
        sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", ("P01",),
                                        {"avatarFaceCode": "fc_A0"})
        sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", ("P02",),
                                        {"avatarFaceCode": "fc_B0"})
        merger.merge("P01", "P02")
        row = sqliteCommon.query_pb_person("pb_person", personCode="P02")[0]
        assert row["avatarFaceCode"] == "fc_B0", "用户自己挑的头像不该被覆盖"

    def test_merge_moves_categories(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPerson("P02", "妈妈")
        for code in ("P01", "P02"):
            sqliteCommon.insertManyTableGeneral(
                "pb_person_category", [{"personCode": code, "category": "家人"}], fillStandard=True)
        sqliteCommon.insertManyTableGeneral(
            "pb_person_category", [{"personCode": "P01", "category": "同事"}], fillStandard=True)
        out = merger.merge("P01", "P02")
        # sorted() 走 Unicode 码位序（同 U+540C < 家 U+5BB6），不是拼音序
        assert sorted(out["categories"]) == sorted(["家人", "同事"])
        rows = sqliteCommon.query_pb_person_category("pb_person_category",
                                                     category="同事")
        assert len(rows) == 1 and rows[0]["personCode"] == "P02", "分类成了孤儿"
        # 同名分类要去重，不能出现两行 "P02/家人"
        dup = sqliteCommon.query_pb_person_category("pb_person_category",
                                                    personCode="P02",
                                                    category="家人")
        assert len(dup) == 1

    def test_merge_keeps_user_memo(self, twoPersons):
        _db, api = twoPersons
        sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", ("P01",),
                                        {"memo": "我写的备注"})
        merger.merge("P01", "P02")
        old = sqliteCommon.query_pb_person("pb_person", personCode="P01",
                                           delFlag="*")[0]
        assert old["memo"] == "我写的备注", "不该覆盖用户自己写的备注"

    def test_merge_soft_delete_can_be_skipped(self, twoPersons):
        twoPersons[0]
        merger.merge("P01", "P02", softDelete=False)
        old = sqliteCommon.query_pb_person("pb_person", personCode="P01")[0]
        assert old["delFlag"] == "0"

    def test_merge_refuses_self_and_missing(self, twoPersons):
        twoPersons[0]
        with pytest.raises(merger.MergeError):
            merger.merge("P01", "P01")
        with pytest.raises(merger.MergeError):
            merger.merge("P01", "P_nope")
        with pytest.raises(merger.MergeError):
            merger.merge("", "P01")

    def test_merged_source_is_excluded_from_later_matching(self, lib):
        """软删的人不该再参与匹配（否则同一张脸会被算两次归属）"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPerson("P02", "妈妈")
        api.addPhoto("PH_1")
        for i in range(3):
            api.addFace("fc_%d" % i, "PH_1", unit(870 + i), "2000-2002", "P01", 1)
        centroid.recomputePerson("P01")
        merger.merge("P01", "P02")
        centroid.recomputePerson("P02")
        _m, index = centroid.loadAllCentroids()
        assert index.rowOf("P01", "2000-2002") == -1
        assert index.rowOf("P02", "2000-2002") == 0


class TestSplit:
    def test_split_to_new_person_creates_record(self, twoPersons):
        _db, api = twoPersons
        assigner.syncLinks()          # 夹具直接插的行，关联要补齐
        out = merger.split("fc_A0", "P03", "小王")
        assert out["created"] == "P03"
        face = api.faceOf_("fc_A0")
        assert face["personCode"] == "P03"
        person = sqliteCommon.query_pb_person("pb_person", personCode="P03")[0]
        assert person["displayName"] == "小王"
        assert person["avatarFaceCode"] == "fc_A0"
        assert api.links("P03")[0]["linkKey"] == "PH_A0:P03"
        assert len(api.links("P01")) == 4, "PH_A0 里还剩 4 张脸属于 P01，关联要留"

    def test_split_without_person_unassigns(self, twoPersons):
        _db, api = twoPersons
        centroid.recompute("P01", "2000-2002")
        out = merger.split("fc_A0")
        assert api.faceOf_("fc_A0")["personCode"] is None
        assert out["droppedLinks"] == 0, "这张照片里还有 4 张脸属于 P01"
        assert api.centroidRow("P01", "2000-2002")["sampleCount"] == 4

    def test_split_last_face_drops_link(self, lib):
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        api.addFace("fc_0", "PH_1", unit(880), "2000-2002")
        assigner.assign("fc_0", "P01", 1)
        out = merger.split("fc_0")
        assert out["droppedLinks"] == 1
        assert api.links("P01") == []
        assert assigner.verifyLinks()["clean"]

    def test_split_default_name(self, lib):
        _db, api = lib
        api.addPhoto("PH_1")
        api.addFace("fc_deadbeef1234", "PH_1", unit(890), "2000-2002")
        merger.split("fc_deadbeef1234", "P04")
        person = sqliteCommon.query_pb_person("pb_person", personCode="P04")[0]
        assert person["displayName"] == "未命名-fc_deadbe"[:9] or \
            person["displayName"].startswith("未命名-")

    def test_split_keeps_centroids_consistent(self, twoPersons):
        """拆分后**双方的**质心都要重算（旧人少一条、新人只有 1 条 -> 都不启用）"""
        _db, api = twoPersons
        for i in range(3):
            api.addFace("fc_D%d" % i, "PH_A0", unit(900 + i), "2000-2002", "P01", 1)
        centroid.recomputePerson("P01")
        assert api.centroidRow("P01", "2000-2002")["sampleCount"] >= 3
        merger.split("fc_A0", "P05", "新的人")
        assert api.centroidOf("P01", "2000-2002") is not None, "P01 仍够样本"
        assert api.centroidOf("P05", "2000-2002") is None, \
            "新人只有 1 张脸，质心不该启用"

    def test_split_rejects_unknown_face(self, twoPersons):
        twoPersons[0]
        with pytest.raises(merger.MergeError):
            merger.split("fc_nope", "P06")
        with pytest.raises(merger.MergeError):
            merger.split("", "P06")

    def test_split_then_verify_links_clean(self, twoPersons):
        _db, api = twoPersons
        for i in range(5):
            assigner.assign("fc_A%d" % i, "P01", 1)
        assigner.syncLinks()
        merger.split("fc_A0", "P07", "其实是他哥哥")
        merger.split("fc_A1", "P08", "其实是他姐姐")
        merger.merge("P07", "P08")
        report = assigner.verifyLinks()
        assert report["clean"], report


# ============================================================
# 五、一致性核对与同步
# ============================================================

class TestLinkConsistency:
    def test_verify_reports_clean_after_normal_flow(self, twoPersons):
        _db, api = twoPersons
        for i in range(5):
            assigner.assign("fc_A%d" % i, "P01", 1)
        # P02 的 4 张脸是夹具**直接插进行里**的（没走 assign），
        # 所以关联行还不存在 —— 这正是 syncLinks 的用途：按 pb_face 补齐
        assigner.syncLinks()
        report = assigner.verifyLinks()
        assert report["clean"], report
        assert report["checked"] == 9

    def test_verify_detects_orphan_link(self, twoPersons):
        """手工塞一条幽灵关联（人已不在照片里）-> 必须报出来"""
        _db, api = twoPersons
        for i in range(5):
            assigner.assign("fc_A%d" % i, "P01", 1)
        assigner.syncLinks()
        sqliteCommon.insertManyTableGeneral(
            "pb_photo_person", [{"linkKey": "PH_B0:P01", "photoCode": "PH_B0",
                                 "personCode": "P01", "source": 1}], fillStandard=True)
        report = assigner.verifyLinks()
        assert not report["clean"]
        assert "PH_B0:P01" in report["orphanLink"]

    def test_verify_detects_missing_link(self, twoPersons):
        _db, api = twoPersons
        assigner.assign("fc_A0", "P01", 1)
        assigner.syncLinks()
        sqliteCommon.deleteTableGeneral("pb_photo_person", "linkKey = %s",
                                        ("PH_A0:P01",))
        report = assigner.verifyLinks()
        assert "PH_A0:P01" in report["missingLink"]

    def test_verify_detects_dangling_person(self, twoPersons):
        _db, api = twoPersons
        assigner.assign("fc_A0", "P01", 1)
        assigner.syncLinks()
        sqliteCommon.deleteTableGeneral("pb_person", "personCode = %s", ("P01",))
        report = assigner.verifyLinks()
        assert report["dangling"]

    def test_sync_links_repairs_and_is_idempotent(self, twoPersons):
        _db, api = twoPersons
        for i in range(5):
            assigner.assign("fc_A%d" % i, "P01", 1)
        assigner.syncLinks()
        sqliteCommon.deleteTableGeneral("pb_photo_person", "linkKey = %s",
                                        ("PH_A0:P01",))
        sqliteCommon.insertManyTableGeneral(
            "pb_photo_person", [{"linkKey": "PH_A0:P99", "photoCode": "PH_A0",
                                 "personCode": "P01", "source": 1}], fillStandard=True)
        dry = assigner.syncLinks(dryRun=True)
        assert dry["dryRun"] and dry["drop"] == 1 and dry["add"] == 1
        stat = assigner.syncLinks()
        assert stat["add"] == 1 and stat["drop"] == 1
        assert assigner.verifyLinks()["clean"]
        again = assigner.syncLinks()
        assert again["add"] == 0 and again["drop"] == 0, "同步必须幂等"

    def test_sync_links_keeps_one_row_per_photo_person(self, twoPersons):
        _db, api = twoPersons
        api.addPhoto("PH_MULTI")
        for i in range(3):
            api.addFace("fc_M%d" % i, "PH_MULTI", unit(920 + i), "2000-2002",
                        "P01", 1)
        assigner.syncLinks()
        rows = sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                  linkKey="PH_MULTI:P01")
        assert len(rows) == 1


# ============================================================
# 六、端到端：确认 -> 匹配 -> 自动归属
# ============================================================

class TestEndToEnd:
    def test_confirm_then_match_then_auto(self, lib):
        """走通完整闭环：确认 3 张 -> 建质心 -> 新脸自动归属 -> 关联与质心同步"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        base = unit(1000)
        for i in range(3):
            api.addFace("fc_seed%d" % i, "PH_1", base, "2000-2002")
        assigner.confirmPerson("P01", ["fc_seed0", "fc_seed1", "fc_seed2"])

        # 新照片里的同一张脸（带一点噪声）
        api.addPhoto("PH_2")
        newVec = faceEngine.l2normalize(base + 0.02 * unit(1001))
        api.addFace("fc_new", "PH_2", newVec, "2000-2002")
        _m, index = centroid.loadAllCentroids()
        rows = sqliteCommon.query_pb_face("pb_face", faceCode="fc_new")
        got = matcher.match(rows[0], index=index)
        assert got.decision == matcher.DECISION_AUTO
        assert got.personCode == "P01"
        assert got.score > basicSettings.matchThresholds()[1]
        assert got.topCandidates[0].bucketKey == "2000-2002"

        stat = assigner.applyAuto([got])
        assert stat["written"] == 1
        assert api.faceOf_("fc_new")["personCode"] == "P01"
        # PH_1（3 张确认过的）与 PH_2（刚自动归属的）**各一行**，不能互相覆盖 ——
        # linkKey = photoCode:personCode，本来就不同
        keys = sorted(r["linkKey"] for r in api.links("P01"))
        assert keys == ["PH_1:P01", "PH_2:P01"]
        assert assigner.verifyLinks()["clean"]

    def test_unknown_face_goes_to_cluster(self, lib):
        """库里没有这个人 -> 分数低于 T_LOW -> 进聚类（不写 personCode）"""
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1")
        for i in range(3):
            api.addFace("fc_s%d" % i, "PH_1", unit(1100 + i), "2000-2002")
        assigner.confirmPerson("P01", ["fc_s0", "fc_s1", "fc_s2"])
        api.addPhoto("PH_3")
        api.addFace("fc_stranger", "PH_3", unit(1200), "2000-2002")
        _m, index = centroid.loadAllCentroids()
        got = matcher.match(sqliteCommon.query_pb_face("pb_face",
                                                       faceCode="fc_stranger")[0],
                            index=index)
        assert got.decision == matcher.DECISION_CLUSTER
        assert got.reason == matcher.REASON_LOW_SCORE
        assert got.personCode == ""
        assigner.applyAuto([got])
        assert api.faceOf_("fc_stranger")["personCode"] is None

    def test_screenshot_face_matches_via_all_bucket(self, lib):
        """**验收第 10 条**（DR-16③）：截图（shotYear NULL）现在靠 ALL 兜底桶匹配。

        修正前这张脸**连一个分数都拿不到**（候选桶 = [] -> 直接 no_bucket），
        于是「库里明明有他，这张截图却认不出来」。现在它的候选桶是 {ALL}。
        """
        _db, api = lib
        api.addPerson("P01", "爸爸")
        api.addPhoto("PH_1", shotYear=2013)
        base = unit(1300)
        for i in range(3):
            api.addFace("fc_s%d" % i, "PH_1", base, "2000-2002")
        assigner.confirmPerson("P01", ["fc_s0", "fc_s1", "fc_s2"])
        assert api.centroidOf("P01", centroid.ALL_BUCKET) is not None, \
            "确认 3 张脸后必须有 ALL 兜底桶"
        api.addPhoto("PH_shot", shotYear=None)
        api.addFace("fc_shot", "PH_shot", base, None)
        _m, index = centroid.loadAllCentroids()
        got = matcher.match(sqliteCommon.query_pb_face("pb_face",
                                                       faceCode="fc_shot")[0],
                            index=index)
        assert got.candidateBuckets == [centroid.ALL_BUCKET]
        assert got.decision == matcher.DECISION_AUTO
        assert got.personCode == "P01"
        # 落库后 isConfirmed 必须是 0（自动归属），否则「我不同意」列表里没有它
        stat = assigner.applyAuto([got])
        assert stat["written"] == 1
        assert int(api.faceOf_("fc_shot")["isConfirmed"]) == 0

    def test_two_runs_identical_on_real_store(self, twoPersons):
        """**验收第 4 条**（真库口径）：同一批脸跑两次判定完全一致"""
        _db, api = twoPersons
        for code in ("P01", "P02"):
            centroid.recomputePerson(code)
        _m, index = centroid.loadAllCentroids()
        faces = sqliteCommon.query_pb_face("pb_face")
        first = [r.toDict() for r in matcher.matchMany(faces, index=index)]
        second = [r.toDict() for r in matcher.matchMany(faces, index=index)]
        assert first == second
        assert len(first) == 9

    def test_threshold_switch_on_real_store(self, twoPersons):
        """**验收第 5 条**（真库口径）：保守 -> 激进 -> 保守，可回退且变化方向正确

        断言的是**集合包含关系**而不是计数：
          auto(保守) ⊆ auto(激进)      —— 保守自动认的，激进一定也自动认
          cluster(保守) ⊆ cluster(激进) —— 保守判陌生的，激进一定也判陌生
        为什么不用计数：「保守的待确认数 >= 激进的」这种话是**错的** ——
        两档同时上移时，一个 0.35 分的脸会从「待确认」变成「进聚类」，
        待确认总数完全可能反而变少。计数关系随数据分布变，只有包含关系是恒真的。
        """
        _db, api = twoPersons
        for code in ("P01", "P02"):
            centroid.recomputePerson(code)
        _m, index = centroid.loadAllCentroids()
        faces = sqliteCommon.query_pb_face("pb_face")
        cons = [r.decision for r in matcher.matchMany(faces, index=index,
                                                      preset="conservative")]
        aggr = [r.decision for r in matcher.matchMany(faces, index=index,
                                                      preset="aggressive")]
        back = [r.decision for r in matcher.matchMany(faces, index=index,
                                                      preset="conservative")]
        assert cons == back, "切回保守档结果不一致"
        autoC = set(i for i, d in enumerate(cons) if d == "auto")
        autoA = set(i for i, d in enumerate(aggr) if d == "auto")
        cluC = set(i for i, d in enumerate(cons) if d == "cluster")
        cluA = set(i for i, d in enumerate(aggr) if d == "cluster")
        assert autoC <= autoA, "保守档自动归属的，激进档必须也自动归属"
        # ⚠️ 方向容易搞反：保守档 T_LOW **更高**（0.42 > 0.30），
        #    所以保守档「判成陌生人」的**更多**。
        #    正确的包含关系是 cluster(激进) ⊆ cluster(保守)。
        assert cluA <= cluC, "激进档判陌生的，保守档必须也判陌生"


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行：python -m pytest code/src/test/test_review_ops.py -v")
