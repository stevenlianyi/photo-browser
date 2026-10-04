#! /usr/bin/env python3
#encoding: utf-8

#Filename: thumbMaker.py
#Description: photo-browser 缩略图生成（步骤 4）—— EXIF 纠正 + 缩放 + WebP
#
# 职责
# ----
#   1. make_thumb_bytes(absOrigPath, size)   单张开解码->转码->出 WebP 字节
#   2. make_thumb(row, size)                 **按需生成**（命中磁盘直接返回，绝不解码原图）
#   3. make_thumbs_bulk(rows, workers)        **批量生成**（进程池，CPU 解码密集）
#   4. ensure_thumb(row, size)               线程池 + 同键互斥，给 /api/thumb 同步等结果用
#
# 三条硬约束
# ----------
#   1. **不改动原图任何字节**：只 open(...,"rb")；不写、不删、不改名、不碰 mtime。
#      顺带一提，缩略图的 EXIF 方向纠正**只作用在内存里的副本**上，
#      不回写原图（相机原始方向信息必须留在原图里，扫描器还要读它）。
#   2. **缩略图不入库**：路径由 fileHash + size 算出来（thumbStore），不占 pb_photo 字段。
#   3. **落盘一律原子写**（thumbStore.write_atomic），绝不出现半文件。
#
# 两种并行为什么不同
# ------------------
#   批量：解码是 CPU 密集（一张 4000x3000 JPEG 解码 ~120ms，Pillow 虽在 C 层
#        但 GIL 只在 Python 边界释放），**必须多进程**才能压满多核。
#   按需：同一时刻通常只有 1~2 张在等，起进程池冷启动（Windows spawn ~0.3s）
#        比解码本身还慢，得不偿失 -> **线程池**。
#
# 单写入者不冲突
# --------------
#   批量进程池的子进程**只读原图、只写 thumb\、绝不连数据库**：
#   行数据由主进程查好（sqliteCommon）再分发（开发计划 §3.3）。
#   这也是为什么 worker 函数只吃 list[dict] 这种可 pickle 的纯数据。

import os
import sys
import threading
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/media
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from processor.media import thumbStore as thumbStore              # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("thumbMaker", "thumbmaker.log")

# ============================================================
# 一、可观测计数器（验收第 2 条：二次请求不能重复解码原图）
# ============================================================
# hit / miss / failed 三个计数是**验收与排障的一手证据**：
# 二次请求 /api/thumb 后 decoded 计数若没涨，就说明确实没碰原图。
# 只在内存里（进程级），不落库、不跨进程。
COUNTERS: dict = {"hit": 0, "miss": 0, "decoded": 0, "failed": 0, "bytes": 0,
                 "failedDecode": 0}

# ---- 失败原因分类（make_thumb 结果里的 errCode）----
# 为什么要分类：前端要区别对待这几类，处理方式完全不同
#   ERR_DECODE —— 原图在磁盘上，但**解不出像素**（截断、全零、扩展名骗人）。
#                 重试一万次也一样。正确反应是显示"文件损坏"占位并把它标出来，
#                 **不能**报 500：5xx 会让前端/代理自动重试并弹错误提示，
#                 而它是永远不会成功的请求。
#   ERR_NO_ORIGINAL —— 原图不在磁盘（拔了移动硬盘 / 目录被挪走）。重试有意义。
#   ERR_STORE —— 落盘侧出问题（权限、路径脏数据、只读边界守卫拦下）。是我们的问题。
#   ERR_PARAM —— 入参/数据不合法。也是我们的问题。
ERR_DECODE = "decode"
ERR_NO_ORIGINAL = "no_original"
ERR_STORE = "store"
ERR_PARAM = "param"

#: errCode -> HTTP 状态码。decode 不是我们的问题，不该冒 5xx
ERR_HTTP_STATUS = {ERR_DECODE: 422, ERR_NO_ORIGINAL: 404, ERR_STORE: 500, ERR_PARAM: 500}


def countersSnapshot() -> dict:
    """COUNTERS 的一份拷贝（测试与 /api/media/stats 都读它）"""
    return dict(COUNTERS)


def resetCounters() -> None:
    """清零计数器（单测用，避免用例之间互相污染）"""
    for key in COUNTERS:
        COUNTERS[key] = 0


# ============================================================
# 二、图像处理
# ============================================================

def _openOriented(absPath: str):
    """打开原图并**在内存里**应用 EXIF orientation。

    返回 (image, transposed)

    为什么用 ImageOps.exif_transpose 而不是手写 6 种旋转：
      * 手机照片普遍带 orientation=6（横拍存成竖），不纠正的话缩略图会躺倒；
      * exif_transpose 覆盖全部 8 种取值（含镜像 2/4/5/7），
        且纠正后会把 orientation 标签**从副本上摘掉**，避免二次纠正。

    只读保证：Pillow 的 Image.open 是惰性读取 + 只读句柄，
    转置产生的是新 Image 对象，原图文件一个字节都不会变。
    """
    from PIL import Image, ImageOps

    image = Image.open(absPath)
    try:
        image.draft("RGB", (basicSettings.THUMB_SIZES[-1], basicSettings.THUMB_SIZES[-1]))
    except Exception:
        # draft 只对 JPEG 有效，且是纯优化；失败不影响正确性
        pass
    fixed = ImageOps.exif_transpose(image)
    if fixed is image:
        # 无 EXIF 方向：直接用原对象，但要转成 RGB（PNG 带 alpha / P 模式不能直接存 WEBP）
        if image.mode != "RGB":
            fixed = image.convert("RGB")
    elif fixed.mode != "RGB":
        fixed = fixed.convert("RGB")
    return fixed, image


def encodeWebp(image, quality: int = None, method: int = None) -> bytes:
    """Image -> WebP 字节（quality/method 缺省取 basicSettings）"""
    from io import BytesIO

    q = basicSettings.THUMB_QUALITY if quality is None else int(quality)
    m = basicSettings.THUMB_WEBP_METHOD if method is None else int(method)
    buffer = BytesIO()
    image.save(buffer, basicSettings.THUMB_FORMAT, quality=q, method=m)
    return buffer.getvalue()


def resizeToWidth(image, size: int):
    """按**宽度**缩到 size，保持宽高比。

    三条口径
    --------
      1. 只缩不放：原图本来就比缩略图小（老照片扫描件 300px）时，
         硬拉到 400px 只会得到一张糊图，还白占磁盘。前端用 CSS 撑即可。
      2. 目标宽 = size，高按比例取整；极端长条图（全景）高可能算成 0，
         此时钳到 1px，保证不出"PIL 高度为 0"这种错。
      3. 采样用 LANCZOS：缩小时质量明显好于默认的 BICUBIC/HAMMING。
    """
    from PIL import Image

    width, height = image.size
    target = int(size)
    if width <= target:
        return image
    newHeight = max(1, int(round(height * float(target) / float(width))))
    return image.resize((target, newHeight), Image.LANCZOS)


def make_thumb_bytes(absOrigPath: str, size: int = thumbStore.THUMB_DEFAULT_SIZE,
                     quality: int = None) -> bytes:
    """一张原图 -> 目标尺寸的 WebP 字节。

    参数
    ----
    absOrigPath : 原图绝对路径（**只读**）
    size        : 目标**宽**（必须是 THUMB_SIZES 之一）
    quality     : WebP 质量，缺省 basicSettings.THUMB_QUALITY(82)

    返回
    ----
    bytes —— WebP 数据
    """
    from common import globalDefinition as comGD

    width = thumbStore.normSize(size)
    if not os.path.isfile(absOrigPath):
        raise OSError("%s: %s" % (absOrigPath, comGD.errText(comGD.ERR_FILE_NOT_FOUND)))
    image, holder = _openOriented(absOrigPath)
    small = None
    try:
        small = resizeToWidth(image, width)
        data = encodeWebp(small, quality=quality)
    finally:
        # Pillow 的 Image 持有解码缓冲；不显式关的话，缩略图尺寸越大
        # 峰值内存越好看（800 张 800px 图足以把几百 MB 堆上去）。
        # 缩放产生的是新对象，必须单独关；exif_transpose 也可能已另建对象。
        if small is not None and small is not image:
            small.close()
        if image is not holder:
            image.close()
        holder.close()
    COUNTERS["decoded"] += 1
    COUNTERS["bytes"] += len(data)
    return data


def make_thumb(row: dict, size: int = thumbStore.THUMB_DEFAULT_SIZE,
               photoRoot: str = None, thumbRoot: str = None,
               force: bool = False) -> dict:
    """给一条 pb_photo 行生成（或复用）缩略图，落盘到 thumb\\thumbs\\<xx>\\。

    参数
    ----
    row        : pb_photo 的一行（**至少要有 relPath 与 fileHash**）
    size       : 目标宽，必须 ∈ THUMB_SIZES
    photoRoot  : 原图根，缺省 paths.photo_dir()
    thumbRoot  : 缩略图根，缺省 paths.thumb_dir()
    force      : True 时忽略已有文件强制重生成（tools/gen_thumbs.py --rebuild 用）

    返回
    ----
    dict
      {
        "ok"        : bool,
        "created"   : bool   —— True=本次真的解码并落盘了
        "cached"    : bool   —— True=磁盘命中，未解码原图
        "absPath"   : str,
        "relpath"   : str,
        "bytes"     : int,   —— 落盘文件字节数
        "elapsed"   : float, —— 秒
        "photoCode" : str,
        "errMsg"    : str,   —— 仅 ok=False 时有
      }

    失败**不抛异常**：单张图解不出来（RAW 缺解码器 / 文件被外接盘带走了）
    不该让整轮批量生成崩掉。调用方看 ok 字段。
    """
    from common import globalDefinition as comGD

    start = time.time()
    result = {"ok": False, "created": False, "cached": False, "absPath": "",
              "relpath": "", "bytes": 0, "elapsed": 0.0, "photoCode": "",
              "errCode": "", "errMsg": ""}
    if not row:
        result["errCode"] = ERR_PARAM
        result["errMsg"] = "photoRow 为空"
        COUNTERS["failed"] += 1
        result["elapsed"] = time.time() - start
        return result

    fileHash = str(row.get("fileHash") or "").strip()
    relPath = str(row.get("relPath") or "").strip()
    result["photoCode"] = str(row.get("photoCode") or "")

    try:
        relpath = thumbStore.thumb_relpath(fileHash, size)
        absPath = thumbStore.thumb_abspath(fileHash, size, thumbRoot=thumbRoot)
    except thumbStore.ThumbStoreError as e:
        result["errCode"] = ERR_STORE
        result["errMsg"] = str(e)
        COUNTERS["failed"] += 1
        result["elapsed"] = time.time() - start
        return result
    result["relpath"] = relpath
    result["absPath"] = absPath

    # ---- 1) 磁盘命中：直接返回，**一次原图都不解码** ----
    if not force and thumbStore.exists(absPath):
        COUNTERS["hit"] += 1
        result.update(ok=True, created=False, cached=True,
                      bytes=thumbStore.fileSize(absPath))
        result["elapsed"] = time.time() - start
        return result

    # ---- 2) 未命中：解码 -> 编码 -> 原子写 ----
    COUNTERS["miss"] += 1
    try:
        origPath = thumbStore.orig_abs_path(relPath, photoRoot=photoRoot)
    except thumbStore.ThumbStoreError as e:
        result["errCode"] = ERR_STORE
        result["errMsg"] = str(e)
        COUNTERS["failed"] += 1
        result["elapsed"] = time.time() - start
        return result
    if not os.path.isfile(origPath):
        result["errCode"] = ERR_NO_ORIGINAL
        result["errMsg"] = "原图不在磁盘上: %s" % origPath
        COUNTERS["failed"] += 1
        result["elapsed"] = time.time() - start
        return result

    try:
        data = make_thumb_bytes(origPath, size)
    except PermissionError as e:
        # 被别的程序独占（相册软件正在导出）。是"文件拿不到"，不是"文件坏了"
        result["errCode"] = ERR_NO_ORIGINAL
        result["errMsg"] = "原图被占用 %s（%s）" % (e, comGD.errText(comGD.ERR_FILE_READ_ERROR))
        COUNTERS["failed"] += 1
        result["elapsed"] = time.time() - start
        return result
    except Exception as e:
        # ⚠️ **这里刻意不按 OSError 分流** —— 踩过：Pillow 对损坏文件抛的
        #   就是 `OSError("image file is truncated")`，而 UnidentifiedImageError
        #   更是 OSError 的子类。把 OSError 归到"原图不在"会把 9 张坏图
        #   全部误报成 404（"拔盘了？"），排障方向直接带偏。
        #   "原图不在"已由上面的 os.path.isfile 单独挡住了。
        #   其余一律当"原图在、就是解不出像素"，统一记日志但不让整批崩。
        result["errCode"] = ERR_DECODE
        result["errMsg"] = "解码失败 %s: %s" % (type(e).__name__, e)
        _LOG.error("make_thumb: %s(%s) 缩略图生成失败: %s"
                   % (row.get("photoCode"), relPath, e))
        COUNTERS["failed"] += 1
        COUNTERS["failedDecode"] += 1
        result["elapsed"] = time.time() - start
        return result

    try:
        # photoRoot 一起传：只读边界校验必须在同一个根上做，
        # 不能让写入守卫去读"另一个进程看到的" photo 目录
        thumbStore.write_atomic(absPath, data, tag=str(os.getpid()), photoRoot=photoRoot)
    except Exception as e:
        result["errCode"] = ERR_STORE
        result["errMsg"] = "写缩略图失败 %s: %s" % (type(e).__name__, e)
        _LOG.error("make_thumb: 写 %s 失败: %s" % (absPath, e))
        COUNTERS["failed"] += 1
        result["elapsed"] = time.time() - start
        return result

    result.update(ok=True, created=True, cached=False, bytes=len(data))
    result["elapsed"] = time.time() - start
    # 单张耗时只在 debug 级输出（开发计划 §6.4：10 万张不许刷屏）
    _LOG.debug("make_thumb: %s %s %.3fs %s"
               % (row.get("photoCode"), relpath, result["elapsed"],
                  misc.humanSize(len(data))))
    return result


# ============================================================
# 三、按需生成：线程池 + 同键互斥
# ============================================================
# 为什么需要互斥
# --------------
#   前端网格会并发拉几十张缩略图，而**同一张照片可能被多个请求同时要**
#   （缩略图刚生成完，首屏的 <img> 与懒加载的 <img> 会各发一次）。
#   没有互斥时这两条请求会各解码一次原图（4000x3000 JPEG ~120ms × 2），
#   并且都去写同一个 .tmp —— 后写的那条会把先写的那条截断成半文件。
#   互斥之后：第二条请求等第一条落盘，进来直接命中磁盘。
#
# 为什么用"条带锁"而不是"每个 key 一把锁"
# ---------------------------------------
#   每个 key 一把锁要给 10 万个 fileHash 各留一个 Lock 对象（字典本身约 6MB，
#   还要在文件被删后清理），纯属浪费。取 key 的 hash 对 256 取模做条带，
#   内存恒定；代价只是极低概率的"两张不同照片撞到同一条锁互相等一下"，
#   而锁内做的事本来就是 CPU 解码，等一下不亏。
_STRIPE_COUNT = 256
_STRIPES = [threading.Lock() for _ in range(_STRIPE_COUNT)]
_POOL_LOCK = threading.Lock()
_POOL = {"pool": None}


def _stripeFor(key: str) -> threading.Lock:
    return _STRIPES[hash(key) % _STRIPE_COUNT]


def getPool():
    """按需生成用的线程池（懒建）。workers 取自 basicSettings。"""
    with _POOL_LOCK:
        pool = _POOL["pool"]
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(
                max_workers=basicSettings.THUMB_ON_DEMAND_WORKERS,
                thread_name_prefix="pbThumb")
            _POOL["pool"] = pool
        return pool


def shutdownPool(wait: bool = True) -> None:
    """关掉按需线程池（进程退出 / 单测收尾用）"""
    with _POOL_LOCK:
        pool = _POOL["pool"]
        _POOL["pool"] = None
    if pool is not None:
        pool.shutdown(wait=wait)


def ensure_thumb(row: dict, size: int = thumbStore.THUMB_DEFAULT_SIZE,
                 photoRoot: str = None, thumbRoot: str = None,
                 timeout: float = 120.0) -> dict:
    """**同步**拿到缩略图（不存在就生成），供 HTTP 端点直接回文件用。

    与 make_thumb 的区别只是"丢进线程池同步等" —— 对调用方（FastAPI 同步端点，
    本身就跑在线程池里）来说，行为完全一致，区别在于：
      * make_thumb 给批量工具用，串行、可预测；
      * ensure_thumb 给按需用，走独立的小线程池，多个请求可以并行解码，
        同一张图串行（条带锁）。

    参数
    ----
    timeout : 等锁 + 等生成的秒数上限。超了返回 ok=False（errMsg 标明超时），
              **不抛异常** —— 让这一张图失败，好过整个请求队列卡死。
    """
    lock = _stripeFor("%s|%s" % (row.get("fileHash"), size))
    if not lock.acquire(timeout=timeout):
        return {"ok": False, "created": False, "cached": False, "absPath": "",
                "relpath": "", "bytes": 0, "elapsed": 0.0,
                "photoCode": str(row.get("photoCode") or ""), "errMsg": "等锁超时"}
    try:
        future = getPool().submit(make_thumb, row, size, photoRoot, thumbRoot, False)
        return future.result(timeout=timeout)
    except TimeoutError:
        return {"ok": False, "created": False, "cached": False, "absPath": "",
                "relpath": "", "bytes": 0, "elapsed": 0.0,
                "photoCode": str(row.get("photoCode") or ""), "errMsg": "生成超时"}
    except Exception as e:  # 兜底：任何意外都不许把HTTP 请求打成 500 栈
        _LOG.error("ensure_thumb 异常: %s: %s" % (type(e).__name__, e))
        return {"ok": False, "created": False, "cached": False, "absPath": "",
                "relpath": "", "bytes": 0, "elapsed": 0.0,
                "photoCode": str(row.get("photoCode") or ""),
                "errMsg": "%s: %s" % (type(e).__name__, e)}
    finally:
        lock.release()


# ============================================================
# 四、批量生成：进程池
# ============================================================

def _bulkChunk(task: tuple) -> list:
    """进程池的一个任务：处理一小批照片，**只碰文件系统，绝不连数据库**。

    必须是模块级函数（Windows 的 spawn 启动方式要能按名字 import 回来）。
    task = (rows, size, photoRoot, thumbRoot, force)
    """
    rows, size, photoRoot, thumbRoot, force = task
    return [make_thumb(row, size, photoRoot=photoRoot, thumbRoot=thumbRoot,
                       force=force)
            for row in rows]


def make_thumbs_bulk(rows: list, workers: int = None,
                     size: int = thumbStore.THUMB_DEFAULT_SIZE,
                     photoRoot: str = None, thumbRoot: str = None,
                     force: bool = False, chunkSize: int = None,
                     onProgress=None) -> dict:
    """批量生成缩略图（**进程池**，CPU 解码密集）。

    参数
    ----
    rows     : list[dict] —— pb_photo 行（主进程已查好，子进程不连库）
    workers  : 进程数，缺省 os.cpu_count()-1（留一个核给主进程与 I/O）
    size     : 目标宽
    force    : True = 全部重生成（tools/gen_thumbs.py --rebuild）
    chunkSize: 每个子进程任务携带多少张（basicSettings.THUMB_BULK_CHUNK）
    onProgress: 可选回调 onProgress(done, total, lastResult)，CLI 用来打进度

    返回
    ----
    dict 汇总 {total, cached, created, failed, elapsed, workers, thumbRoot,
               bytesTotal, failures:[{photoCode, relPath, errMsg}, ...]}

    为什么要分块（chunk）而不是一张一个任务
    --------------------------------------
    一张一个任务的 IPC + pickle 开销，在 10 万张上会变成几分钟纯等待；
    一块 16 张则摊薄到可忽略，且单块失败只需重跑 16 张。
    代价是块内串行（进程内没有并行），但块之间是真并行，总吞吐不受影响。
    """
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutTimeout

    total = len(rows or [])
    summary = {"total": total, "cached": 0, "created": 0, "failed": 0,
               "failedDecode": 0,
               "elapsed": 0.0, "workers": 0, "thumbRoot": thumbRoot or paths.thumb_dir(),
               "bytesTotal": 0, "failures": []}
    if total == 0:
        return summary

    cpu = os.cpu_count() or 4
    workerCount = max(1, int(workers if workers else max(1, cpu - 1)))
    summary["workers"] = workerCount
    step = max(1, int(chunkSize or basicSettings.THUMB_BULK_CHUNK))
    #⚠️ **必须在主进程把两个根都解析成绝对路径再分发给子进程**。
    #   子进程是 spawn 起来的全新解释器，读不到主进程的配置打桩
    #   （更一般地说：子进程若各自去读 local_settings，一旦两边的
    #   cwd/环境不同就会写到**两个不同的 thumb 目录**去 ——
    #   主进程统计说"生成成功"，磁盘上却一张都没有）。
    #   路径是纯数据，跨进程传递没有歧义。
    resolvedThumbRoot = os.path.abspath(thumbRoot or paths.thumb_dir())
    resolvedPhotoRoot = os.path.abspath(photoRoot or paths.photo_dir())
    tasks = [(rows[i:i + step], size, resolvedPhotoRoot, resolvedThumbRoot, force)
             for i in range(0, total, step)]

    start = time.time()
    done = 0
    # workers=1 时不起进程池：100 张以内、或单核机器上，
    # 起进程的开销大于并行收益，直接在主进程串行跑完。
    if workerCount == 1:
        for task in tasks:
            for one in _bulkChunk(task):
                done += 1
                _tally(summary, one)
                if onProgress:
                    onProgress(done, total, one)
        summary["elapsed"] = time.time() - start
        _logSummary("make_thumbs_bulk", summary)
        return summary

    try:
        with ProcessPoolExecutor(max_workers=workerCount) as pool:
            futures = [pool.submit(_bulkChunk, task) for task in tasks]
            for future in futures:
                try:
                    chunkResults = future.result()
                except Exception as e:
                    # 整个 chunk 崩了（例如子进程被 OOM killer 干掉）：
                    # 该块按"全部失败"记账，继续跑下一块，不让一批坏图毁掉整轮
                    _LOG.error("make_thumbs_bulk: 子进程块失败: %s: %s"
                               % (type(e).__name__, e))
                    summary["failed"] += step
                    summary["failures"].append({"photoCode": "", "relPath": "",
                                                "errMsg": "%s: %s" % (type(e).__name__, e)})
                    done += step
                    continue
                for one in chunkResults:
                    done += 1
                    _tally(summary, one)
                    if onProgress:
                        onProgress(done, total, one)
    except FutTimeout:      # pragma: no cover - 当前未设全局超时
        pass
    except Exception as e:  # 进程池起不来（受限机器 / 杀软拦 CreateProcess）
        _LOG.error("make_thumbs_bulk: 进程池不可用(%s)，退回线程池串行: %s"
                   % (type(e).__name__, e))
        summary["workers"] = 0
        for row in rows:
            one = make_thumb(row, size, photoRoot=resolvedPhotoRoot,
                             thumbRoot=resolvedThumbRoot, force=force)
            done += 1
            _tally(summary, one)
            if onProgress:
                onProgress(done, total, one)

    summary["elapsed"] = time.time() - start
    _logSummary("make_thumbs_bulk", summary)
    return summary


def _tally(summary: dict, one: dict) -> None:
    """把单张结果累加进批量汇总"""
    if one.get("ok"):
        if one.get("created"):
            summary["created"] += 1
            summary["bytesTotal"] += int(one.get("bytes") or 0)
        else:
            summary["cached"] += 1
    else:
        summary["failed"] += 1
        if one.get("errCode") == ERR_DECODE:
            # 单独计数：批量跑完看到"失败 9 张"没信息量，
            # "其中 9 张是原图坏了"才决定得了要不要去重新拷贝一次
            summary["failedDecode"] += 1
        if len(summary["failures"]) < 200:
            summary["failures"].append({"photoCode": one.get("photoCode", ""),
                                        "relPath": one.get("relpath", ""),
                                        "errCode": one.get("errCode", ""),
                                        "errMsg": one.get("errMsg", "")})


def _logSummary(tag: str, summary: dict) -> None:
    _LOG.info("%s: 共 %d 张 | 新生成 %d | 命中 %d | 失败 %d | %s | 进程数 %d | 耗时 %.1fs"
              % (tag, summary["total"], summary["created"], summary["cached"],
                 summary["failed"], misc.humanSize(summary["bytesTotal"]),
                 summary["workers"], summary["elapsed"]))


if __name__ == "__main__":
    import json

    print("thumbMaker _VERSION:", _VERSION)
    print("THUMB_SIZES        :", thumbStore.THUMB_SIZES)
    print("QUALITY / METHOD   :", basicSettings.THUMB_QUALITY, "/", basicSettings.THUMB_WEBP_METHOD)
    print("按需线程池          :", basicSettings.THUMB_ON_DEMAND_WORKERS)
    print("批量块大小          :", basicSettings.THUMB_BULK_CHUNK)
    print("counters:", json.dumps(countersSnapshot(), ensure_ascii=False))
    shutdownPool()
