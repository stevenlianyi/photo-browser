#! /usr/bin/env python3
#encoding: utf-8

#Filename: faceCropper.py
#Description: photo-browser 人脸裁剪图（步骤 4 备好，步骤 5 才有数据）
#
# 职责
# ----
#   1. clampBbox()          归一化 bbox(x, y, w, h) -> 像素框，**越界夹紧**
#   2. crop_from_bbox()     原图 + bbox -> 160px JPEG 字节
#   3. crop_to_file()       同上，但原子写落到 thumb\\faces\\<xx>\\<faceCode>.jpg
#
# bbox 口径（步骤 5 定，pb_face.faceBox 存的就是这个）
# ---------------------------------------------------
#   **归一化** 的 (x, y, w, h)，取值 0~1，原点左上：
#     * 归一化而不是像素 —— 换机型/换分辨率不失效，且 512 维 embedding
#       本身与分辨率无关，归一化坐标能直接喂给后续比对；
#     * (x, y) 是**左上角**而不是中心点 —— 与 Pillow 的 crop() 一致，
#       省掉一层心算，少一个出错的地方。
#
# 为什么越界必须夹紧而不是抛异常
# ------------------------------
#   bbox 来自检测模型（SCRFD），模型给的框本来就常常越出画面
#   （贴边的人脸、切了一半的人），浮点误差再叠一层。
#   步骤 5 会在一个循环里对**同一张照片的几十张脸**反复调用本函数：
#   一张脸失败就整张照片失败 -> 整批识别停摆，代价与收益完全不成比例。
#   所以：bbox 相关的任何问题都在这里夹紧消化；只有"原图根本读不了"
#   才允许失败，而那一条由 safeCrop() 以 (None, errMsg) 形式交给调用方。
#
# 硬约束
# ------
#   * 只读原图（同thumbMaker：绝不写、绝不改 mtime）
#   * 落盘一律走 thumbStore.write_atomic（原子写，**不留半文件**）
#   * 路径可推导：faces\\<faceCode[:2]>\\<faceCode>.jpg，不入库

import os
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../processor/media
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import miscCommon as misc                             # noqa: E402
from common import paths as paths                                 # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from processor.media import thumbStore as thumbStore              # noqa: E402
from processor.media import thumbMaker as thumbMaker              # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("faceCropper", "facecropper.log")


# ============================================================
# 一、bbox 归一化与夹紧
# ============================================================

def clampBbox(bbox, width: int, height: int, minEdge: int = 1) -> tuple:
    """归一化 bbox -> 像素框 (left, top, right, bottom)，**越界一律夹紧**。

    参数
    ----
    bbox    : (x, y, w, h) 归一化坐标，**原点左上**，取值 0~1。
              不接受 (x1, y1, x2, y2) 四点式 —— 两种形式在同一组数字上无法区分
              （x=.1, y=.1, w=.5, h=.5 既是"左上角+宽高"也是"对角两点"），
              靠猜必然有一半情况是错的。统一用左上角 + 宽高。
    width   : 原图像素宽
    height  : 原图像素高
    minEdge : 夹紧后允许的最小边长（像素）。**默认 1 = 只挡退化框**。
              人脸质量过滤（短边 < MIN_FACE_EDGE / detScore / yaw）是**步骤 5 的职责**，
              这里刻意不重复过滤：两处各判一次，早晚会出现"某张脸在一边过了、
              在另一边被扔掉"的难查不一致。这里只保证"裁出来不是一张废图"。

    处理掉的畸形输入
    --------------
      * None / 长度不足 4 / 非数字      -> 抛 ValueError（调用方该跳过这张脸）
      * w<=0 或 h<=0                    -> 抛 ValueError
      * x/y 为负、x+w 超1、y+h 超1      -> **夹紧**（不抛）
      * 整框落在画面外（与画面无交集）-> 抛 ValueError
      * 夹紧后短边 < minEdge             -> 抛 ValueError

    为什么"整框在画面外"要抛
    ----------------------
      夹紧后会退化成 0 或 1 像素的竖条，裁出来是一张纯色废图。
      与其把废图当头像存进 thumb\\faces（后面再也认不出它对应哪张脸），
      不如明确失败让调用方走丢弃分支。
    """
    if bbox is None:
        raise ValueError("bbox 为 None")
    try:
        values = [float(v) for v in bbox]
    except (TypeError, ValueError):
        raise ValueError("bbox 不是数字序列: %r" % (bbox,))
    if len(values) != 4:
        raise ValueError("bbox 必须是 (x, y, w, h) 四元组，收到 %d 个值" % len(values))

    x, y, w, h = values
    if w <= 0 or h <= 0:
        raise ValueError("bbox 宽高必须为正: %r" % (bbox,))

    # ---- 第一步：先在**归一化空间**把四个边界夹进 [0, 1] ----
    # 必须先夹后转像素。若先转像素再夹，整框在画面外的情况会被"压"成
    # 画面最边缘 1 像素的竖条（看起来成功了，其实裁出来是废图）。
    nx0 = min(1.0, max(0.0, x))
    ny0 = min(1.0, max(0.0, y))
    nx1 = min(1.0, max(0.0, x + w))
    ny1 = min(1.0, max(0.0, y + h))
    if nx1 <= nx0 or ny1 <= ny0:
        raise ValueError("bbox 完全落在画面之外: %r @ %dx%d" % (bbox, width, height))

    # ---- 第二步：转像素并夹进画面 ----
    left = max(0, min(int(round(nx0 * float(width))), width - 1))
    top = max(0, min(int(round(ny0 * float(height))), height - 1))
    right = max(0, min(int(round(nx1 * float(width))), width))
    bottom = max(0, min(int(round(ny1 * float(height))), height))

    if right - left < minEdge or bottom - top < minEdge:
        raise ValueError("bbox 夹紧后为空或过小: %r @ %dx%d" % (bbox, width, height))
    return (left, top, right, bottom)


def isPlausibleBbox(bbox) -> bool:
    """只判断"能不能用"，不抛异常（批量过滤时用）"""
    try:
        float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        return float(bbox[2]) > 0 and float(bbox[3]) > 0
    except (TypeError, ValueError, IndexError):
        return False


# ============================================================
# 二、裁剪
# ============================================================

def _fitToSize(image, outSize: int, square: bool, allowUpscale: bool = True):
    """把裁剪块调整到目标尺寸。

    square=False：长边 = outSize，等比（人脸横构图也不变形）
    square=True ：先中心裁成正方形再缩到 outSize×outSize（人员头像用，
                   圆形遮罩/九宫格头像位不会把脸挤出圆外）
    allowUpscale：人脸**允许**放大 —— 远处的小脸裁出来可能只有 30px，
                   放大到 160 虽糊但至少能认出是谁；不允许放大的头像就是一片马赛克。
    """
    from PIL import Image

    if square:
        side = min(image.size)
        left = (image.size[0] - side) // 2
        top = (image.size[1] - side) // 2
        image = image.crop((left, top, left + side, top + side))
        if (image.size[0] != outSize or image.size[1] != outSize) and (
                allowUpscale or image.size[0] > outSize):
            image = image.resize((outSize, outSize), Image.LANCZOS)
        return image

    w, h = image.size
    longEdge = max(w, h)
    if longEdge == outSize:
        return image
    if not allowUpscale and longEdge < outSize:
        return image
    ratio = float(outSize) / float(longEdge)
    newSize = (max(1, int(round(w * ratio))), max(1, int(round(h * ratio))))
    return image.resize(newSize, Image.LANCZOS)


def crop_from_bbox(absOrigPath: str, bbox, outSize: int = None,
                   square: bool = None, quality: int = None,
                   image=None) -> bytes:
    """按归一化 bbox 从原图裁出人脸，返回 JPEG 字节。

    参数
    ----
    absOrigPath : 原图绝对路径（**只读**）
    bbox        : (x, y, w, h) 归一化坐标，越界自动夹紧
    outSize     : 输出长边（缺省 basicSettings.FACE_CROP_SIZE = 160）
    square      : True 输出正方形（头像位用）。
                  传 None = **跟着 basicSettings.FACE_CROP_SQUARE**（当前 True），
                  显式传 False 才做等比输出。
    quality     : JPEG 质量，缺省 basicSettings.FACE_CROP_QUALITY
    image       : **可选**，已解码且已摆正的内存图（PIL.Image, RGB）。
                  给了它就不再打开 absOrigPath。
                  ⚠️ 调用方必须保证它**已做 EXIF 方向纠正**（本函数不再重复纠正，
                     重复纠正会把已经转正的图再转一次）。
                  开这个口子只有一个理由：**一张照片里有多张脸时不要重复解码原图**。
                  12MP JPEG 每次 Pillow 解码约 0.2 秒，而一张合影有 4~6 张脸 ->
                  光解码就 1 秒。步骤 5 的进程池已在 cv2 侧解过一次图，
                  转成 PIL 传进来即可（一次转换 ~0.03 秒，摊到每张脸 0.005 秒）。
                  裁剪格式（正方形 / LANCZOS / JPEG q85）仍只有这一份实现，
                  不存在「两处各裁一次、结果可能不一样」的口径分裂。

    行为约定
    --------
      * **bbox 越界不会抛异常** —— clampBbox 夹紧消化（见模块头说明）
      * 原图读不了 / 解不出 / bbox 完全不可用 -> **抛异常**。
        要"永不抛"的语义请用 safeCrop()，它返回 (bytes|None, errMsg)。
      * 小脸**允许放大**（40px 的脸拉到 160px，糊但认得出人）。
        质量过滤（短边 < MIN_FACE_EDGE）是**步骤 5** 的职责，裁剪层不重复判断。

    EXIF方向
    --------
      人脸框是检测模型在**纠正后**的图像上给的，所以这里必须先
      ImageOps.exif_transpose 再裁 —— 否则 orientation=6 的手机照片
      会把脸裁到画面外面去。转置只作用在内存副本上，不改原图字节。
      （传 image 进来时由调用方负责这一步，见参数说明。）
    """
    from io import BytesIO
    from PIL import Image, ImageOps

    width = int(outSize or basicSettings.FACE_CROP_SIZE)
    if square is None:
        square = bool(basicSettings.FACE_CROP_SQUARE)
    else:
        square = bool(square)
    q = basicSettings.FACE_CROP_QUALITY if quality is None else int(quality)

    holder = None
    if image is None:
        if not os.path.isfile(absOrigPath):
            raise OSError("原图不存在: %s" % absOrigPath)
        holder = Image.open(absOrigPath)
    else:
        holder = image
    src = None
    imageObj = None
    piece = None
    fitted = None
    try:
        if holder is image:
            # 调用方给的图已经摆正，这里**不再** exif_transpose（重复纠正会转反）
            src = holder
        else:
            src = ImageOps.exif_transpose(holder) or holder
        if src.mode != "RGB":
            imageObj = src.convert("RGB")
            src = imageObj
        box = clampBbox(bbox, src.size[0], src.size[1], minEdge=1)
        piece = src.crop(box)
        fitted = _fitToSize(piece, width, square)
        buffer = BytesIO()
        fitted.save(buffer, basicSettings.FACE_CROP_FORMAT, quality=q, optimize=True)
        return buffer.getvalue()
    finally:
        for one in (fitted, piece):
            if one is not None:
                try:
                    one.close()
                except Exception:
                    pass
        if imageObj is not None:
            try:
                imageObj.close()
            except Exception:
                pass
        if holder is not None and holder is not image:
            try:
                holder.close()
            except Exception:
                pass


def safeCrop(absOrigPath: str, bbox, outSize: int = None,
             square: bool = None, image=None) -> tuple:
    """crop_from_bbox 的**永不抛异常**版本，返回 (data|None, errMsg)。

    步骤 5 的主循环用这个：一张脸失败只丢这一张脸，
    绝不让整张照片、整批识别失败。
    """
    try:
        return crop_from_bbox(absOrigPath, bbox, outSize=outSize, square=square,
                              image=image), ""
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, e)
        _LOG.warning("safeCrop 丢弃: %s bbox=%r (%s)" % (absOrigPath, bbox, msg))
        return None, msg


def crop_to_file(absOrigPath: str, bbox, faceCode: str,
                 outSize: int = None, square: bool = None,
                 thumbRoot: str = None, force: bool = False,
                 photoRoot: str = None) -> dict:
    """裁人脸并**原子写**到 thumb\\faces\\<xx>\\<faceCode>.jpg。

    路径完全由 faceCode 推导（thumbStore.face_relpath），不查库、不入库。
    square 传 None = 跟 basicSettings.FACE_CROP_SQUARE（当前 True -> 160x160 正方形）。
    photoRoot 只用于写入前的只读边界校验（见 thumbStore.write_atomic）。

    返回
    ----
    dict {ok, created, cached, absPath, relpath, bytes, errMsg}
    """
    result = {"ok": False, "created": False, "cached": False, "absPath": "",
              "relpath": "", "bytes": 0, "errMsg": ""}
    try:
        relpath = thumbStore.face_relpath(faceCode)
        absPath = thumbStore.face_abspath(faceCode, thumbRoot=thumbRoot)
    except thumbStore.ThumbStoreError as e:
        result["errMsg"] = str(e)
        return result
    result["relpath"] = relpath
    result["absPath"] = absPath

    if not force and thumbStore.exists(absPath):
        result.update(ok=True, cached=True, bytes=thumbStore.fileSize(absPath))
        return result

    data, errMsg = safeCrop(absOrigPath, bbox, outSize=outSize, square=square)
    if data is None:
        result["errMsg"] = errMsg
        return result
    try:
        thumbStore.write_atomic(absPath, data, tag=str(os.getpid()), photoRoot=photoRoot)
    except Exception as e:
        result["errMsg"] = "写人脸图失败 %s: %s" % (type(e).__name__, e)
        _LOG.error("crop_to_file: %s" % result["errMsg"])
        return result
    result.update(ok=True, created=True, bytes=len(data))
    return result


def make_face_thumb(row: dict, bbox=None, thumbRoot: str = None,
                    photoRoot: str = None, force: bool = False) -> dict:
    """给一条 pb_face 行裁人脸图（bbox 缺省从行内 "bbox" 列解析）。

    ⚠️ **列名是 `bbox`**，不是 `faceBox`。
       这个错很容易犯且**不报错**：row 里没有 `faceBox` 时 `row.get` 返回 None，
       整条链路走到 isPlausibleBbox(None) 才失败，报"bbox 不可用: ()"，
       看起来像数据有问题，实际是**读错了列名**。所以这里显式两样都试。
    """
    if bbox is None:
        row = row or {}
        # 先读真正的列名，再退到 faceBox（步骤 5 之前的中间产物可能用了这个名字）
        raw = row.get("bbox")
        if raw is None:
            raw = row.get("faceBox")
        bbox = parseFaceBox(raw)
    if not isPlausibleBbox(bbox):
        return {"ok": False, "created": False, "cached": False, "absPath": "",
                "relpath": "", "bytes": 0, "errMsg": "pb_face.bbox 不可用: %r" % (bbox,)}
    relPath = str((row or {}).get("relPath") or "")
    if not relPath:
        return {"ok": False, "created": False, "cached": False, "absPath": "",
                "relpath": "", "bytes": 0, "errMsg": "行内没有 relPath"}
    try:
        absOrig = thumbStore.orig_abs_path(relPath, photoRoot=photoRoot)
    except thumbStore.ThumbStoreError as e:
        return {"ok": False, "created": False, "cached": False, "absPath": "",
                "relpath": "", "bytes": 0, "errMsg": str(e)}
    return crop_to_file(absOrig, bbox, str((row or {}).get("faceCode") or ""),
                        thumbRoot=thumbRoot, force=force, photoRoot=photoRoot)


# ============================================================
# 七、pb_face.bbox 的**存**与**读**（格式已定死）
# ============================================================
# 格式（唯一定义，步骤 5 写入请一律用 formatFaceBox()）：
#     "x,y,w,h" —— 四个**归一化**小数，逗号分隔，**无空格**，原点左上
#
# 为什么定成这个
# --------------
#   1. **字段名与列名一致**（`bbox`）。库表列名是 schema 的事实，
#      代码里再叫它 faceBox 就会出现"读错列名还不报错"（见 make_face_thumb 注释）。
#   2. 逗号分隔的四个数在 TEXT/VARCHAR(64) 里最省心：
#      4 个 8 位小数 + 3 个逗号 = 35 字符，VARCHAR(64) 富余。
#      JSON 数组 `[0.1,0.2,...]` 多两对括号引号，SQL 里转义又更啰嗦。
#   3. **不存像素**：像素框换台机器就废；归一化坐标与分辨率无关，
#      直接可以喂给前端叠加、也可以跨机型比对。
#
# 读的一侧（parseFaceBox）额外容忍两种写法，都是**只读兼容**，
# 不作为写入格式：JSON 数组（有人会顺手 json.dumps）、分号/竖线分隔（手抄）。
# 写的一侧只认 formatFaceBox —— 格式只有一处定义，才不会写读不一致。

BBOX_DECIMALS: int = 4


def formatFaceBox(bbox) -> str:
    """把 bbox 四元组格式化成入库字符串（**写 bbox 列的唯一入口**）。

    越界值**照原样写出，不在这里夹紧** —— 夹紧是裁剪时的事（clampBbox），
    这里一夹，库里就再也看不到检测模型给的原始框，事后无法复查识别质量。
    """
    vals = [float(v) for v in tuple(bbox)]
    if len(vals) != 4:
        raise ValueError("bbox 必须是 4 元组 (x, y, w, h): %r" % (bbox,))
    return ",".join(("%." + str(int(BBOX_DECIMALS)) + "f") % v for v in vals)


def parseFaceBox(bboxText) -> tuple:
    """解析 pb_face.bbox（TEXT）-> (x, y, w, h) 浮点四元组。

    **标准格式**是 formatFaceBox() 产出的 "x,y,w,h"；
    另外容忍 JSON 数组、list/tuple、分号/竖线分隔、多余空格 —— 纯只读兼容。
    解析不出来一律返回 ()，由调用方走"丢弃这一张脸"的分支。
    """
    if bboxText is None:
        return ()
    if isinstance(bboxText, (list, tuple)):
        # 长度也要验：list/tuple 直传时（步骤 5 内存里的中间产物）同样可能是 3 个数
        return tuple(bboxText) if len(bboxText) == 4 else ()
    text = str(bboxText).strip()
    if not text:
        return ()
    if text.startswith("["):
        import json
        try:
            vals = json.loads(text)
        except (TypeError, ValueError):
            return ()
        # 长度也要验：json.loads("[1,2,3]") 成功，但三个数不是框
        return tuple(vals) if isinstance(vals, (list, tuple)) and len(vals) == 4 else ()
    parts = [p for p in text.replace(";", ",").replace("|", ",").split(",") if p.strip()]
    if len(parts) != 4:
        return ()
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        return ()


# 复用 thumbMaker 的方向纠正（同一套口径，避免两处实现漂移）
openOriented = thumbMaker._openOriented


if __name__ == "__main__":
    import json

    print("faceCropper _VERSION:", _VERSION)
    print("FACE_CROP_SIZE      :", basicSettings.FACE_CROP_SIZE)
    print("FACE_CROP_FORMAT    :", basicSettings.FACE_CROP_FORMAT,
          "quality", basicSettings.FACE_CROP_QUALITY)
    print("face_relpath(示例)  :", thumbStore.face_relpath("f0a1b2c3" + "0" * 56))
    print("parseFaceBox        :", parseFaceBox("0.1,0.2,0.3,0.4"))
    for bad in ((-0.2, -0.2, 1.4, 1.4), (0.5, 0.5, 0.1, 0.1), (0.1, 0.1, 0.0, 0.5)):
        try:
            print("clampBbox %-22r ->" % (bad,), clampBbox(bad, 1000, 800))
        except ValueError as e:
            print("clampBbox %-22r -> 拒绝: %s" % (bad, e))
    print("json:", json.dumps({"ok": True}, ensure_ascii=False))
