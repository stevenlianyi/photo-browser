#! /usr/bin/env python3
#encoding: utf-8

#Filename: eraZip.py
#Description: photo-browser 年代档打包接口（R15 · DR-56 ~ DR-58）—— P-05 Tab1
#
# 为什么自成一个模块（而不是塞进 api/browse.py 或 api/eraSample.py）
# -----------------------------------------------------------------
#   · `api/browse.py` 的文件头第 ② 条纪律是「**整个 browse 模块一个字都不写**」，
#     而本功能要起后台任务、落 zip 产物 —— 塞进去会让那条纪律变成一句谎话；
#   · `api/eraSample.py` 是「按年抽样」的命名空间（读写 pb_era_sample*），
#     本功能**按档全量、不落库**，两者的状态模型完全不同
#     （抽样有批次行可回写，打包只有「文件在不在」+ 进程内任务态）。
#   与 api/place.py 同理：功能自成一体的命名空间就自成一个模块。
#
# 端点（4 个 + 1 个停止）
# ---------------------
#   GET    /api/persons/{personCode}/era-zips                该人各年代档的现状 + 体积预估
#   POST   /api/persons/{personCode}/era-zips/{bucketKey}    起打包任务（异步，立刻返回）
#   GET    /api/era-zips/status/{jobCode}                    进度轮询（按张数）
#   POST   /api/era-zips/stop                                请求停止（张数边界生效）
#   GET    /api/persons/{personCode}/era-zips/{bucketKey}/download   下载 zip
#
# 四条纪律
# --------
#   ① **不写任何业务表**（DR-56）：只读 pb_face/pb_photo，产物落 `exports\era-zip\`；
#      进度是**进程内字典**（与 api/settings.py 的重算任务同一条理由：
#      落表会出现「重启后永远停在运行中的僵尸任务」，用户没有办法结束它）。
#   ② **不抢单写入者门闩**（DR-57）：本功能不写 pb_face/pb_photo/pb_person_centroid，
#      而项目是 WAL（`config/sqliteSettings.py` 的注释：「写不阻塞读 ——
#      扫描时前端还能正常浏览」）⇒ 打包与扫描/认脸**天然不冲突**。
#      抢闩等于把一个只读任务错误地归类成写业务表的任务，
#      还会让一个 1~2 分钟的只读任务把扫描全挡住。
#   ③ **错误一律 {code, message}**：engine/media 的异常在这里映射成 400/404/409。
#   ④ **不提供 DELETE**（DR-51）：全站没有DELETE 端点，这是被
#      `test_api_smoke.test_openapiHasNoExportOrDelete` 逐条扫 OpenAPI
#      守住的**项目级不变量**。同理路径里**不含 `export`** 字样。

import os
import sys
import threading
import time
import uuid
from typing import Optional

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))           # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                            # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import browse as browseApi                               # noqa: E402
from api import dto                                               # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from engine.sample import bucketPool as bucketPool                # noqa: E402
from processor.media import eraPack as eraPack                    # noqa: E402

from fastapi import APIRouter                                    # noqa: E402
from fastapi.responses import FileResponse                       # noqa: E402

_VERSION = "20261013"

_LOG = misc.setLogNew("apiEraZip", "apierazip.log")

router = APIRouter(tags=["eraZip"])

#: jobCode 前缀（`ZP_` = zip pack）。与 `ES_`（抽样）/ `RL_`（纠错日志）
#: 同一生成风格 —— 别让同一个项目里出现两套编码风格。
JOB_CODE_PREFIX: str = "ZP"

#: 打包中允许的停止信号检查间隔（秒）—— 与进度节流共用，避免每张都读时间。
_PROGRESS_MIN_INTERVAL: float = float(basicSettings.ERA_PACK_PROGRESS_EVERY)


# ============================================================
# 一、任务状态（进程内，**全局单任务**）
# ============================================================
#: 为什么是模块级字典而不是落一张表（与 api/settings.py / api/match.py 同理由）：
#   · 它是**瞬时状态**：进程退出就没了（届时正需要重打一次）；
#   · 落表会出现「重启后永远停在打包中的僵尸任务」，用户没有任何办法结束它。
#: 为什么**全局只有一个**（用户 Q-1 选定）：打包是磁盘 IO 密集，
#   一档可达 GB 级；同时开两个既拖慢彼此，也会与扫描抢磁盘。
#   串行的体验反而更可预期（列表上直接显示「已打包 47/171」）。
_TASK_LOCK = threading.Lock()
_TASK: dict = {
    "running": False,
    "jobCode": None,
    "state": "idle",              # idle | BUILDING | READY | FAILED | CANCELLED
    "personCode": None,
    "personName": "",
    "bucketKey": None,
    "bucketText": "",
    "total": 0,
    "processed": 0,
    "zipSize": 0,
    "zipPhotoCount": 0,
    "missing": 0,
    "cached": False,
    "force": False,
    "errMsg": "",
    "startedYMDHMS": None,
    "finishedYMDHMS": None,
    "stopEvent": None,            # threading.Event；**不进对外响应**
}


def _patchTask(**fields) -> None:
    with _TASK_LOCK:
        _TASK.update(fields)


def taskSnapshot() -> dict:
    """对外可见的任务快照（**摘掉 stopEvent** —— 它不可序列化，也不该外露）。"""
    with _TASK_LOCK:
        out = {key: value for key, value in _TASK.items() if key != "stopEvent"}
    total = int(out.get("total") or 0)
    processed = int(out.get("processed") or 0)
    out["percent"] = (min(100.0, round(processed * 100.0 / total, 2))
                      if total else 0.0)
    return out


def _newJobCode() -> str:
    """`ZP_<yyyymmddHHMMSS>_<6 位 hex>`（与 basicSettings.ERA_SAMPLE_CODE_PREFIX 同法）。"""
    return "%s_%s_%s" % (JOB_CODE_PREFIX, misc.getTime(), uuid.uuid4().hex[:6])


def _busyMessage() -> str:
    """已经在打包时的可读提示（点名正在打哪个档、到哪了）。"""
    with _TASK_LOCK:
        name = str(_TASK.get("bucketText") or _TASK.get("bucketKey") or "?")
        done, total = int(_TASK.get("processed") or 0), int(_TASK.get("total") or 0)
    return ("已有打包任务在跑：%s（%d/%d）—— 一次只打一个年代档，请等它结束"
            % (name, done, total))


# ============================================================
# 二、响应整形
# ============================================================

def _personOf(personCode: str) -> dict:
    """校验人物存在并取显示名（不存在 -> 404）。"""
    row = browseApi.personRow(str(personCode or "").strip(), withDeleted=True)
    if not row:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "personCode=%s 在 pb_person 里不存在" % personCode)
    return row


def _staleOf(info: dict, photoTotal: int) -> bool:
    """包是否过期 = **打包时的照片集**与现在的不一致。

    ⚠️ 判据是包内 `_meta.json` 记的 `photoTotalAtPack`，**不是**包内实际文件数：
       原图缺失时后者天然更小 —— 拿它当判据会让这个界面**永远显示「已过期」**，
       而用户重打一百次也没用（重打出来还是那么多）。
    ⚠️ 读不到 meta（老包 / 被外部改过）时才退到「张数不等」：
       宁可多提示一次重打，也不要把一个真的过期包当成新的。
    """
    if not info.get("exists"):
        return False
    atPack = int(info.get("photoTotalAtPack") or 0)
    if atPack:
        return atPack != int(photoTotal)
    return int(info.get("zipPhotoCount") or 0) != int(photoTotal)


def _bucketBody(personCode: str, rows: list) -> list:
    """照片池 -> 每个年代档一条（含 zip 现状与体积预估）。

    `stale` = 「打包时那一档的照片集 ≠ 现在这一档的照片集」→ 建议重打。
    它是本功能唯一的「过期」信号，也是**不落库**方案能给出的全部诚实交代。
    """
    out = []
    for one in bucketPool.summaryOf(rows):
        key = one["bucketKey"]
        info = eraPack.zipInfoOf(personCode, key)
        stale = _staleOf(info, one["photoTotal"])
        out.append({
            "bucketKey": key,
            # 时间轴的 groups 用的是**另一套键**（空桶写作「(无拍摄年份)」），
            # 前端要靠它把横带与打包项对上（见 bucketPool.timelineKeyOf）。
            "timelineKey": bucketPool.timelineKeyOf(key),
            "bucketText": one["bucketText"],
            "photoTotal": one["photoTotal"],
            "totalBytes": one["totalBytes"],
            "missingCount": one["missingCount"],
            "zipExists": bool(info["exists"]),
            "zipSize": int(info["zipSize"]),
            "zipYMDHMS": info["zipYMDHMS"],
            "zipPhotoCount": int(info["zipPhotoCount"]),
            "stale": stale,
            "downloadUrl": ("/api/persons/%s/era-zips/%s/download" % (personCode, key))
                           if info["exists"] else None,
        })
    return out


# ============================================================
# 三、端点
# ============================================================

@router.get("/persons/{personCode}/era-zips",
            summary="某人的年代档打包现状（各档张数 / 体积预估 / zip 是否已生成）")
def listEraZips(personCode: str):
    """Tab1 时间轴的数据补充：每一档「有多少张、约多大、打没打过」。

    ⚠️ `photoTotal` 是**排除重复件后**的张数（用户口径），
       所以它会比 Tab1 的 `photoTotal` 小 —— 界面上必须把这两个数分开讲，
       否则用户会以为包打少了（见 plan/年代打包规划.md §6.3）。
    """
    row = _personOf(personCode)
    code = str(row.get("personCode"))
    rows = bucketPool.poolRows(code, excludeDuplicate=True)
    return {"ok": True, "personCode": code,
            "displayName": str(row.get("displayName") or ""),
            "excludeDuplicate": True,
            "exports": eraPack.exportsUsage(),
            "running": taskSnapshot() if _TASK.get("jobCode") else None,
            "buckets": _bucketBody(code, rows)}


@router.post("/persons/{personCode}/era-zips/{bucketKey}",
             summary="打包某个年代档的全部照片（异步；立刻返回 jobCode）")
def startEraZip(personCode: str, bucketKey: str,
                body: Optional[dto.EraZipBody] = None):
    """起一次打包。**立刻返回**，进度看 `/api/era-zips/status/{jobCode}`。

    ⚠️ 照片列表在**请求线程里固定成快照**再交给后台线程：
       打包是「先查列表、再逐张读盘」，中途用户改判归属会重刷 shotBucket ——
       边查边打会让包里的内容与列表对不上（而包本身完全合法）。
    """
    row = _personOf(personCode)
    code = str(row.get("personCode"))
    try:
        key = bucketPool.normalizeKey(bucketKey)
    except bucketPool.BucketPoolError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    force = bool(body and body.force)
    # ⚠️ 判空与取文案**分两步**：`_busyMessage()` 自己也要拿 `_TASK_LOCK`，
    #    而它是 `threading.Lock`（**不可重入**）—— 在锁里调它 = 死锁，
    #    表现是「一点打包就卡住，日志里什么都没有」。这个坑被
    #    test_startWhenBusyReturns409 抓着过。
    with _TASK_LOCK:
        busy = bool(_TASK["running"])
    if busy:
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL, _busyMessage())

    session = eraPack.zipInfoOf(code, key)
    if session["exists"] and not force:
        # 幂等短路：已有产物就直接告诉调用方「去下载」，不起线程。
        return {"ok": True, "jobCode": None, "cached": True, "started": False,
                "personCode": code, "bucketKey": key,
                "bucketText": bucketPool.bucketTextOf(key),
                "zipSize": session["zipSize"],
                "zipPhotoCount": session["zipPhotoCount"],
                "downloadUrl": "/api/persons/%s/era-zips/%s/download" % (code, key)}

    rows = bucketPool.poolRows(code, excludeDuplicate=True)
    photos = bucketPool.photosOfBucket(rows, key)
    if not photos:
        # 空档不打包：只会生成一个「只有 manifest/README」的 zip，
        # 而用户以为照片导出成功了（与 eraSample 空批次的判据同一条）。
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "年代档 %s 下没有照片，无法打包" % bucketPool.bucketTextOf(key))

    needBytes = eraPack.estimateBytes(photos)
    try:
        # 提前失败：磁盘不够就**不要开始**（打到 99% 才满会留下半截文件，
        # 用户只看到「失败」，真正的原因却看不见）。
        disk = eraPack.ensureDiskSpace(needBytes)
    except eraPack.EraPackError as e:
        raise dto.ApiError(dto.CODE_INTERNAL, str(e))

    jobCode = _newJobCode()
    personName = str(row.get("displayName") or "")
    bucketText = bucketPool.bucketTextOf(key)
    stopEvent = threading.Event()
    _patchTask(running=True, jobCode=jobCode, state="BUILDING",
               personCode=code, personName=personName,
               bucketKey=key, bucketText=bucketText,
               total=len(photos), processed=0, zipSize=0, zipPhotoCount=0,
               missing=0, cached=False, force=force, errMsg="",
               startedYMDHMS=misc.getTime(), finishedYMDHMS=None,
               stopEvent=stopEvent)

    thread = threading.Thread(
        target=_runPack,
        args=(jobCode, code, personName, key, bucketText, photos, force, stopEvent),
        name="pb-era-zip-%s" % key, daemon=True)
    thread.start()
    _LOG.info("起打包任务 %s: %s/%s %d 张 约 %.2f GB（磁盘可用 %.2f GB）",
              jobCode, code, key, len(photos),
              needBytes / 1073741824.0, disk["free"] / 1073741824.0)
    return {"ok": True, "started": True, "cached": False, "jobCode": jobCode,
            "personCode": code, "personName": personName,
            "bucketKey": key, "bucketText": bucketText,
            "total": len(photos), "estBytes": needBytes,
            "statusUrl": "/api/era-zips/status/%s" % jobCode}


@router.get("/era-zips/status/{jobCode}",
            summary="年代档打包进度（按张数：processed/total）")
def eraZipStatus(jobCode: str):
    """轮询用。**单任务**语义：jobCode 不匹配就 404（前端据此停止轮询）。

    为什么不返回「当前任务」而不管 jobCode：那样前端拿到的是**另一个包**的进度，
    而它会照着那个数字画进度条 —— 一个看起来完全合理、只是错的界面。
    """
    code = str(jobCode or "").strip()
    with _TASK_LOCK:
        current = str(_TASK.get("jobCode") or "")
    if not code or code != current:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "jobCode=%s 不是当前打包任务（或已被新的任务取代）" % jobCode)
    return dict(taskSnapshot(), ok=True)


@router.post("/era-zips/stop", summary="请求停止当前打包（张数边界生效）")
def stopEraZip():
    """置停止位。**不打断正在写的那一张** —— 在下一张的进度回调里生效。"""
    with _TASK_LOCK:
        event = _TASK.get("stopEvent")
        running = bool(_TASK.get("running"))
    if not running or event is None:
        return {"ok": True, "stopped": False, "message": "当前没有打包任务在跑"}
    event.set()
    return {"ok": True, "stopped": True, "message": "已请求停止，将在当前照片写完后生效"}


@router.get("/persons/{personCode}/era-zips/{bucketKey}/download",
            summary="下载某个年代档的 zip（未生成则先起打包任务）")
def downloadEraZip(personCode: str, bucketKey: str, force: bool = False):
    """下载 zip。

    ⚠️ 与抽样包（`GET /api/era-samples/{batchCode}/zip`）**刻意不同**：
       抽样包几十张，未生成时**同步先打**是合理的；年代档包可以到 GB 级，
       同步打会让浏览器干等一两分钟。所以这里未生成时**起异步任务并回 409**
       （带 jobCode），前端据此切到进度条 —— 既不让人干等，也不假装成功。
    """
    row = _personOf(personCode)
    code = str(row.get("personCode"))
    try:
        key = bucketPool.normalizeKey(bucketKey)
    except bucketPool.BucketPoolError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    target = eraPack.bucketZipAbsPath(code, key)
    if not force and os.path.isfile(target):
        return FileResponse(
            target, media_type="application/zip",
            filename=eraPack.downloadBucketName(str(row.get("displayName") or ""),
                                                bucketPool.bucketTextOf(key)))
    # 未生成：起任务（若空闲）并把 jobCode 交给前端。
    started = None
    try:
        started = startEraZip(code, key, dto.EraZipBody(force=force))
    except dto.ApiError as e:
        if e.code != dto.CODE_TASK_STATE_ILLEGAL:
            raise
        # 已有别的档在打：**如实说**，别让人以为自己在等这一档
        raise dto.ApiError(e.code,
                           "这个年代档还没有打包产物，且%s" % e.message,
                           extra={"jobCode": None})
    raise dto.ApiError(
        dto.CODE_TASK_STATE_ILLEGAL,
        "这个年代档还没有打包产物，已开始打包，请等进度完成后下载",
        extra={"jobCode": started.get("jobCode"),
               "statusUrl": started.get("statusUrl")})


# ============================================================
# 四、后台线程
# ============================================================

def _runPack(jobCode: str, personCode: str, personName: str, bucketKey: str,
             bucketText: str, photos: list, force: bool, stopEvent) -> None:
    """后台线程体。**任何异常都落到任务态，绝不逃出线程**。"""
    lastTick = {"at": 0.0}

    def onProgress(done: int, total: int) -> None:
        now = time.time()
        # 节流：前端 1.2 秒才问一次，每张都写没必要（与 faceScheduler 的
        # _onProgress 同一手法）。终值（done == total）**必须写**。
        if done < total and _PROGRESS_MIN_INTERVAL > 0 \
                and (now - lastTick["at"]) < _PROGRESS_MIN_INTERVAL:
            return
        lastTick["at"] = now
        _patchTask(processed=int(done), total=int(total))
        if stopEvent is not None and stopEvent.is_set():
            # 取消：抛出去让 _buildBucket 的 finally 清掉临时文件。
            # 任务态由下面的 except 统一收敛。
            raise eraPack.EraPackError("已请求停止（已打包 %d 张）" % int(done))

    try:
        info = eraPack.ensureBucketZip(
            personCode, personName, bucketKey, bucketText, photos,
            force=force, onProgress=onProgress, jobCode=jobCode)
    except Exception as e:                                        # noqa: BLE001
        errText = "%s: %s" % (type(e).__name__, e)
        cancelled = (stopEvent is not None and stopEvent.is_set())
        _LOG.error("打包 %s 失败: %s（%s/%s）", jobCode, errText, personCode, bucketKey)
        _patchTask(running=False, stopEvent=None,
                   state="CANCELLED" if cancelled else "FAILED",
                   errMsg=errText, finishedYMDHMS=misc.getTime())
        return

    _patchTask(running=False, stopEvent=None, state="READY",
               processed=int(info.get("zipPhotoCount") or 0),
               total=int(info.get("photoTotal") or 0),
               zipSize=int(info.get("zipSize") or 0),
               zipPhotoCount=int(info.get("zipPhotoCount") or 0),
               missing=int(info.get("zipMissingCount") or 0),
               cached=bool(info.get("cached")),
               errMsg="", finishedYMDHMS=misc.getTime())
    _LOG.info("打包完成 %s: %s/%s 打包 %s 张 / 缺失 %s 张 / %s 字节（cached=%s）",
              jobCode, personCode, bucketKey, info.get("zipPhotoCount"),
              info.get("zipMissingCount"), info.get("zipSize"), info.get("cached"))


def _selfCheck() -> bool:
    """模块自检：任务快照不泄漏 stopEvent、percent 边界、jobCode 形状。"""
    _patchTask(total=0, processed=0)
    snap = taskSnapshot()
    assert "stopEvent" not in snap, "任务快照泄漏了 stopEvent"
    assert snap["percent"] == 0.0
    _patchTask(total=171, processed=47)
    assert taskSnapshot()["percent"] == 27.49, taskSnapshot()["percent"]
    _patchTask(total=10, processed=99)
    assert taskSnapshot()["percent"] == 100.0, "进度百分比必须封顶 100"
    assert _newJobCode().startswith(JOB_CODE_PREFIX + "_")
    _patchTask(running=False, jobCode=None, total=0, processed=0, state="idle")
    return True


if __name__ == "__main__":
    print("apiEraZip _VERSION:", _VERSION)
    print("任务态自检:", _selfCheck())
