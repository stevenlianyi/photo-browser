#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_era_sample.py
#Description: 修正步骤 R11 接口层单测 —— 年代抽样（DR-45 ~ DR-50）
#
# 覆盖 plan/年代抽样规划.md 第九节的接口侧验收 + 第七节的响应契约。
# 库与构造数据来自 conftest 的 api_env（9 张照片 / 3 个人 / 多脸照片 / 重复件 /
# 无日期照片），本文件只额外做两件事：
#   ① 把若干张照片里的脸判给 P_alpha（否则「这个人的年代队列」无从谈起）
#   ② 在 disk 上把原图**真的造出来**（zip 打包要读真实文件）
#
# ⚠️ 原图一律造在临时库里，**绝不碰 d:\PhotoLib**（conftest 的会话闸会判失败）。

import io
import os
import zipfile

import pytest

from common import paths as paths
from config import basicSettings as basicSettings
from database.auto_generated import sqliteCommon as sqliteCommon

#: 判给 P_alpha 的照片（覆盖 2013 / 2016 / 2019 / 2020 / 2024 五年）
ALPHA_PHOTOS = ["PH_2013_01", "PH_2013_07", "PH_2016_03", "PH_2020_11",
                "PH_2024_06", "PH_2019_04", "PH_NODATE"]
#: 这五年（PH_NODATE 无年份，默认不进队列）
ALPHA_YEARS = ["2013", "2016", "2019", "2020", "2024"]


# ============================================================
# 装置：给照片判人 + 把原图造出来
# ============================================================

def assignPhoto(personCode, photoCode, confirmed=1):
    """把某张照片里**全部**脸判给这个人（含 isConfirmed）。

    ⚠️ 走 upsert 而不是 update_pb_face：normalizeDataSet 会把 None 整条丢掉，
       而 personCode 正是「要能写成 NULL」的字段之一（与 assigner._patchFace
       同一个坑，见 rebucket.py 纪律 ②）。
    """
    rows = sqliteCommon.query_pb_face("pb_face", photoCode=photoCode, mode="light")
    data = [{"faceCode": str(one["faceCode"]), "photoCode": photoCode,
             "personCode": personCode, "isConfirmed": confirmed} for one in rows]
    if data:
        sqliteCommon.insertManyTableGeneral(
            "pb_face", data, conflictColumns=("faceCode",),
            updateColumns=("personCode", "isConfirmed"), fillStandard=True,
            forceColumns=("personCode",))


def addFaceTo(photoCode, personCode, faceCode, confirmed=1):
    """给一张**本来没有脸**的照片补一张脸（api_env 里 PH_DUP / PH_2024_06b 就是这样）。"""
    sqliteCommon.insertManyTableGeneral(
        "pb_face", [{"faceCode": faceCode, "photoCode": photoCode,
                     "personCode": personCode, "isConfirmed": confirmed,
                     "isStranger": 0, "shotBucket": "2000-2004"}],
        conflictColumns=("faceCode",), fillStandard=True,
        forceColumns=("personCode", "shotBucket"))


def seedAlpha(photos=None, confirmed=1):
    for code in (photos or ALPHA_PHOTOS):
        assignPhoto("P_alpha", code, confirmed=confirmed)


def writeOriginals(env, codes=None):
    """在临时 photo 目录里造出真实文件（zip 打包读的就是它们）。"""
    root = env["root"] / "photo"
    for code in (codes or env["seed"]["photoCodes"]):
        rel = str(sqliteCommon.query_pb_photo(
            "pb_photo", photoCode=code, mode="light")[0]["relPath"])
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8" + code.encode("utf-8") + b"\xff\xd9")
    return root


def setOverride(photoCode, year):
    row = sqliteCommon.query_pb_photo("pb_photo", photoCode=photoCode, mode="light")[0]
    sqliteCommon.update_pb_photo("pb_photo", int(row["recID"]),
                                 {"shotYearOverride": year})


def markStranger(faceCode):
    row = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)[0]
    sqliteCommon.update_pb_face("pb_face", int(row["recID"]), {"isStranger": 1})


def create(client, personCode="P_alpha", **body):
    got = client.post("/api/persons/%s/era-sample" % personCode, json=body)
    assert got.status_code == 200, got.text
    return got.json()


def yearList(body):
    return [one["yearLabel"] for one in body["items"]]


# ============================================================
# 一、生成 / 回读（同一份读路径）
# ============================================================

def test_create_returnsQueuePerYear(api_env):
    client = api_env["client"]
    seedAlpha()
    body = create(client, seed="api-seed")

    assert body["ok"] is True
    batch = body["batch"]
    assert batch["personCode"] == "P_alpha"
    assert batch["displayName"] == "阿尔法"
    # 2013 那张是**合影**（2 张脸）-> 算一次候选（口径 ②）
    assert batch["candidatePhotoCount"] == 7
    assert batch["candidateYearCount"] == 5
    assert batch["photoCount"] == 5
    assert yearList(body) == ALPHA_YEARS
    assert [one["pickOrder"] for one in body["items"]] == [1, 2, 3, 4, 5]
    assert batch["zipState"] == "NONE"


def test_create_andGet_areIdentical(api_env):
    """「刚生成的」与「刷新后重新读的」必须是同一份结构。

    两条路径各拼一份响应迟早漂移，而用户只在刷新后才发现
    （最典型：刚生成的有 thumbUrl、刷新后没有）。
    """
    client = api_env["client"]
    seedAlpha()
    created = create(client, seed="api-seed")
    reread = client.get("/api/era-samples/%s" % created["batch"]["batchCode"])
    assert reread.status_code == 200, reread.text
    assert reread.json() == created


def test_itemShape_hasProbabilityAndUrls(api_env):
    client = api_env["client"]
    seedAlpha()
    body = create(client, seed="api-seed")
    card = body["items"][0]
    assert card["thumbUrl"].startswith("/api/thumb/PH_")
    assert card["originalUrl"] == "/api/original/%s" % card["photoCode"]
    assert card["yearText"] == card["yearLabel"]
    # 2013 年被抽中的那张：那一年有两张候选（PH_2013_01 与 PH_2013_07）
    assert card["candidateCount"] == 2
    assert 0.0 < card["probability"] < 1.0
    assert card["weight"] == pytest.approx(card["probability"] * card["weightSum"])


def test_create_noCandidate_isOkWithEmptyQueue(api_env):
    """「他名下没有带年份的照片」是要如实告诉用户的事实，不是错误。"""
    body = create(api_env["client"], personCode="P_beta", seed="s")
    assert body["batch"]["photoCount"] == 0
    assert body["items"] == []


def test_create_unknownPerson_is404(api_env):
    got = api_env["client"].post("/api/persons/P_nobody/era-sample", json={})
    assert got.status_code == 404, got.text
    assert got.json()["code"] == "NOT_FOUND"


@pytest.mark.parametrize("body", [
    {"mode": "random"},                       # 抽法不在枚举里
    {"grain": "bucket"},                      # DR-45 的预留位，**未实现**
    {"yearFrom": 2010, "yearTo": 1990},       # 区间反了
    {"yearFrom": "很久以前"},                  # 不是年份
])
def test_create_badParams_are400(api_env, body):
    got = api_env["client"].post("/api/persons/P_alpha/era-sample", json=body)
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"


# ============================================================
# 二、口径（与 §二 的检查表逐条对应）
# ============================================================

def test_usesEffectiveShotYear(api_env):
    """DR-42：shotYearOverride 优先，且与 Tab1 时间轴同源。"""
    seedAlpha(photos=["PH_2016_03"])
    setOverride("PH_2016_03", 1958)
    body = create(api_env["client"], seed="s")
    assert yearList(body) == ["1958"]
    assert body["items"][0]["shotYear"] == 1958


def test_excludesStrangerFaces(api_env):
    seedAlpha()
    faces = sqliteCommon.query_pb_face("pb_face", photoCode="PH_2019_04",
                                       mode="light")
    markStranger(str(faces[0]["faceCode"]))
    body = create(api_env["client"], seed="s")
    assert "2019" not in yearList(body)


def test_excludesDuplicatePhotosByDefault(api_env):
    """PH_DUP 是重复件（dupOfPhotoCode=PH_2013_01）—— 默认不抽。"""
    seedAlpha()
    addFaceTo("PH_DUP", "P_alpha", "FC_DUP_ONLY", confirmed=1)
    strict = create(api_env["client"], seed="s")
    assert strict["batch"]["candidatePhotoCount"] == 7
    loose = create(api_env["client"], seed="s", excludeDuplicate=False)
    assert loose["batch"]["candidatePhotoCount"] == 8


def test_onlyConfirmedNarrowsCandidates(api_env):
    seedAlpha()
    # ⚠️ 顺序不能反：seedAlpha 会把 ALPHA_PHOTOS 全部判给 P_alpha（confirmed=1），
    #    所以「机器认的那张」必须在它**之后**覆盖回去。
    assignPhoto("P_alpha", "PH_2019_04", confirmed=0)
    auto = create(api_env["client"], seed="s")
    assert "2019" in yearList(auto)
    manual = create(api_env["client"], seed="s", onlyConfirmed=True)
    assert "2019" not in yearList(manual)


def test_includeNoYear_goesLast(api_env):
    seedAlpha()
    body = create(api_env["client"], seed="s", includeNoYear=True)
    assert yearList(body) == ALPHA_YEARS + ["NOYEAR"]
    last = body["items"][-1]
    assert last["shotYear"] is None
    assert last["yearText"] == "(无拍摄年份)"


def test_yearRangeNarrowsQueueButNotCandidateYears(api_env):
    seedAlpha()
    body = create(api_env["client"], seed="s", yearFrom=2015, yearTo=2021)
    assert yearList(body) == ["2016", "2019", "2020"]
    # 候选年份数**不受区间影响** —— 否则用户会以为他的照片变少了
    assert body["batch"]["candidateYearCount"] == 5


def test_generate_isReadOnly(api_env, api_rows):
    """验收第 14 条：抽样不碰识别事实、不写 pb_review_log。"""
    seedAlpha()
    before = [(str(r["faceCode"]), str(r["personCode"] or ""),
               int(r["isConfirmed"]), int(r["isStranger"]))
              for r in sqliteCommon.query_pb_face("pb_face", delFlag="*")]
    create(api_env["client"], seed="s")
    after = [(str(r["faceCode"]), str(r["personCode"] or ""),
              int(r["isConfirmed"]), int(r["isStranger"]))
             for r in sqliteCommon.query_pb_face("pb_face", delFlag="*")]
    assert after == before
    assert api_rows.count("pb_review_log") == 0


# ============================================================
# 三、历史批次 / 删除
# ============================================================

def test_listBatches_newestFirst_andNoDeleteEndpoint(api_env):
    client = api_env["client"]
    seedAlpha()
    first = create(client, seed="a")["batch"]["batchCode"]
    second = create(client, seed="b")["batch"]["batchCode"]

    listing = client.get("/api/persons/P_alpha/era-samples")
    assert listing.status_code == 200, listing.text
    codes = [one["batchCode"] for one in listing.json()["items"]]
    assert codes == [second, first]                 # 最近生成的最前（recID DESC）

    # ⚠️ **全站没有 DELETE 端点**是被 `test_api_smoke.test_openapiHasNoExportOrDelete`
    #    扫 OpenAPI 守着的项目级不变量（DR-17 / DR-19）。批次也不例外：
    #    历史只增不删（一批 = 年份数行纯元数据，界面只列最近 20 批）。
    assert client.delete("/api/era-samples/%s" % first).status_code in (404, 405)
    assert client.get("/api/era-samples/%s" % first).status_code == 200


def test_getUnknownBatch_is404(api_env):
    assert api_env["client"].get("/api/era-samples/ES_nope").status_code == 404


def test_create_missingTables_is500WithMigrateHint(api_env):
    """老库没跑迁移 -> **500 DB_ERROR + 那条命令**，不是 400 PARAM_INVALID。

    ⚠️ 错误分类是**给用户看的下一步**：「参数写错了」他改输入，
      「库还没迁移」他要去跑一条命令。混成 400 会让人盯着一个
      完全正常的请求体永远猜不到原因（E2E 实测撞到过）。
    """
    sqliteCommon.drop_pb_era_sample("pb_era_sample")
    sqliteCommon.drop_pb_era_sample_item("pb_era_sample_item")
    got = api_env["client"].post("/api/persons/P_alpha/era-sample", json={})
    assert got.status_code == 500, got.text
    body = got.json()
    assert body["code"] == "DB_ERROR"
    assert "--migrate" in body["message"]


# ============================================================
# 四、zip
# ============================================================

def test_zip_build_thenDownload(api_env, api_rows):
    client = api_env["client"]
    seedAlpha()
    writeOriginals(api_env)
    code = create(client, seed="zip-seed")["batch"]["batchCode"]

    built = client.post("/api/era-samples/%s/zip" % code)
    assert built.status_code == 200, built.text
    info = built.json()
    assert info["zipState"] == "READY"
    assert info["zipPhotoCount"] == 5
    assert info["zipMissingCount"] == 0
    assert info["cached"] is False
    assert info["fileName"].startswith("年代抽样_阿尔法_")
    # 批次行被回写了状态（zip 元数据是**批次级**事实，不冗余到明细）
    assert api_rows.count("pb_era_sample") == 1

    got = client.get("/api/era-samples/%s/zip" % code)
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/zip"
    # 中文文件名按 RFC 5987 承载（否则旧浏览器/Electron 里会乱码）
    assert "filename*=utf-8''" in got.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        names = zf.namelist()
        assert zf.read(names[0])                    # 归档完整、能读
    # zip 是**自描述**的：照片 + 「为什么是这几张」的 manifest + 参数 README
    assert any(name.endswith("/manifest.csv") for name in names)
    assert any(name.endswith("/README.txt") for name in names)
    assert sum(1 for name in names if name.lower().endswith(".jpg")) == 5
    assert any("/2013/" in name for name in names)


def test_zip_isIdempotent_andForceRepacks(api_env):
    client = api_env["client"]
    seedAlpha()
    writeOriginals(api_env)
    code = create(client, seed="s")["batch"]["batchCode"]
    assert client.post("/api/era-samples/%s/zip" % code).json()["cached"] is False
    again = client.post("/api/era-samples/%s/zip" % code).json()
    assert again["cached"] is True and again["zipPhotoCount"] == 5
    forced = client.post("/api/era-samples/%s/zip" % code,
                         json={"force": True}).json()
    assert forced["cached"] is False


def test_zip_getAutoBuildsWhenMissing(api_env):
    """还没打过就点下载 -> **先自动打**（而不是 404）。"""
    client = api_env["client"]
    seedAlpha()
    writeOriginals(api_env)
    code = create(client, seed="s")["batch"]["batchCode"]
    got = client.get("/api/era-samples/%s/zip" % code)
    assert got.status_code == 200, got.text
    assert got.content[:2] == b"PK"


def test_zip_countsMissingOriginals(api_env):
    """原图不在磁盘上 -> **跳过 + 计数**，不许整批失败、也不许静默丢。"""
    client = api_env["client"]
    seedAlpha()
    root = writeOriginals(api_env)
    code = create(client, seed="s")["batch"]["batchCode"]
    picked = sqliteCommon.query_pb_era_sample_item(
        "pb_era_sample_item", batchCode=code, orderBy="pickOrder")
    victim = picked[0]
    rel = str(sqliteCommon.query_pb_photo(
        "pb_photo", photoCode=str(victim["photoCode"]), mode="light")[0]["relPath"])
    root.joinpath(*rel.split("/")).unlink()

    info = client.post("/api/era-samples/%s/zip" % code).json()
    assert info["zipState"] == "READY"
    assert info["zipMissingCount"] == 1
    assert info["zipPhotoCount"] == 4


def test_zip_emptyBatch_is400(api_env):
    body = create(api_env["client"], personCode="P_beta", seed="s")
    got = api_env["client"].post(
        "/api/era-samples/%s/zip" % body["batch"]["batchCode"])
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"


def test_zip_unknownBatch_is404(api_env):
    assert api_env["client"].post("/api/era-samples/ES_nope/zip").status_code == 404


def test_zip_neverTouchesPhotoDir(api_env, tmp_path):
    """DR-48 红线：产物落 `exports\\era-sample\\`，`photo\\` 一个字节都不动。"""
    client = api_env["client"]
    seedAlpha()
    photoDir = writeOriginals(api_env)
    code = create(client, seed="s")["batch"]["batchCode"]
    before = _snapshot(photoDir)

    client.post("/api/era-samples/%s/zip" % code)

    assert _snapshot(photoDir) == before
    # 产物落 exports（**不是 photo**）；路径走 paths 的唯一出口，别在测试里硬拼
    exports = os.path.join(paths.exports_dir(), basicSettings.ERA_SAMPLE_ZIP_SUBDIR)
    assert os.path.isfile(os.path.join(exports, "%s.zip" % code))


def _snapshot(directory):
    out = {}
    for root, _dirs, files in os.walk(str(directory)):
        for name in files:
            path = os.path.join(root, name)
            out[os.path.relpath(path, str(directory))] = (
                os.path.getsize(path), int(os.path.getmtime(path)))
    return out
