#! /usr/bin/env python3
#encoding: utf-8
#Filename: engine.py
#Description: photo-browser 人脸引擎（步骤 5）：SCRFD 检测 + ArcFace 512 维特征 + 质量过滤
#
# 职责
# ----
#   1. imreadUnicode()    **中文路径安全读图**（Windows 上 cv2.imread 读不了非 ASCII 路径）
#   2. FaceEngine.extract()  一张照片 -> 若干张合格人脸（检测 + 提取 + 过滤 + 排序 一步到位）
#   3. getEngine()        **进程内懒加载一次**的引擎单例（子进程每进程一次，不是每张一次）
#   4. deriveFaceCode()   faceCode 幂等键（纯函数，**放在这一层**因为子进程也要用，
#                         而子进程绝不允许 import 任何碰数据库的模块）
#
# 统一输出（FaceHit）
# --------------------
#   bbox        归一化 (x, y, w, h)，原点左上，取值 0~1，入库用 faceCropper.formatFaceBox
#   detScore    SCRFD 检测置信度
#   poseYaw     偏航角（度，正脸≈0，侧脸绝对值大）—— 质量过滤用
#   posePitch   俯仰角（度）
#   embedding   np.float32[512]，**已 L2 归一化**；跨进程/入库统一转 2048 字节小端
#   quality     综合质量分（0~1），detScore 与脸尺寸的折中
#   isPrimary   一张图内**面积最大**的那张脸；其余按 detScore 降序
#
# 中文路径：为什么必须 np.fromfile + cv2.imdecode
# ------------------------------------------------
#   cv2.imread 在 Windows 上把路径交给 fopen（走当前 ANSI 代码页），
#   遇到 GBK 编不下的字符（生僻字、日文、emoji 文件名）直接返回 None，**且不报错**。
#   S0 脚本 tools/verify_accuracy.py 早就踩过这个坑，本项目所有读图一律走
#   「np.fromfile（Python 的 open，走 Unicode）+ cv2.imdecode（只认字节流）」。
#   这条同时也保证了 photoDir 绝对只读：fromfile 只开不写。
#
# 质量过滤（提取阶段做掉，**丢弃不入库**）
# ----------------------------------------
#   detScore < MIN_DET_SCORE(0.6) / 人脸框短边 < MIN_FACE_EDGE(64px) / |poseYaw| > MAX_YAW(45°)
#   阈值全部来自 config/basicSettings，与 S0 脚本逐字对齐 —— 否则步骤 5 的统计
#   与 S0 的统计就不是同一个口径，验收第 1 条（±2%）没有意义。
#   丢弃原因码与 S0 完全一致：decode_fail / no_face / low_det / too_small / side_face / zero_vec
#
# ⚠️ pose 缺失时**保留**该脸（与 S0 一致）
#    S0 脚本是 `if pose is not None and len(pose) >= 3` 才过滤，pose 取不到就放过。
#    这里照抄：口径不一致会导致 S0 与本步的样本数对不上，而"多存了几张脸"的代价
#    远小于"统计口径漂移"—— 多存的脸在步骤 6 匹配阶段一样会被低分挡住。
#
# 后端（两条路，接口完全一致）
# --------------------------
#   1. _InsightFaceBackend  insightface FaceAnalysis(name="buffalo_l", CPUExecutionProvider)
#      prepare(ctx_id=-1, det_size=(640,640))。**本机默认走这条。**
#   2. _RawOnnxBackend       裸 onnxruntime 加载 SCRFD(det_10g) + ArcFace(w600k_r50)
#      + 1k3d68(姿态)。零编译、零 insightface 依赖，用于 insightface 在 Python 3.13
#      装不上的降级预案（开发计划 §6.5 风险表）。
#      ⚠️ 姿态估计需要 68 点 3D 均值形状（insightface 的 meanshape_68.pkl）。
#         拿不到该文件时 pose 不可用 -> 按上面那条"pose 缺失保留"处理，
#         **并打 warning 日志说明本轮 yaw 过滤未生效**，绝不静默降级。
#
# 硬约束
# ------
#   * onnxruntime 只用 CPU 版（**禁止 onnxruntime-gpu**）：显式传 CPUExecutionProvider
#   * photoDir 绝对只读：本模块只 imread / fromfile，不写、不删、不改 mtime
#   * 本模块**不 import 任何数据库相关模块**（sqliteCommon/sqliteHandle/config.sqliteSettings）。
#     子进程会 import 本模块，破了这条就是 database is locked 的起点。
#     pool.assertNoDatabaseImport() 会对本文件做静态自查。

import math
import os
import sys
import threading

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/face
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import numpy as np                                                # noqa: E402

from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from processor.media import faceCropper as faceCropper            # noqa: E402
from processor.media import thumbStore as thumbStore              # noqa: E402

_VERSION = "20261004"

_LOG = misc.setLogNew("faceEngine", "faceengine.log")

# 特征维度与字节数（float32[512] 小端 = 2048 字节），转发自 basicSettings
EMBEDDING_DIM: int = basicSettings.EMBEDDING_DIM
EMBEDDING_BYTES: int = basicSettings.EMBEDDING_BYTES

#: 缺省模型包名
DEFAULT_MODEL_PACK: str = "buffalo_l"
#: 缺省检测边长（与 S0 脚本一致）
DEFAULT_DET_SIZE: tuple = (640, 640)
#: **只允许 CPU**：显式传给 onnxruntime，禁止 GPU 版带来的一堆 CUDA 依赖
CPU_PROVIDERS: tuple = ("CPUExecutionProvider",)

# 丢弃原因码（**与 S0 脚本 verify_accuracy.py 逐字一致**，统计口径才能对齐）
REASON_DECODE_FAIL: str = "decode_fail"
REASON_NO_FACE: str = "no_face"
REASON_LOW_DET: str = "low_det"
REASON_TOO_SMALL: str = "too_small"
REASON_SIDE_FACE: str = "side_face"
REASON_ZERO_VEC: str = "zero_vec"
REASON_NO_FILE: str = "no_file"
#: 全部原因码（backtest 汇总用，保证输出列稳定）
REASON_CODES: tuple = (REASON_NO_FILE, REASON_DECODE_FAIL, REASON_NO_FACE,
                       REASON_LOW_DET, REASON_TOO_SMALL, REASON_SIDE_FACE,
                       REASON_ZERO_VEC)

# faceCode 前缀：thumbStore 只允许纯十六进制做路径分量，'f'/'c' 都在内
FACE_CODE_PREFIX: str = "fc"
FACE_CODE_LEN: int = 32


# ============================================================
# 一、中文路径安全读图（S0 已踩过的坑）
# ============================================================

def imreadUnicode(absPath: str):
    """读图，兼容中文/非 ASCII 路径。**永远不要用 cv2.imread。**

    返回 None 表示读不出（文件不存在 / 权限 / 解不出像素），
    调用方据此走 REASON_DECODE_FAIL 分支。cv2 会把坏 JPEG 的告警打到 stderr，
    这里无法屏蔽（那是 C 层），但返回值可靠。
    """
    if not absPath:
        return None
    try:
        # np.fromfile 走 Python 的 open()，路径是 Unicode，绕开 ANSI 代码页
        buf = np.fromfile(absPath, dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if buf.size == 0:
        return None
    img = cv2Imdecode(buf)
    return img


def cv2Imdecode(buf):
    """延迟 import cv2 的解码包装（import 本身很慢，且缺 cv2 时报错信息要好看）"""
    import cv2
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def cv2Module():
    """取 cv2 模块（供降级后端复用，避免各处重复 import）"""
    import cv2
    return cv2


# ============================================================
# 二、faceCode 幂等键（纯函数，子进程也要用）
# ============================================================

def deriveFaceCode(photoCode: str, bbox, extra: str = "") -> str:
    """由 (photoCode, bbox) 派生 faceCode —— **幂等的根源**。

    为什么不用 uuid（步骤 3 的 photoCode 就是 "PH_" + uuid4）
    --------------------------------------------------
      faceCode 是 pb_face 的幂等键（UNIQUE 索引），也是人脸裁剪图的**文件名**
      （faces\\<faceCode[:2]>\\<faceCode>.jpg，路径可推导、不入库）。
      若用随机 uuid：同一张照片重跑一次提取 -> 新的 faceCode ->
      库里多出一整批重复人脸，旧行变成没人引用的孤儿，
      而磁盘上的人脸图也变成两套。**重跑必须幂等**，这是硬要求。
      photoCode 反而不能派生化（步骤 3 已定：relPathHash 规则一改就会全库变脸）。

    为什么用 bbox 而不是别的
    ----------------------
      一张照片里人脸的位置就是这张脸的天然身份；两张不同的照片即使内容相同，
      photoCode 也不同（fileHash 相同才是重复，那是步骤 3 的 isDuplicate 口径），
      所以 (photoCode, bbox) 足以唯一定位"这张照片上的这张脸"。

    精度口径
    --------
      bbox 按 faceCropper.BBOX_DECIMALS(=4) 位小数参与摘要 —— 与入库值同精度。
      检测模型重跑时 bbox 第 5 位以后的浮点抖动不会改编码（**必须同精度**，
      否则 4 位小数的库值与 16 位的原值会算出两个不同 faceCode）。

    extra
    ----
      预留后手（例如换模型后要另起一套编码）。默认空串。
    """
    key = "%s|%s|%s" % (str(photoCode or ""),
                        ",".join(("%.4f" % float(v)) for v in tuple(bbox)),
                        str(extra or ""))
    return FACE_CODE_PREFIX + misc.sha256Hex(key)[: FACE_CODE_LEN - len(FACE_CODE_PREFIX)]


def encodeEmbedding(vec) -> bytes:
    """np.float32[512] -> **2048 字节小端**（落 MEDIUMBLOB 的唯一口径）。

    显式指定 dtype="<f4"：Windows 与部分 ARM 平台 numpy 默认可能是大端，
    而步骤 6 的余弦比对与其它机器互通，字节序错了相似度会全是噪声且不报错。
    """
    arr = np.asarray(vec, dtype=np.float32).ravel()
    if arr.size != EMBEDDING_DIM:
        raise ValueError("embedding 维度必须是 %d，收到 %d" % (EMBEDDING_DIM, arr.size))
    return arr.astype("<f4", copy=False).tobytes()


def decodeEmbedding(blob):
    """2048 字节小端 -> np.float32[512]（读回侧；不做归一化，库里存的就是已归一化的）"""
    if blob is None:
        return None
    raw = bytes(blob)
    if len(raw) != EMBEDDING_BYTES:
        raise ValueError("embedding 字节数必须是 %d，收到 %d" % (EMBEDDING_BYTES, len(raw)))
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def l2normalize(vec):
    """L2 归一化；零向量返回 None（宁可丢弃，也绝不存一个方向不定的向量）"""
    arr = np.asarray(vec, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(arr))
    if norm < 1e-9:
        return None
    return (arr / norm).astype(np.float32)


def cosine(a, b) -> float:
    """余弦相似度（本项目所有相似度都基于**已归一化**向量，点积即可）"""
    va = np.asarray(a, dtype=np.float32).ravel()
    vb = np.asarray(b, dtype=np.float32).ravel()
    if va.size == 0 or vb.size == 0 or va.size != vb.size:
        return 0.0
    return float(np.dot(va, vb))


# ============================================================
# 三、单张人脸结果
# ============================================================

class FaceHit(object):
    """一张**合格**人脸（已过质量过滤）。

    刻意用普通 class + __slots__ 而不是 dataclass：跨进程 pickle 时
    __slots__ 类比带 __dict__ 的类小得多，而 pool 每秒要传好几张。
    """

    __slots__ = ("faceCode", "bbox", "detScore", "poseYaw", "posePitch",
                 "embedding", "quality", "isPrimary", "areaRatio",
                 "shortEdge", "cropData")

    def __init__(self, bbox, detScore, poseYaw, posePitch, embedding,
                 quality=0.0, isPrimary=False, areaRatio=0.0, shortEdge=0,
                 faceCode="", cropData=None):
        #: 归一化 (x, y, w, h)，原点左上
        self.bbox = tuple(float(v) for v in bbox)
        self.detScore = float(detScore)
        self.poseYaw = poseYaw
        self.posePitch = posePitch
        #: np.float32[512]，已 L2 归一化
        self.embedding = np.asarray(embedding, dtype=np.float32).ravel()
        self.quality = float(quality)
        #: True = 这张图里面积最大的一张脸（主脸）
        self.isPrimary = bool(isPrimary)
        #: 脸框面积 / 画面面积
        self.areaRatio = float(areaRatio)
        #: 脸框短边（原图像素），质量过滤判据
        self.shortEdge = int(shortEdge)
        self.faceCode = str(faceCode or "")
        #: 人脸裁剪图 JPEG 字节（子进程裁好回传，省掉主进程串行解码）
        self.cropData = cropData

    # ---- 跨进程传递 ----

    def toDict(self) -> dict:
        """转成可 pickle 的纯 dict（embedding 转 2048 字节，cropData 保持 bytes）"""
        return {
            "faceCode": self.faceCode,
            "bbox": self.bbox,
            "detScore": self.detScore,
            "poseYaw": self.poseYaw,
            "posePitch": self.posePitch,
            "embedding": encodeEmbedding(self.embedding),
            "quality": self.quality,
            "isPrimary": self.isPrimary,
            "areaRatio": self.areaRatio,
            "shortEdge": self.shortEdge,
            "cropData": self.cropData,
        }

    @staticmethod
    def fromDict(data: dict) -> "FaceHit":
        hit = FaceHit(
            bbox=data["bbox"], detScore=data.get("detScore", 0.0),
            poseYaw=data.get("poseYaw"), posePitch=data.get("posePitch"),
            embedding=decodeEmbedding(data.get("embedding")),
            quality=data.get("quality", 0.0),
            isPrimary=data.get("isPrimary", False),
            areaRatio=data.get("areaRatio", 0.0),
            shortEdge=data.get("shortEdge", 0),
            faceCode=data.get("faceCode", ""),
            cropData=data.get("cropData"))
        return hit

    def __repr__(self) -> str:
        return ("FaceHit(code=%s bbox=%s det=%.3f yaw=%s edge=%d q=%.3f%s)"
                % (self.faceCode or "-",
                   faceCropper.formatFaceBox(self.bbox), self.detScore,
                   "-" if self.poseYaw is None else "%.1f" % self.poseYaw,
                   self.shortEdge, self.quality, " 主脸" if self.isPrimary else ""))


# ============================================================
# 四、质量过滤与排序
# ============================================================

def scoreQuality(detScore: float, shortEdge: int, minFaceEdge: int = None) -> float:
    """综合质量分 = detScore × 尺寸系数，落在 0~1。

    为什么不能直接用 detScore 当 quality
    ------------------------------------
      detScore 只说"模型多确信检测到了这张脸"，**完全不管脸有多大**。
      而 embedding 的可靠性强烈依赖脸框像素数：一张 70px 的正脸和一张 300px 的正脸，
      detScore 可能都是 0.99，但前者的 512 维向量噪声明显更大。
      步骤 6 要按 quality 排序取 Top-5 候选、步骤 7 要按 quality 筛聚类输入，
      若这一列只是 detScore 的副本，等于没做。

    尺寸系数
    --------
      min(1, shortEdge / (4 × MIN_FACE_EDGE))：256px 拿满分。
      64px（下限）得 0.25，正好把"刚过线"的小脸压到候选队列后面。
      为什么不直接用 shortEdge/阈值：那样所有合格的脸系数都=1，尺寸信息全丢。
    """
    edge0 = int(minFaceEdge if minFaceEdge is not None else basicSettings.MIN_FACE_EDGE)
    full = max(1, edge0 * 4)
    sizeFactor = min(1.0, max(0.0, float(shortEdge) / float(full)))
    return round(float(detScore) * sizeFactor, 4)


def clampNormBBox(bbox) -> tuple:
    """把归一化 bbox 夹进 [0,1] 画面内，返回 (x, y, w, h)。

    为什么入库的框必须夹紧（本机实测踩到）
    ------------------------------------
      SCRFD 检测出来的框**会伸出画面**：实测 50 张人脸里有 6 张越界，
      其中一张是 0.1258,-0.0255,0.1175,0.1966 —— y 是**负数**（人脸在画面上沿之外），
      另有 0.9841,...,0.0206 这种 x+w=1.0047（右侧出界 0.5%）。

      不夹紧的直接后果：
        · 步骤 6 做框重叠 / IoU 判定时算出无意义的面积；
        · 前端按框画人脸矩形会画到照片外面，看着像"框错位"；
        · 任何 x>0 and x+w<1 的"在画面内"判断都会漏掉这些人脸。

      「留原始框供事后复查」这个理由不成立：复查可以重跑检测拿到原始框，
      而一个越界的框留在库里会**持续误导下游**。宁可少几个像素的边界信息。
      夹紧后仍然是**同一个框**，只是被画布边界裁了一下：
      脸在画面内的部分一点不少，裁掉的部分本来就不在照片里。
    """
    x, y, bw, bh = [float(v) for v in bbox]
    x1 = min(max(x, 0.0), 1.0)
    y1 = min(max(y, 0.0), 1.0)
    x2 = min(max(x + bw, 0.0), 1.0)
    y2 = min(max(y + bh, 0.0), 1.0)
    if x2 <= x1:
        x2 = min(1.0, x1 + 1e-4)
    if y2 <= y1:
        y2 = min(1.0, y1 + 1e-4)
    return (round(x1, 6), round(y1, 6), round(x2 - x1, 6), round(y2 - y1, 6))


def filterAndRank(rawFaces: list, imgW: int, imgH: int,
                  minDetScore: float = None, minFaceEdge: int = None,
                  maxYaw: float = None) -> tuple:
    """质量过滤 + 主脸判定 + 排序。返回 (合格 FaceHit 列表, 丢弃原因统计 dict)。

    rawFaces
    --------
      [dict(bboxPx=(x1,y1,x2,y2), detScore, poseYaw, posePitch, embedding), ...]
      bboxPx 是**原图像素**坐标（insightface 已经是；降级后端自己换算好再传进来）。

    过滤
    ----
      detScore < minDetScore        -> low_det
      短边 < minFaceEdge            -> too_small   （短边 = min(x2-x1, y2-y1)，像素）
      |poseYaw| > maxYaw            -> side_face   （pose 为 None 时不过滤，见模块头说明）
      embedding L2 范数 < 1e-9      -> zero_vec

    排序
    ----
      **主脸 = 面积最大**（与 S0 的 `max(faces, key=面积)` 同一口径）排第 0，
      其余按 detScore 降序。全部保留、全部入库（合影里的配角也是照片的一部分）。
      面积相等时按 detScore、再按 bbox 兜底，保证**同一张图重跑排序完全一致**——
      排序不稳定会让"取第 1 张当主脸"在重跑时指向另一张脸。

    返回
    ----
      (hits, rejected)
      hits     : list[FaceHit]（embedding 已 L2 归一化，isPrimary 已标）
      rejected : dict[原因码 -> 张数]
    """
    if minDetScore is None:
        minDetScore = basicSettings.MIN_DET_SCORE
    if minFaceEdge is None:
        minFaceEdge = basicSettings.MIN_FACE_EDGE
    if maxYaw is None:
        maxYaw = basicSettings.MAX_YAW
    w = max(1, int(imgW))
    h = max(1, int(imgH))
    rejected = dict((code, 0) for code in REASON_CODES)

    kept = []
    for one in rawFaces or ():
        detScore = float(one.get("detScore") or 0.0)
        x1, y1, x2, y2 = [float(v) for v in one["bboxPx"]]
        shortEdge = int(min(x2 - x1, y2 - y1))
        # ① 置信度
        if detScore < minDetScore:
            rejected[REASON_LOW_DET] += 1
            continue
        # ② 脸框短边（像素）。先滤短边再算面积：退化框（w 或 h ≈ 0）面积也≈0，
        #    留着会在"取面积最大"那一步把主脸位置让给一张废脸
        if shortEdge < minFaceEdge:
            rejected[REASON_TOO_SMALL] += 1
            continue
        # ③ 侧脸。pose 为 None 时**不过滤**（与 S0 一致，见模块头）
        yaw = one.get("poseYaw")
        if yaw is not None and abs(float(yaw)) > maxYaw:
            rejected[REASON_SIDE_FACE] += 1
            continue
        # ④ 向量
        vec = l2normalize(one.get("embedding"))
        if vec is None:
            rejected[REASON_ZERO_VEC] += 1
            continue
        area = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
        kept.append({
            "bboxPx": (x1, y1, x2, y2),
            "detScore": detScore,
            "poseYaw": None if yaw is None else float(yaw),
            "posePitch": (None if one.get("posePitch") is None
                          else float(one.get("posePitch"))),
            "embedding": vec,
            "area": area,
            "shortEdge": shortEdge,
        })

    if not kept:
        if not rawFaces:
            rejected[REASON_NO_FACE] += 1
        return [], rejected

    # 面积降序 -> detScore 降序 -> bbox 兜底（保证确定性）
    kept.sort(key=lambda d: (-d["area"], -d["detScore"],
                             -d["bboxPx"][0], -d["bboxPx"][1]))
    hits = []
    for index, one in enumerate(kept):
        x1, y1, x2, y2 = one["bboxPx"]
        # 归一化后**必须夹紧到画面内**（见 clampNormBBox 的说明）。
        # ⚠️ 排序用的 area 仍是**夹紧前**的原始面积：主脸口径要与 S0 逐字一致
        #    （S0 用 insightface 的原始框算面积），改了会让"谁是主脸"与 S0 不同，
        #    进而让验收第 1 条的对比失去意义。存储与排序在这里刻意用两套。
        bbox = clampNormBBox((x1 / float(w), y1 / float(h),
                              (x2 - x1) / float(w), (y2 - y1) / float(h)))
        hits.append(FaceHit(
            bbox=bbox, detScore=one["detScore"],
            poseYaw=one["poseYaw"], posePitch=one["posePitch"],
            embedding=one["embedding"],
            quality=scoreQuality(one["detScore"], one["shortEdge"], minFaceEdge),
            isPrimary=(index == 0),
            areaRatio=one["area"] / float(w * h),
            shortEdge=one["shortEdge"]))
    return hits, rejected


# ============================================================
# 五、后端一：insightface（本机默认）
# ============================================================

class _Limits(object):
    """三个质量阈值（构造时把 None 解析成 basicSettings 的缺省值）"""

    __slots__ = ("minDetScore", "minFaceEdge", "maxYaw")

    def __init__(self, minDetScore=None, minFaceEdge=None, maxYaw=None):
        self.minDetScore = (basicSettings.MIN_DET_SCORE if minDetScore is None
                            else float(minDetScore))
        self.minFaceEdge = (basicSettings.MIN_FACE_EDGE if minFaceEdge is None
                            else int(minFaceEdge))
        self.maxYaw = (float(basicSettings.MAX_YAW) if maxYaw is None
                       else float(maxYaw))


class _FaceHolder(object):
    """给 insightface 的 landmark 模型用的极简"脸"容器。

    Landmark.get(img, face) 只干两件事：读 `face.bbox`、往 face[taskname] 里写结果。
    所以不必构造 insightface 自己的 Face 类（那个类在不同版本里字段一直在变，
    依赖它等于把版本升级变成一次静默的破坏性变更）。
    这里用 dict + 属性访问同时支持两种写法，跨版本更耐改。
    """

    def __init__(self, bbox):
        self.bbox = np.asarray(bbox, dtype=np.float32)
        self._data = {}

    def __setitem__(self, key, value):
        self._data[key] = value

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getattr__(self, key):
        try:
            return self.__dict__["_data"][key]
        except KeyError:
            raise AttributeError(key)


class _InsightFaceBackend(object):
    """insightface FaceAnalysis 封装。

    两个刻意的配置
    --------------
    ① 固定 `providers=["CPUExecutionProvider"]`：本项目**不需要 GPU**，
       而 onnxruntime-gpu 一旦混进环境里，默认 provider 会变成 CUDA，
       表现为「同样的代码在别人机器上跑不起来」或「默默吃掉一块显存」。
    ② `allowed_modules` 只留 detection / recognition / landmark_3d_68。
       buffalo_l 一共 5 个模型，缺省会把 landmark_2d_106 与 genderage 也跑上，
       而本项目**一个都不需要**：
         · 2d106（106 个 2D 关键点）—— 只有"在照片上画 106 个点"才用得上；
           识别用的 5 点关键点由检测模型直接给，两者不互相覆盖（实测 kps max|Δ|=0），
           所以去掉它**特征一个字节都不变**。
         · genderage —— 本项目不存性别/年龄（pb_face 没有这两列）。
       实测 12MP 单张：1.58s -> 0.85s，**腰斩**，而这正是「单张 <1s」验收项的大头。
       landmark_3d_68 必须留着 —— **pose 只由它提供**（2d106 的 require_pose 是
       False，见 insightface/model_zoo/landmark.py 的 output_shape[1]==3309 判据）。
    """

    name = "insightface"
    #: 只加载这三个模型（缺 landmark_3d_68 时退回全量，姿态改为不可用）
    ALLOWED_MODULES = ("detection", "recognition", "landmark_3d_68")

    def __init__(self, modelPack: str = DEFAULT_MODEL_PACK,
                 detSize: tuple = DEFAULT_DET_SIZE, modelRoot: str = None,
                 minDetScore: float = None, minFaceEdge: int = None,
                 maxYaw: float = None):
        from insightface.app import FaceAnalysis
        kwargs = {"name": modelPack, "providers": list(CPU_PROVIDERS),
                  "allowed_modules": list(self.ALLOWED_MODULES)}
        if modelRoot:
            kwargs["root"] = modelRoot
        try:
            self.app = FaceAnalysis(**kwargs)
        except Exception:
            # 模型包清单里没有 landmark_3d_68（精简包）时退回全量加载，
            # 姿态随之不可用 —— 与「pose 缺失不参与 yaw 过滤」的口径一致
            kwargs.pop("allowed_modules")
            self.app = FaceAnalysis(**kwargs)
        self.app.prepare(ctx_id=-1, det_size=tuple(detSize))
        self.detSize = tuple(detSize)
        self.modelPack = modelPack
        #: 实际加载到的模型名（诊断用：一眼看出 pose 能不能用）
        self.loadedModules = tuple(sorted(self.app.models.keys()))
        self.limits = _Limits(minDetScore, minFaceEdge, maxYaw)
        self.reportRejected = False
        self.det = self.app.models.get("detection")
        self.rec = self.app.models.get("recognition")
        self.pose = self.app.models.get("landmark_3d_68")

    def detect(self, img) -> tuple:
        """分阶段过滤，返回 (rawFaces, preRejected)。

        为什么不用 insightface 的 app.get() 一把梭
        -----------------------------------------
          `app.get()` 会给**每一张检出的人脸**都跑一遍 1k3d68（姿态，143MB 模型）
          和 ArcFace，然后我们才在 filterAndRank 里把不合格的丢掉 ——
          等于「先花 0.14 秒算一张马上要扔掉的脸」。
          实测一张 4 人的合影里有 1~2 张会被质量过滤拦掉，
          分阶段之后这些脸一个模型都不用跑。
          阶段顺序按「便宜的判据先过」排：
            detScore / 短边（纯算术，0 成本） -> 侧脸（1k3d68）-> 特征（ArcFace）
          ArcFace 放最后：它是最贵的一步，而 yaw 过滤已经先拦掉一批了。
        """
        pre = dict((code, 0) for code in REASON_CODES)
        det, kpss = self.det.detect(img)
        out = []
        dropped = []            # reportRejected=True 时带上被丢弃的脸（无 embedding）
        for index in range(det.shape[0]):
            bbox = tuple(float(v) for v in det[index, 0:4])
            detScore = float(det[index, 4])
            shortEdge = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])

            def _drop(reason):
                pre[reason] += 1
                if self.reportRejected:
                    dropped.append({"bboxPx": bbox, "detScore": detScore,
                                    "shortEdge": shortEdge, "area": area,
                                    "poseYaw": None, "posePitch": None,
                                    "embedding": None, "reason": reason})

            # ---- 阶段 1：纯算术判据，零模型开销 ----
            if detScore < self.limits.minDetScore:
                _drop(REASON_LOW_DET)
                continue
            if shortEdge < self.limits.minFaceEdge:
                _drop(REASON_TOO_SMALL)
                continue
            # ---- 阶段 2：姿态（侧脸）----
            poseYaw = posePitch = None
            if self.pose is not None:
                holder = _FaceHolder(bbox)
                try:
                    self.pose.get(img, holder)
                    raw = holder.get("pose")
                    if raw is not None and len(raw) >= 3:
                        # insightface 口径：pose = [pitch, yaw, roll]
                        posePitch = float(raw[0])
                        poseYaw = float(raw[1])
                except Exception as e:            # pragma: no cover - 兜底
                    _LOG.debug("姿态估计失败 bbox=%r: %s: %s", bbox, type(e).__name__, e)
            # pose 取不到时**不拦**（与 S0 一致，见模块头）
            if poseYaw is not None and abs(poseYaw) > self.limits.maxYaw:
                if self.reportRejected:
                    dropped.append({"bboxPx": bbox, "detScore": detScore,
                                    "shortEdge": shortEdge, "area": area,
                                    "poseYaw": poseYaw, "posePitch": posePitch,
                                    "embedding": None, "reason": REASON_SIDE_FACE})
                pre[REASON_SIDE_FACE] += 1
                continue
            # ---- 阶段 3：特征（最贵，放最后）----
            vec = None
            if self.rec is not None:
                try:
                    from insightface.utils import face_align
                    aimg = face_align.norm_crop(img, landmark=np.asarray(kpss[index]),
                                                 image_size=self.rec.input_size[0])
                    vec = self.rec.get_feat(aimg).ravel()
                except Exception as e:            # pragma: no cover - 兜底
                    _LOG.warning("特征提取失败 bbox=%r: %s: %s", bbox,
                                 type(e).__name__, e)
            out.append({
                "bboxPx": bbox, "detScore": detScore,
                "poseYaw": poseYaw, "posePitch": posePitch,
                "embedding": None if vec is None else np.asarray(vec, dtype=np.float32),
            })
        if det.shape[0] == 0:
            pre[REASON_NO_FACE] += 1
        pre["__raw__"] = int(det.shape[0])
        return out, pre, dropped


# ============================================================
# 六、后端二：裸 onnxruntime（SCRFD + ArcFace + 1k3d68）
# ============================================================
#
# 存在的唯一理由：insightface 在 Python 3.13 上可能装不上（依赖 Cython 编译），
# 而这会让整个项目停在这里（开发计划 §6.5 已列为风险）。
# 裸 onnxruntime 零编译、零 insightface 依赖，只要 .onnx 文件在就能跑。
#
# 三个模型的预处理口径**照抄 insightface**（否则 embedding 不可比）：
#   det_10g      1/128 归一化 + mean 127.5 + swapRB + 等比缩放补零到 640x640
#   w600k_r50    1/127.5 归一化 + mean 127.5 + swapRB + 5 点仿射对齐到 112x112
#   1k3d68       1/128 归一化 + mean 127.5 + swapRB + 以脸中心缩放 1.5 倍裁到 192x192
#
# 姿态：1k3d68 输出 1103×3，取后 68 个 3D 点，与 68 点 3D 均值形状做
# 最小二乘仿射拟合 -> P2sRt -> matrix2angle，得到的 [pitch, yaw, roll]
# 与 insightface 完全同源同口径。均值形状文件找不到时 pose 置 None（并 warning）。

#: ArcFace 5 点对齐模板（insightface utils/face_align.arcface_dst，112 尺度）
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32)

#: 模型包内的文件名
DET_FILE = "det_10g.onnx"
REC_FILE = "w600k_r50.onnx"
POSE_FILE = "1k3d68.onnx"


def _umeyama2d(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """最小二乘相似变换（平移 + 旋转 + 各向同性缩放，**不含镜像**），返回 2x3 矩阵。

    等价于 skimage.transform.SimilarityTransform().estimate(src, dst).params[0:2]，
    自己实现是为了**不引入 skimage 依赖**（本项目 requirements 里没有它；
    而 insightface 装不上时更不能指望它的传递依赖还在）。

    解法（一维最大化，闭式解）
    ------------------------
      记中心化后的源点 (x, y)、目标点 (u, v)，要拟合 d = s·R·p（平移另算），R 为旋转 θ。
      展开残差平方，最小化等价于最大化 Σ (R·p)·d
          = cosθ·(Σxu + Σyv) + sinθ·(Σxv - Σyu)
      令 A1 = Σxu + Σyv、A2 = Σxv - Σyu，则最优角 θ = atan2(A2, A1)，
      最优缩放 s = (A1·cosθ + A2·sinθ) / Σ(x² + y²)。
      整段只有 4 个求和，无迭代、无 SVD、不需要 scipy。

    ⚠️ 这里踩过两个坑，两个都只在一类输入上暴露，值得记下来：
      ① 曾经用「2x2 交叉协方差矩阵的主特征向量」求旋转。那是 Procrustes
         **带缩放归一化**的另一支解法，会把纯缩放+平移的输入扭成「旋转 90° + scale=1」。
      ② A2 的符号写反过一次（写成 Σyu - Σxv）。**纯缩放+平移的用例照抄就能通过**
         （此时 A2=0，符号无所谓），只有**带旋转**的真实人脸关键点才暴露 ——
         实测症状是「框完全正确（IoU=1.0）但特征余弦只有 0.86」。
         框对特征错 = 问题在对齐这一段，是个极难定位的组合。
         教训：对齐矩阵这类东西，**必须用带旋转的真实数据验**，不能只验合成数据。
      现在这版与 skimage 在真实人脸关键点上实测 max|Δ| < 1e-9。
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    muS = src.mean(axis=0)
    muD = dst.mean(axis=0)
    s0 = src - muS
    d0 = dst - muD
    x, y = s0[:, 0], s0[:, 1]
    u, v = d0[:, 0], d0[:, 1]
    den = float(np.sum(x * x) + np.sum(y * y))
    if den < 1e-12:
        raise ValueError("相似变换退化：源点全部重合")
    a1 = float(np.sum(x * u) + np.sum(y * v))
    a2 = float(np.sum(x * v) - np.sum(y * u))      # 符号：Σ(x·v) - Σ(y·u)
    theta = math.atan2(a2, a1)
    scale = (a1 * math.cos(theta) + a2 * math.sin(theta)) / den
    cosT = scale * math.cos(theta)
    sinT = scale * math.sin(theta)
    return np.array([
        [cosT, -sinT, muD[0] - (cosT * muS[0] - sinT * muS[1])],
        [sinT, cosT, muD[1] - (sinT * muS[0] + cosT * muS[1])],
    ], dtype=np.float64)


def _matrix2angle(R) -> tuple:
    """旋转矩阵 -> (pitch, yaw, roll)，单位度。口径同 insightface utils/transform。"""
    sy = math.sqrt(R[0, 0] * R[0, 0] + R[1, 0] * R[1, 0])
    if sy >= 1e-6:
        x = math.atan2(R[2, 1], R[2, 2])
        y = math.atan2(-R[2, 0], sy)
        z = math.atan2(R[1, 0], R[0, 0])
    else:
        x = math.atan2(-R[1, 2], R[1, 1])
        y = math.atan2(-R[2, 0], sy)
        z = 0.0
    return (x * 180.0 / math.pi, y * 180.0 / math.pi, z * 180.0 / math.pi)


class _RawOnnxBackend(object):
    """裸 onnxruntime：SCRFD 检测 + ArcFace 特征 + 1k3d68 姿态。"""

    name = "raw-onnx"

    def __init__(self, modelRoot: str = None, detSize: tuple = DEFAULT_DET_SIZE,
                 meanShapePath: str = None, minDetScore: float = None,
                 minFaceEdge: int = None, maxYaw: float = None):
        import onnxruntime as ort

        self.root = modelRoot or defaultModelRoot()
        detFile = os.path.join(self.root, DET_FILE)
        recFile = os.path.join(self.root, REC_FILE)
        for one in (detFile, recFile):
            if not os.path.isfile(one):
                raise FileNotFoundError("裸 ONNX 后端缺少模型文件: %s" % one)
        opts = ort.SessionOptions()
        # 只用 CPU：见模块头「硬约束」
        self.det = ort.InferenceSession(detFile, opts, providers=list(CPU_PROVIDERS))
        self.rec = ort.InferenceSession(recFile, opts, providers=list(CPU_PROVIDERS))
        self.detIn = self.det.get_inputs()[0].name
        self.detOut = [o.name for o in self.det.get_outputs()]
        self.recIn = self.rec.get_inputs()[0].name
        self.recOut = [o.name for o in self.rec.get_outputs()]
        self.detSize = (int(detSize[0]), int(detSize[1]))
        self.fmc = 3
        self.strides = (8, 16, 32)
        self.numAnchors = 2
        self.centerCache = {}
        # SCRFD 的 det_thresh 取 0.5（insightface 缺省）：阈值再高一点会漏掉
        # 0.5~0.6 区间里「刚好够 64px 但不够清晰」的脸，而那正是质量过滤要拦的对象
        self.detThresh = 0.5
        self.nmsThresh = 0.4
        self.limits = _Limits(minDetScore, minFaceEdge, maxYaw)
        self.reportRejected = False
        # ---- 姿态（可选）----
        self.pose = None
        self.meanLmk = None
        poseFile = os.path.join(self.root, POSE_FILE)
        # 该模型把归一化**烤进图里**了（首层是 Sub/Mul），预处理口径与另两个不同，
        # 用错会得到完全离谱的姿态（实测 pitch 170° / yaw 54° / roll 176°）
        self.poseMean, self.poseStd = detectEmbeddedNormalization(poseFile)
        meanFile = meanShapePath or findMeanShape()
        if os.path.isfile(poseFile) and meanFile:
            try:
                self.pose = ort.InferenceSession(poseFile, opts,
                                                 providers=list(CPU_PROVIDERS))
                self.poseIn = self.pose.get_inputs()[0].name
                self.poseOut = [o.name for o in self.pose.get_outputs()]
                self.poseSize = 192
                self.meanLmk = loadMeanShape(meanFile)
                _LOG.info("裸 ONNX 姿态模型预处理: mean=%s std=%s（%s）",
                          self.poseMean, self.poseStd,
                          "图内已归一化" if self.poseStd == 1.0 else "外部归一化")
            except Exception as e:
                _LOG.warning("裸 ONNX 姿态模型不可用(%s: %s)，本轮 yaw 过滤不生效",
                             type(e).__name__, e)
                self.pose = None
        if self.pose is None:
            _LOG.warning("裸 ONNX 后端缺少 %s 或 68 点均值形状，pose 不可用；"
                         "按与 S0 一致的口径，pose 缺失的人脸**不参与 yaw 过滤**",
                         POSE_FILE)

    # ---- SCRFD ----

    def _blob(self, img, size, mean, std):
        cv2 = cv2Module()
        blob = cv2.dnn.blobFromImage(img, 1.0 / std, size,
                                     (mean, mean, mean), swapRB=True)
        return np.ascontiguousarray(blob, dtype=np.float32)

    def _centers(self, height, width, stride):
        key = (height, width, stride)
        cached = self.centerCache.get(key)
        if cached is None:
            centers = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
            centers = (centers * stride).reshape((-1, 2))
            if self.numAnchors > 1:
                centers = np.stack([centers] * self.numAnchors, axis=1).reshape((-1, 2))
            if len(self.centerCache) < 100:
                self.centerCache[key] = centers
            return centers
        return cached

    def _detect640(self, detImg, threshold):
        cv2 = cv2Module()
        blob = self._blob(detImg, (detImg.shape[1], detImg.shape[0]), 127.5, 128.0)
        outs = self.det.run(self.detOut, {self.detIn: blob})
        ih, iw = blob.shape[2], blob.shape[3]
        scoreList, boxList, kpsList = [], [], []
        fmc = self.fmc
        for idx, stride in enumerate(self.strides):
            # ⚠️ SCRFD 导出的 .onnx 形状有两种，必须都兼容（实测 det_10g 是 (N,1)/(N,4)，
            #    而有些导出版本带 batch 维 (1,N,4)）：
            #   ① 带 batch 维：先剥掉第 0 维，否则 centers[:,0] - boxPred[:,0]
            #      会广播成 (1,N)，np.stack 得到 (1,N,4)，后面 vstack 直接报
            #      「维度 1 上 9 vs 4」（insightface 靠 self.batched 标志做同一件事）；
            #   ② scores 必须是 **(N,1) 而不是 (N,)**：np.vstack 会把一维数组当列
            #      拼到 axis 1，于是 (4,) 与 (0,) 混在一起就报「维度 1 上 4 vs 0」。
            #      「某个 stride 无候选、另一个有」是常态（det_thresh=0.5 时很常见），
            #      所以这里显式补成二维列。
            def _one(pos_):
                arr = np.asarray(outs[pos_], dtype=np.float32)
                return arr[0] if arr.ndim == 3 else arr

            scores = _one(idx)
            if scores.ndim == 1:
                scores = scores.reshape(-1, 1)
            elif scores.ndim != 2 or scores.shape[1] != 1:
                raise ValueError("SCRFD score 输出形状异常: %r（模型文件与 SCRFD 不匹配？）"
                                 % (scores.shape,))
            boxPred = _one(idx + fmc) * stride
            kpsPred = _one(idx + fmc * 2) * stride
            if boxPred.ndim != 2 or boxPred.shape[1] != 4:
                raise ValueError("SCRFD bbox 输出形状异常: %r（模型文件与 SCRFD 不匹配？）"
                                 % (boxPred.shape,))
            height, width = ih // stride, iw // stride
            centers = self._centers(height, width, stride)
            if centers.shape[0] != boxPred.shape[0]:
                raise ValueError("SCRFD 锚点数(%d)与预测数(%d)不符：fmc=%d strides=%s "
                                 "numAnchors=%d 与模型不匹配"
                                 % (centers.shape[0], boxPred.shape[0], self.fmc,
                                    list(self.strides), self.numAnchors))
            pos = np.where(scores >= threshold)[0]
            if pos.size == 0:
                scoreList.append(np.empty((0, 1), dtype=np.float32))
                boxList.append(np.empty((0, 4), dtype=np.float32))
                kpsList.append(np.empty((0, 5, 2), dtype=np.float32))
                continue
            x1 = centers[:, 0] - boxPred[:, 0]
            y1 = centers[:, 1] - boxPred[:, 1]
            x2 = centers[:, 0] + boxPred[:, 2]
            y2 = centers[:, 1] + boxPred[:, 3]
            boxes = np.stack([x1, y1, x2, y2], axis=-1)
            # 5 点关键点：模型给的是「相对锚点的偏移」，逐点加回对应锚点
            # （锚点交替取 x / y，与 insightface distance2kps 同一写法）
            kpsCols = []
            for i in range(10):
                kpsCols.append(centers[:, i % 2] + kpsPred[:, i])
            kps = np.stack(kpsCols, axis=-1)
            scoreList.append(scores[pos])
            boxList.append(boxes[pos])
            kpsList.append(kps.reshape((-1, 5, 2))[pos])
        return scoreList, boxList, kpsList

    def _nms(self, dets):
        x1, y1, x2, y2 = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = np.argsort(-dets[:, 4], kind="stable")
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0.0, xx2 - xx1 + 1)
            h = np.maximum(0.0, yy2 - yy1 + 1)
            inter = w * h
            ovr = inter / (areas[i] + areas[order[1:]] - inter)
            order = order[np.where(ovr <= self.nmsThresh)[0] + 1]
        return keep

    def _detect(self, img):
        cv2 = cv2Module()
        tw, th = self.detSize
        imRatio = float(img.shape[0]) / img.shape[1]
        modelRatio = float(th) / float(tw)
        if imRatio > modelRatio:
            newH = th
            newW = int(newH / imRatio)
        else:
            newW = tw
            newH = int(newW * imRatio)
        scale = float(newH) / img.shape[0]
        resized = cv2.resize(img, (newW, newH))
        detImg = np.zeros((th, tw, 3), dtype=np.uint8)
        detImg[:newH, :newW, :] = resized
        scoreList, boxList, kpsList = self._detect640(detImg, self.detThresh)
        if not scoreList or sum(s.size for s in scoreList) == 0:
            return np.empty((0, 5), np.float32), np.empty((0, 5, 2), np.float32)
        scores = np.vstack(scoreList)
        boxes = np.vstack(boxList) / scale
        kpss = np.vstack(kpsList) / scale
        order = np.argsort(-scores.ravel(), kind="stable")
        pre = np.hstack((boxes, scores)).astype(np.float32, copy=False)[order, :]
        kpss = kpss[order, :, :]
        keep = self._nms(pre)
        return pre[keep, :], kpss[keep, :, :]

    # ---- ArcFace ----

    def _embed(self, img, kps):
        cv2 = cv2Module()
        dst = ARCFACE_DST.astype(np.float64)
        M = _umeyama2d(np.asarray(kps, dtype=np.float64), dst)
        aimg = cv2.warpAffine(img, M.astype(np.float32), (112, 112), borderValue=0.0)
        blob = cv2.dnn.blobFromImage(aimg, 1.0 / 127.5, (112, 112),
                                     (127.5, 127.5, 127.5), swapRB=True)
        blob = np.ascontiguousarray(blob, dtype=np.float32)
        return self.rec.run(self.recOut, {self.recIn: blob})[0][0].ravel()

    # ---- 姿态 ----

    def _poseOf(self, img, bbox):
        cv2 = cv2Module()
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        scale = float(self.poseSize) / (max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 1.5)
        # face_align.transform：M 把原图坐标映射到以脸中心为中心、缩放 scale 的
        # 192x192 画布，落点 (96, 96)
        M = np.array([[scale, 0.0, 96.0 - scale * cx],
                      [0.0, scale, 96.0 - scale * cy]], dtype=np.float64)
        aimg = cv2.warpAffine(img, M.astype(np.float32), (192, 192), borderValue=0.0)
        blob = self._blob(aimg, (192, 192), self.poseMean, self.poseStd)
        pred = self.pose.run(self.poseOut, {self.poseIn: blob})[0][0]
        pred = pred.reshape((-1, 3)) if pred.shape[0] >= 3000 else pred.reshape((-1, 2))
        pred = pred[-68:, :]
        pred[:, 0:2] += 1
        pred[:, 0:2] *= (self.poseSize // 2)
        if pred.shape[1] == 3:
            pred[:, 2] *= (self.poseSize // 2)
        IM = cv2.invertAffineTransform(M.astype(np.float32))
        pts = pred.copy()
        xy = np.hstack([pts[:, 0:2], np.ones((pts.shape[0], 1), dtype=pts.dtype)])
        pts[:, 0:2] = (xy @ IM.T)[:, 0:2]
        P = estimateAffine3D23D(np.asarray(self.meanLmk, dtype=np.float64),
                                np.asarray(pts, dtype=np.float64))
        s, R, t = p2sRt(P)
        pitch, yaw, roll = _matrix2angle(R)
        return (pitch, yaw, roll)

    def detect(self, img) -> tuple:
        """分阶段过滤（与 insightface 后端同一套阶段与同一套阈值），返回 (rawFaces, preRejected)。

        阶段顺序同样是「便宜的先过」：detScore/短边（纯算术）-> 姿态（侧脸）-> 特征。
        这里尤其值得：**ArcFace 放最后**，因为侧脸过滤已经先拦掉一批，
        省下的是实打实的 112x112 ResNet50 前向。
        """
        pre = dict((code, 0) for code in REASON_CODES)
        det, kpss = self._detect(img)
        if det.shape[0] == 0:
            pre[REASON_NO_FACE] += 1
            pre["__raw__"] = 0
            return [], pre, []
        out = []
        dropped = []
        for index in range(det.shape[0]):
            bbox = (float(det[index, 0]), float(det[index, 1]),
                    float(det[index, 2]), float(det[index, 3]))
            detScore = float(det[index, 4])
            shortEdge = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])

            def _drop(reason):
                pre[reason] += 1
                if self.reportRejected:
                    dropped.append({"bboxPx": bbox, "detScore": detScore,
                                    "shortEdge": shortEdge, "area": area,
                                    "poseYaw": None, "posePitch": None,
                                    "embedding": None, "reason": reason})

            # ---- 阶段 1：纯算术 ----
            if detScore < self.limits.minDetScore:
                _drop(REASON_LOW_DET)
                continue
            if shortEdge < self.limits.minFaceEdge:
                _drop(REASON_TOO_SMALL)
                continue
            # ---- 阶段 2：姿态（侧脸）----
            pitch = yaw = None
            if self.pose is not None:
                try:
                    pitch, yaw, _roll = self._poseOf(img, bbox)
                except Exception as e:
                    _LOG.debug("裸 ONNX 姿态估计失败 bbox=%r: %s: %s",
                               bbox, type(e).__name__, e)
            if yaw is not None and abs(yaw) > self.limits.maxYaw:
                if self.reportRejected:
                    dropped.append({"bboxPx": bbox, "detScore": detScore,
                                    "shortEdge": shortEdge, "area": area,
                                    "poseYaw": yaw, "posePitch": pitch,
                                    "embedding": None, "reason": REASON_SIDE_FACE})
                pre[REASON_SIDE_FACE] += 1
                continue
            # ---- 阶段 3：特征 ----
            try:
                vec = self._embed(img, kpss[index])
            except Exception as e:
                _LOG.warning("裸 ONNX 特征提取失败 bbox=%r: %s: %s",
                             bbox, type(e).__name__, e)
                pre[REASON_ZERO_VEC] += 1
                continue
            out.append({"bboxPx": bbox, "detScore": detScore,
                        "poseYaw": yaw, "posePitch": pitch, "embedding": vec})
        pre["__raw__"] = int(det.shape[0])
        return out, pre, dropped


def estimateAffine3D23D(X, Y) -> np.ndarray:
    """最小二乘仿射：Y = PX，返回 3x4。口径同 insightface utils/transform。"""
    Xh = np.hstack((X, np.ones((X.shape[0], 1), dtype=X.dtype)))
    P = np.linalg.lstsq(Xh, Y, rcond=None)[0].T
    return P


def p2sRt(P) -> tuple:
    """3x4 仿射相机矩阵 -> (scale, R(3x3), t(3,))。口径同 insightface。"""
    t = P[:, 3]
    R1 = P[0:1, :3]
    R2 = P[1:2, :3]
    s = (np.linalg.norm(R1) + np.linalg.norm(R2)) / 2.0
    r1 = R1 / np.linalg.norm(R1)
    r2 = R2 / np.linalg.norm(R2)
    r3 = np.cross(r1, r2)
    return s, np.concatenate((r1, r2, r3), 0), t


def defaultModelRoot() -> str:
    """模型包目录（与 insightface 的默认位置一致：~/.insightface/models/<pack>）"""
    return os.path.join(os.path.expanduser("~"), ".insightface", "models",
                        DEFAULT_MODEL_PACK)


def detectEmbeddedNormalization(modelFile: str) -> tuple:
    """判断某个 .onnx 是否把归一化**烤进了图里**，返回外部该用的 (mean, std)。

    为什么必须判
    ------------
      buffalo_l 里的三个模型预处理口径**不一样**：
        det_10g      外部归一化 (127.5, 128)
        w600k_r50    外部归一化 (127.5, 127.5)
        1k3d68       **图内已归一化** -> 外部 (0, 1)
      1k3d68 来自 MXNet 导出，首层就是 Sub/Mul；insightface 靠「扫前 8 个图节点，
      见到 Sub/_minus 与 Mul/_mul 就判定为 mxnet 模型」来区分（见 Landmark.__init__）。
      用错口径不会报任何错，只会给出**离谱但看起来像数**的姿态 ——
      实测同一张脸：正确 (pitch 3.1, yaw -2.8)，用错 (pitch 170.3, yaw 53.8, roll 175.8)。
      这类错必须靠"按图判口径"挡住，不能靠记。

    判据与兜底
    ----------
      ① 有 onnx 包 -> 直接扫图节点（与 insightface 同一判据，最准）
      ② 没有 onnx 包（onnxruntime 本身不依赖它）-> **实测法**：
         造一张有内容的脸图（人脸框画个椭圆），两种口径各跑一次，
         取输出 68 点里 x/y 落在画布范围内更多的那一组。
         理由：喂错归一化会让输出饱和/塌缩，坐标成片跑到画布外或全部重合。
    """
    external = (127.5, 128.0)
    embedded = (0.0, 1.0)
    if not modelFile or not os.path.isfile(modelFile):
        return external
    try:
        import onnx
        model = onnx.load(modelFile)
        findSub = findMul = False
        for nid, node in enumerate(model.graph.node[:8]):
            if node.name.startswith("Sub") or node.name.startswith("_minus"):
                findSub = True
            if node.name.startswith("Mul") or node.name.startswith("_mul"):
                findMul = True
            # MXNet 导出的老模型把归一化塞在第一个 BatchNorm 里（节点名 bn_data），
            # 并没有 Sub/Mul 节点。**漏掉这一条会判错**：1k3d68.onnx 的前几个节点是
            # id / bn_data / conv0 / bn0 / relu0，只看 Sub|Mul 会误判成外部归一化。
            if nid < 3 and node.name == "bn_data":
                findSub = True
                findMul = True
        if findSub and findMul:
            return embedded
        return external
    except ImportError:
        pass
    except Exception as e:                      # onnx 包在但模型读不了
        _LOG.debug("扫 onnx 图失败(%s: %s)，改用实测法判归一化口径",
                   type(e).__name__, e)
    # ---- 实测法兜底 ----
    try:
        import cv2
        import onnxruntime as ort
        sess = ort.InferenceSession(modelFile, providers=list(CPU_PROVIDERS))
        names = [o.name for o in sess.get_outputs()]
        inName = sess.get_inputs()[0].name
        side = int(sess.get_inputs()[0].shape[2])
        canvas = np.full((side, side, 3), 200, dtype=np.uint8)
        cv2.ellipse(canvas, (side // 2, side // 2), (side // 4, side // 3),
                    0, 0, 360, (90, 110, 130), -1)
        best, bestScore = external, -1
        for mean, std in (external, embedded):
            blob = cv2.dnn.blobFromImage(canvas, 1.0 / std, (side, side),
                                         (mean, mean, mean), swapRB=True)
            pred = np.asarray(sess.run(names, {inName: blob})[0][0], dtype=np.float64)
            pred = pred.reshape((-1, 3)) if pred.size % 3 == 0 else pred.reshape((-1, 2))
            pred = pred[-68:, :]
            if pred.shape[0] < 68 or pred.shape[1] < 2:
                continue
            xy = (pred[:, 0:2] + 1.0) * (side // 2)
            inside = float(np.mean((xy >= 0) & (xy <= side)))
            spread = float(np.std(xy))          # 全部重合 => 0
            score = inside + min(spread / float(side), 1.0)
            if score > bestScore:
                best, bestScore = (mean, std), score
        _LOG.info("%s 归一化口径由实测法判定为 mean=%s std=%s（onnx 包不可用）",
                  os.path.basename(modelFile), best[0], best[1])
        return best
    except Exception as e:
        _LOG.warning("归一化口径实测失败(%s: %s)，按外部归一化处理", type(e).__name__, e)
        return external


def findMeanShape() -> str:
    """找 68 点 3D 均值形状（裸 ONNX 姿态估计的输入）。

    查找顺序（都不存在时返回 ""，此时 pose 不可用）
    ------------------------------------------------
      1. 环境变量 PHOTO_BROWSER_MEAN_SHAPE 显式指定
      2. 仓库内 **code/data/meanshape_68.npz**（已随仓库分发，见下方说明）
      3. insightface 包内自带的那份（insightface 恰好装了但 import 失败时仍可用）

    为什么把均值形状**作为数据文件随仓库分发**（已决策）
    --------------------------------------------------
      姿态估计的输入就是这张 68×3 的平均脸。少了它，降级到裸 onnxruntime 时
      yaw 过滤会**静默失效**（pose 取不到 -> 不拦侧脸），
      而侧脸过滤是本步的质量门槛之一。
      原先只能靠 insightface 包内自带的那份，那是"安装残留"：
      换机器、换虚拟环境、insightface 升级换路径都可能没有了。
      现在 code/data/meanshape_68.npz 是第一优先来源（1.4KB，纯数据），
      仓库自带 -> 不再依赖任何环境的安装残留。

      该文件由 insightface 的 data/objects/meanshape_68.pkl 转存而来
      （insightface 为 MIT 许可），数值逐位一致，转换方式：
          pickle.load -> np.float64[68,3] -> np.savez_compressed(data=..., source=...)
      loadMeanShape() 两种格式都支持，转成 npz 只是为了不依赖 pickle 与安装路径。

    ⚠️ 路径：**必须是 code/data**，不是 code/src/data。
      本文件在 code/src/engine/face/，往上两级是 code/src，再往上才是 code。
      （先前写成往上两级，结果永远找不到仓库里那份、悄悄退回第 3 条 ——
        这类"路径多一级/少一级"的错不会报错，只会让人以为分发没生效。）
    """
    env = os.environ.get("PHOTO_BROWSER_MEAN_SHAPE", "")
    if env:
        return env
    here = os.path.abspath(_HERE_DIR)                 # code/src/engine/face
    for relative in (("..", "..", "..", "data"),      # code/data   <- 仓库自带
                     ("..", "..", "data")):           # code/src/data（历史布局，兼容）
        # ⚠️ 必须 normpath：直接 join 会留下 "..\..\..\data\meanshape_68.npz" 这种
        #    带 .. 的路径 —— 文件能打开，但日志里看着像"从奇怪的地方加载"，
        #    将来按路径比对/写文档时也对不上。
        local = os.path.normpath(os.path.join(here, *relative))
        candidate = os.path.join(local, "meanshape_68.npz")
        if os.path.isfile(candidate):
            return candidate
    try:
        import insightface
        packaged = os.path.join(os.path.dirname(os.path.abspath(insightface.__file__)),
                                "data", "objects", "meanshape_68.pkl")
        if os.path.isfile(packaged):
            _LOG.warning("仓库内未找到 meanshape_68.npz，退到 insightface 包内副本"
                         "（换环境可能消失，建议把 code/data/meanshape_68.npz 一起带上）")
            return packaged
    except Exception:
        pass
    return ""


def loadMeanShape(path: str):
    """读 68 点均值形状 -> np.float64[68, 3]

    支持 .npz（本项目若自行分发）与 .pkl（insightface 自带，dict{'data': ndarray}）。
    """
    if path.lower().endswith(".npz"):
        data = np.load(path)
        return np.asarray(data["data"], dtype=np.float64)
    import pickle
    with open(path, "rb") as fh:
        obj = pickle.load(fh, encoding="latin1")
    if isinstance(obj, dict):
        for key in ("data", "meanshape", "arr_0"):
            if key in obj:
                obj = obj[key]
                break
    return np.asarray(obj, dtype=np.float64)


# ============================================================
# 七、FaceEngine：对外唯一入口
# ============================================================

class FaceEngine(object):
    """检测 + 提取 + 质量过滤，一步到位。

    一个进程一个实例（模型常驻内存 ~300MB，绝不能每张照片新建）。
    pool 的子进程通过 getEngine() 懒加载**一次**后复用。
    """

    def __init__(self, modelPack: str = DEFAULT_MODEL_PACK,
                 detSize: tuple = DEFAULT_DET_SIZE,
                 minDetScore: float = None, minFaceEdge: int = None,
                 maxYaw: float = None,
                 wantCrop: bool = True, cropSize: int = None,
                 cropSquare: bool = None, cropQuality: int = None,
                 backend: str = "auto", modelRoot: str = None,
                 reportRejected: bool = False):
        self.minDetScore = (basicSettings.MIN_DET_SCORE if minDetScore is None
                            else float(minDetScore))
        self.minFaceEdge = (basicSettings.MIN_FACE_EDGE if minFaceEdge is None
                            else int(minFaceEdge))
        self.maxYaw = (float(basicSettings.MAX_YAW) if maxYaw is None
                       else float(maxYaw))
        self.wantCrop = bool(wantCrop)
        self.cropSize = int(cropSize or basicSettings.FACE_CROP_SIZE)
        self.cropSquare = (bool(basicSettings.FACE_CROP_SQUARE) if cropSquare is None
                           else bool(cropSquare))
        self.cropQuality = (basicSettings.FACE_CROP_QUALITY if cropQuality is None
                            else int(cropQuality))
        self.detSize = tuple(detSize or DEFAULT_DET_SIZE)
        self.modelPack = modelPack
        self._backend = None
        self.backendName = ""
        self.backendError = ""
        self.backendTrace = ""
        self.loadError = ""
        chosen = (backend or "auto").lower()
        limits = (self.minDetScore, self.minFaceEdge, self.maxYaw)
        if chosen in ("auto", "insightface"):
            try:
                self._backend = _InsightFaceBackend(modelPack, self.detSize,
                                                    modelRoot=modelRoot,
                                                    minDetScore=limits[0],
                                                    minFaceEdge=limits[1],
                                                    maxYaw=limits[2])
                self.backendName = self._backend.name
            except Exception as e:
                import traceback
                self.backendError = "%s: %s" % (type(e).__name__, e)
                # 降级是**静默杀伤力最大**的一类故障：主后端挂了就悄悄换算法，
                # 结果照样出得来，只是 yaw 过滤失效、耗时翻几倍、特征分布微变。
                # 所以必须把完整 traceback 一起带走，报到上层日志/汇总里，
                # 否则排查时只剩一句 "TypeError: ..."，定位不到是哪一行。
                self.backendTrace = traceback.format_exc()
                _LOG.error("insightface 后端不可用(%s)，降级裸 onnxruntime\n%s",
                           self.backendError, self.backendTrace)
                if chosen == "insightface":
                    raise
        if self._backend is None:
            self._backend = _RawOnnxBackend(modelRoot=modelRoot, detSize=self.detSize,
                                            minDetScore=limits[0],
                                            minFaceEdge=limits[1],
                                            maxYaw=limits[2])
            self.backendName = self._backend.name
        self.modelRoot = getattr(self._backend, "root", "")
        # reportRejected：把**被丢弃的人脸**也一并回传（只有 detScore/短边/yaw，
        # 没有 embedding）。给 backtest_s0 用 —— 它要复现 S0 的
        # 「先取面积最大的一张脸、再判质量」口径，而本引擎是先过滤再取最大，
        # 两者在一张图里「最大脸是侧脸」时会差出一张样本（实测 100 张里差 1 张，
        # 而这 1 张正好落在组间分布的最大值上，会把推荐阈值从 0.43 顶到 0.64）。
        # 生产路径**必须**保持 False：丢弃的脸就是不该留的。
        self.reportRejected = bool(reportRejected)
        if self.reportRejected:
            self._backend.reportRejected = True

    # ---- 便捷属性 ----

    def describe(self) -> dict:
        info = {
            "backend": self.backendName,
            "backendError": self.backendError,
            "backendTrace": self.backendTrace,
            "modelPack": self.modelPack,
            "modelRoot": self.modelRoot,
            "detSize": self.detSize,
            "minDetScore": self.minDetScore,
            "minFaceEdge": self.minFaceEdge,
            "maxYaw": self.maxYaw,
            "embeddingDim": EMBEDDING_DIM,
            "embeddingBytes": EMBEDDING_BYTES,
            "cropSize": self.cropSize,
            "cropSquare": self.cropSquare,
            "wantCrop": self.wantCrop,
        }
        modules = getattr(self._backend, "loadedModules", None)
        if modules:
            info["loadedModules"] = list(modules)
        # poseReady 两种后端都要报：降级到裸 ONNX 时 loadedModules 是空的，
        # 但只要 meanshape 与 1k3d68 都在，姿态照样可用。
        # ⚠️ 这个字段是"质量过滤有没有在跑"的唯一指示器，必须给全：
        #   姿态缺失时 |yaw|<=45 过滤**静默失效**（不报错、不崩，
        #   只表现为 side_face 计数恒为 0），不报出来就只能靠猜。
        info["poseReady"] = getattr(self._backend, "pose", None) is not None
        return info

    # ---- 主流程 ----

    def extract(self, absPath: str, photoCode: str = "", wantCrop: bool = None) -> dict:
        """跑一张照片。**永不抛异常**（除模型本身不可用）。

        返回
        ----
          dict {
            ok, absPath, photoCode, faces[list[dict]], rejected{dict},
            kept, rawCount, imgW, imgH, elapsed, errCode, errMsg
          }
          faces 里每项是 FaceHit.toDict()（embedding 已转 2048 字节、已 L2 归一化）
        """
        import time
        start = time.perf_counter()
        result = {"ok": False, "absPath": absPath, "photoCode": photoCode,
                  "faces": [], "rejected": dict((c, 0) for c in REASON_CODES),
                  "droppedFaces": [],
                  "kept": 0, "rawCount": 0, "imgW": 0, "imgH": 0,
                  "elapsed": 0.0, "errCode": 0, "errMsg": ""}
        doCrop = self.wantCrop if wantCrop is None else bool(wantCrop)

        if not absPath or not os.path.isfile(absPath):
            result["rejected"][REASON_NO_FILE] = 1
            result["errMsg"] = "原图不存在"
            result["elapsed"] = time.perf_counter() - start
            return result
        try:
            img = imreadUnicode(absPath)
        except Exception as e:
            result["rejected"][REASON_DECODE_FAIL] = 1
            result["errMsg"] = "%s: %s" % (type(e).__name__, e)
            result["elapsed"] = time.perf_counter() - start
            return result
        if img is None:
            result["rejected"][REASON_DECODE_FAIL] = 1
            result["errMsg"] = "解不出像素（文件损坏或扩展名骗人）"
            result["elapsed"] = time.perf_counter() - start
            return result

        result["imgH"], result["imgW"] = int(img.shape[0]), int(img.shape[1])
        try:
            rawFaces, preRejected, droppedFaces = self._backend.detect(img)
        except Exception as e:
            result["rejected"][REASON_DECODE_FAIL] = 1
            result["errMsg"] = "检测失败 %s: %s" % (type(e).__name__, e)
            _LOG.error("extract 检测失败 %s: %s", absPath, e)
            result["elapsed"] = time.perf_counter() - start
            return result
        # rawCount = **检测到的**人脸数（过滤之前），验收第 3 条要拿它对账
        result["rawCount"] = int(preRejected.get("__raw__", len(rawFaces)))

        hits, rejected = filterAndRank(
            rawFaces, result["imgW"], result["imgH"],
            minDetScore=self.minDetScore, minFaceEdge=self.minFaceEdge,
            maxYaw=self.maxYaw)
        # 后端已分阶段拦掉的不重复计数（filterAndRank 对存活者再判一次，幂等）。
        # ⚠️ no_face 例外：后端与 filterAndRank 都会报「这张图一张脸都没有」，
        #    相加会把 7 张无脸照片报成 15 张。这类"同一件事被两处各数一遍"
        #    的错在汇总里最容易被当成真实结论，必须显式取大而不是相加。
        for code, count in (preRejected or {}).items():
            if code == "__raw__" or not count:
                continue
            if code == REASON_NO_FACE:
                rejected[code] = max(rejected.get(code, 0), int(count))
            else:
                rejected[code] = rejected.get(code, 0) + int(count)
        result["rejected"] = rejected
        # 只有 backtest_s0 要「被丢弃的脸」的明细，用于复现 S0 的取脸口径；
        # 生产路径这里是空列表，不给落库流程制造任何多余数据
        result["droppedFaces"] = droppedFaces or []

        for hit in hits:
            if photoCode:
                hit.faceCode = deriveFaceCode(photoCode, hit.bbox)
            result["faces"].append(hit.toDict())

        if doCrop and hits:
            # 一张照片只把 cv2 图转成 PIL **一次**，所有脸共用：
            # 不这么做的话每张脸都要 Pillow 重新解码一次 12MP 原图（~0.2 秒），
            # 一张 4 人的合影光解码就 0.8 秒 —— 实测这是单张耗时里最大的一块。
            # 裁剪格式仍只有 faceCropper 一份实现（口径不分裂）。
            pilImage = self._toPilImage(img)
            try:
                for hit in hits:
                    data, errMsg = faceCropper.safeCrop(
                        absPath, hit.bbox, outSize=self.cropSize,
                        square=self.cropSquare, image=pilImage)
                    if data is None:
                        _LOG.warning("人脸裁剪失败 %s bbox=%s: %s",
                                     absPath, hit.bbox, errMsg)
                    # toDict 已经把 cropData 放进结果 dict 了，这里回填
                    for one in result["faces"]:
                        if one["faceCode"] == hit.faceCode:
                            one["cropData"] = data
                            break
            finally:
                if pilImage is not None:
                    try:
                        pilImage.close()
                    except Exception:
                        pass
        result["kept"] = len(result["faces"])
        result["ok"] = True
        result["elapsed"] = time.perf_counter() - start
        return result

    @staticmethod
    def _toPilImage(img):
        """cv2 BGR ndarray -> PIL RGB Image（EXIF 方向已由 cv2 摆正）。

        cv2.imdecode(IMREAD_COLOR) 自 OpenCV 3.4.1 起**默认按 EXIF 旋转**，
        所以拿到的图已经是正的，与 faceCropper 里 exif_transpose 之后一致。
        """
        if img is None:
            return None
        try:
            import cv2
            from PIL import Image
            if img.ndim == 2:
                return Image.fromarray(img, mode="L").convert("RGB")
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb, mode="RGB")
        except Exception as e:                    # pragma: no cover - 兜底
            _LOG.debug("BGR->PIL 转换失败，回退到按路径解码: %s: %s",
                       type(e).__name__, e)
            return None

    def extractHits(self, absPath: str, photoCode: str = "",
                    wantCrop: bool = None) -> list:
        """只要 FaceHit 对象（主进程内联调试 / 单测用）"""
        return [FaceHit.fromDict(d) for d in self.extract(absPath, photoCode,
                                                         wantCrop)["faces"]]


# ============================================================
# 八、进程内单例（**每进程懒加载一次**）
# ============================================================

_ENGINE = {"obj": None, "key": None}
_ENGINE_LOCK = threading.Lock()


def getEngine(**kwargs) -> FaceEngine:
    """取本进程的引擎单例，**首次调用才加载模型**。

    为什么必须是「每进程一次」而不是「每张一次」
    ------------------------------------------
      buffalo_l 的 SCRFD(143MB) + ArcFace(174MB) 每次重新建 onnxruntime Session
      实测 >3 秒，而单张检测+提取本身只要 0.3~0.8 秒。子进程若每张都重建，
      并行度越高越慢（4 个进程各加载一次 = 1.2GB 内存 + 12 秒纯开销）。
      进程池的 worker 是长驻的，懒加载一次即可摊薄到每进程一次。

    kwargs 参与 key：换了阈值/模型必须拿到新实例，否则第二个调用方拿到的是
    第一个调用方的阈值（这类"参数悄悄不生效"的错极难查）。
    """
    key = tuple(sorted((k, str(v)) for k, v in kwargs.items()))
    with _ENGINE_LOCK:
        if _ENGINE["obj"] is not None and _ENGINE["key"] == key:
            return _ENGINE["obj"]
        engine = FaceEngine(**kwargs)
        _ENGINE["obj"] = engine
        _ENGINE["key"] = key
        return engine


def resetEngine() -> None:
    """丢掉单例（单测 / 切换模型用；顺手把模型内存还给 OS）"""
    with _ENGINE_LOCK:
        _ENGINE["obj"] = None
        _ENGINE["key"] = None


#: 子进程里 ORT 每张图的 intra-op 线程数（见 setOrtThreadLimit）
CHILD_ORT_THREADS: int = 1

_ORT_PATCHED = {"done": False}


def setOrtThreadLimit(threads: int = CHILD_ORT_THREADS) -> int:
    """把 onnxruntime 的 intra-op 线程数钉死（**必须在建 Session 之前调**）。

    为什么非钉不可（本机实测踩过）
    ----------------------------
      onnxruntime 的 SessionOptions 默认 intra_op_num_threads = 0，含义是
      **"用满所有物理核"**。本机 16 核，进程池开 8 个 worker 时就是
      8 × 16 = 128 个计算线程抢 16 个核，线程切换的开销远大于计算本身：
      同样 100 张验证集，单进程均值 0.88 秒，8 进程反而涨到 **4.47 秒**。
      并行度越高越慢 —— 这种"加了并发反而更慢"的现象第一反应总是查参数，
      查线程数是最容易漏掉的一条。
      钉成每进程 1 线程之后，8 × 1 = 8 个线程，16 核用掉一半，
      剩下的一半留给主进程写库与 I/O。

    为什么要 patch onnxruntime.InferenceSession 这个公开符号
    ------------------------------------------------------
      insightface 内部自己 `onnxruntime.InferenceSession(model_file, providers=...)`，
      不接受外部传入的 SessionOptions（ArcFaceONNX/SCRFD 的构造函数只认 model_file
      与可选的 session）。要统一控制线程数，只有两条路：
        ① 绕过 FaceAnalysis、自己拼 SCRFD/ArcFace（等于放弃本项目的主后端）；
        ② 在建模型**之前**把它的工厂函数包一层。
      选 ②：改动集中在一个函数里、可回退、对 insightface 零侵入，
      且降级后端（本模块自己建的 Session）自动一起受益。

    单进程内联（workers=1）时用 threads=0 = 不限制，让 ORT 用满所有核 ——
    那种情况下只有一个进程，全用才是对的。
    """
    import onnxruntime as ort

    if _ORT_PATCHED["done"] or int(threads) <= 0:
        return int(threads)
    limit = int(threads)
    original = ort.InferenceSession

    # ⚠️⚠️ 必须替换成**类**，不能替换成函数
    # ---------------------------------------
    #   insightface/model_zoo/model_zoo.py 第 53 行有一句：
    #       class PickableInferenceSession(onnxruntime.InferenceSession):
    #   也就是说第三方代码会**继承** onnxruntime.InferenceSession。
    #   把类换成函数后，这行 import 时刻就炸：
    #       TypeError: function() argument 'code' must be code, not str
    #   （那句报错完全指不到真正的原因，只说"要 code 不是 str"，
    #     第一次排查极易误判成 pickle / exec 的问题。）
    #   这个坑在主进程里看不到：主进程往往**先** import 过 insightface
    #   （模块缓存命中，patch 不生效），只有 spawn 出来的子进程
    #   才会真的走到 patch 后的类上 —— 于是"主进程好好的、子进程全降级"，
    #   降级后 yaw 过滤静默失效、耗时翻 4 倍、特征分布还略有变化。
    #   故此：子类化是唯一安全的做法。
    class _ThreadLimitedInferenceSession(original):
        """行为与原类完全一致，只多了「默认 SessionOptions 带线程上限」"""

        def __init__(self, pathOrBytes, sess_options=None, providers=None,
                     provider_options=None, **kwargs):
            if sess_options is None:
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = limit
                opts.inter_op_num_threads = 1
                sess_options = opts
            original.__init__(self, pathOrBytes, sess_options, providers,
                              provider_options, **kwargs)

    _ThreadLimitedInferenceSession._photobrowser_threads = limit
    ort.InferenceSession = _ThreadLimitedInferenceSession
    _ORT_PATCHED["done"] = True
    _LOG.debug("onnxruntime intra-op 线程数钉为 %d/进程（子类注入，第三方可继续继承）",
               limit)
    return limit


def resetOrtThreadLimit() -> None:
    """解除线程限制（单测收尾用）"""
    global _ORT_PATCHED
    _ORT_PATCHED = {"done": False}


if __name__ == "__main__":
    import json

    print("faceEngine _VERSION:", _VERSION)
    print("阈值: detScore>=%s  shortEdge>=%s  |yaw|<=%s"
          % (basicSettings.MIN_DET_SCORE, basicSettings.MIN_FACE_EDGE,
             basicSettings.MAX_YAW))
    print("embedding: float32[%d] 小端 %d 字节" % (EMBEDDING_DIM, EMBEDDING_BYTES))
    code = deriveFaceCode("PH_abc", (0.1, 0.2, 0.3, 0.4))
    print("faceCode 示例:", code)
    print("人脸图相对路径:", thumbStore.face_relpath(code))
    print("bbox 入库格式:", faceCropper.formatFaceBox((0.1, 0.2, 0.3, 0.4)))
    eng = getEngine()
    print("后端:", json.dumps(eng.describe(), ensure_ascii=False))
