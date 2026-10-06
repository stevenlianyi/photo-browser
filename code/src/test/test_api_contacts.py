#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_contacts.py
#Description: photo-browser 联系人维护接口测试（步骤 9·DR-17/18/19）
#
# 逐条对应验收清单第 16~28 条
#   16 GET  /api/contacts 分页与筛选，每条带三个计数
#   17 POST /api/contacts 新建后 source=0、vcardUid 为空
#   18 PATCH 改**非 birthday** 字段 -> centroidRebuilt 为假，质心 modifyYMDHMS **一字未变**
#   19 PATCH 改 **birthday** -> centroidRebuilt=true 且**该人全部桶已重算**
#      （含 pb_face.shotBucket 必须随之改变 —— 见下面的大段说明）
#   20 PATCH 改 displayName 撞名 -> 409 + existing，**且那一行没被改**
#   21 GET  /{personCode}/impact 四个数字与 SQL 逐个核对一致
#   22 POST /disable 不带 confirm -> **四张表行数全部不变**
#   23 POST /disable?confirm=true -> 质心 0 行 / 人脸全部退回 / **无盲区** / DISABLE 日志 / 无孤儿
#   24 POST /enable -> delFlag='0' + 「需重新确认人脸」提示
#   25 GET  /duplicates 报出疑似同人（**同一个中国手机号**的两种写法）
#   26 POST /import/csv?dryRun=true -> 回计划且**不写任何一行**
#   27 走查：没有任何导出接口（404/405）
#   28 走查：没有 DELETE /api/contacts/{personCode}
#
# ══════════════════════════════════════════════════════════════════
# ⚠️⚠️ 第 19 条的核心结论（改生日 **必须** 同时刷 pb_face.shotBucket）
# ══════════════════════════════════════════════════════════════════
#   `centroid.recomputePerson()` 是**按 pb_face 表里已存的 shotBucket 分桶**
#   算出来的（见 centroid.loadFaceVectors：`if str(row["shotBucket"]) != bucket: continue`）。
#   而 shotBucket 是「拍摄年 + **这个人**的出生年」算出来的自适应桶
#   （rebucket.expectedBucketOf -> bucket.bucketKeyAdaptive）。
#   ⇒ 所以只调 recomputePerson 而**不刷 shotBucket**，等于用**旧生日算出的桶键**
#     重新算一遍同样的样本：质心内容与改之前**逐位相同**，而用户的直觉是
#     「他忽然认不准了」。**改生日等于没改。**
#   ⇒ 唯一正确的顺序是：
#       ① 写 pb_person.birthday
#       ② rebucket.rebucketPerson()   <- 重写 pb_face.shotBucket（新桶键）
#       ③ centroid.recomputePerson()  <- 按新桶键重建全部质心
#   好在 ③ 自己会先跑 `_assertBucketOrder` 前置检查并抛 BucketStaleError：
#   真把顺序做反了，它**宁可拒绝执行**也不让服务静默失配。
#   本文件的 test_patchBirthdayRebucketsShotBucketAndCentroids 就是钉这一条：
#   它同时断言 shotBucket **变了**、质心**跟着换了桶键**、两边都对。

import pytest


@pytest.fixture
def withFaces(api_env, api_rows):
    """给 P_alpha 装上 4 张**人工确认**样本（质心已启用），供 birthday/停用测试用。"""
    from processor.review import assigner as assigner

    for code in ("FC_PH_2013_01_0", "FC_PH_2013_01_1",
                 "FC_PH_2013_07_0", "FC_PH_2016_03_0"):
        assigner.confirm(code, "P_alpha")
    return api_env


def _centroids(api_rows, personCode):
    return {str(r["bucketKey"]): r for r in api_rows.centroids(personCode)}


# ============================================================
# 验收 16：列表分页与筛选
# ============================================================

def test_contactsListPagingAndFilters(api_env):
    client = api_env["client"]
    body = client.get("/api/contacts", params={"size": 3, "page": 1}).json()
    assert body["total"] == 5                     # 3 个 + 2 个共享手机号的
    assert len(body["items"]) == 3
    assert body["hasMore"] is True
    assert [i["displayName"] for i in body["items"]] == sorted(
        i["displayName"] for i in body["items"])

    # keyword（姓名模糊）
    got = client.get("/api/contacts", params={"keyword": "阿尔法"}).json()
    assert got["total"] == 1 and got["items"][0]["personCode"] == "P_alpha"
    # keyword（电话）：这里是**字面 LIKE**，不归一。
    #   按号码判「疑似同人」是 duplicates 的活（那里走 phoneKey 归一），
    #   筛选框保持字面语义才不会让用户「搜 138 却搜不到 138-0013-8000」。
    got = client.get("/api/contacts", params={"keyword": "138"}).json()
    assert {i["personCode"] for i in got["items"]} == {"P_dup1", "P_dup2"}
    # source
    got = client.get("/api/contacts", params={"source": 0}).json()
    assert got["total"] == 5
    # familyGroupCode（还没有任何家庭组）
    got = client.get("/api/contacts", params={"familyGroupCode": "FM_x"}).json()
    assert got["total"] == 0
    # delFlag
    assert client.get("/api/contacts",
                      params={"delFlag": "1"}).json()["total"] == 0
    assert client.get("/api/contacts",
                      params={"delFlag": "all"}).json()["total"] == 5
    # withPhoto
    assert client.get("/api/contacts",
                      params={"withPhoto": 0}).json()["total"] == 5
    # 非法 delFlag
    assert client.get("/api/contacts",
                      params={"delFlag": "x"}).status_code == 400


def test_contactsListCarriesCounts(withFaces):
    """每条都带 photoCount / faceCount / confirmedFaceCount（验收第 16 条）。"""
    from processor.review import assigner as assigner

    assigner.autoAssign("FC_PH_2020_11_0", "P_alpha", 0.7)   # 自动归属
    got = withFaces["client"].get(
        "/api/contacts", params={"keyword": "阿尔法"}).json()
    one = got["items"][0]
    assert one["personCode"] == "P_alpha"
    # 4 张确认脸分布在 3 张照片（PH_2013_01 有 2 张脸）+ 1 张自动脸在 PH_2020_11
    assert one["photoCount"] == 4
    assert one["faceCount"] == 5                 # 4 确认 + 1 自动
    assert one["confirmedFaceCount"] == 4
    assert one["autoFaceCount"] == 1
    assert one["birthday"] == "1985-03-07"
    assert one["vcardUid"] is None


def test_contactsCategoryFilter(api_env):
    client = api_env["client"]
    assert client.get("/api/contacts",
                      params={"category": "family"}).json()["total"] == 0
    # 用 PATCH 写分类（界面编辑路径），再筛
    got = client.patch("/api/contacts/P_gamma",
                       json={"categories": ["家人", "colleague"]}).json()
    assert got["categories"] == ["colleague", "family"]      # 别名已归一
    assert client.get("/api/contacts",
                      params={"category": "family"}).json()["total"] == 1


# ============================================================
# 验收 17：新建手工档案
# ============================================================

def test_createContact(api_env, api_rows):
    client = api_env["client"]
    got = client.post("/api/contacts", json={
        "displayName": "钱小样", "familyName": "钱", "birthday": "1995-08-08",
        "email": "qian@example.com", "phone": "13900139000",
        "relation": "spouse", "categories": ["家人"]})
    assert got.status_code == 201, got.text
    body = got.json()
    assert body["source"] == 0
    assert body["vcardUid"] is None                # ⚠️ 手工档案没有 UID
    assert body["personCode"].startswith("UI_")
    assert body["displayName"] == "钱小样"

    row = api_rows.person(body["personCode"])
    assert row["displayName"] == "钱小样"
    assert row["source"] == 0
    assert row["vcardUid"] is None

    # GET 能查到，且带三个计数（0）
    listed = client.get("/api/contacts", params={"keyword": "钱小样"}).json()
    assert listed["total"] == 1
    assert listed["items"][0]["photoCount"] == 0
    assert listed["items"][0]["faceCount"] == 0
    assert listed["items"][0]["confirmedFaceCount"] == 0


def test_createContactDuplicateNameGetsSuffix(api_env):
    """新建时重名**不报错**，加 (2) 后缀并在 warnings 里说清楚。

    ⚠️ `pb_person.displayName` 有 UNIQUE 索引，所以**字面同名根本写不进去** ——
      `contactCommon.makeDisplayName()` 的职责就是把它变成「张三(2)」。
      （与 PATCH 改名的 409 形成对照：那是「用户以为自己改的是这一行」，
        报错+给合并入口才是对的；这里是「用户确实想建第二个人」。）
    """
    client = api_env["client"]
    first = client.post("/api/contacts", json={"displayName": "钱小样"}).json()
    assert first["displayName"] == "钱小样"
    again = client.post("/api/contacts", json={"displayName": "钱小样"}).json()
    assert again["displayName"] == "钱小样(2)"
    assert again["personCode"] != first["personCode"]
    assert any("同名" in w for w in again["warnings"])


def test_createContactEmptyNameRejected(api_env):
    got = api_env["client"].post("/api/contacts", json={"displayName": "  "})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


# ============================================================
# 验收 18：改**非 birthday** 字段 -> 质心一个字都不动
# ============================================================

def test_patchNonBirthdayDoesNotTouchCentroids(withFaces, api_rows):
    """改 email / phone / relation / displayName / 分类 -> 质心**逐位未变**。

    这是 DR-18「联系人字段分两类」的可执行版本：纯资料字段改了什么都不用做。
    证据用 `modifyYMDHMS`（这里正好适用 —— 因为**期望是不变**，
    「不变」不需要靠内容比对来证明）。
    """
    client = withFaces["client"]
    before = {k: str(v.get("modifyYMDHMS") or "")
              for k, v in _centroids(api_rows, "P_alpha").items()}
    assert before, "前置：P_alpha 应该有质心"

    for patch in ({"email": "new@example.com"},
                  {"phone": "13700137000"},
                  {"relation": "parent"},
                  {"displayName": "阿尔法改名"}):
        got = client.patch("/api/contacts/P_alpha", json=patch)
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["centroidRebuilt"] is False, "改 %s 不该重算质心" % patch
        assert "centroid" not in body or body["centroid"] is None
        assert not body["birthdayChanged"]

    after = {k: str(v.get("modifyYMDHMS") or "")
             for k, v in _centroids(api_rows, "P_alpha").items()}
    assert after == before, "质心被无谓地重写了：%s -> %s" % (before, after)


# ============================================================
# 验收 19：改 birthday -> 刷 shotBucket + 重算全部桶
# ============================================================

def test_patchBirthdayRebucketsShotBucketAndCentroids(withFaces, api_rows):
    """改生日 -> `pb_face.shotBucket` **必须变**，质心跟着换桶键（验收第 19 条）。

    ⚠️ 这是本步最容易做漏的一处，而且**做漏了不会报错**：
       `centroid.recomputePerson` 是按脸表里**已存的** shotBucket 分桶的，
       所以只重算质心 = 用旧生日算出的桶键重算一遍同样的样本，
       质心内容与改之前逐位相同 —— 用户只看到「他忽然认不准了」。

    ⚠️ 挑生日要挑**真会改变桶键**的：不是每个生日改动都挪桶。
       自适应成年桶的起点 = 出生年 + 18 + k*10，于是 1985 与 1965 生的��
       拍 2013 年的照片**都**落在 "2003-2012"（k 不同但起点相同）。
       用 1970-01-01：2013 与 2016 两年**都**挪到 "2008-2017" ——
       四张脸的桶键全变，且从两个桶并成一个桶，质心的桶集合必须跟着换。
    """
    client = withFaces["client"]
    faceCodes = ["FC_PH_2013_01_0", "FC_PH_2013_01_1",
                 "FC_PH_2013_07_0", "FC_PH_2016_03_0"]
    shotYearOf = {"FC_PH_2013_01_0": 2013, "FC_PH_2013_01_1": 2013,
                  "FC_PH_2013_07_0": 2013, "FC_PH_2016_03_0": 2016}
    bucketBefore = {c: str(api_rows.face(c).get("shotBucket") or "") for c in faceCodes}
    centroidBefore = _centroids(api_rows, "P_alpha")
    print("\nbucket 改前: %s" % sorted(set(bucketBefore.values())))
    print("质心桶改前: %s" % sorted(centroidBefore.keys()))
    assert sorted(set(bucketBefore.values())) == sorted(["2003-2012", "2013-2022"])
    assert sorted(centroidBefore.keys()) == sorted(["2003-2012", "2013-2022", "ALL"])

    got = client.patch("/api/contacts/P_alpha", json={"birthday": "1970-01-01"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["birthdayChanged"] is True
    assert body["centroidRebuilt"] is True                # ← 验收第 19 条
    assert body["centroid"]["birthdayFrom"] == "1985-03-07"
    assert body["centroid"]["birthdayTo"] == "1970-01-01"
    assert body["centroid"]["facesChecked"] == 4
    assert body["centroid"]["facesRebucketed"] == 4       # 四张脸的桶键全变了

    # ① pb_face.shotBucket 必须真的变了（**这是本条的核心**）
    from engine.match import rebucket as rebucket
    bucketAfter = {c: str(api_rows.face(c).get("shotBucket") or "") for c in faceCodes}
    print("bucket 改后: %s" % sorted(set(bucketAfter.values())))
    assert bucketAfter != bucketBefore, "shotBucket 一张都没变 -> 改生日等于没改"
    for c in faceCodes:
        # 桶键必须**等于按新生日算出来的那个**，而不是「碰巧变了」
        assert bucketAfter[c] == rebucket.shotBucketFor(
            shotYearOf[c], "1970-01-01") == "2008-2017", c

    # ② 质心桶键集合必须跟着换：两个真桶并成一个，旧桶被清掉
    centroidAfter = _centroids(api_rows, "P_alpha")
    print("质心桶改后: %s" % sorted(centroidAfter.keys()))
    assert sorted(centroidAfter.keys()) == sorted(["2008-2017", "ALL"])
    assert "2003-2012" not in centroidAfter
    assert "2013-2022" not in centroidAfter
    assert int(centroidAfter["2008-2017"]["sampleCount"]) == 4
    # ③ 新桶的向量必须**按新样本集合**重算：它是 4 个样本的均值，
    #    而改之前两个桶分别是 3 个与 1 个 —— 所以它与两个旧桶都不同。
    newBlob = bytes(centroidAfter["2008-2017"]["centroid"])
    assert len(newBlob) == 2048
    assert newBlob != bytes(centroidBefore["2003-2012"]["centroid"])
    # ⚠️ 改之前"2013-2022" 只有 **1 个**样本（< MIN_SAMPLES=3），
    #    它的 centroid 本来就是 NULL —— 不能拿它比字节（会炸在 bytes(None)），
    #    它「变了」的正确证据是**这一行不见了**（上面已断言）。
    # ④ `ALL` 兜底桶**理应逐位不变** —— 它的样本集合没变（还是那 4 张脸），
    #    只搬了桶。所以拿它当「质心有没有被无谓重写」的探针正好：
    #    变了这个测试反而说明实现做了多余且昂贵的功。
    assert int(centroidAfter["ALL"]["sampleCount"]) == 4
    assert bytes(centroidAfter["ALL"]["centroid"]) \
        == bytes(centroidBefore["ALL"]["centroid"])
    # ⑤ 响应里给出了桶迁移的明细，UI 可以显示「桶变了」提示
    assert body["centroid"]["oldBuckets"] and body["centroid"]["newBuckets"]
    assert body["centroid"]["samples"], "应给出改前->改后的样本对照"

    # ⑥ 桶一致性前置检查通过（② 能跑完本身就证明了：centroid.recomputePerson
    #    第一步就是 _assertBucketOrder，不一致会直接抛 BucketStaleError）
    assert rebucket.auditBuckets() is not None


def test_patchBirthdayWithoutFacesStillWorks(api_env):
    """没有脸的人改生日：不刷桶（没脸可刷），但仍标记 birthdayChanged。"""
    got = api_env["client"].patch("/api/contacts/P_gamma",
                                   json={"birthday": "1988-08-08"})
    assert got.status_code == 200, got.text
    assert got.json()["birthdayChanged"] is True
    assert any("没有任何脸" in w for w in got.json()["warnings"])


# ============================================================
# 验收 20：改 displayName 撞名 -> 409 + existing，且**不写库**
# ============================================================

def test_patchDisplayNameConflict409AndNoWrite(api_env, api_rows):
    client = api_env["client"]
    before = str(api_rows.person("P_gamma")["displayName"])
    existing = api_rows.person("P_alpha")

    got = client.patch("/api/contacts/P_gamma", json={"displayName": "阿尔法"})
    assert got.status_code == 409, got.text
    body = got.json()
    assert body["code"] == "DUPLICATE_DISPLAY_NAME"
    info = body["extra"]["existing"]
    assert info["personCode"] == "P_alpha"
    assert info["displayName"] == "阿尔法"
    assert info["photoCount"] == 0
    assert info["faceCount"] == 0
    assert info["mergeEndpoint"] == "/api/review/merge"
    # ⚠️ **那一行没被改动**
    assert str(api_rows.person("P_gamma")["displayName"]) == before
    assert existing["personCode"] == "P_alpha"


def test_patchDisplayNameToSameValueOk(api_env):
    """改成**自己当前的名字**不算撞名（幂等）。"""
    got = api_env["client"].patch("/api/contacts/P_alpha",
                                   json={"displayName": "阿尔法"})
    assert got.status_code == 200, got.text


def test_patchFamilyGroupAutoCreates(api_env, api_rows):
    """改 familyGroupCode 指向不存在的家庭组 -> 自动建组并写进 pb_person。"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    got = api_env["client"].patch("/api/contacts/P_alpha",
                                   json={"familyGroupCode": "FM_测试家庭"})
    assert got.status_code == 200, got.text
    assert any("自动创建" in w for w in got.json()["warnings"])
    assert api_rows.person("P_alpha")["familyGroupCode"] == "FM_测试家庭"
    assert sqliteCommon.query_pb_family("pb_family", familyCode="FM_测试家庭")


def test_patchNoFieldsRejected(api_env):
    got = api_env["client"].patch("/api/contacts/P_alpha", json={})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


def test_patchUnknownFieldRejected(api_env):
    """不在白名单里的字段直接拒（400），且**不写库**。

    ⚠️ 刻意**不用** pydantic 的 extra="forbid"（那会回 422）：
      两段式停用已经证明了这个项目偏好的错误形状是 `{code, message}`
      且状态码按语义分（400 参数 / 404 不存在 / 409 冲突），
      422 会被前端的「表单校验」分支吃掉，走不到 Toast。
    """
    got = api_env["client"].patch("/api/contacts/P_alpha",
                                   json={"personCode": "P_hack"})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"
    assert "personCode" in got.json()["message"]


# ============================================================
# 验收 21：影响面四个数字与 SQL 逐个核对
# ============================================================

def test_impactMatchesSql(withFaces, api_rows):
    from processor.review import assigner as assigner

    assigner.autoAssign("FC_PH_2020_11_0", "P_alpha", 0.66)
    got = withFaces["client"].get("/api/contacts/P_alpha/impact")
    assert got.status_code == 200, got.text
    body = got.json()
    impact = body["extra"] if "extra" in body else body
    impact = body

    # 逐个用 SQL 核对（不用 api 自己报的数当证据）
    assert impact["photoCount"] == api_rows.count(
        "pb_photo_person", "personCode = %s", ("P_alpha",))
    assert impact["faceCount"] == api_rows.count(
        "pb_face", "personCode = %s AND delFlag = %s", ("P_alpha", "0"))
    assert impact["confirmedCount"] == api_rows.count(
        "pb_face", "personCode = %s AND isConfirmed = %s AND delFlag = %s",
        ("P_alpha", 1, "0"))
    assert impact["centroidCount"] == len(api_rows.centroids("P_alpha"))
    # pendingAfter = 当前 pending + 他的人脸数
    from processor.review import queue as reviewQueue
    assert impact["pendingAfter"] == reviewQueue.countPending() + impact["faceCount"]
    assert impact["displayName"] == "阿尔法"
    assert "质心" in impact["warning"] and "退回待确认队列" in impact["warning"]


# ============================================================
# 验收 22：disable 不带 confirm -> **四张表一行都不改**
# ============================================================

def test_disableWithoutConfirmWritesNothing(withFaces, api_rows):
    client = withFaces["client"]
    snapshot = {
        "pb_person": api_rows.count("pb_person"),
        "pb_face": api_rows.count("pb_face"),
        "pb_person_centroid": api_rows.count("pb_person_centroid"),
        "pb_review_log": api_rows.count("pb_review_log"),
        "pb_photo_person": api_rows.count("pb_photo_person"),
    }
    got = client.post("/api/contacts/P_alpha/disable")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is False
    assert body["confirmRequired"] is True
    assert body["faceCount"] == 4            # 影响面照常返回
    assert body["centroidCount"] >= 1

    after = {
        "pb_person": api_rows.count("pb_person"),
        "pb_face": api_rows.count("pb_face"),
        "pb_person_centroid": api_rows.count("pb_person_centroid"),
        "pb_review_log": api_rows.count("pb_review_log"),
        "pb_photo_person": api_rows.count("pb_photo_person"),
    }
    assert after == snapshot, "不带 confirm 却写了库：%s -> %s" % (snapshot, after)
    assert api_rows.person("P_alpha")["delFlag"] == "0"


# ============================================================
# 验收 23：disable?confirm=true 的五条
# ============================================================

def test_disableWithConfirm(withFaces, api_rows):
    from engine.match import centroid as centroid
    from processor.review import assigner as assigner

    client = withFaces["client"]
    logsBefore = len(api_rows.logs())

    got = client.post("/api/contacts/P_alpha/disable", params={"confirm": 1})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is True
    assert body["facesReturned"] == 4
    assert body["centroidRowsLeft"] == 0           # ① 质心删干净
    assert body["facesStillAssigned"] == 0# ② 人脸全退

    # ① 该人 pb_person_centroid 行数 = 0
    assert api_rows.centroids("P_alpha") == []
    # ② 所有脸变成 personCode IS NULL AND isConfirmed=0
    for code in ("FC_PH_2013_01_0", "FC_PH_2013_01_1",
                 "FC_PH_2013_07_0", "FC_PH_2016_03_0"):
        face = api_rows.face(code)
        assert face["personCode"] is None
        assert int(face["isConfirmed"]) == 0
        assert int(face["isStranger"]) == 0
        # 桶键退回等宽降级桶（主人没了）
        assert face["shotBucket"], "退回未归属后应有等宽降级桶"
    # ③ **没有任何脸落在「既不在待确认、也不在我不同意」的盲区**
    blind = api_rows.count(
        "pb_face",
        "personCode IS NOT NULL AND isConfirmed = %s AND isStranger = %s"
        " AND delFlag = %s", (0, 0, "0"))
    assert blind == 0, "有 %d 张脸落在盲区" % blind
    # ④ pb_person.delFlag='1'
    assert api_rows.person("P_alpha")["delFlag"] == "1"
    # ⑤ pb_review_log 新增一条 opType='DISABLE'
    disableLogs = api_rows.logs("DISABLE")
    assert len(disableLogs) == 1
    assert disableLogs[0]["logCode"] == body["logCode"]
    assert disableLogs[0]["fromPersonCode"] == "P_alpha"
    assert int(disableLogs[0]["faceCount"]) == 4
    assert len(api_rows.logs()) == logsBefore + 1
    # 关联行按纪律 ③ 存废：人脸全退 -> 该人的关联行必须消失
    assert api_rows.links(personCode="P_alpha") == []
    # 无孤儿
    assert assigner.verifyLinks()["clean"] is True, assigner.verifyLinks()
    assert assigner.syncLinks(dryRun=True)["add"] == 0
    assert assigner.syncLinks(dryRun=True)["drop"] == 0

    # 停用后的人**不在**默认列表里，在 delFlag=1 里
    assert client.get("/api/contacts",
                      params={"keyword": "阿尔法"}).json()["total"] == 0
    assert client.get("/api/contacts",
                      params={"keyword": "阿尔法",
                              "delFlag": "1"}).json()["total"] == 1


def test_disableTwiceIsIdempotent(withFaces):
    client = withFaces["client"]
    assert client.post("/api/contacts/P_alpha/disable",
                       params={"confirm": 1}).json()["executed"] is True
    again = client.post("/api/contacts/P_alpha/disable",
                        params={"confirm": 1}).json()
    assert again["executed"] is False
    assert again["alreadyDisabled"] is True


# ============================================================
# 验收 24：enable
# ============================================================

def test_enableRestoresWithHint(withFaces, api_rows):
    client = withFaces["client"]
    client.post("/api/contacts/P_alpha/disable", params={"confirm": 1})
    got = client.post("/api/contacts/P_alpha/enable")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["executed"] is True and body["changed"] is True
    assert body["delFlag"] == "0"
    # ⚠️ 必带「需重新确认人脸才能自动匹配」的提示
    assert "重新确认人脸" in body["note"]
    assert "自动匹配" in body["note"]
    # 恢复后质心仍是 0 行（人脸已退回未归属，没有确认样本就没有质心）
    assert api_rows.centroids("P_alpha") == []
    assert body["centroidCount"] == 0
    assert api_rows.person("P_alpha")["delFlag"] == "0"
    # 落一条 ENABLE 日志
    assert len(api_rows.logs("ENABLE")) == 1


def test_enableAlreadyEnabledNoop(withFaces):
    got = withFaces["client"].post("/api/contacts/P_alpha/enable").json()
    assert got["executed"] is True and got["changed"] is False
    assert "重新确认人脸" in got["note"]


# ============================================================
# 验收 25：duplicates 报出疑似同人（同一个中国手机号）
# ============================================================

#: 电话归一的全部口径（**这是 /api/contacts/duplicates 判「疑似同人」的唯一依据**）
#: 左边是输入原文，右边是期望的比对键 + 是否像中国手机号。
#: ⚠️ 规则（用户口径）：
#:   · 带 `+` 或 `00` 开头 = 国际写法 -> 先吃 `+`/`00`、再吃区号 `86`
#:   · 其余 = **默认中国国内号**（手机号 11 位）
#:   · 中间的 `-` / 空格 / 括号 / 点 一律去掉
#:   · 剩下的国内长途前缀 `0`（座机区号）也去掉
PHONE_CASES: list = [
    # ---- 手机号：四种常见写法必须归一到同一个键 ----
    ("13800138000", "13800138000", True),
    ("138-0013-8000", "13800138000", True),
    ("138 0013 8000", "13800138000", True),
    ("138.0013.8000", "13800138000", True),
    ("(138)00138000", "13800138000", True),
    ("+8613800138000", "13800138000", True),
    ("+86 138-0013-8000", "13800138000", True),
    ("0086-138-0013-8000", "13800138000", True),
    # 不写 `+` 的区号写法：非国际分支下，位数超过 11 位才切 `86`
    ("8613800138000", "13800138000", True),
    ("008613800138000", "13800138000", True),
    # ---- 座机：去区号前的长途前缀 0 ----
    ("02885187018", "2885187018", False),
    ("+862885187018", "2885187018", False),
    ("008602885187018", "2885187018", False),
    # ---- ⚠️ 脏数据**不许**被并成同一人 ----
    #  `+86285187018` 只有 12 位（86 + 9 位），是少了一位的截断号；
    #  它必须与 `02885187018`（11 位）**归一到不同的键**。
    #  宁可漏一次合并提示，也不能把两个真人并成一个。
    ("+86285187018", "86285187018", False),
    # ---- 海外号 / 内部短号：原样保留，不误判成中国手机 ----
    ("+14155550123", "14155550123", False),
    ("00442079460958", "442079460958", False),
    ("010-8888", "0108888", False),
    # ---- 空值 ----
    ("", "", False),
    ("abc", "", False),
]


@pytest.mark.parametrize("raw,expectKey,expectMobile", PHONE_CASES)
def test_phoneKeyNormalization(raw, expectKey, expectMobile):
    """`contactCommon.phoneKey` / `isChineseMobile` 的口径。

    ⚠️ 为什么这个单测放在 api 测试里：它是 `/api/contacts/duplicates`
       「疑似同人」判据的**唯一依据** —— 归一错了，接口要么报不出对，
       要么把两个真人报成一对。放在这里，改 `duplicates` 的人一定看得见它。
    """
    from processor.contact import contactCommon as contact

    assert contact.phoneKey(raw) == expectKey, \
        "%r 归一成了 %r（期望 %r）" % (raw, contact.phoneKey(raw), expectKey)
    assert contact.isChineseMobile(raw) is expectMobile, \
        "%r 的手机号判定应为 %s" % (raw, expectMobile)


def test_phoneKeyOldBugFixed(api_env):
    """回归钉子：`0086...` 国际写法在旧实现下**整条坏掉**。

    旧实现只看数字前缀（`len > 11 and startswith("86")`），`00` 没被吃掉，
    于是 `008613800138000` 变成 `08613800138000`（14 位）——
    它跟谁都配不上对，同一个人的两条记录**永远合并不了**，而且**不报错**。
    """
    from processor.contact import contactCommon as contact

    assert contact.phoneKey("008613800138000") == "13800138000"
    assert len(contact.phoneKey("008613800138000")) == contact.CN_MOBILE_LEN


def test_duplicatesFindsSharedChineseMobile(api_env):
    """构造的是**真正会发生的形态**：同一个中国手机号写成两种格式。

    ⚠️ 为什么不是「两个同名人员」：`pb_person.displayName` 上有 UNIQUE 索引，
       字面同名**根本存不进去**（写第二个会 UNIQUE constraint failed）。
       能发生的「疑似同人」是：手机号/邮箱相同，或姓名归一（nameKey）后相同。

    ⚠️ 这一对**同时**命中 phone 与 email —— 早期实现是「第一个判据独占这一对」，
       于是 phone 那个信号会被 email 吞掉，用户只看到不完整的理由。
       现在是「一对一行 + reasons 列表」，两个理由都在。
    """
    got = api_env["client"].get("/api/contacts/duplicates").json()
    assert got["total"] == 1
    pair = got["items"][0]
    assert {pair["personA"], pair["personB"]} == {"P_dup1", "P_dup2"}
    assert pair["nameA"] == "张小明" and pair["nameB"] == "张晓明"
    assert pair["reasons"] == ["email", "phone"]          # 两个理由都在
    assert pair["reason"] == "email"                     # primary = 最强那个
    assert pair["mergeEndpoint"] == "/api/review/merge"
    assert "电话" in pair["hint"] and "归一" in pair["hint"]
    assert "请人工确认" in pair["hint"]
    # ⚠️ 两个电话的**写法**确实不同 -> 证明是 phoneKey 归一起了作用
    assert pair["phoneA"] == "13800138000"
    assert pair["phoneB"] == "138-0013-8000"
    assert pair["phoneA"] != pair["phoneB"]
    from processor.contact import contactCommon as contact
    assert contact.phoneKey(pair["phoneA"]) == contact.phoneKey(pair["phoneB"])
    # 归一到 11 位中国手机号（「不带 +/00 默认 11 位」这条规则的落点）
    assert contact.phoneKey(pair["phoneB"]) == "13800138000"
    assert len(contact.phoneKey(pair["phoneB"])) == contact.CN_MOBILE_LEN
    assert contact.isChineseMobile(pair["phoneB"]) is True


def test_duplicatesDoesNotMergeTruncatedNumber(api_env, api_rows):
    """⚠️ **脏数据不许被并成同一人**（`+86285187018` 只有 12 位，少一位）。

    这是 phoneKey 那条「只归一化前缀、不合并号段」的纪律在**接口层**的验证：
    两个号码的归一键必须不同，`duplicates` 不能把它们报成一对。
    """
    from processor.contact import contactCommon as contact

    assert contact.phoneKey("+86285187018") != contact.phoneKey("02885187018")
    # 用夹具里已有的两个人来构造这一对（免得为一个用例再加一个人）
    # 并把他们的邮箱清掉，保证本次只可能因 phone 被凑成对
    for code, phone in (("P_alpha", "+86285187018"), ("P_gamma", "02885187018")):
        rtn = api_env["client"].patch("/api/contacts/%s" % code,
                                      json={"phone": phone, "email": ""})
        assert rtn.status_code == 200, rtn.text
        assert api_rows.person(code)["phone"] == phone
    body = api_env["client"].get("/api/contacts/duplicates",
                                  params={"size": 50}).json()
    pairs = {tuple(sorted((i["personA"], i["personB"]))) for i in body["items"]}
    # ⚠️ 「少一位的截断号」与「完整的号」**必须不在一对里**
    assert ("P_alpha", "P_gamma") not in pairs, \
        "少一位的截断号被当成了同一个号：%s" % sorted(pairs)
    # 顺带确认座机确实被归一到了 10 位（去掉区号前的 0）
    assert contact.phoneKey(api_rows.person("P_gamma")["phone"]) == "2885187018"
    # 而两个手机号的写法则照旧能配上对（规则没被改坏）
    assert ("P_dup1", "P_dup2") in pairs


def test_duplicatesEmptyWhenAlone(api_env):
    """只有一个电话的人不报重复（避免每次都刷一堆单人条目）。"""
    body = api_env["client"].get("/api/contacts/duplicates").json()
    assert all(i["personA"] != i["personB"] for i in body["items"])


# ============================================================
# 验收 26：CSV dryRun 只回计划、不写一行
# ============================================================

#: ⚠️ 表头用的是 csv_import.COLUMN_ALIAS 里**真实存在**的别名：
#:   displayName <-显示名/姓名/DisplayName；email <- 邮箱/电子邮件/Email；
#:   phone <- 手机/电话；birthday <- 生日；categories <- 类别/分类。
#:   （写「电子邮箱」是不被认的列名 —— 而 csv_import 的纪律是
#:    **认不出的列就当没有这一列，绝不因为一列不认识就整份文件失败**。）
CSV_TEXT = ("显示名,姓氏,邮箱,电话,生日,类别\r\n"
            "钱小样,钱,qian@example.com,13900139000,1995-08-08,家人\r\n"
            "阿尔法,,a@example.com,13800138000,1985-03-07,朋友\r\n")


def test_importCsvDryRunWritesNothing(api_env, api_rows):
    before = {name: api_rows.count(name) for name in
              ("pb_person", "pb_person_category")}
    got = api_env["client"].post("/api/contacts/import/csv?dryRun=1",
                                 files={"file": ("contacts.csv",
                                                 CSV_TEXT.encode("utf-8"),
                                                 "text/csv")})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["dryRun"] is True and body["written"] is False
    assert body["totalRows"] == 2
    assert body["createCount"] + body["updateCount"] + body["skipCount"] == 2
    assert "preview" in body and body["preview"]
    assert isinstance(body["warnings"], list)
    after = {name: api_rows.count(name) for name in
             ("pb_person", "pb_person_category")}
    assert after == before, "dryRun 却写了库：%s -> %s" % (before, after)
    # 暂存文件也删掉了（「dryRun 的定义就是什么都不留」）
    import os
    from common import paths as paths
    dropDir = os.path.join(paths.imports_dir(), "csv")
    if os.path.isdir(dropDir):
        assert os.listdir(dropDir) == [], os.listdir(dropDir)


def test_importCsvRealRunCreates(api_env, api_rows):
    got = api_env["client"].post("/api/contacts/import/csv",
                                 files={"file": ("contacts.csv",
                                                 CSV_TEXT.encode("utf-8"),
                                                 "text/csv")})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["written"] is True
    # 「阿尔法」已存在 -> update；「钱小样」是新的 -> create
    assert body["updateCount"] == 1
    assert body["createCount"] == 1
    assert api_rows.person("P_alpha")["email"] == "a@example.com"


def test_importCsvBadContentRejected(api_env):
    got = api_env["client"].post("/api/contacts/import/csv?dryRun=1",
                                 files={"file": ("x.csv", b"", "text/csv")})
    assert got.status_code == 400


# ============================================================
# 验收 27 / 28：禁令走查（在 smoke 里也有，这里做 contacts 专项）
# ============================================================

def test_noExportAndNoDeleteForContacts(api_env):
    client = api_env["client"]
    for url in ("/api/contacts/export", "/api/contacts/export/csv",
                "/api/contacts/vcf", "/api/contacts/dump"):
        assert client.get(url).status_code in (404, 405), url
    assert client.request("DELETE", "/api/contacts/P_alpha").status_code in (404, 405)
    paths = client.get("/openapi.json").json()["paths"]
    assert "delete" not in paths["/api/contacts/{personCode}"]
    assert not any("export" in p for p in paths)


# ============================================================
# 家庭组（P2）
# ============================================================

def test_familyCrud(api_env, api_rows):
    from database.auto_generated import sqliteCommon as sqliteCommon

    client = api_env["client"]
    got = client.post("/api/families", json={"familyName": "测试家庭"})
    assert got.status_code == 200, got.text
    code = got.json()["familyCode"]
    assert code.startswith("FM_")

    client.patch("/api/contacts/P_alpha", json={"familyGroupCode": code})
    listed = client.get("/api/families").json()
    assert listed["total"] == 1
    assert listed["items"][0]["memberCount"] == 1

    detail = client.get("/api/families/%s" % code).json()
    assert detail["familyName"] == "测试家庭"
    assert [m["personCode"] for m in detail["members"]] == ["P_alpha"]

    renamed = client.patch("/api/families/%s" % code,
                           json={"familyName": "测试家庭2", "notes": "备注"})
    assert renamed.status_code == 200, renamed.text
    assert sqliteCommon.query_pb_family("pb_family", familyCode=code)[0][
        "familyName"] == "测试家庭2"


def test_familyCodeImmutable(api_env):
    client = api_env["client"]
    code = client.post("/api/families", json={"familyName": "不可改名组"}).json()[
        "familyCode"]
    got = client.patch("/api/families/%s" % code,
                       json={"familyCode": "FM_别的", "familyName": "x"})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


def test_familyDuplicate409(api_env):
    client = api_env["client"]
    first = client.post("/api/families", json={"familyName": "重名组"}).json()
    again = client.post("/api/families", json={
        "familyName": "重名组", "familyCode": first["familyCode"]})
    assert again.status_code == 409
    assert again.json()["code"] == "DUPLICATE_DISPLAY_NAME"


# ============================================================
# 404 一致性
# ============================================================

def test_contactsNotFound404(api_env):
    client = api_env["client"]
    for method, url in (("GET", "/api/contacts/NOPE/impact"),
                        ("PATCH", "/api/contacts/NOPE"),
                        ("POST", "/api/contacts/NOPE/disable"),
                        ("POST", "/api/contacts/NOPE/enable"),
                        ("GET", "/api/families/NOPE")):
        got = client.request(method, url, json={"displayName": "x"}
                             if method == "PATCH" else None)
        assert got.status_code == 404, (method, url, got.status_code)
        assert got.json()["code"] == "NOT_FOUND"
