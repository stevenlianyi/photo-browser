#! /usr/bin/env python3
#encoding: utf-8

#Filename: eraSample.py
#Description: photo-browser 年代抽样接口（R11 · DR-45 ~ DR-50）—— P-05 Tab3
#
# 为什么自成一个模块（而不是塞进 api/browse.py）
# --------------------------------------------
#   api/browse.py 的文件头第 ② 条纪律是「**整个 browse 模块一个字都不写**」
#   （含不发 review 日志）。年代抽样要**写两张自己的表 + 落 zip 产物**，
#   塞进去会让那条纪律变成一句谎话 —— 而接手的人会信它。
#   与 api/place.py 同理：功能自成一体的命名空间就自成一个模块。
#
# 端点（5 个）
# ----------
#   POST   /api/persons/{personCode}/era-sample   生成一次抽样（写批次 + 明细）
#   GET    /api/persons/{personCode}/era-samples  该人的历史批次列表
#   GET    /api/era-samples/{batchCode}           某批次的头 + 明细
#   POST   /api/era-samples/{batchCode}/zip       打包 zip（幂等）
#   GET    /api/era-samples/{batchCode}/zip       下载 zip（没有就**先自动打**）
#
# 四条纪律
# --------
#   ① **只动 pb_era_sample* 两张自己的表**：抽样不改归属、不改质心、不改桶、
#      不写 pb_review_log（DR-49）。它是只读统计 + 一批自己的产物。
#   ② **不碰 `photo\`**：原图只读，zip 落 `exports\era-sample\`（DR-48）。
#   ③ 错误一律 {code, message}：engine 的异常在这里映射成 400/404，
#      不让 pydantic 的 422 或裸 Exception 的 500 漏出去。
#   ④ **不提供 DELETE**（原规划里的 P1「软删批次」已去掉）：全站没有任何
#      DELETE 端点，这是被 `test_api_smoke.test_openapiHasNoExportOrDelete`
#      逐条扫 OpenAPI 守住的**项目级不变量**（DR-17 不做导出 / DR-19 只停用
#      不删除）。为「批次」破这个例，代价是那条闸门从此形同虚设。
#      不提供删除也不影响使用：一批 = 年份数行（≤ 几十行）纯元数据，
#      界面只列最近 20 批 —— 与「不自动删用户文件」（DR-24）同一条思路。

import os
import sys
from typing import Optional

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))           # .../api
_SRC_DIR = os.path.dirname(_HERE_DIR)                            # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api import dto                                               # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from engine.sample import eraSampler as eraSampler                # noqa: E402
from processor.media import eraPack as eraPack                     # noqa: E402

from fastapi import APIRouter, Query                              # noqa: E402
from fastapi.responses import FileResponse                        # noqa: E402

_VERSION = "20261011"

_LOG = misc.setLogNew("apiEraSample", "apierasample.log")

router = APIRouter(tags=["eraSample"])

#: 缩略图尺寸（与人物详情其余区块一致：400 足够，别让 Tab3 拖慢加载）
THUMB_SIZE: int = 400


# ============================================================
# 一、响应整形
# ============================================================

def batchSummary(row: dict, withZipUrl: bool = True) -> dict:
    """pb_era_sample 行 -> 批次响应体。

    ⚠️ 人名取**批次里的快照** `personName`，不现查 pb_person：
       人物改名/停用之后，历史批次仍要显示「当时那一次是谁的」。
       响应里同时给 `displayName`（前端统一叫法）与 `personName`（库列名），
       两者恒等 —— 任一端改字段名时另一个还在，不会静默 undefined。
    """
    code = str(row.get("batchCode") or "")
    name = str(row.get("personName") or "")
    return {
        "batchCode": code,
        "personCode": str(row.get("personCode") or ""),
        "displayName": name,
        "personName": name,
        "grain": str(row.get("grain") or "year"),
        "mode": str(row.get("mode") or basicSettings.ERA_SAMPLE_MODE_DEFAULT),
        "randomSeed": str(row.get("randomSeed") or ""),
        "yearFrom": _intOrNone(row.get("yearFrom")),
        "yearTo": _intOrNone(row.get("yearTo")),
        # 布尔一律转 bool：库里是 0/1，给前端 true/false 少一次隐式判断
        "includeNoYear": int(row.get("includeNoYear") or 0) == 1,
        "onlyConfirmed": int(row.get("onlyConfirmed") or 0) == 1,
        "excludeDuplicate": int(row.get("excludeDuplicate") or 0) == 1,
        "candidatePhotoCount": int(row.get("candidatePhotoCount") or 0),
        "candidateYearCount": int(row.get("candidateYearCount") or 0),
        "photoCount": int(row.get("photoCount") or 0),
        "zipState": str(row.get("zipState") or "NONE"),
        "zipSize": int(row.get("zipSize") or 0),
        "zipPhotoCount": int(row.get("zipPhotoCount") or 0),
        "zipMissingCount": int(row.get("zipMissingCount") or 0),
        "zipYMDHMS": row.get("zipYMDHMS") or None,
        "errMsg": row.get("errMsg") or None,
        "note": row.get("note") or None,
        "regYMDHMS": str(row.get("regYMDHMS") or ""),
        "zipUrl": ("/api/era-samples/%s/zip" % code) if withZipUrl and code else None,
    }


def itemSummary(row: dict) -> dict:
    """一条明细（已被 itemRows JOIN 上 pb_photo）-> 队列里的一张卡片。

    `probability` = weight / weightSum —— **两种抽法同一套解释**
      （uniform 只是「所有权重都是 1」的特例，见 eraSampler.planPicks）。
      它让界面能回答「这一张是 1/14 抽出来的」这种问题，
      而 candidateCount==1 时恒为 1.0（不是 null：「没得选」也是一种概率）。
    """
    photoCode = str(row.get("photoCode") or "")
    yearLabel = str(row.get("yearLabel") or "")
    weight = _floatOrNone(row.get("weight"))
    weightSum = _floatOrNone(row.get("weightSum"))
    probability = None
    if weight is not None and weightSum:
        probability = round(weight / weightSum, 6)
    noYear = yearLabel == basicSettings.ERA_SAMPLE_NOYEAR_LABEL
    return {
        "pickOrder": int(row.get("pickOrder") or 0),
        "yearLabel": yearLabel,
        # 展示用文案在**服务端**定一次，别让前端各自 if 一下（两处翻译必然漂移）
        "yearText": "(无拍摄年份)" if noYear else yearLabel,
        "shotYear": _intOrNone(row.get("shotYear")),
        "photoCode": photoCode,
        "faceCode": row.get("faceCode") or None,
        "thumbUrl": "/api/thumb/%s?size=%d" % (photoCode, THUMB_SIZE),
        "originalUrl": "/api/original/%s" % photoCode,
        "candidateCount": int(row.get("candidateCount") or 0),
        "probability": probability,
        "weight": weight,
        "weightSum": weightSum,
        "quality": _floatOrNone(row.get("quality")),
        "detScore": _floatOrNone(row.get("detScore")),
        "isConfirmed": int(row.get("isConfirmed") or 0) == 1,
        "takenAt": row.get("takenAt") or None,
        "relPath": str(row.get("relPath") or ""),
        "photoFaceCount": int(row.get("faceCount") or 0),
        "isMissing": int(row.get("isMissing") or 0) == 1,
    }


def _floatOrNone(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _intOrNone(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _batchBody(batchCode: str) -> dict:
    """批次 + 明细（**唯一**一份读路径）。

    POST 生成完也走这里而不是直接拼一份 —— 两条路径各拼一份迟早漂移
    （最典型：刚生成的有 thumbUrl、刷新后没有，而用户只在刷新后才发现）。
    """
    got = eraSampler.getBatch(batchCode)
    if not got:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "batchCode=%s 不存在（或已被删除）" % batchCode)
    return {"ok": True,
            "batch": batchSummary(got["batch"]),
            "items": [itemSummary(one) for one in got["items"]]}


def _mapEngineError(e: Exception):
    """engine 的异常 -> api 错误码（**不靠匹配文案**，靠异常类型）。

    三个子类分三条码，因为**用户要做的下一步完全不同**：
      EraPersonNotFoundError -> 404 跳回列表
      EraDbError             -> 500 「库还没迁移 / 落库失败」（要他去跑命令）
      EraSampleError         -> 400 改参数
    子类顺序有讲究：`EraDbError` / `EraPersonNotFoundError` 都是 `EraSampleError`
    的子类，**必须排在父类之前**，否则永远走不到。
    """
    if isinstance(e, eraSampler.EraPersonNotFoundError):
        return dto.ApiError(dto.CODE_NOT_FOUND, str(e))
    if isinstance(e, eraSampler.EraDbError):
        return dto.ApiError(dto.CODE_DB_ERROR, str(e))
    if isinstance(e, eraSampler.EraSampleError):
        return dto.ApiError(dto.CODE_PARAM_INVALID, str(e))
    return dto.ApiError(dto.CODE_INTERNAL, "生成抽样失败：%s" % e)


# ============================================================
# 二、生成 / 列表 / 明细
# ============================================================

@router.post("/persons/{personCode}/era-sample",
             summary="按年抽样：给这个人每年抽一张照片（写批次 + 明细）")
def createEraSample(personCode: str, body: dto.EraSampleBody):
    """生成一次抽样。

    ⚠️ **每次调用都新建一个批次**（不覆盖旧批次）：加权随机本来就允许
       「这次不满意，再抽一次」；覆盖掉就丢掉了用户刚看过的那一版
       （而 zip 可能已经发出去了）。历史批次可回看、可下载。

    ⚠️ 候选为空**不是错误**（200 + photoCount=0）：「这个人没有带年份的照片」
       是要如实告诉用户的事实，界面据此显示信息性空状态（DR-39 的纪律）。
       把它变成 4xx 会让前端弹一个红色 Toast，而真相只是一条空队列。
    """
    try:
        timeout = eraSampler.generate(
            personCode, grain=body.grain, mode=body.mode, seed=body.seed,
            yearFrom=body.yearFrom, yearTo=body.yearTo,
            includeNoYear=body.includeNoYear, onlyConfirmed=body.onlyConfirmed,
            excludeDuplicate=body.excludeDuplicate, note=body.note)
    except eraSampler.EraSampleError as e:
        raise _mapEngineError(e)
    return _batchBody(str(timeout.get("batchCode")))


@router.get("/persons/{personCode}/era-samples",
            summary="某个人的年代抽样历史批次（最新一次在最前）")
def listEraSamples(personCode: str,
                   size: int = Query(default=20, ge=1, le=eraSampler.MAX_LIST_SIZE)):
    """历史批次列表（**不含明细** —— 列表只需要头，明细点进去再取）。"""
    code = str(personCode or "").strip()
    if not code:
        raise dto.ApiError(dto.CODE_PARAM_INVALID, "personCode 不能为空")
    rows = eraSampler.listBatches(code, size=size)
    return {"ok": True, "personCode": code, "total": len(rows),
            "items": [batchSummary(one, withZipUrl=False) for one in rows]}


@router.get("/era-samples/{batchCode}",
            summary="某次年代抽样的批次头 + 按年份排队的明细")
def getEraSample(batchCode: str):
    return _batchBody(str(batchCode or "").strip())


# ============================================================
# 三、zip
# ============================================================

@router.post("/era-samples/{batchCode}/zip",
             summary="打包这次抽样的照片（幂等；force=true 重打）")
def buildEraSampleZip(batchCode: str, body: Optional[dto.EraSampleZipBody] = None):
    """请求体可以完全不传（等价于 force=false）—— 打包大多是「给我一份」。"""
    code = str(batchCode or "").strip()
    batch, info = _ensurePack(code, bool(body and body.force))
    return _packBody(code, batch, info)


@router.get("/era-samples/{batchCode}/zip",
            summary="下载这次抽样的 zip（还没有产物就**先自动打**）")
def downloadEraSampleZip(batchCode: str):
    """下载 zip。

    ⚠️ `zipState=NONE` 时**先自动生成再返回**，而不是回 404：
       用户的本能动作是点「下载」，不是先点「生成 zip」。
       把它做成两步等于要求用户先学会我们的内部状态机。
    """
    code = str(batchCode or "").strip()
    batch, info = _ensurePack(code, False)
    # ⚠️ 本机绝对路径**不进响应体**（它会出现在 /docs 的示例与前端缓存里）。
    #    这里直接问打包结果要，而不是从响应里回读 —— 那正是上一次的 bug：
    #    zipAbsPath 不在对外契约里，回读拿到 None，产物明明打好了却报 500。
    path = str(info.get("zipAbsPath") or "")
    if not os.path.isfile(path):
        raise dto.ApiError(dto.CODE_INTERNAL, "zip 产物不存在：%s" % path)
    return FileResponse(path, media_type="application/zip",
                        filename=eraPack.downloadName(batch, code))


def _ensurePack(batchCode: str, force: bool) -> tuple:
    """校验批次 + 打包，返回 `(批次行, 打包结果)`。

    打包结果里的 `zipAbsPath` 是**本机路径**，只给 GET 下载用；
    对外响应一律走 `_packBody`（那里把它摘掉）。
    """
    code = str(batchCode or "").strip()
    batch = eraSampler.batchRow(code)
    if not batch:
        raise dto.ApiError(dto.CODE_NOT_FOUND,
                           "batchCode=%s 不存在（或已被删除）" % code)
    if int(batch.get("photoCount") or 0) <= 0:
        # 空批次不许打包：会生成一个只有 manifest/README 的 zip，
        # 而用户以为「照片导出成功了」。
        raise dto.ApiError(dto.CODE_PARAM_INVALID,
                           "这一批没有抽到任何照片，无法打包 zip")
    try:
        return batch, eraPack.ensureZip(code, force=force, batch=batch)
    except eraPack.EraPackError as e:
        raise dto.ApiError(dto.CODE_INTERNAL, str(e))
    except Exception as e:                                        # noqa: BLE001
        _LOG.error("打包异常 %s: %s", code, e)
        raise dto.ApiError(dto.CODE_INTERNAL, "打包失败：%s" % e)


def _packBody(code: str, batch: dict, info: dict) -> dict:
    """打包结果 -> 对外响应体（**不含本机路径**）。"""
    return {"ok": True, "batchCode": code, "zipState": info.get("zipState"),
            "cached": bool(info.get("cached")),
            "zipSize": int(info.get("zipSize") or 0),
            "zipPhotoCount": int(info.get("zipPhotoCount") or 0),
            "zipMissingCount": int(info.get("zipMissingCount") or 0),
            "missing": info.get("missing") or [],
            "downloadUrl": "/api/era-samples/%s/zip" % code,
            "fileName": eraPack.downloadName(batch, code)}
