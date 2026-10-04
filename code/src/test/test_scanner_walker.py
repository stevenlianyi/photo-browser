#! /usr/bin/env python3
#encoding: utf-8

# Filename: test_scanner_walker.py
# Description: processor/scanner/walker.py 单测
#
# 覆盖六块：
#   A. normalize_relpath（扫描器口径：正斜杠 / 去 ./ / NFC / 拒绝绝对路径与 ..）
#   B. relPathHash **幂等**：同一路径的等价写法必须算出同一个 hash，
#      否则「relPathHash 命中 = 同一文件」的判定就建立在流沙上
#   C. fileHash **分块一致性**：8MB 分块流式 == 一次性hashlib == 任意其它分块大小
#   D. 遍历：扩展名白名单 / 排除目录 / 5级嵌套 / 排序 / relPath 逐字保留
#   E. 断点游标 afterCursor：不重不漏
#   F. 只读硬约束：遍历前后 photo 目录的 mtime/文件数/内容**一个字节都没变**
#
# 硬约束：本文件**只在 pytest 的 tmp_path 下造图**，绝不碰真实照片库。

import hashlib
import os
import unicodedata

import pytest

from common import paths as paths
from config import basicSettings as basicSettings
from processor.scanner import walker as walker


# ============================================================
# A. normalize_relpath
# ============================================================

class TestNormalizeRelPath:
    def test_backslash_to_slash(self):
        assert paths.normalize_relpath("a\\b\\c.jpg") == "a/b/c.jpg"

    def test_drop_dot_segments_anywhere(self):
        for raw in ("./a/b.jpg", "a/./b.jpg", "a//b.jpg", "/a/b.jpg", "a/b.jpg"):
            assert paths.normalize_relpath(raw) == "a/b.jpg"

    def test_nfd_equals_nfc(self):
        """Windows 上 NTFS 存NFD，同一文件名跨系统字节不同，必须先统一"""
        nfd = unicodedata.normalize("NFD", "café.jpg")
        nfc = unicodedata.normalize("NFC", "café.jpg")
        assert nfd != nfc
        assert paths.normalize_relpath(nfd) == paths.normalize_relpath(nfc)

    def test_strip_surrounding_blank(self):
        assert paths.normalize_relpath("  a/b.jpg  ") == "a/b.jpg"

    def test_empty_or_separator_only(self):
        assert paths.normalize_relpath("") == ""
        assert paths.normalize_relpath("///") == ""

    def test_reject_absolute_and_escape(self):
        with pytest.raises(ValueError):
            paths.normalize_relpath("C:/photos/a.jpg")
        with pytest.raises(ValueError):
            paths.normalize_relpath("//server/share/a.jpg")
        with pytest.raises(ValueError):
            paths.normalize_relpath("../outside/a.jpg")
        with pytest.raises(ValueError):
            paths.normalize_relpath("a/../../b.jpg")

    def test_do_not_change_case_or_content(self):
        """规范化**只动分隔符与 ./**：大小写、括号、空格一律原样"""
        raw = "My Photos/IMG (1).JPG"
        assert paths.normalize_relpath(raw) == raw


# ============================================================
# B. relPathHash 幂等
# ============================================================

class TestRelPathHashIdempotent:
    def test_equal_writings_give_equal_hash(self):
        variants = ("a/b/c.jpg", "a\\b\\c.jpg", "./a/b/c.jpg", "a/./b/c.jpg",
                    "a//b/c.jpg")
        hashes = {walker.calcRelPathHash(v) for v in variants}
        assert len(hashes) == 1

    def test_hash_is_64_hex(self):
        value = walker.calcRelPathHash("a/b.jpg")
        assert len(value) == basicSettings.HASH_HEX_LEN
        assert all(ch in "0123456789abcdef" for ch in value)

    def test_nfd_nfc_same_hash(self):
        nfd = unicodedata.normalize("NFD", "café/1.jpg")
        nfc = unicodedata.normalize("NFC", "café/1.jpg")
        assert walker.calcRelPathHash(nfd) == walker.calcRelPathHash(nfc)

    def test_different_path_gives_different_hash(self):
        assert walker.calcRelPathHash("a/b.jpg") != walker.calcRelPathHash("a/c.jpg")
        # 大小写不同 -> hash 不同（Windows 上虽然指向同一文件，但保持「逐字区分」更安全）
        assert walker.calcRelPathHash("A/b.jpg") != walker.calcRelPathHash("a/b.jpg")

    def test_stable_across_calls(self):
        assert walker.calcRelPathHash("x/y.jpg") == walker.calcRelPathHash("x/y.jpg")


# ============================================================
# C. fileHash 分块一致性
# ============================================================

class TestFileHashChunked:
    def test_matches_hashlib_oneshot(self, tmp_path):
        """8MB 分块流式 == hashlib 一次性读入（**测试里的小文件**允许整读）"""
        target = tmp_path / "blob.bin"
        payload = os.urandom(3 * 1024 * 1024 + 12345)     # 故意不是 8 的整数倍
        target.write_bytes(payload)
        expect = hashlib.sha256(payload).hexdigest()
        assert walker.calcFileHash(str(target)) == expect

    def test_chunk_size_does_not_change_result(self, tmp_path):
        """分块大小不影响结果：8MB / 64KB / 1KB 必须一致"""
        target = tmp_path / "blob.bin"
        target.write_bytes(os.urandom(1024 * 1024 + 7))
        digests = {walker.calcFileHash(str(target), chunkSize=size)
                   for size in (8 * 1024 * 1024, 64 * 1024, 1024, 7, 1)}
        assert len(digests) == 1

    def test_default_chunk_is_8mb(self):
        assert basicSettings.HASH_CHUNK_SIZE == 8 * 1024 * 1024

    def test_empty_file(self, tmp_path):
        target = tmp_path / "empty.jpg"
        target.write_bytes(b"")
        assert walker.calcFileHash(str(target)) == hashlib.sha256(b"").hexdigest()

    def test_content_change_changes_hash(self, tmp_path):
        target = tmp_path / "a.jpg"
        target.write_bytes(b"aaaa")
        first = walker.calcFileHash(str(target))
        target.write_bytes(b"aaab")
        assert walker.calcFileHash(str(target)) != first

    def test_never_reads_whole_file_at_once(self, tmp_path, monkeypatch):
        """回归保护：单次 read 的块大小不得超过配置的分块大小"""
        target = tmp_path / "big.bin"
        target.write_bytes(os.urandom(1024 * 64))
        limit = basicSettings.HASH_CHUNK_SIZE
        seen = []
        realOpen = open

        class _Spy(object):
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                seen.append(size)
                return self._handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

        def _fakeOpen(*args, **kwargs):
            return _Spy(realOpen(*args, **kwargs))

        monkeypatch.setattr("builtins.open", _fakeOpen)
        walker.calcFileHash(str(target))
        monkeypatch.undo()
        assert seen, "应当走 open/read 分块路径"
        assert all(size <= limit for size in seen if size and size > 0)


# ============================================================
# D. 遍历
# ============================================================

def _tree(photoRoot):
    """造一棵 5 级嵌套 + 各类干扰项的小树，返回期望入库的 relPath 列表"""
    expect = []
    for rel in (
        "top.jpg",
        "a/b/IMG_0001.jpg",
        "a/b/c/d/e/deep5.jpg",                       # 5 级
        "a/b/c/d/e/f/IMG_0001.png",                   # 6 级深一点
        "wechat/mmexport_1682908800000.jpg",
        "截图/Screenshot_20240501_120000.png",
        "My Photos/IMG (1).JPG",
        "café/café.jpg",
    ):
        path = os.path.join(str(photoRoot), rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(rel.encode("utf-8"))
        expect.append(rel)

    # 干扰项：一个都不该入库
    for rel in ("notes.txt", "clip.mp4", "noext", "a/b/thumbs/x.jpg",
                "a/@eaDir/y.jpg", ".thumbnails/z.jpg"):
        path = os.path.join(str(photoRoot), rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"junk")
    return expect


class TestWalk:
    def test_whitelist_and_excluded_dirs(self, scan_root):
        expect = _tree(scan_root)
        got = [item[0] for item in walker.listPhotoFiles(str(scan_root))]
        assert sorted(got) == sorted(expect)
        assert not any("thumbs" in g or "@eaDir" in g or ".thumbnails" in g for g in got)

    def test_result_sorted_by_relpath(self, scan_root):
        _tree(scan_root)
        got = [item[0] for item in walker.listPhotoFiles(str(scan_root))]
        assert got == sorted(got)

    def test_relpath_uses_forward_slash_and_keeps_text(self, scan_root):
        _tree(scan_root)
        got = {item[0] for item in walker.listPhotoFiles(str(scan_root))}
        assert "a/b/c/d/e/deep5.jpg" in got           # 分隔符统一为 /
        assert "My Photos/IMG (1).JPG" in got         # 空格/括号/大小写逐字保留
        assert any(g.startswith("caf") for g in got)

    def test_missing_root_returns_empty(self, tmp_path):
        assert walker.listPhotoFiles(str(tmp_path / "nope")) == []
        assert walker.countPhotoFiles(str(tmp_path / "nope")) == 0

    def test_count_matches_list(self, scan_root):
        _tree(scan_root)
        assert walker.countPhotoFiles(str(scan_root)) == \
            len(walker.listPhotoFiles(str(scan_root)))

    def test_file_entry_fields(self, scan_root, make_photo):
        rel = "x/y/IMG_1.jpg"
        make_photo(os.path.join(str(scan_root), "x", "y", "IMG_1.jpg"))
        entry = walker.walkPhotos(str(scan_root))[0]
        assert entry.relPath == rel
        assert entry.normPath == rel
        assert entry.relPathHash == walker.calcRelPathHash(rel)
        assert len(entry.fileHash) == 64
        assert entry.fileSize > 0
        assert entry.mtime > 0

    def test_make_entry_none_when_file_gone(self, scan_root, make_photo):
        target = make_photo(os.path.join(str(scan_root), "gone.jpg"))
        os.remove(target)
        assert walker.makeEntry(target, relPath="gone.jpg") is None

    def test_hash_content_false_skips_hash(self, scan_root, make_photo):
        make_photo(os.path.join(str(scan_root), "a.jpg"))
        entry = walker.makeEntry(os.path.join(str(scan_root), "a.jpg"),
                                 relPath="a.jpg", hashContent=False)
        assert entry.fileHash is None
        assert entry.fileSize > 0

    def test_not_follow_symlinked_dir(self, scan_root, make_photo, tmp_path):
        """目录符号链接不跟随：否则会顺着链接跑到 photo 根之外，
        relPath 变成 "..\\..."，破坏「relPath 恒在 photo 内」的语义"""
        if not hasattr(os, "symlink"):
            pytest.skip("当前平台不支持符号链接")
        outside = tmp_path / "outside"
        outside.mkdir()
        make_photo(str(outside / "secret.jpg"))
        try:
            os.symlink(str(outside), os.path.join(str(scan_root), "link"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("无权创建符号链接")
        got = [item[0] for item in walker.listPhotoFiles(str(scan_root))]
        assert got == []
        assert not any(".." in g for g in got)


# ============================================================
# E. 断点游标
# ============================================================

class TestCursor:
    def test_cursor_no_overlap_no_gap(self, scan_root, make_photo):
        for index in range(10):
            make_photo(os.path.join(str(scan_root), "d%02d" % (index // 3),
                                    "IMG_%02d.jpg" % index))
        allRel = [item[0] for item in walker.listPhotoFiles(str(scan_root))]

        walked = []
        cursor = ""
        while True:
            batch = list(walker.iterPhotoFiles(str(scan_root), afterCursor=cursor))
            if not batch:
                break
            walked.extend(item.relPath for item in batch)
            cursor = batch[-1].relPath

        assert walked == allRel                       # 不重不漏且有序
        assert len(walked) == len(set(walked))

    def test_cursor_is_exclusive(self, scan_root, make_photo):
        make_photo(os.path.join(str(scan_root), "a.jpg"))
        make_photo(os.path.join(str(scan_root), "b.jpg"))
        got = [e.relPath for e in walker.walkPhotos(str(scan_root), afterCursor="a.jpg")]
        assert got == ["b.jpg"]


# ============================================================
# F. 只读硬约束
# ============================================================

def _snapshot(root: str) -> dict:
    """目录内容 + 每个文件的 (size, mtime_ns) 全量指纹"""
    state = {}
    for dirPath, dirNames, fileNames in os.walk(root):
        for name in fileNames:
            full = os.path.join(dirPath, name)
            stat = os.stat(full)
            with open(full, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            state[os.path.relpath(full, root)] = (stat.st_size,
                                                  stat.st_mtime_ns, digest)
    return state


class TestPhotoDirReadOnly:
    def test_walk_changes_nothing_on_disk(self, scan_root, make_photo):
        _tree(scan_root)
        make_photo(os.path.join(str(scan_root), "exif", "IMG_9.jpg"),
                   takenAt="2023:05:01 12:00:00", model="Cam")
        before = _snapshot(str(scan_root))
        beforeMTime = os.stat(str(scan_root)).st_mtime_ns

        walker.walkPhotos(str(scan_root))
        list(walker.iterPhotoFiles(str(scan_root), hashContent=True))

        assert _snapshot(str(scan_root)) == before
        assert os.stat(str(scan_root)).st_mtime_ns == beforeMTime
