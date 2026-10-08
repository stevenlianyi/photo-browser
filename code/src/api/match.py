#! /usr/bin/env python3
#encoding: utf-8

#Filename: match.py
#Description: photo-browser 人脸匹配接口（步骤 6 的 Web 入口）—— 触发 / 轮询 / 预览与落库
#
# 为什么必须有这组端点（它补的是哪条断链）
# ----------------------------------------------------------------
#   「我不同意」= 机器自动归属（personCode 非空）但未经人工确认（isConfirmed=0）
#   的脸，它**只能**由 processor.review.assigner.applyAuto() 写出来；
#   而 applyAuto 唯一的调用方是 CLI `tools/run_match.py --run --assign`。
#   于是：用户确认了一批脸 -> 质心长起来了 -> 但「我不同意」永远是 0 ——
#   概览页那句「确认过一些脸之后，跑一次匹配就会出现…」在 Web 端无从执行，
#   用户唯一的办法是去命令行敲脚本，而那个前提（不敲就永远看不到结果）
#   **在界面上完全看不出来**。本模块把「载质心 -> 比对 -> 落库」这第三步
#   暴露成端点，让「扫描 -> 认脸 -> 匹配」三步都在同一个界面里跑完。
#
# 两个端点
# ----------
#   POST /api/match/run     跑一次匹配（后台轻量线程，立刻返回；可只预览不落库）
#   GET  /api/match/status  轮询真实阶段与计数（不做假进度）
#
# ⚠️ 为什么前缀是 /match 而不是 /face/...（**别改回去**）
# ------------------------------------------------------
#   `/api/face/{faceCode}` 是**人脸裁剪图**的路由（api/static.py，第一个
#   include 的 router）。api/face.py 的文件头记过一整轮教训：FastAPI 0.142
#   跨 router 不保证「静态段优先于参数段」，任何 /api/face/** 的静态段都可能
#   被 {faceCode} 接走、当成 faceCode 去查、查不到就 404，而统一错误处理把
#   它写成「接口不存在」—— 误导性极强。匹配与「人脸裁剪图」不是同一个
#   命名空间，另起 /match 既省事又不会再踩一次。
#
# 与 /facerec 的分工（一句话）
#     /facerec = 从照片里**抽出**人脸（写 pb_face，待确认队列由此产生）
#     /match   = 把已抽出、尚无归属的人脸**判给某个人**（写 personCode，
#                「我不同意」由此产生）
#
# 三条纪律
# --------
#   ① **只跑待确认队列**（`personCode IS NULL AND isStranger=0`）。口径直接复用
#      processor.review.queue（唯一定义处），不在这里重写 WHERE。
#      已自动归属的「我不同意」**不会被重跑** —— 那是「改了阈值之后重判」的语义
#      （CLI 的 `--all`），本端点刻意不提供：它会在用户没点任何东西的情况下
#      一次改写几万条他已经在界面上看过的结论。
#   ② **写库期间必须占单写入者门闩**。applyAuto 要写 pb_face /
#      pb_photo_person / pb_person_centroid 并逐张落 pb_review_log；与人脸提取
#      （FaceScheduler）或扫描并发就是 database is locked。门闩复用
#      scanScheduler 的模块级 _ACTIVE_STATE —— 人脸识别正是靠继承 ScanScheduler
#      拿到这把闩的，匹配不是任务，所以走它的公开取用口（acquireWriteLock）。
#   ③ **默认落库，但把结果全摆出来给用户看**。CLI 的 `--run` 默认不落库，
#      照搬到网页上就是「点了按钮，界面上什么都没变」—— 那正是本项目反复
#      踩过的「看起来成功了其实啥也没干」。所以这里 assign 缺省 True，
#      同时把 自动归属 / 落库 / 仍需确认 / 进聚类 / 「我不同意」前后值
#      全部回给前端；勾了「先预览」则只算不写，用户看过统计再确认落库。

import os
import sys
import threading
import time
from typing import Optional

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import dto                                               # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402
from engine.match import matcher as matcher                       # noqa: E402
from processor.review import assigner as assigner                 # noqa: E402
from processor.review import queue as reviewQueue                 # noqa: E402
from schedule import scanScheduler as scanSched                   # noqa: E402

from fastapi import APIRouter                                     # noqa: E402
from pydantic import BaseModel, Field                             # noqa: E402

_VERSION = "20261008"

_LOG = misc.setLogNew("apiMatch", "apimatch.log")

router = APIRouter(tags=["match"])

#: 抢闩时用的记号（不是任务，没有 jobCode；用它让报错信息说得清「谁在占着」）
_LOCK_OWNER: str = "MATCH"

#: 阶段 -> 给用户看的一句话。**阶段是真的**，不插值、不假装百分比：
#: 比对本身是一次矩阵乘，没有可以切片的中间态，编一个进度条就是骗人。
_PHASE_TEXT: dict = {
    "idle": "还没有跑过匹配",
    "loading_centroids": "正在装载质心…",
    "loading_faces": "正在读取待确认人脸…",
    "matching": "正在比对…",
    "writing": "正在写入自动归属…",
    "done": "已完成",
    "error": "出错终止",
}


# ============================================================
# 一、任务状态（进程内，单任务）
# ============================================================
#: 为什么是模块级字典而不是落一张表（与 api/settings.py 的重算任务同理由）：
#   · 它是**瞬时状态**：进程退出就没了（届时正需要重跑一次）；
#   · 落表会出现「重启后永远停在运行中的僵尸任务」，用户没有任何办法结束它。
_LOCK = threading.Lock()
_TASK: dict = {
    "running": False,
    "phase": "idle",
    "assign": True,
    "preset": "",
    "tLow": 0.0,
    "tHigh": 0.0,
    # ---- 输入 ----
    "pendingBefore": 0,       # 跑之前待确认人脸条数
    "disputedBefore": 0,      # 跑之前「我不同意」条数
    "total": 0,               # 本次参与判定的脸数
    # ---- 质心装载 ----
    "vectors": 0,
    "persons": 0,
    "centroidLoaded": 0,
    "centroidTotal": 0,
    # ---- 判定分布 ----
    "auto": 0,
    "review": 0,
    "cluster": 0,
    #: 原因码分布（matched / no_centroid / no_bucket / no_embedding / low_score）。
    #: 「同样是没认出来，为什么」必须能解释 —— 排障与界面提示都靠它。
    "byReason": {},
    # ---- 落库 ----
    "written": 0,
    "failed": 0,
    "recomputedBuckets": 0,
    # ---- 结果 ----
    "pendingAfter": 0,
    "disputedAfter": 0,
    "elapsed": 0.0,
    "startedYMDHMS": None,
    "finishedYMDHMS": None,
    "lastError": "",
    "message": "",
    "log": [],
}


def _patch(**fields) -> None:
    with _LOCK:
        _TASK.update(fields)


def _logLine(text: str) -> None:
    """往快照里追一行中文说明（上限 20 行，排障与「给用户看」共用）。"""
    with _LOCK:
        lines = list(_TASK.get("log") or [])
        lines.append("%s %s" % (misc.getTime(), text))
        _TASK["log"] = lines[-20:]


def isRunning() -> bool:
    with _LOCK:
        return bool(_TASK.get("running"))


def countEnabledCentroids() -> int:
    """「启用的质心桶」个数（有向量且样本数达下限）。

    ⚠️ 门槛必须与 engine.match.centroid.loadAllCentroids 一致，否则会出现
    「预检说有质心、装载后 0 条」这种自相矛盾的界面。下限的唯一出处是
    basicSettings.MIN_CENTROID_SAMPLES。
    """
    try:
        return int(query.selectValue(
            "SELECT COUNT(*) AS rowNum FROM pb_person_centroid"
            " WHERE centroid IS NOT NULL AND sampleCount >= %s",
            (int(basicSettings.MIN_CENTROID_SAMPLES),)) or 0)
    except Exception:                       # 表还没建 / 库没就绪：当作 0
        return 0


def _currentStates() -> dict:
    """当前四态计数（一次 SQL 聚合，毫秒级）。用于「跑之前 / 跑之后」对照。"""
    try:
        states = reviewQueue.countStates()
    except Exception:
        states = {"pending": 0, "disputed": 0, "confirmed": 0, "stranger": 0}
    return {"pending": int(states.get("pending") or 0),
            "disputed": int(states.get("disputed") or 0),
            "confirmed": int(states.get("confirmed") or 0),
            "stranger": int(states.get("stranger") or 0)}


def _snapshot(withCounts: bool = True) -> dict:
    """当前任务快照（前端轮询这个）。

    withCounts=False 时**不查库**：轮询跑动中的任务每秒一次，四个 COUNT
    在 10 万行的库上是白花的开销，而跑动中这几个数也没有意义
    （pending 正在变）。空闲时才查，页面因此永远看到的是**实时**的四态计数。
    """
    with _LOCK:
        out = dict(_TASK)
    out["log"] = list(out.get("log") or [])[-20:]
    out["phaseText"] = _PHASE_TEXT.get(str(out.get("phase") or "idle"), "")
    out["ok"] = True
    out["busyWriteLock"] = scanSched.writeLockOwner()
    out["enabledCentroids"] = countEnabledCentroids()
    out["minCentroidSamples"] = int(basicSettings.MIN_CENTROID_SAMPLES)
    out["presets"] = {name: dict(values)
                      for name, values in basicSettings.MATCH_THRESHOLD_PRESETS.items()}
    # 当前**生效**的阈值（设置页改过的话是进程内覆盖值）。tLow/tHigh 记的是
    # 「本次运行用的」，这两个不是一回事 —— 界面要同时说得出「上次用的是什么」
    # 与「现在会用什么」。
    effLow, effHigh = basicSettings.matchThresholds()
    out["effective"] = {"preset": str(basicSettings.MATCH_THRESHOLD_PRESET or ""),
                        "tLow": float(effLow), "tHigh": float(effHigh)}
    if withCounts and not out.get("running"):
        out["current"] = _currentStates()
    else:
        out["current"] = None
    return out


# ============================================================
# 二、请求体
# ============================================================

class MatchRunBody(BaseModel):
    """POST /api/match/run"""

    preset: Optional[str] = Field(
        default=None,
        description="阈值预设（conservative / aggressive / s0）；缺省用当前生效值")
    assign: bool = Field(
        default=True,
        description="**缺省 true = 跑完就写库**。false 只算不写（预览），"
                    "用户看过统计后再以 assign=true 确认 —— 两条路径的判定结果相同（幂等）")
    limit: Optional[int] = Field(
        default=0, description="最多判定多少张脸；0 = 待确认队列全量")


# ============================================================
# 三、POST /api/match/run
# ============================================================

@router.post("/match/run", summary="跑一次人脸匹配（后台轻量线程，立刻返回）")
def runMatch(body: MatchRunBody = None) -> dict:
    """把「待确认队列」里还没有归属的脸判给某人，自动归属的结果进入「我不同意」。

    ⚠️ 绝不阻塞：这一步是 numpy 矩阵乘 + 写库，10 万张量级下也可能要几十秒，
    同步跑会把 HTTP 请求挂在那里。所以永远走后台线程，前端轮询 /match/status。

    ⚠️ **前置校验一律在起线程之前做完**（依赖、门闩、质心、空队列）：
    失败要立刻以 4xx/409 说清楚原因，而不是起一个线程让它在后台报错 ——
    那种「后台静默失败」在界面上与「跑完了但没结果」完全一样。
    """
    payload = body or MatchRunBody()

    # ---- ① 阈值预设合法性 ----
    preset = str(payload.preset or basicSettings.MATCH_THRESHOLD_PRESET or "")
    if preset not in basicSettings.MATCH_THRESHOLD_PRESETS:
        raise dto.ApiError(
            dto.CODE_PARAM_INVALID,
            "未知的阈值预设: %r（可选：%s）"
            % (preset, "、".join(sorted(basicSettings.MATCH_THRESHOLD_PRESETS))))

    # ---- ② 单任务 + 单写入者 ----
    if isRunning():
        raise dto.ApiError(dto.CODE_TASK_STATE_ILLEGAL,
                           "上一次匹配还在跑，等它结束",
                           extra={"phase": _snapshot(withCounts=False).get("phase")})
    owner = scanSched.writeLockOwner()
    if owner:
        # 扫描 / 人脸提取正在写库：此时落库会撞锁。**明确告诉用户在跑什么**，
        # 而不是丢一句「数据库锁定」让他自己猜。
        raise dto.ApiError(
            dto.CODE_TASK_STATE_ILLEGAL,
            "已有任务在跑: %s（单写入者不允许并发；等它结束再跑匹配）" % owner,
            extra={"activeJobCode": owner})

    # ---- ③ 质心预检：没有启用质心 = 判了也白判 ----
    enabled = countEnabledCentroids()
    if enabled <= 0:
        states = _currentStates()
        raise dto.ApiError(
            dto.CODE_PARAM_INVALID,
            "没有任何启用的质心 —— 匹配的依据（质心）只由**人工确认**的样本生成："
            "先到「待确认」队列确认至少 %d 张脸（当前已确认 %d 张），再跑匹配。"
            "这不是故障，是防污染规则（一张误认的脸会把质心带偏）。"
            % (int(basicSettings.MIN_CENTROID_SAMPLES), states["confirmed"]),
            extra={"enabledCentroids": 0,
                   "confirmedCount": states["confirmed"],
                   "minCentroidSamples": int(basicSettings.MIN_CENTROID_SAMPLES)})

    before = _currentStates()
    if before["pending"] <= 0:
        # 不是错误：待确认队列空了（用户全处理完了）说清楚即可，
        # 不要起一个「跑了 0 张」的任务让界面看起来像失败了
        return dto.okBody(started=False, running=False,
                          note="待确认队列是空的（personCode IS NULL 的脸为 0 张），"
                               "没有可判定的脸。已自动归属的脸不会被重跑。",
                          **{k: v for k, v in _snapshot().items()})

    tLow, tHigh = basicSettings.matchThresholds(preset)
    _patch(running=True, phase="loading_centroids", assign=bool(payload.assign),
           preset=preset, tLow=float(tLow), tHigh=float(tHigh),
           pendingBefore=int(before["pending"]), disputedBefore=int(before["disputed"]),
           total=0, auto=0, review=0, cluster=0, byReason={}, written=0, failed=0,
           recomputedBuckets=0, vectors=0, persons=0,
           centroidLoaded=0, centroidTotal=0, elapsed=0.0,
           startedYMDHMS=misc.getTime(), finishedYMDHMS=None,
           lastError="", message="", log=[])
    _logLine("开始匹配：预设 %s（T_LOW=%.2f / T_HIGH=%.2f），待确认 %d 条，%s"
             % (preset, tLow, tHigh, before["pending"],
                "跑完即落库" if payload.assign else "只预览、不落库"))

    thread = threading.Thread(
        target=_runMatchTask, args=(bool(payload.assign), preset, int(payload.limit or 0)),
        name="pb-face-match", daemon=True)
    thread.start()

    return dto.okBody(started=True, **{k: v for k, v in _snapshot(withCounts=False).items()})


def _runMatchTask(assign: bool, preset: str, limit: int) -> None:
    """后台线程体：载质心 -> 取待确认脸 -> 比对 ->（可选）落库。

    ⚠️ 门闩**必须**在 finally 里释放：漏一次，之后所有扫描 / 认脸 / 匹配
    都会被「已有任务在跑」永久挡下，只能重启进程。
    """
    started = time.time()
    locked = False
    try:
        if not scanSched.acquireWriteLock(_LOCK_OWNER):
            _patch(running=False, phase="error",
                   lastError="抢单写入者门闩失败：%s 正在写库"
                             % (scanSched.writeLockOwner() or "?"),
                   finishedYMDHMS=misc.getTime())
            _logLine("抢门闩失败，未开始")
            return
        locked = True

        # ---- ① 装载质心 ----
        def _onCentroid(loaded, total):
            _patch(centroidLoaded=int(loaded or 0), centroidTotal=int(total or 0))

        matrix, index = centroid.loadAllCentroids(progress=_onCentroid)
        info = index.summary()
        _patch(vectors=int(info.get("vectors") or 0),
               persons=int(info.get("persons") or 0))
        _logLine("质心装载完成：%d 条向量 / %d 人（跳过 %d 行）"
                 % (info.get("vectors") or 0, info.get("persons") or 0,
                    info.get("skipped") or 0))
        if not info.get("vectors"):
            # 预检与装载之间库可能被改（用户删了人 / 退了确认），这里是兜底
            raise RuntimeError("装载后没有启用质心（预检时还有 %d 个桶）"
                               % countEnabledCentroids())

        # ---- ② 取待确认脸（口径唯一出处：processor/review/queue）----
        _patch(phase="loading_faces")
        rows = reviewQueue.loadPendingRows(limit=int(limit or 0))
        if not rows:
            _patch(running=False, phase="done", total=0,
                   pendingAfter=int(_currentStates()["pending"]),
                   disputedAfter=int(_currentStates()["disputed"]),
                   elapsed=round(time.time() - started, 2),
                   finishedYMDHMS=misc.getTime(),
                   message="待确认队列在读取时已空，没有可判定的脸")
            return
        _patch(total=len(rows))
        _logLine("待判定人脸 %d 条" % len(rows))

        # ---- ③ 比对（一次矩阵乘，无中间态可报）----
        _patch(phase="matching")
        results = matcher.matchMany(rows, matrix=matrix, index=index, preset=preset)
        stat = matcher.summarize(results)
        _patch(auto=int(stat.get("auto") or 0),
               review=int(stat.get("review") or 0),
               cluster=int(stat.get("cluster") or 0),
               byReason={str(k): int(v)
                         for k, v in (stat.get("byReason") or {}).items()})
        _logLine("判定完成：自动归属 %d / 待确认 %d / 进聚类 %d"
                 % (stat.get("auto") or 0, stat.get("review") or 0,
                    stat.get("cluster") or 0))

        # ---- ④ 落库（assign=true 时）----
        if assign:
            _patch(phase="writing")
            applied = assigner.applyAuto(results)
            _patch(written=int(applied.get("written") or 0),
                   failed=int(applied.get("failed") or 0),
                   recomputedBuckets=int(applied.get("centroids") or 0))
            _logLine("落库：写 %d 条（失败 %d），重算质心桶 %d 个"
                     % (applied.get("written") or 0, applied.get("failed") or 0,
                        applied.get("centroids") or 0))

        after = _currentStates()
        elapsed = round(time.time() - started, 2)
        delta = int(after["disputed"]) - int(_TASK.get("disputedBefore") or 0)
        _patch(running=False, phase="done", elapsed=elapsed,
               pendingAfter=int(after["pending"]), disputedAfter=int(after["disputed"]),
               finishedYMDHMS=misc.getTime(),
               message=("已落库：%d 条脸被自动归属，其中 %s 条进入「我不同意」等你确认"
                        % (int(_TASK.get("written") or 0),
                           ("+%d" % delta) if delta else "0")
                        if assign else
                        "只预览、未落库：确认后再点「确认落库」写入"))
        _LOG.info("match/run 完成: assign=%s 判定=%d 自动=%d 写入=%d 用时=%.2fs",
                  assign, len(rows), stat.get("auto") or 0,
                  int(_TASK.get("written") or 0), elapsed)
    except Exception as e:                                  # noqa: BLE001
        errText = "%s: %s" % (type(e).__name__, e)
        _LOG.error("match/run 失败: %s", errText)
        _logLine("出错：%s" % errText)
        _patch(running=False, phase="error", lastError=errText,
               elapsed=round(time.time() - started, 2),
               finishedYMDHMS=misc.getTime())
    finally:
        if locked:
            scanSched.releaseWriteLock()


# ============================================================
# 四、GET /api/match/status
# ============================================================

@router.get("/match/status", summary="匹配阶段与计数（轮询用；不做假进度）")
def matchStatus() -> dict:
    """轮询这个。阶段是真阶段（载质心 / 读脸 / 比对 / 落库），计数是真实计数。

    与 /api/facerec/status 的区别：匹配**不是** pb_scan_job 里的任务
    （没有 jobCode、没有批次、没有断点），所以这里没有 totalCount/percent
    那一套 —— 硬凑一个百分比出来只会让人以为它也会断点续跑。
    """
    return _snapshot(withCounts=True)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("match.py _VERSION:", _VERSION)
    print("路由:", [(r.path, sorted(r.methods)) for r in router.routes])
    print("阶段文案:", _PHASE_TEXT)
    print("启用质心桶:", countEnabledCentroids())
    print("四态:", _currentStates())
    print("快照:", {k: v for k, v in _snapshot().items() if k != "log"})
