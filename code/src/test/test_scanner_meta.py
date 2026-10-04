#! /usr/bin/env python3
#encoding: utf-8

# Filename: test_scanner_meta.py
# Description: processor/scanner/meta.py 单测
#
# 覆盖五块：
#   A. EXIF 时间 -> UTC ISO8601（含 bytes 解码、占位值、时区换算）
#   B. shotYear 三级优先：EXIF -> 文件名 -> mtime，外加截图短路
#   C. 文件名识别：mmexport 13 位毫秒 / 8 位日期 / 非法日期 / mtime-only
#   D. EXIF 解析：方向 / 机型 / 拍摄时间 / GPS（含南纬西经）
#   E. 降级纪律：解不开的图、逆地理依赖缺失，都**只降级不抛错**
#
# 硬约束：只在 tmp_path 下造图，绝不碰真实照片库。

import datetime
import os
import time

import pytest

from config import basicSettings as basicSettings
from processor.scanner import meta as meta


# ============================================================
# A. EXIF 时间
# ============================================================

class TestExifTime:
    def test_local_to_utc(self):
        """东八区 12:00 -> UTC 04:00"""
        assert meta.exifTimeToUTC("2023:05:01 12:00:00") == "2023-05-01T04:00:00Z"

    def test_offset_from_config(self):
        text = meta.exifTimeToUTC("2023:05:01 12:00:00")
        assert text.endswith("Z")
        assert text.startswith("2023-05-01T04:")

    def test_bytes_input(self):
        """Pillow 把 EXIF 的 ASCII 字段读成 bytes，不解码必然挂在定长校验上"""
        assert meta.exifTimeToUTC(b"2023:05:01 12:00:00") == "2023-05-01T04:00:00Z"

    def test_subsecond_and_offset_suffix_ignored(self):
        assert meta.exifTimeToUTC("2023:05:01 12:00:00.123") == "2023-05-01T04:00:00Z"
        assert meta.exifTimeToUTC("2023:05:01 12:00:00+08:00") == "2023-05-01T04:00:00Z"

    @pytest.mark.parametrize("bad", [None, "", "   ", "0000:00:00 00:00:00",
                                     "not a date", "2023-05-01", 12345])
    def test_invalid_returns_none(self, bad):
        assert meta.exifTimeToUTC(bad) is None

    def test_year_of_utc(self):
        assert meta.yearOfUTC("2023-05-01T04:00:00Z") == 2023
        assert meta.yearOfUTC("") is None
        assert meta.yearOfUTC(None) is None


# ============================================================
# B. shotYear 优先级
# ============================================================

class TestResolveShotYear:
    def test_exif_wins(self):
        got = meta.resolveShotYear("2019-05-01T04:00:00Z", "IMG_20240501.jpg", 0)
        assert got == {"shotYear": 2019, "source": meta.SHOT_YEAR_SRC_EXIF}

    def test_fallback_to_name(self):
        got = meta.resolveShotYear(None, "IMG_20240501_120000.jpg", 0)
        assert got == {"shotYear": 2024, "source": meta.SHOT_YEAR_SRC_NAME}

    def test_fallback_to_mtime(self):
        stamp = time.mktime((2015, 6, 1, 12, 0, 0, 0, 0, -1))
        got = meta.resolveShotYear(None, "DSC00001.JPG", stamp)
        assert got == {"shotYear": 2015, "source": meta.SHOT_YEAR_SRC_MTIME}

    def test_mtime_is_last_resort(self):
        """mtime 是最不可靠的一档：文件名能认出来就不看 mtime"""
        stamp = time.mktime((2015, 6, 1, 12, 0, 0, 0, 0, -1))
        got = meta.resolveShotYear(None, "20240501_x.jpg", stamp)
        assert got["source"] == meta.SHOT_YEAR_SRC_NAME
        assert got["shotYear"] == 2024

    def test_nothing_recognizable(self):
        got = meta.resolveShotYear(None, "DSC00001.JPG", 0)
        assert got == {"shotYear": None, "source": meta.SHOT_YEAR_SRC_NONE}

    def test_out_of_range_year_rejected(self):
        """超出 SHOT_YEAR_MIN..MAX 的年份不采信（mktime 在 Windows 上不能早于 1970，
        所以用「未来年份」验上界）"""
        stamp = time.mktime((2200, 6, 1, 12, 0, 0, 0, 0, -1))
        assert meta.resolveShotYear(None, "x.jpg", stamp)["shotYear"] is None


class TestScreenshot:
    @pytest.mark.parametrize("name", [
        "Screenshot_20240501_120000.png", "screenshot.png", "SCREENSHOT 2024.png",
        "截图 20240202.png", "截屏.png", "ScreenShot-1.jpg", "屏幕截图_1.png",
    ])
    def test_detected(self, name):
        assert meta.isScreenshotName(name) is True

    @pytest.mark.parametrize("name", [
        "IMG_20240501.jpg", "我的截图合集.jpg", "DSC_0001.JPG", "",
        "20240501_Screenshot.png",           # 截图在中间不算
    ])
    def test_not_detected(self, name):
        assert meta.isScreenshotName(name) is False

    def test_screenshot_without_exif_is_null(self):
        """默认口径（EXIF 优先）：截图名只在**没有 EXIF** 时短路，落NULL"""
        got = meta.resolveShotYear(None, "Screenshot_20240501_120000.png", 0)
        assert got["shotYear"] is None
        assert got["source"] == meta.SHOT_YEAR_SRC_SCREENSHOT

    def test_screenshot_with_exif_keeps_exif_year(self):
        """截图名**不压过** EXIF：带 DateTimeOriginal 的截图仍取EXIF 年份"""
        got = meta.resolveShotYear("2023-05-01T04:00:00Z", "Screenshot_2024.png", 0)
        assert got == {"shotYear": 2023, "source": meta.SHOT_YEAR_SRC_EXIF}

    def test_default_config_is_exif_first(self):
        assert basicSettings.SCREENSHOT_OVERRIDES_EXIF is False

    def test_can_configure_screenshot_to_override_exif(self, monkeypatch):
        """把 SCREENSHOT_OVERRIDES_EXIF 打开，截图名就连EXIF 也不认（改配置即改行为）"""
        monkeypatch.setattr(basicSettings, "SCREENSHOT_OVERRIDES_EXIF", True)
        got = meta.resolveShotYear("2023-05-01T04:00:00Z", "Screenshot_2024.png", 0)
        assert got == {"shotYear": None, "source": meta.SHOT_YEAR_SRC_SCREENSHOT}


# ============================================================
# C. 文件名识别
# ============================================================

class TestShotYearFromName:
    @pytest.mark.parametrize("name,year", [
        ("mmexport_1682908800000.jpg", 2023),
        ("mmexport1682908800000.jpg", 2023),
        ("IMG_20240501_120000.jpg", 2024),
        ("2024-05-01.jpg", 2024),
        ("2024_05_01_123456.jpg", 2024),
        ("2024.05.01.jpg", 2024),
        ("VID_20240501.mp4.jpg", 2024),          # 只看 stem
        ("PXL_20231231_235959.jpg", 2023),
        ("2024-05.jpg", 2024),
        ("DSC00001.JPG", None),
        ("", None),
        ("19999999.jpg", None),                  # 不是合法日期 -> 拒绝
        ("20241301.jpg", None),                  # 13 月
        ("20240230.jpg", None),                  # 2 月 30 日
    ])
    def test_names(self, name, year):
        got, source = meta.shotYearFromName(name)
        assert got == year
        if year is None:
            assert source == meta.SHOT_YEAR_SRC_NONE

    def test_13digit_epoch_anywhere_in_stem(self):
        year, _src = meta.shotYearFromName("WeChat_mmexport1682908800000_x.jpg")
        assert year == 2023

    def test_10digit_epoch_stem(self):
        year, _src = meta.shotYearFromName("%d.jpg" % 1682908800)
        assert year == 2023

    def test_mtime_only(self, tmp_path):
        target = tmp_path / "DSC1.jpg"
        target.write_bytes(b"x")
        stamp = time.mktime((2016, 3, 4, 5, 6, 7, 0, 0, -1))
        os.utime(str(target), (stamp, stamp))
        assert meta.shotYearFromMTime(os.stat(str(target)).st_mtime) == 2016


# ============================================================
# D. EXIF 解析
# ============================================================

def _exifWithAllTags():
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x0110] = "TestCam X1"
    exif[0x010F] = "TestCam"
    exif.get_ifd(0x8769)[0x9003] = b"2023:05:01 12:00:00"
    gpsIfd = exif.get_ifd(0x8825)
    gpsIfd[0x0001] = b"N"
    gpsIfd[0x0002] = (IFDRational(39, 1), IFDRational(54, 1), IFDRational(26, 1))
    gpsIfd[0x0003] = b"E"
    gpsIfd[0x0004] = (IFDRational(116, 1), IFDRational(23, 1), IFDRational(29, 1))
    return exif


class TestParseExif:
    def test_all_tags(self):
        got = meta.parseExifObject(_exifWithAllTags())
        assert got["orientation"] == 6
        assert got["cameraModel"] == "TestCam X1"
        assert got["takenAt"] == "2023-05-01T04:00:00Z"
        assert got["lat"] == 39.9072222
        assert got["lon"] == 116.3913889

    def test_south_west(self):
        from PIL import Image
        from PIL.TiffImagePlugin import IFDRational
        exif = Image.Exif()
        gpsIfd = exif.get_ifd(0x8825)
        gpsIfd[0x0001] = b"S"
        gpsIfd[0x0002] = (IFDRational(33, 1), IFDRational(52, 1), IFDRational(0, 1))
        gpsIfd[0x0003] = b"W"
        gpsIfd[0x0004] = (IFDRational(151, 1), IFDRational(12, 1), IFDRational(0, 1))
        got = meta.parseExifObject(exif)
        assert got["lat"] < 0 and got["lon"] < 0
        assert round(got["lat"], 4) == -33.8667
        assert round(got["lon"], 4) == -151.2

    def test_fall_back_to_make_when_no_model(self):
        from PIL import Image
        exif = Image.Exif()
        exif[0x010F] = "OnlyMake"
        assert meta.parseExifObject(exif)["cameraModel"] == "OnlyMake"

    def test_empty_exif(self):
        from PIL import Image
        got = meta.parseExifObject(Image.Exif())
        assert got["orientation"] is None
        assert got["takenAt"] is None
        assert got["lat"] is None

    def test_none_exif(self):
        assert meta.parseExifObject(None)["cameraModel"] is None


# ============================================================
# E. readMeta 与降级
# ============================================================

class TestReadMeta:
    def test_real_file_with_exif(self, tmp_path, make_photo):
        path = make_photo(str(tmp_path / "a.jpg"), width=200, height=100,
                          takenAt="2023:05:01 12:00:00", model="TestCam X1",
                          orientation=6)
        got = meta.readMeta(path)
        assert got["mimeType"] == "image/jpeg"
        assert (got["width"], got["height"]) == (200, 100)
        assert got["orientation"] == 6
        assert got["cameraModel"] == "TestCam X1"
        assert got["takenAt"] == "2023-05-01T04:00:00Z"
        assert got["shotYear"] == 2023
        assert got["shotYearSource"] == meta.SHOT_YEAR_SRC_EXIF

    def test_file_without_exif_uses_name(self, tmp_path, make_photo):
        path = make_photo(str(tmp_path / "2021-11-11.jpg"))
        got = meta.readMeta(path)
        assert got["takenAt"] is None
        assert got["shotYear"] == 2021
        assert got["shotYearSource"] == meta.SHOT_YEAR_SRC_NAME

    def test_file_without_exif_and_name_uses_mtime(self, tmp_path, make_photo):
        path = make_photo(str(tmp_path / "DSC1.jpg"))
        stamp = time.mktime((2014, 8, 9, 10, 0, 0, 0, 0, -1))
        os.utime(path, (stamp, stamp))
        got = meta.readMeta(path)
        assert got["shotYear"] == 2014
        assert got["shotYearSource"] == meta.SHOT_YEAR_SRC_MTIME

    def test_screenshot_year_null(self, tmp_path, make_photo):
        """截图类（无 EXIF）shotYear 必须是 NULL"""
        path = make_photo(str(tmp_path / "Screenshot_20240501_120000.png"),
                          fmt="PNG")
        got = meta.readMeta(path)
        assert got["shotYear"] is None
        assert got["mimeType"] == "image/png"

    def test_screenshot_with_exif_keeps_year(self, tmp_path, make_photo):
        """截图名 + 有 EXIF：按默认口径（EXIF 优先）仍取EXIF 年份"""
        # 用 .jpg：Pillow 的 PNG 插件不保证写 eXIf 块，验「有 EXIF」必须走 JPEG
        path = make_photo(str(tmp_path / "Screenshot_20240501.jpg"),
                          takenAt="2022:03:04 05:06:07")
        got = meta.readMeta(path)
        assert got["shotYear"] == 2022
        assert got["shotYearSource"] == meta.SHOT_YEAR_SRC_EXIF

    def test_broken_file_degrades(self, tmp_path):
        """扩展名是 .jpg 但内容是文本 —— 必须降级入库，绝不能抛异常中断整轮扫描"""
        path = tmp_path / "broken.jpg"
        path.write_bytes(b"this is definitely not a jpeg")
        got = meta.readMeta(str(path))
        assert got["width"] is None
        assert got["height"] is None
        assert got["takenAt"] is None
        assert got["cameraModel"] is None
        # EXIF 全空 -> 年份退到 mtime（文件刚建 = 今年）
        assert got["shotYearSource"] == meta.SHOT_YEAR_SRC_MTIME
        assert got["shotYear"] == datetime.datetime.now().year

    def test_missing_file_degrades(self, tmp_path):
        got = meta.readMeta(str(tmp_path / "not-there.jpg"))
        assert got["shotYear"] is None

    def test_empty_meta_shape(self):
        got = meta.emptyMeta("a.jpg")
        for key in ("mimeType", "width", "height", "orientation", "cameraModel",
                    "takenAt", "lat", "lon", "placeName", "shotYear"):
            assert key in got


class TestReverseGeocode:
    def test_missing_dependency_degrades(self, monkeypatch):
        """reverse_geocoder 导入失败 -> placeName 留空 + 只记一次 warning，不抛错"""
        import builtins
        realImport = builtins.__import__

        def _fakeImport(name, *args, **kwargs):
            if name == "reverse_geocoder":
                raise ImportError("No module named 'reverse_geocoder'")
            return realImport(name, *args, **kwargs)

        monkeypatch.setattr(meta, "_RG_READY", None)
        monkeypatch.setattr(meta, "_RG_CACHE", {})
        monkeypatch.setattr(builtins, "__import__", _fakeImport)
        assert meta.reverseGeocode(39.9, 116.4) is None
        assert meta.reverseGeocode(39.9, 116.4) is None      # 第二次走缓存
        assert meta._RG_READY is False      # 记住「不可用」，不再反复 import

    def test_no_gps_returns_none(self):
        assert meta.reverseGeocode(None, None) is None
        assert meta.reverseGeocode(200.0, 300.0) is None

    def test_compose_place_name_dedup_and_truncate(self):
        composed = meta._composePlaceName({"cc": "CN", "admin1": "Beijing",
                                          "admin2": "Beijing", "name": "Dongcheng"})
        assert composed == "CN, Beijing, Dongcheng"
        assert len(meta._composePlaceName({"name": "x" * 400})) == 256
        assert meta._composePlaceName({}) is None
        assert meta._composePlaceName(None) is None
