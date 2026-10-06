#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_smoke.py
#Description: photo-browser API 层冒烟测试（步骤 9）
#
# 覆盖
#   * 全部端点能在 /docs 里看到（OpenAPI paths 数与关键端点逐个点名）
#   * 统一响应结构：分页 {page,size,total,items} / 错误 {code,message}
#   * 时间线三种模式、照片筛选（personCode / 年份区间 / hasFace / isDuplicate）
#   * 人物列表/详情、照片详情、places、overview
#   * 扫描接口的幂等/状态机错误体
#   * **DR-17：无导出接口** / **DR-19：无 DELETE 接口**（走查型断言）
#   * **原图零风险**：整轮测试后 photo 目录文件数与字节数不变（验收第 15 条）
#
# ⚠️ 为什么不把所有断言塞进一个用例
#   端点之间共享同一份构造数据（api_env 是 function-scope 但很便宜），
#   分开写能让失败信息直接指向「哪个端点坏了」，而不是一次失败看到第一个断言就停。

import os

import pytest


# ============================================================
# 一、OpenAPI：接口清单
# ============================================================

#: 本步交付的端点 -> 必须出现在 /docs 里。**逐个点名**而不是只数总数：
#: 少了一个而总数对得上（比如手滑把某个换成别的），是发现不了的那类问题。
EXPECTED_PATHS: dict = {
    # browse
    "/api/timeline": ["get"],
    "/api/photos": ["get"],
    "/api/persons": ["get"],
    "/api/persons/{personCode}": ["get"],
    "/api/photos/{photoCode}": ["get"],
    "/api/places": ["get"],
    "/api/overview": ["get"],
    # scan
    "/api/scan/start": ["post"],
    "/api/scan/status/{jobCode}": ["get"],
    "/api/scan/resume/{jobCode}": ["post"],
    "/api/scan/stop/{jobCode}": ["post"],
    "/api/scan/jobs": ["get"],
    # review
    "/api/review/pending": ["get"],
    "/api/review/disputed": ["get"],
    "/api/review/pending/count": ["get"],
    "/api/review/{faceCode}/assign": ["put"],
    "/api/review/batch-assign": ["post"],
    "/api/review/fix": ["post"],
    "/api/review/batch-fix": ["post"],
    "/api/review/merge": ["post"],
    "/api/review/split": ["post"],
    "/api/review/undo": ["post"],
    "/api/review/log": ["get"],
    "/api/review/revertible": ["get"],
    # contacts
    "/api/contacts": ["get", "post"],
    "/api/contacts/{personCode}": ["patch"],
    "/api/contacts/{personCode}/impact": ["get"],
    "/api/contacts/{personCode}/disable": ["post"],
    "/api/contacts/{personCode}/enable": ["post"],
    "/api/contacts/duplicates": ["get"],
    "/api/contacts/import/csv": ["post"],
    "/api/families": ["get", "post"],
    "/api/families/{familyCode}": ["get", "patch"],
    # 步骤 4 已有（回归保护）
    # ⚠️ 只列 `get`：**HEAD 刻意不出现在 OpenAPI 里**（见 static.py 的注释）——
    #   Starlette 对 GET 路由自动补 HEAD，显式注册两个方法会让 FastAPI
    #   为 GET/HEAD 生成同一个 operationId，每次启动刷 3 条
    #   `Duplicate Operation ID` 警告，`/docs` 里也多 3 组无意义的 HEAD 条目。
    #   HEAD 的**可用性**由 test_headStillWorksOnStaticRoutes 单独守。
    "/api/thumb/{photoCode}": ["get"],
    "/api/original/{photoCode}": ["get"],
    "/api/face/{faceCode}": ["get"],
    "/api/media/stats": ["get"],
    # 步骤 9 新增：地点字典重建
    "/api/places": ["get"],
    "/api/places/rebuild": ["post"],
}


def test_openapiExposesEveryEndpoint(api_env):
    """验收第 1 条：uvicorn 起来后 /docs 能看到全部接口。"""
    spec = api_env["client"].get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    missing = []
    for path, methods in sorted(EXPECTED_PATHS.items()):
        got = paths.get(path)
        if got is None:
            missing.append("%s 整个端点都没有" % path)
            continue
        for method in methods:
            if method.lower() not in got:
                missing.append("%s 缺 %s" % (path, method.upper()))
    assert not missing, "OpenAPI 缺端点: %s" % missing
    # /docs 页面本身也要能打开
    docs = api_env["client"].get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()


# ============================================================
# 二、统一响应结构
# ============================================================

PAGE_KEYS = {"page", "size", "total", "items", "hasMore"}


@pytest.mark.parametrize("url,params", [
    ("/api/photos", {"page": 1, "size": 5}),
    ("/api/persons", {"page": 1, "size": 5}),
    ("/api/contacts", {"page": 1, "size": 5}),
    ("/api/places", {}),
    ("/api/review/pending", {"page": 1, "size": 5}),
    ("/api/review/pending/count", {}),
])
def test_pagedEndpointsShareShape(api_env, url, params):
    """分页统一 { page, size, total, items }；两个角标接口也有明确形状。"""
    got = api_env["client"].get(url, params=params)
    assert got.status_code == 200, got.text
    body = got.json()
    if url == "/api/review/pending/count":
        assert "pendingCount" in body and "disputedCount" in body
        return
    # ⚠️ /api/review/disputed 是**唯一**把分页包在 page 里并额外带
    #    scope/groupCount 的接口（因为它还要说清「total 是脸数、
    #    items 是照片组数」）；/api/review/pending 不需要这层包裹。
    assert PAGE_KEYS.issubset(set(body.keys())), \
        "%s 缺分页字段: %s" % (url, sorted(body.keys()))
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int) and body["total"] >= len(body["items"])


def test_errorBodyIsCodeMessage(api_env):
    """错误统一 { code, message }。"""
    got = api_env["client"].get("/api/persons/查无此人")
    assert got.status_code == 404
    body = got.json()
    assert body["code"] == "NOT_FOUND"
    assert isinstance(body["message"], str) and body["message"]


def test_validationErrorFlattened(api_env):
    """422 也拍平成同一个壳（前端只需要写一个拦截器）。"""
    got = api_env["client"].get("/api/photos", params={"page": 0})
    assert got.status_code == 422
    body = got.json()
    assert body["code"] == "PARAM_INVALID"
    assert body["extra"]["errors"]


def test_deepPageRejected(api_env):
    """深分页被闸门拦下（而不是让 SQLite 扫 10 万行再丢弃）。"""
    got = api_env["client"].get("/api/photos", params={"page": 5000, "size": 100})
    assert got.status_code == 400
    assert got.json()["code"] == "PAGE_TOO_DEEP"


def test_methodNotAllowedIs405WithChineseMessage(api_env):
    """方法不匹配必须是 **405 + METHOD_NOT_ALLOWED**，不是 400 + 英文原文。

    ⚠️ 这条是步骤 9 在真实服务上试出来的：把 `POST /api/places/rebuild`
       发成 GET，拿到的是 `400 {"code":"PARAM_INVALID",
       "message":"Method Not Allowed"}` —— 前端会按「你参数写错了」去走表单
       校验分支，而真相是**调用姿势**错了（方法不对）。而且整句是英文，
       与全 API 的中文错误不一致。
    """
    client = api_env["client"]
    got = client.get("/api/places/rebuild")          # 它只收 POST
    assert got.status_code == 405, got.text
    body = got.json()
    assert body["code"] == "METHOD_NOT_ALLOWED"
    assert "不支持 GET" in body["message"]
    assert "POST" in body["message"]                 # 告诉调用方该用什么
    assert body["extra"]["allow"] and "POST" in body["extra"]["allow"]
    assert got.headers.get("allow") and "POST" in got.headers["allow"]

    # 反方向：对只收 GET 的路径发 POST（FastAPI 对无 body 的 GET 也接受 POST 吗？
    # 这里挑一个明确只注册了 GET 的：/api/overview）
    got = client.post("/api/overview")
    assert got.status_code == 405, got.text
    assert got.json()["code"] == "METHOD_NOT_ALLOWED"


def test_unknownRouteReturnsJson404(api_env):
    """不存在的路由也是 {code,message}，不是 HTML 错误页。"""
    got = api_env["client"].get("/api/压根不存在的接口")
    assert got.status_code == 404
    assert got.json()["code"] == "NOT_FOUND"


# ============================================================
# 三、时间线
# ============================================================

def test_timelineYearIndex(api_env):
    """首屏模式：一次拿到年月索引 + 日期未知的张数。"""
    got = api_env["client"].get("/api/timeline").json()
    assert got["scope"] == "years"
    years = {y["year"]: y for y in got["items"]}
    assert 2013 in years and 2024 in years
    # 2013：PH_2013_01(01 月) + PH_2013_07(07 月) + PH_DUP(01 月) = 3 张
    assert years[2013]["count"] == 3
    assert [m["month"] for m in years[2013]["months"]] == [7, 1]
    assert years[2024]["count"] == 2
    # PH_NODATE 的 takenAt 为空 -> 单列 unknownCount，**不消失**
    assert got["unknownCount"] == 1
    assert got["total"] == 9
    # 每一年的 months 都是降序且加总等于该年 count
    for one in years.values():
        months = one["months"]
        assert [m["month"] for m in months] == sorted(
            (m["month"] for m in months), reverse=True)
        assert sum(m["count"] for m in months) == one["count"]


def test_timelineMonthsOfYear(api_env):
    """带 year：只返回该年的月份分组（走 shotYear 索引）。"""
    got = api_env["client"].get("/api/timeline", params={"year": 2013}).json()
    assert got["scope"] == "months" and got["year"] == 2013
    assert [m["month"] for m in got["items"]] == [7, 1]
    assert got["count"] == 3


def test_timelinePhotosOfMonth(api_env):
    """带 year+month：分段取照片（OFFSET 被一个月兜住）。"""
    got = api_env["client"].get("/api/timeline",
                                params={"year": 2013, "month": 1, "size": 10}).json()
    assert got["scope"] == "photos" and got["ym"] == "2013-01"
    page = got["page"]
    assert PAGE_KEYS.issubset(page.keys())
    # 2013-01 有两张：PH_2013_01 与 PH_DUP（重复照片，takenAt 与前者相同）
    assert sorted(i["photoCode"] for i in page["items"]) == ["PH_2013_01", "PH_DUP"]
    assert page["total"] == 2
    assert page["items"][0]["thumbUrl"].startswith("/api/thumb/PH_")


def test_timelineMonthWithFaceFilter(api_env):
    got = api_env["client"].get("/api/timeline",
                                params={"year": 2024, "month": 6, "hasFace": 1}).json()
    assert [i["photoCode"] for i in got["page"]["items"]] == ["PH_2024_06"]


def test_timelineMonthWithPersonFilter(api_env):
    """`year+month+personCode` —— **回归钉**。

    ⚠️ 这个分支曾经漏了一个右括号：
       `EXISTS (SELECT 1 FROM pb_photo_person pp WHERE ... AND pp.personCode = %s`
       （没有 `)`）。整条 SQL 直接语法错误 -> `queryCommon.selectValue` 返回
       default -> `total=0`、`items=[]`，**接口仍然 200**。
       当时没有任何用例覆盖「timeline + personCode」这条路，所以它一直绿着。
       现在这里盯着它。
    """
    from processor.review import assigner as assigner

    assigner.confirm("FC_PH_2013_01_0", "P_alpha")
    got = api_env["client"].get(
        "/api/timeline",
        params={"year": 2013, "month": 1, "personCode": "P_alpha"}).json()
    assert got["scope"] == "photos"
    assert got["page"]["total"] == 1, got
    assert [i["photoCode"] for i in got["page"]["items"]] == ["PH_2013_01"]
    # 换个人 -> 0 张（证明确实在按 personCode 过滤，而不是索引缺失导致的恒真）
    got = api_env["client"].get(
        "/api/timeline",
        params={"year": 2013, "month": 1, "personCode": "P_beta"}).json()
    assert got["page"]["total"] == 0, got

    # unknown 模式下同样要能用 personCode 过滤
    assigner.confirm("FC_PH_NODATE_0", "P_alpha")
    got = api_env["client"].get(
        "/api/timeline", params={"unknown": 1, "personCode": "P_alpha"}).json()
    assert got["page"]["total"] == 1, got
    assert [i["photoCode"] for i in got["page"]["items"]] == ["PH_NODATE"]


def test_timelineUnknownMode(api_env):
    """`unknown=1`：`takenAt` 为空的照片**必须能被翻到**（不能只报一个数字）。

    ⚠️ 这批照片（截图类）既不在年表里、也不会出现在按年翻页的结果里 ——
       只给 `unknownCount` 数字等于让它们在 UI 上消失。
    """
    base = api_env["client"].get("/api/timeline").json()
    assert base["unknownCount"] == 1                    # PH_NODATE
    assert all(y["year"] is not None for y in base["items"])
    # 年表里的总数 + unknown = 全量
    assert sum(y["count"] for y in base["items"]) + base["unknownCount"] == base["total"]

    got = api_env["client"].get("/api/timeline", params={"unknown": 1}).json()
    assert got["scope"] == "unknown"
    assert got["page"]["total"] == 1
    assert got["page"]["items"][0]["photoCode"] == "PH_NODATE"
    assert got["page"]["items"][0]["takenAt"] is None
    assert got["page"]["items"][0]["shotYear"] is None
    assert "recID" in got["note"]                       # 说明排序依据
    # 与 year/month 互斥
    bad = api_env["client"].get("/api/timeline",
                                params={"unknown": 1, "year": 2013})
    assert bad.status_code == 400
    assert bad.json()["code"] == "PARAM_INVALID"


def test_timelineUnknownModeHasFaceFilter(api_env):
    got = api_env["client"].get("/api/timeline",
                                params={"unknown": 1, "hasFace": 1}).json()
    assert got["page"]["total"] == 1                    # PH_NODATE 有 1 张脸
    got = api_env["client"].get("/api/timeline",
                                params={"unknown": 1, "hasFace": 0}).json()
    assert got["page"]["total"] == 0


# ============================================================
# 四、照片列表筛选
# ============================================================

def test_photosDefaultOrderAndTotal(api_env):
    body = api_env["client"].get("/api/photos", params={"size": 50}).json()
    assert body["total"] == 9                       # 构造了9 张
    assert body["hasMore"] is False
    codes = [i["photoCode"] for i in body["items"]]
    # 默认 takenAt DESC：PH_NODATE(takenAt 为空)在最后
    assert codes[-1] == "PH_NODATE"


def test_photosFilterShotYearRange(api_env):
    body = api_env["client"].get(
        "/api/photos", params={"shotYearFrom": 2016, "shotYearTo": 2020,
                               "size": 50}).json()
    years = sorted({i["shotYear"] for i in body["items"]})
    assert years == [2016, 2019, 2020]
    assert body["total"] == 3


def test_photosFilterHasFace(api_env):
    withFace = api_env["client"].get(
        "/api/photos", params={"hasFace": 1, "size": 50}).json()
    without = api_env["client"].get(
        "/api/photos", params={"hasFace": 0, "size": 50}).json()
    assert all(i["faceCount"] > 0 for i in withFace["items"])
    assert all(i["faceCount"] == 0 for i in without["items"])
    assert withFace["total"] + without["total"] == 9


def test_photosFilterIsDuplicate(api_env):
    body = api_env["client"].get("/api/photos",
                                  params={"isDuplicate": 1, "size": 50}).json()
    assert [i["photoCode"] for i in body["items"]] == ["PH_DUP"]


def test_photosFilterPlaceName(api_env):
    body = api_env["client"].get("/api/photos",
                                  params={"placeName": "北京", "size": 50}).json()
    assert {i["placeName"] for i in body["items"]} == {"北京"}
    assert body["total"] == 3          # PH_2013_01 / PH_2013_07 / PH_DUP


def test_photosFilterPersonCode(api_env, api_rows):
    """按人筛选走 pb_photo_person 关联表 —— 先认领一张脸再建关联。"""
    from processor.review import assigner as assigner

    assigner.confirm("FC_PH_2013_01_0", "P_alpha")
    assigner.confirm("FC_PH_2016_03_0", "P_alpha")
    body = api_env["client"].get("/api/photos",
                                  params={"personCode": "P_alpha", "size": 50}).json()
    assert sorted(i["photoCode"] for i in body["items"]) == ["PH_2013_01", "PH_2016_03"]
    assert body["total"] == 2


def test_photosMultiPersonAndOr(api_env):
    from processor.review import assigner as assigner

    assigner.confirm("FC_PH_2013_01_0", "P_alpha")     # PH_2013_01
    assigner.confirm("FC_PH_2016_03_0", "P_alpha")     # PH_2016_03
    assigner.confirm("FC_PH_2013_01_1", "P_beta")      # 同一张 -> 合影
    base = {"personCodes": "P_alpha,P_beta", "size": 50}
    orBody = api_env["client"].get("/api/photos",
                                   params=dict(base, mode="or")).json()
    andBody = api_env["client"].get("/api/photos",
                                    params=dict(base, mode="and")).json()
    assert sorted(i["photoCode"] for i in orBody["items"]) == ["PH_2013_01", "PH_2016_03"]
    assert [i["photoCode"] for i in andBody["items"]] == ["PH_2013_01"]


def test_photosBadSortRejected(api_env):
    got = api_env["client"].get("/api/photos", params={"orderBy": "rm -rf"})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


# ============================================================
# 五、人物 / 照片详情 / places / overview
# ============================================================

def test_personsListHasCounts(api_env):
    from processor.review import assigner as assigner

    assigner.confirm("FC_PH_2013_01_0", "P_alpha")
    assigner.autoAssign("FC_PH_2016_03_0", "P_alpha", 0.66)
    body = api_env["client"].get("/api/persons",
                                  params={"keyword": "阿尔法", "size": 10}).json()
    assert body["total"] == 1
    one = body["items"][0]
    assert one["personCode"] == "P_alpha"
    assert one["photoCount"] == 2
    assert one["faceCount"] == 2
    assert one["confirmedFaceCount"] == 1
    assert one["autoFaceCount"] == 1
    assert one["yearLow"] == 2013 and one["yearHigh"] == 2016


def test_personsFilterDelFlag(api_env):
    body = api_env["client"].get("/api/persons",
                                  params={"delFlag": "1", "size": 50}).json()
    assert body["total"] == 0


def test_personDetailBuckets(api_env):
    from processor.review import assigner as assigner

    for code in ("FC_PH_2013_01_0", "FC_PH_2013_07_0", "FC_PH_2016_03_0"):
        assigner.confirm(code, "P_alpha")
    body = api_env["client"].get("/api/persons/P_alpha").json()
    assert body["displayName"] == "阿尔法"
    assert body["birthday"] == "1985-03-07"
    keys = [b["bucketKey"] for b in body["buckets"]]
    assert keys == sorted(keys)
    assert sum(b["faceCount"] for b in body["buckets"]) == 3
    assert sum(b["confirmedCount"] for b in body["buckets"]) == 3
    # ⚠️ 质心表里**多一个 `ALL` 兜底桶**（该人的全部确认样本，不分桶）；
    #   它不对应任何 pb_face.shotBucket 值，所以 `buckets` 里不该有它。
    #   断言这一点是为了守住「ALL 是虚拟桶」的语义 —— 哪天有人把 ALL
    #   也塞进 buckets，UI 就会画出一条不存在的年代。
    assert "ALL" not in keys
    centroidKeys = sorted(c["bucketKey"] for c in body["centroids"])
    assert centroidKeys == sorted(keys + ["ALL"])


def test_photoDetailFacesAndStates(api_env):
    from processor.review import assigner as assigner

    assigner.confirm("FC_PH_2013_01_0", "P_alpha")
    assigner.autoAssign("FC_PH_2013_01_1", "P_alpha", 0.61)
    body = api_env["client"].get("/api/photos/PH_2013_01").json()
    assert body["faceCount"] == 2
    assert body["stateCounts"] == {"pending": 0, "confirmed": 1,
                                   "disputed": 1, "stranger": 0}
    assert sorted(f["state"] for f in body["faces"]) == ["confirmed", "disputed"]
    assert [p["personCode"] for p in body["persons"]] == ["P_alpha"]
    assert body["persons"][0]["displayName"] == "阿尔法"
    assert body["exif"]["cameraModel"] == "Canon EOS 350D"
    assert body["gps"]["placeName"] == "北京"


def test_placesGrouping(api_env):
    body = api_env["client"].get("/api/places").json()
    got = {i["placeName"]: i["photoCount"] for i in body["items"]}
    assert got == {"北京": 3, "上海": 2, "广州": 1}
    assert body["total"] == 3          # total 是**不同地点的个数**，不是照片数


def test_overviewCounts(api_env):
    body = api_env["client"].get("/api/overview").json()
    assert body["photoCount"] == 9
    assert body["personCount"] == 5               # 3 个 + 2 个共享手机号的
    # 构造了 10 张脸，全部未归属 -> 全部在待确认队列
    assert body["pendingCount"] == 10
    assert body["disputedCount"] == 0


# ============================================================
# 六、扫描接口
# ============================================================

def test_scanStartStatusJobsFlow(api_env):
    import time

    client = api_env["client"]
    started = client.post("/api/scan/start", json={"batchSize": 2, "maxBatches": 1})
    assert started.status_code == 200, started.text
    jobCode = started.json()["jobCode"]

    # ⚠️ 轮询条件必须是「到达终态」而不是「不再是 RUNNING」——
    #    startBackground 是**立刻返回**的，此刻 jobStatus 还是 IDLE，
    #    线程还没跑到 runBatch。若只等「!= RUNNING」，会在 IDLE 上立刻退出，
    #    用例结束时后台线程还在读库 -> 收尾 closeDb() 撞上未释放的句柄
    #    -> **进程级 access violation**（不是 Python 异常，整个测试会话崩）。
    deadline = time.time() + 20.0
    status = {}
    while time.time() < deadline:
        status = client.get("/api/scan/status/%s" % jobCode).json()
        if status["jobStatus"] in ("PAUSED", "DONE", "FAILED"):
            break
        time.sleep(0.02)
    assert status.get("jobStatus") in ("PAUSED", "DONE"), status
    for key in ("processedCount", "addedCount", "skippedCount", "duplicateCount",
                "pendingCount", "jobStatus", "lastCursor"):
        assert key in status, "status 缺 %s" % key
    # 这次 start 里**一个文件都没扫**（photo 目录是空的），所以计数全 0
    assert status["processedCount"] == 0

    jobs = client.get("/api/scan/jobs").json()
    assert jobs["total"] >= 1
    assert any(j["jobCode"] == jobCode for j in jobs["items"])

    # jobCode 幂等：再 start 一次不会新建
    again = client.post("/api/scan/start", json={"jobCode": jobCode,
                                                 "countTotal": True}).json()
    assert again["jobCode"] == jobCode and again["created"] is False

    stop = client.post("/api/scan/stop/%s" % jobCode).json()
    assert stop["ok"] is True and stop["stopped"] is True, stop


def test_scanStatusUnknownJob404(api_env):
    got = api_env["client"].get("/api/scan/status/SJ_压根不存在")
    assert got.status_code == 404
    assert got.json()["code"] == "NOT_FOUND"


def test_scanJobsBadStatusRejected(api_env):
    got = api_env["client"].get("/api/scan/jobs", params={"jobStatus": "WTF"})
    assert got.status_code == 400
    assert got.json()["code"] == "PARAM_INVALID"


# ============================================================
# 七、DR-17 / DR-19 禁令（走查型断言）
# ============================================================

def test_headStillWorksOnStaticRoutes(api_env):
    """HEAD 必须**在 HTTP 层仍然可用**（只是不出现在 OpenAPI 里）。

    ⚠️ 这条不能省：我们把 `methods=["GET","HEAD"]` 改成 `get(...)` 是**依赖
       Starlette 自动补 HEAD** 这个行为。那次改动如果哪天被「清理」掉
       （比如有人为了"显式"又加回 HEAD），本用例不会红；
       但如果 Starlette 的自动补 HEAD 行为变了、或有人加了中间件屏蔽 HEAD，
       本用例会红 —— 而 `/docs` 的缺 HEAD 那一条**永远查不出来**这个。

    判据：HEAD 的状态码必须与 GET **一致**，且**不能是 405**
    （405 = 方法压根没注册，那才是真坏了）。
    """
    client = api_env["client"]
    for url in ("/api/thumb/PH_2013_01", "/api/original/PH_2013_01",
                "/api/face/FC_PH_2013_01_0"):
        got = client.head(url)
        assert got.status_code != 405, "%s 的 HEAD 没注册（405）" % url
        assert got.status_code == client.get(url).status_code, \
            "%s 的 HEAD 与 GET 状态码不一致" % url
        # HEAD 按 RFC 7231 不带消息体
        assert got.content == b"", "%s 的 HEAD 竟然带回了消息体" % url


def test_noExportEndpoint(api_env):
    """**没有任何导出接口**（DR-17）。

    ⚠️ `/api/contacts/*` 下的路径会命中 `/api/contacts/{personCode}` 这个
       **已存在的**路由（它只注册了 PATCH），所以是 **405** 而不是 404。
       两者都表示「不是一个可用的导出接口」。
       真正的「导出接口存在」由 `test_openapiHasNoExportOrDelete`
       在**路由表**层面抓住 —— 那才是权威判据（HTTP 层会被路径参数吃掉）。
    """
    for url in ("/api/contacts/export", "/api/contacts/CSV",
                "/api/contacts/vcard", "/api/contacts/duplicates/export",
                "/api/clusters/export", "/api/photos/export",
                "/api/review/export"):
        got = api_env["client"].get(url)
        assert got.status_code in (404, 405), \
            "%s 返回了 %d（导出接口不该存在）" % (url, got.status_code)


def test_noDeleteContactEndpoint(api_env):
    """**没有 DELETE /api/contacts/{personCode}**（DR-19：只允许停用）。"""
    got = api_env["client"].delete("/api/contacts/P_alpha")
    assert got.status_code in (404, 405), got.status_code


def test_openapiHasNoExportOrDelete(api_env):
    """同一禁令在 OpenAPI 层面再确认一次（路由表是权威）。"""
    paths = api_env["client"].get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        assert "export" not in path.lower(), "出现导出路径: %s" % path
        assert "delete" not in methods, "%s 暴露了 DELETE" % path


# ============================================================
# 八、原图零风险（验收第 15 条）
# ============================================================

def test_photoDirUntouchedByReadOnlyApi(api_env, tmp_path):
    """**前后比对 photo 目录的文件数与总字节数**。

    ⚠️ 只测「读类接口」是不够的 —— 真正要证明的是「任何接口都不会写原图」。
      这里做两件事：
        ① 读完接口后比对 photo 目录指纹；
        ② **代码走查**：`photoDir` 被触碰的路径只有 `thumbStore` 的读函数，
           而写入侧的 `thumbStore.assertNotPhoto` 会拒写 photo 之下 ——
           这条由 test_thumb_store.py 守着，这里只确认指纹不变。
    """
    def fingerprint(root):
        files, total = 0, 0
        for base, _dirs, names in os.walk(str(root)):
            for name in names:
                files += 1
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
        return files, total

    photoDir = os.path.join(str(api_env["root"]), "photo")
    before = fingerprint(photoDir)
    client = api_env["client"]
    for url in ("/api/timeline", "/api/photos?size=50", "/api/persons",
                "/api/overview", "/api/places"):
        assert client.get(url).status_code == 200
    assert client.get("/api/thumb/PH_2013_01").status_code in (200, 404, 422, 500)
    assert client.get("/api/original/PH_2013_01", headers={"Range": "bytes=0-99"}).status_code in (200, 404)
    after = fingerprint(photoDir)
    assert before == after, "photo 目录被改动了: %s -> %s" % (before, after)
