#! /usr/bin/env python3
#encoding: utf-8

# Filename: test_scanner_runner.py
# Description: processor/scanner/runner.py 单测
#
# 覆盖两大块：
#   A. decide() —— 增量三路判定的**纯函数**分支（不碰库）
#   B. 端到端（临时 photo 根 + 临时库）：首扫 / 幂等 / 增量 / 改名 / 复制 /
#      缺失目录 / 批次限流与断点续扫 / relPath 逐字保留 / 内容变更清空旧元数据
#
# 硬约束：全部落在 pytest 的 tmp_path 下，**绝不碰真实 d:\PhotoLib**。

import os

import pytest

from common import globalDefinition as comGD
from common import paths as paths
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from processor.scanner import runner as runner
from processor.scanner import walker as walker
from schedule import scanScheduler as scanScheduler


# ============================================================
# A. decide() 纯函数分支
# ============================================================

def _entry(relPath: str, fileHash: str):
    """造一个 FileEntry（不落盘，直接给决定函数用）"""
    return walker.FileEntry(absPath="C:/x/" + relPath.replace("/", os.sep),
                            relPath=relPath,
                            normPath=paths.normalize_relpath(relPath),
                            relPathHash=walker.calcRelPathHash(relPath),
                            fileHash=fileHash, fileSize=1, mtime=0.0)


def _codes():
    """确定性的 photoCode 工厂（测试里要能断言 photoCode）"""
    counter = {"n": 0}

    def _factory():
        counter["n"] += 1
        return "PH_TEST_%03d" % counter["n"]
    return _factory


class TestDecide:
    def test_new_when_path_and_content_unknown(self):
        got = runner.decide(_entry("a/b.jpg", "h1"), {}, {}, {}, _codes())
        assert got.action == runner.ACTION_NEW
        assert got.isDuplicate == 0
        assert got.dupOfPhotoCode is None
        assert got.needFaceRefresh is True
        assert got.photoCode == "PH_TEST_001"

    def test_skip_when_both_hit_and_hash_same(self):
        ref = runner.PhotoRef("PH_OLD", "a/b.jpg", walker.calcRelPathHash("a/b.jpg"), "h1")
        got = runner.decide(_entry("a/b.jpg", "h1"), {"%s" % ref.relPathHash: ref},
                           {"h1": [ref]}, {"PH_OLD": ref}, _codes())
        assert got.action == runner.ACTION_SKIP
        assert got.photoCode == "PH_OLD"        # 沿用原编码，绝不新造
        assert got.needFaceRefresh is False

    def test_update_when_path_hit_but_hash_changed(self):
        ref = runner.PhotoRef("PH_OLD", "a/b.jpg", walker.calcRelPathHash("a/b.jpg"), "h1")
        got = runner.decide(_entry("a/b.jpg", "h2"), {ref.relPathHash: ref},
                           {}, {"PH_OLD": ref}, _codes())
        assert got.action == runner.ACTION_UPDATE
        assert got.photoCode == "PH_OLD"
        assert got.needFaceRefresh is True   # 人脸要重提取
        assert got.isDuplicate == 0

    def test_update_into_duplicate_when_new_content_matches_other(self):
        ref = runner.PhotoRef("PH_OLD", "a/b.jpg", walker.calcRelPathHash("a/b.jpg"), "h1")
        other = runner.PhotoRef("PH_OTHER", "z/c.jpg", walker.calcRelPathHash("z/c.jpg"), "h2")
        got = runner.decide(_entry("a/b.jpg", "h2"), {ref.relPathHash: ref},
                           {"h2": [other]}, {"PH_OLD": ref, "PH_OTHER": other}, _codes())
        assert got.action == runner.ACTION_UPDATE
        assert got.isDuplicate == 1
        assert got.dupOfPhotoCode == "PH_OTHER"

    def test_moved_when_content_hit_path_miss(self):
        """老内容换路径 = 移动/重命名：新行标重复，**旧行一个字段都不该被改**"""
        ref = runner.PhotoRef("PH_OLD", "old/b.jpg", walker.calcRelPathHash("old/b.jpg"), "h1")
        got = runner.decide(_entry("new/b.jpg", "h1"), {}, {"h1": [ref]},
                           {"PH_OLD": ref}, _codes())
        assert got.action == runner.ACTION_MOVED
        assert got.photoCode == "PH_TEST_001"          # 新记录
        assert got.dupOfPhotoCode == "PH_OLD"          # 指向旧记录
        assert got.isDuplicate == 1
        assert got.old.photoCode == "PH_OLD"

    def test_moved_prefers_usable_primary(self):
        dup = runner.PhotoRef("PH_DUP", "x.jpg", "h_x", "h1", isDuplicate=1,
                              dupOfPhotoCode="PH_MAIN")
        missing = runner.PhotoRef("PH_MISS", "y.jpg", "h_y", "h1", isMissing=1)
        deleted = runner.PhotoRef("PH_DEL", "z.jpg", "h_z", "h1", isDeleted=1)
        good = runner.PhotoRef("PH_GOOD", "w.jpg", "h_w", "h1")
        byCode = {r.photoCode: r for r in (dup, missing, deleted, good)}
        got = runner.decide(_entry("n.jpg", "h1"), {}, {"h1": [dup, missing, deleted, good]},
                           byCode, _codes())
        assert got.dupOfPhotoCode == "PH_GOOD"

    def test_pick_primary_follows_dup_chain(self):
        dup = runner.PhotoRef("PH_DUP", "x.jpg", "h_x", "h1", isDuplicate=1,
                              dupOfPhotoCode="PH_MAIN")
        main = runner.PhotoRef("PH_MAIN", "m.jpg", "h_m", "h1", isMissing=1)
        byCode = {"PH_DUP": dup, "PH_MAIN": main}
        assert runner.pickPrimary([dup], byCode).photoCode == "PH_MAIN"
        assert runner.pickPrimary([], byCode) is None
        # 链断了也要能兜底，绝不死循环
        assert runner.pickPrimary([dup], {}).photoCode == "PH_DUP"

    def test_decide_does_not_mutate_index(self):
        """decide 是纯函数：判定完索引必须原样（改状态是调用方的活）"""
        ref = runner.PhotoRef("PH_OLD", "old/b.jpg", walker.calcRelPathHash("old/b.jpg"), "h1")
        relIndex = {ref.relPathHash: ref}
        fileIndex = {"h1": [ref]}
        before = (len(relIndex), len(fileIndex))
        runner.decide(_entry("new/b.jpg", "h1"), relIndex, fileIndex, {}, _codes())
        assert (len(relIndex), len(fileIndex)) == before

    def test_action_text_complete(self):
        for action in (runner.ACTION_NEW, runner.ACTION_SKIP, runner.ACTION_UPDATE,
                       runner.ACTION_MOVED):
            assert runner.ACTION_TEXT.get(action)


# ============================================================
# B. 端到端
# ============================================================

def _photoRows(dbFile: str) -> list:
    sqliteCommon.dbHandle(dbFile)
    return sqliteCommon.query_pb_photo("pb_photo", mode="light", delFlag="*")


def sc_rows(dbFile: str, nullFields=()) -> list:
    """按 nullFields 查 pb_photo（待确认队列用，走生成层的白名单参数）"""
    sqliteCommon.dbHandle(dbFile)
    return sqliteCommon.query_pb_photo("pb_photo", mode="light", delFlag="*",
                                      nullFields=nullFields)


def _byRelPath(rows: list) -> dict:
    return {row["relPath"]: row for row in rows}


def _runAll(sched, root, dbFile, batchSize=None):
    job = sched.createJob(root=root, batchSize=batchSize)
    summary = sched.runUntilDone(job["jobCode"])
    return job["jobCode"], summary


@pytest.fixture
def photos(scan_root, make_photo):
    """一棵 12 张的小树：5 级嵌套 + 截图 + 微信名 + 无 EXIF"""
    root = str(scan_root)
    for index in range(8):
        make_photo(os.path.join(root, "2024", "2024-05-01", "d%d" % (index % 2),
                                "IMG_%02d.jpg" % index),
                   takenAt="2024:05:0%d 10:00:00" % (1 + index % 3), model="Cam%d" % index)
    make_photo(os.path.join(root, "2019", "wechat", "mmexport_1682908800000.jpg"))
    make_photo(os.path.join(root, "2019", "misc", "20190505_0001.jpg"))
    make_photo(os.path.join(root, "2023", "misc", "Screenshot_20240501_120000.png"),
               fmt="PNG")
    make_photo(os.path.join(root, "2023", "misc", "截图 20230202.png"), fmt="PNG")
    return root


class TestEndToEnd:
    def test_first_scan_inserts_all(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        jobCode, summary = _runAll(sched, photos, temp_db)
        assert summary["done"] is True
        rows = _photoRows(temp_db)
        assert len(rows) == 12
        assert summary["counts"]["added"] == 12
        assert all(row["isMissing"] == 0 for row in rows)
        assert all(row["scanState"] == comGD.SCAN_STATE_PENDING for row in rows)
        assert all(row["delFlag"] == comGD.DEL_FLAG_NO for row in rows)
        job = sched.getJob(jobCode)
        assert job["jobStatus"] == comGD.JOB_DONE
        assert job["processedCount"] == 12

    def test_second_scan_is_idempotent(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        before = _byRelPath(_photoRows(temp_db))

        jobCode, summary = _runAll(sched, photos, temp_db)
        assert summary["counts"]["added"] == 0          # 验收第 2 条
        assert summary["counts"]["skipped"] == 12
        after = _photoRows(temp_db)
        assert len(after) == 12                        # 没多也没少
        assert set(_byRelPath(after)) == set(before)
        for path, row in _byRelPath(after).items():
            assert row["photoCode"] == before[path]["photoCode"]
            assert row["regYMDHMS"] == before[path]["regYMDHMS"]   # 没被重写

    def test_incremental_only_touches_new_files(self, photos, temp_db, make_photo):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        for index in range(100, 110):
            make_photo(os.path.join(photos, "new", "NEW_%02d.jpg" % index))

        _jobCode, summary = _runAll(sched, photos, temp_db)
        assert summary["counts"]["added"] == 10        # 验收第 3 条
        assert summary["counts"]["skipped"] == 12
        assert len(_photoRows(temp_db)) == 22

    def test_rename_is_move_not_add_and_delete(self, photos, temp_db):
        """验收第 4 条：改名 = 疑似移动，**旧记录 relPath 一个字都不改、记录也不删**"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        before = _byRelPath(_photoRows(temp_db))
        oldRel = "2024/2024-05-01/d0/IMG_00.jpg"
        newRel = "2024/2024-05-01/d0/IMG_00_改名.jpg"
        os.rename(os.path.join(photos, oldRel.replace("/", os.sep)),
                  os.path.join(photos, newRel.replace("/", os.sep)))

        _jobCode, summary = _runAll(sched, photos, temp_db)
        after = _byRelPath(_photoRows(temp_db))
        assert summary["counts"]["moved"] == 1
        assert summary["counts"]["duplicate"] == 1
        assert len(after) == 13                        # 旧行还在 + 新行 1 条

        oldRow = after[oldRel]
        assert oldRow["relPath"] == oldRel             # 没被自动改写
        assert oldRow["isMissing"] == 1                # 老路径确实没了
        assert oldRow["isDuplicate"] == 0              # 旧行身份不变

        newRow = after[newRel]
        assert newRow["isDuplicate"] == 1
        assert newRow["dupOfPhotoCode"] == oldRow["photoCode"]
        assert newRow["relPathHash"] != oldRow["relPathHash"]

    def test_rename_links_both_directions(self, photos, temp_db):
        """新增字段 movedToPhotoCode：移动关系**双向可查**，UI 不用反查"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        oldRel = "2019/misc/20190505_0001.jpg"
        newRel = "2019/misc/20190505_0001_改名.jpg"
        os.rename(os.path.join(photos, oldRel.replace("/", os.sep)),
                  os.path.join(photos, newRel.replace("/", os.sep)))

        _jobCode, summary = _runAll(sched, photos, temp_db)
        assert summary["counts"]["moveLinked"] == 1
        rows = _byRelPath(_photoRows(temp_db))
        oldRow, newRow = rows[oldRel], rows[newRel]
        assert oldRow["movedToPhotoCode"] == newRow["photoCode"]   # 旧 -> 新
        assert newRow["dupOfPhotoCode"] == oldRow["photoCode"]    # 新 -> 旧
        assert oldRow["relPath"] == oldRel                        # 路径仍未改
        # 「待确认移动」队列（步骤 9 用；生成层只提供 nullFields=IS NULL，
        # 取「非空」在 3 万行上直接 Python 过滤即可，不必再加查询参数）
        pending = [r for r in _photoRows(temp_db) if r["movedToPhotoCode"]]
        assert len(pending) == 1
        assert pending[0]["photoCode"] == oldRow["photoCode"]

    def test_plain_copy_has_no_move_link(self, photos, temp_db):
        """纯复制**不该**被标成移动：新行有 dupOfPhotoCode，但旧行 movedToPhotoCode 为空"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        srcRel = "2019/misc/20190505_0001.jpg"
        dstRel = "2019/misc/副本_20190505_0001.jpg"
        shutil = open(os.path.join(photos, srcRel.replace("/", os.sep)), "rb").read()
        with open(os.path.join(photos, dstRel.replace("/", os.sep)), "wb") as handle:
            handle.write(shutil)

        _jobCode, _summary = _runAll(sched, photos, temp_db)
        rows = _byRelPath(_photoRows(temp_db))
        # 字典序：数字 < 汉字，所以「20190505_0001.jpg」在「副本_…」之前 -> 原件是主图
        assert rows[srcRel]["isDuplicate"] == 0
        assert rows[dstRel]["isDuplicate"] == 1
        assert rows[srcRel]["movedToPhotoCode"] is None
        assert rows[dstRel]["movedToPhotoCode"] is None

    def test_move_link_first_wins_and_copies_not_linked(self, photos, temp_db):
        """改名（真移动）建链接；随后复制出的第 3、第 4 份是「复制」，既不覆盖链接也不新建"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        srcRel = "2019/misc/20190505_0001.jpg"
        movedRel = "2019/misc/AAA_moved.jpg"
        srcAbs = os.path.join(photos, srcRel.replace("/", os.sep))
        movedAbs = os.path.join(photos, movedRel.replace("/", os.sep))
        os.rename(srcAbs, movedAbs)

        _jobCode1, summary1 = _runAll(sched, photos, temp_db)
        assert summary1["counts"]["moved"] == 1 and summary1["counts"]["copied"] == 0
        assert summary1["counts"]["moveLinked"] == 1
        data = open(movedAbs, "rb").read()
        for name in ("2019/misc/BBB_copy.jpg", "2019/misc/CCC_copy.jpg"):
            with open(os.path.join(photos, name.replace("/", os.sep)), "wb") as handle:
                handle.write(data)

        _jobCode2, summary2 = _runAll(sched, photos, temp_db)
        assert summary2["counts"]["copied"] == 2        # 两份都是复制
        assert summary2["counts"]["moved"] == 0         # 没有新的移动
        assert summary2["counts"]["moveLinked"] == 0    # 复制不建链接

        rows = _byRelPath(_photoRows(temp_db))
        linked = [r for r in rows.values() if r["movedToPhotoCode"]]
        assert len(linked) == 1
        assert linked[0]["relPath"] == srcRel
        assert linked[0]["movedToPhotoCode"] == rows[movedRel]["photoCode"]
        for copyRel in ("2019/misc/BBB_copy.jpg", "2019/misc/CCC_copy.jpg"):
            assert rows[copyRel]["movedToPhotoCode"] is None
        # 全部关系一条都没丢：同内容记录都能通过 dupOfPhotoCode 追到主图
        byCode = {r["photoCode"]: r for r in rows.values()}
        for rel in (movedRel, "2019/misc/BBB_copy.jpg", "2019/misc/CCC_copy.jpg"):
            row = rows[rel]
            if row["dupOfPhotoCode"]:
                assert byCode[row["dupOfPhotoCode"]]["photoCode"]

    def test_copy_to_other_path_marked_duplicate(self, photos, temp_db, make_photo):
        """验收第 5 条：同一张图复制到另一个路径 -> isDuplicate=1 + dupOfPhotoCode 正确"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        before = _byRelPath(_photoRows(temp_db))
        srcRel = "2019/misc/20190505_0001.jpg"
        dstRel = "2024/copy_of_20190505_0001.jpg"
        src = os.path.join(photos, srcRel.replace("/", os.sep))
        dst = os.path.join(photos, dstRel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            fout.write(fin.read())

        _jobCode, summary = _runAll(sched, photos, temp_db)
        rows = _byRelPath(_photoRows(temp_db))
        assert summary["counts"]["duplicate"] == 1
        srcRow = rows[srcRel]
        dstRow = rows[dstRel]
        assert srcRow["fileHash"] == dstRow["fileHash"]
        assert dstRow["isDuplicate"] == 1
        # 主图是**先扫到的那条**：srcRel 字典序在 dstRel 之后，故主图是 dst 自己那条
        assert dstRow["dupOfPhotoCode"] in (srcRow["photoCode"], dstRow["photoCode"])
        assert rows[srcRel]["isDuplicate"] == 0
        assert len(rows) == 13

    def test_three_copies_all_but_one_are_duplicate(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        srcRel = "2019/misc/20190505_0001.jpg"
        src = os.path.join(photos, srcRel.replace("/", os.sep))
        for index in range(2):
            dst = os.path.join(photos, "copyA%d.jpg" % index)
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                fout.write(fin.read())

        _runAll(sched, photos, temp_db)
        rows = _photoRows(temp_db)
        sameHash = [r for r in rows if r["relPath"] in
                    ("copyA0.jpg", "copyA1.jpg", srcRel)]
        assert len(sameHash) == 3
        assert sum(r["isDuplicate"] for r in sameHash) == 2
        primary = {r["photoCode"] for r in sameHash if not r["isDuplicate"]}
        assert len(primary) == 1
        for row in sameHash:
            if row["isDuplicate"]:
                assert row["dupOfPhotoCode"] in primary

    def test_missing_dir_marked_not_deleted(self, photos, temp_db):
        """验收第 6 条：临时移走一个子目录 -> isMissing=1，**记录一条都没少**"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        assert len(_photoRows(temp_db)) == 12
        moved = os.path.join(photos, "2019", "wechat")
        stash = os.path.join(os.path.dirname(photos), "stash_wechat")
        os.rename(moved, stash)

        _jobCode, summary = _runAll(sched, photos, temp_db)
        rows = _byRelPath(_photoRows(temp_db))
        assert len(rows) == 12                       # 没删！
        assert summary["counts"]["missing"] == 1
        assert rows["2019/wechat/mmexport_1682908800000.jpg"]["isMissing"] == 1
        others = [r for r in rows.values() if not r["isMissing"]]
        assert len(others) == 11

        # 放回来 -> 复位
        os.rename(stash, moved)
        _jobCode2, summary2 = _runAll(sched, photos, temp_db)
        rows2 = _byRelPath(_photoRows(temp_db))
        assert summary2["counts"]["recovered"] >= 1
        assert rows2["2019/wechat/mmexport_1682908800000.jpg"]["isMissing"] == 0
        assert len(rows2) == 12

    def test_empty_root_does_not_mass_mark_missing(self, photos, temp_db):
        """安全阀：盘没插（根目录空）时绝不能把全库标成缺失"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        stash = os.path.join(os.path.dirname(photos), "stash_all")
        os.rename(photos, stash)
        os.makedirs(photos)

        _jobCode, summary = _runAll(sched, photos, temp_db)
        assert summary["counts"]["missing"] == 0
        assert all(row["isMissing"] == 0 for row in _photoRows(temp_db))
        os.rmdir(photos)
        os.rename(stash, photos)

    def test_content_change_updates_and_clears_metadata(self, photos, temp_db,
                                                        make_photo):
        """内容变更：刷元数据 + 人脸归零；且**旧元数据必须被清成 NULL**（forceColumns）"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        rel = "2024/2024-05-01/d0/IMG_00.jpg"
        path = os.path.join(photos, rel.replace("/", os.sep))
        before = _byRelPath(_photoRows(temp_db))[rel]
        assert before["shotYear"] == 2024
        assert before["cameraModel"] is not None
        assert before["faceCount"] == 0

        # 同一路径换成一张**无 EXIF** 的图：shotYear 应从 2024 变 NULL，而不是留 2024
        make_photo(path, color=(200, 10, 10))
        sqliteCommon.update_pb_photo("pb_photo", before["recID"], {"faceCount": 3})

        _jobCode, summary = _runAll(sched, photos, temp_db)
        rows = _byRelPath(_photoRows(temp_db))
        after = rows[rel]
        assert len(rows) == 12                        # 没新增行（是更新不是新增）
        assert after["photoCode"] == before["photoCode"]
        assert after["fileHash"] != before["fileHash"]
        assert after["shotYear"] == datetime_current_year_or_none(after)
        assert after["cameraModel"] is None           # 旧机型被清空
        assert after["takenAt"] is None
        assert after["faceCount"] == 0                # 人脸要重提取
        assert after["scannedYMDHMS"] is not None

    def test_relpath_verbatim_matches_disk(self, photos, temp_db):
        """验收第 9 条：库里 relPath 与磁盘实际路径逐字一致（分隔符统一为 /，其余不改）"""
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        for row in _photoRows(temp_db):
            expect = row["relPath"].replace("/", os.sep)
            assert os.path.isfile(os.path.join(photos, expect))
            assert row["relPathHash"] == walker.calcRelPathHash(row["relPath"])
        relSet = {row["relPath"] for row in _photoRows(temp_db)}
        assert "2023/misc/截图 20230202.png" in relSet          # 中文与空格原样
        assert "2019/wechat/mmexport_1682908800000.jpg" in relSet

    def test_screenshot_shot_year_is_null(self, photos, temp_db):
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, photos, temp_db)
        rows = _byRelPath(_photoRows(temp_db))
        assert rows["2023/misc/Screenshot_20240501_120000.png"]["shotYear"] is None
        assert rows["2023/misc/截图 20230202.png"]["shotYear"] is None
        assert rows["2019/misc/20190505_0001.jpg"]["shotYear"] == 2019
        assert rows["2019/wechat/mmexport_1682908800000.jpg"]["shotYear"] == 2023

    def test_excluded_and_non_photo_never_inserted(self, scan_root, temp_db, make_photo):
        root = str(scan_root)
        make_photo(os.path.join(root, "ok.jpg"))
        os.makedirs(os.path.join(root, "thumbs"), exist_ok=True)
        make_photo(os.path.join(root, "thumbs", "x.jpg"))
        os.makedirs(os.path.join(root, "@eaDir"), exist_ok=True)
        make_photo(os.path.join(root, "@eaDir", "y.jpg"))
        with open(os.path.join(root, "readme.txt"), "wb") as handle:
            handle.write(b"x")

        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        _runAll(sched, root, temp_db)
        relSet = {row["relPath"] for row in _photoRows(temp_db)}
        assert relSet == {"ok.jpg"}


def datetime_current_year_or_none(row):
    """内容变更后那张图没有 EXIF，年份应退到 mtime（即今年）而不是留旧值"""
    import time as _time
    return _time.localtime().tm_year


# ============================================================
# B2. 批次限流与断点续扫
# ============================================================

class TestBatchLimitAndResume:
    def _make20(self, root, make_photo, count=25):
        for index in range(count):
            make_photo(os.path.join(root, "b%02d" % (index % 4), "IMG_%02d.jpg" % index))
        return root

    def test_pause_at_batch_size_then_resume_to_done(self, scan_root, temp_db,
                                                      make_photo):
        """验收第 7 条：batchSize=10 -> 处理到 10 张自动 PAUSED，从 lastCursor 续扫到 DONE"""
        root = self._make20(str(scan_root), make_photo)      # 25 张 -> 10/10/5 三批
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=10)
        jobCode = job["jobCode"]
        assert job["totalCount"] == 25

        first = sched.runBatch(jobCode)
        assert first["ok"] is True
        assert first["paused"] is True and first["done"] is False
        assert first["result"]["processed"] == 10
        row1 = sched.getJob(jobCode)
        assert row1["jobStatus"] == comGD.JOB_PAUSED
        cursor1 = row1["lastCursor"]
        assert cursor1
        assert row1["processedCount"] == 10
        assert len(_photoRows(temp_db)) == 10

        # 续扫
        second = sched.runBatch(jobCode)
        assert second["paused"] is True
        assert second["result"]["processed"] == 10
        assert sched.getJob(jobCode)["lastCursor"] > cursor1
        assert len(_photoRows(temp_db)) == 20

        third = sched.runBatch(jobCode)
        assert third["done"] is True
        job3 = sched.getJob(jobCode)
        assert job3["jobStatus"] == comGD.JOB_DONE
        assert job3["processedCount"] == 25
        assert job3["addedCount"] == 25
        assert job3["batchIndex"] == 3
        assert job3["finishedYMDHMS"]
        assert len(_photoRows(temp_db)) == 25

    def test_batch_ending_on_last_file_goes_done_not_paused(self, scan_root,
                                                              temp_db, make_photo):
        """刚好在最后一张上撞到batchSize：直接 DONE，不多要用户点一次「继续」"""
        root = self._make20(str(scan_root), make_photo, count=20)
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=10)
        jobCode = job["jobCode"]
        assert sched.runBatch(jobCode)["paused"] is True
        last = sched.runBatch(jobCode)
        assert last["done"] is True and last["paused"] is False
        assert sched.getJob(jobCode)["jobStatus"] == comGD.JOB_DONE

    def test_resume_does_not_double_insert(self, scan_root, temp_db, make_photo):
        root = self._make20(str(scan_root), make_photo, count=20)
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=10)
        jobCode = job["jobCode"]
        sched.runBatch(jobCode)
        # 模拟「进程崩了，用户拿同一个 jobCode 重新续扫」：重复跑不产生新行
        for _ in range(3):
            sched.runBatch(jobCode)
        rows = _photoRows(temp_db)
        assert len(rows) == 20
        assert len({r["photoCode"] for r in rows}) == 20
        assert len({r["relPathHash"] for r in rows}) == 20

    def test_run_until_done_stops_at_max_batches(self, scan_root, temp_db, make_photo):
        root = self._make20(str(scan_root), make_photo, count=20)
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=10)
        summary = sched.runUntilDone(job["jobCode"], maxBatches=1)
        assert summary["done"] is False
        assert summary["batches"] == 1
        assert "maxBatches" in summary["errMsg"]
        assert sched.getJob(job["jobCode"])["jobStatus"] == comGD.JOB_PAUSED
        assert len(_photoRows(temp_db)) == 10

    def test_progress_updates_total_and_counts(self, scan_root, temp_db, make_photo):
        root = self._make20(str(scan_root), make_photo, count=20)
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=10)
        sched.runBatch(job["jobCode"])
        info = sched.progress(job["jobCode"])
        assert info["ok"] is True
        assert info["totalCount"] == 20
        assert info["processedCount"] == 10
        assert info["percent"] == 50.0
        assert info["jobStatus"] == comGD.JOB_PAUSED
        assert "50.0%" in sched.describe(job["jobCode"])["text"]

    def test_new_files_added_between_batches_are_picked_up(
            self, scan_root, temp_db, make_photo):
        """续扫期间的目录变动：游标**之后**新增的文件本轮就能扫到；
        游标**之前**新增的本轮扫不到（lastCursor 是一道水位线），
        但下一轮全量扫描一定会补上 —— 两种情况都不丢数据。"""
        root = self._make20(str(scan_root), make_photo)
        sched = scanScheduler.ScanScheduler(dbFile=temp_db)
        job = sched.createJob(root=root, batchSize=10)
        jobCode = job["jobCode"]
        sched.runBatch(jobCode)

        make_photo(os.path.join(root, "b00", "AAA_late.jpg"))     # 字典序在游标之前
        make_photo(os.path.join(root, "z9", "ZZZ_late.jpg"))       # 字典序在游标之后
        summary = sched.runUntilDone(jobCode)
        assert summary["done"] is True
        relSet = {row["relPath"] for row in _photoRows(temp_db)}
        assert "z9/ZZZ_late.jpg" in relSet                # 游标之后的，本轮就扫到
        assert "b00/AAA_late.jpg" not in relSet           # 游标之前的，本轮扫不到
        assert len(relSet) == 26

        # 下一轮全量扫描把它补上（数据不丢，只是晚一轮）
        _jobCode2, summary2 = _runAll(sched, root, temp_db)
        relSet2 = {row["relPath"] for row in _photoRows(temp_db)}
        assert "b00/AAA_late.jpg" in relSet2
        assert len(relSet2) == 27
        assert summary2["counts"]["added"] == 1
        assert summary2["counts"]["skipped"] == 26
