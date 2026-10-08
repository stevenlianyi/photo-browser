#! /usr/bin/env python3
#encoding: utf-8

#Filename: dirNamePlace.py
#Description: 目录名 -> 地点名（`pb_photo.placeNameDir` 的唯一实现）—— 步骤 R4b · DR-30/DR-32/DR-34
#
# 要解决什么（DR-30 的实测数据）
# ----------------------------
#   `pb_photo.placeName` 来自 GPS 逆地理，而正式库 2137 张里只有 **65 张**有地点名
#   （GPS 覆盖 91 张，其中 26 张是 `(0,0)` 占位）。而 `relPath` 里带
#   「日期 + 地名」的目录覆盖约 **598 张（9 倍）**：
#       2013.07.26 华盛顿 114 ／ 2013.07.18 大都会博物馆 99 ／ 2013.07.16 纽约 98 …
#   `relPath` 是**已经存好的事实**（扫描时逐字落库，见 scanner/walker.py），
#   一个正则就能提取，且不依赖 reverse_geocoder / shapely 任何一个可选依赖。
#   ⇒ 不做这一步，地点字典的全部数据来源只有那 65 张 —— 地点层是个空壳。
#
# 为什么另存一列（`placeNameDir`）而不是直接写 `placeName`
# ------------------------------------------------------
#   `placeStore.rebuildPlaces()` 的聚合键是 `COALESCE(NULLIF(placeNameDir,''), placeName)`
#   （目录名优先，DR-32）。不把目录名直接写进 `placeName` 的理由：
#   · `placeName` 是**扫描器写的派生值**（逆地理结果），重扫会把它整列覆盖掉；
#     目录名线索跟扫描无关，混在一列里必然被某一次重扫冲掉，而且**不报错**；
#   · 目录名线索的**生命周期不同**：它由本模块填（新增靠扫描收尾自动、
#     改名靠人手动 --refresh），而 `placeName` 由扫描链路维护。
#     两列 = 两条独立的写路径，互不干扰。
#
# ⚠️⚠️ `placeNameDir` 是**非派生列**：只填空、绝不覆盖（本模块的立身之本）
# ----------------------------------------------------------
#   「只填空」的实现是 **SQL 层面的**：候选行只 SELECT `placeNameDir` 为空的行，
#   已有非空值的行**根本不进 payload** —— 覆盖在 SQL 层面就做不到，不靠调用方自觉。
#   这条纪律的价值：用户手工改过的值（比如某天发现「华盛顿」应是「华盛顿特区」）
#   不会在下一次批量重跑时被冲掉。它也是 `placeStore.REBUILD_COLUMNS` 里
#   **没有** `placeNameDir` 的原因（同 `nameZh`：派生复算绝不能碰非派生列）。
#
# ⚠️ 谁在什么时候填这一列（三档，别记混）—— 这是最容易被问的问题
# ------------------------------------------------------------------
#   · **新增目录 / 新照片**：扫描跑完（任务置 DONE）后由**收尾步骤自动**处理 ——
#     `processor/place/placeFinalize.finalizePlaces()`，挂在
#     `scanScheduler.runBatch` 的 DONE 分支（处内仍是单写入者锁内）。
#     开关：`basicSettings.PLACE_FINALIZE_AFTER_SCAN`（默认开）。
#   · **目录改名**：必须**手动**跑两条（自动收尾**只填空不覆盖**，
#     它不敢判断"这是改名留下的旧值"还是"用户手工改过的值"）：
#         python code\src\tools\place_cli.py --scan-dir --apply --refresh
#         python code\src\tools\place_cli.py --rebuild-only
#     ⚠️ 不跑会怎样：重扫只更新 `relPath`，`placeNameDir` 还停在旧名字上，
#        地点字典于是也不动 —— 而库/接口/界面**没有任何一处会报错**。
#        好消息：**改名不会静默** —— `scanDirNames()` 永远报「漂移」（旧值 ≠ 现在解出的值），
#        CLI/审计/扫描收尾的日志都会列出来。
#   · **判据认不出来**（新命名风格，如 `2016_05_01_三亚`）：`suspectsOf()` 会把它
#     列进「疑似地点」清单（只报告、不改采纳），人确认后加 `DIR_PLACE_ALIAS` 或黑名单。
#   ⚠️ 扫描器（runner）从头到尾**不写**这一列 —— 写入者是本模块与收尾步骤。

#
# ⚠️ 硬顺序（写进本模块、CLI 与 placeStore 的注释，三处一致）
# -----------------------------------------------------
#     ① scanDirNames()            填 pb_photo.placeNameDir
#     ② placeStore.rebuildPlaces() 按新聚合键重建地点字典
#     ③ placeNameZh.rebuildNameZh()（可选）补 GPS 地点的中文名
#   反了会怎样：先 ② 再 ① ⇒ 这一轮重建用的是**旧的**聚合键（上一轮的目录名/没有目录名），
#   新填的值这一轮完全不生效；而 `rebuildPlaces()` 会照常打印「更新 N 个地点」，
#   看起来一切正常 —— 这是本步最容易踩且最难发现的坑。
#
# 判据为什么必须做成配置（`basicSettings`）
# ----------------------------------------
#   判据 = 「**日期前缀 + 非空地名**」（DR-32 实测得出）。它干净地分开了 A 类
#   （`2013.07.26 华盛顿` 等 11 个目录 / 598 张）与 B 类（`BaiRuiQin` / `MOT Friends` /
#   `廉家老照片` 等 41 个目录 / 1539 张，全是人名/组名/活动名）。
#   ⚠️ 它是**用户的命名习惯**，不是普适规律：换一个照片库、或者用户哪天改成
#   `2013-07-26-华盛顿`（短横线）或 `华盛顿 2013.07.26`（日期在后），判据就得改。
#   所以正则与黑名单都在 `basicSettings` 里，改口径不用改代码。
#
# 6 个配置各管一段（**不要用一个正则去覆盖所有例外**）
# ------------------------------------------------
#   `DIR_PLACE_PATTERN`   默认口径（日期前缀 + 非空地名）
#   `DIR_PLACE_ALIAS`     **例外（要）**：某个目录用了另一种写法 ⇒ `目录名=地点名`；
#                         左侧以 `^` 开头就是**正则**（`^2026_05_01 喀纳斯.*=喀纳斯`
#                         一条覆盖一族日期目录，不用逐个登记）
#   `DIR_PLACE_BLACKLIST` **例外（不要）**：正则收得下但不是地点 ⇒ 整名拉黑。
#                         ⚠️ **会改变判定**（reason = `blacklisted`）
#   `DIR_PLACE_CONFIRMED_NOT_PLACE`
#                         **已人工确认"不是地点"的记录**：**不改判定**（那些名字本来
#                         就被排除），只让「疑似地点」报告闭嘴。留痕用，别与黑名单混：
#                         "为什么被排除"的机械原因（`noDatePrefix`）必须留着。
#   `DIR_PLACE_SUSPECT_MIN` / `DIR_PLACE_SUSPECT_MIN_YEAR`
#                         「疑似地点」门槛（含中文 >=5 / 含 4 位年份 >=1）
#   ⚠️ 别用"放宽正则"来解决个例：放宽是全局的，迟早把 `BaiRuiQin` 这类名字也放进来，
#      而那时**没有任何报错**。个例就该写在别名表/黑名单里。

import os
import re
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/place
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                            # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402
from database import queryCommon as query                        # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

_VERSION = "20261008"

_LOG = misc.setLogNew("dirNamePlace", "dirnameplace.log")

#: 一次事务写多少行（与扫描器 COMMIT_ROWS_PER_TXN / fix_placeholder_geo 同量级）
_COMMIT_STEP: int = 500

#: `pb_photo` 里 **NOT NULL 且没有 DEFAULT** 的列 —— upsert 走的是 INSERT 语义，
#: 缺任何一列都会撞 `NOT NULL constraint failed: pb_photo.<列>`。
#: ⚠️ 实测踩过（fix_placeholder_geo 那次是 relPath）：只带 photoCode 直接失败。
#: 它们只进 INSERT 列清单、**不在 updateColumns 里**，所以绝不会被改写。
_IDENTITY_COLUMNS: tuple = ("photoCode", "relPath", "relPathHash", "fileHash")


# ============================================================
# 一、reason 取值（**每个分支都必须给明确 reason**）
# ============================================================
# 为什么不允许「判不出来就返回空字符串」：那样「没填」这件事会塌成一种状态，
# 而它其实有 4 种完全不同的原因（无日期前缀 / 只有日期没有地名 / 被黑名单拦下 /
# 压根没有父目录）。验收第 4 条要求「完整采纳/排除清单 + 理由」，
# 没有 reason 就只能打印一个「排除」，人核对时无从下手。
REASON_ADOPTED: str = "dateAndName"      # 采纳：日期前缀 + 非空地名
REASON_ALIAS: str = "aliased"            # 采纳：命中 basicSettings.DIR_PLACE_ALIAS（人工登记）
REASON_DATE_ONLY: str = "dateOnlyNoName"  # 排除：只有日期（或年月），地名部分为空
REASON_NO_DATE: str = "noDatePrefix"     # 排除：没有日期前缀（人名/组名/活动名全在这里）
REASON_BLACKLIST: str = "blacklisted"    # 排除：命中 basicSettings.DIR_PLACE_BLACKLIST
REASON_NO_PARENT: str = "noParentDir"    # 排除：relPath 里没有可看的父目录
REASON_BAD_PATTERN: str = "patternInvalid"  # 排除：basicSettings.DIR_PLACE_PATTERN 不合法

#: reason -> 中文说明（给 CLI / 日志看，**不参与逻辑**）
REASON_TEXT: dict = {
    REASON_ADOPTED: "采纳：日期前缀 + 非空地名",
    REASON_ALIAS: "采纳：命中别名表 DIR_PLACE_ALIAS（人工登记）",
    REASON_DATE_ONLY: "排除：只有日期，没有地名",
    REASON_NO_DATE: "排除：无日期前缀（人名/组名/活动名）",
    REASON_BLACKLIST: "排除：命中黑名单 DIR_PLACE_BLACKLIST（正则收得下但不是地点）",
    REASON_NO_PARENT: "排除：relPath 里没有父目录",
    REASON_BAD_PATTERN: "排除：DIR_PLACE_PATTERN 不是合法正则",
}

#: 「只有日期」的补判正则（用来把 reason 从「无日期前缀」里分出来）：
#: `20051229`（22 张）/ `201105`（8 张）都落在这里。
#: ⚠️ 它们**必须排除**：没有地名信息的地点名（比如把 `20051229` 当成地点叫
#:   「20051229」）会在地点列表里长出一堆日期，比不给地点更坏。
_DATE_ONLY_RE = re.compile(r"^\d{4}[.\-/]?\d{2}([.\-/]?\d{2})?\s*$")


# ============================================================
# 二、判据（配置化；进程内按「配置字符串」缓存编译结果）
# ============================================================
#: (配置正则原文, 编译结果)。⚠️ 缓存键必须是**配置字符串本身**：
#:   测试会 monkeypatch `basicSettings.DIR_PLACE_PATTERN` 再调用本模块，
#:   只缓存一个对象的话，改配置后仍然用旧正则 —— 表现为「改了配置没生效」，
#:   且不报错（与 placeStore._NAMEZH_CACHE 记库文件路径同一个理由）。
_PATTERN_CACHE: tuple = ("", None)


def _patternOf():
    """编译 `basicSettings.DIR_PLACE_PATTERN`；不合法时返回 None（由调用方给 reason）。"""
    global _PATTERN_CACHE
    text = str(basicSettings.DIR_PLACE_PATTERN or "")
    if _PATTERN_CACHE[0] == text and _PATTERN_CACHE[1] is not None:
        return _PATTERN_CACHE[1]
    try:
        compiled = re.compile(text)
    except re.error as e:                                        # noqa: BLE001
        _LOG.error("dirNamePlace: DIR_PLACE_PATTERN 不是合法正则（%s），"
                   "本模块一律判为「无线索」: %r" % (type(e).__name__, text))
        _PATTERN_CACHE = (text, None)
        return None
    _PATTERN_CACHE = (text, compiled)
    return compiled


def _blacklistOf() -> frozenset:
    """`DIR_PLACE_BLACKLIST` -> 小写集合（大小写不敏感的整名匹配）。"""
    out = set()
    for one in (basicSettings.DIR_PLACE_BLACKLIST or ()):
        text = str(one or "").strip().lower()
        if text:
            out.add(text)
    return frozenset(out)


#: (配置原文, {"exact": {...}, "regex": [(compiled, 地点名)]})。缓存键是**配置字符串本身**，
#: 理由与 `_PATTERN_CACHE` 完全相同：测试会 monkeypatch 配置再调用本模块，
#: 只存结果的话会出现「改了配置没生效」且不报错。
_ALIAS_CACHE: tuple = ("", None)
#: 已经为哪些目录名记过「配置写错」的 warning（**一个名字只喊一次**）：
#: `parseDirName` 在扫描时是**逐行**调用的，不缓存的话一个配错的名字能刷几千条日志。
_CONFIRMED_WARNED: set = set()


def _aliasOf() -> dict:
    """`DIR_PLACE_ALIAS` -> `{"exact": {小写目录名: 地点名}, "regex": [(编译, 地点名)]}`。

    别名表是「**判据认不出的例外**」的人工落点，与黑名单对称：
      · 黑名单：`2016.05.01 全家福`  → 正则收得下，但人知道它不是地点
      · 别名表：`2026_05_01_三亚`    → 正则收不下（下划线），但人知道它是地点

    两种写法（**左侧以 `^` 开头就是正则**，否则整名精确、大小写不敏感）：
      · `"2026_05_01_三亚=三亚"`        —— 整名精确匹配这一个目录
      · `"^2026_05_01 喀纳斯.*=喀纳斯"` —— 正则（可做**前缀/家族**匹配：
        同一个地方分了好几天的目录，一条就能覆盖，不用逐个登记）
    ⚠️ 正则一律 `re.match`（从名字开头匹配），所以想"包含"要自己写 `.*`；
       写法与 `DIR_PLACE_PATTERN` 一致，不用记两套规则。

    ⚠️ 为什么不靠"把正则放宽"来解决：放宽是**全局**的（会影响所有目录名），
       而"某个目录恰好用了另一种写法"是**个例**。用全局规则去覆盖个例，
       迟早会把 `BaiRuiQin` 这类名字也放进来 —— 而那时没有任何报错。
    """
    global _ALIAS_CACHE
    text = str(basicSettings.DIR_PLACE_ALIAS or ())
    if _ALIAS_CACHE[0] == text and _ALIAS_CACHE[1] is not None:
        return _ALIAS_CACHE[1]
    exact, regex = {}, []
    for one in (basicSettings.DIR_PLACE_ALIAS or ()):
        item = str(one or "")
        if "=" not in item:
            _LOG.warning("dirNamePlace: DIR_PLACE_ALIAS 项缺少 '='（已忽略）: %r", item)
            continue
        key, _sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not value:
            _LOG.warning("dirNamePlace: DIR_PLACE_ALIAS 项左侧目录名或右侧地点名为空"
                         "（已忽略）: %r", item)
            continue
        if key.startswith("^"):
            try:
                regex.append((re.compile(key), value))
            except re.error as e:                                 # noqa: BLE001
                _LOG.warning("dirNamePlace: DIR_PLACE_ALIAS 项左侧正则不合法（已忽略）"
                             " %r: %s: %s", item, type(e).__name__, e)
            continue
        exact[key.lower()] = value
    out = {"exact": exact, "regex": regex}
    _ALIAS_CACHE = (text, out)
    return out


def _matchAlias(text: str) -> str:
    """目录名 -> 别名表里登记的地点名（没命中返回 ""）。**精确优先、再按登记顺序试正则**。"""
    aliases = _aliasOf()
    hit = aliases["exact"].get(str(text or "").lower())
    if hit:
        return hit
    for pattern, value in aliases["regex"]:
        if pattern.match(str(text or "")):
            return value
    return ""


def _confirmedOf() -> frozenset:
    """`DIR_PLACE_CONFIRMED_NOT_PLACE` -> 小写集合（**只用于疑似报告闭嘴**）。

    ⚠️ 它**绝不参与判定**（与 `DIR_PLACE_BLACKLIST` 的区别就在这里）：
       那些目录本来就该被排除，而我们不想因此把机械原因（`noDatePrefix`）
       擦掉 —— 排障时"为什么被排除"只有那一个线索。
    """
    out = set()
    for one in (basicSettings.DIR_PLACE_CONFIRMED_NOT_PLACE or ()):
        text = str(one or "").strip().lower()
        if text:
            out.add(text)
    return frozenset(out)


def _trimRangeTail(name: str) -> str:
    """清掉名字开头的**日期区间残留**（`2013.07.24～25 康宁及赫尔希`）。

    ⚠️ 这一段是判据正则的必然产物：正则只吃掉了 `2013.07.24`，
       剩下 `～25 康宁及赫尔希`。不清的话界面上会出现一个叫
       「～25 康宁及赫尔希」的地点 —— 一个字都没错，但没人看得懂。
    ⚠️ 只清**紧跟日期的那一小段**（`～25` / `-25` / `至25` 这种），
       清完为空就退回原值（宁可名字难看，也不能把名字清没了）。
    """
    trimmed = re.sub(str(basicSettings.DIR_PLACE_RANGE_TRIM or ""), "",
                     str(name or ""), count=1).strip()
    return trimmed or str(name or "").strip()


def _trimNameHead(name: str) -> str:
    """清掉名字**开头残留的分隔符**（`DIR_PLACE_NAME_TRIM`）。

    ⚠️ 实测来源：`2016.05.01-三亚` —— 目录名用 `-` 直接连地名时，判据正则
       的日期前缀只吃掉 `2016.05.01`，剩下 `-三亚`，于是地点列表里会出现
       一个名字带前导短横线的地点。它不报错、不崩溃，只是**看着像脏数据**，
       而这类"看着像脏数据"的名字最容易让人以为整个地点功能不可信。
    ⚠️ 与 `_trimRangeTail` 同一条底线：**清完为空就退回原值**。
    """
    trimmed = re.sub(str(basicSettings.DIR_PLACE_NAME_TRIM or ""), "",
                     str(name or ""), count=1).strip()
    return trimmed or str(name or "").strip()


# ============================================================
# 三、判定
# ============================================================
def parseDirName(dirName: str) -> dict:
    """目录名 -> `{isPlace, placeName, dateHint, reason, reasonText, raw}`。

    ⚠️ 判据必须**可解释**：返回值里的 `reason` 说明为什么采纳/排除，
       验收第 4 条要求把 52 个目录逐个列出来给人核对 —— 没有 reason
       就只能打印「采纳 11 / 排除 41」，那等于没有核对。

    返回
    ----
    isPlace    : bool  —— 是否当地点线索采纳
    placeName  : str   —— 采纳时是地点名（日期前缀已剥离、区间残留已清理）；否则 ""
    dateHint   : str   —— `YYYY-MM-DD`（判据里认到的日期；没认到就是 ""）
                         ⚠️ **仅供报告与排障**，不落库、不参与聚合
    reason     : str   —— REASON_* 之一
    reasonText : str   —— 中文说明（打印用）
    raw        : str   —— 原目录名（逐字，给报告做「原值 -> 判定」的对照）
    """
    raw = str(dirName or "")
    text = raw.strip()
    if not text:
        return {"isPlace": False, "placeName": "", "dateHint": "",
                "reason": REASON_NO_PARENT, "reasonText": REASON_TEXT[REASON_NO_PARENT],
                "raw": raw}
    if text.lower() in _blacklistOf():
        return {"isPlace": False, "placeName": "", "dateHint": "",
                "reason": REASON_BLACKLIST, "reasonText": REASON_TEXT[REASON_BLACKLIST],
                "raw": raw}

    # ---- 别名表：人工登记的例外，**优先于正则**（先黑后白，再默认规则）----
    # 顺序理由：黑名单是"明确不要"，别名表是"明确要"，两者都不该被正则的宽松/严格
    # 覆盖掉 —— 正则只是**默认**，人的判断永远赢。
    alias = _matchAlias(text)
    if alias:
        return {"isPlace": True, "placeName": alias, "dateHint": "",
                "reason": REASON_ALIAS, "reasonText": REASON_TEXT[REASON_ALIAS],
                "raw": raw}

    pattern = _patternOf()
    if pattern is None:
        return {"isPlace": False, "placeName": "", "dateHint": "",
                "reason": REASON_BAD_PATTERN, "reasonText": REASON_TEXT[REASON_BAD_PATTERN],
                "raw": raw}

    hit = pattern.match(text)
    if not hit:
        # 分开「只有日期」与「根本没有日期前缀」：前者的处置口径完全不同
        # （`20051229` 是**差点就能采纳**的，将来真加了地名就该收下；
        #  而 `BaiRuiQin` 永远不该被这份判据收下）。
        dateOnly = bool(_DATE_ONLY_RE.match(text))
        reason = REASON_DATE_ONLY if dateOnly else REASON_NO_DATE
        return {"isPlace": False, "placeName": "", "dateHint": "",
                "reason": reason, "reasonText": REASON_TEXT[reason], "raw": raw}

    groups = hit.groupdict()
    # 两道清理都要做：先清日期区间残留（`～25 康宁及赫尔希`），
    # 再清开头分隔符（`-三亚`）。反序也行，但**必须都清** —— 少一道就会
    # 让「～25 …」或「-三亚」这种名字进地点列表。
    name = _trimNameHead(_trimRangeTail(groups.get("name") or ""))
    dateHint = "%s-%s-%s" % (groups.get("y") or "", groups.get("m") or "",
                             groups.get("d") or "")
    if not name:
        # 正则要求 name 非空，走到这里只可能是「名字刚好等于区间残留」这种边角：
        # 按「只有日期」处理（没有可用的地名）
        return {"isPlace": False, "placeName": "", "dateHint": dateHint,
                "reason": REASON_DATE_ONLY, "reasonText": REASON_TEXT[REASON_DATE_ONLY],
                "raw": raw}
    # ⚠️ 自检（一个名字只喊一次）：它被登记在「已确认**不是**地点」里，
    #    却被正则**采纳**了 —— 两份配置互相矛盾，多半是配置写错，
    #    而矛盾的结果只会让"疑似地点"报告闭嘴（不影响判定，所以不报错）。
    if text.lower() in _confirmedOf() and text not in _CONFIRMED_WARNED:
        _CONFIRMED_WARNED.add(text)
        _LOG.warning("dirNamePlace: 目录名 %r 同时出现在 DIR_PLACE_CONFIRMED_NOT_PLACE "
                     "与判据采纳结果里 —— 配置矛盾（判定按判据，疑似报告会让它闭嘴）", text)
    return {"isPlace": True, "placeName": name, "dateHint": dateHint,
            "reason": REASON_ADOPTED, "reasonText": REASON_TEXT[REASON_ADOPTED],
            "raw": raw}


def _dirNameOf(relPath: str, up: int = 1) -> str:
    """`relPath` 的第 up 层祖先目录名（up=1 就是文件的父目录）。取不到返回 ""。"""
    text = str(relPath or "").replace("\\", "/")
    parts = [one for one in text.split("/") if one not in ("", ".")]
    dirs = parts[:-1]                     # 最后一段是文件名本身
    if up < 1 or len(dirs) < up:
        return ""
    return dirs[-up]


def resolveDirPlace(relPath: str, startUp: int = 1, maxUp: int = None) -> dict:
    """`relPath` -> `{level, placeName, dirName, ...parseDirName 的字段}`。

    `level` 表示在第几层祖先上找到线索：1 = 父目录，2 = 祖父目录，0 = 无线索。
    ⚠️ 把「从哪一层找到的」报出来是**必需的**：`placeNameDirOf` 只回一个字符串，
       而报告要能回答「这一行到底是从哪个目录取的名字」—— 目录改名排障全靠它。
    """
    start = max(1, int(startUp or 1))
    stop = (int(basicSettings.DIR_PLACE_MAX_UP or 1) if maxUp is None
            else max(start, int(maxUp)))
    for up in range(start, stop + 1):
        dirName = _dirNameOf(relPath, up)
        if not dirName:
            break                          # 再往上就是 photo 根 —— 明确终止
        got = parseDirName(dirName)
        if got["isPlace"]:
            got = dict(got)
            got["level"] = up
            got["dirName"] = dirName
            return got
    parent = _dirNameOf(relPath, 1)
    out = dict(parseDirName(parent))
    # ⚠️ `level=0` = **在允许的层数内没找到线索** ⇒ 名字必须是空串。
    #    这里保留 parseDirName 的字段（reason / reasonText / raw）只为报告好看，
    #    但 `placeName` 一定要清掉：不清的话 `placeNameDirOf(depth=2)` 会把
    #    「父目录的名字」当成结果返回 —— 而父目录**压根不在本次搜索范围内**。
    #    这是个只看返回值看不出错的 bug：`depth` 参数静默失效。
    out["placeName"] = ""
    out["dirName"] = parent
    out["level"] = 0
    return out


def placeNameDirOf(relPath: str, depth: int = 1) -> str:
    """`relPath` -> 目录名地点名（**空串 = 无线索**）。

    `depth`
    ------
      从哪一层祖先开始看：1 = 父目录（默认，实测覆盖全部 598 张）、2 = 祖父目录。
      向上最多试到 `basicSettings.DIR_PLACE_MAX_UP` 层。

    ⚠️ **上溯有明确终止条件**，不许"一路爬到 photo 根"
    ------------------------------------------------
      理由是实测验出来的：照片库的一级目录是 `2013美国游` / `Friends` /
      `Lian Family` / `Family` —— 它们**不是地点**（`2013美国游` 甚至长得像日期）。
      一旦允许无限上溯，`2013美国游/照片/风景照/2013.07.26 华盛顿` 这类路径就会
      在某个环节把「上一级的分类目录」当成地点，把「按地点归类」退化成
      「按磁盘布局归类」，同一批照片被拆成好几行。所以：
        · 最多试 `DIR_PLACE_MAX_UP` 层（默认 2）；
        · `relPath` 里没有那么多层就停（`break`）。
    """
    start = max(1, int(depth or 1))
    stop = max(start, int(basicSettings.DIR_PLACE_MAX_UP or 1))
    return resolveDirPlace(relPath, startUp=start, maxUp=stop)["placeName"]


# ============================================================
# 四、扫全库填 `pb_photo.placeNameDir`（**只填空不覆盖**）
# ============================================================
def _loadPhotoRows() -> list:
    """取全部照片的判据输入列。

    ⚠️ 用 `query.selectList`（**数据访问层的只读出口**）而不是生成层的
       `query_pb_photo`：后者会 `SELECT *`（31 列 × 全部行），而本函数只要 6 列。
       10 万行规模下这个差别是「几百 MB 的 dict 列表」与「几十 MB」的差别，
       与 DR-12 分页读索引的理由同源。
    ⚠️ **不按 delFlag 过滤**：软删行的 `placeNameDir` 同样是「该填的线索」
       （用户恢复照片后不必再等一次全量重跑），而且它一个字节都不会影响
       派生字典（`rebuildPlaces` 只看 delFlag='0'）。
    """
    return query.selectList(
        "SELECT p.photoCode AS photoCode, p.relPath AS relPath,"
        " p.relPathHash AS relPathHash, p.fileHash AS fileHash,"
        " p.placeName AS placeName, p.placeNameDir AS placeNameDir"
        " FROM pb_photo p ORDER BY p.relPath ASC")


#: 「4 位年份数字」与「含中文」—— 疑似地点的两个信号（**只用于报告**）
_YEAR_IN_NAME_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def suspectsOf(dirList: list, minPhotos: int = None) -> list:
    """从采纳/排除清单里挑出**疑似地点**（需人工确认）的目录。

    为什么需要它（这是「判据是配置」的**运维面**）
    --------------------------------------------
      判据 = **用户的命名习惯**，而习惯会变。新增一个 `2016_05_01_三亚`
      （下划线分隔）或 `三亚 2016.05.01`（日期在后）时，正则判它"无日期前缀"
      ⇒ 排除 ⇒ **静默**：地点列表里没有三亚，而没有任何一处会报错
      （52 个目录的清单人也不会天天看）。
      ⚠️ 这里只**报告、绝不改变采纳结果**：改采纳必须由人显式做（改正则 /
        加 `DIR_PLACE_ALIAS`），因为"看起来像地名"离"就是地名"还差一次判断 ——
        `廉家老照片` / `毕业照` 就长得很像。

    规则（全部满足才报）
    ------------------
      · 被排除；
      · **不在黑名单里**、**也不在「已确认不是地点」清单里**
        （`DIR_PLACE_CONFIRMED_NOT_PLACE` = 人看过的结论，不重复报同一批）；
      · **不是 `dateOnlyNoName`** —— 「只有日期、没有地名」是一种**已理解**的形态
        （`20051229` / `201105`），报它只会淹没真正的新风格；
      · 张数过门槛（**分级**，见下）；
      · 满足任一：含 4 位年份数字（**新日期写法**嫌疑）或含中文（像地名）。

    门槛为什么分级（这是"报出来"与"吵死人"的平衡）
    --------------------------------------------
      · **含 4 位年份数字** ⇒ `DIR_PLACE_SUSPECT_MIN_YEAR`（默认 **1**）：
        人名/组名几乎不会带年份，而"日期 + 地名"是本库最主要的习惯 ——
        只出现一个这样的目录却没被采纳，就该看一眼（`2016_05_01 三亚`）。
      · **只含中文** ⇒ `DIR_PLACE_SUSPECT_MIN`（默认 **5**）：
        `廉家老照片` / `毕业照` / `豆豆家` 这类"像地名"的目录太多，
        门槛低了报告就被噪声淹没 —— 那时它等于**没报**。
    """
    out = []
    confirmed = _confirmedOf()
    floorBase = int(basicSettings.DIR_PLACE_SUSPECT_MIN)
    floorYear = int(basicSettings.DIR_PLACE_SUSPECT_MIN_YEAR)
    for one in dirList:
        if one.get("isPlace"):
            continue
        reason = str(one.get("reason") or "")
        if reason in (REASON_BLACKLIST, REASON_DATE_ONLY):
            continue
        name = str(one.get("dir") or "")
        if name.strip().lower() in confirmed:
            continue
        hasYear = bool(_YEAR_IN_NAME_RE.search(name))
        if not hasYear and not _CJK_RE.search(name):
            continue
        floor = floorYear if hasYear else floorBase
        if minPhotos is not None:
            floor = int(minPhotos)
        if int(one.get("count") or 0) < floor:
            continue
        if hasYear:
            hint = "含 4 位年份数字但判据没认出来 —— **新日期写法**？" \
                   "是地点就加 DIR_PLACE_ALIAS（左侧可写 `^正则`）"
        else:
            hint = "含中文、像地名但没有日期前缀 —— 也可能是人名/活动名。" \
                   "是地点 ⇒ DIR_PLACE_ALIAS；不是 ⇒ DIR_PLACE_CONFIRMED_NOT_PLACE" \
                   "（记录一下，往后不再报它；若它其实会被判据采纳，用 DIR_PLACE_BLACKLIST）"
        item = dict(one)
        item["hint"] = hint
        out.append(item)
    return out


def scanDirNames(dryRun: bool = True, dbFile: str = None,
                 refresh: bool = False) -> dict:
    """遍历 `pb_photo`，从 `relPath` 解出每张照片的 `placeNameDir` 并**填空**。

    `dryRun=True`（默认）**只算不写**，报告「将填多少行 + 完整采纳/排除清单」。
    写库必须显式 `dryRun=False`（与 tools/fix_placeholder_geo.py 同一纪律）。

    ⚠️ **只填空不覆盖**：`placeNameDir` 已有非空值的行**不进 payload**
    -------------------------------------------------------
      这是本列敢做非派生列的前提（见文件头）：用户手工改过的值不会在
      下一次批量重跑时被冲掉。

    ⚠️ 但「只填空」有一个**必须一起解决**的副作用：目录改名之后旧值永远留着
    --------------------------------------------------------------
      真实场景（DR-32 实测）：用户两天内把 `2013.07.26 张家界` 改成了
      `2013.07.26 华盛顿`。重扫会更新 `relPath`，而 `placeNameDir` 里还是
      **张家界** —— 只填空的话那个旧值**永远不会被改掉**，
      于是地点字典永远停在一个已经不存在的目录名上，而**没有任何报错**。
      ⇒ 本函数做两件事：
        ① **永远报漂移**（`drift`：库里存量 ≠ 现在从 relPath 解出来的值）。
           改名因此**不可能静默**，哪怕你什么都不加。
        ② `refresh=True` 时**覆盖**这些漂移行（这是显式动作，命令行上是
           `--refresh`）—— 因为它与「用户手工改过的值」在数据上**长得一样**，
           谁都不能替用户判断，只能由人显式下令（默认 False = 绝不覆盖）。

    返回
    ----
    dict —— {dbFile, dryRun, refresh, total, candidates, written, alreadyFilled,
             refreshed, drift, noClue, suspects, viaAncestor, fieldStats,
             dirStats, samples, nextSteps}
      total         : pb_photo 总行数（含软删）
      candidates    : 本次**将填**的行数（placeNameDir 为空 + 现在能解出名字；
                      `refresh=True` 时还含漂移行）
      written       : 实际写入行数（dryRun 时为 0）
      alreadyFilled : 已有非空值、**原样不动**的行数（只填空的证据）
      refreshed     : 其中被判为**漂移**、本次覆盖掉的行数（refresh=False 时为 0）
      drift         : 漂移清单（list，**永远返回**；每项含 photoCode/relPath/
                      old/now）—— 目录改名后靠它发现「字典停在旧名字上」
      noClue        : 解不出名字的行数（人名/组名目录、截图、根目录下的图）
      suspects      : **疑似地点**（被排除但像地名，只报告不改采纳）——
                      防「新命名风格静默排除」，见 `suspectsOf()`
      viaAncestor   : 从祖父目录（level>=2）取到名字的行数。
                      ⚠️ 单独报出来：它意味着「父目录没有地点线索」，
                      这类行值得人看一眼（实测正式库为 0）
      fieldStats    : 字段级总数（照片总数 / placeNameDir 非空 / placeName 非空 / 两者都空）
      dirStats      : **按父目录名**分组的完整采纳/排除清单
                      （list，按张数降序；每项含 dir/count/isPlace/placeName/reason…）
      samples       : 采纳样例（前若干条，relPath -> placeName）
      nextSteps     : 下一步该跑什么（硬顺序，见文件头）
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)                  # DR-10：必须显式才切库
    if not query.tableExists("pb_photo"):
        raise RuntimeError("pb_photo 表不存在 —— 先跑一次 tools\\build_db.py")
    if "placeNameDir" not in query.columnsOf("pb_photo"):
        raise RuntimeError(
            "pb_photo.placeNameDir 列不存在 —— 改过 pb_photo.txt 之后要重跑生成器并跑 "
            "`tools\\build_db.py --migrate`（它只加列、不动数据）")

    rows = _loadPhotoRows()
    payload, dirStats, samples, drift = [], {}, [], []
    viaAncestor = alreadyFilled = noClue = refreshed = 0
    fieldStats = {"total": len(rows), "placeNameDir": 0, "placeName": 0, "bothEmpty": 0}

    for one in rows:
        relPath = str(one.get("relPath") or "")
        oldDir = str(one.get("placeNameDir") or "")
        oldPlace = str(one.get("placeName") or "")
        if oldDir:
            fieldStats["placeNameDir"] += 1
        if oldPlace:
            fieldStats["placeName"] += 1
        if not oldDir and not oldPlace:
            fieldStats["bothEmpty"] += 1

        clue = parseDirName(_dirNameOf(relPath, 1))
        # 父目录的判定始终入清单（验收第 4 条要的就是「52 个目录逐个给判定」）
        stat = dirStats.setdefault(clue["raw"] or "(无父目录)", {
            "dir": clue["raw"] or "(无父目录)", "count": 0, "isPlace": clue["isPlace"],
            "placeName": clue["placeName"], "reason": clue["reason"],
            "reasonText": clue["reasonText"], "viaAncestor": 0})
        stat["count"] += 1

        got = resolveDirPlace(relPath)
        name = str(got.get("placeName") or "")

        if oldDir:
            alreadyFilled += 1
            # ---- 漂移检查（**永远做**，与 refresh 无关）----
            # 「库里已有值」与「现在从 relPath 解出来的值」不一致 ⇒ 目录改过名。
            # ⚠️ 只在**现在解得出名字**时才算漂移：解不出名字多半是 relPath 已经被
            #   改成新路径、而新目录名字不是地点（例如改成了 `全家福`）——
            #   那不是「漂移」，是「线索消失」，不该被当成改名提示。
            if name and name != oldDir:
                drift.append({"photoCode": str(one.get("photoCode") or ""),
                              "relPath": relPath, "old": oldDir, "now": name})
            if not (refresh and name and name != oldDir):
                continue                               # **只填空不覆盖**
            refreshed += 1
        if not name:
            noClue += 1
            continue
        if int(got.get("level") or 1) >= 2:
            viaAncestor += 1
            stat["viaAncestor"] += 1
        row = {key: one.get(key) for key in _IDENTITY_COLUMNS}
        row["placeNameDir"] = name
        row["modifyYMDHMS"] = misc.getTime()
        payload.append(row)
        if len(samples) < 15:
            samples.append({"relPath": relPath, "placeNameDir": name,
                            "dirName": got.get("dirName"), "level": got.get("level")})

    dirList = sorted(dirStats.values(), key=lambda it: (-it["count"], it["dir"]))
    suspects = suspectsOf(dirList)          # 「疑似地点」——只报告，不改采纳

    written = 0
    if payload and not dryRun:
        for begin in range(0, len(payload), _COMMIT_STEP):
            chunk = payload[begin:begin + _COMMIT_STEP]
            rtn, _columns = sqliteCommon.insertManyTableGeneral(
                "pb_photo", chunk, conflictColumns=("photoCode",),
                # ⚠️ 身份列只用于让 INSERT 语义不违规，**不在 updateColumns 里**
                #    —— 它们一个字节都不会被改写（relPath 是磁盘原值的镜像）。
                updateColumns=("placeNameDir", "modifyYMDHMS"),
                fillStandard=True)
            if int(rtn or 0) < 0:
                raise RuntimeError("scanDirNames 写 pb_photo 失败: %s"
                                   % sqliteCommon.dbHandle().lastErrMsg)
            written += len(chunk)

    out = {"dbFile": sqliteCommon.dbFilePath(), "dryRun": bool(dryRun),
           "refresh": bool(refresh),
           "total": len(rows), "candidates": len(payload), "written": written,
           "alreadyFilled": alreadyFilled, "refreshed": refreshed, "drift": drift,
           "noClue": noClue, "suspects": suspects,
           "viaAncestor": viaAncestor, "fieldStats": fieldStats,
           "dirStats": dirList, "samples": samples}
    out["nextSteps"] = _nextSteps(bool(dryRun), written, drift, bool(refresh),
                                  suspects)
    _LOG.info("scanDirNames: total=%d candidates=%d written=%d alreadyFilled=%d "
              "refreshed=%d drift=%d noClue=%d viaAncestor=%d dryRun=%s refresh=%s",
              out["total"], out["candidates"], out["written"],
              out["alreadyFilled"], out["refreshed"], len(drift),
              out["noClue"], out["viaAncestor"], out["dryRun"], out["refresh"])
    if drift:
        _LOG.warning("scanDirNames: 发现 %d 行 placeNameDir 漂移（目录改名的证据）：%s",
                     len(drift), drift[:5])
    if suspects:
        # ⚠️ 只记 warning，**不改采纳**：它是"让人看一眼"的入口，不是自动决定
        _LOG.warning("scanDirNames: %d 个目录**疑似地点但被排除**（新命名风格？）：%s",
                     len(suspects), [(one["dir"], one["count"]) for one in suspects])
    return out


def _nextSteps(dryRun: bool, written: int, drift: list = None,
               refresh: bool = False, suspects: list = None) -> list:
    """硬顺序提示（写进返回值与 CLI 输出 —— 顺序反了会**静默**用旧键重建）。"""
    steps = []
    if suspects:
        steps.append("⚠️ %d 个目录**疑似地点但被排除**（判据没认出来的新写法？）：%s"
                     % (len(suspects),
                        ", ".join("%s(%d 张)" % (one["dir"], one["count"])
                                  for one in suspects[:5])))
        steps.append("   确认是地点 ⇒ 加 basicSettings.DIR_PLACE_ALIAS"
                     "（`目录名=地点名`，左侧可写 `^正则`）；"
                     "确认不是 ⇒ 加 DIR_PLACE_CONFIRMED_NOT_PLACE（记录一下，往后不再报）")
    if drift:
        steps.append("⚠️ 发现 %d 行 placeNameDir **漂移**（库里存量 ≠ 现在解出来的值）"
                     "—— 目录改过名，而字典还停在旧名字上：%s"
                     % (len(drift), ", ".join("%s -> %s" % (one["old"], one["now"])
                                              for one in drift[:3])))
        steps.append("   处理办法（**必须显式**，因为「目录改名」与「用户手工改过值」"
                     "在数据上长得一样）：加 --refresh 覆盖漂移行")
        if refresh:
            steps.append("   本次已按 --refresh 覆盖其中 %d 行" % written)
    if dryRun:
        steps.append("本步是 dry-run，未写库。确认清单无误后加 --apply（或 CLI 的 --scan-dir）实跑。")
        return steps
    if not written:
        steps.append("本次没有新填任何行（placeNameDir 本来就是满的）。")
        steps.append("若刚改过目录名，请先 --scan-dir --apply --refresh，再跑："
                     "placeStore.rebuildPlaces()（CLI: --rebuild-only）")
        return steps
    steps.extend([
        "① 已填 pb_photo.placeNameDir（%d 行）" % written,
        "② 下一步**必须**重建地点字典：placeStore.rebuildPlaces()（CLI: --rebuild）",
        "   顺序不能反：先 rebuild 再扫目录名 ⇒ 这一轮用的还是旧聚合键，"
        "而 rebuildPlaces 会照常打印「更新 N 个地点」，看不出来。",
        "③（可选）placeNameZh.rebuildNameZh() 补 GPS 地点的中文名（CLI: --rebuild 已含）",
        "⚠️ 什么时候需要人动手（**新增不用，改名才用**）：",
        "   新增目录 / 新照片：扫描完成后的收尾会自动做（placeFinalize.finalizePlaces，"
        "由 scanScheduler 的 DONE 分支触发）—— 无需操作；",
        "   目录改名：必须手动 `--scan-dir --apply --refresh` + `--rebuild-only`"
        "（自动收尾**只填空不覆盖**，它不会冲掉你手工改过的值）。",
        "   本列由 scanDirNames 填，**扫描器（runner）不负责它**，重扫只更新 relPath。",
    ])
    return steps


def pendingScanCount(dbFile: str = None) -> dict:
    """**轻量**统计「该填但还没填」的行数（只取未填行的 `relPath`）。

    用途：CLI 的**顺序闸**（验收第 21 条）。`--rebuild` 若在 `placeNameDir`
    还是空的时候先跑，这一轮重建用的就是**旧聚合键** —— 而 `rebuildPlaces()`
    会照常打印「更新 N 个地点」，**看起来完全正常**。所以顺序必须由工具来挡，
    不能靠人记得住。

    返回 `{unfilled, candidates}`：未填行数 / 其中**现在能解出名字**的行数。
    ⚠️ 与 `scanDirNames()` 的 `candidates` 口径一致（同一套判据、同一次逐行解析），
       只是不装载另外 5 列、不做统计分组。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    rows = query.selectList(
        "SELECT p.relPath AS relPath FROM pb_photo p"
        " WHERE p.placeNameDir IS NULL OR p.placeNameDir = ''"
        " ORDER BY p.relPath ASC")
    candidates = 0
    for one in rows:
        if placeNameDirOf(str(one.get("relPath") or "")):
            candidates += 1
    return {"unfilled": len(rows), "candidates": candidates}


def rebuildDirPlaces(dryRun: bool = False, dbFile: str = None,
                     refresh: bool = False) -> dict:
    """填列 + 报告（**落库后提示调用 `placeStore.rebuildPlaces()`**）。

    ⚠️ 硬顺序（本函数的全部意义就在这一句）
    -------------------------------------
      ① `dirNamePlace.scanDirNames()`   —— 填 `pb_photo.placeNameDir`
      ② `placeStore.rebuildPlaces()`    —— 按新聚合键（目录名优先）重建地点字典
      ③ `placeNameZh.rebuildNameZh()`   ——（可选）补 GPS 地点的中文名

      反了会怎样：先 ② 再 ① ⇒ 这一轮重建读到的 `placeNameDir` 还是空的
      （或上一轮的值），**新填的目录名这一轮完全不生效**，而
      `rebuildPlaces()` 会打印「更新 N 个地点」，看起来一切正常。

    ⚠️ 本函数**刻意不自己调 `rebuildPlaces()`**：那样「填列」与「重建字典」
       就分不开了 —— 而这两步对用户是两件事（前者随目录改名而重跑、
       后者还要在看清单确认之后才决定）。CLI 的 `--rebuild` 负责按顺序把
       三步串起来。
    """
    report = scanDirNames(dryRun=dryRun, dbFile=dbFile, refresh=refresh)
    report["hint"] = ("下一步：placeStore.rebuildPlaces()（CLI: --rebuild）—— "
                      "务必在 scan-dir 之后跑，反了会静默沿用旧聚合键")
    _LOG.info("rebuildDirPlaces: dryRun=%s %s", dryRun, report["hint"])
    return report


# ============================================================
# 五、自检
# ============================================================
def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _selfCheck() -> int:
    """`python dirNamePlace.py [--apply]` —— 判据速览 + 全库 dry-run 报告。"""
    _fixConsole()
    apply = "--apply" in sys.argv[1:]
    print("dirNamePlace.py _VERSION :", _VERSION)
    print("DIR_PLACE_PATTERN        :", basicSettings.DIR_PLACE_PATTERN)
    print("DIR_PLACE_BLACKLIST      :", list(basicSettings.DIR_PLACE_BLACKLIST))
    print("DIR_PLACE_MAX_UP         :", basicSettings.DIR_PLACE_MAX_UP)
    print("库                       : %s" % (sqliteCommon.dbFilePath() or "(未连接)"))
    print("")
    for sample in ("2013.07.26 华盛顿", "20101218 Michael's Home", "2011聚会",
                   "20051229", "201105", "BaiRuiQin", "廉家老照片",
                   "2013.07.24～25 康宁及赫尔希", "Friends", "MOT Friends"):
        got = parseDirName(sample)
        print("  %-26s %-5s %-22s %s"
              % (sample, "采纳" if got["isPlace"] else "排除",
                 got["placeName"] or "-", got["reasonText"]))
    print("")
    report = rebuildDirPlaces(dryRun=not apply, refresh="--refresh" in sys.argv[1:])
    print("---- %s ----" % ("实跑" if apply else "dry-run（未写库）"))
    print("  pb_photo 总行数          : %d" % report["total"])
    print("  将填 placeNameDir        : %d" % report["candidates"])
    print("  已填（原样不动）         : %d（其中漂移 %d 行）"
          % (report["alreadyFilled"], len(report["drift"])))
    print("  解不出名字               : %d（其中从祖父目录取到的 %d）"
          % (report["noClue"], report["viaAncestor"]))
    print("  不同父目录名             : %d" % len(report["dirStats"]))
    for one in report["dirStats"]:
        print("    %-34s n=%-5d %-5s %-24s %s"
              % (one["dir"][:34], one["count"],
                 "采纳" if one["isPlace"] else "排除",
                 one["placeName"] or "-", one["reasonText"]))
    for line in report["nextSteps"]:
        print("  >", line)
    return 0


if __name__ == "__main__":
    sys.exit(_selfCheck())
