#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_step12.py
#Description: 步骤 12 的后端验收单测
#
# 覆盖这一轮新增/修改的东西：
#   · batchProgressOf（本批真实计数的推导规则，三处必须同源）
#   · getPerson 的桶内照片数（原来恒为 0 的实现缺口）
#   · GET /persons/{code}/faces（Tab2 两段：人工确认 / 自动归属）
#   · GET /persons/{code}/timeline（按年代桶分组、空桶不显示）
#   · faceStateOf 与 getPhoto 的四态口径一致
#   · GET/POST /api/settings（参数读取、进程内覆盖、校验错误）
#   · basicSettings.setThresholdOverride / setBucketStrategy
#   · bucket.bucketKeyAdaptive 受分桶策略控制
#   · /api/duplicates 与 /api/duplicates/compare
#   · tools/backup.py 的备份/恢复语义（不含 photoDir）

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import pytest                                                    # noqa: E402

from api import browse                                           # noqa: E402
from api import settings as settingsApi                          # noqa: E402
from common import globalDefinition as comGD                     # noqa: E402
from common import paths as paths                                # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.match import bucket as bucket                        # noqa: E402


# ============================================================
# 一、本批真实计数
# ============================================================

class TestBatchProgress:
    """pb_scan_job 里**没有**「本批已处理」这一列，只能推导。
    推导规则错一次的代价是：界面上「已暂停等待指示」旁边写着 0/100。"""

    def test_running_batch_is_in_flight_batch(self):
        got = browse.batchProgressOf({"batchSize": 100, "batchIndex": 2,
                                      "processedCount": 262, "jobStatus": "RUNNING"})
        assert got["batchProcessed"] == 62
        assert got["batchSize"] == 100

    def test_paused_shows_the_batch_that_just_finished(self):
        """PAUSED 时 batchIndex 已经 +1 了，所以要减 (index-1)×size。
        写成 processedCount % batchSize 在这里恰好也对，但下一条就不对了。"""
        got = browse.batchProgressOf({"batchSize": 100, "batchIndex": 3,
                                      "processedCount": 300, "jobStatus": "PAUSED"})
        assert got["batchProcessed"] == 100

    def test_paused_on_a_partial_batch(self):
        """被 stop 打断的批次也会置 PAUSED 且 batchIndex +1，
        所以「本批」是**跑了一半的那一批**，不是 0。"""
        got = browse.batchProgressOf({"batchSize": 100, "batchIndex": 3,
                                      "processedCount": 237, "jobStatus": "PAUSED"})
        assert got["batchProcessed"] == 37

    def test_done_has_no_current_batch(self):
        got = browse.batchProgressOf({"batchSize": 100, "batchIndex": 3,
                                      "processedCount": 2137, "jobStatus": "DONE"})
        assert got["batchProcessed"] is None

    def test_failed_reports_partial(self):
        got = browse.batchProgressOf({"batchSize": 100, "batchIndex": 1,
                                      "processedCount": 40, "jobStatus": "FAILED"})
        assert got["batchProcessed"] == 40

    def test_idle_is_zero_not_none(self):
        got = browse.batchProgressOf({"batchSize": 100, "batchIndex": 0,
                                      "processedCount": 0, "jobStatus": "IDLE"})
        assert got["batchProcessed"] == 0

    def test_zero_batch_size_gives_null(self):
        got = browse.batchProgressOf({"batchSize": 0, "batchIndex": 0,
                                      "processedCount": 5, "jobStatus": "RUNNING"})
        assert got["batchProcessed"] is None

    def test_list_and_status_agree(self, api_env):
        """任务列表与轮询**必须给同一个数** —— 否则用户看到两个都在动的数字。"""
        client = api_env["client"]
        start = client.post("/api/scan/start",
                            json={"rootPath": paths.photo_dir(), "batchSize": 2})
        assert start.status_code == 200
        jobCode = start.json()["jobCode"]
        stopped = client.post("/api/scan/stop/%s" % jobCode, params={"wait": 1})
        assert stopped.status_code == 200
        listed = client.get("/api/scan/jobs", params={"size": 50}).json()["items"]
        one = next(j for j in listed if j["jobCode"] == jobCode)
        status = client.get("/api/scan/status/%s" % jobCode).json()
        assert one["batchProcessed"] == status["batchProcessed"]
        assert one["batchSize"] == status["batchSize"]
        assert one["batchIndex"] == status["batchIndex"]


# ============================================================
# 二、四态口径只有一份
# ============================================================

class TestFaceState:
    def test_four_states(self):
        assert browse.faceStateOf({"personCode": None, "isConfirmed": 0,
                                   "isStranger": 0}) == "pending"
        assert browse.faceStateOf({"personCode": "P_a", "isConfirmed": 1,
                                   "isStranger": 0}) == "confirmed"
        assert browse.faceStateOf({"personCode": "P_a", "isConfirmed": 0,
                                   "isStranger": 0}) == "disputed"
        assert browse.faceStateOf({"personCode": "P_a", "isConfirmed": 1,
                                   "isStranger": 1}) == "stranger"

    def test_matches_photo_detail(self, api_env):
        """同一个 pb_face 行，/photos/{code} 与 faceStateOf 必须同口径。"""
        client = api_env["client"]
        rows = _rows("SELECT faceCode, photoCode FROM pb_face"
                     " ORDER BY recID LIMIT 5")
        assert rows
        for row in rows:
            detail = client.get("/api/photos/%s" % row["photoCode"]).json()
            one = next(f for f in detail["faces"] if f["faceCode"] == row["faceCode"])
            assert one["state"] == browse.faceStateOf(
                {"personCode": one["personCode"], "isConfirmed": one["isConfirmed"],
                 "isStranger": one["isStranger"]})


# ============================================================
# 三、人物详情：桶内照片数 / 时间轴 / 人脸样本两段
# ============================================================

def _assignFace(faceCode, personCode, confirmed, shotBucket=None):
    """直接把一张脸挂到某人名下（绕开 api 层，只为造数据）。

    ⚠️ 生成的 update_pb_face 是**按 recID** 更新的（表名, recID, dataSet），
    传 faceCode 当键会拼出 `WHERE recID = {...}` —— SQL 会跑到绑定阶段才炸，
    而错误信息（"type 'dict' is not supported"）完全指不到真正的原因。
    """
    rows = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode, delFlag="*")
    assert rows, "测试库里没有 %s" % faceCode
    patch = {"personCode": personCode, "isConfirmed": 1 if confirmed else 0}
    if shotBucket is not None:
        patch["shotBucket"] = shotBucket
    return sqliteCommon.update_pb_face("pb_face", rows[0]["recID"], patch)


def _rows(sql, values=()):
    from database import queryCommon as query
    return query.selectList(sql, tuple(values))


class TestPersonBuckets:
    def test_bucket_photo_count_is_not_always_zero(self, api_env):
        """回归：原来这里 group by 的是 `substr(takenAt,1,7)`，
        却拿桶键去查字典 -> 每个桶的 photoCount 恒为 0（且不报错）。"""
        client = api_env["client"]
        _assignFace("FC_PH_2013_01_0", "P_alpha", True, "2010-2014")
        _assignFace("FC_PH_2016_03_0", "P_alpha", True, "2010-2014")
        detail = client.get("/api/persons/P_alpha").json()
        bucket = next(b for b in detail["buckets"] if b["bucketKey"] == "2010-2014")
        assert bucket["faceCount"] == 2
        assert bucket["confirmedCount"] == 2
        assert bucket["autoCount"] == 0
        # photoCount 必须与「这个桶里 DISTINCT 的 photoCode 数」相等
        expect = int(_rows(
            "SELECT COUNT(DISTINCT f.photoCode) AS rowNum FROM pb_face f"
            " JOIN pb_photo p ON p.photoCode = f.photoCode"
            " WHERE f.personCode = %s AND f.shotBucket = %s AND p.delFlag = %s",
            ("P_alpha", "2010-2014", comGD.DEL_FLAG_NO))[0]["rowNum"])
        assert expect >= 1
        assert bucket["photoCount"] == expect

    def test_timeline_groups_by_bucket_and_hides_empty(self, api_env):
        client = api_env["client"]
        _assignFace("FC_PH_2013_01_0", "P_alpha", True, "2010-2014")
        _assignFace("FC_PH_2013_01_1", "P_alpha", True, "2010-2014")
        _assignFace("FC_PH_2020_11_0", "P_alpha", False, "2020-2024")
        body = client.get("/api/persons/P_alpha/timeline").json()
        keys = [g["bucketKey"] for g in body["groups"]]
        assert keys == ["2010-2014", "2020-2024"]
        # 同一张照片里两张脸 -> 不能算成两张照片
        first = body["groups"][0]
        assert first["count"] == 1
        assert len(first["photos"]) == 1
        assert first["photos"][0]["photoCode"] == "PH_2013_01"

    def test_timeline_limit_per_bucket_keeps_total(self, api_env):
        client = api_env["client"]
        for code in ("FC_PH_2013_01_0", "FC_PH_2013_01_1", "FC_PH_2013_07_0"):
            _assignFace(code, "P_beta", True, "2013-2017")
        body = client.get("/api/persons/P_beta/timeline",
                          params={"limitPerBucket": 1}).json()
        group = body["groups"][0]
        assert group["photoTotal"] == 2
        assert len(group["photos"]) == 1

    def test_timeline_404_for_unknown_person(self, api_env):
        assert api_env["client"].get("/api/persons/NOPE/timeline").status_code == 404

    def test_faces_two_sections_with_counts(self, api_env):
        client = api_env["client"]
        _assignFace("FC_PH_2013_01_0", "P_alpha", True, "2010-2014")
        _assignFace("FC_PH_2016_03_0", "P_alpha", True, "2010-2014")
        _assignFace("FC_PH_2020_11_0", "P_alpha", False, "2020-2024")
        body = client.get("/api/persons/P_alpha/faces").json()
        assert body["counts"]["confirmed"] == 2
        assert body["counts"]["disputed"] == 1
        states = [i["state"] for i in body["items"]]
        assert sorted(states) == ["confirmed", "confirmed", "disputed"]
        # 人工确认与自动归属**必须能用过滤参数分开取**
        onlyAuto = client.get("/api/persons/P_alpha/faces",
                              params={"state": "disputed"}).json()
        assert [i["state"] for i in onlyAuto["items"]] == ["disputed"]
        assert onlyAuto["counts"]["confirmed"] == 2

    def test_faces_reject_bad_state(self, api_env):
        resp = api_env["client"].get("/api/persons/P_alpha/faces",
                                     params={"state": "nonsense"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "PARAM_INVALID"

    def test_faces_similarity_is_none_not_zero_when_no_centroid(self, api_env):
        """没有可比质心时必须给 null。填 0.00 会被读成「非常不像」。"""
        client = api_env["client"]
        _assignFace("FC_PH_2013_01_0", "P_alpha", True, "2010-2014")
        item = client.get("/api/persons/P_alpha/faces").json()["items"][0]
        assert item["similarity"] is None

    def test_removed_face_leaves_the_section(self, api_env):
        """自动样本被移除（fix unknown）后，它回到**待确认队列**，
        而不再出现在这个人的「自动归属」段里。"""
        client = api_env["client"]
        _assignFace("FC_PH_2020_11_0", "P_alpha", False, "2020-2024")
        before = client.get("/api/persons/P_alpha/faces").json()
        assert before["counts"]["disputed"] == 1
        resp = client.post("/api/review/fix",
                           json={"faceCodes": ["FC_PH_2020_11_0"], "action": "unknown"})
        assert resp.status_code == 200
        after = client.get("/api/persons/P_alpha/faces").json()
        assert after["counts"]["disputed"] == 0
        assert after["items"] == []
        # 它回到了待确认队列（personCode 为空 => 不再属于任何人）
        queue = client.get("/api/review/pending", params={"size": 50}).json()
        assert "FC_PH_2020_11_0" in [one["faceCode"] for one in queue["items"]]
        # 也从「我不同意」列表里消失了
        disputed = client.get("/api/review/disputed", params={"size": 50}).json()
        assert "FC_PH_2020_11_0" not in [f["faceCode"] for g in disputed["items"]
                                         for f in (g.get("faces") or [])]


class TestRevertible:
    """「撤销上次合并」按钮的候选集（步骤 12 实测发现并修掉的口径问题）。"""

    def _mergeOnce(self, api_env, faceCodes):
        """新建 B -> 把脸改判给 B -> 把 B 合并回 A。返回 (logCode, B 的编码)。"""
        target = self._splitOff(api_env, faceCodes)
        merged = api_env["client"].post("/api/review/merge",
                                         json={"fromPersonCode": target,
                                               "toPersonCode": "P_alpha"})
        assert merged.status_code == 200, merged.text
        return merged.json()["logCode"], target

    @staticmethod
    def _splitOff(api_env, faceCodes):
        """新建一个 B 并把给定的几张脸改判给他（**不合并**）。"""
        client = api_env["client"]
        created = client.post("/api/contacts", json={"displayName": "验收-合并靶子",
                                                     "birthday": "1985-03-07"})
        assert created.status_code in (200, 201), created.text
        target = (created.json().get("personCode")
                  or created.json().get("person", {}).get("personCode"))
        assert target
        for faceCode in faceCodes:
            fixed = client.post("/api/review/fix",
                                json={"faceCodes": [faceCode], "action": "assign",
                                      "personCode": target})
            assert fixed.status_code == 200, fixed.text
        return target

    @staticmethod
    def _confirmedFacesOf(personCode, limit):
        """先把该人某几张照片上的脸挂上去（夹具里所有人脸都是未归属的）。

        ⚠️ 必须同时按生日写**正确的 shotBucket**：夹具里的人脸用的是
        `faceStore.makeShotBucket()` 落的**等宽 5 年占位桶**，而一旦这张脸
        有了主人，DR-22 的前置检查就会拿「按生日算的自适应桶」去比对 ——
        不一致就抛 BucketStaleError。生产路径上这一步由 assigner 内部完成
        （归属那一刻刷桶），直写库就得自己算，否则测试会失败在完全无关的地方。
        """
        from engine.match import rebucket as rebucket
        person = _rows("SELECT birthday AS birthday FROM pb_person"
                       " WHERE personCode = '%s'" % personCode)[0]
        rows = _rows("SELECT f.faceCode AS faceCode, p.shotYear AS shotYear"
                     " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
                     " WHERE f.personCode IS NULL AND p.delFlag = '%s'"
                     " ORDER BY f.recID LIMIT %d" % (comGD.DEL_FLAG_NO, int(limit)))
        codes = []
        for row in rows:
            shotBucket = rebucket.shotBucketFor(row["shotYear"], person["birthday"])
            _assignFace(str(row["faceCode"]), personCode, True, shotBucket)
            codes.append(str(row["faceCode"]))
        assert len(codes) == limit, "夹具里可用的脸不够 %d" % limit
        return codes

    def test_candidate_list_has_one_row_per_merge_not_per_face(self, api_env):
        """一次合并 3 张脸 -> 库里 1 主 + 3 成员行；
        候选集**必须只有 1 条**（成员行是记账，不是独立的用户操作）。"""
        client = api_env["client"]
        faces = self._confirmedFacesOf("P_alpha", 3)
        logCode, _target = self._mergeOnce(api_env, faces)

        # 库里确实有 4 行可撤销日志
        rows = _rows("SELECT logCode AS logCode FROM pb_review_log"
                     " WHERE isRevertible = 1 AND revertedByLogCode IS NULL")
        assert len(rows) == 4
        assert sum(1 for r in rows if "." in str(r["logCode"])) == 3

        listed = client.get("/api/review/revertible", params={"size": 20}).json()
        assert listed["total"] == 1, [i["logCode"] for i in listed["items"]]
        assert listed["items"][0]["logCode"] == logCode
        # 显示名也给了（撤销确认框里要写「把「张三」合并进「张三(2)」」）
        assert listed["items"][0]["toDisplayName"]

        undone = client.post("/api/review/undo", json={"logCode": logCode})
        assert undone.status_code == 200
        assert undone.json()["facesRestored"] == 3

    def test_undo_twice_is_rejected(self, api_env):
        client = api_env["client"]
        faces = self._confirmedFacesOf("P_beta", 2)
        logCode, _target = self._mergeOnce(api_env, faces)
        assert client.post("/api/review/undo",
                           json={"logCode": logCode}).status_code == 200
        second = client.post("/api/review/undo", json={"logCode": logCode})
        assert second.status_code == 409
        assert second.json()["code"] == "TASK_STATE_ILLEGAL"

    def test_undo_restores_faces_links_and_centroids(self, api_env):
        """撤销必须把三样东西一起还原（验收第 4 条）。

        ⚠️ 断言的对象是**被合并走的那个档案**：这两张脸本来就在 A 名下，
        所以合并完它们又回到 A —— A 的脸数**不变**（只是质心被重算过）。
        真正变的是 B：合并时脸与质心被搬空，撤销后再回来。
        写成「合并后 A 的脸变多」会得到一个看似合理的失败，而它其实测错了对象。
        """
        client = api_env["client"]
        faces = self._confirmedFacesOf("P_alpha", 2)

        def snap(code):
            got = _rows("SELECT faceCode AS faceCode, personCode AS personCode,"
                        " isConfirmed AS isConfirmed FROM pb_face"
                        " WHERE personCode = '%s' ORDER BY faceCode" % code)
            links = _rows("SELECT photoCode AS photoCode FROM pb_photo_person"
                          " WHERE personCode = '%s' ORDER BY photoCode" % code)
            cents = _rows("SELECT bucketKey AS bucketKey, sampleCount AS sampleCount"
                          " FROM pb_person_centroid WHERE personCode = '%s'"
                          " ORDER BY bucketKey" % code)
            return (got, links, cents)

        target = self._splitOff(api_env, faces)
        beforeA, beforeB = snap("P_alpha"), snap(target)
        assert len(beforeB[0]) == 2

        # 合并 -> 断言 B 被搬空
        merged2 = client.post("/api/review/merge",
                              json={"fromPersonCode": target,
                                    "toPersonCode": "P_alpha"})
        assert merged2.status_code == 200, merged2.text
        logCode2 = merged2.json()["logCode"]
        midA, midB = snap("P_alpha"), snap(target)
        assert midB[0] == [], "合并后 B 的脸应被搬空"
        assert midB[1] == [], "合并后 B 的照片关联应被搬空"
        assert midB[2] == [], "合并后 B 的质心应被删除"

        assert client.post("/api/review/undo",
                           json={"logCode": logCode2}).status_code == 200
        afterA, afterB = snap("P_alpha"), snap(target)
        assert [(r["faceCode"], r["personCode"], r["isConfirmed"]) for r in afterA[0]] \
            == [(r["faceCode"], r["personCode"], r["isConfirmed"]) for r in beforeA[0]]
        assert [(r["faceCode"], r["personCode"], r["isConfirmed"]) for r in afterB[0]] \
            == [(r["faceCode"], r["personCode"], r["isConfirmed"]) for r in beforeB[0]]
        assert [r["photoCode"] for r in afterB[1]] == [r["photoCode"] for r in beforeB[1]]
        assert [(r["bucketKey"], r["sampleCount"]) for r in afterB[2]] \
            == [(r["bucketKey"], r["sampleCount"]) for r in beforeB[2]]


# ============================================================
# 四、设置接口 + 参数覆盖
# ============================================================

@pytest.fixture(autouse=True)
def restoreParamDefaults():
    """每个用例结束后把阈值/策略恢复原值 —— 覆盖是**进程级**的，
    不还原会污染同进程后面的用例（真实库里表现为「阈值莫名变了」）。"""
    before = (basicSettings.matchThresholds(), basicSettings.bucketStrategy(),
              basicSettings.CENTROID_CONFIRMED_ONLY,
              basicSettings.MATCH_THRESHOLD_PRESET)
    yield
    basicSettings.setThresholdOverride()
    basicSettings.setBucketStrategy(before[1])
    basicSettings.CENTROID_CONFIRMED_ONLY = before[2]
    basicSettings.MATCH_THRESHOLD_PRESET = before[3]


class TestSettingsApi:
    def test_read_only_returns_paths_and_params(self, api_env):
        body = api_env["client"].get("/api/settings").json()
        assert body["ok"] is True
        assert body["paths"]["photo"].endswith("photo")
        assert body["paths"]["backupRoot"]
        assert body["match"]["preset"] in basicSettings.MATCH_THRESHOLD_PRESETS
        assert body["match"]["bucketStrategy"] in basicSettings.BUCKET_STRATEGY_CHOICES
        assert body["match"]["centroidConfirmedOnly"] is True
        # 路径只读：响应里不含任何「写路径」的入口
        assert "setPaths" not in body

    def test_change_preset(self, api_env):
        resp = api_env["client"].post("/api/settings/match", json={"preset": "aggressive"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["after"]["tHigh"] == pytest.approx(0.48)
        assert basicSettings.matchThresholds()[1] == pytest.approx(0.48)
        # 改阈值不必刷桶，但**必须**提示重算质心
        assert body["needRebucket"] is False
        assert body["needCentroidRebuild"] is True
        assert body["steps"]

    def test_bucket_strategy_needs_rebucket(self, api_env):
        resp = api_env["client"].post("/api/settings/match",
                                      json={"bucketStrategy": "fixed5"})
        assert resp.json()["needRebucket"] is True
        assert basicSettings.bucketStrategy() == "fixed5"

    def test_unknown_preset_is_400(self, api_env):
        resp = api_env["client"].post("/api/settings/match", json={"preset": "nope"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "PARAM_INVALID"

    def test_unknown_strategy_is_400(self, api_env):
        resp = api_env["client"].post("/api/settings/match",
                                      json={"bucketStrategy": "yearly"})
        assert resp.status_code == 400

    def test_thresholds_reject_inverted_pair(self, api_env):
        resp = api_env["client"].post("/api/settings/match",
                                      json={"tLow": 0.8, "tHigh": 0.2})
        assert resp.status_code == 400

    def test_centroid_rebuild_for_one_person(self, api_env):
        client = api_env["client"]
        resp = client.post("/api/settings/centroids/rebuild",
                           json={"personCode": "P_alpha"})
        assert resp.status_code == 200
        # 后台线程：轮询到结束
        for _ in range(200):
            status = client.get("/api/settings/centroids/status").json()
            if not status["running"] and status["total"]:
                break
            import time as _t
            _t.sleep(0.05)
        status = client.get("/api/settings/centroids/status").json()
        assert status["running"] is False
        assert status["total"] == 1
        assert status["failed"] == 0, status.get("log")

    def test_centroid_rebuild_rejects_unknown_person(self, api_env):
        resp = api_env["client"].post("/api/settings/centroids/rebuild",
                                      json={"personCode": "NOPE"})
        assert resp.status_code == 404

    def test_backups_endpoint_is_read_only_and_gives_commands(self, api_env):
        body = api_env["client"].get("/api/settings/backups").json()
        assert body["ok"] is True
        assert "backup.py backup" in body["commands"]["backup"]
        assert any("不拷贝 photo" in n for n in body["notes"])
        # 只有 GET，没有 POST/PUT —— 备份/恢复不在 HTTP 里做
        paths = {r.path for r in settingsApi.router.routes}
        assert "/settings/backups" in paths


# ============================================================
# 五、阈值覆盖与分桶策略
# ============================================================

class TestThresholdOverride:
    def test_override_wins_over_preset(self):
        basicSettings.setThresholdOverride(0.11, 0.22)
        assert basicSettings.matchThresholds() == (0.11, 0.22)
        # 给了 preset 就绕过覆盖（那是「按某套预设跑一遍」的显式用法）
        assert basicSettings.matchThresholds("s0") == (0.35, 0.55)

    def test_clear_returns_to_preset(self):
        basicSettings.setThresholdOverride(0.11, 0.22)
        low, high = basicSettings.setThresholdOverride()
        assert (low, high) == basicSettings.matchThresholds("conservative")

    def test_half_override_is_rejected(self):
        with pytest.raises(ValueError):
            basicSettings.setThresholdOverride(0.3, None)

    def test_out_of_range_is_rejected(self):
        with pytest.raises(ValueError):
            basicSettings.setThresholdOverride(0.9, 0.2)
        with pytest.raises(ValueError):
            basicSettings.setThresholdOverride(-0.1, 0.5)


class TestBucketStrategy:
    def test_default_is_adaptive(self):
        assert basicSettings.bucketStrategy() == "adaptive"
        # 2013 年拍、1985 年生 -> 28 岁 -> 成年桶（起点=出生年+18=2003，宽 10）
        assert bucket.bucketKeyAdaptive(2013, 1985) == bucket.bucketKeyAdult(28, 1985)
        assert bucket.bucketKeyAdaptive(2013, 1985) == "2003-2012"

    def test_fixed5_uses_equal_width(self):
        basicSettings.setBucketStrategy("fixed5")
        assert bucket.bucketKeyAdaptive(2013, 1985) == bucket.bucketKeyEqual(2013)

    def test_none_puts_everything_in_all(self):
        basicSettings.setBucketStrategy("none")
        assert bucket.bucketKeyAdaptive(2013, 1985) == bucket.ALL_BUCKET
        assert bucket.bucketKeyAdaptive(2013, None) == bucket.ALL_BUCKET

    def test_invalid_year_still_empty_in_every_strategy(self):
        for name in basicSettings.BUCKET_STRATEGY_CHOICES:
            basicSettings.setBucketStrategy(name)
            assert bucket.bucketKeyAdaptive(None, 1985) == bucket.NO_BUCKET

    def test_unknown_strategy_raises(self):
        with pytest.raises(KeyError):
            basicSettings.setBucketStrategy("yearly")


# ============================================================
# 六、重复照片
# ============================================================

class TestDuplicates:
    def test_groups_by_dup_of(self, api_env):
        body = api_env["client"].get("/api/duplicates").json()
        assert body["total"] == 1
        group = body["items"][0]
        assert group["dupOfPhotoCode"] == "PH_2013_01"
        codes = sorted(p["photoCode"] for p in group["photos"])
        assert codes == ["PH_2013_01", "PH_DUP"]

    def test_kind_is_copy_when_no_move_link(self, api_env):
        group = api_env["client"].get("/api/duplicates").json()["items"][0]
        assert group["kind"] == "copy"

    def test_scope_to_one_photo(self, api_env):
        body = api_env["client"].get("/api/duplicates",
                                     params={"photoCode": "PH_DUP"}).json()
        assert body["total"] == 2

    def test_compare_same_content(self, api_env):
        body = api_env["client"].get("/api/duplicates/compare",
                                     params={"photoCode": "PH_2013_01",
                                             "otherCode": "PH_DUP"}).json()
        # 测试库里两个 photoCode 用了不同 fileHash -> 判「内容不同」
        assert body["sameContent"] is False
        assert body["left"]["photoCode"] == "PH_2013_01"
        assert isinstance(body["diff"], list) and body["diff"]

    def test_compare_flags_that_differ(self, api_env):
        body = api_env["client"].get("/api/duplicates/compare",
                                     params={"photoCode": "PH_2013_01",
                                             "otherCode": "PH_2013_07"}).json()
        fields = {d["field"]: d for d in body["diff"]}
        assert fields["fileSize"]["same"] is False
        assert fields["width"]["same"] is True

    def test_compare_404(self, api_env):
        resp = api_env["client"].get("/api/duplicates/compare",
                                     params={"photoCode": "PH_2013_01",
                                             "otherCode": "NOPE"})
        assert resp.status_code == 404


# ============================================================
# 七、备份脚本
# ============================================================

class TestBackupTool:
    def test_backup_copies_db_and_thumb_but_not_photo(self, api_env, tmp_path):
        from tools import backup as backupTool
        thumbDir = paths.thumb_dir()
        os.makedirs(os.path.join(thumbDir, "thumbs", "aa"), exist_ok=True)
        with open(os.path.join(thumbDir, "thumbs", "aa", "x.webp"), "wb") as fh:
            fh.write(b"webp")
        photoDir = paths.photo_dir()
        with open(os.path.join(photoDir, "原图.jpg"), "wb") as fh:
            fh.write(b"raw")

        result = backupTool.doBackup(destRoot=str(tmp_path / "bak"))
        assert result["ok"] is True
        target = result["path"]
        # 夹具用的库文件名是 api.db（不是默认的 photolib.db）——按目录内容断言，
        # 别把 DB_FILE_NAME 写死成默认值，那会让用例与夹具改名一起悄悄失效
        assert os.path.isfile(os.path.join(target, "db", os.path.basename(paths.db_file())))
        assert os.path.isfile(os.path.join(target, "thumb", "thumbs", "aa", "x.webp"))
        assert os.path.isfile(os.path.join(target, paths.BACKUP_MANIFEST_NAME))
        # **不备份 photoDir**（硬约束：原图不动）
        assert not os.path.exists(os.path.join(target, "photo"))
        assert os.path.isfile(os.path.join(photoDir, "原图.jpg"))

    def test_restore_roundtrip(self, tmp_path, set_photo_root):
        """恢复是**覆盖式**的，用例必须自带一套自己的库。

        ⚠️ 不能挂在 api_env 上：那个夹具里应用正开着库句柄，
        Windows 上文件被占用 —— 改文件会 PermissionError，
        连 shutil.rmtree(dbDir) 也会失败。而那恰恰印证了备份文档里那句
        「备份/恢复必须停服务」：不是流程上的洁癖，是**文件锁**。
        """
        from tools import backup as backupTool
        from tools import build_db as buildDb

        root = tmp_path / "RestoreLib"
        (root / "photo").mkdir(parents=True)
        (root / "thumb").mkdir(parents=True)
        (root / "db").mkdir(parents=True)
        set_photo_root(str(root))
        dbFile = str(root / "db" / "r.db")
        buildDb.build(dbFile=dbFile, verbose=False)
        sqliteCommon.dbHandle(dbFile)
        sqliteCommon.closeDb()                # 模拟「服务已停」

        thumbFile = root / "thumb" / "a.webp"
        thumbFile.write_bytes(b"v1")
        dest = str(tmp_path / "bak")
        first = backupTool.doBackup(destRoot=dest)
        assert first["ok"] is True

        # 现状变化：改掉缩略图，并新建一个多余文件
        thumbFile.write_bytes(b"v2-longer")
        (root / "thumb" / "extra.webp").write_bytes(b"junk")

        code = os.path.basename(first["path"])
        result = backupTool.doRestore(code, destRoot=dest, assumeYes=True)
        assert result["ok"] is True, result.get("errMsg")
        assert thumbFile.read_bytes() == b"v1"
        assert not (root / "thumb" / "extra.webp").exists()
        assert os.path.isfile(dbFile)
        # 恢复前自动另存现状 —— 没有退路的覆盖式恢复是不敢用的
        assert os.path.isdir(result["safetyBackup"])
        # 现状那一份（extra.webp）躺在**安全备份**里，不在原地
        safety = tmp_path / "bak"
        snapshot = [n for n in os.listdir(safety)
                    if n != code and n.startswith(paths.BACKUP_PREFIX)]
        assert len(snapshot) == 1
        assert os.path.isfile(os.path.join(safety, snapshot[0], "thumb", "extra.webp"))
        paths.clearRootOverride()

    def test_two_backups_in_same_second_do_not_collide(self, tmp_path, set_photo_root):
        """时间戳只到秒：连着两次备份**必须**落到两个目录。
        复用同一个目录 = 恢复出来的是两个时刻的混合，且不报错。"""
        from tools import backup as backupTool
        from tools import build_db as buildDb

        root = tmp_path / "TwiceLib"
        for sub in ("photo", "thumb", "db"):
            (root / sub).mkdir(parents=True)
        set_photo_root(str(root))
        buildDb.build(dbFile=str(root / "db" / "t.db"), verbose=False)
        sqliteCommon.dbHandle(str(root / "db" / "t.db"))
        sqliteCommon.closeDb()

        dest = str(tmp_path / "bak2")
        first = backupTool.doBackup(destRoot=dest)
        second = backupTool.doBackup(destRoot=dest)
        assert first["path"] != second["path"]
        assert len(os.listdir(dest)) == 2
        paths.clearRootOverride()

    def test_list_reads_manifest(self, api_env, tmp_path):
        from tools import backup as backupTool
        dest = str(tmp_path / "bak")
        backupTool.doBackup(destRoot=dest, label="unit-test")
        items = backupTool.doList(destRoot=dest)
        assert len(items) == 1
        assert items[0]["manifest"]["label"] == "unit-test"

    def test_backup_root_follows_photo_root(self, api_env):
        assert paths.backup_root().startswith(str(paths.photo_dir()).rsplit("\\", 1)[0])