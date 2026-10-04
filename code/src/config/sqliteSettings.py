#! /usr/bin/env python3
#encoding: utf-8

#Filename: sqliteSettings.py
#Description: photo-browser SQLite 侧配置 —— PRAGMA 常量 + 库文件装配入口
#
# 本步（步骤 1）范围：
#   ✅ PRAGMA 常量列表
#   ✅ sqlite 文件（主库 + -wal / -shm 边车）路径装配
#   ❌ 建库 / 连库/ 生成器 / 业务 CRUD  → 全部留到步骤 2
#
# 纪律：
#   1. **本模块 import 时绝不连库、绝不建文件**（硬约束：禁止连接数据库、禁止建表）；
#   2. PRAGMA 读写两个连接都要执行同一套（DR-3），故只维护一份常量列表；
#   3. 不建物理外键（数据库设计.md D-2），foreign_keys=ON 只是防误用兜底。

import os
import sys

# 让本文件在任意 cwd / 直接 `python config\sqliteSettings.py` 执行时都能 import 到兄弟包
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import paths as paths  # noqa: E402

_VERSION = "20261004"


# ============================================================
# 一、库文件名
# ============================================================

# SQLite 主库文件名（位于 <PHOTO_ROOT>\db\ 下）
DB_FILE_NAME: str = "photolib.db"

# WAL 模式边车文件后缀。WAL 关闭/合并后这两份文件会自动消失
DB_WAL_SUFFIX: str = "-wal"
DB_SHM_SUFFIX: str = "-shm"


# ============================================================
# 二、PRAGMA 常量（读写连接通用，见开发计划 §6.2）
# ============================================================

# WAL：写不阻塞读 —— 扫描时前端还能正常浏览
PRAGMA_JOURNAL_MODE: str = "WAL"
# SQLite 默认关闭外键，这里显式打开（业务层不建物理外键，仅作兜底）
PRAGMA_FOREIGN_KEYS: str = "ON"
# NORMAL：WAL 下的安全/性能折中
PRAGMA_SYNCHRONOUS: str = "NORMAL"
# 遇锁等待 5000ms 而不是直接抛 database is locked
PRAGMA_BUSY_TIMEOUT: int = 5000
# 临时表/临时索引放内存
PRAGMA_TEMP_STORE: str = "MEMORY"
# 负数 = KB 单位，-64000 ≈ 64MB 页缓存
PRAGMA_CACHE_SIZE: int = -64000

# 有序列表：PRAGMA_LIST 既是文档也是执行顺序（sqliteHandle 逐条 execute）
PRAGMA_LIST: tuple = (
    ("journal_mode", PRAGMA_JOURNAL_MODE),
    ("foreign_keys", PRAGMA_FOREIGN_KEYS),
    ("synchronous", PRAGMA_SYNCHRONOUS),
    ("busy_timeout", PRAGMA_BUSY_TIMEOUT),
    ("temp_store", PRAGMA_TEMP_STORE),
    ("cache_size", PRAGMA_CACHE_SIZE),
)

# 占位符：.txt / 生成器里统一写 %s，由 sqliteHandle 内部转成 sqlite3 的 ?
# （防注入，见数据库设计.md §1.2）
SQL_PLACEHOLDER: str = "%s"
SQLITE_PLACEHOLDER: str = "?"


def pragmaStatements() -> list:
    """把 PRAGMA_LIST 渲染成可直接 execute 的 SQL 语句列表。

    步骤 2 的 sqliteHandle 在建连后逐条执行；本步只产出字符串，不连库。
    """
    return ["PRAGMA %s = %s;" % (key, val) for key, val in PRAGMA_LIST]


def pragmaDict() -> dict:
    """PRAGMA 常量字典形式（便于断言/日志打印）"""
    return dict(PRAGMA_LIST)


# ============================================================
# 三、sqlite 文件装配（只算路径，不建文件、不连库）
# ============================================================

class SqliteFiles:
    """sqlite 文件装配结果（纯路径计算，构造过程零 IO 副作用）"""

    __slots__ = ("dbFile", "walFile", "shmFile", "dbDir")

    def __init__(self, dbFile: str):
        self.dbFile: str = dbFile
        self.walFile: str = dbFile + DB_WAL_SUFFIX
        self.shmFile: str = dbFile + DB_SHM_SUFFIX
        self.dbDir: str = os.path.dirname(dbFile)

    def exists(self) -> bool:
        """主库文件是否已存在（本步不创建，仅供判断「是否首次启动」）"""
        return os.path.isfile(self.dbFile)

    def all(self) -> list:
        return [self.dbFile, self.walFile, self.shmFile]

    def asDict(self) -> dict:
        return {
            "dbFile": self.dbFile,
            "walFile": self.walFile,
            "shmFile": self.shmFile,
            "dbDir": self.dbDir,
        }

    def __repr__(self) -> str:
        return "SqliteFiles(dbFile=%r)" % self.dbFile


def assembleDbFile(dbFile: str = None) -> SqliteFiles:
    """装配 sqlite 主库 / -wal / -shm 三个文件路径。

    参数
    ----
    dbFile : str | None
        为空时取 paths.db_file()（即 <PHOTO_ROOT>\\db\\photolib.db）。

    返回
    ----
    SqliteFiles

    ⚠️ 本函数**只做路径推导**：不 mkdir、不 touch、不 connect。
       目录创建统一走 paths.ensure_dirs()，建库走步骤 2 的 tools/build_db.py。
    """
    if dbFile is None or str(dbFile).strip() == "":
        dbFile = paths.db_file()
    return SqliteFiles(os.path.abspath(os.path.normpath(str(dbFile))))


# ============================================================
# 四、建库入口（步骤 2 实现，此处仅留签名）
# ============================================================

def buildDatabase(dbFile: str = None, createIndexes: bool = True) -> SqliteFiles:
    """建库：生成 sqliteCommon → 逐表 CREATE TABLE + CREATE INDEX。

    ⚠️ **步骤 2 实现**。本步故意留空并显式抛错，避免误以为已经能建库。
    实现要点（见 开发计划 步骤 2 关键约束）：
      - 类型映射顺序：`INT AUTO_INCREMENT PRIMARY KEY` 规则**必须排在** `BIGINT` 之前；
      - 读写连接都执行 pragmaStatements()；
      - chkTableExist 走 sqlite_master，重复执行幂等；
      - 索引由生成器在建表函数里以 CREATE INDEX idx_<表>_<字段> 输出（DR-6）。
    """
    files = assembleDbFile(dbFile)
    raise NotImplementedError(
        "buildDatabase 属步骤 2 交付，本步（步骤 1）只做配置与路径骨架；"
        "当前解析出的库文件为: %s" % files.dbFile
    )


if __name__ == "__main__":
    print("--- PRAGMA ---")
    for sql in pragmaStatements():
        print("  ", sql)
    print("--- sqlite files ---")
    f = assembleDbFile()
    print("   db  =", f.dbFile)
    print("   wal =", f.walFile)
    print("   shm =", f.shmFile)
    print("   exists =", f.exists())
