#! /usr/bin/env python3
#encoding: utf-8

#Filename: bucketPool.py
#Description: photo-browser 年代档照片池（R15 · DR-56 ~ DR-58）——
#             某个人在某个**年代档**下的全部照片（按档打包 zip 的取材口）
#
# 与 eraSampler 的分工
# --------------------
#   eraSampler.py  : 「按**年**抽一张」——写 pb_era_sample* 两张表。
#   bucketPool.py  : 「按**年代档**取**全部**」——纯只读，不抽样、不落库。
#
#   两者取材口径**同源**（都 JOIN pb_face/pb_photo、双 delFlag、
#   一张照片只算一次、代表脸取质量最高那张），差的只是「取多少」：
#   抽样取 1，打包取全部。所以本模块**复用** eraSampler.representativeRows，
#   不另写一份「代表脸」规则 —— 两份规则迟早漂移，而那会让同一个人的
#   时间轴与打包包里的张数对不上。
#
# 四条硬口径（每一条写错都会给出一个「看起来完全合理」的错答案）
# ------------------------------------------------------------
#   ① **档键 = `pb_face.shotBucket` 原值**，不在 SQL 里反解年份。
#      桶宽随生日自适应（童年 3 年 / 成年 10 年 / 降级 5 年），
#      反解再匹配等于重写一遍分桶逻辑，必然与 Tab1 时间轴漂移。
#   ② **一张照片只算一次**：一张合影里同一个人可能有 2 张脸，
#      不去重会把「179 张」数成「183 张」（api/browse.py 的时间轴已踩过）。
#   ③ **空桶键归「无拍摄年份」**：库里 shotBucket 为 ''/NULL 的行自成一档。
#      对外用 `NOYEAR` 当键（**不是**时间轴里那个中文标签）——
#      它要进 URL 与 zip 文件名，中文标签在有些环节会出事。
#   ④ **排除重复件**（用户 Q-4 选定）；**不排除** `isMissing=1` ——
#      原图不在磁盘上的要**如实报数**，而不是在查询阶段悄悄滤掉
#      （滤掉的话界面永远显示「缺失 0 张」，而用户拿到的是少了几张的包）。
#
# 本模块**只读**：不写任何表、不碰 photo 目录、不落 zip（那是 media 层的事）。

import os
import re
import sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/sample
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from engine.sample import eraSampler as eraSampler                # noqa: E402

_VERSION = "20261013"

_LOG = misc.setLogNew("bucketPool", "bucketpool.log")

#: 「无拍摄年份」档的**键**（进 URL 与文件名）。
#: 与 basicSettings.ERA_SAMPLE_NOYEAR_LABEL 同值（R11 抽样的 yearLabel 也用这个串），
#: 同一件事在整个项目里只有一个字符串。
NO_BUCKET_KEY: str = basicSettings.ERA_SAMPLE_NOYEAR_LABEL

#: 「无拍摄年份」档的**显示文案**（与 api/browse.py 的时间轴一致）。
NO_BUCKET_TEXT: str = "(无拍摄年份)"

#: 合法的档键形状：`2005-2009` / `NOYEAR` / `ALL`（策略 none 时的全局桶）。
#: ⚠️ 刻意**只放行** `[A-Za-z0-9_-]`：档键会同时用作 URL 路径段与 zip 文件名，
#:    放行中文/点/斜杠等于把「文件名净化」这件事交给运气。
_BUCKET_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class BucketPoolError(Exception):
    """年代档照片池取数失败（档键非法 / 人物不存在）。"""


# ============================================================
# 一、档键
# ============================================================

def bucketKeyOf(shotBucket) -> str:
    """库里的 `pb_face.shotBucket` -> 对外**档键**。空 -> `NOYEAR`。

    ⚠️ 只做「空 -> NOYEAR」这一层归一，**不**顺手把 `ALL` 也并进来：
       策略 `none` 时全库落在 `ALL` 桶，那是一个**真实的档**，
       时间轴上也会显示成 ALL。在这里吞掉它，用户会在时间轴看到
       一个点不开的档（界面上有、库里查不到）。
    """
    return str(shotBucket or "").strip() or NO_BUCKET_KEY


def bucketTextOf(bucketKey) -> str:
    """档键 -> 界面显示文案（`NOYEAR` -> 「(无拍摄年份)」）。

    展示用文案在**服务端**定一次（与 eraSample.itemSummary 的 `yearText`
    同一条纪律）：让前端各自 `if` 一下，两处翻译必然漂移。
    """
    key = str(bucketKey or "").strip()
    return NO_BUCKET_TEXT if key in ("", NO_BUCKET_KEY) else key


def timelineKeyOf(bucketKey) -> str:
    """对外档键 -> **时间轴（api/browse.py）用的那个键**。

    时间轴把空桶写作「(无拍摄年份)」，打包把它们写作 `NOYEAR`（它要进 URL 与
    文件名）。两者必须能对上，否则前端在时间轴的横带上找不到对应的打包项 ——
    表现是「按钮永远显示未打包」，而库里明明已经有那个 zip 了。
    """
    key = str(bucketKey or "").strip()
    return NO_BUCKET_TEXT if key in ("", NO_BUCKET_KEY) else key


def normalizeKey(bucketKey) -> str:
    """URL / 请求体里传来的档键 -> 规范档键。非法直接抛。

    ⚠️ 校验而不是「净化后照用」：档键既进 SQL 的等值条件、又进 zip 文件名，
       一个 `../` 传进来若被净化成别的档，用户会拿到**别人的照片包**，
       而全程没有任何报错。宁可 400。
    """
    text = str(bucketKey or "").strip()
    if text in ("", NO_BUCKET_KEY, NO_BUCKET_TEXT):
        return NO_BUCKET_KEY
    if not _BUCKET_KEY_RE.match(text):
        raise BucketPoolError("年代档键非法: %r（只允许字母/数字/下划线/连字符）" % bucketKey)
    return text


# ============================================================
# 二、照片池（只读查询）
# ============================================================

def poolSql(excludeDuplicate: bool = True) -> str:
    """照片池 SQL（**唯一一份**）。

    为什么把 SQL 生成单独抽出来：口径要能被单测**逐条钉住**
    （哪一条 WHERE 掉了，测试直接断言 SQL 文本里有没有它），
    而不是靠「跑一遍看看数字对不对」—— 与 eraSampler.candidateSql 同一理由。
    """
    where = ["f.personCode = %s", "f.delFlag = %s", "p.delFlag = %s",
             "f.isStranger = 0"]
    if excludeDuplicate:
        # ⚠️ 注意**不**加 `p.isMissing = 0`：原图不在磁盘上的也要取出来，
        #    让打包层跳过并计数（口径 ④）。
        where.append("p.isDuplicate = %s")
    return ("SELECT f.shotBucket AS shotBucket, f.quality AS quality,"
            " f.detScore AS detScore, f.isConfirmed AS isConfirmed,"
            " f.isStranger AS isStranger,"
            " p.photoCode AS photoCode, p.relPath AS relPath,"
            " p.takenAt AS takenAt, p.shotYear AS shotYear,"
            " p.shotYearOverride AS shotYearOverride,"
            " p.fileSize AS fileSize, p.isMissing AS isMissing,"
            " p.mimeType AS mimeType, p.faceCount AS faceCount"
            " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
            " WHERE " + " AND ".join(where) +
            # 与时间轴同序（api/browse.py:1428）：按档、再按拍摄时间。
            " ORDER BY f.shotBucket ASC, p.takenAt ASC")


def poolRows(personCode: str, excludeDuplicate: bool = True) -> list:
    """某人出现过的**照片行**（一张照片一行，保留该照片里质量最高的那张脸）。

    返回行里带 `bucketKey`（已归一），调用方不必再判空桶。
    """
    values = [str(personCode), comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO]
    if excludeDuplicate:
        values.append(0)
    rows = query.selectList(poolSql(bool(excludeDuplicate)), tuple(values))
    # ⚠️ 复用 eraSampler.representativeRows（「质量优先、detScore 次之、
    #    faceCode 裁决」的唯一实现）—— 自己写第二份会让同一个人的
    #    时间轴张数与打包张数静默不一致。
    picked = eraSampler.representativeRows(rows)
    out = []
    for one in picked:
        record = dict(one)
        record["bucketKey"] = bucketKeyOf(record.get("shotBucket"))
        record["effYear"] = effectiveYearOf(record)
        out.append(record)
    return out


def effectiveYearOf(row) -> int:
    """**有效拍摄年** = `COALESCE(shotYearOverride, shotYear)`（DR-42 唯一口径）。

    zip 内按年份分目录用它，**不用**裸 `shotYear`：老相册翻拍件的 shotYear
    是「翻拍那一刻」，用它会把 1960 年代的老照片放进 `2019/` 目录，
    而它明明在 2005-2009 这个档里。
    """
    override = _intOrNone((row or {}).get("shotYearOverride"))
    if override is not None:
        return override
    return _intOrNone((row or {}).get("shotYear")) or 0


def _intOrNone(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# 三、按档分组
# ============================================================

def groupByBucket(rows) -> dict:
    """照片行 -> `{档键: [行…]}`（保持 poolSql 的 ORDER BY 顺序）。"""
    groups = {}
    for one in rows or []:
        groups.setdefault(str(one.get("bucketKey") or NO_BUCKET_KEY), []).append(one)
    return groups


def sortKeys(keys) -> list:
    """档键排序：年份档升序，**非年份档一律排最后**。

    与 api/browse.py:1439 的时间轴排序逐字一致（`0 if k[:1].isdigit() else 1`）
    —— 两处排序不一致的话，界面上的档顺序会在刷新后跳来跳去。
    """
    return sorted(keys, key=lambda k: (0 if str(k)[:1].isdigit() else 1, str(k)))


def summaryOf(rows) -> list:
    """照片行 -> 每个档一条摘要（**列表接口与打包预检共用**）。

    返回
    ----
      [{bucketKey, bucketText, photoTotal, totalBytes, missingCount}, …]
    """
    groups = groupByBucket(rows)
    out = []
    for key in sortKeys(groups.keys()):
        items = groups[key]
        totalBytes = 0
        missing = 0
        for one in items:
            totalBytes += int(_intOrNone(one.get("fileSize")) or 0)
            if int(one.get("isMissing") or 0) == 1:
                missing += 1
        out.append({"bucketKey": key, "bucketText": bucketTextOf(key),
                    "photoTotal": len(items), "totalBytes": totalBytes,
                    "missingCount": missing})
    return out


def photosOfBucket(rows, bucketKey: str) -> list:
    """从已有池子里取某档的照片行（**不重复查库** —— 打包只查一次）。"""
    key = normalizeKey(bucketKey)
    return list(groupByBucket(rows).get(key) or [])


def summaryOfBucket(rows, bucketKey: str) -> dict:
    """某档的摘要（找不到返回 photoTotal=0，**不抛** —— 空档是合法状态）。"""
    key = normalizeKey(bucketKey)
    for one in summaryOf(rows):
        if one["bucketKey"] == key:
            return one
    return {"bucketKey": key, "bucketText": bucketTextOf(key),
            "photoTotal": 0, "totalBytes": 0, "missingCount": 0}


# ============================================================
# 四、自检
# ============================================================

def _selfCheck() -> bool:
    """模块自检：档键归一与排序不变量（与 eraSampler._selfCheck 同一用途）。"""
    assert bucketKeyOf(None) == NO_BUCKET_KEY
    assert bucketKeyOf("") == NO_BUCKET_KEY
    assert bucketKeyOf("2005-2009") == "2005-2009"
    assert bucketTextOf(NO_BUCKET_KEY) == NO_BUCKET_TEXT
    assert bucketTextOf("2005-2009") == "2005-2009"
    assert normalizeKey("2005-2009") == "2005-2009"
    assert normalizeKey("") == NO_BUCKET_KEY
    assert sortKeys(["NOYEAR", "2010-2014", "2005-2009"]) == \
        ["2005-2009", "2010-2014", "NOYEAR"]
    for bad in ("../x", "2005/2009", "中文档", "2005.2009"):
        try:
            normalizeKey(bad)
        except BucketPoolError:
            continue
        raise AssertionError("非法档键竟然通过了: %r" % bad)
    return True


if __name__ == "__main__":
    print("bucketPool _VERSION:", _VERSION)
    print("档键自检:", _selfCheck())
    print("NO_BUCKET_KEY =", NO_BUCKET_KEY, "| 显示 =", NO_BUCKET_TEXT)
