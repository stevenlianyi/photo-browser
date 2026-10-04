#! /usr/bin/env python3
#encoding: utf-8

#Filename: meta.py
#Description: photo-browser 扫描器·元数据层 —— EXIF / 拍摄年份 / GPS 逆地理
#
# 职责（开发计划 §3.1 扫描链路第 4 步）
# --------------------------------------
#   1. readMeta()        读一张图的 EXIF：尺寸/方向/机型/拍摄时间/GPS，产出可直接入库的 dict
#   2. resolveShotYear() shotYear 识别优先级：**EXIF -> 文件名 -> mtime**（mtime 最不可靠）
#   3. 截图类文件名（Screenshot* / 截图*）-> shotYear = NULL，不参与跨年代桶比对
#   4. reverseGeocode()  GPS -> 离线逆地理地点名
#
# 降级纪律（本模块的第一原则：**任何一张破图都不能让整轮扫描失败**）
# ------------------------------------------------------------------
#   · Pillow 打不开（RAW 缺解码器 / 文件被截断）-> 记warning，该图照样入库，
#     只是 width/height/EXIF 全为空，shotYear 退到文件名 / mtime 兜底；
#   · reverse_geocoder 是**可选依赖**（requirements.txt 里已标注）：导入失败或
#     查不到数据 -> placeName 留空 + **只记一次** warning，绝不抛错中断；
#   · 读文件一律 open(...,"rb")，只读不写（photo 目录只读硬约束）。
#
# 只读文件头，不解像素
# -------------------
#   Image.open() 是懒加载的：只解析文件头，**不 decode 像素数据**。
#   所以EXIF + 尺寸的代价是几十 KB 级别的读，而不是整张图进内存。
#   绝不在这里调image.load() / thumbnail()（那是步骤 4 缩略图的活）。

import datetime
import mimetypes
import os
import re
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                                            # noqa: E402
from config import basicSettings as basicSettings                          # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("scannerMeta", "scannermeta.log")


# ============================================================
# 一、EXIF 标签号（TIFF/EXIF 标准，列出来是为了不背数字也写不错）
# ============================================================

TAG_ORIENTATION = 0x0112      # IFD0 Orientation 1..8
TAG_MAKE = 0x010F             # IFD0 Make 厂商
TAG_MODEL = 0x0110            # IFD0 Model 机型
TAG_DATETIME = 0x0132         # IFD0 DateTime（文件被改过时的修改时间，最不可信）
IFD_EXIF = 0x8769             # Exif SubIFD
TAG_DATETIME_ORIGINAL = 0x9003        # Exif DateTimeOriginal 真正的拍摄时间
TAG_DATETIME_DIGITIZED = 0x9004       # Exif DateTimeDigitized 数字化时间
IFD_GPS = 0x8825              # GPS SubIFD
TAG_GPS_LAT_REF = 0x0001
TAG_GPS_LAT = 0x0002
TAG_GPS_LON_REF = 0x0003
TAG_GPS_LON = 0x0004

# Pillow 的 format 名 -> MIME
_FORMAT_MIME = {
    "JPEG": "image/jpeg", "JPEG2000": "image/jp2", "PNG": "image/png",
    "WEBP": "image/webp", "BMP": "image/bmp", "GIF": "image/gif",
    "TIFF": "image/tiff", "MPO": "image/jpeg", "AVIF": "image/avif",
    "HEIF": "image/heif", "HEIC": "image/heic", "JPEG2000 ": "image/jp2",
    "DDS": "image/vnd-ms.dds", "FITS": "image/fits", "PCX": "image/x-pcx",
    "PPM": "image/x-portable-pixmap", "SGI": "image/sgi", "TGA": "image/x-tga",
    "XBM": "image/x-xbitmap", "WEBP ": "image/webp",
}

# shotYear 的来源标记（只用于日志/测试/报告，**pb_photo 没有这一列**）
SHOT_YEAR_SRC_EXIF = "EXIF"
SHOT_YEAR_SRC_NAME = "NAME"
SHOT_YEAR_SRC_MTIME = "MTIME"
SHOT_YEAR_SRC_SCREENSHOT = "SCREENSHOT"
SHOT_YEAR_SRC_NONE = "NONE"


# ============================================================
# 二、拍摄时间
# ============================================================

def exifTimeToUTC(text) -> str:
    """EXIF 的 "2023:05:01 12:00:00" -> UTC ISO8601 "2023-05-01T04:00:00Z"。

    EXIF 规范里DateTimeOriginal **没有时区字段**，只能按本地时间理解，
    再用 basicSettings.EXIF_LOCAL_UTC_OFFSET_HOURS 换算成 UTC 落库
    （takenAt 字段的口径是 UTC，统一了将来跨时区排序才不会错）。

    非法/占位值（"0000:00:00 00:00:00"、空、长度不对）一律返回 None，
    由调用方退到文件名 / mtime 兜底。
    """
    if not text:
        return None
    # EXIF 里的 ASCII 字段被 Pillow 读成 bytes（b'2023:05:01 12:00:00'），
    # 不先解码的话 str() 会带上 b'...' 前缀，后面的定长校验必挂
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("ascii", "ignore")
    s = str(text).strip()
    # 有些设备写成 "2023:05:01 12:00:00.123" 或带时区偏移 "2023:05:01 12:00:00+08:00"，
    # 统一只取前 19 个字符
    if len(s) >= 19:
        s = s[:19]
    if len(s) != 19 or s[4] != ":" or s[7] != ":" or s[10] != " ":
        return None
    try:
        naive = datetime.datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if naive.year <= 1:            # 0000:00:00 之类的占位
        return None
    offset = datetime.timedelta(hours=basicSettings.EXIF_LOCAL_UTC_OFFSET_HOURS)
    aware = naive.replace(tzinfo=datetime.timezone(offset))
    return aware.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def yearOfUTC(utcText) -> int:
    """从 "2023-05-01T04:00:00Z" 取年份；非法返回 None"""
    if not utcText or len(str(utcText)) < 4:
        return None
    try:
        return int(str(utcText)[0:4])
    except ValueError:
        return None


# ============================================================
# 三、文件名年份识别
# ============================================================

# 微信导出：mmexport1699999999999.jpg（13 位毫秒时间戳）
_RE_MMEXPORT = re.compile(r"mmexport_?(\d{13})", re.IGNORECASE)
# 20240501 / 2024-05-01 / 2024_05_01 / 2024.05.01（8 位或带分隔符的年月日）
_RE_DATE = re.compile(r"(19\d{2}|20\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])")
# 只到月的文件名：2024-05 / 202405
_RE_YM = re.compile(r"(19\d{2}|20\d{2})[-_. ]?(0[1-9]|1[0-2])(?![-_. ]?\d)")


def isScreenshotName(fileName: str) -> bool:
    """截图类文件名？命中则 shotYear = NULL（不参与跨年代桶比对）。

    只看 stem 的开头（"Screenshot_20240501_120000.png"、"截图 2024.png"都算），
    大小写不敏感。放中间的不算（"我的截图合集.jpg" 是相册名，不是截图）。
    """
    if not fileName:
        return False
    stem = os.path.splitext(str(fileName))[0].strip().lower()
    for prefix in basicSettings.SCREENSHOT_NAME_PREFIXES:
        if stem.startswith(str(prefix).lower()):
            return True
    return False


def _yearInRange(year) -> bool:
    try:
        num = int(year)
    except (TypeError, ValueError):
        return False
    return basicSettings.SHOT_YEAR_MIN <= num <= basicSettings.SHOT_YEAR_MAX


def shotYearFromName(fileName: str):
    """从文件名猜年份。识别不出来返回 (None, SHOT_YEAR_SRC_NONE)。

    支持
    ----
      · mmexport<13 位毫秒时间戳>（微信导出，S0 实测最常见的一类）
      · 纯数字 stem：13 位=毫秒时间戳 / 10 位=秒时间戳
      · 8 位年月日：IMG_20240501_120000.jpg / 2024-05-01.jpg / 2024.05.01.jpg
      · 只到月：2024-05.jpg

    每一类都要**校验合法性**（真实日期 + 年份在 SHOT_YEAR_MIN..MAX 之间），
    否则 "IMG_19999999.jpg" 这种会把年份算成 1999。
    """
    if not fileName:
        return None, SHOT_YEAR_SRC_NONE
    stem = os.path.splitext(str(fileName))[0].strip()

    # 1) 微信 mmexport 13 位毫秒时间戳
    hit = _RE_MMEXPORT.search(stem)
    if hit:
        year = yearOfMS(hit.group(1))
        if year:
            return year, SHOT_YEAR_SRC_NAME

    # 2) 纯数字 stem：13 位毫秒 / 10 位秒
    if stem.isdigit():
        if len(stem) == 13:
            year = yearOfMS(stem)
            if year:
                return year, SHOT_YEAR_SRC_NAME
        elif len(stem) == 10:
            year = yearOfSeconds(int(stem))
            if year:
                return year, SHOT_YEAR_SRC_NAME

    # 3) 年月日
    for match in _RE_DATE.finditer(stem):
        year = _yearOfDate(match.group(1), match.group(2), match.group(3))
        if year:
            return year, SHOT_YEAR_SRC_NAME

    # 4) 只到月
    for match in _RE_YM.finditer(stem):
        year = _yearOfDate(match.group(1), match.group(2), None)
        if year:
            return year, SHOT_YEAR_SRC_NAME

    return None, SHOT_YEAR_SRC_NONE


def yearOfMS(text) -> int:
    """13 位毫秒时间戳 -> 年份；非法返回 None"""
    try:
        sec = int(str(text)) / 1000.0
    except (TypeError, ValueError):
        return None
    return yearOfSeconds(sec)


def yearOfSeconds(sec) -> int:
    """秒级时间戳 -> 年份；非法返回 None"""
    try:
        value = float(sec)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        year = time.localtime(value).tm_year
    except (ValueError, OSError, OverflowError):
        return None
    return year if _yearInRange(year) else None


def _yearOfDate(yearText, monthText, dayText):
    """校验真实日期后取年份；非法返回 None"""
    try:
        year = int(yearText)
        month = int(monthText)
        day = int(dayText) if dayText else 1
        datetime.date(year, month, day)
    except (TypeError, ValueError):
        return None
    return year if _yearInRange(year) else None


def shotYearFromMTime(mtime) -> int:
    """mtime 兜底（**最不可靠**：copy 一遍文件 mtime 就变了）"""
    try:
        value = float(mtime)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        year = time.localtime(value).tm_year
    except (ValueError, OSError, OverflowError):
        return None
    return year if _yearInRange(year) else None


def resolveShotYear(exifTakenAt: str, fileName: str, mtime) -> dict:
    """定 shotYear：EXIF -> 文件名 -> mtime；截图类在没有 EXIF 时落 NULL。

    返回 {"shotYear": int|None, "source": "EXIF"/"NAME"/"MTIME"/"SCREENSHOT"/"NONE"}

    优先级说明
    ----------
      **EXIF 最高**（DateTimeOriginal 是相机写的真实拍摄时间）；
      截图名的短路只发生在「EXIF 也没有」的情况下 —— 见
      basicSettings.SCREENSHOT_OVERRIDES_EXIF（默认 False = EXIF 优先）。

    为什么截图名要短路文件名推断
    --------------------------
    截图的「年份」是保存时间而不是拍摄时间，跨年代桶比对（步骤 7）会把
    2015 年的老照片和 2024 年的截图放进同一个桶，白白污染质心。
    所以宁可落 NULL（= 不参与比对），也不能给它一个看起来像模像样的年份。
    """
    screenshot = isScreenshotName(fileName)
    if screenshot and basicSettings.SCREENSHOT_OVERRIDES_EXIF:
        return {"shotYear": None, "source": SHOT_YEAR_SRC_SCREENSHOT}

    year = yearOfUTC(exifTakenAt)
    if year and _yearInRange(year):
        return {"shotYear": year, "source": SHOT_YEAR_SRC_EXIF}

    if screenshot:
        return {"shotYear": None, "source": SHOT_YEAR_SRC_SCREENSHOT}

    year, _src = shotYearFromName(fileName)
    if year:
        return {"shotYear": year, "source": SHOT_YEAR_SRC_NAME}

    year = shotYearFromMTime(mtime)
    if year:
        return {"shotYear": year, "source": SHOT_YEAR_SRC_MTIME}

    return {"shotYear": None, "source": SHOT_YEAR_SRC_NONE}


# ============================================================
# 四、GPS 逆地理（可选依赖，缺失即降级）
# ============================================================

_RG_READY = None            # None=未尝试 / True=可用 / False=不可用
_RG_CACHE = {}              # (lat, lon) -> placeName
_RG_WARNED = False          # 「只记一次 warning」的闸门


def reverseGeocode(lat, lon) -> str:
    """GPS -> 地点名。查不到 / 依赖缺失一律返回 None，**绝不抛错**。

    reverse_geocoder 是可选依赖（数据离线打包，首次 import 约 1~2 秒）。
    导入失败、或坐标落在数据集之外（海里/无人区）都只留空placeName。
    """
    global _RG_READY, _RG_WARNED

    if lat is None or lon is None:
        return None
    try:
        latValue = float(lat)
        lonValue = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= latValue <= 90.0 and -180.0 <= lonValue <= 180.0):
        return None

    # 缓存到 4 位小数（约 11m），同一地点的多张照片只查一次
    key = (round(latValue, 4), round(lonValue, 4))
    if key in _RG_CACHE:
        return _RG_CACHE[key]

    if _RG_READY is None:
        try:
            import reverse_geocoder as reverse_geocoder      # noqa: F401
            _RG_READY = True
            _LOG.info("reverseGeocode: reverse_geocoder 可用，地点名正常填充")
        except Exception as e:
            _RG_READY = False
            _LOG.warning("reverseGeocode: reverse_geocoder 不可用（%s: %s）；"
                         "placeName 一律留空，属降级不属错误，扫描继续"
                         % (type(e).__name__, e))
    if not _RG_READY:
        _RG_CACHE[key] = None
        return None

    placeName = None
    try:
        import reverse_geocoder as rg
        # search() 只接受**一个坐标元组**（传两个标量会抛 TypeError）；
        # verbose=False 别往控制台喷「Loading formatted geocoded file...」。
        # ⚠️ 返回值形态随版本变：mode=1 有版本返 dict、有版本仍返 list，
        #    这里两种都兼容，省得升级依赖就静默丢地点名。
        hit = rg.search((latValue, lonValue), mode=1, verbose=False)
        if isinstance(hit, (list, tuple)):
            hit = hit[0] if hit else None
        if isinstance(hit, dict):
            placeName = _composePlaceName(hit)
    except Exception as e:
        # 依赖装了一半 / 数据文件损坏 —— 同样只记一次 warning，不中断扫描
        if not _RG_WARNED:
            _RG_WARNED = True
            _LOG.warning("reverseGeocode: 查询失败（%s: %s）；placeName 留空，扫描继续"
                         % (type(e).__name__, e))
    _RG_CACHE[key] = placeName
    return placeName


def _composePlaceName(hit: dict) -> str:
    """把 reverse_geocoder 的一条结果拼成 "中国, 北京市, 东城区" 这样的短串。

    reverse_geocoder 的 name/admin1/admin2/cc 大量重复（如 name==admin2），
    这里去重 + 截断到 256（placeName 字段长度）。
    """
    if not hit:
        return None
    parts = []
    for key in ("cc", "admin1", "admin2", "name"):
        value = hit.get(key)
        if not value:
            continue
        value = str(value).strip()
        if not value or value in parts:
            continue
        parts.append(value)
    if not parts:
        return None
    return ", ".join(parts)[:256]


# ============================================================
# 五、EXIF 解析（纯函数，可不落盘单测）
# ============================================================

def _rationalToFloat(value):
    """EXIF 有理数 -> float。IFDRational 直接 float()，三元组按 度/分/秒 展开。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, (tuple, list)):
        total = 0.0
        for index, item in enumerate(value):
            sub = _rationalToFloat(item)
            if sub is None:
                return None
            total += sub / (60.0 ** index)
        return total
    return None


def _subIfd(exif, ifdTag: int) -> dict:
    """取 Exif SubIFD / GPS SubIFD（拿不到就返回空 dict，绝不抛错）。"""
    try:
        return dict(exif.get_ifd(ifdTag))
    except Exception:
        return {}


def parseExifObject(exif) -> dict:
    """从 Pillow 的 Image.Exif 对象抽出入库需要的字段（**纯函数，不碰磁盘**）。

    返回 {"orientation","cameraModel","takenAt","lat","lon"}，取不到就是 None。
    单测直接new 一个 Image.Exif 塞标签即可，无需造图。
    """
    result = {"orientation": None, "cameraModel": None, "takenAt": None,
              "lat": None, "lon": None}
    if exif is None:
        return result

    exifIfd = _subIfd(exif, IFD_EXIF)
    gpsIfd = _subIfd(exif, IFD_GPS)

    # 方向：标准在 IFD0，少数设备塞 ExifIFD，两处都看
    orientation = exif.get(TAG_ORIENTATION)
    if orientation is None:
        orientation = exifIfd.get(TAG_ORIENTATION)
    try:
        orientation = int(orientation) if orientation is not None else None
    except (TypeError, ValueError):
        orientation = None
    result["orientation"] = orientation if orientation and 1 <= orientation <= 8 else None

    # 机型：Model 优先，只给了 Make 就退到 Make
    model = exif.get(TAG_MODEL) or exifIfd.get(TAG_MODEL)
    if not model:
        model = exif.get(TAG_MAKE) or exifIfd.get(TAG_MAKE)
    if model:
        try:
            modelText = model.decode("utf-8", "ignore") if isinstance(model, bytes) else str(model)
        except Exception:
            modelText = None
        modelText = (modelText or "").strip().strip("\x00").strip()
        result["cameraModel"] = modelText[:128] or None

    # 拍摄时间：DateTimeOriginal > DateTimeDigitized > IFD0 DateTime
    takenAt = None
    for tag in (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED):
        takenAt = exifTimeToUTC(exifIfd.get(tag))
        if takenAt:
            break
    if not takenAt:
        takenAt = exifTimeToUTC(exif.get(TAG_DATETIME))
    result["takenAt"] = takenAt

    # GPS
    lat = _rationalToFloat(gpsIfd.get(TAG_GPS_LAT))
    lon = _rationalToFloat(gpsIfd.get(TAG_GPS_LON))
    if lat is not None:
        refLat = gpsIfd.get(TAG_GPS_LAT_REF)
        refLat = refLat.decode("ascii", "ignore") if isinstance(refLat, bytes) else str(refLat or "N")
        if refLat.strip().upper().startswith("S"):
            lat = -lat
    if lon is not None:
        refLon = gpsIfd.get(TAG_GPS_LON_REF)
        refLon = refLon.decode("ascii", "ignore") if isinstance(refLon, bytes) else str(refLon or "E")
        if refLon.strip().upper().startswith("W"):
            lon = -lon
    if lat is not None:
        lat = round(float(lat), 7)
        if not (-90.0 <= lat <= 90.0):
            lat = None
    if lon is not None:
        lon = round(float(lon), 7)
        if not (-180.0 <= lon <= 180.0):
            lon = None
    result["lat"] = lat
    result["lon"] = lon
    return result


def mimeOfFormat(fmt, absPath: str) -> str:
    """Pillow 的 format 名（或退化到扩展名）-> MIME"""
    if fmt:
        mime = _FORMAT_MIME.get(str(fmt).upper().strip())
        if mime:
            return mime
    guess, _enc = mimetypes.guess_type(absPath)
    return guess


def emptyMeta(absPath: str = "") -> dict:
    """一张「什么都没读到」的图也必须能入库 —— 给出全 None 的骨架"""
    return {"mimeType": mimeOfFormat(None, absPath), "width": None, "height": None,
            "orientation": None, "cameraModel": None, "takenAt": None,
            "lat": None, "lon": None, "placeName": None,
            "shotYear": None, "shotYearSource": SHOT_YEAR_SRC_NONE}


def readMeta(absPath: str, fileSize: int = None, mtime: float = None) -> dict:
    """读一张图的元数据，产出**可直接喂给 pb_photo 的 dict**。

    参数
    ----
    absPath  : 绝对路径
    fileSize / mtime : 已知的 stat 结果（调用方遍历时已经 stat 过，别重复 stat）

    返回
    ----
    dict —— mimeType / width / height / orientation / cameraModel / takenAt /
            lat / lon / placeName / shotYear / shotYearSource

    绝不抛错：解不开就退化成「只有文件名的年份」，让这张图仍能入库，
    后续步骤（缩略图/ 人脸）再各自报错。
    """
    absPath = os.path.abspath(str(absPath))
    fileName = os.path.basename(absPath)
    meta = emptyMeta(absPath)
    if mtime is None:
        try:
            mtime = os.stat(absPath).st_mtime
        except OSError:
            mtime = 0.0

    # ---- EXIF（只读文件头，不解像素）----
    try:
        from PIL import Image
    except ImportError as e:      # Pillow 缺失属于环境问题，但同样不该中断扫描
        _LOG.warning("readMeta: Pillow 不可用（%s），本轮全部按无 EXIF 处理" % e)
        Image = None

    if Image is not None:
        try:
            with Image.open(absPath) as image:
                meta["mimeType"] = mimeOfFormat(image.format, absPath)
                meta["width"], meta["height"] = int(image.size[0]), int(image.size[1])
                # getexif() 只解析 APP1 段，代价极小；坏 EXIF 也不能让整张图废掉
                try:
                    meta.update(parseExifObject(image.getexif()))
                except Exception as e:
                    _LOG.warning("readMeta: EXIF 段解析失败（已忽略）: %s (%s)"
                                 % (os.path.basename(absPath), e))
        except Exception as e:
            # RAW 无解码器 / 文件被截断 / 实际是文本文件改了扩展名 —— 都走这里
            _LOG.warning("readMeta: 图像解码失败，按无 EXIF 入库: %s (%s: %s)"
                         % (os.path.basename(absPath), type(e).__name__, e))
            meta = emptyMeta(absPath)

    # ---- 拍摄年份：EXIF -> 文件名 -> mtime ----
    shot = resolveShotYear(meta.get("takenAt"), fileName, mtime)
    meta["shotYear"] = shot["shotYear"]
    meta["shotYearSource"] = shot["source"]

    # ---- GPS 逆地理（可选依赖，缺失即降级）----
    if meta.get("lat") is not None and meta.get("lon") is not None:
        meta["placeName"] = reverseGeocode(meta["lat"], meta["lon"])

    if fileSize is not None:
        meta["fileSize"] = int(fileSize)
    return meta


if __name__ == "__main__":
    import sys as _sys

    _fixOut = getattr(_sys.stdout, "reconfigure", None)
    if _fixOut:
        _fixOut(encoding="utf-8", errors="replace")
    print("meta _VERSION   :", _VERSION)
    print("时区偏移        :", basicSettings.EXIF_LOCAL_UTC_OFFSET_HOURS, "小时")
    print("截图覆盖 EXIF   :", basicSettings.SCREENSHOT_OVERRIDES_EXIF)
    for name in ("mmexport_1682908800000.jpg", "IMG_20240501_120000.jpg",
                 "2021-11-11 合影.jpg", "Screenshot_20240501_120000.png",
                 "截图 2024.png", "DSC00001.JPG"):
        print("  %-34s -> %s" % (name, resolveShotYear(None, name, 0)))
    print("  %-34s -> %s" % ("mmexport_1682908800000.jpg + EXIF",
                             resolveShotYear("2023-05-01T04:00:00Z",
                                             "mmexport_1682908800000.jpg", 0)))
    if len(_sys.argv) > 1:
        for one in _sys.argv[1:]:
            print(one, "->", readMeta(one))
