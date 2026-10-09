#! /usr/bin/env python3
#encoding: utf-8

#Filename: photoRotate.py
#Description: photo-browser 照片人工旋转（DR-43）—— pb_photo.rotateDeg 的唯一写入口
#
# 要解决的现象
# ------------
#   翻拍件 / 扫描件常常**躺着**（拍摄方向不对，而 EXIF Orientation 是 1 或没有），
#   浏览时整张照片转了 90°。用户能做的只有「在别的软件里转好再导入一次」——
#   那等于改写原图，本项目的铁律不允许。
#
# 本模块的做法（DR-43，三个方案里唯一不越线的一个）
# --------------------------------------------------
#   **只写一个角度值**：`pb_photo.rotateDeg ∈ {0, 90, 180, 270}`。
#   显示时由前端做一次 CSS `transform: rotate(deg)`。于是：
#     · 原图**一个字节都不动**（不改写、不改 EXIF、不碰 mtime）；
#     · 缩略图缓存键 `thumb_relpath(fileHash, size)` 不含角度 -> **不失效、不重生成**；
#     · `/api/original` 的**直传与 Range 不受影响**（没有服务端转码，
#       整图编码会把 206 分片变成一次性全量，见 api/static.py 模块头那条红线）。
#
# 另外两条被否掉的方案（记在这里以免以后有人"顺手"改回去）
# --------------------------------------------------------
#   B 服务端转码后返回：每次旋转要重生成 3 档缩略图 + 破坏 Range；
#   C 改写原图 EXIF Orientation：违反「原图只读」铁律
#     （processor/media/thumbMaker.py 第 16 行），而且会**覆盖相机原始方向**
#     —— `thumbMaker._openOriented` 与 `faceCropper` 都还在读它做纠正。
#
# 与 processor/photoTimeFix.py（DR-42）的关系：**同构但更简单**
# ----------------------------------------------------------
#   `rotateDeg` 与 `shotYearOverride` 是**同一类**的用户覆盖值：
#     · `0` = 未修正（默认值就是它，"重置"因此天然有意义）；
#     · 重扫**不覆盖**（正因为没把它加进 runner.py 的 _META_FULL_COLUMNS
#       / _metaColumns() 那两份 `DO UPDATE` 白名单 —— 加了的话重扫一次
#       会把用户旋转全部归零，见本文件同目录 runner.py 第 82 行那段说明）；
#     · 撤销/重置走同一个端点（本模块的 resetRotate = applyRotate(code, 0)）。
#   **没有 previewFix**：旋转没有「影响面」可看（见下面那段）。
#
# 为什么旋转**没有影响面**、也**不落 pb_review_log**（与 DR-42 的关键差别）
# --------------------------------------------------------------------
#   旋转**不改变任何识别事实**：
#     · 不动 `pb_face.bbox`（归一化坐标，与显示角度无关）；
#     · 不动 `pb_face.personCode` / `isConfirmed` / `isStranger`（归属不变）；
#     · 不动质心 `pb_person_centroid`（样本没变）；
#     · 不动 `pb_face.shotBucket` / `pb_photo.shotYear` / 归属（年代档不变）；
#     · 不动 `pb_photo.faceCount`（还是那几张脸）；
#   而且它**完全可逆**（再转回去即可）。
#   所以它既没有"必须先让用户看见的影响面"（对照软删的两段式 confirm），
#   也不属于「归属纠错排障链」（对照 DR-42 的 BUCKET_FIX 日志）。
#   把它塞进那条链只会让「这张脸当初怎么被认成这个人的」这条排障链混进
#   一条与归属无关的记录。
#
# 只改库，不碰磁盘
# --------------
#   本模块连 `paths` 都不 import —— 与 photoTimeFix 同一条纪律：
#   没有任何一处代码路径能写到 photo 目录。

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor
_SRC_DIR = os.path.dirname(_HERE_DIR)                           # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402

_VERSION = "20261009"

_LOG = misc.setLogNew("photoRotate", "photorotate.log")

#: 合法角度（**只有这四个**）：不做任意角度、不做镜像翻转（DR-43）。
#: 顺时针为正（90 = 向右转 / RotateCw，270 = 向左转 / RotateCcw）。
VALID_ANGLES = (0, 90, 180, 270)


class PhotoRotateError(Exception):
    """参数非法 / 照片不存在（api 层映射成 400）。"""


def normalizeAngle(rotateDeg) -> int:
    """外部传进来的角度 -> 合法 int（0/90/180/270）；非法**直接抛**，不猜、不取模。

    ⚠️ 为什么不做 `% 360` 归约：`450` / `-90` 这类值是**前端算错了**的信号。
       静默归约成 90/270 会让「按了 5 次右转」这种 bug 在前后端都看不出来
       （界面显示 90、库里也是 90，看着一切正常）。宁可 400 让调用方暴露出来。
    ⚠️ 字符串数字（`"90"`）放行：HTTP query / JSON 里数字经常是字符串形态，
       这不是错误；但 `"abc"` / `45` / `true` 一律抛。
    """
    if isinstance(rotateDeg, bool):          # bool 是 int 的子类，先挡掉
        raise PhotoRotateError("rotateDeg 不接受布尔值（收到 %r）" % (rotateDeg,))
    if isinstance(rotateDeg, int):
        value = rotateDeg
    elif isinstance(rotateDeg, float):
        if rotateDeg != int(rotateDeg):
            raise PhotoRotateError("rotateDeg 必须是整数（收到 %r）" % (rotateDeg,))
        value = int(rotateDeg)
    else:
        text = str(rotateDeg if rotateDeg is not None else "").strip()
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise PhotoRotateError("rotateDeg 不是整数（收到 %r）" % (rotateDeg,))
    if value not in VALID_ANGLES:
        raise PhotoRotateError("rotateDeg 只支持 %s，收到 %r"
                               % (list(VALID_ANGLES), rotateDeg))
    return int(value)


def _photoRow(photoCode: str) -> dict:
    """取一行 pb_photo（**含软删行** —— 旋转一张已软删的照片不该 404）。查不到抛错。"""
    code = str(photoCode or "").strip()
    if not code:
        raise PhotoRotateError("photoCode 不能为空")
    rows = sqliteCommon.query_pb_photo("pb_photo", photoCode=code,
                                       delFlag="*", limitNum=1)
    if not rows:
        raise PhotoRotateError("photoCode=%s 在 pb_photo 里不存在" % code)
    return rows[0]


def currentAngle(photoCode: str) -> int:
    """这张照片当前的 rotateDeg（**只读**；脏值一律当 0 —— 显示层不该因此炸）。"""
    row = _photoRow(photoCode)
    raw = row.get("rotateDeg")
    try:
        value = int(raw) if raw is not None and str(raw).strip() != "" else 0
    except (TypeError, ValueError):
        return 0
    return value if value in VALID_ANGLES else 0


def _writeAngle(photoCode: str, value: int) -> int:
    """写 pb_photo.rotateDeg。

    ⚠️ 必须走 `updateTableGeneral`（纯 UPDATE，值原样进参数元组）：
       走 upsert 路线时「整批为默认值」的列会被 DO UPDATE 剔除 —— 那正是
       `photoAction.py` 文件头记的那个坑（返回成功、库里一字未改），
       而在这里的表现会是「重置方向看起来成功了、刷新一下又躺回去」。
    ⚠️ 一并写 `modifyYMDHMS`：这是**唯一**的留痕（本操作不落 pb_review_log，
       理由见模块头），不留时间戳的话排障时无法回答"这个角度是什么时候转的"。
    """
    rtn = sqliteCommon.updateTableGeneral(
        "pb_photo", "photoCode = %s", (str(photoCode),),
        {"rotateDeg": int(value), "modifyYMDHMS": misc.getTime()})
    if rtn == -2:                        # sqliteHandle.RET_ERROR
        raise PhotoRotateError("pb_photo.rotateDeg 写入失败: %s"
                               % sqliteCommon.dbHandle().lastErrMsg)
    if rtn is not None and rtn < 0:
        raise PhotoRotateError("pb_photo.rotateDeg 写入返回 %r" % (rtn,))
    return rtn


def applyRotate(photoCode: str, rotateDeg) -> dict:
    """把这张照片的**显示角度**改成 rotateDeg（0/90/180/270）。落库，不碰原图。

    幂等：与当前值相同 -> 返回 `changed=False`，**一行都不写**
    （也不该刷 modifyYMDHMS —— 重复点一次不该看起来像"刚改过"）。

    返回
    ----
      {photoCode, rotateDeg, previous, changed, relPath, undoEndpoint}
      · rotateDeg = 落库后的值（幂等时就是原值）
      · undoEndpoint 指向同一个端点的重置分支，前端可据此做「撤销」
        —— 旋转本来就可逆（再转回去），不需要单独的撤销日志。
    """
    value = normalizeAngle(rotateDeg)
    row = _photoRow(photoCode)
    code = str(row.get("photoCode") or "")
    previous = currentAngle(code)

    if value == previous:
        _LOG.info("旋转幂等：%s 已是 %d 度，未改动", code, value)
        return {"photoCode": code, "rotateDeg": previous, "previous": previous,
                "changed": False, "relPath": str(row.get("relPath") or ""),
                "note": "目标角度与当前角度相同，没有改动（幂等）"}

    _writeAngle(code, value)
    _LOG.info("旋转 %s：%d -> %d 度（只改 pb_photo.rotateDeg，原图未动）",
              code, previous, value)
    return {"photoCode": code, "rotateDeg": value, "previous": previous,
            "changed": True, "relPath": str(row.get("relPath") or ""),
            "undoEndpoint": "/api/photos/%s/rotate-reset" % code,
            "note": "只改显示角度：原图、EXIF、缩略图缓存、人脸框与归属全部未动"}


def resetRotate(photoCode: str) -> dict:
    """重置方向 = `applyRotate(photoCode, 0)`。

    单列一个函数而不是让 api 层直接传 0：语义不同（一个是"转到某角度"、
    一个是"恢复原方向"），而**写的是同一列**——与 DR-42 里
    「修正年份 / 恢复自动」共用一个字段是同一个理由。
    已经是 0 时同样返回 changed=False（幂等）。
    """
    return applyRotate(photoCode, 0)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("photoRotate.py _VERSION:", _VERSION)
    print("改的列    : pb_photo.rotateDeg（0/90/180/270，0=未修正）")
    print("合法角度  :", list(VALID_ANGLES))
    print("原图      : 只读 —— 本模块不 import paths，没有任何写磁盘的路径")
    print("缩略图    : 不参与缓存键（thumb_relpath(fileHash,size)），旋转不使其失效")
    print("日志      : **不落** pb_review_log（旋转不改变任何识别事实，且完全可逆）")
    print("重扫      : 不覆盖（rotateDeg 刻意不进 runner._META_FULL_COLUMNS）")
    print("库        :", sqliteCommon.dbFilePath())
