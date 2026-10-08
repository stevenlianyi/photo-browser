#! /usr/bin/env python3
#encoding: utf-8
# Filename: faceScheduler.py
# Description: photo-browser 人脸识别任务调度（步骤 5 的编排层 / 步骤 9 接入 Web）
#
# 为什么这个文件必须存在（它修的是什么问题）
# ----------------------------------------------
#   扫描任务（步骤 3）只写 pb_photo，而人脸特征提取（步骤 5）**只走 CLI**
#   （`tools/run_faces.py`）。两条链路互不调用，Web 端也没有任何入口——
#   于是「扫了 1000 张照片，待确认 0 张」是**必然**的，不是数据问题：
#   待确认队列数的是 `pb_face` 表里 `personCode IS NULL` 的行，
#   而那 1000 张照片的 scanState 一直停在 0，pb_face 里一行都没有。
#   `faceStore` 文件头那句「由 tools/run_faces.py 或**后续步骤 6 的编排层**驱动」
#   里的"编排层"就是本文件——那个承诺在此兑现。
#
# 继承 ScanScheduler 而不是各写一套
# --------------------------------
#   两者共用 pb_scan_job 表、共用 JOB_* 状态机、断点续跑语义与进度口径逐个对得上
#   （见 globalDefinition 的「二之二 任务类型」）。更重要的是**单写入者互斥**：
#   `ScanScheduler._ACTIVE_STATE` 是**模块级**的，继承过来的 `_acquire` 抢的
#   就是同一把门闩 —— 扫描与人脸提取天然互斥，绝不需要"两边各查一次对方在不在跑"
#   那种会在并发下漏判的写法。
#
# 取数游标为什么是 recID 而不是 lastCursor 式的路径游标
# --------------------------------------------------
#   扫描那边用 relPath 游标，因为扫描要按目录顺序走。这里是按 recID 顺序取
#   `scanState=0` 的行，**单调递增**即可：处理过的行 scanState 变 1，
#   天然退出待提取集合；万一某张提取失败（文件损坏）它仍是 0，
#   游标必须**跨过它**，否则每批都卡在同一张、任务永远跑不完。
#   （扫描那边不会遇到这个问题：它按游标续扫，失败也会前进。）
#
# 进度口径（与扫描任务的同名字段语义不同，**别混用**）
#   totalCount     建任务时 scanState=0 的照片张数
#   processedCount 已处理张数
#   addedCount     **检出人脸**的照片张数（≠ 人脸条数：一张合影 3 张脸算 1）
#   skippedCount   磁盘上已不存在而跳过的张数
#   pendingCount   本次**新写入 pb_face 的行数** = 新增待确认量（人脸条数）
#   duplicateCount 未使用，恒 0

import os
import sys
import threading
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                    # noqa: E402
from common import miscCommon as misc                                  # noqa: E402
from common import paths as paths                                      # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.face import faceStore as faceStore                    # noqa: E402
from schedule import scanScheduler as scanSched                   # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("faceScheduler", "facescheduler.log")

#: pb_scan_job.label 存重提取标记（其余任务留空）。
#: 为什么不存 memo：memo 是给人看的自由文本，参数塞进去迟早被覆写；
#: 而"这一轮是重提取"直接决定要不要删旧人脸行，写错=静默产生孤儿行。
_REPLACE_LABEL: str = "REPLACE"


def parseCursor(raw) -> int:
    """lastCursor（"recID:12345"）-> recID；空/非法一律 0（从头取）"""
    text = str(raw or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def formatCursor(recID) -> str:
    return "recID:%d" % int(recID or 0)


def countPendingPhotos(dbFile: str = None) -> int:
    """当前「待提取人脸」的照片张数（scanState=0 且未软删）。

    ⚠️ 判据是 `scanState = 0` 而不是 IS NULL —— 该列是 NOT NULL DEFAULT 0，
    扫描器插入时压根不写它。写成 IS NULL 会**永远数出 0 张**，
    于是前端显示"没有待识别的照片"而库里明明有 2000 张。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    n = sqliteCommon.countWhereGeneral(
        "pb_photo", "scanState = %s AND delFlag = %s",
        (comGD.SCAN_STATE_PENDING, comGD.DEL_FLAG_NO))
    return max(0, int(n))


#: 起任务前必须能 import 的模块 -> 缺了会怎样（写给用户看的话）
_REQUIRED_MODULES: tuple = (
    ("onnxruntime", "推理后端（跑 SCRFD 检测 + ArcFace 特征）"),
    ("cv2", "图像解码与后处理（insightface 依赖 opencv）"),
)

#: insightface 缺失不算致命：engine.py 有**裸 onnxruntime 后端**可降级
#   （见 engine.FaceEngine.__init__ 的 chosen=="insightface" 才 raise）。
#   只在完全不可用时才是问题，所以它不进 _REQUIRED_MODULES。
_OPTIONAL_MODULES: tuple = (
    ("insightface", "可降级到裸 onnxruntime，但姿态(yaw)过滤会失效、人脸会偏多"),
)


def preflight() -> dict:
    """**起任务前的依赖自检**（fail fast）。

    为什么必须做，而不是等子进程报错
    -------------------------------
      子进程是在 `multiprocessing` 里 spawn 出来的，缺依赖时它们在
      `_queueWorkerLoop` 的第一行就 `ModuleNotFoundError` 死掉，主进程只看到
      「结果队列一条数据都没有」。若不显式处理，结局是：
      任务 DONE、进度 100%、pendingCount 增量 0 —— **和「这批照片没人脸」
      在界面上完全一样**。本项目已经在这个坑上浪费过一整轮扫描：
      用户的观感是「扫了 1000 张，待确认 0 张，明显不对」。

      引擎的 `describe()` 能报 backendError，但它只在**子进程里**有值，
      主进程拿不到 —— 所以只能在这里（主进程、起任务前）自查。

    返回 { ok, missing: [(模块, 用途)], optional: [...], hint }
    """
    import importlib.util

    missing = []
    for name, why in _REQUIRED_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((name, why))
    optional = []
    for name, why in _OPTIONAL_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            optional.append((name, why))
    out = {"ok": not missing, "missing": missing, "optional": optional}
    if missing:
        out["hint"] = ("缺少人脸识别依赖：%s。请用**项目的 venv** 执行"
                       " `code\\.venv\\Scripts\\pip install -i https://pypi.org/simple -r requirements.txt`"
                       "（直接用系统 python 跑会缺 onnxruntime/cv2，"
                       "表现为「任务成功但一张脸都没检出」）"
                       % "、".join("%s（%s）" % (n, w) for n, w in missing))
    return out


class FaceScheduler(scanSched.ScanScheduler):
    """人脸特征提取任务调度器。

    与父类的差异只有三处（其余状态机/幂等/互斥/进度全部继承）：
      1. createJob   —— 固定 jobType=1、jobCode 用 FJ_ 前缀
      2. runBatch    —— 把「遍历文件+算哈希」换成「取 scanState=0 的行 + 提特征」
      3. ensureSchema—— 额外要求 pb_face 在
    """

    def __init__(self, dbFile: str = None, ownerID: str = None, verbose: bool = True):
        self._lock = threading.Lock()          # 只护 _engineOptions，与父类无关
        self._engineOptions = {}
        super(FaceScheduler, self).__init__(dbFile=dbFile, ownerID=ownerID,
                                            verbose=verbose)

    # ========================================================
    # 一、库与任务
    # ========================================================

    def ensureSchema(self) -> bool:
        """人脸识别额外依赖 pb_face（父类只要求 pb_photo / pb_scan_job）。"""
        super(FaceScheduler, self).ensureSchema()
        if not sqliteCommon.chkTableExist("pb_face"):
            raise RuntimeError(
                "数据表缺失: pb_face；请先执行 python code\\src\\tools\\build_db.py --verify")
        return True

    def setEngineOptions(self, **kwargs) -> dict:
        """进程内覆盖引擎参数（模型名 / 阈值）。

        ⚠️ 只覆盖**进程内内存态**：服务重启即失效，不会静默改配置文件。
        界面上必须把这件事说出来，否则用户以为重启后就变了。
        不落库的理由与 basicSettings.setThresholdOverride 相同 ——
        用户调的是参数，而本项目绑 127.0.0.1 无鉴权，
        让 HTTP 请求具备改写源文件的能力不该由设计买单。
        """
        allowed = ("workers", "modelPack", "minDetScore", "minFaceEdge",
                   "maxYaw", "detSize", "wantCrop", "replaceFaces")
        clean = dict((k, v) for k, v in kwargs.items()
                     if k in allowed and v is not None)
        with self._lock:
            self._engineOptions.update(clean)
            return dict(self._engineOptions)

    def engineOptions(self) -> dict:
        with self._lock:
            return dict(self._engineOptions)

    def createJob(self, root: str = None, batchSize: int = None, jobCode: str = None,
                  countTotal: bool = True, replaceFaces: bool = False) -> dict:
        """建一个人脸识别任务（jobCode 幂等；jobType 固定为 1）。

        replaceFaces : True = 重提取（先按 photoCode 删旧人脸行再插）。
                       换检测模型或修了提取 bug 时用；日常增量跑**不要开** ——
                       它会删掉已落库的人脸行，而 pb_face 的 upsert
                       刻意不刷 isConfirmed，于是刚确认的结果会连带丢失。
        """
        self.ensureSchema()
        if jobCode:
            exist = self.getJob(jobCode)
            if exist is not None:
                _LOG.info("createFaceJob: jobCode 已存在，幂等返回: %s（状态 %s）",
                          (jobCode, exist.get("jobStatus")))
                exist["created"] = False
                return exist

        photoRoot = os.path.abspath(str(root or paths.photo_dir()))
        if not os.path.isdir(photoRoot):
            raise FileNotFoundError("createJob: 照片根不存在或不是目录: %s" % photoRoot)
        size = int(batchSize or basicSettings.BATCH_SIZE)
        if size <= 0:
            raise ValueError("createJob: batchSize 非法: %s" % size)

        options = self.engineOptions()
        # 显式 replaceFaces 参数优先于进程内覆盖（后者是 UI 上一次性勾的开关）
        replace = bool(replaceFaces or options.get("replaceFaces"))
        newCode = jobCode or comGD.makeJobCode(comGD.JOB_TYPE_FACE)
        now = misc.getTime()
        dataSet = {
            "jobCode": newCode,
            "rootPath": photoRoot,
            "batchSize": size,
            "batchIndex": 0,
            "totalCount": 0,
            "jobStatus": comGD.JOB_IDLE,
            "jobType": comGD.JOB_TYPE_FACE,
            "label": _REPLACE_LABEL if replace else None,
            "regID": self.ownerID,
            "regYMDHMS": now,
        }
        if countTotal:
            dataSet["totalCount"] = countPendingPhotos(self.dbFile)

        recID = sqliteCommon.insert_pb_scan_job("pb_scan_job", dataSet)
        if recID <= 0:
            raise RuntimeError("createJob: 写入 pb_scan_job 失败: %s"
                               % sqliteCommon.dbHandle().lastErrMsg)
        _LOG.info("createFaceJob: %s root=%s batchSize=%d totalCount=%d replace=%s",
                  newCode, photoRoot, size, dataSet["totalCount"], replace)
        job = self.getJob(newCode)
        job["created"] = True
        return job

    def isReplaceJob(self, job: dict) -> bool:
        return str((job or {}).get("label") or "") == _REPLACE_LABEL

    def listJobs(self, limit: int = 20) -> list:
        """最近的人脸识别任务（**只列 jobType=1**）。

        ⚠️ 少了 jobType 过滤就会把扫描任务也列进来 —— 而两者的
        pendingCount 口径完全不同（移动张数 vs 人脸条数），
        混列出来的数字会让人以为扫描没做完。
        """
        return sqliteCommon.query_pb_scan_job(
            "pb_scan_job", delFlag="*", jobType=str(comGD.JOB_TYPE_FACE),
            orderBy="recID", descFlag=True, limitNum=int(limit))

    # ========================================================
    # 二、执行
    # ========================================================

    def _takeBatch(self, job: dict, batchSize: int = None,
                   onlyPending: bool = True) -> tuple:
        """取本批要处理的 pb_photo 行。返回 (rows, skippedBefore)。

        多取一些再过滤游标与磁盘缺失，保证凑不满时能提前判定"到头了"
        （否则 runBatch 会拿到空批次却以为还有下一批，永远 PAUSED 不 DONE）。
        """
        size = int(batchSize or job.get("batchSize") or basicSettings.BATCH_SIZE)
        cursor = parseCursor(job.get("lastCursor"))
        # overFetch 由 loadPhotoRows 内部的 2 倍 + 50 保证游标与缺失行有余量
        rows = faceStore.loadPhotoRows(
            dbFile=self.dbFile, limit=size, onlyPending=onlyPending,
            photoRoot=job.get("rootPath") or paths.photo_dir(),
            orderBy="recID", descFlag=False, minRecID=cursor)
        return rows, 0

    def _hasMore(self, job: dict, onlyPending: bool = True) -> bool:
        """游标之后还有没有待处理行（决定这一批收尾是 PAUSED 还是 DONE）"""
        cursor = parseCursor(job.get("lastCursor"))
        probe = faceStore.loadPhotoRows(
            dbFile=self.dbFile, limit=1, onlyPending=onlyPending,
            photoRoot=job.get("rootPath") or paths.photo_dir(),
            orderBy="recID", descFlag=False, minRecID=cursor)
        return bool(probe)

    def _engineKwargs(self) -> dict:
        options = self.engineOptions()
        kwargs = {
            "modelPack": options.get("modelPack") or "buffalo_l",
            "minDetScore": float(options.get("minDetScore")
                                 or basicSettings.MIN_DET_SCORE),
            "minFaceEdge": int(options.get("minFaceEdge")
                               or basicSettings.MIN_FACE_EDGE),
            "maxYaw": int(options.get("maxYaw") or basicSettings.MAX_YAW),
            "wantCrop": True,
            # 丢弃明细默认回传（pool.DEFAULT_ENGINE_KWARGS 已为 True）；
            # 不覆盖：收集是常开的，开关只控制"要不要打印"
            "reportRejected": True,
        }
        detSize = int(options.get("detSize") or 0)
        if detSize > 0:
            kwargs["detSize"] = (detSize, detSize)
        return kwargs

    def runBatch(self, jobCode: str, batchSize: int = None,
                 workers: int = None) -> dict:
        """跑**一批**人脸特征提取。返回结构与父类 runBatch 一致。

        正常路径：RUNNING -> (PAUSED | DONE)
        异常路径：任何 Exception -> errMsg 落库 + FAILED（不留僵尸 RUNNING）
        """
        jobCode = str(jobCode)
        # ---- 依赖自检：必须在起进程之前 ----
        #  缺 onnxruntime 时子进程会在第一行 import 就死，而主进程看到的是
        #  「队列没数据」。不拦住的话这一批会被记成「处理了 N 张、检出 0 张脸」，
        #  进度 100%、任务 DONE —— 与「照片里确实没人脸」在界面上无从分辨。
        pre = preflight()
        if not pre.get("ok"):
            raise RuntimeError(pre.get("hint") or "人脸识别依赖缺失")
        for name, why in pre.get("optional") or ():
            _LOG.warning("preflight: 可选依赖 %s 缺失 —— %s", name, why)

        if not self._acquire(jobCode):
            other = scanSched._ACTIVE_STATE["jobCode"]
            return {"ok": False, "code": comGD.ERR_TASK_STATE_ILLEGAL, "done": False,
                    "paused": False, "result": None,
                    "errMsg": "已有任务在跑: %s（单写入者不允许并发）" % other}
        try:
            job = self.getJob(jobCode)
            if job is None:
                raise ValueError("任务不存在: %s" % jobCode)
            current = str(job.get("jobStatus") or comGD.JOB_IDLE)
            if current == comGD.JOB_DONE:
                return {"ok": True, "code": comGD.RET_OK, "done": True, "paused": False,
                        "result": None, "errMsg": ""}
            if current not in (comGD.JOB_IDLE, comGD.JOB_RUNNING, comGD.JOB_PAUSED,
                               comGD.JOB_FAILED):
                raise ValueError("任务状态异常，无法开始: %s" % current)

            startedAt = job.get("startedYMDHMS") or misc.getTime()
            transit = self.setStatus(jobCode, comGD.JOB_RUNNING,
                                     patch={"startedYMDHMS": startedAt, "errMsg": ""})
            if not transit.get("ok"):
                raise ValueError(transit.get("errMsg") or "置RUNNING 失败")

            job = self.getJob(jobCode) or job
            replace = self.isReplaceJob(job)
            options = self.engineOptions()
            useWorkers = workers or options.get("workers")
            rows, _skipped = self._takeBatch(job, batchSize=batchSize)

            if not rows:
                # 没有可处理的了 —— 这才是 DONE，而不是 PAUSED。
                # 反过来写成 PAUSED 会让任务永远停在"还剩 0 张"上，
                # 界面上看起来像"没跑完"，正是本次要修的那类"进度不动"。
                self.setStatus(jobCode, comGD.JOB_DONE,
                               patch={"finishedYMDHMS": misc.getTime()})
                return {"ok": True, "code": comGD.RET_OK, "done": True, "paused": False,
                        "result": {"counts": {}, "rows": 0}, "errMsg": ""}

            stat = {"withFace": 0, "noFace": 0, "failed": 0}

            def _onPhotoDone(photoRow, result):
                result = result or {}
                if not result.get("ok"):
                    stat["failed"] += 1
                    return
                if int(result.get("kept") or 0) > 0:
                    stat["withFace"] += 1
                else:
                    stat["noFace"] += 1

            def _onProgress(done, total, result):
                # 进度写库节流：每 5 秒或跑完时刷一次，绝不每张都写
                # （onProgress 本身已被 pool 按 PROGRESS_EVERY 抽稀）
                now = time.time()
                if now - getattr(self, "_lastFaceProgress", 0.0) < 5.0 and done != total:
                    return
                self._lastFaceProgress = now
                base = self.getJob(jobCode) or {}
                self.setStatus(jobCode, comGD.JOB_RUNNING, patch={
                    "processedCount": int(base.get("processedCount") or 0) + int(done),
                    "addedCount": int(base.get("addedCount") or 0) + stat["withFace"],
                    "skippedCount": int(base.get("skippedCount") or 0) + stat["failed"],
                })

            started = time.time()
            out = faceStore.extractAndStore(
                rows, workers=useWorkers, dbFile=self.dbFile,
                photoRoot=job.get("rootPath") or paths.photo_dir(),
                thumbRoot=paths.thumb_dir(),
                engineKwargs=self._engineKwargs(),
                replaceFaces=replace,
                onProgress=_onProgress, onPhotoDone=_onPhotoDone)
            elapsed = round(time.time() - started, 2)

            store = out.get("store") or {}
            poolSummary = out.get("pool") or {}
            maxRecID = max(int(r.get("recID") or 0) for r in rows)

            # ---- 整池失败的兜底拦截 ----
            #  三种「一张都没算成」的表现：fatal 非空（子进程崩/队列卡死）、
            #  images 为 0（结果一条没回来）、kept 与 rows 都为 0 但也无失败原因。
            #  只要落在这几种里，**绝不能**当成「这批照片没有人脸」正常收尾 ——
            #  那样游标会照常推进、processedCount 照常涨，这批照片被永久跳过，
            #  而库里的 scanState 还停在 0（下次会重来）但用户已经看到「完成」了。
            #  抛出去 -> 任务 FAILED + errMsg 写明原因，用户一眼看得见。
            fatal = str(poolSummary.get("fatal") or "")
            if not fatal and int(poolSummary.get("images") or 0) == 0:
                fatal = ("进程池一张结果都没回传（%d 张待处理）；"
                         "通常是依赖缺失或子进程被系统杀掉"
                         % len(rows))
            if not fatal and len(rows) > 0 \
                    and int(poolSummary.get("images") or 0) < len(rows) \
                    and int(store.get("faceRows") or 0) == 0:
                # 算了但一个人脸都没算出来，且没有一张成功处理完 —— 同样可疑
                fatal = ("本批 %d 张只处理了 %d 张且一张脸都没写入，"
                         "结果不可信，已中止（详见日志）"
                         % (len(rows), int(poolSummary.get("images") or 0)))
            if fatal:
                raise RuntimeError(fatal)

            # ---- 落进度（一次事务，且**在置状态之前**）----
            # 顺序很关键：先记账再改状态。反过来的话，进程在两步之间被杀，
            # 库里的照片已是 scanState=1（提取过了）而 processedCount 没涨，
            # 于是"已处理"永远少一截，界面上进度条卡在 99%。
            sqliteCommon.update_pb_scan_job("pb_scan_job", job["recID"], {
                "processedCount": int(job.get("processedCount") or 0) + len(rows),
                "addedCount": int(job.get("addedCount") or 0) + stat["withFace"],
                "skippedCount": int(job.get("skippedCount") or 0) + stat["failed"],
                "pendingCount": int(job.get("pendingCount") or 0)
                + int(store.get("faceRows") or 0),
                "batchIndex": int(job.get("batchIndex") or 0) + 1,
                "lastCursor": formatCursor(maxRecID),
            })
            # totalCount 会被「中途又扫进新照片」改变；每批对齐一次，
            # 让百分比有意义（分母只增不减，不会出现 100% 又掉下来）
            currentTotal = countPendingPhotos(self.dbFile)
            if currentTotal != int(job.get("totalCount") or 0):
                self.setStatus(jobCode, comGD.JOB_RUNNING,
                               patch={"totalCount": currentTotal})

            job = self.getJob(jobCode) or job
            done = not self._hasMore(job)
            patch = {}
            if done:
                patch["finishedYMDHMS"] = misc.getTime()
            self.setStatus(jobCode,
                           comGD.JOB_DONE if done else comGD.JOB_PAUSED,
                           patch=patch)

            result = {
                "rows": len(rows),
                "counts": {
                    "photos": len(rows),
                    "withFace": stat["withFace"],
                    "noFace": stat["noFace"],
                    "failed": stat["failed"],
                    "faceRows": int(store.get("faceRows") or 0),
                    "cropCreated": int(store.get("cropCreated") or 0),
                },
                "pool": {"workers": poolSummary.get("workers"),
                         "mode": poolSummary.get("mode"),
                         "wallTime": poolSummary.get("wallTime"),
                         "dropped": poolSummary.get("dropped") or {},
                         "failures": poolSummary.get("failures") or [],
                         "engineInfo": poolSummary.get("engineInfo") or {}},
                "elapsed": elapsed,
            }
            _LOG.info("faceBatch %s: %d 张（有人脸 %d / 无人脸 %d / 失败 %d）"
                      "-> 新入库 %d 条人脸，用时 %.1fs%s",
                      jobCode, len(rows), stat["withFace"], stat["noFace"],
                      stat["failed"], int(store.get("faceRows") or 0), elapsed,
                      "，DONE" if done else "，PAUSED")
            return {"ok": True, "code": comGD.RET_OK, "done": done,
                    "paused": not done, "result": result, "errMsg": ""}
        except Exception as e:
            errText = "%s: %s" % (type(e).__name__, e)
            _LOG.error("faceRunBatch %s 失败: %s", jobCode, errText)
            try:
                self.setStatus(jobCode, comGD.JOB_FAILED, errMsg=errText,
                               patch={"finishedYMDHMS": misc.getTime()})
            except Exception as e2:
                _LOG.error("faceRunBatch: 连置 FAILED 都失败了: %s" % e2)
            return {"ok": False, "code": comGD.ERR_UNKNOWN, "done": False,
                    "paused": False, "result": None, "errMsg": errText}
        finally:
            self._release()

    def runUntilDone(self, jobCode: str, maxBatches: int = None,
                     batchSize: int = None, workers: int = None,
                     onBatch=None) -> dict:
        """连续跑批直到 DONE（或 maxBatches 用完 / 收到停止信号）。"""
        jobCode = str(jobCode)
        self.stopEvent.clear()
        summary = {"jobCode": jobCode, "batches": 0, "done": False,
                   "counts": {"photos": 0, "withFace": 0, "noFace": 0,
                              "failed": 0, "faceRows": 0, "cropCreated": 0},
                   "elapsed": 0.0, "errMsg": ""}
        started = time.time()
        try:
            while True:
                if self.stopEvent.is_set():
                    summary["errMsg"] = "收到停止信号，已在批边界停下"
                    break
                if maxBatches is not None and summary["batches"] >= int(maxBatches):
                    summary["errMsg"] = "达到 maxBatches=%d，任务保持 PAUSED" % int(maxBatches)
                    break
                batchResult = self.runBatch(jobCode, batchSize=batchSize,
                                            workers=workers)
                if not batchResult.get("ok"):
                    summary["errMsg"] = batchResult.get("errMsg") or "批次失败"
                    break
                summary["batches"] += 1
                result = batchResult.get("result") or {}
                for key in summary["counts"]:
                    summary["counts"][key] += int((result.get("counts") or {}).get(key, 0))
                summary["elapsed"] = round(time.time() - started, 2)
                if onBatch is not None:
                    try:
                        onBatch(summary["batches"], result, self.getJob(jobCode))
                    except Exception as e:          # 回调异常不许影响提取本身
                        _LOG.warning("runUntilDone: onBatch 回调异常（已忽略）: %s" % e)
                if batchResult.get("done"):
                    summary["done"] = True
                    break
        finally:
            self._runnerCache = {"key": None, "runner": None}
        summary["jobStatus"] = (self.getJob(jobCode) or {}).get("jobStatus")
        _LOG.info("faceRunUntilDone %s: %d 批, done=%s, 计数=%s, 用时 %.1fs",
                  jobCode, summary["batches"], summary["done"],
                  summary["counts"], summary["elapsed"])
        return summary

    # ========================================================
    # 三、进度与描述
    # ========================================================

    def progress(self, jobCode: str) -> dict:
        info = super(FaceScheduler, self).progress(jobCode)
        if not info.get("ok"):
            return info
        job = self.getJob(jobCode) or {}
        info["jobType"] = int(job.get("jobType") or comGD.JOB_TYPE_FACE)
        info["jobTypeText"] = comGD.jobTypeText(job.get("jobType"))
        info["replaceFaces"] = self.isReplaceJob(job)
        # 「还有多少张没提取」是用户真正想知道的那个数（totalCount 是建任务时的快照）
        info["remainingPhotos"] = countPendingPhotos(self.dbFile)
        return info

    def describe(self, jobCode: str) -> dict:
        info = self.progress(jobCode)
        if not info.get("ok"):
            return info
        text = ("[%s] %s | %d/%d (%s%%) | 检出人脸的照片 %s | 新入库人脸 %s | 失败 %s"
                " | 剩余待识别 %d | 批次 %s/%s | 游标 %s"
                % (info["jobStatusText"], info["jobCode"],
                   info["processedCount"], info["totalCount"], info["percent"],
                   info["addedCount"], info["pendingCount"], info["skippedCount"],
                   info.get("remainingPhotos") or 0,
                   info["batchIndex"], info["batchSize"],
                   info["lastCursor"] or "(无)"))
        if info["errMsg"]:
            text += " | err=%s" % info["errMsg"]
        info["text"] = text
        return info


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("faceScheduler _VERSION:", _VERSION)
    print("任务类型:", {k: comGD.JOB_TYPE_TEXT.get(k, k)
                        for k in comGD.JOB_TYPE_ALL})
    print("游标格式:", formatCursor(12345), "-> 解析", parseCursor("recID:12345"))
    try:
        sched = FaceScheduler()
    except Exception as e:
        print("[未就绪] %s" % e)
        raise SystemExit(0)
    print("待识别照片:", countPendingPhotos())
    for job in sched.listJobs(5):
        print(sched.describe(job["jobCode"])["text"])
