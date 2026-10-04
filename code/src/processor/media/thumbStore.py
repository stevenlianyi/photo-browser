#! /usr/bin/env python3
#encoding: utf-8

#Filename: thumbStore.py
#Description: photo-browser 缩略图/人脸裁剪图的**落盘规则**（步骤 4）
#
# 职责（只有落盘规则，不碰图像、不碰数据库）
# ------------------------------------------------
#   1. thumb_relpath(fileHash, size)  推导缩略图相对路径
#   2. face_relpath(faceCode)         推导人脸裁剪图相对路径
#   3. thumb_abspath / face_abspath   拼绝对路径
#   4. ensure_bucket_dir(relpath)     按需创建分桶目录
#   5. write_atomic(absPath, data)    原子写：先 <name>.tmp，再 os.replace
#   6. orig_abs_path(relPath)         由 relPath 还原原图绝对路径（含只读边界校验）
#
# 为什么路径必须"可推导"（DR-1 / 数据库设计 Q-5）
# ------------------------------------------------
#   `pb_photo` **没有 thumbPath 字段** —— 缩略图不入库（Q-5）。
#   于是文件名只能由 `fileHash` + `size` 算出来，库与文件系统之间不留冗余：
#   * 库小（10 万行省掉 10 万个路径字段）
#   * 缩略图目录整体可删可重建（生成物），重建后路径分毫不差
#   * 代价：**这两个函数是全项目唯一的路径真相**，改了就等于换了一套规则，
#     老缩略图会集体变成孤儿文件（库里又没字段能找回它们）。
#
# 目录布局（已定稿，不得改动）
# --------------------------
#   <thumbRoot>\
#     thumbs\<fileHash[:2]>\<fileHash>_<size>.webp     size ∈ (200, 400, 800)
#     faces\<faceCode[:2]>\<faceCode>.jpg              160px（步骤 5 用）
#
# 为什么要分桶（hash[:2]）
# ----------------------
#   10 万张照片若全堆在一个目录下，Windows 的目录项查找会明显劣化
#   （NTFS 的 MFT 扇出是 O(1) 的，但 10 万项目录会让"建索引/删索引/列目录"
#   都变慢，且任何一次误操作都是十万量级的）。按前 2 位十六进制分 256 个桶，
#   平均每桶 400 个文件、单桶最坏也就千级 —— 步骤 4 验收第 5 条盯的就是这个上限。
#
# 硬约束
# ------
#   1. **photo 目录绝对只读**：write_atomic 会校验目标路径不在 photo 之内，
#      命中即抛错（"手滑把配置改错"这类事故在这里被挡住，而不是等到验收才发现）；
#   2. **缩略图绝不留半文件**：任何写入都经write_atomic，
#      前端要么看不到这个文件（还没 replace），要么看到完整文件。

import os
import re
import sys

# 让本文件在任意 cwd / 直接 `python processor\media\thumbStore.py` 执行时都能 import 到兄弟包
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/media
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("thumbStore", "thumbstore.log")


# ============================================================
# 一、常量（转发自 basicSettings，配置项集中，此处只做别名）
# ============================================================

#: 缩略图多尺寸（WebP）
THUMB_SIZES: tuple = tuple(basicSettings.THUMB_SIZES)
#: 缺省缩略图尺寸
THUMB_DEFAULT_SIZE: int = basicSettings.THUMB_DEFAULT_SIZE
#: 分桶子目录名
THUMB_SUBDIR: str = basicSettings.THUMB_SUBDIR
FACE_SUBDIR: str = basicSettings.FACE_SUBDIR
#: 分桶取前几位
HASH_BUCKET_LEN: int = basicSettings.HASH_BUCKET_LEN
#: 落盘扩展名
THUMB_EXT: str = basicSettings.THUMB_EXT
FACE_EXT: str = basicSettings.FACE_EXT
#: 原子写临时文件扩展名
TMP_EXT: str = basicSettings.TMP_EXT

#: 库中 photoCode / faceCode 的最大长度（pb_photo.photoCode VARCHAR(64)）
CODE_MAX_LEN: int = 64

# 编码只允许十六进制 —— fileHash 是 sha256、faceCode 由本项目生成，
# 出现非十六进制字符一律视为脏数据，**绝不允许它拼进路径**（防目录穿越）
_CODE_RE = re.compile(r"^[0-9a-f]+$")


class ThumbStoreError(Exception):
    """落盘规则被违反（非法编码 / 试图写入 photo 目录 / 目标已存在等）。"""


# ============================================================
# 二、路径推导（**全项目唯一的路径真相**）
# ============================================================

def _normCode(code, fieldName: str) -> str:
    """校验并归一一个编码（fileHash / faceCode）。

    规则
    ----
      * None / 空 -> ThumbStoreError（调用方据此跳过，不静默生成垃圾路径）
      * 统一小写（Windows 上路径本就不敏感，但 ETag、比对需要一致形态）
      * 必须是纯十六进制，且长度 <= CODE_MAX_LEN —— **这是目录穿越的唯一防线**：
        编码里若混进 ".." / "/" / 盘符，拼出来的路径就能逃出thumbRoot。
    """
    text = "" if code is None else str(code).strip().lower()
    if not text:
        raise ThumbStoreError("%s 为空" % fieldName)
    if len(text) > CODE_MAX_LEN:
        raise ThumbStoreError("%s 超长(%d > %d): %.16s..."
                              % (fieldName, len(text), CODE_MAX_LEN, text))
    if not _CODE_RE.match(text):
        raise ThumbStoreError("%s 非法（只允许十六进制）: %r" % (fieldName, code))
    return text


def bucketOf(code: str) -> str:
    """取分桶目录名 =编码前 HASH_BUCKET_LEN 位（不足则取全部）。"""
    return _normCode(code, "code")[:HASH_BUCKET_LEN]


def normSize(size=None) -> int:
    """归一缩略图尺寸；不合法直接抛 ThumbStoreError（**不静默回落 400**）。

    静默回落是最坏的一种错：前端要800 却拿到 400，界面上看不出任何异常，
    只是"图看着有点糊"，往往几天后才发现。宁可 400 报错。
    """
    value = THUMB_DEFAULT_SIZE if size is None else int(size)
    if value not in THUMB_SIZES:
        raise ThumbStoreError("size 必须是 %s 之一，收到 %r" % (list(THUMB_SIZES), size))
    return value


def thumb_relpath(fileHash: str, size: int = THUMB_DEFAULT_SIZE) -> str:
    """缩略图相对 thumbRoot 的路径（**正斜杠**，入库/打日志/跨平台都用这个形态）。

    例
    --
        thumb_relpath("ab12cd34...", 400)
        -> 'thumbs/ab/ab12cd34..._400.webp'

    路径分量全部来自十六进制编码，**不含任何用户输入**，因此不可能穿越目录。
    """
    code = _normCode(fileHash, "fileHash")
    width = normSize(size)
    return "%s/%s/%s_%d%s" % (THUMB_SUBDIR, code[:HASH_BUCKET_LEN], code, width, THUMB_EXT)


def face_relpath(faceCode: str) -> str:
    """人脸裁剪图相对 thumbRoot 的路径。

    例
    --
        face_relpath("f0a1b2c3...") -> 'faces/f0/f0a1b2c3....jpg'
    """
    code = _normCode(faceCode, "faceCode")
    return "%s/%s/%s%s" % (FACE_SUBDIR, code[:HASH_BUCKET_LEN], code, FACE_EXT)


def thumb_abspath(fileHash: str, size: int = THUMB_DEFAULT_SIZE, thumbRoot: str = None) -> str:
    """缩略图绝对路径。thumbRoot 缺省取 paths.thumb_dir()（DR-8派生）。"""
    return os.path.join(thumbRoot or paths.thumb_dir(),
                        *thumb_relpath(fileHash, size).split("/"))


def face_abspath(faceCode: str, thumbRoot: str = None) -> str:
    """人脸裁剪图绝对路径。"""
    return os.path.join(thumbRoot or paths.thumb_dir(),
                        *face_relpath(faceCode).split("/"))


def orig_abs_path(relPath: str, photoRoot: str = None) -> str:
    """由库中 relPath 还原原图绝对路径（**只读打开，绝不写**）。

    参数
    ----
    relPath   : pb_photo.relPath 原值（分隔符可能是 "\\" 或 "/"）
    photoRoot : 照片根，缺省paths.photo_dir()

    边界校验
    --------
    还原结果必须仍在photoRoot 之内（`_isWithin`）。库里若出现
    "../别的盘/x.jpg" 这类脏数据（正常流程不会产生），在这里被挡住，
    而不是让 /api/original 把 photo 目录外的任意文件当原图吐出去。
    """
    root = os.path.abspath(photoRoot or paths.photo_dir())
    rel = ("" if relPath is None else str(relPath)).replace("\\", "/").strip()
    if not rel:
        raise ThumbStoreError("relPath 为空")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ThumbStoreError("relPath 不是相对路径: %r" % relPath)
    target = os.path.abspath(os.path.join(root, *rel.split("/")))
    if not _isWithin(target, root):
        raise ThumbStoreError("relPath 逃出照片根: %r" % relPath)
    return target


# ========================================================
# 三、只读边界（photo 绝对只读）
# ============================================================

def _isWithin(child: str, parent: str) -> bool:
    """child 是否等于 parent 或位于 parent 之下（Windows 下大小写不敏感）。

    与 paths._isWithin 同义；这里刻意**重新实现一份**而不是 import 私有名，
    免得paths 内部重构时本模块被无声打断。
    """
    ck = os.path.normcase(os.path.abspath(str(child)))
    pk = os.path.normcase(os.path.abspath(str(parent)))
    while len(pk) > 3 and pk[-1] in ("\\", "/"):
        pk = pk[:-1]
    if ck == pk:
        return True
    return ck.startswith(pk + os.sep)


def isUnderPhotoDir(absPath: str, photoRoot: str = None) -> bool:
    """目标绝对路径是否落在原图目录之内（内部用；测试与守卫用）"""
    return _isWithin(absPath, photoRoot or paths.photo_dir())


def assertNotPhoto(absPath: str, photoRoot: str = None) -> str:
    """守卫：拒绝任何写入原图目录的企图，**命中即抛 ThumbStoreError**。

    与 globalDefinition.ERR_FILE_READONLY_VIOLATION 对应。
    单条报错比"跑完十万张才发现缩略图把原图覆盖了"便宜太多。
    """
    if isUnderPhotoDir(absPath, photoRoot):
        from common import globalDefinition as comGD
        raise ThumbStoreError("%s（%s=%d）"
                              % ("拒绝写入 photo 目录：%s" % absPath,
                                 comGD.errText(comGD.ERR_FILE_READONLY_VIOLATION),
                                 comGD.ERR_FILE_READONLY_VIOLATION))
    return os.path.abspath(absPath)


# ============================================================
# 四、目录与原子写
# ============================================================

def ensure_bucket_dir(relpath: str, thumbRoot: str = None) -> str:
    """按需创建缩略图/人脸图所在的分桶目录，返回该目录绝对路径。

    幂等（exist_ok=True），可以放心每张都调。
    ⚠️ 传入的 relpath **必须来自 thumb_relpath / face_relpath**（本模块生成，
       路径分量只含十六进制），因此不会穿越到 thumbRoot 之外。
    """
    absPath = os.path.join(thumbRoot or paths.thumb_dir(), *str(relpath).split("/"))
    dirPath = os.path.dirname(absPath)
    if os.path.isdir(dirPath):
        return dirPath
    try:
        os.makedirs(dirPath, exist_ok=True)
    except OSError as e:
        _LOG.error("ensure_bucket_dir: 建目录失败 %s (%s)" % (dirPath, e))
        raise
    return dirPath


def tmpPathOf(absPath: str, tag: str = "") -> str:
    """原子写的临时文件路径。

    tag 为空 -> `<name>.tmp`（验收第 5 条按这个形态检查残留）。
    tag 非空 -> `<name>.<tag>.tmp`，给**并发**场景用：
    批量生成时同一个 fileHash 若被两个子进程同时处理（重跑 gen_thumbs 与
    按需生成撞车），共用一个 .tmp 会互相截断出半文件。带上 pid 就各写各的，
    os.replace 依然是原子的；残留检查仍能用 *.tmp 抓到。
    """
    dirPath, name = os.path.split(os.path.abspath(absPath))
    return os.path.join(dirPath, name + ("." + str(tag) if tag else "") + TMP_EXT)


def write_atomic(absPath: str, data, tag: str = "", photoRoot: str = None) -> str:
    """原子写一个文件（开发计划 §6.4）。

    流程
    ----
        1. 守卫：目标不得落在 photo 目录之内（原图只读硬约束）
        2. 校验 data 是 bytes-like
        3. ensure_bucket_dir 建好分桶目录
        4. 写 <name>.tmp
        5. os.replace(tmp,正式名)      ← 同一目录同一文件系统，POSIX/NTFS 均原子
        6. 任何异常 -> 删掉 tmp 后重抛，**磁盘上不留半文件**

    为什么必须这样
    --------------
    前端是并发拉图的：同一张图可能被多个请求同时要。
    若直接 open(正式名,"wb") 写，一个请求会在另一个请求写到一半时
    读到几百字节的残缺 WebP，界面上就是"裂图"，且**事后无从判断是生成坏了还是
    传输坏了**。os.replace 保证：读者只会看到"完整旧文件"或"完整新文件"。

    参数
    ----
    absPath : 目标绝对路径
    data    : bytes / bytearray / memoryview
    tag     : 临时文件名后缀（并发隔离，见 tmpPathOf；单线程可留空）
    photoRoot: 只读边界校验用的照片根，缺省 paths.photo_dir()

    返回
    ----
    str —— 目标绝对路径
    """
    target = assertNotPhoto(absPath, photoRoot)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ThumbStoreError("write_atomic 只接受 bytes-like，收到 %s" % type(data).__name__)
    payload = bytes(data)

    # 目录已存在就直接写；不存在才建（分桶目录建好之后就不必再 makedirs）
    dirPath = os.path.dirname(target)
    if not os.path.isdir(dirPath):
        try:
            os.makedirs(dirPath, exist_ok=True)
        except OSError as e:
            _LOG.error("write_atomic: 建目录失败 %s (%s)" % (dirPath, e))
            raise

    tmpPath = tmpPathOf(target, tag)
    try:
        with open(tmpPath, "wb") as handle:
            handle.write(payload)
            handle.flush()
            # 关键：不 fsync 也能防「前端读到半文件」（半文件窗口在 replace 之前，
            # 而replace 之前 tmp 根本不是正式名）。fsync 只防「掉电丢数据」，
            # 缩略图丢了能重生成，代价不值得每张都刷一次盘 —— 故有意省略。
        os.replace(tmpPath, target)
    except Exception as e:
        # 失败清理：宁可磁盘上什么都没有，也不能留一个会被当缩略图读的半文件
        try:
            if os.path.exists(tmpPath):
                os.remove(tmpPath)
        except OSError as ce:
            _LOG.error("write_atomic: 清理临时文件失败 %s (%s)" % (tmpPath, ce))
        _LOG.error("write_atomic: 原子写失败 %s (%s)" % (target, e))
        raise
    return target


def read_bytes(absPath: str) -> bytes:
    """读整个缩略图/人脸图（**只读缩略图，不读原图**）。

    缩略图上限 800px WebP（几十~几百 KB），整读无压力；
    原图一律走 api/static.py 的流式 + Range，不许经过这里。
    """
    with open(absPath, "rb") as handle:
        return handle.read()


def fileSize(absPath: str) -> int:
    """文件字节数；不存在返回 0（调用方据此判断"要不要重新生成"）"""
    try:
        return int(os.path.getsize(absPath))
    except OSError:
        return 0


def exists(relOrAbsPath: str, thumbRoot: str = None) -> bool:
    """缩略图/人脸图是否已落盘且**非空**。

    刻意不只用 os.path.exists：0 字节的缩略图一定是坏的（生成中断残留），
    当作"不存在"重新生成，比让前端拿到一张裂图好。
    """
    absPath = relOrAbsPath
    if not os.path.isabs(absPath):
        absPath = os.path.join(thumbRoot or paths.thumb_dir(), *absPath.split("/"))
    return fileSize(absPath) > 0


def listBucketDirs(kind: str = THUMB_SUBDIR, thumbRoot: str = None) -> list:
    """列出已建的分桶目录（验收"目录分桶生效"用）。kind: thumbs / faces"""
    root = os.path.join(thumbRoot or paths.thumb_dir(), kind)
    if not os.path.isdir(root):
        return []
    return sorted(name for name in os.listdir(root)
                  if os.path.isdir(os.path.join(root, name)))


def findTmpFiles(thumbRoot: str = None) -> list:
    """找出所有残留的 *.tmp（验收第 5 条：中断后必须为空）。

    刻意扫**整个** thumbRoot 递归，而不是只扫 thumbs/ —— 步骤 5 的人脸裁剪图
    也走 write_atomic，一起查。
    """
    root = thumbRoot or paths.thumb_dir()
    found = []
    if not os.path.isdir(root):
        return found
    for dirPath, _dirNames, fileNames in os.walk(root):
        for name in fileNames:
            if name.lower().endswith(TMP_EXT):
                found.append(os.path.join(dirPath, name))
    return sorted(found)


def thumbStats(thumbRoot: str = None) -> dict:
    """缩略图目录统计（验收第 1/5/7 条用）：文件数 / 总体积 / 单目录最大文件数。"""
    root = os.path.join(thumbRoot or paths.thumb_dir(), THUMB_SUBDIR)
    stat = {"thumbRoot": root, "exists": os.path.isdir(root),
            "files": 0, "bytes": 0, "buckets": 0, "maxPerBucket": 0,
            "maxBucketName": ""}
    if not stat["exists"]:
        return stat
    for name in sorted(os.listdir(root)):
        bucketDir = os.path.join(root, name)
        if not os.path.isdir(bucketDir):
            continue
        stat["buckets"] += 1
        count = 0
        for entry in os.scandir(bucketDir):
            if entry.is_file(follow_symlinks=False):
                count += 1
                try:
                    stat["bytes"] += int(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    pass
        stat["files"] += count
        if count > stat["maxPerBucket"]:
            stat["maxPerBucket"] = count
            stat["maxBucketName"] = name
    return stat


# ============================================================
# 五、ETag（内容寻址：fileHash + size + 编码参数）
# ============================================================

def thumb_variant() -> str:
    """缩略图**编码参数指纹**，形如 `q82m4`。

    为什么要它
    ----------
      ETag 是浏览器缓存的唯一依据，而缩略图是**磁盘上的旧文件**：
      改了 `THUMB_QUALITY` 或换了 Pillow 版本重新生成之后，
      URL 没变、文件Hash 也没变 —— 若 ETag 里没有编码参数，
      浏览器会**一直**拿304 把旧图供下去，怎么刷都刷不出来。
      指纹进了 ETag，改了配置就是一批新 URL，老缓存自然失效。
    指纹随进程启动时读到的配置算一次，**不缓存**（测试里改配置立即生效）。
    """
    return "q%dm%d" % (int(basicSettings.THUMB_QUALITY),
                       int(basicSettings.THUMB_WEBP_METHOD))


def face_variant() -> str:
    """人脸裁剪图编码参数指纹：尺寸 + 形状 + JPEG 质量"""
    return "s%dq%d%s" % (int(basicSettings.FACE_CROP_SIZE),
                         int(basicSettings.FACE_CROP_QUALITY),
                         "sq" if basicSettings.FACE_CROP_SQUARE else "ar")


def thumb_etag(fileHash: str, size: int = THUMB_DEFAULT_SIZE) -> str:
    """缩略图 ETag = sha256(内容) + 尺寸 + 编码参数，**不含路径**。

    带上 size 是必须的：同一张照片的 200/400/800 是三个不同文件，
    只用 fileHash 会让前端在切换尺寸时命中错误的缓存条目。

    引号由调用方补（HTTP 头里 ETag 必须是带引号的字符串），
    本函数只给"裸值"，方便落日志与比较。
    """
    code = _normCode(fileHash, "fileHash")
    return "%s_%d_%s" % (code, normSize(size), thumb_variant())


def face_etag(faceCode: str) -> str:
    """人脸裁剪图 ETag = faceCode + 编码参数（本身即唯一标识）"""
    return "%s_%s" % (_normCode(faceCode, "faceCode"), face_variant())


if __name__ == "__main__":
    import json

    print("thumbStore _VERSION :", _VERSION)
    print("THUMB_SIZES        :", THUMB_SIZES, "缺省", THUMB_DEFAULT_SIZE)
    print("thumb_relpath(示例) :", thumb_relpath("ab12cd34" + "0" * 56, 400))
    print("face_relpath (示例) :", face_relpath("f0a1b2c3" + "0" * 56))
    print("thumbRoot          :", paths.thumb_dir())
    print("thumbStats         :", json.dumps(thumbStats(), ensure_ascii=False))
    print("残留 .tmp          :", findTmpFiles())
    try:
        thumb_relpath("../escape", 400)
    except ThumbStoreError as e:
        print("非法编码被拒        :", e)
