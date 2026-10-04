#! /usr/bin/env python3
#encoding: utf-8

#Filename: sqliteCodeGenerator.py
#Description: photo-browser SQLite 代码生成器
#              database/pb_*.txt（唯一数据源） -> database/auto_generated/sqliteCommon.py
#
# 纪律（数据库设计.md §1.2 / 开发计划 §6.4）：
#   1. **.txt 是唯一权威定义**：改表 = 改 txt + 重跑本生成器；
#   2. **生成物只落 database/auto_generated/**，且**禁止手工编辑**；
#   3. 生成的 SQL 里值一律写 %s 占位（由 common/sqliteHandle 转成 ?，防注入）；
#   4. 不建物理外键（D-2），关联一律用业务编码 photoCode / personCode。
#
# 用法
# ----
#   # 全量：处理本目录下所有 pb_*.txt
#   python database\sqliteCodeGenerator.py
#   # 只处理指定表
#   python database\sqliteCodeGenerator.py -i database\pb_photo.txt
#   # 只跑自检（类型映射 / 索引清单 / .txt 规范），不写文件
#   python database\sqliteCodeGenerator.py --selftest
#   # 强制用内置模板覆盖已有产物的 #common 区段
#   python database\sqliteCodeGenerator.py --reset-common
#
# 类型映射（开发计划 §6.1，**顺序敏感**）
# ---------------------------------------
#   INT AUTO_INCREMENT PRIMARY KEY  ->  INTEGER PRIMARY KEY AUTOINCREMENT
#   BIGINT/INT/SMALLINT/TINYINT     ->  INTEGER
#   VARCHAR/CHAR/MEDIUMTEXT/TEXT    ->  TEXT
#   DECIMAL/FLOAT/DOUBLE            ->  NUMERIC
#   MEDIUMBLOB                      ->  BLOB
#
# ⚠️ 为什么「顺序敏感」：'INT' 是 'BIGINT' 的子串、'INT AUTO_INCREMENT PRIMARY KEY'
#    又含 'INT'。若把自增主键规则排在整数规则之后，recID 会被降级成普通 INTEGER，
#    丢掉 AUTOINCREMENT（表现：删记录后 recID 不再单调、且 PRAGMA table_info 里
#    类型不是 INTEGER PRIMARY KEY）。所以自增规则**必须第一条**。

import os
import re
import sys
import time

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

_VERSION = "20261004"

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../src/database
DEFAULT_OUTPUT = os.path.join(_HERE_DIR, "auto_generated", "sqliteCommon.py")

# 缩进
INDENT = 4
TS = " " * INDENT
TS2 = TS * 2
TS3 = TS * 3


# ============================================================
# 一、类型映射（顺序敏感！第一条必须是被完整串命中的自增主键规则）
# ============================================================

# (关键字元组, SQLite 类型) —— 按顺序逐条匹配 declString（大写），
# 命中即止。改这里的顺序前请先读文件头「为什么顺序敏感」。
SQLITE_TYPE_RULES = (
    (("INT AUTO_INCREMENT PRIMARY KEY",), "INTEGER PRIMARY KEY AUTOINCREMENT"),
    (("BIGINT", "INT", "SMALLINT", "TINYINT", "MEDIUMINT", "INTEGER"), "INTEGER"),
    (("VARCHAR", "CHAR", "MEDIUMTEXT", "LONGTEXT", "TINYTEXT", "TEXT"), "TEXT"),
    (("DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL"), "NUMERIC"),
    (("MEDIUMBLOB", "LONGBLOB", "TINYBLOB", "BLOB"), "BLOB"),
)

# 自检用的映射断言（类型映射错了这里就会炸）
TYPE_MAPPING_SELFCHECK = (
    ("INT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("INT", "INTEGER"),
    ("INT NOT NULL", "INTEGER"),
    ("BIGINT", "INTEGER"),
    ("SMALLINT", "INTEGER"),
    ("TINYINT", "INTEGER"),
    ("VARCHAR(64)", "TEXT"),
    ("VARCHAR(1024) NOT NULL", "TEXT"),
    ("CHAR(64)", "TEXT"),
    ("CHAR(1)", "TEXT"),
    ("MEDIUMTEXT", "TEXT"),
    ("DECIMAL(10,7)", "NUMERIC"),
    ("DECIMAL(6,4)", "NUMERIC"),
    ("FLOAT", "NUMERIC"),
    ("DOUBLE", "NUMERIC"),
    ("MEDIUMBLOB", "BLOB"),
)


def decodeSqliteType(declString):
    """把 .txt 里的类型+约束串（'VARCHAR(64) NOT NULL'）映射成 SQLite 声明类型。

    顺序敏感：见 SQLITE_TYPE_RULES 的注释。

    Raises
    ------
    ValueError —— 没有任何规则命中（说明 .txt 写了本项目不认识的类型）
    """
    upper = str(declString).upper()
    for keywords, sqliteType in SQLITE_TYPE_RULES:
        for keyword in keywords:
            if keyword in upper:
                return sqliteType
    raise ValueError("无法识别的类型: %r" % declString)


# ============================================================
# 二、表级常量（来自 plan/数据库设计.md，**不是**从 .txt 推导的）
# ============================================================

# §二 表清单：表名 -> 中文名。**dict 的声明顺序就是建库顺序**，故另存一份 TABLE_ORDER。
TABLE_ORDER = (
    "pb_family",
    "pb_person",
    "pb_person_category",
    "pb_photo",
    "pb_face",
    "pb_person_centroid",
    "pb_photo_person",
    "pb_scan_job",
)

TABLE_CN = {
    "pb_family": "家庭组",
    "pb_person": "人员",
    "pb_person_category": "人员分类",
    "pb_photo": "照片",
    "pb_face": "人脸",
    "pb_person_centroid": "人员年代桶质心",
    "pb_photo_person": "照片-人员关联",
    "pb_scan_job": "扫描任务",
}

# §1.4 尾部标准七字段（每表必带、顺序固定）—— 生成时校验
STANDARD_TAIL_FIELDS = (
    "label", "memo", "regID", "regYMDHMS", "modifyID", "modifyYMDHMS", "delFlag",
)

# §五 索引清单里「**不由 .txt 的 UNIQUE 关键字自动生成**」的那部分。
#   .txt 里写了 UNIQUE 的列 -> 生成器自动产出 idx_<表>_<字段> 的 UNIQUE 索引；
#   下面登记的是额外的普通索引 / 复合唯一索引 / 部分索引。
#   元素 = (索引名, [列...], 是否 UNIQUE, 部分索引的 WHERE 条件或 None)
INDEX_SPEC = {
    "pb_photo": (
        ("idx_pb_photo_fileHash", ("fileHash",), False, None),
        ("idx_pb_photo_shotYear", ("shotYear",), False, None),
        ("idx_pb_photo_scanState", ("scanState",), False, None),
    ),
    "pb_face": (
        ("idx_pb_face_photoCode", ("photoCode",), False, None),
        ("idx_pb_face_personCode", ("personCode",), False, None),
        # 待确认队列专用部分索引（D-4：personCode 为空即待确认）
        ("idx_pb_face_personCode_isnull", ("personCode",), False, "personCode IS NULL"),
    ),
    "pb_person": (
        # displayName 在 .txt 里没写 UNIQUE（只写了 NOT NULL + 注释「唯一」），
        # 但 §五 要求唯一索引 —— 说明「唯一性约束」有两处来源，别只盯 .txt
        ("idx_pb_person_displayName", ("displayName",), True, None),
    ),
    "pb_person_centroid": (
        ("idx_pb_person_centroid_personCode_bucketKey",
         ("personCode", "bucketKey"), True, None),
    ),
    "pb_photo_person": (
        ("idx_pb_photo_person_personCode", ("personCode",), False, None),
        ("idx_pb_photo_person_photoCode", ("photoCode",), False, None),
    ),
}

# §五 的**期望索引名全集**（自检用：索引清单与文档漂移了就报错）
EXPECTED_INDEX_NAMES = {
    "pb_photo": ("idx_pb_photo_photoCode", "idx_pb_photo_relPathHash",
                 "idx_pb_photo_fileHash", "idx_pb_photo_shotYear", "idx_pb_photo_scanState"),
    "pb_face": ("idx_pb_face_faceCode", "idx_pb_face_photoCode",
                "idx_pb_face_personCode", "idx_pb_face_personCode_isnull"),
    "pb_person": ("idx_pb_person_personCode", "idx_pb_person_displayName"),
    "pb_person_category": (),
    "pb_family": ("idx_pb_family_familyCode",),
    "pb_scan_job": ("idx_pb_scan_job_jobCode",),
    "pb_person_centroid": ("idx_pb_person_centroid_personCode_bucketKey",),
    "pb_photo_person": ("idx_pb_photo_person_linkKey",
                        "idx_pb_photo_person_personCode",
                        "idx_pb_photo_person_photoCode"),
}

# 各表默认的 upsert 冲突键（业务幂等键；调用方可覆盖）
CONFLICT_COLUMNS = {
    "pb_family": ("familyCode",),
    "pb_person": ("personCode",),
    "pb_person_category": (),          # 唯一性由业务保证 (personCode, category)
    "pb_photo": ("photoCode",),
    "pb_face": ("faceCode",),
    "pb_person_centroid": ("personCode", "bucketKey"),
    "pb_photo_person": ("linkKey",),
    "pb_scan_job": ("jobCode",),
}

# 各表 query 的等值过滤字段（都带索引，或表极小）
QUERY_FILTER_FIELDS = {
    "pb_family": ("familyCode",),
    "pb_person": ("personCode", "displayName", "vcardUid", "familyGroupCode"),
    "pb_person_category": ("personCode", "category"),
    "pb_photo": ("photoCode", "relPathHash", "fileHash", "dupOfPhotoCode"),
    "pb_face": ("faceCode", "photoCode", "personCode"),
    "pb_person_centroid": ("personCode", "bucketKey"),
    "pb_photo_person": ("linkKey", "photoCode", "personCode"),
    "pb_scan_job": ("jobCode", "jobStatus"),
}

# 各表允许的 ORDER BY 字段（生成器写死白名单，杜绝 ORDER BY 注入）
ORDER_FIELDS = {
    "pb_family": ("recID", "familyCode", "familyName"),
    "pb_person": ("recID", "personCode", "displayName"),
    "pb_person_category": ("recID", "personCode", "category"),
    "pb_photo": ("recID", "shotYear", "takenAt", "fileSize", "scannedYMDHMS"),
    "pb_face": ("recID", "shotBucket", "quality", "faceCode"),
    "pb_person_centroid": ("recID", "personCode", "bucketKey", "sampleCount"),
    "pb_photo_person": ("recID", "photoCode", "personCode", "confidence"),
    "pb_scan_job": ("recID", "jobStatus", "jobCode"),
}

_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_RE = re.compile(r"^([A-Z]+)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?$")
_COMMENT_RE = re.compile(r"COMMENT\s+'(.*)'\s*$", re.IGNORECASE)
_DEFAULT_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_DEFAULT_STR_RE = re.compile(r"^'.*'$")


class GenError(Exception):
    """表定义不合法（.txt 写错了）"""


# ============================================================
# 三、解析 .txt
# ============================================================

def parseFieldLine(line, tableName, lineno):
    """解析一行字段定义 -> 字段元数据 dict。

    行格式：名称 类型 [NOT NULL|UNIQUE|NULL|DEFAULT x|COMMENT '...']
    例：  relPath VARCHAR(1024) NOT NULL COMMENT '相对photo目录路径 保留原值不改写'
    """
    text = line.strip()
    if not text:
        return None
    if text.startswith("#") or text.startswith("--"):
        return None

    # 1) 先摘 COMMENT '...'（注释里可能有空格，不能按空格切）
    comment = ""
    m = _COMMENT_RE.search(text)
    if m:
        comment = m.group(1).strip()
        text = text[:m.start()].strip()
    if not text:
        raise GenError("%s.txt 第 %d 行: 只有 COMMENT 没有字段: %r" % (tableName, lineno, line))

    tokens = text.split()
    if len(tokens) < 2:
        raise GenError("%s.txt 第 %d 行: 至少要有「字段名 + 类型」: %r" % (tableName, lineno, line))

    name = tokens[0]
    if not _FIELD_NAME_RE.match(name):
        raise GenError("%s.txt 第 %d 行: 非法字段名 %r" % (tableName, lineno, name))

    typeString = tokens[1]
    # 容错：类型与括号被空格分开（'VARCHAR (64)'）
    if typeString.endswith(")") and "(" not in typeString and len(tokens) > 2:
        typeString = typeString + tokens[2]
        tokens = tokens[:2] + tokens[3:]

    # 2) 解析约束
    notNull = False
    unique = False
    primaryKey = False
    autoIncrement = False
    default = None
    restTokens = []
    i = 2
    while i < len(tokens):
        token = tokens[i].upper()
        if token == "NOT" and i + 1 < len(tokens) and tokens[i + 1].upper() == "NULL":
            notNull = True
            i += 2
        elif token == "NULL":
            notNull = False
            i += 1
        elif token == "UNIQUE":
            unique = True
            i += 1
        elif token == "PRIMARY" and i + 1 < len(tokens) and tokens[i + 1].upper() == "KEY":
            primaryKey = True
            i += 2
        elif token == "AUTO_INCREMENT":
            autoIncrement = True
            i += 1
        elif token == "DEFAULT":
            if i + 1 >= len(tokens):
                raise GenError("%s.txt 第 %d 行: DEFAULT 后面没有值" % (tableName, lineno))
            default = tokens[i + 1]
            i += 2
        else:
            restTokens.append(tokens[i])
            i += 1
    if restTokens:
        raise GenError("%s.txt 第 %d 行: 不认识的约束 %s" % (tableName, lineno, restTokens))

    # 3) 类型 -> SQLite
    #    declString 按**固定顺序**重排（AUTO_INCREMENT 在 PRIMARY KEY 之前），
    #    这样 .txt 里约束书写顺序变了也不会影响类型映射结果
    declParts = [typeString]
    if autoIncrement:
        declParts.append("AUTO_INCREMENT")
    if primaryKey:
        declParts.append("PRIMARY KEY")
    if notNull:
        declParts.append("NOT NULL")
    if unique:
        declParts.append("UNIQUE")
    declString = " ".join(declParts)
    try:
        sqliteType = decodeSqliteType(declString)
    except ValueError as e:
        raise GenError("%s.txt 第 %d 行(%s): %s" % (tableName, lineno, name, e))

    # 4) 类型括号参数：VARCHAR(64) -> 64；DECIMAL(10,7) -> 10 / 7
    typeBase = typeString.split("(")[0].strip().upper()
    typeLength = None
    typeScale = None
    tm = _TYPE_RE.match(typeString.strip().upper())
    if tm:
        typeBase = tm.group(1)
        if tm.group(2) is not None:
            typeLength = int(tm.group(2))
        if tm.group(3) is not None:
            typeScale = int(tm.group(3))

    # 5) DEFAULT 值必须是数字或单引号串（否则 SQLite DDL 会被拼坏）
    if default is not None:
        if not (_DEFAULT_NUM_RE.match(default) or _DEFAULT_STR_RE.match(default)):
            raise GenError("%s.txt 第 %d 行(%s): DEFAULT 值只支持数字或'字符串'，收到 %r"
                           % (tableName, lineno, name, default))

    # 6) SQLite 列定义片段
    if primaryKey and autoIncrement:
        dcl = "INTEGER PRIMARY KEY AUTOINCREMENT"
        notNull = True
    else:
        dcl = sqliteType
        if notNull:
            dcl += " NOT NULL"
        if default is not None:
            dcl += " DEFAULT " + default

    return {
        "name": name,
        "typeString": typeString,
        "typeBase": typeBase,
        "typeLength": typeLength,
        "typeScale": typeScale,
        "sqliteType": sqliteType,
        "notNull": notNull,
        "unique": unique,
        "primaryKey": primaryKey,
        "autoIncrement": autoIncrement,
        "default": default,
        "comment": comment,
        "dcl": dcl,
    }


def parseTableFile(fileName):
    """读一个 pb_xxx.txt -> 表元数据 dict（字段列表 + 派生索引清单）"""
    tableName = os.path.splitext(os.path.basename(fileName))[0]
    if not tableName.startswith("pb_"):
        raise GenError("表名必须以 pb_ 开头: %s" % tableName)
    if tableName not in TABLE_CN:
        raise GenError("表名 %s 不在 plan/数据库设计.md §二 表清单里（生成器未登记）" % tableName)

    with open(fileName, "r", encoding="utf-8") as hFile:
        lines = hFile.readlines()

    fields = []
    for lineno, line in enumerate(lines, start=1):
        field = parseFieldLine(line, tableName, lineno)
        if field is not None:
            fields.append(field)
    if not fields:
        raise GenError("%s.txt 里没有任何字段定义" % tableName)

    # ---- 校验 ----
    primaryList = [f["name"] for f in fields if f["primaryKey"]]
    if len(primaryList) != 1:
        raise GenError("%s: 必须且只能有 1 个主键，实际 %s" % (tableName, primaryList))
    if fields[0]["name"] != primaryList[0]:
        raise GenError("%s: 主键 %s 必须是首行字段（当前首行是 %s）"
                       % (tableName, primaryList[0], fields[0]["name"]))
    if fields[0]["typeString"].upper() != "INT" or not fields[0]["autoIncrement"]:
        raise GenError("%s: recID 必须是 'INT AUTO_INCREMENT PRIMARY KEY'（DR-9，不用 BIGINT），"
                       "当前是 %r" % (tableName, fields[0]["typeString"]))
    if fields[0]["sqliteType"] != "INTEGER PRIMARY KEY AUTOINCREMENT":
        raise GenError("%s: recID 的类型映射结果应为 'INTEGER PRIMARY KEY AUTOINCREMENT'，"
                       "实际 %r —— 类型映射规则顺序被改坏了？" % (tableName, fields[0]["sqliteType"]))

    tailNames = tuple(f["name"] for f in fields[-len(STANDARD_TAIL_FIELDS):])
    if tailNames != STANDARD_TAIL_FIELDS:
        raise GenError("%s: 末尾标准七字段不对，应为 %s，实际 %s"
                       % (tableName, list(STANDARD_TAIL_FIELDS), list(tailNames)))

    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        raise GenError("%s: 有重复字段名" % tableName)

    # ---- 索引清单 = .txt 的 UNIQUE 列 + INDEX_SPEC 登记项 ----
    indexes = []
    for field in fields:
        if field["unique"]:
            indexes.append({
                "name": "idx_%s_%s" % (tableName, field["name"]),
                "columns": (field["name"],),
                "unique": True,
                "where": None,
                "from": "txt-UNIQUE",
            })
    for indexName, columns, unique, where in INDEX_SPEC.get(tableName, ()):
        indexes.append({
            "name": indexName,
            "columns": tuple(columns),
            "unique": unique,
            "where": where,
            "from": "INDEX_SPEC",
        })

    # 索引列必须在表里
    fieldSet = set(names)
    for index in indexes:
        for column in index["columns"]:
            if column not in fieldSet:
                raise GenError("%s: 索引 %s 引用了不存在的字段 %s"
                               % (tableName, index["name"], column))
    indexNames = [i["name"] for i in indexes]
    if len(set(indexNames)) != len(indexNames):
        raise GenError("%s: 索引名重复 %s" % (tableName, indexNames))

    # 与 §五 的期望清单比对（防止代码与文档漂移）
    expect = EXPECTED_INDEX_NAMES.get(tableName)
    if expect is not None and tuple(sorted(indexNames)) != tuple(sorted(expect)):
        raise GenError("%s: 索引清单与 plan/数据库设计.md §五 不一致\n  实际: %s\n  期望: %s"
                       % (tableName, sorted(indexNames), sorted(expect)))

    # 派生：query 过滤字段 / 可空字段 / 排序字段
    queryFilters = []
    for name in QUERY_FILTER_FIELDS.get(tableName, ()):
        if name not in fieldSet:
            raise GenError("%s: QUERY_FILTER_FIELDS 里的 %s 不在表里" % (tableName, name))
        queryFilters.append(name)
    nullableFields = tuple(f["name"] for f in fields if not f["notNull"])
    orderFields = []
    for name in ORDER_FIELDS.get(tableName, ("recID",)):
        if name not in fieldSet:
            raise GenError("%s: ORDER_FIELDS 里的 %s 不在表里" % (tableName, name))
        orderFields.append(name)

    return {
        "name": tableName,
        "cnName": TABLE_CN[tableName],
        "fields": fields,
        "indexes": indexes,
        "queryFilters": tuple(queryFilters),
        "nullableFields": nullableFields,
        "orderFields": tuple(orderFields),
        "conflictColumns": tuple(CONFLICT_COLUMNS.get(tableName, ())),
        "primaryKey": primaryList[0],
    }


# ============================================================
# 四、代码生成
# ============================================================

def _py(value):
    """Python 字面量（自动转义中文/引号，生成物里不会因注释里有引号而炸）"""
    return repr(value)


def _tupleLiteral(values):
    if not values:
        return "()"
    if len(values) == 1:
        return "(%s,)" % _py(values[0])
    return "(%s)" % ", ".join(_py(v) for v in values)


HEADER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ======================================================================================
# 自动生成，请勿手改
#   本文件由 database/sqliteCodeGenerator.py 从 database/pb_*.txt 生成。
#   改表 = 改 pb_*.txt + 重跑生成器；直接改这里会在下次生成时被覆盖，
#   且 .txt 与本文件会静默不一致（字段/索引/长度全部对不上）。
#
#   生成时间 : {genTime}
#   生成器   : database/sqliteCodeGenerator.py v{ver}
#   数据源   : {inputs}
#   表数量   : {tableNum} 张，字段 {fieldNum} 个，索引 {indexNum} 个
#
#   上层：processor / engine / api —— **业务层禁止裸 SQL，一律调本文件**（数据库设计.md §1.2）
#   下层：common/sqliteHandle.py（读写双连接 + PRAGMA + %s->? 占位符转换）
#
#   ⚠️ SQLite 没有 VARCHAR(n)：VARCHAR(64) 与 TEXT 完全等价，**长度 (n) 不被强制**。
#      长度约束只存在于本文件的 TABLE_COLUMNS 里，业务层需自行校验。
#   ⚠️ 本模块 **import 时绝不连库**；第一次调用 dbHandle() 时才建连。
# ======================================================================================

import os
import sys
import threading

# 让本文件在任意 cwd 下都能 import 到兄弟包
#（.../src/database/auto_generated/sqliteCommon.py -> .../src）
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD
from common import miscCommon as misc
from common import paths as paths
from common import sqliteHandle as sqliteHandle

_VERSION = "{ver}"

_LOG = misc.setLogNew("sqliteCommon", "sqlitecommon.log")

# 全局读写句柄（懒初始化单例）
_HANDLE = None
_HANDLE_LOCK = threading.RLock()
'''


def genCommonBegin(tables, commonBody=None):
    """生成 #common 区段。

    参数
    ----
    tables     : list[dict] —— 表元数据
    commonBody : str | None —— 已有产物里的 #common 区段原文。
                  传入则**原样保留**（这才是「保留 #common begin/end」的真正含义：
                  通用 CRUD 是跨表资产，不该被逐表重新生成覆盖）；
                  为 None 时用内置模板。
    """
    if commonBody is not None:
        return commonBody

    lines = []
    lines.append("#common begin")
    lines.append("")
    lines.append("# ==========================================================================")
    lines.append("# 通用段（表无关）—— 由生成器维护，跨表复用，禁止手改")
    lines.append("#   包含：表元数据字典 / 句柄 / 存在性判断 / 通用增删改 / 批量 upsert / 自检工具")
    lines.append("# ==========================================================================")
    lines.append("")
    lines.append("")
    lines.append("# ------------------------------------------------------------")
    lines.append("# 1. 表元数据（**从 .txt 抄过来的唯一副本**）")
    lines.append("#    SQLite 不保存 VARCHAR(n) 的长度，也不保存 COMMENT，")
    lines.append("#    所以「长度校验」「字段中文名」只能从这里取。")
    lines.append("# ------------------------------------------------------------")
    lines.append("")

    # 表清单
    lines.append("# 表清单（建库顺序 = 数据库设计.md §二）")
    lines.append("TABLE_ORDER = (")
    for table in tables:
        lines.append("%s%s,   # %s" % (TS, _py(table["name"]), table["cnName"]))
    lines.append(")")
    lines.append("")
    lines.append("# 表名 -> 中文名")
    lines.append("TABLE_CN = {")
    for table in tables:
        lines.append("%s%s: %s," % (TS, _py(table["name"]), _py(table["cnName"])))
    lines.append("}")
    lines.append("")

    # 字段元数据
    lines.append("# 表名 -> 字段元数据列表（顺序 = .txt 里的顺序 = 建表列顺序）")
    lines.append("#   sqliteType: INTEGER / TEXT / NUMERIC / BLOB（INTEGER PRIMARY KEY AUTOINCREMENT 只在 recID）")
    lines.append("#   length/ scale: VARCHAR(64)->64；DECIMAL(10,7)->10/7；其余 None")
    lines.append("TABLE_COLUMNS = {")
    for table in tables:
        lines.append("%s%s: [" % (TS, _py(table["name"])))
        for field in table["fields"]:
            lines.append(TS2 + '{"name": %s, "type": %s,' % (_py(field["name"]), _py(field["typeString"])))
            lines.append(TS3 + '"sqliteType": %s, "length": %s, "scale": %s,'
                         % (_py(field["sqliteType"]), _py(field["typeLength"]), _py(field["typeScale"])))
            lines.append(TS3 + '"notNull": %s, "unique": %s, "primaryKey": %s,'
                         % (_py(field["notNull"]), _py(field["unique"]), _py(field["primaryKey"])))
            lines.append(TS3 + '"autoIncrement": %s, "default": %s, "comment": %s},'
                         % (_py(field["autoIncrement"]), _py(field["default"]), _py(field["comment"])))
        lines.append("%s]," % TS)
    lines.append("}")
    lines.append("")

    # 索引清单
    lines.append("# 表名 -> 索引清单（plan/数据库设计.md §五）")
    lines.append("#   UNIQUE 列的索引名统一 idx_<表>_<字段>；复合索引用 idx_<表>_<字段1>_<字段2>")
    lines.append("TABLE_INDEXES = {")
    for table in tables:
        lines.append("%s%s: (" % (TS, _py(table["name"])))
        for index in table["indexes"]:
            lines.append(TS2 + '{"name": %s, "columns": %s, "unique": %s, "where": %s},'
                         % (_py(index["name"]), _tupleLiteral(index["columns"]),
                            _py(index["unique"]), _py(index["where"])))
        lines.append("%s)," % TS)
    lines.append("}")
    lines.append("")

    # 冲突键 / 过滤字段 / 可空字段 / 排序字段
    lines.append("# 表名 -> upsert 默认冲突键（业务幂等键，见 数据库设计.md §1.6）")
    lines.append("CONFLICT_COLUMNS = {")
    for table in tables:
        lines.append("%s%s: %s," % (TS, _py(table["name"]), _tupleLiteral(table["conflictColumns"])))
    lines.append("}")
    lines.append("")
    lines.append("# 表名 -> query 支持的等值过滤字段（全部带索引）")
    lines.append("QUERY_FILTER_FIELDS = {")
    for table in tables:
        lines.append("%s%s: %s," % (TS, _py(table["name"]), _tupleLiteral(table["queryFilters"])))
    lines.append("}")
    lines.append("")
    lines.append("# 表名 -> 允许为 NULL 的字段（query 的 nullFields 参数白名单：待确认队列等）")
    lines.append("NULLABLE_FIELDS = {")
    for table in tables:
        lines.append("%s%s: %s," % (TS, _py(table["name"]), _tupleLiteral(table["nullableFields"])))
    lines.append("}")
    lines.append("")
    lines.append("# 表名 -> 允许 ORDER BY 的字段（白名单，杜绝排序字段注入）")
    lines.append("ORDER_FIELDS = {")
    for table in tables:
        lines.append("%s%s: %s," % (TS, _py(table["name"]), _tupleLiteral(table["orderFields"])))
    lines.append("}")
    lines.append("")
    lines.append("# 表名 -> 主键名（当前 8 张表统一是 recID，见 数据库设计.md §1.3）")
    lines.append("PRIMARY_KEYS = {")
    for table in tables:
        lines.append("%s%s: %s," % (TS, _py(table["name"]), _py(table["primaryKey"])))
    lines.append("}")
    lines.append("")

    lines.append(COMMON_FUNCTIONS)
    lines.append("")
    lines.append("#common end")
    return "\n".join(lines)


def L(level, text):
    """按缩进级别拼一行。

    ⚠️ 生成器内部**一律用拼接、不用 % 格式化**去拼产物：
       产物里满是 %s / %r，用 % 格式化极易把占位符当成格式符吃掉。
    """
    return " " * (INDENT * level) + text


def _bar(char="=", width=74):
    return char * width


def _filterPairList(filters):
    """query 的等值过滤 for 循环头部：'for fieldName, fieldValue in ((...), (...))'

    ⚠️ 只有一个过滤字段时**必须留尾逗号**，否则外层括号只是「加括号」而非「单元素元组」，
       迭代出来的是字符串，unpack 直接炸（too many values to unpack）。
    """
    if not filters:
        return "()"
    pairList = ", ".join("(" + _py(f) + ", " + f + ")" for f in filters)
    if len(filters) == 1:
        pairList += ","
    return "(" + pairList + ")"


def genCreateCode(table):
    """建表函数：DDL + 索引（数据库设计.md §五）"""
    name = table["name"]
    lines = []
    lines.append("")
    lines.append("# " + _bar())
    lines.append("# " + name + " " + table["cnName"])
    lines.append("# " + _bar())
    lines.append("")
    lines.append("# " + name + " 建表（幂等：已存在直接返回 True，不重复建）")
    lines.append("def create_" + name + "(tableName):")
    lines.append(L(1, '"""建 ' + name + ' 表 + 索引。返回 True = 表已就绪。"""'))
    lines.append(L(1, "if tableName not in TABLE_COLUMNS:"))
    lines.append(L(2, '_LOG.error("create_' + name + ': 非法表名 %r" % tableName)'))
    lines.append(L(2, "return False"))
    lines.append(L(1, "if chkTableExist(tableName):"))
    lines.append(L(2, "return True"))
    lines.append(L(1, "db = dbHandle()"))
    lines.append(L(1, "if db.executeWrite(createTableSQL_" + name + "(tableName)) == sqliteHandle.RET_ERROR:"))
    lines.append(L(2, "return False"))
    lines.append(L(1, "for indexSql in indexSqlList_" + name + "():"))
    lines.append(L(2, "if db.executeWrite(indexSql) == sqliteHandle.RET_ERROR:"))
    lines.append(L(3, "db.rollbackWrite()"))
    lines.append(L(3, "return False"))
    lines.append(L(1, "return chkTableExist(tableName)"))
    lines.append("")

    # DDL
    lines.append("")
    lines.append("")
    lines.append("def createTableSQL_" + name + "(tableName):")
    lines.append(L(1, '"""' + name + ' 的建表 DDL（表名来自调用方，必须是 ' + name + '）。"""'))
    lines.append(L(1, "sqlStr = ("))
    lines.append(L(2, '"CREATE TABLE IF NOT EXISTS " + tableName + " ("'))
    columns = [f["name"] + " " + f["dcl"] for f in table["fields"]]
    for index, dcl in enumerate(columns):
        comma = "," if index < len(columns) - 1 else ""
        lines.append(L(2, '"' + dcl + comma + '"'))
    lines.append(L(2, '");"'))
    lines.append(L(1, ")"))
    lines.append(L(1, "return sqlStr"))
    lines.append("")

    # 索引 SQL
    lines.append("")
    lines.append("")
    lines.append("def indexSqlList_" + name + "():")
    lines.append(L(1, '"""' + name + " 的索引 DDL（清单见 数据库设计.md §五，"
                          "全部 IF NOT EXISTS 故幂等）。\"\"\""))
    lines.append(L(1, "sqlList = ["))
    for index in table["indexes"]:
        unique = "UNIQUE " if index["unique"] else ""
        columnStr = ", ".join(index["columns"])
        where = (" WHERE " + index["where"]) if index["where"] else ""
        sql = ("CREATE " + unique + "INDEX IF NOT EXISTS " + index["name"]
               + " ON " + name + "(" + columnStr + ")" + where + ";")
        lines.append(L(2, _py(sql) + ",") + "   # " + index["from"])
    lines.append(L(1, "]"))
    lines.append(L(1, "return sqlList"))
    lines.append("")
    return "\n".join(lines)


def genQueryCode(table):
    """查询函数：等值过滤 + nullFields + 软删 + 排序 + 分页 + 轻量模式"""
    name = table["name"]
    filters = list(table["queryFilters"])
    sig = ["tableName", "recID = 0"]
    for field in filters:
        sig.append(field + ' = ""')
    sig += ['nullFields = ()', 'delFlag = "0"', 'mode = "full"', 'orderBy = "recID"',
            "descFlag = False", "limitNum = 0", "offsetNum = 0"]
    head = "def query_" + name + "("

    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 查询记录")
    lines.append(head + _wrapSignature(sig, head) + "):")
    lines.append(L(1, '"""查 ' + name + "。"))
    lines.append("")
    lines.append("    参数")
    lines.append("    ----")
    lines.append("    recID        : int  —— 主键精确查，>0 时生效")
    for field in filters:
        lines.append("    %-13s: str  —— 非空时等值过滤" % field)
    lines.append("    nullFields   : tuple  —— 追加 'field IS NULL' 条件，字段必须在 NULLABLE_FIELDS 白名单内")
    lines.append('    delFlag      : str  —— "0" 只看未删（默认）；"1" 只看已删；"" / "*" 不限')
    lines.append('    mode         : str  —— "full" 全部列；"light" 剔除 BLOB 列（人脸向量等），列表页用它省内存')
    lines.append('    orderBy      : str  —— 必须在 ORDER_FIELDS 白名单里，否则回落 recID')
    lines.append("    descFlag     : bool —— 是否倒序")
    lines.append("    limitNum     : int  —— >0 时生效")
    lines.append("    offsetNum    : int  —— 分页偏移")
    lines.append("")
    lines.append("    返回")
    lines.append("    ----")
    lines.append("    list[dict]（空列表 = 没查到，不是出错）")
    lines.append('    """')
    lines.append(L(1, "result = []"))
    lines.append(L(1, "if tableName not in TABLE_COLUMNS:"))
    lines.append(L(2, "return result"))
    lines.append(L(1, "db = dbHandle()"))
    lines.append(L(1, "valuesList = []"))
    lines.append(L(1, "whereList = []"))
    lines.append(L(1, "try:"))
    lines.append(L(2, "try:"))
    lines.append(L(3, "recID = int(recID)"))
    lines.append(L(2, "except (TypeError, ValueError):"))
    lines.append(L(3, "recID = 0"))
    lines.append(L(2, "if recID > 0:"))
    lines.append(L(3, 'whereList.append("recID = %s")'))
    lines.append(L(3, "valuesList.append(recID)"))
    lines.append("")
    if filters:
        lines.append(L(2, "for fieldName, fieldValue in " + _filterPairList(filters) + ":"))
        lines.append(L(3, 'if fieldValue is not None and fieldValue != "":'))
        lines.append(L(4, 'whereList.append(fieldName + " = %s")'))
        lines.append(L(4, "valuesList.append(fieldValue)"))
        lines.append("")
    lines.append(L(2, "for fieldName in (nullFields or ()):"))
    lines.append(L(3, "if fieldName in NULLABLE_FIELDS.get(tableName, ()):"))
    lines.append(L(4, 'whereList.append(fieldName + " IS NULL")'))
    lines.append("")
    lines.append(L(2, 'if delFlag not in (None, "", "*"):'))
    lines.append(L(3, 'whereList.append("delFlag = %s")'))
    lines.append(L(3, "valuesList.append(delFlag)"))
    lines.append("")
    lines.append(L(2, 'columnList = ["*"]'))
    lines.append(L(2, 'if mode == "light":'))
    lines.append(L(3, 'columnList = [c["name"] for c in TABLE_COLUMNS[tableName]'))
    lines.append(L(4, 'if c["sqliteType"] != "BLOB"]'))
    lines.append(L(3, "if not columnList:"))
    lines.append(L(4, 'columnList = ["*"]'))
    lines.append(L(2, 'sqlStr = "SELECT " + ", ".join(columnList) + " FROM " + tableName'))
    lines.append(L(2, "if whereList:"))
    lines.append(L(3, 'sqlStr += " WHERE " + " AND ".join(whereList)'))
    lines.append(L(2, 'orderField = orderBy if orderBy in ORDER_FIELDS.get(tableName, ()) else "recID"'))
    lines.append(L(2, 'sqlStr += " ORDER BY " + orderField'))
    lines.append(L(2, "if descFlag:"))
    lines.append(L(3, 'sqlStr += " DESC"'))
    lines.append(L(2, "if int(limitNum or 0) > 0:"))
    lines.append(L(3, 'sqlStr += " LIMIT %s OFFSET %s"'))
    lines.append(L(3, "valuesList.append(int(limitNum))"))
    lines.append(L(3, "valuesList.append(int(offsetNum or 0))"))
    lines.append("")
    lines.append(L(2, "if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:"))
    lines.append(L(3, "return result"))
    lines.append(L(2, "result = db.fetchAll()"))
    lines.append(L(1, "except Exception as e:"))
    lines.append(L(2, '_LOG.error("query_' + name + ': %s" % e)'))
    lines.append(L(1, "return result"))
    lines.append("")
    return "\n".join(lines)


def genInsertCode(table):
    name = table["name"]
    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 增加记录")
    lines.append("def insert_" + name + "(tableName, dataSet):")
    lines.append(L(1, '"""新增一条 ' + name + "，返回新行 recID（<=0 表示失败）。"))
    lines.append("")
    lines.append("    - 只取 dataSet 中**属于本表**的字段；recID 由库自增，传了也忽略")
    lines.append("    - 值按 .txt 类型归一（INTEGER / NUMERIC / TEXT / BLOB），空数值走库默认值")
    lines.append("    - 未给 delFlag / regYMDHMS 时自动补 comGD.DEL_FLAG_NO / 当前时间")
    lines.append('    """')
    lines.append(L(1, "result = 0"))
    lines.append(L(1, "if tableName not in TABLE_COLUMNS:"))
    lines.append(L(2, "return result"))
    lines.append(L(1, "saveSet, skipList = normalizeDataSet(tableName, dataSet)"))
    lines.append(L(1, 'saveSet.pop("recID", None)'))
    lines.append(L(1, "if skipList:"))
    lines.append(L(2, '_LOG.warning("insert_' + name + ': 跳过无法归一的字段 %s" % skipList)'))
    lines.append(L(1, 'if "delFlag" not in saveSet:'))
    lines.append(L(2, 'saveSet["delFlag"] = comGD.DEL_FLAG_NO'))
    lines.append(L(1, 'if "regYMDHMS" not in saveSet:'))
    lines.append(L(2, 'saveSet["regYMDHMS"] = misc.getTime()'))
    lines.append(L(1, "result = insertTableGeneral(tableName, saveSet)"))
    lines.append(L(1, "return result"))
    lines.append("")
    return "\n".join(lines)


def genInsertManyCode(table):
    name = table["name"]
    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 批量增加记录（executemany + 显式事务）")
    lines.append("def insertMany_" + name + "(tableName, dataSetList):")
    lines.append(L(1, '"""批量新增 ' + name + "（整批一次提交，失败整批回滚），返回写入行数。"))
    lines.append("")
    lines.append("    步骤 3 扫描入库走这里：一次几百行，别一行一提交。")
    lines.append('    """')
    lines.append(L(1, "result = 0"))
    lines.append(L(1, "if tableName not in TABLE_COLUMNS or not dataSetList:"))
    lines.append(L(2, "return result"))
    lines.append(L(1, "rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,"))
    lines.append(L(2, "fillStandard=True)"))
    lines.append(L(1, "return rtn"))
    lines.append("")
    return "\n".join(lines)


def genUpsertManyCode(table):
    name = table["name"]
    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 批量 upsert（幂等重扫走这里）")
    lines.append("def upsertMany_" + name
                 + "(tableName, dataSetList, conflictColumns = None, updateColumns = None):")
    lines.append(L(1, '"""按业务幂等键批量写 ' + name + "："))
    lines.append("")
    lines.append("    INSERT ... ON CONFLICT(<冲突键>) DO UPDATE SET ... / DO NOTHING")
    lines.append("")
    lines.append("    - conflictColumns 缺省用 CONFLICT_COLUMNS 里的本表默认值")
    lines.append("    - updateColumns 为 None 时更新「除冲突键以外」的所有列；传 () 则 DO NOTHING")
    lines.append("    - 冲突时**不覆盖** regYMDHMS（注册时间）与 delFlag（不会悄悄复活软删行）")
    lines.append('    """')
    lines.append(L(1, "result = 0"))
    lines.append(L(1, "if tableName not in TABLE_COLUMNS or not dataSetList:"))
    lines.append(L(2, "return result"))
    lines.append(L(1, "conflict = list(conflictColumns) if conflictColumns else "
                        "list(CONFLICT_COLUMNS.get(tableName, ()))"))
    lines.append(L(1, "update = list(updateColumns) if updateColumns else ()"))
    lines.append(L(1, "rtn, _columnNames = insertManyTableGeneral(tableName, dataSetList,"))
    lines.append(L(2, "conflictColumns=conflict, updateColumns=update,"))
    lines.append(L(2, "fillStandard=True)"))
    lines.append(L(1, "return rtn"))
    lines.append("")
    return "\n".join(lines)


def genUpdateCode(table):
    name = table["name"]
    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 修改记录")
    lines.append("def update_" + name + "(tableName, recID, dataSet):")
    lines.append(L(1, '"""按 recID 改一条 ' + name + "，返回影响行数（0 = 无字段可改或没命中）。"))
    lines.append("")
    lines.append("    - recID 与 modifyYMDHMS 由本函数控制，dataSet 里传了也忽略")
    lines.append("    - TEXT 字段传空串是真的「清空」（不会被当成未提供）")
    lines.append('    """')
    lines.append(L(1, "if tableName not in TABLE_COLUMNS:"))
    lines.append(L(2, "return 0"))
    lines.append(L(1, "saveSet, skipList = normalizeDataSet(tableName, dataSet)"))
    lines.append(L(1, 'saveSet.pop("recID", None)'))
    lines.append(L(1, "if skipList:"))
    lines.append(L(2, '_LOG.warning("update_' + name + ': 跳过无法归一的字段 %s" % skipList)'))
    lines.append(L(1, "if not saveSet:"))
    lines.append(L(2, "return 0"))
    lines.append(L(1, 'saveSet["modifyYMDHMS"] = misc.getTime()'))
    lines.append(L(1, 'return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)'))
    lines.append("")
    return "\n".join(lines)


def genDeleteCode(table):
    name = table["name"]
    keyName = table["conflictColumns"][0] if table["conflictColumns"] else "xxxCode"
    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 删除记录")
    lines.append("def delete_" + name + "(tableName, recID, hardDelete = False):")
    lines.append(L(1, '"""删一条 ' + name + "，返回影响行数。"))
    lines.append("")
    lines.append("    hardDelete=False（默认）-> 软删除：delFlag='1' + 刷 modifyYMDHMS")
    lines.append("    hardDelete=True           -> 物理 DELETE")
    lines.append("")
    lines.append("    ⚠️ 软删除后记录仍在表里，业务幂等键（" + keyName + "）依然被唯一索引占着；")
    lines.append("       想复用同一条记录请走 update，别指望再 insert 一遍。")
    lines.append('    """')
    lines.append(L(1, "if tableName not in TABLE_COLUMNS:"))
    lines.append(L(2, "return 0"))
    lines.append(L(1, "if hardDelete:"))
    lines.append(L(2, 'return deleteTableGeneral(tableName, "recID = %s", [recID])'))
    lines.append(L(1, 'saveSet = {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()}'))
    lines.append(L(1, 'return updateTableGeneral(tableName, "recID = %s", [recID], saveSet)'))
    lines.append("")
    return "\n".join(lines)


def genDropCode(table):
    name = table["name"]
    lines = []
    lines.append("")
    lines.append("")
    lines.append("# " + name + " 删表")
    lines.append("def drop_" + name + "(tableName):")
    lines.append(L(1, '"""删除 ' + name + ' 表及其索引（不可逆；只给「改表要重建」用）。"""'))
    lines.append(L(1, "return dropTableGeneral(tableName)"))
    lines.append("")
    return "\n".join(lines)


def _wrapSignature(sigList, prefix, width=96):
    """把函数签名按行宽折行（prefix = 'def query_xxx(' 这段前缀，用于算首行宽度）。

    每段内部以「逗号 + 空格」分隔，段与段之间由 join 补逗号，故入段时要先去掉尾逗号。
    """
    parts = []
    current = ""
    for item in sigList:
        piece = item + ","
        if current and len(prefix) + len(current) + len(piece) + 1 > width:
            parts.append(current.rstrip().rstrip(","))
            current = L(1, piece)
        else:
            current = (current + " " + piece) if current else piece
    if current:
        parts.append(current.rstrip().rstrip(","))
    return (",\n" + TS).join(parts)


# ============================================================
# 五、通用段函数体（内置模板）
# ============================================================

COMMON_FUNCTIONS = '''
# ------------------------------------------------------------
# 2. 句柄（懒初始化：import 本模块绝不连库）
# ------------------------------------------------------------

def dbHandle(dbFile = None):
    """取（并懒初始化）全局读写句柄。

    - 第一次调用才建连，dbFile 缺省取 paths.db_file()（= <PHOTO_ROOT>/db/photolib.db）
    - **无参调用绝不切换目标库**：已有句柄就直接返回它。
      原因：本模块所有 query_/insert_/update_ 内部都是 dbHandle() 无参调用，
      若无参也按 paths.db_file() 重新解析，那么「先用 dbHandle(临时库) 指定库、
      再调一个生成函数」的组合会被悄悄切回正式库 —— 表现为
      `build_db.py --db 临时库 --verify` 校验的是正式库、
      `scan_cli.py --db 临时库` 把任务写进正式库而把照片写进临时库。
      这种「同一进程同时存在两个库句柄」是最难查的一类错，所以从根上堵死：
      要换库必须**显式**调 dbHandle(路径)。
    - 显式传入与当前不同的 dbFile 时，关掉旧句柄重开（tools/build_db.py --db 用）
    - 线程安全：并发首调只会成功建一次
    """
    global _HANDLE
    with _HANDLE_LOCK:
        if _HANDLE is not None and not dbFile:
            return _HANDLE
        target = paths.db_file() if not dbFile else os.path.abspath(os.path.normpath(str(dbFile)))
        if _HANDLE is not None and _HANDLE.dbFile == target:
            return _HANDLE
        if _HANDLE is not None:
            _HANDLE.close()
        _HANDLE = sqliteHandle.sqliteHandle(target)
        return _HANDLE


def closeDb():
    """关掉全局句柄（进程退出/切库时用）"""
    global _HANDLE
    with _HANDLE_LOCK:
        if _HANDLE is not None:
            _HANDLE.close()
            _HANDLE = None
    return True


# ------------------------------------------------------------
# 3. 存在性判断
# ------------------------------------------------------------

def chkTableExist(tableName):
    """表是否存在。

    SQLite **没有 information_schema**（stock 的 MySQL 版查的那张表这里不存在），
    一律查 sqlite_master。
    """
    result = False
    if tableName not in TABLE_COLUMNS:
        return result
    db = dbHandle()
    sqlStr = "SELECT name FROM sqlite_master WHERE type = \\'table\\' AND name = %s;"
    if db.executeRead(sqlStr, (tableName,)) == sqliteHandle.RET_ERROR:
        return result
    result = db.fetchOne() is not None
    return result


def chkIndexExist(indexName):
    """索引是否存在（含 SQLite 自动建的 sqlite_autoindex_* 隐式唯一索引）"""
    result = False
    db = dbHandle()
    sqlStr = "SELECT name FROM sqlite_master WHERE type = \\'index\\' AND name = %s;"
    if db.executeRead(sqlStr, (indexName,)) == sqliteHandle.RET_ERROR:
        return result
    result = db.fetchOne() is not None
    return result


def dropTableGeneral(tableName):
    """删表（连同其索引）。返回 True = 已删掉。"""
    if tableName not in TABLE_COLUMNS:
        return False
    db = dbHandle()
    sqlStr = "DROP TABLE IF EXISTS " + tableName + ";"
    if db.executeWrite(sqlStr) == sqliteHandle.RET_ERROR:
        return False
    return not chkTableExist(tableName)


# ------------------------------------------------------------
# 4. 数据归一（业务 dict -> 可直接绑定的 dict）
# ------------------------------------------------------------

def normalizeDataSet(tableName, dataSet):
    """按 TABLE_COLUMNS 把业务传入的 dict 归一成「可直接绑定参数」的 dict。

    返回
    ----
    (saveSet, skipList)
      saveSet  —— 只含本表已有字段、且值类型与 SQLite 声明类型一致的项
      skipList —— 被跳过的字段名（表外字段 / 类型对不上 / 空数值）

    归一规则
    --------
      INTEGER : int(v)；None / "" / 非数字 -> 跳过（让库走 DEFAULT 或 NULL）
      NUMERIC : float(v)；同上
      TEXT    : str(v)；None -> 跳过；**空串原样保留**（更新时要能清空字段）
      BLOB    : bytes/bytearray/memoryview 原样；其余一律跳过
                （绝不 str->bytes 猜编码，人脸向量错了是静默脏数据）
      不在 dataSet 里的字段 -> 不出现在 saveSet（INSERT 时即 DEFAULT/NULL）
    """
    saveSet = {}
    skipList = []
    if tableName not in TABLE_COLUMNS or not dataSet:
        return saveSet, skipList
    for column in TABLE_COLUMNS[tableName]:
        name = column["name"]
        if name not in dataSet:
            continue
        value = dataSet.get(name)
        sqliteType = column["sqliteType"]
        if sqliteType.startswith("INTEGER"):
            if value is None or value == "":
                skipList.append(name)
                continue
            try:
                saveSet[name] = int(value)
            except (TypeError, ValueError):
                skipList.append(name)
            continue
        if sqliteType.startswith("NUMERIC"):
            if value is None or value == "":
                skipList.append(name)
                continue
            try:
                saveSet[name] = float(value)
            except (TypeError, ValueError):
                skipList.append(name)
            continue
        if sqliteType.startswith("BLOB"):
            if isinstance(value, (bytes, bytearray, memoryview)):
                saveSet[name] = bytes(value)
            else:
                skipList.append(name)
            continue
        if value is None:
            skipList.append(name)
            continue
        saveSet[name] = value if isinstance(value, str) else str(value)
    return saveSet, skipList


# ------------------------------------------------------------
# 5. 通用增删改
# ------------------------------------------------------------

def insertTableGeneral(tableName, dataSet):
    """通用插入：INSERT INTO t (cols) VALUES (%s,...)；成功返回新行 recID。

    列名全部来自 normalizeDataSet 归一后的 key（= 本表白名单），
    值一律走占位符，**任何情况下都不拼字符串**。
    """
    result = 0
    if tableName not in TABLE_COLUMNS or not dataSet:
        return result
    columnList = list(dataSet.keys())
    sqlStr = ("INSERT INTO " + tableName + " (" + ", ".join(columnList) + ") VALUES ("
              + ", ".join(["%s"] * len(columnList)) + ");")
    db = dbHandle()
    rtn = db.executeWrite(sqlStr, tuple(dataSet[name] for name in columnList))
    if rtn == sqliteHandle.RET_ERROR:
        return 0
    return db.insertID()


def insertManyTableGeneral(tableName, dataSetList, conflictColumns = (), updateColumns = (),
                           fillStandard = False, forceColumns = ()):
    """批量写（executemany，整批一次事务）。

    参数
    ----
    conflictColumns : 业务幂等键列名序列
        非空 -> INSERT ... ON CONFLICT(<列>) DO UPDATE SET ... / DO NOTHING（幂等重扫）
        为空 -> 普通 INSERT
    updateColumns : 要在 DO UPDATE 里刷新的列
        为空且 conflictColumns 非空时，缺省为「除冲突键外、且本行出现的所有列」
    fillStandard : bool
        True 时逐行补 delFlag（未删）与 regYMDHMS（注册时间）
    forceColumns : 列名序列 —— 强制出现在 INSERT 列清单里（只认本表白名单）
        为什么需要它：normalizeDataSet 把 None /"" 整条丢掉，于是当
        **整批所有行在该列上都为空**时，该列不会进入 INSERT，
        ON CONFLICT DO UPDATE 自然也不会覆盖它 -> 旧值永久残留。
        典型场景（步骤 3 扫描器）：某张图的内容被换成一张无 EXIF 的图，
        shotYear 应从 2023 变成 NULL；不强制该列的话2023 会一直留着，
        而且**库里看不出任何异常**，是典型的静默脏数据。
        强制后该列恒在列清单里，缺该键的行按 NULL 写入（=「显式清空」）。
        纯 INSERT 场景不需要它（缺列即DEFAULT/NULL）。

    返回
    ----
    (影响行数, 实际列名元组)
    """
    if tableName not in TABLE_COLUMNS or not dataSetList:
        return 0, ()
    conflict = tuple(conflictColumns or ())
    # 一次归一、一次拼行：不要为了拿列名把 normalizeDataSet 跑两遍
    #（500 行一批的扫描入库，这段会被调用 5 万次级别）
    now = misc.getTime()
    saveSetList = []
    for dataSet in dataSetList:
        saveSet, _skip = normalizeDataSet(tableName, dataSet)
        saveSet.pop("recID", None)
        if fillStandard:
            saveSet.setdefault("delFlag", comGD.DEL_FLAG_NO)
            saveSet.setdefault("regYMDHMS", now)
        saveSetList.append(saveSet)
    columnNames = []
    for saveSet in saveSetList:
        for name in saveSet:
            if name not in columnNames:
                columnNames.append(name)
    # 强制列：让「整批皆空」的列也进入列清单（否则 upsert 无法把旧值清成 NULL）
    if forceColumns:
        allowed = set(column["name"] for column in TABLE_COLUMNS.get(tableName, ()))
        for name in forceColumns:
            if name in allowed and name != "recID" and name not in columnNames:
                columnNames.append(name)
    if not columnNames:
        return 0, ()
    rowsList = [tuple(saveSet.get(name) for name in columnNames) for saveSet in saveSetList]

    sqlStr = ("INSERT INTO " + tableName + " (" + ", ".join(columnNames) + ") VALUES ("
              + ", ".join(["%s"] * len(columnNames)) + ")")
    if conflict:
        sqlStr += " ON CONFLICT (" + ", ".join(conflict) + ") "
        if not updateColumns:
            updateColumns = [name for name in columnNames if name not in conflict]
        # 注册时间与软删标记不参与覆盖：否则重扫会改掉注册时间、把软删行悄悄复活
        updateColumns = [name for name in updateColumns
                         if name in columnNames and name not in conflict
                         and name not in ("recID", "regYMDHMS", "delFlag")]
        if updateColumns:
            sqlStr += "DO UPDATE SET " + ", ".join(
                [name + " = excluded." + name for name in updateColumns])
        else:
            sqlStr += "DO NOTHING"
    sqlStr += ";"
    db = dbHandle()
    rtn = 0
    with db.transaction():
        rtn = db.executeWriteMany(sqlStr, rowsList)
    return rtn, tuple(columnNames)


def updateTableGeneral(tableName, keySqlstr, keyValues, dataSet):
    """通用更新：UPDATE t SET c = %s, ... WHERE <keySqlstr>；返回影响行数。

    keySqlstr 只由本文件的生成代码拼（形如 "recID = %s"），不接受外部字符串。
    """
    if not dataSet:
        return 0
    setList = []
    valuesList = []
    for name, value in dataSet.items():
        setList.append(name + " = %s")
        valuesList.append(value)
    sqlStr = ("UPDATE " + tableName + " SET " + ", ".join(setList)
              + " WHERE " + keySqlstr + ";")
    db = dbHandle()
    rtn = db.executeWrite(sqlStr, tuple(valuesList) + tuple(keyValues or ()))
    if rtn == sqliteHandle.RET_ERROR:
        return 0
    return rtn


def deleteTableGeneral(tableName, keySqlstr, keyValues):
    """物理删除：DELETE FROM t WHERE <keySqlstr>；返回影响行数。"""
    sqlStr = "DELETE FROM " + tableName + " WHERE " + keySqlstr + ";"
    db = dbHandle()
    rtn = db.executeWrite(sqlStr, tuple(keyValues or ()))
    if rtn == sqliteHandle.RET_ERROR:
        return 0
    return rtn


def columnDefOf(tableName, columnName):
    """拼出单列的 DDL 片段（列名 + SQLite 类型 + NOT NULL + DEFAULT），供 ALTER TABLE ADD COLUMN 用。

    返回 None 表示列名不在本表白名单里（**不接受外部随便传的字符串**）。
    ⚠️ NOT NULL 且**没有 DEFAULT** 的列拼出来的片段，加到**非空表**上必然被
       SQLite 拒绝（ADD COLUMN 要求新列有默认值或可空）；这类列只可能出现在
       新建表里，老表上永远不会是「缺列」，所以真遇到时调用方应当跳过并报告。
    """
    for column in TABLE_COLUMNS.get(tableName, ()):
        if column["name"] != columnName:
            continue
        parts = [column["name"] + " " + column["sqliteType"]]
        if column["autoIncrement"]:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        if column["notNull"]:
            parts.append("NOT NULL")
        if column["default"] is not None:
            parts.append("DEFAULT " + str(column["default"]))
        return " ".join(parts)
    return None


def addColumnGeneral(tableName, columnName):
    """ALTER TABLE <表> ADD COLUMN <列> —— schema 演进用（**加列，不改类型、不删列**）。

    为什么需要它
    ------------
    建表 DDL 是 `CREATE TABLE IF NOT EXISTS`：老库里已存在的表**不会**因为
    pb_*.txt 加了字段而自动变出那一列，于是「代码已按新字段写库、老库没这列」
    -> 运行时 `no such column` 直接炸。本函数让 tools/build_db.py --migrate
    能把老库补齐，且**不动任何一行数据**（SQLite 的 ADD COLUMN 是纯元数据操作）。

    返回
    ----
    (ok: bool, errMsg: str)
        ok=False 时 errMsg 说明原因（列名不在白名单 / 约束冲突 / SQL 被拒）。
        幂等：列已存在时直接返回 (True, "已存在")，不报错。
    """
    if tableName not in TABLE_COLUMNS:
        return False, "非法表名 %r" % tableName
    columnDef = columnDefOf(tableName, columnName)
    if not columnDef:
        return False, "列 %s.%s 不在表定义里" % (tableName, columnName)
    db = dbHandle()
    db.executeRead("SELECT name FROM sqlite_master WHERE type = 'table' AND name = %s;",
                   (tableName,))
    if db.fetchOne() is None:
        return False, "表 %s 不存在（先建表）" % tableName
    db.executeRead("PRAGMA table_info(%s);" % tableName)
    for row in db.fetchAll():
        if row.get("name") == columnName:
            return True, "已存在"
    sqlStr = "ALTER TABLE " + tableName + " ADD COLUMN " + columnDef + ";"
    if db.executeWrite(sqlStr) == sqliteHandle.RET_ERROR:
        return False, db.lastErrMsg
    return True, "已新增 %s" % columnName


def countTableGeneral(tableName, delFlag = comGD.DEL_FLAG_NO):
    """表内记录数（默认只数未软删的；delFlag 传 ""/"*" 数全部）。出错返回 -1。"""
    if tableName not in TABLE_COLUMNS:
        return -1
    sqlStr = "SELECT COUNT(*) AS rowNum FROM " + tableName
    valuesList = []
    if delFlag not in (None, "", "*"):
        sqlStr += " WHERE delFlag = %s"
        valuesList.append(delFlag)
    db = dbHandle()
    if db.executeRead(sqlStr, tuple(valuesList)) == sqliteHandle.RET_ERROR:
        return -1
    return int(db.fetchValue(0) or 0)


# ------------------------------------------------------------
# 6. 建库 / 自检
# ------------------------------------------------------------

def createAllTables():
    """按 TABLE_ORDER 逐表建表（幂等）。返回 {表名: 是否就绪}"""
    result = {}
    for tableName in TABLE_ORDER:
        func = globals().get("create_" + tableName)
        if func is None:
            _LOG.error("createAllTables: 缺少 create_%s" % tableName)
            result[tableName] = False
            continue
        result[tableName] = bool(func(tableName))
    return result


def dropAllTables():
    """删掉全部 pb_* 表（不可逆；给「改表要重建」用）。"""
    result = {}
    for tableName in reversed(TABLE_ORDER):
        result[tableName] = dropTableGeneral(tableName)
    return result


def tableInfo(tableName):
    """PRAGMA table_info -> list[dict]（字段名/类型/notnull/dflt/pk，建库自检用）"""
    if tableName not in TABLE_COLUMNS:
        return []
    db = dbHandle()
    if db.executeRead("PRAGMA table_info(%s);" % tableName) == sqliteHandle.RET_ERROR:
        return []
    return db.fetchAll()


def indexList(tableName):
    """PRAGMA index_list -> list[dict]（name/unique/origin/partial）"""
    if tableName not in TABLE_COLUMNS:
        return []
    db = dbHandle()
    if db.executeRead("PRAGMA index_list(%s);" % tableName) == sqliteHandle.RET_ERROR:
        return []
    return db.fetchAll()


def indexSqlList(tableName):
    """取某表**生成器登记的**索引 DDL 列表（用于核对建表结果）"""
    creator = globals().get("indexSqlList_" + tableName)
    if creator is None:
        return []
    return creator()


def dbFilePath():
    """当前句柄指向的库文件绝对路径"""
    return dbHandle().dbFile


def pragmaSnapshot():
    """读写两个连接上 PRAGMA 的实际生效值（验收用）"""
    return dbHandle().pragmaDict()


def integrityCheck():
    """PRAGMA integrity_check -> 'ok' 或错误描述"""
    db = dbHandle()
    if db.executeRead("PRAGMA integrity_check;") == sqliteHandle.RET_ERROR:
        return "ERR"
    return db.fetchValue("ERR")
'''


# ============================================================
# 六、组装产物
# ============================================================

def readCommonSection(fileName):
    """从已有产物里原样取出 #common begin/end 区段（保留通用 CRUD 的意义所在）。

    找不到 / 只有一个标记时返回 None，由调用方回退到内置模板。
    """
    if not fileName or not os.path.isfile(fileName):
        return None
    try:
        with open(fileName, "r", encoding="utf-8") as hFile:
            content = hFile.read()
    except OSError:
        return None
    beginMark = "#common begin"
    endMark = "#common end"
    beginPos = content.find(beginMark)
    endPos = content.find(endMark)
    if beginPos < 0 or endPos < 0 or endPos < beginPos:
        return None
    return content[beginPos:endPos + len(endMark)]


def genFileContent(tables, inputNames, commonBody=None):
    """把整份 sqliteCommon.py 的文本拼出来"""
    fieldNum = sum(len(t["fields"]) for t in tables)
    indexNum = sum(len(t["indexes"]) for t in tables)

    parts = []
    parts.append(HEADER_TEMPLATE.format(
        genTime=time.strftime("%Y-%m-%d %H:%M:%S"),
        ver=_VERSION,
        inputs=", ".join(os.path.basename(n) for n in inputNames),
        tableNum=len(tables),
        fieldNum=fieldNum,
        indexNum=indexNum,
    ))
    parts.append(genCommonBegin(tables, commonBody=commonBody))

    for table in tables:
        parts.append(genCreateCode(table))
        parts.append(genQueryCode(table))
        parts.append(genInsertCode(table))
        parts.append(genInsertManyCode(table))
        parts.append(genUpsertManyCode(table))
        parts.append(genUpdateCode(table))
        parts.append(genDeleteCode(table))
        parts.append(genDropCode(table))

    parts.append(FOOTER_TEMPLATE)
    return "\n".join(parts)


FOOTER_TEMPLATE = '''

# ==========================================================================
# 全库操作
# ==========================================================================

def checkSqliteDataBase():
    """建库入口：按 TABLE_ORDER 建全表（幂等），返回 {表名: 是否就绪}。

    ⚠️ **本模块 import 时不会自动建库**，必须显式调用（tools/build_db.py 就调它）——
       「import 一个模块就在磁盘上落库文件」这种事不能有。
    """
    return createAllTables()


if __name__ == "__main__":
    print("sqliteCommon _VERSION:", _VERSION)
    print("表清单            :", len(TABLE_ORDER), "张", TABLE_ORDER)
    for tableName in TABLE_ORDER:
        print("   %-22s %-10s 字段 %2d  索引 %d"
              % (tableName, TABLE_CN.get(tableName, ""),
                 len(TABLE_COLUMNS[tableName]), len(TABLE_INDEXES[tableName])))
    print("库文件(未连接)   :", paths.db_file())
'''


# ============================================================
# 七、自检
# ============================================================

def selfCheck(tables):
    """生成器自检：类型映射 / 表定义规范 / 索引清单。返回错误列表（空 = 通过）。"""
    errList = []

    for declString, expect in TYPE_MAPPING_SELFCHECK:
        try:
            got = decodeSqliteType(declString)
        except ValueError as e:
            errList.append("类型映射 %r 抛错: %s" % (declString, e))
            continue
        if got != expect:
            errList.append("类型映射 %r 应为 %r，实际 %r" % (declString, expect, got))

    # 自增主键规则必须排在整数规则之前（顺序敏感的根因）
    rule0 = " ".join(SQLITE_TYPE_RULES[0][0])
    if "AUTO_INCREMENT" not in rule0:
        errList.append("SQLITE_TYPE_RULES 第 1 条必须是自增主键规则，当前是 %r" % rule0)
    if decodeSqliteType("INT AUTO_INCREMENT PRIMARY KEY") != "INTEGER PRIMARY KEY AUTOINCREMENT":
        errList.append("recID 声明无法映射成 INTEGER PRIMARY KEY AUTOINCREMENT")

    for table in tables:
        name = table["name"]
        if table["primaryKey"] != "recID":
            errList.append("%s: 主键应为 recID，实际 %s" % (name, table["primaryKey"]))
        if len(table["fields"]) < len(STANDARD_TAIL_FIELDS) + 2:
            errList.append("%s: 字段太少（%d 个），像是漏读了 .txt"
                           % (name, len(table["fields"])))
        expect = EXPECTED_INDEX_NAMES.get(name)
        got = tuple(sorted(i["name"] for i in table["indexes"]))
        if expect is not None and got != tuple(sorted(expect)):
            errList.append("%s: 索引清单与 §五 不一致，实际 %s 期望 %s" % (name, got, sorted(expect)))
        for index in table["indexes"]:
            if not index["name"].startswith("idx_%s_" % name):
                errList.append("%s: 索引名 %s 不符合 idx_<表>_<字段> 规范" % (name, index["name"]))
    return errList


def verifyContent(content, tables):
    """产物内容自检：抓「生成器自己把内容生成漏了」这类错误。

    真实踩过的坑：建表 DDL 只输出了类型片段、**漏掉列名**，
    而相邻字符串字面量隐式拼接让代码照样能 compile() 通过 ——
    所以「能编译」不等于「生成对了」，必须逐项核对。
    """
    errList = []
    for table in tables:
        name = table["name"]
        for field in table["fields"]:
            fragment = '"' + field["name"] + " " + field["dcl"]
            if fragment not in content:
                errList.append("%s: 建表 DDL 里找不到列定义片段 %s" % (name, fragment))
        for index in table["indexes"]:
            if index["name"] not in content:
                errList.append("%s: 产物里找不到索引 %s" % (name, index["name"]))
        # 索引条数必须与清单一致 —— 真实踩过的坑：列表元素漏了逗号，
        # 相邻字符串字面量被隐式拼接成「一条超长 SQL」，代码照样能编译，
        # 跑到建库才报 "You can only execute one statement at a time"
        # （按 'ON <表>(' 计数，只统计本表的索引；建表 DDL 里不含 'ON '）
        gotIndexNum = content.count(" ON " + name + "(")
        if gotIndexNum != len(table["indexes"]):
            errList.append("%s: 产物里的索引语句数 %d != 清单 %d（列表元素漏逗号？）"
                           % (name, gotIndexNum, len(table["indexes"])))
        for funcSuffix in ("create_", "createTableSQL_", "indexSqlList_", "query_",
                           "insert_", "insertMany_", "upsertMany_", "update_",
                           "delete_", "drop_"):
            if ("def " + funcSuffix + name + "(") not in content:
                errList.append("%s: 产物里缺少函数 %s%s" % (name, funcSuffix, name))
        if table["queryFilters"]:
            loopHead = "for fieldName, fieldValue in " + _filterPairList(table["queryFilters"]) + ":"
            if loopHead not in content:
                errList.append("%s: query 的过滤循环头部不对 -> 期望 %r（单字段时漏尾逗号？会 unpack 炸）"
                               % (name, loopHead))
    for funcName in ("chkTableExist", "insertTableGeneral", "updateTableGeneral",
                     "insertManyTableGeneral", "normalizeDataSet", "createAllTables"):
        if ("def " + funcName + "(") not in content:
            errList.append("产物里缺少通用函数 %s" % funcName)
    return errList


# ============================================================
# 八、命令行
# ============================================================

def listTableFiles(inputList=None):
    """取要处理的 .txt 清单（默认本目录全部 pb_*.txt，按 TABLE_ORDER 排序）"""
    if inputList:
        fileList = []
        for item in inputList:
            fileName = item if os.path.isabs(item) else os.path.join(_HERE_DIR, item)
            if not os.path.isfile(fileName):
                fileName = os.path.join(os.getcwd(), item)
            if not os.path.isfile(fileName):
                raise GenError("找不到表定义文件: %s" % item)
            fileList.append(os.path.abspath(fileName))
        return fileList
    fileList = [os.path.join(_HERE_DIR, n) for n in sorted(os.listdir(_HERE_DIR))
                if n.startswith("pb_") and n.endswith(".txt")]
    fileList.sort(key=lambda p: TABLE_ORDER.index(os.path.splitext(os.path.basename(p))[0])
                  if os.path.splitext(os.path.basename(p))[0] in TABLE_ORDER else 99)
    return fileList


def _fixConsole():
    """Windows 控制台默认编码是 GBK，打印中文/符号会 UnicodeEncodeError。

    统一把 stdout/stderr 切到 UTF-8（并对无法编码的字符退化为 ?），
    否则生成成功了却在最后一行 print 上崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv):
    _fixConsole()
    inputList = []
    output = DEFAULT_OUTPUT
    doSelfCheck = False
    resetCommon = False
    showList = False

    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in ("-i", "--input"):
            index += 1
            while index < len(argv) and not argv[index].startswith("-"):
                inputList.append(argv[index])
                index += 1
        elif arg in ("-o", "--output"):
            index += 1
            output = argv[index]
            index += 1
        elif arg == "--selftest":
            doSelfCheck = True
            index += 1
        elif arg == "--reset-common":
            resetCommon = True
            index += 1
        elif arg == "--list":
            showList = True
            index += 1
        else:
            print("[Unknown] %s" % arg, file=sys.stderr)
            print(__doc__)
            return 2

    try:
        fileList = listTableFiles(inputList)
        tables = [parseTableFile(f) for f in fileList]
    except GenError as e:
        print("[GenError] %s" % e, file=sys.stderr)
        return 1

    if not tables:
        print("[GenError] 没有可处理的 pb_*.txt", file=sys.stderr)
        return 1

    errList = selfCheck(tables)
    if errList:
        print("[SelfCheck FAILED]", file=sys.stderr)
        for err in errList:
            print("   -", err, file=sys.stderr)
        return 1
    print("[SelfCheck] 类型映射 %d 条、表定义规范、索引清单（§五）全部通过"
          % len(TYPE_MAPPING_SELFCHECK))

    if showList:
        for table in tables:
            print("  %-22s %-10s 字段 %2d  索引 %d  过滤字段 %d"
                  % (table["name"], table["cnName"], len(table["fields"]),
                     len(table["indexes"]), len(table["queryFilters"])))
        for table in tables:
            print("  -- %s" % table["name"])
            for field in table["fields"]:
                print("       %-18s %-16s -> %-32s %s"
                      % (field["name"], field["typeString"], field["sqliteType"], field["dcl"]))
        return 0

    if doSelfCheck:
        return 0

    # 保留已有产物的 #common 区段（除非 --reset-common）
    commonBody = None
    if not resetCommon:
        commonBody = readCommonSection(output)
        if commonBody is not None:
            print("[Common] 沿用已有产物的 #common 区段（%d 字符）" % len(commonBody))
        else:
            print("[Common] 未找到可沿用的 #common 区段，用内置模板")

    content = genFileContent(tables, fileList, commonBody=commonBody)

    contentErrList = verifyContent(content, tables)
    if contentErrList:
        print("[ContentCheck FAILED] —— 产物未落盘", file=sys.stderr)
        for err in contentErrList:
            print("   -", err, file=sys.stderr)
        return 1
    print("[ContentCheck] %d 张表的 DDL 列定义 / 索引 / 8 个 CRUD 函数 + 6 个通用函数全部就位"
          % len(tables))

    try:
        compile(content, output, "exec")
    except SyntaxError as e:
        print("[GenError] 生成结果语法错误（第 %d 行: %s）—— 产物未落盘" % (e.lineno or 0, e.msg),
              file=sys.stderr)
        return 1

    outDir = os.path.dirname(os.path.abspath(output))
    if outDir and not os.path.isdir(outDir):
        os.makedirs(outDir, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as hFile:
        hFile.write(content)

    print("[Done] 输出: %s（%d 行）" % (output, content.count("\n") + 1))
    print("       表 %d 张 / 字段 %d 个 / 索引 %d 个"
          % (len(tables), sum(len(t["fields"]) for t in tables),
             sum(len(t["indexes"]) for t in tables)))
    for table in tables:
        print("       %-22s %-10s 字段 %2d  索引 %d"
              % (table["name"], table["cnName"], len(table["fields"]), len(table["indexes"])))
    print("⚠️ 生成物，禁止手工编辑；改表请改 pb_*.txt 后重跑本生成器")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
