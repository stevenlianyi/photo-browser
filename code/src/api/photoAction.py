#! /usr/bin/env python3
#encoding: utf-8

#Filename: photoAction.py
#Description: photo-browser 照片级写接口（步骤 11）—— 影响面 / 软删 / 恢复 / 标记重复 / 年代修正
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
#
# 年代修正（DR-42，`/photos/{photoCode}/shot-year-fix`）与上面三个的差别
# --------------------------------------------------------------------
#   · 它同样走**两段式**（不带 confirm=1 只返回影响面）：写入的是**照片级**年份，
#     而年代档是 f(有效拍摄年, 各人出生年) 现算的 —— 一张合影里三个不同生日的人
#     会朝三个方向变，必须先让用户看见这个清单（`persons[]`）。
#   · 它会**重算质心**，因此是这三个之外**唯一**落 pb_review_log 的照片级动作
#     （opType=BUCKET_FIX，可撤销）。理由：它改变了「这张脸属于哪个年代档」，
#     那是归属排障链上的事实；而软删 / 标记重复不影响归属也不影响划分年代档，
#     仍然一条日志都不落。写库逻辑在 processor/photoTimeFix.py。
#
# 旋转（DR-43，`/photos/{photoCode}/rotate` + `/rotate-reset`）—— 三处关键差别
# ------------------------------------------------------------------------
#                    软删 / 标记重复 / 年代修正      旋转
#   两段式 confirm=1 需要（有影响面 / 不可逆语义）     **不需要**
#   落 pb_review_log 软删与标记重复不落；年代修正落    **不落**
#   连带重算         年代修正是 DR-22 三步联动         **零连带**
#
#   理由（一条就够）：**旋转不改变任何识别事实**
#     · 不动 `bbox`、不动质心、不动 `shotBucket`、不动归属，
#       也不动 `pb_photo.faceCount`；
#     · 而且**完全可逆**（再转回去即可）。
#   所以它既没有「必须先让用户看见的影响面」（对照软删的两段式），
#   也不属于「归属纠错排障链」（对照年代修正的 BUCKET_FIX 日志）——
#   pb_review_log 那条链必须保持只装归属事实，塞一条旋转进去就是污染。
#   ⚠️ 别把旋转并入上面那条链：它是**纯显示属性**，与"这张脸怎么被认成这个人"
#      没有任何关系。写库逻辑在 processor/photoRotate.py。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import dto                                               # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from processor import photoAction as photoAct                    # noqa: E402
from processor import photoRotate                                 # noqa: E402
from processor import photoTimeFix                                # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402

_VERSION = "20261009"

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


# ============================================================
# 四、年代修正（DR-42）—— 老相册翻拍件 / 扫描件的「拍摄年代」
# ============================================================

@router.get("/photos/{photoCode}/shot-year-fix",
            summary="年代修正影响面（纯读；不给 shotYear = 预览「恢复自动」）")
def shotYearFixImpact(photoCode: str,
                      shotYear: int = Query(default=None,
                                            description="目标年份；省略 = 预览「恢复自动」")):
    """这张照片按新年代重新划分年代档之后会变成什么样。**一行都不写**。

    为什么要先看影响面：改的是**照片级**年份，而年代档是
    f(有效拍摄年, 各人出生年) 现算的 —— 同一张合影里三个不同生日的人
    会朝三个方向变。`persons[]` 就是「这次修正会牵动谁」的完整清单。
    """
    _require(photoCode)
    try:
        preview = photoTimeFix.previewFix(str(photoCode), shotYear)
    except photoTimeFix.PhotoTimeFixError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    preview["executed"] = False
    preview["confirmRequired"] = True
    return dto.okBody(**preview)


@router.post("/photos/{photoCode}/shot-year-fix",
             summary="修正照片年代（不带 confirm=1 只返回影响面）")
def fixShotYear(photoCode: str, body: dto.ShotYearFixBody = None,
                confirm: int = Query(default=0,
                                     description="**1 才执行**；不给/0 只返回影响面")):
    """把这张照片的年代改成 `shotYear`（**传 null = 恢复自动**）。

    落库后按 DR-22 的硬顺序联动（顺序不可颠倒）：
      ① 写 `pb_photo.shotYearOverride`
      ② `rebucket.rebucketPhoto` —— 重刷这张照片全部人脸的 shotBucket
      ③ `centroid.recomputePerson` —— 按新年代档键重建涉及人物的质心
    日志 opType=BUCKET_FIX（可撤销，入口仍是 `POST /api/review/undo`）。

    ⚠️ `shotYear` **必须显式给**（哪怕给 null）：不传是参数漏了（400），
       传 null 才是「恢复自动」。两者在 JSON 里都是缺失值/None，
       靠 `exclude_unset` 区分 —— 混在一起会让"前端漏传"变成
       "静默把用户填的修正清掉"。
    """
    code = _require(photoCode)
    given = body.model_dump(exclude_unset=True) if body is not None else {}
    if "shotYear" not in given:
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "必须给 shotYear（传 null 表示恢复自动）")
    shotYear = given.get("shotYear")
    if not int(confirm):
        preview = shotYearFixImpact(code, shotYear)
        preview["confirmRequired"] = True
        return preview
    try:
        result = photoTimeFix.applyFix(code, shotYear)
    except photoTimeFix.PhotoTimeFixError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    return dto.okBody(executed=True, **result)


# ============================================================
# 五、人工旋转（DR-43）—— 翻拍件 / 扫描件的显示方向
# ============================================================
# ⚠️ 这两个端点与上面几个的**三处差别**写在模块头（不需要 confirm、不落
#    pb_review_log、零连带）。一句话：旋转是**纯显示属性**，不改变任何识别事实。

@router.post("/photos/{photoCode}/rotate",
             summary="人工旋转显示角度（0/90/180/270，只改显示不动原图）")
def rotate(photoCode: str, body: dto.RotateBody = None) -> dict:
    """把这张照片的**显示角度**设为 `rotateDeg`（顺时针为正：90=右转 / 270=左转）。

    只改 `pb_photo.rotateDeg` 一个值 —— 原图、EXIF、缩略图缓存、人脸框、归属
    全部不变（理由见模块头那段差别表）。前端据此做一次 CSS `transform`。

    ⚠️ 幂等：同值重复提交返回 `changed=false`，不报错
       （前端"连点两次左转"就是靠这里保证不会写两次 modifyYMDHMS）。
    ⚠️ 只接受 0 / 90 / 180 / 270，其它一律 400 —— **不做 `% 360` 归约**：
       静默归约会把「前端算错了角度」这类 bug 变成看不见的。
    """
    code = _require(photoCode)
    angle = getattr(body, "rotateDeg", None) if body is not None else None
    try:
        result = photoRotate.applyRotate(code, angle)
    except photoRotate.PhotoRotateError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    return dto.okBody(executed=True, **result)


@router.post("/photos/{photoCode}/rotate-reset",
             summary="重置显示角度为 0（恢复原方向）")
def rotateReset(photoCode: str) -> dict:
    """把这张照片的显示角度写回 0（恢复扫描件自己的方向）。

    单列一个端点而不是让前端调 `rotate` 传 0：语义不同（「转到某角度」vs
    「恢复原方向」），界面上的「重置方向」按钮直接对应它；而写的是**同一列**
    （与 DR-42 的「修正 / 恢复自动」共用一个字段是同一个理由）。
    ⚠️ 幂等：已经是 0 时返回 `changed=false`。
    """
    code = _require(photoCode)
    try:
        result = photoRotate.resetRotate(code)
    except photoRotate.PhotoRotateError as e:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    return dto.okBody(executed=True, **result)
