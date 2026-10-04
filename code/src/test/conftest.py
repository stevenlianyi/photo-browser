#! /usr/bin/env python3
#encoding: utf-8

#Filename: conftest.py
#Description: pytest 公共装置 —— sys.path 注入 + 临时照片库 fixture
#
# 关键点：
#   1. 把 <repo>/code/src 挂到 sys.path，使 `from common import paths` /
#      `from config import basicSettings` 在任何 cwd 下都能 import；
#   2. 所有涉及「写目录」的测试一律用 tmp 临时根目录，
#      **绝不在真实的 d:\PhotoLib 下做写操作**，更绝不碰 photo 目录；
#   3. 配置通过 monkeypatch 打桩，随用随还原，绝不污染本机local_settings.py。

import os
import sys

import pytest

# .../code/src/test/conftest.py -> .../code/src
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# 让测试可以 `from common import paths` / `from config import basicSettings`
from common import paths as paths  # noqa: E402


# ============================================================
# sys.path 装置
# ============================================================

@pytest.fixture(scope="session", autouse=True)
def ensure_src_on_path():
    """确保 code/src 在 sys.path首位（幂等）"""
    if _SRC_DIR in sys.path:
        sys.path.remove(_SRC_DIR)
    sys.path.insert(0, _SRC_DIR)
    return _SRC_DIR


# ============================================================
# 配置打桩装置
# ============================================================

@pytest.fixture
def set_photo_root(monkeypatch):
    """返回可调用的打桩函数：set_photo_root(root, thumb="", db="")。

    直接改 config.local_settings 的属性，paths.readSetting() 每次调用都重读，
    因此打桩立即生效且随 fixture 结束自动还原。
    """
    from config import local_settings as ls

    def _apply(root, thumb="", db=""):
        monkeypatch.setattr(ls, "PHOTO_ROOT", str(root), raising=False)
        monkeypatch.setattr(ls, "THUMB_ROOT", str(thumb), raising=False)
        monkeypatch.setattr(ls, "DB_FILE", str(db), raising=False)
        return str(root)
    return _apply


@pytest.fixture
def photo_root_of(set_photo_root, tmp_path):
    """创建一个空的临时照片库根目录并打桩到 PHOTO_ROOT。

    只建根目录本身，**不建 photo/thumb/db** —— 让被测代码自己去判断该建什么。
    """
    root = tmp_path / "PhotoLib"
    root.mkdir()
    set_photo_root(str(root))
    return root


@pytest.fixture
def sandbox_photo_root(set_photo_root, tmp_path):
    """临时照片库根目录，且已存在空的 photo/ 子目录。

    有了 photo/ 才能验证「ensure_dirs() 之后 photo/ 里仍然一个文件都没有」。
    """
    root = tmp_path / "PhotoLib"
    (root / "photo").mkdir(parents=True)
    set_photo_root(str(root))
    return root


@pytest.fixture
def layout(tmp_path):
    """合法布局的三元组 (photo, thumb, db)：全部落在 tmp 临时根目录下，彼此不嵌套。

    刻意不依赖 local_settings，用来单测 validate_layout 的「显式传参」分支。
    """
    root = str(tmp_path / "PhotoLib")
    return (
        os.path.join(root, "photo"),
        os.path.join(root, "thumb"),
        os.path.join(root, "db", "photolib.db"),
    )


# ============================================================
# 扫描器（步骤 3）装置
# ============================================================

@pytest.fixture
def scan_root(tmp_path, set_photo_root):
    """扫描器用的临时 photo 根（**空目录**），并把 PHOTO_ROOT 打桩过去。

    打桩 PHOTO_ROOT 是为了让 paths.db_file() / ensure_dirs() 全部落在 tmp 里；
    测试全程**绝不在真实的 d:\\PhotoLib 下写任何东西**。
    """
    root = tmp_path / "PhotoLib"
    photo = root / "photo"
    photo.mkdir(parents=True)
    set_photo_root(str(root))
    return photo


@pytest.fixture
def temp_db(tmp_path, set_photo_root):
    """临时库文件（8 张表已建），用完关掉全局句柄。

    sqliteCommon.dbHandle 是**进程级单例**，不关掉会让下一个测试连到上一个库。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from tools import build_db as build_db

    root = tmp_path / "PhotoLib"
    root.mkdir(exist_ok=True)
    set_photo_root(str(root))
    dbFile = str(tmp_path / "state" / "scantest.db")
    build_db.build(dbFile=dbFile, verbose=False)
    try:
        yield dbFile
    finally:
        sqliteCommon.closeDb()


@pytest.fixture
def make_photo():
    """在指定路径造一张测试图：make_photo(path, takenAt=None, model=None, gps=None)

    takenAt 传 "2023:05:01 12:00:00" 这种 EXIF 串；不传就是**无 EXIF** 的图。

    ⚠️ 颜色与色块位置由**路径的 crc32** 决定：同一路径重复造图结果稳定（幂等），
       不同路径内容必定不同。
       这一条很关键 —— 若所有测试图内容相同，fileHash 就全一样，
       扫描器会（正确地）把它们全判成重复，去重相关的用例就全废了。
    """
    import random
    import zlib

    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    def _make(path, width=64, height=48, color=None, takenAt=None,
              model=None, orientation=None, gps=None, fmt=None):
        path = str(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rnd = random.Random(zlib.crc32(path.encode("utf-8")))
        if color is None:
            color = (rnd.randint(1, 255), rnd.randint(1, 255), rnd.randint(1, 255))
        image = Image.new("RGB", (width, height), color)
        # 画两块随机色块，保证不同路径内容不同 -> fileHash 不同
        for _ in range(2):
            x0 = rnd.randint(0, max(0, width - 2))
            y0 = rnd.randint(0, max(0, height - 2))
            block = Image.new("RGB", (rnd.randint(1, max(1, width // 3)),
                                     rnd.randint(1, max(1, height // 3))),
                              (rnd.randint(1, 255), rnd.randint(1, 255),
                               rnd.randint(1, 255)))
            image.paste(block, (x0, y0))
        if takenAt or model or orientation or gps:
            exif = Image.Exif()
            if orientation:
                exif[0x0112] = orientation
            if model:
                exif[0x0110] = model
            if takenAt:
                exif.get_ifd(0x8769)[0x9003] = str(takenAt).encode("ascii")
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
            image.save(path, exif=exif)
        elif fmt == "PNG":
            image.save(path, "PNG")
        else:
            image.save(path)
        image.close()
        return path

    return _make

