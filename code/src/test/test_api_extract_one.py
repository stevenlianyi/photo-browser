#! /usr/bin/env python3
#encoding: utf-8:

#Filename: test_api_extract_one.py
#Description: 「识别这张照片人脸」接口测试（api/face.py 的 /facerec/extract-one）
#
# 为什么这组用例必须存在
# --------------------
#   这个端点补的是一条**断链**：`/facerec/start` 是全库任务（取数走
#   `scanState = 0` 全表分页），没有 photoCode 的概念 —— 于是「我就要看
#   这一张照片里是谁」在界面上无处可点。
#   而它新增的每一样东西**都能静默出错**：
#     ① 单张路径与批量路径的**引擎参数必须同源**，否则同一张脸"批量能检出、
#        单张检不出"，表现为随机结果（所以打桩的是口径，不是数值）；
#     ② 重提取会清掉这张照片上已有的归属 —— 而 `commitOne` 只删 pb_face、
#        **不碰 pb_photo_person**，漏了就是「人物时间轴里还有这张照片、
#        点进去一张脸都没有」（幽灵关联），库本身不报任何错；
#     ③ 被删的确认样本若没从质心里剔掉，就会"越用越不准"；
#     ④ 匹配必须**只判这一张**：复用 /match/run 会把整库队列一起跑掉，
#        而用户点的是"这张照片"。
#   所以每条断言都尽量**绕过 api 层直查库**（api_rows 夹具），并额外用
#   `assigner.verifyLinks()["clean"]` 做一次全局一致性核对 ——
#   「用 api 自己报的数字证明 api 自己写对了」是自证。
#
# ⚠️ 引擎是**打桩**的（_FakeEngine），这是刻意的
# ----------------------------------------------
#   真跑 insightface 有两个不可接受的代价：① 单测依赖 300MB 权重是否已下载、
#   ONNX 后端是否可用；② 真拿 64×48 的合成色块图去检测**检不出人脸**，
#   那就永远只能测「0 张脸」这一个分支。
#   打桩之后测的正是这个端点唯一新增的东西 —— **编排**：
#   落库、清旧归属、只判这一张、拒绝并发、状态口径。
#
# ⚠️ 与 test_api_match.py 的 primed 是**同一个前提**（每人 >=3 张人工确认样本
#    才有启用的质心）。这里不 import 它：fixture 不跨文件共享。

import time

import pytest

#: P_alpha 的确认样本（4 张 -> ALL 兜底桶样本数 4 >= MIN_CENTROID_SAMPLES）
ALPHA_FACES = ("FC_PH_2013_01_0", "FC_PH_2013_01_1",
               "FC_PH_2013_07_0", "FC_PH_2020_11_0")
#: P_beta 的确认样本（3 张）
BETA_FACES = ("FC_PH_2016_03_0", "FC_PH_2016_03_1", "FC_PH_2019_04_0")

#: 构造数据里的照片 -> 相对路径（见 conftest.api_env 的 plan）
PHOTO_RELPATH = {
    "PH_2013_01": "2013/01/a.jpg",
    "PH_2013_07": "2013/07/b.jpg",
    "PH_2016_03": "2016/03/c.jpg",
    "PH_2020_11": "2020/11/d.jpg",
    "PH_2024_06": "2024/06/e.jpg",
    "PH_2024_06b": "2024/06/f.jpg",
    "PH_2019_04": "2019/04/g.jpg",
    "PH_NODATE": "misc/h.jpg",
}

_WHERE_PENDING = "personCode IS NULL AND isStranger = %s AND delFlag = %s"

#: 打桩引擎"检出"的新脸编码。**必须是合法十六进制**：
#: thumbStore 会校验 faceCode（非法时人脸裁剪图写不出去，而它只记 warning
#: 不抛异常 —— 正是那种"看着过了、其实少了一半产物"的坑）。
NEW_FACE_1 = "fc0000000000000000000000000000a1"
NEW_FACE_2 = "fc0000000000000000000000000000a2"
NEW_FACE_3 = "fc0000000000000000000000000000a3"


# ============================================================
# 装置
# ============================================================

@pytest.fixture(autouse=True)
def waitFaceOneIdle():
    """用例结束时**必须确认单张识别线程已退出**。

    ⚠️ 它不是 FaceScheduler 的后台线程：`api.face.resetScheduler()` **等不到它**
       （lifespan 收尾只等调度器）。线程还在写库时 TestClient 退出会走
       closeDb() —— 那不是 Python 异常，是**进程级 access violation**。
    """
    from api import face as faceApi

    yield
    deadline = time.time() + 60.0
    while time.time() < deadline and faceApi._oneRunning():
        time.sleep(0.05)
    assert not faceApi._oneRunning(), \
        "单张识别线程在 60s 内没退出（后面的收尾可能撞上已释放的库句柄）"


@pytest.fixture
def primed(api_env):
    """两个人各有 >=3 张人工确认样本 -> 质心已启用（MIN_CENTROID_SAMPLES=3）。

    与 test_api_match.py 同一条：512 维随机向量的质心与任何单张样本的余弦
    只有 ~0.5（灰区），所以必须**统一向量**才造得出"必然被自动归属"的脸。
    """
    from processor.review import assigner as assigner

    for code in ALPHA_FACES:
        assigner.confirm(code, "P_alpha")
    for code in BETA_FACES:
        assigner.confirm(code, "P_beta")
    _unifyVectors(ALPHA_FACES, "P_alpha")
    return api_env


def _unifyVectors(faceCodes, personCode: str) -> bytes:
    """把一组脸的向量统一成第一张的样子，并重算该人的质心。返回那个向量。"""
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


def _blobOf(faceCode: str) -> bytes:
    """取某张脸的向量（造「跟 TA 长得一样」的新脸时用）"""
    from database.auto_generated import sqliteCommon as sqliteCommon

    rows = sqliteCommon.query_pb_face("pb_face", mode="full",
                                      faceCode=str(faceCode))
    assert rows, "找不到脸 %s" % faceCode
    blob = bytes(rows[0].get("embedding") or b"")
    assert blob, "脸 %s 没有向量" % faceCode
    return blob


class _FakeEngine(object):
    """假引擎：回放预先算好的结果，**不加载任何模型、不碰数据库**。"""

    def __init__(self, faces, rawCount=None, ok=True, errMsg=""):
        self._faces = list(faces or ())
        self._rawCount = len(self._faces) if rawCount is None else int(rawCount)
        self._ok = bool(ok)
        self._errMsg = str(errMsg)
        self.calls = []

    def extract(self, absPath, photoCode="", wantCrop=None):
        self.calls.append((absPath, photoCode))
        return {"ok": self._ok, "absPath": absPath, "photoCode": photoCode,
                "faces": list(self._faces) if self._ok else [],
                "rejected": {}, "droppedFaces": [],
                "kept": len(self._faces) if self._ok else 0,
                "rawCount": self._rawCount, "imgW": 64, "imgH": 48,
                "elapsed": 0.01, "errCode": 0, "errMsg": self._errMsg}


def _face(faceCode: str, photoCode: str, embedding=None,
          bbox=(0.1, 0.1, 0.2, 0.2)) -> dict:
    """一张假人脸。

    字段名与 FaceHit.toDict() 一致（是 **bbox**，不是 faceBox）；
    bbox 要的是**数值四元组**（入库时才由 formatFaceBox 转成 "x,y,w,h" 字符串）——
    直接给字符串会在落库时抛 `could not convert string to float`。
    """
    return {"faceCode": faceCode, "photoCode": photoCode, "bbox": bbox,
            "detScore": 0.95, "quality": 0.9,
            "poseYaw": 0.0, "posePitch": 0.0, "embedding": embedding}


def _installEngine(monkeypatch, engine) -> None:
    """把假引擎装到 `engine.face.engine.getEngine` 上。

    ⚠️ 打桩点是**模块属性**（api/face.py 里是 `from engine.face import engine as
       faceEngine` 再 `faceEngine.getEngine(...)`），所以改的就是这一处。
    """
    from engine.face import engine as faceEngine

    monkeypatch.setattr(faceEngine, "getEngine", lambda **kwargs: engine)


def _installPreflightOk(monkeypatch) -> None:
    """依赖自检打桩为「通过」。

    ⚠️ 这是**环境**检查（onnxruntime / cv2 装没装），不是本端点的逻辑；
       它自己有一个专门的反向用例（test_preflightFailureIsRejected）。
    """
    from schedule import faceScheduler as faceSched

    monkeypatch.setattr(faceSched, "preflight",
                        lambda: {"ok": True, "missing": (), "optional": (), "hint": ""})


def _makePhotoFile(api_env, make_photo, photoCode: str, relPath: str = None):
    """把构造数据里的那张照片**真的写到磁盘上**。

    端点在起线程前会校验 `os.path.isfile(absPath)`（拔盘/挪走了就不该白跑
    一遍模型），而 api_env 只建了空的 photo/ 目录。
    """
    import os

    rel = relPath or PHOTO_RELPATH[photoCode]
    path = os.path.join(str(api_env["root"]), "photo", rel.replace("/", os.sep))
    return make_photo(path)


def _waitOne(client, timeout: float = 60.0) -> dict:
    """轮询到 running=false。

    ⚠️ POST 返回只代表「已受理」（后台线程还没跑完）。不等它跑完就读计数，
    读到的一定是半截状态 —— 这正是「进度不动」类问题的来源。
    """
    deadline = time.time() + float(timeout)
    body = {}
    while time.time() < deadline:
        body = client.get("/api/facerec/extract-one/status").json()
        if not body.get("running"):
            return body
        time.sleep(0.05)
    pytest.fail("单张识别在 %.1fs 内没跑完：%s" % (timeout, body))


def _post(client, photoCode: str, **extra):
    payload = {"photoCode": photoCode}
    payload.update(extra)
    return client.post("/api/facerec/extract-one", json=payload)


# ============================================================
# 一、前置校验：该拒绝的在起线程之前就拒绝
# ============================================================

def test_photoCodeIsRequired(api_env):
    """缺 photoCode -> 400 + 一句人话（不是起个线程在后台报错）"""
    resp = api_env["client"].post("/api/facerec/extract-one", json={})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "PARAM_INVALID"
    assert "photoCode" in resp.json()["message"]


def test_photoMustExist(api_env):
    """库里没有这张照片 -> 404（而不是跑出一个空结果）"""
    resp = _post(api_env["client"], "PH_不存在")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


def test_missingFileIsRejected(api_env):
    """照片在库里、但原图不在盘上 -> 404。

    不拦住的话会白跑一遍模型（首次还要等 3~10s 载权重），
    最后得到一个「检出 0 张脸」—— 与「照片里确实没人脸」无从分辨。
    """
    resp = _post(api_env["client"], "PH_2024_06")     # 刻意**不**造文件
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "NOT_FOUND"
    assert "磁盘" in body["message"]


def test_preflightFailureIsRejected(api_env, monkeypatch):
    """缺 onnxruntime/cv2 -> 400 + 自检的 hint（不是静默检出 0 张脸）"""
    from schedule import faceScheduler as faceSched

    monkeypatch.setattr(faceSched, "preflight",
                        lambda: {"ok": False,
                                 "missing": (("onnxruntime", "推理后端"),),
                                 "optional": (),
                                 "hint": "缺 onnxruntime，无法识别人脸"})
    resp = _post(api_env["client"], "PH_2024_06")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "PARAM_INVALID"
    assert "onnxruntime" in body["message"]


def test_busyWriterIsRejected(api_env):
    """已有任务在写库 -> 409，且**说得出是谁在占着**（不排队）。

    这与 /facerec/start 的并发语义一致：单写入者不允许并发。
    排队会让按钮看起来"点了没反应"，而静默排队几十秒后突然开始写库更难解释。
    """
    from schedule import scanScheduler as scanSched

    assert scanSched.acquireWriteLock("SJ_20261013_测试") is True
    try:
        resp = _post(api_env["client"], "PH_2024_06")
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "TASK_STATE_ILLEGAL"
        assert "SJ_20261013_测试" in body["message"]
    finally:
        scanSched.releaseWriteLock()


# ============================================================
# 二、只判这一张：写进库、自动归属、别的照片一根汗毛都不动
# ============================================================

def test_extractAndAutoAssignThisPhotoOnly(primed, api_rows, make_photo, monkeypatch):
    """识别一张照片 -> 新脸落库 -> 自动归属（isConfirmed=0）-> **别的照片不动**

    三个断言分别对应三件事：
      · 新脸以 personCode=P_alpha + isConfirmed=0 落库 —— 自动归属的去处是
        「我不同意」（DR-16），少一个 isConfirmed=0 用户就永远看不到这张错认的脸；
      · pb_photo 的 faceCount / scanState 同步 —— 不同步就是「详情说 1 张脸、
        网格角标说 0 张」这种两边各自自洽的矛盾；
      · **别人保持待确认** —— 匹配若复用全库队列，会在这里被抓住。
    """
    client = primed["client"]
    _installPreflightOk(monkeypatch)
    blob = _blobOf(ALPHA_FACES[0])                     # 与 P_alpha 长得一模一样
    engine = _FakeEngine([_face(NEW_FACE_1, "PH_2024_06", embedding=blob)])
    _installEngine(monkeypatch, engine)
    _makePhotoFile(primed, make_photo, "PH_2024_06")

    pendingBefore = api_rows.count("pb_face", _WHERE_PENDING, (0, "0"))
    resp = _post(client, "PH_2024_06", replaceFaces=True, match=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True

    snap = _waitOne(client)
    assert snap["phase"] == "done", snap
    assert snap["lastError"] == ""
    assert snap["faceCount"] == 1
    assert snap["deletedFaces"] == 2, "这张照片原有 2 张脸，重提取应把它们删掉"
    assert snap["matchRan"] is True
    assert snap["autoAssigned"] == 1
    assert [one["displayName"] for one in snap["assignedTo"]] == ["阿尔法"]
    assert "自动归属 1 张" in snap["message"]

    # ---- 直查库核对 ----
    got = api_rows.face(NEW_FACE_1)
    assert got, "新脸没落库"
    assert got.get("personCode") == "P_alpha"
    assert int(got.get("isConfirmed") or 0) == 0, "自动归属必须是 isConfirmed=0"
    assert [one.get("faceCode") for one in api_rows.logs("ASSIGN")], \
        "自动归属必须落 ASSIGN 日志（否则「当初是谁以多少分认下的」查不到）"

    photo = api_rows.photo("PH_2024_06")
    assert int(photo.get("faceCount") or 0) == 1
    assert int(photo.get("scanState") or 0) == 1

    # 旧的 2 张脸已被删除
    assert api_rows.face("FC_PH_2024_06_0") == {}
    assert api_rows.face("FC_PH_2024_06_1") == {}

    # **别的照片不受影响**：这张照片净减 2 条待确认（删 2、新 1 条又被归属）
    assert api_rows.count("pb_face", _WHERE_PENDING, (0, "0")) == pendingBefore - 2
    assert api_rows.face("FC_PH_NODATE_0").get("personCode") in (None, ""), \
        "别的照片的待确认脸被顺带判了 —— 匹配越界跑到全库队列去了"
    from processor.review import assigner as assigner
    assert assigner.verifyLinks()["clean"] is True


def test_noCentroidMeansDetectOnly(api_env, api_rows, make_photo, monkeypatch):
    """一张脸都没确认过 -> 没有质心 -> **只做人脸检测**，并说清为什么。

    这不是故障，是防污染规则的正常结果（质心只由人工确认的样本生成）。
    它必须**照常落库**（检测本身有价值），只是不判是谁。
    """
    client = api_env["client"]
    _installPreflightOk(monkeypatch)
    blob = _blobOf("FC_PH_2024_06_0")
    _installEngine(monkeypatch,
                   _FakeEngine([_face(NEW_FACE_2, "PH_2024_06", embedding=blob)]))
    _makePhotoFile(api_env, make_photo, "PH_2024_06")

    resp = _post(client, "PH_2024_06")
    assert resp.status_code == 200, resp.text
    snap = _waitOne(client)
    assert snap["phase"] == "done", snap
    assert snap["faceCount"] == 1, "没有质心也必须落库（检测本身有用）"
    assert snap["matchRan"] is False
    assert "质心" in snap["matchNote"]
    assert api_rows.face(NEW_FACE_2).get("personCode") in (None, ""), \
        "没有质心却写上了归属"


def test_zeroFacesIsNotAnError(api_env, api_rows, make_photo, monkeypatch):
    """这张照片里没人脸 -> 正常收尾 + 说人话（不是"失败"）。

    「0 张脸」与「依赖缺失 / 文件读不到」必须能分辨 —— 那是三件不同的事。
    """
    client = api_env["client"]
    _installPreflightOk(monkeypatch)
    _installEngine(monkeypatch, _FakeEngine([], rawCount=0))
    _makePhotoFile(api_env, make_photo, "PH_2024_06b")

    resp = _post(client, "PH_2024_06b")
    assert resp.status_code == 200, resp.text
    snap = _waitOne(client)
    assert snap["phase"] == "done", snap
    assert snap["lastError"] == ""
    assert snap["faceCount"] == 0
    assert "检出 0 张人脸" in snap["message"]
    assert api_rows.photo("PH_2024_06b").get("faceCount") is not None


def test_extractFailureIsReported(api_env, api_rows, make_photo, monkeypatch):
    """引擎报错 -> phase=error + errMsg（绝不写库、绝不假装成功）"""
    client = api_env["client"]
    _installPreflightOk(monkeypatch)
    _installEngine(monkeypatch,
                   _FakeEngine([], ok=False, errMsg="解不出像素（文件损坏）"))
    _makePhotoFile(api_env, make_photo, "PH_2024_06")

    resp = _post(client, "PH_2024_06")
    assert resp.status_code == 200, resp.text
    snap = _waitOne(client)
    assert snap["phase"] == "error"
    assert "提取失败" in snap["lastError"]
    assert "文件损坏" in snap["lastError"]
    assert api_rows.face("FC_PH_2024_06_0") != {}, "提取失败却把旧脸删了"


# ============================================================
# 三、重提取的连带后果：幽灵关联 + 质心（这是最容易漏的一环）
# ============================================================

def test_replaceClearsOwnershipAndOrphanLink(primed, api_rows, make_photo, monkeypatch):
    """重提取清掉已有的归属 -> **关联行不能留成幽灵**。

    ⚠️ 为什么这条是这组用例里最重要的
    --------------------------------
      `faceStore._dropOldFaces` 只删 pb_face，**不碰** pb_photo_person。
      不清理的后果是：照片详情（persons 由 faces 派生）说"这张照片没人"，
      而人物时间轴里还有这张照片、点进去一张脸都没有 ——
      `assigner.verifyLinks` 会判 orphanLink，而**库本身不报任何错**。
      批量 replaceFaces 路径原本就有这个缺口（只是几乎没人开那个开关）；
      加了按钮之后它变成用户天天能触发的路径。
    """
    client = primed["client"]
    _installPreflightOk(monkeypatch)
    # 一张"跟谁都对不上"的新脸（向量取自另一张未确认脸）-> 留在待确认
    blob = _blobOf("FC_PH_2024_06_0")
    _installEngine(monkeypatch,
                   _FakeEngine([_face(NEW_FACE_3, "PH_2016_03", embedding=blob)]))
    _makePhotoFile(primed, make_photo, "PH_2016_03")

    assert api_rows.links(photoCode="PH_2016_03"), "构造数据里应当已有 P_beta 的关联行"
    assert len(api_rows.centroids("P_beta")) > 0

    resp = _post(client, "PH_2016_03", replaceFaces=True, match=True)
    assert resp.status_code == 200, resp.text
    snap = _waitOne(client)
    assert snap["phase"] == "done", snap
    assert snap["deletedFaces"] == 2
    assert snap["confirmedBefore"] == 2, "要能报出「清掉了几张人工确认的脸」"
    assert snap["droppedLinks"] >= 1, "幽灵关联没被清掉"
    assert snap["centroidsRecomputed"] >= 1, "受影响人物的质心没重算"

    # ---- 旧脸与旧关联都必须消失 ----
    assert api_rows.face("FC_PH_2016_03_0") == {}
    assert api_rows.face("FC_PH_2016_03_1") == {}
    assert api_rows.links(photoCode="PH_2016_03", personCode="P_beta") == [], \
        "留下幽灵关联：人物时间轴里还会出现这张照片"

    # ---- 全局一致性核对（绕过 api 自己报的数字）----
    from processor.review import assigner as assigner
    report = assigner.verifyLinks()
    assert report["clean"] is True, report

    # ---- 新脸仍是「待确认」：重提取不会顺手把人工确认"复原" ----
    assert api_rows.face(NEW_FACE_3).get("personCode") in (None, "")
    assert api_rows.count("pb_face", _WHERE_PENDING, (0, "0")) >= 1
