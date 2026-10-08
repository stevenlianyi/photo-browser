#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_photo_time_fix.py
#Description: DR-42 照片年代修正接口测试 —— override 口径 / 三步联动 / 幂等 / 撤销 / 重扫不覆盖
#
# 这一组盯住的是「静默错」而不是「跑不通」
# ----------------------------------------
#   桶键不是照片上的列，而是 f(有效拍摄年, 该人出生年) 现算的。所以「改了年份」
#   与「桶跟着变」之间隔着三步（写 override -> rebucket -> recomputePerson），
#   任意一步漏掉或顺序做反，现象都是「接口 200、数字看起来也对、匹配率悄悄掉了」。
#   因此每个断言都落在**库里的列**上（api_rows 夹具直查，绕开 api 自证）：
#     · pb_photo.shotYearOverride       —— 写进去了没有、恢复自动是不是真的 NULL
#     · pb_face.shotBucket              —— 有没有按新年代重刷
#     · pb_person_centroid.bucketKey    —— 旧桶有没有被清掉、新桶有没有建起来
#     · pb_review_log                   —— 留痕（可撤销 + 能在操作历史里查到）
#
# 全部跑在 pytest 的临时库上（api_env），**绝不动 d:\PhotoLib 正式库**。

import pytest

from engine.match import bucket as bucket

#: 目标照片：2013 年拍的、两张脸都属于 P_alpha
PHOTO = "PH_2013_01"
FACES = ["FC_PH_2013_01_0", "FC_PH_2013_01_1"]
PERSON = "P_alpha"
BIRTH = "1985-03-07"          # api_env 造的 P_alpha 生日
EXIF_YEAR = 2013              # 扫描器读到的年份（翻拍时间）
FIX_YEAR = 2000               # 人工修正后的真实年代


def _bucketsOf(api_rows, faceCodes):
    return {code: str(api_rows.face(code).get("shotBucket") or "")
            for code in faceCodes}


@pytest.fixture
def confirmed(api_env, api_rows):
    """把 PHOTO 的两张脸**人工确认**给 P_alpha（质心有样本，桶是自适应桶）。"""
    from processor.review import assigner as assigner

    for code in FACES:
        assigner.confirm(code, PERSON)
    return api_env


# ============================================================
# 一、预览：只读 + 必须给出「会牵动谁」
# ============================================================

def test_previewIsReadOnlyAndListsAffectedPersons(confirmed, api_rows):
    client = confirmed["client"]
    before = _bucketsOf(api_rows, FACES)
    logsBefore = len(api_rows.logs("BUCKET_FIX"))

    got = client.get("/api/photos/%s/shot-year-fix" % PHOTO,
                     params={"shotYear": FIX_YEAR})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is False and body["confirmRequired"] is True
    assert body["changed"] is True
    assert body["currentYear"] == EXIF_YEAR
    assert body["targetYear"] == FIX_YEAR
    assert body["faces"] == 2
    # 影响面必须点名到人（写入的是照片级年份，桶按各人生日现算）
    assert [one["personCode"] for one in body["persons"]] == [PERSON]
    assert body["persons"][0]["oldBuckets"] == [bucket.bucketKeyOf(EXIF_YEAR, BIRTH)]
    assert body["persons"][0]["newBuckets"] == [bucket.bucketKeyOf(FIX_YEAR, BIRTH)]

    # ⚠️ 纯读：预览不能改任何一行
    assert _bucketsOf(api_rows, FACES) == before
    assert api_rows.photo(PHOTO).get("shotYearOverride") is None
    assert len(api_rows.logs("BUCKET_FIX")) == logsBefore


# ============================================================
# 二、提交：三步联动 + 展示口径
# ============================================================

def test_applyWritesOverrideRebucketsAndRebuildsCentroid(confirmed, api_rows):
    client = confirmed["client"]
    oldBucket = bucket.bucketKeyOf(EXIF_YEAR, BIRTH)
    newBucket = bucket.bucketKeyOf(FIX_YEAR, BIRTH)
    assert oldBucket != newBucket, "夹具年份选得不好：修前修后必须落在不同桶"
    assert _bucketsOf(api_rows, FACES) == {c: oldBucket for c in FACES}
    assert oldBucket in {str(r["bucketKey"]) for r in api_rows.centroids(PERSON)}

    got = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                      params={"confirm": 1}, json={"shotYear": FIX_YEAR})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is True and body["changed"] is True
    assert body["newOverride"] == FIX_YEAR
    assert body["bucketsChanged"] == 2
    assert body["logCode"]

    # ① 只写 override；机器读到的 shotYear **一个字都没改**
    row = api_rows.photo(PHOTO)
    assert int(row["shotYearOverride"]) == FIX_YEAR
    assert int(row["shotYear"]) == EXIF_YEAR

    # ② 这张照片的两张脸都刷到了新年代的桶
    assert _bucketsOf(api_rows, FACES) == {c: newBucket for c in FACES}

    # ③ 质心按新桶键重建：新桶在、旧桶被清掉（不是留着一条僵尸向量）
    keys = {str(r["bucketKey"]) for r in api_rows.centroids(PERSON)}
    assert newBucket in keys
    assert oldBucket not in keys

    # 展示口径：列表/详情都用**有效年**，同时把 EXIF 原值如实带出来
    detail = client.get("/api/photos/%s" % PHOTO).json()
    assert detail["shotYear"] == FIX_YEAR
    assert detail["shotYearExif"] == EXIF_YEAR
    assert detail["shotYearOverride"] == FIX_YEAR


def test_timeFilterUsesEffectiveYear(confirmed, api_rows):
    """按修正后的年代能筛出来；按 EXIF 那个年代筛**筛不到**它了。"""
    client = confirmed["client"]
    client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                params={"confirm": 1}, json={"shotYear": FIX_YEAR})

    hits = client.get("/api/photos", params={"shotYearFrom": 1999,
                                             "shotYearTo": 2001,
                                             "size": 50}).json()
    assert PHOTO in {item["photoCode"] for item in hits["items"]}

    misses = client.get("/api/photos", params={"shotYearFrom": EXIF_YEAR,
                                               "shotYearTo": EXIF_YEAR,
                                               "size": 50}).json()
    assert PHOTO not in {item["photoCode"] for item in misses["items"]}


# ============================================================
# 三、幂等 / 恢复自动
# ============================================================

def test_sameValueIsIdempotent(confirmed, api_rows):
    """重复提交同一个年份：不写库、不刷桶、**不留第二条日志**。"""
    client = confirmed["client"]
    first = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                        params={"confirm": 1}, json={"shotYear": FIX_YEAR}).json()
    assert first["changed"] is True
    logsAfterFirst = len(api_rows.logs("BUCKET_FIX"))

    again = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                        params={"confirm": 1}, json={"shotYear": FIX_YEAR}).json()
    assert again["changed"] is False
    assert again["logCode"] == ""
    assert len(api_rows.logs("BUCKET_FIX")) == logsAfterFirst


def test_restoreAutoClearsOverride(confirmed, api_rows):
    """传 null = 恢复自动：override 写成 **NULL**（不是空串、不是留着旧值）。"""
    client = confirmed["client"]
    client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                params={"confirm": 1}, json={"shotYear": FIX_YEAR})

    body = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                       params={"confirm": 1}, json={"shotYear": None}).json()
    assert body["executed"] is True and body["changed"] is True
    assert body["newOverride"] is None
    assert body["effectiveYear"] == EXIF_YEAR

    row = api_rows.photo(PHOTO)
    assert row["shotYearOverride"] is None
    # 桶要回到「按 EXIF 年份算」的那一个
    assert _bucketsOf(api_rows, FACES) == {
        c: bucket.bucketKeyOf(EXIF_YEAR, BIRTH) for c in FACES}


# ============================================================
# 四、日志与撤销
# ============================================================

def test_logIsRevertibleAndVisiblePerPerson(confirmed, api_rows):
    """主日志 + 每个受影响人一条成员日志 —— 后者决定它能否出现在
    「这个人的操作历史」里（/review/log?personCode= 是按归属人等值查的）。"""
    client = confirmed["client"]
    body = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                       params={"confirm": 1}, json={"shotYear": FIX_YEAR}).json()
    logCode = body["logCode"]

    logs = api_rows.logs("BUCKET_FIX")
    heads = [one for one in logs if str(one["logCode"]) == logCode]
    members = [one for one in logs if str(one["logCode"]).startswith(logCode + ".")]
    assert len(heads) == 1 and len(members) == 1
    assert int(heads[0]["isRevertible"]) == 1
    assert str(heads[0]["photoCode"]) == PHOTO
    assert str(members[0]["toPersonCode"]) == PERSON

    # 这个人的操作历史里查得到（前端「操作历史」抽屉就是这么查的）。
    # ⚠️ 出现的是**成员日志**（`<主码>.<personCode>`），不是主日志 ——
    #    主日志的 toPersonCode 是空的（一次修正可能牵动多人，写谁都不对），
    #    而 /review/log?personCode= 是**等值**查 toPersonCode。
    history = client.get("/api/review/log", params={"personCode": PERSON}).json()
    codes = {one["logCode"] for one in history["items"]}
    assert "%s.%s" % (logCode, PERSON) in codes

    # 撤销按钮的候选集里也有它
    revertible = client.get("/api/review/revertible").json()
    assert logCode in {one["logCode"] for one in revertible["items"]}


def test_undoRestoresYearAndBuckets(confirmed, api_rows):
    """撤销 = 把 override 写回原值 + 重刷桶 + 重算质心 + 回填 revertedByLogCode。"""
    client = confirmed["client"]
    oldBucket = bucket.bucketKeyOf(EXIF_YEAR, BIRTH)
    newBucket = bucket.bucketKeyOf(FIX_YEAR, BIRTH)
    logCode = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                          params={"confirm": 1},
                          json={"shotYear": FIX_YEAR}).json()["logCode"]
    assert _bucketsOf(api_rows, FACES) == {c: newBucket for c in FACES}

    got = client.post("/api/review/undo", json={"logCode": logCode})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["opType"] == "BUCKET_FIX"
    assert body["restoredOverride"] is None

    assert api_rows.photo(PHOTO)["shotYearOverride"] is None
    assert _bucketsOf(api_rows, FACES) == {c: oldBucket for c in FACES}
    keys = {str(r["bucketKey"]) for r in api_rows.centroids(PERSON)}
    assert oldBucket in keys and newBucket not in keys
    # 原日志被回填、并留下一条 UNDO
    assert api_rows.logs("BUCKET_FIX")[0]["revertedByLogCode"]
    assert len(api_rows.logs("UNDO")) == 1


def test_undoTwiceRejected(confirmed):
    client = confirmed["client"]
    logCode = client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                          params={"confirm": 1},
                          json={"shotYear": FIX_YEAR}).json()["logCode"]
    assert client.post("/api/review/undo",
                       json={"logCode": logCode}).status_code == 200
    again = client.post("/api/review/undo", json={"logCode": logCode})
    assert again.status_code == 409, again.text


# ============================================================
# 五、参数
# ============================================================

@pytest.mark.parametrize("shotYear", [1200, 99999])
def test_outOfRangeYearRejected(confirmed, shotYear):
    got = confirmed["client"].post("/api/photos/%s/shot-year-fix" % PHOTO,
                                   params={"confirm": 1},
                                   json={"shotYear": shotYear})
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"


def test_missingShotYearRejected(confirmed):
    """`shotYear` **必须显式给**：不传是参数漏了（400），传 null 才是恢复自动。"""
    got = confirmed["client"].post("/api/photos/%s/shot-year-fix" % PHOTO,
                                   params={"confirm": 1}, json={})
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"


# ============================================================
# 六、重扫不得冲掉人工修正（DR-42 的硬承诺）
# ============================================================

def test_rescanUpsertKeepsOverride(confirmed, api_rows):
    """重扫（runner._flush 那套整列替换 upsert）**不得**把 override 洗掉。

    这是「改表加列」之后最容易漏掉的一条：扫描器用 `_META_FULL_COLUMNS`
    做 updateColumns + forceColumns，**新列不在那个白名单里**才不会被动。
    哪天有人把它加进去（"顺手补全列"），这个用例会立刻变红。
    """
    from database.auto_generated import sqliteCommon
    from processor.scanner import runner as scanRunner

    client = confirmed["client"]
    client.post("/api/photos/%s/shot-year-fix" % PHOTO,
                params={"confirm": 1}, json={"shotYear": FIX_YEAR})
    assert int(api_rows.photo(PHOTO)["shotYearOverride"]) == FIX_YEAR

    row = api_rows.photo(PHOTO)
    # 从库里取全部托管列（forceColumns 要求这些列都得有值，缺一个就是
    # `NOT NULL constraint failed`），再模拟重扫读到的 EXIF 年份覆盖回去。
    payload = {name: row.get(name) for name in scanRunner._META_FULL_COLUMNS}
    payload.update({
        "photoCode": PHOTO, "relPath": row["relPath"],
        "relPathHash": row["relPathHash"], "fileHash": row["fileHash"],
        "fileSize": int(row["fileSize"]),
        # 重扫读到的仍然是 EXIF 那个年份 —— 这正是要防的"洗回原值"
        "takenAt": row["takenAt"], "shotYear": EXIF_YEAR})
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_photo", [payload], conflictColumns=("photoCode",),
        updateColumns=list(scanRunner._META_FULL_COLUMNS),
        fillStandard=True, forceColumns=list(scanRunner._META_FULL_COLUMNS))
    assert rtn is not None and rtn != -2

    assert int(api_rows.photo(PHOTO)["shotYearOverride"]) == FIX_YEAR
