#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_scan_interrupt.py
#Description: 扫描的**可中断性**测试（步骤 9 补）—— walker / runner / API 三层
#
# ══════════════════════════════════════════════════════════════════════
# 为什么必须有这个文件
# ══════════════════════════════════════════════════════════════════════
#   步骤 9 之前，`stopEvent` **只在逐文件循环里**被检查，而那段循环在
#   `walker.listPhotoFiles(self.root)` **之后**才进入。也就是说：
#
#       POST /api/scan/stop  ->  stopEvent.set()
#       但后台线程此刻正卡在「全树 stat」里（10 万张库 2~20s，慢盘更久）
#       -> 用户按了停止，界面上十几秒一点反应都没有
#
#   这不是"性能问题"，而是**承诺问题**：接口说「停止」，实际语义是
#   「下个批边界再停」。所以要把它变成真的可中断，并且用测试钉住。
#
# 三层各自的职责
# --------------
#   ① `walker.listPhotoFiles(shouldStop=...)` —— 遍历本身可中断
#   ② `ScanRunner.runBatch()`               —— 接住中止，**游标/计数一律不动**
#   ③ `/api/scan/stop`                      —— 默认等到线程真的退出再返回
#
# ⚠️ 第 ② 层最要紧的一条是「游标不动」：
#    中止发生在**处理任何文件之前**，所以推进游标就是**撒谎** ——
#    下次续扫会从游标往后走，被跳过的那些文件**永久漏掉且不报错**。
#    所以这里专门断言 `lastCursor` 与全部计数在 abort 前后**逐字段相等**。

import os
import threading

import pytest

from common import paths as paths
from database.auto_generated import sqliteCommon as sqliteCommon
from processor.scanner import runner as runner
from processor.scanner import walker as walker
from schedule import scanScheduler as scanScheduler


# ============================================================
# 一、装置：一棵有足够目录层次的小树（遍历要"走得动"才有中断可言）
# ============================================================

@pytest.fixture
def tree(scan_root, make_photo):
    """60 张照片散在 20 个子目录里 —— 目录数够多，遍历检查点才会被触发。"""
    root = str(scan_root)
    for index in range(60):
        make_photo(os.path.join(root, "y%02d" % (index % 5),
                                "m%02d" % (index % 4),
                                "d%d" % (index % 2),
                                "IMG_%03d.jpg" % index),
                   takenAt="202%d:05:0%d 10:00:00" % (index % 5, 1 + index % 3))
    return root


def _alwaysStop() -> bool:
    return True


def _neverStop() -> bool:
    return False


# ============================================================
# 二、walker：遍历级中断
# ============================================================

class TestWalkerInterrupt:

    def test_listPhotoFilesAbortsWhenAsked(self, tree):
        with pytest.raises(walker.WalkAborted):
            walker.listPhotoFiles(tree, shouldStop=_alwaysStop)

    def test_listPhotoFilesFullWhenNotAsked(self, tree):
        """不给 shouldStop -> 老行为，一个文件都不少。"""
        items = walker.listPhotoFiles(tree)
        assert len(items) == 60

    def test_neverStopGivesSameResultAsNoCallback(self, tree):
        """`shouldStop` 恒假时结果与不传**完全一致**。

        ⚠️ 这条防的是「加了探针顺手把结果改小了」——
           那种错不报错，只是每批漏几张，靠幂等重扫也补不回来（判成 SKIP 了）。
        """
        assert walker.listPhotoFiles(tree, shouldStop=_neverStop) \
            == walker.listPhotoFiles(tree)

    def test_countPhotoFilesIsInterruptible(self, tree):
        """建任务时的 totalCount 也要能中断（`countPhotoFiles` 调的是同一条路）。"""
        assert walker.countPhotoFiles(tree, shouldStop=_neverStop) == 60
        with pytest.raises(walker.WalkAborted):
            walker.countPhotoFiles(tree, shouldStop=_alwaysStop)

    def test_iterPhotoFilesHasSecondCheckpoint(self, tree):
        """`iterPhotoFiles` 在**产出阶段**也有检查点。

        为什么需要两个检查点：清单收集只 stat，而产出阶段要**读文件算 sha256**
        —— 一张 RAW 几百毫秒，那才是真正耗时的地方。只在 walk 级检查，
        「停止」仍然要等完整棵树的 hash 算完。
        """
        calls = {"n": 0}
        total = 60

        def stopAfter(k):
            def _probe():
                calls["n"] += 1
                # 前 k 次调用来自 walk 级检查（_STOP_CHECK_EVERY 个目录一次），
                # 之后的调用只可能来自产出级检查。
                return calls["n"] > k
            return _probe

        # 先让 walk 阶段全部通过（给它足够多的"豁免次数"），
        # 再在第 3 个文件处中止 —— 这样唯一可能抛出的位置就是产出级检查点。
        probe = stopAfter(0)
        with pytest.raises(walker.WalkAborted):
            list(walker.iterPhotoFiles(tree, shouldStop=probe))
        assert calls["n"] > 0

    def test_abortNeverReturnsPartialList(self, tree):
        """中止**必须抛异常**，绝不能返回"部分清单"。

        ⚠️ 这是本文件最核心的一条断言。部分清单会被 `runBatch` 当成全量真值：
              `totalCount = len(items)`（偏小 -> 进度永远到不了 100%）
              `bisect_right(relPaths, lastCursor)`（落在错位置 -> **续扫漏一段**）
           两条都是静默丢数据。所以「中止」与「返回」必须互斥。
        """
        got = None
        try:
            got = walker.listPhotoFiles(tree, shouldStop=_alwaysStop)
        except walker.WalkAborted:
            pass
        assert got is None, "中止时返回了清单（%d 条）-> 会导致静默续扫漏数据" % len(got)


# ============================================================
# 三、runner：接住中止，**游标与计数不动**
# ============================================================

class TestRunnerInterrupt:

    def _job(self, temp_db, root, batchSize=10):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=batchSize, countTotal=False)
        return sched, job["jobCode"]

    def test_runBatchAbortsBeforeTouchingAnything(self, tree, temp_db):
        """stopEvent 已置位 -> 本批一张都不处理，且**游标/计数逐字段不变**。"""
        sched, jobCode = self._job(temp_db, tree)
        sched.setStatus(jobCode, "RUNNING")

        # 先把任务推进到"中间态"：手工写一个游标与几个计数，
        # 再中止 —— 这样才验得出"没被改动"，而不是"本来就都是 0"。
        jobRow = sched.getJob(jobCode)
        sqliteCommon.updateTableGeneral(
            "pb_scan_job", "jobCode = %s", (jobCode,),
            {"lastCursor": "y01/m01/d0/IMG_007.jpg",
             "processedCount": 7, "addedCount": 7, "totalCount": 60})

        stopEvent = threading.Event()
        stopEvent.set()                       # 一进来就叫停
        scanRunner = runner.ScanRunner(jobCode=jobCode, root=tree,
                                       batchSize=10, dbFile=temp_db,
                                       stopEvent=stopEvent)
        before = dict(sqliteCommon.query_pb_scan_job("pb_scan_job",
                                                     jobCode=jobCode)[0])
        result = scanRunner.runBatch()

        assert result["walkAborted"] is True, result
        assert result["stopped"] is True
        assert result["done"] is False and result["paused"] is False
        assert result["processed"] == 0
        assert result["cursor"] == before["lastCursor"]

        after = dict(sqliteCommon.query_pb_scan_job("pb_scan_job",
                                                    jobCode=jobCode)[0])
        for column in ("lastCursor", "processedCount", "addedCount",
                       "skippedCount", "duplicateCount", "pendingCount",
                       "totalCount"):
            assert after[column] == before[column], \
                "中止却改了 %s：%r -> %r（会导致续扫漏数据）" \
                % (column, before[column], after[column])
        # 库里也确实一张照片都没进
        assert sqliteCommon.countTableGeneral("pb_photo") == 0

    def test_runBatchWithoutStopStillWorks(self, tree, temp_db):
        """不叫停时行为完全不变（探针不能顺手改变正常路径）。"""
        sched, jobCode = self._job(temp_db, tree)
        scanRunner = runner.ScanRunner(jobCode=jobCode, root=tree, batchSize=10,
                                       dbFile=temp_db, stopEvent=threading.Event())
        result = scanRunner.runBatch()
        assert result["walkAborted"] is False
        assert result["processed"] == 10          # batchSize 限流
        assert result["paused"] is True
        assert result["cursor"], "正常批次必须推进游标"
        assert sqliteCommon.countTableGeneral("pb_photo") == 10

    def test_stopInBatchKeepsProgressAndAdvancesCursor(self, tree, temp_db):
        """**批内**停止：已处理的照常落库、游标照常推进（干完的活不白费）。

        与「遍历中止」形成对照 —— 一个推进游标、一个不动，
        分界是「有没有开始处理文件」。
        """
        sched, jobCode = self._job(temp_db, tree)

        stopEvent = threading.Event()
        scanRunner = runner.ScanRunner(jobCode=jobCode, root=tree, batchSize=50,
                                       dbFile=temp_db, stopEvent=stopEvent)

        originalMakeEntry = walker.makeEntry
        seen = {"n": 0}

        def countingMakeEntry(*args, **kwargs):
            seen["n"] += 1
            if seen["n"] >= 4:
                stopEvent.set()               # 从第 4 张起叫停（批内检查点会命中）
            return originalMakeEntry(*args, **kwargs)

        walker.makeEntry = countingMakeEntry
        try:
            result = scanRunner.runBatch()
        finally:
            walker.makeEntry = originalMakeEntry

        assert result["stopped"] is True, result
        assert result["walkAborted"] is False        # 遍历是走完了的
        assert result["processed"] >= 1
        assert result["cursor"], "批内停止必须推进游标（已处理的记账了）"
        assert sqliteCommon.countTableGeneral("pb_photo") == result["processed"]

    def test_schedulerMapsAbortToPausedNotFailed(self, tree, temp_db):
        """调度层把「遍历中止」映射成 **PAUSED**（不是 FAILED、更不是 DONE）。

        ⚠️ 映射成 DONE 是最坏的一种：任务状态说"扫完了"，而库里其实
           一张都没扫 —— `lastCursor` 也为空，用户再也不会去续扫。
        """
        sched, jobCode = self._job(temp_db, tree)
        sched.stop()                          # 先叫停
        sched.setStatus(jobCode, "RUNNING")
        result = sched.runBatch(jobCode)
        assert result["ok"] is True, result
        assert result["done"] is False
        assert sched.getJob(jobCode)["jobStatus"] == "PAUSED", \
            sched.getJob(jobCode)["jobStatus"]


# ============================================================
# 四、API：stop 的返回值必须反映**真实**停止状态
# ============================================================

class TestApiStopSemantics:

    def test_stopOnIdleJobIsIdempotent(self, api_env):
        client = api_env["client"]
        started = client.post("/api/scan/start", json={"batchSize": 5}).json()
        jobCode = started["jobCode"]
        first = client.post("/api/scan/stop/%s" % jobCode).json()
        assert first["ok"] is True
        # 默认 wait=1：返回时线程必须真的退出了
        assert first["stillRunning"] is False
        assert first["stopped"] is True
        status = client.get("/api/scan/status/%s" % jobCode).json()
        assert status["running"] is False
        assert status["jobStatus"] in ("PAUSED", "DONE", "FAILED")
        # 幂等：再叫一次不报错
        again = client.post("/api/scan/stop/%s" % jobCode).json()
        assert again["ok"] is True

    def test_stopUnknownJob404(self, api_env):
        got = api_env["client"].post("/api/scan/stop/SJ_nope")
        assert got.status_code == 404

    def test_startRejectsRootOutsidePhotoDir(self, api_env):
        """`rootPath` 只允许照片库根本身（选项 b：精确相等）。

        ⚠️ 不挡的话 `{"rootPath": "C:\\\\"}` 会**真的遍历整个 C 盘**
           并把库外目录的图片按错误 relPath 写进 pb_photo ——
           它不碰 `photo\\`（原图只读没破），但会把库搞脏。
        """
        import os
        import tempfile

        client = api_env["client"]
        allowed = os.path.abspath(str(paths.photo_dir()))
        outside = tempfile.mkdtemp(prefix="outside_")
        try:
            # ① 库外目录 -> 400
            got = client.post("/api/scan/start", json={"rootPath": outside})
            assert got.status_code == 400, got.text
            body = got.json()
            assert body["code"] == "PARAM_INVALID"
            assert body["extra"]["allowedRoot"] == allowed
            assert body["extra"]["received"] == os.path.abspath(outside)
            # ② 照片库的**子目录**也不行（会改变 relPath 语义 -> 重复入库）
            got = client.post("/api/scan/start",
                              json={"rootPath": os.path.join(allowed, "sub")})
            assert got.status_code == 400, got.text
            # ③ 照片库根本身 -> 放行
            got = client.post("/api/scan/start",
                              json={"rootPath": allowed, "batchSize": 1})
            assert got.status_code == 200, got.text
            assert got.json()["rootPath"] == allowed
            client.post("/api/scan/stop/%s" % got.json()["jobCode"])
        finally:
            os.rmdir(outside)

    def test_startWithoutRootPathUsesPhotoDir(self, api_env):
        client = api_env["client"]
        got = client.post("/api/scan/start", json={"batchSize": 1,
                                                   "maxBatches": 1}).json()
        assert got["rootPath"] == os.path.abspath(str(paths.photo_dir()))
        client.post("/api/scan/stop/%s" % got["jobCode"])
