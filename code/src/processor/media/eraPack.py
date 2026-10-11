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
import os
import re
import sys
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
