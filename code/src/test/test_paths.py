#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_paths.py
#Description: common/paths.py 单测
#
# 覆盖五块：
#   A. 路径派生（默认值 / 显式覆盖 / 改 PHOTO_ROOT 后全链路跟随 / 缺配置兜底）
#   B. 布局校验（合法一组 / 非法若干组：嵌套、等于 photoRoot、大小写）
#   C. ensure_dirs：只建允许建的目录，绝不碰 photo
#   D. normalize_relpath（正斜杠、去 ./、去空白、NFC、拒绝绝对路径、hash 稳定性）
#   E. dump_paths 输出格式
#
# 硬约束：本文件**绝不写真实照片库**，所有写操作都落在 pytest 的 tmp_path 下。

import os
import sys

import pytest

from common import miscCommon as miscCommon
from common import paths as paths
from common.paths import PathLayoutError

# NFD 形态：e + U+0301 组合重音；NFC 形态：U+00E9 预组合字符
_NFD = "cafe\u0301.jpg"
_NFC = "caf\u00e9.jpg"


# ============================================================
# A. 路径派生
# ============================================================

class TestPathDerive:
    def test_photo_dir_is_photo_subdir_of_root(self, photo_root_of):
        """photo_dir() 恒为 <PHOTO_ROOT>\\photo"""
        assert paths.photo_dir() == os.path.join(str(photo_root_of), "photo")

    def test_thumb_derived_when_empty(self, photo_root_of):
        """THUMB_ROOT 留空 -> 派生 <PHOTO_ROOT>\\thumb（DR-1 / DR-8）"""
        assert paths.thumb_dir() == os.path.join(str(photo_root_of), "thumb")

    def test_db_derived_when_empty(self, photo_root_of):
        """DB_FILE 留空 -> 派生 <PHOTO_ROOT>\\db\\photolib.db"""
        assert paths.db_file() == os.path.join(str(photo_root_of), "db", "photolib.db")

    def test_imports_exports_under_db_dir(self, photo_root_of):
        """imports / exports 都挂在 db 目录下（README §四布局）"""
        assert paths.imports_dir() == os.path.join(str(photo_root_of), "db", "imports")
        assert paths.exports_dir() == os.path.join(str(photo_root_of), "db", "exports")

    def test_explicit_thumb_and_db_override(self, tmp_path, set_photo_root):
        """THUMB_ROOT / DB_FILE 显式给出时派生失效，改用配置值"""
        root = tmp_path / "PhotoLib"
        root.mkdir()
        thumb = tmp_path / "elsewhere" / "thumbcache"
        dbfile = tmp_path / "elsewhere" / "state" / "mylib.db"
        set_photo_root(str(root), thumb=str(thumb), db=str(dbfile))

        assert paths.thumb_dir() == str(thumb)
        assert paths.db_file() == str(dbfile)
        # 派生的 dbDir 跟着显式 DB_FILE 走
        assert paths.db_dir() == str(tmp_path / "elsewhere" / "state")

    def test_all_paths_absolute(self, photo_root_of):
        for key, value in paths.all_paths().items():
            assert os.path.isabs(value), "%s 不是绝对路径: %s" % (key, value)

    def test_changing_photo_root_moves_everything(self, tmp_path, set_photo_root):
        """验收项④：改 PHOTO_ROOT 后 photo/thumb/db 全部随之变化，无需改任何业务代码"""
        root_a = tmp_path / "A"
        root_b = tmp_path / "B"
        root_a.mkdir()
        root_b.mkdir()

        set_photo_root(str(root_a))
        first = paths.all_paths()
        set_photo_root(str(root_b))
        second = paths.all_paths()

        for key in ("photo", "thumb", "database", "dbDir", "imports", "exports"):
            assert first[key] != second[key], "%s 没有随 PHOTO_ROOT 变化" % key
            assert second[key].startswith(str(root_b)), \
                "%s 未落在新根目录下: %s" % (key, second[key])

    def test_photo_root_whitespace_trimmed(self, tmp_path, set_photo_root):
        """配置值首尾空白应被去掉"""
        root = tmp_path / "PhotoLib"
        root.mkdir()
        set_photo_root("  %s  " % str(root))
        assert paths.photo_root() == str(root)

    def test_missing_local_settings_falls_back_to_defaults(self, monkeypatch):
        """local_settings.py 缺失时回退 basicSettings.DEFAULT_*，程序仍能启动"""
        import config as config_pkg
        from config import basicSettings as bs

        # 让 `from config import local_settings` 抛 ImportError：
        # 先摘掉 config 包上的属性，再把 sys.modules 条目置 None（import 机制会直接报错）
        monkeypatch.delattr(config_pkg, "local_settings", raising=False)
        monkeypatch.setitem(sys.modules, "config.local_settings", None)

        with pytest.raises(ImportError):
            from config import local_settings  # noqa: F401

        assert paths.readSetting("PHOTO_ROOT") == bs.DEFAULT_PHOTO_ROOT
        assert paths.photo_dir() == os.path.join(bs.DEFAULT_PHOTO_ROOT, "photo")
        assert paths.thumb_dir() == os.path.join(bs.DEFAULT_PHOTO_ROOT, "thumb")
        assert paths.db_file() == os.path.join(bs.DEFAULT_PHOTO_ROOT, "db", "photolib.db")


# ============================================================
# B. 布局校验
# ============================================================

class TestValidateLayout:
    def test_legal_layout_passes(self, photo_root_of):
        """合法一组：三者互不嵌套、都不等于 photoRoot"""
        result = paths.validate_layout()
        assert result["photo"] == paths.photo_dir()
        assert result["thumb"] == paths.thumb_dir()
        assert result["db"] == paths.db_file()

    def test_legal_layout_with_explicit_args(self, layout):
        """合法一组（显式传参，不依赖配置）"""
        photo, thumb, db = layout
        assert paths.validate_layout(photo, thumb, db) == \
            {"photo": photo, "thumb": thumb, "db": db}

    # ---- 非法组1：thumb 落在 photo 之内（验收项⑤ 的场景）----
    def test_thumb_inside_photo_raises(self, photo_root_of):
        photo = paths.photo_dir()
        with pytest.raises(PathLayoutError, match="thumb 不得位于 photo 之内"):
            paths.validate_layout(photo=photo,
                                  thumb=os.path.join(photo, "thumbs"),
                                  db=None)

    # ---- 非法组 2：db 落在 photo 之内 ----
    def test_db_inside_photo_raises(self, photo_root_of):
        photo = paths.photo_dir()
        with pytest.raises(PathLayoutError, match="db 不得位于 photo 之内"):
            paths.validate_layout(photo=photo, thumb=None,
                                  db=os.path.join(photo, "x.db"))

    # ---- 非法组 3：db 落在 thumb 之内 ----
    def test_db_inside_thumb_raises(self, photo_root_of):
        thumb = paths.thumb_dir()
        with pytest.raises(PathLayoutError, match="db 不得位于 thumb 之内"):
            paths.validate_layout(photo=None, thumb=thumb,
                                  db=os.path.join(thumb, "photolib.db"))

    # ---- 非法组 4：photo 落在 thumb 之内（反向嵌套也要拦）----
    def test_photo_inside_thumb_raises(self, photo_root_of):
        thumb = paths.thumb_dir()
        with pytest.raises(PathLayoutError, match="photo 不得位于 thumb 之内"):
            paths.validate_layout(photo=os.path.join(thumb, "photo"),
                                  thumb=thumb, db=None)

    # ---- 非法组 5：三者完全相同 ----
    def test_all_equal_raises(self, photo_root_of):
        same = os.path.join(str(photo_root_of), "same")
        with pytest.raises(PathLayoutError):
            paths.validate_layout(photo=same, thumb=same, db=same)

    # ---- 非法组 6：等于 photoRoot 本身 ----
    @pytest.mark.parametrize("which", ["photo", "thumb", "db"])
    def test_equals_photo_root_raises(self, photo_root_of, which):
        with pytest.raises(PathLayoutError, match="不得等于 photoRoot"):
            paths.validate_layout(**{which: str(photo_root_of)})

    # ---- 非法组 7：大小写不同也算等于（Windows 语义）----
    def test_equals_photo_root_ignores_case(self, photo_root_of):
        with pytest.raises(PathLayoutError, match="不得等于 photoRoot"):
            paths.validate_layout(thumb=str(photo_root_of).upper())

    def test_legal_thumb_outside_root_ok(self, photo_root_of):
        """thumb 放到库外是允许的（规则只禁「嵌套」与「等于 photoRoot」）"""
        other = os.path.join(str(photo_root_of) + "_other", "thumbcache")
        result = paths.validate_layout(photo=paths.photo_dir(), thumb=other,
                                       db=paths.db_file())
        assert result["thumb"] == other

    def test_ensure_dirs_calls_validate(self, set_photo_root, tmp_path):
        """ensure_dirs() 必须先过校验：thumb 在 photo 内时连目录都不许建"""
        root = tmp_path / "PhotoLib"
        (root / "photo").mkdir(parents=True)
        set_photo_root(str(root), thumb=str(root / "photo" / "thumbs"))
        with pytest.raises(PathLayoutError):
            paths.ensure_dirs()
        assert not (root / "photo" / "thumbs").exists()


# ============================================================
# C. ensure_dirs：只建允许建的，绝不碰 photo
# ============================================================

class TestEnsureDirs:
    def test_creates_all_four_dirs(self, sandbox_photo_root):
        """建出 thumb / db / imports / exports"""
        result = paths.ensure_dirs()
        assert set(result) == {"thumbDir", "dbDir", "importsDir", "exportsDir"}
        for d in result.values():
            assert os.path.isdir(d), "%s 未被创建" % d

    def test_is_idempotent(self, sandbox_photo_root):
        """重复执行不报错"""
        paths.ensure_dirs()
        paths.ensure_dirs()
        assert os.path.isdir(paths.thumb_dir())

    def test_never_touches_photo_dir(self, sandbox_photo_root):
        """原图只读硬约束：ensure_dirs() 之后 photo/ 里必须仍然是空的"""
        photo = paths.photo_dir()
        assert os.path.isdir(photo)
        before = sorted(os.listdir(photo))

        paths.ensure_dirs()

        assert sorted(os.listdir(photo)) == before == []

    def test_photo_not_in_writable_list(self, sandbox_photo_root):
        """可写目录清单里压根不该出现 photo（结构性防呆）"""
        normed = {os.path.normcase(p) for p in paths.writable_dirs()}
        assert os.path.normcase(paths.photo_dir()) not in normed
        assert len(normed) == 4

    def test_does_not_create_db_file(self, sandbox_photo_root):
        """只建目录，不建库文件（建库是步骤 2 的事）"""
        paths.ensure_dirs()
        assert not os.path.exists(paths.db_file())

    def test_rejects_write_target_inside_photo(self, monkeypatch, sandbox_photo_root):
        """即便有人往 writable_dirs() 塞了 photo 内的路径，也要当场拒绝"""
        monkeypatch.setattr(
            paths, "writable_dirs",
            lambda: [paths.thumb_dir(), os.path.join(paths.photo_dir(), "oops")])
        with pytest.raises(PathLayoutError, match="原图只读"):
            paths.ensure_dirs()
        assert not os.path.exists(os.path.join(paths.photo_dir(), "oops"))


# ============================================================
# D. normalize_relpath
# ============================================================

class TestNormalizeRelpath:
    @pytest.mark.parametrize("raw,expected", [
        ("a\\b\\c.jpg", "a/b/c.jpg"),
        ("a/b/c.jpg", "a/b/c.jpg"),
        ("a/b\\c.jpg", "a/b/c.jpg"),
        ("./a/b/c.jpg", "a/b/c.jpg"),
        (".\\a\\b\\c.jpg", "a/b/c.jpg"),
        ("./././a/b.jpg", "a/b.jpg"),
        ("  a/b.jpg  ", "a/b.jpg"),
        ("a/b.jpg/", "a/b.jpg"),
        ("/a/b.jpg", "a/b.jpg"),
        ("a//b///c.jpg", "a/b/c.jpg"),
        ("./a/./b.jpg", "a/b.jpg"),
        ("a/./b/./c.jpg", "a/b/c.jpg"),
        ("2024/2024-05-01/IMG_0001.jpg", "2024/2024-05-01/IMG_0001.jpg"),
    ])
    def test_variants(self, raw, expected):
        assert paths.normalize_relpath(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "/", "\\", "./", "///"])
    def test_empty_like_inputs(self, raw):
        """空/纯分隔符 -> 空串"""
        assert paths.normalize_relpath(raw) == ""

    def test_none_raises(self):
        with pytest.raises(ValueError):
            paths.normalize_relpath(None)

    def test_unicode_nfc(self):
        """NFD 输入（Windows/NTFS 形态）应被规范化成 NFC，且 NFC 输入保持幂等"""
        nfd_dir = "cafe\u0301"          # NFD：e + 组合重音符
        nfc_dir = "caf\u00e9"          # NFC：预组合字符
        assert nfd_dir != nfc_dir
        # 目录名与文件名都会被规范化成 NFC
        assert paths.normalize_relpath(nfd_dir + "/" + _NFD) == nfc_dir + "/" + _NFC
        # 已是 NFC 的输入原样返回（幂等，不会二次变形）
        assert paths.normalize_relpath(nfc_dir + "/" + _NFC) == nfc_dir + "/" + _NFC
        assert paths.normalize_relpath(_NFC) == _NFC
        # NFD / NFC 两种书写形式算出同一个 relPathHash
        assert paths.relPathHash(nfd_dir + "/" + _NFD) == \
            paths.relPathHash(nfc_dir + "/" + _NFC)

    def test_rejects_drive_absolute(self):
        with pytest.raises(ValueError, match="盘符"):
            paths.normalize_relpath(r"d:\PhotoLib\photo\a.jpg")

    def test_rejects_unc(self):
        with pytest.raises(ValueError, match="UNC"):
            paths.normalize_relpath(r"\\server\share\a.jpg")

    def test_rejects_forward_slash_drive(self):
        with pytest.raises(ValueError):
            paths.normalize_relpath("C:/photos/a.jpg")

    @pytest.mark.parametrize("raw", [
        "../a.jpg",
        "..",
        "a/../../b.jpg",
        "2024/../../etc/passwd",
        "./../a.jpg",
        "a/b/../../../c.jpg",
        r"..\a.jpg",
        r"a\..\..\b.jpg",
    ])
    def test_rejects_parent_dir_segments(self, raw):
        """含 ".." 的路径能逃出 photoDir，破坏「原图只读」边界，一律拒绝"""
        with pytest.raises(ValueError, match=r"\.\."):
            paths.normalize_relpath(raw)

    def test_dotdot_lookalike_names_are_fine(self):
        """只拦「恰好等于 .. 」的段，名字里含点或含 .. 字符的正常文件不受影响"""
        assert paths.normalize_relpath("a/..b.jpg") == "a/..b.jpg"
        assert paths.normalize_relpath("a/b../c.jpg") == "a/b../c.jpg"
        assert paths.normalize_relpath("a/...jpg") == "a/...jpg"
        assert paths.normalize_relpath("./a/./b.jpg") == "a/b.jpg"

    def test_rejects_parent_dir_hash_raises(self):
        with pytest.raises(ValueError):
            paths.relPathHash("../escape.jpg")

    def test_equivalent_forms_hash_identical(self):
        """同一相对路径的不同书写形式 -> 同一个 relPathHash（增量扫描幂等的基础）"""
        forms = [
            "2024/2024-05-01/IMG_0001.jpg",
            "2024\\2024-05-01\\IMG_0001.jpg",
            "./2024/2024-05-01/IMG_0001.jpg",
            "  2024/2024-05-01/IMG_0001.jpg  ",
            "2024//2024-05-01//IMG_0001.jpg",
            "2024/./2024-05-01/IMG_0001.jpg",
            "/2024/2024-05-01/IMG_0001.jpg/",
        ]
        hashes = {paths.relPathHash(f) for f in forms}
        assert len(hashes) == 1
        assert len(next(iter(hashes))) == 64

    def test_different_paths_different_hash(self):
        assert paths.relPathHash("a/b.jpg") != paths.relPathHash("a/c.jpg")

    def test_relpathhash_matches_misccommon(self):
        """relPathHash 走的是 miscCommon.sha256Hex，算法统一不另起炉灶"""
        raw = "./2024\\a b.jpg"
        assert paths.relPathHash(raw) == miscCommon.sha256Hex(paths.normalize_relpath(raw))


# ============================================================
# E. dump_paths 输出格式
# ============================================================

class TestDumpPaths:
    KEYS = ("photoRoot", "photo", "thumb", "database", "dbDir", "imports", "exports")

    @staticmethod
    def _parse(out):
        got = {}
        for line in out.splitlines():
            if "=" in line and not line.startswith("="):
                k, v = line.split("=", 1)
                got[k.strip()] = v.strip()
        return got

    def test_prints_all_keys(self, photo_root_of, capsys):
        paths.dump_paths()
        got = self._parse(capsys.readouterr().out)
        for key in self.KEYS:
            assert key in got, "缺少 %s，实际输出: %s" % (key, list(got))

    def test_prints_expected_values_and_alignment(self, photo_root_of, capsys):
        """验收项③：photo / thumb / database 三行，标签对齐到第 11 列"""
        paths.dump_paths()
        out = capsys.readouterr().out
        got = self._parse(out)
        root = str(photo_root_of)
        assert got["photo"] == os.path.join(root, "photo")
        assert got["thumb"] == os.path.join(root, "thumb")
        assert got["database"] == os.path.join(root, "db", "photolib.db")

        for line in out.splitlines():
            if line.startswith(("photo ", "thumb ", "database ")):
                assert line.index("=") == 10, "标签未对齐: %r" % line

    def test_returns_all_paths(self, photo_root_of):
        assert paths.dump_paths() == paths.all_paths()

    def test_raises_on_illegal_layout(self, set_photo_root, tmp_path, capsys):
        """布局非法时：先打印再抛异常，方便排查实际解析成了什么"""
        root = tmp_path / "PhotoLib"
        root.mkdir()
        set_photo_root(str(root), thumb=str(root / "photo" / "thumbs"))
        with pytest.raises(PathLayoutError):
            paths.dump_paths()
        assert "thumb" in capsys.readouterr().out

    def test_validate_false_skips_check(self, set_photo_root, tmp_path):
        root = tmp_path / "PhotoLib"
        root.mkdir()
        set_photo_root(str(root), thumb=str(root / "photo" / "thumbs"))
        paths.dump_paths(validate=False)  # 不抛异常
