#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_era_bucket_pack.py
#Description: R15 年代档打包单测（DR-56 ~ DR-58）
#
# 覆盖 plan/年代打包规划.md 第八节「验收清单」里可自动化的那几条：
#   ① zip 内容 = 该档照片（一张一次、同名去重、缺失跳过并计数）
#   ② 幂等复用 / force 重打
#   ③ 中途失败**不留半截产物**（这是本项目对 zip 的硬要求）
#   ④ 异步接口：起任务 → 轮询 → READY → 下载
#   ⑤ 全局单任务：运行中再点 → 409
#   ⑥ **打包前后 photo 目录指纹不变**（原图只读红线）
#
# 两段结构：
#   §一 engine/media 层 —— 不连库、不起 FastAPI，直接喂记录（快、准）
#   §二 api 层        —— 用 conftest 的 api_env（真实临时库 + TestClient）
#
# ⚠️ 原图一律造在**临时库**里；conftest 的会话闸会抓住任何碰 d:\PhotoLib 的用例。

import io
import os
import time
import zipfile

import pytest

from common import paths as paths
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon
from engine.sample import bucketPool as bucketPool
from processor.media import eraPack as eraPack

#: 单测统一用一个「像样」的年代档键（能进 URL、能进文件名）
BUCKET = "2003-2012"


# ============================================================
# 一、engine / media 层
# ============================================================

def makeOriginal(root, relPath, payload=b"\xff\xd8fake-jpeg\xff\xd9"):
    """在临时 photo 目录里造一张原图（zip 打包读的就是它）。"""
    target = root.joinpath("photo", *relPath.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload + relPath.encode("utf-8"))
    return target


def photoRow(relPath, **kw):
    """一条「照片行」——形状与 bucketPool.poolRows 的产物一致。"""
    rec = {"photoCode": "PH_%s" % relPath.replace("/", "_").replace(".", "_"),
           "relPath": relPath, "effYear": 2005, "quality": 0.8, "detScore": 0.9,
           "isConfirmed": 0, "takenAt": "2005-01-15T03:22:10Z", "fileSize": 32}
    rec.update(kw)
    return rec


def zipNames(zipPath):
    with zipfile.ZipFile(zipPath) as zf:
        return zf.namelist()


def zipText(zipPath, suffix):
    """读 zip 里某个文本成员（按后缀找，避免在测试里拼一遍目录名）。"""
    with zipfile.ZipFile(zipPath) as zf:
        for name in zf.namelist():
            if name.endswith(suffix):
                return zf.read(name).decode("utf-8")
    raise AssertionError("zip 里没有 %s：%s" % (suffix, zipPath))


# ---- 口径：SQL 文本可被断言（与 eraSampler.candidateSql 同一纪律） ----

def test_poolSqlExcludesDuplicateButKeepsMissing():
    """**排除重复件**，但**不排除** `isMissing` —— 后者要如实报数，不是静默滤掉。"""
    sql = bucketPool.poolSql(True)
    assert "p.isDuplicate = %s" in sql
    assert "f.isStranger = 0" in sql, "陌生人必须排除（他不属于这个人）"
    assert "f.delFlag = %s" in sql and "p.delFlag = %s" in sql
    # ⚠️ 只断言 **WHERE 子句**：`p.isMissing` 必须出现在 SELECT 列表里
    #    （打包层要拿它计数并写进 README），但**不许**出现在过滤条件里。
    where = sql.split("WHERE", 1)[1]
    assert "isMissing" not in where, "缺失原图不能在查询阶段被滤掉（口径 ④）"
    # 关掉排除重复件时，那条 WHERE 必须真的消失
    assert "isDuplicate" not in bucketPool.poolSql(False).split("WHERE", 1)[1]


def test_bucketKeyNormalize():
    """档键：空 -> NOYEAR；非法键**抛错而不是净化后照用**。"""
    assert bucketPool.normalizeKey("") == bucketPool.NO_BUCKET_KEY
    assert bucketPool.normalizeKey(None) == bucketPool.NO_BUCKET_KEY
    assert bucketPool.normalizeKey("2005-2009") == "2005-2009"
    assert bucketPool.timelineKeyOf(bucketPool.NO_BUCKET_KEY) == bucketPool.NO_BUCKET_TEXT
    assert bucketPool.timelineKeyOf("2005-2009") == "2005-2009"
    for bad in ("../x", "2005/2009", "中文档"):
        with pytest.raises(bucketPool.BucketPoolError):
            bucketPool.normalizeKey(bad)


# ---- 打包内核 ----

def test_bucketZipWritesOneFilePerName(sandbox_photo_root):
    """同名文件必须被去重成 `_2` —— 否则解压静默丢图（zip 本身还完全合法）。"""
    root = sandbox_photo_root
    rows = []
    for index, rel in enumerate(["a/IMG_1.jpg", "b/IMG_1.jpg", "c/IMG_2.jpg"]):
        makeOriginal(root, rel)
        rows.append(photoRow(rel, photoCode="PH_%d" % index))
    info = eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows, jobCode="ZP_T1")

    assert info["zipState"] == "READY"
    assert info["cached"] is False
    assert info["zipPhotoCount"] == 3
    assert info["zipMissingCount"] == 0
    names = zipNames(info["zipAbsPath"])
    assert len([n for n in names if n.endswith("/IMG_1.jpg")]) == 1
    assert len([n for n in names if n.endswith("/IMG_1_2.jpg")]) == 1
    assert len([n for n in names if n.endswith("/IMG_2.jpg")]) == 1
    # 自描述：manifest 与 README 必须在
    assert any(n.endswith("manifest.csv") for n in names)
    assert any(n.endswith("README.txt") for n in names)
    # 目录按有效拍摄年分（effYear=2005 -> 2005/）
    assert any("/2005/IMG_2.jpg" in n for n in names)
    # 机器可读元数据：判过期的唯一依据，且**不能被算成照片**
    meta = eraPack.readZipMeta(info["zipAbsPath"])
    assert meta["photoTotal"] == 3
    assert meta["photoCount"] == 3
    assert meta["bucketKey"] == BUCKET
    assert meta["excludeDuplicate"] is True
    assert eraPack.countZipPhotos(info["zipAbsPath"]) == 3
    # README 必须写明「全部照片」且指出界面张数可能更多（口径 ⑤）
    readme = zipText(info["zipAbsPath"], "README.txt")
    assert "全部照片" in readme
    assert "重复件" in readme


def test_bucketZipSkipsMissingAndReports(sandbox_photo_root):
    """原图不在磁盘上：**跳过 + 计数 + 写进 README/manifest**，不算失败。"""
    root = sandbox_photo_root
    makeOriginal(root, "a/ok.jpg")
    rows = [photoRow("a/ok.jpg", photoCode="PH_ok"),
            photoRow("a/gone.jpg", photoCode="PH_gone"),          # 故意不造文件
            photoRow("../evil.jpg", photoCode="PH_evil")]         # 越出照片根
    info = eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows, jobCode="ZP_T2")

    assert info["zipPhotoCount"] == 1
    assert info["zipMissingCount"] == 2
    reasons = sorted(one["reason"] for one in info["missing"])
    assert any("原图不在磁盘上" in one for one in reasons), reasons
    assert any("逃出照片根" in one for one in reasons), reasons
    readme = zipText(info["zipAbsPath"], "README.txt")
    assert "PH_gone" in readme and "PH_evil" in readme
    manifest = zipText(info["zipAbsPath"], "manifest.csv")
    assert manifest.startswith("\ufeff"), "manifest 必须带 BOM，否则 Excel 打开是乱码"


def test_bucketZipNoYearGoesToNoYearDir(sandbox_photo_root):
    """无拍摄年份的照片落 `_NOYEAR/`，而不是 `0/` 或 `None/`。"""
    root = sandbox_photo_root
    makeOriginal(root, "misc/h.jpg")
    rows = [photoRow("misc/h.jpg", effYear=0)]
    info = eraPack.ensureBucketZip("P_x", "张三", bucketPool.NO_BUCKET_KEY,
                                   bucketPool.NO_BUCKET_TEXT, rows, jobCode="ZP_T3")
    names = zipNames(info["zipAbsPath"])
    assert any("/%s/h.jpg" % eraPack.NOYEAR_DIR in n for n in names), names


def test_bucketZipIdempotentAndForce(sandbox_photo_root):
    """幂等：第二次是 `cached`；`force=True` 才重打。"""
    root = sandbox_photo_root
    makeOriginal(root, "a/one.jpg")
    rows = [photoRow("a/one.jpg")]
    first = eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows, jobCode="ZP_T4")
    second = eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows, jobCode="ZP_T5")
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["zipPhotoCount"] == 1, "幂等复用时张数要从 zip 中央目录现读"
    third = eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows,
                                    force=True, jobCode="ZP_T6")
    assert third["cached"] is False


def test_bucketZipProgressIsPerPhoto(sandbox_photo_root):
    """进度按**张数**上报（用户选定 47/171 这种口径），首尾必须是 0/N 与 N/N。"""
    root = sandbox_photo_root
    rows = []
    for index in range(3):
        rel = "a/p%d.jpg" % index
        makeOriginal(root, rel)
        rows.append(photoRow(rel, photoCode="PH_p%d" % index))
    seen = []
    eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows,
                            onProgress=lambda done, total: seen.append((done, total)),
                            jobCode="ZP_T7")
    assert seen[0] == (0, 3)
    assert seen[-1] == (3, 3)
    assert [one[0] for one in seen] == [0, 1, 2, 3]


def test_bucketZipAbortLeavesNoHalfProduct(sandbox_photo_root):
    """中途失败**绝不留下半截 zip**（也不留临时文件）—— 半截包能打开、能解压一部分。"""
    root = sandbox_photo_root
    rows = []
    for index in range(3):
        rel = "a/q%d.jpg" % index
        makeOriginal(root, rel)
        rows.append(photoRow(rel, photoCode="PH_q%d" % index))

    def boom(done, total):
        if done >= 1:
            raise eraPack.EraPackError("已请求停止")

    with pytest.raises(eraPack.EraPackError):
        eraPack.ensureBucketZip("P_x", "张三", BUCKET, BUCKET, rows,
                                onProgress=boom, jobCode="ZP_T8")
    target = eraPack.bucketZipAbsPath("P_x", BUCKET)
    assert not os.path.exists(target), "中断后不该留下产物"
    outDir = os.path.dirname(target)
    leftovers = [n for n in os.listdir(outDir)] if os.path.isdir(outDir) else []
    assert leftovers == [], "临时文件必须被 finally 清掉：%s" % leftovers


def test_bucketZipDiskSpaceCheck(sandbox_photo_root):
    """磁盘检查：要一个不可能满足的体积时**先失败**，而不是写到一半。"""
    paths.ensure_dirs()
    with pytest.raises(eraPack.EraPackError):
        eraPack.ensureDiskSpace(10 ** 18)
    ok = eraPack.ensureDiskSpace(1024)
    assert ok["free"] > 0 and ok["required"] >= 1024


def test_downloadBucketNameKeepsChinese():
    """下载名保留中文（人名 + 年代档），落盘名才是纯 ASCII。"""
    name = eraPack.downloadBucketName("张三", "2005-2009")
    assert name == "年代照片_张三_2005-2009.zip"
    rel = eraPack.bucketZipRelName("P_x", "2005-2009")
    assert rel == "era-zip/P_x__2005-2009.zip", rel
    assert rel.isascii()


# ============================================================
# 二、api 层（异步任务 + 下载）
# ============================================================

#: 判给 P_alpha 的照片（含一张「无拍摄年份」）
ALPHA_BUCKET_PHOTOS = ["PH_2013_01", "PH_2013_07", "PH_2016_03",
                       "PH_2020_11", "PH_2024_06", "PH_2019_04"]
ALPHA_NOYEAR_PHOTOS = ["PH_NODATE"]


@pytest.fixture(autouse=True)
def resetEraZipTask():
    """打包任务态是**模块级全局**，用例之间必须互相清干净。"""
    from api import eraZip as eraZipApi

    def _reset():
        eraZipApi._patchTask(
            running=False, jobCode=None, state="idle", stopEvent=None,
            personCode=None, personName="", bucketKey=None, bucketText="",
            total=0, processed=0, zipSize=0, zipPhotoCount=0, missing=0,
            cached=False, force=False, errMsg="",
            startedYMDHMS=None, finishedYMDHMS=None)

    _reset()
    yield
    _reset()


def assignFaces(photoCode, personCode, bucketKey=BUCKET, confirmed=1, stranger=0):
    """把某张照片里**全部**脸判给某人并显式写 shotBucket。

    ⚠️ 走 upsert + forceColumns：`normalizeDataSet` 会把 None 整条丢掉，
       而「空 shotBucket」正是本模块要测的那种行（它要落 NOYEAR 档）。
    """
    rows = sqliteCommon.query_pb_face("pb_face", photoCode=photoCode, mode="light")
    data = [{"faceCode": str(one["faceCode"]), "photoCode": photoCode,
             "personCode": personCode, "shotBucket": bucketKey,
             "isConfirmed": confirmed, "isStranger": stranger} for one in rows]
    if data:
        sqliteCommon.insertManyTableGeneral(
            "pb_face", data, conflictColumns=("faceCode",),
            updateColumns=("personCode", "shotBucket", "isConfirmed", "isStranger"),
            fillStandard=True,
            forceColumns=("personCode", "shotBucket"))


def seedAlpha(env, photos=None, bucketKey=BUCKET):
    for code in (photos or ALPHA_BUCKET_PHOTOS):
        assignFaces(code, "P_alpha", bucketKey=bucketKey)
    for code in ALPHA_NOYEAR_PHOTOS:
        assignFaces(code, "P_alpha", bucketKey="")


def writeOriginals(env, codes=None):
    """在临时 photo 目录里造出**真实文件**（zip 打包读的就是它们）。"""
    root = env["root"] / "photo"
    for code in (codes or env["seed"]["photoCodes"]):
        row = sqliteCommon.query_pb_photo("pb_photo", photoCode=code, mode="light")
        if not row:
            continue
        rel = str(row[0]["relPath"])
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8" + code.encode("utf-8") + b"\xff\xd9")
    return root


def photoFingerprint(root):
    """photo 目录的 (文件数, 总字节数) —— 原图只读红线的可执行判据。"""
    base = root / "photo"
    count = 0
    total = 0
    for dirPath, _dirNames, fileNames in os.walk(str(base)):
        for name in fileNames:
            count += 1
            total += os.path.getsize(os.path.join(dirPath, name))
    return count, total


def waitDone(client, jobCode, timeout=30.0):
    """轮询到终态（BUILDING 之外）。**真实等待**，因为后端是真线程。"""
    deadline = time.time() + timeout
    snap = None
    while time.time() < deadline:
        got = client.get("/api/era-zips/status/%s" % jobCode)
        assert got.status_code == 200, got.text
        snap = got.json()
        if str(snap.get("state")) != "BUILDING":
            return snap
        time.sleep(0.05)
    raise AssertionError("打包没在 %ss 内结束：%s" % (timeout, snap))


def test_listEraZipsCountsAndEstimate(api_env):
    """列档接口：张数（排除重复件）、体积预估、zip 现状、与时间轴对齐的档键。"""
    client = api_env["client"]
    seedAlpha(api_env)
    body = client.get("/api/persons/P_alpha/era-zips").json()

    assert body["ok"] is True
    assert body["displayName"] == "阿尔法"
    assert body["excludeDuplicate"] is True
    byKey = {one["bucketKey"]: one for one in body["buckets"]}
    assert BUCKET in byKey, byKey.keys()
    assert bucketPool.NO_BUCKET_KEY in byKey, byKey.keys()

    one = byKey[BUCKET]
    assert one["photoTotal"] == len(ALPHA_BUCKET_PHOTOS)
    assert one["totalBytes"] > 0
    assert one["zipExists"] is False
    assert one["stale"] is False
    assert one["downloadUrl"] is None
    assert one["timelineKey"] == BUCKET

    noyear = byKey[bucketPool.NO_BUCKET_KEY]
    assert noyear["timelineKey"] == bucketPool.NO_BUCKET_TEXT, "必须能对上时间轴的键"
    assert noyear["photoTotal"] == len(ALPHA_NOYEAR_PHOTOS)


def test_listEraZipsExcludesDuplicate(api_env):
    """界面张数含重复件、打包张数不含 —— 这个差必须由接口如实反映。"""
    client = api_env["client"]
    seedAlpha(api_env)
    before = client.get("/api/persons/P_alpha/era-zips").json()
    totalBefore = [one for one in before["buckets"]
                   if one["bucketKey"] == BUCKET][0]["photoTotal"]

    row = sqliteCommon.query_pb_photo("pb_photo", photoCode="PH_2020_11",
                                      mode="light")[0]
    sqliteCommon.update_pb_photo("pb_photo", int(row["recID"]), {"isDuplicate": 1})

    after = client.get("/api/persons/P_alpha/era-zips").json()
    totalAfter = [one for one in after["buckets"]
                  if one["bucketKey"] == BUCKET][0]["photoTotal"]
    assert totalAfter == totalBefore - 1


def test_packThenDownload(api_env):
    """端到端：起任务 → 轮询到 READY → 下载 → 检查 zip 内容。"""
    client = api_env["client"]
    seedAlpha(api_env)
    writeOriginals(api_env)

    got = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET,
                      json={"force": False})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["started"] is True and body["cached"] is False
    assert body["total"] == len(ALPHA_BUCKET_PHOTOS)
    assert body["statusUrl"].endswith(body["jobCode"])

    snap = waitDone(client, body["jobCode"])
    assert snap["state"] == "READY", snap
    assert snap["zipPhotoCount"] == len(ALPHA_BUCKET_PHOTOS)
    assert snap["missing"] == 0
    assert snap["percent"] == 100.0
    assert snap["zipSize"] > 0

    # 打完立刻出现在列表里（downloadUrl 才有值）
    listed = client.get("/api/persons/P_alpha/era-zips").json()
    one = [item for item in listed["buckets"] if item["bucketKey"] == BUCKET][0]
    assert one["zipExists"] is True
    assert one["zipPhotoCount"] == len(ALPHA_BUCKET_PHOTOS)
    assert one["stale"] is False
    assert one["downloadUrl"]

    got = client.get("/api/persons/P_alpha/era-zips/%s/download" % BUCKET)
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        names = zf.namelist()
        assert any(n.endswith("manifest.csv") for n in names)
        assert any(n.endswith("README.txt") for n in names)
        assert any(n.endswith(eraPack.META_NAME) for n in names)
        photos = [n for n in names
                  if not n.endswith("manifest.csv") and not n.endswith("README.txt")
                  and not n.endswith(eraPack.META_NAME) and not n.endswith("/")]
        assert len(photos) == len(ALPHA_BUCKET_PHOTOS), photos


def test_startIsIdempotentWhenReady(api_env):
    """已有产物时 POST 直接短路（`cached=True`，不起线程）——不重打 1 GB 的包。"""
    client = api_env["client"]
    seedAlpha(api_env)
    writeOriginals(api_env)
    first = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET,
                        json={"force": False}).json()
    waitDone(client, first["jobCode"])

    again = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET,
                        json={"force": False}).json()
    assert again["cached"] is True
    assert again["started"] is False
    assert again["jobCode"] is None
    assert again["downloadUrl"]


def test_downloadWithoutProductStartsTask(api_env):
    """没产物就下载：**不假装成功**，回 409 + 已起任务（前端据此切进度条）。"""
    client = api_env["client"]
    seedAlpha(api_env)
    writeOriginals(api_env)
    got = client.get("/api/persons/P_alpha/era-zips/%s/download" % BUCKET)
    assert got.status_code == 409, got.text
    assert got.json()["code"] == "TASK_STATE_ILLEGAL"

    # 任务确实在跑（不依赖 extra 字段：从列表接口读当前任务的 jobCode）
    listed = client.get("/api/persons/P_alpha/era-zips").json()
    running = listed["running"]
    assert running and running["jobCode"]
    snap = waitDone(client, running["jobCode"])
    assert snap["state"] == "READY"


def test_startWhenBusyReturns409(api_env):
    """全局单任务：运行中再点别的档 → 409，且提示里点名正在打哪个档。"""
    client = api_env["client"]
    from api import eraZip as eraZipApi
    eraZipApi._patchTask(running=True, jobCode="ZP_fake", state="BUILDING",
                         bucketKey=BUCKET, bucketText=BUCKET,
                         total=10, processed=4)
    got = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET,
                      json={"force": True})
    assert got.status_code == 409, got.text
    assert got.json()["code"] == "TASK_STATE_ILLEGAL"
    assert BUCKET in got.json()["message"]
    assert "4/10" in got.json()["message"]


def test_statusUnknownJobCodeIsNotFound(api_env):
    """轮询一个不是当前任务的 jobCode → 404（前端据此停止轮询）。"""
    client = api_env["client"]
    got = client.get("/api/era-zips/status/ZP_nope")
    assert got.status_code == 404
    assert got.json()["code"] == "NOT_FOUND"


def test_stopWithoutTaskIsHarmless(api_env):
    """没有任务时点停止：200 + stopped=False（不是错误）。"""
    got = api_env["client"].post("/api/era-zips/stop")
    assert got.status_code == 200
    assert got.json()["stopped"] is False


def test_emptyBucketIsRejected(api_env):
    """空档不打包：只会生成一个「只有 manifest/README」的 zip，而用户以为导出成功了。"""
    client = api_env["client"]
    got = client.post("/api/persons/P_alpha/era-zips/1999-2001", json={})
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"


def test_unknownPersonIs404(api_env):
    got = api_env["client"].get("/api/persons/P_nobody/era-zips")
    assert got.status_code == 404
    assert got.json()["code"] == "NOT_FOUND"


def test_illegalBucketKeyIs400(api_env):
    """档键非法 → 400（而不是「净化后照用」—— 那会打出**别人的**照片包）。"""
    got = api_env["client"].post("/api/persons/P_alpha/era-zips/..%2Fetc", json={})
    assert got.status_code in (400, 404), got.text
    assert got.json()["code"] in ("PARAM_INVALID", "NOT_FOUND")


def test_missingOriginalDoesNotLookStale(api_env):
    """⚠️ 原图缺失**不许**被判成「包过期」。

    这是本项目最容易做错的一处：包内文件数天然小于界面张数（缺的那几张没打进去），
    若拿「张数不等」当过期判据，界面就会**永远**提示重打 —— 而重打一百次
    结果一样。判据必须是包内 `_meta.json` 记的「打包时那一档有多少张」。
    """
    client = api_env["client"]
    seedAlpha(api_env)
    # 只造前 3 张的原图 -> 其余 3 张在库里但磁盘上没有
    writeOriginals(api_env, codes=ALPHA_BUCKET_PHOTOS[:3])

    body = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET, json={}).json()
    snap = waitDone(client, body["jobCode"])
    assert snap["state"] == "READY"
    assert snap["missing"] == 3, snap

    listed = client.get("/api/persons/P_alpha/era-zips").json()
    one = [item for item in listed["buckets"] if item["bucketKey"] == BUCKET][0]
    assert one["zipExists"] is True
    assert one["zipPhotoCount"] < one["photoTotal"], "这个用例必须真的少几张"
    assert one["stale"] is False, "原图缺失是常态，不是过期"


def test_staleWhenPhotoSetChanges(api_env):
    """打包之后照片集变了 → 必须提示重打（这才叫真正的过期）。"""
    client = api_env["client"]
    for code in ALPHA_BUCKET_PHOTOS[:3]:
        assignFaces(code, "P_alpha", bucketKey=BUCKET)
    writeOriginals(api_env)

    body = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET, json={}).json()
    waitDone(client, body["jobCode"])

    def bucketRow():
        listed = client.get("/api/persons/P_alpha/era-zips").json()
        return [item for item in listed["buckets"] if item["bucketKey"] == BUCKET][0]

    assert bucketRow()["stale"] is False

    # 再判进来三张（同一个档）—— 包里的照片集就过时了
    for code in ALPHA_BUCKET_PHOTOS[3:]:
        assignFaces(code, "P_alpha", bucketKey=BUCKET)
    row = bucketRow()
    assert row["photoTotal"] == len(ALPHA_BUCKET_PHOTOS)
    assert row["stale"] is True, row

    # force 重打之后又变回「新的」
    again = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET,
                        json={"force": True}).json()
    assert again["started"] is True
    waitDone(client, again["jobCode"])
    assert bucketRow()["stale"] is False


def test_photoDirUntouchedByPacking(api_env):
    """**打包前后 photo 目录指纹必须完全一致** —— 原图只读红线。"""
    client = api_env["client"]
    seedAlpha(api_env)
    writeOriginals(api_env)
    before = photoFingerprint(api_env["root"])

    body = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET, json={}).json()
    waitDone(client, body["jobCode"])
    client.get("/api/persons/P_alpha/era-zips/%s/download" % BUCKET)
    # 再打一次 NOYEAR 档，确认第二条路径同样不碰原图
    body2 = client.post("/api/persons/P_alpha/era-zips/%s" % bucketPool.NO_BUCKET_KEY,
                        json={}).json()
    waitDone(client, body2["jobCode"])

    assert photoFingerprint(api_env["root"]) == before


def test_exportsStaysInsideWritableDirs(api_env):
    """产物落 `exports\\era-zip\\`（白名单内），命名是纯 ASCII。"""
    client = api_env["client"]
    seedAlpha(api_env)
    writeOriginals(api_env)
    body = client.post("/api/persons/P_alpha/era-zips/%s" % BUCKET, json={}).json()
    waitDone(client, body["jobCode"])

    rel = eraPack.bucketZipRelName("P_alpha", BUCKET)
    assert rel.startswith(basicSettings.ERA_ZIP_SUBDIR + "/")
    target = eraPack.bucketZipAbsPath("P_alpha", BUCKET)
    exports = os.path.abspath(paths.exports_dir())
    assert os.path.abspath(target).startswith(exports + os.sep)
    assert os.path.isfile(target)

    usage = eraPack.exportsUsage()
    assert usage["bytes"] > 0 and usage["files"] >= 1
    # 列表接口带上占用（只报数，不返回本机路径）
    listed = client.get("/api/persons/P_alpha/era-zips").json()
    assert listed["exports"]["bytes"] == usage["bytes"]
    assert "dir" not in listed["exports"]


def test_openapiHasNoExportOrDelete(api_env):
    """项目级不变量（DR-17 / DR-51）：新模块的路径不含 export、不暴露 DELETE。"""
    pathsOf = api_env["client"].get("/openapi.json").json()["paths"]
    assert any("era-zips" in one for one in pathsOf), "新路由没注册上"
    for path, methods in pathsOf.items():
        assert "export" not in path.lower(), "出现导出路径: %s" % path
        assert "delete" not in methods, "%s 暴露了 DELETE" % path


if __name__ == "__main__":
    print("test_era_bucket_pack: 请用 pytest 运行")
