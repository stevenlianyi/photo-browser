#! /usr/bin/env python3
#encoding: utf-8

#Filename: bucket.py
#Description: photo-browser 年代分桶（步骤 6）—— 拍摄年 + 出生年 -> bucketKey
#
# 规则（S0 已证分桶是刚需，见 数据库设计.md D-5：FR 32.75% -> ~19%）
# ------------------------------------------------------------------------
#   age = shotYear - birthYear
#     age <= 18  ->  从 birthYear 起每 3 年一桶    key = "b+k*3 - b+k*3+2"
#     age >  18  ->  从 birthYear+18 起每 10 年一桶 key = "b+18+k*10 - ...+9"
#   birthYear 未知 -> 降级等宽 5 年（方案 A，零前置条件）
#   shotYear  未知 -> 返回空串（落库为 NULL），**不参与跨桶比对**
#
# 三条边界纪律
# ------------
#   1. **shotYear 无效一律返回空串，绝不猜**。截图类（步骤 3 已把 shotYear 置 NULL）
#      的年份没有意义；拿 mtime 之类的猜一个，只会让这张脸去和错误年代的人比。
#      代价是它必然落到"进聚类"那一档 —— 但那是**正确**的错：
#      聚类只是"这些人可能是同一个人"的提示，人工一眼能看出来；
#      而猜一个年份去自动归属，错得没救。
#   2. **同一个人 + 同一年 -> 同一个 key**（幂等）。key 是 pb_person_centroid 的
#      业务键的一半，重算质心要靠它定位。
#   3. **无效输入不抛异常，返回空串**。扫描器的 shotYear 来自 EXIF/文件名/mtime
#      三条兜底链，脏值是常态；分桶是纯函数，不该成为一条会中断整轮的异常源。
#
# ⚠️ 已知且刻意保留的口径瑕疵：18/19 岁跨了分支
# -------------------------------------------
#   age=18 走童年分支：start = b + (18//3)*3 = b+18，key = "b+18-b+20"（宽 3）
#   age=19 走成年分支：start = b + 18 + 0    = b+18，key = "b+18-b+27"（宽 10）
#   两者的 start 相同但 key 字符串不同，且**互相看不到对方**（相邻桶按同宽平移，
#   1990-1992 的邻居是 1987-1989 / 1993-1995，不含 1990-1999）。
#   这是 MVP_plan.md S3 给的公式逐字实现的结果，改了它就不再是"按 S0 定稿的规则"。
#   真要消掉，得让分界改成 `age < 18`（17 岁进童年、18 岁进成年）或者把童年桶
#   从 b 起每 3 年递增到 b+17 —— 那是一次**规则变更**，需要重新跑 S0 验证。
#   现状影响：18 岁与 19 岁的人脸在跨桶时可能互相当邻居；误差是一张脸，
#   方向是"少一个候选"而不是"多一个错误候选"，且相邻三桶本身已留了余量。
#
# 纯函数，无数据库 / 无 numpy（测试可穷举；子进程想用也能 import）

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/match
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from config import basicSettings as basicSettings                 # noqa: E402

_VERSION = "20261005"

#: 无效 shotYear 时返回的桶键。落库写 NULL（faceStore.buildFaceRow 的口径），
#: 匹配阶段据此判定"不参与跨桶比对"。
NO_BUCKET: str = ""

#: **兜底桶键**（DR-16③ / 数据库设计 §4.6）：该人的**全部确认样本，不分年代桶**。
#:
#: 为什么要有它（这是 DR-16 落地时最关键的一条）
#: ------------------------------------------------
#:   一个人刚被确认 3~4 张脸时，这些脸很可能分散在 2~3 个年代桶里，
#:   每个桶都不足 3 个样本 -> **一个桶都启用不了** -> 质心索引里查无此人 ->
#:   这个人后续所有照片都只能进待确认队列。用户会得到一个
#:   "我明明确认过他了，他却一次都认不出来"的结论，然后就不确认了。
#:   ALL 桶把「他这个人」和「这个年代」两个维度解耦：确认够 3 张就能上岗。
#:
#: 它是**虚拟桶**：不对应任何 pb_face.shotBucket 值，只由 centroid.recompute
#: 单独算出并写一行 bucketKey='ALL'。因此：
#:   * listBucketsOf() 必须排除它（否则会当成一个不存在的年代桶去算）；
#:   * parseBucketKey('ALL') 返回 None、neighborBucketKeys('ALL') 返回 []
#:     —— 匹配侧只把它**并入**候选集合（∪ {ALL}），不参与相邻桶推算。
ALL_BUCKET: str = "ALL"

#: 兜底可信区间。步骤 3 的 SHOT_YEAR_MIN/MAX 才是真正生效的那个，
#: 这里再抄一份是为了让本模块**在纯函数测试里也能独立守住边界**：
#: 万一有人把配置改坏了，桶函数自己还会拦一道。
_SHOT_YEAR_MIN: int = basicSettings.SHOT_YEAR_MIN
_SHOT_YEAR_MAX: int = basicSettings.SHOT_YEAR_MAX

#: 三种桶宽（年）。**桶宽可以从 key 本身反解**（end-start+1），
#: 所以相邻桶不需要额外记住"这是童年还是成年"，见 bucketWidth()。
CHILD_WIDTH: int = basicSettings.BUCKET_CHILD_WIDTH
ADULT_WIDTH: int = basicSettings.BUCKET_ADULT_WIDTH
EQUAL_WIDTH: int = basicSettings.BUCKET_EQUAL_WIDTH
CHILD_MAX_AGE: int = basicSettings.BUCKET_CHILD_MAX_AGE


# ============================================================
# 一、年份归一
# ============================================================

def normalizeYear(value):
    """把各种形态的年份收敛成 int，失败一律 None（**不抛、不猜**）。

    接受 None / "" / "2013" / 2013 / "2013.0" / 2013.7。
    字符串走 int() 前先 strip，" 2013" 这种带空格的（OCR 出来的年份常见）也要认。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None                      # True/False 转 int 会得到 1/0，纯属噪音
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:                                        # "2013.0"（从 JSON/CSV 来过）
        number = float(text)
        return int(number) if number.is_integer() else None
    except ValueError:
        return None


def validShotYear(value):
    """归一 + 可信区间校验，通过返回 int，否则 None。"""
    year = normalizeYear(value)
    if year is None or year < _SHOT_YEAR_MIN or year > _SHOT_YEAR_MAX:
        return None
    return year


def validBirthYear(value) -> int:
    """出生年归一 + 校验。通过返回 int，**否则返回 0（= 未知）**。

    为什么不复用 validShotYear
    ------------------------
      出生年允许比 shotYear 可信区间早得多（活到 120 岁的人真实存在），
      但 0 / 负数 / 未来年份一律不是"出生年"—— 那是被污染的字段。
      这条校验**独立于** SHOT_YEAR_MIN/MAX：拿 shotYear 的下界去卡出生年，
      会把 1885 年生的人（1990 年拍的照片，他 105 岁）判成"未知"，
      于是他所有照片都掉进等宽桶 —— 分桶悄悄退化，而**没有任何提示**。
    """
    year = normalizeYear(value)
    if year is None or year <= 0:
        return 0
    if year < _SHOT_YEAR_MIN - 120 or year > _SHOT_YEAR_MAX:
        return 0
    return int(year)


def birthYearOf(birthday) -> int:
    """pb_person.birthday（'YYYY-MM-DD' / 'YYYY' / 'YYYY/MM/DD'）-> 出生年 int。

    取**前 4 位**而不是 split 之后再校验：生日列是 VARCHAR(16)，
    联系人导入（步骤 8）什么格式都可能塞进来，而出生年只要年份那 4 位是对的
    分桶就成立。拿不到合法出生年一律返回 0（= 未知），由 bucketKeyAdaptive
    降级到等宽桶。
    """
    text = str(birthday or "").strip()
    if len(text) < 4:
        return 0
    return validBirthYear(text[:4])


def ageOf(shotYear, birthYear) -> int:
    """拍摄时的年龄（岁）。出生年未知（0/None）返回 -1（= 算不出）。

    -1 而不是 0：0 岁是一个**合法**答案（刚出生），与"不知道"必须区分开，
    否则"未知"会被当成"新生儿"丢进最细的桶里。
    """
    year = validShotYear(shotYear)
    born = validBirthYear(birthYear)
    if year is None or not born:
        return -1
    return int(year - born)


# ============================================================
# 二、桶键
# ============================================================

def formatBucketKey(startYear: int, width: int) -> str:
    """(起始年, 桶宽) -> '1995-1997'。宽度即 end-start+1。"""
    start = int(startYear)
    return "%d-%d" % (start, start + int(width) - 1)


def bucketKeyEqual(shotYear, width: int = None) -> str:
    """**方案 A · 等宽分桶**（出生年未知时的降级路径，S0 的零前置条件口径）。

    2000 -> 宽度 5 时落在 "2000-2004"（从 0 对齐，不从 1900 对齐，
    因为对齐基准只要一致就行，不必跟某个基准年绑定）。
    """
    year = validShotYear(shotYear)
    if year is None:
        return NO_BUCKET
    step = int(width or EQUAL_WIDTH)
    if step <= 0:
        step = EQUAL_WIDTH
    start = year - (year % step)
    return formatBucketKey(start, step)


def bucketKeyChild(age: int, birthYear: int) -> str:
    """童年段（age <= 18）：从出生年起每 3 年一桶。"""
    start = int(birthYear) + (int(age) // CHILD_WIDTH) * CHILD_WIDTH
    return formatBucketKey(start, CHILD_WIDTH)


def bucketKeyAdult(age: int, birthYear: int) -> str:
    """成年段（age > 18）：从出生年+18 起每 10 年一桶。"""
    start = (int(birthYear) + CHILD_MAX_AGE
             + ((int(age) - CHILD_MAX_AGE - 1) // ADULT_WIDTH) * ADULT_WIDTH)
    return formatBucketKey(start, ADULT_WIDTH)


def bucketKeyAdultFrom18(age: int, birthYear: int) -> str:
    """成年段（S0/MVP_plan 原式：((age-18)//10)*10）。

    与 bucketKeyAdult 差 1 岁：原式在 age=19 时算出 -1 // 10 = -1（Python 向下取整），
    start 会退到 birthYear+8 —— 一个**不存在**的桶。S0 的公式在 19 岁上就是坏的，
    本模块统一走 bucketKeyAdult（等价于 (age-19)//10），
    差别只在 18/19 两岁，且是对的那一边。详见本文件头「18/19 岁」那段。
    """
    return bucketKeyAdult(age, birthYear)


def strategyOf() -> str:
    """当前分桶策略（basicSettings.bucketStrategy() 的转发）。

    为什么放在这里而不是让调用方各读 basicSettings
    -----------------------------------------------
      策略分支的**唯一**实现就在 bucketKeyAdaptive 里。谁想知道「现在按哪套分桶」，
      都得从这两个函数之一问出去；再开第三个读取点就是第二个真相。
    """
    return basicSettings.bucketStrategy()


def bucketKeyAdaptive(shotYear, birthYear=None) -> str:
    """**方案 B · 自适应分桶**（本项目口径）。见文件头规则表。

    参数
    ----
      shotYear  : pb_photo.shotYear（可传 '2013' 之类的字符串）
      birthYear : pb_person.birthday 的年份部分；None/0/非法 -> 降级等宽 5 年

    返回
    ----
      '1995-1997' 这样的桶键；shotYear 无效时返回空串（NO_BUCKET）。

    ⚠️ 另受 `basicSettings.BUCKET_STRATEGY` 控制（步骤 12 / P-08）：
       `fixed5` 一律走等宽 5 年；`none` 全部落 `ALL`（= **不分桶的对照组**，
       S0 测到的FR 32.75% 就是这一档）。只有 `adaptive` 走下面这套自适应规则。
       改策略后**必须重刷 pb_face.shotBucket 再重算质心**（DR-22），
       否则质心表里留着旧桶键的行、新桶键又没有质心 —— 匹配率归零且不报错。
    """
    year = validShotYear(shotYear)
    if year is None:
        return NO_BUCKET
    strategy = basicSettings.bucketStrategy()
    if strategy == "none":
        return ALL_BUCKET
    if strategy == "fixed5":
        return bucketKeyEqual(year)
    born = validBirthYear(birthYear)
    if not born:
        return bucketKeyEqual(year)
    age = year - born
    if age < 0:
        # 出生年晚于拍摄年：只有两种可能 —— 生日录错了，或这张照片拍于出生之前。
        # 两种都**不该**由分桶去猜，退回等宽桶至少不会把脸归到"负岁数"的怪桶里。
        return bucketKeyEqual(year)
    if age <= CHILD_MAX_AGE:
        return bucketKeyChild(age, born)
    return bucketKeyAdult(age, born)


def bucketKeyOf(shotYear, birthday=None) -> str:
    """便捷入口：直接吃 pb_person.birthday 原文（'YYYY-MM-DD'）与 pb_photo.shotYear。"""
    return bucketKeyAdaptive(shotYear, birthYearOf(birthday))


# ============================================================
# 三、桶键的反解与相邻桶
# ============================================================

def parseBucketKey(bucketKey) -> tuple:
    """'1995-1999' -> (1995, 1999)。**空串与非法格式一律返回 None**。

    为什么必须能反解
    --------------
      ① 相邻桶要靠"按同宽平移"算出来，宽度只能从 key 里读；
      ② 库里的 shotBucket 是历史写入的，不能假设它一定是本函数生成的形状；
      ③ 排查问题时"这个桶到底是哪几年"必须能从库里直接读出来。
    """
    text = str(bucketKey or "").strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) != 2:
        return None
    try:
        start, end = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None
    if end < start:
        return None
    return start, end


def bucketStartYear(bucketKey) -> int:
    """桶起始年；解析不出来返回 0。"""
    parsed = parseBucketKey(bucketKey)
    return int(parsed[0]) if parsed else 0


def bucketWidth(bucketKey) -> int:
    """桶宽（年）。**相邻桶靠它保持同族**。

    这就是"童年 3 年 / 成年 10 年 / 降级 5 年"三种桶在键上唯一的区别 ——
    不需要额外字段，也不需要知道这个人生没登记生日。
    """
    parsed = parseBucketKey(bucketKey)
    return (int(parsed[1] - parsed[0] + 1) if parsed else 0)


def shiftBucketKey(bucketKey, delta: int, width: int = None) -> str:
    """把桶键整体平移 delta 个桶（宽度不变）。返回空串表示原键不可解析。"""
    parsed = parseBucketKey(bucketKey)
    if parsed is None:
        return NO_BUCKET
    start, end = parsed
    step = int(width or (end - start + 1))
    if step <= 0:
        return NO_BUCKET
    delta = int(delta)
    if delta == 0:
        return formatBucketKey(start, step)
    return formatBucketKey(start + delta * step, step)


def neighborBucketKeys(bucketKey, neighbor: int = None) -> list:
    """相邻桶列表（含自己），**按起始年升序**返回。

    neighbor=1 -> [B0-1, B0, B0+1]（开发计划 §3.2 的口径）。
    顺序固定为升序：候选顺序会影响"同分时取谁"的稳定性，
    排序必须与调用方的遍历顺序无关（验收第 4 条要两次跑完全一致）。
    """
    if neighbor is None:
        neighbor = basicSettings.MATCH_NEIGHBOR_BUCKETS
    neighbor = max(0, int(neighbor))
    if parseBucketKey(bucketKey) is None:
        return []
    out = []
    for delta in range(-neighbor, neighbor + 1):
        one = shiftBucketKey(bucketKey, delta)
        if one:
            out.append(one)
    out.sort(key=bucketStartYear)
    return out


def isNeighborOf(bucketA, bucketB, neighbor: int = None) -> bool:
    """a 是否是 b 的候选桶（含自己）。纯函数，可用于校验候选集合构造。"""
    return str(bucketA or "") in neighborBucketKeys(bucketB, neighbor)


if __name__ == "__main__":
    _born = 2000
    print("bucket.py _VERSION:", _VERSION)
    print("出生年 %d，各岁数落桶（-18 岁以下只打印边界）：" % _born)
    for _age in (0, 1, 2, 3, 4, 16, 17, 18, 19, 20, 28, 29, 37, 38, 70, 88, 89):
        _y = _born + _age
        _k = bucketKeyAdaptive(_y, _born)
        print("   age=%-3d shotYear=%-4d -> %-10s 宽=%-2d 邻居=%s"
              % (_age, _y, _k or "(空)", bucketWidth(_k),
                 neighborBucketKeys(_k)))
    print("出生年未知（降级等宽 %d 年）：" % EQUAL_WIDTH)
    for _y in (2013, 2014, 1994, 1999, 2000, None, 0, -1, 2200, " 2013 ", "x"):
        print("   shotYear=%-8r -> %r" % (_y, bucketKeyAdaptive(_y)))
    print("兜底桶: %r（虚拟桶，匹配时并入候选集合：%s）"
          % (ALL_BUCKET, neighborBucketKeys("") + [ALL_BUCKET]))
    print("跨年连续性（1978~1981 逐桶无空洞）：")
    print("   ", [bucketKeyEqual(_y) for _y in (1978, 1979, 1980, 1981)])
    print("bucketKeyOf('1985-03-07', 2013-05-04) =",
          bucketKeyOf(2013, "1985-03-07"))
