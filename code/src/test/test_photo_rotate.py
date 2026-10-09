#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_photo_rotate.py
#Description: DR-43 照片人工旋转接口测试 —— 字段/迁移 / 透出面 / 红线（原图与识别数据零改动）
#
# 这一组盯住的是「看起来对、其实串了」的几类静默错
# ------------------------------------------------
#   · **透出面漏一处**：接口 200、结构齐全，只有 rotateDeg 缺一个 ——
#     「照片流里转了、地点详情里没转」。所以这里逐个接口核对。
#   · **重扫把角度洗回 0**：只要有人"顺手"把 rotateDeg 补进
#     `runner._META_FULL_COLUMNS`（那份 forceColumns/DO UPDATE 白名单），
#     重扫一次所有用户的旋转全部归零，而且不报任何错。
#   · **顺手改了识别数据**：旋转是纯显示属性，bbox / 归属 / 质心 / faceCount
#     一个字都不该动。这里逐字节比对（质心是 BLOB）。
#   · **顺手落一条 pb_review_log**：那条链只装归属事实。
#
# 全部跑在 pytest 的临时库上（api_env），**绝不动 d:\PhotoLib 正式库**。

import pytest

PHOTO = "PH_2013_01"
OTHER = "PH_DUP"                       # 同 placeName(北京) 的重复照片，用于对比接口
FACES = ["FC_PH_2013_01_0", "FC_PH_2013_01_1"]
PERSON = "P_alpha"


@pytest.fixture
def confirmed(api_env):
    """把 PHOTO 的两张脸人工确认给 P_alpha —— 这样才有质心（BLOB）可供比对。"""
    from processor.review import assigner as assigner

    for code in FACES:
        assigner.confirm(code, PERSON)
    return api_env


@pytest.fixture
def placeEnv(api_env, api_rows):
    """建好地点字典，并返回「北京」的 placeCode（给第 6 处 SELECT 用）。"""
    client = api_env["client"]
    client.post("/api/places/rebuild")
    for one in client.get("/api/places").json()["items"]:
        if one["placeName"] == "北京":
            return {"photoCode": PHOTO, "placeCode": one["placeCode"]}
    raise AssertionError("夹具里应该有「北京」这个地点")


# ============================================================
# 一、字段与迁移
# ============================================================

def test_rotateDegColumnHasExpectedShape(api_env):
    """列存在、类型 INTEGER、NOT NULL、默认 0（DR-43 的迁移产物）。"""
    from database.auto_generated import sqliteCommon

    got = {row["name"]: row for row in sqliteCommon.tableInfo("pb_photo")}
    assert "rotateDeg" in got, "pb_photo 缺 rotateDeg（忘了重跑生成器 / build_db --migrate？）"
    column = got["rotateDeg"]
    # SMALLINT 在 SQLite 里归到 INTEGER 亲和性（build_db.migrate 的比对口径）
    assert column["type"].upper() == "INTEGER"
    assert int(column["notnull"]) == 1
    assert str(column["dflt_value"]) == "0"


def test_allRowsStartAtZero(api_env, api_rows):
    """加列**不丢数据**，且存量行的 rotateDeg 全是 0（未修正）。"""
    assert api_rows.count("pb_photo", "rotateDeg <> 0") == 0
    assert api_rows.count("pb_photo") == len(api_env["seed"]["photoCodes"])


# ============================================================
# 二、写接口：落库 / 幂等 / 非法值
# ============================================================

def test_rotateWritesAngle(confirmed, api_rows):
    got = confirmed["client"].post("/api/photos/%s/rotate" % PHOTO,
                                   json={"rotateDeg": 90})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is True and body["changed"] is True
    assert body["rotateDeg"] == 90 and body["previous"] == 0
    assert int(api_rows.photo(PHOTO)["rotateDeg"]) == 90
    # ⚠️ 只改这一列：path 与原值都还在（不是"重建了一行"）
    assert api_rows.photo(PHOTO)["relPath"] == "2013/01/a.jpg"


def test_rotateAcceptsStringAngle(confirmed, api_rows):
    """`"180"` 是常见的传输形态，不该被当成非法值。"""
    got = confirmed["client"].post("/api/photos/%s/rotate" % PHOTO,
                                   json={"rotateDeg": "180"})
    assert got.status_code == 200, got.text
    assert got.json()["rotateDeg"] == 180


@pytest.mark.parametrize("bad", [45, -90, 360, "abc", None, True])
def test_rotateRejectsIllegalAngles(confirmed, api_rows, bad):
    """只支持 0/90/180/270，其它一律 **400**（不是 422，也不静默取模）。"""
    got = confirmed["client"].post("/api/photos/%s/rotate" % PHOTO,
                                   json={"rotateDeg": bad})
    assert got.status_code == 400, "%r 应该被拒: %s" % (bad, got.text)
    assert got.json()["code"] == "PARAM_INVALID"
    assert api_rows.count("pb_photo", "rotateDeg <> 0") == 0


def test_rotateRejectsMissingBody(confirmed, api_rows):
    got = confirmed["client"].post("/api/photos/%s/rotate" % PHOTO)
    assert got.status_code == 400, got.text
    assert api_rows.count("pb_photo", "rotateDeg <> 0") == 0


def test_rotateIsIdempotent(confirmed, api_rows):
    client = confirmed["client"]
    assert client.post("/api/photos/%s/rotate" % PHOTO,
                       json={"rotateDeg": 270}).json()["changed"] is True
    again = client.post("/api/photos/%s/rotate" % PHOTO, json={"rotateDeg": 270}).json()
    assert again["changed"] is False and again["rotateDeg"] == 270
    assert int(api_rows.photo(PHOTO)["rotateDeg"]) == 270


def test_rotateOnUnknownPhotoIs404(api_env):
    got = api_env["client"].post("/api/photos/PH_NOT_EXIST/rotate",
                                 json={"rotateDeg": 90})
    assert got.status_code == 404, got.text


def test_resetRotateGoesBackToZero(confirmed, api_rows):
    client = confirmed["client"]
    client.post("/api/photos/%s/rotate" % PHOTO, json={"rotateDeg": 90})
    got = client.post("/api/photos/%s/rotate-reset" % PHOTO)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["rotateDeg"] == 0 and body["previous"] == 90 and body["changed"] is True
    assert int(api_rows.photo(PHOTO)["rotateDeg"]) == 0
    # 幂等：已经是 0 时再重置不报错、也不改
    assert client.post("/api/photos/%s/rotate-reset" % PHOTO).json()["changed"] is False


# ============================================================
# 三、透出面：6 处一个都不能漏
# ============================================================

def test_rotateDegExposedEverywhere(confirmed, api_rows, placeEnv):
    client = confirmed["client"]
    client.post("/api/photos/%s/rotate" % PHOTO, json={"rotateDeg": 90})

    detail = client.get("/api/photos/%s" % PHOTO).json()
    assert detail["rotateDeg"] == 90

    listed = client.get("/api/photos", params={"size": 50}).json()
    byCode = {one["photoCode"]: one for one in listed["items"]}
    assert byCode[PHOTO]["rotateDeg"] == 90
    # 别的照片**不受影响**（"翻页串角度"在接口层的形态）
    assert byCode[OTHER]["rotateDeg"] == 0

    # 第 6 处：地点详情（R5 新增的那条 SELECT）
    placePhotos = client.get("/api/places/%s/photos" % placeEnv["placeCode"],
                             params={"size": 50}).json()
    photos = placePhotos["items"]
    assert photos, "北京的 photos 不该为空"
    assert {one["photoCode"]: one["rotateDeg"] for one in photos}[PHOTO] == 90

    # 重复对比：两侧各自带自己的角度
    compare = client.get("/api/duplicates/compare",
                         params={"photoCode": PHOTO, "otherCode": OTHER}).json()
    assert compare["left"]["rotateDeg"] == 90
    assert compare["right"]["rotateDeg"] == 0


def test_placeCoverCarriesRotateDeg(confirmed, placeEnv):
    """地点卡片的封面角度（PlacesView 的 4:3 封面要用它）。"""
    client = confirmed["client"]
    # 让"北京"的封面正好是 PHOTO：它是该地点 takenAt 最新的那张
    client.post("/api/photos/%s/rotate" % PHOTO, json={"rotateDeg": 270})
    # ⚠️ 必须 groupByNameZh=1：封面/人数/年份集合只在那条**展示层**路径上补
    body = client.get("/api/places", params={"groupByNameZh": 1}).json()
    beijing = next(one for one in body["items"] if one["placeName"] == "北京")
    assert beijing["coverPhotoCode"]
    if beijing["coverPhotoCode"] == PHOTO:
        assert beijing["coverRotateDeg"] == 270
    else:
        assert beijing["coverRotateDeg"] == 0


def test_pendingQueueCarriesRotateDeg(api_env):
    """待确认队列的条目要带来源照片的角度（ReviewView 大图 + 人脸框一起转）。

    ⚠️ 用一张**还有待确认的脸**的照片（PH_2016_03 的 2 张脸没被归属过）——
       拿已确认的照片去查待确认队列只会得到空列表，那会让这条用例变成
       「断言一个空集合」，看不出接口到底有没有透出角度。
    """
    client = api_env["client"]
    target = "PH_2016_03"
    client.post("/api/photos/%s/rotate" % target, json={"rotateDeg": 90})
    items = client.get("/api/review/pending", params={"size": 50}).json()["items"]
    mine = [one for one in items if one["photoCode"] == target]
    assert mine, "PH_2016_03 的两张脸应该还在待确认队列里"
    assert all(one["rotateDeg"] == 90 for one in mine)


# ============================================================
# 四、红线：不落日志 / 不动识别数据 / 不动原图
# ============================================================

def test_rotateWritesNoReviewLog(confirmed, api_rows):
    """旋转**不是归属纠错** —— pb_review_log 一行都不该多。

    ⚠️ 与 DR-42 的年代修正相反（那个落 BUCKET_FIX，因为它动了归属排障链上的
       事实）。把旋转塞进那条链就是往"这张脸当初怎么被认成这个人的"里掺沙子。
    """
    before = api_rows.count("pb_review_log")
    confirmed["client"].post("/api/photos/%s/rotate" % PHOTO,
                             json={"rotateDeg": 90})
    assert api_rows.count("pb_review_log") == before


def test_rotateChangesNoRecognitionData(confirmed, api_rows):
    """bbox / 归属 / isConfirmed / 质心 BLOB / faceCount —— 操作前后**逐字节相同**。"""
    facesBefore = {code: api_rows.face(code) for code in FACES}
    centroidsBefore = api_rows.centroids(PERSON)
    assert centroidsBefore, "夹具没建出质心，这条用例就没意义了"
    countsBefore = api_rows.photo(PHOTO)["faceCount"]

    confirmed["client"].post("/api/photos/%s/rotate" % PHOTO,
                             json={"rotateDeg": 180})

    for code in FACES:
        now, old = api_rows.face(code), facesBefore[code]
        assert now["bbox"] == old["bbox"]
        assert now["personCode"] == old["personCode"] == PERSON
        assert int(now["isConfirmed"]) == int(old["isConfirmed"]) == 1
        assert now["shotBucket"] == old["shotBucket"]

    nowCentroids = api_rows.centroids(PERSON)
    assert [(r["bucketKey"], r["centroid"]) for r in nowCentroids] == \
           [(r["bucketKey"], r["centroid"]) for r in centroidsBefore], \
           "质心 BLOB 被旋转动了（旋转不该影响任何识别数据）"
    assert api_rows.photo(PHOTO)["faceCount"] == countsBefore


# ============================================================
# 五、重扫不得冲掉人工旋转（DR-43 的硬承诺）
# ============================================================

def test_metaWhitelistsExcludeRotateDeg():
    """`rotateDeg` 必须**不在**重扫的整列替换白名单里。

    这是「重扫不覆盖用户覆盖值」的全部实现 —— 正因为不加，才安全。
    哪天有人"顺手补全列"，重扫一次会把所有用户的旋转全部归零，且表里看不出异常。
    """
    from processor.scanner import runner as scanRunner

    assert "rotateDeg" not in scanRunner._META_FULL_COLUMNS
    # 三个补丁桶同理（它们也走 upsert 的 updateColumns 白名单）
    assert "rotateDeg" not in scanRunner._PATCH_COLUMNS
    assert "rotateDeg" not in scanRunner._MOVE_COLUMNS
    # `_metaColumns` 只用到 info 参数（不碰 self），所以可以直接拿类调
    picked = scanRunner.ScanRunner._metaColumns(None, {"rotateDeg": 90})
    assert "rotateDeg" not in picked


def test_rescanUpsertKeepsRotateDeg(confirmed, api_rows):
    """真跑一次重扫那套 upsert（updateColumns + forceColumns 全白名单）。"""
    from database.auto_generated import sqliteCommon
    from processor.scanner import runner as scanRunner

    client = confirmed["client"]
    client.post("/api/photos/%s/rotate" % PHOTO, json={"rotateDeg": 270})
    assert int(api_rows.photo(PHOTO)["rotateDeg"]) == 270

    row = api_rows.photo(PHOTO)
    payload = {name: row.get(name) for name in scanRunner._META_FULL_COLUMNS}
    payload.update({"photoCode": PHOTO, "relPath": row["relPath"],
                    "relPathHash": row["relPathHash"], "fileHash": row["fileHash"],
                    "fileSize": int(row["fileSize"]),
                    "takenAt": row["takenAt"], "shotYear": row["shotYear"]})
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_photo", [payload], conflictColumns=("photoCode",),
        updateColumns=list(scanRunner._META_FULL_COLUMNS),
        fillStandard=True, forceColumns=list(scanRunner._META_FULL_COLUMNS))
    assert rtn is not None and rtn != -2

    assert int(api_rows.photo(PHOTO)["rotateDeg"]) == 270, \
        "重扫把用户旋转洗掉了 —— rotateDeg 被塞进了重扫白名单"
