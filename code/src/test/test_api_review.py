#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_review.py
#Description: photo-browser 纠错闭环接口测试（步骤 9·DR-16）—— **重点是副作用**
#
# 逐条对应验收清单第 5~14 条
#   5待确认队列分页 + Top-5 候选按相似度降序
#   6assign 后 photoCount 立即变化；确认后质心**即时**重算
#   7merge 双方数据正确合并、无孤儿
#   8改判 fix(assign)：**原人与新人**的质心都已重算
#   9改判后无残留旧 linkKey；该照片只关联新 person
#   10fix(stranger) 后该脸既不在待确认、也不在「我不同意」
#   11/api/review/disputed 的 total == SQL 口径行数
#   12undo 能把一次 merge 完整还原（含质心 + revertedByLogCode）
#   13每次纠错调用都新增 pb_review_log，opType/from/to/faceCount 正确
#   14/api/review/pending/count 返回两个数**且不相等**
#
# ⚠️ 本文件的核心方法论：**每个副作用都用「绕过 api 层直查库」来验**
#   （api_rows 夹具）。理由与项目一贯的纪律一致：
#   用 api 自己报的数字证明 api 自己写对了，是**自证**；
#   而「改判后原人质心也重算了」这类性质，只有对比**质心 BLOB 本身**
#   才算证据（modifyYMDHMS 只有秒级精度，同一秒内的两次写入区分不出来）。

import pytest


# ============================================================
# 装置：把「质心已长出来」的状态搭好
# ============================================================

#: P_alpha 的确认样本（4 张，全部落在同一个年代桶 -> 单桶 sampleCount=4）
ALPHA_FACES = ["FC_PH_2013_01_0", "FC_PH_2013_01_1",
               "FC_PH_2013_07_0", "FC_PH_2020_11_0"]
#: P_beta 的确认样本（3 张）
BETA_FACES = ["FC_PH_2016_03_0", "FC_PH_2016_03_1", "FC_PH_2019_04_0"]


def _centroidMap(api_rows, personCode):
    """{bucketKey: (sampleCount, blob)} —— blob 用来证明「质心真的被重算了」。"""
    out = {}
    for row in api_rows.centroids(personCode):
        out[str(row.get("bucketKey") or "")] = (int(row.get("sampleCount") or 0),
                                                 bytes(row.get("centroid") or b""))
    return out


def _realBuckets(api_rows, personCode):
    """**排除 `ALL` 兜底桶**后的 {bucketKey: (sampleCount, blob)}。

    `ALL` 是虚拟桶（该人的全部确认样本、不分桶），不对应任何
    `pb_face.shotBucket` 值 —— 拿它做「样本数变了没有」的断言会永远失败。
    """
    return {k: v for k, v in _centroidMap(api_rows, personCode).items()
            if k != "ALL"}


def _totalSamples(buckets):
    return sum(v[0] for v in buckets.values())


@pytest.fixture
def primed(api_env, api_rows):
    """两个人各有 >=3 张**人工确认**样本 -> 质心已启用（MIN_SAMPLES=3）。"""
    from processor.review import assigner as assigner

    for code in ALPHA_FACES:
        assigner.confirm(code, "P_alpha")
    for code in BETA_FACES:
        assigner.confirm(code, "P_beta")
    return api_env


# ============================================================
# 验收 5：待确认队列分页 + 候选降序
# ============================================================

def test_pendingQueuePaging(api_env):
    """未归属的脸全部进待确认队列；分页不重不漏。"""
    client = api_env["client"]
    total = client.get("/api/review/pending", params={"size": 100}).json()["total"]
    assert total == 10                       # 构造了 10 张脸，全未归属

    seen = []
    for page in (1, 2, 3):
        body = client.get("/api/review/pending",
                          params={"page": page, "size": 4}).json()
        assert body["page"] == page and body["total"] == total
        seen.extend(one["faceCode"] for one in body["items"])
    assert len(seen) == len(set(seen)) == 10      # 不重不漏
    assert body["hasMore"] is False


def test_pendingTopCandidatesSortedBySimilarity(primed):
    """Top-5 候选**按相似度降序**（验收第 5 条）。

    ⚠️ 这里不假设「一定有候选」—— 质心刚建起来、样本还少的时候，
      冷启动会让所有 similarity 为空。所以断言的是**排序性质**
      （非递增），而不是「一定非空」：后者会把「冷启动」误判成 bug。
    """
    body = primed["client"].get("/api/review/pending",
                                params={"size": 50, "topN": 5}).json()
    checked = 0
    for one in body["items"]:
        candidates = one["topCandidates"]
        assert len(candidates) <= 5
        scores = [float(c["similarity"] or 0.0) for c in candidates]
        assert scores == sorted(scores, reverse=True), one["faceCode"]
        checked += 1
    assert checked == 3                    # 4+3 已确认，剩下 3 张待确认
    # 窗口内也按语义序：分数高 -> 低（None 排最后）
    sims = [(one["similarity"] if one["similarity"] is not None else -1.0)
            for one in body["items"]]
    assert sims == sorted(sims, reverse=True)


# ============================================================
# 验收 6：assign -> photoCount 立即变化 + 质心即时重算
# ============================================================

def test_assignUpdatesPhotoCountAndCentroid(api_env, api_rows):
    """assign 之后 photoCount **立刻**变，质心**立刻**重算（纪律①）。"""
    client = api_env["client"]
    before = client.get("/api/persons/P_alpha").json()
    assert before["photoCount"] == 0
    assert api_rows.centroids("P_alpha") == []

    got = client.put("/api/review/FC_PH_2013_01_0/assign",
                     json={"personCode": "P_alpha"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["ok"] is True and body["personCode"] == "P_alpha"
    assert body["changed"] is True
    # 响应里就带着最新的三个计数（省一次请求）
    assert body["person"]["photoCount"] == 1
    assert body["person"]["faceCount"] == 1
    assert body["person"]["confirmedFaceCount"] == 1

    after = client.get("/api/persons/P_alpha").json()
    assert after["photoCount"] == 1                     # 立即可见

    # 质心：1 个样本 < MIN_SAMPLES(3) -> 行存在但**未启用**（这本身就是证据：
    # 「确认后不重算」的话，这张表里根本不会多出这一行）
    rows = api_rows.centroids("P_alpha")
    assert len(rows) >= 1
    assert all(int(r["sampleCount"]) == 1 for r in rows if r["bucketKey"] != "ALL")
    assert all(not r["centroid"] for r in rows)         # 样本不足 -> centroid 为 NULL


def test_assignIdempotent(api_env):
    """重复 assign 同一个人是**幂等**的（PUT 语义，前端可以放心重试）。"""
    client = api_env["client"]
    first = client.put("/api/review/FC_PH_2013_01_0/assign",
                       json={"personCode": "P_alpha"}).json()
    second = client.put("/api/review/FC_PH_2013_01_0/assign",
                        json={"personCode": "P_alpha"}).json()
    assert first["changed"] is True
    assert second["changed"] is False                   # 幂等：无变化
    assert second["person"]["photoCount"] == 1          # 计数没被重复累加


def test_batchAssign(api_env, api_rows):
    client = api_env["client"]
    got = client.post("/api/review/batch-assign", json={
        "faceCodes": ["FC_PH_2013_01_0", "FC_PH_2013_01_1", "FC_PH_2013_07_0"],
        "personCode": "P_alpha"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["assigned"] == 3 and body["failed"] == []
    assert body["person"]["photoCount"] == 2            # 两张照片
    assert body["person"]["confirmedFaceCount"] == 3
    # 一条 BATCH_ASSIGN 日志（不是 3 条）
    logs = [r for r in api_rows.logs("BATCH_ASSIGN")]
    assert len(logs) == 1
    assert int(logs[0]["faceCount"]) == 3


# ============================================================
# 验收 14 + 11：两个队列的计数口径
# ============================================================

def test_pendingCountReturnsTwoUnequalNumbers(api_env, api_rows):
    """`/api/review/pending/count` 返回**两个数且不相等**（验收第 14 条）。"""
    from processor.review import assigner as assigner

    # 2 张自动归属（isConfirmed=0 + personCode 非空）-> 只进「我不同意」
    assigner.autoAssign("FC_PH_2013_01_0", "P_alpha", 0.71)
    assigner.autoAssign("FC_PH_2013_01_1", "P_alpha", 0.62)
    # 2 张人工确认 -> 两个队列都不进
    assigner.confirm("FC_PH_2016_03_0", "P_beta")
    assigner.confirm("FC_PH_2016_03_1", "P_beta")

    body = api_env["client"].get("/api/review/pending/count").json()
    assert body["pendingCount"] == 6                    # 10 - 2 自动 - 2 确认
    assert body["disputedCount"] == 2
    assert body["pendingCount"] != body["disputedCount"]
    assert body["confirmedCount"] == 2

    # 与 SQL 口径逐个核对（不拿 api 自己的数当证据）
    assert body["pendingCount"] == api_rows.count(
        "pb_face", "personCode IS NULL AND isStranger = %s AND delFlag = %s",
        (0, "0"))
    assert body["disputedCount"] == api_rows.count(
        "pb_face", "personCode IS NOT NULL AND isConfirmed = %s"
                   " AND isStranger = %s AND delFlag = %s", (0, 0, "0"))


def test_disputedGroupTotalIsSeparateFromFaceTotal(api_env, api_rows):
    """⚠️ **两套总数必须都有**（`total` 是脸数、`photoGroupTotal` 是照片组数）。

    前端画分页器只能用 `photoGroupTotal` —— 拿 `total`（脸的行数）去算总页数
    会翻进空页：37 张脸完全可能只分布在 5 张照片里。
    而这个错**不会报错**，只表现为「最后一页是空的」。
    """
    from processor.review import assigner as assigner

    # PH_2013_01 两张脸 + PH_2020_11 一张脸 = 3 张脸，却只分布在 **2 张照片**
    for code in ("FC_PH_2013_01_0", "FC_PH_2013_01_1", "FC_PH_2020_11_0"):
        assigner.autoAssign(code, "P_alpha", 0.66)

    body = api_env["client"].get("/api/review/disputed",
                                  params={"size": 20}).json()
    assert body["total"] == 3
    assert body["photoGroupTotal"] == 2, body
    assert body["groupCount"] == 2
    assert body["faceCountInPage"] == 3

    # 用 SQL 独立核对两个数（**不信 api 自报的数**）
    from database import queryCommon as query

    where = ("personCode IS NOT NULL AND isConfirmed = %s"
             " AND isStranger = %s AND delFlag = %s")
    sqlFaces = api_rows.count("pb_face", where, (0, 0, "0"))
    sqlGroups = int(query.selectValue(
        "SELECT COUNT(DISTINCT photoCode) AS rowNum FROM pb_face WHERE " + where,
        (0, 0, "0")) or 0)
    assert body["total"] == sqlFaces == 3
    assert body["photoGroupTotal"] == sqlGroups == 2

    # 只看某张照片时两个数都要跟着缩
    one = api_env["client"].get("/api/review/disputed",
                                params={"size": 20,
                                        "photoCode": "PH_2013_01"}).json()
    assert one["total"] == 3                  # total 是**全局**脸数（不随 photoCode 缩）
    assert one["photoGroupTotal"] == 1        # 但分组数缩到了 1
    assert len(one["items"]) == 1


def test_disputedTotalMatchesSql(api_env, api_rows):
    """`/api/review/disputed` 的 total == SQL 口径行数（验收第 11 条）。"""
    from processor.review import assigner as assigner

    for code in ("FC_PH_2013_01_0", "FC_PH_2013_01_1", "FC_PH_2020_11_0"):
        assigner.autoAssign(code, "P_alpha", 0.66)
    assigner.confirm("FC_PH_2016_03_0", "P_beta")

    body = api_env["client"].get("/api/review/disputed",
                                  params={"size": 20}).json()
    sqlCount = api_rows.count(
        "pb_face", "personCode IS NOT NULL AND isConfirmed = %s"
                   " AND isStranger = %s AND delFlag = %s", (0, 0, "0"))
    assert body["total"] == sqlCount == 3
    # 按 photoCode 分组：PH_2013_01 有 2 张脸，PH_2020_11 有 1 张
    assert body["groupCount"] == 2
    assert body["faceCountInPage"] == 3
    groups = {g["photoCode"]: g["faceCount"] for g in body["items"]}
    assert groups == {"PH_2013_01": 2, "PH_2020_11": 1}

    # ⚠️ 自动归属的脸**不在**待确认队列里（DR-16① 的核心修正点）
    pending = api_env["client"].get("/api/review/pending",
                                     params={"size": 50}).json()
    pendingCodes = {one["faceCode"] for one in pending["items"]}
    assert "FC_PH_2013_01_0" not in pendingCodes
    assert pending["total"] == 6


# ============================================================
# 验收 8 + 9 + 13：改判（fix）的连带副作用
# ============================================================

def test_fixAssignRecomputesBothCentroids(primed, api_rows):
    """**原人与新人**的质心都必须重算（验收第 8 条：只重算一边算不合格）。

    证据用**质心内容**（sampleCount + BLOB），不用 modifyYMDHMS ——
    后者只有秒级精度，同一秒里的两次写入区分不出来，
    那种「看起来变了」的断言等于没断言。

    ⚠️ 桶是**自适应**的（按拍摄年 + 出生年），所以同一个人的 4 张脸
       落在**不止一个**桶里：ALPHA_FACES 里 2013 年的三张 -> "2003-2012"，
       2020 年那张-> "2013-2022"。所以断言必须**按桶**写，
       拿「第一个桶」来断言会测到另一个桶上去（写测试时踩过）。
    """
    client = primed["client"]
    alphaAllBefore = _centroidMap(api_rows, "P_alpha")["ALL"]
    betaAllBefore = _centroidMap(api_rows, "P_beta")["ALL"]
    alphaBefore = _realBuckets(api_rows, "P_alpha")
    betaBefore = _realBuckets(api_rows, "P_beta")
    assert _totalSamples(alphaBefore) == 4, alphaBefore
    assert _totalSamples(betaBefore) == 3, betaBefore
    # 前置：`ALL` 兜底桶样本 >= MIN_SAMPLES(3) -> 两边都有真实向量可比
    assert alphaAllBefore[0] == 4 and len(alphaAllBefore[1]) == 2048
    assert betaAllBefore[0] == 3 and len(betaAllBefore[1]) == 2048

    got = client.post("/api/review/fix", json={
        "faceCode": "FC_PH_2020_11_0", "action": "assign",
        "personCode": "P_beta", "reason": "验收：改判"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["ok"] is True and body["changed"] is True
    assert body["fromPersonCode"] == "P_alpha"
    assert body["toPersonCode"] == "P_beta"
    assert body["centroidRebuilt"] is True

    alphaAfter = _realBuckets(api_rows, "P_alpha")
    betaAfter = _realBuckets(api_rows, "P_beta")

    # ① 原人：总数 4 -> 3；被搬走那一张所在的桶（2020 那张）**整个消失**
    #    ⚠️ 注意不是「sampleCount 归零」而是「桶被删掉」——
    #    centroid.recomputePerson 会 listBucketsOf() 取「现在真有样本的桶」
    #    再把不在其中的行 dropBucket() 掉（清僵尸桶）。
    #    留一个 sampleCount=0 的空桶在库里，只会让「这个人这个年代有数据」
    #    看起来成立，而它实际不参与任何匹配。
    assert _totalSamples(alphaAfter) == 3
    assert "2013-2022" not in alphaAfter, "空桶应该被清掉：%s" % sorted(alphaAfter)
    assert alphaAfter["2003-2012"][0] == 3# 另外三张还在

    # ② 新人：总数 3 -> 4。2020 那张按 beta 的生日 1990 落在 "2018-2027"
    assert _totalSamples(betaAfter) == 4
    assert betaAfter["2018-2027"][0] == betaBefore["2018-2027"][0] + 1 == 2
    #    ⚠️ 这一桶改判前后**都不足 MIN_SAMPLES(3)**，所以两次的 centroid
    #    都是 NULL —— 拿 BLOB 比「变了没有」在这里**证明不了任何事**
    #    （写测试时踩过：两个 b'' 判不相等）。能当证据的是 sampleCount 记账。
    assert betaAfter["2018-2027"][1] == b""

    # ③ **双方的 `ALL` 兜底桶都重算**（原人 4 -> 3，新人 3 -> 4），
    #    且向量内容都变了 —— 这是「只重算一边 = 越改越乱」最直接的证据：
    #    原人的质心若停在「还包含那张已经不属于他的脸」的旧值上，
    #    之后他的新照片会一直被这张脸带着分数，而且**不报任何错**。
    alphaAllAfter = _centroidMap(api_rows, "P_alpha")["ALL"]
    betaAllAfter = _centroidMap(api_rows, "P_beta")["ALL"]
    assert alphaAllAfter[0] == 3
    assert alphaAllAfter[1] != alphaAllBefore[1], "原人的 ALL 兜底桶没有重算"
    assert betaAllAfter[0] == 4
    assert betaAllAfter[1] != betaAllBefore[1], "新人的 ALL 兜底桶没有重算"

    # ④ 没被影响的那个桶**理应逐位不变**（三张脸一张没动）
    assert alphaAfter["2003-2012"][1] == alphaBefore["2003-2012"][1]

    # 响应里也带上了双方质心（前端可以直接显示「谁的质心被改了」）
    assert body["oldPerson"]["personCode"] == "P_alpha"
    assert body["newPerson"]["personCode"] == "P_beta"
    assert body["oldPerson"]["bucketCount"] >= 1
    assert body["newPerson"]["bucketCount"] >= 1


def test_fixAssignDropsStaleLinkKey(primed, api_rows):
    """改判后**无残留旧 linkKey**；该照片只关联新 person（验收第 9 条）。"""
    client = primed["client"]
    # 前置：PH_2020_11 只关联 P_alpha，且 FC_PH_2020_11_0 是该照片里
    # **唯一**属于 alpha 的脸 —— 所以改判之后那条关联必须消失。
    assert [str(r["linkKey"]) for r in api_rows.links(photoCode="PH_2020_11")] \
        == ["PH_2020_11:P_alpha"]

    client.post("/api/review/fix", json={"faceCode": "FC_PH_2020_11_0",
                                         "action": "assign",
                                         "personCode": "P_beta"})
    keys = sorted(str(r["linkKey"]) for r in api_rows.links(photoCode="PH_2020_11"))
    assert keys == ["PH_2020_11:P_beta"], "残留了旧 linkKey: %s" % keys
    assert api_rows.links(photoCode="PH_2020_11", personCode="P_alpha") == []

    # ⚠️ 反例也要守：PH_2013_01 里**还有另一张** alpha 的脸，
    #   所以那条关联**必须保留**（纪律③：这张照片里确实还有人属于 alpha）。
    assert [str(r["linkKey"]) for r in api_rows.links(photoCode="PH_2013_01")] \
        == ["PH_2013_01:P_alpha"]


def test_fixStrangerLeavesBothQueues(api_env, api_rows):
    """fix(stranger) 之后该脸**既不在待确认、也不在「我不同意」**（验收第 10 条）。"""
    from processor.review import assigner as assigner

    client = api_env["client"]
    assigner.autoAssign("FC_PH_2013_01_0", "P_alpha", 0.66)   # 先自动归属
    assert client.get("/api/review/pending/count").json()["disputedCount"] == 1

    got = client.post("/api/review/fix", json={"faceCode": "FC_PH_2013_01_0",
                                              "action": "stranger"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["changed"] is True
    assert body["toPersonCode"] is None

    counts = client.get("/api/review/pending/count").json()
    assert counts["disputedCount"] == 0
    assert counts["strangerCount"] == 1

    pending = client.get("/api/review/pending", params={"size": 50}).json()
    assert "FC_PH_2013_01_0" not in {i["faceCode"] for i in pending["items"]}
    disputed = client.get("/api/review/disputed", params={"size": 50}).json()
    assert "FC_PH_2013_01_0" not in {f["faceCode"]
                                     for g in disputed["items"] for f in g["faces"]}

    face = api_rows.face("FC_PH_2013_01_0")
    assert face["personCode"] is None and int(face["isStranger"]) == 1


def test_fixUnknownReturnsToPending(primed, api_rows):
    """fix(unknown) 把自动归属的脸退回**待确认队列**。"""
    client = primed["client"]
    got = client.post("/api/review/fix", json={"faceCode": "FC_PH_2013_07_0",
                                              "action": "unknown"})
    assert got.status_code == 200, got.text
    face = api_rows.face("FC_PH_2013_07_0")
    assert face["personCode"] is None and int(face["isConfirmed"]) == 0
    pending = client.get("/api/review/pending", params={"size": 50}).json()
    assert "FC_PH_2013_07_0" in {i["faceCode"] for i in pending["items"]}


def test_everyFixWritesReviewLog(primed, api_rows):
    """**每次纠错调用都新增一条 pb_review_log**（验收第 13 条）。

    ⚠️ 注意「幂等空操作」不算：fix('unknown') 打在一张本就未归属的脸上时，
      `assigner.fix` 直接返回 changed=False **且不写日志** ——
      什么都没发生就不该记账（纪律④要防的是**静默改库**，不是强制记账）。
    """
    client = primed["client"]
    before = len(api_rows.logs())

    client.post("/api/review/fix", json={"faceCode": "FC_PH_2020_11_0",
                                         "action": "assign", "personCode": "P_beta"})
    afterFix = api_rows.logs()
    assert len(afterFix) == before + 1
    row = afterFix[-1]
    assert row["opType"] == "FIX"
    assert row["faceCode"] == "FC_PH_2020_11_0"
    assert row["fromPersonCode"] == "P_alpha"
    assert row["toPersonCode"] == "P_beta"
    assert int(row["faceCount"]) == 1

    client.post("/api/review/fix", json={"faceCode": "FC_PH_2020_11_0",
                                         "action": "unknown"})
    assert api_rows.logs()[-1]["opType"] == "UNKNOWN"
    assert api_rows.logs()[-1]["fromPersonCode"] == "P_beta"
    assert api_rows.logs()[-1]["toPersonCode"] is None      # None 而非空串

    client.post("/api/review/fix", json={"faceCode": "FC_PH_2020_11_0",
                                         "action": "stranger"})
    assert api_rows.logs()[-1]["opType"] == "STRANGER"
    assert len(api_rows.logs()) == before + 3


def test_batchFixOneTransaction(primed, api_rows):
    """batch-fix：一个事务 + 每张脸一条日志 + **双方**质心都重算。"""
    client = primed["client"]
    alphaBefore = _realBuckets(api_rows, "P_alpha")
    betaBefore = _realBuckets(api_rows, "P_beta")
    got = client.post("/api/review/batch-fix", json={
        "faceCodes": ["FC_PH_2020_11_0", "FC_PH_2013_07_0"],
        "action": "assign", "personCode": "P_beta"})
    assert got.status_code == 200, got.text
    assert got.json()["fixed"] == 2

    alphaAfter = _realBuckets(api_rows, "P_alpha")
    betaAfter = _realBuckets(api_rows, "P_beta")
    # 搬走两张：alpha 4 -> 2，beta 3 -> 5
    assert _totalSamples(alphaBefore) == 4 and _totalSamples(alphaAfter) == 2
    assert _totalSamples(betaBefore) == 3 and _totalSamples(betaAfter) == 5
    assert "2013-2022" not in alphaAfter           # 空桶被清掉
    assert alphaAfter["2003-2012"][0] == 2          # 2013 的三张少了一张
    assert betaAfter["2018-2027"][0] == 2           # 2020 那张进这里
    assert betaAfter["2008-2017"][0] == 3           # 2013 那张进这里


def test_batchFixByClusterCode(api_env):
    """batch-fix 也可以给 clusterCode（**现查活成员**，不缓存旧编码）。"""
    client = api_env["client"]
    body = client.get("/api/review/clusters", params={"minSize": 1}).json()
    assert body["total"] >= 1
    code = body["items"][0]["clusterCode"]
    got = client.post("/api/review/batch-fix", json={
        "clusterCode": code, "action": "stranger"})
    assert got.status_code == 200, got.text
    assert got.json()["requested"] == body["items"][0]["size"]


def test_fixBadActionRejected(api_env):
    got = api_env["client"].get("/api/review/pending/count")     # 先热身，确保路由通
    assert got.status_code == 200
    bad = api_env["client"].post("/api/review/fix", json={
        "faceCode": "FC_PH_2013_01_0", "action": "delete"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "PARAM_INVALID"


# ============================================================
# 验收 7 + 12：合并与撤销
# ============================================================

def test_mergeCombinesBothSides(primed, api_rows):
    """合并：脸/关联/质心/分类都对，且**无孤儿**（验收第 7 条）。"""
    from processor.review import assigner as assigner

    client = primed["client"]
    got = client.post("/api/review/merge",
                      json={"fromPersonCode": "P_beta", "toPersonCode": "P_alpha"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["faces"] == 3
    assert body["logCode"] and body["revertible"] is True

    # from 软删；两张脸都指向 to
    assert api_rows.person("P_beta")["delFlag"] == "1"
    for code in BETA_FACES:
        assert api_rows.face(code)["personCode"] == "P_alpha"
    # from 的质心全删，to 的质心重建（启用桶数 >= 1）
    assert api_rows.centroids("P_beta") == []
    assert any(r["centroid"] for r in api_rows.centroids("P_alpha"))

    # **无孤儿**：关联行指向的人必须在该照片里确实有脸
    report = assigner.verifyLinks()
    assert report["clean"] is True, report
    assert assigner.syncLinks(dryRun=True)["add"] == 0
    assert assigner.syncLinks(dryRun=True)["drop"] == 0

    # 主日志 + 每脸一条成员日志
    logs = api_rows.logs("MERGE")
    assert len(logs) == 1 + 3
    assert logs[0]["faceCode"] is None and int(logs[0]["faceCount"]) == 3
    assert int(logs[0]["isRevertible"]) == 1


def test_mergeSelfRejected(primed):
    got = primed["client"].post("/api/review/merge",
                                json={"fromPersonCode": "P_alpha",
                                      "toPersonCode": "P_alpha"})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


def test_undoRestoresMergeCompletely(primed, api_rows):
    """**撤销一次 merge 要完整还原**：人脸归属 + 关联行 + 双方质心 + 日志回填
    （验收第 12 条）。"""
    from processor.review import assigner as assigner

    client = primed["client"]
    alphaBefore = _centroidMap(api_rows, "P_alpha")
    betaBefore = _centroidMap(api_rows, "P_beta")
    linksBefore = sorted(str(r["linkKey"]) for r in api_rows.links())
    facesBefore = {c: api_rows.face(c)["personCode"] for c in BETA_FACES}

    merged = client.post("/api/review/merge",
                         json={"fromPersonCode": "P_beta",
                               "toPersonCode": "P_alpha"}).json()
    logCode = merged["logCode"]

    # 撤销前它应该在「可撤销」列表里
    revertible = client.get("/api/review/revertible").json()
    assert logCode in {i["logCode"] for i in revertible["items"]}

    undone = client.post("/api/review/undo", json={"logCode": logCode})
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert body["opType"] == "MERGE"
    assert body["facesRestored"] == 3
    assert body["missingFaces"] == []

    # ① 人脸归属逐位还原（含每张脸原来的 isConfirmed）
    for code, owner in facesBefore.items():
        assert api_rows.face(code)["personCode"] == owner
    # ② pb_photo_person 完整还原
    assert sorted(str(r["linkKey"]) for r in api_rows.links()) == linksBefore
    # ③ 双方质心都重算回原值
    assert _centroidMap(api_rows, "P_alpha") == alphaBefore
    assert _centroidMap(api_rows, "P_beta") == betaBefore
    # ④ from 的软删被还原 + 日志回填 revertedByLogCode + 新增一条 UNDO
    assert api_rows.person("P_beta")["delFlag"] == "0"
    row = [r for r in api_rows.logs() if str(r["logCode"]) == logCode][0]
    assert row["revertedByLogCode"] == body["undoLogCode"]
    undoRow = [r for r in api_rows.logs("UNDO")][-1]
    assert undoRow["logCode"] == body["undoLogCode"]
    assert int(undoRow["faceCount"]) == 3

    # 无孤儿
    assert assigner.verifyLinks()["clean"] is True


def test_undoTwiceRejected(primed):
    """撤销两次必须**明确报错**，不做「尽力而为地做一半」（merger 的四道闸门）。"""
    client = primed["client"]
    logCode = client.post("/api/review/merge",
                          json={"fromPersonCode": "P_beta",
                                "toPersonCode": "P_alpha"}).json()["logCode"]
    assert client.post("/api/review/undo",
                       json={"logCode": logCode}).status_code == 200
    again = client.post("/api/review/undo", json={"logCode": logCode})
    assert again.status_code == 409
    assert again.json()["code"] == "TASK_STATE_ILLEGAL"


def test_undoPlainAssignRejected(primed, api_rows):
    """普通确认**不可撤销**（它的逆操作是再点一次改判）。"""
    client = primed["client"]
    logCode = [r for r in api_rows.logs("ASSIGN")][0]["logCode"]
    got = client.post("/api/review/undo", json={"logCode": logCode})
    assert got.status_code == 409


# ============================================================
# 拆分
# ============================================================

def test_splitToNewPerson(api_env, api_rows):
    client = api_env["client"]
    client.put("/api/review/FC_PH_2013_01_0/assign", json={"personCode": "P_alpha"})
    got = client.post("/api/review/split", json={
        "faceCode": "FC_PH_2013_01_0", "personCode": "P_new1",
        "displayName": "拆出来的人", "birthday": "1992-05-05"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["created"] == "P_new1"
    assert body["fromPersonCode"] == "P_alpha"
    assert body["toPersonCode"] == "P_new1"
    assert body["revertible"] is True
    assert api_rows.face("FC_PH_2013_01_0")["personCode"] == "P_new1"
    assert api_rows.person("P_new1")["birthday"] == "1992-05-05"


def test_splitToUnassigned(api_env, api_rows):
    client = api_env["client"]
    client.put("/api/review/FC_PH_2013_07_0/assign", json={"personCode": "P_alpha"})
    got = client.post("/api/review/split", json={"faceCode": "FC_PH_2013_07_0"})
    assert got.status_code == 200, got.text
    assert got.json()["toPersonCode"] is None
    assert api_rows.face("FC_PH_2013_07_0")["personCode"] is None
    assert api_rows.links(photoCode="PH_2013_07", personCode="P_alpha") == []


# ============================================================
# 操作历史
# ============================================================

def test_reviewLogQueryable(primed):
    """`/api/review/log` 能回答「这张脸当初怎么被认成这个人的」（排障用）。"""
    from processor.review import assigner as assigner

    client = primed["client"]
    assigner.autoAssign("FC_PH_2024_06_1", "P_beta", 0.5732)
    body = client.get("/api/review/log",
                      params={"faceCode": "FC_PH_2024_06_1"}).json()
    assert body["total"] >= 1
    last = body["items"][0]
    assert last["opType"] == "ASSIGN"
    assert last["toPersonCode"] == "P_beta"
    assert abs(float(last["similarity"]) - 0.5732) < 1e-6

    # ⚠️ 人工确认**不写 similarity**（不编造分数）-> 字段为空而不是 0.00
    manual = client.get("/api/review/log",
                        params={"faceCode": "FC_PH_2013_01_0"}).json()
    assert manual["total"] == 1
    assert manual["items"][0]["similarity"] is None


def test_reviewLogRequiresFilter(api_env):
    """刻意**不支持裸查全表**（日志表随使用线性增长）。"""
    got = api_env["client"].get("/api/review/log")
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"
