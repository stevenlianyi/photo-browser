#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_config.py
#Description: config 层单测 —— basicSettings / sqliteSettings / globalDefinition / miscCommon
#
# 这些是全项目的「单一事实源」，常量被改动等于改动业务语义，
# 因此在步骤 1 就把关键值钉住，避免后续步骤无声漂移。

import os

import pytest

from common import globalDefinition as gd
from config import basicSettings as bs
from config import sqliteSettings as ss


# ============================================================
# basicSettings
# ============================================================

class TestBasicSettings:
    def test_batch_size(self):
        """批次限流默认 100 张（数据库设计 §4.8 / 开发计划步骤 3）"""
        assert bs.BATCH_SIZE == 100

    def test_thresholds(self):
        """S0 实测起点 T_HIGH=0.55 / T_LOW=0.35（勿信默认 0.65）"""
        assert bs.T_HIGH == 0.55
        assert bs.T_LOW == 0.35
        assert bs.T_LOW < bs.T_HIGH, "T_LOW 必须小于 T_HIGH，否则灰区为空"

    def test_quality_filter(self):
        """质量过滤：detScore>=0.6 / 短边>=64px / |yaw|<=45"""
        assert bs.MIN_DET_SCORE == 0.6
        assert bs.MIN_FACE_EDGE == 64
        assert bs.MAX_YAW == 45

    def test_hash_chunk_size_is_8mb(self):
        """文件 hash 分块 8MB，绝不整读原图"""
        assert bs.HASH_CHUNK_SIZE == 8 * 1024 * 1024
        assert bs.HASH_CHUNK_SIZE == 8388608

    def test_embedding_layout(self):
        """float32[512] 小端 = 2048 字节（pb_face.embedding MEDIUMBLOB）"""
        assert bs.EMBEDDING_DIM == 512
        assert bs.EMBEDDING_BYTES == 2048

    def test_photo_ext_whitelist(self):
        """扩展名白名单覆盖 spec 列出的全部照片类型，且全部小写含点、无重复"""
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp",
                    ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw", ".arf"):
            assert ext in bs.PHOTO_EXT_SET, "白名单缺少 %s" % ext
        for ext in bs.PHOTO_EXTS:
            assert ext == ext.lower(), "扩展名必须小写: %s" % ext
            assert ext.startswith("."), "扩展名必须带点: %s" % ext
        assert len(bs.PHOTO_EXT_SET) == len(bs.PHOTO_EXTS), "白名单有重复项"

    @pytest.mark.parametrize("ext", [".wmv", ".mp4", ".mov", ".m4v", ".avi", ".mkv"])
    def test_video_exts_excluded(self, ext):
        """本项目只做静态照片，视频不进白名单（否则步骤 4 解码必然炸）"""
        assert ext not in bs.PHOTO_EXT_SET, "%s 不应出现在照片白名单里" % ext
        assert bs.is_photo_file("holiday" + ext) is False

    @pytest.mark.parametrize("name,expected", [
        ("a.jpg", True),
        ("a.JPG", True),
        ("a.Jpeg", True),
        ("2024/2024-05-01/IMG_0001.CR2", True),
        ("notes.txt", False),
        ("noext", False),
        ("", False),
        (".jpg", True),
    ])
    def test_is_photo_file(self, name, expected):
        assert bs.is_photo_file(name) is expected

    def test_example_template_exists_and_matches_defaults(self):
        """.example 必须入库，且兜底默认值与模板一致"""
        import config as cfg_pkg
        example = os.path.join(os.path.dirname(cfg_pkg.__file__),
                               "local_settings.py.example")
        assert os.path.isfile(example), "缺少 local_settings.py.example 模板"
        assert bs.DEFAULT_PHOTO_ROOT == r"d:\PhotoLib"
        assert bs.DEFAULT_THUMB_ROOT == ""
        assert bs.DEFAULT_DB_FILE == ""

    def test_server_binds_loopback_only(self):
        """只绑 127.0.0.1，绝不 0.0.0.0"""
        assert bs.SERVER_HOST == "127.0.0.1"
        assert bs.SERVER_HOST != "0.0.0.0"


# ============================================================
# sqliteSettings
# ============================================================

class TestSqliteSettings:
    EXPECTED = {
        "journal_mode": "WAL",
        "foreign_keys": "ON",
        "synchronous": "NORMAL",
        "busy_timeout": 5000,
        "temp_store": "MEMORY",
        "cache_size": -64000,
    }

    def test_pragma_list_complete(self):
        """读写连接共用的那套 PRAGMA（开发计划 §6.2），一项都不能少"""
        assert ss.pragmaDict() == self.EXPECTED

    def test_pragma_statements_render(self):
        stmts = ss.pragmaStatements()
        assert len(stmts) == len(self.EXPECTED)
        assert "PRAGMA journal_mode = WAL;" in stmts
        assert "PRAGMA busy_timeout = 5000;" in stmts
        assert "PRAGMA cache_size = -64000;" in stmts

    def test_db_file_name(self):
        assert ss.DB_FILE_NAME == "photolib.db"

    def test_assemble_derives_paths(self, photo_root_of):
        """库文件装配：db / -wal / -shm 三个路径"""
        files = ss.assembleDbFile()
        expect = os.path.join(str(photo_root_of), "db", "photolib.db")
        assert files.dbFile == expect
        assert files.walFile == expect + "-wal"
        assert files.shmFile == expect + "-shm"
        assert files.dbDir == os.path.join(str(photo_root_of), "db")

    def test_assemble_accepts_explicit_path(self, tmp_path):
        target = str(tmp_path / "state" / "custom.db")
        assert ss.assembleDbFile(target).dbFile == target

    def test_assemble_does_not_touch_disk(self, photo_root_of):
        """装配只算路径：不得 mkdir、不得建库文件（硬约束）"""
        files = ss.assembleDbFile()
        assert not os.path.exists(files.dbFile)
        assert not os.path.isdir(files.dbDir)

    def test_exists_reports_false_before_build(self, photo_root_of):
        assert ss.assembleDbFile().exists() is False

    def test_build_database_is_step2_stub(self, photo_root_of):
        """建库属步骤 2，本步必须显式抛错而不是悄悄什么都不做"""
        with pytest.raises(NotImplementedError, match="步骤 2"):
            ss.buildDatabase()

    def test_placeholder_convention(self):
        """占位符统一 %s，运行层内部转 ?（防注入）"""
        assert ss.SQL_PLACEHOLDER == "%s"
        assert ss.SQLITE_PLACEHOLDER == "?"


# ============================================================
# globalDefinition
# ============================================================

class TestGlobalDefinition:
    def test_ret_ok(self):
        assert gd.RET_OK == 0
        assert gd.isOK(gd.RET_OK) is True
        assert gd.isOK(gd.ERR_DB_ERROR) is False

    def test_all_error_codes_have_text(self):
        """每个登记的返回码都要有中文提示，API 层直接取，禁止各写一份"""
        assert [c for c in gd.ERROR_TEXT if not gd.errText(c)] == []
        assert gd.errText(999999).startswith("未知错误")

    def test_error_codes_unique_by_value(self):
        """同码不同义是排查噩梦"""
        assert len(set(gd.ERROR_TEXT)) == len(gd.ERROR_TEXT)

    def test_error_code_ranges(self):
        """分段：1xxx 通用/路径、2xxx 存储、3xxx 文件、4xxx 图像、5xxx 任务"""
        assert 1000 <= gd.ERR_PATH_LAYOUT_INVALID < 2000
        assert 2000 <= gd.ERR_DB_LOCKED < 3000
        assert 3000 <= gd.ERR_FILE_READONLY_VIOLATION < 4000
        assert 4000 <= gd.ERR_FACE_LOW_QUALITY < 5000
        assert 5000 <= gd.ERR_TASK_STATE_ILLEGAL < 6000

    def test_job_status_values_match_db(self):
        """pb_scan_job.jobStatus 的取值必须是这五个字符串"""
        assert gd.JOB_STATUS_ALL == ("IDLE", "RUNNING", "PAUSED", "DONE", "FAILED")

    @pytest.mark.parametrize("src,dst,ok", [
        (gd.JOB_IDLE, gd.JOB_RUNNING, True),
        (gd.JOB_IDLE, gd.JOB_DONE, False),
        (gd.JOB_RUNNING, gd.JOB_PAUSED, True),
        (gd.JOB_RUNNING, gd.JOB_DONE, True),
        (gd.JOB_RUNNING, gd.JOB_FAILED, True),
        (gd.JOB_PAUSED, gd.JOB_RUNNING, True),
        (gd.JOB_PAUSED, gd.JOB_DONE, False),
        (gd.JOB_DONE, gd.JOB_RUNNING, False),
        (gd.JOB_FAILED, gd.JOB_IDLE, True),
        ("NOT_A_STATUS", gd.JOB_RUNNING, False),
    ])
    def test_job_status_transitions(self, src, dst, ok):
        assert gd.canTransit(src, dst) is ok

    def test_job_final_status(self):
        assert gd.JOB_DONE in gd.JOB_STATUS_FINAL
        assert gd.JOB_FAILED not in gd.JOB_STATUS_FINAL
        assert gd.isFinalStatus(gd.JOB_DONE) is True
        assert gd.isFinalStatus(gd.JOB_PAUSED) is False

    def test_job_status_text_complete(self):
        for s in gd.JOB_STATUS_ALL:
            assert gd.JOB_STATUS_TEXT.get(s)

    def test_scan_state_values_match_db(self):
        """pb_photo.scanState: 0 待扫描 / 1 已入人脸库 / 2 待人工确认 / 3 完成"""
        assert gd.SCAN_STATE_ALL == (0, 1, 2, 3)
        assert gd.SCAN_STATE_PENDING == 0
        assert gd.SCAN_STATE_FACED == 1
        assert gd.SCAN_STATE_REVIEW == 2
        assert gd.SCAN_STATE_DONE == 3

    def test_scan_state_text_complete(self):
        for s in gd.SCAN_STATE_ALL:
            assert gd.SCAN_STATE_TEXT.get(s)

    def test_soft_delete_flags(self):
        assert gd.DEL_FLAG_NO == "0"
        assert gd.DEL_FLAG_YES == "1"
        assert gd.DEL_FLAG_ALL == ("0", "1")

    def test_source_flags(self):
        assert gd.LINK_SOURCE_AUTO == 0
        assert gd.LINK_SOURCE_MANUAL == 1
        assert gd.PERSON_SOURCE_ALL == (0, 1, 2)


# ============================================================
# miscCommon：时间 / 字符串 / 字节
# ============================================================

class TestMiscCommon:
    def test_gettime_is_14_digits(self):
        """库中 regYMDHMS 统一 YYYYMMDDHHMMSS"""
        from common import miscCommon as mc
        s = mc.getTime()
        assert len(s) == 14 and s.isdigit()

    def test_iso8601_utc_shape(self):
        from common import miscCommon as mc
        s = mc.getISO8601UTC()
        assert s.endswith("Z") and len(s) == 20
        assert s[4] == "-" and s[10] == "T"

    def test_ymdhms_roundtrip(self):
        from common import miscCommon as mc
        raw = "20261004153045"
        assert mc.ymdHMS2human(raw) == "2026-10-04 15:30:45"
        assert mc.human2ymdHMS("2026-10-04 15:30:45") == raw
        assert mc.human2ymdHMS("2026-10-04T15:30:45") == raw
        assert mc.ymdHMS2human("bad") == "bad"

    def test_byte_helpers(self):
        from common import miscCommon as mc
        assert mc.bytes2hex(b"\x00\xff") == "00ff"
        assert mc.hex2bytes("00FF") == b"\x00\xff"
        assert mc.toStr(b"hi") == "hi"
        assert mc.toBytes("hi") == b"hi"

    def test_sha256hex_known_vector(self):
        from common import miscCommon as mc
        assert mc.sha256Hex("abc") == \
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    def test_file_hash_chunked_matches_sha256_of_whole(self, tmp_path):
        """分块流式 hash 与整读算出的 sha256 必须一致（分块只是不整读）"""
        import hashlib
        from common import miscCommon as mc
        data = os.urandom(20 * 1024 * 1024)      # 20MB > 单块 8MB，确保跨块
        f = tmp_path / "big.bin"
        f.write_bytes(data)
        assert mc.fileHashHex(str(f), chunkSize=8 * 1024 * 1024) == \
            hashlib.sha256(data).hexdigest()

    def test_null_empty_trim(self):
        from common import miscCommon as mc
        assert mc.isNull(None) and mc.isNull("  ") and not mc.isNull("a")
        assert mc.isEmpty([]) and mc.isEmpty("") and not mc.isEmpty([1])
        assert mc.trim("  a  ") == "a"

    def test_human_size(self):
        from common import miscCommon as mc
        assert mc.humanSize(512) == "512B"
        assert mc.humanSize(1234567) == "1.2MB"
