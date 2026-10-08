#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_person_avatar.py
#Description: R7 人物头像接口测试（DR-40 代表脸回退 / DR-41 默认头像）
#
# 逐条对应 R7 的验收清单
#   ① 一页 24 人的卡片都有脸可显示（`coverFaceCode` 非空）
#   ② **没有任何脸**的人 `coverFaceCode` / `thumbUrl` 都是 None，接口 200 不报错
#   ③ 最优那张被软删 -> 自动降级到下一张（前端才不会出现破图）
#   ④ 代表脸**一次批量取**，不是逐人 N+1
#   ⑤ PATCH `avatarFaceCode`：设 / 清都只动 `pb_person` 一列
#   ⑥ 传**别人的** faceCode -> 400，且库里连 `modifyYMDHMS` 都没动
#   ⑦ 传不存在的 faceCode -> 404（与「编码查不到」同构），不是 500 也不静默成功
#   ⑧ **机器认的（未确认）**样本也能设默认 —— 头像是展示，不是归属
#   ⑨ 默认那张脸被移除后：`avatarFaceCode` **留着不动**、展示层自动降级（DR-41 ④）
#   ⑩ 四处接线的另外两处（`/api/contacts` 列表、`/api/places/{code}/persons`）
#      也要带同一口径的头像
#
# ⚠️ 为什么代表脸的选择要说清「人工确认优先」而不是「detScore 最高」
#   质心只由确认样本生成（DR-16③），确认过的脸是**用户亲自核对过**的；
#   detScore/quality 只说明「脸大不大、清不清楚」。一个高分但用户已经
#   否决过的人脸，拿它当封面等于把用户的操作当没发生。
#   所以顺序是 `isConfirmed DESC` → `detScore` → `quality` → `regYMDHMS`。
#
# ⚠️ 为什么「失效不清理」要单独测（⑨）
#   这是 DR-41 ④ 的**刻意选择**：把清理逻辑铺进 `fix` / `merge` 会把
#   「头像」与「归属」两个正交的东西耦合起来，而回退链本来就必须存在
#   （首次浏览时 `avatarFaceCode` 就是空的）。这条用例就是钉它没有被误改。

import pytest


# ============================================================
# 装置与工具
# ============================================================

@pytest.fixture
def assigner():
    from processor.review import assigner as assignerMod
    return assignerMod


def _confirm(assignerMod, faceCode, personCode):
    """人工确认一张脸（`isConfirmed=1`，会进质心）。"""
    return assignerMod.confirm(faceCode, personCode)


def _personOf(client, personCode):
    got = client.get("/api/persons/%s" % personCode)
    assert got.status_code == 200, got.text
    return got.json()


def _softDeleteFace(faceCode):
    """把一张脸软删（`delFlag='1'`）—— 代表脸必须跳过它。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    sqliteCommon.updateTableGeneral("pb_face", "faceCode = %s", (faceCode,),
                                    {"delFlag": "1"})


def _addPlace(placeCode, placeName):
    """给「地点 → 在场的人」用例造一条地点字典行（聚合键 = placeName）。"""
    from database.auto_generated import sqliteCommon as sqliteCommon
    row = {"placeCode": placeCode, "placeName": placeName, "source": 0,
           "photoCount": 1, "centerLat": None, "centerLon": None, "regID": "test"}
    sqliteCommon.insertManyTableGeneral(
        "pb_place", [row], conflictColumns=("placeCode",),
        updateColumns=("placeName", "photoCount"), fillStandard=True,
        forceColumns=("centerLat", "centerLon"))


# ============================================================
# 一、A 半：代表脸回退（DR-40）
# ============================================================

def test_coverFaceFallsBackToConfirmedFace(api_env, assigner):
    """① 没设过默认 -> 卡片用代表脸，且**人工确认优先于同分的自动归属**。

    `FC_PH_2013_01_0`（确认）与 `FC_PH_2020_11_0`（自动）的 detScore 都是 0.9，
    所以这一条真的在考「确认优先」那一维，而不是顺带被 detScore 选中。
    """
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    assigner.autoAssign("FC_PH_2020_11_0", "P_alpha", 0.7)

    one = _personOf(client, "P_alpha")
    assert one["avatarFaceCode"] is None                      # 用户从没设过
    assert one["coverFaceCode"] == "FC_PH_2013_01_0"          # 确认的那张赢
    assert one["thumbUrl"] == "/api/face/FC_PH_2013_01_0"


def test_coverFacePrefersHigherDetScoreInSameTier(api_env, assigner):
    """同一档里（都是人工确认）取 detScore / quality 更高的那张。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_1", "P_beta")            # detScore 0.8
    _confirm(assigner, "FC_PH_2013_01_0", "P_beta")            # detScore 0.9
    one = _personOf(client, "P_beta")
    assert one["coverFaceCode"] == "FC_PH_2013_01_0"


def test_coverFaceIsNullForPersonWithoutFaces(api_env):
    """② 一个脸都没有的人：两个字段都是 None，接口**不报错**。

    这是「刚导入的联系人」的常态（正式库 2029 人里绝大多数如此），
    前端靠它退回首字母 —— 所以服务端必须给出一个**明确的空**，
    而不是 404、也不是编一个不存在的 faceCode。
    """
    client = api_env["client"]
    one = _personOf(client, "P_dup1")
    assert one["coverFaceCode"] is None
    assert one["thumbUrl"] is None
    assert one["faceCount"] == 0

    items = client.get("/api/persons", params={"keyword": "张小明"}).json()["items"]
    assert items and items[0]["thumbUrl"] is None


def test_coverFaceSkipsSoftDeletedFace(api_env, assigner):
    """③ 最优那张被软删 -> 降级到下一张（否则前端就是一个必然 404 的 URL）。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_gamma")
    _confirm(assigner, "FC_PH_2013_01_1", "P_gamma")
    assert _personOf(client, "P_gamma")["coverFaceCode"] == "FC_PH_2013_01_0"
    _softDeleteFace("FC_PH_2013_01_0")
    assert _personOf(client, "P_gamma")["coverFaceCode"] == "FC_PH_2013_01_1"


def test_coverFacesFetchedInOneBatchQuery(api_env, monkeypatch):
    """④ 代表脸**一次批量取**（N+1 会随页大小线性增长）。

    只认 `FROM pb_face g`（代表脸那条相关子查询的别名）——
    `personStatsOf` 也查 `pb_face`，按表名计数会把它一起算进来。
    """
    from api import browse as browseApi

    calls = []
    real = browseApi.query.selectList

    def _spy(sql, *args, **kwargs):
        calls.append(sql)
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(browseApi.query, "selectList", _spy)
    client = api_env["client"]

    for size in (1, 24):
        calls.clear()
        client.get("/api/persons", params={"size": size})
        cover = [s for s in calls if "FROM pb_face g" in s]
        assert len(cover) == 1, "size=%d 时代表脸查询了 %d 次" % (size, len(cover))
        # 没人设过默认头像时，**连「默认那张还活着吗」的 IN 查询都不发生**
        assert not [s for s in calls if "f.faceCode IN" in s]


def test_staleDefaultAvatarFallsBackToCoverFace(api_env, assigner):
    """③ 默认头像那张脸被软删后：`avatarFaceCode` **原样留着**（DR-41④ 不清库），
    但 `coverFaceCode` 必须降级 —— 否则卡片指向一个必然 404 的图（破图）。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    _confirm(assigner, "FC_PH_2013_01_1", "P_alpha")
    client.patch("/api/contacts/P_alpha", json={"avatarFaceCode": "FC_PH_2013_01_0"})
    assert _personOf(client, "P_alpha")["coverFaceCode"] == "FC_PH_2013_01_0"

    _softDeleteFace("FC_PH_2013_01_0")
    one = _personOf(client, "P_alpha")
    assert one["avatarFaceCode"] == "FC_PH_2013_01_0"      # 刻意不清库
    assert one["coverFaceCode"] == "FC_PH_2013_01_1"       # 但展示降级
    assert one["thumbUrl"] == "/api/face/FC_PH_2013_01_1"


def test_staleDefaultAvatarOwnedByAnotherPersonFallsBack(api_env, assigner):
    """③ 默认那张脸**事后换了主人**（合并/拆分漂移）同样要降级。

    ⚠️ 这里直接写库造这个状态是**故意的**：接口层已经拦住了「把别人的脸
       设成头像」，但合并/拆分会让一个**曾经合法**的默认值漂移；DR-41④
       选择不在写路径上清理，那展示层就必须兜住 —— 判据是「还在不在他名下」，
       而不是只看 `delFlag`。
    """
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    assigner.autoAssign("FC_PH_2013_07_0", "P_beta", 0.6)

    from database.auto_generated import sqliteCommon as sqliteCommon
    sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", ("P_alpha",),
                                    {"avatarFaceCode": "FC_PH_2013_07_0"})
    one = _personOf(client, "P_alpha")
    assert one["avatarFaceCode"] == "FC_PH_2013_07_0"
    assert one["coverFaceCode"] == "FC_PH_2013_01_0"       # 降级到自己的代表脸


def test_contactsListCarriesCoverFace(api_env, assigner):
    """⑩ `/api/contacts` 列表也是同一口径（联系人页与人物库不能两副面孔）。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    items = client.get("/api/contacts", params={"keyword": "阿尔法"}).json()["items"]
    assert items[0]["avatarFaceCode"] is None
    assert items[0]["coverFaceCode"] == "FC_PH_2013_01_0"
    assert items[0]["thumbUrl"] == "/api/face/FC_PH_2013_01_0"


def test_placePersonsCarryCoverFace(api_env, assigner):
    """⑩ 地点「在场的人」也要带（四处接线里最容易漏的一处）。"""
    client = api_env["client"]
    _addPlace("PL_Beijing", "北京")
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")

    body = client.get("/api/places/PL_Beijing/persons").json()
    assert body["personTotal"] == 1
    one = body["items"][0]
    assert one["personCode"] == "P_alpha"
    assert one["coverFaceCode"] == "FC_PH_2013_01_0"
    assert one["thumbUrl"] == "/api/face/FC_PH_2013_01_0"


# ============================================================
# 二、B 半：默认头像的写入口（DR-41）
# ============================================================

def test_patchAvatarSetsThenClears(api_env, api_rows, assigner):
    """⑤ 设 / 清默认头像：只动一列，**质心与 review 日志一概不动**。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    _confirm(assigner, "FC_PH_2013_01_1", "P_alpha")

    centroidBefore = {str(r["bucketKey"]): str(r["modifyYMDHMS"])
                      for r in api_rows.centroids("P_alpha")}
    logsBefore = len(api_rows.logs())

    got = client.patch("/api/contacts/P_alpha",
                       json={"avatarFaceCode": "FC_PH_2013_01_1"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["changedFields"] == ["avatarFaceCode"]
    assert body["centroidRebuilt"] is False
    assert body["contact"]["avatarFaceCode"] == "FC_PH_2013_01_1"
    assert body["contact"]["coverFaceCode"] == "FC_PH_2013_01_1"
    assert api_rows.person("P_alpha")["avatarFaceCode"] == "FC_PH_2013_01_1"
    # 头像是展示：质心一行都没被重写，也没有新增纠错日志
    assert {str(r["bucketKey"]): str(r["modifyYMDHMS"])
            for r in api_rows.centroids("P_alpha")} == centroidBefore
    assert len(api_rows.logs()) == logsBefore

    # 清空 -> 回到代表脸（确认段里 detScore 最高的那张）
    got = client.patch("/api/contacts/P_alpha", json={"avatarFaceCode": ""})
    assert got.status_code == 200, got.text
    assert got.json()["contact"]["avatarFaceCode"] is None
    assert got.json()["contact"]["coverFaceCode"] == "FC_PH_2013_01_0"
    assert api_rows.person("P_alpha")["avatarFaceCode"] is None


def test_patchAvatarAcceptsAutoAssignedFace(api_env, assigner):
    """⑧ **机器认的（未确认）**样本也能设默认：头像是展示，不是归属。"""
    client = api_env["client"]
    assigner.autoAssign("FC_PH_2020_11_0", "P_alpha", 0.7)
    got = client.patch("/api/contacts/P_alpha",
                       json={"avatarFaceCode": "FC_PH_2020_11_0"})
    assert got.status_code == 200, got.text
    assert got.json()["contact"]["avatarFaceCode"] == "FC_PH_2020_11_0"
    # 那张脸仍然是「未确认」—— 设头像不该把它变成人工确认
    faces = client.get("/api/persons/P_alpha/faces").json()["items"]
    assert [f["isConfirmed"] for f in faces] == [0]
    assert faces[0]["faceCode"] == "FC_PH_2020_11_0"


def test_patchAvatarRejectsFaceOfAnotherPerson(api_env, api_rows, assigner):
    """⑥ 别人的脸 -> 400，且库里**一个字都没变**（连时间戳都不动）。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    _confirm(assigner, "FC_PH_2013_07_0", "P_beta")

    before = api_rows.person("P_alpha")
    got = client.patch("/api/contacts/P_alpha",
                       json={"avatarFaceCode": "FC_PH_2013_07_0"})
    assert got.status_code == 400, got.text
    assert got.json()["code"] == "PARAM_INVALID"
    # 报错要说清「是谁的脸」——只报「不支持」用户不知道该怎么办
    message = got.json()["message"]
    assert "阿尔法" in message and "贝塔" in message
    assert "**" not in message          # message 是给人看的一句话，不是 Markdown

    after = api_rows.person("P_alpha")
    assert after["avatarFaceCode"] is None
    assert str(after.get("modifyYMDHMS") or "") == str(before.get("modifyYMDHMS") or "")


def test_patchAvatarRejectsUnknownOrDeletedFace(api_env, api_rows, assigner):
    """⑦ 不存在 / 已软删的 faceCode -> 404（不是 500，也不是静默成功）。"""
    client = api_env["client"]
    got = client.patch("/api/contacts/P_alpha",
                       json={"avatarFaceCode": "FC_NOT_EXIST"})
    assert got.status_code == 404
    assert got.json()["code"] == "NOT_FOUND"

    _confirm(assigner, "FC_PH_2013_01_0", "P_gamma")
    _softDeleteFace("FC_PH_2013_01_0")
    got = client.patch("/api/contacts/P_gamma",
                       json={"avatarFaceCode": "FC_PH_2013_01_0"})
    assert got.status_code == 404
    assert api_rows.person("P_gamma")["avatarFaceCode"] is None


def test_patchAvatarKeepsNotGivenSemantics(api_env, api_rows):
    """**不给该键 = 一个都不动**（PATCH 语义），不是「清空」。"""
    client = api_env["client"]
    # 先直接写库造一个「已设好的头像」（用真实接口的话还得先有脸），
    # 再 PATCH 别的字段，确认它**没有被顺手清掉**
    from database.auto_generated import sqliteCommon as sqliteCommon
    sqliteCommon.updateTableGeneral("pb_person", "personCode = %s", ("P_beta",),
                                    {"avatarFaceCode": "FC_PH_2016_03_0"})
    got = client.patch("/api/contacts/P_beta", json={"memo": "随便改点别的"})
    assert got.status_code == 200, got.text
    assert api_rows.person("P_beta")["avatarFaceCode"] == "FC_PH_2016_03_0"


def test_unknownFieldErrorMentionsAvatarFaceCode(api_env):
    """白名单的报错信息里要能看见 `avatarFaceCode` —— 猜错字段名时唯一的线索。"""
    got = api_env["client"].patch("/api/contacts/P_alpha", json={"avatar": "x"})
    assert got.status_code == 400
    assert "avatarFaceCode" in got.json()["message"]


def test_defaultFaceRemovedDoesNotClearAvatar(api_env, assigner):
    """⑨ 默认那张被移除后：列**留着不动**、`coverFaceCode` 自动降级（DR-41 ④）。"""
    client = api_env["client"]
    _confirm(assigner, "FC_PH_2013_01_0", "P_alpha")
    _confirm(assigner, "FC_PH_2013_01_1", "P_alpha")
    got = client.patch("/api/contacts/P_alpha",
                       json={"avatarFaceCode": "FC_PH_2013_01_0"})
    assert got.status_code == 200, got.text

    fix = client.post("/api/review/fix", json={
        "faceCodes": ["FC_PH_2013_01_0"], "action": "unknown"})
    assert fix.status_code == 200, fix.text

    one = _personOf(client, "P_alpha")
    assert one["avatarFaceCode"] == "FC_PH_2013_01_0"      # 刻意不清理
    assert one["coverFaceCode"] == "FC_PH_2013_01_1"       # 展示层自动降级
    assert one["thumbUrl"] == "/api/face/FC_PH_2013_01_1"
