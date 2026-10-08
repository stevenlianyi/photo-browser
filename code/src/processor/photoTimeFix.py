#! /usr/bin/env python3
#encoding: utf-8

#Filename: photoTimeFix.py
#Description: photo-browser 照片年代修正（DR-42）—— pb_photo.shotYearOverride 的唯一写入口
#
# 要解决的现象
# ------------
#   老相册的翻拍件 / 扫描件，`shotYear` 来自 EXIF -> 文件名 -> mtime 三条兜底链，
#   而这三条给的都是「翻拍那一刻」。于是这张照片里的人脸**全部**落进错误的年代档
#   （年代档 = bucketKeyAdaptive(拍摄年, 该人出生年)）。典型：2019 年翻拍的 1960 年代
#   老照片，RuQin Bai（1938-12-30 生）落在「2016-2025」，而它应当是「1956-1965」。
#
# 为什么修「照片的年份」而不是「人脸的年代档」（DR-42 的核心决策）
# ----------------------------------------------------------
#   年代档键是**派生值**。把年代档键钉在人脸上有三个硬伤：
#     ① 年代档跨度随该人生日而变（童年 3 年 / 成年 10 年）—— 存一个年代档键等于把
#        「这个人的生日」烘焙进那一行，以后一改生日它就成了口径矛盾体
#        （rebucket.assertFacesFresh 会当场判它 stale 并抛错）；
#     ② 同一张合影里的不同人本来就落在**不同年代档**（按各自生日算），
#        逐脸钉年代档键要人工钉 N 次，且互相之间没有任何一致性保证；
#     ③ 照片的年代是**照片的属性**，与「照片里是谁」无关 ——
#        修一次，年代档 / 时间筛选 / 年代跨度 / 地点年份全部一起对。
#
# 四条纪律
# --------
#   ① **只改 pb_photo.shotYearOverride 一列**，且走 `updateTableGeneral`
#      （值原样进参数元组，None 就是 SQL NULL）——「恢复自动」正是要写 NULL，
#      而 upsert 路线会把 None 整列丢掉（见 processor/photoAction.py 文件头
#      那段实测坑：返回成功、库里一字未改）。
#   ② **顺序不可颠倒**：① 写 override → ② `rebucket.rebucketPhoto`
#      → ③ `centroid.recomputePerson`（DR-22）。反了会**静默**失配：
#      质心按旧年代档建、脸按新年代档刷，该人的匹配率归零而库里看不出任何异常。
#   ③ **规则不在本模块重写**：年代档一律调 `rebucket`（它再调 `bucket`），
#      「有效年份」一律调 `rebucket.effectiveShotYear` /
#      `comGD.sqlEffectiveShotYear`。
#   ④ **只改库，不碰 photo 目录**。本模块连 `paths` 都不 import。
#
# 不碰的东西
# ----------
#   · 磁盘上的原图（没有编辑/覆盖入口）；
#   · `pb_photo.shotYear` —— 机器读到的原值**保留**：它既是审计证据，
#     也是「恢复自动」要回落的那个值；
#   · 人脸归属（personCode / isConfirmed / isStranger）—— 这张照片
#     「谁在里面」是用户的判定，修年代无权改。
#
# 与 pb_review_log 的关系
# ----------------------
#   修正本身**不是归属纠错**，但它是「人工纠错 + 可撤销 + 要留痕」的同一类操作，
#   所以登记 opType=BUCKET_FIX 并沿用同一个撤销入口（merger.undo 委托回本模块），
#   而不是另开一张日志表 —— 用户不必学第二套心智模型。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402
from engine.match import rebucket as rebucket                     # noqa: E402
from processor.review import assigner                             # noqa: E402

_VERSION = "20261008"

_LOG = misc.setLogNew("photoTimeFix", "phototimefix.log")

#: 预览里「未归属人脸」那一组的分组键（不是 personCode，是个人造的哨兵；
#: 空串不会与任何真实 personCode 冲突）。
UNASSIGNED: str = ""


class PhotoTimeFixError(Exception):
    """参数非法 / 状态不允许（api 层映射成 400）。"""


class PhotoTimeFixStale(Exception):
    """修正已落库，但后续质心重算被 DR-22 前置检查挡下（**不是**本操作的问题）。"""


# ============================================================
# 零、内部工具
# ============================================================

def _intOrNone(value):
    """各种形态的年份 -> int / None（**不抛**，脏值一律当"没有"）。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _photoRow(photoCode: str) -> dict:
    """取一行 pb_photo（**含软删行** —— 修正一张已软删的照片不该 404）。查不到抛错。"""
    code = str(photoCode or "").strip()
    if not code:
        raise PhotoTimeFixError("photoCode 不能为空")
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=code,
                                      delFlag="*", limitNum=1)
    if not rows:
        raise PhotoTimeFixError("photoCode=%s 在 pb_photo 里不存在" % code)
    return rows[0]


def _facesOf(photoCode: str) -> list:
    """这张照片的**未软删**人脸。

    ⚠️ 口径与 `rebucket.rebucketPhoto` 逐字一致（那边也是 query_pb_face 的
       默认 delFlag='0'）：软删的脸不参与匹配，刷它的年代档没有意义，
       而把它算进「影响面」只会让用户看到一个不会发生的数字。
    """
    return sqliteCommon.query_pb_face("pb_face", photoCode=str(photoCode),
                                      mode="light", orderBy="recID")


def _personOf(personCode: str) -> dict:
    """pb_person 一行（含软删：停用的人脸仍挂在库里）。查不到返回 {}。"""
    code = str(personCode or "")
    if not code:
        return {}
    for row in sqliteCommon.query_pb_person("pb_person", personCode=code,
                                            delFlag="*", mode="light"):
        return row
    return {}


def targetYearOf(photoRow: dict, shotYear):
    """修正后的**有效年份**（预览与落库共用这一个算法）。

    shotYear 给值  -> 就用它（调用方已过 validShotYear 校验）
    shotYear=None  -> 「恢复自动」= 回到机器读到的 pb_photo.shotYear

    ⚠️ 为什么必须在这里算出来而不是让 rebucket 回查：写入**之前**回查拿到的是
       旧值（那时新的 override 还没落库）。预览阶段尤其要小心 ——
       预览按旧值算出来的"新年代档"会与提交后的实际结果不一致。
    """
    if shotYear is None:
        return _intOrNone((photoRow or {}).get("shotYear"))
    return _intOrNone(shotYear)


def validateYear(shotYear):
    """外部传进来的年份 -> 合法 int；非法直接抛（**不猜、不静默回落**）。"""
    year = bucket.validShotYear(shotYear)
    if year is None:
        raise PhotoTimeFixError(
            "年份 %r 不在可信区间 %d..%d 内"
            % (shotYear, basicSettings.SHOT_YEAR_MIN, basicSettings.SHOT_YEAR_MAX))
    return int(year)


def _emptyBucketText(bucketKey: str) -> str:
    return str(bucketKey or "") or "(无拍摄年份)"


# ============================================================
# 一、预览（**只读，一行都不写**）
# ============================================================

def previewFix(photoCode: str, shotYear) -> dict:
    """这张照片按新年代重新划分年代档之后会变成什么样。**纯读**。

    参数
    ----
      shotYear : 目标年份（int / '1960'）；**None = 恢复自动**

    返回的 `persons[]` 就是「这次修正会牵动谁」的完整清单 —— 界面上必须显示它：
    写入的是**照片级**年份，而这本影楼合影里可能有三个不同生日的人，
    他们各自的年代档会朝不同方向变。
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    oldOverride = _intOrNone(row.get("shotYearOverride"))
    exifYear = _intOrNone(row.get("shotYear"))
    currentYear = (oldOverride if oldOverride is not None else exifYear)

    if shotYear is None:
        writeValue, targetYear, restoreAuto = None, exifYear, True
    else:
        writeValue = validateYear(shotYear)
        targetYear, restoreAuto = writeValue, False

    faces = _facesOf(code)
    personCache = {}
    groups = []
    seen = {}
    for face in faces:
        pcode = str(face.get("personCode") or "")
        key = pcode or UNASSIGNED
        if key not in seen:
            if pcode and pcode not in personCache:
                personCache[pcode] = _personOf(pcode)
            person = personCache.get(pcode) or None
            seen[key] = {"personCode": pcode,
                         "displayName": str((person or {}).get("displayName") or "")
                                        or ("(未归属)" if not pcode else pcode),
                         "birthday": str((person or {}).get("birthday") or ""),
                         "faces": 0, "oldBuckets": [], "newBuckets": []}
            groups.append(seen[key])
        item = seen[key]
        item["faces"] += 1
        oldBucket = str(face.get("shotBucket") or "")
        newBucket = rebucket.expectedBucketOf(face, personCache.get(pcode) or None,
                                              shotYear=targetYear)
        if oldBucket and oldBucket not in item["oldBuckets"]:
            item["oldBuckets"].append(oldBucket)
        if newBucket and newBucket not in item["newBuckets"]:
            item["newBuckets"].append(newBucket)

    for item in groups:
        item["oldBuckets"] = sorted(item["oldBuckets"])
        item["newBuckets"] = sorted(item["newBuckets"])
        item["bucketChanged"] = (item["oldBuckets"] != item["newBuckets"])
        item["faceCount"] = item["faces"]

    changed = (writeValue != oldOverride) or (currentYear != targetYear)
    return {
        "photoCode": code,
        "relPath": str(row.get("relPath") or ""),
        "exifYear": exifYear,
        "currentOverride": oldOverride,
        "currentYear": currentYear,
        "targetYear": targetYear,
        "targetOverride": writeValue,
        "restoreAuto": bool(restoreAuto),
        "changed": bool(changed),
        "faces": len(faces),
        "affectedFaces": sum(1 for item in groups if item["bucketChanged"]),
        "persons": groups,
        "willChangeBucket": any(item["bucketChanged"] for item in groups),
        "note": ("修正的是**这张照片的拍摄年代**：年代档由 "
                 "f(有效拍摄年, 各人出生年) 现算，所以同一张合影里"
                 "每个人的年代档会各自变化（见 persons[]）。"),
    }


# ============================================================
# 二、落库 + 三步联动
# ============================================================

def _writeOverride(photoCode: str, value) -> int:
    """写 pb_photo.shotYearOverride（纪律 ①）。

    ⚠️ 必须走 updateTableGeneral：值**原样**进参数元组，None 就是 SQL NULL
       ——「恢复自动」写的就是 NULL。走 upsert 的话整批为 None 的列会被
       normalizeDataSet 丢掉，于是「恢复自动」变成一次**静默的空操作**
       （返回成功、库里 override 还在）。
    """
    rtn = sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (str(photoCode),),
        {"shotYearOverride": value, "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise PhotoTimeFixError("pb_photo.shotYearOverride 写入失败: %s"
                                % sqliteCommon.dbHandle().lastErrMsg)
    if rtn is not None and rtn < 0:
        raise PhotoTimeFixError("pb_photo.shotYearOverride 写入返回 %r" % (rtn,))
    return rtn


def _recomputePersons(personCodes) -> tuple:
    """③ 重算这批人的**全部**年代档（含 ALL 兜底年代档）。返回 (统计, 警告)。

    ⚠️ 为什么是 recomputePerson 而不是只重算那一个年代档：样本搬走/搬进会连带改变
       「这个人的全部确认样本」这个集合，ALL 兜底年代档与其它年代档的样本数都可能变。
    ⚠️ 为什么会失败：`centroid.recomputePerson` 执行前会跑 `assertFacesFresh`
       （DR-22 前置检查）—— 若这个人**别的照片**还残留着旧口径的年代档键，
       它会抛 BucketStaleError。那不是本次修正造成的（本次该照片的年代档已经刷对了），
       所以这里**不把它变成失败**，而是作为 warning 返回给调用方与界面
       ——「静默吞掉」和「整个操作失败」都不对。
    """
    stats, warnings = [], []
    seen = set()
    for personCode in personCodes or ():
        code = str(personCode or "")
        if not code or code in seen:
            continue
        seen.add(code)
        try:
            stats.append(centroid.recomputePerson(code))
        except Exception as e:                    # BucketStaleError 及同类
            warnings.append({
                "personCode": code,
                "error": "%s: %s" % (type(e).__name__, e),
                "advice": "这个人的**其它**照片还残留旧口径的年代档键，"
                          "请跑一次 python code\\src\\tools\\rebucket_cli.py "
                          "--person %s --recompute（或全库 --all --recompute）" % code,
            })
    return stats, warnings


def _encodeYear(value) -> str:
    """detail 里的年份编码：None -> 空串（`k=v` 是纯文本，必须能与'没值'区分）。"""
    return "" if value is None else str(int(value))


def _decodeYear(text):
    one = str(text or "").strip()
    if one in ("", "None", "null"):
        return None
    try:
        return int(one)
    except ValueError:
        return None


def _detailOf(text) -> dict:
    """`k=v;k=v` -> dict（与 merger._detail 同一套解析）。"""
    out = {}
    for item in str(text or "").split(";"):
        if "=" not in item:
            continue
        key, _sep, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def applyFix(photoCode: str, shotYear=None) -> dict:
    """把这张照片的年代改成 shotYear（None = 恢复自动），并联动重刷年代档与质心。

    顺序严格按纪律 ②：① 写 override → ② rebucketPhoto → ③ recomputePerson。
    返回见 previewFix 的字段 + `logCode` / `centroids` / `warnings`。
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    relPath = str(row.get("relPath") or "")
    oldOverride = _intOrNone(row.get("shotYearOverride"))
    exifYear = _intOrNone(row.get("shotYear"))

    if shotYear is None:
        writeValue, targetYear = None, exifYear
    else:
        writeValue = validateYear(shotYear)
        targetYear = writeValue

    # 幂等：值没变就什么都不做（**不写库、不刷新年代档、不落日志**）——
    # 重复点一次「修正」不该在操作历史里留下第二条一模一样的记录。
    if writeValue == oldOverride:
        return {"photoCode": code, "relPath": relPath, "changed": False,
                "exifYear": exifYear, "oldOverride": oldOverride,
                "newOverride": oldOverride, "effectiveYear": targetYear,
                "facesRebucketed": 0, "bucketsChanged": 0,
                "persons": [], "centroids": [], "warnings": [], "logCode": "",
                "note": "目标年份与当前修正值相同，没有改动（幂等）"}

    faces = _facesOf(code)
    personCodes = sorted({str(f.get("personCode") or "") for f in faces
                          if str(f.get("personCode") or "")})

    # ---- ① 写 override ----
    _writeOverride(code, writeValue)

    # ---- ② 重刷这张照片全部人脸的 shotBucket ----
    # ⚠️ 必须**在写库之后**：rebucketPhoto 经过 shotYearOf -> effectiveShotYear
    #    回查 pb_photo，写入前拿到的是旧值。
    rebucketed = rebucket.rebucketPhoto(code)

    # ---- ③ 重算涉及人物质心 ----
    centroids, warnings = _recomputePersons(personCodes)

    # ---- ④ 留痕（主日志 + 每个受影响人一条成员日志）----
    logCode = assigner.newLogCode()
    detail = ";".join(["op=%s" % logCode, "photo=%s" % code,
                       "old=%s" % _encodeYear(oldOverride),
                       "new=%s" % _encodeYear(writeValue),
                       "persons=%s" % "|".join(personCodes),
                       "relPath=%s" % relPath[:80]])
    assigner.logReview(assigner.OP_BUCKET_FIX, faceCode="", photoCode=code,
                       fromPersonCode="", toPersonCode="",
                       faceCount=len(faces), detail=detail,
                       isRevertible=1, logCode=logCode)
    for personCode in personCodes:
        assigner.logReview(
            assigner.OP_BUCKET_FIX, faceCode="", photoCode=code,
            fromPersonCode="", toPersonCode=personCode, faceCount=1,
            detail="op=%s;old=%s;new=%s" % (logCode, _encodeYear(oldOverride),
                                            _encodeYear(writeValue)),
            isRevertible=1, logCode="%s.%s" % (logCode, personCode))

    _LOG.info("年代修正 %s：%s -> %s（有效年 %s），刷新年代档 %d 张/变更 %d 张，"
              "重算 %d 人，涉及 %d 人，日志 %s",
              code, oldOverride, writeValue, targetYear,
              int(rebucketed.get("faces") or 0),
              int(rebucketed.get("changed") or 0), len(centroids),
              len(personCodes), logCode)
    return {"photoCode": code, "relPath": relPath, "changed": True,
            "exifYear": exifYear, "oldOverride": oldOverride,
            "newOverride": writeValue, "effectiveYear": targetYear,
            "restoreAuto": bool(shotYear is None),
            "facesRebucketed": int(rebucketed.get("faces") or 0),
            "bucketsChanged": int(rebucketed.get("changed") or 0),
            "persons": personCodes,
            "centroids": [c for stat in centroids
                          for c in (stat.get("buckets") or [])],
            "warnings": warnings, "logCode": logCode,
            "undoEndpoint": "/api/review/undo"}


# ============================================================
# 三、撤销（由 merger.undo 委托，**不自己开 API**）
# ============================================================

def _personsOf(logCode: str) -> list:
    """一次年代修正牵动了哪些人。

    先读主日志 detail 里的 persons=，读不到再退回**成员日志**
    （logCode = `<主码>.<personCode>`，见 applyFix 的 ④）。
    为什么要有兜底：detail 是 VARCHAR(400)，合影里人多时 persons= 会被截断 ——
    截断本身不报错，撤销时就会漏掉几个人（质心停在旧年代档上，静默失准）。
    """
    rows = sqliteCommon.query_pb_review_log("pb_review_log", logCode=logCode)
    head = _detailOf(rows[0].get("detail")) if rows else {}
    out = [p for p in str(head.get("persons") or "").split("|") if p]
    if out:
        return out
    prefix = str(logCode) + "."
    for one in sqliteCommon.query_pb_review_log(
            "pb_review_log", opType=assigner.OP_BUCKET_FIX):
        code = str(one.get("logCode") or "")
        if not code.startswith(prefix):
            continue
        pcode = str(one.get("toPersonCode") or "")
        if pcode:
            out.append(pcode)
    return sorted(set(out))


def revertFromLog(logRow: dict) -> dict:
    """撤销一次年代修正：把 override 写回原值 + 重刷年代档 + 重算质心。

    返回结构与 `merger.undo` **同构**（facesRestored / linksRestored / centroids），
    这样 `/api/review/undo` 与前端不需要为这条路径写第二套分支。

    四道闸门与 merger.undo 一致：日志存在 / opType=BUCKET_FIX / isRevertible=1 /
    还没被撤销过（`revertedByLogCode` 为空）。
    """
    row = logRow or {}
    logCode = str(row.get("logCode") or "")
    if str(row.get("opType") or "") != assigner.OP_BUCKET_FIX:
        raise PhotoTimeFixError("日志 %s 不是年代修正（opType=%s），不能走这条撤销路径"
                                % (logCode, row.get("opType")))
    if int(row.get("isRevertible") or 0) != 1:
        raise PhotoTimeFixError("日志 %s 的 isRevertible=0，不可撤销" % logCode)
    if str(row.get("revertedByLogCode") or ""):
        raise PhotoTimeFixError("日志 %s 已被 %s 撤销过，不能重复撤销"
                                % (logCode, row.get("revertedByLogCode")))

    head = _detailOf(row.get("detail"))
    photoCode = str(head.get("photo") or row.get("photoCode") or "")
    if not photoCode:
        raise PhotoTimeFixError("日志 %s 里没有 photoCode，无法定位照片" % logCode)
    restoreValue = _decodeYear(head.get("old"))      # 撤销 = 回到修正前的值
    personCodes = _personsOf(logCode)

    photo = _photoRow(photoCode)
    _writeOverride(photoCode, restoreValue)
    rebucketed = rebucket.rebucketPhoto(photoCode)
    centroids, warnings = _recomputePersons(personCodes)

    db = sqliteCommon.dbHandle()
    undoCode = assigner.newLogCode()
    rtn = sqliteCommon.updateTableGeneral(
        "pb_review_log", "logCode = %s", (logCode,),
        {"revertedByLogCode": undoCode, "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise PhotoTimeFixError("pb_review_log 回填失败: %s"
                                % db.lastErrMsg)
    assigner.logReview(assigner.OP_UNDO, faceCode="", photoCode=photoCode,
                       fromPersonCode="", toPersonCode="",
                       faceCount=int(rebucketed.get("faces") or 0),
                       detail="undo=%s;op=%s;new=%s"
                              % (logCode, assigner.OP_BUCKET_FIX,
                                 _encodeYear(restoreValue)),
                       isRevertible=0, logCode=undoCode)
    _LOG.info("撤销年代修正 %s（照片 %s）：override 还原为 %s，刷新年代档 %d 张，重算 %d 人",
              logCode, photoCode, restoreValue,
              int(rebucketed.get("changed") or 0), len(centroids))
    return {"logCode": logCode, "undoLogCode": undoCode,
            "opType": assigner.OP_BUCKET_FIX,
            "photoCode": photoCode,
            "relPath": str(photo.get("relPath") or ""),
            "restoredOverride": restoreValue,
            "facesRestored": 0, "linksRestored": 0, "linksDropped": 0,
            "facesRebucketed": int(rebucketed.get("faces") or 0),
            "bucketsChanged": int(rebucketed.get("changed") or 0),
            "persons": personCodes,
            "missingFaces": [],
            "centroids": [c for stat in centroids
                          for c in (stat.get("buckets") or [])],
            "warnings": warnings}


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("photoTimeFix.py _VERSION:", _VERSION)
    print("改的列      : pb_photo.shotYearOverride（值原样进参数元组，None=NULL）")
    print("有效年份    : rebucket.effectiveShotYear（override 优先，全项目唯一口径）")
    print("联动顺序    : ①写 override -> ②rebucket.rebucketPhoto -> "
          "③centroid.recomputePerson（DR-22）")
    print("日志        : pb_review_log opType=%s（可撤销，走 /api/review/undo）"
          % assigner.OP_BUCKET_FIX)
    print("库          :", sqliteCommon.dbFilePath())
