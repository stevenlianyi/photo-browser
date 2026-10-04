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

_VERSION = "20261004"


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


if __name__ == "__main__":
    print("globalDefinition _VERSION:", _VERSION)
    print("RET_OK                  :", RET_OK, "/", errText(RET_OK))
    print("JOB_STATUS_ALL          :", JOB_STATUS_ALL)
    print("JOB PAUSED -> RUNNING ok :", canTransit(JOB_PAUSED, JOB_RUNNING))
    print("JOB DONE  -> RUNNING ok :", canTransit(JOB_DONE, JOB_RUNNING))
    print("SCAN_STATE_TEXT         :", SCAN_STATE_TEXT)
    print("DEL_FLAG_ALL            :", DEL_FLAG_ALL)
