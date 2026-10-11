#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_place_fix.py
#Description: DR-52 地点修正（A 单张照片 / B 显示名）测试 —— 聚合键三段 / 三条纪律 / 自动 rebuild
#
# 这一组盯住的是「静默错」而不是「跑不通」
# ----------------------------------------
#   用户改完地点，如果下一次重扫 / rebuild / 目录名填空把它冲掉，**而且不报错**，
#   那就是最难发现的一类退化。所以每个断言都落在**库里的列**上（api_rows 直查，
#   绕开 api 自证）：
#     · pb_photo.placeNameOverride    —— 写进去了没有、恢复自动是不是真的 NULL、
#                                       三条自动流程是不是**都没碰它**
#     · pb_place.photoCount           —— 提交后是不是立刻正确（from -1 / to +1）
#     · pb_place.nameZh               —— 只改显示名、且 rebuild 不覆盖（DR-28）
#   以及「两个口径同源」（placeKeySql 的 SQL 结果 == placeKeyOf 的 Python 结果）。
#
# 全部跑在 pytest 的临时库上（api_env），**绝不动 d:\PhotoLib 正式库**。

import hashlib
import json

import pytest

#: 有目录名线索的照片（目录名「华盛顿」覆盖了它的 placeName=北京）
PHOTO_DIR = "PH_2013_07"
#: 只有 GPS 地点的照片（placeName=广州），改它最干净
PHOTO_GPS = "PH_2019_04"


@pytest.fixture
def fixEnv(api_env, api_rows):
    """把夹具摆成"有目录名地点 + 有 GPS 地点"的样子，并**建好字典**。

    ⚠️ 必须先 rebuild：`placeFix` 的预览要报「改完张数会变成几」，
       而它算的是**实时**照片数（不读缓存），但地点行是否存在要看字典。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.place import placeStore as placeStore

    # PHOTO_DIR：目录名「华盛顿」把它的聚合键从「北京」改成「华盛顿」（dir 优先）
    assert sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (PHOTO_DIR,),
        {"placeNameDir": "华盛顿"}) > 0
    placeStore.rebuildPlaces()
    return api_env


def _placeCounts(api_rows=None):
    """`{placeName: photoCount}`（直查字典表，不看接口）。"""
    from database import queryCommon as query

    return {str(one["placeName"]): int(one["photoCount"] or 0)
            for one in query.selectList(
                "SELECT placeName AS placeName, photoCount AS photoCount"
                " FROM pb_place WHERE delFlag = %s", ("0",))}


def _sqlKey(photoCode):
    """`placeKeySql` 在真库上对某张照片算出的键（SQL 口径）。"""
    from database import queryCommon as query
    from processor.place import placeStore as placeStore

    got = query.selectValue(
        "SELECT " + placeStore.placeKeySql("p") +
        " FROM pb_photo p WHERE p.photoCode = %s", (photoCode,))
    return str(got if got is not None else "")


def _pyKey(row):
    """`placeKeyOf` 对同一行算出的键（Python 口径）。"""
    from processor.place import placeStore as placeStore

    return placeStore.placeKeyOf(row.get("placeName"), row.get("placeNameDir"),
                                 row.get("placeNameOverride"))


def _simulateRescan(photoCode):
    """模拟**扫描器**对一张照片的整列替换（runner 的写路径）。

    ⚠️ 直接复刻 runner 的列集：`_META_FULL_COLUMNS` + forceColumns。
       这样本用例证明的是「重扫这条真实写路径碰不到 override」，
       而不是"某个我编的 UPDATE 碰不到它"。
    ⚠️ 元数据值取**当前行**的原值（只把 placeName 改成标记值）：
       编造别的值会撞 NOT NULL / 类型（fileSize 是 INT NOT NULL），
       那会让用例失败在一个与"override 是否被冲掉"无关的地方。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from processor.scanner import runner as runner

    row = sqliteCommon.query_pb_photo("pb_photo", photoCode=photoCode,
                                      delFlag="*")[0]
    payload = {key: row.get(key) for key in
               ("photoCode", "relPath", "relPathHash", "fileHash")}
    payload.update({key: row.get(key) for key in runner._META_FULL_COLUMNS})
    payload["placeName"] = "RESCAN"                  # 重扫把元数据整列替换掉了
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [payload], conflictColumns=("photoCode",),
        updateColumns=runner._META_FULL_COLUMNS, fillStandard=True,
        forceColumns=runner._META_FULL_COLUMNS)


# ============================================================
# 一、三条「不许碰它」的纪律（本步最关键的一组）
# ============================================================

def test_overrideIsNotInAnyAutoWritePath(fixEnv):
    """三条禁区**逐条**核对常量集合（这是纪律的机械证据）。

    加进去的后果分别是：① 重扫清零；② rebuild 覆盖；③ 扫描收尾填空覆盖。
    三者都不报错 —— 所以必须钉在**常量**上，而不是靠某次跑一遍没出问题。
    """
    from processor.place import placeStore as placeStore
    from processor.scanner import runner as runner

    assert "placeNameOverride" not in runner._META_FULL_COLUMNS, \
        "进了重扫白名单 -> 重扫一次把用户改的地点全部清零"
    assert "placeNameOverride" not in placeStore.REBUILD_COLUMNS, \
        "进了 rebuild 复算列 -> 每轮 POST /places/rebuild 覆盖"
    # ③ 目录名填空只写 placeNameDir 一列（见 dirNamePlace.scanDirNames 的 updateColumns）
    import inspect
    from processor.place import dirNamePlace as dirNamePlace

    assert "placeNameOverride" not in inspect.getsource(dirNamePlace.scanDirNames)


def test_rescanDoesNotClearOverride(fixEnv, api_rows):
    """⭐ 纪律①：重扫（runner 的整列替换写路径）**不清零**用户改的地点。"""
    client = fixEnv["client"]
    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      params={"confirm": 1}, json={"placeName": "外婆家"})
    assert got.status_code == 200, got.text
    assert str(api_rows.photo(PHOTO_GPS)["placeNameOverride"]) == "外婆家"

    _simulateRescan(PHOTO_GPS)

    row = api_rows.photo(PHOTO_GPS)
    assert str(row["placeNameOverride"]) == "外婆家", "重扫把人工修正冲掉了"
    # 反证：重扫**确实写了**（placeName 被打成了扫描器给的值）——
    # 否则上一条断言可能只是"这次更新根本没发生"
    assert str(row["placeName"]) == "RESCAN"


def test_rebuildDoesNotClearOverride(fixEnv, api_rows):
    """⭐ 纪律②：`POST /api/places/rebuild`（全量复算）**不清零**用户改的地点。"""
    client = fixEnv["client"]
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    body = client.post("/api/places/rebuild").json()
    assert body["ok"] is True
    assert str(api_rows.photo(PHOTO_GPS)["placeNameOverride"]) == "外婆家"
    # 而且「外婆家」真的进了字典（这正是 override 参与聚合键的证明）
    assert int(_placeCounts().get("外婆家") or 0) == 1


def test_scanDirNamesDoesNotClearOverride(fixEnv, api_rows):
    """⭐ 纪律③：目录名填空（`dirNamePlace.scanDirNames`）**不清零**用户改的地点。"""
    from processor.place import dirNamePlace as dirNamePlace

    client = fixEnv["client"]
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    report = dirNamePlace.scanDirNames(dryRun=False)
    assert report["written"] >= 0                     # 真跑了一次（可能 0 行要填）
    assert str(api_rows.photo(PHOTO_GPS)["placeNameOverride"]) == "外婆家"


# ============================================================
# 二、两个口径同源（placeKeySql == placeKeyOf）
# ============================================================

def test_twoDialectsAgreeOnSameKey(fixEnv, api_rows):
    """⭐ SQL 口径与 Python 口径对**同一张照片**给出同一个键（两种情形各一张）。

    这两处一旦分叉，症状是「地点列表说 114 张、点进去筛出 0 张」——
    接口 200、两条路径各自自洽，就是合不上。
    """
    client = fixEnv["client"]
    # 改掉 GPS 那张，让它走 override 优先（PHOTO_DIR 保持 dir 优先）
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    for code in (PHOTO_DIR, PHOTO_GPS):
        row = api_rows.photo(code)
        assert _sqlKey(code) == _pyKey(row), (code, row)
    # ① 正常照片（dir 优先）：目录名压过 placeName
    dirRow = api_rows.photo(PHOTO_DIR)
    assert _pyKey(dirRow) == "华盛顿", dirRow
    # ② 改过的照片（override 优先）：人工值压过一切
    ovRow = api_rows.photo(PHOTO_GPS)
    assert _pyKey(ovRow) == "外婆家", ovRow


def test_emptyStringOverrideFallsBack(fixEnv, api_rows):
    """边界：`placeNameOverride` 是**空串**（不是 NULL）时，聚合键**必须回落**。

    空串若被当成地点名，地点列表里会长出一个"空名字"的地点 —— 而且不报错。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    assert sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (PHOTO_DIR,),
        {"placeNameOverride": ""}) > 0
    row = api_rows.photo(PHOTO_DIR)
    assert _pyKey(row) == "华盛顿", row                 # 回落到 dir
    assert _sqlKey(PHOTO_DIR) == "华盛顿"

    # 空串不产生新地点：rebuild 之后没有 placeName 为空串的地点行
    from processor.place import placeStore as placeStore
    placeStore.rebuildPlaces()
    assert "" not in _placeCounts()
    assert int(_placeCounts().get("华盛顿") or 0) == 1


# ============================================================
# 三、接口 A：预览 / 两段式 / 落库 / 恢复自动 / 校验
# ============================================================

def test_previewIsReadOnlyAndReportsImpact(fixEnv, api_rows):
    client = fixEnv["client"]
    beforeCounts = _placeCounts()

    got = client.get("/api/photos/%s/place-fix" % PHOTO_GPS,
                     params={"placeName": "华盛顿"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is False and body["confirmRequired"] is True
    assert body["changed"] is True
    assert body["restoreAuto"] is False
    # 当前：GPS 逆地理（无 override、无目录名）
    assert body["current"] == {"placeKey": "广州", "nameZh": None, "source": "gps"}
    assert body["autoPlaceKey"] == "广州"
    # 目标：已存在的「华盛顿」（1 张 -> 2 张）
    assert body["target"]["placeKey"] == "华盛顿"
    assert body["target"]["exists"] is True
    assert body["target"]["photoCount"] == 1
    # 影响面：广州 1 -> 0（会变空），华盛顿 1 -> 2
    assert body["counts"]["from"] == {"placeKey": "广州", "photoCount": 1, "after": 0}
    assert body["counts"]["to"] == {"placeKey": "华盛顿", "photoCount": 1, "after": 2}
    assert body["fromBecomesEmpty"] is True

    # 纯读：一行都不写
    assert api_rows.photo(PHOTO_GPS).get("placeNameOverride") in (None, "")
    assert _placeCounts() == beforeCounts


def test_previewAutoSummaryForOverriddenPhoto(fixEnv, api_rows):
    """改过之后再看预览：`current.source=override`、`autoPlaceKey` 指向自动值。"""
    client = fixEnv["client"]
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    body = client.get("/api/photos/%s/place-fix" % PHOTO_GPS, params={}).json()
    assert body["current"]["placeKey"] == "外婆家"
    assert body["current"]["source"] == "override"
    assert body["autoPlaceKey"] == "广州"
    assert body["restoreAuto"] is True                 # 不带 placeName = 预览恢复自动
    assert body["changed"] is True                     # 现在清掉会真的变


def test_postWithoutConfirmOnlyPreviews(fixEnv, api_rows):
    """不带 confirm（或 confirm=0）**只回预览**，库一行都不改。"""
    client = fixEnv["client"]
    before = api_rows.photo(PHOTO_GPS).get("placeNameOverride")

    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      json={"placeName": "外婆家"})
    assert got.status_code == 200 and got.json()["executed"] is False
    assert api_rows.photo(PHOTO_GPS).get("placeNameOverride") == before

    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      params={"confirm": 0}, json={"placeName": "外婆家"})
    assert got.status_code == 200 and got.json()["executed"] is False
    assert api_rows.photo(PHOTO_GPS).get("placeNameOverride") == before


def test_applyWritesOverrideRebuildsAndCountsAreCorrect(fixEnv, api_rows):
    """提交：落库 + 自动 rebuild + **计数立刻正确**（from -1 / to +1）+ 返回 rebuildSummary。"""
    client = fixEnv["client"]
    before = _placeCounts()
    assert before["广州"] == 1 and before["华盛顿"] == 1

    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      params={"confirm": 1}, json={"placeName": "华盛顿"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is True and body["changed"] is True
    summary = body["rebuildSummary"]
    assert set(summary) >= {"created", "updated", "zeroed", "placeCount"}
    assert isinstance(body["rebuildElapsedMs"], int)

    assert str(api_rows.photo(PHOTO_GPS)["placeNameOverride"]) == "华盛顿"
    after = _placeCounts()
    assert after["华盛顿"] == before["华盛顿"] + 1      # to +1
    assert after["广州"] == before["广州"] - 1          # from -1（0 张，行保留）
    assert "广州" in after, "0 张的地点行必须保留（不删行、不留幽灵）"


def test_restoreAutoWritesNullAndFallsBack(fixEnv, api_rows):
    """恢复自动：`placeNameOverride` 变 **NULL**，接口返回的 placeName 回到自动值。"""
    client = fixEnv["client"]
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      params={"confirm": 1}, json={"placeName": None})
    assert got.status_code == 200, got.text
    assert got.json()["restoreAuto"] is True
    assert api_rows.photo(PHOTO_GPS).get("placeNameOverride") is None

    detail = client.get("/api/photos/%s" % PHOTO_GPS).json()
    assert detail["placeName"] == "广州"                 # 回到自动
    assert detail["placeAutoName"] == "广州"
    assert detail["placeIsOverride"] is False

    # 空串同样是恢复自动
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": ""})
    assert api_rows.photo(PHOTO_GPS).get("placeNameOverride") is None


def test_invalidInputIs400(fixEnv):
    """非法值：>128 / 全空白 / 漏传字段 -> 400（不是 422、不是静默）。"""
    client = fixEnv["client"]
    url = "/api/photos/%s/place-fix" % PHOTO_GPS

    got = client.post(url, params={"confirm": 1}, json={"placeName": "地" * 129})
    assert got.status_code == 400 and got.json()["code"] == "PARAM_INVALID", got.text

    got = client.post(url, params={"confirm": 1}, json={"placeName": "   "})
    assert got.status_code == 400 and got.json()["code"] == "PARAM_INVALID", got.text

    got = client.post(url, params={"confirm": 1}, json={})     # 漏传 placeName
    assert got.status_code == 400 and got.json()["code"] == "PARAM_INVALID", got.text

    # 预览这条也一样拦
    got = client.get(url, params={"placeName": "地" * 129})
    assert got.status_code == 400


def test_repeatSameValueIsNoop(fixEnv, api_rows):
    """重复提交同值 -> `changed=false`，不报错、也不多跑一遍全量复算。"""
    client = fixEnv["client"]
    url = "/api/photos/%s/place-fix" % PHOTO_GPS
    client.post(url, params={"confirm": 1}, json={"placeName": "外婆家"})
    again = client.post(url, params={"confirm": 1}, json={"placeName": "外婆家"}).json()
    assert again["executed"] is True and again["changed"] is False
    assert again["rebuildSummary"] is None
    assert str(api_rows.photo(PHOTO_GPS)["placeNameOverride"]) == "外婆家"


def test_noReviewLogAndRecognitionDataUntouched(fixEnv, api_rows):
    """不落 `pb_review_log`；人脸 / 质心 / faceCount 逐字节不变。"""
    from database import queryCommon as query

    def recogFingerprint():
        blob = json.dumps(
            query.selectList("SELECT photoCode AS a, faceCount AS b,"
                             " isDuplicate AS c, scanState AS d FROM pb_photo"
                             " ORDER BY photoCode", ()),
            sort_keys=True, ensure_ascii=False)
        blob += json.dumps(
            query.selectList("SELECT faceCode AS a, photoCode AS b,"
                             " personCode AS c, bbox AS d, isConfirmed AS e,"
                             " isStranger AS f FROM pb_face ORDER BY faceCode", ()),
            sort_keys=True, ensure_ascii=False)
        cents = query.selectList(
            "SELECT personCode AS a, bucketKey AS b, centroid AS c"
            " FROM pb_person_centroid ORDER BY personCode, bucketKey", ())
        blob += hashlib.sha256(repr(
            [(r["a"], r["b"], bytes(r["c"] or b"")) for r in cents]
        ).encode()).hexdigest()
        return blob

    client = fixEnv["client"]
    logsBefore = api_rows.count("pb_review_log")
    recogBefore = recogFingerprint()

    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    assert api_rows.count("pb_review_log") == logsBefore
    assert recogFingerprint() == recogBefore


def test_filterAndPlacesFollowOverride(fixEnv, api_rows):
    """筛选跟着变：按新名字命中那 1 张、按旧名字不再命中；地点列表计数正确。"""
    client = fixEnv["client"]
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    got = client.get("/api/photos", params={"placeName": "外婆家", "size": 50}).json()
    assert [one["photoCode"] for one in got["items"]] == [PHOTO_GPS]

    got = client.get("/api/photos", params={"placeName": "广州", "size": 50}).json()
    assert PHOTO_GPS not in [one["photoCode"] for one in got["items"]]
    assert got["total"] == 0

    listed = {one["placeName"]: int(one["photoCount"])
              for one in client.get("/api/places", params={"size": 200}).json()["items"]}
    assert listed.get("外婆家") == 1
    assert listed.get("广州", 0) == 0


def test_freeTextCreatesNewPlaceRow(fixEnv, api_rows):
    """自由输入一个全新名字 -> `pb_place` 新增一行（source=0），photoCount=1。"""
    client = fixEnv["client"]
    before = len(_placeCounts())

    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    from database.auto_generated import sqliteCommon as sqliteCommon
    rows = sqliteCommon.query_pb_place("pb_place", placeName="外婆家")
    assert rows and int(rows[0]["photoCount"]) == 1
    assert int(rows[0]["source"]) == 0
    assert len(_placeCounts()) == before + 1


def test_detailExposesOverrideFlags(fixEnv):
    """照片详情 / 列表都透出 placeAutoName + placeIsOverride（别漏一处）。"""
    client = fixEnv["client"]
    client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                params={"confirm": 1}, json={"placeName": "外婆家"})

    detail = client.get("/api/photos/%s" % PHOTO_GPS).json()
    assert detail["placeIsOverride"] is True
    assert detail["placeAutoName"] == "广州"
    assert detail["placeName"] == "外婆家"

    # `/api/photos` 列表（第 3 处照片 SELECT）也要有这两个字段
    listed = client.get("/api/photos", params={"placeName": "外婆家", "size": 50}).json()
    one = listed["items"][0]
    assert one["placeIsOverride"] is True and one["placeAutoName"] == "广州"

    # 地点照片流（place.py 的 SELECT）也要认它
    from database.auto_generated import sqliteCommon as sqliteCommon
    code = str(sqliteCommon.query_pb_place("pb_place", placeName="外婆家")[0]["placeCode"])
    stream = client.get("/api/places/%s/photos" % code).json()
    assert stream["items"][0]["placeName"] == "外婆家"
    assert stream["items"][0]["placeIsOverride"] is True


# ============================================================
# 四、接口 B：改显示名（nameZh）
# ============================================================

def _placeCodeOf(placeName):
    from database.auto_generated import sqliteCommon as sqliteCommon

    return str(sqliteCommon.query_pb_place("pb_place", placeName=placeName)[0]["placeCode"])


def test_patchRenameSetsNameZh(fixEnv):
    """⭐ PATCH 改 `nameZh` -> 返回新值。"""
    client = fixEnv["client"]
    code = _placeCodeOf("华盛顿")
    got = client.patch("/api/places/%s" % code, json={"nameZh": "旅行 · 华盛顿"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["nameZh"] == "旅行 · 华盛顿"
    assert body["placeCode"] == code
    assert body["changed"] is True

    again = client.get("/api/places/%s" % code).json()
    assert again["nameZh"] == "旅行 · 华盛顿"


def test_patchNameZhSurvivesRebuild(fixEnv, api_rows):
    """⭐ DR-28 纪律的实测证据：跑一次 rebuild -> `nameZh` **不被覆盖**。"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    client = fixEnv["client"]
    code = _placeCodeOf("华盛顿")
    client.patch("/api/places/%s" % code, json={"nameZh": "外婆家"})

    client.post("/api/places/rebuild")

    row = sqliteCommon.query_pb_place("pb_place", placeCode=code)[0]
    assert str(row["nameZh"]) == "外婆家", "rebuild 把 nameZh 覆盖了"
    assert str(row["placeName"]) == "华盛顿", "placeName 被改动了（会污染 placeCode）"
    assert str(row["placeCode"]) == code


def test_patchClearFallsBackToPlaceName(fixEnv):
    """清除 `nameZh` -> 界面回落显示 `placeName`。"""
    client = fixEnv["client"]
    code = _placeCodeOf("华盛顿")
    client.patch("/api/places/%s" % code, json={"nameZh": "外婆家"})

    got = client.patch("/api/places/%s" % code, json={"nameZh": None})
    assert got.status_code == 200, got.text
    assert got.json()["nameZh"] is None
    assert got.json()["nameZhCleared"] is True

    detail = client.get("/api/places/%s" % code).json()
    assert detail["nameZh"] is None
    assert detail["placeName"] == "华盛顿"

    # 全空白 = 清除（不是写入一串空格）
    client.patch("/api/places/%s" % code, json={"nameZh": "外婆家"})
    client.patch("/api/places/%s" % code, json={"nameZh": "   "})
    assert client.get("/api/places/%s" % code).json()["nameZh"] is None


def test_patchValidationAndMissingKey(fixEnv):
    """长度 >128 -> 400；漏传 nameZh -> 400；`placeCode` 一个字都没变。"""
    client = fixEnv["client"]
    code = _placeCodeOf("华盛顿")

    got = client.patch("/api/places/%s" % code, json={"nameZh": "名" * 129})
    assert got.status_code == 400 and got.json()["code"] == "PARAM_INVALID", got.text

    got = client.patch("/api/places/%s" % code, json={})
    assert got.status_code == 400 and got.json()["code"] == "PARAM_INVALID", got.text

    assert _placeCodeOf("华盛顿") == code


def test_patchRenamedZhDrivesFilterAndGrouping(fixEnv):
    """改名后：按**新显示名**筛选命中、按**旧显示名**不命中；归并分组随之变化。

    ⚠️ 「旧名字」指的是**旧的 `nameZh`**（显示名），不是底层的英文聚合键 ——
       `resolvePlaceFilter` ③ 的兜底是「原样精确匹配聚合键」，
       所以按 `placeName`（`华盛顿`）永远还能筛到，那是**要的行为**
       （收藏的 URL / 老前端 / 脚本都还用它）。
    """
    client = fixEnv["client"]
    code = _placeCodeOf("华盛顿")
    # 先给它一个中文显示名（模拟 rebuildNameZh 的产出）
    client.patch("/api/places/%s" % code, json={"nameZh": "华盛顿市"})
    assert client.get("/api/photos",
                      params={"placeName": "华盛顿市", "size": 50}).json()["total"] == 1

    client.patch("/api/places/%s" % code, json={"nameZh": "外婆家"})

    hit = client.get("/api/photos", params={"placeName": "外婆家", "size": 50}).json()
    assert hit["total"] == 1
    miss = client.get("/api/photos", params={"placeName": "华盛顿市", "size": 50}).json()
    assert miss["total"] == 0

    # 归并路径：中文名换了 -> 拉取一次确认它按新名字出现
    grouped = client.get("/api/places",
                         params={"groupByNameZh": 1, "size": 200}).json()
    names = {str(one.get("nameZh") or one.get("placeName")) for one in grouped["items"]}
    assert "外婆家" in names
    assert "华盛顿" not in names


def test_patchRequiresExistingPlaceCode(fixEnv):
    """不存在的 placeCode -> 404（与 GET 详情同一口径）。"""
    got = fixEnv["client"].patch("/api/places/PL_NOT_EXIST", json={"nameZh": "x"})
    assert got.status_code == 404
    assert got.json()["code"] == "NOT_FOUND"


# ============================================================
# 五、photoAction 其他动作未被破坏（回归）
# ============================================================

def test_otherPhotoActionsStillWork(fixEnv, api_rows):
    """软删 / 恢复 / 旋转 / 年代修正各跑一次，确认加了地点修正之后都还在。"""
    client = fixEnv["client"]
    url = "/api/photos/%s" % PHOTO_GPS

    assert client.post(url + "/rotate", json={"rotateDeg": 90}).status_code == 200
    assert int(api_rows.photo(PHOTO_GPS)["rotateDeg"]) == 90
    assert client.post(url + "/rotate-reset").status_code == 200

    got = client.post(url + "/shot-year-fix", params={"confirm": 1},
                      json={"shotYear": 2001})
    assert got.status_code == 200, got.text
    assert int(api_rows.photo(PHOTO_GPS)["shotYearOverride"]) == 2001

    assert client.post(url + "/soft-delete", params={"confirm": 1}).status_code == 200
    assert client.post(url + "/restore").status_code == 200


# ============================================================
# 六、`nameSource` 严格版（R12 / DR-53）——「自由输入的地名不被地理名盖掉」
# ============================================================
# 为什么要有这一节（这是 R12 最容易被忽略的一条连带）
# ----------------------------------------------------
#   A 的「自由输入一个新地点名」（外婆家 / 我家 / 公司）会**新建**一行 pb_place。
#   那一行的 `nameZh`/`nameSource` 都是空 —— 于是下一次 `rebuildNameZh()`
#   （`POST /api/places/rebuild` 或一次扫描收尾）会按坐标把它填成区县名，
#   **用户输入的名字在显示层消失**。数据层完全正常：张数对、override 还在、接口 200。
#   ⇒ 本节的用例都**让目标地点真的有中国坐标**，否则"没被填成中文名"这件事
#     根本证明不了什么（无坐标的地点本来就算不出名字）。

#: 北京朝阳区（望京一带）的真坐标 —— 有它 rebuildNameZh 才算得出「北京市 · 朝阳区」
_BJ_LAT, _BJ_LON = 39.9842, 116.4007


def _giveGps(photoCode) -> None:
    from database.auto_generated import sqliteCommon as sqliteCommon
    assert sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (photoCode,),
        {"lat": _BJ_LAT, "lon": _BJ_LON}) > 0


def _placeRow(placeName: str):
    from database.auto_generated import sqliteCommon as sqliteCommon
    rows = sqliteCommon.query_pb_place("pb_place", placeName=str(placeName))
    return rows[0] if rows else None


def test_newPlaceFromFreeTextIsMarkedManualNamed(fixEnv, api_rows):
    """⭐ 自由输入一个**全新地名** -> 新建的那行标 `nameSource='manual'`，且不被地理名盖掉。"""
    client = fixEnv["client"]
    _giveGps(PHOTO_GPS)                       # 让新地点有坐标（否则算不出中文名）

    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      params={"confirm": 1},
                      json={"placeName": "外婆家"}).json()
    assert got["rebuildSummary"]["created"] >= 1, got["rebuildSummary"]
    assert got["rebuildSummary"]["newPlaceNamedManual"] is True, got["rebuildSummary"]
    # ⚠️ 这里不能拿 `got["target"]["exists"]` 当"改之前不存在"的证据：
    #    `applyFix` 返回的 preview 是在**落库 + rebuild 之后**算的，
    #    那时这一行已经建出来了（exists 当然是 True）。判据用 rebuild 的 `created`。

    row = _placeRow("外婆家")
    assert row is not None and int(row["photoCount"]) == 1, row
    from processor.place import placeNameZh as placeNameZh
    assert row.get("nameSource") == placeNameZh.NAME_SOURCE_MANUAL, row
    # ⚠️ `nameZh` 故意留空：这个名字**就是** placeName 本身，界面回落逻辑
    #    （nameZh || placeName）给出的字一样，抄一遍是冗余（见 _markManualNamed）
    assert not row.get("nameZh"), row

    # 全链路 rebuild（含 rebuildNameZh）之后，名字仍然不许被填成「北京市 · 朝阳区」
    assert client.post("/api/places/rebuild").json()["ok"] is True
    row = _placeRow("外婆家")
    assert not row.get("nameZh"), \
        "自由输入的地名被地理名覆盖了 —— 界面上「外婆家」会变成「北京市 · 朝阳区」"
    assert row.get("nameSource") == placeNameZh.NAME_SOURCE_MANUAL
    # 显示层（照片详情）必须仍然给出用户输入的那个字
    detail = client.get("/api/photos/%s" % PHOTO_GPS).json()
    assert detail["placeName"] == "外婆家"
    assert not detail["placeZh"], detail["placeZh"]


def test_existingPlaceKeepsItsOwnNameSource(fixEnv, api_rows):
    """改成**已存在**的地点 -> 那一行的 `nameZh`/`nameSource` 一个字都不许动。

    ⚠️ 反面即"误伤"：用户从候选下拉里挑了一个已有地点（例如「上海」），
       如果这里顺手把它标成 manual，那个地点就**永远拿不到地理数据集的中文名**了
       —— 而且不报错（有人以为"标 manual 只是加个标记"）。
    """
    client = fixEnv["client"]
    _giveGps(PHOTO_GPS)
    assert client.post("/api/places/rebuild").json()["ok"] is True
    before = _placeRow("上海")
    assert before is not None, "夹具里应当有「上海」这个地点"

    got = client.post("/api/photos/%s/place-fix" % PHOTO_GPS,
                      params={"confirm": 1},
                      json={"placeName": "上海"}).json()
    assert got["target"]["exists"] is True
    assert got["rebuildSummary"]["newPlaceNamedManual"] is False, got["rebuildSummary"]

    after = _placeRow("上海")
    assert after.get("nameZh") == before.get("nameZh")
    assert after.get("nameSource") == before.get("nameSource")


def test_renamePlaceIsManualAndSurvivesRebuild(fixEnv, api_rows):
    """⭐ B 的重命名把来源标成 `manual`；**清除也标 manual**（否则会被地理名填回来）。"""
    from processor.place import placeNameZh as placeNameZh

    client = fixEnv["client"]
    _giveGps(PHOTO_GPS)                       # 广州这个地点因此有了「北京市 · 朝阳区」的坐标
    assert client.post("/api/places/rebuild").json()["ok"] is True
    row = _placeRow("广州")
    code = str(row["placeCode"])

    # ① 改名 -> manual，且 rebuild 全链路不覆盖
    got = client.patch("/api/places/%s" % code, json={"nameZh": "我的广州家"}).json()
    assert got["nameZh"] == "我的广州家" and got["changed"] is True, got
    assert got["nameSource"] == placeNameZh.NAME_SOURCE_MANUAL, got
    assert client.post("/api/places/rebuild").json()["ok"] is True
    assert _placeRow("广州").get("nameZh") == "我的广州家", "rebuild 把人工名盖掉了"

    # ② 同名重复提交 -> 幂等（changed=False）
    again = client.patch("/api/places/%s" % code, json={"nameZh": "我的广州家"}).json()
    assert again["changed"] is False, again

    # ③ 清除 -> nameZh 归 NULL **且 nameSource 仍是 manual**：人工决定"用英文聚合键"
    cleared = client.patch("/api/places/%s" % code, json={"nameZh": None}).json()
    assert cleared["nameZh"] is None and cleared["nameZhCleared"] is True, cleared
    assert cleared["nameSource"] == placeNameZh.NAME_SOURCE_MANUAL, cleared
    assert client.post("/api/places/rebuild").json()["ok"] is True
    row = _placeRow("广州")
    assert not row.get("nameZh"), (
        "清除之后地理名又被填回来了 —— 用户会觉得「清除没生效」，而且不报错")
    assert row.get("nameSource") == placeNameZh.NAME_SOURCE_MANUAL


def test_placeDtosExposeNameSource(fixEnv):
    """`nameSource` 必须**逐个出口**都透出（列表 / 归并 / 详情）。漏一处就是静默不一致。"""
    from processor.place import placeNameZh as placeNameZh

    client = fixEnv["client"]
    _giveGps(PHOTO_GPS)
    assert client.post("/api/places/rebuild").json()["ok"] is True
    code = str(_placeRow("广州")["placeCode"])
    client.patch("/api/places/%s" % code, json={"nameZh": "我的广州家"})

    items = client.get("/api/places", params={"size": 200}).json()["items"]
    one = [i for i in items if i["placeCode"] == code]
    assert one and one[0]["nameSource"] == placeNameZh.NAME_SOURCE_MANUAL, one
    # 其余没改过的地点应当是 geo（zh_env/夹具已回填）或 None，但**绝不能**全是 manual
    others = [i.get("nameSource") for i in items if i["placeCode"] != code]
    assert placeNameZh.NAME_SOURCE_MANUAL not in others, others

    merged = client.get("/api/places",
                        params={"groupByNameZh": 1, "size": 200}).json()["items"]
    hit = [i for i in merged if i.get("nameZh") == "我的广州家"]
    assert hit and hit[0]["nameSource"] == placeNameZh.NAME_SOURCE_MANUAL, hit
    assert hit[0]["members"][0]["nameSource"] == placeNameZh.NAME_SOURCE_MANUAL

    detail = client.get("/api/places/%s" % code).json()
    assert detail["nameSource"] == placeNameZh.NAME_SOURCE_MANUAL, detail
