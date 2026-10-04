#! /usr/bin/env python3
#encoding: utf-8

#Filename: walker.py
#Description: photo-browser 扫描器·遍历层 —— 递归遍历 photo 目录 + 双 hash
#
# 职责（开发计划 §3.1 扫描链路第1~2 步）
# --------------------------------------
#   1. listPhotoFiles()  递归遍历，扩展名白名单 + 排除目录过滤，**只 stat 不读内容**
#   2. iterPhotoFiles()  在上一步结果上按需算双 hash，逐条产出 FileEntry
#      · relPathHash = sha256(规范化后的相对路径)   ← 仅用于算 hash
#      · fileHash    = 流式 SHA-256，**8MB 分块**（绝，整文件读入）
#   3. countPhotoFiles() 只数个数（建任务时填 pb_scan_job.totalCount）
#
# 两条硬约束（写错就是原图风险 / 脏数据）
# -------------------------------------------
#   1. **relPath 保留磁盘原值**：只把分隔符统一成 "/"，**不改写**任何一段
#      （不做 NFC、不改大小写、不 trim、不消解".."）。规范化只发生在
#      paths.normalize_relpath() 里，且**只用于算 hash**。
#      这样库里 relPath 与磁盘实际路径逐字一致，出了事人才查得出来。
#   2. **photo 目录绝对只读**：本模块只 os.scandir / os.stat / open(...,"rb")，
#      没有任何写、不删、不改名、不改 mtime 的调用。
#
# 遍历顺序（断点续扫的前提）
# -------------------------
#   先把全部 (relPath, absPath) 收集起来按 relPath **字典序排好**，再逐条算 hash。
#   为什么不按目录深度优先的天然顺序：DFS 顺序与 relPath 字典序并不一致
#   （"a/b.jpg" 在 DFS 里可能排在 "a.jpg" 之后，因为 'b'<'.' 为假、目录先于文件），
#   一旦不一致，lastCursor 的 `> cursor` 过滤就会漏文件/重文件。
#   排序后 relPath 单调递增，lastCursor 就是一把严格的「水位线」，续扫语义唯一。
#   3 万条 relPath 常驻内存约 3MB，可接受。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))                # .../processor/scanner
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))                  # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc# noqa: E402
from common import paths as paths                                            # noqa: E402
from config import basicSettings as basicSettings                      # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("scannerWalker", "scannerwalker.log")


# ============================================================
# 一、遍历结果载体
# ============================================================

class FileEntry(object):
    """一个待入库文件的全部「事实」（hash 已算好）。

    字段
    ----
    absPath    : 绝对路径（磁盘原值）
    relPath    : 相对 photo 根的路径，**分隔符统一为 "/"，其余逐字保留**
    normPath   : 规范化后的相对路径（paths.normalize_relpath）—— **只用于算 hash**
    relPathHash: sha256(normPath)，路径级幂等键
    fileHash   : sha256(文件内容)，内容级去重键
    fileSize   : 字节数
    mtime      : 文件修改时间（epoch 秒，浮点）；shotYear 的最后兜底
    """

    __slots__ = ("absPath", "relPath", "normPath", "relPathHash",
                 "fileHash", "fileSize", "mtime")

    def __init__(self, absPath, relPath, normPath, relPathHash,
                 fileHash=None, fileSize=0, mtime=0.0):
        self.absPath = absPath
        self.relPath = relPath
        self.normPath = normPath
        self.relPathHash = relPathHash
        self.fileHash = fileHash
        self.fileSize = fileSize
        self.mtime = mtime

    def asDict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self) -> str:
        return ("FileEntry(relPath=%r, fileHash=%r, fileSize=%d)"
                % (self.relPath, self.fileHash, self.fileSize))


# ============================================================
# 二、路径与 hash
# ============================================================

def makeRelPath(root: str, absPath: str) -> str:
    """绝对路径 -> 相对 photo 根的 relPath（**分隔符统一为正斜杠，其余不改**）。

    ⚠️ 这里只做一件事：把 os.sep 换成 "/"。
       不做 NFC、不改大小写、不 strip、不消解 ".." —— 库里的 relPath
       必须与磁盘上的实际路径逐字对应（验收第9 条）。
    """
    rel = os.path.relpath(os.path.abspath(absPath), os.path.abspath(root))
    return rel.replace("\\", "/")


def calcRelPathHash(relPath: str) -> str:
    """relPathHash = sha256(规范化后的相对路径)。

    规范化 = 统一 "/"、去 "./"、Unicode NFC（paths.normalize_relpath）。
    **规范化结果只喂给 hash，绝不回写 relPath。**
    """
    return paths.relPathHash(relPath)


def calcFileHash(absPath: str, chunkSize: int = None) -> str:
    """流式 SHA-256。分块 8MB（basicSettings.HASH_CHUNK_SIZE），**禁止整读原图**。

    单张 RAW 可达 60MB+，整读会瞬时吃掉几十 MB 内存；分块后峰值只有 8MB。
    本函数只 open(...,"rb") 读，不写、不改 mtime（photo 只读硬约束）。
    """
    size = chunkSize or basicSettings.HASH_CHUNK_SIZE
    return misc.fileHashHex(absPath, chunkSize=size, algorithm=basicSettings.HASH_ALGORITHM)


# ============================================================
# 三、遍历
# ============================================================

def listPhotoFiles(root: str, excludedDirs=None) -> list:
    """递归收集 photo 根下所有「受支持的照片文件」，按 relPath 字典序返回。

    参数
    ----
    root         : 扫描根（= photo 目录）。不存在 / 不是目录直接返回空列表并记 error
    excludedDirs : 目录名黑名单，缺省用 basicSettings.EXCLUDED_DIR_NAMES
                   （@eaDir / .thumbnails / thumbs / faces / $RECYCLE.BIN ...）

    返回
    ----
    list[tuple[str, str]] —— [(relPath, absPath), ...]，已按 relPath 升序

    说明
    ----
      * 只 stat，**不打开文件**（不 hash），因此可以先廉价地拿到 totalCount；
      * 不跟随目录符号链接（follow_symlinks=False）：既防环，也防顺着链接
        跑到 photo 根之外去 —— 那会破坏 relPath 的语义（".." 逃逸）；
      * 单个目录读不了（权限/掉线）只记warning 并跳过，绝不让整个扫描失败。
    """
    result = []
    rootAbs = os.path.abspath(str(root))
    skipNames = frozenset(excludedDirs if excludedDirs is not None
                          else basicSettings.EXCLUDED_DIR_NAMES)

    if not os.path.isdir(rootAbs):
        _LOG.error("listPhotoFiles: 扫描根不存在或不是目录: %s" % rootAbs)
        return result

    # 显式栈做迭代式 DFS（不用 os.walk：os.walk 遇到不可读目录会静默跳过、
    # 且不便于统一控制「不跟随符号链接」）
    stack = [rootAbs]
    while stack:
        dirPath = stack.pop()
        try:
            with os.scandir(dirPath) as scanner:
                entries = list(scanner)
        except OSError as e:
            _LOG.warning("listPhotoFiles: 目录不可读已跳过: %s (%s)" % (dirPath, e))
            continue

        for entry in entries:
            name = entry.name
            try:
                isDir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if isDir:
                if name in skipNames:
                    continue
                stack.append(os.path.abspath(entry.path))
                continue
            # 文件（含指向文件的符号链接：内容仍在本树上，纳入）
            if not basicSettings.is_photo_file(name):
                continue
            try:
                if not entry.is_file(follow_symlinks=True):
                    continue
            except OSError:
                continue
            absPath = os.path.abspath(entry.path)
            result.append((makeRelPath(rootAbs, absPath), absPath))

    result.sort(key=lambda item: item[0])
    return result


def countPhotoFiles(root: str, excludedDirs=None) -> int:
    """只数个数（不 hash、不读内容）—— 建扫描任务时用来填 totalCount。"""
    return len(listPhotoFiles(root, excludedDirs=excludedDirs))


def makeEntry(absPath: str, relPath: str = None, root: str = None,
              hashContent: bool = True, chunkSize: int = None) -> FileEntry:
    """为一个已知文件造FileEntry（stat + 可选 hash）。**失败返回 None，绝不抛错。**

    单独抽出来是因为 runner 需要「拿着listPhotoFiles 的结果逐条处理」：
    列表已在手，就不必再走一遍生成器，也才判断得出「这批是不是最后一批」。

    ⚠️ 文件在遍历之后、这里 stat 之前被删/改名（外接盘随时可能掉）-> 返回 None，
       调用方跳过这一条即可，整轮扫描不受影响。
    """
    absPath = os.path.abspath(str(absPath))
    if relPath is None:
        if not root:
            return None
        relPath = makeRelPath(root, absPath)
    try:
        stat = os.stat(absPath)
    except OSError as e:
        _LOG.warning("makeEntry: stat 失败已跳过: %s (%s)" % (relPath, e))
        return None
    try:
        normPath = paths.normalize_relpath(relPath)
    except ValueError as e:
        _LOG.error("makeEntry: relPath 非法已跳过: %s (%s)" % (relPath, e))
        return None
    fileHash = None
    if hashContent:
        try:
            fileHash = calcFileHash(absPath, chunkSize=chunkSize)
        except OSError as e:
            _LOG.warning("makeEntry: 读文件失败已跳过: %s (%s)" % (relPath, e))
            return None
    return FileEntry(absPath=absPath, relPath=relPath, normPath=normPath,
                     relPathHash=calcRelPathHash(relPath), fileHash=fileHash,
                     fileSize=int(stat.st_size), mtime=float(stat.st_mtime))


def iterPhotoFiles(root: str, afterCursor: str = "", hashContent: bool = True,
                   excludedDirs=None, chunkSize: int = None) -> object:
    """逐条产出 FileEntry（生成器：先廉价收集全量清单，再边走边算 hash）。

    参数
    ----
    root        : 扫描根
    afterCursor : 断点游标。**只产出 relPath 严格大于它**的文件
                  （= 上一批最后处理完的那一个），实现「从 lastCursor 续扫」。
                  空串 = 从头扫。
    hashContent : False 时不读文件内容（fileHash=None，fileSize/mtime 仍取）。
                  供「只想看看有多少张」的轻量统计用。
    excludedDirs / chunkSize : 同 listPhotoFiles / calcFileHash

    产出
    ----
    FileEntry，按 relPath 升序
    """
    cursor = "" if not afterCursor else str(afterCursor)
    for relPath, absPath in listPhotoFiles(root, excludedDirs=excludedDirs):
        if cursor and relPath <= cursor:
            continue
        entry = makeEntry(absPath, relPath=relPath, hashContent=hashContent,
                          chunkSize=chunkSize)
        if entry is not None:
            yield entry


def walkPhotos(root: str, afterCursor: str = "", hashContent: bool = True,
               excludedDirs=None, chunkSize: int = None) -> list:
    """iterPhotoFiles 的列表版（一次性物化）。3 万张约 3MB，CLI/测试用。"""
    return list(iterPhotoFiles(root, afterCursor=afterCursor, hashContent=hashContent,
                              excludedDirs=excludedDirs, chunkSize=chunkSize))


if __name__ == "__main__":
    import time

    target = sys.argv[1] if len(sys.argv) > 1 else paths.photo_dir()
    print("walker _VERSION :", _VERSION)
    print("扫描根:", target)
    start = time.time()
    items = walkPhotos(target)
    print("照片数          :", len(items), "（耗时 %.2fs）" % (time.time() - start))
    for entry in items[:5]:
        print("   ", entry)
    if items:
        print("relPath 升序?  :",
              all(items[i].relPath < items[i + 1].relPath for i in range(len(items) - 1)))
        print("总字节         :", misc.humanSize(sum(e.fileSize for e in items)))
