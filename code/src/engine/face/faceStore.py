#! /usr/bin/env python3
#encoding: utf-8
#Filename: faceStore.py
#Description: photo-browser 人脸落库（步骤 5）—— pb_face 的唯一写入口，只在主进程跑
#
# 职责
# ----
#   1. makeShotBucket()   shotYear -> **等宽占位桶**（不重刷，见下面的说明）
#   2. FaceStore          攒行 -> 批量写 pb_face -> 同步 pb_photo.faceCount / scanState
#   3. writeFaceCrops()   子进程裁好的人脸图原子落盘到 thumb\faces\<xx>\<faceCode>.jpg
#   4. loadPhotoRows()    给编排层/CLI 用的取数（分页，避免 10 万行一次全取）
#
# shotBucket 谁负责重刷（修正步骤 R2 / DR-20 落定，**别再改回「步骤 6 会覆盖」**）
# ---------------------------------------------------------------------------
#   本模块只写**占位桶**：提取那一刻还不知道这张脸是谁，
#   没有年龄就定不了「童年3 年」还是「成年 10 年」，所以先落一个等宽 5 年桶。
#
#   ⚠️⚠️ 早期这里写的是「步骤 6 会用自适应规则重算覆盖」——
#      **那个承诺从未兑现**：步骤 6 只在匹配侧读 shotBucket，从没有任何代码
#      改写它。于是 bucket.py 的自适应分桶在生产里是死代码，
#      而「质心按自适应桶建、匹配按等宽桶取」的两套口径互相错开
#      （S0 的 FR 32.75%->19% 等于一直在跑对照组）。
#
#   现在的口径（**唯一正确的一份**，与 engine/match/rebucket.py 文件头一致）：
#     · 提取落库（这里）        -> 等宽 5 年占位桶
#     · 归属那一刻             -> assigner._setBelong按**新主人的生日**重刷
#                                 （assigner.fix('unknown'/'stranger') 刷回等宽）
#     · 合并 / 撤销            -> merger 按**目标人**的生日重刷
#     · 拆成新建档案           -> 经 assigner.setBelong，生日为空则刷成等宽
#     · 联系人导入后           -> 对本次新建/更新的人重刷 + 重算质心
#     · 存量一次性刷干净       -> python code\src\tools\rebucket_cli.py --all
#   换句话说：**归属变更的那一刻生日才确定，桶键在那时才刷**。
#   本模块保持占位不动是有意的：重提取（replace模式）会把这些脸重写成新行，
#   而它们此刻大多仍未归属，刷成自适应桶反而是无源之水。
#
# 写库口径
# --------
#   faceCode 幂等键：由 (photoCode, bbox) 派生（engine.deriveFaceCode），
#   同一张照片重跑提取 -> 同一个 faceCode -> upsert 命中，不产生重复行。
#
#   upsert 的 updateColumns 刻意不含 personCode / clusterCode / isConfirmed：
#   用户花十分钟人工确认了 200 张脸，你重跑一次提取就把 isConfirmed 全刷成 0，
#   personCode 全刷成 NULL —— 这是「重跑比不跑更糟」的典型。
#   人工确认结果只由步骤 6/7 与 review 接口写，那两条路径不碰 upsert 的列清单。
#
#   重提取（照片内容变了 -> 步骤 3 已把 faceCount 归零）时走 replace 模式：
#   先按 photoCode 硬删该照片的旧人脸行，再插新的。检测模型换一版 bbox 就会变，
#   faceCode 随之变；不删就会留下一批永远查不到、也删不掉的孤儿行
#   （还各自占着 2048 字节向量，10 万张孤儿行 = 200MB 纯浪费）。
#
# 单写入者
# --------
#   本模块只允许主进程主线程调用（由 tools/run_faces.py 或后续步骤 6 的编排层驱动）。
#   一次事务一批（basicSettings.COMMIT_ROWS_PER_TXN = 500 行），与步骤 3 同粒度。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/face
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402
from engine.face import engine as faceEngine                      # noqa: E402
from processor.media import faceCropper as faceCropper            # noqa: E402
from processor.media import thumbStore as thumbStore              # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("faceStore", "facestore.log")

#: 临时年代桶的等宽步长（年）
SHOT_BUCKET_WIDTH: int = 5

#: pb_face 的 upsert 会刷新的列（刻意不含人工确认相关的三列，见文件头说明）
FACE_UPSERT_COLUMNS: tuple = (
    "photoCode", "bbox", "detScore", "poseYaw", "posePitch",
    "quality", "embedding", "shotBucket",
)

#: pb_photo 补丁行必须带齐的 NOT NULL 身份列（走 upsert 是 INSERT 语义，缺列会撞约束）
PHOTO_IDENTITY_COLUMNS: tuple = ("photoCode", "relPath", "relPathHash", "fileHash",
                                 "fileSize")


# ============================================================
# 一、年代桶
# ============================================================

def makeShotBucket(shotYear) -> str:
    """pb_photo.shotYear -> pb_face.shotBucket 的**占位**桶（如 "1995-1999"）。

    拿不到年份（NULL / 截图 / 超出可信区间）-> 返回空串，**不参与跨年代比对**
    （basicSettings.SCREENSHOT_NAME_PREFIXES 的口径：截图的拍摄年份没有意义）。

    ⚠️ 这是**占位口径（等宽 5 年）**，不是最终口径。真正的分桶是自适应的
       （0-18 岁每 3 年 / 18+ 每 10 年），而自适应需要「这张脸属于谁」+
       「那个人的生日」—— **提取时这两样都还不知道**，所以这里定不了。
       重刷由 `engine.match.rebucket` 负责，落点是**归属变更的那一刻**
       （assigner._setBelong / fix('unknown'|'stranger') / merger.merge / undo /
         联系人导入后），存量用 `tools/rebucket_cli.py --all` 刷。
       详见文件头「shotBucket 谁负责重刷」——**不要**再在这里写
       「步骤 6 会覆盖」这种已经失效的承诺（它就是 DR-20 那个 P0 缺口）。

       为什么不干脆留 NULL：留 NULL 的话分不清
       「还没算过」与「这张照片没有拍摄年份（截图）」这两种情况。
    """
    if shotYear is None:
        return ""
    try:
        year = int(shotYear)
    except (TypeError, ValueError):
        return ""
    if year <= 0 or year < basicSettings.SHOT_YEAR_MIN or year > basicSettings.SHOT_YEAR_MAX:
        return ""
    start = year - (year % SHOT_BUCKET_WIDTH)
    return "%d-%d" % (start, start + SHOT_BUCKET_WIDTH - 1)


# ============================================================
# 二、行构造
# ============================================================

def _round2(value):
    if value is None:
        return None
    return round(float(value), 2)


def buildFaceRow(photoRow: dict, faceData: dict, shotBucket: str = None) -> dict:
    """把子进程回传的一张人脸 dict 拼成 pb_face 行。

    faceData 就是 FaceHit.toDict()：{faceCode, bbox, detScore, poseYaw, posePitch,
    embedding(bytes 2048), quality, isPrimary, areaRatio, shortEdge, cropData}

    ⚠️ 字段名必须是 **bbox**（不是 faceBox）—— 步骤 4 的 faceCropper 注释里专门
       警告过「读错列名还不报错」这件事，写入侧同样只认 bbox。
       pose 为 None 时（裸 ONNX 降级且均值形状缺失）这一列留空，
       normalizeDataSet 会把空值整条丢掉 —— 也就是**不写这一列**，
       而不是写 0（写 0 会被读成"绝对正视"，比"没测到"更糟）。
    """
    bucket = (makeShotBucket((photoRow or {}).get("shotYear"))
              if shotBucket is None else str(shotBucket or ""))
    return {
        "faceCode": str(faceData.get("faceCode") or ""),
        "photoCode": str((photoRow or {}).get("photoCode")
                         or faceData.get("photoCode") or ""),
        "personCode": None,            # 步骤 6 才归属
        "clusterCode": None,           # 步骤 7 才聚类
        "bbox": faceCropper.formatFaceBox(faceData["bbox"]),
        "detScore": round(float(faceData.get("detScore") or 0.0), 4),
        "poseYaw": _round2(faceData.get("poseYaw")),
        "posePitch": _round2(faceData.get("posePitch")),
        "quality": round(float(faceData.get("quality") or 0.0), 4),
        "embedding": faceData.get("embedding"),
        "shotBucket": bucket or None,
        "isConfirmed": 0,              # 人工确认前一律 0
    }


def buildPhotoPatchRow(photoRow: dict, faceCount: int) -> dict:
    """pb_photo 的补丁行：只改 faceCount 与 scanState。

    必须带齐 NOT NULL 身份列（走的是 INSERT ... ON CONFLICT 的 upsert 语义），
    否则撞 `NOT NULL constraint failed: relPath` —— 步骤 3 已经踩过一次。
    """
    row = dict((name, photoRow.get(name)) for name in PHOTO_IDENTITY_COLUMNS)
    row["faceCount"] = int(faceCount)
    row["scanState"] = comGD.SCAN_STATE_FACED      # 0 -> 1（"已入人脸库"）
    row["modifyYMDHMS"] = misc.getTime()
    return row


# ============================================================
# 三、人脸裁剪图落盘
# ============================================================

def writeFaceCrops(faces: list, absPath: str, thumbRoot: str = None,
                   photoRoot: str = None, force: bool = False) -> dict:
    """把子进程裁好的人脸图原子写到 thumb\\faces\\<xx>\\<faceCode>.jpg。

    落盘一律走 thumbStore.write_atomic（先写 *.tmp 再 os.replace，中断不留半文件），
    并带上「目标是 photo 目录就拒写」的守卫（photoDir 绝对只读）。

    返回 {created, cached, failed, failures:[(faceCode, errMsg)]}
    """
    stat = {"created": 0, "cached": 0, "failed": 0, "failures": []}
    for faceData in faces or ():
        faceCode = str(faceData.get("faceCode") or "")
        data = faceData.get("cropData")
        if not faceCode:
            stat["failed"] += 1
            stat["failures"].append(("", "faceCode 为空"))
            continue
        try:
            target = thumbStore.face_abspath(faceCode, thumbRoot=thumbRoot)
        except thumbStore.ThumbStoreError as e:
            stat["failed"] += 1
            stat["failures"].append((faceCode, str(e)))
            continue
        if data is None:
            # 子进程没裁出来（safeCrop 失败）：库里的行照写，只是没有头像图。
            # 宁可「有人脸行没图」，也不要「有图没行」—— 后者会让
            # /api/thumb?face= 404，而前者只是头像位暂时空白。
            if not force and thumbStore.exists(target):
                stat["cached"] += 1
            else:
                stat["failed"] += 1
                stat["failures"].append((faceCode, "子进程未返回裁剪数据"))
            continue
        try:
            thumbStore.write_atomic(target, data, tag="face", photoRoot=photoRoot)
            stat["created"] += 1
        except Exception as e:
            stat["failed"] += 1
            stat["failures"].append((faceCode, "%s: %s" % (type(e).__name__, e)))
            _LOG.error("人脸图落盘失败 %s: %s", faceCode, e)
    return stat


# ============================================================
# 四、FaceStore
# ============================================================

class FaceStoreError(Exception):
    """写库失败（已回滚）。上层应当停止本批而不是假装成功。"""


class FaceStore(object):
    """攒人脸行 -> 批量写 pb_face -> 同步 pb_photo 计数。**只在主进程用。**

    用法
    ----
        store = FaceStore(thumbRoot=..., photoRoot=...)
        store.submit(photoRow, result)          # 攒行 + 落人脸图
        ... 攒够 500 张 ...
        store.flush()                            # 一次事务写完
        store.summary()

    为什么「写 pb_face」与「写 pb_photo 计数」在**同一个 flush 同一个事务**里
    ---------------------------------------------------------------------
      分两次写就会出现「人脸行已落库、faceCount 还是 0」的中间态。
      那种状态在库里查不出来（faceCount=0 的照片不会显示人脸角标），
      但重跑一次就"好了"，于是永远查不到第一次为什么不对。
      一次 flush = 一个事务 = 要么都成要么都回滚。
    """

    def __init__(self, dbFile: str = None, photoRoot: str = None,
                 thumbRoot: str = None, flushRows: int = None,
                 replaceFaces: bool = False, ownerID: str = None):
        # DR-10：必须**显式** dbHandle(dbFile) 才切库；无参调用绝不切
        if dbFile:
            sqliteCommon.dbHandle(dbFile)
        self.dbFile = dbFile
        self.photoRoot = os.path.abspath(photoRoot or paths.photo_dir())
        self.thumbRoot = os.path.abspath(thumbRoot or paths.thumb_dir())
        self.flushRows = int(flushRows or basicSettings.COMMIT_ROWS_PER_TXN)
        #: True = 重提取：先按 photoCode 硬删旧人脸行再插（内容变更后 faceCount 已被归零）
        self.replaceFaces = bool(replaceFaces)
        self.ownerID = ownerID
        self._faceRows = []
        self._photoRows = {}
        self.counters = {"photos": 0, "faces": 0, "deleted": 0,
                         "cropCreated": 0, "cropCached": 0, "cropFailed": 0,
                         "flushes": 0, "faceRows": 0, "photoRows": 0}

    # ---- 攒 ----

    def submit(self, photoRow: dict, result: dict) -> int:
        """收一张照片的提取结果。返回本次入库的人脸行数。"""
        faces = list((result or {}).get("faces") or ())
        photoCode = str((photoRow or {}).get("photoCode") or "")
        if not photoCode:
            return 0
        self.counters["photos"] += 1
        cropStat = writeFaceCrops(faces, (result or {}).get("absPath", ""),
                                  thumbRoot=self.thumbRoot,
                                  photoRoot=self.photoRoot)
        self.counters["cropCreated"] += cropStat["created"]
        self.counters["cropCached"] += cropStat["cached"]
        self.counters["cropFailed"] += cropStat["failed"]
        for faceCode, errMsg in cropStat["failures"]:
            _LOG.warning("人脸图缺失 %s/%s: %s", photoCode, faceCode, errMsg)

        usable = 0
        for faceData in faces:
            row = buildFaceRow(photoRow, faceData)
            if not row["faceCode"]:
                continue
            self._faceRows.append(row)
            self.counters["faces"] += 1
            usable += 1
        # faceCount 用**实际写库的行数**而不是 result.kept：
        # 两者不一致就说明有 faceCode 为空的行被跳过了，那是 bug，得看得见
        self._photoRows[photoCode] = (photoRow, usable)
        if len(self._faceRows) >= self.flushRows or len(self._photoRows) >= self.flushRows:
            self.flush()
        return usable

    def pending(self) -> int:
        return len(self._faceRows)

    # ---- 落库 ----

    def flush(self) -> dict:
        """一次事务写完攒下的行。返回 {faces, photos, deleted}。"""
        faceRows = self._faceRows
        photoItems = self._photoRows
        self._faceRows = []
        self._photoRows = {}
        if not faceRows and not photoItems:
            return {"faces": 0, "photos": 0, "deleted": 0}
        db = sqliteCommon.dbHandle()
        deleted = 0
        try:
            with db.transaction():
                if self.replaceFaces and photoItems:
                    deleted = self._dropOldFaces(sorted(photoItems.keys()))
                if faceRows:
                    rtn, _cols = sqliteCommon.insertManyTableGeneral(
                        "pb_face", faceRows,
                        conflictColumns=("faceCode",),
                        updateColumns=list(FACE_UPSERT_COLUMNS),
                        fillStandard=True,
                        forceColumns=list(FACE_UPSERT_COLUMNS))
                    if rtn == -2:                 # sqliteHandle.RET_ERROR
                        raise FaceStoreError("pb_face 批量写失败（已回滚）: %s"
                                             % db.lastErrMsg)
                if photoItems:
                    rows = [buildPhotoPatchRow(row, count)
                            for row, count in photoItems.values()]
                    rtn, _cols = sqliteCommon.insertManyTableGeneral(
                        "pb_photo", rows,
                        conflictColumns=("photoCode",),
                        updateColumns=("faceCount", "scanState"),
                        fillStandard=True)
                    if rtn == -2:
                        raise FaceStoreError("pb_photo 计数同步失败（已回滚）: %s"
                                             % db.lastErrMsg)
        except FaceStoreError:
            raise
        except Exception as e:
            raise FaceStoreError("人脸落库异常（已回滚）: %s: %s" % (type(e).__name__, e))
        self.counters["flushes"] += 1
        self.counters["faceRows"] += len(faceRows)
        self.counters["photoRows"] += len(photoItems)
        self.counters["deleted"] += deleted
        return {"faces": len(faceRows), "photos": len(photoItems), "deleted": deleted}

    def _dropOldFaces(self, photoCodes: list) -> int:
        """按 photoCode 硬删旧人脸行（重提取用）。返回删除行数。

        走**生成层**的 delete_pb_face（逐行），不写裸 SQL。
        一次重提取涉及的旧行数 = 照片里原有的人脸数（个位数），逐行完全够用；
        真攒到几万行再考虑批量 DELETE。
        """
        deleted = 0
        for photoCode in photoCodes:
            if not photoCode:
                continue
            rows = sqliteCommon.query_pb_face("pb_face", photoCode=photoCode)
            for row in rows:
                rtn = sqliteCommon.delete_pb_face("pb_face", row["recID"],
                                                  hardDelete=True)
                if rtn and rtn > 0:
                    deleted += int(rtn)
        if deleted:
            _LOG.info("重提取：删除 %d 条旧人脸行（%d 张照片）", deleted, len(photoCodes))
        return deleted

    # ---- 收尾 ----

    def summary(self) -> dict:
        data = dict(self.counters)
        data["pending"] = self.pending()
        data["dbFile"] = self.dbFile or sqliteCommon.dbFilePath()
        data["thumbRoot"] = self.thumbRoot
        data["photoRoot"] = self.photoRoot
        return data


# ============================================================
# 五、取数与编排
# ============================================================

def loadPhotoRows(dbFile: str = None, limit: int = 0, offset: int = 0,
                  onlyPending: bool = True, photoRoot: str = None,
                  orderBy: str = "recID", descFlag: bool = False) -> list:
    """取待做特征提取的 pb_photo 行（**分页**，与步骤 3 的 SCAN_INDEX_PAGE 同理）。

    onlyPending=True -> 只要 scanState=0（步骤 3 刚入库、或内容变更后被归零的行）。
    ⚠️ 一次全取 10 万行峰值 150MB（步骤 3 实测），所以给 limit/offset 分页。
       顺手剔掉磁盘上已经没有的（拔盘 / 挪走），否则白跑一遍模型才发现。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    rows = sqliteCommon.query_pb_photo(
        "pb_photo", nullFields=("scanState",) if onlyPending else (),
        orderBy=orderBy, descFlag=descFlag,
        limitNum=int(limit or 0), offsetNum=int(offset or 0))
    root = os.path.abspath(photoRoot or paths.photo_dir())
    out = []
    for row in rows:
        relPath = str(row.get("relPath") or "")
        if not relPath:
            continue
        if not os.path.isfile(os.path.join(root, relPath.replace("/", os.sep))):
            continue
        out.append(row)
    return out


def extractAndStore(photoRows: list, workers: int = None, dbFile: str = None,
                    photoRoot: str = None, thumbRoot: str = None,
                    engineKwargs: dict = None, replaceFaces: bool = False,
                    onProgress=None, batchRows: int = None) -> dict:
    """**主编排**：查库取行（调用方给）-> 进程池提取 -> 主进程单线程批量落库。

    photoRows  : list[pb_photo 行]（photoCode / relPath / shotYear / 身份列齐全）
    workers    : 进程数，None = cpu_count-1
    replaceFaces : True = 重提取（先删旧人脸行）
    onProgress : 可选回调 onProgress(done, total, result)

    返回 {pool: poolSummary, store: storeSummary, timing: {...}}

    为什么编排放在**写库这一层**而不是单独一个模块
    --------------------------------------------
      步骤 5 的全部难点就是"子进程算、主进程写"这条边界。
      边界代码越少越不容易破，所以把它收在 faceStore.extractAndStore 里，
      步骤 6 复用时直接调它，不会另起一份并行实现。
    """
    from engine.face import pool as facePool

    resolvedPhotoRoot = os.path.abspath(photoRoot or paths.photo_dir())
    resolvedThumbRoot = os.path.abspath(thumbRoot or paths.thumb_dir())
    store = FaceStore(dbFile=dbFile, photoRoot=resolvedPhotoRoot,
                      thumbRoot=resolvedThumbRoot,
                      flushRows=batchRows, replaceFaces=replaceFaces)
    # ⚠️ 子进程**绝对路径**（spawn 出来的子进程读不到主进程的配置打桩）
    tasks = []
    for row in photoRows or ():
        relPath = str(row.get("relPath") or "")
        if not relPath:
            continue
        tasks.append((os.path.join(resolvedPhotoRoot, relPath.replace("/", os.sep)),
                      str(row.get("photoCode") or "")))
    byCode = dict((str(row.get("photoCode") or ""), row) for row in (photoRows or ()))

    def _onBatch(batch):
        for _index, absPath, photoCode, result in batch:
            photoRow = byCode.get(photoCode)
            if photoRow is None:          # 不该发生：任务里的 photoCode 来自同一份 rows
                _LOG.error("落库时找不到 photoCode=%s 的 pb_photo 行，跳过", photoCode)
                continue
            store.submit(photoRow, result)
        store.flush()

    summary = facePool.extractFaces(tasks, workers=workers,
                                    engineKwargs=engineKwargs,
                                    onBatch=_onBatch, onProgress=onProgress)
    store.flush()
    timing = facePool.timingStats(summary["elapsedList"])
    return {"pool": summary, "store": store.summary(), "timing": timing,
            "photos": len(tasks)}


def commitOne(photoRow: dict, result: dict, dbFile: str = None,
              thumbRoot: str = None, photoRoot: str = None,
              replaceFaces: bool = False) -> dict:
    """单张照片的「提取结果 -> 落库」，一步到位（单测与单张调试用）。"""
    store = FaceStore(dbFile=dbFile, thumbRoot=thumbRoot, photoRoot=photoRoot,
                      replaceFaces=replaceFaces)
    kept = store.submit(photoRow, result)
    store.flush()
    out = store.summary()
    out["kept"] = kept
    return out


def countFacesByPhoto(photoCode: str, dbFile: str = None) -> int:
    """某张照片在 pb_face 里的实际条数（验收第 5 条要与 pb_photo.faceCount 对齐）"""
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    return len(sqliteCommon.query_pb_face("pb_face", photoCode=photoCode))


def countPhotoMismatch(dbFile: str = None, limit: int = 0) -> list:
    """找出 faceCount 与实际条数不一致的照片（验收第 5 条的核对工具）。

    返回 [{photoCode, faceCount, realCount}, ...]。
    业务层不写裸 SQL，这里逐条 query_pb_face + 读 pb_photo 的 faceCount 比对；
    limit 用于只抽查前若干张（10 万张逐条查会很久，验收只需抽样）。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    rows = sqliteCommon.query_pb_photo("pb_photo", limitNum=int(limit or 0))
    bad = []
    for row in rows:
        real = countFacesByPhoto(row["photoCode"])
        if int(row.get("faceCount") or 0) != real:
            bad.append({"photoCode": row["photoCode"], "relPath": row.get("relPath"),
                        "faceCount": int(row.get("faceCount") or 0), "realCount": real})
    return bad


if __name__ == "__main__":
    print("faceStore _VERSION:", _VERSION)
    print("年代桶（占位，步骤 6 重算）:")
    for year in (1994, 1995, 1999, 2000, 2013, None, -1, 0, 2200):
        print("   shotYear=%-6r -> %r" % (year, makeShotBucket(year)))
    print("upsert 刷新列:", FACE_UPSERT_COLUMNS)
    print("刻意不刷的列  : ('personCode', 'clusterCode', 'isConfirmed')")
    print("embedding 字节:", faceEngine.EMBEDDING_BYTES)
    print("scanState: 0(%d) -> 1(%d)"
          % (comGD.SCAN_STATE_PENDING, comGD.SCAN_STATE_FACED))
