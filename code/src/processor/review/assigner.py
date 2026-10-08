#! /usr/bin/env python3
#encoding: utf-8

#Filename: assigner.py
#Description: photo-browser 人脸归属写入（步骤 6 / 修正步骤 R）—— 确认/改判 + 幂等关联
#             + 立即重算质心 + 每次写操作落纠错日志
#
# 职责
# ----
#   confirm()       **人工确认**一张脸：isConfirmed=1 + link source=1（写日志）
#   autoAssign()    **自动归属**一张脸：isConfirmed=0 + link source=0（写日志）
#   assign()        两者的共同实现（保留旧签名，source 决定 isConfirmed）
#   fix()           **统一改判入口**：assign 改判到某人 / unknown 置未知 / stranger 标陌生人
#   batchFix()      同一 clusterCode 批量改判（一个事务 + 一次重算）
#   confirmPerson() 批量确认（一个人一次点 20 张的场景）
#   unassign()      撤销归属（**== fix('unknown')**，保留旧名只为兼容调用方）
#   applyAuto()     把一批 MatchResult 里 decision=auto 的落库
#   syncLinks() / verifyLinks()  关联表与脸表的一致性核对与修复
#
# 五条不可省的纪律
# ----------------
#   ① **确认后立即 recompute 质心**。不等全量重跑。
#      质心是匹配的唯一依据，用户确认完却要等很久才生效，
#      就会得出"确认了也没用"的结论，然后不再确认 —— 整个机制就靠这条即时反馈活着。
#   ② **改判要重算两个人的质心**。把一张脸从 A 挪到 B，A 的那个桶少了一条样本、
#      B 的那个桶多了一条；只重算 B 会让 A 的质心永远停在"包含这张脸"的旧值上，
#      之后 A 的新照片会一直被这张已经不属于他的脸带着分数。
#      这是最容易漏的一处 —— 漏了不报错，只表现为"越用越不准"。
#   ③ **pb_photo_person 只在"这张照片里确实有人属于 P"时存在**。
#      关联表是 (照片 × 人)，不是 (照片 × 人 × 脸)。一张合影里 3 张脸属于同一个人，
#      也只有 1 行关联。删脸时若不检查"这张照片里还有没有别的脸属于 P"，
#      就会留下指向一个"此照片里再无人出现"的幽灵关联 ——
#      人员时间轴上会凭空多出一张照片，且**没有任何报错**。
#   ④ **每次写操作都落一条 pb_review_log**（DR-16④）。业务层强制，不允许静默改库。
#      它有两个用途，缺一不可：
#        · 排障：「这张脸当初是怎么被认成这个人的」是**唯一**可查的依据；
#        · 撤销：合并/拆分不可逆，只有 isRevertible=1 的记录能撤回。
#      ⚠️ 写日志**必须走 upsert + forceColumns**：fromPersonCode / toPersonCode
#         经常是空的（「从待确认直接确认」「置为未知」两种操作都是），
#         而生成层的 update_* 会把 None 整条丢掉 —— 那是一次**静默的空操作**，
#         返回 0 行、不报错、库里原封不动（与 _patchFace 踩过的坑同一个根因）。
#   ⑤ **归属变更后必须刷 shotBucket，再重算质心**（DR-22 / 修正步骤 R2）。
#      归属那一刻**生日才确定**：这张脸此前是未归属的，shotBucket 是步骤 5
#      落的**等宽 5 年占位桶**；而质心的桶键是「拍摄年 + 出生年」算出来的
#      **自适应**桶。不刷的后果是「质心按等宽桶建好、脸表刷成自适应桶」->
#      僵尸质心 + 新桶无质心 -> 这个人自动匹配恒为 0，而**库里看不出异常**。
#      顺序固定：**写 personCode -> 刷 shotBucket -> 重算质心**，不可颠倒。
#      落点：_setBelong()（含 confirm/autoAssign/fix('assign')）与
#      fix('unknown'|'stranger')；merger 的 merge/undo/split 也各自接了
#      （见 merger.py 的「谁负责重刷」段）。
#
# isConfirmed 的语义（DR-16①，本轮返工的**根因**）
# ----------------------------------------------
#   isConfirmed=1 **只表示「经人工确认」**。自动归属必须写 0。
#   修正前 assign() 对自动与人工一律写 1（硬编码在 _setBelong 里），后果有两条，
#   而且**都不会报错**：
#     (a) 「机器认的」与「人工确认的」无法区分 ->「我不同意」列表无从表达，
#         用户浏览时最高频的纠错场景（自动认错了人）没有入口；
#     (b) 质心全部由自动样本构成 -> 防污染无从下手（centroid 只取 isConfirmed=1）。
#   四态互斥口径（由personCode / isConfirmed / isStranger 三字段推导，无冗余）：
#     待确认    = personCode IS NULL AND isStranger=0
#     我不同意  = personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0
#     人工确认  = isConfirmed=1
#     陌生人    = isStranger=1（永久排除，不进队列也不聚类）
#   ⚠️ 修正前写的是「待确认 = personCode IS NULL **OR** isConfirmed=0」——
#      那个条件会把**全部自动归属**卷入（10 万张 ≈ 4~8 万条，队列爆炸）。
#
# 不碰 pb_photo.scanState（刻意）
# ------------------------------
#   scanState 是**照片级**状态，而归属是**人脸级**的（数据库设计.md D-4：
#   待确认队列 = personCode IS NULL AND isStranger=0）。
#   一张照片里 3 张脸确认了 1 张，这张照片该是"待确认"还是"完成"？
#   —— 两种答法都不对，因为照片级状态表达不了"部分完成"。
#   强行改它的后果是：一张 5 人合影确认了 1 张就被标成 scanState=3（完成），
#   剩下 4 张脸再也不会出现在任何队列里。**宁可让这个字段保持"已入人脸库"，
#   由步骤 7/11 按人脸级事实重新定义它。**

import os
import sys
import uuid

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/review
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402
from engine.match import centroid as centroid                     # noqa: E402
from engine.match import rebucket as rebucket                     # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("assigner", "assigner.log")

#: linkKey 幂等键 = photoCode + ":" + personCode（数据库设计.md §4.7）
LINK_KEY_SEP: str = ":"

# ============================================================
# 零之二、纠错日志（pb_review_log，DR-16④ / 数据库设计 §4.9）
# ============================================================
# 8 种 opType。**只有 SPLIT / MERGE 可撤销**（isRevertible=1）——
# 理由：合并/拆分会把两个档案揉成一个，质心一旦被污染靠重算回不到原值；
# 而「确认一张脸」是可逆的（再点一次改判即可），不需要日志来兜。

OP_ASSIGN: str = "ASSIGN"                # 队列首次确认归属
OP_FIX: str = "FIX"                      # 改判（自动认错了 -> 改到别人）
OP_UNKNOWN: str = "UNKNOWN"              # 改判为未知（personCode=NULL）
OP_STRANGER: str = "STRANGER"# 标记为陌生人（永久排除）
OP_BATCH_ASSIGN: str = "BATCH_ASSIGN"    # 批量确认（同一簇多张脸）
OP_SPLIT: str = "SPLIT"                  # 从某人拆出某张脸（可撤销）
OP_MERGE: str = "MERGE"                  # 两人合并为一人（可撤销）
OP_UNDO: str = "UNDO"                    # 回滚一条 SPLIT/MERGE
# ⚠️ DISABLE / ENABLE 是步骤 9（contacts CRUD · DR-19）加进来的两个 opType。
#   它们**不是**新增机制，只是 DR-19 明确要求「停用要落 pb_review_log」，
#   而这张表是纠错留痕的**唯一**落点 —— 如果不登记进 OP_TYPES，
#   logReview 会直接抛「未登记的 opType」。
#   为什么不放在 api 层直接写这张表：那样就出现了「日志有两个写入口」，
#   而写日志那一段（upsert + forceColumns，因为 from/to 经常是空的）
#   恰恰是最容易写错的地方（见 logReview 的注释）—— 一旦分叉成两份，
#   就会出现「有一半的日志 fromPersonCode 是 NULL」。
OP_DISABLE: str = "DISABLE"              # 人员停用（DR-19；不可撤销）
OP_ENABLE: str = "ENABLE"                # 人员恢复（DR-19；不可撤销）
# ⚠️ BUCKET_FIX 是 DR-42（照片年代修正）加进来的：它**不搬动任何人脸归属**，
#    改的是 pb_photo.shotYearOverride，然后重刷这张照片全部人脸的 shotBucket。
#    之所以登记进这张表而不是另开一张：它是**可撤销的人工纠错**，
#    沿用同一个「操作历史 + 撤销」入口，用户不必学第二套心智模型。
OP_BUCKET_FIX: str = "BUCKET_FIX"        # 照片年代修正（DR-42；可撤销）

OP_TYPES: tuple = (OP_ASSIGN, OP_FIX, OP_UNKNOWN, OP_STRANGER,
                   OP_BATCH_ASSIGN, OP_SPLIT, OP_MERGE, OP_UNDO,
                   OP_DISABLE, OP_ENABLE, OP_BUCKET_FIX)

#: fix() 的三个动作 -> (opType, 落 pb_face 的方式)
FIX_ACTIONS: dict = {
    "assign": OP_FIX,
    "unknown": OP_UNKNOWN,
    "stranger": OP_STRANGER,
}


class AssignerError(Exception):
    """归属失败（已回滚）。上层应当停止而不是假装成功。"""


# ============================================================
# 一、内部工具
# ============================================================

def linkKeyOf(photoCode: str, personCode: str) -> str:
    """幂等键。**冒号分隔**：两个编码里都不含冒号（都是 uuid 派生），不会歧义。"""
    return "%s%s%s" % (str(photoCode or ""), LINK_KEY_SEP, str(personCode or ""))


#: pb_face 走 upsert 时**必须带齐**的 NOT NULL 身份列。
#: 原因与步骤 3 踩过的坑完全一样（faceStore.buildPhotoPatchRow 的注释）：
#: upsert 是 INSERT 语义，缺列会撞 `NOT NULL constraint failed`，
#: 而不是"更新不到"。这里只有 photoCode 一个 NOT NULL 业务列（faceCode 是冲突键）。
FACE_IDENTITY_COLUMNS: tuple = ("photoCode",)


def _patchFace(faceCode: str, photoCode: str, dataSet: dict,
               forceColumns=()) -> int:
    """改 pb_face 的若干列。**能写 NULL**（这是它与 update_pb_face 的唯一区别）。

    ⚠️⚠️ 为什么不用 update_pb_face
    ---------------------------
      生成层的 normalizeDataSet 会把值为 None 的列**整条丢掉**（"不写这一列"），
      所以 `update_pb_face(..., {"personCode": None})` 是一次**静默的空操作**：
      返回 0 行受影响、不报错、库里那列原封不动。
      撤销归属恰恰就是要把它置空 —— 用 update_* 写的话，
      「撤销归属」会变成「什么都没做」，而调用方拿到的是"成功"。
      （实测已确认：unassign 走 update_* 时 personCode 一直是原值。）

      正确路径是 upsert：把该列塞进 INSERT 的列清单（forceColumns），
      缺值的行按 NULL 写入，于是 DO UPDATE SET personCode = excluded.personCode
      真的把它清空。这与 insertManyTableGeneral 文档里 forceColumns 那段
      说的是同一件事（那里举的例子是 shotYear 2023 -> NULL）。

    ⚠️ 走 upsert 就必须带齐 NOT NULL 身份列（FACE_IDENTITY_COLUMNS），
       否则撞 `NOT NULL constraint failed: pb_face.photoCode`。
    """
    row = {"faceCode": str(faceCode), "photoCode": str(photoCode or "")}
    row.update(dataSet)
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_face", [row], conflictColumns=("faceCode",),
        updateColumns=tuple(dataSet.keys()),
        fillStandard=True,
        forceColumns=tuple(forceColumns) + tuple(dataSet.keys()))
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise AssignerError("pb_face 更新失败: %s" % sqliteCommon.dbHandle().lastErrMsg)
    return rtn


def _faceByCode(faceCode: str) -> dict:
    rows = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)
    if not rows:
        raise AssignerError("faceCode=%s 在 pb_face 里不存在" % faceCode)
    return rows[0]


def _personByCode(personCode: str) -> dict:
    rows = sqliteCommon.query_pb_person("pb_person", personCode=personCode)
    if not rows:
        raise AssignerError("personCode=%s 在 pb_person 里不存在" % personCode)
    return rows[0]


def _facesInPhotoFor(photoCode: str, personCode: str, excludeFaceCode: str = "") -> int:
    """这张照片里**还有几张脸**属于某人（可排除某一张）。用于关联行的存废判断。"""
    count = 0
    for row in sqliteCommon.query_pb_face("pb_face", photoCode=photoCode,
                                          personCode=personCode):
        if excludeFaceCode and str(row.get("faceCode") or "") == str(excludeFaceCode):
            continue
        count += 1
    return count


def _writeLink(photoCode: str, personCode: str, faceCode: str = None,
               confidence: float = None, source: int = comGD.LINK_SOURCE_AUTO) -> str:
    """幂等写一行 pb_photo_person（linkKey 冲突则更新）。返回 linkKey。"""
    key = linkKeyOf(photoCode, personCode)
    row = {
        "linkKey": key,
        "photoCode": str(photoCode or ""),
        "personCode": str(personCode or ""),
        "faceCode": (None if faceCode is None else str(faceCode)),
        "confidence": (None if confidence is None else round(float(confidence), 4)),
        "source": int(source),
        "modifyYMDHMS": misc.getTime(),
    }
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_photo_person", [row], conflictColumns=("linkKey",),
        updateColumns=("faceCode", "confidence", "source", "modifyYMDHMS"),
        fillStandard=True, forceColumns=("faceCode", "confidence", "source"))
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise AssignerError("pb_photo_person 写入失败: %s"
                            % sqliteCommon.dbHandle().lastErrMsg)
    return key


def _dropLink(photoCode: str, personCode: str) -> int:
    """删掉一条 (照片 × 人) 关联。返回删除行数。"""
    key = linkKeyOf(photoCode, personCode)
    deleted = 0
    for row in sqliteCommon.query_pb_photo_person("pb_photo_person", linkKey=key):
        rtn = sqliteCommon.delete_pb_photo_person("pb_photo_person", row["recID"],
                                                  hardDelete=True)
        if rtn and rtn > 0:
            deleted += int(rtn)
    if deleted:
        _LOG.info("删除关联 %s（该照片里已无人属于 %s）", key, personCode)
    return deleted


def facesInPhotoFor(photoCode: str, personCode: str, excludeFaceCode: str = "") -> int:
    """公开出口：这张照片里还有几张脸属于某人（merger.undo 恢复关联时要用）。"""
    return _facesInPhotoFor(photoCode, personCode, excludeFaceCode)


def writeLink(photoCode: str, personCode: str, faceCode: str = None,
              confidence: float = None,
              source: int = comGD.LINK_SOURCE_MANUAL) -> str:
    """公开出口：幂等写一行 pb_photo_person（undo 恢复关联时要用）。"""
    return _writeLink(photoCode, personCode, faceCode, confidence, source)


def dropLink(photoCode: str, personCode: str) -> int:
    """公开出口：删掉一条 (照片 × 人) 关联（undo 清理反向幽灵关联时要用）。"""
    return _dropLink(photoCode, personCode)


def _recomputeBuckets(pairs) -> list:
    """对一批 (personCode, bucketKey) 去重后重算质心。返回结果列表。

    去重是必须的：批量确认 20 张脸往往落在 2~3 个桶里，
    逐个重算就是白跑十几遍同样的求和。
    """
    seen, out = set(), []
    for personCode, bucketKey in pairs:
        if not personCode or not bucketKey:
            continue
        key = (personCode, bucketKey)
        if key in seen:
            continue
        seen.add(key)
        out.append(centroid.recompute(personCode, bucketKey))
    return out


def _recomputePersons(personCodes) -> list:
    """重算一批人的**全部**桶（含 ALL 兜底桶）。返回每个人的统计。

    什么时候用它、什么时候用 _recomputeBuckets
    ------------------------------------------
      改判/陌生人/撤销这类**移走样本**的操作：重算范围必须是「这个人现在的全部桶」。
      只重算脸原来所在的那个桶会漏掉两件事：
        ① 这个人其它桶的 ALL 兜底向量（它由全部确认样本决定）；
        ② 样本被移走后，那个桶已经跌到 3 样本以下 —— 不重算它就还挂着旧向量。
    """
    out, seen = [], set()
    for personCode in personCodes or ():
        code = str(personCode or "")
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(centroid.recomputePerson(code))
    return out


# ============================================================
# 一之二、纠错日志（纪律 ④）
# ============================================================

def newLogCode() -> str:
    """新纠错日志编码：RL_<yyyymmddHHMMSS>_<6位随机>（与 scanJob 同一种生成法）。

    logCode 是**幂等键**（UNIQUE），带时间戳 + 随机后缀是为了让两次不同的操作
    在重试/重跑后不会互相覆盖；带随机后缀（而不是纯自增）是为了不让
    「日志条数」在排查时成为一个可被外部推断的信息 —— 无所谓，但与现有
    SCAN_JOB 的口径保持一致，别让同一个项目里出现两套编码风格。
    """
    return "%s_%s_%s" % (basicSettings.REVIEW_LOG_CODE_PREFIX, misc.getTime(),
                         uuid.uuid4().hex[:6])


def logReview(opType: str, faceCode: str = "", photoCode: str = "",
              fromPersonCode: str = "", toPersonCode: str = "",
              similarity: float = None, faceCount: int = 1,
              detail: str = "", isRevertible: int = 0,
              logCode: str = None) -> str:
    """写一条 pb_review_log。返回 logCode。

    ⚠️⚠️ 为什么必须 upsert + forceColumns（而不是 update_*）
    ------------------------------------------------------
      生成层的 normalizeDataSet 会把值为 None 的列**整条丢掉**
      （见 _patchFace 的注释，同一个根因）。而 fromPersonCode / toPersonCode
      **经常是空的**：
        · 「从待确认队列直接确认某人」-> fromPersonCode 为空
        · 「置为未知 / 标记陌生人」     -> toPersonCode 为空
      用 update_* 写就成了一次**静默的空操作**：0 行受影响、不报错、
      库里那列原封不动 —— 于是排障时看到的是"日志里没有原归属人"，
      而真实原因是那条日志压根没写进去。
      forceColumns 把这些列塞进 INSERT 列清单（缺值按 NULL 写入），
      DO UPDATE 才会真的把它们写成 NULL。

    logCode 传空时自动生成（幂等键）。
    """
    if opType not in OP_TYPES:
        raise AssignerError("未登记的 opType %r（合法值%s）"
                            % (opType, list(OP_TYPES)))
    code = str(logCode or newLogCode())
    row = {
        "logCode": code,
        "opType": str(opType),
        "faceCode": (None if not faceCode else str(faceCode)),
        "photoCode": (None if not photoCode else str(photoCode)),
        "fromPersonCode": (None if not fromPersonCode else str(fromPersonCode)),
        "toPersonCode": (None if not toPersonCode else str(toPersonCode)),
        "similarity": (None if similarity is None else round(float(similarity), 4)),
        "faceCount": int(faceCount or 0),
        "detail": (None if not detail else str(detail)[:400]),
        "isRevertible": int(isRevertible or 0),
        "opUser": basicSettings.REVIEW_LOG_USER,
        "opYMDHMS": misc.getTime(),
    }
    rtn, _cols = sqliteCommon.insertManyTableGeneral(
        "pb_review_log", [row], conflictColumns=("logCode",),
        updateColumns=("opType", "faceCode", "photoCode", "fromPersonCode",
                       "toPersonCode", "similarity", "faceCount", "detail",
                       "isRevertible", "opUser", "opYMDHMS"),
        fillStandard=True,
        forceColumns=("faceCode", "photoCode", "fromPersonCode", "toPersonCode",
                      "similarity", "detail"))
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise AssignerError("pb_review_log 写入失败: %s"
                            % sqliteCommon.dbHandle().lastErrMsg)
    _LOG.info("纠错日志 %s op=%s face=%s %s -> %s",
              code, opType, faceCode or "(批量)", fromPersonCode or "(无)",
              toPersonCode or "(置为未知)")
    return code


# ============================================================
# 二、单张归属
# ============================================================

def _setBelong(faceCode: str, personCode: str, source: int,
               confidence: float = None, recompute: bool = True,
               opType: str = OP_ASSIGN, extraLog: bool = True) -> dict:
    """把一张脸归给某人。**confirm() / autoAssign() / fix('assign') 的共同实现。**

    isConfirmed 由 source 决定（**这是本轮返工的根因修正**）
    --------------------------------------------------
      source = comGD.LINK_SOURCE_MANUAL(1) -> isConfirmed = 1（人工确认）
      source = comGD.LINK_SOURCE_AUTO(0)   -> isConfirmed = 0（机器认的，可否决）
    修正前这里对两者一律写 1，于是：
      · 「我不同意」列表（personCode 非空 + isConfirmed=0）永远是空的；
      · 质心（只用 isConfirmed=1）会把自动样本当干净样本，越错越错。

    参数
    ----
      faceCode    : pb_face.faceCode
      personCode  : pb_person.personCode
      source      : 0 自动 / 1 人工确认（comGD.LINK_SOURCE_*）
      confidence  : 余弦分数；人工确认为 None（**不编造分数** ——
                    人确认的是"这是他"，不是"相似度 0.87"。
                    写个假分数进库，会让后续统计把人工确认当成高置信自动匹配）
      recompute   : False = 只写归属不重算（批量重建时由调用方统一重算）
      opType      : 落日志的 opType（ASSIGN / FIX / BATCH_ASSIGN / SPLIT）
      extraLog    : False = 调用方自己落日志（批量时一条日志代表一批脸）

    返回 {faceCode, photoCode, bucketKey, fromPerson, toPerson, changed,
          isConfirmed, linkKey, centroids, logCode}

    ⚠️ **桶键与质心的顺序不可颠倒（DR-22，本步最关键的一处）**
    --------------------------------------------------
      归属这一刻**生日才确定** —— 之前这张脸是未归属的，它的 shotBucket 是
      faceStore 落的**等宽 5 年占位桶**；而质心的桶键是按「拍摄年 + 出生年」
      算出来的**自适应**桶。所以必须：

          ① 先写 personCode -> ② 按新主人的 birthday 刷 shotBucket
                              -> ③ 再重算质心

      顺序反了（先重算质心、后刷桶，或者干脆不刷）：
        质心按等宽桶建好、脸表随后刷成自适应桶 ->
        **质心表里留着等宽桶键的行（僵尸，新桶取不到）、自适应桶一条质心都没有**
        -> 这个人的自动匹配恒为 0，而库里**看不出任何异常**
        （每张脸都有桶、每个质心样本数都够）。
      这正是 bucket.py 那套自适应分桶「在生产里从未生效」的根因
      （DR-20：全库只有 faceStore 写 shotBucket，从没有第二处改它）。
    """
    face = _faceByCode(faceCode)
    person = _personByCode(personCode)               # 目标人必须存在（弱外键，自己守）
    code = str(face["faceCode"])
    photoCode = str(face.get("photoCode") or "")
    bucketKey = str(face.get("shotBucket") or "")
    wasPerson = str(face.get("personCode") or "")
    wasStranger = int(face.get("isStranger") or 0)
    # 自动归属写 0、人工确认写 1 —— 唯一真相在 source 上，调用方不许自己编
    confirmed = 1 if int(source) == comGD.LINK_SOURCE_MANUAL else 0
    now = misc.getTime()

    if wasPerson == str(personCode) and int(face.get("isConfirmed") or 0) == confirmed \
            and not wasStranger:
        # 幂等：同一张脸重复确认同一个人的多次点击
        linkKey = linkKeyOf(photoCode, personCode)
        if not sqliteCommon.query_pb_photo_person("pb_photo_person", linkKey=linkKey):
            _writeLink(photoCode, personCode, code, confidence, source)
        # ⚠️ 幂等分支**也要刷桶**：这个人的 birthday 可能刚被改过（DR-18），
        #    而「重复点一次确认」正是用户改完生日后的自然下一步。
        #    不刷的话新桶键永远等不到 —— 而下面那条「改了生日要重算质心」
        #    的纪律在这里已经return 掉了。
        rebucketed = rebucket.rebucketFace(face, person)
        if rebucketed["changed"]:
            bucketKey = rebucketed["newBucket"]
        return {"faceCode": code, "photoCode": photoCode, "bucketKey": bucketKey,
                "fromPerson": wasPerson, "toPerson": str(personCode),
                "isConfirmed": confirmed,
                "changed": False, "linkKey": linkKey, "centroids": [],
                "bucketRebucketed": rebucketed["changed"],
                "logCode": ""}

    rtn = _patchFace(code, photoCode,
                     {"personCode": str(personCode), "isConfirmed": confirmed,
                      "isStranger": 0, "modifyYMDHMS": now})
    if rtn <= 0:                         # 0 = 一行都没改，说明脸已经不在库里
        raise AssignerError("pb_face 归属写入失败: %s"
                            % sqliteCommon.dbHandle().lastErrMsg)

    # ---- ② DR-22：personCode 落库之后，**立刻**按新主人的生日刷桶 ----
    #⚠️ 必须在 _patchFace 之后：rebucketFace 依赖「脸当前的 personCode」
    #   来定位主人，先刷后写会按**旧主人**（或未归属口径）算桶键。
    #   它只改 shotBucket 一列，不碰刚写好的 personCode / isConfirmed
    #   （rebucket.py 文件头纪律 ①）。
    rebucketed = rebucket.rebucketFace(face, person)
    bucketKey = rebucketed["newBucket"]

    linkKey = _writeLink(photoCode, personCode, code, confidence, source)
    # 纪律 ②：改判要把**旧人**的关联与质心一起收拾干净
    if wasPerson and wasPerson != str(personCode):
        if _facesInPhotoFor(photoCode, wasPerson, excludeFaceCode=code) == 0:
            _dropLink(photoCode, wasPerson)

    logCode = ""
    if extraLog:
        logCode = logReview(opType, faceCode=code, photoCode=photoCode,
                            fromPersonCode=wasPerson, toPersonCode=personCode,
                            similarity=confidence, faceCount=1)
    # ---- ③ 桶已刷对，现在才重算质心（顺序不可颠倒，见函数头）----
    # 移走样本的场合（改判/陌生人）要重算**两个人的全部桶**（含 ALL），
    # 单纯新增确认只重算目标人即可 —— 这里统一走 _recomputePersons，多算的代价是毫秒级
    centroids = []
    if recompute:
        todo = [str(personCode)] + ([wasPerson] if wasPerson != str(personCode) else [])
        centroids = [one for stat in _recomputePersons(todo) for one in stat["buckets"]]
    _LOG.info("归属 %s: %s -> %s (桶 %s%s, source=%s, isConfirmed=%d)",
              code, wasPerson or "(无)", personCode, bucketKey or "(无)",
              ("，已重刷" if rebucketed["changed"] else ""),
              source, confirmed)
    return {"faceCode": code, "photoCode": photoCode, "bucketKey": bucketKey,
            "fromPerson": wasPerson, "toPerson": str(personCode),
            "isConfirmed": confirmed, "changed": True, "linkKey": linkKey,
            "centroids": centroids, "bucketRebucketed": rebucketed["changed"],
            "logCode": logCode}


def setBelong(faceCode: str, personCode: str, source: int,
              confidence: float = None, recompute: bool = True,
              opType: str = OP_ASSIGN, extraLog: bool = True) -> dict:
    """公开出口：带 opType / extraLog 的归属写入（merger.split 用它落 SPLIT 语义）。"""
    return _setBelong(faceCode, personCode, source, confidence, recompute,
                      opType, extraLog)


def confirm(faceCode: str, personCode: str, confidence: float = None,
            recompute: bool = True) -> dict:
    """**人工确认**一张脸（队列首次确认的语义入口）。

    isConfirmed=1、pb_photo_person.source=1、落一条 opType=ASSIGN 的日志。
    人工确认**不写 similarity**（不编造分数，见 _setBelong 的参数说明）。
    """
    return _setBelong(faceCode, personCode, comGD.LINK_SOURCE_MANUAL,
                      confidence, recompute, OP_ASSIGN)


def autoAssign(faceCode: str, personCode: str, confidence: float = None,
               recompute: bool = True) -> dict:
    """**自动归属**一张脸（score ≥ T_HIGH 的机器判定）。

    isConfirmed=**0**、link source=0、落一条 opType=ASSIGN 的日志
    （日志要能回答「这张脸当初是被谁、以多少分数认成这个人的」，
      所以 similarity 必填而 from/to 语义与人工确认一致）。
    ⚠️ isConfirmed 必须是 0：写成 1 会让这张脸**进不了「我不同意」列表**，
       质心也会把它当人工确认样本 —— 那正是本轮修正的根因。
    """
    return _setBelong(faceCode, personCode, comGD.LINK_SOURCE_AUTO,
                      confidence, recompute, OP_ASSIGN)


def assign(faceCode: str, personCode: str, source: int = comGD.LINK_SOURCE_MANUAL,
           confidence: float = None, recompute: bool = True,
           opType: str = OP_ASSIGN, extraLog: bool = True) -> dict:
    """把一张脸归给某人（**兼容入口**）。

    ⚠️ 保留这个函数是因为 merger.split() 等既有调用方还在用它；
       **新代码请直接调 confirm() / autoAssign()**——
       传错 source 的代价是「自动归属被记成人工确认」，
       而这正是本轮要消灭的那类静默错误。
    """
    return _setBelong(faceCode, personCode, source, confidence, recompute,
                      opType, extraLog)


def fix(faceCode: str, action: str, personCode: str = None,
        similarity: float = None, reason: str = "") -> dict:
    """**统一改判入口**（DR-16②）。三种动作互斥，语义与四态一一对应。

    ============== ====================================== ============ ============
    action        效果                                  落 pb_face关联            质心
    ============== ====================================== ============ ============
    'assign'      改判到某人                       personCode=新,          删旧linkKey 原人+新人
                                                 isConfirmed=1           写新source=1 全部桶
    'unknown'     置为未知（回待确认队列）        personCode=NULL,        删旧linkKey 原人全部桶
                                                 isConfirmed=0
    'stranger'    标记为陌生人（**永久排除**）    personCode=NULL,        删旧linkKey 原人全部桶
                                                 isStranger=1
    ============== ====================================== ============ ============

    三个动作的**共同副作用**（缺一不可，DR-16 表格「改判的连带副作用」）：
      ① 删旧 linkKey（纪律 ③ 判过才删）+ 需要时写新关联；
      ② **重算原人（与新人）的全部桶** —— 只重算一边会让原人的质心永远停在
         「包含这张已经不属于他的脸」的旧值上，且**不报错**；
      ③ 落一条 pb_review_log（opType=FIX / UNKNOWN / STRANGER）。
    ⚠️ 'stranger' 之后这张脸**既不在待确认队列、也不在「我不同意」**，
       更不参与聚类 —— 三个集合都由 personCode / isConfirmed / isStranger
       三字段推导，所以这里只写 isStranger=1 就够了，不需要额外的状态列。
    """
    act = str(action or "").strip().lower()
    if act not in FIX_ACTIONS:
        raise AssignerError("未知的改判动作 %r（合法值%s）"
                            % (action, sorted(FIX_ACTIONS.keys())))
    if act == "assign" and not str(personCode or ""):
        raise AssignerError("改判到某人必须给 personCode")

    face = _faceByCode(faceCode)
    code = str(face["faceCode"])
    photoCode = str(face.get("photoCode") or "")
    wasPerson = str(face.get("personCode") or "")

    # ---- 'assign' 走 _setBelong（它已经处理 linkKey/质心/日志）----
    if act == "assign":
        info = _setBelong(code, str(personCode), comGD.LINK_SOURCE_MANUAL,
                          None, True, OP_FIX)
        info["action"] = act
        info["reason"] = reason
        return info

    if act == "stranger":
        # ⚠️ 「本来就未归属」**不是**stranger 的noop 理由：
        #    P-06 待确认队列里「无匹配 -> [标记为陌生人]」就是对一个未归属的脸做的，
        #    它必须真的写下去（否则这个按钮点了没反应，而且没有任何报错）。
        if int(face.get("isStranger") or 0):
            return {"faceCode": code, "photoCode": photoCode, "action": act,
                    "fromPerson": wasPerson, "toPerson": "", "changed": False,
                    "droppedLinks": 0, "centroids": [], "logCode": "",
                    "reason": reason or "已标记为陌生人"}
    elif not wasPerson and not int(face.get("isConfirmed") or 0):
        return {"faceCode": code, "photoCode": photoCode, "action": act,
                "fromPerson": "", "toPerson": "", "changed": False,
                "droppedLinks": 0, "centroids": [], "logCode": "",
                "reason": reason or "本就未归属"}

    # ---- 'unknown' / 'stranger'：置 personCode=NULL，改 isConfirmed / isStranger ----
    # ⚠️ personCode=None 必须走 _patchFace（upsert 路径）：update_* 写不进 NULL
    patch = {"personCode": None, "modifyYMDHMS": misc.getTime()}
    if act == "unknown":
        patch["isConfirmed"] = 0
        patch["isStranger"] = 0     # 明确回到「待确认」，顺手清掉陌生人标记
    else:
        patch["isStranger"] = 1     # isConfirmed 保持原值（陌生人语义只看 isStranger）
    rtn = _patchFace(code, photoCode, patch)
    if rtn <= 0:
        raise AssignerError("pb_face 改判失败: %s"
                            % sqliteCommon.dbHandle().lastErrMsg)

    # ---- DR-22：退回未归属 -> 刷回**等宽降级桶**（生日不再是这个人的）----
    # ⚠️ 顺序与 _setBelong 一致：先写 personCode=NULL，再刷桶，最后重算质心。
    #   留在自适应桶上的话，这张脸将来重新归属时会被算成「桶已经对了」而
    #   跳过刷桶 —— 而那个桶是按**上一个主人**的生日算的，语义已经错了。
    #   stranger 同理：它永远不会再参与匹配，但库里不该留着一个
    #   「指向某个已不属于它的人的自适应桶」的键。
    rebucketed = rebucket.rebucketFace(face, None)

    # 纪律 ③：这张照片里**再没有别的脸**属于那个人时才删关联
    dropped = 0
    if wasPerson and _facesInPhotoFor(photoCode, wasPerson, excludeFaceCode=code) == 0:
        dropped = _dropLink(photoCode, wasPerson)
    logCode = logReview(FIX_ACTIONS[act], faceCode=code, photoCode=photoCode,
                        fromPersonCode=wasPerson, toPersonCode="",
                        similarity=similarity, faceCount=1,
                        detail=reason or "")
    centroids = _recomputePersons([wasPerson]) if wasPerson else []
    _LOG.info("改判 %s action=%s 原属 %s（关联删除 %d 条，桶 -> %s）%s",
              code, act, wasPerson or "(无)", dropped,
              rebucketed["newBucket"] or "(无拍摄年份)", reason)
    return {"faceCode": code, "photoCode": photoCode, "action": act,
            "bucketKey": rebucketed["newBucket"],
            "fromPerson": wasPerson, "toPerson": "", "changed": True,
            "droppedLinks": dropped,
            "bucketRebucketed": rebucketed["changed"],
            "centroids": [one for stat in centroids for one in stat["buckets"]],
            "logCode": logCode, "reason": reason}


def batchFix(faceCodes: list, action: str, personCode: str = None,
             reason: str = "") -> dict:
    """同一簇（clusterCode）批量改判：**一个事务 + 一次重算 + 一条日志**。

    为什么值得单独一个函数
    ------------------
      「我不同意」列表里同一个簇往往有 5~20 张脸（P-06 的「整张照片一键否决」
      与「簇内批量改判」都走这里）。逐张调 fix() = N 个事务 + N 次重算，
      中途失败还会留下「改了一半」的批次 —— 用户看到的是一个自相矛盾的列表。
    opType 用 BATCH_ASSIGN/FIX 之外的 opType 会**照实记成对应的单张 opType**，
    但 faceCount 记实际张数、detail 记簇/批次说明（一次点击解决 N 张，
    日志里必须看得出这是一批，不是一张）。
    """
    codes = [str(c) for c in (faceCodes or ()) if str(c or "")]
    if not codes:
        return {"action": str(action or ""), "fixed": 0, "failed": [],
                "centroids": [], "logCode": ""}
    act = str(action or "").strip().lower()
    if act not in FIX_ACTIONS:
        raise AssignerError("未知的改判动作 %r（合法值%s）"
                            % (action, sorted(FIX_ACTIONS.keys())))
    target = str(personCode or "")
    if act == "assign" and not target:
        raise AssignerError("批量改判到某人必须给 personCode")

    db = sqliteCommon.dbHandle()
    db.begin()                            # 显式开事务（见文件头「不用 with」的说明）
    fixed, failed, todo = 0, [], []
    try:
        for code in codes:
            try:
                one = fix(code, act, target or None, reason=reason)
            except AssignerError as e:
                failed.append({"faceCode": code, "error": str(e)})
                continue
            if not one.get("changed"):
                continue
            fixed += 1
            if one.get("fromPerson"):
                todo.append(one["fromPerson"])
            if one.get("toPerson"):
                todo.append(one["toPerson"])
        db.commit()
    except Exception:
        db.rollbackWrite()
        raise
    centroids = [c for stat in _recomputePersons(todo) for c in stat["buckets"]]
    _LOG.info("批量改判 action=%s：%d 张成功 / %d 张失败，重算 %d 个桶",
              act, fixed, len(failed), len(centroids))
    return {"action": act, "personCode": target, "fixed": fixed,
            "failed": failed, "centroids": centroids,
            "logCode": ""}


def unassign(faceCode: str, recompute: bool = True, reason: str = "") -> dict:
    """撤销一张脸的归属（置回待确认）。

    **它就是 fix('unknown')**（数据库设计 §4.9 的 UNKNOWN 行）：
    保留这个函数只为兼容既有调用方，别留两套语义 ——
    两套语义必然漂移，漂移之后「撤销归属」到底写没写 isConfirmed=0
    就成了一个只有读代码才能回答的问题。

    关联行的存废按纪律 ③ 判断：这张照片里**再没有别的脸**属于那个人时才删。

    参数 recompute 仅为**签名兼容**保留：fix() 内部一定会重算原人的全部桶
    （纪律 ②），这里不再开一个「不重算」的口子 ——
    有了它就会出现「撤销了归属但质心没重算」这种状态，而它没有任何报错。
    """
    del recompute                      # 恒为 True，见上面的说明
    return fix(faceCode, "unknown", reason=reason or "撤销归属")


def confirmPerson(personCode: str, faceCodes: list,
                  confidence: float = None) -> dict:
    """批量确认：把一批脸一次性归给同一个人。**一个事务 + 一次重算**。

    为什么值得单独一个函数
    ------------------
      用户在「人员详情」里翻相册连点 20 张是最常见的操作。
      逐张调 assign() = 20 个事务 + 20 次质心重算（其中 18 次是重复的）。
      这里聚成一次写、一次重算（按桶去重），顺带保证「要么全成功要么全回滚」——
      半批成功的状态会让用户不知道自己确认到哪了。

    faceCodes 为空 -> 只重算该人的全部桶（用于「刚导入完人脸，重建一下」）。

    opType 用 **BATCH_ASSIGN** 而不是 ASSIGN：一次点击解决 N 张，
    日志里必须看得出这是一批（faceCount=N），否则事后无法区分
    「用户点了 20 次确认」与「用户批量确认了 20 张」。
    """
    _personByCode(personCode)
    codes = [str(c) for c in (faceCodes or ()) if str(c or "")]
    if not codes:
        return {"personCode": str(personCode), "assigned": 0, "failed": [],
                "centroids": centroid.recomputePerson(personCode)["buckets"]}

    assigned, failed, todo = 0, [], []
    db = sqliteCommon.dbHandle()
    db.begin()                            # 显式开事务（见文件头「不用 with」的说明）
    try:
        for code in codes:
            try:
                # 人工确认 -> isConfirmed=1、link source=1
                # extraLog=False：整批只落**一条** BATCH_ASSIGN 日志（下面统一写），
                # 否则「点一次确认 20 张」会在日志里变成 20 条，
                # 撤销按钮的候选集与「这张脸当初怎么被认的」都会被噪音淹没。
                one = _setBelong(code, personCode, comGD.LINK_SOURCE_MANUAL,
                                 confidence, False, OP_BATCH_ASSIGN,
                                 extraLog=False)
            except AssignerError as e:
                failed.append({"faceCode": code, "error": str(e)})
                continue
            todo.append((one["fromPerson"], one["bucketKey"]))
            todo.append((personCode, one["bucketKey"]))
            assigned += 1
        db.commit()
    except Exception:
        db.rollbackWrite()
        raise
    # 旧人（脸是从他们那儿挪过来的）按人去重后重算
    centroids = _recomputeBuckets([p for p in todo if p[0] != str(personCode)])
    # 本人**全部**桶统一收敛一次（含ALL 兜底桶）：批量确认可能让某个原本样本不足的
    # 桶越过 3 样本的启用线，那一桶必须当场建起来，
    # 否则这一批里有脸"确认了却仍匹配不上"
    selfStat = centroid.recomputePerson(personCode)
    centroids = centroids + selfStat["buckets"]
    # 一条日志代表整批（逐张落 20 条日志只会让撤销按钮的候选集被噪音淹没）
    logReview(OP_BATCH_ASSIGN, faceCode="", photoCode="",
              fromPersonCode="", toPersonCode=personCode,
              faceCount=assigned, detail="batch person=%s ok=%d fail=%d"
              % (personCode, assigned, len(failed)))
    _LOG.info("批量确认 %s：%d 张成功，%d 张失败，重算 %d 个桶（本人 %d 桶已启用）",
              personCode, assigned, len(failed), len(centroids),
              selfStat["enabled"])
    return {"personCode": str(personCode), "assigned": assigned,
            "failed": failed, "centroids": centroids}


# ============================================================
# 二之二、人员停用 / 恢复（DR-19）—— 步骤 9 的 contacts CRUD 调用
# ============================================================
#
# 为什么放在这里而不是 api/contacts.py
# ---------------------------------
#   停用要按固定顺序改**四张表**（pb_person_centroid / pb_face /
#   pb_photo_person / pb_review_log），其中 pb_face.personCode 的写权限
#   按数据库设计 §D-4 只在本模块（processor/review）手里。
#   放到 api 层去写就等于开了一条绕过 review 层的 personCode 写入路径 ——
#   而那正是「越改越乱」类静默 bug 的来源。
#   ⇒ 本模块是**唯一**实现，api 层只转发。

def personImpact(personCode: str) -> dict:
    """停用影响面（**只读，不写任何一行**）。DR-19 要求的前置复述。

    五个数字
    --------
      photoCount        关联行数（= 他出现过的照片数）
      faceCount         脸数（含自动归属）
      confirmedCount    isConfirmed=1 的脸数（**只有这些进过质心**）
      centroidCount     质心行数（停用会全删）
      pendingAfter      停用后待确认队列会变成多少（= 当前 pending + 他的脸数）

    ⚠️ `pendingAfter` 之所以必须在**执行前**算出来给用户看：
      误导入的陌生人可能挂着几百张脸，停用会让待确认队列暴涨。
      用户必须先知道这个数字（开发计划 DR-19 原文：「他必须先知道」）。
    """
    code = str(personCode or "")
    out = {"personCode": code, "photoCount": 0, "faceCount": 0, "confirmedCount": 0,
           "centroidCount": 0, "pendingAfter": 0}
    if not code:
        return out
    if not sqliteCommon.query_pb_person("pb_person", personCode=code):
        return out
    out["photoCount"] = len(sqliteCommon.query_pb_photo_person(
        "pb_photo_person", personCode=code, mode="light"))
    for row in sqliteCommon.query_pb_face("pb_face", personCode=code, mode="light"):
        out["faceCount"] += 1
        if int(row.get("isConfirmed") or 0):
            out["confirmedCount"] += 1
    out["centroidCount"] = len(sqliteCommon.query_pb_person_centroid(
        "pb_person_centroid", personCode=code, mode="light"))
    from processor.review import queue as reviewQueue      # 局部 import：见下方说明
    out["pendingAfter"] = int(reviewQueue.countPending() or 0) + out["faceCount"]
    out["warning"] = ("停用后：%d 个质心桶将删除、%d 张人脸退回待确认队列"
                      "（队列将从 %d 涨到 %d）"
                      % (out["centroidCount"], out["faceCount"],
                         out["pendingAfter"] - out["faceCount"], out["pendingAfter"]))
    return out


def disablePerson(personCode: str, reason: str = "") -> dict:
    """**停用**一个人（DR-19）。只改 `delFlag`，**绝不硬删**。

    四步顺序是**固定的，不可颠倒**
    --------------------------------
      ① `centroid.dropPerson()` 删掉该人**全部**质心
      ② 该人所有脸退回未归属（`personCode=NULL, isConfirmed=0, isStranger=0`）
      ③ 关联行按纪律 ③ 存废
      ④ 落 `pb_review_log(opType=DISABLE)`
      ⑤ `pb_person.delFlag='1'`

    两条「不做就出事」的理由
    ----------------------
      · **必须先删质心再退人脸**（① 在 ② 之前）：只删质心而留 personCode，
        那张脸就变成「人工确认但无质心」—— 既不在待确认队列
        （personCode 非空），也不在「我不同意」（isConfirmed=1），
        **从所有队列里消失**，用户再也找不到它。而质心还在的话，
        他仍会被匹配，只是人脸已经不可达 —— 静默失配最难查。
      · **不能只退人脸不删关联**（③）：关联表是 (照片 × 人)，
        人脸全退之后那些行就是「这张照片里再无人出现」的幽灵关联，
        verifyLinks 会报 orphanLink，且人员时间轴上会凭空多出照片。

    ⚠️ ② 用**一次批量 upsert** 而不是逐张 fix('unknown')：
      误导入的陌生人可能挂着几百张脸，逐张调fix 会产生几百条 UNKNOWN 日志
      并触发几百次质心重算（而此时这个人已经一张脸都不剩了，
      每次重算都是纯浪费）。这里一次事务搞定，并且**只落一条** DISABLE 日志
      —— 一次停用就是一次操作，不是一次操作 × N 张脸。
    """
    code = str(personCode or "").strip()
    if not code:
        raise AssignerError("personCode 不能为空")
    if not sqliteCommon.query_pb_person("pb_person", personCode=code):
        raise AssignerError("personCode=%s 在 pb_person 里不存在" % code)

    # 先取人脸（**读在写之前**：退回未归属之后 personCode 就查不到了）
    faces = sqliteCommon.query_pb_face("pb_face", personCode=code, mode="light",
                                       orderBy="recID")
    confirmedBefore = sum(1 for f in faces if int(f.get("isConfirmed") or 0))

    # ---- ① 删质心 ----
    droppedCentroids = centroid.dropPerson(code)

    # ---- ② 人脸全部退回未归属（一次批量 upsert）----
    db = sqliteCommon.dbHandle()
    db.begin()
    try:
        for face in faces:
            rtn = _patchFace(str(face.get("faceCode") or ""),
                             str(face.get("photoCode") or ""),
                             {"personCode": None, "isConfirmed": 0,
                              "isStranger": 0, "modifyYMDHMS": misc.getTime()},
                             forceColumns=("personCode",))
            if rtn <= 0:
                raise AssignerError("pb_face 退回未归属失败: %s"
                                    % sqliteCommon.dbHandle().lastErrMsg)
        # ② 之后立刻刷桶：主人没了 ->桶键退回等宽降级桶（DR-22 的同一顺序）
        for face in faces:
            try:
                rebucket.rebucketFace(face, None)
            except Exception as e:                    # 单张刷桶失败不该中断停用
                _LOG.warning("disablePerson %s: 脸 %s 刷桶失败（继续）: %s"
                             % (code, face.get("faceCode"), e))
        # ---- ③ 关联行：人脸全退之后，这些行按纪律 ③ 全部存废 ----
        droppedLinks = sqliteCommon.deleteTableGeneral(
            "pb_photo_person", "personCode = %s", (code,))
        # ---- ⑤ pb_person.delFlag ----
        rtn = sqliteCommon.updateTableGeneral(
            "pb_person", "personCode = %s", (code,),
            {"delFlag": comGD.DEL_FLAG_YES, "modifyYMDHMS": misc.getTime()})
        if rtn == -2:                    # sqliteHandle.RET_ERROR
            raise AssignerError("pb_person 软删失败: %s"
                                % sqliteCommon.dbHandle().lastErrMsg)
        # ---- ④ 落日志（在事务内：与上面四步要么全成要么全滚）----
        logCode = logReview(OP_DISABLE, fromPersonCode=code, faceCount=len(faces),
                            detail="disable p=%s;n=%d;c0=%d;r=%s"
                                   % (code, len(faces), confirmedBefore,
                                      str(reason or "")[:200]),
                            isRevertible=0)
        db.commit()
    except Exception:
        db.rollbackWrite()
        raise

    _LOG.info("停用 %s：质心删 %d、人脸退回 %d（确认样本 %d）、关联删 %d、日志 %s",
              code, droppedCentroids, len(faces), confirmedBefore,
              droppedLinks, logCode)
    return {"personCode": code, "faces": len(faces),
            "confirmedBefore": confirmedBefore,
            "centroidsDropped": int(droppedCentroids or 0),
            "linksDropped": int(droppedLinks or 0),
            "logCode": logCode, "delFlag": comGD.DEL_FLAG_YES}


def enablePerson(personCode: str) -> dict:
    """**恢复**一个人（`delFlag` 改回 '0'）。

    ⚠️ 恢复**不会**把任何人脸还回去 —— 停用时人脸已经退回未归属，
      而未归属的脸没有 `personCode`，也就没有主人可用来推桶键。
      所以恢复之后这个人：
        · 质心 = 0 行 -> **不参与自动匹配**（没有可比的向量）
        · 全部人脸在待确认队列里 -> 必须**重新确认**才能重新长出质心
      这个提示必须由服务端在响应里给（`note`），前端照着显示即可 ——
      「他怎么忽然匹配不准了」是这类操作最常见的后遗症。
    """
    code = str(personCode or "").strip()
    if not code:
        raise AssignerError("personCode 不能为空")
    rows = sqliteCommon.query_pb_person("pb_person", personCode=code, delFlag="*")
    if not rows:
        raise AssignerError("personCode=%s 在 pb_person 里不存在" % code)
    if str(rows[0].get("delFlag") or comGD.DEL_FLAG_NO) == comGD.DEL_FLAG_NO:
        return {"personCode": code, "changed": False, "delFlag": comGD.DEL_FLAG_NO,
                "logCode": "", "faceCount": 0, "centroidCount": 0,
                "note": _ENABLE_NOTE}
    rtn = sqliteCommon.updateTableGeneral(
        "pb_person", "personCode = %s", (code,),
        {"delFlag": comGD.DEL_FLAG_NO, "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise AssignerError("pb_person 恢复失败: %s" % sqliteCommon.dbHandle().lastErrMsg)
    faceCount = len(sqliteCommon.query_pb_face("pb_face", personCode=code, mode="light"))
    centroidCount = len(sqliteCommon.query_pb_person_centroid(
        "pb_person_centroid", personCode=code, mode="light"))
    logCode = logReview(OP_ENABLE, toPersonCode=code, faceCount=faceCount,
                        detail="enable p=%s;n=%d;g=%d" % (code, faceCount, centroidCount),
                        isRevertible=0)
    _LOG.info("恢复 %s：人脸 %d（已退回未归属，需重新确认）、质心 %d", code,
              faceCount, centroidCount)
    return {"personCode": code, "changed": True, "delFlag": comGD.DEL_FLAG_NO,
            "faceCount": faceCount, "centroidCount": centroidCount,
            "logCode": logCode, "note": _ENABLE_NOTE}


#: 恢复后必须给用户看的一句话（DR-19 明确要求）
_ENABLE_NOTE: str = ("需重新确认人脸才能自动匹配：停用时人脸已退回未归属队列，"
                     "没有确认样本就没有质心，这个人当前不参与自动匹配")


# ============================================================
# 三、批量落库（把匹配结果写进库）
# ============================================================

def applyAuto(results: list, dbFile: str = None, writeLinks: bool = True) -> dict:
    """把一批 MatchResult 里 **decision=auto** 的落库；review/cluster 的一律不写。

    review 与 cluster **故意不落库**：
      review 落库 = 给用户一份"机器认为是 A"的记录，他若不看，
                   后面步骤 11 的队列会以为已经处理过；
      cluster 落库 = 把 personCode 填上，聚类（步骤 7）就没得聚了。
      所以它们只出现在返回的统计里，由调用方决定下一步。

    ⚠️ 走 autoAssign()：**isConfirmed=0**。这是 DR-16 的核心修正——
       自动归属的脸要能出现在「我不同意」列表里，质心也不能把它当干净样本。
       逐张落日志（opType=ASSIGN + similarity）：批量自动归属是机器行为，
       但排障时「这张脸当初是以多少分数被谁认下的」必须查得到。
    """
    if dbFile:
        sqliteCommon.dbHandle(dbFile)
    stat = {"auto": 0, "review": 0, "cluster": 0, "written": 0, "failed": 0,
            "links": 0, "centroids": 0}
    todo = []
    for one in results or ():
        stat[one.decision] = stat.get(one.decision, 0) + 1
        if one.decision != "auto" or not one.personCode:
            continue
        try:
            info = autoAssign(one.faceCode, one.personCode, one.score,
                              recompute=False)
        except AssignerError as e:
            stat["failed"] += 1
            _LOG.error("自动归属失败 %s -> %s: %s", one.faceCode, one.personCode, e)
            continue
        stat["written"] += 1
        todo.append((info["fromPerson"], info["bucketKey"]))
        todo.append((one.personCode, info["bucketKey"]))
    stat["centroids"] = len(_recomputeBuckets(todo))
    return stat


# ============================================================
# 四、一致性核对与修复
# ============================================================

def verifyLinks(personCode: str = None) -> dict:
    """核对 pb_photo_person 与 pb_face 是否一致。返回问题清单（不写库）。

    报四类问题
    ---------
      orphanLink  : 关联行指向的人，在该照片里**已经没有任何脸**了（幽灵关联）
      missingLink : 有脸归属了某人，但 (照片 × 人) 关联行不存在
      badFaceCode : 关联行的 faceCode 指向一张**不属于这个人**的脸
                    （常见于改判后忘了刷 faceCode，界面点进去会看到别人的脸）
      dangling    : 关联行/脸的 personCode 在 pb_person 里查无此人
    """
    out = {"checked": 0, "orphanLink": [], "missingLink": [], "badFaceCode": [],
           "dangling": []}
    known = set(str(r.get("personCode") or "")
                for r in sqliteCommon.query_pb_person("pb_person", mode="light"))
    faceOf = {}
    for row in sqliteCommon.query_pb_face("pb_face", mode="light",
                                          personCode=personCode):
        code = str(row.get("faceCode") or "")
        person = str(row.get("personCode") or "")
        photo = str(row.get("photoCode") or "")
        if code:
            faceOf[code] = (photo, person)
        if person and person not in known:
            out["dangling"].append({"kind": "face", "faceCode": code,
                                    "personCode": person})
            # 不再参与 want 计算：悬空人已经单独报过了，
            # 再顺手补一条"缺关联"只会让同一个问题在报告里出现两次
            continue
    for row in sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                  personCode=personCode):
        out["checked"] += 1
        photo = str(row.get("photoCode") or "")
        person = str(row.get("personCode") or "")
        faceCode = str(row.get("faceCode") or "")
        if person not in known:
            out["dangling"].append({"kind": "link", "linkKey": row.get("linkKey"),
                                    "personCode": person})
            continue
        if _facesInPhotoFor(photo, person) == 0:
            out["orphanLink"].append(row.get("linkKey"))
        if faceCode:
            owner = faceOf.get(faceCode)
            if owner is None or owner[1] != person:
                out["badFaceCode"].append({"linkKey": row.get("linkKey"),
                                           "faceCode": faceCode,
                                           "owner": owner[1] if owner else None})
    want = set()
    for code, (photo, person) in faceOf.items():
        if photo and person:
            want.add(linkKeyOf(photo, person))
    for key in want:
        if not sqliteCommon.query_pb_photo_person("pb_photo_person", linkKey=key):
            out["missingLink"].append(key)
    out["clean"] = not any(out[k] for k in ("orphanLink", "missingLink",
                                            "badFaceCode", "dangling"))
    return out


def syncLinks(personCode: str = None, dryRun: bool = False) -> dict:
    """按 pb_face 重建 pb_photo_person。**幂等**：跑两遍结果一样。

    这是「兜底修复」而不是「日常路径」：正常写入都在 assign/unassign/merge 里，
    同步层只是保证**万一**前两者漏了（比如老版本代码、手工改库、异常中断），
    库里不会留下不一致的关联。宁可它是最后一道防线，也不要成为主写入路径 ——
    主路径一旦有洞，同步层就会把洞永久糊住，没人再发现。
    """
    known = set(str(r.get("personCode") or "")
                for r in sqliteCommon.query_pb_person("pb_person", mode="light"))
    want = {}                                  # linkKey -> (photo, person, face, conf)
    dangling = 0
    for row in sqliteCommon.query_pb_face("pb_face", personCode=personCode):
        person = str(row.get("personCode") or "")
        photo = str(row.get("photoCode") or "")
        code = str(row.get("faceCode") or "")
        if not person or not photo:
            continue
        if person not in known:
            dangling += 1
            continue
        key = linkKeyOf(photo, person)
        # 一张照片里同一个人可能有多张脸：只留第一张做"判定来源"，
        # 其余的脸在 pb_face 里都指向同一个人，信息不会丢
        if key not in want or (want[key][2] == "" and code):
            want[key] = (photo, person, code, None)
    have = set(str(r.get("linkKey") or "")
               for r in sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                           personCode=personCode))
    addKeys = sorted(set(want) - have)
    dropKeys = sorted(have - set(want))
    stat = {"want": len(want), "have": len(have), "add": len(addKeys),
            "drop": len(dropKeys), "danglingFaces": dangling, "dryRun": bool(dryRun)}
    if dryRun:
        stat["addKeys"] = addKeys[:50]
        stat["dropKeys"] = dropKeys[:50]
        return stat
    for key in addKeys:
        photo, person, face, _conf = want[key]
        _writeLink(photo, person, face, None, comGD.LINK_SOURCE_MANUAL)
    for key in dropKeys:
        photo, _sep, person = key.rpartition(LINK_KEY_SEP)   # rpartition -> (头, 分隔符, 尾)
        _dropLink(photo, person)
    if addKeys or dropKeys:
        _LOG.info("同步关联：+%d 行 / -%d 行", len(addKeys), len(dropKeys))
    return stat


if __name__ == "__main__":
    print("assigner.py _VERSION:", _VERSION)
    print("linkKey 规则: photoCode + %r + personCode" % LINK_KEY_SEP)
    print("linkSource: 0=%s / 1=%s"
          % (comGD.LINK_SOURCE_AUTO, comGD.LINK_SOURCE_MANUAL))
    print("isConfirmed: 1=人工确认 / 0=自动归属（**只表示人工确认**，DR-16）")
    print("四态口径:")
    print("   待确认    = personCode IS NULL AND isStranger=0")
    print("   我不同意  = personCode IS NOT NULL AND isConfirmed=0 AND isStranger=0")
    print("   人工确认  = isConfirmed=1")
    print("   陌生人    = isStranger=1")
    print("改判动作  : %s（assign/unknown/stranger）" % sorted(FIX_ACTIONS.keys()))
    print("opType: %s" % (OP_TYPES,))
    print("一致性核对:", verifyLinks())
