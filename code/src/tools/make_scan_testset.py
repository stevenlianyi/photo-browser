#! /usr/bin/env python3
#encoding: utf-8

# Filename: make_scan_testset.py
# Description: 造扫描器验收测试集（约 300 张，覆盖步骤 3 的全部边界）
#
# 造什么
# ------
#   * 4~5 级子目录嵌套
#   * 带 EXIF 的照片（DateTimeOriginal / 机型 / 方向 / GPS）
#   * 缺 EXIF 的照片（走文件名 / mtime 兜底）
#   * 微信导出名 mmexport<13位毫秒>、各种日期文件名
#   * 截图类文件名（Screenshot* / 截图*）—— 期望 shotYear = NULL
#   * 非照片文件（.txt / .mp4 / 无扩展名）—— 期望被白名单过滤掉
#   * @eaDir / thumbs 目录—— 期望被排除目录过滤掉
#   * unicode / 空格 / 括号 等刁钻文件名（验「relPath 逐字保留」）
#   * 若干「成对重复内容」的图（复制出来的，期望 isDuplicate=1）
#
# 用法
# ----
#   python code\src\tools\make_scan_testset.py --root d:\tmp\pb_scan\photo
#   python code\src\tools\make_scan_testset.py --root d:\tmp\pb_scan\photo --count 300
#
# ⚠️ 只往 --root 指定的目录写；**绝不动真实的 d:\PhotoLib\photo**（默认值是临时目录）。

import argparse
import datetime
import os
import random
import sys
import time

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                                  # noqa: E402
from PIL import Image                                                  # noqa: E402
from PIL.TiffImagePlugin import IFDRational                            # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("makeScanTestset", "makescantestset.log")

# 刁钻文件名：空格 / 括号 / 中文 / 组合字符（NFD）/ 短横线
_TRICKY_NAMES = (
    "IMG 0001.jpg", "IMG-0002 (1).jpg", "照片 001.jpg", "café_NFD.jpg",
    "PXL_20231231_235959.jpg", "DSC_00001.JPG",
)

# 5 级子目录
_DIRS = (
    "2024", "2024-05-01", "trip", "day2", "raw",
    "2023", "2023-07-15", "family", "kid", "phone",
    "2019", "misc", "wechat", "import", "batch01",
)


def _rndColor(rnd: random.Random):
    return (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))


def makeJpeg(path: str, width: int, height: int, rnd: random.Random,
             takenAt: str = None, model: str = None, orientation: int = None,
             gps: tuple = None) -> None:
    """造一张 JPEG；给了 takenAt/model/orientation/gps 就写进 EXIF"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image = Image.new("RGB", (width, height), _rndColor(rnd))
    # 画点东西，避免全图纯色导致 fileHash 撞车时不好分辨
    for _ in range(3):
        box = (rnd.randint(0, width - 4), rnd.randint(0, height - 4),
               rnd.randint(0, width), rnd.randint(0, height))
        try:
            image.paste(_rndColor(rnd), box)
        except ValueError:
            pass
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if model:
        exif[0x0110] = model
    if takenAt:
        exifIfd = exif.get_ifd(0x8769)
        exifIfd[0x9003] = takenAt.encode("ascii")
    if gps:
        gpsIfd = exif.get_ifd(0x8825)
        lat, lon = gps
        gpsIfd[0x0001] = b"N" if lat >= 0 else b"S"
        gpsIfd[0x0002] = (IFDRational(int(abs(lat)), 1),
                         IFDRational(int(round((abs(lat) % 1) * 60)), 1),
                         IFDRational(0, 1))
        gpsIfd[0x0003] = b"E" if lon >= 0 else b"W"
        gpsIfd[0x0004] = (IFDRational(int(abs(lon)), 1),
                         IFDRational(int(round((abs(lon) % 1) * 60)), 1),
                         IFDRational(0, 1))
    if takenAt or model or orientation or gps:
        image.save(path, exif=exif, quality=72)
    else:
        image.save(path, quality=72)
    image.close()


def makePlain(path: str, rnd: random.Random, size=(320, 240)) -> None:
    """造一张**没有 EXIF** 的图（走文件名 / mtime 兜底）"""
    makeJpeg(path, size[0], size[1], rnd, takenAt=None, model=None)


def build(root: str, count: int = 300, seed: int = 20261004) -> dict:
    """造测试集。返回统计 dict"""
    rnd = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    start = time.time()

    made = {"photo": 0, "noExif": 0, "screenshot": 0, "wechat": 0, "tricky": 0,
            "gps": 0, "dupPair": 0, "nonPhoto": 0, "excluded": 0}
    plan = []

    # ---- 1) 主体：有 EXIF 的照片，散在 5 级子目录里 ----
    withExif = int(count * 0.55)
    for index in range(withExif):
        subDir = os.path.join(root, _DIRS[index % len(_DIRS)])
        year = 2018 + (index % 7)
        taken = datetime.datetime(year, 1 + (index % 12), 1 + (index % 28),
                                  9 + (index % 10), index % 60, 0)
        gps = (39.9075 + (index % 5) * 0.01, 116.3914 + (index % 7) * 0.01) \
            if index % 9 == 0 else None
        plan.append(("exif", os.path.join(subDir, "IMG_%04d.jpg" % index),
                     taken, gps))

    # ---- 2) 无 EXIF：文件名带日期（走文件名兜底）----
    for index in range(int(count * 0.20)):
        subDir = os.path.join(root, _DIRS[(index + 3) % len(_DIRS)])
        plan.append(("noExif",
                     os.path.join(subDir, "2019%02d%02d_%04d.jpg"
                                  % (1 + index % 12, 1 + index % 28, index)), None, None))

    # ---- 3) 微信导出名 mmexport<13 位毫秒>（无 EXIF）----
    for index in range(int(count * 0.10)):
        subDir = os.path.join(root, "2019", "wechat", "import", "batch01")
        msTime = int(time.mktime((2020 + index % 4, 1 + index % 12, 1 + index % 27,
                                  10, index % 60, 0, 0, 0, -1)) * 1000)
        plan.append(("wechat", os.path.join(subDir, "mmexport_%d.jpg" % msTime),
                     None, None))

    # ---- 4) 截图类（shotYear 必须是 NULL）----
    for index in range(int(count * 0.05)):
        subDir = os.path.join(root, "2023", "misc")
        name = ("Screenshot_2024050%d_12000%d.png" % (1 + index % 9, index % 10)
                if index % 2 == 0 else "截图 2023%02d%02d.png" % (1 + index % 12,
                                                                1 + index % 28))
        plan.append(("screenshot", os.path.join(subDir, name), None, None))

    # ---- 5) 刁钻文件名（验 relPath 逐字保留）----
    for index, name in enumerate(_TRICKY_NAMES):
        subDir = os.path.join(root, "2024", "2024-05-01", "trip", "day2", "raw")
        plan.append(("tricky", os.path.join(subDir, name), None, None))
        made["tricky"] += 1

    # ---- 6) 成对重复内容（同一张图复制到另一个路径，期望 isDuplicate=1）----
    dupSource = os.path.join(root, "2024", "2024-05-01", "IMG_0001.jpg")
    os.makedirs(os.path.dirname(dupSource), exist_ok=True)
    makeJpeg(dupSource, 640, 480, rnd,
             takenAt="2024:05:01 10:00:00", model="ScanCam A1", orientation=1)
    made["photo"] += 1
    for index in range(3):
        target = os.path.join(root, "2023", "family", "copy_of_%d.jpg" % index)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(dupSource, "rb") as src:
            data = src.read()
        with open(target, "wb") as dst:
            dst.write(data)
        made["dupPair"] += 1

    # ---- 真正造图 ----
    for kind, path, taken, gps in plan:
        if os.path.exists(path):
            continue
        if kind == "exif":
            makeJpeg(path, 640 + rnd.randint(0, 400), 480 + rnd.randint(0, 300), rnd,
                     takenAt=taken.strftime("%Y:%m:%d %H:%M:%S") if taken else None,
                     model="TestCam %d" % (rnd.randint(1, 5)),
                     orientation=rnd.choice([1, 1, 1, 3, 6, 8]), gps=gps)
            made["photo"] += 1
            if gps:
                made["gps"] += 1
        elif kind == "screenshot":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Image.new("RGB", (1080, 1920), _rndColor(rnd)).save(path, "PNG")
            made["screenshot"] += 1
        else:
            makePlain(path, rnd)
            made["noExif"] += 1
            if kind == "wechat":
                made["wechat"] += 1

    # ---- 7) 非照片文件与排除目录：期望被过滤掉，一条都不入库 ----
    noise = [
        os.path.join(root, "2024", "notes.txt"),
        os.path.join(root, "2024", "movie.mp4"),
        os.path.join(root, "2024", "noext"),
        os.path.join(root, "@eaDir", "2024", "thumbs", "x.jpg"),
        os.path.join(root, "thumbs", "ab", "ab12cd_200.webp"),
    ]
    for one in noise:
        os.makedirs(os.path.dirname(one), exist_ok=True)
        with open(one, "wb") as handle:
            handle.write(b"not a photo")
        made["nonPhoto" if "eaDir" not in one and "thumbs" not in one
              else "excluded"] += 1

    # ---- 8) 显式设定 mtime（验mtime 兜底）：把 3 张无 EXIF 图的 mtime 设成 2015 ----
    mtimeTouched = []
    base = os.path.join(root, "2019", "misc")
    for index in range(3):
        one = os.path.join(base, "mtime_only_%d.jpg" % index)
        if not os.path.exists(one):
            makePlain(one, rnd)
            made["noExif"] += 1
        stamp = time.mktime((2015, 6, 1 + index, 12, 0, 0, 0, 0, -1))
        os.utime(one, (stamp, stamp))
        mtimeTouched.append(one)

    made["elapsed"] = round(time.time() - start, 2)
    made["root"] = root
    return made


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="make_scan_testset",
        description="造扫描器验收测试集（约 300 张）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--root", required=True, help="测试集 photo 根目录")
    parser.add_argument("--count", type=int, default=300, help="照片数量级（默认 300）")
    parser.add_argument("--seed", type=int, default=20261004)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    print("make_scan_testset _VERSION: %s" % _VERSION)
    stat = build(args.root, count=args.count, seed=args.seed)
    print("测试集根: %s" % stat["root"])
    print("  带 EXIF 照片 : %d（其中带 GPS %d）" % (stat["photo"], stat["gps"]))
    print("  无 EXIF 照片 : %d（其中微信 mmexport %d）"
          % (stat["noExif"], stat["wechat"]))
    print("  截图类       : %d（期望 shotYear = NULL）" % stat["screenshot"])
    print("  刁钻文件名   : %d" % stat["tricky"])
    print("  重复内容副本 : %d（期望 isDuplicate=1）" % stat["dupPair"])
    print("  干扰文件     : %d / 排除目录内 %d（期望一条都不入库）"
          % (stat["nonPhoto"], stat["excluded"]))
    print("  耗时         : %s 秒" % stat["elapsed"])
    print("")
    print("接着可以验收：")
    print("  python code\\src\\tools\\scan_cli.py count --root %s" % stat["root"])
    print("  python code\\src\\tools\\scan_cli.py scan --root %s --db <临时库> --all"
          % stat["root"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
