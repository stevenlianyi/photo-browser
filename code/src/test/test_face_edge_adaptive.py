#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_face_edge_adaptive.py
#Description: 人脸框短边门槛**按图自适应**（R17）的单测
#
# 为什么这组用例必须存在
# --------------------
#   这条改动的失效方式是**静默的**，而且两种失效都很难在日常使用中发现：
#     ① 自适应没生效（老照片继续被判"没有人脸"）——
#        用户只会觉得"怎么还是认不出"，界面上没有任何异常；
#     ② 自适应**过度**生效（大图也被放宽）——
#        12MP 照片里的背景误检会当成脸入库，把待确认队列灌满噪声，
#        而这一切看起来完全正常（有脸、有分数、有候选）。
#   ②尤其危险：它是"多收"，而多收不会报错。所以必须把**大图口径与改动前
#   逐字一致**（64px）钉在测试里 —— 那一条断言就是这条改动的安全绳。
#
#   另外三处调用（两个后端的 detect 阶段 1、filterAndRank）必须同一口径，
#   `_Limits.edgeFor` 存在的意义就是让后两者不可能各写一份 —— 这里也钉住它。

import numpy as np
import pytest

from config import basicSettings as bs
from engine.face import engine as E


# ============================================================
# 装置
# ============================================================

def makeFace(shortEdge, imgW=1280, imgH=960, detScore=0.9, poseYaw=0.0):
    """造一张居中的方形人脸（bboxPx 是**原图像素**坐标，与检测输出同口径）"""
    edge = float(shortEdge)
    x1 = (float(imgW) - edge) / 2.0
    y1 = (float(imgH) - edge) / 2.0
    return {
        "bboxPx": (x1, y1, x1 + edge, y1 + edge),
        "detScore": detScore,
        "poseYaw": poseYaw,
        "posePitch": 0.0,
        "embedding": np.ones(E.EMBEDDING_DIM, dtype=np.float32),
    }


# ============================================================
# 一、门槛函数本身
# ============================================================

class TestEffectiveMinFaceEdge:

    def test_big_photo_identical_to_before(self):
        """**安全绳**：短边 >= 1600 的图，生效门槛必须与改动前逐字一致（64px）。

        这几档覆盖了库里绝大多数照片：12MP 单反 / 4:3 卡片机最高档 /
        2002 年的 2272x1704。它们的行为**一点都不能变**。
        """
        for w, h in [(3456, 2304), (4000, 3000), (2272, 1704), (3264, 2448),
                     (1600, 1600)]:
            assert E.effectiveMinFaceEdge(w, h) == bs.MIN_FACE_EDGE == 64, (w, h)

    def test_boundary(self):
        """边界在短边 1600（= 64 / 0.04）：这里就是"大图不变"与"小图放宽"的分界。

        1440x1080 这类 4MP 老照片**属于放宽档**（-> 43px）—— 它有 1080 的短边，
        人脸同样只有 45~60px。写这条是为了防止"以为 1440 算大图"的误判。
        """
        assert E.effectiveMinFaceEdge(1600, 1600) == 64
        assert E.effectiveMinFaceEdge(1599, 1600) == 64
        assert E.effectiveMinFaceEdge(1440, 1080) == 43
        assert E.effectiveMinFaceEdge(1600, 1200) == 48

    def test_small_photo_relaxed(self):
        """1280x960（2005 年 DSC-T7 这批）-> 38px：43~63px 的可用配角脸从此收得进来

        0.05 -> 0.04 的依据：DSC00206 实测里被拦的 7 张脸是 43~47px，
        降到 32px 后它们与已确认样本的相似度 0.61~0.85（**本可自动归属**）。
        """
        assert E.effectiveMinFaceEdge(1280, 960) == 38
        assert E.effectiveMinFaceEdge(960, 1280) == 38   # 竖图按短边算，一样

    def test_floor_prevents_noise(self):
        """再小的图也不低于 FLOOR：800x600 -> 30 会触底到 32px"""
        assert E.effectiveMinFaceEdge(800, 600) == bs.MIN_FACE_EDGE_FLOOR == 32
        assert E.effectiveMinFaceEdge(640, 480) == 32

    def test_explicit_value_is_fixed(self):
        """显式值 = 固定绝对值：**不做反向放宽**（200 就是 200，不会变成 48）"""
        assert E.effectiveMinFaceEdge(1280, 960, 200) == 200
        assert E.effectiveMinFaceEdge(4000, 3000, 200) == 200
        assert E.effectiveMinFaceEdge(1280, 960, 20) == 20

    def test_adaptive_off(self):
        """adaptive=False = 老口径（S0 复现 / --no-adaptive-edge）"""
        assert E.effectiveMinFaceEdge(1280, 960, 64, False) == 64
        assert E.effectiveMinFaceEdge(800, 600, None, False) == 64
        assert E.effectiveMinFaceEdge(1280, 960, 20, False) == 20

    def test_reads_config_at_call_time(self, monkeypatch):
        """走 basicSettings 的三个开关，且是**调用时**读取（不是 import 时固化）"""
        monkeypatch.setattr(bs, "MIN_FACE_EDGE_ADAPTIVE", False)
        assert E.effectiveMinFaceEdge(1280, 960) == 64

        monkeypatch.setattr(bs, "MIN_FACE_EDGE_ADAPTIVE", True)
        monkeypatch.setattr(bs, "MIN_FACE_EDGE_RATIO", 0.10)
        assert E.effectiveMinFaceEdge(1280, 960) == 64      # min(64, 96)
        monkeypatch.setattr(bs, "MIN_FACE_EDGE_RATIO", 0.02)
        assert E.effectiveMinFaceEdge(1280, 960) == 32      # min(64, 19) -> floor

    def test_degenerate_size(self):
        """尺寸取不到（0 / None）时回落到绝对值，绝不返回 0 —— 返回 0 = 不过滤"""
        assert E.effectiveMinFaceEdge(0, 0) == 64
        assert E.effectiveMinFaceEdge(None, None) == 64


# ============================================================
# 二、filterAndRank 的过滤行为
# ============================================================

class TestFilterAndRankSmallFace:

    def test_relaxed_on_small_photo(self):
        """50px 的脸在 1280x960 上入库（这就是 DSC00214 那 14 张脸）"""
        hits, rejected = E.filterAndRank([makeFace(50)], 1280, 960)
        assert len(hits) == 1
        assert hits[0].shortEdge == 50
        assert rejected[E.REASON_TOO_SMALL] == 0

    def test_still_dropped_on_big_photo(self):
        """同一张 50px 的脸在 3456x2304 上**必须继续被丢**（口径不变）"""
        hits, rejected = E.filterAndRank([makeFace(50, 3456, 2304)], 3456, 2304)
        assert hits == []
        assert rejected[E.REASON_TOO_SMALL] == 1

    def test_adaptive_off_restores_old_behaviour(self):
        """关掉自适应 = 老口径：50px 一律丢"""
        hits, rejected = E.filterAndRank([makeFace(50)], 1280, 960,
                                         adaptiveMinEdge=False)
        assert hits == []
        assert rejected[E.REASON_TOO_SMALL] == 1

    def test_floor_still_drops_noise(self):
        """floor 之下还是丢：800x600 里 30px 的脸（旧手机上的人脸噪声）"""
        hits, _r = E.filterAndRank([makeFace(30, 800, 600)], 800, 600)
        assert hits == []
        hits, _r = E.filterAndRank([makeFace(32, 800, 600)], 800, 600)
        assert len(hits) == 1

    def test_quality_uses_effective_edge(self):
        """quality 的尺寸系数基准跟着生效门槛走 ——
        "刚过线的小脸排在候选队列后面"这条语义（0.25 档）不能被改动破坏。
        """
        # 1280x960 生效 38px：38/(4*38) = 0.25
        assert E.scoreQuality(1.0, 38, E.effectiveMinFaceEdge(1280, 960)) == 0.25
        # 大图生效 64px：64/(4*64) = 0.25
        assert E.scoreQuality(1.0, 64, E.effectiveMinFaceEdge(3456, 2304)) == 0.25

    def test_other_rules_unchanged(self):
        """同一次改动不许碰另外两条门槛：低置信 / 侧脸在大图小图上都照旧"""
        hits, rejected = E.filterAndRank([makeFace(50, detScore=0.5)], 1280, 960)
        assert hits == [] and rejected[E.REASON_LOW_DET] == 1
        hits, rejected = E.filterAndRank([makeFace(50, poseYaw=60.0)], 1280, 960)
        assert hits == [] and rejected[E.REASON_SIDE_FACE] == 1


# ============================================================
# 三、_Limits：两个后端的 detect 阶段与 filterAndRank 同口径
# ============================================================

class TestLimitsEdgeFor:

    def test_limits_share_the_same_function(self):
        """_Limits 的 minFaceEdge 保留 **None 语义**（None = 允许自适应）。

        这条钉的是"后端 detect 与 filterAndRank 不会各写一份门槛"：
        _Limits.edgeFor 与模块级 effectiveMinFaceEdge 必须给出同一个数。
        """
        for imgW, imgH in [(1280, 960), (3456, 2304), (800, 600)]:
            assert (E._Limits(None, None, None, None).edgeFor(imgW, imgH)
                    == E.effectiveMinFaceEdge(imgW, imgH))

    def test_limits_adaptive_off(self):
        lim = E._Limits(None, None, None, False)
        assert lim.edgeFor(1280, 960) == 64
        assert lim.edgeFor(3456, 2304) == 64

    def test_limits_explicit_edge(self):
        lim = E._Limits(None, 100, None, None)
        assert lim.minFaceEdge == 100
        assert lim.edgeFor(4000, 3000) == 100     # 显式值固定，与图尺寸无关
        assert lim.edgeFor(1280, 960) == 100

    def test_limits_keeps_none(self):
        """_Limits 必须**原样保留 None** —— 填成 64 会让两个后端一起失去自适应"""
        assert E._Limits(None, None, None, None).minFaceEdge is None


# ============================================================
# 四、上下限不变（改动前就有的硬约束，防止被人顺手改掉）
# ============================================================

class TestConfigUnchanged:

    @pytest.mark.parametrize("name,value,expected", [
        ("MIN_DET_SCORE", bs.MIN_DET_SCORE, 0.6),
        ("MIN_FACE_EDGE", bs.MIN_FACE_EDGE, 64),
        ("MAX_YAW", bs.MAX_YAW, 45),
        ("MIN_FACE_EDGE_FLOOR", bs.MIN_FACE_EDGE_FLOOR, 32),
    ])
    def test_value(self, name, value, expected):
        assert value == expected, name

    def test_floor_below_edge(self):
        """FLOOR 必须严格小于 MIN_FACE_EDGE，否则自适应等于没开"""
        assert 0 < bs.MIN_FACE_EDGE_FLOOR < bs.MIN_FACE_EDGE

    def test_ratio_range(self):
        assert 0 < bs.MIN_FACE_EDGE_RATIO <= 1
