#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_bucket.py
#Description: 步骤 6 单测 —— 年代分桶的**桶边界**
#
# 为什么边界必须写死成单测
# ----------------------
#   分桶是整条识别链路的**第一道分类**，它错了后面全错，而且**不报错**：
#   18 岁的人被分到"童年桶"，匹配阶段会拿他去和一堆小孩比，
#   分数自然低 -> 进聚类 -> 用户看到一个"莫名其妙的新人物"。
#   这种错没有任何异常、没有日志、没有堆栈，只表现为"识别不准"。
#
#   逐条钉住的口径（MVP_plan.md S3 + 验收清单第 1 条）
#     · age = 0 / 3 / 17 / 18 / 19 / 70 岁各自落在哪个桶
#     · 跨年连续性：桶不能有空洞、不能有重叠
#     · shotYear 缺失/非法 -> 空串（不参与跨桶比对），**绝不猜**
#     · 出生年未知 -> 降级等宽 5 年
#     · 相邻三桶 [B0-1, B0, B0+1] 且**同宽平移**（跨桶宽的混用是隐蔽的错）
#     · 幂等：同输入永远同输出（它是 pb_person_centroid 的业务键的一半）
#
# 本模块是纯函数，**不碰数据库、不碰文件系统**。

import pytest

from config import basicSettings as basicSettings
from engine.match import bucket as bucket


# ============================================================
# 一、基础归一
# ============================================================

class TestNormalizeYear:
    @pytest.mark.parametrize("raw,expect", (
        (2013, 2013), ("2013", 2013), (" 2013 ", 2013), ("2013.0", 2013),
        (2013.0, 2013),
    ))
    def test_accepts(self, raw, expect):
        assert bucket.normalizeYear(raw) == expect

    @pytest.mark.parametrize("raw", (
        None, "", "   ", "abc", "2013-05", True, False, [], {}, 2013.5,
        2013.7, float("nan"),
    ))
    def test_rejects(self, raw):
        """**不抛异常，返回 None**。分桶在整条扫描链路上跑，脏值是常态"""
        assert bucket.normalizeYear(raw) is None

    def test_fractional_float_year_is_rejected_not_truncated(self):
        """2013.7 **不取整成 2013**：截断就是猜。
        宁可当"年份不可信"退回等宽桶的降级路径，也不要造一个不存在的年份。"""
        assert bucket.normalizeYear(2013.7) is None
        assert bucket.bucketKeyAdaptive(2013.7, 1985) == bucket.NO_BUCKET

    def test_bool_is_not_a_year(self):
        """bool 是 int 的子类，不挡掉的话 True 会变成 1 年 —— 那是 1901 年之前"""
        assert bucket.normalizeYear(True) is None
        assert bucket.normalizeYear(False) is None

    @pytest.mark.parametrize("raw", (1899, 2101, 0, -2013, 99999))
    def test_out_of_trusted_range(self, raw):
        """超出 SHOT_YEAR_MIN/MAX 一律不可信：1990 年前的胶片扫件与 2100 的未来文件"""
        assert bucket.validShotYear(raw) is None

    def test_boundary_years_are_valid(self):
        lo, hi = basicSettings.SHOT_YEAR_MIN, basicSettings.SHOT_YEAR_MAX
        assert bucket.validShotYear(lo) == lo
        assert bucket.validShotYear(hi) == hi


class TestBirthYearOf:
    @pytest.mark.parametrize("raw,expect", (
        ("1985-03-07", 1985), ("1985/03/07", 1985), ("1985", 1985),
        ("1985-3-7", 1985), (" 1985 ", 1985), ("19850307", 1985),
    ))
    def test_takes_first_four(self, raw, expect):
        assert bucket.birthYearOf(raw) == expect

    @pytest.mark.parametrize("raw", (None, "", "   ", "abcd", "19", "0000"))
    def test_unknown_is_zero(self, raw):
        """0 = 未知（不是 None）：年龄算式里 0 天然表示"没有出生年" """
        assert bucket.birthYearOf(raw) == 0

    def test_very_old_birth_year_accepted(self):
        """1900 前也认（活到 120 岁的人真实存在），但明显不是人的要拒"""
        assert bucket.birthYearOf("1885-01-01") == 1885
        assert bucket.birthYearOf("1200-01-01") == 0

    def test_future_birth_year_rejected(self):
        assert bucket.birthYearOf("2200-01-01") == 0


class TestAgeOf:
    def test_normal(self):
        assert bucket.ageOf(2013, 1985) == 28

    def test_unknown_birth_is_minus_one(self):
        """-1 而不是 0：0 岁是合法答案（新生儿），与"不知道"必须分开"""
        assert bucket.ageOf(2013, None) == -1
        assert bucket.ageOf(2013, 0) == -1

    def test_zero_age_is_valid(self):
        assert bucket.ageOf(2013, 2013) == 0

    def test_unknown_shot_year(self):
        assert bucket.ageOf(None, 1985) == -1


# ============================================================
# 二、桶边界（本步最关键的一组）
# ============================================================

class TestBucketBoundary:
    """逐岁钉死。age = shotYear - birthYear。"""

    @pytest.mark.parametrize("age,expect", (
        (0, "2000-2002"),      # 0 岁：出生当年，第 0 个童年桶
        (1, "2000-2002"),
        (2, "2000-2002"),      # 0/1/2 三年一桶
        (3, "2003-2005"),      # 3 岁：跨到第 1 个童年桶（**边界**）
        (4, "2003-2005"),
        (5, "2003-2005"),
        (6, "2006-2008"),
        (15, "2015-2017"),
        (16, "2015-2017"),
        (17, "2015-2017"),     # 童年最后一年
        (18, "2018-2020"),     # **分界**：age<=18 仍走童年分支
        (19, "2018-2027"),     # **分界**：age>18 走成年分支，桶宽从 3 变 10
        (20, "2018-2027"),
        (27, "2018-2027"),
        (28, "2018-2027"),     # 成年第 0 桶覆盖 19~28 岁（10 年）
        (29, "2028-2037"),     # 跨到下一个成年桶
        (38, "2028-2037"),     # 第 1 桶覆盖 29~38 岁
        (39, "2038-2047"),
        (70, "2068-2077"),     # 70 岁：成年第 6 个桶
    ))
    def test_adaptive_by_age(self, age, expect):
        assert bucket.bucketKeyAdaptive(2000 + age, 2000) == expect

    def test_width_switches_at_18(self):
        """18 岁与 19 岁的**桶宽不同**（3 -> 10）。这是分桶设计的核心"""
        assert bucket.bucketWidth(bucket.bucketKeyAdaptive(2018, 2000)) == 3
        assert bucket.bucketWidth(bucket.bucketKeyAdaptive(2019, 2000)) == 10

    def test_eighteen_and_nineteen_share_start_year(self):
        """**已知且刻意保留的瑕疵**（见 bucket.py 文件头）：
        MVP_plan 的公式在 18/19 岁给出同一个起始年、不同宽度，
        于是 2018-2020（童年宽）与 2018-2027（成年宽）并存且互相看不到对方。
        这条用例把这个事实钉住 —— 将来若要改规则，这里必须一起改，
        并且重新跑 S0 验证（改的是匹配依据，不是实现细节）。"""
        k18 = bucket.bucketKeyAdaptive(2018, 2000)
        k19 = bucket.bucketKeyAdaptive(2019, 2000)
        assert bucket.bucketStartYear(k18) == bucket.bucketStartYear(k19) == 2018
        assert k18 != k19

    def test_adult_start_is_birth_year_plus_18(self):
        """成年段从 birthYear+18 起算，19 岁正好落在第 0 个成年桶"""
        for age in (19, 20, 27, 28):
            key = bucket.bucketKeyAdaptive(2000 + age, 2000)
            assert 2018 <= bucket.bucketStartYear(key) <= 2000 + age


class TestEqualFallback:
    """出生年未知 -> 降级等宽 5 年（方案 A，零前置条件）"""

    @pytest.mark.parametrize("shotYear,expect", (
        (2013, "2010-2014"), (2014, "2010-2014"), (2015, "2015-2019"),
        (1994, "1990-1994"), (1995, "1995-1999"), (1999, "1995-1999"),
        (2000, "2000-2004"),
    ))
    def test_width_is_five(self, shotYear, expect):
        assert bucket.bucketKeyAdaptive(shotYear) == expect
        assert bucket.bucketWidth(expect) == basicSettings.BUCKET_EQUAL_WIDTH

    @pytest.mark.parametrize("birth", (None, 0, "", "abc", -1, -1985, 2200))
    def test_invalid_birth_year_falls_back(self, birth):
        """负数/未来年份都是被污染的字段，不是"出生年"。

        注意 `-1`：**必须**显式挡掉 —— 写成 `normalizeYear(x) or 0` 时 -1 是真值，
        会被当成公元前 2 年，随后 age 算出 2014，桶整个跑偏，而且不报错。"""
        assert bucket.bucketKeyAdaptive(2013, birth) == "2010-2014"

    def test_birth_year_later_than_shot_year_falls_back(self):
        """生日录错（出生年晚于拍摄年）不猜，退回等宽桶"""
        assert bucket.bucketKeyAdaptive(2013, 2020) == "2010-2014"

    def test_custom_width(self):
        assert bucket.bucketKeyEqual(2013, 10) == "2010-2019"
        assert bucket.bucketKeyEqual(2013, 1) == "2013-2013"

    def test_zero_width_falls_back_to_five(self):
        """配错宽度不能让整条链路返回空串"""
        assert bucket.bucketKeyEqual(2013, 0) == "2010-2014"


class TestMissingShotYear:
    """shotYear 未知 -> 空串，**绝不猜年份**"""

    @pytest.mark.parametrize("shotYear", (
        None, 0, -1, 1899, 2101, "", "   ", "abc", [], {},
    ))
    def test_returns_empty(self, shotYear):
        assert bucket.bucketKeyAdaptive(shotYear, 1985) == bucket.NO_BUCKET == ""

    def test_screenshot_style_missing_year_does_not_cross_compare(self):
        """截图类（步骤 3 置 shotYear=NULL）：拿不到桶键 = 不参与跨桶比对"""
        assert bucket.bucketKeyAdaptive(None, 1985) == ""
        assert bucket.neighborBucketKeys("") == []

    def test_empty_key_never_equals_a_real_bucket(self):
        assert bucket.NO_BUCKET != bucket.bucketKeyAdaptive(2013, 1985)


class TestCrossYearContinuity:
    """跨年：桶不能有空洞、不能有重叠"""

    @staticmethod
    def _distinct(keys):
        """去掉连续重复的桶键。

        必须去重：等宽桶是「一年一个桶」的关系吗？不是 —— 连续 5 个年份落在
        **同一个**桶里。直接检查相邻两个年份的桶会得到 "1975-1979 -> 1975-1979"，
        那不是重叠，是同一个桶。要检查的是「桶序列」而不是「年份序列」。
        """
        out = []
        for one in keys:
            if not out or out[-1] != one:
                out.append(one)
        return out

    def test_equal_buckets_are_disjoint_and_contiguous(self):
        # 起点必须让 base..base+10 全部落在 SHOT_YEAR 可信区间内，
        # 否则后半段返回空串，parseBucketKey 给 None（那是另一条用例管的事）
        for base in (1978, 1985, 1999, 2000, 2013, 2090):
            keys = self._distinct([bucket.bucketKeyEqual(base + d)
                                   for d in range(11)])
            spans = [bucket.parseBucketKey(k) for k in keys]
            for i in range(len(spans) - 1):
                assert spans[i][1] + 1 == spans[i + 1][0], \
                    "等宽桶出现空洞/重叠: %s -> %s" % (keys[i], keys[i + 1])

    def test_adaptive_child_buckets_are_contiguous(self):
        """0~17 岁逐年走，桶必须首尾相接（不能跳过 3 的倍数那一年）"""
        keys = self._distinct([bucket.bucketKeyAdaptive(2000 + age, 2000)
                               for age in range(19)])
        spans = [bucket.parseBucketKey(k) for k in keys]
        for i in range(len(spans) - 1):
            assert spans[i][1] + 1 == spans[i + 1][0], \
                "童年桶出现空洞/重叠: %s -> %s" % (keys[i], keys[i + 1])

    def test_adult_buckets_are_contiguous(self):
        keys = self._distinct([bucket.bucketKeyAdaptive(2000 + age, 2000)
                               for age in range(19, 80)])
        spans = [bucket.parseBucketKey(k) for k in keys]
        for i in range(len(spans) - 1):
            assert spans[i][1] + 1 == spans[i + 1][0], \
                "成年桶出现空洞/重叠: %s -> %s" % (keys[i], keys[i + 1])

    def test_no_adult_bucket_skips_a_decade(self):
        """从 birthYear+18 起**每 10 年**一桶，中间不能整段跳过"""
        starts = [bucket.bucketStartYear(bucket.bucketKeyAdaptive(2000 + age, 2000))
                  for age in range(19, 100)]
        assert starts[0] == 2018
        deltas = set(b - a for a, b in zip(starts, starts[1:]) if b != a)
        assert deltas == {10}


class TestIdempotent:
    """幂等：同输入永远同输出（它是 pb_person_centroid 业务键的一半）"""

    def test_deterministic(self):
        for age in (0, 3, 17, 18, 19, 70):
            year = 2000 + age
            assert bucket.bucketKeyAdaptive(year, 2000) == \
                bucket.bucketKeyAdaptive(year, 2000)

    def test_key_fits_column(self):
        """bucketKey 必须放得进 pb_*.txt 的 VARCHAR(16)"""
        for age in (0, 18, 19, 70, 120):
            key = bucket.bucketKeyAdaptive(bucket.validShotYear(2000 + age), 2000)
            assert len(key) <= basicSettings.BUCKET_KEY_MAX_LEN

    def test_string_and_int_shot_year_agree(self):
        assert bucket.bucketKeyAdaptive("2013", 1985) == \
            bucket.bucketKeyAdaptive(2013, 1985)

    def test_bucketKeyOf_accepts_birthday_string(self):
        assert bucket.bucketKeyOf(2013, "1985-03-07") == \
            bucket.bucketKeyAdaptive(2013, 1985)


# ============================================================
# 三、解析、相邻桶、平移
# ============================================================

class TestParseBucketKey:
    @pytest.mark.parametrize("key,expect", (
        ("1995-1999", (1995, 1999)), ("2000-2002", (2000, 2002)),
        ("2010-2014", (2010, 2014)),
    ))
    def test_parse(self, key, expect):
        assert bucket.parseBucketKey(key) == expect

    @pytest.mark.parametrize("key", (
        "", None, "   ", "1995", "1995-1999-2003", "abc-def", "1999-1995",
        "1995-", "-1999", "1995 - 1999x",
    ))
    def test_reject(self, key):
        assert bucket.parseBucketKey(key) is None

    def test_start_and_width_on_bad_key(self):
        assert bucket.bucketStartYear("garbage") == 0
        assert bucket.bucketWidth("garbage") == 0

    def test_width_roundtrip(self):
        for key in ("2000-2002", "2018-2027", "2010-2014"):
            start, end = bucket.parseBucketKey(key)
            assert bucket.bucketWidth(key) == end - start + 1
            assert bucket.formatBucketKey(start, end - start + 1) == key


class TestNeighborBuckets:
    def test_three_buckets_including_self(self):
        assert bucket.neighborBucketKeys("2000-2002") == \
            ["1997-1999", "2000-2002", "2003-2005"]

    def test_sorted_by_start_year(self):
        """顺序固定为升序：候选顺序会影响"同分取谁"的稳定性"""
        keys = bucket.neighborBucketKeys("2000-2002")
        starts = [bucket.bucketStartYear(k) for k in keys]
        assert starts == sorted(starts)

    def test_same_width_shifts(self):
        """相邻桶**同宽平移**：3 年桶的邻居也是 3 年桶，绝不混进 10 年桶"""
        for key in ("2000-2002", "2018-2027", "2010-2014"):
            for one in bucket.neighborBucketKeys(key):
                assert bucket.bucketWidth(one) == bucket.bucketWidth(key), \
                    "%s 的邻居 %s 宽度变了" % (key, one)

    def test_self_is_always_in_the_set(self):
        for key in ("2000-2002", "2018-2027"):
            assert key in bucket.neighborBucketKeys(key)

    def test_neighbor_zero_is_self_only(self):
        assert bucket.neighborBucketKeys("2000-2002", 0) == ["2000-2002"]

    def test_wider_neighborhood(self):
        keys = bucket.neighborBucketKeys("2000-2002", 2)
        assert len(keys) == 5
        assert keys[0] == "1994-1996" and keys[-1] == "2006-2008"

    def test_bad_key_gives_empty(self):
        assert bucket.neighborBucketKeys("") == []
        assert bucket.neighborBucketKeys("garbage") == []
        assert bucket.neighborBucketKeys(None) == []

    def test_default_is_config_value(self):
        """缺省邻桶数来自 basicSettings（=1 -> 三桶）"""
        assert len(bucket.neighborBucketKeys("2000-2002")) == \
            2 * basicSettings.MATCH_NEIGHBOR_BUCKETS + 1

    def test_matches_sibling_around_self(self):
        """B0 的后一桶应当是 B1 的前一桶（否则候选集会出现空洞）"""
        keys = bucket.neighborBucketKeys("2000-2002")
        assert bucket.neighborBucketKeys("2003-2005")[0] == keys[1]


class TestShift:
    @pytest.mark.parametrize("delta,expect", (
        (-1, "1997-1999"), (0, "2000-2002"), (1, "2003-2005"),
    ))
    def test_shift(self, delta, expect):
        assert bucket.shiftBucketKey("2000-2002", delta) == expect

    def test_shift_bad_key(self):
        assert bucket.shiftBucketKey("garbage", 1) == ""
        assert bucket.shiftBucketKey("", 1) == ""

    def test_is_neighbor_of(self):
        assert bucket.isNeighborOf("2003-2005", "2000-2002")
        assert bucket.isNeighborOf("2000-2002", "2000-2002")
        assert not bucket.isNeighborOf("2010-2014", "2000-2002")
        assert not bucket.isNeighborOf("", "2000-2002")


if __name__ == "__main__":
    raise SystemExit("请用 pytest 运行：python -m pytest code/src/test/test_bucket.py -v")
