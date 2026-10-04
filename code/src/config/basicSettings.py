#! /usr/bin/env python3
#encoding: utf-8

#Filename: basicSettings.py
#Description: photo-browser 全局业务参数集中配置（入库）
#
# 强约束：所有阈值 / 批大小 / 白名单一律在此声明，
#        **禁止散落在 processor / engine / api 等业务模块里硬编码**
#        （见 plan/开发计划.md §6.4 与本文件头部的"配置项集中"原则）。
#
# 分工：
#   basicSettings.py   ← 本文件：与机器无关的业务参数（入库）
#   local_settings.py  ← 跟本机有关的三个路径（不入库，有 .example 模板）
#   sqliteSettings.py  ← SQLite PRAGMA 常量与库文件装配

_VERSION = "20261004"


# ============================================================
# 一、路径默认值（local_settings.py 缺失时的兜底）
# ============================================================
# local_settings.py 不入库。新机器 clone 后若忘了复制 .example，
# paths.py 会退回下面三个默认值，保证程序仍能启动（不静默失败，打印 warning）。
# 命名规则固定为 DEFAULT_<配置项名>，供 paths.readSetting() 统一回退查找。

DEFAULT_PHOTO_ROOT: str = r"d:\PhotoLib"
DEFAULT_THUMB_ROOT: str = ""      # 空 = 派生 <PHOTO_ROOT>\thumb
DEFAULT_DB_FILE: str = ""         # 空 = 派生 <PHOTO_ROOT>\db\photolib.db


# ============================================================
# 二、扫描批次与限流
# ============================================================

# 一次扫描任务处理到该张数即 jobStatus=PAUSED 停止，等待用户「继续下一批」
BATCH_SIZE: int = 100

# 批内写库的事务提交粒度（每 N 行 executemany 提交一次）
COMMIT_ROWS_PER_TXN: int = 500


# ============================================================
# 三、人脸匹配阈值（S0 实测起点，勿信默认 0.65）
# ============================================================
# S0 结论：T_high=0.55 / T_low=0.35；FR 32.75% → 分桶后 ~19%
# 步骤 12 的设置页会暴露这两个值给用户调整，改动后需重算质心。

# 相似度 >= T_HIGH → 自动归属（无需人工确认）
T_HIGH: float = 0.55
# 相似度 <= T_LOW → 判为"库里没有这个人"，进聚类
# T_LOW < 相似度 < T_HIGH → 灰区，记 Top-5 候选进待确认队列（人工兜底）
T_LOW: float = 0.35

# 相邻年代桶候选数：取「本桶 + 前后各一桶」共三桶
MATCH_NEIGHBOR_BUCKETS: int = 1

# 桶内样本数下限：<该值不启用该桶质心（样本太少不可信）
MIN_CENTROID_SAMPLES: int = 3


# ============================================================
# 四、人脸质量过滤（任一不满足即丢弃，不入 pb_face）
# ============================================================

# SCRFD 检测置信度下限
MIN_DET_SCORE: float = 0.6
# 人脸框短边像素下限（太小则特征不可靠）
MIN_FACE_EDGE: int = 64
# 侧脸偏航角绝对值上限（度）；|yaw| > 45 的侧脸 embedding 偏移严重
MAX_YAW: int = 45

# ArcFace 特征维度与存储字节数（float32[512] 小端 = 2048 字节）
EMBEDDING_DIM: int = 512
EMBEDDING_BYTES: int = EMBEDDING_DIM * 4


# ============================================================
# 五、聚类（DBSCAN）
# ============================================================

DBSCAN_EPS: float = 0.45
DBSCAN_MIN_SAMPLES: int = 3


# ============================================================
# 六、照片扩展名白名单（扫描器只认这些，命中才入库）
# ============================================================
# 全部小写、含点。is_photo_file() 做判断，业务层不要自己写后缀匹配。
#
# ⚠️ **只管静态照片，不含视频**（wmv/mp4/mov/... 已刻意移除）。
#    理由：全项目12 步路线里没有任何视频环节——8 张表无视频字段、缩略图走 Pillow
#    解码、8 个 UI 页面无播放器。放进白名单只会让步骤 3 把视频塞进 pb_photo，
#    步骤 4 生成缩略图时再因解不出帧而炸，属自找麻烦。
#    将来真要支持视频，**另建VIDEO_EXTS 并同步改表/改 UI**，不要往这里加。
#
# 关于 .arf：疑为 .arw(Sony RAW) 的笔误，但**仍予保留** ——
#    白名单多一项的代价是零（永不匹配而已），漏一项的代价是照片被静默跳过。

PHOTO_EXTS: tuple = (
    # 常规位图
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".jxl",
    # RAW（只读不改写，绝不回写相机格式）
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".arf", ".raf", ".orf",
    ".rw2", ".pef", ".srw", ".3fr", ".erf", ".kdc", ".mos", ".mrw",
)

# frozenset 供 O(1) 查表；PHOTO_EXTS 供文档展示与遍历
PHOTO_EXT_SET: frozenset = frozenset(PHOTO_EXTS)

# 明确排除的目录名（扫描器跳过，不因扩展名误判）
EXCLUDED_DIR_NAMES: frozenset = frozenset({
    "@eaDir",          # 群晖缩略图目录
    ".thumbnails",
    "thumbs",
    "faces",
    "$RECYCLE.BIN",
    "System Volume Information",
})


def is_photo_file(fileName: str) -> bool:
    """按扩展名判断是否为受支持的照片文件（大小写不敏感）"""
    if not fileName:
        return False
    dot = fileName.rfind(".")
    if dot < 0:
        return False
    return fileName[dot:].lower() in PHOTO_EXT_SET


# ============================================================
# 七、文件 hash
# ============================================================

# 分块大小 8MB —— 绝不允许整读原图（单张 RAW 可达 60MB+）
HASH_CHUNK_SIZE: int = 8 * 1024 * 1024          # 8 * 1024 * 1024

# hash 算法：relPathHash（路径级去重）/ fileHash（内容级去重）统一用 SHA-256
HASH_ALGORITHM: str = "sha256"
# 库中 hash 字段的十六进制长度（CHAR(64)）
HASH_HEX_LEN: int = 64


# ============================================================
# 七之二、扫描器与 EXIF（步骤 3）
# ============================================================

# EXIF 的 DateTimeOriginal **不带时区**（EXIF 规范里就没这玩意儿），
# 只能按「拍摄地当时的本地时间」理解，再换算成 UTC 落 pb_photo.takenAt。
# 东八区 = 8；本机若不在东八区，改这一处即可。
EXIF_LOCAL_UTC_OFFSET_HOURS: float = 8.0

# shotYear 的可信区间：超出即视为「文件名误命中」，不采信
#（1990 年前的胶片扫件与 2100 的未来文件都不该出现在正常照片库里）
SHOT_YEAR_MIN: int = 1900
SHOT_YEAR_MAX: int = 2100

# 截图类文件名前缀（对 stem 做前缀匹配，大小写不敏感）
# 命中即 shotYear = NULL，**不参与跨年代桶比对**（截图的拍摄年份没有意义）
SCREENSHOT_NAME_PREFIXES: tuple = (
    "screenshot", "screen shot", "screencap", "screen recording",
    "截图", "截屏", "屏幕截图", "屏幕录制",
)

# 截图名是否连 EXIF 也不认。
#   False（**默认**）= EXIF 优先：只有「EXIF 也没有」时才当截图处理，shotYear=NULL。
#       理由：EXIF 的 DateTimeOriginal 是相机写的真实拍摄时间，比文件名可信；
#       误命名成 Screenshot_xxx.jpg 的真实照片不该丢掉EXIF 年份。
#   True  = 截图名压过 EXIF，一律 shotYear=NULL。
#       代价：少数带 EXIF 的截图（截图软件顺手写入了 EXIF）会把保存时间当年份。
SCREENSHOT_OVERRIDES_EXIF: bool = False

# 扫描索引加载的分页大小（**10 万行规模实测定的**，别随手改大）
#
# 为什么必须分页（实测数据，10 万行 pb_photo）
# ------------------------------------------
#   一次性 query_pb_photo 全量取行：峰值 **157.5 MB**，耗时 1.57 s
#   每页 2000 行分页取：  峰值 **  3.2 MB**，耗时 1.55 s   <- 内存 50 倍差，速度一样
#   （分页后紧凑索引本身常驻约 54 MB，那是判定必需的，不在优化范围）
# 一次性全取的那 150 MB 纯属「dict 列表的中间态」，白给。
# 2000 是实测拐点：再大内存线性涨，再小 SQLite 往返次数变多。
SCAN_INDEX_PAGE: int = 2000

# 每次扫描结束是否顺带做「库中存在但磁盘找不到」的缺失判定（默认开）。
# 关掉它可省掉每条库记录一次 stat（10 万条约 1 秒，实测 0.92 s），代价是 isMissing 永远不更新。
SCAN_MISSING_SWEEP: bool = True

# 每处理多少个文件写一次 pb_scan_job 进度（避免每张都写库）
PROGRESS_EVERY: int = 50

# 扫描任务默认归属人（pb_photo.ownerID / pb_scan_job.regID）。
# 步骤 9接API 后由登录态覆盖。
DEFAULT_OWNER_ID: str = "local"

# 扫描任务 jobCode 前缀（格式 SJ_<yyyymmddHHMMSS>_<6位随机>，VARCHAR(64) 内绰绰有余）
SCAN_JOB_CODE_PREFIX: str = "SJ"



# ============================================================
# 八、缩略图与人脸裁剪图（DR-1：落thumb\，路径可推导、不入库）
# ============================================================

# 缩略图宽度多尺寸（WebP）
THUMB_SIZES: tuple = (200, 400, 800)
# 缩略图格式与质量
THUMB_FORMAT: str = "WEBP"
THUMB_QUALITY: int = 82
# 人脸裁剪图边长与格式
FACE_CROP_SIZE: int = 160
FACE_CROP_FORMAT: str = "JPEG"
FACE_CROP_QUALITY: int = 85
# 人脸图裁成**正方形**（长=宽=FACE_CROP_SIZE）还是保持 bbox 长宽比
# 选 True：人员头像位按圆形遮罩渲染，方形图不必再由前端裁；斜脸/侧脸也不会
#         被拉成"人脑袋被压扁"的观感。选 False 则输出长边=160 的等比图。
FACE_CROP_SQUARE: bool = True

# 分桶子目录名（<thumb>\thumbs\<fileHash[:2]>\...、<thumb>\faces\<faceCode[:2]>\...）
THUMB_SUBDIR: str = "thumbs"
FACE_SUBDIR: str = "faces"
# 分桶取hash 前几位
HASH_BUCKET_LEN: int = 2

# 缩略图落盘的扩展名（**由路径推导决定，不能改**——改了等于换了一套路径规则，
# 库里又没有 thumbPath 字段，老缩略图会全部变成孤儿文件）
THUMB_EXT: str = ".webp"
FACE_EXT: str = ".jpg"

# 原子写的临时文件扩展名：先写 <name>.tmp，再 os.replace 覆盖正式名。
# 磁盘上**永远不允许**出现 *.tmp 被前端读到（验收第 5 条）。
TMP_EXT: str = ".tmp"

# 缩略图接口的缺省尺寸（?size= 不传时用它；必须是 THUMB_SIZES 成员）
THUMB_DEFAULT_SIZE: int = 400

# WebP 编码 method：0 最快最差 / 6 最慢最好。4 是「体积/速度」拐点，
# 400px 缩略图用它比 method=6 快 3-4 倍而体积只大3%~5%。
THUMB_WEBP_METHOD: int = 4


# ============================================================
# 八之二、步骤 4 服务参数（缩略图与原图文件服务）
# ============================================================

# /api/thumb 的 Cache-Control。
# 缩略图是**内容寻址**的（文件名 = fileHash + size，同名即同内容，且永不原地改写），
# 所以可以放心给足一年 + immutable：改 quality 只会换 ETag 之外的东西，
# 真要作废整批缩略图，删掉 thumb\thumbs\ 重生成即可（生成物，可随时重建）。
THUMB_CACHE_CONTROL: str = "public, max-age=31536000, immutable"

# 单张按需生成用的线程池大小。
# 为什么按需用线程池、批量用进程池：
#   * 批量是 CPU 解码密集（Pillow 解 JPEG 主体在 C 层但仍吃满一个核），
#     多进程才能真正并行 -> make_thumbs_bulk 用 ProcessPoolExecutor；
#   * 按需是「等前端要图」，同一时刻通常只有1~2 张，用线程池即可，
#     免得起进程池的冷启动（Windows spawn 一个进程 ~0.3s，比解码还慢）
#     —— 顺带把 Pillow 的 GIL 等待也错开了一些。
THUMB_ON_DEMAND_WORKERS: int = 4

# 批量生成时每个子进程任务携带的照片条数。
# 太大：单条失败就要整批重跑；太小：IPC 往返与 pickle 开销占比上升。16 是个稳的值。
THUMB_BULK_CHUNK: int = 16

# /api/original 流式回传原图时的读取块大小（Range 请求同样用它）。
# **绝不整读原图**（单张 RAW 可达 60MB+），也不整读缩略图。
STREAM_CHUNK_SIZE: int = 1024 * 1024          # 1 MB

# 原图 MIME 映射：mimetypes 在 Windows 注册表里对 .heic/.avif/.jxl 常查不到，
# 查不到就回退 application/octet-stream，浏览器会当下载而不是显示图 —— 必须自己兜住。
MIME_BY_EXT: dict = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif",
    ".avif": "image/avif", ".jxl": "image/jxl",
    # RAW 浏览器基本都不认，给个通用二进制类型（前端走 /api/original 下载原件）
    ".dng": "application/octet-stream", ".cr2": "application/octet-stream",
    ".cr3": "application/octet-stream", ".nef": "application/octet-stream",
    ".arw": "application/octet-stream", ".arf": "application/octet-stream",
    ".raf": "application/octet-stream", ".orf": "application/octet-stream",
    ".rw2": "application/octet-stream", ".pef": "application/octet-stream",
    ".srw": "application/octet-stream", ".3fr": "application/octet-stream",
    ".erf": "application/octet-stream", ".kdc": "application/octet-stream",
    ".mos": "application/octet-stream", ".mrw": "application/octet-stream",
}


# ============================================================
# 九、服务
# ============================================================

# ⚠️ 只绑 127.0.0.1，绝不 0.0.0.0（见开发计划 §一 硬约束表）
SERVER_HOST: str = "127.0.0.1"
SERVER_PORT: int = 8765


if __name__ == "__main__":
    print("BATCH_SIZE      :", BATCH_SIZE)
    print("T_HIGH / T_LOW  :", T_HIGH, "/", T_LOW)
    print("MIN_DET_SCORE   :", MIN_DET_SCORE)
    print("MIN_FACE_EDGE   :", MIN_FACE_EDGE)
    print("MAX_YAW         :", MAX_YAW)
    print("PHOTO_EXTS      :", len(PHOTO_EXTS), "个")
    print("HASH_CHUNK_SIZE :", HASH_CHUNK_SIZE, "(%d MB)" % (HASH_CHUNK_SIZE // 1024 // 1024))
    print("EXIF 时区偏移   :", EXIF_LOCAL_UTC_OFFSET_HOURS, "小时")
    print("截图压过 EXIF   :", SCREENSHOT_OVERRIDES_EXIF, "（False = EXIF 优先）")
