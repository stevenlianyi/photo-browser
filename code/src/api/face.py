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
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from schedule import faceScheduler as faceSched                   # noqa: E402

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
                               "人脸识别表未就绪：%s（先跑 tools\\build_db.py --migrate）" % e)
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
