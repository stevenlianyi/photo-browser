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
import stat
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
def guard_production_db():
    """**绝不允许测试碰正式库** —— 会话前后各校验一次。

    为什么需要这道闸
    --------------
      sqliteCommon.dbHandle() 是**进程级单例**，而生成层的 query_* /
      insertManyTableGeneral **不校验库文件是谁**。于是一个**忘了挂
      temp_db 夹具**的用例会安静地连上 d:\PhotoLib\db\photolib.db
      并往里写数据 —— 不报错、不中断，跑完还是「全绿」。
      实际踩过：R2 补 test_rebucket.py 时三个巡检用例漏挂 lib，
      把 10 行人脸 + 2 个人写进了正式库（靠本步开工前的整库备份救回来）。

      这里在会话开始时记住正式库的大小与修改时间，结束时比对：
      变了就直接**判整个测试会话失败**，让污染不可能无感知晓地溜过去。

    ⚠️ 会误报，报出来时**先排除外部原因**（步骤 9 就误报过一次：
       开发期间自己在用这个库 / 起了服务，闸照样变红）。
       判据在下面：**size 变没变**。
         · size 变了 -> **真有数据写进去**，按下面第 2 步查用例；
         · 只有 mtime 变、size 没变 -> 很可能是外部打开（连库时会跑一遍
           `PRAGMA journal_mode=WAL`，一次 checkpoint 就会改 mtime），
           先问自己刚才有没有动过库。
       这道闸宁可误报也不能漏报 —— 漏报的代价是正式库被静默写脏。
    """
    from common import paths as pathsMod

    try:
        prodFile = pathsMod.db_file()
    except Exception:                    # 拿不到路径就放弃这道闸
        yield
        return
    before = _fingerprint(prodFile)
    yield
    after = _fingerprint(prodFile)
    if before is None or after is None or before == after:
        return

    raise AssertionError(describeDbChange(prodFile, before, after))


def describeDbChange(prodFile, before, after) -> str:
    """把「正式库变了」这件事说清楚 —— **重点是把 size/mtime 分开判**。

    ⚠️ 为什么要把这个函数单独抽出来并单测：**这里的"产物"就是那句话**。
       上个版本不区分 size 与 mtime，于是「开发期间自己动过库」这种
       外部原因会被写成「多半是用例漏挂夹具」的断言 —— 接手的人
       按这句话去逐个用例排查，一定会空手而归（步骤 9 实测就白查了一轮）。
    """
    if int(before[0]) != int(after[0]):
        judge = ("⚠️ **文件大小也变了**（%d -> %d 字节）—— 这是「真有数据被写进去」"
                 "的强信号，基本可以确定是有用例漏挂了 temp_db / lib 夹具，"
                 "于是 sqliteCommon.dbHandle() 连上了正式库。"
                 % (before[0], after[0]))
    else:
        judge = ("⚠️ **只有 mtime 变、大小没变** —— 这**也可能不是测试干的**："
                 "你自己在跑服务 / 用这个库 / 有后台扫描在写，"
                 "或者只是有人把库打开了一下（连库时会执行 "
                 "`PRAGMA journal_mode=WAL`，一次 checkpoint 就会改 mtime）。"
                 "**先确认是不是你自己的操作**，确定不是再往下查。")
    return ("**测试期间正式库被改动了**（%s：%s -> %s）。\n%s\n"
            "排查办法（按文件 -> 再二分用例，30 秒能定位）：\n"
            "  1. 逐个文件跑：`python -m pytest src/test/test_xxx.py -q`，"
            "哪个文件让本闸变红就是它；\n"
            "  2. 再在该文件里二分用例（或用 `-k` 缩小范围）；\n"
            "  3. 找到后给它挂上 `temp_db` / `lib` / `api_env` 夹具。\n"
            "若已确认被写脏，用开工前的整库备份恢复。"
            % (prodFile, before, after, judge))


def _fingerprint(path: str):
    """(size, mtime) —— 取不到就返回 None（不做无谜的失败）。

    ⚠️ 必须**确认是普通文件**：
      Windows 上 `os.stat()` 对**目录**照样成功（返回 size=0 + 目录的 mtime），
      于是"路径配错成了目录"会被当成一个合法指纹参与比对 ——
      两个无意义的元组相等，闸就静默失效了（而这正是最需要它开口的时候）。
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_size, int(info.st_mtime))


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


# ============================================================
# api 层装置（步骤 9）
# ============================================================
#
# 为什么不用 temp_db 直接写 api 测试
# --------------------------------
#   api 测试要的不只是「一个空库」，而是**一份构造好的、能触发每条验收的数据**：
#   有跨年月的照片、有已确认/自动/未归属三种脸、有人脸已挂上人、
#   有质心。没有这些，验收第 8 条（改判后双方质心都重算）
#   与第 11 条（disputed 条数 == SQL 口径）就只能写成「跑通就算过」的假测试。
#
# ⚠️ 三处必须显式收尾的东西（否则会**污染下一个用例**）：
#   1. `sqliteCommon.dbHandle` 是**进程级单例** —— 不 closeDb() 就换库；
#   2. `paths` 的三条 root 覆盖是进程级的 —— 用完 clearRootOverride()；
#   3. `api.scan._scheduler` 是单例且**缓存着 ScanRunner 的全表索引** ——
#      换库后必须 resetScheduler()，否则它会拿着上一个库的索引去写新库，
#      而且**不报错**（那正是 scanScheduler._runnerFor 注释里说的反面情形）。

@pytest.fixture
def api_env(tmp_path, set_photo_root):
    """建一个带**构造数据**的临时库 + 一个已连上它的 FastAPI TestClient。

    返回 dict：
      dbFile   库文件路径
      root     临时PhotoLib 根
      client   fastapi.testclient.TestClient（**已进 lifespan**，库句柄已就位）
      seed()   返回构造数据（personCode / photoCode / faceCode 的对应关系）
    """
    import numpy as np
    from fastapi.testclient import TestClient

    from api import scan as scanApi
    from common import paths as pathsMod
    from database.auto_generated import sqliteCommon as sqliteCommon
    from engine.face import faceStore as faceStore
    from main.app import createApp
    from tools import build_db as build_db

    root = tmp_path / "PhotoLib"
    (root / "photo").mkdir(parents=True)
    (root / "thumb").mkdir(parents=True)
    (root / "db").mkdir(parents=True)
    set_photo_root(str(root))
    dbFile = str(root / "db" / "api.db")
    build_db.build(dbFile=dbFile, verbose=False)
    sqliteCommon.dbHandle(dbFile)
    scanApi.resetScheduler()

    # ---- 构造数据：3 个人 / N 张照片 / 4 类脸 ----
    people = [("P_alpha", "阿尔法", "1985-03-07"),
              ("P_beta", "贝塔", "1990-07-19"),
              ("P_gamma", "伽马", "1978-11-02")]
    sqliteCommon.insertManyTableGeneral(
        "pb_person",
        [{"personCode": c, "displayName": n, "birthday": b, "source": 0,
          "isConfirmed": 0, "regID": "test"} for c, n, b in people],
        conflictColumns=("personCode",),
        updateColumns=("displayName", "birthday", "modifyYMDHMS"),
        fillStandard=True, forceColumns=("birthday",))
    # ⚠️ **不能构造两个「字面同名」的人员** —— pb_person.displayName 上有
    #    UNIQUE 索引，写第二个会直接 `UNIQUE constraint failed`。
    #    所以验收第 25 条的「疑似同人」构造的是**真正会发生的形态**：
    #    同一个**中国手机号**、写成两种格式（"13800138000" / "138-0013-8000"）。
    #    pb_person.phone 没有唯一约束，可以重复；而 phoneKey 归一后两者相等
    #    —— 这正是通讯录里最常见的「同一个人被导进两次」。
    sqliteCommon.insertManyTableGeneral(
        "pb_person",
        [{"personCode": "P_dup1", "displayName": "张小明", "phone": "13800138000",
          "email": "xiaoming@example.com", "source": 0, "regID": "test"},
         {"personCode": "P_dup2", "displayName": "张晓明", "phone": "138-0013-8000",
          "email": "xiaoming@example.com", "source": 0, "regID": "test"}],
        conflictColumns=("personCode",),
        updateColumns=("displayName", "phone", "email", "modifyYMDHMS"),
        fillStandard=True)

    rng = np.random.default_rng(20261006)

    def _vec(seed: int):
        v = rng.normal(size=512).astype("float32")
        return faceStore.faceEngine.encodeEmbedding(
            faceStore.faceEngine.l2normalize(v))

    photos, faces = [], []
    # (photoCode, 相对路径, takenAt, shotYear, placeName, faceCount)
    plan = [("PH_2013_01", "2013/01/a.jpg", "2013-01-15T03:22:10Z", 2013, "北京", 2),
            ("PH_2013_07", "2013/07/b.jpg", "2013-07-02T09:00:00Z", 2013, "北京", 1),
            ("PH_2016_03", "2016/03/c.jpg", "2016-03-20T11:11:11Z", 2016, "上海", 2),
            ("PH_2020_11", "2020/11/d.jpg", "2020-11-05T04:44:00Z", 2020, "上海", 1),
            ("PH_2024_06", "2024/06/e.jpg", "2024-06-18T08:08:08Z", 2024, None, 2),
            ("PH_2024_06b", "2024/06/f.jpg", "2024-06-18T08:09:08Z", 2024, None, 0),
            ("PH_2019_04", "2019/04/g.jpg", "2019-04-09T06:00:00Z", 2019, "广州", 1),
            ("PH_NODATE", "misc/h.jpg", None, None, None, 1),
            ("PH_DUP", "misc/dup.jpg", "2013-01-15T03:22:10Z", 2013, "北京", 0)]
    for index, (code, rel, taken, year, place, faceCount) in enumerate(plan):
        photos.append({
            "photoCode": code, "relPath": rel,
            "relPathHash": "rh_%s" % code, "fileHash": "fh_%s" % code,
            "fileSize": 100000 + index, "mimeType": "image/jpeg",
            "width": 4000, "height": 3000, "orientation": 1,
            "takenAt": taken, "shotYear": year, "placeName": place,
            "cameraModel": "Canon EOS 350D", "faceCount": faceCount,
            "isDuplicate": 1 if code == "PH_DUP" else 0,
            "dupOfPhotoCode": "PH_2013_01" if code == "PH_DUP" else None,
            "isMissing": 0, "scanState": 1 if faceCount else 0,
            "regID": "test"})
        for f in range(faceCount):
            faces.append({
                "faceCode": "FC_%s_%d" % (code, f), "photoCode": code,
                "personCode": None, "clusterCode": "CL_%s_%d" % (code, f),
                "bbox": "0.1,0.1,0.2,0.2", "detScore": 0.9 - f * 0.1,
                "poseYaw": 1.0, "posePitch": -1.0, "quality": 0.8 - f * 0.1,
                "embedding": _vec(index * 10 + f),
                "shotBucket": faceStore.makeShotBucket(year),
                "isConfirmed": 0, "isStranger": 0})
    sqliteCommon.insertManyTableGeneral("pb_photo", photos,
                                        conflictColumns=("photoCode",),
                                        updateColumns=tuple(photos[0].keys()),
                                        fillStandard=True)
    sqliteCommon.insertManyTableGeneral("pb_face", faces,
                                        conflictColumns=("faceCode",),
                                        updateColumns=("photoCode", "personCode",
                                                       "clusterCode", "shotBucket",
                                                       "isConfirmed", "isStranger",
                                                       "modifyYMDHMS"),
                                        fillStandard=True,
                                        forceColumns=("personCode", "clusterCode",
                                                      "shotBucket"))

    seed = {"people": people,
            "photoCodes": [p["photoCode"] for p in photos],
            "faceCodes": [f["faceCode"] for f in faces]}

    application = createApp(dbFile=dbFile, photoRoot=str(root / "photo"),
                            thumbRoot=str(root / "thumb"), mountWeb=False, cors=False)
    with TestClient(application) as client:
        try:
            yield {"dbFile": dbFile, "root": root, "client": client, "seed": seed}
        finally:
            # ⚠️ **必须先确认后台扫描线程退出，再 closeDb()**：
            #    closeDb() 关掉两个 sqlite 连接，而一个还在读库的扫描线程
            #    拿到的是已释放的句柄 —— 那不是 Python 异常，是**进程级
            #    access violation**（实测踩过，堆栈落在 fetchAll 里，
            #    看起来像 sqlite 的 bug）。
            stopped = scanApi.resetScheduler(timeout=15.0)
            if stopped:
                sqliteCommon.closeDb()
            pathsMod.clearRootOverride()
            assert stopped, "后台扫描线程没退出；已跳过 closeDb() 以免段错误"


@pytest.fixture
def api_rows(api_env):
    """直查库的逃生口 —— 验收里「用 SQL 逐个核对」的那一半必须绕过 api 层。

    没有它，验收第 21 / 23 条只能信 api 自己报的数字 ——
    而那正是本项目反复强调的「自己验自己」陷阱。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    class _Rows(object):
        @staticmethod
        def count(tableName, where="", values=()):
            if where:
                return sqliteCommon.countWhereGeneral(tableName, where, values)
            return sqliteCommon.countTableGeneral(tableName, delFlag="*")

        @staticmethod
        def face(faceCode):
            got = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode,
                                             delFlag="*")
            return got[0] if got else {}

        @staticmethod
        def person(personCode):
            got = sqliteCommon.query_pb_person("pb_person", personCode=personCode,
                                               delFlag="*")
            return got[0] if got else {}

        @staticmethod
        def centroids(personCode):
            return sqliteCommon.query_pb_person_centroid(
                "pb_person_centroid", personCode=personCode, delFlag="*",
                orderBy="bucketKey")

        @staticmethod
        def links(photoCode=None, personCode=None):
            return sqliteCommon.query_pb_photo_person(
                "pb_photo_person", photoCode=str(photoCode or ""),
                personCode=str(personCode or ""), delFlag="*")

        @staticmethod
        def logs(opType=None):
            return sqliteCommon.query_pb_review_log("pb_review_log",
                                                   opType=str(opType or ""),
                                                   delFlag="*", orderBy="recID")

        @staticmethod
        def photo(photoCode):
            got = sqliteCommon.query_pb_photo("pb_photo", photoCode=photoCode,
                                              delFlag="*")
            return got[0] if got else {}

    return _Rows

