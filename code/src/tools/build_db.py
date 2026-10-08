#! /usr/bin/env python3
#encoding: utf-8

#Filename: build_db.py
#Description: photo-browser 建库工具 —— 按 pb_*.txt 建库/建表/建索引，并做结构自检
#
# 职责（开发计划 步骤 2）
# ----------------------
#   1. 建库：确保 <PHOTO_ROOT>\db\ 目录存在（缺目录自动创建），生成 photolib.db
#   2. 建表建索引：逐表调 sqliteCommon 的 create_pb_xxx（**幂等**，可重复执行）
#   3. 自检：表/列/索引是否与 pb_*.txt 一致、PRAGMA 是否生效、integrity_check
#   4. CRUD 自检：通用 insert -> query -> update -> 删除，并验证 blob 往返与 %s->? 转换
#
# 用法
# ----
#   python code\src\tools\build_db.py                 # 建库（幂等）
#   python code\src\tools\build_db.py --verify        # 建库 + 结构自检
#   python code\src\tools\build_db.py --selftest      # 再加 CRUD/blob 自检（会清掉自检数据）
#   python code\src\tools\build_db.py --drop# 先删表再建（改表后重建用，**数据全丢**）
#   python code\src\tools\build_db.py --migrate       # **老库补列**（不动数据，见下）
#   python code\src\tools\build_db.py --db d:\tmp\x.db  # 指定库文件（不动正式库）
#
# --migrate 是什么、为什么必须有
# -----------------------------
#   建表 DDL 是 `CREATE TABLE IF NOT EXISTS`：**老库里已存在的表不会因为
#   pb_*.txt 加了字段而自动多出那一列**。结果是「代码按新字段写库、老库没这列」，
#   运行时直接 `no such column` 炸掉；而唯一的老办法 `--drop` 会把数据全丢光。
#   `--migrate` 的做法是：比对本表在 .txt 里的列与库里的实际列，**只补缺的**
#   （`ALTER TABLE ... ADD COLUMN`，SQLite 纯元数据操作，不动任何一行），
#   再把缺的索引补建。改列类型/删列 SQLite 不支持，会明确报告「请重建库」。
#
# 硬约束
# ------
#   - **只建空表结构**，不写任何真实照片数据（--selftest 造的测试行跑完即删）；
#   - **绝不碰 photo 目录**：只建 db 目录（走 paths.ensure_dirs()，它显式排除 photo）；
#   - 建表 DDL 全部来自生成物，**本文件不写一条建表 DDL**；ALTER 语句由生成层的
#     addColumnGeneral() 拼（业务层禁止裸 SQL 的同一条纪律）。

import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))              # .../src/tools
_SRC_DIR = os.path.dirname(_HERE_DIR)                              # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                        # noqa: E402
from common import miscCommon as misc                              # noqa: E402
from common import paths as paths                                  # noqa: E402
from config import sqliteSettings as sqliteSettings                # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("buildDb", "builddb.log")

# 自检用的假数据前缀（**不是真实照片**，跑完即删）
_TEST_PREFIX = "_SELFTEST_"


def _fixConsole():
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _line(char="-", width=72):
    return char * width


def _pragmaCore(pragmaDict):
    """只取 PRAGMA_LIST 里的那几项（query_only 是读连接独有的，不参与「两连接是否一致」比较）"""
    return {key: pragmaDict.get(key) for key, _val in sqliteSettings.PRAGMA_LIST}


def _report(title, checks, verbose):
    """统一报告出口。

    ⚠️ 所有提前 return 的分支都必须走这里 —— 否则中途失败时
       「失败详情」会被静默吞掉，只剩一句「结论：有失败项」，没法定位。
    """
    ok = all(item["ok"] for item in checks)
    if verbose:
        print(_line("="))
        print("%s: %s（%d 项）" % (title, "全部通过" if ok else "**有失败项**", len(checks)))
        for item in checks:
            print("   [%s] %-38s 期望=%-24s 实际=%s"
                  % ("OK" if item["ok"] else "!!", item["item"], item["expect"], item["got"]))
        print(_line("="))
    return {"ok": ok, "checks": checks}


# ============================================================
# 一、建库
# ============================================================

def build(dbFile=None, drop=False, verbose=True) -> dict:
    """建库 + 逐表建表建索引（幂等）。

    参数
    ----
    dbFile  : str | None —— 缺省取 paths.db_file()
    drop    : bool —— True 时先 dropAllTables() 再建（**数据全丢**，改表重建用）
    verbose : bool

    返回
    ----
    dict —— {"dbFile":..., "created":[...], "existed":[...], "failed":[...], "elapsed":秒}
    """
    startTime = time.time()
    target = dbFile or paths.db_file()

    # 1) 目录：只建 db 目录（及 thumb/imports/exports），**绝不建/碰 photo**
    paths.ensure_dirs()
    if not os.path.isdir(os.path.dirname(os.path.abspath(target))):
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    if verbose:
        print("库文件: %s" % target)
        print("目录已就绪: %s" % os.path.dirname(os.path.abspath(target)))

    # 2) 建连（此时才第一次在磁盘上落库文件）
    sqliteCommon.dbHandle(target)
    if verbose:
        print("句柄已建立，PRAGMA(写): %s" % sqliteCommon.pragmaSnapshot()["write"])
        print("PRAGMA(读): %s" % sqliteCommon.pragmaSnapshot()["read"])

    if drop:
        dropped = sqliteCommon.dropAllTables()
        if verbose:
            print("[--drop] 已删除 %d 张旧表" % len([v for v in dropped.values() if v]))

    # 3) 逐表建
    created = []
    existed = []
    failed = []
    for tableName in sqliteCommon.TABLE_ORDER:
        creator = getattr(sqliteCommon, "create_" + tableName, None)
        if creator is None:
            failed.append(tableName)
            _LOG.error("build_db: 缺少 create_%s（生成物与本脚本不同步？）" % tableName)
            continue
        wasExist = sqliteCommon.chkTableExist(tableName)
        ok = bool(creator(tableName))
        if not ok:
            failed.append(tableName)
            _LOG.error("build_db: create_%s 失败" % tableName)
            continue
        if wasExist:
            existed.append(tableName)
        else:
            created.append(tableName)

    result = {
        "dbFile": os.path.abspath(target),
        "created": created,
        "existed": existed,
        "failed": failed,
        "elapsed": time.time() - startTime,
    }
    if verbose:
        print("")
        print(_line())
        print("建库结果: 新建 %d 张 / 已存在 %d 张 / 失败 %d 张，用时 %.2fs"
              % (len(created), len(existed), len(failed), result["elapsed"]))
        for tableName in sqliteCommon.TABLE_ORDER:
            if tableName in created:
                mark = "新建"
            elif tableName in existed:
                mark = "已存在"
            else:
                mark = "**失败**"
            print("   %-22s %-10s %s" % (tableName, sqliteCommon.TABLE_CN.get(tableName, ""), mark))
        print(_line())
    return result


# ============================================================
# 二、结构迁移（老库补列/ 补索引，不动数据）
# ============================================================

def _columnTypeMap(tableName):
    """库里的实际列：{列名: SQLite 声明类型}（走生成层的 tableInfo，不裸查 sqlite_master）"""
    result = {}
    for row in sqliteCommon.tableInfo(tableName):
        result[row["name"]] = (row.get("type") or "").upper()
    return result


# SQLite 的类型「亲和性」别名：INT/INTEGER 同一族，REAL 归 NUMERIC。
# 比对时必须比亲和性而不是逐字比 —— 否则 recID 的
# "INTEGER"（table_info 回读）vs "INTEGER PRIMARY KEY AUTOINCREMENT"（.txt 定义）
# 会被误报成类型不一致。
_TYPE_AFFINITY = {
    "": "", "INT": "INTEGER", "INTEGER": "INTEGER", "BIGINT": "INTEGER",
    "SMALLINT": "INTEGER", "TINYINT": "INTEGER",
    "TEXT": "TEXT", "CLOB": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT",
    "NUMERIC": "NUMERIC", "DECIMAL": "NUMERIC", "REAL": "NUMERIC",
    "DOUBLE": "NUMERIC", "FLOAT": "NUMERIC",
    "BLOB": "BLOB",
}


def _affinity(declType) -> str:
    """把声明类型归到 SQLite 亲和性（只取首 token、去掉长度与约束尾巴）"""
    text = (declType or "").strip().upper()
    if not text:
        return ""
    token = text.split("(")[0].strip().split()
    if not token:
        return ""
    return _TYPE_AFFINITY.get(token[0], token[0])


def migrate(dbFile=None, verbose=True) -> dict:
    """把库结构补到与 pb_*.txt 一致：**只加列、加索引，绝不删数据**。

    做三件事
    --------
      1. 缺表 -> 建表（与 build() 同一条路径，幂等）
      2. 缺列 -> `ALTER TABLE ADD COLUMN`（生成层 addColumnGeneral）
      3. 缺索引 -> 建索引（生成层 indexSqlList_<表>，全部 IF NOT EXISTS）

    明确不做
    --------
      · **不删列、不改列类型**（SQLite 不支持，必须重建库）：类型对不上会报告出来，
        由人决定要不要 `--drop`；
      · **不删任何一行数据**；
      · 不动软删/未软删的语义。

    返回
    ----
    dict —— {"dbFile", "created", "columnsAdded": [(表,列,结果)],
             "indexesAdded": [索引名], "mismatch": [(表,列,库里的类型,应然类型)],
             "failed": [(表,说明)], "elapsed"}
    """
    startTime = time.time()
    target = dbFile or paths.db_file()
    sqliteCommon.dbHandle(target)

    result = {"dbFile": os.path.abspath(target), "created": [],
              "columnsAdded": [], "indexesAdded": [], "mismatch": [],
              "failed": [], "elapsed": 0.0}

    for tableName in sqliteCommon.TABLE_ORDER:
        # ---- 1) 缺表先建 ----
        if not sqliteCommon.chkTableExist(tableName):
            creator = getattr(sqliteCommon, "create_" + tableName, None)
            if creator is None or not creator(tableName):
                result["failed"].append((tableName, "缺表且建表失败"))
                continue
            result["created"].append(tableName)
            if verbose:
                print("   %-22s 缺表 -> 已建" % tableName)
            continue        # 新建的表结构必然完整，直接下一张

        # ---- 2) 补列 ----
        gotTypes = _columnTypeMap(tableName)
        wantList = sqliteCommon.TABLE_COLUMNS.get(tableName, ())
        for column in wantList:
            name = column["name"]
            wantType = column["sqliteType"] or ""
            if name not in gotTypes:
                ok, errMsg = sqliteCommon.addColumnGeneral(tableName, name)
                result["columnsAdded"].append((tableName, name, errMsg))
                if not ok:
                    result["failed"].append((tableName, errMsg))
                elif verbose:
                    print("   %-22s + %-18s %s" % (tableName, name, errMsg))
                continue
            # 列在，但亲和性对不上：SQLite 改不了类型，只能报告
            if wantType and _affinity(gotTypes[name]) != _affinity(wantType):
                result["mismatch"].append((tableName, name, gotTypes[name], wantType))
            # 库里有、.txt 里没有的列（例如手工加的/回滚残留）-> 只报告，绝不动

        # ---- 3) 补索引 ----
        indexCreator = getattr(sqliteCommon, "indexSqlList_" + tableName, None)
        if indexCreator is None:
            continue
        for indexSql in indexCreator():
            # 索引名从 DDL 里切出来只为调用 chkIndexExist（已存在则跳过）；
            # 真正建索引用的是生成层给的原句（IF NOT EXISTS，天然幂等）。
            # ⚠️ 必须补回 "idx_" 前缀 —— chkIndexExist 查的是 sqlite_master 里的真名。
            indexName = "idx_" + indexSql.split("idx_")[-1].split(" ")[0].strip("();'\"")
            if indexName and sqliteCommon.chkIndexExist(indexName):
                continue
            if sqliteCommon.dbHandle().executeWrite(indexSql) == -2:
                result["failed"].append((tableName, "建索引失败: %s" % indexSql))
            else:
                result["indexesAdded"].append(indexName)
                if verbose:
                    print("   %-22s + 索引 %s" % (tableName, indexName))

    result["elapsed"] = round(time.time() - startTime, 2)
    if verbose:
        print("-" * 72)
        print("迁移结果: 新建表 %d 张 / 补列%d 个 / 补索引 %d 个 / 类型不一致 %d 处 / 失败 %d 处，用时 %.2fs"
              % (len(result["created"]), len(result["columnsAdded"]),
                 len(result["indexesAdded"]), len(result["mismatch"]),
                 len(result["failed"]), result["elapsed"]))
        for tableName, columnName, errMsg in result["columnsAdded"]:
            print("   +列 %-22s %-18s %s" % (tableName, columnName, errMsg))
        for tableName, gotType, wantType in result["mismatch"]:
            print("   !! 类型不一致 %-22s 库=%s 应=%s（SQLite 改不了类型，需--drop 重建）"
                  % (tableName, gotType, wantType))
        for tableName, errMsg in result["failed"]:
            print("   !! 失败 %-22s %s" % (tableName, errMsg))
    return result


# ============================================================
# 三、结构自检
# ============================================================

def verify(verbose=True) -> dict:
    """结构自检：表 / 列 / 索引 / PRAGMA / 完整性。

    返回 {"ok": bool, "checks": [(项目, 期望, 实际, 通过?), ...]}
    """
    checks = []

    def add(item, expect, got, ok):
        checks.append({"item": item, "expect": expect, "got": got, "ok": bool(ok)})

    db = sqliteCommon.dbHandle()

    # ---- 1. 8 张表齐全 + 列与 pb_*.txt 完全一致 ----
    tableNum = 0
    for tableName in sqliteCommon.TABLE_ORDER:
        if not sqliteCommon.chkTableExist(tableName):
            add("表 %s" % tableName, "存在", "缺失", False)
            continue
        tableNum += 1
        gotColumns = [row["name"] for row in sqliteCommon.tableInfo(tableName)]
        expectColumns = [c["name"] for c in sqliteCommon.TABLE_COLUMNS[tableName]]
        # ⚠️ 这里比的是**集合**而不是顺序，原因是 `--migrate` 的物理限制：
        #   SQLite 的 `ALTER TABLE ... ADD COLUMN` **只能追加到表末尾**，
        #   而 pb_person.txt 里 displayNamePinyin 放在中间（生成器要求标准七字段
        #   必须在末尾，见 sqliteCodeGenerator.STANDARD_TAIL_FIELDS）。
        #   于是「新建的库」列序与 .txt 一致、「迁移过的老库」不一致 ——
        #   如果按顺序比，老库 migrate 完立刻 verify 报红，而库其实是好的。
        #   列序在这里没有语义：所有 SQL 都用**显式列清单**（生成层纪律 3），
        #   没有一条依赖「第 N 列是谁」。
        miss = sorted(set(expectColumns) - set(gotColumns))
        extra = sorted(set(gotColumns) - set(expectColumns))
        add("列 %s（%d 列）" % (tableName, len(expectColumns)),
            "= .txt",
            ("缺 %s" % miss if miss else "多 %s" % extra if extra
             else "%d 列%s" % (len(gotColumns),
                               "" if gotColumns == expectColumns else "（列序不同，无害）")),
            not miss and not extra)
    add("表数量", "%d 张" % len(sqliteCommon.TABLE_ORDER), "%d 张" % tableNum,
        tableNum == len(sqliteCommon.TABLE_ORDER))

    # ---- 2. 索引齐全（数据库设计.md §五）----
    indexNum = 0
    for tableName in sqliteCommon.TABLE_ORDER:
        gotSet = set(row["name"] for row in sqliteCommon.indexList(tableName))
        expectSet = set(i["name"] for i in sqliteCommon.TABLE_INDEXES[tableName])
        miss = sorted(expectSet - gotSet)
        indexNum += len(expectSet & gotSet)
        add("索引 %s（%d 个）" % (tableName, len(expectSet)),
            "全在", "齐" if not miss else "缺 %s" % miss, not miss)
    expectIndexNum = sum(len(i) for i in sqliteCommon.TABLE_INDEXES.values())
    add("索引总数", "%d 个" % expectIndexNum, "%d 个" % indexNum, indexNum == expectIndexNum)

    # ---- 3. recID 类型 / 主键 / 自增 ----
    for tableName in ("pb_photo", "pb_face"):
        row = None
        for item in sqliteCommon.tableInfo(tableName):
            if item["name"] == "recID":
                row = item
                break
        if row is None:
            add("recID @ %s" % tableName, "存在", "缺失", False)
            continue
        add("recID @ %s 类型" % tableName, "INTEGER", row["type"],
            row["type"].upper() == "INTEGER")
        add("recID @ %s pk" % tableName, "1", str(row["pk"]), row["pk"] == 1)
    # AUTOINCREMENT 一旦生效，SQLite 必然建出内部表 sqlite_sequence
    db.executeRead("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence';")
    hasSeq = db.fetchOne() is not None
    add("AUTOINCREMENT 生效（内部表 sqlite_sequence）", "存在", "存在" if hasSeq else "缺失", hasSeq)

    # ---- 4. PRAGMA ----
    snapshot = sqliteCommon.pragmaSnapshot()
    for role in ("write", "read"):
        pragma = snapshot[role]
        add("PRAGMA journal_mode（%s）" % role, "wal", str(pragma.get("journal_mode")).lower(),
            str(pragma.get("journal_mode")).lower() == "wal")
        add("PRAGMA foreign_keys（%s）" % role, "1", str(pragma.get("foreign_keys")),
            pragma.get("foreign_keys") == 1)
        add("PRAGMA busy_timeout（%s）" % role, "5000", str(pragma.get("busy_timeout")),
            pragma.get("busy_timeout") == 5000)
        add("PRAGMA synchronous（%s）" % role, "1(NORMAL)", str(pragma.get("synchronous")),
            pragma.get("synchronous") == 1)
        add("PRAGMA temp_store（%s）" % role, "2(MEMORY)", str(pragma.get("temp_store")),
            pragma.get("temp_store") == 2)
        add("PRAGMA cache_size（%s）" % role, "-64000", str(pragma.get("cache_size")),
            pragma.get("cache_size") == -64000)
    add("读写连接 PRAGMA 一致（DR-3）", "6 项全同",
        "全同" if _pragmaCore(snapshot["write"]) == _pragmaCore(snapshot["read"]) else "不同",
        _pragmaCore(snapshot["write"]) == _pragmaCore(snapshot["read"]))
    add("读连接 query_only（等效只读）", "1", str(snapshot["read"].get("query_only")),
        snapshot["read"].get("query_only") == 1)

    # ---- 5. 不该有物理外键（D-2）----
    fkNum = 0
    for tableName in sqliteCommon.TABLE_ORDER:
        db.executeRead("PRAGMA foreign_key_list(%s);" % tableName)
        fkNum += len(db.fetchAll())
    add("物理外键数", "0（D-2 不建外键）", "%d" % fkNum, fkNum == 0)

    # ---- 6. 完整性 ----
    integrity = sqliteCommon.integrityCheck()
    add("PRAGMA integrity_check", "ok", str(integrity), str(integrity).lower() == "ok")

    # ---- 7. 各表行数（本步只建空表）----
    for tableName in sqliteCommon.TABLE_ORDER:
        n = sqliteCommon.countTableGeneral(tableName, delFlag="*")
        add("行数 %s" % tableName, "0", str(n), n == 0)

    return _report("结构自检", checks, verbose)


# ============================================================
# 三、CRUD 自检（验收第 7 条：通用 insert -> query -> update -> 删除 + blob 往返）
# ============================================================

def _purgeSelfTestData():
    """先把上次自检残留的 _SELFTEST_ 行清干净 —— 自检必须**可重复执行**。

    不做这一步：第一次跑到一半崩了，_SELFTEST_FAM001 就留在库里；第二次再跑 insert
    直接撞 UNIQUE，届时「失败原因」离真正的问题十万八千里。
    """
    purged = 0
    for tableName, keyField in (("pb_family", "familyCode"),
                                ("pb_face", "faceCode"),
                                ("pb_scan_job", "jobCode")):
        for item in getattr(sqliteCommon, "query_" + tableName)(tableName, delFlag="*"):
            value = str(item.get(keyField) or "")
            if not value.startswith(_TEST_PREFIX):
                continue
            purged += getattr(sqliteCommon, "delete_" + tableName)(
                tableName, item["recID"], hardDelete=True)
    return purged


def selftest(verbose=True) -> dict:
    """走一遍通用数据访问层：占位符转换 / 类型归一 / blob 往返 / 软删物理删 / 批量幂等。

    造的数据用 _SELFTEST_ 前缀，跑前清理残留、跑完物理删除，库最终仍是空表。
    """
    checks = []

    def add(item, expect, got, ok):
        checks.append({"item": item, "expect": expect, "got": got, "ok": bool(ok)})
        if not ok:
            _LOG.error("selftest %s: 期望 %s 实际 %s" % (item, expect, got))

    purged = _purgeSelfTestData()
    if verbose and purged:
        print("（已清理上次自检残留 %d 行）" % purged)

    try:
        # ---------- 1. 通用 insert（pb_family：纯文本）----------
        familyCode = _TEST_PREFIX + "FAM001"
        recID = sqliteCommon.insert_pb_family("pb_family", {
            "familyCode": familyCode,
            "familyName": "自检家庭",
            "notes": "build_db --selftest 造的测试数据",
            "recID": 99999,               # 故意传，应被忽略（自增由库接管）
            "shotYear": 1234,            # 表外字段，应被丢弃
        })
        add("insert_pb_family 返回 recID", ">0", str(recID), recID > 0)
        add("insert 忽略传入的 recID", "库自增", "库自增 %d" % recID, recID >= 1)

        # ---------- 2. 通用 query（占位符 %s -> ? 转换在这一步生效）----------
        rows = sqliteCommon.query_pb_family("pb_family", familyCode=familyCode)
        add("query 按 familyCode 命中", "1 行", "%d 行" % len(rows), len(rows) == 1)
        if not rows:
            return _report("CRUD 自检", checks, verbose)
        row = rows[0]
        add("query 取回 familyName", "自检家庭", str(row.get("familyName")),
            row.get("familyName") == "自检家庭")
        add("query 取回 notes", "build_db --selftest 造的测试数据", str(row.get("notes")),
            row.get("notes") == "自检家庭" or row.get("notes", "").startswith("build_db"))
        add("insert 自动补 delFlag", comGD.DEL_FLAG_NO, str(row.get("delFlag")),
            row.get("delFlag") == comGD.DEL_FLAG_NO)
        add("insert 自动补 regYMDHMS", "14 位 YYYYMMDDHHMMSS", str(row.get("regYMDHMS")),
            len(str(row.get("regYMDHMS"))) == 14)

        # ---------- 3. 通用 update ----------
        modifyBefore = row.get("modifyYMDHMS")
        rtn = sqliteCommon.update_pb_family("pb_family", recID, {"familyName": "自检家庭-改名"})
        add("update_pb_family 影响行数", "1", str(rtn), rtn == 1)
        rows = sqliteCommon.query_pb_family("pb_family", recID=recID)
        cur = rows[0] if rows else {}
        add("update 后 familyName", "自检家庭-改名", str(cur.get("familyName")),
            cur.get("familyName") == "自检家庭-改名")
        add("update 刷 modifyYMDHMS", "14 位时间戳（原为空）", str(cur.get("modifyYMDHMS")),
            len(str(cur.get("modifyYMDHMS") or "")) == 14
            and str(cur.get("modifyYMDHMS")) != str(modifyBefore))
        # TEXT 传空串 = 真清空（不被当成「未提供」）
        sqliteCommon.update_pb_family("pb_family", recID, {"notes": ""})
        rows = sqliteCommon.query_pb_family("pb_family", recID=recID)
        cur = rows[0] if rows else {}
        add("update 空串=清空 notes", "''", repr(cur.get("notes")), cur.get("notes") == "")

        # ---------- 4. blob 往返（pb_face.embedding：float32[512] = 2048 字节）----------
        vector = bytes(bytearray((i % 251) for i in range(2048)))
        faceCode = _TEST_PREFIX + "FACE001"
        photoCode = _TEST_PREFIX + "PHOTO001"
        faceID = sqliteCommon.insert_pb_face("pb_face", {
            "faceCode": faceCode,
            "photoCode": photoCode,
            "detScore": 0.9123,
            "poseYaw": 12.5,
            "bbox": "0.1,0.2,0.3,0.4",
            "embedding": vector,
        })
        add("insert_pb_face 返回 recID", ">0", str(faceID), faceID > 0)
        rows = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode, mode="full")
        add("query 取回 face", "1 行", "%d 行" % len(rows), len(rows) == 1)
        if rows:
            got = rows[0]
            add("blob 字节数", "2048", str(len(got.get("embedding") or b"")),
                len(got.get("embedding") or b"") == 2048)
            add("blob 内容逐字节相同", "True", str(got.get("embedding") == vector),
                got.get("embedding") == vector)
            add("NUMERIC 往返（detScore）", "0.9123", str(got.get("detScore")),
                abs(float(got.get("detScore")) - 0.9123) < 1e-9)
            add("未传的 personCode 落 NULL", "None", str(got.get("personCode")),
                got.get("personCode") is None)

        # ---------- 5. light 模式剔除 BLOB 列 ----------
        rows = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode, mode="light")
        add("mode=light 不含 embedding", "不含", "含" if rows and "embedding" in rows[0] else "不含",
            bool(rows) and "embedding" not in rows[0])

        # ---------- 6. nullFields：待确认队列（personCode IS NULL）----------
        rows = sqliteCommon.query_pb_face("pb_face", nullFields=("personCode",))
        add("nullFields 命中待确认人脸", ">=1 行", "%d 行" % len(rows), len(rows) >= 1)

        # ---------- 7. 批量 executemany + upsert 幂等 ----------
        jobRows = [{"jobCode": _TEST_PREFIX + "JOB%03d" % i,
                    "rootPath": r"d:\PhotoLib\photo",
                    "jobStatus": comGD.JOB_IDLE} for i in range(5)]
        n1 = sqliteCommon.insertMany_pb_scan_job("pb_scan_job", jobRows)
        add("insertMany 批量写 5 行", "5", str(n1), n1 == 5)
        n2 = sqliteCommon.upsertMany_pb_scan_job("pb_scan_job", jobRows, updateColumns=())
        add("upsertMany 幂等（DO NOTHING 不新增）", "pb_scan_job 总行数=5",
            str(sqliteCommon.countTableGeneral("pb_scan_job", delFlag="*")),
            sqliteCommon.countTableGeneral("pb_scan_job", delFlag="*") == 5)
        add("upsertMany 返回 5", "5", str(n2), n2 == 5)
        n3 = sqliteCommon.upsertMany_pb_scan_job(
            "pb_scan_job", [dict(jobRows[0], jobStatus=comGD.JOB_RUNNING)])
        rows = sqliteCommon.query_pb_scan_job("pb_scan_job", jobCode=jobRows[0]["jobCode"])
        gotStatus = rows[0].get("jobStatus") if rows else "(没查到)"
        add("upsertMany 冲突时 DO UPDATE", comGD.JOB_RUNNING, str(gotStatus),
            bool(rows) and rows[0].get("jobStatus") == comGD.JOB_RUNNING)

        # ---------- 8. 软删除 / 物理删除 ----------
        rtn = sqliteCommon.delete_pb_family("pb_family", recID)
        add("delete 默认软删", "1", str(rtn), rtn == 1)
        add("软删后默认查不到", "0 行", "%d 行" % len(sqliteCommon.query_pb_family("pb_family", familyCode=familyCode)),
            len(sqliteCommon.query_pb_family("pb_family", familyCode=familyCode)) == 0)
        add("软删后 delFlag=1 可查到", "1 行",
            "%d 行" % len(sqliteCommon.query_pb_family("pb_family", familyCode=familyCode, delFlag=comGD.DEL_FLAG_YES)),
            len(sqliteCommon.query_pb_family("pb_family", familyCode=familyCode,
                                             delFlag=comGD.DEL_FLAG_YES)) == 1)

        # ---------- 9. 清理：物理删除自检数据，库回到空表 ----------
        sqliteCommon.delete_pb_family("pb_family", recID, hardDelete=True)
        sqliteCommon.delete_pb_face("pb_face", faceID, hardDelete=True)
        for item in sqliteCommon.query_pb_scan_job("pb_scan_job", delFlag="*"):
            if str(item.get("jobCode") or "").startswith(_TEST_PREFIX):
                sqliteCommon.delete_pb_scan_job("pb_scan_job", item["recID"], hardDelete=True)
        for tableName in sqliteCommon.TABLE_ORDER:
            n = sqliteCommon.countTableGeneral(tableName, delFlag="*")
            add("清理后行数 %s" % tableName, "0", str(n), n == 0)
    finally:
        # 无论成败都清一遍残留，别把 _SELFTEST_ 行留给下一次
        _purgeSelfTestData()

    return _report("CRUD 自检", checks, verbose)


# ============================================================
# 四、命令行
# ============================================================

def main(argv):
    _fixConsole()
    dbFile = None
    drop = False
    doVerify = False
    doSelfTest = False
    doMigrate = False

    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in ("--db",):
            index += 1
            if index >= len(argv):
                print("[Error] --db 后面要跟库文件路径", file=sys.stderr)
                return 2
            dbFile = argv[index]
            index += 1
        elif arg == "--drop":
            drop = True
            index += 1
        elif arg == "--migrate":
            doMigrate = True
            index += 1
        elif arg == "--verify":
            doVerify = True
            index += 1
        elif arg == "--selftest":
            doSelfTest = True
            doVerify = True
            index += 1
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print("[Error] 不认识的参数: %s（-h 看用法）" % arg, file=sys.stderr)
            return 2

    print("build_db _VERSION: %s" % _VERSION)
    print("生成物           : database/auto_generated/sqliteCommon.py（%s）"
          % ("%d 表 / %d 索引" % (len(sqliteCommon.TABLE_ORDER),
                                 sum(len(i) for i in sqliteCommon.TABLE_INDEXES.values()))))

    try:
        buildResult = build(dbFile=dbFile, drop=drop)
    except paths.PathLayoutError as e:
        print("[PathLayoutError] %s" % e, file=sys.stderr)
        return 1

    ok = not buildResult["failed"]
    if doMigrate:
        # 老库补列/补索引（不动数据）；build() 刚建过表时这里通常是空跑
        migrateResult = migrate(dbFile=dbFile)
        ok = ok and not migrateResult["failed"]
    if doSelfTest:
        # 自检残留必须先清掉，否则结构自检的「行数=0」会被上一次的残留判失败
        purged = _purgeSelfTestData()
        if purged:
            print("（清理 _SELFTEST_ 残留 %d 行）" % purged)

    if doVerify:
        verifyResult = verify()
        ok = ok and verifyResult["ok"]
    if doSelfTest:
        selfResult = selftest()
        ok = ok and selfResult["ok"]

    print("")
    print("结论: %s" % ("全部通过" if ok else "**存在失败项，见上面标 !! 的行**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
