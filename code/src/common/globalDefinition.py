#! /usr/bin/env python3
#encoding: utf-8

#Filename: globalDefinition.py
#Description: photo-browser 全局常量：返回码 / 任务状态机 / 枚举字典
#
# 纪律：
#   1. 库中落的是**字符串或数字**（jobStatus='PAUSED'、scanState=2），
#      本文件是它们在 Python 侧的唯一出口，禁止在业务模块里裸写字面量；
#   2. 与 pb_*.txt 的字段注释一一对应，改表时同步改这里；
#   3. 本模块零依赖、零IO，可安全被任何层import。

_VERSION = "20261008"


# ============================================================
# 一、通用返回码
# ============================================================
# 0 成功；1 段按 4 位分段：1xxx 通用/配置、2xxx 存储、3xxx 文件、4xxx 图像、5xxx 任务
RET_OK: int = 0

# ---- 1xxx 通用 / 配置 / 路径 ----
ERR_UNKNOWN: int = 1000
ERR_PARAM_INVALID: int = 1001
ERR_CONFIG_INVALID: int = 1002
ERR_PATH_NOT_FOUND: int = 1003
#三目录互相嵌套，或等于 photoRoot（paths.PathLayoutError）
ERR_PATH_LAYOUT_INVALID: int = 1004
# relPath 非法（绝对路径 / 盘符 / UNC）
ERR_PATH_NOT_RELATIVE: int = 1005
ERR_MODULE_MISSING: int = 1006

# ---- 2xxx 存储（SQLite）----
ERR_DB_ERROR: int = 2001
ERR_DB_LOCKED: int = 2002
ERR_DB_NOT_FOUND: int = 2003
ERR_DB_TABLE_MISSING: int = 2004

# ---- 3xxx 文件读写 ----
ERR_FILE_NOT_FOUND: int = 3001
ERR_FILE_READ_ERROR: int = 3002
ERR_FILE_WRITE_ERROR: int = 3003
# 试图写入 photo 目录 —— 触碰「原图只读」硬约束，必须立刻中止
ERR_FILE_READONLY_VIOLATION: int = 3004

# ---- 4xxx 图像 / 人脸 ----
ERR_IMAGE_DECODE_ERROR: int = 4001
ERR_IMAGE_TOO_LARGE: int = 4002
ERR_FACE_MODEL_MISSING: int = 4003
# 人脸被质量过滤丢弃（detScore/短边/yaw 不达标），非致命
ERR_FACE_LOW_QUALITY: int = 4004
# 可选依赖缺失（如 reverse_geocoder）——降级不报错
ERR_OPTIONAL_DEP_MISSING: int = 4005

# ---- 5xxx 任务调度 ----
ERR_TASK_STATE_ILLEGAL: int = 5001
ERR_TASK_NOT_FOUND: int = 5002

# 返回码 -> 中文提示（API 层直接取，避免各层各写一份）
ERROR_TEXT: dict = {
    RET_OK: "成功",
    ERR_UNKNOWN: "未知错误",
    ERR_PARAM_INVALID: "参数非法",
    ERR_CONFIG_INVALID: "配置非法",
    ERR_PATH_NOT_FOUND: "路径不存在",
    ERR_PATH_LAYOUT_INVALID: "路径布局非法（三目录不得互相嵌套、不得等于 photoRoot）",
    ERR_PATH_NOT_RELATIVE: "相对路径非法（不得为绝对路径/盘符/UNC）",
    ERR_MODULE_MISSING: "缺少可选依赖，已降级",
    ERR_DB_ERROR: "数据库错误",
    ERR_DB_LOCKED: "数据库被锁（单写入者约束被破坏）",
    ERR_DB_NOT_FOUND: "数据库文件不存在",
    ERR_DB_TABLE_MISSING: "数据表不存在",
    ERR_FILE_NOT_FOUND: "文件不存在",
    ERR_FILE_READ_ERROR: "文件读取失败",
    ERR_FILE_WRITE_ERROR: "文件写入失败",
    ERR_FILE_READONLY_VIOLATION: "违反原图只读约束，禁止写入 photo 目录",
    ERR_IMAGE_DECODE_ERROR: "图像解码失败",
    ERR_IMAGE_TOO_LARGE: "图像尺寸超限",
    ERR_FACE_MODEL_MISSING: "人脸模型缺失（buffalo_l未下载）",
    ERR_FACE_LOW_QUALITY: "人脸质量不达标，已丢弃",
    ERR_OPTIONAL_DEP_MISSING: "可选依赖缺失，已降级处理",
    ERR_TASK_STATE_ILLEGAL: "任务状态流转非法",
    ERR_TASK_NOT_FOUND: "任务不存在",
}


def errText(code: int) -> str:
    """返回码 -> 中文提示；未登记的码回显 '未知错误(<code>)'"""
    return ERROR_TEXT.get(code, "未知错误(%s)" % code)


def isOK(code: int) -> bool:
    return code == RET_OK


# ============================================================
# 二、扫描任务状态机（pb_scan_job.jobStatus）
# ============================================================
# 批次限流语义：处理到 batchSize（默认 100）张即置 PAUSED 停止，
# 等用户点「继续下一批」再从 lastCursor 续扫。

JOB_IDLE: str = "IDLE"        # 已创建未开始
JOB_RUNNING: str = "RUNNING"    # 正在扫描
JOB_PAUSED: str = "PAUSED"      # 本批处理完，等待用户指示
JOB_DONE: str = "DONE"          # 全部处理完
JOB_FAILED: str = "FAILED"      # 出错终止

JOB_STATUS_ALL: tuple = (JOB_IDLE, JOB_RUNNING, JOB_PAUSED, JOB_DONE, JOB_FAILED)

JOB_STATUS_TEXT: dict = {
    JOB_IDLE: "空闲",
    JOB_RUNNING: "扫描中",
    JOB_PAUSED: "本批已暂停，等待继续",
    JOB_DONE: "已完成",
    JOB_FAILED: "失败",
}

# 允许的状态流转：key -> 允许迁移到的目标状态集合
JOB_STATUS_TRANSITIONS: dict = {
    JOB_IDLE: (JOB_RUNNING,),
    JOB_RUNNING: (JOB_PAUSED, JOB_DONE, JOB_FAILED),
    # PAUSED 可续扫（RUNNING）或作废（FAILED）
    JOB_PAUSED: (JOB_RUNNING, JOB_FAILED),
    JOB_DONE: (),
    # 失败可重试
    JOB_FAILED: (JOB_IDLE, JOB_RUNNING),
}

# 终态：不再自动流转
JOB_STATUS_FINAL: tuple = (JOB_DONE,)


def canTransit(src: str, dst: str) -> bool:
    """判断 jobStatus 能否从 src 迁到 dst"""
    if src not in JOB_STATUS_TRANSITIONS:
        return False
    return dst in JOB_STATUS_TRANSITIONS[src]


def isFinalStatus(status: str) -> bool:
    return status in JOB_STATUS_FINAL


# ============================================================
# 二之二、任务类型（pb_scan_job.jobType）
# ============================================================
# 为什么扫描与人脸识别共用一张任务表
# ----------------------------------
#   两者是**同一条流水线上的前后两道工序**，共用同一套状态机
#   （IDLE→RUNNING→PAUSED/DONE/FAILED）、同一套断点续跑语义
#   （batchIndex / lastCursor）、同一套进度口径（totalCount/processedCount），
#   而 pb_scan_job 的字段对两者**逐个都对得上**：
#     · rootPath     —— 人脸识别就是照片库根
#     · totalCount   —— 待提取特征的**照片**张数
#     · processedCount—— 已处理张数
#     · addedCount   —— 提取出**至少一张脸**的照片张数
#     · pendingCount —— 新写入 pb_face 的**人脸条数**（= 新增待确认量）
#     · lastCursor   —— 最后处理的 photoCode（断点续跑靠它）
#   为此另立一张 pb_face_job 只是把同一组列抄一遍，换来的是
#   「前端要维护两套列表页、两套轮询、两处单写入者互斥」。
#   ⇒ 一张表 + 一个 jobType 判别位，是这里的最小且一致的做法。
#
# ⚠️ 正因为同表，**所有**按类型过滤的查询都必须带上 jobType：
#    混在一起查会让「扫描台」显示出人脸识别任务的进度条，
#    而那几个计数字段的口径完全不同（见上）。

JOB_TYPE_SCAN: int = 0    # 照片扫描（步骤 3：遍历 -> 写 pb_photo）
JOB_TYPE_FACE: int = 1    # 人脸识别（步骤 5：提取特征 -> 写 pb_face）

JOB_TYPE_ALL: tuple = (JOB_TYPE_SCAN, JOB_TYPE_FACE)

JOB_TYPE_TEXT: dict = {
    JOB_TYPE_SCAN: "照片扫描",
    JOB_TYPE_FACE: "人脸识别",
}

#: pb_scan_job.jobCode 前缀（与 basicSettings.SCAN_JOB_CODE_PREFIX 呼应）
JOB_CODE_PREFIX: dict = {
    JOB_TYPE_SCAN: "SJ",
    JOB_TYPE_FACE: "FJ",
}


def jobTypeText(jobType) -> str:
    """任务类型 -> 中文名；未知值原样回显（不静默变成「扫描」）"""
    try:
        return JOB_TYPE_TEXT[int(jobType)]
    except (TypeError, ValueError, KeyError):
        return "未知类型(%s)" % jobType


def makeJobCode(jobType: int) -> str:
    """任务类型 -> 带类型前缀的任务编码：SJ_/FJ_ + 时间戳 + 6 位随机。

    前缀让 `GET /api/scan/jobs` 的返回肉眼可辨（排障时看日志最省事），
    且**不影响幂等**——幂等键是整串 jobCode，不靠前缀推断类型。
    """
    import random
    import time as _time
    prefix = JOB_CODE_PREFIX.get(int(jobType), "XX")
    return "%s_%s_%06d" % (prefix, _time.strftime("%Y%m%d%H%M%S"),
                           random.randint(0, 999999))


# ============================================================
# 三、pb_photo.scanState
# ============================================================

SCAN_STATE_PENDING: int = 0    # 待扫描（新入库/ 内容变更后回到此态）
SCAN_STATE_FACED: int = 1      # 已入人脸库（特征已提取，尚未归类）
SCAN_STATE_REVIEW: int = 2     # 待人工确认（落在 T_LOW–T_HIGH 灰区）
SCAN_STATE_DONE: int = 3# 完成（已归属或已确认为无需归属）

SCAN_STATE_ALL: tuple = (
    SCAN_STATE_PENDING,
    SCAN_STATE_FACED,
    SCAN_STATE_REVIEW,
    SCAN_STATE_DONE,
)

SCAN_STATE_TEXT: dict = {
    SCAN_STATE_PENDING: "待扫描",
    SCAN_STATE_FACED: "已入人脸库",
    SCAN_STATE_REVIEW: "待人工确认",
    SCAN_STATE_DONE: "完成",
}

# 允许的 scanState 流转
SCAN_STATE_TRANSITIONS: dict = {
    SCAN_STATE_PENDING: (SCAN_STATE_FACED,),
    SCAN_STATE_FACED: (SCAN_STATE_REVIEW, SCAN_STATE_DONE, SCAN_STATE_PENDING),
    SCAN_STATE_REVIEW: (SCAN_STATE_DONE, SCAN_STATE_FACED, SCAN_STATE_PENDING),
    SCAN_STATE_DONE: (SCAN_STATE_PENDING,),
}


# ============================================================
# 四、来源标记（pb_person.source / pb_photo_person.source）
# ============================================================

PERSON_SOURCE_MANUAL: int = 0    # 手工新建
PERSON_SOURCE_IMPORT: int = 1    # vCard / CSV 导入
PERSON_SOURCE_GRAPH: int = 2      # 通讯录 Graph

PERSON_SOURCE_ALL: tuple = (PERSON_SOURCE_MANUAL, PERSON_SOURCE_IMPORT, PERSON_SOURCE_GRAPH)

# pb_photo_person.source：关联是算法判的还是人点出来的
LINK_SOURCE_AUTO: int = 0        # 自动（相似度 >= T_HIGH）
LINK_SOURCE_MANUAL: int = 1      # 人工确认

LINK_SOURCE_ALL: tuple = (LINK_SOURCE_AUTO, LINK_SOURCE_MANUAL)


# ============================================================
# 五、软删除（所有表 delFlag）
# ============================================================

DEL_FLAG_NO: str = "0"           # 未删
DEL_FLAG_YES: str = "1"          # 已删

DEL_FLAG_ALL: tuple = (DEL_FLAG_NO, DEL_FLAG_YES)


# ============================================================
# 六、人脸聚类簇（pb_face.clusterCode）
# ============================================================

# 未归类且未聚到任何簇时的簇名占位（DBSCAN 噪声点）
CLUSTER_NOISE: str = "_NOISE_"


# ============================================================
# 七、照片年份缺省值
# ============================================================

# 截图等无法识别年份的照片，shotYear 落库为 NULL；
# 内存/查询层需要占位时统一用这个值，勿散落 0 / -1 / "UNK"
SHOT_YEAR_UNKNOWN: int = -1


# ============================================================
# 七之三、有效拍摄年（DR-42）
# ============================================================
# `pb_photo.shotYear` 是**机器读出来的**年份（EXIF -> 文件名 -> mtime），
# 而老相册翻拍 / 扫描件的这三条兜底链给出的都是"翻拍那一刻"，不是照片被拍下的年代
# —— 于是这张照片的人脸会落进错误的年代桶（桶 = f(拍摄年, 该人出生年)）。
#
# `pb_photo.shotYearOverride` 是用户手工填的**修正年**（P-03「年代修正」）。
# 口径只有一条：**override 优先于 shotYear**，全项目一致。
#
# ⚠️ 为什么必须收口成一个函数，而不是各处手写 COALESCE：
#    分桶（rebucket）、时间筛选（browse.listPhotos）、年代跨度（MIN/MAX）、
#    地点聚合（placeStore.rebuildPlaces）四处都要用同一个口径；
#    散着写的话，"漏改一处"的症状是**某一处仍按 2019 年算**，
#    而界面上那几个数字都合法 —— 又是一个不报错的静默不一致。

#: 有效拍摄年在 SQL 里的列表达式。alias 是 pb_photo 在该查询里的表别名。
#: ⚠️ 生成层**没有**给这两列建联合索引：本表达式会让 idx_pb_photo_shotYear
#:    失效（表达式不匹配索引）。目前照片量级（万张）下全表扫可接受；
#:    真要提速，应改为「物化一列 effectiveShotYear 并随修正一起维护」。
def sqlEffectiveShotYear(alias: str = "p") -> str:
    """`COALESCE(p.shotYearOverride, p.shotYear)` —— 全项目唯一口径（DR-42）。"""
    name = str(alias or "p").strip() or "p"
    return "COALESCE(%s.shotYearOverride, %s.shotYear)" % (name, name)

# ============================================================
# 七之二、pb_person.relation / pb_person_category.category（步骤 8 联系人导入）
# ============================================================
# 为什么不放在导入模块里：这两个字段的值会出现在 API 过滤条件、人物库筛选 Chip、
# 合并/拆分校验里，属于**全项目共用的枚举**，必须只有一个出口（见本文件纪律第 1 条）。

#: pb_person.relation —— 家庭关系（数据库设计§4.2）。空串 = 未知（**不猜**）
RELATION_PARENT: str = "parent"
RELATION_SPOUSE: str = "spouse"
RELATION_CHILD: str = "child"
RELATION_SIBLING: str = "sibling"

RELATION_ALL: tuple = (RELATION_PARENT, RELATION_SPOUSE,
                       RELATION_CHILD, RELATION_SIBLING)

#: pb_person_category.category 的三个规范值（数据库设计 §4.3）
CATEGORY_FAMILY: str = "family"
CATEGORY_FRIEND: str = "friend"
CATEGORY_COLLEAGUE: str = "colleague"

CATEGORY_ALL: tuple = (CATEGORY_FAMILY, CATEGORY_FRIEND, CATEGORY_COLLEAGUE)

#: 归一表：Outlook「类别」列是**用户自定义标签**，而库里 category 只有三个规范值。
#: 只映射「语义明确」的写法；命中不了的**原样保留**（截到 VARCHAR(32)）——
#: 丢标签比标签不归一更糟：用户按「大学同学」筛选时找不到人。
CATEGORY_ALIASES: dict = {
    # 家人
    "家人": CATEGORY_FAMILY, "家庭": CATEGORY_FAMILY, "家属": CATEGORY_FAMILY,
    "亲戚": CATEGORY_FAMILY, "亲人": CATEGORY_FAMILY, "家人组": CATEGORY_FAMILY,
    "family": CATEGORY_FAMILY, "families": CATEGORY_FAMILY,
    "relatives": CATEGORY_FAMILY, "home": CATEGORY_FAMILY,
    # 朋友
    "朋友": CATEGORY_FRIEND, "好友": CATEGORY_FRIEND, "好友们": CATEGORY_FRIEND,
    "friend": CATEGORY_FRIEND, "friends": CATEGORY_FRIEND,
    "friendship": CATEGORY_FRIEND,
    # 同事
    "同事": CATEGORY_COLLEAGUE, "同事们": CATEGORY_COLLEAGUE, "同学": CATEGORY_COLLEAGUE,
    "客户": CATEGORY_COLLEAGUE, "合作伙伴": CATEGORY_COLLEAGUE,
    "colleague": CATEGORY_COLLEAGUE, "colleagues": CATEGORY_COLLEAGUE,
    "co-worker": CATEGORY_COLLEAGUE, "coworker": CATEGORY_COLLEAGUE,
    "classmate": CATEGORY_COLLEAGUE, "client": CATEGORY_COLLEAGUE,
}

#: relation 归一表（vCard/Outlook 没有「家庭关系」标准列，导出方爱写中文口语）
RELATION_ALIASES: dict = {
    "父亲": RELATION_PARENT, "母亲": RELATION_PARENT, "爸爸": RELATION_PARENT,
    "妈妈": RELATION_PARENT, "父母": RELATION_PARENT, "长辈": RELATION_PARENT,
    "parent": RELATION_PARENT, "father": RELATION_PARENT, "mother": RELATION_PARENT,
    "配偶": RELATION_SPOUSE, "妻子": RELATION_SPOUSE, "丈夫": RELATION_SPOUSE,
    "太太": RELATION_SPOUSE, "先生": RELATION_SPOUSE,
    "spouse": RELATION_SPOUSE, "wife": RELATION_SPOUSE, "husband": RELATION_SPOUSE,
    "partner": RELATION_SPOUSE,
    "儿子": RELATION_CHILD, "女儿": RELATION_CHILD, "孩子": RELATION_CHILD,
    "子女": RELATION_CHILD,
    "child": RELATION_CHILD, "son": RELATION_CHILD, "daughter": RELATION_CHILD,
    "兄弟": RELATION_SIBLING, "姐妹": RELATION_SIBLING, "兄弟姐妹": RELATION_SIBLING,
    "sibling": RELATION_SIBLING, "brother": RELATION_SIBLING, "sister": RELATION_SIBLING,
}


def normalizeCategory(raw: str) -> str:
    """Outlook「类别」标签 -> 库里 category 的值。

    命中归一表 -> 规范值（family/friend/colleague）；
    未命中    -> **原样返回**（去首尾空白、截到 32 字），绝不返回空串。

    ⚠️ 不要在这里 raise 或返回 ""：一个没归一的标签仍然是一条有效信息，
       丢掉它等于「用户导了 10 个类别，库里只剩 3 个」。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    return CATEGORY_ALIASES.get(text.lower(), text[:32])


def normalizeRelation(raw: str) -> str:
    """关系列 -> parent/spouse/child/sibling；认不出来返回 ""（**不猜**）。"""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in RELATION_ALL:
        return text
    return RELATION_ALIASES.get(text, "")


if __name__ == "__main__":
    print("globalDefinition _VERSION:", _VERSION)
    print("RET_OK                  :", RET_OK, "/", errText(RET_OK))
    print("JOB_STATUS_ALL          :", JOB_STATUS_ALL)
    print("JOB PAUSED -> RUNNING ok :", canTransit(JOB_PAUSED, JOB_RUNNING))
    print("JOB DONE  -> RUNNING ok :", canTransit(JOB_DONE, JOB_RUNNING))
    print("JOB_TYPE_TEXT          :", JOB_TYPE_TEXT)
    print("样例 jobCode           :", makeJobCode(JOB_TYPE_SCAN), "/",
          makeJobCode(JOB_TYPE_FACE))
    print("SCAN_STATE_TEXT         :", SCAN_STATE_TEXT)
    print("DEL_FLAG_ALL            :", DEL_FLAG_ALL)
