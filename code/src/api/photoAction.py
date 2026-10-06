#! /usr/bin/env python3
#encoding: utf-8

#Filename: photoAction.py
#Description: photo-browser 照片级写接口（步骤 11）—— 影响面 / 软删 / 恢复 / 标记重复
#
# 为什么不并进 api/browse.py
# ------------------------------------------------
#   browse.py 纪律②：**整个模块一个字都不写**（只读模型出口）。
#   这里是要写的 —— 照片级的写（软删 / 恢复 / 标记重复）必须有自己的家，
#   否则那条纪律形同虚设。而它们**不改任何一张脸的归属**，
#   所以不归 review 层（pb_review_log 是归属纠错专用的排障链，见 processor/photoAction.py 尾注）。
#
# 两条端点设计上的硬约束
# ------------------------------------------------
#   ① **软删必须两段式**（与 api/contacts.py 的 /disable 同一套做法）：
#      不带 `confirm=1` 只返回**影响面**、一行都不写；带 confirm 才执行。
#      理由：软删是「不可逆语义的批量动作」（P0-3），必须先让用户知道
#      「这张照片里的 3 张待确认人脸会一起离开队列」。
#   ② **原图零风险**：这三个动作全部只改数据库。磁盘上的 photo\ 目录
#      一个字节都不动。UI 也不提供任何编辑 / 覆盖 / 删除原图的入口。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import dto                                               # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from processor import photoAction as photoAct                    # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402

_VERSION = "20261006"

_LOG = misc.setLogNew("apiPhotoAction", "apiphotoaction.log")

router = APIRouter(tags=["photoAction"])


def _photoAnywhere(photoCode: str) -> dict:
    """含软删行地取照片（browse.photoRow 只查未删行，恢复流程要看得见软删行）。"""
    from database.auto_generated import sqliteCommon
    code = str(photoCode or "").strip()
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=code,
                                       delFlag="*", limitNum=1)
    return rows[0] if rows else {}


def _require(photoCode: str) -> str:
    """校验 photoCode 存在（含软删行），返回规范化后的编码。不存在就抛 404。"""
    code = str(photoCode or "").strip()
    if not code:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "photoCode 不能为空")
    if not _photoAnywhere(code):
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "photoCode=%s 在 pb_photo 里不存在" % code)
    return code


@router.get("/photos/{photoCode}/delete-impact",
            summary="软删除影响面（纯读，一行都不写）")
def deleteImpact(photoCode: str) -> dict:
    _require(photoCode)
    try:
        impact = photoAct.deleteImpact(str(photoCode))
    except photoAct.PhotoActionError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    # ⚠️ `impact` 自带 note（processor 层写的），这里不要再传一个同名的 ——
    #    `okBody(note=..., **impact)` 会抛「multiple values for keyword argument」
    #    （与 contacts.disable 里 personCode 那处同一个坑）。
    impact["executed"] = False
    impact["confirmRequired"] = True
    return dto.okBody(**impact)


@router.post("/photos/{photoCode}/soft-delete",
             summary="软删除（不带 confirm=true 只返回影响面）")
def softDelete(photoCode: str,
               confirm: int = Query(default=0,
                                    description="**1 才执行**；不给/0 只返回影响面")):
    """软删一张照片：pb_photo.delFlag='1' + 该照片全部人脸级联软删。

    ⚠️ **关联行 pb_photo_person 不删**：恢复时要能重建原样
       （照片里「谁出现过」是当时检测 + 人工确认的结果，重新检测未必一致）。
       人物的照片数在 personStatsOf 里 JOIN pb_photo 过滤 delFlag 自动少 1。
    ⚠️ 幂等：已是软删状态时返回 changed=false，不报错。
    """
    _require(photoCode)
    if not int(confirm):
        impact = deleteImpact(photoCode)
        impact["confirmRequired"] = True
        impact["executed"] = False
        return dto.okBody(**impact)

    try:
        result = photoAct.softDelete(str(photoCode))
    except photoAct.PhotoActionError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))

    from processor.review import queue as reviewQueue
    after = reviewQueue.countStates()
    return dto.okBody(executed=True, **result,
                      pendingCount=int(after.get("pending") or 0),
                      disputedCount=int(after.get("disputed") or 0))


@router.post("/photos/{photoCode}/restore", summary="恢复软删除（级联恢复人脸）")
def restore(photoCode: str) -> dict:
    _require(photoCode)
    try:
        result = photoAct.restore(str(photoCode))
    except photoAct.PhotoActionError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    from processor.review import queue as reviewQueue
    after = reviewQueue.countStates()
    return dto.okBody(executed=True, **result,
                      pendingCount=int(after.get("pending") or 0),
                      disputedCount=int(after.get("disputed") or 0))


@router.post("/photos/{photoCode}/mark-duplicate", summary="标记为某张照片的副本")
def markDuplicate(photoCode: str, body: dto.MarkDuplicateBody = None) -> dict:
    """把当前照片标成 `dupOfPhotoCode` 的副本。

    ⚠️ 必填 body：没有「指向谁」的重复标记是**无意义的** ——
       UI 上对应「选一张作为主照片」的对话框，不允许留空提交。
    ⚠️ **不删任何文件**：只打标记 + 建双向可查的 dupOfPhotoCode。
    """
    code = _require(photoCode)
    target = str(getattr(body, "dupOfPhotoCode", "") or "").strip()
    if not target:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "必须指定「它是谁的副本」（dupOfPhotoCode）")
    try:
        result = photoAct.markDuplicate(code, target)
    except photoAct.PhotoActionError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    return dto.okBody(executed=True, **result)


@router.post("/photos/{photoCode}/unmark-duplicate", summary="取消重复标记")
def unmarkDuplicate(photoCode: str) -> dict:
    code = _require(photoCode)
    try:
        result = photoAct.unmarkDuplicate(code)
    except photoAct.PhotoActionError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    return dto.okBody(executed=True, **result)
