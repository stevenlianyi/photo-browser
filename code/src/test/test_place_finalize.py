#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_place_finalize.py
#Description: 地点侧收尾（`processor/place/placeFinalize.py`）与扫描收尾接入的单测
#
# 覆盖
#   A. `finalizePlaces()` 本体
#      1. **硬顺序**：先填 placeNameDir，再重建字典 —— 新地点必须出现
#      2. 三步都跑、结果都在返回值里
#      3. `withNameZh=False` 时第③步真的不跑
#      4. **只填空不覆盖**（用户手工改过的值必须活下来）
#      5. 目录改名 ⇒ **报漂移、不覆盖**
#      6. 失败隔离：① 抛错 ⇒ ② 仍执行；② 抛错 ⇒ 不向上抛（进 errors）
#   B. 扫描收尾接入（`scanScheduler.runBatch` 的 DONE 分支）
#      7. **跑到 DONE ⇒ 目录名自动入库、地点自动进字典**（本步的核心价值）
#      8. 半截扫描（maxBatches 用完）⇒ **不收尾**（半截库不许建字典）
#      9. 批次失败（FAILED）⇒ **不收尾**
#     10. 收尾自己抛异常 ⇒ 任务**仍是 DONE**（绝不把照片已入库说成失败）
#     11. 开关关闭 ⇒ 跳过；但显式调 `finalizePlaces()` 仍然工作
#     12. **CLI 路径（不注册 onFinished）也收尾** —— 防"只在服务端生效"
#
# 硬约束：只在 tmp 临时根与临时库上跑（conftest 的 guard_production_db 兜底）。

import os

import pytest

from common import globalDefinition as comGD
from database import queryCommon as query
from database.auto_generated import sqliteCommon as sqliteCommon


@pytest.fixture
def placeRows(temp_db, set_photo_root):
    """临时库 + 一批照片行（2 张目录名线索 / 1 张 GPS 线索）。"""
    def _add(photoCode, relPath, placeName=None, placeNameDir=None,
             lat=None, lon=None, shotYear=2013):
        sqliteCommon.insertManyTableGeneral(
            "pb_photo",
            [{"photoCode": photoCode, "relPath": relPath,
              "relPathHash": "rh_%s" % photoCode, "fileHash": "fh_%s" % photoCode,
              "fileSize": 100, "shotYear": shotYear,
              "placeName": placeName, "placeNameDir": placeNameDir,
              "lat": lat, "lon": lon,
              "faceCount": 0, "isDuplicate": 0, "isMissing": 0, "scanState": 1}],
            conflictColumns=("photoCode",),
            updateColumns=("placeName", "placeNameDir", "lat", "lon"),
            fillStandard=True,
            forceColumns=("placeName", "placeNameDir", "lat", "lon"))

    _add("PH_W_0", "2013美国游/照片/风景照/2013.07.26 华盛顿/IMG_0.jpg")
    _add("PH_W_1", "2013美国游/照片/风景照/2013.07.26 华盛顿/IMG_1.jpg")
    _add("PH_BJ_0", "Lian Family/BaiRuiQin/IMG_bj.jpg",
         placeName="CN, Beijing, Datun", lat=39.98, lon=116.40)
    return {"add": _add, "dbFile": temp_db}


@pytest.fixture
def scanFiles(scan_root, make_photo):
    """扫描根：一个「日期 + 地名」目录，4 张图（走真实扫描链路）。"""
    root = str(scan_root)
    target = os.path.join(root, "2013美国游", "照片", "风景照", "2013.07.26 华盛顿")
    os.makedirs(target, exist_ok=True)
    for index in range(4):
        make_photo(os.path.join(target, "IMG_%02d.jpg" % index))
    return root


def _placeDirs():
    return [str(one.get("placeNameDir") or "") for one in query.selectList(
        "SELECT p.placeNameDir AS placeNameDir FROM pb_photo p ORDER BY p.photoCode")]


def _placeNames():
    return [str(one.get("placeName") or "") for one in query.selectList(
        "SELECT g.placeName AS placeName FROM pb_place g ORDER BY g.placeName")]


# ============================================================
# A. finalizePlaces() 本体
# ============================================================
class TestFinalizePlaces:
    def test_fillsThenRebuildsInOrder(self, placeRows):
        """**硬顺序**：① 填列 → ② 重建 ⇒ 目录名地点必须出现在字典里。"""
        from processor.place import placeFinalize

        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert report["ok"] is True, report["errors"]
        assert report["scan"]["written"] == 2          # 两张华盛顿
        assert report["rebuild"]["placeCount"] == 2    # 华盛顿 + Datun
        assert set(_placeNames()) == {"华盛顿", "CN, Beijing, Datun"}
        rows = {str(one["placeName"]): one for one in query.selectList(
            "SELECT placeName, photoCount, centerLat, nameZh FROM pb_place")}
        assert int(rows["华盛顿"]["photoCount"]) == 2
        # 目录名地点没有 GPS ⇒ 中心点 NULL；nameZh 由第③步负责（这里关掉了）
        assert rows["华盛顿"]["centerLat"] is None
        assert rows["华盛顿"]["nameZh"] is None

    def test_runsAllThreeSteps(self, placeRows):
        """三步都在返回值里（第③步开着时 `nameZh` 不为 None）。"""
        from processor.place import placeFinalize

        report = placeFinalize.finalizePlaces(withNameZh=True, source="test")
        assert report["ok"] is True, report["errors"]
        assert report["scan"] is not None
        assert report["rebuild"] is not None
        assert report["nameZh"] is not None
        # 只有 Datun 有坐标 ⇒ 它才是中文名的候选；华盛顿没坐标（noCoordinate）
        assert report["nameZh"]["byReason"].get("noCoordinate", 0) >= 1
        assert report["elapsed"] >= 0

    def test_skipsNameZhWhenAsked(self, placeRows):
        from processor.place import placeFinalize

        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert report["nameZh"] is None
        assert report["rebuild"] is not None

    def test_onlyFillsNeverOverwrites(self, placeRows):
        """**只填空不覆盖**：手工改过的值必须活下来（自动收尾同一条纪律）。"""
        from processor.place import placeFinalize

        placeRows["add"]("PH_W_2", "2013美国游/照片/风景照/2013.07.16 纽约/IMG_2.jpg",
                         placeNameDir="外婆家")           # 用户手工改过
        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert report["scan"]["written"] == 2            # 只填了两张华盛顿
        assert report["scan"]["alreadyFilled"] == 1
        got = sorted(_placeDirs())
        # ⚠️ `_placeDirs()` 看的是 `placeNameDir` 列：GPS 那一行的目录名线索是**空**
        #    （它的地点来自 `placeName`，不是目录名）—— 两列不要混。
        assert got == sorted(["", "外婆家", "华盛顿", "华盛顿"]), got
        assert "外婆家" in got, "手工改过的值被自动收尾冲掉了"

    def test_reportsDriftWithoutOverwriting(self, placeRows):
        """目录改名 ⇒ 报漂移，但**不**自动覆盖（改名旧值与手工值长得一样）。"""
        from processor.place import placeFinalize

        placeRows["add"]("PH_OLD", "2013美国游/照片/风景照/2013.07.26 华盛顿/IMG_9.jpg",
                         placeNameDir="张家界")           # 改名前的旧值
        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert len(report["drift"]) == 1
        assert report["drift"][0]["old"] == "张家界"
        assert report["drift"][0]["now"] == "华盛顿"
        assert "张家界" in _placeDirs(), "漂移行被自动覆盖了（纪律②被破坏）"

    def test_scanFailureStillRebuilds(self, placeRows, monkeypatch):
        """① 失败**不能**跳过 ②：填列失败只意味着"这轮没新线索"。"""
        from processor.place import dirNamePlace, placeFinalize

        def _boom(*args, **kwargs):
            raise RuntimeError("填列炸了")

        monkeypatch.setattr(dirNamePlace, "scanDirNames", _boom)
        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert report["ok"] is False
        assert report["scan"] is None
        assert any(one.startswith("scan:") for one in report["errors"]), report["errors"]
        assert report["rebuild"] is not None, "第②步被连带跳过了"
        assert "CN, Beijing, Datun" in _placeNames()

    def test_rebuildFailureIsContained(self, placeRows, monkeypatch):
        """② 抛错 ⇒ `finalizePlaces` **不抛**（调用方是扫描收尾，抛出去会污染任务状态）。"""
        from processor.place import placeFinalize, placeStore

        def _boom(*args, **kwargs):
            raise RuntimeError("重建炸了")

        monkeypatch.setattr(placeStore, "rebuildPlaces", _boom)
        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert report["ok"] is False
        assert report["rebuild"] is None
        assert any(one.startswith("rebuild:") for one in report["errors"])
        # ① 仍然跑过了（顺序没被异常打乱）
        assert report["scan"] is not None and report["scan"]["written"] == 2


# ============================================================
# B. 扫描收尾接入
# ============================================================
def _scanToDone(scanFiles, temp_db, maxBatches=None, batchSize=2):
    from schedule import scanScheduler
    sched = scanScheduler.ScanScheduler(dbFile=temp_db)     # ⚠️ 刻意**不注册** onFinished
    job = sched.createJob(root=scanFiles, batchSize=batchSize)
    summary = sched.runUntilDone(job["jobCode"], maxBatches=maxBatches)
    return sched, job["jobCode"], summary


class TestScanFinalize:
    def test_scanDoneFillsDirNamesAndDictionary(self, scanFiles, temp_db):
        """跑到 DONE ⇒ 目录名自动入库 + 地点自动进字典（**用户零操作**）。"""
        sched, jobCode, summary = _scanToDone(scanFiles, temp_db)
        assert summary["done"] is True
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_DONE
        assert len(sqliteCommon.query_pb_photo("pb_photo", delFlag="*")) == 4
        # 目录名被填
        assert _placeDirs() == ["华盛顿"] * 4
        # 地点进了字典（张数正确）
        rows = {str(one["placeName"]): one for one in query.selectList(
            "SELECT placeName, photoCount, source FROM pb_place")}
        assert set(rows) == {"华盛顿"}
        assert int(rows["华盛顿"]["photoCount"]) == 4
        # 收尾结果带在 summary 上（CLI/接口看得见）
        place = summary["placeFinalize"]
        assert place["ok"] is True
        assert place["filled"] == 4 and place["placeCount"] == 1
        assert place["driftCount"] == 0 and place["errors"] == []

    def test_halfScanDoesNotFinalize(self, scanFiles, temp_db):
        """半截扫描（`maxBatches` 用完）⇒ **不收尾**（半截库不许建字典）。"""
        sched, jobCode, summary = _scanToDone(scanFiles, temp_db, maxBatches=1)
        assert summary["done"] is False
        assert "placeFinalize" not in summary
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_PAUSED
        # 只扫了 2 张（batchSize=2），这 2 行的目录名必须还是空 —— 收尾没跑
        assert len(sqliteCommon.query_pb_photo("pb_photo", delFlag="*")) == 2
        assert _placeDirs() == ["", ""], "半截扫描也收尾了"
        assert _placeNames() == []

    def test_failedScanDoesNotFinalize(self, scanFiles, temp_db, monkeypatch):
        """批次失败（FAILED）⇒ **不收尾**。"""
        from schedule import scanScheduler

        def _boom(*args, **kwargs):
            raise RuntimeError("磁盘炸了")

        monkeypatch.setattr(scanScheduler.runner, "ScanRunner", _boom)
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=scanFiles, batchSize=2)
        got = sched.runBatch(job["jobCode"])
        assert got["ok"] is False
        assert sched.getJob(job["jobCode"])["jobStatus"] == comGD.JOB_FAILED
        assert _placeNames() == []

    def test_finalizeFailureKeepsJobDone(self, scanFiles, temp_db, monkeypatch):
        """收尾自己炸了 ⇒ 任务**仍是 DONE**（照片确实扫进去了，不能报成失败）。"""
        from processor.place import placeFinalize
        from schedule import scanScheduler

        def _boom(*args, **kwargs):
            raise RuntimeError("收尾炸了")

        monkeypatch.setattr(placeFinalize, "finalizePlaces", _boom)
        sched, jobCode, summary = _scanToDone(scanFiles, temp_db)
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_DONE
        assert summary["placeFinalize"]["ok"] is False
        assert "收尾炸了" in summary["placeFinalize"]["errMsg"]
        # 收尾没跑成 ⇒ 目录名/字典都没动（但不是"扫描失败"）
        assert _placeDirs() == [""] * 4
        assert _placeNames() == []
        assert len(sqliteCommon.query_pb_photo("pb_photo", delFlag="*")) == 4

    def test_switchOffSkipsAutoFinalize(self, scanFiles, temp_db, monkeypatch):
        """开关关闭 ⇒ 扫描后不收尾；但**显式**调 `finalizePlaces()` 仍然工作。"""
        from config import basicSettings as basicSettings
        from processor.place import placeFinalize

        monkeypatch.setattr(basicSettings, "PLACE_FINALIZE_AFTER_SCAN", False,
                            raising=False)
        sched, jobCode, summary = _scanToDone(scanFiles, temp_db)
        assert summary["done"] is True
        assert summary["placeFinalize"] == {"ok": True, "skipped": True,
                                           "reason": "PLACE_FINALIZE_AFTER_SCAN = False"}
        assert _placeDirs() == [""] * 4
        assert _placeNames() == []
        # 显式调用（= CLI `--rebuild`）不受这个开关影响
        report = placeFinalize.finalizePlaces(withNameZh=False, source="test")
        assert report["scan"]["written"] == 4
        assert _placeNames() == ["华盛顿"]

    def test_cliPathWithoutOnFinishedAlsoFinalizes(self, scanFiles, temp_db):
        """**CLI 路径**（不注册 `onFinished`）也必须收尾 —— 防"只在服务端生效"。

        `tools/scan_cli.py` 建调度器时没传 `onFinished`，所以当年的人脸自动识别
        钩子对 CLI 全量扫描**不生效**。地点收尾刻意挂在状态机（`runBatch` 的
        DONE 分支）上，正是为了不重复这个毛病 —— 这条用例就是它的守门员。
        """
        from schedule import scanScheduler

        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        assert sched.onFinished is None, "本用例的前提：没有注册完成钩子"
        job = sched.createJob(root=scanFiles, batchSize=2)
        batchResult = sched.runBatch(job["jobCode"])       # CLI 交互模式也是这么调的
        assert "placeFinalize" not in (batchResult["result"] or {}), \
            "还没扫完就收尾了"
        # 逐批跑到 DONE（CLI 交互模式的循环）
        while not batchResult.get("done"):
            batchResult = sched.runBatch(job["jobCode"])
        assert sched.getJob(job["jobCode"])["jobStatus"] == comGD.JOB_DONE
        # 收尾结果挂在**真的跑完**的那一批上
        assert batchResult["result"]["placeFinalize"]["filled"] == 4
        assert _placeDirs() == ["华盛顿"] * 4
        assert _placeNames() == ["华盛顿"]


# ============================================================
# C. HTTP 入口：POST /api/places/rebuild 走**同一条**链路
# ============================================================
def test_apiRebuildAlsoFillsDirNames(api_env):
    """`POST /api/places/rebuild` 也必须补扫目录名。

    ⚠️ 不补扫会怎样（一条静默的失效）：界面上「刷新地点」只做 `GROUP BY`，
       而新目录的地点在 `placeNameDir` 里还没填 ⇒ 刷新之后**新目录还是不出现**，
       而响应里 `source="rebuilt"`、统计齐全，看不出任何异常。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    sqliteCommon.insertManyTableGeneral(
        "pb_photo",
        [{"photoCode": "PH_NEWDIR",
          "relPath": "2013美国游/照片/风景照/2013.07.26 华盛顿/x.jpg",
          "relPathHash": "rh_newdir", "fileHash": "fh_newdir", "fileSize": 100,
          "shotYear": 2013, "placeName": None, "placeNameDir": None,
          "faceCount": 0, "isDuplicate": 0, "isMissing": 0, "scanState": 1}],
        conflictColumns=("photoCode",),
        updateColumns=("placeName", "placeNameDir"), fillStandard=True,
        forceColumns=("placeName", "placeNameDir"))

    body = api_env["client"].post("/api/places/rebuild").json()
    assert body["ok"] is True and body["source"] == "rebuilt"
    # ① 原有的 rebuild 契约一个字段都没少（别把老前端打坏）
    for key in ("placeCount", "groups", "created", "updated", "total",
                "unnamed", "zeroed"):
        assert key in body, key
    # ② 新增：目录名补扫跑了、没有失败步骤
    assert body["scanFilled"] == 1, body
    assert body["finalizeErrors"] == []
    assert body["driftCount"] == 0
    # ③ 新目录的地点真的进了字典
    rows = {str(one["placeName"]): one for one in query.selectList(
        "SELECT placeName, photoCount, centerLat, nameZh FROM pb_place")}
    assert "华盛顿" in rows, rows
    assert int(rows["华盛顿"]["photoCount"]) == 1
    assert rows["华盛顿"]["centerLat"] is None      # 没有 GPS ⇒ 老实留 NULL
