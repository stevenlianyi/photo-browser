#! /usr/bin/env python3
#encoding: utf-8

#Filename: photoAction.py
#Description: photo-browser 照片级写操作（步骤 11）—— 软删除 / 恢复 / 标记重复
#
# 为什么单独一个模块，而不并进 api/browse.py
# ------------------------------------------------
#   browse.py 的纪律②写得很死：**整个模块一个字都不写**（只读模型出口）。
#   而软删除与标记重复必须写库。把写操作塞进 browse 会让那条纪律失效 ——
#   而纪律存在的理由是「所有归属相关的写都必须经 review 层并落 pb_review_log」。
#
#   照片级的写**不涉及人脸归属**，所以不进 review 层；但它必须**级联**处理
#   这张照片里的人脸，否则会留下「从所有队列里消失但还占着记录」的死数据。
#
# 三条不可省的纪律
# ----------------
#   ① **原图零风险**：这里只改数据库。磁盘上的 photo\ 目录一个字节都不动，
#      不删、不改名、不覆盖。软删 = pb_photo.delFlag 改 '1'（P0-5）。
#   ② **关联行不删**：pb_photo_person 的行**保留**，只在 personStatsOf 的
#      计数时 JOIN pb_photo 过滤 delFlag='0'。删行的话恢复就重建不出原样
#      （照片里「谁出现过」是当时检测 + 确认的结果，重新检测未必一致）。
#   ③ **人脸必须级联软删**：pb_face 的 delFlag 一起改 '1'。不改的话这张照片的
#      脸会继续留在待确认队列里，而用户点进去会发现照片「不存在」——
#      队列里出现一条点不开的条目，比不显示更糟。
#      恢复时一起改回 '0'（级联可逆）。
#
# 不落 pb_review_log
# -----------------
#   pb_review_log 的 opType 是**人脸归属纠错**语义（ASSIGN/FIX/UNKNOWN/
#   STRANGER/BATCH_ASSIGN/SPLIT/MERGE/UNDO/DISABLE/ENABLE）。软删一张照片
#   不改任何一张脸的归属，硬塞一条 opType 进去只会污染「这张脸当初怎么被
#   认成这个人的」这条排障链 —— 那个链子必须保持只装归属事实。
#   照片级的留痕靠 pb_photo.delFlag + modifyYMDHMS（本模块会写）。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("photoAction", "photoaction.log")


class PhotoActionError(Exception):
    """参数非法 / 状态不允许（api 层映射成 400）。"""


# ⚠️⚠️ 写 delFlag **必须走 updateTableGeneral，不能走 insertManyTableGeneral**
# ----------------------------------------------------------------------
#   这是本模块实测踩到的真坑，记在这里以免下次再踩：
#   `insertManyTableGeneral` 拼 `ON CONFLICT ... DO UPDATE SET` 时会
#   **主动剔除 delFlag**（见 auto_generated/sqliteCommon.py 里那段
#   「注册时间与软删标记不参与覆盖：否则重扫会改掉注册时间、把软删行悄悄复活」）。
#   于是 `insertManyTableGeneral("pb_photo", [{..., "delFlag": "1"}], ...)`
#   **返回 1 行受影响、看起来完全成功，而 delFlag 一字未改** ——
#   一次「静默的空操作」：不报错、返回成功、库里原封不动。
#   （与 assigner._patchFace 注释里「update_* 写 NULL 是空操作」同一个根因。）
#   实测症状：`soft-delete?confirm=1` 返回 `executed=true, facesDeleted=8`，
#   而库里 pb_photo.delFlag 仍是 '0'、那 8 张脸也还在待确认队列里。
#   正解：`updateTableGeneral(表, "photoCode = %s", (code,), {"delFlag": ...})` ——
#   纯 UPDATE，值原样进参数元组（None 就是 NULL）。assigner.disablePerson
#   写 pb_person.delFlag 用的也是它。


def _setColumns(tableName: str, keyColumn: str, keyValue: str,
                dataSet: dict) -> int:
    """按业务键改若干列。**delFlag 也能改**（见上面那条警告）。

    keyColumn 是**本文件里的白名单常量**，keyValue 与 dataSet 的值全部进参数
    元组 —— 业务层仍然不拼用户输入（纪律①）。
    """
    rtn = sqliteCommon.updateTableGeneral(
        tableName, "%s = %%s" % keyColumn, (str(keyValue),), dataSet)
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise PhotoActionError("%s 更新失败: %s"
                               % (tableName, sqliteCommon.dbHandle().lastErrMsg))
    return rtn


def _photoRow(photoCode: str) -> dict:
    """取一行 pb_photo（**含软删行** —— 恢复流程要看得见软删行）。查不到抛错。"""
    code = str(photoCode or "").strip()
    if not code:
        raise PhotoActionError("photoCode 不能为空")
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=code,
                                       delFlag="*", limitNum=1)
    if not rows:
        raise PhotoActionError("photoCode=%s 在 pb_photo 里不存在" % code)
    return rows[0]


def _facesOf(photoCode: str) -> list:
    """这张照片的全部人脸（**含软删** —— 恢复要一并放回来）。"""
    return sqliteCommon.query_pb_face("pb_face", photoCode=str(photoCode),
                                      delFlag="*", orderBy="recID")


def _isLive(face: dict) -> bool:
    """这张脸当前是否「在队列里」（未软删）。"""
    return str(face.get("delFlag") or comGD.DEL_FLAG_NO) == comGD.DEL_FLAG_NO


def _setFaceDelFlag(faceCode: str, delFlag: str) -> int:
    """改一张脸的 delFlag（同样必须走 updateTableGeneral，理由见 _setColumns）。"""
    return _setColumns("pb_face", "faceCode", str(faceCode),
                       {"delFlag": delFlag, "modifyYMDHMS": misc.getTime()})


# ============================================================
# 一、影响面（只读，一行都不写）
# ============================================================

def deleteImpact(photoCode: str) -> dict:
    """软删除影响面。

    与 contacts 的 /impact 同一个理由：不可逆语义的操作必须**先说清楚后果**
    （开发计划 P0-3「批量动作做减速」）。
    ⚠️ 纯读 —— 验收要核对调它之后库里一行都没变。
    """
    row = _photoRow(photoCode)
    live = [f for f in _facesOf(str(row.get("photoCode") or "")) if _isLive(f)]
    pending = [f for f in live
               if not str(f.get("personCode") or "")
               and not int(f.get("isStranger") or 0)]
    disputed = [f for f in live
                if str(f.get("personCode") or "")
                and not int(f.get("isConfirmed") or 0)
                and not int(f.get("isStranger") or 0)]
    confirmed = [f for f in live if int(f.get("isConfirmed") or 0) == 1]
    persons = sorted({str(f.get("personCode") or "") for f in live
                      if str(f.get("personCode") or "")})
    return {
        "photoCode": str(row.get("photoCode") or ""),
        "relPath": str(row.get("relPath") or ""),
        "fileSize": int(row.get("fileSize") or 0),
        "alreadyDeleted": str(row.get("delFlag") or comGD.DEL_FLAG_NO) == comGD.DEL_FLAG_YES,
        "faceCount": len(live),
        "pendingFaces": len(pending),
        "disputedFaces": len(disputed),
        "confirmedFaces": len(confirmed),
        "personCodes": persons,
        "destructive": False,
        "note": ("软删除 = 只把库里的记录标记为已删除，**磁盘上的原图不会被删或改动**；"
                 "人脸会一并从待确认 / 我不同意两个队列里移出（可恢复）"),
    }


# ============================================================
# 二、软删除 / 恢复
# ============================================================

def softDelete(photoCode: str) -> dict:
    """软删一张照片（可恢复）。

    ⚠️ 幂等：已经是软删状态时直接返回，不重复改。
    """
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    if str(row.get("delFlag") or comGD.DEL_FLAG_NO) == comGD.DEL_FLAG_YES:
        return {"photoCode": code, "changed": False, "alreadyDeleted": True,
                "facesDeleted": 0, "note": "这张照片已经是软删状态（幂等）"}

    faces = [f for f in _facesOf(code) if _isLive(f)]
    if _setColumns("pb_photo", "photoCode", code,
                   {"delFlag": comGD.DEL_FLAG_YES,
                    "modifyYMDHMS": misc.getTime()}) <= 0:
        raise PhotoActionError("pb_photo 软删没有生效（photoCode=%s）" % code)
    done = 0
    for face in faces:
        if _setFaceDelFlag(str(face.get("faceCode") or ""),
                           comGD.DEL_FLAG_YES) > 0:
            done += 1
    _LOG.info("软删除 %s（%s）级联人脸 %d 张", code, row.get("relPath"), done)
    return {"photoCode": code, "changed": True, "alreadyDeleted": False,
            "facesDeleted": done,
            "note": "已软删除；磁盘原图未动，可随时恢复"}


def restore(photoCode: str) -> dict:
    """恢复一张软删照片（人脸一起回来）。幂等。"""
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    if str(row.get("delFlag") or comGD.DEL_FLAG_NO) != comGD.DEL_FLAG_YES:
        return {"photoCode": code, "changed": False, "alreadyRestored": True,
                "facesRestored": 0, "note": "这张照片不是软删状态（幂等）"}

    faces = _facesOf(code)
    if _setColumns("pb_photo", "photoCode", code,
                   {"delFlag": comGD.DEL_FLAG_NO,
                    "modifyYMDHMS": misc.getTime()}) <= 0:
        raise PhotoActionError("pb_photo 恢复没有生效（photoCode=%s）" % code)
    done = 0
    for face in faces:
        if _setFaceDelFlag(str(face.get("faceCode") or ""),
                           comGD.DEL_FLAG_NO) > 0:
            done += 1
    _LOG.info("恢复 %s 级联人脸 %d 张", code, done)
    return {"photoCode": code, "changed": True, "alreadyRestored": False,
            "facesRestored": done,
            "note": "已恢复；人脸回到原来的队列状态"}


# ============================================================
# 三、标记重复
# ============================================================

def markDuplicate(photoCode: str, dupOfPhotoCode: str) -> dict:
    """把 photoCode 标成「dupOfPhotoCode 的副本」（可撤销）。

    为什么要有这个手动入口
    ----------------------
      扫描器只能按 fileHash 认出**内容完全一致**的复制（步骤 3）。
      但用户真正想标的是「同一件事的第二张」——重新压缩过、裁剪过、
      连拍里构图几乎一样的两张，fileHash 不同，扫描器认不出来。
      界面上给一个「标记重复」入口，是让用户补上机器认不出的那部分。
    ⚠️ 不做物理去重、不删任何文件：只是打一个标记，
       photos 流里带 isDuplicate 角标可筛出来。
    """
    code = str(photoCode or "").strip()
    target = str(dupOfPhotoCode or "").strip()
    if not code:
        raise PhotoActionError("photoCode 不能为空")
    if not target:
        raise PhotoActionError("必须指定「它是谁的副本」（dupOfPhotoCode）")
    if code == target:
        raise PhotoActionError("不能把一张照片标成它自己的副本")
    row = _photoRow(code)
    origin = _photoRow(target)
    if str(origin.get("delFlag") or comGD.DEL_FLAG_NO) == comGD.DEL_FLAG_YES:
        raise PhotoActionError("被指向的那张照片已被软删，请先恢复或换一张")

    if _setColumns("pb_photo", "photoCode", code,
                   {"isDuplicate": 1, "dupOfPhotoCode": target,
                    "modifyYMDHMS": misc.getTime()}) <= 0:
        raise PhotoActionError("pb_photo 标记重复没有生效（photoCode=%s）" % code)
    _LOG.info("标记重复 %s -> %s", code, target)
    return {"photoCode": code, "changed": True, "isDuplicate": 1,
            "dupOfPhotoCode": target,
            "dupOfRelPath": str(origin.get("relPath") or ""),
            "sameFileHash": str(row.get("fileHash") or "") == str(origin.get("fileHash") or ""),
            "note": "已标记为重复；**不删除任何文件**，随时可以取消标记"}


def unmarkDuplicate(photoCode: str) -> dict:
    """取消重复标记（幂等）。"""
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    if not int(row.get("isDuplicate") or 0) and not str(row.get("dupOfPhotoCode") or ""):
        return {"photoCode": code, "changed": False, "alreadyClean": True,
                "note": "这张照片本来就没有重复标记（幂等）"}
    # ⚠️ dupOfPhotoCode 要真的写成 NULL：updateTableGeneral 的值**原样**进参数
    #    元组，None 就是 SQL NULL（这与 update_pb_face 会把 None 整条丢掉不同，
    #    那个坑见 assigner._patchFace 的注释 —— 走 upsert 才会踩到）。
    if _setColumns("pb_photo", "photoCode", code,
                   {"isDuplicate": 0, "dupOfPhotoCode": None,
                    "modifyYMDHMS": misc.getTime()}) <= 0:
        raise PhotoActionError("pb_photo 取消重复没有生效（photoCode=%s）" % code)
    return {"photoCode": code, "changed": True, "isDuplicate": 0,
            "dupOfPhotoCode": None, "note": "已取消重复标记"}
