#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_sqlite_handle_concurrency.py
#Description: `sqliteHandle` 的**多线程游标隔离**测试（步骤 9 补）
#
# ══════════════════════════════════════════════════════════════════════
# 为什么必须有这个文件（**回归全绿证明不了并发正确**）
# ══════════════════════════════════════════════════════════════════════
#   步骤 9 第一次让「后台扫描线程 + FastAPI 请求线程」**同时**访问同一个
#   `sqliteHandle`。而原实现的 `executeRead` 只在 `execute` 时持锁，
#   `fetchAll/fetchMany/fetchOne/fetchValue` 用的是**同一个共享游标**
#   且**不持锁**。单线程时完全看不出问题：
#
#       线程A: executeRead(SELECT A)  -> dbRCursor 定位到 A 的结果集
#       线程B: executeRead(SELECT B)  -> **同一个游标**被定位到 B
#       线程A: fetchAll()             -> **拿到 B 的行**
#
#   后果两种，都很糟且都**不报错**：
#     · 读到别人的行 -> 「这张照片有 3 张脸」变成 1 张；
#     · fetchall 与 execute 并发作用于同一 cursor -> **进程级 access violation**
#       （实测：pytest 进程被 Windows 直接杀死，exit 0xC0000005，
#        traceback 只有 `runner.loadIndex <- query_pb_photo` 的片段，看不出根因）。
#
#   ⚠️ 关键在于：**旧的单线程测试全部通过**。它们与并发正确性完全无关 ——
#      一个共享游标在单线程下与每线程游标的行为**逐位相同**。
#      所以并发修复必须有自己的证据，不能靠「跑了一遍别的用例没炸」。
#
# 三条断言的分工
# --------------
#   ① **结果不串行**：两线程各自 execute + fetch，结果必须各自正确。
#      这是最直接的证据（现象级）。
#   ② **读到的行数正确**：用互不相同的行数当指纹 —— 数量对不上必然串了。
#   ③ **表名当指纹**：两个查询查**不同的表**且列名不重叠。
#      串行时 `dict(sqlite3.Row)` 会用错行的列名 -> 要么少列要么 KeyError。
#      这一条连「行数恰好相同」的极端情况也能抓住。
#
# ⚠️ 测试本身必须是**确定性的**：不能靠「跑一遍没炸就算过」。
#    所以用 barrier 让两个线程**精确地**交错在
#    「A.execute -> B.execute -> A.fetch -> B.fetch」这个顺序上，
#    而不是靠随机 timing 去碰。

import threading

import pytest


@pytest.fixture
def lib(tmp_path, set_photo_root):
    """一个带 3 张表、行数互不相同的临时库 + 已连接的 sqliteHandle。"""
    from common import paths as pathsMod
    from common import sqliteHandle as sqliteHandle
    from database.auto_generated import sqliteCommon as sqliteCommon
    from tools import build_db as build_db

    root = tmp_path / "PhotoLib"
    (root / "photo").mkdir(parents=True)
    set_photo_root(str(root))
    dbFile = str(root / "db" / "conc.db")
    build_db.build(dbFile=dbFile, verbose=False)
    sqliteCommon.dbHandle(dbFile)
    handle = sqliteCommon.dbHandle()
    try:
        yield {"dbFile": dbFile, "handle": handle, "sqliteCommon": sqliteCommon}
    finally:
        sqliteCommon.closeDb()
        pathsMod.clearRootOverride()


#: 表 -> **带 UNIQUE 索引的列**（一个都不能漏）。
#: ⚠️ 必须逐行造唯一值：`pb_person` 上 **personCode 与 displayName 都是 UNIQUE**，
#:    只给 personCode 造唯一、displayName 塞同值，整批照样 `UNIQUE constraint failed`
#:    （这篇测试自己踩过两次：seeds 全失败 -> 断言里的行数全变成 0 ->
#:     看起来像「并发把结果串了」，其实是被测代码压根没错；
#:     第二次是漏了 displayName 这一列）。
_UNIQUE_COLUMNS = {
    "pb_family": ("familyCode",),
    "pb_person": ("personCode", "displayName"),
    "pb_person_category": (),
    "pb_photo": ("photoCode", "relPathHash", "fileHash"),
    "pb_face": ("faceCode",),
    "pb_scan_job": ("jobCode",),
    "pb_review_log": ("logCode",),
}


def _seed(sqliteCommon, table, n, prefix="k", **fixed):
    """插 n 行（**行数就是这篇测试的指纹**），**全部唯一列**逐行取值。"""
    keys = _UNIQUE_COLUMNS.get(table, ())
    rows = []
    for i in range(n):
        row = dict(fixed)
        for column in keys:
            # 调用方显式给了值就尊重它（但那样也只会有 1 行，属调用方的事）
            row.setdefault(column, "%s%d" % (column, i))
            row[column] = "%s_%s%d" % (prefix, column, i)
        row.setdefault("delFlag", "0")
        rows.append(row)
    if not rows:
        return 0
    sqliteCommon.insertManyTableGeneral(table, rows, fillStandard=True)
    return len(rows)


def test_interleavedExecuteFetchDoesNotStealResultSets(lib):
    """① + ②：两线程**精确交错** execute/fetch，各自必须拿到自己的行数。"""
    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    from common import sqliteHandle as sqliteHandle

    # 用**行数**当指纹：A 只看 a_ 前缀的 5 行，B 看全部（5+3+2=10 行）。
    _seed(sqliteCommon, "pb_family", 5, prefix="a", familyName="x")
    _seed(sqliteCommon, "pb_family", 3, prefix="b", familyName="y")
    _seed(sqliteCommon, "pb_family", 2, prefix="c", familyName="z")
    assert sqliteCommon.countTableGeneral("pb_family") == 10

    # 用 barrier 把两个线程**顶在同一个点上**：两边都 execute 完才允许 fetch。
    # ⇒ 游标若是共享的，后 execute 的那个必然把前一个的结果集顶掉，
    #   于是先 fetch 的那个拿到别人的行。**确定性复现，不靠 timing。**
    bothExecuted = threading.Barrier(2)
    results, errors = {}, {}

    # ⚠️ 用 `fetchAll` 取**行**（不是 COUNT 聚合）：
    #    聚合查询无论串不串都只返回 1 行 -> 行数指纹失效。
    #    这里 A 期望 5 行、B 期望 10 行，串了必然对不上。
    def worker(tag: str, sql: str, values: tuple):
        try:
            code = handle.executeRead(sql, values)
            if code != sqliteHandle.RET_HAS_ROWSET:
                raise AssertionError("executeRead 返回 %s（期望有结果集）" % code)
            bothExecuted.wait(timeout=10)          # 等另一边也 execute 完
            rows = handle.fetchAll()
            results[tag] = len(rows)
            results[tag + "_codes"] = sorted(str(r.get("familyCode") or "")
                                             for r in rows)
        except Exception as e:                     # noqa: BLE001
            errors[tag] = "%s: %s" % (type(e).__name__, e)
            try:
                bothExecuted.abort()               # 别让另一边永远等下去
            except Exception:
                pass

    threads = [
        threading.Thread(target=worker, name="A",
                         args=("A", "SELECT familyCode, familyName FROM pb_family"
                                    " WHERE familyCode LIKE %s", ("a_%",))),
        threading.Thread(target=worker, name="B",
                         args=("B", "SELECT familyCode, familyName FROM pb_family"
                                    " WHERE delFlag = %s", ("0",))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    #: `_seed` 的取值格式是 `<prefix>_<列名><序号>`
    def codes(prefix, n):
        return ["%s_familyCode%d" % (prefix, i) for i in range(n)]

    assert not errors, "并发读抛异常了：%s" % errors
    assert results.get("A") == 5, results     # a_ 前缀那 5 行
    assert results.get("B") == 10, results    # 全部 10 行
    # 连**具体内容**也要对：串行时 A 可能恰好也拿到「10 行里的 5 行」
    # —— 行数看起来正常，内容却是别人的。
    assert results.get("A_codes") == codes("a", 5), results
    assert results.get("B_codes") == sorted(
        codes("a", 5) + codes("b", 3) + codes("c", 2)), results


def test_interleavedFetchUsesOwnColumnNames(lib):
    """③：查**不同的表**（列名不重叠）—— 串行时 `dict(sqlite3.Row)` 会用错列名。

    这一条比行数更狠：即便两个查询恰好返回同样的行数，
    拿错结果集也必然导致列名对不上（少列 / KeyError / 值类型不对）。
    """
    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]

    _seed(sqliteCommon, "pb_family", 3, prefix="f", familyName="n1")
    _seed(sqliteCommon, "pb_scan_job", 3, prefix="j", rootPath="/tmp")

    bothExecuted = threading.Barrier(2)
    results, errors = {}, {}

    def worker(tag: str, sql: str, values: tuple, wantColumn: str):
        try:
            handle.executeRead(sql, values)
            bothExecuted.wait(timeout=10)
            row = handle.fetchOne()
            results[tag] = sorted(row.keys()) if row else []
            results[tag + "_wanted"] = bool(row and wantColumn in row)
        except Exception as e:                     # noqa: BLE001
            errors[tag] = "%s: %s" % (type(e).__name__, e)
            try:
                bothExecuted.abort()
            except Exception:
                pass

    threads = [
        # F：2 列，带占位符
        threading.Thread(target=worker, name="F",
                         args=("F", "SELECT familyCode, familyName FROM pb_family"
                                    " WHERE delFlag = %s", ("0",), "familyCode")),
        # J：3 列（列数与 F 不同），无占位符
        threading.Thread(target=worker, name="J",
                         args=("J", "SELECT jobCode, rootPath, batchSize"
                                    " FROM pb_scan_job", (), "jobCode")),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, "并发读抛异常了：%s" % errors
    assert results["F"] == ["familyCode", "familyName"], results
    assert results["J"] == ["batchSize", "jobCode", "rootPath"], results
    assert results["F_wanted"] is True and results["J_wanted"] is True


def test_manyThreadsMixedReadsStayConsistent(lib):
    """压力版：6 线程 × 30 轮读写混合，**一次都不许串**。

    与上面两个「精确交错」互补：这个跑的是**长时间的真实竞争**，
    覆盖到 fetchMany / fetchValue / 写语句与读语句交错等分支。
    """
    from common import sqliteHandle as sqliteHandle

    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    _seed(sqliteCommon, "pb_family", 50, prefix="s", familyName="n")
    _seed(sqliteCommon, "pb_person", 13, prefix="p", displayName="d")

    expected = {"pb_family": 50, "pb_person": 13}
    failures, stop = [], threading.Event()

    def reader(table: str, rounds: int):
        sql = "SELECT COUNT(*) AS cnt FROM %s WHERE delFlag = %%s" % table
        try:
            for _ in range(rounds):
                if stop.is_set():
                    return
                # ⚠️ **必须检查返回码**。`executeRead` 出错时返回 RET_ERROR
                #    而**不抛异常** —— 不检查的话，随后的 `fetchValue(-1)`
                #    会去读**上一次**遗留的游标（已耗尽）-> 拿到默认值 -1，
                #    表现成「COUNT 串了」，而真实原因是「这条查询压根没执行」。
                #    （这篇测试第一版就是这么写的：偶发失败报 `got -1`，
                #      误导方向，多查了一轮。）
                code = handle.executeRead(sql, ("0",))
                # ⚠️ 只在 execute 之后**记一个 id**（不持强引用，避免因
                #    "测试自己把游标救活了"而掩盖问题）。失败时比对它有没有变。
                cursorIdAfterExec = id(getattr(handle._tls, "readCursor", None))
                if code != sqliteHandle.RET_HAS_ROWSET:
                    failures.append("%s executeRead 失败 code=%s err=%s"
                                    % (table, code, handle.lastErrMsg))
                    return
                value = handle.fetchValue(-1)
                if int(value or 0) != expected[table]:
                    nowId = id(getattr(handle._tls, "readCursor", None))
                    cur = handle._readCursor()
                    # ⚠️ 关键取证：另起一条**完全独立的**查询（走 sqliteCommon，
                    #    内部再开游标），用来区分两种情况：
                    #      · 数据不在 -> 独立查询也会给出 0/异常
                    #      · 只有**这一个游标**坏了 -> 独立查询正常
                    try:
                        independent = sqliteCommon.countTableGeneral(table)
                    except Exception as e:          # noqa: BLE001
                        independent = "raise:%s" % e
                    failures.append(
                        "%s COUNT 串了: got %s want %s | round=%d code=%s"
                        " idAfterExec=%s idNow=%s same=%s"
                        " isMainReadConn=%s rowcount=%s secondFetch=%s"
                        " independentCount=%s lastSQL=%r lastErr=%r"
                        % (table, value, expected[table], _,
                           code, cursorIdAfterExec, nowId,
                           cursorIdAfterExec == nowId,
                           handle._readConn() is handle.dbR,
                           getattr(cur, "rowcount", None), cur.fetchone(),
                           independent, handle.lastSQL, handle.lastErrMsg))
                    return
                # 再走 fetchMany 的分支（也要检查返回码）
                code = handle.executeRead(
                    "SELECT familyCode, familyName FROM pb_family"
                    " WHERE delFlag = %s LIMIT 3", ("0",))
                if code != sqliteHandle.RET_HAS_ROWSET:
                    failures.append("fetchMany 前 executeRead 失败 code=%s err=%s"
                                    % (code, handle.lastErrMsg))
                    return
                rows = handle.fetchMany(2)
                if any("familyCode" not in r for r in rows):
                    failures.append("fetchMany 拿到了别的表的行: %s" % rows)
                    return
        except Exception as e:                     # noqa: BLE001
            failures.append("%s: %s" % (type(e).__name__, e))

    threads = ([threading.Thread(target=reader, args=("pb_family", 30))
                for _ in range(3)]
               + [threading.Thread(target=reader, args=("pb_person", 30))
                  for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    stop.set()

    assert not failures, failures


def test_perThreadReadConnectionIsIsolated(lib):
    """把「实现细节」也钉住：每个子线程拿到**自己的读连接**，主线程沿用 `dbR`。

    ⚠️ 为什么不只看行为：行为断言在「偶然没踩上」时会绿。这一条直接检查
       `_readConn()` 的隔离性 —— 一旦有人把它改回"所有线程共用 `dbR`"，
       它会**立刻**红，而不必等一个概率性的竞态复现。

    ⚠️ 为什么是**连接**而不是游标：请读 `sqliteHandle._readConn` 的注释 ——
       游标隔离只解决一半问题；共享**连接**上的 per-connection 预处理语句缓存
       仍会让两个线程的同文本 SQL 共享同一个 `sqlite3_stmt`，
       后被 execute 的那个会 `sqlite3_reset` 掉前者待取的行，
       前者 `fetchOne()` 静默返回 None。**只有连接级隔离才治本。**
    """
    handle = lib["handle"]
    seen = {}

    def grab(tag: str):
        handle.executeRead("SELECT COUNT(*) AS cnt FROM pb_family WHERE delFlag = %s",
                           ("0",))
        # ⚠️ 存**连接对象本身**（强引用），不存 id()：
        #    线程结束后它的 thread-local 会被释放，对象可被 GC，
        #    而 id() 会被后面新建的对象复用 -> 断言**假红**
        #    （这篇测试第一版就踩过：t0 与 t1 的 id 相同）。
        seen[tag] = handle._readConn()

    mainConn = handle._readConn()
    threads = [threading.Thread(target=grab, args=("t%d" % i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(seen) == 3, seen
    assert mainConn is handle.dbR, "主线程应沿用启动时的 dbR"
    conns = list(seen.values())
    assert all(c is not handle.dbR for c in conns), \
        "子线程共用了主线程的读连接 -> 预处理语句缓存仍会被共享"
    for i in range(len(conns)):
        for j in range(i + 1, len(conns)):
            assert conns[i] is not conns[j], \
                "两个子线程共用了同一条读连接 -> 隔离失效: %s" % {
                    k: id(v) for k, v in seen.items()}
    # 建出来的连接必须被登记（close 时要一起关，否则句柄泄漏）
    assert len(handle._threadReadConns) == 3


def test_threadReadConnectionsAreClosedWithHandle(lib):
    """每线程连接必须被 `close()` 收干净（否则反复换库会漏文件句柄）。"""
    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    _seed(sqliteCommon, "pb_family", 50, prefix="c", familyName="n")

    def grab():
        handle.executeRead("SELECT COUNT(*) AS cnt FROM pb_family WHERE delFlag = %s",
                           ("0",))

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(handle._threadReadConns) == 3

    sqliteCommon.closeDb()
    assert handle._threadReadConns == [], "close 之后不该还留着每线程连接"
    # 复开一次，确认 close 之后还能正常用（不残留半个状态）
    handle2 = sqliteCommon.dbHandle(lib["dbFile"])
    handle2.executeRead("SELECT COUNT(*) AS cnt FROM pb_family WHERE delFlag = %s",
                        ("0",))
    assert handle2.fetchValue(-1) == 50


def test_sameSqlTextAcrossThreadsDoesNotLoseRows(lib):
    """**这个 bug 的直接回归钉**：多线程跑**同一段 SQL 文本**，一行都不许丢。

    ⚠️ 这一条与 `test_manyThreadsMixedReadsStayConsistent` 的区别在于
       **可复现性**：它把并发压在同一段 SQL 上（正是缓存键相同的条件），
       并跑很多轮 —— 改回"共享连接 + 每线程游标"时它几乎必然红。
    背景（实测数据）：
      · 6 线程 × 同一段 SQL 文本 -> 第 1 次尝试就复现丢行
      · 每线程 SQL 文本各加几个空格（= 不同缓存键）-> 60 次零失败
    这条断言的就是"同文本"这个最坏情况。
    """
    from common import sqliteHandle as sqliteHandle

    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    sqliteCommon.insertManyTableGeneral(
        "pb_family",
        [{"familyCode": "k%d" % i, "familyName": "n", "delFlag": "0"}
         for i in range(50)], fillStandard=True)

    #: 所有线程共用**同一段 SQL 文本**（缓存键相同 -> 最容易撞上共享语句）
    SQL = "SELECT COUNT(*) AS cnt FROM pb_family WHERE delFlag = %s"
    failures = []
    stop = threading.Event()

    def reader(tag, rounds):
        for n in range(rounds):
            if stop.is_set() or failures:
                return
            code = handle.executeRead(SQL, ("0",))
            if code != sqliteHandle.RET_HAS_ROWSET:
                failures.append("%s: executeRead code=%s err=%s"
                                % (tag, code, handle.lastErrMsg))
                return
            value = handle.fetchValue(None)
            if value != 50:
                failures.append(
                    "%s round=%d: 丢了行 -> fetchValue=%r（期望 50）"
                    " lastSQL=%r lastErr=%r"
                    % (tag, n, value, handle.lastSQL, handle.lastErrMsg))
                return

    threads = [threading.Thread(target=reader, args=("t%d" % i, 300))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    stop.set()

    assert not failures, failures


# ============================================================
# insertID 的契约（**特征化钉子，不是"修了 bug 的证据"**）
# ============================================================
#
# ⚠️ 先说结论，免得下一个人重复调查一整轮（本轮就白查了一次）：
#
#   一开始怀疑 `Cursor.lastrowid` 是**连接级**的
#   （= 引擎里的 `sqlite3_last_insert_rowid(connection)`），于是多线程下
#       线程A: INSERT ...  线程B: INSERT ...  线程A: insertID() -> 拿到 B 的 id
#   会造成「A 拿着 B 的 recID 去 update，静默改到 B 那一行」。
#   并且据此写了一版「在锁内拍 lastrowid 快照」的实现。
#
#   **实测把这个怀疑否掉了**（CPython 3.13.14 / sqlite3 3.53.1）：
#     · 另一个游标在同一连接上 INSERT -> **本游标的值不变** => 是**游标级**，无竞态；
#     · commit 之后、UPDATE/DELETE 之后 -> 都不变；
#     · 只有 **executemany 之后** 会变成 None（CPython 明确不更新）。
#   于是那版快照实现被**撤掉了** —— 它修的是一个不存在的问题，
#   而给共用的 DB 层加投机性代码，比不加更贵。
#   （`sqliteHandle.insertID()` 的 docstring 里有完整的实测表。）
#
#   那还留着下面这两条用例干什么？
#     · `test_insertIdIsNotStolenByAnotherThread`：
#       钉住「**每个线程拿到自己的 id**」这个**对外契约**。
#       当前实现靠「每线程自己的写游标」满足它；
#       谁要是把 `insertID()` 改成读某个共享变量/连接级全局，它会**立刻红**。
#       它也是「换个 Python 版本后 lastrowid 变回连接级」的探测器。
#     · `test_insertIdNotClobberedByNonInsertWrite`：
#       钉住「非 INSERT 的写不影响『上一次插入』」——
#       这是 update 链路的隐含前提（拿 id 去 update 之间可能夹着别的写）。
#
#   ⚠️ 用栅栏（Event）而不是靠概率：A 插完 -> 放 B 插 -> 等 B 插完 -> A 才取 id。
#      这样「读共享状态」的实现**必然**红，而不是"跑一百次红一次"。

def _makeTagTable(sqliteCommon):
    """建一张带自增主键的小表（`pb_*` 之外的表，不参与生成层校验）。"""
    db = sqliteCommon.dbHandle()
    assert db.executeWrite("DROP TABLE IF EXISTS t_insid") >= 0
    assert db.executeWrite(
        "CREATE TABLE t_insid (id_ INTEGER PRIMARY KEY AUTOINCREMENT,"
        " tag TEXT)") >= 0


def _idOfTag(handle, tag):
    """读回 tag 对应的自增 id。

    ⚠️ 这里**不能用 `queryCommon`**：它有表/列白名单（防注入），
       而 `t_insid` / `id_` / `tag` 不在生成层的 schema 里 —— 会被直接拒掉
       （报「SQL 里出现未知标识符」）。校验是好事，但测试自己的临时表
       本来就不该进白名单。所以走底层 handle：
       反正这条用例要验证的**就是** `sqliteHandle` 自己。
    """
    from common import sqliteHandle as sqliteHandle

    code = handle.executeRead("SELECT id_ AS rowNum FROM t_insid WHERE tag = %s",
                             (tag,))
    assert code == sqliteHandle.RET_HAS_ROWSET, handle.lastErrMsg
    return int(handle.fetchValue(0) or 0)


def test_insertIdIsNotStolenByAnotherThread(lib):
    """A 插完、B 再插、A 才取 id -> A 必须拿到**自己的** id（特征化钉子）。"""
    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    _makeTagTable(sqliteCommon)

    aDone = threading.Event()          # A 已经插完
    bDone = threading.Event()          # B 已经插完
    seen = {}

    def threadA():
        handle.executeWrite("INSERT INTO t_insid (tag) VALUES (%s)", ("A",))
        aDone.set()
        assert bDone.wait(timeout=10), "B 没插进来，栅栏失效"
        # 此刻「最后插入的行」在连接上已经是 B 的（B 后插）。
        # 本线程的 insertID() 仍必须给出 A 自己的 id。
        seen["got"] = int(handle.insertID() or 0)
        seen["want"] = _idOfTag(handle, "A")
        seen["bId"] = _idOfTag(handle, "B")

    def threadB():
        assert aDone.wait(timeout=10), "A 没插进来，栅栏失效"
        handle.executeWrite("INSERT INTO t_insid (tag) VALUES (%s)", ("B",))
        bDone.set()

    ta = threading.Thread(target=threadA, name="insA")
    tb = threading.Thread(target=threadB, name="insB")
    ta.start(); tb.start()
    ta.join(timeout=20); tb.join(timeout=20)

    assert seen, "A 线程没跑完"
    assert seen["want"] and seen["bId"] and seen["want"] != seen["bId"], seen
    assert seen["got"] == seen["want"], (
        "insertID() 拿到了**别人的** id：got=%s（应是 A 自己的 %s，"
        "B 的是 %s）—— 说明它读的不是本线程的写游标"
        % (seen["got"], seen["want"], seen["bId"]))


def test_insertIdNotClobberedByNonInsertWrite(lib):
    """非 INSERT 的写不许影响「上一次插入」的 id（特征化钉子）。

    ⚠️ 这是 update 链路的隐含前提：**拿 id 去 update 之前，
       中间很可能夹着别的写**（写日志、刷计数、拉关联）。
       如果那些写会把「上一次插入」冲掉，调用方就会拿 0 去 update ——
       影响 0 行、不报错、数据静默没改。
    """
    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    _makeTagTable(sqliteCommon)

    handle.executeWrite("INSERT INTO t_insid (tag) VALUES (%s)", ("X",))
    first = int(handle.insertID() or 0)
    assert first > 0
    assert first == _idOfTag(handle, "X")

    # 非 INSERT 的写：夹在「插入」与「取值」之间
    handle.executeWrite("UPDATE t_insid SET tag = %s WHERE id_ = %s", ("X2", first))
    handle.executeWrite("DELETE FROM t_insid WHERE 1 = 0")      # 影响 0 行的 DELETE
    assert int(handle.insertID() or 0) == first, \
        "UPDATE/DELETE 影响了「上一次插入」的 id"

    # 真的再插一行时必须更新（否则上面那条断言就是"永远返回老值"的假绿）
    handle.executeWrite("INSERT INTO t_insid (tag) VALUES (%s)", ("Y",))
    second = int(handle.insertID() or 0)
    assert second != first and second == _idOfTag(handle, "Y")


def test_insertIdIsMeaninglessAfterExecutemany(lib):
    """⚠️ **批量插入之后 `insertID()` 无意义** —— 这是真的坑，钉住它。

    实测：CPython 在 `executemany()` 之后**不更新** `cursor.lastrowid`
       （新游标上就是 None）。所以「批量插完再取 id」拿不到任何一个 id。
    本仓库目前没有任何地方那样用（`insertMany*` 返回 `(影响行数, 列名)`），
    这条用例的作用是**把坑标出来**：
      · 谁将来想在批量插入后拿 id，会在这里看到正确做法
        （`SELECT last_insert_rowid()`，或用单条 `insert_pb_*`）；
      · 万一某个 Python 版本开始更新它，这条用例会红 —— 那时
        上面的注释和 `insertID()` 的 docstring 也该一起改。
    """
    sqliteCommon = lib["sqliteCommon"]
    handle = lib["handle"]
    _makeTagTable(sqliteCommon)

    # ---- 单条插入：id 可靠 ----
    handle.executeWrite("INSERT INTO t_insid (tag) VALUES (%s)", ("a",))
    single = int(handle.insertID() or 0)
    assert single == _idOfTag(handle, "a") == 1

    # ---- 批量插入：三行都进去了，但 `insertID()` 给不出 id ----
    rtn = handle.executeWriteMany(
        "INSERT INTO t_insid (tag) VALUES (%s)", [("b",), ("c",), ("d",)])
    assert rtn == 3, rtn
    assert _idOfTag(handle, "d") == 4, "批量插入的行确实在库里"
    assert handle.insertID() is None, (
        "CPython 在 executemany() 之后**不更新** cursor.lastrowid，"
        "所以这里应当是 None（= 本函数对批量插入无意义）。"
        "如果它不再是 None，说明 CPython 行为变了 —— 请同时更新 "
        "sqliteHandle.insertID() 的 docstring 里那张实测表。")

    # ---- 再单条插入一次，必须立刻恢复可靠 ----
    handle.executeWrite("INSERT INTO t_insid (tag) VALUES (%s)", ("e",))
    assert int(handle.insertID() or 0) == _idOfTag(handle, "e") == 5
