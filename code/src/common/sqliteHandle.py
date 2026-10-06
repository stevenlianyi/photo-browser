#! /usr/bin/env python3
#encoding: utf-8

#Filename: sqliteHandle.py
#Description: photo-browser SQLite 运行层 —— 原生 sqlite3 封装（读/写双连接 + PRAGMA + %s->?）
#
# 三层结构中的「运行层」（见 plan/数据库设计.md §1.2）：
#     database/pb_*.txt  →  sqliteCodeGenerator.py  →  common/sqliteCommon.py（生成层）
#                                                        ↓
#                                              common/sqliteHandle.py（本文件，运行层）
#                                                        ↓
#                                          processor / engine（业务层，禁止裸 SQL）
#
# 职责
# ----
#   1. open_db(db_path, read_only=False) —— 建连 + 逐条执行 PRAGMA；
#   2. sqliteHandle —— 读写两个连接（DR-3：两个连接执行**同一套** PRAGMA）；
#   3. 占位符 %s -> ?（防注入，**禁止把值拼进 SQL 字符串**）；
#   4. 事务：executemany 批量写 + 显式 commit / rollback。
#
# 返回码约定（生成层与业务层都按这套判断，勿改）
# ---------------------------------------------
#    >= 1   命中/影响行数
#      0    执行成功但无行（DML 影响 0 行 / PRAGMA 无结果）
#     -1    有结果集，行数未知 —— 请接着调 fetchAll / fetchMany / fetchOne
#     -2    出错，详情见 sqliteHandle.lastErrMsg
#
# 硬约束
# ------
#   1. **单写入者**（开发计划 §3.3）：写连接只在主进程/主线程用；
#      check_same_thread=False 是为了 FastAPI 同步端点的线程池，**不是**为了多线程写库。
#   2. 不建物理外键（数据库设计.md D-2），PRAGMA foreign_keys=ON 仅作兜底。
#   3. 本模块**不 import 时连库**：连接一律在 dbHandle()/sqliteHandle() 被显式调用时创建。

import os
import sqlite3
import sys
import threading
from contextlib import contextmanager

# 让本文件在任意 cwd / 直接 `python common\sqliteHandle.py` 执行时都能 import 到兄弟包
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc  # noqa: E402
from config import sqliteSettings as sqliteSettings  # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("sqliteHandle", "sqlitehandle.log")

# fetchMany 的默认批量
FETCH_MANY_DEFAULT = 2000

#: **非主线程**的读连接页缓存上限（KB 为单位取负）。
#:
#: 为什么要单独一个常量：`PRAGMA_LIST` 里的 `cache_size = -64000`（≈64MB）
#: 是按「一条主连接」定的。而步骤 9 之后读路径是**每线程一条连接**
#: （理由见 sqliteHandle._readConn），线程数上界 ≈ FastAPI 线程池 40 + 扫描线程，
#: 64MB × 几十 = 上 GB —— 页缓存虽然是按需增长，但**上限必须收住**，
#: 否则一个失控的查询就能把内存吃满。
#:
#: 8MB 的依据：这些连接只做「点查 + 按索引翻页 + 小聚合」，
#: 实测 10 万张照片的 `/api/timeline` 首屏仍在 100ms 量级，无感。
THREAD_READ_CACHE_SIZE: int = -8000

# 返回码（同时供生成层引用，故挂在类外）
RET_NO_ROW = 0     # 执行成功但无行
RET_HAS_ROWSET = -1   # 有结果集，行数未知 -> 调 fetch*
RET_ERROR = -2    # 出错

# 占位符：.txt / 生成器里统一写 %s，由本模块转成 sqlite3 的 ?
SQL_PLACEHOLDER = sqliteSettings.SQL_PLACEHOLDER          # "%s"
SQLITE_PLACEHOLDER = sqliteSettings.SQLITE_PLACEHOLDER    # "?"


# ============================================================
# 一、占位符转换（防注入的唯一入口）
# ============================================================

def toSqliteSQL(sqlstr, values=None):
    """把 %s 占位符转成 sqlite3 的 ?，并**校验占位符个数与参数个数一致**。

    参数
    ----
    sqlstr : str
        SQL 文本，值一律用 %s 占位（**禁止 f-string / % / + 拼接值**）。
    values : tuple | list | None
        绑定值；None 视作空元组。

    返回
    ----
    (sqlText, valuesTuple)

    Raises
    ------
    TypeError  —— sqlstr 不是字符串
    ValueError —— 占位符个数与参数个数不一致（**早失败好过运行期写脏数据**）
    """
    if not isinstance(sqlstr, str):
        raise TypeError("toSqliteSQL: sql 必须是字符串, 收到 %r" % type(sqlstr))
    values = () if values is None else tuple(values)
    nPlaceholder = sqlstr.count(SQL_PLACEHOLDER)
    if nPlaceholder != len(values):
        raise ValueError(
            "toSqliteSQL: 占位符/参数个数不匹配 -> %d 个 %s vs %d 个值; sql=%s"
            % (nPlaceholder, SQL_PLACEHOLDER, len(values), sqlstr))
    if nPlaceholder == 0:
        return sqlstr, values
    return sqlstr.replace(SQL_PLACEHOLDER, SQLITE_PLACEHOLDER), values


def toSqliteSQLMany(sqlstr, rowLen):
    """executemany 专用的占位符转换：**按「每行的宽度」校验**，不按参数个数。

    与 toSqliteSQL 的区别：批量语句一条 SQL 对应 N 行 × M 列，
    参数个数是 N*M，若按单条语句那样校验必然误报。

    返回 (sqlText, nCol)
    """
    if not isinstance(sqlstr, str):
        raise TypeError("toSqliteSQLMany: sql 必须是字符串, 收到 %r" % type(sqlstr))
    sqlText = sqlstr.replace(SQL_PLACEHOLDER, SQLITE_PLACEHOLDER)
    nCol = sqlText.count(SQLITE_PLACEHOLDER)
    if rowLen and nCol != rowLen:
        raise ValueError("toSqliteSQLMany: 列数不匹配 -> SQL %d 列, 每行数据 %d 列; sql=%s"
                         % (nCol, rowLen, sqlstr))
    return sqlText, nCol


# ============================================================
# 二、建连 + PRAGMA
# ============================================================

def _connect(dbFile, read_only):
    """建一个裸连接（isolation_level=None = autocommit，事务由本模块显式控制）。

    ⚠️ 关于「只读连接」：本项目恒用 WAL 模式，而 **SQLite 不支持对 WAL 库做
       文件级只读连接**（只读连接无法参与 -shm 的读标记协调，open 时直接报
       unable to open database file）。所以 read_only 走等效方案：
       **正常连接 + PRAGMA query_only=ON** —— SQL 层任何写语句都会被 SQLite 拒绝，
       同时还能正常享受 WAL 的「读不阻塞写」。

    ⚠️ query_only 必须在 applyPragmas **之后**才设：journal_mode=WAL 属于写操作，
       query_only 生效后再设会失败（那样读连接就拿不到同一套 PRAGMA，违反 DR-3）。
    """
    timeout = sqliteSettings.PRAGMA_BUSY_TIMEOUT / 1000.0
    return sqlite3.connect(dbFile, timeout=timeout,
                           isolation_level=None, check_same_thread=False)


def applyPragmas(conn, strict=True):
    """逐条执行 config.sqliteSettings.PRAGMA_LIST 里的同一套 PRAGMA（开发计划 §6.2）。

    参数
    ----
    conn   : sqlite3.Connection
    strict : bool
        True —— 任一条失败立即抛错（open_db 对读写连接都传 True：
        少一条 PRAGMA 就会破坏并发模型，不能容忍）；
        False —— 失败只记 warning 并把 "ERR:xxx" 写进返回值（留给将来的降级场景）。

    返回
    ----
    dict —— PRAGMA 名 -> 实际生效值
    """
    result = {}
    for key, val in sqliteSettings.PRAGMA_LIST:
        sql = "PRAGMA %s = %s;" % (key, val)
        try:
            cur = conn.execute(sql)
            row = cur.fetchone()
            result[key] = row[0] if row else None
        except sqlite3.Error as e:
            result[key] = "ERR:%s" % e
            if strict:
                raise
            _LOG.warning("applyPragmas: %s 失败(tolerated): %s" % (sql, e))
    return result


def open_db(db_path, read_only=False):
    """打开 SQLite 连接并执行整套 PRAGMA。

    参数
    ----
    db_path  : str —— 库文件绝对/相对路径
    read_only: bool
        False —— 读写连接（同时负责建库/建文件，缺目录自动创建）
        True  —— 只读连接；库文件不存在直接抛 FileNotFoundError（不静默建库）

    返回
    ----
    sqlite3.Connection
        row_factory = sqlite3.Row、check_same_thread = False、isolation_level = None；
        read_only=True 时额外带 PRAGMA query_only=ON（等效只读，见 _connect 的说明）。
    """
    dbFile = os.path.abspath(os.path.normpath(str(db_path)))
    dbDir = os.path.dirname(dbFile)
    if read_only:
        if not os.path.isfile(dbFile):
            raise FileNotFoundError("open_db: 库文件不存在（read_only=True）: %s" % dbFile)
    elif dbDir and not os.path.isdir(dbDir):
        os.makedirs(dbDir, exist_ok=True)

    conn = _connect(dbFile, read_only)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d;" % sqliteSettings.PRAGMA_BUSY_TIMEOUT)
    # 读写连接执行**同一套** PRAGMA（DR-3）；strict 一律 True —— 少一条都不行
    applyPragmas(conn, strict=True)
    if read_only:
        # 必须在 PRAGMA 之后：journal_mode=WAL 是写操作，query_only 生效后会失败
        conn.execute("PRAGMA query_only = ON")
    return conn


def readPragmas(conn):
    """回读某个连接上 PRAGMA 的**实际生效值**（自检/打印用）。

    注意：journal_mode / synchronous 等是「库级持久项 + 连接级会话项」混合，
    回读到的值才是此刻真正生效的，别拿配置常量当结论。
    query_only 虽不在 PRAGMA_LIST 里，但读连接是否真的只读全靠它，故一并回读。
    """
    result = {}
    for key, _val in sqliteSettings.PRAGMA_LIST:
        try:
            row = conn.execute("PRAGMA %s;" % key).fetchone()
            result[key] = row[0] if row else None
        except sqlite3.Error as e:
            result[key] = "ERR:%s" % e
    try:
        row = conn.execute("PRAGMA query_only;").fetchone()
        result["query_only"] = row[0] if row else None
    except sqlite3.Error as e:
        result["query_only"] = "ERR:%s" % e
    return result


# ============================================================
# 三、sqliteHandle（读写双连接）
# ============================================================

class sqliteHandle:
    """读写双连接封装。

    构造
    ----
    dbHandle = sqliteHandle(dbFile)
        -> dbW 读写连接（负责建库/DDL/DML）、dbR 只读连接（WAL 下互不阻塞）
    dbHandle = sqliteHandle(dbFile, read_only=True)
        -> 两个连接都是只读（用于工具脚本的只读体检）

    说明
    ----
      * 两个连接**各自**执行一遍同一套 PRAGMA（DR-3）；
      * 写连接 isolation_level=None（autocommit），批量写请用
        executeWriteMany + transaction()，否则每行一次 fsync；
      * 线程：check_same_thread=False（FastAPI 线程池需要），
        但**单写入者**是架构硬约束，锁只用来保护游标，不是用来放宽并发写的。
    """

    lastErrMsg = ""
    lastRtnMsg = ""

    def __init__(self, dbFile, read_only=False, autoCommitFlag=True):
        self.dbFile = os.path.abspath(os.path.normpath(str(dbFile)))
        self.readOnly = bool(read_only)
        self.autoCommitFlag = bool(autoCommitFlag)
        self.lastErrMsg = ""
        self.lastSQL = ""

        # ⚠️ 顺序要紧：先开写连接（它负责建库文件），再开只读连接
        self.dbW = open_db(self.dbFile, read_only=self.readOnly)
        self.dbR = self.dbW if self.readOnly else open_db(self.dbFile, read_only=True)
        self.dbWCursor = self.dbW.cursor()
        self.dbRCursor = self.dbR.cursor()
        self._ownerThread = threading.current_thread()
        self._tls = threading.local()
        #: 为「非主线程」懒建的读连接（close 时要一起关）
        self._threadReadConns = []
        self.pragmaWrite = readPragmas(self.dbW)
        self.pragmaRead = readPragmas(self.dbR)
        self.fetchManyBatchNum = FETCH_MANY_DEFAULT
        self._lock = threading.RLock()

    # ---------- 内部 ----------
    #
    # ⚠️⚠️ 为什么读路径必须**每线程一个连接**，而不只是"每线程一个游标"
    # ------------------------------------------------------------------
    #   「每线程一个游标」只解决了一半问题（见下面 _readConn 的说明）。
    #   第二半是 CPython `sqlite3` 的 **per-connection prepared-statement cache**：
    #   同一个连接上**文本相同的 SQL 会被缓存并复用同一个 `sqlite3_stmt`**，
    #   于是两个游标共享同一个底层语句 —— B 游标 execute 时会
    #   `sqlite3_reset` 掉这个共享语句，**把 A 游标待取的那一行丢掉**：
    #
    #       线程A: executeRead("SELECT COUNT(*) ... delFlag = ?")   -> 语句已 step 出 1 行
    #       线程B: executeRead(同一段 SQL 文本)                      -> reset 掉共享语句
    #       线程A: fetchValue()                                      -> None（静默！）
    #
    #   实测证据（`code/.probe_min.py`，现已删除）：
    #     · 6 线程跑**同一段 SQL 文本** -> 第 1 次尝试就复现
    #       （`code=-1` 有结果集、`description` 非空、`rowcount=-1`、
    #        同一游标两次 `fetchone()` 都是 None、另起独立查询却正常）
    #     · 把每线程的 SQL 文本各加几个空格（= 不同缓存键）-> **60 次零失败**
    #   ⇒ 结论：语句缓存是**连接级**的，所以只有"每线程一个**连接**"才治本。
    #
    #   为什么只给**读**路径开每线程连接（写路径不用）
    # ------------------------------------------------
    #     · 读是并发的主体（FastAPI 请求线程池 + 后台扫描线程同时读）；
    #     · 读路径是「execute 之后另起一次调用才 fetch」，天然要给每条线程
    #       自己的结果集；而**写路径每条语句都在 `self._lock` 里一次做完
    #       （bind + step 到底，不迭代取行）**，不存在"待取的行被别人 reset"；
    #     · 写路径受「单写入者」硬约束保护（scanScheduler 的门闩 + 本项目
    #       不用多线程并发写），开多条写连接反而会把"静默交错进同一事务"
    #       变成"database is locked"，是另一种回退。
    #     · 另外：读连接本来就和写连接不是同一条（WAL 设计），所以这一步**没有
    #       引入任何新的可见性语义** —— 改动前非主线程也在读 `dbR`。

    def _readConn(self):
        """本线程该用的**读连接**。

        主线程沿用启动时那条 `dbR`（单线程调用方行为与改动前逐位相同）；
        其余线程**懒建**一条自己的只读连接并缓存进 thread-local。
        """
        conn = getattr(self._tls, "readConn", None)
        if conn is not None:
            return conn
        if threading.current_thread() is self._ownerThread:
            conn = self.dbR
        else:
            conn = self._openReadConn()
        self._tls.readConn = conn
        return conn

    def _openReadConn(self):
        """给当前（非主）线程开一条只读连接。

        ⚠️ `cache_size` 必须调小：PRAGMA_LIST 里是 **-64000 ≈ 64MB/连接**，
           那是为"一条主连接"定的。读线程最多可能有几十条
           （FastAPI 默认线程池 40 + 扫描线程），64MB × 几十 = 上 GB，
           而每条连接实际只会摸到很少的页 —— 页缓存是**按需增长**的，
           但上限必须收住，否则一个失控的查询能把内存吃满。
           8MB 对「点查 + 按索引翻页」绰绰有余；正经的大分析查询
           （timeline 的 GROUP BY）走的是请求线程，也是 8MB，实测无感
           （10 万张首屏 p50 仍在 100ms 量级）。
        """
        conn = open_db(self.dbFile, read_only=True)
        try:
            conn.execute("PRAGMA cache_size = %d" % THREAD_READ_CACHE_SIZE)
        except sqlite3.Error as e:                    # 只降级，不失败
            _LOG.warning("_openReadConn: 设置 cache_size 失败（沿用默认）: %s" % e)
        with self._lock:
            self._threadReadConns.append(conn)
            total = len(self._threadReadConns)
        _LOG.debug("_openReadConn: 为线程 %s 新建读连接（当前共 %d 条）"
                   % (threading.current_thread().name, total))
        return conn

    def _readCursor(self):
        """本线程的读游标。

        executeRead 会**每条语句新建一个游标**并存进 thread-local，
        所以 fetch* 取回的必定是「本线程、本条语句」那个游标。
        """
        return getattr(self._tls, "readCursor", None) or self._readConn().cursor()

    def _writeCursor(self):
        """本线程的写游标（写在共享的 `dbW` 上，理由见上面那段说明）。"""
        return getattr(self._tls, "writeCursor", None) or self.dbWCursor

    def _err(self, tag, e, sqlstr=""):
        self.lastErrMsg = "%s:%s,sql:%s" % (tag, e, sqlstr)
        _LOG.error(self.lastErrMsg)
        return RET_ERROR

    def _rowToDict(self, row):
        return dict(row) if row is not None else None

    def _rtn(self, cursor, hasRowSet):
        """归一化 rowcount。

        SQLite 对 DDL（CREATE/DROP INDEX/TABLE 等）返回 -1，
        与「有结果集请 fetch*」的 -1 语义冲突，故统一折成 RET_NO_ROW(0)：
        **返回值 -1 只可能表示「有结果集」**。
        """
        if hasRowSet:
            return RET_HAS_ROWSET
        rtn = cursor.rowcount
        if rtn is None or rtn < 0:
            return RET_NO_ROW
        return rtn

    # ---------- 读 ----------

    def executeRead(self, sqlstr, values=()):
        """执行读语句（SELECT / PRAGMA 读）。

        返回 RET_HAS_ROWSET(-1) 表示有结果集，请接着调 fetchAll/fetchMany/fetchOne。

        ⚠️ 用**本线程自己的**游标（见 __init__ 里的说明）：共享游标会让
           「后台线程 + 请求线程」交替时互相抢结果集。
        """
        try:
            sqlText, vals = toSqliteSQL(sqlstr, values)
            self.lastSQL = sqlText
            # ⚠️ 每条语句都新建游标，且用**本线程的连接**（见 _readConn 的说明）：
            #    · 新游标 -> 上一次 execute 的结果集不会被这次 execute 顶掉，
            #      "execute A -> execute B -> fetch" 也不会拿错（B 的游标在 tls 里）
            #    · 本线程连接 -> 不与其他线程共享 per-connection 的语句缓存
            #      （共享语句会被对方的 execute `sqlite3_reset` 掉 -> 静默丢行）
            cursor = self._readConn().cursor()
            self._tls.readCursor = cursor
            cursor.execute(sqlText, vals)
            return self._rtn(cursor, cursor.description is not None)
        except Exception as e:
            return self._err("executeRead", e, sqlstr)

    def executeReadList(self, sqlList):
        """连续执行多条读语句（每条是 (sql, values)）。返回每条的结果码列表。

        ⚠️ 逐条**各自**用新游标（与 executeRead 一致），所以只有最后一条的
           结果集还在游标上 —— 这与老行为一致（老代码也是只有最后一个）。
        """
        result = []
        for item in sqlList:
            sqlstr, values = item[0], (item[1] if len(item) > 1 else ())
            result.append(self.executeRead(sqlstr, values))
        return result

    # ---------- 写 ----------

    def executeWrite(self, sqlstr, values=()):
        """执行写语句（INSERT/UPDATE/DELETE/DDL）。autoCommitFlag=True 时自动提交。"""
        try:
            sqlText, vals = toSqliteSQL(sqlstr, values)
            self.lastSQL = sqlText
            with self._lock:
                cursor = self.dbW.cursor()
                self._tls.writeCursor = cursor
                cursor.execute(sqlText, vals)
                rtn = self._rtn(cursor, False)
                if self.autoCommitFlag:
                    self.dbW.commit()
            return rtn
        except Exception as e:
            self.rollbackWrite()
            return self._err("executeWrite", e, sqlstr)

    def executeWriteList(self, sqlList):
        """连续执行多条写语句，整批一次提交（任一条失败整批回滚）。"""
        result = []
        try:
            with self._lock:
                cursor = self.dbW.cursor()
                self._tls.writeCursor = cursor
                for item in sqlList:
                    sqlstr, values = item[0], (item[1] if len(item) > 1 else ())
                    sqlText, vals = toSqliteSQL(sqlstr, values)
                    self.lastSQL = sqlText
                    result.append(self._rtn(cursor.execute(sqlText, vals), False))
                if self.autoCommitFlag:
                    self.dbW.commit()
            return result
        except Exception as e:
            self.rollbackWrite()
            self.lastErrMsg = "executeWriteList:%s" % e
            _LOG.error(self.lastErrMsg)
            return RET_ERROR

    def executeWriteMany(self, sqlstr, valuesList):
        """executemany 批量写 —— 返回影响行数（-1 表示驱动未给出）。

        典型用法（步骤 3 扫描入库）：
            with db.transaction():
                n = db.executeWriteMany(sqlStr, rows)
            # 出了 with 自动 commit；抛异常自动 rollback
        """
        rows = list(valuesList or [])
        if not rows:
            return 0
        try:
            sqlText, nCol = toSqliteSQLMany(sqlstr, len(rows[0]))
            self.lastSQL = sqlText
            for row in rows:
                if len(row) != nCol:
                    raise ValueError(
                        "executeWriteMany: 列数不匹配 -> SQL %d 列, 数据 %d 列"
                        % (nCol, len(row)))
            with self._lock:
                cursor = self.dbW.cursor()
                self._tls.writeCursor = cursor
                cursor.executemany(sqlText, rows)
                rtn = self._rtn(cursor, False)
                # ⚠️ 这里**不要**去取 `cursor.lastrowid`：CPython 在
                #    `executemany()` 之后不更新它（实测为 None），
                #    要批量的最后一个 id 得用 `SELECT last_insert_rowid()`。
                #    详见 insertID() 的 docstring（那里有完整的实测表）。
                if self.autoCommitFlag and not self.inTransaction():
                    self.dbW.commit()
            return rtn
        except Exception as e:
            self.rollbackWrite()
            return self._err("executeWriteMany", e, sqlstr)

    # ---------- 取数 ----------
    #
    # ⚠️ 下面四个都从 thread-local 取**自己那个**游标（见 __init__ 的说明）。
    #    改动前它们用的是共享游标，于是「A 执行 -> B 执行 -> A 取数」
    #    会取到 B 的行；并发 fetchall 与 execute 还会直接段错误。
    #    单线程调用方拿到的游标与以前是同一个（thread-local 里只存一个）。

    def insertID(self):
        """最近一次 INSERT 的自增主键（recID）。

        ⚠️ 关于 `lastrowid` 的**实测结论**（步骤 9 专门查过，别再重复调查）
        ------------------------------------------------------------------
        本机 CPython 3.13.14 / sqlite3 3.53.1 上实测（`Cursor.lastrowid`）：

        | 场景 | 行为 |
        |---|---|
        | 同游标两次 INSERT | **会更新**为新的 id |
        | **另一个游标**在同一连接上 INSERT | **互不影响** -> 是**游标级**，无跨线程竞态 |
        | commit 之后 | 不受影响 |
        | UPDATE / DELETE 之后 | 不受影响（仍是上次插入的 id，SQLite 不清零） |
        | **executemany 之后** | **`None`**（CPython 明确不更新，文档有写） |

        所以：
          * **不需要**为了并发去加锁或拍快照 —— `_writeCursor()` 取的是
            **本线程自己的**游标，值天然是本线程的（早期怀疑过
            「连接级 lastrowid 会被别的线程抢走」，实测不成立）。
          * 真正要注意的是最后一行：**批量插入之后 `insertID()` 无意义**。
            本仓库目前没有任何地方那样用（`insertMany*` 返回的是
            `(影响行数, 列名)`，不返回 id；只有 `insert_pb_*` 单条插入才配
            `insertID()`）。将来若要拿批量的最后一个 id，
            得另写（`SELECT last_insert_rowid()`），别指望本函数。
        """
        return self._writeCursor().lastrowid

    def fetchAll(self):
        """取全部行 -> list[dict]"""
        return [dict(row) for row in self._readCursor().fetchall()]

    def fetchMany(self, num=FETCH_MANY_DEFAULT):
        """取前 num 行 -> list[dict]（默认 2000，批量翻页用）"""
        if not num:
            num = self.fetchManyBatchNum
        return [dict(row) for row in self._readCursor().fetchmany(num)]

    def fetchOne(self):
        """取一行 -> dict | None"""
        return self._rowToDict(self._readCursor().fetchone())

    def fetchValue(self, default=None):
        """取首行首列的值（COUNT(*) / MAX(id) 之类），无结果返回 default"""
        row = self._readCursor().fetchone()
        if row is None:
            return default
        return row[0] if len(row) > 0 else default

    # ---------- 事务 ----------

    def inTransaction(self):
        """当前写连接是否处于显式事务中"""
        return bool(self.dbW.in_transaction)

    def begin(self):
        """显式开事务（配合 autoCommitFlag=False 使用）"""
        with self._lock:
            if not self.dbW.in_transaction:
                self.dbW.execute("BEGIN")
        return True

    def commit(self):
        with self._lock:
            if self.dbW.in_transaction:
                self.dbW.commit()
        return True

    def rollbackWrite(self):
        """回滚写连接（出错时兜底，绝不吞掉未提交的事务）"""
        try:
            if self.dbW.in_transaction:
                self.dbW.rollback()
        except sqlite3.Error as e:
            _LOG.error("rollbackWrite:%s" % e)
        return True

    def rollbackRead(self):
        try:
            if self.dbR.in_transaction:
                self.dbR.rollback()
        except sqlite3.Error as e:
            _LOG.error("rollbackRead:%s" % e)
        return True

    @contextmanager
    def transaction(self):
        """事务上下文：`with db.transaction(): ...` 正常则 commit，异常则 rollback。

        写连接是 autocommit 模式（isolation_level=None），所以必须用本函数
        显式包住，才能把「一批 N 行」变成「一次 fsync」。
        """
        self.begin()
        try:
            yield self
        except Exception:
            self.rollbackWrite()
            raise
        else:
            self.commit()

    # ---------- 杂项 ----------

    def pragmaDict(self):
        """当前两个连接实际生效的 PRAGMA 值（自检/打印用）"""
        return {"write": self.pragmaWrite, "read": self.pragmaRead}

    def close(self):
        """关闭**全部**连接（主读写两条 + 所有每线程读连接）。

        ⚠️ 必须在**主线程**调（`sqliteCommon.closeDb()` 就是这么用的）：
           每线程读连接是"谁建谁用"的，别的线程不该去关它们 ——
           那个线程若还在用，关掉就是 use-after-free（段错误的温床）。
           本函数只负责**进程收尾**时把句柄交还给 OS；
           因此调用前必须确保后台线程已经停下
           （`api.scan.resetScheduler()` 会等后台扫描线程退出，见 main/app.py 的 lifespan）。
        """
        try:
            self.rollbackWrite()
        finally:
            # ⚠️ 先去重：readOnly 模式下 dbW 与 dbR 是**同一条**
            conns, seen = [], set()
            with self._lock:
                extra = list(self._threadReadConns)
                self._threadReadConns = []
            for conn in [self.dbW, self.dbR] + extra:
                if conn is None or id(conn) in seen:
                    continue
                seen.add(id(conn))
                conns.append(conn)
            for conn in conns:
                try:
                    conn.close()
                except sqlite3.Error as e:
                    _LOG.error("close:%s" % e)
            if extra:
                _LOG.info("close: 已关闭 %d 条每线程读连接" % len(extra))
        return True


if __name__ == "__main__":
    # 自检：建临时库 -> 双连接 PRAGMA -> %s->? -> blob 往返
    import tempfile

    tmpDir = tempfile.mkdtemp(prefix="pbsqlite_")
    tmpDb = os.path.join(tmpDir, "selftest.db")
    print("sqliteHandle _VERSION :", _VERSION)
    print("temp db               :", tmpDb)

    h = sqliteHandle(tmpDb)
    print("pragma(write)         :", h.pragmaDict()["write"])
    print("pragma(read)          :", h.pragmaDict()["read"])

    rtn = h.executeWrite(
        "CREATE TABLE IF NOT EXISTS t_selftest ("
        "recID INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, vec BLOB NULL)")
    print("create                :", rtn)

    rtn = h.executeWrite("INSERT INTO t_selftest (name, vec) VALUES (%s, %s)",
                         ("abc", b"\x00\x01" * 8))
    print("insert                :", rtn, "recID =", h.insertID())

    h.executeRead("SELECT recID, name, vec FROM t_selftest WHERE name = %s", ("abc",))
    row = h.fetchOne()
    print("query                 :", row["recID"], row["name"], len(row["vec"]), "bytes")

    h.executeWrite("UPDATE t_selftest SET name = %s WHERE recID = %s", ("xyz", row["recID"]))
    h.executeRead("SELECT name FROM t_selftest WHERE recID = %s", (row["recID"],))
    print("update                :", h.fetchOne()["name"])

    h.executeRead("SELECT COUNT(*) AS n FROM t_selftest")
    print("count                 :", h.fetchValue())
    h.executeRead("PRAGMA integrity_check")
    print("integrity_check       :", h.fetchValue())

    h.executeWrite("DELETE FROM t_selftest WHERE recID = %s", (row["recID"],))
    h.executeRead("SELECT COUNT(*) AS n FROM t_selftest")
    print("delete                :", h.fetchValue())

    h.close()
    print("OK  ->  临时库保留在", tmpDb)
