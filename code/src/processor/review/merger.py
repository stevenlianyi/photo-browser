#! /usr/bin/env python3
#encoding: utf-8

#Filename: merger.py
#Description: photo-browser 人员合并 / 拆分 / 撤销（步骤 6 + 修正步骤 R）
#
# merge(fromPerson, toPerson)  两人本来就是一个人，合并档案与人脸
# split(faceCode, newPersonCode) 把一张脸从某人身上摘下来（另立新档 / 置为未归属）
# undo(logCode)                撤销一条合并 / 拆分（**唯一**的逆操作入口）
#
# 要搬的表（数据库设计.md §4.5–4.7 的弱外键全图）
# -------------------------------------------------
#   pb_face.personCode          脸归谁
#   pb_person_centroid          (人, 桶) 质心 —— **删干净后按目标人重算**，
#                               不是把 from 的行改个 personCode（见下）
#   pb_photo_person             (照片 × 人) 关联 —— linkKey 里含 personCode，
#                               所以**必须重算 key**，改列会撞 UNIQUE
#   pb_person.avatarFaceCode    头像指向一张具体脸，合并后要选一张仍然有效的
#   pb_person_category          分类关系（弱外键，漏了就成孤儿）
#   pb_person.delFlag           from 软删（**不硬删**：审计与"我合并错了"的回退）
#   pb_review_log               **每次合并/拆分落一条 isRevertible=1 的日志**
#                               （没有它就回不去，见下）
#
# 五条容易做错、做错了还不报错的地方
# ----------------------------------
#   ① **质心不能靠改 personCode 搬**。两个原因：
#      (a) (personCode, bucketKey) 上有 UNIQUE，两人在同一个桶上就撞；
#      (b) 两人的 birthday 可能不同 -> 桶键体系不同 ->
#          from 的 "1990-1999" 和 to 的 "2000-2009" 语义不是一回事，
#          硬拼在一起就是一个"横跨两个年龄段"的质心。
#      正解：删掉 from 的全部质心，按 to **现有的脸**重算。
#   ② **linkKey 必须重算**。linkKey = photoCode:personCode 是 UNIQUE 幂等键，
#      把 from 的行 personCode 改成 to 之后，它会与 to 已有的同一张照片的行撞 UNIQUE；
#      而"两张照片各自被两人认过"这种情形必须收敛成 1 行，不是 2 行。
#      收敛规则：**to 已有的行保留**（to 的证据优先），from 的行删除。
#   ③ **关联行不是"脸"的行**。合并后要按 to 重新推导一遍：
#      某张照片里 to 本来就有人脸、from 迁移过来也有人脸 -> 1 行，faceCode 保留 to 的。
#      反过来，from 的人脸全部迁走后，from 的关联行必须消失，否则是幽灵关联。
#   ④ **合并/拆分必须留可撤销的日志**（DR-16④）。理由不是"操作员手滑"：
#      合并会把两个人的历史揉成一个，**质心一旦被污染，靠重算回不到原值**
#      （重算只能反映"现在这些脸"，而"哪些脸本来属于谁"这个信息一旦丢了
#      就再也拿不回来）。所以：
#        · 一次合并/拆分 = 一条 opType=MERGE/SPLIT、isRevertible=1 的**主日志**
#          （faceCode 为空、faceCount=N，带 detail）；
#        · 再加**每张脸一条**成员日志（faceCode 填上，detail 里记 op=<主日志码>
#          与这张脸操作前的 isConfirmed）。
#      成员日志不是为了好看：撤销时必须**精确知道哪些脸要搬回去**，
#      而"合并前 from 有哪些脸"这个事实只存在于日志里。
#   ⑤ **迁移 personCode 之后必须刷 shotBucket**（DR-22 / 修正步骤 R2）。
#      桶键是「拍摄年 + **这个人**的出生年」算出来的，换了主人就得重算：
#        · merge()  -> 按**目标人**的 birthday 刷全部迁移的脸
#                      （源与目标的生日可能不同 -> 桶键体系不同）
#        · undo()   -> 按**还原后的主人**刷（撤销合并就是把脸还给from，
#                      from 的生日与 to 不同，所以刷的方向与合并时相反）
#        · split()  -> 走 assigner.setBelong / unassign，由assigner 刷
#                      （新建档案的生日可能为空 -> 刷成等宽降级桶）
#      顺序固定：**搬 personCode -> 刷 shotBucket -> 重算质心**，不可颠倒。
#
# 事务纪律
# --------
#   全程一个事务：中途失败就整体回滚，绝不留下"脸搬了、关联没搬"的中间态。
#   刻意用 db.begin()/commit()/rollbackWrite() 而**不是** `with db.transaction()`：
#   后者内层块退出时会 commit，把外层事务提前结束（sqliteHandle.begin 只判
#   in_transaction，嵌套是"看起来能跑、实际上不原子"的经典陷阱）。
#   代价：merge 不可在别的事务里调用，调用方需自己保证。

import os
import sys

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
from processor.review import assigner as assigner                 # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("merger", "merger.log")

#: 合并后 fromPerson 的 delFlag（软删，不硬删）
_MERGED_FLAG: str = comGD.DEL_FLAG_YES

#: 软删时在 memo 里留的痕迹（**只在 memo 为空时写**，不覆盖用户自己写的备注）
_MERGE_MEMO: str = "已合并"


class MergeError(Exception):
    """合并/拆分失败（已回滚）。"""


def _personByCode(personCode: str) -> dict:
    rows = sqliteCommon.query_pb_person("pb_person", personCode=personCode)
    if not rows:
        raise MergeError("personCode=%s 在 pb_person 里不存在" % personCode)
    return rows[0]


def _personMaybe(personCode: str) -> dict:
    """同 _personByCode，但**查不到返回 None**（刷桶时"未归属"就是None）。

    为什么不复用 _personByCode：撤销时fromPerson 可能为空（拆成未归属），
    或者那个人已经被软删/硬删了 —— 这两种情况下刷等宽降级桶才是对的，
    不该让整个撤销崩掉。
    """
    code = str(personCode or "")
    if not code:
        return None
    # ⚠️ delFlag="*" 是必须的：撤销合并时 fromPerson **还处于软删状态**
    #    （delFlag 的还原在后面那一步），而生成层的 query_* 默认只查
    #    delFlag='0' —— 不加这个参数就会查不到人，于是刷桶退化成
    #    「未归属 -> 等宽降级桶」，撤销合并后的桶键**刷不回去**。
    for row in sqliteCommon.query_pb_person("pb_person", personCode=code,
                                            mode="light", delFlag="*"):
        return row
    return None


def _updatePerson(personCode: str, dataSet: dict) -> int:
    row = _personByCode(personCode)
    dataSet = dict(dataSet)
    dataSet.setdefault("modifyYMDHMS", misc.getTime())
    rtn = sqliteCommon.update_pb_person("pb_person", int(row["recID"]), dataSet)
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise MergeError("pb_person 更新失败: %s" % sqliteCommon.dbHandle().lastErrMsg)
    return rtn


def _faceCodesOf(personCode: str) -> list:
    return [str(r.get("faceCode") or "")
            for r in sqliteCommon.query_pb_face("pb_face", personCode=personCode)
            if str(r.get("faceCode") or "")]


def _faceByCode(faceCode: str) -> dict:
    rows = sqliteCommon.query_pb_face("pb_face", faceCode=faceCode)
    if not rows:
        raise MergeError("faceCode=%s 在 pb_face 里不存在" % faceCode)
    return rows[0]


def _displayNameOf(personCode: str) -> str:
    """取一个人名（写进日志 detail，便于事后看日志时不查库）。查不到就回显编码。"""
    for row in sqliteCommon.query_pb_person("pb_person", personCode=str(personCode or ""),
                                           mode="light"):
        return str(row.get("displayName") or personCode)
    return str(personCode or "")


# ============================================================
# 一之二、纠错日志（纪律 ④）
# ============================================================

def _firstFaceOf(photoCode: str, personCode: str) -> str:
    """这张照片里属于某人的第一张脸（用作关联行的 faceCode = 判定来源）。

    关联表是 (照片 × 人)，faceCode 只是「据哪张脸判的」这一条线索，
    取哪张都不影响信息完整性（其余的脸在 pb_face 里都指向同一个人）。
    """
    for row in sqliteCommon.query_pb_face("pb_face", photoCode=photoCode,
                                          personCode=personCode, mode="light"):
        return str(row.get("faceCode") or "")
    return ""


def _detail(text: str) -> dict:
    """detail 字段（VARCHAR(400)）-> dict。格式 `k=v;k=v`。

    为什么塞进 detail 而不是加列：pb_review_log 的字段是定稿的（§4.9），
    而「这张脸操作前是什么状态」是**每行不同**的信息，
    塞进一个单值列必然要么截断要么丢 —— 塞进 detail 并配一个解析函数最省事。
    解析失败一律返回空 dict：宁可当"没有附加信息"，也不让一条坏日志
    把 undo() 整个炸掉。
    """
    out = {}
    for item in str(text or "").split(";"):
        if "=" not in item:
            continue
        key, _sep, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def _logMergeOperation(opType: str, fromPerson: str, toPerson: str,
                       faces: list, faceCount: int = None,
                       extra: dict = None) -> str:
    """落「主日志 + 每脸一条成员日志」。返回主日志 logCode。

    成员日志的价值
    --------------
      撤销要**精确**知道哪些脸要搬回去。「合并前 from 有哪些脸」这个事实
      在合并完成后就只存在于日志里了（pb_face 里它们已经都指向 to）。
      把它们写进 detail 的单行会撞400 字符上限，
      所以按「一张脸一条日志」摊开 —— logCode 用 `<主码>.<faceCode>`，
      长度 = 22 + 1 + 32 = 55 < VARCHAR(64)，放得下。
    """
    opCode = assigner.newLogCode()
    head = {"op": opCode, "n": str(len(faces))}
    head.update(extra or {})
    assigner.logReview(opType, faceCode="", photoCode="",
                       fromPersonCode=fromPerson, toPersonCode=toPerson,
                       faceCount=int(faceCount if faceCount is not None else len(faces)),
                       detail=";".join("%s=%s" % (k, v) for k, v in head.items()),
                       isRevertible=1, logCode=opCode)
    for code in faces:
        info = _detailOfFaceForLog(code)
        assigner.logReview(opType, faceCode=code, photoCode=info["photoCode"],
                           fromPersonCode=fromPerson, toPersonCode=toPerson,
                           detail="op=%s;c0=%d" % (opCode, info["isConfirmed"]),
                           isRevertible=1,
                           logCode="%s.%s" % (opCode, code))
    return opCode


def _detailOfFaceForLog(faceCode: str) -> dict:
    """取一张脸写日志需要的两个事实：photoCode 与**当前** isConfirmed。"""
    try:
        row = _faceByCode(faceCode)
    except MergeError:
        return {"photoCode": "", "isConfirmed": 0}
    return {"photoCode": str(row.get("photoCode") or ""),
            "isConfirmed": int(row.get("isConfirmed") or 0)}


def _membersOf(operationLogCode: str, opType: str, fromPerson: str,
               toPerson: str) -> list:
    """把一次合并/拆分的**成员日志**捞出来（按 op 标记精确筛）。

    为什么不直接按 detail LIKE 'op=xxx%' 查
    --------------------------------------
      生成层的 query_* 只支持等值过滤（没有 LIKE、没有函数条件）。
      所以先用**有索引的等值条件**（opType + fromPersonCode + toPersonCode）
      取一个**超集**，再在 Python 里按 detail 里的 op 标记精确筛。
      这张表是"每次人工操作一行"的量级（远小于 pb_face），超集很小。
    """
    out = []
    for row in sqliteCommon.query_pb_review_log(
            "pb_review_log", opType=opType, fromPersonCode=fromPerson or "",
            toPersonCode=toPerson or ""):
        if _detail(row.get("detail")).get("op") != operationLogCode:
            continue
        out.append(row)
    return out


# ============================================================
# 一、合并
# ============================================================

def merge(fromPerson: str, toPerson: str, softDelete: bool = True) -> dict:
    """把 fromPerson 合并进 toPerson。**一个事务 + 事后一次重算**。

    参数
    ----
      fromPerson : 被合并方（会被软删）
      toPerson   : 保留方
      softDelete : True = 把 fromPerson 标 delFlag=1（保留审计与回退可能）。
                    False = 完全不动 fromPerson 那行（只搬数据），
                    用于「先看看合并结果、fromPerson 暂时还要留着」的场景。

    返回 {fromPerson, toPerson, faces, linksKept, linksDropped, categories,
          centroidsDropped, centroids}
    """
    fromCode, toCode = str(fromPerson or ""), str(toPerson or "")
    if not fromCode or not toCode:
        raise MergeError("fromPerson / toPerson 都不能为空")
    if fromCode == toCode:
        raise MergeError("不能自己合并自己（%s）" % fromCode)
    _personByCode(fromCode)
    toRow = _personByCode(toCode)

    db = sqliteCommon.dbHandle()
    db.begin()
    try:
        # ---- ① 脸 ----
        faces = _faceCodesOf(fromCode)
        photos = set()
        # 操作前每张脸的 isConfirmed：撤销时要**逐位还原**，日志就是靠它还原的
        prevConfirmed = {}
        for code in faces:
            rows = sqliteCommon.query_pb_face("pb_face", faceCode=code)
            if not rows:
                continue
            row = rows[0]
            photos.add(str(row.get("photoCode") or ""))
            prevConfirmed[code] = int(row.get("isConfirmed") or 0)
            rtn = sqliteCommon.update_pb_face("pb_face", int(row["recID"]),
                                              {"personCode": toCode,
                                               "isConfirmed": 1,
                                               "modifyYMDHMS": misc.getTime()})
            if rtn == -2:                # sqliteHandle.RET_ERROR
                raise MergeError("pb_face 迁移失败: %s"
                                 % sqliteCommon.dbHandle().lastErrMsg)
            # ⚠️ ⑤ 迁移之后**立刻**按**目标人**的 birthday 重刷 shotBucket
            #   （DR-22 / R2）。源与目标的生日可能不同 -> 桶键体系不同 ->
            #   源按自己的生日算出的「1985-1994」到了目标人这里可能是
            #   「1985-1997」。不刷的话这张脸在目标人的桶体系里是个孤儿，
            #   而质心是按**脸表里的桶键**建的 —— 于是这张脸永远不进
            #   目标人的任何一个桶，合并等于"合了但认不出来"。
            rebucket.rebucketFace(row, toRow)

        # ---- ② 质心：先删干净（纪律 ①）----
        droppedCentroids = centroid.dropPerson(fromCode)

        # ---- ③ 关联：linkKey 必须重算（纪律 ②③）----
        kept, dropped = [], []
        for link in sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                       personCode=fromCode):
            photo = str(link.get("photoCode") or "")
            newKey = assigner.linkKeyOf(photo, toCode)
            exists = sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                         linkKey=newKey)
            if exists:
                # to 本来就认领这张照片 -> 保留 to 的行，删掉 from 的行
                rtn = sqliteCommon.delete_pb_photo_person("pb_photo_person",
                                                          int(link["recID"]),
                                                          hardDelete=True)
                if rtn and rtn > 0:
                    dropped.append(link.get("linkKey"))
            else:
                rtn = sqliteCommon.updateTableGeneral(
                    "pb_photo_person", "linkKey = %s",
                    (str(link.get("linkKey")),),
                    {"linkKey": newKey, "personCode": toCode,
                     "modifyYMDHMS": misc.getTime()})
                if rtn == -2:            # sqliteHandle.RET_ERROR
                    raise MergeError("pb_photo_person 迁移失败: %s"
                                     % sqliteCommon.dbHandle().lastErrMsg)
                kept.append(newKey)
        # from 的所有脸都已迁走 => **任何还残留的 from 关联都是多余的**。
        # ③ 已经处理了「to 没有的照片」（改名搬走）与「to 已有的照片」（删掉），
        # 这里再扫一遍清掉异常残留 —— 宁可多删一条自己造出来的孤儿，
        # 也不把"这张照片里明明没人了却还挂着他"留给用户去发现。
        for link in sqliteCommon.query_pb_photo_person("pb_photo_person",
                                                       personCode=fromCode):
            rtn = sqliteCommon.delete_pb_photo_person("pb_photo_person",
                                                      int(link["recID"]),
                                                      hardDelete=True)
            if rtn and rtn > 0:
                dropped.append(link.get("linkKey"))

        # ---- ④ 分类关系（漏了就成孤儿）----
        movedCategories = []
        for row in sqliteCommon.query_pb_person_category("pb_person_category",
                                                         personCode=fromCode):
            category = str(row.get("category") or "")
            dup = False
            for exist in sqliteCommon.query_pb_person_category(
                    "pb_person_category", personCode=toCode, category=category):
                dup = True
                break
            if dup:
                rtn = sqliteCommon.delete_pb_person_category("pb_person_category",
                                                             int(row["recID"]),
                                                             hardDelete=True)
                if rtn and rtn > 0:
                    movedCategories.append(category)
                continue
            rtn = sqliteCommon.update_pb_person_category(
                "pb_person_category", int(row["recID"]),
                {"personCode": toCode, "modifyYMDHMS": misc.getTime()})
            if rtn == -2:                # sqliteHandle.RET_ERROR
                raise MergeError("pb_person_category 迁移失败: %s"
                                 % sqliteCommon.dbHandle().lastErrMsg)
            movedCategories.append(category)

        # ---- ⑤ 头像 ----
        fromRow = _personByCode(fromCode)
        avatarFrom = str(fromRow.get("avatarFaceCode") or "")
        avatarTo = str(toRow.get("avatarFaceCode") or "")
        # to 已有头像就不动（那是用户自己挑的）；to 没有就继承 from 的
        # 两者都指向的 faceCode 现在都归 to，所以**不需要**验证脸是否还属于 to
        if not avatarTo and avatarFrom:
            _updatePerson(toCode, {"avatarFaceCode": avatarFrom})

        # ---- ⑥ 家庭组：to 没有家庭归属时继承 from 的 ----
        # （不搬的话 from 软删后，这个家庭在 pb_family 的成员表里就少了一人）
        groupTo = str(toRow.get("familyGroupCode") or "")
        groupFrom = str(fromRow.get("familyGroupCode") or "")
        if not groupTo and groupFrom:
            _updatePerson(toCode, {"familyGroupCode": groupFrom})

        # ---- ⑦ from 软删 ----
        memoWritten = 0
        if softDelete:
            memo = str(fromRow.get("memo") or "")
            patch = {"delFlag": _MERGED_FLAG}
            if not memo:
                patch["memo"] = "%s -> %s" % (_MERGE_MEMO, toCode)
                memoWritten = 1
            _updatePerson(fromCode, patch)
        db.commit()
    except Exception:
        db.rollbackWrite()
        raise

    # ---- 事务外：① 按 to 现有的脸重算全部质心（纪律 ① 的另一半）----
    # ⚠️ 必须**在刷桶之后**：事务里已经按 to 的 birthday 把迁移过来的脸
    #    全部刷过一遍了（纪律 ⑤），这里重算出来的桶键才与脸表一致。
    #    顺序反了就是 DR-22 说的僵尸质心。
    rebuilt = centroid.recomputePerson(toCode)
    # ---- 事务外：② 落可撤销的日志（纪律 ④）----
    # ⚠️ 必须在事务**外**写：日志走自己的 upsert 事务，
    #    放在里面会被 merge 的 db.begin/commit 一起提交，
    #    一旦 commit 之后日志写失败，脸已经搬走却查不到"当初有谁被搬走了"——
    #    那就正好失去了撤销能力。
    opCode = _logMergeOperation(
        assigner.OP_MERGE, fromCode, toCode, faces,
        extra={"soft": "1" if softDelete else "0",
               "memo": str(memoWritten),
               "nFrom": _displayNameOf(fromCode)})
    _LOG.info("合并 %s -> %s：脸 %d 张、关联保留 %d/删除 %d、分类 %d 项、"
              "质心删 %d 重建 %d（启用 %d）、日志 %s",
              fromCode, toCode, len(faces), len(kept), len(dropped),
              len(movedCategories), droppedCentroids, len(rebuilt["buckets"]),
              rebuilt["enabled"], opCode)
    return {"fromPerson": fromCode, "toPerson": toCode, "faces": faces,
            "linksKept": kept, "linksDropped": dropped,
            "categories": movedCategories,
            "centroidsDropped": droppedCentroids, "logCode": opCode,
            "centroids": rebuilt["buckets"], "enabled": rebuilt["enabled"]}


# ============================================================
# 二、拆分
# ============================================================

def split(faceCode: str, newPersonCode: str = "", displayName: str = "",
          source: int = comGD.LINK_SOURCE_MANUAL, birthday: str = None) -> dict:
    """把一张脸从当前主人身上摘下来。

    参数
    ----
      faceCode     : 要拆的那张脸
      newPersonCode: 给值 = 归给这个人（不存在则**自动建档**）
                     空/None = 置为**未归属**（回到待确认队列 / 交给步骤 7 聚类）
      displayName  : 自动建档时的姓名（缺省 "未命名-<faceCode[:8]>"）
      birthday     : 自动建档时的生日（**直接影响分桶**，能填就填）

    为什么自动建档而不是报错
    ----------------------
      「这张脸不属于他」在用户那里的自然下一步就是「那这是谁」——
      他多半已经想好了名字。让他先去联系人页建人、再回来点一次，
      是把一次修正拆成两个页面四步操作，待确认队列会被他放弃。
      名字给不出就用「未命名-xxx」占位（步骤 7 聚类也是这个命名口径）。

    返回 {faceCode, fromPerson, toPerson, created, logCode, ...assign.fix 的字段}

    桶键（DR-22 /纪律 ⑤）
    ------------------
      本函数**自己不刷 shotBucket**，两条分支都交给 assigner：
        · newPersonCode 为空 -> assigner.unassign -> fix('unknown')
          -> 刷回**等宽降级桶**（生日不再属于任何人）
        · newPersonCode 有值 -> assigner.setBelong -> _setBelong
          -> 按**新主人**的 birthday 刷（新建档案的 birthday 可能为空，
             此时自动落到等宽降级桶，这正是我们要的）
      为什么不在这里再写一遍刷桶：刷桶规则只有 rebucket.py 一处实现，
      两处各写一遍就等于两套规则迟早分叉；而setBelong 的签名与顺序
      （personCode ->刷桶 -> 重算质心）已经由assigner 保证。

    日志（纪律 ④）
    ------------
      拆分落**一条** opType=SPLIT、**isRevertible=1** 的日志（单脸操作，
      faceCode 直接填在主日志上，不必再摊成员行），detail 里记：
        op      这次操作的标识（撤销时回填用）
        c0      拆分前这张脸的 isConfirmed（撤销要逐位还原）
        p0      拆分前的归属人（可能为空 = 本就未归属）
      撤销 = 把这张脸按 p0/c0 还原，并把**双方**质心重算。
      为什么只有拆分/合并可撤销，而「确认一张脸」不可撤销：
      确认的逆操作就是再点一次改判，状态机自己就回得去，
      而合并/拆分会**抹掉"谁本来属于谁"这个事实**，只能靠日志。
    """
    code = str(faceCode or "")
    if not code:
        raise MergeError("faceCode 不能为空")
    rows = sqliteCommon.query_pb_face("pb_face", faceCode=code)
    if not rows:
        raise MergeError("faceCode=%s 在 pb_face 里不存在" % code)
    fromPerson = str(rows[0].get("personCode") or "")
    wasConfirmed = int(rows[0].get("isConfirmed") or 0)

    if not newPersonCode:
        info = assigner.unassign(code, reason="split 置为未归属")
        info["created"] = ""
        info["logCode"] = _logSplitOperation(code, fromPerson, "", wasConfirmed)
        _LOG.info("拆分 %s：置为未归属（原属 %s，日志 %s）",
                  code, fromPerson or "(本就无)", info["logCode"])
        return info

    target = str(newPersonCode)
    if not sqliteCommon.query_pb_person("pb_person", personCode=target):
        name = str(displayName or "") or ("未命名-%s" % code[:8])
        rtn = sqliteCommon.insertManyTableGeneral(
            "pb_person",
            [{"personCode": target, "displayName": name,
              "birthday": (None if not birthday else str(birthday)),
              "avatarFaceCode": code,          # 拆出来的那张脸就是他的头像
              "source": comGD.PERSON_SOURCE_MANUAL,
              "isConfirmed": 0,                # 档案本身还没被用户确认过
              "modifyYMDHMS": misc.getTime()}],
            conflictColumns=("personCode",),
            updateColumns=("displayName", "birthday", "avatarFaceCode",
                           "modifyYMDHMS"),
            fillStandard=True, forceColumns=("birthday", "avatarFaceCode"))
        if rtn == -2:                    # sqliteHandle.RET_ERROR
            raise MergeError("pb_person 建档失败: %s"
                             % sqliteCommon.dbHandle().lastErrMsg)
        _LOG.info("拆分 %s：新建人员 %s（%s）", code, target, name)
        created = target
    else:
        created = ""
    # source 默认人工（拆分是用户动作）；isConfirmed 由 source 决定，不再硬编码
    info = assigner.setBelong(code, target, source, None, True,
                              assigner.OP_SPLIT, extraLog=False)
    info["created"] = created
    info["logCode"] = _logSplitOperation(code, fromPerson, target, wasConfirmed)
    _LOG.info("拆分 %s：%s -> %s%s（日志 %s）", code, fromPerson or "(本就无)",
              target, "（新建档案）" if created else "", info["logCode"])
    return info


def _logSplitOperation(faceCode: str, fromPerson: str, toPerson: str,
                       wasConfirmed: int) -> str:
    """落一条 opType=SPLIT、isRevertible=1 的日志，返回 logCode。"""
    opCode = assigner.newLogCode()
    assigner.logReview(assigner.OP_SPLIT, faceCode=faceCode,
                       photoCode=str(_detailOfFaceForLog(faceCode)["photoCode"]),
                       fromPersonCode=fromPerson, toPersonCode=toPerson,
                       faceCount=1,
                       detail="op=%s;c0=%d;p0=%s" % (opCode, int(wasConfirmed or 0),
                                                    fromPerson or ""),
                       isRevertible=1, logCode=opCode)
    return opCode


# ============================================================
# 二之二、撤销（DR-16④：只有 SPLIT / MERGE 可撤销）
# ============================================================

def revertibleList(limit: int = 20) -> list:
    """「可撤销且未撤销」的日志列表 = 撤销按钮的候选集（不写库）。

    口径与 §五 的部分索引 `WHERE isRevertible=1 AND revertedByLogCode IS NULL` 一致。
    """
    out = []
    for row in sqliteCommon.query_pb_review_log(
            "pb_review_log", orderBy="recID", descFlag=True,
            limitNum=int(limit or 0)):
        if int(row.get("isRevertible") or 0) != 1:
            continue
        if str(row.get("revertedByLogCode") or ""):
            continue
        out.append({"logCode": row.get("logCode"), "opType": row.get("opType"),
                    "faceCount": int(row.get("faceCount") or 0),
                    "fromPersonCode": row.get("fromPersonCode"),
                    "toPersonCode": row.get("toPersonCode"),
                    "opYMDHMS": row.get("opYMDHMS"),
                    "detail": row.get("detail")})
    return out


def undo(logCode: str) -> dict:
    """撤销一条**可撤销**的日志（只有 SPLIT / MERGE）。

    四道闸门（任一不满足就抛错，**绝不"尽力而为地做一半"**）
    ----------------------------------------------------
      ① 日志存在
      ② opType ∈ {SPLIT, MERGE}（普通确认 isRevertible=0，明确拒绝）
      ③ isRevertible = 1
      ④ revertedByLogCode IS NULL（还没被撤销过）
    为什么这么严：撤销会**搬动人脸归属**，而人脸归属是用户逐张核对过的事实。
    一次"撤销了两次"或"撤销了一条普通确认"造成的错分，
    用户只能在几百张照片里一张张找回来 —— 宁可明确报错让他重试。

    撤销做四件事（缺一不可）
    ----------------------
      ① 反向恢复 pb_face.personCode 与 isConfirmed（按日志里的 c0/p0 逐位还原）；
      ② 反向恢复 pb_photo_person 关联（按纪律 ③：这张照片里确实有人属于 P 才建行）；
      ③ **重算涉及双方的质心**（含 ALL 兜底桶）—— 不重算的话质心会停在
         「包含这些已经搬走的脸」的旧值上，而且**没有任何报错**；
      ④ 回填原记录的 revertedByLogCode，并写一条 opType=UNDO 的新日志
         （撤销本身也是一次人工操作，同样要留痕）。
    """
    code = str(logCode or "")
    if not code:
        raise MergeError("logCode 不能为空")
    rows = sqliteCommon.query_pb_review_log("pb_review_log", logCode=code)
    if not rows:
        raise MergeError("logCode=%s 在 pb_review_log 里不存在" % code)
    row = rows[0]
    opType = str(row.get("opType") or "")
    if opType not in (assigner.OP_SPLIT, assigner.OP_MERGE):
        raise MergeError("opType=%s 不可撤销（只有 SPLIT / MERGE 能撤销，"
                         "普通确认的逆操作是再点一次改判）" % opType)
    if int(row.get("isRevertible") or 0) != 1:
        raise MergeError("日志 %s 的 isRevertible=0，不可撤销" % code)
    if str(row.get("revertedByLogCode") or ""):
        raise MergeError("日志 %s 已被 %s 撤销过，不能重复撤销"
                         % (code, row.get("revertedByLogCode")))

    fromPerson = str(row.get("fromPersonCode") or "")
    toPerson = str(row.get("toPersonCode") or "")
    head = _detail(row.get("detail"))
    op = str(head.get("op") or code)

    # ---- ① 收集「要搬回去的脸 + 它们原来的 isConfirmed」----
    if opType == assigner.OP_SPLIT:
        faceCodes = [str(row.get("faceCode") or "")]
        prevConfirmed = {faceCodes[0]: int(head.get("c0") or 0)}
    else:
        members = _membersOf(op, opType, fromPerson, toPerson)
        faceCodes = [str(m.get("faceCode") or "") for m in members]
        prevConfirmed = dict((str(m.get("faceCode") or ""),
                              int(_detail(m.get("detail")).get("c0") or 0))
                             for m in members)
    faceCodes = [c for c in faceCodes if c]
    if not faceCodes:
        raise MergeError("日志 %s 找不到可撤销的成员行（faceCode 全为空）" % code)

    db = sqliteCommon.dbHandle()
    db.begin()
    try:
        restored, links, dropped, missing = 0, 0, 0, []
        touchedPhotos = set()
        for faceCode in faceCodes:
            try:
                face = _faceByCode(faceCode)
            except MergeError:
                missing.append(faceCode)
                continue
            photoCode = str(face.get("photoCode") or "")
            touchedPhotos.add(photoCode)
            # ⚠️ personCode 可能要写 NULL（拆分前的 p0 为空），必须走 upsert；
            #    但 isConfirmed / isStranger 是 NOT NULL，**必须把值真写进行里** ——
            #    只靠 forceColumns 把列名塞进 INSERT 的话，取值会是 None ->
            #    `NOT NULL constraint failed: pb_face.isConfirmed`。
            rtn, _cols = sqliteCommon.insertManyTableGeneral(
                "pb_face",
                [{"faceCode": faceCode, "photoCode": photoCode,
                  "personCode": (fromPerson or None),
                  "isConfirmed": int(prevConfirmed.get(faceCode, 0)),
                  "isStranger": 0,
                  "modifyYMDHMS": misc.getTime()}],
                conflictColumns=("faceCode",),
                updateColumns=("personCode", "isConfirmed", "isStranger",
                               "modifyYMDHMS"),
                fillStandard=True,
                forceColumns=("personCode",))
            if rtn == -2:                # sqliteHandle.RET_ERROR
                raise MergeError("pb_face 还原失败: %s"
                                 % sqliteCommon.dbHandle().lastErrMsg)
            # ⚠️ 纪律 ⑤：还原 personCode 之后**立刻**按新主人重刷 shotBucket。
            #   撤销合并 = 把脸还给 fromPerson，而 from 的生日与 to 可能不同 ->
            #   刷的方向与合并时**相反**。不刷的话这些脸留在 to 的桶体系里，
            #   而 from 的质心按 from 的桶键建 -> 这个人的匹配静默失准。
            #   fromPerson 为空（撤销的是"拆成未归属"）-> 刷回等宽降级桶。
            rebucket.rebucketFace(face, _personMaybe(fromPerson))
            restored += 1
        # ---- ② 反向恢复关联（纪律 ③ 的**双向**版本）----
        #只补 fromPerson 那一侧是不够的：撤销合并后 toPerson 在这些照片里
        #可能**一张脸都不剩**（合并时它是把 from 的脸全搬过去的），
        #而 photo:toPerson 的关联行还在 —— 那就是「这张照片里再无人出现」的
        #幽灵关联，verifyLinks 会报 orphanLink + badFaceCode。
        for photoCode in sorted(touchedPhotos):
            if not photoCode:
                continue
            if fromPerson and assigner.facesInPhotoFor(photoCode, fromPerson) > 0:
                assigner.writeLink(photoCode, fromPerson,
                                   _firstFaceOf(photoCode, fromPerson), None,
                                   comGD.LINK_SOURCE_MANUAL)
                links += 1
            elif fromPerson:
                dropped += assigner.dropLink(photoCode, fromPerson)
            if toPerson and assigner.facesInPhotoFor(photoCode, toPerson) == 0:
                dropped += assigner.dropLink(photoCode, toPerson)
        # ---- 合并撤销：把 fromPerson 的软删与合并痕迹一起还原 ----
        if opType == assigner.OP_MERGE and fromPerson:
            patch = {"delFlag": comGD.DEL_FLAG_NO}
            if str(head.get("memo") or "0") == "1":
                # memo 是合并时补的痕迹（只在原本为空时写），撤销要一起清掉
                for one in sqliteCommon.query_pb_person(
                        "pb_person", personCode=fromPerson, delFlag="*"):
                    if str(one.get("memo") or "").startswith(_MERGE_MEMO):
                        patch["memo"] = None
            rtn = sqliteCommon.updateTableGeneral(
                "pb_person", "personCode = %s", (fromPerson,), patch)
            if rtn == -2:                # sqliteHandle.RET_ERROR
                raise MergeError("pb_person 还原失败: %s"
                                 % sqliteCommon.dbHandle().lastErrMsg)
        # ---- ④ 回填 revertedByLogCode + 写一条 UNDO 日志 ----
        undoCode = assigner.newLogCode()
        rtn = sqliteCommon.updateTableGeneral(
            "pb_review_log", "logCode = %s", (code,),
            {"revertedByLogCode": undoCode, "modifyYMDHMS": misc.getTime()})
        if rtn == -2:                    # sqliteHandle.RET_ERROR
            raise MergeError("pb_review_log 回填失败: %s"
                             % sqliteCommon.dbHandle().lastErrMsg)
        assigner.logReview(assigner.OP_UNDO, faceCode="", photoCode="",
                           fromPersonCode=toPerson, toPersonCode=fromPerson,
                           faceCount=restored,
                           detail="undo=%s;op=%s;n=%d" % (code, op, restored),
                           isRevertible=0, logCode=undoCode)
        db.commit()
    except Exception:
        db.rollbackWrite()
        raise

    # ---- ③ 事务外：重算涉及双方的质心（纪律 ② 的逆操作）----
    stats = []
    for personCode in (toPerson, fromPerson):
        if personCode:
            stats.append(centroid.recomputePerson(personCode))
    _LOG.info("撤销 %s（%s）：脸还原 %d 张、关联补回 %d 行/清理 %d 行、质心重算 %d 人%s",
              code, opType, restored, links, dropped, len(stats),
              ("，%d 张脸已不在库里" % len(missing)) if missing else "")
    return {"logCode": code, "undoLogCode": undoCode, "opType": opType,
            "fromPerson": fromPerson, "toPerson": toPerson,
            "facesRestored": restored, "linksRestored": links,
            "linksDropped": dropped,
            "missingFaces": missing,
            "centroids": [c for stat in stats for c in stat["buckets"]]}


# ============================================================
# 三、拆分一个"疑似多人"的档案（合并的反向操作，批量版）
# ============================================================

def suggestSplitPersons(personCode: str, minSeparation: float = 0.35) -> dict:
    """看一个人的各桶质心彼此差多远，报告"这个档案里可能混了几个人"。

    **只报告，不动手。** 自动拆分比自动合并危险得多：
    合并错了可以再拆（数据都在），拆错了是把一个人的历史打散到几个新档案里，
    而且**用户看不出哪个才对**。
    所以这里只给线索（哪些桶彼此不像），动手的永远是用户。

    minSeparation : 桶间余弦低于该值即视为"可能不是同一个人"
                    （0.35 = 本项目 T_LOW：低于它本来就会被判成陌生人）
    """
    person = str(personCode or "")
    rows = sqliteCommon.query_pb_person_centroid("pb_person_centroid",
                                                 personCode=person)
    limit = basicSettings.MIN_CENTROID_SAMPLES
    vectors = {}
    for row in rows:
        if int(row.get("sampleCount") or 0) < limit:
            continue
        vec = centroid.centroidOf(person, str(row.get("bucketKey") or ""))
        if vec is not None:
            vectors[str(row.get("bucketKey"))] = vec
    pairs = []
    keys = sorted(vectors)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            score = float(vectors[keys[i]] @ vectors[keys[j]])
            if score < float(minSeparation):
                pairs.append({"bucketA": keys[i], "bucketB": keys[j],
                              "cosine": round(score, 4)})
    pairs.sort(key=lambda p: (p["cosine"], p["bucketA"], p["bucketB"]))
    return {"personCode": person, "buckets": keys, "farPairs": pairs,
            "minSeparation": float(minSeparation),
            "suspicious": bool(pairs)}


if __name__ == "__main__":
    print("merger.py _VERSION:", _VERSION)
    print("合并要搬的表: pb_face.personCode / pb_person_centroid / "
          "pb_photo_person / pb_person.avatarFaceCode")
    print("                + pb_person_category / pb_person.familyGroupCode / "
          "pb_person.delFlag")
    print("linkKey 重算  : photoCode:personCode（UNIQUE 幂等键，改列必撞）")
    print("质心处理      : 删 from 的全部 -> 按 to 现有的脸重算（不搬列）")
    print("纠错日志      : merge/split 落 isRevertible=1 的日志（主日志 + 每脸成员行）")
    print("可撤销且未撤销:", revertibleList())
