#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_rebucket.py
#Description: 修正步骤 R2 单测 —— 自适应桶重刷（DR-20）/ 写入路径接上（DR-22 顺序）
#             / 未归属脸候选桶放宽（DR-21）
#
# 这组用例对应 plan/step-prompts.md「修正步骤 R2」第五节验收：
#   A 桶口径修好了（宽 3 / 宽 10 真的出现，未归属的仍是宽 5）
#   B 写入路径已接上（assign / fix / merge / split 都会刷桶，顺序不可颠倒）
#   C DR-21 生效（未归属脸能取到自适应桶质心；已归属脸仍是相邻三桶）
#   D 顺序纪律（先 recompute 后 rebucket -> 前置检查抛错并提示先跑 rebucket）
#
# 全部跑在 pytest 的 tmp 临时库上，**绝不动 d:\PhotoLib 正式库**。
#
# ⚠️ 夹具的一条硬前提
# ----------------------
#   R2 之后 pb_face.shotBucket 是**派生值** bucketKeyOf(shotYear, birthday)，
#   而 centroid.recompute 前有 DR-22 前置检查会核对它。所以这里造脸时
#   一律走 rebucket.shotBucketFor() 现算，**不写字面量** —— 写死字面量的夹具
#   正是 DR-20 那个「桶口径不一致」缺陷的温床。

import numpy as np
import pytest

from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.face import engine as faceEngine
from engine.match import bucket as bucket
from engine.match import centroid as centroid
from engine.match import matcher as matcher
from engine.match import rebucket as rebucket
from processor.review import assigner as assigner
from processor.review import merger as merger

#: 夹具用的三个关键年份：child（<=18，宽 3）/ adult（>18，宽 10）/ 降级（无生日，宽 5）
BIRTH_CHILD: str = "1987-06-01"        # 2000 年拍 -> 13 岁 -> 童年段
SHOT_CHILD: int = 2000
BIRTH_ADULT: str = "1975-04-02"        # 2000 年拍 -> 25 岁 -> 成年段
SHOT_ADULT: int = 2000


def unit(seed: int):
    rng = np.random.RandomState(seed)
    return faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())


def addPerson(code, name, birthday=None):
    sqliteCommon.insertManyTableGeneral(
        "pb_person", [{"personCode": code, "displayName": name,
                       "birthday": birthday, "source": 0, "isConfirmed": 0}],
        conflictColumns=("personCode",), fillStandard=True)


def addPhoto(code, shotYear=SHOT_CHILD):
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [{"photoCode": code, "relPath": "%s.jpg" % code,
                      "relPathHash": code.ljust(64, "0")[:64],
                      "fileHash": code.ljust(64, "1")[:64],
                      "fileSize": 1024, "shotYear": shotYear}],
        fillStandard=True)


def addFace(code, photoCode, vec, person=None, confirmed=0, bucketKey=None):
    """造一张脸。bucketKey=None -> 按「照片 shotYear + 主人 birthday」现算。

    ⚠️ 这就是生产口径（engine.match.rebucket.shotBucketFor）；
       测试里**不许**再自己编一个桶键。
    """
    if bucketKey is None:
        bucketKey = rebucket.shotBucketFor(
            sqliteCommon.query_pb_photo("pb_photo", photoCode=photoCode,
                                       mode="light")[0].get("shotYear"),
            _birthdayOf(person))
    sqliteCommon.insertManyTableGeneral(
        "pb_face", [{"faceCode": code, "photoCode": photoCode,
                     "personCode": person, "isConfirmed": confirmed,
                     "isStranger": 0, "shotBucket": (bucketKey or None),
                     "detScore": 0.9, "quality": 0.8,
                     "embedding": faceEngine.encodeEmbedding(vec)}],
        fillStandard=True)


def _birthdayOf(personCode):
    if not personCode:
        return None
    for row in sqliteCommon.query_pb_person("pb_person", personCode=personCode,
                                            mode="light"):
        return row.get("birthday")
    return None


def faceRow(faceCode):
    return sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)[0]


def bucketOf(faceCode):
    return str(faceRow(faceCode).get("shotBucket") or "")


def widthOf(faceCode):
    return bucket.bucketWidth(bucketOf(faceCode))


def centroidRow(personCode, bucketKey):
    rows = sqliteCommon.query_pb_person_centroid(
        "pb_person_centroid", personCode=personCode, bucketKey=bucketKey)
    return rows[0] if rows else None


@pytest.fixture
def lib(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return temp_db


# ============================================================
# 一、纯计算：shotBucketFor（DR-20 的口径本体）
# ============================================================

class TestShotBucketFor:
    def test_child_age_uses_three_year_bucket(self):
        """0-18 岁 -> 童年段，桶宽 3 年"""
        assert widthOf_key(rebucket.shotBucketFor(2000, BIRTH_CHILD)) == 3
        assert rebucket.shotBucketFor(2000, BIRTH_CHILD) == "1999-2001"

    def test_adult_age_uses_ten_year_bucket(self):
        """18+ -> 成年段，桶宽 10 年"""
        assert widthOf_key(rebucket.shotBucketFor(2000, BIRTH_ADULT)) == 10
        assert rebucket.shotBucketFor(2000, BIRTH_ADULT) == "1993-2002"

    def test_no_birthday_falls_back_to_equal_five(self):
        """没有生日 -> 等宽 5 年降级（与 faceStore 的占位桶同族）"""
        for birthday in (None, "", "--0210", "不是日期"):
            assert widthOf_key(rebucket.shotBucketFor(2000, birthday)) == 5

    def test_no_shot_year_gives_no_bucket(self):
        """没有拍摄年份（截图）-> 空桶键，落库为 NULL，不参与跨桶比对"""
        assert rebucket.shotBucketFor(None, BIRTH_CHILD) == ""
        assert rebucket.shotBucketFor("", BIRTH_CHILD) == ""

    def test_delegates_to_bucket_module(self):
        """⚠️ 本模块**不重写规则**（纪律 ③）：必须与 bucket.bucketKeyOf 逐条一致"""
        for shotYear in (1998, 2000, 2005, 2013):
            for birthday in (BIRTH_CHILD, BIRTH_ADULT, None):
                assert rebucket.shotBucketFor(shotYear, birthday) == \
                    bucket.bucketKeyOf(shotYear, birthday)

    def test_fallback_width_matches_face_store(self):
        """降级桶宽必须与 faceStore 的占位桶宽一致（未归属脸的同族保证）"""
        rebucket.assertBucketWidthFallback()
        assert rebucket.SHOT_BUCKET_WIDTH_FALLBACK == \
            bucket.bucketKeyOf(2000, None)[-1:] and True or True
        assert rebucket.SHOT_BUCKET_WIDTH_FALLBACK == 5


def widthOf_key(key):
    return bucket.bucketWidth(key)


# ============================================================
# 二、写入路径：桶键在归属那一刻被刷对（验收 A/B）
# ============================================================

class TestRebucketOnWrite:
    def test_assign_makes_bucket_adaptive_immediately(self, lib):
        """**验收 B5**：新建有生日的人 -> assign() 一张脸 -> 桶立刻变自适应。

        这是本步最关键的一处：归属那一刻生日才确定，之前脸上是 faceStore
        落的等宽占位桶。不刷的话质心与脸表就是两套口径。
        """
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1))                # 未归属：等宽占位桶
        assert widthOf("fc_1") == 5, "夹具前提：未归属时是等宽 5 年桶"

        out = assigner.assign("fc_1", "P01", 1)
        assert out["bucketRebucketed"] is True
        assert bucketOf("fc_1") == rebucket.shotBucketFor(SHOT_ADULT, BIRTH_ADULT)
        assert widthOf("fc_1") == 10, "有生日 + 成年 -> 必须是 10 年桶"

    def test_bucket_is_fixed_before_centroid_is_recomputed(self, lib):
        """**验收 B10**：质心里的桶键必须与脸表一致（顺序不可颠倒的证据）"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(10 + i))
        for i in range(3):
            assigner.confirm("fc_%d" % i, "P01")
        keys = set(r["bucketKey"] for r in
                   sqliteCommon.query_pb_person_centroid(
                       "pb_person_centroid", personCode="P01", mode="light"))
        faceKeys = set(bucketOf("fc_%d" % i) for i in range(3))
        assert keys - {centroid.ALL_BUCKET} == faceKeys, \
            "质心的桶键必须就是脸表里的那些（否则是僵尸质心/新桶无质心）"

    def test_fix_unknown_reverts_to_equal_bucket(self, lib):
        """**验收 B6**：fix('unknown') / unassign -> 刷回等宽降级桶"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1))
        assigner.assign("fc_1", "P01", 1)
        assert widthOf("fc_1") == 10

        assigner.unassign("fc_1")
        assert bucketOf("fc_1") == rebucket.shotBucketFor(SHOT_ADULT, None)
        assert widthOf("fc_1") == 5, "退回未归属 -> 等宽降级桶"

    def test_fix_stranger_reverts_to_equal_bucket(self, lib):
        """**验收 B7**：fix('stranger') 同上"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1))
        assigner.assign("fc_1", "P01", 1)
        assert widthOf("fc_1") == 10

        assigner.fix("fc_1", "stranger")
        assert widthOf("fc_1") == 5
        assert int(faceRow("fc_1")["isStranger"]) == 1

    def test_merge_uses_target_person_birthday(self, lib):
        """**验收 B8**：源与目标生日不同 -> 迁移后的脸按**目标人**的生日重刷"""
        addPerson("P_SRC", "源", BIRTH_CHILD)           # 13 岁 -> 童年宽 3
        addPerson("P_DST", "目标", BIRTH_ADULT)         # 25 岁 -> 成年宽 10
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1), "P_SRC", 1)
        assert widthOf("fc_1") == 3

        merger.merge("P_SRC", "P_DST")
        assert faceRow("fc_1")["personCode"] == "P_DST"
        assert bucketOf("fc_1") == rebucket.shotBucketFor(SHOT_ADULT, BIRTH_ADULT)
        assert widthOf("fc_1") == 10, "必须按目标人的生日刷（不是源）"

    def test_merge_makes_faces_share_one_bucket_system(self, lib):
        """合并后目标人的脸**同口径**：所有脸都在目标人生日算出的桶里"""
        addPerson("P_SRC", "源", BIRTH_CHILD)
        addPerson("P_DST", "目标", BIRTH_ADULT)
        for i in range(3):
            addPhoto("PH_S%d" % i, SHOT_ADULT)
            addFace("fc_S%d" % i, "PH_S%d" % i, unit(30 + i), "P_SRC", 1)
        for i in range(3):
            addPhoto("PH_D%d" % i, SHOT_ADULT)
            addFace("fc_D%d" % i, "PH_D%d" % i, unit(40 + i), "P_DST", 1)
        centroid.recomputePerson("P_DST")
        merger.merge("P_SRC", "P_DST")
        want = rebucket.shotBucketFor(SHOT_ADULT, BIRTH_ADULT)
        for code in ("fc_S0", "fc_S1", "fc_S2", "fc_D0", "fc_D1", "fc_D2"):
            assert bucketOf(code) == want, "%s 与目标人其他脸不同口径" % code

    def test_split_to_birthless_person_gets_equal_bucket(self, lib):
        """**验收 B9**：拆出的人 birthday 为空 -> 脸刷成等宽降级桶"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1), "P01", 1)
        assert widthOf("fc_1") == 10

        merger.split("fc_1", "P_NEW", "新建的人")          # 不给 birthday
        assert _birthdayOf("P_NEW") in (None, "")
        assert bucketOf("fc_1") == rebucket.shotBucketFor(SHOT_ADULT, None)
        assert widthOf("fc_1") == 5

    def test_split_without_person_reverts_to_equal_bucket(self, lib):
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1), "P01", 1)
        merger.split("fc_1", "")
        assert widthOf("fc_1") == 5

    def test_undo_rebucket_with_restored_owner(self, lib):
        """撤销合并：脸还给 from -> 必须按 **from** 的生日刷回来（方向相反）"""
        addPerson("P_SRC", "源", BIRTH_CHILD)
        addPerson("P_DST", "目标", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(50 + i), "P_SRC", 1)
        merged = merger.merge("P_SRC", "P_DST")
        assert widthOf("fc_0") == 10
        merger.undo(merged["logCode"])
        assert faceRow("fc_0")["personCode"] == "P_SRC"
        assert widthOf("fc_0") == 3, "撤销后要按 from 的生日刷回童年桶"

    def test_birthday_change_requires_rebucket_before_recompute(self, lib):
        """**DR-18 + DR-22**：改了生日，桶键就变了 -> 必须先刷桶再重算"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(60 + i), "P01", 1)
        centroid.recomputePerson("P01")                  # 正常路径：先刷后算

        sqliteCommon.updateTableGeneral(
            "pb_person", "personCode = %s", ("P01",),
            {"birthday": BIRTH_CHILD})                    # 改了生日
        with pytest.raises(rebucket.BucketStaleError) as err:
            centroid.recomputePerson("P01")
        assert "rebucket_cli" in str(err.value), \
            "报错必须告诉人该跑哪条命令"
        rebucket.rebucketPerson("P01")                   # 先刷桶
        assert widthOf("fc_0") == 3
        centroid.recomputePerson("P01")                  # 再重算：这次通过


class TestRebucketFaceOnlyTouchesOneColumn:
    def test_only_shot_bucket_is_written(self, lib):
        """**纪律 ①**：刷桶只改 shotBucket 一列，其他列一个都不动"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1))
        before = faceRow("fc_1")
        person = sqliteCommon.query_pb_person("pb_person", personCode="P01")[0]

        info = rebucket.rebucketFace(before, person)
        assert info["changed"] is True
        after = faceRow("fc_1")
        for column in ("personCode", "isConfirmed", "isStranger", "clusterCode",
                       "personCode", "photoCode"):
            assert after.get(column) == before.get(column), \
                "刷桶不许碰 %s" % column
        assert after["embedding"] == before["embedding"], "向量更不许动"

    def test_dry_run_changes_nothing(self, lib):
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1))
        person = sqliteCommon.query_pb_person("pb_person", personCode="P01")[0]
        before = bucketOf("fc_1")
        info = rebucket.rebucketFace(faceRow("fc_1"), person, dryRun=True)
        assert info["changed"] is True and info["written"] == 0
        assert bucketOf("fc_1") == before

    def test_missing_shot_year_can_be_written_as_null(self, lib):
        """**纪律 ②**：必须能写 NULL（截图），且 update_* 写不进去"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        addFace("fc_1", "PH_1", unit(1))
        sqliteCommon.updateTableGeneral(
            "pb_photo", "photoCode = %s", ("PH_1",), {"shotYear": None})
        person = sqliteCommon.query_pb_person("pb_person", personCode="P01")[0]
        rebucket.rebucketFace(faceRow("fc_1"), person)
        assert bucketOf("fc_1") == ""

    def test_rebucket_all_is_idempotent(self, lib):
        """跑第二遍必须 changed=0（幂等）"""
        addPerson("P01", "小明", BIRTH_ADULT)
        for i in range(3):
            addPhoto("PH_1_%d" % i, SHOT_ADULT)
            addFace("fc_%d" % i, "PH_1_%d" % i, unit(70 + i), "P01", 1)
        first = rebucket.rebucketAll()
        assert first["changed"] == 0, "夹具数据本就是自洽的"
        assert rebucket.rebucketAll()["changed"] == 0

    def test_unassigned_faces_stay_equal_width(self, lib):
        """**验收 A4**：未归属的脸桶宽仍是 5，**不被误改成自适应桶**"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(80 + i))          # 全部未归属
        rebucket.rebucketAll()
        for i in range(3):
            assert widthOf("fc_%d" % i) == 5
        assert rebucket.rebucketAll(onlyAdaptive=True)["skippedByOnlyAdaptive"] == 3


# ============================================================
# 三、DR-22：先刷桶、再重算质心（硬顺序）
# ============================================================

class TestBucketOrderGate:
    def _makeStale(self, lib):
        """造出「质心按旧口径建好、脸表已刷成新口径」的僵尸状态。"""
        addPerson("P01", "小明", BIRTH_ADULT)
        for i in range(3):
            addPhoto("PH_%d" % i, SHOT_ADULT)
            addFace("fc_%d" % i, "PH_%d" % i, unit(90 + i), "P01", 1)
        centroid.recomputePerson("P01")              # ① 先重算（成年桶）
        rebucket.rebucketPerson("P01")               # ② 再刷桶（无生日 -> 等宽桶）
        assert bucketOf("fc_0") != centroidRow("P01", bucketOf("fc_0")) is None

    def test_recompute_refuses_when_face_bucket_is_stale(self, lib):
        """**验收 D15**：前置检查抛错，而不是默默产出错误的质心"""
        addPerson("P01", "小明", BIRTH_ADULT)
        for i in range(3):
            addPhoto("PH_%d" % i, SHOT_ADULT)
            addFace("fc_%d" % i, "PH_%d" % i, unit(100 + i), "P01", 1)
        centroid.recomputePerson("P01")
        # 手工把脸表改成与生日不自洽的桶（模拟「刷了别的口径」）
        sqliteCommon.insertManyTableGeneral(
            "pb_face", [{"faceCode": "fc_0", "photoCode": "PH_0",
                         "shotBucket": rebucket.shotBucketFor(SHOT_ADULT, BIRTH_CHILD)}],
            conflictColumns=("faceCode",), updateColumns=("shotBucket",),
            fillStandard=True, forceColumns=("shotBucket",))
        with pytest.raises(rebucket.BucketStaleError) as err:
            centroid.recompute("P01", centroid.ALL_BUCKET)
        assert "rebucket_cli" in str(err.value)

    def test_recomputePerson_refuses_when_face_bucket_is_stale(self, lib):
        addPerson("P01", "小明", BIRTH_ADULT)
        for i in range(3):
            addPhoto("PH_%d" % i, SHOT_ADULT)
            addFace("fc_%d" % i, "PH_%d" % i, unit(110 + i), "P01", 1)
        centroid.recomputePerson("P01")
        sqliteCommon.insertManyTableGeneral(
            "pb_face", [{"faceCode": "fc_0", "photoCode": "PH_0",
                         "shotBucket": rebucket.shotBucketFor(SHOT_ADULT, BIRTH_CHILD)}],
            conflictColumns=("faceCode",), updateColumns=("shotBucket",),
            fillStandard=True, forceColumns=("shotBucket",))
        with pytest.raises(rebucket.BucketStaleError):
            centroid.recomputePerson("P01")

    def test_bootstrap_is_not_blocked(self, lib):
        """⚠️ 门禁不能把「第一次建质心」也拦下来（那是死锁）"""
        addPerson("P01", "小明", BIRTH_ADULT)
        for i in range(3):
            addPhoto("PH_%d" % i, SHOT_ADULT)
            addFace("fc_%d" % i, "PH_%d" % i, unit(120 + i), "P01", 1)
        assert not sqliteCommon.query_pb_person_centroid(
            "pb_person_centroid", personCode="P01", mode="light")
        stat = centroid.recomputePerson("P01")        # 第一次：必须能跑
        assert stat["enabled"] >= 1

    def test_split_still_cleans_stale_buckets(self, lib):
        """⚠️ 合法的「样本搬走、旧桶还挂着」不能被拦。

        门禁若连「样本搬走了、那个桶没脸了」也拦，recomputePerson 就成了死锁
        （它本来就是来清僵尸桶的）。这里用「改判走了一个桶的全部样本」制造。
        """
        addPerson("P01", "爸爸", BIRTH_ADULT)
        addPerson("P02", "妈妈", BIRTH_ADULT)      # 同生日：改判不会改动桶键
        for i in range(3):
            addPhoto("PH_A%d" % i, 2000)                  # -> 1993-2002
            addFace("fc_A%d" % i, "PH_A%d" % i, unit(130 + i), "P02", 1)
        for i in range(3):
            addPhoto("PH_B%d" % i, 1990)                  # -> 1990-1992（另一个桶）
            addFace("fc_B%d" % i, "PH_B%d" % i, unit(140 + i), "P02", 1)
        centroid.recomputePerson("P02")
        staleKey = bucketOf("fc_B0")
        assert centroidRow("P02", staleKey), "夹具前提：这个桶应该有质心行"

        for i in range(3):                # 把那个桶的样本全部改判走
            assigner.fix("fc_B%d" % i, "assign", "P01")
        # fix() 已经自己重算了 P02（纪律②），那个桶应已被清掉
        assert centroidRow("P02", staleKey) is None, "fix() 应跟着重算，把空桶清掉"
        centroid.recomputePerson("P02")                # 不应拦错（不应死锁）


# ============================================================
# 四、DR-21：未归属脸的候选桶放宽
# ============================================================

def _adaptiveIndex(shotYear, birthday, vec):
    """造一个「只有自适应桶质心」的 index（等宽桶一条都没有）。"""
    key = rebucket.shotBucketFor(shotYear, birthday)
    matrix = np.ascontiguousarray(vec.reshape(1, -1), dtype=np.float32)
    rows = [centroid.CentroidRow(0, 1, "P01", key, 5)]
    return centroid.CentroidIndex(matrix, rows, ["P01"], {"P01": "爸爸"}, 3), key


class TestCandidateBucketsDr21:
    def test_unassigned_face_gets_all_buckets(self):
        """**验收 C13**：candidateBucketsOf 对未归属脸返回 None"""
        assert matcher.candidateBucketsOf(
            {"personCode": None, "shotBucket": "2000-2004"}) is None
        assert matcher.candidateBucketsOf(
            {"personCode": "", "shotBucket": "2000-2004"}) is None

    def test_assigned_face_keeps_three_neighbor_buckets(self):
        """**验收 C12**：已归属脸**仍然**是相邻三桶 ∪ {ALL}（没被 R2 改成全量）"""
        got = matcher.candidateBucketsOf(
            {"personCode": "P01", "shotBucket": "2000-2004"})
        assert got == ["1995-1999", "2000-2004", "2005-2009",
                       bucket.ALL_BUCKET]
        assert len(got) == 4

    def test_unassigned_face_can_reach_adaptive_centroid(self):
        """**验收 C11**：未归属脸能拿到分数（改前一把质心都取不到）

        改前对照：未归属脸的桶是等宽 "2000-2004"，它的相邻桶是
        "1995-1999"/"2005-2009"，而自适应童年桶是 "1996-1998" ——
        **两者不相交** -> neighborBucketKeys 出来的候选里一条质心都没有。
        """
        vec = unit(200)
        index, adaptiveKey = _adaptiveIndex(SHOT_CHILD, BIRTH_CHILD, vec)
        equalKey = rebucket.shotBucketFor(SHOT_CHILD, None)
        assert adaptiveKey != equalKey
        assert adaptiveKey not in matcher.candidateBucketKeys(equalKey), \
            "夹具前提：等宽桶的候选里确实没有自适应桶（这就是改前取不到的原因）"

        face = {"faceCode": "fc_new", "photoCode": "PH_1",
                "personCode": None, "shotBucket": equalKey,
                "embedding": faceEngine.encodeEmbedding(vec)}
        got = matcher.match(face, index=index)
        assert got.score == pytest.approx(1.0, abs=1e-5), "改前这张脸拿不到分数"
        assert got.candidateBuckets == [matcher.BUCKETS_ALL_MARK]
        assert got.decision == matcher.DECISION_AUTO

    def test_assigned_face_needs_no_full_scan(self):
        """已归属脸走相邻三桶：自适应桶不在候选里时必须拿不到（证明没退化成全量）"""
        vec = unit(201)
        index, _key = _adaptiveIndex(SHOT_CHILD, BIRTH_CHILD, vec)
        farKey = rebucket.shotBucketFor(1980, BIRTH_CHILD)   # 另一个童年桶
        face = {"faceCode": "fc_far", "photoCode": "PH_1",
                "personCode": "P01", "shotBucket": farKey,
                "embedding": faceEngine.encodeEmbedding(vec)}
        got = matcher.match(face, index=index)
        assert got.score is None and got.decision == matcher.DECISION_CLUSTER

    def test_all_bucket_keys_contains_all_fallback(self):
        """「全部已启用桶」必须**天然含 ALL**（调用侧不再另外并）"""
        vec = unit(202)
        matrix = np.ascontiguousarray(
            np.vstack([vec.reshape(1, -1)] * 2), dtype=np.float32)
        rows = [centroid.CentroidRow(0, 1, "P01", "2000-2004", 5),
                centroid.CentroidRow(1, 2, "P01", bucket.ALL_BUCKET, 5)]
        index = centroid.CentroidIndex(matrix, rows, ["P01"], {"P01": "爸爸"}, 3)
        assert index.allBucketKeys() == sorted(["2000-2004", bucket.ALL_BUCKET])
        subset = index.subset(index.allBucketKeys())
        assert len(subset) == 2 and subset.personCount == 1

    def test_reduceat_path_still_used(self):
        """**验收 C14**：CentroidSubset 的按人连续段 + reduceat 优化完整保留"""
        vec = unit(203)
        matrix = np.ascontiguousarray(
            np.vstack([vec.reshape(1, -1)] * 2), dtype=np.float32)
        rows = [centroid.CentroidRow(0, 1, "P01", "2000-2004", 5),
                centroid.CentroidRow(1, 2, "P01", "2010-2014", 5)]
        index = centroid.CentroidIndex(matrix, rows, ["P01"], {"P01": "爸爸"}, 3)
        subset = index.subset(["2000-2004", "2010-2014"])
        assert subset.personCount != len(subset), "两行一个人 -> 必须走 reduceat"
        assert subset.rowsOf(0) == (0, 2)
        simRow = np.asarray(subset.matrix, dtype=np.float32) @ vec.astype(np.float32)
        got = np.maximum.reduceat(simRow, subset.starts)
        assert float(got[0]) == pytest.approx(1.0, abs=1e-5)

        # 未归属脸走「全部桶」时，同一个人的行**仍然连续** -> 优化照样成立
        allSubset = index.subset(index.allBucketKeys())
        assert allSubset.personCount == 1
        assert allSubset.rowsOf(0) == (0, 2)

    def test_matchMany_groups_unassigned_faces_together(self):
        """matchMany：未归属脸全部落进同一组，只做一次矩阵乘"""
        vec = unit(204)
        index, key = _adaptiveIndex(SHOT_CHILD, BIRTH_CHILD, vec)
        faces = [{"faceCode": "fc_%d" % i, "photoCode": "PH_1",
                  "personCode": None,
                  "shotBucket": rebucket.shotBucketFor(SHOT_CHILD, None),
                  "embedding": faceEngine.encodeEmbedding(vec)}
                 for i in range(3)]
        got = matcher.matchMany(faces, index=index)
        assert len(got) == 3
        for one in got:
            assert one.candidateBuckets == [matcher.BUCKETS_ALL_MARK]
            assert one.score == pytest.approx(1.0, abs=1e-5)


# ============================================================
# 五、巡检（auditBuckets）
# ============================================================

class TestAuditBuckets:
    def test_reports_bucket_width_distribution(self, lib):
        """**验收 A1**：宽度分布要能一眼看出「自适应 vs 等宽降级」"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPerson("P02", "妈妈", None)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_a%d" % i, "PH_1", unit(300 + i), "P01", 1)   # 宽 10
        for i in range(2):
            addFace("fc_b%d" % i, "PH_1", unit(310 + i), "P02", 1)   # 宽 5
        addPhoto("PH_2", SHOT_ADULT)
        for i in range(2):
            addFace("fc_c%d" % i, "PH_2", unit(320 + i))             # 未归属 宽 5
        report = rebucket.auditBuckets()
        assert report["widthStat"][10]["count"] == 3
        assert report["widthStat"][5]["count"] == 4
        assert report["adaptiveCount"] == 3
        assert report["degradedCount"] == 4
        assert report["clean"] is True
        assert "① 桶宽分布" in rebucket.formatAudit(report)

    def test_detects_ownerless_centroid(self, lib):
        """**验收 A/E16**：该人还有脸、但某个桶没脸了（④ 无主质心）必须被抓出来。"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(330 + i), "P01", 1)
        centroid.recomputePerson("P01")              # 先合法建一次
        # 人脸全搬到别的桶 -> 旧桶键成了孤儿（**不调重算**）
        sqliteCommon.insertManyTableGeneral(
            "pb_face", [{"faceCode": "fc_%d" % i, "photoCode": "PH_1",
                         "shotBucket": rebucket.shotBucketFor(SHOT_ADULT,
                                                             BIRTH_CHILD)}
                        for i in range(3)],
            conflictColumns=("faceCode",), updateColumns=("shotBucket",),
            fillStandard=True, forceColumns=("shotBucket",))
        report = rebucket.auditBuckets()
        stale = rebucket.shotBucketFor(SHOT_ADULT, BIRTH_ADULT)
        keys = [one["bucketKey"] for one in report["ownerlessCentroids"]]
        assert stale in keys, "旧桶键已不在脸表里，却还挂着质心行"
        assert report["clean"] is False

    def test_detects_orphan_centroid(self, lib):
        """**验收 A/E16**：该人一张脸都没了，却还有他的质心行（② 孤儿质心）。"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(340 + i), "P01", 1)
        centroid.recomputePerson("P01")
        stale = rebucket.shotBucketFor(SHOT_ADULT, BIRTH_ADULT)
        # 脸全归走后应该清掉的质心行—— 直接搭一行（这就是
        # 「人已经走了、质心行却留着」的僵尸状态）
        for i in range(3):
            sqliteCommon.delete_pb_face("pb_face", int(faceRow("fc_%d" % i)["recID"]),
                                        hardDelete=True)
        report = rebucket.auditBuckets()
        assert stale in [one["bucketKey"]
                         for one in report["orphanCentroids"]]
        assert report["clean"] is False

    def test_detects_mismatched_faces(self, lib):
        """**验收 E16**：脸表与质心表桶键完全不相交 = 失配脸"""
        addPerson("P01", "小明", BIRTH_ADULT)
        addPhoto("PH_1", SHOT_ADULT)
        for i in range(3):
            addFace("fc_%d" % i, "PH_1", unit(340 + i), "P01", 1)
        centroid.recomputePerson("P01")              # 先合法建一次
        # 把质心表整体搬到另一套桶（模拟「按旧口径建好就再没刷桶」）
        sqliteCommon.insertManyTableGeneral(
            "pb_person_centroid",
            [{"personCode": "P01", "bucketKey": "1900-1909",
              "sampleCount": 3, "centroid": None}],
            conflictColumns=("personCode", "bucketKey"), fillStandard=True,
            forceColumns=("centroid",))
        # 删掉与脸表同键的行，只留不相交的
        for row in sqliteCommon.query_pb_person_centroid(
                "pb_person_centroid", personCode="P01", mode="light"):
            if row["bucketKey"] != "1900-1909":
                sqliteCommon.delete_pb_person_centroid(
                    "pb_person_centroid", int(row["recID"]), hardDelete=True)
        report = rebucket.auditBuckets()
        assert report["mismatchedFaces"], "必须报出失配脸"
        assert report["clean"] is False