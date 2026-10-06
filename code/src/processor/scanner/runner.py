#! /usr/bin/env python3
#encoding: utf-8

#Filename: runner.py
#Description: photo-browser 扫描器·编排层 —— 增量判定 + 批量 upsert + 进度 + 批次限流 + 断点续扫
#
# 职责（开发计划 §3.1 扫描链路第 3~7 步）
# ------------------------------------------
#   1. 增量三路判定（decide()，**纯函数**，不碰库，可单测）
#   2. 批量 upsert：executemany，每 500 条一个事务（COMMIT_ROWS_PER_TXN）
#   3. 进度写 pb_scan_job：processed/added/skipped/duplicate/pending/lastCursor
#   4. **批次限流**：累计处理到 batchSize（默认 100）即 PAUSED 停下，
#      等「继续下一批」，从 lastCursor 续扫
#   5. 缺失判定：库中存在但磁盘找不到 -> isMissing=1，**绝不删记录**
#      （可能只是移动硬盘没插）
#
# 单写入者（开发计划 §3.3，写错就database is locked）
# ---------------------------------------------------
#   本模块是**主进程里的单线程**写库方：遍历 + hash + 批量写，全部在主进程串行做。
#   CPU 密集的读图/检测/提特征（步骤 5）才会放子进程，且**子进程绝对不能连数据库**，
#   结果经 Queue 回主进程由本模块落库。
#   本步（步骤 3）刻意只用单进程：hash 是 IO 密集、EXIF 解析只读文件头，
#   瓶颈在磁盘而不是 CPU，进程池此时只会白搭上跨进程传图的代价。
#
# photo 目录绝对只读
# ------------------
#   本模块对 photo 只做 os.scandir / os.stat / open(...,"rb")。
#   **不写、不删、不改名、不 touch**。改路径这种动作一律留给用户确认（步骤 9）。
#
# 五路判定（开发计划 §3.1 表，逐条对应）
# -------------------------------------
#   relPathHash 未命中                -> 新增（scanState=0）
#   relPathHash 命中且 fileHash 相同   -> 跳过（幂等，**不写库**）
#   relPathHash 命中但 fileHash 不同   -> 更新元数据 + 人脸需重提取（faceCount=0）
#   fileHash 命中但 relPathHash 未命中 -> 「同内容出现在新路径」，再按旧文件是否还在拆两种：
#       · 旧文件没了   = 移动/重命名：新行 isDuplicate=1 + dupOfPhotoCode=旧行，
#                       旧行 movedToPhotoCode=新行（双向可查），计入待确认；
#                       **两条记录都不自动改 relPath**
#       · 旧文件还在   = 复制：只标 isDuplicate=1，**不建移动链接、不提示移动**
#   库中存在但磁盘找不到              -> isMissing=1，**绝不删记录**

import bisect
import os
import sys
import time
import uuid

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                # noqa: E402
from common import miscCommon as misc                              # noqa: E402
from common import paths as paths                                  # noqa: E402
from config import basicSettings as basicSettings            # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.scanner import meta as meta                              # noqa: E402
from processor.scanner import walker as walker                    # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("scannerRunner", "scannerrunner.log")


# ============================================================
# 一、动作常量与轻量投影
# ============================================================

ACTION_NEW = "NEW"            # 新增
ACTION_SKIP = "SKIP"          # 未变（幂等跳过）
ACTION_UPDATE = "UPDATE"      # 内容变更
ACTION_MOVED = "MOVED"        # 同内容出现在新路径（移动/重命名 **或** 复制，由旧文件是否还在区分）

ACTION_TEXT = {
    ACTION_NEW: "新增",
    ACTION_SKIP: "未变跳过",
    ACTION_UPDATE: "内容变更",
    ACTION_MOVED: "疑似移动/重命名",
}

# 扫描器托管的元数据列：**整列替换**语义。
# 既作为 DO UPDATE 的刷新清单，也作为 forceColumns（保证这些列恒出现在 INSERT 列清单里，
# 否则「整批皆空」的列不会进 DO UPDATE，旧值会永久残留 —— 例如内容换成无 EXIF 的图之后
# shotYear 应该是 NULL，却一直留着上一版的 2023。见 sqliteCodeGenerator 的 forceColumns）。
_META_FULL_COLUMNS = (
    "fileHash", "fileSize", "mimeType", "width", "height", "orientation",
    "takenAt", "shotYear", "lat", "lon", "placeName", "cameraModel",
    "faceCount", "isDuplicate", "dupOfPhotoCode", "movedToPhotoCode", "isMissing",
    "scanState", "scannedYMDHMS",
)

# 补丁行按「要改哪几列」分桶，每桶独立走一次 upsert。
#   ("isMissing",)            —— 缺失判定/ 复位
#   ("movedToPhotoCode",)     —— 疑似移动：旧记录指向新记录
# ⚠️ 补丁行**绝不能和整列替换的行混在一个批次里**：整列替换批次带 forceColumns，
#    会把没写的列一律刷成 NULL（那正是 isMissing 补丁会踩的坑）。
_PATCH_COLUMNS = ("isMissing",)
_MOVE_COLUMNS = ("movedToPhotoCode",)


class ScanWriteError(Exception):
    """批量写库失败（已回滚）。任务置FAILED，不做任何「假装成功」的兜底。"""


class PhotoRef(object):
    """库中一条 pb_photo 的轻量投影（判定只需要这几个字段）。

    刻意不把整行读进来：3 万行 × 30 列的 dict 是几十 MB，
    而判定只需要 photoCode / relPathHash / fileHash / 三个标记位。
    """

    __slots__ = ("photoCode", "relPath", "relPathHash", "fileHash", "fileSize",
                 "isDuplicate", "dupOfPhotoCode", "movedToPhotoCode",
                 "isMissing", "isDeleted")

    def __init__(self, photoCode, relPath, relPathHash, fileHash, fileSize=0,
                 isDuplicate=0, dupOfPhotoCode=None, movedToPhotoCode=None,
                 isMissing=0, isDeleted=0):
        self.photoCode = photoCode
        self.relPath = relPath
        self.relPathHash = relPathHash
        self.fileHash = fileHash
        self.fileSize = fileSize
        self.isDuplicate = 1 if isDuplicate else 0
        self.dupOfPhotoCode = dupOfPhotoCode or None
        self.movedToPhotoCode = movedToPhotoCode or None
        self.isMissing = 1 if isMissing else 0
        self.isDeleted = 1 if isDeleted else 0

    def identityRow(self, **patch) -> dict:
        """补丁行：带齐全部 NOT NULL 身份列+ 要改的列。

        为什么补丁行也必须带 relPath/relPathHash/fileHash/fileSize
        ------------------------------------------------------
        补丁走的是 upsert（INSERT ... ON CONFLICT DO UPDATE），SQLite **先按 INSERT
        语义校验约束**，哪怕最终走 ON CONFLICT 分支也一样。pb_photo 的这四列都是
        NOT NULL，只写 (photoCode, isMissing) 会直接撞
        `NOT NULL constraint failed: pb_photo.relPath`，整批 500 行一起回滚。
        带齐之后既有 NOT NULL 约束可满足，ON CONFLICT 又只改 isMissing，两边都对。
        （真发生「行不存在」的意外插入时，写进去的也是一条身份完全正确的行。）
        """
        row = {"photoCode": self.photoCode, "relPath": self.relPath,
               "relPathHash": self.relPathHash, "fileHash": self.fileHash,
               "fileSize": self.fileSize}
        row.update(patch)
        return row

    @property
    def isUsable(self) -> bool:
        """能否作为「主图」：没被软删、文件还在、自己不是重复"""
        return not (self.isDeleted or self.isMissing or self.isDuplicate)

    def __repr__(self) -> str:
        return "PhotoRef(%s, %r)" % (self.photoCode, self.relPath)


class Decision(object):
    """增量判定的结果（纯数据）。"""

    __slots__ = ("action", "photoCode", "old", "isDuplicate", "dupOfPhotoCode",
                 "needFaceRefresh", "reason")

    def __init__(self, action, photoCode, old=None, isDuplicate=0,
                 dupOfPhotoCode=None, needFaceRefresh=False, reason=""):
        self.action = action
        self.photoCode = photoCode
        self.old = old
        self.isDuplicate = 1 if isDuplicate else 0
        self.dupOfPhotoCode = dupOfPhotoCode or None
        self.needFaceRefresh = bool(needFaceRefresh)
        self.reason = reason

    def __repr__(self) -> str:
        return ("Decision(%s, photoCode=%s, dupOf=%s, face=%s)"
                % (self.action, self.photoCode, self.dupOfPhotoCode, self.needFaceRefresh))


def newPhotoCode() -> str:
    """新照片编码（幂等键，VARCHAR(64)）。

    **刻意不用 relPathHash 派生**：relPathHash 会随 paths.normalize_relpath 的
    规则演进而变化，一旦规则变了（比如将来决定统一小写），全库 photoCode 会集体
    变脸-> 重扫变成「全删全插」，pb_face.photoCode 的引用全断。
    幂等性由 relPathHash 的 UNIQUE 索引 + 判定逻辑保证，不需要 photoCode 可推导。
    """
    return "PH_" + uuid.uuid4().hex


# ============================================================
# 二、增量判定（纯函数，单测直接调）
# ============================================================

def pickPrimary(candidates: list, byCode: dict = None):
    """从同内容（fileHash 相同）的若干记录里挑一个当「主图」。

    优先级：未软删 + 文件在 + 自己不是重复 >顺dupOfPhotoCode 跳到它指向的主图 > 第一条。
    跳链只走一跳：链被人工改乱过就以第一条兜底，绝不while 循环（防环）。
    """
    if not candidates:
        return None
    for ref in candidates:
        if ref.isUsable:
            return ref
    for ref in candidates:
        code = ref.dupOfPhotoCode
        if code and byCode:
            target = byCode.get(code)
            if target is not None:
                return target
    return candidates[0]


def decide(entry, relIndex: dict, fileIndex: dict, byCode: dict = None,
           codeFactory=None) -> Decision:
    """**增量三路判定**（开发计划 §3.1 的表，一个字不差）。

    参数
    ----
    entry     : walker.FileEntry
    relIndex  : {relPathHash: PhotoRef}   —— 路径级
    fileIndex : {fileHash: [PhotoRef, ...]} —— 内容级（一条 fileHash 可能对应多行）
    byCode    : {photoCode: PhotoRef}    —— 顺 dupOfPhotoCode 跳主图用，可为 None
    codeFactory: 生成新 photoCode 的 callable（测试里注入确定性的实现）

    返回
    ----
    Decision —— action见 ACTION_*，并带 isDuplicate / dupOfPhotoCode / needFaceRefresh

    判定表
    ------
      relPathHash 未命中                -> NEW
      relPathHash 命中 & fileHash 相同   -> SKIP
      relPathHash 命中 & fileHash 不同   -> UPDATE（人脸重提取）
      relPathHash 未命中 & fileHash 命中 -> MOVED（疑似移动 + 重复，**两条都不改路径**；
                     旧记录的 movedToPhotoCode 由 _linkMove() 单独补写）

    ⚠️ 本函数**绝不改任何状态**：索引的增删由调用方在真正落库后做。
       这样「判定」可以脱离数据库单测，也保证「判定」和「写库」不会互相污染。
    """
    if codeFactory is None:
        codeFactory = newPhotoCode

    relHit = relIndex.get(entry.relPathHash)

    # ---- 路径没命中：要么全新，要么是「老内容换了个路径」（移动/重命名/复制）----
    if relHit is None:
        primary = pickPrimary(fileIndex.get(entry.fileHash) or [], byCode)
        if primary is not None:
            return Decision(ACTION_MOVED, photoCode=codeFactory(), old=primary,
                            isDuplicate=1, dupOfPhotoCode=primary.photoCode,
                            needFaceRefresh=True,
                            reason="fileHash 与已有记录相同但路径不同：疑似移动/重命名"
                                   "（不自动改 relPath，等用户确认）")
        return Decision(ACTION_NEW, photoCode=codeFactory(), isDuplicate=0,
                        needFaceRefresh=True, reason="路径与内容都没有命中：全新")

    # ---- 路径命中：看内容变没变 ----
    if relHit.fileHash == entry.fileHash:
        return Decision(ACTION_SKIP, photoCode=relHit.photoCode, old=relHit,
                        reason="relPathHash + fileHash 双命中：未变，幂等跳过")

    # 内容变了：人脸特征必须重提取（faceCount 归0、scanState 回待扫描）
    others = [ref for ref in (fileIndex.get(entry.fileHash) or [])
              if ref.photoCode != relHit.photoCode]
    primary = pickPrimary(others, byCode)
    return Decision(ACTION_UPDATE, photoCode=relHit.photoCode, old=relHit,
                    isDuplicate=1 if primary else 0,
                    dupOfPhotoCode=primary.photoCode if primary else None,
                    needFaceRefresh=True,
                    reason="relPathHash 命中但 fileHash 变了：内容变更，重刷元数据")


# ============================================================
# 三、扫描执行器
# ============================================================

class ScanRunner(object):
    """跑「一批」扫描的執行器。单线程、单写入者。

    一次 runBatch() 的动作序列
    ------------------------
      1. listPhotoFiles()  一次廉价stat 全量收集（顺带得到 totalCount）
      2. 按 lastCursor 定位续扫起点（bisect，relPath 严格大于游标）
      3. 逐个算 fileHash -> decide() -> 攒行
      4. 攒够 500 行 -> executemany 一个事务落库
      5. 每 50 个文件写一次 pb_scan_job 进度
      6. 处理满 batchSize -> 停下（PAUSED 由调度层置位），或走完 -> 缺失判定
    """

    def __init__(self, jobCode: str, root: str = None, batchSize: int = None,
                 dbFile: str = None, ownerID: str = None, missingSweep=None,
                 stopEvent=None):
        """
        参数
        ----
        jobCode  : pb_scan_job.jobCode（必须已存在，状态流转归调度层管）
        root     : 扫描根，缺省取任务里的 rootPath
        batchSize: 本批处理上限，缺省取任务里的 batchSize（再缺省 basicSettings.BATCH_SIZE）
        dbFile   : 库文件，缺省 paths.db_file()
        ownerID  : 写入 pb_photo.ownerID
        missingSweep: 是否做缺失判定，缺省 basicSettings.SCAN_MISSING_SWEEP
        stopEvent: threading.Event，置位后本批在下一个文件前优雅收尾（不打断事务）
        """
        self.jobCode = str(jobCode)
        self.dbFile = dbFile
        sqliteCommon.dbHandle(dbFile)                # 切换/懒建全局读写句柄
        self.ownerID = ownerID or basicSettings.DEFAULT_OWNER_ID
        self.missingSweep = (basicSettings.SCAN_MISSING_SWEEP
                             if missingSweep is None else bool(missingSweep))
        self.stopEvent = stopEvent
        self.batchSize = int(batchSize) if batchSize else 0

        job = self._loadJob()
        self.jobRecID = job["recID"]
        self.root = os.path.abspath(str(root or job.get("rootPath") or ""))
        if not self.root or not os.path.isdir(self.root):
            raise FileNotFoundError("scanJob %s: 扫描根不存在或不是目录: %s"
                                    % (self.jobCode, self.root))
        if not self.batchSize:
            self.batchSize = int(job.get("batchSize") or basicSettings.BATCH_SIZE)
        if self.batchSize <= 0:
            raise ValueError("scanJob %s: batchSize 非法: %s" % (self.jobCode, self.batchSize))

        self.lastCursor = job.get("lastCursor") or ""
        self.totalCount = int(job.get("totalCount") or 0)
        self.counters = {
            "processedCount": int(job.get("processedCount") or 0),
            "addedCount": int(job.get("addedCount") or 0),
            "skippedCount": int(job.get("skippedCount") or 0),
            "duplicateCount": int(job.get("duplicateCount") or 0),
            "pendingCount": int(job.get("pendingCount") or 0),
        }
        # 本次运行（这一批）内的新增计数，报告用
        self.runCounters = {"added": 0, "skipped": 0, "updated": 0, "duplicate": 0,
                            "moved": 0, "moveLinked": 0, "copied": 0,
                            "missing": 0, "recovered": 0}

        self.relIndex = {}# relPathHash -> PhotoRef
        self.fileIndex = {}                # fileHash -> [PhotoRef]
        self.byCode = {}                   # photoCode -> PhotoRef
        self._indexLoaded = None           # 跨批复用的索引（见 ensureIndex）
        self._fullRows = []                # 整列替换的行
        self._patchBuckets = {}            # (列名...) -> [补丁行]

    # ---------- 任务与索引 ----------

    def _loadJob(self) -> dict:
        rows = sqliteCommon.query_pb_scan_job("pb_scan_job", jobCode=self.jobCode,
                                            delFlag="*")
        if not rows:
            raise ValueError("scanJob 不存在: %s" % self.jobCode)
        return rows[0]

    def _syncFromJob(self) -> dict:
        """每批开始前把内存状态重新对齐到库里的任务行（**跨批复用时的必需品**）。

        为什么每批都要对齐
        ------------------
        实例构造时读了一次 lastCursor / 计数。若实例被复用（同一轮扫描跑 1000 批，
        见 ensureIndex 的说明）而不重新对齐，第二批就会拿着**第一批的旧游标**重扫，
        进度原地打转 —— 表现为「PAUSED forever，永远扫不完」。
        库里的任务行才是唯一真相，每批对齐一次（一次主键点查，µs 级）。
        """
        job = self._loadJob()
        self.jobRecID = job["recID"]
        self.lastCursor = job.get("lastCursor") or ""
        self.totalCount = int(job.get("totalCount") or 0)
        for key in self.counters:
            self.counters[key] = int(job.get(key) or 0)
        return job

    def _shouldStop(self) -> bool:
        """「用户叫停了吗」的统一探针（给 walker 的中断回调用）。

        为什么包成一个方法而不是直接在调用点读 `self.stopEvent`
        --------------------------------------------------
          `stopEvent` 可以是 None（CLI 与单测走的就是这条路径，没有调度层）。
          散在各处写 `self.stopEvent is not None and self.stopEvent.is_set()`
          早晚会漏一处 —— 而漏掉的那一处就是「停止按钮在某个阶段失灵」。
          一个入口、一处判空，以后加新的检查点不会忘。
        """
        event = self.stopEvent
        return bool(event is not None and event.is_set())

    def ensureIndex(self, force: bool = False) -> dict:
        """确保内存索引已建；已建且未 force 时直接返回（**跨批复用**）。

        为什么必须复用（10 万行实测）
        ------------------------------
        索引加载 = 全表10 万行 -> 两个 dict，实测 2.3 s。而 batchSize=100 时
        10 万张要跑 **1000 批**；每批重建索引就是 1000 × 2.3s ≈ **38 分钟纯开销**，
        而且随库增长是 O(批数 × 库大小) = **平方级劣化**（3 万张时不明显，
        10 万张就是灾难）。复���后每批只剩「stat 遍历 + hash + 写库」，回到 O(N)。

        复用的正确性
        ------------
        · 同一轮扫描内（runUntilDone 的多批之间）库只被**自己**写，写完都 flush 过，
          内存索引与库内容一致 —— 每批的写入还会同步更新索引（_registerRef）；
        · 每批仍然**重新 stat 全树**（listPhotoFiles），所以批次之间新增/删除的文件
          照样看得见（用户要的「每批重算」指的是目录，不是索引）；
        · 缓存**只在 runUntilDone 调用期间有效**（调度层 finally 里清掉），
          跨轮重新建，避免拿到别的进程改过的过期索引。
        """
        if self._indexLoaded is not None and not force:
            return self._indexLoaded
        self._indexLoaded = self.loadIndex()
        return self._indexLoaded

    def loadIndex(self, pageSize: int = None) -> dict:
        """把全库 pb_photo 读成两个内存索引（判定全靠它，O(1) 命中）。

        **分页读**（10 万行实测，见 basicSettings.SCAN_INDEX_PAGE 注释）
        ------------------------------------------------------
        一次性 query_pb_photo 取 10 万行，峰值 157.5MB；分页取只有 3.2MB，速度一样。
        那 150MB 是「10 万个 dict 的中间态」，纯属白给 —— 索引真正需要常驻的只有
        紧凑的 PhotoRef（10 万条约 54MB）。页大小用生成层已有的 limitNum/offsetNum，
        不需要新增查询参数。

        **软删的行也必须进索引**：relPathHash 上有 UNIQUE 索引，
        软删行若被排除在索引外，同名文件再来一次就会撞唯一索引把整个 500 行事务打回。
        软删行命中时按 SKIP 处理，且落库时不碰 delFlag（生成层已把 delFlag 排除在
        DO UPDATE 之外），所以**不会被扫描器偷偷复活**。
        """
        self.relIndex = {}
        self.fileIndex = {}
        self.byCode = {}
        size = int(pageSize or basicSettings.SCAN_INDEX_PAGE)
        if size <= 0:
            size = 2000
        total = 0
        offset = 0
        while True:
            rows = sqliteCommon.query_pb_photo("pb_photo", mode="light", delFlag="*",
                                              orderBy="recID", limitNum=size,
                                              offsetNum=offset)
            if not rows:
                break
            for row in rows:
                ref = PhotoRef(
                    photoCode=row.get("photoCode") or "",
                    relPath=row.get("relPath") or "",
                    relPathHash=row.get("relPathHash") or "",
                    fileHash=row.get("fileHash") or "",
                    fileSize=int(row.get("fileSize") or 0),
                    isDuplicate=row.get("isDuplicate") or 0,
                    dupOfPhotoCode=row.get("dupOfPhotoCode"),
                    movedToPhotoCode=row.get("movedToPhotoCode"),
                    isMissing=row.get("isMissing") or 0,
                    isDeleted=(comGD.DEL_FLAG_YES
                               if str(row.get("delFlag") or comGD.DEL_FLAG_NO)
                               == comGD.DEL_FLAG_YES else 0),
                )
                if not ref.photoCode or not ref.relPathHash:
                    continue
                self.byCode[ref.photoCode] = ref
                self.relIndex[ref.relPathHash] = ref
                self.fileIndex.setdefault(ref.fileHash, []).append(ref)
            total += len(rows)
            if len(rows) < size:               # 最后一页
                break
            offset += len(rows)
        _LOG.info("loadIndex: 库内 %d 条（分页 %d）-> relIndex %d / fileIndex %d"
                  % (total, size, len(self.relIndex), len(self.fileIndex)))
        return {"photoCount": total, "relIndex": len(self.relIndex),
                "fileIndex": len(self.fileIndex), "pageSize": size}

    # ---------- 行构造 ----------

    def _metaColumns(self, info: dict) -> dict:
        """从 meta.readMeta() 的返回值里挑出入库列（**只挑白名单里的**，不多不少）"""
        return {name: info.get(name) for name in
                ("mimeType", "width", "height", "orientation", "takenAt",
                 "shotYear", "lat", "lon", "placeName", "cameraModel")}

    def _buildRow(self, entry, decision: Decision, info: dict) -> dict:
        """新增/更新行的完整字段集（键齐全，值可为 None -> 由 forceColumns 写成 NULL）"""
        now = misc.getTime()
        row = {
            "photoCode": decision.photoCode,
            "relPath": entry.relPath,                 # 磁盘原值，不规范化
            "relPathHash": entry.relPathHash,
            "fileHash": entry.fileHash,
            "fileSize": entry.fileSize,
            "isMissing": 0,
            "scanState": comGD.SCAN_STATE_PENDING,
            "scannedYMDHMS": now,
            "ownerID": self.ownerID,
            "isDuplicate": decision.isDuplicate,
            "dupOfPhotoCode": decision.dupOfPhotoCode,
        }
        row.update(self._metaColumns(info))
        if decision.needFaceRefresh:
            # 人脸特征与旧内容绑定：内容变了/是新行 -> 归零，等步骤 5 重提取
            row["faceCount"] = 0
        return row

    def _registerRef(self, entry, decision: Decision) -> PhotoRef:
        """把刚落库（或即将落库）的行挂进内存索引 —— 同一批里的重复内容要能互相看见。

        三种情形必须区别对待（早先在这里只按「有没有 old」分支，MOVED 会被当成
        UPDATE 处理，把**旧记录从索引里摘掉**，连带让第三份拷贝找不到主图）：
          ·NEW   —— 直接挂到relPathHash / fileHash 两个索引上
          · UPDATE—— 同一路径内容变了：把「自己」从旧 fileHash 桶里摘掉（否则它会
                      把自己当成同内容的重复），再挂到新 fileHash 下
          · MOVED —— 路径是新的、内容是旧的：旧记录在它自己的路径上**仍然存在**
                      （isMissing 只是本轮末尾的判定结果），索引必须原样保留
        """
        ref = PhotoRef(photoCode=decision.photoCode, relPath=entry.relPath,
                       relPathHash=entry.relPathHash, fileHash=entry.fileHash or "",
                       fileSize=entry.fileSize, isDuplicate=decision.isDuplicate,
                       dupOfPhotoCode=decision.dupOfPhotoCode, isMissing=0)
        old = decision.old
        if old is not None and decision.action == ACTION_UPDATE:
            oldRefs = self.fileIndex.get(old.fileHash) or []
            self.fileIndex[old.fileHash] = [r for r in oldRefs
                                            if r.photoCode != old.photoCode]
            self.relIndex.pop(old.relPathHash, None)
        self.relIndex[ref.relPathHash] = ref
        self.fileIndex.setdefault(ref.fileHash, []).append(ref)
        self.byCode[ref.photoCode] = ref
        return ref

    # ---------- 落库 ----------

    def _addPatch(self, row: dict, columns) -> None:
        """攒一条补丁行（按「要改哪几列」分桶，各桶的 DO UPDATE 清单不同）"""
        self._patchBuckets.setdefault(tuple(columns), []).append(row)

    def _pendingPatchCount(self) -> int:
        return sum(len(rows) for rows in self._patchBuckets.values())

    def _flush(self) -> dict:
        """把攒下的行写库。整列替换行与补丁行**分批写**，互不污染。

        事务粒度 = 一次 executemany = 500 行（COMMIT_ROWS_PER_TXN），
        由 sqliteCommon.insertManyTableGeneral 内部的 with db.transaction() 保证。
        """
        result = {"full": 0, "patch": 0}
        if self._fullRows:
            rows, self._fullRows = self._fullRows, []
            rtn, columns = sqliteCommon.insertManyTableGeneral(
                "pb_photo", rows,
                conflictColumns=("photoCode",),
                updateColumns=list(_META_FULL_COLUMNS),
                fillStandard=True,
                forceColumns=list(_META_FULL_COLUMNS))
            if rtn == -2:                      # sqliteHandle.RET_ERROR
                raise ScanWriteError("pb_photo 批量写失败（已回滚）: %s"
                                     % sqliteCommon.dbHandle().lastErrMsg)
            result["full"] = rtn if rtn and rtn > 0 else len(rows)
        for key in sorted(self._patchBuckets):
            rows = self._patchBuckets.pop(key)
            for start in range(0, len(rows), basicSettings.COMMIT_ROWS_PER_TXN):
                chunk = rows[start:start + basicSettings.COMMIT_ROWS_PER_TXN]
                rtn, _columns = sqliteCommon.insertManyTableGeneral(
                    "pb_photo", chunk,
                    conflictColumns=("photoCode",),
                    updateColumns=list(key),
                    fillStandard=True)
                if rtn == -2:
                    raise ScanWriteError("pb_photo 补丁写失败（已回滚，列=%s）: %s"
                                         % (key, sqliteCommon.dbHandle().lastErrMsg))
                result["patch"] += rtn if rtn and rtn > 0 else len(chunk)
        return result

    def writeProgress(self, lastCursor=None, totalCount=None) -> bool:
        """进度写 pb_scan_job。**只写非 None 的键**，避免把已有值抹成 NULL。"""
        data = {
            "processedCount": self.counters["processedCount"],
            "addedCount": self.counters["addedCount"],
            "skippedCount": self.counters["skippedCount"],
            "duplicateCount": self.counters["duplicateCount"],
            "pendingCount": self.counters["pendingCount"],
        }
        if lastCursor is not None:
            data["lastCursor"] = str(lastCursor)
        if totalCount is not None:
            data["totalCount"] = int(totalCount)
        rtn = sqliteCommon.update_pb_scan_job("pb_scan_job", self.jobRecID, data)
        return rtn >= 0

    # ---------- 移动/重命名的双向链接 ----------

    def _contentStillOnDisk(self, entry, decision: Decision) -> bool:
        """同内容的**其它**记录里，是否还有文件真的在磁盘上（命中即说明这份是「复制」）。

        这是区分「移动/重命名」与「复制」的唯一依据
        --------------------------------------
        两种情况在判定阶段长得一模一样：fileHash 命中、relPathHash 未命中。
        但语义完全相反：
          · 移动/重命名 —— 同内容的旧路径**全都不在了**（内容被挪到了新路径）
          · 复制       —— 同内容的旧路径**至少还有一个在**，只是多了一份
        缺了这个判断就会把「我复制了一张照片」提示成「你的照片被移动了」，
        属于会误导用户的假警报，所以宁可多几次 stat 也要分清。
        （stat 只在命中「同内容异路径」时才发生，属罕见分支。）

        判定用「集合」而不是「主图那一条」
        ----------------------------------
        一张图本来就有 3 份副本，其中一份被改名：此时另外两份还在磁盘上，
        应当判成「复制/无关」而不是「移动」。反过来，只有当同内容的所有旧记录
        都找不到文件时，才认定为「移动」—— 这个方向是**保守**的：
        宁可少提示一次移动，也不要误报移动。
        """
        for ref in self.fileIndex.get(entry.fileHash or "", ()):
            if ref.photoCode == decision.photoCode or not ref.relPath:
                continue                      # 跳过它自己
            absPath = os.path.join(self.root, ref.relPath.replace("/", os.sep))
            if os.path.isfile(absPath):
                return True
        return False

    def _linkMove(self, decision: Decision) -> bool:
        """确认为「移动/重命名」后，给**旧记录**补一个 movedToPhotoCode = 新记录的 photoCode。

        为什么要在旧记录上留这个字段（步骤 9 的 UI 靠它一眼看出「这俩是同一张图」）
        ------------------------------------------------------------------------
          新记录只有 dupOfPhotoCode（= 旧记录），但 dupOfPhotoCode 有两种含义：
          「复制」和「移动」都长这样，分不出来。加上旧记录侧的 movedToPhotoCode 后：
            旧.movedToPhotoCode == 新.photoCode 且 新.dupOfPhotoCode == 旧.photoCode
              -> 确凿的「移动/重命名」对，UI 可直接提示「确认后把旧记录的路径改成新路径」
            只有 dupOfPhotoCode（旧记录的 movedToPhotoCode 为空）
              -> 只是「复制」，不该提示移动
          **两条记录都不自动改 relPath**，改不改由用户点确认（开发计划 §3.1）。
          纯复制的情况下本方法根本不会被调用，链接天然为空。

        先到先得，不覆盖
        ----------------
        同一张图被移走后又复制出第 3、第 4 份时，那些副本都是「复制」不建链接；
        即便真出现多份「移动」（例如换过两次名），movedToPhotoCode 只有一个坑位，
        这里只允许**第一次**写入，其余记 warning —— 先出现的关系不会被悄悄抹掉。
        """
        old = decision.old
        if old is None:
            return False
        if old.movedToPhotoCode:
            _LOG.warning("_linkMove: %s 的 movedToPhotoCode 已被 %s 占用，"
                         "本次疑似移动到 %s 的关系不覆盖（可用 dupOfPhotoCode 反查）"
                         % (old.photoCode, old.movedToPhotoCode, decision.photoCode))
            return False
        self._addPatch(old.identityRow(movedToPhotoCode=decision.photoCode),
                       _MOVE_COLUMNS)
        old.movedToPhotoCode = decision.photoCode
        self.runCounters["moveLinked"] += 1
        return True

    # ---------- 缺失判定 ----------

    def sweepMissing(self, foundCount: int) -> int:
        """库中存在但磁盘找不到 -> isMissing=1；回来了 -> 复位为 0。**绝不删记录。**

        为什么是「标」而不是「删」
        ----------------------
        移动硬盘没插、目录被临时挪走、权限掉了，磁盘上就是找不到。
        直接删记录 = 把用户 3 万张照片的记录一次性抹光，且不可逆。标上就好，
        步骤 9 让用户自己判断「是真删了还是盘没插」。

        安全阀：walk 一个文件都没找到时**直接跳过**整个缺失判定。
        盘没插 = 整个根目录是空的，这时候若还照扫，3 万条记录会被集体标 missing ——
        恰好是本函数要防的那种事故。
        """
        if not self.missingSweep:
            return 0
        if foundCount <= 0 and self.byCode:
            _LOG.warning("sweepMissing: 扫描根 %s 下一个文件都没有（盘没插？），"
                         "跳过缺失判定，绝不把 %d 条记录标成缺失"
                         % (self.root, len(self.byCode)))
            return 0

        patchRows = []
        for ref in self.byCode.values():
            if ref.isDeleted:
                continue                    # 软删的行不参与缺失判定
            absPath = os.path.join(self.root, ref.relPath.replace("/", os.sep))
            exists = os.path.isfile(absPath)
            if exists and ref.isMissing:
                patchRows.append(ref.identityRow(isMissing=0))
                self.runCounters["recovered"] += 1
            elif not exists and not ref.isMissing:
                patchRows.append(ref.identityRow(isMissing=1))
                self.runCounters["missing"] += 1
        for start in range(0, len(patchRows), basicSettings.COMMIT_ROWS_PER_TXN):
            for one in patchRows[start:start + basicSettings.COMMIT_ROWS_PER_TXN]:
                self._addPatch(one, _PATCH_COLUMNS)
            self._flush()
        if patchRows:
            _LOG.info("sweepMissing: 标缺失 %d / 复位 %d"
                      % (self.runCounters["missing"], self.runCounters["recovered"]))
        return self.runCounters["missing"]

    # ---------- 主循环 ----------

    def runBatch(self) -> dict:
        """跑一批。返回本次运行的统计（调度层据此置 PAUSED / DONE / FAILED）。

        返回 dict 关键键
        --------------
          done    : bool —— 本次是否把整个根目录走完了（可以置 DONE）
          paused  : bool —— 因batchSize 上限而停下（应置 PAUSED 等「继续」）
          stopped : bool —— 因收到停止信号而停下（本批**一张都没处理**）
          counts  : dict —— 本批的 added/skipped/updated/duplicate/moved/missing
          cursor  : 本批结束时的 lastCursor

        ⚠️ 「停止」的两级语义（步骤 9 接FastAPI 时补上的）
          ① **批内停止**：逐文件循环里检查 stopEvent -> `stopped=True`，
             已处理的文件照常落库并推进游标（不浪费）。
          ② **遍历中止**：全树 stat 阶段也接上了 stopEvent
             （`walker.listPhotoFiles(shouldStop=...)`）。大库/慢盘上这一段
             本身就要 2~20s，不接的话「停止」的响应下限就是这段耗时。
             中止时**本批一张都不处理、游标与计数一律不动**，
             下次 `resume` 会重走一遍遍历 —— 宁可多花一次遍历时间，
             也不能推进一个「其实没扫完」的游标（那会永久漏掉一段文件）。
        """
        startTime = time.time()
        self._syncFromJob()# 跨批复用时必须重新对齐游标/计数（见 _syncFromJob）
        self.runCounters = {"added": 0, "skipped": 0, "updated": 0, "duplicate": 0,
                            "moved": 0, "moveLinked": 0, "copied": 0,
                            "missing": 0, "recovered": 0,
                            # 停止信号相关（本批是否因它而空跑，见函数头②）
                            "stopped": 0, "walkAborted": 0}
        self.ensureIndex()

        # 一次廉价stat 全量收集：既是续扫的定位依据，也顺带修正 totalCount
        walkStart = time.time()
        try:
            items = walker.listPhotoFiles(self.root, shouldStop=self._shouldStop)
        except walker.WalkAborted as e:
            # 遍历被叫停：游标/计数/totalCount **一律不动**（见函数头②）
            _LOG.info("runBatch: 遍历被中止（%s），本批不处理任何文件", e)
            self.runCounters["stopped"] = 1
            self.runCounters["walkAborted"] = 1
            # ⚠️ 键集合与正常返回**逐一对齐**（多一个 `processed=0`、少一个就
            #    会让调用方 KeyError）。调用方不该去记"哪条路径有哪些键"。
            return {"jobCode": self.jobCode, "root": self.root,
                    "processed": 0, "totalCount": int(self.totalCount or 0),
                    "startIndex": 0, "cursor": self.lastCursor,
                    "done": False, "paused": False, "stopped": True,
                    "walkAborted": True, "missingCount": 0, "walkCost": 0.0,
                    "counts": dict(self.runCounters),
                    "jobCounters": dict(self.counters),
                    "elapsed": round(time.time() - startTime, 3)}
        walkCost = time.time() - walkStart
        totalCount = len(items)
        if totalCount != self.totalCount:
            _LOG.info("runBatch: totalCount %d -> %d（目录有变动，以实际为准）"
                      % (self.totalCount, totalCount))
            self.totalCount = totalCount
        # 遍历耗时单独打点：10 万张库、batchSize=100 时这一行是 1000 次，
        # 每次 stat 全树（2~20s 视磁盘而定）——是「每批重算」策略的主要成本来源，
        # 现场要调策略时先看这行，别靠猜。
        _LOG.info("runBatch: 遍历 %d 个文件耗时 %.2fs（每批重算，10 万张库约 %d 批/轮）"
                  % (totalCount, walkCost,
                     (totalCount // self.batchSize + 1) if self.batchSize else 0))

        relPaths = [item[0] for item in items]
        startIndex = 0
        if self.lastCursor:
            # relPath 已排序 -> 「严格大于游标」就是 bisect_right
            startIndex = bisect.bisect_right(relPaths, self.lastCursor)
            _LOG.info("runBatch: 从 lastCursor 续扫: %r -> 第 %d/%d 个文件"
                      % (self.lastCursor, startIndex, totalCount))

        processed = 0
        walkDone = True
        cursor = self.lastCursor
        stopped = False

        for index in range(startIndex, totalCount):
            relPath, absPath = items[index]
            # 批内停止（函数头①）：**已处理的文件照常落库、游标照常推进**，
            # 下一个 resume 从游标往后接着走 —— 已经干完的活不白费。
            if self._shouldStop():
                walkDone = False
                stopped = True
                break
            entry = walker.makeEntry(absPath, relPath=relPath, hashContent=True)
            if entry is None:
                # 遍历之后、算hash 之前被删/改名 —— 跳这一条，不中断整轮
                continue

            decision = decide(entry, self.relIndex, self.fileIndex, self.byCode)
            action = decision.action

            if action == ACTION_SKIP:
                self.counters["skippedCount"] += 1
                self.runCounters["skipped"] += 1
                # 文件回来了（上次标过缺失）-> 顺手复位。正常幂等重扫**一行都不写**。
                if decision.old is not None and decision.old.isMissing:
                    self._addPatch(decision.old.identityRow(isMissing=0),
                                   _PATCH_COLUMNS)
                    self.runCounters["recovered"] += 1
            else:
                info = meta.readMeta(entry.absPath, fileSize=entry.fileSize,
                                     mtime=entry.mtime)
                self._fullRows.append(self._buildRow(entry, decision, info))
                self._registerRef(entry, decision)
                if action == ACTION_NEW:
                    self.counters["addedCount"] += 1
                    self.runCounters["added"] += 1
                elif action == ACTION_MOVED:
                    # 老内容出现在新路径：新增一行并标为重复，**旧行路径一个字都不改**。
                    # 再按「同内容的旧路径是否都还在」把它拆成两种语义：
                    #   都没了  -> 移动/重命名（建 movedToPhotoCode 链接 + 计入待确认）
                    #   还有在  -> 复制（只是多一份相同内容，不该提示用户「照片被移动」）
                    self.counters["addedCount"] += 1
                    self.counters["duplicateCount"] += 1
                    self.runCounters["added"] += 1
                    self.runCounters["duplicate"] += 1
                    if self._contentStillOnDisk(entry, decision):
                        self.runCounters["copied"] += 1
                    else:
                        self.counters["pendingCount"] += 1
                        self.runCounters["moved"] += 1
                        self._linkMove(decision)
                else:      # ACTION_UPDATE
                    self.runCounters["updated"] += 1

            self.counters["processedCount"] += 1
            cursor = entry.relPath
            processed += 1

            if (len(self._fullRows) >= basicSettings.COMMIT_ROWS_PER_TXN
                    or self._pendingPatchCount() >= basicSettings.COMMIT_ROWS_PER_TXN):
                self._flush()
            if processed % basicSettings.PROGRESS_EVERY == 0:
                self._flush()
                self.writeProgress(lastCursor=cursor, totalCount=totalCount)

            if processed >= self.batchSize:
                # 批次限流：处理够数就收手。是否真的走完看 index（列表已全量在手，不用多探一个文件）
                walkDone = (index == totalCount - 1)
                break

        self._flush()
        self.writeProgress(lastCursor=cursor, totalCount=totalCount)

        missingCount = 0
        if walkDone and not stopped:
            missingCount = self.sweepMissing(foundCount=totalCount)
            self.writeProgress(lastCursor=cursor, totalCount=totalCount)

        result = {
            "jobCode": self.jobCode,
            "root": self.root,
            "processed": processed,
            "totalCount": totalCount,
            "startIndex": startIndex,
            "cursor": cursor,
            "done": bool(walkDone and not stopped),
            "paused": bool(not walkDone and not stopped),
            "stopped": bool(stopped),
            # ⚠️ 中止路径与正常路径**必须返回同一个键集合**：
            #    少了这个键，调用方写 result["walkAborted"] 会在正常批上 KeyError，
            #    于是"只在异常路径才出现"的字段迟早被漏判（.get() 默认值会掩盖它）。
            "walkAborted": False,
            "missingCount": missingCount,
            "walkCost": round(walkCost, 3),
            "counts": dict(self.runCounters),
            "jobCounters": dict(self.counters),
            "elapsed": round(time.time() - startTime, 3),
        }
        _LOG.info("runBatch: %s 本批 %d 张 / 共 %d 张，用时 %.2fs，计数=%s"
                  % (self.jobCode, processed, totalCount, result["elapsed"],
                     result["counts"]))
        return result


# ============================================================
# 四、便捷入口
# ============================================================

def makeJobCode() -> str:
    """新任务编码：SJ_<yyyymmddHHMMSS>_<6 位随机>（VARCHAR(64) 绰绰有余）"""
    return "%s_%s_%s" % (basicSettings.SCAN_JOB_CODE_PREFIX, misc.getTime(),
                         uuid.uuid4().hex[:6])


if __name__ == "__main__":
    print("runner _VERSION :", _VERSION)
    print("动作常量:", {k: ACTION_TEXT[k] for k in (ACTION_NEW, ACTION_SKIP,
                                                  ACTION_UPDATE, ACTION_MOVED)})
    print("整列替换列:", _META_FULL_COLUMNS)
    print("事务粒度:", basicSettings.COMMIT_ROWS_PER_TXN, "行")
    print("批次限流:", basicSettings.BATCH_SIZE, "张")
    print("扫描根  :", paths.photo_dir())
