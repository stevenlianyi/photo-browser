#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_matcher.py
#Description: 步骤 6 单测 —— 匹配决策：相邻三桶取 max / 样本数门禁 / 三段式 / 可复现
#
# 本文件测的是**决策逻辑**，不依赖真实人脸：
#   用构造出来的单位向量当质心，用**已知**的余弦关系驱动每一条分支。
#   真实人脸的分布是 S0 的事（S0 数据由 tools/backtest_s0.py 负责），
#   决策逻辑必须能在没有真实数据的情况下被证明正确 —— 否则只能靠"跑正式库看看"。
#
# 五组用例对应验收清单
#   第 2 条  相邻三桶**取 max**，不是 mean
#   第 3 条  sampleCount < 3 的桶不参与匹配
#   第 4 条  三段式决策可复现（同一批跑两次完全一致）
#   第 5 条  保守/激进阈值切换，结果按预期变化且可回退
#   另      Top-5 候选同分按 displayName 排序（稳定）
#
# ⚠️ 修正步骤 R（DR-16③）之后：**候选桶集合 = 相邻三桶 ∪ {ALL 兜底桶}**，
#    且**没有 shotBucket 的脸不再直接判 no_bucket**（它的候选桶是 {ALL}）。
#    本文件里凡是断言 candidateBuckets 的地方都按新口径写。

import numpy as np
import pytest

from config import basicSettings as basicSettings
from engine.face import engine as faceEngine
from engine.match import bucket as bucket
from engine.match import centroid as centroid
from engine.match import matcher as matcher


# ============================================================
# 一、构造质心索引的夹具
# ============================================================

def unit(seed: int):
    """确定性的伪随机单位向量（512 维）。

    为什么要固定 seed：单测里最忌讳「随机但恰好通过」。
    固定 seed 之后，每次跑的向量都一样，失败可复现、结论可对照。
    """
    rng = np.random.RandomState(seed)
    return faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())


def mix(a, b, ratio: float):
    """a 与 b 的线性插值并归一化。ratio=0 -> a；1 -> b。"""
    vec = (1.0 - ratio) * a + ratio * b
    return faceEngine.l2normalize(vec)


def atSimilarity(face, target: float, seed: int = 777):
    """构造一个与 face **余弦恰为 target** 的单位向量。

    阈值类用例必须**精确**构造相似度：靠 mix(face, other, ratio) 去"猜"一个
    落在 [0.48, 0.62) 的分数，ratio 改一点点结果就跨过阈值，
    失败时也说不清是代码错了还是 ratio 选偏了 —— 而这两者的处理方式完全不同。
    """
    rng = np.random.RandomState(seed)
    other = faceEngine.l2normalize(rng.randn(1, basicSettings.EMBEDDING_DIM).ravel())
    other = faceEngine.l2normalize(other - float(other @ face) * face)  # 正交化
    target = float(min(max(target, -1.0), 1.0))
    rest = np.sqrt(max(0.0, 1.0 - target * target))
    return faceEngine.l2normalize(target * face + rest * other)


def makeIndex(specs, minSamples: int = 3, names=None) -> centroid.CentroidIndex:
    """按 [(personCode, bucketKey, sampleCount, vector), ...] 造一个内存索引。

    刻意**不连数据库**：决策逻辑的验证不该依赖建库与数据准备，
    否则「单测失败」和「环境没建好」永远分不清。
    """
    ordered = sorted(specs, key=lambda s: (s[0], s[1], s[2]))
    matrix = (np.ascontiguousarray(
        np.vstack([faceEngine.l2normalize(s[3]) for s in ordered]), dtype=np.float32)
        if ordered else np.zeros((0, basicSettings.EMBEDDING_DIM), dtype=np.float32))
    rows = [centroid.CentroidRow(i, i + 1, s[0], s[1], s[2])
            for i, s in enumerate(ordered)]
    persons = sorted(set(s[0] for s in ordered))
    display = dict(names or {})
    for code in persons:
        display.setdefault(code, code)
    return centroid.CentroidIndex(matrix, rows, persons, display, minSamples)


def faceOf(vec, bucketKey, faceCode="fc_test", photoCode="PH_test",
           personCode="P_assigned"):
    """造一张 pb_face 行。

    ⚠️ **默认 personCode 非空 = 已归属**（DR-21 / 修正步骤 R2）
    ------------------------------------------------
      未归属脸的候选桶被**放宽成「全部已启用桶」**了（等宽桶与自适应桶的键
      根本不对齐，按相邻三桶一把质心都取不到）。而本文件绝大多数用例测的
      是「已归属脸按相邻三桶取 max」这条**没变**的路径，所以默认给一个
      personCode；要测未归属路径就显式传 personCode=""。
    """
    return {"faceCode": faceCode, "photoCode": photoCode,
            "personCode": (personCode or None),
            "shotBucket": bucketKey,
            "embedding": faceEngine.encodeEmbedding(vec)}


# ============================================================
# 二、相邻三桶取 max（验收第 2 条）
# ============================================================

class TestMaxNotMean:
    """硬约束：score = max(相邻桶的全部质心)，**不是 mean**。"""

    def test_neighbor_bucket_wins_over_own_bucket(self):
        """本桶质心很像 0.30，**前邻桶**很像 0.90 -> 必须取 0.90。

        构造：脸 F 落在 2000-2002。它与 P01 的 1997-1999 质心夹角小（0.90），
        与 P01 的 2000-2002 质心几乎无关（0.30）。
        若误用 mean，会算出 0.60；而 0.60 在保守档(0.42~0.62)里恰好是"灰区"，
        于是这张**明显认得出**的脸被扔进待确认队列 —— 这正是取 mean 的真实危害。
        """
        face = unit(1)
        near = mix(face, unit(2), 0.10)            # cos ≈ 0.995 -> 取 0.90 那一档
        far = mix(face, unit(3), 0.95)             # 与 face 几乎无关
        index = makeIndex([("P01", "1997-1999", 5, near),
                           ("P01", "2000-2002", 5, far)])
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        mean = (float(near @ face) + float(far @ face)) / 2.0
        assert got.score > max(float(near @ face), float(far @ face)) - 1e-6
        assert got.score == pytest.approx(max(float(near @ face),
                                              float(far @ face)), abs=1e-5)
        assert got.score > mean, "取到的是 mean 而不是 max"
        assert got.decision == matcher.DECISION_AUTO

    def test_max_picks_the_best_of_three_buckets(self):
        """三桶分数 0.30 / 0.55 / 0.80 -> 取 0.80，且要说清是哪个桶认出来的"""
        face = unit(4)
        scores = {}
        rows = []
        # ratio 越小越接近 face（mix(face, other, 0.05) ≈ face）
        for offset, key, target in ((-1, "1997-1999", 0.30),
                                    (0, "2000-2002", 0.55),
                                    (1, "2003-2005", 0.80)):
            vec = atSimilarity(face, target, seed=10 + offset)
            scores[key] = float(vec @ face)
            rows.append(("P01", key, 4, vec))
        index = makeIndex(rows)
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        assert got.score == pytest.approx(max(scores.values()), abs=1e-4)
        assert got.candidateBuckets == ["1997-1999", "2000-2002", "2003-2005",
                                        bucket.ALL_BUCKET]
        assert got.topCandidates[0].bucketKey == "2003-2005"
        assert got.topCandidates[0].score == pytest.approx(0.80, abs=1e-4)

    def test_only_neighbor_buckets_are_candidates(self):
        """B0-2 的质心再像也不算候选（候选桶 = [B0-1, B0, B0+1] ∪ {ALL}）"""
        face = unit(7)
        index = makeIndex([("P01", "1994-1996", 5, mix(face, unit(8), 0.01)),
                           ("P01", "2000-2002", 5, mix(face, unit(9), 0.99))])
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        assert got.candidateBuckets == ["1997-1999", "2000-2002", "2003-2005",
                                        bucket.ALL_BUCKET]
        assert "1994-1996" not in got.candidateBuckets
        assert got.score < basicSettings.matchThresholds()[0], \
            "1994-1996 不该被算进来（否则分数会很高）"

    def test_max_across_persons_not_own_bucket_only(self):
        """本桶的 P01 与邻桶的 P02，取**全场**最大，不是本桶优先"""
        face = unit(11)
        index = makeIndex([("P01", "2000-2002", 5, mix(face, unit(12), 0.70)),
                           ("P02", "2003-2005", 5, mix(face, unit(13), 0.02))])
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        assert got.personCode == "P02"
        assert got.decision == matcher.DECISION_AUTO

    def test_two_buckets_of_one_person_reduce_to_max(self):
        """同一个人在候选桶里有两条质心 -> 取 max（不是平均）"""
        face = unit(14)
        a = mix(face, unit(15), 0.05)
        b = mix(face, unit(16), 0.80)
        index = makeIndex([("P01", "1997-1999", 4, a), ("P01", "2000-2002", 4, b)])
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        assert got.score == pytest.approx(float(a @ face), abs=1e-5)
        assert got.score > float(b @ face)
        assert got.score > (float(a @ face) + float(b @ face)) / 2.0


# ============================================================
# 三、sampleCount 门禁（验收第 3 条）
# ============================================================

class TestMinSamplesGate:
    def test_centroid_not_enabled_below_threshold(self):
        """样本数不足 -> 质心不启用（返回 None），不参与匹配"""
        rows = [("P01", "2000-2002", 1, unit(21)),
                ("P01", "1997-1999", 2, unit(22))]
        for sampleCount, key in ((1, "2000-2002"), (2, "1997-1999")):
            index = makeIndex([r for r in rows if r[2] >= 3] or [("P00", key, 3, unit(99))])
            assert sampleCount < basicSettings.MIN_CENTROID_SAMPLES

    def test_min_samples_comes_from_config(self):
        assert centroid.MIN_SAMPLES == basicSettings.MIN_CENTROID_SAMPLES == 3

    def test_a_centroid_built_from_one_sample_would_always_match(self):
        """**为什么**这条门禁是正确性问题而不是精度问题：
        1 个样本的"质心"就是那个样本本身，cosine(face, centroid) = 1.0。
        若不禁用，任何一张脸都能匹配上它自己那张脸 —— 而那张脸
        往往是张证件照/摆拍，FA 直接 100%。"""
        sample = unit(23)
        assert float(sample @ sample) == pytest.approx(1.0, abs=1e-6)
        # 门禁在写库侧生效：recompute 对 <3 样本的桶写 NULL（test_review_ops 覆盖落库）

    def test_sample_count_does_not_bias_score(self):
        """**归一化均值**的硬要求：桶内样本数不得影响分数。

        不归一化的话，3 个样本的均值长度约 0.9、30 个样本约 0.5，
        分数会朝「样本多的人」倾斜 —— 而用户完全看不出来。
        """
        face = unit(24)
        base = mix(face, unit(25), 0.10)
        few = faceEngine.l2normalize(base * 3.0 / 3.0)          # 3 个同向样本的均值
        many = faceEngine.l2normalize(base * 30.0 / 30.0)       # 30 个同向样本的均值
        index = makeIndex([("P01", "2000-2002", 3, few),
                           ("P02", "2000-2002", 30, many)])
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        top = dict((c.personCode, c.score) for c in got.topCandidates)
        assert top["P01"] == pytest.approx(top["P02"], abs=1e-5), \
            "样本多的桶分数偏高 -> 均值没归一化"


# ============================================================
# 四、三段式决策（边界口径）
# ============================================================

class TestThreeWayDecision:
    @pytest.mark.parametrize("score,expect", (
        (0.99, matcher.DECISION_AUTO),
        (0.62, matcher.DECISION_AUTO),      # == T_HIGH 算自动（>= 口径）
        (0.6199, matcher.DECISION_REVIEW),
        (0.42, matcher.DECISION_REVIEW),    # == T_LOW 算灰区（>= 口径）
        (0.4199, matcher.DECISION_CLUSTER),
        (0.30, matcher.DECISION_CLUSTER),
        (0.0, matcher.DECISION_CLUSTER),
        (-0.2, matcher.DECISION_CLUSTER),
    ))
    def test_boundaries(self, score, expect):
        low, high = basicSettings.matchThresholds("conservative")
        decision, _reason = matcher.decide(score, low, high)
        assert decision == expect

    def test_none_score_is_cluster(self):
        assert matcher.decide(None)[0] == matcher.DECISION_CLUSTER

    def test_no_bucket_uses_all_fallback(self):
        """**验收第 10 条**（DR-16③）：shotBucket 为空的脸 -> 候选桶 = {ALL}。

        修正前这里是「一律进聚类、绝不自动归属」，而那张脸连一个分数都拿不到。
        现在的口径：没有拍摄年份 = **没有年代信息**，而ALL 桶恰恰是唯一
        不含年代假设的向量，所以它是最合适的候选。
        """
        face = unit(31)
        got = matcher.match({"faceCode": "fc_s", "shotBucket": None,
                             "personCode": "P01",
                             "embedding": faceEngine.encodeEmbedding(face)},
                            index=makeIndex([("P01", bucket.ALL_BUCKET, 5, face)]))
        assert got.candidateBuckets == [bucket.ALL_BUCKET]
        assert got.decision == matcher.DECISION_AUTO
        assert got.personCode == "P01"
        assert got.topCandidates[0].bucketKey == bucket.ALL_BUCKET
        assert got.score == pytest.approx(1.0, abs=1e-5)

    def test_no_bucket_and_no_all_centroid_is_cluster(self):
        """候选桶里连一个启用的质心都没有时才是 no_bucket（原来是无条件判它）"""
        got = matcher.match({"faceCode": "fc_s", "shotBucket": None,
                             "personCode": "P01",
                             "embedding": faceEngine.encodeEmbedding(unit(31))},
                            index=makeIndex([("P01", "2000-2002", 5, unit(31))]))
        assert got.decision == matcher.DECISION_CLUSTER
        assert got.reason == matcher.REASON_NO_BUCKET
        assert got.score is None
        assert got.candidateBuckets == [bucket.ALL_BUCKET], \
            "候选桶必须如实报告实际参与的桶（这里是 {ALL}）"

    def test_empty_centroid_store_is_cluster(self):
        got = matcher.match(faceOf(unit(32), "2000-2002"),
                            index=makeIndex([]))
        assert got.decision == matcher.DECISION_CLUSTER
        assert got.reason == matcher.REASON_NO_CENTROID

    def test_missing_embedding_is_cluster(self):
        got = matcher.match({"faceCode": "fc_x", "shotBucket": "2000-2002",
                             "embedding": None},
                            index=makeIndex([("P01", "2000-2002", 5, unit(33))]))
        assert got.reason == matcher.REASON_NO_EMBEDDING
        assert got.decision == matcher.DECISION_CLUSTER

    def test_wrong_size_embedding_is_rejected(self):
        """长度不对的向量直接判脏，不"截断前 512 位"凑合用"""
        got = matcher.match({"faceCode": "fc_x", "shotBucket": "2000-2002",
                             "embedding": b"\x00" * 1024},
                            index=makeIndex([("P01", "2000-2002", 5, unit(34))]))
        assert got.reason == matcher.REASON_NO_EMBEDDING

    def test_low_score_reason(self):
        face = unit(35)
        index = makeIndex([("P01", "2000-2002", 5, mix(face, unit(36), 0.97))])
        got = matcher.match(faceOf(face, "2000-2002"), index=index)
        assert got.reason == matcher.REASON_LOW_SCORE
        assert got.personCode == "", "非自动归属不该写 personCode"

    def test_result_records_thresholds_used(self):
        """结果里记下实际用的阈值与预设 —— 否则事后无法解释为什么没自动归属"""
        got = matcher.match(faceOf(unit(37), "2000-2002"),
                            index=makeIndex([("P01", "2000-2002", 5, unit(37))]),
                            preset="aggressive")
        assert got.preset == "aggressive"
        assert (got.tLow, got.tHigh) == basicSettings.matchThresholds("aggressive")


# ============================================================
# 五、阈值切换（验收第 5 条）
# ============================================================

class TestThresholdSwitch:
    def _indexAndFace(self):
        """造一张「保守档判不了、激进档能自动归属」的脸。

        分数 0.55 落在 [激进 T_HIGH 0.48, 保守 T_HIGH 0.62) 之间 ——
        也正好在保守 T_LOW 0.42 之上，所以保守档判「待确认」而不是「进聚类」。
        """
        face = unit(41)
        index = makeIndex([("P01", "2000-2002", 5, atSimilarity(face, 0.55))])
        return face, index

    def test_conservative_vs_aggressive(self):
        face, index = self._indexAndFace()
        cons = matcher.match(faceOf(face, "2000-2002"), index=index,
                             preset="conservative")
        aggr = matcher.match(faceOf(face, "2000-2002"), index=index,
                             preset="aggressive")
        assert cons.decision == matcher.DECISION_REVIEW
        assert aggr.decision == matcher.DECISION_AUTO
        assert cons.score == pytest.approx(aggr.score, abs=1e-6), \
            "**分数不该随阈值变**（变的是判定，不是相似度）"

    def test_switch_back_is_reversible(self):
        face, index = self._indexAndFace()
        first = matcher.match(faceOf(face, "2000-2002"), index=index,
                              preset="aggressive").toDict()
        matcher.match(faceOf(face, "2000-2002"), index=index, preset="conservative")
        again = matcher.match(faceOf(face, "2000-2002"), index=index,
                              preset="aggressive").toDict()
        assert first == again

    def test_every_preset_has_a_non_empty_grey_zone(self):
        for name in basicSettings.MATCH_THRESHOLD_PRESETS:
            low, high = basicSettings.matchThresholds(name)
            assert low < high, "%s 的灰区为空" % name
            mid = (low + high) / 2.0
            assert matcher.decide(mid, low, high)[0] == matcher.DECISION_REVIEW

    def test_conservative_is_strictest(self):
        """保守档必须两头都更严：T_HIGH 更高（少自动）、T_LOW 更高（少聚类）"""
        cLow, cHigh = basicSettings.matchThresholds("conservative")
        aLow, aHigh = basicSettings.matchThresholds("aggressive")
        assert cHigh > aHigh and cLow > aLow

    def test_baseline_preset_matches_legacy_constants(self):
        """s0 档必须与 T_HIGH/T_LOW 逐位一致（步骤 5/6 的回归都按它跑）"""
        assert basicSettings.matchThresholds("s0") == \
            (basicSettings.T_LOW, basicSettings.T_HIGH)

    def test_unknown_preset_raises(self):
        """配错预设名必须**立刻炸**，不能静默回落（那正是"配错了看不出来"的来源）"""
        with pytest.raises(KeyError):
            basicSettings.matchThresholds("turbo")
        with pytest.raises(KeyError):
            matcher.match(faceOf(unit(43), "2000-2002"),
                          index=makeIndex([("P01", "2000-2002", 5, unit(43))]),
                          preset="turbo")

    def test_config_default_preset_is_conservative(self):
        """缺省保守：错分的代价远高于多确认几张"""
        assert basicSettings.MATCH_THRESHOLD_PRESET == "conservative"


# ============================================================
# 六、Top-5 候选与稳定性（验收第 4 条）
# ============================================================

class TestTopCandidates:
    def test_top_n_is_five_by_default(self):
        assert basicSettings.MATCH_TOP_CANDIDATES == 5
        face = unit(51)
        rows = [("P%02d" % i, "2000-2002", 5,
                 mix(face, unit(60 + i), 0.02 * i)) for i in range(8)]
        got = matcher.match(faceOf(face, "2000-2002"), index=makeIndex(rows))
        assert len(got.topCandidates) == 5
        assert [c.personCode for c in got.topCandidates] == \
            ["P00", "P01", "P02", "P03", "P04"]

    def test_ties_broken_by_display_name(self):
        """同分按 displayName 排（personCode 是随机 uuid，按它排对用户是随机的）

        ⚠️ 排的是 **Unicode 码位序**，不是拼音序。
        为什么不引 pypinyin：① 同分只是兜底排序，不是主排序，不值得为它加依赖；
        ② 码位序是**稳定且全序**的，拼音序在同名/多音字上还会引入另一套不确定性。
        中文用户看到的顺序可能不是拼音顺序，但**两次跑一定一样** ——
        而这正是这条规则存在的理由。
        """
        face = unit(52)
        same = mix(face, unit(53), 0.10)          # 5 个人共用**同一条**质心 -> 必然同分
        rows = [("P_z", "2000-2002", 5, same), ("P_a", "2000-2002", 5, same),
                ("P_m", "2000-2002", 5, same), ("P_b", "2000-2002", 5, same),
                ("P_c", "2000-2002", 5, same)]
        names = {"P_z": "张伟", "P_a": "李娜", "P_m": "王芳", "P_b": "陈磊",
                 "P_c": "张伟"}
        got = matcher.match(faceOf(face, "2000-2002"),
                            index=makeIndex(rows, names=names))
        # 码位序：张(5F20) < 李(674E) < 王(738B) < 陈(9648)；同名再按 personCode
        assert [c.displayName for c in got.topCandidates] == \
            ["张伟", "张伟", "李娜", "王芳", "陈磊"]
        assert [c.personCode for c in got.topCandidates] == \
            ["P_c", "P_z", "P_a", "P_m", "P_b"]

    def test_tie_fully_deterministic_across_runs(self):
        """同分 + 同名时靠 personCode 兜底，**重跑顺序完全一致**"""
        face = unit(54)
        same = mix(face, unit(55), 0.10)
        rows = [("P_%d" % i, "2000-2002", 5, same) for i in range(9)]
        names = dict(("P_%d" % i, "同名") for i in range(9))
        first = [c.personCode for c in
                 matcher.match(faceOf(face, "2000-2002"),
                               index=makeIndex(rows, names=names)).topCandidates]
        for _ in range(3):
            again = [c.personCode for c in
                     matcher.match(faceOf(face, "2000-2002"),
                                   index=makeIndex(rows, names=names)).topCandidates]
            assert again == first

    def test_ties_do_not_depend_on_input_order(self):
        """候选的输入顺序不同 -> 排序结果必须相同"""
        face = unit(56)
        same = mix(face, unit(57), 0.10)
        rows = [("P_%d" % i, "2000-2002", 5, same) for i in range(6)]
        names = dict(("P_%d" % i, "同名") for i in range(6))
        forward = [c.personCode for c in
                   matcher.match(faceOf(face, "2000-2002"),
                                 index=makeIndex(rows, names=names)).topCandidates]
        backward = [c.personCode for c in
                    matcher.match(faceOf(face, "2000-2002"),
                                  index=makeIndex(list(reversed(rows)),
                                                  names=names)).topCandidates]
        assert forward == backward

    def test_top_candidates_serializable(self):
        face = unit(58)
        index = makeIndex([("P01", "2000-2002", 5, mix(face, unit(59), 0.05))],
                          names={"P01": "爸爸"})
        data = matcher.match(faceOf(face, "2000-2002"), index=index).toDict()
        assert data["topCandidates"][0]["displayName"] == "爸爸"
        assert "score" in data["topCandidates"][0]
        assert data["bucketKey"] == "2000-2002"


# ============================================================
# 七、批量与可复现（验收第 4 条）
# ============================================================

class TestBatchAndReproducibility:
    def _fixture(self, count: int = 40):
        rng = np.random.RandomState(99)
        faces, rows = [], []
        for i in range(6):
            rows.append(("P%02d" % i, "2000-2002", 5 + i, unit(200 + i)))
            rows.append(("P%02d" % i, "1997-1999", 4, unit(300 + i)))
        index = makeIndex(rows, names=dict(("P%02d" % i, "人%02d" % i)
                                            for i in range(6)))
        for i in range(count):
            base = unit(400 + (i % 6))
            vec = mix(base, unit(500 + i), 0.05 * (i % 7))
            bucketKey = "2000-2002" if i % 3 else "1997-1999"
            faces.append(faceOf(vec, bucketKey, "fc_%03d" % i, "PH_%03d" % i))
        del rng
        return faces, index

    def test_batch_equals_single(self):
        """批量（矩阵乘）与逐张（点积）必须**逐位一致**。

        两者实现路径完全不同（BLAS 矩阵乘 vs 512 维点积），
        一致才说明 reduceat 的段边界算对了 —— 这段是最容易错且最难看出错的地方。
        """
        faces, index = self._fixture(30)
        batch = matcher.matchMany(faces, index=index)
        for i, one in enumerate(faces):
            single = matcher.match(one, index=index)
            assert batch[i].faceCode == single.faceCode
            assert batch[i].decision == single.decision
            assert batch[i].personCode == single.personCode
            assert batch[i].score == pytest.approx(single.score, abs=1e-5)
            assert [c.personCode for c in batch[i].topCandidates] == \
                [c.personCode for c in single.topCandidates]

    def test_two_runs_identical(self):
        """同一批脸跑两次，结果**完全一致**（验收第 4 条）"""
        faces, index = self._fixture(40)
        first = [r.toDict() for r in matcher.matchMany(faces, index=index)]
        second = [r.toDict() for r in matcher.matchMany(faces, index=index)]
        assert first == second

    def test_result_length_and_order_match_input(self):
        """结果**等长同序**：调用方要 zip 回去写库，顺序错了会张冠李戴且不报错"""
        faces, index = self._fixture(25)
        got = matcher.matchMany(faces, index=index)
        assert len(got) == len(faces)
        for i, one in enumerate(faces):
            assert got[i].faceCode == faces[i]["faceCode"]

    def test_dirty_rows_keep_their_slot(self):
        """脏行（无桶/无向量）**占位**返回 cluster，绝不静默缩短列表"""
        faces = [faceOf(unit(600), "2000-2002", "fc_ok"),
                 {"faceCode": "fc_novec", "shotBucket": "2000-2002",
                  "personCode": "P01",
                  "embedding": None},
                 {"faceCode": "fc_nobucket", "shotBucket": None,
                  "personCode": "P01",
                  "embedding": faceEngine.encodeEmbedding(unit(603))},
                 faceOf(unit(601), "2000-2002", "fc_ok2")]
        index = makeIndex([("P01", "2000-2002", 5, unit(602))])
        got = matcher.matchMany(faces, index=index)
        assert len(got) == 4
        # 无向量：报**真实原因**（no_embedding）而不是「没有桶」——
        # 两个原因都退回聚类，但排障方向完全不同，混淆它们会让人白查年份
        assert got[1].faceCode == "fc_novec"
        assert got[1].reason == matcher.REASON_NO_EMBEDDING
        assert got[1].decision == matcher.DECISION_CLUSTER
        # 无桶但有向量：候选桶 {ALL}，而索引里没有 ALL 桶 -> no_bucket
        assert got[2].faceCode == "fc_nobucket"
        assert got[2].reason == matcher.REASON_NO_BUCKET
        assert got[2].candidateBuckets == [bucket.ALL_BUCKET]
        assert got[2].decision == matcher.DECISION_CLUSTER

    def test_empty_input(self):
        assert matcher.matchMany([], index=makeIndex([])) == []

    def test_batch_chunk_size_does_not_change_result(self):
        """分片大小只影响内存，**不该影响结果**"""
        faces, index = self._fixture(20)
        one = [r.toDict() for r in matcher.matchMany(faces, index=index,
                                                     batchFaces=1000)]
        many = [r.toDict() for r in matcher.matchMany(faces, index=index,
                                                      batchFaces=3)]
        assert one == many

    def test_neighbor_setting_changes_candidates(self):
        faces, index = self._fixture(10)
        one = matcher.matchMany(faces, index=index, neighbor=0)[0]
        three = matcher.matchMany(faces, index=index, neighbor=1)[0]
        # neighbor 只改**年代桶**的宽度；ALL 兜底桶恒定并入（DR-16③）
        assert one.candidateBuckets in (["2000-2002", bucket.ALL_BUCKET],
                                        ["1997-1999", bucket.ALL_BUCKET])
        assert len(three.candidateBuckets) == 4
        assert bucket.ALL_BUCKET in three.candidateBuckets

    def test_summarize(self):
        faces, index = self._fixture(12)
        stat = matcher.summarize(matcher.matchMany(faces, index=index))
        assert stat["total"] == 12
        assert stat["auto"] + stat["review"] + stat["cluster"] == 12


# ============================================================
# 八、输入容错
# ============================================================

class TestTolerance:
    def test_centroid_buckets_from_config(self):
        """桶键完全由 bucket 模块决定，matcher 不自己拼字符串"""
        assert matcher.bucketKeyOfFace({"shotBucket": "2000-2002"}) == "2000-2002"
        assert matcher.bucketKeyOfFace({"shotBucket": None}) == ""
        assert matcher.bucketKeyOfFace({}) == ""
        assert matcher.bucketKeyOfFace("2000-2002") == "2000-2002"

    def test_bucket_keys_are_interoperable(self):
        """matcher 用的候选桶 == bucket 模块算出来的相邻桶 ∪ {ALL}

        （**两处口径不许分叉**：候选桶集合只由 matcher.candidateBucketKeys 一处产生，
          手工在这里拼一遍就等于埋了一个「两处慢慢漂移」的雷）
        """
        key = bucket.bucketKeyAdaptive(2013, 1985)
        faces = [faceOf(unit(900), key, "fc")]
        got = matcher.matchMany(faces, index=makeIndex(
            [("P01", key, 5, unit(901))]))
        assert got[0].candidateBuckets == matcher.candidateBucketKeys(key)
        assert got[0].candidateBuckets == \
            sorted(bucket.neighborBucketKeys(
                key, basicSettings.MATCH_NEIGHBOR_BUCKETS) + [bucket.ALL_BUCKET])

    def test_accepts_ndarray_and_bytes(self):
        face = unit(701)
        index = makeIndex([("P01", "2000-2002", 5, mix(face, unit(702), 0.03))])
        a = matcher.match(faceOf(face, "2000-2002"), index=index)
        b = matcher.match({"faceCode": "fc", "shotBucket": "2000-2002",
                           "embedding": face}, index=index)
        c = matcher.match({"faceCode": "fc", "shotBucket": "2000-2002",
                           "embedding": face.tobytes()}, index=index)
        assert a.score == pytest.approx(b.score, abs=1e-6)
        assert a.score == pytest.approx(c.score, abs=1e-6)

    def test_none_input(self):
        got = matcher.match(None, index=makeIndex([]))
        assert got.decision == matcher.DECISION_CLUSTER

    def test_face_embedding_helper(self):
        assert matcher.faceEmbedding(None) is None
        assert matcher.faceEmbedding({"embedding": None}) is None
        assert matcher.faceEmbedding(np.zeros(10)) is None
        assert matcher.faceEmbedding(unit(703)) is not None

    def test_decide_uses_config_when_not_given(self):
        low, high = basicSettings.matchThresholds()
        assert matcher.decide(high)[0] == matcher.DECISION_AUTO
        assert matcher.decide(low - 1e-6)[0] == matcher.DECISION_CLUSTER


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行：python -m pytest code/src/test/test_matcher.py -v")
