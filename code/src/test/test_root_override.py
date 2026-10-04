#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_root_override.py
#Description: 步骤 4 单测 —— 进程级根目录覆盖（paths.setRootOverride）
#
# 为什么这条机制要单独立文件测
# --------------------------
#   它的唯一目的是"让 HTTP 服务也能指到临时库做实验，而不用去改正式配置"。
#   一旦它出错，后果是**静默写错库**：服务以为在 tmp，实际把缩略图写进
#   真实的 d:\PhotoLib\thumb（库里没 thumbPath 字段，事后极难发现）。
#   这类"默认走错地方且不报错"的坑只能靠测试钉住。
#
# 覆盖是**进程级全局**，所以每条用例都必须还原 —— 用 autouse fixture 兜底，
# 绝不靠某条用例"记得调用"。

import os

import pytest

from common import paths as paths


@pytest.fixture(autouse=True)
def _clearOverride():
    """每条用例前后都清干净覆盖，绝不污染同进程的其它用例"""
    paths.clearRootOverride()
    yield
    paths.clearRootOverride()


class TestNoOverrideByDefault:
    def test_default_uses_local_settings(self, sandbox_photo_root):
        """不调 setRootOverride 时行为与之前**完全一致**"""
        assert paths.photo_dir() == os.path.join(str(sandbox_photo_root), "photo")
        assert paths.thumb_dir() == os.path.join(str(sandbox_photo_root), "thumb")
        assert paths.db_file() == os.path.join(str(sandbox_photo_root), "db",
                                               paths.DB_FILE_NAME)
        assert paths.rootOverrides() == {"photo": None, "thumb": None, "db": None}

    def test_config_wins_when_no_override(self, set_photo_root, tmp_path):
        """配置给什么就是什么（回归保护：覆盖不能反过来压住 local_settings）"""
        set_photo_root(str(tmp_path / "Configured"))
        paths.clearRootOverride()
        assert paths.photo_dir() == os.path.join(str(tmp_path / "Configured"), "photo")


class TestOverride:
    def test_override_all_three(self, tmp_path, monkeypatch):
        # 覆盖要过 validate_layout，而 validate_layout 会用 photo_root()，
        # 所以先给一个合法的 PHOTO_ROOT（覆盖后的三目录在它之外）
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        photo = tmp_path / "exp" / "photo"
        thumb = tmp_path / "exp" / "thumb"
        db = tmp_path / "exp" / "db" / "x.db"
        got = paths.setRootOverride(photo=str(photo), thumb=str(thumb), db=str(db))
        assert got["photo"] == str(photo)
        assert paths.photo_dir() == str(photo)
        assert paths.thumb_dir() == str(thumb)
        assert paths.db_file() == str(db)

    def test_derived_dirs_follow_override(self, tmp_path, monkeypatch):
        """db_dir / imports_dir / exports_dir 必须跟着走 —— 否则备份会拷错地方"""
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        db = tmp_path / "exp" / "db" / "x.db"
        paths.setRootOverride(photo=str(tmp_path / "exp" / "photo"),
                             thumb=str(tmp_path / "exp" / "thumb"), db=str(db))
        assert paths.db_dir() == str(tmp_path / "exp" / "db")
        assert paths.imports_dir() == str(tmp_path / "exp" / "db" / "imports")
        assert paths.exports_dir() == str(tmp_path / "exp" / "db" / "exports")

    def test_dump_paths_works_with_override(self, tmp_path, monkeypatch, capsys):
        """有覆盖时 dump_paths 也要能跑（启动横幅会调它，NameError 就起不来服务）"""
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        root = tmp_path / "exp"
        paths.setRootOverride(photo=str(root / "photo"), thumb=str(root / "thumb"),
                             db=str(root / "db" / "x.db"))
        paths.dump_paths()
        out = capsys.readouterr().out
        assert "[override]" in out
        assert str(root / "photo") in out

    def test_all_paths_and_ensure_dirs_follow(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        root = tmp_path / "exp"
        paths.setRootOverride(photo=str(root / "photo"), thumb=str(root / "thumb"),
                             db=str(root / "db" / "x.db"))
        made = paths.ensure_dirs()
        assert os.path.isdir(str(root / "thumb"))
        assert os.path.isdir(str(root / "db"))
        assert not os.path.isdir(str(root / "photo")), "**建出了 photo 目录**"
        assert paths.all_paths()["photo"] == str(root / "photo")
        assert set(made) == {"thumbDir", "dbDir", "importsDir", "exportsDir"}

    def test_partial_override(self, tmp_path, monkeypatch):
        """只给 thumb，其余走配置"""
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        alt = tmp_path / "alt_thumb"
        paths.setRootOverride(thumb=str(alt))
        assert paths.thumb_dir() == str(alt)
        assert paths.photo_dir() == os.path.join(str(tmp_path / "RealLib"), "photo")
        assert paths.db_file() == os.path.join(str(tmp_path / "RealLib"), "db",
                                               paths.DB_FILE_NAME)

    def test_empty_string_means_no_override(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        paths.setRootOverride(thumb="", photo=None, db="")
        assert paths.rootOverrides()["thumb"] is None
        assert paths.thumb_dir() == os.path.join(str(tmp_path / "RealLib"), "thumb")

    def test_relative_path_is_normalized(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        paths.setRootOverride(thumb=str(tmp_path / "exp" / ".." / "exp2" / "thumb"))
        assert paths.thumb_dir() == str(tmp_path / "exp2" / "thumb")

    def test_clear(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        paths.setRootOverride(thumb=str(tmp_path / "alt_thumb"))
        paths.clearRootOverride()
        assert paths.rootOverrides()["thumb"] is None
        assert paths.thumb_dir() == os.path.join(str(tmp_path / "RealLib"), "thumb")

    def test_writable_dirs_never_contains_photo(self, tmp_path, monkeypatch):
        """**最要紧的一条**：可写目录清单里永远不许出现 photo"""
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        root = tmp_path / "exp"
        paths.setRootOverride(photo=str(root / "photo"), thumb=str(root / "thumb"),
                             db=str(root / "db" / "x.db"))
        assert str(root / "photo") not in [paths._key(p) for p in paths.writable_dirs()]


class TestOverrideRejectsIllegalLayout:
    def test_thumb_inside_photo_rejected(self, tmp_path, monkeypatch):
        """把 thumb 指到 photo 里面 -> 立刻抛错

        这会让缩略图生成物落进只读原图区，是最不能容忍的一种错。
        """
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        with pytest.raises(paths.PathLayoutError):
            paths.setRootOverride(photo=str(tmp_path / "exp" / "photo"),
                                 thumb=str(tmp_path / "exp" / "photo" / "thumb"),
                                 db=str(tmp_path / "exp" / "db" / "x.db"))

    def test_db_inside_thumb_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        with pytest.raises(paths.PathLayoutError):
            paths.setRootOverride(photo=str(tmp_path / "exp" / "photo"),
                                 thumb=str(tmp_path / "exp" / "thumb"),
                                 db=str(tmp_path / "exp" / "thumb" / "x.db"))

    def test_photo_equals_photo_root_rejected(self, tmp_path, monkeypatch):
        """photo 覆盖成 photoRoot 本身 -> 拒（否则等于让服务往根目录里写）"""
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        with pytest.raises(paths.PathLayoutError):
            paths.setRootOverride(photo=str(tmp_path / "RealLib"),
                                 thumb=str(tmp_path / "exp" / "thumb"),
                                 db=str(tmp_path / "exp" / "db" / "x.db"))


class TestApiLayerFollowsOverride:
    """API 层直接调 paths.photo_dir() / paths.thumb_dir()，必须跟着覆盖走"""

    def test_thumb_path_follows_override(self, tmp_path, monkeypatch):
        from processor.media import thumbStore as thumbStore
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        alt = tmp_path / "alt_thumb"
        paths.setRootOverride(photo=str(tmp_path / "exp" / "photo"), thumb=str(alt),
                             db=str(tmp_path / "exp" / "db" / "x.db"))
        got = thumbStore.thumb_abspath("ab" + "0" * 62, 400)
        assert got.startswith(str(alt))

    def test_orig_path_follows_override(self, tmp_path, monkeypatch):
        from processor.media import thumbStore as thumbStore
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        photo = tmp_path / "exp" / "photo"
        paths.setRootOverride(photo=str(photo), thumb=str(tmp_path / "exp" / "thumb"),
                             db=str(tmp_path / "exp" / "db" / "x.db"))
        assert thumbStore.orig_abs_path("a/b.jpg") == str(photo / "a" / "b.jpg")

    def test_readonly_guard_uses_overridden_photo_dir(self, tmp_path, monkeypatch):
        """**只读守卫必须守在"当前生效的 photo 根"上**

        守错了根，等于没守：覆盖之后守卫还在拿正式库的 photo 去比对。
        """
        from processor.media import thumbStore as thumbStore
        monkeypatch.setattr("config.local_settings.PHOTO_ROOT",
                            str(tmp_path / "RealLib"), raising=False)
        photo = tmp_path / "exp" / "photo"
        paths.setRootOverride(photo=str(photo), thumb=str(tmp_path / "exp" / "thumb"),
                             db=str(tmp_path / "exp" / "db" / "x.db"))
        with pytest.raises(thumbStore.ThumbStoreError):
            thumbStore.write_atomic(str(photo / "x.webp"), b"x")
        # 清掉覆盖后守卫改守默认的 photo 根：此时 exp\photo 下的路径**允许写**，
        # 反倒是 RealLib\photo 下的被拒 —— 证明守卫确实是"跟着当前生效的根"走的
        paths.clearRootOverride()
        assert thumbStore.isUnderPhotoDir(str(photo / "x.webp")) is False
        assert thumbStore.isUnderPhotoDir(
            os.path.join(str(tmp_path / "RealLib"), "photo", "x.webp")) is True
