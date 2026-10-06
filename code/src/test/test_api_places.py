#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_places.py
#Description: 地点字典（pb_place）与 /api/places 的测试（步骤 9 补）
#
# 覆盖
#   * pb_place 表真的被建出来（build_db 幂等补建）
#   * `makePlaceCode` 的**稳定性**（同一地点名两次算出的编码必须相同 ——
#     这是 upsert 幂等的地基，它变了就会在字典里留下重复地点且不报错）
#   * `POST /api/places/rebuild` 全量复算：新建/更新/归零三路都对
#   * `GET /api/places` 读字典表（不是实时聚合），`source == "dictionary"`
#   * **降级路径**：字典为空时回退实时聚合，且 `source == "lib"`
#   * 两条路径下同一个地点的 `placeCode` **必须一致**
#     （否则前端缓存/选中项会在降级与字典之间失效）
#   * `rebuildPlaces` **不清表、不删行、不覆盖 source**
#   * 排序白名单（非法 orderBy -> 400）

import pytest


@pytest.fixture
def placesEnv(api_env, api_rows):
    """造 4 个地点（含一个无 GPS、一个只有 1 张），供字典测试用。"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    # 夹具里已有：PH_2013_01(北京) PH_2013_07(北京) PH_2016_03(上海)
    #             PH_2020_11(上海) PH_2019_04(广州) PH_DUP(北京)
    # 再补两张：一个只出现一次的地点、一个 placeName 为空（截图类）
    sqliteCommon.insertManyTableGeneral(
        "pb_photo",
        [{"photoCode": "PH_2021_05", "relPath": "2021/05/x.jpg",
          "relPathHash": "rh_x", "fileHash": "fh_x", "fileSize": 100,
          "takenAt": "2021-05-01T00:00:00Z", "shotYear": 2021,
          "placeName": "广州", "lat": 23.1291, "lon": 113.2644,
          "faceCount": 0, "isDuplicate": 0, "isMissing": 0, "scanState": 1},
         {"photoCode": "PH_2022_08", "relPath": "2022/08/y.jpg",
          "relPathHash": "rh_y", "fileHash": "fh_y", "fileSize": 100,
          "takenAt": "2022-08-01T00:00:00Z", "shotYear": 2022,
          "placeName": "丽江", "lat": None, "lon": None,
          "faceCount": 0, "isDuplicate": 0, "isMissing": 0, "scanState": 1},
         {"photoCode": "PH_2023_09", "relPath": "2023/09/z.jpg",
          "relPathHash": "rh_z", "fileHash": "fh_z", "fileSize": 100,
          "takenAt": "2023-09-01T00:00:00Z", "shotYear": 2023,
          "placeName": None, "faceCount": 0, "isDuplicate": 0,
          "isMissing": 0, "scanState": 1}],
        conflictColumns=("photoCode",),
        updateColumns=("placeName", "lat", "lon", "shotYear"),
        fillStandard=True, forceColumns=("placeName", "lat", "lon"))
    return api_env


# ============================================================
# 一、表与编码
# ============================================================

def test_placeTableExists(api_env, api_rows):
    """`build_db` 必须把 pb_place 建出来（它是步骤 9 新增表）。"""
    from database import queryCommon as query

    assert query.tableExists("pb_place") is True
    # 表结构：幂等键 + 三个缓存列
    columns = set(query.columnsOf("pb_place"))
    assert {"placeCode", "placeName", "photoCount", "firstShotYear",
            "lastShotYear", "centerLat", "centerLon", "source",
            "modifyYMDHMS"} <= columns


def test_makePlaceCodeIsStable():
    """编码必须**稳定**：同一地点名两次算出同一个值。

    ⚠️ 为什么单列一条：`placeCode` 是 `rebuildPlaces` 的 upsert 冲突键。
       它一旦不稳定（比如有人把派生规则从 sha1 改成顺序号），
       第二次 rebuild 就会把每个地点**当成新的插一遍** ——
       字典里出现两条同名地点，照片数各占一半，而且**不报错**。
    """
    from processor.place import placeStore

    for name in ("北京", "上海", "San Francisco", "北京市", "  北京  "):
        first = placeStore.makePlaceCode(name)
        assert first == placeStore.makePlaceCode(name), name
    # 空白归一：前后空格不该产生两个地点
    assert placeStore.makePlaceCode("  北京  ") == placeStore.makePlaceCode("北京")
    # 不同地点必须不同（否则字典会把它们合并，照片数相加 —— 静默错）
    assert placeStore.makePlaceCode("北京") != placeStore.makePlaceCode("北京市")
    # 空 -> 空（调用方据此跳过）
    assert placeStore.makePlaceCode("") == ""
    assert placeStore.makePlaceCode(None) == ""
    # 前缀与长度上限
    assert placeStore.makePlaceCode("北京").startswith(placeStore.PLACE_CODE_PREFIX)
    assert len(placeStore.makePlaceCode("x" * 500)) <= 64


# ============================================================
# 二、rebuild
# ============================================================

def test_rebuildPlacesFromPhotos(placesEnv, api_rows):
    """全量复算：分组 / 起止年 / 中心点 / 未命名照片数都要对。"""
    from database import queryCommon as query

    body = placesEnv["client"].post("/api/places/rebuild").json()
    assert body["ok"] is True
    assert body["source"] == "rebuilt"
    # 地点：北京(3) / 上海(2) / 广州(2) / 丽江(1)
    assert body["placeCount"] == 4 and body["groups"] == 4
    assert body["created"] == 4 and body["updated"] == 0
    # 夹具 9 张 + 本用例补 3 张 = 12 张。
    # total = placeName 非空（进字典）的照片数 = 8；
    # unnamed = placeName 为空的（截图类，**不进字典**）= 4
    #   （夹具里 PH_2024_06 / PH_2024_06b / PH_NODATE 三张 + 本用例的 PH_2023_09）
    assert body["total"] == 8
    assert body["unnamed"] == 4
    # 库里真的落了 4 行
    assert query.selectValue("SELECT COUNT(*) AS rowNum FROM pb_place",
                             ()) == 4

    rows = {str(r["placeName"]): r for r in
            query.selectList("SELECT placeName, photoCount, firstShotYear,"
                             " lastShotYear, centerLat FROM pb_place"
                             " WHERE delFlag = %s", ("0",))}
    assert set(rows) == {"北京", "上海", "广州", "丽江"}
    assert int(rows["北京"]["photoCount"]) == 3          # 2013_01 + 2013_07 + DUP
    assert int(rows["上海"]["photoCount"]) == 2
    assert int(rows["广州"]["photoCount"]) == 2          # 2019_04 + 2021_05
    assert int(rows["丽江"]["photoCount"]) == 1
    # 起止年：北京 2013..2013（三张都是 2013）
    assert int(rows["北京"]["firstShotYear"]) == 2013
    assert int(rows["北京"]["lastShotYear"]) == 2013
    assert int(rows["广州"]["firstShotYear"]) == 2019
    assert int(rows["广州"]["lastShotYear"]) == 2021
    # 中心点：上海两张夹具照片没有 GPS -> 该列必须是 NULL（不是 0！）
    assert rows["上海"]["centerLat"] is None
    # 广州只有 2021_05 有 GPS
    assert abs(float(rows["广州"]["centerLat"]) - 23.1291) < 1e-6


def test_rebuildIsIdempotentAndKeepsManualSource(placesEnv):
    """第二次 rebuild **不清表、不删行、不覆盖 source**；统计要显示"更新"。"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    client = placesEnv["client"]
    client.post("/api/places/rebuild")
    # 把"北京"标成手工行（模拟步骤 12 的手工建档）
    assert sqliteCommon.updateTableGeneral(
        "pb_place", "placeName = %s", ("北京",),
        {"source": 1, "memo": "手工加的"}) > 0

    again = client.post("/api/places/rebuild").json()
    assert again["created"] == 0, "第二次不该再新建"
    assert again["updated"] == 4, "四个都应算更新"
    assert again["placeCount"] == 4

    rows = sqliteCommon.query_pb_place("pb_place", placeName="北京")
    assert rows, "手工行被删了"
    assert int(rows[0]["source"]) == 1, "手工行的 source 被复算打回了 0"
    assert rows[0]["memo"] == "手工加的", "手工填的 memo 被复算清掉了"


def test_rebuildZeroesStalePlaceButKeepsRow(placesEnv, api_rows):
    """地点在 pb_photo 里消失后：**行要留、photoCount 要归零**。

    ⚠️ 归零而不是删行：手工地点/历史地点要留着（步骤 12 还要把变体并进来）。
       但不归零就会在地图上留下「0 张照片却显示 300 张」的幽灵点。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    client = placesEnv["client"]
    client.post("/api/places/rebuild")
    assert int(sqliteCommon.query_pb_place(
        "pb_place", placeName="丽江")[0]["photoCount"]) == 1

    # 把丽江那张照片改到别处（模拟重扫后 EXIF 变了）
    assert sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", ("PH_2022_08",),
        {"placeName": "香格里拉"}) > 0

    again = client.post("/api/places/rebuild").json()
    assert again["zeroed"] == 1
    assert again["created"] == 1, "香格里拉是新地点"
    rows = sqliteCommon.query_pb_place("pb_place", placeName="丽江")
    assert rows, "不该删行"
    assert int(rows[0]["photoCount"]) == 0, "缓存值必须归零"


def test_rebuildRefusesWhenTableMissing(api_env):
    """表缺失时给**明确报错**而不是静默 0 行。

    真实场景：升级后没跑 `build_db.py`。此时接口必须说清「先去补建表」，
    否则用户看到的是「重建成功，0 个地点」—— 一个看起来完全正常的空结果。
    """
    from common import sqliteHandle as sqliteHandle
    from database.auto_generated import sqliteCommon as sqliteCommon

    db = sqliteCommon.dbHandle()
    assert db.executeWrite("DROP TABLE IF EXISTS pb_place") >= 0
    try:
        got = api_env["client"].post("/api/places/rebuild")
        assert got.status_code == 500, got.text
        assert got.json()["code"] == "DB_ERROR"
        assert "build_db" in got.json()["message"]
    finally:
        # 补回来，免得污染同进程里后续的用例（夹具是 function 级临时库，
        # 但同一个库文件在本用例内还要被自己用到）
        # ⚠️ 这两个生成函数的签名不一样：`createTableSQL_*` 收表名，
        #    `indexSqlList_*` **不收参数**（返回该表全部 CREATE INDEX 语句）。
        db.executeWrite(sqliteCommon.createTableSQL_pb_place("pb_place"))
        for one in sqliteCommon.indexSqlList_pb_place():
            db.executeWrite(one)


# ============================================================
# 三、读取（两条路径）
# ============================================================

def test_listPlacesReadsDictionary(placesEnv):
    """rebuild 之后读的是**字典表**，不是实时聚合。"""
    client = placesEnv["client"]
    client.post("/api/places/rebuild")
    body = client.get("/api/places").json()
    assert body["source"] == "dictionary"
    assert body["dictionaryRebuilt"] is True
    assert body["total"] == 4
    assert [i["placeName"] for i in body["items"]] == ["北京", "上海", "广州", "丽江"]
    assert [i["photoCount"] for i in body["items"]] == [3, 2, 2, 1]
    for one in body["items"]:
        assert one["dataSource"] == "dictionary"
        assert one["placeCode"].startswith("PL_")
    assert "缓存" in body["note"]


def test_listPlacesFallsBackToLive(placesEnv):
    """字典表**空**时降级成实时聚合，并如实标明来源。"""
    body = placesEnv["client"].get("/api/places").json()
    assert body["source"] == "lib", "还没 rebuild，应走降级路径"
    assert body["dictionaryRebuilt"] is False
    assert body["total"] == 4
    assert [i["photoCount"] for i in body["items"]] == [3, 2, 2, 1]
    assert all(i["dataSource"] == "lib" for i in body["items"])


def test_placeCodeSameOnBothPaths(placesEnv):
    """两条路径下同一地点的 placeCode **必须一致**。

    ⚠️ 否则：用户先在地点下拉里选中了「北京」（降级路径给的编码），
       rebuild 之后编码变了 —— 已缓存的前端状态、收藏的 URL 全部失效，
       而接口不会报任何错。
    """
    client = placesEnv["client"]
    live = {i["placeName"]: i["placeCode"]
            for i in client.get("/api/places").json()["items"]}
    client.post("/api/places/rebuild")
    dicted = {i["placeName"]: i["placeCode"]
              for i in client.get("/api/places").json()["items"]}
    assert live == dicted, "两条路径的 placeCode 不一致: %s vs %s" % (live, dicted)


def test_listPlacesFiltersAndOrder(placesEnv):
    client = placesEnv["client"]
    client.post("/api/places/rebuild")
    # keyword
    got = client.get("/api/places", params={"keyword": "广"}).json()
    assert [i["placeName"] for i in got["items"]] == ["广州"]
    # minPhotoCount
    got = client.get("/api/places", params={"minPhotoCount": 2}).json()
    assert got["total"] == 3
    # 升序（desc=0）
    got = client.get("/api/places", params={"orderBy": "photoCount",
                                            "desc": 0}).json()
    assert [i["photoCount"] for i in got["items"]] == [1, 2, 2, 3]

    # ⚠️ `orderBy=placeName` **缺省也是降序**（desc 默认 1）——
    #    所以这里显式传 desc=1 并断言「等于 Python 的降序」，
    #    把「方向到底有没有生效」这件事说清楚（不传 desc 时容易误解）。
    got = client.get("/api/places", params={"orderBy": "placeName",
                                            "desc": 1}).json()
    names = [i["placeName"] for i in got["items"]]
    assert names == sorted(names, reverse=True), names
    got = client.get("/api/places", params={"orderBy": "placeName",
                                            "desc": 0}).json()
    names = [i["placeName"] for i in got["items"]]
    assert names == sorted(names), names

    # ⚠️ **次级排序固定 ASC，不跟着 desc 翻**：photoCount 相同的上海与广州
    #    必须在两种方向下**都**按名字升序相邻出现。
    #    （早期实现把 desc 应用到每一列，于是"按张数降序"时同名次的地点
    #      反而按名字倒序 —— 分页顺序看起来乱跳，而且前端传 desc=0 时
    #      顺序又会变一遍。）
    for direction in (0, 1):
        got = client.get("/api/places", params={"orderBy": "photoCount",
                                               "desc": direction}).json()
        tied = [i["placeName"] for i in got["items"] if i["photoCount"] == 2]
        assert tied == sorted(tied), \
            "desc=%d 时同张数的地点没有按名字升序: %s" % (direction, tied)

    # 非法排序 -> 400（白名单，不静默回落）
    got = client.get("/api/places", params={"orderBy": "DROP TABLE"})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


def test_listPlacesPaging(placesEnv):
    client = placesEnv["client"]
    client.post("/api/places/rebuild")
    first = client.get("/api/places", params={"size": 2, "page": 1}).json()
    second = client.get("/api/places", params={"size": 2, "page": 2}).json()
    assert first["total"] == second["total"] == 4
    assert len(first["items"]) == 2 and len(second["items"]) == 2
    assert first["hasMore"] is True and second["hasMore"] is False
    names = [i["placeName"] for i in first["items"] + second["items"]]
    assert len(set(names)) == 4, "分页出现重复: %s" % names


def test_listPlacesDoesNotWrite(placesEnv, api_rows):
    """`GET /api/places` 是**纯读** —— **两条路径都不能写**。

    ⚠️ 尤其是**降级路径**：很容易图省事在 GET 里顺手回填字典，
       那等于让只读接口有副作用。缓存了代理、客户端重试、
       预取扫描都会替用户触发写库，且没人能解释数据为什么变了。

    ⚠️ 两条路径要**分别**取证（分段比对，不能把 rebuild 夹在两次快照中间）：
       否则 rebuild 造成的 0 -> 4 会把「GET 有没有写」这个问题盖掉 ——
       第一版就是这么写的，报出来的失败信息指向 GET，其实是 rebuild 写的。
    """
    client = placesEnv["client"]

    def fingerprint():
        return {name: api_rows.count(name)
                for name in ("pb_place", "pb_photo", "pb_review_log")}

    # ---- ① 降级路径（字典还空着）----
    before = fingerprint()
    for _ in range(3):
        body = client.get("/api/places").json()
        assert body["source"] == "lib"
    assert fingerprint() == before, "GET（降级路径）写了库"

    # ---- ② rebuild 之后走字典路径 ----
    client.post("/api/places/rebuild")
    before = fingerprint()
    for _ in range(3):
        body = client.get("/api/places").json()
        assert body["source"] == "dictionary"
    assert fingerprint() == before, "GET（字典路径）写了库"

    # ---- ③ rebuild 是纯派生缓存重建，不该往纠错日志里写 ----
    logs = [str(r.get("opType") or "") for r in api_rows.logs()]
    assert "REBUILD" not in logs and "PLACE" not in logs, logs
    assert "DISABLE" not in logs
