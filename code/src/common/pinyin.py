#! /usr/bin/env python3
#encoding: utf-8

#Filename: pinyin.py
#Description: 人物姓名的拼音检索串生成（pb_person.displayNamePinyin 的**唯一**写入口）
#
# 为什么需要它
# ------------
#   `GET /api/persons?keyword=` 原来只在 displayName / familyName / email / phone
#   上做 `LIKE %kw%`。这意味着**用户必须打出汉字**才能搜到自己的人 ——
#   而「鲁文」在一千张待确认的脸里靠肉眼比对几乎不可能被认出来。
#   界面看起来有个搜索框，却只认汉字，这就是「搜索没起作用」。
#
# 这一列存的是什么
# ----------------
#   **不是**一个拼音串，而是**一组空格分隔的检索 token**，一次 LIKE 全命中：
#       王小明        -> "wangxiaoming wxm"
#       王小明(2)     -> "wangxiaoming2 wxm2"
#       王小明（姓王）-> "wangxiaoming wxm wang"
#       Lucy Chen     -> "lucychen lc"
#       Alice 王小明  -> "alicewangxiaoming awxm"
#       未命名-ab12cd -> "weimingmingab12cd wmmab"
#
#   为什么同时存「全拼」和「首字母」：
#     用户实际会打的两种东西是「wangxiaoming」和「wxm」，
#     只存全拼的话输 wxm 零结果，只存首字母的话输 wang 零结果。
#     两者都放，且**用空格隔开**（不连写）：
#       `LIKE '%wang%'` 命中全拼 token；
#       `LIKE '%wxm%'` 命��首字母 token；
#       `LIKE '%lwn%'` **不该**命中 —— 连写会把两个不存在的音节缝成
#         「看起来像拼音」的东西，那才是真的骗人。宁可少召回，不要假召回。
#
#   姓氏（familyName）单独出一个全拼 token：
#     displayName 常常只写名（"小明" + familyName "王"），
#     只对 displayName 转拼音的话输 "wang" 依然零结果。
#
# 为什么这里用拼音、而 engine/match 不用
# ------------------------------------
#   engine/match/matcher.py 的注释已经明确拒绝为「同分兜底排序」引拼音库
#   （那里排的是 Unicode 码位序，是有意的）。本模块与之不冲突：
#     **搜索**要的是召回 —— 用户正在敲字，必须立刻有结果；
#     **排序**要的是确定性 —— 库里没这一列就排不了，还得全表算一遍。
#   一个解决召回，一个解决确定性，别混。
#
# 纪律
# ----
#   1. **纯函数，不 import database.\***（与 engine/match 的纪律一致）；
#   2. 缺 pypinyin 时**降级但不崩**：只保留 ASCII/数字原样，界面照常能用，
#      只是拼音检索失效；且只在 import 时 warning 一次，不刷屏。

import re
import sys
import os

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                          # noqa: E402

_VERSION = "20261007"

#: 分词保留字符：中文（含扩展 A / 基本区）、字母、数字。
#: 其余（空格、括号、连字符、冒号…）一律当分隔符。
_SPLIT_RE = re.compile("[^0-9a-zA-Z㐀-䶿一-鿿]+")

#: displayNamePinyin 的截断上限。取 512 而不是 128：
#: 一个汉字最长的全拼 6 个字母（"庄" -> zhuang），128 字的名字理论上能到 768。
#: 真实姓名 2–5 字，实际占用 <40 字符；留 512 是为了**基本永不截断** ——
#: 截在 token 中间会造出一个真实的假匹配（见上面「宁可少召回」）。
_MAX_LEN = 512

#: 首字母 token 的最小长度。**1 个字母不算缩写，是噪声**：
#: 单字名「王」的 initials 是 "w"，而 `LIKE '%w%'` 会命中几乎每一个
#: 带 w 的拼音（全拼里 w 本来就极常见）—— 那不是召回，是刷屏。
_MIN_INITIALS_LEN = 2

try:
    from pypinyin import lazy_pinyin
    _HAS_PINYIN = True
except ImportError:                                            # pragma: no cover
    _HAS_PINYIN = False
    lazy_pinyin = None
    misc.setLogNew("pinyin", "photolib.log").warning(
        "未安装 pypinyin：人物名的拼音检索不可用，库里只剩汉字能搜。"
        "修复：pip install pypinyin")


def _one(text) -> tuple:
    """一段文本 -> (全拼连写, 首字母连写)。

    非汉字部分（英文名、数字、"未命名-ab12" 这类兜底名）按小写原样保留，
    这样「输 lu 能命中 Lucy」也成立 —— 库里本来就有一批英文名的人物。
    """
    if not _HAS_PINYIN:
        pieces = [p for p in _SPLIT_RE.split(str(text).lower()) if p]
        return "".join(pieces), "".join(p[0] for p in pieces)
    full, initials = [], []
    for token in lazy_pinyin(str(text)):
        for piece in _SPLIT_RE.split(str(token).lower()):
            if not piece:
                continue
            full.append(piece)
            initials.append(piece[0])
    return "".join(full), "".join(initials)


def personPinyin(displayName, familyName="") -> str:
    """pb_person.displayNamePinyin 的**唯一**算法。

    参数
    ----
    displayName : pb_person.displayName（**已避让重名后的最终值**，不是用户原填的）
    familyName  : pb_person.familyName（可空；与 displayName 相同时不重复出 token）

    返回
    ----
    空格分隔的小写 token 串；入参全空时返回 ""（是否落 NULL 由调用方决定）。

    ⚠️ 必须传**写库时的最终 displayName**：`createContact` 会把「张三」避让成
       「张三(2)」，拼音要跟着最终值算，否则库里是「张三(2)」而拼音是
       「zhangsan」—— 用户搜 "zhangsan2" 零结果，看着就像坏了。
    """
    tokens = []

    def _add(value):
        if value and value not in tokens:
            tokens.append(value)

    full, initials = _one(displayName)
    _add(full)
    if len(initials) >= _MIN_INITIALS_LEN:
        _add(initials)
    if familyName and str(familyName) != str(displayName):
        familyFull, _unused = _one(familyName)
        _add(familyFull)

    text = " ".join(tokens)
    return text[:_MAX_LEN]


if __name__ == "__main__":
    print("pinyin.py _VERSION: %s（pypinyin 可用：%s）" % (_VERSION, _HAS_PINYIN))
    for _name, _family in (("王小明", ""), ("王小明", "王"), ("王 小明(2)", ""),
                           ("Lucy Chen", ""), ("Alice 王小明", ""),
                           ("未命名-ab12cd", ""), ("", "王"), ("张伟", "张")):
        print("%-16s| %-4s -> %r" % (_name, _family,
                                     personPinyin(_name, _family)))