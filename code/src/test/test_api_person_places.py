#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_person_places.py
#Description: R5 地点浏览接口的接口测试（人物×地点**双向** + 归并下钻）
#
# 覆盖
#   * `GET /api/persons/{code}/places`（§3.3.1）
#       - 有地点 / 无地点两条**基准用例**（对应验收第 6 条）
#       - 空状态必须带 `photoTotal` / `locatedPhotoTotal`（引导文案要用）
#       - **实时 join**：先确认一张脸，接口立刻返回（证明不是落库的）
#       - 聚合键是 `COALESCE(placeNameDir, placeName)`，**不是** `placeName`
#       - `nameZh` 按**聚合键**取，不按 placeCode 取（DR-36）
#       - `lastShotYear` 倒序
#   * `GET /api/places/{code}/persons`（验收第 5 条的反方向）
#   * `GET /api/places?groupByNameZh=1`（展示层归并 + 幽灵行过滤，DR-36 方案①）
#   * `GET /api/places/{code}?placeCodes=a,b`（同组下钻）
#
# ⚠️ 为什么这两个方向写在同一个文件里
#   它们是**同一件事的两个入口**（「这个人去过哪」与「这个地点有谁」），
#   而两者的正确性由同一条聚合键决定。拆成两个文件，键一改就要改两处，
#   而漏改一处的症状是「一边有数据、一边空」——看起来像数据问题。

import pytest


def _assign(client, faceCode, personCode):
    """走**真实接口**确认一张脸（不许直接 INSERT pb_photo_person）。

    ⚠️ 必须走接口：`/persons/{code}/places` 要证明的是「实时 join
       `pb_photo_person`」。如果用例自己手工插关联行，那证明的只是
       「SQL 会读这张表」，而不是「确认一张脸之后它立刻出现」——
       而后者才是 DR-26 要保住的性质（不落库、无同步点）。
    """
    got = client.put("/api/review/%s/assign" % faceCode, json={"personCode": personCode})
    assert got.status_code == 200, got.text
    return got.json()


def _addPhoto(photoCode, relPath, shotYear, placeName=None, placeNameDir=None,
              lat=None, lon=None, faceCode=None):
    """插一张照片（可带一张脸），返回 (photoCode, faceCode)。"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    row = {"photoCode": photoCode, "relPath": relPath,
           "relPathHash": "rh_%s" % photoCode, "fileHash": "fh_%s" % photoCode,
           "fileSize": 1000, "mimeType": "image/jpeg", "width": 4000, "height": 3000,
           "takenAt": "%d-06-01T00:00:00Z" % shotYear, "shotYear": shotYear,
           "placeName": placeName, "cameraModel": "Canon EOS 350D",
           "faceCount": 1 if faceCode else 0,
           "isDuplicate": 0, "isMissing": 0, "scanState": 1, "regID": "test"}
    sqliteCommon.insertManyTableGeneral(
        "pb_photo", [row], conflictColumns=("photoCode",),
        updateColumns=tuple(row.keys()), fillStandard=True,
        # ⚠️ placeName / placeNameDir 必须进 forceColumns：整批为 NULL 的列会被
        #    normalizeDataSet 丢掉，于是「目录名地点」这条用例的照片进来时
        #    **没有 placeNameDir 这一列**，用例会以为聚合键没生效。
        forceColumns=("placeName", "placeNameDir", "lat", "lon"))
    if placeNameDir is not None or lat is not None:
        sqliteCommon.updateTableGeneral(
            "pb_photo", "photoCode = %s", (photoCode,),
            {"placeNameDir": placeNameDir, "lat": lat, "lon": lon})
    if faceCode:
        sqliteCommon.insertManyTableGeneral(
            "pb_face",
            [{"faceCode": faceCode, "photoCode": photoCode, "personCode": None,
              "clusterCode": "CL_%s" % faceCode, "bbox": "0.1,0.1,0.2,0.2",
              "detScore": 0.9, "poseYaw": 0.0, "posePitch": 0.0, "quality": 0.8,
              "embedding": None, "shotBucket": "ALL",
              "isConfirmed": 0, "isStranger": 0}],
            conflictColumns=("faceCode",),
            updateColumns=("photoCode", "personCode", "clusterCode", "shotBucket"),
            fillStandard=True,
            forceColumns=("personCode", "embedding"))
    return photoCode, faceCode


def _addPlace(placeCode, placeName, nameZh=None, photoCount=0,
              firstShotYear=None, lastShotYear=None):
    from database.auto_generated import sqliteCommon as sqliteCommon

    row = {"placeCode": placeCode, "placeName": placeName, "nameZh": nameZh,
           "source": 0, "photoCount": photoCount,
           "firstShotYear": firstShotYear, "lastShotYear": lastShotYear,
           "centerLat": None, "centerLon": None, "regID": "test"}
    sqliteCommon.insertManyTableGeneral(
        "pb_place", [row], conflictColumns=("placeCode",),
        updateColumns=("placeName", "nameZh", "photoCount",
                       "firstShotYear", "lastShotYear"),
        fillStandard=True,
        forceColumns=("nameZh", "firstShotYear", "lastShotYear"))


# ============================================================
# 一、GET /api/persons/{personCode}/places
# ============================================================

def test_personPlacesEmptyButCountsAreHonest(api_env):
    """**无地点**的基准用例：`places=[]`，但两个计数要如实给 0。

    ⚠️ 这条是界面的「空状态文案」的数据来源。只回一个空数组的话，
       用户看到的是一片空白，分不清「功能没做」与「他确实没有带地点的照片」。
    """
    client = api_env["client"]
    body = client.get("/api/persons/P_gamma/places").json()
    assert body["ok"] is True
    assert body["personCode"] == "P_gamma"
    assert body["photoTotal"] == 0
    assert body["locatedPhotoTotal"] == 0
    assert body["places"] == []


def test_personPlacesUnknownPersonIs404(api_env):
    """人物不存在 -> 404（不是空列表：那会让界面显示「他 0 张照片」）。"""
    got = api_env["client"].get("/api/persons/查无此人/places")
    assert got.status_code == 404, got.text
    assert got.json()["code"] == "NOT_FOUND"


def test_personPlacesIsRealtimeJoinNotSnapshot(api_env):
    """**有地点**的基准用例 + 实时性证明（对应验收第 5/6 条）。

    步骤：确认前是空的 -> 走接口确认一张「有地点照片」上的脸
          -> **同一个进程、同一条连接**再查，立刻出现。
    """
    client = api_env["client"]
    # ① 确认前：P_alpha 一张照片都没有
    before = client.get("/api/persons/P_alpha/places").json()
    assert before["photoTotal"] == 0 and before["places"] == []

    # ② PH_2013_01 的 placeName='北京'（夹具里那张），确认它的一张脸
    _assign(client, "FC_PH_2013_01_0", "P_alpha")

    after = client.get("/api/persons/P_alpha/places").json()
    assert after["photoTotal"] == 1
    assert after["locatedPhotoTotal"] == 1
    assert [one["placeName"] for one in after["places"]] == ["北京"]
    one = after["places"][0]
    assert one["photoCount"] == 1
    assert one["firstShotYear"] == 2013 and one["lastShotYear"] == 2013
    # ⚠️ placeCode 必须与字典路径**同一条派生规则**（否则前端从人物详情
    #    跳到地点详情会 404，而 `/api/places` 里明明有这个名字的地点）
    from processor.place import placeStore
    assert one["placeCode"] == placeStore.makePlaceCode("北京")

    # ③ 「有照片但没地点」的那张：photoTotal 涨、locatedPhotoTotal 不涨 ——
    #    这是空状态文案「N 张里只有 M 张有地点」的分母与分子
    _assign(client, "FC_PH_2024_06_0", "P_alpha")
    grew = client.get("/api/persons/P_alpha/places").json()
    assert grew["photoTotal"] == 2
    assert grew["locatedPhotoTotal"] == 1
    assert [one["placeName"] for one in grew["places"]] == ["北京"]


def test_personPlacesUsesPlaceNameDirKey(api_env):
    """聚合键必须是 `COALESCE(NULLIF(placeNameDir,''), placeName)`（DR-32）。

    ⚠️ 写成 `p.placeName` 的后果：598 张「目录名带地点」的照片
       （`placeName` 是空的）**全部进 NULL 组被丢掉**，于是「去过的地方」
       只剩 GPS 那批 —— 接口 200、列表结构完好，没有任何报错。
    """
    client = api_env["client"]
    _addPhoto("PH_DIR_US", "2013.07.26 华盛顿/a.jpg", 2013,
              placeName=None, placeNameDir="华盛顿", faceCode="FC_DIR_US_0")
    _assign(client, "FC_DIR_US_0", "P_beta")

    body = client.get("/api/persons/P_beta/places").json()
    assert body["photoTotal"] == 1
    assert body["locatedPhotoTotal"] == 1, "目录名线索没被算进「有地点」"
    assert [one["placeName"] for one in body["places"]] == ["华盛顿"]
    # 目录名地点没有中文名（名字本身就是中文，DR-34）
    assert body["places"][0]["nameZh"] is None


def test_personPlacesNameZhComesFromAggregationKey(api_env):
    """`nameZh` 按**聚合键**从 `pb_place` 取（不按 placeCode 取，DR-36）。

    ⚠️ 按 placeCode 关联的失败方式：目录名地点的聚合键与 placeCode
       **不是一对一**（可能被重名归并、也可能是改名后归零的旧行），
       于是会取到**另一个地点**的中文名 —— 而界面上它看起来完全正常。
    """
    client = api_env["client"]
    key = "CN, Beijing, Datun"
    _addPlace("PL_CN_Beijing_Datun", key, nameZh="北京市 · 朝阳区",
              photoCount=1, firstShotYear=2009, lastShotYear=2010)
    _addPhoto("PH_GPS_DATUN", "2009/01/x.jpg", 2009,
              placeName=key, placeNameDir=None,
              faceCode="FC_GPS_DATUN_0")
    _assign(client, "FC_GPS_DATUN_0", "P_alpha")

    body = client.get("/api/persons/P_alpha/places").json()
    assert len(body["places"]) == 1
    one = body["places"][0]
    assert one["placeName"] == key            # 英文聚合键原值
    assert one["nameZh"] == "北京市 · 朝阳区"  # 中文显示名（界面读它）
    assert one["photoCount"] == 1


def test_personPlacesOrderIsLastShotYearDesc(api_env):
    """`lastShotYear` **倒序**（最近去过的在前）。

    ⚠️ 用两张**年份不同**的照片取证：年份相同的话，升序降序看起来一样，
       用例会「通过但没测到东西」。
    """
    client = api_env["client"]
    _assign(client, "FC_PH_2013_01_0", "P_alpha")     # 北京 / 2013
    _assign(client, "FC_PH_2020_11_0", "P_alpha")     # 上海 / 2020

    body = client.get("/api/persons/P_alpha/places").json()
    assert [one["placeName"] for one in body["places"]] == ["上海", "北京"]
    assert [one["lastShotYear"] for one in body["places"]] == [2020, 2013]


def test_personPlacesCountsDistinctPhotos(api_env):
    """同一个人在同一张照片里有**多张脸**时，张数不能重复计。

    ⚠️ `pb_photo_person` 是「照片×人」的关联表，理论上一个人一张照片
       只有一行；但 `COUNT(*)` 与 `COUNT(DISTINCT photoCode)` 的差别在
       「关联表被写重了」这种脏数据下才显形 —— 而那时界面会显示
       「北京 2 张」而实际上只有 1 张。
    """
    client = api_env["client"]
    # PH_2013_01 有 2 张脸（FC_PH_2013_01_0 / _1），两张都确认为同一个人
    _assign(client, "FC_PH_2013_01_0", "P_alpha")
    _assign(client, "FC_PH_2013_01_1", "P_alpha")

    body = client.get("/api/persons/P_alpha/places").json()
    assert body["photoTotal"] == 1
    assert body["places"][0]["photoCount"] == 1


# ============================================================
# 二、GET /api/places/{placeCode}/persons（反方向，同一把键）
# ============================================================

def test_placePersonsIsRealtimeAndCarriesEmptyStateCounts(api_env):
    """地点侧的「在场的人」：**实时**取，且空的时候带得出 N/0。"""
    client = api_env["client"]
    client.post("/api/places/rebuild")                  # 先建字典
    from processor.place import placeStore
    code = placeStore.makePlaceCode("北京")

    before = client.get("/api/places/%s/persons" % code).json()
    assert before["items"] == []
    assert before["personTotal"] == 0
    assert before["photoTotal"] > 0, "北京有照片（空状态的引导文案要这个数）"

    _assign(client, "FC_PH_2013_01_0", "P_alpha")

    after = client.get("/api/places/%s/persons" % code).json()
    assert [one["personCode"] for one in after["items"]] == ["P_alpha"]
    assert after["items"][0]["displayName"] == "阿尔法"
    assert after["items"][0]["photoCount"] == 1
    assert after["items"][0]["lastShotYear"] == 2013
    assert after["personTotal"] == 1


# ============================================================
# 三、列表归并（展示层，DR-36 方案①）
# ============================================================

def test_groupedListMergesSameNameZhAndSkipsGhosts(api_env):
    """`groupByNameZh=1`：同 `nameZh` 并成一行、`photoCount` 求和、幽灵行不出现。

    ⚠️ 三条都要断言，少一条就会漏掉一类静默退化：
       · 张数求和 —— 写成「取第一行」会少算一半（15 张显示成 8 张）
       · `placeCodes` 两个都留 —— 只留一个的话下钻拿不到另一个点
       · 幽灵行（photoCount=0）不出现在展示层
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    client = api_env["client"]
    _addPlace("PL_CN_Beijing_Datun", "CN, Beijing, Datun", nameZh="北京市 · 朝阳区",
              photoCount=8, firstShotYear=2009, lastShotYear=2010)
    _addPlace("PL_CN_Beijing_Wangjing", "CN, Beijing, Wangjing", nameZh="北京市 · 朝阳区",
              photoCount=7, firstShotYear=2009, lastShotYear=2009)
    _addPlace("PL_GH_Western_Takoradi", "GH, Western, Takoradi", nameZh=None,
              photoCount=0, firstShotYear=2009, lastShotYear=2010)

    body = client.get("/api/places", params={"groupByNameZh": 1,
                                             "minPhotoCount": 1}).json()
    names = [one["placeName"] for one in body["items"]]
    assert "GH, Western, Takoradi" not in names, "幽灵行不该出现在展示层"
    merged = next(one for one in body["items"] if one["nameZh"] == "北京市 · 朝阳区")
    assert merged["photoCount"] == 15
    assert len(merged["placeCodes"]) == 2
    assert len(merged["members"]) == 2
    assert {one["photoCount"] for one in merged["members"]} == {8, 7}
    # 归并前后**总张数不变**（不能因归并丢照片）
    all0 = client.get("/api/places", params={"groupByNameZh": 0,
                                             "minPhotoCount": 1}).json()
    assert sum(one["photoCount"] for one in all0["items"]) == \
        sum(one["photoCount"] for one in body["items"])
    # 诊断字段：归并前的行数 == 同一筛选下未归并的 total；归并后行数变少
    assert body["ungroupedTotal"] == all0["total"] == 2
    assert body["total"] == 1
    assert body["mergedGroups"] >= 1

    # 不给 minPhotoCount 时，幽灵行是**归并路径自己滤掉**的 -> filteredZero 记账
    plain = client.get("/api/places", params={"groupByNameZh": 1}).json()
    assert plain["ungroupedTotal"] == 3
    assert plain["filteredZero"] == 1
    assert plain["total"] == 1
    # ⚠️ 恒等式：归并前的行数 = 滤掉的幽灵 + 被并掉的组 + 归并后的行数。
    #    少回其中任何一个数字，「列表里少了两行」都只能靠人去数。
    assert (plain["ungroupedTotal"] - plain["filteredZero"]
            - plain["mergedGroups"]) == plain["total"]
    assert all("Takoradi" not in one["placeName"] for one in plain["items"])

    # ⚠️ 未归并路径**保持步骤 9 的行为**（0 张的幽灵行照给）——
    #    这是「归零但留行」的既定语义，本步不替它改口径
    raw = client.get("/api/places", params={"groupByNameZh": 0}).json()
    assert raw["total"] == 3, "幽灵行在未归并路径上仍然可见（行为未改）"


def test_detailDrillsDownToGroupMembers(api_env):
    """`?placeCodes=a,b` 返回**组内各点各自的张数**（下钻，DR-36）。"""
    client = api_env["client"]
    _addPlace("PL_A_Datun", "CN, Beijing, Datun", nameZh="北京市 · 朝阳区",
              photoCount=8, firstShotYear=2009, lastShotYear=2010)
    _addPlace("PL_B_Wangjing", "CN, Beijing, Wangjing", nameZh="北京市 · 朝阳区",
              photoCount=7, firstShotYear=2009, lastShotYear=2009)

    body = client.get("/api/places/PL_A_Datun",
                      params={"placeCodes": "PL_A_Datun,PL_B_Wangjing"}).json()
    assert body["photoCount"] == 15
    assert body["merged"] is True
    assert body["firstShotYear"] == 2009 and body["lastShotYear"] == 2010
    assert {one["placeCode"]: one["photoCount"] for one in body["members"]} == \
        {"PL_A_Datun": 8, "PL_B_Wangjing": 7}
    # 不给 placeCodes 时按同 nameZh **自动**并同组（收藏夹/直接敲 URL 那条路径）
    auto = client.get("/api/places/PL_A_Datun").json()
    assert auto["photoCount"] == 15
    assert len(auto["placeCodes"]) == 2
    # 目录名地点没有坐标 -> hasCoord=False（界面据此不画地图，DR-37②）
    assert auto["hasCoord"] is False


def test_detailPlacePhotosAndYearsUseAggregationKey(api_env):
    """地点的照片流按**聚合键**筛，并按年给出**整点**计数（不是本页的）。"""
    client = api_env["client"]
    _addPhoto("PH_DIR_US1", "2013.07.26 华盛顿/a.jpg", 2013,
              placeName=None, placeNameDir="华盛顿", faceCode="FC_DIR_US1_0")
    _addPhoto("PH_DIR_US2", "2014.07.26 华盛顿/b.jpg", 2014,
              placeName=None, placeNameDir="华盛顿", faceCode="FC_DIR_US2_0")
    client.post("/api/places/rebuild")
    from processor.place import placeStore
    code = placeStore.makePlaceCode("华盛顿")

    body = client.get("/api/places/%s/photos" % code, params={"size": 1}).json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    # photoSummary 必须在列表项里带出地点（placeName 空、目录名有值的照片）
    assert body["items"][0]["placeName"] == "华盛顿"
    assert {one["year"]: one["count"] for one in body["years"]} == {2013: 1, 2014: 1}
    assert body["place"]["placeName"] == "华盛顿"
