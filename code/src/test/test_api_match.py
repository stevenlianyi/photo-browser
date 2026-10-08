#! /usr/bin/env python3
#encoding: utf-8

#Filename: test_api_match.py
#Description: photo-browser 人脸匹配接口测试（步骤 6 的 Web 入口 / api/match.py）
#
# 为什么这组用例必须存在
# --------------------
#   api/match.py 补的是一条**断链**：匹配原本只有 CLI（tools/run_match.py），
#   Web 端没有任何入口，于是「确认了一批脸之后，我不同意永远是 0」。
#   而这个模块做的三件事**每一件都能静默出错**：
#     ① 后台线程 + 门闩：漏释放 -> 之后所有扫描/认脸/匹配被永久挡下（不报错，只是点不动）；
#     ② 预览（assign=false）**不能写库**：写了的话用户看到的统计与库里的状态对不上，
#        而「我不同意」会莫名其妙地涨，用户完全不知道是谁写的；
#     ③ 落库必须幂等：重复点按钮不能重复归属（否则同一张脸会被反复写日志、
#        质心被反复重算，"再跑一次"变成一个危险动作）。
#   所以每条都用**绕过 api 层直查库**来验（api_rows 夹具）—— 与本项目其余
#   api 测试同一方法论：用 api 自己报的数字证明 api 自己写对了，是自证。
#
# ⚠️ 与 test_api_review.py 的 primed 是**同一个前提**（每人 >=3 张人工确认样本
#    才有启用的质心）。这里不 import 它：fixture 不跨文件共享，而且两份
#    确认清单若将来分叉，读者会以为测的是同一份数据。

import time

import pytest

#: P_alpha 的确认样本（4 张 -> ALL 兜底桶样本数 4 >= MIN_CENTROID_SAMPLES）
ALPHA_FACES = ("FC_PH_2013_01_0", "FC_PH_2013_01_1",
               "FC_PH_2013_07_0", "FC_PH_2020_11_0")
#: P_beta 的确认样本（3 张）
BETA_FACES = ("FC_PH_2016_03_0", "FC_PH_2016_03_1", "FC_PH_2019_04_0")

#: 被改造成「长得跟 P_alpha 一模一样」的那张待确认脸 —— 它应当被自动归属
TWIN_FACE = "FC_PH_2024_06_1"
#: 提供向量的那张确认脸（P_alpha 的）
TWIN_SOURCE = "FC_PH_2013_01_0"

_WHERE_PENDING = "personCode IS NULL AND isStranger = %s AND delFlag = %s"
_WHERE_DISPUTED = ("personCode IS NOT NULL AND isConfirmed = %s"
                   " AND isStranger = %s AND delFlag = %s")


@pytest.fixture
def primed(api_env, api_rows):
    """两个人各有 >=3 张人工确认样本 -> 质心已启用（MIN_CENTROID_SAMPLES=3）。

    ⚠️ 确认完之后还要**把 P_alpha 的样本向量统一成同一个**（见 _unifyVectors）：
       否则质心与任何单张样本的余弦只有 ~0.5，永远判不出 auto，
       用例会退化成「跑通就算过」。
    """
    from processor.review import assigner as assigner

    for code in ALPHA_FACES:
        assigner.confirm(code, "P_alpha")
    for code in BETA_FACES:
        assigner.confirm(code, "P_beta")
    _unifyVectors(ALPHA_FACES, "P_alpha")
    return api_env


def _unifyVectors(faceCodes, personCode: str) -> bytes:
    """把一组脸的向量统一成第一张的样子，并**重算该人的质心**。

    为什么必须这么做
    --------------
      512 维随机高斯向量两两余弦 ≈ 0（见 engine/cluster/dbscan.py 的说明），
      4 张随机样本的均值质心与其中任何一张的余弦只有 ≈1/√4≈0.5 ——
      正好落在 T_LOW..T_HIGH 的灰区里，判不出 auto。
      统一向量后质心就是那张脸本身，score=1 > T_HIGH，
      「必然被自动归属的脸」才造得出来。

    ⚠️ 改了样本**必须**重算质心：否则比对用的还是旧质心，
       而库里看不出任何异常（这正是 DR-22 记的那个静默失配）。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon
    from engine.match import centroid as centroid

    rows = sqliteCommon.query_pb_face("pb_face", mode="full",
                                      faceCode=str(faceCodes[0]))
    assert rows, "找不到样本脸 %s" % faceCodes[0]
    blob = bytes(rows[0].get("embedding") or b"")
    assert blob, "样本脸 %s 没有向量" % faceCodes[0]

    for code in faceCodes:
        got = sqliteCommon.query_pb_face("pb_face", mode="full",
                                        faceCode=str(code))
        assert got, "找不到样本脸 %s" % code
        changed = sqliteCommon.update_pb_face("pb_face", int(got[0]["recID"]),
                                              {"embedding": blob})
        assert changed == 1, "统一 %s 的向量失败" % code
    centroid.recomputePerson(str(personCode))
    return blob


def _copyEmbedding(srcFaceCode: str, dstFaceCode: str) -> None:
    """把 dst 的向量改成与 src 完全相同 —— 造一张「必然被自动归属」的脸。

    ⚠️ 刻意**只改向量、不改 shotBucket**：matcher 的候选桶是
       「本桶 + 前后各一桶 ∪ ALL」，而 P_alpha 的 ALL 兜底桶样本数已达下限，
       所以只要向量一致（余弦=1 > T_HIGH）就一定会判 auto。
       这样用例不依赖分桶规则（改了分桶策略也不会莫名其妙地红）。

    ⚠️ 走 `update_pb_face(recID, ...)` 而**不是** upsert：
       pb_face.photoCode 是 NOT NULL，而 upsert 的 INSERT 分支会先撞上它
       （实测：`NOT NULL constraint failed: pb_face.photoCode`）——
       即便冲突后本意只是改一列。
    """
    from database.auto_generated import sqliteCommon as sqliteCommon

    srcRows = sqliteCommon.query_pb_face("pb_face", mode="full",
                                         faceCode=str(srcFaceCode))
    assert srcRows, "构造数据里找不到源脸 %s" % srcFaceCode
    blob = bytes(srcRows[0].get("embedding") or b"")
    assert blob, "源脸 %s 没有向量" % srcFaceCode

    dstRows = sqliteCommon.query_pb_face("pb_face", mode="full",
                                         faceCode=str(dstFaceCode))
    assert dstRows, "构造数据里找不到目标脸 %s" % dstFaceCode
    changed = sqliteCommon.update_pb_face("pb_face", int(dstRows[0]["recID"]),
                                          {"embedding": blob})
    assert changed == 1, "改写 %s 的向量失败（影响 %s 行）" % (dstFaceCode, changed)

    # 回读校验：更新接口若静默把 BLOB 归一化了，后面「为什么没自动归属」
    # 会变成一个查不出来的谜（数字看着都对，就是判定不中）
    back = sqliteCommon.query_pb_face("pb_face", mode="full",
                                      faceCode=str(dstFaceCode))
    assert bytes(back[0].get("embedding") or b"") == blob, \
        "写完回读的向量与源不一致（长度 %d vs %d）" % (
            len(bytes(back[0].get("embedding") or b"")), len(blob))


def _waitDone(client, timeout: float = 60.0) -> dict:
    """轮询到 running=false。

    匹配在**后台线程**里跑，POST 返回只代表「已受理」—— 不等它跑完就读计数，
    读到的一定是半截状态（这正是「进度不动」类问题的来源）。
    """
    deadline = time.time() + float(timeout)
    body = {}
    while time.time() < deadline:
        body = client.get("/api/match/status").json()
        if not body.get("running"):
            return body
        time.sleep(0.05)
    pytest.fail("匹配在 %.1fs 内没跑完：%s" % (timeout, body))


# ============================================================
# 一、前置校验：没有质心就不该让人点（而不是跑出一个空结果）
# ============================================================

def test_noCentroidIsRejected(api_env):
    """一张脸都没确认 -> 没有启用的质心 -> 400 + 一句人话（不是静默跑空）。

    ⚠️ 这是最容易被误当成「功能坏了」的情形：质心只由人工确认的样本生成，
       确认不足 3 张时**必然**没有质心。后端必须把这条规则说出来。
    """
    client = api_env["client"]
    assert client.get("/api/match/status").json()["enabledCentroids"] == 0

    resp = client.post("/api/match/run", json={"assign": True})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "PARAM_INVALID"
    assert "人工确认" in body["message"]


def test_unknownPresetIsRejected(primed):
    """阈值预设必须校验 —— 否则会一路传到 matcher 里变成 KeyError（500）。"""
    resp = primed["client"].post("/api/match/run", json={"preset": "不存在的档位"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "PARAM_INVALID"


# ============================================================
# 二、预览：只算不写
# ============================================================

def test_previewOnlyDoesNotWrite(primed, api_rows):
    """assign=false：判定结果照给，但**一个字节都不写**。

    这条是「给用户看」那一半的地基：用户先看统计再决定落不落库；
    预览若偷偷写了库，用户看到的数字与库里的状态就对不上了。
    """
    client = primed["client"]
    _copyEmbedding(TWIN_SOURCE, TWIN_FACE)
    pendingBefore = api_rows.count("pb_face", _WHERE_PENDING, (0, "0"))
    disputedBefore = api_rows.count("pb_face", _WHERE_DISPUTED, (0, 0, "0"))

    resp = client.post("/api/match/run", json={"assign": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True

    snap = _waitDone(client)
    assert snap["phase"] == "done" and snap["lastError"] == ""
    assert snap["assign"] is False
    assert snap["auto"] >= 1, ("造了一张与 P_alpha 向量相同的脸，应当自动归属；"
                               "实际原因分布=%s" % snap.get("byReason"))
    assert snap["written"] == 0

    assert api_rows.count("pb_face", _WHERE_PENDING, (0, "0")) == pendingBefore
    assert api_rows.count("pb_face", _WHERE_DISPUTED, (0, 0, "0")) == disputedBefore
    assert api_rows.face(TWIN_FACE).get("personCode") in (None, "")


# ============================================================
# 三、落库：写进去、进「我不同意」、且幂等
# ============================================================

def test_assignWritesDisputedAndIsIdempotent(primed, api_rows):
    """assign=true：自动归属落库 -> 「我不同意」增加 -> 再跑一次不重复写。

    三个断言分别对应三件事：
      · written == auto      —— 报给用户的「已落库」必须与判定数一致；
      · disputed 精确增加      —— 自动归属的去处就是「我不同意」（DR-16），
                                少一条用户就永远看不到那张错认的脸；
      · 再跑一次 written == 0 —— 幂等（已归属的脸不再进待确认队列）。
    """
    client = primed["client"]
    _copyEmbedding(TWIN_SOURCE, TWIN_FACE)
    disputedBefore = api_rows.count("pb_face", _WHERE_DISPUTED, (0, 0, "0"))

    resp = client.post("/api/match/run", json={"assign": True})
    assert resp.status_code == 200, resp.text
    snap = _waitDone(client)
    assert snap["phase"] == "done" and snap["lastError"] == ""
    assert snap["auto"] >= 1
    assert snap["written"] == snap["auto"]

    disputedAfter = api_rows.count("pb_face", _WHERE_DISPUTED, (0, 0, "0"))
    assert disputedAfter == disputedBefore + snap["written"]
    assert int(snap["disputedAfter"]) == disputedAfter
    # 门闩必须已释放，否则后续任务全被挡下
    assert snap["busyWriteLock"] is None

    # ---- 幂等：同一批脸再跑一次，不产生新行 ----
    client.post("/api/match/run", json={"assign": True})
    snap2 = _waitDone(client)
    assert snap2["written"] == 0
    assert api_rows.count("pb_face", _WHERE_DISPUTED, (0, 0, "0")) == disputedAfter


# ============================================================
# 四、单写入者：别的任务在写库时，匹配必须被挡下
# ============================================================

def test_writeLockBlocksConcurrentRun(primed):
    """扫描 / 人脸提取正在写库时点匹配 -> 409，且**报出是谁在跑**。

    为什么不能只做前端禁用：「单写入者」是 SQLite 下的硬约束，
    前端禁用只是礼貌，真正的闸必须在这一层。
    """
    from schedule import scanScheduler as scanSched

    client = primed["client"]
    assert scanSched.acquireWriteLock("SJ_test") is True
    try:
        resp = client.post("/api/match/run", json={"assign": True})
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "TASK_STATE_ILLEGAL"
        assert "SJ_test" in body["message"]
    finally:
        scanSched.releaseWriteLock()

    assert scanSched.writeLockOwner() is None
