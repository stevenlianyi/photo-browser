#! /usr/bin/env python3
#encoding: utf-8
# Filename: face.py
# Description: photo-browser 人脸识别任务接口 —— 触发 / 轮询 / 停止 / 任务列表
#
# 为什么必须有这组端点（它补的是哪条断链）
# ------------------------------------------------
#   「扫描了 1000 张照片，待确认 0 张」的根因不是数据，是**两步之间没有连线**：
#     · POST /api/scan/start 只走 processor.scanner.runner —— 只写 pb_photo；
#     · 人脸特征提取（写 pb_face）唯一的入口是 CLI `tools/run_faces.py`。
#   Web 端没有第二个入口，于是扫描完成后 pb_face 一行没变，
#   而侧栏「待确认」数的就是 pb_face 里 personCode IS NULL 的行 ——
#   于是无论扫多少张，那个数字都不会动。
#   本模块把 `schedule.faceScheduler` 暴露成端点，让 Web 端能发起人脸识别。
#
# 四个端点
# ----------
#   POST /api/facerec/start          建任务 + 后台开跑，立刻返回 jobCode
#   GET  /api/facerec/status/{code}  轮询真实计数（total/processed/added/pending）
#   POST /api/facerec/stop/{code}    在**批边界**叫停（不打断正在提交的事务）
#   GET  /api/facerec/jobs           人脸识别任务列表（只列 jobType=1）
#   GET  /api/facerec/pending-count  还要认多少张（扫完 1000 张却不知道没认脸时用）
#
# ⚠️⚠️ 为什么前缀是 /facerec 而不是 /face（**别改回去**）
# ------------------------------------------------------
#   `/api/face/{faceCode}` 是 static.py 里**人脸裁剪图**的路由，而
#   staticApi 是**第一个** include 的 router。FastAPI 0.142 的
#   include_router 把子路由包成 `_IncludedRouter`，跨 router 的匹配顺序
#   不保证「静态段优先于参数段」——于是 `/api/face/jobs`、
#   `/api/face/pending-count` 会先落进 `{faceCode}`，被当成 faceCode 去查脸，
#   查不到就返回 404。
#
#   而这个 404 极具误导性：dto 的统一错误处理对**任何** 404 都写成
#   「接口或资源不存在: <请求路径>」，所以界面上/日志里看到的是
#   「接口不存在」，真实原因却是「被别的路由接走了」。
#   （本项目已在这一步浪费过一整轮：路由明明在 openapi 里，却一直 404。）
#   ⇒ 任务类端点一律走 /facerec/**，与 {faceCode} 完全不同的命名空间。
#
# 与 /api/scan 的分工
# ------------------
#   状态机、断点续跑、进度口径、单写入者互斥**全部共用**（同表同状态机，
#   理由见 globalDefinition「二之二 任务类型」）。本模块只是把
#   `runBatch` 换成"提特征"的那一套，因此**不复述**任何状态机规则。

import os
import sys
import threading
import time
from typing import Optional

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import browse                                            # noqa: E402
from api import dto                                               # noqa: E402
from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from common import resourcePath as resourcePath                   # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from engine.face import engine as faceEngine                      # noqa: E402
from engine.face import faceStore as faceStore                    # noqa: E402
from engine.match import centroid as matchCentroid                # noqa: E402
from engine.match import matcher as matcher                       # noqa: E402
from processor.review import assigner as assigner                 # noqa: E402
from processor.review import queue as reviewQueue                 # noqa: E402
from schedule import faceScheduler as faceSched                   # noqa: E402
from schedule import scanScheduler as scanSched                   # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402
from pydantic import BaseModel, Field                           # noqa: E402

_VERSION = "20261007"

_LOG = misc.setLogNew("apiFace", "apiface.log")

router = APIRouter(tags=["face"])

#: 进程内唯一的人脸识别调度器（理由与 api/scan.py 的 _SCHEDULER 完全一样：
#: 它缓存着引擎参数覆盖与后台线程，两个实例 = 两个写入者 = database is locked）
_SCHED_LOCK = threading.Lock()
_SCHEDULER = {"instance": None}


def _scheduler() -> faceSched.FaceScheduler:
    with _SCHED_LOCK:
        if _SCHEDULER["instance"] is not None:
            return _SCHEDULER["instance"]
        try:
            _SCHEDULER["instance"] = faceSched.FaceScheduler(
                dbFile=paths.db_file(), verbose=False)
        except Exception as e:
            raise dto.ApiError(dto.CODE_DB_ERROR,
                               "人脸识别表未就绪：%s\n%s"
                               % (e, resourcePath.dbInitHint("请先建库 / 补表")))
        return _SCHEDULER["instance"]


def resetScheduler(timeout: float = 10.0) -> bool:
    """丢掉调度器单例（**只给测试与进程收尾用**）。

    返回「后台线程确实已退出」。**调用方必须先确认它停了再关库** ——
    理由与 api/scan.py 的同名函数逐条相同（那里记了实测的段错误堆栈）：
    一个还在读库的提取线程拿到已释放的 sqlite 句柄，不是 Python 异常，
    是进程级 access violation。
    """
    with _SCHED_LOCK:
        current = _SCHEDULER["instance"]
        _SCHEDULER["instance"] = None
    if current is None:
        return True
    stopped = True
    try:
        current.stop()                      # 批边界生效：当前批跑完就停
        stopped = bool(current.waitBackground(timeout=timeout))
    except Exception as e:
        _LOG.error("face/resetScheduler: 停后台线程异常: %s: %s", type(e).__name__, e)
        stopped = False
    if not stopped:
        _LOG.error("face/resetScheduler: 后台提取线程在 %.1fs 内没退出 —— "
                   "**不要 closeDb()**", float(timeout))
    return stopped


def _requireJob(jobCode: str) -> dict:
    """取任务行；不存在或**不是人脸识别任务**就404。

    ⚠️ 类型必须校验：`GET /api/facerec/status/SJ_xxx` 若不挡住，
    就会拿扫描任务去套人脸提取的进度口径 —— 而两者的 pendingCount
    一个是"移动张数"、一个是"人脸条数"，混起来就是一个静默错数。
    """
    sched = _scheduler()
    job = sched.getJob(str(jobCode or ""))
    if job is None:
        raise dto.ApiError(dto.CODE_NOT_FOUND, "jobCode=%s 不存在" % jobCode)
    if int(job.get("jobType") or comGD.JOB_TYPE_SCAN) != comGD.JOB_TYPE_FACE:
        raise dto.ApiError(
            dto.CODE_PARAM_INVALID,
            "任务 %s 是「%s」任务，不是人脸识别任务"
            % (jobCode, comGD.jobTypeText(job.get("jobType"))),
            extra={"jobCode": jobCode, "jobType": int(job.get("jobType") or 0)})
    return job


def _statusBody(jobCode: str) -> dict:
    """status 端点的响应体：进度**全部来自 pb_scan_job 行**（不是内存估算）。"""
    sched = _scheduler()
    _requireJob(jobCode)
    info = sched.progress(str(jobCode))
    status = str(info.get("jobStatus") or comGD.JOB_IDLE)
    return {
        "ok": True,
        "jobCode": str(info.get("jobCode") or ""),
        "jobType": int(info.get("jobType") or comGD.JOB_TYPE_FACE),
        "jobTypeText": str(info.get("jobTypeText") or ""),
        "jobStatus": status,
        "jobStatusText": str(info.get("jobStatusText") or status),
        # ---- 计数：口径见 faceScheduler 文件头「进度口径」----
        "totalCount": int(info.get("totalCount") or 0),
        "processedCount": int(info.get("processedCount") or 0),
        # addedCount = **检出人脸的照片张数**（一张合影 3 张脸算 1），
        # 与 pendingCount（人脸条数）不是一回事，两个都给人看
        "addedCount": int(info.get("addedCount") or 0),
        "skippedCount": int(info.get("skippedCount") or 0),
        "pendingCount": int(info.get("pendingCount") or 0),
        # 库里还剩多少张没认（totalCount 只是建任务时的快照）
        "remainingPhotos": int(info.get("remainingPhotos") or 0),
        "percent": (min(100.0, round(int(info.get("processedCount") or 0) * 100.0
                                      / int(info.get("totalCount") or 1), 2))
                    if int(info.get("totalCount") or 0) > 0 else 0.0),
        "replaceFaces": bool(info.get("replaceFaces")),
        # 推导规则见 browse.batchProgressOf（同一份口径，不另写一套）
        **browse.batchProgressOf(_scheduler().getJob(str(jobCode)) or {}),
        "lastCursor": info.get("lastCursor") or None,
        "startedYMDHMS": info.get("startedYMDHMS") or None,
        "finishedYMDHMS": info.get("finishedYMDHMS") or None,
        "errMsg": info.get("errMsg") or "",
        "running": sched.isRunning(),
        "canResume": status in (comGD.JOB_IDLE, comGD.JOB_PAUSED, comGD.JOB_FAILED),
    }


# ============================================================
# 一、请求体
# ============================================================

class FaceStartBody(BaseModel):
    """POST /api/face/start"""
    batchSize: Optional[int] = Field(default=None, description="每批处理张数；缺省 basicSettings.BATCH_SIZE")
    maxBatches: Optional[int] = Field(default=None, description="后台最多跑几批；缺省跑到 DONE")
    countTotal: bool = Field(default=True,
                             description="是否在建任务时数一遍待识别张数。"
                                         "**缺省 true**：这只是一次带索引的 COUNT"
                                         "（idx_pb_photo_scanState），10 万行约毫秒级；"
                                         "而界面上「还剩多少张没认」是用户最需要的那个数")
    replaceFaces: bool = Field(default=False,
                               description="重提取：先删该照片的旧人脸行再插。"
                                           "**日常增量跑不要开** —— 它会连带"
                                           "丢掉已人工确认的结果（upsert 刻意不刷 isConfirmed）")
    jobCode: Optional[str] = Field(default=None, description="指定则幂等复用该任务")
    workers: Optional[int] = Field(default=None, description="进程数；缺省自动（见 FACE_MAX_WORKERS）")
    modelPack: Optional[str] = Field(default=None, description="模型包名；缺省 buffalo_l")
    minDetScore: Optional[float] = Field(default=None, description="检测置信度下限；缺省 basicSettings.MIN_DET_SCORE")
    minFaceEdge: Optional[int] = Field(default=None, description="人脸框短边下限(px)")
    maxYaw: Optional[int] = Field(default=None, description="侧脸 |yaw| 上限(度)")


# ============================================================
# 二、POST /api/face/start
# ============================================================

@router.post("/facerec/start", summary="开始人脸识别（后台线程，立刻返回 jobCode）")
def startFace(body: FaceStartBody = None) -> dict:
    """建任务 + 后台开跑。**请求内不做任何模型推理**。

    返回 { ok, jobCode, created, jobStatus, totalCount, remainingPhotos, poll }

    ⚠️ 绝不阻塞：人脸提取是 CPU 密集的（单张 0.2~1s），同步跑一批会把
    HTTP 请求挂几十分钟。引擎第一次加载模型还要 3~10s。
    """
    payload = body or FaceStartBody()
    sched = _scheduler()

    if sched.isRunning():
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "已有任务在跑: %s（单写入者不允许并发）"
                           % sched.activeJobCode(),
                           extra={"activeJobCode": sched.activeJobCode()})

    # 引擎参数先进内存态（进程级），建任务失败也不会留下半套配置
    sched.setEngineOptions(workers=payload.workers, modelPack=payload.modelPack,
                           minDetScore=payload.minDetScore,
                           minFaceEdge=payload.minFaceEdge, maxYaw=payload.maxYaw,
                           replaceFaces=payload.replaceFaces or None)

    try:
        job = sched.createJob(batchSize=payload.batchSize, jobCode=payload.jobCode,
                              countTotal=bool(payload.countTotal),
                              replaceFaces=bool(payload.replaceFaces))
    except FileNotFoundError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    except ValueError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    jobCode = str(job.get("jobCode") or "")
    if str(job.get("jobStatus") or "") == comGD.JOB_DONE:
        # 幂等命中一个已跑完的任务：不是错误，但必须说清楚，
        # 否则前端会把 DONE 当成"没反应"
        return dto.okBody(jobCode=jobCode, created=False,
                          jobStatus=comGD.JOB_DONE, started=False,
                          note="任务已 DONE（要重跑请不带 jobCode 新建一个任务）",
                          remainingPhotos=faceSched.countPendingPhotos(),
                          poll="/api/facerec/status/%s" % jobCode)

    started = sched.startBackground(jobCode, maxBatches=payload.maxBatches)
    if not started.get("ok"):
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           started.get("errMsg") or "启动失败",
                           extra={"jobCode": jobCode})
    _LOG.info("face/start: %s 后台启动（total=%s replace=%s workers=%s）",
              jobCode, job.get("totalCount"), payload.replaceFaces,
              sched.engineOptions().get("workers") or "auto")
    return dto.okBody(jobCode=jobCode, created=bool(job.get("created")),
                      jobStatus=str(job.get("jobStatus") or comGD.JOB_IDLE),
                      batchSize=int(job.get("batchSize") or 0),
                      totalCount=int(job.get("totalCount") or 0),
                      remainingPhotos=faceSched.countPendingPhotos(),
                      maxBatches=payload.maxBatches, started=True, running=True,
                      poll="/api/facerec/status/%s" % jobCode)


# ============================================================
# 三、GET /api/facerec/status/{jobCode}
# ============================================================

@router.get("/facerec/status/{jobCode}", summary="人脸识别进度（真实计数）")
def getFaceStatus(jobCode: str) -> dict:
    """轮询这个。进度全部来自 pb_scan_job 行，不是内存里的估算。

    与 /api/scan/status 的响应形状**刻意保持一致**（同一套字段名），
    前端的进度条组件因此可以原样复用，不必为两种任务各写一套渲染。
    """
    return _statusBody(jobCode)


# ============================================================
# 四、POST /api/face/stop/{jobCode}
# ============================================================

@router.post("/facerec/stop/{jobCode}", summary="停止人脸识别（默认等到真停）")
def stopFace(jobCode: str,
             wait: int = Query(default=1,
                               description="**1 = 等到后台线程真的退出再返回**。0 = 只发信号"),
             timeout: float = Query(default=60.0, ge=0.1, le=600.0,
                                    description="wait=1 时的最长等待秒数")):
    """叫停，**在批边界生效**（绝不打断正在提交的事务）。

    ⚠️ 停的代价比扫描大得多：提取一批要几十秒到几分钟（本批 100 张），
       而进程池在跑的时候 `stopEvent` 只在**批与批之间**被检查。
       所以这里的默认 timeout 给到 60s（扫描那边 30s 是按"遍历+哈希"定的）。
       返回里 `stillRunning=true` 时界面应提示"正在跑完当前批"，
       **不要**立刻当成失败 —— 那会让人以为任务坏了。

    ⚠️ 停止是"安全"的：已落库的人脸行与已推进的游标都保留，
       下次 resume 从游标往后继续（faceStore 的 faceCode 幂等键保证不重复）。
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
    _LOG.info("face/stop: %s 停止信号已发出（已停=%s）", code, not alive)
    return dto.okBody(jobCode=code, stopped=not alive, stillRunning=alive,
                      jobStatus=status,
                      note=("已在批边界停下" if not alive
                            else "仍在跑完当前批（最多 batchSize 张），"
                                 "可稍后再轮询 status"),
                      poll="/api/facerec/status/%s" % code)


# ============================================================
# 五、GET /api/face/jobs
# ============================================================

@router.get("/facerec/jobs", summary="人脸识别任务列表")
def listFaceJobs(page: int = Query(default=1, ge=1),
                 size: int = Query(default=20),
                 jobStatus: str = Query(default=None,
                                        description="按状态过滤（IDLE/RUNNING/PAUSED/DONE/FAILED）")):
    """人脸识别任务列表（新的在前）。**只列 jobType=1**。

    ⚠️ 少了 jobType 过滤就会把扫描任务也列进来 —— 两者 pendingCount 口径不同
    （扫描=疑似移动张数，人脸=新入库人脸条数），混列出来的数字没法解释。
    """
    p, s = dto.clampPage(page, size, defaultSize=20)
    at = dto.offsetOf(p, s)

    where = ["jobType = %s", "delFlag = %s"]
    values = [str(comGD.JOB_TYPE_FACE), comGD.DEL_FLAG_NO]
    if jobStatus:
        if jobStatus not in comGD.JOB_STATUS_ALL:
            raise dto.ApiError(dto.CODE_PARAM_INVALID,
                               "jobStatus 只支持 %s" % list(comGD.JOB_STATUS_ALL))
        where.append("jobStatus = %s")
        values.append(jobStatus)
    total = sqliteCommon.countWhereGeneral("pb_scan_job", " AND ".join(where),
                                           tuple(values))

    rows = sqliteCommon.query_pb_scan_job(
        "pb_scan_job", delFlag=comGD.DEL_FLAG_NO,
        jobStatus=jobStatus or "", jobType=str(comGD.JOB_TYPE_FACE),
        orderBy="recID", descFlag=True, limitNum=s, offsetNum=at)
    items = []
    for row in rows:
        item = browse._jobBrief(row)
        item["pendingCount"] = int(row.get("pendingCount") or 0)
        item["skippedCount"] = int(row.get("skippedCount") or 0)
        item["createdYMDHMS"] = row.get("regYMDHMS") or None
        item["errMsg"] = row.get("errMsg") or None
        item["replaceFaces"] = str(row.get("label") or "") == "REPLACE"
        items.append(item)
    return dto.pageBody(items, p, s, total)


# ============================================================
# 六、GET /api/face/pending-count
# ============================================================

@router.get("/facerec/pending-count", summary="还剩多少张没识别人脸（不建任务）")
def pendingCount() -> dict:
    """待识别的**照片张数**（scanState=0），以及待确认的**人脸条数**。

    为什么单独给一个端点：这是「扫完 1000 张却不知道有没有在认脸」时
    唯一该看的东西。两个数字的含义完全不同，所以**并列返回、不合并**：
      · pendingPhotos —— 还没提取过特征的照片（人脸识别任务的输入）
      · pendingFaces  —— 已提取但未归属的脸（待确认队列，也是侧栏角标）
    早期只暴露后者，于是"扫了很多张"看起来一切正常，而人脸一步都没走。

    `environment` 段是**依赖自检**结果：缺 onnxruntime 时任务仍会"成功"
    却检出 0 张脸（子进程 import 就死，主进程只看到队列没数据），
    所以必须在点按钮之前就把问题摆出来。
    """
    from processor.review import queue as reviewQueue
    sched = _scheduler()
    pre = faceSched.preflight()
    return {
        "ok": True,
        "pendingPhotos": faceSched.countPendingPhotos(),
        "pendingFaces": int(reviewQueue.countPending()),
        "faceJobRunning": sched.isRunning(),
        "faceJobCode": sched.activeJobCode(),
        "environment": {
            "ok": bool(pre.get("ok")),
            "missing": [list(item) for item in pre.get("missing") or ()],
            "optional": [list(item) for item in pre.get("optional") or ()],
            "hint": pre.get("hint") or "",
        },
        "note": "pendingPhotos>0 说明有照片还没识别人脸；"
                "扫完照片不会自动产生待确认人脸 —— 需要先跑人脸识别",
    }


# ============================================================
# 七、POST /api/facerec/extract-one（识别**这一张**照片的人脸）
# ============================================================
#
# 为什么要有它（它补的是哪条断链）
# --------------------------------
#   `/facerec/start` 是**全库任务**：取数走 `scanState = 0` 全表分页，
#   没有、也不该有 photoCode 的概念。于是「我就要看这一张照片里是谁」
#   在界面上无处可点 —— 用户只能把整个库认一遍，或者去敲 CLI。
#
#   它与全库任务是同一条链路的两种粒度，复用顺序刻意与批量一致
#   （不按同一顺序走，同一张脸会得到两种结果）：
#     engine.extract -> faceStore.commitOne -> reviewQueue.loadPendingRows
#     -> matcher.matchMany -> assigner.applyAuto
#
# 三条纪律
# --------
#   ① **不建 pb_scan_job 任务**。单张是秒级操作：建任务要走状态机、游标、
#      断点续跑，而它没有「续跑」的语义 —— 落一张 DONE 任务只会在任务列表里
#      堆噪音。所以进度放在进程内快照 `_ONE`（与 api/match.py 同一理由）：
#      进程退出就没了，而那正是需要重跑的时候。
#   ② **并发 = 直接拒绝，不排队**。与扫描 / 人脸提取 / 匹配共用同一把单写入者
#      门闩；抢不到就 409 说清「谁在占着」。排队会让按钮看起来"点了没反应"，
#      而静默排队几十秒后突然开始写库更难解释。
#   ③ **重提取（replaceFaces）会清掉这张照片上已有的归属**，连带三件事必须
#      做全，缺一件都是静默的数据不一致：
#        · 人工确认 / 自动归属（isConfirmed / personCode）**不复原**
#          -> 界面必须事前预告（`confirmedBefore` 就是给它用的）；
#        · `pb_photo_person` 的关联行会变成「幽灵关联」（这张照片里再无此人）
#          —— commitOne 只删 pb_face、不碰关联表 -> 这里按 photoCode 清；
#        · 被删掉的确认样本还留在质心里 -> 重算受影响人物的质心
#          （DR-16：质心只能由 isConfirmed=1 的样本生成）。

#: 抢闩时用的记号（不是任务，没有 jobCode；用它让报错说得出「谁占着」）
_ONE_LOCK_OWNER: str = "FACE-ONE"

_ONE_LOCK = threading.Lock()

_ONE: dict = {
    "running": False,
    "phase": "idle",
    "photoCode": "",
    # ---- 输入 ----
    "replaceFaces": True,
    "match": True,
    # ---- 提取 ----
    "faceCount": 0,          # 本次写入的人脸条数
    "rawCount": 0,           # 检测到的（过滤前）人脸数
    "deletedFaces": 0,       # 重提取删掉的旧人脸行
    "confirmedBefore": 0,    # 旧人脸里人工确认过的条数（用户要知道丢了什么）
    "droppedLinks": 0,       # 清掉的幽灵关联行
    "centroidsRecomputed": 0,
    "engineElapsed": 0.0,
    # ---- 匹配（只判这一张）----
    "matchRan": False,
    "matched": 0,            # 参与判定的脸数
    "autoAssigned": 0,       # 自动归属
    "needReview": 0,         # 落在灰区、仍需人工确认
    "unknown": 0,            # 判不出来（进聚类）
    "assignedTo": [],        # [{faceCode, personCode, displayName, score}]
    "matchNote": "",
    # ---- 收尾 ----
    "elapsed": 0.0,
    "startedYMDHMS": None,
    "finishedYMDHMS": None,
    "lastError": "",
    "message": "",
    "log": [],
}

#: 阶段 -> 给用户看的一句话（**阶段是真的**，不编百分比：单张提取只有
#: 「检测 -> 写入 -> 比对」三段，中间没有可切片的进度）
_ONE_PHASE_TEXT: dict = {
    "idle": "还没有识别过单张照片",
    "loading_model": "正在加载人脸模型（首次约 10 秒）…",
    "extracting": "正在检测人脸…",
    "storing": "正在写入人脸…",
    "cleanup": "正在清理旧归属…",
    "matching": "正在比对是谁…",
    "done": "已完成",
    "error": "出错终止",
}


def _onePatch(**fields) -> None:
    with _ONE_LOCK:
        _ONE.update(fields)


def _oneLog(text: str) -> None:
    """往快照里追一行中文说明（上限 20 行，排障与「给用户看」共用）。"""
    with _ONE_LOCK:
        lines = list(_ONE.get("log") or [])
        lines.append("%s %s" % (misc.getTime(), text))
        _ONE["log"] = lines[-20:]


def _oneRunning() -> bool:
    with _ONE_LOCK:
        return bool(_ONE.get("running"))


def _oneReset(photoCode: str, replaceFaces: bool, doMatch: bool) -> None:
    """把快照重置成本次任务的初值（**在起线程之前、同步做完**）。

    为什么必须同步做：POST 一返回，前端立刻开始轮询，而后台线程可能还没跑到
    第一行 —— 那几百微秒里 status 会返回上一次的 `running=False`，
    前端于是以为"已经跑完了"，读到的却是**上一张照片**的结果。
    这类"读到别人的结果"在界面上极像"点了没反应 / 结果莫名其妙"。
    """
    _onePatch(running=True, phase="loading_model", photoCode=str(photoCode),
              replaceFaces=bool(replaceFaces), match=bool(doMatch),
              faceCount=0, rawCount=0, deletedFaces=0, confirmedBefore=0,
              droppedLinks=0, centroidsRecomputed=0, engineElapsed=0.0,
              matchRan=False, matched=0, autoAssigned=0, needReview=0,
              unknown=0, assignedTo=[], matchNote="",
              startedYMDHMS=misc.getTime(), finishedYMDHMS=None,
              lastError="", message="", log=[], elapsed=0.0)


def _oneSnapshot() -> dict:
    with _ONE_LOCK:
        out = dict(_ONE)
    out["assignedTo"] = list(out.get("assignedTo") or [])
    out["log"] = list(out.get("log") or [])[-20:]
    out["phaseText"] = _ONE_PHASE_TEXT.get(str(out.get("phase") or "idle"), "")
    out["ok"] = True
    # 谁占着写锁：抢不外乎「扫描 / 认脸 / 匹配」三者之一，界面上要说得出
    out["busyWriteLock"] = scanSched.writeLockOwner()
    return out


def _blockedOwner() -> str:
    """当前占着单写入者门闩的人（"" = 空闲）。

    一个出口就够：扫描 / 人脸提取 / 匹配**共用** `scanScheduler` 的模块级
    `_ACTIVE_STATE`（FaceScheduler 正是靠继承拿到这把闩的），所以
    `writeLockOwner()` 同时覆盖三者的「正在写库」。
    """
    return str(scanSched.writeLockOwner() or "")


def _loadPhotoRow(photoCode: str) -> dict:
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=str(photoCode))
    if not rows:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "照片不存在或已软删除: %s" % photoCode)
    return rows[0]


def _photoAbsPath(photoRow: dict) -> str:
    """pb_photo 行 -> 原图绝对路径（与 faceStore.loadPhotoRows / pool 同一算法）"""
    relPath = str((photoRow or {}).get("relPath") or "")
    if not relPath:
        return ""
    return os.path.join(os.path.abspath(paths.photo_dir()),
                        relPath.replace("/", os.sep))


def _personNames() -> dict:
    """personCode -> displayName（一次取回；人物数量级是几百，不是十万）。"""
    out = {}
    for row in sqliteCommon.query_pb_person("pb_person", mode="light"):
        code = str(row.get("personCode") or "")
        if code:
            out[code] = str(row.get("displayName") or code)
    return out


def _photoFaceSnapshot(photoCode: str) -> dict:
    """重提取**之前**这张照片的人脸概览（要被清掉的东西，得先数清楚）。

    返回 {faces, confirmed, persons[], pairs[(person, bucket)]}
    """
    faces = sqliteCommon.query_pb_face("pb_face", photoCode=str(photoCode),
                                       mode="light")
    persons, pairs, confirmed = set(), set(), 0
    for row in faces or ():
        if int(row.get("isConfirmed") or 0):
            confirmed += 1
        person = str(row.get("personCode") or "")
        if not person:
            continue
        persons.add(person)
        bucketKey = str(row.get("shotBucket") or "")
        if bucketKey:
            pairs.add((person, bucketKey))
    return {"faces": len(faces or ()), "confirmed": confirmed,
            "persons": sorted(persons), "pairs": sorted(pairs)}


def _dropOrphanLinks(photoCode: str) -> int:
    """清掉这张照片的「幽灵关联」：关联行指向的人在这张照片里已无脸。

    ⚠️ 为什么必须做：`faceStore._dropOldFaces` 只删 pb_face，**不碰**
    pb_photo_person。重提取之后那张关联行还在，症状是
    「照片详情说这张照片没人」（persons 由 faces 派生，browse.getPhoto）
    而「人物时间轴里还有这张照片、点进去一张脸都没有」——
    `assigner.verifyLinks` 会把它判成 orphanLink。
    """
    dropped = 0
    for row in sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                  photoCode=str(photoCode)):
        person = str(row.get("personCode") or "")
        if not person:
            continue
        if assigner.facesInPhotoFor(photoCode, person) == 0:
            dropped += int(assigner.dropLink(photoCode, person) or 0)
    return dropped


def _recomputePersons(personCodes) -> int:
    """重算一批人物的质心（他们在这张照片上的样本刚被删掉）。返回成功人数。

    为什么必须重算：质心只由 isConfirmed=1 的样本生成（DR-16）。样本删了而
    质心没重算，那条向量会继续参与匹配 —— 表现是「越用越不准」，且库里
    看不出任何异常（sampleCount 还停在旧值，看着挺健康）。

    单人失败不拖垮整体：例如那个人脸的桶键与他的生日对不上时，
    `assertFacesFresh` 会抛 BucketStaleError —— 记日志、继续下一个人。
    """
    done = 0
    for code in sorted(set(str(c) for c in (personCodes or ()) if c)):
        try:
            matchCentroid.recomputePerson(code)
            done += 1
        except Exception as e:                              # noqa: BLE001
            _LOG.warning("face/extract-one: 重算人物质心失败 %s: %s: %s",
                         code, type(e).__name__, e)
    return done


def _matchThisPhoto(photoCode: str) -> dict:
    """只判**这一张照片**里尚无归属的脸。返回统计，不抛（失败写进 note）。

    为什么不直接调 `/match/run`：那是全库端点，带一个模块级任务快照
    （api/match.py 的 `_TASK`）—— 从后台线程里调它会污染全局状态，
    也会把整库待确认队列一起跑掉。这里复用它的**下层实现**：
    `matcher.matchMany`（纯计算）+ `assigner.applyAuto`（落库 + 日志 + 重算质心）。
    """
    out = {"ran": False, "total": 0, "auto": 0, "review": 0, "cluster": 0,
           "assignedTo": [], "note": ""}

    # 口径唯一出处：processor.review.queue（personCode IS NULL + isStranger=0）
    rows = reviewQueue.loadPendingRows(photoCode=str(photoCode))
    if not rows:
        out["note"] = "这张照片没有需要判定的脸"
        return out

    matrix, index = matchCentroid.loadAllCentroids()
    if not int(index.summary().get("vectors") or 0):
        # 不是故障，是防污染规则的正常结果：没有人工确认过就没有质心
        out["note"] = ("库里还没有可用的质心（质心只由人工确认的样本生成），"
                       "本次只完成了人脸检测 —— 先去「待确认」确认一些脸再试")
        return out

    results = matcher.matchMany(rows, matrix=matrix, index=index)
    stat = matcher.summarize(results)
    out.update({"ran": True, "total": len(rows),
                "auto": int(stat.get("auto") or 0),
                "review": int(stat.get("review") or 0),
                "cluster": int(stat.get("cluster") or 0)})
    if out["auto"] > 0:
        applied = assigner.applyAuto(results)
        _oneLog("匹配：自动归属 %d 张（落库 %d 条，重算质心桶 %d 个）"
                % (out["auto"], int(applied.get("written") or 0),
                   int(applied.get("centroids") or 0)))
    names = _personNames()
    out["assignedTo"] = [
        {"faceCode": one.faceCode, "personCode": one.personCode,
         "displayName": names.get(one.personCode, one.personCode),
         "score": (None if one.score is None else round(float(one.score), 4))}
        for one in results
        if one.decision == matcher.DECISION_AUTO and one.personCode]
    return out


class FaceOneBody(BaseModel):
    """POST /api/facerec/extract-one"""
    photoCode: str = Field(default="", description="要识别的照片（必填）")
    replaceFaces: bool = Field(
        default=True,
        description="重提取：先删这张照片的旧人脸行再插。**缺省 true** —— "
                    "按钮的语义就是「重新识别一遍」，代价是这张照片上已有的"
                    "人工确认与自动归属都会被清掉（界面负责事前预告）")
    match: bool = Field(
        default=True,
        description="提取完顺便判「是谁」（**只判这张照片里的脸**，不动全库队列）")


@router.post("/facerec/extract-one", summary="识别这一张照片的人脸（单张；后台线程）")
def extractOneFace(body: FaceOneBody = None) -> dict:
    """识别**当前这一张**照片：检测 + 提特征 +（可选）判是谁。

    返回 { ok, started, photoCode, remainingPhotos, poll }。请求内**不做推理**：
    首次要加载模型（3~10s）、单张 0.2~1s，同步跑会把 HTTP 挂住。

    ⚠️ 前置校验一律在起线程之前做完（依赖、门闩、照片在不在）：
    失败要立刻以 4xx/409 说清楚，而不是起一个线程让它在后台报错 ——
    那种「后台静默失败」在界面上与「跑完了但没结果」完全一样。
    """
    payload = body or FaceOneBody()
    photoCode = str(payload.photoCode or "").strip()
    if not photoCode:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "photoCode 必填")

    # ---- ① 单张任务自身互斥（同时只识别一张）----
    if _oneRunning():
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "上一张照片还在识别中，等它结束",
                           extra={"photoCode": _ONE.get("photoCode")})

    # ---- ② 与全库任务互斥：直接拒绝（用户选的语义是"不排队、不覆盖"）----
    owner = _blockedOwner()
    if owner:
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "已有任务在跑: %s（单写入者不允许并发；"
                           "等它结束再识别这张）" % owner,
                           extra={"activeJobCode": owner})

    # ---- ③ 依赖自检：缺 onnxruntime/cv2 时子进程会静默失败 ----
    pre = faceSched.preflight()
    if not pre.get("ok"):
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           pre.get("hint") or "人脸识别依赖缺失",
                           extra={"missing": [list(i) for i in pre.get("missing") or ()]})

    # ---- ④ 照片必须存在，且原图在盘上（拔盘/挪走了就别白跑一遍模型）----
    photoRow = _loadPhotoRow(photoCode)
    absPath = _photoAbsPath(photoRow)
    if not absPath or not os.path.isfile(absPath):
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "原图不在磁盘上: %s" % (absPath or "（relPath 为空）"),
                           extra={"photoCode": photoCode})

    thread = threading.Thread(
        target=_runExtractOne,
        args=(photoCode, photoRow, absPath, bool(payload.replaceFaces), bool(payload.match)),
        name="pb-face-extract-one", daemon=True)
    _oneReset(photoCode, bool(payload.replaceFaces), bool(payload.match))
    try:
        thread.start()
    except Exception as e:                                  # noqa: BLE001
        # 起不来就必须把 running 放回去，否则状态永远停在"识别中"
        _onePatch(running=False, phase="error",
                  lastError="起后台线程失败: %s: %s" % (type(e).__name__, e),
                  finishedYMDHMS=misc.getTime())
        raise dto.ApiError(dto.CODE_DB_ERROR,
                           "起后台线程失败: %s" % e)
    _LOG.info("face/extract-one: %s 后台启动（replace=%s match=%s）",
              photoCode, payload.replaceFaces, payload.match)
    return dto.okBody(started=True, running=True, photoCode=photoCode,
                      replaceFaces=bool(payload.replaceFaces),
                      match=bool(payload.match),
                      remainingPhotos=faceSched.countPendingPhotos(),
                      poll="/api/facerec/extract-one/status")


@router.get("/facerec/extract-one/status", summary="单张识别的进度（轮询用）")
def extractOneStatus() -> dict:
    """轮询这个。阶段是真的（检测 / 写入 / 清旧归属 / 比对），计数来自真实行数。

    `assignedTo` 是本次**自动归属**的结果（谁被判成了谁 + 分数）——
    自动归属是 isConfirmed=0（「我不同意」），界面上要说得出它认了谁，
    否则用户只能看到一个「识别完成」而不知道机器做了什么。
    """
    return _oneSnapshot()


def _runExtractOne(photoCode: str, photoRow: dict, absPath: str,
                   replaceFaces: bool, doMatch: bool) -> None:
    """后台线程体：提取 -> 落库 -> 清旧归属 ->（可选）匹配这一张。

    ⚠️ 门闩**必须**在 finally 里释放：漏一次，之后所有扫描 / 认脸 / 匹配
    都会被「已有任务在跑」永久挡下，只能重启进程（api/match.py 的血泪）。
    """
    started = time.time()
    locked = False
    # 快照的初值由端点里的 _oneReset() 同步写好（避免"读到上一张的结果"），
    # 这里只推进阶段。
    try:
        if not scanSched.acquireWriteLock(_ONE_LOCK_OWNER):
            _onePatch(running=False, phase="error",
                      lastError="抢单写入者门闩失败：%s 正在写库"
                                % (scanSched.writeLockOwner() or "?"),
                      finishedYMDHMS=misc.getTime())
            return
        locked = True

        # ---- ① 先记下「这张照片原来挂着谁」：重提取会把它一并清掉 ----
        before = _photoFaceSnapshot(photoCode)
        _onePatch(confirmedBefore=int(before["confirmed"]))
        if replaceFaces and before["faces"]:
            _oneLog("重提取：这张照片原有 %d 张人脸（其中人工确认 %d 张），已一并清除"
                    % (before["faces"], before["confirmed"]))

        # ---- ② 提特征（单张同步；引擎首次加载模型 3~10s）----
        _onePatch(phase="loading_model")
        engine = faceEngine.getEngine(**_scheduler().engineKwargs())
        _onePatch(phase="extracting")
        result = engine.extract(absPath, photoCode)
        if not result.get("ok"):
            raise RuntimeError("提取失败：%s" % (result.get("errMsg") or "未知原因"))
        _onePatch(rawCount=int(result.get("rawCount") or 0),
                  engineElapsed=round(float(result.get("elapsed") or 0.0), 2))
        _oneLog("检出 %d 张人脸（检测到 %s 张，用时 %.2fs）"
                % (len(result.get("faces") or ()), result.get("rawCount"),
                   float(result.get("elapsed") or 0.0)))

        # ---- ③ 落库（replaceFaces = 先删这张照片的旧人脸行再插）----
        # 用 commitOne（单张一步到位）而不是 extractAndStore：后者要建进程池，
        # 而起池的代价（spawn N 个进程 + 各自加载模型）远大于"一张脸"的收益。
        _onePatch(phase="storing")
        storeStat = faceStore.commitOne(
            photoRow, result, dbFile=paths.db_file(),
            thumbRoot=paths.thumb_dir(), photoRoot=paths.photo_dir(),
            replaceFaces=bool(replaceFaces))
        _onePatch(faceCount=int(storeStat.get("faces") or 0),
                  deletedFaces=int(storeStat.get("deleted") or 0))

        # ---- ④ 清旧归属：幽灵关联 + 受影响人物的质心 ----
        _onePatch(phase="cleanup")
        droppedLinks = _dropOrphanLinks(photoCode)
        recomputed = _recomputePersons(before["persons"])
        _onePatch(droppedLinks=int(droppedLinks), centroidsRecomputed=int(recomputed))
        if droppedLinks or recomputed:
            _oneLog("清理旧归属：清掉 %d 条幽灵关联，重算 %d 个人物的质心"
                    % (droppedLinks, recomputed))

        # ---- ⑤ 匹配（只判这张照片刚提取出的待确认脸）----
        mstat = {"ran": False, "total": 0, "auto": 0, "review": 0,
                 "cluster": 0, "assignedTo": [], "note": ""}
        if doMatch:
            _onePatch(phase="matching")
            mstat = _matchThisPhoto(photoCode)
            _onePatch(matchRan=bool(mstat["ran"]), matched=int(mstat["total"]),
                      autoAssigned=int(mstat["auto"]),
                      needReview=int(mstat["review"]),
                      unknown=int(mstat["cluster"]),
                      assignedTo=list(mstat["assignedTo"] or []),
                      matchNote=str(mstat["note"] or ""))

        # ---- ⑥ 收尾：把「一共做了什么」写成人话 ----
        elapsed = round(time.time() - started, 2)
        _onePatch(running=False, phase="done", elapsed=elapsed,
                  finishedYMDHMS=misc.getTime(),
                  message=_oneResultText(int(storeStat.get("faces") or 0),
                                        int(storeStat.get("deleted") or 0),
                                        mstat if doMatch else None))
        _LOG.info("face/extract-one: %s 完成（人脸 %d，删除旧行 %d，自动归属 %d，"
                  "用时 %.2fs）", photoCode, int(storeStat.get("faces") or 0),
                  int(storeStat.get("deleted") or 0), int(mstat.get("auto") or 0),
                  elapsed)
    except Exception as e:                                  # noqa: BLE001
        errText = "%s: %s" % (type(e).__name__, e)
        _LOG.error("face/extract-one %s 失败: %s", photoCode, errText)
        _oneLog("出错：%s" % errText)
        _onePatch(running=False, phase="error", lastError=errText,
                  elapsed=round(time.time() - started, 2),
                  finishedYMDHMS=misc.getTime())
    finally:
        if locked:
            scanSched.releaseWriteLock()


def _oneResultText(faceCount: int, deletedFaces: int, mstat: dict = None) -> str:
    """把结果拼成一句人话（漏掉任何一档都会让用户以为「点了没反应」）"""
    parts = ["检出 %d 张人脸" % faceCount]
    if deletedFaces:
        parts.append("已清除原有 %d 张" % deletedFaces)
    if mstat is None:
        return "；".join(parts)
    if mstat.get("ran"):
        parts.append("自动归属 %d 张" % int(mstat.get("auto") or 0))
        if int(mstat.get("review") or 0):
            parts.append("仍需确认 %d 张" % int(mstat.get("review") or 0))
        if int(mstat.get("cluster") or 0):
            parts.append("没认出的 %d 张" % int(mstat.get("cluster") or 0))
    elif mstat.get("note"):
        parts.append(str(mstat.get("note")))
    return "；".join(parts)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("face.py _VERSION:", _VERSION)
    print("库:", paths.db_file())
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    try:
        _s = _scheduler()
        print("待识别照片:", faceSched.countPendingPhotos())
        print("任务数:", len(_s.listJobs(5)))
        for one in _s.listJobs(3):
            print("  ", _s.describe(one["jobCode"])["text"])
    except dto.ApiError as e:
        print("[未就绪]", e.code, e.message)
