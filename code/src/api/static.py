#! /usr/bin/env python3
#encoding: utf-8

#Filename: static.py
#Description: photo-browser 图片文件服务（步骤 4）—— 缩略图 / 原图 / 人脸裁剪图
#
# 三个端点
# ----------
#   GET|HEAD /api/thumb/{photoCode}?size=400
#       缩略图。命中磁盘直接 FileResponse；未命中**按需生成**。
#       带 ETag(= fileHash + size) + Cache-Control；支持 If-None-Match -> 304。
#   GET|HEAD /api/original/{photoCode}
#       原图。**必须支持 Range**（RFC 7233），否则浏览器滚动预加载会卡死；
#       MIME 按扩展名；支持 HEAD。
#   GET|HEAD /api/face/{faceCode}
#       人脸裁剪图（160px JPEG）。步骤 5 才有数据，本步先留好接口与路径推导。
#
# 三条纪律
# --------
#   1. **原图只读**：本模块对 photo 目录只有 open(...,"rb")，
#      连 os.stat 之外的写接口都不碰（连改名/删除的入口都不该有）。
#   2. **不裸 SQL**：查库一律走 database.auto_generated.sqliteCommon。
#   3. **绝不整读原图**：Range 与非 Range 都走流式，按 STREAM_CHUNK_SIZE 吐字节。
#      单张 RAW 60MB+，整读会把 uvicorn 的工作线程内存打爆。
#
# 为什么 Range 是"必须"而不是"锦上添花"
# ------------------------------------
#   浏览器的 <img> 在页面预加载、滚动懒加载、缩放手势（Ctrl+滚轮）时
#   都会发 Range 请求去**只取前几十 KB** 试读。一条不支持 Range 的接口，
#   这些优化全部退化：每次都传整张 20MB 的 RAW，页面直接卡住。
#   另一个现实理由：60MB 的 RAW 想中途取消时，
#   服务端必须能看到客户端断连（流式才能感知到，写在内存里的响应感知不到）。
#
# 关于多段 Range（"bytes=0-99,200-299"）
# --------------------------------------
#   按 RFC 7233 允许服务端**忽略** Range 头、退回 200 全量。
#   浏览器（尤其 <img>）实际上从不发多段 Range，真要支持 multipart/byteranges
#   只会引入一堆边界 bug，收益为零。本实现显式走"忽略"分支并注明。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                     # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon as sqliteCommon  # noqa: E402
from processor.media import faceCropper as faceCropper            # noqa: E402
from processor.media import thumbMaker as thumbMaker              # noqa: E402
from processor.media import thumbStore as thumbStore              # noqa: E402

from fastapi import APIRouter, HTTPException, Request             # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("apiStatic", "apistatic.log")

#: 本模块的路由（main/app.py 挂到 /api 前缀下）
router = APIRouter(tags=["static"])


# ============================================================
# 一、Range 解析（RFC 7233 §2.1 / §4.3）
# ============================================================

class RangeNotSatisfiable(Exception):
    """Range 合法但当前文件满足不了（必须回416 + Content-Range: bytes */size）"""

    def __init__(self, size: int):
        Exception.__init__(self, "Range 不可满足(size=%d)" % size)
        self.size = int(size)


def parseRangeHeader(value: str, fileSize: int) -> tuple:
    """解析 Range 头 -> (start, end) **闭区间**；None 表示"当作没有 Range"。

    支持的写法
    ----------
      bytes=0-1023      前 1024 字节
      bytes=500-        从第 500 字节到文件尾
      bytes=-500        **最后** 500 字节（后缀式）
      bytes=0-99999     end 超出文件尾 -> 截到size-1（合法，不算错）

    返回 None（= 忽略 Range，当普通 200 处理）的情形
    -----------------------------------------------
      * 头不是 bytes= 开头（其他单位本实现不认）
      * 多段 Range（"bytes=0-99,200-299"）—— 规范允许忽略
      * 语法乱（"bytes=abc"、缺 "-"）

    抛 RangeNotSatisfiable 的情形（回416）
    -----------------------------------
      * start >= fileSize（要的东西在文件之外）
      * 后缀长度 <= 0（"bytes=-0"）
      * end < start（"bytes=500-100"）
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if not text.lower().startswith("bytes="):
        return None
    spec = text[len("bytes="):].strip()
    if not spec or "-" not in spec:
        return None
    if "," in spec:
        # 多段 Range：规范允许忽略（见模块头说明）
        return None

    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if not first and not last:
        return None
    if fileSize <= 0:
        raise RangeNotSatisfiable(fileSize)

    if not first:
        # 后缀式 bytes=-N：取最后 N 字节
        if not last.isdigit():
            return None
        suffix = int(last)
        if suffix <= 0:
            raise RangeNotSatisfiable(fileSize)
        start = max(0, fileSize - suffix)
        end = fileSize - 1
    else:
        if not first.isdigit():
            return None
        if last and not last.isdigit():
            return None
        start = int(first)
        end = int(last) if last else fileSize - 1

    if start >= fileSize or end < start:
        raise RangeNotSatisfiable(fileSize)
    return (start, min(end, fileSize - 1))


def _iterFileRange(absPath: str, start: int, end: int, chunkSize: int = None):
    """按块读出 [start, end] 闭区间并yield —— **绝不整读原图**。

    每次 yield 一块，客户端断连时生成器被 GC，随即 with 语句关句柄。
    """
    step = int(chunkSize or basicSettings.STREAM_CHUNK_SIZE)
    remaining = end - start + 1
    with open(absPath, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(step, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def _matchesEtag(headerValue: str, etag: str) -> bool:
    """If-None-Match 是否命中（支持逗号分隔列表与 W/ 弱标签前缀）。

    "*" 恒命中（只要资源存在）。
    """
    if not headerValue:
        return False
    text = str(headerValue).strip()
    if text == "*":
        return True
    want = etag.strip('"')
    for one in text.split(","):
        token = one.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token.strip('"') == want:
            return True
    return False


def _quoteEtag(raw: str) -> str:
    """裸值 -> HTTP 头里的带引号 ETag"""
    text = "" if raw is None else str(raw).strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text
    return '"%s"' % text


def mimeOf(absPath: str, dbMime: str = "") -> str:
    """按扩展名定 MIME；查不到就用库里记录的 mimeType，再兜 application/octet-stream。

    为什么要自己映射：mimetypes 在 Windows 上是读注册表，
    .heic/.avif/.jxl 在多数机器上都查不到 -> 回退 octet-stream ->
    浏览器把<image> 当下载处理，界面上就是一个坏掉的图标。
    """
    ext = os.path.splitext(absPath or "")[1].lower()
    mime = basicSettings.MIME_BY_EXT.get(ext)
    if not mime:
        import mimetypes
        mime = mimetypes.guess_type(absPath or "")[0]
    if not mime and dbMime:
        mime = str(dbMime).split(";")[0].strip()
    return mime or "application/octet-stream"


def _httpDate(timestamp: float) -> str:
    import email.utils
    return email.utils.formatdate(timestamp, usegmt=True)


# ============================================================
# 二、查库（**只走 sqliteCommon，不裸 SQL**）
# ============================================================

def photoRow(photoCode: str) -> dict:
    """按 photoCode 取一条 pb_photo（未删）。查不到返回 {}。"""
    if not photoCode:
        return {}
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=str(photoCode),
                                      delFlag=comGD.DEL_FLAG_NO, limitNum=1)
    return rows[0] if rows else {}


def faceRow(faceCode: str) -> dict:
    """按 faceCode 取一条 pb_face（未删）。查不到返回 {}。"""
    if not faceCode:
        return {}
    rows = sqliteCommon.query_pb_face("pb_face", faceCode=str(faceCode),
                                     delFlag=comGD.DEL_FLAG_NO, limitNum=1)
    return rows[0] if rows else {}


def _notFound(detail: str) -> HTTPException:
    return HTTPException(status_code=404,
                        detail="%s（%s=%d）" % (detail, comGD.errText(comGD.ERR_FILE_NOT_FOUND),
                                                comGD.ERR_FILE_NOT_FOUND))


# ============================================================
# 三、GET /api/thumb/{photoCode}
# ============================================================

@router.api_route("/thumb/{photoCode}", methods=["GET", "HEAD"],
                  summary="缩略图（按需生成 + ETag + 304）")
def getThumb(photoCode: str, request: Request, size: int = thumbStore.THUMB_DEFAULT_SIZE):
    """返回缩略图 WebP。

    流程
    ----
      1. size 必须是 200/400/800 之一（**不静默回落**，前端传错要立刻知道）
      2. 查 pb_photo 拿 fileHash -> **推导**落盘路径（库里没有 thumbPath 字段）
      3. If-None-Match 命中 -> 直接 304（连stat 都不用做）
      4. 磁盘上有 -> FileResponse（**不解码原图**）
      5. 磁盘上没有 -> 按需生成（线程池 + 同键互斥）-> FileResponse
      6. 手动删掉缩略图后再请求，走第 5 步自动重建（验收第 6 条）

    响应头
    ------
      ETag          : "<fileHash>_<size>_<变体>"（内容寻址，size 必须参与，否则
                      同一张图切尺寸会命中错误缓存；变体是编码参数指纹，
                      改了质量重新生成后 ETag 才会变，见 thumbStore.thumb_variant）
      Cache-Control : public, max-age=31536000, immutable
                      （缩略图永不原地改写，同名即同内容，可以放心长期缓存）
      X-Thumb-Cache : hit | miss | generated | not-modified
                      —— 非标准诊断头，专门用来验"二次请求没有重复解码原图"

    出图失败时的状态码（**刻意不都是 500**）
    ------------------------------------------
      422 : 原图在磁盘上但**解不出像素**（截断、全零、扩展名骗人）。
            这不是服务端的 bug，重试一万次也一样 —— 回 5xx 会让前端和代理
            无限重试并弹错误提示。正解是显示"文件损坏"占位并把它标出来。
      404 : 原图不在磁盘上（拔了移动硬盘 / 目录挪走），重试有意义。
      500 : 落盘侧或数据不对（权限、路径脏数据）—— 这才是我们的问题。
    """
    try:
        width = thumbStore.normSize(size)
    except thumbStore.ThumbStoreError as e:
        raise HTTPException(status_code=400,
                            detail="%s（%s=%d）" % (e, comGD.errText(comGD.ERR_PARAM_INVALID),
                                                    comGD.ERR_PARAM_INVALID))

    row = photoRow(photoCode)
    if not row:
        raise _notFound("库中无此 photoCode: %s" % photoCode)

    try:
        etag = _quoteEtag(thumbStore.thumb_etag(row.get("fileHash"), width))
        absPath = thumbStore.thumb_abspath(row.get("fileHash"), width)
    except thumbStore.ThumbStoreError as e:
        raise HTTPException(status_code=500, detail="缩略图路径推导失败: %s" % e)

    cacheHeaders = {"ETag": etag, "Cache-Control": basicSettings.THUMB_CACHE_CONTROL}

    # ---- 304：ETag 命中，连磁盘都不用碰 ----
    if _matchesEtag(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=dict(cacheHeaders,
                                                      **{"X-Thumb-Cache": "not-modified"}))

    # ---- 命中磁盘 ----
    if thumbStore.exists(absPath):
        return FileResponse(absPath, media_type="image/webp",
                            headers=dict(cacheHeaders, **{"X-Thumb-Cache": "hit"}))

    # ---- 未命中：按需生成（线程池；同键互斥，见 thumbMaker.ensure_thumb）----
    made = thumbMaker.ensure_thumb(row, width)
    if not made.get("ok"):
        errCode = str(made.get("errCode") or "")
        status = thumbMaker.ERR_HTTP_STATUS.get(errCode, 500)
        if status == 500:
            _LOG.error("getThumb %s 生成失败[%s]: %s"
                       % (photoCode, errCode, made.get("errMsg")))
        else:
            # 422/404 不是我们这边的bug，日志降一个级别，别把日志刷爆
            _LOG.warning("getThumb %s 出图失败[%s]: %s"
                         % (photoCode, errCode, made.get("errMsg")))
        raise HTTPException(status_code=status,
                            detail="缩略图生成失败: %s" % (made.get("errMsg") or "未知"))
    if not thumbStore.exists(absPath):
        raise HTTPException(status_code=500, detail="缩略图生成后仍不存在: %s" % absPath)

    return FileResponse(absPath, media_type="image/webp",
                        headers=dict(cacheHeaders,
                                     **{"X-Thumb-Cache": "generated" if made.get("created")
                                        else "hit"}))


# ============================================================
# 四、GET /api/original/{photoCode}
# ============================================================

@router.api_route("/original/{photoCode}", methods=["GET", "HEAD"],
                  summary="原图（支持 Range）")
def getOriginal(photoCode: str, request: Request):
    """返回原图。**支持 HTTP Range**（206/416/Accept-Ranges）。

    行为矩阵
    --------
      Range: bytes=0-1023        -> 206 + Content-Range: bytes 0-1023/<size>
      Range: bytes=0-            -> 206 从 0 到尾
      Range: bytes=-1024         -> 206 最后 1024 字节
      Range: bytes=99999-100000  -> 416 + Content-Range: bytes */<size>
      Range: bytes=0-99,200-299  -> 200 全量（规范允许忽略多段 Range）
      无Range                    -> 200 + Content-Length: <size>
      HEAD                       -> 同状态码同头，**不返回 body**
      If-None-Match 命中         -> 304（**仅在无 Range 时**，
                                      有 Range 时该头的语义交给 If-Range）

    为什么非 Range 也要流式
    ----------------------
    Content-Length 明确给出（等于文件大小），前端能显示进度条；
    好处是 uvicorn 不必把 60MB 缓冲在内存里，且能感知客户端中途断开。
    """
    row = photoRow(photoCode)
    if not row:
        raise _notFound("库中无此 photoCode: %s" % photoCode)

    relPath = str(row.get("relPath") or "")
    try:
        absPath = thumbStore.orig_abs_path(relPath)
    except thumbStore.ThumbStoreError as e:
        # relPath 脏数据（正常流程不会出现）：这是**数据问题**不是文件问题
        raise HTTPException(status_code=500, detail="原图路径非法: %s" % e)

    if not os.path.isfile(absPath):
        # 库里有、磁盘没有 = isMissing 的情况（拔了移动硬盘），回404 而不是 500
        raise _notFound("原图不在磁盘上(可能已移走/拔盘): %s" % photoCode)

    try:
        fileSize = int(os.path.getsize(absPath))
    except OSError as e:
        raise HTTPException(status_code=500, detail="原图 stat 失败: %s" % e)

    mediaType = mimeOf(absPath, row.get("mimeType") or "")
    etag = _quoteEtag(str(row.get("fileHash") or photoCode))
    baseHeaders = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": _httpDate(os.path.getmtime(absPath)),
        "Cache-Control": "private, max-age=0, must-revalidate",
    }

    rangeHeader = request.headers.get("range")

    # ---- 304（仅无 Range 时）----
    if not rangeHeader and _matchesEtag(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=baseHeaders)

    # ---- Range ----
    if rangeHeader:
        try:
            span = parseRangeHeader(rangeHeader, fileSize)
        except RangeNotSatisfiable:
            headers = dict(baseHeaders)
            headers["Content-Range"] = "bytes */%d" % fileSize
            return Response(status_code=416, headers=headers)
        if span is not None:
            start, end = span
            headers = dict(baseHeaders)
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, fileSize)
            headers["Content-Length"] = str(end - start + 1)
            headers["Content-Type"] = mediaType
            if request.method == "HEAD":
                return Response(status_code=206, headers=headers)
            return StreamingResponse(_iterFileRange(absPath, start, end),
                                     status_code=206, headers=headers,
                                     media_type=mediaType)

    # ---- 无 Range：200 全量（仍是流式）----
    headers = dict(baseHeaders)
    headers["Content-Length"] = str(fileSize)
    headers["Content-Type"] = mediaType
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)
    return StreamingResponse(_iterFileRange(absPath, 0, fileSize - 1),
                             status_code=200, headers=headers, media_type=mediaType)


# ============================================================
# 五、GET /api/face/{faceCode}
# ============================================================

@router.api_route("/face/{faceCode}", methods=["GET", "HEAD"],
                  summary="人脸裁剪图（160px JPEG）")
def getFace(faceCode: str, request: Request):
    """按 faceCode 返回人脸裁剪图。

    路径完全可推导：faces\\<faceCode[:2]>\\<faceCode>.jpg（thumbStore.face_relpath）。

    **本步（步骤 4）还没有任何 pb_face 数据**，所以：
      * faceCode 查不到 -> 404
      * 查得到但裁剪图不在磁盘 -> 404（步骤 5 识别完才会落盘）
    刻意**不做按需生成**：人脸裁剪必须由步骤 5 的检测结果驱动，
    在这里"凭 faceCode 反推 bbox"是不可能的（库里存的是 faceBox，不在本端点参数里）。
    """
    row = faceRow(faceCode)
    if not row:
        raise _notFound("库中无此 faceCode（步骤 5 才会有人脸数据）: %s" % faceCode)
    try:
        etag = _quoteEtag(thumbStore.face_etag(faceCode))
        absPath = thumbStore.face_abspath(faceCode)
    except thumbStore.ThumbStoreError as e:
        raise HTTPException(status_code=500, detail="人脸图路径推导失败: %s" % e)

    cacheHeaders = {"ETag": etag, "Cache-Control": basicSettings.THUMB_CACHE_CONTROL}
    if _matchesEtag(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=cacheHeaders)
    if not thumbStore.exists(absPath):
        raise _notFound("人脸裁剪图尚未生成（步骤 5 产出）: %s" % faceCode)
    return FileResponse(absPath, media_type="image/jpeg", headers=cacheHeaders)


# ============================================================
# 六、诊断端点（验收与排障用）
# ============================================================

@router.get("/media/stats", summary="缩略图目录统计（验收/排障）")
def mediaStats() -> JSONResponse:
    """缩略图 / 人脸图目录统计 + 生成器计数器。

    刻意只读、只数文件，不扫原图。
    """
    return JSONResponse({
        "ok": True,
        "thumbRoot": paths.thumb_dir(),
        "thumbs": thumbStore.thumbStats(),
        "buckets": thumbStore.listBucketDirs(thumbStore.THUMB_SUBDIR),
        "faceBuckets": thumbStore.listBucketDirs(thumbStore.FACE_SUBDIR),
        "tmpFiles": thumbStore.findTmpFiles(),
        "counters": thumbMaker.countersSnapshot(),
    })


if __name__ == "__main__":
    print("api.static _VERSION:", _VERSION)
    print("路由              :", [(r.path, sorted(r.methods)) for r in router.routes])
    for spec, size in (("bytes=0-1023", 2000), ("bytes=500-", 2000),
                       ("bytes=-500", 2000), ("bytes=0-99999", 2000),
                       ("bytes=99999-100000", 2000), ("bytes=0-99,200-299", 2000),
                       ("bytes=abc", 2000), (None, 2000)):
        try:
            print("Range %-22r ->" % spec, parseRangeHeader(spec, size))
        except RangeNotSatisfiable as e:
            print("Range %-22r -> 416 (%d)" % (spec, e.size))
    print("ETag 匹配        :", _matchesEtag('"abc_400"', '"abc_400"'),
          _matchesEtag('W/"abc_400", "x"', '"abc_400"'),
          _matchesEtag('"other"', '"abc_400"'))
    print("MIME .heic/.nef  :", mimeOf("a.HEIC"), mimeOf("a.nef"), mimeOf("a.xyz"))
