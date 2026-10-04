#! /usr/bin/env python3
#encoding: utf-8

# Filename: test_build_db_migrate.py
# Description: tools/build_db.py migrate() 单测 —— 老库补列/ 补索引，且**不动数据**
#
# 为什么值得单测：--migrate 是「改表之后唯一不丢数据的路子」，
# 一旦它误删数据或把 NOT NULL 列加坏，损失不可逆。
#
# 覆盖：
#   A. 新建库上跑 migrate是空跑（幂等）
#   B. 老库缺列 -> 补上，**原有数据一行不少、值不变**
#   C. 缺索引 -> 补建
#   D. 类型不一致 -> 只报告不动手（SQLite 改不了类型）
#   E. NOT NULL 无默认值的列在非空表上被拒绝且报告清楚
#   F. 重复跑 migrate 结果一致（幂等）
#
# 硬约束：只在 tmp 临时库上跑。

import os

import pytest

from common import globalDefinition as comGD
from database.auto_generated import sqliteCommon as sqliteCommon
from tools import build_db as build_db


@pytest.fixture
def dbFile(tmp_path, set_photo_root):
    root = tmp_path / "PhotoLib"
    root.mkdir()
    set_photo_root(str(root))
    target = str(tmp_path / "state" / "m.db")
    build_db.build(dbFile=target, verbose=False)
    try:
        yield target
    finally:
        sqliteCommon.closeDb()


def _seedPhotoRows(count=3):
    """塞几条真实形态的 pb_photo（软删/未软删各一条），用来验数据没被动过"""
    rows = []
    for index in range(count):
        rows.append({
            "photoCode": "PH_M_%03d" % index,
            "relPath": "2024/x%03d.jpg" % index,
            "relPathHash": "h%063d" % index,
            "fileHash": "f%063d" % index,
            "fileSize": 1000 + index,
            "shotYear": 2020 + (index % 5),
        })
    sqliteCommon.insertManyTableGeneral("pb_photo", rows, fillStandard=True)
    return rows


class TestMigrateNoop:
    def test_fresh_db_is_noop(self, dbFile):
        result = build_db.migrate(dbFile=dbFile, verbose=False)
        assert result["created"] == []
        assert result["columnsAdded"] == []
        assert result["indexesAdded"] == []
        assert result["mismatch"] == []
        assert result["failed"] == []

    def test_idempotent(self, dbFile):
        first = build_db.migrate(dbFile=dbFile, verbose=False)
        second = build_db.migrate(dbFile=dbFile, verbose=False)
        assert first["columnsAdded"] == second["columnsAdded"] == []
        assert second["failed"] == []


class TestMigrateAddColumn:
    def _dropColumn(self, tableName, columnName):
        """模拟「老库没有这一列」。SQLite 3.35+ 支持 DROP COLUMN"""
        db = sqliteCommon.dbHandle()
        assert db.executeWrite("ALTER TABLE %s DROP COLUMN %s;" % (tableName, columnName)) >= 0

    def test_adds_missing_column_and_keeps_data(self, dbFile):
        _seedPhotoRows(3)
        self._dropColumn("pb_photo", "movedToPhotoCode")
        assert "movedToPhotoCode" not in [
            c["name"] for c in sqliteCommon.tableInfo("pb_photo")]

        result = build_db.migrate(dbFile=dbFile, verbose=False)
        added = [(t, c) for t, c, _m in result["columnsAdded"]]
        assert added == [("pb_photo", "movedToPhotoCode")]
        assert result["failed"] == []

        cols = [c["name"] for c in sqliteCommon.tableInfo("pb_photo")]
        assert "movedToPhotoCode" in cols
        rows = sqliteCommon.query_pb_photo("pb_photo", mode="light", delFlag="*")
        assert len(rows) == 3# 数据一行没少
        assert [r["photoCode"] for r in rows] == ["PH_M_000", "PH_M_001", "PH_M_002"]
        assert rows[0]["fileSize"] == 1000
        assert rows[2]["shotYear"] == 2022         # 2020 + (2 % 5)
        # 新列在老行上是 NULL（而不是被塞了默认值）
        assert all(r["movedToPhotoCode"] is None for r in rows)

    def test_not_null_without_default_is_refused(self, dbFile):
        """NOT NULL 且无默认值的列在非空表上必然被 SQLite 拒绝 —— 必须报告而不是崩"""
        _seedPhotoRows(2)
        self._dropColumn("pb_photo", "relPath")
        result = build_db.migrate(dbFile=dbFile, verbose=False)
        assert result["failed"], "NOT NULL 无默认值的列应当报告失败"
        assert any("relPath" in msg for _t, msg in result["failed"])
        # 表没被搞坏，数据还在
        assert sqliteCommon.countTableGeneral("pb_photo", delFlag="*") == 2

    def test_add_column_is_idempotent_on_second_run(self, dbFile):
        _seedPhotoRows(1)
        self._dropColumn("pb_photo", "movedToPhotoCode")
        build_db.migrate(dbFile=dbFile, verbose=False)
        second = build_db.migrate(dbFile=dbFile, verbose=False)
        assert second["columnsAdded"] == []
        assert second["failed"] == []
        assert sqliteCommon.countTableGeneral("pb_photo", delFlag="*") == 1

    def test_column_def_only_accepts_whitelist(self, dbFile):
        assert sqliteCommon.columnDefOf("pb_photo", "shotYear") == "shotYear INTEGER"
        assert sqliteCommon.columnDefOf("pb_photo", "isMissing") == \
            "isMissing INTEGER NOT NULL DEFAULT 0"
        assert sqliteCommon.columnDefOf("pb_photo", "no_such_col") is None
        assert sqliteCommon.columnDefOf("no_such_table", "shotYear") is None
        ok, msg = sqliteCommon.addColumnGeneral("pb_photo", "no_such_col")
        assert ok is False and "不在表定义里" in msg


class TestMigrateIndex:
    def test_recreates_dropped_index(self, dbFile):
        db = sqliteCommon.dbHandle()
        assert db.executeWrite("DROP INDEX idx_pb_photo_fileHash;") >= 0
        assert sqliteCommon.chkIndexExist("idx_pb_photo_fileHash") is False
        result = build_db.migrate(dbFile=dbFile, verbose=False)
        assert "idx_pb_photo_fileHash" in result["indexesAdded"]
        assert sqliteCommon.chkIndexExist("idx_pb_photo_fileHash") is True

    def test_no_duplicate_indexes(self, dbFile):
        build_db.migrate(dbFile=dbFile, verbose=False)
        result = build_db.migrate(dbFile=dbFile, verbose=False)
        assert result["indexesAdded"] == []
        db = sqliteCommon.dbHandle()
        db.executeRead("SELECT name FROM sqlite_master WHERE type = 'index' "
                       "AND tbl_name = 'pb_photo';")
        names = [row["name"] for row in db.fetchAll()]
        assert len(names) == len(set(names))


class TestMigrateCreateMissingTable:
    def test_creates_absent_table(self, dbFile):
        sqliteCommon.dropTableGeneral("pb_scan_job")
        assert sqliteCommon.chkTableExist("pb_scan_job") is False
        result = build_db.migrate(dbFile=dbFile, verbose=False)
        assert "pb_scan_job" in result["created"]
        assert sqliteCommon.chkTableExist("pb_scan_job") is True
        # 新建的表必须列全（不需要再补列）
        cols = [c["name"] for c in sqliteCommon.tableInfo("pb_scan_job")]
        want = [c["name"] for c in sqliteCommon.TABLE_COLUMNS["pb_scan_job"]]
        assert cols == want


class TestMigrateTypeMismatch:
    def test_type_mismatch_reported_not_fixed(self, dbFile):
        """SQLite 改不了列类型：只报告，让人决定要不要 --drop"""
        db = sqliteCommon.dbHandle()
        assert db.executeWrite(
            "ALTER TABLE pb_photo ADD COLUMN legacyFlag VARCHAR(8) NULL;") >= 0
        result = build_db.migrate(dbFile=dbFile, verbose=False)
        # 库里有、.txt 里没有的列：只在 mismatch / 报告里体现，绝不删除
        assert sqliteCommon.chkTableExist("pb_photo") is True
        cols = [c["name"] for c in sqliteCommon.tableInfo("pb_photo")]
        assert "legacyFlag" in cols, "migrate 绝不能删列"
        assert result["failed"] == []

    def test_soft_deleted_rows_untouched(self, dbFile):
        _seedPhotoRows(2)
        rows = sqliteCommon.query_pb_photo("pb_photo", delFlag="*")
        sqliteCommon.delete_pb_photo("pb_photo", rows[0]["recID"])       # 软删
        self_check = sqliteCommon.query_pb_photo("pb_photo", delFlag="*")
        assert len(self_check) == 2
        assert len(sqliteCommon.query_pb_photo("pb_photo")) == 1

        self._drop = build_db.migrate(dbFile=dbFile, verbose=False)
        assert len(sqliteCommon.query_pb_photo("pb_photo", delFlag="*")) == 2
        assert len(sqliteCommon.query_pb_photo("pb_photo")) == 1
        assert comGD.DEL_FLAG_YES == "1"
