#! /usr/bin/env python3
#encoding: utf-8
#Filename: test_backup_cli.py
#Description: tools/backup.py 测试 —— 备份/恢复这条链最危险的地方是"拷到一半"
#
# 为什么值得单独测它（它是这么坏掉的）
# --------------------------------------
#   `copyTree` 每拷 N 个文件打一行进度：`"…（%.1f）" % (copied, humanSize(total))`。
#   而 `humanSize()` 返回的是**已经格式化好的字符串**（"12.3 MB"）——
#   于是 `bytes.__mod__` 在**第一次打进度**时就抛 TypeError：备份必崩，
#   而崩之前已经建好目录、拷完了整个 db\（正式库 1.6 GB）。
#   一条没人测过的工具链就是这样坏的：不是逻辑错，是**格式化**错。
#
# 三条要盯住的不变量
#   ① copyTree 打进度不抛（人话说出来就行，别再格式化一遍）
#   ② 备份**中途失败不留半成品目录**：否则 list 会把它列成一个可用备份，
#      而 restore 只检查 db\ 与 thumb\ 两个子目录在不在（拷到 60% 也能过）——
#      恢复之后缩略图与库里的人脸数从此对不上，且不报错
#   ③ restore **必须有 manifest** 才放行：manifest 是最后写的，
#      它才是"这次备份完整跑完了"的唯一凭据
#
# 全部跑在 pytest 的 tmp_path 上（set_photo_root 打桩 PHOTO_ROOT），
# **绝不动真实的 d:\PhotoLib**。

import json
import os

import pytest


@pytest.fixture
def fake_lib(set_photo_root, tmp_path):
    r"""一棵"小库"：PHOTO_ROOT 下建 db\ 与 thumb\，路径全部打桩过去。

    backup_root() 是从 PHOTO_ROOT 派生的（paths.backup_root），所以打桩
    PHOTO_ROOT 一个就够 —— 备份也会落在 tmp 里。
    """
    root = tmp_path / "PhotoLib"
    (root / "db").mkdir(parents=True)
    (root / "thumb" / "aa").mkdir(parents=True)
    (root / "photo").mkdir(parents=True)
    set_photo_root(str(root))
    from common import paths as paths

    paths.ensure_dirs()
    (root / "db" / "photolib.db").write_bytes(b"x" * 2048)
    # ⚠️ -wal/-shm 必须排除在备份外（copyTree 的口径）：放一个哨兵文件，
    #    跑完断言它**没有**被拷过去。
    (root / "db" / "photolib.db-wal").write_bytes(b"wal")
    (root / "thumb" / "aa" / "t1.webp").write_bytes(b"y" * 512)
    return root


def test_copyTreeProgressDoesNotRaise(fake_lib, monkeypatch, capsys):
    """① 打进度那一行**不能**抛 —— 那正是备份必崩的那一步"""
    from tools import backup as backupMod

    monkeypatch.setattr(backupMod, "_PROGRESS_EVERY", 2)
    src = fake_lib / "thumb"
    for i in range(7):                       # 7 个新文件 -> 至少打 3 次进度
        (src / ("f%d.webp" % i)).write_bytes(b"z" * 32)

    files, total = backupMod.copyTree(str(src), str(fake_lib / "out"))
    assert files == 8 and total > 0
    out = capsys.readouterr().out
    assert "已复制" in out, "进度行要照打（它是「拷很久没输出」时唯一的安慰）"
    assert "webp" not in out                  # 只打计数与体积，不逐个文件刷屏
    assert "B" in out or "KB" in out          # humanSize 的产物原样出现


def test_backupSkipsWalAndWritesManifest(fake_lib):
    """正常路径：-wal 不进备份、manifest 写得出来、编号唯一"""
    from tools import backup as backupMod

    got = backupMod.doBackup(label="unit")
    assert got["ok"] is True
    target = got["path"]
    assert os.path.isfile(os.path.join(target, "db", "photolib.db"))
    assert not os.path.exists(os.path.join(target, "db",
                                          "photolib.db-wal")), \
        "库里还开着连接时拷走 -wal，恢复后会拿到陈旧状态（copyTree 的口径）"
    manifestPath = os.path.join(target, backupMod.paths.BACKUP_MANIFEST_NAME)
    assert os.path.isfile(manifestPath)
    with open(manifestPath, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["db"]["files"] >= 1
    assert manifest["thumb"]["files"] >= 1
    assert manifest["label"] == "unit"

    # 同一秒连跑两次：编号必须带后缀，不许互相覆盖
    again = backupMod.doBackup(label="unit2")
    assert again["ok"] is True and again["path"] != target
    assert os.path.isfile(os.path.join(target, "db", "photolib.db")), \
        "第二次备份不许把第一次的目录当成同一个"


def test_backupFailureRemovesHalfMadeDir(fake_lib, monkeypatch):
    """② 中途失败**不留半成品**：它会被 list 列成可用备份（restore 只看子目录）"""
    from tools import backup as backupMod

    realCopy = backupMod.copyTree
    state = {"calls": 0}

    def _boomOnSecond(src, dst, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:               # db 拷完、thumb 刚开始时炸
            raise RuntimeError("模拟：磁盘掉线")
        return realCopy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backupMod, "copyTree", _boomOnSecond)
    got = backupMod.doBackup(label="half")
    assert got["ok"] is False
    assert "磁盘掉线" in got["errMsg"]
    assert not os.path.exists(got["path"]), "半成品目录必须删掉，否则能拿它恢复"

    listed = backupMod.doList()
    assert all(one["backupCode"] != os.path.basename(got["path"])
               for one in listed)


def test_restoreRefusesBackupWithoutManifest(fake_lib):
    """③ 缺 manifest 的目录**不能**恢复：那是"拷到一半"的备份"""
    from tools import backup as backupMod

    got = backupMod.doBackup(label="full")
    assert got["ok"] is True

    # 手工造一个"拷到一半"：子目录都在，manifest 没有
    half = os.path.join(str(fake_lib), "backup", "pb_20990101000000")
    os.makedirs(os.path.join(half, "db"))
    os.makedirs(os.path.join(half, "thumb"))
    with open(os.path.join(half, "db", "photolib.db"), "wb") as fh:
        fh.write(b"truncated")

    refused = backupMod.doRestore("pb_20990101000000", assumeYes=True)
    assert refused["ok"] is False
    assert "manifest" in refused["errMsg"]

    # 完整的那个仍然可以走完流程的前置检查（真正覆盖 db\ 会动真实目录，
    # 所以这里只验证"它通过了完整性判定"）
    source = os.path.join(str(fake_lib), "backup",
                          os.path.basename(got["path"]))
    assert os.path.isfile(os.path.join(source,
                                       backupMod.paths.BACKUP_MANIFEST_NAME))
