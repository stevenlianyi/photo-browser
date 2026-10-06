#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_conftest_guard.py
#Description: conftest.py 里那道「正式库不许被测试碰」的闸 —— 只测它的**判定话术**
#
# 为什么不测闸本身：它是 session 级 autouse 夹具，要"真让它红"就得真去写正式库 ——
# 那正是它要防的事，不可能在测试里做。所以把可判定的部分（改动的**解读**）
# 抽成 `describeDbChange` 来单测；「前后各取一次指纹再比对」那三行留在夹具里。

import importlib.util
import os

# ⚠️ conftest.py 不在任何包里，直接 `import conftest` 可能拿到别的同名模块。
#    按**文件路径**加载，保证测的就是本目录这一份。
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_pb_conftest_guard", os.path.join(_HERE, "conftest.py"))
_cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cf)

describeDbChange = _cf.describeDbChange
_fingerprint = _cf._fingerprint


# ============================================================
# A. 「正式库被改动」的判定话术
# ============================================================
#
# ⚠️ 为什么要单测一段"生成文案"的代码：这段文案是**排障的唯一入口**。
#    上个版本把「size 变了」与「只有 mtime 变了」混为一谈，
#    于是「开发期间自己动过库」会被断言成「多半是用例漏挂夹具」——
#    接手的人照着去逐个用例排查，必然空手而归（步骤 9 实测白查一轮）。
#    文案错了，这道闸就不只是"多报一次"，而是**把人引到错的方向**。

def test_sizeChangeIsJudgedAsTestPollution():
    """size 变了 -> 必须**明确指向用例漏挂夹具**（这是真有数据写进去）。"""
    msg = describeDbChange(r"d:\PhotoLib\db\photolib.db",
                           (2453504, 100), (2457600, 200))
    assert "文件大小也变了" in msg
    assert "2453504 -> 2457600" in msg
    assert "漏挂" in msg


def test_mtimeOnlyChangeWarnsAboutExternalCause():
    """只有 mtime 变 -> 必须**先提示外部原因**，不能一口咬定是用例。"""
    msg = describeDbChange(r"d:\PhotoLib\db\photolib.db",
                           (2453504, 100), (2453504, 999))
    assert "只有 mtime 变、大小没变" in msg
    assert "也可能不是测试干的" in msg
    assert "你自己在跑服务" in msg
    # ⚠️ 反向断言：这一支**不能**出现「基本可以确定是用例」那种口径
    assert "基本可以确定" not in msg


def test_bothBranchesGiveTheSameTroubleshootingSteps():
    """两支都要给可执行的排查步骤（否则收到失败的人不知道下一步做什么）。"""
    for before, after in (((100, 1), (200, 2)), ((100, 1), (100, 9))):
        msg = describeDbChange("x.db", before, after)
        assert "逐个文件跑" in msg and "-k" in msg and "夹具" in msg
        assert "备份恢复" in msg


# ============================================================
# B. 指纹函数
# ============================================================

def test_fingerprintIsAnIntTupleOnRealFile(tmp_path):
    """正常文件上必须给 (size, mtime) 整数对。"""
    target = tmp_path / "some.db"
    target.write_bytes(b"12345")
    got = _fingerprint(str(target))
    assert isinstance(got, tuple) and len(got) == 2
    assert got[0] == 5
    assert isinstance(got[1], int)


def test_fingerprintReturnsNoneInsteadOfRaising(tmp_path):
    """取不到就返回 None —— 这道闸**不许**因为自己的 IO 出错而误判失败。"""
    assert _fingerprint(str(tmp_path / "does_not_exist.db")) is None
    assert _fingerprint(str(tmp_path)) is None          # 目录也不行


def test_fingerprintChangesWhenFileIsRewritten(tmp_path):
    """指纹必须真的能感知"被写"（否则整道闸是空的）。"""
    target = tmp_path / "some.db"
    target.write_bytes(b"1")
    before = _fingerprint(str(target))
    os.utime(str(target), (before[1] + 5, before[1] + 5))     # 只动 mtime
    afterMtime = _fingerprint(str(target))
    assert afterMtime[0] == before[0] and afterMtime[1] != before[1]
    target.write_bytes(b"123456")                             # 连 size 一起变
    afterSize = _fingerprint(str(target))
    assert afterSize[0] != afterMtime[0]
