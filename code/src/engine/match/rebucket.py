#! /usr/bin/env python3
#encoding: utf-8

#Filename: rebucket.py
#Description: photo-browser 人脸年代桶重刷（修正步骤 R2 / DR-20）——
#             pb_face.shotBucket 的**唯一重算入口**，只动这一列
#
# 这个模块为什么存在（DR-20，P0 实现缺口）
# ------------------------------------------------
#   faceStore.makeShotBucket() 写的是**等宽 5 年占位桶**（提取那一刻还不知道
#   这张脸是谁，也就没有年龄，定不了 3 年还是 10 年），注释里承诺
#   「步骤 6 会用自适应规则重算覆盖」—— 而**步骤 6 从未实现这个覆盖**。
#   后果三条，全都不报错：
#     ① 生产库里所有脸都是等宽 5 年桶 -> S0 的「自适应分桶让 FR 32.75%->19%」
#        等于一直在跑对照组；
#     ② bucket.py 那套自适应分桶是死代码（只在验证脚本与测试里被调用）；
#     ③ DR-18 的「改birthday 要重算质心」是无效的（桶键根本不依赖 birthday）。
#   本模块就是补上那个「覆盖」，并且**接进每一条会改变 personCode 的写入路径**
#   （见文件头「谁负责重刷」），而不是留一个「记得跑脚本」的口头约定。
#
# 谁负责重刷（**根治点，写入路径必须接上**，否则重刷永远会漏）
# ------------------------------------------------------------
#   faceStore（步骤 5 提取）      写等宽占位桶 —— 此时不知道这张脸是谁，
#                                 重刷不了也不该重刷
#   assigner._setBelong()归属那一刻生日才确定 -> **先刷桶，再重算质心**
#   assigner.fix('unknown'/'stranger')     退回未归属 -> 刷回等宽降级桶
#   merger.merge()                迁到目标人 -> 按**目标人**的生日重刷
#   merger.undo()                 归属回退 -> 同上
#   merger.split()                经 setBelong/unassign，自动覆盖
#   联系人导入后（applyPlan / import_contacts）生日到位 -> 重刷 + 重算
#   tools/rebucket_cli.py --all            存量一次性刷干净
#
# 硬纪律（四条，缺一条就出静默错误）
# --------------------------------------
#   ① **只改 shotBucket 一列**。绝不碰 personCode / isConfirmed / isStranger /
#      clusterCode / embedding / bbox / 任何 score。这张脸属不属于某个人是
#      用户的判定，刷桶无权改。
#   ② **写 shotBucket 必须走 upsert + forceColumns**，绝不用 update_pb_face。
#      两个原因（与 assigner._patchFace 同一个根因，这里再抄一遍免得被"简化"掉）：
#        · 生成层的 normalizeDataSet 把None 整条丢掉 ->
#          `update_pb_face(..., {"shotBucket": None})` 是一次**静默的空操作**：
#          0 行受影响、不报错、库里原封不动。而「这张照片没有拍摄年份」
#          （截图/EXIF 缺失，shotBucket 本来就该是 NULL）正是最需要写 NULL 的场景；
#        · 一批脸里若**全部**算出空串，不加 forceColumns 的话该列压根不进
#          INSERT 列清单 -> DO UPDATE 不会覆盖它 -> 旧桶键永久残留。
#      走 upsert 还必须带齐 pb_face 的 NOT NULL 身份列 photoCode（见
#      FACE_IDENTITY_COLUMNS），否则撞 `NOT NULL constraint failed`。
#   ③ **规则一律调 bucket.bucketKeyAdaptive()**，本模块不重写分桶公式。
#      18/19 岁跨分支那种刻意的口径瑕疵（bucket.py 文件头有详述）只存在于
#      bucket.py 一处；在这里抄一遍就等于埋一个「两套规则迟早分叉」的雷。
#   ④ **只改库，不碰 photo 目录**。本模块连paths 都不 import。
#
# 顺序纪律（DR-22，「先刷桶、再重算质心」是硬顺序）
# --------------------------------------------------
#   顺序反了会**静默**失配：先按旧桶键建好质心、再改脸表的桶键 ->
#   质心表里留着旧桶键的行（僵尸，新桶取不到），新桶键又没有质心 ->
#   该人匹配率归零，而**库里看不出任何异常**。
#   所以 centroid.recompute/recomputePerson 执行前会调assertFacesFresh()
#   做前置检查（见 centroid.py 的 `_assertBucketOrder`），不一致就**抛错**
#   并提示先跑 rebucket，而不是默默按旧口径把错误的质心算出来。
#
# 分层（engine/match/__init__.py 的反向依赖禁令）
# -----------------------------------------------
#   bucket.py   纯函数，不 import 任何 database.*
#   rebucket.py 本模块：import bucket + sqliteCommon，**不 import centroid**
#               （刷桶与重算质心是两个动作，顺序由调用方保证；混在一个模块里
#                 就有了「顺手在刷桶时重算」的隐患）
#   centroid.py import rebucket 做前置检查（单向，无环）

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/match
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402

_VERSION = "20261008"

_LOG = misc.setLogNew("rebucket", "rebucket.log")

#: **无生日时的降级等宽桶宽（年）**。
#: 取 5 而不是转发 faceStore.SHOT_BUCKET_WIDTH，是为了不让 engine/match 反向
#: 依赖 engine/face（faceStore import 了一堆 ONNX/缩略图的东西，拖进来会让
#: 纯函数单测跑不起来）。两个常量必须相等 —— assertBucketWidthFallback()
#: 在模块加载时就会核对，不一致直接抛错，不会等到库里出了两种桶宽才发现。
SHOT_BUCKET_WIDTH_FALLBACK: int = basicSettings.BUCKET_EQUAL_WIDTH

#: pb_face 走 upsert 时**必须带齐**的 NOT NULL 身份列（纪律 ②）。
#: faceCode 是冲突键（不进 INSERT 值列表的必填位），photoCode 是 NOT NULL 外键。
FACE_IDENTITY_COLUMNS: tuple = ("faceCode", "photoCode")

#: 自适应桶的两种桶宽（从 basicSettings 转发，全项目只有这一个口径）
CHILD_WIDTH: int = bucket.CHILD_WIDTH
ADULT_WIDTH: int = bucket.ADULT_WIDTH

#: 分页行数。全库刷桶时10 万张脸要分页读+分页写（与步骤 3/5/6 同一理由：
#: 一次性把 10 万行的 shotBucket 全读回来是纯浪费，embedding 更是 200MB）
PAGE_ROWS: int = basicSettings.CENTROID_PAGE_ROWS

#: 桶宽 -> 语义（报告与巡检用）。**宽5 不等于"错"**：无生日 + 未归属的脸
#: 本来就该是等宽降级桶（bucket.py 的降级路径），它和截图（无 shotYear，
#: shotBucket=NULL）是两回事。
WIDTH_MEANING: dict = {
    CHILD_WIDTH: "自适应·童年段（0-18 岁）",
    ADULT_WIDTH: "自适应·成年段（18+）",
    SHOT_BUCKET_WIDTH_FALLBACK: "等宽降级（无生日或未归属）",
}

#: 巡检报告里每类问题的取样条数（只给几条就够判断性质，全量会淹没输出）
_SAMPLE_NUM: int = 5


class BucketStaleError(Exception):
    """脸表的 shotBucket 与「当前主人 + 生日」算出来的口径不一致（DR-22）。

    抛它的**唯一目的**是让人去跑 rebucket，而不是让它被try/except 吞掉。
    """


# ============================================================
# 零、口径自检（模块加载时跑一次）
# ============================================================

def assertBucketWidthFallback() -> None:
    """核对降级桶宽与 faceStore 的占位桶宽一致。**不一致直接抛**。

    为什么值得在加载时抛：这两个常量分居两个模块、又必须相等
    （「未归属脸的桶」与「刚提取出来的脸的桶」是同一个东西 ——
      否则 DR-21 放宽候选桶之后，一张刚提取、还没归属的脸会因为
      桶键族不同而取不到任何自适应质心，而且**不报错**）。
    真出分歧时要改的是**两边一起改**，不是让某一边悄悄让步。
    """
    from engine.face import faceStore as faceStore        # 局部 import：见下方说明
    if int(faceStore.SHOT_BUCKET_WIDTH) != int(SHOT_BUCKET_WIDTH_FALLBACK):
        raise BucketStaleError(
            "降级桶宽分歧：rebucket.SHOT_BUCKET_WIDTH_FALLBACK=%d，"
            "faceStore.SHOT_BUCKET_WIDTH=%d。两者必须相等（未归属脸的桶要与"
            "刚提取时的占位桶同族），请一起改。"
            % (int(SHOT_BUCKET_WIDTH_FALLBACK), int(faceStore.SHOT_BUCKET_WIDTH)))


# ============================================================
# 一、纯计算：桶键（纪律 ③：规则一律在 bucket.py）
# ============================================================

def shotBucketFor(shotYear, birthday=None) -> str:
    """(拍摄年, pb_person.birthday 原文) -> 自适应桶键。

    参数
    ----
      shotYear  : pb_photo.shotYear（int / '2013' / None 都可以）
      birthday  : pb_person.birthday **原文**（'1985-03-07' / '1985' / ''）；
                  None / 空/ 解析不出合法出生年 -> 降级等宽 5 年

    返回
    ----
      '1995-1997' 这样的桶键；shotYear 无效时返回空串（落库为 NULL，
      **不参与跨桶比对**，见 bucket.py 文件头纪律 1）。

    ⚠️ **薄封装**：最终一律调 bucket.bucketKeyAdaptive()。
       本函数存在的理由只有两个：① 把「生日原文 -> 出生年」这一步收口
       （birthYearOf 的容错规则不该在每个调用点各写一遍）；
       ② 让全项目只有一个「脸属于某个人时该算哪个桶」的入口。
    """
    return bucket.bucketKeyOf(shotYear, birthday)


def expectedBucketOf(faceRow: dict, personRow: dict = None, shotYear=None) -> str:
    """一张脸**应该**是哪个桶（纯计算，不写库）。

    参数
    ----
      faceRow   : pb_face 行（要faceCode / photoCode / personCode / shotBucket）
      personRow : 该脸**当前**主人所在的 pb_person 行；None = 未归属
      shotYear  : pb_photo.shotYear。**不给就回查 pb_photo**（一次等值查询，
                  有索引；调用方若已知就直接传，省一次往返）

    三种情形
    --------
      已归属 + 主人有合法生日 -> 自适应桶（宽 3 或宽 10）
      已归属 + 主人无生日     -> 等宽降级桶（宽 5）
      未归属                   -> 等宽降级桶（宽 5）
    最后两种**同键**：一张没归属的脸和一个没生日的人，本来就分不出年龄，
    按同一套等宽桶处理才能让「刚提取的脸」与「退回未归属的脸」取到同一批
    候选质心（否则就是 DR-21 说的「一把质心都取不到」）。
    """
    row = faceRow or {}
    if shotYear is None and str(row.get("photoCode") or ""):
        shotYear = shotYearOf(str(row.get("photoCode") or ""))
    birthday = (personRow or {}).get("birthday") if personRow else None
    return shotBucketFor(shotYear, birthday)


def effectiveShotYear(photoRow) -> object:
    """照片的**有效拍摄年**：人工修正优先于机器读到的年份（DR-42）。

    `pb_photo.shotYear` 来自 EXIF -> 文件名 -> mtime 三条兜底链，而老相册的
    翻拍件/扫描件给的都是"翻拍那一刻"；用户手工填的 `shotYearOverride`
    才是这张照片真正的年代。

    ⚠️ 只做「有值就用、没值才回落到 shotYear」这一条判断，**不做区间校验**：
       override 写库时已经过 bucket.validShotYear（见 processor/photoTimeFix），
       这里再卡一次区间只会让"库里有一条越界值"这件事从分桶结果里消失。
       越界值继续走 bucketKeyAdaptive 的 validShotYear -> 返回空桶键（=不进跨桶比对），
       是**可见**的，比静默按某个合法年份分桶安全。
    ⚠️ 空串也当"未修正"：SQLite 里该列应为 NULL，但手工改过库的行可能是 ''。
    """
    row = photoRow or {}
    override = row.get("shotYearOverride")
    if override is None or str(override).strip() == "":
        return row.get("shotYear")
    return override


def shotYearOf(photoCode: str, cache: dict = None) -> object:
    """pb_photo.photoCode -> **有效拍摄年**（取不到返回 None）。

    cache 可选：传一个 dict 就在里面复用结果。全库刷桶时同一个 photoCode
    会被反复用到（一张照片里多张脸、以及 scanYear 为空的照片），
    缓存能把 N 次查询压成 1 次。

    ⚠️ 返回的是 effectiveShotYear（人工修正优先），不是裸 shotYear —— DR-42 之后
       「照片是哪一年的」全项目只有这一个答案。这是本模块唯一一处读 pb_photo
       的地方，改在这里就覆盖了 rebucketFace / rebucketPhoto / rebucketPerson /
       rebucketAll 全部刷桶路径（它们都经 expectedBucketOf -> 本函数）。
    """
    code = str(photoCode or "")
    if not code:
        return None
    if cache is not None and code in cache:
        return cache[code]
    year = None
    for row in sqliteCommon.query_pb_photo("pb_photo", photoCode=code, mode="light"):
        year = effectiveShotYear(row)
        break
    if cache is not None:
        cache[code] = year
    return year


# ============================================================
# 二、写库：只改 shotBucket 一列（纪律 ①②）
# ============================================================

def _writeBucketRows(rows: list) -> int:
    """把 (faceCode, photoCode, shotBucket) 行批量 upsert 进 pb_face。返回写出行数。

    纪律 ② 的落点。三个要点一个都不能少：
      * conflictColumns=("faceCode",)  —— 幂等键；
      * updateColumns 只含 shotBucket 与 modifyYMDHMS —— **不碰任何其他列**；
      * forceColumns 含 shotBucket    —— 整批皆空时该列仍进 INSERT 列清单，
        DO UPDATE 才会真的把它写成 NULL（否则旧桶键永久残留且不报错）。
    """
    if not rows:
        return 0
    payload = [{"faceCode": str(one["faceCode"]),
                "photoCode": str(one.get("photoCode") or ""),
                "shotBucket": (one.get("newBucket") or None),
                "modifyYMDHMS": misc.getTime()}
               for one in rows]
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_face", payload,
        conflictColumns=("faceCode",),
        updateColumns=("shotBucket", "modifyYMDHMS"),
        fillStandard=True,
        forceColumns=("shotBucket",))
    if rtn == -2:                            # sqliteHandle.RET_ERROR
        raise BucketStaleError("pb_face.shotBucket 写入失败: %s"
                               % sqliteCommon.dbHandle().lastErrMsg)
    return rtn


def rebucketFace(faceRow: dict, personRow: dict = None, dryRun: bool = False,
                 shotYear=None) -> dict:
    """单张脸：按 (pb_photo.shotYear, 该脸当前主人的 birthday) 重算并写回
    pb_face.shotBucket。

    参数
    ----
      faceRow   : pb_face 行（至少要 faceCode / photoCode / shotBucket）
      personRow : 该脸**当前**主人所在的 pb_person 行。
                  ⚠️⚠️ **None 的语义是「这张脸未归属」-> 刷成等宽降级桶**，
                  它**不会**再回头读 faceRow 里的 personCode 去猜主人。
                  为什么必须这样钉死：调用方几乎总是在**改完 personCode 之前**
                  把旧行读出来传进来（那是它手上唯一一份 face 行）；
                  若本函数拿这份**旧行**的 personCode 去回查主人，
                  `fix('unknown')` / `merger.undo()` 这类「把脸还给/退回
                  别人或退回未归属」的路径就会按**上一个主人**的生日算桶，
                  桶键永远刷不回去 —— 而返回值里的 changed=True 会让人以为刷成功了。
                  要按某个人刷，就**显式**把那个人的 pb_person 行传进来。
      dryRun    : True = 只算不写（返回 changed=True 但库里不动）
      shotYear  : 已知拍摄年时直接传，省一次 pb_photo 回查

    返回 {faceCode, photoCode, oldBucket, newBucket, changed, written}

    ⚠️ **只改 shotBucket 一列**（纪律 ①）。绝不碰 personCode / isConfirmed /
       isStranger / clusterCode / embedding —— 这张脸属不属于某个人是用户的
       判定，刷桶无权改。返回值里的 changed 指**桶键**变了，不是脸变了。
    """
    row = faceRow or {}
    faceCode = str(row.get("faceCode") or "")
    photoCode = str(row.get("photoCode") or "")
    old = str(row.get("shotBucket") or "")
    if not faceCode:
        raise BucketStaleError("rebucketFace 需要 faceCode")
    if not photoCode:
        # photoCode 是 pb_face 的 NOT NULL 外键，upsert 缺它会撞约束。
        # 这种情况只可能是脸表本身已经坏了 —— 明确报错，不猜。
        raise BucketStaleError("脸 %s 的 photoCode 为空，无法 upsert" % faceCode)
    new = expectedBucketOf(row, personRow, shotYear=shotYear)
    out = {"faceCode": faceCode, "photoCode": photoCode,
           "oldBucket": old, "newBucket": new,
           "changed": bool(new != old), "written": 0}
    if out["changed"] and not dryRun:
        out["written"] = _writeBucketRows([out])
    return out


def rebucketPerson(personCode: str, dryRun: bool = False,
                   progress=None) -> dict:
    """把某个人**全部**脸的 shotBucket 按他的 birthday 重刷。

    分页读+ 分页写（别一次取全）：一个人的脸在真实库里可达数千张，
    而 pb_face 行里带着 2048 字节的 embedding。

    返回 {personCode, faces, changed, oldBuckets, newBuckets, samples}
      oldBuckets / newBuckets : 该人脸表的桶键集合（改前 / 改后），升序
      samples                 : 最多_SAMPLE_NUM 条「改前 -> 改后」的明细
    """
    code = str(personCode or "")
    person = _personOf(code) if code else None
    out = {"personCode": code, "faces": 0, "changed": 0, "written": 0,
           "oldBuckets": [], "newBuckets": [], "samples": [], "dryRun": bool(dryRun)}
    if not code or person is None:
        out["error"] = "personCode=%r 在 pb_person 里不存在" % personCode
        return out
    yearCache = {}
    oldSet, newSet = set(), set()
    offset = 0
    while True:
        rows = sqliteCommon.query_pb_face(
            "pb_face", personCode=code, mode="light",
            orderBy="recID", limitNum=PAGE_ROWS, offsetNum=offset)
        if not rows:
            break
        offset += len(rows)
        for one in rows:
            out["faces"] += 1
            # 陌生人（isStranger=1）永远不会再参与匹配，且它的 personCode 已被
            # 置空 —— 刷回等宽降级桶。留着「按某个人生日算出的自适应桶」只会
            # 让库里留下一个已经查不到主人语义的键。
            owner = None if int(one.get("isStranger") or 0) else person
            info = rebucketFace(one, owner, dryRun=dryRun)
            if info["oldBucket"]:
                oldSet.add(info["oldBucket"])
            if info["newBucket"]:
                newSet.add(info["newBucket"])
            if info["changed"]:
                out["changed"] += 1
                out["written"] += int(info["written"] or 0)
                if len(out["samples"]) < _SAMPLE_NUM:
                    out["samples"].append(
                        {"faceCode": info["faceCode"],
                         "shotYear": shotYearOf(info["photoCode"], yearCache),
                         "birthday": str(person.get("birthday") or ""),
                         "old": info["oldBucket"] or "(NULL)",
                         "new": info["newBucket"] or "(NULL)"})
        if progress:
            progress(out["faces"], None)
        if len(rows) < PAGE_ROWS:
            break
    out["oldBuckets"] = sorted(oldSet)
    out["newBuckets"] = sorted(newSet)
    return out


def rebucketPhoto(photoCode: str, dryRun: bool = False) -> dict:
    """把一张照片**全部**脸的 shotBucket 重刷。

    与 rebucketPerson 的区别只在于「主人是谁」是逐张脸查的 ——
    一张合影里可能有好几个不同的人。
    """
    code = str(photoCode or "")
    out = {"photoCode": code, "faces": 0, "changed": 0, "written": 0,
           "samples": [], "dryRun": bool(dryRun)}
    if not code:
        out["error"] = "photoCode 不能为空"
        return out
    yearCache = {}
    year = shotYearOf(code, yearCache)
    personCache = {}
    rows = sqliteCommon.query_pb_face("pb_face", photoCode=code, mode="light",
                                      orderBy="recID")
    for one in rows:
        pcode = str(one.get("personCode") or "")
        if pcode and pcode not in personCache:
            personCache[pcode] = _personOf(pcode)
        info = rebucketFace(one, personCache.get(pcode), dryRun=dryRun,
                            shotYear=year)
        out["faces"] += 1
        if info["changed"]:
            out["changed"] += 1
            out["written"] += int(info["written"] or 0)
            if len(out["samples"]) < _SAMPLE_NUM:
                out["samples"].append(
                    {"faceCode": info["faceCode"], "shotYear": year,
                     "personCode": pcode or "(未归属)",
                     "old": info["oldBucket"] or "(NULL)",
                     "new": info["newBucket"] or "(NULL)"})
    return out


def rebucketAll(batchRows: int = None, progress=None, onlyAdaptive: bool = False,
                dryRun: bool = False) -> dict:
    """全库刷桶。

    参数
    ----
      batchRows    : 每批多少张脸落库（缺省 basicSettings.COMMIT_ROWS_PER_TXN）
      progress     : 可选回调 progress(已处理脸数, 总脸数估计)
      onlyAdaptive : True = **只刷「已归属 + 目标人有合法生日」的那些**，
                    其余（未归属 / 无生日）一律跳过。
                    为什么默认口径要分两种：未归属的脸**没有生日可用**，
                    它的正确桶就是等宽降级桶，而库里现在已经是这个值了 ——
                    全量重算对它是纯浪费（10 万张脸里绝大多数是未归属的）。
      dryRun       : 只算不写

    未归属的脸怎么处理
    ------------------
      它们的**期望值就是当前的等宽桶**（expectedBucketOf 对 personRow=None
      返回 bucketKeyEqual），所以全量跑下来 changed=0。这是正确结果，
      不是「没刷到」—— 别为了让 changed 看起来不为 0 而给它们编一个生日。

    返回 {faces, changed, written, adaptive, degraded, noYear, noShotYear,
          skippedByOnlyAdaptive, samples, elapsed}
    """
    import time
    step = int(batchRows or basicSettings.COMMIT_ROWS_PER_TXN)
    yearCache, personCache = {}, {}
    out = {"faces": 0, "changed": 0, "written": 0, "adaptive": 0,
           "degraded": 0, "noYear": 0, "noShotYear": 0,
           "skippedByOnlyAdaptive": 0, "samples": [], "dryRun": bool(dryRun)}
    start = time.perf_counter()
    offset, pending, total = 0, [], None
    while True:
        rows = sqliteCommon.query_pb_face("pb_face", mode="light",
                                          orderBy="recID",
                                          limitNum=PAGE_ROWS, offsetNum=offset)
        if not rows:
            break
        offset += len(rows)
        total = offset
        for one in rows:
            pcode = str(one.get("personCode") or "")
            if pcode and pcode not in personCache:
                personCache[pcode] = _personOf(pcode)
            person = personCache.get(pcode)
            birthday = str((person or {}).get("birthday") or "")
            hasYear = bool(bucket.birthYearOf(birthday))
            if onlyAdaptive and not (pcode and hasYear):
                out["skippedByOnlyAdaptive"] += 1
                continue
            info = rebucketFace(one, person, dryRun=dryRun)
            out["faces"] += 1
            new = info["newBucket"]
            if not new:
                out["noShotYear"] += 1
            elif pcode and hasYear:
                out["adaptive"] += 1
            else:
                out["degraded"] += 1
            if info["changed"]:
                out["changed"] += 1
                out["written"] += int(info["written"] or 0)
                if len(out["samples"]) < _SAMPLE_NUM:
                    out["samples"].append(
                        {"faceCode": info["faceCode"], "shotYear": None,
                         "personCode": pcode or "(未归属)",
                         "birthday": birthday or "(无)",
                         "old": info["oldBucket"] or "(NULL)",
                         "new": new or "(NULL)"})
                pending.append(info)
                if len(pending) >= step:
                    _flush(pending, dryRun)
                    pending = []
        if progress:
            progress(out["faces"], total)
        if len(rows) < PAGE_ROWS:
            break
    _flush(pending, dryRun)
    out["totalFaces"] = total or 0
    out["elapsed"] = round(time.perf_counter() - start, 3)
    if out["changed"]:
        _LOG.info("全库刷桶：%d/%d 张脸改桶（自适应 %d / 降级 %d / 无拍摄年 %d），"
                  "耗时 %.2fs", out["changed"], out["faces"], out["adaptive"],
                  out["degraded"], out["noShotYear"], out["elapsed"])
    else:
        _LOG.info("全库刷桶：%d 张脸全部已是正确桶键，无需改动", out["faces"])
    return out


def _flush(pending: list, dryRun: bool) -> None:
    """把攒下的变更行落库（rebucketAll 的批提交）。"""
    if not pending or dryRun:
        return
    _writeBucketRows(pending)
    del pending[:]


# ============================================================
# 三、读：一致性巡检（只读，**不写任何一行**）
# ============================================================

def _personOf(personCode: str) -> dict:
    """pb_person 一行；查不到返回 None（**不抛** —— 巡检要能容忍脏数据）。"""
    code = str(personCode or "")
    if not code:
        return None
    for row in sqliteCommon.query_pb_person("pb_person", personCode=code,
                                            mode="light"):
        return row
    return None


def personFaceBuckets(personCode: str, confirmedOnly: bool = False) -> dict:
    """一个人当前脸表的桶键集合 -> {faceCount, confirmedCount, buckets, adaptive}。

    与 centroid.listBucketsOf 的口径差别只有一个：这里**默认统计全部脸**
    （巡检要看的是「脸表实际上都写了些什么」），而 listBucketsOf 默认只看
    确认样本（它服务的是质心计算）。两个口径都不能改，否则巡检会看不见
    自动归属样本的桶 —— 而那些桶同样是匹配侧要用的。
    """
    code = str(personCode or "")
    buckets, adaptive = set(), set()
    faces = confirmed = 0
    for row in sqliteCommon.query_pb_face("pb_face", personCode=code, mode="light"):
        faces += 1
        if int(row.get("isConfirmed") or 0):
            confirmed += 1
        key = str(row.get("shotBucket") or "")
        if not key or key == bucket.ALL_BUCKET:
            continue
        if confirmedOnly and not int(row.get("isConfirmed") or 0):
            continue
        buckets.add(key)
        if bucket.bucketWidth(key) != SHOT_BUCKET_WIDTH_FALLBACK:
            adaptive.add(key)
    return {"personCode": code, "faceCount": faces, "confirmedCount": confirmed,
            "buckets": sorted(buckets), "adaptive": sorted(adaptive)}


def auditBuckets(sampleNum: int = None) -> dict:
    """**桶口径一致性巡检**（只读，不写库）。

    报四件事
    --------
      ① **桶宽分布**：宽 3 / 宽 10 = 自适应（桶键跟着生日走）；
         宽 5 = 等宽降级（无生日或未归属，**这是合法状态**）；
         宽 0 = shotBucket 为 NULL（这张照片没有拍摄年份，不参与跨桶比对）。
      ② **孤儿质心**：pb_person_centroid 里的 bucketKey，在该人脸表中
         **没有任何脸**（且不是虚拟桶 ALL）。这类行会继续参与匹配，
         而它代表的年代桶已经不存在了 —— 匹配率会莫名其妙地低，且不报错。
      ③ **失配脸**：该人脸表的 shotBucket 集合与该人质心的 bucketKey 集合
         **不相交**。这正是 DR-22 描述的那个静默失配状态
         （质心按旧口径建、脸表已按新口径刷）。
      ④ **无主质心**：bucketKey != ALL 的行 sampleCount>0，但该桶已无脸。
         与 ② 的区别：② 是「桶键整个不存在」，④ 是「桶还在、脸走了」。

    为什么这个巡检是本步最有价值的产出
    ------------------------------
      「桶口径不一致」这个故障的全部表现是「匹配率下降」，而库里**没有任何
      一处会报错**。把它变成一条明确的计数 + 举例，才谈得上「修好了」。

    返回 dict（可直接 json 化；人脸库规模下 samples 只取样）
    """
    keep = int(sampleNum or _SAMPLE_NUM)

    # ---- ① 桶宽分布 ----
    widthStat = {}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light"):
        key = str(row.get("shotBucket") or "")
        width = bucket.bucketWidth(key)
        one = widthStat.setdefault(width, {"count": 0, "samples": [],
                                           "assigned": 0, "degradedOnly": 0})
        one["count"] += 1
        if str(row.get("personCode") or ""):
            one["assigned"] += 1
            if width == SHOT_BUCKET_WIDTH_FALLBACK:
                one["degradedOnly"] += 1
        if len(one["samples"]) < keep:
            one["samples"].append(
                {"faceCode": str(row.get("faceCode") or ""),
                 "shotBucket": key or "(NULL)",
                 "personCode": str(row.get("personCode") or "") or "(未归属)"})

    # ---- ②③④ 逐人比对 ----
    orphanCentroids, mismatchedFaces, ownerlessCentroids = [], [], []
    livePersons = set(str(p.get("personCode") or "")
                      for p in sqliteCommon.query_pb_person("pb_person",
                                                             mode="light")
                      if str(p.get("personCode") or ""))
    faceInfo = {}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light"):
        code = str(row.get("personCode") or "")
        if code:
            faceInfo.setdefault(code, set())
            key = str(row.get("shotBucket") or "")
            if key and key != bucket.ALL_BUCKET:
                faceInfo[code].add(key)
    for row in sqliteCommon.query_pb_person_centroid("pb_person_centroid",
                                                     mode="light"):
        code = str(row.get("personCode") or "")
        key = str(row.get("bucketKey") or "")
        if not code or key == bucket.ALL_BUCKET:
            continue                                   # ALL 是虚拟桶，另说
        have = faceInfo.get(code, set())
        if not have:
            orphanCentroids.append(
                {"personCode": code, "bucketKey": key,
                 "sampleCount": int(row.get("sampleCount") or 0),
                 "note": "该人脸表里这个桶一张脸都没有"})
        elif key not in have:
            ownerlessCentroids.append(
                {"personCode": code, "bucketKey": key,
                 "sampleCount": int(row.get("sampleCount") or 0),
                 "note": "桶键已不在脸表里（人脸被刷到别的桶去了）"})
    for code, have in sorted(faceInfo.items()):
        if code not in livePersons:
            continue                # 人已软删/查无此人：属assigner.verifyLinks 的活
        keys = set(str(r.get("bucketKey") or "") for r in
                   sqliteCommon.query_pb_person_centroid(
                       "pb_person_centroid", personCode=code, mode="light"))
        keys.discard(bucket.ALL_BUCKET)
        if keys and have and not (keys & have):
            mismatchedFaces.append(
                {"personCode": code, "faceBuckets": sorted(have),
                 "centroidBuckets": sorted(keys),
                 "note": "脸表与质心表的桶键完全不相交 —— 先重算质心后刷桶会造成的状态"})

    adaptiveCount = sum(one["count"] for width, one in widthStat.items()
                        if int(width) in (CHILD_WIDTH, ADULT_WIDTH))
    degradedCount = sum(one["count"] for width, one in widthStat.items()
                        if int(width) == SHOT_BUCKET_WIDTH_FALLBACK)
    warnings = []
    if degradedCount and not adaptiveCount:
        warnings.append(
            "全库 %d 张脸**全是等宽 %d 年桶、一条自适应桶都没有** —— "
            "自适应分桶从未生效（DR-20）。若这些人已归属且主人有生日，"
            "说明脸表桶键还没刷：跑 rebucket_cli.py --all --recompute"
            % (degradedCount, SHOT_BUCKET_WIDTH_FALLBACK))
    return {"widthStat": widthStat, "orphanCentroids": orphanCentroids,
            "mismatchedFaces": mismatchedFaces,
            "ownerlessCentroids": ownerlessCentroids,
            "warnings": warnings,
            "adaptiveCount": adaptiveCount, "degradedCount": degradedCount,
            "personCount": len(livePersons),
            "faceCount": sum(one["count"] for one in widthStat.values()),
            "clean": not (orphanCentroids or mismatchedFaces or ownerlessCentroids)}


def assertFacesFresh(personCode: str, confirmedOnly: bool = False,
                     shotYearCache: dict = None) -> dict:
    """**DR-22 前置检查**：这个人的脸表桶键是否都已是「当前口径」的。

    不一致 -> 抛 BucketStaleError，消息里直接给出该跑的命令。
    一致   -> 返回 {"personCode", "checked", "stale": []}

    为什么检查的是「脸表 vs 期望」而不是「脸表 vs 质心表」
    --------------------------------------------------
      脸表的桶键是**因**，质心的桶键是**果**。若拿「果」去校验「因」，
      「还没重算过」与「按错口径重算过」会混成同一个信号 ——
      于是第一次给某人建质心永远会被误判成不一致（死锁），
      而 split/merge 造成的「样本搬走、桶还挂着」又永远不会被判出来。
      直接问「脸表现在写的桶，是不是按这个人当前的生日算出来的」，
      答案唯一、精确、无歧义，而且**每一次归属变更都会立刻被覆盖**。

    confirmedOnly
      只检查人工确认样本（默认 False = 全部脸）。
      ⚠️ 默认 False 是刻意的：质心只用确认样本（DR-16③），但**匹配侧对
      自动归属样本一样按 shotBucket 取候选**。若只查确认样本，一个「已确认
      样本刷过、自动样本没刷」的档案会漏过检查，而它的自动样本仍会拿不到
      正确的候选桶 —— 又是一个不报错的失配。
    """
    code = str(personCode or "")
    person = _personOf(code)
    birthday = str((person or {}).get("birthday") or "")
    stale, checked = [], 0
    cache = {} if shotYearCache is None else shotYearCache
    offset = 0
    while True:
        rows = sqliteCommon.query_pb_face("pb_face", personCode=code, mode="light",
                                          orderBy="recID",
                                          limitNum=PAGE_ROWS, offsetNum=offset)
        if not rows:
            break
        offset += len(rows)
        for one in rows:
            if confirmedOnly and not int(one.get("isConfirmed") or 0):
                continue
            checked += 1
            want = expectedBucketOf(one, person)
            have = str(one.get("shotBucket") or "")
            if want != have:
                stale.append({"faceCode": str(one.get("faceCode") or ""),
                              "have": have or "(NULL)", "want": want or "(NULL)"})
        if len(rows) < PAGE_ROWS:
            break
    if stale:
        raise BucketStaleError(
            "%s 的 %d/%d 张脸 shotBucket 与当前口径不一致（生日=%s）："
            "库里 %s，应为 %s。**先刷桶再重算质心**："
            "python code\\src\\tools\\rebucket_cli.py --person %s"
            % (code, len(stale), checked, birthday or "(无)",
               stale[0]["have"], stale[0]["want"], code))
    return {"personCode": code, "checked": checked, "stale": []}


# ============================================================
# 四、报告
# ============================================================

def formatAudit(report: dict) -> str:
    """把auditBuckets() 的结果排成一张一眼能看完的表（纯文本，不写库）。"""
    out = []
    add = out.append
    add("=" * 74)
    add("桶口径一致性巡检（只读）")
    add("=" * 74)
    add("人员 %d 个/人脸 %d 张" % (report.get("personCount", 0),
                                  report.get("faceCount", 0)))
    add("")
    add("① 桶宽分布（宽 %d / 宽 %d = 自适应，宽 %d = 等宽降级，宽 0 = 无拍摄年份）"
        % (CHILD_WIDTH, ADULT_WIDTH, SHOT_BUCKET_WIDTH_FALLBACK))
    add("   %-9s %-28s %7s %10s   %s"
        % ("桶宽", "语义", "张数", "其中已归属", "举例"))
    for width in sorted(report.get("widthStat") or {}, reverse=True):
        one = report["widthStat"][width]
        meaning = WIDTH_MEANING.get(width, "**非三种标准宽度之一**")
        sample = one["samples"][0] if one["samples"] else {}
        add("   %-9s %-28s %7d %10d   桶=%-10s 人=%s"
            % (width or "(NULL)", meaning, one["count"], one["assigned"],
               sample.get("shotBucket", "-"), sample.get("personCode", "-")))
    add("   小结：自适应桶 %d 张 / 等宽降级 %d 张"
        % (report.get("adaptiveCount", 0), report.get("degradedCount", 0)))
    add("")
    for key, title, note in (
            ("orphanCentroids", "② 孤儿质心",
             "pb_person_centroid 里的桶键在该人脸表中一张脸都没有"),
            ("mismatchedFaces", "③ 失配脸",
             "脸表桶键与质心桶键**完全不相交**（先重算质心后刷桶的典型症状）"),
            ("ownerlessCentroids", "④ 无主质心",
             "非 ALL 桶 sampleCount>0，但该桶已无脸")):
        items = report.get(key) or []
        add("%s：%d 条 —— %s" % (title, len(items), note))
        for one in items[:_SAMPLE_NUM]:
            add("     %s" % one)
        if len(items) > _SAMPLE_NUM:
            add("     …… 另有 %d 条" % (len(items) - _SAMPLE_NUM))
    add("")
    for text in report.get("warnings") or ():
        add("⚠ %s" % text)
    if report.get("warnings"):
        add("")
    add("结论：%s" % ("桶口径一致：无孤儿质心 / 失配脸 / 无主质心"
                    if report.get("clean")
                    else "❌ 存在上述问题，先跑 rebucket 再重算质心"))
    return "\n".join(out)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK
        try:                                          # 打印 ✅/❌ 会 UnicodeEncodeError
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("rebucket.py _VERSION:", _VERSION)
    print("降级等宽桶宽  : %d 年（无生日 / 未归属）" % SHOT_BUCKET_WIDTH_FALLBACK)
    print("自适应桶宽    : 童年 %d 年 / 成年 %d 年（分界 %d 岁）"
          % (CHILD_WIDTH, ADULT_WIDTH, bucket.CHILD_MAX_AGE))
    print("桶键计算      : 一律走 bucket.bucketKeyAdaptive()（规则不在本模块重写）")
    print("写入纪律      : 只改 shotBucket 一列；upsert + forceColumns；不用 update_pb_face")
    print("库            :", sqliteCommon.dbFilePath())
    print()
    print(formatAudit(auditBuckets()))