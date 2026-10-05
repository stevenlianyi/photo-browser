#! /usr/bin/env python3
#encoding: utf-8
#Filename: pool.py
#Description: photo-browser 人脸提取进程池（步骤 5）—— 单写入者的边界就在这个文件
#
# 职责
# ----
#   1. extractFaces()            ProcessPoolExecutor 封装，任务进 Queue、结果出 Queue
#   2. extractOne()              主进程内联跑一张（单张/单测/降级用）
#   3. assertNoDatabaseImport()  静态自查：engine.py / pool.py 有没有 import 数据库模块
#
# 单写入者（**这个项目唯一的硬架构约束**）
# --------------------------------------------
#   主进程：查库 -> 分发任务 -> **单线程批量写库**
#   子进程：只做 CPU 计算（解码 / 检测 / 提特征 / 裁人脸图）
#
#   子进程**绝对不能连数据库**。SQLite 的写锁是库级独占：子进程哪怕只是
#   SELECT 一次（读连接也要参与 WAL 的 -shm 协调）都可能与主进程的事务撞上，
#   表现为 `database is locked`，而且**偶发、最难查**（重跑一次又好了）。
#   这条在 MySQL 上只是"建议"，在 SQLite 下是"必须"。
#
#   本文件用三道措施把它变成可验证的，而不是靠自觉：
#     ① 分发给子进程的是**纯数据**（绝对路径 + photoCode + 阈值），
#        不是对象、不是连接、不是行游标；
#     ② 子进程启动时 **monkeypatch sqlite3.connect 直接抛异常** ——
#        任何人不小心在 engine/pool 里写了 SQL，会当场炸在子进程里、
#        带着 traceback 指向真正的行号，而不是变成"偶发的 database is locked"；
#     ③ assertNoDatabaseImport() 做静态自查（验收第 7 条就查这个）。
#
# 模型懒加载
# ----------
#   buffalo_l 有 300MB+（SCRFD 143MB + ArcFace 174MB），
#   建一次 onnxruntime Session 实测 1.2~3.0 秒，而单张检测+提取只要 0.3~0.9 秒。
#   子进程若每张都重建，4 进程并行 = 4x300MB 内存 + 10 秒纯开销，**并行度越高越慢**。
#   进程池的 worker 是长驻的 -> engine.getEngine() 在每个子进程里懒加载**一次**即可。
#
# 任务分块 vs 一张一任务
# ---------------------
#   一张一个任务的 IPC + pickle 开销，在 10 万张上会变成几分钟纯等待；
#   一块 FACE_BULK_CHUNK 张则摊薄到可忽略，单块失败也只需重跑该块。
#   代价是块内串行，但块之间是真并行，总吞吐不受影响（与步骤 4 的缩略图池同口径）。

import multiprocessing
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/face
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from engine.face import engine as faceEngine                      # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("facePool", "facepool.log")

#: 每个子进程任务携带的照片条数（与 basicSettings.THUMB_BULK_CHUNK 同理）
FACE_BULK_CHUNK: int = 16
#: 子进程结果回主进程后，攒够多少张调一次 onBatch（让上层写库）
FACE_RESULT_BATCH: int = 16
#: 等待子进程结果的轮询间隔（秒）
_QUEUE_POLL_SEC: float = 0.05
#: 结果队列连续多少秒没有任何数据就判定"卡死"（含模型首次加载，故给得宽）
_QUEUE_STALL_SEC: float = 300.0
#: 单个子进程按多少 MB 内存估（实测 RSS 约 470MB，留到 600 是给解码缓冲的余量）。
#: 缺省进程数按「物理内存的 60% 能养几个」再夹一道 ——
#: 只看核数，16 核就是 15 个进程、约 7GB；8GB 机器上会一路换页直到被系统杀进程。
#: 数值与硬上限都在 basicSettings（改配置不必改这个文件）：
FACE_WORKER_MEMORY_MB: int = basicSettings.FACE_WORKER_MEMORY_MB
FACE_MAX_WORKERS: int = basicSettings.FACE_MAX_WORKERS
FACE_MAX_MEMORY_MB: int = basicSettings.FACE_MAX_MEMORY_MB

#: **禁止出现在 engine.py / pool.py 里的数据库相关模块**（assertNoDatabaseImport 用）
FORBIDDEN_MODULES: tuple = ("sqlite3", "sqliteHandle", "sqliteCommon", "sqliteSettings",
                            "dbHandle")

#: 豁免标记：守卫函数自己 import sqlite3 是必需的，带这个行尾注释才不被自查拦下
GUARD_EXEMPT_MARK: str = "db-guard"

#: 引擎参数的缺省值（主进程解析成绝对路径/纯数据后再分发，子进程不读配置）
DEFAULT_ENGINE_KWARGS: dict = {
    "modelPack": faceEngine.DEFAULT_MODEL_PACK,
    "detSize": faceEngine.DEFAULT_DET_SIZE,
    "minDetScore": None,
    "minFaceEdge": None,
    "maxYaw": None,
    "wantCrop": True,
    "cropSize": None,
    "cropSquare": None,
    "cropQuality": None,
    # 回传"被丢弃的人脸"的明细（detScore / 短边 / yaw / 丢弃原因，**无 embedding**）。
    # 缺省 True：这是事后能回答"这张照片为什么少一张脸"的唯一数据来源，
    # 而这类问题在真实照片库上一定会被问到（"我记得这张里明明有三个人"）。
    # 代价很小：每张脸几十字节、只随结果队列回主进程一次，不进库。
    # ⚠️ 它只是"让人能查"，不是"让人能用"：被丢弃的脸**绝不**进 pb_face。
    "reportRejected": True,
}


# ============================================================
# 一、子进程侧的"禁止连库"守卫
# ============================================================

def forbidDatabase() -> None:
    """在**子进程**里把 sqlite3.connect 打成抛异常（幂等，可重复调）。

    为什么用 monkeypatch 而不是"约定不要 import"
    --------------------------------------------
      约定只对写代码的人有效，而这里的失效场景是**沉默的**：某天有人在
      engine.py 里为了"方便取一下 relPath"顺手 import 了 sqliteCommon，
      代码能跑、单测也能过（单测常在主进程跑），直到上库跑几万张时才炸
      `database is locked`，而且是偶发的。patch 掉 connect 之后，
      那个 import 会在**第一次真的连库时**当场抛错，traceback 精确到行。

    为什么 patch connect 而不是拦 import
    ----------------------------------
      拦 import（sys.meta_path）会误伤：numpy / onnxruntime / Pillow 在某些平台上
      会间接 import sqlite3（字体缓存、OpenCV 持久化等），一拦就是"进程起不来"，
      比原问题更难查。patch connect 不影响任何 import，只在真正开库时炸。
    """
    try:
        import sqlite3            # db-guard: 守卫自己必须 import 才能 patch，见 assertNoDatabaseImport
    except ImportError:                          # pragma: no cover - 解释器自带
        return
    if getattr(sqlite3, "_photobrowser_guarded", False):
        return

    def _denied(*args, **kwargs):
        raise RuntimeError(
            "人脸提取子进程被禁止连接数据库（单写入者约束）。"
            "engine.py / pool.py 只做 CPU 计算；结果请经结果队列回主进程写库。"
            "确实需要查库的值，在主进程查好当纯数据传进来。")

    sqlite3.connect = _denied
    sqlite3._photobrowser_guarded = True


def initChildProcess(threads: int = faceEngine.CHILD_ORT_THREADS) -> None:
    """子进程启动初始化：禁连库 + 钉住 ORT 线程数。**必须在建模型之前调。**

    为什么这两件事必须放在子进程初始化里做
    ----------------------------------------
      禁连库：见 forbidDatabase()。
      线程数：见 engine.setOrtThreadLimit() —— onnxruntime 默认每个 Session 用满
      所有物理核，8 个 worker × 16 核 = 128 线程抢 16 核，实测单张均值从
      0.88 秒劣化到 4.47 秒（**并行度越高越慢**）。钉成每进程 1 线程后恢复。
    """
    forbidDatabase()
    faceEngine.setOrtThreadLimit(threads)


# ============================================================
# 二、静态自查（验收第 7 条）
# ============================================================

def assertNoDatabaseImport(targets=None) -> list:
    """静态扫描 engine.py / pool.py 的 import，发现数据库相关 import 就抛错。

    返回扫过的文件名列表。**这是"子进程不连库"的可复核证据**：
    比"我觉得没连"可靠，因为它是按 import 语句文本匹配的，
    任何 `import sqlite3` / `from database import ...` 都会被抓出来。
    """
    import re
    files = list(targets or (os.path.join(_HERE_DIR, "engine.py"),
                             os.path.join(_HERE_DIR, "pool.py")))
    pattern = re.compile(r"^(\s*)(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
    bad = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for match in pattern.finditer(text):
            name = match.group(2)
            head = name.split(".")[0]
            leaf = name.split(".")[-1]
            if head not in FORBIDDEN_MODULES and leaf not in FORBIDDEN_MODULES:
                continue
            # 显式豁免标记：守卫函数自己**必须** import sqlite3 才能把它 patch 掉。
            # 用行尾注释豁免而不是把 import 藏进 __import__()，是为了让这一行
            # 在代码走查时依然**肉眼可见**（藏起来就等于没有约束）。
            lineText = text[match.start():text.find("\n", match.start())]
            if GUARD_EXEMPT_MARK in lineText:
                continue
            line = text[:match.start()].count("\n") + 1
            bad.append("%s:%d -> %s" % (os.path.basename(path), line, name))
    if bad:
        raise AssertionError(
            "engine.py / pool.py 出现数据库相关 import（子进程连库 = database is locked）：\n  "
            + "\n  ".join(bad))
    return [os.path.basename(p) for p in files]


# ============================================================
# 三、引擎参数在进程间的表示
# ============================================================

def resolveEngineKwargs(overrides: dict = None) -> dict:
    """把引擎参数解析成**纯数据**（None 用缺省、路径转绝对、dict 可 pickle）。

    必须在主进程里解析好再分发
    --------------------------
      子进程是 spawn 起来的全新解释器，读不到主进程对 local_settings 的打桩
      （单测里就是这样）。让它自己读配置，轻则"阈值不生效"，重则"两个进程
      用了不同的模型目录" —— 都属于「主进程报成功、实际结果不对」那一类最难查的错。
    """
    kwargs = dict(DEFAULT_ENGINE_KWARGS)
    kwargs.update(overrides or {})
    if kwargs.get("modelRoot"):
        kwargs["modelRoot"] = os.path.abspath(str(kwargs["modelRoot"]))
    if kwargs.get("detSize") is not None:
        kwargs["detSize"] = tuple(int(v) for v in kwargs["detSize"])
    return kwargs


# ============================================================
# 四、worker（子进程）
# ============================================================

def _memMb() -> tuple:
    """(物理内存总量 MB, 可用 MB)。拿不到返回 (0, 0)，调用方按"未知"处理。"""
    try:
        import ctypes
        buf = (ctypes.c_ulonglong * 9)()
        ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0] = 64  # sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(buf))
        return int(buf[1] // (1024 * 1024)), int(buf[2] // (1024 * 1024))
    except Exception:
        return 0, 0


def failedResult(absPath, photoCode, errMsg) -> dict:
    """统一的失败结果结构（字段与成功时完全一致，调用方不用分支）"""
    return {"ok": False, "absPath": absPath, "photoCode": photoCode,
            "faces": [], "rejected": dict((c, 0) for c in faceEngine.REASON_CODES),
            "droppedFaces": [],
            "kept": 0, "rawCount": 0, "imgW": 0, "imgH": 0, "elapsed": 0.0,
            "errCode": 0, "errMsg": errMsg}


def _chunkWorker(task: tuple) -> list:
    """处理一小批照片，**只做 CPU 计算，绝不碰数据库**。

    必须是模块级函数（Windows 的 spawn 启动方式要能按名字 import 回来）。
    task = (engineKwargs, items)，items = [(index, absPath, photoCode), ...]

    返回 list[(index, absPath, photoCode, resultDict), ...]
    resultDict 是 FaceEngine.extract() 的返回值（纯 dict + bytes，可 pickle）。
    """
    engineKwargs, items = task
    initChildProcess()
    engine = faceEngine.getEngine(**engineKwargs)     # 每个子进程**只加载一次**
    out = []
    for index, absPath, photoCode in items:
        try:
            result = engine.extract(absPath, photoCode)
        except Exception as e:                       # pragma: no cover - 兜底
            # 单张照片失败绝不能带走整块（否则这一块 16 张白跑）
            _LOG.error("子进程提取失败 %s: %s: %s", absPath, type(e).__name__, e)
            result = failedResult(absPath, photoCode, "%s: %s" % (type(e).__name__, e))
        if not out:
            # 每一块的第一条结果捎带上报引擎信息：主进程据此判断
            # 「子进程里 landmark_3d_68 到底加载上没有」。
            # 这条信息缺失的后果是**静默的** —— 姿态为 None 时
            # |yaw|<=45 过滤不报错也不生效，只表现为 side_face 计数恒为 0。
            result = dict(result)
            result["engineInfo"] = engine.describe()
        out.append((index, absPath, photoCode, result))
    return out


def _queueWorkerLoop(taskQueue, resultQueue, engineKwargs) -> None:
    """Queue 模式的 worker：从任务队列取块，算完把结果**推进结果队列**。

    为什么用 mp.Queue 而不是让 worker 直接 return
    ----------------------------------------------
      两条路都能把结果送回主进程，但 Queue 模式让"结果回主进程"这件事
      在代码里**看得见**（一个真实的 mp.Queue），主进程边收边攒批写库，
      内存占用与库大小无关；而 return 模式要把整批结果 pickle 完才拿得到，
      10 万张时会攒出一个巨大的列表（face 与 cropData 都是 bytes，几百 MB 起）。
    """
    forbidDatabase()
    faceEngine.setOrtThreadLimit()
    try:
        faceEngine.getEngine(**engineKwargs)         # 懒加载一次（在第一个任务前）
    except Exception as e:                           # pragma: no cover - 模型不可用
        _LOG.error("子进程模型加载失败: %s: %s", type(e).__name__, e)
        resultQueue.put(("__fatal__", "%s: %s" % (type(e).__name__, e)))
        return
    while True:
        item = taskQueue.get()
        if item is None:                             # 关闭哨兵
            return
        chunkKwargs, items = item
        try:
            results = _chunkWorker((chunkKwargs, items))
        except Exception as e:                       # pragma: no cover - 兜底
            _LOG.error("子进程块失败: %s: %s", type(e).__name__, e)
            results = []
            for index, absPath, photoCode in items:
                results.append((index, absPath, photoCode,
                                failedResult(absPath, photoCode,
                                             "%s: %s" % (type(e).__name__, e))))
        resultQueue.put(("__chunk__", results))


# ============================================================
# 五、对外入口
# ============================================================

def _tally(summary: dict, result: dict) -> None:
    """把单张照片的结果并进汇总（与步骤 4 的缩略图池同口径）"""
    summary["images"] += 1
    elapsed = float(result.get("elapsed") or 0.0)
    summary["elapsedTotal"] += elapsed
    summary["kept"] += int(result.get("kept") or 0)
    summary["rawFaces"] += int(result.get("rawCount") or 0)
    if result.get("ok"):
        summary["elapsedList"].append(elapsed)
    else:
        summary["noFace"] += 1
        msg = str(result.get("errMsg") or "")
        if msg and msg not in summary["failures"]:
            summary["failures"].append(msg)
    for code, count in (result.get("rejected") or {}).items():
        if count:
            summary["dropped"][code] = summary["dropped"].get(code, 0) + int(count)
    # 被丢弃人脸的**聚合明细**（不是原始列表）：
    #   回答"为什么这张照片少了一张脸"要的是"最接近阈值的那几个样本"，
    #   而不是把几万条明细攒在内存里。这里只留每档的极值 + 少量样本，
    #   所以跑 10 万张也不会涨内存。
    for face in (result.get("droppedFaces") or ()):
        _tallyDroppedFace(summary, str(result.get("absPath") or ""), face)


def _tallyDroppedFace(summary: dict, absPath: str, face: dict) -> None:
    """把一张被丢弃的人脸并进 droppedDetail（极值 + 最多 3 条样本）"""
    reason = str(face.get("reason") or "")
    if not reason:
        return
    one = summary["droppedDetail"].setdefault(
        reason, {"count": 0, "samples": [], "detScore": [], "shortEdge": [],
                 "poseYaw": []})
    one["count"] += 1
    for key, column in (("detScore", "detScore"), ("shortEdge", "shortEdge"),
                        ("poseYaw", "poseYaw")):
        value = face.get(column)
        if value is not None:
            one[key].append(float(value))
    if len(one["samples"]) < 3:
        one["samples"].append({
            "file": os.path.basename(absPath),
            "detScore": round(float(face.get("detScore") or 0.0), 4),
            "shortEdge": round(float(face.get("shortEdge") or 0.0), 1),
            "poseYaw": (None if face.get("poseYaw") is None
                        else round(float(face["poseYaw"]), 2)),
        })


def _newSummary(workers: int) -> dict:
    return {"images": 0, "kept": 0, "rawFaces": 0, "noFace": 0, "failed": 0,
            "elapsedTotal": 0.0, "elapsedList": [], "dropped": {}, "failures": [],
            "droppedDetail": {},
            "workers": workers, "mode": "queue", "wallTime": 0.0,
            "engineInfo": {}}


def attachEngineInfo(summary: dict, kwargsResolved: dict) -> dict:
    """把**本进程实际加载到的引擎信息**挂到 summary 上（诊断用）。

    为什么值得单独做
    --------------
      子进程里如果 landmark_3d_68 没加载成功，姿态就是 None，
      于是 |yaw|<=45 这条质量过滤会**静默失效**（不报错、不崩，
      只是 side_face 计数恒为 0）。这类"过滤悄悄不生效"最难发现：
      看汇总数字才发现端倪，但数字本身看着完全正常。
      把 loadedModules 报出来，一眼就能看出 poseReady 是 True 还是 False。
    """
    try:
        summary["engineInfo"] = faceEngine.getEngine(**kwargsResolved).describe()
    except Exception as e:                          # pragma: no cover - 诊断用
        summary["engineInfo"] = {"error": "%s: %s" % (type(e).__name__, e)}
    return summary


def _runInline(tasks, kwargsResolved, absorb) -> None:
    """主进程内联串行跑（workers=1 或进程池不可用时的退路）"""
    # threads=0 = 不钉线程：内联时全机器就这一个进程，钉 1 线程会白扔一半算力
    initChildProcess(threads=0)
    engine = faceEngine.getEngine(**kwargsResolved)
    for index, (absPath, photoCode) in enumerate(tasks):
        try:
            result = engine.extract(absPath, photoCode)
        except Exception as e:
            result = failedResult(absPath, photoCode, "%s: %s" % (type(e).__name__, e))
        if index == 0:
            result = dict(result)
            result["engineInfo"] = engine.describe()
        absorb(index, absPath, photoCode, result)


def _resolveWorkers(workers, autoCount: int, cpu: int, totalMb: int, availMb: int,
                    byCpu: int, byMem: int) -> int:
    """定最终进程数，并把"为什么是这个数"说清楚（缺省靠猜的时候一定要有日志）。

    优先级
    ------
      1. 显式 workers 参数（CLI 的 --workers）—— **不受任何上限约束**。
         上限只管"没指定时别乱猜"，用户明确要 12 个就给 12 个。
      2. 环境变量 PHOTO_BROWSER_FACE_WORKERS —— 同样显式、同样不设上限。
         用途：临时调高某个工具/服务的缺省值，不用改代码。
      3. 缺省 = min(核数-1, 内存预算/600MB, FACE_MAX_WORKERS)

    为什么"显式值不设上限"也要打警告
    -------------------------------
      有人会在 4GB 机器上顺手 --workers 16，然后抱怨机器卡死。
      这时至少让他知道"你要的 16 个进程按 600MB 估要 9.6GB"，
      比默默跑到一半被系统杀掉、最后不知道死在哪强。
    """
    explicit = workers
    if not explicit:
        env = os.environ.get(basicSettings.FACE_WORKERS_ENV, "").strip()
        if env:
            try:
                explicit = int(env)
            except ValueError:
                _LOG.warning("%s=%r 不是整数，忽略", basicSettings.FACE_WORKERS_ENV, env)
    if explicit:
        count = max(1, int(explicit))
        needMb = count * FACE_WORKER_MEMORY_MB
        if totalMb > 0 and needMb > totalMb * 0.8:
            _LOG.warning("按要求开 %d 个进程，每个约 %dMB，合计约 %dMB，"
                         "已超物理内存 %dMB 的 80%%（当前可用 %dMB）"
                         "—— 可能一路换页，甚至被系统杀进程。机器小就把 --workers 调小。",
                         count, FACE_WORKER_MEMORY_MB, needMb, totalMb, availMb)
        return count
    reasons = []
    if byCpu > autoCount:
        reasons.append("核数只允许 %d（%d 核留 1 个给主进程与 I/O）" % (byCpu, cpu))
    if byMem > autoCount:
        reasons.append("内存只够 %d 个（预算 %dMB ÷ 每个 %dMB）"
                       % (byMem, min(totalMb, FACE_MAX_MEMORY_MB), FACE_WORKER_MEMORY_MB))
    if FACE_MAX_WORKERS <= min(byCpu, byMem):
        reasons.append("保守上限 %d（要更快请显式 --workers）" % FACE_MAX_WORKERS)
    _LOG.info("进程数取 %d：%s（物理内存 %dMB，可用 %dMB）",
              autoCount, "；".join(reasons) if reasons else "核数与内存都宽裕",
              totalMb, availMb)
    return max(1, int(autoCount))


def extractFaces(tasks: list, workers: int = None, engineKwargs: dict = None,
                 onBatch=None, batchSize: int = None, onProgress=None) -> dict:
    """批量提取人脸（**进程池**），结果**经结果队列回主进程**。

    参数
    ----
    tasks        : list[(absPath, photoCode)]
                   absPath 必须是**主进程解析好的绝对路径**（子进程不读配置）
    workers      : 进程数，缺省 os.cpu_count()-1（留一个核给主进程与 I/O）
    engineKwargs : 引擎参数（阈值/模型），经 resolveEngineKwargs 归一成纯数据
    onBatch      : 可选回调 onBatch(list[(index, absPath, photoCode, result)])。
                   **数据库只允许在这个回调里写**（它跑在主进程主线程）。
    batchSize    : 攒够多少张调一次 onBatch（缺省 FACE_RESULT_BATCH）
    onProgress   : 可选回调 onProgress(done, total, result)，CLI 打进度用

    返回
    ----
      dict 汇总 {images, kept, rawFaces, noFace, failed, elapsedTotal,
                 elapsedList, dropped, failures, workers, mode, wallTime}
      elapsedList 留着单张耗时，调用方自己算 p50/p95（别在池里定口径：
      CLI 要的是「端到端单张耗时」，单测要的是「纯计算耗时」，两者不同）

    为什么 onBatch 在主进程主线程跑
    ------------------------------
      单写入者。回调里是 sqliteCommon.insertManyTableGeneral（一次事务 500 行），
      绝不能把这个动作交给线程 —— SQLite 的写连接是库级独占，
      两个写连接并发就是 `database is locked`。
      本函数**不 import 任何数据库模块**：它只负责把结果摆到回调面前。
    """
    total = len(tasks or [])
    cpu = os.cpu_count() or 4
    byCpu = max(1, cpu - 1)                    # 留一个核给主进程与 I/O
    totalMb, availMb = _memMb()
    # 按内存再夹一道。每个子进程独立加载一份 buffalo_l，实测单进程 RSS
    # 约 470MB（模型 460MB + onnxruntime arena + 解码缓冲），这里按 600MB 估。
    # 16 核就意味着 15 个进程 —— 只看核数不看内存，8GB 机器上必 OOM。
    # 内存不够时的表现最难查：不报错、不崩，只是越跑越慢、机器发烫
    # （在换页），严重时 OS 直接杀进程、批次断在半路。
    # ⚠️ 用**总量**而不是"可用量"：可用量随别的程序起伏，
    # 同一批照片两次运行会得到不同的并行度，吞吐忽高忽低且无法复现。
    budgetMb = totalMb if totalMb > 0 else FACE_MAX_MEMORY_MB
    budgetMb = min(budgetMb, FACE_MAX_MEMORY_MB)      # 硬上限：最多吃 4GB
    byMem = max(1, int(budgetMb * 0.6) // int(FACE_WORKER_MEMORY_MB))
    autoCount = max(1, min(byCpu, byMem, FACE_MAX_WORKERS))
    workerCount = _resolveWorkers(workers, autoCount, cpu, totalMb, availMb,
                                  byCpu, byMem)
    summary = _newSummary(workerCount)
    if total == 0:
        return summary

    kwargsResolved = resolveEngineKwargs(engineKwargs)
    flushSize = max(1, int(batchSize or FACE_RESULT_BATCH))
    # 块内每项是**扁平的** (index, absPath, photoCode) 三元组：index 保留原始顺序，
    # 让 onBatch 的调用方能按扫描顺序落库（进度条与断点续跑都需要它）。
    # ⚠️ 必须摊平：直接切 enumerate 的结果会得到 (index, (absPath, photoCode))
    #    这样的二元组，子进程解包时炸 "not enough values to unpack"。
    indexed = [(index, absPath, photoCode)
               for index, (absPath, photoCode) in enumerate(tasks)]
    taskChunks = [(kwargsResolved, indexed[i:i + FACE_BULK_CHUNK])
                  for i in range(0, total, FACE_BULK_CHUNK)]

    pending = []
    state = {"done": 0, "lastProgress": 0}

    def _flush():
        if not onBatch or not pending:
            return
        batch = list(pending)
        del pending[:]
        onBatch(batch)

    def _absorb(index, absPath, photoCode, result):
        # 子进程把引擎信息捎在每块第一条结果里，这里收进 summary。
        # 不这么做的话，"子进程里 landmark_3d_68 到底加载上没有"就永远看不见 ——
        # 而姿态缺失时 |yaw|<=45 过滤是**静默失效**的，只表现为 side_face 恒为 0。
        if result.get("engineInfo") and not summary["engineInfo"]:
            summary["engineInfo"] = result["engineInfo"]
        _tally(summary, result)
        state["done"] += 1
        done = state["done"]
        if onBatch:
            pending.append((index, absPath, photoCode, result))
            if len(pending) >= flushSize:
                _flush()
        if onProgress and (done - state["lastProgress"] >= basicSettings.PROGRESS_EVERY
                           or done == total):
            state["lastProgress"] = done
            onProgress(done, total, result)

    # ---- workers=1：不起进程池（起进程的开销大于并行收益）----
    if workerCount == 1:
        summary["mode"] = "inline"
        start = time.time()
        _runInline(tasks, kwargsResolved, _absorb)
        _flush()
        summary["wallTime"] = time.time() - start
        return summary

    # ---- 进程池 + mp.Queue ----
    start = time.time()
    procs = []
    try:
        ctx = multiprocessing.get_context("spawn")
        taskQueue = ctx.Queue()
        resultQueue = ctx.Queue()
        for _ in range(workerCount):
            proc = ctx.Process(target=_queueWorkerLoop,
                               args=(taskQueue, resultQueue, kwargsResolved),
                               daemon=True)
            proc.start()
            procs.append(proc)
        _LOG.info("人脸提取进程池启动: %d 进程 / %d 张 / 每块 %d 张",
                  workerCount, total, FACE_BULK_CHUNK)

        for task in taskChunks:
            taskQueue.put(task)
        for _ in procs:
            taskQueue.put(None)                    # 关闭哨兵

        expect = len(taskChunks)
        got = 0
        lastData = time.time()
        fatal = ""
        while got < expect:
            try:
                tag, payload = resultQueue.get(timeout=_QUEUE_POLL_SEC)
            except Exception:
                now = time.time()
                if now - lastData > _QUEUE_STALL_SEC:
                    fatal = "结果队列 %.0f 秒无数据，判定子进程卡死" % _QUEUE_STALL_SEC
                    break
                if all(not p.is_alive() for p in procs) and got < expect:
                    fatal = "子进程全部退出，只收到 %d/%d 块" % (got, expect)
                    break
                continue
            lastData = time.time()
            if tag == "__fatal__":
                fatal = str(payload)
                break
            if tag != "__chunk__":
                continue
            got += 1
            for index, absPath, photoCode, result in payload:
                _absorb(index, absPath, photoCode, result)
            # 收一块落一批：主进程内存与总量解耦
            _flush()
        _flush()
        if fatal:
            _LOG.error("人脸提取进程池异常: %s", fatal)
            summary["failures"].append(fatal)
    except Exception as e:
        # 进程池起不来（受限机器 / 杀软拦 CreateProcess）：**退回主进程串行**，
        # 不让一批坏环境毁掉整轮（与步骤 4 的缩略图池同口径）
        _LOG.error("人脸提取进程池不可用(%s: %s)，退回主进程串行",
                   type(e).__name__, e)
        summary["workers"] = 0
        summary["mode"] = "inline-fallback"
        summary["failures"].append("进程池不可用: %s: %s" % (type(e).__name__, e))
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        procs = []
        _runInline(tasks, kwargsResolved, _absorb)
        _flush()
    finally:
        for proc in procs:
            proc.join(timeout=5.0)
            if proc.is_alive():                    # pragma: no cover
                proc.terminate()
        _flush()
    summary["wallTime"] = time.time() - start
    return summary


def extractOne(absPath: str, photoCode: str = "", engineKwargs: dict = None) -> dict:
    """在**当前进程**内跑一张（单张调试 / 单测用；不建进程池）。

    结果结构与 extractFaces 里的一致。模型走进程内单例，重复调用不会重复加载。
    """
    forbidDatabase()
    engine = faceEngine.getEngine(**resolveEngineKwargs(engineKwargs))
    return engine.extract(absPath, photoCode)


# ============================================================
# 六、耗时统计（p50 / p95）
# ============================================================

def percentile(values: list, q: float) -> float:
    """线性插值分位数（q 取 0~1）。空列表返回 0.0。"""
    data = sorted(float(v) for v in values or [])
    if not data:
        return 0.0
    if len(data) == 1:
        return data[0]
    pos = max(0.0, min(1.0, float(q))) * (len(data) - 1)
    low = int(math_floor(pos))
    high = min(low + 1, len(data) - 1)
    frac = pos - low
    return data[low] * (1.0 - frac) + data[high] * frac


def math_floor(x: float) -> int:
    import math
    return int(math.floor(x))


def timingStats(elapsedList: list) -> dict:
    """单张耗时统计 {count, mean, p50, p95, max, total}（单位：秒）"""
    data = sorted(float(v) for v in elapsedList or [])
    if not data:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "total": 0.0}
    return {"count": len(data), "mean": sum(data) / len(data),
            "p50": percentile(data, 0.50), "p95": percentile(data, 0.95),
            "max": data[-1], "total": sum(data)}


if __name__ == "__main__":
    import json

    print("facePool _VERSION:", _VERSION)
    print("静态自查:", assertNoDatabaseImport(), "-> 无数据库 import")
    print("任务分块:", FACE_BULK_CHUNK, "张/块   攒批:", FACE_RESULT_BATCH, "张/批")
    print("引擎参数:", json.dumps(resolveEngineKwargs(), ensure_ascii=False))
    print("禁止的模块:", FORBIDDEN_MODULES)
