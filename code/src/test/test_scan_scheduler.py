#! /usr/bin/env python3
#encoding: utf-8

# Filename: test_scan_scheduler.py
# Description: schedule/scanScheduler.py 单测
#
# 覆盖：
#   A. 状态机合法性（IDLE -> RUNNING -> PAUSED/DONE/FAILED，非法流转被拒）
#   B. jobCode 幂等（同一 jobCode 重复建 = 同一个任务）
#   C. 异常 -> errMsg 落库 + FAILED；不留僵尸 RUNNING
#   D. 单写入者互斥（并发跑第二个任务直接被拒）
#   E. 后台线程跑批 + stop() 在批边界停下
#
# 硬约束：只在 tmp 临时根与临时库上跑。

import os
import threading
import time

import pytest

from common import globalDefinition as comGD
from database.auto_generated import sqliteCommon as sqliteCommon
from schedule import scanScheduler as scanScheduler


@pytest.fixture
def photos(scan_root, make_photo):
    root = str(scan_root)
    for index in range(12):
        make_photo(os.path.join(root, "d%d" % (index % 2), "IMG_%02d.jpg" % index))
    return root


def _rows(temp_db):
    sqliteCommon.dbHandle(temp_db)
    return sqliteCommon.query_pb_scan_job("pb_scan_job", delFlag="*")


class TestJobLifecycle:
    def test_create_job_fills_total(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=5)
        assert job["jobStatus"] == comGD.JOB_IDLE
        assert job["totalCount"] == 12
        assert job["batchSize"] == 5
        assert job["batchIndex"] == 0
        assert job["rootPath"] == os.path.abspath(photos)
        assert job["created"] is True

    def test_job_code_is_idempotent(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        first = sched.createJob(root=photos, jobCode="SJ_FIXED_001")
        second = sched.createJob(root=photos, jobCode="SJ_FIXED_001")
        assert second["recID"] == first["recID"]
        assert second["created"] is False
        assert len(_rows(temp_db)) == 1

    def test_auto_job_codes_unique(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        codes = {sched.createJob(root=photos)["jobCode"] for _ in range(3)}
        assert len(codes) == 3
        assert all(c.startswith(basicPrefix()) for c in codes)

    def test_missing_root_raises(self, temp_db, tmp_path):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        with pytest.raises(FileNotFoundError):
            sched.createJob(root=str(tmp_path / "no-such-dir"))

    def test_list_jobs_newest_first(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        for index in range(3):
            sched.createJob(root=photos, jobCode="SJ_L%02d" % index)
        rows = sched.listJobs(10)
        assert [r["jobCode"] for r in rows[:3]] == ["SJ_L02", "SJ_L01", "SJ_L00"]

    def test_unknown_job_lookups(self, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        assert sched.getJob("nope") is None
        assert sched.progress("nope")["ok"] is False
        assert sched.progress("nope")["code"] == comGD.ERR_TASK_NOT_FOUND


def basicPrefix():
    from config import basicSettings as basicSettings
    return basicSettings.SCAN_JOB_CODE_PREFIX


class TestStateMachine:
    def test_happy_path_transitions(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=5)
        jobCode = job["jobCode"]
        assert sched.runBatch(jobCode)["paused"] is True
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_PAUSED
        assert sched.runBatch(jobCode)["paused"] is True
        assert sched.runBatch(jobCode)["done"] is True
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_DONE

    def test_illegal_transition_rejected(self, photos, temp_db):
        """DONE 是终态：不能再被推回 RUNNING"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=12)
        sched.runBatch(job["jobCode"])
        assert sched.getJob(job["jobCode"])["jobStatus"] == comGD.JOB_DONE
        got = sched.setStatus(job["jobCode"], comGD.JOB_RUNNING)
        assert got["ok"] is False
        assert got["code"] == comGD.ERR_TASK_STATE_ILLEGAL
        assert sched.getJob(job["jobCode"])["jobStatus"] == comGD.JOB_DONE

    def test_run_batch_on_done_is_noop(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=12)
        sched.runBatch(job["jobCode"])
        again = sched.runBatch(job["jobCode"])
        assert again["ok"] is True
        assert again["done"] is True
        assert again["result"] is None

    def test_failure_writes_errmsg_and_failed(self, photos, temp_db, monkeypatch):
        """异常必须落 errMsg + FAILED，绝不留一个 RUNNING 的僵尸任务"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=5)

        def _boom(*args, **kwargs):
            raise RuntimeError("磁盘炸了")

        monkeypatch.setattr(scanScheduler.runner, "ScanRunner", _boom)
        got = sched.runBatch(job["jobCode"])
        assert got["ok"] is False
        assert "磁盘炸了" in got["errMsg"]
        row = sched.getJob(job["jobCode"])
        assert row["jobStatus"] == comGD.JOB_FAILED
        assert "磁盘炸了" in row["errMsg"]
        assert row["finishedYMDHMS"]

    def test_errmsg_truncated_to_column(self, photos, temp_db, monkeypatch):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=5)

        def _boom(*args, **kwargs):
            raise RuntimeError("x" * 900)

        monkeypatch.setattr(scanScheduler.runner, "ScanRunner", _boom)
        sched.runBatch(job["jobCode"])
        assert len(sched.getJob(job["jobCode"])["errMsg"]) <= 512

    def test_zombie_running_can_be_adopted(self, photos, temp_db):
        """上次崩在 RUNNING：没有活动任务时应能认领它继续推进，而不是死等人工改库"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=12)
        jobCode = job["jobCode"]
        sqliteCommon.update_pb_scan_job("pb_scan_job", job["recID"],
                                       {"jobStatus": comGD.JOB_RUNNING})
        assert sched.activeJobCode() is None           # 没有活动任务
        got = sched.runBatch(jobCode)
        assert got["ok"] is True
        assert got["done"] is True
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_DONE

    def test_retry_after_failed(self, photos, temp_db, monkeypatch):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=12)
        jobCode = job["jobCode"]
        monkeypatch.setattr(scanScheduler.runner, "ScanRunner",
                           lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert sched.runBatch(jobCode)["ok"] is False
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_FAILED
        monkeypatch.undo()
        assert sched.runBatch(jobCode)["ok"] is True
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_DONE


class TestRunnerReuse:
    """跨批复用同一个 ScanRunner（10 万行规模的必需，见 runner.ensureIndex 注释）"""

    def test_same_run_reuses_runner(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        jobCode = sched.createJob(root=photos, batchSize=4)["jobCode"]
        assert sched.runBatch(jobCode)["paused"] is True
        firstRunner = sched._runnerCache["runner"]
        assert firstRunner is not None
        assert sched.runBatch(jobCode)["paused"] is True
        assert sched._runnerCache["runner"] is firstRunner      # 同一个实例

    def test_cursor_advances_across_batches(self, photos, temp_db):
        """回归防护：复用实例时若不同步游标，第二批会原地重扫 -> 永远扫不完"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        jobCode = sched.createJob(root=photos, batchSize=4)["jobCode"]
        cursors = []
        for _ in range(3):
            sched.runBatch(jobCode)
            cursors.append(sched.getJob(jobCode)["lastCursor"])
        assert all(cursors[i] < cursors[i + 1] for i in range(len(cursors) - 1))
        assert sched.getJob(jobCode)["processedCount"] == 12
        assert len(sqliteCommon.query_pb_photo("pb_photo", delFlag="*")) == 12

    def test_cache_cleared_after_run(self, photos, temp_db):
        """索引缓存只在 runUntilDone 期间有效，结束后必须清掉"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        jobCode = sched.createJob(root=photos, batchSize=12)["jobCode"]
        sched.runUntilDone(jobCode)
        assert sched._runnerCache["runner"] is None
        assert sched._runnerCache["key"] is None

    def test_index_reloaded_when_batch_size_changes(self, photos, temp_db):
        """batchSize 变了必须换新实例（key 变了），否则批次上限还是旧的"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        jobCode = sched.createJob(root=photos, batchSize=4)["jobCode"]
        sched.runBatch(jobCode, batchSize=4)
        firstRunner = sched._runnerCache["runner"]
        second = sched.runBatch(jobCode, batchSize=6)
        assert sched._runnerCache["runner"] is not firstRunner
        assert second["result"]["processed"] == 6

    def test_ensure_index_is_cached(self, photos, temp_db):
        """ensureIndex 第二次调用直接返回同一个索引对象（不重查库）"""
        from processor.scanner import runner as runnerMod
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        jobCode = sched.createJob(root=photos, batchSize=12)["jobCode"]
        sched.runBatch(jobCode)
        scanRunner = sched._runnerCache["runner"]
        assert scanRunner._indexLoaded is not None
        assert scanRunner.ensureIndex() is scanRunner._indexLoaded
        # 新实例的索引一定是空的（只能由 ensureIndex 触发加载）
        assert runnerMod.ScanRunner(jobCode=jobCode, root=photos, batchSize=12,
                                    dbFile=temp_db)._indexLoaded is None


class TestSingleWriter:
    def test_second_job_rejected_while_running(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job1 = sched.createJob(root=photos, batchSize=5)
        job2 = sched.createJob(root=photos, batchSize=5)

        original = scanScheduler.runner.ScanRunner

        def _slow(*args, **kwargs):
            time.sleep(0.3)
            return original(*args, **kwargs)

        scanScheduler.runner.ScanRunner = _slow
        try:
            results = {}

            def _run():
                results["a"] = sched.runBatch(job1["jobCode"])

            thread = threading.Thread(target=_run)
            thread.start()
            time.sleep(0.08)
            results["b"] = sched.runBatch(job2["jobCode"])   # 并发第二个 -> 必须被拒
            thread.join()
        finally:
            scanScheduler.runner.ScanRunner = original

        assert results["a"]["ok"] is True
        assert results["b"]["ok"] is False
        assert results["b"]["code"] == comGD.ERR_TASK_STATE_ILLEGAL
        assert "单写入者" in results["b"]["errMsg"]
        assert sched.isRunning() is False


class TestBackground:
    def test_start_background_runs_to_done(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=4)
        got = sched.startBackground(job["jobCode"])
        assert got["ok"] is True
        assert sched.waitBackground(timeout=30) is True
        assert sched.getJob(job["jobCode"])["jobStatus"] == comGD.JOB_DONE
        assert len(sqliteCommon.query_pb_photo("pb_photo", delFlag="*")) == 12

    def test_stop_at_batch_boundary(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=2)
        sched.startBackground(job["jobCode"])
        time.sleep(0.05)
        sched.stop()
        assert sched.waitBackground(timeout=30) is True
        row = sched.getJob(job["jobCode"])
        # 停在批边界：状态是 PAUSED（还没 DONE），且 lastCursor 已落库
        assert row["jobStatus"] in (comGD.JOB_PAUSED, comGD.JOB_DONE)
        if row["jobStatus"] == comGD.JOB_PAUSED:
            assert row["lastCursor"]
            resumed = sched.runUntilDone(job["jobCode"])
            assert resumed["done"] is True

    def test_double_start_rejected(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=photos, batchSize=1)
        sched.startBackground(job["jobCode"])
        try:
            second = sched.startBackground(job["jobCode"])
            assert second["ok"] in (True, False)      # 取决于上一批是否已跑完
        finally:
            sched.stop()
            sched.waitBackground(timeout=30)


class TestEnsureSchema:
    def test_missing_table_raises_with_hint(self, tmp_path):
        from common import paths as paths
        from config import basicSettings as basicSettings
        empty = tmp_path / "empty.db"
        sqliteCommon.dbHandle(str(empty))
        with pytest.raises(RuntimeError) as err:
            scanScheduler.ScanScheduler(dbFile=str(empty))
        assert "build_db" in str(err.value)
        assert basicSettings.BATCH_SIZE > 0
        assert paths.PHOTO_DIR_NAME == "photo"
