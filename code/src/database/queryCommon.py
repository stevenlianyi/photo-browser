#! /usr/bin/env python3
#encoding: utf-8

#Filename: queryCommon.py
#Description: photo-browser **扩展查询层**（手写，非生成物）—— 生成层表达不了的 SELECT 出口
#
# 它解决什么问题
# --------------
#   `auto_generated/sqliteCommon.py` 的 `query_pb_*` 只能按
#   「主键 + 4 个业务码等值 + nullFields(IS NULL)」过滤，
#   ORDER BY 也只在白名单里。**表达不了**下面这些，而它们全是步骤 9 的刚需：
#
#     · 区间比较   shotYear BETWEEN 2000 AND 2010（/api/photos 的年份区间筛选）
#     · 非等值     placeName LIKE '北京%'、hasFace=1（faceCount > 0）
#     · 分组聚合   GROUP BY shotYear / personCode / placeName（时间线、人物卡片、地图）
#     · 布尔列等值 isConfirmed = 0（review 的两个队列）
#     · 多值IN     personCode IN (?,?,?)（多人 AND/OR 筛选）
#
#   而这些都不能塞进 `pb_*.txt` 的查询参数里 —— 那要改生成器 + 重生成 +
#   全库迁移 + 改所有既有调用方（与 `centroid.loadFaceVectors` 里那条注释
#   同一个判断：**为一个人的脸筛两个 0 值去动 schema 不划算**）。
#
# 为什么是「database 层的一个手写模块」而不是「api 层直接写 SQL」
# ----------------------------------------------------------
#   硬约束是「业务层禁止裸 SQL，全部经 sqliteCommon」。步骤 9 的 api 层要做
#   分组聚合与区间筛选，于是需要一个出口。这个出口属于 **数据访问层**：
#   · 它和 `countWhereGeneral`（生成器产出的、带任意 WHERE 的计数）**同一个定位**，
#     只是一个是生成器模板里的，一个是本文件；
#   · 它做了三件生成层不做的事：**只允许 SELECT**、**值一律 %s 占位**、
#     **表名/列名对白名单校验**；
#   · 于是 api 层（与 processor 层）只传「条件片段 + 参数元组」，
#     全项目没有任何一处把用户输入拼进 SQL 字符串。
#
# ⚠️ 一条纪律：本模块**只读**
#    写入一律走 sqliteCommon 的 insert_/update_/delete_/insertManyTableGeneral，
#    否则会把「生成层的 fillStandard / forceColumns / updateColumns 白名单」
#    这些坑全部重开一遍。
#
# 参数写法（与 sqliteHandle 一致）
# ------------------------------
#   sqlstr 里的每一个值都写成 %s，值走 values 元组；**禁止 f-string / % / + 拼值**。
#   toSqliteSQL 会把 %s 转成 ? 并**校验占位符个数与参数个数一致** ——
#   所以拼错一个占位符会立刻抛错，而不是变成一条语义不同的 SQL 悄悄跑偏。

import os
import re
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../database
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from common import sqliteHandle as sqliteHandle                  # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("queryCommon", "querycommon.log")

#: 只允许这些表（直接取生成层的白名单，不另维护一份 —— 两份必然会漂移）
KNOWN_TABLES = frozenset(sqliteCommon.TABLE_COLUMNS.keys())

#: 允许出现在 SQL 里的表名（`pb_photo` 或它的别名 `p`）。正则刻意保守：
#: 只放行 `字母/下划线/数字` 与单个字母别名，杜绝 `a JOIN b` 这类拼接。
_TABLE_TOKEN = re.compile(r"^%s$" % "|".join(sorted(KNOWN_TABLES)))

#: SQL 里出现的裸标识符（表别名 p/f/... 与列名）必须落在这个集合里。
#: 列名从生成层的 TABLE_COLUMNS 取并集 —— **schema 加列自动放行**，不用改这里。
KNOWN_COLUMNS = frozenset(
    col["name"] for cols in sqliteCommon.TABLE_COLUMNS.values() for col in cols)


class QuerySqlError(Exception):
    """SQL 文本没通过本模块的静态校验（表名/列名不合法、或不是 SELECT）。"""


def knownColumns(tableName: str = "") -> frozenset:
    """某张表的合法列名集合（不传表名 = 全部表的列名并集）。"""
    if not tableName:
        return KNOWN_COLUMNS
    if tableName not in KNOWN_TABLES:
        raise QuerySqlError("未知表名: %r" % tableName)
    return frozenset(col["name"] for col in sqliteCommon.TABLE_COLUMNS[tableName])


def assertSelect(sqlstr: str) -> str:
    """校验一条 SQL 文本是「只读 SELECT」，返回去掉首尾空白的原文。

    为什么要校验而不只是约定
    ----------------------
      本模块存在的意义是「给 api 层一个能写 WHERE 的出口」。如果它同时也能写
      UPDATE/DELETE，那么「写入必须走生成层白名单（fillStandard / forceColumns）」
      这条纪律就有了一个绕过口 —— 而那个白名单正是为了防「update_* 写不进 NULL
      却返回成功」这类静默空操作（见 assigner._patchFace 的注释）。
      所以这里**结构性地**只留读。
    """
    text = str(sqlstr or "").strip()
    if not text:
        raise QuerySqlError("SQL 不能为空")
    head = re.sub(r"\s+", " ", text[:64]).strip()
    if not re.match(r"^(SELECT|WITH)\b", head, flags=re.IGNORECASE):
        raise QuerySqlError("本模块只允许 SELECT / WITH，收到: %s..." % text[:48])
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "REPLACE", "ATTACH", "PRAGMA", "VACUUM"):
        if re.search(r"\b%s\b" % word, text, flags=re.IGNORECASE):
            raise QuerySqlError("本模块是只读出口，SQL 里出现 %s: %s..."
                                % (word, text[:48]))
    return text


def _tableTokens(text: str) -> list:
    """抽出 SQL 里出现的 `pb_xxx` 表名片段（含 FROM / JOIN / 子查询里的）。"""
    return re.findall(r"\b(pb_[A-Za-z0-9_]+)\b", text)


def assertKnownTables(sqlstr: str) -> str:
    """SQL 里出现的每个 `pb_*` 必须在已知表集合内。"""
    text = str(sqlstr or "")
    for name in _tableTokens(text):
        if name not in KNOWN_TABLES:
            raise QuerySqlError("SQL 里出现未知表名 %r（不在生成层白名单内）" % name)
    return text


#: SQL 关键字/聚合函数/别名。关键字是语言的一部分，永远不会是列名；
#: 别名是**调用方自己起的短名**（`p` / `f` / `pp` / `cnt` / `val` ...），
#: 不可能预先枚举，所以这里不校验别名，只校验**紧跟在 AS 后面的那个**。
_SQL_KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL",
    "LIKE", "BETWEEN", "ORDER", "BY", "GROUP", "HAVING", "LIMIT", "OFFSET",
    "ASC", "DESC", "DISTINCT", "JOIN", "LEFT", "INNER", "OUTER", "ON", "AS",
    "CASE", "WHEN", "THEN", "ELSE", "END", "WITH", "COLLATE", "NOCASE",
    "EXISTS", "UNION", "ALL", "ANY", "CAST", "IFNULL", "COALESCE", "GLOB",
    "ESCAPE", "RECURSIVE", "NULLS", "FIRST", "LAST",
})

_SQL_FUNCTIONS = frozenset({
    "COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL", "GROUP_CONCAT", "LENGTH",
    "SUBSTR", "TRIM", "UPPER", "LOWER", "ROUND", "ABS", "IF",
})


def _aliasesOf(text: str) -> set:
    """抽出 SQL 里出现过的**别名**：`AS xxx` 与 `FROM t AS xxx` / `FROM t xxx`。"""
    out = set()
    for hit in re.finditer(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", text, flags=re.IGNORECASE):
        out.add(hit.group(1))
    for hit in re.finditer(r"\b(?:FROM|JOIN)\s+[A-Za-z_][A-Za-z0-9_]*"
                           r"(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
                           text, flags=re.IGNORECASE):
        if hit.group(1):
            out.add(hit.group(1))
    return out


def checkColumns(sqlstr: str, tableName: str = "") -> list:
    """找出 SQL 里出现的**未知裸标识符**并抛错；返回全部识别到的标识符。

    判别顺序（每一步都是为了让下一步只看该看的东西）
    --------------------------------------------------
      1. 去掉字符串字面量      —— 否则 '北京' 里的字会被当列名
      2. 去掉表名 `pb_xxx`     —— 表名由 assertKnownTables 单独校验
      3. 收走别名（AS / FROM t x）—— 别名是调用方起的，不在 schema 里
      4. 剩下的是列名 + 关键字 + 函数名 + 限定前缀（``p.`` / ``f.``）
         → 关键字/函数名/限定前缀放行，剩下的**必须**是 schema 里的列

    ⚠️ 这是**尽力而为**的静态检查，不是完整的 SQL 解析器：
      它能挡住「把用户输入直接当列名拼进来」这一类错误（那种写法本来也过不了
      toSqliteSQL 的占位符个数校验）。真正保证「值不被拼接」的是 %s 占位符纪律，
      本函数是第二道防线。
    """
    text = re.sub(r"'[^']*'", " ", str(sqlstr or ""))
    text = re.sub(r'"[^"]*"', " ", text)
    # ⚠️ 必须先抹掉 %s 占位符：`%` 是非单词字符，于是 `\bs\b` 会把
    #    「%s」里的 s 当成一个标识符 —— 而每个真实查询都带占位符，
    #    不抹掉的话**每一条**查询都会误报「未知标识符 s」。
    text = text.replace("%s", " ")
    # ⚠️ 别名必须**在剥掉表名之前**抽：`FROM pb_photo p` 去掉表名后变成
    #    `FROM  p`，那条正则会把 `p` 当成表名吃掉，别名就丢了。
    aliases = {a.lower() for a in _aliasesOf(text)}
    text = re.sub(r"\bpb_[A-Za-z0-9_]+\b", " ", text)
    found = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text))
    lowered = {f.lower(): f for f in found}

    allowed = knownColumns(tableName) if tableName else KNOWN_COLUMNS
    allowedLow = {c.lower() for c in allowed}
    drop = ({k.lower() for k in _SQL_KEYWORDS} | {k.lower() for k in _SQL_FUNCTIONS}
            | {k.lower() for k in KNOWN_TABLES})

    unknown = []
    for low, original in lowered.items():
        if low in drop or low in allowedLow or low in aliases:
            continue
        unknown.append(original)
    if unknown:
        raise QuerySqlError("SQL 里出现未知标识符 %s（不在生成层列白名单内，"
                            "schema 加了列请重跑生成器）" % sorted(unknown)[:6])
    return sorted(found)


def selectList(sqlstr: str, values=(), dbFile: str = None, strict: bool = True) -> list:
    """跑一条只读 SELECT，返回 list[dict]（空列表 = 没查到，不是出错）。

    values 里的值一律绑定，**不要**拼进 sqlstr。
    strict=True 时额外做列名白名单校验（正则扫一遍，微秒级；关掉只为压测）。
    """
    text = assertSelect(assertKnownTables(sqlstr))
    if strict:
        checkColumns(text)
    if dbFile:
        sqliteCommon.dbHandle(dbFile)              # DR-10：必须显式才切库
    db = sqliteCommon.dbHandle()
    if db.executeRead(text, tuple(values or ())) == sqliteHandle.RET_ERROR:
        _LOG.error("selectList 执行失败: %s | err=%s", text, db.lastErrMsg)
        return []
    return db.fetchAll()


def selectOne(sqlstr: str, values=(), dbFile: str = None, strict: bool = True) -> dict:
    """跑一条只读 SELECT，取第一行（无行返回 {}）。"""
    rows = selectList(sqlstr, values, dbFile, strict)
    return rows[0] if rows else {}


def selectValue(sqlstr: str, values=(), default=None, dbFile: str = None,
                strict: bool = True):
    """跑一条只读聚合 SELECT（COUNT/SUM/MAX...），取首行首列。

    ⚠️ 聚合**一定**要给别名（``COUNT(*) AS rowNum``），否则 ``dict(sqlite3.Row)``
       在列名重复时会静默丢列 —— 这与生成层 countWhereGeneral 的写法一致。
    """
    text = assertSelect(assertKnownTables(sqlstr))
    if strict:
        checkColumns(text)
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    db = sqliteCommon.dbHandle()
    if db.executeRead(text, tuple(values or ())) == sqliteHandle.RET_ERROR:
        _LOG.error("selectValue 执行失败: %s | err=%s", text, db.lastErrMsg)
        return default
    return db.fetchValue(default)


def countWhere(tableName: str, whereSqlstr: str, keyValues=()) -> int:
    """带条件计数（转发到生成层的 countWhereGeneral，口径完全一致）。"""
    return sqliteCommon.countWhereGeneral(tableName, whereSqlstr, keyValues)


def tableExists(tableName: str) -> bool:
    """表在不在（api 层判「库还没建」时用，避免抛 sqlite 错误）。"""
    return sqliteCommon.chkTableExist(tableName)


def columnsOf(tableName: str) -> list:
    """列名列表（给 dto 之类的模块做字段白名单）。"""
    if tableName not in KNOWN_TABLES:
        raise QuerySqlError("未知表名: %r" % tableName)
    return [col["name"] for col in sqliteCommon.TABLE_COLUMNS[tableName]]


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("queryCommon.py _VERSION:", _VERSION)
    print("已知表:", sorted(KNOWN_TABLES))
    print("库    :", sqliteCommon.dbFilePath() or "(未连接)")
    _sql = ("SELECT shotYear AS year, COUNT(*) AS cnt FROM pb_photo"
            " WHERE delFlag = %s AND shotYear BETWEEN %s AND %s"
            " GROUP BY shotYear ORDER BY shotYear DESC")
    print("\n示例 SQL:", _sql)
    print("识别到的标识符:", checkColumns(_sql, "pb_photo"))
    print("执行:", selectValue("SELECT COUNT(*) AS rowNum FROM pb_photo"
                               " WHERE delFlag = %s", ("0",)))
    for _bad in ("UPDATE pb_photo SET faceCount = 0",
                 "SELECT * FROM pb_secret",
                 "SELECT bogusCol FROM pb_photo"):
        try:
            assertKnownTables(assertSelect(_bad))
            checkColumns(_bad)
            print("未拦截（不该发生）:", _bad)
        except QuerySqlError as _e:
            print("已拦截:", _bad, "->", _e)
