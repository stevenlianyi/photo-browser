#! /usr/bin/env python3
#encoding: utf-8

#Filename: backfill_shotyear_from_dir.py
#Description: photo-browser 年份目录回填（存量修正）—— 无 EXIF 拍摄时间的照片,
#             按「所在年份目录名」回填 pb_photo.shotYearOverride
#
# 要解决的现象
# ------------
#   老相册的组织习惯是「一年一个目录」（photo\1993\ 、photo\2024\202405\ ...）。
#   而 shotYear 的兜底链是 EXIF -> 文件名 -> mtime：
#     · 扫描件 / 翻拍件**没有 EXIF**；
#     · 文件名大多是 `lc-wedding-photo-1.jpg` / `pic147.jpg` 这种没有日期的；
#     · 于是兜底落到 mtime —— 一个**纯粹由复制/下载时间决定**的垃圾值。
#   实测正式库：无 EXIF 且在年份目录内的 3572 张里，1736 张当前年份 ≠ 目录年份
#   （例：`1993/lc-wedding-photo-1.jpg` 被 mtime 推成 2003），真正"文件名能推出年份"
#   的只有 15 张。而**目录名本身就是人工整理过的事实**，比 mtime 可信得多。
#
# 为什么写 shotYearOverride 而不是 shotYear（DR-42）
# --------------------------------------------------
#   `pb_photo.shotYear` 是**机器读到的原值**，它的存在意义就是留作审计证据
#   （也是「恢复自动」要回落的那个值）；人工修正一律写 `shotYearOverride`
#   （分桶优先、可撤销、留 pb_review_log）。本工具就是**批量版**的
#   `processor.photoTimeFix`，所以：
#     · 只改 shotYearOverride 一列，走 updateTableGeneral（None=NULL 的纪律同 photoTimeFix）；
#     · 写入后按 DR-22 硬顺序联动：rebucket.rebucketPhoto -> centroid.recomputePerson；
#     · 每张照片登记一条 opType=BUCKET_FIX 的日志（可走 /api/review/undo 撤销）。
#   ⇒ 复用同一套心智模型，用户不必学第二套。
#
# 判定口径（三条，写死在代码里而不是散在各处）
# ------------------------------------------
#   ① 「所属年份目录」= relPath 中**最靠近根**的那一段「恰好 4 位年份」且落在
#      [--min-year, --max-year] 内的目录段（`2013美国游` 这种不算，不是纯年份）。
#      「递归处理所有子目录」= 年份目录**之下**再深的子目录（如 2024/202405/）
#      都属于这个年份目录。
#   ② 「无 EXIF 拍摄时间」= 文件头里 DateTimeOriginal / DateTimeDigitized /
#      IFD0 DateTime 三者都取不到（与 meta.parseExifObject 同口径）。
#      ⚠️ 读不到文件（缺失 / RAW 无解码器 / HEIC 缺 pillow-heif）**不算无 EXIF**，
#         而是跳过并记 `unreadable` —— 宁可不动，也不给一张读不出来的图瞎填年份。
#   ③ 目录年**优先于**文件名推断年（用户选定口径）：只要没 EXIF 就用目录年。
#      实测会改变 1736 张，其中文件名能推出年份的仅 15 张，覆盖风险很小。
#
# 默认只读（先看再改）
# ------------------
#   缺省 **dry-run**：只列清单、不写一行（与 rebucket_cli.py / fix_confirmed_flag.py
#   同一口径）。确认无误后加 `--yes` 实跑。改生产数据必须显式给。
#
# 用法
# ----
#   # 预览：默认根 d:\PhotoLib\photo，年份 1993..2026（只读）
#   python code\src\tools\backfill_shotyear_from_dir.py
#
#   # 实跑（写 override + 重刷年代档 + 重算质心 + 写日志）
#   python code\src\tools\backfill_shotyear_from_dir.py --yes
#
#   # 自定义根目录 / 年份范围 / 库 / 报告落点
#   python code\src\tools\backfill_shotyear_from_dir.py --root d:\tmp\photos ^
#          --min-year 1990 --max-year 2030 --db d:\tmp\test.db --report d:\tmp\r.txt --yes
#
#   # 只处理前 50 张候选（试跑）；
#   # 覆盖「人工已修正过」的那些（默认跳过，尊重你的手工结论）
#   python code\src\tools\backfill_shotyear_from_dir.py --limit 50 --force-override
#
# 硬约束：**只改数据库**；photo\ 目录一律只读（本工具只 open(...,"rb") 读文件头，
#         连 `paths.ensure_dirs()` 都不调，绝不创建/写入/改名任何原图）。

import argparse
import os
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../src/tools
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                                # noqa: E402
from common import paths as paths                                    # noqa: E402
from config import basicSettings as basicSettings                    # noqa: E402
from database.auto_generated import sqliteCommon                     # noqa: E402
from engine.match import bucket as bucket                            # noqa: E402
from engine.match import centroid as centroid                        # noqa: E402
from engine.match import rebucket as rebucket                        # noqa: E402
from processor.review import assigner                                # noqa: E402
from processor.scanner import meta                                   # noqa: E402

_VERSION = "20261011"

_LOG = misc.setLogNew("backfillShotYear", "backfillshotyear.log")

#: 报告落盘目录（<code>\log，与 miscCommon 的日志目录同一个地方）
_CODE_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))

#: 默认年份范围（用户需求：1993 至 2026 的年份子目录）
DEFAULT_MIN_YEAR: int = 1993
DEFAULT_MAX_YEAR: int = 2026

#: 读库分页行数（与全场同口径：一次性全取 3.5 万行的 dict 列表是纯浪费）
PAGE_ROWS: int = basicSettings.SCAN_INDEX_PAGE

#: 年份目录段判据：**整段**恰好 4 位数字（前后不许有别的东西）。
#: 用字符串判断而不是正则：`seg.isdigit() and len(seg)==4` 就是「纯年份」的
#: 唯一含义，`2013美国游` / `202405` / `1993-1994` 一律不算。
_YEAR_SEG_LEN: int = 4

#: 进度打印间隔（每处理多少张照片打一行）
_PROGRESS_EVERY: int = 2000

#: 动作类型
ACT_CHANGE = "change"
ACT_SKIP = "skip"

#: 跳过原因（写进报告，便于分类核对）
R_HAS_EXIF = "hasExif"                 # 有 EXIF 拍摄时间 —— 按需求不动
R_ALREADY = "alreadyCorrect"           # 当前有效年份已等于目录年 —— 无需改
R_MANUAL = "manualOverride"            # 你手工修正过且与目录年不同 —— 默认不动
R_MISSING = "missingFile"              # 库里有、磁盘上找不到 —— 不动
R_UNREADABLE = "unreadable"            # 读不出文件头（RAW 无解码器 / HEIC 缺 pillow-heif）—— 不动
R_OUT_OF_RANGE = "outOfRange"          # 年份目录不在 --min-year..--max-year 内 —— 不动


# ============================================================
# 零、内部工具
# ============================================================

def _fixConsole() -> None:
    """Windows 控制台默认 GBK，打印中文/符号会 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _toSlash(text) -> str:
    """统一正斜杠（relPath 原值就是正斜杠，这里只是防御）"""
    return ("" if text is None else str(text)).replace("\\", "/")


def _intOrNone(value):
    """各种形态的年份 -> int / None（不抛，脏值一律当"没有"）"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def yearDirOf(relPath: str, minYear: int, maxYear: int):
    """relPath -> (所属年份目录名 int, 年份目录所在的第几段) / (None, None)。

    「所属年份目录」= 从根往下**第一个**恰好 4 位年份、且落在 [minYear, maxYear]
    内的目录段。取**最靠近根**的那个是有意的：`1993/2005/x.jpg` 属于 1993，
    年份目录之下再深的子目录（2024/202405/）都归它管 —— 这正是"递归处理
    所有子目录"的含义。

    纯函数，无 IO，可单测。
    """
    text = _toSlash(relPath).strip("/")
    if not text:
        return None, None
    parts = text.split("/")
    # 最后一段是文件名，不参与
    for index, seg in enumerate(parts[:-1]):
        seg = seg.strip()
        if len(seg) != _YEAR_SEG_LEN or not seg.isdigit():
            continue
        year = int(seg)
        if minYear <= year <= maxYear:
            return year, index
        # 恰好 4 位年份但越界：不认（继续往后找也没有意义，直接判"不在范围内"）
        return None, index
    return None, None


def _absOf(photoRoot: str, relPath: str) -> str:
    """relPath -> 磁盘绝对路径（relPath 是正斜杠，交给 os.path.join 前先归一）"""
    return os.path.normpath(os.path.join(photoRoot, _toSlash(relPath)))


#: pillow-heif 只在**首次遇到 HEIC/HEIF** 时尝试注册一次（没装就永远跳过该类文件）
_HEIF_READY = None


def _ensureHeif() -> bool:
    """尝试注册 pillow-heif 解码器。返回是否可用（导入失败不抛，只记一次 warning）。"""
    global _HEIF_READY
    if _HEIF_READY is not None:
        return _HEIF_READY
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _HEIF_READY = True
        _LOG.info("pillow-heif 已注册，HEIC/HEIF 的 EXIF 可读")
    except Exception as e:
        _HEIF_READY = False
        _LOG.warning("pillow-heif 不可用（%s: %s）；HEIC/HEIF 一律按"
                     "「读不出 EXIF」跳过，不猜年份（属降级不属错误）"
                     % (type(e).__name__, e))
    return _HEIF_READY


def readExifTime(absPath: str) -> dict:
    """读一张图的 EXIF 拍摄时间。**只读文件头，不解像素，绝不写文件**。

    返回
    ----
      {"ok": True,  "takenAt": "2023-05-01T04:00:00Z"|None, "raw": "2023:05:01 12:00:00"|None}
      {"ok": False, "reason": "missingFile"|"noDecoder: ..."|"..."}

    ok=False 表示**没能判断**（文件缺失 / 打不开 / 缺解码器）—— 调用方必须跳过，
    绝不可以把「读不出来」当成「没有 EXIF」而给一张图瞎填年份。
    """
    if not os.path.isfile(absPath):
        return {"ok": False, "reason": R_MISSING}
    try:
        from PIL import Image
    except ImportError as e:
        return {"ok": False, "reason": "noPillow: %s" % e}
    try:
        _ensureHeif()
        with Image.open(absPath) as image:
            exif = image.getexif()
            info = meta.parseExifObject(exif)
            return {"ok": True, "takenAt": info.get("takenAt"),
                    "raw": _rawExifTime(exif)}
    except Exception as e:
        # RAW 无解码器 / HEIC 缺 pillow-heif / 文件被截断 —— 全走这里
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}


def _rawExifTime(exif):
    """取原始拍摄时间字符串（仅用于报告展示，取不到返回 None）。"""
    if exif is None:
        return None
    try:
        exifIfd = meta._subIfd(exif, meta.IFD_EXIF)            # noqa: SLF001 - 复用既有解析
        for tag in (meta.TAG_DATETIME_ORIGINAL, meta.TAG_DATETIME_DIGITIZED):
            value = exifIfd.get(tag)
            if value:
                return _decodeExifText(value)
        return _decodeExifText(exif.get(meta.TAG_DATETIME))
    except Exception:
        return None


def _decodeExifText(value):
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("ascii", "ignore").strip() or None
    return str(value).strip() or None


# ============================================================
# 一、盘点（只读：查库 + 读文件头，一行都不写）
# ============================================================

def _iterPhotos(pageRows: int = PAGE_ROWS):
    """分页遍历 pb_photo（**只取未软删**行；mode=light 不带 BLOB 列）。"""
    offset = 0
    while True:
        rows = sqliteCommon.query_pb_photo(
            "pb_photo", mode="light", orderBy="recID",
            limitNum=pageRows, offsetNum=offset)
        if not rows:
            break
        offset += len(rows)
        for one in rows:
            yield one
        if len(rows) < pageRows:
            break


def collect(photoRoot: str, minYear: int, maxYear: int, limit: int = 0,
            forceOverride: bool = False, rereadExif: bool = False,
            progress=None) -> dict:
    """盘点全部「在年份目录内」的照片，逐张判定该不该改。**纯读**。

    参数
    ----
      photoRoot     : 原图根（只用于拼绝对路径读文件头）
      minYear/maxYear: 年份目录认哪些年份
      limit         : >0 时最多盘点这么多张"在年份目录内"的照片（试跑用）
      forceOverride : True = 连「人工已修正过」的也覆盖（默认跳过，尊重手工结论）
      rereadExif    : True = 每张都重读磁盘文件头；False（默认）= 库中 takenAt 非空
                      即认定"有 EXIF"（takenAt 只可能来自 EXIF，见 meta.resolveShotYear），
                      省掉 3 万次无谓的文件打开

    返回
    ----
      {"decisions": [...], "counters": {...}, "diskOnly": [...]}
    """
    decisions = []
    counters = {"examined": 0, ACT_CHANGE: 0, ACT_SKIP: 0,
                R_HAS_EXIF: 0, R_ALREADY: 0, R_MANUAL: 0,
                R_MISSING: 0, R_UNREADABLE: 0, R_OUT_OF_RANGE: 0}
    scanned = 0
    for row in _iterPhotos():
        relPath = str(row.get("relPath") or "")
        dirYear, _index = yearDirOf(relPath, minYear, maxYear)
        if dirYear is None:
            # 不在年份目录内 / 年份越界 —— 都不属于本工具的处理范围
            if _index is not None:
                counters[R_OUT_OF_RANGE] += 1
            continue
        scanned += 1
        if limit and scanned > limit:
            break
        counters["examined"] += 1

        item = _judge(row, photoRoot, dirYear, forceOverride, rereadExif)
        decisions.append(item)
        counters[item["action"]] += 1
        if item["action"] == ACT_SKIP:
            counters[item["reason"]] = counters.get(item["reason"], 0) + 1
        if progress and counters["examined"] % _PROGRESS_EVERY == 0:
            progress(counters["examined"], counters)
    return {"decisions": decisions, "counters": counters, "diskOnly": []}


def _judge(row: dict, photoRoot: str, dirYear: int,
           forceOverride: bool, rereadExif: bool) -> dict:
    """判定单张照片：改还是跳过（**纯读**）。返回统一的 decision dict。"""
    photoCode = str(row.get("photoCode") or "")
    relPath = str(row.get("relPath") or "")
    shotYear = _intOrNone(row.get("shotYear"))
    override = _intOrNone(row.get("shotYearOverride"))
    effective = override if override is not None else shotYear
    item = {"photoCode": photoCode, "relPath": relPath, "dirYear": dirYear,
            "shotYear": shotYear, "currentOverride": override,
            "effectiveYear": effective, "takenAt": None, "exifRaw": None,
            "action": ACT_SKIP, "reason": "", "note": "",
            "newYear": None, "oldYear": override}

    # ---- ① 有 EXIF 拍摄时间？按需求不动 ----
    takenAt = str(row.get("takenAt") or "")
    if takenAt and not rereadExif:
        item["takenAt"] = takenAt
        item["reason"] = R_HAS_EXIF
        item["note"] = "库中 takenAt=%s" % takenAt
        return item
    probe = readExifTime(_absOf(photoRoot, relPath))
    if not probe.get("ok"):
        item["reason"] = R_UNREADABLE if probe.get("reason") != R_MISSING else R_MISSING
        item["note"] = str(probe.get("reason") or "读取失败")
        return item
    if probe.get("takenAt"):
        item["takenAt"] = probe["takenAt"]
        item["exifRaw"] = probe.get("raw")
        item["reason"] = R_HAS_EXIF
        item["note"] = "EXIF=%s" % (probe.get("raw") or probe["takenAt"])
        return item

    # ---- ② 无 EXIF：目录年优先（用户口径）----
    if override is not None and override != dirYear and not forceOverride:
        item["reason"] = R_MANUAL
        item["note"] = "人工已修正为 %s，与目录年 %s 不同（--force-override 可覆盖）" \
                       % (override, dirYear)
        return item
    if effective is not None and int(effective) == dirYear and override is None:
        item["reason"] = R_ALREADY
        item["note"] = "当前年份（来自文件名）= 目录年 %s，无需改动" % dirYear
        return item
    if override is not None and override == dirYear:
        item["reason"] = R_ALREADY
        item["note"] = "已修正为目录年 %s" % dirYear
        return item

    item["action"] = ACT_CHANGE
    item["newYear"] = int(dirYear)
    item["reason"] = "noExif"
    item["note"] = "无 EXIF；原年份 %s -> 目录年 %s" % (
        "(空)" if effective is None else effective, dirYear)
    return item


# ============================================================
# 二、落库 + 三步联动（只在 --yes 时跑）
# ============================================================

def _writeOverride(photoCode: str, value) -> int:
    """写 pb_photo.shotYearOverride（与 photoTimeFix._writeOverride 同一纪律）。

    ⚠️ 必须走 updateTableGeneral：值**原样**进参数元组，None 就是 SQL NULL。
       走 upsert 会把 None 整列丢掉（恢复自动 = 静默空操作）。
    """
    rtn = sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (str(photoCode),),
        {"shotYearOverride": value, "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise RuntimeError("pb_photo.shotYearOverride 写入失败: %s"
                           % sqliteCommon.dbHandle().lastErrMsg)
    return int(rtn or 0)


def _facesOf(photoCode: str) -> list:
    """这张照片的未软删人脸（口径与 rebucket.rebucketPhoto 一致）"""
    return sqliteCommon.query_pb_face("pb_face", photoCode=str(photoCode),
                                      mode="light", orderBy="recID")


def _encodeYear(value) -> str:
    return "" if value is None else str(int(value))


def applyBackfill(changes: list, recompute: bool = True, progress=None) -> dict:
    """把变更清单落库并联动。**顺序严格按 DR-22**：

      ① 写全部 shotYearOverride
      ② rebucket.rebucketPhoto 重刷这些照片**全部**人脸的 shotBucket
      ③ centroid.recomputePerson 重算涉及人物的质心（每人**一次**）
      ④ 写 pb_review_log（opType=BUCKET_FIX，可撤销）

    ⚠️ 为什么不是「逐张 写->刷->算」：
       质心重算前有 assertFacesFresh（DR-22 前置检查），它要求**这个人所有脸**
       的桶键都已是当前口径。若逐张来，处理照片 A 时同一人的照片 B 还是旧口径，
       重算会**抛错**并被记成 warning 噪音。所以先①写全、再②刷全、最后③算一次。
    """
    out = {"written": 0, "writeFailed": [], "rebucketed": 0, "bucketsChanged": 0,
           "persons": 0, "centroids": 0, "warnings": [], "logs": 0,
           "noFaces": 0, "elapsed": 0.0}
    if not changes:
        return out
    start = time.perf_counter()

    # ---- 先记下每张照片涉及的人（人脸归属本工具无权改，写库前后都一样）----
    faceCache = {}
    personSet = set()
    for index, one in enumerate(changes):
        code = one["photoCode"]
        faces = _facesOf(code)
        faceCache[code] = faces
        for face in faces:
            pcode = str(face.get("personCode") or "")
            if pcode:
                personSet.add(pcode)
        if progress and (index + 1) % _PROGRESS_EVERY == 0:
            progress(index + 1, len(changes))

    # ---- ① 写 override ----
    for one in changes:
        rtn = _writeOverride(one["photoCode"], one["newYear"])
        if rtn <= 0:
            out["writeFailed"].append({"photoCode": one["photoCode"],
                                       "relPath": one["relPath"], "rtn": rtn})
        else:
            out["written"] += 1

    # ---- ② 重刷这些照片的全部人脸年代档 ----
    for one in changes:
        try:
            stat = rebucket.rebucketPhoto(one["photoCode"])
        except Exception as e:                       # 单张失败不该中断整批
            out["warnings"].append({"photoCode": one["photoCode"],
                                    "error": "%s: %s" % (type(e).__name__, e)})
            continue
        out["rebucketed"] += int(stat.get("faces") or 0)
        out["bucketsChanged"] += int(stat.get("changed") or 0)
        if int(stat.get("faces") or 0) == 0:
            out["noFaces"] += 1

    # ---- ③ 重算涉及人物的质心（每人一次）----
    if recompute:
        for pcode in sorted(personSet):
            try:
                stat = centroid.recomputePerson(pcode)
            except Exception as e:                   # BucketStaleError 及同类
                out["warnings"].append({
                    "personCode": pcode,
                    "error": "%s: %s" % (type(e).__name__, e),
                    "advice": "这个人的**其它**照片还残留旧口径的年代档键，"
                              "请跑 python code\\src\\tools\\rebucket_cli.py "
                              "--person %s --recompute" % pcode})
                continue
            out["persons"] += 1
            out["centroids"] += len(stat.get("buckets") or [])

    # ---- ④ 留痕（每张照片一条主日志 + 每个受影响人一条成员日志）----
    for one in changes:
        code = one["photoCode"]
        relPath = one["relPath"]
        pcodes = sorted({str(f.get("personCode") or "") for f in faceCache.get(code) or []
                         if str(f.get("personCode") or "")})
        logCode = assigner.newLogCode()
        detail = ";".join(["op=%s" % logCode, "photo=%s" % code,
                           "old=%s" % _encodeYear(one.get("currentOverride")),
                           "new=%s" % _encodeYear(one.get("newYear")),
                           "persons=%s" % "|".join(pcodes),
                           "relPath=%s" % relPath[:80],
                           "src=dirYear"])
        assigner.logReview(assigner.OP_BUCKET_FIX, faceCode="", photoCode=code,
                           fromPersonCode="", toPersonCode="",
                           faceCount=len(faceCache.get(code) or []),
                           detail=detail, isRevertible=1, logCode=logCode)
        out["logs"] += 1
        for pcode in pcodes:
            assigner.logReview(
                assigner.OP_BUCKET_FIX, faceCode="", photoCode=code,
                fromPersonCode="", toPersonCode=pcode, faceCount=1,
                detail="op=%s;old=%s;new=%s" % (logCode,
                                                _encodeYear(one.get("currentOverride")),
                                                _encodeYear(one.get("newYear"))),
                isRevertible=1, logCode="%s.%s" % (logCode, pcode))
            out["logs"] += 1

    out["elapsed"] = round(time.perf_counter() - start, 3)
    _LOG.info("年份目录回填：写 %d 张 override（失败 %d），重刷人脸 %d 张/变更 %d，"
              "重算 %d 人（%d 桶），日志 %d 条，耗时 %.2fs",
              out["written"], len(out["writeFailed"]), out["rebucketed"],
              out["bucketsChanged"], out["persons"], out["centroids"],
              out["logs"], out["elapsed"])
    return out


# ============================================================
# 三、磁盘核对（可选：报告「磁盘有、库里无」，不写库）
# ============================================================

def walkDiskYears(photoRoot: str, minYear: int, maxYear: int) -> dict:
    """遍历磁盘，返回 {规范化 relPath: 年份} —— 仅用于核对，不写任何东西。"""
    found = {}
    exts = basicSettings.PHOTO_EXT_SET
    excluded = basicSettings.EXCLUDED_DIR_NAMES
    for dirPath, dirNames, fileNames in os.walk(photoRoot):
        dirNames[:] = [d for d in dirNames if d not in excluded]
        for name in fileNames:
            dot = name.rfind(".")
            if dot < 0 or name[dot:].lower() not in exts:
                continue
            absPath = os.path.join(dirPath, name)
            relPath = os.path.relpath(absPath, photoRoot)
            year, _idx = yearDirOf(relPath, minYear, maxYear)
            if year is None:
                continue
            try:
                key = paths.normalize_relpath(relPath)
            except ValueError:
                continue
            found[key] = year
    return found


# ============================================================
# 四、报告
# ============================================================

def writeReport(reportPath: str, summary: dict, decisions: list,
                applied: dict = None, diskOnly: list = None) -> str:
    """把处理日志（含**已修改**与**已跳过**明细）写成文本报告。返回落点路径。"""
    lines = []
    add = lines.append
    add("=" * 80)
    add("photo-browser 年份目录回填报告")
    add("=" * 80)
    add("版本      : %s" % _VERSION)
    add("时间      : %s" % misc.getTime())
    add("库        : %s" % summary.get("dbFile"))
    add("原图根    : %s" % summary.get("photoRoot"))
    add("年份范围  : %d..%d" % (summary.get("minYear"), summary.get("maxYear")))
    add("模式      : %s" % ("APPLY（已写库）" if summary.get("apply") else "DRY-RUN（只读）"))
    add("")
    counters = summary.get("counters") or {}
    add("---- 汇总 ----")
    add("在年份目录内的照片 : %d" % counters.get("examined", 0))
    add("  待修改/已修改    : %d" % counters.get(ACT_CHANGE, 0))
    add("  跳过-已有EXIF    : %d" % counters.get(R_HAS_EXIF, 0))
    add("  跳过-年份已一致  : %d" % counters.get(R_ALREADY, 0))
    add("  跳过-人工已修正  : %d" % counters.get(R_MANUAL, 0))
    add("  跳过-文件缺失    : %d" % counters.get(R_MISSING, 0))
    add("  跳过-无法读取    : %d" % counters.get(R_UNREADABLE, 0))
    if counters.get(R_OUT_OF_RANGE):
        add("  跳过-年份越界    : %d（目录是 4 位年份但不在范围内）"
            % counters.get(R_OUT_OF_RANGE, 0))
    if applied:
        add("")
        add("---- 落库联动 ----")
        add("  写 override      : %d 张（失败 %d）"
            % (applied.get("written", 0), len(applied.get("writeFailed") or [])))
        add("  重刷人脸         : %d 张（其中改桶 %d 张）"
            % (applied.get("rebucketed", 0), applied.get("bucketsChanged", 0)))
        add("  重算质心         : %d 人 / %d 个桶（无脸照片 %d 张）"
            % (applied.get("persons", 0), applied.get("centroids", 0),
               applied.get("noFaces", 0)))
        add("  日志(BUCKET_FIX) : %d 条（可经 /api/review/undo 撤销）"
            % applied.get("logs", 0))
        add("  耗时             : %.2fs" % applied.get("elapsed", 0.0))
        for warn in applied.get("warnings") or []:
            add("  ⚠ %s" % warn)
    if diskOnly:
        add("")
        add("---- 磁盘有、库里无（需先扫描才入库）: %d 个 ----" % len(diskOnly))
        for one in diskOnly:
            add("  %s" % one)

    changed = [d for d in decisions if d["action"] == ACT_CHANGE]
    add("")
    add("---- %s明细（%d 张）----" % ("已修改" if summary.get("apply") else "待修改",
                                      len(changed)))
    add("%-34s\t%-6s\t%-8s\t%-8s\t%-10s\t%s"
        % ("photoCode", "目录年", "原override", "原shotYear", "现有效年", "relPath"))
    for one in changed:
        add("%-34s\t%-6s\t%-8s\t%-8s\t%-10s\t%s"
            % (one["photoCode"], one["dirYear"],
               _encodeYear(one.get("currentOverride")) or "(空)",
               _encodeYear(one.get("shotYear")) or "(空)",
               _encodeYear(one.get("effectiveYear")) or "(空)",
               one["relPath"]))

    skipped = [d for d in decisions if d["action"] == ACT_SKIP]
    add("")
    add("---- 跳过明细（%d 张）----" % len(skipped))
    add("%-16s\t%-34s\t%s" % ("原因", "photoCode", "relPath"))
    for one in skipped:
        add("%-16s\t%-34s\t%s" % (one.get("reason"), one["photoCode"], one["relPath"]))

    text = "\n".join(lines) + "\n"
    if reportPath:
        os.makedirs(os.path.dirname(os.path.abspath(reportPath)), exist_ok=True)
        with open(reportPath, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
    return reportPath


def _printSummary(summary: dict, applied: dict = None) -> None:
    counters = summary.get("counters") or {}
    print("")
    print("---- 汇总 ----")
    print("  库            : %s" % summary.get("dbFile"))
    print("  原图根        : %s" % summary.get("photoRoot"))
    print("  年份范围      : %d..%d" % (summary.get("minYear"), summary.get("maxYear")))
    print("  在年份目录内  : %d 张" % counters.get("examined", 0))
    print("  待修改        : %d 张" % counters.get(ACT_CHANGE, 0))
    print("  跳过-有EXIF   : %d 张" % counters.get(R_HAS_EXIF, 0))
    print("  跳过-已一致   : %d 张" % counters.get(R_ALREADY, 0))
    print("  跳过-人工修正 : %d 张" % counters.get(R_MANUAL, 0))
    print("  跳过-缺失文件 : %d 张" % counters.get(R_MISSING, 0))
    print("  跳过-无法读取 : %d 张" % counters.get(R_UNREADABLE, 0))
    if counters.get(R_OUT_OF_RANGE):
        print("  跳过-年份越界 : %d 张" % counters.get(R_OUT_OF_RANGE, 0))
    if applied:
        print("")
        print("---- 落库联动 ----")
        print("  写 override   : %d 张（失败 %d）"
              % (applied.get("written", 0), len(applied.get("writeFailed") or [])))
        print("  重刷人脸      : %d 张（改桶 %d）"
              % (applied.get("rebucketed", 0), applied.get("bucketsChanged", 0)))
        print("  重算质心      : %d 人 / %d 个桶"
              % (applied.get("persons", 0), applied.get("centroids", 0)))
        print("  日志          : %d 条（BUCKET_FIX，可撤销）" % applied.get("logs", 0))
        print("  耗时          : %.2fs" % applied.get("elapsed", 0.0))
        for warn in applied.get("warnings") or []:
            print("  ⚠ %s" % warn)


# ============================================================
# 五、命令行
# ============================================================

def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_shotyear_from_dir.py",
        description="按年份目录回填无 EXIF 照片的拍摄年（写 pb_photo.shotYearOverride）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--root", default=None,
                        help="原图根目录（缺省 paths.photo_dir()，**只读**）")
    parser.add_argument("--db", default=None, help="库文件（缺省 paths.db_file()）")
    parser.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR,
                        help="年份目录下界（缺省 %d）" % DEFAULT_MIN_YEAR)
    parser.add_argument("--max-year", type=int, default=DEFAULT_MAX_YEAR,
                        help="年份目录上界（缺省 %d）" % DEFAULT_MAX_YEAR)
    parser.add_argument("--limit", type=int, default=0,
                        help="最多盘点这么多张（试跑用，0=不限）")
    parser.add_argument("--force-override", action="store_true",
                        help="连「人工已修正过」的也覆盖（默认跳过，尊重手工结论）")
    parser.add_argument("--reread-exif", action="store_true",
                        help="每张都重读磁盘文件头（默认：库中 takenAt 非空即认定有 EXIF）")
    parser.add_argument("--scan-disk", action="store_true",
                        help="额外遍历磁盘，报告「磁盘有、库里无」的文件（不写库）")
    parser.add_argument("--no-recompute", action="store_true",
                        help="只写 override+刷桶，不重算质心（排障用；正常不要加）")
    parser.add_argument("--report", default=None,
                        help="报告落点（缺省 <code>\\log\\backfill_shotyear_<时间>.txt）")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报不改（**默认就是只读**，加 --yes 才实跑）")
    parser.add_argument("--yes", action="store_true", help="确认实跑（改生产数据必须显式给）")
    parser.add_argument("--quiet", action="store_true", help="少打印")
    return parser


def main(argv=None) -> int:
    _fixConsole()
    args = buildParser().parse_args(argv if argv is not None else sys.argv[1:])
    apply = bool(args.yes) and not bool(args.dry_run)

    # ---- 年份范围校验（复用 bucket 的可信区间，绝不猜）----
    minYear = bucket.validShotYear(args.min_year)
    maxYear = bucket.validShotYear(args.max_year)
    if minYear is None or maxYear is None or minYear > maxYear:
        print("[Error] 年份范围非法：%s..%s（需落在 %d..%d 且下界<=上界）"
              % (args.min_year, args.max_year,
                 basicSettings.SHOT_YEAR_MIN, basicSettings.SHOT_YEAR_MAX),
              file=sys.stderr)
        return 2

    # ---- 路径 / 库 ----
    try:
        photoRoot = os.path.abspath(args.root or paths.photo_dir())
    except paths.PathLayoutError as e:
        print("[PathLayoutError] %s" % e, file=sys.stderr)
        return 1
    dbFile = os.path.abspath(args.db or paths.db_file())
    if args.root:
        paths.setRootOverride(photo=photoRoot)          # 让 paths.* 也跟随
    if not os.path.isdir(photoRoot):
        print("[Error] 原图根不存在或不是目录: %s" % photoRoot, file=sys.stderr)
        return 1
    if not os.path.isfile(dbFile):
        print("[Error] 库文件不存在: %s" % dbFile, file=sys.stderr)
        return 1
    sqliteCommon.dbHandle(dbFile)
    if not sqliteCommon.chkTableExist("pb_photo"):
        print("[Error] 库里没有 pb_photo 表（先跑 tools\\build_db.py）", file=sys.stderr)
        return 1

    print("backfill_shotyear_from_dir.py _VERSION: %s" % _VERSION)
    print("库        : %s" % dbFile)
    print("原图根    : %s（只读，绝不写入）" % photoRoot)
    print("年份范围  : %d..%d" % (minYear, maxYear))
    print("模式      : %s" % ("APPLY（会写库）" if apply else "DRY-RUN（只读）"))
    if not apply:
        print("[dry-run] 未写任何一行。确认无误后加 --yes 实跑。")

    def _progress(done, _counters):
        if not args.quiet:
            print("   ...已盘点 %d 张" % done)

    start = time.perf_counter()
    result = collect(photoRoot, minYear, maxYear, limit=args.limit,
                     forceOverride=args.force_override, rereadExif=args.reread_exif,
                     progress=_progress)
    decisions = result["decisions"]
    elapsed = time.perf_counter() - start

    diskOnly = []
    if args.scan_disk:
        print("正在遍历磁盘核对（--scan-disk）...")
        disk = walkDiskYears(photoRoot, minYear, maxYear)
        known = set()
        for row in _iterPhotos():
            try:
                known.add(paths.normalize_relpath(str(row.get("relPath") or "")))
            except ValueError:
                continue
        diskOnly = sorted(set(disk.keys()) - known)

    changes = [d for d in decisions if d["action"] == ACT_CHANGE]
    applied = None
    if apply and changes:
        print("\n开始写库：%d 张 override -> 重刷年代档 -> 重算质心 -> 写日志 ..."
              % len(changes))
        applied = applyBackfill(changes, recompute=not args.no_recompute)
    elif apply:
        print("\n没有需要修改的照片，未写库。")

    summary = {"dbFile": dbFile, "photoRoot": photoRoot,
               "minYear": minYear, "maxYear": maxYear, "apply": apply,
               "counters": result["counters"], "elapsed": round(elapsed, 3)}
    _printSummary(summary, applied)
    print("盘点耗时  : %.2fs" % elapsed)

    # ---- 报告 ----
    if not args.no_report:
        reportPath = args.report or os.path.join(
            _CODE_DIR, "log", "backfill_shotyear_%s.txt" % misc.getTime())
        writeReport(reportPath, summary, decisions, applied=applied, diskOnly=diskOnly)
        print("报告      : %s" % reportPath)

    _LOG.info("backfill_shotyear: root=%s minYear=%d maxYear=%d apply=%s "
              "examined=%d change=%d elapsed=%.2fs", photoRoot, minYear, maxYear,
              apply, result["counters"].get("examined"), len(changes), elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
