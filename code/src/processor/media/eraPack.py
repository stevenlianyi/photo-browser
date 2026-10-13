#! /usr/bin/env python3
#encoding: utf-8

#Filename: eraPack.py
#Description: photo-browser 年代抽样 zip 打包（R11 · DR-48）——
#             把一次抽样的照片打成自描述的 zip
#
# 为什么放在 processor/media（而不是 engine/sample）
# ------------------------------------------------
#   它是「读原图、产出文件」的媒体活，与缩略图/人脸裁剪同一层。
#   engine/sample 只管「抽哪几张」（写库），**不碰 photo 目录** ——
#   这条边界让抽样算法能在没有照片库的单测里跑。
#
# 五条硬纪律
# ----------
#   ① **绝不写 `photo\`**：原图只读是项目红线（common/paths.py:216 写死
#      「绝不创建、绝不写入」）。产物一律落 `paths.exports_dir()`，
#      它是 writable_dirs() 白名单内的目录（paths.py:284）。
#   ② **写盘直接写目标文件，不进内存**：一个人的抽样可能几十张高清原图
#      （几百 MB），整包塞进 BytesIO 是事故（对照 DR-12 的 157.5MB 峰值教训）。
#   ③ **先写临时文件，成功后再 os.replace 改名**：中途失败绝不留下一个
#      「看起来是成品」的半截 zip —— 那种文件能打开、能解压一部分，
#      用户不会怀疑，直到发现少了几年的照片。
#   ④ **原图缺失不是失败**：跳过 + 计数 + 写进 manifest/README。
#      只报「成功 38 张」而悄悄丢 2 张 = 静默少数据。
#   ⑤ **只动自己的批次行**：状态回写一律走 eraSampler.updateZipState
#      （批次表只有一个写者），本模块不直接写 pb_era_sample。
#
# zip 是**自描述**的（脱离本程序也能看懂）
# --------------------------------------
#   年代抽样_张明_ES_20261011_ab12cd/
#   ├── 1958/IMG_0042.jpg
#   ├── NOYEAR/xxx.jpg
#   ├── manifest.csv     ← 「为什么是这几张」的答案（年份/候选数/权重/概率）
#   └── README.txt       ← 参数快照 + 随机种子 + 缺失清单

import csv
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))           # .../processor/media
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                            # noqa: E402
from common import paths as paths                                # noqa: E402
from config import basicSettings as basicSettings                # noqa: E402
from engine.sample import eraSampler as eraSampler               # noqa: E402
from processor.media import thumbStore as thumbStore             # noqa: E402

_VERSION = "20261011"

_LOG = misc.setLogNew("eraPack", "erapack.log")

#: 「无年份」在 zip 里的目录名。加下划线是为了让它排在年份目录之后
#: （文件名排序时 '1' < '_'），与队列里「无年份排最后」的约定一致。
NOYEAR_DIR: str = "_%s" % basicSettings.ERA_SAMPLE_NOYEAR_LABEL

#: 目录/文件名里必须替换掉的字符（Windows 上非法，且会破坏 zip 内路径）
_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: 单段名字的最大长度（给重名后缀留位置）
_MAX_SEGMENT: int = 80

#: 原图缺失时的 manifest 备注
MISS_REASON: str = "原图不在磁盘上"

#: 年代档包内的**机器可读元数据**文件名（R15）。
#: 为什么需要它：这是「不落库」方案里唯一能回答「这个包是按**哪一份**照片集
#: 打的」的地方。没有它，「包内张数 ≠ 当前张数」既可能是**原图缺失**
#: （常态，重打也一样少），也可能是**照片集真的变了**（该重打）——
#: 两者分不开，界面就只能永远显示「已过期」，而用户重打一百次也没用。
META_NAME: str = "_meta.json"


class EraPackError(Exception):
    """打包失败（批次不存在 / 落盘失败）。"""


# ============================================================
# 一、路径与命名
# ============================================================

def zipRelName(batchCode: str) -> str:
    """zip 相对 `exports\\` 的路径（**正斜杠**，落库与日志统一用这个形态）。

    落盘名用纯 ASCII 的 `<batchCode>.zip`（中文名只出现在**下载时的
    Content-Disposition** 里）—— 中文路径在不同编码的 shell/工具里会出事，
    而 zip 文件自己叫什么对用户几乎无感。
    """
    return "%s/%s.zip" % (basicSettings.ERA_SAMPLE_ZIP_SUBDIR, str(batchCode or ""))

def zipAbsPath(batchCode: str) -> str:
    """zip 的绝对路径（`exports_dir()` 之下）。"""
    return os.path.join(paths.exports_dir(),
                        *zipRelName(batchCode).split("/"))


def zipAbsPathOf(relName: str) -> str:
    """由库里的 `zipRelName` 还原绝对路径（读取/下载用）。

    ⚠️ 必须挡住越界：库里的值若被人改成 `../../x.zip`，还原出来就成了
       照片库外的任意文件 —— 与 `thumbStore.orig_abs_path` 同一道边界校验。
    """
    root = os.path.abspath(paths.exports_dir())
    rel = str(relName or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise EraPackError("zipRelName 不是相对路径: %r" % relName)
    target = os.path.abspath(os.path.join(root, *rel.split("/")))
    if target != root and not target.startswith(root + os.sep):
        raise EraPackError("zipRelName 逃出 exports 目录: %r" % relName)
    return target


def downloadName(batch: dict, batchCode: str) -> str:
    """下载时对用户显示的文件名：`年代抽样_<人名>_<批次码>.zip`。

    人名取**批次里的快照**（`personName`），不是现查 pb_person ——
    人物改名/停用之后，历史批次的 zip 名仍应对应「当时那一次」。
    """
    person = sanitizeSegment(str((batch or {}).get("personName") or "")) or "未知人物"
    return "年代抽样_%s_%s.zip" % (person, str(batchCode or ""))


def sanitizeSegment(text: str) -> str:
    """把一段文本变成可安全用作目录/文件名的片段。

    保留中文（用户看得懂才有意义），只清掉路径分隔符、Windows 保留字符
    与控制字符，并把连续的空白/点收敛掉（`..` 会被当上级目录）。
    """
    value = _BAD_CHARS_RE.sub("_", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(". ").strip()
    if len(value) > _MAX_SEGMENT:
        value = value[:_MAX_SEGMENT].strip()
    return value


def _uniqueName(used: set, name: str) -> str:
    """在同目录内去重：`IMG_1.jpg` -> `IMG_1_2.jpg`。

    ⚠️ 不做去重会**静默丢图**：zipfile 允许同名成员，但解压时后者会覆盖前者
       （或者解压工具直接报错），而 zip 本身完全合法、大小也对。
    """
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    index = 2
    while "%s_%d%s" % (stem, index, ext) in used:
        index += 1
    final = "%s_%d%s" % (stem, index, ext)
    used.add(final)
    return final


# ============================================================
# 二、清单与说明文件
# ============================================================

def _manifestCsv(batch: dict, items: list, missing: list) -> str:
    """manifest.csv 文本（**带 UTF-8 BOM**，否则 Excel 打开是乱码）。

    它回答的是「**为什么是这几张**」：每行都有当年的候选数、抽中那张的
    权重与概率 —— 没有这几列，用户看着一条年份队列只能选择相信。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["年份", "photoCode", "zip内文件名", "原始相对路径", "拍摄时间",
                     "人脸质量", "检测分", "权重", "抽中概率", "当年候选数",
                     "是否人工确认", "备注"])
    for item in items:
        candidateCount = int(item.get("candidateCount") or 0)
        weight = _floatOrNone(item.get("weight"))
        weightSum = _floatOrNone(item.get("weightSum"))
        probability = None
        if weight is not None and weightSum:
            probability = "%.4f" % (weight / weightSum)
        writer.writerow([
            str(item.get("yearLabel") or ""),
            str(item.get("photoCode") or ""),
            str(item.get("zipName") or ""),
            str(item.get("relPath") or ""),
            str(item.get("takenAt") or ""),
            _num(item.get("quality")),
            _num(item.get("detScore")),
            _num(weight),
            probability or "",
            candidateCount,
            "是" if int(item.get("isConfirmed") or 0) else "否",
            str(item.get("missReason") or ""),
        ])
    return "\ufeff" + buffer.getvalue()


def _readmeText(batch: dict, batchCode: str, items: list, missing: list) -> str:
    """README.txt：参数快照 + 随机种子 + 缺失清单 + 口径说明。"""
    lines = [
        "photo-browser 年代抽样导出",
        "=" * 40,
        "批次编码   : %s" % batchCode,
        "人物       : %s (%s)" % (str(batch.get("personName") or ""),
                                  str(batch.get("personCode") or "")),
        "生成时间   : %s" % str(batch.get("regYMDHMS") or ""),
        "随机种子   : %s" % str(batch.get("randomSeed") or ""),
        "抽法       : %s" % str(batch.get("mode") or ""),
        "粒度       : 每年一张（%s）" % str(batch.get("grain") or "year"),
        "年份范围   : %s ~ %s" % (str(batch.get("yearFrom") or "不限"),
                                  str(batch.get("yearTo") or "不限")),
        "含无年份   : %s" % ("是" if int(batch.get("includeNoYear") or 0) else "否"),
        "仅人工确认 : %s" % ("是" if int(batch.get("onlyConfirmed") or 0) else "否"),
        "排除重复件 : %s" % ("是" if int(batch.get("excludeDuplicate") or 0) else "否"),
        "候选照片数 : %s" % str(batch.get("candidatePhotoCount") or 0),
        "实打包张数 : %d" % (len(items) - len(missing)),
        "缺失张数   : %d" % len(missing),
        "",
        "照片按拍摄年份分目录存放；「无拍摄年份」的在 %s/ 下。" % NOYEAR_DIR,
        "年份取自**有效拍摄年**（人工修正优先于 EXIF/文件名/mtime）。",
        "manifest.csv 里逐张写了当年的候选数与抽中概率 —— 「为什么是这几张」看它。",
        "同一个种子 + 同样的参数可以复现同一批（前提：候选集没有变化 ——",
        "新增照片、改判归属都会改变候选池）。",
    ]
    if missing:
        lines += ["", "以下 %d 张的**原图在磁盘上找不到**，未能打包：" % len(missing)]
        for one in missing:
            lines.append("  - %s/%s  (%s)  %s"
                         % (str(one.get("yearLabel") or ""), str(one.get("photoCode") or ""),
                            str(one.get("relPath") or ""), str(one.get("missReason") or "")))
    lines += ["", "由 photo-browser 生成；原图目录未被修改（只读）。"]
    return "\r\n".join(lines) + "\r\n"


def _num(value):
    """数值 -> 字符串（取不到给空串，**不填 0**：0 会被读成「非常差」）。"""
    got = _floatOrNone(value)
    return "" if got is None else ("%.4f" % got)


def _floatOrNone(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# 三、打包（唯一入口）
# ============================================================

def ensureZip(batchCode: str, force: bool = False, batch: dict = None) -> dict:
    """确保批次有 zip 产物，返回状态 dict（**幂等**：已有产物直接复用）。

    参数
    ----
      batchCode : 批次编码
      force     : True = 已有产物也重打（原图补齐后想重打时用）
      batch     : 已取到的批次行（省一次查询；不给则自己查）

    返回
    ----
      {zipState, zipRelName, zipAbsPath, zipSize, zipPhotoCount,
       zipMissingCount, cached, missing:[...]}

    失败一律**先回写 FAILED + errMsg 再抛**：
      不写状态的话，批次看上去还是 NONE（=还没打过），
      下次点下载会重来一遍，而真正的原因（磁盘满 / 权限 / 原图目录拔了）
      永远不出现在界面上。
    """
    code = str(batchCode or "").strip()
    row = batch if batch else eraSampler.batchRow(code)
    if not row:
        raise EraPackError("批次不存在: %r" % batchCode)

    relName = str(row.get("zipRelName") or "") or zipRelName(code)
    target = zipAbsPathOf(relName)
    if (not force) and str(row.get("zipState") or "") == "READY" and os.path.isfile(target):
        return {"zipState": "READY", "zipRelName": relName, "zipAbsPath": target,
                "zipSize": int(row.get("zipSize") or 0),
                "zipPhotoCount": int(row.get("zipPhotoCount") or 0),
                "zipMissingCount": int(row.get("zipMissingCount") or 0),
                "cached": True, "missing": []}

    try:
        return _build(code, row, relName, target)
    except Exception as e:                                       # noqa: BLE001
        eraSampler.updateZipState(code, "FAILED", zipRelName=relName,
                                  errMsg="%s: %s" % (type(e).__name__, e))
        if isinstance(e, EraPackError):
            raise
        raise EraPackError("打包失败：%s" % e)


def _build(batchCode: str, batch: dict, relName: str, target: str) -> dict:
    """真正干活的那一半（把异常收敛留给 ensureZip 统一回写状态）。"""
    items = eraSampler.itemRows(batchCode)
    folder = sanitizeSegment("年代抽样_%s_%s" % (str(batch.get("personName") or "未知人物"),
                                              batchCode)) or batchCode

    paths.ensure_dirs()                       # exports 在白名单内（DR-48），photo 不在
    outDir = os.path.dirname(target)
    if outDir and not os.path.isdir(outDir):
        os.makedirs(outDir, exist_ok=True)

    packed, missing = [], []
    usedPerDir = {}
    # ③ 先写临时文件，全部成功后 os.replace —— 中途失败绝不留下半截成品
    tmpPath = target + ".part"
    try:
        with zipfile.ZipFile(tmpPath, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=int(basicSettings.ERA_SAMPLE_ZIP_COMPRESS_LEVEL)) as zf:
            for item in items:
                record = dict(item)
                if not _addOne(zf, folder, record, usedPerDir):
                    missing.append(record)
                packed.append(record)
            zf.writestr("%s/manifest.csv" % folder,
                        _manifestCsv(batch, packed, missing))
            zf.writestr("%s/README.txt" % folder,
                        _readmeText(batch, batchCode, packed, missing))
        os.replace(tmpPath, target)
    finally:
        if os.path.isfile(tmpPath):
            try:
                os.remove(tmpPath)
            except OSError:
                _LOG.warning("清理临时产物失败（无害）: %s", tmpPath)

    size = os.path.getsize(target)
    photoCount = len(packed) - len(missing)
    eraSampler.updateZipState(batchCode, "READY", zipRelName=relName,
                              zipSize=size, zipPhotoCount=photoCount,
                              zipMissingCount=len(missing), errMsg="")
    _LOG.info("年代抽样 zip 就绪 %s: 打包 %d 张 / 缺失 %d 张 / %d 字节",
              batchCode, photoCount, len(missing), size)
    return {"zipState": "READY", "zipRelName": relName, "zipAbsPath": target,
            "zipSize": size, "zipPhotoCount": photoCount,
            "zipMissingCount": len(missing), "cached": False,
            "missing": [{"yearLabel": str(one.get("yearLabel") or ""),
                         "photoCode": str(one.get("photoCode") or ""),
                         "relPath": str(one.get("relPath") or ""),
                         "reason": str(one.get("missReason") or "")}
                        for one in missing]}


def _addOne(zf, folder: str, record: dict, usedPerDir: dict) -> bool:
    """把一条明细的原图写进 zip。成功返回 True，跳过（缺失）返回 False。

    ⚠️ 取原图路径**只能**走 `thumbStore.orig_abs_path`：它是唯一带
       「结果必须仍在 photoRoot 之内」校验的出口。自己拼 `photo_dir() + relPath`
       就等于把那条边界校验绕过去（库里若有脏数据，就能把照片库外的
       任意文件打进 zip 发给别人）。
    """
    relPath = str(record.get("relPath") or "")
    try:
        absPath = thumbStore.orig_abs_path(relPath)
    except Exception:                                            # noqa: BLE001
        record["missReason"] = "relPath 非法（逃出照片根）"
        record["zipName"] = ""
        return False
    if not os.path.isfile(absPath):
        record["missReason"] = MISS_REASON
        record["zipName"] = ""
        return False

    yearDir = str(record.get("yearLabel") or "")
    if yearDir == basicSettings.ERA_SAMPLE_NOYEAR_LABEL:
        yearDir = NOYEAR_DIR
    yearDir = sanitizeSegment(yearDir) or "UNKNOWN"

    name = sanitizeSegment(os.path.basename(absPath)) or "%s.jpg" % str(record.get("photoCode") or "photo")
    used = usedPerDir.setdefault(yearDir, set())
    final = _uniqueName(used, name)
    arcName = "%s/%s/%s" % (folder, yearDir, final)
    zf.write(absPath, arcName)
    record["zipName"] = arcName
    record["missReason"] = ""
    return True


# ============================================================
# 四、年代档全量打包（R15 · DR-56 ~ DR-58）
# ============================================================
#
# 与上面「抽样打包」的关系：**同一层、同一套内核，语义不同**。
#   · 抽样包：一个批次 = 每年一张，zip 里有「权重/抽中概率」这类抽样专属列。
#   · 年代档包：一个档 = **全部**照片（已排除重复件），
#     没有权重、没有概率，只有「这张在哪、哪年拍的、是否人工确认过」。
#
# 三条与抽样包**不同**的纪律
# --------------------------
#   ① **不落库**（DR-56）：抽样包要回写 pb_era_sample 的 zipState；
#      年代档包的状态 = 「文件在不在」+ 进程内任务态。所以本段
#      **一次 sqliteCommon.update_* 都不调**。
#   ② **不进内存**：一个档可达 GB 级，条目列表由调用方从只读查询得到后
#      传进来（本模块**自己连库**，保持「media 层不查业务口径」的边界）。
#   ③ **临时名带 tag**：抽样的固定 `<target>.part` 在同一目标并发时会互相
#      破坏；这里走 `thumbStore.tmpPathOf(target, tag=jobCode)`。

def bucketZipRelName(personCode: str, bucketKey: str) -> str:
    """年代档 zip 相对 `exports\\` 的路径（**正斜杠**）。

    落盘名全 ASCII：`<personCode>__<bucketKey>.zip`（双下划线做分隔，
    因为 personCode 与 bucketKey 各自都可能含单个下划线）。
    中文人名只出现在**下载时**的 Content-Disposition 里 —— 与抽样包同一条理由。
    """
    person = sanitizeSegment(str(personCode or "unknown")) or "unknown"
    key = sanitizeSegment(str(bucketKey or "NOYEAR")) or "NOYEAR"
    return "%s/%s__%s.zip" % (basicSettings.ERA_ZIP_SUBDIR, person, key)


def bucketZipAbsPath(personCode: str, bucketKey: str) -> str:
    """年代档 zip 的绝对路径（`exports_dir()` 之下）。"""
    return zipAbsPathOf(bucketZipRelName(personCode, bucketKey))


def downloadBucketName(personName: str, bucketText: str) -> str:
    """下载时对用户显示的文件名：`年代照片_<人名>_<年代档>.zip`。

    人名由调用方从 **pb_person 现查**后传入（年代档包不落库，没有「当时的
    快照」可依；这与抽样包取 `batch.personName` 快照的做法不同，是
    「不落库」这个选择的直接后果，已记在 plan/年代打包规划.md §三）。
    """
    person = sanitizeSegment(str(personName or "")) or "未知人物"
    span = sanitizeSegment(str(bucketText or "")) or "未知年代"
    return "年代照片_%s_%s.zip" % (person, span)


def estimateBytes(photos) -> int:
    """照片行 -> 预计包体字节数（= 各图 `fileSize` 之和）。

    为什么要预估：一档几百张原图，**用户点之前就该知道要占多少磁盘**。
    这个数字是纯加总，不读盘、不压测，毫秒级。
    """
    total = 0
    for one in photos or []:
        total += max(0, int(_floatOrNone((one or {}).get("fileSize")) or 0))
    return total


def ensureDiskSpace(needBytes: int, extraBytes: int = None) -> dict:
    """确认 `exports\\` 所在磁盘放得下这个包，放不下就抛（**先失败，别写一半**）。

    余量 = 压缩后的富余 + 固定缓冲：deflate 对 JPEG 几乎无收益，
    所以「需要多少」基本就是原图总和；这里再留 `ERA_PACK_DISK_SLACK` 与
    一份等量缓冲，避免「打到 99% 磁盘满了」——那会留下一个半截临时文件，
    用户只看到「失败」，而真正的原因是磁盘。
    """
    need = max(0, int(needBytes or 0))
    slack = int(basicSettings.ERA_PACK_DISK_SLACK)
    root = paths.exports_dir()
    try:
        os.makedirs(root, exist_ok=True)
        usage = shutil.disk_usage(root)
    except OSError as e:
        raise EraPackError("无法读取磁盘可用空间（%s）：%s" % (root, e))
    required = need + max(slack, int(extraBytes or 0))
    if usage.free < required:
        raise EraPackError("磁盘可用空间不足：需要约 %.2f GB，可用 %.2f GB（%s）"
                           % (required / 1073741824.0, usage.free / 1073741824.0, root))
    return {"free": int(usage.free), "required": required, "needBytes": need,
            "total": int(usage.total)}


def countZipPhotos(zipPath: str) -> int:
    """数一个 zip 里的**照片文件**数（不含 manifest.csv / README.txt）。

    只读**中央目录**，不解压 —— 1GB 的包也是几十毫秒。
    幂等复用时用它回答「包里到底有几张」，而不是相信一个可能存在别处的计数
    （本项目不落库，没有那个「别处」）。
    """
    if not os.path.isfile(zipPath):
        return 0
    try:
        with zipfile.ZipFile(zipPath, "r") as zf:
            names = zf.namelist()
    except Exception as e:                                       # noqa: BLE001
        _LOG.warning("countZipPhotos: 读中央目录失败 %s: %s", zipPath, e)
        return 0
    skip = ("manifest.csv", "README.txt", META_NAME)
    return sum(1 for one in names
               if not one.endswith("/") and os.path.basename(one) not in skip)


def readZipMeta(zipPath: str) -> dict:
    """读年代档包内的 `_meta.json`（读不到一律返回 `{}`，**不抛**）。

    只解压那一个几百字节的成员 —— 1 GB 的包也是毫秒级。
    调用方必须容忍空字典：老包（R15 之前没有这个文件）与第三方改过的包都会读不到。
    """
    if not os.path.isfile(zipPath):
        return {}
    try:
        with zipfile.ZipFile(zipPath, "r") as zf:
            for name in zf.namelist():
                if os.path.basename(name) == META_NAME:
                    return json.loads(zf.read(name).decode("utf-8"))
    except Exception as e:                                       # noqa: BLE001
        _LOG.warning("readZipMeta: 读 %s 失败（按「读不到」处理）: %s", zipPath, e)
    return {}


def zipInfoOf(personCode: str, bucketKey: str) -> dict:
    """年代档 zip 的**现状**（从文件系统读；不存在返回 exists=False）。

    这是「不落库」方案的唯一状态来源：存在性、大小、生成时间、
    包内张数、**以及打包时那一档有多少张**（`photoTotalAtPack`，用于判过期）
    全部现场从文件和 zip 中央目录得到。
    """
    target = bucketZipAbsPath(personCode, bucketKey)
    if not os.path.isfile(target):
        return {"exists": False, "zipSize": 0, "zipYMDHMS": None,
                "zipPhotoCount": 0, "photoTotalAtPack": 0,
                "zipRelName": bucketZipRelName(personCode, bucketKey)}
    stat = os.stat(target)
    meta = readZipMeta(target)
    return {"exists": True, "zipSize": int(stat.st_size),
            "zipYMDHMS": time.strftime("%Y%m%d%H%M%S", time.localtime(stat.st_mtime)),
            "zipPhotoCount": countZipPhotos(target),
            "photoTotalAtPack": int(meta.get("photoTotal") or 0),
            "zipRelName": bucketZipRelName(personCode, bucketKey)}


def ensureBucketZip(personCode: str, personName: str, bucketKey: str, bucketText: str,
                    photos: list, force: bool = False, onProgress=None,
                    jobCode: str = "") -> dict:
    """确保某年代档有 zip 产物，返回状态 dict（**幂等**：已有产物直接复用）。

    参数
    ----
      personCode / personName : 人物（人名只用于包内目录名与下载名）
      bucketKey / bucketText  : 年代档键（如 `2005-2009`）与显示文案
      photos                  : **已固定快照**的照片行（来自 bucketPool，
                                一张照片一行，含 photoCode/relPath/effYear/…）
      force                   : True = 已有产物也重打
      onProgress              : `onProgress(done, total)` —— 按**张数**上报
      jobCode                 : 临时文件名里的 tag（并发/重入时互不覆盖）

    返回
    ----
      {zipState, zipRelName, zipAbsPath, zipSize, zipPhotoCount,
       zipMissingCount, cached, photoTotal, missing:[…]}

    ⚠️ 与 `ensureZip`（抽样包）的差别：**不写任何库**。失败时也不回写状态 ——
       没有行可写。失败信息靠异常文本带回给调用方的进程内任务态。
    """
    code = str(personCode or "").strip()
    key = str(bucketKey or "").strip()
    if not code:
        raise EraPackError("personCode 为空")
    if not key:
        raise EraPackError("bucketKey 为空")
    items = [dict(one) for one in (photos or [])]
    relName = bucketZipRelName(code, key)
    target = zipAbsPathOf(relName)

    if (not force) and os.path.isfile(target):
        info = zipInfoOf(code, key)
        return {"zipState": "READY", "zipRelName": relName, "zipAbsPath": target,
                "zipSize": info["zipSize"], "zipPhotoCount": info["zipPhotoCount"],
                "zipMissingCount": 0, "cached": True, "photoTotal": len(items),
                "missing": []}
    try:
        return _buildBucket(code, personName, key, bucketText, relName, target,
                            items, onProgress, jobCode)
    except EraPackError:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise EraPackError("打包失败：%s: %s" % (type(e).__name__, e))


def _buildBucket(personCode: str, personName: str, bucketKey: str, bucketText: str,
                 relName: str, target: str, items: list, onProgress,
                 jobCode: str) -> dict:
    """年代档全量包的实际写盘（异常收敛留给 ensureBucketZip）。"""
    folder = sanitizeSegment("年代照片_%s_%s" % (str(personName or "未知人物"), bucketText)) \
        or ("年代照片_%s" % personCode)

    paths.ensure_dirs()                       # exports 在白名单内，photo 不在
    outDir = os.path.dirname(target)
    if outDir and not os.path.isdir(outDir):
        os.makedirs(outDir, exist_ok=True)

    packed, missing = [], []
    usedPerDir = {}
    total = len(items)
    if onProgress is not None:
        onProgress(0, total)
    # ③ 先写临时文件，全部成功后 os.replace —— 中途失败绝不留下半截成品。
    #    ⚠️ tag 用 jobCode：同一个目标被两个线程同时打时各写各的临时文件，
    #    完成后 os.replace 依然是原子的（抽样的固定 `.part` 做不到这一点）。
    tmpPath = thumbStore.tmpPathOf(target, tag=str(jobCode or os.getpid()))
    try:
        with zipfile.ZipFile(tmpPath, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=int(basicSettings.ERA_SAMPLE_ZIP_COMPRESS_LEVEL)) as zf:
            for index, item in enumerate(items, start=1):
                record = dict(item)
                if not _addBucketOne(zf, folder, record, usedPerDir):
                    missing.append(record)
                packed.append(record)
                if onProgress is not None:
                    onProgress(index, total)
            zf.writestr("%s/manifest.csv" % folder,
                        _bucketManifestCsv(packed))
            zf.writestr("%s/README.txt" % folder,
                        _bucketReadmeText(personCode, personName, bucketKey,
                                          bucketText, packed, missing))
            # 机器可读元数据：**判过期的唯一依据**（见 META_NAME 处的说明）。
            zf.writestr("%s/%s" % (folder, META_NAME),
                        _bucketMetaJson(personCode, bucketKey, folder, packed, missing))
        os.replace(tmpPath, target)
    finally:
        if os.path.isfile(tmpPath):
            try:
                os.remove(tmpPath)
            except OSError:
                _LOG.warning("清理临时产物失败（无害）: %s", tmpPath)

    size = os.path.getsize(target)
    photoCount = len(packed) - len(missing)
    _LOG.info("年代档 zip 就绪 %s/%s: 打包 %d 张 / 缺失 %d 张 / %d 字节",
              personCode, bucketKey, photoCount, len(missing), size)
    return {"zipState": "READY", "zipRelName": relName, "zipAbsPath": target,
            "zipSize": size, "zipPhotoCount": photoCount,
            "zipMissingCount": len(missing), "cached": False, "photoTotal": total,
            "missing": [{"photoCode": str(one.get("photoCode") or ""),
                         "relPath": str(one.get("relPath") or ""),
                         "reason": str(one.get("missReason") or "")}
                        for one in missing]}


def _addBucketOne(zf, folder: str, record: dict, usedPerDir: dict) -> bool:
    """把一条照片的原图写进年代档 zip。成功 True / 跳过（缺失）False。

    目录规则与抽样包一致：**按有效拍摄年**分目录，「无年份」落 `_NOYEAR/`。
    年份取自 `bucketPool.effectiveYearOf`（调用方已算好放在 `effYear`），
    这里只做「0/空 -> _NOYEAR」的呈现。
    """
    relPath = str(record.get("relPath") or "")
    try:
        absPath = thumbStore.orig_abs_path(relPath)
    except Exception:                                            # noqa: BLE001
        record["missReason"] = "relPath 非法（逃出照片根）"
        record["zipName"] = ""
        return False
    if not os.path.isfile(absPath):
        record["missReason"] = MISS_REASON
        record["zipName"] = ""
        return False

    yearLabel = str(_floatOrNone(record.get("effYear")) or "").split(".")[0]
    if not yearLabel or yearLabel in ("0", "None"):
        yearDir = NOYEAR_DIR
        record["yearLabel"] = basicSettings.ERA_SAMPLE_NOYEAR_LABEL
    else:
        yearDir = sanitizeSegment(yearLabel) or "UNKNOWN"
        record["yearLabel"] = yearLabel

    name = sanitizeSegment(os.path.basename(absPath)) \
        or "%s.jpg" % str(record.get("photoCode") or "photo")
    used = usedPerDir.setdefault(yearDir, set())
    final = _uniqueName(used, name)
    arcName = "%s/%s/%s" % (folder, yearDir, final)
    zf.write(absPath, arcName)
    record["zipName"] = arcName
    record["missReason"] = ""
    return True


def _bucketManifestCsv(items: list) -> str:
    """年代档包的 manifest.csv（**带 UTF-8 BOM**，否则 Excel 打开是乱码）。

    列与抽样包的差别：**没有**「权重 / 抽中概率 / 当年候选数」——
    这里不做任何抽样，那些列要么恒空要么恒 1，留着只会让人以为「是不是抽过了」。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["年份", "photoCode", "zip内文件名", "原始相对路径", "拍摄时间",
                     "人脸质量", "检测分", "是否人工确认", "备注"])
    for item in items:
        writer.writerow([
            str(item.get("yearLabel") or ""),
            str(item.get("photoCode") or ""),
            str(item.get("zipName") or ""),
            str(item.get("relPath") or ""),
            str(item.get("takenAt") or ""),
            _num(item.get("quality")),
            _num(item.get("detScore")),
            "是" if int(item.get("isConfirmed") or 0) else "否",
            str(item.get("missReason") or ""),
        ])
    return "\ufeff" + buffer.getvalue()


def _bucketMetaJson(personCode: str, bucketKey: str, folder: str,
                    packed: list, missing: list) -> str:
    """包内 `_meta.json`：**机器可读**的打包快照。

    与 README.txt 的分工：README 是给人看的散文，这个是给程序看的判据。
    `photoTotal` 是**打包那一刻该档有多少张照片**（已排除重复件）——
    界面把它与现在现算的张数一比，就能区分：
      · 相等   -> 包是新的（哪怕包内文件数更少：那只是原图缺失）
      · 不等   -> 照片集真的变了（新增/删除/改判归属），该重打
    没有这个字段，两种情形分不开，界面只能永远显示「已过期」。
    """
    payload = {
        "app": "photo-browser",
        "kind": "era-bucket-pack",
        "version": _VERSION,
        "personCode": str(personCode or ""),
        "bucketKey": str(bucketKey or ""),
        "folder": str(folder or ""),
        "excludeDuplicate": True,
        "photoTotal": len(packed),
        "photoCount": len(packed) - len(missing),
        "missingCount": len(missing),
        "packedYMDHMS": misc.getTime(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _bucketReadmeText(personCode: str, personName: str, bucketKey: str,
                      bucketText: str, items: list, missing: list) -> str:
    """README.txt：人物 + 年代档 + 张数 + 缺失清单 + 口径说明。

    ⚠️ 一定要写「**全部照片**（已排除重复件）」这一句：抽样包与年代档包
       长得几乎一样（都是按年份分目录的原图 + 两个元数据文件），
       用户拿到手分不清哪个是「他这一年的那几张」哪个是「他这个年代的全部」。
    """
    lines = [
        "photo-browser 年代档照片导出",
        "=" * 40,
        "人物       : %s (%s)" % (str(personName or ""), str(personCode or "")),
        "年代档     : %s" % str(bucketText or bucketKey or ""),
        "档键       : %s" % str(bucketKey or ""),
        "生成时间   : %s" % misc.getTime(),
        "照片张数   : %d" % (len(items) - len(missing)),
        "缺失张数   : %d" % len(missing),
        "",
        "本包是**该年代档下的全部照片**（已排除重复件；原图不在磁盘上的已跳过）。",
        "照片按**有效拍摄年**分目录存放（人工修正的年份优先于 EXIF/文件名/mtime）；",
        "「无拍摄年份」的照片在 %s/ 下。" % NOYEAR_DIR,
        "manifest.csv 里逐张写了原相对路径与拍摄时间 —— 每张照片对应哪一行看它。",
        "⚠️ 界面上的张数可能比本包多：那里还包含**重复件**与**原图已不在磁盘上**的。",
    ]
    if missing:
        lines += ["", "以下 %d 张的**原图在磁盘上找不到**，未能打包：" % len(missing)]
        for one in missing:
            lines.append("  - %s  %s  (%s)"
                         % (str(one.get("photoCode") or ""),
                            str(one.get("relPath") or ""),
                            str(one.get("missReason") or "")))
    lines += ["", "由 photo-browser 生成；原图目录未被修改（只读）。"]
    return "\r\n".join(lines) + "\r\n"


def exportsUsage() -> dict:
    """`exports\\` 目录当前占用（字节数 + 文件数）。

    ⚠️ **只报数，不清理**：DR-24「不自动删用户文件」。zip 是原图的**完整副本**
       （一档几百 MB ~ 1 GB），删不删由用户决定 —— 但必须让他知道
       「打包出去的副本正在占磁盘」，否则磁盘满了才来问「是谁占的」。
    ⚠️ 不返回目录路径：本机绝对路径不进出响应体（与 eraSample 的下载接口
       同一条纪律），它不该出现在 /docs 的示例或前端缓存里。
    """
    root = paths.exports_dir()
    total = 0
    files = 0
    if os.path.isdir(root):
        for dirPath, _dirNames, fileNames in os.walk(root):
            for name in fileNames:
                try:
                    total += os.path.getsize(os.path.join(dirPath, name))
                    files += 1
                except OSError:
                    continue
    return {"bytes": total, "files": files}
