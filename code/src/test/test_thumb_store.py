#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_thumb_store.py
#Description: 步骤 4 单测 —— 缩略图/人脸图的**落盘规则**（路径可推导 / 原子写 / 分桶目录）
#
# 为什么这些必须写死成单测
# ----------------------
#   1. **路径可推导**是全项目唯一的真相来源：pb_photo 没有 thumbPath 字段，
#      缩略图路径完全由 fileHash + size 算出来。改了 thumb_relpath 的形状，
#      磁盘上十万张老缩略图会集体变成孤儿，而且**库里没有任何字段能找回它们**
#      —— 这类破坏是不可逆的，必须有测试钉住。
#   2. **原子写**的正确性无法靠"看代码觉得对"：真正要验的是
#      「os.replace 抛异常时磁盘上不留 .tmp」这种反例，
#      只能靠 monkeypatch 把 os.replace 打崩来构造。
#   3. **分桶目录**要验的是「按需创建 + 幂等 + 单桶不超千级」的前两条，
#      第三条是规模问题，验收时用真实测试集统计。
#
# 全部测试都跑在 pytest 的 tmp 临时根目录下，**绝不在真实 d:\PhotoLib 写任何东西**。

import os

import pytest

from common import paths as paths
from config import basicSettings as basicSettings
from processor.media import thumbStore as thumbStore


_HASH = "ab12cd34" + "0" * 56
_FACE = "f0a1b2c3" + "0" * 56


# ============================================================
# 一、路径可推导性
# ============================================================

class TestRelPathDerivable:
    """路径必须**只由 fileHash + size 推出来**，且形状定死"""

    @pytest.mark.parametrize("size", (200, 400, 800))
    def test_shape(self, size):
        got = thumbStore.thumb_relpath(_HASH, size)
        assert got == "thumbs/ab/%s_%d.webp" % (_HASH, size)

    def test_default_size_is_400(self):
        assert thumbStore.THUMB_DEFAULT_SIZE == 400
        assert thumbStore.thumb_relpath(_HASH) == thumbStore.thumb_relpath(_HASH, 400)

    def test_deterministic(self):
        """同样输入永远同样输出 —— 这是"不入库也能找回来"的前提"""
        assert thumbStore.thumb_relpath(_HASH, 400) == thumbStore.thumb_relpath(_HASH, 400)

    def test_sizes_are_distinct(self):
        """三个尺寸是三个不同文件；ETag 里必须带 size 正是这个原因"""
        rels = {thumbStore.thumb_relpath(_HASH, s) for s in thumbStore.THUMB_SIZES}
        assert len(rels) == len(thumbStore.THUMB_SIZES) == 3

    def test_hash_changes_path(self):
        other = "ff" + _HASH[2:]
        assert thumbStore.thumb_relpath(other, 400) != thumbStore.thumb_relpath(_HASH, 400)

    def test_posix_separators(self):
        """相对路径统一正斜杠：入库/打日志/跨平台都用这个形态"""
        assert "\\" not in thumbStore.thumb_relpath(_HASH, 400)

    @pytest.mark.parametrize("bad", ("", None, "   ",
                                     "../escape", "ab/cd", "ab\\cd", r"c:\x",
                                     "ZZZZ", "ab 12", "..", "./ab"))
    def test_reject_non_hex(self, bad):
        """非十六进制一律拒绝 —— 这是目录穿越的唯一防线

        编码若能混进 ".." / "/" /盘符，拼出来的路径就能逃出 thumbRoot。
        """
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.thumb_relpath(bad, 400)

    def test_reject_too_long(self):
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.thumb_relpath("a" * (thumbStore.CODE_MAX_LEN + 1), 400)

    def test_normalize_lowercase(self):
        """统一小写：Windows 上路径本就不敏感，但 ETag / 比对需要一致形态"""
        assert thumbStore.thumb_relpath(_HASH.upper(), 400) == \
            thumbStore.thumb_relpath(_HASH, 400)

    @pytest.mark.parametrize("size", (100, 300, 401, 0, -200, 1600))
    def test_reject_unknown_size(self, size):
        """非法 size **不静默回落 400** —— 前端传错要立刻知道"""
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.thumb_relpath(_HASH, size)

    def test_bucket_of(self):
        assert thumbStore.bucketOf(_HASH) == "ab"
        assert thumbStore.bucketOf(_FACE) == "f0"
        # 取前 2 位十六进制 = 256 个桶，10 万张平均每桶 400 个（验收第 5 条盯上限）
        assert thumbStore.HASH_BUCKET_LEN == 2
        assert all(len(thumbStore.bucketOf("%02x" % v + _HASH[2:])) == 2 for v in range(256))

    def test_face_relpath(self):
        assert thumbStore.face_relpath(_FACE) == "faces/f0/%s.jpg" % _FACE
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.face_relpath("../x")

    def test_etag_includes_size(self):
        assert thumbStore.thumb_etag(_HASH, 200) != thumbStore.thumb_etag(_HASH, 400)
        assert "_400_" in thumbStore.thumb_etag(_HASH, 400)
        assert _FACE in thumbStore.face_etag(_FACE)

    def test_etag_includes_encoder_settings(self, monkeypatch):
        """**改了编码参数，ETag 必须跟着变**

        缩略图是磁盘上的旧文件：URL 没变、fileHash 也没变。
        若 ETag 里不含编码参数，改了 THUMB_QUALITY 重新生成之后，
        浏览器会一直拿304 把旧图供下去 —— **怎么刷都刷不出来**。
        """
        before = thumbStore.thumb_etag(_HASH, 400)
        monkeypatch.setattr(basicSettings, "THUMB_QUALITY",
                            basicSettings.THUMB_QUALITY + 1)
        after = thumbStore.thumb_etag(_HASH, 400)
        assert before != after
        assert str(basicSettings.THUMB_QUALITY) in after

    def test_etag_changes_with_webp_method(self, monkeypatch):
        before = thumbStore.thumb_etag(_HASH, 400)
        monkeypatch.setattr(basicSettings, "THUMB_WEBP_METHOD",
                            basicSettings.THUMB_WEBP_METHOD + 1)
        assert thumbStore.thumb_etag(_HASH, 400) != before

    def test_face_etag_includes_size_and_shape(self, monkeypatch):
        """人脸图：尺寸、方形/等比、JPEG 质量都进 ETag"""
        before = thumbStore.face_etag(_FACE)
        monkeypatch.setattr(basicSettings, "FACE_CROP_SIZE",
                            basicSettings.FACE_CROP_SIZE + 1)
        assert thumbStore.face_etag(_FACE) != before
        monkeypatch.setattr(basicSettings, "FACE_CROP_SQUARE", True)
        squareTag = thumbStore.face_etag(_FACE)
        monkeypatch.setattr(basicSettings, "FACE_CROP_SQUARE", False)
        assert thumbStore.face_etag(_FACE) != squareTag

    def test_etag_is_stable_within_a_config(self):
        """同一配置下重复调用必须一致（否则每次请求都是新 URL，缓存全废）"""
        assert thumbStore.thumb_etag(_HASH, 400) == thumbStore.thumb_etag(_HASH, 400)
        assert thumbStore.thumb_variant() == thumbStore.thumb_variant()
        assert thumbStore.face_variant() == thumbStore.face_variant()

    def test_etag_fits_http_header(self):
        """sha256(64) + 分隔 + 数字 + 变体串，HTTP 头里放得下，且不含引号/逗号"""
        tag = thumbStore.thumb_etag(_HASH, 800)
        assert len(tag) < 128
        assert not set(tag) & set('", \r\n')
        assert thumbStore.face_etag(_FACE).count("_") >= 1


class TestAbsPath:
    def test_thumb_abspath_under_thumb_root(self, scan_root):
        thumbRoot = str(scan_root.parent / "thumb")
        got = thumbStore.thumb_abspath(_HASH, 400)
        assert got.startswith(thumbRoot)
        assert os.path.basename(got) == "%s_400.webp" % _HASH
        assert os.path.basename(os.path.dirname(got)) == "ab"

    def test_face_abspath_under_thumb_root(self, scan_root):
        thumbRoot = str(scan_root.parent / "thumb")
        got = thumbStore.face_abspath(_FACE)
        assert got.startswith(thumbRoot)
        assert got.endswith(os.path.join("faces", "f0", "%s.jpg" % _FACE))

    def test_thumb_root_override(self, scan_root):
        """显式 thumbRoot（测试与临时库实验用）"""
        alt = str(scan_root.parent / "alt_thumb")
        got = thumbStore.thumb_abspath(_HASH, 800, thumbRoot=alt)
        assert got.startswith(alt)


class TestOrigAbsPath:
    """由 relPath 还原原图绝对路径 —— 边界校验是 /api/original 的安全底座"""

    def test_round_trip(self, scan_root):
        got = thumbStore.orig_abs_path("2024/2024-05-01/IMG_0001.jpg")
        assert got == os.path.join(str(scan_root), "2024", "2024-05-01", "IMG_0001.jpg")

    def test_accept_backslash(self, scan_root):
        """库里 relPath 分隔符可能是反斜杠（Windows 常见）"""
        assert thumbStore.orig_abs_path("a\\b\\c.jpg") == \
            thumbStore.orig_abs_path("a/b/c.jpg")

    @pytest.mark.parametrize("bad", ("../../secret.jpg", "..\\..\\secret.jpg",
                                     "/etc/passwd", "C:/Windows/win.ini", "", "   ", None))
    def test_reject_escape(self, bad):
        """逃出照片根 / 绝对路径 / 盘符 —— 一律拒绝

        库里若出现这类脏数据（正常流程不会产生），必须在这里被挡住，
        而不是让 /api/original 把 photo 目录外的文件当原图吐出去。
        """
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.orig_abs_path(bad)

    def test_photo_root_override(self, scan_root):
        other = str(scan_root.parent / "other_photo")
        assert thumbStore.orig_abs_path("x.jpg", photoRoot=other) == \
            os.path.join(other, "x.jpg")


# ============================================================
# 二、分桶目录
# ============================================================

class TestBucketDir:
    def test_creates_nested_dir(self, scan_root):
        relpath = thumbStore.thumb_relpath(_HASH, 400)
        dirPath = thumbStore.ensure_bucket_dir(relpath)
        assert os.path.isdir(dirPath)
        assert dirPath.endswith(os.path.join("thumbs", "ab"))

    def test_idempotent(self, scan_root):
        relpath = thumbStore.thumb_relpath(_HASH, 400)
        first = thumbStore.ensure_bucket_dir(relpath)
        second = thumbStore.ensure_bucket_dir(relpath)
        assert first == second

    def test_face_bucket_dir(self, scan_root):
        dirPath = thumbStore.ensure_bucket_dir(thumbStore.face_relpath(_FACE))
        assert dirPath.endswith(os.path.join("faces", "f0"))

    def test_list_bucket_dirs(self, scan_root):
        for size in thumbStore.THUMB_SIZES:
            thumbStore.ensure_bucket_dir(thumbStore.thumb_relpath(_HASH, size))
        thumbStore.ensure_bucket_dir(thumbStore.face_relpath(_FACE))
        assert thumbStore.listBucketDirs("thumbs") == ["ab"]
        assert thumbStore.listBucketDirs("faces") == ["f0"]

    def test_list_bucket_dirs_missing_root(self, scan_root):
        """目录还没建时返回空列表，不抛异常"""
        assert thumbStore.listBucketDirs("thumbs") == []


# ============================================================
# 三、原子写
# ============================================================

class TestWriteAtomic:
    def test_creates_file(self, scan_root):
        target = thumbStore.thumb_abspath(_HASH, 400)
        assert thumbStore.write_atomic(target, b"webp-bytes") == target
        assert thumbStore.read_bytes(target) == b"webp-bytes"

    def test_creates_bucket_dir_automatically(self, scan_root):
        """write_atomic 自带建目录 —— 批量生成时不该每张都担心目录不存在"""
        target = thumbStore.thumb_abspath(_HASH, 400)
        assert not os.path.isdir(os.path.dirname(target))
        thumbStore.write_atomic(target, b"x")
        assert os.path.isdir(os.path.dirname(target))

    def test_no_tmp_left_on_success(self, scan_root):
        target = thumbStore.thumb_abspath(_HASH, 400)
        thumbStore.write_atomic(target, b"x")
        assert thumbStore.findTmpFiles() == []
        assert os.listdir(os.path.dirname(target)) == ["%s_400.webp" % _HASH]

    def test_overwrite_is_atomic(self, scan_root):
        """同名覆盖：os.replace 语义 —— 读者只会看到完整旧值或完整新值"""
        target = thumbStore.thumb_abspath(_HASH, 400)
        thumbStore.write_atomic(target, b"old-value-long")
        thumbStore.write_atomic(target, b"new")
        assert thumbStore.read_bytes(target) == b"new"
        assert thumbStore.findTmpFiles() == []

    def test_cleans_tmp_when_replace_fails(self, scan_root, monkeypatch):
        """**核心反例**：os.replace 抛异常时，磁盘上不许留下 *.tmp

        半文件是缩略图最危险的失败模式：前端会把它当缩略图读进去，
        界面上是一张裂图，事后还分不清是生成坏了还是传输坏了。
        """
        target = thumbStore.thumb_abspath(_HASH, 400)

        def boom(src, dst):
            raise OSError("磁盘满/杀软拦截（构造的失败）")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            thumbStore.write_atomic(target, b"half-written")
        monkeypatch.undo()

        assert not os.path.exists(target)
        assert thumbStore.findTmpFiles() == [], "残留了半文件"
        assert os.listdir(os.path.dirname(target)) == []

    def test_cleans_tmp_when_write_fails(self, scan_root, monkeypatch):
        """写 .tmp 本身就失败（路径是目录/权限不足）时也不能留残骸"""
        target = thumbStore.thumb_abspath(_HASH, 400)
        os.makedirs(target)          # 故意让 <name>.tmp 无法创建（同名目录已存在）
        with pytest.raises(Exception):
            thumbStore.write_atomic(target, b"x")
        assert thumbStore.findTmpFiles() == []

    def test_tag_isolates_concurrent_tmp(self, scan_root):
        """tag 非空 -> <name>.<tag>.tmp

        批量进程池与按需线程池可能同时处理同一张图，共用一个 .tmp 会互相截断。
        """
        plain = thumbStore.tmpPathOf("a/b_400.webp")
        tagged = thumbStore.tmpPathOf("a/b_400.webp", "12345")
        assert plain.endswith("b_400.webp.tmp")
        assert tagged.endswith("b_400.webp.12345.tmp")
        assert plain != tagged

    def test_tagged_write_no_leftover(self, scan_root):
        target = thumbStore.thumb_abspath(_HASH, 400)
        thumbStore.write_atomic(target, b"x", tag="999")
        assert thumbStore.findTmpFiles() == []
        assert thumbStore.exists(target)

    def test_refuses_photo_dir(self, scan_root):
        """**原图只读硬约束**：目标落在 photo 之内 -> 立刻抛错，不写一个字节"""
        target = os.path.join(str(scan_root), "2024", "evil.webp")
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.write_atomic(target, b"x")
        assert not os.path.exists(target)
        # photo 目录连子目录都不该被建出来
        assert not os.path.isdir(os.path.dirname(target))

    def test_refuses_photo_dir_with_bucket_shape(self, scan_root):
        """就算路径形状看起来完全正常（thumbs/xx/...），落在 photo 里照样拒"""
        rel = thumbStore.thumb_relpath(_HASH, 400)
        target = os.path.join(str(scan_root), *rel.split("/"))
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.write_atomic(target, b"x")

    def test_reject_non_bytes(self, scan_root):
        target = thumbStore.thumb_abspath(_HASH, 400)
        for bad in ("字符串", 123, None, {"a": 1}, ["x"]):
            with pytest.raises(thumbStore.ThumbStoreError):
                thumbStore.write_atomic(target, bad)
        assert thumbStore.findTmpFiles() == []

    def test_accepts_bytearray_and_memoryview(self, scan_root):
        target = thumbStore.thumb_abspath(_HASH, 400)
        thumbStore.write_atomic(target, bytearray(b"ba"))
        assert thumbStore.read_bytes(target) == b"ba"
        thumbStore.write_atomic(target, memoryview(b"mv"))
        assert thumbStore.read_bytes(target) == b"mv"


# ============================================================
# 四、存在性判断与统计
# ============================================================

class TestExistsAndStats:
    def test_exists_true_after_write(self, scan_root):
        target = thumbStore.thumb_abspath(_HASH, 400)
        thumbStore.write_atomic(target, b"x")
        assert thumbStore.exists(target) is True
        assert thumbStore.exists(thumbStore.thumb_relpath(_HASH, 400)) is True

    def test_zero_byte_counts_as_missing(self, scan_root):
        """0 字节缩略图一定是坏的（生成中断残留）—— 当作不存在重新生成

        宁可让前端等 100ms 重新生成一张，也不要给它一张裂图。
        """
        target = thumbStore.thumb_abspath(_HASH, 400)
        thumbStore.write_atomic(target, b"")
        assert os.path.exists(target)
        assert thumbStore.exists(target) is False

    def test_exists_false_when_absent(self, scan_root):
        assert thumbStore.exists(thumbStore.thumb_abspath(_HASH, 400)) is False

    def test_file_size_missing_is_zero(self, scan_root):
        assert thumbStore.fileSize(thumbStore.thumb_abspath(_HASH, 400)) == 0

    def test_thumb_stats(self, scan_root):
        for size in (200, 400, 800):
            thumbStore.write_atomic(thumbStore.thumb_abspath(_HASH, size), b"12345")
        stat = thumbStore.thumbStats()
        assert stat["exists"] is True
        assert stat["files"] == 3
        assert stat["bytes"] == 15
        assert stat["buckets"] == 1
        assert stat["maxPerBucket"] == 3
        assert stat["maxBucketName"] == "ab"

    def test_thumb_stats_missing_root(self, scan_root):
        stat = thumbStore.thumbStats()
        assert stat["exists"] is False
        assert stat["files"] == 0 and stat["buckets"] == 0

    def test_find_tmp_files_detects_anywhere(self, scan_root):
        """刻意扫整个 thumbRoot 递归：步骤 5 的人脸图也走 write_atomic，一起查"""
        thumbStore.write_atomic(thumbStore.thumb_abspath(_HASH, 400), b"x")
        stray = os.path.join(str(scan_root.parent / "thumb"), "faces", "zz",
                             "left" + thumbStore.TMP_EXT)
        os.makedirs(os.path.dirname(stray), exist_ok=True)
        with open(stray, "wb") as handle:
            handle.write(b"half")
        found = thumbStore.findTmpFiles()
        assert found == [stray]

    def test_find_tmp_files_empty_when_clean(self, scan_root):
        thumbStore.write_atomic(thumbStore.thumb_abspath(_HASH, 400), b"x")
        assert thumbStore.findTmpFiles() == []
