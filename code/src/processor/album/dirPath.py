#! /usr/bin/env python3
#encoding: utf-8

#Filename: dirPath.py
#Description: 「文件夹」浏览的**目录键唯一口径**（R16 · DR-60）
#
# 目录 / 张数全部现推自 `pb_photo.relPath`（扫描器写的既有事实，见
# `processor/scanner/walker.makeRelPath`），**不落任何表、不新增列**。
#
# ============================================================
# 一、目录键长什么样
# ============================================================
#   relPath = "2013.07.26 华盛顿/DSC_0001.jpg"  ->  目录键 = "2013.07.26 华盛顿"
#   relPath = "IMG_0001.jpg"（直接放在 photo 根下）->  目录键 = ""（**空串 = 根目录**）
#
#   根目录用**空串**表示，不是 "/"、不是 "."：它是「没有父目录」这个事实的
#   最自然编码，且 `GROUP BY` 天然把它聚成一组。
#
# ============================================================
# 二、SQL 侧：只用两个内建函数，且必须先登记进白名单
# ============================================================
#   rtrim(rtrim(relPath, replace(relPath, '/', '')), '/')
#
#   ⚠️⚠️ **`RTRIM` 与 `REPLACE` 必须已登记在
#     `database/queryCommon._SQL_FUNCTIONS`**（那是**手写枚举**，不会自动跟上）。
#     不登记则 `checkColumns()` 把它们判成「未知标识符」并抛 QuerySqlError，
#     **整条查询当场报错**，而报错原文只说「未知标识符 RTRIM」—— 与本业务
#     毫无字面关联（R4b 补 `NULLIF` 时踩过一次同一个坑）。
#
#   为什么这个写法成立（逐例，见文件末尾 `__main__` 的自检表）：
#     第一次 rtrim 的 X 是整条 relPath，而 **Y 是「这条路径里所有非斜杠字符」
#     的集合**；要从右侧裁掉的恰好是最后一段（它的字符**全都**属于 Y）
#     ⇒ 它必然停在**最后一个 `/`** 处，既不会提前停，也不会多吃一段。
#     `relPath` 里没有 `/` 时全部被裁掉，正好得到根目录的空串。
#
# ============================================================
# 三、Python 侧：取「最后一个分隔符之前」的前缀 —— 与上面**逐字等价**
# ============================================================
#   等价性证明：SQL 的结果 = 「到最后一个 `/` 为止的前缀」再去掉尾斜杠，
#   而没有 `/` 时为空串（根目录）。
#   ⚠️⚠️ **不能写成 `text[:text.rfind('/')]`** —— 没有分隔符时 `rfind` 返回
#     **-1**，而 `text[:-1]` 是「去掉最后一个字符」，不是空串：
#     `abc.jpg` 会被算成 `abc.jp`（自检表当场抓到过这个 bug）。
#     必须显式判 `index >= 0`。
#   ⚠️ 也不要改成 `os.path.dirname`：它在 Windows 上认 `\` 并返回反斜杠版本，
#     与 SQL 侧（只认 `/`）**不一致**；那正是「树说 45 张、网格说 44 张」的配方。
#
# ============================================================
# 四、严禁 LIKE
# ============================================================
#   绝不可以用 `relPath LIKE 'dir/%'` 代替等值比较：
#     ① 它会把**子目录**的照片一并捞进来（本功能的口径是「仅当前目录直属」）；
#     ② 目录名里的 `%` 与 `_` 在 LIKE 下是**通配符** —— 本库实测存在
#        `2016_05_01_三亚` 这类目录名，用它去 LIKE 会**静默多报**，且零报错。
#
# ============================================================
# 五、消费点（三处都必须调本模块，不许各写一遍）
# ============================================================
#   ① `api/album.py`：`GROUP BY <dirKeySql>` 聚合出「不同目录 -> 张数」
#   ② `api/browse.py` 的 `listPhotos(dirPath=...)`：`WHERE <dirKeySql> = %s`
#   ③ 前端：树节点名 / 面包屑由本模块的 `dirNameOf` / `ancestorsOf` 派生
#      （前端只做展示，键本身由服务端给出）

import os
import re
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/album
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as comBS                         # noqa: E402

_VERSION = "20261016"

_LOG = misc.setLogNew("dirPath", "dirpath.log")

#: 目录分隔符。**必须与 `walker.makeRelPath()` 写进 `relPath` 的那个字符一致**：
#: relPath 是 `a/b/c.jpg` 这种**正斜杠**形态（Windows 上也是），
#: 所以这里不是 `os.sep`、也不接受 `\\`。
_DIR_SEP: str = comBS.ALBUM_DIR_SEPARATOR

#: 根目录的**保留 token**（前后端共用同一个常量，见 basicSettings 的说明）。
#: ⚠️ 为什么必须有它：空串在 HTTP query 上会被前端 `paramsSerializer` 丢弃
#:    （`api/request.js`），在 scope 载荷上会被 `scopeParam()` / `parseScope()`
#:    丢掉 —— 两处都会让「根目录」静默退化成「全库」。详见 `normalizeDirKey`。
ALBUM_ROOT_TOKEN: str = comBS.ALBUM_ROOT_TOKEN


def dirKeySql(alias: str = "p") -> str:
    """目录键的 **SQL 表达式**（返回 目录 或 空串 表示根目录）。

    ⚠️ 返回的是**表达式**，调用方把它拼进 SQL 的 `WHERE` / `GROUP BY`：
        与 `placeStore.placeKeySql()` 的用法同构（那里拼的是聚合键）。
    ⚠️ 表达式里的表别名来自本函数参数，**值是绑定进 `%s` 的**，不要反过来。
    """
    name = str(alias or "p")
    return ("rtrim(rtrim(%s.relPath, replace(%s.relPath, '%s', '')), '%s')"
            % (name, name, _DIR_SEP, _DIR_SEP))


def dirKeyOf(relPath: str) -> str:
    """目录键的 **Python 实现**；必须与 `dirKeySql()` 逐字等价（见文件头二/三节）。

    用途：逐行的场景（组装树、拼面包屑、自检对照）。
    SQL 口径的用途：聚合（`GROUP BY`）与筛选（`WHERE ... = %s`）。
    两处必须给同一个答案 —— 不一致的症状是「树说 45 张、网格说 44 张」。
    """
    text = str(relPath if relPath is not None else "")
    index = text.rfind(_DIR_SEP)
    # ⚠️ 必须显式判 `-1`：`text[:-1]` 是"去掉末字符"，不是空串（见文件头第三节）。
    return text[:index] if index >= 0 else ""


def dirNameOf(dirKey: str) -> str:
    """目录键 -> **末段名称**（树节点与面包屑显示用）。根目录返回空串。"""
    text = str(dirKey or "")
    index = text.rfind(_DIR_SEP)
    return text[index + 1:] if index >= 0 else text


def parentKeyOf(dirKey: str) -> str:
    """目录键 -> 父目录键。根目录的父目录**还是根目录**（不是 None）。

    ⚠️ 返回 "" 而不是 None 是刻意的：调用方用它做「一直往上走到根」的循环，
        None 会让每处都得多写一个判空，而那个判断漏一次就是死循环。
    """
    text = str(dirKey or "")
    index = text.rfind(_DIR_SEP)
    return text[:index] if index >= 0 else ""


def ancestorsOf(dirKey: str) -> list:
    """目录键 -> **由外向内的全部祖先键（含自身）**，根目录在首位。

    `a/b/c` -> `["", "a", "a/b", "a/b/c"]`

    为什么需要它：树是「每个目录键只知道自己」的**扁平聚合结果**（一次 GROUP BY
    拿回几十~几百行），而中间层目录**可能没有直属照片**（照片全在更深一层）——
    不补祖先，`a/b/c` 就会在树里**凭空消失**（它的父 `a/b` 不在结果集里），
    用户看到的是「有些目录点不到」，而接口 200、数据全在。
    """
    text = str(dirKey or "")
    if not text:
        return [""]
    out = [""]
    cursor = ""
    for segment in text.split(_DIR_SEP):
        if not segment:
            continue
        cursor = segment if not cursor else cursor + _DIR_SEP + segment
        out.append(cursor)
    return out


def normalizeDirKey(value) -> str:
    """接口入参 -> 规范目录键。**保留 token -> 空串**（根目录），其余原样。

    ⚠️ **为什么不能靠「不传参数」表示根目录**（本函数存在的唯一理由）
      前端 `api/request.js` 的 `paramsSerializer` 会**主动丢弃**
      `undefined` / `null` / `''`（那是为了修「`hasFace=''` 让后端 422 白屏」）。
      于是 `listPhotos({ dirPath: '' })` 的参数**根本不会发出**，后端收到 `None`
      只能理解成「不筛选」⇒ **返回全库照片** —— 用户以为进了根目录，
      实际看到全库，且不报任何错。所以根目录必须用 `ALBUM_ROOT_TOKEN` 传过来。

    这里只做**入参卫生**（去空白 / 去掉首尾分隔符 / 折叠重复分隔符），
    **不改写键的语义**：键的真身永远来自 `relPath`，本函数不生成键。
    """
    text = str(value if value is not None else "").strip()
    if not text or text == ALBUM_ROOT_TOKEN:
        return ""
    parts = [one for one in text.split(_DIR_SEP) if one]
    return _DIR_SEP.join(parts)


def invalidReason(value) -> str:
    """入参是否**格式非法**；合法返回空串。由 api 层决定抛什么错误码。

    为什么把「合法性」与「归一」分开：`processor` 层不许依赖 `api/dto`（层次纪律），
    所以这里只给**原因**，`ApiError` 由 `api/album.py` 抛。

    三条判据都指向「这个键不可能来自 relPath」：
      · `\\` —— relPath 恒用正斜杠，反斜杠只可能是用户手打或别处拼错；
      · `.` / `..` —— 路径语义残留，磁盘目录名里不可能出现（且本功能不碰文件系统）；
      · 以 `/` 开头的绝对路径 —— 键是**相对 photo 根**的。
        ⚠️ 绝对路径没有安全风险（本功能不打开任何文件），但它 100% 匹配不到
           任何一条数据 ⇒ 返回一个「空的目录」比返回 400 更让人困惑。
    """
    text = str(value if value is not None else "").strip()
    if not text or text == ALBUM_ROOT_TOKEN:
        return ""
    if "\\" in text:
        return "目录键必须用正斜杠 `/` 分隔（收到反斜杠）：%r" % text
    if text.startswith(_DIR_SEP):
        return "目录键是**相对照片库根**的，不能以 `/` 开头：%r" % text
    for segment in text.split(_DIR_SEP):
        if segment in (".", ".."):
            return "目录键里不能出现 `.` / `..` 这种路径语义残留：%r" % text
    return ""


# ============================================================
# 六、目录名排序：**数字感知**（只在这里做一份）
# ============================================================
# 为什么不能直接用字符串序：本库目录名**大量以日期开头**
# （`2013.07.26 华盛顿` / `2016_05_01_三亚` 都是实测形态），
# 纯字符串比较会把 `2013.10.01` 排在 `2013.7.1` **前面**——
# 用户看到的是「树里的顺序莫名其妙」，而排序代码本身没错。
#
# 口径：把名称切成「非数字段 / 数字段」交替的序列，逐段比较；
#   数字段按**数值**比、非数字段按**小写字符串**比；某一段缺失时短的在前。
#   没有数字前缀时**自然退化为普通字符串比较**（不留特例）。
_NUM_SPLIT = re.compile(r"(\d+)")


def nameSortKey(name: str) -> tuple:
    """目录名 -> 可比较的排序键（数字段按数值、其余按小写字符串）。

    ⚠️ 每一段都带一个**类型标记**（0 = 数字段 / 1 = 文本段）：
       否则同一位置上一个 int 一个 str 会在 Python 里直接 `TypeError`
       （`re.split` 的捕获组保证段序列是交替的，但两个不同名字的同一位
       恰好一段是数字、一段是文本完全可能 —— 例如 `2013.07` 与 `abc`）。
    ⚠️ 末尾追加原名：让**同键不同名**（`BaiRuiQin` 与 `bairuiqin` 小写后相同）
       有一个确定的先后，否则同一页刷新两次顺序可能变，看起来像在跳动。
    """
    text = str(name if name is not None else "")
    segments = []
    for index, segment in enumerate(_NUM_SPLIT.split(text)):
        if index % 2 == 1:
            segments.append((0, int(segment), ""))
        else:
            segments.append((1, 0, segment.lower()))
    return (tuple(segments), text)


def sortDirNames(names) -> list:
    """按数字感知口径排序目录名（返回新列表，不改入参）。"""
    return sorted((str(one) for one in (names or ())), key=nameSortKey)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("dirPath.py _VERSION:", _VERSION)
    print("保留 token        :", ALBUM_ROOT_TOKEN, "（分隔符 %r）" % _DIR_SEP)
    # ---- 逐例自检：SQL 表达式与 Python 实现必须给同一个答案 ----
    samples = [
        "abc.jpg",                                  # 根目录
        "a/b/c.jpg",                                # 一级
        "a/b/c/d/e.jpg",                            # 四级
        "2013.07.26 华盛顿/DSC_0001.jpg",           # 空格 + 中文
        "2016_05_01_三亚/IMG_1.JPG",                # 下划线（LIKE 的通配符）
        "50%off/1.jpg",                             # 百分号（LIKE 的通配符）
        "a/b/c",                                    # 无扩展名
        "a/b/",                                     # 尾斜杠（畸形）
        "a//b/c.jpg",                               # 重复分隔符（畸形）
        "/a.jpg",                                   # 前导斜杠（畸形）
    ]
    print("---- dirKeyOf 逐例 ----")
    for one in samples:
        print("  %-40r -> %r" % (one, dirKeyOf(one)))
    # ---- ⭐ 逐例对照：**SQL 表达式与 Python 实现必须给同一个答案** ----
    # 用内存库跑真实的 SQLite，而不是"照着注释再想一遍"：两套口径的等价性
    # 是本功能唯一的真风险点，必须能被**执行**验证（test_api_album.py 里
    # 还有一份对现网数据的对照用例）。
    import sqlite3                                                  # noqa: PLC0415
    handle = sqlite3.connect(":memory:")
    handle.execute("CREATE TABLE t (relPath TEXT)")
    handle.executemany("INSERT INTO t (relPath) VALUES (?)",
                       [(one,) for one in samples])
    sql = handle.execute(
        "SELECT relPath, " + dirKeySql("t") + " AS dirKey FROM t").fetchall()
    mismatch = 0
    print("---- SQL 口径 vs Python 口径 ----")
    for relPath, sqlKey in sql:
        pyKey = dirKeyOf(relPath)
        same = (sqlKey or "") == pyKey
        if not same:
            mismatch += 1
        print("  %-6s %-40r sql=%-22r py=%r"
              % ("OK" if same else "**不同**", relPath, sqlKey, pyKey))
    handle.close()
    print("对照结论：", "全部相同" if not mismatch else "**有 %d 条不同**" % mismatch)
    print("---- 数字感知排序 ----")
    for one in sortDirNames(["2013.10.01 a", "2013.7.1 b", "BaiRuiQin",
                             "bairuiqin", "2016_05_01_三亚", "MOT Friends"]):
        print("  ", one)
    print("---- 祖先前缀 ----")
    for one in ("", "a", "a/b/c"):
        print("  %-8r -> %s" % (one, ancestorsOf(one)))
