#! /usr/bin/env python3
#encoding: utf-8

#Filename: scan.py
#Description: photo-browser 扫描任务接口（步骤 9·P0）—— 触发 / 轮询 / 续扫 / 停止 / 任务列表
#
# 五个端点
# ----------
#   POST /api/scan/start建任务并**后台线程**开跑，立刻返回 jobCode
#   GET  /api/scan/status/{jobCode}  轮询**真实计数**（不是内存里的估算值）
#   POST /api/scan/resume/{jobCode}  继续下一批 / 跑到完
#   POST /api/scan/stop/{jobCode}在**批边界**叫停（不打断正在提交的事务）
#   GET  /api/scan/jobs              任务列表（分页）
#
# 「不得阻塞请求」是怎么落实的
# --------------------------
#   1. **没有一条端点同步跑扫描**。start / resume 都只做三件极快的事：
#      建一行 pb_scan_job（幂等）、起一个 daemon 线程、把 jobCode 返回前端。
#      `ScanScheduler.startBackground()` 的 docstring 里写得很直白：
#      「立刻返回，扫描在后台推进」—— 它就是为这一步写的。
#   2. **连 totalCount 都不在 start 里数**（countTotal 缺省 false）。
#      `walker.countPhotoFiles()` 是一次全树 scandir，10 万张要 0.3~2s，
#      放在请求里就是一次可感知的卡顿。而第一批本来就会重新数一遍
#      （runner.runBatch 里`totalCount != self.totalCount` 那段），
#      所以先数一遍纯属白花两秒。
#   3. 唯一的同步例外是 `resume?wait=true`：同步跑**一批**并返回本批统计。
#      它只给测试与验收脚本用（要确定性地看到「一批之后是 PAUSED」）；
#      **前端不要传 wait**，那会把一个可能耗时数秒的批次放进 HTTP 请求里。
#
# 内存状态 vs 库内状态：为什么轮询读的是**库**不是内存
# -------------------------------------------------
#   硬约束写的是「后台任务 + 内存状态 + jobId 轮询」。这里的落点是：
#     · 内存里放的是**调度器单例**（互斥门闩 + 后台线程 + ScanRunner 索引缓存）
#       —— 这些是「进程级」的东西，放内存才对：换进程就该重来，
#       而且 ScanRunner 的全表索引有 2.3s 的建索引成本，绝不能每批重建。
#     · 而**进度计数全部落 pb_scan_job**，轮询时读库。
#   为什么进度不留在内存：进程被 Ctrl+C / 被强杀之后，内存里的进度就没了，
#   库里还在。用户重启服务后 `GET /api/scan/jobs` 还能看到上次扫到哪、
#   `lastCursor` 还能续上。把进度放内存等于把「断点续扫」这个核心特性
#   绑在「进程不许崩」这个不成立的前提上。
#   ⇒ **单写入者互斥在内存，跨进程续扫靠 lastCursor 落库。**

import os
import sys
import threading

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import browse                                            # noqa: E402
from api import dto                                               # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.scanner import runner as scanRunner                # noqa: E402
from schedule import scanScheduler as scanSched                   # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("apiScan", "apiscan.log")

router = APIRouter(tags=["scan"])

#: 进程内**唯一**的调度器。必须是单例：`startBackground` 把线程与
#: ScanRunner 索引缓存挂在实例上，两个实例 = 两个线程抢同一个 pb_scan_job
#: = 两个写入者 = database is locked（scanScheduler 文件头的「单写入者」纪律）。
_SCHED_LOCK = threading.Lock()
_SCHEDULER = {"instance": None}


def _scheduler() -> scanSched.ScanScheduler:
    """取（或懒建）进程内唯一的 ScanScheduler。

    ⚠️ **dbFile 必须显式传**（DR-10）：`sqliteCommon.dbHandle()` 无参调用
       绝不切库，所以「第一次连库」这件事必须在这里说清楚 ——
       传了别的库之后再调 query_*，不会被悄悄切回正式库。
    """
    with _SCHED_LOCK:
        if _SCHEDULER["instance"] is not None:
            return _SCHEDULER["instance"]
        try:
            _SCHEDULER["instance"] = scanSched.ScanScheduler(
                dbFile=paths.db_file(), verbose=False)
        except Exception as e:
            # 最典型：pb_photo / pb_scan_job 还没建（没跑 build_db.py）
            raise dto.ApiError(dto.CODE_DB_ERROR,
                               "扫描表未就绪：%s（先跑 tools\\build_db.py）" % e)
        return _SCHEDULER["instance"]


def resetScheduler(timeout: float = 10.0) -> bool:
    """丢掉调度器单例（**只给测试与进程收尾用**：换库 / 换库文件后必须调）。

    返回「后台线程确实已退出」。**调用方必须先确认它停了再关库** ——
    sqliteCommon.closeDb() 会把两个连接都关掉，而一个还在读库的扫描线程
    拿到的是已释放的句柄：那不是 Python 异常，是**进程级 access violation**
    （实测踩过：段错误堆栈落在 `query_pb_photo -> fetchAll` 里，
    看起来像 sqlite 的 bug，实际是「关库时有人在读」）。

    为什么必须显式暴露这个重置
    --------------------------
      sqliteCommon.dbHandle 是进程级单例，测试里每个用例都指向不同的临时库；
      而调度器内部缓存着 ScanRunner（它持有**上一个库的**全表索引）。
      不重置就会拿着旧库的 relIndex 去写新库 —— 判定结果全是错的，
      而且**不报错**（那正是 scanScheduler._runnerFor 注释里说的
      「每批重建索引 = 38 分钟纯开销」的反面：拿着错索引跑得飞快）。
    """
    with _SCHED_LOCK:
        current = _SCHEDULER["instance"]
        _SCHEDULER["instance"] = None
    if current is None:
        return True
    stopped = True
    try:
        current.stop()                       # 批边界生效：当前批跑完就停
        stopped = bool(current.waitBackground(timeout=timeout))
    except Exception as e:
        _LOG.error("resetScheduler: 停后台线程异常: %s: %s", type(e).__name__, e)
        stopped = False
    if not stopped:
        _LOG.error("resetScheduler: 后台扫描线程在 %.1fs 内没退出 —— "
                   "**不要 closeDb()**，否则会段错误", float(timeout))
    return stopped


def _requireJob(jobCode: str) -> dict:
    """取任务行；不存在就404（用统一错误体，不用 404 HTML）。"""
    sched = _scheduler()
    job = sched.getJob(str(jobCode or ""))
    if job is None:
        raise dto.ApiError(dto.CODE_NOT_FOUND, "jobCode=%s 不存在" % jobCode)
    return job


def _assertScanRoot(rootPath: str) -> str:
    """扫描根**只允许是 `paths.photo_dir()` 本身**（缺省即它）。不在就 400。

    为什么必须挡（选项 b：精确相等，不允许子目录）
    ---------------------------------------------
      `ScanScheduler.createJob` 只判 `os.path.isdir(scanRoot)`。于是
      `{"rootPath": "C:\\\\"}` 会**真的去遍历整个 C 盘**，并把所有图片
      按 `relPath` 相对于 `C:\\` 写进 `pb_photo`（库里立刻混进几千行
      `Windows/.../somedecor.jpg`）。
      它确实不写 `photo\\`（原图只读不变式没破），但**会把库搞脏** ——
      而 `paths.validate_layout()` 只管启动时那三个**配置**根，管不到请求参数。

    为什么是「精确相等」而不是「在其之下」
    -------------------------------------
      扫描的语义就是「扫我的照片库」，也就是 `photo_dir()` 这一棵树。
      允许子目录会带来两个说不清的问题：
        · 扫子目录时 `relPath` 仍然相对于**照片库根**（`makeRelPath(rootAbs, ...)`
          用的是传入的 root）-> 同一张照片在不同 root 下 relPath 不同
          -> `relPathHash` 不同 -> **判定成两张不同的照片，重复入库**；
        · 那等于把「清点整个库」与「清点一部分」混进同一张任务表，
          而 `lastCursor` 的语义只在「同一个 root」下才说得通。
      ⇒ 「多盘照片库」这种需求应该改 `PHOTO_ROOT` 配置，而不是走请求参数。
    """
    want = os.path.abspath(str(paths.photo_dir()))
    got = "" if not rootPath else os.path.abspath(str(rootPath))
    if not got or got == want:
        return want
    raise dto.ApiError(
        dto.CODE_PARAM_INVALID,
        "rootPath 只允许指向照片库根：%s（收到 %s）。"
        "扫描的语义是「扫我的照片库」，换库请改配置 PHOTO_ROOT，"
        "不要在请求里指定任意路径 —— 那会把库外目录的图片按错误的 relPath 写进 pb_photo"
        % (want, got),
        extra={"allowedRoot": want, "received": got})


def _statusBody(jobCode: str) -> dict:
    """status 端点的响应体：**真实计数**（全部来自 pb_scan_job 行）。"""
    sched = _scheduler()
    job = _requireJob(jobCode)
    total = int(job.get("totalCount") or 0)
    processed = int(job.get("processedCount") or 0)
    status = str(job.get("jobStatus") or comGD.JOB_IDLE)
    body = {
        "ok": True,
        "jobCode": str(job.get("jobCode") or ""),
        "rootPath": str(job.get("rootPath") or ""),
        "jobStatus": status,
        "jobStatusText": comGD.JOB_STATUS_TEXT.get(status, status),
        #---- 六个计数：前端进度条的**全部**数据 ----
        "totalCount": total,
        "processedCount": processed,
        "addedCount": int(job.get("addedCount") or 0),
        "skippedCount": int(job.get("skippedCount") or 0),
        "duplicateCount": int(job.get("duplicateCount") or 0),
        # pendingCount 是**疑似移动/重命名待确认的张数**（DR-11），
        # 不是「待确认人脸条数」—— 后者在 /api/review/pending/count。
        "pendingCount": int(job.get("pendingCount") or 0),
        "percent": (min(100.0, round(processed * 100.0 / total, 2))
                    if total > 0 else 0.0),
        "batchSize": int(job.get("batchSize") or 0),
        "batchIndex": int(job.get("batchIndex") or 0),
        "lastCursor": job.get("lastCursor") or None,
        "startedYMDHMS": job.get("startedYMDHMS") or None,
        "finishedYMDHMS": job.get("finishedYMDHMS") or None,
        "errMsg": job.get("errMsg") or "",
        "running": sched.isRunning(),
        "canResume": status in (comGD.JOB_IDLE, comGD.JOB_PAUSED, comGD.JOB_FAILED),
    }
    return body


# ============================================================
# 一、POST /api/scan/start
# ============================================================

@router.post("/scan/start", summary="开始扫描（后台线程，立刻返回 jobCode）")
def startScan(body: dto.ScanStartBody = None) -> dict:
    """建任务 + 后台开跑。**请求内不做任何遍历或哈希**。

    参数
    ----
      rootPath   : 扫描根；缺省 `paths.photo_dir()`（**只读**，绝不创建）
      batchSize  : 每批张数；缺省 basicSettings.BATCH_SIZE(100)
      maxBatches : 后台最多跑几批 —— **验收第 2 条就靠它**：
                   传 1 就能确定性地看到「跑完一批 -> PAUSED」
      countTotal : 是否在 start 里先数一遍总数。**缺省 false**（理由见文件头）

    返回
    ----
      { ok, jobCode, created, jobStatus, batchSize, maxBatches, running }

    ⚠️ `jobCode` 幂等：同一个 jobCode 重复 start **不会**新建任务，
       也不会把已 DONE 的任务重置 —— `createJob` 直接原样返回。
       「重跑一遍」的正确姿势是**不传 jobCode**，让它新建一个。
    """
    payload = body or dto.ScanStartBody()
    sched = _scheduler()
    # ⚠️ 先挡 rootPath，再判「已有任务在跑」：参数错是**用户输入错**，
    #    不管此刻有没有任务在跑都该立刻告诉他（而"已在跑"只是暂时状态）。
    scanRoot = _assertScanRoot(payload.rootPath)

    if sched.isRunning():
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "已有扫描任务在跑: %s（单写入者不允许并发）"
                           % sched.activeJobCode(),
                           extra={"activeJobCode": sched.activeJobCode()})

    try:
        # ⚠️ countTotal **缺省 False**：walker.countPhotoFiles() 是一次全树
        #    scandir（10 万张 0.3~2s），放在请求里就是一次可感知的卡顿；
        #    而第一批本来就会重数一遍（runner.runBatch 里totalCount 对齐那段），
        #    先数一遍纯属白花两秒。要精确总数让它跑起来自然会填上。
        job = sched.createJob(root=scanRoot, batchSize=payload.batchSize,
                              jobCode=payload.jobCode,
                              countTotal=bool(payload.countTotal))
    except FileNotFoundError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    except ValueError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    jobCode = str(job.get("jobCode") or "")
    started = sched.startBackground(jobCode, maxBatches=payload.maxBatches)
    if not started.get("ok"):
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL, started.get("errMsg") or "启动失败",
                           extra={"jobCode": jobCode})
    _LOG.info("scan/start: %s 后台启动（batchSize=%s maxBatches=%s）",
              jobCode, job.get("batchSize"), payload.maxBatches)
    return dto.okBody(jobCode=jobCode, created=bool(job.get("created")),
                      rootPath=scanRoot,
                      jobStatus=str(job.get("jobStatus") or comGD.JOB_IDLE),
                      batchSize=int(job.get("batchSize") or 0),
                      maxBatches=payload.maxBatches, running=True,
                      poll="/api/scan/status/%s" % jobCode)


# ============================================================
# 二、GET /api/scan/status/{jobCode}
# ============================================================

@router.get("/scan/status/{jobCode}", summary="扫描进度（真实计数）")
def getScanStatus(jobCode: str) -> dict:
    """轮询这个。**六个计数全部来自 pb_scan_job 行**，不是内存里的估算。

    前端进度条 / 扫描台（P-07）直接吃这个响应：
    百分比、四类计数、批次、游标、能否续扫，全部齐了，不用再调别的接口。
    """
    return _statusBody(jobCode)


# ============================================================
# 三、POST /api/scan/resume/{jobCode}
# ============================================================

@router.post("/scan/resume/{jobCode}", summary="继续扫描（后台；wait=true 则同步跑一批）")
def resumeScan(jobCode: str,
               maxBatches: int = Query(default=None,
                                       description="后台最多跑几批；缺省跑到 DONE"),
               wait: int = Query(default=0,
                                 description="**1 = 同步跑完再返回**（仅测试/验收用，"
                                             "前端不要传：一批可能耗时数秒）")):
    """续扫。`maxBatches=1` 就精确地「继续下一批」。

    ⚠️ 状态机：`runBatch` 自己会把 PAUSED -> RUNNING 推过去，
       这里**不预先置 RUNNING** —— 预置的话，一旦 `runBatch` 抢不到
       单写入者门闩，任务就永远卡在 RUNNING 而实际没人扫
       （scanScheduler 文件头专门为「僵尸 RUNNING」加了一条认领通道，
       就是为了不让这种事发生）。
    """
    sched = _scheduler()
    job = _requireJob(jobCode)
    code = str(job.get("jobCode") or "")
    status = str(job.get("jobStatus") or comGD.JOB_IDLE)

    if status == comGD.JOB_RUNNING:
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "任务 %s 正在扫描中，无需续扫" % code)
    if status == comGD.JOB_DONE:
        # 已跑完：不是错误，但必须说清楚，否则前端会把 DONE 当成「没反应」
        return dto.okBody(jobCode=code, started=False, jobStatus=status,
                          errMsg="任务已 DONE（要重跑请不带 jobCode 新建一个任务）",
                          **_countsOf(job))
    if sched.isRunning():
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "已有扫描任务在跑: %s" % sched.activeJobCode(),
                           extra={"activeJobCode": sched.activeJobCode()})

    if int(wait):
        # 同步跑（验收脚本用）：拿得到本批的**增量**计数
        result = sched.runBatch(code)
        if not result.get("ok"):
            raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                               result.get("errMsg") or "批次失败",
                               extra={"jobCode": code})
        return dto.okBody(jobCode=code, started=True, waited=True,
                          done=bool(result.get("done")),
                          paused=bool(result.get("paused")),
                          batchCounts=(result.get("result") or {}).get("counts") or {},
                          **_countsOf(sched.getJob(code) or {}))

    started = sched.startBackground(code, maxBatches=maxBatches)
    if not started.get("ok"):
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL, started.get("errMsg") or "续扫失败",
                           extra={"jobCode": code})
    return dto.okBody(jobCode=code, started=True, waited=False,
                      maxBatches=maxBatches,
                      poll="/api/scan/status/%s" % code, **_countsOf(job))


def _countsOf(job: dict) -> dict:
    """pb_scan_job 行 -> 六个计数字段（start/resume 与 status 共用同一口径）。"""
    return {"totalCount": int(job.get("totalCount") or 0),
            "processedCount": int(job.get("processedCount") or 0),
            "addedCount": int(job.get("addedCount") or 0),
            "skippedCount": int(job.get("skippedCount") or 0),
            "duplicateCount": int(job.get("duplicateCount") or 0),
            "pendingCount": int(job.get("pendingCount") or 0),
            "lastCursor": job.get("lastCursor") or None,
            "jobStatus": str(job.get("jobStatus") or comGD.JOB_IDLE)}


# ============================================================
# 四、POST /api/scan/stop/{jobCode}
# ============================================================

@router.post("/scan/stop/{jobCode}", summary="停止扫描（默认等到真停）")
def stopScan(jobCode: str,
             wait: int = Query(default=1,
                               description="**1 = 等到后台线程真的退出再返回**。"
                                           "0 = 只发信号立刻返回"),
             timeout: float = Query(default=30.0, ge=0.1, le=300.0,
                                    description="wait=1 时的最长等待秒数")):
    """叫停。**两级停止语义**，响应时间取决于停在哪一级：

      ① **全树遍历阶段**（`walker.listPhotoFiles(shouldStop=...)`）
         10 万张库里这一段本身要 2~20s（只 stat，不 hash）。
         它才是「按了停止没反应」的主因。现在它也会中止：
         中止时本批**一张都不处理**、`lastCursor` 与全部计数**一律不动**，
         下次 resume 重走一遍遍历。⇒ 延迟降到「最多 64 个目录」（毫秒级）。
      ② **批内逐文件循环**
         已处理的文件照常落库、游标照常推进 —— 干完的活不白费。
         最坏情况 = 多跑完当前批（batchSize 张）。

    ⚠️ 两级检查点都**只在批边界 / 遍历边界生效，绝不打断正在提交的事务**：
       那会在库里留下「半批」数据而 lastCursor 已推进 ——
       下次续扫从游标往后走，那半批就永远丢了。
       （注意 ① 的「不动游标」与 ② 的「推进游标」不是矛盾：① 是**压根没开始处理**，
         所以游标本来就不该动；② 是**处理了一部分**，那部分必须记账。）

    ⚠️ `wait=1`（默认）为什么必须等
       ---------------------------
       「已发出停止信号」和「已停止」是两件事：扫描线程可能正在
       提交一个 500 行的事务。不等它退出就关库（进程退出、测试收尾、
       服务重启）会拿到**已释放的 sqlite 句柄** —— 那不是 Python 异常，
       是**进程级 access violation**（实测踩过，堆栈落在 `fetchAll`，
       看起来像 sqlite 的 bug）。所以「停止」这个动作的返回值必须
       反映**真实的停止状态**，前端也才能确定地开始下一步。
    """
    sched = _scheduler()
    job = _requireJob(jobCode)
    code = str(job.get("jobCode") or "")
    status = str(job.get("jobStatus") or comGD.JOB_IDLE)

    if status == comGD.JOB_DONE:
        return dto.okBody(jobCode=code, stopped=True, jobStatus=status,
                          note="任务已结束，无需停止")
    sched.stop()
    alive = False
    if int(wait):
        alive = not bool(sched.waitBackground(timeout=float(timeout)))
    _LOG.info("scan/stop: %s 停止信号已发出（已停=%s）", code, not alive)
    return dto.okBody(jobCode=code, stopped=not alive, stillRunning=alive,
                      jobStatus=status,
                      note=("已在批边界停下" if not alive
                            else "仍在跑完当前批（最多 batchSize 张），"
                                 "可稍后再轮询 status"),
                      poll="/api/scan/status/%s" % code)


# ============================================================
# 五、GET /api/scan/jobs
# ============================================================

@router.get("/scan/jobs", summary="扫描任务列表")
def listJobs(page: int = Query(default=1, ge=1),
             size: int = Query(default=20),
             jobStatus: str = Query(default=None,
                                    description="按状态过滤（IDLE/RUNNING/PAUSED/DONE/FAILED）")):
    """任务列表（新的在前）。

    ⚠️ 查库用生成层 `query_pb_scan_job`（它有 `jobStatus` 等值过滤参数），
       `total` 走 `countWhereGeneral`。**含软删任务**（delFlag='*'）：
       排障时「上周那个任务是失败还是压根没跑起来」必须查得到。
    """
    p, s = dto.clampPage(page, size, defaultSize=20)
    at = dto.offsetOf(p, s)

    where = ["delFlag = %s"]
    values = [comGD.DEL_FLAG_NO if jobStatus is None else "*"]
    if jobStatus:
        if jobStatus not in comGD.JOB_STATUS_ALL:
            raise dto.ApiError(dto.CODE_PARAM_INVALID,
                               "jobStatus 只支持 %s" % list(comGD.JOB_STATUS_ALL))
        where.append("jobStatus = %s")
        values.append(jobStatus)
    cond = " AND ".join(where)
    total = sqliteCommon.countWhereGeneral("pb_scan_job", cond, tuple(values))

    rows = sqliteCommon.query_pb_scan_job(
        "pb_scan_job", delFlag=comGD.DEL_FLAG_NO if jobStatus is None else "*",
        jobStatus=jobStatus or "", orderBy="recID", descFlag=True,
        limitNum=s, offsetNum=at)
    items = []
    for row in rows:
        item = browse._jobBrief(row)
        item["createdYMDHMS"] = row.get("regYMDHMS") or None
        item["errMsg"] = row.get("errMsg") or None
        items.append(item)
    return dto.pageBody(items, p, s, total)


# ============================================================
# 六、诊断：新建一个空任务的编码（供前端/CLI 展示格式）
# ============================================================

@router.get("/scan/code-preview", summary="新建任务的 jobCode 会长什么样（不写库）")
def codePreview() -> dict:
    """只返回格式说明 + 一个**样例**编码，不建任务、不写一行。

    存在的意义：前端/P-07 要显示「任务编码」给人看，
    而 jobCode 是 `SJ_<yyyymmddHHMMSS>_<6位随机>`（见 runner.makeJobCode）。
    给一个**格式说明**比让前端去猜正则稳得多 —— 猜错了只是显示难看，
    但如果前端按前缀去截断日志里的路径，就会静默丢信息。
    """
    return {"ok": True, "prefix": basicSettings.SCAN_JOB_CODE_PREFIX,
            "format": "%s_<yyyymmddHHMMSS>_<6位随机>" % basicSettings.SCAN_JOB_CODE_PREFIX,
            "sample": scanRunner.makeJobCode(),
            "note": "样例编码是现生成的，**不写库**；start 时真正用的是另一个"}


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("scan.py _VERSION:", _VERSION)
    print("库:", paths.db_file())
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    print("状态机:", {k: comGD.JOB_STATUS_TEXT.get(k, k)
                     for k in comGD.JOB_STATUS_ALL})
    print("样例 jobCode:", scanRunner.makeJobCode())
    try:
        _sched = _scheduler()
        print("调度器就绪，任务数:", len(_sched.listJobs(5)))
        for one in _sched.listJobs(3):
            print("  ", _statusBody(one["jobCode"])["jobStatusText"],
                  one["jobCode"], one.get("processedCount"), "/", one.get("totalCount"))
    except dto.ApiError as e:
        print("[未就绪]", e.code, e.message)
