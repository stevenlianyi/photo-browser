#! /usr/bin/env python3
#encoding: utf-8

#Filename: eraSampler.py
#Description: photo-browser 年代抽样（R11 · DR-45 ~ DR-50）——
#             给一个人，从「他出现过的每一年」里抽一张照片，按年排成一条队列
#
# 这个模块解决什么
# ----------------
#   人物详情页（P-05）的 Tab3：用户想看「这个人的每一年长什么样」。
#   原来的时间轴（Tab1）按**年代档**分组（桶宽随生日 3/10 年），
#   而这里要的是**每年一张**的等间隔队列 —— 粒度不同，口径必须同源。
#
# 四条硬口径（每一条写错都会给出一个「看起来完全合理」的错答案）
# ------------------------------------------------------------
#   ① **年份 = 有效拍摄年** `COALESCE(p.shotYearOverride, p.shotYear)`
#      （comGD.sqlEffectiveShotYear，DR-42 唯一出口）。
#      用裸 shotYear：1960 年代翻拍的老照片被归到「翻拍那年」2019，
#      而同一个人 Tab1 的时间轴上它在 1956-1965 —— 同一页自相矛盾，且不报错。
#   ② **一张照片只算一次**。一张合影里同一个人可能有 2 张脸（检测重复/侧脸），
#      不去重会让 candidateCount 与抽样概率全错（Tab1 的 timeline 已踩过同一个坑）。
#   ③ **候选集口径固定**：JOIN pb_photo 且两级 delFlag='0'；排除 isStranger=1
#      （陌生人是「永久排除」语义，见 DR-16①）；排除 isMissing=1（原图不在，
#      抽出来既不能打包也无法保证可信）；isDuplicate / 无年份 / 仅人工确认
#      三个开关由调用方决定。
#   ④ **只读统计**：不写 pb_face / pb_photo / pb_person_centroid，不写 pb_review_log（DR-49）。
#      本模块只写 pb_era_sample* 两张自己的表。
#
# 随机性（DR-47）：加权随机 + 种子落库
# ------------------------------------
#   · `weighted`（默认）权重 = (0.5+quality) * (0.5+detScore)
#                              * (1 + AREA_K * 人脸画面占比)
#                              * (人工确认 ? CONFIRM_BOOST : 1)
#     目标是「通常抽到像他、清楚、不是角落里的小脸的那张」，
#     但**不是** argmax —— 否则「重新抽」按钮毫无意义。
#     `uniform` 是它的特例：把所有权重都当 1.0。
#   · **权重恒记录**（含 uniform）：于是「抽中概率 = weight / weightSum」
#     这一条解释对两种模式都成立，不会出现「NULL 到底是唯一候选还是没记」的歧义。
#   · 种子落库 ⇒ 同 personCode + 同参数 + 同种子 **逐字重现**。
#   · ⚠️ **每年一个独立子种子** `Random("%s:%s" % (seed, yearLabel))`：
#     不是「一条随机流顺序取」。差别是**改年份范围不会连带改掉其它年份的结果** ——
#     用一条流的话，把范围从 1970 收窄到 1971，后面每一年的结果全变，
#     用户只会觉得「这个功能是随机的、不可信」。顺带也更适合复现（只依赖那一年）。
#
# 单写入者（D-8）
# --------------
#   生成一次 = 1 行批次 + N 行明细，在**请求线程内一次写完**，与
#   /api/review/fix、PATCH /api/contacts 同一模式，不需要新的调度器。

import os
import random
import sys
import uuid

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../engine/sample
_SRC_DIR = os.path.dirname(os.path.dirname(_HERE_DIR))          # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from common import globalDefinition as comGD                      # noqa: E402
from common import miscCommon as misc                             # noqa: E402
from common import resourcePath as resourcePath                   # noqa: E402
from config import basicSettings as basicSettings                 # noqa: E402
from database import queryCommon as query                         # noqa: E402
from database.auto_generated import sqliteCommon                  # noqa: E402
from engine.match import bucket as bucket                         # noqa: E402

_VERSION = "20261011"

_LOG = misc.setLogNew("eraSampler", "erasampler.log")

#: 批次表 / 明细表的表名（生成层按表名白名单工作，这里只写一次，不散在各处）
BATCH_TABLE: str = "pb_era_sample"
ITEM_TABLE: str = "pb_era_sample_item"

#: itemKey 的分隔符：`<batchCode>:<yearLabel>`。
#: batchCode 里的字符是 `[A-Za-z0-9_]`（ES_ + 数字 + hex），不含冒号，
#: 所以这个拼接**不会歧义**（同 pb_photo_person.linkKey 的 photoCode:personCode）。
ITEM_KEY_SEP: str = ":"

#: 本机单用户（与 pb_review_log.opUser 同一口径，见 basicSettings.REVIEW_LOG_USER）
DEFAULT_REG_ID: str = basicSettings.REVIEW_LOG_USER

#: 分页读的天花板（`listBatches` 的 size 上限）
MAX_LIST_SIZE: int = 200


class EraSampleError(Exception):
    """年代抽样失败：参数非法 / 人物不存在 / 落库失败。"""


#: 老库没跑迁移时的可读提示。**这不是假设的场景** ——
#: 本功能新增两张表，而建表 DDL 是 `CREATE TABLE IF NOT EXISTS`，
#: **老库不会因为 .txt 加了表就自动多出它**（DR-13）。手工跑过一次 E2E
#: 就撞上了它：错误原文是 `executeWrite:no such table: pb_era_sample`，
#: 又长又没有任何指向性。
#:
#: ⚠️ R10：具体命令走 `resourcePath.dbInitHint()` 的 frozen 分支 ——
#:    打包用户**没有 Python**，原先那句 `python code\src\tools\build_db.py --migrate`
#:    对他是废纸（照做只会得到「'python' 不是内部或外部命令」）。
MIGRATE_HINT: str = ("pb_era_sample / pb_era_sample_item 表不存在 —— "
                     "这是**老库还没跑建表迁移**（DR-13）。\n"
                     "    %s\n"
                     "（纯元数据操作，只 ADD TABLE / ADD INDEX，不动任何一行数据）"
                     % resourcePath.dbInitHint("请先执行一次建库 / 补表"))


class EraDbError(EraSampleError):
    """落库失败 / 表不存在 -> api 层映射 **500 DB_ERROR**。

    为什么不复用 EraSampleError（400 PARAM_INVALID）
    ------------------------------------------------
      「参数写错了」与「库还没迁移」是**两件事**：前者用户改一下输入即可，
      后者要他去跑一条命令。都回 400 会让界面上显示「参数不合法」，
      而用户盯着一个完全正常的请求体，**永远猜不到真正的原因**。
    """


class EraPersonNotFoundError(EraSampleError):
    """personCode 在 pb_person 里不存在 -> api 层映射 **404**。

    为什么单独一个子类而不是让 api 层去匹配报错文案
    ----------------------------------------------
      「找不到」与「参数写错了」在 HTTP 上是 **404 与 400** 两个码，
      前端要据此决定「跳回列表」还是「提示改正参数」。
      靠 `"不存在" in message` 这样的字符串匹配来分流，改一个错字就会
      让 404 悄悄变成 400 —— 而两者在界面上都只是弹一句提示。
    """


# ============================================================
# 一、编码与参数归一
# ============================================================

def newBatchCode() -> str:
    """新批次编码：`ES_<yyyymmddHHMMSS>_<6位随机>`。

    与 `assigner.newLogCode()`（RL_）同一种生成法 —— 别让同一个项目里出现两套编码风格。
    batchCode 是**幂等键**（UNIQUE），带时间戳 + 随机后缀是为了让两次生成
    在重试/重跑后不会互相覆盖。
    """
    return "%s_%s_%s" % (basicSettings.ERA_SAMPLE_CODE_PREFIX, misc.getTime(),
                         uuid.uuid4().hex[:6])


def newSeed() -> str:
    """服务端随机种子（十进制字符串）。

    刻意不用 `random.randint`：种子本身要**不可预测**（用户连点两次「重新抽」
    不该得到同一批 —— 那会让人以为按钮坏了）。用 SystemRandom 取熵。
    """
    return str(random.SystemRandom().randrange(10 ** 12))


def normalizeMode(mode) -> str:
    """抽法归一，非法直接抛（**不静默回落** weighted）。

    静默回落是最坏的一种错：用户选了 uniform，界面显示 uniform，
    实际按 weighted 抽 —— 抽出来的照片看着也挺合理，没人会发现。
    """
    text = str(mode or "").strip().lower() or basicSettings.ERA_SAMPLE_MODE_DEFAULT
    if text not in basicSettings.ERA_SAMPLE_MODE_CHOICES:
        raise EraSampleError("mode 只支持 %s，收到 %r"
                             % (list(basicSettings.ERA_SAMPLE_MODE_CHOICES), mode))
    return text


def normalizeGrain(grain) -> str:
    """粒度归一。本期只实现 year；bucket 是 DR-45 明确不做的预留位。

    ⚠️ 传 bucket **要报错**，不许「接受但按 year 做」—— 那会让界面上
       写着「每个年代档一张」而结果是一年一张，用户拿不到他选的东西，
       还完全看不出来。
    """
    text = str(grain or "").strip().lower() or "year"
    if text not in basicSettings.ERA_SAMPLE_GRAIN_CHOICES:
        raise EraSampleError("grain 目前只支持 %s（bucket 是 DR-45 的预留位，未实现），收到 %r"
                             % (list(basicSettings.ERA_SAMPLE_GRAIN_CHOICES), grain))
    return text


def normalizeYearBound(value, name: str):
    """年份上下限归一：空 -> None（不限）；非法 -> 抛错。

    ⚠️ 不做区间校验（不用 bucket.validShotYear）：那是**分桶**的可信区间，
       而这里只是「用户想看哪一段」。传 1800 得到的是一批空结果，
       这是**可见**的；静默改成 1900 会让用户以为筛选生效了。
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise EraSampleError("%s 必须是整数年份，收到 %r" % (name, value))


def normalizeSeed(seed) -> str:
    """种子：给了就用（可复现），没给就生成一个。"""
    text = str(seed or "").strip()
    return text[:32] if text else newSeed()


# ============================================================
# 二、候选集（口径 ①②③）
# ============================================================

def candidateSql(onlyConfirmed: bool, excludeDuplicate: bool) -> str:
    """候选集 SQL（**唯一一份**）。

    为什么把 SQL 生成单独抽出来：口径要能被单测**逐条钉住**（哪一条 WHERE 掉了，
    测试直接断言 SQL 文本里有没有它），而不是靠「跑一遍看看数字对不对」。
    """
    where = ["f.personCode = %s", "f.delFlag = %s", "p.delFlag = %s",
             "f.isStranger = 0", "p.isMissing = 0"]
    if onlyConfirmed:
        where.append("f.isConfirmed = %s")
    if excludeDuplicate:
        where.append("p.isDuplicate = %s")
    return ("SELECT f.faceCode AS faceCode, f.photoCode AS photoCode,"
            " f.quality AS quality, f.detScore AS detScore, f.bbox AS bbox,"
            " f.isConfirmed AS isConfirmed,"
            " " + comGD.sqlEffectiveShotYear("p") + " AS effYear,"
            " p.takenAt AS takenAt, p.relPath AS relPath, p.faceCount AS faceCount"
            " FROM pb_face f JOIN pb_photo p ON p.photoCode = f.photoCode"
            " WHERE " + " AND ".join(where) +
            # ⚠️ ORDER BY effYear 而不是 p.shotYear（DR-42 同源）；
            #    同一张照片的多张脸会被 quality DESC 排到一起，
            #    representativeRows 据此取「质量最高那张」。
            " ORDER BY effYear ASC, f.quality DESC")


def candidateRows(personCode: str, onlyConfirmed: bool = False,
                  excludeDuplicate: bool = True) -> list:
    """某人物出现过的**脸行**（未按照片去重，见 representativeRows）。"""
    values = [str(personCode), comGD.DEL_FLAG_NO, comGD.DEL_FLAG_NO]
    if onlyConfirmed:
        values.append(1)
    if excludeDuplicate:
        values.append(0)
    return query.selectList(candidateSql(bool(onlyConfirmed), bool(excludeDuplicate)),
                            tuple(values))


def representativeRows(rows) -> list:
    """脸行 -> **每张照片一行**（口径 ②），保留该照片里质量最高的那张脸。

    为什么不把这一步塞进 SQL 的 GROUP BY：
      代表脸的取法是**业务规则**（质量优先，并列再比 detScore），
      放 Python 里显式写、显式测，比藏在 SQL 里更好解释，
      也给响应留了「这张脸为什么被选中」的依据。
    """
    best = {}
    for row in rows or []:
        code = str(row.get("photoCode") or "")
        if not code:
            continue
        current = best.get(code)
        if current is None or _betterFace(row, current):
            best[code] = row
    return list(best.values())


def yearLabelOf(effYear) -> str:
    """有效拍摄年 -> 年档标签（`'1958'` / `'NOYEAR'`）。**唯一出口**。

    越界年份（手工改库、文件名误命中）与 shotYear 为空一样落 `NOYEAR`，
    与分桶口径（bucket.validShotYear 判越界 -> 空桶键）保持一致。
    """
    year = bucket.validShotYear(effYear)
    if year is None:
        return basicSettings.ERA_SAMPLE_NOYEAR_LABEL
    return str(year)


def candidateYears(rows, includeNoYear: bool = False) -> list:
    """候选覆盖的**年份标签**（升序，无年份在最后，**不受 yearFrom/yearTo 影响**）。

    为什么要单独算一遍：界面要能说清「他有 41 个年份有照片，当前筛选出 12 个」。
    若拿 photoCount 当这个数，用户把年份区间一收窄就会以为「他的照片变少了」。
    """
    labels = {yearLabelOf(one.get("effYear")) for one in representativeRows(rows)}
    noYear = basicSettings.ERA_SAMPLE_NOYEAR_LABEL
    if not includeNoYear:
        labels.discard(noYear)
    return sorted(labels, key=lambda k: (1 if k == noYear else 0, k))


def _betterFace(candidate: dict, current: dict) -> bool:
    """候选脸是否比当前代表脸更该代表这张照片。

    规则：**quality 优先，其次 detScore，并列取 faceCode 更小的那张**。

    ⚠️ 并列时**不能**用「先遇到的就赢」：那一来结果就取决于
       SQL 返回顺序（同一分数下 SQLite 的顺序无承诺），
       同一批数据跑两次可能选出不同的代表脸 ——
       「同种子可复现」（DR-47）会当场失效，而且**看不出来**。
       faceCode 是稳定业务码，用它做最后的确定性裁决。
    """
    left = (_floatOrZero(candidate.get("quality")), _floatOrZero(candidate.get("detScore")))
    right = (_floatOrZero(current.get("quality")), _floatOrZero(current.get("detScore")))
    if left != right:
        return left > right
    return str(candidate.get("faceCode") or "") < str(current.get("faceCode") or "")


def _floatOrZero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bboxArea(bbox) -> float:
    """归一化框 'x,y,w,h' -> 面积（w*h），解析不出返回 0。

    ⚠️ 解析失败返回 **0** 而不是 0.25 之类的猜测值：猜一个「中等大小」
       会让一张 bbox 脏掉的照片在加权里获得中等权重，
       而它本来该是「没有任何画面占比信息」。
    """
    text = str(bbox or "").strip()
    if not text:
        return 0.0
    parts = text.split(",")
    if len(parts) != 4:
        return 0.0
    try:
        width = float(parts[2])
        height = float(parts[3])
    except (TypeError, ValueError):
        return 0.0
    if width <= 0 or height <= 0:
        return 0.0
    return max(0.0, min(1.0, width * height))


# ============================================================
# 三、权重与抽取（DR-47）
# ============================================================

def weightOf(row, areaK: float = None, confirmBoost: float = None,
             minWeight: float = None) -> float:
    """一张候选照片的权重（weighted 模式）。

    权重 = (0.5 + quality) * (0.5 + detScore)
           * (1 + AREA_K * 人脸画面占比)
           * (人工确认 ? CONFIRM_BOOST : 1)

    · `0.5 +` 的平移是为了**零分也有非零权重**（否则一张 quality=0 的候选
      永远抽不到，而它可能是那一年唯一一张「他出现过」的照片）；
    · 画面占比这一项是**压制「他站得很远的合影」**用的：没有它，
      权重高的永远是最清晰的那张，而清晰度与小脸无关；
    · 三个系数都在 basicSettings 里（不硬编码），因为它们是**观感参数**，
      要按真实照片库调。
    """
    quality = max(0.0, _floatOrZero(row.get("quality")))
    detScore = max(0.0, _floatOrZero(row.get("detScore")))
    area = bboxArea(row.get("bbox"))
    k = basicSettings.ERA_SAMPLE_AREA_K if areaK is None else float(areaK)
    boost = (basicSettings.ERA_SAMPLE_CONFIRM_BOOST if confirmBoost is None
             else float(confirmBoost))
    floor = basicSettings.ERA_SAMPLE_MIN_WEIGHT if minWeight is None else float(minWeight)
    weight = (0.5 + quality) * (0.5 + detScore) * (1.0 + k * area)
    if int(row.get("isConfirmed") or 0):
        weight *= boost
    return max(weight, floor)


def planPicks(rows, mode: str = "weighted", seed: str = "", yearFrom=None,
              yearTo=None, includeNoYear: bool = False,
              maxYears: int = None) -> list:
    """候选脸行 -> **抽样计划**（纯函数，不读库不写库）。

    参数
    ----
      rows        : candidateRows() 的原始脸行（**未去重**，本函数内部去重）
      mode        : weighted / uniform
      seed        : 随机种子（**必填**，空串会得到确定但不可复现的固定序列）
      yearFrom/To : 年份区间（含），None = 不限
      includeNoYear : 是否把「无拍摄年份」也算一档（排在队列**最后**）
      maxYears    : 年档数上限（防「年份列被污染」的事故，默认取配置）

    返回
    ----
      [{pickOrder, yearLabel, shotYear, photoCode, faceCode, candidateCount,
        weight, weightSum, quality, detScore, isConfirmed, takenAt, relPath,
        photoFaceCount}]，已按年份升序（无年份在最后）。

    为什么 shotYear 要过 bucket.validShotYear
    -----------------------------------------
      `COALESCE(shotYearOverride, shotYear)` 是**原始值**，可能越界（手工改库、
      文件名误命中）。`validShotYear` 把它收敛成「可信年份或 None」——
      越界的落进 NOYEAR 档，与分桶口径（bucket.bucketKeyAdaptive 同样会返回
      空桶键 = 不进跨桶比对）保持一致。**可见的「无年份」比静默按某个合法年份分组安全。**
    """
    mode = normalizeMode(mode)
    rows = representativeRows(rows)

    groups = {}
    for row in rows:
        key = yearLabelOf(row.get("effYear"))
        if key == basicSettings.ERA_SAMPLE_NOYEAR_LABEL:
            if not includeNoYear:
                continue
        else:
            year = int(key)
            if yearFrom is not None and year < yearFrom:
                continue
            if yearTo is not None and year > yearTo:
                continue
        groups.setdefault(key, []).append(row)

    limit = basicSettings.ERA_SAMPLE_MAX_YEARS if maxYears is None else int(maxYears)
    if len(groups) > limit:
        raise EraSampleError(
            "年档数 %d 超过上限 %d —— 这不该发生在正常数据上（年份可信区间只有 %d 年），"
            "多半是 pb_photo 的年份列或 validShotYear 被改坏了。"
            "已拒绝生成，请先查数据。" % (len(groups), limit,
                                          basicSettings.SHOT_YEAR_MAX
                                          - basicSettings.SHOT_YEAR_MIN + 1))

    # 年份升序，无年份排最后（"队列"的可见顺序 = 时间顺序）
    order = sorted(groups, key=lambda k: (1 if k == basicSettings.ERA_SAMPLE_NOYEAR_LABEL
                                          else 0, k))

    picks = []
    for index, key in enumerate(order):
        pool = groups[key]
        weights = [1.0 for _ in pool] if mode == "uniform" else \
            [weightOf(one) for one in pool]
        weightSum = float(sum(weights))
        # ⚠️ 每年一个独立子种子（文件头「随机性」一节）：改年份范围不影响其它年份
        rng = random.Random("%s%s%s" % (seed, ITEM_KEY_SEP, key))
        chosen = rng.choices(pool, weights=weights, k=1)[0] if len(pool) > 1 else pool[0]
        # ⚠️ 按**身份**回找下标，不用 list.index()：
        #    index() 走的是 == 比较，两张脸若字段值恰好全等（同一照片重复检测、
        #    bbox/quality 都一样）就会被判成同一个元素，取到的权重是别人的。
        at = next(pos for pos, one in enumerate(pool) if one is chosen)
        shotYear = (None if key == basicSettings.ERA_SAMPLE_NOYEAR_LABEL else int(key))
        picks.append({
            "pickOrder": index + 1,
            "yearLabel": key,
            "shotYear": shotYear,
            "photoCode": str(chosen.get("photoCode") or ""),
            "faceCode": str(chosen.get("faceCode") or "") or None,
            "candidateCount": len(pool),
            # uniform 下权重恒为 1.0 -> 概率 = 1/候选数，
            # 于是「抽中概率 = weight / weightSum」这一条解释对两种模式都成立
            "weight": weights[at],
            "weightSum": weightSum,
            "quality": _floatOrNone(chosen.get("quality")),
            "detScore": _floatOrNone(chosen.get("detScore")),
            "isConfirmed": int(chosen.get("isConfirmed") or 0),
            "takenAt": chosen.get("takenAt") or None,
            "relPath": str(chosen.get("relPath") or ""),
            "photoFaceCount": int(chosen.get("faceCount") or 0),
        })
    return picks


def _floatOrNone(value):
    """数值 -> float，取不到给 None（**不填 0**）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# 四、生成（读 -> 算 -> 落库）
# ============================================================

def generate(personCode: str, mode: str = None, grain: str = None, seed: str = None,
             yearFrom=None, yearTo=None, includeNoYear: bool = False,
             onlyConfirmed: bool = False, excludeDuplicate: bool = True,
             note: str = "") -> dict:
    """生成一次抽样：写 1 行批次 + N 行明细。返回 `{batchCode, ...计数值}`。

    ⚠️ **返回的不是完整批次**：调用方应当接着 `getBatch(batchCode)` 取。
       为什么绕这一下：让「刚生成的」与「刷新页面重新读的」走**同一条读路径**，
       两条路径各拼一份响应迟早会漂移（最典型的是刚生成的有 thumbUrl、
       重新读的没有，而界面上只在刷新后才发现）。

    人物不存在 -> EraSampleError（api 层映射 404）。
    候选为空 -> **正常返回** photoCount=0 的批次（**不是错误**）：
      「这个人没有带年份的照片」是一件要如实告诉用户的事实，不是失败。
      界面据此显示信息性空状态（DR-39 的纪律）。
    """
    person = str(personCode or "").strip()
    if not person:
        raise EraSampleError("personCode 不能为空")
    personRows = sqliteCommon.query_pb_person("pb_person", personCode=person, limitNum=1)
    if not personRows:
        raise EraPersonNotFoundError("personCode=%s 在 pb_person 里不存在" % person)
    row = personRows[0]

    # 前置检查：表在不在（DR-13 的迁移是**手工一步**，忘了它不该只报一句
    # 没有指向性的 "no such table"）。放在参数校验**之前**是因为这是环境问题，
    # 无论参数对不对，用户都得先去跑那条命令。
    if not sqliteCommon.chkTableExist(BATCH_TABLE) or not sqliteCommon.chkTableExist(ITEM_TABLE):
        raise EraDbError(MIGRATE_HINT)

    grain = normalizeGrain(grain)
    mode = normalizeMode(mode)
    seed = normalizeSeed(seed)
    yearFrom = normalizeYearBound(yearFrom, "yearFrom")
    yearTo = normalizeYearBound(yearTo, "yearTo")
    if yearFrom is not None and yearTo is not None and yearFrom > yearTo:
        raise EraSampleError("yearFrom(%d) 不能大于 yearTo(%d)" % (yearFrom, yearTo))

    rows = candidateRows(person, onlyConfirmed=onlyConfirmed,
                         excludeDuplicate=excludeDuplicate)
    candidates = representativeRows(rows)
    picks = planPicks(rows, mode=mode, seed=seed, yearFrom=yearFrom,
                      yearTo=yearTo, includeNoYear=includeNoYear)
    # ⚠️ 候选年份数**不受 yearFrom/yearTo 影响**（见 candidateYears 的说明）：
    #    界面用它说「他有 N 个年份有照片，本次抽出 M 个」。
    allYears = candidateYears(rows, includeNoYear=includeNoYear)

    batchCode = newBatchCode()
    batch = {
        "batchCode": batchCode,
        "personCode": person,
        # displayName **快照**：人物改名/停用后，历史批次仍能显示「这是谁的那一次」
        "personName": str(row.get("displayName") or ""),
        "grain": grain,
        "mode": mode,
        "randomSeed": seed,
        "yearFrom": yearFrom,
        "yearTo": yearTo,
        "includeNoYear": 1 if includeNoYear else 0,
        "onlyConfirmed": 1 if onlyConfirmed else 0,
        "excludeDuplicate": 1 if excludeDuplicate else 0,
        "candidatePhotoCount": len(candidates),
        "candidateYearCount": len(allYears),
        "photoCount": len(picks),
        "zipState": "NONE",
        "note": str(note or "")[:200],
        "regID": DEFAULT_REG_ID,
    }
    # ⚠️ forceColumns：candidateYearCount=0 的批次也要把「空的一批」如实写下来。
    #    这里其实用不着（photoCount 等都不是 NULL），但 yearFrom/yearTo/note
    #    在**全部为空**时会被 normalizeDataSet 丢掉 -> 走库 DEFAULT / NULL，正确。
    if sqliteCommon.insert_pb_era_sample(BATCH_TABLE, batch) <= 0:
        raise EraDbError("写 pb_era_sample 失败：%s"
                         % (sqliteCommon.dbHandle().lastErrMsg or "未知错误"))

    if picks:
        items = [_itemRow(batchCode, person, one) for one in picks]
        written, _columns = sqliteCommon.insertManyTableGeneral(
            ITEM_TABLE, items, conflictColumns=("itemKey",),
            updateColumns=tuple(items[0].keys()), fillStandard=True,
            forceColumns=("shotYear", "faceCode"))
        if int(written) < len(items):
            # 半成品批次是最坏的状态（界面上一半的年份凭空消失）。
            # 明确标记 + 抛错，让人看得见而不是拿到一个「少了几年的队列」。
            updateZipState(batchCode, "FAILED", errMsg="明细只写入 %d/%d 行"
                           % (int(written), len(items)))
            raise EraDbError("写 pb_era_sample_item 失败：只写入 %d/%d 行"
                             % (int(written), len(items)))

    _LOG.info("年代抽样 %s: person=%s 候选照片 %d 张 / 抽中 %d 年 / mode=%s seed=%s",
              batchCode, person, len(candidates), len(picks), mode, seed)
    return {"batchCode": batchCode, "personCode": person,
            "personName": batch["personName"], "grain": grain, "mode": mode,
            "randomSeed": seed, "candidatePhotoCount": len(candidates),
            "candidateYearCount": len(picks), "photoCount": len(picks)}


def _itemRow(batchCode: str, personCode: str, pick: dict) -> dict:
    """一条抽样明细行（pick -> pb_era_sample_item）。"""
    return {
        "itemKey": "%s%s%s" % (batchCode, ITEM_KEY_SEP, pick["yearLabel"]),
        "batchCode": batchCode,
        "personCode": personCode,
        "shotYear": pick["shotYear"],
        "yearLabel": pick["yearLabel"],
        "photoCode": pick["photoCode"],
        "faceCode": pick["faceCode"],
        "candidateCount": pick["candidateCount"],
        "weight": pick["weight"],
        "weightSum": pick["weightSum"],
        "quality": pick["quality"],
        "detScore": pick["detScore"],
        "isConfirmed": pick["isConfirmed"],
        "pickOrder": pick["pickOrder"],
        "regID": DEFAULT_REG_ID,
    }


# ============================================================
# 五、读（批次 / 明细）
# ============================================================

def batchRow(batchCode: str) -> dict:
    """按 batchCode 取批次行（取不到返回 {}）。

    ⚠️ 只看 `delFlag='0'`：本模块**不提供删除批次的能力**（见 api/eraSample.py
       纪律 ④：全站没有 DELETE 端点，那是被 `test_openapiHasNoExportOrDelete`
       守着的项目级不变量）。`delFlag` 列来自「每表必带的标准尾部七字段」，
       留着只是为了规范一致，没有任何代码路径会写 `'1'`。
    """
    code = str(batchCode or "").strip()
    if not code:
        return {}
    rows = sqliteCommon.query_pb_era_sample(BATCH_TABLE, batchCode=code, limitNum=1)
    return rows[0] if rows else {}


def listBatches(personCode: str, size: int = 20) -> list:
    """某人的历史批次（**插入倒序**，最新生成的那一批在最前面）。

    ⚠️ 按 `recID DESC` 而不是 `regYMDHMS DESC`：时间戳只到**秒**，
       而「连点两次『重新抽』」就发生在同一秒内 —— 只按时间戳排序时
       这两批的先后没有承诺，而列表第一行正是界面默认展示的那一批。
       recID 是 AUTOINCREMENT，插入顺序的精确表达，没有并列。
    """
    person = str(personCode or "").strip()
    if not person:
        return []
    limit = max(1, min(int(size or 20), MAX_LIST_SIZE))
    return sqliteCommon.query_pb_era_sample(
        BATCH_TABLE, personCode=person, orderBy="recID", descFlag=True,
        limitNum=limit)


def itemRows(batchCode: str) -> list:
    """某批次的明细（**已按 pickOrder 排队**）+ pb_photo 的展示字段。

    为什么在这里 JOIN pb_photo（而不是把 takenAt/relPath 冗余进明细表）：
      `takenAt` / `relPath` / `faceCount` 是**照片的当前属性**，冗余进明细
      就要在「改了照片元数据」时同步维护 —— 而少维护一处就是错的统计，
      且不报错（与 D-6「冗余要付维护代价」同一判断）。
      `shotYear` / `weight` / `candidateCount` 则**必须**冗余：它们是
      「当时怎么抽的」的事实快照，不随照片变化。
    """
    code = str(batchCode or "").strip()
    if not code:
        return []
    return query.selectList(
        "SELECT i.pickOrder AS pickOrder, i.yearLabel AS yearLabel,"
        " i.shotYear AS shotYear, i.photoCode AS photoCode, i.faceCode AS faceCode,"
        " i.candidateCount AS candidateCount, i.weight AS weight,"
        " i.weightSum AS weightSum, i.quality AS quality, i.detScore AS detScore,"
        " i.isConfirmed AS isConfirmed,"
        " p.takenAt AS takenAt, p.relPath AS relPath, p.faceCount AS faceCount,"
        " p.isMissing AS isMissing"
        " FROM pb_era_sample_item i LEFT JOIN pb_photo p ON p.photoCode = i.photoCode"
        " WHERE i.batchCode = %s AND i.delFlag = %s"
        " ORDER BY i.pickOrder ASC",
        (code, comGD.DEL_FLAG_NO))


def getBatch(batchCode: str) -> dict:
    """批次头 + 明细。取不到返回 {}（api 层映射 404）。"""
    batch = batchRow(batchCode)
    if not batch:
        return {}
    return {"batch": batch, "items": itemRows(batchCode)}


# ============================================================
# 六、批次状态回写（zip 打包用；**批次表只有一个写者** —— 本模块）
# ============================================================

def updateZipState(batchCode: str, zipState: str, zipRelName: str = None,
                   zipSize=None, zipPhotoCount=None, zipMissingCount=None,
                   errMsg: str = None) -> bool:
    """回写批次的 zip 状态字段。返回是否成功。

    ⚠️ 走 update_pb_era_sample（按 recID），不用 upsert：
      这里只改几列，而 upsert 需要带齐 NOT NULL 身份列，容易把别的列
       按「本行出现的列」覆盖一遍（batchCode 是冲突键、不算，但
       randomSeed 等 NOT NULL 列漏传会撞约束）。
    """
    batch = batchRow(batchCode)
    if not batch:
        return False
    dataSet = {"zipState": str(zipState or "NONE"), "modifyID": DEFAULT_REG_ID}
    if zipRelName is not None:
        dataSet["zipRelName"] = str(zipRelName)
    if zipSize is not None:
        dataSet["zipSize"] = int(zipSize)
    if zipPhotoCount is not None:
        dataSet["zipPhotoCount"] = int(zipPhotoCount)
    if zipMissingCount is not None:
        dataSet["zipMissingCount"] = int(zipMissingCount)
    if errMsg is not None:
        dataSet["errMsg"] = str(errMsg)[:512]
    if str(zipState or "").upper() == "READY":
        dataSet["zipYMDHMS"] = misc.getTime()
    # ⚠️ 判 `> 0` 而不是 `>= 0`：updateTableGeneral 在**失败**与**没有字段可改**时
    #    都返回 0。SQLite 的 changes() 数的是 WHERE 命中的行数（不是真被改写的行数），
    #    所以只要命中就一定是 1 —— 0 只能是「没找到那一行」或「执行失败」。
    return sqliteCommon.update_pb_era_sample(
        BATCH_TABLE, int(batch.get("recID") or 0), dataSet) > 0


# ============================================================
# 自检
# ============================================================

def _selfCheck() -> None:
    """模块加载时核对配置合法性（写坏了立刻炸，而不是等用户点按钮）。"""
    if not basicSettings.ERA_SAMPLE_CODE_PREFIX:
        raise EraSampleError("ERA_SAMPLE_CODE_PREFIX 不能为空")
    if len(basicSettings.ERA_SAMPLE_CODE_PREFIX) + 1 + 14 + 1 + 6 > 64:
        raise EraSampleError("batchCode 会超出 VARCHAR(64)")
    if basicSettings.ERA_SAMPLE_MODE_DEFAULT not in basicSettings.ERA_SAMPLE_MODE_CHOICES:
        raise EraSampleError("ERA_SAMPLE_MODE_DEFAULT 不在 ERA_SAMPLE_MODE_CHOICES 里")
    if basicSettings.ERA_SAMPLE_MAX_YEARS <= (basicSettings.SHOT_YEAR_MAX
                                              - basicSettings.SHOT_YEAR_MIN + 1):
        raise EraSampleError(
            "ERA_SAMPLE_MAX_YEARS(%d) 必须大于年份可信区间长度(%d) —— "
            "否则它会变成一条**正常数据也会触发**的业务错误，而不是事故保险丝。"
            % (basicSettings.ERA_SAMPLE_MAX_YEARS,
               basicSettings.SHOT_YEAR_MAX - basicSettings.SHOT_YEAR_MIN + 1))
    for name in (BATCH_TABLE, ITEM_TABLE):
        if name not in sqliteCommon.TABLE_COLUMNS:
            raise EraSampleError(
                "%s 不在生成层表清单里 —— 先跑 sqliteCodeGenerator.py 再跑 build_db.py --migrate" % name)


_selfCheck()
